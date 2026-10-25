"""`oi live` (W8): Markov predictions shown on the HUD during real use.

Every `live.poll_s` (2 s): pull the new aw events (incremental, into the data dir), re-tokenise only the
tail of today (the full day is rebuilt from raw/ at start and every `live.rebuild_s`), and on a CONTEXT
CHANGE (a new activate / url / space token; afk ignored; nothing while the front app is denylisted):
build the current moment, route the form (A or C), build the candidates with the Markov model, apply the
gate (settings.json `gate`) and send `show` to oi-hud's socket. The reply (pressed k / dismissed / dropped)
is read on a thread, so the loop never blocks on the HUD. On `pressed k` the chosen primitive is executed:

  app.open   -> open -a "<App>"
  url.open   -> open -a Safari "<url>"   (the last full http(s) URL seen in Safari for that host/slug,
                                         else https://<host>/)
  space.move -> osascript ctrl+→ / ctrl+← (key code 124/123), only when System Events accepts keystrokes
                (Accessibility); otherwise skipped and logged
  nothing    -> no-op.  Nothing else is ever executed (no title/key primitives, no cloud call).

The Markov model is refitted from all token days every `live.retrain_s` on a thread (swap = one assignment).
Every evaluated moment lands in hud-log/<UTC day>.jsonl (see `log_row`); oi.normalise turns the shown rows
into `hud:shown` / `hud:pressed:<k>` / `hud:dismissed` tokens. A status file (status/oi-live.json) is
rewritten every loop for oi-menubar. Logs never contain titles or URLs.

W10 summon: a reader thread keeps ONE persistent connection to oi-hud's socket and listens for
the `{"event":"summon"}` broadcast (⌥+⌘ pressed together, W11). It answers at once with a show built from the current
moment: Markov top-k with NO threshold and NO cooldown (max_show still applies; when the model has
nothing, an empty show = the HUD's wish option alone). The hud-log row carries `"summoned": true`.
The regular context-change gate path is unchanged.

W12: the status file carries `context` {app, host, desk} of the last evaluated moment (no title, no
URL path), read by `oi vocal-send` for the vocal's context line. A `pressed` reply's `via` ("key" | "click",
The user 16:5x: the capsules are clickable) is kept on the hud-log row. hud-log also holds `oi vocal-send`'s rows
({"kind": "vocal"}): they are not moments (the counters skip them).

W13 Laya layer. With settings `live.laya_shadow` true (or `live.laya_rank` blend|live), every evaluated
moment (gate path and summon) also gets a Laya prediction on the same form, menu and rendered state: the call
runs on one worker thread (oi.laya, `live.laya_timeout_ms`; at most one call running and one queued, else
`busy`; a state already asked is not asked again), never delays a show, and its result lands in the moment's
hud-log row as `laya` {probs per option key, latency_ms, checkpoint, markov_top, markov_probs, tokens (key ->
token, for `oi laya-report`)} or {error, checkpoint}. The row is written when both the HUD outcome and the Laya
call are done. A server found down triggers `laya.ensure_server()` at most every 10 min (memory rule inside).
`live.laya_rank` (shadow | blend | live; the user's switch, default shadow) says what Laya does to
the HUD. Markov always answers first: with blend|live a show ranks on Laya's (or the 50/50 blend's)
probabilities only if Laya's answer for the SAME state (digest + menu) is already in (`laya_rank` in the row;
typically a summon or a re-evaluation seconds after the moment changed), else on Markov's
(`laya_rank_fallback` = pending | the Laya error). Nothing ever waits for Laya.

W15 glide: oi-hud tells the show's owner (this driver, on the show's connection) `{"event":"glide"}` when a
⌥⌘ hold starts over its options (-> one batched haiku prefetch of their follow-ups) and `{"event":"expand","path":[…]}`
when the pointer rests on an option (-> `subring` command); a release answers `pressed` with `path` (+ the glide
log). See oi/glide.py. A follow-up runs through the same executor (app / url / desk / plain mail on the mail it was
built for), the W14 Claude variant (draft, summary) or `oi.vocal.send` (a message to an agent, "Ask Atlas"). The
row's `glide` field keeps paths, dwell times and the action id; oi.normalise adds a `hud:glide:<depth>` token.
`oi live --dry-exec` sends the shows but executes nothing (prints what would run; for functional tests).
W16 page actions (oi/page_actions.py). In Safari, `ui.press` options: learned clicks (form C
`press_button`, Markov on `click:` tokens) and, on a summon, the rule-based key button of the page (GitHub
"Squash and merge", ...), pinned first. Each is offered only if oi-hud's page snapshot (Accessibility) shows
the element NOW; its hint outlines the button. A press (one tap) that is not known read-only waits for the
outward gate's panel (oi/gate.py: the one confirmation, which also issues oi-hud's press ticket). A press sends
`ax_press` (role + label slug, re-found at press time, never a coordinate) to oi-hud; a gone element presses
nothing. The hud-log row gets `ui_press` {ok, reason}.

W19 hints: with settings `hud.auto_mode` "hint" (default) a GATED show goes out as `"mode":"hint"` (+
`hint_ttl_ms`): oi-hud draws tiny coloured dots only, and ⌥⌘ develops them into the full cross on the same
connection (it says `{"event":"hint","phase":"expanded"}` first). "full" = the cross as before, "off" = nothing
is sent (row `dropped:auto_off`). A summon is always full. The row keeps `show_mode` ("hint" | "full") and, for a
hint, `expanded` (true when ⌥⌘ developed it): the signal of hints that get developed.

(docs/DESIGN.md "context is state, not timers"): a LIVE context state is part of the current context exactly while
it is live. oi.shots keeps the screenshot one (its thumbnail / markup window on screen, a window-less or clipboard
one until the context changes, a clipboard one also until the clipboard changes). While it is live, `current_moment()` has its
`shot:new` row as the latest action (Markov's h1) and `context["live"] = ["shot"]` (Laya's state text says so), and
its options (`shot.send` / `shot.content` / `shot.pick` / `shot.annotate`, `live: "shot"`) join the ONE pool the gate
and the summon rank, first. Its appearing shows them at once (`summon(trigger="shot")`, no gate); a show holding them
goes out full with `"live":"shot"` and is taken down
(`hide`) when the state ends; Claude's content actions redraw it in place when they come. A press runs through
`Shots.press` (tap = tier 1, long-press = tier 2 note); the row's options carry `outward` for what can leave the Mac.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import URLError

from . import aw, candidates, claude_actions, drill, forms, glide, hud_keys, laya, normalise, page_actions, paths, serialise, settings, vocab
from . import vocal as vocal_mod
from . import gate as outward_gate   # the outward gate
from .proactive.engine import OUTWARD_PRIMITIVES
from . import shots as shots_mod   # the screenshot as live context state
from . import intent_resolve   # W18
from . import starter          # W22: starter-catalogue priors
from .markov import Markov, latest_model_path
from .moments import FEEDBACK_PREFIXES, NOTHING, build_moments

ALLOWED_PRIMITIVES = ("app.open", "url.open", "space.move", "mail.reply", "mail.forward",   # W14: mail (form B)
                      page_actions.PRIMITIVE,                                               # W16: ui.press
                      "mail.draft", "agent.ask",                                            # W22: starter items
                      "shot.send", "shot.pick", "shot.annotate", "shot.content")            # a live screenshot
# W24: a proactive hint's actions also use mail.show (that mail in Mail), file.open / folder.open / telegram.chat
# (the W23 primitives); they come from the event only, never from the Markov menu.
# Outward gate: a W24 option marked `outward` asks at the pick ("reply"), except agent.ask (gated at its vocal send).
OUTWARD_AT_PICK = OUTWARD_PRIMITIVES - {"agent.ask"}
CONTEXT_KINDS = ("activate", "url", "space")
TAIL_ACTIVATES = 2          # the tail re-tokenised every loop starts at the 2nd-to-last app switch
TTL_MS = 6000               # fallback (W19: was 15000); settings.json gate.ttl_ms (W9) wins, clamped
TTL_RANGE_MS = (1500, 60000)
REPLY_SLACK_S = 30          # the HUD may linger while the pointer is in reach of the options
STALE_S = 15                # oi-menubar: a status older than this means the driver is stopped
SPACE_KEY = {1: 124, -1: 123}   # ctrl+→ next desk, ctrl+← previous desk
MAX_SPACE_STEPS = 8
AX_RECHECK_S = 600          # a refused Accessibility probe is retried after this (grant without restart)
AW_DOWN_LOG_S = 60          # "aw-server unreachable" is logged at most once per minute
LAYA_ENSURE_S = 600         # W13: a Laya server found down is (re)started at most this often
LAYA_MAX_INFLIGHT = 2       # W13: one shadow call running + one queued; more = `busy`, never a backlog
LAYA_CACHE = 8              # Laya answers kept per state digest, for a re-rank without waiting


def show_p(o: dict) -> float:
    """The probability the gate ranks on: Laya's / the blend's when promoted (W13 `p_show`), else Markov's."""
    v = o.get("p_show", o.get("p_markov"))
    return float(v or 0.0)


def now_iso(t: float | None = None) -> str:
    return vocab.iso_utc(datetime.fromtimestamp(time.time() if t is None else t, timezone.utc))


LAST_HOST_MAX_AGE_S = 300   # a remembered browser host older than this is never used (stale tab)


