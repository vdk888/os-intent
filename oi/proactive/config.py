"""W24: settings.json `proactive` (python-owned; oi-menubar's Settings window edits the main keys, OISettings keeps
the rest as it is). Every key optional; a wrongly typed key falls back to its default; every source is OFF until
the user turns it on. No relevance window: an event is hintable exactly while its state is live (docs/DESIGN.md design rule).

  {"proactive": {
     "sources": {"mail": false, "agents": false, "files": false, "cockpit": false, "metrics": false, "markets": false},
     "min_p": 0.5,                  # history probability of a follow-up action needed for a hint (0-1)
     "min_interval_min": 10,        # display: at most one proactive hint per N minutes (priorities skip it)
     "quiet_hours": {"start": "22:00", "end": "08:00"},   # Paris time; null = none
     "respect_focus": true,         # no hint while a macOS Focus is on (when readable)
     "priorities": ["mail:client", "cockpit:outage"],     # source:class (or source:*): skip min_p and the rate limit
     "priorities_break_quiet": false,
     "mail": {"poll_s": 6, "agent_senders": [], "team_domains": []},   # who is a client: the wiki (judge.py), never a settings list
     "agents": {"poll_s": 8,
                "chat_ids": [],                # your Telegram user id(s): the chat your agents' bots write to
                "outbound": {},                # e.g. {"atlas": {"transcripts": "~/.claude/projects/<atlas workspace>",
                                               #                 "state_dir": "~/.claude/channels/atlas"}}: Mac agents'
                                               # own records (sensors.OutboundLog); null removes one
                "bots": {}},                   # {slug: bot username}: "Ouvrir Telegram" opens tg://resolve?domain=<bot>
     "files": {"poll_s": 5, "folders": ["~/Desktop"]},
     "cockpit": {"poll_s": 60, "url": null, "alerts_file": null, "pr_owners": []},   # your ops dashboard (optional)
     "metrics": {"poll_s": 300, "file": null},
     "markets": {"poll_s": 300, "file": null, "move_pct": 3.0},
     "decisions": {"poll_s": 120},             # the dashboard's decision queue: GET <cockpit.url>/a-decider.json with
                                               # a scoped read token from the Keychain (TOKEN_KEYCHAIN; never in settings)
     "calendar": {"poll_s": 60, "horizon_min": 120},          # meetings starting within it, or ongoing (EventKit)
     "prs": {"poll_s": 300, "repos": []}}}   # open PRs of these repos + every PR whose review is requested of you
"""
from __future__ import annotations

import copy
import re

SOURCES = ("mail", "agents", "files", "cockpit", "metrics", "markets", "decisions", "calendar", "prs")
# the token's <source> for each settings source (evt:mail:client, evt:agent:orion, evt:file:desktop, …)
TOKEN_SOURCE = {"mail": "mail", "agents": "agent", "files": "file", "cockpit": "cockpit", "metrics": "metric",
                "markets": "market", "decisions": "cockpit", "calendar": "calendar", "prs": "github"}
# the dashboard's SCOPED read-only token (it should allow GET /a-decider.json only, 403 elsewhere); never an admin
# bearer. Store it once: security add-generic-password -s os-intent-dashboard-read -a oi -w
TOKEN_KEYCHAIN = {"service": "os-intent-dashboard-read", "account": "oi"}
COCKPIT_URL = None
DEFAULTS: dict = {
    "sources": {s: False for s in SOURCES},
    "min_p": 0.5,
    "min_interval_min": 10,
    "quiet_hours": {"start": "22:00", "end": "08:00"},
    "respect_focus": True,
    "priorities": ["mail:client", "cockpit:outage"],
    "priorities_break_quiet": False,
    "mail": {"poll_s": 6, "agent_senders": [], "team_domains": []},
    "agents": {"poll_s": 8, "chat_ids": [], "outbound": {}, "bots": {}},
    "files": {"poll_s": 5, "folders": ["~/Desktop"]},
    "cockpit": {"poll_s": 60, "url": COCKPIT_URL, "alerts_file": None, "pr_owners": []},
    "metrics": {"poll_s": 300, "file": None},
    "markets": {"poll_s": 300, "file": None, "move_pct": 3.0},
    "decisions": {"poll_s": 120},          # the dashboard's decision queue (/a-decider.json, url = cockpit.url)
    "calendar": {"poll_s": 60, "horizon_min": 120},
    "prs": {"poll_s": 300, "repos": []},
}
RANGES = {"min_p": (0.0, 1.0), "min_interval_min": (0, 240)}
POLL_RANGE = (2, 3600)
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SLUG = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_BOT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$")
_CHAT_ID = re.compile(r"^-?\d{3,20}$")
_PRIO = re.compile(r"^(mail|agent|file|cockpit|metric|market|calendar|github):([a-z0-9-]+|\*)$")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _strings(v, lower: bool = False) -> list[str] | None:
    if not isinstance(v, list):
        return None
    out = [x.strip() for x in v if isinstance(x, str) and x.strip()]
    return [x.lower().lstrip("@.") for x in out] if lower else out


