"""`oi demo <scene> | contrast | all`: scripted HUD scenes for screen recording, with fake data only.

Each scene drives oi-hud through its debug commands (hint, summon, glide, press, wish, vocal, shot note, outward
gate), timed for a screen recorder. Every person, company, mail, PR and agent below is fictional (features.md:
Sam Rivera, Dana Park, Pat Lee, Northwind Billing, acme/rocket #42, agents Atlas / Nova / Orion / Lyra).
`oi demo contrast` is the heart of the film: the same client mail on day 1 (generic intents), a title card, then
day 30 (intents that know the user).

Isolation (the demo can never mix with the real data or send anything):
- its OWN oi-hud instance (`serve --socket <tmp>/hud.sock`) with `OI_DATA_DIR` = a fresh temp dir holding a demo
  settings.json; the live data dir (~/.local/share/os-intent) and its socket are refused, and the demo does
  not start while the live HUD is enabled (settings hud_enabled or a live hud.sock answering) unless --force;
- the vocal sender is a stub that only prints a dry-run answer (`OI_VOCAL_BIN`), plus `OI_VOCAL_DRY_RUN=1`;
  `OI_HUD_AGENTS` gives the picker the fictional roster (the vocal scene checks it via `state`);
- this driver is the only client: a press, a wish or a note executes NOTHING (no Mail, no page press, no app);
  Claude is never called (canned cards); the outward gate is always answered `cancel`;
- TELEGRAM_* / GH_TOKEN / GITHUB_TOKEN / ANTHROPIC_* are removed from the HUD's environment.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

PKG = Path(__file__).resolve().parent.parent           # the os-intent checkout
BACKDROPS = PKG / "demo" / "backdrops"
AGENTS = [("atlas", "Atlas"), ("nova", "Nova"), ("orion", "Orion"), ("lyra", "Lyra"),
          ("vega", "Vega"), ("sol", "Sol"), ("iris", "Iris"), ("juno", "Juno"), ("kai", "Kai")]
AGENTS_SPEC = ",".join(f"{s}:{n}" for s, n in AGENTS)
# names of the example roster compiled into oi-hud (Vocal.swift) that the demo roster does not use: seeing one in
# the picker means the HUD ignored OI_HUD_AGENTS (your real roster could be on screen)
REAL_AGENT_NAMES = {"Hera", "Echo"}
SECRET_ENV_PREFIXES = ("TELEGRAM_", "ANTHROPIC_", "CLAUDE_CODE_OAUTH")
SECRET_ENV = {"GH_TOKEN", "GITHUB_TOKEN"}
TTL_MS = 15000                                           # shotlist: gate.ttl_ms 15000 while filming

MAIL = "app:/System/Applications/Mail.app"
CAL = "app:/System/Applications/Calendar.app"
SAFARI = "app:/Applications/Safari.app"
FINDER = "app:/System/Library/CoreServices/Finder.app"
DESK2 = {"key": 4, "label": "Desk 2", "icon": "sf:rectangle.split.3x1", "hint": {"kind": "edge", "dir": "right"}}

# day 1 vs day 30: the SAME incoming client mail (inbox.html): Sam Rivera "Thursday review: can we move it?"
# (storyboard v3, S5/S7/S9: the option labels and their order are Juno's exact ask)
INCOMING_MAIL = "Sam Rivera \"Thursday review: can we move it?\""
# day 1, a fresh user: a model with no history only knows app jumps
DAY1_MENU = [
    {"key": 1, "label": "Safari", "icon": SAFARI},
    {"key": 2, "label": "Open Calendar", "icon": CAL},
    {"key": 3, "label": "Finder", "icon": FINDER},
    DESK2,
]
# day 30: it knows Sam is a client, the review slot, Dana Park who joins for the signage
DAY30_MENU = [
    {"key": 1, "label": "Reply to Sam", "icon": "sf:arrowshape.turn.up.left", "claude": True},
    {"key": 2, "label": "Open Calendar: Friday", "icon": CAL},
    {"key": 3, "label": "Forward to Dana Park", "icon": "sf:arrowshape.turn.up.right"},
    DESK2,
]
DAY30_DRAFT = ("Draft, not sent:\nHi Sam, Friday 10:00 works for me.\n"
               "I'll move the review invite and keep Dana in for the signage.\nAlex")
DAY30_OUTWARD = {"cmd": "confirm_outward", "id": "demo-day30", "cls": "reply",
                 "title": "Send this reply to Sam?",
                 "preview": ["Hi Sam, Friday 10:00 works for me.",
                             "I'll move the review invite and keep Dana in for the signage."],
                 "ttl_ms": 15000, "lang": "en", "action_key": "1"}
LATER_TITLE = "30 days later"
CONTRAST = ["day1", "later", "day30"]

# storyboard v3 S11 variants, each on its own everyday backdrop (fictional people only)
# architect: Dana Park (Park & Hale Architects), a client asks for the Friday render of the loft
ARCHITECT_MENU = [
    {"key": 1, "label": "Reply to Lena", "icon": "sf:arrowshape.turn.up.left", "claude": True},
    {"key": 2, "label": "Open Calendar: Friday", "icon": CAL},
    {"key": 3, "label": "Open Loft v3 folder", "icon": "sf:folder"},
    DESK2,
]
ARCHITECT_DRAFT = ("Draft, not sent:\nHi Lena, the render of the loft will be with you on Friday by noon.\n"
                   "I'll send it with the v3 plans.\nDana")
ARCHITECT_OUTWARD = {"cmd": "confirm_outward", "id": "demo-architect", "cls": "reply",
                     "title": "Send this reply to Lena?",
                     "preview": ["Hi Lena, the render of the loft will be with you on Friday by noon.",
                                 "I'll send it with the v3 plans."],
                     "ttl_ms": 15000, "lang": "en", "action_key": "1"}
# manager: Pat Lee forwards the Q3 report; glide on Forward, then Paul or Sarah
MANAGER_MENU = [
    {"key": 1, "label": "Reply to Pat", "icon": "sf:arrowshape.turn.up.left", "claude": True},
    {"key": 2, "label": "Forward", "icon": "sf:arrowshape.turn.up.right"},
    {"key": 3, "label": "Open Calendar", "icon": CAL},
    DESK2,
]
MANAGER_RINGS = {
    (2,): [{"key": 1, "label": "Paul Weston", "icon": "initial:P"},
           {"key": 2, "label": "Sarah Kim", "icon": "initial:S"}],
}
MANAGER_OUTWARD = {"cmd": "confirm_outward", "id": "demo-manager", "cls": "reply",
                   "title": "Forward the Q3 report to Paul Weston?",
                   "preview": ["Fwd: Q3 report", "Margins up 2 pts. Can someone check travel?"],
                   "ttl_ms": 15000, "lang": "en", "action_key": "2"}
# recruiter: Jordan Blake (Talentbridge); a candidate answers about the call
RECRUITER_MENU = [
    {"key": 1, "label": "Reply to Priya", "icon": "sf:arrowshape.turn.up.left", "claude": True},
    {"key": 2, "label": "Open Calendar: Thu 15:00", "icon": CAL},
    {"key": 3, "label": "Open Priya's CV", "icon": "sf:doc.richtext"},
    DESK2,
]
RECRUITER_DRAFT = ("Draft, not sent:\nHi Priya, Thursday 15:00 works. I'll send the video link.\n"
                   "The team is looking forward to meeting you.\nJordan")
RECRUITER_OUTWARD = {"cmd": "confirm_outward", "id": "demo-recruiter", "cls": "reply",
                     "title": "Send this reply to Priya?",
                     "preview": ["Hi Priya, Thursday 15:00 works. I'll send the video link.",
                                 "The team is looking forward to meeting you."],
                     "ttl_ms": 15000, "lang": "en", "action_key": "1"}

# Mail on Sam Rivera's "Thursday review: can we move it?" (Alex Moreau, a freelance designer; Sam is a client)
MAIL_MENU = [
    {"key": 1, "label": "Open Calendar", "icon": CAL},
    {"key": 2, "label": "Reply to Sam", "icon": "sf:arrowshape.turn.up.left", "claude": True},
    {"key": 3, "label": "Open acme/rocket #42", "icon": SAFARI},
    DESK2,
]
GLIDE_RINGS = {
    (2,): [{"key": 1, "label": "Draft: Friday 10:00 ok", "icon": "sf:sparkles", "claude": True},
           {"key": 2, "label": "Propose Monday 9:00", "icon": "sf:calendar"},
           {"key": 3, "label": "Forward to Dana Park", "icon": "sf:arrowshape.turn.up.right"}],
}
SAM_DRAFT = ("Draft, not sent:\nHi Sam, Friday 10:00 works for me.\n"
             "I'll move the review invite.\nAlex")
# Safari on the fake PR acme/rocket #42 "Faster boot sequence" by octocat
PR_MENU = [
    {"key": 1, "label": "Open CI checks", "icon": "sf:checkmark.seal"},
    {"key": 2, "label": "Squash & merge", "icon": "sf:arrow.triangle.merge", "confirm": True,
     "target": "button:squash-and-merge"},
    {"key": 3, "label": "Reply to octocat", "icon": "sf:text.bubble"},
    {"key": 4, "label": "Desk 2", "icon": "sf:rectangle.split.3x1", "hint": {"kind": "edge", "dir": "right"}},
]
SHOT_MENU = [
    {"key": 1, "label": "→ Atlas", "icon": "initial:A", "claude": True},
    {"key": 2, "label": "→ other", "icon": "sf:person.2"},
    {"key": 3, "label": "Annotate", "icon": "sf:square.and.pencil"},
]
SHOT_NOTE = ("Northwind Billing invoice NB-2291: €1,240, due 30 Oct.\n"
             "Line 3 (\"cloud seats\") looks doubled vs. September.\n"
             "Can you check before we pay?")
# a proactive mail: the recruiter Jordan Blake (Talentbridge) — "Offer: Senior Product Designer, call today?"
PROACTIVE_MENU = [
    {"key": 1, "label": "Add call to Calendar", "icon": CAL},
    {"key": 2, "label": "Reply to Jordan", "icon": "sf:arrowshape.turn.up.left", "claude": True},
    {"key": 3, "label": "Open the offer PDF", "icon": "sf:doc.richtext"},
    {"key": 4, "label": "Snooze 1 h", "icon": "sf:moon.zzz"},
]

class DemoError(RuntimeError):
    pass


# ---- the socket client -----------------------------------------------------------------------------------------

class Conn:
    """One persistent connection to oi-hud: JSON lines out, events into a queue (reader thread)."""

    def __init__(self, path: str, connect_timeout_s: float = 5.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(connect_timeout_s)
        self.sock.connect(path)
        self.sock.settimeout(None)
        self.events: queue.Queue = queue.Queue()
        self._w = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        buf = b""
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        try:
                            self.events.put(json.loads(line))
                        except ValueError:
                            pass
        except OSError:
            pass
        self.events.put({"event": "_closed"})

    def send(self, obj: dict) -> None:
        with self._w:
            self.sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode())

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---- the scene driver ------------------------------------------------------------------------------------------

class Demo:
    """Sends a scene's commands and answers the HUD's events with canned data (never executes anything)."""

    def __init__(self, conn: Conn, speed: float = 1.0, out: Callable[[str], None] = print):
        self.conn, self.speed, self.out = conn, speed, out
        self.sent: list[dict] = []
        self.seen: list[dict] = []
        self.menu: list[dict] = []          # answers a summon
        self.rings: dict[tuple, list[dict]] = {}
        self.cards: dict[tuple, str | None] = {}   # (path) -> card text after a Claude-assisted press
        self.claude_s = 2.5                 # the fake "Claude is working" spinner
        self.resolver: Callable[[dict], dict | None] | None = None
        self._timers: list[tuple[float, Callable[[], None]]] = []
        self._mark = 0
        self.scene = ""
        self.on_scene: Callable[[str], None] | None = None   # e.g. open the scene's backdrop

    # -- output and time
    def say(self, msg: str) -> None:
        self.out(f"[{self.scene}] {msg}")

    def send(self, obj: dict) -> None:
        self.sent.append(obj)
        self.conn.send(obj)

    def later(self, s: float, fn: Callable[[], None]) -> None:
        self._timers.append((time.monotonic() + s * self.speed, fn))

    def _run_timers(self) -> None:
        now = time.monotonic()
        due = [t for t in self._timers if t[0] <= now]
        self._timers = [t for t in self._timers if t[0] > now]
        for _, fn in due:
            fn()

    def _pump_one(self, timeout: float) -> dict | None:
        self._run_timers()
        if self._timers:
            timeout = max(0.0, min(timeout, min(t for t, _ in self._timers) - time.monotonic()))
        try:
            ev = self.conn.events.get(timeout=timeout)
        except queue.Empty:
            self._run_timers()
            return None
        self.seen.append(ev)
        self._handle(ev)
        return ev

    def sleep(self, s: float) -> None:
        """Wait `s` seconds (scaled by speed) while answering the HUD."""
        end = time.monotonic() + s * self.speed
        while True:
            left = end - time.monotonic()
            self._pump_one(max(0.0, min(left, 0.05)))
            if left <= 0 and self.conn.events.empty() and not self._due():
                return

    def _due(self) -> bool:
        return any(t <= time.monotonic() for t, _ in self._timers)

    def mark(self) -> None:
        """`wait` only looks at events from here on (set it just before the command whose answer is awaited)."""
        self._mark = len(self.seen)

    def wait(self, pred: Callable[[dict], bool], timeout_s: float = 6.0) -> dict | None:
        """The first event since `mark()` matching `pred`; None after `timeout_s` (real time)."""
        end = time.monotonic() + timeout_s
        while True:
            for i in range(self._mark, len(self.seen)):
                if pred(self.seen[i]):
                    self._mark = i + 1
                    return self.seen[i]
                if self.seen[i].get("event") == "_closed":
                    raise DemoError("oi-hud closed the connection")
            self._mark = len(self.seen)
            if time.monotonic() >= end:
                return None
            self._pump_one(min(0.05, max(0.0, end - time.monotonic())))

    # -- canned answers
    def _handle(self, ev: dict) -> None:
        kind = ev.get("event")
        if kind == "summon" and self.menu:
            self.send({"cmd": "show", "options": self.menu, "ttl_ms": TTL_MS})
        elif kind == "expand":
            path = tuple(ev.get("path") or [])
            self.send({"cmd": "subring", "path": list(path), "options": self.rings.get(path, [])})
        elif kind == "resolve" and self.resolver:
            ans = self.resolver(ev)
            if ans:
                delay = 0.8 if ev.get("final") else 0.0
                self.later(delay, lambda a=ans: self.send(dict(a, cmd="resolution", id=ev["id"])))
        elif kind == "pressed":
            path = tuple(ev.get("path") or [ev.get("key")])
            if ev.get("mode") == "claude":
                text = self.cards.get(path)
                self.say(f"Claude-assisted {'.'.join(map(str, path))}: fake Claude working (nothing is called)")
                self.later(self.claude_s, lambda k=path[0], t=text: self.send(
                    {"cmd": "result", "key": k, "ok": True, **({"text": t} if t else {}), "ttl_ms": 8000}))
            else:
                self.say(f"pressed {'.'.join(map(str, path))} (demo: nothing runs)")
        elif kind == "note":
            self.say(f"note submitted ({len(ev.get('text') or '')} chars; demo: nothing sent)")
        elif kind == "outward":
            self.say(f"validation window: {ev.get('decision')} ({ev.get('reason')})")
        elif kind == "error":
            self.say(f"HUD error: {ev.get('message')}")

    def reset(self) -> None:
        self.menu, self.rings, self.cards, self.resolver = [], {}, {}, None
        self._timers = []

    def hide(self) -> None:
        self.send({"cmd": "hide"})

    def summon(self) -> None:
        """⌥⌘ without a key: develops the hint up, else the HUD broadcasts `summon` and `menu` answers it."""
        self.mark()
        self.send({"cmd": "debug_summon"})
        self.wait(lambda e: e.get("event") in ("summon", "hint"), 3)


# ---- scenes ----------------------------------------------------------------------------------------------------

def incoming_mail(d: Demo, menu: list[dict]) -> None:
    """Sam's mail lands (ringed dots by the pointer), ⌥⌘ develops the cross on it."""
    d.menu = menu
    d.say(f"new mail: {INCOMING_MAIL} (ringed dots)")
    d.send({"cmd": "show", "mode": "hint", "marker": "ring", "hint_ttl_ms": 6000, "ttl_ms": TTL_MS, "options": menu})
    d.sleep(2.5)
    d.say("⌥⌘ tap: the cross opens on the mail")
    d.summon()


