"""W21: the option number is the key (fn 1, ⌃ 2, ⌥ 3, ⌘ 4), filled in `hud.fill_order`, stable per context."""
import io
import json
import threading

import pytest

from oi import hud_keys, improve, live, normalise, settings, shots

A = {"key": "a", "p_markov": 0.5, "primitive": "app.open", "token": "activate:Mail", "params": {"app": "Mail"}}
B = {"key": "b", "p_markov": 0.3, "primitive": "app.open", "token": "activate:Notes", "params": {"app": "Notes"}}
C = {"key": "c", "p_markov": 0.2, "primitive": "app.open", "token": "activate:Safari", "params": {"app": "Safari"}}
D = {"key": "d", "p_markov": 0.1, "primitive": "app.open", "token": "activate:Telegram", "params": {"app": "Telegram"}}


def test_numbers_are_keys_and_default_fill_is_control_first():
    assert hud_keys.KEY_OF == {"fn": 1, "ctrl": 2, "option": 3, "cmd": 4}
    assert hud_keys.fill_keys() == [2, 3, 4, 1]                       # ⌃ ⌥ ⌘ fn
    assert hud_keys.assign(["a"]) == [2]                              # rank 1 on control
    assert hud_keys.assign(["a", "b", "c"]) == [2, 3, 4]              # 3 options: ⌘ keeps 4, fn (1) stays empty
    assert hud_keys.assign(["a", "b", "c", "d"]) == [2, 3, 4, 1]
    assert hud_keys.assign([]) == []
    with pytest.raises(ValueError):
        hud_keys.assign(["a", "b", "c", "d", "e"])


def test_fill_order_setting_aliases_and_fallback():
    assert hud_keys.fill_keys(["fn", "command", "alt", "control"]) == [1, 4, 3, 2]
    assert hud_keys.assign(["a", "b"], ["⌘", "⌥", "⌃", "fn"]) == [4, 3]
    for bad in (None, "ctrl", ["ctrl", "option", "cmd"], ["ctrl", "ctrl", "cmd", "fn"], ["ctrl", "option", "cmd", "shift"], [1, 2, 3, 4]):
        assert hud_keys.fill_order(bad) == ["ctrl", "option", "cmd", "fn"]


def test_settings_hud_fill_order(data_dir):
    assert settings.load()["hud"]["fill_order"] == ["ctrl", "option", "cmd", "fn"]
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"hud": {"fill_order": ["fn", "ctrl", "option", "cmd"]}}))
    assert settings.load()["hud"]["fill_order"] == ["fn", "ctrl", "option", "cmd"]
    (data_dir / "settings.json").write_text(json.dumps({"hud": {"fill_order": ["fn"]}}))
    assert settings.load()["hud"]["fill_order"] == ["ctrl", "option", "cmd", "fn"]
    assert settings.HUD_DEFAULTS["fill_order"] == ["ctrl", "option", "cmd", "fn"]     # never mutated by a load


def test_stability_over_rank_in_the_same_context():
    m = hud_keys.KeyMemory()
    assert m.assign("A|Mail", ["a", "b", "c"]) == [2, 3, 4]
    assert m.assign("A|Mail", ["c", "a"]) == [4, 2]                   # c now ranks first: it stays on ⌘ 4
    assert m.assign("A|Mail", ["d", "a"]) == [3, 2]                   # a keeps ⌃; d takes the first free key (⌥)
    assert m.assign("A|Mail", ["b", "d", "a", "c"]) == [1, 3, 2, 4]   # b's ⌥ was taken by d: b gets the free fn
    assert m.assign("A|Safari", ["c", "a"]) == [2, 3]                  # another context: plain fill order
    # one id lives on one key: d moved, its old key is free for the next newcomer
    assert m.assign("A|Mail", ["e"]) == [2]                            # a not shown: its ⌃ goes to rank 1


def test_memory_is_bounded():
    m = hud_keys.KeyMemory(max_contexts=2)
    m.assign("x", ["a", "b"])
    assert m.assign("x", ["b"]) == [3]
    m.assign("y", ["a"]); m.assign("z", ["a"])                         # x evicted
    assert m.assign("x", ["b"]) == [2]


# ---- the live driver: payload, protocol, hud-log, glide all speak key numbers --------------------------

def _lv(replies):
    sent, runs = [], []
    replies = list(replies)

    def sender(payload, on_reply):
        sent.append(payload)
        t = threading.Thread(target=on_reply, args=(replies.pop(0),))
        t.start()
        return t
    lv = live.Live(pull=False, sender=sender, runner=lambda a: (runs.append(a), (0, ""))[1], out=io.StringIO(),
                   resolve_app=lambda a: None, is_executable=lambda o: True)
    return lv, sent, runs


def _row(shown):
    return {"ts": "2026-01-05T09:00:00.000Z", "form": "A", "options": [{"key": None, "token": o["token"]} for o in shown],
            "shown": False, "outcome": "x", "executed": None, "latency_ms": None}


