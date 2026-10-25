# oi-watcher (OS-intent W2)

A small Swift command-line daemon that adds three signals ActivityWatch does not log
to the local aw-server (`http://127.0.0.1:5600`, the only network it talks to):

| Bucket (host = `gethostname()`, e.g. `my-mac`) | Event `data` | Token (docs/DESIGN.md) |
|---|---|---|
| `oi-watcher-space_<host>` (type `os.space.change`) | `{"space": <int or null>, "delta": +1/-1/0}` | `space:+1`, `space:-1`, `space:=k` |
| `oi-watcher-key_<host>` (type `os.keyboard.chord`) | `{"chord": "⌘C"}` | `key:⌘C`, `key:⌘⇧4` |
| `oi-watcher-safari_<host>` (type `web.tab.current`) | `{"url": "https://host/path", "title": "..."}` | `url:<host>/<slug>` |
| `oi-watcher-file_<host>` (type `os.file.state`) | `{"kind": "file", "state": "live", "path", "class", "mtime", "size"}` / `{"kind": "file", "state": "gone", "path", "class", "reason"}` | none (oi live's « files » connector, not a history token) |

Events carry `duration: 0` and an ISO-8601 UTC timestamp with milliseconds.

## What it logs, and what it never logs
- **Spaces:** one event per desktop switch (`NSWorkspace.activeSpaceDidChangeNotification`).
- **Keys:** only key presses **while ⌘ is held**, as a chord name (`⌘⇧4`, `⌘C`, `⌘F1`,
  `⌘→`). Modifier order is ⌘ ⇧ ⌥ ⌃. Letters follow the current layout (AZERTY ⌘A is `⌘A`),
  the top digit row is always a digit. Auto-repeat is ignored. The tap is **listen-only**
  and checks the ⌘ flag before reading the key code: plain typing is never read,
  formatted, queued or logged. Password fields under Secure Input never reach any tap.
- **Safari:** the front tab's URL and title, only while Safari is the frontmost app,
  only when they change. The URL is cut to `scheme://host[:port]/path`: user:password,
  the query string and the `#fragment` are dropped (they can carry tokens). Non-web pages
  (start page, `about:blank`, `favorites://`, `file://`) are not logged. Titles are capped
  at 300 characters.
- **Pause:** every 5 s the watcher checks `~/.local/share/os-intent/settings.json` (mtime);
  while `"paused": true` it posts **nothing** (observers stay alive, so resuming is instant)
  and logs one line on each transition. Toggle it from `oi-menubar` (Pause logging / Resume).
- **Screenshots (W17):** a new screenshot file in the screenshot folder (`defaults read
  com.apple.screencapture location`, default ~/Desktop) → bucket `oi-watcher-shot_<host>` with its path; an image
  put on the clipboard (public.png / public.tiff only, never content marked concealed/transient) is saved as
  `<data dir>/shots/clip-<stamp>.png` (0600, the 30 newest kept) and that path is posted. settings.json
  `shots.enabled` / `shots.clipboard` turn them off. The clipboard is otherwise only checked for its change count
  and types: text is never read. While logging is paused the clipboard image is never read nor saved and no
  screenshot is posted. **macOS pasteboard privacy:** recent macOS versions may show a "oi-watcher wants to paste
  from …" alert when a background process reads pasteboard data; if it appears (or to avoid it), set
  `"shots": {"clipboard": false}` in settings.json: file screenshots keep working, clipboard ones are not offered.
  `--dry-run --shots-only` (+ `OI_SHOT_DIR`) runs this source alone (tests).
- **Files (oi live's « files » connector):** TCC denies ~/Desktop to `oi live` (a launchd Python), so the watcher lists
  the folders of settings.json `proactive.files.folders` (default ~/Desktop; a Drive folder under ~/Library/CloudStorage
  works) while `proactive.sources.files` is true, every 3 s (names only). A new top-level entry (hidden, `~$`, screenshot
  and partial-download names skipped) → a `live` event with path, class, mtime and size; it ends (`gone`) when removed,
  opened (Spotlight's kMDItemLastUsedDate changed) or pushed out by 20 newer ones. Files already there when it starts
  watching a folder are never events. Each folder's read state (`ok | unreadable | missing`) is written to
  `status/oi-watcher-files.json` (on change and every 30 s) for oi live's health. Never a file's content; log lines are
  counts only. Paused: nothing posted, new files meanwhile are never posted later. A Drive (File Provider) folder may
  need its own macOS grant the first time (amber, unverified).
- **Never:** typed text, clipboard text, screen pixels, other browsers' URLs,
  anything sent beyond 127.0.0.1. The log file holds status lines only, never URLs, titles, chords or file names.

## Amber points (best effort, documented)
- **Space index:** macOS has no public API for the desktop number. The index comes from
  the private CoreGraphics/SkyLight calls `CGSMainConnectionID`, `CGSGetActiveSpace`,
  `CGSCopyManagedDisplaySpaces`, resolved at runtime with `dlsym` (read-only). The index
  is 1-based per display, in Mission Control order (full-screen apps count as spaces).
  If the symbols disappear in a future macOS, events still flow with `space: null,
  delta: 0`.
- **Safari Private windows:** not detectable. Safari's AppleScript dictionary and the
  Accessibility tree expose no "private" property, so private tabs are logged like the
  others (still only on this Mac). If this matters, quit oi-watcher while browsing
  privately: `launchctl bootout gui/$(id -u)/com.osintent.oi-watcher`.
- **Polling:** Safari is read immediately when it comes to the front, then every 2 s.
  With Accessibility granted, a cheap read of the window title decides whether the
  AppleScript runs (only on a title change); without it the AppleScript runs every 2 s
  while Safari is front, and identical results are dropped.

## Install (the user's Mac)
```bash
cd watchers/oi-watcher
./install.sh        # build → ~/Applications/OS-intent/oi-watcher.app (signed), loads com.osintent.oi-watcher
./uninstall.sh      # stops the agent, removes the app, plist and CLI symlink; keeps logs and buckets
```
Since W7 the watcher is a real `.app` bundle (`Contents/MacOS/oi-watcher`,
`Info.plist` with `CFBundleIdentifier com.osintent.oi-watcher`, `LSUIElement`), signed with
the self-signed identity **OS-intent Dev** (`../../scripts/make-signing-identity.sh`,
created once in the login keychain; `install.sh` calls it). The LaunchAgent runs
`~/Applications/OS-intent/oi-watcher.app/Contents/MacOS/oi-watcher`; `~/.local/bin/oi-watcher`
is a symlink to it for CLI use. Log: `~/.local/share/os-intent/logs/oi-watcher.log`.
Build needs only the CommandLineTools Swift toolchain (`swift build`), no Xcode.app.

Why: the signature's designated requirement is now
`identifier "com.osintent.oi-watcher" and certificate root = H"<cert sha1>"`, identical on
every rebuild, so macOS keeps the grants. The old ad-hoc signature's requirement was
`cdhash H"…"`, new on each build, so every reinstall silently dropped Input Monitoring and
Accessibility. A bundle also gets the Input Monitoring prompt, appears in the `+` picker
(under ~/Applications/OS-intent) and works with `tccutil reset <service> com.osintent.oi-watcher`.

## Re-grant permissions
Needed **once** after the first W7 install (the grants made for the old ad-hoc
`~/.local/bin/oi-watcher` do not carry over to the bundle), then never again on rebuilds.
1. **Input Monitoring** (needed for ⌘ chords): on its first start the watcher asks macOS,
   which shows *"oi-watcher" would like to receive keystrokes from any application* →
   **Open System Settings** → switch **oi-watcher** on. If no prompt appears: System
   Settings → Privacy & Security → Input Monitoring → `+` → ~/Applications/OS-intent/
   `oi-watcher.app` → on. If an old `oi-watcher` row is there from before, remove it (`−`).
2. **Accessibility** (optional: the Safari source then queries only on a title change):
   Privacy & Security → Accessibility → `+` → `oi-watcher.app` → on.
3. **Automation → Safari:** bring Safari to the front once; macOS asks "oi-watcher wants
   to control Safari" → **OK** (Privacy & Security → Automation → oi-watcher → Safari).

The watcher picks up a new grant within 30 s (it retries the event tap); no restart needed.
The menu bar app's **Watcher permissions** rows show what the watcher itself sees (from
`~/.local/share/os-intent/status/oi-watcher.json`, rewritten every 30 s: `key_tap`, `ax`,
`safari_automation`, `space_api`, `pid`, `ts`). To start over:
`tccutil reset ListenEvent com.osintent.oi-watcher` (or `Accessibility`, `AppleEvents`),
then `launchctl kickstart -k gui/$(id -u)/com.osintent.oi-watcher`.

Run from a terminal, macOS checks the **terminal's** permissions instead of the binary's,
so `oi-watcher --once` in Terminal can say "granted" while the LaunchAgent is not.
The LaunchAgent log is the truth for the installed agent.

## Commands
```bash
oi-watcher --once         # status of the three sources + aw-server, no prompts, exits
oi-watcher --dry-run      # run, print events to stdout instead of posting
oi-watcher --aw-selftest  # create/post/read/delete a throwaway bucket, exits
```

## Verify (no screen needed)
```bash
H=$(hostname)
for b in space key safari; do
  echo "== oi-watcher-${b}_$H"
  curl -s "http://127.0.0.1:5600/api/0/buckets/oi-watcher-${b}_$H/events?limit=3"; echo
done
tail -n 20 ~/.local/share/os-intent/logs/oi-watcher.log
```
Card pass: after the grants, a Space change, a ⌘C and a Safari navigation each
appear in their bucket within 2 s.

## Layout
- `Sources/OIWatcherCore/` pure logic (chord names, event JSON, bucket ids, URL/title
  handling, space index/delta), tested by `swift test` (swift-testing, no permissions).
- `Sources/oi-watcher/` the daemon: `SpaceSource`, `KeySource` (CGEvent tap),
  `SafariSource` (NSAppleScript + AX), `AWSink` (REST client, bounded retry queue of
  1000 events if aw-server is down), `StatusReporter` (status/oi-watcher.json every 30 s),
  `main.swift` (flags).
- `Support/Info.plist` the base of the bundle's Info.plist (usage strings); `../../scripts/bundle.sh`
  adds the bundle keys and signs.
- `Support/Info.plist` embedded in the binary (`__TEXT,__info_plist`): bundle id
  `com.osintent.oi-watcher` and the Automation usage string shown in the Safari prompt.
- `../../settings/` (local package `OISettings`): the shared settings.json reader, also used
  by `oi-menubar` and `oi-hud`. `Sources/oi-watcher/PauseGate.swift` applies `paused`.
- Swift 6 language mode, macOS 14+.
- `swift test`: with the current CommandLineTools the swift-testing macro plugin sits in a
  subfolder; run `swift test --scratch-path .build-test -Xswiftc -plugin-path -Xswiftc /Library/Developer/CommandLineTools/usr/lib/swift/host/plugins/testing`.
