"""W12: `oi vocal-send` — recipients, command construction, payload, empty guard, dry run, hud-log
row and the `vocal:<slug>` token. NOTHING is ever sent: every test uses a fake runner / fake inject, and
the real `_run` is replaced by a tripwire."""
import os
import json
from datetime import datetime, timezone

import pytest

from oi import cli, live, normalise, paths, vocab, vocal
from oi.moments import FEEDBACK_PREFIXES

T = datetime(2026, 10, 20, 14, 52, 7, tzinfo=timezone.utc).timestamp()     # 16:52 in Paris (CEST)
RELAY = os.path.expanduser("~/.local/bin/agent-send")


@pytest.fixture(autouse=True)
def no_real_send(monkeypatch):
    def tripwire(*a, **k):
        raise AssertionError("a real sender was started")
    monkeypatch.setattr(vocal, "_run", tripwire)
    monkeypatch.setattr(vocal, "_append_inject", tripwire)


class Fake:
    def __init__(self, code=0, err=""):
        self.calls, self.code, self.err = [], code, err

    def __call__(self, argv, data):
        self.calls.append((argv, data))
        return self.code, self.err


def _send(slug, text="Bonjour Nova, peux-tu regarder la PR", runner=None, inject=None, **kw):
    kw.setdefault("raw_settings", {})
    return vocal.send(slug, text, now=T, runner=runner or Fake(),
                      inject=inject or (lambda p, l: (_ for _ in ()).throw(AssertionError("inject"))), **kw)


@pytest.mark.parametrize("slug,argv", [
    ("nova", [RELAY, "--to", "agent-host", "--state-dir", "/opt/agents/nova/inbox", "--from", "atlas"]),
    ("orion", [RELAY, "--to", "agent-host", "--state-dir", "/opt/agents/orion/inbox", "--from", "atlas"]),
    ("lyra", [RELAY, "--to", "agent-host", "--state-dir", "/opt/agents/lyra/inbox", "--from", "atlas"]),
    ("iris", [RELAY, "--to", "agent-host", "--state-dir", "/opt/agents/iris/inbox", "--from", "atlas"]),
    ("vega", [RELAY, "--to", "studio-host", "--state-dir", "/opt/agents/vega/inbox", "--from", "atlas"]),
    ("hera", [RELAY, "--to", "laptop-host", "--state-dir", "/opt/agents/hera/inbox", "--from", "atlas"]),
    ("echo", [RELAY, "--to", "laptop-host", "--state-dir", "/opt/agents/echo/inbox", "--from", "atlas"]),
])
def test_a2a_command_per_recipient_text_on_stdin_only(slug, argv):
    f = Fake()
    r = _send(slug, runner=f, app="Safari", desk=2)
    assert r["ok"] and r["to"] == slug
    (got, data), = f.calls
    assert got == argv
    assert not any("Bonjour" in a for a in got)                    # never in argv
    line = data.decode()
    assert line.endswith("\n") and line.count("\n") == 1          # one line = one turn
    assert "Bonjour Nova, peux-tu regarder la PR" in line


@pytest.mark.parametrize("slug,path", [("atlas", os.path.expanduser("~/.claude/channels/atlas/inject")),
                                       ("juno", os.path.expanduser("~/.claude/channels/juno/inject"))])
def test_local_recipients_use_the_inject_file(slug, path):
    got = []
    r = _send(slug, inject=lambda p, l: got.append((p, l)))
    assert r["ok"] and got[0][0] == path and "\n" not in got[0][1]
    assert vocal.command(vocal.DEFAULT_RECIPIENTS[slug]) is None


def test_every_picker_agent_has_a_recipient():
    assert set(vocal.DEFAULT_RECIPIENTS) == {"atlas", "nova", "orion", "lyra", "vega", "juno", "iris",
                                             "hera", "echo"}


def test_payload_format_paris_time_and_context():
    when = datetime.fromtimestamp(T, timezone.utc)
    assert vocal.payload("salut", app="Safari", host="notion.so", desk=3, when=when) == \
        "[Vocal from the user, 16:52, context: Safari · notion.so · desk 3] salut"
    assert vocal.payload("salut", app=None, host=None, desk=None, when=when) == "[Vocal from the user, 16:52] salut"
    assert vocal.payload("a\nb", app="Mail", host=None, desk=1, when=when,
                         audio="/x/vocals/20261020T145207Z-nova.caf") == \
        "[Vocal from the user, 16:52, context: Mail · desk 1] a b (audio: /x/vocals/20261020T145207Z-nova.caf)"


