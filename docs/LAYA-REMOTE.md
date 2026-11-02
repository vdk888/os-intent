# Remote Laya (optional)

Laya, the reranker, normally runs on the Mac: one local server under the memory rule, about 3.2 GB at peak.
Remote mode runs it on another machine you control (your own server on a private network) instead, so the Mac
keeps that memory. It is off by default and nothing changes unless you turn it on.

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

- the front app name, the web site host and the window / page title (60 characters at most);
- your last 5 actions (app switches and pages, with how long ago and for how long), the day, time and desktop
  number, and today's top 3 apps with durations (the serialised moment, about 256 tokens at most);
- the menu of candidate next actions (short labels; a label can name an app, a site, a file or a contact);
- for a typed wish: the wish text (200 characters at most) and the front app.

It never sends screenshots, file contents, or mail or message bodies. The server answers with probabilities
only. Whoever runs that server sees this data: use a machine you control, on a private network or over https.
`oi laya-remote enable` prints this list and asks before anything is switched on.

## Safety rules on the Mac

- **URL.** `https://` to any host, or plain `http://` only to a private address: `127.0.0.0/8`, `10.0.0.0/8`,
  `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10` (CGNAT, used by tailnets), `::1`, `fd00::/8`, `localhost`
  or a `*.ts.net` name. Plain http to anything else is refused and nothing is sent. Host names are never
  resolved to decide: a LAN name such as `laya.lan` needs https, or use its address.
- **Token.** A bearer token in the macOS Keychain, service `os-intent-laya`, account = the URL's host. It is
  never stored in `settings.json`, never passed on a command line, never logged. Without a token nothing is
  sent (`no_token`).
- **Redirects.** A redirect to another origin (scheme, host or port) is never followed: the call fails
  (`redirect_refused`), so neither the token nor the moment reaches a third host.
- **Timeout.** `live.laya_remote_timeout_ms`, default **5000 ms** (range 100-10000), replaces
  `live.laya_timeout_ms` (3000 ms, local) in remote mode. A remote server usually runs on CPU (no Apple GPU) and
  adds a network round trip, so its tail sits above the local one; the call is off the HUD path, so a longer
  bound costs nothing on screen. The typed-wish path keeps its 0.15 s health check: fine on a LAN or tailnet.
- `oi live` builds its client once per setting change: after replacing the token in the Keychain, restart it
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
  `MemoryMax=3.5G`, `CPUQuota=200%`, CPU device, `LAYA_LOG_LEVEL=warning` (no per-request log lines),
  Hugging Face hub offline (put the weights in `HF_HOME` first; the unit never downloads).
- `oi-laya-auth-proxy.service.example` + `oi_laya_auth_proxy.py`: laya-serve has no authentication, so a small
  stdlib proxy listens on your **private interface address**, port 8781, checks `Authorization: Bearer <token>`
  (constant time), forwards only `GET /health` and `POST /v1/systemone` (64 KB body cap) to `127.0.0.1:8782`,
  logs no request and never passes a redirect on.
- `token.env.example`: `/etc/oi-laya/token.env`, `root:root`, mode `0600`, one line `OI_LAYA_TOKEN=…`
  (`openssl rand -base64 32`). systemd reads it as root before dropping to the dynamic user.

```bash
sudo install -d -m 0755 /opt/oi-laya && sudo install -m 0644 laya/remote/oi_laya_auth_proxy.py /opt/oi-laya/
sudo install -d -m 0700 /etc/oi-laya && sudo install -m 0600 laya/remote/token.env.example /etc/oi-laya/token.env
# edit the token, then the two units (placeholders), copy them to /etc/systemd/system/ without `.example`
sudo systemctl daemon-reload && sudo systemctl enable --now oi-laya-remote oi-laya-auth-proxy
```

Bind the proxy to a private address only (LAN, VPN or tailnet) and keep port 8781 closed on any public
interface. For a server reached over the internet, put it behind https (a reverse proxy with a certificate) and
use an `https://` URL on the Mac.
