"""W13: the Laya layer — client, memory rule, supervisor, shadow mode in `oi live`, replay metrics,
split discipline, dataset/training plumbing, promotion gate, improvement report.
Synthetic data only; the Laya server is a fake HTTP server or a fake client (no model is ever loaded)."""
import io
import json
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import synth
from conftest import TZ

from oi import cli, improve, laya, laya_train, live, normalise, paths, replay, settings
from oi.moments import build_moments

FORM = {"form_id": "generic.next_action", "question": {"instructions": "Which action will the user take next?"}}
MENU = [{"key": "open_app_mail", "criteria": "switch to Mail"}, {"key": "nothing", "criteria": "keep working here"}]


# ---- fake laya server ------------------------------------------------------------------------------------

class FakeLaya:
    def __init__(self, probs=None, delay=0.0, status=200, body=None):
        self.probs, self.delay, self.status, self.body, self.requests = probs, delay, status, body, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200 if self.path == "/health" else 404)
                self.end_headers()

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(req)
                time.sleep(fake.delay)
                crit = req["questions"]["next"]["criteria"]
                probs = fake.probs or {k: 1 / len(crit) for k in crit}
                data = fake.body if fake.body is not None else json.dumps(
                    {"answers": {"next": {"choice": max(probs, key=probs.get), "probabilities": probs}}}).encode()
                try:
                    self.send_response(fake.status)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_laya():
    servers = []

    def make(**kw):
        s = FakeLaya(**kw)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


# ---- client ----------------------------------------------------------------------------------------------

def test_client_request_shape_and_probs(fake_laya):
    s = fake_laya(probs={"open_app_mail": 0.7, "nothing": 0.3})
    r = laya.LayaClient(s.url, timeout_s=2).predict(FORM, MENU, "time: Mon 09:00\nnow: Mail")
    assert r["probs"] == {"open_app_mail": 0.7, "nothing": 0.3} and r["checkpoint"] == "laya:english"
    assert isinstance(r["latency_ms"], int) and r["latency_ms"] >= 0
    req = s.requests[0]
    assert req["state"] == {"body": "time: Mon 09:00\nnow: Mail"} and req["model"] == "english"
    assert req["questions"]["next"] == {"type": "choice", "instructions": FORM["question"]["instructions"],
                                        "criteria": {"open_app_mail": "switch to Mail", "nothing": "keep working here"}}
    assert laya.health(s.url)


def test_client_timeout_unreachable_http_and_bad_reply(fake_laya):
    slow = fake_laya(delay=0.6)
    t0 = time.monotonic()
    with pytest.raises(laya.LayaError) as e:
        laya.LayaClient(slow.url, timeout_s=0.1).predict(FORM, MENU, "x")
    assert e.value.reason == "timeout" and time.monotonic() - t0 < 0.5
    with pytest.raises(laya.LayaError) as e:
        laya.LayaClient("http://127.0.0.1:9", timeout_s=0.2).predict(FORM, MENU, "x")
    assert e.value.reason == "unreachable"
    assert not laya.health("http://127.0.0.1:9", timeout_s=0.2)
    with pytest.raises(laya.LayaError) as e:
        laya.LayaClient(fake_laya(status=500).url, timeout_s=2).predict(FORM, MENU, "x")
    assert e.value.reason == "http_500"
    with pytest.raises(laya.LayaError) as e:
        laya.LayaClient(fake_laya(body=b"not json").url, timeout_s=2).predict(FORM, MENU, "x")
    assert e.value.reason == "bad_reply"
    with pytest.raises(laya.LayaError) as e:   # the server answered for other keys: never trusted
        laya.LayaClient(fake_laya(probs={"zzz": 1.0}).url, timeout_s=2).predict(FORM, MENU, "x")
    assert e.value.reason == "keys_mismatch"


def test_temperature_scaling():
    p = {"a": 0.8, "b": 0.2}
    assert laya.apply_temperature(p, 1.0) == p
    hot = laya.apply_temperature(p, 2.0)
    assert abs(sum(hot.values()) - 1) < 1e-9 and 0.5 < hot["a"] < 0.8          # flatter
    cold = laya.apply_temperature(p, 0.5)
    assert cold["a"] > 0.9                                                      # sharper
    c = laya.LayaClient("http://x", temperatures={"generic.next_action": 2.0})
    assert c.temperatures["generic.next_action"] == 2.0


