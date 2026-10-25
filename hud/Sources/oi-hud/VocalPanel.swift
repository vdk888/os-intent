import AppKit
import HUDCore
import OISettings
import QuartzCore

/// W12: the recording panel of "Send a vocal to…". Takes keyboard focus (Enter sends, Esc
/// cancels) without activating oi-hud when it can, like the wish panel. Shows the agent (initial in its
/// key's colour + "To Nova"), the live on-device transcript and the hint. Owns its own Dictation (the
/// wish panel's class: on-device, fr-FR then en-US, here settings `vocal.max_s` (default 5 min) and the audio
/// written to a .caf). The last 15 s replace "listening…" with a gentle "sends in N s" (orange), never a silent cut.
/// The user 2026-10-21 16:32Z: the transcript is an editable field. A key that writes (HUDCore.VocalKeyRule) stops the
/// listening ("Écoute…" -> "Saisie"), keeps the words, focuses the field with the caret at the end and types there.
final class VocalKeyPanel: NSPanel {
    var onCancel: (() -> Void)?
    /// Every keyDown that reaches the panel (listening: the field is not first responder yet).
    var onKey: ((NSEvent) -> Void)?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    override func cancelOperation(_ sender: Any?) { onCancel?() }
    override func keyDown(with event: NSEvent) { onKey?(event) }
}

final class VocalPanelController: NSObject, NSTextFieldDelegate {
    static let size = NSSize(width: 440, height: 104)

    let dictation = Dictation()
    var onEnter: (() -> Void)?
    var onCancel: (() -> Void)?
    /// A key that writes while listening: the flow answers `.startTyping`, then `beginTyping()` replays it.
    var onTyped: (() -> Void)?

    private let panel: VocalKeyPanel
    private let badge = NSTextField(labelWithString: "")
    private let title = NSTextField(labelWithString: "")
    private let transcript = NSTextField(wrappingLabelWithString: "")   // made editable in build()
    private let hint = NSTextField(labelWithString: "")
    private let mic = NSImageView()
    private let listening = NSTextField(labelWithString: "listening…")   // W20
    private var hideSeq = 0
    private var activatedSelf = false
    private var tick: Timer?
    private var startedAt: TimeInterval = 0
    private var limitS: Double = OISettings.defaultVocalMaxS
    private var locale = "fr-FR"
    private var key = ""
    private var agent: VocalAgent?
    private(set) var isOpen = false
    private(set) var typing = false
    private var pendingKey: NSEvent?         // the key that switched to typing, replayed into the field

    /// The message as it stands (typing mode: what the field holds).
    var text: String { transcript.stringValue }

    override init() {
        panel = VocalKeyPanel(contentRect: NSRect(origin: .zero, size: VocalPanelController.size),
                              styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        super.init()
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.isFloatingPanel = true
        panel.becomesKeyOnlyIfNeeded = false
        panel.animationBehavior = .none
        panel.appearance = NSAppearance(named: .vibrantDark)
        panel.onCancel = { [weak self] in self?.onCancel?() }
        panel.onKey = { [weak self] e in self?.handleKey(e) }
        build()
    }

    /// A keyDown that reached the panel (listening; in typing mode only what the field editor did not take).
    private func handleKey(_ e: NSEvent) {
        let m = e.modifierFlags
        switch VocalKeyRule.classify(keyCode: e.keyCode, characters: e.characters,
                                     ignoringModifiers: e.charactersIgnoringModifiers,
                                     command: m.contains(.command), control: m.contains(.control), typing: typing) {
        case .send: onEnter?()
        case .cancel: onCancel?()
        case .startTyping: pendingKey = e; onTyped?()
        case .field, .ignore: break
        }
    }

    /// Typing mode: the field takes focus, the caret goes to the end, the key that started it is typed there.
    func beginTyping() {
        guard isOpen, !typing else { return }
        typing = true
        tick?.invalidate(); tick = nil                   // the time limit is the microphone's: gone with it
        listening.layer?.removeAllAnimations()
        listening.layer?.opacity = 1
        listening.stringValue = VocalModeText.indicator(typing: true, locale: locale)
        listening.textColor = .secondaryLabelColor
        listening.setAccessibilityLabel(listening.stringValue)
        mic.image = NSImage(systemSymbolName: "keyboard", accessibilityDescription: listening.stringValue)
        mic.contentTintColor = .secondaryLabelColor
        hint.stringValue = VocalModeText.hint(typing: true, key: key, limitS: limitS, locale: locale)
        if transcript.placeholderString == "Listening…" {          // a dictation hint ("… type instead") stays
            transcript.placeholderString = locale.lowercased().hasPrefix("fr") ? "Votre message…" : "Your message…"
        }
        transcript.refusesFirstResponder = false
        transcript.isEditable = true
        transcript.isSelectable = true
        panel.makeFirstResponder(transcript)
        if let ed = transcript.currentEditor() {
            ed.selectedRange = NSRange(location: (transcript.stringValue as NSString).length, length: 0)
            if let e = pendingKey { ed.keyDown(with: e) }
        }
        pendingKey = nil
    }

    // NSTextFieldDelegate: Enter / Esc inside the field (typing mode)
    func control(_ control: NSControl, textView: NSTextView, doCommandBy sel: Selector) -> Bool {
        if sel == #selector(NSResponder.insertNewline(_:)) || sel == #selector(NSResponder.insertNewlineIgnoringFieldEditor(_:)) {
            onEnter?(); return true
        }
        if sel == #selector(NSResponder.cancelOperation(_:)) { onCancel?(); return true }
        return false
    }

