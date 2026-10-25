"""W8: `oi live` — gate, context change, HUD JSON, executor, hud-log -> tokens, status file.
Synthetic data only; no real `open`/`osascript` (fake runner), no real socket (fake sender)."""
import io
import json
import threading

from conftest import TZ

from oi import cli, live, normalise, paths, settings, vocab
from oi.moments import build_moments

CFG = dict(settings.GATE_DEFAULTS)


def opt(key, p, prim="app.open", token="activate:Mail", **params):
    return {"key": key, "p_markov": p, "primitive": prim, "token": token,
            "params": params or ({"app": token.split(":", 1)[1]} if prim == "app.open" else {})}


def _gate(menu, nothing_p=0.1, **kw):
    args = dict(now=100.0, last_show_at=None, paused=False, hud_enabled=True, support=5)
    args.update(kw)
    return live.gate(menu, nothing_p, CFG, **args)


# ---- gate -------------------------------------------------------------------------------------

def test_gate_thresholds_and_order():
    menu = [opt("a", 0.10, token="activate:A"), opt("b", 0.40, token="activate:B"),
            opt("c", 0.20, token="activate:C"), opt("t", 0.9, prim="app.focus", token="title:Notes/x"),
            opt("k", 0.9, prim="key.send", token="key:⌘C"),
            {"key": "nothing", "p_markov": None, "primitive": None, "token": "nothing", "params": {}}]
    shown, why = _gate(menu)
    assert why == "show"
    assert [o["key"] for o in shown] == ["b", "c"]       # >= 0.15, best first; title/key never shown
    many = [opt(f"o{i}", 0.2 + i / 100, token=f"activate:A{i}") for i in range(6)]
    assert len(_gate(many)[0]) == 3                         # max_show
    assert _gate([opt("a", 0.10)]) == ([], "no_option")


def test_gate_nothing_history_cooldown_pause():
    menu = [opt("a", 0.5)]
    assert _gate(menu, nothing_p=0.61) == ([], "nothing_likely")
    assert _gate(menu, nothing_p=0.6)[1] == "show"
    assert _gate(menu, support=0) == ([], "thin_history")
    assert _gate(menu, last_show_at=85.0) == ([], "cooldown")    # 15 s < 20 s
    assert _gate(menu, last_show_at=80.0)[1] == "show"            # 20 s: elapsed
    assert _gate(menu, paused=True) == ([], "paused")
    assert _gate(menu, hud_enabled=False) == ([], "hud_disabled")


def test_w20_hint_cooldown_split():
    """W20: a hint has its own cooldown (gate.hint_cooldown_s, default 8); a full show keeps cooldown_s (20)."""
    menu = [opt("a", 0.5)]
    assert settings.GATE_DEFAULTS["hint_cooldown_s"] == 8 and settings.GATE_DEFAULTS["cooldown_s"] == 20
    assert _gate(menu, last_show_at=93.0, hint=True) == ([], "cooldown")     # 7 s < 8 s
    assert _gate(menu, last_show_at=92.0, hint=True)[1] == "show"            # 8 s: elapsed for a hint
    assert _gate(menu, last_show_at=92.0) == ([], "cooldown")                # ... not for a full show (8 < 20)
    cfg = dict(settings.GATE_DEFAULTS, hint_cooldown_s=3)
    assert live.gate(menu, 0.0, cfg, now=100.0, last_show_at=96.0, paused=False, hud_enabled=True,
                     support=5, hint=True)[1] == "show"
    cfg.pop("hint_cooldown_s")                                               # an old cfg without the key
    assert live.gate(menu, 0.0, cfg, now=100.0, last_show_at=93.0, paused=False, hud_enabled=True,
                     support=5, hint=True) == ([], "cooldown")


