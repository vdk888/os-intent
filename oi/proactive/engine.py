"""W24: the proactive engine run by `oi live`.

Sensor thread (`start`): every second, each ENABLED source whose `poll_s` is due is polled (nothing while logging is
paused); its rows (appear AND gone: sensors.py) are appended to events/<day>.jsonl and queued. The driver's loop
(`Live.step` -> `drain`, the loop's own thread, so the token stream and the model are never touched from two
threads): each queued row's token joins the stream at once (`live.rows`) and updates `self.current` (store.Live:
the events whose state is live NOW). Each new live event goes through the relevance gate, and a passing event
becomes ONE hint through `Live.dispatch` (keys, glide, long-press, hud-log as any show; the row carries `proactive`
{source, class, p, priority, important, tier, hist_n, wiki_page?}). ADDENDUM 2: an IMPORTANT event
(relevance.importance: a priority, the wiki judge — judge.py, run here on the sensor thread before the row is stored —,
a W22 profile match) OPENS the full cross with its actions, no window change or
pointer move needed; any other one is the W19 dots with `"marker":"ring"`. Any send those actions lead to goes
through the outward validation gate (`outward_gate` below marks them; oi.live asks oi/gate.py's panel at the pick —
reply / forward / draft / the chat — and "Ask <agent>" at its vocal send: pick -> gate -> validate). A gated event
= one hud-log row {"kind": "proactive", source, class, reason, p, …}. Never a name, a subject or a path in a log.
Context is STATE, not timers (docs/DESIGN.md design rule): an event is hintable exactly while it is live. Held back for a
passing reason (relevance.TRANSIENT: rate limit, quiet hours, Focus, AFK… or the HUD busy), it is considered again
while live; when its gone row comes, it leaves `current` (the Claude "Events" context) and a hint still up for it is
withdrawn (oi-hud {"cmd":"hide"}, hud-log reason "state_ended").
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Callable

from .. import drill, glide, health as health_mod, normalise, settings, vocab
from ..moments import NOTHING
from ..orchestrator import pack
from . import judge as judge_mod, relevance, sensors as sensors_mod, store
from .config import TOKEN_SOURCE, enabled_sources, proactive_settings

TICK_S = 1.0
ERR_LOG_S = 60
MAX_OPTIONS = 4
HINT_MARKER = "ring"
# primitives whose next step can leave the machine (a mail sent, a message to an agent): validated before any send
OUTWARD_PRIMITIVES = frozenset({"mail.reply", "mail.forward", "mail.draft", "agent.ask", "telegram.chat"})
RECHECK_S = 5.0              # poll cadence of the held-back live events (how often, not how long: no expiry)
EVENTS_CONTEXT = 6            # live events in the Claude "Events" context (newest first)
HUD_BUSY = frozenset({"visible", "rate_limited", "wish_open", "vocal", "claude", "summon"})   # oi-hud said "not now"


def log(msg: str) -> None:
    print(f"{vocab.iso_utc(datetime.now(timezone.utc))} oi-proactive: {msg}", file=sys.stderr, flush=True)


def _raw_settings() -> dict:
    import json
    try:
        d = json.loads(settings.settings_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


# ---- the event's actions -----------------------------------------------------------------------------------

def _names() -> dict[str, str]:
    try:
        from .. import vocal
        return {s: spec.get("name") or s.title() for s, spec in vocal.recipients(vocal._raw_settings()).items()}
    except Exception:
        return {}


def _opt(n: int, primitive: str, params: dict, token: str, label: str, icon: str, p: float, *,
         claude: bool = False, private: bool = False) -> dict:
    return {"key": f"evt_{n}", "primitive": primitive, "params": params, "token": token, "label": label[:22],
            "icon": icon, "p_markov": p, "evt": True, "claude": claude and not private, "private": private}


def event_options(ev: dict, p: float, *, names: dict[str, str] | None = None,
                  route: Callable[[str], tuple[str | None, float]] | None = None) -> list[dict]:
    """The event's own actions (most useful first). Never a send: replies are drafts / the vocal panel; Telegram
    opens the chat; files open in their app (never run: drill.launchable) or show in Finder."""
    names = names if names is not None else _names()
    if route is None:
        from ..orchestrator import route as route_fn
        route = route_fn
    src, ref, who, priv = ev["source"], ev.get("ref") or {}, ev.get("who") or "", bool(ev.get("private"))
    out: list[dict] = []

    def add(*a, **kw):
        out.append(_opt(len(out) + 1, *a, p=p, private=priv, **kw))

    def ask(slug: str | None):
        if slug and slug in names and not any(o["primitive"] == "agent.ask" for o in out):
            add("agent.ask", {"to": slug}, f"agent.ask:{slug}", f"Ask {names[slug]}", f"initial:{names[slug][:1].upper()}")
    if src == "mail" and ref.get("message_id"):
        mid = ref["message_id"]
        add("mail.reply", {"message_id": mid}, "mail.reply", f"Reply to {who}" if who else "Reply",
            "sf:arrowshape.turn.up.left", claude=True)
        add("mail.show", {"message_id": mid}, "mail.show", "Open the mail", "sf:envelope.open")
        if not priv and ev.get("subject"):
            ask(route(ev["subject"])[0])
    elif src == "agent" and ref.get("slug"):
        slug = ref["slug"]
        name = names.get(slug, slug.title())
        if ref.get("chat"):                       # « Ouvrir Telegram — <Agent> »
            label = f"Ouvrir Telegram — {name}"
            params = {"chat": ref["chat"], **({"bot": ref["bot"]} if isinstance(ref.get("bot"), str) else {})}
            add("telegram.chat", params, f"telegram.chat:{slug}", label if len(label) <= 22 else f"Telegram — {name}",
                "sf:paperplane")
        ask(slug)
        if out and out[-1]["primitive"] == "agent.ask":
            out[-1]["label"] = f"Reply to {name}"[:22]
    elif src == "file" and ref.get("path"):
        path = ref["path"]
        if drill.safe_path(path) and not drill.launchable(path):
            add("file.open", {"path": path}, "file.open", f"Open {os.path.basename(path)}"[:22], "sf:doc")
        parent = os.path.dirname(path)
        if drill.safe_path(parent):
            add("folder.open", {"path": parent}, "folder.open", "Show in Finder", "sf:folder")
    elif src == "cockpit" and ev["class"].startswith("decide-"):   # a decision waiting: the dashboard page that decides it
        if ref.get("url"):
            add("url.open", {"href": ref["url"]}, "url.open:cockpit",
                "Ouvrir la PR" if ev["class"] == "decide-pr" else "Ouvrir la carte" if ev["class"] == "decide-board"
                else "Ouvrir la décision", "sf:checkmark.seal")
        slug = ref.get("slug")
        if ev["class"] == "decide-gate" and isinstance(ref.get("bot"), str) and slug:   # its approval buttons
            add("telegram.chat", {"chat": names.get(slug, slug.title()), "bot": ref["bot"]}, f"telegram.chat:{slug}",
                "Approuver (Telegram)", "sf:paperplane")
        ask(slug if ev["class"] != "decide-pr" else "atlas")
    elif src == "calendar":
        if ref.get("join"):
            add("url.open", {"href": ref["join"]}, "url.open:meeting", "Rejoindre la réunion", "sf:video")
        add("app.open", {"app": "Calendar"}, "activate:Calendar", "Ouvrir Calendrier", "sf:calendar")
    elif src == "github":
        if ref.get("url"):
            add("url.open", {"href": ref["url"]}, "url.open:github", "Ouvrir la PR", "sf:arrow.triangle.pull")
        ask("atlas")
    elif src == "cockpit":
        url = ref.get("url")
        if url:
            add("url.open", {"href": url}, "url.open:cockpit", "Open the alert" if ev["class"] != "outage" else "Open cockpit",
                "sf:exclamationmark.triangle", claude=not priv)
        ask("atlas" if ev["class"] == "outage" else (route(ev.get("subject") or "")[0] or "nova"))
    elif src == "metric":
        if ref.get("url"):
            add("url.open", {"href": ref["url"]}, "url.open:metric", "Open the metric", "sf:chart.line.downtrend.xyaxis")
        ask(ref.get("owner") or route(ev.get("who") or "")[0] or "nova")
    elif src == "market":
        ask("orion")
        if ref.get("url"):
            add("url.open", {"href": ref["url"]}, "url.open:market", "Open the brief", "sf:chart.xyaxis.line")
    return out[:MAX_OPTIONS]


def outward_gate(opts: list[dict]) -> list[dict]:
    """Marks each option whose follow-up can send (`"outward": true`): oi.live (`_outward_pick`) asks the outward
    gate's panel (class "reply") at the pick before its window opens; agent.ask is gated at its vocal send."""
    return [dict(o, outward=True) if o.get("primitive") in OUTWARD_PRIMITIVES else o for o in opts]


