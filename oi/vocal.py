"""`oi vocal-send` (W12): deliver a "Send a vocal to…" transcript to ONE fleet agent's inbox.

oi-hud records the voice note (on-device dictation, audio kept in vocals/<ts>-<slug>.caf, 0600), writes the
transcript next to it (.txt) and runs `oi vocal-send --to <slug> --text-file <f> [--audio <caf>] [--app A]
[--desk k]`. This module turns it into ONE line and hands it to the agent's inbox:

  kind "inject": append the line to a LOCAL inject file (the agent's telegram plugin drains it as a turn);
                 the file must exist (a missing file = no live session: failure, nothing created);
  kind "a2a":    a configurable RELAY command you provide (settings `script`, default $OI_RELAY_CMD or
                 ~/.local/bin/agent-send; a .py script runs with python3), called as
                 `<relay> --to <host alias> --state-dir <the agent's inbox dir there> --from <your slug>`,
                 the line on STDIN. How it gets there (ssh, a signed relay, a queue) is up to that command.

The message never goes in argv, the sender runs with TELEGRAM_STATE_DIR / TELEGRAM_BOT_TOKEN / GH_TOKEN /
GITHUB_TOKEN removed from its env, and nothing but the chosen agent's inbox ever receives it (no cloud call).
`--dry-run` prints what would run (command + line) and sends nothing.

Line: "[Vocal from the user, <Paris HH:MM>, context: <app> · <host> · desk <k>] <transcript> (audio: <path>)"; with `--typed` the head reads "[Message typed by the user, …]"
so the recipient never takes typed words for a transcript. The audio suffix only for a
local inject recipient (atlas, juno): an A2A recipient on another host cannot open a path on this Mac.
Context: app and desk from oi-hud (the moment the vocal key was pressed), the page host from oi live's status
(status/oi-live.json `context`, when fresh); never a window title or a URL path.

Recipients: `DEFAULT_RECIPIENTS`, overridden per slug by settings.json `vocal_recipients`
({"nova": {"kind": "a2a", "script": "...", "to": "...", "state_dir": "..."}, "x": null} removes x).
Every attempt (sent or not, dry run included) is one hud-log row {"kind": "vocal", "to", "ok", ...}; a sent
one becomes the token `vocal:<slug>` (oi.normalise).

Long vocals:
oi-hud records up to settings `vocal.max_s` (default 300 s, a countdown in the last 15 s). The transcript is
on-device, already cut in recogniser segments by oi-hud (W20), so this side only sees text: `MAX_CHARS` fits a
5-minute dictation. The line goes on the relay command's STDIN (never in argv, so no argument-length cap) or
into the inject file in ONE write.

Screenshot offer (same vocal, "ce serait bien que ça propose d'envoyer un screenshot"): `oi vocal-send` (the
HUD's vocal:<slug> path, `offer_shot`) asks the outward gate with the option "Joindre une capture de l'écran",
OFF by default, one key (C) or a click to include. It is offered only when a capture is safe now (`shot_offer_ok`:
settings `vocal.shot_offer` and `shots.enabled` on, not paused, a fresh oi live context, the front app not
denylisted, the page host not in `claude.local_only_domains`, and not a browser with no known page: a private
window gives no host). Chosen + validated = ONE `screencapture -x` of the display under the pointer at send time
(checked again then: a context that turned private sends the vocal alone), stored next to the audio
(vocals/<stem>.png, 0600), and named in the SAME line: "(screenshot: <inbox path>)" for an inject recipient (the
image copied into its channel inbox, as oi.shots tier 1 does), "(screenshot on the user's Mac at <path>)" for an A2A
one. Never captured without that explicit choice, never in a dry run, never uploaded.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import gate, paths, settings

RELAY_CMD = os.path.expanduser(os.environ.get("OI_RELAY_CMD", "~/.local/bin/agent-send"))   # your relay command
FROM = "atlas"
SCRUB_ENV = ("TELEGRAM_STATE_DIR", "TELEGRAM_BOT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
TIMEOUT_S = 30
MAX_CHARS = 16000          # a 5-minute French dictation is ~5000 chars (vocal.max_s up to 900 s)
STATUS_FRESH_S = 15
PARIS = ZoneInfo("Europe/Paris")
# A browser page with no host read now is offered a capture only when the last host of that browser is one of these
# (or a subdomain), set in settings `vocal.shot_safe_hosts` (e.g. your dashboard and your own sites). Empty = never.
VOCAL_DEFAULTS = {"shot_offer": True, "shot_safe_hosts": []}
BROWSERS = frozenset({"safari", "safari technology preview", "google chrome", "chrome", "chromium", "firefox", "arc",
                      "brave browser", "microsoft edge", "opera", "vivaldi", "orion", "zen", "dia"})
SHOT_SETTLE_S = 0.35       # the confirm panel fades out (Metrics.appear) before the capture
CAPTURE_TIMEOUT_S = 10
_SLUG = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def _remote(slug: str, name: str, host: str) -> dict:
    """An agent on another host, reached through the relay command (example values)."""
    return {"name": name, "kind": "a2a", "script": RELAY_CMD, "to": host, "state_dir": f"/opt/agents/{slug}/inbox"}


# EXAMPLE fleet: replace it with your own agents in settings.json `vocal_recipients` (a slug set to null removes one).
DEFAULT_RECIPIENTS: dict[str, dict] = {
    "atlas": {"name": "Atlas", "kind": "inject", "path": os.path.expanduser("~/.claude/channels/atlas/inject")},
    "juno": {"name": "Juno", "kind": "inject", "path": os.path.expanduser("~/.claude/channels/juno/inject")},
    "nova": _remote("nova", "Nova", "agent-host"),
    "orion": _remote("orion", "Orion", "agent-host"),
    "lyra": _remote("lyra", "Lyra", "agent-host"),
    "iris": _remote("iris", "Iris", "agent-host"),
    "vega": _remote("vega", "Vega", "studio-host"),
    "hera": _remote("hera", "Hera", "laptop-host"),
    "echo": _remote("echo", "Echo", "laptop-host"),
}


class VocalError(ValueError):
    pass


def _valid(spec) -> bool:
    if not isinstance(spec, dict):
        return False
    if spec.get("kind") == "inject":
        return isinstance(spec.get("path"), str) and spec["path"].startswith("/")
    if spec.get("kind") == "a2a":
        return all(isinstance(spec.get(k), str) and spec[k] for k in ("script", "to", "state_dir")) \
            and spec["script"].startswith("/") and spec["state_dir"].startswith("/")
    return False


def recipients(raw_settings: dict | None = None) -> dict[str, dict]:
    """The defaults, overridden per slug by settings.json `vocal_recipients` (an invalid entry is ignored,
    null removes the slug)."""
    out = {k: dict(v) for k, v in DEFAULT_RECIPIENTS.items()}
    over = (raw_settings or {}).get("vocal_recipients")
    if isinstance(over, dict):
        for slug, spec in over.items():
            if not isinstance(slug, str) or not _SLUG.match(slug):
                continue
            if spec is None:
                out.pop(slug, None)
            elif _valid(spec):
                out[slug] = {"name": spec.get("name") or out.get(slug, {}).get("name") or slug.title(), **spec}
    return out


def command(spec: dict, sender: str = FROM) -> list[str] | None:
    """argv of the relay command for an "a2a" recipient (the line goes on its stdin); None for "inject"."""
    if spec["kind"] != "a2a":
        return None
    script = spec["script"]
    head = ["python3", script] if script.endswith(".py") else [script]
    return head + ["--to", spec["to"], "--state-dir", spec["state_dir"], "--from", sender]


def clean(text: str | None) -> str | None:
    """One line (one inject line = one turn), trimmed, capped; None when empty: an empty vocal is never sent."""
    if not isinstance(text, str):
        return None
    one = " ".join(text.split())
    return one[:MAX_CHARS] or None


def live_context(now: float | None = None) -> dict:
    """oi live's current-moment context (status/oi-live.json `context`) when the status is fresh, else {}."""
    try:
        st = json.loads((paths.status_dir() / "oi-live.json").read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(st["ts"].replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    if (time.time() if now is None else now) - ts > STATUS_FRESH_S:
        return {}
    ctx = st.get("context")
    return ctx if isinstance(ctx, dict) else {}


def payload(text: str, *, app: str | None, host: str | None, desk: int | None, when: datetime,
            audio: str | None = None, image: str | None = None, typed: bool = False) -> str:
    """`image` = the screenshot reference, already worded ("(screenshot: <path>)"); `typed` = typed / edited by hand
    in the HUD panel (not only a transcript)."""
    hhmm = when.astimezone(PARIS).strftime("%H:%M")
    parts = [p for p in (app, host, f"desk {desk}" if desk is not None else None) if p]
    head = f"[{'Message typed by the user' if typed else 'Vocal from the user'}, {hhmm}" + (f", context: {' · '.join(parts)}" if parts else "") + "]"
    line = f"{head} {text}" + (f" (audio: {audio})" if audio else "") + (f" {image}" if image else "")
    return " ".join(line.split())


def vocal_settings(raw: dict | None) -> dict:
    """settings.json `vocal`, python's keys (wrongly typed = default): {"shot_offer": true, "shot_safe_hosts":
    ["host", ...]}. Its `max_s` (the recording limit, default 300, clamped 15-900) belongs to oi-hud
    (OISettings.vocalMaxS)."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in VOCAL_DEFAULTS.items()}
    v = (raw or {}).get("vocal")
    if isinstance(v, dict):
        if isinstance(v.get("shot_offer"), bool):
            out["shot_offer"] = v["shot_offer"]
        if isinstance(v.get("shot_safe_hosts"), list):
            out["shot_safe_hosts"] = [h.strip().lower().removeprefix("www.") for h in v["shot_safe_hosts"]
                                      if isinstance(h, str) and h.strip()]
    return out


def safe_host(host: str | None, raw: dict | None) -> bool:
    """`host` (port dropped) is (a subdomain of) a `vocal.shot_safe_hosts` entry."""
    h = (host or "").split(":", 1)[0].strip().lower().removeprefix("www.")
    return bool(h) and any(h == x or h.endswith("." + x) for x in vocal_settings(raw)["shot_safe_hosts"])


def shot_offer_ok(raw: dict | None, lc: dict, app: str | None) -> tuple[bool, str]:
    """(may the gate offer "Joindre une capture de l'écran" now, why not). Fail-safe: any doubt = no offer.
    `lc` = oi live's fresh context ({} when stale, afk or a denylisted app was in front), `app` = oi-hud's.
    A browser with no page host now (back on the same tab, nothing new logged) is offered only when oi live's
    `last_host` for that browser is a safe host; else "private_or_unknown_page" (a private window has no host)."""
    from . import claude_actions, normalise
    from .shots import shot_settings
    raw = raw or {}
    if not vocal_settings(raw)["shot_offer"]:
        return False, "setting"
    if raw.get("paused") is True:
        return False, "paused"
    if not shot_settings(raw)["enabled"]:
        return False, "shots_off"
    if not lc:
        return False, "no_context"
    deny = normalise.effective_denylist()
    for a in (app, lc.get("app")):
        if isinstance(a, str) and a and normalise._denied(a, deny):
            return False, "denylisted"
    host = lc.get("host") if isinstance(lc.get("host"), str) else None
    front = (app or lc.get("app") or "")
    browser = isinstance(front, str) and front.casefold() in BROWSERS
    last = lc.get("last_host") if isinstance(lc.get("last_host"), str) else None
    same = isinstance(lc.get("app"), str) and lc["app"].casefold() == str(front).casefold()
    if browser and not host and same and safe_host(last, raw):
        host = last
    if host and claude_actions.local_only(host.split(":", 1)[0], claude_actions.claude_settings()):
        return False, "local_only"
    if browser and not host:
        return False, "private_or_unknown_page"
    return True, ""


def shot_path(audio: str | None, slug: str, when: datetime) -> Path:
    """vocals/<stem>.png next to the audio, else a new stem in the vocals dir (0700)."""
    if audio:
        p = Path(audio).with_suffix(".png")
    else:
        p = paths.data_dir() / "vocals" / f"{when.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{slug}.png"
    paths.ensure_private_root()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def capture_screen(dst: Path, display: int | None) -> bool:
    """ONE `screencapture -x` (no sound) of `display` (1-based, the one under the pointer), else the main
    display, to `dst` (0600). True when the image exists."""
    time.sleep(SHOT_SETTLE_S)
    argv = ["screencapture", "-x", "-t", "png"] + (["-D", str(display)] if display else ["-m"]) + [str(dst)]
    try:
        subprocess.run(argv, capture_output=True, timeout=CAPTURE_TIMEOUT_S, stdin=subprocess.DEVNULL,
                       preexec_fn=lambda: os.umask(0o077))
    except (OSError, subprocess.TimeoutExpired):
        return False
    if not dst.is_file() or dst.stat().st_size == 0:
        return False
    dst.chmod(0o600)
    return True


def _run(argv: list[str], data: bytes) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    try:
        p = subprocess.run(argv, input=data, capture_output=True, timeout=TIMEOUT_S, env=env)
    except FileNotFoundError:
        return 127, "sender not found"
    except subprocess.TimeoutExpired:
        return 124, f"timeout {TIMEOUT_S}s"
    err = p.stderr.decode("utf-8", "replace").strip().splitlines()
    return p.returncode, (err[-1][:120] if err else "")


def _append_inject(path: str, line: str) -> None:
    p = Path(path)
    if not p.is_file():
        raise VocalError("no live session (inject file missing)")
    data = (line + "\n").encode("utf-8")
    fd = os.open(p, os.O_WRONLY | os.O_APPEND)          # never created here
    try:
        n = os.write(fd, data)                          # ONE write: a long vocal is never drained half-written
        while n < len(data):
            n += os.write(fd, data[n:])
    finally:
        os.close(fd)


def log_row(row: dict) -> None:
    """hud-log/<UTC day>.jsonl, appended 0600 (one short write: safe next to the live driver's appends)."""
    paths.ensure_private_root()
    path = paths.hud_log_dir() / f"{row['ts'][:10]}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def send(slug: str, text: str | None, *, app: str | None = None, desk: int | None = None,
         audio: str | None = None, dry_run: bool = False, now: float | None = None,
         runner: Callable[[list[str], bytes], tuple[int, str]] = _run,
         inject: Callable[[str, str], None] = _append_inject, out=None,
         raw_settings: dict | None = None, action_key: str | None = None,
         ask: Callable[[dict, float], dict] | None = None, offer_shot: bool = False,
         capture: Callable[[Path, int | None], bool] = capture_screen,
         copy: Callable[[str, Path], None] | None = None, typed: bool = False) -> dict:
    """Deliver one vocal; returns {"ok", "to", "name", ["error"], ["dry_run"], ["shot"]} and logs one hud-log row.
    Outward gate: nothing goes out (dry run included) unless the user validates oi.gate's confirm panel.
    `offer_shot` (the HUD vocal path): the panel offers a screenshot when `shot_offer_ok`; captured only if chosen."""
    fixed_now = now is not None
    now = time.time() if now is None else now
    raw = raw_settings if raw_settings is not None else _raw_settings()
    table = recipients(raw)
    spec = table.get(slug)
    result: dict = {"ok": False, "to": slug}
    body = clean(text)
    line = gate_id = None
    offered = attached = False
    shot_note = image = None
    if spec is None:
        result["error"] = f"unknown recipient {slug}"
    elif body is None:
        result["error"] = "empty transcript"
    else:
        result["name"] = spec["name"]
        lc = live_context(now)
        host = lc.get("host") if isinstance(lc.get("host"), str) else None
        if desk is None and isinstance(lc.get("desk"), int):
            desk = lc["desk"]
        app = app or (lc.get("app") if isinstance(lc.get("app"), str) else None)
        when = datetime.fromtimestamp(now, timezone.utc)
        argv = command(spec)
        offered, why = shot_offer_ok(raw, lc, app) if offer_shot else (False, None)
        g = gate.outward("vocal", slug, spec["name"], body, action_key=action_key, dry_run=dry_run, ask=ask, raw=raw,
                         offer_shot=offered, shot_reason=why or None)
        gate_id = g["id"]
        image_src = None
        if g["ok"] and offered and g.get("attach_shot"):
            if dry_run:
                shot_note = "dry_run"                         # never captured in a dry run
            elif not shot_offer_ok(raw, live_context(now if fixed_now else None), app)[0]:
                shot_note = "skipped"                         # the context turned private while the panel was up
            else:
                try:
                    dst = shot_path(audio, slug, when)
                except OSError:
                    dst = None
                if dst is not None and capture(dst, g.get("display")):
                    image_src, attached = str(dst), True
                else:
                    shot_note = "capture_failed"
        if image_src and spec["kind"] == "inject" and not dry_run:
            from .shots import _copy_private, inbox_target
            dst_inbox = inbox_target(spec, image_src, when)
            if Path(spec["path"]).is_file():
                try:
                    (copy or _copy_private)(image_src, dst_inbox)
                    image = f"(screenshot: {dst_inbox})"
                except OSError:
                    attached, shot_note = False, "copy_failed"
        elif image_src:
            image = f"(screenshot on the user's Mac at {image_src})"   # a remote agent: the path, never the pixels
        line = payload(body, app=app, host=host, desk=desk, when=when,
                       audio=audio if spec["kind"] == "inject" else None,   # a remote agent cannot open a Mac path
                       image=image, typed=typed)
        if not g["ok"]:
            result["error"] = g["error"]
        elif dry_run:
            result.update(ok=True, dry_run=True)
            plan = {"kind": spec["kind"], "env_unset": list(SCRUB_ENV), "stdin": line}
            plan.update({"argv": argv} if argv else {"append_to": spec["path"]})
            if shot_note == "dry_run":
                plan["screenshot"] = f"would capture display {g.get('display') or 'main'}"
            print(json.dumps(plan, ensure_ascii=False), file=out or sys.stdout)
        elif argv is None:
            try:
                inject(spec["path"], line)
                result["ok"] = True
            except (OSError, VocalError) as e:
                result["error"] = str(e) if isinstance(e, VocalError) else f"inject: {e.strerror or e}"
        else:
            code, err = runner(argv, (line + "\n").encode("utf-8"))
            result["ok"] = code == 0
            if code != 0:
                result["error"] = f"exit {code}" + (f": {err}" if err else "")
    if attached and image and result["ok"]:
        result["shot"] = True
    elif shot_note:
        result["shot_note"] = shot_note
    row = {"ts": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
           "kind": "vocal", "to": slug, "ok": result["ok"], "dry_run": dry_run,
           "text_len": len(body) if body else 0, "audio": audio, "error": result.get("error"), "gate": gate_id,
           "shot_offered": offered, "shot": bool(result.get("shot")), "shot_note": shot_note,
           "typed": typed}
    log_row(row)
    return result


def _raw_settings() -> dict:
    try:
        raw = json.loads(settings.settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def main_send(args) -> int:
    try:
        text = Path(args.text_file).read_text(encoding="utf-8")
    except OSError as e:
        print(json.dumps({"ok": False, "to": args.to, "error": f"text file: {e.strerror}"}))
        return 1
    r = send(args.to, text, app=args.app, desk=args.desk, audio=args.audio, dry_run=args.dry_run,
             action_key=getattr(args, "action_key", None), offer_shot=True,   # the HUD's vocal:<slug> path
             typed=bool(getattr(args, "typed", False)))
    print(json.dumps(r, ensure_ascii=False))     # the LAST line is what oi-hud reads
    return 0 if r["ok"] else 1