def test_w20_hint_cooldown_setting_roundtrip(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    assert settings.load()["gate"]["hint_cooldown_s"] == 8
    (data_dir / "settings.json").write_text(json.dumps({"gate": {"hint_cooldown_s": 12, "cooldown_s": 30}}))
    g = settings.load()["gate"]
    assert g["hint_cooldown_s"] == 12 and g["cooldown_s"] == 30


def test_settings_gate_and_live_sections(data_dir):
    s = settings.load()
    assert s["gate"] == settings.GATE_DEFAULTS and s["live"] == {"poll_s": 2, "rebuild_s": 600, "retrain_s": 600,
                                                                  **settings.LIVE_LAYA_DEFAULTS}   # W13 keys
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps(
        {"gate": {"show_min_p": 0.3, "max_show": 2.0, "cooldown_s": "x", "bogus": 1}, "live": {"poll_s": -1}}))
    s = settings.load()
    assert s["gate"]["show_min_p"] == 0.3 and s["gate"]["max_show"] == 2 and isinstance(s["gate"]["max_show"], int)
    assert s["gate"]["cooldown_s"] == 20 and "bogus" not in s["gate"] and s["live"]["poll_s"] == 2
    assert s["gate"]["ttl_ms"] == 6000                    # W19 default (W9: 15000)
    (data_dir / "settings.json").write_text(json.dumps(
        {"gate": {"ttl_ms": 20000.0, "hide_on_pointer_travel": True}}))   # the HUD's bool key is ignored here
    s = settings.load()
    assert s["gate"]["ttl_ms"] == 20000 and isinstance(s["gate"]["ttl_ms"], int)
    assert "hide_on_pointer_travel" not in s["gate"]


# ---- context change ----------------------------------------------------------------------------

def _r(ts, token):
    return {"ts": f"2026-01-05T09:00:{ts:02d}.000Z", "dur": 1, "token": token, "app": None, "src": "aw-window",
            "raw_id": None}


def test_context_change_detection():
    t = live.ContextTracker()
    rows = [_r(0, "activate:Mail"), _r(1, "key:⌘C")]
    t.prime(rows)
    assert t.change(rows) is None                                  # primed: nothing new
    rows.append(_r(2, "title:Mail/x"))
    assert t.change(rows) is None                                  # titles/keys are not context changes
    rows.append(_r(3, "afk:start"))
    assert t.change(rows) is None                                  # afk ignored
    rows.append(_r(4, "activate:Safari"))
    assert t.change(rows)["token"] == "activate:Safari"
    assert t.change(rows) is None                                  # handled once
    rows += [_r(5, "url:example.org/kanban"), _r(6, "space:+1")]
    assert t.change(rows)["token"] == "space:+1"                   # the newest one


# ---- options -> HUD JSON -----------------------------------------------------------------------

def test_hud_payload_labels_and_icons():
    opts = [opt("open_app_mail", 0.41234, token="activate:Mail"),
            opt("open_url", 0.3, prim="url.open", token="url:example.org/kanban", url="example.org/kanban"),
            opt("move", 0.2, prim="space.move", token="space:+1", move="+1")]
    p = live.hud_payload(opts, {"space": 2}, resolve_app=lambda a: f"/Applications/{a}.app")
    assert p == {"cmd": "show", "ttl_ms": 6000, "options": [
        {"key": 2, "label": "Mail", "icon": "app:/Applications/Mail.app", "confidence": 0.412, "claude": True,
         "hint": {"kind": "window", "app": "Mail"}},
        {"key": 3, "label": "example.org", "icon": "sf:safari", "confidence": 0.3, "claude": True,
         "hint": {"kind": "window", "app": "Safari"}},
        {"key": 4, "label": "3", "icon": "sf:arrow.right.square", "confidence": 0.2,
         "hint": {"kind": "edge", "dir": "right"}}]}
    q = live.hud_payload([opt("x", 0.2, token="activate:Nope"),
                          opt("m", 0.2, prim="space.move", token="space:-1", move="-1")], {"space": None},
                         resolve_app=lambda a: None)
    assert [o["label"] for o in q["options"]] == ["Nope", "←"]
    assert q["options"][0]["icon"] == "sf:app"
    assert live.space_target("space:=4", 1) == (3, 4)


