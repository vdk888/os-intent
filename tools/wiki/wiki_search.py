#!/usr/bin/env python3
"""
Wiki Semantic Search — local hybrid search over the shared wiki.

Uses Ollama nomic-embed-text for embeddings (local, free) + SQLite FTS5 for keyword search.
Combines both scores for hybrid retrieval.

Usage:
  python3 wiki_search.py "How does the release process work?"
  python3 wiki_search.py "telegram bot tokens" --top 5
  python3 wiki_search.py "portfolio rebalancing" --keyword-only
  python3 wiki_search.py --reindex   # rebuild the index

Wiki folder: $OI_WIKI_DIR (default ~/wiki). Index: $OI_WIKI_DB (default ~/.local/share/os-intent/wiki-search.db).
Auto-freshened at query time: any wiki page whose mtime is newer than its indexed_at
(plus new pages, minus deleted ones) is re-embedded incrementally before the search runs,
so results are never stale. A full rebuild is still available with --reindex.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
WIKI_DIR = Path(os.environ.get("OI_WIKI_DIR") or HOME / "wiki").expanduser()
DB_PATH = Path(os.environ.get("OI_WIKI_DB") or HOME / ".local/share/os-intent/wiki-search.db").expanduser()
OLLAMA_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768

_UPSERT_PAGE_SQL = """
    INSERT INTO pages (
        path, title, description, content, domain, embedding, indexed_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(path) DO UPDATE SET
        title = excluded.title,
        description = excluded.description,
        content = excluded.content,
        domain = excluded.domain,
        embedding = excluded.embedding,
        indexed_at = excluded.indexed_at
"""


def get_embedding(text: str) -> list:
    """Get embedding from local Ollama."""
    data = json.dumps({"model": EMBED_MODEL, "prompt": text[:2000]}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())["embedding"]
    except Exception as e:
        print(f"Ollama error: {e}", file=sys.stderr)
        return []


def cosine_sim(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0


def extract_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from markdown."""
    fm = {}
    match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
    if match:
        for line in match.group(1).split("\n"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                fm[parts[0].strip()] = parts[1].strip().strip("'\"")
    return fm


def extract_title(content: str, path: str) -> str:
    """Extract page title from frontmatter or first heading."""
    fm = extract_frontmatter(content)
    if "title" in fm:
        return fm["title"]
    if "name" in fm:
        return fm["name"]
    for line in content.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return path


def extract_description(content: str) -> str:
    """Extract description from frontmatter or first paragraph."""
    fm = extract_frontmatter(content)
    if "description" in fm:
        return fm["description"]
    # First non-empty, non-frontmatter, non-heading line
    in_fm = False
    for line in content.split("\n"):
        if line.strip() == "---":
            in_fm = not in_fm
            continue
        if in_fm:
            continue
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("|") and len(line) > 20:
            return line[:200]
    return ""


def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize SQLite database with FTS5 for keyword search."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pages (
            path TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            content TEXT,
            domain TEXT,
            embedding BLOB,
            indexed_at REAL
        )
    """)
    # FTS5 virtual table for keyword search
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            path, title, description, content,
            content='pages',
            content_rowid='rowid'
        )
    """)
    # Triggers to keep FTS in sync
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, path, title, description, content)
            VALUES (new.rowid, new.path, new.title, new.description, new.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, path, title, description, content)
            VALUES ('delete', old.rowid, old.path, old.title, old.description, old.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, path, title, description, content)
            VALUES ('delete', old.rowid, old.path, old.title, old.description, old.content);
            INSERT INTO pages_fts(rowid, path, title, description, content)
            VALUES (new.rowid, new.path, new.title, new.description, new.content);
        END
    """)
    conn.commit()
    return conn


def iter_wiki_files():
    """Yield every indexable wiki .md path (skips archive)."""
    for f in sorted(WIKI_DIR.rglob("*.md")):
        if "archive" in str(f):
            continue
        yield f


def upsert_page(conn: sqlite3.Connection, f: Path) -> bool:
    """Embed + upsert a single wiki page. Returns True on success, False if no embedding."""
    rel = str(f.relative_to(WIKI_DIR))
    content = f.read_text()
    title = extract_title(content, rel)
    desc = extract_description(content)
    domain = rel.split("/")[0] if "/" in rel else "root"
    # Embed title + description + first 500 chars of content
    embed_text = f"{title}\n{desc}\n{content[:500]}"
    embedding = get_embedding(embed_text)
    if not embedding:
        return False
    emb_blob = json.dumps(embedding).encode()
    # Keep the existing rowid on conflict so the external-content FTS table and
    # its UPDATE trigger continue to reference the same content row. SQLite's
    # INSERT OR REPLACE performs an implicit DELETE whose trigger is skipped
    # with the default recursive_triggers=OFF, leaving orphan FTS rowids.
    conn.execute(
        _UPSERT_PAGE_SQL,
        (rel, title, desc, content[:5000], domain, emb_blob, time.time()),
    )
    return True


def freshen_index(conn: sqlite3.Connection, verbose: bool = False) -> int:
    """Incrementally bring the index up to date WITHOUT a full rebuild.

    Re-embeds only pages whose file mtime is newer than their stored indexed_at,
    adds new pages, and drops pages whose file was deleted. Cheap no-op (a stat
    per page) when nothing changed — so it's safe to run before every query.
    Returns the number of pages touched (0 = index already fresh)."""
    stored = dict(conn.execute("SELECT path, indexed_at FROM pages").fetchall())
    on_disk = {str(f.relative_to(WIKI_DIR)): f for f in iter_wiki_files()}

    removed = [p for p in stored if p not in on_disk]
    changed = [(rel, f) for rel, f in on_disk.items()
               if stored.get(rel) is None or f.stat().st_mtime > stored[rel]]

    if not removed and not changed:
        return 0

    if verbose:
        print(f"Freshening wiki index: {len(changed)} new/changed, {len(removed)} removed...",
              file=sys.stderr)

    for rel in removed:
        conn.execute("DELETE FROM pages WHERE path = ?", (rel,))

    embedded = 0
    for rel, f in changed:
        if upsert_page(conn, f):
            embedded += 1
        elif verbose:
            print(f"  SKIP (no embedding, left stale): {rel}", file=sys.stderr)

    conn.commit()
    return embedded + len(removed)


def reindex(conn: sqlite3.Connection):
    """Reindex all wiki pages."""
    print("Scanning wiki pages...")
    pages = []
    for f in iter_wiki_files():
        rel = str(f.relative_to(WIKI_DIR))
        content = f.read_text()
        title = extract_title(content, rel)
        desc = extract_description(content)
        domain = rel.split("/")[0] if "/" in rel else "root"
        pages.append((rel, title, desc, content, domain))

    print(f"Embedding {len(pages)} pages...")
    start = time.time()

    # Clear existing data
    conn.execute("DELETE FROM pages")
    conn.execute("INSERT INTO pages_fts(pages_fts) VALUES('delete-all')")
    conn.commit()

    for rel, title, desc, content, domain in pages:
        # Embed title + description + first 500 chars of content
        embed_text = f"{title}\n{desc}\n{content[:500]}"
        embedding = get_embedding(embed_text)
        if not embedding:
            print(f"  SKIP (no embedding): {rel}")
            continue

        emb_blob = json.dumps(embedding).encode()
        conn.execute(
            _UPSERT_PAGE_SQL,
            (rel, title, desc, content[:5000], domain, emb_blob, time.time()),
        )

    conn.commit()
    elapsed = time.time() - start
    print(f"Indexed {len(pages)} pages in {elapsed:.1f}s")


def search_vector(conn: sqlite3.Connection, query_emb: list, top_k: int = 5) -> list:
    """Vector similarity search."""
    results = []
    rows = conn.execute("SELECT path, title, description, domain, embedding FROM pages").fetchall()
    for path, title, desc, domain, emb_blob in rows:
        emb = json.loads(emb_blob)
        score = cosine_sim(query_emb, emb)
        results.append((score, path, title, desc, domain))
    results.sort(key=lambda x: -x[0])
    return results[:top_k]


def search_keyword(conn: sqlite3.Connection, query: str, top_k: int = 5) -> list:
    """FTS5 BM25 keyword search."""
    # Escape special FTS5 characters
    safe_query = re.sub(r'[^\w\s]', ' ', query)
    tokens = safe_query.split()
    if not tokens:
        return []
    fts_query = " OR ".join(tokens)
    try:
        rows = conn.execute(
            """SELECT path, title, description,
                      rank * -1 as score
               FROM pages_fts
               WHERE pages_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (fts_query, top_k)
        ).fetchall()
        return [(row[3], row[0], row[1], row[2], "") for row in rows]
    except sqlite3.OperationalError:
        return []


