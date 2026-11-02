"""Optional remote Laya: run the reranker on another machine you control and free the Mac's memory (~3.2 GB peak).

Settings `live.laya_mode`: "local" (default: the supervised server on 127.0.0.1), "remote" (no local server is ever
started; every Laya call goes to `live.laya_url` on your own server) or "off" (Markov only, like the menu-bar switch
off). See docs/LAYA-REMOTE.md.

Remote rules:
  URL     https to any host, or plain http only to a private address: 127.0.0.0/8, 10/8, 172.16/12, 192.168/16,
          100.64.0.0/10 (CGNAT / tailnet), ::1, fd00::/8, `localhost` or a `*.ts.net` name. Plain http to anything
          else is refused (no request is sent; the call is a logged miss `url_refused`). Names are never resolved.
  Token   a bearer token in the macOS Keychain (service `os-intent-laya`, account = the URL's host), read with
          `security find-generic-password`; never in settings.json, never in argv, never logged. No token: no request
          (`no_token`): the moment is never sent unauthenticated.
  Redirect  never followed to another origin (scheme, host or port): the request fails instead, so the token and
          the moment never reach a third host.
  Timeout `live.laya_remote_timeout_ms` (default 5000, 100..10000): a network round trip plus a CPU forward pass on
          the server sits above the local 3 s tail. The call stays off the HUD path, so a late or failed answer is a
          logged miss and Markov ranks as today.

`oi laya-remote enable --url URL` prints what is sent, asks y/N, stores the token (typed, never echoed) in the
Keychain, switches to remote and stops the local server. `oi laya-remote disable` goes back to local.
"""
from __future__ import annotations

import getpass
import ipaddress
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request

from . import settings

SERVICE = "os-intent-laya"
MODES = ("local", "remote", "off")
REMOTE_TIMEOUT_MS = 5000
REMOTE_TIMEOUT_RANGE_MS = (100, 10000)
SECURITY = "/usr/bin/security"
_PRIVATE_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "::1/128", "fd00::/8")]
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,512}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.:-]{1,253}$")

NOTICE = """\
Remote Laya sends, for every moment Laya is asked about, to {url}:
  - the front app name, the web site host and the window / page title (60 characters at most),
  - your last 5 actions (app switches, pages, with how long ago and how long), the day, time and desktop
    number, and today's top 3 apps with durations (the serialised moment, about 256 tokens at most),
  - the menu of candidate next actions (short labels such as "switch to Mail"; a label can name an app,
    a site, a file or a contact),
  - for a typed wish: the wish text (200 characters at most) and the front app.
It never sends screenshots, file contents, or mail or message bodies. The token (kept in the Keychain)
travels only in the Authorization header and is never written to settings or logs.
The server answers with probabilities only. Who runs that server sees this data: use a machine you control,
on a private network or over https."""


class RemoteURLRefused(ValueError):
    pass


def mode(live: dict) -> str:
    m = live.get("laya_mode")
    return m if m in MODES else "local"


def is_private_host(host: str) -> bool:
    host = (host or "").strip("[]").lower().rstrip(".")
    if host == "localhost" or host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False                        # a name other than localhost / *.ts.net: never resolved, not trusted
    return any(ip in n for n in _PRIVATE_NETS)


