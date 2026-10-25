"""W17: who is the user talking with right now? The current conversation partner, for "send this screenshot
to who I'm talking with".

Sources, newest wins, only within the last `WINDOW_S` (15 min) of today's tokens:
  - the Telegram front chat: a `title:Telegram…/<slug>` token whose slug starts with the slug of a
    settings.json `contact_map` title ("Atlas - R&D" -> atlas, "Nova - CEO…" -> nova, …). The title is read only
    to map it; it is never logged (only the resolved agent slug leaves this module);
  - the last voice note sent from the HUD: a `vocal:<slug>` token (W12).
A slug that is not a known recipient (oi.vocal.recipients) is ignored. None = nobody resolved.

settings.json `contact_map`: {"<chat title>": "<agent slug>" | null}, merged over DEFAULT_CONTACT_MAP (null
removes a default). A trailing "…" / "..." in a title is accepted (all titles match as a prefix anyway).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .vocab import parse_ts, slug

WINDOW_S = 15 * 60
DEFAULT_CONTACT_MAP: dict[str, str] = {
    "Atlas - R&D": "atlas",
    "Nova - CEO": "nova",
    "Orion - Fund": "orion",
    "Lyra - Sales": "lyra",
    "Vega - Marketing": "vega",
    "Juno - Ext R&D": "juno",
}
_AGENT = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def _merged(raw_settings: dict | None) -> dict[str, str]:
    """{chat title: agent slug}: the defaults, overridden by settings.json `contact_map` (null removes a title)."""
    table = dict(DEFAULT_CONTACT_MAP)
    over = (raw_settings or {}).get("contact_map")
    if isinstance(over, dict):
        for title, agent in over.items():
            if not isinstance(title, str):
                continue
            if agent is None:
                table.pop(title, None)
            elif isinstance(agent, str) and _AGENT.match(agent):
                table[title] = agent
    return table


def contact_map(raw_settings: dict | None = None) -> dict[str, str]:
    """{slug of a chat title: agent slug}: the defaults, overridden by settings.json `contact_map`."""
    out: dict[str, str] = {}
    for title, agent in _merged(raw_settings).items():
        s = slug(title.rstrip(".… "))
        if s:
            out[s] = agent
    return out


def chat_titles(raw_settings: dict | None = None) -> dict[str, str]:
    """{agent slug: its Telegram chat title} (the first title of the merged `contact_map` naming that agent)."""
    out: dict[str, str] = {}
    for title, agent in _merged(raw_settings).items():
        t = title.rstrip(".… ").strip()
        if t:
            out.setdefault(agent, t)
    return out


def telegram_contact(token: str, table: dict[str, str]) -> str | None:
    """The agent of a `title:Telegram…/<slug>` token, else None. Longest matching title wins."""
    if not token.startswith("title:Telegram"):
        return None
    head, _, s = token.partition("/")
    if not s or not head[len("title:"):].startswith("Telegram"):
        return None
    best = max((k for k in table if s.startswith(k)), key=len, default=None)
    return table[best] if best else None


def resolve(rows: list[dict], now: float, *, raw_settings: dict | None = None,
            known: set[str] | None = None, window_s: float = WINDOW_S) -> dict | None:
    """{"slug", "via": "telegram"|"vocal", "ts"} of the most recent conversation partner within `window_s`
    before `now` (epoch s), or None. `known`: the recipient slugs that can be delivered to (None = any)."""
    table = contact_map(raw_settings)
    since = datetime.fromtimestamp(now, timezone.utc) - timedelta(seconds=window_s)
    for r in reversed(rows):
        tok = r.get("token") or ""
        try:
            t = parse_ts(r["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if t < since:
            break
        if t.timestamp() > now + 1:
            continue
        agent, via = None, None
        if tok.startswith("vocal:"):
            agent, via = tok.split(":", 1)[1], "vocal"
        else:
            agent = telegram_contact(tok, table)
            via = "telegram" if agent else None
        if agent and (known is None or agent in known):
            return {"slug": agent, "via": via, "ts": r["ts"]}
    return None
