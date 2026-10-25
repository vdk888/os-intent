"""W15: hold ⌥⌘ + glide + dwell = a Claude-enriched sub-ring marking menu (the driver side).

The user: "when i hold option + cmd ... i can move the mouse pointer to an option,
and if i stay it triggers context enrichment with claude, so i can chain clicks in relevant context, low distance".

oi-hud owns the gesture (hold ≥ 250 ms, hover, dwell `hud.dwell_ms`, sub-ring layout, release). It talks to the
show's owner (`oi live`, on the show's own connection):
  {"event":"glide","phase":"start"}   -> `GlideSession.prefetch()`: ONE batched haiku call that returns the
                                         follow-ups of every visible option, cached per context key;
  {"event":"expand","path":[2,1]}     -> `GlideSession.expand()`: from the cache (or waits for the running prefetch),
                                         else one haiku call (8 s timeout) for that option; answered with the socket
                                         command {"cmd":"subring","path":[2,1],"options":[{key,label,icon,action}]}
                                         (0 options = a leaf: Claude off or unavailable, nothing that maps);
  {"event":"pressed","key":2,"path":[2,1],"via":"glide","glide":{...}}  -> `GlideSession.execute()`.

Context is read at REQUEST time (the W14 note): the Mail selection when the prefetch or the expand happens (never
an earlier one), and a mail action runs only on the mail it was built for (message id checked by AppleScript).

Follow-ups map to what the driver can execute: the catalogue primitives (app.open of an installed app, url.open of
a site seen today, space.move, mail.reply / mail.forward), the W14 Claude actions (draft:mail.reply /
draft:mail.forward, summarise) and a message to a fleet agent (vocal:<slug>:<text>, the W12 path). Anything else
becomes "Ask Atlas: <text>" (the W12 path to atlas: a local inject). Nothing runs on hover: only on a release or a
click.

W23 drill-down: each level acts INSIDE / ON its parent (oi/drill.py): the parent's scope picks local
providers (Finder files / folders, Telegram chats, Music playlists, a site's pages, the mail's reply / forward),
ranked by history (Markov on the parent token + the logged glide choices); Claude (haiku) only orders / labels /
fills the generative scopes (a fleet agent, the mail) from those items + the scope's forms, with the path chain;
the scope rejector drops any other app / site switch (except "back"). A send ("Ask Atlas" included) runs on release
and waits for oi.gate's confirm panel, the ONE confirmation: no "Send ✓" child any more.
Cache per (parent chain ids, context); each chosen branch is logged as a training pair (ids only).

Runtime: Claude only through the subscription runner of oi.claude_actions (headless `claude -p`, no API key ever).
Privacy: settings.claude.local_only_domains applied to the mail's sender, cc and reply-to (no mail detail in the
prompt) and to the page host. Logged (hud-log `glide`): visited paths and dwell times, the final path, the action
id, the expansions' latency / cache hit — never a label, a message or a prompt.

Context cards: the same hold also shows WHAT a direction is about. At hold start the
session sends {"cmd":"context","cards":[{key, what, summary?, excerpt?, why?, pending?}]}: `what` = one local line
(the mail's sender + subject, a Telegram agent's last line, a file's name + folder, a page's title), shown by the
HUD as a small card at level 2; `summary` (3-4 lines: what is asked, any deadline) + `excerpt` (verbatim from the
mail) come from the SAME batched prefetch call and are re-sent when ready (level 3, a bigger card); `why` = the wiki
page + snippet the proactive judge (oi/proactive/judge.py) found. A local-only / private mail never reaches the model
(no summary) and a private event gets no `why`: such cards stay at level 2.

App directions list their live items: when a direction is an APP (Telegram, Mail,
Calendar...), its next level opens with the LIVE events of that app's source (oi/proactive store, read only: the agents
/ chats waiting « Atlas · 8 », the important unread mails, the next meeting), in judge order (important first, then the
newest), each a one-click action (the chat by its bot's tg://resolve link, that mail, the meeting's link) carrying its
own level-2/3 card (`event_card`, local: no model). Claude's follow-ups and the local providers stay below them; an app
with no live item (Safari today) behaves as before.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from . import claude_actions as ca, drill
from .orchestrator import dispatch

EXPAND_TIMEOUT_S = 8
CACHE_SIZE = 64
MAX_FOLLOWUPS = 4
MAX_DEPTH = 3                 # sub-ring levels (the HUD enforces it too)
LABEL_MAX = 40                # the HUD fits it in its capsule (22 shown)
TEXT_MAX = 300                # a message / an ask
MAIL_BODY_MAX = 300
LIST_MAX = 10                 # apps / hosts offered to the model
MODEL = "sonnet"   # (2026-10-20): "passe à Sonnet"
PARIS = ZoneInfo("Europe/Paris")
MAIL_ACTIONS = ("- mail.reply or mail.forward  open an empty reply / forward of the selected mail\n"
                "- draft:mail.reply or draft:mail.forward  Claude drafts that reply / forward in Mail (never sent)\n")
ICONS = {"url.open": "sf:safari", "mail.reply": "sf:arrowshape.turn.up.left",
         "mail.forward": "sf:arrowshape.turn.up.right", "draft": "sf:sparkles", "summarise": "sf:text.alignleft",
         "space+": "sf:arrow.right.square", "space-": "sf:arrow.left.square", "ask": "initial:R",
         "back": "sf:arrow.uturn.backward", "dispatch": "sf:arrow.triangle.branch"}
DISPATCH_FORM = "dispatch:<agent> or dispatch:<agent>:<message>"   # W25: hand over to the roster agent
_SPACE = re.compile(r"^(?:[+-]1|=\d{1,2})$")
# context cards
WHAT_MAX = 90
SUMMARY_LINES = 4
SUMMARY_LINE_MAX = 90
EXCERPT_MAX = 160
SNIPPET_MAX = 200
MATERIAL_MAX = 1500           # the mail text a summary may read
MAIL_OPTS = ca.MAIL_PRIMITIVES + ("mail.show",)
# an app direction -> the event sources whose live items its next level lists (a source without a
# connector yet, like calendar, simply has no live item)
APP_SOURCES = {"Telegram": ("agent",), "Mail": ("mail",), "Calendar": ("calendar",)}


def dotted(path) -> str:
    return ".".join(str(int(k)) for k in path)


def _text(x, n: int) -> str | None:
    if not isinstance(x, str):
        return None
    t = " ".join(x.replace("—", "-").split())[:n]
    return t or None


def _node(label, icon, action, kind, primitive=None, params=None, token=None, claude=False) -> dict:
    return {"label": label, "icon": icon, "action": action, "kind": kind, "primitive": primitive,
            "params": params or {}, "token": token, "claude": claude}


def describe(opt: dict) -> str:
    """A top option in words (for the prompt): what it does, no user text beyond app / host."""
    prim, params, tok = opt.get("primitive"), opt.get("params") or {}, opt.get("token") or ""
    if prim == "app.open":
        return f"switch to {params.get('app', '?')}"
    if prim == "url.open":
        return f"open {tok.split(':', 1)[-1].split('/', 1)[0] or params.get('url', '?')}"
    if prim == "space.move":
        return f"move to desk {tok.split(':', 1)[-1]}"
    if prim == "mail.reply":
        return "reply to the selected mail"
    if prim == "mail.forward":
        return "forward the selected mail"
    if prim in ("folder.open", "file.open", "telegram.chat", "music.play"):
        return {"folder.open": "open folder ", "file.open": "open file ", "telegram.chat": "open the Telegram chat ",
                "music.play": "play "}[prim] + (opt.get("label") or "?")
    return opt.get("label") or prim or "?"


# ---- follow-up -> executable node -----------------------------------------------------------------------

def resolve(fu, parent: dict, env: dict, strict: bool = False) -> dict | None:
    """A model follow-up {"label", "action"} -> an executable node, or "Ask Atlas: <text>" when the action is
    unknown, not allowed here (an app not installed, a site not seen today) or malformed. None = nothing usable.
    Node: {label, icon, action (the id logged: "app.open", "draft:mail.reply", "vocal:nova", "ask"...), kind
    ("primitive" | "draft" | "summary" | "vocal" | "ask"), primitive, params, token, claude}.
    W23 `strict`: no "Ask Atlas" fallback (an unknown / not-allowed action is dropped; only an explicit ask: stays);
    "back" = switch back to the app in front at the show (env["front_app"])."""
    if not isinstance(fu, dict):
        return None
    label = _text(fu.get("label"), LABEL_MAX)
    action = (fu.get("action") if isinstance(fu.get("action"), str) else "").strip()
    kind, _, arg = action.partition(":")
    arg = arg.strip()
    if not label and not action:
        return None
    label = label or _text(action, LABEL_MAX)
    if action == "back":
        app = env.get("front_app")
        path = env["resolve_app"](app) if isinstance(app, str) and app else None
        if not path:
            return None
        return _node(_text(f"Back to {app}", LABEL_MAX), ICONS["back"], "back", "primitive", "app.open", {"app": app},
                     f"activate:{app}")
    if kind == "app.open" and arg:
        app = next((a for a in env.get("apps", []) if a.lower() == arg.lower()), arg)
        path = env["resolve_app"](app)
        if path:
            return _node(label, f"app:{path}", "app.open", "primitive", "app.open", {"app": app}, f"activate:{app}")
    elif kind == "url.open" and arg:
        want = arg.lower().split("://", 1)[-1].strip("/")
        host = want.split("/", 1)[0].removeprefix("www.")
        urls = env.get("urls", {})
        slug = want if want in urls.values() else urls.get(host)    # only sites seen today, never an invented one
        if slug:
            return _node(label, ICONS["url.open"], "url.open", "primitive", "url.open", {"url": slug}, f"url:{slug}")
    elif kind == "space.move" and _SPACE.match(arg):
        return _node(label, ICONS["space-" if arg.startswith("-") else "space+"], "space.move", "primitive",
                     "space.move", {}, f"space:{arg}")
    elif action in ca.MAIL_PRIMITIVES and env.get("mail"):
        return _node(label, ICONS[action], action, "primitive", action)
    elif kind == "draft" and arg in ca.MAIL_PRIMITIVES and env.get("mail") and env.get("claude_ok"):
        return _node(label, ICONS["draft"], f"draft:{arg}", "draft", arg, claude=True)
    elif action == "summarise" and parent.get("primitive") in ("url.open", "app.open") and env.get("claude_ok"):
        return _node(label, ICONS["summarise"], "summarise", "summary", parent["primitive"],
                     dict(parent.get("params") or {}), parent.get("token"), claude=True)
    elif kind == "dispatch":           # W25: "pass me to X" (arm) / hand this over to X (a confirmed send)
        slug, _, msg = arg.partition(":")
        slug, msg = slug.strip().lower(), _text(msg, TEXT_MAX)
        name = env.get("agents", {}).get(slug)
        if name and msg:
            return _node(label, ICONS["dispatch"], f"dispatch:{slug}", "vocal",
                         params={"to": slug, "text": msg, "dispatch": True})
        if name:
            return _node(label, ICONS["dispatch"], f"dispatch:{slug}", "dispatch", params={"to": slug})
    elif kind == "vocal":
        slug, _, msg = arg.partition(":")
        slug, msg = slug.strip().lower(), _text(msg, TEXT_MAX)
        name = env.get("agents", {}).get(slug)
        if name and msg and slug != "atlas":
            return _node(label, f"initial:{name[:1].upper()}", f"vocal:{slug}", "vocal", params={"to": slug, "text": msg})
        if slug == "atlas" and msg:
            arg, kind = msg, "ask"
    if strict and kind != "ask":
        return None
    text = _text(arg if kind == "ask" and arg else (label or action), TEXT_MAX)
    if not text or "atlas" not in env.get("agents", {}):
        return None
    shown = label or text
    shown = shown if shown.lower().startswith("ask atlas") else f"Ask Atlas: {shown}"
    return _node(_text(shown, LABEL_MAX), ICONS["ask"], "ask", "ask", params={"to": "atlas", "text": text})


def resolve_list(raw, parent: dict, env: dict, *, scope: dict | None = None, items: dict | None = None) -> list[dict]:
    """2-4 nodes (duplicates and unusable entries dropped; more than 4 cut). W23 with a `scope`: an action may name
    one of `items` (id -> provider node; the model may relabel it), anything else is resolved strictly and must pass
    the scope rejector (drill.in_scope)."""
    out, seen = [], set()
    for fu in raw if isinstance(raw, list) else []:
        if scope is not None:
            n = scoped(fu, parent, env, scope, items or {})
        else:
            n = resolve(fu, parent, env)
        if not n:
            continue
        sig = (n["action"], n["label"].lower())
        if sig in seen:
            continue
        seen.add(sig)
        out.append(n)
        if len(out) == MAX_FOLLOWUPS:
            break
    return out


def scoped(fu, parent: dict, env: dict, scope: dict, items: dict) -> dict | None:
    """One model follow-up under a scoped parent: an allowed item, or an in-scope form; None otherwise."""
    if not isinstance(fu, dict):
        return None
    action = (fu.get("action") if isinstance(fu.get("action"), str) else "").strip()
    if action in items:
        n = dict(items[action])
        n["label"] = _text(fu.get("label"), LABEL_MAX) or n["label"]
        return n
    n = resolve(fu, parent, env, strict=True)
    if n and n["action"].startswith("dispatch:") and scope["kind"] not in ("leaf", "space"):
        return finish(n)                                      # W25: a hand-over is in scope anywhere (like "back")
    if not n or not drill.in_scope(n, scope):
        return None
    if n["action"] == "back" and scope.get("app") == env.get("front_app"):
        return None                                           # "back" to where we already are: nothing
    return finish(n)


def finish(n: dict) -> dict:
    """W23: the node's id (a send needs no confirm child: the outward gate's panel confirms it)."""
    n.setdefault("id", drill.node_id(n))
    return n


# ---- context cards ------------------------------------------------------------------------

def _join(*parts) -> str | None:
    return _text(" · ".join(p.strip() for p in parts if isinstance(p, str) and p.strip()), WHAT_MAX)


def _place(path: str) -> str | None:
    """A file / folder as "name · parent folder"."""
    p = path.rstrip("/")
    return _join(os.path.basename(p), os.path.basename(os.path.dirname(p)))


def _hhmm(ts) -> str | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(PARIS).strftime("%H:%M")
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(PARIS)


def _since(ts, now: datetime | None = None) -> str | None:
    """"14:05" today (Paris), "le 19/10" before: since when a decision / PR has waited."""
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(PARIS)
    except ValueError:
        return None
    today = (now or _now()).astimezone(PARIS).date()
    return t.strftime("%H:%M") if t.date() == today else t.strftime("le %d/%m")


def _num(x) -> str:
    return f"{x:g}" if isinstance(x, (int, float)) and not isinstance(x, bool) else "?"


def sujet_line(ref: dict) -> str | None:
    """« Sujet : <page title> » of a cockpit decision (its board sujet; « (déduit) » when the cockpit inferred it)."""
    s = ref.get("sujet")
    if not isinstance(s, dict) or not isinstance(s.get("label"), str) or not s["label"].strip():
        return None
    return f"Sujet : {s['label'].strip()}" + (" (déduit)" if s.get("inferred") else "")


def local_summary(ev: dict) -> list[str] | None:
    """Level 3 of the sources whose content is already local (contract.LEVEL3 "local"): a few lines from the event's
    own data, no model. None for a private event or a source without a local provider."""
    src, ref, cls = ev.get("source"), ev.get("ref") or {}, ev.get("class")
    if ev.get("private"):
        return None
    since = _hhmm(ev.get("ts"))
    if src == "metric":
        lines = [f"{ev.get('who') or cls}: {_num(ref.get('value'))} (seuil {_num(ref.get('threshold'))})",
                 f"Sous le seuil depuis {since}" if since else None,
                 f"Responsable : {ref['owner']}" if isinstance(ref.get("owner"), str) else None]
    elif src == "market" and cls == "move":
        pct = ref.get("pct")
        lines = [f"{ev.get('who') or '?'} {pct:+.1f} %" if isinstance(pct, (int, float)) else ev.get("subject"),
                 f"Au-delà de ±{_num(ref.get('move_pct'))} % depuis {since}" if since else None]
    elif src == "market":
        lines = [f"Brief de Orion du {ref['date']}, non lu" if isinstance(ref.get("date"), str) else "Brief de Orion, non lu"]
    elif src == "cockpit" and str(cls).startswith("decide-"):   # « À décider »: the cockpit's own description lines
        risk = {"low": "faible", "medium": "moyen", "moderate": "moyen", "high": "élevé", "critical": "critique"}
        head = _join(f"risque {risk.get(ref.get('risk'), ref.get('risk'))}" if ref.get("risk") else None,
                     {"APPROVE": "revue validée", "REQUEST_CHANGES": "revue : à corriger"}.get(ref.get("verdict")),
                     f"en attente depuis {_since(ref.get('since'))}" if _since(ref.get("since")) else None)
        lines = [sujet_line(ref), *(x for x in ref.get("detail") or [] if isinstance(x, str)), head]
    elif src == "calendar":
        who = ref.get("attendees") or []
        lines = [f"Avec : {', '.join(who[:6])}" if who else None,
                 _join(f"{_hhmm(ref.get('start'))}–{_hhmm(ref.get('end'))}" if ref.get("start") else None, ref.get("calendar")),
                 f"Lien : {ref['join'].split('://', 1)[-1].split('/', 1)[0]}" if isinstance(ref.get("join"), str) else None]
    elif src == "github":
        verdict = {"APPROVE": "Revue indépendante : validée", "REQUEST_CHANGES": "Revue indépendante : à corriger",
                   "COMMENT": "Revue indépendante : commentaires"}.get(ref.get("verdict"), "Pas encore de revue indépendante")
        lines = [ref.get("summary"), verdict, _join(ref.get("repo"), f"ouverte {_since(ref.get('since'))}" if _since(ref.get("since")) else None)]
    elif src == "cockpit" and cls == "outage":
        lines = ["La sonde de santé du cockpit échoue (2 fois de suite)", f"Injoignable depuis {since}" if since else None]
    elif src == "cockpit":
        lines = [ev.get("subject"), f"Alerte {ref.get('kind') or cls} ouverte depuis {since}" if since else None]
    else:
        return None
    out = [t for t in (_text(x, SUMMARY_LINE_MAX) for x in lines) if t][:SUMMARY_LINES]
    return out or None


def event_card(ev: dict) -> dict:
    """A proactive event's card fields (local): `what` in one line (level 2: who · what · since when), `why` = the
    judge's wiki page + snippet (never for a private event), `private`, `summary` = the local level 3 of the sources
    that have one (local_summary). Set by the proactive engine as the glide env's `event_card`."""
    src, ref, priv = ev.get("source"), ev.get("ref") or {}, bool(ev.get("private"))
    who, subject = ev.get("who") or "", ev.get("subject") or ""
    since = _hhmm(ev["ts"]) if ev.get("ts") else None
    if src == "file" and isinstance(ref.get("path"), str):
        what = _join(_place(ref["path"]), f"depuis {since}" if since else None)
    elif src == "agent" and isinstance(ref.get("n"), int) and ref["n"] > 0:   # « Atlas t'attend · 2 messages · depuis 14:05 »
        since = _hhmm(ref.get("since"))
        n = ref["n"]
        what = _join(f"{who or ref.get('chat') or ''} t'attend", f"{n} message{'s' if n > 1 else ''}",
                     f"depuis {since}" if since else None)
    elif src == "calendar":                  # « Point Acme · à 15:30 · Sam, Anne » / « … · jusqu'à 16:30 »
        start, end = ref.get("start"), ref.get("end")
        try:
            started = datetime.fromisoformat(str(start).replace("Z", "+00:00")) <= _now()
        except ValueError:
            started = False
        what = _join(subject, (f"jusqu'à {_hhmm(end)}" if started else f"à {_hhmm(start)}") if start and end else None, who)
    elif src in ("cockpit", "github") and ref.get("since") and (src == "github" or str(ev.get("class")).startswith("decide-")):
        what = _join(who, subject, f"depuis {_since(ref['since'])}" if _since(ref["since"]) else None,
                     None if priv else sujet_line(ref))
    elif src == "agent":                     # the agent's last line when the sensor read one, else its chat
        what = _text(f"{who}: {subject}", WHAT_MAX) if subject else _join(who, ref.get("chat"))
    else:
        what = _join(who, subject, f"depuis {since}" if since else None)
    w = ev.get("wiki") or {}
    why = None
    if not priv and isinstance(w.get("page"), str) and w["page"]:
        why = {"page": w["page"][:120], **({"snippet": _text(w.get("snippet"), SNIPPET_MAX)} if w.get("snippet") else {})}
    card = {"what": what, "why": why, "private": priv}
    summary = local_summary(ev)
    if summary:
        card["summary"] = summary
    if src == "agent" and isinstance(ref.get("slug"), str):   # local only: which agent's messages a level 3 may read
        card.update(agent=ref["slug"], since=ref.get("since"))
    return card


