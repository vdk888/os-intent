"""Moments: one per token boundary while not afk (docs/DESIGN.md).

A moment is taken right after token i. Tokens inside an afk span (from afk:start
until afk:end, or until afk:start's own `dur` has elapsed when the afk:end is missing,
e.g. a day file cut mid-afk) never become moments. Its label is token i+1 if it comes within
LABEL_WINDOW_S seconds (and is not an afk edge), else `nothing`. Its context is only
what was knowable at token i's timestamp: durations of earlier tokens are clipped to
the moment time, the current token's own duration is never shown (it is the future).

`wish:` tokens (W6) are feedback, not actions: they are taken out of the stream before the
moments are built, so a wish is never a moment, never a label and never in a history. Each wish
is attached to the moment that precedes it (the last moment with ts <= the wish's ts) as
`wish` = the wished-for action slug; when several wishes follow the same moment the last wins.
A wish before the first moment of the stream is dropped.

`evt:` tokens (W24) are CONTEXT: a moment is taken at an event (its history[0] is the event) but an event is never
a label: the label of any moment is the next non-event token within LABEL_WINDOW_S ("after event E the user does A").

`hud:` tokens (W8) are feedback too: taken out of the stream the same way. A `hud:pressed:<k>` sets
`hud_pressed` = k (int) on the preceding moment (same rule as the wish); shown/dismissed attach nothing.

Moment dict (JSON-serialisable, the `render --moment` input):
  ts, label, [wish] (only on a moment followed by a wish), [hud_pressed] (k, only after a HUD press),
  context: weekday, hhmm, daypart, hour_bucket, space, spaces, app, url, title, token
  history: newest first [{token, ago_s, dur_s|None}], history[0] = current token
  day_summary: [[app, seconds], ...] top apps since local midnight
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import vocab
from .vocab import parse_ts

LABEL_WINDOW_S = 120
NOTHING = "nothing"
HISTORY_KEEP = 20
DAY_SUMMARY_TOP = 5
FEEDBACK_PREFIXES = ("wish:", "hud:", "vocal:")  # never moments, labels or history (see module doc)
EVENT_PREFIX = "evt:"   # W24: context tokens (a moment, never a label)
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def default_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("OI_TZ", "Europe/Paris"))


def daypart(hour: int) -> str:
    if hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def hour_bucket(hour: int) -> int:
    """6 buckets of 4 h: 0 = 00-04, ..., 5 = 20-24 (local time)."""
    return hour // 4


def build_moments(rows: list[dict], tz: ZoneInfo | None = None,
                  history_keep: int = HISTORY_KEEP) -> list[dict]:
    tz = tz or default_tz()
    rows = sorted(rows, key=lambda r: r["ts"])
    wishes = [r for r in rows if r["token"].startswith("wish:")]
    pressed = [r for r in rows if r["token"].startswith("hud:pressed:")]
    rows = [r for r in rows if not r["token"].startswith(FEEDBACK_PREFIXES)]
    times = [parse_ts(r["ts"]) for r in rows]
    out: list[dict] = []
    afk = False
    afk_until = None  # afk:start ts + dur: ends the span even without an afk:end
    app = url = title = None
    space = spaces = None
    day_key = None
    day_secs: dict[str, float] = defaultdict(float)
    day_open: list = []  # [app, start_dt] of the current foreground app (for day summary)

    for i, row in enumerate(rows):
        tok = row["token"]
        t = times[i]
        local = t.astimezone(tz)
        if local.date() != day_key:
            day_key, day_secs = local.date(), defaultdict(float)
            midnight = datetime.combine(day_key, time(0), tzinfo=tz)
            day_open = [app, midnight] if (app and not afk) else []  # carry the running app over
        try:
            kind, g = vocab.parse(tok)
        except ValueError:
            kind, g = tok.split(":", 1)[0], {}
        if afk and afk_until is not None and t >= afk_until and kind != "afk":
            afk = False  # afk span elapsed with no afk:end
            day_open = [app, afk_until] if app else []

        # close the running foreground interval at this boundary
        if day_open:
            day_secs[day_open[0]] += max(0.0, (t - day_open[1]).total_seconds())
            day_open[1] = t

        if kind == "afk":
            afk = g.get("state") == "start"
            dur = row.get("dur")
            afk_until = (t + timedelta(seconds=float(dur))) if (afk and dur) else None
            # away time is not foreground time; resume the current app on return
            day_open = [] if afk else ([app, t] if app else [])
        if kind == "activate":
            app, url, title = g["app"], None, None
            day_open = [app, t]
        elif kind == "url":
            url = f"{g['host']}/{g['slug']}"
        elif kind == "title":
            title = g["slug"]
        elif kind == "space":
            mv = g["move"]
            if mv.startswith("="):
                space = int(mv[1:])
            elif space is not None:
                space = max(1, space + int(mv))
            if space is not None:
                spaces = max(spaces or 0, space)
        if afk:
            continue  # no moments while away (afk:start itself included)

        # label
        label = NOTHING
        j = i + 1
        while j < len(rows) and rows[j]["token"].startswith(EVENT_PREFIX):   # W24: an event is never a label
            j += 1
        if j < len(rows):
            nxt = rows[j]["token"]
            gap = (times[j] - t).total_seconds()
            if gap <= LABEL_WINDOW_S and not nxt.startswith("afk:"):
                label = nxt

        hist = []
        for j in range(i, max(-1, i - history_keep), -1):
            ago = (t - times[j]).total_seconds()
            dur = None
            if j != i and rows[j].get("dur") is not None:
                dur = min(float(rows[j]["dur"]), ago)
            hist.append({"token": rows[j]["token"], "ago_s": round(ago, 1),
                         "dur_s": None if dur is None else round(dur, 1)})

        summary = sorted(day_secs.items(), key=lambda kv: (-kv[1], kv[0]))[:DAY_SUMMARY_TOP]
        out.append({
            "ts": row["ts"],
            "label": label,
            "context": {
                "weekday": WEEKDAYS[local.weekday()],
                "hhmm": local.strftime("%H:%M"),
                "daypart": daypart(local.hour),
                "hour_bucket": hour_bucket(local.hour),
                "space": space,
                "spaces": spaces,
                "app": app,
                "url": url,
                "title": title,
                "token": tok,
            },
            "history": hist,
            "day_summary": [[a, round(s)] for a, s in summary if s > 0],
        })
    attach_wishes(out, wishes)
    _attach(out, pressed, "hud_pressed", lambda tok: int(tok.rsplit(":", 1)[1]))
    return out


def attach_wishes(moments: list[dict], wishes: list[dict]) -> None:
    """Set `wish` (the slug after `wish:`) on the moment preceding each wish (see module doc)."""
    _attach(moments, wishes, "wish", lambda tok: tok.split(":", 1)[1])


def _attach(moments: list[dict], feedback: list[dict], field: str, value) -> None:
    """Set `field` = value(token) on the last moment with ts <= each feedback row's ts (last one wins)."""
    if not moments:
        return
    m_times = [parse_ts(m["ts"]) for m in moments]
    j = -1
    for w in sorted(feedback, key=lambda r: r["ts"]):
        t = parse_ts(w["ts"])
        while j + 1 < len(moments) and m_times[j + 1] <= t:
            j += 1
        if j >= 0:
            moments[j][field] = value(w["token"])
