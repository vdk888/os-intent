"""W23: drill-down glide — each sub-ring level acts INSIDE / ON its parent.

The user: "après Finder, ça ne peut être que des trucs qui découlent de l'action
Finder … qui te font sauter plusieurs pas en un … derrière Telegram, envoyer dans la conversation de… Eric … derrière
le jaune, des actions jaunes … musique → telle playlist".

A parent node has a SCOPE (its target: Finder, Telegram, Music, a playlist, a folder, a site, the selected mail, a
fleet agent, a desk move, another app). Under it:
  1. per-scope LOCAL PROVIDERS give the real vocabulary (read-only, on-device, cached `PROVIDER_TTL_S`):
       Finder    -> recent files / folders (Spotlight kMDItemLastUsedDate), the current project folder (a recent folder
                    whose name is in a recent window-title token), Downloads, Desktop   -> folder.open / file.open
       folder    -> its newest entries (names only, never contents)                  -> folder.open / file.open
       Telegram  -> recent chat titles (AX of Telegram's chat list, the FIRST text of each row only = the title,
                    never the preview); fallback: Telegram window-title tokens          -> telegram.chat
       Music     -> playlists (AppleScript, only when Music already runs); playlist -> its tracks   -> music.play
       a site    -> today's pages of that host (url tokens)                          -> url.open
       Mail      -> the plain reply / forward of the selected mail                    -> mail.reply / mail.forward
  2. a HISTORY provider ranks them: "after <parent token>, the user did <item> within HISTORY_WINDOW_S" (first-order
     Markov conditioned on the parent token, over today's tokens) + the logged glide choices (training pairs);
  3. Claude (haiku) only when the scope is generative (a fleet agent, the mail: it writes messages / drafts) and
     providers are thin: it orders / labels / fills from the allowed items + forms, with the path chain (glide.py,
     prompts/glide.md). A site / another app gets its pages + "Summary" locally (the summary runs Claude on press);
  4. the SCOPE REJECTOR (`in_scope`) drops anything else: a generic switch to another app / site is never offered,
     except the explicit "back" (to the app that was in front at the show).

Confirm rule: every send (a vocal to an agent, "Ask Atlas") runs on release and waits for oi.gate's
confirm panel, the ONE confirmation (the W23 "Send ✓" child is gone). Opens (folders, files,
chats, playlists, pages) never confirm. An executable / installer (`launchable`) is never opened: only its parent
folder is offered (revealed in Finder).
Privacy: titles / names only (no message text, no file content); the training pairs keep ids (hashed when they would
name a file, a chat or a playlist), never a label.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import paths
from .vocab import slug

PROVIDER_TTL_S = 45
HISTORY_WINDOW_S = 120
PAIRS_DAYS = 14
FINDER_DAYS = 7
FINDER_MAX = 8
FOLDER_MAX = 8
CHATS_MAX = 8
PAGES_MAX = 8
MUSIC_MAX = 8
LABEL_MAX = 40
SEND_KINDS = ("vocal",)                            # a send (oi.gate confirms it; "ask" is one too)
GENERATIVE = ("agent", "mail")                      # scopes where Claude may fill (messages, drafts: beyond items)
SCOPED_APPS = {"Finder": "finder", "Telegram": "telegram", "Music": "music", "Mail": "mail"}
GENERIC_TELEGRAM_TITLES = frozenset({"telegram", "", "quick-look", "settings", "preferences", "media-viewer",
                                     "about-telegram", "untitled"})      # Telegram windows that are not a chat
_HIDDEN_HOME = ("Library",)


# ---- node ids (logged) -------------------------------------------------------------------------------------

def _h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]


def node_id(n: dict) -> str:
    """A stable id for a node: its token when it is a token kind of the stream (activate:, url:, space:), else
    "<primitive>#<hash>" for what would name a file / chat / playlist, else its action id."""
    prim, params, tok = n.get("primitive"), n.get("params") or {}, n.get("token") or ""
    if n.get("kind") in ("vocal", "ask"):                # two messages to one agent are two nodes
        return f"{n.get('action')}#{_h(params.get('text', ''))}"
    if n.get("kind") in ("summary", "draft"):
        return n.get("action") or "?"
    if prim in ("folder.open", "file.open"):
        return f"{prim}#{_h(params.get('path', ''))}"
    if prim == "telegram.chat":
        return f"telegram.chat#{_h(slug(params.get('chat', '')))}"
    if prim == "music.play":
        return f"music.play#{_h(params.get('playlist', '') + '|' + params.get('track', ''))}"
    if tok and tok.split(":", 1)[0] in ("activate", "url", "space"):
        return tok
    return n.get("action") or prim or "?"


# ---- scope + rejector --------------------------------------------------------------------------------------

def scope_of(n: dict | None) -> dict:
    """The target a node's follow-ups must act within."""
    if not n:
        return {"kind": "leaf"}
    prim, params, kind = n.get("primitive"), n.get("params") or {}, n.get("kind")
    if kind in ("summary", "draft"):        # Claude does it: nothing deeper
        return {"kind": "leaf"}
    if kind in ("vocal", "ask"):
        return {"kind": "agent", "slug": params.get("to")}
    if prim == "app.open":
        app = params.get("app") or ""
        return {"kind": SCOPED_APPS.get(app, "app"), "app": app}
    if prim == "url.open":
        return {"kind": "host", "host": host_of(params.get("url") or (n.get("token") or "").split(":", 1)[-1])}
    if prim in ("mail.reply", "mail.forward"):
        return {"kind": "mail"}
    if prim == "folder.open":
        return {"kind": "folder", "path": params.get("path")}
    if prim == "music.play" and not params.get("track"):
        return {"kind": "playlist", "playlist": params.get("playlist")}
    if prim == "space.move":
        return {"kind": "space", "token": n.get("token")}
    return {"kind": "leaf"}


