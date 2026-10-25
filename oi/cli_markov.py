"""`oi-markov` CLI (W3). W1's `oi` CLI may later absorb these as `oi markov` / `oi render`.

  oi-markov markov --days N        fit on the last N days of $OI_DATA_DIR/tokens/*.jsonl,
                                   save models/markov-<today>.json, print counts only
  oi-markov render --moment <json|file> [--form A|C] [--model PATH] [--opaque]
                                   print the state text, its token count, truncations and
                                   the candidate menu (no Laya call: batch 2)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import candidates, forms, serialise
from .markov import Markov, latest_model_path
from .moments import build_moments
from .paths import tokens_dir
from .tokcount import tokenizer_name


def read_tokens(days: int) -> tuple[list[dict], list[str]]:
    files = sorted(tokens_dir().glob("????-??-??.jsonl"))[-days:]
    rows = []
    for f in files:
        rows += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    return rows, [f.stem for f in files]


def cmd_markov(a) -> int:
    rows, days = read_tokens(a.days)
    if not rows:
        print(f"no token files under {tokens_dir()}", file=sys.stderr)
        return 1
    moments = build_moments(rows)
    model = Markov.fit(moments, tokens=[r["token"] for r in rows], min_count=a.min_count,
                       meta={"days": days, "moments": len(moments)})
    path = model.save(Path(a.out) if a.out else None, day=date.today())
    print(json.dumps({"days": len(days), "tokens": len(rows), "moments": len(moments),
                      "catalogue": len(model.catalogue), "model": str(path)}))
    return 0


def cmd_render(a) -> int:
    src = a.moment
    moment = json.loads(Path(src).read_text() if not src.lstrip().startswith("{") else src)
    form = forms.load_form(a.form) if a.form else forms.route(moment["context"])
    mpath = Path(a.model) if a.model else latest_model_path()
    model = Markov.load(mpath) if mpath and mpath.exists() else None
    text, n, trunc = serialise.render(form, moment, budget=a.budget)
    menu = candidates.build(form, moment, model, opaque=a.opaque,
                            is_executable=(lambda o: True) if a.no_exec_check else candidates.default_is_executable)
    print(json.dumps({"form": form["_version"], "tokenizer": tokenizer_name(), "n_tokens": n,
                      "truncations": trunc, "model": str(mpath) if model else None,
                      "state": {"body": text},
                      "questions": {"next": candidates.as_question(form, menu)}},
                     ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="oi-markov")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("markov", help="fit + save the Markov shortlist model")
    m.add_argument("--days", type=int, default=7)
    m.add_argument("--min-count", type=int, default=5)
    m.add_argument("--out")
    r = sub.add_parser("render", help="render a moment through a form")
    r.add_argument("--moment", required=True, help="moment JSON or a file path")
    r.add_argument("--form", choices=["A", "C"])
    r.add_argument("--model")
    r.add_argument("--budget", type=int, default=serialise.BUDGET)
    r.add_argument("--opaque", action="store_true", help="option keys A..P")
    r.add_argument("--no-exec-check", action="store_true")
    a = p.parse_args(argv)
    return {"markov": cmd_markov, "render": cmd_render}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