def habit_line(model, tok: str, top: int = 3) -> str:
    """"After a mail (client) the user usually: Mail 60%, mail.google.com 20% (12 times)" from the order-1 counts."""
    n, _ = relevance.history(model, tok)
    if not n:
        return ""
    c = model.counts["o1"][tok]
    parts = []
    for y, k in sorted(c.items(), key=lambda kv: -kv[1]):
        if y == NOTHING:
            continue
        shown = pack._shown(y)
        if shown:
            parts.append(f"{shown} {round(100 * k / n)}%")
        if len(parts) == top:
            break
    _, src, cls = tok.split(":", 2)
    nothing = round(100 * c.get(NOTHING, 0) / n)
    return (f"After a {src} event ({cls}), the user usually: {', '.join(parts) or '(nothing specific)'}; "
            f"nothing {nothing}% ({n} times)")


def event_line(ev: dict, tz) -> str:
    t = vocab.parse_ts(ev["ts"]).astimezone(tz).strftime("%H:%M")
    if ev.get("private"):
        return f"{t} {ev['source']} ({ev['class']}): details withheld"
    s = f"{t} {ev['source']} ({ev['class']})"
    if ev.get("who"):
        s += f" from {ev['who']}"
    if ev.get("subject"):
        s += f": {ev['subject'][:80]}"
    return s