def scene_day1(d: Demo) -> None:
    """Day 1, a fresh user: the cross only offers app jumps (Safari, Open Calendar, Finder, Desk 2)."""
    incoming_mail(d, DAY1_MENU)
    d.say("day 1, no history: only app jumps (Safari · Open Calendar · Finder · Desk 2)")
    d.sleep(5.0)


def scene_later(d: Demo) -> None:
    """The title card between day 1 and day 30: the HUD stays hidden over title.html."""
    d.hide()
    d.say(f"title card: \"{LATER_TITLE}\"")
    d.sleep(3.0)


def draft_then_gate(d: Demo, key: int, draft: str, gate: dict, who: str) -> None:
    """Long press on the reply: spinner, then the canned draft card; then the real outward gate panel, cancelled."""
    d.cards = {(key,): draft}
    d.claude_s = 2.0
    d.say(f"hold ⌃ on \"Reply to {who}\": the draft (canned, nothing sent)")
    d.mark()
    d.send({"cmd": "debug_press", "key": key, "mode": "claude"})
    d.wait(lambda e: e.get("event") == "pressed", 3)
    d.sleep(d.claude_s + 4.0)
    d.say("send? the outward gate panel opens (dry run, always cancelled)")
    d.mark()
    d.send(dict(gate))
    d.sleep(4.5)
    d.send({"cmd": "debug_outward", "decision": "cancel"})
    d.wait(lambda e: e.get("event") == "outward", 3)
    d.sleep(1.0)


