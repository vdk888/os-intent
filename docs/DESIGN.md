# OS-intent — design notes and contracts

The technical contract of every component (token vocabulary, settings schema, socket protocol, live driver,
Laya layer, Claude actions, glide, page actions, screenshots, wish resolution, orchestrator). W-numbers are the
build milestones. Start with README.md for the overview.


## Layout
```
os-intent/
  docs/DESIGN.md                 this file
  pyproject.toml          package `oi`, python 3.12, venv at .venv (gitignored)
  oi/aw.py                W1  pull ActivityWatch buckets incrementally → raw/YYYY-MM-DD.jsonl
  oi/normalise.py         W1  raw events → action tokens → tokens/YYYY-MM-DD.jsonl
  oi/cli.py               W1  `oi pull`, `oi tokens --day`, later `oi markov`, `oi render`
  oi/markov.py            W3  catalogue mining + order-2 Markov (+hour bucket, +space) top-k
  oi/serialise.py         W3  state template → ≤256-token text, truncation order per design §1
  oi/candidates.py        W3  Markov top-k ∪ form fixed options ∪ nothing, filtered, ≤16
  oi/forms/*.yaml         W3  form A (generic), form C (Safari) in the design §2 schema
  watchers/oi-watcher/    W2  Swift CLI: Space changes, ⌘ chords, Safari front URL → aw buckets
  hud/                    W4  SwiftUI radial HUD prototype, static options, no prediction
                          W6  wish quick action (always-on hotkey, typed/dictated) + LaunchAgent com.osintent.oi-hud
  menubar/                W5  oi-menubar: status item (status, pause, permissions, denylist, hotkeys)
  settings/               W5  OISettings: shared settings.json reader/writer (Swift package)
  oi/settings.py          W5  python reader of settings.json (denylist for the normaliser)
  oi/shots.py, oi/contact.py  W17 screenshot -> offer -> send to the current contact (tier 1 / tier 2 note)
  ocr/                    W17 oi-ocr: on-device Vision OCR CLI (Swift package)
  tests/                  pytest (python) / `swift test` (swift) with fixtures, no real data
```
Data (never in git): `~/.local/share/os-intent/{raw,tokens,moments,models,logs,wishes}/`.

## Token vocabulary (the one contract W1, W2, W3 share)
One JSONL row per token: `{"ts": ISO-8601 UTC, "dur": seconds, "token": str, "app": str|null, "src": "aw-window|aw-afk|oi-space|oi-key|oi-safari", "raw_id": int|null}`.
Tokens:
- `activate:<App>` app came to front (window title changes within the same app are NOT new tokens unless the title maps to a url/title token below).
- `url:<host>/<slug>` Safari front tab (host lower-case, slug = first path segment or page-title slug ≤ 30 chars); emitted on tab/URL change.
- `title:<App>/<slug30>` for apps where the title is the context (Notion, Mail subject, Terminal session), slug = normalised title ≤ 30 chars; emitted on title change.
- `space:+1` / `space:-1` / `space:=k` desk change (k = index if known).
- `key:<chord>` ⌘-chords only, e.g. `key:⌘C`, `key:⌘⇧4`; plain typing is never logged.
- `afk:start` / `afk:end`.
- later: `clip:changed`, `hud:shown`, `hud:pressed:<k>`, `hud:dismissed`.
- `vocal:<slug>` (W12) a voice note sent to that agent from the HUD (src `oi-hud`, from the hud-log row
  `{"kind":"vocal","ok":true}`; failures and dry runs give nothing). Feedback: never a moment, label or primitive.
- `shot:new` (W17) a new screenshot (bucket `oi-watcher-shot_<host>`, src `oi-shot`, dur 0): an ordinary
  action token; the image path stays in the raw row, never in a token.
Rules: merge flicker (an `activate` reverted in < 1 s is dropped); denylisted apps emit `activate:<App>` only, never title/url; timestamps UTC; the vocabulary file is `oi/vocab.py` (regexes + validator) owned by W1, imported by W3.

A **moment** = every token boundary while not afk; its **label** = the next token within 120 s (`nothing` otherwise). Train days 1-5, test days 6-7, test touched once.

**Wish tokens (W6).** `wish:<slug30>` (src `oi-wish`, dur 0, at the wish's ts, app = the front app when it
was made) comes from `oi-hud`'s wish panel: `wishes/YYYY-MM-DD.jsonl` (UTC day, 0600) rows
`{"ts","text","source":"typed|dictated","context":{"app","space","hud_visible","hud_options"}}`, read by
`oi/normalise.py` (the day file and its neighbours, filtered to the day). A wish is feedback, not an action:
`oi/moments.py` takes it out of the stream before building moments, so it is **never a moment, never a label and
never in a history** (the next real token stays the label); it is attached to the moment that precedes it as
`"wish": "<slug>"` (several wishes after one moment: the last wins; a wish before the first moment is dropped).
Markov never sees it. `oi stats` counts it (`kinds.wish`, `wishes`). Training use (Batch 2/3): a moment with a
`wish` is a labelled miss — what the HUD should have offered there.



## Pass bar for v0 (unchanged from design §5)
tuned Laya beats Markov@3 by ≥ 3 points on test days; shown-precision ≥ 40 % at coverage ≥ 30 %; state fits in L256. If only Markov passes: HUD on Markov, Laya in shadow. If neither: explicit primitives as a hotkey palette, no prediction.

## Settings (W5)
One file, `~/.local/share/os-intent/settings.json` (or `$OI_DATA_DIR/settings.json`), mode 0600, written atomically
(temp file + rename) by `oi-menubar`; every key optional, a missing/corrupt file or a wrongly typed key gives the default.
```json
{ "paused": false,
  "hud_enabled": true,
  "denylist_apps": ["Passwords", "Keychain Access", "1Password"],
  "repo_dir": "~/os-intent",
  "hotkeys": { "v": 3, "option_1": {"tap": "ctrl"}, "option_2": {"tap": "option"},
               "option_3": {"tap": "cmd"}, "option_4": {"tap": "fn"},
               "dismiss":  {"modifiers": [], "key": "Esc"},       "wish":     {"modifiers": ["option"], "key": "`"},
               "wish_visible": {"tap": "shift"},                  "summon":   {"tap": ["option", "cmd"]},
               "vocal": {"tap": ["ctrl", "option"]} },
  "hud": { "slots": {"fn": "left", "ctrl": "top", "option": "right", "cmd": "bottom"},
           "colors": {"ctrl": {"light": "#4F86E8", "dark": "#9CC3FF"}, "option": {"light": "#D9A400", "dark": "#FFE08A"},
                      "cmd": {"light": "#3FA95A", "dark": "#A6E3A1"}, "fn": {"light": "#E06A9C", "dark": "#F5A9C8"}} },
  "wish": { "locale": "fr-FR" },
  "gate": { "show_min_p": 0.15, "none_max_p": 0.6, "max_show": 3, "cooldown_s": 20, "min_history": 1,
            "ttl_ms": 15000, "hide_on_pointer_travel": true, "pointer_grace_ms": 2000,
            "pointer_fast_px_s": 1200, "pointer_fast_ms": 250 },
  "live": { "poll_s": 2, "rebuild_s": 600, "retrain_s": 600 },
  "vocal_recipients": { "nova": {"kind": "a2a", "script": "/Users/you/.local/bin/agent-send",
                                 "to": "agent-host", "state_dir": "/opt/agents/nova/inbox"},
                        "atlas": {"kind": "inject", "path": os.path.expanduser("~/.claude/channels/atlas/inject")} },
  "updated_at": "2026-10-20T11:00:00Z" }
