import AppKit
import SwiftUI
import ApplicationServices
import HUDCore
import OISettings

/// A panel that can never become key or main: the front app keeps focus and typing.
final class HUDPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

/// The HUD content view: the first click on a capsule is the click (no "activate first" click), and the
/// panel being non-activating, oi-hud never comes to the front.
final class FirstMouseHostingView<Content: View>: NSHostingView<Content> {
    /// W14: a right-click on a capsule = the Claude-assisted variant (point in window coordinates).
    var onRightClick: ((NSPoint) -> Void)?
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func rightMouseDown(with event: NSEvent) { onRightClick?(event.locationInWindow) }
}

/// Owns the panel, the visibility gates (ttl, Esc, typing, fast pointer), the hotkeys, the
/// modifier taps (W9), the summon key (W10), the vocal flow (W12) and the rate limit. All methods run on
/// the main thread.
final class HUDController {
    private let panel: HUDPanel
    private let model = HUDModel()
    private let hotkeys = HotKeys()
    private var limiter: RateLimiter
    private var gate = VelocityGate()
    private var liveBound: String?            // the live context state the cross on screen is bound to
    private var pointerHide = true            // settings.json gate.hide_on_pointer_travel, read on each show
    // W9: exists only while the HUD is visible
    private let taps = ModifierTapMonitor(onLine: "modifier taps ON (⌃ ⌥ ⌘ fn = options 1-4, ⇧ = wish, listen-only tap while the HUD is visible)",
                                          offLine: "modifier taps OFF: using ⌥1-⌥4")
    // W10: permanent while the daemon runs (when the summon key is a tap)
    private let summonTap = ModifierTapMonitor(onLine: "summon tap ON (listen-only, permanent)",
                                               offLine: "summon tap OFF, retried every 60 s")
    private var summonArm: SummonArm?
    private var summonRetryAt: TimeInterval = 0
    private var summonWindow = SummonWindow()
    private var summonTimer: Timer?
    private var tapKeys: [TapModifier: Int] = [:]
    private var owner: Connection?
    private var ttlTimer: Timer?
    private var tick: Timer?
    private var keyMonitor: Any?
    private var hideSeq = 0
    private let axTrusted: Bool
    private var warnedHotkeys = false
    private let settings = SettingsCache()   // settings.json, re-read only when its mtime changes
    private var wishTracker = WishHotkeyTracker()
    private var settingsPoll: Timer?
    private let wish = WishPanelController()
    // W11: target hints, drawn only while the HUD is visible
    private let overlay = HintOverlay()
    private var hintLife = HintOverlayLifecycle()
    // W12: "Send a vocal to…" — picker (in the HUD) → recording panel → `oi vocal-send`
    private var vocal = VocalFlow()
    private let vocalPanel = VocalPanelController()
    private let toast = ToastController()
    // the vocal key while RECORDING (the HUD is hidden then, so the per-show tap is gone)
    private let vocalTap = ModifierTapMonitor(onLine: "vocal tap ON (listen-only, while recording)",
                                              offLine: "vocal tap OFF: Enter sends")
    private var vocalSession: VocalSession?
    // W14: a long-press / right-click / ⌥-click runs the option's Claude-assisted variant
    private var claudeWork = ClaudeWork()
    private var claudeTimer: Timer?
    private var cardDraft: Int?               // the draft the card's « Open in Mail » opens (press only)
    // W15: hold ⌥⌘ + glide + dwell → Claude sub-rings (a marking menu)
    private var glide = GlideState()
    private var glideSentStart = false        // the owner was told `glide start` (it prefetches level 1)
    private var glideTTLms = 0                // the show's ttl, restored when a short hold stays a tap
    private var glideAnchor: NSPoint?         // the pointer when the hold began: the cross is fixed there
    private var contextCards: [Int: ContextCard] = [:]   // this show's context cards (by top key)
    private var contextSig: String?           // what the drawn card was placed for (card, level, rings)
    private var debugGlideOn = false          // `oi-hud debug-glide`: the hovered path is simulated
    private var debugHover: [Int]?
    private var debugPlan: DebugGlidePlan?
    private var debugTimer: Timer?
    // W16: page actions — the AX reader/presser, the per-tap press ticket (the outward gate confirms)
    private let pageAX = PageAX()
    private var pressGuard = PressGuard()
    // W17: the screenshot note panel (Annotate / the tier-2 Claude note), driven by `oi live`
    private let shotNote = ShotNotePanelController()
    // Outward gate: the confirm panel every send / outward page press waits for (oi/gate.py)
    private let outwardGate = OutwardConfirmController()
    // W19: automatic shows as hints (dots), developed by the summon key
    private var hintDots = HintLife()
    private var hintOwner: Connection?        // the hint's show connection (answered when developed or expired)
    private var hintTimer: Timer?             // fade, then the grace end
    private var hintSeq = 0
    private var developAnchor: NSPoint?       // develop: the cross opens at the pointer (W21b)
    // end W19
    // W21b: the dots follow the pointer while drawn (a 60 Hz timer only then; nothing runs when hidden)
    private var follower = HintFollower()
    private var followTimer: Timer?
    private var followLast: TimeInterval = 0
    private var hintEscMonitor: Any?          // listen-only: Esc ends a drawn hint (the front app still gets it)
    /// Broadcast hook: a saved wish → `{"event":"wish","text_len":n}` to every connected client.
    var onWishSaved: ((Int) -> Void)?
    /// Broadcast hook: a summon → `{"event":"summon","ts":…}` to every connected client (W10).
    var onSummon: ((String) -> Void)?

    init(minInterval: TimeInterval) {
        limiter = RateLimiter(minInterval: minInterval)
        panel = HUDPanel(contentRect: NSRect(origin: .zero, size: Metrics.panelSize),
                         styleMask: [.borderless, .nonactivatingPanel, .hudWindow],
                         backing: .buffered, defer: false)
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.ignoresMouseEvents = true
        panel.hidesOnDeactivate = false
        panel.isFloatingPanel = true
        panel.becomesKeyOnlyIfNeeded = true
        panel.animationBehavior = .none
        axTrusted = AXIsProcessTrusted()
        if !axTrusted {
            log("Accessibility not granted: 'hide on typing' is off (ttl, Esc, taps/hotkeys and fast-pointer still work)")
        }
        var view = HUDView(model: model) { [weak self] key, claude in
            self?.pressed(key, via: "click", claude: claude)       // ⌥-click = Claude (W14)
        }
        view.onPressPath = { [weak self] path in self?.executePath(path, via: "click") }   // W15 sub-ring click
        view.onOpenDraft = { [weak self] in self?.openDraftInMail() }
        let host = FirstMouseHostingView(rootView: view)
        host.onRightClick = { [weak self] p in self?.rightClick(at: p) }
        host.frame = NSRect(origin: .zero, size: Metrics.panelSize)
        panel.contentView = host
        // option taps: one modifier only (a ⌥+⌘ summon set never fires option 2 or 3); W12: the vocal SET
        // (default ⌃+⌥) on the same monitor while the HUD is visible, on its own monitor while recording
        taps.onTap = { [weak self] set in
            if set.count == 1, let m = set.first { self?.tapped(m) } else { self?.vocalSetTapped(set) }
        }
        vocalTap.onTap = { [weak self] set in self?.vocalSetTapped(set) }
        taps.onLongPress = { [weak self] m in self?.longPressed(m) }
        vocalPanel.onEnter = { [weak self] in self?.vocalInput(.enter) }
        vocalPanel.onCancel = { [weak self] in self?.vocalInput(.cancel) }
        vocalPanel.onTyped = { [weak self] in self?.vocalInput(.typed) }   // a text key: listening -> typing
        vocalPanel.dictation.onPartial = { [weak self] text in
            guard let self, self.vocal.isRecording, !self.vocal.isTyping else { return }
            self.vocalSession?.transcript = text
            self.vocalPanel.setTranscript(text)
        }
        vocalPanel.dictation.onEnd = { [weak self] hint in
            guard let self, self.vocal.isRecording, !self.vocal.isTyping else { return }
            if let hint {                                  // could not listen: the panel becomes a text field
                self.vocalSession?.hint = hint; self.vocalPanel.setHint(hint)
                self.vocalInput(.dictationUnavailable); return
            }
            self.vocalInput(.dictationEnded)
        }
        // summon: exactly the configured set (⌥ alone, ⌘ alone, ⌥ then ⌘ never summon); while the HUD
        // is visible SummonGate ignores it ("visible"), so the set is consumed without effect.
        summonTap.onTap = { [weak self] set in
            if case .tap(let want)? = self?.summonArm, set == want { self?.summon("tap") }
        }
        // W15: the same set HELD ≥ 250 ms = the glide (its release is then not a tap)
        summonTap.onHoldStart = { [weak self] in self?.holdStarted() }
        summonTap.onHoldEnd = { [weak self] downAt in self?.holdEnded(downAt: downAt) }
        summonTap.onHoldInterrupt = { [weak self] in self?.glideCancel("glide_esc") }
        hotkeys.onPress = { [weak self] id in
            self?.taps.chordFired()       // ⌥` etc.: the held ⌥ must not also count as a tap
            self?.summonTap.chordFired()
            self?.vocalTap.chordFired()
            if id == wishHotkeyID { self?.wishRequested() }
            else if id == vocalHotkeyID { self?.vocalKey() }
            else if id == summonHotkeyID { self?.summon("chord") }
            else if id == HotKeys.escapeID { self?.dismiss("esc") } else { self?.pressed(Int(id)) }
        }
        wish.localeProvider = { [weak self] in self?.settings.current.wish.locale ?? WishSettings.defaultLocale }
        wish.onSave = { [weak self] text, source, ctx in self?.saveWish(text, source: source, context: ctx) }
        // W16: a confirmed wish for a page action ("merge") = the user's explicit second step: its press ticket
        wish.resolve.onConfirmPress = { [weak self] target in
            self?.pressGuard.issue(target: target, confirmed: true, at: ProcessInfo.processInfo.systemUptime)
            log("wish: page action confirmed, press ticket issued")
        }
        wish.resolve.onCancelPress = { [weak self] target in self?.pressGuard.revoke(target: target) }   // W18 follow-up
        // Outward gate: a validated page press = the user's explicit step: its press ticket (the gate may outlast the tap's)
        outwardGate.hotkeys = { [weak self] in self?.settings.current.hotkeys ?? HotkeySet.defaults }
        outwardGate.onValidatedTarget = { [weak self] target in
            self?.pressGuard.issue(target: target, confirmed: true, at: ProcessInfo.processInfo.systemUptime)
        }
        // The wish hotkey is PERMANENT: registered now, kept in step with settings.json every 2 s,
        // independent of hud_enabled (the wish is the one action always available).
        // W10: the summon key too (a permanent tap, or a permanent chord).
        syncWishHotkey()
        syncSummon()
        let t = Timer(timeInterval: 2, repeats: true) { [weak self] _ in self?.syncWishHotkey(); self?.syncSummon() }
        RunLoop.main.add(t, forMode: .common)
        settingsPoll = t
    }

