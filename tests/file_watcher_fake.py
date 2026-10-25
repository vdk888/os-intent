"""A fake oi-watcher for the « files » connector (FolderSensor reads the watcher's bucket and status file): events as
the watcher posts them to `oi-watcher-file_<host>` (Sources/oi-watcher/FolderSource.swift) and the read state of
status/oi-watcher-files.json. Not a test module itself (no test_ prefix). No folder, aw-server or watcher is touched."""
import os
from datetime import datetime, timezone

from oi import vocab
from oi.proactive import sensors

BUCKET = "oi-watcher-file_host-a"


class FakeFileWatcher:
    def __init__(self, folders, now: float):
        self.events: list[dict] = []
        self.folders = {f: "ok" for f in folders}
        self.now = now                      # the status file's ts (fresh by default)
        self.running = True
        self.aw_down = False
        self.n = 0

    def _ts(self, t: float) -> str:
        return vocab.iso_utc(datetime.fromtimestamp(t, timezone.utc))

    def live(self, path: str, t: float) -> None:
        self.n += 1
        self.events.append({"id": self.n, "timestamp": self._ts(t), "duration": 0.0,
                            "data": {"kind": "file", "state": "live", "path": path, "class": "x", "mtime": int(t),
                                     "size": 1}})

    def gone(self, path: str, t: float, reason: str = "removed") -> None:
        self.n += 1
        self.events.append({"id": self.n, "timestamp": self._ts(t), "duration": 0.0,
                            "data": {"kind": "file", "state": "gone", "path": path, "class": "x", "reason": reason}})

    # the sensor's collaborators
    def buckets(self) -> list[str]:
        if self.aw_down:
            raise OSError("aw-server down")
        return ["aw-watcher-window_host-a", BUCKET]

    def fetch(self, bucket: str, start: str | None) -> list[dict]:
        if self.aw_down:
            raise OSError("aw-server down")
        assert bucket == BUCKET
        lo = vocab.parse_ts(start).timestamp() if start else float("-inf")
        return [e for e in self.events if vocab.parse_ts(e["timestamp"]).timestamp() >= lo]

    def status(self) -> dict | None:
        if not self.running:
            return None
        return {"ts": self._ts(self.now), "pid": 1, "enabled": True, "folders": dict(self.folders)}

    def sensor(self, **kw) -> "sensors.FolderSensor":
        kw.setdefault("claude_cfg", lambda: {"local_only_domains": []})
        return sensors.FolderSensor(buckets=self.buckets, fetch=self.fetch, status=self.status, **kw)


def path_in(folder, name: str) -> str:
    return os.path.join(str(folder), name)
