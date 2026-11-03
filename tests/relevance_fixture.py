"""Synthetic data dir for `oi relevance-eval` (invented content only, no real data).

tokens/ from synth.stream, plus a daily calendar event (events/) usually followed by opening Calendar, a small
starter catalogue, and a few Laya shadow rows (hud-log/) on the last day.
Run: python tests/relevance_fixture.py OUT_DIR [DAYS] -> then OI_DATA_DIR=OUT_DIR oi relevance-eval --days ...
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import synth  # noqa: E402

STARTERS = """items:
  - {id: guide, label: "Docs guide", primitive: url.open, params: {href: "https://docs.example.com/guide"},
     trigger: {apps: [Code]}, prior: 0.5}
  - {id: notes, label: "Notes", primitive: app.open, params: {app: Notes}, trigger: {apps: [Code]}, prior: 0.4}
"""


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def rows(days: int = 8, seed: int = 11) -> tuple[list[dict], list[dict]]:
    """(token rows, event rows): one calendar event per day, live at the first token after 09:00 UTC."""
    rng = random.Random(seed)
    toks = synth.stream(days=days, seed=seed)
    out, events, seen = [], [], set()
    for r in toks:
        out.append(r)
        t, day = _ts(r["ts"]), r["ts"][:10]
        if day not in seen and t.hour >= 9 and not r["token"].startswith("afk:"):
            seen.add(day)
            ev = {"ts": _iso(t - timedelta(seconds=1)), "id": f"calendar:{day}", "source": "calendar",
                  "class": "meeting-soon", "who": None, "subject": None,
                  "ref": {"join": "https://meet.example.com/standup"}, "private": False, "state": "live"}
            events += [ev, {"ts": _iso(t + timedelta(seconds=90)), "id": ev["id"], "source": "calendar",
                            "class": "meeting-soon", "state": "gone"}]
            nxt = "activate:Calendar" if rng.random() < 0.7 else "url:meet.example.com/standup"
            out.append({"ts": _iso(t + timedelta(seconds=1)), "dur": 1.0, "token": nxt,
                        "app": "Calendar" if nxt.startswith("activate") else r["app"], "src": "aw-window",
                        "raw_id": None})
    return sorted(out, key=lambda x: x["ts"]), events


def laya_rows(tokens: list[dict], n: int = 20) -> list[dict]:
    """Shadow rows on Code moments: Laya puts Safari first, Markov the copy key (never showable), then Telegram."""
    out = []
    for r in tokens:
        if r["token"] == "activate:Code" and len(out) < n:
            out.append({"ts": r["ts"], "form": "A", "laya": {
                "probs": {"open_app_Safari": 0.5, "open_app_Telegram": 0.2, "press_key_cmdc": 0.2, "nothing": 0.1},
                "markov_probs": {"press_key_cmdc": 0.4, "open_app_Telegram": 0.3, "open_app_Safari": 0.2,
                                 "nothing": 0.1},
                "tokens": {"open_app_Safari": "activate:Safari", "open_app_Telegram": "activate:Telegram",
                           "press_key_cmdc": "key:⌘C", "nothing": "nothing"}}})
    return out


def write(root: Path, days: int = 8, seed: int = 11) -> list[str]:
    """Writes tokens/, events/, hud-log/ and the starter catalogue under root; returns the days."""
    toks, events = rows(days, seed)
    by_day: dict[str, list[dict]] = {}
    for r in toks:
        by_day.setdefault(r["ts"][:10], []).append(r)
    for sub in ("tokens", "events", "hud-log"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    for d, rs in by_day.items():
        (root / "tokens" / f"{d}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rs))
        evs = [e for e in events if e["ts"][:10] == d]
        (root / "events" / f"{d}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evs))
    last = max(by_day)
    (root / "hud-log" / f"{last}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in laya_rows(by_day[last])))
    (root / "starter_catalogue.yaml").write_text(STARTERS)
    return sorted(by_day)


if __name__ == "__main__":
    ds = write(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 8)
    print(f"{ds[0]}..{ds[-1]}")
