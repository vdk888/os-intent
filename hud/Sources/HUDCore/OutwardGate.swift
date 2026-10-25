import Foundation
import OISettings

/// The outward gate: every action that leaves the Mac (a message / vocal / screenshot to an agent,
/// a page button such as Squash & merge) waits for this panel. `oi.gate` sends on hud.sock
///   `{"cmd":"confirm_outward","id":"…","cls":"vocal|shot|page|reply","title":"Envoyer ce message à Orion ?",
///     "preview":["…"],"ttl_ms":20000,"lang":"fr","action_key":"1-4|vocal","target":"button:squash-and-merge"}`
/// and gets ONE `{"event":"outward","id":"…","decision":"validated"|"cancelled","reason":"…"}` on the same
/// connection. Validate = Return / keypad Enter, a tap of the action key, or a click on ✓. Cancel = Esc, ✗, a click
/// outside, the timeout, or a newer request (replaced). Nothing is ever validated by default.
///   `{"cmd":"debug_outward","decision":"validate"|"cancel"}` (tests, demo): answer the open panel.
/// `"offer_shot": true`: the panel also shows "Joindre une capture de l'écran", OFF by
/// default; C or a click toggles it, and a validate then answers `"attach_shot": true, "display": n` (1-based, the
/// display under the pointer). oi.vocal takes the capture after the panel is gone; oi-hud never captures.
public struct OutwardConfirmRequest: Equatable, Sendable {
    public var id: String
    public var cls: String
    public var title: String
    public var preview: [String]
    public var ttlMs: Int
    public var lang: String
    public var actionKey: String?
    public var target: String?
    public var offerShot: Bool

    public static let maxPreviewLines = 3
    public static let maxLine = 120
    public static let maxTitle = 90
    public static let ttlRange = 1000...60_000
    public static let defaultTTLms = 20_000

    public init(id: String, cls: String, title: String, preview: [String] = [], ttlMs: Int = defaultTTLms,
                lang: String = "fr", actionKey: String? = nil, target: String? = nil, offerShot: Bool = false) {
        self.id = String(id.prefix(40)); self.cls = cls
        self.title = String(title.prefix(Self.maxTitle))
        self.preview = preview.prefix(Self.maxPreviewLines).map { String($0.prefix(Self.maxLine)) }
        self.ttlMs = min(max(ttlMs, Self.ttlRange.lowerBound), Self.ttlRange.upperBound)
        self.lang = lang == "en" ? "en" : "fr"
        self.actionKey = actionKey; self.target = target
        self.offerShot = offerShot && cls == "vocal"
    }

    /// The screenshot toggle's label (with its key).
    public var shotLabel: String {
        lang == "en" ? "Attach a screenshot (C)" : "Joindre une capture de l'écran (C)"
    }

    /// The bottom line of the panel.
    public var hint: String {
        lang == "en" ? "↩ Validate · Esc Cancel · cancels by itself in \(ttlMs / 1000) s"
                     : "↩ Valider · Esc Annuler · s'annule seul dans \(ttlMs / 1000) s"
    }

    /// The modifier set whose clean tap validates (the option's key 1-4, or the vocal key); nil = Return only.
    public func actionTaps(_ hotkeys: HotkeySet) -> Set<TapModifier>? {
        let set = hotkeys.problems().isEmpty ? hotkeys : HotkeySet.defaults
        guard let k = actionKey else { return nil }
        let h: Hotkey?
        if k == "vocal" { h = set.vocal } else if let n = Int(k) { h = set.option(n) } else { h = nil }
        guard let h, h.isTap else { return nil }
        return Set(h.tapSet)
    }
}

/// The panel's decision logic, pure: inputs within `armDelay` of opening are ignored (a Return or key still
/// bouncing from the previous panel never validates), the first decision wins, the timeout cancels.
public struct OutwardDecider: Equatable, Sendable {
    public static let armDelay: TimeInterval = 0.3
    public enum Input: Equatable, Sendable { case validate(String), cancel(String), timeout }
    public private(set) var openedAt: TimeInterval?
    public private(set) var decided: OutwardDecision?
    /// The screenshot toggle: offered by the request, OFF at every open, carried by a validate only.
    public private(set) var shotOffered = false
    public private(set) var attachShot = false

    public init() {}

    public mutating func open(at t: TimeInterval, offerShot: Bool = false) {
        openedAt = t; decided = nil; shotOffered = offerShot; attachShot = false
    }

    /// C / a click on the toggle: flips it while the panel is open and undecided; returns the new state.
    @discardableResult
    public mutating func toggleShot() -> Bool {
        guard openedAt != nil, decided == nil, shotOffered else { return false }
        attachShot.toggle()
        return attachShot
    }

    /// The decision this input makes now (once), else nil.
    public mutating func input(_ i: Input, at t: TimeInterval) -> OutwardDecision? {
        guard let o = openedAt, decided == nil else { return nil }
        let d: OutwardDecision
        switch i {
        case .validate(let via):
            guard t - o >= Self.armDelay else { return nil }
            d = OutwardDecision(validated: true, reason: via, attachShot: shotOffered && attachShot)
        case .cancel(let why): d = OutwardDecision(validated: false, reason: why)
        case .timeout: d = OutwardDecision(validated: false, reason: "timeout")
        }
        decided = d
        return d
    }
}

public struct OutwardDecision: Equatable, Sendable {
    public var validated: Bool
    public var reason: String
    /// A validated vocal with the screenshot toggle on; `display` (1-based) is filled by the panel at that moment.
    public var attachShot: Bool
    public var display: Int?
    public init(validated: Bool, reason: String, attachShot: Bool = false, display: Int? = nil) {
        self.validated = validated; self.reason = reason
        self.attachShot = validated && attachShot; self.display = self.attachShot ? display : nil
    }
}

private struct RawOutward: Decodable {
    let id: String?
    let cls: String?
    let title: String?
    let preview: [String]?
    let ttl_ms: Int?
    let lang: String?
    let action_key: String?
    let target: String?
    let decision: String?
    let offer_shot: Bool?
}

/// Decoding of `confirm_outward` / `debug_outward` (called by `decodeCommand`).
public func decodeOutwardCommand(_ cmd: String, _ line: Data) throws -> HUDCommand {
    guard let raw = try? JSONDecoder().decode(RawOutward.self, from: line) else { throw CommandError.badJSON }
    switch cmd {
    case "confirm_outward":
        guard let id = raw.id, !id.isEmpty, let cls = raw.cls, ["vocal", "shot", "page", "reply"].contains(cls),
              let title = raw.title?.trimmingCharacters(in: .whitespacesAndNewlines), !title.isEmpty
        else { throw CommandError.badJSON }
        return .confirmOutward(OutwardConfirmRequest(
            id: id, cls: cls, title: title, preview: raw.preview ?? [],
            ttlMs: raw.ttl_ms ?? OutwardConfirmRequest.defaultTTLms, lang: raw.lang ?? "fr",
            actionKey: raw.action_key, target: raw.target, offerShot: raw.offer_shot == true))
    case "debug_outward":
        return .debugOutward(validate: raw.decision == "validate")
    default:
        throw CommandError.unknownCommand(cmd)
    }
}
