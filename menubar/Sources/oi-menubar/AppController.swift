import AppKit
import MenubarCore
import OISettings

/// Owns the status item and its menu. All on the main actor; probes run detached.
@MainActor
final class AppController: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private let menu = NSMenu()
    private var items: [String: NSMenuItem] = [:]
    private var ids: [String] = []
    private var status = Status()
    private let cache = SettingsCache()
    private var openTimer: Timer?
    private var probing = false
    private var rebuilding = false
    private var lastRebuild: String?
    private var layaBusy = false
    private var denylistWindow: DenylistWindowController?
    private var hotkeysWindow: HotkeysWindowController?
    private var settingsWindow: SettingsWindowController?   // W26

    func applicationDidFinishLaunching(_ note: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        menu.delegate = self
        menu.autoenablesItems = false
        statusItem.menu = menu
        apply(Probe.placeholder(cache.current))
        updateIcon()
        // Icon follows `paused` even when the menu is closed (cheap: mtime check only).
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in
            MainActor.assumeIsolated { self.updateIcon() }
        }
        refresh()
        log("started; status item shown")
        offerOnboarding()   // W22: first launch only
    }

    private func updateIcon() {
        let s = cache.current
        guard let button = statusItem.button else { return }
        let img = NSImage(systemSymbolName: "brain.head.profile", accessibilityDescription: "OS-intent")
        img?.isTemplate = true
        button.image = img
        button.appearsDisabled = s.paused     // dimmed while logging is paused
        button.toolTip = s.paused ? "OS-intent — logging paused" : "OS-intent"
    }

    // MARK: refresh

    func menuWillOpen(_ menu: NSMenu) {
        refresh()
        openTimer?.invalidate()
        let t = Timer(timeInterval: 5, repeats: true) { _ in MainActor.assumeIsolated { self.refresh() } }
        RunLoop.main.add(t, forMode: .common)    // .common includes event tracking (menu open)
        openTimer = t
    }

    func menuDidClose(_ menu: NSMenu) {
        openTimer?.invalidate(); openTimer = nil
    }

    func refresh() {
        guard !probing else { return }
        probing = true
        let settings = cache.current
        let rb = rebuilding, last = lastRebuild, lb = layaBusy
        Task.detached {
            let s = Probe.collect(settings, rebuilding: rb, lastRebuild: last, layaBusy: lb)
            await MainActor.run {
                self.probing = false
                self.apply(s)
                self.updateIcon()
            }
        }
    }

    private func apply(_ s: Status) {
        status = s
        let entries = MenuModel.entries(s)
        if entries.map(\.id) == ids {
            for e in entries { if let it = items[e.id] { configure(it, e) } }
            return
        }
        menu.removeAllItems(); items.removeAll()
        for e in entries {
            let it: NSMenuItem
            if e.kind == .separator { it = .separator() } else {
                it = NSMenuItem(title: e.title, action: nil, keyEquivalent: "")
                configure(it, e)
            }
            menu.addItem(it)
            items[e.id] = it
        }
        ids = entries.map(\.id)
    }

    private func configure(_ it: NSMenuItem, _ e: MenuEntry) {
        guard e.kind != .separator else { return }
        if it.title != e.title { it.title = e.title }
        it.state = e.checked ? .on : .off
        it.isEnabled = e.enabled
        it.indentationLevel = e.indent
        it.toolTip = e.tooltip
        if case .action(let a) = e.kind {
            it.target = self
            it.action = #selector(menuAction(_:))
            it.representedObject = a.rawValue
        } else if e.kind == .header {
            it.attributedTitle = NSAttributedString(string: e.title, attributes: [.font: NSFont.boldSystemFont(ofSize: NSFont.systemFontSize)])
        }
    }

    // MARK: actions

    @objc private func menuAction(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String, let a = MenuAction(rawValue: raw) else { return }
        switch a {
        case .togglePause:
            save { $0.paused.toggle() }
            log(cache.current.paused ? "paused logging" : "resumed logging")
        case .toggleHUD:
            save { $0.hudEnabled.toggle() }
        case .toggleLaya:   // 2026-10-21: oi laya-switch on|off (persists laya.enabled; start under the memory rule / stop)
            guard !layaBusy, let st = status.laya else { return }
            let on = !st.enabled
            layaBusy = true
            let settings = cache.current
            Task.detached {
                let r = Probe.switchLaya(settings, on: on)
                log("laya switch \(on ? "on" : "off"): \(r)")
                await MainActor.run {
                    self.layaBusy = false
                    self.refresh()
                }
            }
        case .toggleLogin:
            // LaunchAgent only (W9): `launchctl enable|disable` keeps the plist; RunAtLoad
            // then starts (or no longer starts) the app at the next login.
            let on = status.login != .enabled
            if FileManager.default.fileExists(atPath: Paths.menubarPlist.path) {
                let verb = on ? "enable" : "disable"
                let r = Shell.launchctl([verb, "\(Shell.domain)/\(Labels.menubar)"])
                log("launch at login: launchctl \(verb) -> \(r.code)")
            }
        case .restartWatcher:
            Task.detached {
                let r = Shell.launchctl(["kickstart", "-k", "\(Shell.domain)/\(Labels.watcher)"])
                log("restart watcher: launchctl kickstart -k -> \(r.code)")
                await MainActor.run { self.refresh() }
            }
        case .restartHUD:
            Task.detached {
                let r = Shell.launchctl(["kickstart", "-k", "\(Shell.domain)/\(Labels.hud)"])
                log("restart HUD: launchctl kickstart -k -> \(r.code)")
                await MainActor.run { self.refresh() }
            }
        case .restartLive:   // W8
            Task.detached {
                let r = Shell.launchctl(["kickstart", "-k", "\(Shell.domain)/\(LiveStatus.label)"])
                log("restart live predictor: launchctl kickstart -k -> \(r.code)")
                await MainActor.run { self.refresh() }
            }
        case .rebuildTokens:
            guard !rebuilding else { return }
            rebuilding = true
            let settings = cache.current
            Task.detached {
                let line = Probe.rebuild(settings)
                await MainActor.run {
                    self.rebuilding = false
                    self.lastRebuild = line
                    self.refresh()
                }
            }
        case .editDenylist:
            if denylistWindow == nil { denylistWindow = DenylistWindowController { [weak self] in self?.refresh() } }
            denylistWindow?.present(cache.current.denylistApps)
        case .editHotkeys:
            openHotkeys()
        case .openSettings:   // W26
            if settingsWindow == nil { settingsWindow = SettingsWindowController { [weak self] in self?.openHotkeys() } }
            settingsWindow?.present()
        case .openOnboarding:   // W22
            openOnboarding()
        case .openData:
            NSWorkspace.shared.open(Paths.data)
        case .openLogs:
            try? FileManager.default.createDirectory(at: Paths.logs, withIntermediateDirectories: true)
            NSWorkspace.shared.open(Paths.logs)
        case .quit:
            NSApp.terminate(nil)
        case .openInputMonitoring, .openAccessibility, .openAutomation:
            if let s = privacyPaneURLs[a], let u = URL(string: s) { NSWorkspace.shared.open(u) }
        case .openRegrantGuide:
            if let u = URL(string: regrantGuideURL) { NSWorkspace.shared.open(u) }
        }
        refresh()
    }

    /// W22: `oi onboard` in Terminal, through `<data>/onboard.command` (0700).
    private func openOnboarding() {
        let url = Paths.data.appendingPathComponent(Onboarding.script)
        let text = Onboarding.commandScript(oiBinary: Paths.oiBinary(cache.current), dataDir: Paths.data.path)
        do {
            try FileManager.default.createDirectory(at: Paths.data, withIntermediateDirectories: true)
            try text.write(to: url, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
        } catch {
            log("onboarding: script not written (\(error))"); return
        }
        let cfg = NSWorkspace.OpenConfiguration()
        let terminal = URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app")
        NSWorkspace.shared.open([url], withApplicationAt: terminal, configuration: cfg) { _, err in
            if let err { log("onboarding: Terminal did not open (\(err))") }
        }
        log("onboarding: opened in Terminal")
    }

    /// W22: at the first launch with no profile.yaml, offer the onboarding once (the marker keeps it from coming back).
    private func offerOnboarding() {
        guard Onboarding.shouldOffer(dataDir: Paths.data) else { return }
        let marker = Paths.data.appendingPathComponent(Onboarding.offeredMarker)
        try? FileManager.default.createDirectory(at: marker.deletingLastPathComponent(), withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: marker.path, contents: Data())
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = "Set up OS-intent with Claude?"
        a.informativeText = "Claude (your subscription) reads the sources you choose — Claude memory, wiki, recent "
            + "mails, Notion — asks a few questions, and you check the result, so the HUD proposes useful actions from "
            + "day one (W22b). You can run it later from the menu: Onboarding…"
        a.addButton(withTitle: "Start onboarding")
        a.addButton(withTitle: "Later")
        if a.runModal() == .alertFirstButtonReturn { openOnboarding() }
    }

    private func openHotkeys() {
        if hotkeysWindow == nil { hotkeysWindow = HotkeysWindowController() }
        hotkeysWindow?.present(cache.current.hotkeys)
    }

    private func save(_ change: (inout OISettings) -> Void) {
        do { try SettingsStore.update(change) } catch { log("settings write failed: \(error)") }
        updateIcon()
    }
}

extension Probe {
    /// Shown for the instant before the first probe returns.
    static func placeholder(_ s: OISettings) -> Status {
        Status(paused: s.paused, hudEnabled: s.hudEnabled, tokens: .none("…"), denylistCount: s.denylistApps.count)
    }
}
