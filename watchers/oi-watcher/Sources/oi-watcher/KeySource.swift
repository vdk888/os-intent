import AppKit
import Carbon.HIToolbox
import OIWatcherCore

/// Listen-only session event tap on keyDown. The callback checks the ⌘ flag FIRST
/// and returns immediately without reading the key code when ⌘ is not held:
/// plain typing is never inspected, formatted, queued or logged.
@MainActor
final class KeySource {
    private let sink: AWSink
    private var tap: CFMachPort?
    private var retryTimer: Timer?

    init(sink: AWSink) { self.sink = sink }

    /// The listen-only tap exists, i.e. Input Monitoring is effective for this process.
    var tapActive: Bool { tap != nil }

    func start() {
        if tryCreateTap() { return }
        // Make the binary appear in System Settings → Input Monitoring (one prompt,
        // The user decides), then keep retrying so a later grant takes effect.
        _ = CGRequestListenEventAccess()
        Hints.once("key", "key: event tap not created — grant Input Monitoring (System Settings → Privacy & Security → Input Monitoring → enable oi-watcher); retrying every 30 s. Space and Safari sources keep running.")
        retryTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                if self.tryCreateTap() { self.retryTimer?.invalidate(); self.retryTimer = nil }
            }
        }
    }

    private func tryCreateTap() -> Bool {
        let mask = CGEventMask(1 << CGEventType.keyDown.rawValue)
        guard let port = CGEvent.tapCreate(
            tap: .cgSessionEventTap, place: .headInsertEventTap, options: .listenOnly,
            eventsOfInterest: mask, callback: keyTapCallback,
            userInfo: Unmanaged.passUnretained(self).toOpaque()) else { return false }
        tap = port
        let src = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, port, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), src, .commonModes)
        CGEvent.tapEnable(tap: port, enable: true)
        Log.info("key: listen-only tap active (⌘ chords only)")
        return true
    }

    fileprivate func reenable() {
        if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
    }

    /// Called only for non-repeat keyDown events with ⌘ held (gated in the C callback).
    fileprivate func handleCommandKey(keyCode: UInt16, flags: UInt64) {
        guard let chord = Chord.chord(keyCode: keyCode, cgEventFlags: flags,
                                      layoutCharacter: Self.layoutCharacter(keyCode: keyCode)) else { return }
        let ev = AWEvent.chord(at: Date(), chord)
        let sink = self.sink
        Task { await sink.post(.key, ev) }
    }

    /// Base (unmodified) character of `keyCode` in the current keyboard layout, so
    /// ⌘A on AZERTY is reported as ⌘A (not ⌘Q). Only called once ⌘ is known held.
    static func layoutCharacter(keyCode: UInt16) -> String? {
        guard let src = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
              let ptr = TISGetInputSourceProperty(src, kTISPropertyUnicodeKeyLayoutData) else { return nil }
        let data = Unmanaged<CFData>.fromOpaque(ptr).takeUnretainedValue() as Data
        var dead: UInt32 = 0
        var len = 0
        var chars = [UniChar](repeating: 0, count: 4)
        let status = data.withUnsafeBytes { raw -> OSStatus in
            guard let layout = raw.bindMemory(to: UCKeyboardLayout.self).baseAddress else { return -1 }
            return UCKeyTranslate(layout, keyCode, UInt16(kUCKeyActionDown), 0, UInt32(LMGetKbdType()),
                                  OptionBits(kUCKeyTranslateNoDeadKeysBit), &dead, chars.count, &len, &chars)
        }
        guard status == noErr, len > 0 else { return nil }
        return String(utf16CodeUnits: chars, count: len)
    }

    /// For --once: does the tap work right now? Never prompts.
    static func probe() -> (preflight: Bool, created: Bool) {
        let pre = CGPreflightListenEventAccess()
        guard pre else { return (false, false) }
        let mask = CGEventMask(1 << CGEventType.keyDown.rawValue)
        guard let port = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap,
                                           options: .listenOnly, eventsOfInterest: mask,
                                           callback: { _, _, e, _ in Unmanaged.passUnretained(e) },
                                           userInfo: nil) else { return (true, false) }
        CGEvent.tapEnable(tap: port, enable: false)
        CFMachPortInvalidate(port)
        return (true, true)
    }
}

/// C callback: runs on the main run loop (the source is added to CFRunLoopGetMain).
/// Order matters: the ⌘ flag is checked before the key code is ever read.
private func keyTapCallback(proxy: CGEventTapProxy, type: CGEventType, event: CGEvent,
                            refcon: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    guard let refcon else { return Unmanaged.passUnretained(event) }
    let source = Unmanaged<KeySource>.fromOpaque(refcon).takeUnretainedValue()
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        MainActor.assumeIsolated { source.reenable() }
        return Unmanaged.passUnretained(event)
    }
    let flags = event.flags.rawValue
    guard type == .keyDown,
          ChordModifiers(cgEventFlags: flags).contains(.command),          // ⌘ gate first
          event.getIntegerValueField(.keyboardEventAutorepeat) == 0 else {
        return Unmanaged.passUnretained(event)
    }
    let keyCode = UInt16(truncatingIfNeeded: event.getIntegerValueField(.keyboardEventKeycode))
    MainActor.assumeIsolated { source.handleCommandKey(keyCode: keyCode, flags: flags) }
    return Unmanaged.passUnretained(event)   // listen-only: the event is never altered
}