def mail_of(ev: dict) -> dict | None:
    """The event's mail as the glide / pack see a selected mail (no body: subject and sender only)."""
    ref = ev.get("ref") or {}
    if ev["source"] != "mail" or not ref.get("message_id"):
        return None
    return {"sender_name": ev.get("who") or "", "sender_address": ref.get("address") or "",
            "subject": ev.get("subject") or "", "message_id": ref["message_id"], "cc": [], "reply_to": "",
            "body": "", "previous": None, "_private": bool(ev.get("private"))}


# ---- the engine --------------------------------------------------------------------------------------------

class Engine:
    def __init__(self, live, *, sensors: dict | None = None, raw_settings: Callable[[], dict] = _raw_settings,
                 clock: Callable[[], float] = time.time, focus: Callable[[], bool | None] = sensors_mod.focus_on,
                 profile: Callable[[], dict] = relevance.load_profile, names: Callable[[], dict] = _names,
                 hud_cmd: Callable[[dict], dict] | None = None, health: "health_mod.SensorHealth | None" = None,
                 judge: Callable[[dict], dict] | None = None):
        self.live = live
        self.judge = judge or judge_mod.Judge()        # the ONE relevance judge (wiki), run here on the sensor thread
        self.hud_cmd = hud_cmd or (lambda payload: live._hud_cmd(payload))
        self.current = store.Live()                     # the events whose state is live now
        self.held: dict[str, dict] = {}                 # live events held back for a TRANSIENT reason (id -> event)
        self.showing: dict | None = None                # {"id", "done", "at", "prev"} of the last proactive hint sent
        self.recheck_at = float("-inf")
        self._sensors = sensors
        self.raw_settings, self.clock, self.focus, self.profile, self.names = raw_settings, clock, focus, profile, names
        self.q: "queue.Queue[dict]" = queue.Queue()
        self.last_poll: dict[str, float] = {}
        self.err_at: dict[str, float] = {}
        self.last_hint_at: float | None = None
        self.stop = threading.Event()
        self.health = health or health_mod.SensorHealth()   # <data dir>/health/sensors.json (`oi health`)

    @property
    def sensors(self) -> dict:
        if self._sensors is None:
            self._sensors = sensors_mod.default_sensors(self.live)
        return self._sensors

    def config(self) -> dict:
        return proactive_settings(self.raw_settings())

    # -- sensing (its own thread) ---------------------------------------------------------------------------
    def poll_once(self, now: float | None = None) -> list[dict]:
        now = self.clock() if now is None else now
        raw = self.raw_settings()
        cfg = proactive_settings(raw)
        srcs = list(enabled_sources(cfg) or [])
        self.health.flush(now, srcs, paused=raw.get("paused") is True)   # at most every minute
        if raw.get("paused") is True:
            return []
        got: list[dict] = []
        for src in srcs:
            if now - self.last_poll.get(src, float("-inf")) < float(cfg[src]["poll_s"]):
                continue
            self.last_poll[src] = now
            s = self.sensors.get(src)
            if s is None:
                continue
            try:
                evs = s.poll(now, cfg) or []
            except Exception as e:
                self.health.record(src, now, f"exception:{type(e).__name__}")
                if now - self.err_at.get(src, float("-inf")) >= ERR_LOG_S:
                    self.err_at[src] = now
                    log(f"{src} sensor failed: " + traceback.format_exc().splitlines()[-1])
                continue
            if not getattr(s, "skipped", False):           # a sensor backing off (mail) did not read: no health row
                self.health.record(src, now, getattr(s, "last_error", None))
            for ev in evs:
                if TOKEN_SOURCE.get(src) != ev.get("source"):
                    continue
                if not store.is_gone(ev):
                    judge_mod.apply(ev, self.judge)           # never on the HUD's thread (≤ judge.TIMEOUT_S)
                store.append(ev)
                self.q.put(ev)
                got.append(ev)
        return got

    def start(self) -> threading.Thread:
        def work():
            while not self.stop.is_set():
                try:
                    self.poll_once()
                except Exception:
                    log("poll failed: " + traceback.format_exc().splitlines()[-1])
                self.stop.wait(TICK_S)
        t = threading.Thread(target=work, daemon=True, name="oi-proactive")
        t.start()
        return t

    # -- the driver's side ----------------------------------------------------------------------------------
    def drain(self, st: dict, now: float) -> list[dict]:
        """Called by Live.step: every queued row -> its token in the stream and the live set; each new live event ->
        the gate; then the held-back live events again (until one is hinted)."""
        evs, retry = [], []
        while True:
            try:
                x = self.q.get_nowait()
            except queue.Empty:
                break
            (retry if "retry" in x else evs).append(x)
        live = self.live
        if evs:
            live.rows = sorted(live.rows + [store.token_row(e) for e in evs], key=lambda r: r["ts"])
        for e in evs:
            self.current.apply(e)
        for x in retry:                                   # oi-hud was busy: the event waits while it is live
            if self.current.is_live(x["retry"]["id"]):
                self.held[x["retry"]["id"]] = x["retry"]
        results = []
        for e in evs:
            if store.is_gone(e):
                self.held.pop(e["id"], None)
                ended = self.end(e, now)
                if ended:
                    results.append(ended)
        new = [e for e in evs if not store.is_gone(e)]
        recheck = bool(self.held) and now - self.recheck_at >= RECHECK_S
        if not new and not recheck:
            return results
        cfg = self.config()
        hinted = False
        for ev in new:
            results.append(self.consider(ev, st, cfg, now, queued=hinted))
            hinted = hinted or results[-1]["reason"] == "hint"
        if not recheck or hinted:
            return results
        self.recheck_at = now
        for ev_id, ev in sorted(self.held.items(), key=lambda kv: kv[1]["ts"]):
            if ev_id in {e["id"] for e in new}:
                continue
            if not self.current.is_live(ev_id):
                self.held.pop(ev_id, None)
                continue
            r = self.consider(ev, st, cfg, now, again=True)
            if r["reason"] == "hint":
                results.append(r)
                break
        return results

    def end(self, gone: dict, now: float) -> dict | None:
        """A gone row: the hint still up for that event is withdrawn (its state ended, so has its relevance)."""
        sh = self.showing
        if not sh or sh["id"] != gone["id"] or sh["done"]:
            return None
        sh["done"] = True
        try:
            self.hud_cmd({"cmd": "hide"})
        except Exception:
            log("hide failed: " + traceback.format_exc().splitlines()[-1])
        info = {"source": gone["source"], "class": gone["class"]}
        self.live.log_row({"kind": "proactive", "ts": live_now_iso(now), **info, "reason": "state_ended"})
        log(f"hint {gone['source']}:{gone['class']} withdrawn: its state ended")
        return {"reason": "state_ended", **info}

    def consider(self, ev: dict, st: dict, cfg: dict, now: float, again: bool = False, queued: bool = False) -> dict:
        """The gate for one live event; a pass = its hint. `queued`: another event of this drain was just hinted
        (one hint at a time: this one waits while it is live, priorities included)."""
        live = self.live
        moment = live.current_moment()
        ctx = moment["context"] if moment else {}
        front = ctx.get("app")
        denied = bool(front and normalise._denied(front, normalise.effective_denylist()))
        hud = st.get("hud") or settings.HUD_DEFAULTS
        g = relevance.gate(ev, cfg, now=now, last_hint_at=self.last_hint_at, model=live.model, profile=self.profile(),
                           live=self.current.is_live(ev["id"]), paused=st.get("paused", False), hud_enabled=st.get("hud_enabled", True),
                           auto_mode=hud.get("auto_mode", "hint"), front_denied=denied,
                           focus=self.focus() if cfg.get("respect_focus", True) else None)
        info = {"source": ev["source"], "class": ev["class"], "p": g["p"], "priority": g["priority"],
                "important": g["important"], "tier": g["tier"], "hist_n": g["hist_n"], "prior": g["prior"]}
        if g["important"] == "wiki":                     # the WHY, by page name only (the snippet stays in the event)
            info["wiki_page"] = ev["wiki"]["page"]
        if g["ok"] and moment is None:
            g = dict(g, ok=False, reason="afk")
        if g["ok"] and queued:
            g = dict(g, ok=False, reason="queued")
        opts = event_options(ev, g["p"], names=self.names()) if g["ok"] else []
        if g["ok"]:
            opts = outward_gate(self.with_history(opts, moment, st))
        if not opts:
            reason = g["reason"] if not g["ok"] else "no_option"
            if reason in relevance.TRANSIENT:
                self.held[ev["id"]] = ev                  # considered again while its state is live
            else:
                self.held.pop(ev["id"], None)
            if not again:                                 # one log row per event (not one per re-check)
                live.log_row({"kind": "proactive", "ts": live_now_iso(now), **info, "reason": reason})
            return {"reason": reason, **info}
        self.held.pop(ev["id"], None)
        prev_hint_at, self.last_hint_at = self.last_hint_at, now
        row = {"ts": live_now_iso(now), "form": "proactive", "state_digest": None,
               "options": [{"key": None, "token": o["token"], "p": o.get("p_markov")} for o in opts],
               "shown": False, "outcome": "dropped:no_reply", "executed": None, "latency_ms": None,
               "proactive": info}
        tz = relevance.default_tz()
        extra = {"events": [event_line(e, tz) for e in self.current.current(EVENTS_CONTEXT)],   # live now, no window
                 "event_habit": habit_line(live.model, store.token(ev)) if live.model is not None else "",
                 "event_card": glide.event_card(ev)}     # the glide's context card (what + why)
        full = g["tier"] == "full" or hud.get("auto_mode") == "full"   # important: the cross opens by itself
        showing = self.showing = {"id": ev["id"], "done": False, "at": now, "prev": prev_hint_at}
        r = live.dispatch(opts, row, ctx, st["gate"], now, mode="full" if full else "hint",
                          hint_ttl_ms=int(hud.get("hint_ttl_ms", settings.HUD_DEFAULTS["hint_ttl_ms"])),
                          fill_order=hud.get("fill_order"), marker=None if full else HINT_MARKER, mail_fixed=mail_of(ev), env_extra=extra,
                          on_done=lambda reply, sh=showing, ev=ev: self.hint_over(sh, ev, reply))
        if r.get("reason") == "dry_run":
            showing["done"] = True
        log(f"hint {ev['source']}:{ev['class']} {'full' if full else 'ring'} p={g['p']} ({len(opts)} options)"
            f" -> {r.get('reason')}")
        return {"reason": "hint", **info, "payload": r.get("payload")}

    def hint_over(self, showing: dict, ev: dict, reply: dict) -> None:
        """oi-hud answered the hint (pressed / dismissed / dropped; the sender's thread). Busy = try again while live."""
        showing["done"] = True
        if reply.get("event") == "dropped" and reply.get("reason") in HUD_BUSY:
            if self.last_hint_at == showing["at"]:        # never drawn: it does not count for the display limit
                self.last_hint_at = showing["prev"]
            self.q.put({"retry": ev})

    def with_history(self, opts: list[dict], moment: dict | None, st: dict) -> list[dict]:
        """+ the most likely action after this event by history (Markov at the event token), when the event's own
        actions leave a slot and it is not one of them."""
        live = self.live
        if len(opts) >= MAX_OPTIONS or live.model is None or moment is None:
            return opts
        from .. import candidates, forms
        from ..live import ALLOWED_PRIMITIVES, show_p
        try:
            menu = candidates.build(forms.route(moment["context"]), moment, live.model, is_executable=live.is_executable)
        except Exception:
            return opts
        best = [o for o in menu if o.get("primitive") in ("app.open", "url.open", "space.move")
                and o.get("primitive") in ALLOWED_PRIMITIVES and show_p(o) >= st["gate"]["show_min_p"]]
        best.sort(key=lambda o: -show_p(o))
        have = {o["token"] for o in opts}
        for o in best[:1]:
            if o["token"] not in have:
                opts = opts + [o]
        return opts


