import Foundation

/// What oi-watcher can actually do right now, as seen by oi-watcher itself (macOS checks
/// permissions per app, so only the watcher can tell). Written by the watcher to
/// `<data_dir>/status/oi-watcher.json` at start and every 30 s; read by oi-menubar for its
/// "Watcher permissions" rows (). Never contains event contents.
public struct WatcherStatus: Codable, Equatable, Sendable {
    /// Safari Automation as seen by the watcher.
    public enum Automation: String, Codable, Sendable {
        case granted, denied, notAsked = "not_asked", unknown
    }

    public var ts: String              // ISO-8601 UTC, when the watcher wrote the file
    public var keyTap: Bool            // listen-only event tap created (Input Monitoring effective)
    public var ax: Bool                // AXIsProcessTrusted()
    public var safariAutomation: Automation
    public var spaceAPI: Bool          // private CGS space-index API resolved
    public var pid: Int32

    public init(ts: String, keyTap: Bool, ax: Bool, safariAutomation: Automation, spaceAPI: Bool, pid: Int32) {
        self.ts = ts; self.keyTap = keyTap; self.ax = ax
        self.safariAutomation = safariAutomation; self.spaceAPI = spaceAPI; self.pid = pid
    }

    enum CodingKeys: String, CodingKey {
        case ts, ax, pid
        case keyTap = "key_tap", safariAutomation = "safari_automation", spaceAPI = "space_api"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ts = try c.decode(String.self, forKey: .ts)
        keyTap = try c.decode(Bool.self, forKey: .keyTap)
        ax = try c.decode(Bool.self, forKey: .ax)
        // An unknown future value must not make the whole file unreadable.
        safariAutomation = (try? c.decode(Automation.self, forKey: .safariAutomation)) ?? .unknown
        spaceAPI = (try? c.decode(Bool.self, forKey: .spaceAPI)) ?? false
        pid = (try? c.decode(Int32.self, forKey: .pid)) ?? 0
    }

    /// Maps `AEDeterminePermissionToAutomateTarget(..., askUserIfNeeded: false)`.
    /// -600 (Safari not running) and anything else → unknown.
    public static func automation(fromOSStatus status: Int32) -> Automation {
        switch status {
        case 0: return .granted
        case -1743: return .denied        // errAEEventNotPermitted
        case -1744: return .notAsked      // errAEEventWouldRequireUserConsent
        default: return .unknown
        }
    }

    public static var defaultURL: URL {
        SettingsStore.dataDir().appendingPathComponent("status/oi-watcher.json")
    }

    /// Older than this, the menu says "watcher silent" (the watcher writes every 30 s).
    public static let staleAfter: TimeInterval = 90

    public func encoded() throws -> Data {
        let e = JSONEncoder()
        e.outputFormatting = [.sortedKeys]
        return try e.encode(self) + Data("\n".utf8)
    }

    public static func decode(_ data: Data) -> WatcherStatus? { try? JSONDecoder().decode(WatcherStatus.self, from: data) }

    public static func load(_ url: URL = defaultURL) -> WatcherStatus? {
        guard let d = try? Data(contentsOf: url) else { return nil }
        return decode(d)
    }

    /// Seconds since `ts` (nil if `ts` does not parse).
    public func age(now: Date = Date()) -> TimeInterval? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: ts).map { now.timeIntervalSince($0) }
    }

    /// Atomic write (temp 0600 + rename), creating `status/` 0700 if missing.
    public func write(to url: URL = defaultURL) throws {
        try SettingsStore.atomicWrite(encoded(), to: url)
    }
}
