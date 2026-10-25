import AppKit
import ApplicationServices
import OIWatcherCore

/// Safari front tab. Polls only while Safari is frontmost: immediately on activation,
/// then every 2 s. With Accessibility granted, a cheap AX read of the front window
/// title gates the AppleScript call (run only when the title changed); without it,
/// the AppleScript runs on each 2 s tick and results are de-duplicated.
/// Private windows: NOT detectable through Safari's AppleScript dictionary or AX
/// (no public "private" property), so this is not filtered — see README (amber).
@MainActor
final class SafariSource {
    nonisolated static let bundleID = "com.apple.Safari"
    static let interval: TimeInterval = 2

    private let sink: AWSink
    private var timer: Timer?
    private var lastAXTitle: String?
    private var lastTab: SafariTab?
    private var scriptBlockedUntil: Date = .distantPast
    private var observer: NSObjectProtocol?
    private let script = NSAppleScript(source: """
        tell application id "com.apple.Safari" to get {URL, name} of current tab of front window
        """)

    init(sink: AWSink) { self.sink = sink }

    func start() {
        script?.compileAndReturnError(nil)
        observer = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] note in
            let bid = (note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)?.bundleIdentifier
            MainActor.assumeIsolated { self?.activated(bundleID: bid) }
        }
        if !AXIsProcessTrusted() {
            Hints.once("ax", "safari: Accessibility not granted — polling AppleScript every 2 s while Safari is front (grant Accessibility to oi-watcher to only query on title change)")
        }
        activated(bundleID: NSWorkspace.shared.frontmostApplication?.bundleIdentifier)
        Log.info("safari: watching front tab while Safari is frontmost (poll \(Int(Self.interval)) s)")
    }

    private func activated(bundleID: String?) {
        timer?.invalidate()
        timer = nil
        guard bundleID == Self.bundleID else { return }
        lastAXTitle = nil
        poll(force: true)
        timer = Timer.scheduledTimer(withTimeInterval: Self.interval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.poll(force: false) }
        }
    }

    private func poll(force: Bool) {
        guard let app = NSWorkspace.shared.frontmostApplication, app.bundleIdentifier == Self.bundleID else { return }
        if AXIsProcessTrusted() {
            let title = Self.axFrontWindowTitle(pid: app.processIdentifier)
            if !force, title != nil, title == lastAXTitle { return }
            lastAXTitle = title
        }
        guard Date() >= scriptBlockedUntil, let tab = readTab() else { return }
        guard tab != lastTab else { return }
        lastTab = tab
        let ev = AWEvent.safari(at: Date(), tab)
        let sink = self.sink
        Task { await sink.post(.safari, ev) }
    }

    private func readTab() -> SafariTab? {
        var err: NSDictionary?
        guard let script, let desc = script.executeAndReturnError(&err) as NSAppleEventDescriptor?, err == nil else {
            let code = (err?[NSAppleScript.errorNumber] as? Int) ?? 0
            if code == -1743 || code == -1744 {   // errAEEventNotPermitted / WouldRequireUserConsent
                Hints.once("safari-automation", "safari: Automation not allowed — System Settings → Privacy & Security → Automation → oi-watcher → enable Safari. Backing off 60 s between tries.")
                scriptBlockedUntil = Date().addingTimeInterval(60)
            }
            // -1728 (no front window) and others: nothing to log this tick.
            return nil
        }
        guard desc.numberOfItems >= 2 else { return nil }
        return SafariTab.normalise(rawURL: desc.atIndex(1)?.stringValue, rawTitle: desc.atIndex(2)?.stringValue)
    }

    static func axFrontWindowTitle(pid: pid_t) -> String? {
        let app = AXUIElementCreateApplication(pid)
        var win: CFTypeRef?
        guard AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &win) == .success,
              let win, CFGetTypeID(win) == AXUIElementGetTypeID() else { return nil }
        var title: CFTypeRef?
        guard AXUIElementCopyAttributeValue(win as! AXUIElement, kAXTitleAttribute as CFString, &title) == .success else { return nil }
        return title as? String
    }

    /// For --once: Automation permission state for Safari, never prompting.
    static func probeAutomation() -> String {
        let status = automationStatus()
        switch status {
        case noErr: return "allowed"
        case OSStatus(errAEEventNotPermitted): return "DENIED (-1743) — enable Safari under Automation → oi-watcher"
        case OSStatus(errAEEventWouldRequireUserConsent): return "not yet asked (-1744) — the daemon's first Safari read will prompt; click OK"
        case OSStatus(procNotFound): return "unknown — Safari not running (-600); start Safari and rerun"
        case OSStatus(paramErr): return "unknown (no AE descriptor)"
        default: return "unknown (OSStatus \(status))"
        }
    }

    /// Raw `AEDeterminePermissionToAutomateTarget(Safari, ask: false)`; never prompts.
    /// Thread-safe (no main-actor state): StatusReporter calls it off the main thread.
    nonisolated static func automationStatus() -> OSStatus {
        let target = NSAppleEventDescriptor(bundleIdentifier: bundleID)
        guard let aeDesc = target.aeDesc else { return OSStatus(paramErr) }
        return AEDeterminePermissionToAutomateTarget(aeDesc, typeWildCard, typeWildCard, false)
    }
}
