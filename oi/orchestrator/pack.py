"""W25: the context pack — what the runtime model knows about the moment (≤ 1.5k tokens).

Sections, most important first (a section that does not fit the budget is cut, then dropped):
  Now          front app, page host, desk, Paris time (per call, never cached)
  Dispatch     the armed "pass me to" agent (oi.orchestrator.dispatch), when any
  Talking to   the agent the moment is about (glide under an agent, a screenshot note), from the roster
  Path         the glide's path chain ("Mail > Forward to Nova")
  Mail sender  the selected mail's sender + subject, and their wiki people / client page (excerpt)
  Wiki notes   top-3 wiki snippets for the moment (oi.rag warm search: app, host, subject, path, wish)
  Habits       "from <context> the user usually goes next to …" (the last HABIT_DAYS days of tokens)
  After event  W24: "after a mail event (client) the user usually: …" (Markov counts at the proactive event token)
  Events       W24: the recent proactive events ("18:02 mail (client) from Sam: <subject>"; a gated one =
               "details withheld"); a line naming a local-only term is dropped

Privacy gate (settings.claude.local_only_domains, the W14/W15 rule): a local-only page host is dropped (from the
pack AND from the RAG query); a local-only mail (sender, cc or reply-to) gives only "a mail is selected (details
withheld)" — no sender, no subject, no people page, nothing of it in the query; a path chain, wish or wiki text
naming a local-only domain is dropped ("Acme-Conseil", "AcmeConseil", "acme_conseil SAS" all count, see leaks). The pack itself is never written to disk (debug files aside: claude.debug).

Speed: the RAG runs under a deadline (RAG_BUDGET_S, default 0.3 s; the warm index answers in ~60-80 ms); a late
search leaves the wiki out and is not cached. Cached per context key (app, host, desk, mail, path, agent, wish,
gates) for CACHE_TTL_S. Logged: latencies, token estimate, cache hit — never the text.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .. import claude_actions as ca, paths, rag
from . import dispatch, fold, roster

MAX_TOKENS = 1500
CHARS_PER_TOKEN = 3.5                   # conservative for FR/EN prose (over-counts a little)
RAG_BUDGET_S = 0.3
CACHE_TTL_S = 120
CACHE_SIZE = 64
HABIT_DAYS = 7
HABIT_TTL_S = 600
HABIT_TOP = 4
PEOPLE_PREFIXES = ("shared/people/",)
PARIS = ZoneInfo("Europe/Paris")
CAPS = {"Now": 200, "Dispatch": 160, "Talking to": 300, "Path": 300, "Mail sender": 1000, "Wiki notes": 1200,
        "Habits": 400, "After event": 300, "Events": 600}


def tokens(text: str) -> int:
    return math.ceil(len(text or "") / CHARS_PER_TOKEN)


def _words(text: str) -> str:
    """fold() with "-" and "_" as separators too: "Acme-Conseil" / "acme_conseil" -> "acme conseil"."""
    return " ".join(re.sub(r"[-_]", " ", fold(text)).split())


def _compact(text: str) -> str:
    """Letters and digits only: "Acme-Conseil SAS" / "AcmeConseil" -> "acmeconseilsas" / "acmeconseil"."""
    return re.sub(r"[\W_]", "", fold(text))


def deny_terms(cfg: dict, withheld: dict | None = None) -> list[str]:
    """What a pack line must not name: each local-only domain, its first label ("acme-conseil" -> "acme conseil")
    and that label's parts of ≥ 4 chars ("acme", "conseil"); plus the name words of a withheld mail's sender."""
    out: list[str] = []
    for d in cfg.get("local_only_domains") or []:
        label = d.split(".")[0]
        out += [d, _words(label)] + [_words(x) for x in re.split(r"[-_]", label) if len(x) >= 4]
    for a in [(withheld or {}).get("sender_address"), (withheld or {}).get("reply_to"), *((withheld or {}).get("cc") or [])]:
        if isinstance(a, str) and "@" in a:
            out.append(a.lower())
    out += [fold(w) for w in ((withheld or {}).get("sender_name") or "").split() if len(w) >= 4]
    return [t for t in dict.fromkeys(out) if t]


def leaks(text: str, cfg: dict, terms: list[str] | None = None) -> bool:
    """True when `text` names a denied term (deny_terms: a domain as a substring, the words as whole words, "-" / "_"
    being separators). A multi-word term is also caught glued ("AcmeConseil", "acmeconseil.com"): its
    separator-stripped form as a substring of the separator-stripped text."""
    terms = terms if terms is not None else deny_terms(cfg)
    if not terms or not text:
        return False
    low, words, flat = text.lower(), f" {_words(text)} ", _compact(text)
    return any((t in low) if ("." in t or "@" in t) else
               (f" {t} " in words or (" " in t and _compact(t) in flat)) for t in terms)


# ---- habits (tokens) --------------------------------------------------------------------------------------

def _shown(tok: str) -> str | None:
    """A token as words for the habits line (actions only: apps, sites, desks, vocals; never a title)."""
    kind, _, arg = tok.partition(":")
    if kind == "activate" and arg:
        return arg
    if kind == "url" and arg:
        return arg.split("/", 1)[0]
    if kind == "vocal" and arg:
        return f"a vocal to {arg}"
    if kind == "space" and arg:
        return f"desk {arg.lstrip('=')}"
    return None


def _ctx_of(tok: str) -> str | None:
    kind, _, arg = tok.partition(":")
    if kind == "activate" and arg:
        return f"app:{arg}"
    if kind == "url" and arg:
        return f"host:{arg.split('/', 1)[0]}"
    return None


class Habits:
    """Next-action counts per context (app / host) over the last HABIT_DAYS of tokens, re-read every HABIT_TTL_S."""

    def __init__(self, days: int = HABIT_DAYS, clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], date] = date.today):
        self.days, self.clock, self.today = days, clock, today
        self.counts: dict[str, Counter] = {}
        self.at = -1e9
        self.lock = threading.Lock()

    def _load(self) -> dict[str, Counter]:
        counts: dict[str, Counter] = defaultdict(Counter)
        d0 = self.today()
        for i in range(self.days):
            f = paths.tokens_dir() / f"{(d0 - timedelta(days=i)).isoformat()}.jsonl"
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            prev = None
            for ln in lines:
                try:
                    tok = json.loads(ln).get("token") or ""
                except (ValueError, AttributeError):
                    continue
                if _shown(tok) is None:
                    continue
                if prev and prev != tok:
                    c = _ctx_of(prev)
                    if c:
                        counts[c][tok] += 1
                prev = tok
        return dict(counts)

    def line(self, app: str | None, host: str | None, cfg: dict) -> str:
        now = self.clock()
        with self.lock:
            if now - self.at >= HABIT_TTL_S:
                self.counts, self.at = self._load(), now
            counts = self.counts
        key, where = (f"host:{host}", host) if host and counts.get(f"host:{host}") else (f"app:{app}", app)
        c = counts.get(key)
        if not c or not where:
            return ""
        total = sum(c.values())
        out = []
        for tok, n in c.most_common(12):
            shown = _shown(tok)
            if not shown or (tok.startswith("url:") and ca.local_only(shown, cfg)):
                continue
            out.append(f"{shown} {round(100 * n / total)}%")
            if len(out) == HABIT_TOP:
                break
        return f"From {where}, the user usually goes next to: {', '.join(out)} ({total} moves, {self.days} days)" if out else ""


# ---- the pack ------------------------------------------------------------------------------------------------

def gate(ctx: dict, cfg: dict) -> dict:
    """The context with the privacy gates applied (host, mail; a path chain / wish naming a local-only term is
    dropped, from the pack and the RAG query)."""
    host = ctx.get("host") if isinstance(ctx.get("host"), str) else None
    if host and ca.local_only(host, cfg):
        host = None
    mail = ctx.get("mail") if isinstance(ctx.get("mail"), dict) else None
    gated = bool(ctx.get("mail_gated")) or ca.mail_local_only(mail, cfg)
    withheld = mail if gated else None
    terms = deny_terms(cfg, withheld)
    chain, query = (None if leaks(str(ctx.get(k) or ""), cfg, terms) else ctx.get(k) for k in ("chain", "query"))
    return dict(ctx, host=host, mail=None if gated else mail, mail_gated=gated and bool(mail or ctx.get("mail_gated")),
                chain=chain, query=query, _withheld=withheld)


def key_of(g: dict, cfg: dict) -> str:
    m = g.get("mail") or {}
    raw = [g.get("app"), g.get("host"), g.get("space"), m.get("message_id") or m.get("sender_address"),
           m.get("subject"), g.get("mail_gated"), g.get("chain"), g.get("agent"), g.get("query"),
           cfg.get("local_only_domains")]
    return hashlib.sha1(json.dumps(raw, default=str, ensure_ascii=False).encode()).hexdigest()[:16]


class Packer:
    def __init__(self, *, wiki: "Callable[[], rag.WarmWiki | None] | None" = None,
                 habits: Habits | None = None, agents: Callable[[], list[dict]] = roster,
                 armed: Callable[[], str | None] = dispatch.armed, budget_s: float = RAG_BUDGET_S,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 log: Callable[[str], None] = ca.log):
        self.wiki = wiki or (lambda: rag.default())   # () -> the warm index (None: no wiki notes)
        self.habits = habits or Habits()
        self.agents, self.armed, self.budget_s = agents, armed, budget_s
        self.clock, self.wall, self.log = clock, wall, log
        self.cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="oi-rag")

    # -- the RAG part (deadline-bound) -------------------------------------------------------------------
    def query(self, g: dict) -> str:
        m = g.get("mail") or {}
        parts = [g.get("app"), g.get("host"), m.get("subject"), g.get("chain"), g.get("query")]
        if g.get("agent"):
            parts.append(next((a.get("ask_for") for a in self.agents() if a["slug"] == g["agent"]), ""))
        return " ".join(str(p) for p in parts if p)[:400]

    def _rag(self, g: dict, cfg: dict) -> dict:
        terms = deny_terms(cfg, g.get("_withheld"))
        w = self.wiki()
        if w is None:
            return {"wiki": [], "people": ""}
        people = ""
        m = g.get("mail")
        if m:
            name = (m.get("sender_name") or "").strip()
            dom = ca.domain_of(m.get("sender_address"))
            company = dom.split(".")[0] if dom and dom not in ca.FREE_MAIL else ""
            needles = [fold(x) for x in name.split() if len(x) >= 3] + ([fold(company)] if len(company) >= 3 else [])
            if needles:
                for r in w.search(f"{name} {company}".strip(), 3, where=lambda p: p.startswith(PEOPLE_PREFIXES)):
                    hay = f" {fold(r['title'])} {fold(r['path'].replace('-', ' ').replace('/', ' '))} "
                    if any(f" {n} " in hay for n in needles):          # the page is really about them
                        txt = w.page_text(r["path"], 700)
                        if txt and not leaks(txt, cfg, terms):
                            people = f"{r['title']} ({r['path']}): {' '.join(txt.split())}"
                        break
        rows = w.search(self.query(g), 5) if self.query(g) else []
        return {"wiki": rows, "people": people}

    # -- build -------------------------------------------------------------------------------------------
    def build(self, ctx: dict, cfg: dict, task: str = "") -> dict:
        t0 = self.clock()
        g = gate(ctx or {}, cfg)
        key = key_of(g, cfg)
        now = self.clock()
        with self.lock:
            hit = self.cache.get(key)
            if hit and now - hit[0] < CACHE_TTL_S:
                self.cache.move_to_end(key)
                part, cached = hit[1], True
            else:
                part, cached = None, False
        rag_ms = 0
        if part is None:
            t1 = self.clock()
            complete = True
            try:
                part = self.pool.submit(self._rag, g, cfg).result(timeout=self.budget_s)
            except FutureTimeout:
                part, complete = {"wiki": [], "people": ""}, False
            except Exception:
                part, complete = {"wiki": [], "people": ""}, False
            rag_ms = int((self.clock() - t1) * 1000)
            part = dict(part, habits=self.habits.line(g.get("app"), g.get("host"), cfg))
            if complete:
                with self.lock:
                    self.cache[key] = (now, part)
                    while len(self.cache) > CACHE_SIZE:
                        self.cache.popitem(last=False)
        sections = self.sections(g, cfg, part, task, deny_terms(cfg, g.get("_withheld")))
        text, kept = assemble(sections)
        out = {"text": text, "tokens": tokens(text), "sections": kept, "cache": cached, "rag_ms": rag_ms,
               "ms": int((self.clock() - t0) * 1000), "key": key}
        self.log(f"pack {out['ms']} ms (rag {rag_ms} ms, {'hit' if cached else 'miss'}) {out['tokens']} tok "
                 f"{'/'.join(k for k, _ in kept)}")
        return out

    def sections(self, g: dict, cfg: dict, part: dict, task: str, terms: list[str]) -> list[tuple[str, str]]:
        agents = {a["slug"]: a for a in self.agents()}
        now = [f"front app {g['app']}" if g.get("app") else None, f"page {g['host']}" if g.get("host") else None,
               f"desk {g['space']}" if isinstance(g.get("space"), int) else None,
               "Paris " + datetime.fromtimestamp(self.wall(), PARIS).strftime("%a %d %b %H:%M")]
        out = [("Now", ", ".join(p for p in now if p))]
        armed = self.armed()
        if armed in agents:
            out.append(("Dispatch", f"Armed: The user's next note goes to {agents[armed].get('name')} ({armed})."))
        a = agents.get(g.get("agent") or "")
        if a:
            out.append(("Talking to", f"{a.get('name')} ({a['slug']}): {a.get('ask_for') or a.get('role')}. "
                                      f"Mandate: {a.get('mandate') or '(none on file)'}"))
        if g.get("chain"):
            out.append(("Path", str(g["chain"])))
        m = g.get("mail")
        if g.get("mail_gated"):
            out.append(("Mail sender", "A mail is selected (details withheld: local-only domain)."))
        elif m:
            dom = ca.domain_of(m.get("sender_address"))
            line = (f"{m.get('sender_name') or '?'} <{m.get('sender_address') or '?'}>"
                    f" ({'personal address' if dom in ca.FREE_MAIL else 'domain ' + dom if dom else 'no domain'}),"
                    f" subject: {(m.get('subject') or '')[:120]}")
            if part.get("people"):
                line += f"\nWiki page: {part['people']}"
            out.append(("Mail sender", line))
        seen = (part.get("people") or "").split(" (", 1)[0]
        notes = []
        for r in part.get("wiki") or []:
            snip = f"{r['title']}: {r.get('description') or ''}".strip()
            if r["title"] == seen or (task and r["title"] in task) or leaks(snip, cfg, terms):
                continue
            notes.append("- " + " ".join(snip.split())[:280])
            if len(notes) == 3:
                break
        if notes:
            out.append(("Wiki notes", "\n".join(notes)))
        if part.get("habits"):
            out.append(("Habits", part["habits"]))
        if isinstance(g.get("event_habit"), str) and g["event_habit"]:         # W24
            out.append(("After event", g["event_habit"]))
        ev_lines = [str(x)[:200] for x in (g.get("events") or []) if isinstance(x, str) and not leaks(x, cfg, terms)]
        if ev_lines:
            out.append(("Events", "\n".join("- " + x for x in ev_lines[:6])))
        return out


def assemble(sections: list[tuple[str, str]], max_tokens: int = MAX_TOKENS) -> tuple[str, list[tuple[str, int]]]:
    """'## Name\\nbody' blocks in order under the token budget (a block is capped, cut to fit, or dropped)."""
    budget = int(max_tokens * CHARS_PER_TOKEN)
    blocks, kept = [], []
    used = 0
    for name, body in sections:
        body = (body or "").strip()[:CAPS.get(name, 600)]
        if not body:
            continue
        block = f"## {name}\n{body}"
        room = budget - used - (2 if blocks else 0)
        if room < 40:
            break
        if len(block) > room:
            block = block[:room - 1].rstrip() + "…"
        blocks.append(block)
        used += len(block) + (2 if len(blocks) > 1 else 0)
        kept.append((name, tokens(block)))
    return "\n\n".join(blocks), kept


_DEFAULT: Packer | None = None
_DEFAULT_LOCK = threading.Lock()


def default() -> Packer:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = Packer()
        return _DEFAULT