def what_line(opt: dict, env: dict, ctx: dict) -> str | None:
    """Level 2: what a direction is about, in one line, from local data only (no model)."""
    ev = env.get("event_card") or {}
    if opt.get("evt") and ev.get("what"):
        return ev["what"]
    prim, params = opt.get("primitive"), opt.get("params") or {}
    mail = env.get("mail")
    if prim in MAIL_OPTS and mail:
        return _join(mail.get("sender_name") or mail.get("sender_address"), mail.get("subject"))
    if prim in ("file.open", "folder.open") and isinstance(params.get("path"), str):
        return _place(params["path"])
    if prim == "telegram.chat":
        return _join(opt.get("label"), params.get("chat") if isinstance(params.get("chat"), str) else None)
    if prim == "url.open":
        slug = params.get("url") or params.get("href") or (opt.get("token") or "").split(":", 1)[-1]
        slug = slug.split("://", 1)[-1] if isinstance(slug, str) else ""
        here = isinstance(ctx.get("url"), str) and ctx["url"].split("/", 1)[0] == slug.split("/", 1)[0]
        title = ctx.get("title") if here and isinstance(ctx.get("title"), str) else None
        return _join(title, slug.split("/", 1)[0]) if title else _text(slug, WHAT_MAX)
    if prim == "app.open":
        return _text(params.get("app"), WHAT_MAX)
    return None


