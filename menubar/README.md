# oi-menubar (W5)

Menu-bar app for OS-intent: one brain icon (dimmed while logging is paused), no Dock icon.
It shows the state of the pieces and edits `~/.local/share/os-intent/settings.json`, which
oi-watcher, oi-hud and the `oi` normaliser read. Local only: it talks to launchd, to
127.0.0.1:5600 and runs the repo's `oi` CLI; nothing leaves the Mac, no new logging source.

## Build, test, run

```sh
cd menubar
swift build -c release                    # -> .build/release/oi-menubar
swift test --scratch-path .build-test -Xswiftc -plugin-path -Xswiftc /Library/Developer/CommandLineTools/usr/lib/swift/host/plugins/testing
.build/release/oi-menubar --print-menu    # probe once, print the menu as text, exit (no UI)
.build/release/oi-menubar                 # run it (status item appears; Quit from its menu)
./install.sh                              # ~/Applications/OS-intent/oi-menubar.app (signed) + LaunchAgent com.osintent.oi-menubar
./uninstall.sh                            # remove app, agent, CLI symlink; settings.json and logs are kept
```
The `-plugin-path` flag: with the current CommandLineTools the swift-testing macro plugin lives in
`host/plugins/testing/` and is not found otherwise ("plugin for module 'TestingMacros' not found").
Swift 6 language mode, macOS 14+, SwiftPM only. Log: `~/.local/share/os-intent/logs/oi-menubar.log`.

## The menu (refreshed on open, then every 5 s while open)

**Status**
- *Watcher*: `launchctl print gui/<uid>/com.osintent.oi-watcher` → running (pid) / stopped / not installed,
  plus "paused" when settings.json says so.
- *ActivityWatch*: `GET 127.0.0.1:5600/api/0/info` (1 s timeout) → reachable (version) / UNREACHABLE.
- *HUD daemon*: launchd state of `com.osintent.oi-hud` if that agent exists, else "running (manual)" when
  `hud.sock` exists, else "not installed"; "display paused" when `hud_enabled` is false.
- *Today*: `<repo_dir>/.venv/bin/oi stats --day today --json` → total, distinct, one row
  per token kind (no titles). "no tokens yet" if today's file is missing, "n/a" if the venv is missing.

**Watcher permissions** — oi-watcher's OWN view of its grants (macOS checks permissions per app, so
the menu bar app cannot probe them for the watcher; before W7 it showed its own grants, which misled).
Read from `~/.local/share/os-intent/status/oi-watcher.json`, which the watcher rewrites every 30 s:
Input Monitoring = `key_tap` (event tap active), Accessibility = `ax`, Automation → Safari =
`safari_automation` (granted / denied / not yet asked / unknown when Safari is not running).
✓ granted, ✗ not granted / denied / not yet asked, ? unknown. File older than 90 s or absent → header
"watcher silent" and ? rows. Clicking a row opens the pane
(`x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent|Privacy_Accessibility|Privacy_Automation`);
*Re-grant guide…* opens the watcher README's "Re-grant permissions" section. The app never requests or grants anything.

**Controls**
- *Pause logging / Resume logging*: flips `paused`. The watcher stops posting within 5 s.
- *HUD enabled*: flips `hud_enabled`.
- *Laya* (2026-10-21): checkmark = settings `laya.enabled`, status line from `oi laya-switch status --json`
  (« en marche · modèle chargé » / « démarrage… » / « refusé : mémoire » / « arrêté »). The click runs
  `oi laya-switch on|off` (oi/laya_switch.py): off persists `laya.enabled=false` and SIGTERMs the supervisor
  (`launchctl kill SIGTERM gui/<uid>/com.osintent.oi-laya`; the agent stays loaded, RunAtLoad/KeepAlive false), so oi
  live runs Markov only and never starts Laya, `oi health` shows the info « Laya désactivé »; on persists
  `laya.enabled=true` and runs ensure_server (memory rule: refused when swap ≥ 4096 MB, shown in the status line).
  It is a setting, so it survives a reboot: on, oi live starts Laya at its own start; off, nothing starts it.
- *Launch at login*: the LaunchAgent only (W9): ON while `~/Library/LaunchAgents/com.osintent.oi-menubar.plist`
  exists and is not disabled; the toggle runs `launchctl enable|disable gui/<uid>/com.osintent.oi-menubar`
  (RunAtLoad then starts it, or not, at the next login). Greyed out when the plist is missing (run install.sh).
  The W7 `SMAppService` login item is gone; if you registered it earlier, remove "oi-menubar" from
  System Settings → General → Login Items (a second instance would exit at once anyway).
