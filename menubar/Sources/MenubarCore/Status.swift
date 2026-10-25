import Foundation
import OISettings

/// launchd state of one LaunchAgent, from `launchctl print gui/<uid>/<label>`.
public enum LaunchdState: Equatable, Sendable {
    case running(pid: Int?)
    case notRunning          // loaded, not running (crashed / exited / waiting for throttle)
    case notLoaded           // `launchctl print` exit 113: not bootstrapped (not installed)

    /// `exitCode` != 0 means the service is not loaded (launchctl returns 113).
    public static func parse(printOutput: String, exitCode: Int32) -> LaunchdState {
        guard exitCode == 0 else { return .notLoaded }
        var state: String?
        var pid: Int?
        for raw in printOutput.split(separator: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            // Only the service's own top-level keys (one tab deep) matter; nested dicts
            // (e.g. "job state = exited" in sub-sections) are skipped by matching exact prefixes.
            if state == nil, line.hasPrefix("state = ") { state = String(line.dropFirst("state = ".count)) }
            if pid == nil, line.hasPrefix("pid = ") { pid = Int(line.dropFirst("pid = ".count)) }
        }
        return state == "running" ? .running(pid: pid) : .notRunning
    }
}

/// `launchctl print-disabled gui/<uid>`: `"label" => disabled|enabled` (older: `=> true|false`).
/// Returns nil when the label is not listed (launchd default: enabled).
public func parseDisabled(printDisabledOutput: String, label: String) -> Bool? {
    for raw in printDisabledOutput.split(separator: "\n") {
        let line = raw.trimmingCharacters(in: .whitespaces)
        guard line.hasPrefix("\"\(label)\"") else { continue }
        guard let arrow = line.range(of: "=>") else { continue }
        let v = line[arrow.upperBound...].trimmingCharacters(in: .whitespaces)
        if v == "disabled" || v == "true" { return true }
        if v == "enabled" || v == "false" { return false }
    }
    return nil
}

/// One oi-watcher capability, as reported by oi-watcher itself (status/oi-watcher.json).
public enum PermState: Equatable, Sendable {
    case granted
    case notGranted
    case denied
    case notAsked
    case unknown(String)

    public var symbol: String {
        switch self {
        case .granted: return "✓"
        case .notGranted, .denied, .notAsked: return "✗"
        case .unknown: return "?"
        }
    }

    public var detail: String {
        switch self {
        case .granted: return "granted"
        case .notGranted: return "not granted"
        case .denied: return "denied"
        case .notAsked: return "not yet asked"
        case .unknown(let why): return "unknown (\(why))"
        }
    }

    public static func from(_ a: WatcherStatus.Automation) -> PermState {
        switch a {
        case .granted: return .granted
        case .denied: return .denied
        case .notAsked: return .notAsked
        case .unknown: return .unknown("Safari not running")
        }
    }
}

/// The watcher's own view of its grants, from `<data_dir>/status/oi-watcher.json`.
public enum WatcherPerms: Equatable, Sendable {
    case fresh(WatcherStatus)
    case stale(seconds: Int)     // file older than WatcherStatus.staleAfter (or unparsable ts)
    case missing                 // no file / unreadable: watcher never ran the W7 build

    public static func from(_ status: WatcherStatus?, now: Date = Date()) -> WatcherPerms {
        guard let status else { return .missing }
        guard let age = status.age(now: now) else { return .stale(seconds: -1) }
        return age > WatcherStatus.staleAfter ? .stale(seconds: Int(age)) : .fresh(status)
    }

    /// (input monitoring, accessibility, automation → Safari)
    public var rows: (input: PermState, ax: PermState, safari: PermState) {
        switch self {
        case .fresh(let s):
            return (s.keyTap ? .granted : .notGranted, s.ax ? .granted : .notGranted, PermState.from(s.safariAutomation))
        case .stale, .missing:
            let u = PermState.unknown("watcher silent")
            return (u, u, u)
        }
    }

    public var headerSuffix: String {
        switch self {
        case .fresh: return ""
        case .stale(let sec): return sec < 0 ? " — watcher silent" : " — watcher silent (last report \(sec / 60) min ago)"
        case .missing: return " — watcher silent (no status file yet)"
        }
    }
}

/// Decoded `oi stats --day today --json` (only the fields the menu shows; never titles).
public struct TokenStats: Decodable, Equatable, Sendable {
    public var tokens: Int
    public var distinct: Int
    public var kinds: [(kind: String, count: Int)]

