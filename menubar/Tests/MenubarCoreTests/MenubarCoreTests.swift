import Foundation
import Testing
@testable import MenubarCore
import OISettings

@Suite struct LaunchctlTests {
    let running = """
    gui/501/com.osintent.oi-watcher = {
    \tactive count = 3
    \tpath = /Users/you/Library/LaunchAgents/com.osintent.oi-watcher.plist
    \ttype = LaunchAgent
    \tstate = running

    \tprogram = /Users/you/.local/bin/oi-watcher
    \tpid = 4242
    \tjob state = running
    }
    """
    @Test func runningWithPid() {
        #expect(LaunchdState.parse(printOutput: running, exitCode: 0) == .running(pid: 4242))
    }
    @Test func loadedNotRunning() {
        let out = running.replacingOccurrences(of: "state = running", with: "state = not running")
            .replacingOccurrences(of: "\tpid = 4242\n", with: "")
        #expect(LaunchdState.parse(printOutput: out, exitCode: 0) == .notRunning)
    }
    @Test func notLoadedIsExit113() {
        #expect(LaunchdState.parse(printOutput: "Bad request.\nCould not find service", exitCode: 113) == .notLoaded)
    }
    @Test func printDisabled() {
        let out = """
        \tdisabled services = {
        \t\t"com.osintent.oi-menubar" => disabled
        \t\t"com.osintent.oi-watcher" => enabled
        \t\t"com.old.style" => true
        \t}
        """
        #expect(parseDisabled(printDisabledOutput: out, label: "com.osintent.oi-menubar") == true)
        #expect(parseDisabled(printDisabledOutput: out, label: "com.osintent.oi-watcher") == false)
        #expect(parseDisabled(printDisabledOutput: out, label: "com.old.style") == true)
        #expect(parseDisabled(printDisabledOutput: out, label: "com.osintent.oi-hud") == nil)
    }
}

@Suite struct PermissionTests {
    let now = ISO8601DateFormatter().date(from: "2026-10-20T12:00:00Z")!
    func ws(_ ts: String = "2026-10-20T11:59:40Z", tap: Bool = true, ax: Bool = false,
            safari: WatcherStatus.Automation = .granted) -> WatcherStatus {
        WatcherStatus(ts: ts, keyTap: tap, ax: ax, safariAutomation: safari, spaceAPI: true, pid: 9)
    }
    @Test func symbols() {
        #expect(PermState.granted.symbol == "✓")
        #expect(PermState.notGranted.symbol == "✗")
        #expect(PermState.denied.symbol == "✗")
        #expect(PermState.notAsked.symbol == "✗")
        #expect(PermState.unknown("x").symbol == "?")
    }
    @Test func automationMapping() {
        #expect(PermState.from(.granted) == .granted)
        #expect(PermState.from(.denied) == .denied)
        #expect(PermState.from(.notAsked) == .notAsked)
        #expect(PermState.from(.unknown) == .unknown("Safari not running"))
    }
    @Test func freshFileGivesWatcherRows() {
        let p = WatcherPerms.from(ws(), now: now)
        #expect(p == .fresh(ws()))
        #expect(p.rows.input == .granted && p.rows.ax == .notGranted && p.rows.safari == .granted)
        #expect(p.headerSuffix == "")
        #expect(WatcherPerms.from(ws(tap: false, safari: .notAsked), now: now).rows.input == .notGranted)
    }
    @Test func staleOrMissingIsWatcherSilent() {
        let stale = WatcherPerms.from(ws("2026-10-20T11:55:00Z"), now: now)    // 300 s > 90 s
        #expect(stale == .stale(seconds: 300))
        #expect(stale.rows.input == .unknown("watcher silent"))
        #expect(stale.headerSuffix == " — watcher silent (last report 5 min ago)")
        #expect(WatcherPerms.from(ws("2026-10-20T11:58:31Z"), now: now) == .fresh(ws("2026-10-20T11:58:31Z")))  // 89 s
        #expect(WatcherPerms.from(nil, now: now) == .missing)
        #expect(WatcherPerms.missing.headerSuffix.contains("no status file"))
        #expect(WatcherPerms.from(ws("garbage"), now: now) == .stale(seconds: -1))
    }
    @Test func loginResolve() {
        #expect(LoginState.resolve(agentInstalled: true, agentDisabled: nil) == .enabled)
        #expect(LoginState.resolve(agentInstalled: true, agentDisabled: false) == .enabled)
        #expect(LoginState.resolve(agentInstalled: true, agentDisabled: true) == .disabled)
        #expect(LoginState.resolve(agentInstalled: false, agentDisabled: nil) == .notInstalled)
    }
}

