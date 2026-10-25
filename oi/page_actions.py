"""W16: page actions — the HUD proposes clicking the button that matters on the current Safari page.

The user 2026-10-20 ~17:50 Paris, on a GitHub PR page: "i would expect an action proposed to click on squash and
merge". Three parts, all local:

1. READ (oi-hud, Accessibility): `{"cmd":"page"}` on hud.sock answers `{"event":"page","trusted",
   "app","host","elements":[{"role":"button|link","label","slug","frame":[x,y,w,h]}]}` — the AXButton /
   AXLink elements of Safari's front web area, labels and frames only (no page text, no field values).
   Without Accessibility for oi-hud: `trusted: false`, no elements (nothing is offered, nothing breaks).
2. CANDIDATES: rule-based primary actions per site (`RULES`: GitHub PR merge / approve / delete branch / close;
   Gmail, Notion, Linear left as empty tables to fill) plus LEARNED clicks: oi-watcher logs
   `click:<host>/<role>:<label-slug>` at mouse-up (oi/normalise.py `click_tokens`), they enter the Markov
   catalogue like any token and come back through form C's `press_button` option. Both become `ui.press`
   options, offered only when the element is on the page NOW (snapshot), at most `MAX_PAGE_OPTIONS` rules.
3. PRESS: `ui.press` never stores a coordinate: `{"cmd":"ax_press","role","slug","host"}` makes oi-hud
   re-find the element by role + label slug in the front page at press time and AXPress it; the page
   changed / element gone / other host -> nothing pressed + a toast. oi-hud only presses with a fresh
   ticket from the user's own tap on that option (HUDCore PressGuard). One tap picks; a press that is not known
   read-only then waits for the outward gate's panel (oi/gate.py, the ONE confirmation, the W16 two-tap
   is gone); an irreversible label (merge, delete, send, pay, publish, close, ...) presses only with the ticket
   that panel's validate issues.

The option's target hint (W11 overlay) is the button's own frame: `{"kind":"element","rect":[x,y,w,h]}`
(CG global coordinates, only for drawing; the press never uses it).
"""
from __future__ import annotations

import json
import re
import socket
from pathlib import Path

from . import vocab

PRIMITIVE = "ui.press"
ROLES = ("button", "link")
MAX_PAGE_OPTIONS = 2          # rule-based page actions per show (pinned first in a summon)
PAGE_TIMEOUT_S = 0.6          # oi-hud prefetches on the summon key; the live show replaces the fallback
PRESS_TIMEOUT_S = 3.0

# Irreversible verbs.
# Matched on the label's slug words: "squash-and-merge", "delete-branch", "close-pull-request" -> the gate's ticket.
IRREVERSIBLE = frozenset({"merge", "delete", "send", "pay", "publish", "close", "remove", "discard",
                          "submit", "archive", "transfer", "purchase", "buy"})


def irreversible(slug: str) -> bool:
    return any(w in IRREVERSIBLE for w in (slug or "").split("-"))


# Destructive verbs (incident 2026-10-21 12:28Z: a summoned HUD offered "Close PR" as a one-tap option next to
# "Squash & merge"; the user pressed it and his PR was closed). A destructive page button is NEVER a HUD option
# (summoned or automatic, rule-based or learned) and a press / click on one never feeds the learned ranking
# (oi/normalise.py). It stays reachable only through a spoken / typed wish, which always asks the gate's panel.
# Matched on the label slug's words (ASCII-folded, so "Supprimer" / "Désabonner" match). "merge" / "approve" are
# irreversible (gated) but not destructive: they stay offered.
DESTRUCTIVE = frozenset({
    # EN
    "close", "delete", "discard", "remove", "cancel", "reject", "revert", "abandon", "archive", "unsubscribe",
    "leave", "lock", "decline", "deny", "trash", "erase", "unpublish", "block", "dismiss",
    # FR (infinitive + imperative / 3rd person, accents folded by the slug)
    "fermer", "ferme", "supprimer", "supprime", "annuler", "annule", "rejeter", "rejette", "refuser", "refuse",
    "retirer", "retire", "archiver", "abandonner", "abandonne", "quitter", "quitte", "verrouiller", "verrouille",
    "desabonner", "desabonne", "desinscrire", "effacer", "efface", "revoquer", "bloquer", "bloque",
})


