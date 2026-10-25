import Foundation
import OISettings

// W15 hold ⌥⌘ + glide + dwell = a Claude-enriched marking menu (the ask:
// "when i hold option + cmd ... i can move the mouse pointer to an option, and if i stay it triggers context
// enrichment with claude, so i can chain clicks in relevant context, low distance").
//
// Pure parts (unit-tested): the hold-vs-tap detector of the summon SET, the glide state (hover, dwell,
// pending expansion, sub-rings, release), the sub-ring layout around a capsule, the hit test over every ring,
// the lighter tints of a sub-ring and the glide log sent back to the driver (paths and times only, no text).

// MARK: - hold vs tap

/// The summon SET (⌥+⌘ by default) held down ≥ `threshold` (250 ms) with nothing else (no key, click or scroll
/// before the threshold) = a HOLD: the glide mode. Released earlier it stays the W11 tap (TapDetector, ≤ 500 ms).
/// The threshold is checked by a timer (flagsChanged events do not come at 250 ms): `armed` asks the owner to call
/// `fire(at:)` after `threshold`. Only a set of two or more modifiers can be held (a single ⇧ is held to type).
public struct SetHoldDetector: Sendable {
    public static let defaultThreshold: TimeInterval = 0.25
    public let want: Set<TapModifier>
    public let threshold: TimeInterval
    private var held: HeldModifiers = []
    private var since: TimeInterval?
    private var spoiled = false
    public private(set) var holding = false

    public enum Event: Equatable, Sendable {
        case armed                                  // the exact set is down: call fire(at:) after `threshold`
        case released(downAt: TimeInterval)         // a hold ended (one of the set went up)
        case interrupted                            // a key went down during a hold (Esc, a chord): cancel
    }

    public init(want: Set<TapModifier>, threshold: TimeInterval = SetHoldDetector.defaultThreshold) {
        self.want = want; self.threshold = threshold
    }

    public var enabled: Bool { want.count >= 2 }

    /// `now` = the full modifier state after the change.
    public mutating func flagsChanged(_ now: HeldModifiers, at t: TimeInterval) -> Event? {
        defer { held = now; if now.isEmpty { spoiled = false } }
        guard enabled else { return nil }
        let set = now.tapModifiers
        if holding {
            if want.isSubset(of: set) { return nil }        // another modifier joining does not end it
            holding = false
            defer { since = nil }
            return .released(downAt: since ?? t)
        }
        if set == want {
            // reached from a part of the set (⌥ then ⌘), not by releasing a third modifier
            if since == nil, !spoiled, held.tapModifiers.isSubset(of: want) { since = t; return .armed }
            return nil
        }
        since = nil
        return nil
    }

    /// The timer: true exactly once, when the set has been held `threshold` with nothing else.
    public mutating func fire(at t: TimeInterval) -> Bool {
        guard !holding, !spoiled, let s = since, t - s >= threshold - 1e-9 else { return false }
        holding = true
        return true
    }

    /// A keyDown (`isKey`), a click or a scroll. Before the threshold it makes the set a chord (never a hold);
    /// during a hold a key interrupts it (Esc = cancel) and a click is just the click.
    public mutating func otherInput(isKey: Bool) -> Event? {
        if holding { return isKey ? .interrupted : nil }
        if !held.isEmpty { spoiled = true; since = nil }
        return nil
    }

    public mutating func reset() { held = []; since = nil; spoiled = false; holding = false }

    /// The monitor (re)starts with modifiers already down: their release is never a hold.
    public mutating func prime(_ now: HeldModifiers) { reset(); held = now; spoiled = !now.isEmpty }
}

// MARK: - sub-rings