@Suite struct StatsTests {
    @Test func decodeSortedKinds() {
        let json = #"{"tokens": 10, "kinds": {"key": 3, "activate": 6, "url": 1}, "top_apps": [], "distinct_tokens": 7}"#
        let t = TokenStats.decode(Data(json.utf8))
        #expect(t?.tokens == 10 && t?.distinct == 7)
        #expect(t?.kinds.map(\.kind) == ["activate", "key", "url"])
        #expect(TokenStats.decode(Data("nope".utf8)) == nil)
    }
    @Test func summariseTokens() {
        #expect(summariseTokensOutput("2026-10-20: 512 tokens (0 invalid) -> /Users/you/.local/share/os-intent/tokens/2026-10-20.jsonl\n")
                == "2026-10-20: 512 tokens (0 invalid)")
        #expect(summariseTokensOutput("garbage") == nil)
    }
}

@Suite struct MenuModelTests {
    func status() -> Status {
        Status(watcher: .running(pid: 7), paused: false, awVersion: "v0.14.1", hud: nil, hudSocket: false,
               hudEnabled: true,
               tokens: .stats(TokenStats(tokens: 9, distinct: 5, kinds: [("activate", 6), ("key", 3)])),
               watcherPerms: .fresh(WatcherStatus(ts: "2026-10-20T12:00:00Z", keyTap: true, ax: false,
                                                  safariAutomation: .unknown, spaceAPI: true, pid: 7)),
               login: .enabled, denylistCount: 3)
    }
    @Test func sectionsAndRows() {
        let e = MenuModel.entries(status())
        let titles = e.map(\.title)
        #expect(titles.contains("Watcher: running (pid 7)"))
        #expect(titles.contains("ActivityWatch: reachable (v0.14.1)"))
        #expect(titles.contains("HUD daemon: not installed"))
        #expect(titles.contains("Today: 9 tokens, 5 distinct"))
        #expect(titles.contains("activate: 6"))
        #expect(titles.contains("Watcher permissions"))
        #expect(titles.contains("✓ Input Monitoring — granted · open pane…"))
        #expect(titles.contains("✗ Accessibility (optional) — not granted · open pane…"))
        #expect(titles.contains("? Automation → Safari — unknown (Safari not running) · open pane…"))
        #expect(e.first { $0.id == "perm.guide" }?.kind == .action(.openRegrantGuide))
        #expect(titles.contains("Pause logging"))
        #expect(e.first { $0.id == "login" }?.checked == true)
        #expect(e.first { $0.id == "hudtoggle" }?.checked == true)
        #expect(e.first { $0.id == "perm.input" }?.tooltip?.contains("status/oi-watcher.json") == true)
        #expect(e.last?.kind == .action(.quit))
        #expect(Set(e.map(\.id)).count == e.count)    // ids unique (in-place refresh relies on it)
    }
    @Test func pausedAndDisabledStates() {
        var s = status()
        s.paused = true; s.hudEnabled = false; s.login = .notInstalled; s.watcher = .notLoaded
        s.watcherPerms = .missing
        s.hud = .running(pid: 1); s.awVersion = nil; s.tokens = .none("n/a (venv missing)"); s.rebuilding = true
        s.lastRebuild = "10:02 2026-10-20: 512 tokens (0 invalid)"
        let e = MenuModel.entries(s)
        let by = Dictionary(uniqueKeysWithValues: e.map { ($0.id, $0) })
        #expect(by["pause"]?.title == "Resume logging")
        #expect(by["watcher"]?.title == "Watcher: not installed")
        #expect(by["aw"]?.title == "ActivityWatch: UNREACHABLE at 127.0.0.1:5600")
        #expect(by["hud"]?.title == "HUD daemon: running — display paused")
        #expect(by["hudtoggle"]?.checked == false)
        #expect(by["login"]?.enabled == false)
        #expect(by["restart"]?.enabled == false)
        #expect(by["restart.hud"]?.enabled == true)             // com.osintent.oi-hud is loaded
        #expect(by["rebuild"]?.enabled == false && by["rebuild.last"] != nil)
        #expect(by["tokens"]?.title == "Today: n/a (venv missing)")
        #expect(by["h.perms"]?.title == "Watcher permissions — watcher silent (no status file yet)")
        #expect(by["perm.input"]?.title == "? Input Monitoring — unknown (watcher silent) · open pane…")
        s.login = .disabled
        let a = Dictionary(uniqueKeysWithValues: MenuModel.entries(s).map { ($0.id, $0) })
        #expect(a["login"]?.enabled == true && a["login"]?.checked == false && a["login"]?.title == "Launch at login")
    }
    @Test func restartHUDNeedsTheLaunchAgent() {
        var s = status()
        s.hud = nil; s.hudSocket = true                          // manual `oi-hud serve`
        var by = Dictionary(uniqueKeysWithValues: MenuModel.entries(s).map { ($0.id, $0) })
        #expect(by["hud"]?.title == "HUD daemon: running (manual, no LaunchAgent)")
        #expect(by["restart.hud"]?.enabled == false && by["restart.hud"]?.tooltip?.contains("install.sh") == true)
        s.hud = .notRunning
        by = Dictionary(uniqueKeysWithValues: MenuModel.entries(s).map { ($0.id, $0) })
        #expect(by["hud"]?.title == "HUD daemon: stopped")
        #expect(by["restart.hud"]?.enabled == true && by["restart.hud"]?.kind == .action(.restartHUD))
    }
    @Test func paneURLs() {
        #expect(privacyPaneURLs[.openInputMonitoring]?.hasSuffix("?Privacy_ListenEvent") == true)
        #expect(privacyPaneURLs[.openAccessibility]?.hasSuffix("?Privacy_Accessibility") == true)
        #expect(privacyPaneURLs[.openAutomation]?.hasSuffix("?Privacy_Automation") == true)
        #expect(regrantGuideURL.hasSuffix("watchers/oi-watcher/README.md#re-grant-permissions"))
    }
    @Test func renderText() {
        let r = MenuModel.render(MenuModel.entries(status()))
        #expect(r.contains("[x] HUD enabled"))
        #expect(r.contains("----"))
    }
}

