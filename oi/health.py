"""Health of OS-intent in production: failures must be visible, not silent.

  SensorHealth   the proactive engine's per-source poll record, written to <data dir>/health/sensors.json
                 (atomic, at most every FLUSH_S): {last_ok_ts, last_error_ts, last_error_kind, consecutive_failures,
                 polls_ok_24h, polls_failed_24h, info_kind} per enabled source. An error kind is a short code
                 (osa_timeout, osa_error, bad_output, exception:<Type>), never a name, a subject or a path.
                 INFO_KINDS (`not_running`: Mail / Telegram closed, the user's choice) are NOT failures: `info_kind`
                 is set, no failure counted, last_ok untouched; `oi health` prints them as info (« mail: Mail fermé »).
  `oi health`    that file + the LaunchAgents (launchctl list com.osintent.oi-*) + ERROR / Traceback lines of the last
                 24 h in <data dir>/logs/*.log + a compile check (osacompile, nothing runs) of every embedded
                 AppleScript / JXA constant + the Laya layer (check_laya). One line per problem (counts and codes
                 only). Exit 0 healthy, 1 something unhealthy, 2 cannot check.
  Laya           only when it is on (`live.laya_shadow`, or `laya_rank` blend|live). Problems: server down (unless the
                 memory rule refused it recently), no model loaded, and over the last LAYA_LAST_N shows with a Laya
                 result (24 h): timeout rate > 10 %, or other errors > 10 %. A recent memory-rule refusal (status
                 oi-laya.json `refused`, < LAYA_REFUSED_INFO_S old) is info: the rule protecting the Mac, retried by
                 oi-live every 10 min; if it lasts, the error rate makes it a problem. The LaunchAgent is lazy
                 (RunAtLoad false): "not running" is not a service problem, "not loaded" still is.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import laya, paths, settings

FLUSH_S = 60
DAY_S = 86400
STALE_S = 300                 # an engine that wrote nothing for 5 min (it flushes every minute) is not running
FAILS_UNHEALTHY = 3           # consecutive failed polls of a source before it is a problem
GRACE_S = 600                 # a source with no successful poll 10 min after the engine started is a problem
SERVICES = ("oi-live", "oi-hud", "oi-watcher", "oi-laya")
SCRIPT_MODULES = ("oi.calendar_slots", "oi.claude_actions", "oi.drill", "oi.onboard", "oi.onboard_agent.readers", "oi.proactive.sensors")
JS_MARK = "function run("
AS_MARKS = ("on run argv", "tell application")
_TS = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
_ERR = re.compile(r"ERROR|Traceback")
INFO_KINDS = frozenset({"not_running", "watcher_not_running"})   # the app closed (the user's choice) / oi-watcher down
                                                                 # (counted once, as the oi-watcher service): never a sensor failure
LAZY_SERVICES = frozenset({"oi-laya"})        # started on demand by oi-live: its state is check_laya's
LAYA_LAST_N = 50              # the last N shows with a Laya result (24 h)
LAYA_MIN_N = 10               # fewer results: no rate verdict
LAYA_RATE_UNHEALTHY = 0.10    # timeout (or other error) rate above this = a problem
LAYA_REFUSED_INFO_S = 1800    # a memory-rule refusal younger than this is info, not a problem
INFO_TEXT = {("mail", "not_running"): "Mail fermé", ("agents", "not_running"): "Telegram fermé",
             ("files", "watcher_not_running"): "oi-watcher arrêté"}


def health_path() -> Path:
    return paths.data_dir() / "health" / "sensors.json"


def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SensorHealth:
    """record(src, now, error_kind | None) per poll; flush(now) writes the file at most every FLUSH_S."""

    def __init__(self, path: Callable[[], Path] = health_path):
        self.path = path
        self.src: dict[str, dict] = {}
        self.hours: dict[str, dict[int, list[int]]] = {}       # src -> {hour: [ok, failed]} (the last 24 h)
        self.flushed: float | None = None
        self.started: float | None = None                       # the first poll / flush of this engine run

    def record(self, src: str, now: float, error: str | None) -> None:
        self.started = now if self.started is None else self.started
        s = self.src.setdefault(src, {"last_ok_ts": None, "last_error_ts": None, "last_error_kind": None,
                                      "consecutive_failures": 0, "info_kind": None})
        if error in INFO_KINDS:                                 # the app closed: info, no failure, last_ok kept
            s["info_kind"] = error
            return
        s["info_kind"] = None
        h = self.hours.setdefault(src, {})
        b = h.setdefault(int(now // 3600), [0, 0])
        if error:
            s.update(last_error_ts=now, last_error_kind=str(error)[:40], consecutive_failures=s["consecutive_failures"] + 1)
            b[1] += 1
        else:
            s.update(last_ok_ts=now, consecutive_failures=0)
            b[0] += 1
        for k in [k for k in h if k <= int(now // 3600) - 24]:
            del h[k]

    def snapshot(self, now: float, sources: list[str] | None = None, paused: bool = False) -> dict:
        out = {}
        for src in (sources if sources is not None else sorted(self.src)):
            s = self.src.get(src) or {"last_ok_ts": None, "last_error_ts": None, "last_error_kind": None,
                                      "consecutive_failures": 0}
            h = [v for k, v in (self.hours.get(src) or {}).items() if k > int(now // 3600) - 24]
            out[src] = {"last_ok_ts": _iso(s["last_ok_ts"]), "last_error_ts": _iso(s["last_error_ts"]),
                        "last_error_kind": s["last_error_kind"], "consecutive_failures": s["consecutive_failures"],
                        "polls_ok_24h": sum(v[0] for v in h), "polls_failed_24h": sum(v[1] for v in h),
                        "info_kind": s.get("info_kind")}
        return {"updated_ts": _iso(now), "started_ts": _iso(self.started), "paused": paused, "sources": out}

    def flush(self, now: float, sources: list[str] | None = None, paused: bool = False, force: bool = False) -> bool:
        if not force and self.flushed is not None and now - self.flushed < FLUSH_S:
            return False
        self.flushed = now
        self.started = now if self.started is None else self.started
        tmp = None
        try:
            p = self.path()
            paths.ensure_private_root()
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".sensors.json.")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(self.snapshot(now, sources, paused)) + "\n")
            os.replace(tmp, p)
            tmp = None
            return True
        except Exception:
            return False                                       # health never breaks the engine
        finally:
            if tmp is not None:                                # any failure: no temp file left behind
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


# ---- checks -------------------------------------------------------------------------------------------------

def embedded_scripts() -> list[tuple[str, str, str]]:
    """[(module.CONSTANT, "AppleScript" | "JavaScript", source)] of every upper-case module string holding a script."""
    out = []
    for name in SCRIPT_MODULES:
        mod = importlib.import_module(name)
        for k, v in sorted(vars(mod).items()):
            if not (k.isupper() and isinstance(v, str)):
                continue
            if JS_MARK in v:
                out.append((f"{name}.{k}", "JavaScript", v))
            elif any(m in v for m in AS_MARKS):
                out.append((f"{name}.{k}", "AppleScript", v))
    return out


def compile_script(lang: str, src: str) -> tuple[bool, str]:
    """osacompile only (nothing runs). (ok, osacompile's error line)."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / ("s.js" if lang == "JavaScript" else "s.applescript")
        f.write_text(src, encoding="utf-8")
        p = subprocess.run(["osacompile", "-l", lang, "-o", str(Path(d) / "s.scpt"), str(f)],
                           capture_output=True, text=True, timeout=30)
        return p.returncode == 0, (p.stderr or "").strip().replace(str(f), "<script>")


def check_scripts(compile_: Callable = compile_script) -> list[str]:
    return [f"script {n} does not compile" for n, lang, src in embedded_scripts() if not compile_(lang, src)[0]]


def launchctl_list(run: Callable = subprocess.run) -> dict[str, tuple[str, str]]:
    """{label: (pid, last exit status)} of `launchctl list`."""
    p = run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
    if p.returncode != 0:
        raise OSError("launchctl list failed")
    out = {}
    for line in (p.stdout or "").splitlines():
        f = line.split("\t")
        if len(f) == 3:
            out[f[2].strip()] = (f[0].strip(), f[1].strip())
    return out


def check_services(listing: dict[str, tuple[str, str]]) -> list[str]:
    probs = []
    for s in SERVICES:
        got = listing.get(f"com.osintent.{s}")
        if got is None:
            probs.append(f"service {s}: not loaded")
        elif not got[0].isdigit() and s not in LAZY_SERVICES:
            probs.append(f"service {s}: not running (last exit {got[1]})")
    return probs


def log_errors(log_dir: Path, now: float) -> dict[str, int]:
    """{log file: ERROR / Traceback lines of the last 24 h}. A line without a timestamp takes the last one seen
    above it (a traceback's lines); before any, the file's mtime decides."""
    out = {}
    for f in sorted(log_dir.glob("*.log")):
        try:
            mtime = f.stat().st_mtime
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        recent = now - mtime < DAY_S
        n = 0
        for line in lines:
            m = _TS.search(line[:60])
            if m:
                try:
                    t = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc).timestamp()
                    recent = now - t < DAY_S
                except ValueError:
                    pass
            if recent and _ERR.search(line):
                n += 1
        out[f.name] = n
    return out