    // MARK: wish

    private func syncWishHotkey() {
        let current = settings.current
        guard let reg = wishTracker.sync(current) else { return }
        let ok = hotkeys.setPermanent(reg, id: wishHotkeyID)
        log("wish hotkey \(WishHotkeyTracker.display(current)) \(ok ? "registered" : "NOT registered")")
    }

    // MARK: summon (W10)

    /// Keeps the summon key armed as settings.json says: (re)arms on change, retries a missing tap
    /// every 60 s (Input Monitoring granted later, no restart needed).
    private func syncSummon() {
        let want = SummonArm(hotkeys: settings.current.hotkeys)
        let now = ProcessInfo.processInfo.systemUptime
        if want != summonArm {
            summonTap.stop()
            hotkeys.setPermanent(nil, id: summonHotkeyID)
            summonArm = want
            summonRetryAt = 0
            if case .tap(let set) = want, set.count >= 2 { summonTap.hold = SetHoldDetector(want: set) } else { summonTap.hold = nil }
            if case .chord(let reg) = want {
                let ok = hotkeys.setPermanent(reg, id: summonHotkeyID)
                log("summon hotkey \(SummonArm.display(settings.current.hotkeys)) \(ok ? "registered" : "NOT registered")")
            }
        }
        if case .tap = want, !summonTap.isActive, now >= summonRetryAt {
            summonRetryAt = now + 60
            _ = summonTap.start()
        }
    }

    /// A summon (the key, or `oi-hud debug-summon`): ignored when the HUD is off, logging is paused,
    /// the HUD is already up or the wish panel is open; otherwise broadcast it and wait
    /// `summonFallbackDelay` for the live driver's `show`, then draw the fallback (wish option alone).
    /// Nothing is logged for a key summon (only the debug path and the fallback say something).
    func summon(_ source: String) {
        let current = settings.current
        if let why = SummonGate.check(hudEnabled: current.hudEnabled, paused: current.paused,
                                      visible: model.visible, wishOpen: wish.isOpen || vocal.isRecording) {
            if source == "debug" { log("summon ignored: \(why)") }
            return
        }
        if developHint(source) { return }       // W19: a hint up (or just faded): develop it, no broadcast
        if source == "debug" { log("summon (debug)") }
        summonWindow.begin(at: ProcessInfo.processInfo.systemUptime)
        onSummon?(isoNow())
        pageAX.prefetch()                // W16: the page snapshot is ready when `oi live` asks for it
        summonTimer?.invalidate()
        summonTimer = Timer.scheduledTimer(withTimeInterval: summonWindow.delay, repeats: false) { [weak self] _ in
            self?.summonFallback()
        }
    }

    private func summonFallback() {
        summonTimer = nil
        guard summonWindow.fallbackDue(at: ProcessInfo.processInfo.systemUptime) else { return }
        let current = settings.current
        guard SummonGate.check(hudEnabled: current.hudEnabled, paused: current.paused,
                               visible: model.visible, wishOpen: wish.isOpen || vocal.isRecording) == nil else { return }
        log("summon: no show from the live driver in \(Int(summonWindow.delay * 1000)) ms, fallback (wish option)")
        show([], ttlMs: current.gateTTLms, conn: nil)
        if model.visible { summonWindow.fallbackShown() }   // a late live show replaces it, rate limit skipped
    }