def _norm(t: str) -> str:
    return " ".join(t.replace("’", "'").split()).lower()


def parse_card(out, material: str) -> dict | None:
    """The batched answer's "context" -> {summary: ≤ 4 lines, excerpt?}; the excerpt is kept only when it is
    really in the mail (verbatim, whitespace / case aside)."""
    c = (out or {}).get("context") if isinstance(out, dict) else None
    if not isinstance(c, dict):
        return None
    lines = c.get("summary")
    lines = [lines] if isinstance(lines, str) else lines if isinstance(lines, list) else []
    summary = [t for t in (_text(x, SUMMARY_LINE_MAX) for x in lines) if t][:SUMMARY_LINES]
    ex = c.get("excerpt") if isinstance(c.get("excerpt"), str) else ""
    ex = ex.strip().strip('"«»“”').strip()
    excerpt = _text(ex, EXCERPT_MAX) if ex and _norm(ex) in _norm(material) else None
    if not summary and not excerpt:
        return None
    return {"summary": summary, **({"excerpt": excerpt} if excerpt else {})}


def agent_messages(slug: str) -> str:
    """The pending Telegram messages of a Mac agent to the user (oi.proactive.sensors.OutboundLog), oldest first,
    separated by blank lines; "" when none / no record (a VPS agent). Read only for a level-3 summary."""
    from .proactive import config, engine as pe, sensors
    a = config.proactive_settings(pe._raw_settings())["agents"]
    spec = (a.get("outbound") or {}).get(slug)
    if not spec:
        return ""
    return "\n\n".join(sensors.OutboundLog().texts(spec, {str(x) for x in a.get("chat_ids") or []}))