def test_client_from_settings_uses_checkpoint_meta(tmp_path):
    ck = tmp_path / "ck"
    ck.mkdir()
    (ck / "oi-laya.json").write_text(json.dumps({"version": "oi-laya-x", "temperatures": {"f": 1.5}}))
    c = laya.LayaClient.from_settings({"laya_url": "http://127.0.0.1:8784", "laya_checkpoint": str(ck),
                                       "laya_timeout_ms": 300})
    assert c.checkpoint == "oi-laya-x" and c.model is None and c.temperatures == {"f": 1.5} and c.timeout_s == 0.3
    d = laya.LayaClient.from_settings(settings.load()["live"])
    assert d.model == "english" and d.url == "http://127.0.0.1:8781" and d.timeout_s == laya.TIMEOUT_S


# ---- memory rule + supervisor ------------------------------------------------------------------------------

def test_memory_parsers_and_preflight():
    assert laya.swap_used_mb("total = 5120.00M  used = 4208.00M  free = 912.00M  (encrypted)") == 4208.0
    assert laya.swap_used_mb("total = 8.00G  used = 1.50G  free = 6.5G") == 1536.0
    vm = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:  1000.\n"
          "Pages active: 5.\nPages inactive:  1000.\nPages speculative:  48.\n")
    assert laya.available_mb(vm) == 2048 * 16384 / 2**20
    assert laya.footprint_mb(1, "zsh [1]: 64-bit    Footprint: 1680 KB (16384 bytes per page)") == 1680 / 1024
    assert laya.footprint_mb(1, "python [1]: 64-bit    Footprint: 2.5 GB") == 2560
    ps = ("  10 /x/laya/.venv/bin/python /x/laya/.venv/bin/laya-serve\n  11 /usr/bin/vim\n"
          "  12 python -m laya_ft.train --data-dir d\n  14 /bin/zsh -c cd x && oi laya-serve --check\n"
          "  15 grep laya-serve\n  13 /venv/bin/python -m oi.cli laya-serve --port 8781\n")
    assert [h.split()[0] for h in laya.heavy_processes(ps)] == ["10", "12", "13"]   # a supervisor counts
    assert [h.split()[0] for h in laya.heavy_processes(ps, exclude={13})] == ["10", "12"]   # ...but not itself
    secret = laya.heavy_processes("  21 /venv/bin/python /x/serve_coreml.py --token=SECRET --key SECRET2\n"
                                  "  22 /venv/bin/python -m laya_ft.train --token=SECRET\n"
                                  "  23 /x/laya-serve --api-key=SECRET", exclude=set())
    assert secret == ["21 python serve_coreml.py", "22 python -m laya_ft.train", "23 laya-serve"]
    assert "SECRET" not in " ".join(secret)
    assert laya.memory_preflight(swap_mb=1000, avail_mb=5000, heavy=[])[0]
    ok, why = laya.memory_preflight(swap_mb=4208, avail_mb=5000, heavy=[], top_mem=["Safari 9 2100 MB"])
    assert not ok and why.startswith("swap used 4208 MB >= 4096 MB")
    assert "heavy_processes: none" in why and "top memory: Safari 9 2100 MB" in why   # who holds the memory
    ok, why = laya.memory_preflight(swap_mb=1000, avail_mb=2000, heavy=["7 laya_ft.train"], top_mem=[])
    assert not ok and "heavy_processes: 7 laya_ft.train" in why
    ps_m = "  9 2150400 /Applications/Safari.app/Contents/MacOS/Safari\n 11 512000 /usr/bin/python3\n x y z\n 12 1024 zsh\n"
    assert laya.top_memory(2, ps_m) == ["Safari 9 2100 MB", "python3 11 500 MB"]
    ok, why = laya.memory_preflight(swap_mb=1000, avail_mb=5000, heavy=["10 laya-serve"])
    assert not ok and "heavy" in why


class FakeChild:
    def __init__(self, polls):
        self.pid, self.polls, self.terminated = 4242, list(polls), False

    def poll(self):
        return 0 if self.terminated else (self.polls.pop(0) if self.polls else 0)

    def terminate(self):
        self.terminated = True

    def wait(self, t=None):
        return 0

    def kill(self):
        self.terminated = True


def test_supervise_refuses_and_kills_over_cap(data_dir):
    msgs = []
    assert laya.supervise(preflight=lambda: (False, "swap used 4208 MB"), log=msgs.append) == 2
    assert json.loads(laya.status_path().read_text())["state"] == "refused"
    child = FakeChild([None, None, None])
    launched = []

    def popen(argv, env, stdin):
        launched.append((argv, env))
        return child
    sizes = iter([900.0, 5000.0])
    up, warmed = iter([False, True]), []
    rc = laya.supervise(preflight=lambda: (True, "ok"), measure=lambda pid: next(sizes), popen=popen,
                        sleep=lambda s: None, log=msgs.append, cap_mb=4096, healthy=lambda url: next(up),
                        warm=lambda url, model: warmed.append((url, model)) or 1234)
    assert rc == 3 and child.terminated
    st = json.loads(laya.status_path().read_text())
    assert st["state"] == "killed_cap" and st["peak_mb"] == 5000
    assert warmed == [("http://127.0.0.1:8781", "english")] and st["warm_ms"] == 1234   # once, when healthy
    argv, env = launched[0]
    assert argv[-1].endswith("laya-serve") and env["LAYA_HOST"] == "127.0.0.1" and env["LAYA_MODELS"] == "english"
    assert env["HF_HUB_OFFLINE"] == "1" and env["LAYA_PRELOAD"] == "1"           # one checkpoint, no hub call


