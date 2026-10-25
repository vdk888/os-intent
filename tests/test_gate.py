"""Outward gate: every outward path refuses without an explicit validate, sends after it (dry
run), and Esc / a timeout / no HUD cancels. Never a real send, never the real HUD (tests/conftest.py `gate_asks`)."""
from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from oi import cli, gate, intent_resolve, live, page_actions, paths, shots, vocal

T = datetime(2026, 10, 20, 21, 50, tzinfo=timezone.utc).timestamp()
WHEN = datetime(2026, 10, 20, 21, 50, tzinfo=timezone.utc)
CANCELS = ["cancelled", "timeout", "hud_down", lambda req: {"event": "outward", "id": "other", "decision": "validated"},
           lambda req: {"event": "error", "message": "bad_json"}, lambda req: {}]
CANCEL_IDS = ["esc", "timeout", "hud_down", "wrong_id", "hud_error", "garbage"]


@pytest.fixture(autouse=True)
def no_real_send(monkeypatch):
    def tripwire(*a, **k):
        raise AssertionError("a real sender ran")
    monkeypatch.setattr(vocal, "_run", tripwire)
    monkeypatch.setattr(vocal, "_append_inject", tripwire)
    monkeypatch.setattr(vocal, "_raw_settings", lambda: {})


def gate_rows():
    d = paths.data_dir() / "gate-log"
    return [json.loads(l) for f in sorted(d.glob("*.jsonl")) for l in f.read_text().splitlines()] if d.exists() else []


class Recorder:
    def __init__(self):
        self.calls = []

    def runner(self, argv, data):
        self.calls.append(("a2a", argv, data))
        return 0, ""

    def inject(self, path, line):
        self.calls.append(("inject", path, line))


# ---- the gate itself -------------------------------------------------------------------------------------

def test_validate_lets_it_through_and_logs_ids_only(gate_asks):
    g = gate.outward("vocal", "orion", "Orion", "Salut Orion, la PR 2230 est prête pour toi", action_key="vocal")
    req = gate_asks.asked[-1]
    assert g["ok"] and g["decision"] == "validated" and g["id"] == req["id"]
    assert req["cmd"] == "confirm_outward" and req["title"] == "Envoyer ce message à Orion ?"
    assert req["preview"] == ["Salut Orion, la PR 2230 est prête pour toi"] and req["action_key"] == "vocal"
    assert req["ttl_ms"] == gate.TTL_MS
    row = gate_rows()[-1]
    assert row == {**row, "kind": "gate", "id": g["id"], "cls": "vocal", "to": "orion", "decision": "validated", "sent": True}
    assert "Salut" not in json.dumps(row) and "Orion" not in json.dumps(row)     # ids only: no preview, no name


@pytest.mark.parametrize("answer", CANCELS, ids=CANCEL_IDS)
def test_anything_but_validate_cancels(gate_asks, answer):
    gate_asks.answer = answer
    g = gate.outward("shot", "atlas", "Atlas", "x")
    assert not g["ok"] and g["decision"] == "cancelled" and g["error"].startswith("cancelled (")
    assert gate_rows()[-1]["decision"] == "cancelled" and gate_rows()[-1]["sent"] is False


def test_english_titles_and_preview_lines(gate_asks):
    gate.outward("vocal", "orion", "Orion", "x", raw={"wish": {"locale": "en-US"}})
    assert gate_asks.asked[-1]["title"] == "Send this to Orion?" and gate_asks.asked[-1]["lang"] == "en"
    gate.outward("page", "button:squash-and-merge", "Squash & merge", "github.com", raw={})
    assert gate_asks.asked[-1]["title"] == "Cliquer « Squash & merge » ?"
    lines = gate.preview_lines(" ".join(["mot"] * 200))
    assert len(lines) == 3 and all(len(l) <= gate.PREVIEW_LINE_CHARS for l in lines) and lines[-1].endswith("…")
    assert gate.preview_lines("a\n\nb") == ["a b"] and gate.preview_lines(None) == []


def test_setting_off_globally_or_per_class(gate_asks):
    assert gate.outward("vocal", "orion", "Orion", "x", raw={"gate": {"outward": False}})["decision"] == "off"
    per = {"gate": {"outward": {"page": False}}}
    assert gate.outward("page", "button:approve", "Approve", "github.com", raw=per)["decision"] == "off"
    assert gate.outward("vocal", "orion", "Orion", "x", raw=per)["decision"] == "validated"     # a missing class stays on
    assert gate.outward("vocal", "orion", "Orion", "x", raw={"gate": {"outward": "nope"}})["decision"] == "validated"
    assert len(gate_asks.asked) == 2 and gate_rows()[0]["decision"] == "off"
    with pytest.raises(gate.GateError):
        gate.outward("mail", "x", "X", "y")