# ---- an app direction's live items ---------------------------------------------------------

def judge_order(evs: list[dict]) -> list[dict]:
    """The judge's order: the events it found important (a wiki verdict) first, then the newest."""
    newest = sorted(evs, key=lambda e: str(e.get("ts") or ""), reverse=True)
    return sorted(newest, key=lambda e: not e.get("wiki"))


def item_card(ev: dict) -> dict | None:
    """An item's own card (level 2 what, level 3 local summary + why): `event_card` minus its private / agent keys."""
    c = event_card(ev)
    if not c.get("what"):
        return None
    return {k: c[k] for k in ("what", "summary", "why") if c.get(k)}


def live_item(ev: dict, env: dict) -> dict | None:
    """One live event -> a one-click node (None when it carries nothing to act on)."""
    src, ref, who = ev.get("source"), ev.get("ref") or {}, ev.get("who") or ""
    if src == "agent" and isinstance(ref.get("slug"), str):
        slug = ref["slug"]
        name = env.get("agents", {}).get(slug) or who or slug.title()
        bot = ref.get("bot") if drill.bot_name(ref.get("bot")) else (env.get("bots") or {}).get(slug)
        chat = ref.get("chat") if isinstance(ref.get("chat"), str) and ref["chat"] else None
        if not (drill.bot_name(bot) or chat):
            return None
        n = ref.get("n") if isinstance(ref.get("n"), int) and ref["n"] > 0 else None
        params = {"chat": chat or name, **({"bot": bot} if drill.bot_name(bot) else {})}
        node = drill._node(f"{name} · {n}" if n else name, f"initial:{name[:1].upper()}", "telegram.chat",
                           "telegram.chat", params)
    elif src == "mail" and isinstance(ref.get("message_id"), str) and ref["message_id"]:
        label = _join(who, None if ev.get("private") else ev.get("subject")) or "Mail"
        node = drill._node(label, "sf:envelope.open", "mail.show", "mail.show", {"message_id": ref["message_id"]},
                           "mail.show")
        node["id"] = f"mail.show#{drill._h(ref['message_id'])}"     # two mails are two nodes (an id, never the subject)
    elif isinstance(ref.get("url"), str) and ref["url"]:             # e.g. a meeting: open / join its link
        node = drill._node(_join(who, None if ev.get("private") else ev.get("subject")) or src or "?", "sf:link",
                           "url.open", "url.open", {"href": ref["url"]}, f"url.open:{src}")
        node["id"] = f"url.open#{drill._h(ref['url'])}"
    else:
        return None
    node["live"] = {"source": src, "message_id": ref.get("message_id")} if src == "mail" else {"source": src}
    card = item_card(ev)
    if card:
        node["card"] = card
    return node