def test_server_command_for_a_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("OI_LAYA_DIR", str(tmp_path / "ldir"))
    (tmp_path / "ck" / "final").mkdir(parents=True)
    argv, env = laya.server_command(str(tmp_path / "ck"), 8784)
    assert argv[1:] == ["-m", "laya_ft.serve_wrapper"] and env["LAYA_FT_MODEL_DIR"].endswith("ck/final")
    assert env["LAYA_FT_SERVE_HOST"] == "127.0.0.1" and env["LAYA_FT_SERVE_PORT"] == "8784"


def test_ensure_server(fake_laya):
    s = fake_laya()
    assert laya.ensure_server(s.url)[0] == "running"
    assert laya.ensure_server("http://127.0.0.1:9", preflight=lambda: (False, "swap"))[0] == "refused"
    spawned = []
    state, _ = laya.ensure_server("http://127.0.0.1:9", preflight=lambda: (True, "ok"),
                                  spawn=lambda argv: spawned.append(argv) or 1)
    assert state == "starting" and spawned[0][-3:] == ["laya-serve", "--port", "9"]


# ---- shadow mode in oi live ----------------------------------------------------------------------------------

class FakeSender:
    def __init__(self, reply):
        self.reply, self.sent = reply, []

    def __call__(self, payload, on_reply):
        self.sent.append(payload)
        t = threading.Thread(target=on_reply, args=(self.reply,))
        t.start()
        return t


class FakeClient:
    def __init__(self, delay=0.0, probs=None, error=None):
        self.delay, self.probs, self.error, self.calls, self.checkpoint = delay, probs, error, 0, "laya:fake"

    def predict(self, form, menu, text):
        self.calls += 1
        time.sleep(self.delay)
        if self.error:
            raise laya.LayaError(self.error)
        probs = self.probs(menu) if callable(self.probs) else {o["key"]: 1 / len(menu) for o in menu}
        return {"probs": probs, "latency_ms": int(self.delay * 1000), "checkpoint": self.checkpoint}


def _showable(rows, model):
    from oi import candidates, forms
    cfg = settings.GATE_DEFAULTS
    for mo in reversed(build_moments(rows, tz=TZ)[200:400]):
        menu = candidates.build(forms.route(mo["context"]), mo, model, is_executable=lambda o: True)
        sup = sum(model.counts["o1"].get(model.features(mo)[1], {}).values())
        if live.gate(menu, model.distribution(mo)[0].get("nothing", 0), cfg, now=0, last_show_at=None,
                     paused=False, hud_enabled=True, support=sup)[1] == "show":
            return [r for r in rows if r["ts"] <= mo["ts"]]
    raise AssertionError("no showable moment")


def _live(rows, model, client, reply=None, ensure=None):
    lv = live.Live(pull=False, sender=FakeSender(reply or {"event": "dismissed", "reason": "ttl"}),
                   runner=lambda argv: (0, ""), out=io.StringIO(), resolve_app=lambda a: None,
                   is_executable=lambda o: True, laya_client=lambda cfg: client,
                   ensure_laya=ensure or (lambda *a: ("refused", "test")))
    lv.day = rows[-1]["ts"][:10]
    lv.rows, lv.model = list(rows), model
    return lv


def _settings(data_dir, **live_cfg):
    paths.ensure_private_root()
    (data_dir / "settings.json").write_text(json.dumps({"live": live_cfg}))
    return settings.load()