def test_dispatch_sends_key_numbers_and_a_press_runs_that_keys_option(data_dir):
    lv, sent, runs = _lv([{"event": "pressed", "key": 3, "via": "key"}, {"event": "pressed", "key": 4, "via": "key"}])
    shown = [A, B, C]
    lv.dispatch(shown, _row(shown), {"app": "Mail"}, {"ttl_ms": 6000}, 1.0)
    lv.wait_replies(5)
    assert [(o["key"], o["label"]) for o in sent[0]["options"]] == [(2, "Mail"), (3, "Notes"), (4, "Safari")]
    assert runs == [["open", "-a", "Notes"]]                           # pressed:3 = ⌥ = Notes (rank 2)
    out = normalise.read_hud_log("2026-01-05")[-1]
    assert out["outcome"] == "pressed:3" and out["executed"] == "activate:Notes"
    assert [o["key"] for o in out["options"]] == [2, 3, 4]            # the hud-log names keys, never ranks
    # the same context again, Safari now ranked first: it keeps ⌘ 4, pressing ⌘ still opens Safari
    shown = [C, A]
    lv.dispatch(shown, _row(shown), {"app": "Mail"}, {"ttl_ms": 6000}, 2.0)
    lv.wait_replies(5)
    assert [(o["key"], o["label"]) for o in sent[1]["options"]] == [(4, "Safari"), (2, "Mail")]
    assert runs[-1] == ["open", "-a", "Safari"]


def test_dispatch_uses_the_fill_order_setting(data_dir):
    lv, sent, runs = _lv([{"event": "pressed", "key": 1, "via": "key"}])
    lv.dispatch([A, B], _row([A, B]), {"app": "Mail"}, {"ttl_ms": 6000}, 1.0, fill_order=["fn", "cmd", "option", "ctrl"])
    lv.wait_replies(5)
    assert [o["key"] for o in sent[0]["options"]] == [1, 4]
    assert runs == [["open", "-a", "Mail"]]


def test_hud_payload_without_keys_follows_the_fill_order():
    p = live.hud_payload([A, B, C], {}, resolve_app=lambda a: None)
    assert [o["key"] for o in p["options"]] == [2, 3, 4]


def test_screenshot_offer_and_picker_use_the_keys():
    table = {s: {"name": s.title()} for s in ("atlas", "nova", "orion", "lyra", "vega")}
    # the live screenshot's options are pool candidates: Live.dispatch gives them the keys (fill order)
    shot = {"id": "s", "table": table, "who": {"slug": "atlas", "via": "default"}, "content": None, "claude": True}
    opts = shots.shot_options(shot, telegram_icon=False)
    payload = live.hud_payload(opts, {})
    assert [(o["key"], o["label"]) for o in payload["options"]] == [(2, "→ Atlas"), (3, "→ other"), (4, "Annotate")]
    payload = live.hud_payload(opts, {}, keys=hud_keys.fill_keys(["fn", "ctrl", "option", "cmd"])[:3])
    assert [o["key"] for o in payload["options"]] == [1, 2, 3] and payload["options"][0].get("claude") is True
    opts, keys = shots.picker_options(table, 0)
    assert [o["key"] for o in opts] == [2, 3, 4, 1] and keys[1] == "more"   # "More" on fn


# ---- W21b: hud-log rows say how they number their options -------------------------------------------------------

def test_hud_numbering_defaults_to_rank_for_old_rows():
    assert normalise.HUD_NUMBERING == 2
    assert normalise.hud_numbering({"numbering": 2}) == 2
    assert normalise.hud_numbering({}) == 1                       # rows written before W21
    assert normalise.hud_numbering({"numbering": True}) == 1      # junk -> the old numbering
    assert normalise.hud_numbering({"numbering": "2"}) == 1


def test_every_live_row_carries_the_key_numbering(data_dir):
    lv = live.Live(pull=False, dry_run=True, out=io.StringIO())
    lv.log_row({"ts": "2026-10-20T19:00:00.000Z", "form": "A", "options": [], "shown": False, "outcome": "dropped:cooldown"})
    lv.log_row({"ts": "2026-10-20T19:00:01.000Z", "form": "A", "numbering": 2, "outcome": "dropped:dry_run"})
    rows = normalise.read_hud_log("2026-10-20")
    assert [r["numbering"] for r in rows] == [2, 2]


def test_improve_report_splits_rows_by_numbering(data_dir):
    hud = [{"ts": "2026-10-19T09:00:00.000Z", "form": "A", "shown": True, "outcome": "pressed:1"},          # pre-W21
           {"ts": "2026-10-20T19:00:00.000Z", "form": "A", "shown": True, "outcome": "pressed:4", "numbering": 2}]
    r = improve.report(["2026-10-20"], reader=lambda d: [], hud_reader=lambda d: hud)
    assert r["forms"]["A"]["numbering"] == {"1": 1, "2": 1}
    assert "by numbering" in improve.to_markdown(r)