def test_settings_file_is_read(gate_asks):
    paths.ensure_private_root()
    (paths.data_dir() / "settings.json").write_text(json.dumps({"gate": {"show_min_p": 0.2, "outward": False}}))
    assert gate.outward("vocal", "orion", "Orion", "x")["decision"] == "off" and gate_asks.asked == []


def test_page_outward_is_fail_safe():
    """Gated unless KNOWN read-only (a learned label nobody listed asks too)."""
    for s in ("squash-and-merge", "approve", "delete-branch", "close-pull-request", "send", "comment", "publish",
              "ship-it", "go-live", "lgtm", "deploy-to-production", "", None):
        assert gate.page_outward(s, "button"), s
    for s in ("delete-branch", "merge", "post-comment"):
        assert gate.page_outward(s, "link"), s                      # a link with an outward word asks too
    for s in ("code", "files-changed", "insights", "copy", "expand-all", "show-more", "view-file", "next"):
        assert not gate.page_outward(s, "button"), s
    assert not gate.page_outward("anything-at-all", "link")         # a link = navigation


# ---- the HUD round trip (a fake hud.sock) ------------------------------------------------------------------

@pytest.fixture
def short_sock(monkeypatch):
    """AF_UNIX paths are short: the fake hud.sock lives in a fresh /tmp dir."""
    d = tempfile.mkdtemp(prefix="oig", dir="/tmp")
    monkeypatch.setattr(paths, "hud_socket", lambda: Path(d) / "h.sock")
    yield
    shutil.rmtree(d, ignore_errors=True)


def _fake_hud(answer):
    """A one-connection hud.sock: reads the request, writes `answer(req)` lines."""
    path = paths.hud_socket()
    path.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    got = {}

    def work():
        c, _ = srv.accept()
        with c:
            buf = b""
            while b"\n" not in buf:
                buf += c.recv(4096)
            got["req"] = json.loads(buf.split(b"\n")[0])
            for line in answer(got["req"]):
                c.sendall((json.dumps(line) + "\n").encode())
            c.recv(1)                                # hold until the client closes
        srv.close()
    t = threading.Thread(target=work, daemon=True)
    t.start()
    return got


def test_hud_ask_waits_for_its_own_outward_event(short_sock):
    got = _fake_hud(lambda r: [{"event": "wish", "text_len": 3},
                               {"event": "outward", "id": r["id"], "decision": "validated", "reason": "return"}])
    g = gate.outward("vocal", "orion", "Orion", "Salut", ask=gate.hud_ask)
    assert g["ok"] and g["reason"] == "return" and got["req"]["id"] == g["id"]


def test_hud_ask_esc_and_silence_cancel(monkeypatch, short_sock):
    _fake_hud(lambda r: [{"event": "outward", "id": r["id"], "decision": "cancelled", "reason": "esc"}])
    assert gate.outward("vocal", "orion", "Orion", "x", ask=gate.hud_ask)["reason"] == "esc"
    _fake_hud(lambda r: [])                                           # the HUD never answers
    monkeypatch.setattr(gate, "TTL_MS", 100)
    monkeypatch.setattr(gate, "REPLY_SLACK_S", 0.2)
    g = gate.outward("vocal", "orion", "Orion", "x", ask=gate.hud_ask)
    assert not g["ok"] and g["reason"] == "timeout"


def test_no_hud_cancels(short_sock):
    g = gate.outward("vocal", "orion", "Orion", "x", ask=gate.hud_ask)            # nothing listens on hud.sock
    assert not g["ok"] and g["reason"] == "hud_down"


# ---- every outward path ------------------------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["atlas", "nova"])          # inject (local) and a2a (relay command)
def test_vocal_send_refuses_without_validate(gate_asks, slug):
    rec = Recorder()
    for answer in ("cancelled", "timeout"):
        gate_asks.answer = answer
        for dry in (False, True):
            out = io.StringIO()
            r = vocal.send(slug, "Salut", now=T, runner=rec.runner, inject=rec.inject, raw_settings={}, dry_run=dry, out=out)
            assert not r["ok"] and r["error"].startswith("cancelled") and out.getvalue() == ""
    assert rec.calls == []
    row = [json.loads(l) for l in (paths.hud_log_dir()).glob("*.jsonl").__next__().read_text().splitlines()][-1]
    assert row["kind"] == "vocal" and row["ok"] is False and row["gate"]