```
- `paused` — oi-watcher re-reads every 5 s (mtime) and posts nothing while true.
- `hud_enabled` — `oi-hud serve` answers every show with `{"event":"dropped","reason":"disabled"}` while false.
- `denylist_apps` — `oi/normalise.py` merges it with its built-in list: a denylisted app yields `activate:<App>`
  only (no title, url or ⌘-chord tokens); "Safari" in the list drops url tokens. Applied at the next `oi tokens`.
- `repo_dir` — where oi-menubar finds `projects/os-intent/.venv/bin/oi`.
- `hotkeys` — HUD keys. W9: an option (1-4 only) can be a bare modifier TAP `{"tap": "ctrl|option|cmd|fn"}`
  (the default: ⌃ ⌥ ⌘ fn = options 1-4, one key; detected by a listen-only event tap that exists only while the HUD
  is visible, needs Input Monitoring for oi-hud.app, else ⌥n fallback; see hud/README.md). `"v": 2` marks the W9
  schema: a file without it whose options are exactly ⌥1-⌥4 (the W5 defaults oi-menubar wrote out) reads as the
  tap defaults. Chords: `modifiers` ⊂ {control, option, shift, command}; `key` is a name ("1", "a", "`",
  "Esc", "F5", "Space", …) mapped to a macOS virtual key code, i.e. a PHYSICAL key position (US layout names;
  ⌥1 stays ⌥ + the key left of 2 on AZERTY); `"#<n>"` = raw virtual key code. `wish` opens the "tell me what
  you wanted" panel (W6): registered permanently by `oi-hud serve`, even with `hud_enabled` false; the
  validator forbids collisions with it. `summon` (W10; W11: default the ⌥+⌘ SET
  tap `{"tap": ["option", "cmd"]}` — both down, then released, no other key, ≤ 500 ms first down → last up; a set of
  2-3 modifiers is allowed for summon only; the single form `{"tap": "shift|ctrl|option|cmd|fn"}` and a chord are
  still accepted; `"v": 3` — a file below v3 whose summon is the W10 ⇧ default reads as ⌥+⌘): opens the HUD at any
  time with the current best options. oi-hud keeps a PERMANENT listen-only flagsChanged/keyDown/click/scroll tap
  for it (same TapDetector as the option taps, which returns the SET of modifiers of one tap: ⌥ alone, or ⌥ then
  ⌘ sequentially, never summon; ⌥⌘C / ⌥⌘+scroll are chords; the option taps take one-modifier sets only, so ⌥+⌘
  never fires option 2 or 3; retried every 60 s without Input Monitoring; a chord form is a permanent Carbon hotkey). Ignored when
  `hud_enabled` is false, `paused` is true, the HUD is already up or the wish panel is open. Otherwise oi-hud
  broadcasts `{"event":"summon","ts":…}`; `oi live` (persistent socket reader) answers with a show built from the
  current moment, Markov top-k with no threshold and no cooldown (max_show kept; an empty show = the wish option
  alone), hud-log row `"summoned": true`; with no `show` within 400 ms oi-hud draws the wish option alone, and a
  live show arriving after that replaces it in place without the rate limit (W11).
  ⇧ is a summon-only tap (an option slot with a ⇧ tap falls back to its default). `oi-hud debug-summon` runs
  the same path without a key.
- `hud.slots` (W10) — FIXED
  places around the pointer keyed to the modifier that picks the option, clockwise from the left in keyboard
  order: fn left (9 o'clock), ⌃ top (12), ⌥ right (3), ⌘ bottom (6). An absent option leaves its place empty
  (no re-flow, no capsule travelling between places); a chord option takes its default modifier's place. The
  wish option has a fixed fifth place below-right (4:30); 7:30 below-left is RESERVED (kept free) for the W12
  "Send a vocal to…" option. The whole cross shifts as a block at screen edges. An invalid map (not the four
  modifiers on four different places) = the defaults.
- `hotkeys.wish_visible` (W11) — a
  single-modifier tap (default ⇧) that opens the wish panel while the HUD is visible; the wish capsule's badge
  shows it. Armed in the same per-show listen-only tap as the option taps (only while visible; ⇧A, ⇧-click,
  ⇧-scroll are chords, never taps); without Input Monitoring the badge shows the ⌥` chord. `hotkeys.wish` (⌥`)
  stays the always-on chord, also while the HUD is hidden. A chord is not accepted here (read as the default).
  It may share its tap with `summon` (summon is ignored while the HUD is up), not with an option tap.
- `hud.colors` (W11) — option modifier → colour, `{"light":
  "#RRGGBB", "dark": "#RRGGBB"}` or a bare "#RRGGBB" for both; defaults pastel ⌃ blue, ⌥ yellow, ⌘ green, fn pink
  (the user may set option 1 red). An option takes the colour of the modifier owning its fixed place, so colour =
  key = place, stable across shows. Tolerant per entry (bad/missing → that key's default). The same colour
  draws the option's target hint while the HUD is visible (`oi live` sends `hint` per option: app.open → the
  app's window, url.open → Safari's window, space.move → the screen edge right/left; fallbacks Dock icon (with
  Accessibility) then a halo on the capsule icon; see hud/README.md "Colours and target hints").
- `wish.locale` — on-device dictation language for the wish panel (default `fr-FR`, fallback `en-US`); also used
  by the W12 vocal recording.
- `hotkeys.vocal` (W12: "send a vocal to, and when i press it i have to choose one
  of you guys") — the permanent "Send a vocal to…" option at 7:30 (mic icon + key, neutral colour), armed while
  the HUD is visible. Default the ⌃+⌥ SET tap (every single modifier is taken by options 1-4 and ⇧; the card's
  caps-lock tap is not used, see hud/README.md "Send a vocal to…"); a SET of 2-3 modifiers or a chord, never a
  single tap (read as the default). Press → the HUD turns into the agent picker at the same four places (fn ⌃ ⌥ ⌘ =
  Atlas Nova Orion Lyra; the vocal key again = Vega Juno Iris Hera, then Echo, then back) → the
  agent's key (or a click) → the recording panel (on-device dictation, `vocal.max_s` max: default 5 min, countdown in the last 15 s, the vocal key or Enter sends, Esc
  cancels; an empty transcript sends nothing) → `oi vocal-send` delivers it. A file without it gets the default.
- `vocal_recipients` (W12) — slug → inbox, overriding the built-in table of `oi/vocal.py` per slug (`null` removes
  one; an invalid entry is ignored). `{"kind": "inject", "path": "<abs local inject file>"}` (atlas, juno) or
  `{"kind": "a2a", "script": "<abs path of your relay command>", "to": "<host alias>", "state_dir": "<the agent's
  inbox dir there>"}` (the example table: nova/orion/lyra/iris on agent-host, vega on studio-host, hera/echo on
  laptop-host), run as `<relay> --to <host> --state-dir <dir> --from atlas`, the line on stdin; the relay command
  (default $OI_RELAY_CMD or ~/.local/bin/agent-send) is yours: ssh, a signed relay, a queue. Optional `name`. oi-menubar keeps the key verbatim.
- `gate` (W8) — when `oi live` shows the HUD on a context change. Shown only if not `paused`, `hud_enabled`,
  the current context (last token) was seen ≥ `min_history` times in training, P(nothing) ≤ `none_max_p`, at least
  one option (app.open / url.open / space.move only) has p ≥ `show_min_p` (at most `max_show`, ≤ 4, best first),
  and ≥ `cooldown_s` seconds since the last show. Read every loop (no restart needed).
- `live` (W8) — `poll_s` loop period; `rebuild_s` full rebuild of today's tokens from raw/ (the tail is
  re-tokenised every loop); `retrain_s` Markov refit from all token days (on a thread).
  `gate` and `live` are flat numbers (and booleans); oi-menubar keeps them as they are when it writes the file.
- `gate` HUD keys (W9): `ttl_ms` how long each
  show stays (default 15000, clamped 1500-60000; `oi live` sends it with every show, `TTL_MS` is only the
  fallback); oi-hud reads `hide_on_pointer_travel` (default true), `pointer_grace_ms` (2000: no pointer hide
  before), `pointer_fast_px_s` (1200) and `pointer_fast_ms` (250: sustained travel only) on each show.

## Live driver (W8)
`oi live` (LaunchAgent `com.osintent.oi-live`, `live/install.sh`; log `logs/oi-live.log`) pulls aw every `poll_s`,
detects a context change (new `activate:` / `url:` / `space:` token; afk ignored; nothing while the front app is
denylisted), builds the moment, routes form A/C, takes the Markov candidates, applies `gate`, sends `show` to
`hud.sock` and reads the reply on a thread. `pressed k` executes: app.open → `open -a <App>`; url.open →
`open -a Safari <last full http(s) URL seen for that host/slug, else https://<host>/>`; space.move → ctrl+→/←
via System Events, only if it accepts keystrokes (Accessibility), else skipped and logged; `nothing` → no-op.
Nothing else is executed and nothing leaves the Mac.
- `hud-log/YYYY-MM-DD.jsonl` (UTC day, 0600): one row per evaluated context change
  `{ts, form, state_digest (sha1 of the rendered state), options [{key, token, p}], shown, outcome
  "pressed:k|dismissed:<reason>|dropped:<reason>", executed token|null, latency_ms}`. Gate rejections are logged
  as `dropped:<gate reason>` (paused, hud_disabled, thin_history, nothing_likely, no_option, cooldown), `--dry-run`
  as `dropped:dry_run`.
- `oi tokens` turns shown rows into `hud:shown` (at ts) + `hud:pressed:<k>` / `hud:dismissed` (at ts + latency),
  src `oi-hud`, dur 0. Like `wish:`, they are never moments, labels, history or catalogue entries; a press sets
  `hud_pressed = k` on the preceding moment. `oi stats` counts `hud_shown` / `hud_pressed`.
- `status/oi-live.json` (every loop): `{ts, state running|paused|hud_disabled, pid, dry_run, shows_today,
  pressed_today, last_show_ts, model_trained_at, moments_today}`; oi-menubar shows "Live predictor: …" (stale
  > 15 s = stopped) and a "Restart live predictor" item.
- `oi live --dry-run` prints the payloads instead of sending; `--once` evaluates the current moment once;
  `--socket PATH` targets another HUD socket.
