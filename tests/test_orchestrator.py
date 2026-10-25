"""W25: the orchestrator — roster, topic routing, warm RAG, context pack (privacy, budget, cache, deadline),
the wrap of every runtime prompt, dispatch. Fake Claude, fake embeddings, a temp wiki db: nothing real is read or
sent."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sqlite3
import time
from pathlib import Path

import pytest
import yaml

from oi import claude_actions as ca, glide, intent_resolve as ir, orchestrator, rag, shots, vocal
from oi.orchestrator import dispatch, gen_roster, pack

REPO = Path(__file__).resolve().parents[1]          # the os-intent checkout holding tools/wiki/wiki_search.py
AGENTS = [
    {"slug": "orion", "name": "Orion", "bu": ["Portfolio BU"], "ask_for": "the fund: NAV, positions",
     "mandate": "Grow the portfolio.", "topics": ["nav", "fonds", "fund", "portefeuille"]},
    {"slug": "lyra", "name": "Lyra", "bu": ["Client BU"], "ask_for": "prospection, LinkedIn",
     "topics": ["prospection", "linkedin", "prospects"]},
    {"slug": "atlas", "name": "Atlas", "bu": ["Methods BU"], "ask_for": "tools, bugs, the HUD",
     "topics": ["bug", "hud", "outil"]},
    {"slug": "hera", "name": "Hera", "bu": [], "ask_for": "accounting",
     "topics": ["facture", "comptabilité", "note de frais"]},
]


def fake_embed(text: str, dim: int = 32) -> list[float]:
    """Bag of words hashed into `dim` buckets (deterministic, no model)."""
    v = [0.0] * dim
    for w in orchestrator.fold(text).split():
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1.0
    return v


PAGES = {
    "shared/people/sam-martin.md": ("Sam Martin", "Client at Acme Conseil, training quote",
                                          "Sam Martin runs training at Acme. Prefers short mails."),
    "shared/systems/fund-nav.md": ("Fund NAV", "How the fund NAV is computed daily", "nav fonds daily orion"),
    "shared/systems/hud.md": ("OS-intent HUD", "The radial HUD on the user's Mac", "hud glide wish"),
    "shared/people/secret-client.md": ("Secret Client", "Works at secretco.example", "secretco.example deal"),
    "shared/systems/linkedin.md": ("LinkedIn outbound", "Lyra prospection pipeline", "linkedin prospects lyra"),
}


@pytest.fixture
def wiki_db(tmp_path):
    ws = rag.load_wiki_search(str(REPO))
    db = tmp_path / "wiki.db"
    conn = ws.init_db(db)
    for p, (title, desc, content) in PAGES.items():
        emb = json.dumps(fake_embed(f"{title}\n{desc}\n{content}")).encode()
        conn.execute(ws._UPSERT_PAGE_SQL, (p, title, desc, f"---\ntitle: {title}\n---\n<!-- x -->\n# {title}\n{content}",
                                            p.split("/")[0], emb, time.time()))
    conn.commit()
    conn.close()
    return db


def warm(db, embed=fake_embed):
    return rag.WarmWiki(db, repo_dir=str(REPO), embedder=embed)


# ---- warm RAG --------------------------------------------------------------------------------------------------

def test_warm_wiki_hybrid_search_read_only_and_filters(wiki_db):
    w = warm(wiki_db)
    assert w.warm() == len(PAGES)
    before = wiki_db.stat().st_mtime
    r = w.search("NAV du fonds", 2)
    assert r[0]["path"] == "shared/systems/fund-nav.md" and set(r[0]) == {"path", "title", "description", "domain", "score", "cos"}
    people = w.search("Sam Martin acme", 3, where=lambda p: p.startswith("shared/people/"))
    assert people and all(x["path"].startswith("shared/people/") for x in people)
    assert people[0]["path"] == "shared/people/sam-martin.md"
    assert "Prefers short mails" in w.page_text(people[0]["path"]) and "title:" not in w.page_text(people[0]["path"])
    with pytest.raises(sqlite3.OperationalError):                 # read-only connection: the HUD never writes
        w.conn.execute("DELETE FROM pages")
    assert wiki_db.stat().st_mtime == before


def test_warm_wiki_caches_query_embeddings_and_falls_back_to_keywords(wiki_db):
    calls = []
    w = warm(wiki_db, embed=lambda t: (calls.append(t), fake_embed(t))[1])
    w.search("linkedin prospects", 2)
    w.search("linkedin prospects", 2)
    assert calls == ["linkedin prospects"]                       # the second query costs no embedding
    k = warm(wiki_db, embed=lambda t: [])                        # Ollama down: keyword-only, still answers
    assert k.search("linkedin", 1)[0]["path"] == "shared/systems/linkedin.md"
    assert warm(wiki_db).search("   ", 3) == []


def test_warm_wiki_reloads_when_the_db_changes(wiki_db, monkeypatch):
    t = [0.0]
    w = rag.WarmWiki(wiki_db, repo_dir=str(REPO), embedder=fake_embed, clock=lambda: t[0])
    w.warm()
    conn = sqlite3.connect(wiki_db)
    conn.execute("INSERT INTO pages (path, title, description, content, domain, embedding, indexed_at) VALUES "
                 "(?, ?, ?, ?, ?, ?, ?)", ("shared/x.md", "Zebra", "zebra page", "zebra", "shared",
                                           json.dumps(fake_embed("zebra")).encode(), time.time()))
    conn.commit()
    conn.close()
    os.utime(wiki_db, (time.time() + 5, time.time() + 5))
    t[0] = rag.RELOAD_CHECK_S + 1
    assert w.search("zebra", 1)[0]["title"] == "Zebra"


def test_rag_snippets_none_when_unavailable():
    assert rag.snippets("anything") is None                      # conftest: no real wiki in tests
    assert ca.wiki_snippets("x", repo_dir="/nonexistent") == []  # W14 fallback path still answers []


# ---- roster ----------------------------------------------------------------------------------------------------

def test_gen_roster_merges_the_registry_and_never_copies_paths(tmp_path):
    team = tmp_path / "team.md"
    team.write_text("# Team\n## Agents (full)\n| First Name | Code | Role | Machine | Reach |\n|---|---|---|---|---|\n"
                    "| **Orion** | Orion (fund) | Portfolio manager | VPS | root-SSH |\n"
                    "| **Atlas** | Lab | Prototypes, tools | M4 | local |\n## Other\n| x | y | z | w |\n")
    rooms = tmp_path / "rooms.yaml"
    rooms.write_text(yaml.safe_dump({"rooms": [{"bu": "fund", "members": ["orion", "atlas"]},
                                               {"bu": "ai_methods", "members": ["atlas"]}]}))
    ws = tmp_path / "ws"
    (ws / "team-ops-orion").mkdir(parents=True)
    (ws / "team-ops-orion/dept.yaml").write_text(yaml.safe_dump({"department": {
        "slug": "orion", "display_name": "Orion", "mandate": "Grow the value. " * 30, "secrets": {"token": "NOPE"}}}))
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump({"agents": {"orion": {"ask_for": "the fund", "topics": ["NAV", "fonds"]}}}))
    recips = {"orion": vocal._remote("orion", "Orion", "agent-host"), "atlas": vocal.DEFAULT_RECIPIENTS["atlas"]}
    r = gen_roster.build(recipients=recips, overlay_path=overlay, roster_path=team, rooms_path=rooms,
                         globs=[(ws, "*/dept.yaml")])
    atlas, orion = r["agents"]
    assert orion["slug"] == "orion" and orion["role"] == "Portfolio manager" and orion["bu"] == ["Portfolio BU"]
    assert orion["topics"] == ["nav", "fonds"] and len(orion["mandate"]) <= gen_roster.MANDATE_MAX + 2
    assert orion["reach"] == {"via": "vocal", "kind": "a2a", "where": "agent-host"}
    assert atlas["reach"]["kind"] == "inject" and set(atlas["bu"]) == {"Portfolio BU", "Methods BU"}
    text = gen_roster.dump(r)
    assert "NOPE" not in text and "/home/" not in text and "/Users/" not in text and "state_dir" not in text


def test_committed_roster_covers_every_vocal_recipient_and_holds_no_secret():
    agents = orchestrator.roster()
    assert {a["slug"] for a in agents} == set(vocal.DEFAULT_RECIPIENTS)
    raw = orchestrator.ROSTER.read_text(encoding="utf-8")
    assert "/Users/" not in raw and "/home/" not in raw and not any(w in raw.lower() for w in ("token:", "secret", "password"))
    assert all(a.get("ask_for") and a.get("topics") for a in agents)


@pytest.mark.parametrize("wish,want", [
    ("demande à quelqu'un le NAV du fonds", "orion"),
    ("qui peut relancer les prospects LinkedIn ?", "lyra"),
    ("someone should fix the HUD bug", "atlas"),
    ("envoie la note de frais à quelqu'un", "hera"),
])
def test_route_picks_the_topic_owner(wish, want):
    assert orchestrator.route(wish, AGENTS)[0] == want


def test_route_none_on_no_topic_or_a_tie():
    assert orchestrator.route("bonjour", AGENTS) == (None, 0.0)
    assert orchestrator.route("nav linkedin", AGENTS)[0] is None            # one topic each: a tie, no guess
    assert orchestrator.route("demande à quelqu'un le NAV du fonds")[0] == "orion"   # the committed roster too


# ---- the pack ---------------------------------------------------------------------------------------------------

MAIL = {"sender_name": "Sam Martin", "sender_address": "sam@acme-conseil.example", "subject": "devis formation",
        "message_id": "<m1>", "cc": [], "reply_to": ""}


def packer(wiki=None, **kw):
    kw.setdefault("agents", lambda: AGENTS)
    kw.setdefault("armed", lambda: None)
    kw.setdefault("log", lambda m: None)
    return pack.Packer(wiki=(lambda: wiki), wall=lambda: 1_800_000_000, **kw)


def test_pack_sections_people_page_wiki_and_budget(wiki_db):
    p = packer(warm(wiki_db))
    out = p.build({"app": "Mail", "space": 2, "mail": MAIL, "chain": "reply to the selected mail > Tell Orion",
                   "agent": "orion", "query": "NAV du fonds"}, dict(ca.CLAUDE_DEFAULTS))
    names = [n for n, _ in out["sections"]]
    assert names[:5] == ["Now", "Talking to", "Path", "Mail sender", "Wiki notes"]
    assert "Sam Martin (shared/people/sam-martin.md)" in out["text"] and "Prefers short mails" in out["text"]
    assert "Fund NAV" in out["text"] and "Paris" in out["text"] and "front app Mail" in out["text"]
    assert out["tokens"] <= pack.MAX_TOKENS and out["cache"] is False


def test_pack_privacy_gate_host_mail_and_wiki_leaks(wiki_db):
    cfg = dict(ca.CLAUDE_DEFAULTS, local_only_domains=["acme-conseil.example", "secretco.example", "intra.example.com"])
    seen_q = []

    class Spy(rag.WarmWiki):
        def search(self, q, k=3, **kw):
            seen_q.append(q)
            return super().search(q, k, **kw)

    p = packer(Spy(wiki_db, repo_dir=str(REPO), embedder=fake_embed))
    out = p.build({"app": "Safari", "host": "intra.example.com", "mail": MAIL, "query": "secret client deal"}, cfg)
    t = out["text"]
    assert "intra.example.com" not in t and "Sam" not in t and "acme" not in t.lower()
    assert "details withheld" in t
    assert "secretco" not in t.lower() and "Secret Client" not in t           # a wiki note naming a gated domain
    assert all("intra.example.com" not in q and "devis" not in q for q in seen_q)   # nor in the RAG query
    cc = dict(MAIL, sender_address="x@gmail.com", cc=["boss@acme-conseil.example"])
    assert "details withheld" in p.build({"app": "Mail", "mail": cc}, cfg)["text"]  # the W15 cc rule


def test_pack_cache_per_context_key_and_the_rag_deadline(wiki_db):
    calls = []

    class Slow:
        def __init__(self, delay):
            self.delay = delay

        def search(self, q, k=3, **kw):
            calls.append(q)
            time.sleep(self.delay)
            return [{"path": "a.md", "title": "Fast note", "description": "d", "domain": "", "score": 1.0}]

        def page_text(self, p, n=0):
            return ""

    p = packer(Slow(0.0))
    a = p.build({"app": "Safari", "host": "github.com"}, dict(ca.CLAUDE_DEFAULTS))
    b = p.build({"app": "Safari", "host": "github.com"}, dict(ca.CLAUDE_DEFAULTS))
    assert a["cache"] is False and b["cache"] is True and len(calls) == 1 and "Fast note" in b["text"]
    p.build({"app": "Safari", "host": "notion.so"}, dict(ca.CLAUDE_DEFAULTS))
    assert len(calls) == 2                                                       # another context key
    late = packer(Slow(0.5), budget_s=0.05)
    t0 = time.monotonic()
    out = late.build({"app": "Finder"}, dict(ca.CLAUDE_DEFAULTS))
    assert time.monotonic() - t0 < 0.4 and "Wiki notes" not in [n for n, _ in out["sections"]]
    assert late.cache == {}                                                      # a late search is not cached


def test_pack_budget_cuts_low_priority_sections():
    sections = [("Now", "x" * 100), ("Mail sender", "m" * 5000), ("Wiki notes", "w" * 5000), ("Habits", "h" * 400)]
    text, kept = pack.assemble(sections, max_tokens=500)
    assert pack.tokens(text) <= 500 and kept[0][0] == "Now" and "Habits" not in [n for n, _ in kept]


def test_habits_from_tokens(data_dir):
    from oi import paths
    d = paths.tokens_dir()
    d.mkdir(parents=True)
    today = time.strftime("%Y-%m-%d")
    toks = ["activate:Mail", "activate:Safari", "activate:Mail", "title:Telegram/secret-chat", "activate:Telegram",
            "activate:Mail", "activate:Safari", "url:secretco.example/x"]
    (d / f"{today}.jsonl").write_text("".join(json.dumps({"token": t}) + "\n" for t in toks))
    h = pack.Habits()
    line = h.line("Mail", None, {"local_only_domains": []})
    assert line.startswith("From Mail, the user usually goes next to: Safari 67%, Telegram 33%") and "secret-chat" not in line
    gated = h.line("Safari", None, {"local_only_domains": ["secretco.example"]})
    assert "secretco" not in gated


# ---- the wrap: every runtime prompt ----------------------------------------------------------------------------

def test_wrap_puts_system_roster_skills_pack_then_the_task():
    p = packer(None)
    w = orchestrator.wrap("glide", "TASK BODY", {"app": "Mail"}, cfg=dict(ca.CLAUDE_DEFAULTS), packer=p)
    assert w.index("HUD orchestrator") < w.index("- orion: Orion") < w.index("Skill — dispatch") < \
        w.index("<orchestrator_context>\n## Now") < w.index("TASK BODY")
    assert w.rstrip().endswith("TASK BODY") and "Nothing happens without the user's key or click" in w
    assert orchestrator.wrap("check", "T", {}, cfg=dict(ca.CLAUDE_DEFAULTS), packer=p) == "T"
    assert orchestrator.wrap("glide", "T", {}, cfg=dict(ca.CLAUDE_DEFAULTS, orchestrator=False), packer=p) == "T"


def test_every_runtime_prompt_is_wrapped(data_dir, monkeypatch):
    prompts = []
    runner = lambda prompt, model: (prompts.append(prompt), {"body": "ok", "lines": ["a"], "labels": {}})[1]
    acts = ca.ClaudeActions(runner=runner, mail_reader=lambda **kw: dict(MAIL, body="hi", previous=None),
                            wiki=lambda q: [], osa=lambda s, a: (0, "OK"), sender=lambda p: {"event": "ok"},
                            settings_loader=lambda: dict(ca.CLAUDE_DEFAULTS), dry_run=True, out=io.StringIO())
    acts.press({"primitive": "mail.reply", "params": {}}, {"app": "Mail"}, 1, plain=lambda: None)
    prompts.append(ca.render_prompt("summary", {"what": "app", "page": "x"}, context={"app": "Notes"}))
    prompts.append(ir.claude_prompt("demande le NAV", {"app": "Finder"}, ir.build_catalogue(
        {"app": "Finder"}, apps=[], app_uses={}, host_uses={}, recipients={"orion": {"name": "Orion"}})))
    prompts.append(shots.note_prompt("Orion", {"app": "Safari", "host": "github.com", "desk": 1},
                                     __import__("datetime").datetime.now(), [], "", slug="orion"))
    assert len(prompts) == 4
    for p in prompts:
        assert "HUD orchestrator" in p and "<orchestrator_context>" in p and "## Now" in p
    assert "Sam Martin <sam@acme-conseil.example>" in prompts[0]           # the mail pack
    assert "Orion (orion)" in prompts[3]                                              # the agent the note is for
    assert "send a message to Orion (finance" in prompts[2]                 # the roster in the catalogue
    assert "HUD orchestrator" not in ca.render_prompt("check", {})


# ---- dispatch -----------------------------------------------------------------------------------------------------

def test_dispatch_arm_expire_clear(data_dir):
    assert dispatch.armed() is None
    dispatch.arm("atlas", now=1000)
    assert dispatch.armed(now=1000 + 5) == "atlas" and dispatch.armed(now=1000 + dispatch.DISPATCH_TTL_S + 1) is None
    assert oct(os.stat(dispatch._path()).st_mode & 0o777) == "0o600"
    dispatch.clear()
    assert dispatch.armed() is None


def wish_engine(sent, claude_on=False):
    return ir.WishEngine(
        executor=lambda opt, ctx: opt["token"], dry_run=False, out=io.StringIO(),
        claude=ca.ClaudeActions(runner=lambda p, m: {}, wiki=lambda q: [], settings_loader=lambda: dict(ca.CLAUDE_DEFAULTS)),
        claude_runner=lambda p, m: (_ for _ in ()).throw(ca.ClaudeUnavailable("no_login")),
        claude_settings=lambda: dict(ca.CLAUDE_DEFAULTS, enabled=claude_on),
        laya_health=lambda url: False, apps=lambda: ["Mail", "Safari"], history=lambda: ({}, {}),
        raw_settings=lambda: {}, vocal_send=lambda to, text, **kw: (sent.append((to, text)), {"ok": True, "name": to})[1])


def test_wish_topic_routes_to_orion_and_sends_through_the_gate_only(data_dir):
    sent = []
    eng = wish_engine(sent)
    pv = eng.handle({"id": 1, "text": "demande à quelqu'un le NAV du fonds", "final": False, "context": {"app": "Mail"}})
    assert pv["label"].startswith("→ Send to Orion")
    r = eng.handle({"id": 2, "text": "demande à quelqu'un le NAV du fonds", "final": True, "context": {"app": "Mail"}})
    # no W18 confirm: oi.vocal.send asks the outward gate's panel (the one confirmation)
    assert r["state"] == "done" and r["tier"] == "roster" and sent == [("orion", "demande à quelqu'un le NAV du fonds")]


def test_pass_me_to_atlas_arms_then_the_next_note_goes_to_atlas(data_dir):
    sent = []
    eng = wish_engine(sent)
    r = eng.handle({"id": 1, "text": "passe-moi Atlas", "final": True, "context": {"app": "Safari"}})
    assert r["state"] == "done" and r["token"] == "dispatch:atlas" and sent == [] and dispatch.armed() == "atlas"
    n = eng.handle({"id": 2, "text": "le build casse sur la branche w25", "final": True, "context": {"app": "Safari"}})
    assert n["state"] == "done" and n["tier"] == "dispatch" and sent == [("atlas", "le build casse sur la branche w25")]
    dispatch.clear()
    assert eng.handle({"id": 3, "text": "le build casse", "final": True, "context": {}})["state"] == "noted"


def test_glide_dispatch_forms_resolve_execute_and_arm(data_dir):
    env = {"agents": {"atlas": "Atlas", "orion": "Orion"}, "apps": [], "urls": {}, "resolve_app": lambda a: None,
           "mail": None, "claude_ok": True}
    parent = {"primitive": "url.open", "params": {"url": "github.com/x"}, "token": "url:github.com/x"}
    scope = {"kind": "host", "host": "github.com"}
    arm = glide.scoped({"label": "Pass to Orion", "action": "dispatch:orion"}, parent, env, scope, {})
    send = glide.scoped({"label": "NAV to Orion", "action": "dispatch:orion:NAV du jour ?"}, parent, env, scope, {})
    assert arm["kind"] == "dispatch" and not arm.get("confirm")
    assert send["kind"] == "vocal" and not send.get("confirm") and send["params"] == {"to": "orion", "text": "NAV du jour ?", "dispatch": True}
    assert glide.scoped({"label": "x", "action": "dispatch:nobody"}, parent, env, scope, {}) is None
    s = glide.GlideSession(glide.GlideEngine(lambda p: {"event": "ok"}, runner=lambda p, m: {}), {1: parent}, {}, env)
    s.children[(1,)] = [arm, send]
    vocals = []
    kw = {"run_primitive": lambda n: None, "press": lambda n, m: (None, None),
          "send_vocal": lambda to, text: (vocals.append((to, text)), {"ok": True})[1]}
    assert s.execute([1, 1], **kw)[0] == "dispatch:orion" and dispatch.armed() == "orion" and vocals == []
    dispatch.clear()
    assert s.execute([1, 2], **kw)[0] == "vocal:orion"     # on release: the gate (in oi.vocal.send) confirms and vocals == [("orion", "NAV du jour ?")] and dispatch.armed() == "orion"


def test_glide_prompt_lists_the_dispatch_form_and_carries_the_pack():
    env = {"agents": {"atlas": "Atlas", "orion": "Orion"}, "host": "github.com", "mail": None, "mail_gated": False,
           "front_app": "Safari"}
    prompt, _ = glide.build_prompt({"app": "Safari", "space": 1}, env,
                                   [(1, "open github.com", [], {"kind": "host", "host": "github.com"})])
    assert glide.DISPATCH_FORM in prompt and "<orchestrator_context>" in prompt and "## Path\nopen github.com" in prompt


def test_screenshot_contact_prefers_the_armed_dispatch(data_dir):
    class Live:
        rows = []
    so = shots.Shots.__new__(shots.Shots)
    so.live = Live()
    so.sent_rows = lambda now: []
    table = vocal.recipients({})
    assert so.contact_for({}, table, {"fallback_contact": "atlas"}, time.time())["slug"] == "atlas"
    dispatch.arm("lyra")
    c = so.contact_for({}, table, {"fallback_contact": "atlas"}, time.time())
    assert c == {"slug": "lyra", "via": "dispatch", "ts": None}


@pytest.mark.parametrize("text", ["Acme Conseil", "Acme-Conseil", "AcmeConseil", "acme_conseil", "Acme-Conseil SAS",
                                  "ACME CONSEIL", "acmeconseil", "Acmé-Conseil", "meeting with AcmeConseil SAS today",
                                  "see shared/people/acme-conseil.md", "acme_conseil_sas", "Acme"])
def test_leaks_catches_separator_and_suffix_variants(text):
    assert pack.leaks(text, {"local_only_domains": ["acme-conseil.example"]})


@pytest.mark.parametrize("text", ["acmes of design", "Fund NAV", "conseiller", "Acmeco"])
def test_leaks_variants_no_false_positive(text):
    assert not pack.leaks(text, {"local_only_domains": ["acme-conseil.example"]})


def test_pack_drops_a_chain_or_wish_naming_a_local_only_term():
    cfg = dict(ca.CLAUDE_DEFAULTS, local_only_domains=["acme-conseil.example"])
    seen_q = []

    class Spy:
        def search(self, q, k=3, **kw):
            seen_q.append(q)
            return []

        def page_text(self, p, n=0):
            return ""

    p = packer(Spy())
    out = p.build({"app": "Mail", "chain": "Mail > Forward to AcmeConseil", "query": "devis Acme-Conseil SAS"}, cfg)
    assert "Path" not in [n for n, _ in out["sections"]] and "acme" not in out["text"].lower()
    assert all("acme" not in q.lower() for q in seen_q)
    ok = p.build({"app": "Mail", "chain": "Mail > Forward to Nova", "query": "NAV du fonds"}, cfg)
    assert "Forward to Nova" in ok["text"] and any("NAV du fonds" in q for q in seen_q)


def test_query_vec_does_not_cache_a_failed_embed(wiki_db):
    up = {"on": False}
    w = rag.WarmWiki(wiki_db, repo_dir=str(REPO), embedder=lambda t: fake_embed(t) if up["on"] else None)
    assert w.query_vec("hello") is None and "hello" not in w.qcache
    up["on"] = True
    assert w.query_vec("hello") and "hello" in w.qcache
