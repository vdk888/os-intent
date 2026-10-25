import Foundation
import OISettings

/// At most one show per `minInterval` seconds; extra shows are dropped.
public struct RateLimiter: Sendable {
    public let minInterval: TimeInterval
    private var last: TimeInterval?

    public init(minInterval: TimeInterval) { self.minInterval = minInterval }

    /// Returns true (and records `now`) if a show is allowed at `now`.
    public mutating func allow(now: TimeInterval) -> Bool {
        if let last, now - last < minInterval { return false }
        last = now
        return true
    }
}

/// The ttl timer never fires before this (W9: time to read and decide).
public let minTTL: TimeInterval = 1.5

/// The ttl timer actually armed: the requested ttl, never shorter than `minTTL`.
public func effectiveTTL(ms: Int) -> TimeInterval { max(Double(ms) / 1000, minTTL) }

/// Trips when the pointer moves faster than `speedThreshold` px/s for at least `holdTime` s,
/// never within `grace` s of the show (`reset(shownAt:)`).
/// Fed with pointer samples (a 60 Hz timer while the HUD is visible).
/// W9: 1200 px/s sustained 250 ms, nothing in the first 2 s
/// (was 600 px/s for 100 ms from the first frame: the HUD vanished before he could decide).
/// The values come from settings.json `gate` (OISettings.pointerHide).
public struct VelocityGate: Sendable {
    public let speedThreshold: Double
    public let holdTime: TimeInterval
    public let grace: TimeInterval
    private var prev: (t: TimeInterval, x: Double, y: Double)?
    private var fastSince: TimeInterval?
    private var shownAt: TimeInterval?

    public init(speedThreshold: Double = 1200, holdTime: TimeInterval = 0.25, grace: TimeInterval = 2.0) {
        self.speedThreshold = speedThreshold; self.holdTime = holdTime; self.grace = grace
    }

    public init(_ p: PointerHide) {
        self.init(speedThreshold: p.fastPxS, holdTime: p.fastMs / 1000, grace: p.graceMs / 1000)
    }

    public mutating func reset(shownAt: TimeInterval? = nil) { prev = nil; fastSince = nil; self.shownAt = shownAt }

    /// Returns true once the pointer has been fast for `holdTime` (after the grace period).
    public mutating func feed(t: TimeInterval, x: Double, y: Double) -> Bool {
        defer { prev = (t, x, y) }
        if let s = shownAt, t - s < grace { fastSince = nil; return false }
        guard let p = prev, t > p.t else { return false }
        let speed = hypot(x - p.x, y - p.y) / (t - p.t)
        if speed > speedThreshold {
            // sustained travel is counted from the end of the grace period at the earliest
            if fastSince == nil { fastSince = max(p.t, (shownAt ?? -.infinity) + grace) }
            return t - fastSince! >= holdTime
        }
        fastSince = nil
        return false
    }
}

/// settings.json `hud_enabled`: when false every `show` is answered with
/// `{"event":"dropped","reason":"disabled"}` and nothing is drawn. Checked before the rate limit.
public enum EnabledGate {
    public static func check(hudEnabled: Bool) -> HUDEvent? {
        hudEnabled ? nil : .dropped(reason: "disabled")
    }
}