def test_hud_hint_mapping():
    """W11: app.open -> the app's window, url.open -> Safari's window, space.move -> edge bar."""
    h = live.hud_hint
    assert h(opt("a", 0.3, token="activate:Telegram", app="Telegram"), {}) == {"kind": "window", "app": "Telegram"}
    assert h(opt("u", 0.3, prim="url.open", token="url:notion.so/x", url="notion.so/x"), {}) == {"kind": "window", "app": "Safari"}
    assert h(opt("n", 0.3, prim="space.move", token="space:+1", move="+1"), {"space": None}) == {"kind": "edge", "dir": "right"}
    assert h(opt("p", 0.3, prim="space.move", token="space:-1", move="-1"), {"space": 2}) == {"kind": "edge", "dir": "left"}
    assert h(opt("g", 0.3, prim="space.move", token="space:=1", move="=1"), {"space": 3}) == {"kind": "edge", "dir": "left"}
    assert h(opt("g", 0.3, prim="space.move", token="space:=4", move="=4"), {"space": 2}) == {"kind": "edge", "dir": "right"}
    assert h(opt("g", 0.3, prim="space.move", token="space:=4", move="=4"), {"space": None}) is None   # direction unknown
    assert h(opt("g", 0.3, prim="space.move", token="space:=2", move="=2"), {"space": 2}) is None      # no move
    assert h({"primitive": "nothing", "token": "nothing"}, {}) is None
    assert h({"primitive": "app.open", "token": "activate:", "params": {"app": " "}}, {}) is None
    q = live.hud_payload([opt("n", 0.2, prim="space.move", token="space:=4", move="=4")], {"space": None},
                         resolve_app=lambda a: None)
    assert "hint" not in q["options"][0]


# ---- executor ----------------------------------------------------------------------------------

class FakeRunner:
    def __init__(self, ax_rc=0, ax_err=""):
        self.calls, self.ax_rc, self.ax_err = [], ax_rc, ax_err

    def __call__(self, argv):
        self.calls.append(argv)
        if 'keystroke ""' in argv[-1]:
            return self.ax_rc, self.ax_err
        return 0, ""


def test_executor_dispatch():
    r = FakeRunner()
    ex = live.Executor(r)
    url_for = lambda hs: "https://example.org/kanban?board=1" if hs == "example.org/kanban" else "https://x/"
    assert ex.run(opt("a", 0.5, token="activate:Mail"), {}, url_for) == "activate:Mail"
    assert ex.run(opt("u", 0.5, prim="url.open", token="url:example.org/kanban", url="example.org/kanban"),
                  {}, url_for) == "url:example.org/kanban"
    assert ex.run(opt("s", 0.5, prim="space.move", token="space:=3", move="=3"), {"space": 1}, url_for) == "space:=3"
    assert ex.run(None, {}, url_for) is None                                    # nothing
    assert ex.run(opt("t", 0.5, prim="key.send", token="key:⌘C", chord="⌘C"), {}, url_for) is None
    assert r.calls[0] == ["open", "-a", "Mail"]
    assert r.calls[1] == ["open", "-a", "Safari", "https://example.org/kanban?board=1"]
    assert r.calls[2][0] == "osascript"                                          # the one AX probe
    assert [c[-1] for c in r.calls[3:]] == ['tell application "System Events" to key code 124 using control down'] * 2
    assert len(r.calls) == 5                                                     # key.send ran nothing


def test_executor_space_skipped_without_accessibility():
    r = FakeRunner(ax_rc=1, ax_err="execution error: (-1719)")
    ex = live.Executor(r)
    o = opt("s", 0.5, prim="space.move", token="space:-1", move="-1")
    assert ex.run(o, {"space": 2}, lambda hs: "") is None
    assert ex.run(o, {"space": 2}, lambda hs: "") is None
    assert len(r.calls) == 1                                                     # probed once, never pressed


def test_accessibility_reprobed_after_ten_minutes():
    r = FakeRunner(ax_rc=1, ax_err="(-1719)")
    t = [0.0]
    ex = live.Executor(r, clock=lambda: t[0])
    o = opt("s", 0.5, prim="space.move", token="space:+1", move="+1")
    assert ex.run(o, {"space": 1}, lambda hs: "") is None
    t[0] = 599.0
    assert ex.run(o, {"space": 1}, lambda hs: "") is None and len(r.calls) == 1   # cached refusal
    r.ax_rc = 0                                                                   # granted meanwhile
    t[0] = 600.0
    assert ex.run(o, {"space": 1}, lambda hs: "") == "space:+1"
    assert len(r.calls) == 3                                                      # re-probe + the key press
    t[0] = 5000.0
    ex.run(o, {"space": 1}, lambda hs: "")
    assert sum('keystroke ""' in c[-1] for c in r.calls) == 2                     # "granted" stays cached