def _wait_log(day, n=1, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        rows = normalise.read_hud_log(day)
        if len(rows) >= n:
            return rows
        time.sleep(0.02)
    return normalise.read_hud_log(day)


def test_shadow_off_never_calls_laya(rows, model, data_dir):
    pre = _showable(rows, model)
    client = FakeClient()
    lv = _live(pre, model, client)
    lv.evaluate(settings.load(), now=1000.0)
    lv.wait_replies(5)
    row = _wait_log(live.now_iso(1000.0)[:10])[0]
    assert client.calls == 0 and "laya" not in row


def test_shadow_logs_laya_and_never_delays_the_show(rows, model, data_dir):
    pre = _showable(rows, model)
    st = _settings(data_dir, laya_shadow=True)
    client = FakeClient(delay=0.5, probs=lambda menu: {o["key"]: (0.97 if o["key"] == "nothing" else 0.03 / (len(menu) - 1))
                                                      for o in menu})
    lv = _live(pre, model, client)
    t0 = time.monotonic()
    r = lv.evaluate(st, now=1000.0)
    assert time.monotonic() - t0 < 0.3 and r["reason"] == "show"     # the show went out before Laya answered
    assert lv.sender.sent                                              # Laya's 0.97 on nothing changed nothing
    lv.wait_replies(5)
    row = _wait_log(live.now_iso(1000.0)[:10])[0]
    assert row["shown"] is True and row["outcome"] == "dismissed:ttl"
    lay = row["laya"]
    assert set(lay) == {"probs", "latency_ms", "checkpoint", "markov_top", "markov_probs", "tokens"}
    assert lay["checkpoint"] == "laya:fake" and lay["probs"]["nothing"] == 0.97 and lay["latency_ms"] == 500
    assert set(lay["markov_probs"]) == set(lay["probs"]) == set(lay["tokens"]) and lay["tokens"]["nothing"] == "nothing"
    assert "laya_rank" not in row and "laya_rank_fallback" not in row                 # shadow: never ranks


def test_shadow_on_a_gate_drop_and_on_errors(rows, model, data_dir):
    pre = _showable(rows, model)
    st = _settings(data_dir, laya_shadow=True)
    calls = []
    client = FakeClient(error="unreachable")
    lv = _live(pre, model, client, ensure=lambda url, ck: calls.append((url, ck)) or ("starting", "pid 1"))
    lv.evaluate(st, now=1000.0)
    lv.wait_replies(5)
    assert lv.evaluate(st, now=1005.0)["reason"] == "cooldown"          # a gate drop is shadowed too
    rows_ = _wait_log(live.now_iso(1000.0)[:10], n=2)
    assert all(r["laya"]["error"] == "unreachable" for r in rows_)
    assert calls == [("http://127.0.0.1:8781", None)]                 # ensure_server once (10 min rate limit)


def test_shadow_busy_never_queues_more_than_one(rows, model, data_dir):
    pre = _showable(rows, model)
    st = _settings(data_dir, laya_shadow=True)
    client = FakeClient(delay=0.4)
    lv = _live(pre, model, client)
    rows_now = {}
    for i in range(4):
        row = {"ts": live.now_iso(2000.0 + i)}
        lv.shadow(row, {"form_id": "x"}, [{"key": "nothing", "p_markov": None}], f"s{i}", st["live"], {})
        rows_now[i] = row["_laya_fut"]
    assert rows_now[2].result(0)["error"] == "busy" and rows_now[3].result(0)["error"] == "busy"
    assert rows_now[1].result(5)["latency_ms"] == 400 and client.calls == 2


def test_shadow_same_state_asks_laya_once(rows, model, data_dir):
    st = _settings(data_dir, laya_shadow=True)
    client = FakeClient(delay=0.05)
    lv = _live(rows, model, client)
    menu = [{"key": "open_app_mail", "token": "activate:Mail", "p_markov": 0.4}, {"key": "nothing", "p_markov": None}]
    futs = []
    for i in range(3):
        row = {"ts": live.now_iso(3000.0 + i)}
        lv.shadow(row, {"form_id": "x"}, menu, "same state", st["live"], {"nothing": 0.6})
        futs.append(row["_laya_fut"])
    assert all(f.result(5)["latency_ms"] == 50 for f in futs) and client.calls == 1
    assert futs[0].result(0)["markov_top"] == "nothing" and futs[0].result(0)["tokens"]["open_app_mail"] == "activate:Mail"


def test_laya_rank_live_reranks_only_when_the_answer_is_in(rows, model, data_dir):
    pre = _showable(rows, model)
    nothing_heavy = lambda menu: {o["key"]: (0.97 if o["key"] == "nothing" else 0.03 / (len(menu) - 1)) for o in menu}
    st = _settings(data_dir, laya_rank="live")
    client = FakeClient(delay=0.3, probs=nothing_heavy)
    lv = _live(pre, model, client)
    t0 = time.monotonic()
    assert lv.evaluate(st, now=1000.0)["reason"] == "show"              # Markov first: the show never waits
    assert time.monotonic() - t0 < 0.25
    lv.wait_replies(5)
    first = _wait_log(live.now_iso(1000.0)[:10])[0]
    assert first["laya_rank_fallback"] == "pending" and first["laya"]["probs"]["nothing"] == 0.97
    assert lv.evaluate(st, now=1100.0)["reason"] == "nothing_likely"    # same state, answer in: Laya's P(nothing) gates
    second = _wait_log(live.now_iso(1000.0)[:10], n=2)[-1]
    assert second["laya_rank"] == "live" and client.calls == 1          # the same state was not asked twice
    lv2 = _live(pre, model, FakeClient(error="timeout"))
    lv2.evaluate(st, now=5000.0)
    lv2.wait_replies(5)
    time.sleep(0.1)
    assert lv2.evaluate(st, now=5100.0)["reason"] == "show"              # Laya failed: Markov, logged as a fallback
    lv2.wait_replies(5)
    assert _wait_log(live.now_iso(5000.0)[:10], n=4)[-1]["laya_rank_fallback"] == "timeout"


def test_laya_rank_blend_on_a_summon(rows, model, data_dir):
    pre = _showable(rows, model)
    st = _settings(data_dir, laya_rank="blend")
    lv = _live(pre, model, FakeClient())
    lv.evaluate(st, now=1000.0)
    lv.wait_replies(5)
    _wait_log(live.now_iso(1000.0)[:10])
    lv.summon(now=1001.0)
    lv.wait_replies(5)
    row = _wait_log(live.now_iso(1000.0)[:10], n=2)[-1]
    assert row["summoned"] is True and row["laya_rank"] == "blend"


def test_settings_laya_keys(data_dir):
    s = _settings(data_dir, laya_shadow="yes", predictor="gpt", laya_url="", laya_timeout_ms=-1)
    assert {k: s["live"][k] for k in settings.LIVE_LAYA_DEFAULTS} == settings.LIVE_LAYA_DEFAULTS
    s = _settings(data_dir, laya_shadow=True, predictor="blend", laya_url="http://127.0.0.1:8784", poll_s=3)
    assert s["live"]["laya_shadow"] is True and s["live"]["predictor"] == "blend" and s["live"]["poll_s"] == 3
    assert s["live"]["laya_rank"] == "blend"                         # legacy predictor -> the laya_rank switch
    assert settings.LIVE_LAYA_DEFAULTS["laya_rank"] == "shadow"      # default: shadow until the user switches
    assert _settings(data_dir, laya_rank="live", predictor="blend")["live"]["laya_rank"] == "live"
    assert _settings(data_dir, laya_rank="on")["live"]["laya_rank"] == "shadow"
    (data_dir / "settings.json").write_text(json.dumps({"paused": True, "gate": {"show_min_p": 0.1},
                                                        "live": {"poll_s": 5}}))
    settings.update_live({"predictor": "laya"})
    raw = json.loads((data_dir / "settings.json").read_text())
    assert raw["paused"] is True and raw["gate"] == {"show_min_p": 0.1} and raw["live"] == {"poll_s": 5, "predictor": "laya"}
    assert stat.S_IMODE((data_dir / "settings.json").stat().st_mode) == 0o600


# ---- replay metrics on a known toy set ----------------------------------------------------------------------

def rec(probs, gold, form="f", showable=("a", "b", "c"), lat=10):
    return {"form": form, "gold": gold, "probs": probs, "latency_ms": lat, "error": None, "showable": set(showable)}


def test_metrics_on_a_toy_set():
    recs = [rec({"a": 0.7, "b": 0.2, "nothing": 0.1}, "a", lat=10),     # top1 hit, shown at τ .5
            rec({"a": 0.6, "b": 0.3, "nothing": 0.1}, "b", lat=20),     # top3 hit only, shown but wrong
            rec({"a": 0.1, "b": 0.1, "nothing": 0.8}, "nothing", lat=30),  # top1 hit, nothing shown
            rec({"a": 0.4, "b": 0.35, "nothing": 0.25}, None, lat=40)]   # label outside the menu
    m = replay.metrics(recs, tau=0.5, tau_met=True)
    assert m["n"] == 4 and m["acc1"] == 0.5 and m["acc3"] == 0.75
    assert m["shown"] == 2 and m["shown_precision"] == 0.5 and m["coverage"] == 0.5
    assert m["latency_p50_ms"] == 20 and m["latency_p95_ms"] == 40
    # ECE: bins .4 (1 rec, acc 0), .6 (1, acc 0), .7 (1, acc 1), .8 (1, acc 1)
    assert m["ece"] == round((0.4 + 0.6 + 0.3 + 0.2) / 4, 4)
    assert replay.gate_stats(recs, 0.65) == (1.0, 0.25, 1)


def test_calibrate_tau_and_temperature():
    good = [rec({"a": 0.9, "b": 0.1}, "a") for _ in range(6)] + [rec({"a": 0.3, "b": 0.7}, "a") for _ in range(6)]
    tau, met = replay.calibrate_tau(good)
    assert met and replay.gate_stats(good, tau)[0] >= 0.4
    never = [rec({"a": 0.9, "b": 0.1}, "nothing") for _ in range(6)]   # gold never showable
    assert replay.calibrate_tau(never)[1] is False
    over = [rec({"a": 0.99, "b": 0.01}, "a") for _ in range(6)] + [rec({"a": 0.99, "b": 0.01}, "b") for _ in range(4)]
    assert replay.fit_temperature(over) > 1.5                          # over-confident -> flattened
    assert replay.fit_temperature([rec({"a": 1.0}, None)]) == 1.0


SYN_DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]


