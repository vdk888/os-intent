import AppKit
import HUDCore
import OISettings
import QuartzCore

/// Outward gate: "vous allez envoyer ce message à ce destinataire, validé ou pas ?". A small Liquid
/// Glass panel at the pointer: the title ("Envoyer ce message à Orion ?"), 1-3 preview lines, ✓ / ✗ and the hint.
/// Return / keypad Enter / a tap of the action key / ✓ validates; Esc / ✗ / a click outside / the timeout / a newer
/// request cancels. Exactly one `outward` event per request goes back on the asking connection. Takes keyboard
/// focus without activating oi-hud when it can (the wish panel's trick). The preview is never logged.
/// A vocal request with `offer_shot` also shows the toggle "Joindre une capture de l'écran (C)", OFF at every open:
/// C or a click flips it; only a validate carries it (with the display under the pointer).
final class OutwardKeyPanel: NSPanel {
    var onCancel: (() -> Void)?
    var onEnter: (() -> Void)?
    var onToggleShot: (() -> Void)?
    var onFlags: ((NSEvent.ModifierFlags) -> Void)?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    override func cancelOperation(_ sender: Any?) { onCancel?() }
    override func keyDown(with event: NSEvent) {
        if event.isARepeat { return }
        if event.keyCode == 36 || event.keyCode == 76 { onEnter?() }                       // Return / keypad Enter
        else if event.keyCode == 8, onToggleShot != nil { onToggleShot?() }                // C: the screenshot toggle
        else { super.keyDown(with: event) }
    }
    override func flagsChanged(with event: NSEvent) { onFlags?(event.modifierFlags); super.flagsChanged(with: event) }
}

final class OutwardConfirmController: NSObject {
    static let size = NSSize(width: 460, height: 132)

    /// The validated page target: oi-hud issues its press ticket (the gate's validate is the user's own tap).
    var onValidatedTarget: ((String) -> Void)?
    var hotkeys: () -> HotkeySet = { HotkeySet.defaults }

    private let panel: OutwardKeyPanel
    private let title = NSTextField(labelWithString: "")
    private let preview = NSTextField(wrappingLabelWithString: "")
    private let hint = NSTextField(labelWithString: "")
    private let ok = NSButton(title: "✓", target: nil, action: nil)
    private let no = NSButton(title: "✗", target: nil, action: nil)
    private let shot = NSButton(checkboxWithTitle: "", target: nil, action: nil)
    private var request: OutwardConfirmRequest?
    private var reply: ((HUDEvent) -> Void)?
    private var decider = OutwardDecider()
    private var taps: Set<TapModifier>?
    private var tapDetector = TapDetector()
    private var timer: Timer?
    private var mouseMonitor: Any?
    private var resignObserver: NSObjectProtocol?
    private var activatedSelf = false
    private var hideSeq = 0

    var isOpen: Bool { reply != nil }