def check_url(url: str) -> str | None:
    """None when the URL is allowed in remote mode, else a short reason."""
    try:
        u = urllib.parse.urlsplit(url or "")
        host = u.hostname or ""
        u.port                              # raises on a bad port
    except ValueError:
        return "bad URL"
    if u.scheme not in ("http", "https") or not host:
        return "URL must be http(s)://host[:port]"
    if u.username or u.password:
        return "no credentials in the URL (the token lives in the Keychain)"
    if u.scheme == "http" and not is_private_host(host):
        return "plain http to a public address (use https, or a private / tailnet address)"
    return None


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def keychain_token(host: str, run=subprocess.run) -> str | None:
    """The bearer token for `host` (Keychain service os-intent-laya, account = host), None when absent."""
    if not host or not _HOST_RE.match(host):
        return None
    try:
        p = run([SECURITY, "find-generic-password", "-s", SERVICE, "-a", host, "-w"],
                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    tok = (p.stdout or "").strip()
    return tok if p.returncode == 0 and tok else None


def store_token(host: str, token: str, run=subprocess.run) -> bool:
    """Add or update the Keychain item. The command goes through `security -i` on stdin, so the token never appears
    in a process list. Returns True when the token reads back."""
    if not _HOST_RE.match(host or "") or not _TOKEN_RE.match(token or ""):
        return False
    try:
        run([SECURITY, "-i"], input=f"add-generic-password -U -s {SERVICE} -a {host} -w {token}\n",
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return keychain_token(host, run) == token


def delete_token(host: str, run=subprocess.run) -> bool:
    if not _HOST_RE.match(host or ""):
        return False
    try:
        return run([SECURITY, "delete-generic-password", "-s", SERVICE, "-a", host],
                   capture_output=True, text=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _origin(url: str) -> tuple:
    u = urllib.parse.urlsplit(url)
    return u.scheme, (u.hostname or "").lower(), u.port or {"http": 80, "https": 443}.get(u.scheme)


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the same origin; any other one fails the request (urllib would otherwise
    carry the Authorization header and the body to the new host)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = urllib.parse.urljoin(req.full_url, newurl)
        if _origin(newurl) != _origin(req.full_url):
            return None                     # -> HTTPError(code): a logged miss, nothing sent elsewhere
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener():
    return urllib.request.build_opener(SameOriginRedirect).open


def timeout_s(live: dict) -> float:
    v = live.get("laya_remote_timeout_ms")
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
        v = REMOTE_TIMEOUT_MS
    lo, hi = REMOTE_TIMEOUT_RANGE_MS
    return max(lo, min(hi, int(v))) / 1000


def request_kw(live: dict, token_fn=None) -> dict:
    """{} in local mode. Remote: {"headers": {"authorization": "Bearer …"}, "opener": same-origin opener}.
    Raises RemoteURLRefused(reason) for a refused URL or a missing token: nothing may be sent then."""
    if mode(live) != "remote":
        return {}
    url = live.get("laya_url") or ""
    why = check_url(url)
    if why:
        raise RemoteURLRefused("url_refused")
    tok = (token_fn or keychain_token)(host_of(url))
    if not tok:
        raise RemoteURLRefused("no_token")
    return {"headers": {"authorization": f"Bearer {tok}"}, "opener": opener()}


# ---- CLI ---------------------------------------------------------------------------------------------------

def cmd_laya_remote(args, *, ask=input, secret=getpass.getpass, run=subprocess.run, stop=None) -> int:
    from . import laya
    if args.action == "status":
        lv = settings.load()["live"]
        m, url = mode(lv), lv.get("laya_url") or laya.DEFAULT_URL
        out = {"mode": m, "url": url}
        if m == "remote":
            out["url_ok"] = check_url(url) is None
            out["token_in_keychain"] = keychain_token(host_of(url), run) is not None
            info = laya.configured_health_info(url)
            out["reachable"], out["probe_ms"] = info is not None, (info or {}).get("probe_ms")
        print(json.dumps(out))
        return 0
    if args.action == "disable":
        lv = settings.load()["live"]
        host = host_of(lv.get("laya_url") or "")
        settings.update_live({"laya_mode": "local", "laya_url": laya.DEFAULT_URL})
        msg = "laya: local mode (the local server starts on demand, under the memory rule)"
        if args.forget_token and host:
            msg += "; token " + ("removed from" if delete_token(host, run) else "not found in") + " the Keychain"
        print(msg)
        return 0
    # enable
    url = (args.url or "").rstrip("/")
    why = check_url(url)
    if why:
        print(f"refused: {why}", file=sys.stderr)
        return 2
    print(NOTICE.format(url=url))
    if not args.yes and ask("Send this to that server? [y/N] ").strip().lower() not in ("y", "yes"):
        print("not enabled: nothing changed")
        return 1
    host = host_of(url)
    if keychain_token(host, run) and not args.new_token:
        print(f"using the token already in the Keychain for {host}")
    else:
        tok = (sys.stdin.readline() if args.token_stdin else secret(f"Bearer token for {host} (not echoed): ")).strip()
        if not _TOKEN_RE.match(tok):
            print("refused: the token must be 16-512 characters of A-Z a-z 0-9 . _ ~ + / = -", file=sys.stderr)
            return 2
        if not store_token(host, tok, run):
            print("refused: could not store the token in the Keychain", file=sys.stderr)
            return 2
    settings.update_live({"laya_mode": "remote", "laya_url": url})
    from . import laya_switch
    print(f"laya: remote mode -> {url}; local server: {(stop or laya_switch.stop)()}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("laya-remote", help="optional remote Laya on a machine you control (docs/LAYA-REMOTE.md)")
    p.add_argument("action", choices=["enable", "disable", "status"])
    p.add_argument("--url", help="enable: https://… or http:// to a private / tailnet address")
    p.add_argument("--yes", action="store_true", help="enable: skip the y/N question (the notice is still printed)")
    p.add_argument("--token-stdin", action="store_true", help="enable: read the token from stdin instead of a prompt")
    p.add_argument("--new-token", action="store_true", help="enable: replace the token already in the Keychain")
    p.add_argument("--forget-token", action="store_true", help="disable: also delete the token from the Keychain")
    p.set_defaults(func=cmd_laya_remote)
