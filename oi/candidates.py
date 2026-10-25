"""Candidate generator (design §1): Markov top-k ∪ form fixed options ∪ nothing.

Code builds the menu, the model only picks from it (contract.md). Steps:
1. Markov top-k (k from the form, clamped to 8..15) over the moment's context;
2. each predicted token is expanded through the form option whose `expand` kind matches
   the token kind (tokens with no matching option in this form are skipped);
   no-op candidates (the app/url already in front) are skipped;
3. the form's fixed options; 4. `nothing`, always present (always last).
De-duplicated by key, filtered by `is_executable(option)`, capped at MAX_OPTIONS (16,
the Core ML option slots) with `nothing` kept.
"""
from __future__ import annotations

import os
import re
from typing import Callable

from . import vocab
from .moments import NOTHING

MAX_OPTIONS = 16
K_MIN, K_MAX = 8, 15
MAX_CRITERIA_WORDS = 8
_PH = re.compile(r"\{(\w+)\}")
_CHORD = {"⌘": "cmd", "⇧": "shift", "⌥": "opt", "⌃": "ctrl"}
APP_DIRS = ["/Applications", "~/Applications", "/System/Applications",
            "/System/Applications/Utilities", "/System/Library/CoreServices"]


def app_installed(app: str) -> bool:
    return any(os.path.isdir(os.path.join(os.path.expanduser(d), f"{app}.app")) for d in APP_DIRS)


def default_is_executable(option: dict) -> bool:
    """Apps must be installed (/Applications, ~/Applications, plus macOS system app dirs
    where Mail/Finder live); URLs and everything else are always ok."""
    if option.get("primitive") in ("app.open", "app.focus"):
        return app_installed(option["params"].get("app", ""))
    return True


def token_values(token: str) -> tuple[str, dict] | None:
    try:
        kind, g = vocab.parse(token)
    except ValueError:
        return None
    if kind == "activate":
        return kind, {"app": g["app"], "id": g["app"]}
    if kind == "url":
        words = g["slug"].replace("-", " ").strip()
        page = f"{words} on {g['host']}" if words else g["host"]
        return kind, {"url": f"{g['host']}/{g['slug']}", "host": g["host"], "page": page,
                      "id": f"{g['host']}-{g['slug']}"}
    if kind == "title":
        return kind, {"app": g["app"], "page": g["slug"].replace("-", " "),
                      "id": f"{g['app']}-{g['slug']}"}
    if kind == "space":
        mv = g["move"]
        text = {"+1": "slide to next desk", "-1": "slide to previous desk"}.get(mv) or f"go to desk {mv[1:]}"
        return kind, {"move": mv, "desk_move": text,
                      "id": {"+1": "next", "-1": "prev"}.get(mv, f"desk{mv[1:]}")}
    if kind == "key":
        ch = g["chord"]
        return kind, {"chord": ch, "id": "".join(_CHORD.get(c, c) for c in ch)}
    if kind == "click":   # W16: a learned page button / link -> form C press_button (ui.press)
        return kind, {"host": g["host"], "role": g["role"], "slug": g["slug"],
                      "label": g["slug"].replace("-", " "), "id": f"{g['role']}-{g['slug']}"}
    return None


def _fill(template, vals: dict):
    if isinstance(template, dict):
        return {k: _fill(v, vals) for k, v in template.items()}
    if isinstance(template, str):
        return _PH.sub(lambda m: str(vals.get(m.group(1), m.group(0))), template)
    return template


def _criteria(text: str) -> str:
    return " ".join(text.split()[:MAX_CRITERIA_WORDS])


def _is_noop(token: str, ctx: dict) -> bool:
    return token == f"activate:{ctx.get('app')}" or (ctx.get("url") and token == f"url:{ctx['url']}")


def build(form: dict, moment: dict, model=None, *, k: int | None = None,
          is_executable: Callable[[dict], bool] = default_is_executable,
          opaque: bool = False) -> list[dict]:
    opts = form["options"]
    by_kind = {o["expand"]: key for key, o in opts.items() if o.get("expand")}
    k = max(K_MIN, min(K_MAX, k or int(form["candidates"].get("k", 10))))
    ctx = moment["context"]
    out: dict[str, dict] = {}

    def add(key, spec, vals, token, p):
        if key in out:
            return
        o = {"key": key, "criteria": _criteria(_fill(spec["criteria"], vals)),
             "primitive": spec.get("primitive"), "params": _fill(spec.get("params") or {}, vals),
             "token": token, "p_markov": p}
        if spec.get("pin"):          # W14: a summon lists it first (form B mail options, no Markov token)
            o["pin"] = True
        if key == NOTHING or is_executable(o):
            out[key] = o

    if model is not None:
        for token, p in model.top_k(moment, k, include_nothing=False):
            tv = token_values(token)
            if not tv or tv[0] not in by_kind or _is_noop(token, ctx):
                continue
            base = by_kind[tv[0]]
            key = f"{base}_{vocab.slug(tv[1]['id'], 24).replace('-', '_')}"
            add(key, opts[base], tv[1], token, p)
    for f in form["candidates"].get("fixed") or []:
        if f != NOTHING:
            add(f, opts[f], {}, None, None)
    nothing = {"key": NOTHING, "criteria": _criteria(opts[NOTHING]["criteria"]), "primitive": None,
               "params": {}, "token": NOTHING, "p_markov": None}
    menu = [o for key, o in out.items() if key != NOTHING][: MAX_OPTIONS - 1] + [nothing]
    if opaque:
        for i, o in enumerate(menu):
            o["semantic_key"], o["key"] = o["key"], chr(ord("A") + i)
    return menu


def as_question(form: dict, menu: list[dict]) -> dict:
    """The Laya `choice` question for this menu (wire format of design §0)."""
    return {"type": "choice", "instructions": form["question"]["instructions"],
            "criteria": {o["key"]: o["criteria"] for o in menu}}
