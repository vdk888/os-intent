"""Raw ActivityWatch / oi-watcher rows -> action tokens (docs/DESIGN.md "Token vocabulary").

Rebuilding a day is idempotent: tokens/<day>.jsonl is regenerated from raw/ every
time. The neighbouring days are read as context so app runs, afk state and flicker
are judged correctly across midnight; only tokens whose ts falls in the day are kept.
"""
from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import aw, page_actions, paths, settings
from .proactive import store as evt_store   # W24
from .vocab import TokenRow, iso_utc, parse_ts, slug, validate

TITLE_APPS = frozenset({"Notion", "Mail", "Terminal", "Claude", "Telegram"})
DENYLIST = frozenset({"Passwords", "Keychain Access", "1Password"})  # built-in, always applied


def effective_denylist(extra=None) -> frozenset[str]:
    """Built-in DENYLIST ∪ `denylist_apps` from settings.json (oi-menubar edits it) ∪ `extra`."""
    return DENYLIST | frozenset(settings.denylist_apps()) | frozenset(extra or ())
FLICKER_S = 1.0

# bucket-name prefix -> src (custom buckets may carry a _<host> suffix)
SRC_PREFIXES = (
    ("aw-watcher-window", "aw-window"),
    ("aw-watcher-afk", "aw-afk"),
    ("oi-watcher-space", "oi-space"),
    ("oi-watcher-key", "oi-key"),
    ("oi-watcher-safari", "oi-safari"),
    ("oi-watcher-click", "oi-click"),     # W16
    ("oi-watcher-shot", "oi-shot"),          # W17
)


def src_of(bucket: str) -> str | None:
    for prefix, src in SRC_PREFIXES:
        if bucket.startswith(prefix):
            return src
    return None


@dataclass
class Ev:
    ts: datetime
    end: datetime
    data: dict
    raw_id: int | None

    @classmethod
    def of(cls, row: dict) -> "Ev":
        ts = parse_ts(row["timestamp"]).astimezone(timezone.utc)
        return cls(ts, ts + timedelta(seconds=float(row.get("duration") or 0)), row.get("data") or {}, row.get("id"))


@dataclass
class Run:
    app: str
    events: list[Ev] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.events[0].ts

    @property
    def end(self) -> datetime:
        return max(e.end for e in self.events)


def _row(ts: datetime, end: datetime, token: str, app: str | None, src: str, raw_id: int | None) -> TokenRow:
    return TokenRow(ts=iso_utc(ts), dur=round(max((end - ts).total_seconds(), 0.0), 3),
                    token=token, app=app, src=src, raw_id=raw_id)


def _denied(app: str, denylist: frozenset[str]) -> bool:
    return app.casefold() in {d.casefold() for d in denylist}


def app_runs(events: list[Ev], flicker_s: float = FLICKER_S) -> list[Run]:
    runs: list[Run] = []
    for e in events:
        app = (e.data.get("app") or "").strip()
        if not app:
            continue
        if runs and runs[-1].app == app:
            runs[-1].events.append(e)
        else:
            runs.append(Run(app, [e]))
    # Flicker: A -> B (B < 1 s) -> A. Drop B and the return to A (both activates).
    i = 1
    while i < len(runs) - 1:
        prev, mid, nxt = runs[i - 1], runs[i], runs[i + 1]
        if prev.app == nxt.app and (nxt.start - mid.start).total_seconds() < flicker_s:
            prev.events.extend(nxt.events)
            del runs[i:i + 2]
            i = max(i - 1, 1)
        else:
            i += 1
    return runs


def window_tokens(events: list[Ev], title_apps=TITLE_APPS, denylist=DENYLIST) -> list[TokenRow]:
    out: list[TokenRow] = []
    runs = app_runs(events)
    for k, run in enumerate(runs):
        run_end = runs[k + 1].start if k + 1 < len(runs) else run.end
        tok = f"activate:{run.app}"
        if not validate(tok):
            continue
        out.append(_row(run.start, run_end, tok, run.app, "aw-window", run.events[0].raw_id))
        if run.app not in title_apps or _denied(run.app, denylist):
            continue
        titles: list[tuple[Ev, str]] = []
        for e in run.events:
            s = slug(e.data.get("title"))
            if s and (not titles or titles[-1][1] != s):
                titles.append((e, s))
        for j, (e, s) in enumerate(titles):
            end = titles[j + 1][0].ts if j + 1 < len(titles) else run_end
            out.append(_row(e.ts, end, f"title:{run.app}/{s}", run.app, "aw-window", e.raw_id))
    return out