def live_now_iso(t: float) -> str:
    return vocab.iso_utc(datetime.fromtimestamp(t, timezone.utc))


# ---- `oi proactive-probe` ----------------------------------------------------------------------------------

def cmd_probe(args) -> int:
    """W24: poll the chosen sources twice (`--wait` s apart: prime, then new events), print the event TOKENS only
    (never a name, subject or path), write nothing (no events file, no hint). For install checks."""
    from .config import SOURCES
    cfg = proactive_settings(_raw_settings())
    want = args.source or enabled_sources(cfg) or []
    sens = sensors_mod.default_sensors(None)
    jd = judge_mod.Judge()
    out = {}
    for src in want:
        if src not in SOURCES:
            out[src] = "unknown source"
            continue
        s = sens[src]
        try:
            s.poll(time.time(), cfg)
            if args.wait:
                time.sleep(args.wait)
            evs = s.poll(time.time(), cfg)
            for e in evs:
                if not store.is_gone(e):
                    judge_mod.apply(e, jd)
            out[src] = [store.token(e) + (" (private)" if e.get("private") else "") for e in evs]
        except Exception as e:  # a probe reports, never raises
            out[src] = f"error: {type(e).__name__}"
    import json
    print(json.dumps(out, ensure_ascii=False))
    return 0


def register(sub) -> None:
    p = sub.add_parser("proactive-probe", help="W24: poll proactive sensors once (tokens only; nothing written or shown)")
    p.add_argument("--source", action="append", help="mail | agents | files | cockpit | metrics | markets (default: enabled)")
    p.add_argument("--wait", type=float, default=0.0, help="seconds between the priming poll and the reported one")
    p.set_defaults(func=cmd_probe)
