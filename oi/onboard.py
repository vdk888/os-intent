"""W22b: `oi onboard` is now an AGENTIC session — Claude reads the person's memory, wiki, mails and Notion; this module keeps the validators it relies on and the W22 deterministic path,
`oi onboard --offline`, described below.

W22: `oi onboard --offline` — a short conversation with Claude that gathers the person's context (opt-in sources,
read-only, summarised then discarded) and writes `profile.yaml` + `starter_catalogue.yaml` (oi/starter.py) so the
HUD can propose useful actions before any history exists.

The user: an onboarding at first launch so the tool is useful from day one and
shareable — "Lena, architect → SketchUp, 3D render requests to her cloud; us → our agent team". The Q&A also asks what deserves attention vs noise → attention priors in profile.yaml (W24's
relevance gate starts from them).

Flow (each step skippable; Ctrl-C = nothing written):
  1. the privacy statement, then each SOURCE with its privacy line and an explicit y/N (installed apps: always);
  2. the core QUESTIONS (typed, or dictated with the macOS dictation key), your agent team (the roster) as counterparts
     when the person says so (the W25 roster: `oi.orchestrator.roster`);
  2b. LIST AND CONFIRM (before any Claude prompt): every name read from the sources (apps, sites, Drive folders,
     Mail accounts / mailboxes, Notion) is listed and the person removes what must not go to Claude; only the
     confirmed names are ever sent (non-interactive: none; `--send-source-names` for a synthetic sources file);
  3. ≤ 3 follow-up questions from Claude (one sonnet call, `--no-followups` skips it);
  4. one sonnet call turns answers + source summaries into the profile and 15-30 catalogue items; every item is
     VALIDATED (primitive, params, app installed / in the person's app list, https host, roster slug, prior range,
     trigger shape) and topped up / backed by the local builder (`local_catalogue`, no Claude) to 15-30; an item
     whose target or trigger app / host is what the person called noise (profile attention.noise) is dropped;
  5. both files written 0600 in the data dir (`--out DIR` elsewhere, `--dry-run` prints only).

Privacy: every text sent to Claude goes through the W25 gate (`oi.orchestrator.pack.leaks` over
settings.claude.local_only_domains): a line naming a local-only domain / client is dropped before the prompt, and
any answer item that still names one is dropped after. Denylisted apps never appear. Source summaries keep NAMES
and COUNTS only (app names, site hosts, hours, folder / mailbox names): never a title, a URL path, a file, a mail.
The answers themselves stay in memory; the profile keeps Claude's short structured summary of them (job, tools,
vocabulary, counterparts, attention), never a document. Claude runs only through claude_actions.run_claude
(subscription seat, no API key, `--tools ""`); Claude off or unavailable → the local builder alone.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import yaml

from . import aw, claude_actions as ca, paths, settings as oi_settings, starter, vocab
from .intent_resolve import installed_apps

MODEL = "sonnet"
TIMEOUT_S = 120
MIN_ITEMS, MAX_ITEMS = 15, 30
PRIOR_RANGE = (0.05, 0.6)
LABEL_MAX = 22
TEXT_MAX = 300
ACTIVITY_DAYS = 14
TOP_APPS, TOP_HOSTS = 25, 25
FORMAT_PROFILE, FORMAT_CATALOGUE = "oi-profile/1", "oi-starter-catalogue/1"
SYSTEM_APPS = {"App Store", "System Settings", "System Preferences", "Finder", "Activity Monitor", "Console",
               "Terminal", "Disk Utility", "Migration Assistant", "Boot Camp Assistant", "Bluetooth File Exchange",
               "Audio MIDI Setup", "ColorSync Utility", "Directory Utility", "Keychain Access", "Passwords",
               "Screenshot", "Script Editor", "System Information", "VoiceOver Utility", "Grapher",
               "Digital Color Meter", "Font Book", "Image Capture", "Automator", "Stocks", "Tips", "Chess",
               "Photo Booth", "Time Machine", "Mission Control", "Launchpad", "Siri", "Stickies",
               "loginwindow", "SecurityAgent", "UserNotificationCenter", "universalAccessAuthWarn", "CoreServicesUIAgent",
               "oi-menubar", "oi-hud", "oi-watcher", "Dock", "Spotlight", "Control Center", "NotificationCenter"}
NOT_A_SITE = re.compile(r"^(localhost|\d+(\.\d+){3}|\[.*\])(:\d+)?$|\.(local|internal|lan|ts\.net)(:\d+)?$|:\d+$"
                        r"|^(accounts|login|auth|sso|signin|id)\.")
MIN_WINDOW_H = 4

PRIVACY = ("Everything stays on this Mac. Each source below is read only if you say yes, read-only, and only\n"
           "names and counts are kept (app names, site hosts, folder names) — never a document, a page, a mail or a\n"
           "title. Your answers and those summaries go once to Claude (your subscription, never an API key) to\n"
           "build the profile; domains you marked local-only (Settings → Claude) are removed before that.")

SOURCES = [   # key, title, privacy line, always on
    ("apps", "Installed apps", "Reads the NAMES of the apps in /Applications and ~/Applications. Nothing else.", True),
    ("activity", "Recent activity (ActivityWatch, this Mac)",
     f"Counts app switches and site visits of the last {ACTIVITY_DAYS} days (oi's own token files, else the local "
     "aw-server at 127.0.0.1:5600): app names, site HOSTS and hours only; titles and URL paths are never kept.", False),
    ("safari", "Safari top sites and bookmarks",
     "Reads the HOSTS of Safari's Top Sites and bookmarks (no history, no page, no title). Needs Full Disk Access "
     "for this terminal; without it, skipped.", False),
    ("drive", "Google Drive folder structure",
     "Reads the NAMES of the first two folder levels of Google Drive for desktop (~/Library/CloudStorage). Never "
     "a file name below that, never a file's content.", False),
    ("mail", "Mail mailboxes",
     "Asks Mail for its account and mailbox NAMES only (no message, subject or sender). macOS may ask once for "
     "Automation access.", False),
    ("notion", "Notion",
     "No Notion connector is available on this Mac yet: nothing is read (stub).", False),
]

QUESTIONS = [
    ("job", "What is your job, in a sentence or two? (what you do, for whom)"),
    ("tools", "Which tools do you use every day? (apps, sites, services — and what for)"),
    ("clients", "Who are your typical clients or projects right now? (names are fine: it stays on this Mac)"),
    ("tasks", "Which tasks come back every day or week? (e.g. 'Monday: weekly report', 'send renders to the client')"),
    ("counterparts", "Who do you hand work to, or ask things of — people, agents, services? How (mail, Telegram, "
                     "a portal)?"),
    ("attention", "What deserves your attention, and what is noise? (senders, clients, agents, alerts, metrics, "
                  "markets — important vs ignore)"),
    ("other", "Anything else the assistant should know to propose the right next step?"),
]


def log(msg: str) -> None:
    print(f"oi-onboard: {msg}", file=sys.stderr, flush=True)


# ---- sources (opt-in, read-only, names + counts only) -----------------------------------------------------

def is_site(host: str) -> bool:
    """A public site worth proposing (not localhost / an IP / a LAN, tailnet or :port host / a login redirect)."""
    return bool(host) and "." in host and not NOT_A_SITE.search(host) and not re.search(r"\.tail[0-9a-f]+\.", host)


def _visible_app(name: str, deny: list[str]) -> bool:
    return bool(name) and name not in SYSTEM_APPS and not any(name.lower() == d.lower() for d in deny)


def read_apps(deny: list[str], dirs: list[str] | None = None) -> dict:
    names = [a for a in installed_apps(dirs) if _visible_app(a, deny)]
    return {"names": names, "count": len(names)}


def _local_hour(ts: str) -> int | None:
    try:
        return vocab.parse_ts(ts).astimezone().hour
    except (ValueError, TypeError):
        return None


def summarise_tokens(rows: Iterable[dict], deny: list[str]) -> dict:
    """Token rows -> {top_apps, top_hosts, hours (peak window per app / host), after (most common preceding app)}."""
    apps, hosts = Counter(), Counter()
    hours: dict[str, Counter] = defaultdict(Counter)
    after: dict[str, Counter] = defaultdict(Counter)
    days = set()
    prev_app = None
    for r in rows:
        tok = r.get("token") if isinstance(r, dict) else None
        if not isinstance(tok, str):
            continue
        kind, _, arg = tok.partition(":")
        h = _local_hour(r.get("ts") or "")
        days.add((r.get("ts") or "")[:10])
        if kind == "activate" and arg:
            if not _visible_app(arg, deny):
                prev_app = None
                continue
            key = f"app:{arg}"
            apps[arg] += 1
            if prev_app and prev_app != arg:
                after[key][prev_app] += 1
            prev_app = arg
        elif kind == "url" and arg:
            host = arg.split("/", 1)[0].removeprefix("www.")
            if not is_site(host):
                continue
            key = f"host:{host}"
            hosts[host] += 1
            if prev_app:
                after[key][prev_app] += 1
        else:
            continue
        if h is not None:
            hours[key][h] += 1
    out = {"days": len(days - {""}), "top_apps": apps.most_common(TOP_APPS), "top_hosts": hosts.most_common(TOP_HOSTS),
           "hours": {}, "after": {}}
    for key in [f"app:{a}" for a, _ in out["top_apps"]] + [f"host:{h}" for h, _ in out["top_hosts"]]:
        if hours.get(key):
            out["hours"][key] = peak_window(hours[key], min_width=MIN_WINDOW_H)
        if after.get(key):
            out["after"][key] = after[key].most_common(1)[0][0]
    return out


def peak_window(c: Counter, cover: float = 0.8, min_width: int = 1) -> list[int]:
    """The narrowest [h0, h1) window (in whole hours, no wrap) holding ≥ `cover` of the counts, widened
    symmetrically to `min_width` hours (a few days of data must not pin an action to one hour)."""
    total = sum(c.values())
    best = [0, 24]
    for h0 in range(24):
        acc = 0
        for h1 in range(h0 + 1, 25):
            acc += c.get(h1 - 1, 0)
            if acc >= cover * total:
                if h1 - h0 < best[1] - best[0]:
                    best = [h0, h1]
                break
    while best[1] - best[0] < min(min_width, 24):
        if best[1] < 24:
            best[1] += 1
        if best[1] - best[0] < min_width and best[0] > 0:
            best[0] -= 1
    return best


def read_activity(deny: list[str], *, days: int = ACTIVITY_DAYS, fetch: Callable | None = None) -> dict:
    """oi's own token files first (already normalised), else the aw-server's window / web buckets in memory."""
    files = sorted(paths.tokens_dir().glob("????-??-??.jsonl"))[-days:]
    rows: list[dict] = []
    for f in files:
        try:
            rows += aw.read_jsonl(f)
        except (OSError, ValueError):
            continue
    if rows:
        return {"from": "tokens", **summarise_tokens(rows, deny)}
    try:
        rows = (fetch or _aw_rows)(days)
    except OSError as e:
        return {"from": "unavailable", "why": f"aw-server unreachable ({type(e).__name__})"}
    return {"from": "aw-server", **summarise_tokens(rows, deny)}


def _aw_rows(days: int) -> list[dict]:
    """Window + web events of the last `days` from the local aw-server, as minimal token rows (kept in memory)."""
    from datetime import timedelta
    start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = []
    with urllib.request.urlopen(f"{aw.server_url()}/api/0/info", timeout=2):
        pass
    for b in aw.list_buckets():
        kind = "app" if b.startswith("aw-watcher-window") else ("web" if b.startswith("aw-watcher-web") else None)
        if not kind:
            continue
        for e in aw.fetch_events(b, start):
            d = e.get("data") or {}
            if kind == "app" and d.get("app"):
                out.append({"ts": e["timestamp"], "token": f"activate:{d['app']}"})
            elif kind == "web" and d.get("url"):
                h = starter.host_of(d["url"])
                if h:
                    out.append({"ts": e["timestamp"], "token": f"url:{h}/x"})
    out.sort(key=lambda r: r["ts"])
    return out


def _bookmark_urls(node) -> Iterable[str]:
    if isinstance(node, dict):
        if isinstance(node.get("URLString"), str):
            yield node["URLString"]
        for ch in node.get("Children") or []:
            yield from _bookmark_urls(ch)


def read_safari(home: Path | None = None) -> dict:
    base = (home or Path.home()) / "Library" / "Safari"
    hosts = Counter()
    errors = []
    for name in ("TopSites.plist", "Bookmarks.plist"):
        try:
            with open(base / name, "rb") as f:
                d = plistlib.load(f)
        except PermissionError:
            errors.append(f"{name}: needs Full Disk Access")
            continue
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        urls = [t.get("TopSiteURLString") for t in d.get("TopSites") or [] if isinstance(t, dict)] \
            if name == "TopSites.plist" else list(_bookmark_urls(d))
        for u in urls:
            h = starter.host_of(u) if isinstance(u, str) and u.startswith("http") else ""
            if h:
                hosts[h] += 3 if name == "TopSites.plist" else 1
    return {"hosts": [h for h, _ in hosts.most_common(40)], **({"why": "; ".join(errors)} if errors and not hosts else {})}


def read_drive(home: Path | None = None) -> dict:
    root = (home or Path.home()) / "Library" / "CloudStorage"
    folders = []
    try:
        drives = [d for d in root.iterdir() if d.name.startswith("GoogleDrive-")]
    except OSError:
        drives = []
    for d in drives:
        for top in ("My Drive", "Mon Drive", "Shared drives", "Drive partagés"):
            base = d / top
            try:
                level1 = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
            except OSError:
                continue
            for p in level1[:30]:
                try:
                    kids = sorted(c.name for c in p.iterdir() if c.is_dir() and not c.name.startswith("."))[:10]
                except OSError:
                    kids = []
                folders.append({"name": p.name, "sub": kids})
    return {"folders": folders} if folders else {"folders": [], "why": "Google Drive for desktop not found"}


MAILBOXES_AS = r'''set out to ""
tell application "Mail"
  repeat with a in accounts
    set out to out & (name of a) & linefeed
    repeat with m in mailboxes of a
      set out to out & "  " & (name of m) & linefeed
    end repeat
  end repeat
end tell
return out'''


def read_mail(osa: Callable | None = None) -> dict:
    rc, out = (osa or ca.osascript)(MAILBOXES_AS, [], 15)
    if rc != 0:
        return {"accounts": [], "why": "Mail not readable (not running or Automation refused)"}
    accounts: list[dict] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        if not line.startswith("  "):
            accounts.append({"name": line.strip(), "mailboxes": []})
        elif accounts:
            accounts[-1]["mailboxes"].append(line.strip())
    return {"accounts": accounts}


def read_source(key: str, deny: list[str]) -> dict:
    if key == "apps":
        return read_apps(deny)
    if key == "activity":
        return read_activity(deny)
    if key == "safari":
        return read_safari()
    if key == "drive":
        return read_drive()
    if key == "mail":
        return read_mail()
    return {"why": "no connector on this Mac (stub)"}


# ---- privacy gate (W25) ------------------------------------------------------------------------------------

def gate_sources(src: dict, cfg: dict) -> dict:
    """Drop every name / host naming a local-only domain or client (W25 pack.leaks)."""
    from .orchestrator import pack
    terms = pack.deny_terms(cfg)

    def ok(s) -> bool:
        return not pack.leaks(str(s), cfg, terms) and not ca.local_only(str(s), cfg)
    out = json.loads(json.dumps(src))
    if "apps" in out:
        out["apps"]["names"] = [a for a in out["apps"].get("names") or [] if ok(a)]
    a = out.get("activity") or {}
    for k in ("top_apps", "top_hosts"):
        if k in a:
            a[k] = [x for x in a[k] if ok(x[0])]
    for k in ("hours", "after"):
        if k in a:
            a[k] = {key: v for key, v in a[k].items() if ok(key.split(":", 1)[1]) and ok(v)}
    if "safari" in out:
        out["safari"]["hosts"] = [h for h in out["safari"].get("hosts") or [] if ok(h)]
    if "drive" in out:
        out["drive"]["folders"] = [{"name": f["name"], "sub": [s for s in f.get("sub") or [] if ok(s)]}
                                   for f in out["drive"].get("folders") or [] if ok(f["name"])]
    if "mail" in out:
        out["mail"]["accounts"] = [{"name": x["name"], "mailboxes": [m for m in x.get("mailboxes") or [] if ok(m)]}
                                   for x in out["mail"].get("accounts") or [] if ok(x["name"])]
    return out


def gate_answers(answers: dict, cfg: dict) -> tuple[dict, int]:
    """Answers with every line / sentence naming a local-only term removed; (answers, number of parts withheld)."""
    from .orchestrator import pack
    terms = pack.deny_terms(cfg)
    out, withheld = {}, 0
    for k, v in answers.items():
        if not isinstance(v, str):
            continue
        parts = [p for p in re.split(r"(?<=[.;!?\n])\s+", v) if p.strip()]
        keep = [p for p in parts if not pack.leaks(p, cfg, terms)]
        withheld += len(parts) - len(keep)
        out[k] = " ".join(keep)[:2000]
    return out, withheld


# ---- list and confirm (the names a Claude prompt would carry) ----------------------------------------------

def source_names(src: dict) -> list[tuple[str, str]]:
    """Every source-derived name a Claude prompt would carry, as (kind, name), in prompt order, unique."""
    out: list[tuple[str, str]] = [("app", a) for a in (src.get("apps") or {}).get("names") or []]
    a = src.get("activity") or {}
    out += [("app", x[0]) for x in a.get("top_apps") or []]
    out += [("site", x[0]) for x in a.get("top_hosts") or []]
    out += [("site", h) for h in (src.get("safari") or {}).get("hosts") or []]
    for f in (src.get("drive") or {}).get("folders") or []:
        out += [("drive folder", f["name"])] + [("drive folder", f"{f['name']}/{s}") for s in f.get("sub") or []]
    for acc in (src.get("mail") or {}).get("accounts") or []:
        out += [("mail account", acc["name"])] + [("mailbox", f"{acc['name']}/{m}") for m in acc.get("mailboxes") or []]
    out += [("notion", n) for n in (src.get("notion") or {}).get("names") or []]
    return list(dict.fromkeys((k, str(n)) for k, n in out))


def keep_names(src: dict, allowed: Iterable[tuple[str, str]]) -> dict:
    """The sources with every name NOT in `allowed` removed (an allowlist: a name never shown is never sent)."""
    ok = {(k, n) for k, n in allowed}
    app, site = (lambda n: ("app", n) in ok), (lambda n: ("site", n) in ok)
    out = json.loads(json.dumps(src))
    if "apps" in out:
        out["apps"]["names"] = [x for x in out["apps"].get("names") or [] if app(x)]
        out["apps"]["count"] = len(out["apps"]["names"])
    a = out.get("activity") or {}
    if "top_apps" in a:
        a["top_apps"] = [x for x in a["top_apps"] if app(x[0])]
    if "top_hosts" in a:
        a["top_hosts"] = [x for x in a["top_hosts"] if site(x[0])]
    for k in ("hours", "after"):
        if k in a:
            a[k] = {key: v for key, v in a[k].items()
                    if (app if key.startswith("app:") else site)(key.split(":", 1)[1]) and (k == "hours" or app(v))}
    if "safari" in out:
        out["safari"]["hosts"] = [h for h in out["safari"].get("hosts") or [] if site(h)]
    if "drive" in out:
        out["drive"]["folders"] = [{"name": f["name"], "sub": [s for s in f.get("sub") or []
                                                               if ("drive folder", f"{f['name']}/{s}") in ok]}
                                   for f in out["drive"].get("folders") or [] if ("drive folder", f["name"]) in ok]
    if "mail" in out:
        out["mail"]["accounts"] = [{"name": x["name"], "mailboxes": [m for m in x.get("mailboxes") or []
                                                                     if ("mailbox", f"{x['name']}/{m}") in ok]}
                                   for x in out["mail"].get("accounts") or [] if ("mail account", x["name"]) in ok]
    if "notion" in out and "names" in out["notion"]:
        out["notion"]["names"] = [n for n in out["notion"]["names"] if ("notion", n) in ok]
    return out


def _picks(text: str, n: int) -> set[int] | None:
    """'3 5-8, 12' -> {3, 5, 6, 7, 8, 12} (1-based, ≤ n); None when it does not parse."""
    out: set[int] = set()
    for part in re.split(r"[\s,]+", text.strip()):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            return None
        lo, hi = int(m[1]), int(m[2] or m[1])
        if not 1 <= lo <= hi <= n:
            return None
        out.update(range(lo, hi + 1))
    return out


def confirm_names(io: "IO", sources: dict, cfg: dict) -> set[tuple[str, str]]:
    """Show every source name that would go to Claude (after the local-only gate) and let the person remove
    entries. Returns the CONFIRMED names (the only ones a prompt may carry). Non-interactive: none."""
    names = source_names(gate_sources(sources, cfg))
    if not names:
        return set()
    if not io.interactive:
        io.say(f"(not interactive: the {len(names)} names read from your sources stay on this Mac; Claude gets "
               "your answers only)")
        return set()
    io.say(f"These {len(names)} names from your sources would go to Claude with your answers (local-only ones "
           "already removed). Remove any that must stay on this Mac:")
    for i, (kind, name) in enumerate(names, 1):
        io.say(f"  {i:>3}. {kind}: {name}")
    while True:
        r = io.ask("Numbers to remove (e.g. 3 5-8), 'all' to send none of them, Enter to send them as listed\n> ")
        if r.lower() in ("all", "tout", "tous"):
            io.say("  → none of them goes to Claude (the local builder still uses them, on this Mac)")
            return set()
        drop = _picks(r, len(names)) if r else set()
        if drop is not None:
            break
        io.say(f"  (numbers 1-{len(names)}, ranges like 5-8, 'all', or Enter)")
    kept = {x for i, x in enumerate(names, 1) if i not in drop}
    io.say(f"  → {len(kept)} names go to Claude, {len(drop)} stay on this Mac")
    return kept


# ---- local builder (no Claude) -----------------------------------------------------------------------------

def _prior(n: int, total: int, lo: float = 0.12, hi: float = 0.45) -> float:
    return round(lo + (hi - lo) * min(1.0, 4 * n / max(1, total)), 3)


def local_catalogue(sources: dict, *, agents: list[dict] | None = None, apps_ok: set[str] | None = None) -> list[dict]:
    """Starter items from the source summaries alone: the most used apps and sites (trigger = their usual hours
    and the app they usually follow), the person's other installed tools, then "Ask <agent>" for the team."""
    items: list[dict] = []
    act = sources.get("activity") or {}
    hours, after = act.get("hours") or {}, act.get("after") or {}
    tot_a = sum(n for _, n in act.get("top_apps") or []) or 1
    for app, n in (act.get("top_apps") or [])[:10]:
        if apps_ok is not None and app not in apps_ok:
            continue
        trig = {"hours": hours.get(f"app:{app}", [8, 20])}
        if after.get(f"app:{app}"):
            trig["apps"] = [after[f"app:{app}"]]
        items.append({"id": "app-" + vocab.slug(app, 30), "label": app[:LABEL_MAX], "primitive": "app.open",
                      "params": {"app": app}, "trigger": trig, "prior": _prior(n, tot_a), "source": "activity"})
    tot_h = sum(n for _, n in act.get("top_hosts") or []) or 1
    for host, n in (act.get("top_hosts") or [])[:10]:
        trig = {"hours": hours.get(f"host:{host}", [8, 20])}
        if after.get(f"host:{host}"):
            trig["apps"] = [after[f"host:{host}"]]
        items.append({"id": "site-" + vocab.slug(host, 30), "label": host[:LABEL_MAX], "primitive": "url.open",
                      "params": {"href": f"https://{host}/"}, "trigger": trig, "prior": _prior(n, tot_h),
                      "source": "activity"})
    seen_hosts = {starter.host_of(i["params"].get("href")) for i in items}
    for host in (sources.get("safari") or {}).get("hosts") or []:
        if is_site(host) and host not in seen_hosts and len([i for i in items if i["source"] == "safari"]) < 5:
            items.append({"id": "site-" + vocab.slug(host, 30), "label": host[:LABEL_MAX], "primitive": "url.open",
                          "params": {"href": f"https://{host}/"}, "trigger": {"hours": [8, 20]}, "prior": 0.1,
                          "source": "safari"})
            seen_hosts.add(host)
    for a in agents or []:
        items.append({"id": "ask-" + a["slug"], "label": f"Ask {a.get('name') or a['slug']}"[:LABEL_MAX],
                      "primitive": "agent.ask", "params": {"to": a["slug"]}, "trigger": {"hours": [8, 21]},
                      "prior": 0.08, "source": "roster", "why": (a.get("ask_for") or "")[:120]})
    seen_apps = {i["params"].get("app") for i in items}
    for app in (sources.get("apps") or {}).get("names") or []:
        if len(items) >= MIN_ITEMS:
            break
        if app not in seen_apps and (apps_ok is None or app in apps_ok):
            items.append({"id": "app-" + vocab.slug(app, 30), "label": app[:LABEL_MAX], "primitive": "app.open",
                          "params": {"app": app}, "trigger": {"hours": [8, 20]}, "prior": 0.06, "source": "apps"})
            seen_apps.add(app)
    return items


