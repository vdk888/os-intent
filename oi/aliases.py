"""W18: the words of a wish, FR + EN — text normalisation, verbs, built-in app aliases and the learned
alias table (<data dir>/aliases.json, grown only from CONFIRMED resolutions: executed, and confirmed when the
action needed it).

Learned table (0600, atomic write):
  {"v": 1, "aliases": {"<normalised phrase>": {"id": "app:Telegram", "label": "Telegram", "n": 3,
                                               "last": "2026-10-20T16:00:00.000Z"}}}
A phrase is the whole normalised wish ("ouvre tg") and its target part once the verb is stripped ("tg"). Only
catalogue ids are stored (never a message, a mail instruction or a URL path).
"""
from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import paths

MAX_LEARNED = 500
PHRASE_MAX = 60

# verb -> words that say it (normalised: lower case, no accents). Longest phrases are matched first.
VERBS: dict[str, tuple[str, ...]] = {
    "open": ("ouvre", "ouvrir", "ouvres", "open", "lance", "lancer", "launch", "start", "demarre", "va sur", "va a",
             "aller sur", "go to", "goto", "switch to", "passe a", "passe sur", "bascule sur", "affiche", "show",
             "montre", "focus"),
    "reply": ("reponds a", "reponds", "repondre a", "repondre", "reponse a", "reply to", "reply", "respond to",
              "respond", "answer"),
    "forward": ("transfere a", "transfere", "transferer a", "transferer", "fais suivre", "forward to", "forward",
                "fwd"),
    "summary": ("resume moi", "resume", "resumer", "resume la page", "summarise", "summarize", "summary", "tldr",
                "tl dr", "de quoi parle"),
    "send": ("envoie a", "envoie", "envoyer a", "envoyer", "dis a", "dire a", "ecris a", "message a",
             "send to", "send", "tell", "message", "write to", "ping"),
    "desk": ("bureau", "desk", "espace", "space", "ecran"),
}
FILLERS = {"le", "la", "les", "l", "un", "une", "the", "a", "an", "app", "appli", "application", "mon", "ma",
           "my", "stp", "svp", "please", "moi", "de", "du", "sur", "on", "to", "page", "site"}
DESK_WORDS = {"un": 1, "one": 1, "premier": 1, "first": 1, "deux": 2, "two": 2, "trois": 3, "three": 3,
              "quatre": 4, "four": 4, "cinq": 5, "five": 5, "six": 6, "sept": 7, "seven": 7, "huit": 8, "eight": 8,
              "neuf": 9, "nine": 9}

# built-in app aliases: normalised word -> app name (only used when that app is installed)
APP_ALIASES: dict[str, str] = {
    "tg": "Telegram", "telegramme": "Telegram", "tele": "Telegram",
    "mail": "Mail", "mails": "Mail", "courrier": "Mail", "email": "Mail", "emails": "Mail", "e mail": "Mail",
    "boite mail": "Mail", "inbox": "Mail",
    "navigateur": "Safari", "browser": "Safari", "web": "Safari",
    "calendrier": "Calendar", "agenda": "Calendar", "cal": "Calendar",
    "reglages": "System Settings", "preferences": "System Settings", "settings": "System Settings",
    "parametres": "System Settings",
    "terminal": "Terminal", "term": "Terminal", "shell": "Terminal",
    "imessage": "Messages", "sms": "Messages", "texto": "Messages", "textos": "Messages",
    "musique": "Music", "notes": "Notes", "note": "Notes", "rappels": "Reminders", "fichiers": "Finder",
    "files": "Finder", "photo": "Photos", "plans": "Maps", "carte": "Maps", "cartes": "Maps",
    "calculette": "Calculator", "calculatrice": "Calculator", "wa": "WhatsApp", "whatsapp": "WhatsApp",
    "code": "Visual Studio Code", "vscode": "Visual Studio Code", "vs code": "Visual Studio Code",
    "claude": "Claude", "chatgpt": "ChatGPT", "gpt": "ChatGPT",
}


