"""Production health: the engine's sensors.json and `oi health`. Temp OI_DATA_DIR (conftest); launchctl,
osacompile and settings are fakes. Nothing real is read or run."""
import io
import json
import os
from contextlib import redirect_stdout
from datetime import datetime, timezone

import pytest

from oi import health
from oi.proactive import engine, sensors

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc).timestamp()
ISO = lambda t: health._iso(t)  # noqa: E731
GOOD = {f"com.osintent.{s}": ("123", "0") for s in health.SERVICES}


def test_sensor_health_counts_flushes_atomically_once_a_minute():
    h = health.SensorHealth()
    h.record("mail", NOW - 25 * 3600, "osa_error")                     # older than 24 h: not counted
    h.record("mail", NOW - 10, None)
    h.record("mail", NOW - 5, "osa_timeout")
    h.record("mail", NOW, "osa_timeout")
    assert h.flush(NOW, ["mail", "files"]) and not h.flush(NOW + 30, ["mail"])
    p = health.health_path()
    d = json.loads(p.read_text())
    assert d["sources"]["mail"] == {"last_ok_ts": ISO(NOW - 10), "last_error_ts": ISO(NOW),
                                    "last_error_kind": "osa_timeout", "consecutive_failures": 2,
                                    "polls_ok_24h": 1, "polls_failed_24h": 2, "info_kind": None}
    assert d["sources"]["files"]["polls_ok_24h"] == 0 and d["paused"] is False
    assert [f for f in os.listdir(p.parent)] == ["sensors.json"]           # no temp file left
    assert h.flush(NOW + 60, ["mail"])


class FailingMail:
    source = "mail"

    def __init__(self):
        self.last_error = None

    def poll(self, now, cfg):
        self.last_error = "osa_error"
        return []


class Boom:
    source = "files"

    def poll(self, now, cfg):
        raise ValueError("x")


def test_engine_records_sensor_failures_and_exceptions():
    raw = {"proactive": {"sources": {"mail": True, "files": True}}}
    eng = engine.Engine(None, sensors={"mail": FailingMail(), "files": Boom()}, raw_settings=lambda: raw,
                        clock=lambda: NOW, focus=lambda: None, profile=lambda: {}, hud_cmd=lambda p: {})
    for i in range(4):
        eng.poll_once(NOW + i * 10)
    eng.health.flush(NOW + 100, ["mail", "files"], force=True)
    d = json.loads(health.health_path().read_text())["sources"]
    assert d["mail"]["consecutive_failures"] == 4 and d["mail"]["last_error_kind"] == "osa_error"
    assert d["files"]["last_error_kind"] == "exception:ValueError" and d["files"]["polls_ok_24h"] == 0


def test_mail_failure_kinds_keep_closed_apart_from_broken():
    res, running = [(1, "")], [True]
    s = sensors.MailSensor(osa=lambda script, args, timeout=6: res[-1], claude_cfg=lambda: {}, log=lambda m: None,
                           running=lambda: running[-1])
    s.poll(NOW, {})
    assert s.last_error == "osa_error"
    res.append((-1, ""))
    s.poll(NOW + 6, {})
    assert s.last_error == "osa_timeout"
    running.append(False)
    s.poll(NOW + 7 + sensors.MAIL_BACKOFF_S, {})                          # after the timeout's back-off
    assert s.last_error == "not_running"           # Mail closed: state unknown, health INFO (not a failure)
    res.append((0, "NOTRUNNING"))
    s.poll(NOW + 13 + sensors.MAIL_BACKOFF_S, {})
    assert s.last_error == "not_running"                                   # no back-off for a closed Mail
    assert s.poll(NOW + 14 + sensors.MAIL_BACKOFF_S, {}) == [] and not s.skipped


def write_health(sources, updated=NOW):
    p = health.health_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"updated_ts": ISO(updated), "paused": False, "sources": sources}))


OK_SRC = {"last_ok_ts": ISO(NOW), "last_error_ts": None, "last_error_kind": None, "consecutive_failures": 0,
          "polls_ok_24h": 10, "polls_failed_24h": 0}


def checks(**kw):
    kw.setdefault("listing", lambda: GOOD)
    kw.setdefault("compile_", lambda lang, src: (True, ""))
    kw.setdefault("enabled", lambda: ["mail"])
    return health.run_checks(NOW, **kw)


def test_healthy():
    write_health({"mail": OK_SRC})
    r = checks()
    assert r["healthy"] and r["problems"] == [] and r["scripts_checked"] > 10


