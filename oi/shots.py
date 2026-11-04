"""screenshot -> "send it to who I'm talking with".

Goal: after a screenshot, offer to send it to the agent you are currently talking with, with enough context
(a tier-2 action when a note is useful).

1. oi-watcher posts every new screenshot (a file in the screenshot location, or a png/tiff image put on the
   clipboard) to the bucket `oi-watcher-shot_<host>` as {"kind": "shot", "src": "file"|"clipboard", "path"};
   oi.normalise turns it into the token `shot:new`.
2. ("context is state, not timers"): the screenshot is part of the CURRENT CONTEXT exactly
   while it is live: its floating thumbnail or markup window on screen (oi-watcher `shot_state` thumbnail live →
   gone, from the window list: being taken, no image yet), then the screenshot FILE (macOS writes it as the
   thumbnail slides away, so the thumbnail's gone never ends it) until the user sends it, the next screenshot replaces
   it, the file is gone or the context changes (never while a thumbnail / markup window is up), a clipboard
   screenshot also until the clipboard changes (whichever comes first: a copied screenshot once stayed live
   1h40 and came back on every summon). `oi live` runs `Shots`: a thread polls ONLY that bucket
   (0.5 s cadence) and keeps the live set. While a screenshot is live, Live's moment context has `live: ["shot"]`,
   its `shot:new` row is the latest action (Markov h1; Laya's state text says "live: shot"), and its options join
   the predictor's ONE pool (hint, ⌥⌘ summon, glide, long-press alike), ranked first by the same gate. It appearing
   shows them at once (the full cross, bound to the state: typing / pointer travel do not dismiss it, oi live
   takes it down when the state ends); Claude's content actions join the pool the moment they come. No timer
   decides relevance. Nothing is in the context while logging is paused, settings `shots.enabled` is false or the
   front app is denylisted (the screenshot may hold a secret).
3. The options: "→ <Agent>" (the current contact: oi.contact, else settings `shots.fallback_contact`), "→ other"
   (an agent picker, 3 per page + "More"), "Annotate" (the note panel, empty).
     tap "→ <Agent>" = tier 1: the image + a context line (app, host, desk, Paris time);
     long-press it     = tier 2: on-device OCR (`oi-ocr`, Vision; the text never leaves this process except in
                         the prompt, never logged) + the context + the last actions -> haiku on the Team
                         subscription (oi.claude_actions.run_claude) writes a 3-5 line note ("what this shows /
                         what the user was doing / likely ask"), shown EDITABLE in the note panel; Enter sends.
   Nothing is ever sent without the user's tap (and Enter for a note). A send carries the NEWEST live screenshot:
   an option is bound to the screenshot live when it was shown, and one taken after it (while the cross or the
   note panel is up) is what the user means (an older one going out instead is the bug this prevents).
4. Delivery (`deliver`): atlas / juno (inject recipients on this Mac): copy the image into the channel's
   `inbox/` and append ONE line to its `inject` file: "[Screenshot from the user, HH:MM, context: …] <note> (image:
   <inbox path>)"; any other agent: the signed A2A sender with the note and the image's path on ITS host
   (see 6; "image on the user's Mac at <path>" when `shots.remote_image` is off). Never an upload, never a cloud call for the pixels.
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
   passes the outward gate (oi/gate.py, class "shot") in deliver, right before delivery: the one confirmation.

6. picker first (a screenshot should offer at once to forward it to someone on the team, with a typed or dictated
   note; settings `shots.picker_first`, default true): a new screenshot opens the TEAM PICKER at once instead of the cross: every
   recipient of vocal.recipients, the current contact on the first key, 3 per page + "More" (fn), one tap. The pick
   opens the note panel ("→ Nova · your note"): type, or Tab / the mic button for oi-hud's on-device dictation
   (settings `shots.note_dictation`, every note panel; the transcript lands in the field, editable; the line then
   says "note dictated"); Enter = ONE delivery (image + note; empty = the image alone), Esc / a click outside / no
   pick = nothing sent. Claude's content actions never replace the picker: they wait in the ⌥⌘ cross. Off = the
   cross as in 3.
   Remote image (settings `shots.remote_image`, default true): an A2A recipient gets the image copied first, over its
   ssh route (`ssh <to>`, the recipient's `to` host; log in as the agent's own user there), bytes on stdin, into <its channel state dir>/inbox/oi-shot-<stamp>.png (0600), and
   the line names that path; a failed copy sends nothing. Still after the outward gate, still no cloud upload.

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
import stat
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import URLError

from . import aw, claude_actions, contact, drill, gate, health, hud_keys, live, normalise, settings, vocab, vocal
from .orchestrator import dispatch
from .orchestrator import pack as orch_pack

BUCKET_PREFIX = "oi-watcher-shot"
POLL_S = 0.5
REFRESH_S = 30           # each shot bucket re-fetched at least this often, even when its signature did not move
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
SHOT_DEFAULTS = {"enabled": True, "clipboard": True, "fallback_contact": None, "ttl_ms": SHOW_TTL_MS, "actions": True,
                 "picker_first": True, "note_dictation": True, "remote_image": True}   # the last three: section 6
REMOTE_COPY_TIMEOUT_S = 60   # the image over ssh (a few MB on a private network)
SUGGEST_SAFETY_S = 90        # a safety bound on the Claude call only: its answer is used whenever it comes while live
P_LIVE = {"send": 1.0, "content": 0.95, "pick": 0.5, "annotate": 0.4}   # rank among the live options (all first)
OUTWARD_CONTENT = ("agent.send",)   # the content action that sends (gate.outward "shot" in deliver); mail.draft
                                    # only opens a draft the user sends himself in Mail (oi/gate.py: not outward)
FREE_TEXT = {"mail.draft": ("subject", "body"), "calendar.event": ("title", "location"), "agent.send": ("message",)}
NEEDED = {"calendar.event": "title", "agent.send": "message"}   # flagged = the action is dropped, not scrubbed
PROBE_S = 60                 # oi live re-probes the screenshot folder (a TCC grant / revoke shows in oi health)
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
    {"enabled": true, "clipboard": true, "fallback_contact": "<agent slug>" | null (default), "ttl_ms": 15000, "actions": true,
     "picker_first": true, "note_dictation": true, "remote_image": true}.
    `clipboard` is read by oi-watcher (clipboard images); null fallback_contact = no default contact. 
    `picker_first` = a new screenshot opens the team picker then the note panel (false: the cross as before),
    `note_dictation` = the note panels offer dictation (Tab / mic), `remote_image` = an agent on another host gets
    the image copied into its channel inbox over ssh (false: only the Mac path, as before)."""
    out = dict(SHOT_DEFAULTS)
    s = (raw or {}).get("shots")
    if isinstance(s, dict):
        for k in ("enabled", "clipboard", "actions", "picker_first", "note_dictation", "remote_image"):
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


