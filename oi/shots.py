"""W17: screenshot -> "send it to who I'm talking with".

The user: "when i screenshot that, i would have expected an action to send it to you via telegram because we
are currently talking. that would need some quality context so maybe a tier 2 action?"

1. oi-watcher posts every new screenshot (a file in the screenshot location, or a png/tiff image put on the
   clipboard) to the bucket `oi-watcher-shot_<host>` as {"kind": "shot", "src": "file"|"clipboard", "path"};
   oi.normalise turns it into the token `shot:new`.
2. ("context is state, not timers"): the screenshot is part of the CURRENT CONTEXT exactly
   while it is live: its floating thumbnail or markup window on screen (oi-watcher `shot_state` thumbnail live →
   gone, from the window list), a screenshot with no window (thumbnail off, or a clipboard one) until the context
   changes, a clipboard screenshot also until the clipboard changes (whichever comes first; seen
   2026-10-21, a copied screenshot stayed live 1h40 and came back on every summon). `oi live` runs `Shots`: a thread polls ONLY that bucket
   (0.5 s cadence) and keeps the live set. While a screenshot is live, Live's moment context has `live: ["shot"]`,
   its `shot:new` row is the latest action (Markov h1; Laya's state text says "live: shot"), and its options join
   the predictor's ONE pool (hint, ⌥⌘ summon, glide, long-press alike), ranked first by the same gate. It appearing
   shows them at once (the full cross, bound to the state: typing / pointer travel do not dismiss it, oi live
   takes it down when the state ends); Claude's content actions join the pool the moment they come. No timer
   decides relevance. Nothing is in the context while logging is paused, settings `shots.enabled` is false or the
   front app is denylisted (the screenshot may hold a secret).
3. The options: "→ <Agent>" (the current contact: oi.contact, else settings `shots.fallback_contact`), "→ other"
   (an agent picker, 3 per page + "More"), "Annotate" (the note panel, empty).
     tap "→ <Agent>"   = tier 1: the image + a context line (app, host, desk, Paris time);
     long-press it     = tier 2: on-device OCR (`oi-ocr`, Vision; the text never leaves this process except in
                         the prompt, never logged) + the context + the last actions -> haiku on the Team
                         subscription (oi.claude_actions.run_claude) writes a 3-5 line note ("what this shows /
                         what the user was doing / likely ask"), shown EDITABLE in the note panel; Enter sends.
   Nothing is ever sent without the user's tap (and Enter for a note). A send carries the NEWEST live screenshot:
   an option is bound to the screenshot live when it was shown, and one taken after it (while the cross or the
   note panel is up) is what the user means.
4. Delivery (`deliver`): atlas / juno (inject recipients on this Mac): copy the image into the channel's
   `inbox/` and append ONE line to its `inject` file: "[Screenshot from the user, HH:MM, context: …] <note> (image:
   <inbox path>)"; any other agent: the W12 relay command with the note and "image on the user's Mac at <path>"
   (moving the image to another host is a follow-up). Never an upload, never a cloud call for the pixels.
   OI_SHOT_DRY_RUN=1 (or `oi live --dry-run`) prints what would run and sends nothing.

5. Content actions: the
   offer above is shown at once; in parallel the screenshot is OCR'd (warm, on-device) and sonnet (the W25 context
   pack: app, host, desk, time, roster; + the OCR text, the last actions and the user's past picks) proposes 1-3
   next actions on existing primitives: mail.draft (a mailto draft, never sent), calendar.event (an .ics that
   Calendar asks to add), url.open, file.open / folder.open (drill.safe_path, never launchable), agent.send (the
   note panel, pre-filled for a roster agent: Enter sends) and copy (pbcopy). Every value is checked against the
   OCR text (a URL host, an address, a copied text, a file name must be read on the screenshot). When the answer
   comes while the screenshot is live, the cross still up is redrawn in place: "→ <Agent>" first, the content actions
   follow, then "→ other" / "Annotate" while keys remain. Claude down, slow, empty, a local-only host or an OCR
   text naming a local-only domain = the send options alone. The free model texts (agent.send message, mail.draft
   subject / body, calendar title / location, labels; not read on the screenshot) go through orch_pack.leaks after
   Claude answers: flagged = blanked, or the action dropped when that text is its point (scrub). settings
   `shots.actions` (default true). Every send these options lead to (→ <Agent>, → other, Annotate, agent.send)
   passes the outward gate (oi/gate.py, class "shot") in deliver(), right before delivery: the one confirmation.

hud-log rows {"kind": "shot"}: per press {"event": "press", "src", "to", "via", "outcome", "action", "picked": kind,
"tier", "ok", "dry_run", "error", "note_len", "ocr_ms", "claude": {model, latency_ms, ok}, "content": {type, slot},
"live", ["newer": true when the send carried a screenshot taken after the one offered]}; per screenshot leaving the context {"event": "gone", "reason", "slots": [kinds offered], "suggest": {model,
latency_ms, ok, n, ocr_ms, skipped, late}}: never a title, an OCR text, a note or an action's text. `slots` +
`picked` feed the next prompt (pick_history). The shows themselves are Live's regular rows.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import URLError

from . import aw, claude_actions, contact, drill, gate, hud_keys, live, normalise, settings, vocab, vocal
from .orchestrator import dispatch
from .orchestrator import pack as orch_pack

BUCKET_PREFIX = "oi-watcher-shot"
POLL_S = 0.5
BUCKETS_REFRESH_S = 30
SHOW_TTL_MS = 15000          # display setting (shots.ttl_ms): how long the cross stays, not how long the shot is live
NOTE_TIMEOUT_S = 600         # the note panel: the user may take his time
OCR_TIMEOUT_S = 15          # warm: ~0.1-0.2 s; the very first Vision call of a binary can take ~30 s (warm_ocr)
OCR_MAX = 4000
NOTE_MAX_LINES = 5
NOTE_LINE_MAX = 140
ACTIONS_KEEP = 8
ACTIONS_WINDOW_S = 600
PICKER_PAGE = 3
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".heic", ".tiff", ".tif")
TELEGRAM_APP = "/Applications/Telegram.app"
SHOT_DEFAULTS = {"enabled": True, "clipboard": True, "fallback_contact": "atlas", "ttl_ms": SHOW_TTL_MS, "actions": True}
SUGGEST_SAFETY_S = 90        # a safety bound on the Claude call only: its answer is used whenever it comes while live
P_LIVE = {"send": 1.0, "content": 0.95, "pick": 0.5, "annotate": 0.4}   # rank among the live options (all first)
OUTWARD_CONTENT = ("agent.send",)   # the content action that sends (gate.outward "shot" in deliver); mail.draft
                                    # only opens a draft the user sends himself in Mail (oi/gate.py: not outward)
FREE_TEXT = {"mail.draft": ("subject", "body"), "calendar.event": ("title", "location"), "agent.send": ("message",)}
NEEDED = {"calendar.event": "title", "agent.send": "message"}   # flagged = the action is dropped, not scrubbed
PAST_KEEP = 8                # ended screenshots a late press may still name (their image stays on disk)
ACTION_TYPES = ("mail.draft", "calendar.event", "url.open", "file.open", "folder.open", "agent.send", "copy")
ACTION_ICONS = {"mail.draft": "sf:square.and.pencil", "calendar.event": "sf:calendar.badge.plus", "url.open": "sf:safari",
                "file.open": "sf:doc", "folder.open": "sf:folder", "copy": "sf:doc.on.doc"}
ACTION_DEFAULT_LABEL = {"mail.draft": "Draft mail", "calendar.event": "Add to calendar", "url.open": "Open link",
                        "file.open": "Open file", "folder.open": "Open folder", "copy": "Copy text"}
ACTION_LABEL_MAX = 22
CONTENT_MAX = 3
HISTORY_DAYS = 7
EMAIL_RE = re.compile(r"^[^@\s<>\"]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
SHOW_EVENTS = ("pressed", "dismissed", "dropped", "error")
NOTE_EVENTS = ("note", "dismissed", "dropped", "error")


def log(msg: str) -> None:
    print(f"{vocab.iso_utc(datetime.now(timezone.utc))} oi-shots: {msg}", file=sys.stderr, flush=True)


# ---- settings ------------------------------------------------------------------------------------------

def shot_settings(raw: dict | None) -> dict:
    """settings.json `shots` (every key optional, wrongly typed = default):
    {"enabled": true, "clipboard": true, "fallback_contact": "atlas" | null, "ttl_ms": 15000, "actions": true}.
    `clipboard` is read by oi-watcher (clipboard images); null fallback_contact = no default contact."""
    out = dict(SHOT_DEFAULTS)
    s = (raw or {}).get("shots")
    if isinstance(s, dict):
        for k in ("enabled", "clipboard", "actions"):
            if isinstance(s.get(k), bool):
                out[k] = s[k]
        if "fallback_contact" in s and (s["fallback_contact"] is None or isinstance(s["fallback_contact"], str)):
            out["fallback_contact"] = s["fallback_contact"] or None
        v = s.get("ttl_ms")
        if isinstance(v, int) and not isinstance(v, bool) and 1500 <= v <= 60000:
            out["ttl_ms"] = v
    return out


# ---- options ---------------------------------------------------------------------------------------------

def agent_icon(name: str, telegram: bool) -> str:
    """The Telegram app icon for the current contact (the conversation happens there), else the initial."""
    if telegram and os.path.isdir(TELEGRAM_APP):
        return f"app:{TELEGRAM_APP}"
    return f"initial:{(name or '?')[0].upper()}"


def picker_options(table: dict[str, dict], page: int, exclude: str | None = None,
                   fill_order=None) -> tuple[list[dict], dict[int, str]]:
    """One picker page: up to 3 agents (initial icons) on the first 3 keys of `hud.fill_order` (default ⌃ 2, ⌥ 3,
    ⌘ 4) + "More" on the last one (fn 1) when another page follows (the last page wraps to the first).
    {key: slug | "more"}."""
    k = hud_keys.fill_keys(fill_order)
    slugs = [s for s in table if s != exclude]
    pages = max(1, -(-len(slugs) // PICKER_PAGE))
    page %= pages
    chunk = slugs[page * PICKER_PAGE:(page + 1) * PICKER_PAGE]
    opts = [{"key": k[i], "label": table[s]["name"], "icon": f"initial:{table[s]['name'][0].upper()}"}
            for i, s in enumerate(chunk)]
    keys: dict[int, str] = {k[i]: s for i, s in enumerate(chunk)}
    if pages > 1:
        opts.append({"key": k[3], "label": f"More {page + 1}/{pages}", "icon": "sf:ellipsis.circle"})
        keys[k[3]] = "more"
    return opts, keys


# ---- text ------------------------------------------------------------------------------------------------

def context_of(ctx: dict) -> dict:
    """{app, host, desk} of a moment context: the page HOST only (never a path or a title)."""
    url = ctx.get("url") if isinstance(ctx.get("url"), str) else None
    return {"app": ctx.get("app") if isinstance(ctx.get("app"), str) else None,
            "host": url.split("/", 1)[0] if url else None,
            "desk": ctx.get("space") if isinstance(ctx.get("space"), int) and not isinstance(ctx.get("space"), bool) else None}


def head_line(ctx: dict, when: datetime) -> str:
    """"[Screenshot from the user, 18:13, context: Safari · github.com · desk 2]" (Paris time)."""
    hhmm = when.astimezone(vocal.PARIS).strftime("%H:%M")
    parts = [p for p in (ctx.get("app"), ctx.get("host"), f"desk {ctx['desk']}" if ctx.get("desk") is not None else None) if p]
    return f"[Screenshot from the user, {hhmm}" + (f", context: {' · '.join(parts)}" if parts else "") + "]"


def one_line(note: str | None) -> str:
    """A note on ONE line (one inject line = one turn): its lines joined with " / ", capped."""
    if not isinstance(note, str):
        return ""
    lines = [" ".join(x.split()) for x in note.splitlines()]
    return " / ".join(x for x in lines if x)[:vocal.MAX_CHARS]


def message(ctx: dict, when: datetime, note: str | None, image_ref: str) -> str:
    body = one_line(note)
    return " ".join(f"{head_line(ctx, when)}{' ' + body if body else ''} {image_ref}".split())


def recent_actions(rows: list[dict], now: float, keep: int = ACTIONS_KEEP, window_s: float = ACTIONS_WINDOW_S) -> list[str]:
    """The last actions before `now` as "HH:MM token" (Paris), oldest first: app switches, pages (host/slug),
    desk moves, ⌘-chords. Never a window title."""
    out: list[str] = []
    for r in reversed(rows):
        try:
            t = vocab.parse_ts(r["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - t.timestamp() > window_s:
            break
        tok = r.get("token") or ""
        if tok.split(":", 1)[0] in ("activate", "url", "space", "key"):
            out.append(f"{t.astimezone(vocal.PARIS).strftime('%H:%M')} {tok}")
            if len(out) >= keep:
                break
    return out[::-1]


def note_prompt(agent: str, ctx: dict, when: datetime, actions: list[str], ocr_text: str, slug: str | None = None) -> str:
    pack_ctx = {"app": ctx.get("app"), "host": ctx.get("host"), "agent": slug,     # W25: the orchestrator's pack
                "space": ctx.get("desk") if isinstance(ctx.get("desk"), int) else None}
    return claude_actions.render_prompt("shot_note", {
        "agent": agent, "time": when.astimezone(vocal.PARIS).strftime("%H:%M"),
        "app": ctx.get("app") or "(unknown)", "host": ctx.get("host") or "(none)",
        "desk": ctx.get("desk") if ctx.get("desk") is not None else "(unknown)",
        "actions": "\n".join(actions) or "(none)", "ocr": (ocr_text or "")[:OCR_MAX] or "(no text read on the image)"},
        context=pack_ctx)


def note_lines(out: dict | None) -> str:
    """The model's {"lines": [...]} -> 3-5 clean lines joined with newlines ('' when unusable)."""
    lines = (out or {}).get("lines")
    if not isinstance(lines, list):
        return ""
    clean = [" ".join(x.split())[:NOTE_LINE_MAX] for x in lines if isinstance(x, str) and x.strip()]
    return "\n".join(clean[:NOTE_MAX_LINES])


