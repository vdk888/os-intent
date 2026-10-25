"""The ONE relevance judge: every proactive event weighed against the wiki.

Design goal: not a pile of tools, one effective judge that relies only on your knowledge base (the "wiki": a local
folder of markdown pages, see tools/wiki/wiki_search.py) to weigh the relevance of a context, a client, anything.

Any event (mail, agent message, file, web page, cockpit / metric / market) -> a short query from its non-private
fields (`query`: the mail sender's name, the name in the address, the company of a non-free-mail domain, the subject
or title; an agent / system event: its subject only, the agent's own name is not a signal) -> the warm local wiki
search oi already uses (oi/rag.py WarmWiki: tools/wiki/wiki_search.py's index, local embeddings + FTS, nothing leaves
the Mac) restricted to the pages that can make something important (`page_kind`, from the page frontmatter): an
active client, a prospect / lead / investor, a current project, a known person (type person). Important when one of
the TOP_K hits is NAMED by the event — a word of its file name (the wiki's own identifier of the thing) is in the
query and in at most MAX_DF_FRAC of the wiki's pages (the wiki itself says which words identify something:
"durand", "globex", not "acme"); a project page (descriptive file names) needs two such words — or when its meaning
matches strongly (cosine >= MIN_COS). Calibrated on real inbox headers: the cosine alone does not separate
(unrelated mails reach 0.68) and title words over-match (company forms such as "finance", "intelligence").

Sujet first: an event whose `ref.sujet` names a board sujet (a cockpit « À décider » item)
is judged on THAT sujet's wiki page when one exists — the page whose frontmatter declares `sujet: <slug>`, else the
page the cockpit linked — before any search (reason "sujet"). No page = the search below, as for any event.

Verdict: {"important": bool, "reason": "wiki" | "sujet" | None, "page": <wiki path> | None, "kind": client | prospect | project |
person | None, "snippet": "<title: description>" (<= 200 chars) | None}. The snippet stays in the event in memory (the
HUD's "why" later); logs and the events file keep the page name only (store.append). A private event (local-only
domain / leaks gate) is judged on its sender only (its subject is never stored) and gets no snippet.
Latency: the engine calls it on the sensor thread, never the HUD's; one cached verdict per query (sender + subject);
a search that takes more than TIMEOUT_S = not important (not cached: the next event asks again).

Follow-ups: a generic business word ("training", "formation", "facture", "projet" ...,
STOPWORDS) never names a page even when it is rare in the wiki (a client file named after a training must not catch
every training newsletter); a MAIL is important only when its SENDER (the person's name, the name in the address or
the company of the domain) names the page — a subject word alone never does, and a strong cosine also needs a sender
word in the page's file name or title. Circuit breaker: after BREAK_AFTER timeouts in a row the judge is skipped for
BREAK_S (not important) so a wedged search cannot serialize a burst of events behind it.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Callable

from .. import claude_actions as ca, rag
from ..orchestrator import fold
from .sensors import FLEET_DOMAINS

MIN_COS = 0.72                # meaning alone: calibrated on 40 real inbox headers
MAX_DF_FRAC = 0.05            # a word that NAMES a page: in its file name and in <= 5% of the wiki's pages
TOP_K = 3
TIMEOUT_S = 1.5
BREAK_AFTER = 3               # consecutive timeouts that open the circuit breaker
BREAK_S = 60.0                # how long the judge is skipped once it is open
CACHE = 512
SNIPPET = 200
KIND_TAGS = (("client", "client"), ("customer", "client"), ("prospect", "prospect"), ("lead", "prospect"),
             ("investor", "prospect"))
KIND_TYPES = {"project": "project", "person": "person", "client": "client", "prospect": "prospect"}
DEAD_STATUS = {"stale", "removed", "superseded", "archived", "completed", "done", "abandoned", "inactive", "lost"}
CLIENT_KINDS = ("client", "prospect")       # a mail whose sender matches one = class "client" (evt:mail:client)
# Generic business words (EN / FR, folded): never a page's identifier, however rare in the wiki.
STOPWORDS = frozenset("""
training trainings formation formations meeting meetings reunion reunions invoice invoices facture factures
newsletter newsletters update updates offer offers offre offres project projects projet projets report reports
rapport rapports webinar webinaire event events evenement invitation invite reminder rappel welcome bienvenue
order commande payment paiement receipt recu quote devis contract contrat proposal proposition review revue
team equipe news info infos information informations service services support account compte notification
digest weekly hebdo monthly mensuel annual annuel summary resume confirmation demande request
""".split())
NOT = {"important": False, "reason": None, "page": None, "kind": None, "snippet": None}


def words(text: str) -> set[str]:
    """The identifying words of a text: folded (accents off), >= 4 letters, not a generic business word."""
    return {x for x in re.split(r"[^a-z0-9]+", fold(text)) if len(x) >= 4 and x not in STOPWORDS}


def page_kind(path: str, fm: dict) -> str | None:
    """client | prospect | project | person, or None (not a page that makes an event important / not active)."""
    p = path.lower()
    if p.startswith(("archive/", "_archive/")) or "/archive/" in p:
        return None
    if str(fm.get("status") or "").strip().lower() in DEAD_STATUS:
        return None
    tags = {t.strip().strip("'\"").lower() for t in str(fm.get("tags") or "").strip("[]").split(",")}
    for tag, kind in KIND_TAGS:
        if tag in tags:
            return kind
    return KIND_TYPES.get(str(fm.get("type") or "").strip().lower())


def sender(ev: dict) -> str:
    """A mail's sender text: the name, the name in the address, the company of a non-free / non-fleet domain
    ("" for any other event)."""
    if ev.get("source") != "mail":
        return ""
    ref = ev.get("ref") or {}
    parts: list[str] = []
    addr = str(ref.get("address") or "").lower()
    local, dom = addr.partition("@")[0], ca.domain_of(addr)
    parts.append(str(ref.get("name") or ev.get("who") or ""))
    if re.search(r"[._-]", local):                              # firstname.lastname@ carries a name
        parts.append(local)
    if dom and dom not in ca.FREE_MAIL and not any(dom == f or dom.endswith("." + f) for f in FLEET_DOMAINS):
        parts.append(dom.rsplit(".", 1)[0])                      # the company: dupont-gestion.example -> dupont-gestion
    return _clean(parts)


def _clean(parts: list[str]) -> str:
    return " ".join(re.sub(r"[._@-]+", " ", " ".join(parts)).split())[:300]


def query(ev: dict) -> str:
    """The event's search text: non-private fields only (a private event has no subject: store.make). A meeting
    adds its attendees' names (their wiki pages = the meeting's prep context, level 3)."""
    who = str(ev.get("who") or "") if ev.get("source") == "calendar" and not ev.get("private") else ""
    return _clean([sender(ev), who, str(ev.get("subject") or "")])


class Judge:
    """`judge(ev)` -> the verdict (NOT on no wiki, no query, an error or a timeout). Thread-safe."""

    def __init__(self, wiki: Callable[[], "rag.WarmWiki | None"] | None = None, *, min_cos: float = MIN_COS,
                 timeout_s: float = TIMEOUT_S, clock: Callable[[], float] = time.monotonic):
        self.wiki = wiki or (lambda: rag.default())
        self.min_cos, self.timeout_s, self.clock = min_cos, timeout_s, clock
        self.timeouts = 0                               # consecutive timeouts (circuit breaker)
        self.open_until = float("-inf")
        self.cache: OrderedDict[str, dict] = OrderedDict()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oi-judge")
        self._kinds: tuple[int, dict[str, str]] = (0, {})

    def kinds(self, w) -> dict[str, str]:
        """{path: kind} of the pages that count (recomputed when the wiki's frontmatter map changes)."""
        meta = w.meta()
        if self._kinds[0] != id(meta):
            self._kinds = (id(meta), {p: k for p, fm in meta.items() if (k := page_kind(p, fm))})
        return self._kinds[1]

    def search(self, q: str, by: str | None = None) -> dict | None:
        """The verdict for one query; None when the wiki is not available (not cached: asked again next time).
        `by` = a mail's sender text: then one of its words must name the page (file name), or, for a strong cosine,
        be in the page's file name or title."""
        w = self.wiki()
        kinds = self.kinds(w) if w is not None else {}
        if not kinds:
            return None
        qw, sw = words(q), (words(by) if by is not None else None)
        rare = max(1, int(MAX_DF_FRAC * max(1, len(w.meta()))))
        for r in w.search(q, TOP_K, where=kinds.__contains__):
            kind = kinds[r["path"]]
            fw = words(r["path"].rsplit("/", 1)[-1].removesuffix(".md"))
            named = [x for x in qw & fw if w.doc_freq(x) <= rare]
            by_name = len(named) >= (2 if kind == "project" else 1)
            by_cos = (r.get("cos") or 0.0) >= self.min_cos
            if sw is not None:                          # a mail: the sender must name the page, never the subject alone
                by_name = by_name and bool(sw.intersection(named))
                by_cos = by_cos and bool(sw & (fw | words(str(r.get("title") or ""))))
            if by_name or by_cos:
                snip = f"{r['title']}: {r['description']}".strip(": ")[:SNIPPET]
                return {"important": True, "reason": "wiki", "page": r["path"], "kind": kind, "snippet": snip}
        return NOT

    def sujet_page(self, ev: dict) -> dict | None:
        """The verdict from the event's sujet page (exact, no search), or None (no sujet, no wiki, no such page)."""
        s = (ev.get("ref") or {}).get("sujet")
        if ev.get("private") or not isinstance(s, dict):
            return None
        w = self.wiki()
        meta = w.meta() if w is not None else {}
        slug = s.get("slug") if isinstance(s.get("slug"), str) else ""
        path = next((p for p, fm in meta.items() if slug and str(fm.get("sujet") or "").strip().lower() == slug), None)
        if path is None and isinstance(s.get("wiki"), str) and s["wiki"] in meta:
            path = s["wiki"]
        if path is None:
            return None
        fm = meta[path]
        snip = f"{fm.get('title') or path}: {fm.get('description') or ''}".strip(": ")[:SNIPPET]
        return {"important": True, "reason": "sujet", "page": path, "kind": page_kind(path, fm), "snippet": snip}

    def __call__(self, ev: dict) -> dict:
        try:
            v = self.sujet_page(ev)
        except Exception:                        # the search below still runs
            v = None
        if v is not None:
            return v
        q = query(ev)
        if not q:
            return dict(NOT)
        by = sender(ev) if ev.get("source") == "mail" else None
        key = f"mail\x1f{q}" if by is not None else q
        with self.lock:
            v = self.cache.get(key)
            if v is not None:
                self.cache.move_to_end(key)
        if v is None:
            if self.clock() < self.open_until:  # circuit open: a wedged search does not hold this event
                return dict(NOT)
            try:
                v = self.pool.submit(self.search, q, by).result(timeout=self.timeout_s)
            except FutureTimeout:
                with self.lock:
                    self.timeouts += 1
                    if self.timeouts >= BREAK_AFTER:
                        self.open_until, self.timeouts = self.clock() + BREAK_S, 0
                return dict(NOT)
            except Exception:                    # any failure: not important, not cached
                return dict(NOT)
            with self.lock:
                self.timeouts = 0
            if v is None:
                return dict(NOT)
            with self.lock:
                self.cache[key] = v
                while len(self.cache) > CACHE:
                    self.cache.popitem(last=False)
        return dict(v, snippet=None) if ev.get("private") else dict(v)


def apply(ev: dict, judge: Callable[[dict], dict]) -> dict:
    """Judge a new (appear) event in place: `ev["wiki"]` = the verdict when important; a mail from a client /
    prospect page becomes class "client" (its token `evt:mail:client`; the sensor's live copy is this same dict, so
    its gone row follows). Returns the verdict."""
    v = judge(ev)
    if v.get("important"):
        ev["wiki"] = v
        if ev.get("source") == "mail" and ev.get("class") == "other" and v.get("kind") in CLIENT_KINDS:
            ev["class"] = "client"
    return v
