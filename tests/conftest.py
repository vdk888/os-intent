"""Synthetic fixtures copying the real aw-server shapes (invented content only)."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from oi import vocal as _vocal

# The shipped example recipients (three) are kept here for the tests that check them; every other test runs against
# a larger fictional team, so pickers page, fall back and route across hosts.
SHIPPED_RECIPIENTS = {k: dict(v) for k, v in _vocal.DEFAULT_RECIPIENTS.items()}
TEST_TEAM = {
    "atlas": {"name": "Atlas", "kind": "inject", "path": _vocal.os.path.expanduser("~/.claude/channels/atlas/inject")},
    "juno": {"name": "Juno", "kind": "inject", "path": _vocal.os.path.expanduser("~/.claude/channels/juno/inject")},
    "nova": _vocal._remote("nova", "Nova", "agent-host"),
    "orion": _vocal._remote("orion", "Orion", "agent-host"),
    "lyra": _vocal._remote("lyra", "Lyra", "agent-host"),
    "iris": _vocal._remote("iris", "Iris", "agent-host"),
    "vega": _vocal._remote("vega", "Vega", "studio-host"),
    "hera": _vocal._remote("hera", "Hera", "laptop-host"),
    "echo": _vocal._remote("echo", "Echo", "laptop-host"),
}
_vocal.DEFAULT_RECIPIENTS.clear()
_vocal.DEFAULT_RECIPIENTS.update(TEST_TEAM)

from oi import contact as _contact  # noqa: E402

SHIPPED_CONTACT_MAP = dict(_contact.DEFAULT_CONTACT_MAP)
_contact.DEFAULT_CONTACT_MAP.clear()
_contact.DEFAULT_CONTACT_MAP.update({"Atlas - Tools": "atlas", "Nova - Planning": "nova", "Orion - Finance": "orion",
                                     "Lyra - Sales": "lyra", "Vega - Marketing": "vega", "Juno - Support": "juno"})

T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)
WIN = "aw-watcher-window_host-a"
AFK = "aw-watcher-afk_host-a"


def ts(sec: float, base: datetime = T0) -> str:
    return (base + timedelta(seconds=sec)).isoformat()


def ev(i: int, sec: float, dur: float, **data) -> dict:
    return {"id": i, "timestamp": ts(sec), "duration": dur, "data": data}


@pytest.fixture(autouse=True)
def claude_opted_in(monkeypatch):
    """Claude is opt-in for users (settings `claude.enabled` / `claude.enrich` default false); the feature tests run
    as an opted-in user. test_claude_actions.py::test_claude_is_opt_in_by_default checks the shipped default."""
    from oi import claude_actions
    monkeypatch.setitem(claude_actions.CLAUDE_DEFAULTS, "enabled", True)
    monkeypatch.setitem(claude_actions.CLAUDE_DEFAULTS, "enrich", True)


@pytest.fixture(autouse=True)
def no_real_providers(monkeypatch):
    """the glide's real readers (Spotlight, Telegram AX, Music AppleScript) never run in tests."""
    from oi import drill

    def blocked(*a, **kw):
        raise AssertionError("a real W23 provider ran in a test")
    for name in ("_mdfind", "_telegram_titles", "_osa_lines"):
        monkeypatch.setattr(drill, name, blocked)


@pytest.fixture(autouse=True)
def no_real_remote_copy(monkeypatch):
    """the screenshot's ssh copy to an agent's host never runs in tests (a test injects `remote_copy`)."""
    from oi import shots

    def blocked(*a, **kw):
        raise AssertionError("a real ssh image copy ran in a test")
    monkeypatch.setattr(shots, "_run_copy", blocked)


@pytest.fixture(autouse=True)
def no_real_calendar(monkeypatch):
    """The EventKit busy reader never runs in tests (a mail proposing a time = "no access" unless faked)."""
    from oi import calendar_slots
    calls: list = []
    monkeypatch.setattr(calendar_slots, "run_busy", lambda t0, t1: calls.append((t0, t1)) or ("permission", None))
    return calls


