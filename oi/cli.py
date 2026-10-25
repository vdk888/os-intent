"""`oi` command line. Privacy: no window title, URL or slug is printed unless asked (--show)."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from urllib.error import URLError

from . import aw, normalise, paths
from .vocab import parse, validate_row

# W3 hook: Markov/serialise/candidates subcommands register from oi/cli_markov.py (not in W1).


def _day(value: str) -> str:
    if value == "today":
        return datetime.now(timezone.utc).date().isoformat()
    datetime.strptime(value, "%Y-%m-%d")
    return value


def cmd_pull(args: argparse.Namespace) -> int:
    try:
        results = aw.pull(since=args.since)
    except URLError as e:
        print(f"aw-server unreachable at {aw.server_url()}: {e.reason}", file=sys.stderr)
        return 2
    for r in results:
        print(f"{r['bucket']}: fetched {r['fetched']}, new {r['new']}")
    print(f"data dir: {paths.data_dir()}")
    return 0


def cmd_tokens(args: argparse.Namespace) -> int:
    path, rows = normalise.build_day(args.day)
    bad = sum(1 for r in rows if validate_row(r))
    print(f"{args.day}: {len(rows)} tokens ({bad} invalid) -> {path}")
    if args.show:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
    return 1 if bad else 0


def stats(rows: list[dict], top: int = 10) -> dict:
    kinds = Counter(parse(r["token"])[0] for r in rows)
    apps = Counter(r["app"] for r in rows if r["token"].startswith("activate:"))
    secs = Counter()
    for r in rows:
        if r["token"].startswith("activate:"):
            secs[r["app"]] += r["dur"]
    return {
        "tokens": len(rows),
        "kinds": dict(kinds.most_common()),
        "top_apps": [{"app": a, "activations": n, "minutes": round(secs[a] / 60, 1)} for a, n in apps.most_common(top)],
        "distinct_tokens": len({r["token"] for r in rows}),
        "wishes": kinds.get("wish", 0),
        "hud_shown": sum(1 for r in rows if r["token"] == "hud:shown"),
        "hud_pressed": sum(1 for r in rows if r["token"].startswith("hud:pressed:")),
    }


def cmd_stats(args: argparse.Namespace) -> int:
    path = paths.tokens_dir() / f"{args.day}.jsonl"
    if not path.exists():
        print(f"no tokens for {args.day}; run `oi tokens --day {args.day}` first", file=sys.stderr)
        return 1
    s = stats(aw.read_jsonl(path), args.top)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    print(f"{args.day}: {s['tokens']} tokens, {s['distinct_tokens']} distinct, {s['wishes']} wishes, "
          f"HUD shown {s['hud_shown']}, pressed {s['hud_pressed']}")
    print("kinds: " + ", ".join(f"{k}={n}" for k, n in s["kinds"].items()))
    print("top apps (activations, minutes in front):")
    for a in s["top_apps"]:
        print(f"  {a['app']}: {a['activations']}, {a['minutes']}")
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    from . import live  # W8
    return live.main_live(args.once, args.dry_run, args.socket, dry_exec=args.dry_exec)


def cmd_vocal_send(args: argparse.Namespace) -> int:
    from . import vocal  # W12
    return vocal.main_send(args)


def cmd_shot_send(args: argparse.Namespace) -> int:
    from . import shots  # W17
    return shots.main_send(args)


def cmd_mail_open_draft(args: argparse.Namespace) -> int:
    from . import claude_actions  # the draft card's « Open in Mail » (the user's press, never automatic)
    if args.dry_run:
        print(json.dumps({"dry_run": "osascript", "script": "SHOW_DRAFT", "args": [str(args.id)]}))
        return 0
    return 0 if claude_actions.show_draft(args.id) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oi", description="OS-intent: ActivityWatch -> action tokens")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("pull", help="incrementally pull every aw-server bucket into raw/")
    sp.add_argument("--since", help="ISO timestamp to pull from (overrides the cursors)")
    sp.set_defaults(func=cmd_pull)
    st = sub.add_parser("tokens", help="(re)build tokens/<day>.jsonl from raw/")
    st.add_argument("--day", type=_day, default="today")
    st.add_argument("--show", action="store_true", help="print the token rows (contains titles/URL slugs)")
    st.set_defaults(func=cmd_tokens)
    ss = sub.add_parser("stats", help="counts per token kind and top apps (no titles)")
    ss.add_argument("--day", type=_day, default="today")
    ss.add_argument("--top", type=int, default=10)
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=cmd_stats)
    sl = sub.add_parser("live", help="live driver: Markov predictions on the HUD (LaunchAgent com.osintent.oi-live)")
    sl.add_argument("--once", action="store_true", help="one evaluation of the current moment, then exit")
    sl.add_argument("--dry-run", action="store_true", help="print the show payloads instead of sending them")
    sl.add_argument("--socket", help="oi-hud socket (default <data dir>/hud.sock)")
    sl.add_argument("--dry-exec", action="store_true",
                    help="W15: send the shows but execute nothing (print what a press would run; tests, demo)")
    sl.set_defaults(func=cmd_live)
    sv = sub.add_parser("vocal-send", help="deliver a HUD voice note's transcript to one fleet agent (W12)")
    sv.add_argument("--to", required=True, help="recipient slug (atlas, nova, orion, lyra, vega, juno, ...)")
    sv.add_argument("--text-file", required=True, help="the transcript (read from the file, never argv)")
    sv.add_argument("--audio", help="the .caf kept on this Mac (its path is mentioned, never transferred)")
    sv.add_argument("--app", help="front app when the vocal key was pressed")
    sv.add_argument("--desk", type=int, help="desk number when the vocal key was pressed")
    sv.add_argument("--dry-run", action="store_true", help="print the exact command and line; send nothing")
    sv.add_argument("--action-key", help="outward gate: the key that also validates the confirm panel (1-4, vocal)")
    sv.add_argument("--typed", action="store_true",
                    help="the text was typed / edited by hand in the HUD panel (the line says so)")
    sv.set_defaults(func=cmd_vocal_send)
    # W17: one screenshot delivery by hand (oi live does it from the HUD offer)
    sh = sub.add_parser("shot-send", help="send a screenshot + context line / note to one fleet agent (W17)")
    sh.add_argument("--to", required=True, help="recipient slug (atlas, juno: inbox + inject; others: A2A, note only)")
    sh.add_argument("--image", required=True, help="the screenshot (copied to a local inbox, never uploaded)")
    sh.add_argument("--note-file", help="the note (read from the file, never argv)")
    sh.add_argument("--app")
    sh.add_argument("--host")
    sh.add_argument("--desk", type=int)
    sh.add_argument("--dry-run", action="store_true", help="print what would run; send nothing")
    sh.set_defaults(func=cmd_shot_send)
    # the draft card's « Open in Mail » (oi-hud runs it on the user's press)
    md = sub.add_parser("mail-open-draft", help="show a saved in-place reply draft in Mail and bring Mail forward")
    md.add_argument("--id", type=int, required=True, help="the draft's outgoing message id (from the HUD card)")
    md.add_argument("--dry-run", action="store_true", help="print what would run; touch nothing")
    md.set_defaults(func=cmd_mail_open_draft)
    from .cli_laya import register   # W13: laya-serve, replay, laya-train, laya-promote, improve-report
    register(sub)
    from .laya_switch import register as register_laya_switch   # the menu-bar Laya switch (2026-10-21)
    register_laya_switch(sub)
    from .intent_resolve import register as register_wish   # W18: wish-resolve
    register_wish(sub)
    from .onboard import register as register_onboard   # W22: onboard, starter-preview
    register_onboard(sub)
    from .proactive.engine import register as register_proactive   # W24: proactive-probe
    register_proactive(sub)
    from .health import register as register_health   # production health: oi health
    register_health(sub)
    from .demo import register as register_demo   # demo mode for screen recording (fake data only)
    register_demo(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
