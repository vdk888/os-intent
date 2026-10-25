"""Token vocabulary: the contract shared by W1 (normaliser), W2 (watcher) and W3 (markov/forms).

Source of truth: docs/DESIGN.md "Token vocabulary". One JSONL row per token:
    {"ts": ISO-8601 UTC, "dur": seconds, "token": str, "app": str|null,
     "src": "aw-window|aw-afk|oi-space|oi-key|oi-safari|oi-wish", "raw_id": int|null}

`wish:<slug30>` (W6) is what the user said they wanted, typed or dictated in the oi-hud wish
panel: src "oi-wish", dur 0. It is feedback, not an action: moments.py never makes it a moment
or a label, it attaches it to the preceding moment as `wish`.

`hud:shown`, `hud:pressed:<k>`, `hud:dismissed` (W8), `hud:glide:<depth>` (W15) are what the live driver (`oi live`) showed on
the HUD and what the user did with it, read from hud-log/<day>.jsonl: src "oi-hud", dur 0. Like wishes they
are feedback: never a moment, never a label; a `pressed` attaches to the preceding moment as `hud_pressed`.

`vocal:<slug>` (W12): the user sent a voice note to that agent from the HUD's "Send a vocal to…" option
(hud-log row {"kind": "vocal", "ok": true}; dry runs and failures give nothing): src "oi-hud", dur 0.
Feedback too: never a moment, never a label, never a Markov primitive.

`click:<host>/<role>:<label-slug>` (W16): the user clicked a button / link in Safari's page (oi-watcher at
mouse-up, the AX element under the pointer: its label only, never page text or field values): src "oi-click".
An ACTION like `key:`: a moment, a label and a Markov primitive (frequent buttons become `ui.press` options).
`shot:new` (W17): oi-watcher saw a new screenshot (bucket oi-watcher-shot_<host>), src "oi-shot", dur 0.
An ordinary action token (a moment, a label); the screenshot's path stays in the raw row, never in the token.
`evt:<source>:<class>` (W24): the state changed without the user acting (a mail, an agent message, a file, a
cockpit alert, a metric, a market move), from oi/proactive (events/<day>.jsonl), src "oi-evt", dur 0. A CONTEXT
token: a moment and a history token (Markov learns what follows it), never a label and never a primitive.
`evt:<source>:<class>:gone` = that state ended (the event leaves the context; no time window).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

SLUG_MAX = 30
SRCS = frozenset({"aw-window", "aw-afk", "oi-space", "oi-key", "oi-safari", "oi-wish", "oi-hud", "oi-click",  # W16: oi-click
                  "oi-shot", "oi-evt"})   # W17: oi-shot, W24: oi-evt
MODIFIERS = "⌘⌃⌥⇧"

_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_APP = r"\S(?:[^\n]*\S)?"

GRAMMAR: dict[str, re.Pattern[str]] = {
    "activate": re.compile(rf"^activate:(?P<app>{_APP})$"),
    "title": re.compile(rf"^title:(?P<app>{_APP})/(?P<slug>{_SLUG})$"),
    "url": re.compile(rf"^url:(?P<host>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::\d+)?)/(?P<slug>{_SLUG})$"),
    "space": re.compile(r"^space:(?P<move>[+-]1|=\d+)$"),
    # ⌘-chords only: one or more modifiers including ⌘, then one key name.
    "key": re.compile(rf"^key:(?P<chord>(?=[{MODIFIERS}]*⌘)[{MODIFIERS}]+[^\s{MODIFIERS}]+)$"),
    "afk": re.compile(r"^afk:(?P<state>start|end)$"),
    # W6: the wish quick action (slug of the wished-for action text, <= 30 chars).
    "wish": re.compile(rf"^wish:(?P<slug>{_SLUG})$"),
    # W8: what the live driver showed on the HUD and the outcome (src oi-hud).
    # W15: `hud:glide:<depth>` = a ⌥⌘ glide ended (pressed or cancelled) at that sub-ring depth (0-3).
    "hud": re.compile(r"^hud:(?P<event>shown|dismissed|pressed:(?P<k>\d+)|glide:(?P<depth>[0-3]))$"),
    # W12: a voice note sent to a fleet agent (slug = the recipient, src oi-hud).
    "vocal": re.compile(rf"^vocal:(?P<slug>{_SLUG})$"),
    # W16: a click on a page button / link in Safari (label slug only).
    "click": re.compile(rf"^click:(?P<host>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::\d+)?)/(?P<role>button|link):(?P<slug>{_SLUG})$"),
    # W17: a new screenshot (a file in the screenshot location or an image on the clipboard), src oi-shot.
    "shot": re.compile(r"^shot:(?P<event>new)$"),
    # W24: a proactive event (source + class only; never a name, subject or path).
    # `:gone` = that state ended (mail read / replied, chat read, alert closed …): context is state, not timers.
    "evt": re.compile(rf"^evt:(?P<source>mail|agent|file|cockpit|metric|market|calendar|github):(?P<cls>{_SLUG})(?::(?P<end>gone))?$"),
    # Reserved for later waves (docs/DESIGN.md "later").
    "clip": re.compile(r"^clip:(?P<event>changed)$"),
}

KINDS = tuple(GRAMMAR)


def parse(token: str) -> tuple[str, dict[str, str]]:
    """Return (kind, payload groups). Raises ValueError if the token is not in the grammar."""
    if not isinstance(token, str):
        raise ValueError("token must be a str")
    kind = token.split(":", 1)[0]
    pat = GRAMMAR.get(kind)
    m = pat.match(token) if pat else None
    if not m:
        raise ValueError(f"invalid token: {token!r}")
    payload = {k: v for k, v in m.groupdict().items() if v is not None}
    if len(payload.get("slug", "")) > SLUG_MAX:
        raise ValueError(f"slug longer than {SLUG_MAX}: {token!r}")
    return kind, payload


def validate(token: str) -> bool:
    try:
        parse(token)
    except ValueError:
        return False
    return True


def slug(text: str | None, n: int = SLUG_MAX) -> str:
    """Lower-case ASCII slug with '-' separators, at most n chars; '' when nothing survives."""
    if not text:
        return ""
    ascii_ = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")
    return s[:n].rstrip("-")


class TokenRow(TypedDict):
    ts: str
    dur: float
    token: str
    app: str | None
    src: str
    raw_id: int | None


ROW_KEYS = frozenset(TokenRow.__annotations__)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp: {ts!r}")
    return dt


def validate_row(row: Any) -> list[str]:
    """Return the list of problems with a token row ([] means valid)."""
    if not isinstance(row, dict):
        return ["row is not an object"]
    errs: list[str] = []
    if set(row) != ROW_KEYS:
        errs.append(f"keys {sorted(row)} != {sorted(ROW_KEYS)}")
    ts = row.get("ts")
    try:
        if not isinstance(ts, str) or parse_ts(ts).utcoffset() != timedelta(0):
            errs.append("ts must be an ISO-8601 UTC string")
    except ValueError:
        errs.append("ts not ISO-8601")
    dur = row.get("dur")
    if isinstance(dur, bool) or not isinstance(dur, (int, float)) or dur < 0:
        errs.append("dur must be a number >= 0")
    if not validate(row.get("token")):
        errs.append("token not in grammar")
    if row.get("app") is not None and not isinstance(row.get("app"), str):
        errs.append("app must be str or null")
    if row.get("src") not in SRCS:
        errs.append("src unknown")
    rid = row.get("raw_id")
    if rid is not None and (isinstance(rid, bool) or not isinstance(rid, int)):
        errs.append("raw_id must be int or null")
    return errs