# ---- Claude ------------------------------------------------------------------------------------------------

def _sources_text(src: dict) -> str:
    lines = []
    if src.get("apps"):
        lines.append(f"Installed apps ({src['apps']['count']}): " + ", ".join(src["apps"]["names"][:150]))
    a = src.get("activity") or {}
    if a.get("top_apps") or a.get("top_hosts"):
        lines.append(f"Activity over {a.get('days', '?')} days (count, usual hours, usually after):")
        for kind, lst in (("app", a.get("top_apps") or []), ("host", a.get("top_hosts") or [])):
            for name, n in lst:
                k = f"{kind}:{name}"
                h = a.get("hours", {}).get(k)
                lines.append(f"  {k} ×{n}" + (f" hours {h[0]}-{h[1]}h" if h else "")
                             + (f" after {a['after'][k]}" if a.get("after", {}).get(k) else ""))
    if (src.get("safari") or {}).get("hosts"):
        lines.append("Safari top sites / bookmarks (hosts): " + ", ".join(src["safari"]["hosts"]))
    for f in (src.get("drive") or {}).get("folders") or []:
        lines.append(f"Drive folder: {f['name']}" + (f" ({', '.join(f['sub'])})" if f.get("sub") else ""))
    for acc in (src.get("mail") or {}).get("accounts") or []:
        lines.append(f"Mail account: {acc['name']} — mailboxes: {', '.join(acc['mailboxes'][:30])}")
    return "\n".join(lines) or "(no source chosen)"


