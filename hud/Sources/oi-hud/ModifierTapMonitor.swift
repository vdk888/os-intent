import AppKit
import CoreGraphics
import HUDCore
import OISettings

/// Bare modifier taps through a passive (listen-only) session event tap on flagsChanged, keyDown,
/// mouse-down and scroll (W11), fed to a HUDCore.TapDetector. Two instances in oi-hud:
/// - W9 the OPTION taps (⌃ ⌥ ⌘ fn = options 1-4): exists ONLY while the HUD is on screen,
///   `start()` on show, `stop()` on every hide (invisible = inert, logged "taps OFF");
/// - W10 the SUMMON tap (W11 default: the ⌥+⌘ set tap): created once when the daemon starts and kept while it
///   runs (retried every 60 s while Input Monitoring is missing). The keyDown/click/scroll events only
///   mark a held modifier as part of a chord (⌥⌘C or ⌥⌘+scroll is not a summon).
/// `onTap` gets the SET of modifiers tapped together; each owner matches what it wants (the option
/// taps a one-modifier set, the summon its configured set).
/// The callback reads the event type and, for flagsChanged, the modifier flags; it never reads a key
/// code or a character, never alters or swallows an event (a chord like ⌘C reaches the app as
/// usual), and nothing is logged about keys. Needs Input Monitoring for oi-hud.app; without it
/// `start()` returns false (the option taps fall back to the Carbon ⌥1-⌥4 hotkeys,
/// HUDCore.hotkeyPlan(tapsAvailable: false); the summon tap is skipped).
final class ModifierTapMonitor {
    private var port: CFMachPort?
    private var source: CFRunLoopSource?
    private var detector = TapDetector()
    private static var requested = false      // one Input Monitoring prompt per daemon run, all monitors
    private var lastOK: Bool?
    private let onLine: String
    private let offLine: String
    /// Delivered on the main queue, after the event callback has returned (W9 review fix).
    var onTap: ((Set<TapModifier>) -> Void)?
    /// W14: the long-press detector (the option taps only; nil = off). Fed with the same events.
    var longPress: LongPressDetector?
    /// W14: a completed long press (one option modifier held alone ≥ hud.long_press_ms), on the main queue.
    var onLongPress: ((TapModifier) -> Void)?

    /// W15: the summon SET held ≥ 250 ms (the summon tap only; nil = off). Fed with the same events.
    var hold: SetHoldDetector?
    /// W15: a hold began (the set has been down 250 ms; its release will not be a tap), on the main queue.
    var onHoldStart: (() -> Void)?
    /// W15: the hold ended (the set released); `downAt` = when the set went down (uptime).
    var onHoldEnd: ((TimeInterval) -> Void)?
    /// W15: a key went down during the hold (Esc, a chord): cancel the glide.
    var onHoldInterrupt: (() -> Void)?

    /// W14: the modifier held and the progress ring's fill, for the HUD's 60 Hz tick.
    func longPressProgress(at t: TimeInterval) -> (mod: TapModifier, fill: Double)? { longPress?.progress(at: t) }

    /// `onLine`: logged when the tap is created; `offLine`: logged once when it cannot be.
    init(onLine: String, offLine: String) { self.onLine = onLine; self.offLine = offLine }

    var isActive: Bool { port != nil }

