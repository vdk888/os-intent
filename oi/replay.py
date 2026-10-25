"""`oi replay` (W13): offline replay of logged moments, Markov vs Laya (design §5 table).

Read-only on tokens/. Days are split by DAY, never shuffled: train days (strictly before the test days) fit the
Markov model and the calibration; test days are scored. Inside the train days the last CALIB_FRAC of moments
(chronological) is the calibration slice: per-form Laya temperature (NLL grid) and the gate threshold τ are
fitted there, never on the test days. Each moment: route the form, build the candidate menu (Markov top-k ∪
fixed ∪ nothing, the live code path), render the state, then score every predictor on the SAME menu:

  markov     the Markov probabilities of the menu options (P(nothing) for `nothing`), renormalised;
  laya       Laya zero-shot (stock English checkpoint), per-form temperature from the calib slice;
  tuned      an oi checkpoint (`oi laya-train`), its own temperatures re-fitted on the calib slice;
  blend      0.5 markov + 0.5 (tuned, else laya).

gold = the menu key whose token is the moment's label (the next token within 120 s, or `nothing`); a label
outside the menu is a miss for every predictor (`label_in_menu` = the ceiling). Metrics per predictor:
acc@1, acc@3, ECE (10 bins, top-1 probability), shown-precision and coverage at the calibrated gate (up to 3
showable options with p >= τ; a hit = gold among them; τ = the lowest threshold reaching 40 % precision on the
calib slice, else the most precise one), latency p50/p95; plus tokens per state (L128/L256/L512 buckets) and the
majority baseline. Only ONE Laya server runs at a time: with `manage`, each Laya predictor gets its own server
started and stopped in turn (memory rule).

Test discipline: a Laya predictor (laya / tuned) is scored on a given test day once per (predictor, form
versions). A second touch is refused (`SplitError`) unless `retouch=True`, which the report records and which
`oi laya-promote` refuses. The ledger is reports/test-ledger.json. `smoke=True` (one day only) splits that day
chronologically (first 80 % train, last 20 % test): flagged as a smoke test, not a result, never in the ledger,
never promotable.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import aw, candidates, forms, laya, paths, serialise
from .markov import Markov
from .moments import NOTHING, build_moments

CALIB_FRAC = 0.2
SMOKE_TEST_FRAC = 0.2
SHOW_PRIMITIVES = ("app.open", "url.open", "space.move")   # = live.ALLOWED_PRIMITIVES
MAX_SHOW = 3
TARGET_PRECISION = 0.40
MIN_CALIB_SHOWN = 5
TAU_GRID = [round(0.05 + i * 0.01, 2) for i in range(91)]
T_GRID = [round(0.5 + i * 0.05, 2) for i in range(91)]       # laya's own clamp is [0.5, 5.0]
LAYA_TIMEOUT_S = 5.0                                          # offline: a slow call is measured, not dropped
LAYA_PREDICTORS = ("laya", "tuned")


class SplitError(ValueError):
    pass


# ---- data ----------------------------------------------------------------------------------------------

def read_day(day: str) -> list[dict]:
    p = paths.tokens_dir() / f"{day}.jsonl"
    if not p.exists():
        raise FileNotFoundError(f"no tokens for {day} ({p})")
    return aw.read_jsonl(p)


def check_split(train_days: list[str], test_days: list[str], smoke: bool = False) -> None:
    if smoke:
        if len(set(train_days) | set(test_days)) != 1:
            raise SplitError("smoke mode takes exactly one day")
        return
    if not train_days or not test_days:
        raise SplitError("need at least one train day and one test day (one day only: --smoke)")
    if set(train_days) & set(test_days):
        raise SplitError(f"train and test days overlap: {sorted(set(train_days) & set(test_days))}")
    if max(train_days) >= min(test_days):
        raise SplitError("every train day must come before every test day (no future leakage)")


def split_days(days: list[str], test_last: int = 2) -> tuple[list[str], list[str]]:
    days = sorted(set(days))
    if len(days) <= test_last:
        raise SplitError(f"{len(days)} day(s): need more than --test-last {test_last} (one day only: --smoke)")
    return days[:-test_last], days[-test_last:]


def load_moments(days: list[str], reader=read_day) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    for d in sorted(set(days)):
        rows += reader(d)
    rows.sort(key=lambda r: r["ts"])
    ms = build_moments(rows)
    for m in ms:
        m["day"] = m["ts"][:10]
    return rows, ms


# ---- per-moment scoring ----------------------------------------------------------------------------------

def gold_key(menu: list[dict], label: str) -> str | None:
    for o in menu:
        if o.get("token") == label:
            return o["key"]
    return None


def markov_probs(menu: list[dict], model: Markov, moment: dict) -> dict[str, float]:
    dist, _ = model.distribution(moment)
    raw = {o["key"]: (dist.get(NOTHING, 0.0) if o["key"] == NOTHING else float(o.get("p_markov") or 0.0))
           for o in menu}
    z = sum(raw.values())
    if z <= 0:
        return {k: 1 / len(raw) for k in raw}
    return {k: v / z for k, v in raw.items()}


def ranked(probs: dict[str, float]) -> list[str]:
    return [k for k, _ in sorted(probs.items(), key=lambda kv: (-kv[1], kv[0]))]


def prepare(moments: list[dict], model: Markov, count=None) -> list[dict]:
    """Route, menu, state text and gold for each moment (independent of the predictor)."""
    out = []
    for m in moments:
        form = forms.route(m["context"])
        menu = candidates.build(form, m, model, is_executable=lambda o: True)
        text, n_tok, trunc = serialise.render(form, m, **({"count": count} if count else {}))
        out.append({"moment": m, "form": form, "menu": menu, "text": text, "tokens": n_tok,
                    "over_budget": "over_budget" in trunc, "gold": gold_key(menu, m["label"]),
                    "label": m["label"], "showable": {o["key"] for o in menu if o.get("primitive") in SHOW_PRIMITIVES},
                    "markov": markov_probs(menu, model, m)})
    return out


def score(items: list[dict], predict, name: str, log=None) -> list[dict]:
    """predict(form, menu, text) -> {"probs", "latency_ms"}; errors become a uniform miss (counted)."""
    recs = []
    for i, it in enumerate(items):
        t0 = time.monotonic()
        err = None
        if name == "markov":
            probs, lat = it["markov"], (time.monotonic() - t0) * 1000
        else:
            try:
                r = predict(it["form"], it["menu"], it["text"])
                probs, lat = r["probs"], r["latency_ms"]
            except laya.LayaError as e:
                probs, lat, err = {o["key"]: 1 / len(it["menu"]) for o in it["menu"]}, None, e.reason
        recs.append({"form": it["form"]["form_id"], "gold": it["gold"], "probs": probs, "latency_ms": lat,
                     "error": err, "showable": it["showable"], "day": it["moment"]["day"]})
        if log and (i + 1) % 100 == 0:
            log(f"  {name}: {i + 1}/{len(items)}")
    return recs


# ---- calibration -------------------------------------------------------------------------------------------

def fit_temperature(recs: list[dict]) -> float:
    """T minimising the NLL of gold (records whose gold is in the menu); 1.0 when there is nothing to fit."""
    pairs = [(r["probs"], r["gold"]) for r in recs if r["gold"] is not None and not r.get("error")]
    if not pairs:
        return 1.0
    best, best_t = math.inf, 1.0
    for t in T_GRID:
        nll = -sum(math.log(max(laya.apply_temperature(p, t)[g], 1e-9)) for p, g in pairs)
        if nll < best - 1e-12:
            best, best_t = nll, t
    return best_t


def fit_temperatures(recs: list[dict]) -> dict[str, float]:
    return {f: fit_temperature([r for r in recs if r["form"] == f]) for f in sorted({r["form"] for r in recs})}


def with_temperatures(recs: list[dict], temps: dict[str, float]) -> list[dict]:
    return [dict(r, probs=laya.apply_temperature(r["probs"], temps.get(r["form"]))) for r in recs]


def shown(rec: dict, tau: float) -> list[str]:
    opts = [(k, p) for k, p in rec["probs"].items() if k in rec["showable"] and p >= tau]
    opts.sort(key=lambda kp: (-kp[1], kp[0]))
    return [k for k, _ in opts[:MAX_SHOW]]


def gate_stats(recs: list[dict], tau: float) -> tuple[float | None, float, int]:
    """(shown-precision, coverage, shown count) at threshold tau."""
    n_shown = hits = 0
    for r in recs:
        s = shown(r, tau)
        if s:
            n_shown += 1
            hits += r["gold"] in s
    return (hits / n_shown if n_shown else None), (n_shown / len(recs) if recs else 0.0), n_shown


def calibrate_tau(recs: list[dict]) -> tuple[float, bool]:
    """(τ, target met): the lowest τ whose calib precision >= 40 % (max coverage), else the most precise τ."""
    for tau in TAU_GRID:
        prec, _, n = gate_stats(recs, tau)
        if prec is not None and n >= MIN_CALIB_SHOWN and prec >= TARGET_PRECISION:
            return tau, True
    best = (-1.0, 0.5)
    for tau in TAU_GRID:
        prec, _, n = gate_stats(recs, tau)
        if prec is not None and n >= MIN_CALIB_SHOWN and prec > best[0]:
            best = (prec, tau)
    return best[1], False


# ---- metrics ---------------------------------------------------------------------------------------------

def ece(recs: list[dict], bins: int = 10) -> float | None:
    if not recs:
        return None
    acc = [[0, 0.0, 0.0] for _ in range(bins)]
    for r in recs:
        top = ranked(r["probs"])[0]
        p = r["probs"][top]
        b = min(bins - 1, int(p * bins))
        acc[b][0] += 1
        acc[b][1] += p
        acc[b][2] += top == r["gold"]
    return sum(abs(c / n - s / n) * n for n, s, c in acc if n) / len(recs)


def pct(xs: list[float], q: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    i = min(len(xs) - 1, max(0, math.ceil(q * len(xs)) - 1))
    return xs[i]


def metrics(recs: list[dict], tau: float, tau_met: bool) -> dict:
    n = len(recs)
    top = [ranked(r["probs"]) for r in recs]
    prec, cov, n_shown = gate_stats(recs, tau)
    lat = [r["latency_ms"] for r in recs]
    e = ece(recs)
    return {"n": n, "acc1": sum(t[0] == r["gold"] for t, r in zip(top, recs)) / n if n else None,
            "acc3": sum(r["gold"] in t[:3] for t, r in zip(top, recs)) / n if n else None,
            "ece": None if e is None else round(e, 4), "tau": tau, "tau_target_met": tau_met,
            "shown_precision": None if prec is None else round(prec, 4), "coverage": round(cov, 4),
            "shown": n_shown, "latency_p50_ms": _r1(pct(lat, 0.5)), "latency_p95_ms": _r1(pct(lat, 0.95)),
            "errors": sum(1 for r in recs if r.get("error"))}


def _r1(x):
    return None if x is None else round(x, 1)


def token_stats(items: list[dict]) -> dict:
    toks = [it["tokens"] for it in items]
    if not toks:
        return {"n": 0}
    return {"n": len(toks), "p50": pct(toks, 0.5), "p95": pct(toks, 0.95), "max": max(toks),
            "L128": sum(t <= 128 for t in toks), "L256": sum(128 < t <= 256 for t in toks),
            "L512": sum(256 < t <= 512 for t in toks), "over_512": sum(t > 512 for t in toks),
            "over_budget": sum(it["over_budget"] for it in items)}


# ---- ledger ------------------------------------------------------------------------------------------------

def reports_dir() -> Path:
    return paths.data_dir() / "reports"


def ledger_path() -> Path:
    return reports_dir() / "test-ledger.json"


def read_ledger() -> list[dict]:
    try:
        return json.loads(ledger_path().read_text()).get("entries", [])
    except (OSError, ValueError):
        return []


def check_touch(pred_id: str, form_versions: list[str], test_days: list[str]) -> list[str]:
    """Test days already scored for this (predictor, form versions)."""
    seen = set()
    for e in read_ledger():
        if e.get("predictor") == pred_id and sorted(e.get("forms", [])) == sorted(form_versions):
            seen |= set(e.get("test_days", [])) & set(test_days)
    return sorted(seen)


def record_touch(entries: list[dict]) -> None:
    led = read_ledger() + entries
    _write_private(ledger_path(), json.dumps({"entries": led}, indent=1))


def _write_private(p: Path, text: str) -> None:
    paths.ensure_private_root()
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)


# ---- the run -------------------------------------------------------------------------------------------------

def run(train_days: list[str], test_days: list[str], *, predictors=("markov", "laya"), smoke: bool = False,
        retouch: bool = False, laya_url: str = laya.DEFAULT_URL, tuned_ckpt: str | None = None,
        tuned_url: str | None = None, clients: dict | None = None, manage: bool = True,
        device: str = "mps", reader=read_day, count=None, log=print, kind: str = "replay",
        calib_frac: float = CALIB_FRAC) -> dict:
    """Score `predictors` on the test days; returns the report dict (and writes it under reports/).
    `clients` = {name: object with predict(form, menu, text)} bypasses the HTTP servers (tests)."""
    train_days, test_days = sorted(set(train_days)), sorted(set(test_days))
    check_split(train_days, test_days, smoke)
    predictors = [p for p in predictors if p != "tuned" or tuned_ckpt or (clients and "tuned" in clients)]
    _, ms = load_moments(sorted(set(train_days) | set(test_days)), reader)
    if smoke:
        cut = int(len(ms) * (1 - SMOKE_TEST_FRAC))
        train_ms, test_ms = ms[:cut], ms[cut:]
    else:
        train_ms = [m for m in ms if m["day"] in train_days]
        test_ms = [m for m in ms if m["day"] in test_days]
    if not train_ms or not test_ms:
        raise SplitError(f"empty split: {len(train_ms)} train / {len(test_ms)} test moments")
    ccut = int(len(train_ms) * (1 - calib_frac))
    fit_ms, calib_ms = train_ms[:ccut], train_ms[ccut:]
    markov_cal = Markov.fit(fit_ms, tokens=[m["context"]["token"] for m in fit_ms])
    markov = Markov.fit(train_ms, tokens=[m["context"]["token"] for m in train_ms])
    calib_items = prepare(calib_ms, markov_cal, count)
    test_items = prepare(test_ms, markov, count)
    form_versions = sorted({it["form"]["_version"] for it in test_items})
    ids = {"markov": "markov", "laya": "laya:english", "tuned": f"tuned:{_ckpt_version(tuned_ckpt)}",
           "blend": "blend"}

    first_touch = {}
    if not smoke:
        for p in predictors:
            if p in LAYA_PREDICTORS:
                hit = check_touch(ids[p], form_versions, test_days)
                first_touch[p] = not hit
                if hit and not retouch:
                    raise SplitError(f"test day(s) {hit} already scored for {ids[p]} with these forms: "
                                     "the test split is touched once (pass --retouch to override; never promotable)")

    results, temps, laya_recs = {}, {}, {}
    for p in predictors:
        if p == "markov":
            cal, tst = score(calib_items, None, "markov"), score(test_items, None, "markov")
        else:
            if clients and p in clients:
                cal, tst = (score(calib_items, clients[p].predict, p, log),
                            score(test_items, clients[p].predict, p, log))
            else:
                ckpt = tuned_ckpt if p == "tuned" else None
                url = (tuned_url or f"http://127.0.0.1:{laya.TUNED_PORT}") if p == "tuned" else laya_url
                cli = laya.LayaClient(url, timeout_s=LAYA_TIMEOUT_S, model=None if ckpt else "english",
                                      checkpoint=ids[p])          # raw probabilities: temperatures fitted below
                ctx = laya.managed_server(url, ckpt, device=device, log=log) if manage else _null()
                with ctx:
                    log(f"{p}: {len(calib_items)} calib + {len(test_items)} test moments")
                    cal, tst = score(calib_items, cli.predict, p, log), score(test_items, cli.predict, p, log)
            temps[p] = fit_temperatures(cal)
            cal, tst = with_temperatures(cal, temps[p]), with_temperatures(tst, temps[p])
            laya_recs[p] = (cal, tst)
        tau, met = calibrate_tau(cal)
        results[p] = metrics(tst, tau, met)
        results[p]["calib"] = metrics(cal, tau, met)
    base = "tuned" if "tuned" in laya_recs else ("laya" if "laya" in laya_recs else None)
    if base and "markov" in results:
        mk_cal, mk_tst = score(calib_items, None, "markov"), score(test_items, None, "markov")
        bl = [_blend(m, l) for m, l in zip(mk_cal, laya_recs[base][0])]
        bt = [_blend(m, l) for m, l in zip(mk_tst, laya_recs[base][1])]
        tau, met = calibrate_tau(bl)
        results["blend"] = metrics(bt, tau, met) | {"of": base}

    golds = [it["gold"] for it in test_items]
    in_menu = [g for g in golds if g is not None]
    majority = max(sorted(set(in_menu)), key=in_menu.count) if in_menu else None   # the best constant guess
    report = {
        "kind": kind, "created_at": _now(), "smoke": smoke, "retouched": bool(retouch and any(
            not v for v in first_touch.values())),
        "train_days": train_days, "test_days": test_days, "first_touch": first_touch,
        "moments": {"train": len(train_ms), "calib": len(calib_ms), "test": len(test_ms)},
        "forms": form_versions, "per_form_test": {f: sum(1 for it in test_items if it["form"]["_version"] == f)
                                                  for f in form_versions},
        "label_in_menu": round(sum(it["gold"] is not None for it in test_items) / len(test_items), 4),
        "nothing_share": round(sum(it["label"] == NOTHING for it in test_items) / len(test_items), 4),
        "majority_baseline": {"label": majority if majority in (NOTHING, None) else "<one option>",
                              "acc": round(in_menu.count(majority) / len(golds), 4) if golds else None},
        "tokens_per_state": token_stats(test_items), "tokenizer": _tokenizer_name(count),
        "predictors": {p: ids.get(p, p) for p in results}, "results": results, "temperatures": temps,
        "tuned_checkpoint": tuned_ckpt,
    }
    write_report(report)
    if not smoke:
        entries = [{"test_days": test_days, "predictor": ids[p], "forms": form_versions, "at": report["created_at"],
                    "report": report["path"]} for p in predictors if p in LAYA_PREDICTORS]
        if entries:
            record_touch(entries)
    return report


def _blend(m: dict, l: dict) -> dict:
    return dict(m, probs={k: 0.5 * m["probs"].get(k, 0) + 0.5 * l["probs"].get(k, 0) for k in m["probs"]},
                latency_ms=l["latency_ms"])


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def _ckpt_version(ckpt: str | None) -> str:
    meta = laya.load_checkpoint_meta(ckpt) if ckpt else None
    return (meta or {}).get("version") or (Path(ckpt).name if ckpt else "none")


def _tokenizer_name(count) -> str:
    if count is not None:
        return "custom"
    from .tokcount import tokenizer_name
    return tokenizer_name()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- report files ------------------------------------------------------------------------------------------

def _f(x, pct_=False):
    if x is None:
        return "-"
    return f"{100 * x:.1f} %" if pct_ else (f"{x:.0f}" if isinstance(x, (int, float)) and x >= 10 else f"{x}")


def to_markdown(r: dict) -> str:
    lines = [f"# oi {r['kind']} — {r['created_at']}", ""]
    if r["smoke"]:
        lines += ["**SMOKE TEST, not a result**: one day split chronologically (first 80 % train, last 20 % "
                  "test); never in the test ledger, never promotable.", ""]
    lines += [f"- train days: {', '.join(r['train_days'])}; **test days: {', '.join(r['test_days'])}**"
              f" (first touch: {r['first_touch'] or 'n/a'}; retouched: {r['retouched']})",
              f"- moments: train {r['moments']['train']} (calib slice {r['moments']['calib']}), "
              f"test {r['moments']['test']} {r['per_form_test']}",
              f"- gold label in the menu (ceiling): {_f(r['label_in_menu'], True)}; majority baseline "
              f"({r['majority_baseline']['label']}): {_f(r['majority_baseline']['acc'], True)}",
              f"- tokens per state ({r['tokenizer']}): {r['tokens_per_state']}", ""]
    lines += ["| predictor | acc@1 | acc@3 | ECE | τ (calib) | shown-precision | coverage | latency p50 / p95 ms "
              "| errors |", "|---|---|---|---|---|---|---|---|---|"]
    for p, m in r["results"].items():
        lines.append(f"| {r['predictors'].get(p, p)} | {_f(m['acc1'], True)} | {_f(m['acc3'], True)} | "
                     f"{_f(m['ece'])} | {m['tau']}{'' if m['tau_target_met'] else ' (40 % not reached)'} | "
                     f"{_f(m['shown_precision'], True)} | {_f(m['coverage'], True)} | "
                     f"{_f(m['latency_p50_ms'])} / {_f(m['latency_p95_ms'])} | {m['errors']} |")
    if r["temperatures"]:
        lines += ["", f"Per-form temperatures (fitted on the calib slice): {r['temperatures']}"]
    if r.get("tuned_checkpoint"):
        lines += [f"Tuned checkpoint: `{r['tuned_checkpoint']}`"]
    return "\n".join(lines) + "\n"


def write_report(r: dict) -> None:
    stamp = r["created_at"].replace(":", "").replace("-", "")
    base = reports_dir() / f"{r['kind']}-{stamp}{'-smoke' if r['smoke'] else ''}"
    r["path"] = str(base) + ".json"
    _write_private(Path(str(base) + ".json"), json.dumps(r, indent=1, ensure_ascii=False, default=list))
    _write_private(Path(str(base) + ".md"), to_markdown(r))

