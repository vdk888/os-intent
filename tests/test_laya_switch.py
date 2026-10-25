"""The menu-bar Laya switch: settings `laya.enabled`, oi.laya_switch, and who respects it."""
from __future__ import annotations

import io
import json
import signal
import subprocess

import pytest

from oi import health, intent_resolve, laya, laya_switch, live, paths, settings

NOW = 1_800_000_000.0


def _write(data_dir, raw: dict):
    paths.ensure_private_root()
    (data_dir / "settings.json").write_text(json.dumps(raw))


# ---- settings ------------------------------------------------------------------------------------------

def test_default_is_on_and_a_bad_value_falls_back_to_on(data_dir):
    assert settings.load()["laya"] == {"enabled": True}
    _write(data_dir, {"laya": {"enabled": "no"}})
    assert settings.load()["laya"]["enabled"] is True


def test_off_is_markov_only_and_on_restores_the_stored_live_keys(data_dir):
    _write(data_dir, {"live": {"laya_shadow": True, "laya_rank": "blend", "laya_timeout_ms": 1500}, "paused": True})
    on = settings.load()["live"]
    assert on["laya_shadow"] is True and on["laya_rank"] == "blend"
    assert settings.set_laya_enabled(False) == {"enabled": False}
    s = settings.load()
    assert s["laya"]["enabled"] is False and s["paused"] is True
    assert (s["live"]["laya_shadow"], s["live"]["laya_rank"], s["live"]["predictor"]) == (False, "shadow", "markov")
    raw = json.loads((data_dir / "settings.json").read_text())
    assert raw["live"] == {"laya_shadow": True, "laya_rank": "blend", "laya_timeout_ms": 1500}   # untouched
    assert (data_dir / "settings.json").stat().st_mode & 0o777 == 0o600
    settings.set_laya_enabled(True)
    back = settings.load()["live"]
    assert back["laya_shadow"] is True and back["laya_rank"] == "blend"


# ---- ensure_server / the wish resolver / oi live respect it -------------------------------------------

def test_ensure_server_never_starts_when_off(data_dir):
    settings.set_laya_enabled(False)
    boom = lambda *a, **k: pytest.fail("must not run")   # noqa: E731
    assert laya.ensure_server("http://127.0.0.1:9", preflight=boom, spawn=boom)[0] == "disabled"


def test_ensure_server_refusal_is_written_for_the_menu(data_dir):
    assert laya.ensure_server("http://127.0.0.1:9", preflight=lambda: (False, "swap used 5000 MB >= 4096 MB; x"))[0] \
        == "refused"
    st = laya_switch.read_status()
    assert st["state"] == "refused" and st["reason"].startswith("swap used 5000 MB")


def test_wish_resolver_never_asks_laya_when_off(data_dir):
    settings.set_laya_enabled(False)
    r = intent_resolve.WishEngine(laya_health=lambda url: pytest.fail("never probed"))
    with pytest.raises(laya.LayaError) as e:
        r._laya("open mail", {}, {})
    assert e.value.reason == "disabled"


def _live(calls):
    return live.Live(pull=False, sender=lambda payload, on_reply: None, runner=lambda argv: (0, ""), out=io.StringIO(),
                     resolve_app=lambda a: None, is_executable=lambda o: True, laya_client=lambda cfg: None,
                     ensure_laya=lambda url, ck: calls.append((url, ck)) or ("starting", "test"))


def test_live_starts_laya_at_its_start_only_when_on(data_dir):
    calls = []
    _live(calls).ensure_laya_at_start()
    assert calls == [("http://127.0.0.1:8781", None)]                 # on (default): back after a reboot
    settings.set_laya_enabled(False)
    calls.clear()
    _live(calls).ensure_laya_at_start()
    assert calls == []


# ---- status for the menu ----------------------------------------------------------------------------------