def search_hybrid(conn: sqlite3.Connection, query: str, top_k: int = 5,
                  vector_weight: float = 0.7, keyword_weight: float = 0.3) -> list:
    """Hybrid search: weighted combination of vector + keyword scores."""
    query_emb = get_embedding(query)

    vec_results = search_vector(conn, query_emb, top_k=top_k * 2) if query_emb else []
    kw_results = search_keyword(conn, query, top_k=top_k * 2)

    # Normalize scores to 0-1 range
    if vec_results:
        max_vec = max(r[0] for r in vec_results)
        min_vec = min(r[0] for r in vec_results)
        range_vec = max_vec - min_vec if max_vec != min_vec else 1
    if kw_results:
        max_kw = max(r[0] for r in kw_results)
        min_kw = min(r[0] for r in kw_results)
        range_kw = max_kw - min_kw if max_kw != min_kw else 1

    # Merge scores by path
    scores = {}
    for score, path, title, desc, domain in vec_results:
        norm = (score - min_vec) / range_vec if vec_results else 0
        scores[path] = {"vec": norm, "kw": 0, "title": title, "desc": desc, "domain": domain}

    for score, path, title, desc, _ in kw_results:
        norm = (score - min_kw) / range_kw if kw_results else 0
        if path in scores:
            scores[path]["kw"] = norm
        else:
            scores[path] = {"vec": 0, "kw": norm, "title": title, "desc": desc, "domain": ""}

    # Weighted combination
    ranked = []
    for path, s in scores.items():
        combined = s["vec"] * vector_weight + s["kw"] * keyword_weight
        ranked.append((combined, path, s["title"], s["desc"], s["domain"]))

    ranked.sort(key=lambda x: -x[0])
    return ranked[:top_k]


