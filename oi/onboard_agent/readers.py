"""W22b: what the onboarding agent may read, and the two content readers (Mail, Notion).

Settings (settings.json `onboarding`, every key optional, wrongly typed = default):
  {"model": "sonnet", "mail_limit": 100, "mail_body_max": 1500, "notion_pages": 40, "notion_days": 90,
   "memory_roots": ["~/notes"], "timeout_s": 1200}
`memory_roots` = extra memory systems the person points to (read-only, like the wiki). A root that is the home
folder, a parent of it, ~/.claude itself or a secrets folder is refused.

Local-only (settings.claude.local_only_domains, the one exclusion): a mail whose sender, cc or reply-to is in such
a domain, or whose subject / excerpt names one (W25 pack.leaks), is removed BEFORE the tool prints anything; same
for a Notion page whose title or text names one.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable

from .. import claude_actions as ca, paths

DEFAULTS = {"model": "sonnet", "mail_limit": 100, "mail_body_max": 1500, "notion_pages": 40, "notion_days": 90,
            "memory_roots": [], "timeout_s": 1200}
LIMITS = {"mail_limit": (1, 1000), "mail_body_max": (200, 5000), "notion_pages": (1, 200), "notion_days": (1, 3650),
          "timeout_s": (60, 3600)}
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-\[\]]{1,60}$")
NOTION_DB = Path("Library/Application Support/Notion/notion.db")
SECRET_DIRS = (".ssh", ".gnupg", ".aws", ".claude", "Library/Keychains", ".config/gh")
# Never read by the agent (deny rules under every read root) and never copied into its context.
SECRET_GLOBS = ("**/*.env", "**/.env*", "**/*secret*", "**/.credentials*", "**/*token*", "**/*.pem", "**/*.key",
                "**/.ssh/**", "**/id_*", "**/*.keychain*", "**/Keychains/**", "**/*sops*")
SECRET_NAMES = ("*.env", ".env*", "*secret*", ".credentials*", "*token*", "*.pem", "*.key", "id_*", "*.keychain*",
                "*sops*")
SECRET_PARTS = (".ssh", "Keychains", ".gnupg")


def is_secret(p: Path) -> bool:
    """A path the onboarding never copies (the same patterns as SECRET_GLOBS, case-insensitive)."""
    parts = [x.lower() for x in p.parts]
    return (any(fnmatch.fnmatch(parts[-1], g) for g in SECRET_NAMES) if parts else False) \
        or any(x.lower() in parts[:-1] for x in SECRET_PARTS)


def settings(path: Path | None = None) -> dict:
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    try:
        raw = json.loads((path or paths.data_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    o = raw.get("onboarding") if isinstance(raw, dict) else None
    if not isinstance(o, dict):
        return out
    if isinstance(o.get("model"), str) and MODEL_RE.match(o["model"]):
        out["model"] = o["model"]
    for k, (lo, hi) in LIMITS.items():
        v = o.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = int(min(max(v, lo), hi))
    if isinstance(o.get("memory_roots"), list):
        out["memory_roots"] = [x.strip() for x in o["memory_roots"] if isinstance(x, str) and x.strip()]
    return out


# ---- the memory roots (read-only) -----------------------------------------------------------------------------

def workspaces_dir(home: Path) -> Path:
    """Where your Claude Code workspaces live: $OI_WORKSPACES_DIR, default ~/claude-workspaces."""
    raw = os.environ.get("OI_WORKSPACES_DIR")
    return Path(raw).expanduser() if raw else home / "claude-workspaces"


def claude_md_files(home: Path) -> list[Path]:
    """~/.claude/CLAUDE.md and the CLAUDE.md of each workspace (workspaces_dir: ~/claude-workspaces/* by default)."""
    out = [home / ".claude" / "CLAUDE.md"] + sorted(workspaces_dir(home).glob("*/CLAUDE.md"))
    return [p for p in out if p.is_file() and not is_secret(p)]


def memory_dirs(home: Path) -> list[Path]:
    """~/.claude/projects/*/memory folders holding at least one file (empty ones are skipped)."""
    out = []
    for d in sorted((home / ".claude" / "projects").glob("*/memory")):
        try:
            if d.is_dir() and any(p.is_file() for p in d.rglob("*")):
                out.append(d)
        except OSError:
            continue
    return out


def wiki_dir(home: Path) -> Path | None:
    """Your knowledge base: a local folder of markdown pages ($OI_WIKI_DIR, default ~/wiki; the same folder
    tools/wiki/wiki_search.py indexes). None when it does not exist."""
    raw = os.environ.get("OI_WIKI_DIR")
    d = Path(raw).expanduser() if raw else home / "wiki"
    return d if d.is_dir() else None


def safe_root(p: Path, home: Path) -> str | None:
    """None when `p` may be a read root, else why not."""
    try:
        r, h = p.expanduser().resolve(), home.resolve()
    except OSError:
        return "unreadable"
    if not r.is_dir():
        return "not a folder"
    if r == h or r in h.parents or r == Path("/"):
        return "the home folder or a parent of it"
    for s in SECRET_DIRS:
        sd = (h / s).resolve()
        if r == sd or sd in r.parents:
            return f"under ~/{s}"
    return None


def custom_roots(cfg: dict, home: Path) -> tuple[list[Path], list[str]]:
    """(accepted roots, refusals) of settings onboarding.memory_roots."""
    ok, refused = [], []
    for raw in cfg.get("memory_roots") or []:
        p = Path(raw.replace("~", str(home), 1) if raw.startswith("~") else raw)
        why = safe_root(p, home)
        if why:
            refused.append(f"{raw}: {why}")
        else:
            ok.append(p.resolve())
    return ok, refused


def count_files(d: Path, pattern: str = "*") -> int:
    try:
        return sum(1 for p in d.rglob(pattern) if p.is_file())
    except OSError:
        return 0


# ---- Mail (AppleScript: the last N messages of the inbox) --------------------------------------------------------

RS = "\x1e"
MAIL_BATCH = 25
# Messages i0 .. i0 + n - 1 of the unified inbox (newest first). One batch per call: a message body costs
# ~1 s through Apple Events, so the reader goes batch by batch (and the default 120 s event timeout is raised).
MAIL_RECENT_AS = r'''on run argv
	set i0 to (item 1 of argv) as integer
	set n to (item 2 of argv) as integer
	set maxBody to (item 3 of argv) as integer
	set sep to character id 31
	set rs to character id 30
	set out to ""
	with timeout of 300 seconds
	tell application "Mail"
		set total to count of messages of inbox
		set i1 to i0 + n - 1
		if total < i1 then set i1 to total
		if i1 < i0 then return ""
		set msgs to messages i0 thru i1 of inbox
		repeat with i from 1 to (count of msgs)
			set m to item i of msgs
			set nm to ""
			set ad to ""
			set subj to ""
			set dr to ""
			set cc to ""
			set rt to ""
			set b to ""
			try
				set s to sender of m
				set nm to extract name from s
				set ad to extract address from s
			end try
			try
				set subj to subject of m
			end try
			try
				set dr to ((date received of m) as «class isot» as string)
			end try
			try
				repeat with r in (cc recipients of m)
					set cc to cc & (address of r) & ","
				end repeat
			end try
			try
				set rt to reply to of m
			end try
			try
				set b to content of m
				if (length of b) > maxBody then set b to text 1 thru maxBody of b
			end try
			set out to out & "M" & sep & nm & sep & ad & sep & subj & sep & dr & sep & cc & sep & rt & sep & b & rs
		end repeat
	end tell
	end timeout
	return out
end run
'''


_QUOTE_HEAD = re.compile(r"^((on|le) .{4,160}(wrote|a écrit)\s*:|-{2,}\s*(original message|message d'origine).*)$", re.I)


_FILLER = re.compile("[\u034f\u00ad\u200b-\u200f\u2060\ufeff\ufffc\u2007\u00a0]+")


def _clean_body(text: str, n: int) -> str:
    """Invisible filler (newsletter preheader padding) removed, quoted replies (`>` lines, "On … wrote:") cut, blank
    runs collapsed, first `n` characters."""
    lines = []
    for ln in _FILLER.sub(" ", text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = ln.strip()
        if _QUOTE_HEAD.match(s):
            break
        if not s.startswith(">"):
            lines.append(s)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return re.sub(r"[ \t]{2,}", " ", out)[:n]


def parse_mails(out: str, body_max: int) -> list[dict]:
    mails = []
    for rec in (out or "").split(RS):
        parts = rec.lstrip("\n").split(ca.SEP)
        if len(parts) < 8 or parts[0] != "M":
            continue
        mails.append({"sender_name": parts[1].strip(), "sender_address": parts[2].strip().lower(),
                      "subject": " ".join(parts[3].split()), "date": parts[4].strip(),
                      "cc": [c for c in parts[5].split(",") if c.strip()], "reply_to": parts[6].strip(),
                      "body": _clean_body(ca.SEP.join(parts[7:]), body_max)})
    mails.sort(key=lambda m: m["date"], reverse=True)
    return mails


def gate_mails(mails: list[dict], cfg: dict) -> tuple[list[dict], int]:
    """(kept, removed): a local-only sender / cc / reply-to, or a subject / excerpt naming a local-only term."""
    from ..orchestrator import pack
    terms = pack.deny_terms(cfg)
    kept = [m for m in mails if not ca.mail_local_only(m, cfg)
            and not pack.leaks(f"{m['sender_name']} {m['subject']}\n{m['body']}", cfg, terms)]
    return kept, len(mails) - len(kept)


def read_mail_batches(limit: int, body_max: int, cfg: dict, *, osa: Callable = ca.osascript,
                      batch: int = MAIL_BATCH, timeout: float = 320, stop: Callable[[], bool] = lambda: False):
    """Yields (mails kept, mails removed as local-only) per batch of `batch` inbox messages, newest first, up to
    `limit`; raises OSError when Mail cannot be read (not running, Automation refused, timeout) on the first batch."""
    seen: set = set()
    for first in range(1, limit + 1, batch):
        if stop():
            return
        rc, out = osa(MAIL_RECENT_AS, [str(first), str(min(batch, limit - first + 1)), str(body_max * 2)], timeout)
        if rc != 0:
            if first == 1:
                raise OSError("Mail not readable (not running, Automation refused or timeout)")
            return
        mails = [m for m in parse_mails(out, body_max)
                 if (k := (m["date"], m["sender_address"], m["subject"])) not in seen and not seen.add(k)]
        if not mails:
            return
        yield gate_mails(mails, cfg)


def read_mails(limit: int, body_max: int, cfg: dict, *, osa: Callable = ca.osascript,
               batch: int = MAIL_BATCH) -> tuple[list[dict], int, str | None]:
    """(mails kept, mails removed as local-only, error or None), all batches at once."""
    kept, removed = [], 0
    try:
        for k, r in read_mail_batches(limit, body_max, cfg, osa=osa, batch=batch):
            kept += k
            removed += r
    except OSError as e:
        return [], 0, str(e)
    return kept, removed, None


# ---- Notion (the desktop app's local SQLite cache, read-only, immutable, no copy) -------------------------------

TEXT_BLOCKS = ("text", "header", "sub_header", "sub_sub_header", "header_4", "bulleted_list", "numbered_list",
               "to_do", "toggle", "quote", "callout", "code")
PAGE_TEXT_MAX = 2500


def notion_db(home: Path) -> Path:
    return home / NOTION_DB


def _title(props: str | None) -> str:
    """Notion rich text (`{"title": [["text", [annotations]], …]}`) -> plain text."""
    try:
        segs = (json.loads(props) or {}).get("title") if props else None
    except ValueError:
        return ""
    if not isinstance(segs, list):
        return ""
    return "".join(s[0] for s in segs if isinstance(s, list) and s and isinstance(s[0], str)).strip()


def read_notion(db: Path, *, pages: int, days: int, cfg: dict, now: float | None = None) -> tuple[list[dict], int, str | None]:
    """(pages [{title, edited, text}], pages removed as local-only, error or None): the most recently edited
    pages of the last `days` days with the text of their blocks (two levels), in page order."""
    from ..orchestrator import pack
    if not db.is_file():
        return [], 0, "Notion desktop cache not found"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as e:
        return [], 0, f"Notion cache not readable ({type(e).__name__})"
    terms = pack.deny_terms(cfg)
    since = ((now or time.time()) - days * 86400) * 1000
    out, removed = [], 0
    try:
        rows = con.execute(
            "SELECT id, properties, content, last_edited_time FROM block WHERE type IN ('page', 'collection_view_page')"
            " AND alive = 1 AND COALESCE(is_template, 0) = 0 AND last_edited_time >= ?"
            " ORDER BY last_edited_time DESC LIMIT ?", (since, pages * 3)).fetchall()
        for pid, props, content, edited in rows:
            if len(out) >= pages:
                break
            title = _title(props)
            text = _page_text(con, content)
            if not title and not text:
                continue
            if pack.leaks(f"{title}\n{text}", cfg, terms):
                removed += 1
                continue
            out.append({"title": title or "(untitled)", "edited": time.strftime("%Y-%m-%d", time.localtime(edited / 1000)),
                        "text": text})
    except sqlite3.Error as e:
        return out, removed, f"Notion cache schema not understood ({type(e).__name__})"
    finally:
        con.close()
    return out, removed, None


def _children(con, content: str | None) -> list[tuple]:
    try:
        ids = [i for i in json.loads(content) if isinstance(i, str)] if content else []
    except ValueError:
        return []
    if not ids:
        return []
    rows = con.execute(f"SELECT id, type, properties, content FROM block WHERE alive = 1 AND id IN "
                       f"({','.join('?' * len(ids[:300]))})", ids[:300]).fetchall()
    order = {i: n for n, i in enumerate(ids)}
    return sorted(rows, key=lambda r: order.get(r[0], 0))


def _page_text(con, content: str | None, depth: int = 2) -> str:
    lines: list[str] = []

    def walk(c, level):
        for _bid, typ, props, sub in _children(con, c):
            if sum(len(x) for x in lines) > PAGE_TEXT_MAX:
                return
            if typ in TEXT_BLOCKS:
                t = _title(props)
                if t:
                    prefix = {"header": "# ", "sub_header": "## ", "sub_sub_header": "### ", "bulleted_list": "- ",
                              "numbered_list": "- ", "to_do": "[ ] "}.get(typ, "")
                    lines.append("  " * level + prefix + " ".join(t.split()))
            if level + 1 < depth and typ not in ("page", "collection_view_page"):
                walk(sub, level + 1)
    walk(content, 0)
    return "\n".join(lines)[:PAGE_TEXT_MAX]