def scene_day30(d: Demo) -> None:
    """Day 30, the SAME mail: « Reply to Sam » first, Open Calendar: Friday, Forward to Dana Park, Desk 2; a long
    press shows the draft, then « Send this reply to Sam? » (cancelled)."""
    incoming_mail(d, DAY30_MENU)
    d.say("day 30: Reply to Sam · Open Calendar: Friday · Forward to Dana Park · Desk 2")
    d.sleep(3.5)
    draft_then_gate(d, 1, DAY30_DRAFT, DAY30_OUTWARD, "Sam")


def scene_architect(d: Demo) -> None:
    """Architect (Dana Park): a client asks for the Friday render; reply draft, gate cancelled."""
    incoming = "Lena Ortiz \"Loft: can I see the render on Friday?\""
    d.say(f"new mail: {incoming} (ringed dots)")
    d.menu = ARCHITECT_MENU
    d.send({"cmd": "show", "mode": "hint", "marker": "ring", "hint_ttl_ms": 6000, "ttl_ms": TTL_MS,
            "options": ARCHITECT_MENU})
    d.sleep(2.5)
    d.say("⌥⌘ tap: the cross opens on the mail")
    d.summon()
    d.sleep(3.0)
    draft_then_gate(d, 1, ARCHITECT_DRAFT, ARCHITECT_OUTWARD, "Lena")