def test_problems_one_line_each_no_private_content():
    write_health({"mail": dict(OK_SRC, consecutive_failures=5, last_error_kind="osa_error")})
    logs = health.paths.data_dir() / "logs"
    logs.mkdir(parents=True)
    old, new = ISO(NOW - 2 * 86400), ISO(NOW - 60)
    (logs / "oi-live.log").write_text(f"{old} ERROR old\n{new} oi-live: ok\n{new} ERROR Subject: secret deal\n"
                                      "Traceback (most recent call last):\n  File x\n")
    listing = dict(GOOD)
    listing["com.osintent.oi-hud"] = ("-", "-15")
    del listing["com.osintent.oi-laya"]
    r = checks(listing=lambda: listing, compile_=lambda lang, src: (lang != "JavaScript", ""))
    assert not r["healthy"]
    ps = r["problems"]
    assert "sensor mail: 5 failed polls in a row (osa_error)" in ps
    assert "service oi-hud: not running (last exit -15)" in ps and "service oi-laya: not loaded" in ps
    assert "log oi-live.log: 2 ERROR/Traceback lines in 24 h" in ps
    assert any(p.startswith("script oi.proactive.sensors.TELEGRAM_UNREAD_JS") for p in ps)
    assert not any("secret" in p for p in ps)


def test_stale_or_missing_health_file():
    assert checks()["problems"] == ["sensors: no health file (proactive engine not running?)"]
    assert checks(enabled=lambda: [])["healthy"]                          # no source on: nothing to watch
    write_health({"mail": OK_SRC}, updated=NOW - 900)
    assert checks()["problems"] == ["sensors: health file not updated for 15 min"]


@pytest.mark.parametrize("healthy,code", [(True, 0), (False, 1)])
def test_cli_exit_codes(monkeypatch, healthy, code):
    monkeypatch.setattr(health.sys, "platform", "darwin")
    monkeypatch.setattr(health.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(health, "run_checks", lambda: {"healthy": healthy, "problems": [] if healthy else ["x bad"],
                                                       "enabled_sources": [], "scripts_checked": 1})
    from oi import cli
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main(["health"]) == code
    assert ("x bad" in buf.getvalue()) != healthy
    with redirect_stdout(io.StringIO()) as b:
        assert cli.main(["health", "--json"]) == code
    assert json.loads(b.getvalue())["healthy"] is healthy


def test_cli_cannot_check(monkeypatch):
    monkeypatch.setattr(health.sys, "platform", "linux")
    from oi import cli
    with redirect_stdout(io.StringIO()):
        assert cli.main(["health"]) == 2
    monkeypatch.setattr(health.sys, "platform", "darwin")
    monkeypatch.setattr(health.shutil, "which", lambda n: "/usr/bin/" + n)

    def broken():
        raise OSError("launchctl list failed")
    monkeypatch.setattr(health, "run_checks", broken)
    with redirect_stdout(io.StringIO()):
        assert cli.main(["health"]) == 2


def test_never_succeeded_after_grace_is_unhealthy():
    never = dict(OK_SRC, last_ok_ts=None, polls_ok_24h=0)          # e.g. polled, Mail read "unknown" -> no ok yet
    p = health.health_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"updated_ts": ISO(NOW), "started_ts": ISO(NOW - 120), "paused": False,
                             "sources": {"mail": never}}))
    assert checks()["healthy"]                                      # 2 min after start: still in the grace period
    p.write_text(json.dumps({"updated_ts": ISO(NOW), "started_ts": ISO(NOW - 900), "paused": False,
                             "sources": {"mail": dict(never, last_error_kind="osa_timeout")}}))
    assert checks()["problems"] == ["sensor mail: no successful poll since the engine started 15 min ago (osa_timeout)"]
    write_health({"mail": never})                                   # an older file without started_ts: no grace rule
    assert checks()["healthy"]


def test_snapshot_carries_the_engine_start():
    h = health.SensorHealth()
    h.flush(NOW, ["mail"])
    h.record("mail", NOW + 5, None)
    assert json.loads(health.health_path().read_text())["started_ts"] == ISO(NOW)
    assert h.snapshot(NOW + 10, ["mail"])["started_ts"] == ISO(NOW)


def test_flush_removes_the_temp_file_on_any_failure(monkeypatch):
    h = health.SensorHealth()
    monkeypatch.setattr(h, "snapshot", lambda *a, **k: {"bad": object()})   # json.dumps -> TypeError
    assert h.flush(NOW, ["mail"]) is False                          # never raises into the engine
    d = health.health_path().parent
    assert [f for f in os.listdir(d) if f.startswith(".sensors.json.")] == []


