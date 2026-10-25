"""Seeded synthetic token streams (docs/DESIGN.md row shape). No real data."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

# next-token table: token -> [(next, weight)]
TABLE = {
    "activate:Mail": [("key:⌘C", 3), ("activate:Safari", 4), ("activate:Code", 2)],
    "key:⌘C": [("activate:Telegram", 3), ("key:⌘V", 2), ("activate:Safari", 1)],
    "key:⌘V": [("activate:Mail", 2), ("activate:Code", 2)],
    "activate:Safari": [("url:example.org/prr-approuve", 7), ("url:example.org/kanban", 2),
                        ("activate:Mail", 1)],
    "url:example.org/prr-approuve": [("url:example.org/kanban", 5), ("activate:Code", 2),
                                     ("url:docs.example.com/guide", 2)],
    "url:example.org/kanban": [("activate:Code", 3), ("activate:Telegram", 2), ("space:+1", 1)],
    "url:docs.example.com/guide": [("activate:Code", 4), ("activate:Safari", 1)],
    "activate:Code": [("key:⌘C", 2), ("activate:Safari", 3), ("space:+1", 1), ("activate:Notes", 1)],
    "activate:Telegram": [("activate:Mail", 3), ("space:-1", 2)],
    "space:+1": [("activate:Telegram", 3), ("activate:Finder", 1)],
    "space:-1": [("activate:Code", 2), ("activate:Mail", 1)],
    "activate:Finder": [("activate:Code", 1), ("space:-1", 1)],
    "activate:Notes": [("title:Notes/meeting-notes", 3), ("activate:Code", 1)],
    "title:Notes/meeting-notes": [("activate:Mail", 1), ("activate:Code", 1)],
}
APP_OF = {"Mail", "Safari", "Code", "Telegram", "Finder", "Notes"}
SRC = {"activate": "aw-window", "url": "oi-safari", "title": "aw-window", "space": "oi-space",
       "key": "oi-key", "afk": "aw-afk"}


def _pick(rng, opts):
    total = sum(w for _, w in opts)
    x = rng.uniform(0, total)
    for tok, w in opts:
        x -= w
        if x <= 0:
            return tok
    return opts[-1][0]


def stream(days: int = 2, seed: int = 7, start: str = "2026-10-12", rare: int = 3) -> list[dict]:
    """~2 working days (06:00-17:00 UTC), lunch afk, occasional long gaps, a few rare tokens."""
    rng = random.Random(seed)
    rows: list[dict] = []
    d0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    rid = 0
    for d in range(days):
        t = d0 + timedelta(days=d, hours=6)
        end = t + timedelta(hours=11)
        tok = "space:=1"
        app = None
        lunch = False
        while t < end:
            if not lunch and t.hour >= 11:
                for edge, gap in (("afk:start", 3600), ("afk:end", 5)):
                    rows.append({"ts": t, "token": edge, "app": app, "src": "aw-afk"}); t += timedelta(seconds=gap)
                lunch = True
                tok = "activate:Mail"
            kind, arg = tok.split(":", 1)
            if kind == "activate":
                app = arg
            rows.append({"ts": t, "token": tok, "app": app, "src": SRC[kind]})
            t += timedelta(seconds=rng.choice([4, 8, 15, 30, 45, 70, 110, 200, 400]))
            if rare and rng.random() < 0.01:
                rows.append({"ts": t, "token": f"url:rare.example/r{rng.randrange(1000)}", "app": app,
                             "src": "oi-safari"})
                t += timedelta(seconds=10)
            tok = _pick(rng, TABLE.get(tok, [("activate:Mail", 1)]))
    out = []
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["ts"] if i + 1 < len(rows) else r["ts"] + timedelta(seconds=30)
        out.append({"ts": r["ts"].isoformat().replace("+00:00", "Z"),
                    "dur": (nxt - r["ts"]).total_seconds(), "token": r["token"], "app": r["app"],
                    "src": r["src"], "raw_id": i})
    return out