# ---- content actions -----------------------------------------------------------------------------

def _flat(text: str | None) -> str:
    return " ".join((text or "").split()).lower()


def _text(v, n: int) -> str:
    return " ".join(v.split())[:n].strip() if isinstance(v, str) else ""


def _action_params(t: str, a: dict, flat: str, table: dict, exclude: str | None, now: float) -> dict | None:
    """The checked params of one proposed action, None when it does not hold (every value read on the screenshot)."""
    if t == "url.open":
        url = a.get("url").strip() if isinstance(a.get("url"), str) else ""
        if url and "://" not in url:
            url = "https://" + url
        href = live.safe_url(url)
        host = (href or "").split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower().removeprefix("www.")
        return {"url": href} if href and host and host in flat else None
    if t in ("file.open", "folder.open"):
        p = os.path.expanduser(a["path"].strip()) if isinstance(a.get("path"), str) else ""
        if (not drill.safe_path(p) or drill.launchable(p) or os.path.isdir(p) != (t == "folder.open")
                or os.path.basename(p.rstrip("/")).lower() not in flat):
            return None
        return {"path": p}
    if t == "mail.draft":
        to = a.get("to").strip() if isinstance(a.get("to"), str) else ""
        if to and (not EMAIL_RE.match(to) or to.lower() not in flat):
            return None
        body = a.get("body")[:2000].strip() if isinstance(a.get("body"), str) else ""
        subject = _text(a.get("subject"), 120)
        return {"to": to, "subject": subject, "body": body} if (to or subject or body) else None
    if t == "calendar.event":
        title = _text(a.get("title"), 120)
        try:
            start = datetime.strptime(str(a.get("start")), "%Y-%m-%dT%H:%M").replace(tzinfo=vocal.PARIS)
        except ValueError:
            return None
        m = a.get("minutes", 60)
        m = m if isinstance(m, int) and not isinstance(m, bool) and 5 <= m <= 720 else 60
        if not title or not -86400 <= start.timestamp() - now <= 400 * 86400:
            return None
        return {"title": title, "start": start.strftime("%Y-%m-%dT%H:%M"), "minutes": m,
                "location": _text(a.get("location"), 120)}
    if t == "agent.send":
        to = a.get("to")
        msg = a.get("message")[:500].strip() if isinstance(a.get("message"), str) else ""
        return {"to": to, "message": msg} if isinstance(to, str) and to in table and to != exclude and msg else None
    if t == "copy":
        text = a.get("text")[:500].strip() if isinstance(a.get("text"), str) else ""
        return {"text": text} if text and _flat(text) in flat else None
    return None


