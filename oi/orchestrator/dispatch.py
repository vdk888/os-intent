"""W25: "pass me to Atlas" — the armed dispatch target.

The user: "il dispatche ultra rapide, il peut me passer en direct à toi avec une touche, quand je vais envoyer un vocal
il va partir à toi".

A glide follow-up `dispatch:<agent>` or a wish "passe-moi Atlas" ARMS that agent for DISPATCH_TTL_S: The user's next
typed note (a wish that resolves to nothing else) is proposed to that agent (confirmed before it leaves, the W12
vocal path), and the next screenshot's "→ <Agent>" offer names them (oi.shots current contact). A dispatch WITH a
message sends it through the same W12 path after the user's confirm. Nothing is ever sent from here.

State: <data dir>/dispatch.json {"to": slug, "ts": epoch s} (0600). The vocal key's own picker (HUD side) reading
it is the later "UX du vocal" step.
"""
from __future__ import annotations

import json
import os
import time

from .. import paths

DISPATCH_TTL_S = 10 * 60


def _path():
    return paths.data_dir() / "dispatch.json"


def arm(slug: str, now: float | None = None) -> dict:
    paths.ensure_private_root()
    row = {"to": slug, "ts": time.time() if now is None else now}
    fd = os.open(_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(row, f)
    return row


def armed(now: float | None = None, ttl: float = DISPATCH_TTL_S) -> str | None:
    """The armed agent slug, or None (nothing armed / expired / unreadable)."""
    try:
        row = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    now = time.time() if now is None else now
    if not isinstance(row, dict) or not isinstance(row.get("to"), str) or not isinstance(row.get("ts"), (int, float)):
        return None
    return row["to"] if 0 <= now - row["ts"] <= ttl else None


def clear() -> None:
    try:
        _path().unlink()
    except OSError:
        pass
