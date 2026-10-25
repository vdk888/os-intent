"""Laya forms: loader, schema validation, router (design §1 "form router", §2 schema)."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

FORMS_DIR = Path(__file__).parent
REQUIRED = ["form_id", "_version", "status", "trigger", "state_template", "question",
            "candidates", "options", "gate", "display", "promotion"]
STATUSES = {"draft", "shadow", "live", "retired"}
EXPAND_KINDS = {"activate", "url", "title", "space", "key", "click"}   # W16: click
FORBIDDEN_KEYS = {"yes", "no", "true", "false"}  # checkpoints follow the key word (design §0)
GATE_KEYS = {"temperature", "show_min_p", "none_max_p", "max_show", "cooldown_s"}
PROMOTION_KEYS = {"beats_markov_at3_by", "ece_max", "min_test_moments"}
MAX_CRITERIA_WORDS = 8
_PLACEHOLDER = re.compile(r"\{[^}]+\}")


def load_form(name_or_path: str | Path) -> dict:
    """'A' / 'C' (packaged) or a path to a YAML form."""
    p = Path(name_or_path)
    if not p.suffix:
        p = FORMS_DIR / f"{name_or_path}.yaml"
    form = yaml.safe_load(p.read_text())
    trig = form.get("trigger")
    if isinstance(trig, dict) and True in trig:  # YAML 1.1 reads a bare `on:` key as True
        trig["on"] = trig.pop(True)
    form["_file"] = p.stem
    return form


@lru_cache(maxsize=1)
def _packaged() -> tuple:
    return tuple(load_form(p) for p in sorted(FORMS_DIR.glob("*.yaml")))


def all_forms() -> list[dict]:
    return [dict(f) for f in _packaged()]


def criteria_words(criteria: str) -> int:
    """Word count with each placeholder counted as one word (expanded later, then capped)."""
    return len(_PLACEHOLDER.sub("X", criteria).split())


def validate(form: dict) -> list[str]:
    errs = [f"missing {k}" for k in REQUIRED if k not in form]
    if errs:
        return errs
    fid = form["form_id"]
    if not str(form["_version"]).startswith(f"{fid}@v"):
        errs.append("_version must be <form_id>@v<N>")
    if form["status"] not in STATUSES:
        errs.append(f"status not in {sorted(STATUSES)}")
    trig = form["trigger"]
    if not isinstance(trig, dict) or "app" not in trig or not trig.get("on"):
        errs.append("trigger needs app and on")
    q = form["question"]
    if q.get("type") != "choice":
        errs.append("question.type must be choice (score has the slot bug, design §0)")
    if not str(q.get("instructions", "")).strip():
        errs.append("question.instructions empty")
    cand = form["candidates"]
    if cand.get("source") != "markov" or not (8 <= int(cand.get("k", 0)) <= 15):
        errs.append("candidates: source markov, k in 8..15")
    opts = form["options"]
    if "nothing" not in opts:
        errs.append("options must define nothing")
    for key, o in opts.items():
        if key.lower() in FORBIDDEN_KEYS:
            errs.append(f"option key {key!r} forbidden")
        if not o.get("criteria"):
            errs.append(f"{key}: criteria missing")
        elif criteria_words(o["criteria"]) > MAX_CRITERIA_WORDS:
            errs.append(f"{key}: criteria over {MAX_CRITERIA_WORDS} words")
        if key != "nothing" and not o.get("primitive"):
            errs.append(f"{key}: needs exactly one primitive")
        if key == "nothing" and o.get("primitive"):
            errs.append("nothing has no primitive")
        if o.get("expand") is not None and o["expand"] not in EXPAND_KINDS:
            errs.append(f"{key}: expand not in {sorted(EXPAND_KINDS)}")
    for f in cand.get("fixed") or []:
        if f not in opts:
            errs.append(f"fixed option {f!r} not in options")
        elif opts[f].get("expand"):
            errs.append(f"fixed option {f!r} must not expand")
    if set(form["gate"]) != GATE_KEYS:
        errs.append(f"gate keys must be {sorted(GATE_KEYS)}")
    if set(form["promotion"]) != PROMOTION_KEYS:
        errs.append(f"promotion keys must be {sorted(PROMOTION_KEYS)}")
    for k in form["display"] or {}:
        if k not in opts:
            errs.append(f"display for unknown option {k!r}")
    if "{" not in form["state_template"]:
        errs.append("state_template has no placeholders")
    return errs


def route(context: dict, forms: list[dict] | None = None) -> dict:
    """Most specific form whose trigger matches (app-specific first), else the generic '*'."""
    forms = forms if forms is not None else all_forms()
    live = [f for f in forms if f.get("status") != "retired"]
    app = (context or {}).get("app")
    for f in live:
        if f["trigger"]["app"] != "*" and f["trigger"]["app"] == app:
            return f
    for f in live:
        if f["trigger"]["app"] == "*":
            return f
    raise LookupError("no generic form")
