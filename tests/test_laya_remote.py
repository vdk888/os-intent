"""Optional remote Laya (docs/LAYA-REMOTE.md): mode parsing, URL rule, Keychain token (fake `security`), no local
supervisor, auth header, redirect refusal, Markov fallback on timeout, health, CLI, and the server-side auth proxy.
Fake HTTP servers on 127.0.0.1 and invented tokens only; the real Keychain and no model are ever touched."""
from __future__ import annotations

import importlib.util
import io
import json
import threading
import time
import urllib.request
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from oi import health, intent_resolve, laya, laya_remote, laya_switch, paths, settings

FORM = {"form_id": "generic.next_action", "question": {"instructions": "Which action will the user take next?"}}
MENU = [{"key": "open_app_mail", "criteria": "switch to Mail"}, {"key": "nothing", "criteria": "keep working here"}]
TOKEN = "test-token-0123456789abcdef"
REPLY = json.dumps({"answers": {"next": {"choice": "nothing",
                                         "probabilities": {"open_app_mail": 0.25, "nothing": 0.75}}}}).encode()


class Server:
    """A fake remote: records every request (path, headers, body); `route(handler, method)` may answer instead."""

    def __init__(self, route=None):
        self.requests: list[dict] = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                srv.requests.append({"method": method, "path": self.path, "headers": dict(self.headers), "body": body})
                if route and route(self, method):
                    return
                data = REPLY if method == "POST" else json.dumps({"status": "ok", "loaded": ["english"]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._any("GET")

            def do_POST(self):
                self._any("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def servers():
    made = []

    def make(**kw):
        s = Server(**kw)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


@pytest.fixture
def keychain(monkeypatch):
    """A fake `security`: {(service, account): password}; every call recorded (argv + stdin)."""
    store: dict = {}
    calls: list = []

    def run(argv, input=None, **kw):
        calls.append((list(argv), input))
        if argv[1] == "find-generic-password":
            pw = store.get((argv[argv.index("-s") + 1], argv[argv.index("-a") + 1]))
            return SimpleNamespace(returncode=0 if pw else 44, stdout=(pw or "") + "\n", stderr="")
        if argv[1] == "-i":
            t = input.split()
            assert t[0] == "add-generic-password"
            store[(t[t.index("-s") + 1], t[t.index("-a") + 1])] = t[t.index("-w") + 1]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1] == "delete-generic-password":
            hit = store.pop((argv[argv.index("-s") + 1], argv[argv.index("-a") + 1]), None)
            return SimpleNamespace(returncode=0 if hit else 44, stdout="", stderr="")
        raise AssertionError(argv)
    monkeypatch.setattr(laya_remote, "keychain_token", lambda host, run_=run: _orig_token(host, run_))
    return SimpleNamespace(store=store, calls=calls, run=run)


_orig_token = laya_remote.keychain_token


def _write(data_dir, live: dict, **extra):
    paths.ensure_private_root()
    (data_dir / "settings.json").write_text(json.dumps({"live": live, **extra}))
    return settings.load()


def _remote(url, **kw):
    return {"laya_mode": "remote", "laya_url": url, "laya_shadow": True, **kw}


# ---- settings --------------------------------------------------------------------------------------------

def test_mode_parsing_defaults_and_bad_values(data_dir):
    s = settings.load()["live"]
    assert s["laya_mode"] == "local" and s["laya_remote_timeout_ms"] == 5000
    assert _write(data_dir, {"laya_mode": "cloud", "laya_remote_timeout_ms": "x"})["live"]["laya_mode"] == "local"
    lv = _write(data_dir, {"laya_mode": "remote", "laya_remote_timeout_ms": 99999})["live"]
    assert lv["laya_mode"] == "remote" and lv["laya_remote_timeout_ms"] == 10000
    assert laya_remote.mode({"laya_mode": None}) == "local"


def test_off_mode_is_markov_only_whatever_the_switch(data_dir):
    s = _write(data_dir, {"laya_mode": "off", "laya_shadow": True, "laya_rank": "live"}, laya={"enabled": True})
    assert s["laya"]["enabled"] is False
    assert (s["live"]["laya_shadow"], s["live"]["laya_rank"], s["live"]["predictor"]) == (False, "shadow", "markov")
    boom = lambda *a, **k: pytest.fail("nothing may start in off mode")  # noqa: E731
    assert laya.ensure_server("http://127.0.0.1:9", preflight=boom, spawn=boom)[0] == "disabled"


# ---- URL rule --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8781", "http://10.1.2.3:8781", "http://172.20.0.5", "http://192.168.1.20:8781",
    "http://100.64.0.10:8781", "http://100.127.255.1", "http://[fd00::1]:8781", "http://[::1]:8781",
    "http://localhost:8781", "http://laya-box.example.ts.net:8781", "https://laya.example.com",
    "https://203.0.113.7:8443"])
