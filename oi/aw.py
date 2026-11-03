"""Incremental, read-only pull of every bucket of the local aw-server.

Raw rows are stored as returned by aw-server (id, timestamp, duration, data), one
file per bucket per UTC day: <data>/raw/<bucket>/YYYY-MM-DD.jsonl. A cursor per
bucket (<data>/cursors/<bucket>.json: last timestamp + id) makes pulls incremental.

aw-server merges heartbeats into the latest event (same id, growing duration), so
each pull re-fetches from the cursor timestamp inclusive and upserts rows by id:
repeated pulls never duplicate and the open event's duration stays current.
aw-server also CLIPS events that straddle the requested start/end (same id, shifted
timestamp, shorter duration; observed on v0.14.1), so an upsert merges by id to the
widest span seen instead of overwriting: a clipped copy never shrinks a stored row.

`oi live` pulls every 2 s, so it passes a `Gate` to `pull`: one `GET /buckets/` gives each
bucket's `last_updated` (aw-server Python) or `metadata.end` (aw-server-rust), and only the buckets
whose value moved are fetched. A heartbeat moves the end of the latest event, so an active bucket is
still fetched on every poll; a bucket without either field is always fetched. An event inserted late
with an older timestamp does not move that value, so a Gate can keep a bucket that moved "hot"
(fetched every poll) for `hot_s` (the screenshot poller), and every bucket is re-fetched at least
every `refresh_s` whatever its value.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import paths
from .vocab import parse_ts

DEFAULT_URL = "http://127.0.0.1:5600"
PAGE = 5000
TIMEOUT_S = 3.0          # live polls (localhost): a hung aw-server stalls the 2 s loop for 3 s, not 30 s
FULL_TIMEOUT_S = 30.0    # `oi pull` and first pulls (no cursor) may page through a long history
HOT_S = 10.0             # Gate default: a bucket that moved is fetched every poll this long (late, older inserts)
LIVE_REFRESH_S = 300.0   # `oi live`: every bucket re-fetched at least this often, whatever its signature


def server_url() -> str:
    return os.environ.get("OI_AW_URL", DEFAULT_URL).rstrip("/")


def _get(url: str, timeout: float = FULL_TIMEOUT_S) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # localhost only
        return json.load(r)


def list_buckets(base: str | None = None) -> list[str]:
    base = base or server_url()
    return sorted(_get(f"{base}/api/0/buckets/"))


class Gate:
    """Which buckets a poller fetches this poll, from their `bucket_meta` signature.

    Due: no signature, a new or moved signature, moved less than `hot_s` ago, or not fetched for
    `refresh_s`. Call `fetched(b, sig)` only after a successful fetch, so a failed one is retried."""

    def __init__(self, refresh_s: float, hot_s: float = HOT_S, clock=time.monotonic):
        self.refresh_s, self.hot_s, self.clock = refresh_s, hot_s, clock
        self.state: dict[str, tuple] = {}          # bucket -> (signature, moved_at, fetched_at)

    def due(self, bucket: str, sig: Any) -> bool:
        st = self.state.get(bucket)
        if sig is None or st is None or st[0] != sig:
            return True
        now = self.clock()
        return now - st[1] < self.hot_s or now - st[2] >= self.refresh_s

    def fetched(self, bucket: str, sig: Any) -> None:
        now, st = self.clock(), self.state.get(bucket)
        self.state[bucket] = (sig, now if st is None or st[0] != sig else st[1], now)

    def keep(self, buckets) -> None:
        """Forget the buckets aw-server no longer lists."""
        for b in set(self.state) - set(buckets):
            del self.state[b]


def bucket_meta(base: str | None = None) -> dict[str, Any]:
    """{bucket: change signature} from one `GET /buckets/`; None when the server gives no hint."""
    base = base or server_url()
    out: dict[str, Any] = {}
    for b, m in _get(f"{base}/api/0/buckets/", TIMEOUT_S).items():
        m = m if isinstance(m, dict) else {}
        lu, end = m.get("last_updated"), (m.get("metadata") or {}).get("end")
        out[b] = (lu, end) if (lu or end) else None
    return out


def fetch_events(bucket: str, start: str | None, base: str | None = None, page: int = PAGE,
                 timeout: float = FULL_TIMEOUT_S) -> list[dict]:
    """All events of a bucket from `start` (inclusive, None = everything), oldest first.

    aw-server returns newest first and caps by `limit`; we page backwards with `end`.
    """
    base = base or server_url()
    q = urllib.parse.quote(bucket, safe="")
    out: dict[Any, dict] = {}
    end: str | None = None
    while True:
        params = {"limit": str(page)}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        batch = _get(f"{base}/api/0/buckets/{q}/events?{urllib.parse.urlencode(params)}", timeout)
        new = 0
        for ev in batch:
            k = _key(ev)
            if k not in out:
                new += 1
            out[k] = merge(out.get(k), ev)
        if len(batch) < page or new == 0:
            break
        end = min(batch, key=_sort_key)["timestamp"]
    return sorted(out.values(), key=_sort_key)


def merge(old: dict | None, new: dict) -> dict:
    """Same event seen twice: keep the widest span (aw clips at query bounds), newest data."""
    if old is None:
        return new
    o0, n0 = parse_ts(old["timestamp"]), parse_ts(new["timestamp"])
    start = min(o0, n0)
    end = max(o0 + timedelta(seconds=old.get("duration") or 0), n0 + timedelta(seconds=new.get("duration") or 0))
    return {**new, "timestamp": (old if o0 <= n0 else new)["timestamp"],
            "duration": round((end - start).total_seconds(), 6)}


def _key(ev: dict) -> Any:
    rid = ev.get("id")
    return rid if rid is not None else (ev.get("timestamp"), json.dumps(ev.get("data"), sort_keys=True))


def _sort_key(ev: dict) -> tuple:
    return (parse_ts(ev["timestamp"]), ev.get("id") or 0)


def day_of(ev: dict) -> str:
    return parse_ts(ev["timestamp"]).astimezone(timezone.utc).date().isoformat()


def safe_name(bucket: str) -> str:
    return bucket.replace("/", "_")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    paths.ensure_private_root()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def cursor_path(bucket: str) -> Path:
    return paths.cursor_dir() / f"{safe_name(bucket)}.json"


def load_cursor(bucket: str) -> dict | None:
    p = cursor_path(bucket)
    return json.loads(p.read_text()) if p.exists() else None


def store(bucket: str, events: list[dict]) -> int:
    """Upsert events into the per-day raw files. Returns the number of NEW rows."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        by_day[day_of(ev)].append(ev)
    added = 0
    for day, evs in by_day.items():
        p = paths.raw_dir() / safe_name(bucket) / f"{day}.jsonl"
        rows = {_key(r): r for r in read_jsonl(p)}
        for ev in evs:
            k = _key(ev)
            if k not in rows:
                added += 1
            rows[k] = merge(rows.get(k), ev)
        write_jsonl(p, sorted(rows.values(), key=_sort_key))
    return added