def format_results(results: list, show_snippet: bool = True) -> str:
    """Format search results for display."""
    if not results:
        return "No results found."
    lines = []
    for i, (score, path, title, desc, domain) in enumerate(results, 1):
        lines.append(f"{i}. [{domain}] {title}")
        lines.append(f"   Path: {path}")
        if desc and show_snippet:
            lines.append(f"   {desc[:150]}")
        lines.append(f"   Score: {score:.3f}")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Wiki semantic search")
    parser.add_argument("query", nargs="*", help="Search query")
    parser.add_argument("--reindex", action="store_true", help="Rebuild the search index")
    parser.add_argument("--top", type=int, default=5, help="Number of results")
    parser.add_argument("--keyword-only", action="store_true", help="Keyword search only (no embeddings)")
    parser.add_argument("--vector-only", action="store_true", help="Vector search only (no keywords)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    conn = init_db(DB_PATH)

    if args.reindex:
        reindex(conn)
        return

    query = " ".join(args.query)
    if not query:
        # Check if index exists
        count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        if count == 0:
            print("Index is empty. Run: python3 wiki_search.py --reindex")
        else:
            print(f"Wiki search index: {count} pages. Pass a query to search.")
        return

    # Check if index exists
    count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    if count == 0:
        print("Index is empty. Building now...", file=sys.stderr)
        reindex(conn)
    else:
        # Auto-freshen: re-embed only pages changed since last index (and add new / drop
        # deleted ones). Keeps results current without a full nightly rebuild. Messages go
        # to stderr so --json stdout stays clean.
        freshen_index(conn, verbose=True)

    if args.keyword_only:
        results = search_keyword(conn, query, args.top)
    elif args.vector_only:
        query_emb = get_embedding(query)
        results = search_vector(conn, query_emb, args.top) if query_emb else []
    else:
        results = search_hybrid(conn, query, args.top)

    if args.json:
        out = [{"score": s, "path": p, "title": t, "description": d, "domain": dm}
               for s, p, t, d, dm in results]
        print(json.dumps(out, indent=2))
    else:
        print(format_results(results))

    conn.close()


if __name__ == "__main__":
    main()
