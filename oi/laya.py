"""The Laya layer (W13): client to the local Laya server + a memory-safe server supervisor.

Client. One `choice` question per moment, built from the routed form (design §0/§2): instructions = the form's
`question.instructions`, criteria = the candidate menu (`candidates.as_question`, criteria <= 8 words, <= 16
options, `nothing` always present), state = the serialised moment (`serialise.render`, <= 256 tokens target).
Wire format = laya-serve's `POST /v1/systemone` (the Laya wire format; the fine-tuned checkpoint is served by
laya-ft's `serve_wrapper`, same route and shape). Reply: `answers.next.probabilities` {key: p}. Every request
names ONE checkpoint (`model`, default "english"), so the server never lazily loads a second one (laya's
Router would otherwise route French titles to `laya-multilingual`). Runtime timeout = settings
`live.laya_timeout_ms` (default TIMEOUT_S = 3 s). The call runs off the HUD path (its answer merges into the row and
the re-rank cache whenever it lands), so the timeout only bounds a hung server: it must sit ABOVE the real live tail,
not at a bench p95. 2026-10-21: at 1.05 s, 79 of 225 live calls timed out (35 %), 12 of 17 after an idle gap > 10 min
(the cold forward pass measured 1116-1139 ms by the warm-up) and the successful calls ran right up to the cap
(p99 1048 ms). A slow or absent server is a logged miss, never a delay (`LayaError`). Optional per-form temperature (fitted offline by
`oi laya-train`) is applied to the returned probabilities: p_i ∝ p_i^(1/T) (= logits / T).

Server. `oi laya-serve` = a supervisor (the LaunchAgent `com.osintent.oi-laya` runs it; written, NOT installed:
`laya/install.sh`). It refuses to start unless the memory rule holds (one heavy ML process at a time, 4 GB cap): swap used < 4 GB, >= 3 GB available, no other heavy ML process (another laya server, a
laya-ft training run). It then runs ONE server with ONE checkpoint (stock English laya via `laya-serve`, or an oi
checkpoint via laya-ft's serve_wrapper) bound to 127.0.0.1 with the Hugging Face hub OFFLINE (no cloud call,
cached weights only), polls the child's physical footprint (`footprint`, which counts MPS/GPU memory on Apple
Silicon, unlike RSS) every 2 s and kills it above the cap (4 GB). State in status/oi-laya.json (pid, port,
checkpoint, current and peak MB, warm_ms). The checkpoint is loaded at start (LAYA_PRELOAD=1); once /health
answers, the supervisor sends ONE warm-up prediction (`warm_up`) so the first forward pass (the slow, cold one:
~1.2 s measured vs ~150 ms warm) is never a user's call. A refusal for memory names the top memory users
(`top_memory`) so whoever reads the log can act. `ensure_server()` starts that supervisor detached when the server is down
and memory allows (rate-limited by the caller); it never waits.
"""
from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from . import candidates, paths, settings

DEFAULT_URL = "http://127.0.0.1:8781"
TUNED_PORT = 8784
QUESTION = "next"
TIMEOUT_S = 3.0           # = settings live.laya_timeout_ms default; was 1.05 (a bench p95): 35 % live timeouts (2026-10-21)
CAP_MB = 4096              # hard cap: the server's physical footprint (RSS + MPS)
SWAP_MAX_MB = 4096         # do not start anything heavy above this swap use
MIN_AVAIL_MB = 3072        # free + inactive + speculative pages before starting
POLL_S = 2.0
HEALTH_WAIT_S = 240.0
NOT_HEAVY_EXE = {"zsh", "-zsh", "bash", "-bash", "sh", "grep", "rg", "pgrep", "ps", "tail", "less", "vim", "launchctl"}
HEAVY_PATTERNS = ("laya-serve", "laya_ft.train", "laya_ft.serve_wrapper", "serve_coreml.py")


