"""Data locations. Data never lives in the repo; override the root with OI_DATA_DIR."""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("OI_DATA_DIR")
    return Path(env).expanduser() if env else Path.home() / ".local" / "share" / "os-intent"


def raw_dir() -> Path:
    return data_dir() / "raw"


def tokens_dir() -> Path:
    return data_dir() / "tokens"


def wishes_dir() -> Path:
    """oi-hud writes the wish panel's rows here: wishes/YYYY-MM-DD.jsonl (UTC day, 0600)."""
    return data_dir() / "wishes"


def hud_log_dir() -> Path:
    """`oi live` (W8) logs every evaluated moment and its HUD outcome here: hud-log/YYYY-MM-DD.jsonl."""
    return data_dir() / "hud-log"


def status_dir() -> Path:
    """Small JSON status files read by oi-menubar (status/oi-live.json)."""
    return data_dir() / "status"


def hud_socket() -> Path:
    """oi-hud's Unix socket (hud/README.md "Socket protocol")."""
    return data_dir() / "hud.sock"


def cursor_dir() -> Path:
    return data_dir() / "cursors"


def ensure_private_root() -> Path:
    """Create the data root owner-only (0700): it holds raw window titles and URLs."""
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root