@pytest.fixture(scope="module")
def syn_rows():
    rows = synth.stream(days=4, seed=11, start=SYN_DAYS[0])
    by_day = {}
    for r in rows:
        by_day.setdefault(r["ts"][:10], []).append(r)
    return by_day


def _reader(by_day):
    return lambda d: list(by_day[d])


class NothingFirst:
    checkpoint = "fake"

    def predict(self, form, menu, text):
        return {"probs": {o["key"]: (0.6 if o["key"] == "nothing" else 0.4 / (len(menu) - 1)) for o in menu},
                "latency_ms": 7, "checkpoint": "fake"}


def test_replay_end_to_end_ledger_and_split_discipline(syn_rows, data_dir):
    rd = _reader(syn_rows)
    r = replay.run(SYN_DAYS[:2], SYN_DAYS[2:], predictors=("markov", "laya"), clients={"laya": NothingFirst()},
                   reader=rd, log=lambda m: None)
    assert r["test_days"] == SYN_DAYS[2:] and r["train_days"] == SYN_DAYS[:2] and r["first_touch"] == {"laya": True}
    assert set(r["results"]) == {"markov", "laya", "blend"} and r["results"]["laya"]["latency_p50_ms"] == 7
    mk = r["results"]["markov"]
    assert 0 < mk["acc1"] <= mk["acc3"] <= r["label_in_menu"] <= 1
    lay = r["results"]["laya"]          # a constant "nothing first" predictor: acc@1 = share of nothing labels
    assert 0 < r["majority_baseline"]["acc"] <= r["label_in_menu"]   # a constant guess among in-menu labels
    assert 0 < r["nothing_share"] < 1 and lay["acc1"] == pytest.approx(r["nothing_share"], abs=1e-3)
    assert r["tokens_per_state"]["n"] == r["moments"]["test"] and r["tokens_per_state"]["max"] <= 256
    md = open(r["path"][:-5] + ".md").read()
    assert "test days: 2026-10-14, 2026-10-15" in md and "| markov |" in md
    assert json.loads(replay.ledger_path().read_text())["entries"][0]["predictor"] == "laya:english"
    with pytest.raises(replay.SplitError, match="touched once"):
        replay.run(SYN_DAYS[:2], SYN_DAYS[2:], clients={"laya": NothingFirst()}, reader=rd, log=lambda m: None)
    again = replay.run(SYN_DAYS[:2], SYN_DAYS[2:], clients={"laya": NothingFirst()}, reader=rd, retouch=True,
                       log=lambda m: None)
    assert again["retouched"] is True and not laya_train.pass_bar(again, "laya")[0]
    # markov alone never touches the ledger
    replay.run(SYN_DAYS[:2], SYN_DAYS[2:], predictors=("markov",), reader=rd, log=lambda m: None)
    with pytest.raises(replay.SplitError, match="overlap"):
        replay.run(SYN_DAYS[:3], SYN_DAYS[2:], reader=rd)
    with pytest.raises(replay.SplitError, match="before"):
        replay.run(SYN_DAYS[2:], SYN_DAYS[:1], reader=rd)
    assert replay.split_days(SYN_DAYS, 2) == (SYN_DAYS[:2], SYN_DAYS[2:])
    with pytest.raises(replay.SplitError):
        replay.split_days(SYN_DAYS[:1], 2)


