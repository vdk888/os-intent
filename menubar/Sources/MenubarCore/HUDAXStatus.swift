import Foundation

/// W16 page actions: oi-hud's own Accessibility grant, from `<data_dir>/status/oi-hud.json`
/// (`{"ts": ISO-8601, "ax": bool, "pid": n}`, rewritten by oi-hud every 30 s). Page actions (read the
/// Safari page's buttons, AXPress the chosen one) need Accessibility for oi-hud.app; without it the HUD
/// simply offers none, and the menu shows this hint.
public enum HUDAXStatus {
    public static let staleAfter: TimeInterval = 120

    public static func parse(_ data: Data?, now: Date = Date()) -> PermState {
        guard let data, let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let ax = o["ax"] as? Bool else { return .unknown("HUD silent") }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let ts = (o["ts"] as? String).flatMap { f.date(from: $0) ?? ISO8601DateFormatter().date(from: $0) }
        guard let ts, now.timeIntervalSince(ts) <= staleAfter else { return .unknown("HUD silent") }
        return ax ? .granted : .notGranted
    }

    public static func load(dataDir: URL, now: Date = Date()) -> PermState {
        parse(try? Data(contentsOf: dataDir.appendingPathComponent("status/oi-hud.json")), now: now)
    }

    /// The menu row: one line under the watcher permissions.
    public static func entry(_ st: PermState) -> MenuEntry {
        let tail = st == .granted ? "" : " (needed to offer page buttons)"
        return MenuEntry(id: "perm.hudax", title: "\(st.symbol) Page actions: Accessibility for oi-hud — \(st.detail)\(tail) · open pane…",
                         kind: .action(.openAccessibility), indent: 1,
                         tooltip: "System Settings → Privacy & Security → Accessibility → enable oi-hud. W16: the HUD reads the Safari page's buttons (labels only) and presses the one you tap.")
    }
}
