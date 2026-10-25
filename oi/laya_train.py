"""`oi laya-train` and `oi laya-promote` (W13): head-only fine-tune of Laya on our moments + the gate.

Dataset (design §4, "the form is the training format"): every moment of the TRAIN days becomes one laya-ft row
{id, state (the rendered form state), question (the form's `choice` question over the candidate menu, exactly as
served), label (the gold menu key: the next token within 120 s, or `nothing`), split}. Moments whose label is
not in the menu cannot be learnt from that menu and are skipped (counted). Positives from feedback:
  - a moment followed by a HUD press (`hud_pressed`) is repeated x3 in train (design §4: pressed = strong +);
  - a moment followed by a wish (`wish:<slug>`) whose slug names a menu option (its app / host / page words) adds
    a row labelled with that option (a labelled miss: what the HUD should have offered); unmatched wishes are
    counted, not guessed.
Splits by day and time, never shuffled: train days -> first 70 % train / next 15 % val (early stopping) / last
15 % calib (per-form temperature + gate τ, via the replay harness); test days -> scored once at the end
(`oi replay` machinery, ledger). Class weighting: inverse label frequency, mean 1, capped at 10
(class_weights.json, laya-ft `--class-weight file`).

Training = $OI_LAYA_DIR/laya-ft (`python -m laya_ft.train`) in the laya venv, FROZEN encoder +
embedding cache (head only), CPU fp32, `--max-process-mb 4096` (laya-ft's own hard RSS+MPS cap, checked every
step) and `--min-avail-mb 900`, early stopping (patience 2). Before starting: the memory rule (swap < 4 GB, no
other heavy ML process, i.e. no laya server either: one heavy job at a time).

Output: <data dir>/models/laya/<version>/ = laya-ft's run dir (final/ = the checkpoint), data/ (0700), and
oi-laya.json {version, base, train_days, test_days, temperatures, tau, report}. The report (markdown + JSON) is
under reports/. `oi laya-promote --report R` applies the design §5 pass bar and only then sets
settings.live.predictor (+ laya_checkpoint, laya_url).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import laya, paths, replay, settings
from .markov import Markov

TRAIN_FRAC, VAL_FRAC = 0.70, 0.15            # the rest of the train days = calib slice
PRESS_REPEAT = 3
CW_CAP = 10.0
PASS_BAR = {"beats_markov_at3_by": 0.03, "shown_precision_min": 0.40, "coverage_min": 0.30, "ece_max": 0.08,
            "min_test_moments": 300, "state_tokens_max": 256}


# ---- dataset ---------------------------------------------------------------------------------------------

def _words(s: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", s.lower()) if len(w) >= 3}


def wish_key(menu: list[dict], slug: str) -> str | None:
    """The menu option a wish slug names: the option whose app / host / page words overlap the slug most."""
    want = _words(slug)
    best, best_n = None, 0
    for o in menu:
        if o["key"] == "nothing":
            continue
        have = _words(" ".join(str(v) for v in (o.get("params") or {}).values()) + " " + (o.get("token") or ""))
        n = len(want & have)
        if n > best_n:
            best, best_n = o["key"], n
    return best


def rows_for(items: list[dict], split: str, *, feedback: bool) -> tuple[list[dict], Counter]:
    out, c = [], Counter()
    for it in items:
        m = it["moment"]
        q = {"type": "choice", "instructions": it["form"]["question"]["instructions"],
             "criteria": {o["key"]: o["criteria"] for o in it["menu"]}}
        base = {"state": it["text"], "question": q, "split": split, "form": it["form"]["form_id"], "day": m["day"]}
        if it["gold"] is None:
            c["label_not_in_menu"] += 1
        else:
            reps = PRESS_REPEAT if (feedback and m.get("hud_pressed")) else 1
            c["pressed_x3"] += reps > 1
            for r in range(reps):
                out.append({**base, "id": f"{m['ts']}#{r}", "label": it["gold"]})
        if feedback and m.get("wish"):
            k = wish_key(it["menu"], m["wish"])
            if k:
                out.append({**base, "id": f"{m['ts']}#wish", "label": k})
                c["wish_rows"] += 1
            else:
                c["wish_unmatched"] += 1
    c["rows"] = len(out)
    return out, c


def class_weights(rows: list[dict], cap: float = CW_CAP) -> dict:
    n = Counter(r["label"] for r in rows)
    if not n:
        return {"weights": {}, "cap": cap}
    raw = {k: min(cap, len(rows) / (len(n) * v)) for k, v in n.items()}
    mean = sum(raw.values()) / len(raw)
    return {"weights": {k: round(v / mean, 6) for k, v in sorted(raw.items())}, "cap": cap}


def build_dataset(train_days: list[str], test_days: list[str], out_dir: Path, *, reader=replay.read_day,
                  count=None) -> dict:
    """Writes train/val/calib/test.jsonl + class_weights.json; returns counts. Split by day, then by time."""
    replay.check_split(train_days, test_days)
    _, ms = replay.load_moments(sorted(set(train_days) | set(test_days)), reader)
    train_ms = [m for m in ms if m["day"] in train_days]
    test_ms = [m for m in ms if m["day"] in test_days]
    a, b = int(len(train_ms) * TRAIN_FRAC), int(len(train_ms) * (TRAIN_FRAC + VAL_FRAC))
    fit_ms, val_ms = train_ms[:a], train_ms[a:b]
    if not fit_ms or not val_ms or not test_ms:
        raise replay.SplitError(f"too few moments: train {len(fit_ms)}, val {len(val_ms)}, test {len(test_ms)}")
    mk_fit = Markov.fit(fit_ms, tokens=[m["context"]["token"] for m in fit_ms])
    mk_all = Markov.fit(train_ms, tokens=[m["context"]["token"] for m in train_ms])
    tr, ctr = rows_for(replay.prepare(fit_ms, mk_fit, count), "train", feedback=True)
    va, cva = rows_for(replay.prepare(val_ms, mk_fit, count), "val", feedback=False)
    te, cte = rows_for(replay.prepare(test_ms, mk_all, count), "test", feedback=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    for name, rows in (("train", tr), ("val", va), ("test", te)):
        replay._write_private(out_dir / f"{name}.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                                               for r in rows))
    replay._write_private(out_dir / "class_weights.json", json.dumps(class_weights(tr), indent=1))
    return {"train": dict(ctr), "val": dict(cva), "test": dict(cte),
            "moments": {"train_fit": len(fit_ms), "val": len(val_ms), "calib": len(train_ms) - b,
                        "test": len(test_ms)}}


# ---- training --------------------------------------------------------------------------------------------

def models_dir() -> Path:
    return paths.data_dir() / "models" / "laya"


def new_version(now: datetime | None = None) -> str:
    return "oi-laya-" + (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%MZ")


def base_model_dir() -> str | None:
    """The cached stock checkpoint laya-ft starts from: $OI_LAYA_BASE_MODEL_DIR, else the newest Hugging Face cache
    snapshot of convaiinnovations/laya (laya-ft's own default, laya-local/hf-cache, is empty since the repair)."""
    import glob
    env = os.environ.get("OI_LAYA_BASE_MODEL_DIR")
    if env:
        return env
    hits = sorted(glob.glob(os.path.expanduser(
        "~/.cache/huggingface/hub/models--convaiinnovations--laya/snapshots/*/model.safetensors")))
    return str(Path(hits[-1]).parent) if hits else None


def train_command(data_dir: Path, run_dir: Path, *, epochs: int = 6, max_steps: int = 0,
                  max_process_mb: int = 4096) -> tuple[list[str], dict]:
    ldir = laya.laya_dir()
    argv = [str(ldir / "laya" / ".venv" / "bin" / "python"), "-m", "laya_ft.train",
            "--data-dir", str(data_dir), "--output-dir", str(run_dir),
            "--device", "cpu", "--dtype", "fp32", "--freeze-encoder", "--embed-cache-dir", str(run_dir / "embed_cache"),
            "--objective", "rlcd", "--optimizer", "adafactor", "--group-size", "2",
            "--epochs", str(epochs), "--micro-batch", "4", "--grad-accum", "4",
            "--class-weight", "file", "--early-stop-patience", "2",
            "--min-avail-mb", "900", "--max-process-mb", str(max_process_mb), "--no-resume", "--log-every", "10"]
    if max_steps:
        argv += ["--max-steps", str(max_steps)]
    env = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false",
           "PYTHONPATH": str(ldir / "laya-ft" / "src")}
    base = base_model_dir()
    if base:
        env["LAYA_BASE_MODEL_DIR"] = base
    return argv, env


