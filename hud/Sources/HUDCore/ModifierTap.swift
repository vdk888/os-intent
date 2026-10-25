import Foundation
import OISettings

/// The modifier keys the tap detector tracks (from CGEventFlags on flagsChanged events).
public struct HeldModifiers: OptionSet, Sendable, Hashable {
    public let rawValue: UInt8
    public init(rawValue: UInt8) { self.rawValue = rawValue }
    public static let control = HeldModifiers(rawValue: 1 << 0)
    public static let option = HeldModifiers(rawValue: 1 << 1)
    public static let command = HeldModifiers(rawValue: 1 << 2)
    public static let fn = HeldModifiers(rawValue: 1 << 3)
    public static let shift = HeldModifiers(rawValue: 1 << 4)   // a tap for summon only (W10)

    /// From raw CGEventFlags bits (maskControl 0x40000, maskAlternate 0x80000, maskCommand 0x100000,
    /// maskSecondaryFn 0x800000, maskShift 0x20000). Caps Lock and the numeric-pad bit are ignored.
    public init(cgEventFlags f: UInt64) {
        var m: HeldModifiers = []
        if f & 0x40000 != 0 { m.insert(.control) }
        if f & 0x80000 != 0 { m.insert(.option) }
        if f & 0x100000 != 0 { m.insert(.command) }
        if f & 0x800000 != 0 { m.insert(.fn) }
        if f & 0x20000 != 0 { m.insert(.shift) }
        self = m
    }

    /// The tap modifiers held, as a set (W11: a SET tap returns all of them).
    public var tapModifiers: Set<TapModifier> {
        var out = Set<TapModifier>()
        if contains(.control) { out.insert(.ctrl) }
        if contains(.option) { out.insert(.option) }
        if contains(.command) { out.insert(.cmd) }
        if contains(.fn) { out.insert(.fn) }
        if contains(.shift) { out.insert(.shift) }
        return out
    }

    /// The tap modifier when exactly one tappable modifier (and nothing else) is held.
    var single: TapModifier? { rawValue.nonzeroBitCount == 1 ? tapModifiers.first : nil }
}

/// Detects a bare modifier TAP (W9, shared by W10, sets W11). One episode runs from the
/// first modifier down to the moment none is held. It is a tap when no keyDown, click or scroll happened
/// in between and it was short: ≤ `maxDuration` (350 ms) for ONE modifier, ≤ `maxSetDuration` (500 ms,
/// first down → last up) for a SET (e.g. ⌥+⌘ pressed together, the W11 summon). The result is the set of
/// every modifier that went down in the episode, so ⌥ alone gives {⌥}, ⌥+⌘ together gives {⌥, ⌘}, and
/// ⌥ then ⌘ one after the other gives {⌥} then {⌘} (two episodes, never the set). A chord (⌘C, ⌥`,
/// ⌘-click, ⇧A, ⌥⌘C) is never a tap. Each consumer matches the exact set it wants: the option taps take
/// one-modifier sets only (so a ⌥+⌘ tap never fires option 2 or 3), the summon its configured set.
/// Pure: fed by oi-hud's passive event taps with the modifier state only (never a key code). ONE type,
/// two users (the per-show option taps and the permanent summon tap), each with its own instance.
public struct TapDetector: Sendable {
    public static let maxDuration: TimeInterval = 0.35
    public static let maxSetDuration: TimeInterval = 0.5
    /// CGEventType raw values that make a held modifier a chord (keyDown 10, left/right/other mouse down
    /// 1/3/25, scrollWheel 22; W11: ⇧/⌥⌘+scroll is a chord, never a summon). The monitor listens to these
    /// plus flagsChanged.
    public static let spoilerEventTypes: [UInt32] = [10, 1, 3, 25, 22]
    public let maxDuration: TimeInterval
    public let maxSetDuration: TimeInterval
    private var held: HeldModifiers = []
    private var episode: (mods: HeldModifiers, since: TimeInterval)?
    private var spoiled = false

    public init(maxDuration: TimeInterval = TapDetector.maxDuration, maxSetDuration: TimeInterval = TapDetector.maxSetDuration) {
        self.maxDuration = maxDuration; self.maxSetDuration = maxSetDuration
    }

    /// A flagsChanged event: `now` is the full modifier state after the change. Returns the tapped set
    /// on the release that completes a clean tap, else nil.
    public mutating func flagsChanged(_ now: HeldModifiers, at t: TimeInterval) -> Set<TapModifier>? {
        defer { held = now }
        if now.isEmpty {
            defer { episode = nil; spoiled = false }
            guard let e = episode, !spoiled, !e.mods.isEmpty else { return nil }
            let mods = e.mods.tapModifiers
            let limit = mods.count == 1 ? maxDuration : maxSetDuration
            guard t - e.since <= limit + 1e-9 else { return nil }
            return mods
        }
        if held.isEmpty {                       // first modifier down
            // flagsChanged reports one modifier at a time: two at once = one was down before we listened
            if now.single != nil { episode = (now, t); spoiled = false } else { episode = nil; spoiled = true }
            return nil
        }
        if let e = episode { episode = (e.mods.union(now), e.since) }   // more modifiers join the set
        return nil
    }

    /// A keyDown, a mouse click or a scroll: a modifier held now is part of a chord, not a tap.
    public mutating func otherInput() {
        if !held.isEmpty { spoiled = true }
    }

    public mutating func reset() { held = []; episode = nil; spoiled = false }

    /// W15: the tap (re)starts while modifiers are already down (e.g. ⌥⌘ still held after a glide summon): the
    /// release of those is never a tap.
    public mutating func prime(_ now: HeldModifiers) { held = now; episode = nil; spoiled = !now.isEmpty }
}
