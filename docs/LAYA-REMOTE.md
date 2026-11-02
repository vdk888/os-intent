# Remote Laya (optional)

Laya, the reranker, normally runs on the Mac: one local server under the memory rule, about 3.2 GB at peak.
Remote mode runs it on another machine you control (your own server on a private network) instead, so the Mac
keeps that memory. It is off by default and nothing changes unless you turn it on.

**Which machine.** Use another Apple Silicon Mac or a machine with a GPU. A CPU-only server works but is slow:
measured with the English checkpoint on 2 vCPU, it answered in about 2.5 s once warm (the Mac's own server answers
in about 150 ms warm), so its answers mostly arrive after the HUD has already shown Markov's ranking.

## Modes

`settings.json` → `live.laya_mode`:

| Mode | What runs | What the Mac sends |
|---|---|---|
| `local` (default) | the supervised server on `127.0.0.1` (`oi laya-serve`, started on demand under the memory rule) | nothing leaves the Mac |
| `remote` | **no local Laya server is ever started**; every Laya call goes to `live.laya_url` | the serialised moment, to your server (below) |
| `off` | nothing: Markov only, whatever the menu-bar Laya switch says | nothing |

When Laya is slow, down or refused, the HUD ranks with Markov exactly as before: a remote miss is a logged miss
(`timeout`, `unreachable`, `http_401`, `url_refused`, `no_token`, `redirect_refused`), never a HUD delay.

## Privacy: what remote mode sends

For every moment Laya is asked about (shadow, blend or live ranking, and typed wishes), the Mac sends to the
configured server:

- the front app name and the window title, or in Safari the site host and the page's URL path turned into
  words (60 characters at most);
- your last 5 actions, with how long ago and for how long: app switches, pages, the labels of page buttons you
  clicked (`click:<host>/<role>:<label>`), short slugs of your past typed wishes (`wish:<slug>`) and the names
  of voice-note recipients (`vocal:<name>`);
- the day, time and desktop number, and today's top 3 apps with durations (the serialised moment, about 256
  tokens at most);
- the menu of candidate next actions (short labels; a label can name an app, a site, a file or a contact);
- for a typed wish: the wish text (200 characters at most) and the front app.

It never sends screenshots, file contents, or mail or message bodies. The server answers with probabilities
only. Whoever runs that server sees this data: use a machine you control, on a private network or over https.
`oi laya-remote enable` prints this list and asks before anything is switched on.

## Safety rules on the Mac

- **URL.** `https://` to any host, or plain `http://` only to a private address: `127.0.0.0/8`, `10.0.0.0/8`,
  `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10` (CGNAT, used by tailnets), `::1`, `fd00::/8`, `localhost`
  or a `*.ts.net` name. Plain http to anything else is refused and nothing is sent. Host names are never
  resolved to decide: a LAN name such as `laya.lan` needs https, or use its address. The host must be an IP
  address or a plain DNS name: no percent-encoding, backslash, user info or trailing dot.
- **When plain http is not safe.** A private address is only trustworthy on a network you control. If the Mac
  (a laptop) joins another network that uses the same range (a café, a hotel, another office on `192.168.1.x`
  or `10.x`), every moment and the token go **in clear** to whatever answers at that address there. A
  `*.ts.net` name over http is only safe while Tailscale's DNS answers it: with Tailscale off, the name goes to
  the local resolver, which can return any address. Prefer **https**, or a **tailnet IP** (`100.64.0.0/10`),
  which is only routed while the tailnet is up.
- **No proxy.** Remote calls never go through a proxy: neither `http_proxy` / `HTTPS_PROXY` nor the macOS system
  proxy (which a managed or corporate network can set). They connect straight to the configured server.
- **Token.** A bearer token in the macOS Keychain, service `os-intent-laya`, account = the URL's host. It is
  never stored in `settings.json`, never passed on a command line, never logged. Without a token nothing is
  sent (`no_token`).
- **Redirects.** A redirect to another origin (scheme, host or port) is never followed: the call fails
  (`redirect_refused`), so neither the token nor the moment reaches a third host.
- **Timeout.** `live.laya_remote_timeout_ms`, default **5000 ms** (range 100-10000), replaces
  `live.laya_timeout_ms` (3000 ms, local) in remote mode. It adds a network round trip to the server's own time
  (about 2.5 s warm on a 2 vCPU CPU server, above); the call is off the HUD path, so a longer bound costs
  nothing on screen and a late answer still merges into the next ranking.