def host_of(slug_or_url: str) -> str:
    return (slug_or_url or "").lower().split("://", 1)[-1].split("/", 1)[0].removeprefix("www.")


def in_scope(n: dict, scope: dict) -> bool:
    """The scoping rule: a follow-up acts within / on the parent's target, or is the explicit "back"."""
    if n.get("action") == "back":
        return scope["kind"] not in ("leaf", "space")
    prim, params, kind, action = n.get("primitive"), n.get("params") or {}, n.get("kind"), n.get("action") or ""
    k = scope["kind"]
    if k == "finder":
        return prim in ("folder.open", "file.open")
    if k == "folder":
        base = (scope.get("path") or "").rstrip("/") + "/"
        return prim in ("folder.open", "file.open") and (params.get("path") or "").startswith(base)
    if k == "telegram":
        return prim == "telegram.chat"
    if k == "music":
        return prim == "music.play" and not params.get("track")
    if k == "playlist":
        return prim == "music.play" and bool(params.get("track")) and params.get("playlist") == scope.get("playlist")
    if k == "host":
        return (prim == "url.open" and host_of(params.get("url", "")) == scope.get("host")) or action == "summarise"
    if k == "app":
        return action == "summarise"
    if k == "mail":
        return action in ("mail.reply", "mail.forward") or action.startswith("draft:") or kind in ("vocal", "ask")
    if k == "agent":
        return kind == "vocal" and params.get("to") == scope.get("slug")
    if k == "space":
        return prim == "space.move"
    return False


def forms(scope: dict) -> list[str]:
    """The action forms Claude may write under this scope (besides the item ids)."""
    k = scope["kind"]
    out = {"host": ["summarise"], "app": ["summarise"],
           "mail": ["mail.reply", "mail.forward", "draft:mail.reply", "draft:mail.forward", "vocal:<agent>:<message>",
                    "ask:<request for Atlas>"],
           "agent": [f"vocal:{scope.get('slug')}:<message>"]}.get(k, [])
    return out + (["back"] if k not in ("leaf", "space") else [])


# ---- providers ---------------------------------------------------------------------------------------------

def _node(label, icon, action, primitive, params, token=None) -> dict:
    n = {"label": (label or "?")[:LABEL_MAX], "icon": icon, "action": action, "kind": "primitive",
         "primitive": primitive, "params": params, "token": token, "claude": False}
    n["id"] = node_id(n)
    if token is None and primitive in ("folder.open", "file.open", "telegram.chat", "music.play"):
        n["token"] = n["id"]                         # the executed token logged (an id, never a name)
    return n