def destructive(slug: str | None) -> bool:
    """True when the label slug carries a destructive verb (close, delete, cancel, supprimer, ...)."""
    return any(w in DESTRUCTIVE for w in (slug or "").split("-"))


_CLICK_SLUG = re.compile(r"^click:[^/]+/(?:button|link):(?P<slug>[a-z0-9-]+)$")


def destructive_token(token: str | None) -> bool:
    """True for a `click:<host>/<role>:<slug>` token whose label is destructive."""
    m = _CLICK_SLUG.match(token or "")
    return bool(m and destructive(m.group("slug")))


# Rule table: host -> ordered rules (best first). A rule = (label slug, role, short label, SF symbol).
# Only rules whose element is on the page right now become options.
RULES: dict[str, list[tuple[str, str, str, str]]] = {
    "github.com": [
        ("confirm-squash-and-merge", "button", "Confirm squash", "arrow.triangle.merge"),
        ("confirm-merge", "button", "Confirm merge", "arrow.triangle.merge"),
        ("squash-and-merge", "button", "Squash & merge", "arrow.triangle.merge"),
        ("merge-pull-request", "button", "Merge PR", "arrow.triangle.merge"),
        ("approve", "button", "Approve", "checkmark.seal"),
        # destructive (incident 2026-10-21): never a HUD option (rule_options / apply skip them); kept here so a
        # spoken / typed wish ("ferme la PR") still finds them, always through the gate's panel
        ("delete-branch", "button", "Delete branch", "trash"),
        ("close-pull-request", "button", "Close PR", "xmark.circle"),
    ],
    "mail.google.com": [],        # later (): Send, Archive, Reply
    "www.notion.so": [],          # later
    "linear.app": [],             # later
}
DEFAULT_ICON = "cursorarrow.click"


def rules_for(host: str | None) -> list[tuple[str, str, str, str]]:
    h = (host or "").lower().split(":", 1)[0]
    return RULES.get(h) or RULES.get(h.removeprefix("www.")) or []


# ---- tokens ----------------------------------------------------------------------------------------

def click_token(host: str, role: str, label: str) -> str | None:
    """`click:<host>/<role>:<label-slug>` (None when it does not validate)."""
    s = vocab.slug(label)
    tok = f"click:{(host or '').lower()}/{role}:{s}"
    return tok if s and role in ROLES and vocab.validate(tok) else None


# ---- the page snapshot (from oi-hud) ----------------------------------------------------------------

def _clean_element(e) -> dict | None:
    if not isinstance(e, dict) or e.get("role") not in ROLES:
        return None
    label = e.get("label") if isinstance(e.get("label"), str) else ""
    slug = vocab.slug(e.get("slug") or label)
    if not slug:
        return None
    fr = e.get("frame")
    frame = [float(x) for x in fr] if (isinstance(fr, list) and len(fr) == 4
                                       and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in fr)) else None
    return {"role": e["role"], "label": label[:80], "slug": slug, "frame": frame}


def parse_page(reply) -> dict:
    """oi-hud's `page` reply -> {trusted, host, elements} (anything malformed -> no elements)."""
    if not isinstance(reply, dict) or reply.get("event") != "page":
        return {"trusted": False, "host": None, "elements": [], "reason": "no_reply"}
    els = [c for c in (_clean_element(e) for e in reply.get("elements") or []) if c]
    host = reply.get("host") if isinstance(reply.get("host"), str) else None
    return {"trusted": reply.get("trusted") is True, "host": host.lower() if host else None, "elements": els,
            "reason": reply.get("reason") if isinstance(reply.get("reason"), str) else None}


