import AppKit
import ApplicationServices
import HUDCore
import OISettings

/// W16 page actions: Accessibility read of Safari's front page and the AXPress of one element.
///
/// READ: only when Safari is the frontmost app and oi-hud is trusted for Accessibility. From the focused
/// window, the first AXWebArea (the page); inside it, every AXButton / AXLink (and button variants): role,
/// label (AXTitle, else AXDescription, else the element's OWN static text, ≤ 80 chars) and frame. Never
/// read: text fields, their values, page text outside a button or link. Bounded: ≤ `maxNodes` nodes,
/// ≤ `budget` seconds, ≤ `maxElements` elements, 0.25 s AX messaging timeout. Runs on its own serial queue
/// (never the main thread). Not trusted -> `trusted: false`, nothing read (the menubar shows the hint).
///
/// PRESS: re-read the page NOW, check the host, re-find by role + label slug (never a stored coordinate),
/// AXPress. The caller (HUDController) first redeems the tap ticket (PressGuard) on the main thread.
final class PageAX {
    static let safariID = "com.apple.Safari"
    static let maxNodes = 5000
    static let maxElements = 300
    static let budget: TimeInterval = 0.45
    private static let containerSkip: Set<String> = ["AXMenuBar", "AXToolbar", "AXMenu"]
    private static let textRoles: Set<String> = ["AXTextField", "AXTextArea", "AXSecureTextField", "AXComboBox", "AXSearchField"]

    private let queue = DispatchQueue(label: "oi-hud.page-ax", qos: .userInitiated)
    private var cache: (snap: PageSnapshot, at: TimeInterval)?      // only touched on `queue`

    struct Found { let ax: AXUIElement; let element: PageElement }

    // MARK: API (any thread; the work runs on `queue`)

    /// The summon key: start reading now, `oi live`'s `page` request ~50 ms later gets this result.
    func prefetch() {
        guard NSWorkspace.shared.frontmostApplication?.bundleIdentifier == Self.safariID, AXIsProcessTrusted() else { return }
        queue.async { [weak self] in
            let snap = Self.read().snap
            self?.cache = (snap, ProcessInfo.processInfo.systemUptime)
        }
    }

    /// The snapshot (a prefetch younger than `maxAge` is reused, the serial queue waits for one in flight).
    func snapshot(maxAge: TimeInterval = 1.5, reply: @escaping (PageSnapshot) -> Void) {
        queue.async { [weak self] in
            let now = ProcessInfo.processInfo.systemUptime
            if let c = self?.cache, now - c.at <= maxAge {
                reply(c.snap); return
            }
            let snap = Self.read().snap
            self?.cache = (snap, now)
            reply(snap)
        }
    }

    func press(role: String, slug: String, host: String?, dryRun: Bool, reply: @escaping (PressOutcome) -> Void) {
        queue.async { [weak self] in
            self?.cache = nil                       // a press changes the page
            reply(Self.press(role: role, slug: slug, host: host, dryRun: dryRun))
        }
    }

    // MARK: read

    static func read() -> (snap: PageSnapshot, found: [Found], viewport: CGRect?) {
        let t0 = Date()
        func ms() -> Int { Int(Date().timeIntervalSince(t0) * 1000) }
        guard AXIsProcessTrusted() else { return (PageSnapshot(trusted: false, reason: "not_trusted"), [], nil) }
        guard let front = NSWorkspace.shared.frontmostApplication else {
            return (PageSnapshot(trusted: true, reason: "not_safari"), [], nil)
        }
        guard front.bundleIdentifier == safariID else {
            return (PageSnapshot(trusted: true, app: front.localizedName, reason: "not_safari"), [], nil)
        }
        let app = AXUIElementCreateApplication(front.processIdentifier)
        AXUIElementSetMessagingTimeout(app, 0.25)
        guard let win = (value(app, kAXFocusedWindowAttribute) ?? value(app, kAXMainWindowAttribute)).flatMap(asElement),
              let web = webArea(in: win) else {
            return (PageSnapshot(trusted: true, app: "Safari", reason: "no_page", ms: ms()), [], nil)
        }
        let host = pageHost((value(web, "AXURL") as? URL) ?? (value(web, "AXURL") as? String).flatMap(URL.init(string:)))
        let viewport = frame(web)
        var found: [Found] = []
        var nodes = 0
        var stack: [AXUIElement] = [web]
        let deadline = t0.addingTimeInterval(budget)
        while let e = stack.popLast(), nodes < maxNodes, found.count < maxElements, Date() < deadline {
            nodes += 1
            let attrs = multi(e, [kAXRoleAttribute, kAXChildrenAttribute])
            let axRole = attrs[0] as? String
            if let r = axRole, textRoles.contains(r) { continue }          // never read a field or below it
            if let role = PageLabel.role(ax: axRole) {
                if let label = label(e), !PageLabel.slug(label).isEmpty, let f = frame(e), f.width > 0, f.height > 0 {
                    found.append(Found(ax: e, element: PageElement(role: role, label: label, frame: f)))
                }
                continue                                                    // a button's insides are its label
            }
            if let kids = attrs[1] as? [AXUIElement] { stack.append(contentsOf: kids.reversed()) }   // page order
        }
        let snap = PageSnapshot(trusted: true, app: "Safari", host: host, elements: found.map(\.element),
                                reason: nodes >= maxNodes || Date() >= deadline ? "truncated" : nil, ms: ms())
        return (snap, found, viewport)
    }