def safe_path(p: str | None, home: str | None = None) -> bool:
    """A local path a follow-up may open: absolute, existing, under the home folder, no hidden part, not ~/Library
    (except W24: ~/Library/CloudStorage/<provider>/…, the synced Drive folders), not an app bundle."""
    if not isinstance(p, str) or not p.startswith("/"):
        return False
    home = (home or os.path.expanduser("~")).rstrip("/")
    real = os.path.realpath(p)
    if not real.startswith(home + "/"):
        return False
    rel = real[len(home) + 1:].split("/")
    drive = len(rel) >= 3 and rel[0] == "Library" and rel[1] == "CloudStorage"   # W24: a synced Drive folder's files
    if any(part.startswith(".") for part in rel) or (rel[0] in _HIDDEN_HOME and not drive) or real.endswith(".app"):
        return False
    return os.path.exists(real)


LAUNCH_EXTS = (".command", ".sh", ".zsh", ".tool", ".app", ".pkg", ".dmg", ".terminal", ".scpt", ".applescript",
               ".workflow")


def launchable(p: str | None) -> bool:
    """`open` would run / install it: a script, an app, an installer, a workflow, or a file with the executable bit.
    Never opened by a follow-up (file.open / folder.open); only its parent folder may be revealed in Finder."""
    if not isinstance(p, str):
        return False
    real = os.path.realpath(p)
    if any(x.rstrip("/").lower().endswith(LAUNCH_EXTS) for x in (p, real)):
        return True
    return os.path.isfile(real) and os.access(real, os.X_OK)


def path_node(p: str, *, label: str | None = None, is_dir: bool | None = None) -> dict:
    if launchable(p):                                # never opened: reveal it in Finder (its parent folder)
        parent = os.path.dirname(p.rstrip("/"))
        return _node(os.path.basename(parent) or parent, "sf:folder", "folder.open", "folder.open", {"path": parent})
    d = os.path.isdir(p) if is_dir is None else is_dir
    prim = "folder.open" if d else "file.open"
    return _node(label or os.path.basename(p.rstrip("/")) or p, "sf:folder" if d else "sf:doc", prim, prim, {"path": p})