    private func isoNow() -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.string(from: Date())
    }

    /// The wish hotkey, or the HUD's "Tell me what you wanted" option. First press opens the panel
    /// (recording what the HUD showed, then closing the HUD); a press while open toggles dictation.
    func wishRequested() {
        if vocal.isRecording { return }                  // never over the vocal recording
        if wish.isOpen { wish.input(.hotkey); return }
        let shown = model.visible ? model.options.filter { $0.key != wishOptionKey }.map(\.label) : []
        let ctx = WishContext(app: NSWorkspace.shared.frontmostApplication?.localizedName,
                              space: SpaceIndex.current(), hudVisible: model.visible, hudOptions: shown)
        if model.visible { finish(.dismissed(reason: "wish")) }
        wish.hotkey(context: ctx)
    }

    @discardableResult
    private func saveWish(_ text: String, source: WishSource, context: WishContext) -> Bool {
        let now = Date()
        guard var row = WishRow(date: now, rawText: text, source: source, context: context) else {
            log("wish: empty, nothing saved"); return false
        }
        row.action = wish.resolve.takeAction()          // W18: a resolved wish keeps what it ran (strong label)
        do {
            try WishStore.append(row, date: now)
            log("wish saved (\(source.rawValue), \(row.text.count) chars)")     // never the text
            onWishSaved?(row.text.count)
            return true
        } catch {
            log("wish NOT saved: \(error)"); return false
        }
    }

    func stateInfo() -> HUDStateInfo {
        let current = settings.current
        let armed: Bool
        switch summonArm {
        case .tap?: armed = summonTap.isActive
        case .chord?: armed = hotkeys.isPermanentRegistered(summonHotkeyID)
        default: armed = false
        }
        var info = HUDStateInfo(visible: model.visible, options: model.visible ? model.options : [],
                            hudEnabled: current.hudEnabled, wishHotkey: WishHotkeyTracker.display(current),
                            wishRegistered: hotkeys.isPermanentRegistered(wishHotkeyID), wishPanel: wish.stateName,
                            tapsActive: taps.isActive, summonKey: SummonArm.display(current.hotkeys),
                            summonArmed: armed, hints: hintLife.drawn,
                            vocalKey: VocalArm.display(current.hotkeys), vocal: vocal.stateName,
                            claude: claudeStateName,
                            glide: glide.active ? "gliding:\(model.trail.map(dotted) ?? "-");rings:\(glide.rings.count)"
                                + (model.contextCard.map { ";card:\($0.card.key).\($0.level)" } ?? "") : "idle")
        info.hint = hintDots.stateName   // W19
        return info
    }

    /// `serve --debug-open-wish`: open the panel once, as the hotkey would (for the demo clip).
    func debugOpenWish() { wishRequested() }

    // MARK: commands

    func handle(_ result: Result<HUDCommand, CommandError>, from conn: Connection) {
        switch result {
        case .failure(let e):
            conn.send(.error(e.description))
        case .success(.hide):
            switch hintDots.hideTarget(crossVisible: model.visible) {   // W24: a hint whose state ended goes too
            case .cross: dismiss("hide_cmd")
            case .hint: endHint("hide_cmd")
            case .nothing: break
            }
            conn.send(.ok)
        case .success(.show(let options, let ttlMs)):
            show(options, ttlMs: ttlMs, conn: conn)
        case .success(.showHint(let options, let ttlMs, let hintTTLms, let marker)):   // W19 (+ W24 marker)
            showHint(options, ttlMs: ttlMs, hintTTLms: hintTTLms, marker: marker, conn: conn)
        case .success(.state):
            conn.send(.state(stateInfo()))
        case .success(.debugSummon):
            summon("debug")
            conn.send(.ok)
        case .success(.debugVocal(let pick, let text)):
            debugVocal(pick: pick, text: text)
            conn.send(.ok)
        case .success(.vocalTo(let slug)):               // W22: "Ask <agent>" starter option
            vocalTo(slug)
            conn.send(.ok)
        case .success(.update(let updates)):
            updateLabels(updates, conn: conn)
        case .success(.result(let key, let ok, let text, let ttlMs, let openDraft)):
            claudeResult(key: key, ok: ok, text: text, ttlMs: ttlMs, openDraft: openDraft, conn: conn)
        case .success(.subring(let path, let options)):
            glideSubring(path, options: options, conn: conn)
        case .success(.context(let cards)):              // kept for this show, drawn while gliding
            guard model.visible else { conn.send(.dropped(reason: "not_visible")); return }
            contextCards = ContextCard.merge(contextCards, cards)
            contextSig = nil
            refreshContextCard(hover: model.trail)
            conn.send(.ok)
        case .success(.w18(let c)):                      // W18: wish -> action
            wish.resolve.handle(c, conn: conn) { [weak self] app in
                guard let self, !self.wish.isOpen, !self.vocal.isRecording else { return }
                if self.model.visible { self.finish(.dismissed(reason: "wish")) }
                self.wish.hotkey(context: WishContext(app: app ?? NSWorkspace.shared.frontmostApplication?.localizedName,
                                                      space: SpaceIndex.current(), hudVisible: false, hudOptions: []))
            }
        case .success(.debugGlide(let path, let cancel)):
            debugGlide(path, cancel: cancel)
        case .success(.showLive(let options, let ttlMs, let live)):   // bound to a live state, never rate-limited
            if shotNote.isOpen { conn.send(.dropped(reason: "note_open")); return }
            show(options, ttlMs: ttlMs, conn: conn, force: true, live: live)
        case .success(.shotNote(let req)):                    // W17: the note panel; replies note / dismissed
            if model.visible { finish(.dismissed(reason: "shot_note")) }
            shotNote.open(req) { ev in conn.send(ev) }
        case .success(.debugNote(let text)):                  // W17: tests / demo
            shotNote.debugSubmit(text)
            conn.send(.ok)
        case .success(.confirmOutward(let req)):              // outward gate: one `outward` event per request
            outwardGate.open(req) { ev in conn.send(ev) }
        case .success(.debugOutward(let validate)):
            outwardGate.debugAnswer(validate: validate)
            conn.send(.ok)
        case .success(.debugPress(let key, let mode)):
            if model.visible { pressed(key, via: "debug", claude: mode == "claude") }
            conn.send(.ok)
        case .success(.wish(let text, let source)):
            // context as if the hotkey had been pressed now; the reply and the broadcast carry the length only
            let ctx = WishContext(app: NSWorkspace.shared.frontmostApplication?.localizedName,
                                  space: SpaceIndex.current(), hudVisible: false, hudOptions: [])
            if !saveWish(text, source: source, context: ctx) { conn.send(.error("wish_not_saved")) }
        }
    }

    /// `conn` nil = the summon fallback (no client owns it). The first show after a summon answers
    /// it: the fallback timer is cancelled and the rate limit is skipped.
    private func show(_ options: [HUDOption], ttlMs: Int, conn: Connection?, force: Bool = false,
                      live: String? = nil) {   // W17: force; live

        let current = settings.current
        if let drop = EnabledGate.check(hudEnabled: current.hudEnabled) {
            conn?.send(drop); return
        }
        if wish.isOpen { conn?.send(.dropped(reason: "wish_open")); return }   // never cover the wish panel
        if vocal.state != .idle { conn?.send(.dropped(reason: "vocal")); return }   // W12: picker or recording
        if claudeWork.isBusy { conn?.send(.dropped(reason: "claude")); return }      // W14: Claude works / its card
        // W15: while gliding, the options under the pointer never change (only the summon's answer replaces them)
        if glide.active, model.visible, !summonWindow.isOpen, !summonWindow.fallbackOnScreen {
            conn?.send(.dropped(reason: "glide")); return
        }
        let now = ProcessInfo.processInfo.systemUptime
        let answersSummon = conn != nil && summonWindow.answer()
        if answersSummon { summonTimer?.invalidate(); summonTimer = nil }
        guard conn == nil || answersSummon || force || limiter.allow(now: now) else {
            conn?.send(.dropped(reason: "rate_limited")); return
        }
        // ≤ 4 predicted + "Tell me what you wanted" (4:30) + W12 "Send a vocal to…" (7:30)
        present(optionsWithWishAndVocal(options), ttlMs: ttlMs, conn: conn)
        liveBound = live                                  // set after present (a swap resets it)
    }

    /// Draws `options` (already final: predicted + wish + vocal, or the W12 agent picker + vocal) and
    /// arms their keys; swaps in place when the HUD is already up.
    private func present(_ options: [HUDOption], ttlMs: Int, conn: Connection?) {
        liveBound = nil                               // show() binds it again after a live show
        contextCards = [:]; contextSig = nil; model.contextCard = nil   // cards belong to one show
        let current = settings.current
        let ws = NSWorkspace.shared
        model.reduceMotion = ws.accessibilityDisplayShouldReduceMotion
        model.increaseContrast = ws.accessibilityDisplayShouldIncreaseContrast
        if !hintDots.isIdle { endHint("replaced") }   // W19: a full show (or the picker) replaces a hint
        model.glass = liquidGlassAvailable()          // W19
        // keys follow the current option set; combos and taps come from settings.json `hotkeys`.
        // Taps need the event tap (Input Monitoring); without it the tap slots fall back to ⌥n.
        let optionKeys = options.map(\.key).filter { (1...maxOptions).contains($0) }   // (the wish tap is added by the plan)
        var plan = hotkeyPlan(optionKeys: optionKeys, hotkeys: current.hotkeys)
        let vocalIsTap: Bool = { if case .tap = VocalArm(hotkeys: current.hotkeys) { return true }; return false }()
        taps.longPress = LongPressDetector(thresholdMs: current.hud.longPressMs)     // W14
        model.claudeKeys = Set(options.filter(\.offersClaude).map(\.key))
        if !plan.taps.isEmpty || vocalIsTap, !taps.start() {
            plan = hotkeyPlan(optionKeys: optionKeys, hotkeys: current.hotkeys, tapsAvailable: false)
        }
        if plan.taps.isEmpty, !vocalIsTap { taps.stop() }
        tapKeys = plan.taps
        // badges show what is armed: ⌃ ⌥ ⌘ fn for taps, the chord otherwise (⌥n in the fallback)
        let keys = current.hotkeys.problems().isEmpty ? current.hotkeys : HotkeySet.defaults
        // the wish option: its one-key tap while visible (W11, default ⇧), else the permanent chord (⌥`)
        var badges: [Int: String] = [wishOptionKey: plan.taps.values.contains(wishOptionKey)
                                     ? keys.wishVisible.display : WishHotkeyTracker.display(current),
                                     vocalOptionKey: VocalArm.display(current.hotkeys)]
        for k in 1...maxOptions {
            guard let h = keys.option(k) else { continue }
            badges[k] = optionBadge(key: k,   // W19: "2 ⌃" (number first; W21: the number is the key)
                                    symbol: (h.isTap && !plan.taps.values.contains(k)) ? HotkeySet.legacyChord(k).display : h.display)
        }
        model.badges = badges
        model.positions = FixedSlots.positions(hotkeys: current.hotkeys, slots: current.hud.slots)   // W10 fixed places
        // W11: colour per key (the colour of the modifier that owns the option's place), stable across shows.
        // Capsules are always dark: their dark variant. On-screen hints follow the system appearance.
        let darkSystem = NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        var capsuleColors: [Int: Color] = [:], hintColors: [Int: NSColor] = [:]
        for o in options where (1...maxOptions).contains(o.key) {
            guard let c = OptionColors.color(forOption: o.key, positions: model.positions, hud: current.hud) else { continue }
            capsuleColors[o.key] = Color(nsColor: .hex(c.dark))
            model.colorHex[o.key] = c.dark
            hintColors[o.key] = .hex(OptionColors.variant(c, dark: darkSystem))
        }
        model.colors = capsuleColors

        if model.visible {
            // Already on screen: swap the content in place (each option at its key's fixed place).
            owner?.send(.dismissed(reason: "replaced"))
            owner = conn
            glideSentStart = false
            withAnimation(model.reduceMotion ? .easeInOut(duration: Metrics.appear) : Metrics.spring) {
                model.options = options
            }
        } else {
            owner = conn
            hideSeq += 1
            placeAtPointer(glide.active ? glideAnchor : developAnchor)   // W19/W21b: develop = the pointer at ⌥⌘
            panel.orderFrontRegardless()
            if !model.dots.isEmpty {   // W19: develop: the capsules spring out of the dots (same glass ids)
                withAnimation(model.reduceMotion ? .easeInOut(duration: Metrics.appear) : Metrics.spring) {
                    model.dots = []; model.options = options; model.visible = true
                }
            } else {
                model.options = options
                withAnimation(.easeOut(duration: Metrics.appear)) { model.visible = true }
            }
            let ph = current.pointerHide
            pointerHide = ph.enabled
            gate = VelocityGate(ph)
            gate.reset(shownAt: ProcessInfo.processInfo.systemUptime)
            startTick()
            startKeyMonitor()
        }
        if plan.fellBack, !warnedHotkeys {
            warnedHotkeys = true
            log("settings.json hotkeys invalid (\(current.hotkeys.problems().joined(separator: "; "))): using defaults (⌃ ⌥ ⌘ fn taps, Esc)")
        }
        hotkeys.register(plan.regs)
        showHints(options, colors: hintColors)
        ttlTimer?.invalidate()
        ttlTimer = Timer.scheduledTimer(withTimeInterval: effectiveTTL(ms: ttlMs), repeats: false) { [weak self] _ in
            self?.dismiss("ttl")
        }
        glideTTLms = ttlMs
        if glide.active { enterGlide() }
    }

    // MARK: hint (W19)

    /// A `show` with `"mode":"hint"`: tiny coloured dots at the options' places, nothing armed (no keys, no
    /// taps, no clicks, no target hints), not hidden by pointer travel or typing. They fade after `hintTTLms`;
    /// the summon key develops them until `hintTTLms` after the fade, then the owner is told `dismissed:
    /// hint_ttl`. A full cross on screen is never downgraded (`dropped: visible`).
    private func showHint(_ options: [HUDOption], ttlMs: Int, hintTTLms: Int, marker: HintMarker? = nil, conn: Connection) {
        let current = settings.current
        if let drop = EnabledGate.check(hudEnabled: current.hudEnabled) { conn.send(drop); return }
        if wish.isOpen { conn.send(.dropped(reason: "wish_open")); return }
        if vocal.state != .idle { conn.send(.dropped(reason: "vocal")); return }
        if claudeWork.isBusy { conn.send(.dropped(reason: "claude")); return }
        if glide.active || model.visible { conn.send(.dropped(reason: "visible")); return }
        if summonWindow.isOpen { conn.send(.dropped(reason: "summon")); return }   // the summon's answer is due
        guard limiter.allow(now: uptime) else { conn.send(.dropped(reason: "rate_limited")); return }
        if !hintDots.isIdle { endHint("replaced") }
        hintDots.show(options, fullTTLms: ttlMs, hintTTLms: hintTTLms, at: uptime, marker: marker)
        guard !hintDots.options.isEmpty else { hintDots.reset(); conn.send(.dropped(reason: "no_option")); return }
        hintOwner = conn
        model.reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        model.increaseContrast = NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast
        model.glass = liquidGlassAvailable()
        model.positions = FixedSlots.positions(hotkeys: current.hotkeys, slots: current.hud.slots)
        var colors: [Int: Color] = [:]
        for o in hintDots.options {
            if let c = OptionColors.color(forOption: o.key, positions: model.positions, hud: current.hud) {
                colors[o.key] = Color(nsColor: .hex(c.dark))
            }
        }
        model.colors = colors
        model.options = []; model.card = nil; model.rings = []; model.halos = []   // nothing stale under the dots
        hideSeq += 1
        let p = NSEvent.mouseLocation          // the dots sit AT the pointer (only their own few points kept on screen)
        model.dotSize = Double(current.hud.hintDotSize)                                     // W20
        model.hintRing = marker == .ring                                                     // W24
        let half = HintDotLayout.halfExtent(dotSize: model.dotSize, marker: hintDots.marker)   // W24: ring
        setCentre(clampCentre((p.x, p.y), screen: screenBounds(for: p), halfSize: (w: half, h: half)))
        panel.ignoresMouseEvents = true
        panel.orderFrontRegardless()
        let dots = HintDotLayout.offsets(keys: hintDots.options.map(\.key), positions: model.positions)
        model.dotPulse = false
        withAnimation(.easeOut(duration: hintFadeIn)) { model.dots = dots }
        pulseHintDots(HintDotLayout.pulses(reduceMotion: NSWorkspace.shared.accessibilityDisplayShouldReduceMotion))   // W20
        log("hint: dots \(hintDots.options.map { String($0.key) }.joined(separator: ","))\(marker == .ring ? " ring" : "") (\(hintTTLms) ms)")
        armHintTimer()
        syncHintFollow()   // W21b
    }

    /// W21b: start / stop the follow timer (and the Esc listener) in step with the hint: on while the dots are
    /// drawn and the cross is not up, off on every end path (develop, fade, Esc, replace).
    private func syncHintFollow() {
        let f = panel.frame
        if follower.sync(hintDots, crossVisible: model.visible, centre: (Double(f.midX), Double(f.midY))) == .start {
            followLast = uptime
            let t = Timer(timeInterval: HintFollower.interval, repeats: true) { [weak self] _ in self?.followTick() }
            RunLoop.main.add(t, forMode: .common)
            followTimer = t
            if hintEscMonitor == nil {
                hintEscMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] e in
                    guard e.keyCode == 53 else { return }          // Esc
                    DispatchQueue.main.async { self?.hintEsc() }
                }
            }
        }
        if !follower.running {
            followTimer?.invalidate(); followTimer = nil
            if let m = hintEscMonitor { NSEvent.removeMonitor(m); hintEscMonitor = nil }
        }
    }

    /// W21b, 60 Hz while the dots are drawn: they slide after the pointer (Reduce Motion: no easing), on screen.
    private func followTick() {
        guard follower.running, hintDots.isDrawn, !model.visible else { syncHintFollow(); return }
        let now = uptime
        defer { followLast = now }
        let p = NSEvent.mouseLocation
        let half = HintDotLayout.halfExtent(dotSize: model.dotSize, marker: hintDots.marker)   // W24: ring
        let c = follower.step(pointer: (Double(p.x), Double(p.y)), dt: now - followLast, screen: screenBounds(for: p),
                              halfSize: (w: half, h: half), reduceMotion: model.reduceMotion)
        let o = panel.frame.origin
        if (c.x - Metrics.panelSize.width / 2).rounded() != Double(o.x) || (c.y - Metrics.panelSize.height / 2).rounded() != Double(o.y) {
            setCentre(c)
        }
    }

    private func hintEsc() {
        guard hintDots.isDrawn, !model.visible else { return }
        log("hint: esc")
        endHint("esc")
    }

    /// W20: the gentle appear pulse, `n` cycles (0 = none), then the dots rest at their size.
    private func pulseHintDots(_ n: Int) {
        guard n > 0 else { return }
        let half = HintDotLayout.pulseCycle / 2
        for i in 0..<(2 * n) {
            DispatchQueue.main.asyncAfter(deadline: .now() + hintFadeIn + Double(i) * half) { [weak self] in
                guard let self, !self.model.dots.isEmpty else { return }
                withAnimation(.easeInOut(duration: half)) { self.model.dotPulse = (i % 2 == 0) }
            }
        }
    }

    private func armHintTimer() {
        hintTimer?.invalidate()
        guard let due = hintDots.fadeDue ?? hintDots.graceDue else { hintTimer = nil; return }
        hintSeq += 1
        let seq = hintSeq
        hintTimer = Timer.scheduledTimer(withTimeInterval: max(0, due - uptime), repeats: false) { [weak self] _ in
            guard let self, self.hintSeq == seq else { return }
            if self.hintDots.isDrawn { self.fadeHint(); self.armHintTimer() } else { self.endHint("hint_ttl") }
        }
    }

    /// The dots go (the hint stays developable for its grace).
    private func fadeHint() {
        guard hintDots.fade(at: uptime) else { return }
        syncHintFollow()   // W21b
        withAnimation(.easeIn(duration: Metrics.appear)) { model.dots = [] }
        let seq = hideSeq
        DispatchQueue.main.asyncAfter(deadline: .now() + Metrics.appear + 0.03) { [weak self] in
            guard let self, self.hideSeq == seq, !self.model.visible, self.model.dots.isEmpty else { return }
            self.panel.orderOut(nil)
        }
    }

    /// The hint is over without being developed: its owner is answered `dismissed: <reason>`.
    private func endHint(_ reason: String) {
        hintTimer?.invalidate(); hintTimer = nil; hintSeq += 1
        let wasDrawn = hintDots.isDrawn
        hintDots.reset()
        hintOwner?.send(.dismissed(reason: reason)); hintOwner = nil
        syncHintFollow()   // W21b
        if wasDrawn, !model.visible {
            model.dots = []
            panel.orderOut(nil)
        } else {
            model.dots = []
        }
    }

    /// The summon key (tap or hold) while a hint is up or just faded: the same options as the full cross, on the
    /// hint's connection (told `{"event":"hint","phase":"expanded"}` first). False = no hint: summon as before.
    private func developHint(_ source: String) -> Bool {
        let drawn = hintDots.isDrawn
        guard let d = hintDots.develop(at: uptime) else { return false }
        hintTimer?.invalidate(); hintTimer = nil; hintSeq += 1
        let conn = hintOwner
        hintOwner = nil
        conn?.send(.hintExpanded)
        log("hint: developed (\(source), \(d.options.count) options\(drawn ? "" : ", after the fade"))")
        // W21b: the follow stops; the cross opens at the pointer of this moment and stays there
        let p = NSEvent.mouseLocation
        let a = follower.develop(pointer: (Double(p.x), Double(p.y)))
        syncHintFollow()
        developAnchor = NSPoint(x: a.x, y: a.y)
        defer { developAnchor = nil }
        present(optionsWithWishAndVocal(d.options), ttlMs: d.ttlMs, conn: conn)
        return true
    }
    // end W19

    /// W11: resolve and draw each option's target hint (window / Dock icon / halo / edge bar).
    private func showHints(_ options: [HUDOption], colors: [Int: NSColor]) {
        let r = overlay.resolve(options: options, colors: colors, axTrusted: axTrusted)
        let drawn = r.plan.map { DrawnHint(key: $0.key, drawn: $0.drawing) }
            + r.halos.sorted().map { DrawnHint(key: $0, drawn: .halo) }
        for e in hintLife.show(drawn.sorted { $0.key < $1.key }) {
            switch e {
            case .draw: overlay.draw(r.plan, reduceMotion: model.reduceMotion, contrast: model.increaseContrast)
            case .teardown: overlay.teardown()
            }
        }
        model.halos = r.halos
        if !hintLife.drawn.isEmpty {
            log("hints: " + hintLife.drawn.map { "\($0.key)=\($0.drawn.rawValue)" }.joined(separator: " "))
        }
    }

    /// A bare modifier tap (only delivered while the tap exists, i.e. while visible).
    private func tapped(_ m: TapModifier) {
        guard model.visible, !glide.active, let k = tapKeys[m] else { return }
        log(k == wishOptionKey ? "tap -> wish" : "tap -> option \(k)")
        pressed(k)
    }

    /// An option chosen by its key (`via` "key") or by a click on its capsule ("click"): the same path.
    /// `claude` (W14): the long-press / right-click / ⌥-click; the plain press when the option has no Claude variant.
    private func pressed(_ key: Int, via: String = "key", claude: Bool = false) {
        if claudeWork.isBusy { return }                    // W14: Claude works / its card: only Esc
        if glide.active { executePath([key], via: via); return }   // W15: a click while gliding (never the ⌥-click variant)
        if key == vocalOptionKey { vocalKey(); return }
        if case .picking(let page) = vocal.state {        // W12: the picker's options are agents
            guard model.visible, let a = VocalPicker.agent(forOptionKey: key, page: page, hotkeys: settings.current.hotkeys) else { return }
            log("vocal: picked \(a.slug)")
            vocalInput(.pick(a)); return
        }
        if key == wishOptionKey { if model.visible { wishRequested() }; return }
        guard model.visible, let o = model.options.first(where: { $0.key == key }) else { return }
        if claude, o.offersClaude { startClaude(key, via: via); return }
        // W16: a page action's tap issues the single-use ticket oi-hud requires before it AXPresses anything
        // (`ax_press`). One tap: the outward gate's panel (oi/gate.py) is the one confirmation; an irreversible label
        // presses only with the confirmed ticket its validate issues (`outwardGate.onValidatedTarget`).
        let now = ProcessInfo.processInfo.systemUptime
        if let t = o.target, PageLabel.target(t) != nil { pressGuard.issue(target: t, confirmed: false, at: now) }
        finish(.pressed(key: key, via: via))
    }

    // MARK: page actions (W16)

    /// `page` and `ax_press` from `oi live` (main thread). The AX work runs on PageAX's queue; the press
    /// ticket is redeemed HERE first, so nothing is ever pressed without the user's tap (a dry run presses nothing).
    func handlePage(_ result: Result<PageCommand, PressOutcome>, from conn: Connection) {
        switch result {
        case .failure(let o):
            conn.sendLine(o.jsonLine())
        case .success(.page):
            pageAX.snapshot { snap in
                log("page: \(snap.trusted ? "\(snap.elements.count) elements" : "not trusted")\(snap.reason.map { " (\($0))" } ?? "") in \(snap.ms) ms")
                conn.sendLine(snap.jsonLine())
            }
        case .success(.press(let role, let slug, let host, let dryRun)):
            if !dryRun, let refusal = pressGuard.redeem(role: role, slug: slug, at: ProcessInfo.processInfo.systemUptime) {
                log("ax_press refused: \(refusal)")
                conn.sendLine(PressOutcome(ok: false, reason: refusal).jsonLine())
                return
            }
            pageAX.press(role: role, slug: slug, host: host, dryRun: dryRun) { [weak self] out in
                conn.sendLine(out.jsonLine())
                DispatchQueue.main.async {
                    log("ax_press \(role):\(slug) -> \(out.ok ? (out.dryRun ? "found (dry run)" : "pressed") : "nothing pressed (\(out.reason ?? "?"))")")
                    if !dryRun, let t = out.toast { self?.toast.show(t, color: .systemOrange) }
                }
            }
        }
    }

    // MARK: Claude (W14)

    /// A long press of an option modifier (held alone ≥ hud.long_press_ms).
    private func longPressed(_ m: TapModifier) {
        guard model.visible, vocal.state == .idle, !glide.active, let k = tapKeys[m], (1...maxOptions).contains(k) else { return }
        log("long-press -> option \(k)")
        pressed(k, via: "key", claude: true)
    }

    private func rightClick(at p: NSPoint) {
        guard model.visible, !glide.active else { return }
        let slots = Metrics.slots(model.options, model.positions, badges: model.badges)
        let rel = (x: Double(p.x) - Metrics.panelSize.width / 2, y: Metrics.panelSize.height / 2 - Double(p.y))
        if let k = capsuleHit(rel, slots: slots) { pressed(k, via: "click", claude: true) }
    }

    /// Answer the driver (`pressed` with mode claude), keep the HUD with a spinner on the capsule: ttl paused,
    /// option keys disarmed, only Esc (or the 45 s safety net) hides it until the `result` command arrives.
    private func startClaude(_ key: Int, via: String, path: [Int]? = nil, glide gl: GlideLog? = nil) {
        owner?.send(.pressed(key: key, via: via, mode: "claude", path: path, glide: gl))
        owner = nil
        claudeWork.begin(key: key, at: ProcessInfo.processInfo.systemUptime)
        log("claude: option \(key) (\(via)), waiting for the result")
        let esc = hotkeyPlan(optionKeys: [], hotkeys: settings.current.hotkeys).regs.filter { $0.id == dismissHotkeyID }
        hotkeys.register(esc)
        taps.stop(); tapKeys = [:]
        ttlTimer?.invalidate(); ttlTimer = nil
        model.ringKey = nil
        withAnimation(.easeOut(duration: Metrics.appear)) { model.working = key }
        claudeTimer?.invalidate()
        claudeTimer = Timer.scheduledTimer(withTimeInterval: ClaudeWork.maxWait, repeats: false) { [weak self] _ in
            self?.dismiss("claude_timeout")
        }
    }

    private func claudeResult(key: Int, ok: Bool, text: String?, ttlMs: Int, openDraft: Int? = nil, conn: Connection) {
        switch claudeWork.result(key: key, text: text) {
        case .cancelled:
            conn.send(.dropped(reason: "cancelled"))
        case .hide:
            conn.send(.ok)
            claudeTimer?.invalidate(); claudeTimer = nil
            finish(.dismissed(reason: "claude_done"))
        case .showCard(let t):
            conn.send(.ok)
            claudeTimer?.invalidate(); claudeTimer = nil
            log("claude: result card (\(ok ? "ok" : "fail"), \(t.split(separator: "\n").count) lines)")   // never the text
            model.cardOK = ok
            cardDraft = openDraft
            model.cardOpen = openDraft != nil
            withAnimation(.easeOut(duration: Metrics.appear)) { model.working = nil; model.card = t }
            ttlTimer?.invalidate()
            ttlTimer = Timer.scheduledTimer(withTimeInterval: Double(ttlMs) / 1000, repeats: false) { [weak self] _ in
                self?.dismiss("card_done")
            }
        }
    }

    /// the card's « Open in Mail » — the user's own press, the only moment a draft brings Mail forward.
    /// Runs `oi mail-open-draft --id N` off the main thread (the same runner as vocal-send), then hides the HUD.
    private func openDraftInMail() {
        guard let id = cardDraft else { return }
        cardDraft = nil; model.cardOpen = false
        let env = ProcessInfo.processInfo.environment
        let bin = env["OI_VOCAL_BIN"].flatMap { $0.isEmpty ? nil : $0 }
            ?? settings.current.repoDir + "/projects/os-intent/.venv/bin/oi"
        var argv = ["mail-open-draft", "--id", String(id)]
        if env["OI_VOCAL_DRY_RUN"] == "1" { argv.append("--dry-run") }
        log("claude: open draft in Mail (press)")
        dismiss("card_open")
        DispatchQueue.global(qos: .userInitiated).async {
            let (code, _) = runVocalSend(bin: bin, args: argv)
            if code != 0 { DispatchQueue.main.async { log("claude: open draft in Mail failed (rc \(code))") } }
        }
    }

    /// W14 label enrichment: upgrade labels in place while the same options are still on screen.
    private func updateLabels(_ updates: [LabelUpdate], conn: Connection) {
        if !model.visible, !hintDots.isIdle {             // W19: a hint: relabel what ⌥⌘ would develop
            if hintDots.relabel(updates) > 0 { conn.send(.ok); log("labels updated (hint)") } else { conn.send(.dropped(reason: "stale")) }
            return
        }
        guard model.visible, vocal.state == .idle, !claudeWork.isBusy else { conn.send(.dropped(reason: "not_visible")); return }
        let (opts, n) = LabelUpdate.apply(updates, to: model.options)
        guard n > 0 else { conn.send(.dropped(reason: "stale")); return }
        withAnimation(model.reduceMotion ? .easeInOut(duration: Metrics.appear) : Metrics.spring) { model.options = opts }
        conn.send(.ok)
        log("labels updated (\(n))")
    }

    private var claudeStateName: String {
        switch claudeWork.state {
        case .idle: return "idle"
        case .working(let k, _): return "working:\(k)"
        case .card(let k, _): return "card:\(k)"
        }
    }

    private func dismiss(_ reason: String) {
        guard model.visible else { return }
        guard LiveBound.dismisses(reason, live: liveBound) else { return }   // the user works on the screenshot
        guard claudeWork.allowsDismiss(reason) else { return }   // W14: while Claude works only Esc / hide / timeout
        finish(.dismissed(reason: reason))
    }

    private func finish(_ event: HUDEvent) {
        var event = event
        if glide.active {                                   // W15: a glide ended by Esc, a key, a hide: its log goes along
            if case .dismissed(let r, .none) = event {
                let gl = glide.log(final: nil)
                event = .dismissed(reason: r, glide: gl)
                log("glide: cancelled (\(r), depth \(gl.depth), \(gl.visited.count) visited, \(gl.expanded) sub-rings)")
            }
            endGlide()
        }
        owner?.send(event)
        owner = nil
        liveBound = nil
        hotkeys.unregisterAll()
        let tapsOff = taps.stop(); tapKeys = [:]               // invisible = inert
        log("hide: \(event.jsonLine().trimmingCharacters(in: .newlines))" + (tapsOff ? " (taps OFF)" : ""))
        ttlTimer?.invalidate(); ttlTimer = nil
        tick?.invalidate(); tick = nil
        for e in hintLife.finish() where e == .teardown { overlay.teardown() }   // W11: hints vanish with the HUD
        model.halos = []
        model.hovered = nil
        claudeWork.reset(); claudeTimer?.invalidate(); claudeTimer = nil      // W14
        model.working = nil; model.card = nil; model.ringKey = nil
        cardDraft = nil; model.cardOpen = false
        summonWindow.hidden()
        if let m = keyMonitor { NSEvent.removeMonitor(m); keyMonitor = nil }
        panel.ignoresMouseEvents = true
        withAnimation(.easeIn(duration: Metrics.appear)) { model.visible = false }
        if vocal.isPicking { vocalInput(.cancel) }        // W12: the picker went away (Esc, ttl, pointer, typing)
        let seq = hideSeq
        DispatchQueue.main.asyncAfter(deadline: .now() + Metrics.appear + 0.03) { [weak self] in
            guard let self, self.hideSeq == seq, !self.model.visible else { return }
            self.panel.orderOut(nil)
        }
    }

    // MARK: vocal (W12)

    /// A modifier SET tap from one of the passive monitors: the vocal key when it matches.
    private func vocalSetTapped(_ set: Set<TapModifier>) {
        guard !glide.active, case .tap(let want) = VocalArm(hotkeys: settings.current.hotkeys), set == want else { return }
        vocalKey()
    }

    /// The vocal key (or a click on the 7:30 capsule): open the picker / next page while the HUD is up,
    /// send while recording. Ignored otherwise (it is armed only then anyway).
    private func vocalKey() {
        guard model.visible || vocal.isRecording else { return }
        vocalInput(.vocalKey)
    }

    private func vocalInput(_ i: VocalInput) {
        for effect in vocal.handle(i) {
            switch effect {
            case .showPicker(let page): showPicker(page: page)
            case .closePicker: if model.visible { finish(.dismissed(reason: "vocal_pick")) }
            case .startRecording(let a): startRecording(a)
            case .send(let a): stopAndSend(a)
            case .cancelRecording: cancelRecording()
            case .startTyping: startTyping()
            }
        }
    }

    /// The HUD turns into the agent picker in the same fixed places (+ the vocal capsule: next page).
    /// A live show on screen is answered `dismissed: vocal` (its hud-log row says so).
    private func showPicker(page: Int) {
        let current = settings.current
        if vocalSession == nil {            // context of the moment the key was pressed (oi-hud never in front)
            vocalSession = VocalSession(app: NSWorkspace.shared.frontmostApplication?.localizedName,
                                        desk: SpaceIndex.current())
        }
        owner?.send(.dismissed(reason: "vocal")); owner = nil
        let agents = VocalPicker.options(page: page, hotkeys: current.hotkeys)
        let vocalOption = HUDOption(key: vocalOptionKey, label: vocalOptionLabel, icon: vocalOptionIcon)
        log("vocal: picker page \(page + 1)/\(VocalPicker.pageCount) (\(agents.map(\.label).joined(separator: ", ")))")
        present(agents + [vocalOption], ttlMs: current.gateTTLms, conn: nil)
    }

    private func agentColor(_ a: VocalAgent) -> NSColor {
        let hud = settings.current.hud
        guard let m = VocalPicker.modifier(for: a) else { return .systemGray }
        return .hex(hud.colors[m].dark)
    }

    private func startRecording(_ a: VocalAgent, debugText: String? = nil) {
        let current = settings.current
        var session = vocalSession ?? VocalSession(app: NSWorkspace.shared.frontmostApplication?.localizedName,
                                                   desk: SpaceIndex.current())
        session.agent = a
        do {
            let u = try VocalStore.urls(Date(), slug: a.slug)
            session.audio = u.audio; session.text = u.text
        } catch {
            log("vocal: vocals/ not writable (\(error))")
        }
        vocalSession = session
        vocalPanel.open(agent: a, color: agentColor(a), key: VocalArm.display(current.hotkeys), limitS: current.vocalMaxS,
                        locale: current.wish.locale)
        switch VocalArm(hotkeys: current.hotkeys) {            // the vocal key sends: arm it while recording
        case .tap: _ = vocalTap.start()
        case .chord(let reg): hotkeys.setPermanent(reg, id: vocalHotkeyID)
        case .none: break
        }
        if let debugText {                                     // `oi-hud debug-vocal --pick … --text …`: no microphone
            vocalSession?.transcript = debugText
            vocalSession?.audio = nil
            vocalPanel.setTranscript(debugText)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in self?.vocalInput(.enter) }
            return
        }
        vocalPanel.dictation.start(preferredLocale: current.wish.locale, limitS: current.vocalMaxS, recordTo: session.audio)
    }

    /// The user 2026-10-21 16:32Z: the microphone stops, what was transcribed stays in the field, he types / edits.
    /// No words heard yet = the audio is dropped (nothing worth keeping); else it stays (sent as `(audio: …)`).
    private func startTyping() {
        vocalPanel.dictation.stop()
        vocalSession?.typed = true
        if let s = vocalSession, s.transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, let audio = s.audio {
            try? FileManager.default.removeItem(at: audio)
            vocalSession?.audio = nil
        }
        vocalPanel.beginTyping()
        log("vocal: typing (microphone off)")
    }

    private func endRecording() -> VocalSession? {
        if vocalSession?.typed == true { vocalSession?.transcript = vocalPanel.text }   // the field is the message now
        vocalPanel.dictation.stop()
        vocalPanel.close()
        vocalTap.stop()
        hotkeys.setPermanent(nil, id: vocalHotkeyID)
        defer { vocalSession = nil }
        return vocalSession
    }

    private func cancelRecording() {
        guard let s = endRecording() else { return }
        if let audio = s.audio { try? FileManager.default.removeItem(at: audio) }   // nothing sent, nothing kept
        log("vocal: cancelled")
    }

    /// Stop, then deliver through `oi vocal-send` (a subprocess, off the main thread). An empty transcript
    /// sends nothing (and keeps no audio). Logs lengths and the recipient only, never the text.
    private func stopAndSend(_ a: VocalAgent) {
        guard let s = endRecording() else { return }
        let color = agentColor(a)
        guard let text = VocalText.clean(s.transcript) else {
            if let audio = s.audio { try? FileManager.default.removeItem(at: audio) }
            log("vocal: empty transcript, nothing sent")
            toast.show(s.hint.map { "Not sent: \($0)" } ?? "Nothing heard — not sent", color: color)
            return
        }
        guard let textURL = s.text, (try? VocalStore.writeText(text, to: textURL)) != nil else {
            toast.show("Failed: vocals/ not writable", color: color); return
        }
        var argv = ["vocal-send", "--to", a.slug, "--text-file", textURL.path, "--action-key", "vocal"]   // outward gate
        if let audio = s.audio, FileManager.default.fileExists(atPath: audio.path) { argv += ["--audio", audio.path] }
        if s.typed { argv.append("--typed") }               // typed / edited by hand: the line says so
        if let app = s.app { argv += ["--app", app] }
        if let desk = s.desk { argv += ["--desk", String(desk)] }
        let env = ProcessInfo.processInfo.environment
        if env["OI_VOCAL_DRY_RUN"] == "1" { argv.append("--dry-run") }
        let bin = env["OI_VOCAL_BIN"].flatMap { $0.isEmpty ? nil : $0 }
            ?? settings.current.repoDir + "/.venv/bin/oi"
        log("vocal: sending to \(a.slug) (\(text.count) chars)")
        DispatchQueue.global(qos: .userInitiated).async {
            let (code, out) = runVocalSend(bin: bin, args: argv)
            let r = VocalResult.parse(stdout: out, exitCode: code)
            DispatchQueue.main.async { [weak self] in
                log("vocal: \(r.ok ? (r.dryRun ? "dry run, nothing sent to \(a.slug)" : "sent to \(a.slug)") : "FAILED to \(a.slug): \(r.reason)")")
                self?.toast.show(r.toast(agent: a), color: color)
            }
        }
    }

    /// `oi-hud debug-vocal`: the vocal key without a key; with `pick` + `text`, the whole flow with
    /// `text` as the transcript (no microphone).
    private func debugVocal(pick: String?, text: String?) {
        guard let pick, let a = VocalPicker.agent(slug: pick) else {
            if vocal.isRecording { log("vocal (debug): recording, ignored"); return }
            log("vocal (debug): key")
            vocalInput(.vocalKey); return
        }
        if vocal.state == .idle { vocalInput(.vocalKey) }
        guard vocal.isPicking else { return }
        log("vocal (debug): pick \(a.slug)")
        for effect in vocal.handle(.pick(a)) {
            switch effect {
            case .closePicker: if model.visible { finish(.dismissed(reason: "vocal_pick")) }
            case .startRecording(let ag): startRecording(ag, debugText: text ?? "")
            default: break
            }
        }
    }

    /// W22: a starter option "Ask <agent>" was pressed: the recording panel to that agent (real microphone;
    /// nothing is sent until Enter / the vocal key, Esc cancels). Ignored while a recording is already up.
    private func vocalTo(_ slug: String) {
        guard let a = VocalPicker.agent(slug: slug), !vocal.isRecording else { return }
        if vocal.state == .idle { vocalInput(.vocalKey) }
        guard vocal.isPicking else { return }
        log("vocal: ask \(a.slug) (starter option)")
        for effect in vocal.handle(.pick(a)) {
            switch effect {
            case .closePicker: if model.visible { finish(.dismissed(reason: "vocal_pick")) }
            case .startRecording(let ag): startRecording(ag)
            default: break
            }
        }
    }

    // MARK: glide (W15)

    private var uptime: TimeInterval { ProcessInfo.processInfo.systemUptime }

    /// ⌥⌘ held 250 ms: the glide. On screen already → over the current options; else a summon (its answer draws
    /// the HUD at the press point). Nothing while the HUD is off, paused, the wish / vocal / Claude is busy.
    private func holdStarted() {
        let current = settings.current
        guard !glide.active, current.hudEnabled, !current.paused, !wish.isOpen, vocal.state == .idle,
              !claudeWork.isBusy else { return }
        glide = GlideState(dwellMs: current.hud.dwellMs)
        glide.begin(at: uptime)
        glideSentStart = false
        glideAnchor = NSEvent.mouseLocation
        log("glide: hold" + (debugGlideOn ? " (debug)" : ""))
        if model.visible { enterGlide() } else { summon("hold") }
    }

    /// The HUD is up and gliding: no ttl, no pointer hide, no leash; the owner may prefetch level 1.
    private func enterGlide() {
        guard glide.active, model.visible else { return }
        ttlTimer?.invalidate(); ttlTimer = nil
        if !glideSentStart, let o = owner { o.send(.glideStart); glideSentStart = true }
    }

    private func holdEnded(downAt: TimeInterval) {
        guard glide.active, !debugGlideOn else { return }
        glideRelease(tapWindow: uptime - downAt <= TapDetector.maxSetDuration)
    }

    /// ⌥⌘ released: execute the option under the pointer, cancel on empty space, or (a short hold that touched
    /// nothing) leave the HUD up exactly as the W11 tap would.
    private func glideRelease(tapWindow: Bool) {
        guard glide.active else { return }
        guard model.visible else { endGlide(); return }      // released before the HUD came: the summon goes on (a tap)
        switch glide.release(at: uptime, tapWindow: tapWindow) {
        case .execute(let path): executePath(path, via: "glide")
        case .cancel: glideCancel("glide_cancel")
        case .keepAsTap:
            log("glide: short hold, the HUD stays (tap)")
            endGlide()
            ttlTimer?.invalidate()
            ttlTimer = Timer.scheduledTimer(withTimeInterval: effectiveTTL(ms: glideTTLms), repeats: false) { [weak self] _ in
                self?.dismiss("ttl")
            }
            gate.reset(shownAt: uptime)
        }
    }

    private func glideCancel(_ reason: String) {
        guard glide.active else { return }
        if model.visible { dismiss(reason) } else { endGlide() }
    }

    private func endGlide() {
        glide.end()
        stopDebugGlide()
        debugGlideOn = false; debugHover = nil; glideAnchor = nil
        model.rings = []; model.trail = nil; model.thinkingPath = nil
        model.contextCard = nil; contextSig = nil     // (the cards stay for a next hold on this show)
    }

    /// Run the option at `path` (a glide release, a click on any capsule while gliding). Only ever on a release or
    /// a click, never on hover. A Claude-drafted follow-up goes through the W14 spinner and result card.
    private func executePath(_ path: [Int], via: String) {
        guard glide.active, model.visible, let first = path.first else { return }
        if path.count == 1, !(1...maxOptions).contains(first) {   // the wish / vocal capsule: as its click
            endGlide(); pressed(first, via: via); return
        }
        guard let o = glide.option(at: path, top: model.options) else { glideCancel("glide_cancel"); return }
        let gl = glide.log(final: path)
        log("glide: \(via) -> \(dotted(path)) (depth \(gl.depth), \(gl.visited.count) visited, \(gl.expanded) sub-rings)")
        endGlide()
        if path.count > 1, o.offersClaude { startClaude(first, via: via, path: path, glide: gl); return }
        finish(.pressed(key: first, via: via, path: path, glide: gl))
    }

    /// `g`: a copy of the glide (the check runs inside its own mutating `hover`).
    private func expandable(_ p: [Int], _ g: GlideState) -> Bool {
        guard owner != nil, let first = p.first, (1...maxOptions).contains(first), p.count <= GlideState.maxDepth else { return false }
        return g.option(at: p, top: model.options) != nil
    }

    /// 60 Hz while gliding: hover (real pointer, or the debug path), dwell → `expand`, the thinking arc.
    private func glideTick(_ rel: (x: Double, y: Double), slots: [HUDSlot]) {
        let t = uptime
        let real = glideHit(rel, top: slots, rings: glide.rings)
        panel.ignoresMouseEvents = real == nil
        let path = debugGlideOn ? debugHover : real
        let snapshot = glide
        if let p = glide.hover(path, at: t, expandable: { self.expandable($0, snapshot) }) {
            owner?.send(.expand(path: p))
            log("glide: expand \(dotted(p))")
        }
        glide.expire(at: t)
        refreshContextCard(hover: path)               // 
        if model.trail != path { model.trail = path }
        if model.hovered != nil { model.hovered = nil }
        if model.ringKey != nil { model.ringKey = nil }
        let th = glide.thinking(at: t)
        if model.thinkingPath != th?.path { model.thinkingPath = th?.path }
        if let f = th?.fill, abs(model.thinkingFill - f) >= 0.01 { model.thinkingFill = f }
    }

    /// The driver's follow-ups for `path`: laid out around that capsule (no overlap with the cross or the other
    /// rings, inside the screen) and bloomed in (spring). Answered `ok`, or `dropped: stale` when not awaited.
    private func glideSubring(_ path: [Int], options: [HUDOption], conn: Connection) {
        guard glide.active, model.visible, glide.pending == path else { conn.send(.dropped(reason: "stale")); return }
        let top = Metrics.slots(model.options, model.positions, badges: model.badges)
        func slot(_ p: [Int]) -> HUDSlot? {
            p.count == 1 ? top.first { $0.key == p[0] } : glide.rings.first { $0.parent == Array(p.dropLast()) }?.slot(p.last!)
        }
        guard let parent = slot(path) else { conn.send(.dropped(reason: "stale")); return }
        let from = path.count == 1 ? nil : slot(Array(path.dropLast()))
        let keep = glide.rings.filter { $0.parent.count < path.count && path.starts(with: $0.parent) }
        let avoid = top + keep.flatMap(\.slots) + [HUDSlot(key: Int.min, x: 0, y: 0, width: 48, height: 48, position: nil)]
        let placed = SubRingLayout.place(parent: parent, origin: (from?.x ?? 0, from?.y ?? 0), keys: options.map(\.key),
                                         widths: options.map { Metrics.width($0, badge: "") },
                                         height: Metrics.capsuleSize.height, avoid: avoid, bounds: glideBounds())
        let pairs = zip(options, placed).compactMap { o, s in s.map { (o, $0) } }
        guard glide.subring(path, options: pairs.map { $0.0 }, slots: pairs.map { $0.1 }) else {
            conn.send(.dropped(reason: "stale")); return
        }
        withAnimation(model.reduceMotion ? .easeInOut(duration: Metrics.appear) : Metrics.spring) { model.rings = glide.rings }
        model.thinkingPath = nil
        conn.send(.ok)
        let lost = options.count - pairs.count
        log("glide: sub-ring \(dotted(path)) (\(pairs.count) options" + (lost > 0 ? ", \(lost) did not fit)" : ")"))
    }

    /// the context card of the direction the glide is on, at its level (2 small, 3 big), placed beside its
    /// capsule clear of the cross and the sub-rings; re-placed only when the card, its level or the rings change.
    private func refreshContextCard(hover: [Int]?) {
        if glide.active, model.visible, showItemCard(hover: hover) { return }   // a live item's own card
        guard glide.active, model.visible,
              let cur = ContextCardLevel.current(hover: hover, rings: glide.rings, dwelt: glide.dwelt),
              let card = contextCards[cur.key] else {
            if model.contextCard != nil { withAnimation(.easeOut(duration: Metrics.appear)) { model.contextCard = nil } }
            contextSig = nil
            return
        }
        let level = card.shownLevel(cur.level)
        let sig = "\(card.key).\(level).\(card.hashValue).\(glide.rings.map { dotted($0.parent) })"
        guard sig != contextSig else { return }
        contextSig = sig
        let top = Metrics.slots(model.options, model.positions, badges: model.badges)
        guard let anchor = top.first(where: { $0.key == cur.key }) else { model.contextCard = nil; return }
        let avoid = top.filter { $0.key != cur.key } + glide.rings.flatMap(\.slots)
            + [HUDSlot(key: Int.min, x: 0, y: 0, width: 48, height: 48, position: nil)]
        guard let p = ContextCardLayout.place(anchor: anchor, size: card.size(level: level), avoid: avoid,
                                              bounds: glideBounds()) else { model.contextCard = nil; return }
        withAnimation(model.reduceMotion ? .easeInOut(duration: Metrics.appear) : Metrics.spring) {
            model.contextCard = ShownContextCard(card: card, level: level, x: p.x, y: p.y)
        }
    }

    /// the hovered sub-ring item's own card, beside that item; false = none (the direction's card applies).
    private func showItemCard(hover: [Int]?) -> Bool {
        guard let cur = ContextCardLevel.item(hover: hover, rings: glide.rings, dwelt: glide.dwelt),
              let ring = glide.rings.first(where: { $0.parent == Array(cur.path.dropLast()) }),
              let anchor = ring.slot(cur.path[cur.path.count - 1]) else { return false }
        let level = cur.card.shownLevel(cur.level)
        let sig = "item.\(dotted(cur.path)).\(level).\(cur.card.hashValue).\(glide.rings.map { dotted($0.parent) })"
        if sig == contextSig { return true }
        contextSig = sig
        let top = Metrics.slots(model.options, model.positions, badges: model.badges)
        let avoid = top + glide.rings.flatMap(\.slots).filter { $0 != anchor }
            + [HUDSlot(key: Int.min, x: 0, y: 0, width: 48, height: 48, position: nil)]
        guard let p = ContextCardLayout.place(anchor: anchor, size: cur.card.size(level: level), avoid: avoid,
                                              bounds: glideBounds()) else { model.contextCard = nil; return true }
        withAnimation(model.reduceMotion ? .easeInOut(duration: Metrics.appear) : Metrics.spring) {
            model.contextCard = ShownContextCard(card: cur.card, level: level, x: p.x, y: p.y)
        }
        return true
    }

    /// The area the sub-rings may use, relative to the HUD centre (y-down): the window ∩ the screen's visible frame.
    private func glideBounds() -> (minX: Double, minY: Double, maxX: Double, maxY: Double) {
        let f = panel.frame
        let mid = NSPoint(x: f.midX, y: f.midY)
        let s = (NSScreen.screens.first { NSMouseInRect(mid, $0.frame, false) } ?? NSScreen.main)?.visibleFrame ?? f
        let hw = Double(Metrics.panelSize.width / 2), hh = Double(Metrics.panelSize.height / 2)
        return (max(-hw, Double(s.minX - f.midX)) + 4, max(-hh, Double(f.midY - s.maxY)) + 4,
                min(hw, Double(s.maxX - f.midX)) - 4, min(hh, Double(f.midY - s.minY)) - 4)
    }

    /// `oi-hud debug-glide --path 2,1`: hold, rest on [2] until its sub-ring opens, then on [2,1], release.
    private func debugGlide(_ path: [Int], cancel: Bool) {
        guard !glide.active else { log("glide (debug): already gliding, ignored"); return }
        debugGlideOn = true; debugHover = nil
        holdStarted()
        guard glide.active else { debugGlideOn = false; log("glide (debug): not started (HUD off, paused or busy)"); return }
        log("glide (debug): path \(dotted(path))" + (cancel ? ", then release on empty space" : ""))
        debugPlan = DebugGlidePlan(path: path, cancel: cancel, since: uptime)
        debugTimer?.invalidate()
        let t = Timer(timeInterval: 0.05, repeats: true) { [weak self] _ in self?.debugGlideTick() }
        RunLoop.main.add(t, forMode: .common)
        debugTimer = t
    }

    private func debugGlideTick() {
        guard var plan = debugPlan, glide.active, debugGlideOn else { stopDebugGlide(); return }
        let now = uptime
        defer { if debugPlan != nil { debugPlan = plan } }
        if plan.step < 0 {                                   // wait for the summon's answer
            if model.visible { plan.step = 0; plan.since = now; debugHover = [plan.path[0]] }
            else if now - plan.since > 3 { log("glide (debug): no HUD"); glideCancel("glide_cancel") }
            return
        }
        if plan.step < plan.path.count - 1 {                 // rest until this capsule's sub-ring is open, then go on
            let prefix = Array(plan.path[0...plan.step])
            if glide.rings.contains(where: { $0.parent == prefix }) {
                if plan.ringSeenAt == nil { plan.ringSeenAt = now }
                if now - plan.ringSeenAt! >= 0.6 {
                    plan.step += 1; plan.since = now; plan.ringSeenAt = nil
                    debugHover = Array(plan.path[0...plan.step])
                }
            } else if now - plan.since > glide.dwell + GlideState.expandTimeout + 0.5 {
                log("glide (debug): no sub-ring for \(dotted(prefix))")
                glideCancel("glide_cancel")
            }
            return
        }
        if plan.cancel, debugHover != nil, now - plan.since >= 0.4 { debugHover = nil; plan.since = now; return }
        if now - plan.since >= min(0.6, glide.dwell * 0.9) { // less than the dwell: no further expansion
            stopDebugGlide()
            glideRelease(tapWindow: false)
        }
    }

    private func stopDebugGlide() { debugTimer?.invalidate(); debugTimer = nil; debugPlan = nil }

    // MARK: pointer

    private func screenBounds(for p: NSPoint) -> (minX: Double, minY: Double, maxX: Double, maxY: Double) {
        let s = NSScreen.screens.first { NSMouseInRect(p, $0.frame, false) } ?? NSScreen.main
        let f = s?.frame ?? .zero
        return (f.minX, f.minY, f.maxX, f.maxY)
    }

    /// What must stay on screen (the cross, wish, vocal); the window itself is larger (W15 sub-rings).
    private var halfSize: (w: Double, h: Double) { (Metrics.contentSize.width / 2, Metrics.contentSize.height / 2) }

    private func setCentre(_ c: (x: Double, y: Double)) {
        panel.setFrameOrigin(NSPoint(x: (c.x - Metrics.panelSize.width / 2).rounded(), y: (c.y - Metrics.panelSize.height / 2).rounded()))
    }

    private func placeAtPointer(_ at: NSPoint? = nil) {
        let p = at ?? NSEvent.mouseLocation
        setCentre(clampCentre((p.x, p.y), screen: screenBounds(for: p), halfSize: halfSize))
    }

    private func startTick() {
        tick?.invalidate()
        let t = Timer(timeInterval: 1.0 / 60, repeats: true) { [weak self] _ in self?.onTick() }
        RunLoop.main.add(t, forMode: .common)
        tick = t
    }

    /// 60 Hz, only while visible: fast-travel gate, leash follow, click-through outside capsules.
    private func onTick() {
        let p = NSEvent.mouseLocation
        let f0 = panel.frame
        let slots = Metrics.slots(model.options, model.positions, badges: model.badges)
        let rel0 = (x: Double(p.x - f0.midX), y: Double(f0.midY - p.y))      // screen y-up -> SwiftUI y-down
        if glide.active { glideTick(rel0, slots: slots); return }          // W15: fixed cross, no ttl / pointer hide
        // inside the cross (+ 24 px) moving fast is reaching for a capsule: never a hide (the user 16:5x)
        let fast = gate.feed(t: ProcessInfo.processInfo.systemUptime, x: p.x, y: p.y)
        if fast, pointerHide, !model.cardOpen, !insideCross(rel0, slots: slots) {   // reaching for « Open in Mail »
            dismiss("pointer_fast"); return
        }
        let centre = (x: Double(f0.midX), y: Double(f0.midY))
        let leash = Metrics.reach + 24
        let next = clampCentre(leashStep(centre: centre, pointer: (p.x, p.y), leash: leash, factor: 0.25),
                               screen: screenBounds(for: p), halfSize: halfSize)
        if abs(next.x - centre.x) >= 0.5 || abs(next.y - centre.y) >= 0.5 { setCentre(next) }
        // clicks: the panel takes mouse events only over a capsule's own shape, everything else goes through
        let f = panel.frame
        let hit = capsuleHit((x: Double(p.x - f.midX), y: Double(f.midY - p.y)), slots: slots)
        // the draft card (centred, ≤ 320 wide) takes clicks for its « Open in Mail » button
        let overCard = model.card != nil && model.cardOpen && abs(p.x - f.midX) <= 165 && abs(p.y - f.midY) <= 75
        panel.ignoresMouseEvents = hit == nil && !overCard
        if model.hovered != hit { model.hovered = hit }
        // W14: the long-press ring around the capsule whose modifier is held (Claude-capable options only)
        if !claudeWork.isBusy, let pr = taps.longPressProgress(at: ProcessInfo.processInfo.systemUptime),
           let k = tapKeys[pr.mod], model.claudeKeys.contains(k) {
            if model.ringKey != k || abs(model.ringFill - pr.fill) >= 0.01 { model.ringKey = k; model.ringFill = pr.fill }
        } else if model.ringKey != nil {
            model.ringKey = nil
        }
    }

    // MARK: typing

    private func startKeyMonitor() {
        guard axTrusted, keyMonitor == nil else { return }
        // Global monitors only see keys the hotkeys did not consume; any other key = typing.
        keyMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] _ in
            DispatchQueue.main.async { self?.dismiss("typing") }
        }
    }
}

