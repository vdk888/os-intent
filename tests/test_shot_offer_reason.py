"""A vocal from a dashboard tab (Safari, back on the same tab after a HUD press to another app) got no
« Joindre une capture de l'écran » toggle, and why was not logged. oi-watcher logs a Safari url only when it
changes and `activate:` clears the moment's url, so the live context had no host: private_or_unknown_page.
Now the gate-log row carries `shot_reason` (a code only) and oi live's `last_host` of the same browser lets the
offer through for a `vocal.shot_safe_hosts` host. A browser with no host and no safe last host still gets no offer.
Nothing is ever sent or captured: the fakes of test_vocal_long_and_shot, the gate is tests/conftest.py's."""
import json
from datetime import datetime, timezone

import pytest

from oi import gate, live, paths, vocal
from tests.test_vocal_long_and_shot import Cap, Fake, T, _send, _settings, _status, no_real_send  # noqa: F401

SAFE = {"vocal": {"shot_safe_hosts": ["dash.example.net", "example.org"]}}


def _gate_rows():
    d = paths.data_dir() / "gate-log"
    return [json.loads(l) for f in sorted(d.glob("*.jsonl")) for l in f.read_text().splitlines()]


# ---- (1) the reason is logged, a code only --------------------------------------------------------------------

def test_no_offer_reason_is_logged_as_a_code(gate_asks):
    _status({"app": "Safari", "host": None, "desk": 1})
    _send("nova")
    row = _gate_rows()[-1]
    assert row["offer_shot"] is False and row["shot_reason"] == "private_or_unknown_page"


def test_offered_row_has_no_reason_and_no_page_detail(gate_asks):
    _status({"app": "Safari", "host": "github.com", "desk": 2})
    _send("nova")
    row = _gate_rows()[-1]
    assert row["offer_shot"] is True and "shot_reason" not in row
    assert "github" not in json.dumps(row)


def test_other_paths_log_no_reason(gate_asks):
    _status({"app": "Safari", "host": None, "desk": 1})
    vocal.send("nova", "salut", now=T, runner=Fake(), raw_settings={})      # offer_shot False: nothing to explain
    assert "shot_reason" not in _gate_rows()[-1]


def test_gate_keeps_the_reason_short():
    g = gate.outward("vocal", "nova", "Nova", "x", raw={}, ask=lambda r, t: {"event": "dropped"},
                     shot_reason="r" * 100)
    assert not g["ok"] and len(_gate_rows()[-1]["shot_reason"]) == 40


# ---- (2) the live context remembers the browser's last host ---------------------------------------------------

NOW = datetime(2026, 10, 22, 15, 30, 0, tzinfo=timezone.utc).timestamp()
ROWS = [{"ts": "2026-10-22T15:28:00Z", "token": "activate:Safari", "app": "Safari"},
        {"ts": "2026-10-22T15:28:10Z", "token": "url:dash.example.net/home", "app": "Safari"},
        {"ts": "2026-10-22T15:29:00Z", "token": "activate:Telegram", "app": "Telegram"},
        {"ts": "2026-10-22T15:29:44Z", "token": "activate:Safari", "app": "Safari"}]


def test_status_context_last_host_back_on_the_same_tab():
    ctx = live.status_context({"app": "Safari", "url": None, "space": 2}, ROWS, now=NOW)
    assert ctx == {"app": "Safari", "host": None, "desk": 2, "last_host": "dash.example.net"}


def test_status_context_no_last_host_for_another_app_or_with_a_host():
    assert "last_host" not in live.status_context({"app": "Google Chrome", "url": None}, ROWS, now=NOW)
    ctx = live.status_context({"app": "Safari", "url": "github.com/x"}, ROWS)
    assert ctx["host"] == "github.com" and "last_host" not in ctx
    assert live.status_context({"app": "Safari"}) == {"app": "Safari", "host": None, "desk": None}


# ---- (3) the offer on a safe last host; refused otherwise ---------------------------------------------------------

