import json

from conftest import AFK, WIN, ev

from oi import aw, paths


def _raw(bucket, day="2026-01-05"):
    return aw.read_jsonl(paths.raw_dir() / bucket / f"{day}.jsonl")


def test_pull_incremental_no_duplicates(fake_aw):
    fake_aw.buckets[WIN] = [ev(1, 0, 5, app="AppA", title="doc-1"), ev(2, 5, 3, app="AppB", title="doc-2")]
    fake_aw.buckets[AFK] = [ev(1, 0, 60, status="not-afk")]
    r1 = {r["bucket"]: r for r in aw.pull()}
    assert r1[WIN]["new"] == 2 and r1[AFK]["new"] == 1
    cur = json.loads(aw.cursor_path(WIN).read_text())
    assert cur["id"] == 2

    # second pull: same events again (heartbeat extended the open one) plus one new
    fake_aw.buckets[WIN][1] = ev(2, 5, 9, app="AppB", title="doc-2")
    fake_aw.buckets[WIN].append(ev(3, 14, 1, app="AppA", title="doc-1"))
    r2 = {r["bucket"]: r for r in aw.pull()}
    assert r2[WIN]["new"] == 1 and r2[AFK]["new"] == 0
    rows = _raw(WIN)
    assert [r["id"] for r in rows] == [1, 2, 3]
    assert rows[1]["duration"] == 9  # open event updated, not duplicated
    # cursor was used: the second request asked from the last timestamp
    assert any("start=" in p for p in fake_aw.requests if WIN.split("_")[0] in p)

    r3 = {r["bucket"]: r for r in aw.pull()}
    assert r3[WIN]["new"] == 0 and len(_raw(WIN)) == 3


def test_pull_pages_and_splits_days(fake_aw):
    evs = [ev(i, i * 3600, 10, app="AppA", title="t") for i in range(1, 30)]  # crosses midnight
    fake_aw.buckets[WIN] = evs
    got = aw.fetch_events(WIN, None, page=7)
    assert [e["id"] for e in got] == list(range(1, 30))
    aw.pull()
    d1, d2 = _raw(WIN, "2026-01-05"), _raw(WIN, "2026-01-06")
    assert len(d1) + len(d2) == 29 and d1 and d2


def test_since_overrides_cursor(fake_aw):
    fake_aw.buckets[WIN] = [ev(1, 0, 1, app="AppA"), ev(2, 100, 1, app="AppB")]
    aw.pull()
    fake_aw.requests.clear()
    aw.pull(since="2026-01-05T08:00:00+00:00")
    assert any("start=2026-01-05T08" in p for p in fake_aw.requests)
    assert len(_raw(WIN)) == 2


def test_data_dir_env_and_private(data_dir):
    assert paths.data_dir() == data_dir
    aw.write_jsonl(paths.raw_dir() / "b" / "2026-01-05.jsonl", [{"id": 1}])
    assert (data_dir.stat().st_mode & 0o777) == 0o700


def test_clipped_refetch_never_shrinks_stored_row(fake_aw):
    fake_aw.buckets[WIN] = [ev(1, 0, 100, app="AppA"), ev(2, 100, 10, app="AppB")]
    aw.pull()
    aw.pull(since="2026-01-05T09:00:50+00:00")  # mid-event: server returns event 1 clipped
    rows = _raw(WIN)
    assert [(r["id"], r["timestamp"], r["duration"]) for r in rows][0] == (1, "2026-01-05T09:00:00+00:00", 100)
    assert len(rows) == 2


def test_merge_keeps_widest_span():
    a = ev(1, 0, 10, app="AppA")
    b = ev(1, 5, 20, app="AppA")  # clipped start, extended by heartbeat
    m = aw.merge(a, b)
    assert m["timestamp"] == a["timestamp"] and m["duration"] == 25



# ---- poll only the buckets that changed (last_updated / metadata.end) ----
import socket  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

import pytest  # noqa: E402

OLD = "aw-watcher-window_old-host"


def _with_last_updated(monkeypatch, fake):
    """Serve `last_updated` (end of the latest event, as aw-server Python does) on GET /buckets/."""
    real = aw._get

    def get(url, timeout=aw.FULL_TIMEOUT_S):
        body = real(url, timeout)
        if url.endswith("/api/0/buckets/"):
            for b, meta in body.items():
                evs = fake.buckets.get(b) or []
                if evs:
                    last = max(evs, key=lambda e: e["timestamp"])
                    end = datetime.fromisoformat(last["timestamp"]) + timedelta(seconds=last["duration"])
                    meta["last_updated"] = end.isoformat()
        return body
    monkeypatch.setattr(aw, "_get", get)


def _event_requests(fake):
    return [p for p in fake.requests if "/events" in p]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_pull_with_gate_fetches_only_changed_buckets(fake_aw, monkeypatch):
    _with_last_updated(monkeypatch, fake_aw)
    fake_aw.buckets[WIN] = [ev(1, 0, 5, app="AppA")]
    fake_aw.buckets[AFK] = [ev(1, 0, 60, status="not-afk")]
    fake_aw.buckets[OLD] = [ev(1, 0, 9, app="AppZ")]
    clock = Clock()
    gate = aw.Gate(aw.LIVE_REFRESH_S, clock=clock)
    assert {r["bucket"] for r in aw.pull(gate=gate)} == {WIN, AFK, OLD}   # first pull: everything

    clock.t += aw.HOT_S
    fake_aw.requests.clear()
    assert aw.pull(gate=gate) == []                                     # nothing moved: one GET /buckets/
    assert fake_aw.requests == ["/api/0/buckets/"]

    fake_aw.requests.clear()
    fake_aw.buckets[WIN][0] = ev(1, 0, 8, app="AppA")                   # a heartbeat extends the open event
    r = aw.pull(keep_events=True, gate=gate)
    assert [x["bucket"] for x in r] == [WIN] and r[0]["events"][0]["duration"] == 8
    assert len(_event_requests(fake_aw)) == 1 and _raw(WIN)[0]["duration"] == 8