- Do NOT run `oi pull` by hand (or "Rebuild today's tokens" in oi-menubar) while the live agent runs: both
  share `cursors/` and the raw files with no lock. Stop it first (`launchctl bootout gui/$(id -u)/com.osintent.oi-live`)
  or use a separate `OI_DATA_DIR`.
- Logs: "aw-server unreachable" at most once per 60 s; url.open opens scheme://host/path only (query string and
  fragment dropped); a refused Accessibility probe is retried every 10 min (granting it needs no restart).
## Laya layer (W13): "build the laya layer with fine tuning". Shadow first; nothing from Laya
is shown before promotion.
- `oi/laya.py` — client to the local Laya server (`POST /v1/systemone`, one `choice` question `next`: the routed
  form's instructions, the candidate menu's criteria (≤ 8 words, ≤ 16 options, `nothing` always), state = the
  rendered moment). Timeout `live.laya_timeout_ms` (1050 ms, measured with `oi laya-bench --gap 5`; was 300 ms,
  below the first call after an idle gap, so every live call timed out); any error is a `LayaError(reason)` (timeout, unreachable, http_N, bad_reply,
  keys_mismatch), never a delay. Every request names ONE checkpoint (`model: english`), so laya's Router never
  loads `laya-multilingual` on the side. Per-form temperatures of a promoted checkpoint are applied client-side.
- `oi laya-serve` — supervisor of ONE server (stock English laya via `laya-serve`, or an oi checkpoint via laya-ft's
  `serve_wrapper` on 8784), 127.0.0.1 only, HF hub OFFLINE (no cloud call). Refuses unless swap < 4 GB, ≥ 3 GB
  available and no other heavy ML process (laya server, laya-ft run); polls the child's physical footprint
  (`footprint`, counts MPS memory) every 2 s and kills it above 4 GB. `status/oi-laya.json`. LaunchAgent
  `com.osintent.oi-laya` (`laya/install.sh`, run by you): LAZY (not started at load, no
  KeepAlive); `ensure_server()` kickstarts it (or spawns the supervisor) when shadow mode finds the server down,
  at most every 10 min. `oi laya-serve --check` prints the preflight. Once /health answers, the supervisor
  sends ONE warm-up prediction (status `warm_ms`), so the cold first forward pass is never a user's call; a
  memory refusal names `heavy_processes` and the top memory users (`top_memory`: name, pid, MB).
- Shadow in `oi live` — settings `live.laya_shadow` (default false): every evaluated moment (gate path + summon)
  also gets a Laya prediction on one worker thread (one running + one queued, else `busy`); the hud-log row
  gains `laya: {probs per option key, latency_ms, checkpoint, markov_top}` or `{error, checkpoint}`, written
  when both the HUD outcome and the Laya call are done. The row also carries `markov_probs` and `tokens`
  (key -> token) so `oi laya-report` can score both on the same menu; one state is asked once (cache by digest).
  `live.laya_rank` = shadow | blend | live (default shadow; legacy `predictor` laya|blend maps to live|blend) is
  the user's switch, set by hand or by `oi laya-promote`, never automatically. Markov always answers first: blend/
  live rank a show on Laya's (or 50/50) probabilities only if Laya's answer for that state is already in
  (`laya_rank` in the row), else Markov (`laya_rank_fallback` pending | error). No show waits. Other W13 `live` keys: `laya_url`,
  `laya_checkpoint`, `laya_model`, `laya_timeout_ms`. OISettings (Swift) round-trips strings/null in `live`.
- `oi replay --days … [--test-last 2] | --train-days … --test-days … [--smoke] [--predictors markov,laya,tuned]` —
  offline, read-only on tokens/. Split by day (train strictly before test); calib slice = the last 20 % of train
  moments (per-form temperature by NLL, gate τ = lowest threshold reaching 40 % shown-precision). Same menu for
  every predictor; metrics acc@1/@3, ECE, shown-precision + coverage at τ, latency p50/p95, tokens per state
  (L128/L256/L512), label-in-menu ceiling, majority baseline. One Laya server at a time (started/stopped per
  predictor). Test touched once: reports/test-ledger.json per (predictor, form versions); a re-touch needs
  `--retouch` and is never promotable. `--smoke` = one day split by time, flagged, never in the ledger.
- `oi laya-train --train-days … --test-days …` — laya-ft head-only (frozen encoder + embedding cache, CPU fp32,
  `--max-process-mb 4096`, class weights = inverse label frequency capped at 10, early stopping patience 2).
  Rows = (rendered state, menu question, gold key); HUD presses x3; wishes matched to a menu option add a row.
  Train days -> 70 % train / 15 % val / 15 % calib; the test days are scored once at the end (replay
  machinery). Output `models/laya/oi-laya-<UTC>/` (final/ + oi-laya.json: days, temperatures, τ, report).
- `oi laya-promote --report R [--predictor laya|blend] [--dry-run]` — the design §5 bar in code: not smoke, test
  first touch, ≥ 300 test moments, beats Markov@3 by ≥ 3 pts, shown-precision ≥ 40 % at coverage ≥ 30 %,
  ECE ≤ 0.08, state max ≤ 256 tokens; only then `live.predictor`, `laya_checkpoint`, `laya_url` are written.
- `oi laya-bench [--day D] [-n 50] [--gap 5]` — latency of the RUNNING server on requests built by the live code
  from a token day (never starts a server): first call apart, then p50/p95/max, `suggested_timeout_ms` = p95 x 1.5
  + 50. 2026-10-20: back to back p50 147 / p95 162 ms; spaced 5 s (live-like) p50 466 / p95 655 / max 962 ms.
- `oi laya-report [--days …|--last 7]` — shadow evaluation from the hud-log: Laya vs Markov top-1/top-3 vs the next
  action (120 s) and vs HUD picks, top-1 agreement, error rate, and the proposed promotion bar (`laya_eval.PROMOTION`:
  ≥ 300 moments over ≥ 3 days, Laya@3 ≥ Markov@3 + 3 pts, @1 not worse, errors ≤ 10 %, ≥ 50 picks with @3 not
  worse). It only says whether the bar is met; the user flips `live.laya_rank`.
- `oi improve-report [--days …|--last 7]` — weekly input for an offline improvement pass (a stronger model reviewing forms and labels):
  per form HUD funnel, dismissals, wishes, misses (not in menu / outside Markov top 3), Laya shadow errors,
  latency and agreement. Local file (app names, hosts, wish slugs): aggregate numbers only outside this Mac.
- Nightly retrain: scheduled once ≥ 3 token days exist (card: 23/10), not before.

