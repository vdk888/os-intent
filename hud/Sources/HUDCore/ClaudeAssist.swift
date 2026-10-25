import Foundation
import OISettings

// W14 "Claude in the app": a long-press on an option's modifier (≥ settings hud.long_press_ms, default
// 600 ms) = the Claude-assisted variant of its primitive (the ask: "if i press 'respond to email' option,
// for longer, it also writes the response with context in mind"). Pure parts: the long-press detector, the
// HUD's Claude work state (spinner → result card or the action) and the card text.

/// Detects a LONG press of ONE modifier held alone: down, nothing else (no other modifier, key, click or
/// scroll), released after `threshold`. A short tap stays the `TapDetector`'s (≤ 350 ms); between the two
/// nothing fires. Fed by the same passive event tap as the option taps (modifier state only, never a key code).
public struct LongPressDetector: Sendable {
    public let threshold: TimeInterval
    /// The ring is drawn only after this much of the hold (a normal tap never flashes it).
    public static let ringDelay: TimeInterval = 0.15
    private var held: HeldModifiers = []
    private var holding: (mod: TapModifier, since: TimeInterval)?

    public init(thresholdMs: Int = HUDSettings.defaultLongPressMs) {
        threshold = Double(HUDSettings.clampLongPress(thresholdMs)) / 1000
    }

    /// `now` = the full modifier state after the change. Returns the modifier on the release that completes a
    /// long press, else nil.
    public mutating func flagsChanged(_ now: HeldModifiers, at t: TimeInterval) -> TapModifier? {
        defer { held = now }
        if now.isEmpty {
            defer { holding = nil }
            guard let h = holding, t - h.since >= threshold - 1e-9 else { return nil }
            return h.mod
        }
        if held.isEmpty, let m = now.single, TapModifier.optionTaps.contains(m) {
            holding = (m, t)                       // one option modifier went down alone
        } else {
            holding = nil                          // a second modifier joined (a set, a chord): not a long press
        }
        return nil
    }

    /// A keyDown, click or scroll while held: a chord, never a long press.
    public mutating func otherInput() { holding = nil }

    public mutating func reset() { held = []; holding = nil }

    /// W15: started while modifiers are down: none of them is a long press.
    public mutating func prime(_ now: HeldModifiers) { held = now; holding = nil }

    /// The modifier being held and the ring's fill (0...1), nil when nothing is held or the ring is not due yet.
    public func progress(at t: TimeInterval) -> (mod: TapModifier, fill: Double)? {
        guard let h = holding, t - h.since >= LongPressDetector.ringDelay else { return nil }
        return (h.mod, min(1, max(0, (t - h.since) / threshold)))
    }
}

/// What the HUD does while Claude works on a long-pressed option.
public enum ClaudeWorkState: Equatable, Sendable {
    case idle
    case working(key: Int, since: TimeInterval)   // spinner on the capsule, ttl paused, only Esc hides
    case card(key: Int, text: String)             // the result card (≤ 4 lines), hidden after its ttl or Esc
}

public struct ClaudeWork: Sendable {
    /// Safety net: the python side answers within its 25 s budget (+ AppleScript); after this the HUD gives up.
    public static let maxWait: TimeInterval = 45
    public private(set) var state: ClaudeWorkState = .idle

    public init() {}

    public var isWorking: Bool { if case .working = state { return true }; return false }
    public var isBusy: Bool { state != .idle }
    public var workingKey: Int? { if case .working(let k, _) = state { return k }; return nil }
    public var cardText: String? { if case .card(_, let t) = state { return t }; return nil }

    public mutating func begin(key: Int, at t: TimeInterval) { state = .working(key: key, since: t) }

    public enum ResultAction: Equatable, Sendable {
        case cancelled                   // nothing was waiting for it (Esc, ttl, another show): python does nothing
        case showCard(String)
        case hide                        // ok and no text: the action itself is the result
    }

    /// The `result` command for `key`.
    public mutating func result(key: Int, text: String?) -> ResultAction {
        guard case .working(let k, _) = state, k == key else { return .cancelled }
        if let t = CardText.clean(text) { state = .card(key: key, text: t); return .showCard(t) }
        state = .idle
        return .hide
    }

    /// Whether a hide for `reason` is allowed now: while Claude works only Esc, the hide command and the safety
    /// timeout hide the HUD (not the ttl, typing or pointer travel); a card goes with anything.
    public func allowsDismiss(_ reason: String) -> Bool {
        guard isWorking else { return true }
        return ["esc", "hide_cmd", "claude_timeout"].contains(reason)
    }

    public func timedOut(at t: TimeInterval) -> Bool {
        if case .working(_, let since) = state { return t - since >= ClaudeWork.maxWait }
        return false
    }

    public mutating func reset() { state = .idle }
}

/// The result card's text: at most 4 non-empty lines of at most 64 characters (whitespace collapsed).
public enum CardText {
    public static let maxLines = 4
    public static let maxChars = 64

    public static func clean(_ text: String?) -> String? {
        guard let text else { return nil }
        let lines = text.split(whereSeparator: \.isNewline)
            .map { $0.split(whereSeparator: \.isWhitespace).joined(separator: " ") }
            .filter { !$0.isEmpty }
            .prefix(maxLines)
            .map { $0.count > maxChars ? String($0.prefix(maxChars - 1)) + "…" : $0 }
        return lines.isEmpty ? nil : lines.joined(separator: "\n")
    }
}

/// `{"cmd":"update","options":[{"key":1,"label":"Reply to Sam","expect":"Reply"}]}` (W14 label enrichment):
/// the new label is applied only while option `key` still shows `expect` (the same option, not a newer show's).
public struct LabelUpdate: Codable, Equatable, Sendable {
    public var key: Int
    public var label: String
    public var expect: String?
    public init(key: Int, label: String, expect: String? = nil) { self.key = key; self.label = label; self.expect = expect }

    /// `options` with the updates applied; also how many were applied.
    public static func apply(_ updates: [LabelUpdate], to options: [HUDOption]) -> ([HUDOption], Int) {
        var out = options, n = 0
        for u in updates {
            guard let i = out.firstIndex(where: { $0.key == u.key }) else { continue }
            if let e = u.expect, out[i].label != e { continue }
            let l = u.label.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !l.isEmpty else { continue }
            out[i].label = String(l.prefix(40)); n += 1
        }
        return (out, n)
    }
}