    /// BFS from the window to the first AXWebArea (the page), skipping menus and toolbars.
    private static func webArea(in win: AXUIElement) -> AXUIElement? {
        var queue: [(AXUIElement, Int)] = [(win, 0)]
        var seen = 0
        while !queue.isEmpty, seen < 400 {
            let (e, d) = queue.removeFirst()
            seen += 1
            let a = multi(e, [kAXRoleAttribute, kAXChildrenAttribute])
            let role = a[0] as? String
            if role == "AXWebArea" { return e }
            if let r = role, containerSkip.contains(r) || d >= 10 { continue }
            for k in (a[1] as? [AXUIElement]) ?? [] { queue.append((k, d + 1)) }
        }
        return nil
    }

    /// AXTitle, else AXDescription, else the element's own static text (depth ≤ 3), trimmed, ≤ 80 chars.
    private static func label(_ e: AXUIElement) -> String? {
        let a = multi(e, [kAXTitleAttribute, kAXDescriptionAttribute])
        for v in a {
            if let s = (v as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty { return String(s.prefix(80)) }
        }
        var parts: [String] = []
        func collect(_ x: AXUIElement, _ d: Int) {
            guard d <= 3, parts.joined(separator: " ").count < 80 else { return }
            for k in (value(x, kAXChildrenAttribute) as? [AXUIElement]) ?? [] {
                let r = value(k, kAXRoleAttribute) as? String
                if let r, textRoles.contains(r) { continue }
                if r == "AXStaticText", let s = (value(k, kAXValueAttribute) as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty {
                    parts.append(s)
                } else {
                    collect(k, d + 1)
                }
            }
        }
        collect(e, 1)
        let s = parts.joined(separator: " ")
        return s.isEmpty ? nil : String(s.prefix(80))
    }

    // MARK: press

    static func press(role: String, slug: String, host: String?, dryRun: Bool) -> PressOutcome {
        let (snap, found, viewport) = read()
        if !snap.trusted { return PressOutcome(ok: false, reason: "not_trusted", dryRun: dryRun) }
        if let r = snap.reason, r != "truncated" { return PressOutcome(ok: false, reason: r, dryRun: dryRun) }
        if let host, snap.host != host { return PressOutcome(ok: false, reason: "host_changed", dryRun: dryRun) }
        guard let hit = refind(role: role, slug: slug, in: found, element: { $0.element }, viewport: viewport) else {
            return PressOutcome(ok: false, reason: "gone", dryRun: dryRun)
        }
        if dryRun { return PressOutcome(ok: true, reason: "found", dryRun: true) }
        let err = AXUIElementPerformAction(hit.ax, kAXPressAction as CFString)
        return err == .success ? PressOutcome(ok: true) : PressOutcome(ok: false, reason: "press_failed")
    }

    // MARK: AX helpers

    private static func asElement(_ v: CFTypeRef) -> AXUIElement? {
        CFGetTypeID(v) == AXUIElementGetTypeID() ? (v as! AXUIElement) : nil
    }
    private static func value(_ e: AXUIElement, _ attr: String) -> CFTypeRef? {
        var v: CFTypeRef?
        return AXUIElementCopyAttributeValue(e, attr as CFString, &v) == .success ? v : nil
    }
    /// Several attributes in one IPC round trip (missing ones come back as nil).
    private static func multi(_ e: AXUIElement, _ attrs: [String]) -> [Any?] {
        var out: CFArray?
        guard AXUIElementCopyMultipleAttributeValues(e, attrs as CFArray, AXCopyMultipleAttributeOptions(rawValue: 0), &out) == .success,
              let arr = out as? [Any], arr.count == attrs.count else { return attrs.map { _ in nil } }
        return arr.map { v -> Any? in
            if CFGetTypeID(v as CFTypeRef) == AXValueGetTypeID() {
                var err = AXError.success
                if AXValueGetType(v as! AXValue) == .axError, AXValueGetValue(v as! AXValue, .axError, &err) { return nil }
            }
            return v
        }
    }
    private static func frame(_ e: AXUIElement) -> CGRect? {
        let a = multi(e, [kAXPositionAttribute, kAXSizeAttribute])
        var p = CGPoint.zero, s = CGSize.zero
        guard let pv = a[0], let sv = a[1], CFGetTypeID(pv as CFTypeRef) == AXValueGetTypeID(),
              CFGetTypeID(sv as CFTypeRef) == AXValueGetTypeID(),
              AXValueGetValue(pv as! AXValue, .cgPoint, &p), AXValueGetValue(sv as! AXValue, .cgSize, &s) else { return nil }
        return CGRect(origin: p, size: s)
    }
}

/// W16: `status/oi-hud.json` {ts, ax, pid}, rewritten every 30 s, so oi-menubar can say whether oi-hud has
/// Accessibility (page actions need it). No other content.
final class HUDStatusWriter {
    private var timer: Timer?

    func start() {
        write()
        let t = Timer(timeInterval: 30, repeats: true) { [weak self] _ in self?.write() }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    private func write() {
        let dir = SettingsStore.dataDir().appendingPathComponent("status", isDirectory: true)
        let o: [String: Any] = ["ts": SettingsStore.isoTimestamp(Date()), "ax": AXIsProcessTrusted(),
                                "pid": Int(ProcessInfo.processInfo.processIdentifier)]
        guard let data = try? JSONSerialization.data(withJSONObject: o, options: [.sortedKeys]) else { return }
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try? SettingsStore.atomicWrite(data + Data("\n".utf8), to: dir.appendingPathComponent("oi-hud.json"))
    }
}