def roster(table: dict[str, dict], first: str | None) -> dict[str, dict]:
    """every recipient, `first` (the current contact) at the top, the rest in the table's order."""
    if first not in table:
        return dict(table)
    return {first: table[first], **{k: v for k, v in table.items() if k != first}}


# ---- text ------------------------------------------------------------------------------------------------

def context_of(ctx: dict) -> dict:
    """{app, host, desk} of a moment context: the page HOST only (never a path or a title)."""
    url = ctx.get("url") if isinstance(ctx.get("url"), str) else None
    return {"app": ctx.get("app") if isinstance(ctx.get("app"), str) else None,
            "host": url.split("/", 1)[0] if url else None,
            "desk": ctx.get("space") if isinstance(ctx.get("space"), int) and not isinstance(ctx.get("space"), bool) else None}


def head_line(ctx: dict, when: datetime, dictated: bool = False) -> str:
    """"[Screenshot from the user, 18:13, context: Safari · github.com · desk 2]" (Paris time); ", note dictated"
    when on-device dictation wrote the note (the recipient never takes a transcript for typed words)."""
    hhmm = when.astimezone(vocal.PARIS).strftime("%H:%M")
    parts = [p for p in (ctx.get("app"), ctx.get("host"), f"desk {ctx['desk']}" if ctx.get("desk") is not None else None) if p]
    return (f"[Screenshot from the user, {hhmm}" + (f", context: {' · '.join(parts)}" if parts else "")
            + (", note dictated" if dictated else "") + "]")


