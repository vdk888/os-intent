import AppKit
import HUDCore
import OISettings   // W16: PageLabel

/// W18 wish → action, the AppKit side: owns the resolver connection (`oi live` registers with
/// `{"cmd":"resolver"}`), debounces the live preview (150 ms), sends `resolve` requests to that connection ONLY
/// (the wish text never goes to any other client), applies the resolutions to the wish panel (preview, confirm
/// line, "noted", done) and watches the ⇧ tap that confirms an irreversible action. Main thread only.
/// Without a resolver the panel behaves exactly as before (Enter saves the wish).
final class WishResolveController {
    weak var panel: WishPanelController?
    private var machine = WishResolveMachine()
    private var resolver: Connection?
    private var context = ResolveContext(app: nil, space: nil)
    private var debounce: Timer?
    private var timeout: Timer?
    private var finishTimer: Timer?
    private var pendingAction: WishAction?
    private var shiftMonitor: Any?
    private var shiftTap = TapDetector()
    /// W16: the page element of the action awaiting confirmation; the user's confirm issues its press ticket.
    private var pendingPress: String?
    var onConfirmPress: ((String) -> Void)?
    /// W18 follow-up: a cancelled confirm (Esc while "Working…") revokes the press ticket it issued.
    var onCancelPress: ((String) -> Void)?
    private var issuedPress: (target: String, id: Int)?

    var connected: Bool { resolver.map { !$0.isClosed } ?? false }

    /// The socket commands (`openPanel(app)` = the debug path opening the panel as the hotkey would).
    func handle(_ c: W18Command, conn: Connection, openPanel: (String?) -> Void) {
        switch c {
        case .resolver:
            resolver = conn
            conn.send(.ok)
            log("wish resolver registered")
        case .resolution(let r):
            let effects = machine.receive(r)
            if r.state == .confirm, machine.isConfirming { pendingPress = PageLabel.target(r.press) != nil ? r.press : nil }   // W16
            apply(effects)
        case .debugWish(let text, let app, let enter, let confirm):
            conn.send(.ok)
            openPanel(app)
            guard let panel else { return }
            panel.debugType(text)
            guard enter else { return }
            // the preview first (debounce + answer), then Enter; then ⇧ when asked (a confirm needs the answer)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                self?.panel?.debugEnter()
                guard confirm else { return }
                self?.whenConfirming(deadline: Date().addingTimeInterval(resolveFinalTimeoutS)) { self?.confirmKey() }
            }
        }
    }

    private func whenConfirming(deadline: Date, _ body: @escaping () -> Void) {
        if machine.isConfirming { body(); return }
        guard Date() < deadline, panel?.isOpen == true else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in self?.whenConfirming(deadline: deadline, body) }
    }

    // MARK: panel events

    func opened(context: ResolveContext) {
        machine.reset()
        self.context = context
        pendingAction = nil
        pendingPress = nil
        cancelTimers()
    }

    /// The panel closed (saved, Esc, click outside). The pending action stays until the save takes it.
    func closed() {
        apply(machine.closed())                    // W18 follow-up: Esc while "Working…" cancels it
        pendingPress = nil
        cancelTimers()
        stopShiftWatch()
    }

    func textChanged(_ text: String) {
        guard connected else { return }
        apply(machine.textChanged(text))
        debounce?.invalidate()
        debounce = Timer.scheduledTimer(withTimeInterval: Double(resolvePreviewDebounceMs) / 1000, repeats: false) { [weak self] _ in
            guard let self else { return }
            self.apply(self.machine.previewDue(context: self.context))
        }
    }

    /// Enter in the field: true = handled here (the panel stays open until the resolution).
    func enter(_ text: String) -> Bool {
        guard let effects = machine.enter(text, context: context, connected: connected) else { return false }
        debounce?.invalidate()
        apply(effects)
        return true
    }

    func confirmKey() { apply(machine.confirmKey(context: context)) }

    func takeAction() -> WishAction? {
        defer { pendingAction = nil }
        return pendingAction
    }

    // MARK: effects

    private func apply(_ effects: [WishResolveEffect]) {
        for e in effects {
            switch e {
            case .send(let req):
                guard let r = resolver, !r.isClosed else { continue }
                if req.confirmed, let p = pendingPress {                                         // W16: ticket first
                    pendingPress = nil; onConfirmPress?(p); issuedPress = (p, req.id)            // W18 follow-up
                }
                r.send(.resolve(req))
                if req.final {
                    timeout?.invalidate()
                    timeout = Timer.scheduledTimer(withTimeInterval: resolveFinalTimeoutS, repeats: false) { [weak self] _ in
                        guard let self else { return }
                        log("wish: no resolution in \(Int(resolveFinalTimeoutS)) s, noted")
                        self.apply(self.machine.timedOut(id: req.id))
                    }
                }
            case .cancel(let id):
                if let r = resolver, !r.isClosed { r.send(.resolveCancel(id: id)); log("wish: cancelled (request \(id))") }
                if let p = issuedPress, p.id == id { onCancelPress?(p.target); log("wish: press ticket revoked") }
                issuedPress = nil
            case .preview(let s):
                panel?.setPreview(s)
            case .hint(let s):
                panel?.setHint(s)
                if machine.isConfirming { timeout?.invalidate(); startShiftWatch() } else { stopShiftWatch() }
            case .finish(let action, let note, let holdMs):
                issuedPress = nil                                  // W18 follow-up: the resolver answered
                timeout?.invalidate()
                stopShiftWatch()
                pendingAction = action
                panel?.setPreview("")
                panel?.setHint(note)
                log("wish: " + (action.map { "resolved (\($0.tier)) -> \($0.token)" } ?? "not resolved"))   // never the text
                finishTimer?.invalidate()
                finishTimer = Timer.scheduledTimer(withTimeInterval: Double(holdMs) / 1000, repeats: false) { [weak self] _ in
                    self?.panel?.commitSave()
                }
            }
        }
    }

    private func cancelTimers() {
        debounce?.invalidate(); debounce = nil
        timeout?.invalidate(); timeout = nil
        finishTimer?.invalidate(); finishTimer = nil
    }

    // MARK: ⇧ tap = confirm (local monitor: the panel is the key window)

    private func startShiftWatch() {
        guard shiftMonitor == nil else { return }
        shiftTap.reset()
        shiftMonitor = NSEvent.addLocalMonitorForEvents(matching: [.flagsChanged, .keyDown]) { [weak self] ev in
            guard let self else { return ev }
            if ev.type == .keyDown { self.shiftTap.otherInput(); return ev }
            let held = HeldModifiers(cgEventFlags: UInt64(ev.modifierFlags.rawValue))
            if let set = self.shiftTap.flagsChanged(held, at: ev.timestamp), set == [.shift] {
                DispatchQueue.main.async { self.confirmKey() }
            }
            return ev
        }
    }

    private func stopShiftWatch() {
        if let m = shiftMonitor { NSEvent.removeMonitor(m); shiftMonitor = nil }
    }
}
