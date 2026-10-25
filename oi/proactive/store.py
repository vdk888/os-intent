"""W24: events/<UTC day>.jsonl and the `evt:<source>:<class>` tokens.

An event row (local only, 0600, next to raw/ which already holds window titles):
  {"ts": ISO UTC, "id": "<source>:<16 hex>", "source": "mail|agent|file|cockpit|metric|market|calendar|github", "class": "<slug>",
   "who": display name | null, "subject": str | null, "ref": {message_id | address | chat | slug | path | url},
   "private": bool, "state": "live", "wiki": {"page"} (only when the wiki judge found it important: judge.py)}
`private` (local-only domain / leaks gate): no subject is stored, nothing of it goes to Claude.
Context is STATE, not timers (docs/DESIGN.md design rule): an event is live from its appear row until its sensor sees the
state end (mail read or replied, chat read, file opened or gone, alert closed, metric / market back inside its
threshold) and writes the matching gone row {"ts", "id" (the same), "source", "class", "state": "gone"} (token
`evt:<source>:<class>:gone`). No age limit anywhere: `Live` holds what is live now (the hint's validity and the
Claude "Events" context).
The token never carries more than the source and the class.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import paths, vocab

TOKEN_SOURCES = ("mail", "agent", "file", "cockpit", "metric", "market", "calendar", "github")
SRC = "oi-evt"
_LOCK = threading.Lock()


def events_dir() -> Path:
    return paths.data_dir() / "events"


def event_id(source: str, *parts) -> str:
    h = hashlib.sha1(json.dumps([source, *parts], default=str, ensure_ascii=False).encode()).hexdigest()[:16]
    return f"{source}:{h}"


def make(source: str, cls: str, *, ts: float, key, who: str | None = None, subject: str | None = None,
         ref: dict | None = None, private: bool = False) -> dict | None:
    """One event (None when its token would not be valid). `key` = what makes it unique (message id, path, alert id)."""
    c = vocab.slug(cls, 24) or "other"
    tok = f"evt:{source}:{c}"
    if source not in TOKEN_SOURCES or not vocab.validate(tok):
        return None
    return {"ts": vocab.iso_utc(datetime.fromtimestamp(ts, timezone.utc)), "id": event_id(source, key),
            "source": source, "class": c, "who": (who or None) and str(who)[:60],
            "subject": None if private else ((subject or None) and " ".join(str(subject).split())[:160]),
            "ref": dict(ref or {}), "private": bool(private), "state": "live"}


def gone(ev: dict, *, ts: float) -> dict:
    """The row that ends `ev` (its state is over): same id, source and class, nothing else."""
    return {"ts": vocab.iso_utc(datetime.fromtimestamp(ts, timezone.utc)), "id": ev["id"], "source": ev["source"],
            "class": ev["class"], "state": "gone"}


def is_gone(ev: dict) -> bool:
    return ev.get("state") == "gone"


def token(ev: dict) -> str:
    return f"evt:{ev['source']}:{ev['class']}" + (":gone" if is_gone(ev) else "")


def token_row(ev: dict) -> dict:
    return {"ts": ev["ts"], "dur": 0.0, "token": token(ev), "app": None, "src": SRC, "raw_id": None}


def append(ev: dict) -> Path:
    """One row to events/<day>.jsonl. The wiki verdict keeps its page name only (its snippet stays in memory)."""
    if ev.get("wiki"):
        ev = dict(ev, wiki={"page": ev["wiki"].get("page")})
    p = events_dir() / f"{ev['ts'][:10]}.jsonl"
    with _LOCK:
        paths.ensure_private_root()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.parent.chmod(0o700)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return p


def read_day(day: str) -> list[dict]:
    p = events_dir() / f"{day}.jsonl"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("ts"), str) and r.get("source") in TOKEN_SOURCES:
            out.append(r)
    return out


def tokens(rows: list[dict]) -> list[dict]:
    """Event rows -> token rows (invalid ones skipped)."""
    out = []
    for r in rows:
        try:
            vocab.parse_ts(r["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        tok = f"evt:{r.get('source')}:{r.get('class')}" + (":gone" if is_gone(r) else "")
        if vocab.validate(tok):
            out.append({"ts": r["ts"], "dur": 0.0, "token": tok, "app": None, "src": SRC, "raw_id": None})
    return out


class Live:
    """The events whose state is live NOW (appear rows not yet ended by their gone row), newest first.
    In memory, filled by the engine as the sensors report: a sensor primes at start (what exists then is never an
    event), so after a restart nothing old is live and nothing waits for a gone that would never come."""

    def __init__(self):
        self._evs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def apply(self, ev: dict) -> None:
        with self._lock:
            if is_gone(ev):
                self._evs.pop(ev.get("id"), None)
            elif ev.get("id"):
                self._evs[ev["id"]] = ev

    def is_live(self, ev_id: str | None) -> bool:
        with self._lock:
            return ev_id in self._evs

    def current(self, limit: int | None = None) -> list[dict]:
        with self._lock:
            evs = sorted(self._evs.values(), key=lambda e: e["ts"], reverse=True)
        return evs[:limit] if limit else evs