def scene_manager(d: Demo) -> None:
    """Manager (Pat Lee): hold ⌥⌘, rest on Forward, glide to Paul Weston (Sarah Kim is the other); gate cancelled."""
    d.menu, d.rings = MANAGER_MENU, MANAGER_RINGS
    d.say("Pat Lee \"Fwd: Q3 report\": hold ⌥⌘ → rest on \"Forward\" → glide to \"Paul Weston\" (or Sarah Kim)")
    d.mark()
    d.send({"cmd": "debug_glide", "path": [2, 1], "cancel": False})
    d.wait(lambda e: e.get("event") == "pressed", 15)
    d.sleep(2.0)
    d.say("forward? the outward gate panel opens (dry run, always cancelled)")
    d.mark()
    d.send(dict(MANAGER_OUTWARD))
    d.sleep(4.5)
    d.send({"cmd": "debug_outward", "decision": "cancel"})
    d.wait(lambda e: e.get("event") == "outward", 3)
    d.sleep(1.0)


def scene_recruiter(d: Demo) -> None:
    """Recruiter (Jordan Blake): a candidate answers about the call; reply draft, calendar slot, gate cancelled."""
    incoming = "Priya Nair \"Re: Product designer role, call this week?\""
    d.say(f"new mail: {incoming} (ringed dots)")
    d.menu = RECRUITER_MENU
    d.send({"cmd": "show", "mode": "hint", "marker": "ring", "hint_ttl_ms": 6000, "ttl_ms": TTL_MS,
            "options": RECRUITER_MENU})
    d.sleep(2.5)
    d.say("⌥⌘ tap: the cross opens on the mail")
    d.summon()
    d.sleep(3.0)
    draft_then_gate(d, 1, RECRUITER_DRAFT, RECRUITER_OUTWARD, "Priya")