def test_allowed_urls(url):
    assert laya_remote.check_url(url) is None


@pytest.mark.parametrize("url", [
    "http://203.0.113.7:8781", "http://8.8.8.8", "http://172.32.0.1", "http://100.128.0.1",
    "http://laya.example.com", "http://laya.lan:8781", "http://[2001:db8::1]:8781", "ftp://10.0.0.1",
    "http://", "not a url", "http://user:pw@10.0.0.1:8781", "http://10.0.0.1:99999",
    "http://example.ts.net.evil.com"])
def test_refused_urls(url):
    assert laya_remote.check_url(url)


def test_a_refused_url_sends_nothing(data_dir, servers, keychain):
    keychain.store[(laya_remote.SERVICE, "203.0.113.7")] = TOKEN
    c = laya.LayaClient.from_settings(_remote("http://203.0.113.7:8781"))
    with pytest.raises(laya.LayaError) as e:
        c.predict(FORM, MENU, "time: Mon 09:00")
    assert e.value.reason == "url_refused" and keychain.calls == []


# ---- Keychain token --------------------------------------------------------------------------------------

def test_keychain_read_store_delete_with_a_fake_security(keychain):
    assert laya_remote.keychain_token("10.0.0.5", keychain.run) is None
    assert laya_remote.store_token("10.0.0.5", TOKEN, keychain.run)
    assert keychain.store == {("os-intent-laya", "10.0.0.5"): TOKEN}
    add = [c for c in keychain.calls if c[0][1] == "-i"][0]
    assert TOKEN not in " ".join(add[0]) and TOKEN in add[1]          # never in argv (process list), only on stdin
    assert laya_remote.keychain_token("10.0.0.5", keychain.run) == TOKEN
    assert laya_remote.delete_token("10.0.0.5", keychain.run) and keychain.store == {}


def test_bad_token_or_host_never_reaches_security(keychain):
    assert not laya_remote.store_token("10.0.0.5", "short", keychain.run)
    assert not laya_remote.store_token("10.0.0.5", TOKEN + " -A", keychain.run)
    assert not laya_remote.store_token("bad host; x", TOKEN, keychain.run)
    assert keychain.calls == []


def test_missing_security_binary_is_no_token():
    def run(*a, **k):
        raise FileNotFoundError("security")
    assert laya_remote.keychain_token("10.0.0.5", run) is None


def test_no_token_sends_nothing(data_dir, servers, keychain):
    s = servers()
    with pytest.raises(laya.LayaError) as e:
        laya.LayaClient.from_settings(_remote(s.url)).predict(FORM, MENU, "x")
    assert e.value.reason == "no_token" and s.requests == []


# ---- the client in remote mode ---------------------------------------------------------------------------

