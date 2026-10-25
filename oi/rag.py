"""W25: the warm, in-process wiki search the orchestrator's context pack uses (budget ≤ 300 ms p50).

The user: "un pipeline de RAG … la requête du wiki, très rapidement".

Same index and same hybrid scoring as tools/wiki/wiki_search.py (nomic-embed-text through the local Ollama +
SQLite FTS5, 0.7 vector / 0.3 keyword, min-max normalised): its functions are imported (`search_keyword`,
`extract_*`), never re-implemented, and never run as a subprocess per call. What is warm:
  - the SQLite connection, opened READ-ONLY (`mode=ro`): the HUD never writes the index (wiki_search.py's own
    runs freshen it);
  - every page embedding, decoded once and L2-normalised in memory (≈ 1.2k pages x 768 floats); reloaded when the
    db file changes (checked at most every RELOAD_CHECK_S);
  - one keep-alive HTTP connection to Ollama for the query embedding (Ollama keeps the model loaded) + an LRU of
    query embeddings, so a repeated context costs no embedding at all.
No other model is loaded. Any failure = [] (the pack goes on without wiki notes); no Ollama = keyword-only.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import math
import os
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

DEFAULT_REPO = str(Path(__file__).resolve().parents[1])   # the os-intent checkout (it holds tools/wiki/)
OLLAMA_HOST, OLLAMA_PORT = "localhost", 11434
EMBED_TIMEOUT_S = 2.0
RELOAD_CHECK_S = 30.0
QUERY_CACHE = 256
VECTOR_WEIGHT, KEYWORD_WEIGHT = 0.7, 0.3


def load_wiki_search(repo_dir: str | None = None):
    """tools/wiki/wiki_search.py as a module (imported by path, once per process)."""
    path = Path(repo_dir or DEFAULT_REPO) / "tools/wiki/wiki_search.py"
    name = "oi_wiki_search"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[name] = mod
    return mod


class OllamaEmbedder:
    """nomic-embed-text on the local Ollama, one keep-alive connection (re-opened on error)."""

    def __init__(self, model: str = "nomic-embed-text", host: str = OLLAMA_HOST, port: int = OLLAMA_PORT,
                 timeout: float = EMBED_TIMEOUT_S):
        self.model, self.host, self.port, self.timeout = model, host, port, timeout
        self.conn: http.client.HTTPConnection | None = None
        self.lock = threading.Lock()

    def __call__(self, text: str) -> list[float]:
        body = json.dumps({"model": self.model, "prompt": text[:2000]})
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.conn is None:
                        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
                    self.conn.request("POST", "/api/embeddings", body, {"Content-Type": "application/json"})
                    r = self.conn.getresponse()
                    data = r.read()
                    if r.status != 200:
                        return []
                    emb = json.loads(data).get("embedding")
                    return emb if isinstance(emb, list) else []
                except (OSError, http.client.HTTPException, ValueError):
                    if self.conn is not None:
                        self.conn.close()
                    self.conn = None
                    if attempt:
                        return []
        return []


def _unit(v) -> list[float] | None:
    n = math.sqrt(sum(x * x for x in v)) if v else 0.0
    return [x / n for x in v] if n else None


class WarmWiki:
    """Hybrid wiki search kept warm in the `oi live` process. `search(q, k)` -> [{path, title, description,
    domain, score, cos}], best first (`score`: the hybrid one, min-max normalised per query; `cos`: the raw cosine,
    None when the page came from the keyword side only). Thread-safe (one lock around the connection and the matrix)."""

    def __init__(self, db_path: str | Path | None = None, *, repo_dir: str | None = None,
                 embedder: Callable[[str], list[float]] | None = None, module=None,
                 clock: Callable[[], float] = time.monotonic):
        self.ws = module or load_wiki_search(repo_dir)
        self.db_path = Path(db_path or self.ws.DB_PATH)
        self.embed = embedder or OllamaEmbedder(getattr(self.ws, "EMBED_MODEL", "nomic-embed-text"))
        self.clock = clock
        self.lock = threading.Lock()
        self.conn: sqlite3.Connection | None = None
        self.rows: list[tuple[str, str, str, str]] = []      # (path, title, description, domain)
        self.vecs: list[list[float]] = []
        self.mtime = -1.0
        self.checked = -1e9
        self.qcache: OrderedDict[str, list[float] | None] = OrderedDict()
        self._meta: dict[str, dict] = {}
        self._meta_at: float | None = None
        self._df: dict[str, int] = {}
        self._df_at: float | None = None

    # -- the warm state ----------------------------------------------------------------------------------
    def _open(self) -> None:
        if self.conn is None:
            self.conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)

    def _refresh(self) -> None:
        now = self.clock()
        if self.rows and now - self.checked < RELOAD_CHECK_S:
            return
        self.checked = now
        try:
            m = os.stat(self.db_path).st_mtime
            wal = self.db_path.with_name(self.db_path.name + "-wal")
            if wal.exists():
                m = max(m, os.stat(wal).st_mtime)
        except OSError:
            return
        if m == self.mtime and self.rows:
            return
        self._open()
        rows, vecs = [], []
        for path, title, desc, domain, blob in self.conn.execute(
                "SELECT path, title, description, domain, embedding FROM pages"):
            try:
                v = _unit(json.loads(blob)) if blob else None
            except ValueError:
                v = None
            rows.append((path, title or path, desc or "", domain or ""))
            vecs.append(v or [])
        self.rows, self.vecs, self.mtime = rows, vecs, m

    def warm(self) -> int:
        """Load the index (and the Ollama model through one embedding). The number of pages."""
        with self.lock:
            self._refresh()
        self.query_vec("warm up")
        return len(self.rows)

    def query_vec(self, q: str) -> list[float] | None:
        with self.lock:
            if q in self.qcache:
                self.qcache.move_to_end(q)
                return self.qcache[q]
        v = _unit(self.embed(q) or [])
        if v is None:                                    # Ollama down / empty: not cached, retried next query
            return None
        with self.lock:
            self.qcache[q] = v
            while len(self.qcache) > QUERY_CACHE:
                self.qcache.popitem(last=False)
        return v

    # -- the search (wiki_search.search_hybrid's scoring, on the warm matrix) ------------------------------------
    def search(self, query: str, k: int = 3, *, where: Callable[[str], bool] | None = None) -> list[dict]:
        """`where(path)`: keep only those pages (e.g. people/ client pages)."""
        q = " ".join((query or "").split())[:500]
        if not q:
            return []
        qv = self.query_vec(q)
        try:
            with self.lock:
                self._refresh()
                if not self.rows:
                    return []
                vec = []
                if qv:
                    for i, v in enumerate(self.vecs):
                        if v and (where is None or where(self.rows[i][0])):
                            vec.append((sum(map(float.__mul__, qv, v)), i))
                    vec.sort(reverse=True)
                    vec = vec[:k * 2]
                kw = self.ws.search_keyword(self.conn, q, top_k=k * 6 if where else k * 2)
        except sqlite3.Error:
            return []
        by_path = {r[0]: i for i, r in enumerate(self.rows)}
        kw = [(s, p) for s, p, *_ in kw if p in by_path and (where is None or where(p))][:k * 2]
        scores: dict[int, list[float]] = {}
        cos = {i: c for c, i in vec}                     # the raw cosine: an absolute score (the hybrid one is relative)
        if vec:
            hi, lo = vec[0][0], vec[-1][0]
            rng = (hi - lo) or 1.0
            for s, i in vec:
                scores[i] = [(s - lo) / rng, 0.0]
        if kw:
            hi, lo = max(s for s, _ in kw), min(s for s, _ in kw)
            rng = (hi - lo) or 1.0
            for s, p in kw:
                scores.setdefault(by_path[p], [0.0, 0.0])[1] = (s - lo) / rng
        ranked = sorted(((VECTOR_WEIGHT * a + KEYWORD_WEIGHT * b, i) for i, (a, b) in scores.items()),
                        key=lambda x: (-x[0], self.rows[x[1]][0]))
        out = []
        for s, i in ranked[:k]:
            path, title, desc, domain = self.rows[i]
            out.append({"path": path, "title": title, "description": desc, "domain": domain, "score": round(s, 3),
                        "cos": round(cos[i], 3) if i in cos else None})
        return out

    def meta(self) -> dict[str, dict]:
        """{path: frontmatter} of every page (wiki_search.extract_frontmatter: flat `key: value` strings), re-read when
        the index changes. {} on any error."""
        try:
            with self.lock:
                self._refresh()
                if self._meta_at != self.mtime:
                    self._open()
                    self._meta = {p: self.ws.extract_frontmatter(c or "") for p, c in
                                  self.conn.execute("SELECT path, substr(content, 1, 3000) FROM pages")}
                    self._meta_at = self.mtime
                return self._meta
        except sqlite3.Error:
            return {}

    def doc_freq(self, word: str) -> int:
        """In how many pages `word` appears (FTS5; cached until the index changes). Large on any error."""
        with self.lock:
            if self._df_at != self.mtime:
                self._df, self._df_at = {}, self.mtime
            if word in self._df:
                return self._df[word]
            try:
                self._open()
                n = self.conn.execute("SELECT count(*) FROM pages_fts WHERE pages_fts MATCH ?",
                                      (f'"{word}"',)).fetchone()[0]
            except sqlite3.Error:
                return 1 << 30
            self._df[word] = n
            return n

    def page_text(self, path: str, limit: int = 1200) -> str:
        """A page's stored content without its frontmatter / compile markers (≤ `limit` chars)."""
        try:
            with self.lock:
                self._open()
                row = self.conn.execute("SELECT content FROM pages WHERE path = ?", (path,)).fetchone()
        except sqlite3.Error:
            return ""
        text = (row or [""])[0] or ""
        if text.startswith("---"):
            end = text.find("\n---", 3)
            text = text[end + 4:] if end > 0 else text
        lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("<!--")]
        return "\n".join(lines)[:limit]