def test_url_for_uses_last_full_safari_url():
    lv = live.Live(pull=False, sender=lambda *a: None)
    b = "oi-watcher-safari_host-a"
    mk = lambda i, url: {"id": i, "timestamp": f"2026-01-05T09:00:0{i}+00:00", "duration": 1, "data": {"url": url}}
    lv.raw = {b: {1: mk(1, "https://example.org/kanban/old"), 2: mk(2, "https://example.org/kanban/b?token=s3cr3t#x"),
                  3: mk(3, "javascript:alert(1)")}}
    assert lv.url_for("example.org/kanban") == "https://example.org/kanban/b"   # newest; query + fragment dropped
    assert lv.url_for("other.net/page") == "https://other.net/"
    assert live.safe_url("http://h.example:8080?q=1") == "http://h.example:8080/"
    assert live.safe_url("file:///etc/passwd") is None and live.safe_url("javascript:x") is None


def test_aw_down_logged_once_per_minute(monkeypatch, capsys):
    from urllib.error import URLError
    monkeypatch.setattr(live.aw, "pull", lambda **kw: (_ for _ in ()).throw(URLError("refused")))
    t = [1000.0]
    monkeypatch.setattr(live.time, "monotonic", lambda: t[0])
    lv = live.Live(sender=lambda *a: None)
    for dt in (0, 2, 30, 59):
        t[0] = 1000.0 + dt
        lv.pull()
    t[0] = 1060.0
    lv.pull()
    assert capsys.readouterr().err.count("aw-server unreachable") == 2


# ---- driver: evaluate -> show -> reply -> hud-log -> tokens -> attach --------------------------

class FakeSender:
    def __init__(self, reply):
        self.reply, self.sent = reply, []

    def __call__(self, payload, on_reply):
        self.sent.append(payload)
        t = threading.Thread(target=on_reply, args=(self.reply,))
        t.start()
        return t


def _live(rows, model, reply, **kw):
    runner = FakeRunner()
    lv = live.Live(pull=False, sender=FakeSender(reply), runner=runner, out=io.StringIO(),
                   resolve_app=lambda a: f"/Applications/{a}.app", is_executable=lambda o: True, **kw)
    lv.day = rows[-1]["ts"][:10]
    lv.rows, lv.model = list(rows), model
    return lv, runner


def _showable(rows, model):
    """A prefix of the synthetic stream whose current moment passes the default gate."""
    from oi import candidates, forms
    for mo in reversed(build_moments(rows, tz=TZ)[200:400]):
        menu = candidates.build(forms.route(mo["context"]), mo, model, is_executable=lambda o: True)
        probs = model.distribution(mo)[0]
        h1 = model.features(mo)[1]
        sup = sum(model.counts["o1"].get(h1, {}).values())
        if live.gate(menu, probs.get("nothing", 0), CFG, now=0, last_show_at=None, paused=False,
                     hud_enabled=True, support=sup)[1] == "show":
            return [r for r in rows if r["ts"] <= mo["ts"]]
    raise AssertionError("no showable moment in the synthetic stream")


def test_evaluate_show_press_execute_log(rows, model):
    pre = _showable(rows, model)
    lv, runner = _live(pre, model, {"event": "pressed", "key": 2})
    st = settings.load()
    r = lv.evaluate(st, now=1000.0)
    assert r["reason"] == "show" and 1 <= len(r["payload"]["options"]) <= 3
    assert r["payload"]["ttl_ms"] == 6000           # W19: settings gate.ttl_ms default (W9: 15000)
    assert r["payload"]["mode"] == "hint" and r["payload"]["hint_ttl_ms"] == 4000   # W19: a hint by default (W20: 4000 ms)
    lv.wait_replies(5)
    logged = normalise.read_hud_log(live.now_iso(1000.0)[:10])
    assert len(logged) == 1
    row = logged[0]
    assert set(row) == {"ts", "form", "state_digest", "options", "shown", "outcome", "executed", "latency_ms",
                        "show_mode", "expanded", "numbering"}          # W19, W21b
    assert row["numbering"] == 2                                       # W21b: options numbered by key
    assert row["show_mode"] == "hint" and row["expanded"] is False
    assert row["shown"] is True and row["outcome"] == "pressed:2" and row["executed"] == row["options"][0]["token"]
    assert len(row["state_digest"]) == 40 and row["options"][0]["key"] == 2
    # W11: the hint each shown option carried (kind[:dir] only, never a title or url)
    sent = r["payload"]["options"]
    assert [o.get("hint") for o in row["options"]] == [
        (h["kind"] + (f":{h['dir']}" if h.get("dir") else "")) if (h := so.get("hint")) else None for so in sent]
    assert all(o.get("hint") in (None, "window", "edge:left", "edge:right") for o in row["options"])
    assert runner.calls and runner.calls[0][0] in ("open", "osascript")
    assert (lv.shows_today, lv.pressed_today) == (1, 1)
    # cooldown: a second context change 5 s later is not shown, and is logged as such
    lv.sender.reply = {"event": "dismissed", "reason": "ttl"}
    assert lv.evaluate(st, now=1005.0)["reason"] == "cooldown"
    assert normalise.read_hud_log(live.now_iso(1000.0)[:10])[-1]["outcome"] == "dropped:cooldown"
    assert len(lv.sender.sent) == 1