def scrub(t: str, p: dict, terms: list[str]) -> dict | None:
    """Privacy after Claude answers: a free model text (not grounded in the OCR) naming a local-only term
    (orch_pack.leaks) is blanked; the action is dropped when that text is what it is for, or nothing is left."""
    p = dict(p)
    for k in FREE_TEXT.get(t, ()):
        if p.get(k) and orch_pack.leaks(p[k], {}, terms):
            if NEEDED.get(t) == k:
                return None
            p[k] = ""
    if t == "mail.draft" and not (p["to"] or p["subject"] or p["body"]):
        return None
    return p


def clean_actions(out: dict | None, ocr_text: str, table: dict, *, exclude: str | None = None,
                  now: float | None = None, cfg: dict | None = None) -> list[dict]:
    """The model's {"actions": [...]} -> at most 3 checked {type, label, params} (one per type, `copy` aside).
    `cfg` = settings `claude` (local_only_domains) for the leak check of the free texts and labels."""
    items = (out or {}).get("actions")
    if not isinstance(items, list):
        return []
    flat, now = _flat(ocr_text), time.time() if now is None else now
    terms = orch_pack.deny_terms(cfg if cfg is not None else claude_actions.claude_settings())
    res: list[dict] = []
    for a in items:
        t = a.get("type") if isinstance(a, dict) else None
        if t not in ACTION_TYPES or (t != "copy" and any(r["type"] == t for r in res)):
            continue
        p = _action_params(t, a, flat, table, exclude, now)
        p = scrub(t, p, terms) if p is not None else None
        if p is None:
            continue
        label = _text(a.get("label"), ACTION_LABEL_MAX)
        label = "" if orch_pack.leaks(label, {}, terms) else label
        label = label or (
            f"Ask {table[p['to']]['name']}" if t == "agent.send" else ACTION_DEFAULT_LABEL[t])
        res.append({"type": t, "label": label, "params": p})
        if len(res) >= CONTENT_MAX:
            break
    return res


def content_option(key: int, item: dict, table: dict) -> dict:
    if item["type"] == "agent.send":
        icon = f"initial:{table[item['params']['to']]['name'][0].upper()}"
    else:
        icon = ACTION_ICONS[item["type"]]
    return {"key": key, "label": item["label"], "icon": icon}


def slot_kind(act: tuple) -> str:
    """What a key does, for the log: send / pick / annotate / <content type>."""
    return act[1]["type"] if act[0] == "content" else act[0]


def pick_history(now: float, days: int = HISTORY_DAYS) -> str:
    """"url.open picked 2 of 3 offers" per slot kind over the last `days` hud-log days ('' when none)."""
    offered: dict[str, int] = {}
    picked: dict[str, int] = {}
    for d in range(days):
        day = datetime.fromtimestamp(now - d * 86400, timezone.utc).date().isoformat()
        for r in normalise.read_hud_log(day):       # slots on the `gone` row, picked on the `press` rows
            if r.get("kind") != "shot" or r.get("dry_run"):
                continue
            for s in set(x for x in r.get("slots") or [] if isinstance(x, str)):
                offered[s] = offered.get(s, 0) + 1
            if isinstance(r.get("picked"), str):
                picked[r["picked"]] = picked.get(r["picked"], 0) + 1
    names = {"send": "send to the agent", "pick": "send to another agent", "annotate": "annotate"}
    parts = [f"{names.get(s, s)} picked {picked.get(s, 0)} of {n}" for s, n in sorted(offered.items(), key=lambda kv: (-kv[1], kv[0]))]
    return ("the user's picks on past screenshot offers (last 7 days): " + "; ".join(parts) + ".") if parts else ""


