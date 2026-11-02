"""`oi relevance-eval` on synthetic data only (tests/relevance_fixture.py)."""
from __future__ import annotations

import json
import os
import stat

import pytest

import relevance_fixture
from oi import cli, live, markov, normalise, relevance_eval as rv, replay
from oi.moments import NOTHING


@pytest.fixture
def synth_dir(data_dir, monkeypatch):
    monkeypatch.setenv("OI_TZ", "UTC")
    days = relevance_fixture.write(data_dir, days=6)
    return data_dir, days


def test_showable_list_is_the_live_gates():
    assert replay.SHOW_PRIMITIVES is live.ALLOWED_PRIMITIVES
    assert replay.is_showable({"primitive": "mail.reply"}) and replay.is_showable({"primitive": "ui.press"})
    assert not replay.is_showable({"primitive": "app.focus"}) and not replay.is_showable({"primitive": None})


def test_hud_order_keeps_showable_only_events_first():
    menu = [{"key": "open_title_x", "primitive": "app.focus", "token": "title:Notes/x", "p_markov": 0.9},
            {"key": "open_app_b", "primitive": "app.open", "token": "activate:B", "p_markov": 0.2},
            {"key": "open_app_a", "primitive": "app.open", "token": "activate:A", "p_markov": 0.2},
            {"key": "open_url_c", "primitive": "url.open", "token": "url:c.example/x", "p_markov": 0.1,
             "p_show": 0.6, "starter": "c"},
            {"key": NOTHING, "primitive": None, "token": NOTHING, "p_markov": None}]
    evt = [{"key": "evt_1", "primitive": "mail.show", "token": "mail.show", "evt": True}]
    order = rv.hud_order(menu, evt)
    assert [o["key"] for o in order] == ["evt_1", "open_url_c", "open_app_a", "open_app_b"]
    assert [rv.option_source(o) for o in order] == ["proactive", "starter", "markov", "markov"]
    assert rv.option_source({"key": "reply_sender", "token": None}) == "fixed"


def test_stream_token_maps_options_to_the_token_stream():
    assert rv.stream_token({"token": "activate:Mail", "primitive": "app.open"}) == "activate:Mail"
    assert rv.stream_token({"token": "url.open:meeting", "primitive": "url.open",
                            "params": {"href": "https://meet.example.com/standup"}}) == "url:meet.example.com/standup"
    assert rv.stream_token({"token": "mail.reply", "primitive": "mail.reply", "params": {}}) is None


def test_live_events_follow_appear_and_gone_rows():
    at = rv.live_events([{"ts": "2026-10-12T09:00:00Z", "id": "a"}, {"ts": "2026-10-12T09:01:00Z", "id": "a",
                                                                      "state": "gone"}])
    assert at("2026-10-12T08:59:59Z") == []
    assert [e["id"] for e in at("2026-10-12T09:00:30.000Z")] == ["a"]
    assert at("2026-10-12T09:01:00Z") == []


def test_run_on_synthetic_data(synth_dir):
    root, days = synth_dir
    train, test = replay.split_days(days, 2)
    from oi import starter
    from oi.proactive import store
    r = rv.run(train, test, alphas=[1, 10], depths=["o1", "o2hs"],
               starter_items=starter.load_catalogue(root / "starter_catalogue.yaml"), events_reader=store.read_day)
    names = list(r["variants"])
    assert names[0] == "markov alpha=1 depth=o2hs" and len(names) == 4 + 3
    assert r["variants"][names[0]]["delta"] == {"top1": 0.0, "top3": 0.0, "mrr": 0.0}
    for v in r["variants"].values():
        assert v["acted"] > 100
        assert 0 <= v["top1"] <= v["top3"] <= v["label_in_menu"] <= 1
        assert v["top1"] <= v["mrr"] <= 1
        for k in ("top1", "top3", "mrr"):     # the sources add up to the overall figure
            assert sum(s[k] for s in v["per_source"].values()) == pytest.approx(v[k], abs=0.002)
    full = r["variants"][names[-1]]["per_source"]
    assert {"markov", "starter", "proactive"} <= set(full)
    assert r["laya"]["n"] > 0 and r["laya"]["laya"]["top1"] > r["laya"]["markov"]["top1"]
    # aggregates only: no token, app or host in the report files, both 0600
    for p in (r["path"], r["path"][:-3] + ".json"):
        text = open(p, encoding="utf-8").read()
        assert "example" not in text and "Safari" not in text
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_measurement_only_ranking_untouched(synth_dir):
    root, days = synth_dir
    _, ms = replay.load_moments(days)
    model = markov.Markov.fit(ms)
    before = json.dumps(model.to_json(), sort_keys=True)
    rv.score_moments(ms[:50], model)
    assert json.dumps(model.to_json(), sort_keys=True) == before and markov.ALPHA == 1.0


def test_laya_section_absent_without_shadow_rows(synth_dir):
    _, days = synth_dir
    assert rv.laya_section(days[:1]) is None
    assert rv.laya_section(days, hud_reader=normalise.read_hud_log)["n"] > 0


def test_cli(synth_dir, capsys):
    _, days = synth_dir
    assert cli.main(["relevance-eval", "--days", f"{days[0]}..{days[-1]}", "--alpha", "1,3", "--starters",
                     "--proactive"]) == 0
    out = capsys.readouterr().out
    assert "| markov alpha=3 depth=o2hs |" in out and "+starter +proactive" in out and "Per source" in out
    assert cli.main(["relevance-eval", "--days", days[0]]) == 2                 # one day: no split
    assert cli.main(["relevance-eval", "--days", *days, "--depth", "o3"]) == 2  # unknown depth