    public init(tokens: Int, distinct: Int, kinds: [(kind: String, count: Int)]) {
        self.tokens = tokens; self.distinct = distinct; self.kinds = kinds
    }

    enum CodingKeys: String, CodingKey { case tokens, kinds, distinct = "distinct_tokens" }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        tokens = try c.decode(Int.self, forKey: .tokens)
        distinct = (try? c.decode(Int.self, forKey: .distinct)) ?? 0
        let k = (try? c.decode([String: Int].self, forKey: .kinds)) ?? [:]
        kinds = k.map { (kind: $0.key, count: $0.value) }
            .sorted { $0.count != $1.count ? $0.count > $1.count : $0.kind < $1.kind }
    }

    public static func == (a: TokenStats, b: TokenStats) -> Bool {
        a.tokens == b.tokens && a.distinct == b.distinct
            && a.kinds.map(\.kind) == b.kinds.map(\.kind) && a.kinds.map(\.count) == b.kinds.map(\.count)
    }

    public static func decode(_ data: Data) -> TokenStats? { try? JSONDecoder().decode(TokenStats.self, from: data) }
}

public enum TokensInfo: Equatable, Sendable {
    case stats(TokenStats)
    case none(String)         // e.g. "no tokens yet today", "n/a (venv missing)"
}

public enum LoginState: Equatable, Sendable {
    case notInstalled         // no LaunchAgent plist (run install.sh)
    case enabled
    case disabled

    /// Launch at login = the LaunchAgent only (W9: the SMAppService login item is gone, one
    /// mechanism): ON when the plist is installed and not `launchctl disable`d.
    public static func resolve(agentInstalled: Bool, agentDisabled: Bool?) -> LoginState {
        guard agentInstalled else { return .notInstalled }
        return agentDisabled == true ? .disabled : .enabled
    }
}

/// Everything the menu shows, gathered off the main thread by the app.
public struct Status: Equatable, Sendable {
    public var watcher: LaunchdState
    public var paused: Bool
    public var awVersion: String?        // nil = unreachable
    public var hud: LaunchdState?        // nil = no com.osintent.oi-hud agent
    public var hudSocket: Bool
    public var hudEnabled: Bool
    public var tokens: TokensInfo
    public var watcherPerms: WatcherPerms
    public var login: LoginState
    public var rebuilding: Bool
    public var lastRebuild: String?
    public var denylistCount: Int
    public var live: LiveStatus? = nil    // W8: status/oi-live.json (LiveStatus.swift)
    public var liveNow: Date = Date()     // when `live` was read (staleness check)
    public var hudAX: PermState = .unknown("HUD silent")   // W16: status/oi-hud.json (HUDAXStatus.swift)
    public var onboarded: Bool = false    // W22: <data dir>/profile.yaml exists (Onboarding.swift)
    public var laya: LayaSwitchStatus? = nil   // `oi laya-switch status --json` (LayaSwitch.swift); nil = n/a
    public var layaBusy: Bool = false          // an `oi laya-switch on|off` is running

    public init(watcher: LaunchdState = .notLoaded, paused: Bool = false, awVersion: String? = nil,
                hud: LaunchdState? = nil, hudSocket: Bool = false, hudEnabled: Bool = true,
                tokens: TokensInfo = .none("n/a"), watcherPerms: WatcherPerms = .missing,
                login: LoginState = .notInstalled, rebuilding: Bool = false, lastRebuild: String? = nil,
                denylistCount: Int = 0) {
        self.watcher = watcher; self.paused = paused; self.awVersion = awVersion; self.hud = hud
        self.hudSocket = hudSocket; self.hudEnabled = hudEnabled; self.tokens = tokens
        self.watcherPerms = watcherPerms; self.login = login; self.rebuilding = rebuilding
        self.lastRebuild = lastRebuild; self.denylistCount = denylistCount
    }
}

/// Parse the last line of `oi tokens --day today` ("2026-10-20: 512 tokens (0 invalid) -> /path")
/// into a short line without the path.
public func summariseTokensOutput(_ out: String) -> String? {
    guard let line = out.split(separator: "\n").last(where: { $0.contains(" tokens (") }) else { return nil }
    let s = String(line)
    if let r = s.range(of: " -> ") { return String(s[..<r.lowerBound]) }
    return s
}