def test_replay_smoke_is_flagged_and_not_in_ledger(syn_rows, data_dir):
    d = SYN_DAYS[0]
    r = replay.run([d], [d], smoke=True, clients={"laya": NothingFirst()}, reader=_reader(syn_rows), log=lambda m: None)
    assert r["smoke"] is True and r["path"].endswith("-smoke.json") and not replay.ledger_path().exists()
    assert "SMOKE TEST, not a result" in open(r["path"][:-5] + ".md").read()
    with pytest.raises(replay.SplitError):
        replay.run(SYN_DAYS[:2], SYN_DAYS[:2], smoke=True, reader=_reader(syn_rows))


def test_cli_replay_one_day_needs_smoke(data_dir, capsys):
    paths.ensure_private_root()
    assert cli.main(["replay", "--days", "2026-10-12"]) == 2
    assert "--smoke" in capsys.readouterr().err


# ---- dataset / training plumbing -----------------------------------------------------------------------------

def _with_feedback(by_day):
    """Copy of the synthetic days with a wish and a HUD press after some moments of the first day."""
    out = {d: list(rs) for d, rs in by_day.items()}
    d0 = out[SYN_DAYS[0]]
    acts = [r for r in d0 if r["token"] == "activate:Mail"][:3]
    for r in acts:
        d0.append({**r, "token": "wish:open-safari-please", "dur": 0, "src": "oi-wish"})
    acts2 = [r for r in d0 if r["token"] == "activate:Code"][:2]
    for r in acts2:
        d0.append({**r, "token": "hud:pressed:1", "dur": 0, "src": "oi-hud"})
    for d in out:
        out[d].sort(key=lambda r: r["ts"])
    return out


