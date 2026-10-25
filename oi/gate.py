"""The outward gate: ONE validation before every action that leaves the Mac.

The user: "il manque une gate de validation pour toutes les actions extérieures de OS-intent : si
j'envoie un mail ou un Telegram il faut que j'aie quand même une petite fenêtre qui me dit : vous allez envoyer ce
message à ce destinataire, validé ou pas ?" ("Ask Atlas" sent on release is gated too).

`outward(cls, to, name, preview)` is called by every sender right before it sends, dry runs included:
  "vocal" oi.vocal.send   (HUD voice note, glide "Ask Atlas" / a send, W25 hand-over, wish "send to X")
  "shot"  oi.shots.deliver (W17 screenshot to an agent, every tier, `oi shot-send`)
  "page"  oi.live ui.press of a page button / link that is not KNOWN read-only (`page_outward`, fail-safe: merge,
          approve, an unknown learned label … all ask; navigation / open / expand / copy … do not), from a HUD
          press or a wish
  "reply" oi.live: a W24 proactive option marked `"outward": true` (reply / forward / draft to the event's sender,
          the agent's chat), at the pick, before its window opens; its "Ask <agent>" (agent.ask) is gated at the
          send itself (vocal), never twice
The panel is the ONE confirmation: pick -> gate panel -> validate. The older confirm steps (W16 two-tap, the glide
"Send ✓", W18's ⇧ confirm) are gone on these paths.
It asks oi-hud (`{"cmd":"confirm_outward",…}` on hud.sock): a small Liquid Glass panel "Envoyer ce message à Orion ?"
+ 1-3 preview lines, Return (or the action key) / ✓ validates, Esc / ✗ / a click outside / the timeout cancels.
Only `{"event":"outward","id":<same id>,"decision":"validated"}` lets the action run: no HUD, no reply, a socket
error, a timeout or anything else = cancelled (never an auto-send).

Not outward (no gate): opening a draft the user sends himself in Mail (mail.reply / mail.forward / mail.draft, W14
/ W22: there is no `send` in this code) unless it is a W24 `outward` option, opening apps / pages / folders / files /
a Telegram chat, music, desk moves, arming a dispatch ("pass me to X" sends nothing), a read-only page press.

settings.json `gate.outward`: true (default) | false (every class off) | {"vocal": bool, "shot": bool, "page":
bool, "reply": bool} (a missing class stays on). Off = the action runs ungated, logged `decision: "off"`; except
`force` (a page press whose press ticket only the panel can issue: an irreversible label, any wish press).
Every decision is one row in gate-log/<UTC day>.jsonl: ids only (gate id, class, recipient slug / page target,
decision, reason, latency): never the preview, a name or the message.

`offer_shot` (a "vocal" from the HUD): the panel also shows "Joindre une capture de
l'écran", OFF by default, C or a click toggles it; the `outward` event then carries `"attach_shot": true` and
`"display"` (1-based, the display under the pointer). Only a validated decision of a request that offered it counts
(`attach_shot` in the result); the caller (oi.vocal) takes the capture, never this module.
"""
from __future__ import annotations

import json
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import page_actions, paths, settings

CLASSES = ("vocal", "shot", "page", "reply")
TTL_MS = 20_000                  # the panel's own timeout (oi-hud cancels; python waits TTL + slack)
REPLY_SLACK_S = 3.0
PREVIEW_LINES = 3
PREVIEW_LINE_CHARS = 90
# page_outward is FAIL-SAFE: a press is gated unless it is KNOWN read-only. Known read-only = no irreversible
# (page_actions.IRREVERSIBLE) or outward word, and a link (navigation) or a button whose first word is a read-only
# verb / view name. An unknown learned label ("ship-it", "go") is gated.
PAGE_OUTWARD = frozenset({"approve", "comment", "reply", "post", "share", "invite", "request", "assign",
                          "review", "forward", "tweet", "like", "follow", "subscribe", "order", "confirm",
                          "accept", "reject", "decline", "sign", "save", "update", "create", "add", "upload",
                          "unsubscribe", "block", "report", "vote", "react", "star", "rerun", "retry", "run",
                          "deploy", "release", "resolve", "lock", "edit", "rename", "move", "mark", "dismiss"})
PAGE_READ_ONLY = frozenset({"open", "view", "show", "see", "expand", "collapse", "copy", "back", "next", "previous",
                            "prev", "more", "less", "details", "search", "find", "filter", "sort", "zoom", "refresh",
                            "reload", "preview", "home", "menu", "hide", "jump", "scroll", "load",
                            "files", "code", "commits", "checks", "conversation", "overview", "insights", "issues",
                            "pull", "actions", "tab", "page", "skip", "help"})

TITLES = {
    "fr": {"vocal": "Envoyer ce message à {name} ?", "shot": "Envoyer cette capture à {name} ?",
           "page": "Cliquer « {name} » ?", "reply": "{name} ?"},
    "en": {"vocal": "Send this to {name}?", "shot": "Send this screenshot to {name}?",
           "page": "Press “{name}”?", "reply": "{name}?"},
}

Asker = Callable[[dict, float], dict]


class GateError(RuntimeError):
    pass


def page_outward(slug: str | None, role: str | None = None) -> bool:
    """True = gated. Only a KNOWN read-only press skips the gate (fail-safe: anything unknown is outward)."""
    words = [w for w in (slug or "").split("-") if w]
    if not words or page_actions.irreversible(slug or "") or any(w in PAGE_OUTWARD for w in words):
        return True
    return not (role == "link" or words[0] in PAGE_READ_ONLY)


