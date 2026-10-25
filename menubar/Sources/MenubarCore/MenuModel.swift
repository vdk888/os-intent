import Foundation

/// What a clickable menu row does. The raw value travels in NSMenuItem.representedObject.
public enum MenuAction: String, Sendable, CaseIterable {
    case togglePause, toggleHUD, toggleLogin, restartWatcher, restartHUD, rebuildTokens
    case editDenylist, editHotkeys, openData, openLogs, quit
    case openInputMonitoring, openAccessibility, openAutomation, openRegrantGuide
    case restartLive    // W8: LiveStatus.swift
    case openSettings   // W26: SettingsWindow.swift
    case openOnboarding // W22: Onboarding.swift
    case toggleLaya     // 2026-10-21: LayaSwitch.swift
}

/// System Settings deep links for the three privacy panes.
public let privacyPaneURLs: [MenuAction: String] = [
    .openInputMonitoring: "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    .openAccessibility: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    .openAutomation: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
]

public struct MenuEntry: Equatable, Sendable {
    public enum Kind: Equatable, Sendable { case header, info, action(MenuAction), separator }
    public var id: String
    public var title: String
    public var kind: Kind
    public var checked: Bool = false
    public var enabled: Bool = true
    public var indent: Int = 0
    public var tooltip: String? = nil

    public init(id: String, title: String, kind: Kind, checked: Bool = false, enabled: Bool = true,
                indent: Int = 0, tooltip: String? = nil) {
        self.id = id; self.title = title; self.kind = kind; self.checked = checked
        self.enabled = enabled; self.indent = indent; self.tooltip = tooltip
    }
}

public let watcherPermsTooltip = """
oi-watcher's own view of its grants (macOS checks permissions per app), read from \
~/.local/share/os-intent/status/oi-watcher.json, which the watcher rewrites every 30 s. \
Click to open the pane; the watcher picks up a new grant within 30 s.
"""

/// The README section that walks through re-granting the watcher ().
public let regrantGuideURL =
    "https://github.com/vdk888/os-intent/blob/main/watchers/oi-watcher/README.md#re-grant-permissions"

public enum MenuModel {
    public static func watcherText(_ s: Status) -> String {
        switch s.watcher {
        case .running(let pid):
            let p = pid.map { " (pid \($0))" } ?? ""
            return s.paused ? "Watcher: paused — running\(p), posting nothing" : "Watcher: running\(p)"
        case .notRunning: return s.paused ? "Watcher: stopped (and paused)" : "Watcher: stopped"
        case .notLoaded: return "Watcher: not installed"
        }
    }

    public static func hudText(_ s: Status) -> String {
        let state: String
        switch s.hud {
        case .running?: state = "running"
        case .notRunning?: state = "stopped"
        case .notLoaded?, nil: state = s.hudSocket ? "running (manual, no LaunchAgent)" : "not installed"
        }
        return "HUD daemon: \(state)" + (s.hudEnabled ? "" : " — display paused")
    }