def test_build_dataset_splits_labels_weights(syn_rows, data_dir, tmp_path):
    fb = _with_feedback(syn_rows)
    counts = laya_train.build_dataset(SYN_DAYS[:3], SYN_DAYS[3:], tmp_path / "data", reader=_reader(fb))
    rows = {n: [json.loads(l) for l in open(tmp_path / "data" / f"{n}.jsonl")] for n in ("train", "val", "test")}
    assert all(r["label"] in r["question"]["criteria"] for rs in rows.values() for r in rs)
    assert {r["day"] for r in rows["test"]} == {SYN_DAYS[3]} and SYN_DAYS[3] not in {r["day"] for r in rows["train"]}
    assert max(r["id"] for r in rows["train"]) < min(r["id"] for r in rows["val"])     # time order kept
    assert all(len(r["question"]["criteria"]) <= 16 for r in rows["train"])
    assert all(len(c.split()) <= 8 for r in rows["train"] for c in r["question"]["criteria"].values())
    assert counts["train"]["wish_rows"] >= 1 and counts["train"]["pressed_x3"] >= 1
    assert sum(1 for r in rows["train"] if r["id"].endswith("#wish")) == counts["train"]["wish_rows"]
    cw = json.loads((tmp_path / "data" / "class_weights.json").read_text())
    w = list(cw["weights"].values())
    assert abs(sum(w) / len(w) - 1) < 1e-3 and set(cw["weights"]) == {r["label"] for r in rows["train"]}
    assert stat.S_IMODE((tmp_path / "data" / "train.jsonl").stat().st_mode) == 0o600


def test_wish_key_matches_menu_words():
    menu = [{"key": "open_app_safari", "params": {"app": "Safari"}, "token": "activate:Safari"},
            {"key": "open_url_x", "params": {"url": "example.org/kanban"}, "token": "url:example.org/kanban"},
            {"key": "nothing", "params": {}, "token": "nothing"}]
    assert laya_train.wish_key(menu, "open-the-kanban") == "open_url_x"
    assert laya_train.wish_key(menu, "safari") == "open_app_safari"
    assert laya_train.wish_key(menu, "call-mum") is None


def test_train_runs_laya_ft_head_only_then_scores_once(syn_rows, data_dir):
    calls = []

    class P:
        returncode = 0

    def runner(argv, env):
        calls.append((argv, env))
        return P()
    meta = laya_train.train(SYN_DAYS[:3], SYN_DAYS[3:], runner=runner, preflight=lambda: (True, "ok"),
                            clients={"tuned": NothingFirst()}, reader=_reader(syn_rows), log=lambda m: None)
    argv, env = calls[0]
    assert argv[1:3] == ["-m", "laya_ft.train"] and "--freeze-encoder" in argv and "--embed-cache-dir" in argv
    for flag, val in (("--max-process-mb", "4096"), ("--class-weight", "file"), ("--early-stop-patience", "2"),
                      ("--device", "cpu")):
        assert argv[argv.index(flag) + 1] == val
    assert env["HF_HUB_OFFLINE"] == "1"
    ck = laya_train.models_dir() / meta["version"]
    saved = json.loads((ck / "oi-laya.json").read_text())
    assert saved["test_days"] == SYN_DAYS[3:] and saved["temperatures"] and saved["report"].endswith(".json")
    rep = meta["report_obj"]
    assert rep["kind"] == "laya-train" and rep["first_touch"] == {"tuned": True} and set(rep["results"]) >= {"markov", "tuned"}
    with pytest.raises(laya.LayaError, match="memory rule"):
        laya_train.train(SYN_DAYS[:3], SYN_DAYS[3:], runner=runner, preflight=lambda: (False, "swap 4208 MB"))


# ---- promotion gate ---------------------------------------------------------------------------------------------

def _report(**over):
    r = {"smoke": False, "retouched": False, "first_touch": {"tuned": True},
         "tokens_per_state": {"max": 240}, "tuned_checkpoint": "/ck/oi-laya-x",
         "results": {"markov": {"n": 400, "acc3": 0.50},
                     "tuned": {"n": 400, "acc3": 0.54, "shown_precision": 0.45, "coverage": 0.35, "ece": 0.05}}}
    for k, v in over.items():
        if k in ("tuned", "markov"):
            r["results"][k] = {**r["results"][k], **v}
        else:
            r[k] = v
    return r