def one_line(note: str | None) -> str:
    """A note on ONE line (one inject line = one turn): its lines joined with " / ", capped."""
    if not isinstance(note, str):
        return ""
    lines = [" ".join(x.split()) for x in note.splitlines()]
    return " / ".join(x for x in lines if x)[:vocal.MAX_CHARS]


def message(ctx: dict, when: datetime, note: str | None, image_ref: str, dictated: bool = False) -> str:
    body = one_line(note)
    return " ".join(f"{head_line(ctx, when, dictated and bool(body))}{' ' + body if body else ''} {image_ref}".split())


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
    pack_ctx = {"app": ctx.get("app"), "host": ctx.get("host"), "agent": slug,     # the orchestrator's pack
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
    """load the orchestrator's warm wiki index (~1 s) and Ollama's embedding model at `oi live` start,
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


def file_state(path: str) -> str:
    """"present", "gone" (no such file) or "unreadable" (any other stat error: TCC's EPERM on a denied
    folder is NOT a missing file; os.path.isfile says False for both)."""
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "gone"
    except OSError:
        return "unreadable"
    return "present" if stat.S_ISREG(st.st_mode) else "gone"


def remote_inbox_target(spec: dict, image: str, when: datetime) -> str:
    """<the agent's channel dir on its host>/inbox/oi-shot-<UTC stamp>-<4 hex><ext> (the A2A state dir; a state dir
    that already is an `inbox` is used as is)."""
    ext = Path(image).suffix.lower() if Path(image).suffix.lower() in IMAGE_EXT else ".png"
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    inbox = spec["state_dir"].rstrip("/")
    if not inbox.endswith("/inbox"):
        inbox += "/inbox"
    return f"{inbox}/oi-shot-{stamp}-{secrets.token_hex(2)}{ext}"


def remote_copy_command(spec: dict, dst: str) -> list[str]:
    """The image onto the agent's host over the A2A recipient's ssh route (`to`), the bytes on STDIN (never in
    argv): `ssh <to> '…'` creates the inbox if needed (umask 077), writes a NEW file (set -C: never over an existing
    one) and makes it 0600. Point `to` at an ssh route that logs in as the agent's own user (never root), so the
    file is the agent's. Nothing else runs there."""
    import shlex
    d, f = shlex.quote(dst.rsplit("/", 1)[0]), shlex.quote(dst)
    script = f"umask 077; set -C; mkdir -p {d} && cat > {f} && chmod 600 {f}"
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", spec["to"], script]


def _run_copy(argv: list[str], data: bytes) -> tuple[int, str]:
    """The ssh copy (env scrubbed like the A2A sender), its own timeout (an image, not a line)."""
    env = {k: v for k, v in os.environ.items() if k not in vocal.SCRUB_ENV}
    try:
        p = subprocess.run(argv, input=data, capture_output=True, timeout=REMOTE_COPY_TIMEOUT_S, env=env)
    except FileNotFoundError:
        return 127, "ssh not found"
    except subprocess.TimeoutExpired:
        return 124, f"timeout {REMOTE_COPY_TIMEOUT_S}s"
    err = p.stderr.decode("utf-8", "replace").strip().splitlines()
    return p.returncode, (err[-1][:120] if err else "")


def _copy_private(src: str, dst: Path) -> None:
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    dst.chmod(0o600)


