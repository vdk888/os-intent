# `oi` command reference

ActivityWatch events → action tokens, per the token contract in `docs/DESIGN.md`.

## Install
```sh
python3.12 -m venv .venv && .venv/bin/pip install -e .[dev]
.venv/bin/python -m pytest -q
```

## Commands
- `oi pull [--since ISO]` — incremental, read-only pull of every bucket of the local aw-server
  (`OI_AW_URL`, default `http://127.0.0.1:5600`). A cursor per bucket; rows are upserted by id,
  so repeated pulls never duplicate.
- `oi tokens --day YYYY-MM-DD|today` — (re)build that UTC day's token file from raw (idempotent).
  Prints a count only; `--show` prints the rows.
- `oi stats --day YYYY-MM-DD|today` — counts per token kind and top apps; no titles or URLs.
- `oi live [--once] [--dry-run] [--socket PATH]` — the live driver (W8): Markov predictions on the HUD,
  run by the LaunchAgent `com.osintent.oi-live` (`live/install.sh`). See docs/DESIGN.md "Live driver".
  **Do not run `oi pull` by hand while the live agent runs**: they share `cursors/` and the raw files with no
  lock. Stop the agent first (`launchctl bootout gui/$(id -u)/com.osintent.oi-live`) or use another `OI_DATA_DIR`.