## Claude in the app (W14)
`oi/claude_actions.py` + `oi/prompts/*.md` + form B (`oi/forms/B.yaml`, Mail: reply_sender / forward_to (enrich,
pinned first in a summon) / archive (not executed yet) / nothing). Long-press (hud/README.md "Claude-assisted
options") -> `pressed` with `mode: claude` -> the Claude variant, which always answers the HUD with `result`:
- mail.reply / mail.forward: the selected Mail message (sender, subject, date, body ≤ 6000 chars, previous message of
  the thread ≤ 2000 when found) + top-3 local wiki notes (`tools/wiki/wiki_search.py --json`) -> sonnet -> a Mail
  reply / forward window pre-filled (AppleScript `reply … with opening window`, then `content`). NEVER sent.
  A tap = the same window, empty.
- calendar (mail.reply only): `oi/calendar_slots.py` extracts the times the mail (or the previous message)
  proposes (French / English: weekday, dd/mm, "26 octobre", "October 26", demain / tomorrow + 10h / 10:00 / 2pm /
  midi, ranges, "30 min"; ≤ 6 slots, next 60 days, one hour by default), reads the busy intervals of that window
  only (EventKit through JXA, `BUSY_JS`, read-only, never requests access, the same Calendars grant as the
  calendar sensor; only [start, end] leaves EventKit, never a title / attendee / note; cancelled, free, declined
  and all-day events are skipped, except all-day out of office) and passes "Fri 23/10 10:00-11:00: free" lines in
  the prompt's `<calendar>`: the draft accepts the first free slot, or says none fits. No proposed time = no
  calendar read; no access / timeout / error = `(none)`, the draft as before. Logged: slot count + result kind only.
- url.open / app.open: open, then a 3-line haiku summary card (Safari text only when "Allow JavaScript from Apple
  Events" is on, else title + URL; another app: its name + window title).
- enrichment: after a show with `enrich` options, a local label ("Reply to <first name>"), then one haiku call per
  mail (cached, 64 entries), via `update`. Never blocks the HUD; nothing runs without a show.
- runtime = your Claude subscription only: headless `claude -p`: env token / credentials file -> isolated CLAUDE_CONFIG_DIR; keychain Mac -> caller's config with
  `--setting-sources project`; always `--strict-mcp-config --tools "" --no-session-persistence`, prompt on stdin,
  API-key / Telegram / GitHub env removed, 25 s timeout, auth-stub guard. Failure = "Claude unavailable" + the plain
  primitive. `python -m oi.claude_actions check` = one harmless haiku call (auth path + latency).
- settings.json `claude`: `enabled` (true), `enrich` (true), `local_only_domains` ([]: those mail domains / page
  hosts never go to Claude), `debug` (false; or OI_CLAUDE_DEBUG=1: prompt + output in `claude-debug/`, 0600).
  hud-log rows of a long-press carry `"mode":"claude"` and `"claude":{action, model, latency_ms, ok}` only.
- Permissions the user is asked: Automation → Mail (for `oi live`'s python), optionally Safari → Develop → "Allow
  JavaScript from Apple Events"; System Events (window titles) reuses the Accessibility grant when present.

## Glide marking menu (W15)
`oi/glide.py` + `oi/prompts/glide.md` + the HUD glide (hud/README.md "Glide"). Hold ⌥⌘ ≥ 250 ms → the cross stays at
the press point; rest `hud.dwell_ms` (500) on a capsule → Claude follow-ups bloom around it (sub-ring, depth ≤ 3);
release over any capsule executes it, on empty space / Esc cancels; tap ⌥⌘ unchanged.
- Prefetch: at the hold start ONE batched haiku call returns the follow-ups of every visible option, cached per
  context key (app, page host, desk, selected mail id, the options; 64 entries); an `expand` is served from the
  cache, waits for the running prefetch, or makes one haiku call (8 s timeout) with the chain of labels.
- Context is read at request time (the W14 note): the Mail selection when the prefetch / expand happens; a mail
  follow-up opens only on the mail it was built for (message id checked by AppleScript), a draft refuses another
  selection.
- Mapping: app.open (installed, switched to today), url.open (sites seen today only), space.move, mail.reply /
  mail.forward, draft:mail.* and summarise (W14 Claude variants), vocal:<agent>:<text> (W12 path); anything else
  "Ask Atlas: <text>" (local inject to atlas). Messages say "(via the HUD glide)".
- Privacy: `claude.local_only_domains` now applies to a mail's sender, cc and reply-to (W14's long-press and label
  enrichment too) and to the page host; a gated mail is "details withheld" in the prompt.
- Logging: hud-log `glide` {visited [{path "2.1", ms}], depth, expanded, final, action id, expansions [{path, ms,
  cache, ok, n}]}; oi.normalise adds `hud:glide:<depth>` (0-3). No label, message or prompt is logged.
- `oi live --dry-exec` (functional tests): shows go out, presses print what would run, nothing executes or is sent.
- Context cards: the same hold shows WHAT a direction is about. At the prefetch the
  driver sends `{"cmd":"context","cards":[{key, what, summary?, excerpt?, why?, pending?}]}`: `what` is local and
  instant (mail: sender · subject; Telegram agent: its last line, else its chat; file: name · folder; page: title ·
  host), drawn small next to the direction once the dwell on it completes (level 2). The mail's summary (3-4 lines:
  what is asked, deadline) + a verbatim excerpt come from the SAME batched Sonnet call (`<material>` in
  prompts/glide.md; the excerpt is dropped unless it is really in the mail), cached per context, re-sent when ready;
  `why` = the proactive judge's wiki page + snippet. One level deeper the card grows (level 3). A proactive mail's
  body is read read-only by message id (`claude_actions.read_mail_body`, once per show). Privacy: a local-only /
  private mail never reaches the model (no summary), a private event has no `why`; the cards travel on hud.sock
  only and the hud-log keeps their count. Release on an action acts; empty / Esc cancels and nothing changes.
## Page actions (W16)
- READ (oi-hud, needs **Accessibility for oi-hud.app**; without it nothing is offered and oi-menubar shows "Page
  actions: Accessibility for oi-hud — not granted · open pane…", from `status/oi-hud.json` {ts, ax, pid} every 30 s):
  `{"cmd":"page"}` on hud.sock → `{"event":"page","trusted","host","elements":[{role button|link, label ≤ 80,
  slug, frame [x,y,w,h] CG}]}` — Safari frontmost only, the front window's first AXWebArea, buttons/links only (label
  = AXTitle / AXDescription / the element's own text), never text fields, field values or page text; bounded (5000
  nodes, 0.45 s, 300 elements). The summon key prefetches it. `oi-hud page` / `page-local` print it (debug).
- CANDIDATES (Safari, form C): rule table `RULES` per host (github.com: Confirm squash and merge > Confirm merge >
  Squash and merge > Merge pull request > Approve > Delete branch > Close pull request; mail.google.com / notion /
  linear: empty, later), at most 2, pinned first in rule order (`rank`: the positive primary action first, whatever
  a probability says), on a SUMMON; DESTRUCTIVE labels (`page_actions.DESTRUCTIVE`: close, delete, discard, remove,
  cancel, reject, revert, abandon, archive, unsubscribe, leave, lock, … + FR fermer, supprimer, annuler, rejeter,
  retirer, archiver, …) are NEVER a HUD option, rule or learned, summoned or automatic (an incident:
  "Close PR" offered one-tap next to "Squash & merge", pressed, PR closed); only a spoken / typed wish reaches them,
  always through the gate's panel. LEARNING: a destructive click, a HUD press that executed one, and a page press
  undone within 5 min (⌘Z, or Reopen / Restore / Undo / Rouvrir / Restaurer) never enter the token stream
  (oi/normalise.py, applied at every rebuild, the raw logs untouched); plus LEARNED clicks on every path: oi-watcher logs a
  left-mouse-up on a page button/link (`oi-watcher-click_<host>`: {host, role, label slug}; Safari front, not
  denylisted, paused = nothing, needs Accessibility for oi-watcher) → token `click:<host>/<role>:<label-slug>` (src
  `oi-click`, an ACTION: moment, label, Markov primitive) → form C `press_button` (`safari.next_page@v2`). Every
  `ui.press` option is dropped unless the snapshot shows that element on the current page now (same host).
- HUD option: compact label ("Squash & merge"), SF icon, `target` "button:<slug>", hint `{"kind":"element","rect":[…]}` = an outline around the button (W11 overlay;
  off-screen → halo).
- CONFIRM (since the outward gate): ONE tap picks; a press not known read-only then waits for the
  gate panel (pick → gate panel → validate). The W16 two-tap is gone. Irreversible words (merge, delete, send, pay,
  publish, close, remove, discard, submit, archive, transfer, purchase, buy) press only with the confirmed ticket the
  panel's validate issues, so the panel asks for them even with `gate.outward` off.
- PRESS (`ui.press`): `oi live` sends `{"cmd":"ax_press","role","slug","host"}`; oi-hud requires a single-use ticket
  issued by the user's own tap on that option ≤ 5 s ago (irreversible: the gate's validate) else `no_tap` /
  `not_confirmed`, re-reads the page, checks the host, re-finds by role + slug (visible one first, never a stored
  coordinate) and AXPresses it; gone / other page → nothing pressed + toast "Page changed — nothing pressed".
  hud-log row: `executed` = the click token, `ui_press` {ok, reason}. `oi live --dry-run` never presses;
  `oi-hud ax-press --dry-run` finds only.
- WISH (W18 resolver): in Safari the wish catalogue also holds the page's buttons (`page:<role>:<slug>`, kind
  `page`, primitive `ui.press`; rule buttons first, FR/EN phrasings: "merge", "fusionne la PR", "approuve", "supprime
  la branche", …). No W18 ⇧ confirm: Enter runs it and a wish press ALWAYS asks the gate panel (no tap = its
  validate is the only source of oi-hud's press ticket).
## Screenshot → current contact (W17)
- oi-watcher `ScreenshotSource`: a DispatchSource on the screenshot folder (`defaults read com.apple.screencapture
  location`, default ~/Desktop; names "Screenshot …" / "Capture d’écran …" / the custom `name`; png/jpg/heic/tiff)
  + NSPasteboard.changeCount every 1 s (public.png / public.tiff only, concealed/transient markers skipped; the
  image is saved to `<data>/shots/clip-<stamp>.png`, 0600, 30 kept) → bucket `oi-watcher-shot_<host>`
  `{"kind":"shot","src":"file|clipboard","path"}` → token `shot:new`. **Amber:** with the floating thumbnail on
  (macOS default) the file lands only when the thumbnail slides away (~5 s); clipboard shots are instant.