    /// Creates the tap (idempotent). False = not permitted / not created (logged once per state change).
    func start() -> Bool {
        if port != nil { return true }
        // W15: modifiers already down (⌥⌘ still held after a glide summon) never make a tap / long press / hold
        let now = HeldModifiers(cgEventFlags: UInt64(CGEventSource.flagsState(.combinedSessionState).rawValue))
        detector.prime(now)
        longPress?.prime(now)
        hold?.prime(now)
        guard CGPreflightListenEventAccess() else {
            // From the .app bundle only: one Input Monitoring prompt per daemon run (a bare
            // build in a terminal never prompts).
            if !Self.requested, Bundle.main.bundleURL.pathExtension == "app" {
                Self.requested = true
                _ = CGRequestListenEventAccess()
            }
            report(false, offLine + " (Input Monitoring not granted to oi-hud: System Settings → Privacy & Security → Input Monitoring)")
            return false
        }
        let types: [UInt32] = [CGEventType.flagsChanged.rawValue] + TapDetector.spoilerEventTypes   // + scrollWheel (W11)
        let mask = types.reduce(CGEventMask(0)) { $0 | (CGEventMask(1) << CGEventMask($1)) }
        guard let p = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .listenOnly,
                                        eventsOfInterest: mask, callback: modifierTapCallback,
                                        userInfo: Unmanaged.passUnretained(self).toOpaque()) else {
            report(false, offLine + " (event tap not created: Input Monitoring?)")
            return false
        }
        let src = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, p, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), src, .commonModes)
        CGEvent.tapEnable(tap: p, enable: true)
        port = p; source = src
        report(true, onLine)
        return true
    }

    /// Removes the tap (every hide, for the option taps). True when a tap was actually removed.
    @discardableResult
    func stop() -> Bool {
        guard let p = port else { return false }
        CGEvent.tapEnable(tap: p, enable: false)
        if let s = source { CFRunLoopRemoveSource(CFRunLoopGetMain(), s, .commonModes) }
        CFMachPortInvalidate(p)
        port = nil; source = nil
        detector.reset()
        longPress?.reset()
        hold?.reset()
        return true
    }

    /// A Carbon hotkey fired (⌥` wish, a custom chord): its keyDown may never reach a session
    /// tap, so mark the held modifier as part of a chord explicitly.
    func chordFired() {
        detector.otherInput(); longPress?.otherInput()
        if hold?.otherInput(isKey: true) == .interrupted {
            DispatchQueue.main.async { [weak self] in self?.onHoldInterrupt?() }
        }
    }

    private func report(_ ok: Bool, _ line: String) {
        guard lastOK != ok else { return }
        lastOK = ok
        log(line)
    }

    fileprivate func handle(_ type: CGEventType, _ event: CGEvent) {
        switch type {
        case .tapDisabledByTimeout, .tapDisabledByUserInput:
            if let p = port { CGEvent.tapEnable(tap: p, enable: true) }
        case .flagsChanged:
            let held = HeldModifiers(cgEventFlags: event.flags.rawValue)
            let now = ProcessInfo.processInfo.systemUptime
            if let h = hold?.flagsChanged(held, at: now) {
                switch h {
                case .armed:
                    let wait = hold?.threshold ?? SetHoldDetector.defaultThreshold
                    DispatchQueue.main.asyncAfter(deadline: .now() + wait) { [weak self] in self?.checkHold() }
                case .released(let downAt):
                    DispatchQueue.main.async { [weak self] in self?.onHoldEnd?(downAt) }
                case .interrupted: break
                }
            }
            if let m = longPress?.flagsChanged(held, at: now) {
                DispatchQueue.main.async { [weak self] in self?.onLongPress?(m) }
            }
            if let m = detector.flagsChanged(held, at: now) {
                // never act (hide the HUD, remove this very tap) inside the CGEvent callback
                DispatchQueue.main.async { [weak self] in self?.onTap?(m) }
            }
        default:
            detector.otherInput()        // keyDown / click / scroll: whatever modifier is held is a chord
            longPress?.otherInput()
            if hold?.otherInput(isKey: type == .keyDown) == .interrupted {
                DispatchQueue.main.async { [weak self] in self?.onHoldInterrupt?() }
            }
        }
    }

    /// W15: the hold timer (main queue, as the tap callback): the set still down, nothing else → a hold.
    /// Its release is then not a tap (the summon tap is spoiled here).
    private func checkHold() {
        guard port != nil, hold?.fire(at: ProcessInfo.processInfo.systemUptime) == true else { return }
        detector.otherInput()
        onHoldStart?()
    }
}

/// C callback on the main run loop. Listen-only: the event is always passed through untouched.
private func modifierTapCallback(proxy: CGEventTapProxy, type: CGEventType, event: CGEvent,
                                 refcon: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    if let refcon {
        Unmanaged<ModifierTapMonitor>.fromOpaque(refcon).takeUnretainedValue().handle(type, event)
    }
    return Unmanaged.passUnretained(event)
}