def test_show_ttl_from_settings_every_show(rows, model, data_dir):
    """W9: gate.ttl_ms is read for every show (no restart), clamped to 1.5-60 s."""
    pre = _showable(rows, model)
    lv, _ = _live(pre, model, {"event": "dismissed", "reason": "ttl"})
    paths.ensure_private_root()
    (data_dir / "settings.json").write_text(json.dumps({"gate": {"ttl_ms": 22000, "cooldown_s": 0}}))
    r = lv.evaluate(settings.load(), now=1000.0)
    assert r["reason"] == "show" and r["payload"]["ttl_ms"] == 22000
    assert lv.sender.sent[-1]["ttl_ms"] == 22000
    lv.wait_replies(5)


def test_show_ttl_ms_clamp_and_fallback():
    assert live.show_ttl_ms({}) == live.TTL_MS == 6000
    assert live.show_ttl_ms({"ttl_ms": 100}) == 1500 and live.show_ttl_ms({"ttl_ms": 10**7}) == 60000
    assert live.show_ttl_ms({"ttl_ms": True}) == 6000 and live.show_ttl_ms({"ttl_ms": "9"}) == 6000
    assert live.show_ttl_ms({"ttl_ms": 9000.7}) == 9000


def test_paused_or_hud_disabled_never_shows(rows, model, data_dir):
    pre = _showable(rows, model)
    lv, _ = _live(pre, model, {"event": "pressed", "key": 2})
    st = settings.load()
    assert lv.evaluate({**st, "paused": True}, now=1000.0)["reason"] == "paused"
    assert lv.evaluate({**st, "hud_enabled": False}, now=1000.0)["reason"] == "hud_disabled"
    assert lv.sender.sent == []
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"paused": True}))
    assert lv.step(force=True) == {"reason": "paused"}
    assert json.loads((paths.status_dir() / "oi-live.json").read_text())["state"] == "paused"


def test_denylisted_front_app_is_skipped_silently(rows, model, data_dir):
    pre = _showable(rows, model)
    app = build_moments(pre, tz=TZ)[-1]["context"]["app"]
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"denylist_apps": [app]}))
    lv, _ = _live(pre, model, {"event": "pressed", "key": 2})
    assert lv.evaluate(settings.load(), now=1000.0) == {"reason": "denylisted"}
    assert lv.sender.sent == [] and not paths.hud_log_dir().exists()


def test_dry_run_prints_and_does_not_send(rows, model):
    pre = _showable(rows, model)
    lv, _ = _live(pre, model, {"event": "pressed", "key": 2}, dry_run=True)
    assert lv.evaluate(settings.load(), now=1000.0)["reason"] == "dry_run"
    assert lv.sender.sent == []
    line = json.loads(lv.out.getvalue().splitlines()[0])
    assert line["cmd"] == "show" and line["options"][0]["key"] == 2
    assert normalise.read_hud_log(live.now_iso(1000.0)[:10])[0]["outcome"] == "dropped:dry_run"