def scene_hint(d: Demo) -> None:
    """Shots 4-6: four hint dots by the pointer, ⌥⌘ develops them into the cross, ⌘ = "Desk 2"."""
    d.menu = MAIL_MENU
    d.say("Mail on Sam Rivera's \"Thursday review: can we move it?\": the hint dots appear")
    d.send({"cmd": "show", "mode": "hint", "hint_ttl_ms": 6000, "ttl_ms": TTL_MS, "options": MAIL_MENU})
    d.sleep(3.0)
    d.say("⌥⌘ tap: the dots spring into the cross")
    d.summon()
    d.sleep(3.5)
    d.say("⌘ tap: \"Desk 2\"")
    d.send({"cmd": "debug_press", "key": 4})
    d.wait(lambda e: e.get("event") == "pressed", 3)
    d.sleep(1.0)


def scene_glide(d: Demo) -> None:
    """Shot 7: hold ⌥⌘, rest on "Reply to Sam", the follow-ups bloom, release on "Draft: Friday 10:00 ok"."""
    d.menu, d.rings = MAIL_MENU, GLIDE_RINGS
    d.cards = {(2, 1): SAM_DRAFT}
    d.claude_s = 2.0
    d.say("hold ⌥⌘ → rest on \"Reply to Sam\" → glide to \"Draft: Friday 10:00 ok\" → release")
    d.mark()
    d.send({"cmd": "debug_glide", "path": [2, 1], "cancel": False})
    d.wait(lambda e: e.get("event") == "pressed", 15)
    d.sleep(d.claude_s + 6.0)


def scene_longpress(d: Demo) -> None:
    """Shot 8: summon, long-press ⌃ on "Reply to Sam": spinner, then the draft card (never sent)."""
    d.menu = MAIL_MENU
    d.cards = {(2,): SAM_DRAFT}
    d.claude_s = 3.0
    d.say("⌥⌘ tap: the cross")
    d.summon()
    d.sleep(2.5)
    d.say("hold ⌃ on \"Reply to Sam\": Claude drafts (canned, nothing sent)")
    d.mark()
    d.send({"cmd": "debug_press", "key": 2, "mode": "claude"})
    d.wait(lambda e: e.get("event") == "pressed", 3)
    d.sleep(d.claude_s + 6.0)