def live_items(app: str | None, env: dict) -> list[dict]:
    """The live items of an app direction (its sources' live events, judge order), MAX_FOLLOWUPS at most."""
    sources = APP_SOURCES.get(app or "")
    if not sources:
        return []
    evs = [e for e in env.get("live_events") or [] if isinstance(e, dict) and e.get("source") in sources
           and e.get("state", "live") == "live"]
    out, seen = [], set()
    for ev in judge_order(evs):
        n = live_item(ev, env)
        if n and n["id"] not in seen:
            seen.add(n["id"])
            out.append(n)
        if len(out) == MAX_FOLLOWUPS:
            break
    return out


def live_children(parent: dict) -> list[dict]:
    """Under a live mail item: reply to THAT mail (an empty reply window, never sent)."""
    mid = ((parent.get("live") or {}).get("message_id")) if parent.get("primitive") == "mail.show" else None
    if not mid:
        return []
    n = drill._node("Reply", ICONS["mail.reply"], "mail.reply", "mail.reply", {"message_id": mid})
    n["id"] = f"mail.reply#{drill._h(mid)}"
    return [n]


# ---- the prompt -------------------------------------------------------------------------------------------

def context_text(ctx: dict, env: dict, now: float | None = None) -> str:
    lines = []
    if ctx.get("app"):
        lines.append(f"Front app: {ctx['app']}")
    if env.get("host"):
        lines.append(f"Page: {env['host']}")
    if isinstance(ctx.get("space"), int):
        lines.append(f"Desk: {ctx['space']}")
    lines.append("Time (Paris): " + datetime.fromtimestamp(time.time() if now is None else now, PARIS).strftime("%a %H:%M"))
    mail = env.get("mail")
    if mail and env.get("mail_gated"):
        lines.append("A mail is selected in Mail (details withheld: local-only domain).")
    elif mail:
        cc = ", ".join((mail.get("cc") or [])[:4])
        lines.append(f"Selected mail: from {mail.get('sender_name') or '?'} <{mail.get('sender_address') or '?'}>"
                     + (f", cc {cc}" if cc else "") + f", subject: {(mail.get('subject') or '')[:120]}")
        body = _text(mail.get("body"), MAIL_BODY_MAX)
        if body:
            lines.append(f"Mail start: {body}")
    return "\n".join(lines)


ITEM_KINDS = {"folder.open": "folder", "file.open": "file", "telegram.chat": "chat", "music.play": "music",
              "url.open": "page", "mail.reply": "mail", "mail.forward": "mail"}


def option_block(k: int, chain: str, items: list[dict], scope: dict, agents: dict) -> tuple[str, dict]:
    """W23: one option of the prompt: its path, its allowed items (ids i1..), its forms -> (text, id -> item)."""
    ids = {f"i{i + 1}": n for i, n in enumerate(items)}
    listed = "; ".join(f'{i} = {ITEM_KINDS.get(n.get("primitive"), "item")} "{n["label"]}"' for i, n in ids.items())
    fs = drill.forms(scope)
    if scope["kind"] not in ("leaf", "space"):
        fs = fs + [DISPATCH_FORM]                             # W25: the agent from the roster by topic
    if "vocal:<agent>:<message>" in fs:
        names = ", ".join(f"{s} ({n})" for s, n in agents.items() if s != "atlas") or "(none)"
        fs = [f if f != "vocal:<agent>:<message>" else f"vocal:<agent>:<message> (agents: {names})" for f in fs]
    return "\n".join([f"{k}. Path: {chain}", f"   Items: {listed or '(none)'}",
                      f"   Forms: {', '.join(fs) or '(none)'}"]), ids


def build_prompt(ctx: dict, env: dict, options: list[tuple[int, str, list[dict], dict]], now: float | None = None,
                 wiki: list[str] | None = None, material: str | None = None) -> tuple[str, dict[int, dict]]:
    """W23: options = [(number, path chain, allowed items, scope)] -> (prompt, number -> {item id -> item}).
    `material` = the mail text to summarise for the level-3 context card (None = no card asked)."""
    blocks, ids = [], {}
    for k, chain, items, scope in options:
        text, ids[k] = option_block(k, chain, items, scope, env.get("agents", {}))
        blocks.append(text)
    # W25: the orchestrator's context pack for this moment (gated host / mail, the path chain, the agent)
    agent = next((sc.get("slug") for _, _, _, sc in options if sc.get("kind") == "agent"), None)
    context = {"app": ctx.get("app"), "host": env.get("host"), "space": ctx.get("space"),
               "mail": None if env.get("mail_gated") else env.get("mail"), "mail_gated": env.get("mail_gated"),
               "chain": " | ".join(c for _, c, _, _ in options)[:300], "agent": agent,
               "events": env.get("events"), "event_habit": env.get("event_habit")}   # W24: proactive context
    return ca.render_prompt("glide", {
        "max": str(MAX_FOLLOWUPS),
        "front_app": env.get("front_app") or "(unknown)",
        "context": context_text(ctx, env, now),
        "wiki": "\n".join(f"- {w}" for w in wiki or []) or "(none)",
        "options": "\n".join(blocks) or "(none)",
        "material": material or "(none)",
    }, context=context), ids


# ---- the engine (shared cache) and one session per show ---------------------------------------------------

