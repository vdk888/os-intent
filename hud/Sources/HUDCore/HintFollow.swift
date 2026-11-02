import Foundation

// W21b// should"): while a hint's dots are drawn they follow the pointer (≈ 80 ms lag, no jitter, kept on screen).
// The follow runs ONLY while the dots are drawn: it stops the moment the hint ends (develop, fade, Esc, replace),
// and the developed cross is anchored at the pointer of that moment (it does not follow). Pure: the controller
// owns the timer (60 Hz) and the panel.

public struct HintFollower: Sendable {
    /// The smoothing time constant: the dots trail the pointer by about this much.
    public static let lag: TimeInterval = 0.08
    /// Closer than this (pt) to the target: snap onto it (no endless sub-pixel creep, no shimmer).
    public static let snap = 0.5
    /// The timer's period.
    public static let interval: TimeInterval = 1.0 / 60
    /// A frame longer than this (a stalled run loop) counts as this long (no teleport after a hiccup).
    public static let maxStep: TimeInterval = 0.1

    public enum Transition: Equatable, Sendable { case start, stop }

    public private(set) var running = false
    /// The unrounded centre of the dots (Cocoa global coordinates); the panel origin is its rounded form.
    public private(set) var centre: (x: Double, y: Double) = (0, 0)

    public init() {}

    /// Should the follow run for this hint? Only while its dots are drawn and the full cross is not up.
    public static func wanted(_ life: HintLife, crossVisible: Bool) -> Bool { life.isDrawn && !crossVisible }

    /// Bring the follower in step with the hint: `.start` (create the timer) / `.stop` (invalidate it) / nil.
    public mutating func sync(_ life: HintLife, crossVisible: Bool, centre c: (x: Double, y: Double)) -> Transition? {
        let want = HintFollower.wanted(life, crossVisible: crossVisible)
        guard want != running else { return nil }
        running = want
        if want { centre = c }
        return want ? .start : .stop
    }

    /// One frame: the centre moves toward the pointer, kept so the dots (`halfSize` around it) stay on `screen`.
    /// Reduce Motion: straight onto the pointer (no easing). Returns the new centre (unchanged when not running).
    public mutating func step(pointer p: (x: Double, y: Double), dt: TimeInterval,
                              screen: (minX: Double, minY: Double, maxX: Double, maxY: Double),
                              halfSize: (w: Double, h: Double), reduceMotion: Bool) -> (x: Double, y: Double) {
        guard running else { return centre }
        let target = clampCentre(p, screen: screen, halfSize: halfSize)
        if reduceMotion {
            centre = target
            return centre
        }
        let a = 1 - exp(-min(max(dt, 0), HintFollower.maxStep) / HintFollower.lag)
        var next = (x: centre.x + (target.x - centre.x) * a, y: centre.y + (target.y - centre.y) * a)
        if hypot(target.x - next.x, target.y - next.y) < HintFollower.snap { next = target }
        centre = next
        return centre
    }

    /// ⌥⌘ develops the hint: the follow stops and the cross is anchored at the pointer of this moment (not where
    /// the trailing dots happen to be), so the options open under the fingers.
    public mutating func develop(pointer p: (x: Double, y: Double)) -> (x: Double, y: Double) {
        running = false
        return p
    }
}