- **Live state.** oi-watcher also polls
  the on-screen window list (CGWindowListCopyWindowInfo: owner, bounds, alpha; no title, no pixels; 0.3 s cadence)
  and posts `{"kind":"shot_state","state":"live|gone","src":"thumbnail"}` when the floating thumbnail (bottom-right,
  screencaptureui) or the screenshot / markup window appears / goes (`ShotWindows`: not the ⌘⇧4 overlay, not the ⌘⇧5
  toolbar), and `{"…","state":"gone","src":"clipboard"}` when a clipboard screenshot stops being the clipboard's
  content; a new file is posted after a fresh window poll. `oi live` → `oi/shots.py` `Shots` (own 0.5 s poll of that
  bucket) keeps the live set: a screenshot is live while its window is up, a clipboard one while on the clipboard, a
  window-less one (thumbnail off) until the context changes. While live: the moment's latest action is its
  `shot:new` row (in memory at once; tokens/<day>.jsonl is the periodic snapshot), its context has `live:["shot"]`
  (Laya's state text: "live: shot (on screen now)"), and its options join the predictor's ONE pool (hint, ⌥⌘ summon,
  glide context, long-press), ranked first by the same gate; the appearing shows them at once as the full cross
  bound to the state (`"live":"shot"`: oi-hud does not dismiss it on typing or pointer travel; `hide` when the state
  ends). Claude's content actions (first part: OCR + W25 pack → sonnet) join the pool when they come (safety
  bound 90 s on the call, no display cap); the cross still up is redrawn in place. No relevance timer anywhere
  (`shots.ttl_ms` is the cross's display time). Sends from these options are marked `outward` for the validation
  gate. macOS keeps the thumbnail's image in a TCC-protected temp folder: the image used is the file /
  clipboard one.
- Not in the context while paused / `shots.enabled` false / a denylisted app in front. Options: "→ <Agent>" (Telegram icon when the contact came from Telegram), "→ other"
  (agent picker, 3 per page + More), "Annotate" (note panel). Tap = tier 1 (image + context line: app, host, desk,
  Paris time); long-press "→ <Agent>" = tier 2: `oi-ocr` (Vision, on-device, ~0.1-0.2 s warm; the first call of a
  binary loads the model, ~30 s, paid at `oi live` start by `warm_ocr`) + context + last actions → haiku on the
  subscription → a 3-5 line note, editable in the note panel, Enter sends. Esc anywhere = nothing sent.
- Current contact (`oi/contact.py`): newest of, within 15 min: a `title:Telegram…/<slug>` matching
  `contact_map` (defaults Atlas - R&D → atlas, Nova - CEO → nova, Orion - Fund → orion, Lyra - Sales → lyra, Vega -
  Marketing → vega, Juno - Ext R&D → juno; the title is never logged), a `vocal:<slug>`, a shot sent;
  else `shots.fallback_contact` (default "atlas"). **Measured:** Telegram's macOS window title is
  "Telegram" (not the chat) in ~95% of today's title tokens, so the title map rarely fires: the vocal / shot-sent /
  fallback paths carry it (follow-up: read the chat header through Accessibility).
- Delivery (`shots.deliver`): atlas / juno → image copied to `<channel>/inbox/oi-shot-<stamp>-<hex>.png` (0600) +
  ONE line appended to `<channel>/inject` ("[Screenshot from the user, HH:MM, context: …] <note> (image: <path>)");
  other agents → the W12 relay command, note + "image on the user's Mac at <path>" (image transfer to another host
  = follow-up). Never an upload. `OI_SHOT_DRY_RUN=1` / `oi live --dry-run` / `oi shot-send --dry-run` send nothing.
- Settings (python-owned, round-tripped by OISettings): `shots` {enabled, clipboard, fallback_contact, ttl_ms},
  `contact_map` {title: slug | null}. hud-log rows `{"kind":"shot", "event":"press", src, to, via, outcome, action,
  picked, tier, ok, dry_run, error, note_len, ocr_ms, claude:{model, latency_ms, ok}, content}` per press and
  `{"kind":"shot","event":"gone", reason, slots, suggest}` per screenshot leaving the context (the shows are Live's
  regular rows, `trigger:"shot"` for the immediate one): never a title, an OCR text or a note.


## Wish → action (W18)
HUD side `hud/Sources/HUDCore/WishResolve.swift` + `hud/Sources/oi-hud/WishResolveController.swift` (protocol in
hud/README.md "Wish → action").
- Catalogue: installed apps (+ FR/EN aliases: tg, courrier, navigateur, réglages…), hosts of the last 14 token days
  (top 60), desks 1-16, the fleet agents (settings `vocal_recipients`), mail.reply / mail.forward (front app Mail),
  summary. Nothing outside it ever runs.
- Tiers: local (learned aliases, verbs FR+EN, exact / alias / unique prefix / fuzzy; ~1 ms) → Laya (only if a server
  is already up: a `choice` over the ≤ 12 closest actions, p ≥ 0.5; never started from here) → haiku on the
  subscription (`claude_actions.run_claude`, ~2-3 s), its JSON validated against the catalogue ids. Claude off /
  unavailable → the local guess if ≥ 0.6, else noted. Typing = local only; Enter = all tiers.
- Execution: app / url / desk through `live.Executor`; mail.reply / forward = a Claude draft (never sent; plain window
  when Claude is off, fails or the sender is local-only); summary = 3 haiku lines in the panel; agent sends =
  `vocal.send`. Confirm (⇧ or Enter again) for vocal / A2A sends and every primitive not known safe (W16 page
  actions). Unresolved = "noted" (the wish row as before).
- Learning: an executed (+ confirmed) resolution writes its phrases into `<data dir>/aliases.json` (0600; never a
  message or a mail instruction) → the next identical wish is local; the wish row gets `action` and
  `oi.normalise` turns it into `wish:<target>` (a strong label that names the menu option). Each final request:
  one row in `wish-actions/<UTC day>.jsonl` (tier, state, token, latency per tier, text length, never the text).
- `oi wish-resolve "TEXT" [--app Mail] [--final] [--execute [--yes]] [--dry-run] [--mail-context-file F]`.
- W15 follow-up (same PR): a summon (⌥⌘ tap) prefetches the glide follow-ups of its shown options at once (one batched
  haiku call), so a hold right after a summon blooms instantly; never on automatic gated shows.
- Follow-ups: Esc / click outside while "Working…" or a confirm waits sends `{"event":"cancel","id":n}`;
  the engine drops that request (never executed, its confirm dropped, a not-yet-started Claude call skipped).
  Alias learning: a local / learned match is learned at once; a Claude / Laya / local-fallback resolution only once
  the same wish text resolved to the same action twice (`pending` counts in aliases.json; a different action restarts).

## Orchestrator context (W25)
"orchestrator/" workspace) + `oi/rag.py`.
- Identity: `oi/orchestrator/system.md` (chief of staff on the user's Mac, dispatches by topic, proposes, never acts without
  his key) + the roster + `skills/*.md` (dispatch, mail triage, page actions, files) picked per task prompt.
- Roster: `roster.yaml`, GENERATED by `python -m oi.orchestrator.gen_roster` (`--check` = stale?) from
  $OI_FLEET_DIR/team-roster.md, the agents' dept.yaml mandates, $OI_FLEET_DIR/rooms.yaml (business units),
  `oi.vocal.recipients` (reach: kind + host, never a path) and the hand-curated `roster_overlay.yaml` (ask_for, topics).
- Every runtime prompt goes through `claude_actions.render_prompt(name, values, context=…)` → `orchestrator.wrap`:
  system + roster + skills + `<orchestrator_context>` pack + the task last (`check` stays bare). settings
  `claude.orchestrator` (true) = false → the bare W14-W23 prompts. Callers pass their context: glide (app, gated host /
  mail, path chain, agent scope), mail drafts and label enrichment (the mail), summaries (app, host), wish resolution
  (app, host, desk, the wish as RAG query), screenshot notes (app, host, desk, the agent).
- Context pack (`oi/orchestrator/pack.py`, ≤ 1.5k tokens, sections cut/dropped by priority): Now · Dispatch · Talking
  to · Path · Mail sender (+ the sender's wiki people page, matched on name / company) · Wiki notes (top 3) · Habits
  ("from Mail the user usually goes next to …", 7 days of tokens; actions only, never a title). Cached per context key
  (120 s); the RAG runs under a 300 ms deadline (late = no wiki notes, not cached). Logged: `pack <ms> (rag <ms>,
  hit|miss) <tok> <sections>` — never the text.
- Privacy: `local_only_domains` applies to the pack and the RAG query: a gated host is dropped; a gated mail (sender,
  cc, reply-to) is "details withheld" with no sender / subject / people page; any wiki line naming a gated domain, its
  label or label parts (≥ 4 chars), or the withheld sender's name words is dropped. The wish path's mail draft now uses
  the W15 sender+cc+reply-to rule too.
- Warm RAG (`oi/rag.py`): tools/wiki/wiki_search.py imported (its keyword search + hybrid weights), its SQLite index
  opened read-only, all page embeddings decoded once in memory (reloaded when the db changes), one keep-alive
  connection to the local Ollama nomic-embed-text + an LRU of query embeddings. W14's `wiki_snippets` uses it (the
  subprocess only as a fallback). `oi live` warms it at start (`shots.warm_rag`). `python -m oi.rag bench` = 10
  queries. **Measured on a ~1200-page wiki: load 0.96 s once; search p50 60 ms, p95 72 ms; a full
  pack on a cache miss p50 58 ms, max 97 ms; a hit ~0 ms.**
