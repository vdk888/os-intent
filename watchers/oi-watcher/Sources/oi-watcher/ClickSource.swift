import AppKit
import ApplicationServices
import OISettings
import OIWatcherCore

/// W16: learned page clicks. A global left-mouse-up monitor (no permission needed for mouse
/// events); only while Safari is frontmost, Safari is not denylisted and oi-watcher has Accessibility:
/// the AX element under the pointer, up to its page (AXWebArea), gives `{host, role, label slug}` →
/// bucket `oi-watcher-click_<host>` → `click:<host>/<role>:<label-slug>` tokens. Never logged: the
/// pointer position, page text, field values, clicks in Safari's own toolbar / tabs, anything while
/// paused (the sink drops it).
@MainActor
final class ClickSource {
    private let sink: AWSink
    private var monitor: Any?
    private let settings = SettingsCache()

    init(sink: AWSink) { self.sink = sink }

    func start() {
        monitor = NSEvent.addGlobalMonitorForEvents(matching: .leftMouseUp) { [weak self] _ in
            let p = NSEvent.mouseLocation
            MainActor.assumeIsolated { self?.mouseUp(cocoa: p) }
        }
        if !AXIsProcessTrusted() {
            Hints.once("click-ax", "click: Accessibility not granted — page clicks are not logged (grant Accessibility to oi-watcher)")
        }
        Log.info("click: watching page button / link clicks in Safari (label only)")
    }

    private func mouseUp(cocoa p: NSPoint) {
        guard let app = NSWorkspace.shared.frontmostApplication, app.bundleIdentifier == SafariSource.bundleID,
              AXIsProcessTrusted(), ClickTarget.allowed(denylist: settings.current.denylistApps) else { return }
        let mainH = NSScreen.screens.first?.frame.height ?? 0
        let sys = AXUIElementCreateSystemWide()
        AXUIElementSetMessagingTimeout(sys, 0.2)
        var hit: AXUIElement?
        guard AXUIElementCopyElementAtPosition(sys, Float(p.x), Float(mainH - p.y), &hit) == .success, let hit else { return }
        var pid: pid_t = 0
        guard AXUIElementGetPid(hit, &pid) == .success, pid == app.processIdentifier else { return }
        var chain: [ClickTarget.Node] = []
        var web: AXUIElement?
        var cur: AXUIElement? = hit
        while let e = cur, chain.count < 40 {
            let role = Self.string(e, kAXRoleAttribute)
            let label = PageLabel.role(ax: role) != nil ? Self.label(e) : nil
            chain.append(.init(role: role, label: label))
            if role == "AXWebArea" { web = e; break }
            cur = Self.value(e, kAXParentAttribute).flatMap { CFGetTypeID($0) == AXUIElementGetTypeID() ? ($0 as! AXUIElement) : nil }
        }
        guard let target = ClickTarget.resolve(chain), let web else { return }
        let url = (Self.value(web, "AXURL") as? URL) ?? (Self.value(web, "AXURL") as? String).flatMap(URL.init(string:))
        guard let u = url, let scheme = u.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let h = u.host?.lowercased(), !h.isEmpty else { return }
        let host = u.port.map { "\(h):\($0)" } ?? h
        let ev = AWEvent.click(at: Date(), host: host, role: target.role, slug: target.slug)
        let sink = self.sink
        Task { await sink.post(.click, ev) }
    }

    private static func value(_ e: AXUIElement, _ a: String) -> CFTypeRef? {
        var v: CFTypeRef?
        return AXUIElementCopyAttributeValue(e, a as CFString, &v) == .success ? v : nil
    }
    private static func string(_ e: AXUIElement, _ a: String) -> String? { value(e, a) as? String }

    /// AXTitle, else AXDescription, else the element's own static text (one level), ≤ 80 chars.
    private static func label(_ e: AXUIElement) -> String? {
        for a in [kAXTitleAttribute, kAXDescriptionAttribute] {
            if let s = string(e, a)?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty { return String(s.prefix(80)) }
        }
        let kids = (value(e, kAXChildrenAttribute) as? [AXUIElement]) ?? []
        let text = kids.prefix(6).compactMap { k -> String? in
            string(k, kAXRoleAttribute) == "AXStaticText" ? string(k, kAXValueAttribute) : nil
        }.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : String(text.prefix(80))
    }
}