@pytest.mark.parametrize("slug", ["atlas", "nova"])
def test_vocal_send_after_validate(gate_asks, slug):
    rec = Recorder()
    out = io.StringIO()
    r = vocal.send(slug, "Salut", now=T, runner=rec.runner, inject=rec.inject, raw_settings={}, dry_run=True, out=out)
    assert r["ok"] and r["dry_run"] and json.loads(out.getvalue())["stdin"].endswith("Salut") and rec.calls == []
    r = vocal.send(slug, "Salut", now=T, runner=rec.runner, inject=rec.inject, raw_settings={})
    assert r["ok"] and len(rec.calls) == 1 and len(gate_asks.asked) == 2


def test_vocal_cli_passes_the_action_key(gate_asks, tmp_path, capsys):
    f = tmp_path / "t.txt"
    f.write_text("Salut Nova")
    gate_asks.answer = "cancelled"
    assert cli.main(["vocal-send", "--to", "nova", "--text-file", str(f), "--dry-run", "--action-key", "vocal"]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and json.loads(out[0])["error"] == "cancelled (esc)"     # no plan printed: nothing sent
    assert gate_asks.asked[-1]["action_key"] == "vocal"


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "Screenshot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


def test_shot_deliver_refuses_then_sends(gate_asks, png, tmp_path):
    chan = tmp_path / "chan"
    chan.mkdir()
    (chan / "inject").write_text("")
    table = {"atlas": {"name": "Atlas", "kind": "inject", "path": str(chan / "inject")},
             "nova": {"name": "Nova", "kind": "a2a", "script": "/x/agent_send.py", "to": "h", "state_dir": "/s"}}
    rec, copies = Recorder(), []
    kw = dict(ctx={"app": "Telegram"}, when=WHEN, table=table, runner=rec.runner, inject=rec.inject,
              copy=lambda s, d: copies.append(d))
    for answer in ("cancelled", "timeout", "hud_down"):
        gate_asks.answer = answer
        for slug in table:
            for dry in (False, True):
                out = io.StringIO()
                r = shots.deliver(slug, str(png), "regarde", dry_run=dry, out=out, **kw)
                assert not r["ok"] and r["error"].startswith("cancelled") and out.getvalue() == ""
    assert rec.calls == [] and copies == []
    assert gate_asks.asked[-1]["title"] == "Envoyer cette capture à Nova ?" and "regarde" in gate_asks.asked[-1]["preview"][0]
    gate_asks.answer = "validated"
    out = io.StringIO()
    assert shots.deliver("nova", str(png), "regarde", dry_run=True, out=out, **kw)["ok"] and out.getvalue()
    assert shots.deliver("atlas", str(png), "regarde", **kw)["ok"] and len(copies) == 1 and rec.calls[0][0] == "inject"


class FakePage:
    def __init__(self):
        self.presses = []

    def press(self, params, *, dry_run=False):
        self.presses.append(params["slug"])
        return {"ok": True, "dry_run": dry_run}


def _lv():
    lv = live.Live(pull=False, sender=lambda p, r: None, runner=lambda a: (0, ""), out=io.StringIO(),
                   resolve_app=lambda a: None, is_executable=lambda o: True)
    lv.page = FakePage()
    return lv


def test_outward_page_press_refuses_without_validate(gate_asks):
    lv = _lv()
    p = {"host": "github.com", "role": "button", "slug": "squash-and-merge", "short": "Squash & merge"}
    for answer in ("cancelled", "timeout", "hud_down"):
        gate_asks.answer = answer
        assert lv._ui_press(p) is False and lv.page.presses == []
        assert lv.last_press["reason"] == "gate_cancelled"
    req = gate_asks.asked[-1]
    assert req["cls"] == "page" and req["target"] == "button:squash-and-merge" and req["preview"] == ["github.com"]
    gate_asks.answer = "validated"
    assert lv._ui_press(p) is True and lv.page.presses == ["squash-and-merge"]
    # through the executor, as a HUD press / a wish runs it
    gate_asks.answer = "cancelled"
    opt = {"primitive": page_actions.PRIMITIVE, "params": dict(p, slug="approve"), "token": "click:x"}
    assert lv.executor.run(opt, {}, lv.url_for) is None and lv.page.presses == ["squash-and-merge"]


def test_a_non_outward_page_press_is_not_gated(gate_asks):
    lv = _lv()
    assert lv._ui_press({"host": "github.com", "role": "link", "slug": "files-changed"}) is True
    assert gate_asks.asked == [] and lv.page.presses == ["files-changed"]


def test_glide_ask_atlas_and_wish_sends_are_gated(gate_asks):
    lv = _lv()
    lv.exec_dry_run = True
    gate_asks.answer = "cancelled"
    assert not lv._send_vocal({"app": "Mail"})("atlas", "peux-tu regarder")["ok"]     # glide "Ask Atlas" / "Send ✓"
    eng = intent_resolve.WishEngine(dry_run=True, out=io.StringIO(), raw_settings=lambda: {})
    r = {"primitive": "vocal.send", "params": {"to": "orion", "text": "la PR"}, "label": "Send to Orion", "token": "vocal:orion"}
    ok, label = eng.execute(r, {"app": "Safari"})
    assert not ok and label.startswith("Not sent: cancelled")
    assert [a["cls"] for a in gate_asks.asked] == ["vocal", "vocal"]
    gate_asks.answer = "validated"
    assert eng.execute(r, {"app": "Safari"}) == (True, "Sent to Orion")


def test_opening_a_mail_draft_is_not_gated(gate_asks):
    lv = _lv()
    seen = []
    lv.executor.runner = lambda argv: (seen.append(argv), (0, ""))[1]
    assert lv.executor.run({"primitive": "mail.draft", "params": {"to": "orion@x.example", "subject": "Hi"}, "token": "t"},
                           {}, lv.url_for) == "t"
    assert seen[0][0] == "open" and seen[0][1].startswith("mailto:") and gate_asks.asked == []


def test_an_irreversible_press_asks_even_with_the_gate_off(gate_asks):
    """oi-hud presses an irreversible label only with the ticket the panel's validate issues: `force`."""
    lv = _lv()
    paths.ensure_private_root()
    (paths.data_dir() / "settings.json").write_text(json.dumps({"gate": {"outward": {"page": False}}}))
    assert lv._ui_press({"host": "github.com", "role": "button", "slug": "approve"}) is True and gate_asks.asked == []
    gate_asks.answer = "cancelled"
    assert lv._ui_press({"host": "github.com", "role": "button", "slug": "squash-and-merge"}) is False
    assert gate_asks.asked[-1]["cls"] == "page" and lv.page.presses == ["approve"]


class FakeExec:
    def __init__(self):
        self.ran = []

    def run(self, opt, ctx, url_for, message_id=""):
        self.ran.append(opt["primitive"])
        return opt.get("token")

    def select_mail(self, mid):
        return True


def _proactive_press(lv, opt, mode=None):
    row = {"ts": "2026-10-20T21:50:00.000Z", "form": "proactive", "options": [], "shown": False, "outcome": "x",
           "executed": None, "latency_ms": None}
    lv.on_reply({"event": "pressed", "key": 1, **({"mode": mode} if mode else {})}, row, {1: opt}, {"app": "Mail"}, 0.0)
    return json.loads((paths.hud_log_dir() / "2026-10-20.jsonl").read_text().splitlines()[-1])


def test_w24_outward_options_go_pick_gate_validate(gate_asks):
    """W24 marks send-capable proactive options `outward: true`: the pick asks the panel (class "reply") before the
    reply / forward / draft window opens; cancelled = nothing runs."""
    lv = _lv()
    lv.executor = FakeExec()
    reply = {"primitive": "mail.reply", "params": {"message_id": "<m1>"}, "token": "mail.reply", "label": "Reply to Lena",
             "evt": True, "outward": True}
    gate_asks.answer = "cancelled"
    row = _proactive_press(lv, reply)
    assert row["executed"] is None and lv.executor.ran == [] and row["gate"]["decision"] == "cancelled"
    req = gate_asks.asked[-1]
    assert req["cls"] == "reply" and req["title"] == "Reply to Lena ?"
    gate_asks.answer = "validated"
    row = _proactive_press(lv, reply)
    assert row["executed"] == "mail.reply" and lv.executor.ran == ["mail.reply"] and len(gate_asks.asked) == 2
    log = (paths.data_dir() / "gate-log").glob("*.jsonl")
    rows = [json.loads(x) for f in log for x in f.read_text().splitlines()]
    assert [r["cls"] for r in rows] == ["reply", "reply"] and "Lena" not in json.dumps(rows)


def test_w24_ask_agent_is_gated_at_its_send_not_at_the_pick(gate_asks):
    lv = _lv()
    lv.executor = FakeExec()
    ask = {"primitive": "agent.ask", "params": {"to": "orion"}, "token": "agent.ask:orion", "label": "Ask Orion",
           "evt": True, "outward": True}
    assert _proactive_press(lv, ask)["executed"] == "agent.ask:orion" and gate_asks.asked == []   # the panel opens
    plain = {"primitive": "mail.reply", "params": {}, "token": "mail.reply", "label": "Reply"}   # not W24: a draft
    assert _proactive_press(lv, plain)["executed"] == "mail.reply" and gate_asks.asked == []