@Suite struct OnboardingTests {   // W22
    @Test func menuRowAndOffer() throws {
        var s = Status()
        var row = MenuModel.entries(s).first { $0.id == "onboarding" }
        #expect(row?.title == "Onboarding… (not done yet)" && row?.kind == .action(.openOnboarding))
        s.onboarded = true
        row = MenuModel.entries(s).first { $0.id == "onboarding" }
        #expect(row?.title == "Onboarding… (redo)")
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("oi-onb-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir.appendingPathComponent("status"), withIntermediateDirectories: true)
        #expect(Onboarding.shouldOffer(dataDir: dir) && !Onboarding.done(dataDir: dir))
        FileManager.default.createFile(atPath: dir.appendingPathComponent(Onboarding.offeredMarker).path, contents: Data())
        #expect(!Onboarding.shouldOffer(dataDir: dir))
        FileManager.default.createFile(atPath: dir.appendingPathComponent("profile.yaml").path, contents: Data())
        #expect(Onboarding.done(dataDir: dir))
        try? FileManager.default.removeItem(at: dir)
    }
    @Test func commandScriptQuotes() {
        let t = Onboarding.commandScript(oiBinary: "/r/it's/oi", dataDir: "/d")
        #expect(t.hasPrefix("#!/bin/zsh\n"))
        #expect(t.contains("export OI_DATA_DIR='/d'\n"))
        #expect(t.contains("'/r/it'\\''s/oi' onboard\n"))
    }
}