def test_hud_log_round_trip_to_tokens_and_attach(data_dir):
    d = paths.hud_log_dir()
    d.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": "2026-01-05T09:00:10.000Z", "shown": True, "outcome": "pressed:2", "latency_ms": 1500},
        {"ts": "2026-01-05T09:00:30.000Z", "shown": True, "outcome": "dismissed:ttl", "latency_ms": 3000},
        {"ts": "2026-01-05T09:00:40.000Z", "shown": False, "outcome": "dropped:cooldown", "latency_ms": None},
    ]
    (d / "2026-01-05.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "{corrupt\n")
    toks = normalise.hud_tokens(normalise.read_hud_log("2026-01-05"))
    assert [(t["ts"], t["token"]) for t in toks] == [
        ("2026-01-05T09:00:10.000Z", "hud:shown"), ("2026-01-05T09:00:11.500Z", "hud:pressed:2"),
        ("2026-01-05T09:00:30.000Z", "hud:shown"), ("2026-01-05T09:00:33.000Z", "hud:dismissed")]
    assert all(vocab.validate_row(t) == [] and t["src"] == "oi-hud" for t in toks)
    # build_day merges them; moments ignore them except the press, attached to the preceding moment
    from conftest import WIN, ev
    from oi import aw
    aw.write_jsonl(paths.raw_dir() / WIN / "2026-01-05.jsonl",
                   [ev(1, 0, 5, app="AppA"), ev(2, 20, 5, app="AppB"), ev(3, 50, 5, app="AppA")])
    _, day = normalise.build_day("2026-01-05")
    assert [r["token"] for r in day] == ["activate:AppA", "hud:shown", "hud:pressed:2", "activate:AppB",
                                         "hud:shown", "hud:dismissed", "activate:AppA"]
    ms = build_moments(day, tz=TZ)
    assert [m["context"]["token"] for m in ms] == ["activate:AppA", "activate:AppB", "activate:AppA"]
    assert ms[0]["label"] == "activate:AppB" and ms[0]["hud_pressed"] == 2
    assert "hud_pressed" not in ms[1] and all(not h["token"].startswith("hud:") for m in ms for h in m["history"])
    assert cli.main(["stats", "--day", "2026-01-05", "--json"]) == 0


def test_stats_counts_hud(data_dir, capsys):
    test_hud_log_round_trip_to_tokens_and_attach(data_dir)
    capsys.readouterr()
    assert cli.main(["stats", "--day", "2026-01-05", "--json"]) == 0
    s = json.loads(capsys.readouterr().out)
    assert (s["hud_shown"], s["hud_pressed"]) == (2, 1)


def test_markov_catalogue_ignores_feedback():
    from oi.markov import mine_catalogue
    assert mine_catalogue(["hud:shown"] * 9 + ["activate:A"] * 5) == {"activate:A": 5}


# ---- status file -------------------------------------------------------------------------------

def test_status_file_and_counters(rows, model):
    pre = _showable(rows, model)
    lv, _ = _live(pre, model, {"event": "dismissed", "reason": "esc"})
    lv.state = "running"
    lv.evaluate(settings.load(), now=1000.0)
    lv.wait_replies(5)
    lv.write_status(1001.0)
    st = json.loads((paths.status_dir() / "oi-live.json").read_text())
    assert set(st) == {"ts", "state", "pid", "dry_run", "shows_today", "pressed_today", "last_show_ts",
                       "model_trained_at", "moments_today", "context"}
    assert set(st["context"]) == {"app", "host", "desk"}          # W12: never a title or a URL path
    assert (st["state"], st["shows_today"], st["pressed_today"], st["moments_today"]) == ("running", 1, 0, 1)
    assert st["last_show_ts"] == live.now_iso(1000.0)
    assert oct((paths.status_dir() / "oi-live.json").stat().st_mode & 0o777) == "0o600"
    # a restart re-reads today's counters from the hud-log
    lv2, _ = _live(pre, model, {"event": "dismissed", "reason": "esc"})
    lv2.day = live.now_iso(1000.0)[:10]
    lv2.load_counters()
    assert (lv2.shows_today, lv2.pressed_today, lv2.last_show_ts) == (1, 0, live.now_iso(1000.0))


# ---- W10 summon ---------------------------------------------------------------------------

def test_summon_options_no_threshold_but_max_show():
    menu = [opt("a", 0.01, token="activate:A"), opt("b", 0.05, token="activate:B"),
            opt("t", 0.9, prim="app.focus", token="title:Notes/x"),
            {"key": "nothing", "p_markov": None, "primitive": None, "token": "nothing", "params": {}}]
    assert [o["key"] for o in live.summon_options(menu, CFG)] == ["b", "a"]   # below show_min_p: still shown
    many = [opt(f"o{i}", 0.01 * i, token=f"activate:A{i}") for i in range(6)]
    assert len(live.summon_options(many, CFG)) == CFG["max_show"]
    assert live.summon_options(menu[2:], CFG) == []                          # nothing allowed: empty show


def test_summon_bypasses_gate_and_marks_row(rows, model):
    pre = _showable(rows, model)
    lv, _ = _live(pre, model, {"event": "dismissed", "reason": "ttl"})
    st = settings.load()
    assert lv.evaluate(st, now=1000.0)["reason"] == "show"
    assert lv.evaluate(st, now=1001.0)["reason"] == "cooldown"     # the gate would refuse now ...
    r = lv.summon(now=1002.0)                                       # ... a summon does not
    assert r["reason"] == "show" and 1 <= len(r["payload"]["options"]) <= CFG["max_show"]
    assert lv.sender.sent[-1] == r["payload"] and r["payload"]["ttl_ms"] == 6000
    assert "mode" not in r["payload"]                       # W19: a summon is always the full cross
    lv.wait_replies(5)
    logged = normalise.read_hud_log(live.now_iso(1000.0)[:10])
    summoned = [x for x in logged if x.get("summoned")]
    assert len(summoned) == 1 and summoned[0]["outcome"] == "dismissed:ttl" and summoned[0]["shown"] is True
    assert summoned[0]["show_mode"] == "full" and "expanded" not in summoned[0]   # W19
    assert all("summoned" not in x for x in logged if x is not summoned[0])   # gate rows unchanged


def test_summon_without_model_sends_an_empty_show(rows):
    lv, _ = _live(rows[:50], None, {"event": "dismissed", "reason": "ttl"})
    r = lv.summon(now=1000.0)
    assert r["reason"] == "show" and r["payload"]["options"] == []  # oi-hud draws the wish option alone
    lv.wait_replies(5)


def test_summon_respects_pause_and_hud_off(rows, model, data_dir):
    lv, _ = _live(_showable(rows, model), model, {"event": "pressed", "key": 2})
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"paused": True}))
    assert lv.summon(now=1000.0) == {"reason": "paused"}
    (data_dir / "settings.json").write_text(json.dumps({"hud_enabled": False}))
    assert lv.summon(now=1000.0) == {"reason": "hud_disabled"}
    assert lv.sender.sent == []


