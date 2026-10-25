import AppKit
import HUDCore
import QuartzCore

/// W17: the screenshot note panel (Annotate / the tier-2 Claude note), the wish panel's look with a
/// multi-line field: title ("→ Atlas · Claude note"), the note (editable, prefilled), "Enter sends · ⇧Enter new
/// line · Esc cancels". Takes keyboard focus without activating oi-hud (same trick as the wish panel). The
/// reply goes back on the connection that asked: `note` (Enter) or `dismissed`. The text is never logged.
final class ShotNotePanelController: NSObject, NSTextViewDelegate {
    static let size = NSSize(width: 500, height: 176)

    private let panel: WishKeyPanel
    private let title = NSTextField(labelWithString: "")
    private let scroll = NSScrollView()
    private let text = NSTextView()
    private let placeholder = NSTextField(labelWithString: "")
    private let hint = NSTextField(labelWithString: "Enter sends · ⇧Enter new line · Esc cancels")
    private var reply: ((HUDEvent) -> Void)?
    private var mouseMonitor: Any?
    private var resignObserver: NSObjectProtocol?
    private var activatedSelf = false
    private var hideSeq = 0

    var isOpen: Bool { reply != nil }

    override init() {
        panel = WishKeyPanel(contentRect: NSRect(origin: .zero, size: ShotNotePanelController.size),
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
        panel.onCancel = { [weak self] in self?.close(.dismissed(reason: "esc")) }
        build()
    }

    private func build() {
        let size = ShotNotePanelController.size
        let bg = NSVisualEffectView(frame: NSRect(origin: .zero, size: size))
        bg.material = .hudWindow
        bg.blendingMode = .behindWindow
        bg.state = .active
        bg.maskImage = WishPanelController.roundedMask(radius: 14)
        bg.wantsLayer = true
        panel.contentView = bg

        title.font = .systemFont(ofSize: 12, weight: .semibold)
        title.textColor = .secondaryLabelColor
        title.frame = NSRect(x: 18, y: size.height - 30, width: size.width - 36, height: 16)
        bg.addSubview(title)

        scroll.frame = NSRect(x: 14, y: 34, width: size.width - 28, height: size.height - 70)
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .noBorder
        text.frame = NSRect(origin: .zero, size: scroll.contentSize)
        text.minSize = NSSize(width: 0, height: scroll.contentSize.height)
        text.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        text.isVerticallyResizable = true
        text.autoresizingMask = [.width]
        text.textContainer?.widthTracksTextView = true
        text.font = .systemFont(ofSize: 14)
        text.textColor = .labelColor
        text.drawsBackground = false
        text.isRichText = false
        text.allowsUndo = true
        text.isAutomaticQuoteSubstitutionEnabled = false
        text.delegate = self
        scroll.documentView = text
        bg.addSubview(scroll)

        placeholder.font = .systemFont(ofSize: 14)
        placeholder.textColor = .tertiaryLabelColor
        placeholder.frame = NSRect(x: 19, y: size.height - 58, width: size.width - 38, height: 20)
        placeholder.lineBreakMode = .byTruncatingTail
        bg.addSubview(placeholder)

        hint.font = .systemFont(ofSize: 11)
        hint.textColor = .secondaryLabelColor
        hint.frame = NSRect(x: 18, y: 10, width: size.width - 36, height: 16)
        bg.addSubview(hint)
    }

    // MARK: open / close

    /// Opens (or replaces) the panel; `reply` gets exactly one event.
    func open(_ req: ShotNoteRequest, reply: @escaping (HUDEvent) -> Void) {
        if isOpen { close(.dismissed(reason: "replaced"), animate: false) }
        self.reply = reply
        hideSeq += 1
        title.stringValue = req.title
        text.string = req.text
        placeholder.stringValue = req.placeholder
        placeholder.isHidden = !req.text.isEmpty
        placeAtPointer()
        panel.alphaValue = 0
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(text)
        if !panel.isKeyWindow {                       // see WishPanelController.open
            NSApp.activate(ignoringOtherApps: true)
            activatedSelf = true
            panel.makeKeyAndOrderFront(nil)
            panel.makeFirstResponder(text)
        }
        text.setSelectedRange(NSRange(location: (text.string as NSString).length, length: 0))
        log("shot note panel open (\(req.text.count) chars prefilled, key: \(panel.isKeyWindow))")
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = Metrics.appear
            panel.animator().alphaValue = 1
        }
        startOutsideWatch()
    }