def afk_tokens(events: list[Ev]) -> list[TokenRow]:
    out: list[TokenRow] = []
    prev: str | None = None
    for e in events:
        status = e.data.get("status")
        if status not in ("afk", "not-afk") or status == prev:
            continue
        if status == "afk":
            out.append(_row(e.ts, e.end, "afk:start", None, "aw-afk", e.raw_id))
        elif prev == "afk":
            out.append(_row(e.ts, e.end, "afk:end", None, "aw-afk", e.raw_id))
        prev = status
    return out


def url_token(data: dict) -> str | None:
    if data.get("incognito") or data.get("private"):
        return None
    try:
        u = urllib.parse.urlsplit(data.get("url") or "")
        host = (u.hostname or "").lower()
        port = f":{u.port}" if u.port else ""
    except ValueError:
        return None
    if not host:
        return None
    seg = next((p for p in u.path.split("/") if p), "")
    s = slug(seg) or slug(data.get("title")) or "untitled"
    tok = f"url:{host}{port}/{s}"
    return tok if validate(tok) else None


def safari_tokens(events: list[Ev], denylist=DENYLIST) -> list[TokenRow]:
    if _denied("Safari", denylist):
        return []  # Safari denylisted: activate:Safari only, no url tokens
    pending: list[tuple[Ev, str]] = []
    for e in events:
        tok = url_token(e.data)
        if tok and (not pending or pending[-1][1] != tok):
            pending.append((e, tok))
    return [_row(e.ts, pending[j + 1][0].ts if j + 1 < len(pending) else e.end, tok, "Safari", "oi-safari", e.raw_id)
            for j, (e, tok) in enumerate(pending)]


def space_tokens(events: list[Ev]) -> list[TokenRow]:
    out: list[TokenRow] = []
    for e in events:
        delta, space = e.data.get("delta"), e.data.get("space")
        if delta in (1, -1):
            tok = f"space:{delta:+d}"
        elif isinstance(space, int) and not isinstance(space, bool) and space >= 0:
            tok = f"space:={space}"
        else:
            continue
        out.append(_row(e.ts, e.end, tok, None, "oi-space", e.raw_id))
    return out


def key_tokens(events: list[Ev], denylist=DENYLIST) -> list[TokenRow]:
    out: list[TokenRow] = []
    for e in events:
        if _denied(e.data.get("app") or "", denylist):
            continue  # a denylisted app yields activate:<App> only
        tok = f"key:{e.data.get('chord') or ''}"
        if validate(tok):  # ⌘-chords only; anything else is dropped
            out.append(_row(e.ts, e.end, tok, e.data.get("app"), "oi-key", e.raw_id))
    return out


def click_tokens(events: list[Ev], denylist=DENYLIST) -> list[TokenRow]:
    """W16: oi-watcher click rows {"host","role","label"} -> `click:<host>/<role>:<label-slug>`
    (app Safari, src oi-click). Safari denylisted -> nothing; a row that does not validate is dropped. A click on a
    destructive button (close, delete, cancel, supprimer, ...: page_actions.DESTRUCTIVE) is dropped too: it never
    becomes a moment, a label or a Markov catalogue entry, so it is never learnt (incident 2026-10-21)."""
    if _denied("Safari", denylist):
        return []
    out: list[TokenRow] = []
    for e in events:
        host, role = str(e.data.get("host") or "").lower(), e.data.get("role")
        tok = f"click:{host}/{role}:{slug(e.data.get('label'))}"
        if host and validate(tok) and not page_actions.destructive_token(tok):
            out.append(_row(e.ts, e.end, tok, "Safari", "oi-click", e.raw_id))
    return out
def shot_tokens(events: list[Ev]) -> list[TokenRow]:
    """W17: oi-watcher screenshot events ({"kind": "shot", "src", "path"}) -> `shot:new` (the path stays raw)."""
    return [_row(e.ts, e.ts, "shot:new", None, "oi-shot", e.raw_id) for e in events if e.data.get("kind") == "shot"]


def wish_tokens(rows: list[dict]) -> list[TokenRow]:
    """oi-hud wish rows ({"ts","text","source","context":{"app",...}}) -> `wish:<slug30>` tokens,
    src "oi-wish", dur 0, at the wish's ts. Rows without a usable ts or text are skipped."""
    out: list[TokenRow] = []
    for r in rows:
        try:
            ts = parse_ts(r["ts"]).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        text = r.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        tok = f"wish:{slug(text) or 'untitled'}"
        act = r.get("action") if isinstance(r.get("action"), dict) else None   # W18: a resolved wish = a strong
        if act and isinstance(act.get("target"), str) and slug(act["target"]):   # label: its slug names the target
            tok = f"wish:{slug(act['target'])}"
        if not validate(tok):
            continue
        ctx = r.get("context") if isinstance(r.get("context"), dict) else {}
        app = ctx.get("app") if isinstance(ctx.get("app"), str) else None
        out.append(_row(ts, ts, tok, app, "oi-wish", None))
    return out