def _answers_text(answers: dict) -> str:
    asked = dict(QUESTIONS)
    out = []
    for k, v in answers.items():
        if isinstance(v, str) and v.strip():
            out.append(f"Q: {asked.get(k, k)}\nA: {v.strip()}")
    return "\n\n".join(out) or "(no answer)"


def _roster_text(agents: list[dict] | None) -> str:
    if not agents:
        return "(no agent team: counterparts are people and services; use url.open / mail.draft for them)"
    from . import orchestrator
    return orchestrator.roster_text(agents)


def followups(answers: dict, sources: dict, agents: list[dict] | None, *, run: Callable = ca.run_claude) -> list[str]:
    prompt = ca.render_prompt("onboard_followups", {"answers": _answers_text(answers), "sources": _sources_text(sources),
                                                    "roster": _roster_text(agents)})
    ans = run(prompt, MODEL, timeout=TIMEOUT_S)
    qs = ans.get("questions") if isinstance(ans, dict) else None
    return [q.strip()[:TEXT_MAX] for q in qs or [] if isinstance(q, str) and q.strip()][:3]


def synthesise(answers: dict, sources: dict, agents: list[dict] | None, *, run: Callable = ca.run_claude) -> dict:
    prompt = ca.render_prompt("onboard", {"answers": _answers_text(answers), "sources": _sources_text(sources),
                                          "roster": _roster_text(agents), "min_items": MIN_ITEMS, "max_items": MAX_ITEMS})
    ans = run(prompt, MODEL, timeout=TIMEOUT_S)
    if not isinstance(ans, dict):
        raise ca.ClaudeUnavailable("bad_json")
    return ans


