#!/usr/bin/env python3
"""Bearer-token front for a remote laya-serve (stdlib only; docs/LAYA-REMOTE.md).

laya-serve has no authentication, so it stays on 127.0.0.1 and this proxy listens on the private interface:
it checks `Authorization: Bearer <token>` (constant-time), forwards only GET /health and POST /v1/systemone
(body <= 64 KB) to the upstream, and returns the upstream's status and JSON. It logs no request (no path, no
body, no client address), never sends a redirect, and has no other route. Each connection gets 10 s of socket
inactivity at most and at most 32 run at once (more are closed at once).

Environment: OI_LAYA_TOKEN (required, >= 32 characters, never the example placeholder; from an EnvironmentFile
readable by root only),
OI_LAYA_LISTEN (host:port, e.g. a private interface address), OI_LAYA_UPSTREAM (default 127.0.0.1:8782).
"""
from __future__ import annotations

import hmac
import http.client
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 64 * 1024
ROUTES = {("GET", "/health"), ("POST", "/v1/systemone")}
UPSTREAM_TIMEOUT_S = 30
CONN_TIMEOUT_S = 10          # slow clients (slowloris, a short body) are dropped
MAX_CONNS = 32
MIN_TOKEN = 32


def token_problem(token: str) -> str | None:
    """Why this token may not be used (empty, short, or a placeholder from the example file), None when fine."""
    if not token:
        return "OI_LAYA_TOKEN is empty"
    if len(token) < MIN_TOKEN:
        return f"OI_LAYA_TOKEN is shorter than {MIN_TOKEN} characters"
    if "REPLACE" in token.upper() or "__" in token or len(set(token)) < 8:
        return "OI_LAYA_TOKEN looks like a placeholder: generate one (openssl rand -base64 32)"
    return None


def make_handler(token: str, upstream: tuple[str, int]):
    want = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "oi-laya-proxy"
        sys_version = ""
        timeout = CONN_TIMEOUT_S

        def log_message(self, *a):          # no request logging
            pass

        def _reply(self, code: int, body: bytes = b"", ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self, method: str):
            got = (self.headers.get("Authorization") or "").encode()
            if not hmac.compare_digest(got, want):
                return self._reply(401, b'{"error":"unauthorized"}')
            if (method, self.path) not in ROUTES:
                return self._reply(404, b'{"error":"not_found"}')
            body = None
            if method == "POST":
                try:
                    n = int(self.headers.get("Content-Length") or "-1")
                except ValueError:
                    n = -1
                if not 0 <= n <= MAX_BODY:
                    return self._reply(413, b'{"error":"body_size"}')
                body = self.rfile.read(n)
            try:
                c = http.client.HTTPConnection(*upstream, timeout=UPSTREAM_TIMEOUT_S)
                c.request(method, self.path, body=body, headers={"Content-Type": "application/json"} if body else {})
                r = c.getresponse()
                data = r.read()
                c.close()
            except OSError:
                return self._reply(502, b'{"error":"upstream"}')
            code = 502 if 300 <= r.status < 400 else r.status      # never pass a redirect on
            self._reply(code, data, r.getheader("Content-Type") or "application/json")

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer with at most MAX_CONNS connections at once."""
    daemon_threads = True

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(MAX_CONNS)
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(listen: tuple[str, int], token: str, upstream: tuple[str, int]) -> Server:
    cls = Server
    if ":" in listen[0]:                                    # an IPv6 address (e.g. fd00::/8)
        cls = type("V6Server", (Server,), {"address_family": socket.AF_INET6})
    return cls(listen, make_handler(token, upstream))


def _addr(s: str) -> tuple[str, int]:
    host, _, port = s.rpartition(":")
    return host.strip("[]"), int(port)


def main() -> int:
    token = os.environ.get("OI_LAYA_TOKEN", "").strip()
    why = token_problem(token)
    if why:
        print(f"oi-laya-proxy: refusing to start: {why}", file=sys.stderr)
        return 2
    listen = _addr(os.environ.get("OI_LAYA_LISTEN", "127.0.0.1:8781"))
    upstream = _addr(os.environ.get("OI_LAYA_UPSTREAM", "127.0.0.1:8782"))
    srv = make_server(listen, token, upstream)
    print(f"oi-laya-proxy: listening on {listen[0]}:{listen[1]}", file=sys.stderr, flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