- **Typed wishes.** You wait for a wish, so in remote mode its Laya call is capped at **1500 ms** and its health
  probe at 0.5 s (0.15 s locally); past that, the next tier (Claude, if enabled) answers. A CPU-only server is
  too slow for this path.
- **Token cache.** The token is read from the Keychain once and kept in memory per process for 5 minutes (an
  absent token for 30 s), so neither moments nor wishes start `security` each time. A replaced token is picked
  up within 5 minutes; restart `oi live` to use it at once
  (`launchctl kickstart -k gui/$(id -u)/com.osintent.oi-live`).

## Commands

```bash
oi laya-remote enable --url http://100.64.0.10:8781   # prints what is sent, asks y/N, asks the token (not echoed)
oi laya-remote status                                 # {"mode", "url", "url_ok", "token_in_keychain", "reachable", "probe_ms"}
oi laya-remote disable [--forget-token]               # back to local (and delete the Keychain item)
oi health                                             # remote: reachability + health round trip, timeouts of the last shows
```

`enable` options: `--yes` (skip the question; the notice is still printed), `--token-stdin` (read the token from
standard input, for scripts), `--new-token` (replace the token already stored). On success it sets
`live.laya_mode = "remote"` and `live.laya_url`, then stops the local Laya server if one runs. The token is stored
through `security -i` on standard input, so it never shows in a process list. By hand:

```bash
security add-generic-password -U -s os-intent-laya -a <host> -w   # prompts for the token
```

To go back: `oi laya-remote disable`, or set `live.laya_mode` to `local` in `settings.json`.

## The server side

`laya/remote/` holds a generic example for a Linux machine with systemd. Every value in `__CAPITALS__` is a
placeholder.

- `oi-laya-remote.service.example`: `laya-serve` bound to `127.0.0.1:8782` only, `DynamicUser`,
  `MemoryMax=3.5G`, `CPUQuota=200%`, `LAYA_LOG_LEVEL=warning` (no per-request log lines), Hugging Face hub
  offline (put the weights in `HF_HOME` first; the unit never downloads), `IPAddressAllow=localhost` only. It
  says `LAYA_DEVICE=cpu`: set your GPU device there if your laya build supports one (CPU is slow, above).
- `oi-laya-auth-proxy.service.example` + `oi_laya_auth_proxy.py`: laya-serve has no authentication, so a small
  stdlib proxy listens on your **private interface address**, port 8781, checks `Authorization: Bearer <token>`
  (constant time), forwards only `GET /health` and `POST /v1/systemone` (64 KB body cap) to `127.0.0.1:8782`,
  logs no request and never passes a redirect on. A connection idle for 10 s is dropped and at most 32 run at
  once. The unit is hardened (`ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, an empty
  `CapabilityBoundingSet`, `SystemCallFilter=@system-service`, `MemoryDenyWriteExecute`,
  `RestrictAddressFamilies`, `IPAddressAllow=localhost __CLIENT_RANGE__`).
- `token.env.example`: `/etc/oi-laya/token.env`, `root:root`, mode `0600`, one line `OI_LAYA_TOKEN=…`. It ships
  **empty**: the proxy refuses to start on an empty token, one under 32 characters or a placeholder. Generate
  it with `openssl rand -base64 32`. systemd reads it as root before dropping to the dynamic user.

```bash
sudo install -d -m 0755 /opt/oi-laya && sudo install -m 0644 laya/remote/oi_laya_auth_proxy.py /opt/oi-laya/
sudo install -d -m 0700 /etc/oi-laya && sudo install -m 0600 laya/remote/token.env.example /etc/oi-laya/token.env
# edit the token, then the two units (placeholders), copy them to /etc/systemd/system/ without `.example`
sudo systemctl daemon-reload && sudo systemctl enable --now oi-laya-remote oi-laya-auth-proxy
```

Bind the proxy to a private address only (LAN, VPN or tailnet) and keep port 8781 closed on any public
interface. For a server reached over the internet, put it behind https (a reverse proxy with a certificate) and
use an `https://` URL on the Mac.

On another Mac (no systemd), run `laya-serve` with `LAYA_HOST=127.0.0.1 LAYA_PORT=8782` and the proxy with the
same environment variables, each under its own LaunchAgent; the proxy is plain Python 3 with no Linux
dependency.