def test_host_and_desk_from_the_fresh_live_status():
    d = paths.status_dir()
    d.mkdir(parents=True)
    st = {"ts": live.now_iso(T - 3), "context": {"app": "Safari", "host": "github.com", "desk": 4}}
    (d / "oi-live.json").write_text(json.dumps(st))
    f = Fake()
    _send("nova", runner=f, app="Google Chrome")                     # oi-hud's app wins, desk from status
    assert f.calls[0][1].decode().startswith("[Vocal from the user, 16:52, context: Google Chrome · github.com · desk 4] ")
    st["ts"] = live.now_iso(T - 60)                                   # stale status: no host
    (d / "oi-live.json").write_text(json.dumps(st))
    f = Fake()
    _send("nova", runner=f, app="Mail", desk=1)
    assert f.calls[0][1].decode().startswith("[Vocal from the user, 16:52, context: Mail · desk 1] ")


def test_empty_transcript_sends_nothing():
    f = Fake()
    for text in ("", "   \n ", None):
        r = _send("nova", text=text, runner=f)
        assert not r["ok"] and r["error"] == "empty transcript"
    assert f.calls == []


def test_failures_are_reported():
    assert _send("bob")["error"] == "unknown recipient bob"
    r = _send("nova", runner=Fake(255, "ssh: connect to host agent-host: timeout"))
    assert not r["ok"] and r["error"] == "exit 255: ssh: connect to host agent-host: timeout"


def _untouched():
    """A fresh copy of oi.vocal (the module itself keeps its tripwires), for the two real-helper tests."""
    ns: dict = {"__name__": "oi.vocal_copy", "__package__": "oi"}
    exec(compile(vocal.__loader__.get_source("oi.vocal"), "vocal_copy", "exec"), ns)
    return ns


def test_inject_file_must_exist(tmp_path):
    ns = _untouched()
    target = tmp_path / "inject"
    with pytest.raises(ns["VocalError"]):
        ns["_append_inject"](str(target), "line")                    # missing: never created
    assert not target.exists()
    target.write_text("")
    ns["_append_inject"](str(target), "one")
    ns["_append_inject"](str(target), "two")
    assert target.read_text() == "one\ntwo\n"


def test_sender_env_is_scrubbed(monkeypatch):
    ns = _untouched()
    seen = {}

    class P:
        returncode, stderr = 0, b""

    def fake_run(argv, input, capture_output, timeout, env):
        seen.update(argv=argv, input=input, env=env)
        return P()
    monkeypatch.setattr(ns["subprocess"], "run", fake_run)
    for k in vocal.SCRUB_ENV:
        monkeypatch.setenv(k, "secret")
    monkeypatch.setenv("KEEP_ME", "1")
    assert ns["_run"](["/bin/true"], b"x\n") == (0, "")
    assert not set(vocal.SCRUB_ENV) & set(seen["env"]) and seen["env"]["KEEP_ME"] == "1"
    assert seen["input"] == b"x\n"


def test_settings_override_recipients():
    t = vocal.recipients({"vocal_recipients": {
        "nova": {"kind": "inject", "path": "/tmp/nova-inject"},
        "echo": None,
        "orion": {"kind": "a2a", "script": "relative.py", "to": "x", "state_dir": "/s"},     # invalid: ignored
        "nina": {"name": "Nina", "kind": "a2a", "script": "/opt/send.sh", "to": "m1", "state_dir": "/n"},
        "Bad Slug": {"kind": "inject", "path": "/x"},
    }})
    assert t["nova"] == {"name": "Nova", "kind": "inject", "path": "/tmp/nova-inject"}
    assert "echo" not in t and t["orion"] == vocal.DEFAULT_RECIPIENTS["orion"]
    assert vocal.command(t["nina"]) == ["/opt/send.sh", "--to", "m1", "--state-dir", "/n", "--from", "atlas"]
    assert "Bad Slug" not in t