def scene_page(d: Demo) -> None:
    """Shot 9: Safari on acme/rocket #42: "Squash & merge" arms on the first tap, fires on the second."""
    d.menu = PR_MENU
    d.say("Safari on acme/rocket #42 \"Faster boot sequence\": ⌥⌘ tap")
    d.summon()
    d.sleep(2.5)
    d.say("⌃ tap: \"Squash & merge again?\" (armed)")
    d.send({"cmd": "debug_press", "key": 2})
    d.sleep(1.6)
    d.say("⌃ again: fires (demo: nothing is pressed on any page)")
    d.mark()
    d.send({"cmd": "debug_press", "key": 2})
    d.wait(lambda e: e.get("event") == "pressed", 3)
    d.sleep(1.0)


def wish_resolver(ev: dict) -> dict | None:
    text = (ev.get("text") or "").lower()
    hit = "tel" in text
    if ev.get("final"):
        if hit:
            return {"state": "done", "label": "✓ Telegram", "token": "activate:Telegram", "tier": "local",
                    "target": "Telegram"}
        return {"state": "noted", "label": "Noted"}
    return {"state": "preview", "label": "→ Telegram" if hit else ""}


def scene_wish(d: Demo, text: str = "open telegram") -> None:
    """Shot 10: ⌥` → "Tell me what you wanted"; the text is typed, "→ Telegram" resolves under it; Enter."""
    d.resolver = wish_resolver
    d.send({"cmd": "resolver"})
    d.say(f"⌥`: the wish panel; typing \"{text}\"")
    for i in range(1, len(text) + 1):
        d.send({"cmd": "debug_wish", "text": text[:i], "app": "Mail", "enter": False})
        d.sleep(0.11)
    d.sleep(1.2)
    d.say("Enter (demo: nothing is opened)")
    d.mark()
    d.send({"cmd": "debug_wish", "text": text, "app": "Mail", "enter": True})
    d.wait(lambda e: e.get("event") == "resolve" and e.get("final"), 5)
    d.sleep(4.0)


def check_roster(d: Demo) -> list[str]:
    """The picker's names, from `state`; refuses a HUD without the fictional roster (it would film real agents)."""
    d.mark()
    d.send({"cmd": "state"})
    st = d.wait(lambda e: e.get("event") == "state", 3) or {}
    names = st.get("agents")
    if not names or set(names) & REAL_AGENT_NAMES or not set(names) <= {n for _, n in AGENTS}:
        raise DemoError("this oi-hud shows the real agent roster: build it from this branch "
                        "(cd hud && swift build -c release) and run the demo again")
    return names


def scene_vocal(d: Demo) -> None:
    """Shot 11: ⌃⌥ → the agent picker (Atlas, Nova, Orion, Lyra); ⌃ = Nova; the transcript; dry-run toast."""
    names = check_roster(d)
    d.say(f"⌃⌥ tap: the agent picker ({', '.join(names[:4])})")
    d.send({"cmd": "debug_vocal"})
    d.sleep(3.0)
    d.say("⌃ = Nova: \"Move the review to Friday\" (dry run, nothing sent)")
    d.send({"cmd": "debug_vocal", "pick": "nova", "text": "Move the review to Friday"})
    d.sleep(5.0)


def scene_screenshot(d: Demo) -> None:
    """Screenshot → contact: the offer ("→ Atlas", "→ other", "Annotate"), long-press "→ Atlas", the note."""
    d.cards = {(1,): None}                         # tier 2: the spinner, then the note panel
    d.claude_s = 2.0
    d.say("⌘⇧4 taken: the HUD offers the screenshot")
    d.send({"cmd": "shot_show", "options": SHOT_MENU, "ttl_ms": TTL_MS})
    d.sleep(3.0)
    d.say("hold fn on \"→ Atlas\": the note is drafted (canned OCR + context)")
    d.mark()
    d.send({"cmd": "debug_press", "key": 1, "mode": "claude"})
    d.wait(lambda e: e.get("event") == "pressed", 3)
    d.sleep(d.claude_s + 0.4)
    d.send({"cmd": "shot_note", "title": "→ Atlas · Claude note", "text": SHOT_NOTE,
            "placeholder": "What should go with the screenshot?"})
    d.sleep(5.0)
    d.say("Enter (demo: nothing sent)")
    d.send({"cmd": "debug_note"})
    d.sleep(1.0)


def scene_proactive(d: Demo) -> None:
    """Proactive: an important mail arrives (ringed dots), ⌥⌘ opens the cross on it."""
    d.menu = PROACTIVE_MENU
    d.say("new important mail: Jordan Blake (Talentbridge) \"Offer: Senior Product Designer, call today?\"")
    d.send({"cmd": "show", "mode": "hint", "marker": "ring", "hint_ttl_ms": 6000, "ttl_ms": TTL_MS,
            "options": PROACTIVE_MENU})
    d.sleep(3.0)
    d.say("⌥⌘ tap: the cross opens on the mail")
    d.summon()
    d.sleep(5.0)
    d.hide()
    d.sleep(0.5)


