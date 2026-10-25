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