def test_hud_log_row_and_vocal_token():
    _send("nova", runner=Fake())
    _send("orion", runner=Fake(1, "boom"))
    _send("lyra", runner=Fake(), dry_run=True, out=open("/dev/null", "w"))
    rows = normalise.read_hud_log("2026-10-20")
    assert [(r["kind"], r["to"], r["ok"], r["dry_run"]) for r in rows] == \
        [("vocal", "nova", True, False), ("vocal", "orion", False, False), ("vocal", "lyra", True, True)]
    assert all("Bonjour" not in json.dumps(r) for r in rows)        # never the text
    assert rows[0]["text_len"] == len("Bonjour Nova, peux-tu regarder la PR")
    assert oct((paths.hud_log_dir() / "2026-10-20.jsonl").stat().st_mode & 0o777) == "0o600"
    toks = normalise.hud_tokens(rows)
    assert [t["token"] for t in toks] == ["vocal:nova"]              # failed and dry runs give nothing
    assert toks[0]["src"] == "oi-hud" and not vocab.validate_row(toks[0])
    assert vocab.parse("vocal:hera") == ("vocal", {"slug": "hera"})
    assert not vocab.validate("vocal:") and "vocal:" in FEEDBACK_PREFIXES


def test_dry_run_cli_prints_the_exact_command(tmp_path, capsys):
    f = tmp_path / "t.txt"
    f.write_text("Salut Nova\n", encoding="utf-8")
    rc = cli.main(["vocal-send", "--to", "nova", "--text-file", str(f), "--dry-run", "--app", "Mail", "--desk", "2"])
    assert rc == 0
    plan, result = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert plan["argv"] == [RELAY, "--to", "agent-host", "--state-dir", "/opt/agents/nova/inbox", "--from", "atlas"]
    assert plan["env_unset"] == ["TELEGRAM_STATE_DIR", "TELEGRAM_BOT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"]
    assert plan["stdin"].endswith("context: Mail · desk 2] Salut Nova")
    assert result == {"ok": True, "to": "nova", "name": "Nova", "dry_run": True}
    rc = cli.main(["vocal-send", "--to", "atlas", "--text-file", str(f), "--dry-run"])
    plan = json.loads(capsys.readouterr().out.splitlines()[0])
    assert rc == 0 and plan["append_to"] == os.path.expanduser("~/.claude/channels/atlas/inject")
    empty = tmp_path / "e.txt"
    empty.write_text(" \n")
    assert cli.main(["vocal-send", "--to", "nova", "--text-file", str(empty), "--dry-run"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "empty transcript"


def test_audio_suffix_only_for_local_inject_recipients():
    """W12 review follow-up (folded in W14): a remote A2A agent cannot open a path on this Mac."""
    f = Fake()
    assert _send("nova", runner=f, audio="/x/vocals/a-nova.caf")["ok"]
    assert "(audio:" not in f.calls[0][1].decode()
    got = []
    assert _send("atlas", inject=lambda p, l: got.append(l), audio="/x/vocals/a-atlas.caf")["ok"]
    assert got[0].endswith("(audio: /x/vocals/a-atlas.caf)")


# The user 2026-10-21 16:32Z: typing in the vocal panel stops the listening; oi-hud then adds `--typed`.
def test_typed_payload_says_it_was_typed():
    when = datetime.fromtimestamp(T, timezone.utc)
    assert vocal.payload("salut", app="Mail", host=None, desk=1, when=when, typed=True) == \
        "[Message typed by the user, 16:52, context: Mail · desk 1] salut"
    assert vocal.payload("salut", app=None, host=None, desk=None, when=when,
                         audio="/x/a.caf", typed=True) == "[Message typed by the user, 16:52] salut (audio: /x/a.caf)"


def test_typed_send_line_and_log_row():
    f = Fake()
    r = _send("nova", runner=f, typed=True)
    assert r["ok"] and f.calls[0][1].decode().startswith("[Message typed by the user, 16:52")
    rows = [json.loads(l) for l in (paths.hud_log_dir() / "2026-10-20.jsonl").read_text().splitlines()]
    assert rows[-1]["kind"] == "vocal" and rows[-1]["typed"] is True and "Bonjour" not in json.dumps(rows[-1])
    f = Fake()
    _send("nova", runner=f)
    assert f.calls[0][1].decode().startswith("[Vocal from the user, 16:52")


def test_cli_typed_flag():
    a = cli.build_parser().parse_args(["vocal-send", "--to", "nova", "--text-file", "/x", "--typed"])
    assert a.typed is True
    b = cli.build_parser().parse_args(["vocal-send", "--to", "nova", "--text-file", "/x"])
    assert b.typed is False
