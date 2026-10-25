"""W13 subcommands: laya-serve, replay, laya-train, laya-promote, improve-report; laya-bench, laya-report.
Privacy: nothing personal is printed; reports (local, 0600) live under <data dir>/reports/."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone


def _days(values: list[str]) -> list[str]:
    """Days as YYYY-MM-DD, 'today', or an inclusive range A..B."""
    out = []
    for v in values:
        if v == "today":
            out.append(datetime.now(timezone.utc).date().isoformat())
        elif ".." in v:
            a, b = (date.fromisoformat(x) for x in v.split("..", 1))
            while a <= b:
                out.append(a.isoformat())
                a += timedelta(days=1)
        else:
            out.append(date.fromisoformat(v).isoformat())
    return sorted(set(out))


def cmd_laya_serve(args) -> int:
    from . import laya
    if args.check:
        ok, why = laya.memory_preflight()
        url = f"http://127.0.0.1:{args.port or 8781}"
        print(json.dumps({"memory_ok": ok, "detail": why, "server_up": laya.health(url), "url": url}))
        return 0 if ok else 2
    return laya.supervise(args.checkpoint, args.port, cap_mb=args.cap_mb, device=args.device, model=args.model)


def cmd_replay(args) -> int:
    from . import laya, replay
    try:
        if args.days:
            days = _days(args.days)
            train, test = (days, days) if args.smoke else replay.split_days(days, args.test_last)
        else:
            train, test = _days(args.train_days or []), _days(args.test_days or [])
        r = replay.run(train, test, predictors=args.predictors.split(","), smoke=args.smoke, retouch=args.retouch,
                       laya_url=args.laya_url, tuned_ckpt=args.tuned, manage=not args.no_manage, device=args.device,
                       log=lambda m: print(m, file=sys.stderr))
    except (replay.SplitError, laya.LayaError, FileNotFoundError) as e:
        print(f"replay: {e}", file=sys.stderr)
        return 2
    print(replay.to_markdown(r))
    print(f"report: {r['path'][:-5]}.md")
    return 0


def cmd_laya_train(args) -> int:
    from . import laya_train
    args.train_days, args.test_days = _days(args.train_days), _days(args.test_days)
    return laya_train.main_train(args)


def cmd_laya_promote(args) -> int:
    from . import laya_train
    ok, checks = laya_train.promote(args.report, predictor=args.predictor, dry_run=args.dry_run)
    for name, good, detail in checks:
        print(f"{'PASS' if good else 'FAIL'}  {name}  ({detail})")
    if ok:
        print("promoted: settings.live.predictor = " + args.predictor + (" (dry run, not written)" if args.dry_run else ""))
        return 0
    print("not promoted: the HUD stays on Markov (Laya keeps running in shadow)")
    return 1


def cmd_improve_report(args) -> int:
    from . import improve
    days = _days(args.days) if args.days else improve.default_days(args.last)
    path = improve.write(improve.report(days))
    print(f"improvement report ({len(days)} days): {path}")
    return 0


def cmd_laya_bench(args) -> int:
    from . import laya, laya_eval, settings
    url = args.url or settings.load()["live"]["laya_url"]
    try:
        r = laya_eval.bench(_days([args.day])[0], args.n, gap_s=args.gap, url=url,
                            log=(lambda m: print(m, file=sys.stderr)) if args.verbose else None)
    except (laya.LayaError, FileNotFoundError) as e:
        print(f"laya-bench: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r))
    return 0


def cmd_laya_report(args) -> int:
    from . import laya_eval
    days = _days(args.days) if args.days else [(datetime.now(timezone.utc).date() - timedelta(days=i)).isoformat()
                                               for i in range(args.last - 1, -1, -1)]
    r = laya_eval.report(days)
    print(laya_eval.to_markdown(r))
    print(f"report: {laya_eval.write(r)}")
    return 0


def register(sub) -> None:
    s = sub.add_parser("laya-serve", help="run ONE laya server under the memory rule (LaunchAgent com.osintent.oi-laya)")
    s.add_argument("--checkpoint", help="an oi checkpoint dir (oi laya-train); default stock English laya")
    s.add_argument("--port", type=int, help="default 8781 (stock) / 8784 (checkpoint)")
    s.add_argument("--device", default="mps", choices=["mps", "cpu"])
    s.add_argument("--model", default="english", help="laya-serve checkpoint name (one only)")
    s.add_argument("--cap-mb", type=float, default=4096, help="kill the server above this footprint (RSS + MPS)")
    s.add_argument("--check", action="store_true", help="print the memory preflight and server health, then exit")
    s.set_defaults(func=cmd_laya_serve)

    r = sub.add_parser("replay", help="offline replay: Markov vs Laya on logged moments (read-only on tokens)")
    r.add_argument("--days", nargs="+", help="days (YYYY-MM-DD, A..B, today); the last --test-last are test")
    r.add_argument("--test-last", type=int, default=2)
    r.add_argument("--train-days", nargs="+")
    r.add_argument("--test-days", nargs="+")
    r.add_argument("--smoke", action="store_true", help="one day, split by time: a smoke test, never a result")
    r.add_argument("--retouch", action="store_true", help="re-score test days already scored (never promotable)")
    r.add_argument("--predictors", default="markov,laya", help="comma list of markov,laya,tuned")
    r.add_argument("--tuned", help="oi checkpoint dir for the tuned predictor")
    r.add_argument("--laya-url", default="http://127.0.0.1:8781")
    r.add_argument("--no-manage", action="store_true", help="never start/stop a laya server (use the running one)")
    r.add_argument("--device", default="mps", choices=["mps", "cpu"])
    r.set_defaults(func=cmd_replay)

    t = sub.add_parser("laya-train", help="head-only fine-tune of Laya on moments (laya-ft), then score once")
    t.add_argument("--train-days", nargs="+", required=True)
    t.add_argument("--test-days", nargs="+", required=True)
    t.add_argument("--epochs", type=int, default=6)
    t.add_argument("--max-steps", type=int, default=0, help="cap optimizer steps (smoke)")
    t.add_argument("--no-eval", action="store_true", help="train only; do not touch the test days")
    t.add_argument("--device", default="mps", choices=["mps", "cpu"], help="device of the scoring server")
    t.set_defaults(func=cmd_laya_train)

    p = sub.add_parser("laya-promote", help="apply the design §5 pass bar; on pass set settings.live.predictor")
    p.add_argument("--report", required=True, help="a laya-train report JSON")
    p.add_argument("--predictor", default="laya", choices=["laya", "blend"])
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_laya_promote)

    b = sub.add_parser("laya-bench", help="latency of the RUNNING laya server on real-shaped requests (never starts one)")
    b.add_argument("--day", default="today", help="token day the requests are built from (YYYY-MM-DD or today)")
    b.add_argument("-n", type=int, default=50, help="requests after the first one")
    b.add_argument("--gap", type=float, default=0.0, help="seconds between calls (5 = like live use)")
    b.add_argument("--url", help="default settings live.laya_url")
    b.add_argument("-v", "--verbose", action="store_true")
    b.set_defaults(func=cmd_laya_bench)

    lr = sub.add_parser("laya-report", help="shadow evaluation: Laya vs Markov top-1/top-3 vs what the user did")
    lr.add_argument("--days", nargs="+", help="days (YYYY-MM-DD, A..B, today); default the last --last days")
    lr.add_argument("--last", type=int, default=7)
    lr.set_defaults(func=cmd_laya_report)

    i = sub.add_parser("improve-report", help="weekly improvement input for an offline improvement pass (local report)")
    i.add_argument("--days", nargs="+")
    i.add_argument("--last", type=int, default=7)
    i.set_defaults(func=cmd_improve_report)
