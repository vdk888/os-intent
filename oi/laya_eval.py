"""W13 follow-up: measure Laya's latency on real-shaped requests and evaluate the shadow log.

`oi laya-bench`: the requests are built by the code `oi live` uses (moments of a token day -> form route ->
candidate menu -> serialised state -> `laya.build_request`), sent to the RUNNING server (never starts one: the
memory rule allows one heavy ML process), with a long client timeout so a slow call is measured, not dropped.
The first call is reported apart (`first_ms`: cold if the server has not answered since it started), then
p50/p95/max over the next `n`, back to back (`--gap 0`) or spaced like live use (`--gap 5`): measured on
2026-10-20, the first call after an idle gap of >= 2 s costs 3-4x a back-to-back one (p95 655 vs 162 ms) (the GPU idles down), so
the live timeout comes from the SPACED p95. `suggested_timeout_ms` = warm p95 x 1.5 + 50 ms, rounded up to 50 ms: the value
for settings `live.laya_timeout_ms`.

`oi laya-report`: from the hud-log, every row with a Laya shadow result, against what the user actually did:
  pick  = a HUD press (`pressed:<k>`): the token of the option he pressed (needs the shadow's key->token map);
  next  = the next action within 120 s (moments.LABEL_WINDOW_S) in tokens/<day>, `nothing` when none came:
          every evaluated moment has one, so this reaches a decision far sooner than presses alone.
For each: Laya and Markov top-1 / top-3 hit rates on the SAME menu (only rows where Laya answered), and how
often they agree on the top option. `promotion` applies PROMOTION (below) and only says whether the bar is met:
switching `live.laya_rank` is the user's decision (settings), never automatic.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone

from . import laya, normalise, paths, replay
from .markov import Markov, latest_model_path
from .moments import EVENT_PREFIX, FEEDBACK_PREFIXES, LABEL_WINDOW_S, NOTHING, build_moments

BENCH_TIMEOUT_S = 30.0
TIMEOUT_MARGIN = 1.5
TIMEOUT_PAD_MS = 50

# Promotion bar. Same N and top-3 gain as the offline design §5 bar (`oi laya-promote`:
# >= 300 test moments, Markov@3 + 3 pts), here on LIVE shadow data. Over the `next` labels of rows Laya answered:
#   n >= min_n moments over >= min_days days; Laya top-3 >= Markov top-3 + top3_gain (absolute);
#   Laya top-1 >= Markov top-1 - top1_slack; Laya error rate <= max_error_rate of the shadow calls;
#   HUD picks: >= min_picks and Laya top-3 >= Markov top-3 (no gain asked: the user could only press what MARKOV
#   showed, so picks favour Markov by construction; "not worse" there is the honest bar).
# All pass -> "live" may be set; all but top-1 -> "blend" (the cautious step). Never applied automatically.
PROMOTION = {"min_n": 300, "min_days": 3, "top3_gain": 0.03, "top1_slack": 0.0, "max_error_rate": 0.10,
             "min_picks": 50}


def pct(xs: list[float], q: float) -> float | None:
    return replay.pct(xs, q)


def suggested_timeout_ms(p95_ms: float | None) -> int | None:
    if p95_ms is None:
        return None
    return int(math.ceil((p95_ms * TIMEOUT_MARGIN + TIMEOUT_PAD_MS) / 50.0) * 50)


# ---- bench ------------------------------------------------------------------------------------------------

def bench(day: str, n: int = 50, *, gap_s: float = 0.0, url: str = laya.DEFAULT_URL, client=None,
          model: Markov | None = None, log=None, sleep=time.sleep, reader=None) -> dict:
    """Latency of the running server on the last n+1 moments of `day` (first one = the cold/first call).
    `gap_s` > 0 spaces the calls like live use (moments come seconds apart): the server idles between them."""
    if client is None:
        if not laya.health(url):
            raise laya.LayaError("server down (laya-bench never starts one)")
        client = laya.LayaClient(url, timeout_s=BENCH_TIMEOUT_S)
    if model is None:
        p = latest_model_path()
        if p is None:
            raise FileNotFoundError("no Markov model under models/")
        model = Markov.load(p)
    _, ms = replay.load_moments([day], reader=reader or replay.read_day)
    items = [it for it in replay.prepare(ms[-(n + 1) * 2:], model) if it["menu"]][-(n + 1):]
    if not items:
        raise FileNotFoundError(f"no moments on {day}")
    lat, errors, wall = [], {}, []
    for i, it in enumerate(items):
        if i and gap_s > 0:
            sleep(gap_s)
        t0 = time.monotonic()
        try:
            lat.append(client.predict(it["form"], it["menu"], it["text"])["latency_ms"])
        except laya.LayaError as e:
            errors[e.reason] = errors.get(e.reason, 0) + 1
            lat.append(None)
        wall.append(int((time.monotonic() - t0) * 1000))
        if log:
            log(f"  {len(wall)}/{len(items)}: {lat[-1]} ms")
    first, warm = lat[0], [x for x in lat[1:] if x is not None]
    p95 = pct(warm, 0.95)
    return {"day": day, "gap_s": gap_s, "n_warm": len(warm), "first_ms": first, "p50_ms": pct(warm, 0.5), "p95_ms": p95,
            "max_ms": max(warm) if warm else None, "errors": errors,
            "tokens_p50": pct([it["tokens"] for it in items], 0.5),
            "options_p50": pct([len(it["menu"]) for it in items], 0.5),
            "suggested_timeout_ms": suggested_timeout_ms(p95)}


# ---- shadow report ----------------------------------------------------------------------------------------

def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def next_action(tokens: list[dict], ts: str) -> str:
    """The first action token after `ts` within LABEL_WINDOW_S (events and feedback skipped), else nothing."""
    t = _ts(ts)
    end = t + timedelta(seconds=LABEL_WINDOW_S)
    for r in tokens:
        rt = _ts(r["ts"])
        if rt <= t:
            continue
        if rt > end:
            break
        if r["token"].startswith(FEEDBACK_PREFIXES) or r["token"].startswith(EVENT_PREFIX):
            continue
        return r["token"]
    return NOTHING


def _ranked(probs: dict[str, float]) -> list[str]:
    return [k for k, _ in sorted(probs.items(), key=lambda kv: (-kv[1], kv[0]))]


def _picked_token(row: dict) -> str | None:
    out = str(row.get("outcome") or "")
    if not out.startswith("pressed:"):
        return None
    k = out.split(":", 1)[1]
    for o in row.get("options") or []:
        if str(o.get("key")) == k:
            return o.get("token")
    return row.get("executed")


def _acc(recs: list[tuple[list[str], list[str], str]]) -> dict:
    """recs = (laya ranking, markov ranking, gold key)."""
    n = len(recs)
    if not n:
        return {"n": 0}
    f = lambda xs: round(sum(xs) / n, 3)
    return {"n": n,
            "laya_top1": f([r[0][:1] == [r[2]] for r in recs]), "markov_top1": f([r[1][:1] == [r[2]] for r in recs]),
            "laya_top3": f([r[2] in r[0][:3] for r in recs]), "markov_top3": f([r[2] in r[1][:3] for r in recs])}


def report(days: list[str], *, hud_reader=normalise.read_hud_log, tokens_reader=replay.read_day) -> dict:
    errors: dict[str, int] = {}
    calls, agree, lat, used_days = 0, [], [], set()
    pick, nxt = [], []
    for day in days:
        rows = [r for r in hud_reader(day) if isinstance(r.get("laya"), dict)]
        if not rows:
            continue
        toks = None
        for r in rows:
            L = r["laya"]
            calls += 1
            if L.get("error"):
                errors[L["error"]] = errors.get(L["error"], 0) + 1
                continue
            lp, mp, k2t = L.get("probs") or {}, L.get("markov_probs") or {}, L.get("tokens") or {}
            if not lp or not mp or not k2t:
                continue            # an older shadow row (no Markov ranking / key->token map): not scorable
            used_days.add(day)
            if L.get("latency_ms") is not None:
                lat.append(L["latency_ms"])
            lr, mr = _ranked(lp), _ranked(mp)
            agree.append(lr[0] == mr[0])
            t2k = {t: k for k, t in k2t.items()}
            if toks is None:
                try:
                    toks = sorted(tokens_reader(day), key=lambda x: x["ts"])
                except FileNotFoundError:
                    toks = []
            gold = t2k.get(next_action(toks, r["ts"]))
            if gold is not None:
                nxt.append((lr, mr, gold))
            p = _picked_token(r)
            if p is not None and p in t2k:
                pick.append((lr, mr, t2k[p]))
    out = {"days": days, "shadow_calls": calls, "errors": errors,
           "error_rate": round(sum(errors.values()) / calls, 3) if calls else None,
           "scored_days": len(used_days), "top1_agreement": round(sum(agree) / len(agree), 3) if agree else None,
           "latency_p50_ms": pct(lat, 0.5), "latency_p95_ms": pct(lat, 0.95),
           "next": _acc(nxt), "pick": _acc(pick)}
    out["promotion"] = promotion(out)
    return out


def promotion(r: dict, bar: dict = PROMOTION) -> dict:
    """The proposed bar applied to a report: {checks: [(name, ok|None, detail)], verdict}. None = pending."""
    nx, pk = r["next"], r["pick"]
    checks = [("enough moments", nx["n"] >= bar["min_n"] and r["scored_days"] >= bar["min_days"],
               f"{nx['n']} >= {bar['min_n']} over {r['scored_days']} >= {bar['min_days']} days")]
    er = r["error_rate"]
    checks.append(("laya answers", er is not None and er <= bar["max_error_rate"],
                   f"error rate {er} <= {bar['max_error_rate']}"))
    if nx["n"]:
        checks.append(("top-3 gain (next)", nx["laya_top3"] >= nx["markov_top3"] + bar["top3_gain"],
                       f"laya {nx['laya_top3']} >= markov {nx['markov_top3']} + {bar['top3_gain']}"))
        checks.append(("top-1 kept (next)", nx["laya_top1"] >= nx["markov_top1"] - bar["top1_slack"],
                       f"laya {nx['laya_top1']} >= markov {nx['markov_top1']} - {bar['top1_slack']}"))
    else:
        checks.append(("top-3 gain (next)", None, "no scored moment yet"))
    if pk["n"] >= bar["min_picks"]:
        checks.append(("top-3 not worse (picks)", pk["laya_top3"] >= pk["markov_top3"],
                       f"laya {pk['laya_top3']} >= markov {pk['markov_top3']} over {pk['n']} picks"))
    else:
        checks.append(("top-3 not worse (picks)", None, f"{pk['n']} of {bar['min_picks']} picks"))
    hard = [c for c in checks if c[0] != "top-1 kept (next)"]
    if all(c[1] is True for c in checks):
        verdict = "live criteria met (the user may set live.laya_rank = live)"
    elif all(c[1] is True for c in hard):
        verdict = "blend criteria met (the user may set live.laya_rank = blend)"
    elif any(c[1] is None for c in checks) or not checks[0][1]:
        verdict = "pending: not enough shadow data"
    else:
        verdict = "not met: stay in shadow"
    return {"bar": bar, "checks": checks, "verdict": verdict}


def to_markdown(r: dict) -> str:
    def line(name, a):
        if not a["n"]:
            return f"| {name} | 0 | - | - | - | - |"
        return (f"| {name} | {a['n']} | {a['laya_top1']:.0%} | {a['markov_top1']:.0%} | {a['laya_top3']:.0%} "
                f"| {a['markov_top3']:.0%} |")
    out = [f"# Laya shadow report ({r['days'][0]}..{r['days'][-1]})", "",
           f"shadow calls {r['shadow_calls']}, errors {r['errors'] or 0} (rate {r['error_rate']}), "
           f"scored days {r['scored_days']}, Laya/Markov top-1 agreement {r['top1_agreement']}, "
           f"latency p50 {r['latency_p50_ms']} ms p95 {r['latency_p95_ms']} ms", "",
           "| gold | n | Laya top-1 | Markov top-1 | Laya top-3 | Markov top-3 |", "|---|---|---|---|---|---|",
           line("next action (120 s)", r["next"]), line("HUD pick", r["pick"]), "", "## Promotion (proposed bar)", ""]
    for name, ok, detail in r["promotion"]["checks"]:
        out.append(f"- {'PASS' if ok else ('PENDING' if ok is None else 'FAIL')} {name}: {detail}")
    out += ["", f"**{r['promotion']['verdict']}** — the switch (settings `live.laya_rank`) is the user's call."]
    return "\n".join(out)


def write(r: dict) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = replay.reports_dir() / f"laya-shadow-{stamp}.md"
    replay._write_private(p, to_markdown(r) + "\n")
    replay._write_private(p.with_suffix(".json"), json.dumps(r, indent=1) + "\n")
    return str(p)