    override init() {
        panel = OutwardKeyPanel(contentRect: NSRect(origin: .zero, size: OutwardConfirmController.size),
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
        panel.onCancel = { [weak self] in self?.input(.cancel("esc")) }
        panel.onEnter = { [weak self] in self?.input(.validate("return")) }
        panel.onFlags = { [weak self] f in self?.flags(f) }
        panel.onToggleShot = { [weak self] in self?.toggleShot() }
        build()
    }

    private func build() {
        let size = OutwardConfirmController.size
        let bg: NSView                                   // Liquid Glass on macOS 26+, else the HUD material
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
        title.font = .systemFont(ofSize: 15, weight: .semibold)
        title.textColor = .labelColor
        title.lineBreakMode = .byTruncatingTail
        title.frame = NSRect(x: 18, y: size.height - 36, width: size.width - 124, height: 20)
        bg.addSubview(title)

        preview.font = .systemFont(ofSize: 12.5)
        preview.textColor = .secondaryLabelColor
        preview.maximumNumberOfLines = OutwardConfirmRequest.maxPreviewLines
        preview.lineBreakMode = .byTruncatingTail
        preview.frame = NSRect(x: 18, y: 32, width: size.width - 36, height: 56)
        bg.addSubview(preview)

        for (b, x, sel, tint) in [(no, size.width - 98, #selector(clickNo), NSColor.systemRed),
                                  (ok, size.width - 54, #selector(clickOk), NSColor.systemGreen)] {
            b.target = self; b.action = sel
            b.bezelStyle = .circular
            b.font = .systemFont(ofSize: 15, weight: .bold)
            b.contentTintColor = tint
            b.frame = NSRect(x: x, y: size.height - 46, width: 38, height: 34)
            bg.addSubview(b)
        }
        ok.keyEquivalent = ""                            // Return is handled by the panel (arm delay applies)

        shot.target = self; shot.action = #selector(clickShot)
        shot.font = .systemFont(ofSize: 12.5)
        shot.frame = NSRect(x: 16, y: 30, width: size.width - 32, height: 18)
        shot.isHidden = true
        bg.addSubview(shot)

        hint.font = .systemFont(ofSize: 11)
        hint.textColor = .tertiaryLabelColor
        hint.frame = NSRect(x: 18, y: 10, width: size.width - 36, height: 16)
        bg.addSubview(hint)
    }

    // MARK: open / decide

    /// Opens (a request already open is cancelled "replaced"); `reply` gets exactly one `outward` event.
    func open(_ req: OutwardConfirmRequest, reply: @escaping (HUDEvent) -> Void) {
        if isOpen { input(.cancel("replaced"), animate: false) }
        request = req
        self.reply = reply
        hideSeq += 1
        decider.open(at: ProcessInfo.processInfo.systemUptime, offerShot: req.offerShot)
        shot.isHidden = !req.offerShot                   // offered only when python found it safe (oi.vocal.shot_offer_ok)
        shot.title = req.shotLabel
        shot.state = .off
        let size = OutwardConfirmController.size
        preview.frame = req.offerShot ? NSRect(x: 18, y: 52, width: size.width - 36, height: 36)
                                      : NSRect(x: 18, y: 32, width: size.width - 36, height: 56)
        preview.maximumNumberOfLines = req.offerShot ? 2 : OutwardConfirmRequest.maxPreviewLines
        taps = req.actionTaps(hotkeys())
        tapDetector = TapDetector()
        title.stringValue = req.title
        preview.stringValue = req.preview.joined(separator: "\n")
        hint.stringValue = req.hint
        ok.toolTip = req.lang == "en" ? "Validate" : "Valider"
        no.toolTip = req.lang == "en" ? "Cancel" : "Annuler"
        placeAtPointer()
        panel.alphaValue = 0
        panel.makeKeyAndOrderFront(nil)
        if !panel.isKeyWindow {                          // see WishPanelController.open
            NSApp.activate(ignoringOtherApps: true)
            activatedSelf = true
            panel.makeKeyAndOrderFront(nil)
        }
        log("outward gate: open \(req.id) (\(req.cls), \(req.preview.count) preview lines, key: \(panel.isKeyWindow))")
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = Metrics.appear
            panel.animator().alphaValue = 1
        }
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: Double(req.ttlMs) / 1000, repeats: false) { [weak self] _ in
            self?.input(.timeout)
        }
        startOutsideWatch()
    }

    /// `debug_outward` (tests, demo): answer as ✓ / ✗ would (the arm delay still applies to a validate).
    func debugAnswer(validate: Bool) {
        input(validate ? .validate("debug") : .cancel("debug"))
    }

    @objc private func clickOk() { input(.validate("click")) }
    @objc private func clickShot() { toggleShot(fromClick: true) }

    private func toggleShot(fromClick: Bool = false) {
        guard isOpen, request?.offerShot == true else { return }
        let on = decider.toggleShot()
        shot.state = on ? .on : .off                     // a click already flipped it: keep the decider's truth
        log("outward gate: screenshot toggle \(on ? "on" : "off")\(fromClick ? " (click)" : " (C)")")
    }
    @objc private func clickNo() { input(.cancel("click")) }

    private func flags(_ f: NSEvent.ModifierFlags) {
        guard isOpen, let want = taps else { return }
        var held: HeldModifiers = []
        if f.contains(.control) { held.insert(.control) }
        if f.contains(.option) { held.insert(.option) }
        if f.contains(.command) { held.insert(.command) }
        if f.contains(.function) { held.insert(.fn) }
        if f.contains(.shift) { held.insert(.shift) }
        if let got = tapDetector.flagsChanged(held, at: ProcessInfo.processInfo.systemUptime), got == want {
            input(.validate("action_key"))
        }
    }

    private func input(_ i: OutwardDecider.Input, animate: Bool = true) {
        guard let req = request, let r = reply,
              var d = decider.input(i, at: ProcessInfo.processInfo.systemUptime) else { return }
        if d.attachShot { d.display = displayUnderPointer() }
        reply = nil; request = nil
        timer?.invalidate(); timer = nil
        stopOutsideWatch()
        if d.validated, req.cls == "page", let t = req.target { onValidatedTarget?(t) }
        r(.outward(id: req.id, decision: d))
        log("outward gate: \(req.id) \(d.validated ? "validated" : "cancelled") (\(d.reason))"
            + (d.attachShot ? " + screenshot (display \(d.display.map(String.init) ?? "main"))" : ""))
        let seq = hideSeq
        let done = { [weak self] in
            guard let self, self.hideSeq == seq, self.reply == nil else { return }
            self.panel.orderOut(nil)
            self.preview.stringValue = ""                // not kept in memory longer than needed
            if self.activatedSelf { self.activatedSelf = false; NSApp.hide(nil) }
        }
        if !animate { done(); return }
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = Metrics.appear
            panel.animator().alphaValue = 0
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + Metrics.appear + 0.03, execute: done)
    }

