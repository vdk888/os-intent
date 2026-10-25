"""W25: the HUD orchestrator — the runtime model's identity and context.

The user: "il faut absolument qu'il ait un contexte … tu es orchestrateur …
il faut qu'il ait des skills … un pipeline de RAG … tu es un peu le chief of staff, tu vas dispatcher, tu connais
tout le monde".

Every runtime Claude prompt (glide, label enrichment, summaries, mail drafts, wish resolution, screenshot notes)
is rendered by oi.claude_actions.render_prompt, which hands it to `wrap()`:

    system.md (chief of staff on the user's Mac; proposes, never acts without their key) + the fleet roster
    + the skill fragments of that prompt (skills/*.md)
    + the context pack (oi.orchestrator.pack: now / mail sender + people page / path / dispatch / wiki / habits,
      ≤ 1.5k tokens, privacy-gated by claude.local_only_domains, cached per context key)
    + the task prompt itself (last: its "Answer with JSON only" stays the final instruction).

Everything goes on the `claude -p` stdin (never argv). settings.json `claude.orchestrator` (default true) = false
-> the bare task prompts of W14-W23. Routing helpers: `route(text)` = the roster agent whose topics fit a wish best
("demande à quelqu'un le NAV du fonds" -> orion).
"""
from __future__ import annotations

import re
import threading
import unicodedata
from pathlib import Path

import yaml

HERE = Path(__file__).parent
ROSTER = HERE / "roster.yaml"
SKILLS_DIR = HERE / "skills"
SKILLS_FOR = {                      # task prompt (oi/prompts/<name>.md) -> skill fragments
    "glide": ("dispatch", "mail", "page", "files"),
    "wish_resolve": ("dispatch",),
    "mail_reply": ("mail",), "mail_forward": ("mail",), "labels": ("mail",),
    "summary": ("page",),
    "shot_note": ("dispatch",),
    "shot_actions": ("dispatch", "mail", "files"),   # screenshot -> content actions
}
NO_WRAP = ("check",                 # the harmless auth probe stays bare
           "onboard", "onboard_followups")   # W22: maybe not the user; the roster is passed in the prompt itself
_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _read_cached(path: Path, parse):
    """`parse(text)` of a file, re-read only when its mtime changes."""
    try:
        m = path.stat().st_mtime
    except OSError:
        return parse("")
    key = str(path)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == m:
            return hit[1]
    try:
        val = parse(path.read_text(encoding="utf-8"))
    except OSError:
        val = parse("")
    with _lock:
        _cache[key] = (m, val)
    return val


def roster(path: Path | None = None) -> list[dict]:
    """The generated roster's agents ([] when missing)."""
    def parse(text):
        try:
            d = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            return []
        return [a for a in d.get("agents") or [] if isinstance(a, dict) and a.get("slug")]
    return _read_cached(path or ROSTER, parse)


def roster_text(agents: list[dict] | None = None) -> str:
    """One compact line per agent for the system prompt."""
    out = []
    for a in agents if agents is not None else roster():
        bu = ", ".join(a.get("bu") or []) or "fleet"
        out.append(f"- {a['slug']}: {a.get('name') or a['slug']} ({bu}): {a.get('ask_for') or a.get('role') or ''}")
    return "\n".join(out) or "(roster unavailable)"


def skill(name: str) -> str:
    return _read_cached(SKILLS_DIR / f"{name}.md", lambda t: t.strip())


def system_text(agents: list[dict] | None = None) -> str:
    base = _read_cached(HERE / "system.md", lambda t: t.strip())
    return base.replace("{{roster}}", roster_text(agents))


# ---- topic -> agent ---------------------------------------------------------------------------------------

def fold(text: str) -> str:
    """Lower case, accents off, punctuation -> spaces (the router's matching form)."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s-]", " ", t.replace("’", "'")).split())


def route(text: str, agents: list[dict] | None = None, *, exclude: tuple[str, ...] = ()) -> tuple[str | None, float]:
    """(slug, score) of the agent whose roster topics best match `text` (whole words / phrases); (None, 0) when no
    topic matches or two agents tie. Score = matched topics, a multi-word topic counting 2."""
    words = f" {fold(text)} "
    best: list[tuple[float, str]] = []
    for a in agents if agents is not None else roster():
        if a["slug"] in exclude:
            continue
        s = 0.0
        for t in a.get("topics") or []:
            ft = fold(str(t))
            if ft and f" {ft} " in words:
                s += 2.0 if " " in ft else 1.0
        if s:
            best.append((s, a["slug"]))
    best.sort(reverse=True)
    if not best or (len(best) > 1 and best[0][0] == best[1][0]):
        return None, 0.0
    return best[0][1], best[0][0]


# ---- the wrap ------------------------------------------------------------------------------------------------

def enabled(cfg: dict) -> bool:
    return cfg.get("orchestrator", True) is not False


def wrap(name: str, task: str, context: dict | None = None, *, cfg: dict | None = None, packer=None) -> str:
    """The full runtime prompt for task prompt `name`: orchestrator + skills + context pack + the task."""
    if name in NO_WRAP:
        return task
    from .. import claude_actions as ca
    cfg = cfg if cfg is not None else ca.claude_settings()
    if not enabled(cfg):
        return task
    from . import pack as pk
    try:
        text = (packer or pk.default()).build(context or {}, cfg, task=task)["text"]
    except Exception as e:                      # the pack is a bonus: never take a call down over it
        ca.log(f"pack failed: {type(e).__name__}")
        text = ""
    parts = [system_text()]
    parts += [s for s in (skill(n) for n in SKILLS_FOR.get(name, ())) if s]
    if text:
        parts.append(f"<orchestrator_context>\n{text}\n</orchestrator_context>")
    parts.append("---\nThe task:\n\n" + task)
    return "\n\n".join(parts)