@pytest.mark.parametrize("last", ["dash.example.net", "dash.example.net:8443", "www.example.org",
                                  "blog.example.org"])
def test_offered_back_on_a_safe_host(gate_asks, last):
    _status({"app": "Safari", "host": None, "desk": 1, "last_host": last})
    _send("nova", raw_settings=SAFE, app="Safari")
    assert gate_asks.asked[-1]["offer_shot"] is True
    assert "shot_reason" not in _gate_rows()[-1]


def test_safe_host_capture_still_needs_the_choice(gate_asks):
    _status({"app": "Safari", "host": None, "desk": 1, "last_host": "dash.example.net"})
    cap = Cap()
    r = _send("nova", raw_settings=SAFE, capture=cap, runner=Fake())
    assert r["ok"] and cap.calls == []                  # offered, OFF by default: validate alone captures nothing


@pytest.mark.parametrize("ctx,app,raw", [
    ({"app": "Safari", "host": None, "last_host": "github.com"}, "Safari", SAFE),             # not a safe host
    ({"app": "Safari", "host": None, "last_host": "evilexample.org"}, "Safari", SAFE),        # not a subdomain
    ({"app": "Safari", "host": None}, "Safari", SAFE),                                         # private: no host
    ({"app": "Safari", "host": None, "last_host": "dash.example.net"}, "Google Chrome", SAFE),   # other browser
    ({"app": "Safari", "host": None, "last_host": "dash.example.net"}, "Safari",
     {"vocal": {"shot_safe_hosts": []}}),                                                      # list emptied
    ({"app": "Safari", "host": None, "last_host": 7}, "Safari", SAFE),                        # wrongly typed
], ids=["unsafe", "lookalike", "private", "other-browser", "empty-list", "bad-type"])
def test_still_refused(gate_asks, ctx, app, raw):
    _status(ctx)
    _send("nova", raw_settings=raw, app=app)
    assert "offer_shot" not in gate_asks.asked[-1]
    assert _gate_rows()[-1]["shot_reason"] == "private_or_unknown_page"


def test_local_only_wins_over_a_safe_last_host(gate_asks):
    _status({"app": "Safari", "host": None, "last_host": "dash.example.net"})
    _settings({"claude": {"local_only_domains": ["example.net"]}})
    _send("nova", raw_settings=SAFE, app="Safari")
    assert _gate_rows()[-1]["shot_reason"] == "local_only"


def test_no_safe_host_by_default(gate_asks):
    _status({"app": "Safari", "host": None, "last_host": "dash.example.net"})
    _send("nova", app="Safari")
    assert _gate_rows()[-1]["shot_reason"] == "private_or_unknown_page"


def test_safe_hosts_setting_parse():
    assert vocal.vocal_settings({"vocal": {"shot_safe_hosts": [" WWW.A.org ", 3, ""]}})["shot_safe_hosts"] == ["a.org"]
    assert vocal.vocal_settings({"vocal": {"shot_safe_hosts": "a.org"}})["shot_safe_hosts"] == \
        vocal.VOCAL_DEFAULTS["shot_safe_hosts"]
    vocal.vocal_settings({})["shot_safe_hosts"].append("x")          # a copy: the defaults are never mutated
    assert "x" not in vocal.VOCAL_DEFAULTS["shot_safe_hosts"]


def test_last_host_expires_after_max_age():
    later = NOW + live.LAST_HOST_MAX_AGE_S + 1 + 110     # url row is 110 s before NOW
    assert "last_host" not in live.status_context({"app": "Safari", "url": None}, ROWS, now=later)


def test_last_host_dropped_when_another_browser_was_in_front_since():
    rows = ROWS[:2] + [{"ts": "2026-10-22T15:29:10Z", "token": "activate:Google Chrome", "app": "Google Chrome"},
                       {"ts": "2026-10-22T15:29:44Z", "token": "activate:Safari", "app": "Safari"}]
    assert "last_host" not in live.status_context({"app": "Safari", "url": None}, rows, now=NOW)