def hhmm(s) -> int | None:
    """'22:30' -> minutes since midnight (None when not HH:MM)."""
    if not isinstance(s, str) or not _HHMM.match(s.strip()):
        return None
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def proactive_settings(raw: dict | None) -> dict:
    """The `proactive` block of a raw settings.json dict, defaults filled in, wrongly typed keys ignored."""
    out = copy.deepcopy(DEFAULTS)
    p = (raw or {}).get("proactive") if isinstance(raw, dict) else None
    if not isinstance(p, dict):
        return out
    src = p.get("sources")
    if isinstance(src, dict):
        out["sources"] = {s: (src[s] if isinstance(src.get(s), bool) else False) for s in SOURCES}
    for k, (lo, hi) in RANGES.items():
        if _num(p.get(k)) and lo <= p[k] <= hi:
            out[k] = int(p[k]) if k != "min_p" else float(p[k])
    for k in ("respect_focus", "priorities_break_quiet"):
        if isinstance(p.get(k), bool):
            out[k] = p[k]
    if "quiet_hours" in p:
        q = p["quiet_hours"]
        if q is None:
            out["quiet_hours"] = None
        elif isinstance(q, dict) and hhmm(q.get("start")) is not None and hhmm(q.get("end")) is not None:
            out["quiet_hours"] = {"start": q["start"].strip(), "end": q["end"].strip()}
    pr = _strings(p.get("priorities"), lower=True)
    if pr is not None:
        out["priorities"] = [x for x in pr if _PRIO.match(x)]
    for s in SOURCES:
        sub = p.get(s)
        if not isinstance(sub, dict):
            continue
        if _num(sub.get("poll_s")) and POLL_RANGE[0] <= sub["poll_s"] <= POLL_RANGE[1]:
            out[s]["poll_s"] = float(sub["poll_s"])
        if s == "mail":
            v = _strings(sub.get("agent_senders"), lower=True)
            if v is not None:
                out["mail"]["agent_senders"] = v
            v = _strings(sub.get("team_domains"), lower=True)
            if v is not None:
                out["mail"]["team_domains"] = v[:10]
        elif s == "agents":
            ids = sub.get("chat_ids")
            if isinstance(ids, list):
                out["agents"]["chat_ids"] = [str(x) for x in ids if isinstance(x, (str, int)) and not isinstance(x, bool)
                                             and _CHAT_ID.match(str(x))]
            ob = sub.get("outbound")
            if isinstance(ob, dict):
                for slug, spec in ob.items():
                    if not isinstance(slug, str) or not _SLUG.match(slug):
                        continue
                    if spec is None:
                        out["agents"]["outbound"].pop(slug, None)
                    elif isinstance(spec, dict) and all(isinstance(spec.get(k), str) and spec[k]
                                                        for k in ("transcripts", "state_dir")):
                        out["agents"]["outbound"][slug] = {k: spec[k] for k in ("transcripts", "state_dir")}
            bots = sub.get("bots")
            if isinstance(bots, dict):
                out["agents"]["bots"] = {k: v.lstrip("@") for k, v in bots.items() if isinstance(k, str) and _SLUG.match(k)
                                         and isinstance(v, str) and _BOT.match(v.lstrip("@"))}
        elif s == "files":
            v = _strings(sub.get("folders"))
            if v is not None:
                out["files"]["folders"] = v[:12]
        elif s == "cockpit":
            if isinstance(sub.get("url"), str) and sub["url"].startswith("https://"):
                out["cockpit"]["url"] = sub["url"].rstrip("/")
            if isinstance(sub.get("alerts_file"), str):
                out["cockpit"]["alerts_file"] = sub["alerts_file"]
            v = _strings(sub.get("pr_owners"))
            if v is not None:
                out["cockpit"]["pr_owners"] = [o for o in v if re.match(r"^[A-Za-z0-9_.-]{1,39}$", o)][:20]
        elif s == "calendar":
            if _num(sub.get("horizon_min")) and 5 <= sub["horizon_min"] <= 24 * 60:
                out["calendar"]["horizon_min"] = int(sub["horizon_min"])
        elif s == "prs":
            v = _strings(sub.get("repos"))
            if v is not None:
                out["prs"]["repos"] = [r for r in v if _REPO.match(r)][:20]
        elif s in ("metrics", "markets"):
            if isinstance(sub.get("file"), str):
                out[s]["file"] = sub["file"]
            if s == "markets" and _num(sub.get("move_pct")) and sub["move_pct"] > 0:
                out["markets"]["move_pct"] = float(sub["move_pct"])
    return out


def enabled_sources(cfg: dict) -> list[str]:
    return [s for s in SOURCES if (cfg.get("sources") or {}).get(s) is True]
