"""`oi improve-report` (W13): the weekly improvement input for an offline improvement pass (a stronger model reviewing forms and labels).

A stronger model (e.g. Opus) is for the training / weekly-improvement phase (form evolution, label review, fine-tune
runs), never at runtime. This report is what that pass reads: per form, over the given days (default the
last 7 UTC days that have tokens), from the hud-log and the token days (read-only):

  - HUD funnel: evaluated moments, shown, pressed, dismissed by reason, gate drops by reason, summons;
  - wishes (the "what I wanted" panel): count per form + the wished slugs (LOCAL ONLY: typed text);
  - misses: moments whose next action was not in the candidate menu (by token kind and app/host), and moments
    where it was in the menu but outside Markov's top 3;
  - Laya shadow: rows with a Laya prediction, errors by reason, latency p50/p95, agreement of Laya's top
    option with Markov's.

Written to reports/improve-<last day>.{md,json} (0600). It contains app names, hosts and wish slugs: it stays
on this Mac and is never pasted into cards, PRs or rooms (aggregate numbers only there).
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, timedelta

from . import normalise, paths, replay
from .markov import Markov

TOP = 10


def default_days(n: int = 7, today: date | None = None) -> list[str]:
    have = sorted(p.stem for p in paths.tokens_dir().glob("????-??-??.jsonl"))
    if today is not None:
        lo = (today - timedelta(days=n - 1)).isoformat()
        have = [d for d in have if lo <= d <= today.isoformat()]
    return have[-n:]


def _where(token: str) -> str:
    kind, _, rest = token.partition(":")
    if kind == "url":
        return f"url:{rest.split('/', 1)[0]}"
    if kind in ("activate", "title"):
        return f"{kind}:{rest.split('/', 1)[0]}"
    return kind


def report(days: list[str], reader=replay.read_day, hud_reader=normalise.read_hud_log) -> dict:
    forms: dict = defaultdict(lambda: {"moments": 0, "shown": 0, "pressed": 0, "dismissed": Counter(),
                                       "dropped": Counter(), "summoned": 0, "wishes": 0, "wish_slugs": Counter(),
                                       "not_in_menu": 0, "not_in_menu_where": Counter(), "outside_top3": 0,
                                       "laya_rows": 0, "laya_errors": Counter(), "laya_latency": [],
                                       "laya_agree_top1": 0, "numbering": Counter()})
    for d in days:
        for r in hud_reader(d):
            if r.get("kind") == "vocal":
                continue    # W12: `oi vocal-send` rows are not moments
            f = forms[r.get("form") or "?"]
            f["numbering"][str(normalise.hud_numbering(r))] += 1   # W21b: "1" = rank-numbered (pre-W21) rows
            out = str(r.get("outcome", ""))
            f["summoned"] += bool(r.get("summoned"))
            if r.get("shown") is True:
                f["shown"] += 1
            if out.startswith("pressed:"):
                f["pressed"] += 1
            elif out.startswith("dismissed:"):
                f["dismissed"][out.split(":", 1)[1]] += 1
            elif out.startswith("dropped:"):
                f["dropped"][out.split(":", 1)[1]] += 1
            lay = r.get("laya")
            if isinstance(lay, dict):
                f["laya_rows"] += 1
                if lay.get("error"):
                    f["laya_errors"][lay["error"]] += 1
                else:
                    f["laya_latency"].append(lay.get("latency_ms"))
                    probs = lay.get("probs") or {}
                    if probs and r.get("options"):
                        top = max(probs, key=probs.get)
                        f["laya_agree_top1"] += top == (lay.get("markov_top") or "")
    _, ms = replay.load_moments(days, reader) if days else ([], [])
    if ms:
        mk = Markov.fit(ms, tokens=[m["context"]["token"] for m in ms])
        for it in replay.prepare(ms, mk):
            f = forms[it["form"]["_file"]]
            f["moments"] += 1
            m = it["moment"]
            if m.get("wish"):
                f["wishes"] += 1
                f["wish_slugs"][m["wish"]] += 1
            if m["label"] == "nothing":
                continue
            if it["gold"] is None:
                f["not_in_menu"] += 1
                f["not_in_menu_where"][_where(m["label"])] += 1
            elif it["gold"] not in replay.ranked(it["markov"])[:3]:
                f["outside_top3"] += 1
    out = {"kind": "improve", "created_at": replay._now(), "days": days, "forms": {}}
    for name, f in sorted(forms.items()):
        lat = [x for x in f.pop("laya_latency") if x is not None]
        out["forms"][name] = {**{k: (dict(v.most_common(TOP)) if isinstance(v, Counter) else v) for k, v in f.items()},
                              "laya_latency_p50": replay.pct(lat, 0.5), "laya_latency_p95": replay.pct(lat, 0.95)}
    return out


def to_markdown(r: dict) -> str:
    lines = [f"# oi improvement report — {', '.join(r['days']) or 'no days'}", "",
             "Input for a weekly offline improvement pass. LOCAL ONLY: app names, hosts and "
             "wish slugs; share aggregate numbers only.", ""]
    for name, f in r["forms"].items():
        lines += [f"## form {name}",
                  f"- moments {f['moments']}; shown {f['shown']}, pressed {f['pressed']}, dismissed "
                  f"{f['dismissed']}, summoned {f['summoned']}; gate drops {f['dropped']}",
                  f"- wishes {f['wishes']}: {f['wish_slugs']}",
                  f"- next action not in the menu: {f['not_in_menu']} {f['not_in_menu_where']}; in the menu but "
                  f"outside Markov top 3: {f['outside_top3']}",
                  f"- Laya shadow rows {f['laya_rows']}, errors {f['laya_errors']}, latency p50/p95 "
                  f"{f['laya_latency_p50']}/{f['laya_latency_p95']} ms, top-1 agrees with Markov "
                  f"{f['laya_agree_top1']}",
                  f"- hud-log rows by numbering (1 = rank, before W21; 2 = key): {f['numbering']}", ""]
    lines += ["## Mission prompts", "- Forms with many not-in-menu misses: add the missing primitive kind or raise k.",
              "- Wishes that repeat: candidate new options / forms (catalogue primitives only).",
              "- Dismissals by reason: tighten the gate τ for that form or reword the criteria (evolve).",
              "- Retrain (`oi laya-train`) only on days never used as test; promote only via `oi laya-promote`."]
    return "\n".join(lines) + "\n"


def write(r: dict) -> str:
    base = replay.reports_dir() / f"improve-{(r['days'] or ['none'])[-1]}"
    replay._write_private(base.with_suffix(".json"), json.dumps(r, indent=1, ensure_ascii=False))
    replay._write_private(base.with_suffix(".md"), to_markdown(r))
    return str(base.with_suffix(".md"))