def test_summon_listener_round_trip_on_a_fake_socket(tmp_path):
    """A fake oi-hud: accepts the persistent connection, broadcasts wish + summon; the listener
    calls back on summon only, and reconnects after the server drops it."""
    import os
    import socket as so
    from pathlib import Path
    import tempfile
    d = tempfile.mkdtemp(prefix="oi-", dir="/tmp")
    path = f"{d}/h.sock"
    srv = so.socket(so.AF_UNIX, so.SOCK_STREAM)
    srv.bind(path)
    srv.listen(2)
    got, stop = [], threading.Event()
    seen = threading.Event()

    def on(ev):
        got.append(ev)
        seen.set()
    live.summon_listener(path, on, retry_s=0.05, stop=stop)
    try:
        for n in (1, 2):                                   # second round = after a reconnect
            c, _ = srv.accept()
            c.sendall(b'{"event":"wish","text_len":3}\nnot json\n{"event":"summon","ts":"2026-10-20T13:40:00Z"}\n')
            assert seen.wait(5)
            seen.clear()
            c.close()
        assert [e["event"] for e in got] == ["summon", "summon"]
    finally:
        stop.set()
        srv.close()


def test_click_press_keeps_via_and_vocal_rows_are_not_moments(rows, model):
    """the user 16:5x: a capsule clicked replies pressed + "via":"click"; the hud-log row keeps it. W12: the
    vocal rows of the same hud-log never count as moments or shows."""
    pre = _showable(rows, model)
    lv, runner = _live(pre, model, {"event": "pressed", "key": 2, "via": "click"})
    lv.evaluate(settings.load(), now=1000.0)
    lv.wait_replies(5)
    day = live.now_iso(1000.0)[:10]
    row = normalise.read_hud_log(day)[0]
    assert row["outcome"] == "pressed:2" and row["via"] == "click" and runner.calls
    lv.log_row({"ts": live.now_iso(1001.0), "kind": "vocal", "to": "nova", "ok": True, "dry_run": False,
                "text_len": 5, "audio": None, "error": None})
    lv.day = day
    lv.load_counters()
    assert (lv.moments_today, lv.shows_today, lv.pressed_today) == (1, 1, 1)
    toks = [t["token"] for t in normalise.hud_tokens(normalise.read_hud_log(day))]
    assert toks == ["hud:shown", "hud:pressed:2", "vocal:nova"]


# ---- W19: automatic shows as hints -----------------------------------------------------