# order = the film's order; (name, function, shot, backdrop)
SCENES: list[tuple[str, Callable[[Demo], None], str, str]] = [
    ("day1", scene_day1, "contrast 1/2: day 1, generic intents", "inbox.html"),
    ("later", scene_later, f"contrast title card: {LATER_TITLE}", "title.html"),
    ("day30", scene_day30, "contrast 2/2: day 30, same mail, personal intents", "inbox.html"),
    ("architect", scene_architect, "variant: architect, the Friday render request", "architect.html"),
    ("manager", scene_manager, "variant: manager, glide on Forward (Paul, Sarah)", "manager.html"),
    ("recruiter", scene_recruiter, "variant: recruiter, candidate reply + calendar", "recruiter.html"),
    ("hint", scene_hint, "shots 4-6: hint dots → cross → one key", "mail.html"),
    ("glide", scene_glide, "shot 7: glide drill-down", "mail.html"),
    ("longpress", scene_longpress, "shot 8: long-press → Claude draft", "mail.html"),
    ("page", scene_page, "shot 9: page action (two taps)", "pr.html"),
    ("wish", scene_wish, "shot 10: wish, typed", "mail.html"),
    ("vocal", scene_vocal, "shot 11: vocal to an agent (dry run)", "mail.html"),
    ("screenshot", scene_screenshot, "screenshot → contact + note", "invoice.html"),
    ("proactive", scene_proactive, "proactive important mail → cross", "mail.html"),
]
SCENE_NAMES = [s[0] for s in SCENES]


def run_scene(d: Demo, name: str) -> None:
    fn = next(s[1] for s in SCENES if s[0] == name)
    d.reset()
    d.scene = name
    if d.on_scene:
        d.on_scene(name)
    fn(d)
    d.hide()
    d.sleep(0.3)


# ---- isolation: the demo's own data dir and HUD ---------------------------------------------------------------

def live_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "os-intent"


def refuse_live(path: Path) -> None:
    p, live = Path(os.path.realpath(path)), Path(os.path.realpath(live_data_dir()))
    if p == live or live in p.parents:
        raise DemoError(f"refused: {path} is the live os-intent data dir; the demo uses its own temp dir")


def live_hud_state(live: Path | None = None) -> str | None:
    """Why the live HUD is active (settings `hud_enabled`, or its hud.sock answers), else None. Read-only."""
    live = live or live_data_dir()
    try:
        raw = json.loads((live / "settings.json").read_text())
        if isinstance(raw, dict) and raw.get("hud_enabled", True):
            return "live settings have hud_enabled true"
    except (OSError, ValueError):
        pass
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(live / "hud.sock"))
        finally:
            probe.close()
        return "the live hud.sock answers"
    except OSError:
        return None


def refuse_live_hud(force: bool = False) -> None:
    why = live_hud_state()
    if why and not force:
        raise DemoError(f"refused: the live HUD is enabled ({why}); both HUDs would answer the same keys on "
                        "screen. Menu bar → OS-intent → untick \"HUD enabled\", then run the demo again "
                        "(or --force if you know what you are doing).")


VOCAL_STUB = """#!/bin/sh
# oi demo: the vocal sender. Sends nothing, ever: answers as `oi vocal-send --dry-run` would.
echo '{"ok": true, "dry_run": true, "demo": true}'
"""


def prepare_dir(root: Path) -> Path:
    """The demo's OI_DATA_DIR: settings.json (demo values) + the vocal stub. Returns the stub's path."""
    refuse_live(root)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    stub = root / "vocal-send-stub"
    stub.write_text(VOCAL_STUB)
    stub.chmod(0o700)
    settings = {
        "paused": False, "hud_enabled": True,
        "repo_dir": str(root),                       # no real `oi` under it: the default vocal binary is absent
        "gate": {"ttl_ms": TTL_MS},
        "hud": {"auto_mode": "hint", "dwell_ms": 500, "long_press_ms": 600, "hint_ttl_ms": 4000},
        "claude": {"enabled": False}, "shots": {"enabled": False},
        "vocal_recipients": {},
    }
    s = root / "settings.json"
    s.write_text(json.dumps(settings, indent=2) + "\n")
    s.chmod(0o600)
    return stub


def demo_env(root: Path, stub: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k not in SECRET_ENV and not k.startswith(SECRET_ENV_PREFIXES)}
    env.update(OI_DATA_DIR=str(root), OI_VOCAL_DRY_RUN="1", OI_VOCAL_BIN=str(stub), OI_HUD_AGENTS=AGENTS_SPEC)
    return env


def find_hud_bin(explicit: str | None) -> Path:
    for c in (explicit, os.environ.get("OI_DEMO_HUD_BIN"), str(PKG / "hud" / ".build" / "release" / "oi-hud")):
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return Path(c)
    raise DemoError("no oi-hud build: cd projects/os-intent/hud && swift build -c release "
                    "(or --hud-bin PATH). The installed live bundle is never used.")