def train(train_days: list[str], test_days: list[str], *, epochs: int = 6, max_steps: int = 0,
          evaluate: bool = True, reader=replay.read_day, runner=subprocess.run, preflight=laya.memory_preflight,
          clients: dict | None = None, log=print, device: str = "mps") -> dict:
    """Build the dataset, run laya-ft (head only), then calibrate + score on the test days once."""
    ok, why = preflight()
    if not ok:
        raise laya.LayaError(f"memory rule: {why}")
    version = new_version()
    ckpt = models_dir() / version
    data = ckpt / "data"
    counts = build_dataset(train_days, test_days, data, reader=reader)
    log(f"{version}: dataset {counts}")
    argv, env = train_command(data, ckpt, epochs=epochs, max_steps=max_steps)
    log("training: " + " ".join(argv[1:4]) + " … (frozen encoder, cpu fp32, --max-process-mb 4096)")
    p = runner(argv, env={**os.environ, **env})
    if getattr(p, "returncode", 0) != 0:
        raise RuntimeError(f"laya-ft exited {p.returncode}; see its log")
    meta = {"version": version, "base": "convaiinnovations/laya (english)", "train_days": sorted(train_days),
            "test_days": sorted(test_days), "dataset": counts, "epochs": epochs, "max_steps": max_steps,
            "created_at": replay._now(), "temperatures": {}, "tau": None, "report": None}
    replay._write_private(ckpt / "oi-laya.json", json.dumps(meta, indent=1))
    if not evaluate:
        return meta
    rep = replay.run(train_days, test_days, predictors=("markov", "tuned"), tuned_ckpt=str(ckpt),
                     clients=clients, reader=reader, log=log, kind="laya-train", device=device,
                     calib_frac=1 - TRAIN_FRAC - VAL_FRAC)
    meta.update(temperatures=rep["temperatures"].get("tuned", {}), tau=rep["results"]["tuned"]["tau"],
                report=rep["path"])
    replay._write_private(ckpt / "oi-laya.json", json.dumps(meta, indent=1))
    meta["report_obj"] = rep
    return meta