# ---- validation --------------------------------------------------------------------------------------------

def _s(v, n: int = TEXT_MAX) -> str | None:
    return " ".join(v.split())[:n] if isinstance(v, str) and v.strip() else None


def _strs(v, n: int = 30, m: int = 80) -> list[str]:
    return [x for x in (_s(i, m) for i in (v if isinstance(v, list) else [])) if x][:n]


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]{1,30}://\S*$", re.I)
_BAD_SCHEMES = ("file", "javascript", "data", "http", "https", "ssh", "x-apple.systempreferences")


def validate_item(it, *, apps: set[str], agents: set[str], cfg: dict, terms: list[str]) -> dict | None:
    """One Claude catalogue item -> a clean item, or None (dropped)."""
    from .orchestrator import pack
    if not isinstance(it, dict):
        return None
    prim, p = it.get("primitive"), it.get("params") if isinstance(it.get("params"), dict) else {}
    label = _s(it.get("label"), LABEL_MAX)
    if prim not in starter.PRIMITIVES or prim == "folder.open" or not label:
        return None
    params: dict = {}
    if prim == "app.open":
        app = _s(p.get("app"), 60)
        if not app or app not in apps:
            return None
        params["app"] = app
        dl = _s(p.get("deeplink"), 200)
        if dl and _SCHEME.match(dl) and dl.split(":", 1)[0].lower() not in _BAD_SCHEMES:
            params["deeplink"] = dl
    elif prim == "url.open":
        href = starter.safe_href(_s(p.get("href") or p.get("url"), 300))
        if not href or "?" in href or "#" in href:
            return None
        if href.startswith("http://"):
            href = "https://" + href[7:]
        if ca.local_only(starter.host_of(href), cfg):
            return None
        params["href"] = href
    elif prim == "mail.draft":
        to = _s(p.get("to"), 120)
        if to and not _EMAIL.match(to):
            to = None
        if to and ca.local_only(ca.domain_of(to), cfg):
            return None
        params = {"to": to or "", "subject": _s(p.get("subject"), 120) or "", "body": (p.get("body") or "")[:600]
                  if isinstance(p.get("body"), str) else ""}
    elif prim == "agent.ask":
        to = _s(p.get("to"), 30)
        if to not in agents:
            return None
        params["to"] = to
    if pack.leaks(json.dumps([label, params], ensure_ascii=False), cfg, terms):
        return None
    t = it.get("trigger") if isinstance(it.get("trigger"), dict) else {}
    trig: dict = {}
    ta = [a for a in _strs(t.get("apps"), 6, 60) if a in apps]
    th = [starter.host_of(h) for h in _strs(t.get("hosts"), 6, 120)]
    th = [h for h in th if h and "." in h and not ca.local_only(h, cfg)]
    if ta:
        trig["apps"] = ta
    if th:
        trig["hosts"] = th
    hrs = t.get("hours")
    if isinstance(hrs, list) and len(hrs) == 2 and all(isinstance(h, int) and 0 <= h <= 24 for h in hrs) and hrs[0] != hrs[1]:
        trig["hours"] = [hrs[0], hrs[1]]
    days = [d for d in _strs(t.get("days"), 7, 3) if d in starter.DAYS]
    if days:
        trig["days"] = days
    try:
        prior = float(it.get("prior"))
    except (TypeError, ValueError):
        prior = 0.15
    out = {"id": vocab.slug(_s(it.get("id"), 40) or label, 40) or "item", "label": label, "primitive": prim,
           "params": params, "trigger": trig, "prior": round(min(max(prior, PRIOR_RANGE[0]), PRIOR_RANGE[1]), 3),
           "source": "claude"}
    why = _s(it.get("why"), 160)
    if why and not pack.leaks(why, cfg, terms):
        out["why"] = why
    return out


