import AppKit
import HUDCore
import QuartzCore

/// The wish panel takes keyboard focus for typing WITHOUT activating oi-hud:
/// `.nonactivatingPanel` + `canBecomeKey` + `makeKeyAndOrderFront` (the front app stays front).
final class WishKeyPanel: NSPanel {
    var onCancel: (() -> Void)?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    override func cancelOperation(_ sender: Any?) { onCancel?() }   // Esc outside the field
}

/// "What did you want here?" — one line, a mic button, "Enter saves · Esc cancels".
/// Owns the panel state machine (HUDCore.WishPanelMachine) and the dictation; hands a finished
/// wish to `onSave(text, source, context)`. Main thread only.
final class WishPanelController: NSObject, NSTextFieldDelegate {
    static let size = NSSize(width: 440, height: 92)

    var onSave: ((String, WishSource, WishContext) -> Void)?
    /// settings.json `wish.locale`, read when dictation starts.
    var localeProvider: () -> String = { "fr-FR" }

    private let panel: WishKeyPanel
    private let field = NSTextField()
    private let mic = NSButton()
    private let hint = NSTextField(labelWithString: "Enter saves · Esc cancels")
    // W18: wish -> action — the resolver link and the live preview ("→ Telegram") on the hint row
    let resolve = WishResolveController()
    private let preview = NSTextField(labelWithString: "")
    static let defaultHint = "Enter saves · Esc cancels"
    private let dictation = Dictation()
    private var machine = WishPanelMachine()
    private var context = WishContext(app: nil, space: nil, hudVisible: false, hudOptions: [])
    private var dictated = false
    private var mouseMonitor: Any?
    private var resignObserver: NSObjectProtocol?
    private var hideSeq = 0
    private var activatedSelf = false
    static let placeholder = "What did you want here?"

    var stateName: String {
        switch machine.state { case .closed: return "closed"; case .typing: return "typing"; case .dictating: return "dictating" }
    }

    override init() {
        panel = WishKeyPanel(contentRect: NSRect(origin: .zero, size: WishPanelController.size),
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
        panel.onCancel = { [weak self] in self?.input(.escape) }
        buildContent()
        resolve.panel = self                                                   // W18
        dictation.onPartial = { [weak self] text in
            guard let self, self.machine.state == .dictating else { return }
            self.field.stringValue = text
            self.dictated = true
            self.resolve.textChanged(text)                                     // W18: dictation previews too
        }
        dictation.onEnd = { [weak self] hintText in
            guard let self else { return }
            if let hintText { self.field.placeholderString = hintText }
            self.input(.dictationEnded)
        }
    }

    private func buildContent() {
        let size = WishPanelController.size
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

        field.placeholderString = WishPanelController.placeholder
        field.font = .systemFont(ofSize: 17)
        field.isBordered = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.usesSingleLineMode = true
        field.cell?.isScrollable = true
        field.cell?.wraps = false
        field.delegate = self
        field.frame = NSRect(x: 18, y: 40, width: size.width - 18 - 56, height: 26)
        bg.addSubview(field)

        mic.bezelStyle = .regularSquare
        mic.isBordered = false
        mic.imagePosition = .imageOnly
        mic.image = NSImage(systemSymbolName: "mic", accessibilityDescription: "Dictate")
        mic.contentTintColor = .secondaryLabelColor
        mic.target = self
        mic.action = #selector(micPressed)
        mic.toolTip = "Dictate (on-device)"
        mic.frame = NSRect(x: size.width - 48, y: 36, width: 32, height: 32)
        bg.addSubview(mic)

        hint.font = .systemFont(ofSize: 11)
        hint.textColor = .secondaryLabelColor
        hint.frame = NSRect(x: 18, y: 12, width: size.width - 36, height: 16)
        hint.lineBreakMode = .byTruncatingTail
        bg.addSubview(hint)
        // W18: the resolution preview, right-aligned on the hint row
        preview.font = .systemFont(ofSize: 12, weight: .semibold)
        preview.textColor = .controlAccentColor
        preview.alignment = .right
        preview.lineBreakMode = .byTruncatingTail
        preview.frame = NSRect(x: size.width / 2, y: 11, width: size.width / 2 - 18, height: 17)
        bg.addSubview(preview)
    }

    static func roundedMask(radius r: CGFloat) -> NSImage {
        let img = NSImage(size: NSSize(width: 2 * r + 1, height: 2 * r + 1), flipped: false) { rect in
            NSColor.black.setFill()
            NSBezierPath(roundedRect: rect, xRadius: r, yRadius: r).fill()
            return true
        }
        img.capInsets = NSEdgeInsets(top: r, left: r, bottom: r, right: r)
        img.resizingMode = .stretch
        return img
    }

    // MARK: inputs

    /// The wish hotkey (or the HUD's wish option). `context` is used only when the panel opens.
    func hotkey(context: WishContext) {
        if machine.state == .closed { self.context = context }
        input(.hotkey)
    }

    var isOpen: Bool { machine.state != .closed }

    @objc private func micPressed() { input(.mic) }

    func input(_ i: WishPanelInput) {
        for effect in machine.handle(i) {
            switch effect {
            case .open: open()
            case .startDictation:
                field.placeholderString = "Listening… (Enter saves, 20 s max)"
                setMic(active: true)
                dictation.start(preferredLocale: localeProvider())
            case .stopDictation:
                dictation.stop()
                setMic(active: false)
                if field.placeholderString?.hasPrefix("Listening") == true { field.placeholderString = WishPanelController.placeholder }
            case .save:
                let text = field.stringValue
                close()
                onSave?(text, dictated ? .dictated : .typed, context)
            case .cancel:
                close()
            }
        }
    }

    private func setMic(active: Bool) {
        mic.image = NSImage(systemSymbolName: active ? "mic.fill" : "mic", accessibilityDescription: "Dictate")
        mic.contentTintColor = active ? .systemRed : .secondaryLabelColor
    }

    // NSTextFieldDelegate: Enter / Esc inside the field
    func control(_ control: NSControl, textView: NSTextView, doCommandBy sel: Selector) -> Bool {
        if sel == #selector(NSResponder.insertNewline(_:)) {
            if resolve.enter(field.stringValue) { return true }               // W18: resolve, then do it
            input(.enter); return true
        }
        if sel == #selector(NSResponder.cancelOperation(_:)) { input(.escape); return true }
        return false
    }