def read_wishes(day: str) -> list[dict]:
    """Raw wish rows of wishes/<day>.jsonl (missing file -> []; a corrupt line is skipped)."""
    return _read_rows(paths.wishes_dir() / f"{day}.jsonl")


# W21b: how a hud-log row numbers its options. 1 = by rank (rows before W21, written without the field),
# 2 = by key (W21: fn 1, ⌃ 2, ⌥ 3, ⌘ 4; `pressed:<k>` and options[].key name the key).
HUD_NUMBERING = 2


def hud_numbering(row: dict) -> int:
    """A hud-log row's numbering: its `numbering` field, 1 when absent (rows written before W21)."""
    n = row.get("numbering")
    return n if isinstance(n, int) and not isinstance(n, bool) else 1


def read_hud_log(day: str) -> list[dict]:
    """Rows of hud-log/<day>.jsonl written by `oi live` (missing file -> []; corrupt lines skipped)."""
    return _read_rows(paths.hud_log_dir() / f"{day}.jsonl")


def hud_tokens(rows: list[dict]) -> list[TokenRow]:
    """hud-log rows -> `hud:shown` at the show ts, then `hud:pressed:<k>` / `hud:dismissed` at
    ts + latency_ms (src "oi-hud", dur 0), W15 + `hud:glide:<depth>` for a glide. Rows that were not shown (gate, dry run, HUD dropped) give nothing."""
    out: list[TokenRow] = []
    for r in rows:
        if r.get("kind") == "vocal":                      # W12: a sent voice note -> vocal:<slug>
            out.extend(vocal_tokens([r]))
            continue
        if r.get("shown") is not True:
            continue
        try:
            ts = parse_ts(r["ts"]).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        out.append(_row(ts, ts, "hud:shown", None, "oi-hud", None))
        outcome = r.get("outcome") if isinstance(r.get("outcome"), str) else ""
        lat = r.get("latency_ms")
        t2 = ts + timedelta(milliseconds=lat if isinstance(lat, (int, float)) and lat >= 0 else 0)
        tok = None
        if outcome.startswith("pressed:"):
            # a press that executed a destructive page button is never positive feedback (no `hud_pressed`,
            # so no x3 repeat in laya-train): incident 2026-10-21, "Close PR" pressed by mistake
            if page_actions.destructive_token(r.get("executed") if isinstance(r.get("executed"), str) else None):
                continue
            tok = f"hud:pressed:{outcome.split(':', 1)[1]}"
        elif outcome.startswith("dismissed"):
            tok = "hud:dismissed"
        if tok and validate(tok):
            out.append(_row(t2, t2, tok, None, "oi-hud", None))
        g = r.get("glide")                                 # W15: the glide's depth (training signal)
        if isinstance(g, dict) and isinstance(g.get("depth"), int) and validate(f"hud:glide:{g['depth']}"):
            out.append(_row(t2, t2, f"hud:glide:{g['depth']}", None, "oi-hud", None))
    return out


# ---- regretted page presses (incident 2026-10-21) ----------------------------------------------------------
# A page press (a HUD `ui.press` of a `click:` option, or a mouse click on a page button) followed by an UNDO within
# UNDO_WINDOW_S is a mistake: its token is dropped from the stream, so it is never learnt (no moment, no label, no
# `hud_pressed`). Undo = ⌘Z, or a click on a button / link whose label carries an undo word (Reopen pull request,
# Restore branch, Undo, Rouvrir, Restaurer, Rétablir).
UNDO_WINDOW_S = 300
UNDO_WORDS = frozenset({"undo", "reopen", "restore", "undelete", "rouvrir", "restaurer", "retablir"})
UNDO_KEYS = frozenset({"key:⌘Z", "key:⌘z"})


def is_undo(token: str) -> bool:
    if token in UNDO_KEYS:
        return True
    if token.startswith("click:"):
        return any(w in UNDO_WORDS for w in token.rsplit(":", 1)[-1].split("-"))
    return False


def page_press_times(rows: list[dict]) -> set[str]:
    """The `hud:pressed:<k>` token ts (same arithmetic as hud_tokens) of hud-log rows that executed a page press."""
    out: set[str] = set()
    for r in rows:
        ex = r.get("executed")
        if r.get("shown") is not True or not isinstance(ex, str) or not ex.startswith("click:"):
            continue
        if not str(r.get("outcome") or "").startswith("pressed:"):
            continue
        try:
            ts = parse_ts(r["ts"]).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        lat = r.get("latency_ms")
        out.add(iso_utc(ts + timedelta(milliseconds=lat if isinstance(lat, (int, float)) and lat >= 0 else 0)))
    return out