def _mdfind(days: int = FINDER_DAYS) -> str:
    try:
        return subprocess.run(["mdfind", "-onlyin", os.path.expanduser("~"), "-attr", "kMDItemLastUsedDate",
                               f"kMDItemLastUsedDate >= $time.today(-{days})"],
                              capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


_MD_LINE = re.compile(r"^(/.+?)\s+kMDItemLastUsedDate = (\S+ \S+)")


def parse_mdfind(out: str) -> list[tuple[str, str]]:
    """`mdfind -attr kMDItemLastUsedDate` lines -> [(path, "YYYY-MM-DD HH:MM:SS")] newest first."""
    rows = []
    for line in (out or "").splitlines():
        m = _MD_LINE.match(line)
        if m:
            rows.append((m.group(1), m.group(2)))
    return sorted(rows, key=lambda r: r[1], reverse=True)


def finder_items(mdfind_out: str, title_slugs: list[str], *, home: str | None = None,
                 ok: Callable[[str], bool] | None = None) -> list[dict]:
    """[current project folder] + recent files / folders (newest first) + Downloads + Desktop (deduped)."""
    home = (home or os.path.expanduser("~")).rstrip("/")
    ok = ok or (lambda p: safe_path(p, home))
    recent = [p for p, _ in parse_mdfind(mdfind_out) if ok(p)]
    folders = list(dict.fromkeys([p for p in recent if os.path.isdir(p)] + [os.path.dirname(p) for p in recent]))
    out, seen = [], set()

    def add(n):
        if n["params"]["path"] not in seen:
            seen.add(n["params"]["path"])
            out.append(n)
    proj = next((f for f in folders if f != home and ok(f) and len(slug(os.path.basename(f))) >= 4
                 and any(slug(os.path.basename(f)) in t for t in title_slugs)), None)
    if proj:
        add(path_node(proj, is_dir=True))
    for p in recent[:FINDER_MAX]:
        add(path_node(p))
    for name in ("Downloads", "Desktop"):
        p = f"{home}/{name}"
        if ok(p):
            add(path_node(p, is_dir=True))
    return out


def folder_items(path: str, *, scandir=os.scandir, ok: Callable[[str], bool] = safe_path) -> list[dict]:
    """A folder's newest entries (names + mtime only; nothing is read)."""
    if not ok(path):
        return []
    try:
        ents = [(e.stat().st_mtime, e.path, e.is_dir()) for e in scandir(path) if not e.name.startswith(".")]
    except OSError:
        return []
    ents.sort(reverse=True)
    return [path_node(p, is_dir=d) for _, p, d in ents[:FOLDER_MAX]]


# Telegram: the FIRST text of each row of the chat list (= the title; the preview comes after and is never read).
TELEGRAM_TITLES_JS = r"""
function kids(e){try{return e.uiElements();}catch(_){return [];}}
function role(e){try{return e.role();}catch(_){return '';}}
function firstText(e,d){if(d>5)return null;for(const k of kids(e)){if(role(k)==='AXStaticText'){try{const v=k.value();if(v)return String(v);}catch(_){}}const t=firstText(k,d+1);if(t)return t;}return null;}
function rows(p){if(p.windows.length===0)return null;let q=[p.windows[0]];for(let d=0;d<8&&q.length;d++){const nq=[];for(const e of q){for(const k of kids(e)){const r=role(k);if(r==='AXTable'||r==='AXOutline'||r==='AXList'){const rs=kids(k).filter(x=>['AXRow','AXCell','AXGroup'].includes(role(x)));if(rs.length>=3)return rs;}nq.push(k);}}q=nq;}return null;}
function norm(s){return String(s).normalize('NFKD').replace(/[̀-ͯ]/g,'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,30).replace(/-+$/,'');}
function run(argv){const se=Application('System Events');if(!se.processes.whose({name:'Telegram'}).length){if(argv.length)throw new Error('notfound');return '[]';}
const p=se.processes.byName('Telegram');
if(argv.length){Application('Telegram').activate();delay(0.3);const rs=rows(p)||[];const want=norm(argv[0]);
 for(const r of rs.slice(0,40)){const t=firstText(r,0);if(t&&norm(t)===want){try{se.perform(r.actions.byName('AXPress'));}catch(_){try{r.selected=true;}catch(_){throw new Error('nopress');}}return 'ok';}}throw new Error('notfound');}
const rs=rows(p);if(!rs)return '[]';const out=[];for(const r of rs.slice(0,12)){const t=firstText(r,0);if(t)out.push(t.slice(0,40));}return JSON.stringify(out);}
"""


_BOT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def bot_name(x) -> bool:
    """A Telegram bot username safe for a tg://resolve link (letters, digits, underscore; ends in "bot")."""
    return isinstance(x, str) and bool(_BOT.match(x)) and x.lower().endswith("bot")


def _telegram_titles() -> list[str]:
    try:
        p = subprocess.run(["osascript", "-l", "JavaScript", "-e", TELEGRAM_TITLES_JS],
                           capture_output=True, text=True, timeout=4)
        got = json.loads(p.stdout.strip() or "[]") if p.returncode == 0 else []
        return [t for t in got if isinstance(t, str)]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def telegram_items(ax_titles: list[str], title_tokens: list[str], *, app_icon: str = "sf:paperplane") -> list[dict]:
    """Chat titles from AX; when AX gives nothing, the Telegram window titles of the token stream (newest first)."""
    names = [t for t in (" ".join(x.split())[:LABEL_MAX] for x in ax_titles) if t]
    if not names:
        for tok in title_tokens:
            s = tok.split("/", 1)[1] if tok.startswith("title:Telegram/") else ""
            if s not in GENERIC_TELEGRAM_TITLES and not s.startswith("telegram-"):   # "Telegram – <account>"
                names.append(s.replace("-", " ").title())
    out, seen = [], set()
    for name in names:
        if slug(name) in seen or slug(name) in GENERIC_TELEGRAM_TITLES:
            continue
        seen.add(slug(name))
        out.append(_node(name, f"initial:{name[:1].upper()}", "telegram.chat", "telegram.chat", {"chat": name}))
        if len(out) == CHATS_MAX:
            break
    return out


MUSIC_TRACKS_AS = """on run argv
if application "Music" is not running then return ""
set AppleScript's text item delimiters to linefeed
tell application "Music"
  set ts to name of tracks of user playlist (item 1 of argv)
end tell
set n to count of ts
if n > 8 then set n to 8
if n is 0 then return ""
return (items 1 thru n of ts) as text
end run"""
MUSIC_LIST_AS = """if application "Music" is not running then return ""
set AppleScript's text item delimiters to linefeed
tell application "Music" to return (name of user playlists whose special kind is none) as text"""
MUSIC_PLAY_AS = """on run argv
tell application "Music"
  if (count of argv) > 1 and (item 2 of argv) is not "" then
    play (first track of user playlist (item 1 of argv) whose name is (item 2 of argv))
  else
    play user playlist (item 1 of argv)
  end if
end tell
end run"""


def _osa_lines(script: str, *args: str) -> list[str]:
    try:
        p = subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True, timeout=3)
        return [x.strip() for x in p.stdout.splitlines() if x.strip()] if p.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        return []