def _target(it: dict) -> str:
    p = it["params"]
    return {"app.open": f"app:{p.get('app')}:{p.get('deeplink', '')}", "url.open": f"url:{p.get('href')}",
            "agent.ask": f"agent:{p.get('to')}", "mail.draft": f"mail:{p.get('to')}:{p.get('subject')}"}.get(
        it["primitive"], it["id"])


def merge_catalogue(claude_items: list[dict], local: list[dict]) -> list[dict]:
    """Claude's items first, then local ones for targets it did not cover; 15-30 items; unique ids."""
    out: list[dict] = []
    seen_t, seen_id = set(), set()
    for it in claude_items + local:
        t = _target(it)
        host = starter.host_of(it["params"].get("href")) if it["primitive"] == "url.open" else ""
        if t in seen_t or (it["source"] != "claude" and host and f"host:{host}" in seen_t):
            continue                         # a local site Claude already covers (maybe on a deeper page)
        if host:
            seen_t.add(f"host:{host}")
        if it["source"] != "claude" and len(out) >= MIN_ITEMS and it["source"] in ("apps", "safari"):
            continue
        base, n = it["id"], 2
        while it["id"] in seen_id:
            it = {**it, "id": f"{base}-{n}"}
            n += 1
        seen_t.add(t)
        seen_id.add(it["id"])
        out.append(it)
    if len(out) > MAX_ITEMS:
        out = sorted(out, key=lambda i: -i["prior"])[:MAX_ITEMS]
    return out