def test_classify():
    c = laya_switch.classify
    assert c(False, None, {"state": "running"}, NOW) == ("disabled", "")
    assert c(False, {"loaded": ["english"]}, {}, NOW)[0] == "disabled"
    assert c(True, {"status": "ok", "loaded": ["english"]}, {}, NOW) == ("running", "english")
    assert c(True, {}, {}, NOW) == ("running", "")                      # a server without `loaded`: up is up
    assert c(True, {"loaded": []}, {}, NOW)[0] == "loading"
    assert c(True, None, {"state": "refused", "reason": "swap used 13189 MB >= 4096 MB; top memory: x"}, NOW) == \
        ("refused", "swap used 13189 MB >= 4096 MB")
    assert c(True, None, {"state": "running", "ts": NOW - 5}, NOW)[0] == "starting"
    assert c(True, None, {"state": "running", "ts": NOW - 600}, NOW) == ("stopped", "running")
    assert c(True, None, {"state": "killed_cap"}, NOW) == ("stopped", "killed_cap")
    assert c(True, None, {}, NOW) == ("stopped", "")


def test_status_reads_settings_and_the_status_file(data_dir):
    laya._write_status({"state": "refused", "reason": "available 900 MB < 3072 MB", "ts": NOW})
    r = laya_switch.status(probe=lambda url: None, now=NOW)
    assert r == {"enabled": True, "state": "refused", "detail": "available 900 MB < 3072 MB", "up": False,
                 "url": "http://127.0.0.1:8781"}
    settings.set_laya_enabled(False)
    assert laya_switch.status(probe=lambda url: None, now=NOW)["state"] == "disabled"


# ---- stop / switch ------------------------------------------------------------------------------------------

def _run(rc=0, ps="python -m oi.cli laya-serve --port 8781"):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        out = ps if argv[0] == "/bin/ps" else ""
        return subprocess.CompletedProcess(argv, 0 if argv[0] == "/bin/ps" else rc, out, "")
    return run, calls


def test_stop_uses_launchctl_when_the_agent_is_loaded():
    run, calls = _run(0)
    out = laya_switch.stop(agent_loaded=lambda: True, run=run, kill=lambda *a: pytest.fail("no kill"), st={})
    assert out.startswith("launchctl kill SIGTERM com.osintent.oi-laya") and calls[0][:3] == ["/bin/launchctl", "kill", "SIGTERM"]


def test_stop_signals_a_spawned_supervisor_only_if_it_is_laya_serve():
    killed = []
    run, _ = _run()
    st = {"state": "running", "pid": 4242}
    assert laya_switch.stop(agent_loaded=lambda: False, run=run, kill=lambda p, s: killed.append((p, s)), st=st) \
        == "SIGTERM supervisor pid 4242"
    assert killed == [(4242, signal.SIGTERM)]
    run, _ = _run(ps="/Applications/Safari.app/Contents/MacOS/Safari")           # a reused pid: left alone
    killed.clear()
    assert laya_switch.stop(agent_loaded=lambda: False, run=run, kill=lambda p, s: killed.append(p), st=st) \
        == "nothing running"
    assert killed == []
    assert laya_switch.stop(agent_loaded=lambda: False, run=run, st={"state": "stopped", "pid": 4242}) == "nothing running"


def test_switch_persists_then_stops_or_ensures(data_dir):
    r = laya_switch.switch(False, stop_fn=lambda: "stopped it")
    assert r == {"enabled": False, "result": "stopped", "detail": "stopped it"}
    assert settings.load()["laya"]["enabled"] is False
    seen = []
    r = laya_switch.switch(True, ensure=lambda url, ck: seen.append((url, ck)) or ("refused", "swap"))
    assert r == {"enabled": True, "result": "refused", "detail": "swap"} and seen == [("http://127.0.0.1:8781", None)]
    assert settings.load()["laya"]["enabled"] is True


def test_cli_status_json(data_dir, capsys):
    from oi import cli
    settings.set_laya_enabled(False)
    assert cli.main(["laya-switch", "status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enabled"] is False and out["state"] == "disabled"


# ---- oi health ----------------------------------------------------------------------------------------------

def test_health_off_is_info_not_a_failure(data_dir):
    assert health.check_info(None, [], laya_on=False) == ["laya: Laya désactivé"]
    assert health.check_info(None, []) == []                            # on (default): nothing
    settings.set_laya_enabled(False)
    listing = {f"com.osintent.{s}": ("123", "0") for s in health.SERVICES}
    listing["com.osintent.oi-laya"] = ("-", "0")                          # stopped on purpose
    r = health.run_checks(NOW, listing=lambda: listing, compile_=lambda lang, src: (True, ""), enabled=lambda: [])
    assert not any(p.startswith("service oi-laya") for p in r["problems"])
    assert "laya: Laya désactivé" in r["info"]