def actions_prompt(agent: str, slug: str | None, ctx: dict, when: datetime, actions: list[str], ocr_text: str,
                   history: str = "") -> str:
    pack_ctx = {"app": ctx.get("app"), "host": ctx.get("host"), "agent": slug,
                "space": ctx.get("desk") if isinstance(ctx.get("desk"), int) else None}
    local = when.astimezone(vocal.PARIS)
    return claude_actions.render_prompt("shot_actions", {
        "agent": agent, "slug": slug or "(none)", "today": local.strftime("%A %Y-%m-%d"), "time": local.strftime("%H:%M"),
        "app": ctx.get("app") or "(unknown)", "host": ctx.get("host") or "(none)",
        "desk": ctx.get("desk") if ctx.get("desk") is not None else "(unknown)",
        "actions": "\n".join(actions) or "(none)", "history": f"- {history}\n" if history else "",
        "ocr": (ocr_text or "")[:OCR_MAX]}, context=pack_ctx)


def ics_text(p: dict, now: float) -> str:
    """One VEVENT (Europe/Paris local time) for Calendar's add dialog."""
    start = datetime.strptime(p["start"], "%Y-%m-%dT%H:%M").replace(tzinfo=vocal.PARIS).astimezone(timezone.utc)
    end = datetime.fromtimestamp(start.timestamp() + p["minutes"] * 60, timezone.utc)

    def esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    fmt = "%Y%m%dT%H%M%SZ"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//os-intent//shot//EN", "BEGIN:VEVENT",
             f"UID:oi-shot-{secrets.token_hex(6)}@os-intent",
             f"DTSTAMP:{datetime.fromtimestamp(now, timezone.utc).strftime(fmt)}",
             f"DTSTART:{start.strftime(fmt)}", f"DTEND:{end.strftime(fmt)}", f"SUMMARY:{esc(p['title'])}"]
    if p.get("location"):
        lines.append(f"LOCATION:{esc(p['location'])}")
    return "\r\n".join(lines + ["END:VEVENT", "END:VCALENDAR", ""])


def write_ics(text: str) -> str:
    import tempfile
    d = tempfile.mkdtemp(prefix="oi-shot-ics-")
    path = os.path.join(d, "event.ics")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def action_command(item: dict, now: float, ics_writer: Callable[[str], str] = write_ics) -> tuple[list[str], bytes]:
    """(argv, stdin) for a content action other than agent.send."""
    t, p = item["type"], item["params"]
    if t == "url.open":
        return ["open", "-a", "Safari", p["url"]], b""
    if t in ("file.open", "folder.open"):
        return ["open", p["path"]], b""
    if t == "mail.draft":
        return ["open", live.mailto(p)], b""
    if t == "calendar.event":
        return ["open", "-a", "Calendar", ics_writer(ics_text(p, now))], b""
    if t == "copy":
        return ["pbcopy"], p["text"].encode("utf-8")
    raise ValueError(t)


# ---- OCR (on-device, Vision) -----------------------------------------------------------------------------

def ocr_bin(raw: dict | None = None) -> str | None:
    """$OI_OCR_BIN, else <repo_dir>/ocr/.build/release/oi-ocr, else `oi-ocr` on PATH."""
    env = os.environ.get("OI_OCR_BIN")
    if env:
        return env
    repo = (raw or {}).get("repo_dir") if isinstance((raw or {}).get("repo_dir"), str) else settings.DEFAULTS["repo_dir"]
    p = Path(repo) / "ocr/.build/release/oi-ocr"
    return str(p) if p.is_file() else shutil.which("oi-ocr")


def warm_rag() -> None:
    """W25: load the orchestrator's warm wiki index (~1 s) and Ollama's embedding model at `oi live` start,
    so the first context pack is not the slow one."""
    from . import rag
    try:
        w = rag.default()
        if w is not None:
            w.warm()
    except Exception:
        pass


def warm_ocr(binary: str | None) -> None:
    """Vision loads its text model on the first call of a binary (~30 s measured cold): `oi live` pays it at
    start on a rendered test image, so a tier-2 long-press stays fast."""
    if not binary:
        return
    import tempfile
    with tempfile.TemporaryDirectory(prefix="oi-ocr-warm-") as d:
        p = os.path.join(d, "warm.png")
        try:
            subprocess.run([binary, "--render", "warm up 42", p], capture_output=True, timeout=20)
            subprocess.run([binary, p], capture_output=True, timeout=90)
        except (OSError, subprocess.TimeoutExpired):
            pass