@pytest.mark.parametrize("over", [
    {"smoke": True}, {"retouched": True}, {"first_touch": {"tuned": False}}, {"tuned": {"n": 299}},
    {"tuned": {"acc3": 0.529}}, {"tuned": {"shown_precision": 0.39}}, {"tuned": {"coverage": 0.29}},
    {"tuned": {"ece": 0.081}}, {"tokens_per_state": {"max": 300}}])
def test_promotion_gate_refuses(over, data_dir, tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(_report(**over)))
    ok, checks = laya_train.promote(str(p))
    assert not ok and sum(not c[1] for c in checks) == 1
    assert settings.load()["live"]["predictor"] == "markov"


def test_promotion_gate_passes_and_sets_predictor(data_dir, tmp_path, capsys):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(_report()))
    assert laya_train.promote(str(p), dry_run=True)[0] and settings.load()["live"]["predictor"] == "markov"
    assert cli.main(["laya-promote", "--report", str(p)]) == 0
    lv = settings.load()["live"]
    assert lv["predictor"] == "laya" and lv["laya_rank"] == "live" and lv["laya_checkpoint"] == "/ck/oi-laya-x" and lv["laya_url"].endswith(":8784")
    assert "PASS  beats Markov@3" in capsys.readouterr().out


# ---- improvement report ------------------------------------------------------------------------------------------

def test_improve_report_counts(syn_rows, data_dir):
    fb = _with_feedback(syn_rows)
    hud = [{"ts": "2026-10-12T09:00:00.000Z", "form": "A", "shown": True, "outcome": "pressed:1",
            "laya": {"probs": {"a": 0.9, "nothing": 0.1}, "latency_ms": 80, "markov_top": "a"}, "options": [{}]},
           {"ts": "2026-10-12T09:01:00.000Z", "form": "A", "shown": True, "outcome": "dismissed:esc",
            "laya": {"error": "timeout"}},
           {"ts": "2026-10-12T09:02:00.000Z", "form": "C", "shown": False, "outcome": "dropped:cooldown"}]
    r = improve.report(SYN_DAYS[:1], reader=_reader(fb), hud_reader=lambda d: hud)
    a = r["forms"]["A"]
    assert a["pressed"] == 1 and a["dismissed"] == {"esc": 1} and a["laya_rows"] == 2
    assert a["laya_errors"] == {"timeout": 1} and a["laya_latency_p50"] == 80 and a["laya_agree_top1"] == 1
    assert r["forms"]["C"]["dropped"] == {"cooldown": 1}
    assert a["numbering"] == {"1": 2}                    # W21b: rows without the field are rank-numbered (1)
    assert sum(f["wishes"] for f in r["forms"].values()) >= 1
    assert sum(f["moments"] for f in r["forms"].values()) > 100
    path = improve.write(r)
    assert "LOCAL ONLY" in open(path).read()


def test_laya_timeout_clamped(data_dir):
    assert _settings(data_dir, laya_timeout_ms=10)["live"]["laya_timeout_ms"] == 100
    assert _settings(data_dir, laya_timeout_ms=10**9)["live"]["laya_timeout_ms"] == 5000
    assert laya.LayaClient.from_settings({"laya_timeout_ms": 1}).timeout_s == 0.1
    assert laya.LayaClient.from_settings({"laya_timeout_ms": 10**9}).timeout_s == 5.0


def test_laya_timeout_former_defaults_read_as_current(data_dir):
    """2026-10-21: 1050 ms timed out 35 % of live calls. A stored former default (the settings app writes every key
    back) is the current default (3000, above the live tail); another value is the user's choice and kept."""
    assert laya.TIMEOUT_S == 3.0 and settings.LIVE_LAYA_DEFAULTS["laya_timeout_ms"] == 3000
    for legacy in settings.LEGACY_LAYA_TIMEOUTS_MS:
        assert _settings(data_dir, laya_timeout_ms=legacy)["live"]["laya_timeout_ms"] == 3000
    assert _settings(data_dir, laya_timeout_ms=1500)["live"]["laya_timeout_ms"] == 1500
    assert laya.LayaClient.from_settings(_settings(data_dir, laya_timeout_ms=1050)["live"]).timeout_s == 3.0


def test_health_info_reads_loaded_models():
    class R:
        status = 200

        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    assert laya.health_info(opener=lambda u, timeout: R(b'{"status":"ok","loaded":["english"]}'))["loaded"] == ["english"]
    assert laya.health_info(opener=lambda u, timeout: R(b"[]")) is None

    def down(u, timeout):
        raise OSError("refused")
    assert laya.health_info(opener=down) is None
