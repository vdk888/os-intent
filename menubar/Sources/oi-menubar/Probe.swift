import AppKit
import Foundation
import MenubarCore
import OISettings

enum Labels {
    static let watcher = "com.osintent.oi-watcher"
    static let hud = "com.osintent.oi-hud"
    static let menubar = "com.osintent.oi-menubar"
}

func log(_ msg: String) {
    let line = "\(SettingsStore.isoTimestamp(Date())) oi-menubar: \(msg)\n"
    FileHandle.standardError.write(Data(line.utf8))
}

/// Small synchronous process runner (call it off the main thread for anything slow).
enum Shell {
    struct Result: Sendable { var code: Int32; var out: String; var err: String }

    static func run(_ path: String, _ args: [String], timeout: TimeInterval = 10) -> Result {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        let o = Pipe(), e = Pipe()
        p.standardOutput = o; p.standardError = e
        p.standardInput = FileHandle.nullDevice
        do { try p.run() } catch { return Result(code: -1, out: "", err: "\(error)") }
        let killer = DispatchWorkItem { if p.isRunning { p.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: killer)
        let errBox = Box<Data>(Data())
        let g = DispatchGroup()
        g.enter()
        DispatchQueue.global().async { errBox.value = e.fileHandleForReading.readDataToEndOfFile(); g.leave() }
        let out = o.fileHandleForReading.readDataToEndOfFile()
        g.wait()
        p.waitUntilExit()
        killer.cancel()
        return Result(code: p.terminationStatus, out: String(decoding: out, as: UTF8.self),
                      err: String(decoding: errBox.value, as: UTF8.self))
    }

    static func launchctl(_ args: [String]) -> Result { run("/bin/launchctl", args, timeout: 5) }
    static var domain: String { "gui/\(getuid())" }
}

final class Box<T>: @unchecked Sendable {
    var value: T
    init(_ v: T) { value = v }
}

enum Paths {
    static var data: URL { SettingsStore.dataDir() }
    static var logs: URL { data.appendingPathComponent("logs", isDirectory: true) }
    static var hudSocket: URL { data.appendingPathComponent("hud.sock") }
    static var menubarPlist: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents/\(Labels.menubar).plist")
    }
    static func oiBinary(_ s: OISettings) -> String { s.repoDir + "/.venv/bin/oi" }
}

/// Gathers a Status. Blocking (launchctl, HTTP with a 1 s timeout, `oi stats`): never on the main thread
/// while the app runs.
enum Probe {
    static func collect(_ settings: OISettings, rebuilding: Bool, lastRebuild: String?, layaBusy: Bool = false) -> Status {
        var s = Status()
        s.paused = settings.paused
        s.hudEnabled = settings.hudEnabled
        s.denylistCount = settings.denylistApps.count
        s.rebuilding = rebuilding
        s.lastRebuild = lastRebuild

        let w = Shell.launchctl(["print", "\(Shell.domain)/\(Labels.watcher)"])
        s.watcher = LaunchdState.parse(printOutput: w.out, exitCode: w.code)
        let h = Shell.launchctl(["print", "\(Shell.domain)/\(Labels.hud)"])
        let hs = LaunchdState.parse(printOutput: h.out, exitCode: h.code)
        s.hud = hs == .notLoaded ? nil : hs
        s.hudSocket = FileManager.default.fileExists(atPath: Paths.hudSocket.path)

        s.live = LiveStatus.load(dataDir: Paths.data); s.liveNow = Date()   // W8
        s.awVersion = awInfo()
        s.tokens = tokens(settings)
        s.laya = layaStatus(settings); s.layaBusy = layaBusy

        // The WATCHER's grants, as it reports them (not this app's own: macOS checks per app).
        s.watcherPerms = WatcherPerms.from(WatcherStatus.load())
        s.hudAX = HUDAXStatus.load(dataDir: Paths.data)   // W16
        s.onboarded = Onboarding.done(dataDir: Paths.data)   // W22

        let installed = FileManager.default.fileExists(atPath: Paths.menubarPlist.path)
        var disabled: Bool?
        if installed {
            let d = Shell.launchctl(["print-disabled", Shell.domain])
            disabled = parseDisabled(printDisabledOutput: d.out, label: Labels.menubar)
        }
        s.login = LoginState.resolve(agentInstalled: installed, agentDisabled: disabled)
        return s
    }