- Dispatch (`oi/orchestrator/dispatch.py`, `<data>/dispatch.json`, 10 min): glide form `dispatch:<agent>` arms "pass me
  to X" (nothing sent) and `dispatch:<agent>:<message>` is a confirmed W12 send that also arms; wish "passe-moi Atlas" /
  "pass me to Orion" arms; "demande à quelqu'un le NAV du fonds" (a topic, no person) → the roster router → a confirmed
  send to Orion; a wish nothing resolves while armed → a confirmed send to the armed agent; the next screenshot's "→
  <Agent>" names the armed agent. The vocal key's own picker reading it = the later "UX du vocal" step (HUD side).
- Real calls (2026-10-20, Sonnet on the subscription, dry-run): the wish "il faudrait vérifier la NAV du fonds avant la
  réunion de demain" → `Send to Orion` (confirm, 2.5 s); a glide on a synthetic investor mail asking the NAV → "Draft
  reply to Dana", "Ask Orion for NAV", "Forward to Orion", "Write reply" (6.3 s batched prefetch).

## Design rule: context is state, not timers
A thing stays in the current context exactly as long as its real state is live: a screenshot while its thumbnail is grabbable bottom-right or its window is open, a mail while unread, an agent message while unanswered, a dashboard alert while open. Predictions use it while live; when it ends, normal context resumes. No fixed "N seconds/minutes after" windows for relevance. Display timings the user chose (hint dot ttl, cooldown) are display settings, not context lifetimes. Every change is reviewed against this rule.

## Behaviour contract
Normative for every connector, existing or new. Code: `oi/proactive/contract.py` (schema, level-3 providers);
check: `tests/connector_contract.py` (`check_connector`), which every connector test MUST call.

1. **One thing.** Anything that changes state and might need the user, whatever its source, MUST be ONE uniform live
   event: who, what, since when. Sensors are thin connectors: they read a state and emit rows, nothing else
   (NEVER send, act, mark read, or read a body outside a level-3 request).