def enabled(cls: str, raw: dict) -> bool:
    """settings `gate.outward` for this class (default on; anything malformed = on)."""
    g = raw.get("gate")
    v = g.get("outward", True) if isinstance(g, dict) else True
    if isinstance(v, bool):
        return v
    if isinstance(v, dict):
        return v.get(cls) is not False
    return True


def lang(raw: dict | None) -> str:
    loc = ((raw or {}).get("wish") or {}).get("locale") if isinstance((raw or {}).get("wish"), dict) else None
    return "en" if isinstance(loc, str) and loc.lower().startswith("en") else "fr"


def preview_lines(text: str | None) -> list[str]:
    """1-3 lines of the content, each capped (the last one ends with … when cut)."""
    words = " ".join((text or "").split())
    out: list[str] = []
    while words and len(out) < PREVIEW_LINES:
        if len(words) <= PREVIEW_LINE_CHARS:
            out.append(words)
            words = ""
            break
        cut = words.rfind(" ", 0, PREVIEW_LINE_CHARS)
        cut = cut if cut > PREVIEW_LINE_CHARS // 2 else PREVIEW_LINE_CHARS
        out.append(words[:cut].rstrip())
        words = words[cut:].lstrip()
    if words and out:
        out[-1] = out[-1][:PREVIEW_LINE_CHARS - 1].rstrip() + "…"
    return out


def request(cls: str, *, name: str, preview: str | None, action_key: str | None = None,
            target: str | None = None, raw: dict | None = None, gid: str | None = None,
            offer_shot: bool = False) -> dict:
    """The `confirm_outward` command oi-hud draws."""
    req = {"cmd": "confirm_outward", "id": gid or uuid.uuid4().hex[:12], "cls": cls,
           "title": TITLES[lang(raw)][cls].format(name=name), "preview": preview_lines(preview),
           "ttl_ms": TTL_MS, "lang": lang(raw)}
    if action_key:
        req["action_key"] = action_key
    if target:
        req["target"] = target          # page: oi-hud issues the press ticket on validate
    if offer_shot:
        req["offer_shot"] = True        # the "Joindre une capture de l'écran" toggle, off by default
    return req


def hud_ask(req: dict, timeout_s: float) -> dict:
    """Send the request on a fresh hud.sock connection and wait for ITS `outward` event."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_s)
            s.connect(str(paths.hud_socket()))
            s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
            buf = b""
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                chunk = s.recv(4096)
                if not chunk:
                    return {"event": "dropped", "reason": "hud_closed"}
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and ev.get("event") in ("outward", "error", "dropped"):
                        return ev
            return {"event": "dropped", "reason": "timeout"}
    except socket.timeout:
        return {"event": "dropped", "reason": "timeout"}
    except OSError:
        return {"event": "dropped", "reason": "hud_down"}


def log_row(row: dict) -> None:
    paths.ensure_private_root()
    path = paths.data_dir() / "gate-log" / f"{row['ts'][:10]}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def outward(cls: str, to: str, name: str, preview: str | None, *, action_key: str | None = None,
            target: str | None = None, dry_run: bool = False, ask: Asker | None = None,
            raw: dict | None = None, force: bool = False, offer_shot: bool = False,
            shot_reason: str | None = None) -> dict:
    """The gate. {"ok": True, "decision": "validated"|"off", "id"} lets the caller send; {"ok": False,
    "decision": "cancelled", "reason", "error"} = send nothing. `ask` (tests) replaces the HUD round trip.
    `force`: asks even when the setting is off (a page press only the panel's ticket lets oi-hud make).
    `offer_shot`: the panel offers the screenshot toggle; {"attach_shot": True, ["display": n]} when the user chose it
    (never with the setting off: no panel, no choice, no capture). `shot_reason`: why the toggle was not
    offered, a reason code only (oi.vocal.shot_offer_ok), logged as the row's `shot_reason`."""
    if cls not in CLASSES:
        raise GateError(f"unknown gate class {cls}")
    if raw is None:
        try:
            raw = json.loads(settings.settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
    t0 = time.monotonic()
    req = request(cls, name=name, preview=preview, action_key=action_key, target=target, raw=raw, offer_shot=offer_shot)
    attach, display = False, None
    if not force and not enabled(cls, raw):
        decision, reason = "off", "setting"
    else:
        try:
            ev = (ask or ASK)(req, TTL_MS / 1000 + REPLY_SLACK_S)
        except Exception:
            ev = {"event": "dropped", "reason": "ask_failed"}
        ev = ev if isinstance(ev, dict) else {}
        if ev.get("event") == "outward" and ev.get("id") == req["id"] and ev.get("decision") == "validated":
            decision, reason = "validated", ev.get("reason") if isinstance(ev.get("reason"), str) else "validate"
            attach = offer_shot and ev.get("attach_shot") is True
            d = ev.get("display")
            display = d if attach and isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 16 else None
        else:
            decision = "cancelled"
            reason = ev.get("reason") or ev.get("message") or ev.get("event") or "no_reply"
            reason = reason if isinstance(reason, str) else "no_reply"
    log_row({"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
             "kind": "gate", "id": req["id"], "cls": cls, "to": to, "decision": decision,
             "sent": decision != "cancelled", "reason": reason[:40], "dry_run": dry_run,
             "latency_ms": int((time.monotonic() - t0) * 1000), "offer_shot": offer_shot, "attach_shot": attach,
             **({"shot_reason": shot_reason[:40]} if shot_reason and not offer_shot else {})})
    out = {"ok": decision != "cancelled", "decision": decision, "id": req["id"], "reason": reason}
    if attach:
        out["attach_shot"] = True
        if display:
            out["display"] = display
    if not out["ok"]:
        out["error"] = f"cancelled ({reason})"
    return out


ASK: Asker = hud_ask       # the one HUD round trip (tests/conftest.py replaces it: never a real socket in tests)