class LayaError(RuntimeError):
    """Laya unreachable, too slow, or an unexpected reply. `reason` is short and loggable."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---- client -------------------------------------------------------------------------------------------

def build_request(form: dict, menu: list[dict], state_text: str, model: str | None = "english") -> dict:
    req = {"state": {"body": state_text},
           "questions": {QUESTION: candidates.as_question(form, menu)}}
    if model:
        req["model"] = model
    return req


def apply_temperature(probs: dict[str, float], t: float | None) -> dict[str, float]:
    """p_i ∝ p_i^(1/T): temperature scaling of the logits, done on probabilities (log p = logit - const)."""
    if not probs or not t or t == 1.0:
        return dict(probs)
    logs = {k: math.log(max(p, 1e-9)) / t for k, p in probs.items()}
    m = max(logs.values())
    ex = {k: math.exp(v - m) for k, v in logs.items()}
    z = sum(ex.values())
    return {k: v / z for k, v in ex.items()}


class LayaClient:
    """predict(form, menu, text) -> {"probs": {key: p}, "latency_ms": int, "checkpoint": str}.
    `opener` is injectable (tests); `temperatures` = {form_id: T} from an oi checkpoint's oi-laya.json."""

    def __init__(self, url: str = DEFAULT_URL, *, timeout_s: float = TIMEOUT_S, model: str | None = "english",
                 checkpoint: str | None = None, temperatures: dict | None = None, opener=None):
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self.model = model
        self.checkpoint = checkpoint or f"laya:{model or 'auto'}"
        self.temperatures = temperatures or {}
        self.opener = opener or urllib.request.urlopen

    @classmethod
    def from_settings(cls, live_cfg: dict, **kw) -> "LayaClient":
        meta = load_checkpoint_meta(live_cfg.get("laya_checkpoint"))
        return cls(live_cfg.get("laya_url") or DEFAULT_URL,
                   timeout_s=max(0.1, min(5.0, float(live_cfg.get("laya_timeout_ms") or TIMEOUT_S * 1000) / 1000)),
                   model=None if meta else (live_cfg.get("laya_model") or "english"),
                   checkpoint=meta.get("version") if meta else None,
                   temperatures=(meta or {}).get("temperatures"), **kw)

    def predict(self, form: dict, menu: list[dict], state_text: str) -> dict:
        body = json.dumps(build_request(form, menu, state_text, self.model)).encode()
        req = urllib.request.Request(self.url + "/v1/systemone", data=body,
                                     headers={"content-type": "application/json"}, method="POST")
        t0 = time.monotonic()
        try:
            with self.opener(req, timeout=self.timeout_s) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise LayaError(f"http_{e.code}") from None
        except (TimeoutError, OSError, urllib.error.URLError) as e:
            reason = getattr(e, "reason", e)
            raise LayaError("timeout" if "timed out" in str(reason) or isinstance(reason, TimeoutError)
                            else "unreachable") from None
        latency = int((time.monotonic() - t0) * 1000)
        try:
            probs = json.loads(raw)["answers"][QUESTION]["probabilities"]
            probs = {str(k): float(v) for k, v in probs.items()}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise LayaError("bad_reply") from None
        want = {o["key"] for o in menu}
        if set(probs) != want:
            raise LayaError("keys_mismatch")
        probs = apply_temperature(probs, self.temperatures.get(form.get("form_id")))
        return {"probs": {k: round(v, 4) for k, v in probs.items()}, "latency_ms": latency,
                "checkpoint": self.checkpoint}


def health(url: str = DEFAULT_URL, timeout_s: float = 1.0, opener=None) -> bool:
    try:
        with (opener or urllib.request.urlopen)(url.rstrip("/") + "/health", timeout=timeout_s) as r:
            return r.status == 200
    except Exception:
        return False


def health_info(url: str = DEFAULT_URL, timeout_s: float = 1.0, opener=None) -> dict | None:
    """laya-serve's /health JSON ({"status", "loaded": [checkpoints], "device"}), None when down or unreadable."""
    try:
        with (opener or urllib.request.urlopen)(url.rstrip("/") + "/health", timeout=timeout_s) as r:
            if r.status != 200:
                return None
            d = json.loads(r.read())
            return d if isinstance(d, dict) else None
    except Exception:
        return None


def load_checkpoint_meta(ckpt: str | None) -> dict | None:
    """oi-laya.json of an oi checkpoint dir (written by `oi laya-train`), None for stock laya."""
    if not ckpt:
        return None
    try:
        return json.loads((Path(ckpt).expanduser() / "oi-laya.json").read_text())
    except (OSError, ValueError):
        return None


# ---- memory rule -------------------------------------------------------------------------------------