def _row_epoch(ts) -> float | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def status_context(ctx: dict, rows: list[dict] | None = None, now: float | None = None) -> dict:
    """W12: the vocal context of a moment: app, page HOST only (never the slug/path or a title), desk.
    `last_host` = the host of the newest url token of the same app, when the moment has none. Back on the
    same Safari tab, oi-watcher logs no new url (it only logs a change) and `activate:` clears the moment's url;
    oi.vocal offers the screenshot on it only for a settings safe host."""
    url = ctx.get("url") if isinstance(ctx.get("url"), str) else None
    app = ctx.get("app") if isinstance(ctx.get("app"), str) else None
    out = {"app": app, "host": url.split("/", 1)[0] if url else None,
           "desk": ctx.get("space") if isinstance(ctx.get("space"), int) else None}
    if not url and app:
        # Newest url token of THIS browser, but only if no other browser came to the front since (its tab
        # is a different page) and it is at most LAST_HOST_MAX_AGE_S old (a stale host must not make an
        # unrelated or private window look like a safe page).
        for r in reversed(rows or []):
            tok, rapp = r.get("token", ""), r.get("app") if isinstance(r.get("app"), str) else ""
            if tok.startswith("activate:") and rapp.casefold() != app.casefold() \
                    and rapp.casefold() in vocal_mod.BROWSERS:
                break
            if tok.startswith("url:"):
                t = _row_epoch(r.get("ts"))
                fresh = t is not None and (time.time() if now is None else now) - t <= LAST_HOST_MAX_AGE_S
                if rapp.casefold() == app.casefold() and fresh:
                    out["last_host"] = tok[4:].split("/", 1)[0]
                break
    return out


def utc_day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).date().isoformat()


def log(msg: str) -> None:
    print(f"{now_iso()} oi-live: {msg}", file=sys.stderr, flush=True)


# ---- gate ------------------------------------------------------------------------------------------

def summon_options(menu: list[dict], cfg: dict) -> list[dict]:
    """The summon menu: every allowed option (form fixed options included), best Markov p first, at most
    max_show (<= 4). No threshold, no cooldown, no nothing/thin-history check: the user asked. the options of
    a live context state (`live`, e.g. the screenshot on screen) come first."""
    opts = [o for o in menu if o.get("primitive") in ALLOWED_PRIMITIVES]
    # W14: pinned (form B mail) first; W16: pinned page rules keep their rule `rank` (the primary action, e.g.
    # Squash & merge, before Approve) whatever a probability says (incident 2026-10-21)
    opts.sort(key=lambda o: (not o.get("live"), not o.get("pin"), o.get("rank", 0), -show_p(o), o["key"]))
    return opts[: max(0, min(int(cfg["max_show"]), 4))]


def gate(menu: list[dict], nothing_p: float, cfg: dict, *, now: float, last_show_at: float | None,
         paused: bool, hud_enabled: bool, support: int, hint: bool = False) -> tuple[list[dict], str]:
    """(options to show, reason). reason == "show" iff the list is non-empty. Order of the checks:
    paused, hud_disabled, thin_history (the current context seen < min_history times in training),
    nothing_likely (P(nothing) > none_max_p), no_option (no allowed option with p >= show_min_p),
    cooldown (< cooldown_s since the last show; W20: < hint_cooldown_s when the show goes out as a `hint`).
    At most max_show (<= 4, the HUD's slots) options."""
    if paused:
        return [], "paused"
    if not hud_enabled:
        return [], "hud_disabled"
    if support < cfg["min_history"]:
        return [], "thin_history"
    if nothing_p > cfg["none_max_p"]:
        return [], "nothing_likely"
    opts = [o for o in menu if o.get("primitive") in ALLOWED_PRIMITIVES and show_p(o) >= cfg["show_min_p"]]
    opts.sort(key=lambda o: (not o.get("live"), -show_p(o), o["key"]))   # a live state's options first
    opts = opts[: max(0, min(int(cfg["max_show"]), 4))]
    if not opts:
        return [], "no_option"
    cooldown = cfg.get("hint_cooldown_s", settings.GATE_DEFAULTS["hint_cooldown_s"]) if hint else cfg["cooldown_s"]
    if last_show_at is not None and now - last_show_at < cooldown:
        return [], "cooldown"
    return opts, "show"


# ---- context change ------------------------------------------------------------------------------

def _kind(token: str) -> str:
    return token.split(":", 1)[0]


class ContextTracker:
    """A context change = a new activate/url/space token newer than the last one handled."""

    def __init__(self):
        self.last: tuple[str, str] | None = None   # (ts, token) of the newest context token handled

    @staticmethod
    def newest(rows: list[dict]) -> dict | None:
        for r in reversed(rows):
            if _kind(r["token"]) in CONTEXT_KINDS:
                return r
        return None

    def prime(self, rows: list[dict]) -> None:
        r = self.newest(rows)
        self.last = (r["ts"], r["token"]) if r else None

    def change(self, rows: list[dict]) -> dict | None:
        r = self.newest(rows)
        if r is None or (self.last is not None and (r["ts"], r["token"]) <= self.last):
            return None
        self.last = (r["ts"], r["token"])
        return r


# ---- options -> HUD JSON --------------------------------------------------------------------------

def app_path(app: str) -> str | None:
    for d in candidates.APP_DIRS:
        p = os.path.join(os.path.expanduser(d), f"{app}.app")
        if os.path.isdir(p):
            return p
    return None


def space_target(token: str, current: int | None) -> tuple[int | None, int | None]:
    """(delta, target desk) of a space token given the current desk (None when unknown)."""
    mv = token.split(":", 1)[1]
    if mv in ("+1", "-1"):
        d = int(mv)
        return d, (max(1, current + d) if current is not None else None)
    n = int(mv[1:])
    return (n - current if current is not None else None), n


def hud_hint(opt: dict, ctx: dict) -> dict | None:
    """W11: what the option acts on, drawn by oi-hud in the option's colour while the HUD is visible.
    app.open -> the app's window; url.open -> Safari's window (pages open in Safari); space.move -> a bar on
    the screen edge in the slide direction (right for a move to a higher desk, left for a lower one; none
    when the direction is unknown or zero). Anything else (nothing, wish) -> no hint."""
    prim, params = opt.get("primitive"), opt.get("params") or {}
    if prim == "app.open":
        app = (params.get("app") or "").strip()
        return {"kind": "window", "app": app} if app else None
    if prim == "url.open":
        return {"kind": "window", "app": "Safari"}
    if prim == "space.move":
        delta, _ = space_target(opt.get("token") or "", ctx.get("space"))
        if delta:
            return {"kind": "edge", "dir": "right" if delta > 0 else "left"}
    return None


def hud_option(key: int, opt: dict, ctx: dict, resolve_app: Callable[[str], str | None] = app_path) -> dict:
    tok, prim, params = opt.get("token") or "", opt["primitive"], opt.get("params") or {}
    if opt.get("shot"):                        # a live screenshot's option (label + icon from oi.shots)
        out = {"key": key, "label": str(opt.get("label") or "?")[:22], "icon": opt.get("icon") or "sf:camera",
               "confidence": round(show_p(opt), 3)}
        if opt.get("claude"):
            out["claude"] = True
        return out
    if opt.get("evt"):                         # W24: a proactive event's action carries its own label + icon
        out = {"key": key, "label": str(opt.get("label") or "?")[:22], "icon": opt.get("icon") or "sf:bell",
               "confidence": round(show_p(opt), 3)}
        if opt.get("claude") and not opt.get("private") and claude_actions.offers(prim):
            out["claude"] = True
        return out
    if prim == page_actions.PRIMITIVE:          # W16: label, icon, confirm, target, element hint
        return {"key": key, "confidence": round(show_p(opt), 3), **page_actions.hud_fields(opt)}
    if prim == "app.open":
        app = params.get("app", "")
        path = resolve_app(app)
        label, icon = app[:12], (f"app:{path}" if path else "sf:app")   # compact: the icon says "switch" (the user 16:40 Paris)
    elif prim == "url.open":
        host = tok.split(":", 1)[1].split("/", 1)[0].removeprefix("www.")
        label, icon = (opt.get("label") or host)[:22 if opt.get("label") else 14], "sf:safari"   # W22: starter label
    elif prim == "mail.draft":                 # W22: a starter template (a draft window, never sent)
        label, icon = (opt.get("label") or "New mail")[:22], opt.get("icon") or "sf:square.and.pencil"
    elif prim == "agent.ask":                  # W22: "Ask <agent>" = the vocal panel addressed to that agent
        to = str(params.get("to") or "?")
        label, icon = (opt.get("label") or f"Ask {to.title()}")[:22], f"initial:{to[:1].upper()}"
    elif prim in claude_actions.MAIL_LABELS:   # W14: "Reply" / "Forward" (the label is enriched after the show)
        label, icon = claude_actions.MAIL_LABELS[prim]
    else:  # space.move
        delta, target = space_target(tok, ctx.get("space"))
        label = str(target) if target is not None else ("→" if delta == 1 else "←")
        icon = "sf:arrow.right.square" if delta == 1 else "sf:arrow.left.square"
    out = {"key": key, "label": label, "icon": icon, "confidence": round(show_p(opt), 3)}
    if claude_actions.offers(prim):            # W14: a long-press runs the Claude-assisted variant
        out["claude"] = True
    hint = hud_hint(opt, ctx)
    if hint:
        out["hint"] = hint
    return out