def request(path: Path, payload: dict, want: str, timeout_s: float) -> dict | None:
    """One command on hud.sock; the first line whose `event` == want (broadcasts skipped), else None."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_s)
            s.connect(str(path))
            s.sendall((json.dumps(payload) + "\n").encode())
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return None
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and ev.get("event") in (want, "error"):
                        return ev
    except (OSError, ValueError):
        return None


class PageClient:
    """Talks to oi-hud for the page snapshot and the press. Swapped for a fake in tests."""

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path

    def snapshot(self) -> dict:
        return parse_page(request(self.socket_path, {"cmd": "page"}, "page", PAGE_TIMEOUT_S))

    def press(self, params: dict, *, dry_run: bool = False) -> dict:
        cmd = {"cmd": "ax_press", "role": params.get("role"), "slug": params.get("slug"),
               "host": params.get("host"), "dry_run": bool(dry_run)}
        r = request(self.socket_path, cmd, "ax_press", PRESS_TIMEOUT_S)
        if not isinstance(r, dict) or r.get("event") != "ax_press":
            return {"ok": False, "reason": (r or {}).get("message") or "no_reply"}
        return {"ok": r.get("ok") is True, "reason": r.get("reason"), "dry_run": r.get("dry_run") is True}


# ---- menu ----------------------------------------------------------------------------------------

def find(page: dict, role: str, slug: str) -> dict | None:
    """The element with this role and label slug (the first in page order), else None."""
    for e in page.get("elements") or []:
        if e["role"] == role and e["slug"] == slug:
            return e
    return None


def _host_of(ctx: dict) -> str | None:
    """The page host of a moment context (`url` host/slug) or of a W18 wish context (`host`)."""
    url = ctx.get("url") if isinstance(ctx.get("url"), str) else None
    if url:
        return url.split("/", 1)[0].lower()
    return ctx["host"].lower() if isinstance(ctx.get("host"), str) and ctx["host"] else None


def rule_options(page: dict, host: str | None) -> list[dict]:
    """The rule-based page actions present on the page now, best first (positive primary action first: merge >
    approve), at most MAX_PAGE_OPTIONS. Destructive rules are never options. `rank` keeps the rule order in a
    summon even when a probability would rank them otherwise."""
    out: list[dict] = []
    for slug, role, short, icon in rules_for(host):
        if destructive(slug):
            continue
        e = find(page, role, slug)
        if not e:
            continue
        # the rank is in the key: a summon sorts pinned options by key when no probability ranks them
        out.append({"key": f"page{len(out) + 1}_{slug.replace('-', '_')}", "criteria": f"click {e['label'] or short}"[:60],
                    "primitive": PRIMITIVE, "token": click_token(host or "", role, slug),
                    "params": {"host": host, "role": role, "slug": slug, "short": short, "icon": icon},
                    "p_markov": None, "pin": True, "rank": len(out) + 1})
        if len(out) >= MAX_PAGE_OPTIONS:
            break
    return out


def apply(menu: list[dict], page: dict | None, ctx: dict, *, with_rules: bool) -> list[dict]:
    """Learned `ui.press` options stay only when their element is on the current page (same host); with
    `with_rules` (a summon) the rule-based actions are added, pinned first. No page (no Accessibility, not
    Safari, oi-hud down) -> no ui.press option at all. A destructive button (close, delete, cancel, ...) is never
    an option, on any path. Each kept option carries `_frame` for the hint."""
    host = _host_of(ctx)
    page = page or {"elements": []}
    if page.get("host") and host and page["host"] != host:
        page = {"elements": []}                         # the snapshot is of another page: offer nothing
    kept: list[dict] = []
    seen: set[tuple[str, str]] = set()
    rules = rule_options(page, host) if with_rules else []
    for o in rules + menu:
        if o.get("primitive") != PRIMITIVE:
            kept.append(o)
            continue
        p = o.get("params") or {}
        if destructive(p.get("slug")) or destructive_token(o.get("token")):
            continue
        if (p.get("host") or "").lower() != (host or "") or (p.get("role"), p.get("slug")) in seen:
            continue
        e = find(page, p.get("role"), p.get("slug"))
        if not e:
            continue
        seen.add((p["role"], p["slug"]))
        kept.append({**o, "_frame": e["frame"], "_label": e["label"]})
    return kept


def hud_fields(opt: dict) -> dict:
    """label / icon / target / hint of a ui.press option for the HUD show."""
    p = opt.get("params") or {}
    slug = p.get("slug") or ""
    label = p.get("short") or opt.get("_label") or slug.replace("-", " ").capitalize()
    out = {"label": label[:22], "icon": f"sf:{p.get('icon') or DEFAULT_ICON}",
           "target": f"{p.get('role')}:{slug}"}
    fr = opt.get("_frame")
    if fr and len(fr) == 4 and fr[2] > 0 and fr[3] > 0:
        out["hint"] = {"kind": "element", "rect": [round(float(x), 1) for x in fr]}
    return out


_TARGET = re.compile(r"^(button|link):[a-z0-9]+(?:-[a-z0-9]+)*$")


def valid_params(params: dict) -> bool:
    return (params.get("role") in ROLES and isinstance(params.get("slug"), str)
            and bool(_TARGET.match(f"{params['role']}:{params['slug']}")))


# ---- W18 wish -> action: the page's buttons as catalogue actions -----------------------------------------

# Spoken / typed phrasings of the rule verbs (FR + EN), matched by W18's local tier (exact / prefix / fuzzy).
WISH_PHRASES: dict[str, tuple[str, ...]] = {
    "merge": ("merge", "fusionne", "fusionner", "merge la pr", "fusionne la pr", "merge the pr", "merge pr"),
    "approve": ("approve", "approuve", "approuver", "approve the pr", "approuve la pr"),
    "delete": ("delete branch", "supprime la branche", "delete the branch"),
    "close": ("close pr", "ferme la pr", "close the pr"),
}
MAX_WISH_BUTTONS = 40


def wish_items(page: dict | None, ctx: dict) -> list[dict]:
    """W18 catalogue items (kind "page", primitive ui.press) for the page on screen: its rule-based key buttons
    (best first: higher `uses`) and up to MAX_WISH_BUTTONS other buttons. A wish press always asks the outward gate's
    panel (oi.live `_wish_exec`): its validate is the one confirmation and issues oi-hud's press ticket."""
    from .aliases import norm
    host = _host_of(ctx) or ((page or {}).get("host"))
    if not page or not host or (page.get("host") and page["host"] != host):
        return []
    items: dict[str, dict] = {}
    rules = rules_for(host)
    for rank, (slug, role, short, _icon) in enumerate(rules):
        e = find(page, role, slug)
        if not e:
            continue
        words = [w for w in slug.split("-") if w not in ("and", "the", "pull", "request")]
        names = [norm(e["label"]), norm(short), norm(" ".join(words))]
        for verb, phrases in WISH_PHRASES.items():
            if verb in words:
                names += [norm(p) for p in phrases]
        items[f"page:{role}:{slug}"] = _wish_item(host, role, slug, short, names, uses=100 - rank)
    for e in page.get("elements") or []:
        if len(items) >= MAX_WISH_BUTTONS + len(rules):
            break
        key = f"page:{e['role']}:{e['slug']}"
        if e["role"] == "button" and key not in items:
            items[key] = _wish_item(host, "button", e["slug"], e["label"][:40] or e["slug"], [norm(e["label"])], uses=0)
    return list(items.values())


def _wish_item(host: str, role: str, slug: str, label: str, names: list[str], *, uses: int) -> dict:
    return {"id": f"page:{role}:{slug}", "kind": "page", "primitive": PRIMITIVE,
            "params": {"host": host, "role": role, "slug": slug}, "token": click_token(host, role, slug),
            "label": f"Click “{label}”", "target": label, "names": list(dict.fromkeys(n for n in names if n)),
            "uses": uses, "criteria": f"click {label} on this page", "press": f"{role}:{slug}"}
