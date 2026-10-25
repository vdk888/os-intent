"""The user 2026-10-21 15:35Z (vocal): « Quand j'envoie un vocal comme ça ce serait bien que ça propose d'envoyer un
screenshot […] et aussi il faut augmenter les 60 secondes max parce que ça me coupe au milieu ».
Long vocals (the python side: a 5-minute transcript is kept whole, one write) and the screenshot offer in the
outward gate (offered only when safe, OFF unless chosen, never captured in a dry run, attached to the SAME line).
Nothing is ever sent or captured for real: fake runner / inject / capture / copy, the gate is tests/conftest.py's."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from oi import gate, live, paths, vocal

T = datetime(2026, 10, 21, 13, 35, 0, tzinfo=timezone.utc).timestamp()     # 15:35 in Paris
REAL_APPEND = vocal._append_inject


@pytest.fixture(autouse=True)
def no_real_send(monkeypatch):
    def tripwire(*a, **k):
        raise AssertionError("a real sender / capture was started")
    monkeypatch.setattr(vocal, "_run", tripwire)
    monkeypatch.setattr(vocal, "_append_inject", tripwire)
    monkeypatch.setattr(vocal, "capture_screen", tripwire)


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, data):
        self.calls.append((argv, data))
        return 0, ""


class Cap:
    """A fake screencapture: writes a tiny file, records (dst, display)."""
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, dst, display):
        self.calls.append((dst, display))
        if self.ok:
            Path(dst).write_bytes(b"\x89PNG fake")
        return self.ok


def _status(ctx, age=3):
    d = paths.status_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "oi-live.json").write_text(json.dumps({"ts": live.now_iso(T - age), "context": ctx}))


def _settings(obj):
    p = paths.data_dir()
    p.mkdir(parents=True, exist_ok=True)
    (p / "settings.json").write_text(json.dumps(obj))


def _attach(display=2):
    return lambda req: {"event": "outward", "id": req["id"], "decision": "validated", "reason": "return",
                        "attach_shot": True, "display": display}


def _send(slug, text="Regarde ce que je fais là", **kw):
    kw.setdefault("raw_settings", {})
    kw.setdefault("runner", Fake())
    kw.setdefault("inject", lambda p, l: (_ for _ in ()).throw(AssertionError("inject")))
    kw.setdefault("capture", Cap())
    return vocal.send(slug, text, now=T, offer_shot=True, **kw)


# ---- long vocals --------------------------------------------------------------------------------------------

def test_a_five_minute_transcript_is_sent_whole():
    text = " ".join(["mot"] * 1500)                       # ~6000 chars: a 5-minute dictation, over the old 4000
    f = Fake()
    assert vocal.send("nova", text, now=T, runner=f, raw_settings={})["ok"]
    line = f.calls[0][1].decode()
    assert text in line and line.count("\n") == 1
    assert len(vocal.clean("a" * 20000)) == vocal.MAX_CHARS == 16000


def test_inject_append_is_one_line_one_write(tmp_path):
    inj = tmp_path / "inject"
    inj.write_text("")
    long = "x" * 15000
    REAL_APPEND(str(inj), long)
    assert inj.read_text() == long + "\n"


# ---- the offer: only when a capture is safe now ---------------------------------------------------------------

def test_offered_in_a_normal_context(gate_asks):
    _status({"app": "Safari", "host": "github.com", "desk": 2})
    _send("nova")
    assert gate_asks.asked[-1]["offer_shot"] is True
    assert gate_asks.asked[-1]["cls"] == "vocal"


@pytest.mark.parametrize("ctx,raw,settings_file,app", [
    (None, {}, None, "Safari"),                                                   # no fresh oi live context
    ({"app": "Safari", "host": "client.example", "desk": 1}, {}, {"claude": {"local_only_domains": ["client.example"]}}, None),
    ({"app": "Safari", "host": "app.client.example", "desk": 1}, {}, {"claude": {"local_only_domains": ["client.example"]}}, None),
    ({"app": "Safari", "host": None, "desk": 1}, {}, None, None),                 # a private window has no host
    ({"app": "Google Chrome", "host": None, "desk": 1}, {}, None, None),
    ({"app": "Notes", "host": None, "desk": 1}, {}, None, "1Password"),          # oi-hud's front app denylisted
    ({"app": "Notes", "host": None, "desk": 1}, {}, {"denylist_apps": ["Signal"]}, "Signal"),
    ({"app": "Notes", "host": None, "desk": 1}, {"paused": True}, None, None),
    ({"app": "Notes", "host": None, "desk": 1}, {"shots": {"enabled": False}}, None, None),
    ({"app": "Notes", "host": None, "desk": 1}, {"vocal": {"shot_offer": False}}, None, None),
], ids=["no-context", "local-only", "local-only-sub", "safari-no-host", "chrome-no-host", "denylisted",
        "denylisted-setting", "paused", "shots-off", "setting-off"])
def test_not_offered_when_unsafe(gate_asks, ctx, raw, settings_file, app):
    if ctx is not None:
        _status(ctx)
    if settings_file:
        _settings(settings_file)
    gate_asks.answer = _attach()                       # even a forged attach answer captures nothing
    cap = Cap()
    r = _send("nova", raw_settings=raw, capture=cap, app=app)
    assert r["ok"]
    assert "offer_shot" not in gate_asks.asked[-1]
    assert cap.calls == []


def test_other_vocal_paths_never_offer(gate_asks):
    _status({"app": "Safari", "host": "github.com", "desk": 2})
    vocal.send("nova", "salut", now=T, runner=Fake(), raw_settings={})   # glide / wish / hand-over: offer_shot False
    assert "offer_shot" not in gate_asks.asked[-1]


# ---- the choice ---------------------------------------------------------------------------------------------------

def test_off_by_default_validate_alone_captures_nothing(gate_asks):
    _status({"app": "Safari", "host": "github.com", "desk": 2})
    cap, f = Cap(), Fake()
    r = _send("nova", capture=cap, runner=f)
    assert r["ok"] and "shot" not in r and cap.calls == []
    assert "screenshot" not in f.calls[0][1].decode()


def test_cancel_with_attach_captures_nothing(gate_asks):
    _status({"app": "Safari", "host": "github.com", "desk": 2})
    gate_asks.answer = lambda req: {"event": "outward", "id": req["id"], "decision": "cancelled", "reason": "esc",
                                    "attach_shot": True}
    cap = Cap()
    r = _send("nova", capture=cap)
    assert not r["ok"] and cap.calls == []


def test_chosen_a2a_recipient_gets_the_path_in_the_same_line(gate_asks, tmp_path):
    _status({"app": "Safari", "host": "github.com", "desk": 2})
    gate_asks.answer = _attach(display=2)
    audio = paths.data_dir() / "vocals" / "20261021T133500Z-nova.caf"
    cap, f = Cap(), Fake()
    r = _send("nova", capture=cap, runner=f, audio=str(audio))
    assert r["ok"] and r["shot"] is True
    (dst, display), = cap.calls
    assert display == 2 and Path(dst) == audio.with_suffix(".png")
    assert oct(Path(dst).parent.stat().st_mode & 0o777) == "0o700"
    (argv, data), = f.calls                                   # ONE delivery: the vocal + its screenshot
    line = data.decode()
    assert line.count("\n") == 1 and "Regarde ce que je fais là" in line
    assert line.rstrip("\n").endswith(f"(screenshot on the user's Mac at {dst})")
    assert not any("screenshot" in a for a in argv)


def test_chosen_inject_recipient_gets_the_image_in_its_inbox(gate_asks, tmp_path):
    _status({"app": "Safari", "host": "github.com", "desk": 2})
    gate_asks.answer = _attach(display=None)
    inj = tmp_path / "chan" / "inject"
    inj.parent.mkdir()
    inj.write_text("")
    raw = {"vocal_recipients": {"atlas": {"kind": "inject", "path": str(inj)}}}
    got, copies, cap = [], [], Cap()
    r = _send("atlas", raw_settings=raw, capture=cap, inject=lambda p, l: got.append(l),
              copy=lambda src, dst: copies.append((src, dst)), audio=str(tmp_path / "vocals" / "a-atlas.caf"))
    assert r["ok"] and r["shot"] is True
    assert cap.calls[0][1] is None                             # no display said: the main display
    (src, dst), = copies
    assert src == str(cap.calls[0][0]) and dst.parent == inj.parent / "inbox" and dst.name.startswith("oi-shot-")
    assert got[0].endswith(f"(audio: {tmp_path / 'vocals' / 'a-atlas.caf'}) (screenshot: {dst})")


def test_never_captured_in_a_dry_run(gate_asks, capsys):
    _status({"app": "Safari", "host": "github.com", "desk": 2})
    gate_asks.answer = _attach()
    cap = Cap()
    r = _send("nova", capture=cap, dry_run=True)
    assert r["ok"] and r["dry_run"] and cap.calls == [] and r["shot_note"] == "dry_run"
    plan = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert plan["screenshot"] == "would capture display 2" and "screenshot" not in plan["stdin"]


def test_context_turning_private_while_the_panel_is_up_sends_the_vocal_alone(gate_asks):
    _status({"app": "Safari", "host": "github.com", "desk": 2})

    def answer(req):
        _status({"app": "1Password", "host": None, "desk": 2})   # The user switched to a denylisted app meanwhile
        return _attach()(req)
    gate_asks.answer = answer
    cap, f = Cap(), Fake()
    r = _send("nova", capture=cap, runner=f, app=None)
    assert r["ok"] and cap.calls == [] and r["shot_note"] == "skipped"
    assert "screenshot" not in f.calls[0][1].decode()


def test_capture_failure_still_sends_the_vocal(gate_asks):
    _status({"app": "Safari", "host": "github.com", "desk": 2})
    gate_asks.answer = _attach()
    f = Fake()
    r = _send("nova", capture=Cap(ok=False), runner=f)
    assert r["ok"] and r["shot_note"] == "capture_failed" and "screenshot" not in f.calls[0][1].decode()


def test_hud_log_row_says_offered_and_attached(gate_asks):
    _status({"app": "Safari", "host": "github.com", "desk": 2})
    gate_asks.answer = _attach()
    _send("nova")
    day = datetime.fromtimestamp(T, timezone.utc).date().isoformat()
    row = json.loads((paths.hud_log_dir() / f"{day}.jsonl").read_text().splitlines()[-1])
    assert row["shot_offered"] is True and row["shot"] is True
    assert "Regarde" not in json.dumps(row)                     # never the text


# ---- the gate and the capture command ----------------------------------------------------------------------------

def test_gate_attach_only_when_offered_and_validated(gate_asks):
    gate_asks.answer = _attach(display=3)
    g = gate.outward("vocal", "nova", "Nova", "salut", raw={}, offer_shot=True)
    assert g["attach_shot"] is True and g["display"] == 3
    g = gate.outward("vocal", "nova", "Nova", "salut", raw={})                 # not offered: a forged answer is ignored
    assert "attach_shot" not in g
    gate_asks.answer = _attach(display=True)                                  # a bool is not a display
    assert "display" not in gate.outward("vocal", "nova", "Nova", "salut", raw={}, offer_shot=True)
    g = gate.outward("vocal", "nova", "Nova", "s", raw={"gate": {"outward": False}}, offer_shot=True)
    assert g["decision"] == "off" and "attach_shot" not in g                  # no panel = no choice = no capture


def test_capture_command(monkeypatch, tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        Path(argv[-1]).write_bytes(b"png")
        return None
    monkeypatch.setattr(vocal.subprocess, "run", run)
    monkeypatch.setattr(vocal, "SHOT_SETTLE_S", 0)
    cap = vocal.send.__kwdefaults__["capture"]           # the real capture_screen (the module name is a tripwire here)
    dst = tmp_path / "a.png"
    assert cap(dst, 2) and calls[-1] == ["screencapture", "-x", "-t", "png", "-D", "2", str(dst)]
    assert oct(dst.stat().st_mode & 0o777) == "0o600"
    assert cap(tmp_path / "b.png", None) and calls[-1][4] == "-m"
