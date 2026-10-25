"""W24: the relevance gate — does this event deserve a proactive hint NOW?

Checks, in order (the first failing one is the row's reason):
  gone (its state already ended: context is state, not timers — no age limit) · paused · hud_disabled ·
  auto_off (hud.auto_mode "off": nothing automatic) · denylisted (front app) · quiet_hours / focus (unless a
  priority and priorities_break_quiet) · below_threshold (p < min_p, important events skip it) · rate_limited
  (< min_interval_min since the last proactive hint: a DISPLAY setting, important events skip it).
Importance tier: an IMPORTANT event (a hard priority, an event the ONE wiki judge found
important — judge.py, an active client / prospect / current project / known person page —, or a
sender / topic named in the W22 profile `attention.important`) is dispatched "full": the cross with its actions OPENS, with no window change or pointer move. Every other passing
event is a ring on the dots. Focus and quiet hours apply to both (only `priorities_break_quiet` lets a priority by).
An event held back for a passing reason (TRANSIENT) is considered again while its state is live (engine.py).
p = the history probability that the user acts within the label window after this event token (Markov order-1 counts
at `evt:<source>:<class>`: 1 - P(nothing)), blended with the W22 attention prior (profile.yaml) while history is
thin: p = lam * hist + (1 - lam) * prior, lam = n / (n + HIST_K) — the starter-catalogue rule.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from ..moments import NOTHING, default_tz
from . import store
from .config import hhmm

HIST_K = 5
# the W22 attention source of each token source, and the prior when the profile has none
ATTENTION = {"mail": "mail", "agent": "agents", "file": "files", "cockpit": "alerts", "metric": "metrics",
             "market": "markets",
             "calendar": "calendar", "github": "alerts"}
DEFAULT_PRIOR = {"mail": 0.4, "agent": 0.5, "file": 0.3, "cockpit": 0.6, "metric": 0.4, "market": 0.3, "calendar": 0.6,
                 "github": 0.4}

# reasons that may pass later while the event's state is still live (the engine re-runs the gate on them)
TRANSIENT = frozenset({"paused", "hud_disabled", "denylisted", "quiet_hours", "focus", "rate_limited", "afk",
                       "queued"})

_PROFILE: dict = {"mtime": None, "data": {}}


def load_profile(path: Path | None = None) -> dict:
    """profile.yaml (W22), cached on its mtime; {} when missing or unreadable."""
    from ..starter import profile_path
    p = path or profile_path()
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {}
    if _PROFILE["mtime"] != mt:
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            d = {}
        _PROFILE.update(mtime=mt, data=d if isinstance(d, dict) else {})
    return _PROFILE["data"]


def history(model, tok: str) -> tuple[int, float]:
    """(n, P(an action follows)) at the event token, order-1 Markov counts; (0, 0) when unseen / no model."""
    c = ((getattr(model, "counts", None) or {}).get("o1") or {}).get(tok) if model is not None else None
    if not c:
        return 0, 0.0
    n = sum(c.values())
    return n, (1.0 - c.get(NOTHING, 0) / n) if n else 0.0


def prior(ev: dict, profile: dict) -> float:
    from ..onboard import attention_prior
    who = " ".join(str(x) for x in (ev.get("who"), (ev.get("ref") or {}).get("address"), (ev.get("ref") or {}).get("slug"))
                   if x)
    return float(attention_prior(profile, ATTENTION.get(ev["source"], ev["source"]), who or None,
                                 default=DEFAULT_PRIOR.get(ev["source"], 0.4)))


def probability(ev: dict, model, profile: dict) -> dict:
    n, h = history(model, store.token(ev))
    pr = prior(ev, profile)
    lam = n / (n + HIST_K) if n else 0.0
    return {"p": round(lam * h + (1 - lam) * pr, 3), "hist_n": n, "hist_p": round(h, 3), "prior": round(pr, 3)}


def is_priority(ev: dict, cfg: dict) -> bool:
    pr = cfg.get("priorities") or []
    return f"{ev['source']}:{ev['class']}" in pr or f"{ev['source']}:*" in pr


def importance(ev: dict, cfg: dict, profile: dict | None = None) -> str | None:
    """Why the event is IMPORTANT (-> the full cross), or None (-> a ring): "priority" (a hard priority) | "wiki"
    (judge.py weighed it against the wiki on the sensor thread: `ev["wiki"]`) | "profile" (a W22 `attention.important`
    sender / topic the user declared). Local data only; a private event is matched on its sender, never on a subject."""
    if is_priority(ev, cfg):
        return "priority"
    if (ev.get("wiki") or {}).get("important"):
        return "wiki"
    ref = ev.get("ref") or {}
    who = " ".join(str(x).lower() for x in (ev.get("who"), ref.get("address"), ref.get("slug")) if x)
    subject = "" if ev.get("private") else str(ev.get("subject") or "").lower()
    for r in ((profile or {}).get("attention") or {}).get("important") or []:
        w = str(r.get("who") or "").strip().lower()
        if len(w) >= 3 and (w in who or (r.get("class") == "topic" and w in subject)):
            return "profile"
    return None


def in_quiet(local: datetime, q: dict | None) -> bool:
    if not q:
        return False
    a, b, m = hhmm(q.get("start")), hhmm(q.get("end")), local.hour * 60 + local.minute
    if a is None or b is None or a == b:
        return False
    return a <= m < b if a < b else (m >= a or m < b)


def gate(ev: dict, cfg: dict, *, now: float, last_hint_at: float | None, model=None, profile: dict | None = None,
         live: bool = True, paused: bool = False, hud_enabled: bool = True, auto_mode: str = "hint",
         front_denied: bool = False, focus: bool | None = None, tz=None) -> dict:
    """{"ok", "reason", "p", "priority", "important", "tier", "hist_n", "hist_p", "prior"} for one event. `live`: its
    state is still on. `tier`: "full" (important: the cross opens) or "ring" (the dots with a ring)."""
    imp = importance(ev, cfg, profile)
    out = {"ok": False, "priority": is_priority(ev, cfg), "important": imp, "tier": "full" if imp else "ring",
           **probability(ev, model, profile or {})}

    def no(reason: str) -> dict:
        return dict(out, reason=reason)
    if not live or store.is_gone(ev):
        return no("gone")
    if paused:
        return no("paused")
    if not hud_enabled:
        return no("hud_disabled")
    if auto_mode == "off":
        return no("auto_off")
    if front_denied:
        return no("denylisted")
    quiet_ok = out["priority"] and cfg.get("priorities_break_quiet")
    local = datetime.fromtimestamp(now, tz or default_tz())
    if not quiet_ok and in_quiet(local, cfg.get("quiet_hours")):
        return no("quiet_hours")
    if not quiet_ok and cfg.get("respect_focus", True) and focus:
        return no("focus")
    if not imp and out["p"] < cfg.get("min_p", 0.5):
        return no("below_threshold")
    if not imp and last_hint_at is not None and now - last_hint_at < cfg.get("min_interval_min", 10) * 60:
        return no("rate_limited")
    return dict(out, ok=True, reason="hint")