def deliver(slug: str, image: str, note: str | None, *, ctx: dict, when: datetime, dry_run: bool = False,
            table: dict[str, dict] | None = None, runner: Callable = vocal._run,
            inject: Callable[[str, str], None] = vocal._append_inject,
            copy: Callable[[str, Path], None] = _copy_private, out=None,
            ask: Callable[[dict, float], dict] | None = None, dictated: bool = False,
            remote_image: bool | None = None,
            remote_copy: Callable[[list[str], bytes], tuple[int, str]] | None = None) -> dict:
    """Send the screenshot to ONE agent. {"ok", "to", ["name"], ["error"], ["dry_run"]}. The outward
    gate first: nothing is copied or sent (dry run included) unless the user validates.
    an A2A recipient gets the image copied into its channel inbox on its host (`remote_copy`, ssh) before
    the line, which names that path; a failed copy sends nothing. `remote_image` None = settings `shots.remote_image`."""
    table = table if table is not None else vocal.recipients(vocal._raw_settings())
    spec = table.get(slug)
    result: dict = {"ok": False, "to": slug}
    if spec is None:
        result["error"] = f"unknown recipient {slug}"
        return result
    result["name"] = spec["name"]
    fs = file_state(image)
    if fs == "gone":
        result["error"] = "image missing"
        return result
    if fs == "unreadable":                     # present, but this process may not stat it (TCC)
        result.update(error="read: Operation not permitted", denied=True)
        return result
    g = gate.outward("shot", slug, spec["name"], message(ctx, when, note, "", dictated), dry_run=dry_run, ask=ask)
    if not g["ok"]:
        result["error"] = g["error"]
        return result
    if spec["kind"] == "inject":
        dst = inbox_target(spec, image, when)
        line = message(ctx, when, note, f"(image: {dst})", dictated)
        if dry_run:
            print(json.dumps({"kind": "inject", "copy": [image, str(dst)], "append_to": spec["path"],
                              "line": line}, ensure_ascii=False), file=out or sys.stdout, flush=True)
            result.update(ok=True, dry_run=True)
            return result
        if not Path(spec["path"]).is_file():
            result["error"] = "no live session (inject file missing)"   # nothing copied
            return result
        try:                                   # the failing step is named (copy: / inject:)
            copy(image, dst)
        except OSError as e:
            result["error"] = f"copy: {e.strerror or e}"
            result["denied"] = isinstance(e, PermissionError)   # TCC: launchd python denied the screenshot folder
            return result
        try:
            inject(spec["path"], line)
            result["ok"] = True
        except (OSError, vocal.VocalError) as e:
            result["error"] = str(e) if isinstance(e, vocal.VocalError) else f"inject: {e.strerror or e}"
        return result
    if remote_image is None:
        remote_image = shot_settings(vocal._raw_settings())["remote_image"]
    plan: dict = {}
    if remote_image:                           # the pixels go to the agent's host, over its ssh route
        rdst = remote_inbox_target(spec, image, when)
        cargv = remote_copy_command(spec, rdst)
        line = message(ctx, when, note, f"(image: {rdst})", dictated)
        plan["copy"] = {"argv": cargv, "stdin": f"<{image}>"}
    else:
        line = message(ctx, when, note, f"(image on the user's Mac at {image})", dictated)
    argv = vocal.command(spec)
    if dry_run:
        print(json.dumps({"kind": "a2a", **plan, "argv": argv, "env_unset": list(vocal.SCRUB_ENV), "stdin": line},
                         ensure_ascii=False), file=out or sys.stdout, flush=True)
        result.update(ok=True, dry_run=True)
        return result
    if remote_image:
        try:
            data = Path(image).read_bytes()
        except OSError as e:
            result["error"] = f"copy: {e.strerror or e}"
            result["denied"] = isinstance(e, PermissionError)
            return result
        code, err = (remote_copy or _run_copy)(cargv, data)
        del data
        if code != 0:                          # nothing half-sent: no line naming an image that is not there
            result["error"] = f"copy: exit {code}" + (f": {err}" if err else "")
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
    keeps `live`: a thumbnail / markup window up (watcher `shot_state`: a screenshot being taken) and the screenshots
    taken. A file screenshot lives with its file, not its thumbnail: until it is sent (`answered`), the next
    one lands (`replaced`), the file is gone or the context changes (a new app / page / desk token, not while a
    window is up); a clipboard one ends too when the clipboard changes. A failed send is said on the HUD
    (`notice`). A send carries the newest live screenshot (`latest`).
    While one is live, Live's context has `live: ["shot"]`, its `shot:new` token is the latest action of the
    moment, and `options()` joins the predictor's pool; Claude's content actions are added the moment they come.
    Collaborators injectable (tests, dry run)."""

    def __init__(self, live, *, request: Callable[[dict, tuple, float], dict] | None = None,
                 buckets: Callable[[], dict | list[str]] = aw.bucket_meta,
                 fetch: Callable[[str, str], list[dict]] = lambda b, s: aw.fetch_events(b, s, timeout=aw.TIMEOUT_S),
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
        self.gate = aw.Gate(REFRESH_S, clock=clock)   # which shot buckets moved (aw.bucket_meta)
        self.lock = threading.RLock()
        self.shots: dict[str, dict] = {}           # the live screenshots (id -> shot), oldest first
        self.past: dict[str, dict] = {}            # ended ones a press may still name (the image stays on disk)
        self._probe_at = float("-inf")             # the screenshot-folder probe is due (start, PROBE_S, a new shot)
        self.window = False                        # a screenshot thumbnail / markup window is on screen (being taken)
        self.threads: list[threading.Thread] = []

    @property
    def dry_run(self) -> bool:
        return (bool(getattr(self.live, "dry_run", False)) or bool(getattr(self.live, "exec_dry_run", False))   # --dry-exec
                or os.environ.get("OI_SHOT_DRY_RUN") == "1")

    # -- the bucket ----------------------------------------------------------------------------------------
    def prime(self) -> None:
        """Only events from now on (a restart never revives an old screenshot)."""
        self.since = vocab.iso_utc(datetime.fromtimestamp(self.clock(), timezone.utc))

    def new_events(self) -> list[dict]:
        now = self.clock()
        if self.since is None:
            self.prime()
        listing = self.buckets_fn()               # one GET /buckets/ per poll; events only where it moved
        meta = listing if isinstance(listing, dict) else dict.fromkeys(listing)
        self.gate.keep(meta)
        fresh: list[dict] = []
        for b, sig in sorted(meta.items()):
            if not b.startswith(BUCKET_PREFIX) or not self.gate.due(b, sig):
                continue
            evs = self.fetch(b, self.since)
            self.gate.fetched(b, sig)
            for ev in evs:
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
        threading.Thread(target=warm_rag, daemon=True, name="oi-rag-warm").start()

        def work():
            while True:
                try:
                    self.poll()
                except Exception:
                    log("poll failed: " + traceback.format_exc().splitlines()[-1])
                self.probe_due()                   # `oi health` follows a TCC grant / revoke
                time.sleep(POLL_S)
        t = threading.Thread(target=work, daemon=True, name="oi-shots")
        t.start()
        return t

    # -- the live state --------------------------------------------------------------------------------------
    def apply(self, ev: dict) -> dict | None:
        """One bucket event. shot_state thumbnail live / gone: a thumbnail / markup window is up / went (the
        screenshot being taken: in the context, no image yet); clipboard gone: the clipboard shot ends. shot: a new
        live screenshot (returned). a FILE screenshot is never bound to the thumbnail: macOS writes the file
        as its thumbnail slides away (a watcher log: file 11:58:02.506, thumbnail gone 11:58:02.927, the
        offer was taken down 531 ms after it appeared). The file is the state."""
        data = ev.get("data") or {}
        kind, src = data.get("kind"), data.get("src")
        if kind == "shot_state":
            ended = []
            with self.lock:
                if src == "thumbnail":
                    self.window = data.get("state") == "live"
                elif src == "clipboard" and data.get("state") == "gone":
                    ended = [s for s in self.shots.values() if s["clip"]]
            for s in ended:
                self.end(s, "clipboard_changed")
            return None
        if kind != "shot":
            return None
        return self.appear(ev, data)

    def appear(self, ev: dict, data: dict) -> dict | None:
        image = data.get("path")
        fs = file_state(image) if isinstance(image, str) else "gone"
        if fs == "gone":
            return None
        self._probe_at = float("-inf")             # each new screenshot re-probes the folder (next loop)
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
                "content": None, "suggest": None, "slots": set(), "ended": False, "unreadable": False,
                "after": newest_context(list(getattr(self.live, "rows", []) or []))}
        with self.lock:
            older = list(self.shots.values())
            self.shots[shot["id"]] = shot
        log(f"screenshot ({shot['src']}) live: contact {who['slug'] if who else '-'} ({who['via'] if who else '-'})")
        for s in older:                               # a new screenshot replaces the one before (its offer moves on)
            self.end(s, "replaced")
        self.notify("appear", shot)                   # shown at once; Claude's content actions join when they come
        if fs == "unreadable":
            self.unreadable(shot)
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
        """Live's loop, after each pull: a screenshot is the current context until the context changes (a newer
        app / page / desk token), never while a thumbnail / markup window is up (a screenshot is being taken).
        A clipboard one ends here too, not only when the
        clipboard changes. a file screenshot whose file is gone (deleted, moved: ENOENT) ends here too; one
        present but unreadable (TCC EPERM) stays and the failure is said (`unreadable`)."""
        newest = newest_context(rows)
        with self.lock:
            files = {s["id"]: file_state(s["image"]) for s in self.shots.values() if not s["clip"]}
            ended = [(s, "file_gone") for s in self.shots.values() if files.get(s["id"]) == "gone"]
            denied = [s for s in self.shots.values() if files.get(s["id"]) == "unreadable" and not s["unreadable"]]
            ended += [(s, "context_changed") for s in self.shots.values() if not self.window
                      and newest is not None and (s["after"] is None or newest > s["after"])
                      and newest[0] >= s["token"]["ts"] and all(s is not e for e, _ in ended)]
        for s, why in ended:
            self.end(s, why)
        for s in denied:
            self.unreadable(s)

    def unreadable(self, shot: dict) -> None:
        """the screenshot file is there but oi live may not read it (TCC): said once on the HUD, the folder
        re-probed (oi health); the offer stays (a press says it again, a grant makes it work)."""
        with self.lock:
            if shot["unreadable"]:
                return
            shot["unreadable"] = True
        log("screenshot file unreadable (denied)")
        self.request({"cmd": "notice", "text": UNREADABLE_NOTICE}, ("ok", "error", "dropped"), 2)
        self.probe_read()

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
        armed = dispatch.armed(now)                  # "pass me to X" names the next screenshot's agent
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
        row = self.press_row(shot, act[0], slot_kind(act), f"pressed:{reply.get('key')}")
        try:
            self._press(act, reply, shot, row)
        except Exception:
            row["error"] = "exception"
            log("shot press failed: " + traceback.format_exc().splitlines()[-1])
        vocal.log_row(row)
        return opt.get("token") if row["ok"] else None

    def press_row(self, shot: dict, action: str, picked: str, outcome: str) -> dict:
        who = shot.get("who") or {}
        return {"ts": vocab.iso_utc(datetime.fromtimestamp(self.clock(), timezone.utc)), "kind": "shot", "event": "press",
                "src": shot["src"], "to": who.get("slug"), "via": who.get("via"), "outcome": outcome,
                "action": action, "picked": picked, "tier": None, "ok": False, "dry_run": self.dry_run,
                "error": None, "live": not shot["ended"]}

    # -- picker first: a screenshot offers to forward it to a teammate, with a typed or dictated note ----------------
    def picker_first(self) -> bool:
        return bool(shot_settings(self.raw_settings())["picker_first"])

    def forward(self, shot: dict, show: dict | None = None) -> str | None:
        """Run by Live when a screenshot appears (settings `shots.picker_first`): the team picker at once (every
        recipient, the current contact first, one tap; "More" pages), then the note panel (type, or Tab / the mic
        to dictate on-device), Enter = ONE delivery of the image + the note (empty note = the image alone), through
        the outward gate in deliver(). Esc / no pick / the panel dismissed = nothing sent. `show` = Live's marker of
        the show bound to the screenshot, done once the picker is answered. One press row."""
        if getattr(self.live, "dry_run", False):       # `oi live --dry-run`: the picker printed, never shown
            who = (shot.get("who") or {}).get("slug")
            opts, _ = picker_options(roster(shot["table"], who), 0)
            print(json.dumps({"shot_forward": opts}, ensure_ascii=False), file=self.out or sys.stdout, flush=True)
            if show is not None:
                show["done"] = True
            return None
        row = self.press_row(shot, "forward", "pick", "forward")
        try:
            to = self.pick(shot, first=True)
            if show is not None:
                show["done"] = True
            if not to:
                row["outcome"] += ":dismissed"
            else:
                row["to"], row["via"] = to, ("contact" if to == (shot.get("who") or {}).get("slug") else "picked")
                name = shot["table"][to]["name"]
                info: dict = {}
                text = self.note(f"→ {name} · your note", "",
                                 "Type, or Tab to dictate · Enter sends (empty = the screenshot alone)", info=info)
                if text is None:
                    row["outcome"] += ":note_cancelled"
                else:
                    self.send(to, text, shot, row, tier=1, dictated=bool(info.get("dictated")))
        except Exception:
            row["error"] = "exception"
            log("shot forward failed: " + traceback.format_exc().splitlines()[-1])
        finally:
            if show is not None:
                show["done"] = True
        vocal.log_row(row)
        return f"shot.send:{row['to']}" if row["ok"] else None

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

    def pick(self, shot: dict, first: bool = False) -> str | None:
        """The agent picker. `first`: the whole roster, the current contact on the first key; else ("→ other")
        everyone but the contact."""
        who = (shot.get("who") or {}).get("slug")
        table, exclude = (roster(shot["table"], who), None) if first else (shot["table"], who)
        fill = (settings.load().get("hud") or {}).get("fill_order")
        page = 0
        for _ in range(12):
            opts, keys = picker_options(table, page, exclude=exclude, fill_order=fill)
            reply = self.request({"cmd": "shot_show", "options": opts, "ttl_ms": shot["ttl_ms"]},
                                 SHOW_EVENTS, shot["ttl_ms"] / 1000 + 30)
            if reply.get("event") != "pressed":
                return None
            v = keys.get(reply.get("key"))
            if v != "more":
                return v
            page += 1
        return None

    def note(self, title: str, text: str, placeholder: str, info: dict | None = None) -> str | None:
        """The editable note panel; the text the user sent with Enter, or None (Esc, click outside, no HUD). the
        panel offers on-device dictation (settings `shots.note_dictation`); `info["dictated"]` = it wrote some."""
        cmd = {"cmd": "shot_note", "title": title, "text": text, "placeholder": placeholder}
        if shot_settings(self.raw_settings())["note_dictation"]:
            cmd["dictate"] = True
        reply = self.request(cmd, NOTE_EVENTS, NOTE_TIMEOUT_S)
        if reply.get("event") != "note" or not isinstance(reply.get("text"), str):
            return None
        if info is not None:
            info["dictated"] = reply.get("dictated") is True
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

    def send(self, to: str, note: str | None, shot: dict, row: dict, *, tier: int, dictated: bool = False) -> dict:
        row["tier"], row["to"] = tier, to
        cur = self.latest(shot)                                # the newest live screenshot goes
        if cur is not shot:
            log("send: a newer screenshot is live, it goes instead of the one offered")
            shot, row["src"], row["newer"] = cur, cur["src"], True
        row["note_len"] = len(one_line(note))
        kw = {"dictated": True} if dictated else {}
        if dictated:
            row["dictated"] = True
        r = self.deliver_fn(to, shot["image"], note, ctx=shot["ctx"], when=shot["when"], dry_run=self.dry_run,
                            table=shot["table"], out=self.out, **kw)
        row["ok"], row["error"] = r.get("ok", False), r.get("error")
        log(f"tier {tier} -> {to}: " + ("dry run, nothing sent" if r.get("dry_run") else ("sent" if r.get("ok") else f"FAILED ({r.get('error')})")))
        if r.get("ok"):                                # the user answered: the forwarded shot leaves the context
            with self.lock:
                done = [s for s in self.shots.values() if s["now"] <= shot["now"]]
            for s in done:
                self.end(s, "answered")
        elif not str(r.get("error") or "").startswith("cancelled"):   # a failure is shown; the user's own cancel: silent
            self.request({"cmd": "notice", "text": failure_notice(shot["table"].get(to, {}).get("name") or to, r)},
                         ("ok", "error", "dropped"), 2)
            if r.get("denied"):
                self.probe_read()
        return r

    def probe_due(self) -> None:
        """re-probe every PROBE_S (and right after a new screenshot): a grant clears the FAIL, a revoke shows."""
        now = self.clock()
        if now - self._probe_at >= PROBE_S:
            self._probe_at = now
            self.probe_read()

    def probe_read(self) -> None:
        """can this process (oi live, launchd) read the screenshot folder? -> <data>/health/shots.json."""
        try:
            health.write_shot_probe(health.probe_shot_read(health.screenshot_folder(), self.clock()))
        except Exception:
            log("screenshot folder probe failed: " + traceback.format_exc().splitlines()[-1])


UNREADABLE_NOTICE = "Screenshot not readable: macOS denies oi live the screenshot folder (see oi health)"


def failure_notice(name: str, r: dict) -> str:
    """the one line the HUD shows when a forward failed (never a path, never the note)."""
    if r.get("denied"):
        return f"Not sent to {name}: macOS denies oi live the screenshot folder (see oi health)"
    return f"Not sent to {name}: {one_line(r.get('error') or 'failed')}"[:90]


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