- *Restart watcher*: `launchctl kickstart -k gui/<uid>/com.osintent.oi-watcher`.
- *Rebuild today's tokens*: `oi pull && oi tokens --day today` in the background; the result line
  (time, token count, invalid count) appears under the item.
- *Settings…* (W26): a SwiftUI window (Liquid Glass cards on macOS 26, material fallback otherwise or with
  Increase Contrast / Reduce Transparency), one sidebar section each: HUD (`hud.auto_mode`, `hud.hint_dot_size`),
  Timings (`hud.hint_ttl_ms`, `gate.hint_cooldown_s`, `gate.ttl_ms`), Pointer (`gate.hide_on_pointer_travel`,
  `pointer_grace_ms`, `pointer_fast_px_s`), Colours (`hud.colors`, light + dark per key), Fill order (`hud.fill_order`),
  Keys (read-only recap + Hotkeys…), Claude (`claude.enabled`, `enrich`, `local_only_domains`; model shown), Screenshots
  (`shots.enabled`, `clipboard`, `fallback_contact`), Vocal recipients and Laya (read-only), Privacy (`paused`,
  `denylist_apps`). Ranges = the readers' clamps; an invalid value is shown in red and nothing is written until the
  form is valid; each valid change is written ~0.3 s later through OISettings (atomic, unknown keys kept): only the
  values changed in the window, merged into the current file, so a menu Pause while the window is open stays. Reset per
  section. The daemons apply it themselves: oi-hud on its next show, `oi live` on its next tick, oi-watcher within 5 s.
  Preview without touching the real settings: `OI_DATA_DIR=/tmp/x OI_SETTINGS_SECTION=claude .build/release/oi-menubar --settings`.
- *Denylist…*: a window, one app name per line → `denylist_apps`. A denylisted app yields `activate:<App>`
  tokens only (no title, url or ⌘-chord tokens). The built-in Passwords / Keychain Access / 1Password
  always apply. Takes effect at the next token build (use Rebuild).
- *Hotkeys…*: options 1-4 each have a segmented choice "⌃ tap | ⌥ tap | ⌘ tap | fn tap | chord" (W9: a
  tap = the bare modifier pressed and released alone while the HUD is visible); "chord" enables the recorder
  field (click, press the combo, like the macOS Keyboard Shortcuts prefs). Dismiss and wish are chords only.
  → `hotkeys`; collisions, a tap used twice and typing keys without a modifier are shown in red and block Save;
  *Reset to defaults* restores the ⌃ ⌥ ⌘ fn taps, Esc, ⌥`. The HUD reads them on its next show.
- *Open data folder* / *Open logs*: Finder on `~/.local/share/os-intent/` and its `logs/`.
- *Quit oi-menubar*: quits until the next login (KeepAlive is false on purpose).

## Onboarding… (W22)
"Onboarding… (not done yet)" / "(redo)" (done = `<data>/profile.yaml` exists) writes `<data>/onboard.command` (0700:
`OI_DATA_DIR=… <repo>/projects/os-intent/.venv/bin/oi onboard`) and opens it in Terminal. At the first launch with no
profile, an alert offers it once (`status/onboarding-offered` keeps it from coming back). Logic: `MenubarCore/Onboarding.swift`.

## settings.json

Schema, defaults and who reads what: `../docs/DESIGN.md` → "Settings". Reader/writer: `../settings/` (Swift package
`OISettings`, atomic temp-file + rename, mode 0600, dir 0700) and `../oi/settings.py` (python, read-only).

## Layout
- `Sources/MenubarCore/` pure logic, unit-tested: launchctl `print` / `print-disabled` parsing, permission
  state → symbol, `oi stats --json` decoding, Status → menu entries.
- `Sources/oi-menubar/` the app: `AppController` (NSStatusItem + NSMenu, in-place refresh), `Probe`
  (launchctl, aw-server, TCC, oi CLI; runs off the main thread), `Windows` (denylist + hotkeys editors).
- `Support/Info.plist` embedded in the binary: bundle id `com.osintent.oi-menubar`, `LSUIElement`.
- `com.osintent.oi-menubar.plist` LaunchAgent template (RunAtLoad true, KeepAlive false).