class GlideEngine:
    """The haiku calls, the providers and the cache shared by every show: key "<context key>|<chain ids>" -> nodes."""

    def __init__(self, sender: Callable[[dict], dict], *, runner: Callable[..., dict] | None = None,
                 mail_reader: Callable[[], dict | None] | None = None,
                 settings_loader: Callable[[], dict] = ca.claude_settings,
                 clock: Callable[[], float] = time.monotonic, timeout: float = EXPAND_TIMEOUT_S,
                 providers: "drill.Providers | None" = None, wiki: Callable[[str], list[str]] = ca.wiki_snippets,
                 pairs_reader: Callable[[], list[dict]] = drill.read_pairs,
                 pair_logger: Callable[..., dict] = drill.log_pair,
                 body_reader: Callable[[str], str] | None = None,
                 agent_reader: Callable[[str], str] | None = None):
        self.sender = sender
        self.runner = runner or (lambda prompt, model: ca.run_claude(prompt, model, timeout=timeout))
        self.mail_reader = mail_reader or (lambda: ca.read_mail_context(previous=False, body_max=MATERIAL_MAX))
        # a proactive mail's body (its event carries none), read by message id for the level-3 summary
        self.body_reader = body_reader or (lambda mid: ca.read_mail_body(mid, body_max=MATERIAL_MAX))
        # an agent's pending Telegram messages (its own local record), read at hold for the level-3 summary
        self.agent_reader = agent_reader or agent_messages
        self.settings_loader = settings_loader
        self.clock = clock
        self.timeout = timeout
        self.providers = providers or drill.Providers()
        self.wiki, self.pairs_reader, self.pair_logger = wiki, pairs_reader, pair_logger
        self._pairs: tuple[float, list[dict]] | None = None
        self.cache: OrderedDict[str, list[dict]] = OrderedDict()
        self.inflight: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.calls = 0

    def get(self, key: str) -> list[dict] | None:
        with self.lock:
            v = self.cache.get(key)
            if v is not None:
                self.cache.move_to_end(key)
            return v

    def put(self, key: str, nodes: list[dict]) -> None:
        with self.lock:
            self.cache[key] = nodes
            while len(self.cache) > CACHE_SIZE:
                self.cache.popitem(last=False)

    def begin(self, key: str) -> bool:
        """True = the caller makes the call (and must `done(key)`); False = one is running already."""
        with self.lock:
            if key in self.inflight:
                return False
            self.inflight[key] = threading.Event()
            return True

    def done(self, key: str) -> None:
        with self.lock:
            ev = self.inflight.pop(key, None)
        if ev:
            ev.set()

    def wait(self, key: str, timeout: float) -> None:
        with self.lock:
            ev = self.inflight.get(key)
        if ev:
            ev.wait(timeout)

    def pairs(self) -> list[dict]:
        """The logged glide choices (training pairs), re-read at most once a minute."""
        now = self.clock()
        if self._pairs is None or now - self._pairs[0] > 60:
            try:
                rows = self.pairs_reader()
            except Exception:
                rows = []
            self._pairs = (now, rows)
        return self._pairs[1]

    def call(self, prompt: str) -> tuple[dict | None, str, int]:
        """One haiku call: (answer, code, latency ms). Logged: model, latency, code (never the prompt)."""
        t0 = self.clock()
        self.calls += 1
        try:
            out, code = self.runner(prompt, MODEL), "ok"
        except ca.ClaudeUnavailable as e:
            out, code = None, e.code
        ms = int((self.clock() - t0) * 1000)
        ca.log(f"glide {MODEL} {ms} ms {code}")
        return out, code, ms

    def session(self, keymap: dict[int, dict], ctx: dict, env: dict) -> "GlideSession":
        return GlideSession(self, keymap, ctx, env)