    // MARK: show / hide

    private func open() {
        hideSeq += 1
        field.stringValue = ""
        field.placeholderString = WishPanelController.placeholder
        dictated = false
        setMic(active: false)
        resolve.opened(context: ResolveContext(app: context.app, space: context.space))   // W18
        setPreview(""); setHint(nil)
        placeAtPointer()
        let reduce = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        panel.alphaValue = 0
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(field)
        let keyAlone = panel.isKeyWindow
        if !keyAlone {
            // Some setups refuse key status to a non-activating panel of an inactive app.
            NSApp.activate(ignoringOtherApps: true)
            activatedSelf = true
            panel.makeKeyAndOrderFront(nil)
            panel.makeFirstResponder(field)
        }
        log("wish panel open (key: \(panel.isKeyWindow), without activating: \(keyAlone), app active: \(NSApp.isActive))")
        animate(show: true, reduceMotion: reduce)
        startOutsideWatch()
    }

    private func close() {
        stopOutsideWatch()
        resolve.closed()                                                       // W18
        let reduce = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        animate(show: false, reduceMotion: reduce)
        let seq = hideSeq
        DispatchQueue.main.asyncAfter(deadline: .now() + Metrics.appear + 0.03) { [weak self] in
            guard let self, self.hideSeq == seq, self.machine.state == .closed else { return }
            self.panel.orderOut(nil)
            self.field.stringValue = ""          // the text is not kept in memory longer than needed
            if self.activatedSelf { self.activatedSelf = false; NSApp.hide(nil) }   // give focus back
        }
    }

    /// Same motion as the HUD: 150 ms fade + scale (0.88 → 1); Reduce Motion → fade only.
    private func animate(show: Bool, reduceMotion: Bool) {
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = Metrics.appear
            ctx.timingFunction = CAMediaTimingFunction(name: show ? .easeOut : .easeIn)
            panel.animator().alphaValue = show ? 1 : 0
        }
        guard !reduceMotion, let layer = panel.contentView?.layer else { return }
        let s = CABasicAnimation(keyPath: "transform.scale")
        s.fromValue = show ? 0.88 : 1.0
        s.toValue = show ? 1.0 : 0.88
        s.duration = Metrics.appear
        s.timingFunction = CAMediaTimingFunction(name: show ? .easeOut : .easeIn)
        let b = layer.bounds
        layer.anchorPoint = CGPoint(x: 0.5, y: 0.5)
        layer.position = CGPoint(x: b.midX, y: b.midY)
        layer.add(s, forKey: "scale")
        layer.transform = show ? CATransform3DIdentity : CATransform3DMakeScale(0.88, 0.88, 1)
    }

    private func placeAtPointer() {
        let p = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(p, $0.frame, false) } ?? NSScreen.main
        let vf = screen?.visibleFrame ?? .zero
        let size = WishPanelController.size
        // just below-right of the pointer, so the pointer target stays visible; clamped on screen
        var o = NSPoint(x: p.x - size.width / 2, y: p.y - size.height - 24)
        o.x = min(max(o.x, vf.minX + 8), vf.maxX - size.width - 8)
        o.y = min(max(o.y, vf.minY + 8), vf.maxY - size.height - 8)
        panel.setFrameOrigin(NSPoint(x: o.x.rounded(), y: o.y.rounded()))
    }

    // MARK: click outside

    private func startOutsideWatch() {
        stopOutsideWatch()
        // clicks in other apps (global monitors for mouse events need no permission)
        mouseMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]) { [weak self] _ in
            DispatchQueue.main.async { self?.input(.clickOutside) }
        }
        // focus taken away some other way (⌘-Tab, another panel)
        resignObserver = NotificationCenter.default.addObserver(forName: NSWindow.didResignKeyNotification, object: panel,
                                                                queue: .main) { [weak self] _ in
            self?.input(.clickOutside)
        }
    }

    // MARK: W18 wish -> action

    func controlTextDidChange(_ obj: Notification) { resolve.textChanged(field.stringValue) }
    func setPreview(_ s: String) { preview.stringValue = s }
    func setHint(_ s: String?) { hint.stringValue = s ?? WishPanelController.defaultHint; hint.toolTip = s }
    /// The resolution is done (or noted): save the wish as Enter always did, and close.
    func commitSave() { if machine.state != .closed { input(.enter) } }
    /// `debug_wish`: type `text` as if typed (tests, demo).
    func debugType(_ text: String) { field.stringValue = text; resolve.textChanged(text) }
    func debugEnter() { if machine.state != .closed, !resolve.enter(field.stringValue) { input(.enter) } }

    private func stopOutsideWatch() {
        if let m = mouseMonitor { NSEvent.removeMonitor(m); mouseMonitor = nil }
        if let o = resignObserver { NotificationCenter.default.removeObserver(o); resignObserver = nil }
    }
}
