import Foundation
import OISettings

/// W10: the SUMMON key. W11: ⌥+⌘ pressed together then released (settings.json
/// `hotkeys.summon`, default `{"tap":["option","cmd"]}`; W10 was a bare ⇧ tap) asks for the HUD now: oi-hud broadcasts `{"event":"summon","ts":…}` to every connected client, the
/// live driver (`oi live`) answers with a `show` built from the current moment (no threshold, no
/// cooldown), and if no `show` arrives within `summonFallbackDelay` the HUD shows its fallback: the
/// wish option alone ("Tell me what you wanted"), so the key always gives feedback.

/// How long oi-hud waits for the live driver's `show` before drawing the fallback.
public let summonFallbackDelay: TimeInterval = 0.4

/// Carbon id of the summon hotkey when it is a chord (the tap form needs no Carbon registration).
public let summonHotkeyID: UInt32 = 300

/// Why a summon is ignored (nil = go). Order: HUD off, logging paused, HUD already on screen,
/// wish panel open (never cover it).
public enum SummonGate {
    public static func check(hudEnabled: Bool, paused: Bool, visible: Bool, wishOpen: Bool) -> String? {
        if !hudEnabled { return "hud_disabled" }
        if paused { return "paused" }
        if visible { return "visible" }
        if wishOpen { return "wish_open" }
        return nil
    }
}

/// The pending summon: opened by a summon, closed by the first `show` that arrives within
/// `delay` (that show skips the rate limit: it is the answer) or by the fallback.
public struct SummonWindow: Sendable {
    public let delay: TimeInterval
    private var since: TimeInterval?

    public init(delay: TimeInterval = summonFallbackDelay) { self.delay = delay }

    public var isOpen: Bool { since != nil }

    public mutating func begin(at t: TimeInterval) { since = t }

    /// The wish-only fallback is on screen (set by `fallbackShown()`, cleared by `hidden()`).
    public private(set) var fallbackOnScreen = false

    /// A client `show` arrived: true (and closes the window) when a summon is pending, or when the
    /// fallback is still on screen (W11, W10 review: a late live answer replaces the fallback in place
    /// and skips the rate limit, it is still the answer to the summon).
    public mutating func answer() -> Bool {
        defer { since = nil; fallbackOnScreen = false }
        return since != nil || fallbackOnScreen
    }

    /// The fallback timer fired at `t`: true (and closes the window) when nothing answered.
    public mutating func fallbackDue(at t: TimeInterval) -> Bool {
        guard let s = since, t - s >= delay - 1e-6 else { return false }
        since = nil
        return true
    }

    /// The fallback was drawn.
    public mutating func fallbackShown() { fallbackOnScreen = true }

    /// The HUD was hidden (finish): a later show is an ordinary show again.
    public mutating func hidden() { fallbackOnScreen = false }
}

/// What to arm for the summon key: a permanent passive tap, a permanent Carbon chord, or nothing.
/// An invalid hotkey set falls back to the defaults as a whole (as the option keys and wish do).
public enum SummonArm: Equatable, Sendable {
    case tap(Set<TapModifier>)          // one modifier, or a SET pressed together (W11 default ⌥+⌘)
    case chord(HotkeyRegistration)
    case none

    public init(hotkeys: HotkeySet) {
        let set = hotkeys.problems().isEmpty ? hotkeys : HotkeySet.defaults
        if set.summon.isTap { self = .tap(Set(set.summon.tapSet)); return }
        guard let code = set.summon.keyCode else { self = .none; return }
        self = .chord(HotkeyRegistration(code: UInt32(code), mods: set.summon.carbonModifiers, id: summonHotkeyID))
    }

    /// The summon key as shown in `oi-hud state` and the logs: "⌥⌘", "⇧", "⌥⇧Space".
    public static func display(_ hotkeys: HotkeySet) -> String {
        (hotkeys.problems().isEmpty ? hotkeys : HotkeySet.defaults).summon.display
    }
}
