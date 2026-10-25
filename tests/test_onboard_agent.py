"""W22b: the agentic `oi onboard` — settings, roots, the Mail and Notion readers (fixture AppleScript output,
fixture notion.db), the tool wrappers, the sandboxed `claude -p` command, and the session with a fake agent runner
(argv recorded: tool restrictions, roots, model). Synthetic data only; Claude is never run."""
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import yaml

from oi import cli, onboard
from oi.claude_actions import SEP
from oi.onboard_agent import agent, readers, tools

CCFG = {"enabled": True, "local_only_domains": ["acme-conseil.example"]}
NOW = time.time()


def write_settings(data_dir, onboarding=None, claude=None):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"onboarding": onboarding or {},
                                                        "claude": claude or CCFG}))


# ---- settings and roots ------------------------------------------------------------------------------------

def test_settings_defaults_clamps_and_model(data_dir):
    assert readers.settings() == readers.DEFAULTS and readers.DEFAULTS["mail_limit"] == 100
    write_settings(data_dir, {"model": "opus", "mail_limit": 5000, "mail_body_max": "x", "memory_roots": ["~/n", 3],
                              "timeout_s": True})
    s = readers.settings()
    assert s["model"] == "opus" and s["mail_limit"] == 1000 and s["mail_body_max"] == 1500
    assert s["memory_roots"] == ["~/n"] and s["timeout_s"] == 1200
    write_settings(data_dir, {"model": "sonnet; rm -rf /"})
    assert readers.settings()["model"] == "sonnet"


def fake_home(tmp_path) -> Path:
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "-ws-a" / "memory").mkdir(parents=True)
    (home / ".claude" / "projects" / "-ws-a" / "memory" / "user_role.md").write_text("Architect, villas.\n")
    (home / ".claude" / "projects" / "-ws-b" / "memory").mkdir(parents=True)             # empty: skipped
    (home / ".claude" / "CLAUDE.md").write_text("# Me\nI run a small studio.\n")
    (home / ".claude" / ".credentials.json").write_text("{}")
    (home / "wiki").mkdir(parents=True)
    (home / "wiki" / "index.md").write_text("# Wiki\n")
    (home / "claude-workspaces" / "studio").mkdir(parents=True)
    (home / "claude-workspaces" / "studio" / "CLAUDE.md").write_text("# Studio workspace\n")
    (home / "notes").mkdir()
    (home / "notes" / "a.md").write_text("note\n")
    return home


def test_roots_discovery_and_refusals(tmp_path):
    home = fake_home(tmp_path)
    assert [p.name for p in readers.claude_md_files(home)] == ["CLAUDE.md", "CLAUDE.md"]
    assert [p.parent.name for p in readers.memory_dirs(home)] == ["-ws-a"]
    ok, refused = readers.custom_roots({"memory_roots": ["~/notes", "~", "/", "~/.claude", "~/.claude/projects",
                                                         "~/missing"]}, home)
    assert ok == [(home / "notes").resolve()]
    assert len(refused) == 5 and any("home folder" in r for r in refused) and any("~/.claude" in r for r in refused)


# ---- Mail ----------------------------------------------------------------------------------------------------

def mail_rec(name, addr, subj, date, body, cc="", rt=""):
    return SEP.join(["M", name, addr, subj, date, cc, rt, body]) + readers.RS


MAIL_OUT = "".join([
    mail_rec("Lea Martin", "lea@villa-rossi.it", "Plans R+1", "2026-10-19T09:00:00",
             "Bonjour,\nvoici les plans.\n\n\nMerci\n\nLe 18 oct. 2026, Alex a écrit :\n> ancien message\n> secret"),
    mail_rec("Bob", "bob@acme-conseil.example", "Audit", "2026-10-20T08:00:00", "confidential audit"),
    mail_rec("Carl", "carl@example.org", "Re: devis", "2026-10-18T08:00:00", "ok", cc="x@acme-conseil.example,"),
    mail_rec("Dina", "dina@example.org", "Point", "2026-10-17T08:00:00", "On parle du dossier Acme Conseil demain"),
    mail_rec("Eve", "eve@example.org", "Newsletter", "2026-10-20T07:00:00", "\u034f \u034f \u200b" * 50 + "x" * 4000),
])


