# OS-intent film kit

- `pitch.md` — EN + FR: 1 line, 3 lines, 30 s and 90 s voice-over scripts (each claim grounded in a real feature).
- `features.md` — each feature: what the viewer sees, the key, the timing, the still; the fake data to reuse.
- `shotlist.md` — 15 shots, ~90 s, and the 30 s cut; which shots come from `oi demo` and which are live captures.
- `render/` — the offscreen renderer of the stills in `../img/`:
  `cd render && swift build -c release && .build/release/HUDRender /tmp/os-intent-stills` (2880×1800 PNGs;
  `../img/` holds them downscaled to 1440 px).
  It compiles the HUD's own `HUDCore`, `HUDView.swift` and `HintDots.swift` (symlinks into `../../../hud`), opens no
  socket, registers no hotkey and puts no window on screen. Every name, mail, page and agent in it is fictional.