def norm(text: str | None) -> str:
    """lower case, accents removed, apostrophes / punctuation -> spaces, single spaces."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^a-z0-9.#@]+", " ", t)
    t = re.sub(r"(?<![a-z0-9])\.|\.(?![a-z0-9])", " ", t)      # keep dots inside hosts only
    return " ".join(t.split())


_VERB_LIST = sorted(((w, v) for v, ws in VERBS.items() for w in ws), key=lambda x: -len(x[0]))


def split_verb(text: str) -> tuple[str | None, str]:
    """(verb, rest) of a NORMALISED wish: the leading verb phrase, if any, and what follows it."""
    for w, v in _VERB_LIST:
        if text == w:
            return v, ""
        if text.startswith(w + " "):
            return v, text[len(w) + 1:].strip()
    return None, text


def strip_fillers(text: str) -> str:
    words = text.split()
    while words and words[0] in FILLERS:
        words.pop(0)
    return " ".join(words)


def desk_number(rest: str) -> int | None:
    """'3', 'trois', 'numero 3', 'n 3' -> 3 (1..16), else None."""
    words = [w for w in rest.split() if w not in ("numero", "n", "no", "number", "#")]
    if len(words) != 1:
        return None
    w = words[0].lstrip("#")
    n = int(w) if w.isdigit() else DESK_WORDS.get(w)
    return n if n is not None and 1 <= n <= 16 else None


# ---- the learned table ------------------------------------------------------------------------------------

def aliases_path() -> Path:
    return paths.data_dir() / "aliases.json"


class AliasStore:
    """The learned aliases (thread-safe). `get(phrase)` -> {"id", "label", "n", "last"} or None."""

    def __init__(self, path: Path | None = None):
        self._path = path
        self.lock = threading.Lock()
        self._data: dict | None = None
        self._mtime: float | None = None
        self._pending: dict = {}      # W18 follow-up: non-local resolutions seen once, waiting for a 2nd agreement

    @property
    def path(self) -> Path:
        return self._path or aliases_path()

    def _load(self) -> dict:
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            mt = None
        if self._data is None or mt != self._mtime:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                al = raw.get("aliases") if isinstance(raw, dict) else None
                self._data = {k: v for k, v in (al or {}).items()
                              if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("id"), str)}
                pe = raw.get("pending") if isinstance(raw, dict) else None   # W18 follow-up
                self._pending = {k: v for k, v in (pe or {}).items()
                                 if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("id"), str)}
            except (OSError, ValueError):
                self._data, self._pending = {}, {}
            self._mtime = mt
        return self._data

    def all(self) -> dict[str, dict]:
        with self.lock:
            return dict(self._load())

    def get(self, phrase: str) -> dict | None:
        with self.lock:
            return self._load().get(phrase)

    def learn(self, phrases: list[str], item_id: str, label: str, now: datetime | None = None,
              agree: int = 1) -> list[str]:
        """Record `phrases` -> item_id (a confirmed resolution). Returns the phrases written. W18 follow-up:
        `agree` = how many times the same phrase must resolve to the same id before it becomes an alias (2 for a
        Claude / Laya resolution, kept in `pending` meanwhile; a different id restarts the count)."""
        ts = (now or datetime.now(timezone.utc)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        out = []
        with self.lock:
            data = dict(self._load())
            pending = dict(self._pending)
            for p in dict.fromkeys(phrases):
                if not p or len(p) > PHRASE_MAX:
                    continue
                if agree > 1 and not (data.get(p) or {}).get("id") == item_id:
                    prev = pending.get(p)
                    seen = (prev.get("n", 0) if prev and prev.get("id") == item_id else 0) + 1
                    if seen < agree:
                        pending[p] = {"id": item_id, "n": seen, "last": ts}
                        continue
                pending.pop(p, None)
                prev = data.get(p)
                n = (prev.get("n", 0) if prev and prev.get("id") == item_id else 0) + 1
                data[p] = {"id": item_id, "label": label, "n": n, "last": ts}
                out.append(p)
            if len(data) > MAX_LEARNED:        # forget the least recently confirmed
                keep = sorted(data.items(), key=lambda kv: kv[1].get("last") or "", reverse=True)[:MAX_LEARNED]
                data = dict(keep)
            if len(pending) > MAX_LEARNED:
                pending = dict(sorted(pending.items(), key=lambda kv: kv[1].get("last") or "", reverse=True)[:MAX_LEARNED])
            if data == self._data and pending == self._pending:
                return out
            self._write(data, pending)
            self._data, self._pending = data, pending
            try:
                self._mtime = self.path.stat().st_mtime
            except OSError:
                self._mtime = None
        return out

    def pending(self) -> dict[str, dict]:
        with self.lock:
            self._load()
            return dict(self._pending)

    def _write(self, data: dict, pending: dict | None = None) -> None:
        p = self.path
        paths.ensure_private_root()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"v": 1, "aliases": data, "pending": pending or {}}, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n")
        os.replace(tmp, p)