def test_auth_header_timeout_and_reply(data_dir, servers, keychain):
    s = servers()
    keychain.store[("os-intent-laya", "127.0.0.1")] = TOKEN
    c = laya.LayaClient.from_settings(_remote(s.url))
    assert c.timeout_s == 5.0                                           # remote default, not the local 3 s
    assert laya.LayaClient.from_settings(_remote(s.url, laya_remote_timeout_ms=800)).timeout_s == 0.8
    r = c.predict(FORM, MENU, "time: Mon 09:00")
    assert r["probs"] == {"open_app_mail": 0.25, "nothing": 0.75}
    req = s.requests[-1]
    assert req["path"] == "/v1/systemone" and req["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in repr(r)


def test_local_mode_sends_no_authorization(data_dir, servers, keychain):
    s = servers()
    keychain.store[("os-intent-laya", "127.0.0.1")] = TOKEN
    laya.LayaClient.from_settings({"laya_url": s.url}).predict(FORM, MENU, "x")
    assert "Authorization" not in s.requests[-1]["headers"] and keychain.calls == []


def _redirect_to(target):
    def route(h, method):
        h.send_response(307)
        h.send_header("Location", target + h.path)
        h.send_header("Content-Length", "0")
        h.end_headers()
        return True
    return route


def test_redirect_to_another_host_is_never_followed(data_dir, servers, keychain):
    other = servers()                                                  # another origin (another port)
    s = servers(route=_redirect_to(other.url))
    keychain.store[("os-intent-laya", "127.0.0.1")] = TOKEN
    c = laya.LayaClient.from_settings(_remote(s.url))
    with pytest.raises(laya.LayaError) as e:
        c.predict(FORM, MENU, "x")
    assert e.value.reason == "redirect_refused" and other.requests == []
    _write(data_dir, _remote(s.url))
    assert laya.configured_health(s.url) is False and other.requests == []


def test_same_origin_redirect_handler_only():
    h = laya_remote.SameOriginRedirect()
    req = urllib.request.Request("http://10.0.0.5:8781/health", headers={"authorization": "Bearer x"})
    assert h.redirect_request(req, None, 302, "", {}, "http://10.0.0.6:8781/health") is None
    assert h.redirect_request(req, None, 302, "", {}, "https://10.0.0.5:8781/health") is None
    assert h.redirect_request(req, None, 302, "", {}, "http://10.0.0.5:8782/health") is None
    assert h.redirect_request(req, None, 302, "", {}, "/v2/health") is not None


def test_remote_timeout_is_a_logged_miss_and_markov_ranks(data_dir, servers, keychain):
    def slow(h, method):
        time.sleep(0.5)
        return False
    s = servers(route=slow)
    keychain.store[("os-intent-laya", "127.0.0.1")] = TOKEN
    c = laya.LayaClient.from_settings(_remote(s.url, laya_remote_timeout_ms=100))
    t0 = time.monotonic()
    with pytest.raises(laya.LayaError) as e:
        c.predict(FORM, MENU, "x")
    assert e.value.reason == "timeout" and time.monotonic() - t0 < 0.45


def test_live_laya_call_returns_an_error_row_and_never_starts_a_server(data_dir, servers, keychain):
    """oi live's _laya_call: a remote miss is a result ({"error"}), Markov keeps the row; ensure_server ->
    'remote' starts nothing."""
    from oi import live
    _write(data_dir, _remote("http://127.0.0.1:9"))
    keychain.store[("os-intent-laya", "127.0.0.1")] = TOKEN
    spawned = []
    monkey_spawn = lambda argv: spawned.append(argv)  # noqa: E731
    calls = []

    def ensure(url, ckpt):
        r = laya.ensure_server(url, ckpt, preflight=lambda: pytest.fail("no memory preflight in remote mode"),
                               spawn=monkey_spawn)
        calls.append(r)
        return r
    d = live.Live.__new__(live.Live)
    d._laya_ensure_at, d.ensure_laya = -1e9, ensure
    out = d._laya_call(laya.LayaClient.from_settings(settings.load()["live"]), FORM, MENU, "x", settings.load()["live"])
    assert out["error"] == "unreachable" and calls == [("remote", "live.laya_mode is remote: no local server")]
    assert spawned == []


# ---- no local supervisor ---------------------------------------------------------------------------------

def test_ensure_server_never_starts_in_remote_mode(data_dir, monkeypatch):
    _write(data_dir, _remote("http://10.0.0.5:8781"))
    boom = lambda *a, **k: pytest.fail("no local server in remote mode")  # noqa: E731
    monkeypatch.setattr(laya, "health", boom)
    monkeypatch.setattr(laya, "_agent_loaded", boom)
    monkeypatch.setattr(laya.subprocess, "Popen", boom)
    monkeypatch.setattr(laya.subprocess, "run", boom)
    assert laya.ensure_server("http://10.0.0.5:8781", preflight=boom, spawn=boom)[0] == "remote"
    assert laya_switch.switch(True, stop_fn=boom)["result"] == "remote"


def test_switch_status_in_remote_mode_ignores_the_local_supervisor_file(data_dir, monkeypatch):
    _write(data_dir, _remote("http://10.0.0.5:8781"))
    monkeypatch.setattr(laya_switch, "read_status", lambda: pytest.fail("local status file read in remote mode"))
    r = laya_switch.status(probe=lambda url: None, now=1.0)
    assert r["mode"] == "remote" and r["state"] == "stopped" and r["up"] is False


def test_wish_resolver_health_uses_the_token(data_dir, servers, keychain):
    s = servers()
    keychain.store[("os-intent-laya", "127.0.0.1")] = TOKEN
    _write(data_dir, _remote(s.url))
    assert laya.configured_health(s.url, timeout_s=1.0)
    assert s.requests[-1]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "configured_health" in Path(intent_resolve.__file__).read_text()


# ---- oi health -------------------------------------------------------------------------------------------

def test_health_reports_remote_reachability_and_latency(data_dir, servers, keychain):
    s = servers()
    keychain.store[("os-intent-laya", "127.0.0.1")] = TOKEN
    _write(data_dir, _remote(s.url))
    info = laya.configured_health_info(s.url)
    assert info["loaded"] == ["english"] and isinstance(info["probe_ms"], int)
    p, notes, summ = health.check_laya(settings.load()["live"], info, {"state": "refused", "ts": 0}, [], 0.0)
    assert p == [] and summ["mode"] == "remote" and summ["timeout_ms"] == 5000 and summ["probe_ms"] is not None
    assert notes and "round trip" in notes[0] and "memory rule" not in " ".join(notes)


def test_health_remote_down_or_refused(data_dir):
    lv = _write(data_dir, _remote("http://10.0.0.5:8781"))["live"]
    assert health.check_laya(lv, None, None, [], 0.0)[0] == ["laya: remote server unreachable (or no token)"]
    lv = dict(lv, laya_url="http://203.0.113.7:8781")
    p = health.check_laya(lv, None, None, [], 0.0)[0]
    assert len(p) == 1 and p[0].startswith("laya: remote URL refused (plain http")


def test_health_no_oi_laya_agent_needed_in_remote_mode(data_dir, monkeypatch):
    _write(data_dir, _remote("http://10.0.0.5:8781"))
    listing = {f"com.osintent.{s}": ("123", "0") for s in health.SERVICES if s != "oi-laya"}
    r = health.run_checks(0.0, listing=lambda: listing, compile_=lambda lang, src: (True, ""),
                          enabled=lambda: [], laya_probe=lambda url: {"loaded": ["english"], "probe_ms": 12})
    assert not any(p.startswith("service oi-laya") for p in r["problems"]) and r["laya"]["probe_ms"] == 12


# ---- CLI -------------------------------------------------------------------------------------------------

def _args(action, **kw):
    base = dict(action=action, url=None, yes=False, token_stdin=False, new_token=False, forget_token=False)
    return SimpleNamespace(**(base | kw))


def test_cli_enable_prints_the_notice_asks_and_stores_the_token(data_dir, keychain):
    out = io.StringIO()
    stopped = []
    with redirect_stdout(out):
        rc = laya_remote.cmd_laya_remote(_args("enable", url="http://100.64.0.10:8781/"), ask=lambda q: "y",
                                         secret=lambda q: TOKEN, run=keychain.run, stop=lambda: stopped.append(1) or "x")
    text = out.getvalue()
    assert rc == 0 and "window / page title" in text and "screenshots" in text and TOKEN not in text
    assert keychain.store == {("os-intent-laya", "100.64.0.10"): TOKEN} and stopped == [1]
    raw = json.loads((data_dir / "settings.json").read_text())
    assert raw["live"] == {"laya_mode": "remote", "laya_url": "http://100.64.0.10:8781"}
    assert TOKEN not in (data_dir / "settings.json").read_text()


def test_cli_enable_no_answer_changes_nothing(data_dir, keychain):
    with redirect_stdout(io.StringIO()):
        rc = laya_remote.cmd_laya_remote(_args("enable", url="http://10.0.0.5:8781"), ask=lambda q: "",
                                         secret=lambda q: pytest.fail("no token asked"), run=keychain.run,
                                         stop=lambda: pytest.fail("nothing stopped"))
    assert rc == 1 and keychain.store == {} and not (data_dir / "settings.json").exists()


def test_cli_enable_refuses_public_http_before_any_question(data_dir, keychain, capsys):
    rc = laya_remote.cmd_laya_remote(_args("enable", url="http://203.0.113.7:8781"),
                                     ask=lambda q: pytest.fail("no question"), run=keychain.run)
    assert rc == 2 and "refused" in capsys.readouterr().err


def test_cli_disable_back_to_local_and_forget_token(data_dir, keychain, monkeypatch):
    _write(data_dir, _remote("http://10.0.0.5:8781"))
    keychain.store[("os-intent-laya", "10.0.0.5")] = TOKEN
    monkeypatch.setattr(laya_remote.subprocess, "run", keychain.run)
    with redirect_stdout(io.StringIO()):
        assert laya_remote.cmd_laya_remote(_args("disable", forget_token=True), run=keychain.run) == 0
    lv = settings.load()["live"]
    assert lv["laya_mode"] == "local" and lv["laya_url"] == laya.DEFAULT_URL and keychain.store == {}


def test_cli_is_registered():
    from oi import cli
    assert cli.build_parser().parse_args(["laya-remote", "status"]).func is laya_remote.cmd_laya_remote


# ---- server-side auth proxy ------------------------------------------------------------------------------

def _load_proxy():
    path = Path(__file__).resolve().parents[1] / "laya" / "remote" / "oi_laya_auth_proxy.py"
    spec = importlib.util.spec_from_file_location("oi_laya_auth_proxy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def proxy(servers):
    mod = _load_proxy()
    up = servers()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.make_handler(TOKEN, ("127.0.0.1", up.port)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{httpd.server_address[1]}", up=up, mod=mod)
    httpd.shutdown()


def _call(url, path, token=None, body=None):
    h = {"content-type": "application/json"} | ({"authorization": f"Bearer {token}"} if token else {})
    req = urllib.request.Request(url + path, data=body, headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_proxy_checks_the_token_and_forwards(proxy):
    assert _call(proxy.url, "/health")[0] == 401
    assert _call(proxy.url, "/health", token="wrong-token-0123456789")[0] == 401
    assert proxy.up.requests == []                                      # nothing forwarded without the token
    code, data = _call(proxy.url, "/v1/systemone", TOKEN, b'{"state": {"body": "x"}}')
    assert code == 200 and json.loads(data)["answers"]["next"]["probabilities"]["nothing"] == 0.75
    fwd = proxy.up.requests[-1]
    assert fwd["path"] == "/v1/systemone" and fwd["body"] == b'{"state": {"body": "x"}}'
    assert "Authorization" not in fwd["headers"]                       # the token stops at the proxy
    assert _call(proxy.url, "/health", TOKEN)[0] == 200


def test_proxy_routes_and_body_cap(proxy):
    assert _call(proxy.url, "/admin", TOKEN)[0] == 404
    assert _call(proxy.url, "/health", TOKEN, b"{}")[0] == 404            # POST /health is not a route
    assert _call(proxy.url, "/v1/systemone", TOKEN, b"x" * (64 * 1024 + 1))[0] == 413


def test_proxy_upstream_down_or_redirect_is_502(servers):
    mod = _load_proxy()
    up = servers(route=_redirect_to("http://203.0.113.7"))
    for port in (up.port, 9):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.make_handler(TOKEN, ("127.0.0.1", port)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            assert _call(f"http://127.0.0.1:{httpd.server_address[1]}", "/health", TOKEN)[0] == 502
        finally:
            httpd.shutdown()


def test_proxy_refuses_to_start_without_a_token(monkeypatch, capsys):
    mod = _load_proxy()
    monkeypatch.delenv("OI_LAYA_TOKEN", raising=False)
    assert mod.main() == 2 and "OI_LAYA_TOKEN" in capsys.readouterr().err


def test_proxy_logs_nothing(proxy, capsys):
    _call(proxy.url, "/v1/systemone", TOKEN, b'{"state": {"body": "secret title"}}')
    _call(proxy.url, "/health")
    cap = capsys.readouterr()
    assert "secret title" not in cap.err + cap.out and "/v1/systemone" not in cap.err + cap.out


def test_unit_examples_hold_placeholders_only():
    d = Path(__file__).resolve().parents[1] / "laya" / "remote"
    unit = (d / "oi-laya-remote.service.example").read_text()
    for need in ("DynamicUser=yes", "MemoryMax=3.5G", "CPUQuota=200%", "LAYA_HOST=127.0.0.1", "HF_HUB_OFFLINE=1"):
        assert need in unit
    front = (d / "oi-laya-auth-proxy.service.example").read_text()
    assert "EnvironmentFile=/etc/oi-laya/token.env" in front and "__PRIVATE_ADDR__" in front