def _sh(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def swap_used_mb(text: str | None = None) -> float | None:
    """`sysctl vm.swapusage` -> used MB ("total = 5120.00M  used = 4208.00M  free = ...")."""
    text = text if text is not None else _sh(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used\s*=\s*([\d.]+)([MG])", text or "")
    if not m:
        return None
    return float(m.group(1)) * (1024 if m.group(2) == "G" else 1)


def available_mb(text: str | None = None) -> float | None:
    """`vm_stat` free + inactive + speculative pages, in MB."""
    text = text if text is not None else _sh(["/usr/bin/vm_stat"])
    page = re.search(r"page size of (\d+) bytes", text or "")
    if not page:
        return None
    n = 0
    for name in ("free", "inactive", "speculative"):
        m = re.search(rf"Pages {name}:\s+(\d+)", text)
        n += int(m.group(1)) if m else 0
    return n * int(page.group(1)) / 2**20


def footprint_mb(pid: int, text: str | None = None) -> float | None:
    """Physical footprint of a process (`footprint <pid>`): counts MPS/GPU allocations, unlike RSS."""
    text = text if text is not None else _sh(["/usr/bin/footprint", str(pid)])
    m = re.search(r"Footprint:\s*([\d.]+)\s*(KB|MB|GB|B)\b", text or "")
    if not m:
        rss = _sh(["/bin/ps", "-o", "rss=", "-p", str(pid)]).strip() if text is None else ""
        return int(rss) / 1024 if rss.isdigit() else None
    return float(m.group(1)) * {"B": 1 / 2**20, "KB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2)]


def _safe_cmd(cmd: str) -> str:
    """Executable basename + (python) the script basename or `-m` module. Never any other argument."""
    toks = cmd.split()
    exe = os.path.basename(toks[0])
    if not exe.lower().startswith("python"):
        return exe
    for i, t in enumerate(toks[1:], 1):
        if t == "-m" and i + 1 < len(toks):
            return f"{exe} -m {toks[i + 1].split('=')[0]}"
        if not t.startswith("-"):
            return f"{exe} {os.path.basename(t)}"
    return exe


def heavy_processes(ps_text: str | None = None, exclude: set[int] | None = None) -> list[str]:
    """Running heavy ML processes (a laya server or its `oi laya-serve` supervisor, a laya-ft run), as
    "pid exe [script|module]" (never arguments: they can hold secrets). This process itself is never counted (a supervisor checks the others before starting)."""
    ps_text = ps_text if ps_text is not None else _sh(["/bin/ps", "-axo", "pid=,command="])
    exclude = {os.getpid()} if exclude is None else exclude
    out = []
    for line in (ps_text or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, cmd = int(parts[0]), parts[1]
        if exclude and pid in exclude:
            continue
        if os.path.basename(cmd.split()[0]) in NOT_HEAVY_EXE:
            continue    # a shell / grep whose command TEXT mentions laya-serve is not a server
        if any(p in cmd for p in HEAVY_PATTERNS):
            out.append(f"{pid} {_safe_cmd(cmd)}")
    return out


def top_memory(n: int = 5, ps_text: str | None = None) -> list[str]:
    """The n biggest processes by resident memory, as "name pid MB" (executable name only, never arguments):
    what to look at when the swap rule refuses a start."""
    ps_text = ps_text if ps_text is not None else _sh(["/bin/ps", "-axo", "pid=,rss=,comm="])
    procs = []
    for line in (ps_text or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            procs.append((int(parts[1]), int(parts[0]), os.path.basename(parts[2].strip()) or parts[2]))
    procs.sort(reverse=True)
    return [f"{name} {pid} {rss // 1024} MB" for rss, pid, name in procs[:n]]


def memory_preflight(*, swap_mb: float | None = None, avail_mb: float | None = None,
                     heavy: list[str] | None = None, top_mem: list[str] | None = None) -> tuple[bool, str]:
    """(ok, reason): swap used < 4 GB, >= 3 GB available, no other heavy ML process."""
    swap_mb = swap_used_mb() if swap_mb is None else swap_mb
    avail_mb = available_mb() if avail_mb is None else avail_mb
    heavy = heavy_processes() if heavy is None else heavy

    def who() -> str:   # the swap/RAM refusal says who holds the memory (and any heavy ML job), so Atlas can act
        top = top_memory() if top_mem is None else top_mem
        return ("; heavy_processes: " + ("; ".join(heavy) if heavy else "none")
                + ("; top memory: " + ", ".join(top) if top else ""))
    if swap_mb is None or swap_mb >= SWAP_MAX_MB:
        return False, f"swap used {swap_mb if swap_mb is None else round(swap_mb)} MB >= {SWAP_MAX_MB} MB" + who()
    if avail_mb is None or avail_mb < MIN_AVAIL_MB:
        return False, f"available {avail_mb if avail_mb is None else round(avail_mb)} MB < {MIN_AVAIL_MB} MB" + who()
    if heavy:
        return False, "another heavy ML process runs: " + "; ".join(heavy)
    return True, f"swap {round(swap_mb)} MB, available {round(avail_mb)} MB"


# ---- server ------------------------------------------------------------------------------------------

def laya_dir() -> Path:
    env = os.environ.get("OI_LAYA_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / "laya-local"


def server_command(checkpoint: str | None, port: int, *, model: str = "english",
                   device: str = "mps") -> tuple[list[str], dict]:
    """(argv, env) for ONE checkpoint on 127.0.0.1:port, hub offline."""
    ldir = laya_dir()
    env = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTORCH_ENABLE_MPS_FALLBACK": "1",
           "TOKENIZERS_PARALLELISM": "false"}
    if os.environ.get("OI_LAYA_HF_HOME"):
        env["HF_HOME"] = os.environ["OI_LAYA_HF_HOME"]
    if checkpoint:
        final = Path(checkpoint).expanduser() / "final"
        env.update(LAYA_FT_MODEL_DIR=str(final if final.is_dir() else Path(checkpoint).expanduser()),
                   LAYA_FT_SERVE_HOST="127.0.0.1", LAYA_FT_SERVE_PORT=str(port), LAYA_FT_SERVE_DEVICE=device,
                   PYTHONPATH=str(ldir / "laya-ft" / "src"))
        return [str(ldir / "laya" / ".venv" / "bin" / "python"), "-m", "laya_ft.serve_wrapper"], env
    env.update(LAYA_HOST="127.0.0.1", LAYA_PORT=str(port), LAYA_MODELS=model, LAYA_PRELOAD="1",
               LAYA_DEVICE=device, LAYA_THREADS="4", LAYA_LOG_LEVEL="warning")
    return [str(ldir / "laya" / ".venv" / "bin" / "laya-serve")], env


def status_path() -> Path:
    return paths.status_dir() / "oi-laya.json"


def _write_status(st: dict) -> None:
    paths.ensure_private_root()
    d = paths.status_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".oi-laya.json.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(st) + "\n")
    os.replace(tmp, status_path())


WARM_TIMEOUT_S = 60.0
WARM_FORM = {"form_id": "warm", "question": {"instructions": "Which action comes next?"}}
WARM_MENU = [{"key": "open_app", "criteria": "open an app"}, {"key": "nothing", "criteria": "do nothing"}]


def warm_up(url: str, model: str | None = "english", opener=None) -> int | None:
    """One throw-away prediction (the cold first forward pass) -> its latency in ms, None on failure."""
    try:
        r = LayaClient(url, timeout_s=WARM_TIMEOUT_S, model=model, opener=opener).predict(
            WARM_FORM, WARM_MENU, "warm-up")
        return r["latency_ms"]
    except LayaError:
        return None


def supervise(checkpoint: str | None = None, port: int | None = None, *, cap_mb: float = CAP_MB,
              device: str = "mps", model: str = "english", preflight=memory_preflight,
              measure=footprint_mb, popen=subprocess.Popen, sleep=time.sleep, log=None,
              healthy=health, warm=warm_up) -> int:
    """Foreground: preflight, run ONE server, kill it above cap_mb. Returns an exit code
    (0 child exited, 2 refused by the memory rule, 3 killed by the cap)."""
    log = log or (lambda m: print(f"oi-laya: {m}", file=sys.stderr, flush=True))
    port = port or (TUNED_PORT if checkpoint else 8781)
    ok, why = preflight()
    if not ok:
        log(f"refused: {why}")
        _write_status({"state": "refused", "reason": why, "ts": time.time()})
        return 2
    argv, extra = server_command(checkpoint, port, model=model, device=device)
    child = popen(argv, env={**os.environ, **extra}, stdin=subprocess.DEVNULL)
    peak = 0.0
    st = {"state": "running", "pid": os.getpid(), "child_pid": child.pid, "port": port,
          "checkpoint": checkpoint or f"laya:{model}", "device": device, "cap_mb": cap_mb,
          "started_at": time.time()}
    log(f"started pid {child.pid} on 127.0.0.1:{port} ({st['checkpoint']}, {device}); {why}")

    def on_term(*_):
        child.terminate()
        _write_status({**st, "state": "stopped", "peak_mb": round(peak), "ts": time.time()})
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    url = f"http://127.0.0.1:{port}"
    try:
        while child.poll() is None:
            if "warm_ms" not in st and healthy(url):    # once: the cold forward pass, before any user call
                st["warm_ms"] = warm(url, None if checkpoint else model)
                log(f"warm-up prediction: {st['warm_ms']} ms")
            mb = measure(child.pid)
            if mb is not None:
                peak = max(peak, mb)
                if mb > cap_mb:
                    log(f"footprint {mb:.0f} MB > cap {cap_mb:.0f} MB: killing the server")
                    child.terminate()
                    try:
                        child.wait(10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                    _write_status({**st, "state": "killed_cap", "mb": round(mb), "peak_mb": round(peak),
                                   "ts": time.time()})
                    return 3
            _write_status({**st, "mb": None if mb is None else round(mb), "peak_mb": round(peak), "ts": time.time()})
            sleep(POLL_S)
    finally:
        if child.poll() is None:
            child.terminate()
    _write_status({**st, "state": "exited", "peak_mb": round(peak), "ts": time.time()})
    return 0


AGENT = "com.osintent.oi-laya"


def _agent_loaded() -> bool:
    """Is the (lazy) LaunchAgent loaded? Then ensure_server kickstarts it instead of spawning."""
    try:
        return subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{AGENT}"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_server(url: str = DEFAULT_URL, checkpoint: str | None = None, *, preflight=memory_preflight,
                  spawn=None) -> tuple[str, str]:
    """('running'|'starting'|'refused', detail). Starts `oi laya-serve` detached when the server is down and the
    memory rule allows; never waits for it (the first predictions miss until it is up). ('disabled', why) when
    the menu-bar switch `laya.enabled` is off (oi.laya_switch): nothing is started. A refusal is written to
    status/oi-laya.json like the supervisor's own, so the menu bar shows it."""
    if not settings.load()["laya"]["enabled"]:
        return "disabled", "laya.enabled is false (menu bar)"
    if health(url):
        return "running", url
    ok, why = preflight()
    if not ok:
        _write_status({"state": "refused", "reason": why, "ts": time.time()})
        return "refused", why
    port = int(url.rsplit(":", 1)[1].split("/")[0]) if url.count(":") >= 2 else 8781
    argv = [sys.executable, "-m", "oi.cli", "laya-serve", "--port", str(port)]
    if checkpoint:
        argv += ["--checkpoint", str(checkpoint)]
    if spawn is None and not checkpoint and port == 8781 and _agent_loaded():
        rc = subprocess.run(["/bin/launchctl", "kickstart", f"gui/{os.getuid()}/{AGENT}"],
                            capture_output=True, timeout=10).returncode
        return "starting", f"launchctl kickstart {AGENT} -> {rc}"
    if spawn is None:
        paths.ensure_private_root()
        logd = paths.data_dir() / "logs"
        logd.mkdir(parents=True, exist_ok=True)
        out = open(logd / "oi-laya.log", "a")
        p = subprocess.Popen(argv, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
        return "starting", f"pid {p.pid}"
    return "starting", str(spawn(argv))


@contextmanager
def managed_server(url: str, checkpoint: str | None = None, *, device: str = "mps", model: str = "english",
                   wait_s: float = HEALTH_WAIT_S, log=print):
    """For offline jobs (replay, laya-train): use the server at `url` if it is up (left running), else start ONE
    supervised server, wait for /health, yield, and stop it (so a later job can load another checkpoint)."""
    if health(url):
        yield {"started": False, "url": url}
        return
    ok, why = memory_preflight()
    if not ok:
        raise LayaError(f"memory rule: {why}")
    port = int(url.rsplit(":", 1)[1])
    argv = [sys.executable, "-m", "oi.cli", "laya-serve", "--port", str(port), "--device", device, "--model", model]
    if checkpoint:
        argv += ["--checkpoint", str(checkpoint)]
    paths.ensure_private_root()
    logd = paths.data_dir() / "logs"
    logd.mkdir(parents=True, exist_ok=True)
    with open(logd / "oi-laya.log", "a") as out:
        p = subprocess.Popen(argv, stdout=out, stderr=out, stdin=subprocess.DEVNULL)
    try:
        t0 = time.monotonic()
        while not health(url):
            if p.poll() is not None:
                raise LayaError(f"server exited ({p.returncode}); see logs/oi-laya.log")
            if time.monotonic() - t0 > wait_s:
                raise LayaError("server not healthy in time")
            time.sleep(1.0)
        log(f"laya server up in {time.monotonic() - t0:.0f} s at {url}")
        yield {"started": True, "url": url, "pid": p.pid}
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()
        try:
            st = json.loads(status_path().read_text())
            log(f"laya server stopped (peak footprint {st.get('peak_mb')} MB)")
        except (OSError, ValueError):
            pass