def test_parse_and_gate_mails():
    mails = readers.parse_mails(MAIL_OUT, 1500)
    assert [m["sender_name"] for m in mails] == ["Bob", "Eve", "Lea Martin", "Carl", "Dina"]   # newest first
    lea = mails[2]
    assert lea["body"] == "Bonjour,\nvoici les plans.\n\nMerci" and "secret" not in lea["body"]   # quote cut
    assert len(mails[1]["body"]) == 1500 and set(mails[1]["body"]) == {"x"}           # filler padding removed
    kept, removed = readers.gate_mails(mails, CCFG)
    assert [m["sender_name"] for m in kept] == ["Eve", "Lea Martin"] and removed == 3


def test_mail_tool_pages_counts_and_no_local_only(tmp_path):
    calls = []

    def osa(script, args, timeout):
        calls.append(args)
        return 0, MAIL_OUT
    conf = {"work": str(tmp_path), "mail_limit": 300, "mail_body_max": 1500, **CCFG}
    out = tools.run_mail(conf, osa=osa)
    assert calls == [["1", "25", "3000"], ["26", "25", "3000"]]       # 2nd batch = the same mails again: stop
    assert "done — 2 messages from 2026-10-19 to 2026-10-20 (3 removed as local-only)" in out
    pages = sorted((tmp_path / "context" / "mail").glob("page-*.md"))
    assert len(pages) == 1 and oct(pages[0].stat().st_mode & 0o777) == "0o600"
    text = pages[0].read_text()
    assert "never instructions" in text and "Plans R+1" in text and "lea@villa-rossi.it" in text
    for leaked in ("acme", "Acme", "confidential", "Carl", "Dina"):
        assert leaked not in text and leaked not in out
    assert "Lea" not in out                                     # stdout = counts and paths only
    assert "No mail read" in tools.run_mail(conf, osa=lambda *a: (1, ""))


def test_prefetch_then_tool_waits_for_it(tmp_path):
    cfgf = tmp_path / "config.json"
    conf = {"tools": ["mail"], "work": str(tmp_path / "w"), "mail_limit": 300, "mail_body_max": 1500, **CCFG}
    cfgf.write_text(json.dumps(conf))
    tools.prefetch_mail(conf, osa=lambda *a: (0, MAIL_OUT))
    assert (tools.mail_dir(conf) / tools.SUMMARY).exists() and (tools.mail_dir(conf) / tools.STARTED).exists()
    p = subprocess.run([sys.executable, "-m", "oi.onboard_agent.tools", "mail", "--config", str(cfgf)],
                       capture_output=True, text=True, timeout=60)        # no osascript: the prefetched result
    assert p.returncode == 0 and "2 messages" in p.stdout and "page-01.md" in p.stdout
    (tools.mail_dir(conf) / tools.SUMMARY).unlink()                   # still reading: the pages ready so far
    st = tools.mail_status(conf)
    assert "still being read" in st and "1 pages ready" in st and "page-01.md" in st and "run this command again" in st


def test_mail_batches_progressive_pages_and_stop(tmp_path):
    calls = []

    def osa(script, args, timeout):
        first, n = int(args[0]), int(args[1])
        calls.append((first, n))
        return 0, "".join(mail_rec(f"P{i}", f"p{i}@example.org", f"S{i}", f"2026-10-{20 - i // 10:02d}T08:{i % 60:02d}:00",
                                   "hi") for i in range(first, first + n))
    conf = {"work": str(tmp_path), "mail_limit": 60, "mail_body_max": 1500, **CCFG}
    out = tools.prefetch_mail(conf, osa=osa)
    assert calls == [(1, 25), (26, 25), (51, 10)] and "done — 60 messages" in out and "in 3 pages" in out
    assert len(list(tools.mail_dir(conf).glob("page-*.md"))) == 3
    calls.clear()
    stops = iter([False, True])
    out = tools.prefetch_mail({**conf, "work": str(tmp_path / "b")}, osa=osa, stop=lambda: next(stops))
    assert calls == [(1, 25)] and "done — 25 messages" in out


