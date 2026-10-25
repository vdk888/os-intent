import Foundation
import OISettings

// W19: automatic shows become HINTS. `oi live` sends
// `{"cmd":"show","mode":"hint","hint_ttl_ms":2500,"ttl_ms":6000,…}` → the HUD draws only tiny coloured dots
// at the fixed places of the options (no labels, icons, taps or target hints). The summon key (⌥⌘) tapped
// while the dots are up, or within `hint_ttl` after they faded, DEVELOPS them: the same options become the
// full cross (with `ttl_ms`), on the same connection. ⌥⌘ held from a hint = the W15 glide on them.

/// Default `hint_ttl_ms` (settings.json `hud.hint_ttl_ms`): how long the dots stay, then fade.
/// W20: 2500 -> 4000.
public let defaultHintTTLMs = HUDSettings.defaultHintTTLms
/// Dot fade-in (the card: 120 ms).
public let hintFadeIn: TimeInterval = 0.12

/// W24: what a hint's marker says. `ring` = proactive ("something changed": a mail, an agent message, a new
/// file, a cockpit alert), drawn as a thin ring around the dots; no marker = a W19 prediction hint.
public enum HintMarker: String, Sendable, Equatable {
    case ring
}

/// The lifecycle of one hint: pure, unit-tested; the controller (oi-hud) owns the timers and the drawing.
public struct HintLife: Sendable {
    public enum Phase: Equatable, Sendable {
        case idle
        case dots(since: TimeInterval)           // the dots are drawn
        case faded(at: TimeInterval)             // gone from the screen, still developable for `hintTTL`
    }

    public private(set) var phase: Phase = .idle
    /// The hinted options (predicted only, keys 1-4), the full ttl after development, the hint ttl.
    public private(set) var options: [HUDOption] = []
    public private(set) var fullTTLms = 0
    public private(set) var hintTTL: TimeInterval = Double(defaultHintTTLMs) / 1000
    /// W24: the hint's marker (nil = a prediction hint).
    public private(set) var marker: HintMarker?

    public init() {}

    public var isDrawn: Bool { if case .dots = phase { return true }; return false }
    public var isIdle: Bool { phase == .idle }

    /// What `{"cmd":"hide"}` ends (W24: oi live withdraws a proactive hint when the event's state ends — the mail was
    /// read, the alert closed): the cross when it is up, else this hint (drawn or faded-but-developable), else nothing.
    public enum HideTarget: Equatable, Sendable { case cross, hint, nothing }
    public func hideTarget(crossVisible: Bool) -> HideTarget {
        crossVisible ? .cross : (isIdle ? .nothing : .hint)
    }

    /// A new hint (replaces any previous one). Only keys 1-4 get a dot.
    public mutating func show(_ opts: [HUDOption], fullTTLms: Int, hintTTLms: Int, at t: TimeInterval,
                              marker: HintMarker? = nil) {
        options = opts.filter { (1...maxOptions).contains($0.key) }.sorted { $0.key < $1.key }
        self.marker = marker
        self.fullTTLms = fullTTLms
        hintTTL = Double(hintTTLms) / 1000
        phase = .dots(since: t)
    }

    /// The dots' own ttl is over: they fade (the hint stays developable for another `hintTTL`).
    /// Returns true when it was drawn (the controller removes the dots).
    @discardableResult
    public mutating func fade(at t: TimeInterval) -> Bool {
        guard isDrawn else { return false }
        phase = .faded(at: t)
        return true
    }

    /// When the dots must fade (nil when not drawn).
    public var fadeDue: TimeInterval? { if case .dots(let s) = phase { return s + hintTTL }; return nil }
    /// When the grace after the fade ends (nil when not faded): the owner is then told `dismissed: hint_ttl`.
    public var graceDue: TimeInterval? { if case .faded(let f) = phase { return f + hintTTL }; return nil }

    /// Can a summon tap at `t` develop this hint? (the dots are up, or faded less than `hintTTL` ago)
    public func canDevelop(at t: TimeInterval) -> Bool {
        switch phase {
        case .idle: return false
        case .dots: return true
        case .faded(let f): return t - f <= hintTTL
        }
    }

    /// The summon key at `t`: the options to draw in full and their ttl; nil = nothing to develop (the summon
    /// goes on as today). The hint is then over (idle).
    public mutating func develop(at t: TimeInterval) -> (options: [HUDOption], ttlMs: Int)? {
        guard canDevelop(at: t) else { return nil }
        defer { reset() }
        return (options, fullTTLms)
    }