def drop_undone(rows: list[TokenRow], page_presses: set[str] | None = None,
                window_s: float = UNDO_WINDOW_S) -> list[TokenRow]:
    """`rows` (sorted by ts) without the page presses undone within `window_s`: a `click:` token, or a
    `hud:pressed:<k>` whose ts is in `page_presses`, followed by an undo token (is_undo) in that window."""
    undos = [parse_ts(r["ts"]) for r in rows if is_undo(r["token"])]
    if not undos:
        return rows
    presses = page_presses or set()
    out: list[TokenRow] = []
    for r in rows:
        tok = r["token"]
        if (tok.startswith("click:") and not is_undo(tok)) or (tok.startswith("hud:pressed:") and r["ts"] in presses):
            t = parse_ts(r["ts"])
            if any(0 < (u - t).total_seconds() <= window_s for u in undos):
                continue
        out.append(r)
    return out


def vocal_tokens(rows: list[dict]) -> list[TokenRow]:
    """hud-log rows {"kind":"vocal","to":slug,"ok":true} -> `vocal:<slug>` (src "oi-hud", dur 0). A failed
    attempt or a dry run gives nothing."""
    out: list[TokenRow] = []
    for r in rows:
        if r.get("kind") != "vocal" or r.get("ok") is not True or r.get("dry_run"):
            continue
        try:
            ts = parse_ts(r["ts"]).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        tok = f"vocal:{slug(r.get('to') if isinstance(r.get('to'), str) else '')}"
        if validate(tok):
            out.append(_row(ts, ts, tok, None, "oi-hud", None))
    return out


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


BUILDERS = {
    "aw-window": window_tokens,
    "aw-afk": afk_tokens,
    "oi-safari": safari_tokens,
    "oi-space": space_tokens,
    "oi-key": key_tokens,
    "oi-click": click_tokens,             # W16
    "oi-shot": shot_tokens,                  # W17
}


def tokens_from_raw(raw: dict[str, list[dict]], title_apps=TITLE_APPS, denylist=None) -> list[TokenRow]:
    """All buckets -> one token stream sorted by time (stable across sources).
    `denylist=None` means effective_denylist() (built-in ∪ settings.json)."""
    if denylist is None:
        denylist = effective_denylist()
    by_src: dict[str, list[Ev]] = {}
    for bucket, rows in raw.items():
        src = src_of(bucket)
        if src:
            by_src.setdefault(src, []).extend(Ev.of(r) for r in rows)
    out: list[TokenRow] = []
    for src, build in BUILDERS.items():
        evs = sorted(by_src.get(src, []), key=lambda e: (e.ts, e.raw_id or 0))
        if src == "aw-window":
            out.extend(build(evs, title_apps, denylist))
        elif src in ("oi-key", "oi-safari", "oi-click"):
            out.extend(build(evs, denylist))
        else:
            out.extend(build(evs))
    out.sort(key=lambda r: r["ts"])
    return out


def build_day(day: str, title_apps=TITLE_APPS, denylist=None) -> tuple[Path, list[TokenRow]]:
    d = date.fromisoformat(day)
    merged: dict[str, dict] = {}
    for ctx in (d - timedelta(days=1), d, d + timedelta(days=1)):
        for bucket, rows in aw.raw_rows(ctx.isoformat()).items():
            seen = merged.setdefault(bucket, {})
            for r in rows:  # a clipped copy may sit in a neighbour day's file: merge by id
                k = aw._key(r)
                seen[k] = aw.merge(seen.get(k), r)
    raw = {b: list(rows.values()) for b, rows in merged.items()}
    rows = tokens_from_raw(raw, title_apps, denylist)
    # wishes and HUD outcomes: the file of the day plus its neighbours (a row is filed by its UTC write day)
    presses: set[str] = set()
    for ctx in (d - timedelta(days=1), d, d + timedelta(days=1)):
        rows.extend(wish_tokens(read_wishes(ctx.isoformat())))
        hud = read_hud_log(ctx.isoformat())
        rows.extend(hud_tokens(hud))
        presses |= page_press_times(hud)
        rows.extend(evt_store.tokens(evt_store.read_day(ctx.isoformat())))   # W24: proactive events
    rows.sort(key=lambda r: r["ts"])
    rows = drop_undone(rows, presses)                                         # regretted page presses: never learnt
    rows = [r for r in rows if r["ts"].startswith(day)]
    path = paths.tokens_dir() / f"{day}.jsonl"
    aw.write_jsonl(path, rows)
    return path, rows
