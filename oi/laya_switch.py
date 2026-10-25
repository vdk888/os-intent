"""The menu-bar Laya switch (the user 2026-10-21: « I need a toggle for Laya in the menubar »; Laya, ~3 GB, had not
come back after a reboot and the 16 GB Mac crashed on RAM the same day).

`oi laya-switch on|off|status [--json]`, run by oi-menubar (its « Laya » item) or by hand.
  off     settings.json `laya.enabled` = false, then stop the server: SIGTERM to the supervisor, through
          `launchctl kill SIGTERM gui/<uid>/com.osintent.oi-laya` when that LaunchAgent is loaded, else to the supervisor
          pid of status/oi-laya.json when that pid is an `oi laya-serve` process (ensure_server's detached spawn).
          The agent stays loaded: RunAtLoad and KeepAlive are false, so nothing restarts it, and `launchctl print`
          still works for the menu and `oi health`. While off: settings.load() hands out the Laya layer off (Markov
          only), ensure_server returns 'disabled', the wish resolver never asks Laya, `oi health` prints the info
          « laya: Laya désactivé ». Survives a reboot (it is a setting).
  on      `laya.enabled` = true, then the usual ensure_server path (its memory rule refuses when swap >= 4096 MB,
          < 3 GB available or another heavy ML job runs). ensure_server writes a refusal to status/oi-laya.json (`refused`,
          like the supervisor's own) so the menu shows « refusé : mémoire ». After a reboot oi live starts it once
          at its own start (and, as before, whenever it finds it down, at most every 10 min).
  status  {enabled, state, detail, up}; state = disabled | running | loading | starting | refused | stopped.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.request

from . import laya, settings

STARTING_FRESH_S = 30      # a supervisor `running` row younger than this, server not answering yet: starting


def health_json(url: str, timeout_s: float = 1.0) -> dict | None:
    """The server's /health JSON (laya-serve: {"status", "loaded": [...], "device"}), {} when it answers 200 with
    something else, None when down."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout_s) as r:
            if r.status != 200:
                return None
            try:
                d = json.loads(r.read())
            except ValueError:
                return {}
            return d if isinstance(d, dict) else {}
    except Exception:
        return None


def read_status() -> dict:
    try:
        d = json.loads(laya.status_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def classify(enabled: bool, info: dict | None, st: dict, now: float) -> tuple[str, str]:
    """(state, detail) for the menu. info = /health JSON (None = down), st = status/oi-laya.json."""
    if not enabled:
        return "disabled", "server still answering" if info is not None else ""
    if info is not None:
        loaded = info.get("loaded")
        if isinstance(loaded, list) and not loaded:
            return "loading", "server up, no model loaded yet"
        return "running", ", ".join(map(str, loaded)) if isinstance(loaded, list) else ""
    state = st.get("state")
    if state == "refused":
        return "refused", str(st.get("reason") or "").split(";")[0][:80]
    try:
        age = now - float(st.get("ts") or 0)
    except (TypeError, ValueError):
        age = float("inf")
    if state == "running" and age < STARTING_FRESH_S:
        return "starting", "supervisor up, model loading"
    return "stopped", str(state or "")


def status(*, probe=health_json, now: float | None = None) -> dict:
    cfg = settings.load()
    url = cfg["live"].get("laya_url") or laya.DEFAULT_URL
    info = probe(url)
    state, detail = classify(cfg["laya"]["enabled"], info, read_status(), time.time() if now is None else now)
    return {"enabled": cfg["laya"]["enabled"], "state": state, "detail": detail, "up": info is not None, "url": url}


def _is_laya_serve(pid: int, run=subprocess.run) -> bool:
    try:
        p = run(["/bin/ps", "-p", str(int(pid)), "-o", "command="], capture_output=True, text=True, timeout=5)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and "laya-serve" in (p.stdout or "")


def stop(*, agent_loaded=laya._agent_loaded, run=subprocess.run, kill=os.kill, st: dict | None = None) -> str:
    """SIGTERM to the supervisor (it stops its server and writes `stopped`). Returns what it did, one line."""
    if agent_loaded():
        rc = run(["/bin/launchctl", "kill", "SIGTERM", f"gui/{os.getuid()}/{laya.AGENT}"],
                 capture_output=True, timeout=10).returncode
        if rc == 0:
            return f"launchctl kill SIGTERM {laya.AGENT} -> 0"
    st = read_status() if st is None else st
    pid = st.get("pid") if st.get("state") == "running" else None
    if isinstance(pid, int) and pid > 1 and _is_laya_serve(pid, run):
        try:
            kill(pid, signal.SIGTERM)
            return f"SIGTERM supervisor pid {pid}"
        except ProcessLookupError:
            pass
    return "nothing running"


def switch(on: bool, *, ensure=None, stop_fn=None) -> dict:
    """Persist `laya.enabled`, then stop (off) or ensure_server (on). Returns {enabled, result, detail}."""
    settings.set_laya_enabled(on)
    if not on:
        return {"enabled": False, "result": "stopped", "detail": (stop_fn or stop)()}
    lv = settings.load()["live"]
    state, detail = (ensure or laya.ensure_server)(lv.get("laya_url") or laya.DEFAULT_URL, lv.get("laya_checkpoint"))
    return {"enabled": True, "result": state, "detail": detail}


def cmd_laya_switch(args) -> int:
    r = status() if args.action == "status" else switch(args.action == "on")
    if args.json:
        print(json.dumps(r, ensure_ascii=False))
    else:
        print(" ".join(f"{k}={v}" for k, v in r.items()))
    return 0


def register(sub) -> None:
    p = sub.add_parser("laya-switch", help="the menu-bar Laya switch: on (start, memory rule) | off (stop, Markov only) | status")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_laya_switch)
