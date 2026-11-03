"""`oi relevance-eval`: offline, HUD-faithful relevance metrics (measurement only; it changes no ranking).

Read-only on tokens/ (plus events/ with --proactive, the starter catalogue with --starters, hud-log/ for Laya).
The split is replay's: whole days, train days strictly before test days. For every test moment it builds the
menu exactly like `oi live` (route the form, `candidates.build`, then `Starters.blend` and the proactive event
options when asked) and ranks what the HUD could actually show:

  HUD order  = the live event's options first (a proactive hint takes the ring), then the menu options whose
               primitive the live gate allows (`replay.is_showable`), by show_p, then key (the gate's sort,
               without its threshold). `nothing`, `app.focus`, `key.send` never count: the HUD cannot show them.
  gold       = the moment's label (the next action within 120 s). Acted moments = label != nothing.
  top-1/3    = the gold is at HUD rank 1 / within 3; MRR = mean 1/rank (rank > 10 counts 0), on acted moments.
  per source = the same metrics split by where the gold option came from (markov, fixed, starter, proactive);
               the sources add up to the overall figure.
  gate       = the live gate (`live.gate`, default thresholds, no cooldown) on every moment: coverage (share of
               moments with a show) and precision (shows whose options hold the next action).
  laya       = when hud-log rows carry Laya shadow answers: Laya vs Markov on the same showable keys, against the
               next action (no Laya server is started).

Variants compare Markov settings (`--alpha`, `--depth`: the deepest context level kept) and add +starter /
+proactive rows. Aggregates only: the report (reports/relevance-<stamp>.{json,md}, 0600) holds no token.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import candidates, forms, laya_eval, normalise, replay, settings, starter, vocab
from .live import gate
from .markov import ALPHA, LEVELS, Markov
from .moments import NOTHING, default_tz

DEPTHS = LEVELS[1:]          # o1 .. o2hs (the o1 level is kept: the gate's support reads it)
MRR_AT = 10
SOURCES = ("markov", "fixed", "starter", "proactive")


# ---- per option ----------------------------------------------------------------------------------------------

def option_source(o: dict) -> str:
    if o.get("evt"):
        return "proactive"
    if o.get("starter"):
        return "starter"          # a starter item, or a Markov option a starter boosted
    return "markov" if o.get("token") else "fixed"


def stream_token(o: dict) -> str | None:
    """The token an option leads to in tokens/ (what the next action is compared with), None when it has none."""
    tok = o.get("token")
    if tok:
        try:
            vocab.parse(tok)
            return tok
        except ValueError:
            pass
    p = o.get("params") or {}
    if o.get("primitive") in ("app.open", "url.open"):
        t = starter.token_of({"id": "", "primitive": o["primitive"], "params": p})
        return None if t.startswith("starter:") else t
    return None


def hud_order(menu: list[dict], event_opts: list[dict] = ()) -> list[dict]:
    opts = [o for o in menu if replay.is_showable(o)]
    opts.sort(key=lambda o: (not o.get("live"), -_show_p(o), o["key"]))
    return list(event_opts) + opts


def _show_p(o: dict) -> float:
    return float(o.get("p_show", o.get("p_markov")) or 0.0)


def _epoch(ts: str) -> float:
    return vocab.parse_ts(ts).timestamp()


# ---- inputs --------------------------------------------------------------------------------------------------

class OfflineStarters(starter.Starters):
    """The live Starters with a fixed catalogue and in-memory stats, re-learnt in time order (never saved)."""

    def __init__(self, items: list[dict]):
        super().__init__(now_local=lambda t: datetime.fromtimestamp(t, default_tz()))
        self._fixed = items

    @staticmethod
    def _load_stats() -> dict:
        return {"items": {}, "learned": []}

    def items(self) -> list[dict]:
        return self._fixed

    def save(self) -> None:
        pass


def live_events(events: list[dict]):
    """events (appear / gone rows) -> f(ts) = the events live at ts (appeared, not gone yet). Calls in time order."""
    rows = sorted(((_epoch(r["ts"]), r) for r in events), key=lambda x: x[0])
    state: dict[str, dict] = {}
    i = 0

    def at(ts: str) -> list[dict]:
        nonlocal i
        t = _epoch(ts)
        while i < len(rows) and rows[i][0] <= t:
            r = rows[i][1]
            if r.get("state") == "gone":
                state.pop(r.get("id"), None)
            else:
                state[r.get("id")] = r
            i += 1
        return list(state.values())
    return at


def event_options(evs: list[dict]) -> list[dict]:
    from .proactive.engine import event_options as ev_opts
    out = []
    for ev in evs:
        out += ev_opts(ev, 1.0, names={}, route=lambda s: (None, 0.0))   # offline: no agent roster, no routing
    return out


# ---- one variant ---------------------------------------------------------------------------------------------

def variant_model(base: Markov, alpha: float, depth: str) -> Markov:
    keep = LEVELS[: LEVELS.index(depth) + 1]
    return Markov(base.catalogue, {lv: c for lv, c in base.counts.items() if lv in keep}, alpha=alpha)


def score_moments(moments: list[dict], model: Markov, *, starter_items: list[dict] | None = None,
                  events: list[dict] | None = None, cfg: dict | None = None) -> list[dict]:
    """One record per moment: {kind, acted, rank, source, in_menu, shown, hit, day} (no token kept)."""
    cfg = cfg or settings.load()["gate"]
    starters = OfflineStarters(starter_items) if starter_items else None
    live_at = live_events(events) if events is not None else None
    s_cfg = starter.settings()
    recs = []
    for m in moments:
        form = forms.route(m["context"])
        menu = candidates.build(form, m, model, is_executable=lambda o: True)
        probs = model.distribution(m)[0]
        support = sum(model.counts.get("o1", {}).get(model.features(m)[1], {}).values())
        if starters:
            menu, any_starter = starters.blend(menu, m["context"], support, s_cfg, now=_epoch(m["ts"]))
            if any_starter:
                support = max(support, cfg["min_history"])
        evt = event_options(live_at(m["ts"])) if live_at else []
        order = hud_order(menu, evt)
        label = m["label"]
        rank = next((i + 1 for i, o in enumerate(order) if stream_token(o) == label), None)
        shown, _ = gate(menu, probs.get(NOTHING, 0.0), cfg, now=0.0, last_show_at=None, paused=False,
                        hud_enabled=True, support=support)
        shown_toks = {stream_token(o) for o in shown}
        if starters:
            ids = [o["starter"] for o in shown if o.get("starter")]
            picked = next((o["starter"] for o in shown if o.get("starter") and stream_token(o) == label), None)
            starters.record(ids, picked)
        recs.append({"kind": label.split(":", 1)[0] if label != NOTHING else NOTHING, "acted": label != NOTHING,
                     "rank": rank, "source": option_source(order[rank - 1]) if rank else None,
                     "in_menu": any(stream_token(o) == label for o in menu + evt), "shown": bool(shown),
                     "hit": bool(shown) and label in shown_toks, "day": m["ts"][:10]})
    return recs


def _hits(recs: list[dict], n: int) -> dict:
    f = lambda x: round(x / n, 4) if n else None
    return {"top1": f(sum(r["rank"] == 1 for r in recs)), "top3": f(sum(r["rank"] is not None and r["rank"] <= 3
                                                                      for r in recs)),
            "mrr": f(sum(1 / r["rank"] for r in recs if r["rank"] and r["rank"] <= MRR_AT))}


def summarise(recs: list[dict]) -> dict:
    acted = [r for r in recs if r["acted"]]
    n = len(acted)
    shown = [r for r in recs if r["shown"]]
    out = {"moments": len(recs), "acted": n, **_hits(acted, n),
           "label_in_menu": round(sum(r["in_menu"] for r in acted) / n, 4) if n else None,
           "gate_coverage": round(len(shown) / len(recs), 4) if recs else None,
           "gate_precision": round(sum(r["hit"] for r in shown) / len(shown), 4) if shown else None,
           "per_source": {s: _hits([r for r in acted if r["source"] == s], n) for s in SOURCES
                          if any(r["source"] == s for r in acted)},
           "per_kind": {}, "per_day": {}}
    for k in sorted({r["kind"] for r in acted}):
        sub = [r for r in acted if r["kind"] == k]
        out["per_kind"][k] = {"n": len(sub), **_hits(sub, len(sub)),
                              "in_menu": round(sum(r["in_menu"] for r in sub) / len(sub), 4)}
    for d in sorted({r["day"] for r in acted}):
        sub = [r for r in acted if r["day"] == d]
        out["per_day"][d] = {"n": len(sub), **_hits(sub, len(sub))}
    return out


# ---- Laya (logged shadow answers) ------------------------------------------------------------------------------

def _key_primitive(form: dict, key: str) -> str | None:
    opts = form.get("options") or {}
    if key in opts:
        return opts[key].get("primitive")
    base = max((b for b in opts if key.startswith(b + "_")), key=len, default=None)
    return opts[base].get("primitive") if base else None


def laya_section(days: list[str], *, hud_reader=normalise.read_hud_log, tokens_reader=replay.read_day) -> dict | None:
    """Laya vs Markov from hud-log shadow rows, showable keys only, against the next action; None without rows."""
    recs: dict[str, list[dict]] = {"laya": [], "markov": []}
    for day in days:
        rows = [r for r in hud_reader(day) if isinstance(r.get("laya"), dict) and not r["laya"].get("error")]
        if not rows:
            continue
        try:
            toks = sorted(tokens_reader(day), key=lambda x: x["ts"])
        except FileNotFoundError:
            toks = []
        for r in rows:
            L = r["laya"]
            lp, mp, k2t = L.get("probs") or {}, L.get("markov_probs") or {}, L.get("tokens") or {}
            nxt = laya_eval.next_action(toks, r["ts"])
            if not lp or not mp or not k2t or nxt == NOTHING:
                continue
            try:
                form = forms.load_form(r.get("form") or "A")
            except (OSError, ValueError):
                continue
            show = {k for k in k2t if _key_primitive(form, k) in replay.SHOW_PRIMITIVES}
            for name, probs in (("laya", lp), ("markov", mp)):
                order = [k for k in laya_eval._ranked(probs) if k in show]
                rank = next((i + 1 for i, k in enumerate(order) if k2t.get(k) == nxt), None)
                recs[name].append({"rank": rank})
    n = len(recs["laya"])
    return {"n": n, "laya": _hits(recs["laya"], n), "markov": _hits(recs["markov"], n)} if n else None


# ---- the run -------------------------------------------------------------------------------------------------

def run(train_days: list[str], test_days: list[str], *, alphas=(ALPHA,), depths=("o2hs",),
        starter_items: list[dict] | None = None, events_reader=None, reader=replay.read_day,
        hud_reader=normalise.read_hud_log, write: bool = True) -> dict:
    """Score every variant on the test days. `events_reader(day)` turns +proactive on; `starter_items` +starter."""
    train_days, test_days = sorted(set(train_days)), sorted(set(test_days))
    replay.check_split(train_days, test_days)
    for d in depths:
        if d not in DEPTHS:
            raise replay.SplitError(f"unknown depth {d!r} (one of {', '.join(DEPTHS)})")
    _, ms = replay.load_moments(train_days + test_days, reader)
    train_ms = [m for m in ms if m["day"] in train_days]
    test_ms = [m for m in ms if m["day"] in test_days]
    if not train_ms or not test_ms:
        raise replay.SplitError(f"empty split: {len(train_ms)} train / {len(test_ms)} test moments")
    base = Markov.fit(train_ms, tokens=[m["context"]["token"] for m in train_ms])
    events = [e for d in test_days for e in events_reader(d)] if events_reader else None

    grid = [(ALPHA, "o2hs")] + [(float(a), d) for a in alphas for d in depths if (float(a), d) != (ALPHA, "o2hs")]
    variants: dict[str, dict] = {}
    for a, d in grid:
        variants[f"markov alpha={a:g} depth={d}"] = summarise(score_moments(test_ms, variant_model(base, a, d)))
    extra = [("+starter", starter_items, None)] if starter_items else []
    if events is not None:
        extra.append(("+proactive", None, events))
        if starter_items:
            extra.append(("+starter +proactive", starter_items, events))
    for name, items, evs in extra:
        variants[f"markov alpha={ALPHA:g} depth=o2hs {name}"] = summarise(
            score_moments(test_ms, base, starter_items=items, events=evs))
    baseline = next(iter(variants.values()))
    for v in variants.values():
        v["delta"] = {k: None if v[k] is None or baseline[k] is None else round(v[k] - baseline[k], 4)
                      for k in ("top1", "top3", "mrr")}
    report = {"kind": "relevance", "created_at": replay._now(), "smoke": False, "train_days": train_days,
              "test_days": test_days, "moments": {"train": len(train_ms), "test": len(test_ms)},
              "variants": variants, "laya": laya_section(test_days, hud_reader=hud_reader, tokens_reader=reader)}
    if write:
        stamp = report["created_at"].replace(":", "").replace("-", "")
        p = replay.reports_dir() / f"relevance-{stamp}"
        report["path"] = str(p) + ".md"
        replay._write_private(Path(str(p) + ".json"), json.dumps(report, indent=1))
        replay._write_private(Path(report["path"]), to_markdown(report))
    return report


# ---- report --------------------------------------------------------------------------------------------------

def _p(x) -> str:
    return "-" if x is None else f"{100 * x:.1f} %"


def _d(x) -> str:
    return "-" if x is None else f"{100 * x:+.1f}"


def to_markdown(r: dict) -> str:
    lines = [f"# oi relevance-eval — {r['created_at']}", "",
             f"- train days: {', '.join(r['train_days'])}; test days: {', '.join(r['test_days'])}",
             f"- moments: train {r['moments']['train']}, test {r['moments']['test']}; metrics on acted moments "
             "(next action != nothing), HUD-showable options only", "",
             "| variant | acted | top-1 | top-3 | MRR | Δ top-1 / top-3 / MRR (pts) | label in menu | gate coverage "
             "| gate precision |", "|---|---|---|---|---|---|---|---|---|"]
    for i, (name, v) in enumerate(r["variants"].items()):
        delta = "baseline" if i == 0 else " / ".join(_d(v["delta"][k]) for k in ("top1", "top3", "mrr"))
        lines.append(f"| {name} | {v['acted']} | {_p(v['top1'])} | {_p(v['top3'])} | {_p(v['mrr'])} | {delta} | "
                     f"{_p(v['label_in_menu'])} | {_p(v['gate_coverage'])} | {_p(v['gate_precision'])} |")
    last_name, last = list(r["variants"].items())[-1]
    lines += ["", f"Per source of the gold option ({last_name}; the rows add up to the overall figure):", "",
              "| source | top-1 | top-3 | MRR |", "|---|---|---|---|"]
    for s, v in last["per_source"].items():
        lines.append(f"| {s} | {_p(v['top1'])} | {_p(v['top3'])} | {_p(v['mrr'])} |")
    first_name, first = next(iter(r["variants"].items()))
    lines += ["", f"Per label kind ({first_name}):", "", "| kind | n | top-1 | top-3 | MRR | in menu |",
              "|---|---|---|---|---|---|"]
    for k, v in first["per_kind"].items():
        lines.append(f"| {k} | {v['n']} | {_p(v['top1'])} | {_p(v['top3'])} | {_p(v['mrr'])} | {_p(v['in_menu'])} |")
    L = r.get("laya")
    lines += [""]
    if L:
        lines += [f"Laya shadow rows (hud-log, n={L['n']}, showable keys only): Laya top-1 {_p(L['laya']['top1'])}, "
                  f"top-3 {_p(L['laya']['top3'])}, MRR {_p(L['laya']['mrr'])}; Markov on the same rows top-1 "
                  f"{_p(L['markov']['top1'])}, top-3 {_p(L['markov']['top3'])}, MRR {_p(L['markov']['mrr'])}."]
    else:
        lines += ["Laya: no scorable shadow rows in hud-log for the test days."]
    return "\n".join(lines) + "\n"
