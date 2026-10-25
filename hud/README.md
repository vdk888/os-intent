# oi-hud: the radial HUD (W4), wish quick action (W6), "Send a vocal to…" (W12), glide (W15), hint dots (W19)

W-numbers are build milestones; later ones override earlier ones (e.g. W21: fn = 1, ⌃ = 2, ⌥ = 3, ⌘ = 4).
W6: the **wish** quick action, see "Wish" below.
W9: options are picked with ONE key, a bare modifier tap (⌃ ⌥ ⌘ fn = options 1-4), the HUD stays
15 s, oi-hud is a signed .app bundle. See "Keys and permissions".
W10: the **summon** key (a bare ⇧ tap opens the HUD with the current options) and **fixed places**
(each option always at its modifier's place: fn left, ⌃ top, ⌥ right, ⌘ bottom; wish 4:30; 7:30 reserved).
W11: a **colour per key** (⌃ blue, ⌥ yellow, ⌘ green, fn pink; settings `hud.colors`), wider capsules
(22 characters, 28-pt icon), **target hints** (the option's target pulses in its colour while the HUD is up, see
"Target hints") and the summon key becomes **⌥+⌘ pressed together** (⇧ is free again).
A pie-menu HUD that pops around the pointer with 1-4 options, picked with a modifier tap (⌥1-⌥4 before W9). **Static options only**: no
prediction, no logging, no action is executed. It exists so the user can judge the *feel* (motion, material, timing).

## Build, test, run

Swift 6.4 from the Command Line Tools is enough (no Xcode.app).

```sh
cd hud
swift build -c release                       # -> .build/release/oi-hud
swift test --scratch-path .build-test -Xswiftc -plugin-path -Xswiftc /Library/Developer/CommandLineTools/usr/lib/swift/host/plugins/testing   # pure logic
# (the -plugin-path is needed with the current CommandLineTools: the TestingMacros plugin moved to plugins/testing/)
.build/release/oi-hud serve &                # daemon (no Dock icon)
.build/release/oi-hud demo                   # 3 option sets, 4 s apart, each reordered after 2 s
.build/release/oi-hud show --options '[{"key":1,"label":"Chrome","icon":"app:/Applications/Google Chrome.app"},{"key":2,"label":"Back","icon":"sf:arrowshape.turn.up.left"}]'
.build/release/oi-hud hide
.build/release/oi-hud state                  # what is drawn + wish hotkey state (debug)
.build/release/oi-hud speech-check           # on-device dictation availability, permission status (never prompts)
./install.sh                                 # signed ~/Applications/OS-intent/oi-hud.app + LaunchAgent com.osintent.oi-hud
                                             # (…/oi-hud.app/Contents/MacOS/oi-hud serve), ~/.local/bin/oi-hud -> bundle
./uninstall.sh
```

`swift test` uses a separate scratch path because, with this toolchain, running the tests in the same `.build`
right after a `-c release` build fails with "plugin for module 'TestingMacros' not found" (a stale-cache issue,
not a code error). The package uses the Swift 5 language mode: the socket threads share file descriptors with the
main thread, and Swift 6 strict concurrency would need actors for a prototype.

## Socket protocol

Unix socket `~/.local/share/os-intent/hud.sock` (dir 0700, socket 0600; `--socket PATH` overrides). One JSON
object per line in each direction. Keep the connection open: the reply to a `show` comes when the HUD goes away.

| Command | Reply on the same connection |
|---|---|
| `{"cmd":"show","options":[{"key":1,"label":"Chrome","icon":"app:/Applications/Safari.app","confidence":0.41}, ...],"ttl_ms":15000}` | `{"event":"pressed","key":n,"via":"key\|click"}`, `{"event":"dismissed","reason":R}` or `{"event":"dropped","reason":"rate_limited\|vocal"}` |
| W19: `{"cmd":"show","mode":"hint","hint_ttl_ms":2500,"ttl_ms":6000,"options":[…]}` | tiny coloured dots only (no keys, taps, clicks or target hints; pointer travel and typing do not hide them), faded after `hint_ttl_ms`; the summon key (tap or hold) while they are up or within `hint_ttl_ms` after develops them into the full cross on this connection (`{"event":"hint","phase":"expanded"}` first, then the usual reply); otherwise `{"event":"dismissed","reason":"hint_ttl"}`; `dropped: visible` over a full cross. `state` gains `hint` ("idle" \| "dots:1,2" \| "faded"). W21b: while drawn the dots follow the pointer (≈ 80 ms lag, kept on screen; Reduce Motion: no easing; a 60 Hz timer only then, none when hidden); a listen-only Esc ends them (`dismissed: esc`, the front app still gets Esc); ⌥⌘ opens the cross at the pointer of that moment, where it stays. |
| W24: `{"cmd":"show","mode":"hint","marker":"ring",…}` | a PROACTIVE hint (the state changed: mail, agent, file, cockpit…): the same dots with a thin ring around them; everything else as the W19 hint. An unknown marker = a plain hint; a full show ignores it. `state` `hint` = "dots:1,2:ring". |
| `{"cmd":"hide"}` | `{"event":"ok"}` (the visible show's owner gets `dismissed`/`hide_cmd`) |
| `{"cmd":"state"}` | `{"event":"state","visible":b,"options":[{"key":k,"label":l},...],"hud_enabled":b,"wish_hotkey":"⌥\`","wish_registered":b,"wish_panel":"closed\|typing\|dictating"}` |
| `{"cmd":"wish","text":"...","source":"typed\|dictated"}` | saves a wish without the panel (scripts/tests): `{"event":"wish","text_len":n}` |
| (no command: any wish saved) | `{"event":"wish","text_len":n}` broadcast to **every** connected client (never the text) |
| `{"cmd":"debug_summon"}` (`oi-hud debug-summon`) | runs the summon path as the summon tap would; `{"event":"ok"}` |
| (no command: summon key tapped) | `{"event":"summon","ts":"…"}` broadcast to **every** connected client (W10) |
| `{"cmd":"debug_vocal"}` (`oi-hud debug-vocal`) | W12: the vocal key without a key (picker / next page); `{"event":"ok"}` |
| `{"cmd":"debug_vocal","pick":"nova","text":"…"}` (`oi-hud debug-vocal --pick nova --text …`) | W12: picks that agent and sends `text` as the transcript (no microphone; serve with `OI_VOCAL_DRY_RUN=1` to send nothing); `{"event":"ok"}` |
| `{"cmd":"vocal_to","pick":"orion"}` | W22: a starter "Ask <agent>" option was pressed: the recording panel addressed to that agent (real microphone; Enter / the vocal key sends, Esc cancels; ignored while recording); `{"event":"ok"}`, unknown slug = `error` |
| `{"cmd":"update","options":[{"key":1,"label":"Reply to Sam","expect":"Reply"}]}` | W14 label enrichment: applied only while option `key` still shows `expect`; `{"event":"ok"}`, else `dropped`: `stale` / `not_visible` |
| `{"cmd":"result","key":1,"ok":true,"text":"…","ttl_ms":8000}` | W14: the outcome of a Claude-assisted press: `text` (≤ 4 lines) as a card for `ttl_ms`, no text = hide; `{"event":"ok"}` when the HUD still waited, `{"event":"dropped","reason":"cancelled"}` after Esc (the driver then does nothing) |
| `{"cmd":"debug_press","key":1,"mode":"claude"}` (`oi-hud debug-press --key 1 [--claude]`) | W14: presses option `key` as its key would (the long-press variant with `--claude`); `{"event":"ok"}` |
| `{"cmd":"subring","path":[2],"options":[{"key":1,"label":"Reply to Sam","icon":"sf:sparkles","action":"draft:mail.reply","claude":true}, ...]}` | W15: the follow-ups (0-4; 0 = a leaf) of the option at `path` (1-3 keys), the answer to an `expand` event during a glide; `{"event":"ok"}`, else `dropped`: `stale` (not the pending expansion, the glide ended). an option may carry `"card":{key, what, summary?, why?}` (a live item: an agent waiting, an unread mail), drawn beside it while hovered (level 2, level 3 after its dwell); a malformed card is ignored |
| `{"cmd":"context","cards":[{"key":2,"what":"Sam · Re: devis","summary":["…"],"excerpt":"…","why":{"page":"…","snippet":"…"}}]}` | the context cards of the top options (1-4, merged by key), drawn beside the direction while gliding (level 2 / 3, see "Glide"); `{"event":"ok"}`, else `dropped: not_visible` |
| `{"cmd":"debug_glide","path":[2,1],"cancel":false}` (`oi-hud debug-glide --path 2,1 [--cancel]`) | W15: a glide without keys: hold, rest on `2` until its sub-ring opens, then on `2.1`, release (= execute; `--cancel`: release on empty space); `{"event":"ok"}` at once, the outcome goes to the show's owner |
| anything invalid | `{"event":"error","message":...}` |

- `options`: 0-4 items (W10: 0 = the wish option alone), `key` 1-4 and unique; `key` picks the hotkey (tap or
  chord) and, through it, the FIXED place (settings.json `hud.slots`; an absent key leaves its place empty). `icon` is `app:<path>`
  (real app icon via `NSWorkspace.icon(forFile:)`) or `sf:<SF Symbol name>`; `confidence` is accepted, not shown.
  `hint` (W11, optional): `{"kind":"window","app":"Telegram"}` or `{"kind":"edge","dir":"right|left"}`; a missing,
  unknown or malformed hint is ignored (never fails the show). `label` is fitted to 22 characters on screen
  ("Open notion.so/…", never a mid-word ellipsis except for a single over-long word).
- `state` (W11) also carries `"hints":[{"key":k,"drawn":"window|dock|halo|edge"}]` (empty whenever hidden) and
  `summon_key` ("⌥⌘" by default).
- `ttl_ms`: default 15000 (100-60000; the timer never fires before 1.5 s). `oi live` sends `settings.json`
  `gate.ttl_ms` (default 15000) with every show; `oi-hud show` without `--ttl-ms` uses the same setting.
- Dismiss reasons: `ttl`, `esc`, `typing`, `pointer_fast`, `replaced` (a newer show took over the visible HUD:
  the content swaps in place and reorders with a spring), `hide_cmd`, `wish` (the wish option/hotkey was used).
- `{"event":"dropped","reason":"wish_open"}`: a show while the wish panel is open (it is never covered).
- The socket is `<data dir>/hud.sock` (`$OI_DATA_DIR` respected); a path over 103 bytes is refused cleanly
  (use `--socket` with a short or relative path).
- Rate limit: at most one show per 2 s (`oi-hud serve --min-interval-ms N`); replacements count too.

## Behaviour

- Window: `NSPanel` `[.borderless, .nonactivatingPanel, .hudWindow]`, level `.floating`, `canJoinAllSpaces +
  fullScreenAuxiliary + transient`, can never become key or main, so the front app keeps focus and typing.
  Clicks pass through (`ignoresMouseEvents`) except while the pointer is over an option capsule (a click = press).
- Layout: a ring around the pointer, slot 1 at 12 o'clock, the rest clockwise (3 options: 0/120/240°,
  4 options: 0/90/180/270°). The pointer sits in the empty middle, so the click target is never covered.
- Follow: the HUD stays put while the pointer is within reach of the options, and is pulled along on a soft leash
  (a damped step each frame) when the pointer drifts slowly beyond the ring; it stays on screen at the edges.
- Motion: appear/disappear = 150 ms fade + scale; reorder = `.spring(response: 0.3, dampingFraction: 0.8)` with
  stable identities (`matchedGeometryEffect`), so a capsule *moves* to its new slot.
- Material: each capsule has an `NSVisualEffectView` `.hudWindow` blur, a hairline and a soft shadow.
- Accessibility: Reduce Motion -> no scale, reorder becomes a crossfade; Increase Contrast -> opaque black capsules
  with a white border. Both are read on each show.
- Clicks (the user 20/10 16:5x): every capsule (options, wish, vocal, picker agents) is a click target. The panel
  stays non-activating (never key, `acceptsFirstMouse`: oi-hud never comes to the front) and takes mouse events
  only over a capsule's own stadium shape (`capsuleHit`, re-tested at 60 Hz); everywhere else clicks reach the app
  below. Hover = a stronger wash and border in the key's colour. A click is the same path as the key: the reply is
  `{"event":"pressed","key":k,"via":"click"}` (taps and chords say `"via":"key"`; `oi live` keeps `via` on the
  hud-log row).
- Capsule width follows the content (the user 16:40, "less text, more symbols"): icon 20 + label + badge + padding,
  72-200 px, centred on its fixed place (side capsules keep their inner edge 16 px from the centre). The wish
  capsule shows its speech-balloon icon + ⇧ only, the vocal capsule its mic + ⌃⌥ only.
- Hides on: ttl (`gate.ttl_ms`, 15 s), Esc, a tap / hotkey (pressed), any other key press (typing), and fast
  pointer travel (never while the pointer is inside the cross's box + 24 px: reaching for a capsule): never in the first `gate.pointer_grace_ms` (2000) after a show, then only when the pointer
  moves faster than `gate.pointer_fast_px_s` (1200 px/s) for `gate.pointer_fast_ms` (250 ms), sampled at 60 Hz
  only while visible; slow or short moves never hide it. `gate.hide_on_pointer_travel: false` turns it off
  (default true). All read on each show. (W4-W8: 3 s ttl and 600 px/s for 100 ms from the first frame.)

## Keys and permissions

- **Modifier taps (W9, defaults).** While the HUD is visible, a TAP of ⌃ / ⌥ / ⌘ / fn picks option
  1 / 2 / 3 / 4: one key, nothing else. A tap = that modifier alone goes down and back up within 350 ms with no
  key press, no click and no other modifier in between; ⌘C, ⌥`, ⌘-click, ⌃⌥… are chords, never taps, and are
  never consumed (they reach the front app as usual). The capsule badges show ⌃ ⌥ ⌘ fn.
  How: a **listen-only** session `CGEventTap` on flagsChanged + keyDown + mouse-down + scroll, created on show and removed
  on every hide (hidden = no tap at all). The callback reads the event type and the modifier flags only, never
  a key code or character; nothing is written except the resulting HUD choice ("tap -> option 1" in the log;
  the hide line ends with "(taps OFF)" and `oi-hud state` shows `taps_active`).
  Needs **Input Monitoring for oi-hud.app**. Without it oi-hud logs one line and the tap slots fall back to the
  Carbon hotkeys ⌥1-⌥4 (badges then show ⌥1…), skipping any ⌥n already used by another action.
- **Summon (W10, W11).** ⌥ and ⌘ pressed together and released, no other key, ≤ 500 ms from the
  first down to the last up (settings `hotkeys.summon`, default `{"tap":["option","cmd"]}`; the single form
  `{"tap":"shift"}` is still accepted; a W10 file whose summon is ⇧ reads as the new default; editable in
  oi-menubar Hotkeys…: "⌥⌘ tap | ⇧ tap | record tap | chord", record tap captures any set pressed together)
  opens the HUD with the current best options. A PERMANENT listen-only tap (same `TapDetector` as the option
  taps, its own instance) exists while the daemon runs. The detector returns the SET of modifiers of one
  episode: ⌥ alone = {⌥}, ⌥ then ⌘ one after the other = {⌥} then {⌘}, ⌥⌘C / ⌥⌘-click / ⌥⌘+scroll = no tap.
  The option taps only take one-modifier sets, so a ⌥+⌘ tap never fires option 2 or 3; while the HUD is up a
  summon is ignored (consumed without effect). Chords pass through untouched. Ignored when `hud_enabled` is false, `paused` is true, the HUD is up or the wish
  panel is open. On a summon oi-hud broadcasts `summon`; `oi live` answers with a show (skips the rate limit);
  with no show within 400 ms the HUD shows the wish option alone; a live show arriving later replaces that
  fallback in place without the rate limit (W11). Key summons are not logged. Without Input
  Monitoring: one log line, retried every 60 s. `oi-hud state` shows `summon_key` / `summon_armed`.
- **fn:** the fn/🌐 key arrives as a flagsChanged event with the SecondaryFn flag (kVK_Function), like the other
  modifiers. macOS ALSO runs its own "Press 🌐 key to" action on an fn tap (System Settings → Keyboard; on this
  Mac the setting is unset, i.e. the system default: change input source / emoji picker). Set it to
  **"Do Nothing"** if option 4 by fn should not also trigger that. (Not verified by a physical key press while
  building: the builder does not press keys; the log line "tap fn -> option 4" confirms it on first use.)
- The combos come from `settings.json` `hotkeys` (read on each show; edit them in `oi-menubar` → Hotkeys…).
  An option is either `{"tap": "ctrl|option|cmd|fn"}` or a chord `{"modifiers": [...], "key": "1"}`; dismiss
  and wish are chords; `wish_visible` (W11) is a single tap. Defaults: ⌃ ⌥ ⌘ fn taps, Esc, ⌥` (wish), ⇧ tap
  (wish while the HUD is up), ⌥+⌘ tap (summon). A file without `hotkeys.v` whose options are
  exactly ⌥1-⌥4 (what oi-menubar W5-W8 wrote on every save) is read as the new tap defaults. An invalid set
  (unknown key, collision, typing key without modifier, tap on dismiss/wish) falls back to the defaults as a
  whole and logs one line.
- `settings.json` `"hud_enabled": false` → every `show` is answered `{"event":"dropped","reason":"disabled"}`
  and nothing is drawn (checked before the rate limit).
- Chords are Carbon `RegisterEventHotKey` hotkeys, registered on show and unregistered on hide (never
  active otherwise). They need **no permission** and consume the key, so it does not also type into the app.
  Hotkeys use physical key positions: on a French AZERTY keyboard ⌥1 is ⌥ + the `&/1` key, no Shift needed.
  While the HUD is up, Esc goes to the HUD, not the front app.
- "Hide on typing" uses `NSEvent.addGlobalMonitorForEvents(.keyDown)`, which needs **Accessibility** for
  oi-hud.app (optional). Without it the daemon logs one line at start and the HUD still hides on ttl, Esc,
  taps/hotkeys and fast pointer travel.
- Permissions for oi-hud.app (signed with the stable "OS-intent Dev" identity, so grants survive rebuilds):

  | Permission | For | When asked |
  |---|---|---|
  | Input Monitoring | modifier taps | the first HUD show after install (one prompt; grant, no restart needed: the next show retries) |
  | Speech Recognition, Microphone | wish dictation, W12 vocal notes | first mic press or vocal recording (usage strings in Support/Info.plist = the bundle's Info.plist) |
  | Accessibility (optional) | hide on typing | never prompted; add oi-hud.app with "+" if wanted, then Restart HUD |

  Reset one: `tccutil reset ListenEvent com.osintent.oi-hud`. Nothing else: no Screen Recording, no network.

## Wish (W6): "tell me what you wanted", at any moment

The user: "always have the option to open dictation or writing to write what I would have liked, as a
quick action option at any point".

- **One key while the HUD is up (W11).** A bare ⇧ tap while the HUD is visible opens the wish panel
  (`hotkeys.wish_visible`, default `{"tap":"shift"}`; the wish capsule's badge shows ⇧). It is part of the
  per-show listen-only tap (only while visible; ⇧A, ⇧-click, ⇧-scroll are chords); the log says "tap -> wish".
  Without Input Monitoring the badge shows the ⌥` chord instead.
- **The wish hotkey is always on.** `settings.json` `hotkeys.wish` (default ⌥`) is registered for as long as
  `oi-hud serve` runs — also when `hud_enabled` is false and when logging is `paused`: the wish is the one
  action that is always available. (With `hud_enabled` false the radial HUD never appears; the wish panel
  still opens on the hotkey.) The combo is re-read from settings.json every 2 s.
- **Panel:** a small floating panel near the pointer: one line "What did you want here?", a mic button, and
  "Enter saves · Esc cancels". Enter saves, Esc or a click outside (or ⌘-Tab away) cancels; an empty line is a
  cancel. It takes keyboard focus as a non-activating panel (`.nonactivatingPanel` + `makeKeyAndOrderFront`,
  `canBecomeKey`), so the front app stays front; only if macOS refuses key status does oi-hud activate itself
  (and hides again on close to hand focus back). Same 150 ms fade + scale as the HUD; Reduce Motion = fade only.
- **In the HUD:** every show gets a last, permanent option **"Tell me what you wanted"** below the ring, with the
  wish hotkey as its badge (≤ 4 predicted options + this one). Clicking it or pressing the wish hotkey while the
  HUD is up closes the HUD (owner gets `dismissed`/`wish`) and opens the panel with the shown labels recorded.
- **Dictation (on-device only):** the mic button, or a second press of the wish hotkey while the panel is open,
  starts the Speech framework with `requiresOnDeviceRecognition = true` (audio never leaves the Mac), locale
  `settings.json` `wish.locale` (default `fr-FR`, fallback `en-US` when that locale has no on-device model).
  Partial results fill the field; it stops on Enter, Esc, a mic/hotkey press or after 20 s. Checked 2026-10-20
  on the M4: fr-FR and en-US both on-device. Denied/unavailable → a one-line hint in the field, typing still works.
- **Saved to** `~/.local/share/os-intent/wishes/YYYY-MM-DD.jsonl` (UTC day; dir 0700, file 0600), one row:
  `{"ts": ISO UTC, "text": str, "source": "typed|dictated", "context": {"app": front app, "space": index|null,
  "hud_visible": bool, "hud_options": [labels shown if the HUD was visible, else []]}}`. The logs say
  "wish saved (typed, 23 chars)", never the text; connected socket clients get `{"event":"wish","text_len":n}`.
  `oi tokens` turns each row into a `wish:<slug30>` token (see docs/DESIGN.md).
- **Tests without the keyboard:** `oi-hud --debug-wish "<text>"` writes one row directly (no daemon needed);
  `oi-hud serve --debug-open-wish [--debug-delay-ms N]` opens the panel once, as the hotkey would.

### Permissions for the wish

| Permission | When | Who asks |
|---|---|---|
| Speech Recognition | first mic press | oi-hud.app (usage string in its Info.plist) |
| Microphone | first mic press | oi-hud |

Nothing for typing. Dictation runs only when oi-hud is started by launchd (the LaunchAgent): started from a
terminal, macOS attributes the prompt to the terminal app, which may lack the usage strings and gets the process
killed instead of prompting; set `OI_HUD_ALLOW_DICTATION=1` to force it there.

## Deliberately static

No prediction, no Laya/Markov call, no candidates, no execution of the chosen action, no logging of what was
shown or pressed (the `hud:shown` / `hud:pressed:<k>` / `hud:dismissed` tokens of the plan come in batch 3).
The caller decides what to show; `oi-hud` only shows it and reports the outcome. Since W8 that caller is
`oi live` (docs/DESIGN.md "Live driver"): it picks the options, executes the press and writes the hud-log.

## Colours and target hints (W11)

- **Colour per key.** Each option takes the colour of the modifier that owns its fixed place (settings.json
  `hud.colors`, e.g. `{"ctrl": {"light": "#4F86E8", "dark": "#9CC3FF"}, "option": "#FFE08A", …}`; a bare
  "#RRGGBB" sets both variants; a missing or bad entry keeps its default). Defaults (pastel): ⌃ blue, ⌥ yellow,
  ⌘ green, fn pink. The capsule gets a light wash, a coloured rim and a coloured badge (dark variant: the HUD is
  always dark). Increase Contrast: black capsule, solid 2-pt rim in the colour. The colour is stable per key.
- **Target hints.** `oi live` sends a `hint` per option: app.open → `window` of that app, url.open → `window` of
  Safari, space.move → `edge` right (to a higher desk) or left. While the HUD is visible, oi-hud draws, in the
  option's colour (light/dark variant by the system appearance), on a transparent click-through
  non-activating overlay panel per screen (`ignoresMouseEvents`, canJoinAllSpaces + fullScreenAuxiliary, level
  just below the HUD):
  - `window`: a soft rounded outline + glow pulsing (1.2 s period, low alpha) around the app's frontmost
    on-screen window on this desk — `CGWindowListCopyWindowInfo(.optionOnScreenOnly)`, owner pid + bounds,
    layer 0; window titles are never read, no permission needed;
  - not on this desk → `dock`: the same glow on the app's Dock icon, read with Accessibility (read-only AX on
    the Dock process: AXPosition/AXSize of the item titled with the app name), only when oi-hud is trusted;
    the panel then sits just above the Dock;
  - neither → `halo`: a ring pulsing around the option's own icon in the HUD;
  - `edge`: a soft vertical bar on the pointer screen's edge, spring bounce (3 decaying cycles, 1.3 s) then a
    slow breathing pulse.
  Reduce Motion: static outline / bar / ring. All hints are drawn at once. Core Animation only; `finish()`
  (the single exit) removes every layer and orders the overlay panels out, so nothing is drawn or animated
  (no CPU) while the HUD is hidden. The log says `hints: 1=window 2=edge …`; `oi live` writes the hint kind of
  each shown option in hud-log (`"hint": "window" | "edge:right" | …`).

## Send a vocal to… (W12)

The user 20/10 15:45 Paris: "one always the same: send a vocal to, and when i press it i have to choose one of you guys".

- **Option.** Always drawn at 7:30 (below-left, the mirror of the wish at 4:30) while the HUD is up: mic icon + its
  key, neutral colour. Key = `hotkeys.vocal`, default **⌃ and ⌥ pressed together, then released** (the W11 set-tap
  code, matched on the per-show tap monitor); a click works too.
- **Why not caps lock (the card's proposal).** Every single modifier is taken (⌃ ⌥ ⌘ fn options, ⇧ wish), so caps
  lock was the candidate, but it could not be made reliable: Apple keyboards ignore a short caps-lock press by
  design (the caps-lock delay, so a quick tap often produces no event at all), restoring the lock state means
  writing the HID lock state (`IOHIDSetModifierLockState`) on every press, racing the LED and the key repeat, and
  the passive monitors deliberately never read a key code. It cannot be verified without a physical press either.
  So the default is the ⌃+⌥ set tap; a chord can be set in Hotkeys… (e.g. ⌃⌥V).
- **Picker.** Press → the HUD turns into the agent picker in the SAME fixed places, each capsule = the agent's
  initial (a disc in the key's colour) + first name: fn Atlas (left), ⌃ Nova (top), ⌥ Orion (right), ⌘ Lyra (bottom);
  the vocal key again → Vega, Juno, Iris, Hera; again → Echo; again → page 1. A live show on screen is
  answered `dismissed: vocal`; new shows are `dropped: vocal` while the picker or the recording is up. Esc, ttl,
  typing or fast travel away cancel.
- **Recording.** The agent's key (or a click) → a panel at the pointer: "To Nova" in the key's colour, the live
  transcript (on-device Speech, `wish.locale` then en-US, never the cloud), "⌃⌥ or Enter sends · Esc cancels · 5 min
  max". The vocal key (armed by a dedicated listen-only tap while recording), Enter or the time limit sends; the last
  15 s show "sends in N s" in orange instead of "listening…". The recogniser's own ~1 min task limit is not a
  limit: W20 segments restart it on the same audio (HUDCore.DictationSegments). Esc
  cancels (the audio is deleted). The audio is written to `vocals/<UTC ts>-<slug>.caf` (dir 0700, file 0600) and
  the transcript to `.txt`; an empty transcript sends nothing ("Nothing heard — not sent", audio deleted).
- **Typing instead**. The
  transcript is a real text field (VoiceOver: "Message à Nova"). The indicator says « Écoute… » while listening and
  « Saisie » once typing (en locale: listening… / typing; the language follows `wish.locale`). The first key that
  WRITES something stops the microphone for good (one way: no going back to listening), keeps the words already
  transcribed, focuses the field with the caret at the end and types that key there; from then on he types / edits
  freely, Enter or the vocal key sends, Esc cancels, and the time limit no longer applies. Typing before speaking
  works the same (the field starts empty; no words heard = the audio is dropped). Dictation that cannot start
  (permission, no model) now turns the panel into the text field instead of sending nothing. The key rule
  (`HUDCore.VocalKeyRule`): Return / keypad Enter = send and Esc = cancel in both modes; while listening a printable
  character, a dead key (^), Delete / forward Delete or an arrow starts typing, while a ⌘ / ⌃ chord, Tab or an
  F-key does nothing; while typing every other key goes to the field. **C is a letter in this panel**: the
  screenshot toggle « Joindre une capture de l'écran (C) » belongs to the outward gate's confirm panel, which opens
  only after Enter, when this panel is closed, so typing a c can never toggle it. A typed vocal is sent with
  `--typed`: the line then starts "[Message typed by the user, …]" (hud-log row `"typed": true`); `oi-hud state` shows
  `vocal: "typing:<slug>"`.
- **Delivery.** oi-hud runs `<repo_dir>/projects/os-intent/.venv/bin/oi vocal-send --to <slug> --text-file <txt>
  --audio <caf> --app <front app> --desk <k>` off the main thread, with TELEGRAM_STATE_DIR / TELEGRAM_BOT_TOKEN /
  GH_TOKEN / GITHUB_TOKEN removed (`OI_VOCAL_BIN` overrides the binary, `OI_VOCAL_DRY_RUN=1` adds `--dry-run`).
- **Screenshot offer**. The outward gate's panel of that vocal also shows "Joindre une capture
  de l'écran (C)", OFF by default; C or a click toggles it. It is offered only when python finds a capture safe
  (`oi.vocal.shot_offer_ok`: `vocal.shot_offer` and `shots.enabled` on, not paused, a fresh oi live context, no
  denylisted front app, no `claude.local_only_domains` host, not a browser without a known page = a possible
  private window). Validated with it on → `oi vocal-send` takes ONE `screencapture -x` of the display under the
  pointer (`vocals/<stem>.png`, 0600) and names it in the same line as the vocal. Never in a dry run.
  The line: `[Vocal from the user, <Paris HH:MM>, context: <app> · <host> · desk <k>] <transcript> (audio: <path>)`,
  the host from `oi live`'s status (`context`, when fresh); never a title or URL path. Recipients:

  | slug | inbox |
  |---|---|
  | atlas | local inject `~/.claude/channels/atlas/inject` |
  | juno | local inject `~/.claude/channels/juno/inject` |
  | nova, orion, lyra, iris | relay command: `<relay> --to agent-host --state-dir /opt/agents/<slug>/inbox --from atlas` |
  | vega | relay command: `<relay> --to studio-host --state-dir /opt/agents/vega/inbox --from atlas` |
  | hera, echo | relay command: `<relay> --to laptop-host --state-dir /opt/agents/<slug>/inbox --from atlas` |

  (example table.) `<relay>` is a command you provide (`script` per recipient, default `$OI_RELAY_CMD` or
  `~/.local/bin/agent-send`; a `.py` runs with python3): it receives the line on stdin and delivers it however you
  like (ssh to the host and append to the agent's inbox, a signed relay, a queue).

  Overridable per slug with settings.json `vocal_recipients` (docs/DESIGN.md "Settings"). The message is on the sender's
  stdin (local: appended as one line to an EXISTING inject file), never in argv.
- **Result.** A toast at the pointer in the agent's colour: "Sent to Nova" / "Failed: <reason>"; a hud-log row
  `{"kind":"vocal","to","ok","dry_run","text_len","audio","error"}` (never the text) and, when sent, the token
  `vocal:<slug>`.
- **Permissions.** Speech Recognition + Microphone (the wish's, asked once at the first recording; the LaunchAgent
  only, as for the wish), Input Monitoring for the taps (already granted for W9-W11). The relay command brings its own
  transport and credentials; OS-intent passes none.
- **Demo roster** (`oi demo`): `OI_HUD_AGENTS="atlas:Atlas,nova:Nova,…"` (slug:Name, 1-12 agents) replaces the
  picker's roster for that oi-hud process, so a screen recording shows fictional agents; absent or invalid = the
  fleet roster. `state` carries `"agents":[names]` (the demo refuses a HUD that would show the real ones).
- **Test without a key or a microphone:** `OI_VOCAL_DRY_RUN=1 oi-hud serve --socket /tmp/x.sock`, then
  `oi-hud debug-vocal --socket /tmp/x.sock` (picker, again = next page) and
  `oi-hud debug-vocal --socket /tmp/x.sock --pick nova --text "…"` (panel, then the dry-run toast);
  `oi vocal-send --dry-run --to nova --text-file f` prints the exact command and line.

## Claude-assisted options (W14)

The user: "if i press 'respond to email' option, for longer, it also writes the response with context in mind".
- An option sent with `"claude": true` has a Claude-assisted variant: HOLD its modifier alone ≥ `hud.long_press_ms`
  (settings.json, default 600, clamped 400-3000) and release, or right-click / ⌥-click its capsule. A ring fills
  around the capsule while the modifier is held (drawn after 150 ms, so a normal tap never flashes it). A short tap
  (≤ 350 ms) stays the plain action; between the two nothing fires; a second modifier, a key, a click or a scroll
  cancels the hold.
- The reply is `{"event":"pressed","key":k,"via":"key|click","mode":"claude"}`. The HUD then stays with a spinner on
  that capsule (the others dimmed): ttl paused, option keys disarmed, typing / pointer travel ignored; only Esc (or
  a 45 s safety net) hides it until the driver's `result`: a card (≤ 4 lines, 64 characters each) for its ttl, or
  nothing (hide). A show arriving meanwhile is `dropped: claude`.
- `state` carries `"claude":"idle|working:<k>|card:<k>"`. Logged: "claude: option k", "result card (ok, n lines)",
  never the text.
- The python side (prompts, the headless subscription call, Mail AppleScript, privacy gate) is `oi/claude_actions.py`.

## Glide: hold ⌥⌘, rest, chain, release (W15)

The user: "when i hold option + cmd ... i can move the mouse pointer to an option, and if i stay it triggers
context enrichment with claude, so i can chain clicks in relevant context, low distance". A marking menu:
- TAP ⌥⌘ (released < 250 ms) = the W11 summon, unchanged. HOLD ⌥⌘ ≥ 250 ms (nothing else pressed before) = the
  glide: the summon is answered as usual but the cross stays FIXED at the press point (no leash), with no ttl and no
  pointer-travel hide while the keys are held. Only a set of two or more modifiers can be held (a ⇧ summon cannot).
- Glide onto a capsule: it highlights. Rest `hud.dwell_ms` on it (settings.json, default 500, clamped 250-2000):
  the HUD sends the show's owner `{"event":"expand","path":[k]}`; a thin arc runs round the capsule while the driver
  (Claude) works (after 150 ms). The answer `subring` blooms 2-4 follow-ups around THAT capsule (spring), as close as
  they fit, fanned outward from the cross, never over the cross, its centre or another ring, clamped to the screen,
  in lighter tints of the parent's colour. Rest on a follow-up = the next level (`path` [k, j]), up to depth 3; a
  sibling's ring closes the deeper ones. An unanswered expansion is given up after 9 s.
- Release ⌥⌘ over any capsule (any level) = execute it: `{"event":"pressed","key":k,"path":[k,j],"via":"glide",
  "glide":{…}}` (a follow-up marked `claude` goes through the W14 spinner + result card, `"mode":"claude"`). Release
  on empty space, Esc or any key = cancel (`dismissed`, reason `glide_cancel` / `glide_esc` / `esc`). A short hold
  (≤ 500 ms) that touched nothing leaves the HUD up exactly as a tap. A click still works (any capsule, any level).
  Nothing ever runs on hover alone.
- `glide` = {visited: [{path, ms}], expanded, final, depth}: paths and times only. `state` carries
  `"glide":"idle|gliding:<path>;rings:<n>"`. The panel stays non-activating and click-through outside the capsules
  (the window is 1200x900 around the cross for the rings; what is kept on screen is still the 600x400 cross).
- To the owner at the hold start: `{"event":"glide","phase":"start"}` (the driver prefetches level 1). The driver side
  (one batched haiku prefetch, cache, follow-up mapping, privacy, logging) is `oi/glide.py`.
- Context cards: `{"cmd":"context","cards":[{"key":2,"what":"Sam · Re: devis","summary":[…],
  "excerpt":"…","why":{"page":"…/acme.md","snippet":"…"},"pending":true}]}` (1-4 cards, merged by key, reset by
  each show; `dropped: not_visible` when no HUD). While gliding, the card of the direction the pointer is on appears
  beside its capsule once the dwell completes (level 2: the « what », one line); one level deeper it grows (level 3:
  summary, quoted excerpt, the wiki page + snippet, "Preparing the summary…" while `pending`). Placed clear of the
  cross and the sub-rings (HUDCore `ContextCardLayout`), never a click target; `state` adds `;card:<key>.<level>`.

## Wish → action (W18)
The wish panel's text is resolved to an action by `oi live` (oi/intent_resolve.py) and executed. `oi live`
registers on its persistent connection with `{"cmd":"resolver"}` (answered `ok`); the wish text is sent to that
connection ONLY, never broadcast.
- While typing (or dictating), 150 ms after the last change: `{"event":"resolve","id":n,"text":"…","final":false,
  "confirmed":false,"context":{"app":"Mail","space":2}}` → `{"cmd":"resolution","id":n,"state":"preview",
  "label":"→ Telegram"}`, drawn right-aligned on the hint row (local tier only, a few ms; " ?" = a guess).
- Enter: the same request with `"final":true` → the panel says "Working…" and waits (≤ 45 s, Esc closes it):
  `done` (✓ label, then the wish is saved WITH `"action":{"token","tier","target"}` and the panel closes; a summary
  shows its lines for 6 s), `noted` (saved as before, no action), `failed` (the reason, saved as before),
  `confirm` (an action neither gated nor known safe: the hint shows e.g. "… ? ⇧ again"; a ⇧ tap or Enter on the
  same text sends `"confirmed":true` with the same id; editing the text drops the confirm). Sends and page presses
  never get it: they run on Enter and the outward gate's panel is their one confirmation.
- No resolver connected (oi live stopped): Enter saves the wish exactly as in W6.
- `{"cmd":"debug_wish","text":"telegram","app":"Mail","enter":true,"confirm":false}`: opens the panel (front app
  overridable), types the text, presses Enter (and ⇧ when the answer is a confirm) — tests and demo.
- Esc / click outside while "Working…" or confirming: `{"event":"cancel","id":n}` to the resolver, which then never
  executes that request (W18 follow-up).

## Page actions (W16)
The HUD can propose clicking the key button of the current Safari page (GitHub "Squash and merge", …). Full
contract: docs/DESIGN.md "Page actions".
- **Permission:** Accessibility for **oi-hud.app** (System Settings → Privacy & Security → Accessibility). Without
  it `page` answers `trusted: false`, no page action is ever offered, and oi-menubar shows the hint row. oi-hud
  writes `status/oi-hud.json` {ts, ax, pid} every 30 s for that row. (Learned clicks need Accessibility for
  oi-watcher, which it already has.)
- **Socket** (handled off the main thread, outside the HUDCommand decoder): `{"cmd":"page"}` →
  `{"event":"page",…}`; `{"cmd":"ax_press","role","slug","host","dry_run"}` → `{"event":"ax_press","ok","reason",
  "dry_run"}`. Without `dry_run`, a press needs the single-use ticket of the user's own tap on the option whose `target`
  matches (≤ 5 s; an irreversible label needs the confirmed ticket a validated outward-gate panel issues).
- **Options:** one tap picks (no two-tap since the outward gate: the panel is the one confirmation); `target`
  "button:<slug>"; hint `{"kind":"element","rect":[x,y,w,h]}` (CG global) = outline around the button.
- **CLI:** `oi-hud page` (the daemon's view), `oi-hud page-local` (this process's own grant, e.g. from Terminal),
  `oi-hud ax-press --role button --label "Squash and merge" [--dry-run]`.
## Screenshot offer (W17)

`oi live` offers each new screenshot (docs/DESIGN.md "Screenshot → current contact"); the HUD side is three commands:

- `{"cmd":"show",…,"live":"shot"}` = a show bound to a live context state (the screenshot still on screen):
  never rate-limited, and typing / fast pointer travel do NOT dismiss it; Esc,
  a press, the ttl and `hide` (oi live: the state ended) do (`LiveBound`). `dropped: note_open` while the note panel
  is up. `{"cmd":"shot_show","options":[…],"ttl_ms":15000}` (the agent picker after "→ other") = the same with
  `"live":"shot"`.
- `{"cmd":"shot_note","title":"→ Atlas · Claude note","text":"…","placeholder":"…"}`: the note panel (wish look,
  multi-line, prefilled, takes focus without activating). Enter → `{"event":"note","text":"…"}`; ⇧/⌥-Enter = new
  line; Esc / click outside → `{"event":"dismissed","reason":"esc|click_outside"}`; a new `shot_note` replaces it
  (`replaced`). A visible HUD is dismissed (`shot_note`). Logged: lengths only, never the text.
- `{"cmd":"debug_note","text":"…"}` (tests): submit the open panel as Enter would (`text` absent = keep it).

The tier-2 long-press uses the W14 path unchanged (`pressed` mode claude → spinner → `result` with no text = hide),
then `shot_note` with the Claude note.

## Outward gate: validate before anything leaves the Mac

Every send (vocal / "Ask Atlas" / a glide send / W25 hand-over / wish "send to X", screenshot delivery), every page
press not known read-only (merge, approve, an unknown learned label …) and every W24 `outward` option (reply /
forward / draft) waits for `oi/gate.py`'s confirm panel, the ONE confirmation (pick → panel → validate):

- `{"cmd":"confirm_outward","id":"…","cls":"vocal|shot|page|reply","title":"Envoyer ce message à Orion ?","preview":["…"],
  "ttl_ms":20000,"lang":"fr|en","action_key":"1-4|vocal","target":"button:squash-and-merge"}`: a small Liquid Glass
  panel at the pointer (title, 1-3 preview lines, ✓ / ✗, hint). Answered on the same connection by exactly one
  `{"event":"outward","id":"…","decision":"validated|cancelled","reason":"…"}`. Validate: Return / keypad Enter, a
  clean tap of the action key (option key 1-4 or the vocal key, from `hotkeys`), a click on ✓ (inputs in the first
  0.3 s are ignored). Cancel: Esc, ✗, a click outside, the timeout (`ttl_ms`, 1-60 s), a newer request (`replaced`).
  A validated `page` request issues the press ticket for `target` (the gate may outlast the 5 s tap ticket).
- `{"cmd":"debug_outward","decision":"validate|cancel"}` (tests, demo): answer the open panel.
Logged: the request id, class, decision and reason only, never the title or the preview.