    /// The whole menu, top to bottom. Pure: the app maps entries to NSMenuItems.
    public static func entries(_ s: Status) -> [MenuEntry] {
        var e: [MenuEntry] = []
        e.append(.init(id: "h.status", title: "OS-intent", kind: .header, enabled: false))
        e.append(.init(id: "watcher", title: watcherText(s), kind: .info, enabled: false))
        e.append(.init(id: "aw", title: s.awVersion.map { "ActivityWatch: reachable (\($0))" } ?? "ActivityWatch: UNREACHABLE at 127.0.0.1:5600",
                       kind: .info, enabled: false))
        e.append(.init(id: "hud", title: hudText(s), kind: .info, enabled: false))
        e += liveEntries(s)   // W8: LiveStatus.swift
        switch s.tokens {
        case .stats(let t):
            e.append(.init(id: "tokens", title: "Today: \(t.tokens) tokens, \(t.distinct) distinct", kind: .info, enabled: false))
            for k in t.kinds {
                e.append(.init(id: "kind.\(k.kind)", title: "\(k.kind): \(k.count)", kind: .info, enabled: false, indent: 1))
            }
        case .none(let why):
            e.append(.init(id: "tokens", title: "Today: \(why)", kind: .info, enabled: false))
        }

        e.append(.init(id: "sep.1", title: "", kind: .separator))
        e.append(.init(id: "h.perms", title: "Watcher permissions" + s.watcherPerms.headerSuffix, kind: .header,
                       enabled: false, tooltip: watcherPermsTooltip))
        let r = s.watcherPerms.rows
        for (id, name, st, act) in [("perm.input", "Input Monitoring", r.input, MenuAction.openInputMonitoring),
                                    ("perm.ax", "Accessibility (optional)", r.ax, .openAccessibility),
                                    ("perm.auto", "Automation → Safari", r.safari, .openAutomation)] {
            e.append(.init(id: id, title: "\(st.symbol) \(name) — \(st.detail) · open pane…", kind: .action(act),
                           indent: 1, tooltip: watcherPermsTooltip))
        }
        e.append(HUDAXStatus.entry(s.hudAX))      // W16: oi-hud's Accessibility (page actions)
        e.append(.init(id: "perm.guide", title: "Re-grant guide…", kind: .action(.openRegrantGuide), indent: 1))

        e.append(.init(id: "sep.2", title: "", kind: .separator))
        e.append(.init(id: "pause", title: s.paused ? "Resume logging" : "Pause logging", kind: .action(.togglePause)))
        e.append(.init(id: "hudtoggle", title: "HUD enabled", kind: .action(.toggleHUD), checked: s.hudEnabled))
        e += layaEntries(s)   // 2026-10-21: LayaSwitch.swift
        switch s.login {
        case .notInstalled:
            e.append(.init(id: "login", title: "Launch at login (run install.sh first)", kind: .action(.toggleLogin), enabled: false))
        case .enabled, .disabled:
            e.append(.init(id: "login", title: "Launch at login", kind: .action(.toggleLogin), checked: s.login == .enabled))
        }
        e.append(.init(id: "restart", title: "Restart watcher", kind: .action(.restartWatcher), enabled: s.watcher != .notLoaded))
        // only a LaunchAgent-managed HUD can be restarted (a manual `oi-hud serve` is left alone)
        e.append(.init(id: "restart.hud", title: "Restart HUD", kind: .action(.restartHUD), enabled: s.hud != nil,
                       tooltip: s.hud == nil ? "No com.osintent.oi-hud LaunchAgent: run hud/install.sh" : nil))
        e.append(.init(id: "rebuild", title: s.rebuilding ? "Rebuilding today's tokens…" : "Rebuild today's tokens",
                       kind: .action(.rebuildTokens), enabled: !s.rebuilding))
        if let r = s.lastRebuild {
            e.append(.init(id: "rebuild.last", title: r, kind: .info, enabled: false, indent: 1))
        }
        e.append(.init(id: "settings", title: "Settings…", kind: .action(.openSettings)))   // W26
        e.append(Onboarding.entry(onboarded: s.onboarded))                                   // W22
        e.append(.init(id: "denylist", title: "Denylist… (\(s.denylistCount) apps)", kind: .action(.editDenylist)))
        e.append(.init(id: "hotkeys", title: "Hotkeys…", kind: .action(.editHotkeys)))
        e.append(.init(id: "data", title: "Open data folder", kind: .action(.openData)))
        e.append(.init(id: "logs", title: "Open logs", kind: .action(.openLogs)))
        e.append(.init(id: "sep.3", title: "", kind: .separator))
        e.append(.init(id: "quit", title: "Quit oi-menubar", kind: .action(.quit)))
        return e
    }

    /// Plain-text rendering (for `oi-menubar --print-menu` and the logs).
    public static func render(_ entries: [MenuEntry]) -> String {
        entries.map { m -> String in
            if m.kind == .separator { return "----" }
            let pad = String(repeating: "  ", count: m.indent)
            let check = m.checked ? "[x] " : (m.kind == .action(.toggleHUD) || m.kind == .action(.toggleLogin) || m.kind == .action(.toggleLaya) ? "[ ] " : "")
            let dim = (!m.enabled && m.kind != .header && m.kind != .info) ? " (disabled)" : ""
            return pad + check + m.title + dim
        }.joined(separator: "\n")
    }
}