    // MARK: placement, click outside

    /// The display under the pointer as `screencapture -D` numbers it (1-based, CGGetActiveDisplayList order,
    /// the main display first); nil = let python capture the main display.
    private func displayUnderPointer() -> Int? {
        let p = NSEvent.mouseLocation
        guard let screen = NSScreen.screens.first(where: { NSMouseInRect(p, $0.frame, false) }),
              let n = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else { return nil }
        var count: UInt32 = 0
        guard CGGetActiveDisplayList(0, nil, &count) == .success, count > 0 else { return nil }
        var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
        guard CGGetActiveDisplayList(count, &ids, &count) == .success else { return nil }
        return ids.prefix(Int(count)).firstIndex(of: CGDirectDisplayID(n.uint32Value)).map { $0 + 1 }
    }

    private func placeAtPointer() {
        let p = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(p, $0.frame, false) } ?? NSScreen.main
        let vf = screen?.visibleFrame ?? .zero
        let size = OutwardConfirmController.size
        var o = NSPoint(x: p.x - size.width / 2, y: p.y - size.height - 24)
        o.x = min(max(o.x, vf.minX + 8), vf.maxX - size.width - 8)
        o.y = min(max(o.y, vf.minY + 8), vf.maxY - size.height - 8)
        panel.setFrameOrigin(NSPoint(x: o.x.rounded(), y: o.y.rounded()))
    }

    private func startOutsideWatch() {
        stopOutsideWatch()
        mouseMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]) { [weak self] _ in
            DispatchQueue.main.async { self?.input(.cancel("click_outside")) }
        }
        resignObserver = NotificationCenter.default.addObserver(forName: NSWindow.didResignKeyNotification, object: panel,
                                                                queue: .main) { [weak self] _ in
            self?.input(.cancel("click_outside"))
        }
    }

    private func stopOutsideWatch() {
        if let m = mouseMonitor { NSEvent.removeMonitor(m); mouseMonitor = nil }
        if let o = resignObserver { NotificationCenter.default.removeObserver(o); resignObserver = nil }
    }
}