def laya_switch_on() -> bool:
    """The menu-bar Laya switch (settings `laya.enabled`). Off: oi-laya stopped on purpose, info not a failure."""
    from . import settings
    return (settings.load().get("laya") or {}).get("enabled", True) is not False


def check_sensors(data: dict | None, enabled: list[str], now: float) -> list[str]:
    if not enabled:
        return []
    if data is None:
        return ["sensors: no health file (proactive engine not running?)"]
    probs = []
    try:
        upd = datetime.fromisoformat(str(data.get("updated_ts")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        upd = 0.0
    if now - upd > STALE_S:
        probs.append(f"sensors: health file not updated for {int((now - upd) // 60)} min")
    try:
        started = datetime.fromisoformat(str(data.get("started_ts")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        started = None                                         # an older file: no grace information
    for src in enabled:
        s = (data.get("sources") or {}).get(src)
        if s is None:
            probs.append(f"sensor {src}: never polled")
        elif s.get("info_kind") in INFO_KINDS:
            continue                                           # closed by the user: info (check_info), not a problem
        elif int(s.get("consecutive_failures") or 0) >= FAILS_UNHEALTHY:
            probs.append(f"sensor {src}: {s['consecutive_failures']} failed polls in a row ({s.get('last_error_kind')})")
        elif s.get("polls_failed_24h") and not s.get("polls_ok_24h"):
            probs.append(f"sensor {src}: no successful poll in 24 h ({s.get('last_error_kind')})")
        elif s.get("last_ok_ts") is None and started is not None and now - started >= GRACE_S:
            probs.append(f"sensor {src}: no successful poll since the engine started {int((now - started) // 60)} min ago"
                         + (f" ({s.get('last_error_kind')})" if s.get("last_error_kind") else ""))
    return probs


def check_info(data: dict | None, enabled: list[str], laya_on: bool | None = None) -> list[str]:
    """The non-failing states, one line each: « mail: Mail fermé »."""
    out = [] if (laya_switch_on() if laya_on is None else laya_on) else ["laya: Laya désactivé"]
    for src in enabled:
        k = ((data or {}).get("sources") or {}).get(src, {}).get("info_kind")
        if k in INFO_KINDS:
            out.append(f"{src}: {INFO_TEXT.get((src, k), k)}")
    return out


def laya_rows(hud_dir: Path, now: float, n: int = LAYA_LAST_N) -> list[dict]:
    """The `laya` results ({probs..} or {error..}) of the last n hud-log rows of the last 24 h that have one."""
    out: list[dict] = []
    for f in sorted(hud_dir.glob("????-??-??.jsonl"))[-2:]:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                r = json.loads(line)
                t = datetime.fromisoformat(str(r.get("ts")).replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                continue
            if isinstance(r.get("laya"), dict) and now - DAY_S < t <= now + 60:
                out.append(r["laya"])
    return out[-n:]


def laya_enabled(live: dict) -> bool:
    return bool(live.get("laya_shadow")) or live.get("laya_rank") in ("blend", "live")


def check_laya(live: dict, info: dict | None, status: dict | None, rows: list[dict], now: float
               ) -> tuple[list[str], list[str], dict]:
    """(problems, info lines, summary) of the Laya layer. `info` = /health JSON (None = down), `status` = the
    supervisor's oi-laya.json, `rows` = laya_rows()."""
    if not laya_enabled(live):
        return [], [], {"enabled": False}
    probs, notes = [], []
    st = status or {}
    refused = st.get("state") == "refused" and now - float(st.get("ts") or 0) < LAYA_REFUSED_INFO_S
    if refused:
        notes.append("laya: refused by the memory rule (" + str(st.get("reason") or "").split(";")[0][:60] + ")")
    if info is None:
        if not refused:
            probs.append(f"laya: server down (last state {st.get('state') or 'unknown'})")
    elif not info.get("loaded"):
        probs.append("laya: server up, no model loaded")
    n = len(rows)
    kinds: dict[str, int] = {}
    for r in rows:
        if r.get("error"):
            kinds[str(r["error"])[:20]] = kinds.get(str(r["error"])[:20], 0) + 1
    timeouts = kinds.pop("timeout", 0)
    if refused:
        kinds.pop("unreachable", None)                        # expected while the memory rule holds it down
    others = sum(kinds.values())
    if n >= LAYA_MIN_N:
        if timeouts / n > LAYA_RATE_UNHEALTHY:
            probs.append(f"laya: {timeouts}/{n} timeouts in the last {n} shows ({round(100 * timeouts / n)} %)")
        if others / n > LAYA_RATE_UNHEALTHY:
            probs.append(f"laya: {others}/{n} errors in the last {n} shows ("
                         + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) + ")")
    ok = [r["latency_ms"] for r in rows if not r.get("error") and isinstance(r.get("latency_ms"), (int, float))]
    return probs, notes, {"enabled": True, "up": info is not None, "loaded": (info or {}).get("loaded"),
                          "state": st.get("state"), "shows": n, "timeouts": timeouts, "other_errors": others,
                          "p50_ms": sorted(ok)[len(ok) // 2] if ok else None,
                          "timeout_ms": live.get("laya_timeout_ms")}


def _laya_status() -> dict | None:
    try:
        return json.loads(laya.status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_checks(now: float | None = None, *, listing: Callable[[], dict] = launchctl_list,
               compile_: Callable = compile_script, enabled: Callable[[], list[str]] | None = None,
               laya_probe: Callable[[str], dict | None] = laya.health_info) -> dict:
    now = time.time() if now is None else now
    if enabled is None:
        def enabled():
            from .proactive.config import enabled_sources, proactive_settings
            from .proactive.engine import _raw_settings
            return list(enabled_sources(proactive_settings(_raw_settings())) or [])
    try:
        data = json.loads(health_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    srcs = enabled()
    logs = log_errors(paths.data_dir() / "logs", now)
    services = listing()
    probs = check_sensors(data, srcs, now) + [p for p in check_services(services)       # Laya off: stopped on purpose
                                              if laya_switch_on() or not p.startswith("service oi-laya:")]
    probs += [f"log {k}: {n} ERROR/Traceback lines in 24 h" for k, n in logs.items() if n]
    scripts = check_scripts(compile_)
    probs += scripts
    live = settings.load()["live"]
    lprobs, linfo, lsum = check_laya(live, laya_probe(live.get("laya_url") or laya.DEFAULT_URL) if laya_enabled(live)
                                     else None, _laya_status(), laya_rows(paths.hud_log_dir(), now), now)
    probs += lprobs
    return {"healthy": not probs, "problems": probs, "info": check_info(data, srcs) + linfo, "laya": lsum,
            "sensors": (data or {}).get("sources") or {},
            "enabled_sources": srcs, "services": {s: services.get(f"com.osintent.{s}") for s in SERVICES},
            "log_errors_24h": logs, "scripts_checked": len(embedded_scripts()), "scripts_failed": len(scripts)}


def cmd_health(args) -> int:
    if sys.platform != "darwin" or not shutil.which("launchctl") or not shutil.which("osacompile"):
        print("cannot check: macOS with launchctl and osacompile needed")
        return 2
    try:
        r = run_checks()
    except Exception as e:                                     # a check that cannot run: 2, never a traceback
        print(f"cannot check: {type(e).__name__}")
        return 2
    if args.json:
        print(json.dumps(r, ensure_ascii=False))
    else:
        for p in r["problems"]:
            print(p)
        for i in r.get("info") or []:
            print(f"info: {i}")
        if r["healthy"]:
            print(f"healthy: {len(r['enabled_sources'])} sensors, {len(SERVICES)} services, "
                  f"{r['scripts_checked']} scripts")
    return 0 if r["healthy"] else 1


def register(sub) -> None:
    p = sub.add_parser("health", help="production health: sensors, LaunchAgents, log errors, script compile (exit 0/1/2)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_health)