def run_ocr(image: str, binary: str | None, timeout: float = OCR_TIMEOUT_S) -> tuple[str, int]:
    """(recognised text, ms). '' on any failure. The text is returned only, never written or logged."""
    t0 = time.monotonic()
    if not binary:
        return "", 0
    try:
        p = subprocess.run([binary, image], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        text = p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        text = ""
    return text.strip()[:OCR_MAX], int((time.monotonic() - t0) * 1000)


# ---- delivery ------------------------------------------------------------------------------------------

def inbox_target(spec: dict, image: str, when: datetime) -> Path:
    """<channel dir>/inbox/oi-shot-<UTC stamp>-<4 hex><ext> (the channel dir = the inject file's dir)."""
    ext = Path(image).suffix.lower() if Path(image).suffix.lower() in IMAGE_EXT else ".png"
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(spec["path"]).parent / "inbox" / f"oi-shot-{stamp}-{secrets.token_hex(2)}{ext}"


def _copy_private(src: str, dst: Path) -> None:
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    dst.chmod(0o600)


def deliver(slug: str, image: str, note: str | None, *, ctx: dict, when: datetime, dry_run: bool = False,
            table: dict[str, dict] | None = None, runner: Callable = vocal._run,
            inject: Callable[[str, str], None] = vocal._append_inject,
            copy: Callable[[str, Path], None] = _copy_private, out=None,
            ask: Callable[[dict, float], dict] | None = None) -> dict:
    """Send the screenshot to ONE agent. {"ok", "to", ["name"], ["error"], ["dry_run"]}. The outward
    gate first: nothing is copied or sent (dry run included) unless the user validates."""
    table = table if table is not None else vocal.recipients(vocal._raw_settings())
    spec = table.get(slug)
    result: dict = {"ok": False, "to": slug}
    if spec is None:
        result["error"] = f"unknown recipient {slug}"
        return result
    result["name"] = spec["name"]
    if not os.path.isfile(image):
        result["error"] = "image missing"
        return result
    g = gate.outward("shot", slug, spec["name"], message(ctx, when, note, ""), dry_run=dry_run, ask=ask)
    if not g["ok"]:
        result["error"] = g["error"]
        return result
    if spec["kind"] == "inject":
        dst = inbox_target(spec, image, when)
        line = message(ctx, when, note, f"(image: {dst})")
        if dry_run:
            print(json.dumps({"kind": "inject", "copy": [image, str(dst)], "append_to": spec["path"],
                              "line": line}, ensure_ascii=False), file=out or sys.stdout, flush=True)
            result.update(ok=True, dry_run=True)
            return result
        if not Path(spec["path"]).is_file():
            result["error"] = "no live session (inject file missing)"   # nothing copied
            return result
        try:
            copy(image, dst)
            inject(spec["path"], line)
            result["ok"] = True
        except (OSError, vocal.VocalError) as e:
            result["error"] = str(e) if isinstance(e, vocal.VocalError) else f"inject: {e.strerror or e}"
        return result
    line = message(ctx, when, note, f"(image on the user's Mac at {image})")
    argv = vocal.command(spec)
    if dry_run:
        print(json.dumps({"kind": "a2a", "argv": argv, "env_unset": list(vocal.SCRUB_ENV), "stdin": line},
                         ensure_ascii=False), file=out or sys.stdout, flush=True)
        result.update(ok=True, dry_run=True)
        return result
    code, err = runner(argv, (line + "\n").encode("utf-8"))
    result["ok"] = code == 0
    if code != 0:
        result["error"] = f"exit {code}" + (f": {err}" if err else "")
    return result


# ---- HUD socket ----------------------------------------------------------------------------------------

def hud_request(path: Path, payload: dict, accept: tuple[str, ...], timeout: float) -> dict:
    """One command on a fresh connection; the first event in `accept` (broadcasts such as wish/summon are
    skipped). A socket problem = {"event": "dropped", "reason": "socket_error"}."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and ev.get("event") in accept:
                        return ev
    except (OSError, ValueError):
        return {"event": "dropped", "reason": "socket_error"}
    return {"event": "dropped", "reason": "no_reply"}


# ---- the live screenshot (context is state, not timers) ------------------------------------------

def shot_options(shot: dict, *, telegram_icon: bool = True) -> list[dict]:
    """The live screenshot's candidates for the predictor's ONE option pool: "→ <Agent>" (tap = tier 1, long-press
    = tier 2 when Claude is on), Claude's content actions once they came, "→ other", "Annotate" (with a contact).
    Each carries `live: "shot"` (the gate ranks the live state's options first), `shot` {id, act} (what a press
    does, run by Shots.press) and `outward` when its follow-up sends (deliver() asks gate.outward("shot") first)."""
    table, who = shot["table"], (shot.get("who") or {}).get("slug")
    out: list[dict] = []

    def add(prim: str, token: str, label: str, icon: str, act: tuple, *, claude: bool = False, outward: bool = False):
        out.append({"key": f"shot_{len(out) + 1}", "primitive": prim, "token": token, "params": {}, "label": label,
                    "icon": icon, "p_markov": P_LIVE[act[0]], "live": "shot", "shot": {"id": shot["id"], "act": act},
                    "claude": claude, "outward": outward})
    if who in table:
        add("shot.send", f"shot.send:{who}", f"→ {table[who]['name']}", agent_icon(table[who]["name"], telegram_icon),
            ("send", who), claude=bool(shot.get("claude")), outward=True)
    for i, c in enumerate(shot.get("content") or []):
        o = content_option(0, c, table)
        add("shot.content", f"shot.content:{c['type']}" + (f":{i}" if c["type"] == "copy" else ""), o["label"], o["icon"],
            ("content", c), outward=c["type"] in OUTWARD_CONTENT)
    add("shot.pick", "shot.pick", "→ other", "sf:person.2", ("pick",), outward=True)
    if who in table:
        add("shot.annotate", f"shot.annotate:{who}", "Annotate", "sf:square.and.pencil", ("annotate", who), outward=True)
    return out


class Shots:
    """Run by `oi live`: the screenshot as LIVE context state. A thread polls the shot bucket (0.5 s cadence) and
    keeps `live`: the screenshots on screen now. A screenshot is live while its thumbnail / markup window is up
    (watcher `shot_state` thumbnail live → gone), and a screenshot with no window (thumbnail off, or a clipboard
    one) until the context changes (a new app / page / desk token); a clipboard one ends too when the clipboard
    changes. A send carries the newest live screenshot (`latest`).
    While one is live, Live's context has `live: ["shot"]`, its `shot:new` token is the latest action of the
    moment, and `options()` joins the predictor's pool; Claude's content actions are added the moment they come.
    Collaborators injectable (tests, dry run)."""

    def __init__(self, live, *, request: Callable[[dict, tuple, float], dict] | None = None,
                 buckets: Callable[[], list[str]] = aw.list_buckets,
                 fetch: Callable[[str, str], list[dict]] = aw.fetch_events,
                 ocr: Callable[[str], tuple[str, int]] | None = None,
                 claude: Callable[[str, str], dict] | None = None,
                 deliver_fn: Callable[..., dict] = deliver, raw_settings: Callable[[], dict] = vocal._raw_settings,
                 clock: Callable[[], float] = time.time, out=None, suggest: Callable[[str, str], dict] | None = None,
                 runner: Callable[[list[str], bytes], tuple[int, str]] = vocal._run, actions: bool | None = None):
        self.live = live
        self.request = request or (lambda p, accept, timeout: hud_request(live.socket_path, p, accept, timeout))
        self.buckets_fn, self.fetch = buckets, fetch
        self.raw_settings = raw_settings
        self.ocr = ocr or (lambda image: run_ocr(image, ocr_bin(self.raw_settings())))
        self.claude = claude or claude_actions.run_claude
        self.suggest_fn = suggest or claude or (lambda p, m: claude_actions.run_claude(p, m, timeout=SUGGEST_SAFETY_S))
        self.runner = runner                       # content actions: (argv, stdin) -> (rc, err)
        self.actions_on = actions                  # None = settings `shots.actions`
        self.deliver_fn = deliver_fn
        self.clock = clock
        self.out = out
        self.since: str | None = None
        self.seen: dict = {}
        self._buckets: list[str] = []
        self._buckets_at = float("-inf")
        self.lock = threading.RLock()
        self.shots: dict[str, dict] = {}           # the live screenshots (id -> shot), oldest first
        self.past: dict[str, dict] = {}            # ended ones a press may still name (the image stays on disk)
        self.window = False                        # a screenshot thumbnail / markup window is on screen
        self.threads: list[threading.Thread] = []

    @property
    def dry_run(self) -> bool:
        return (bool(getattr(self.live, "dry_run", False)) or bool(getattr(self.live, "exec_dry_run", False))   # W15 --dry-exec
                or os.environ.get("OI_SHOT_DRY_RUN") == "1")

    # -- the bucket ----------------------------------------------------------------------------------------
    def prime(self) -> None:
        """Only events from now on (a restart never revives an old screenshot)."""
        self.since = vocab.iso_utc(datetime.fromtimestamp(self.clock(), timezone.utc))

    def new_events(self) -> list[dict]:
        now = self.clock()
        if self.since is None:
            self.prime()
        if now - self._buckets_at >= BUCKETS_REFRESH_S or not self._buckets:
            self._buckets_at = now
            self._buckets = [b for b in self.buckets_fn() if b.startswith(BUCKET_PREFIX)]
        fresh: list[dict] = []
        for b in self._buckets:
            for ev in self.fetch(b, self.since):
                k = (b, aw._key(ev))
                if k in self.seen:
                    continue
                self.seen[k] = now
                fresh.append(ev)
        if len(self.seen) > 500:   # bounded: keep the newest half
            for k, _ in sorted(self.seen.items(), key=lambda kv: kv[1])[:250]:
                del self.seen[k]
        fresh.sort(key=aw._sort_key)
        return fresh

    def poll(self) -> list[dict]:
        """One poll: apply each new event (live / gone / a new image). Returns the shots that appeared."""
        try:
            evs = self.new_events()
        except (URLError, OSError, ValueError):
            return []
        return [s for s in (self.apply(ev) for ev in evs) if s]

    def start(self) -> threading.Thread:
        self.prime()
        threading.Thread(target=lambda: warm_ocr(ocr_bin(self.raw_settings())), daemon=True, name="oi-ocr-warm").start()
        threading.Thread(target=warm_rag, daemon=True, name="oi-rag-warm").start()     # W25

        def work():
            while True:
                try:
                    self.poll()
                except Exception:
                    log("poll failed: " + traceback.format_exc().splitlines()[-1])
                time.sleep(POLL_S)
        t = threading.Thread(target=work, daemon=True, name="oi-shots")
        t.start()
        return t

    # -- the live state --------------------------------------------------------------------------------------
    def apply(self, ev: dict) -> dict | None:
        """One bucket event. shot_state thumbnail live: the window is up (a window-less live shot now lives with
        it); gone: every window-bound shot ends; clipboard gone: the clipboard shot ends. shot: a new live
        screenshot (returned)."""
        data = ev.get("data") or {}
        kind, src = data.get("kind"), data.get("src")
        if kind == "shot_state":
            ended = []
            with self.lock:
                if src == "thumbnail" and data.get("state") == "live":
                    self.window = True
                    for s in self.shots.values():
                        if not s["clip"]:
                            s["window"] = True
                elif src == "thumbnail" and data.get("state") == "gone":
                    self.window = False
                    ended = [s for s in self.shots.values() if s["window"]]
                elif src == "clipboard" and data.get("state") == "gone":
                    ended = [s for s in self.shots.values() if s["clip"]]
            for s in ended:
                self.end(s, "window_gone" if src == "thumbnail" else "clipboard_changed")
            return None
        if kind != "shot":
            return None
        return self.appear(ev, data)

    def appear(self, ev: dict, data: dict) -> dict | None:
        image = data.get("path")
        if not isinstance(image, str) or not os.path.isfile(image):
            return None
        st = settings.load()
        raw = self.raw_settings()
        cfg = shot_settings(raw)
        if st["paused"] or not cfg["enabled"]:
            return None
        mctx = self.current()
        if mctx.get("app") and normalise._denied(mctx["app"], normalise.effective_denylist()):
            log("screenshot while a denylisted app is in front: not in the context")
            return None
        now = self.clock()
        table = vocal.recipients(raw)
        who = self.contact_for(raw, table, cfg, now)
        claude_on = bool(claude_actions.claude_settings().get("enabled"))
        tok = token_row(ev)
        shot = {"id": f"{tok['ts']}|{aw._key(ev)}", "image": image, "src": data.get("src") or "file",
                "token": tok, "ctx": context_of(mctx), "when": datetime.fromtimestamp(now, timezone.utc), "now": now,
                "table": table, "who": who, "claude": claude_on, "ttl_ms": cfg["ttl_ms"],
                "telegram": bool(who and who["via"] == "telegram"), "clip": data.get("src") == "clipboard",
                "content": None, "suggest": None, "slots": set(), "ended": False,
                "after": newest_context(list(getattr(self.live, "rows", []) or []))}
        with self.lock:
            shot["window"] = self.window and not shot["clip"]
            self.shots[shot["id"]] = shot
        log(f"screenshot ({shot['src']}) live" + (" (window)" if shot["window"] else "")
            + f": contact {who['slug'] if who else '-'} ({who['via'] if who else '-'})")
        self.notify("appear", shot)                   # shown at once; Claude's content actions join when they come
        if claude_on and st["hud_enabled"] and (cfg["actions"] if self.actions_on is None else self.actions_on):
            t = threading.Thread(target=self._suggest, args=(shot,), daemon=True, name="oi-shot-actions")
            self.threads = [x for x in self.threads if x.is_alive()] + [t]
            t.start()
        return shot

    def end(self, shot: dict, reason: str) -> None:
        with self.lock:
            if shot["ended"]:
                return
            shot["ended"] = True
            self.shots.pop(shot["id"], None)
            self.past[shot["id"]] = shot
            while len(self.past) > PAST_KEEP:
                self.past.pop(next(iter(self.past)))
        who = shot.get("who") or {}
        vocal.log_row({"ts": vocab.iso_utc(datetime.fromtimestamp(self.clock(), timezone.utc)), "kind": "shot",
                       "event": "gone", "reason": reason, "src": shot["src"], "to": who.get("slug"), "via": who.get("via"),
                       "slots": sorted(shot["slots"]), "suggest": shot.get("suggest"), "dry_run": self.dry_run})
        log(f"screenshot gone ({reason})")
        self.notify("gone", shot)

    def on_context(self, rows: list[dict]) -> None:
        """Live's loop, after each pull: a screenshot with no window (thumbnail off, or a clipboard one) is the
        current context until the context changes (a newer app / page / desk token). (2026-10-21,
        "end on app/page change"): a clipboard one ends here too, not only when the clipboard changes; never while
        a thumbnail / markup window is up."""
        newest = newest_context(rows)
        with self.lock:
            ended = [s for s in self.shots.values() if not s["window"] and not (s["clip"] and self.window)
                     and newest is not None and (s["after"] is None or newest > s["after"])
                     and newest[0] >= s["token"]["ts"]]
        for s in ended:
            self.end(s, "context_changed")

    def notify(self, what: str, shot: dict) -> None:
        cb = getattr(self.live, "on_live_state", None)
        if callable(cb):
            try:
                cb(what, shot)
            except Exception:
                log(f"live {what} failed: " + traceback.format_exc().splitlines()[-1])

    def live_set(self) -> set[str]:
        with self.lock:
            return {"shot"} if (self.window or self.shots) else set()

    def newest(self) -> dict | None:
        with self.lock:
            return next(reversed(self.shots.values()), None)

    def token_rows(self) -> list[dict]:
        """The live screenshots' `shot:new` rows (Live merges the ones its 2 s pull has not brought yet)."""
        with self.lock:
            return [dict(s["token"]) for s in self.shots.values()]

    def options(self) -> list[dict]:
        """The newest live screenshot's candidates ([] when none)."""
        s = self.newest()
        return shot_options(s, telegram_icon=s["telegram"]) if s else []

    def context_line(self) -> str | None:
        """The glide's "Events" context while a screenshot is live (W24's field): no OCR text, no path."""
        s = self.newest()
        if not s:
            return None
        hhmm = s["when"].astimezone(vocal.PARIS).strftime("%H:%M")
        where = " · ".join(str(p) for p in (s["ctx"].get("app"), s["ctx"].get("host")) if p)
        return f"{hhmm} screenshot on screen now (not sent yet){', taken in ' + where if where else ''}"

    def offered(self, opts: list[dict]) -> None:
        """Live.dispatch: these shot options were shown (the `gone` row lists the slot kinds offered)."""
        with self.lock:
            for o in opts:
                sh = o.get("shot")
                s = sh and (self.shots.get(sh["id"]) or self.past.get(sh["id"]))
                if s:
                    s["slots"].add(slot_kind(sh["act"]))

    def latest(self, shot: dict) -> dict:
        """The screenshot a send carries: the newest live one when it appeared after `shot` (an option is bound to
        the screenshot live when it was shown; one taken while that cross or the note panel was up is the one
        the user means), else `shot`."""
        with self.lock:
            n = next(reversed(self.shots.values()), None)
        return n if n is not None and n is not shot and n["now"] > shot["now"] else shot

    def get(self, sid: str) -> dict | None:
        with self.lock:
            return self.shots.get(sid) or self.past.get(sid)

    # -- context -------------------------------------------------------------------------------------------
    def current(self) -> dict:
        """The moment context ({} when nothing is known)."""
        try:
            m = self.live.current_moment()
        except Exception:
            m = None
        return (m or {}).get("context") or {}

    def contact_for(self, raw: dict, table: dict, cfg: dict, now: float) -> dict | None:
        armed = dispatch.armed(now)                  # W25: "pass me to X" names the next screenshot's agent
        if armed in table:
            return {"slug": armed, "via": "dispatch", "ts": None}
        rows = list(getattr(self.live, "rows", []) or [])
        rows += [{"ts": r["ts"], "token": f"vocal:{r['to']}"} for r in self.sent_rows(now)]   # a shot sent = a contact
        rows.sort(key=lambda r: r["ts"])
        c = contact.resolve(rows, now, raw_settings=raw, known=set(table))
        if c:
            return c
        fb = cfg.get("fallback_contact")
        return {"slug": fb, "via": "default", "ts": None} if fb in table else None

    def sent_rows(self, now: float) -> list[dict]:
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        return [r for r in normalise.read_hud_log(day)
                if r.get("kind") == "shot" and r.get("ok") is True and not r.get("dry_run")
                and isinstance(r.get("to"), str) and isinstance(r.get("ts"), str)]

    # -- content actions (Claude) ------------------------------------------------------------------------------
    def _suggest(self, shot: dict) -> None:
        try:
            content = self.suggest(shot)
        except Exception:
            shot.setdefault("suggest", {})["error"] = "exception"
            log("content actions failed: " + traceback.format_exc().splitlines()[-1])
            return
        with self.lock:
            if shot["ended"]:
                shot["suggest"]["late"] = True       # the screenshot left the context first: dropped
                return
            shot["content"] = content
        if content:
            self.notify("content", shot)

    def suggest(self, shot: dict) -> list[dict]:
        """OCR (on-device) + the context -> sonnet -> checked content actions ([] = the send options alone)."""
        info: dict = {"model": "sonnet", "ok": False, "n": 0}
        shot["suggest"] = info
        ccfg = claude_actions.claude_settings()
        host = shot["ctx"].get("host")
        if host and claude_actions.local_only(host, ccfg):
            info["skipped"] = "local_only"
            return []
        text, info["ocr_ms"] = self.ocr(shot["image"])            # the text stays in memory only
        if not text.strip():
            info["skipped"] = "no_text"
            return []
        if orch_pack.leaks(text, ccfg):
            info["skipped"] = "local_only_text"
            return []
        who = (shot.get("who") or {}).get("slug")
        name = shot["table"][who]["name"] if who in shot["table"] else "an agent he picks"
        prompt = actions_prompt(name, who, shot["ctx"], shot["when"],
                                recent_actions(list(getattr(self.live, "rows", []) or []), shot["now"]), text,
                                pick_history(shot["now"]))
        t0 = time.monotonic()
        try:
            out = self.suggest_fn(prompt, "sonnet")
        except claude_actions.ClaudeUnavailable as e:
            out, info["error"] = None, str(e)[:40]
        del prompt
        content = clean_actions(out, text, shot["table"], exclude=who, now=shot["now"], cfg=ccfg)
        del text
        info.update(latency_ms=int((time.monotonic() - t0) * 1000), ok=bool(content), n=len(content))
        log(f"content actions: {[c['type'] for c in content]} in {info['latency_ms']} ms")
        return content

    # -- a press on a shot option (Live.on_reply) -----------------------------------------------------------
    def press(self, opt: dict, reply: dict) -> str | None:
        """Run what the pressed shot option does; one hud-log row {"kind": "shot", "event": "press", ...}.
        The executed token, or None (cancelled, failed, unknown screenshot)."""
        sh = opt.get("shot") or {}
        shot, act = self.get(sh.get("id")), sh.get("act")
        if not shot or not act:
            return None
        who = shot.get("who") or {}
        row = {"ts": vocab.iso_utc(datetime.fromtimestamp(self.clock(), timezone.utc)), "kind": "shot", "event": "press",
               "src": shot["src"], "to": who.get("slug"), "via": who.get("via"), "outcome": f"pressed:{reply.get('key')}",
               "action": act[0], "picked": slot_kind(act), "tier": None, "ok": False, "dry_run": self.dry_run,
               "error": None, "live": not shot["ended"]}
        try:
            self._press(act, reply, shot, row)
        except Exception:
            row["error"] = "exception"
            log("shot press failed: " + traceback.format_exc().splitlines()[-1])
        vocal.log_row(row)
        return opt.get("token") if row["ok"] else None

    def _press(self, act: tuple, reply: dict, shot: dict, row: dict) -> None:
        if act[0] == "content":
            items = shot.get("content") or []
            row["content"] = {"type": act[1]["type"], "slot": next((i for i, c in enumerate(items) if c is act[1]), None)}
            self.run_content(act[1], shot, row)
        elif act[0] == "send" and reply.get("mode") == "claude":
            self.tier2(act[1], reply.get("key"), shot, row)
        elif act[0] == "send":
            self.send(act[1], None, shot, row, tier=1)
        elif act[0] == "pick":
            to = self.pick(shot)
            if to:
                row["to"], row["via"] = to, "picked"
                self.send(to, None, shot, row, tier=1)
        elif act[0] == "annotate":
            name = shot["table"][act[1]]["name"]
            text = self.note(f"→ {name} · your note", "", "What should go with the screenshot?")
            if text is None:
                row["outcome"] += ":note_cancelled"
                return
            self.send(act[1], text, shot, row, tier=1)

    def run_content(self, item: dict, shot: dict, row: dict) -> None:
        """One content action. agent.send = the note panel pre-filled (Enter sends); the others run at once."""
        row["tier"] = 2
        if item["type"] == "agent.send":
            to = item["params"]["to"]
            name = shot["table"][to]["name"]
            text = self.note(f"→ {name} · Claude note", item["params"]["message"], "Enter sends the screenshot")
            if text is None:
                row["outcome"] += ":note_cancelled"
                return
            self.send(to, text, shot, row, tier=2)
            return
        argv, data = action_command(item, shot["now"])
        if self.dry_run:
            print(json.dumps({"shot_action": item["type"], "argv": argv}, ensure_ascii=False),
                  file=self.out or sys.stdout, flush=True)
            row["ok"] = True
            return
        code, err = self.runner(argv, data)
        row["ok"], row["error"] = code == 0, (None if code == 0 else f"exit {code}")
        log(f"content action {item['type']}: " + ("done" if code == 0 else f"FAILED (exit {code})"))

    def pick(self, shot: dict) -> str | None:
        exclude = (shot.get("who") or {}).get("slug")
        fill = (settings.load().get("hud") or {}).get("fill_order")
        page = 0
        for _ in range(12):
            opts, keys = picker_options(shot["table"], page, exclude=exclude, fill_order=fill)
            reply = self.request({"cmd": "shot_show", "options": opts, "ttl_ms": shot["ttl_ms"]},
                                 SHOW_EVENTS, shot["ttl_ms"] / 1000 + 30)
            if reply.get("event") != "pressed":
                return None
            v = keys.get(reply.get("key"))
            if v != "more":
                return v
            page += 1
        return None

    def note(self, title: str, text: str, placeholder: str) -> str | None:
        """The editable note panel; the text the user sent with Enter, or None (Esc, click outside, no HUD)."""
        reply = self.request({"cmd": "shot_note", "title": title, "text": text, "placeholder": placeholder},
                             NOTE_EVENTS, NOTE_TIMEOUT_S)
        if reply.get("event") != "note" or not isinstance(reply.get("text"), str):
            return None
        return reply["text"]

    def tier2(self, to: str, key: int, shot: dict, row: dict) -> None:
        """OCR + context -> sonnet note -> the HUD's spinner released -> the note panel (editable) -> send."""
        row["tier"] = 2
        shot = self.latest(shot)                               # the note describes the image that will go
        name = shot["table"][to]["name"]
        ccfg = claude_actions.claude_settings()
        host = shot["ctx"].get("host")
        note, hint = "", "Claude unavailable: Enter sends the screenshot with the context line"
        if host and claude_actions.local_only(host, ccfg):
            hint = "Local-only site: no Claude note. Enter sends the screenshot with the context line"
        elif ccfg.get("enabled"):
            text, row["ocr_ms"] = self.ocr(shot["image"])      # on-device; the text stays in memory only
            prompt = note_prompt(name, shot["ctx"], shot["when"],
                                 recent_actions(list(getattr(self.live, "rows", []) or []), shot["now"]), text, slug=to)
            t0 = time.monotonic()
            try:
                out, ok = self.claude(prompt, "sonnet"), True
            except claude_actions.ClaudeUnavailable:
                out, ok = None, False
            del prompt, text                                   # the OCR text is not kept
            note = note_lines(out)
            if orch_pack.leaks(note, ccfg):                    # privacy: a model note naming a local-only term
                note, hint = "", "Claude note withheld (local-only): Enter sends the screenshot with the context line"
            row["claude"] = {"model": "sonnet", "latency_ms": int((time.monotonic() - t0) * 1000), "ok": ok and bool(note)}
        r = self.request({"cmd": "result", "key": key, "ok": bool(note), "text": "", "ttl_ms": 1000}, ("ok", "dropped", "error"), 5)
        if r.get("event") != "ok":
            row["outcome"] += ":cancelled"                   # Esc while Claude worked: nothing more
            return
        edited = self.note(f"→ {name} · Claude note", note, hint)
        if edited is None:
            row["outcome"] += ":note_cancelled"
            return
        self.send(to, edited, shot, row, tier=2)

    def send(self, to: str, note: str | None, shot: dict, row: dict, *, tier: int) -> dict:
        row["tier"], row["to"] = tier, to
        cur = self.latest(shot)                                # the newest live screenshot goes
        if cur is not shot:
            log("send: a newer screenshot is live, it goes instead of the one offered")
            shot, row["src"], row["newer"] = cur, cur["src"], True
        row["note_len"] = len(one_line(note))
        r = self.deliver_fn(to, shot["image"], note, ctx=shot["ctx"], when=shot["when"], dry_run=self.dry_run,
                            table=shot["table"], out=self.out)
        row["ok"], row["error"] = r.get("ok", False), r.get("error")
        log(f"tier {tier} -> {to}: " + ("dry run, nothing sent" if r.get("dry_run") else ("sent" if r.get("ok") else f"FAILED ({r.get('error')})")))
        return r


def token_row(ev: dict) -> dict:
    """The `shot:new` token row of a shot event, exactly as oi.normalise builds it from raw/ (same ts)."""
    e = normalise.Ev.of(ev)
    return {"ts": vocab.iso_utc(e.ts), "dur": 0.0, "token": "shot:new", "app": None, "src": "oi-shot", "raw_id": e.raw_id}


def newest_context(rows: list[dict]) -> tuple[str, str] | None:
    """(ts, token) of the newest app / page / desk token (the context the user is in)."""
    for r in reversed(rows):
        tok = r.get("token") or ""
        if tok.split(":", 1)[0] in ("activate", "url", "space"):
            return (r["ts"], tok)
    return None


# ---- CLI: `oi shot-send` -------------------------------------------------------------------------------

def main_send(args) -> int:
    """`oi shot-send --to <slug> --image <png> [--note-file f] [--app A --host H --desk k] [--dry-run]`: one
    delivery by hand (tests, demo). The note is read from a file, never argv."""
    note = None
    if args.note_file:
        try:
            note = Path(args.note_file).read_text(encoding="utf-8")
        except OSError as e:
            print(json.dumps({"ok": False, "to": args.to, "error": f"note file: {e.strerror}"}))
            return 1
    ctx = {"app": args.app, "host": args.host, "desk": args.desk}
    r = deliver(args.to, args.image, note, ctx=ctx, when=datetime.now(timezone.utc), dry_run=args.dry_run)
    vocal.log_row({"ts": vocab.iso_utc(datetime.now(timezone.utc)), "kind": "shot", "src": "cli", "to": args.to,
                   "via": "cli", "outcome": "cli", "action": "send", "tier": 1, "ok": r["ok"],
                   "dry_run": args.dry_run, "error": r.get("error"), "note_len": len(one_line(note))})
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r["ok"] else 1