# ---- Notion ----------------------------------------------------------------------------------------------------

def notion_fixture(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE block (id TEXT NOT NULL, space_id TEXT NOT NULL, type TEXT NOT NULL, properties TEXT,"
                " content TEXT, parent_id TEXT, parent_table TEXT, alive INTEGER NOT NULL, is_template INTEGER,"
                " last_edited_time REAL, meta_user_id TEXT NOT NULL)")

    def blk(i, typ, title=None, content=None, edited=NOW, alive=1, template=None):
        props = json.dumps({"title": [[title]]}) if title is not None else None
        con.execute("INSERT INTO block VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (i, "s", typ, props, json.dumps(content) if content else None, None, "block", alive, template,
                     edited * 1000, "u"))
    blk("p1", "page", "Villa Rossi — suivi", ["h", "t1", "l1", "gone"], edited=NOW - 3600)
    blk("h", "header", "Planning")
    blk("t1", "text", "Rendu 3D vendredi")
    blk("l1", "bulleted_list", "Envoyer plans", ["l2"])
    blk("l2", "bulleted_list", "au bureau d'études")
    blk("gone", "text", "deleted line", alive=0)
    blk("p2", "page", "Weekly", ["t2"], edited=NOW - 60)
    con.execute("UPDATE block SET properties = ? WHERE id = 'p2'",
                (json.dumps({"title": [["Week", [["b"]]], ["ly"]]}),))
    blk("t2", "to_do", "Rapport du lundi")
    blk("p3", "page", "Acme Conseil audit", ["t3"], edited=NOW - 120)
    blk("t3", "text", "confidential")
    blk("p4", "page", "Old", edited=NOW - 400 * 86400)
    blk("p5", "page", "Template", edited=NOW, template=1)
    con.commit()
    con.close()
    return path


def test_notion_reader_fixture_db(tmp_path):
    db = notion_fixture(tmp_path / "notion.db")
    before = sorted(p.name for p in tmp_path.iterdir())
    pages, removed, err = readers.read_notion(db, pages=10, days=90, cfg=CCFG, now=NOW)
    assert err is None and removed == 1
    assert [p["title"] for p in pages] == ["Weekly", "Villa Rossi — suivi"]          # rich text joined, newest first
    assert pages[0]["text"] == "[ ] Rapport du lundi"
    assert pages[1]["text"] == ("# Planning\nRendu 3D vendredi\n- Envoyer plans\n  - au bureau d'études")
    assert sorted(p.name for p in tmp_path.iterdir()) == before                       # read-only: no -wal / copy
    assert readers.read_notion(tmp_path / "none.db", pages=5, days=9, cfg=CCFG)[2] == "Notion desktop cache not found"