def test_w19_hud_settings_defaults_and_clamp(data_dir):
    assert settings.load()["hud"] == {"auto_mode": "hint", "hint_ttl_ms": 4000, "fill_order": ["ctrl", "option", "cmd", "fn"]}   # W20: 2500 -> 4000
    data_dir.mkdir(parents=True, exist_ok=True)
    for raw, want in (({"auto_mode": "full", "hint_ttl_ms": 4000}, ("full", 4000)),
                      ({"auto_mode": "off"}, ("off", 4000)),
                      ({"auto_mode": "loud", "hint_ttl_ms": True}, ("hint", 4000)),
                      ({"hint_ttl_ms": 50}, ("hint", 500)), ({"hint_ttl_ms": 10**6}, ("hint", 10000)),
                      ("nope", ("hint", 4000))):
        (data_dir / "settings.json").write_text(json.dumps({"hud": raw}))
        h = settings.load()["hud"]
        assert (h["auto_mode"], h["hint_ttl_ms"]) == want


def _w19_mode(rows, model, data_dir, hud: dict, reply: dict):
    pre = _showable(rows, model)
    lv, runner = _live(pre, model, reply)
    paths.ensure_private_root()
    (data_dir / "settings.json").write_text(json.dumps({"hud": hud}))
    r = lv.evaluate(settings.load(), now=1000.0)
    lv.wait_replies(5)
    return lv, r, normalise.read_hud_log(live.now_iso(1000.0)[:10]), runner


def test_w19_auto_mode_full_keeps_old_show(rows, model, data_dir):
    lv, r, logged, _ = _w19_mode(rows, model, data_dir, {"auto_mode": "full"}, {"event": "dismissed", "reason": "ttl"})
    assert r["reason"] == "show" and "mode" not in r["payload"] and "hint_ttl_ms" not in r["payload"]
    assert logged[-1]["show_mode"] == "full" and "expanded" not in logged[-1]


def test_w19_auto_mode_off_sends_nothing(rows, model, data_dir):
    lv, r, logged, _ = _w19_mode(rows, model, data_dir, {"auto_mode": "off"}, {"event": "pressed", "key": 2})
    assert r == {"reason": "auto_off"} and lv.sender.sent == []
    assert logged[-1]["outcome"] == "dropped:auto_off" and logged[-1]["shown"] is False
    assert lv.last_show_at is None                      # nothing shown: no cooldown either


def test_w19_hint_developed_then_pressed_logs_expanded(rows, model, data_dir):
    lv, r, logged, runner = _w19_mode(rows, model, data_dir, {"auto_mode": "hint", "hint_ttl_ms": 3000},
                                      {"event": "pressed", "key": 2, "via": "key", "expanded": True})
    assert r["payload"]["mode"] == "hint" and r["payload"]["hint_ttl_ms"] == 3000
    row = logged[-1]
    assert row["show_mode"] == "hint" and row["expanded"] is True and row["outcome"] == "pressed:2"
    assert row["executed"] and runner.calls


def test_w19_hint_faded_logs_not_expanded(rows, model, data_dir):
    _, _, logged, runner = _w19_mode(rows, model, data_dir, {}, {"event": "dismissed", "reason": "hint_ttl"})
    row = logged[-1]
    assert row["show_mode"] == "hint" and row["expanded"] is False and row["outcome"] == "dismissed:hint_ttl"
    assert not runner.calls


def test_w19_socket_sender_marks_expanded(tmp_path):
    """oi-hud says {"event":"hint","phase":"expanded"} before the reply: the reply carries expanded=True."""
    import os
    import socket as so
    from pathlib import Path
    path = Path("/tmp") / f"oi-w19-{os.getpid()}.sock"
    if path.exists():
        path.unlink()
    srv = so.socket(so.AF_UNIX, so.SOCK_STREAM)
    srv.bind(str(path)); srv.listen(2)

    def serve(lines):
        c, _ = srv.accept()
        c.recv(65536)
        for ln in lines:
            c.sendall((json.dumps(ln) + "\n").encode())
        c.close()
    got = []
    try:
        for lines, want in (([{"event": "hint", "phase": "expanded"}, {"event": "pressed", "key": 2}], True),
                            ([{"event": "dismissed", "reason": "hint_ttl"}], None)):
            t = threading.Thread(target=serve, args=(lines,)); t.start()
            live.socket_sender(path, 5)({"cmd": "show", "mode": "hint", "options": []}, got.append).join(5)
            t.join(5)
            assert got[-1].get("expanded") is want
        assert got[0]["event"] == "pressed" and got[1]["reason"] == "hint_ttl"
    finally:
        srv.close(); path.unlink()