_DEFAULT: WarmWiki | None = None
_DEFAULT_LOCK = threading.Lock()


def default(repo_dir: str | None = None) -> WarmWiki | None:
    """The process-wide warm index (None when the wiki tooling or its db is missing)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            try:
                w = WarmWiki(repo_dir=repo_dir)
                if not w.db_path.is_file():
                    return None
                _DEFAULT = w
            except (ImportError, OSError, SyntaxError, AttributeError):
                return None
        return _DEFAULT


def snippets(query: str, k: int = 3, repo_dir: str | None = None) -> list[str] | None:
    """W14's "title: description" lines from the warm index; None when the warm index is not available."""
    w = default(repo_dir)
    if w is None:
        return None
    try:
        rows = w.search(query, k)
    except Exception:
        return None
    return [f"{r['title']}: {r['description']}".strip()[:300] for r in rows]


# ---- `python -m oi.rag bench` -----------------------------------------------------------------------------

BENCH_QUERIES = ["fund NAV this month", "reply to a client about an audit", "LinkedIn prospection follow-up",
                 "github pull request merge review", "weekly team meeting notes", "agent inbox delivery",
                 "invoice and VAT question", "client deployment release", "wiki search embeddings",
                 "broker execution gate"]


def _main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m oi.rag", description="warm wiki search (W25)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bench", help="cold load + 10 queries: p50 / p95 latency (prints titles only with --show)")
    b.add_argument("--repo-dir", default=None)
    b.add_argument("--show", action="store_true")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--top", type=int, default=3)
    a = ap.parse_args(argv)
    w = WarmWiki(repo_dir=getattr(a, "repo_dir", None))
    t0 = time.perf_counter()
    n = w.warm()
    load_ms = (time.perf_counter() - t0) * 1000
    if a.cmd == "search":
        print(json.dumps(w.search(a.query, a.top), ensure_ascii=False, indent=1))
        return 0
    lat = []
    for q in BENCH_QUERIES:
        t = time.perf_counter()
        r = w.search(q, 3)
        lat.append((time.perf_counter() - t) * 1000)
        if a.show:
            print(f"{lat[-1]:6.1f} ms  {q!r} -> {[x['path'] for x in r]}")
    lat.sort()
    p = lambda f: lat[min(len(lat) - 1, math.ceil(f * len(lat)) - 1)]
    print(json.dumps({"pages": n, "load_ms": round(load_ms), "queries": len(lat), "p50_ms": round(p(0.5), 1),
                      "p95_ms": round(p(0.95), 1), "max_ms": round(lat[-1], 1)}))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