/// One sub-ring: the follow-ups of the option at `parent` (a path of option keys, top ring first), drawn around
/// that capsule. `slots` are relative to the HUD centre (y-down), keyed by the sub-option key (1-4).
public struct SubRing: Equatable, Sendable {
    public var parent: [Int]
    public var options: [HUDOption]
    public var slots: [HUDSlot]
    public init(parent: [Int], options: [HUDOption], slots: [HUDSlot]) {
        self.parent = parent; self.options = options; self.slots = slots
    }
    public var depth: Int { parent.count }
    public func option(_ key: Int) -> HUDOption? { options.first { $0.key == key } }
    public func slot(_ key: Int) -> HUDSlot? { slots.first { $0.key == key } }
}

/// The capsule under `p` as a path: the deepest ring first (drawn on top), then the top ring.
public func glideHit(_ p: (x: Double, y: Double), top: [HUDSlot], rings: [SubRing]) -> [Int]? {
    for r in rings.reversed() {
        if let k = capsuleHit(p, slots: r.slots) { return r.parent + [k] }
    }
    return capsuleHit(p, slots: top).map { [$0] }
}

/// Sub-ring placement: each follow-up as close to its parent capsule as it fits, fanned around the direction
/// from `origin` (the HUD centre for the top ring, the grandparent capsule deeper) to the parent, never over
/// `avoid` (the cross, its centre, the other rings) nor another follow-up, inside `bounds` (the screen and the
/// panel, relative to the HUD centre). nil for a follow-up that cannot be placed (it is not drawn).
public enum SubRingLayout {
    public static let spread = 0.66              // radians between neighbours (≈ 38°)
    public static let angleStep = 0.2            // search step around the wanted angle
    public static let maxTurns = 6               // ± 6 steps (≈ ± 69°): never wraps back onto the cross
    public static let radiusStep = 6.0
    public static let maxRadius = 260.0
    public static let gap = 6.0

    public static func overlaps(_ a: HUDSlot, _ b: HUDSlot, gap: Double = SubRingLayout.gap) -> Bool {
        abs(a.x - b.x) < (a.width + b.width) / 2 + gap && abs(a.y - b.y) < (a.height + b.height) / 2 + gap
    }

    public static func inside(_ s: HUDSlot, _ b: (minX: Double, minY: Double, maxX: Double, maxY: Double)) -> Bool {
        s.x - s.width / 2 >= b.minX && s.x + s.width / 2 <= b.maxX && s.y - s.height / 2 >= b.minY && s.y + s.height / 2 <= b.maxY
    }

    public static func place(parent: HUDSlot, origin: (x: Double, y: Double), keys: [Int], widths: [Double],
                             height: Double, avoid: [HUDSlot],
                             bounds: (minX: Double, minY: Double, maxX: Double, maxY: Double)) -> [HUDSlot?] {
        var dx = parent.x - origin.x, dy = parent.y - origin.y
        if hypot(dx, dy) < 1e-6 { dx = 1; dy = 0 }
        let base = atan2(dy, dx)
        let n = keys.count
        var placed: [HUDSlot] = []
        var out: [HUDSlot?] = []
        let turns = [0] + (1...maxTurns).flatMap { [$0, -$0] }
        for i in 0..<n {
            let want = base + (Double(i) - Double(n - 1) / 2) * spread
            var found: HUDSlot?
            var r = height / 2 + gap
            search: while r <= maxRadius {
                for k in turns {
                    let a = want + Double(k) * angleStep
                    let s = HUDSlot(key: keys[i], x: parent.x + r * cos(a), y: parent.y + r * sin(a),
                                    width: widths[i], height: height, position: nil)
                    guard inside(s, bounds), !overlaps(s, parent) else { continue }
                    guard !avoid.contains(where: { overlaps(s, $0) }), !placed.contains(where: { overlaps(s, $0) }) else { continue }
                    found = s
                    break search
                }
                r += radiusStep
            }
            if let f = found { placed.append(f) }
            out.append(found)
        }
        return out
    }
}