def test_mail_lookup_failure_counts_like_a_scan_failure():
    mid = "<live@x>"
    row = sensors.SEP.join([mid, "Paul", "paul@x.example", "Hi", "2026-10-21", "0", "0"])
    scan = (0, "OK" + sensors.RS + row + sensors.RS)
    calls = {"state": (0, "OK" + sensors.RS)}

    def osa(script, args, timeout=6):
        return calls["state"] if script == sensors.MAIL_STATE else scan
    s = sensors.MailSensor(osa=osa, claude_cfg=lambda: {}, log=lambda m: None, running=lambda: True)
    s.poll(NOW, {})                                                  # primes
    scan = (0, "OK" + sensors.RS)                                    # the live mail scrolled out of the scan
    s.states.live[mid] = {"id": "x"}
    s.poll(NOW + 6, {})
    assert s.last_error is None                                      # lookup read OK
    calls["state"] = (-1, "")
    s.poll(NOW + 12, {})
    assert s.last_error == "osa_timeout"                             # the lookup failed: counted like a scan


def test_mail_backs_off_after_a_timeout_and_records_no_health_while_skipped():
    """2026-10-21: the per-message scan took 33-37 s > MAIL_TIMEOUT_S, every poll_s. A timeout now backs off
    (MAIL_BACKOFF_S, doubled per timeout in a row, capped); a skipped poll does not read Mail and is no health row."""
    calls, res = [], [(-1, "")]

    def osa(script, args, timeout=6):
        calls.append(script)
        return res[-1]
    s = sensors.MailSensor(osa=osa, claude_cfg=lambda: {}, log=lambda m: None, running=lambda: True)
    s.poll(NOW, {})
    assert s.last_error == "osa_timeout" and len(calls) == 1 and not s.skipped
    assert s.poll(NOW + 6, {}) == [] and s.skipped and len(calls) == 1                   # backing off: no read
    s.poll(NOW + sensors.MAIL_BACKOFF_S, {})
    assert len(calls) == 2 and s.backoff_s == 2 * sensors.MAIL_BACKOFF_S                  # timed out again: doubled
    for k in range(10):
        s.poll(NOW + 10_000 * (k + 1), {})
    assert s.backoff_s == sensors.MAIL_BACKOFF_MAX_S                                     # capped
    res.append((0, "OK" + sensors.RS))
    s.poll(NOW + 200_000, {})
    assert s.last_error is None and s.backoff_s == 0 and not s.skipped                  # Mail answered: reset
    s.poll(NOW + 200_006, {})
    assert not s.skipped and calls[-1] == sensors.MAIL_NEW
    res.append((1, ""))
    s.poll(NOW + 200_012, {})
    assert s.backoff_s == 0 and not s.skipped                                            # a non-timeout error: no back-off


def test_engine_records_no_health_for_a_skipped_poll():
    class Skip:
        last_error, skipped = "osa_timeout", True

        def poll(self, now, cfg):
            return []
    raw = {"proactive": {"sources": {"mail": True}}}
    eng = engine.Engine(None, sensors={"mail": Skip()}, raw_settings=lambda: raw,
                        clock=lambda: NOW, focus=lambda: None, profile=lambda: {}, hud_cmd=lambda p: {})
    rec = []
    eng.health.record = lambda src, now, err: rec.append(err)
    eng.poll_once(NOW)
    assert rec == []
    Skip.skipped = False
    eng.poll_once(NOW + 3600)
    assert rec == ["osa_timeout"]


def test_app_closed_is_info_never_a_failure():
    """Design decision: Mail / Telegram closed is the user's choice: `info_kind`, never counted toward
    the 3-failures rule, never resetting last_ok; only an unreadable read while the app runs is a failure."""
    h = health.SensorHealth(path=lambda: None)
    h.record("mail", NOW, None)
    h.record("mail", NOW + 6, "osa_error")
    for i in range(5):
        h.record("mail", NOW + 12 + i * 6, "not_running")
    snap = h.snapshot(NOW + 60, ["mail"])["sources"]["mail"]
    assert snap["info_kind"] == "not_running" and snap["consecutive_failures"] == 1
    assert snap["last_ok_ts"] == ISO(NOW) and snap["polls_failed_24h"] == 1 and snap["polls_ok_24h"] == 1
    write_health({"mail": snap, "agents": dict(OK_SRC, last_ok_ts=None, polls_ok_24h=0, info_kind="not_running")})
    r = checks(enabled=lambda: ["mail", "agents"])
    assert r["healthy"] and r["info"] == ["mail: Mail fermé", "agents: Telegram fermé"]
    h.record("mail", NOW + 70, "osa_error")                   # Mail open again, unreadable: a failure again
    h.record("mail", NOW + 76, "osa_error")
    snap = h.snapshot(NOW + 80, ["mail"])["sources"]["mail"]
    assert snap["info_kind"] is None and snap["consecutive_failures"] == 3
    write_health({"mail": snap})
    r = checks()
    assert not r["healthy"] and r["info"] == [] and "3 failed polls in a row (osa_error)" in r["problems"][0]