def _noise_key(who: str) -> str:
    w = who.strip().lower()
    return starter.host_of(w) if "://" in w or w.startswith("www.") else w


def is_noise(it: dict, noise: set[str]) -> bool:
    """An item whose target (app, site, agent, mail address / domain) or trigger app / host is what the person
    called noise (profile attention.noise `who`, exact name: "Pinterest" or "pinterest.com" both match the site)."""
    if not noise:
        return False
    p, t = it.get("params") or {}, it.get("trigger") or {}
    apps = [p.get("app")] + list(t.get("apps") or []) if it.get("primitive") == "app.open" else list(t.get("apps") or [])
    hosts = ([starter.host_of(p.get("href"))] if it.get("primitive") == "url.open" else []) + \
        [starter.host_of(h) for h in t.get("hosts") or [] if isinstance(h, str)]
    names = {str(a).lower() for a in apps if a}
    if it.get("primitive") in ("agent.ask", "mail.draft") and isinstance(p.get("to"), str) and p["to"]:
        names.add(p["to"].lower())
        if "@" in p["to"]:
            hosts.append(p["to"].rsplit("@", 1)[1].lower())
    if names & noise:
        return True
    for h in filter(None, hosts):
        name = h.split(".")[-2] if h.count(".") else h          # pinterest.com, fr.pinterest.com -> pinterest
        if any(h == w or h.endswith("." + w) or w == name for w in noise):
            return True
    return False


def drop_noise(items: list[dict], profile: dict) -> list[dict]:
    noise = {_noise_key(r["who"]) for r in ((profile or {}).get("attention") or {}).get("noise") or []
             if isinstance(r, dict) and isinstance(r.get("who"), str) and r["who"].strip()}
    return [i for i in items if not is_noise(i, noise)]


PROFILE_LISTS = ("domain_vocabulary", "tools", "recurring_tasks", "clients_projects")


def validate_profile(p, cfg: dict, terms: list[str]) -> dict:
    from .orchestrator import pack
    p = p if isinstance(p, dict) else {}

    def clean(v):
        return v if v and not pack.leaks(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), cfg, terms) else None
    out: dict = {"job": clean(_s(p.get("job"))), "summary": clean(_s(p.get("summary"), 600))}
    for k in PROFILE_LISTS:
        out[k] = [x for x in (clean(s) for s in _strs(p.get(k), 30, 120)) if x]
    cps = []
    for c in p.get("counterparts") if isinstance(p.get("counterparts"), list) else []:
        if not isinstance(c, dict) or not _s(c.get("name"), 60):
            continue
        row = {"name": _s(c.get("name"), 60), "kind": c.get("kind") if c.get("kind") in ("agent", "person", "service") else "person"}
        for k in ("slug", "ask_for", "via"):
            if _s(c.get(k), 160):
                row[k] = _s(c.get(k), 160)
        if clean(json.dumps(row, ensure_ascii=False)):
            cps.append(row)
    out["counterparts"] = cps[:20]
    out["attention"] = validate_attention(p.get("attention"), clean)
    return {k: v for k, v in out.items() if v not in (None, [], {})}


ATTENTION_SOURCES = ("mail", "telegram", "agents", "alerts", "calendar", "markets", "metrics", "news")


def validate_attention(a, clean: Callable = lambda v: v) -> dict:
    """{sources: {mail: 0-1, …}, important: [{who, class, weight}], noise: [{who, class, weight}]}."""
    a = a if isinstance(a, dict) else {}
    out: dict = {"sources": {}, "important": [], "noise": []}
    src = a.get("sources") if isinstance(a.get("sources"), dict) else {}
    for k, v in src.items():
        if k in ATTENTION_SOURCES and isinstance(v, (int, float)) and not isinstance(v, bool):
            out["sources"][k] = round(min(max(float(v), 0.0), 1.0), 2)
    for key, default in (("important", 0.9), ("noise", 0.1)):
        for r in a.get(key) if isinstance(a.get(key), list) else []:
            if not isinstance(r, dict) or not _s(r.get("who"), 80):
                continue
            w = r.get("weight")
            w = default if not isinstance(w, (int, float)) or isinstance(w, bool) else min(max(float(w), 0.0), 1.0)
            row = {"who": _s(r.get("who"), 80), "class": r.get("class") if r.get("class") in
                   ("sender", "client", "agent", "alert", "metric", "market", "topic") else "topic", "weight": round(w, 2)}
            if clean(row["who"]):
                out[key].append(row)
        out[key] = out[key][:20]
    return out


def attention_prior(profile: dict, source: str, who: str | None = None, default: float = 0.5) -> float:
    """W24 entry point: the prior attention for a notification from `source` (and sender / class `who`)."""
    att = (profile or {}).get("attention") or {}
    w = (who or "").lower()
    if w:
        for key in ("important", "noise"):
            for r in att.get(key) or []:
                if r["who"].lower() in w or w in r["who"].lower():
                    return r["weight"]
    return float((att.get("sources") or {}).get(source, default))


# ---- the session ---------------------------------------------------------------------------------------------

