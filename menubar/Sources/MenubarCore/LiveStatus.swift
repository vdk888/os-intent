import Foundation

/// W8: `<data dir>/status/oi-live.json`, rewritten every loop (~2 s) by `oi live`.
/// Only counts and timestamps (never a title, URL or label).
public struct LiveStatus: Decodable, Equatable, Sendable {
    public var ts: String
    public var state: String              // running | paused | hud_disabled
    public var shows_today: Int
    public var pressed_today: Int
    public var last_show_ts: String?
    public var model_trained_at: String?
    public var moments_today: Int?

    public static let label = "com.osintent.oi-live"
    public static let staleAfter: TimeInterval = 15

    public init(ts: String, state: String, shows_today: Int = 0, pressed_today: Int = 0,
                last_show_ts: String? = nil, model_trained_at: String? = nil, moments_today: Int? = nil) {
        self.ts = ts; self.state = state; self.shows_today = shows_today; self.pressed_today = pressed_today
        self.last_show_ts = last_show_ts; self.model_trained_at = model_trained_at; self.moments_today = moments_today
    }

    public static func decode(_ data: Data) -> LiveStatus? { try? JSONDecoder().decode(LiveStatus.self, from: data) }

    public static func load(dataDir: URL) -> LiveStatus? {
        guard let d = try? Data(contentsOf: dataDir.appendingPathComponent("status/oi-live.json")) else { return nil }
        return decode(d)
    }

    static func date(_ s: String?) -> Date? {
        guard let s else { return nil }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }

    /// "Live predictor: running · shows today 3 · pressed 1 · last 14:02" (local HH:mm).
    public static func text(_ s: LiveStatus?, now: Date = Date(), tz: TimeZone = .current) -> String {
        guard let s else { return "Live predictor: stopped (no status yet)" }
        let fresh = date(s.ts).map { now.timeIntervalSince($0) <= staleAfter } ?? false
        let state: String
        if !fresh { state = "stopped" }
        else if s.state == "hud_disabled" { state = "running — HUD disabled" }
        else { state = s.state }
        var out = "Live predictor: \(state) · shows today \(s.shows_today) · pressed \(s.pressed_today)"
        if let d = date(s.last_show_ts) {
            let f = DateFormatter(); f.dateFormat = "HH:mm"; f.timeZone = tz
            out += " · last \(f.string(from: d))"
        }
        return out
    }
}

extension MenuModel {
    /// The live-driver row + its restart item (inserted after the HUD row).
    public static func liveEntries(_ s: Status) -> [MenuEntry] {
        [.init(id: "live", title: LiveStatus.text(s.live, now: s.liveNow), kind: .info, enabled: false),
         .init(id: "restart.live", title: "Restart live predictor", kind: .action(.restartLive), indent: 1,
               tooltip: "launchctl kickstart -k \(LiveStatus.label) (install: live/install.sh)")]
    }
}