def test_cli_prints_closed_apps_as_info(monkeypatch):
    monkeypatch.setattr(health.sys, "platform", "darwin")
    monkeypatch.setattr(health.shutil, "which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr(health, "run_checks", lambda: {"healthy": True, "problems": [], "info": ["mail: Mail fermé"],
                                                       "enabled_sources": ["mail"], "scripts_checked": 3})
    out = io.StringIO()
    with redirect_stdout(out):
        code = health.cmd_health(type("A", (), {"json": False})())
    assert code == 0 and "info: mail: Mail fermé" in out.getvalue()


# ---- Laya layer (2026-10-21: 31 % timeouts, `oi health` silent) ------------------------------------------------

LAYA_ON = {"laya_shadow": True, "laya_rank": "shadow", "laya_timeout_ms": 3000}
UP = {"status": "ok", "loaded": ["english"], "device": "mps"}


def _rows(ok: int, timeout: int = 0, other: str | None = None, n_other: int = 0) -> list[dict]:
    return ([{"probs": {"a": 1.0}, "latency_ms": 500 + i} for i in range(ok)] + [{"error": "timeout"}] * timeout
            + [{"error": other}] * n_other)


def test_laya_off_is_not_checked():
    assert health.check_laya({"laya_shadow": False, "laya_rank": "shadow"}, None, None, _rows(0, 50), NOW) == \
        ([], [], {"enabled": False})


def test_laya_healthy_and_timeout_rate_over_ten_percent():
    p, i, s = health.check_laya(LAYA_ON, UP, {"state": "running"}, _rows(46, 4), NOW)
    assert p == [] and i == [] and s["timeouts"] == 4 and s["p50_ms"] == 523
    p, _, _ = health.check_laya(LAYA_ON, UP, {"state": "running"}, _rows(13, 37)[:50], NOW)
    assert p == ["laya: 37/50 timeouts in the last 50 shows (74 %)"]
    assert health.check_laya(LAYA_ON, UP, None, _rows(2, 7), NOW)[0] == []          # < LAYA_MIN_N: no verdict


def test_laya_down_no_model_and_other_errors():
    p, _, _ = health.check_laya(LAYA_ON, None, {"state": "stopped"}, _rows(20), NOW)
    assert p == ["laya: server down (last state stopped)"]
    p, _, _ = health.check_laya(LAYA_ON, {"status": "ok", "loaded": []}, None, _rows(10, 0, "unreachable", 10), NOW)
    assert p == ["laya: server up, no model loaded", "laya: 10/20 errors in the last 20 shows (unreachable 10)"]


def test_laya_recent_memory_refusal_is_info_old_one_is_a_problem():
    st = {"state": "refused", "ts": NOW - 300, "reason": "swap used 13189 MB >= 4096 MB; top memory: Python 1 641 MB"}
    p, i, _ = health.check_laya(LAYA_ON, None, st, _rows(10, 0, "unreachable", 10), NOW)
    assert p == [] and i == ["laya: refused by the memory rule (swap used 13189 MB >= 4096 MB)"]
    old = dict(st, ts=NOW - 2 * health.LAYA_REFUSED_INFO_S)
    p, i, _ = health.check_laya(LAYA_ON, None, old, _rows(10, 0, "unreachable", 10), NOW)
    assert i == [] and p == ["laya: server down (last state refused)",
                             "laya: 10/20 errors in the last 20 shows (unreachable 10)"]


def test_laya_rows_last_n_of_24h_and_run_checks_wiring(monkeypatch):
    d = health.paths.hud_log_dir()
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"ts": ISO(NOW - 2 * 86400), "laya": {"error": "timeout"}}, {"ts": ISO(NOW - 60), "outcome": "x"}]
    rows += [{"ts": ISO(NOW - 50 + k), "laya": {"error": "timeout"} if k % 2 else {"probs": {}, "latency_ms": 9}}
             for k in range(20)]
    (d / "2026-10-21.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    got = health.laya_rows(d, NOW)
    assert len(got) == 20 and sum(1 for r in got if r.get("error")) == 10
    assert len(health.laya_rows(d, NOW, n=5)) == 5
    monkeypatch.setattr(health.settings, "load", lambda: {"live": dict(LAYA_ON, laya_url="http://127.0.0.1:1")})
    write_health({"mail": OK_SRC})
    r = checks(laya_probe=lambda url: UP)
    assert r["problems"] == ["laya: 10/20 timeouts in the last 20 shows (50 %)"] and r["laya"]["up"]


def test_lazy_laya_agent_not_running_is_not_a_service_problem():
    listing = dict(GOOD)
    listing["com.osintent.oi-laya"] = ("-", "0")
    assert health.check_services(listing) == []
    del listing["com.osintent.oi-laya"]
    assert health.check_services(listing) == ["service oi-laya: not loaded"]