class DemoHUD:
    """A private oi-hud: `serve` on <root>/hud.sock with the demo env; stopped (SIGTERM) on exit."""

    def __init__(self, bin_path: Path, root: Path, stub: Path):
        self.sock = root / "hud.sock"
        self.proc = subprocess.Popen([str(bin_path), "serve", "--socket", str(self.sock), "--min-interval-ms", "300"],
                                     env=demo_env(root, stub), stdout=subprocess.DEVNULL,
                                     stderr=open(root / "oi-hud.log", "w"), start_new_session=True)
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if self.sock.exists():
                return
            if self.proc.poll() is not None:
                raise DemoError(f"oi-hud exited ({self.proc.returncode}), see {root / 'oi-hud.log'}")
            time.sleep(0.05)
        self.stop()
        raise DemoError("oi-hud did not open its socket in 8 s")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---- CLI -------------------------------------------------------------------------------------------------------

def countdown(d: Demo, s: float, label: str) -> None:
    n = int(round(s))
    for i in range(n, 0, -1):
        d.out(f"  {label} in {i}…")
        d.sleep(1.0)


def run(names: list[str], conn: Conn, *, speed: float = 1.0, lead_s: float = 3.0, gap_s: float = 4.0,
        step: bool = False, out: Callable[[str], None] = print,
        on_scene: Callable[[str], None] | None = None) -> Demo:
    d = Demo(conn, speed=speed, out=out)
    d.on_scene = on_scene
    for i, name in enumerate(names):
        shot = next(s[2] for s in SCENES if s[0] == name)
        out(f"== {name} — {shot}")
        if step:
            input("  press Enter to start (start the recorder first)… ")
        countdown(d, lead_s if i == 0 else gap_s, name)
        run_scene(d, name)
    return d


def backdrop_opener() -> Callable[[str], None]:
    """Opens each scene's fake page (demo/backdrops/*.html) in the default browser, once per change."""
    last = [None]

    def open_for(name: str) -> None:
        page = next(s[3] for s in SCENES if s[0] == name)
        if page != last[0]:
            last[0] = page
            subprocess.run(["open", str(BACKDROPS / page)], check=False)
            time.sleep(1.0)
    return open_for


def main_demo(args: argparse.Namespace) -> int:
    if args.scene == "list":
        for name, fn, shot, backdrop in SCENES:
            print(f"{name:11} {shot}  (backdrop: demo/backdrops/{backdrop})")
        return 0
    names = SCENE_NAMES if args.scene == "all" else CONTRAST if args.scene == "contrast" else [args.scene]
    hud = root = None
    try:
        refuse_live_hud(args.force)
        if args.socket:
            refuse_live(Path(args.socket).parent)
            sock = args.socket
        else:
            root = Path(tempfile.mkdtemp(prefix="oi-demo-", dir="/tmp"))
            stub = prepare_dir(root)
            hud = DemoHUD(find_hud_bin(args.hud_bin), root, stub)
            sock = str(hud.sock)
            print(f"demo HUD: {sock} (data dir {root}, fake data only, nothing is sent)")
        conn = Conn(sock)
        try:
            run(names, conn, speed=args.speed, lead_s=args.lead, gap_s=args.gap, step=args.step,
                on_scene=backdrop_opener() if args.backdrop else None)
        finally:
            conn.close()
        return 0
    except DemoError as e:
        print(f"oi demo: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        if hud:
            hud.stop()
        if root and not args.keep:
            shutil.rmtree(root, ignore_errors=True)


def register(sub) -> None:
    sp = sub.add_parser("demo", help="scripted HUD scenes for screen recording (fake data only, nothing sent)")
    sp.add_argument("scene", choices=SCENE_NAMES + ["contrast", "all", "list"])
    sp.add_argument("--socket", help="drive an already-running demo oi-hud (tests); default: start a private one")
    sp.add_argument("--hud-bin", help="oi-hud binary (default $OI_DEMO_HUD_BIN, else hud/.build/release/oi-hud)")
    sp.add_argument("--lead", type=float, default=3.0, help="countdown before the first scene (s)")
    sp.add_argument("--gap", type=float, default=4.0, help="countdown between scenes (s)")
    sp.add_argument("--speed", type=float, default=1.0, help="timing factor (2 = twice as slow)")
    sp.add_argument("--step", action="store_true", help="wait for Enter before each scene")
    sp.add_argument("--backdrop", action="store_true", help="open each scene's fake backdrop page (browser)")
    sp.add_argument("--force", action="store_true", help="start even if the live HUD is enabled (both would answer)")
    sp.add_argument("--keep", action="store_true", help="keep the temp data dir (oi-hud.log) after the run")
    sp.set_defaults(func=main_demo)