    /// `debug_note`: submit as Enter would (`text` nil = what the field holds).
    func debugSubmit(_ t: String?) {
        guard isOpen else { return }
        if let t { text.string = t }
        send()
    }

    private func send() {
        let t = ShotNoteRequest.clean(text.string)
        log("shot note sent (\(t.count) chars)")
        close(.note(text: t))
    }

    private func close(_ event: HUDEvent, animate: Bool = true) {
        guard let r = reply else { return }
        reply = nil
        stopOutsideWatch()
        r(event)
        if case .dismissed(let why) = event { log("shot note panel closed (\(why))") }
        let seq = hideSeq
        let done = { [weak self] in
            guard let self, self.hideSeq == seq, self.reply == nil else { return }
            self.panel.orderOut(nil)
            self.text.string = ""                    // not kept in memory longer than needed
            if self.activatedSelf { self.activatedSelf = false; NSApp.hide(nil) }
        }
        if !animate { done(); return }
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = Metrics.appear
            panel.animator().alphaValue = 0
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + Metrics.appear + 0.03, execute: done)
    }

    // MARK: keys

    func textView(_ textView: NSTextView, doCommandBy sel: Selector) -> Bool {
        let flags = NSApp.currentEvent?.modifierFlags ?? []
        let isReturn = sel == #selector(NSResponder.insertNewline(_:))
            || sel == #selector(NSResponder.insertNewlineIgnoringFieldEditor(_:))
        let key = ShotNoteKey.classify(isReturn: isReturn, isEscape: sel == #selector(NSResponder.cancelOperation(_:)),
                                       shift: flags.contains(.shift), option: flags.contains(.option))
        switch key {
        case .send: send(); return true
        case .cancel: close(.dismissed(reason: "esc")); return true
        case .newline: textView.insertText("\n", replacementRange: textView.selectedRange()); return true
        case .other: return false
        }
    }

    func textDidChange(_ notification: Notification) {
        placeholder.isHidden = !text.string.isEmpty
    }

    // MARK: placement, click outside

    private func placeAtPointer() {
        let p = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(p, $0.frame, false) } ?? NSScreen.main
        let vf = screen?.visibleFrame ?? .zero
        let size = ShotNotePanelController.size
        var o = NSPoint(x: p.x - size.width / 2, y: p.y - size.height - 24)
        o.x = min(max(o.x, vf.minX + 8), vf.maxX - size.width - 8)
        o.y = min(max(o.y, vf.minY + 8), vf.maxY - size.height - 8)
        panel.setFrameOrigin(NSPoint(x: o.x.rounded(), y: o.y.rounded()))
    }

    private func startOutsideWatch() {
        stopOutsideWatch()
        mouseMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]) { [weak self] _ in
            DispatchQueue.main.async { self?.close(.dismissed(reason: "click_outside")) }
        }
        resignObserver = NotificationCenter.default.addObserver(forName: NSWindow.didResignKeyNotification, object: panel,
                                                                queue: .main) { [weak self] _ in
            self?.close(.dismissed(reason: "click_outside"))
        }
    }

    private func stopOutsideWatch() {
        if let m = mouseMonitor { NSEvent.removeMonitor(m); mouseMonitor = nil }
        if let o = resignObserver { NotificationCenter.default.removeObserver(o); resignObserver = nil }
    }
}