class IO:
    """Terminal I/O (a fake in tests)."""

    def __init__(self, inp: Callable[[str], str] = input, out=None, interactive: bool | None = None):
        self.inp, self.out = inp, out or sys.stdout
        self.interactive = interactive if interactive is not None else (inp is not input or sys.stdin.isatty())

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def ask(self, prompt: str) -> str:
        try:
            return self.inp(prompt).strip()
        except EOFError:
            return ""

    def yes(self, prompt: str) -> bool:
        return self.ask(prompt + " [y/N] ").lower() in ("y", "yes", "o", "oui")


def _agents_for(team: bool) -> list[dict]:
    if not team:
        return []
    from . import orchestrator
    return [a for a in orchestrator.roster() if a.get("slug")]


def build(answers: dict, sources: dict, *, team: bool, use_claude: bool, cfg: dict,
          run: Callable = ca.run_claude, apps_universe: set[str] | None = None,
          confirmed: Iterable[tuple[str, str]] = (), raw: dict | None = None,
          by: str = "oi onboard (W22)") -> tuple[dict, dict, dict]:
    """(profile, catalogue, meta). `apps_universe`: the app names an app.open may name (default: the source's).
    `confirmed`: the source names the person agreed to send (confirm_names); every other one stays local.
    `raw`: an answer already produced ({profile, catalogue}: the W22b agent's files) — validated the same way,
    no synthesis call."""
    from .orchestrator import pack
    terms = pack.deny_terms(cfg)
    deny = oi_settings.denylist_apps()
    src = gate_sources(sources, cfg)
    ans, withheld = gate_answers(answers, cfg)
    agents = _agents_for(team)
    apps = set(apps_universe if apps_universe is not None else (src.get("apps") or {}).get("names") or [])
    if not apps:            # no app list: the apps seen in use (an app.open must still be installed to show)
        apps = {a for a, _ in (src.get("activity") or {}).get("top_apps") or []}
    apps = {a for a in apps if _visible_app(a, deny)}
    meta = {"claude": None, "withheld_answer_parts": withheld, "dropped_items": 0, "noise_dropped": 0,
            "names_sent": 0}
    claude_items, prof = [], {}
    if use_claude:
        try:
            to_claude = keep_names(src, confirmed)
            meta["names_sent"] = len(source_names(to_claude))
            raw = raw if raw is not None else synthesise(ans, to_claude, agents, run=run)
            meta["claude"] = "ok"
            for it in raw.get("catalogue") or []:
                v = validate_item(it, apps=apps, agents={a["slug"] for a in agents}, cfg=cfg, terms=terms)
                if v:
                    claude_items.append(v)
                else:
                    meta["dropped_items"] += 1
            prof = validate_profile(raw.get("profile"), cfg, terms)
        except ca.ClaudeUnavailable as e:
            meta["claude"] = f"unavailable:{e}"
            log(f"Claude unavailable ({e}): local catalogue only")
    n = len(claude_items)
    claude_items = drop_noise(claude_items, prof)            # what the person called noise: never proposed
    local = local_catalogue(src, agents=agents, apps_ok=apps)
    n += len(local)
    local = drop_noise(local, prof)
    meta["noise_dropped"] = n - len(claude_items) - len(local)
    items = drop_noise(merge_catalogue(claude_items, local), prof)
    now = vocab.iso_utc(datetime.now(timezone.utc))
    if not prof:
        prof = {"job": _s(ans.get("job")), "tools": [a for a, _ in (src.get("activity") or {}).get("top_apps") or []][:15]}
        prof = {k: v for k, v in prof.items() if v}
    if agents and not prof.get("counterparts"):
        prof["counterparts"] = [{"name": a.get("name") or a["slug"], "kind": "agent", "slug": a["slug"],
                                 "ask_for": (a.get("ask_for") or "")[:160]} for a in agents]
    prof.setdefault("attention", validate_attention(None))
    profile = {"format": FORMAT_PROFILE, "created_at": now, "by": by,
               "sources": {k: _source_count(k, v) for k, v in src.items()}, "team": "roster" if team else None, **prof}
    catalogue = {"format": FORMAT_CATALOGUE, "created_at": now, "items": items}
    meta["items"] = len(items)
    meta["by_source"] = dict(Counter(i["source"] for i in items))
    return profile, catalogue, meta


def _source_count(k: str, v: dict) -> dict:
    if k == "apps":
        return {"apps": v.get("count", 0)}
    if k == "activity":
        return {"from": v.get("from"), "days": v.get("days", 0), "apps": len(v.get("top_apps") or []),
                "hosts": len(v.get("top_hosts") or [])}
    if k == "safari":
        return {"hosts": len(v.get("hosts") or [])}
    if k == "drive":
        return {"folders": len(v.get("folders") or [])}
    if k == "mail":
        return {"accounts": len(v.get("accounts") or [])}
    return {"read": False}


def write(profile: dict, catalogue: dict, out_dir: Path | None = None) -> tuple[Path, Path]:
    d = out_dir or paths.data_dir()
    pp, cp = d / starter.PROFILE, d / starter.CATALOGUE
    head = "# Written by `oi onboard` (W22). On this Mac only; no document content. Re-run to rebuild.\n"
    for p, data in ((pp, profile), (cp, catalogue)):
        if out_dir is None:
            starter._write_private(p, head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120))
        else:
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120))
    return pp, cp


def converse(io: IO, *, preset: dict | None = None, use_claude: bool = True, ask_followups: bool = True,
             run: Callable = ca.run_claude, read: Callable[[str, list[str]], dict] = read_source,
             has_roster: bool | None = None) -> tuple[dict, dict, bool, set[tuple[str, str]]]:
    """The interactive part: (answers, sources, team, confirmed source names). `preset` (answers file) skips what
    it already holds. The source names are listed and confirmed BEFORE the first Claude prompt."""
    preset = preset or {}
    deny = oi_settings.denylist_apps()
    io.say("OS-intent onboarding — about 10-15 minutes. Type, or dictate with the dictation key; Enter skips a question.")
    io.say(PRIVACY)
    io.say()
    chosen = preset.get("sources")
    sources: dict = {}
    for key, title, line, always in SOURCES:
        if chosen is not None and not always and key not in chosen:
            continue
        io.say(f"· {title}: {line}")
        if always or (chosen is not None and key in chosen) or io.yes("  Use it?"):
            sources[key] = read(key, deny)
            why = sources[key].get("why")
            io.say(f"  → {_source_line(key, sources[key])}" + (f" ({why})" if why else ""))
    io.say()
    answers = dict(preset.get("answers") or {})
    for key, q in QUESTIONS:
        if key not in answers:
            answers[key] = io.ask(f"{q}\n> ")
    roster_ok = has_roster if has_roster is not None else bool(_agents_for(True))
    team = preset.get("team")
    if team is None:
        team = roster_ok and io.yes("Do you work with your agent team (the roster: Atlas, Nova, Orion, Lyra, …)?")
    confirmed: set[tuple[str, str]] = set()
    if use_claude:
        cfg = ca.claude_settings()
        io.say()
        confirmed = confirm_names(io, sources, cfg)
    if use_claude and ask_followups and not preset.get("no_followups"):
        try:
            for i, q in enumerate(followups(gate_answers(answers, cfg)[0], keep_names(gate_sources(sources, cfg), confirmed),
                                            _agents_for(bool(team)), run=run)):
                answers[f"followup_{i + 1}"] = io.ask(f"{q}\n> ")
        except ca.ClaudeUnavailable as e:
            io.say(f"(Claude unavailable: {e}; no follow-up questions)")
    return answers, sources, bool(team), confirmed