/// A sub-ring's colour: the parent's colour family, lighter with each level and a touch per option
/// (mixed toward white; "#RRGGBB" in and out, nil when the base is malformed).
/// W23: colour inheritance per branch: every node of a branch carries its ROOT key's colour, lighter per
/// depth; the per-option touch is capped below one depth step, so a deeper level is always lighter than the one above.
public enum GlideTint {
    public static func fraction(depth: Int, index: Int) -> Double {
        min(0.75, 0.22 * Double(depth) + 0.07 * Double(min(max(index, 0), 3)))
    }

    /// The tint of the node at `path` (root key first) from the root key's colour `base`.
    public static func hex(base: String, path: [Int]) -> String? {
        guard path.count >= 2, let last = path.last else { return path.count == 1 ? base : nil }
        return hex(base: base, depth: path.count - 1, index: last - 1)
    }

    public static func hex(base: String, depth: Int, index: Int) -> String? {
        guard let c = HUDColor.rgb(base) else { return nil }
        let f = fraction(depth: depth, index: index)
        func mix(_ v: Double) -> Int { Int(((v + (1 - v) * f) * 255).rounded()) }
        return String(format: "#%02X%02X%02X", mix(c.r), mix(c.g), mix(c.b))
    }
}

// MARK: - the glide

/// One option the pointer rested on during a glide (path + how long, ms).
public struct GlideVisit: Equatable, Sendable {
    public var path: [Int]
    public var ms: Int
    public init(path: [Int], ms: Int) { self.path = path; self.ms = ms }
}

/// What a glide sends back with its `pressed` / `dismissed` (training signal: paths and times, never text).
public struct GlideLog: Equatable, Sendable {
    public var visited: [GlideVisit]
    public var expanded: Int          // sub-rings opened
    public var final: [Int]?          // the executed path (nil = cancelled)
    public var depth: Int             // final.count - 1, or the deepest path visited when cancelled

    public init(visited: [GlideVisit], expanded: Int, final: [Int]?, depth: Int) {
        self.visited = visited; self.expanded = expanded; self.final = final; self.depth = depth
    }

    public var json: [String: Any] {
        var o: [String: Any] = ["visited": visited.map { ["path": $0.path, "ms": $0.ms] as [String: Any] },
                                "expanded": expanded, "depth": depth]
        if let final { o["final"] = final }
        return o
    }
}

public enum GlideRelease: Equatable, Sendable {
    case execute([Int])     // the option under the pointer (any level)
    case cancel             // empty space
    case keepAsTap          // a short hold with nothing visited: the HUD stays, as the W11 tap would leave it
}

/// The glide while ⌥⌘ is held: hover → dwell (settings hud.dwell_ms) → expand request → sub-ring, up to depth 3.
public struct GlideState: Sendable {
    public static let maxDepth = 3
    public static let thinkingDelay: TimeInterval = 0.15        // the progress arc shows after 150 ms
    public static let expandTimeout: TimeInterval = 9           // python answers within its 8 s budget
    public static let minVisitMs = 60                           // shorter = crossing a capsule, not a visit
    public static let maxVisits = 32
    public let dwell: TimeInterval
    public private(set) var active = false
    public private(set) var startedAt: TimeInterval = 0
    public private(set) var rings: [SubRing] = []
    public private(set) var hover: [Int]?
    private var hoverSince: TimeInterval = 0
    public private(set) var pending: [Int]?
    private var pendingSince: TimeInterval = 0
    private var leaves: Set<[Int]> = []                         // answered with nothing: not asked again
    public private(set) var visits: [GlideVisit] = []
    public private(set) var expanded = 0
    private var everHovered = false
    /// the last path whose dwell completed (asked, open, pending or a known leaf): the context card's level.
    public private(set) var dwelt: [Int]?

    public init(dwellMs: Int = HUDSettings.defaultDwellMs) { dwell = Double(HUDSettings.clampDwell(dwellMs)) / 1000 }