def test_gate_hot_window_refetches_a_bucket_that_just_moved(fake_aw, monkeypatch):
    """A late insert with an older timestamp leaves last_updated as is: a bucket that just moved is still fetched.
    (What the main pull then gets is bounded by the bucket cursor, as before.)"""
    _with_last_updated(monkeypatch, fake_aw)
    fake_aw.buckets[WIN] = [ev(1, 0, 5, app="AppA")]
    clock = Clock()
    gate = aw.Gate(aw.LIVE_REFRESH_S, clock=clock)
    aw.pull(gate=gate)
    clock.t += aw.HOT_S
    fake_aw.buckets[WIN].append(ev(3, 20, 1, app="AppC"))               # moves the signature
    aw.pull(gate=gate)
    clock.t += 2
    fake_aw.requests.clear()
    assert [r["bucket"] for r in aw.pull(gate=gate)] == [WIN]           # unchanged signature, still hot: fetched
    assert len(_event_requests(fake_aw)) == 1
    clock.t += aw.HOT_S
    assert aw.pull(gate=gate) == []                                     # cooled down


def test_gate_refreshes_every_bucket_and_forgets_deleted_ones():
    clock = Clock()
    gate = aw.Gate(60, hot_s=5, clock=clock)
    gate.fetched("a", "s1")
    gate.fetched("b", "s1")
    clock.t += 10
    assert not gate.due("a", "s1") and gate.due("a", "s2") and gate.due("a", None) and gate.due("new", "s")
    clock.t += 60
    assert gate.due("a", "s1")                                          # refresh_s elapsed
    gate.keep(["a"])
    assert set(gate.state) == {"a"}


def test_pull_with_gate_without_hint_fetches_all(fake_aw):
    fake_aw.buckets[WIN] = [ev(1, 0, 5, app="AppA")]                    # FakeAW serves no last_updated
    clock = Clock()
    gate = aw.Gate(aw.LIVE_REFRESH_S, clock=clock)
    aw.pull(gate=gate)
    clock.t += aw.HOT_S
    fake_aw.requests.clear()
    assert [r["bucket"] for r in aw.pull(gate=gate)] == [WIN]
    assert len(_event_requests(fake_aw)) == 1


def test_pull_with_gate_since_overrides_gate(fake_aw, monkeypatch):
    _with_last_updated(monkeypatch, fake_aw)
    fake_aw.buckets[WIN] = [ev(1, 0, 5, app="AppA")]
    clock = Clock()
    gate = aw.Gate(aw.LIVE_REFRESH_S, clock=clock)
    aw.pull(gate=gate)
    clock.t += aw.HOT_S
    assert [r["bucket"] for r in aw.pull(since="2026-01-05T08:00:00+00:00", gate=gate)] == [WIN]


def test_pull_with_gate_retries_a_bucket_whose_fetch_failed(fake_aw, monkeypatch):
    _with_last_updated(monkeypatch, fake_aw)
    fake_aw.buckets[WIN] = [ev(1, 0, 5, app="AppA")]
    gate = aw.Gate(aw.LIVE_REFRESH_S)
    real = aw.fetch_events
    monkeypatch.setattr(aw, "fetch_events", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(OSError):
        aw.pull(gate=gate)
    assert WIN not in gate.state
    monkeypatch.setattr(aw, "fetch_events", real)
    assert [r["bucket"] for r in aw.pull(gate=gate)] == [WIN]


def test_bucket_meta_python_and_rust_shapes(monkeypatch):
    monkeypatch.setattr(aw, "_get", lambda url, timeout=aw.FULL_TIMEOUT_S: {
        "py": {"id": "py", "last_updated": "2026-01-05T09:00:05+00:00"},
        "rust": {"id": "rust", "last_updated": None, "metadata": {"start": "x", "end": "2026-01-05T09:00:07Z"}},
        "bare": {"id": "bare"}})
    m = aw.bucket_meta("http://x")
    assert m["py"] == ("2026-01-05T09:00:05+00:00", None)
    assert m["rust"] == (None, "2026-01-05T09:00:07Z")
    assert m["bare"] is None


def test_timeouts_short_for_live_polls_long_for_oi_pull(fake_aw, monkeypatch):
    fake_aw.buckets[WIN] = [ev(1, 0, 5, app="AppA")]
    real, seen = aw._get, []
    monkeypatch.setattr(aw, "_get", lambda url, timeout=aw.FULL_TIMEOUT_S: seen.append(timeout) or real(url, timeout))
    aw.pull()                                                           # `oi pull`: listing + first fetch
    aw.pull()                                                           # `oi pull` again: incremental, still 30 s
    assert set(seen) == {aw.FULL_TIMEOUT_S}
    seen.clear()
    aw.pull(gate=aw.Gate(aw.LIVE_REFRESH_S))                            # `oi live`: cursor exists
    assert seen == [aw.TIMEOUT_S, aw.TIMEOUT_S] and aw.TIMEOUT_S <= 3


def test_hung_server_times_out_fast_as_oserror(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)                                                       # accepts, never answers
    monkeypatch.setattr(aw, "TIMEOUT_S", 0.3)
    t0 = time.monotonic()
    try:
        with pytest.raises(OSError):                                    # what Live.pull catches
            aw.pull(base=f"http://127.0.0.1:{srv.getsockname()[1]}", gate=aw.Gate(aw.LIVE_REFRESH_S))
    finally:
        srv.close()
    assert time.monotonic() - t0 < 2