def _source_line(key: str, v: dict) -> str:
    c = _source_count(key, v)
    return ", ".join(f"{k} {n}" for k, n in c.items()) or "nothing"


def summary_lines(catalogue: dict) -> list[str]:
    out = []
    for it in catalogue["items"]:
        t = it.get("trigger") or {}
        where = ", ".join((t.get("apps") or []) + (t.get("hosts") or [])) or "anywhere"
        hrs = f" {t['hours'][0]}-{t['hours'][1]}h" if t.get("hours") else ""
        out.append(f"  {it['prior']:.2f}  {it['label']:<22} {it['primitive']:<10} when {where}{hrs}  [{it['source']}]")
    return out


def cmd_onboard(a: argparse.Namespace) -> int:
    if not (a.offline or a.no_claude or a.sources_file):      # W22b: the agentic session (oi/onboard_agent)
        from .onboard_agent import agent
        preset = yaml.safe_load(Path(a.answers).read_text(encoding="utf-8")) or {} if a.answers else {}
        return agent.session(IO(), model=a.model, dry_run=a.dry_run, preset=preset,
                             out_dir=Path(a.out).expanduser() if a.out else None)
    cfg = ca.claude_settings()
    use_claude = bool(cfg.get("enabled")) and not a.no_claude
    preset = {}
    if a.answers:
        preset = yaml.safe_load(Path(a.answers).read_text(encoding="utf-8")) or {}
    io = IO()
    if a.sources_file:      # a synthetic person: their source summaries instead of this Mac's
        sources = yaml.safe_load(Path(a.sources_file).read_text(encoding="utf-8")) or {}
        answers, team = dict(preset.get("answers") or {}), bool(preset.get("team"))
        universe = set((sources.get("apps") or {}).get("names") or [])
        confirmed = set(source_names(sources)) if a.send_source_names else set()   # nobody to ask: opt-in flag
    else:
        try:
            answers, sources, team, confirmed = converse(io, preset=preset, use_claude=use_claude,
                                              ask_followups=not a.no_followups)
        except KeyboardInterrupt:
            io.say("\ncancelled: nothing written")
            return 130
        universe = None
    profile, catalogue, meta = build(answers, sources, team=team, use_claude=use_claude, cfg=cfg,
                                     apps_universe=universe, confirmed=confirmed)
    io.say(f"\nStarter catalogue: {meta['items']} actions ({', '.join(f'{k} {n}' for k, n in meta['by_source'].items())});"
           f" Claude: {meta['claude'] or 'off'}; withheld answer parts: {meta['withheld_answer_parts']};"
           f" items dropped by validation: {meta['dropped_items']}, as noise: {meta['noise_dropped']};"
           f" source names sent: {meta['names_sent']}")
    for line in summary_lines(catalogue):
        io.say(line)
    if a.dry_run:
        io.say("(dry run: nothing written)")
        return 0
    pp, cp = write(profile, catalogue, Path(a.out).expanduser() if a.out else None)
    io.say(f"wrote {pp} and {cp} (0600). `oi live` picks the catalogue up at once; re-run `oi onboard` to rebuild.")
    return 0


def register(sub) -> None:
    so = sub.add_parser("onboard", help="W22b: Claude reads your memory, wiki, mail, Notion -> profile.yaml + "
                                       "starter_catalogue.yaml (--offline: the W22 local conversation)")
    so.add_argument("--offline", action="store_true",
                    help="W22 deterministic path: questionnaire + names-only sources (+ one synthesis call unless "
                         "--no-claude); no agent")
    so.add_argument("--model", help="the agent's model (default: settings onboarding.model, else sonnet)")
    so.add_argument("--answers", help="YAML {answers: {job: …}, sources: [memory, wiki, mail, notion, activity, …], "
                                      "team: bool} (skips those questions / opt-ins)")
    so.add_argument("--sources-file", help="(offline) YAML source summaries of a synthetic person (nothing of this "
                                           "Mac is read)")
    so.add_argument("--send-source-names", action="store_true",
                    help="with --sources-file: send its names to Claude (default: answers only, names stay local)")
    so.add_argument("--no-claude", action="store_true", help="(offline) local builder only (no Claude call)")
    so.add_argument("--no-followups", action="store_true", help="(offline) no Claude follow-up questions")
    so.add_argument("--out", help="write the two files here instead of the data dir")
    so.add_argument("--dry-run", action="store_true", help="print the catalogue; write nothing")
    so.set_defaults(func=cmd_onboard)
    sp = sub.add_parser("starter-preview", help="W22: what the starter catalogue proposes in a context (labels only)")
    sp.add_argument("--app")
    sp.add_argument("--host")
    sp.add_argument("--hour", type=int, help="local hour (default now)")
    sp.add_argument("--support", type=int, default=0, help="Markov support of the context (decay)")
    sp.set_defaults(func=cmd_starter_preview)


def cmd_starter_preview(a: argparse.Namespace) -> int:
    now = datetime.now()
    if a.hour is not None:
        now = now.replace(hour=a.hour % 24, minute=0)
    st = starter.Starters(clock=lambda: now.timestamp())
    ctx = {"app": a.app, "url": f"{a.host}/x" if a.host else None}
    menu, used = st.blend([], ctx, a.support)
    for o in sorted(menu, key=lambda o: -(o.get("p_show") or 0)):
        print(f"{o['p_show']:.3f}  {o.get('label') or o['token']}  ({o['primitive']})")
    if not used:
        print("(no starter item fits this context)")
    return 0