    public mutating func begin(at t: TimeInterval) {
        self = GlideState(dwellMs: Int((dwell * 1000).rounded()))
        active = true; startedAt = t
    }

    public mutating func end() { active = false; rings = []; hover = nil; pending = nil; leaves = []; dwelt = nil }

    private mutating func closeVisit(at t: TimeInterval) {
        guard let h = hover else { return }
        let ms = Int(((t - hoverSince) * 1000).rounded())
        if ms >= GlideState.minVisitMs, visits.count < GlideState.maxVisits { visits.append(GlideVisit(path: h, ms: ms)) }
    }

    /// The pointer is over `path` (nil = empty space) at `t`. Returns the path to expand when the dwell on it
    /// completes (once; not when its ring is open, pending, a known leaf, or deeper than `maxDepth`).
    public mutating func hover(_ path: [Int]?, at t: TimeInterval, expandable: ([Int]) -> Bool = { _ in true }) -> [Int]? {
        guard active else { return nil }
        if path != hover {
            closeVisit(at: t)
            hover = path; hoverSince = t
            if path != nil { everHovered = true }
            return nil
        }
        guard let p = hover, t - hoverSince >= dwell - 1e-9 else { return nil }
        dwelt = p
        guard p.count <= GlideState.maxDepth, pending != p,
              !leaves.contains(p), !rings.contains(where: { $0.parent == p }), expandable(p) else { return nil }
        pending = p; pendingSince = t                // a newer dwell supersedes an older request
        return p
    }

    /// The driver's answer for `path`. Accepted only for the pending request; empty = a leaf. The rings kept are
    /// the ancestors of `path` (a sibling's ring closes).
    public mutating func subring(_ path: [Int], options: [HUDOption], slots: [HUDSlot]) -> Bool {
        guard active, pending == path else { return false }
        pending = nil
        let pairs = zip(options, slots).filter { o, s in o.key == s.key }
        guard !pairs.isEmpty else { leaves.insert(path); return true }
        rings = rings.filter { $0.parent.count < path.count && path.starts(with: $0.parent) }
            + [SubRing(parent: path, options: pairs.map { $0.0 }, slots: pairs.map { $0.1 })]
        expanded += 1
        return true
    }

    /// A request unanswered for `expandTimeout`: given up (a leaf, the arc stops).
    public mutating func expire(at t: TimeInterval) {
        if let p = pending, t - pendingSince >= GlideState.expandTimeout { pending = nil; leaves.insert(p) }
    }

    /// The thin progress arc while the driver (Claude) works: the pending path and a fill that creeps toward 1.
    public func thinking(at t: TimeInterval) -> (path: [Int], fill: Double)? {
        guard let p = pending, t - pendingSince >= GlideState.thinkingDelay else { return nil }
        let e = t - pendingSince - GlideState.thinkingDelay
        return (p, min(0.95, 0.08 + 0.92 * (1 - exp(-e / 1.2))))
    }

    public func option(at path: [Int], top: [HUDOption]) -> HUDOption? {
        guard let first = path.first else { return nil }
        if path.count == 1 { return top.first { $0.key == first } }
        return rings.first { $0.parent == Array(path.dropLast()) }?.option(path.last!)
    }

    /// ⌥⌘ released at `t`. `tapWindow`: the set went down ≤ 500 ms ago (a slow tap must stay a tap).
    public mutating func release(at t: TimeInterval, tapWindow: Bool) -> GlideRelease {
        closeVisit(at: t)
        hoverSince = t
        if let h = hover { return .execute(h) }
        if tapWindow, !everHovered, rings.isEmpty { return .keepAsTap }
        return .cancel
    }

    public func log(final: [Int]?) -> GlideLog {
        let depth = final.map { $0.count - 1 } ?? max(0, (visits.map(\.path.count).max() ?? 1) - 1)
        return GlideLog(visited: visits, expanded: expanded, final: final, depth: depth)
    }
}
