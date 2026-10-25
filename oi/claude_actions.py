"""W14: Claude in the app. Long-press = the Claude-assisted variant of a primitive, summaries, label
enrichment. The user: "if i press 'respond to email' option, for longer, it also writes the response
with context in mind".

Runtime budget: Claude ONLY through your Claude subscription (never an API key), as a headless `claude -p` run:
  - auth: $CLAUDE_CODE_OAUTH_TOKEN, or a copied ~/.claude/.credentials.json, both in an ISOLATED
    CLAUDE_CONFIG_DIR (no plugins = no telegram poller); on a keychain-login Mac (no file, no env token) the
    caller's config dir with `--setting-sources project` (no user settings, so no plugins and no hooks) and
    TELEGRAM_STATE_DIR pointed at the temp dir;
  - always `--strict-mcp-config --tools "" --no-session-persistence`, a neutral cwd (the temp dir), the prompt
    on stdin (never argv), and ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / TELEGRAM_* / GH_TOKEN / GITHUB_TOKEN
    removed from the child env: an API key can never be used, the call is the seat or nothing;
  - the output is guarded against auth stubs ("Not logged in", ...); any failure = "Claude unavailable" and the
    plain primitive (never an API fallback).

Actions (a long-press reply `pressed` with "mode": "claude"):
  mail.reply    -> the selected Mail message (+ the previous one of the thread when found) + top-3 local wiki
                   notes (+ the times the mail proposes, free / busy in the local calendars, read-only)
                   -> sonnet drafts the reply in the incoming mail's language -> a Mail reply window opens
                   pre-filled. NEVER sent.
  mail.forward  -> same, a covering note; the recipient is left for the user. NEVER sent.
  url.open / app.open -> open first, then a 3-line haiku summary card (Safari: title + URL + page text when
                   Safari allows JavaScript from Apple Events, else title + URL; another app: its name + window
                   title).
The HUD keeps a spinner while Claude works; the result goes back with the socket command `result`. The HUD
answers `ok` when it still waits (the action is then performed) or `dropped: cancelled` after Esc (nothing is
done, not even the plain primitive).

Label enrichment (design section 6, design B): options marked `enrich: true` in a form get, after a show, a local
label first ("Reply to <first name>") and then a haiku label (one call per mail, cached), sent with the socket
command `update` (applied only while the same option is still on screen). Never blocks the HUD; nothing runs
when no show happened.

Privacy: settings.json `claude.local_only_domains` (default []): mails from those domains (and pages on those
hosts) never go to Claude (plain action only). W15: a mail is local-only when its sender, ANY cc recipient
or its reply-to address is in such a domain (`mail_local_only`). Logged: action, model, latency, ok. The prompt and the output are
never written to disk (no session persistence either) unless `claude.debug` / OI_CLAUDE_DEBUG=1 (0600 files in
<data dir>/claude-debug/).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import calendar_slots, forms, paths

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAIL_PRIMITIVES = ("mail.reply", "mail.forward")
CLAUDE_PRIMITIVES = MAIL_PRIMITIVES + ("url.open", "app.open")
MODELS = {"mail.reply": "sonnet", "mail.forward": "sonnet", "url.open": "sonnet", "app.open": "sonnet",
          "enrich": "sonnet", "check": "haiku", "glide": "sonnet"}   # sonnet everywhere at runtime: (2026-10-20)
TIMEOUT_S = 25
BODY_MAX = 6000
PREV_MAX = 2000
PAGE_MAX = 6000
LABEL_BODY_MAX = 600
LABEL_MAX = 22
CARD_TTL_MS = 8000
DRAFT_CARD_TTL_MS = 2500
DRAFT_SHOW_TTL_MS = 15000          # the draft itself is on the card (read it, « Open in Mail » or leave it)
OSA_TIMEOUT_S = 6
WIKI_TIMEOUT_S = 5
CACHE_SIZE = 64
SCRUB_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_STATE_DIR", "GH_TOKEN", "GITHUB_TOKEN",
             "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
STUB_RE = re.compile(r"not logged in|please run|/login|unauthorized|not authenticated|invalid api|api key"
                     r"|login (required|expired)|credit balance", re.I)
FREE_MAIL = {"gmail.com", "googlemail.com", "hotmail.com", "hotmail.fr", "outlook.com", "outlook.fr", "live.com",
             "live.fr", "yahoo.com", "yahoo.fr", "icloud.com", "me.com", "mac.com", "orange.fr", "free.fr",
             "laposte.net", "sfr.fr", "wanadoo.fr", "proton.me", "protonmail.com", "gmx.fr", "gmx.com"}
SEP = "\x1f"
CLAUDE_DEFAULTS = {"enabled": True, "enrich": True, "local_only_domains": [], "debug": False,
                   "orchestrator": True}   # W25: the orchestrator prompt + context pack on every runtime call


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} oi-claude: {msg}", file=sys.stderr, flush=True)


# ---- settings --------------------------------------------------------------------------------------

def claude_settings(path: Path | None = None) -> dict:
    """settings.json `claude` (every key optional, wrongly typed = default):
    {"enabled": true, "enrich": true, "local_only_domains": ["client.example"], "debug": false, "orchestrator": true}."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in CLAUDE_DEFAULTS.items()}
    try:
        raw = json.loads((path or paths.data_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    c = raw.get("claude") if isinstance(raw, dict) else None
    if isinstance(c, dict):
        for k in ("enabled", "enrich", "debug", "orchestrator"):
            if isinstance(c.get(k), bool):
                out[k] = c[k]
        d = c.get("local_only_domains")
        if isinstance(d, list):
            out["local_only_domains"] = [x.strip().lower().lstrip("@.") for x in d if isinstance(x, str) and x.strip()]
    if os.environ.get("OI_CLAUDE_DEBUG") == "1":
        out["debug"] = True
    return out


def domain_of(address: str | None) -> str:
    a = (address or "").strip().lower()
    return a.rsplit("@", 1)[1].strip(">") if "@" in a else ""


def local_only(domain_or_host: str | None, cfg: dict) -> bool:
    """True when the mail domain / page host is (a subdomain of) a settings.claude.local_only_domains entry."""
    d = (domain_or_host or "").strip().lower().removeprefix("www.")
    return bool(d) and any(d == x or d.endswith("." + x) for x in cfg.get("local_only_domains") or [])


def offers(primitive: str | None, cfg: dict | None = None) -> bool:
    """Whether the HUD may offer the Claude variant (long-press) for this primitive."""
    cfg = cfg if cfg is not None else claude_settings()
    return bool(cfg.get("enabled")) and primitive in CLAUDE_PRIMITIVES


# ---- the headless runner ------------------------------------------------------------------------------

class ClaudeUnavailable(Exception):
    """The subscription call failed (no login, timeout, error, stub, unparsable output)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def claude_bin() -> str:
    return os.environ.get("OI_CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")


def auth_mode(env: dict, home: Path, platform: str = sys.platform) -> str | None:
    """'env' ($CLAUDE_CODE_OAUTH_TOKEN), 'file' (~/.claude/.credentials.json), 'keychain' (macOS login), else None.
    Never an API key."""
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "env"
    cfg = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    if (cfg / ".credentials.json").is_file():
        return "file"
    if platform == "darwin":
        return "keychain"
    return None


ONBOARDED = '{"hasCompletedOnboarding":true,"hasTrustDialogAccepted":true,"bypassPermissionsModeAccepted":true}'
INSTRUCTION = "Follow the instructions in the text below exactly. Answer with JSON only."


def build_command(model: str, mode: str, tmp: Path, env: dict, home: Path, binary: str) -> tuple[list[str], dict]:
    """(argv, child env) for one call. The prompt goes on stdin."""
    child = {k: v for k, v in env.items() if k not in SCRUB_ENV}
    argv = [binary, "-p", INSTRUCTION, "--model", model, "--output-format", "json",
            "--strict-mcp-config", "--tools", "", "--no-session-persistence"]
    if mode in ("env", "file"):
        (tmp / ".claude.json").write_text(ONBOARDED)
        if mode == "file":
            src = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / ".credentials.json"
            dst = tmp / ".credentials.json"
            shutil.copyfile(src, dst)
            dst.chmod(0o600)
        child["CLAUDE_CONFIG_DIR"] = str(tmp)
    else:   # keychain: the caller's config dir (the login is keyed by it), no user settings = no plugins/hooks
        argv += ["--setting-sources", "project"]
        child["TELEGRAM_STATE_DIR"] = str(tmp)
    return argv, child


def extract_json(text: str) -> dict:
    """The JSON object in a model answer (code fences and surrounding words tolerated)."""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < i:
        raise ValueError("no JSON object")
    obj = json.loads(t[i:j + 1])
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    return obj


def parse_output(stdout: str) -> dict:
    """`claude -p --output-format json` stdout -> the JSON object the model answered. ClaudeUnavailable on an
    error result, an auth stub or an unparsable answer."""
    try:
        env = json.loads(stdout)
    except ValueError:
        raise ClaudeUnavailable("stub" if STUB_RE.search(stdout or "") else "bad_output") from None
    if not isinstance(env, dict):
        raise ClaudeUnavailable("bad_output")
    result = env.get("result")
    if env.get("is_error") or not isinstance(result, str) or not result.strip():
        raise ClaudeUnavailable("error_result")
    if STUB_RE.search(result) and "{" not in result:
        raise ClaudeUnavailable("stub")
    try:
        return extract_json(result)
    except ValueError:
        raise ClaudeUnavailable("bad_json") from None


def run_claude(prompt: str, model: str, *, timeout: float = TIMEOUT_S, env: dict | None = None,
               home: Path | None = None, binary: str | None = None) -> dict:
    """One headless call on the subscription. Returns the parsed JSON answer; raises ClaudeUnavailable."""
    env = dict(os.environ if env is None else env)
    home = home or Path.home()
    mode = auth_mode(env, home)
    if mode is None:
        raise ClaudeUnavailable("no_login")
    tmp = Path(tempfile.mkdtemp(prefix="oi-claude-"))
    try:
        tmp.chmod(0o700)
        argv, child = build_command(model, mode, tmp, env, home, binary or claude_bin())
        try:
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 cwd=tmp, env=child, start_new_session=True, text=True)
        except OSError:
            raise ClaudeUnavailable("no_binary") from None
        try:
            out, _ = p.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.communicate()
            raise ClaudeUnavailable("timeout") from None
        if p.returncode != 0:
            raise ClaudeUnavailable("stub" if STUB_RE.search(out or "") else f"rc_{p.returncode}")
        return parse_output(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- prompts ------------------------------------------------------------------------------------------

_SLOT = re.compile(r"\{\{(\w+)\}\}")


def render_prompt(name: str, values: dict, context: dict | None = None) -> str:
    """oi/prompts/<name>.md with {{slots}} filled in ONE pass (a value containing {{x}} stays literal).
    W25: then wrapped by the orchestrator (system prompt + roster + skills + the context pack of
    `context`: app, host, space, mail, mail_gated, chain, agent, query); `check` stays bare."""
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    text = _SLOT.sub(lambda m: str(values.get(m.group(1), "")), text)
    from . import orchestrator
    return orchestrator.wrap(name, text, context)


def relationship(domain: str, wiki_lines: list[str]) -> str:
    if not domain or domain in FREE_MAIL:
        return "personal address (write plainly unless the mail or the notes say it is a client or partner)"
    if wiki_lines:
        return f"business domain {domain}, known in the notes (likely client, prospect or partner)"
    return f"business domain {domain} (client, prospect or partner unless the mail says otherwise)"


def mail_prompt(primitive: str, mail: dict, wiki_lines: list[str], calendar: str = "(none)") -> str:
    """`calendar`: the proposed slots with their free / busy state (calendar_slots), reply prompt only."""
    prev = mail.get("previous") or {}
    values = {
        "sender_name": mail.get("sender_name", ""), "sender_address": mail.get("sender_address", ""),
        "date": mail.get("date", ""), "subject": mail.get("subject", ""),
        "body": (mail.get("body") or "")[:BODY_MAX],
        "previous": (f"From: {prev.get('sender_name', '')}\nDate: {prev.get('date', '')}\n\n"
                     f"{(prev.get('body') or '')[:PREV_MAX]}") if prev else "(none)",
        "wiki": "\n".join(f"- {w}" for w in wiki_lines) or "(none)",
        "relationship": relationship(domain_of(mail.get("sender_address")), wiki_lines),
        "calendar": calendar or "(none)",
    }
    return render_prompt("mail_reply" if primitive == "mail.reply" else "mail_forward", values,
                         context={"app": "Mail", "mail": mail})      # W25: sender's people page, habits


def wiki_query(mail: dict) -> str:
    d = domain_of(mail.get("sender_address"))
    company = d.split(".")[-2] if d and d not in FREE_MAIL and d.count(".") >= 1 else (mail.get("sender_name") or "")
    return f"{company} {mail.get('subject') or ''}".strip()


def wiki_snippets(query: str, repo_dir: str | None = None, *, timeout: float = WIKI_TIMEOUT_S) -> list[str]:
    """Top-3 local wiki notes ("title: description") for `query`; [] on any failure (the action goes on).
    W25: the warm in-process index (oi.rag) first; the subprocess only when it is not available."""
    if not query:
        return []
    from . import rag
    warm = rag.snippets(query, 3, repo_dir)
    if warm is not None:
        return warm
    repo = repo_dir or _repo_dir()
    script = Path(repo) / "tools/wiki/wiki_search.py"
    if not script.is_file():
        return []
    try:
        p = subprocess.run([sys.executable, str(script), query, "--top", "3", "--json"], capture_output=True,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        rows = json.loads(p.stdout) if p.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    out = []
    for r in rows[:3] if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("title"):
            out.append(f"{r['title']}: {r.get('description') or ''}".strip()[:300])
    return out


def _repo_dir() -> str:
    try:
        raw = json.loads((paths.data_dir() / "settings.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("repo_dir"), str):
            return raw["repo_dir"]
    except (OSError, ValueError):
        pass
    return os.path.expanduser("~/os-intent")


# ---- AppleScript (Mail, Safari, System Events) --------------------------------------------------------

READ_MAIL = r'''on run argv
	set maxBody to (item 1 of argv) as integer
	set mid to ""
	if (count of argv) > 1 then set mid to item 2 of argv
	set sep to character id 31
	if application "Mail" is not running then return "ERR" & sep & "not_running"
	tell application "Mail"
		set m to missing value
		if mid is not "" then
			set ms to (messages of inbox whose message id is mid)
			if (count of ms) > 0 then set m to item 1 of ms
		end if
		if m is missing value then
			set sel to selection
			if (count of sel) is 0 then return "ERR:no_selection"
			set m to item 1 of sel
			if mid is not "" then
				if (message id of m) is not mid then return "ERR:selection_changed"
			end if
		end if
		set nm to ""
		set ad to ""
		try
			set s to sender of m
			set nm to extract name from s
			set ad to extract address from s
		end try
		set subj to ""
		try
			set subj to subject of m
		end try
		set dr to ""
		try
			set dr to ((date received of m) as «class isot» as string)
		end try
		set mid to ""
		try
			set mid to message id of m
		end try
		set cc to ""
		try
			repeat with r in (cc recipients of m)
				set cc to cc & (address of r) & ","
			end repeat
		end try
		set rt to ""
		try
			set rt to reply to of m
		end try
		set b to ""
		try
			set b to content of m
		end try
		if (length of b) > maxBody then set b to text 1 thru maxBody of b
	end tell
	return "OK" & sep & nm & sep & ad & sep & subj & sep & dr & sep & mid & sep & cc & sep & rt & sep & b
end run
'''

# The previous message of the thread: the newest older message of the same mailbox whose subject contains the
# base subject (Re:/Fwd:/TR: stripped). Best effort, bounded by the osascript timeout.
READ_PREVIOUS = r'''on run argv
	set base to item 1 of argv
	set maxLen to (item 2 of argv) as integer
	set mid to ""
	if (count of argv) > 2 then set mid to item 3 of argv
	set sep to character id 31
	if application "Mail" is not running then return "NONE"
	tell application "Mail"
		set m to missing value
		if mid is not "" then
			set ms to (messages of inbox whose message id is mid)
			if (count of ms) > 0 then set m to item 1 of ms
		end if
		if m is missing value then
			set sel to selection
			if (count of sel) is 0 then return "ERR:no_selection"
			set m to item 1 of sel
			if mid is not "" then
				if (message id of m) is not mid then return "ERR:selection_changed"
			end if
		end if
		set dr to date received of m
		set cands to (messages of (mailbox of m) whose subject contains base and date received < dr)
		set best to missing value
		repeat with c in cands
			if best is missing value then
				set best to contents of c
			else if (date received of c) > (date received of best) then
				set best to contents of c
			end if
		end repeat
		if best is missing value then return "NONE"
		set b to content of best
		if (length of b) > maxLen then set b to text 1 thru maxLen of b
		set nm to ""
		try
			set nm to extract name from (sender of best)
		end try
		return "OK" & sep & nm & sep & ((date received of best) as «class isot» as string) & sep & b
	end tell
end run
'''

# the reply / forward for the mail with that message id (the selection only when it is not in the inbox,
# checked against the id), optionally pre-filled. There is no `send` anywhere: the user sends it themselves.
# mode "place" (the Claude draft): no window, no `activate`, saved to Drafts -> "OK:<outgoing message id>", so the
#   user never leaves the current task (no Space switch); the HUD card shows it and offers « Open in Mail ».
# mode "window" (the plain, empty reply the user picked): opens the compose window in Mail and activates it.
# Mail is never launched: not running = "ERR:not_running".
OPEN_DRAFT = r'''on run argv
	set akind to item 1 of argv
	set mid to item 2 of argv
	set txt to item 3 of argv
	set mode to "place"
	if (count of argv) > 3 then set mode to item 4 of argv
	if application "Mail" is not running then return "ERR:not_running"
	tell application "Mail"
		set m to missing value
		if mid is not "" then
			set ms to (messages of inbox whose message id is mid)
			if (count of ms) > 0 then set m to item 1 of ms
		end if
		if m is missing value then
			set sel to selection
			if (count of sel) is 0 then return "ERR:no_selection"
			set m to item 1 of sel
			if mid is not "" then
				if (message id of m) is not mid then return "ERR:selection_changed"
			end if
		end if
		set win to (mode is "window")
		if akind is "reply" then
			set r to reply m opening window win
		else
			set r to forward m opening window win
		end if
		if txt is not "" then
			set q to ""
			try
				set q to content of r
			end try
			set content of r to txt & return & return & q
		end if
		if win then
			activate
			return "OK"
		end if
		save r
		return "OK:" & ((id of r) as string)
	end tell
end run
'''

# « Open in Mail » on the draft card (on demand only): show that saved draft's compose window, then activate
# Mail. The draft is gone from Mail's outgoing messages (Mail restarted, sent, deleted): show the Drafts mailbox.
SHOW_DRAFT = r'''on run argv
	set did to (item 1 of argv) as integer
	tell application "Mail"
		try
			set visible of (outgoing message id did) to true
			activate
			return "OK"
		end try
		activate
		try
			if (count of message viewers) is 0 then make new message viewer
			set selected mailboxes of (message viewer 1) to {drafts mailbox}
		end try
		return "OK:drafts"
	end tell
end run
'''

# W24: select (and show) the message with that Message-ID in the front viewer — read-only, nothing marked,
# moved or sent. Used before a proactive mail's reply / draft / "Open the mail", so they act on THAT mail.
SELECT_MAIL = r'''on run argv
	set mid to item 1 of argv
	if application "Mail" is not running then return "ERR:not_running"
	tell application "Mail"
		set ms to (messages of inbox whose message id is mid)
		if (count of ms) is 0 then return "ERR:not_found"
		set m to item 1 of ms
		if (count of message viewers) is 0 then make new message viewer
		set mv to message viewer 1
		try
			set selected mailboxes of mv to {mailbox of m}
		end try
		set selected messages of mv to {m}
		activate
		return "OK"
	end tell
end run
'''

# Context cards: the body of one inbox message by id, READ-ONLY (reading `content` does not mark it read;
# nothing is selected or opened), for a proactive mail's level-3 summary.
READ_MAIL_BODY = r'''on run argv
	set mid to item 1 of argv
	set maxBody to (item 2 of argv) as integer
	if application "Mail" is not running then return "ERR:not_running"
	tell application "Mail"
		set ms to (messages of inbox whose message id is mid)
		if (count of ms) is 0 then return "ERR:not_found"
		set b to ""
		try
			set b to content of (item 1 of ms)
		end try
	end tell
	if (length of b) > maxBody then set b to text 1 thru maxBody of b
	return "OK" & character id 31 & b
end run
'''

READ_SAFARI = r'''on run argv
	set maxLen to (item 1 of argv) as integer
	set sep to character id 31
	tell application "Safari"
		if (count of windows) is 0 then return "ERR" & sep & "no_window"
		set t to current tab of front window
		set ti to name of t
		set u to URL of t
		set js to "0"
		set vState to ""
		set body to ""
		try
			set vState to do JavaScript "document.readyState" in t
			set body to do JavaScript "document.body ? document.body.innerText : ''" in t
			set js to "1"
		end try
	end tell
	if (length of body) > maxLen then set body to text 1 thru maxLen of body
	return "OK" & sep & ti & sep & u & sep & js & sep & vState & sep & body
end run
'''

READ_WINDOW_TITLE = r'''on run argv
	tell application "System Events"
		set p to first application process whose name is (item 1 of argv)
		return name of front window of p
	end tell
end run
'''


def osascript(script: str, args: list[str], timeout: float = OSA_TIMEOUT_S) -> tuple[int, str]:
    """Runs `script` (from stdin, so no quoting) with `args` as argv. (returncode, stdout)."""
    try:
        p = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.rstrip("\n")
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def _clean(s: str) -> str:
    return s.replace(SEP, " ").replace("\r\n", "\n").replace("\r", "\n")


_RE_PREFIX = re.compile(r"^\s*((re|tr|fw|fwd|aw|wg)\s*:\s*)+", re.I)


def base_subject(subject: str) -> str:
    return _RE_PREFIX.sub("", subject or "").strip()


def parse_mail(out: str) -> dict | None:
    """READ_MAIL's output: OK, name, address, subject, date, message id, cc addresses ("a@x,b@y,"), reply-to, body."""
    parts = out.split(SEP)
    if len(parts) < 9 or parts[0] != "OK":
        return None
    name, address, subject, date, mid, cc, reply_to = parts[1:8]
    return {"sender_name": name.strip(), "sender_address": address.strip(), "subject": subject.strip(),
            "date": date.strip(), "message_id": mid.strip(),
            "cc": [a.strip() for a in cc.split(",") if a.strip()], "reply_to": reply_to.strip(),
            "body": _clean(SEP.join(parts[8:]))[:BODY_MAX], "previous": None}


def mail_local_only(mail: dict | None, cfg: dict) -> bool:
    """W15: the mail never goes to Claude when its sender, any cc recipient or its reply-to address is in a
    settings.claude.local_only_domains domain (a "Name <a@b>" reply-to is read for its address)."""
    if not mail:
        return False
    addrs = [mail.get("sender_address"), mail.get("reply_to"), *(mail.get("cc") or [])]
    return any(local_only(domain_of(a), cfg) for a in addrs if isinstance(a, str) and a)


def read_mail_context(osa: Callable = osascript, *, body_max: int = BODY_MAX, previous: bool = True,
                      message_id: str = "") -> dict | None:
    """The inbox message `message_id` (looked up, never selected), else the selected Mail message (None: no
    selection / not found, Mail not running, Automation refused)."""
    rc, out = osa(READ_MAIL, [str(body_max)] + ([message_id] if message_id else []))
    mail = parse_mail(out) if rc == 0 else None
    if mail and previous:
        base = base_subject(mail["subject"])
        if base and base != mail["subject"]:            # only a reply has a previous message worth a lookup
            rc, out = osa(READ_PREVIOUS, [base, str(PREV_MAX)] + ([message_id] if message_id else []))
            parts = out.split(SEP) if rc == 0 else []
            if len(parts) >= 4 and parts[0] == "OK":
                mail["previous"] = {"sender_name": parts[1].strip(), "date": parts[2].strip(),
                                    "body": _clean(SEP.join(parts[3:]))[:PREV_MAX]}
    return mail


def read_mail_body(message_id: str, osa: Callable = osascript, *, body_max: int = BODY_MAX) -> str:
    """Context cards: the body of the inbox message `message_id` ("" when not found / Mail not running)."""
    if not message_id:
        return ""
    rc, out = osa(READ_MAIL_BODY, [message_id, str(body_max)])
    parts = out.split(SEP, 1) if rc == 0 else []
    return _clean(parts[1])[:body_max] if len(parts) == 2 and parts[0] == "OK" else ""


def read_safari(osa: Callable = osascript, *, want_host: str | None = None, wait_s: float = 3.0,
                sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict | None:
    """Front Safari tab: {title, url, text?}. Waits up to `wait_s` for the page that was just opened (host match +
    readyState complete). `text` only when Safari allows JavaScript from Apple Events."""
    deadline = clock() + wait_s
    page = None
    while True:
        rc, out = osa(READ_SAFARI, [str(PAGE_MAX)])
        parts = out.split(SEP) if rc == 0 else []
        if len(parts) >= 6 and parts[0] == "OK":
            page = {"title": parts[1].strip(), "url": parts[2].strip(), "js": parts[3] == "1",
                    "text": _clean(SEP.join(parts[5:]))[:PAGE_MAX] if parts[3] == "1" else None}
            host_ok = not want_host or want_host in page["url"]
            if host_ok and (not page["js"] or parts[4].strip() == "complete"):
                return page
        if clock() >= deadline:
            return page
        sleep(0.4)


def open_draft_script(primitive: str, message_id: str, text: str, window: bool = False) -> tuple[str, list[str]]:
    """in place (saved to Drafts, Mail never fronted) unless `window` (the plain reply the user picked)."""
    return OPEN_DRAFT, ["reply" if primitive == "mail.reply" else "forward", message_id or "", text or "",
                        "window" if window else "place"]


def draft_id(res: str) -> int | None:
    """OPEN_DRAFT's "OK:<id>" (in place) -> the outgoing message id (None: no id)."""
    if not isinstance(res, str) or not res.startswith("OK:"):
        return None
    try:
        return int(res[3:].strip())
    except ValueError:
        return None


def show_draft(draft: int, osa: Callable | None = None) -> bool:
    """« Open in Mail »: the user's explicit press, the one place a draft activates Mail."""
    rc, out = (osa or osascript)(SHOW_DRAFT, [str(int(draft))])
    return rc == 0 and out.startswith("OK")


def plain_mail(primitive: str, osa: Callable | None = None, message_id: str = "") -> bool:
    """The plain (non-Claude) mail primitive: an empty reply / forward window (the user picked it: Mail comes to
    the front). With `message_id` (W15 / W24) that mail, looked up by id (never selected first); nothing
    opens when it is neither in the inbox nor the current selection."""
    script, args = open_draft_script(primitive, message_id, "", window=True)
    rc, out = (osa or osascript)(script, args)
    return rc == 0 and out.startswith("OK")


# ---- HUD socket ---------------------------------------------------------------------------------------

def send_command(path: Path, payload: dict, timeout: float = 3.0) -> dict:
    """One command on a fresh connection; the first ok / dropped / error line (W14 `result`, `update`)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and ev.get("event") in ("ok", "dropped", "error"):
                        return ev
    except (OSError, ValueError):
        return {"event": "dropped", "reason": "socket_error"}
    return {"event": "dropped", "reason": "no_reply"}


# ---- HUD labels ---------------------------------------------------------------------------------------

MAIL_LABELS = {"mail.reply": ("Reply", "sf:arrowshape.turn.up.left"),
               "mail.forward": ("Forward", "sf:arrowshape.turn.up.right")}


def first_name(name: str, address: str = "") -> str:
    n = (name or "").strip().strip('"')
    if not n or "@" in n:
        n = (address or "").split("@")[0].replace(".", " ").replace("_", " ")
    if "," in n:                       # "Dupont, Sam"
        n = n.split(",", 1)[1]
    w = n.split()
    return w[0].capitalize() if w else ""


def clean_label(label) -> str | None:
    if not isinstance(label, str):
        return None
    t = " ".join(label.replace("—", "-").split())
    return t[:LABEL_MAX] if t else None


def card_text(lines, limit: int = 4) -> str:
    out = [" ".join(str(x).split())[:80] for x in (lines or []) if isinstance(x, str) and x.strip()]
    return "\n".join(out[:limit])


def draft_card(text: str) -> str:
    """the result card of an in-place draft: where it is, then its first lines."""
    return "Draft saved in Mail Drafts (not sent):\n" + card_text((text or "").splitlines(), limit=3)


# ---- the actions --------------------------------------------------------------------------------------

class ClaudeActions:
    """Everything W14 does on the python side. Collaborators are injectable (tests, dry-run)."""

    def __init__(self, socket_path: Path | None = None, *, runner: Callable[..., dict] = run_claude,
                 osa: Callable = osascript, wiki: Callable[[str], list[str]] = wiki_snippets,
                 mail_reader: Callable[..., dict | None] | None = None, safari_reader: Callable[..., dict | None] | None = None,
                 settings_loader: Callable[[], dict] = claude_settings, sender: Callable[[dict], dict] | None = None,
                 calendar: Callable[[dict], str] | None = None, dry_run: bool = False, out=None, clock: Callable[[], float] = time.monotonic):
        self.socket_path = socket_path or paths.hud_socket()
        self.runner = runner
        self.osa = osa
        self.wiki = wiki
        self.mail_reader = mail_reader or (lambda **kw: read_mail_context(self.osa, **kw))
        self.safari_reader = safari_reader or (lambda **kw: read_safari(self.osa, **kw))
        self.settings_loader = settings_loader
        # the slots a mail proposes, free / busy from the local calendars (read-only); "(none)" = no calendar
        self.calendar = calendar or (lambda mail: calendar_slots.calendar_context(mail, log=log))
        self.sender = sender or (lambda payload: send_command(self.socket_path, payload))
        self.dry_run = dry_run
        self.out = out or sys.stdout
        self.clock = clock
        self.cache: OrderedDict[str, dict] = OrderedDict()
        self.inflight: set[str] = set()
        self.lock = threading.Lock()

    # -- the model call, timed and (debug only) kept --------------------------------------------------
    def ask(self, action: str, prompt: str, cfg: dict) -> tuple[dict | None, dict]:
        model = MODELS[action]
        t0 = self.clock()
        try:
            out, ok, code = self.runner(prompt, model), True, "ok"
        except ClaudeUnavailable as e:
            out, ok, code = None, False, e.code
        meta = {"action": action, "model": model, "latency_ms": int((self.clock() - t0) * 1000), "ok": ok}
        log(f"{action} {model} {meta['latency_ms']} ms {code}")
        if cfg.get("debug"):
            self._debug(action, prompt, out, code)
        return out, meta

    def _debug(self, action: str, prompt: str, out, code: str) -> None:
        d = paths.data_dir() / "claude-debug"
        paths.ensure_private_root()
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
        f = d / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{action}.json"
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"action": action, "code": code, "prompt": prompt, "output": out}, fh, ensure_ascii=False)

    def respond(self, key: int, ok: bool, text: str, ttl_ms: int = CARD_TTL_MS, open_draft: int | None = None) -> bool:
        """Sends `result` to the HUD; True when it still waited (not cancelled by Esc). `open_draft`: the
        card offers « Open in Mail » for that saved draft (the press runs `oi mail-open-draft`)."""
        payload = {"cmd": "result", "key": key, "ok": ok, "text": text, "ttl_ms": ttl_ms}
        if open_draft is not None:
            payload["open_draft"] = open_draft
        r = self.sender(payload)
        return r.get("event") == "ok"

    # -- a long-press --------------------------------------------------------------------------------------
    def press(self, opt: dict | None, ctx: dict, key: int, plain: Callable[[], str | None],
              expect_mid: str | None = None) -> tuple[str | None, dict | None]:
        """Runs the Claude variant of `opt` (always answers the HUD). Returns (executed token, claude log meta).
        `expect_mid` (W15): the mail a glide follow-up was built for; another selection = nothing is done."""
        prim = (opt or {}).get("primitive")
        cfg = self.settings_loader()
        if not opt or not offers(prim, cfg) or opt.get("private"):   # W24: a gated proactive event never reaches Claude
            # no Claude variant (or Claude switched off): release the HUD, then the plain action
            return (plain() if self.respond(key, True, "") else None), None
        if prim in MAIL_PRIMITIVES:
            return self._mail(prim, key, cfg, plain, expect_mid)
        return self._summary(opt, ctx, key, cfg, plain)

    def _mail(self, prim: str, key: int, cfg: dict, plain: Callable[[], str | None], expect_mid: str | None = None):
        # read NOW (the press); a proactive / follow-up mail by its id (no selection, Mail stays behind)
        mail = self.mail_reader(previous=True, **({"message_id": expect_mid} if expect_mid else {}))
        if not mail:
            self.respond(key, False, "No mail selected (or Mail access refused)")
            return None, None
        if expect_mid and mail.get("message_id") != expect_mid:
            self.respond(key, False, "The selected mail changed: nothing done")
            return None, None
        if mail_local_only(mail, cfg):
            done = self.respond(key, True, "Local-only sender: plain " + ("reply" if prim == "mail.reply" else "forward"),
                                ttl_ms=DRAFT_CARD_TTL_MS)
            return (plain() if done else None), None
        snippets = self.wiki(wiki_query(mail))
        cal = self.calendar(mail) if prim == "mail.reply" else "(none)"
        out, meta = self.ask(prim, mail_prompt(prim, mail, snippets, cal), cfg)
        text = (out or {}).get("body" if prim == "mail.reply" else "note")
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            meta["ok"] = False
            done = self.respond(key, False, "Claude unavailable: plain " + ("reply" if prim == "mail.reply" else "forward"),
                                ttl_ms=DRAFT_CARD_TTL_MS)
            return (plain() if done else None), meta
        # the draft is made IN PLACE (no window, Mail not fronted, saved to Drafts), then shown on the card
        script, args = open_draft_script(prim, mail.get("message_id", ""), text)
        if self.dry_run:
            print(json.dumps({"dry_run": "osascript", "script": script, "args": args}, ensure_ascii=False),
                  file=self.out, flush=True)
            self.respond(key, True, draft_card(text), ttl_ms=DRAFT_SHOW_TTL_MS)
            return prim, meta
        rc, res = self.osa(script, args)
        if rc != 0 or not res.startswith("OK"):
            log(f"{prim}: Mail draft not saved ({res or rc})")
            self.respond(key, False, "Mail draft not saved: nothing done")
            return None, meta
        if not self.respond(key, True, draft_card(text), ttl_ms=DRAFT_SHOW_TTL_MS, open_draft=draft_id(res)):
            log(f"{prim}: card cancelled (Esc): the draft stays in Drafts, never sent")
        return prim, meta

    def _summary(self, opt: dict, ctx: dict, key: int, cfg: dict, plain: Callable[[], str | None]):
        prim, params = opt.get("primitive"), opt.get("params") or {}
        tok = plain()                                # open first, then summarise
        if not tok:
            self.respond(key, False, "Could not open it")
            return None, None
        app = params.get("app") if prim == "app.open" else "Safari"
        shost = None
        if app == "Safari":
            host = (params.get("url") or "").split("/", 1)[0] if prim == "url.open" else None
            if host and local_only(host, cfg):
                self.respond(key, True, "Local-only site: not summarised", ttl_ms=DRAFT_CARD_TTL_MS)
                return tok, None
            page = self.safari_reader(want_host=host)
            if not page:
                self.respond(key, False, "Safari page not readable")
                return tok, None
            from urllib.parse import urlsplit
            shost = urlsplit(page.get("url") or "").hostname
            if local_only(shost, cfg):
                self.respond(key, True, "Local-only site: not summarised", ttl_ms=DRAFT_CARD_TTL_MS)
                return tok, None
            what = "web page"
            body = f"Title: {page.get('title', '')}\nURL: {page.get('url', '')}\n"
            body += f"\n{page['text']}" if page.get("text") else "\n(page text not available: title and address only)"
        else:
            rc, title = self.osa(READ_WINDOW_TITLE, [app])
            what = "app"
            body = f"App: {app}\nWindow: {title if rc == 0 and title else '(unknown)'}"
        sctx = {"app": app, "host": shost, "space": ctx.get("space")}          # W25: the pack's context
        out, meta = self.ask(prim, render_prompt("summary", {"what": what, "page": body}, context=sctx), cfg)
        lines = card_text((out or {}).get("lines"), limit=3)
        if not lines:
            meta["ok"] = False
            self.respond(key, False, "Claude unavailable: no summary")
            return tok, meta
        self.respond(key, True, lines)
        return tok, meta

    # -- label enrichment ------------------------------------------------------------------------------
    def after_show(self, form_file: str | None, shown: list[dict], payload: dict) -> threading.Thread | None:
        """After a show: upgrade the labels of the form's `enrich` options in place (local first, then haiku,
        cached per mail). A daemon thread; None when nothing to do."""
        form = next((f for f in forms.all_forms() if f.get("_file") == form_file), None)
        if not form:
            return None
        enrich = {k for k, o in form["options"].items() if o.get("enrich")}
        targets = [(sent["key"], o, sent["label"]) for o, sent in zip(shown, payload.get("options") or [])
                   if o.get("key") in enrich]
        if not targets:
            return None
        t = threading.Thread(target=self._enrich, args=(targets,), daemon=True, name="oi-enrich")
        t.start()
        return t

    def _enrich(self, targets: list[tuple[int, dict, str]]) -> None:
        try:
            mail = self.mail_reader(previous=False, body_max=LABEL_BODY_MAX)
            if not mail:
                return
            cfg = self.settings_loader()
            first = first_name(mail.get("sender_name", ""), mail.get("sender_address", ""))
            local = {}
            for hk, o, sent in targets:
                if o.get("primitive") == "mail.reply" and first:
                    local[hk] = clean_label(f"Reply to {first}")
            current = {hk: sent for hk, _, sent in targets}
            if local:
                self._update(local, current)
            if not (cfg.get("enabled") and cfg.get("enrich")) or mail_local_only(mail, cfg):
                return
            ck = mail.get("message_id") or hashlib.sha1(
                f"{mail.get('sender_address')}|{mail.get('subject')}".encode()).hexdigest()
            with self.lock:
                labels = self.cache.get(ck)
                if labels is None and ck in self.inflight:
                    return
                if labels is None:
                    self.inflight.add(ck)
            if labels is None:
                try:
                    keys = sorted({o["key"] for _, o, _ in targets})
                    prompt = render_prompt("labels", {
                        "actions": ", ".join(keys), "sender_name": mail.get("sender_name", ""),
                        "sender_address": mail.get("sender_address", ""), "subject": mail.get("subject", ""),
                        "body": (mail.get("body") or "")[:LABEL_BODY_MAX]},
                        context={"app": "Mail", "mail": mail})        # W25: the pack
                    out, _ = self.ask("enrich", prompt, cfg)
                    raw = (out or {}).get("labels")
                    labels = {k: clean_label(v) for k, v in raw.items() if clean_label(v)} if isinstance(raw, dict) else {}
                finally:
                    with self.lock:
                        self.inflight.discard(ck)
                with self.lock:
                    self.cache[ck] = labels
                    while len(self.cache) > CACHE_SIZE:
                        self.cache.popitem(last=False)
            upd = {hk: labels[o["key"]] for hk, o, _ in targets if labels.get(o["key"])}
            if upd:
                self._update(upd, {hk: local.get(hk, sent) for hk, _, sent in targets})
        except Exception as e:   # never take the driver down over a label
            log(f"enrich failed: {type(e).__name__}")

    def _update(self, labels: dict[int, str], expect: dict[int, str]) -> dict:
        r = self.sender({"cmd": "update", "options": [{"key": k, "label": v, "expect": expect.get(k)}
                                                       for k, v in sorted(labels.items())]})
        if r.get("event") == "ok":
            expect.update(labels)
        return r


# ---- CLI (`python -m oi.claude_actions ...`) ---------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m oi.claude_actions", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help='one real headless call (haiku) with a harmless prompt: {"ok": true}')
    run = sub.add_parser("run", help="one action on a SYNTHETIC mail context (no Mail read), --dry-run prints the AppleScript")
    run.add_argument("--action", choices=MAIL_PRIMITIVES, required=True)
    run.add_argument("--context-file", required=True, help="JSON: sender_name, sender_address, subject, date, message_id, body")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--no-wiki", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        mode = auth_mode(dict(os.environ), Path.home())
        t0 = time.monotonic()
        try:
            out = run_claude(render_prompt("check", {}), MODELS["check"])
            ok = out.get("ok") is True
            code = "ok" if ok else "unexpected_answer"
        except ClaudeUnavailable as e:
            ok, code = False, e.code
        print(json.dumps({"ok": ok, "auth": mode, "model": MODELS["check"], "code": code,
                          "latency_ms": int((time.monotonic() - t0) * 1000)}))
        return 0 if ok else 1
    mail = json.loads(Path(a.context_file).read_text(encoding="utf-8"))
    acts = ClaudeActions(mail_reader=lambda **kw: dict(mail), sender=lambda p: {"event": "ok"},
                         wiki=(lambda q: []) if a.no_wiki else wiki_snippets, dry_run=a.dry_run)
    executed, meta = acts.press({"primitive": a.action, "params": {}}, {"app": "Mail"}, 1,
                                plain=lambda: None)
    print(json.dumps({"executed": executed, "claude": meta}))
    return 0 if executed else 1


if __name__ == "__main__":
    sys.exit(_main())