2. **Schema.** A live row MUST carry exactly `{ts, id, source, class, who, subject, ref, private, state:"live"}`
   (+ the judge's `wiki`); its gone row exactly `{ts, id, source, class, state:"gone"}` with the SAME id.
   `source` ∈ mail | agent | file | cockpit | metric | market | calendar | github; `id` = `<source>:<hash of the connector's key>`
   (stable for the life of the state); `ts` = since when it is live (`ref.since` when the source knows an earlier
   start); `who` / `subject` = who / what (`subject` MUST be null when private); `ref` = what the level-1 actions act
   on (message id, chat, path, url, values). Built only through `store.make` / `store.gone`.
3. **State, not timers.** An event MUST stay exactly while its real state is live and end (gone row) once resolved.
   NEVER an expiry, a relevance window or a max age. Re-emitting a live state is a refresh (same id), never a duplicate.
4. **Priming.** Live states MUST show at start (an agent waiting, an open alert, a metric below threshold, an unread
   brief, a market beyond its band). Backlogs PRIME: unread mail and files already there at start are never events.
5. **Health: never a silent failure.** Every poll MUST report health (`last_error`, recorded by the engine in
   health/sensors.json, read by `oi health`). A source that cannot be read while it should be — a script error /
   timeout, a badge or chat list unreadable while the app RUNS, permission, missing folder, unreadable feed, enabled
   but `not_configured` — is a FAILURE, never "nothing". An app the user closed (Mail / Telegram: `not_running`) is
   NOT a failure: a distinct INFO state (`info_kind`, « mail: Mail fermé » in `oi health`), never counted toward
   the 3-failures rule, never touching `last_ok`. On a failed or closed read the state is unknown: nothing starts,
   nothing ends.
6. **One judge.** Every new live event, any source, MUST be weighed by the ONE wiki judge (`judge.py`) before it is
   stored; no per-source relevance lists.
7. **Three levels, the same everywhere** (one renderer: `engine.event_options` + `glide.event_card` / cards):
   level 1 = the action(s) to get there (open, approve, reply, delegate), every send through the outward gate;
   level 2 = who / what / since when, one local line, no model; level 3 = summary + context + why it matters (the
   judge's page), from a declared provider (`contract.LEVEL3`: "model" = local material summarised by Claude,
   "local" = lines from the event's own data).
8. **Privacy.** Local-only domains and the leaks gate make an event `private` when it is made: no subject stored, no
   level-3 summary, no wiki snippet, nothing of it to Claude. Logs carry source / class / reason / numbers only:
   NEVER a name, subject, message id, path or text. Event files stay local (0600).
9. **Learning, within limits.** The system MAY learn from what the user does or ignores after an event (the event token
   in the history stream: Markov / Laya, priors) to rank and to gate display. It MUST NOT learn or take a
   destructive or outward action: no auto-send, no auto-archive / delete / mark-read, no trade; learning changes
   ranking and display only.
10. **Discovery.** Each settings source MUST have its connector, its health and its contract test
    (`test_every_settings_source_has_a_contract_test` fails otherwise).
11. **An app direction lists its live items**. When a glide direction is an APP, its next
    level MUST open with that app's LIVE events (the store, read only; `glide.APP_SOURCES`: Telegram → agent, Mail →
    mail, Calendar → calendar), in the judge's order (wiki-important first, then newest), each a one-click action
    (Telegram: the chat via `tg://resolve?domain=<bot>`, the bot from the event or settings `proactive.agents.bots`,
    labelled « Atlas · 8 »; Mail: that message, then « Reply » to it; a source with a `ref.url`: open / join it) and
    carrying its own level-2/3 card (`event_card`, sent in the `subring` option's `card`). No model call for the list;
    Claude's follow-ups and the local providers stay below. An app with no live item (Safari, Calendar until it has a
    connector) behaves as before. Code: `oi/glide.py` (`live_items`), `oi/live.py` (`glide_env` live_events / bots);
    tests: `tests/test_glide_live_items.py`.

## Connector compliance
✓ = complies, ~ = partial, ✗ = gap. In brackets: the state before the contract was enforced.

| Connector | Schema | Health counted | Unreadable = failure | L1 action | L2 card | L3 content | Judge | Contract test (L1/L2/L3 + gone) |
|---|---|---|---|---|---|---|---|---|
| mail | ✓ | ✓ | ✓ (Mail closed = info) [~ silent] | ✓ reply / open / ask | ✓ [~ no since] | ✓ model (body by id) | ✓ | ✓ [~ ad hoc tests] |
| agents (Telegram) | ✓ | ✓ | ✓ (Telegram closed = info) [~ silent] | ✓ open chat / reply | ✓ | ~ Mac agents only (remote agents: none) | ✓ | ✓ [~] |
| files (sensed by oi-watcher) | ✓ | ✓ | ✓ (watcher-side unreadable / missing folder = failure via status/oi-watcher-files.json; aw-server unreadable = failure; watcher stopped = info) [✗ never read ~/Desktop: TCC denied oi live's Python] | ✓ open / Finder | ✓ | ✗ | ✓ | ✓ (bucket reader, tests/file_watcher_fake.py) |
| cockpit | ✓ | ✓ [✗] | ✓ [✗ alert feed unreadable silent] | ✓ open / ask | ✓ [~] | ✓ local [✗] | ✓ | ✓ [~] |
| metrics (stub feed) | ✓ | ✓ [✗] | ✓ [✗ no / bad feed silent] | ✓ open / ask owner | ✓ [~] | ✓ local [✗] | ✓ | ✓ [~] |
| markets (stub feed) | ✓ | ✓ [✗] | ✓ [✗] | ✓ ask Orion / brief | ✓ [~] | ✓ local [✗] | ✓ | ✓ [~] |
| decisions (dashboard decision queue) | ✓ | ✓ | ✓ (no credential / refused / feed unreadable; a part the cockpit could not read = unknown) | ✓ open the decision / Approuver (Telegram) / ask | ✓ who · what · depuis | ✓ local (titles only: kind, risk, verdict, since; the content stays on the dashboard page) | ✓ | ✓ |
| calendar | ✓ | ✓ | ✓ (no Calendars access = `permission`, script error) | ✓ join / open Calendar | ✓ title · à / jusqu'à · who | ✓ local (attendees, time, link) + the judge on the attendees' wiki pages | ✓ | ✓ |
| prs (GitHub via `gh`) | ✓ | ✓ | ✓ (`gh` unreadable) | ✓ open the PR (the dashboard's PR page for `cockpit.pr_owners`) / ask Atlas | ✓ | ✓ local (summary line + independent verdict, read once at start) | ✓ | ✓ |
| shots (W17, oi/shots.py) | ✗ own live set | ✗ | ✗ (ActivityWatch down = silent) | ✓ → agent / other / annotate | ✗ | ~ tier-2 OCR note | ✗ | ✗ |

Files via oi-watcher: macOS TCC denies ~/Desktop
to `oi live` (a launchd Python): every poll was `folder_unreadable`, the source never worked. The signed
oi-watcher.app already reads the screenshot folder there, so it now lists `proactive.files.folders` (only while
`proactive.sources.files` is true; every 3 s, names only, the first listing primes) and posts to `oi-watcher-file_<host>`
(`os.file.state`): `{"kind":"file","state":"live","path","class","mtime","size"}` for a new entry, `{"state":"gone",
"reason": removed | opened | dropped}` when a live one is removed, opened (kMDItemLastUsedDate changed) or pushed out
by 20 newer ones. Never content. Its read state per folder (`ok | unreadable | missing`) goes to
`status/oi-watcher-files.json` (every 30 s and on change). FolderSensor is a thin reader: bucket edges -> live / gone
rows (ts = when the watcher saw it), status -> health (`folder_unreadable` / `folder_missing` failures, the folder's
files unknown; `aw_unreadable` failure; no fresh status = `watcher_not_running`, INFO, since `oi health` already counts
the oi-watcher service).

Not event sources (context inputs, outside the contract): ActivityWatch window / AFK stream (`oi/aw.py`), the Mail
selection reader, Safari's URL, the onboarding readers.

Three more sources, each through `check_connector` (tests/test_connector_contract.py).
- **decisions**: your dashboard's decision queue through a JSON endpoint `GET <cockpit.url>/a-decider.json`
  (read-only, a stable `id` per item, titles only, no draft body, the parts it could not read flagged:
  `prs_known` / `prs_unavailable` / `board_unavailable` / `gates_unreadable`). Auth = a SCOPED read-only bearer the
  dashboard accepts ONLY for that route (403 everywhere else); never an admin token. On the Mac it lives in the login
  Keychain, service `os-intent-dashboard-read`, account `oi` (`security add-generic-password -s
  os-intent-dashboard-read -a oi -w`) — never in settings, a log or a URL; absent = `not_configured`. Token
  `evt:cockpit:decide-gate|board|pr`; level 1 = the dashboard page that decides it, « Approuver (Telegram) » (the
  agent bot's chat, where its approval buttons are; `proactive.agents.bots`), ask the agent; level 3 = the item's
  kind label + risk + verdict + since (titles only: the content stays on the dashboard, behind its login).
- **calendar**: EventKit through JXA (no pyobjc; `CALENDAR_JS`), read-only, never requests access (status ≠ granted =
  `permission`; Calendars access is granted once at install: grant it once, from Terminal). A meeting starting within `calendar.horizon_min` (120) or under way = `evt:calendar:meeting`, live
  until it ends; moved = a new state. Only a join link (zoom / meet / teams / whereby / webex) leaves the notes, without its query / fragment (a passcode
  lives there), and it is never logged (the hud-log has the option token only).
  Level 1 join / open Calendar; level 2 title · à 15:30 (· jusqu'à 16:30 under way) · attendees; level 3 attendees,
  time, link + the ONE judge, whose query now adds the attendees' names (their wiki pages = the prep context).
- **prs**: local `gh`: PRs whose review is requested of you (any owner) + the open PRs of `prs.repos` (default none);
  drafts skipped. `evt:github:<repo>`; level 1 opens it (the dashboard's PR page for an owner in `cockpit.pr_owners`,
  else GitHub); level 3 = the description's first line + the last « Independent review: » verdict, read once when the
  state starts.

Next steps: (1) screenshots onto the contract (source `shot`, event rows, engine health, card, judge); (2) agents
running on another host waiting on you (needs a read on that host); metrics / markets from a real read-only feed;
(3) level 3 for files (a decision: reading a file's content) and for remote agents; (4) one declarative action
register instead of `engine.event_options`' per-source branches; (5) connector discovery from a registry; (6) the
"ignored" signal (hint dismissed) fed back as a weight.

## Onboarding → profile + starter catalogue (W22)
The user: an onboarding at first launch so the tool is useful from day one and
shareable ("Lena, architect → SketchUp, 3D render requests to her cloud; us → our agent team"); the Q&A also seeds attention priors for the W24 relevance gate. `oi/onboard.py`, `oi/starter.py`,
`oi/prompts/onboard*.md`; oi-menubar `Onboarding.swift`; oi-hud `vocal_to`.
- `oi onboard` (menubar "Onboarding…", offered once at the first launch with no profile.yaml: a Terminal window via
  `<data>/onboard.command`): privacy statement, then each source with its privacy line and an explicit y/N — installed
  app NAMES (always), recent activity (oi's token files, else the local aw-server, in memory: app names, site hosts,
  usual hours, the app before), Safari Top Sites + bookmarks (hosts; needs Full Disk Access, else skipped), Google
  Drive for desktop (first two folder levels, names), Mail (account + mailbox names), Notion (stub: no connector).
  Then 7 questions (job, tools, clients / projects, recurring tasks, counterparts, attention vs noise, other), "your
  agent team?" (the W25 roster), ≤ 3 Claude follow-ups, one sonnet synthesis. `--answers F` / `--sources-file F`
  (synthetic person) / `--no-claude` / `--no-followups` / `--out DIR` / `--dry-run`. Ctrl-C = nothing written.
- Privacy: every source keeps names and counts only, never a title, URL path, file, page or mail; the answers stay in
  memory. Before the prompt, `pack.leaks` (W25, settings.claude.local_only_domains) drops any answer sentence, host,
  folder or mailbox naming a local-only domain / client; after it, any item or profile field that still names one.
  Denylisted and system apps never appear. The onboarding prompts are NOT wrapped by the orchestrator (NO_WRAP: the
  person may not be the user); the roster goes in the prompt only when the person works with that agent team.
- `profile.yaml` (0600): job, summary, domain vocabulary, tools, clients / projects, recurring tasks, counterparts
  (agent / person / service + slug / ask_for / via), `attention` {sources {mail…: 0-1}, important / noise [{who, class,
  weight}]} (`onboard.attention_prior(profile, source, who)` = W24's entry point), source counts.
- `starter_catalogue.yaml` (0600): 15-30 items {id, label ≤ 22, primitive, params, trigger {apps, hosts, hours [h0,
  h1), days}, prior 0.05-0.6, source claude|activity|safari|apps|roster, why}. Primitives: `app.open` {app, deeplink
  (custom scheme only)}, `url.open` {href https, no query}, `mail.draft` {to, subject, body} (`open mailto:` = a draft,
  never sent), `agent.ask` {to: roster slug} (oi-hud `{"cmd":"vocal_to","pick":slug}` = the recording panel to that
  agent; Enter sends, Esc cancels). Claude's items are validated (app installed / in the person's list, roster slug,
  https, local-only, prior / label / trigger shape), then topped up by the local builder (most used apps / sites with
  their hours and predecessor app, Safari hosts, "Ask <agent>", installed apps) and deduped by target / host.
- Live driver (`Starters.blend`, after Markov / Laya): λ = half_support / (half_support + support of the context);
  p_item = (prior·4 + picked + wished) / (4 + shown); a Markov / Laya option on the same app / host → (1-λ)·p + λ·p_item,
  else a new option at λ·p_item; "anywhere" items count half; the front app / site itself is never proposed. A starter
  option lifts the thin-history gate. Picks / shows → `starter-stats.json` (0600); every retrain re-counts executed W18
  wishes (`wished`) and turns a wished app / site no item covers into a `learned` item (prior 0.15, trigger = the app).
  hud-log option rows carry `starter: <id>`. settings.json `starter` {enabled, half_support (20)}.
  `oi starter-preview --app A [--host H] [--hour h] [--support n]` prints what fits a context (labels only).
- Dry runs (labels + counts only): a real user (1 token day + their Mac's apps, seeded answers) 29 items; a synthetic
  architect 26 items. Day one in Telegram 15h / Safari github / Mail: 4-6 options ≥ show_min_p.

## Proactive hints (W24)
The user: "je n'ai pas bougé la souris, mais l'état a changé (un mail reçu, un
message d'un agent, le Drive, les marchés, une métrique qui a chuté, une panne dans le cockpit) … l'action la plus
probable que je vais faire après, c'est X". `oi/proactive/` (config, sensors, judge, store, relevance, engine) + oi-hud
`marker` + the Settings window "Proactive" section.
- Design rule applied: every sensor reads a STATE and reports both edges: an appear row
  when it starts, a gone row (same id, token `evt:<source>:<class>:gone`) when it ends. No relevance window, no
  max age: an event is hintable and in the Claude context exactly while it is live (`store.Live` in the engine). A
  read that fails (Mail / Telegram not running, a folder or feed unreadable, not configured) is unknown: nothing
  starts, nothing ends; it is a health failure (`last_error`, « Behaviour contract » 5). What is on
  at the first poll is live state and shows (agents waiting, open cockpit alerts, metrics below threshold, an unread
  brief / a market move; an agent already waiting when `oi live` starts shows).
  Mail and files PRIME (the unread backlog / the files already there are not news: never an event; news again only
  after seen off). An agent's event is refreshed when its pending count grows (same id, new n / since, gated again).
- Sensors (local, READ-ONLY, each opt-in, ALL OFF by default: settings `proactive.sources`): **mail** (Mail's inbox per
  account, newest 8: message id, sender, subject, date, read + replied flags; AppleScript only while Mail runs, never
  launched; a new unread mail = event, live until read or replied or out of the inbox; a live mail beyond the scan is
  looked up by id, `MAIL_STATE`), **agents** (Telegram's unread badges: each chat row's numeric badge through
  Accessibility — the row title and its count only, never a message text — and the Dock badge via `lsappinfo`
  (0 = every chat read); an agent chat of `contact_map` with unread messages = event, live until its badge is gone;
  a chat not in the visible list is unknown), **files** (new top-level entries of
  `proactive.files.folders`, default ~/Desktop; a Drive folder under ~/Library/CloudStorage works: one stat per poll,
  the listing only when the folder's mtime changed; hidden / partial downloads / screenshots skipped; live until
  opened — Spotlight's kMDItemLastUsedDate changes — or removed; a folder Spotlight does not index: until removed),
  **cockpit** (your own ops dashboard, optional: outage = its unauthenticated probe `<url>/health-noauth` fails twice,
  live until it answers again; alerts = the ids of a local JSON feed file, live while listed and not `"open": false`),
  **metrics** / **markets** (local JSON feed files; never a trading action: a metric live while below its
  threshold; the brief live while current and not `read`; a market move keyed on the STATE "symbol beyond ±move_pct"
  (+ direction), fired on entering it, live while it lasts, fired again only after it ended).
  Sender class (agent / other): settings `proactive.mail.agent_senders`, or your team's mail domain (`proactive.mail.team_domains`) + a roster slug. Whether
  it is a client is the wiki's call (below): the judge turns "other" into "client".
- ONE relevance judge (`judge.py`: not a pile of tools, one judge weighing everything against the wiki — your local
  markdown knowledge base, `OI_WIKI_DIR`): every new event, any source, is weighed against the wiki on the sensor thread before it is stored.
  Query = its non-private fields (mail: sender name, the name in the address, the company of a non-free-mail domain,
  the subject; agent / file / system events: the subject / title only) -> the warm local wiki search (`oi/rag.py`,
  the same index as wiki_search.py: local embeddings + FTS) restricted to pages whose frontmatter makes them count
  (tags client / customer -> client; prospect / lead / investor -> prospect; type project / person; status stale /
  removed / superseded… and archive/ excluded). Important when a top-3 hit is NAMED by the event (a word of its file
  name, in <= 5% of the wiki's pages; two words for a project page) or its cosine >= 0.72. Verdict {important, reason
  "wiki", page, kind, snippet <= 200} in the event (the HUD's "why", level 2/3 cards next); the events file and the
  hud-log keep the page name only; a private event gets no snippet. Cached per query, 1.5 s budget (-> not important).
  Calibrated on 40 real inbox headers (sender + subject, read-only): 2 important (2 client pages);
  the cosine alone does not separate (unrelated mails reach 0.68) and title words over-match ("finance").
  Removed with it: settings `proactive.mail.client_domains` / `client_senders`,
  `proactive.pipeline_file`, people.py (wiki people-page tag scan + profile "client" rows). Kept: hard `priorities`
  and the W22 profile `attention.important`.
- Events → `events/<UTC day>.jsonl` (0600, local: name + subject stay here) and the token `evt:<source>:<class>`
  (`mail:client`, `agent:orion`, `file:desktop`, `cockpit:outage`, `metric:<id>`, `market:brief|move`; src oi-evt) in the
  same stream (oi.normalise reads the events files; `oi live` inserts it at once). An event is a moment and a history
  token, never a label (moments.py: the label skips events), so Markov / Laya learn "after E the user does A" with the
  same training loop.
- Relevance gate (`relevance.gate`, order): gone (the state already ended) · paused · hud_disabled · auto_off · denylisted
  front app · quiet hours (`quiet_hours` 22:00-08:00 Paris) / Focus (`respect_focus`; read from
  ~/Library/DoNotDisturb/DB/Assertions.json when readable, else ignored) unless a priority with
  `priorities_break_quiet` · `below_threshold`: p < `min_p` (0.5), skipped by `priorities` (["mail:client",
  "cockpit:outage"]) and by important events (priority · wiki judge · W22 profile: the full cross) · `rate_limited`: one proactive hint per `min_interval_min` (10), a DISPLAY setting the
  priorities skip (Focus / quiet hours still apply to them). An event held back for a passing reason (rate limit,
  quiet hours, Focus, AFK, the HUD busy, another hint in the same loop) is considered again while live (every 5 s);
  once gone it is dropped. p = λ·hist + (1-λ)·prior, hist =
  1 - P(nothing) at the event token (order-1 Markov counts), λ = n/(n+5), prior = W22 `attention_prior` (profile.yaml;
  defaults mail 0.4, agent 0.5, file 0.3, cockpit 0.6, metric 0.4, market 0.3).
- Delivery: the W19 hint (`"mode":"hint"`) with `"marker":"ring"` → oi-hud draws a thin ring around the dots
  (state `dots:1,2:ring`); ⌥⌘ develops the event's actions: mail → "Reply to <first name>" (`mail.reply` on THAT mail:
  `mail.show` selects it first; long-press = the W14 draft), "Open the mail" (`mail.show`), "Ask <agent>" (roster topic
  of the subject); agent → "Open <Agent>" (`telegram.chat`), "Reply to <Agent>" (`agent.ask`); file → "Open <name>"
  (never a script / app / installer: drill.launchable) / "Show in Finder"; cockpit → "Open cockpit / the alert"
  (`url.open`), "Ask Atlas" (outage) / roster; metric / market → "Ask <owner / Orion>", the feed's link. + the most likely
  history action after the event (Markov at the event token) when a slot is left. The glide (W15/W23) works on the
  EVENT's mail (`GlideSession.mail_fixed`, no body), the long-press too (the event's mail is selected first,
  `expect_mid`). "full" auto mode = the cross; "off" = nothing. The event's gone row withdraws its hint while it is
  still up (oi-hud `{"cmd":"hide"}` now also ends hint dots, `HintLife.hideTarget`; hud-log reason "state_ended").
- Context for the model (W25 pack, item 5): "After event", "Events"
  (the events live NOW, newest first, at most 6 — no time window: time, source, class, sender, subject; a gated one = "details withheld"; a line naming a
  local-only term dropped), and the sender's wiki people page through the existing "Mail sender" section.
- Privacy: local_only_domains (sender) + the W25 leaks gate (name / subject / file name) make an event `private` when it
  is made: no subject stored, no Claude flag on its options, `ClaudeActions.press` refuses it (plain action), the glide
  sees it gated. Logs: a shown hint row carries `proactive` {source, class, p, priority, hist_n, prior}; a gated event =
  {"kind": "proactive", source, class, reason, p, …}. Never a name, subject, message id or path.
- `oi proactive-probe [--source S] [--wait s]`: poll sources twice, print tokens only, write nothing (install check).
  Mail and the Telegram AX reader prompt for Automation / Accessibility the first time: grant them at install.
- Latency: mail poll 6 s + `oi live` loop 2 s → a hint ≤ 8 s after Mail shows the message (test on a fake clock).

## Outward gate
"il manque une gate de validation pour toutes les actions extérieures … vous allez envoyer ce message à ce
destinataire, validé ou pas ?" — `oi/gate.py` `outward(cls, to, name, preview)` is the ONE check every sender calls
right before it sends, dry runs included: `vocal` (oi.vocal.send: HUD voice note, glide "Ask Atlas" / a send, W25
hand-over, wish send), `shot` (oi.shots.deliver), `page` (oi.live ui.press unless KNOWN read-only:
`gate.page_outward` is fail-safe — a link, or a button whose first word is a read-only verb / view (open, view, show,
expand, copy, next, files, code …), with no irreversible / outward word; an unknown learned label asks), `reply`
(a W24 proactive option marked `"outward": true`: reply / forward / draft / the chat, at the pick; its "Ask <agent>"
is gated at the vocal send, never twice).
ONE confirmation: pick → gate panel → validate. Removed on gated paths: the W16 two-tap, the glide
"Send ✓" child, the W18 ⇧ confirm (kept only for an action neither gated nor known safe, e.g. a2a.send).
oi-hud draws the confirm panel (hud/README.md "Outward gate"); only an explicit validate sends; Esc / ✗ / click
outside / timeout (20 s) / no HUD = cancelled, never an auto-send. "Ask Atlas" sent on release is gated too.
Not gated: drafts the user sends himself in Mail (mail.reply / forward / draft) outside W24, opens, music, desk moves,
dispatch.arm, read-only page presses.
settings.json `gate.outward`: true (default) | false | {"vocal"|"shot"|"page"|"reply": bool} (menubar Settings →
Privacy: one toggle, writes a bool); off never skips the panel for an irreversible page label or a wish press. gate-log/<UTC day>.jsonl:
{id, cls, to (slug / page target), decision validated|cancelled|off, sent, reason, dry_run, latency_ms}, ids only.