# ---- promotion ----------------------------------------------------------------------------------------------

def pass_bar(report: dict, predictor: str = "tuned") -> tuple[bool, list[tuple[str, bool, str]]]:
    """Design §5 / docs/DESIGN.md pass bar, every check listed (name, ok, detail)."""
    res = report.get("results", {})
    t, m = res.get(predictor) or {}, res.get("markov") or {}
    toks = report.get("tokens_per_state") or {}
    checks = [
        ("not a smoke test", not report.get("smoke"), f"smoke={report.get('smoke')}"),
        ("test split touched once", not report.get("retouched") and bool(report.get("first_touch", {}).get(
            t.get("of", predictor))), f"first_touch={report.get('first_touch')}"),
        (f"≥ {PASS_BAR['min_test_moments']} test moments", (t.get("n") or 0) >= PASS_BAR["min_test_moments"],
         f"n={t.get('n')}"),
        ("beats Markov@3 by ≥ 3 pts", t.get("acc3") is not None and m.get("acc3") is not None
         and t["acc3"] - m["acc3"] >= PASS_BAR["beats_markov_at3_by"] - 1e-12,
         f"{t.get('acc3')} vs {m.get('acc3')}"),
        ("shown-precision ≥ 40 %", (t.get("shown_precision") or 0) >= PASS_BAR["shown_precision_min"],
         f"{t.get('shown_precision')}"),
        ("coverage ≥ 30 %", (t.get("coverage") or 0) >= PASS_BAR["coverage_min"], f"{t.get('coverage')}"),
        ("ECE ≤ 0.08", t.get("ece") is not None and t["ece"] <= PASS_BAR["ece_max"], f"{t.get('ece')}"),
        ("state fits L256", (toks.get("max") or 10**9) <= PASS_BAR["state_tokens_max"], f"max={toks.get('max')}"),
    ]
    return all(ok for _, ok, _ in checks), checks


def promote(report_path: str, *, predictor: str = "laya", dry_run: bool = False) -> tuple[bool, list]:
    """Apply the pass bar to a laya-train report; on pass set settings.live.laya_rank (+ checkpoint, url).
    Run by hand: the switch stays a deliberate decision."""
    if predictor not in ("laya", "blend"):
        raise ValueError("predictor must be laya or blend")
    rep = json.loads(Path(report_path).read_text())
    scored = "tuned" if "tuned" in rep.get("results", {}) else "laya"
    ok, checks = pass_bar(rep, "blend" if predictor == "blend" else scored)
    if ok and not dry_run:
        ckpt = rep.get("tuned_checkpoint")
        settings.update_live({"predictor": predictor, "laya_rank": {"laya": "live", "blend": "blend"}[predictor],
                              "laya_checkpoint": ckpt,
                              "laya_url": f"http://127.0.0.1:{laya.TUNED_PORT}" if ckpt else laya.DEFAULT_URL,
                              "promoted_report": report_path})
    return ok, checks


def main_train(args) -> int:
    try:
        meta = train(args.train_days, args.test_days, epochs=args.epochs, max_steps=args.max_steps,
                     evaluate=not args.no_eval, device=args.device)
    except (laya.LayaError, replay.SplitError, RuntimeError, FileNotFoundError) as e:
        print(f"laya-train: {e}", file=sys.stderr)
        return 2
    print(f"checkpoint: {models_dir() / meta['version']}")
    if meta.get("report"):
        print(f"report: {meta['report'][:-5]}.md")
    return 0