    private func build() {
        let size = VocalPanelController.size
        // W19: Liquid Glass on macOS 26+ (NSGlassEffectView), else the HUD material as before
        let bg: NSView
        if let g = glassPanelRoot(size: size, radius: 14) {
            panel.contentView = g.root; bg = g.content
        } else {
            let ve = NSVisualEffectView(frame: NSRect(origin: .zero, size: size))
            ve.material = .hudWindow
            ve.blendingMode = .behindWindow
            ve.state = .active
            ve.maskImage = WishPanelController.roundedMask(radius: 14)
            ve.wantsLayer = true
            panel.contentView = ve; bg = ve
        }
        // end W19

        badge.alignment = .center
        badge.font = .systemFont(ofSize: 12, weight: .bold)
        badge.textColor = NSColor.black.withAlphaComponent(0.85)
        badge.wantsLayer = true
        badge.layer?.cornerRadius = 11
        badge.frame = NSRect(x: 16, y: size.height - 34, width: 22, height: 22)
        bg.addSubview(badge)

        title.font = .systemFont(ofSize: 13, weight: .semibold)
        title.frame = NSRect(x: 46, y: size.height - 33, width: size.width - 46 - 48 - 88, height: 18)
        bg.addSubview(title)

        mic.image = NSImage(systemSymbolName: "mic.fill", accessibilityDescription: "Recording")
        mic.contentTintColor = .systemRed
        mic.frame = NSRect(x: size.width - 40, y: size.height - 36, width: 24, height: 24)
        bg.addSubview(mic)

        // W20: a small "listening…" next to the mic, softly pulsing while the recording runs (static with Reduce Motion)
        listening.font = .systemFont(ofSize: 11, weight: .medium)
        listening.textColor = NSColor.systemRed.withAlphaComponent(0.9)
        listening.alignment = .right
        listening.frame = NSRect(x: size.width - 40 - 84, y: size.height - 31, width: 80, height: 15)
        listening.wantsLayer = true
        bg.addSubview(listening)

        transcript.font = .systemFont(ofSize: 15)
        transcript.maximumNumberOfLines = 2
        transcript.lineBreakMode = .byTruncatingHead      // the newest words stay visible
        transcript.delegate = self
        transcript.focusRingType = .none
        transcript.setAccessibilityRole(.textField)        // a real text field for VoiceOver, editable once typing
        transcript.frame = NSRect(x: 16, y: 26, width: size.width - 32, height: 40)
        bg.addSubview(transcript)

        hint.font = .systemFont(ofSize: 11)
        hint.textColor = .secondaryLabelColor
        hint.frame = NSRect(x: 16, y: 8, width: size.width - 32, height: 16)
        bg.addSubview(hint)
    }

