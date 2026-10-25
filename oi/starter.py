"""W22: the starter catalogue in the live driver — priors that make the HUD useful from day one.

`oi onboard` (oi/onboard.py) writes `<data dir>/starter_catalogue.yaml`: 15-30 contextual actions, each
{id, label, primitive, params, trigger {apps, hosts, hours, days}, prior}. `oi live` asks `Starters` for the items
whose trigger fits the moment and BLENDS them into the Markov / Laya menu:

    λ        = half_support / (half_support + support)     support = the Markov count of the current context
    p_item   = (prior · A + picked) / (A + shown)             the item's own usage (A = PRIOR_WEIGHT pseudo-shows)
    matched  : p_show = (1 - λ) · p_markov/laya + λ · p_item  (a Markov option on the same app / host)
    new      : p_show = λ · p_item                            (no history for it yet)

So on day one (support 0) the priors rank alone; as real usage of a context accumulates λ → 0 and Markov / Laya
take over; an item shown again and again but never picked sinks, a picked one rises. An item with no app / host in
its trigger ("anywhere") counts half. A starter option present lifts the thin-history gate (that is its purpose).

Refinement (W18 learning): `starter-stats.json` (0600) counts per item `shown` / `picked` (HUD presses, from
`Live.on_reply`) and `wished` (a W18 wish executed to the same action, from wish-actions/); a wish executed to an
app / site that no item covers becomes a `learned` item (prior LEARNED_PRIOR, trigger = the app it was made in).
Settings (settings.json `starter`, every key optional): {"enabled": true, "half_support": 20}.
Logged: the item id in the hud-log option row (`starter`), never a label's context.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import yaml

from . import paths, vocab

CATALOGUE = "starter_catalogue.yaml"
PROFILE = "profile.yaml"
STATS = "starter-stats.json"
PRIMITIVES = ("app.open", "url.open", "mail.draft", "agent.ask", "folder.open")
DEFAULTS = {"enabled": True, "half_support": 20.0}
PRIOR_WEIGHT = 4.0          # A: how many shows the prior is worth
ANYWHERE = 0.5              # an item without app / host in its trigger
LEARNED_PRIOR = 0.15
WISH_DAYS = 14
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def catalogue_path() -> Path:
    return paths.data_dir() / CATALOGUE


def profile_path() -> Path:
    return paths.data_dir() / PROFILE


def stats_path() -> Path:
    return paths.data_dir() / STATS


def settings(path: Path | None = None) -> dict:
    out = dict(DEFAULTS)
    try:
        raw = json.loads((path or paths.data_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    s = raw.get("starter") if isinstance(raw, dict) else None
    if isinstance(s, dict):
        if isinstance(s.get("enabled"), bool):
            out["enabled"] = s["enabled"]
        v = s.get("half_support")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out["half_support"] = float(v)
    return out


def host_of(url: str | None) -> str:
    u = (url or "").split("://", 1)[-1]
    return u.split("/", 1)[0].split(":", 1)[0].lower().removeprefix("www.")


def token_of(item: dict) -> str:
    """The token an item stands for: the Markov token when there is one (merge key), else starter:<id>."""
    p, prim = item.get("params") or {}, item.get("primitive")
    if prim == "app.open" and p.get("app") and not p.get("deeplink"):
        return f"activate:{p['app']}"
    if prim == "url.open" and host_of(p.get("href")):
        return f"url:{host_of(p['href'])}/{vocab.slug(p['href'].split('://', 1)[-1].partition('/')[2].split('/')[0]) or 'home'}"
    return f"starter:{item['id']}"


def _target(token: str | None) -> str | None:
    """Merge key of a token: app name or page host."""
    kind, _, arg = (token or "").partition(":")
    if kind == "activate":
        return f"app:{arg}"
    if kind == "url":
        return f"host:{arg.split('/', 1)[0].removeprefix('www.')}"
    return None


def fits(item: dict, ctx: dict, when: datetime) -> float:
    """0 when the trigger does not fit the moment, else its weight (1 for an app / host match, ANYWHERE otherwise)."""
    t = item.get("trigger") or {}
    hours = t.get("hours")
    if isinstance(hours, list) and len(hours) == 2:
        try:
            h0, h1 = int(hours[0]), int(hours[1])
        except (TypeError, ValueError):     # a hand-edited catalogue must never break the live loop
            return 0.0
        h = when.hour
        if not (h0 <= h < h1 if h0 <= h1 else (h >= h0 or h < h1)):
            return 0.0
    days = t.get("days")
    if isinstance(days, list) and days and DAYS[when.weekday()] not in days:
        return 0.0
    apps, hosts = t.get("apps") or [], t.get("hosts") or []
    if not apps and not hosts:
        return ANYWHERE
    app = (ctx.get("app") or "").lower()
    host = host_of(ctx.get("url"))
    if app and any(app == str(a).lower() for a in apps):
        return 1.0
    if host and any(host == str(h).lower() or host.endswith("." + str(h).lower()) for h in hosts):
        return 1.0
    return 0.0


def _is_noop(item: dict, ctx: dict) -> bool:
    p = item.get("params") or {}
    if item.get("primitive") == "app.open" and not p.get("deeplink"):
        return (p.get("app") or "").lower() == (ctx.get("app") or "").lower()
    if item.get("primitive") == "url.open":
        return bool(host_of(ctx.get("url"))) and host_of(p.get("href")) == host_of(ctx.get("url"))
    return False


def _write_private(p: Path, text: str) -> None:
    paths.ensure_private_root()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)


def load_catalogue(path: Path | None = None) -> list[dict]:
    try:
        d = yaml.safe_load((path or catalogue_path()).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    items = d.get("items") if isinstance(d, dict) else None
    return [i for i in items or [] if isinstance(i, dict) and i.get("id") and i.get("primitive") in PRIMITIVES]


class Starters:
    """The catalogue + its usage stats for `oi live` (re-read when the files change; thread-safe)."""

    def __init__(self, *, clock: Callable[[], float] = time.time, now_local: Callable[[float], datetime] | None = None):
        self.clock = clock
        self.now_local = now_local or (lambda t: datetime.fromtimestamp(t))
        self.lock = threading.Lock()
        self._mtime: float | None = None
        self._items: list[dict] = []
        self.stats = self._load_stats()

    # -- files ------------------------------------------------------------------------------------------
    def items(self) -> list[dict]:
        p = catalogue_path()
        try:
            m = p.stat().st_mtime
        except OSError:
            self._mtime, self._items = None, []
            return []
        if m != self._mtime:
            self._mtime, self._items = m, load_catalogue(p)
        return self._items + [i for i in self.stats.get("learned") or [] if isinstance(i, dict) and i.get("id")]

    @staticmethod
    def _load_stats() -> dict:
        try:
            d = json.loads(stats_path().read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("items"), dict):
                d.setdefault("learned", [])
                return d
        except (OSError, ValueError):
            pass
        return {"items": {}, "learned": []}

    def save(self) -> None:
        with self.lock:
            text = json.dumps(self.stats, ensure_ascii=False, indent=1) + "\n"
        _write_private(stats_path(), text)

    # -- scoring ----------------------------------------------------------------------------------------
    def p_item(self, item: dict) -> float:
        s = self.stats["items"].get(item["id"]) or {}
        try:
            prior = min(max(float(item.get("prior") if item.get("prior") is not None else 0.1), 0.0), 1.0)
        except (TypeError, ValueError):
            prior = 0.1
        picked = int(s.get("picked", 0)) + int(s.get("wished", 0))
        return min(1.0, (prior * PRIOR_WEIGHT + picked) / (PRIOR_WEIGHT + int(s.get("shown", 0))))

    def matches(self, ctx: dict, now: float | None = None) -> list[tuple[dict, float]]:
        """(item, p_item × fit) of every item whose trigger fits the moment, best first."""
        when = self.now_local(self.clock() if now is None else now)
        out = []
        for it in self.items():
            w = fits(it, ctx, when)
            if w and not _is_noop(it, ctx):
                out.append((it, self.p_item(it) * w))
        out.sort(key=lambda x: -x[1])
        return out

    def blend(self, menu: list[dict], ctx: dict, support: int, cfg: dict | None = None,
              now: float | None = None) -> tuple[list[dict], bool]:
        """(menu with the starters blended in, whether any starter option is in it). See the module doc."""
        cfg = cfg or settings()
        if not cfg["enabled"]:
            return menu, False
        found = self.matches(ctx, now)
        if not found:
            return menu, False
        lam = cfg["half_support"] / (cfg["half_support"] + max(0, support))
        out = [dict(o) for o in menu]
        by_target: dict[str, dict] = {}
        for o in out:
            t = _target(o.get("token"))
            if t and (t not in by_target or _p(o) > _p(by_target[t])):
                by_target[t] = o
        new: list[dict] = []
        for it, p in found:
            tok = token_of(it)
            o = by_target.get(_target(tok) or "")
            if o is not None:
                if "starter" in o:
                    continue
                o["p_show"] = round((1 - lam) * _p(o) + lam * p, 6)
                o["starter"] = it["id"]
                if it["primitive"] == "url.open":
                    o["params"] = {**(o.get("params") or {}), "href": it["params"]["href"]}
                continue
            if any(n["starter"] == it["id"] or n["token"] == tok for n in new):
                continue
            new.append({"key": f"starter_{it['id']}", "criteria": it.get("label", "")[:60],
                        "primitive": it["primitive"], "params": dict(it.get("params") or {}), "token": tok,
                        "p_markov": None, "p_show": round(lam * p, 6), "starter": it["id"],
                        "label": it.get("label"), **({"icon": it["icon"]} if it.get("icon") else {})})
        nothing = [o for o in out if o.get("primitive") is None]
        rest = [o for o in out if o.get("primitive") is not None]
        return rest + new + nothing, bool(new) or any("starter" in o for o in rest)

    # -- learning ---------------------------------------------------------------------------------------
    def record(self, shown: list[str], picked: str | None) -> None:
        """One HUD show's outcome: every starter id that was on screen, and the one pressed (executed)."""
        if not shown and not picked:
            return
        with self.lock:
            for sid in dict.fromkeys(shown):
                self.stats["items"].setdefault(sid, {}).setdefault("shown", 0)
                self.stats["items"][sid]["shown"] += 1
            if picked:
                s = self.stats["items"].setdefault(picked, {})
                s["picked"] = s.get("picked", 0) + 1
        try:
            self.save()
        except OSError:
            pass

    def refine_from_wishes(self, days: int = WISH_DAYS) -> int:
        """Re-count `wished` from the last `days` of wish-actions/ (executed wishes only) and add a `learned` item
        for an app / site wished in an app with no item for it. Returns the number of learned items added."""
        items = self.items()
        by_tok = {token_of(i): i for i in items}
        by_target = {_target(t): i for t, i in by_tok.items() if _target(t)}
        wished: dict[str, int] = {}
        learned = list(self.stats.get("learned") or [])
        have = {i["id"] for i in items}
        since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        for f in sorted((paths.data_dir() / "wish-actions").glob("????-??-??.jsonl")):
            if f.stem < since:
                continue
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or r.get("state") != "done" or r.get("dry_run"):
                    continue
                tok = r.get("token") if isinstance(r.get("token"), str) else ""
                it = by_tok.get(tok) or by_target.get(_target(tok))
                if it:
                    wished[it["id"]] = wished.get(it["id"], 0) + 1
                    continue
                kind, _, arg = tok.partition(":")
                app = r.get("app") if isinstance(r.get("app"), str) else None
                if kind == "activate" and arg:
                    new = {"primitive": "app.open", "params": {"app": arg}, "label": arg[:22]}
                elif kind == "url" and arg:
                    h = arg.split("/", 1)[0]
                    new = {"primitive": "url.open", "params": {"href": f"https://{h}/"}, "label": h[:22]}
                else:
                    continue
                nid = "learned-" + vocab.slug(f"{kind}-{arg.split('/', 1)[0]}-{app or 'any'}", 40)
                if nid in have:
                    wished[nid] = wished.get(nid, 0) + 1
                    continue
                new.update(id=nid, trigger={"apps": [app]} if app else {}, prior=LEARNED_PRIOR, source="wish")
                learned.append(new)
                have.add(nid)
                wished[nid] = wished.get(nid, 0) + 1
        added = len(learned) - len(self.stats.get("learned") or [])
        with self.lock:
            for sid, s in self.stats["items"].items():
                s["wished"] = wished.pop(sid, 0)
            for sid, n in wished.items():
                self.stats["items"][sid] = {"wished": n}
            self.stats["learned"] = learned
        try:
            self.save()
        except OSError:
            pass
        return added


def _p(o: dict) -> float:
    return float(o.get("p_show", o.get("p_markov")) or 0.0)


_SAFE_HREF = re.compile(r"^https?://[a-z0-9.-]+(?::\d+)?(/[^\s]*)?$", re.I)


def safe_href(href: str | None) -> str | None:
    """A starter's http(s) link (query and fragment kept out: see live.safe_url)."""
    return href if isinstance(href, str) and _SAFE_HREF.match(href) else None