def music_items(names: list[str], playlist: str | None = None) -> list[dict]:
    """Playlists (playlist None) or the tracks of `playlist` -> music.play nodes."""
    out = []
    for name in list(dict.fromkeys(n for n in names if n))[:MUSIC_MAX]:
        if playlist is None:
            out.append(_node(name, "sf:music.note.list", "music.play", "music.play", {"playlist": name}))
        else:
            out.append(_node(name, "sf:music.note", "music.play", "music.play", {"playlist": playlist, "track": name}))
    return out


def host_items(host: str, pages: dict[str, list[str]]) -> list[dict]:
    """Today's pages of `host` (url token slugs, newest first)."""
    out = []
    for s in (pages.get(host) or [])[:PAGES_MAX]:
        rest = s.split("/", 1)[1] if "/" in s else ""
        out.append(_node(rest.replace("-", " ") or host, "sf:safari", "url.open", "url.open", {"url": s}, f"url:{s}"))
    return out


def space_items(token: str | None) -> list[dict]:
    """Under a one-desk move: the way back."""
    back = {"space:+1": "-1", "space:-1": "+1"}.get(token or "")
    if not back:
        return []
    return [_node("Back", "sf:arrow.left.square" if back == "-1" else "sf:arrow.right.square", "space.move",
                  "space.move", {}, f"space:{back}")]


def mail_items(mail: dict | None) -> list[dict]:
    if not mail:
        return []
    return [_node("Reply", "sf:arrowshape.turn.up.left", "mail.reply", "mail.reply", {}),
            _node("Forward", "sf:arrowshape.turn.up.right", "mail.forward", "mail.forward", {})]