def pull_bucket(bucket: str, since: str | None = None, base: str | None = None,
                keep_events: bool = False, timeout: float = FULL_TIMEOUT_S) -> dict:
    """keep_events: also return the fetched rows under "events" (`oi live` merges them in memory)."""
    cur = load_cursor(bucket)
    start = since or (cur or {}).get("ts")
    events = fetch_events(bucket, start, base, timeout=timeout if start else FULL_TIMEOUT_S)
    added = store(bucket, events)
    if events:
        last = events[-1]
        if not cur or _sort_key(last) >= (parse_ts(cur["ts"]), cur.get("id") or 0):
            p = cursor_path(bucket)
            paths.ensure_private_root()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"ts": last["timestamp"], "id": last.get("id")}))
    out = {"bucket": bucket, "fetched": len(events), "new": added}
    if keep_events:
        out["events"] = events
    return out


def pull(since: str | None = None, base: str | None = None, keep_events: bool = False,
         gate: Gate | None = None) -> list[dict]:
    """Pull every bucket (`oi pull`: 30 s timeout). With a `gate` (`oi live`, kept between pulls):
    short timeout, and only the buckets the gate says are due (all of them when `since` is given)."""
    if gate is None:
        return [pull_bucket(b, since, base, keep_events) for b in list_buckets(base)]
    meta = bucket_meta(base)
    gate.keep(meta)
    out = []
    for b, sig in sorted(meta.items()):
        if since is None and not gate.due(b, sig):
            continue
        out.append(pull_bucket(b, since, base, keep_events, timeout=TIMEOUT_S))
        gate.fetched(b, sig)
    return out


def raw_rows(day: str) -> dict[str, list[dict]]:
    """{bucket: rows} for one UTC day from the local raw store."""
    out: dict[str, list[dict]] = {}
    root = paths.raw_dir()
    if not root.exists():
        return out
    for bdir in sorted(p for p in root.iterdir() if p.is_dir()):
        rows = read_jsonl(bdir / f"{day}.jsonl")
        if rows:
            out[bdir.name] = rows
    return out
