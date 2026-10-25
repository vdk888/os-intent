# OS-intent — shot list (≈ 90 s, and a 30 s cut)

Fits the 90 s and 30 s voice-overs in `pitch.md`. Format 16:9, 2560×1440 or 1920×1080, 60 fps (the HUD springs;
30 fps looks choppy). Energy: "spaceship cockpit". Cuts on the beat, punch-ins (150-200 %) on the HUD, a subtle
whoosh on each open, a soft click on each key tap. Show a **keycap overlay** (big fn / ⌃ / ⌥ / ⌘ caps in their
colours: pink, blue, yellow, green) bottom-centre whenever a key is pressed: the viewer cannot see the keyboard.

Set-up before filming:
- Record from a **demo user account**, or use `oi demo` (its own HUD, fake data, nothing sent; see docs/CLI.md
  "Recording a demo"). Dark mode, clean desktop, Dock auto-hidden, notifications off.
- Live takes: settings `hud.auto_mode: "hint"`, `gate.ttl_ms: 15000` while filming; the fictional agents Atlas /
  Nova / Orion / Lyra in `vocal_recipients`, `OI_VOCAL_DRY_RUN=1` so nothing is sent.
- Fake data only (features.md). Never a real inbox, chat list or agent name on screen.

## 90 s

| # | t (s) | Shot | On screen | Key / action | VO (EN) | Source |
|---|---|---|---|---|---|---|
| 1 | 0-4 | Cold open | Black → a pointer on a wallpaper; four coloured dots blink in beside it | — | "You know your next move before you make it." | `01-hint-dots.png` |
| 2 | 4-8 | The problem | Fast montage: ⌘-Tab, trackpad swipes, hunting for a button (speed ramp ×2) | — | "Your computer doesn't. Until now." | live b-roll |
| 3 | 8-16 | Day 1 vs day 30 | The same client mail lands. Day 1: the cross only offers app jumps. Title card "30 days later". Day 30: "Reply to Sam · Open Calendar: Friday · Forward to Dana Park" | **⌥⌘** | "On day one, it knows your apps. On day thirty, it knows your work." | `oi demo contrast` |
| 4 | 16-21 | Hint → cross | Mail; dots by the pointer; ⌥⌘: they spring into capsules | **⌥⌘** | "Four dots. Tap Option-Command: four next steps." | `oi demo hint`, `02-cross.png` |
| 5 | 21-26 | One key | Overlay lights fn ⌃ ⌥ ⌘ in turn at their places; a real ⌘ tap → "Desk 2" slides | **⌘** | "Four fixed places. One tap, done." | live |
| 6 | 26-35 | Glide + context card | Hold ⌥⌘, rest on "Reply to Sam": the level-2 line, then the level-3 card (summary, excerpt, "northwind — active client"); follow-ups bloom; release on "Draft: Friday 10:00 ok" | **⌥⌘ hold**, rest, release | "Hold, and it tells you what it's about, and why it matters. Glide. Release." | `oi demo glide`, `09-context-card.png` |
| 7 | 35-41 | Claude draft | Long-press ⌃ on "Reply to Sam": ring fills, spinner, the draft card ("Draft, not sent") | **⌃ hold** | "Claude drafts the reply. You still press Send." | `oi demo longpress`, `04-long-press.png` |
| 8 | 41-49 | Something changed | Hands off. A new mail lands: the dots return with a thin ring. ⌥⌘ → the cross on that mail | none, then **⌥⌘** | "You didn't move. Something changed. It noticed." | `oi demo proactive`, `08-proactive-ring.png` |
| 9 | 49-56 | An agent is waiting | Telegram direction: "Atlas · 2", "Nova · 1"; rest on Atlas: its card; click → the chat | **⌥⌘ hold**, rest, click | "Your agents are waiting? One click to the right chat." | `10-agent-waiting.png` (+ live) |
| 10 | 56-60 | Files | A PDF lands on the Desktop; ringed dots; ⌥⌘ → "Open devis.pdf" | **⌥⌘**, **fn** | "A file lands. It's one key away." | live |
| 11 | 60-68 | Vocal | ⌃⌥ → picker; ⌃ picks Nova; panel: live transcript, the countdown; type a word: the mic stops, the text stays; toggle "Attach a screenshot"; Enter → the gate panel → validate | **⌃⌥**, **⌃**, speak, type, **Enter**, **Enter** | "Talk to an agent. Or type. Add the screen. Nothing leaves without your yes." | `oi demo vocal`, `06-vocal-picker.png` + live |
| 12 | 68-74 | Page action + gate | Safari PR #42: "Squash & merge" at ⌃; tap → the panel "Press “Squash & merge”?" → Enter | **⌥⌘**, **⌃**, **Enter** | "The button you were about to look for. Confirmed, then pressed." | `oi demo page`, `07-page-action.png` |
| 13 | 74-79 | Wish | ⌥` → panel; type "open telegram"; it resolves under the text; Enter | **⌥`**, type, **Enter** | "Not on the menu? Say it. It remembers." | `oi demo wish` |
| 14 | 79-84 | Local, in control | Menu bar: Pause, Laya ✓ (toggle off: "~3 GB freed"), Settings → each source on/off; cut to Terminal `oi health`: all green | click | "Local first. Every sensor is yours to switch off. Nothing fails in silence." | live |
| 15 | 84-90 | End card | `00-hero.png` blurred behind the logo; "Your next decision, one key away."; the repository URL | — | "OS-intent. Your next decision, one key away." | `00-hero.png` |

## 30 s cut

Shots 1 → 3 (day 1 / day 30, 6 s) → 4 → 6 (glide + level-3 card) → 8 (proactive ring) → 11 (vocal, 4 s) → 15.
Use the 30 s voice-over.

## Stills vs. live

`../img/*.png` are rendered offscreen from the real HUD views (HUDView, the capsules, the hint dots, the context
card) over a fake desktop by `render/` (`cd render && swift build -c release && .build/release/HUDRender <out>`).
They are faithful for layout, colours, badges and labels, but the capsule material is the non-glass fallback (an
offscreen bitmap cannot sample the desktop behind a panel), and motion plus the AppKit panels (wish, vocal recording,
outward gate, screenshot note, toasts, the menu bar) are not in the stills: capture those live, from a demo account
or with `oi demo`.

### Live captures (demo account, screen recording on)

1. **Hint → cross** (shot 4): open Mail on the fake Sam mail; wait for the dots; tap **⌥ and ⌘ together**, release
   within 0.5 s.
2. **Glide + card** (shot 6): in Mail, **hold ⌥⌘**; move onto the top capsule; rest until the level-3 card shows and
   the follow-ups bloom; move onto "Draft: …"; release.
3. **Proactive** (shots 8-10): enable Mail, Agents and Files in Settings → Proactive; send yourself a fake mail from
   the demo account; drop a PDF on the Desktop; for the agent shot, a fictional bot writes to the demo Telegram.
4. **Vocal** (shot 11): `OI_VOCAL_DRY_RUN=1`; tap **⌃ and ⌥ together**; tap **⌃**; say "Move the review to Friday";
   type one word; toggle the screenshot with **C**; **Enter**, then **Enter** on the gate panel.
5. **Page action** (shot 12): Safari on a throwaway PR in a test repository you own; ⌥⌘; the key of "Squash & merge";
   **Enter** on the panel.
6. **Menu bar + health** (shot 14): click the OS-intent menu bar item, untick **Laya**; Terminal: `oi health`.