    /// W14 label enrichment arriving while the dots are up (or faded): the developed cross shows the new labels.
    public mutating func relabel(_ updates: [LabelUpdate]) -> Int {
        guard !isIdle else { return 0 }
        let (o, n) = LabelUpdate.apply(updates, to: options)
        options = o
        return n
    }

    public mutating func reset() { phase = .idle; options = []; fullTTLms = 0; marker = nil }

    /// `oi-hud state` → `hint`: "idle" | "dots:1,2,3" | "dots:1,2:ring" (W24) | "faded".
    public var stateName: String {
        switch phase {
        case .idle: return "idle"
        case .dots: return "dots:" + options.map { String($0.key) }.joined(separator: ",") + (marker.map { ":\($0.rawValue)" } ?? "")
        case .faded: return "faded"
        }
    }
}

/// The dots' geometry: one dot per option, in the DIRECTION of its fixed place, close to the pointer
/// (the full cross later springs out from there). Offsets are y-down from the anchor.
/// W20: 10-pt dots (settings
/// `hud.hint_dot_size`), 0.85 opaque, a thin white/black ring for contrast on any background, 22 pt from the
/// pointer, a gentle 2-cycle pulse on appear (none with Reduce Motion).
public enum HintDotLayout {
    public static let dotSize: Double = Double(HUDSettings.defaultHintDotSize)
    public static let radius: Double = 22
    public static let alpha: Double = 0.85
    /// The appear pulse: scale 1 -> `pulseScale` -> 1, `pulseCycles` times, `pulseCycle` s each.
    public static let pulseCycles = 2
    public static let pulseScale: Double = 1.35
    public static let pulseCycle: TimeInterval = 0.45

    public static func offsets(keys: [Int], positions: [Int: SlotPosition], radius: Double = radius) -> [(key: Int, x: Double, y: Double)] {
        keys.compactMap { k in
            guard let p = positions[k] else { return nil }
            let o = FixedSlots.offset(p, radius: radius)
            return (k, o.x, o.y)
        }
    }

    /// The dot's drawing: fill opacity, the inner white ring and the outer black hairline (pt). Increase
    /// Contrast = fully opaque and thicker rings.
    public struct Style: Equatable, Sendable {
        public let alpha: Double
        public let whiteRing: Double
        public let blackRing: Double
    }

    public static func style(increaseContrast: Bool) -> Style {
        increaseContrast ? Style(alpha: 1, whiteRing: 2, blackRing: 1) : Style(alpha: alpha, whiteRing: 1.25, blackRing: 0.5)
    }

    /// How many pulse cycles on appear (0 with Reduce Motion).
    public static func pulses(reduceMotion: Bool) -> Int { reduceMotion ? 0 : pulseCycles }

    /// Half the square the dots (rings and pulse included) need around the pointer, for the screen clamp.
    /// W24: a ring marker needs its own radius (plus its stroke).
    public static func halfExtent(dotSize: Double, marker: HintMarker? = nil) -> Double {
        let dots = radius + dotSize * pulseScale / 2 + 3
        return (marker == .ring ? max(dots, ringRadius(dotSize: dotSize) + ringWidth + 2) : dots).rounded(.up)
    }

    /// W24: the proactive ring around the dots — just outside them (pulse included), thin, white with a black hairline.
    public static let ringWidth: Double = 1.5
    public static let ringAlpha: Double = 0.8
    public static func ringRadius(dotSize: Double) -> Double { radius + dotSize * pulseScale / 2 + 4 }
}

// W19: an option's badge shows its NUMBER first,
// then the key symbol: "1 fn", "2 ⌃", "3 ⌥", "4 ⌘" (W21: the number is the key). A chord that already carries the number ("⌥1", the fallback
// without Input Monitoring) stays as is; the wish (⇧) and vocal (⌃⌥) badges are not option badges (no number).
public func optionBadge(key: Int, symbol: String) -> String {
    guard (1...maxOptions).contains(key), !symbol.isEmpty, !symbol.hasSuffix(String(key)) else { return symbol }
    return "\(key) \(symbol)"
}

/// "2 ⌃" → ("2", "⌃") for the two-weight badge (number bold, symbol secondary); nil = draw the badge as one text.
public func splitOptionBadge(_ badge: String) -> (number: String, symbol: String)? {
    let parts = badge.split(separator: " ", maxSplits: 1)
    guard parts.count == 2, let n = Int(parts[0]), (1...maxOptions).contains(n) else { return nil }
    return (String(parts[0]), String(parts[1]))
}
