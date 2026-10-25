"""W23: drill-down glide. Every provider runs on fakes (no Spotlight, AX, AppleScript or chat is touched)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from oi import claude_actions as ca, drill, glide, live

AGENTS = {"atlas": "Atlas", "nova": "Nova"}


def tree(tmp_path):
    home = tmp_path / "home"
    for d in ("Downloads", "Desktop", "Projets/os-intent", "Projets/os-intent/docs", "Library/Caches", ".secret"):
        (home / d).mkdir(parents=True)
    for f in ("Projets/os-intent/plan.md", "Downloads/devis.pdf", "Library/Caches/x.db", ".secret/k.txt"):
        (home / f).write_text("never read")
    return home


def md(home, *rel_dates):
    return "\n".join(f"{home}/{rel}   kMDItemLastUsedDate = {d} +0000" for rel, d in rel_dates)


# ---- providers -------------------------------------------------------------------------------------------------

def test_finder_items_project_first_then_recent_then_downloads_desktop(tmp_path):
    home = tree(tmp_path)
    out = md(home, ("Downloads/devis.pdf", "2026-10-20 15:00:00"), ("Projets/os-intent/plan.md", "2026-10-20 18:00:00"),
             ("Library/Caches/x.db", "2026-10-20 19:00:00"), (".secret/k.txt", "2026-10-20 19:30:00"))
    items = drill.finder_items(out, ["os-intent-zsh", "telegram"], home=str(home))
    labels = [n["label"] for n in items]
    assert labels == ["os-intent", "plan.md", "devis.pdf", "Downloads", "Desktop"]   # Library / hidden never offered
    assert [n["primitive"] for n in items] == ["folder.open", "file.open", "file.open", "folder.open", "folder.open"]
    assert items[1]["params"]["path"] == f"{home}/Projets/os-intent/plan.md"
    assert items[1]["token"].startswith("file.open#") and "plan" not in items[1]["token"]   # an id, never the name


def test_folder_items_lists_names_newest_first_without_reading(tmp_path):
    home = tree(tmp_path)
    proj = home / "Projets/os-intent"
    os.utime(proj / "plan.md", (1, 1))
    got = drill.folder_items(str(proj), ok=lambda p: drill.safe_path(p, str(home)))
    assert [n["label"] for n in got] == ["docs", "plan.md"] and got[0]["primitive"] == "folder.open"
    assert drill.folder_items(str(home / ".secret"), ok=lambda p: drill.safe_path(p, str(home))) == []


def test_safe_path(tmp_path):
    home = str(tree(tmp_path))
    assert drill.safe_path(f"{home}/Downloads/devis.pdf", home)
    for bad in (f"{home}/.secret/k.txt", f"{home}/Library/Caches", "/etc/hosts", "relative", None, f"{home}/nope"):
        assert not drill.safe_path(bad, home)


def test_launchables_are_never_opened_only_revealed(tmp_path, monkeypatch):
    home = tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    dl = home / "Downloads"
    names = ["run.command", "x.sh", "y.zsh", "z.tool", "a.pkg", "b.dmg", "c.terminal", "d.scpt", "e.applescript",
             "f.workflow", "G.DMG"]
    for n in names:
        (dl / n).write_text("never run")
    (dl / "Tool.app").mkdir()
    (dl / "bin").write_text("#!/bin/sh")
    os.chmod(dl / "bin", 0o755)
    (dl / "link.txt").symlink_to(dl / "x.sh")
    bad = [str(dl / n) for n in names + ["Tool.app", "bin", "link.txt"]]
    assert all(drill.launchable(p) for p in bad)
    assert not drill.launchable(str(dl / "devis.pdf")) and not drill.launchable(str(dl)) and not drill.launchable(None)
    for p in bad:                                     # offered only as its folder (revealed in Finder)
        n = drill.path_node(p)
        assert n["primitive"] == "folder.open" and n["params"] == {"path": str(dl)} and n["label"] == "Downloads"
    got = drill.folder_items(str(dl), ok=lambda p: drill.safe_path(p, str(home)))
    assert all(n["primitive"] == "folder.open" for n in got)
    assert not any(n["primitive"] == "file.open" for n in drill.folder_items(str(dl), ok=lambda p: True))
    runs = []
    ex = live.Executor(lambda argv: (runs.append(argv), (0, ""))[1])
    for p in bad:                                     # a forged file.open / folder.open is refused at press
        for prim in ("file.open", "folder.open"):
            assert ex.run({"primitive": prim, "params": {"path": p}, "token": f"{prim}#x"}, {}, str) is None
    assert runs == []
    out = md(home, ("Downloads/x.sh", "2026-10-20 19:00:00"), ("Downloads/devis.pdf", "2026-10-20 18:00:00"))
    items = drill.finder_items(out, [], home=str(home))
    assert [(n["primitive"], n["label"]) for n in items][:2] == [("folder.open", "Downloads"), ("file.open", "devis.pdf")]


def test_telegram_items_titles_from_ax_else_window_title_tokens():
    got = drill.telegram_items(["Pat Lee", "  Team Ops ", "Pat Lee", ""], [])
    assert [n["label"] for n in got] == ["Pat Lee", "Team Ops"]
    assert got[0]["primitive"] == "telegram.chat" and got[0]["params"] == {"chat": "Pat Lee"} and got[0]["icon"] == "initial:P"
    fb = drill.telegram_items([], ["title:Telegram/telegram", "title:Telegram/telegram-vd", "title:Telegram/quick-look",
                                   "title:Telegram/quentin-dupont",
                                   "title:Notion/x"])
    assert [n["label"] for n in fb] == ["Quentin Dupont"]                   # the generic "Telegram" title skipped


def test_music_host_mail_space_items():
    pl = drill.music_items(["Focus", "Jazz", "Focus"])
    assert [(n["label"], n["params"]) for n in pl] == [("Focus", {"playlist": "Focus"}), ("Jazz", {"playlist": "Jazz"})]
    tr = drill.music_items(["So What"], "Jazz")
    assert tr[0]["params"] == {"playlist": "Jazz", "track": "So What"}
    pages = drill.host_items("notion.so", {"notion.so": ["notion.so/prr", "notion.so/road-map"], "x.example": ["x.example/a"]})
    assert [(n["label"], n["params"]["url"], n["token"]) for n in pages] == [
        ("prr", "notion.so/prr", "url:notion.so/prr"), ("road map", "notion.so/road-map", "url:notion.so/road-map")]
    assert drill.mail_items(None) == [] and [n["action"] for n in drill.mail_items({"message_id": "m"})] == ["mail.reply", "mail.forward"]
    assert drill.space_items("space:+1")[0]["token"] == "space:-1" and drill.space_items("space:=3") == []


def test_providers_cache_per_target_with_ttl():
    t = [0.0]
    calls = []
    p = drill.Providers(telegram_titles=lambda: calls.append(1) or ["Eric"], music_playlists=lambda: ["Focus"],
                        music_tracks=lambda pl: calls.append(pl) or ["A"], mdfind=lambda: "", clock=lambda: t[0], ttl=45)
    assert [n["label"] for n in p.items({"kind": "telegram"}, {})] == ["Eric"]
    p.items({"kind": "telegram"}, {})
    assert calls == [1]
    p.items({"kind": "playlist", "playlist": "Jazz"}, {}), p.items({"kind": "playlist", "playlist": "Rock"}, {})
    assert calls == [1, "Jazz", "Rock"]                                      # per target
    t[0] = 50
    p.items({"kind": "telegram"}, {})
    assert calls[-1] == 1 and p.reads["telegram"] == 2                      # expired: read again
    broken = drill.Providers(telegram_titles=lambda: 1 / 0)
    assert broken.items({"kind": "telegram"}, {}) == []                      # a failing reader gives nothing


# ---- scope rejector ------------------------------------------------------------------------------------------

def test_scope_of_each_parent():
    assert drill.scope_of({"primitive": "app.open", "params": {"app": "Finder"}})["kind"] == "finder"
    assert drill.scope_of({"primitive": "app.open", "params": {"app": "Telegram"}})["kind"] == "telegram"
    assert drill.scope_of({"primitive": "app.open", "params": {"app": "Notion"}}) == {"kind": "app", "app": "Notion"}
    assert drill.scope_of({"primitive": "url.open", "params": {"url": "www.Notion.so/prr"}}) == {"kind": "host", "host": "notion.so"}
    assert drill.scope_of({"kind": "vocal", "params": {"to": "nova"}}) == {"kind": "agent", "slug": "nova"}
    assert drill.scope_of({"kind": "summary", "primitive": "url.open"})["kind"] == "leaf"
    assert drill.scope_of({"primitive": "file.open"})["kind"] == "leaf" and drill.scope_of(None)["kind"] == "leaf"


def test_scope_rejector_drops_generic_switches_but_keeps_back():
    env = {"apps": ["Claude", "Finder"], "urls": {"github.com": "github.com/x", "notion.so": "notion.so/prr"},
           "agents": AGENTS, "resolve_app": lambda a: f"/Applications/{a}.app", "front_app": "Safari",
           "mail": {"message_id": "m"}, "claude_ok": True}
    finder = {"primitive": "app.open", "params": {"app": "Finder"}, "token": "activate:Finder"}
    telegram = {"primitive": "app.open", "params": {"app": "Telegram"}, "token": "activate:Telegram"}
    site = {"primitive": "url.open", "params": {"url": "notion.so/prr"}, "token": "url:notion.so/prr"}

    def under(parent, action, label="x", items=None):
        return glide.scoped({"label": label, "action": action}, parent, env, drill.scope_of(parent), items or {})
    # The user's complaint: "after Finder: send Eric, open Claude"
    assert under(finder, "app.open:Claude") is None and under(finder, "vocal:nova:hi") is None
    assert under(finder, "ask:open Claude") is None and under(finder, "launch.rocket") is None
    assert under(telegram, "url.open:github.com") is None and under(telegram, "summarise") is None
    assert under(site, "url.open:github.com") is None
    assert under(site, "url.open:notion.so")["params"] == {"url": "notion.so/prr"} and under(site, "summarise")
    back = under(finder, "back")
    assert back["action"] == "back" and back["params"] == {"app": "Safari"} and back["label"] == "Back to Safari"
    assert under({"primitive": "app.open", "params": {"app": "Safari"}}, "back") is None   # already there
    item = drill.path_node("/Users/you/Projets", is_dir=True)
    assert under(finder, "i1", "Projets ★", {"i1": item})["label"] == "Projets ★"
    mail = {"primitive": "mail.reply", "params": {}}
    assert "confirm" not in under(mail, "vocal:nova:see the devis") and "confirm" not in under(mail, "ask:archive it")
    assert under(mail, "app.open:Claude") is None
    agent = {"kind": "vocal", "action": "vocal:nova", "params": {"to": "nova", "text": "hi"}}
    assert under(agent, "vocal:nova:other")["params"]["to"] == "nova" and under(agent, "vocal:orion:x") is None
    folder = {"primitive": "folder.open", "params": {"path": "/Users/you/Projets"}}
    assert drill.in_scope(drill.path_node("/Users/you/Projets/a.md", is_dir=False), drill.scope_of(folder))
    assert not drill.in_scope(drill.path_node("/Users/you/Other/a.md", is_dir=False), drill.scope_of(folder))
    pl = {"primitive": "music.play", "params": {"playlist": "Jazz"}}
    assert drill.in_scope(drill.music_items(["T"], "Jazz")[0], drill.scope_of(pl))
    assert not drill.in_scope(drill.music_items(["T"], "Rock")[0], drill.scope_of(pl))


# ---- history provider + training pairs --------------------------------------------------------------------------

def test_history_ranks_items_by_markov_follows_and_past_choices():
    hist = [(0, "activate:Telegram"), (5, "title:Telegram/team-ops"), (300, "activate:Telegram"),
            (310, "title:Telegram/team-ops"), (900, "activate:Telegram"), (1500, "title:Telegram/maman")]
    c = drill.follow_counts("activate:Telegram", hist)
    assert c["title:Telegram/team-ops"] == 2 and c["title:Telegram/maman"] == 0       # 600 s later: not a follow
    items = drill.telegram_items(["Pat Lee", "Maman", "Team Ops"], [])
    assert [n["label"] for n in drill.rank(items, "activate:Telegram", "activate:Telegram", hist, [])] == \
        ["Team Ops", "Pat Lee", "Maman"]
    pairs = [{"parent": "activate:Telegram", "choice": items[1]["id"]}] * 2
    assert [n["label"] for n in drill.rank(items, "activate:Telegram", "activate:Telegram", hist, pairs)][0] == "Maman"
    assert [n["label"] for n in drill.rank(items, None, None, [], [])] == ["Pat Lee", "Maman", "Team Ops"]


def test_pairs_are_logged_as_ids_only_and_read_back(data_dir):
    chat = drill.telegram_items(["Pat Lee"], [])[0]
    row = drill.log_pair("activate:Telegram", ["activate:Telegram"], chat["id"], "Safari", ts="2026-10-20T19:00:00Z")
    p = data_dir / "glide-pairs" / "2026-10-20.jsonl"
    assert oct(p.stat().st_mode & 0o777) == "0o600" and oct(p.parent.stat().st_mode & 0o777) == "0o700"
    assert "Eric" not in p.read_text() and row["depth"] == 1
    got = drill.read_pairs(now=datetime(2026, 10, 21, tzinfo=timezone.utc))
    assert got == [row]


# ---- the session: Finder drill-down, cache per (parent, context), colour --------------------------------------------

class FinderRig:
    def __init__(self, home, answer=None):
        self.prompts, self.logged = [], []
        mdout = md(home, ("Projets/os-intent/plan.md", "2026-10-20 18:00:00"))
        self.prov = drill.Providers(mdfind=lambda: mdout, ok=lambda p: drill.safe_path(p, str(home)), home=str(home),
                                    telegram_titles=lambda: [], music_playlists=lambda: [], music_tracks=lambda p: [])
        self.engine = glide.GlideEngine(lambda p: {"event": "ok"}, runner=lambda pr, m: self.prompts.append(pr) or (answer or {}),
                                        mail_reader=lambda: None, settings_loader=lambda: dict(ca.CLAUDE_DEFAULTS),
                                        providers=self.prov, pairs_reader=lambda: [],
                                        pair_logger=lambda *a: self.logged.append(a), wiki=lambda q: [])

    def session(self, app="Safari"):
        top = {1: {"primitive": "app.open", "params": {"app": "Finder"}, "token": "activate:Finder"}}
        env = {"apps": [], "urls": {}, "agents": AGENTS, "resolve_app": lambda a: f"/Applications/{a}.app",
               "host": None, "title_tokens": ["title:Terminal/os-intent-zsh"]}
        return self.engine.session(top, {"app": app, "space": 1}, env)


def test_finder_drill_down_is_local_cached_and_scoped(tmp_path):
    home = tree(tmp_path)
    rig = FinderRig(home)
    s = rig.session()
    s.prefetch()
    p1 = s.expand([1])
    assert [o["label"] for o in p1["options"]] == ["os-intent", "plan.md", "Downloads", "Desktop"]
    assert rig.prompts == []                                                  # Finder: providers only, no Claude
    p2 = s.expand([1, 1])                                                     # inside the project folder
    assert [o["label"] for o in p2["options"]][:2] in (["docs", "plan.md"], ["plan.md", "docs"])
    assert all(o["action"] in ("folder.open", "file.open") for o in p2["options"])
    assert s.expand([1, 2])["options"] == []                                  # a file: a leaf
    s2 = rig.session()                                                        # another show, same context
    s2.expand([1])
    assert rig.prov.reads["finder"] == 1                                      # cache per (parent, context)
    s3 = rig.session(app="Notion")                                            # another context: a new entry, provider TTL
    s3.expand([1])
    assert len(rig.engine.cache) == 4 and rig.prov.reads["finder"] == 1
    tok, action, _ = s.execute([1, 1, 2] if s.node([1, 1, 2])["label"] == "plan.md" else [1, 1, 1],
                               run_primitive=lambda n: n["token"], press=None, send_vocal=None)
    assert action == "file.open" and tok.startswith("file.open#")
    assert rig.logged[-1][1][:1] == ["activate:Finder"] and rig.logged[-1][1][1].startswith("folder.open#")


def test_path_chain_prompt_lists_items_forms_and_wiki():
    env = {"agents": AGENTS, "front_app": "Mail", "mail": None}
    items = [drill.mail_items({"message_id": "m"})[0]]
    prompt, ids = glide.build_prompt({"app": "Mail"}, env, [(1, "reply to the selected mail > Tell Nova", items,
                                                             {"kind": "mail"})], now=0, wiki=["Nova: CEO notes"])
    assert "1. Path: reply to the selected mail > Tell Nova" in prompt
    assert 'Items: i1 = mail "Reply"' in prompt and "vocal:<agent>:<message> (agents: nova (Nova))" in prompt
    assert "- Nova: CEO notes" in prompt.split("<business_context>\n")[1] and ids == {1: {"i1": items[0]}}
    assert "return to Mail" in prompt and "Never propose switching to another app" in prompt
    prompt2, _ = glide.build_prompt({}, env, [(2, "switch to Finder", [], {"kind": "finder"})], now=0)
    assert "Items: (none)" in prompt2 and "Forms: back" in prompt2 and "<business_context>\n(none)" in prompt2


def test_wiki_only_at_depth_two_under_agent_or_mail(tmp_path):
    rig = FinderRig(tree(tmp_path))
    rig.engine.wiki = lambda q: ["note"]
    s = rig.session()
    assert s.wiki_for([1], {"kind": "mail"}) == [] and s.wiki_for([1, 1], {"kind": "finder"}) == []
    s.children[(1,)] = [drill.path_node("/x", is_dir=True)]
    assert s.wiki_for([1, 1], {"kind": "agent"}) == ["note"]


# ---- executor: new primitives (fake runner; nothing opened) ----------------------------------------------------------

def test_executor_runs_the_new_primitives_through_the_runner(tmp_path, monkeypatch):
    home = tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    runs = []
    ex = live.Executor(lambda argv: (runs.append(argv), (0, ""))[1])
    f = drill.path_node(f"{home}/Downloads/devis.pdf")
    assert ex.run(f, {}, str) == f["token"] and runs[-1] == ["open", f"{home}/Downloads/devis.pdf"]
    assert ex.run(drill.path_node(f"{home}/.secret/k.txt"), {}, str) is None                  # re-checked at press
    chat = drill.telegram_items(["Pat Lee"], [])[0]
    assert ex.run(chat, {}, str) == chat["token"]
    assert runs[-1][:3] == ["osascript", "-l", "JavaScript"] and runs[-1][-1] == "Pat Lee"
    song = drill.music_items(["So What"], "Jazz")[0]
    assert ex.run(song, {}, str) == song["token"] and runs[-1][-2:] == ["Jazz", "So What"]
    ex_fail = live.Executor(lambda argv: (1, "notfound"))
    assert ex_fail.run(chat, {}, str) is None


def test_glide_env_carries_pages_history_and_titles(data_dir):
    lv = live.Live(pull=False, sender=lambda *a: None, resolve_app=lambda a: f"/Applications/{a}.app")
    lv.rows = [{"ts": "2026-10-20T10:00:00Z", "token": "url:notion.so/prr"},
               {"ts": "2026-10-20T10:01:00Z", "token": "title:Telegram/eric-martin"},
               {"ts": "2026-10-20T10:02:00Z", "token": "url:notion.so/road-map"},
               {"ts": "2026-10-20T10:03:00Z", "token": "url:notion.so/prr"}]
    e = lv.glide_env({"app": "Safari"})
    assert e["pages"] == {"notion.so": ["notion.so/prr", "notion.so/road-map"]}
    assert e["title_tokens"] == ["title:Telegram/eric-martin"] and len(e["history"]) == 4
    assert json.dumps(e["history"])                                            # plain (ts, token) pairs