- `oi vocal-send --to <slug> --text-file <f> [--audio <caf>] [--app A] [--desk k] [--dry-run]` — W12:
  delivers a HUD voice note's transcript to ONE agent's inbox (local inject file, or your relay command with the
  line on stdin and TELEGRAM_*/GH_TOKEN/GITHUB_TOKEN removed from the sender's env). oi-hud runs it after a
  "Send a vocal to…" recording. `--dry-run` prints the exact command and line and sends nothing. One hud-log row
  per attempt (`{"kind":"vocal"}`, never the text); a sent one becomes the token `vocal:<slug>`.

- W13 Laya layer (docs/DESIGN.md "Laya layer"): `oi laya-serve [--check]`, `oi replay --days …`,
  `oi laya-train --train-days … --test-days …`, `oi laya-promote --report …`, `oi improve-report`.
  `oi laya-bench --gap 5` (latency on real-shaped requests), `oi laya-report` (shadow: Laya vs Markov vs what you
  did, plus the promotion bar); `live.laya_rank: shadow|blend|live` (default shadow) is your switch. `oi laya-switch
  on|off|status` = the menu bar's Laya toggle (settings `laya.enabled`; off stops the server and frees its memory).
  Shadow mode: settings.json `live.laya_shadow: true`. One heavy ML process at a time (4 GB cap, swap < 4 GB).
- `python -m oi.claude_actions check` — W14: one harmless headless `claude -p --model haiku` call on your Claude
  subscription (never an API key); prints the auth path and the latency. `python -m oi.claude_actions run --action
  mail.reply --context-file <synthetic.json> --dry-run` runs the draft pipeline on a given context (no Mail read) and
  prints the AppleScript instead of opening a window.
- `oi live --dry-exec` + `oi-hud debug-glide --path 2,1 [--cancel]` — W15: a ⌥⌘ glide without keys (hold,
  rest until each sub-ring opens, release) against a driver that executes nothing (prints what a press would run).
- W23 drill-down glide (oi/drill.py): each sub-ring acts INSIDE its parent. Finder → recent files / folders
  (Spotlight kMDItemLastUsedDate, Downloads, Desktop, the project folder) → `folder.open` / `file.open`; Telegram →
  chat titles (AX of the chat list, the row's first text only; fallback: Telegram window-title tokens) →
  `telegram.chat` (AX press of that row); Music (only when running) → playlists → tracks → `music.play`; a site →
  today's pages + Summary; Mail → reply / forward + Claude drafts / messages; a send (to an agent, "Ask Atlas") runs on release and waits for the outward gate's panel (the one confirmation); scripts / apps / installers / executables are never opened (only their folder, in Finder).
  History (Markov on the parent token + `<data dir>/glide-pairs/` choices, ids only) ranks; Claude (haiku) only for
  agent / mail scopes; any other app / site switch is dropped except "back". Needs Accessibility for the python of
  `oi live` (Telegram titles / press) and the Automation prompt for Music on first use.

`oi shot-send --to <slug> --image <png> [--note-file f] [--app A --host H --desk k] [--dry-run]` (W17): one
screenshot delivery by hand; `oi live` offers it on the HUD after each screenshot (docs/DESIGN.md "Screenshot → current
contact"). The tier-2 note needs `ocr/` built: `cd ocr && swift build -c release`.
Recipients: atlas / juno (this Mac) get the image copied into their channel inbox; A2A recipients on other hosts
(Nova, Orion, Lyra, Iris, Vega, Hera, Echo) get the note + "image on the user's Mac at <path>", a path
they CANNOT open: by design until the image-transfer follow-up. Clipboard screenshots: see
watchers/oi-watcher/README.md (possible macOS pasteboard privacy alert; `"shots": {"clipboard": false}` turns them off).

- `oi onboard [--answers F] [--sources-file F [--send-source-names]] [--no-claude] [--no-followups] [--out DIR] [--dry-run] [--offline]`
  — the onboarding (W22): an agentic Claude session reads, read-only and only what you opt into, your Claude memory,
  your knowledge base (`OI_WIKI_DIR`), recent mail and Notion, asks a few questions, and writes `profile.yaml` +
  `starter_catalogue.yaml` (15-30 contextual actions with priors) to the data dir; `oi live` blends them with Markov /
  Laya from day one, decaying with real usage; what you call noise is never proposed. `--offline` = the short
  deterministic Q&A instead. oi-menubar "Onboarding…" opens it in Terminal. `oi starter-preview --app A [--host H]
  [--hour h]` shows what fits a context. See docs/DESIGN.md "Onboarding".
- `oi proactive-probe [--source mail|agents|files|cockpit|metrics|markets|decisions|calendar|prs] [--wait s]` — poll
  the proactive sensors once (prime, then report) and print the event tokens only; nothing written or shown. `oi live`
  runs the sensors that settings.json `proactive.sources` turns on (all off by default) and shows a ring hint for the
  relevant events. See docs/DESIGN.md "Proactive hints" and "Behaviour contract".
- `oi health [--json]` — production health: each enabled sensor's last poll (failures vs info such as "Mail closed"),
  the LaunchAgents (`launchctl list com.osintent.oi-*`), ERROR / Traceback lines in the recent logs, Laya, and whether
  the AppleScripts compile. Exit 0 ok / 1 problem / 2 cannot check.

## Recording a demo
`oi demo <scene>` / `oi demo contrast` / `oi demo all` (`oi/demo.py`) plays scripted HUD scenes for a screen recorder,
with **fake data only** (Sam Rivera, Dana Park, Pat Lee, Jordan Blake, Northwind Billing, acme/rocket #42, agents
Atlas / Nova / Orion / Lyra). It starts its **own** oi-hud (`serve --socket /tmp/oi-demo-…/hud.sock`) with a fresh
temp `OI_DATA_DIR`, never the live HUD, the live data dir or its socket (refused). Nothing executes and nothing is
sent: a press, a wish or a note runs nothing, Claude is never called (canned cards), the vocal goes to a dry-run stub,
the outward gate panel is always cancelled; TELEGRAM_* / GH_TOKEN / GITHUB_TOKEN / ANTHROPIC_* are removed from the
HUD's environment. `oi demo` refuses to start while the live HUD is enabled (`--force` overrides). Backdrops (fake
pages, no network) live in `demo/backdrops/`; `--backdrop` opens each scene's page. `oi demo list` prints the scenes.
```sh
(cd hud && swift build -c release)          # the demo HUD (fictional roster via OI_HUD_AGENTS)
.venv/bin/oi demo contrast --backdrop --step  # day 1 → "30 days later" → day 30
.venv/bin/oi demo all --backdrop --step       # every scene in film order
```
The film script and shot list are in `docs/film/`.

## Data
`~/.local/share/os-intent/` (override with `OI_DATA_DIR`), created mode 0700:
`raw/<bucket>/YYYY-MM-DD.jsonl`, `cursors/<bucket>.json`, `tokens/YYYY-MM-DD.jsonl`,
`vocals/<YYYYMMDDTHHMMSSZ>-<slug>.caf|.txt` (W12: the voice note's audio, never transferred, and its transcript; 0600).

## Privacy
Raw titles and URLs stay on this Mac, outside the repo. Nothing personal
goes in git, issues or PRs. Nothing personal is printed by default. Denylisted apps
(Passwords, Keychain Access, 1Password) emit `activate:<App>` only; Safari rows flagged
`incognito`/`private` are dropped. Test fixtures are synthetic.