def test_tool_wrapper_runs_only_opted_in_tools(tmp_path, data_dir):
    home = fake_home(tmp_path)
    notion_fixture(home / "notion.db")
    plan = agent.Plan(tools=["notion"], sources={"apps": {"names": ["SketchUp"], "count": 1}})
    work, tdir = tmp_path / "work", tmp_path / "tools"
    cfg = dict(readers.DEFAULTS)
    cmds = agent.prepare(work, tdir, plan, cfg, CCFG, home)
    assert set(cmds) == {"notion"} and not (tdir / "oi-mail").exists()
    conf = json.loads((tdir / "config.json").read_text())
    conf["notion_db"] = str(home / "notion.db")
    (tdir / "config.json").write_text(json.dumps(conf))
    p = subprocess.run([cmds["notion"]], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "2 recently edited pages (1 removed as local-only)" in p.stdout
    assert "Rapport du lundi" in (work / "context" / "notion" / "page-01.md").read_text()
    r = subprocess.run([sys.executable, "-m", "oi.onboard_agent.tools", "mail", "--config", str(tdir / "config.json")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2                                                           # mail not opted in


# ---- the sandboxed command -----------------------------------------------------------------------------------

def test_agent_command_restricts_tools_roots_and_auth(tmp_path):
    work, auth = tmp_path / "work", tmp_path / "auth"
    auth.mkdir()
    roots = [tmp_path / "mem", tmp_path / "wiki"]
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "sk-x", "GH_TOKEN": "g", "TELEGRAM_BOT_TOKEN": "b",
           "PATH": "/bin"}
    argv, child = agent.agent_command(model="sonnet", mode="env", auth_tmp=auth, work=work, roots=roots,
                                      tool_cmds={"mail": "/t/oi-mail"}, system_prompt="SYS", env=env,
                                      home=tmp_path, binary="claude")
    a = " ".join(argv)
    assert argv[:3] == ["claude", "-p", agent.AGENT_INSTRUCTION]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--restricted" in argv and argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep,Write,Edit,Bash" and argv.count("--tools") == 1
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv and "--mcp-config" not in argv
    assert [argv[i + 1] for i, x in enumerate(argv) if x == "--add-dir"] == [str(r) for r in roots]
    allow = argv[argv.index("--allowedTools") + 1].split(",")
    deny = argv[argv.index("--disallowedTools") + 1].split(",")
    assert f"Write(/{work}/out/**)" in allow and f"Edit(/{work}/out/**)" in allow
    assert [x for x in allow if x.startswith(("Write", "Edit"))] == [f"Write(/{work}/out/**)", f"Edit(/{work}/out/**)"]
    assert [x for x in allow if x.startswith("Bash")] == ["Bash(/t/oi-mail)"]                # exact, no wildcard
    assert {f"Read(/{r}/**)" for r in roots} <= set(allow)
    assert "Glob" not in allow and "Grep" not in allow                                  # never unscoped
    for t in ("Read", "Glob", "Grep"):
        assert [x for x in allow if x.startswith(t + "(")] == [f"{t}(/{r}/**)" for r in
                                                                [work / "context", work / "out"] + roots]
        assert f"{t}(/{auth}/**)" in deny
        for r in roots + [work / "context"]:
            for g in ("**/*.env", "**/*secret*", "**/.credentials*", "**/*token*", "**/*.pem", "**/*.key",
                      "**/.ssh/**", "**/id_*", "**/*.keychain*", "**/Keychains/**", "**/*sops*"):
                assert f"{t}(/{r}/{g})" in deny
    assert {"WebFetch", "WebSearch", "Agent", "Task"} <= set(deny)
    assert {f"Edit(/{r}/**)" for r in roots} | {f"Write(/{r}/**)" for r in roots} <= set(deny)
    assert f"Read(/{auth}/**)" in deny
    assert argv[argv.index("--append-system-prompt") + 1] == "SYS"
    assert child["CLAUDE_CONFIG_DIR"] == str(auth) and str(work) not in child["CLAUDE_CONFIG_DIR"]
    for k in ("ANTHROPIC_API_KEY", "GH_TOKEN", "TELEGRAM_BOT_TOKEN"):
        assert k not in child
    assert "sk-x" not in a
    argv2, _ = agent.agent_command(model="opus", mode="env", auth_tmp=auth, work=work, roots=[], tool_cmds={},
                                   system_prompt="S", env=env, home=tmp_path, binary="claude")
    assert argv2[argv2.index("--tools") + 1] == "Read,Glob,Grep,Write,Edit" and "--add-dir" not in argv2
    assert not [x for x in argv2[argv2.index("--allowedTools") + 1].split(",") if x.startswith("Bash")]


def test_secret_paths_are_never_copied(tmp_path):
    for name in ("x/.env", "x/prod.env", "x/secrets.sops.env", "x/.credentials.json", "x/gh_token.txt", "x/a.pem",
                 "x/server.key", "x/.ssh/config", "x/id_ed25519", "x/login.keychain-db", "Library/Keychains/k.db",
                 "x/SECRET-notes.md"):
        assert readers.is_secret(Path("/h") / name), name
    for name in ("x/CLAUDE.md", "x/MEMORY.md", "x/user_role.md", "x/hot.md"):
        assert not readers.is_secret(Path("/h") / name), name
    home = fake_home(tmp_path)
    (home / "claude-workspaces" / "tokens").mkdir()
    (home / "claude-workspaces" / "tokens" / "CLAUDE.md").write_text("ok\n")         # a folder name is fine
    assert len(readers.claude_md_files(home)) == 3
    work, tdir = tmp_path / "w", tmp_path / "t"
    work.mkdir()
    plan = agent.Plan(claude_md=[home / ".claude" / "CLAUDE.md", home / ".claude" / ".credentials.json"])
    agent.prepare(work, tdir, plan, readers.DEFAULTS, CCFG, home)
    copied = list((work / "context" / "claude-md").iterdir())
    assert len(copied) == 1 and "{}" not in copied[0].read_text()


def test_explore_prompt_follows_links_with_a_minimum_read_count():
    p = agent.render("explore", {"min_reads": agent.MIN_READS, "sources": "S", "context": "/c", "out": "/o",
                                 "max_questions": 6, "roster": "R"})
    assert agent.MIN_READS >= 25 and f"at least {agent.MIN_READS} files" in p
    for k in ("MEMORY.md", "index.md", "hot.md", "FOLLOW its links", "FOLLOW their links"):
        assert k in p, k
    assert "{{" not in p


def test_describe_shows_tool_and_path_never_content(tmp_path):
    ev = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "the client is SECRET"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": str(tmp_path / "home/.claude/x.md")}},
        {"type": "tool_use", "name": "Write", "input": {"file_path": str(tmp_path / "w/out/profile.yaml"),
                                                        "content": "SECRET"}}]}}
    d = agent.describe(ev, tmp_path / "home", tmp_path / "w")
    assert d == "  · Read ~/.claude/x.md\n  · Write out/profile.yaml" and "SECRET" not in d
    assert agent.describe({"type": "user"}, tmp_path, tmp_path) is None