def show_ttl_ms(cfg: dict) -> int:
    """settings.json gate.ttl_ms for this show (TTL_MS when absent), clamped to TTL_RANGE_MS."""
    v = cfg.get("ttl_ms", TTL_MS) if isinstance(cfg, dict) else TTL_MS
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        v = TTL_MS
    return int(min(max(v, TTL_RANGE_MS[0]), TTL_RANGE_MS[1]))


def hud_payload(options: list[dict], ctx: dict, ttl_ms: int = TTL_MS,
                resolve_app: Callable[[str], str | None] = app_path, keys: list[int] | None = None) -> dict:
    """`options` ranked (most likely first); `keys` = their key numbers (W21: fn 1, ⌃ 2, ⌥ 3, ⌘ 4; default the
    fill order ⌃ ⌥ ⌘ fn, see oi/hud_keys.py). The payload keeps the rank order."""
    keys = keys or hud_keys.fill_keys()[:len(options)]
    return {"cmd": "show", "options": [hud_option(k, o, ctx, resolve_app) for k, o in zip(keys, options)],
            "ttl_ms": ttl_ms}


# ---- executor -------------------------------------------------------------------------------------

def _run(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return p.returncode, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def safe_url(url: str | None) -> str | None:
    """scheme://host[:port]/path of an http(s) URL: query string and fragment dropped (they can carry
    tokens or session ids); None for anything else."""
    try:
        u = urllib.parse.urlsplit(url or "")
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path or "/", "", ""))