/// `oi-hud debug-glide`: where the simulated glide is (step -1 = waiting for the HUD).
struct DebugGlidePlan {
    var path: [Int]
    var cancel: Bool
    var step = -1
    var since: TimeInterval
    var ringSeenAt: TimeInterval?
}

/// A path as "2.1" (logs, state).
func dotted(_ p: [Int]) -> String { p.map(String.init).joined(separator: ".") }

/// One vocal in flight: the context captured when the vocal key was first pressed, the files, the text.
struct VocalSession {
    var app: String?
    var desk: Int?
    var agent: VocalAgent?
    var audio: URL?
    var text: URL?
    var transcript = ""
    var hint: String?
    var typed = false          // the panel went to typing mode (the text was typed / edited by hand)
}

/// Runs `oi vocal-send` with the fleet-secret env vars removed (the message goes in a file / on the
/// sender's stdin, never argv). Returns (exit code, stdout).
func runVocalSend(bin: String, args: [String]) -> (Int32, String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: bin)
    p.arguments = args
    var env = ProcessInfo.processInfo.environment
    for k in ["TELEGRAM_STATE_DIR", "TELEGRAM_BOT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"] { env[k] = nil }
    p.environment = env
    let out = Pipe()
    p.standardOutput = out
    p.standardError = FileHandle.nullDevice
    p.standardInput = FileHandle.nullDevice
    do { try p.run() } catch { return (127, #"{"ok": false, "error": "oi not found"}"#) }
    let data = out.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (p.terminationStatus, String(decoding: data, as: UTF8.self))
}