@pytest.fixture(autouse=True)
def gate_asks(monkeypatch):
    """Outward gate: no test ever reaches oi-hud. By default the user validates every confirm panel (the
    send mechanics tests); tests/test_gate.py sets `answer` to cancel / time out. The list keeps every request."""
    from oi import gate
    asked: list[dict] = []

    def ask(req, timeout_s):
        asked.append(req)
        a = getattr(ask, "answer", "validated")
        if a in ("validated", "cancelled"):
            return {"event": "outward", "id": req["id"], "decision": a, "reason": "esc" if a == "cancelled" else None}
        return a(req) if callable(a) else {"event": "dropped", "reason": str(a)}
    ask.asked = asked
    monkeypatch.setattr(gate, "ASK", ask)
    return ask


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OI_DATA_DIR", str(tmp_path / "oi-data"))
    return tmp_path / "oi-data"


class FakeAW:
    """Minimal aw-server: GET /api/0/buckets/ and /api/0/buckets/<id>/events (newest first)."""

    def __init__(self):
        self.buckets: dict[str, list[dict]] = {}
        self.requests: list[str] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                u = urlsplit(self.path)
                parts = [unquote(p) for p in u.path.strip("/").split("/")]
                if parts == ["api", "0", "buckets"]:
                    body = {b: {"id": b, "type": "x"} for b in fake.buckets}
                elif len(parts) == 5 and parts[4] == "events":
                    q = parse_qs(u.query)
                    lo = datetime.fromisoformat(q["start"][0]) if "start" in q else None
                    hi = datetime.fromisoformat(q["end"][0]) if "end" in q else None
                    evs = []
                    for e in fake.buckets.get(parts[3], []):
                        a = datetime.fromisoformat(e["timestamp"])
                        b = a + timedelta(seconds=e["duration"])
                        if (lo and b <= lo and a < lo) or (hi and a > hi):
                            continue
                        # like aw-server v0.14.1: events straddling the bounds come back clipped
                        a2, b2 = max(a, lo) if lo else a, min(b, hi) if hi else b
                        evs.append({**e, "timestamp": a2.isoformat(), "duration": (b2 - a2).total_seconds()})
                    evs.sort(key=lambda e: e["timestamp"], reverse=True)
                    body = evs[: int(q.get("limit", ["-1"])[0])] if int(q.get("limit", ["-1"])[0]) > 0 else evs
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake_aw(monkeypatch):
    f = FakeAW()
    monkeypatch.setenv("OI_AW_URL", f.url)
    yield f
    f.server.shutdown()


# ---- W3 (markov / forms / serialiser) fixtures: seeded synthetic streams, no real data ----
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import synth  # noqa: E402

TZ = ZoneInfo("Europe/Paris")


@pytest.fixture(autouse=True)
def _oi_tz(monkeypatch):
    monkeypatch.setenv("OI_TZ", "Europe/Paris")


@pytest.fixture(scope="session")
def rows():
    return synth.stream(days=2, seed=7)


@pytest.fixture(scope="session")
def moments(rows):
    from oi.moments import build_moments
    return build_moments(rows, tz=TZ)


@pytest.fixture(scope="session")
def model(rows, moments):
    from oi.markov import Markov
    return Markov.fit(moments, tokens=[r["token"] for r in rows])


@pytest.fixture(autouse=True)
def no_real_wiki(monkeypatch):
    """tests never touch the real wiki index or Ollama (the warm RAG answers "not available")."""
    from oi import rag
    from oi.orchestrator import pack
    monkeypatch.setattr(rag, "default", lambda repo_dir=None: None)
    monkeypatch.setattr(pack, "_DEFAULT", None)


@pytest.fixture(autouse=True)
def no_real_mail_probes(monkeypatch):
    """The Mail sensor's cheap probes (pgrep, ps, the Dock badge) never run in tests: Mail runs, is idle,
    its badge is unreadable (= the full scan every poll, as before the change detector). Tests inject their own."""
    from oi.proactive import sensors
    monkeypatch.setattr(sensors, "mail_running", lambda: True)
    monkeypatch.setattr(sensors, "mail_cpu", lambda: None)
    monkeypatch.setattr(sensors, "mail_badge", lambda: None)


@pytest.fixture(autouse=True)
def no_real_cadence_probes(monkeypatch):
    """2026-11-03 cadence: the shared Dock read and the power probe (pmset) never run in tests: the Dock is
    unreadable, the power source unknown (= AC: factor 1). Tests inject their own DockBadges / Power."""
    from oi.proactive import sensors
    monkeypatch.setattr(sensors, "DOCK", sensors.DockBadges(read=lambda: None, factor=lambda: 1.0))
    monkeypatch.setattr(sensors, "POWER", sensors.Power(read=lambda: None))