class Providers:
    """The real, read-only providers (each reader injectable for tests), cached per (scope kind, target)."""

    def __init__(self, *, mdfind: Callable[[], str] | None = None, telegram_titles: Callable[[], list[str]] | None = None,
                 music_playlists: Callable[[], list[str]] | None = None,
                 music_tracks: Callable[[str], list[str]] | None = None,
                 scandir=os.scandir, ok: Callable[[str], bool] = safe_path, home: str | None = None,
                 clock: Callable[[], float] = time.monotonic, ttl: float = PROVIDER_TTL_S):
        # the real readers are looked up at call time (the test suite blocks them in conftest.py)
        self.mdfind = mdfind or (lambda: _mdfind())
        self.telegram_titles = telegram_titles or (lambda: _telegram_titles())
        self.music_playlists = music_playlists or (lambda: _osa_lines(MUSIC_LIST_AS))
        self.music_tracks = music_tracks or (lambda pl: _osa_lines(MUSIC_TRACKS_AS, pl))
        self.scandir, self.ok, self.home, self.clock, self.ttl = scandir, ok, home, clock, ttl
        self.cache: dict[tuple, tuple[float, list[dict]]] = {}
        self.lock = threading.Lock()
        self.reads = Counter()

    def _cached(self, key: tuple, fn: Callable[[], list[dict]]) -> list[dict]:
        now = self.clock()
        with self.lock:
            hit = self.cache.get(key)
            if hit and now - hit[0] < self.ttl:
                return [dict(n) for n in hit[1]]
        self.reads[key[0]] += 1
        try:
            items = fn()
        except Exception:
            items = []
        with self.lock:
            self.cache[key] = (now, items)
        return [dict(n) for n in items]

    def items(self, scope: dict, env: dict) -> list[dict]:
        k = scope["kind"]
        titles = [t for t in env.get("title_tokens") or []]
        if k == "finder":
            return self._cached(("finder",), lambda: finder_items(self.mdfind(), [t.split("/", 1)[-1] for t in titles],
                                                                  home=self.home, ok=self.ok))
        if k == "folder":
            return self._cached(("folder", scope.get("path")),
                                lambda: folder_items(scope.get("path") or "", scandir=self.scandir, ok=self.ok))
        if k == "telegram":
            return self._cached(("telegram",), lambda: telegram_items(self.telegram_titles(), titles))
        if k == "music":
            return self._cached(("music",), lambda: music_items(self.music_playlists()))
        if k == "playlist":
            pl = scope.get("playlist") or ""
            return self._cached(("playlist", pl), lambda: music_items(self.music_tracks(pl), pl))
        if k == "host":
            return host_items(scope.get("host") or "", env.get("pages") or {})
        if k == "mail":
            return mail_items(env.get("mail"))
        if k == "space":
            return space_items(scope.get("token"))
        return []


# ---- history provider + training pairs ---------------------------------------------------------------------

def pairs_dir() -> Path:
    return paths.data_dir() / "glide-pairs"


def follow_counts(parent_token: str | None, history: list[tuple[float, str]], window: float = HISTORY_WINDOW_S) -> Counter:
    """First-order Markov conditioned on the parent token: the tokens seen within `window` s after each occurrence."""
    c: Counter = Counter()
    if not parent_token:
        return c
    for i, (t, tok) in enumerate(history):
        if tok != parent_token:
            continue
        seen = set()
        for t2, tok2 in history[i + 1:]:
            if t2 - t > window:
                break
            if tok2 != parent_token and tok2 not in seen:
                seen.add(tok2)
                c[tok2] += 1
    return c


def read_pairs(days: int = PAIRS_DAYS, now: datetime | None = None, root: Path | None = None) -> list[dict]:
    root = root or pairs_dir()
    now = now or datetime.now(timezone.utc)
    out = []
    for d in range(days):
        p = root / f"{(now - timedelta(days=d)).strftime('%Y-%m-%d')}.jsonl"
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    out.append(r)
        except OSError:
            continue
    return out


def rank(items: list[dict], parent_id: str | None, parent_token: str | None,
         history: list[tuple[float, str]], pairs: list[dict]) -> list[dict]:
    """Provider order, re-ranked by history: Markov follows of the parent token + 2 × past glide choices."""
    fol = follow_counts(parent_token, history)
    chosen = Counter(r.get("choice") for r in pairs if r.get("parent") == parent_id)

    def score(n):
        tok = n.get("token") or ""
        title = f"title:Telegram/{slug(n['params'].get('chat', ''))}" if n.get("primitive") == "telegram.chat" else ""
        return fol.get(tok, 0) + fol.get(title, 0) + 2 * chosen.get(n.get("id"), 0)
    return sorted(items, key=lambda n: -score(n))           # stable: ties keep the provider order


def log_pair(parent_id: str, chain: list[str], choice: str, app: str | None, *, ts: str | None = None,
             root: Path | None = None, lock: threading.Lock | None = None) -> dict:
    """A chosen branch -> one training pair (ids only; 0600, owner-only dir)."""
    row = {"ts": ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "parent": parent_id,
           "chain": chain, "choice": choice, "depth": len(chain), "app": app if isinstance(app, str) else None}
    root = root or pairs_dir()
    paths.ensure_private_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    with lock or threading.Lock():
        fd = os.open(root / f"{row['ts'][:10]}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row
