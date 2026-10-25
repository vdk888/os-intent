"""Reader for the shared settings file `<data dir>/settings.json` (written by oi-menubar).

Schema (every key optional; see docs/DESIGN.md "Settings"):
  {"paused": false, "hud_enabled": true, "denylist_apps": ["Passwords", ...],
   "repo_dir": "/Users/you/os-intent", "updated_at": "2026-10-20T10:00:00Z",
   "gate": {"show_min_p": 0.15, ..., "outward": true}, "live": {"poll_s": 2, ...},   # W8: `oi live`
   (`gate.outward`: the outward gate, read by oi.gate.enabled: true | false | {"vocal"|"shot"|"page": bool})
   "vocal_recipients": {"nova": {...}, ...},   # W12: read by oi.vocal.recipients()
   "vocal": {"max_s": 300, "shot_offer": true, "shot_safe_hosts": [...]},   # max_s: oi-hud's recording limit;
                                               # shot_offer, shot_safe_hosts: oi.vocal_settings
   "laya": {"enabled": true}}   # the menu-bar Laya switch (oi.laya_switch); false = Markov only, server stopped
A missing, unreadable or corrupt file gives the defaults; a wrongly typed key falls back to its default.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import hud_keys, paths

DEFAULTS: dict = {
    "paused": False,
    "hud_enabled": True,
    "denylist_apps": ["Passwords", "Keychain Access", "1Password"],
    "repo_dir": str(Path.home() / "os-intent"),
    "updated_at": None,
}

# W8: the live driver's gate and timings. Flat numbers only (oi-menubar round-trips them).
# W9: `ttl_ms` = how long the HUD stays per show (oi-hud reads the pointer keys itself:
# hide_on_pointer_travel / pointer_grace_ms / pointer_fast_px_s / pointer_fast_ms).
GATE_DEFAULTS: dict = {"show_min_p": 0.15, "none_max_p": 0.6, "max_show": 3, "cooldown_s": 20, "min_history": 1,
                       "ttl_ms": 6000,   # W19: ttl_ms 15000 -> 6000 (the user: disappear faster)
                       "hint_cooldown_s": 8}   # W20: hints have their own (shorter) cooldown; full shows keep cooldown_s
LIVE_DEFAULTS: dict = {"poll_s": 2, "rebuild_s": 600, "retrain_s": 600}
_INT_KEYS = {"max_show", "min_history", "ttl_ms"}
# W13: the Laya layer. `laya_shadow` = also ask Laya for every evaluated moment and log it (never
# shown); `predictor` = what the HUD shows: "markov" (default) | "laya" | "blend", set ONLY by
# `oi laya-promote` once the design §5 pass bar is met; `laya_url` = the laya server; `laya_checkpoint` = the
# promoted oi checkpoint dir (None = stock English laya, zero-shot); `laya_model` = the laya-serve checkpoint
# name sent with every request (one checkpoint loaded, never auto-routed to a second one).
# `laya_rank` = the ONE switch for what Laya does to the HUD: "shadow" (default: asked and
# logged, never shown), "blend" (50/50 with Markov) or "live" (Laya's ranking). The user sets it (by hand, or via
# `oi laya-promote`), after `oi laya-report` says the promotion bar is met; nothing flips it automatically. A
# legacy `predictor` laya|blend (pre-switch settings) still maps to live|blend when `laya_rank` is absent.
# `laya_timeout_ms` 1050 = `oi laya-bench --gap 5` on 2026-10-20 (live-spaced p95 655 ms x 1.5 + 50; the 300 ms it replaces
# was below the first call after an idle gap, so every live call timed out). The call never delays a show.
# 2026-10-21: 1050 still timed out 35 % of live calls (cold after idle, memory pressure); default now 3000 (above the
# live tail; a late answer still merges). A stored value equal to a former default (LEGACY_LAYA_TIMEOUTS_MS: the
# settings app writes every key back) is read as the current default; any other value is the user's choice and kept.
PREDICTORS = ("markov", "laya", "blend")
LAYA_RANKS = ("shadow", "blend", "live")
LIVE_LAYA_DEFAULTS: dict = {"laya_shadow": False, "predictor": "markov", "laya_rank": "shadow",
                            "laya_url": "http://127.0.0.1:8781", "laya_checkpoint": None, "laya_model": "english",
                            "laya_timeout_ms": 3000}
LEGACY_LAYA_TIMEOUTS_MS = (300, 1050)


# W19: `hud.auto_mode` = how the gated (automatic) shows are sent: "hint" (default: coloured dots, ⌥⌘
# develops them), "full" (the cross, as before W19) or "off" (nothing; ⌥⌘ still summons); `hud.hint_ttl_ms` = how
# long the dots stay (4000 since W20, was 2500; clamped 500-10000). The other `hud` keys (W20: `hint_dot_size`)
# belong to oi-hud and are not read here.
AUTO_MODES = ("hint", "full", "off")
# W21: `hud.fill_order` = which key gets the most likely option, then the next… (see oi/hud_keys.py).
HUD_DEFAULTS: dict = {"auto_mode": "hint", "hint_ttl_ms": 4000,   # W20: hint_ttl_ms 2500 -> 4000
                      "fill_order": list(hud_keys.DEFAULT_FILL_ORDER)}   # W21
HINT_TTL_RANGE_MS = (500, 10000)


def _hud(raw) -> dict:
    out = dict(HUD_DEFAULTS)
    out["fill_order"] = list(hud_keys.DEFAULT_FILL_ORDER)   # never the shared default list
    if not isinstance(raw, dict):
        return out
    out["fill_order"] = hud_keys.fill_order(raw.get("fill_order"))   # W21
    if raw.get("auto_mode") in AUTO_MODES:
        out["auto_mode"] = raw["auto_mode"]
    v = raw.get("hint_ttl_ms")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
        out["hint_ttl_ms"] = int(min(max(v, HINT_TTL_RANGE_MS[0]), HINT_TTL_RANGE_MS[1]))
    return out
# end W19

# 2026-10-21: `laya.enabled` = the master switch, set by the menu bar
# (`oi laya-switch on|off`). Off = Markov only: `load()` then hands out an EFFECTIVE `live` view with the Laya
# layer off (laya_shadow false, laya_rank shadow, predictor markov), so oi live never calls, ranks on or starts
# Laya, and `oi health` does not check it. The stored `live` keys are untouched: switching back on restores them.
LAYA_SWITCH_DEFAULTS: dict = {"enabled": True}


def settings_path() -> Path:
    return paths.data_dir() / "settings.json"


def load(path: Path | None = None) -> dict:
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    out["gate"], out["live"] = dict(GATE_DEFAULTS), dict(LIVE_DEFAULTS) | dict(LIVE_LAYA_DEFAULTS)
    out["hud"] = dict(HUD_DEFAULTS)   # W19
    out["laya"] = dict(LAYA_SWITCH_DEFAULTS)
    try:
        raw = json.loads((path or settings_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out
    for key in ("paused", "hud_enabled"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    apps = raw.get("denylist_apps")
    if isinstance(apps, list):
        out["denylist_apps"] = [a.strip() for a in apps if isinstance(a, str) and a.strip()]
    for key in ("repo_dir", "updated_at"):
        if isinstance(raw.get(key), str):
            out[key] = raw[key]
    out["gate"] = _numbers(raw.get("gate"), GATE_DEFAULTS)
    out["live"] = _numbers(raw.get("live"), LIVE_DEFAULTS) | _laya(raw.get("live"))
    out["hud"] = _hud(raw.get("hud"))   # W19
    lw = raw.get("laya")
    if isinstance(lw, dict) and isinstance(lw.get("enabled"), bool):
        out["laya"]["enabled"] = lw["enabled"]
    if not out["laya"]["enabled"]:                     # the switch is off: the Laya layer is off, Markov only
        out["live"].update(laya_shadow=False, laya_rank="shadow", predictor="markov")
    return out


def _laya(raw) -> dict:
    """The W13 keys of `live`, each falling back to its default when absent or wrongly typed."""
    out = dict(LIVE_LAYA_DEFAULTS)
    if not isinstance(raw, dict):
        return out
    if isinstance(raw.get("laya_shadow"), bool):
        out["laya_shadow"] = raw["laya_shadow"]
    if raw.get("predictor") in PREDICTORS:
        out["predictor"] = raw["predictor"]
    if raw.get("laya_rank") in LAYA_RANKS:
        out["laya_rank"] = raw["laya_rank"]
    elif out["predictor"] in ("laya", "blend"):
        out["laya_rank"] = {"laya": "live", "blend": "blend"}[out["predictor"]]
    for k in ("laya_url", "laya_checkpoint", "laya_model"):
        if isinstance(raw.get(k), str) and raw[k].strip():
            out[k] = raw[k].strip()
    v = raw.get("laya_timeout_ms")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 and v not in LEGACY_LAYA_TIMEOUTS_MS:
        out["laya_timeout_ms"] = max(100, min(5000, int(v)))      # sane range: 100..5000 ms
    return out


def update_live(changes: dict, path: Path | None = None) -> dict:
    """Read-modify-write of settings.json `live` (other keys kept as they are), atomic (temp + rename),
    mode 0600. Used only by `oi laya-promote` (W13). Returns the raw `live` dict written."""
    return _update_section("live", changes, path)


def set_laya_enabled(on: bool, path: Path | None = None) -> dict:
    """The menu-bar Laya switch: settings.json `laya.enabled` (other keys kept). Returns the raw `laya` dict."""
    return _update_section("laya", {"enabled": bool(on)}, path)


def _update_section(section: str, changes: dict, path: Path | None = None) -> dict:
    import os
    p = path or settings_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        raw = {}
    live = raw.get(section) if isinstance(raw.get(section), dict) else {}
    live.update(changes)
    raw[section] = live
    paths.ensure_private_root()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, p)
    return live


def _numbers(raw, defaults: dict) -> dict:
    """defaults overlaid with the numeric (>= 0) keys of `raw` that the defaults know."""
    out = dict(defaults)
    if isinstance(raw, dict):
        for k in defaults:
            v = raw.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                out[k] = int(v) if k in _INT_KEYS else v
    return out


def denylist_apps(path: Path | None = None) -> list[str]:
    return load(path)["denylist_apps"]