# ---- the session (fake agent) --------------------------------------------------------------------------------

PROFILE = {"job": "Architect", "summary": "Runs a studio; villas.", "tools": ["SketchUp", "Mail"],
           "clients_projects": ["Villa Rossi", "Acme Conseil audit"], "recurring_tasks": ["Weekly report — Monday"],
           "attention": {"sources": {"mail": 0.8}, "noise": [{"who": "pinterest.com", "class": "topic", "weight": 0.05}]}}


def cat_items(final: bool):
    items = [
        {"id": "sketchup", "label": "Open SketchUp", "primitive": "app.open", "params": {"app": "SketchUp"},
         "trigger": {"apps": ["Mail"], "hours": [9, 18]}, "prior": 0.5, "why": "daily, notes"},
        {"id": "render", "label": "Render request", "primitive": "mail.draft",
         "params": {"to": "lea@villa-rossi.it", "subject": "Rendu", "body": "Bonjour"}, "trigger": {}, "prior": 0.9},
        {"id": "ghost", "label": "Ghost app", "primitive": "app.open", "params": {"app": "NotInstalled"}},
        {"id": "phish", "label": "Pay invoice", "primitive": "url.open", "params": {"href": "https://x.io/p?id=1"}},
        {"id": "acme", "label": "Acme portal", "primitive": "url.open", "params": {"href": "https://acme-conseil.example/"}},
        {"id": "pin", "label": "Pinterest", "primitive": "url.open", "params": {"href": "https://pinterest.com/"}},
        {"id": "shell", "label": "Run", "primitive": "shell.run", "params": {"cmd": "rm -rf ~"}},
    ]
    if final:
        items.append({"id": "weekly", "label": "Weekly report", "primitive": "mail.draft",
                      "params": {"to": "", "subject": "Weekly", "body": ""}, "trigger": {"days": ["mon"], "hours": [8, 11]},
                      "prior": 0.45})
    return items


