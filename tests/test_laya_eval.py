"""oi laya-bench (latency on real-shaped requests) and oi laya-report (shadow evaluation)."""
from __future__ import annotations

import json

import pytest

from oi import laya, laya_eval, paths
from oi.cli import main as cli_main

TOK = {"mail": "activate:Mail", "term": "activate:Terminal", "web": "url:github.com", "nothing": "nothing"}


def shadow_row(ts, laya_p, markov_p, outcome="dropped:cooldown", options=None, error=None):
    lay = ({"error": error, "checkpoint": "laya:english"} if error else
           {"probs": laya_p, "latency_ms": 150, "checkpoint": "laya:english"})
    lay.update(markov_top=max(markov_p, key=markov_p.get), markov_probs=markov_p, tokens=TOK)
    return {"ts": ts, "form": "A", "options": options or [], "outcome": outcome, "laya": lay}


LAYA = {"mail": 0.6, "term": 0.2, "web": 0.15, "nothing": 0.05}      # Laya: mail first
MARKOV = {"term": 0.5, "web": 0.3, "nothing": 0.15, "mail": 0.05}  # Markov: mail last


def test_next_action_skips_events_and_feedback_and_windows():
    toks = [{"ts": "2026-10-20T10:00:00Z", "token": "activate:Mail"},
            {"ts": "2026-10-20T10:00:05Z", "token": "hud:shown"},
            {"ts": "2026-10-20T10:00:06Z", "token": "evt:mail"},
            {"ts": "2026-10-20T10:00:30Z", "token": "activate:Terminal"},
            {"ts": "2026-10-20T10:10:00Z", "token": "activate:Safari"}]
    assert laya_eval.next_action(toks, "2026-10-20T10:00:01.000Z") == "activate:Terminal"
    assert laya_eval.next_action(toks, "2026-10-20T10:05:00Z") == "nothing"           # > 120 s: nothing


def test_report_scores_laya_vs_markov_on_next_and_picks():
    hud = {"2026-10-20": [
        shadow_row("2026-10-20T10:00:00Z", LAYA, MARKOV, outcome="pressed:2",
                   options=[{"key": 2, "token": "activate:Mail"}, {"key": 1, "token": "activate:Terminal"}]),
        shadow_row("2026-10-20T11:00:00Z", LAYA, MARKOV),
        shadow_row("2026-10-20T12:00:00Z", LAYA, MARKOV, error="timeout"),
        {"ts": "2026-10-20T12:30:00Z", "outcome": "pressed:1", "options": []},                 # no shadow: ignored
        {"ts": "2026-10-20T12:40:00Z", "laya": {"probs": LAYA, "markov_top": "term"}},        # old row: unscorable
    ]}
    toks = {"2026-10-20": [{"ts": "2026-10-20T10:00:02Z", "token": "activate:Mail"},
                           {"ts": "2026-10-20T11:00:20Z", "token": "url:github.com"}]}
    r = laya_eval.report(["2026-10-20"], hud_reader=lambda d: hud.get(d, []), tokens_reader=lambda d: toks.get(d, []))
    assert r["shadow_calls"] == 4 and r["errors"] == {"timeout": 1} and r["error_rate"] == 0.25
    assert r["next"] == {"n": 2, "laya_top1": 0.5, "markov_top1": 0.0, "laya_top3": 1.0, "markov_top3": 0.5}
    assert r["pick"] == {"n": 1, "laya_top1": 1.0, "markov_top1": 0.0, "laya_top3": 1.0, "markov_top3": 0.0}
    assert r["top1_agreement"] == 0.0 and r["scored_days"] == 1
    assert r["promotion"]["verdict"].startswith("pending")                  # 2 moments: far from the bar
    md = laya_eval.to_markdown(r)
    assert "| next action (120 s) | 2 | 50% | 0% | 100% | 50% |" in md and "the user's call" in md


def _summary(n=400, days=3, err=0.02, l1=0.40, m1=0.38, l3=0.80, m3=0.70, picks=60, pl3=0.8, pm3=0.7):
    return {"next": {"n": n, "laya_top1": l1, "markov_top1": m1, "laya_top3": l3, "markov_top3": m3},
            "pick": {"n": picks, "laya_top1": 0.5, "markov_top1": 0.5, "laya_top3": pl3, "markov_top3": pm3},
            "scored_days": days, "error_rate": err}


@pytest.mark.parametrize("kw,verdict", [
    ({}, "live criteria met"),
    ({"l1": 0.30}, "blend criteria met"),                    # top-3 gain holds, top-1 dropped: blend only
    ({"l3": 0.72}, "not met"),                               # +2 pts < +3 pts
    ({"pl3": 0.6}, "not met"),                               # worse than Markov on what the user pressed
    ({"pl3": 0.7}, "live criteria met"),                     # picks: "not worse" is enough (Markov-biased)
    ({"err": 0.3}, "not met"),                               # Laya too often absent
    ({"n": 100}, "pending"),
    ({"days": 1}, "pending"),
    ({"picks": 10}, "pending"),                              # not enough HUD picks yet
])
def test_promotion_bar(kw, verdict):
    assert laya_eval.promotion(_summary(**kw))["verdict"].startswith(verdict)


class Client:
    def __init__(self, lat):
        self.lat, self.i = lat, 0

    def predict(self, form, menu, text):
        self.i += 1
        if self.lat[self.i - 1] is None:
            raise laya.LayaError("timeout")
        return {"probs": {}, "latency_ms": self.lat[self.i - 1]}


def test_bench_reports_first_call_apart_and_suggests_a_timeout(rows, model, data_dir, monkeypatch):
    day = rows[-1]["ts"][:10]
    lat = [1200] + [100] * 18 + [200, None]
    slept = []
    r = laya_eval.bench(day, 20, gap_s=5, client=Client(lat), model=model, sleep=slept.append,
                        reader=lambda d: [r for r in rows if r["ts"][:10] == d])
    assert r["first_ms"] == 1200 and r["n_warm"] == 19 and r["errors"] == {"timeout": 1}
    assert r["p50_ms"] == 100 and r["p95_ms"] == 200 and r["max_ms"] == 200
    assert r["suggested_timeout_ms"] == 350 and slept == [5] * 20        # 200 x 1.5 + 50, rounded up to 50
    assert laya_eval.suggested_timeout_ms(None) is None


def test_bench_never_starts_a_server(data_dir, monkeypatch):
    monkeypatch.setattr(laya, "health", lambda url, **kw: False)
    with pytest.raises(laya.LayaError):
        laya_eval.bench("2026-10-20", 5, url="http://127.0.0.1:9")


def test_cli_laya_report_writes_a_private_report(data_dir, capsys):
    paths.ensure_private_root()
    d = paths.hud_log_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-10-20.jsonl").write_text(json.dumps(shadow_row("2026-10-20T10:00:00Z", LAYA, MARKOV)) + "\n")
    assert cli_main(["laya-report", "--days", "2026-10-20"]) == 0
    out = capsys.readouterr().out
    assert "Laya shadow report" in out and "pending" in out
    p = out.strip().splitlines()[-1].split("report: ", 1)[1]
    assert (paths.data_dir() / "reports").exists() and p.endswith(".md")
    assert oct(__import__("os").stat(p).st_mode & 0o777) == "0o600"