def mailto(params: dict) -> str:
    """W22: mailto:<to>?subject=…&body=… for a starter mail template (opens a draft; Mail never sends it)."""
    q = {k: params[k] for k in ("subject", "body") if isinstance(params.get(k), str) and params[k]}
    to = params.get("to") if isinstance(params.get("to"), str) else ""
    return "mailto:" + urllib.parse.quote(to, safe="@.") + ("?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote) if q else "")


class Executor:
    """Runs the three safe primitives through `runner(argv) -> (returncode, stderr)` (a fake in tests)."""

    def __init__(self, runner: Callable[[list[str]], tuple[int, str]] = _run,
                 clock: Callable[[], float] = time.monotonic, osa: Callable | None = None,
                 ui_press: Callable[[dict], bool] | None = None, hud_cmd: Callable[[dict], dict] | None = None):
        self.runner = runner
        self.hud_cmd = hud_cmd            # W22: agent.ask -> oi-hud {"cmd":"vocal_to"} (None: never)
        self.clock = clock
        self.osa = osa                    # the mail primitives' AppleScript runner (None = osascript)
        self.ui_press = ui_press          # W16: oi-hud re-finds + AXPresses (None: never pressed)
        self._ax: bool | None = None
        self._ax_at = 0.0

    def select_mail(self, message_id: str) -> bool:
        """W24: select (and show) the message with that Message-ID in Mail's viewer. Read-only."""
        rc, out = (self.osa or claude_actions.osascript)(claude_actions.SELECT_MAIL, [message_id])
        return rc == 0 and out.startswith("OK")

    def ax_ok(self) -> bool:
        """Can we send keystrokes via System Events? Probed lazily (no prompt unless a desk move is pressed);
        a "granted" is kept, a refusal is re-probed after AX_RECHECK_S (Accessibility granted later)."""
        if self._ax is None or (not self._ax and self.clock() - self._ax_at >= AX_RECHECK_S):
            self._ax_at = self.clock()
            rc, err = self.runner(["osascript", "-e", 'tell application "System Events" to keystroke ""'])
            self._ax = rc == 0
            if not self._ax:
                log("space.move disabled: System Events refused keystrokes"
                    + (" (no Accessibility)" if ("-1719" in err or "-25211" in err) else f" (rc {rc})"))
        return self._ax

    def run(self, opt: dict | None, ctx: dict, url_for: Callable[[str], str], message_id: str = "") -> str | None:
        """Execute; returns the executed token, or None (nothing / skipped / failed). `message_id` (W15): a mail
        primitive opens only on that mail (the one a glide follow-up was built for)."""
        if not opt:
            return None
        prim, params, tok = opt.get("primitive"), opt.get("params") or {}, opt.get("token")
        if prim == "app.open" and params.get("app"):
            dl = params.get("deeplink")                 # W22: a starter's app deep link (custom scheme only)
            rc, _ = self.runner(["open", "-a", params["app"]] + ([dl] if isinstance(dl, str) and dl else []))
        elif prim == "url.open" and (params.get("url") or params.get("href")):
            href = safe_url(starter.safe_href(params.get("href")))   # W22: a starter's own link (no query)
            href = href or (url_for(params["url"]) if params.get("url") else None)
            if not href:
                return None
            rc, _ = self.runner(["open", "-a", "Safari", href])
        elif prim == "mail.draft":                     # W22: mailto: -> a compose window, never sent
            rc, _ = self.runner(["open", mailto(params)])
        elif prim == "agent.ask" and params.get("to"):  # W22: oi-hud opens the recording panel to that agent
            r = self.hud_cmd({"cmd": "vocal_to", "pick": params["to"]}) if self.hud_cmd else {}
            return tok if (r or {}).get("event") == "ok" else None
        elif prim == page_actions.PRIMITIVE:            # W16: role + label re-found by oi-hud at press time
            ok = bool(self.ui_press and page_actions.valid_params(params) and self.ui_press(params))
            return tok if ok else None
        elif prim == "mail.show" and params.get("message_id"):   # W24: show that mail in Mail (read-only)
            return tok if self.select_mail(params["message_id"]) else None
        elif prim in claude_actions.MAIL_PRIMITIVES:   # W14: an empty reply / forward window, never sent
            if params.get("message_id"):               # W24: a proactive mail: the window on IT (by id, not selected)
                message_id = params["message_id"]
            return prim if claude_actions.plain_mail(prim, self.osa, message_id) else None
        elif prim in ("folder.open", "file.open"):     # W23: a provider path, re-checked (Finder / default app)
            if not drill.safe_path(params.get("path")) or drill.launchable(params.get("path")):
                return None                             # never run / install (scripts, apps, installers, +x)
            rc, _ = self.runner(["open", params["path"]])
        elif prim == "telegram.chat" and drill.bot_name(params.get("bot")):   # a fleet bot's chat: its deep link
            rc, _ = self.runner(["open", f"tg://resolve?domain={params['bot']}"])
        elif prim == "telegram.chat" and params.get("chat"):   # W23: open that chat (AX press of its row)
            rc, _ = self.runner(["osascript", "-l", "JavaScript", "-e", drill.TELEGRAM_TITLES_JS, params["chat"]])
        elif prim == "music.play" and params.get("playlist"):  # W23: a playlist, or a track of it
            rc, _ = self.runner(["osascript", "-e", drill.MUSIC_PLAY_AS, params["playlist"], params.get("track", "")])
        elif prim == "space.move" and tok:
            delta, _ = space_target(tok, ctx.get("space"))
            if not delta or abs(delta) > MAX_SPACE_STEPS:
                return None
            if not self.ax_ok():
                log("space.move pressed but skipped (no Accessibility)")
                return None
            code = SPACE_KEY[1 if delta > 0 else -1]
            rc = 0
            for _ in range(abs(delta)):
                rc, _ = self.runner(["osascript", "-e",
                                     f'tell application "System Events" to key code {code} using control down'])
                if rc:
                    break
        else:
            return None
        return tok if rc == 0 else None


# ---- HUD socket -----------------------------------------------------------------------------------

def socket_sender(path: Path, timeout_s: float) -> Callable[..., threading.Thread]:
    """send(payload, on_reply, on_event=None) -> thread. The reply is the first pressed/dismissed/dropped/error line
    (wish broadcasts are skipped); a socket problem gives {"event":"dropped","reason":"socket_error"}. W15: the
    `glide` / `expand` lines of the show (before its reply) go to on_event, each on its own thread."""
    def send(payload: dict, on_reply: Callable[[dict], None],
             on_event: Callable[[dict], None] | None = None) -> threading.Thread:
        def work():
            reply = {"event": "dropped", "reason": "no_reply"}
            expanded = False   # W19
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(timeout_s)
                    s.connect(str(path))
                    s.sendall((json.dumps(payload) + "\n").encode())
                    buf = b""
                    done = False
                    while not done:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf and not done:
                            line, buf = buf.split(b"\n", 1)
                            try:
                                ev = json.loads(line)
                            except ValueError:
                                continue
                            if isinstance(ev, dict) and ev.get("event") in ("pressed", "dismissed", "dropped", "error"):
                                reply, done = ev, True
                                if expanded:                                      # W19: the hint was developed
                                    reply = dict(reply, expanded=True)
                            elif isinstance(ev, dict) and ev.get("event") == "hint" and ev.get("phase") == "expanded":
                                expanded = True                                   # W19
                            elif isinstance(ev, dict) and ev.get("event") in ("glide", "expand") and on_event:
                                threading.Thread(target=on_event, args=(ev,), daemon=True, name="oi-glide").start()
            except (OSError, ValueError):
                reply = {"event": "dropped", "reason": "socket_error"}
            on_reply(reply)
        t = threading.Thread(target=work, daemon=True)
        t.start()
        return t
    return send


def summon_listener(path: Path, on_summon: Callable[[dict], None], *, retry_s: float = 5.0,
                    stop: threading.Event | None = None,
                    on_event: Callable[[dict, Callable[[dict], None]], None] | None = None,   # W18
                    hello: list[dict] | None = None) -> threading.Thread:
    """A daemon thread holding one persistent connection to the HUD socket; every `summon` broadcast
    calls on_summon(event). Reconnects every `retry_s` while oi-hud is down; other lines are ignored.
    W18: `hello` lines are sent on each (re)connect (`{"cmd":"resolver"}`), and every other event goes
    to on_event(event, reply) where reply(cmd) writes one command line back on this same connection."""
    stop = stop or threading.Event()

    def work():
        while not stop.is_set():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(str(path))
                    # W18: register as the wish resolver; replies share the connection (one lock for writes)
                    wlock = threading.Lock()

                    def reply(cmd: dict, s=s, wlock=wlock) -> None:
                        try:
                            with wlock:
                                s.sendall((json.dumps(cmd, ensure_ascii=False) + "\n").encode())
                        except OSError:
                            pass
                    for h in hello or []:
                        reply(h)
                    # /W18
                    s.settimeout(1.0)
                    buf = b""
                    while not stop.is_set():
                        try:
                            chunk = s.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            try:
                                ev = json.loads(line)
                            except ValueError:
                                continue
                            if isinstance(ev, dict) and ev.get("event") == "summon":
                                try:
                                    on_summon(ev)
                                except Exception:
                                    log("summon failed: " + traceback.format_exc().splitlines()[-1])
                            elif isinstance(ev, dict) and on_event is not None:   # W18: resolve requests
                                try:
                                    on_event(ev, reply)
                                except Exception:
                                    log("event failed: " + traceback.format_exc().splitlines()[-1])
            except OSError:
                pass
            stop.wait(retry_s)
    t = threading.Thread(target=work, daemon=True, name="oi-summon")
    t.start()
    return t


# ---- the driver -----------------------------------------------------------------------------------

def state_digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def append_jsonl(path: Path, row: dict, lock: threading.Lock) -> None:
    with lock:
        paths.ensure_private_root()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class Live:
    def __init__(self, *, dry_run: bool = False, socket_path: Path | None = None,
                 runner: Callable[[list[str]], tuple[int, str]] = _run,
                 sender: Callable | None = None, clock: Callable[[], float] = time.time,
                 out=None, pull: bool = True, resolve_app: Callable[[str], str | None] = app_path,
                 is_executable: Callable[[dict], bool] = candidates.default_is_executable,
                 laya_client: Callable[[dict], object] = laya.LayaClient.from_settings,
                 ensure_laya: Callable[..., tuple] = laya.ensure_server,
                 claude: "claude_actions.ClaudeActions | None" = None,
                 glide_engine: "glide.GlideEngine | None" = None, exec_dry_run: bool = False,
                 vocal_send: Callable[..., dict] = vocal_mod.send):
        self.dry_run = dry_run
        self.exec_dry_run = exec_dry_run      # W15 functional tests: shows go out, nothing is executed
        self.clock = clock
        self.out = out or sys.stdout
        self.do_pull = pull
        self.resolve_app = resolve_app
        self.is_executable = is_executable
        dry_osa = None
        if exec_dry_run:
            def dry_argv(argv: list[str]) -> tuple[int, str]:
                print(json.dumps({"dry_exec": argv}, ensure_ascii=False), file=self.out, flush=True)
                return 0, ""

            def dry_osa(script: str, args: list[str], timeout: float = 6) -> tuple[int, str]:
                print(json.dumps({"dry_exec": "osascript", "args": args}, ensure_ascii=False), file=self.out, flush=True)
                return 0, "OK"
            runner = dry_argv
        self.socket_path = socket_path or paths.hud_socket()
        self.page = page_actions.PageClient(self.socket_path)        # W16: page snapshot + press via oi-hud
        self.last_press: dict | None = None
        self.starters = starter.Starters(clock=clock)              # W22
        self.executor = Executor(runner, osa=dry_osa, ui_press=self._ui_press, hud_cmd=self._hud_cmd)
        self.sender = sender or socket_sender(self.socket_path, TTL_RANGE_MS[1] / 1000 + REPLY_SLACK_S)
        self._sender_events = "on_event" in inspect.signature(self.sender).parameters
        self.claude = claude or claude_actions.ClaudeActions(   # W14
            self.socket_path, **({"dry_run": True, "osa": dry_osa, "out": self.out} if exec_dry_run else {}))
        self.glide = glide_engine or glide.GlideEngine(   # W15; --dry-exec never reads Mail either
            getattr(self.claude, "sender", None) or (lambda p: claude_actions.send_command(self.socket_path, p)),
            **({"mail_reader": lambda: None, "body_reader": lambda mid: ""} if exec_dry_run else {}))
        self.vocal_send = vocal_send
        self.lock = threading.Lock()          # hud-log, counters
        self.model: Markov | None = None
        self.model_trained_at: str | None = None
        self._retraining = False
        self.tracker = ContextTracker()
        self.day = ""
        self.raw: dict[str, dict] = {}        # bucket -> {event key: raw row} for today (UTC)
        self.rows: list[dict] = []            # today's tokens (prefix from the last rebuild + live tail)
        self.last_show_at: float | None = None
        self.last_show_ts: str | None = None
        self.keys = hud_keys.KeyMemory()      # W21: the key each option sat on, per context (stability)
        self.shows_today = self.pressed_today = self.moments_today = 0
        self.last_rebuild = self.last_retrain = 0.0
        self.threads: list[threading.Thread] = []
        self.state = "starting"
        self.context: dict = {}               # W12: {app, host, desk} of the last evaluated moment
        self._aw_down_logged = float("-inf")
        # W13 Laya layer
        self.laya_client_factory, self.ensure_laya = laya_client, ensure_laya
        self._laya_client, self._laya_key = None, None
        self._laya_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oi-laya")
        self._laya_inflight = 0
        self._laya_ensure_at = float("-inf")
        self._laya_cache: dict[str, tuple] = {}     # state digest -> (menu keys asked, the call), newest last
        # W18: wish -> action (oi/intent_resolve.py); executes through the same Executor
        self.wish = intent_resolve.WishEngine(executor=self._wish_exec,
                                              dry_run=dry_run or exec_dry_run, out=self.out,
                                              live_context=lambda: self.context,
                                              page_items=self.wish_page_items)   # W16: page buttons as wishes
        self._wish_page: tuple[float, str | None, list[dict]] | None = None
        self.shots = shots_mod.Shots(self)     # the screenshot as live context state
        self.live_show: dict | None = None    # the last show bound to a live state {"done"}
        from .proactive import engine as proactive_engine   # W24: sensors -> relevance -> ring hints
        self.proactive = proactive_engine.Engine(self, clock=clock)

    # -- data -------------------------------------------------------------------------------------
    def rebuild(self) -> None:
        """Full rebuild of today's tokens from raw/ (writes tokens/<day>.jsonl, as `oi tokens` does)."""
        now = self.clock()
        self.day = utc_day(now)
        _, rows = normalise.build_day(self.day)
        self.rows = rows
        self.raw = {b: {aw._key(r): r for r in rs} for b, rs in aw.raw_rows(self.day).items()}
        self.last_rebuild = now

    def pull(self) -> None:
        if not self.do_pull:
            return
        try:
            results = aw.pull(keep_events=True)
        except (URLError, OSError) as e:
            now = time.monotonic()
            if now - self._aw_down_logged >= AW_DOWN_LOG_S:
                self._aw_down_logged = now
                log(f"aw-server unreachable: {getattr(e, 'reason', e)} (logged at most once per {AW_DOWN_LOG_S} s)")
            return
        changed = False
        for r in results:
            b = aw.safe_name(r["bucket"])
            for ev in r.get("events") or []:
                if aw.day_of(ev) != self.day:
                    continue
                k = aw._key(ev)
                bucket = self.raw.setdefault(b, {})
                merged = aw.merge(bucket.get(k), ev)
                if bucket.get(k) != merged:
                    bucket[k], changed = merged, True
        if changed:
            self.retokenise_tail()

    def retokenise_tail(self) -> None:
        """Keep the tokens before the anchor (2nd-to-last app switch), rebuild the rest from raw events
        ending at or after it. Cheap: a few minutes of events instead of the whole day."""
        acts = [r for r in self.rows if r["token"].startswith("activate:")]
        anchor = acts[-TAIL_ACTIVATES]["ts"] if len(acts) >= TAIL_ACTIVATES else (acts[0]["ts"] if acts else None)
        if anchor is None:
            prefix, tail_raw = [], {b: list(rs.values()) for b, rs in self.raw.items()}
        else:
            a = vocab.parse_ts(anchor)
            prefix = [r for r in self.rows if r["ts"] < anchor and not r["token"].startswith(FEEDBACK_PREFIXES)]
            tail_raw = {}
            for b, rs in self.raw.items():
                keep = [r for r in rs.values() if normalise.Ev.of(r).end >= a]
                if keep:
                    tail_raw[b] = keep
        tail = [r for r in normalise.tokens_from_raw(tail_raw)
                if (anchor is None or r["ts"] >= anchor) and r["ts"].startswith(self.day)]
        feedback = [r for r in self.rows if r["token"].startswith(FEEDBACK_PREFIXES)
                    or (r["token"].startswith("evt:") and (anchor is None or r["ts"] >= anchor))]   # W24: not in raw/
        self.rows = sorted(prefix + tail + feedback, key=lambda r: r["ts"])

    def url_for(self, host_slug: str) -> str:
        """The newest full http(s) URL seen in Safari today whose token is url:<host_slug>, else https://<host>/."""
        want = f"url:{host_slug}"
        for b, rs in self.raw.items():
            if normalise.src_of(b) != "oi-safari":
                continue
            for r in sorted(rs.values(), key=aw._sort_key, reverse=True):
                data = r.get("data") or {}
                clean = safe_url(data.get("url"))
                if normalise.url_token(data) == want and clean:
                    return clean
        return f"https://{host_slug.split('/', 1)[0]}/"

    def current_moment(self) -> dict | None:
        """The moment right after the newest token (None when it is inside an afk span). a live screenshot's
        `shot:new` row is in the stream at once (the 2 s pull brings the same row later), and the context's `live`
        lists the live states (["shot"]) for as long as they are live."""
        rows = self.rows
        have = {(r["ts"], r["token"]) for r in rows[-200:]}
        extra = [r for r in self.shots.token_rows() if (r["ts"], r["token"]) not in have]
        if extra:
            rows = sorted(rows + extra, key=lambda r: r["ts"])
        acts = [r for r in rows if not r["token"].startswith(FEEDBACK_PREFIXES)]
        if not acts:
            return None
        ms = build_moments(rows)
        m = ms[-1] if ms and ms[-1]["ts"] == acts[-1]["ts"] else None
        live = sorted(self.shots.live_set())
        if m is not None and live:
            m["context"]["live"] = live
        return m

    # -- model ------------------------------------------------------------------------------------
    def load_model(self) -> None:
        p = latest_model_path()
        if p:
            try:
                self.model = Markov.load(p)
                self.model_trained_at = (self.model.meta or {}).get("trained_at") or now_iso(p.stat().st_mtime)
                return
            except (OSError, ValueError, KeyError):
                log("saved model unreadable; refitting")
        self.retrain()

    def retrain(self) -> None:
        """Fit on every past token day + today's in-memory rows; swap atomically; save models/markov-<day>.json."""
        try:
            rows: list[dict] = []
            for f in sorted(paths.tokens_dir().glob("????-??-??.jsonl")):
                if f.stem != self.day:
                    rows += aw.read_jsonl(f)
            rows += list(self.rows)
            if not rows:
                return
            ms = build_moments(rows)
            at = now_iso(self.clock())
            m = Markov.fit(ms, tokens=[r["token"] for r in rows],
                           meta={"moments": len(ms), "trained_at": at, "by": "oi live"})
            self.model, self.model_trained_at = m, at
            if not self.dry_run:
                m.save(day=datetime.fromtimestamp(self.clock(), timezone.utc).date())
        except Exception:  # never kill the loop over a refit
            log("retrain failed: " + traceback.format_exc().splitlines()[-1])
        try:
            self.starters.refine_from_wishes()       # W22: wishes refine the starter catalogue
        except Exception:
            log("starter refine failed: " + traceback.format_exc().splitlines()[-1])
        finally:
            self._retraining = False

    def _hud_cmd(self, payload: dict) -> dict:
        """W22: one command to oi-hud (agent.ask -> vocal_to); --dry-exec prints it instead."""
        if self.exec_dry_run or self.dry_run:
            print(json.dumps({"dry_exec": payload}, ensure_ascii=False), file=self.out, flush=True)
            return {"event": "ok"}
        return claude_actions.send_command(self.socket_path, payload)

    def maybe_retrain(self, now: float, retrain_s: float) -> None:
        if self._retraining or now - self.last_retrain < retrain_s:
            return
        self._retraining, self.last_retrain = True, now
        threading.Thread(target=self.retrain, daemon=True).start()

    # -- one evaluation ----------------------------------------------------------------------------
    def evaluate(self, st: dict, now: float) -> dict:
        """Gate the current moment; show it (or print it in dry-run). Returns a small result dict."""
        moment = self.current_moment()
        if moment is None:
            return {"reason": "afk"}
        ctx = moment["context"]
        if ctx.get("app") and normalise._denied(ctx["app"], normalise.effective_denylist()):
            self.context = {}
            return {"reason": "denylisted"}   # nothing is logged about a denylisted app
        self.context = status_context(ctx, self.rows)
        self.moments_today += 1
        form = forms.route(ctx)
        model = self.model
        menu = candidates.build(form, moment, model, is_executable=self.is_executable) if model else []
        menu = self.page_menu(menu, ctx, summoned=False)          # W16: ui.press only if on the page now
        probs = model.distribution(moment)[0] if model else {}
        support = 0
        if model:
            h1 = model.features(moment)[1]
            support = sum(model.counts.get("o1", {}).get(h1, {}).values())
        cfg = st["gate"]
        text, _, _ = serialise.render(form, moment)
        lv = st.get("live") or {}
        nothing_p = probs.get(NOTHING, 0.0)
        extra: dict = {}
        digest = state_digest(text)
        if menu and lv.get("laya_rank") in ("blend", "live"):
            menu, nothing_p, extra = self.laya_rerank(digest, menu, nothing_p, lv)
        menu, starters = self.starters.blend(menu, ctx, support, now=now)   # W22: day-one priors, decaying
        if starters:
            support = max(support, cfg["min_history"])
        predicted, menu = menu, menu + self.shots.options()          # the live screenshot joins the pool
        hud = st.get("hud") or settings.HUD_DEFAULTS
        mode = hud.get("auto_mode", "hint")
        shown, reason = gate(menu, nothing_p, cfg, now=now, last_show_at=self.last_show_at,
                             paused=st["paused"], hud_enabled=st["hud_enabled"], support=support,
                             hint=mode == "hint")   # W20: a hint has its own cooldown
        listed = shown or sorted([o for o in menu if o.get("primitive") in ALLOWED_PRIMITIVES],
                                 key=lambda o: -show_p(o))[:3]
        row = {"ts": now_iso(now), "form": form.get("_file"), "state_digest": digest,
               "options": [{"key": (i + 1) if shown else None, "token": o["token"], "p": o.get("p_show", o.get("p_markov")),
                            **({"starter": o["starter"]} if o.get("starter") else {})}   # W22
                           for i, o in enumerate(listed)],
               "shown": False, "outcome": f"dropped:{reason}", "executed": None, "latency_ms": None, **extra}
        self.shadow(row, form, predicted, text, lv, probs, digest)
        if reason != "show":
            self.log_row(row)
            return {"reason": reason}
        # W19: the automatic show as a hint (default), the full cross, or nothing
        if mode == "off":
            row["outcome"] = "dropped:auto_off"
            self.log_row(row)
            return {"reason": "auto_off"}
        if any(o.get("live") for o in shown):
            mode = "full"     # a live screenshot's options are the cross (the dots went unseen), bound to it
        return self.dispatch(shown, row, ctx, cfg, now, mode=mode,
                             hint_ttl_ms=int(hud.get("hint_ttl_ms", settings.HUD_DEFAULTS["hint_ttl_ms"])),
                             fill_order=hud.get("fill_order"))   # W21
        # end W19

    def summon(self, now: float | None = None, trigger: str | None = None) -> dict:
        """W10: answer a summon broadcast with the current moment's best options, gate bypassed
        (no threshold, no cooldown; max_show kept). Paused / HUD off / denylisted front app: nothing.
        `trigger` "shot" = the same show the moment a screenshot becomes live."""
        now = self.clock() if now is None else now
        st = settings.load()
        if st["paused"]:
            return {"reason": "paused"}
        if not st["hud_enabled"]:
            return {"reason": "hud_disabled"}
        moment = self.current_moment()
        ctx = moment["context"] if moment else {}
        if ctx.get("app") and normalise._denied(ctx["app"], normalise.effective_denylist()):
            return {"reason": "denylisted"}   # nothing logged; oi-hud draws its fallback (wish option)
        form = forms.route(ctx)
        model = self.model
        menu = candidates.build(form, moment, model, is_executable=self.is_executable) if (model and moment) else []
        if moment:
            menu = self.page_menu(menu, ctx, summoned=True)       # W16: + the page's key button (rules)
        support = 0
        if model and moment:
            support = sum(model.counts.get("o1", {}).get(model.features(moment)[1], {}).values())
        text = serialise.render(form, moment)[0] if moment else None
        digest = state_digest(text) if moment else None
        lv = st.get("live") or {}
        probs = model.distribution(moment)[0] if (model and moment) else {}
        extra: dict = {}
        if moment and menu and lv.get("laya_rank") in ("blend", "live"):   # Laya's answer if already in
            menu, _, extra = self.laya_rerank(digest, menu, probs.get(NOTHING, 0.0), lv)
        menu, _ = self.starters.blend(menu, ctx, support, now=now)   # W22: a summon on day one is not empty
        predicted, menu = menu, menu + self.shots.options()          # the live screenshot joins the pool
        cfg = st["gate"]
        shown = summon_options(menu, cfg)
        if trigger and not any(o.get("live") for o in shown):
            return {"reason": "no_option"}
        row = {"ts": now_iso(now), "form": form.get("_file"), "state_digest": digest,
               "options": [{"key": i + 1, "token": o["token"], "p": o.get("p_show", o.get("p_markov")),
                            **({"starter": o["starter"]} if o.get("starter") else {})} for i, o in enumerate(shown)],
               "shown": False, "outcome": "dropped:no_reply", "executed": None, "latency_ms": None,
               **({"summoned": True} if trigger is None else {"trigger": trigger}), **extra}
        if moment and predicted:
            self.shadow(row, form, predicted, text, lv, probs, digest)
        return self.dispatch(shown, row, ctx, cfg, now, fill_order=(st.get("hud") or {}).get("fill_order"))

    def dispatch(self, shown: list[dict], row: dict, ctx: dict, cfg: dict, now: float, *,
                 mode: str = "full", hint_ttl_ms: int = settings.HUD_DEFAULTS["hint_ttl_ms"],
                 fill_order: list[str] | None = None, marker: str | None = None,
                 mail_fixed: dict | None = None, env_extra: dict | None = None,
                 on_done: Callable[[dict], None] | None = None) -> dict:
        """Send (or, in dry-run, print) the show for `shown`; the reply completes and logs `row`.
        W19: `mode` "hint" = dots only until ⌥⌘ develops them (`hint_ttl_ms`), "full" = the cross.
        W21: each option's key = its modifier (fn 1, ⌃ 2, ⌥ 3, ⌘ 4), chosen by `fill_order` (rank 1 first) and kept
        for an option shown again in the same context (form + front app) when its key is free."""
        keys = self.keys.assign(f"{row.get('form')}|{ctx.get('app')}", [o.get("token") or "" for o in shown], fill_order)
        for logged, k in zip(row["options"], keys):   # the hud-log names the key, never the rank
            logged["key"] = k
        live = [o for o in shown if o.get("live")]   # options of a live context state (the screenshot)
        ttl = show_ttl_ms(cfg)
        if live:
            ttl = max(ttl, int(shots_mod.shot_settings(vocal_mod._raw_settings())["ttl_ms"]))   # display setting
        payload = hud_payload(shown, ctx, ttl_ms=ttl, resolve_app=self.resolve_app, keys=keys)
        # W19
        row["show_mode"] = "hint" if mode == "hint" else "full"
        if mode == "hint":
            payload["mode"] = "hint"
            payload["hint_ttl_ms"] = hint_ttl_ms
            row["expanded"] = False
        # end W19
        if marker:                                   # W24: a proactive hint (oi-hud draws a ring around the dots)
            payload["marker"] = marker
        live_show = None
        if live:                                     # bound to the state: kept while typing, hidden when it ends
            if mode != "hint":
                payload["live"] = live[0]["live"]
            row["live"] = sorted({o["live"] for o in live})
            for logged, o in zip(row["options"], shown):
                if o.get("outward"):
                    logged["outward"] = True
            self.shots.offered(live)
            line = self.shots.context_line()
            if line:
                env_extra = dict(env_extra or {}, events=[line] + list((env_extra or {}).get("events") or []))
            live_show = self.live_show = {"done": False}
        for logged, sent in zip(row["options"], payload["options"]):   # W11: which hint each option carried
            h = sent.get("hint")
            if h:
                logged["hint"] = h["kind"] + (f":{h['dir']}" if h.get("dir") else "")
        self.last_show_ts = row["ts"]
        if not row.get("summoned"):   # a summoned show never starts the cooldown
            self.last_show_at = now
        if self.dry_run:
            row["outcome"] = "dropped:dry_run"
            self.log_row(row)
            print(json.dumps({"form": row["form"], **payload}, ensure_ascii=False), file=self.out, flush=True)
            return {"reason": "dry_run", "payload": payload}
        keymap = dict(zip(keys, shown))       # W21: `pressed:<k>` = the key number
        t0 = time.monotonic()
        session = self.glide.session(keymap, ctx, dict(self.glide_env(ctx), **(env_extra or {})))      # W15
        if mail_fixed is not None:                    # W24: the glide works on the EVENT's mail, not the selection
            session.mail_fixed = mail_fixed

        def on_reply(reply: dict) -> None:
            if live_show is not None:                 # 
                live_show["done"] = True
            if on_done is not None:                   # W24: the proactive engine learns its hint is over
                on_done(reply)
            self.on_reply(reply, row, keymap, ctx, t0, session)

        def on_event(ev: dict) -> None:
            try:
                if ev.get("event") == "glide":
                    session.prefetch()
                elif ev.get("event") == "expand":
                    p = ev.get("path")
                    if isinstance(p, list) and 1 <= len(p) <= glide.MAX_DEPTH and all(isinstance(x, int) for x in p):
                        session.expand(p)
            except Exception:
                log("glide failed: " + traceback.format_exc().splitlines()[-1])
        if self._sender_events:
            self.threads.append(self.sender(payload, on_reply, on_event=on_event))
        else:
            self.threads.append(self.sender(payload, on_reply))
        self.threads = [t for t in self.threads if t.is_alive()]
        if row.get("summoned") and keymap:   # W15 follow-up (W18 PR): a summon prefetches the glide follow-ups so a
            threading.Thread(target=on_event, args=({"event": "glide", "phase": "summon"},),   # hold right after
                             daemon=True, name="oi-glide-prefetch").start()   # it blooms at once; never on gated shows
        self.claude.after_show(row.get("form"), shown, payload)   # W14: label enrichment, async
        return {"reason": "show", "payload": payload}

    def glide_env(self, ctx: dict) -> dict:
        """W15: what a follow-up may target: apps switched to today (installed), sites seen today (host -> the
        newest host/slug), the fleet agents, and the page host of the moment. Denylisted apps never appear."""
        deny = normalise.effective_denylist()
        apps: list[str] = []
        urls: dict[str, str] = {}
        for r in reversed(self.rows):
            tok = r.get("token") or ""
            if tok.startswith("activate:") and len(apps) < glide.LIST_MAX:
                a = tok.split(":", 1)[1]
                if a not in apps and not normalise._denied(a, deny) and self.resolve_app(a):
                    apps.append(a)
            elif tok.startswith("url:") and len(urls) < glide.LIST_MAX:
                hs = tok.split(":", 1)[1]
                urls.setdefault(hs.split("/", 1)[0], hs)
        try:
            agents = {s: spec.get("name") or s.title() for s, spec in vocal_mod.recipients(vocal_mod._raw_settings()).items()}
        except Exception:
            agents = {}
        pages: dict[str, list[str]] = {}               # W23: today's pages per host (newest first)
        history: list[tuple[float, str]] = []           # W23: today's tokens (history provider)
        titles: list[str] = []                          # W23: newest window titles (Telegram chats, project folder)
        for r in self.rows:
            tok = r.get("token") or ""
            try:
                history.append((vocab.parse_ts(r["ts"]).timestamp(), tok))
            except (KeyError, TypeError, ValueError):
                continue
        for _, tok in reversed(history):
            if tok.startswith("url:"):
                hs = tok.split(":", 1)[1]
                lst = pages.setdefault(hs.split("/", 1)[0], [])
                if hs not in lst and len(lst) < drill.PAGES_MAX:
                    lst.append(hs)
            elif tok.startswith("title:") and tok not in titles and len(titles) < 40:
                titles.append(tok)
        live_events: list[dict] = []                    # what is live now (an app direction lists its items)
        bots: dict[str, str] = {}
        try:
            live_events = self.proactive.current.current()
            from .proactive import config as proactive_config, engine as proactive_engine
            bots = proactive_config.proactive_settings(proactive_engine._raw_settings())["agents"].get("bots") or {}
        except Exception:
            pass
        return {"apps": apps, "urls": urls, "agents": agents, "resolve_app": self.resolve_app,
                "host": status_context(ctx)["host"], "pages": pages, "history": history[-2000:],
                "title_tokens": titles, "live_events": live_events, "bots": bots}

    def _send_vocal(self, ctx: dict) -> Callable[[str, str], dict]:
        def send(to: str, text: str) -> dict:
            return self.vocal_send(to, f"(via the HUD glide) {text}", app=ctx.get("app") if isinstance(ctx.get("app"), str) else None,
                                   desk=ctx.get("space") if isinstance(ctx.get("space"), int) else None,
                                   dry_run=self.exec_dry_run, out=self.out)
        return send

    def on_reply(self, reply: dict, row: dict, keymap: dict, ctx: dict, t0: float,
                 session: "glide.GlideSession | None" = None) -> None:
        row = dict(row, latency_ms=int((time.monotonic() - t0) * 1000))
        ev = reply.get("event")
        action = None
        if ev == "pressed":
            k = reply.get("key")
            row.update(shown=True, outcome=f"pressed:{k}")
            if reply.get("via") in ("key", "click", "glide"):
                row["via"] = reply["via"]
            path = reply.get("path")
            if not (isinstance(path, list) and path and all(isinstance(x, int) for x in path) and path[0] == k):
                path = [k]
            try:
                opt = keymap.get(k)
                action = (opt or {}).get("primitive")
                if (opt or {}).get("shot") and len(path) == 1:   # the live screenshot's option (tap / long-press)
                    if reply.get("mode") == "claude":
                        row["mode"] = "claude"
                    row["executed"] = self.shots.press(opt, reply)
                elif len(path) > 1:                   # W15: a follow-up from the glide's sub-ring
                    if reply.get("mode") == "claude":
                        row["mode"] = "claude"
                    if session is None:
                        raise ValueError("glide path without a session")

                    # W24: the event's mail is found by its message id at press time (never selected first)

                    def run_primitive(n: dict) -> str | None:
                        if not self._outward_pick(opt, n, row):
                            return None
                        return self.executor.run(n, ctx, self.url_for, message_id=session.mail_id or "")

                    def press(n: dict, mid: str | None):
                        if not self._outward_pick(opt, n, row):
                            return None, None
                        o = {"primitive": n["primitive"], "params": n["params"], "token": n["token"]}
                        return self.claude.press(o, ctx, k, lambda: self.executor.run(o, ctx, self.url_for, message_id=mid or ""),
                                                 expect_mid=mid)
                    row["executed"], action, meta = session.execute(path, run_primitive=run_primitive, press=press,
                                                                    send_vocal=self._send_vocal(ctx))
                    if meta:
                        row["claude"] = meta
                elif not self._outward_pick(opt, opt, row):   # outward gate: a W24 `outward` option, cancelled
                    row["executed"] = None
                elif reply.get("mode") == "claude":   # W14: long-press -> the Claude variant (always answers the HUD)
                    row["mode"] = "claude"
                    mid = ((opt or {}).get("params") or {}).get("message_id") if (opt or {}).get("evt") else None
                    # W24: the draft is for the event's mail, read by its id (not selected, Mail stays behind)
                    row["executed"], row["claude"] = self.claude.press(
                        opt, ctx, k, lambda: self.executor.run(opt, ctx, self.url_for), **({"expect_mid": mid} if mid else {}))
                else:
                    self.last_press = None
                    row["executed"] = self.executor.run(opt, ctx, self.url_for)
                    if (opt or {}).get("primitive") == page_actions.PRIMITIVE:   # W16
                        row["ui_press"] = self.last_press or {"ok": False, "reason": "not_sent"}
            except Exception:
                log("execute failed: " + traceback.format_exc().splitlines()[-1])
        elif ev == "dismissed":
            row.update(shown=True, outcome=f"dismissed:{reply.get('reason', '?')}")
        else:
            row.update(shown=False, outcome=f"dropped:{reply.get('reason') or reply.get('message') or ev}")
        if row.get("show_mode") == "hint":           # W19: was the hint developed by ⌥⌘?
            row["expanded"] = reply.get("expanded") is True
        if isinstance(reply.get("glide"), dict):     # W15: paths, dwell times, the action id (no text)
            row["glide"] = glide.log_entry(reply["glide"], session, action if ev == "pressed" else None)
        with self.lock:
            if row["shown"]:
                self.shows_today += 1
                self.pressed_today += ev == "pressed"
        if row["shown"]:                             # W22: starter usage (picks raise an item, ignored shows sink it)
            ids = [o["starter"] for o in keymap.values() if isinstance(o, dict) and o.get("starter")]
            picked = (keymap.get(reply.get("key")) or {}).get("starter") if ev == "pressed" and row["executed"] else None
            if ids:
                self.starters.record(ids, picked)
        self.log_row(row)
        log(f"show -> {row['outcome']} ({row['latency_ms']} ms)" + (" executed" if row["executed"] else ""))

    def log_row(self, row: dict) -> None:
        """Append the row; with a pending Laya shadow call, when that call is done (the show never waits).
        W21b: every row carries `numbering` = normalise.HUD_NUMBERING (option numbers are keys, not ranks)."""
        row.setdefault("numbering", normalise.HUD_NUMBERING)
        fut = row.pop("_laya_fut", None)
        if fut is None:
            append_jsonl(paths.hud_log_dir() / f"{row['ts'][:10]}.jsonl", row, self.lock)
            return

        def done(f: Future) -> None:
            try:
                row["laya"] = f.result()
            except Exception:
                row["laya"] = {"error": "exception"}
            append_jsonl(paths.hud_log_dir() / f"{row['ts'][:10]}.jsonl", row, self.lock)
        fut.add_done_callback(done)

    # -- W16 page actions -----------------------------------------------------------------------------
    def page_menu(self, menu: list[dict], ctx: dict, *, summoned: bool) -> list[dict]:
        """ui.press options only in Safari and only when oi-hud sees the element now; a summon adds the
        rule-based key button(s). Outside Safari every ui.press option is dropped (no snapshot taken)."""
        if ctx.get("app") != "Safari":
            return [o for o in menu if o.get("primitive") != page_actions.PRIMITIVE]
        if not summoned and not any(o.get("primitive") == page_actions.PRIMITIVE for o in menu):
            return menu
        try:
            page = self.page.snapshot()
        except Exception:
            page = None
        return page_actions.apply(menu, page, ctx, with_rules=summoned)

    def wish_page_items(self, ctx: dict) -> list[dict]:
        """W18 wish catalogue: the current Safari page's buttons (snapshot cached 3 s: previews run per keystroke)."""
        now, host = time.monotonic(), page_actions._host_of(ctx)
        c = self._wish_page
        if c and c[1] == host and now - c[0] < 3.0:
            return c[2]
        try:
            items = page_actions.wish_items(self.page.snapshot(), ctx)
        except Exception:
            items = []
        self._wish_page = (now, host, items)
        return items

    def _wish_exec(self, opt: dict, ctx: dict) -> str | None:
        """W18: a wish runs through the same Executor; its page press is marked (the gate always asks for it)."""
        if opt.get("primitive") == page_actions.PRIMITIVE:
            opt = dict(opt, params=dict(opt.get("params") or {}, via="wish"))
        return self.executor.run(opt, ctx, self.url_for)

    def _outward_pick(self, opt: dict | None, n: dict | None, row: dict) -> bool:
        """Outward gate, class "reply": a W24 option marked `"outward": true` (reply / forward / draft, the chat)
        asks at the pick, before its window opens (the one confirmation). agent.ask is gated at its send (vocal)."""
        prim = (n or {}).get("primitive")
        if not (opt or {}).get("outward") or prim not in OUTWARD_AT_PICK:
            return True
        g = outward_gate.outward("reply", str(opt.get("token") or prim), str(opt.get("label") or prim), None,
                                 dry_run=self.dry_run or self.exec_dry_run)
        row["gate"] = {"id": g["id"], "decision": g["decision"]}
        if not g["ok"]:
            log(f"outward option: not run ({g['error']})")
        return g["ok"]

    def _ui_press(self, params: dict) -> bool:
        slug, wish = params.get("slug"), params.get("via") == "wish"
        if wish or outward_gate.page_outward(slug, params.get("role")):   # outward gate: fail-safe
            target = f"{params.get('role')}:{slug}"
            # the panel is the one confirmation; for an irreversible label or a wish it is also the only source of
            # oi-hud's press ticket, so it asks even with gate.outward off
            g = outward_gate.outward("page", target, params.get("short") or str(slug).replace("-", " "),
                                     params.get("host"), target=target, dry_run=self.dry_run or self.exec_dry_run,
                                     force=wish or page_actions.irreversible(slug or ""))
            if not g["ok"]:
                self.last_press = {"ok": False, "reason": "gate_cancelled", "gate": g["id"]}
                log(f"ui.press: not pressed ({g['error']})")
                return False
        r = self.page.press(params, dry_run=self.dry_run or self.exec_dry_run)   # W15 --dry-exec: find only
        self.last_press = {"ok": bool(r.get("ok")), "reason": r.get("reason")}
        if not r.get("ok"):
            log(f"ui.press: nothing pressed ({r.get('reason')})")
        return bool(r.get("ok")) and not r.get("dry_run")

    # -- W13 Laya layer ------------------------------------------------------------------------------
    def laya_client(self, lv: dict):
        key = (lv.get("laya_url"), lv.get("laya_checkpoint"), lv.get("laya_model"), lv.get("laya_timeout_ms"))
        if self._laya_client is None or key != self._laya_key:
            self._laya_client, self._laya_key = self.laya_client_factory(lv), key
        return self._laya_client

    def ensure_laya_at_start(self) -> None:
        """Once at start: when the menu-bar switch `laya.enabled` is on, ensure_server (memory rule inside), so Laya
        comes back after a reboot without waiting for a first miss. Off: nothing (Markov only)."""
        cfg = settings.load()
        if not cfg["laya"]["enabled"]:
            return
        lv = cfg["live"]
        self._laya_ensure_at = time.monotonic()
        try:
            state, detail = self.ensure_laya(lv.get("laya_url") or laya.DEFAULT_URL, lv.get("laya_checkpoint"))
            log(f"laya at start: ensure_server -> {state} ({detail})")
        except Exception:
            log("laya ensure_server failed: " + traceback.format_exc().splitlines()[-1])

    def _laya_call(self, client, form: dict, menu: list[dict], text: str, lv: dict) -> dict:
        """One Laya prediction; never raises (an error is a result). A server found down -> ensure_server()."""
        try:
            return client.predict(form, menu, text)
        except laya.LayaError as e:
            if e.reason == "unreachable" and time.monotonic() - self._laya_ensure_at >= LAYA_ENSURE_S:
                self._laya_ensure_at = time.monotonic()
                try:
                    state, detail = self.ensure_laya(lv.get("laya_url") or laya.DEFAULT_URL, lv.get("laya_checkpoint"))
                    log(f"laya server down: ensure_server -> {state} ({detail})")
                except Exception:
                    log("laya ensure_server failed: " + traceback.format_exc().splitlines()[-1])
            return {"error": e.reason, "checkpoint": getattr(client, "checkpoint", None)}
        except Exception:
            return {"error": "exception", "checkpoint": getattr(client, "checkpoint", None)}

    def shadow(self, row: dict, form: dict, menu: list[dict], text: str, lv: dict, probs: dict,
               digest: str | None = None) -> None:
        """Queue a Laya prediction for this row (live.laya_shadow, or laya_rank blend|live); never blocks. A state
        already asked (same digest and menu) reuses that call: the server is not asked twice for one answer."""
        if not (lv.get("laya_shadow") or lv.get("laya_rank") in ("blend", "live")) or not menu:
            return
        mp = {o["key"]: round(probs.get(NOTHING, 0.0) if o["key"] == NOTHING else (o.get("p_markov") or 0.0), 6)
              for o in menu}
        top = max(menu, key=lambda o: mp[o["key"]])
        meta = {"markov_top": top["key"], "markov_probs": mp, "tokens": {o["key"]: o.get("token") for o in menu}}
        ck, keys = digest or state_digest(text), tuple(o["key"] for o in menu)
        keys_asked, fut = self._laya_cache.get(ck, (None, None))
        if keys_asked != keys or (fut is not None and fut.done() and (fut.result() or {}).get("error")):
            fut = None                                  # another menu, or an error (not an answer): ask again
        if fut is None:
            with self.lock:
                busy = self._laya_inflight >= LAYA_MAX_INFLIGHT
                if not busy:
                    self._laya_inflight += 1
            if busy:
                fut = Future()
                fut.set_result({"error": "busy"})
            else:
                client = self.laya_client(lv)

                def work() -> dict:
                    try:
                        return self._laya_call(client, form, menu, text, lv)
                    finally:
                        with self.lock:
                            self._laya_inflight -= 1
                fut = self._laya_pool.submit(work)
                self._laya_cache.pop(ck, None)
                self._laya_cache[ck] = (keys, fut)
                while len(self._laya_cache) > LAYA_CACHE:
                    self._laya_cache.pop(next(iter(self._laya_cache)))
        out: Future = Future()                          # this row's view: the shared answer + its own Markov side

        def view(f: Future) -> None:
            try:
                out.set_result({**(f.result() or {}), **meta})
            except Exception:
                out.set_result({"error": "exception", **meta})
        fut.add_done_callback(view)
        row["_laya_fut"] = out

    def laya_rerank(self, digest: str, menu: list[dict], nothing_p: float, lv: dict
                    ) -> tuple[list[dict], float, dict]:
        """laya_rank blend|live (the user's switch): rank on Laya's / the blend's probabilities IF Laya's answer for
        this state (covering this menu) is already in; never waits (Markov answers first). Else the Markov menu unchanged."""
        mode = lv.get("laya_rank")
        _, fut = self._laya_cache.get(digest, (None, None))
        if fut is None or not fut.done():
            return menu, nothing_p, {"laya_rank_fallback": "pending"}
        r = fut.result() or {}
        if r.get("error") or not r.get("probs"):
            return menu, nothing_p, {"laya_rank_fallback": r.get("error") or "no_probs"}
        if not {o["key"] for o in menu} <= set(r["probs"]):
            return menu, nothing_p, {"laya_rank_fallback": "menu_changed"}
        lp, blend = r["probs"], mode == "blend"
        out = []
        for o in menu:
            p = lp.get(o["key"], 0.0)
            if blend:
                p = 0.5 * p + 0.5 * (nothing_p if o["key"] == NOTHING else (o.get("p_markov") or 0.0))
            out.append({**o, "p_show": round(p, 6)})
        lnoth = lp.get(NOTHING, 0.0)
        return out, (0.5 * lnoth + 0.5 * nothing_p) if blend else lnoth, {"laya_rank": mode}

    # -- status ---------------------------------------------------------------------------------
    def load_counters(self) -> None:
        rows = [r for r in normalise.read_hud_log(self.day) if r.get("kind") not in ("vocal", "shot", "proactive")]   # W17, W24
        self.shows_today = sum(1 for r in rows if r.get("shown") is True)
        self.pressed_today = sum(1 for r in rows if str(r.get("outcome", "")).startswith("pressed:"))
        self.moments_today = len(rows)
        shown = [r["ts"] for r in rows if r.get("shown") is True and isinstance(r.get("ts"), str)]
        self.last_show_ts = shown[-1] if shown else None

    def write_status(self, now: float) -> None:
        with self.lock:
            st = {"ts": now_iso(now), "state": self.state, "pid": os.getpid(), "dry_run": self.dry_run,
                  "shows_today": self.shows_today, "pressed_today": self.pressed_today,
                  "last_show_ts": self.last_show_ts, "model_trained_at": self.model_trained_at,
                  "moments_today": self.moments_today, "context": self.context}
        d = paths.status_dir()
        paths.ensure_private_root()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".oi-live.json.tmp-{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(st) + "\n")
        os.replace(tmp, d / "oi-live.json")

    # -- loop ------------------------------------------------------------------------------------
    def start(self) -> None:
        self.rebuild()
        self.load_model()        # the saved model now; a fresh refit runs on the first step's thread
        self.last_retrain = 0.0
        self.tracker.prime(self.rows)
        self.load_counters()

    def step(self, force: bool = False) -> dict | None:
        now = self.clock()
        st = settings.load()
        lv = st["live"]
        if st["paused"]:                      # logging paused: no pull, no show
            self.state = "paused"
            self.write_status(now)
            return {"reason": "paused"} if force else None
        self.state = "running" if st["hud_enabled"] else "hud_disabled"
        if utc_day(now) != self.day:
            self.rebuild()
            self.load_counters()
            self.tracker.prime(self.rows)
        elif now - self.last_rebuild >= lv["rebuild_s"]:
            self.rebuild()
        self.pull()
        self.shots.on_context(self.rows)      # a window-less screenshot leaves the context when it changes
        self.maybe_retrain(now, lv["retrain_s"])
        try:
            self.proactive.drain(st, now)    # W24: queued events -> tokens in the stream, gate, ring hint
        except Exception:
            log("proactive failed: " + traceback.format_exc().splitlines()[-1])
        changed = self.tracker.change(self.rows)
        result = self.evaluate(st, now) if (changed or force) else None
        self.write_status(now)
        return result

    def wait_replies(self, timeout: float) -> None:
        for t in self.threads:
            t.join(timeout)

    def on_live_state(self, what: str, shot: dict) -> None:
        """(oi.shots): a live state changed. "appear": its options show at once (the summon's show, no gate);
        "content": Claude's content actions came: the cross still up for it is redrawn in place; "gone": the state
        ended: the cross still up for it is taken down (its relevance ended with it)."""
        if what in ("appear", "content"):
            if what == "content" and (self.live_show is None or self.live_show["done"]):
                return                                # not on screen: the next hint / summon has them
            r = self.summon(trigger="shot")
            log(f"screenshot {what} -> {r.get('reason')} ({len((r.get('payload') or {}).get('options', []))} options)")
        elif what == "gone" and self.live_show is not None and not self.live_show["done"] and not self.shots.live_set():
            self.live_show["done"] = True
            if not self.dry_run:
                self._hud_cmd({"cmd": "hide"})
            log("screenshot gone: its cross taken down")

    def on_summon(self, ev: dict) -> None:
        r = self.summon()
        n = len((r.get("payload") or {}).get("options", []))
        log(f"summon -> {r.get('reason')} ({n} options)")

    def run_forever(self) -> None:
        self.start()
        summon_listener(self.socket_path, self.on_summon, on_event=self.wish.on_event,   # W18: wish resolver
                        hello=[{"cmd": "resolver"}])
        self.shots.start()                   # W17 / own 0.5 s poll of the shot bucket (live state)
        self.proactive.start()               # W24: the opt-in sensors (all off by default)
        self.ensure_laya_at_start()          # the menu-bar Laya switch on: Laya comes back after a reboot (2026-10-21)
        log(f"started (dry_run={self.dry_run}, model={'yes' if self.model else 'none'}, tokens today={len(self.rows)})")
        while True:
            t = time.monotonic()
            poll = 2.0
            try:
                poll = float(settings.load()["live"]["poll_s"]) or 2.0
                r = self.step()
                if r and r.get("reason") == "show":
                    log(f"show sent ({len(r['payload']['options'])} options)")
            except Exception:
                log("loop error: " + traceback.format_exc().splitlines()[-1])
            time.sleep(max(0.2, poll - (time.monotonic() - t)))


def main_live(once: bool, dry_run: bool, socket_path: str | None, dry_exec: bool = False) -> int:
    live = Live(dry_run=dry_run, socket_path=Path(socket_path).expanduser() if socket_path else None,
                exec_dry_run=dry_exec)
    if not once:
        live.run_forever()
        return 0
    live.start()
    live.retrain()               # --once: evaluate with a model fitted on today too
    live.last_retrain = live.clock()
    r = live.step(force=True) or {}
    live.wait_replies(TTL_RANGE_MS[1] / 1000 + REPLY_SLACK_S)
    print(json.dumps({"reason": r.get("reason"), "options": len((r.get("payload") or {}).get("options", [])),
                      "shows_today": live.shows_today, "pressed_today": live.pressed_today}))
    return 0