class FakeAgent:
    """Records each run (argv, stdin, env) and plays the agent: writes the files, streams stream-json events."""

    def __init__(self, questions=("Which day is the weekly report?", "Who receives renders?"), fail=None):
        self.runs, self.questions, self.fail = [], list(questions), fail

    def __call__(self, argv, env, cwd, stdin, timeout, on_line):
        self.runs.append({"argv": argv, "stdin": stdin, "env": env, "cwd": Path(cwd), "timeout": timeout,
                          "context": sorted(str(p.relative_to(cwd)) for p in Path(cwd, "context").rglob("*"))})
        if self.fail:
            on_line(json.dumps({"type": "result", "is_error": True, "result": self.fail}))
            return 1
        out = Path(cwd) / "out"
        final = len(self.runs) == 2
        (out / "profile.yaml").write_text(yaml.safe_dump(PROFILE))
        (out / "starter_catalogue.yaml").write_text(yaml.safe_dump({"items": cat_items(final)}))
        if not final:
            (out / "questions.json").write_text(json.dumps({"questions": self.questions + ["q"] * 6}))
            (out / "notes.md").write_text("notes")
        on_line(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": str(Path(cwd) / "context/sources.md")}}]}}))
        on_line("not json")
        on_line(json.dumps({"type": "result", "is_error": False, "num_turns": 7, "result": "done"}))
        return 0


APPS = {"names": ["SketchUp", "Mail", "Safari"], "count": 3}


def run_session(tmp_path, data_dir, replies, *, interactive=True, fake=None, preset=None, dry_run=False):
    home = fake_home(tmp_path)
    write_settings(data_dir, {"memory_roots": ["~/notes"]})
    fake = fake or FakeAgent()
    out = io.StringIO()
    it = iter(replies)
    rc = agent.session(onboard.IO(lambda p: next(it), out, interactive=interactive), home=home, runner=fake,
                       read=lambda k, deny: APPS if k == "apps" else {"why": "test"}, has_roster=False,
                       env={"CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "sk-x", "PATH": os.environ["PATH"]},
                       out_dir=tmp_path / "installed", preset=preset, dry_run=dry_run,
                       osa=lambda script, args, timeout: (0, MAIL_OUT))
    return rc, fake, out.getvalue(), home


def test_session_end_to_end(tmp_path, data_dir):
    # memory y, wiki y, mail y, notion (not found: shown, not asked), activity n, safari n
    replies = ["y", "y", "y", "n", "n", "Monday 9h", "", "q3", "", "", "", "r 1 SketchUp now", "2", ""]
    rc, fake, text, home = run_session(tmp_path, data_dir, replies)
    assert rc == 0 and len(fake.runs) == 2
    first, second = fake.runs
    argv = first["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"                                   # default model
    adds = [argv[i + 1] for i, x in enumerate(argv) if x == "--add-dir"]
    assert adds == [str((home / ".claude/projects/-ws-a/memory")), str(home / "wiki"),
                    str((home / "notes").resolve())]
    assert str(home / ".claude") not in adds and not any(a.endswith("-ws-b/memory") for a in adds)
    bash = [x for x in argv[argv.index("--allowedTools") + 1].split(",") if x.startswith("Bash")]
    assert len(bash) == 1 and bash[0].startswith("Bash(/") and bash[0].endswith("/oi-mail)")
    assert "oi-notion" not in " ".join(argv)
    assert "ANTHROPIC_API_KEY" not in first["env"]
    assert "context/mail/.prefetch-started" in first["context"] and "context/sources.md" in first["context"]
    assert len([c for c in first["context"] if c.startswith("context/claude-md/")]) == 2
    assert "oi-mail" in first["stdin"] and str(home / "wiki") in first["stdin"]
    assert "Monday 9h" in second["stdin"] and "q3" in second["stdin"] and "Who receives renders?" not in second["stdin"]
    assert "Claude has 6 question(s)" in text                  # capped at 6
    assert "· Read context/sources.md" in text
    for r in fake.runs:                                                                   # everything cleaned up
        assert not r["cwd"].exists()
    prof = yaml.safe_load((tmp_path / "installed" / "profile.yaml").read_text())
    cat = yaml.safe_load((tmp_path / "installed" / "starter_catalogue.yaml").read_text())
    assert oct((tmp_path / "installed" / "profile.yaml").stat().st_mode & 0o777) == "0o600"
    assert prof["by"] == "oi onboard (W22b agent)" and prof["job"] == "Architect"
    assert prof["clients_projects"] == ["Villa Rossi"]                                   # local-only dropped
    assert {"memory", "wiki", "mail"} <= set(prof["sources"])
    ids = [i["id"] for i in cat["items"]]
    for bad in ("ghost", "phish", "acme", "pin", "shell"):                               # validation + noise
        assert bad not in ids
    assert "render" not in ids                                                            # removed at the last edit
    first_item = cat["items"][0]
    assert first_item["id"] == "sketchup" and first_item["label"] == "SketchUp now"     # renamed
    assert "weekly" in ids and next(i for i in cat["items"] if i["id"] == "weekly")["trigger"]["days"] == ["mon"]
    assert all(0.05 <= i["prior"] <= 0.6 for i in cat["items"])


def test_session_claude_unavailable_writes_nothing(tmp_path, data_dir):
    rc, fake, text, _ = run_session(tmp_path, data_dir, ["y", "y", "y", "n", "n"],
                                    fake=FakeAgent(fail="Not logged in · Please run /login"))
    assert rc == 1 and "Claude unavailable (stub)" in text and "--offline" in text
    assert not (tmp_path / "installed").exists() and not fake.runs[0]["cwd"].exists()


def test_session_non_interactive_preset_dry_run(tmp_path, data_dir):
    rc, fake, text, _ = run_session(tmp_path, data_dir, [], interactive=False, dry_run=True,
                                    preset={"sources": ["wiki"], "team": False})
    assert rc == 0 and len(fake.runs) == 1 and "left unanswered" in text and "dry run: nothing written" in text
    argv = fake.runs[0]["argv"]
    assert "Bash" not in argv[argv.index("--tools") + 1]                                 # no mail / notion chosen
    assert not (tmp_path / "installed").exists()


def test_session_model_from_settings_and_flag(tmp_path, data_dir):
    home = fake_home(tmp_path)
    write_settings(data_dir, {"model": "opus"})
    for model, want in ((None, "opus"), ("haiku", "haiku")):
        fake = FakeAgent(questions=())
        agent.session(onboard.IO(lambda p: "", io.StringIO(), interactive=False), home=home, runner=fake, model=model,
                      read=lambda k, d: APPS, has_roster=False, env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
                      preset={"sources": [], "team": False}, dry_run=True)
        argv = fake.runs[0]["argv"]
        assert argv[argv.index("--model") + 1] == want


def test_cli_routes_default_to_agent_and_offline_to_w22(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(agent, "session", lambda io_, **kw: seen.setdefault("kw", kw) and 0)
    assert cli.main(["onboard", "--dry-run", "--model", "opus"]) == 0
    assert seen["kw"]["model"] == "opus" and seen["kw"]["dry_run"] is True
    seen.clear()
    ans = tmp_path / "a.yaml"
    ans.write_text(yaml.safe_dump({"answers": {"job": "x"}, "sources": [], "team": False}))
    monkeypatch.setattr(onboard, "converse", lambda *a, **k: ({"job": "x"}, {"apps": APPS}, False, set()))
    assert cli.main(["onboard", "--offline", "--no-claude", "--dry-run", "--answers", str(ans)]) == 0
    assert not seen