    /// Opens at the pointer for `agent`, in the key's colour `color`; `key` = the vocal key display.
    func open(agent: VocalAgent, color: NSColor, key: String, limitS: Double, locale: String) {
        hideSeq += 1
        isOpen = true
        typing = false
        pendingKey = nil
        self.agent = agent; self.key = key; self.locale = locale
        transcript.isEditable = false                      // listening: keys come to the panel (VocalKeyRule)
        transcript.isSelectable = false
        transcript.refusesFirstResponder = true
        transcript.setAccessibilityLabel(VocalModeText.fieldLabel(agent: agent, locale: locale))
        mic.image = NSImage(systemSymbolName: "mic.fill", accessibilityDescription: "Recording")
        mic.contentTintColor = .systemRed
        badge.stringValue = agent.initial
        badge.layer?.backgroundColor = color.cgColor
        title.stringValue = "To \(agent.name)"
        title.textColor = color
        transcript.stringValue = ""
        transcript.placeholderString = "Listening…"
        hint.stringValue = VocalModeText.hint(typing: false, key: key, limitS: limitS, locale: locale)
        listening.stringValue = VocalModeText.indicator(typing: false, locale: locale)
        listening.setAccessibilityLabel(listening.stringValue)
        listening.textColor = NSColor.systemRed.withAlphaComponent(0.9)
        startListeningPulse()
        self.limitS = limitS
        startedAt = ProcessInfo.processInfo.systemUptime
        tick?.invalidate()
        tick = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.updateCountdown() }
        placeAtPointer()
        panel.alphaValue = 0
        panel.makeKeyAndOrderFront(nil)
        if !panel.isKeyWindow {
            NSApp.activate(ignoringOtherApps: true)   // some setups refuse key status to a non-activating panel
            activatedSelf = true
            panel.makeKeyAndOrderFront(nil)
        }
        panel.makeFirstResponder(nil)                      // the panel itself gets the keys until typing starts
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = Metrics.appear
            panel.animator().alphaValue = 1
        }
    }

    func setTranscript(_ text: String) { transcript.stringValue = text }

    /// The last `vocalCountdownS` seconds: "sends in N s" in orange (static, no pulse) instead of "listening…".
    private func updateCountdown() {
        guard isOpen, !typing else { tick?.invalidate(); tick = nil; return }
        let elapsed = ProcessInfo.processInfo.systemUptime - startedAt
        guard let t = VocalCountdown.text(elapsed: elapsed, limitS: limitS) else { return }
        if listening.stringValue == VocalModeText.indicator(typing: false, locale: locale) {
            listening.layer?.removeAllAnimations()
            listening.layer?.opacity = 1
            listening.textColor = NSColor.systemOrange
        }
        listening.stringValue = t
    }

    func setHint(_ text: String) { transcript.placeholderString = text }

    private func startListeningPulse() {
        listening.layer?.removeAllAnimations()
        guard !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else { return }
        let a = CABasicAnimation(keyPath: "opacity")
        a.fromValue = 1.0; a.toValue = 0.35
        a.duration = 0.8; a.autoreverses = true; a.repeatCount = .infinity
        listening.layer?.add(a, forKey: "pulse")
    }

    func close() {
        guard isOpen else { return }
        isOpen = false
        typing = false
        pendingKey = nil
        panel.makeFirstResponder(nil)
        tick?.invalidate(); tick = nil
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = Metrics.appear
            panel.animator().alphaValue = 0
        }
        let seq = hideSeq
        DispatchQueue.main.asyncAfter(deadline: .now() + Metrics.appear + 0.03) { [weak self] in
            guard let self, self.hideSeq == seq, !self.isOpen else { return }
            self.panel.orderOut(nil)
            self.transcript.stringValue = ""          // the text is not kept on screen longer than needed
            self.listening.layer?.removeAllAnimations()
            if self.activatedSelf { self.activatedSelf = false; NSApp.hide(nil) }   // give focus back
        }
    }

    private func placeAtPointer() {
        let p = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(p, $0.frame, false) } ?? NSScreen.main
        let vf = screen?.visibleFrame ?? .zero
        let size = VocalPanelController.size
        var o = NSPoint(x: p.x - size.width / 2, y: p.y - size.height - 24)
        o.x = min(max(o.x, vf.minX + 8), vf.maxX - size.width - 8)
        o.y = min(max(o.y, vf.minY + 8), vf.maxY - size.height - 8)
        panel.setFrameOrigin(NSPoint(x: o.x.rounded(), y: o.y.rounded()))
    }
}

/// A one-line toast near the pointer ("Sent to Nova" / "Failed: …"), in the agent's colour; never key,
/// click-through, gone after `life` seconds.
final class ToastController {
    private let panel: HUDPanel
    private let label = NSTextField(labelWithString: "")
    private let box = NSView()
    private var seq = 0

    init() {
        panel = HUDPanel(contentRect: NSRect(x: 0, y: 0, width: 300, height: 34),
                         styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.ignoresMouseEvents = true
        panel.animationBehavior = .none
        box.wantsLayer = true
        box.layer?.cornerRadius = 17
        panel.contentView = box
        label.font = .systemFont(ofSize: 12.5, weight: .semibold)
        label.textColor = NSColor.black.withAlphaComponent(0.88)
        label.alignment = .center
        label.lineBreakMode = .byTruncatingTail
        box.addSubview(label)
    }

    func show(_ text: String, color: NSColor, life: TimeInterval = 2.5) {
        seq += 1
        let s = seq
        label.stringValue = text
        let w = min(max(Double(label.intrinsicContentSize.width) + 32, 120), 420)
        let size = NSSize(width: w, height: 34)
        box.layer?.backgroundColor = color.cgColor
        label.frame = NSRect(x: 12, y: 8, width: w - 24, height: 18)
        let p = NSEvent.mouseLocation
        panel.setFrame(NSRect(x: (p.x - w / 2).rounded(), y: (p.y + 28).rounded(), width: w, height: size.height), display: true)
        panel.alphaValue = 0
        panel.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { $0.duration = Metrics.appear; panel.animator().alphaValue = 1 }
        DispatchQueue.main.asyncAfter(deadline: .now() + life) { [weak self] in
            guard let self, self.seq == s else { return }
            NSAnimationContext.runAnimationGroup({ $0.duration = Metrics.appear; self.panel.animator().alphaValue = 0 },
                                                 completionHandler: { [weak self] in
                guard let self, self.seq == s else { return }
                self.panel.orderOut(nil)
            })
        }
    }
}
