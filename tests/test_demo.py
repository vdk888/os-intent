"""`oi demo`: every scene runs headless against a fake oi-hud socket; isolation and fake-data guards."""
from __future__ import annotations

import json
import shutil
import socketserver
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from oi import cli, demo


class FakeHUD:
    """A tiny oi-hud: records every command and answers like the real one (hint develop, glide expand, presses,
    wish resolve, note, outward gate)."""

    def __init__(self, agents=("Atlas", "Nova", "Orion", "Lyra")):
        self.dir = Path(tempfile.mkdtemp(prefix="oi-fake-", dir="/tmp"))
        self.path = str(self.dir / "hud.sock")
        self.agents = list(agents)
        self.cmds: list[dict] = []
        self.conns: list = []
        self.hint_owner = self.owner = self.resolver = self.note_owner = None
        self.visible: list[dict] = []
        self.armed: set[int] = set()
        self.glide: list[int] | None = None
        self.wish_id = 0
        fake = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                fake.conns.append(self)
                for line in self.rfile:
                    if line.strip():
                        fake.on(json.loads(line), self)

        class S(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.server = S(self.path, H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def tell(conn, obj):
        if conn is not None:
            try:
                conn.wfile.write((json.dumps(obj) + "\n").encode())
                conn.wfile.flush()
            except OSError:                     # the demo already closed its connection
                pass

    def broadcast(self, obj):
        for c in self.conns:
            self.tell(c, obj)

    def on(self, c, conn):
        cmd = c["cmd"]
        if cmd == "_sync":                      # tests: every earlier command of this connection is handled
            self.tell(conn, {"event": "_sync"})
            return
        self.cmds.append(c)
        ok = {"event": "ok"}
        if cmd in ("show", "shot_show"):
            if c.get("mode") == "hint":
                self.hint_owner = (conn, c["options"])
                return
            self.owner, self.visible, self.armed = conn, c["options"], set()
            if self.glide:
                self.tell(conn, {"event": "glide", "phase": "start"})
                self.tell(conn, {"event": "expand", "path": self.glide[:1]})
        elif cmd == "subring":
            self.tell(conn, ok)
            p = c["path"]
            if c["options"]:
                self.last_ring = c["options"]
            if self.glide and len(p) < len(self.glide):
                self.tell(conn, {"event": "expand", "path": self.glide[:len(p) + 1]})
            elif self.glide:
                leaf = next((o for o in self.last_ring if o["key"] == self.glide[-1]), {})
                ev = {"event": "pressed", "key": self.glide[0], "path": self.glide, "via": "glide"}
                if leaf.get("claude"):
                    ev["mode"] = "claude"
                self.glide = None
                self.tell(self.owner, ev)
        elif cmd == "debug_summon":
            self.tell(conn, ok)
            if self.hint_owner:
                owner, opts = self.hint_owner
                self.hint_owner = None
                self.owner, self.visible = owner, opts
                self.tell(owner, {"event": "hint", "phase": "expanded"})
            else:
                self.broadcast({"event": "summon", "ts": "2026-10-22T08:00:00Z"})
        elif cmd == "debug_glide":
            self.tell(conn, ok)
            self.glide = c["path"]
            self.broadcast({"event": "summon", "ts": "2026-10-22T08:00:00Z"})
        elif cmd == "debug_press":
            self.tell(conn, ok)
            o = next((o for o in self.visible if o["key"] == c["key"]), None)
            if o is None:
                return
            if o.get("confirm") and c["key"] not in self.armed:
                self.armed.add(c["key"])
                return
            ev = {"event": "pressed", "key": c["key"], "via": "debug"}
            if c.get("mode") == "claude":
                ev["mode"] = "claude"
            else:
                self.visible = []
            self.tell(self.owner, ev)
        elif cmd == "resolver":
            self.resolver = conn
            self.tell(conn, ok)
        elif cmd == "debug_wish":
            self.tell(conn, ok)
            self.wish_id += 1
            self.tell(self.resolver, {"event": "resolve", "id": self.wish_id, "text": c["text"], "final": False,
                                      "confirmed": False, "context": {"app": c.get("app"), "space": 1}})
            if c.get("enter"):
                self.wish_id += 1
                self.tell(self.resolver, {"event": "resolve", "id": self.wish_id, "text": c["text"], "final": True,
                                          "confirmed": False, "context": {"app": c.get("app"), "space": 1}})
        elif cmd == "state":
            self.tell(conn, {"event": "state", "visible": bool(self.visible), "agents": self.agents})
        elif cmd == "shot_note":
            self.note_owner = conn
        elif cmd == "debug_note":
            self.tell(conn, ok)
            self.tell(self.note_owner, {"event": "note", "text": "…"})
        elif cmd == "confirm_outward":
            self.outward_owner = conn
        elif cmd == "debug_outward":
            self.tell(conn, ok)
            self.tell(self.outward_owner, {"event": "outward", "id": "demo-1",
                                            "decision": "validated" if c.get("decision") == "validate" else "cancelled",
                                            "reason": "debug"})
        elif cmd in ("hide", "debug_vocal", "result", "resolution"):
            if cmd == "hide":
                self.visible = []
            self.tell(conn, ok)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def sent(self, name):
        return [c for c in self.cmds if c["cmd"] == name]


@pytest.fixture
def hud():
    h = FakeHUD()
    yield h
    h.close()


def sync(d: demo.Demo):
    d.mark()
    d.conn.send({"cmd": "_sync"})
    assert d.wait(lambda e: e.get("event") == "_sync", 5)


def play(h: FakeHUD, *names: str):
    lines: list[str] = []
    conn = demo.Conn(h.path)
    try:
        d = demo.run(list(names), conn, speed=0, lead_s=0, gap_s=0, out=lines.append)
        sync(d)
    finally:
        conn.close()
    return d, lines


FORBIDDEN = {"vocal_to", "ax_press", "page", "wish"}   # real microphone, page presses, saved wishes


def assert_safe(h: FakeHUD):
    assert not FORBIDDEN & {c["cmd"] for c in h.cmds}
    assert all(c.get("decision") != "validate" for c in h.sent("debug_outward"))


@pytest.mark.parametrize("name", demo.SCENE_NAMES)
def test_each_scene_runs_headless(hud, name):
    d, lines = play(hud, name)
    assert lines[0].startswith(f"== {name}")
    assert hud.cmds and hud.cmds[-1]["cmd"] == "hide"
    assert not [e for e in d.seen if e.get("event") == "error"]
    assert_safe(hud)


def test_day1_offers_only_generic_intents(hud):
    d, _ = play(hud, "day1")
    s = hud.sent("show")[0]
    assert s["mode"] == "hint" and s["marker"] == "ring" and s["options"] == demo.DAY1_MENU
    assert [o["label"] for o in demo.DAY1_MENU] == ["Safari", "Open Calendar", "Finder", "Desk 2"]
    assert not any(o.get("claude") for o in demo.DAY1_MENU)                # no history: nothing personal
    assert any(e.get("event") == "hint" for e in d.seen)                   # the cross developed on the mail
    assert not hud.sent("debug_press") and not hud.sent("result") and not hud.sent("confirm_outward")


def test_day30_same_mail_reply_draft_then_gate_cancelled(hud):
    d, lines = play(hud, "day30")
    s = hud.sent("show")[0]
    assert s["mode"] == "hint" and s["marker"] == "ring" and s["options"] == demo.DAY30_MENU
    labels = [o["label"] for o in demo.DAY30_MENU]
    assert labels == ["Reply to Sam", "Open Calendar: Friday", "Forward to Dana Park", "Desk 2"]   # Juno's order
    assert {"cmd": "debug_press", "key": 1, "mode": "claude"} in hud.cmds
    assert hud.sent("result") == [{"cmd": "result", "key": 1, "ok": True, "text": demo.DAY30_DRAFT, "ttl_ms": 8000}]
    assert demo.DAY30_DRAFT.startswith("Draft, not sent") and "Friday 10:00 works" in demo.DAY30_DRAFT
    gate = hud.sent("confirm_outward")
    assert len(gate) == 1 and gate[0]["cls"] == "reply" and gate[0]["title"] == "Send this reply to Sam?"
    assert hud.sent("debug_outward") == [{"cmd": "debug_outward", "decision": "cancel"}]
    assert [e["decision"] for e in d.seen if e.get("event") == "outward"] == ["cancelled"]
    names = [c["cmd"] for c in hud.cmds]                                   # draft first, then the gate
    assert names.index("result") < names.index("confirm_outward") < names.index("debug_outward")


def test_day1_and_day30_are_the_same_mail_on_the_same_backdrop(hud):
    _, l1 = play(hud, "day1")
    _, l30 = play(hud, "day30")
    mail = next(s for s in l1 if "new mail" in s).split("] ", 1)[1]
    assert any(s.endswith(mail) for s in l30)
    pages = {n: p for n, _, _, p in demo.SCENES}
    assert pages["day1"] == pages["day30"] == "inbox.html" and pages["later"] == "title.html"
    assert "Thursday review: can we move it?" in (demo.BACKDROPS / "inbox.html").read_text()


def test_contrast_plays_day1_title_card_day30(hud, monkeypatch):
    played = []
    monkeypatch.setattr(demo, "run", lambda names, conn, **kw: played.append(names) or demo.Demo(conn))
    assert cli.main(["demo", "contrast", "--socket", hud.path, "--lead", "0", "--force"]) == 0
    assert played == [["day1", "later", "day30"]]
    monkeypatch.undo()
    _, lines = play(hud, *demo.CONTRAST)
    assert [s.split()[1] for s in lines if s.startswith("== ")] == ["day1", "later", "day30"]
    assert any(demo.LATER_TITLE in s for s in lines)
    assert "30 days later" in (demo.BACKDROPS / "title.html").read_text()
    assert demo.SCENE_NAMES[:3] == demo.CONTRAST                          # the film opens on the contrast
    assert_safe(hud)


def test_hint_dots_develop_then_one_key(hud):
    d, _ = play(hud, "hint")
    shows = hud.sent("show")
    assert shows[0]["mode"] == "hint" and shows[0]["options"] == demo.MAIL_MENU
    assert [c["cmd"] for c in hud.cmds[1:3]] == ["debug_summon", "debug_press"]
    assert any(e.get("event") == "hint" for e in d.seen)
    assert any(e.get("event") == "pressed" and e["key"] == 4 for e in d.seen)


def test_glide_answers_the_expansions_and_shows_the_draft_card(hud):
    d, _ = play(hud, "glide")
    assert hud.sent("debug_glide") == [{"cmd": "debug_glide", "path": [2, 1], "cancel": False}]
    assert hud.sent("show")[0]["options"] == demo.MAIL_MENU          # the summon was answered
    rings = hud.sent("subring")
    assert rings[0]["path"] == [2] and rings[0]["options"] == demo.GLIDE_RINGS[(2,)]
    assert rings[1] == {"cmd": "subring", "path": [2, 1], "options": []}   # a leaf
    assert hud.sent("result") == [{"cmd": "result", "key": 2, "ok": True, "text": demo.SAM_DRAFT, "ttl_ms": 8000}]


def test_longpress_spinner_then_canned_card(hud):
    play(hud, "longpress")
    assert {"cmd": "debug_press", "key": 2, "mode": "claude"} in hud.cmds
    assert hud.sent("result")[0]["text"] == demo.SAM_DRAFT


def test_page_action_arms_then_fires_and_presses_nothing(hud):
    d, _ = play(hud, "page")
    assert [c["key"] for c in hud.sent("debug_press")] == [2, 2]
    assert [e["key"] for e in d.seen if e.get("event") == "pressed"] == [2]   # first tap armed only
    assert not hud.sent("ax_press")


def test_wish_types_char_by_char_and_resolves_locally(hud):
    play(hud, "wish")
    typed = [c["text"] for c in hud.sent("debug_wish")]
    assert typed[0] == "o" and typed[-2] == typed[-1] == "open telegram"
    assert hud.sent("debug_wish")[-1]["enter"] is True
    res = hud.sent("resolution")
    assert res[-1]["state"] == "done" and res[-1]["label"] == "✓ Telegram"
    assert any(r["state"] == "preview" and r["label"] == "→ Telegram" for r in res)


def test_vocal_needs_the_fictional_roster():
    h = FakeHUD(agents=["Atlas", "Nova", "Hera", "Echo"])
    try:
        conn = demo.Conn(h.path)
        with pytest.raises(demo.DemoError, match="real agent roster"):
            demo.run(["vocal"], conn, speed=0, lead_s=0, out=lambda s: None)
        sync(demo.Demo(conn))
        conn.close()
        assert not h.sent("debug_vocal")                                     # nothing drawn
    finally:
        h.close()


def test_vocal_picks_nova_with_the_transcript(hud):
    play(hud, "vocal")
    assert hud.sent("debug_vocal") == [{"cmd": "debug_vocal"},
                                       {"cmd": "debug_vocal", "pick": "nova", "text": "Move the review to Friday"}]


def test_screenshot_offer_note_and_nothing_sent(hud):
    d, lines = play(hud, "screenshot")
    assert hud.sent("shot_show")[0]["options"] == demo.SHOT_MENU
    assert hud.sent("result")[0] == {"cmd": "result", "key": 1, "ok": True, "ttl_ms": 8000}   # no text = hide
    assert hud.sent("shot_note")[0]["text"] == demo.SHOT_NOTE
    assert any("nothing sent" in s for s in lines)


def test_proactive_ring_hint_develops(hud):
    d, _ = play(hud, "proactive")
    s = hud.sent("show")[0]
    assert s["mode"] == "hint" and s["marker"] == "ring"
    assert any(e.get("event") == "hint" for e in d.seen)


def gate_is_cancelled(hud, d, n=1):
    gates = hud.sent("confirm_outward")
    assert len(gates) == n and all(g["cls"] in ("vocal", "shot", "page", "reply") for g in gates)   # the real gate classes
    assert hud.sent("debug_outward") == [{"cmd": "debug_outward", "decision": "cancel"}] * n
    assert [e["decision"] for e in d.seen if e.get("event") == "outward"] == ["cancelled"] * n
    return gates[0]


def test_architect_friday_render_reply_draft_gate_cancelled(hud):
    d, _ = play(hud, "architect")
    assert hud.sent("show")[0]["options"] == demo.ARCHITECT_MENU
    assert hud.sent("result")[0]["text"] == demo.ARCHITECT_DRAFT and "Friday" in demo.ARCHITECT_DRAFT
    assert gate_is_cancelled(hud, d)["title"] == "Send this reply to Lena?"
    assert_safe(hud)


def test_manager_glides_on_forward_to_paul_and_sarah(hud):
    d, _ = play(hud, "manager")
    assert hud.sent("debug_glide") == [{"cmd": "debug_glide", "path": [2, 1], "cancel": False}]
    ring = hud.sent("subring")[0]
    assert ring["path"] == [2] and [o["label"] for o in ring["options"]] == ["Paul Weston", "Sarah Kim"]
    assert demo.MANAGER_MENU[1]["label"] == "Forward"
    assert "Paul Weston" in gate_is_cancelled(hud, d)["title"]
    assert_safe(hud)


def test_recruiter_candidate_reply_and_calendar(hud):
    d, _ = play(hud, "recruiter")
    labels = [o["label"] for o in hud.sent("show")[0]["options"]]
    assert labels[0] == "Reply to Priya" and any(l.startswith("Open Calendar") for l in labels)
    assert hud.sent("result")[0]["text"] == demo.RECRUITER_DRAFT
    assert gate_is_cancelled(hud, d)["title"] == "Send this reply to Priya?"
    assert_safe(hud)


def test_validation_scene_naming_a_fictional_agent_is_retired():
    assert "validation" not in demo.SCENE_NAMES and not hasattr(demo, "OUTWARD") and not hasattr(demo, "scene_validation")


def test_all_runs_every_scene_in_film_order(hud):
    _, lines = play(hud, *demo.SCENE_NAMES)
    assert [s.split()[1] for s in lines if s.startswith("== ")] == demo.SCENE_NAMES
    assert_safe(hud)


# ---- isolation ------------------------------------------------------------------------------------------------

def test_live_data_dir_is_refused(tmp_path):
    with pytest.raises(demo.DemoError):
        demo.refuse_live(demo.live_data_dir())
    with pytest.raises(demo.DemoError):
        demo.refuse_live(demo.live_data_dir() / "sub")
    demo.refuse_live(tmp_path)                                              # anything else is fine
    rc = cli.main(["demo", "hint", "--socket", str(demo.live_data_dir() / "hud.sock"), "--lead", "0"])
    assert rc == 2


def test_live_hud_enabled_refuses_unless_forced(tmp_path, monkeypatch, capsys):
    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setattr(demo, "live_data_dir", lambda: live)
    assert demo.live_hud_state() is None                                    # no live install: fine
    (live / "settings.json").write_text('{"hud_enabled": false}')
    assert demo.live_hud_state() is None
    (live / "settings.json").write_text('{"hud_enabled": true}')
    assert "hud_enabled" in demo.live_hud_state()
    rc = cli.main(["demo", "hint", "--lead", "0"])                          # refused before any HUD starts
    assert rc == 2 and "untick" in capsys.readouterr().err
    demo.refuse_live_hud(force=True)
    # a live hud.sock that answers counts even with the setting off
    (live / "settings.json").write_text('{"hud_enabled": false}')
    srv = FakeHUD()
    try:
        monkeypatch.setattr(demo, "live_data_dir", lambda: Path(srv.path).parent)
        assert "hud.sock" in demo.live_hud_state()
    finally:
        srv.close()


def test_prepare_dir_and_the_vocal_stub_send_nothing(tmp_path):
    root = tmp_path / "d"
    stub = demo.prepare_dir(root)
    s = json.loads((root / "settings.json").read_text())
    assert s["repo_dir"] == str(root) and s["gate"]["ttl_ms"] == 15000 and s["claude"]["enabled"] is False
    assert (root / "settings.json").stat().st_mode & 0o777 == 0o600
    out = subprocess.run([str(stub), "vocal-send", "--to", "nova"], capture_output=True, text=True)
    assert out.returncode == 0 and json.loads(out.stdout) == {"ok": True, "dry_run": True, "demo": True}


def test_demo_env_strips_secrets_and_isolates(tmp_path):
    base = {"PATH": "/usr/bin", "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_STATE_DIR": "y", "GH_TOKEN": "g",
            "GITHUB_TOKEN": "h", "ANTHROPIC_API_KEY": "k", "OI_DATA_DIR": "/Users/someone/.local/share/os-intent"}
    env = demo.demo_env(tmp_path, tmp_path / "stub", base)
    assert env == {"PATH": "/usr/bin", "OI_DATA_DIR": str(tmp_path), "OI_VOCAL_DRY_RUN": "1",
                   "OI_VOCAL_BIN": str(tmp_path / "stub"), "OI_HUD_AGENTS": demo.AGENTS_SPEC}


def test_hud_bin_never_falls_back_to_the_live_install(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "PKG", tmp_path)
    monkeypatch.delenv("OI_DEMO_HUD_BIN", raising=False)
    with pytest.raises(demo.DemoError, match="swift build"):
        demo.find_hud_bin(None)
    b = tmp_path / "hud" / ".build" / "release" / "oi-hud"
    b.parent.mkdir(parents=True)
    b.write_text("#!/bin/sh\n")
    b.chmod(0o755)
    assert demo.find_hud_bin(None) == b


# ---- fake data only -------------------------------------------------------------------------------------------

def test_scene_data_is_fictional(hud):
    play(hud, *demo.SCENE_NAMES)
    blob = json.dumps(hud.cmds, ensure_ascii=False) + demo.AGENTS_SPEC
    for real in demo.REAL_AGENT_NAMES:
        assert real not in blob, real


def test_every_backdrop_exists_and_is_fictional():
    for _, _, _, page in demo.SCENES:
        html = (demo.BACKDROPS / page).read_text()
        assert "fictional demo data" in html and not any(n in html for n in demo.REAL_AGENT_NAMES)


def test_list(capsys):
    assert cli.main(["demo", "list"]) == 0
    assert capsys.readouterr().out.split()[0] == "day1"