class GlideSession:
    """One show's glide: the visible options (`keymap`), the moment's context, the follow-ups served so far."""

    def __init__(self, engine: GlideEngine, keymap: dict[int, dict], ctx: dict, env: dict):
        self.engine, self.keymap, self.ctx, self.env_base = engine, keymap, ctx, env
        self.children: dict[tuple, list[dict]] = {}
        self.mail_id: str | None = None
        self.mail_fixed: dict | None = None             # W24: set by oi live for a proactive mail hint
        self._fixed_body: str | None = None              # that mail's body, read once
        self._agent_text: str | None = None              # a proactive agent event's pending messages, read once
        self.expansions: list[dict] = []
        self.lock = threading.Lock()

    def wants_mail(self) -> bool:
        return self.ctx.get("app") == "Mail" or any(o.get("primitive") in ca.MAIL_PRIMITIVES for o in self.keymap.values())

    def read_context(self) -> tuple[str, dict]:
        """At REQUEST time: settings, the Mail selection NOW (when relevant), the privacy gates -> (key, env).
        W23: the key is the context only (front app, host, desk, mail, gate); the chain ids complete it per level."""
        cfg = self.engine.settings_loader()
        mail = self.mail_fixed                          # W24: a proactive mail hint = the event's mail
        if mail is None and cfg.get("enabled") and self.wants_mail():
            try:
                mail = self.engine.mail_reader()
            except Exception:
                mail = None
        host = self.env_base.get("host")
        gated = ca.mail_local_only(mail, cfg) or bool((mail or {}).get("_private"))   # W24: the event's own gate
        if mail is self.mail_fixed and mail and not gated and cfg.get("enabled") and not mail.get("body"):
            if self._fixed_body is None:                # read once per session, never for a gated mail
                try:
                    self._fixed_body = self.engine.body_reader(mail.get("message_id") or "") or ""
                except Exception:
                    self._fixed_body = ""
            mail = dict(mail, body=self._fixed_body)
        env = dict(self.env_base, mail=mail, mail_gated=gated, claude_ok=bool(cfg.get("enabled")),
                   host=None if (host and ca.local_only(host, cfg)) else host, front_app=self.ctx.get("app"))
        env["urls"] = {h: s for h, s in (self.env_base.get("urls") or {}).items() if not ca.local_only(h, cfg)}
        mid = (mail or {}).get("message_id") or None
        with self.lock:
            self.mail_id = mid
        ev = self.env_base.get("event_card") or {}
        raw = [self.ctx.get("app"), env["host"], self.ctx.get("space"), mid, env["mail_gated"]]
        if ev.get("agent"):                             # an agent event: its summary is its own (agent + episode)
            raw += [ev["agent"], ev.get("since")]
        live = self.env_base.get("live_events") or []   # the live items listed change with the live state
        if live:
            raw.append(sorted(f"{e.get('id')}#{(e.get('ref') or {}).get('n')}" for e in live if isinstance(e, dict)))
        return hashlib.sha1(json.dumps(raw, default=str).encode()).hexdigest()[:16], env

    def note(self, row: dict) -> None:
        with self.lock:
            if len(self.expansions) < 16:
                self.expansions.append(row)

    def _tops(self) -> list[int]:
        return sorted(k for k, o in self.keymap.items() if o.get("primitive"))

    def node(self, path) -> dict | None:
        if not path:
            return None
        if len(path) == 1:
            return self.keymap.get(path[0])
        kids = self.children.get(tuple(path[:-1])) or []
        i = path[-1] - 1
        return kids[i] if 0 <= i < len(kids) else None

    def ids(self, path) -> list[str]:
        out = []
        for i in range(1, len(path) + 1):
            n = self.node(path[:i])
            out.append(n.get("id") or drill.node_id(n) if n else "?")
        return out

    def chain(self, path) -> str:
        parts = [describe(self.keymap.get(path[0]) or {})]
        for i in range(2, len(path) + 1):
            n = self.node(path[:i])
            parts.append(n["label"] if n else "?")
        return " > ".join(parts)

    def plan(self, path, env: dict) -> tuple[dict, list[dict], list[dict], bool]:
        """W23: (scope, fixed head, ranked local items, needs Claude) of the option at `path`. Claude only for a
        generative scope (agent, mail) whose providers are thin."""
        parent = self.node(path)
        scope = drill.scope_of(parent)
        head: list[dict] = []
        if parent and parent.get("live"):                       # a live item acts on its own target only
            return {"kind": "leaf"}, live_children(parent), [], False
        if parent and parent.get("primitive") == "app.open":   # an app direction lists its live items first
            head = live_items((parent.get("params") or {}).get("app"), env)
        items = self.engine.providers.items(scope, env)
        if scope["kind"] in ("host", "app"):                   # the summary of that page / app (Claude on press)
            items.append(resolve({"label": "Summary", "action": "summarise"}, parent, env, strict=True))
        ids = self.ids(path)
        items = [n for n in items if n and n.get("id") != ids[-1]]   # never the parent itself (a page under its page)
        items = drill.rank(items, ids[-1], parent.get("token"), env.get("history") or [], self.engine.pairs())
        items = [finish(n) for n in items if drill.in_scope(n, scope)]
        gen = scope["kind"] in drill.GENERATIVE and env["claude_ok"]
        return scope, head, items, gen and len(head) + len(items) < MAX_FOLLOWUPS

    @staticmethod
    def merge(head: list[dict], picked: list[dict], items: list[dict]) -> list[dict]:
        """Head, then Claude's picks, then the remaining provider items (deduped), MAX_FOLLOWUPS at most."""
        out, seen = [], set()
        for n in head + picked + items:
            if n["id"] not in seen:
                seen.add(n["id"])
                out.append(n)
        return out[:MAX_FOLLOWUPS]

    def wiki_for(self, path, scope: dict) -> list[str]:
        """The local wiki (business context) only at depth ≥ 2 under an agent / mail parent."""
        if len(path) < 2 or scope["kind"] not in ("agent", "mail"):
            return []
        try:
            return list(self.engine.wiki(self.chain(path)))[:3]
        except Exception:
            return []

    # ---- context cards ----

    def summarised(self, opt: dict, env: dict) -> bool:
        """The direction a level-3 summary belongs to: a mail direction, or the options of an agent event."""
        return opt.get("primitive") in MAIL_OPTS or bool(opt.get("evt") and (env.get("event_card") or {}).get("agent"))

    def agent_material(self, env: dict) -> str | None:
        """An agent event's pending messages (read once per hold, from the agent's local record; never stored), only
        when they may go to the model: Claude on, not a private event, no denied term (W25 leaks gate)."""
        ev = env.get("event_card") or {}
        if not ev.get("agent") or ev.get("private") or not env.get("claude_ok"):
            return None
        if not any(o.get("evt") for o in self.keymap.values()):
            return None
        if self._agent_text is None:
            try:
                self._agent_text = self.engine.agent_reader(ev["agent"]) or ""
            except Exception:
                self._agent_text = ""
        text = self._agent_text
        if not text:
            return None
        try:
            from .orchestrator import pack
            if pack.leaks(text, self.engine.settings_loader()):
                return None
        except Exception:
            return None
        return text[-MATERIAL_MAX:].strip() or None

    def material(self, env: dict) -> str | None:
        """The text a level-3 summary may read: the mail of a mail direction, only when it may go to the model
        (Claude on, no local-only domain, not a private event); for an agent event, its pending messages."""
        if (env.get("event_card") or {}).get("agent"):
            return self.agent_material(env)
        mail = env.get("mail")
        if not mail or env.get("mail_gated") or not env.get("claude_ok") or (env.get("event_card") or {}).get("private"):
            return None
        if not any(o.get("primitive") in MAIL_OPTS for o in self.keymap.values()):
            return None
        return _text(mail.get("body"), MATERIAL_MAX)

    def cards(self, key: str, env: dict, pending: bool = False) -> list[dict]:
        """One card per top option that has a `what`: + the cached summary (mail directions), + the judge's why
        (the event's options). `pending`: a summary is being prepared."""
        ev = env.get("event_card") or {}
        got = self.engine.get(f"{key}|ctx")
        out = []
        for k in self._tops():
            opt = self.keymap[k]
            what = what_line(opt, env, self.ctx)
            if not what:
                continue
            c: dict = {"key": k, "what": what}
            if self.summarised(opt, env) and self.material(env):
                if got:
                    c.update(got[0])
                elif pending:
                    c["pending"] = True
            elif opt.get("evt") and ev.get("summary") and not ev.get("private"):
                c["summary"] = ev["summary"]           # a local level 3 (metric, market, cockpit): no model
            if opt.get("evt") and ev.get("why") and not ev.get("private"):
                c["why"] = ev["why"]
            out.append(c)
        return out

    def send_cards(self, key: str, env: dict, pending: bool = False) -> int:
        cards = self.cards(key, env, pending)
        if cards:
            self.engine.sender({"cmd": "context", "cards": cards})
        return len(cards)

    def prefetch(self) -> None:
        """Hold start: the context cards (level 2 at once, local), the local items of every visible option, then ONE
        batched call for those that need Claude and for the level-3 summary (skipped when all are cached); the cards
        are sent again with the summary when it lands."""
        t0 = self.engine.clock()
        key, env = self.read_context()
        tops = self._tops()
        if not tops:
            return
        mat = self.material(env)
        ck_ctx = f"{key}|ctx"
        need_ctx = mat is not None and self.engine.get(ck_ctx) is None
        n_cards = self.send_cards(key, env, pending=need_ctx)
        if not need_ctx and all(self.engine.get(f"{key}|{self.ids([k])[0]}") is not None for k in tops):
            self.note({"path": "*", "ms": 0, "cache": True, "ok": True, "cards": n_cards})
            return
        if not self.engine.begin(f"{key}|*"):
            if need_ctx:                                # the running prefetch of this context brings the summary
                self.engine.wait(f"{key}|*", self.engine.timeout)
                self.send_cards(key, env)
            return
        code, asked, summarised = "ok", False, False
        try:
            want = []
            for k in tops:
                ck = f"{key}|{self.ids([k])[0]}"
                if self.engine.get(ck) is not None:
                    continue
                scope, head, items, needs = self.plan([k], env)
                if needs:
                    want.append((k, ck, head, items, scope))
                else:
                    self.engine.put(ck, self.merge(head, [], items))
            if want or need_ctx:
                asked = True
                prompt, ids = build_prompt(self.ctx, env, [(k, describe(self.keymap[k]), items, scope)
                                                           for k, _, _, items, scope in want],
                                           material=mat if need_ctx else None)
                out, code, _ = self.engine.call(prompt)
                fus = (out or {}).get("followups")
                if isinstance(fus, dict):
                    for k, ck, head, items, scope in want:
                        picked = resolve_list(fus.get(str(k)), self.keymap[k], env, scope=scope, items=ids[k])
                        self.engine.put(ck, self.merge(head, picked, items))
                card = parse_card(out, mat) if need_ctx and mat else None
                if need_ctx and code == "ok":           # an answer without a card is cached too (no re-ask per hold)
                    self.engine.put(ck_ctx, [card or {}])
                summarised = bool(card)
        finally:
            self.engine.done(f"{key}|*")
        if need_ctx:                                    # level 3 ready (or given up: no more "preparing")
            self.send_cards(key, env)
        self.note({"path": "*", "ms": int((self.engine.clock() - t0) * 1000), "cache": False, "ok": code == "ok",
                   "claude": asked, "cards": n_cards, "summary": summarised})

    def expand(self, path: list[int]) -> dict:
        """The follow-ups of the option at `path`, sent to the HUD as `subring` (the payload is returned)."""
        t0 = self.engine.clock()
        key, env = self.read_context()
        parent = self.node(path)
        nodes: list[dict] = []
        hit, ok, asked = False, True, False
        if parent is not None and 1 <= len(path) <= MAX_DEPTH:
            ck = f"{key}|{'>'.join(self.ids(path))}"
            got = self.engine.get(ck)
            if got is None and len(path) == 1:                 # the hold's prefetch may still be running
                self.engine.wait(f"{key}|*", self.engine.timeout)
                got = self.engine.get(ck)
            hit = got is not None
            if got is None and self.engine.begin(ck):
                try:
                    scope, head, items, needs = self.plan(path, env)
                    got = self.merge(head, [], items)
                    if needs:
                        asked = True
                        prompt, ids = build_prompt(self.ctx, env, [(1, self.chain(path), items, scope)],
                                                   wiki=self.wiki_for(path, scope))
                        out, code, _ = self.engine.call(prompt)
                        ok = code == "ok"
                        fus = (out or {}).get("followups")
                        if isinstance(fus, dict):
                            raw = fus.get("1") if "1" in fus else next(iter(fus.values()), None)
                            got = self.merge(head, resolve_list(raw, parent, env, scope=scope, items=ids[1]), items)
                    if ok:                                     # a failed call is not cached (retried next time)
                        self.engine.put(ck, got)
                finally:
                    self.engine.done(ck)
            elif got is None:                                  # the same expansion is running for another show
                self.engine.wait(ck, self.engine.timeout)
                got = self.engine.get(ck)
            nodes = got or []
        with self.lock:
            self.children[tuple(path)] = nodes
        payload = {"cmd": "subring", "path": list(path), "options": [
            {"key": i + 1, "label": n["label"], "icon": n["icon"], "action": n["action"],
             **({"claude": True} if n["claude"] else {}),
             **({"card": dict(n["card"], key=i + 1)} if n.get("card") else {})}   # a live item's card
            for i, n in enumerate(nodes)]}
        self.engine.sender(payload)
        self.note({"path": dotted(path), "ms": int((self.engine.clock() - t0) * 1000), "cache": hit, "ok": ok,
                   "n": len(nodes), "claude": asked})
        return payload

    def record(self, path: list[int]) -> None:
        """W23: a chosen branch -> a training pair (parent id, chain ids, choice id; no label)."""
        try:
            ids = self.ids(path)
            self.engine.pair_logger(ids[-2], ids[:-1], ids[-1], self.ctx.get("app"))
        except Exception:
            pass

    def execute(self, path: list[int], *, run_primitive: Callable[[dict], str | None],
                press: Callable[[dict, str | None], tuple[str | None, dict | None]],
                send_vocal: Callable[[str, str], dict]) -> tuple[str | None, str | None, dict | None]:
        """Run the follow-up at `path` (len ≥ 2). Returns (executed token, action id, claude meta). A send ("vocal",
        "ask") runs on release: oi.vocal.send asks the outward gate's panel, the one confirmation."""
        n = self.node(path)
        if not n:
            return None, None, None
        kind = n["kind"]
        executed, action, meta = None, n["action"], None
        if kind == "primitive":
            executed = run_primitive(n)
        elif kind in ("draft", "summary"):
            executed, meta = press(n, self.mail_id if kind == "draft" else None)
        elif kind == "dispatch":                              # W25: arm "pass me to X" (nothing is sent)
            dispatch.arm(n["params"]["to"])
            executed = f"dispatch:{n['params']['to']}"
        elif kind in drill.SEND_KINDS or kind == "ask":
            r = send_vocal(n["params"]["to"], n["params"]["text"])
            executed = f"vocal:{n['params']['to']}" if r.get("ok") else None
            if executed and n["params"].get("dispatch"):          # W25: a hand-over also arms that agent
                dispatch.arm(n["params"]["to"])
        if executed:
            self.record(path)
        return executed, action, meta


def log_entry(glide: dict, session: GlideSession | None, action: str | None) -> dict:
    """The hud-log `glide` field: paths, times, counts and the action id only (no label, message or prompt)."""
    def path_of(p):
        return dotted(p) if isinstance(p, list) and all(isinstance(k, int) for k in p) else None
    visited = []
    for v in glide.get("visited") or []:
        if isinstance(v, dict) and path_of(v.get("path")) and isinstance(v.get("ms"), int):
            visited.append({"path": path_of(v["path"]), "ms": v["ms"]})
    depth = glide.get("depth") if isinstance(glide.get("depth"), int) else 0
    out = {"visited": visited[:32], "depth": max(0, min(depth, MAX_DEPTH)),
           "expanded": glide.get("expanded") if isinstance(glide.get("expanded"), int) else 0,
           "final": path_of(glide.get("final")), "action": action}
    if session is not None:
        with session.lock:
            out["expansions"] = [dict(e) for e in session.expansions]
    return out