    /// GET /api/0/info with a 1 s timeout; returns the version string, or nil if unreachable.
    static func awInfo() -> String? {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 1
        cfg.timeoutIntervalForResource = 1
        cfg.connectionProxyDictionary = [:]
        let session = URLSession(configuration: cfg)
        defer { session.finishTasksAndInvalidate() }
        let box = Box<String?>(nil)
        let sem = DispatchSemaphore(value: 0)
        session.dataTask(with: URL(string: "http://127.0.0.1:5600/api/0/info")!) { data, resp, _ in
            if (resp as? HTTPURLResponse)?.statusCode == 200 {
                let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
                box.value = (obj?["version"] as? String) ?? "ok"
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 1.5)
        return box.value
    }

    static func tokens(_ settings: OISettings) -> TokensInfo {
        let oi = Paths.oiBinary(settings)
        guard FileManager.default.isExecutableFile(atPath: oi) else { return .none("n/a (venv missing at repo_dir)") }
        let r = Shell.run(oi, ["stats", "--day", "today", "--json"], timeout: 10)
        if r.code == 0, let t = TokenStats.decode(Data(r.out.utf8)) { return .stats(t) }
        if r.code == 1 { return .none("no tokens yet — use Rebuild") }
        return .none("n/a (oi stats exit \(r.code))")
    }

    /// `oi laya-switch status --json` (settings laya.enabled + /health with a 1 s timeout + status/oi-laya.json).
    static func layaStatus(_ settings: OISettings) -> LayaSwitchStatus? {
        let oi = Paths.oiBinary(settings)
        guard FileManager.default.isExecutableFile(atPath: oi) else { return nil }
        let r = Shell.run(oi, ["laya-switch", "status", "--json"], timeout: 5)
        return r.code == 0 ? LayaSwitchStatus.decode(Data(r.out.utf8)) : nil
    }

    /// `oi laya-switch on|off --json`: persist laya.enabled, then start (memory rule) or stop the server.
    static func switchLaya(_ settings: OISettings, on: Bool) -> String {
        let oi = Paths.oiBinary(settings)
        guard FileManager.default.isExecutableFile(atPath: oi) else { return "venv missing at repo_dir" }
        let r = Shell.run(oi, ["laya-switch", on ? "on" : "off", "--json"], timeout: 30)
        return "exit \(r.code) \(r.out.trimmingCharacters(in: .whitespacesAndNewlines).prefix(200))"
    }

    /// `oi pull && oi tokens --day today`; returns one short line for the menu.
    static func rebuild(_ settings: OISettings) -> String {
        let oi = Paths.oiBinary(settings)
        let f = DateFormatter(); f.dateFormat = "HH:mm"
        let at = f.string(from: Date())
        guard FileManager.default.isExecutableFile(atPath: oi) else { return "\(at) failed: venv missing at repo_dir" }
        let pull = Shell.run(oi, ["pull"], timeout: 300)
        guard pull.code == 0 else {
            let why = pull.err.split(separator: "\n").first.map(String.init) ?? "exit \(pull.code)"
            return "\(at) pull failed: \(why.prefix(80))"
        }
        let tok = Shell.run(oi, ["tokens", "--day", "today"], timeout: 300)
        let line = summariseTokensOutput(tok.out) ?? "exit \(tok.code)"
        return "\(at) \(tok.code == 0 ? "" : "with errors: ")\(line)"
    }
}
