import Foundation

/// W10: the HUD options sit at FIXED places around the pointer,
/// keyed to the modifier that picks them, so the position is learned by muscle memory, not read.
/// Clockwise from the left in keyboard order (bottom row fn ⌃ ⌥ ⌘; the user 15:45): fn left (9 o'clock),
/// ⌃ top (12), ⌥ right (3), ⌘ bottom (6). An absent option leaves its place empty. Two more fixed
/// places outside this map: the wish option at 4:30 (below-right) and, RESERVED for the upcoming
/// permanent "Send a vocal to…" option (W12), 7:30 (below-left) — kept free, not configurable here.
public enum SlotPosition: String, CaseIterable, Codable, Sendable {
    case left, top, right, bottom
}

/// settings.json `hud`: `{"slots": {"fn": "left", "ctrl": "top", "option": "right", "cmd": "bottom"},
/// "colors": {"ctrl": {"light": "#4F86E8", "dark": "#9CC3FF"}, ...}}`.
/// `slots`: the map must give the four option modifiers four different places; anything else (missing
/// key, unknown name, two modifiers on one place) gives the defaults as a whole.
/// `colors` (W11): one colour per option modifier (the colour = the key, stable across shows);
/// tolerant per entry (a missing or bad entry keeps that key's default; see `HUDColors`).
public struct HUDSettings: Codable, Equatable, Sendable {
    public static let defaultSlots: [TapModifier: SlotPosition] = [.fn: .left, .ctrl: .top, .option: .right, .cmd: .bottom]
    public var slots: [TapModifier: SlotPosition]
    public var colors: HUDColors
    /// W14 `long_press_ms`: holding an option's modifier at least this long = the Claude-assisted
    /// variant (default 600, clamped 400-3000: always longer than a tap, 350 ms).
    public var longPressMs: Int
    public static let defaultLongPressMs = 600
    public static func clampLongPress(_ v: Int) -> Int { min(max(v, 400), 3000) }
    /// W15 `dwell_ms`: during a ⌥⌘ glide, resting this long on a capsule asks for its Claude follow-ups
    /// (the sub-ring). Default 500, clamped 250-2000.
    public var dwellMs: Int
    public static let defaultDwellMs = 500
    public static func clampDwell(_ v: Int) -> Int { min(max(v, 250), 2000) }
    // W19: `auto_mode` = how `oi live` sends its automatic (gated) shows: "hint" (default: coloured
    // dots, ⌥⌘ develops them), "full" (the cross, as before W19) or "off" (nothing; ⌥⌘ still summons).
    // `hint_ttl_ms` = how long the dots stay (default 4000 since W20, was 2500; clamped 500-10000). Read by the python driver;
    // kept here so a menu toggle writes them back unchanged.
    public static let autoModes = ["hint", "full", "off"]
    public static let defaultAutoMode = "hint"
    public var autoMode: String
    public static let defaultHintTTLms = 4000      // W20: 2500 -> 4000 (the user: the dots were never seen)
    public static func clampHintTTL(_ v: Int) -> Int { min(max(v, 500), 10_000) }
    public var hintTTLms: Int
    // end W19
    /// W20 `hint_dot_size` (pt): the hint dots' diameter (default 10, was 6; clamped 4-24). Read by oi-hud.
    public static let defaultHintDotSize = 10
    public static func clampHintDotSize(_ v: Int) -> Int { min(max(v, 4), 24) }
    public var hintDotSize: Int
    /// W21 `fill_order`: which key gets the most likely option, then the next… (rank 1 first). The NUMBER of an
    /// option is its key (fn 1, ⌃ 2, ⌥ 3, ⌘ 4: `HotkeySet.defaults`), never its rank; fewer than 4 options leave
    /// the last keys of this order empty. Default ⌃ ⌥ ⌘ fn (rank 1 on control). Read by the python driver
    /// (`oi live`, which also keeps an option on its key while it is shown again in the same context); kept here
    /// so a menu save writes it back. Anything but a permutation of the four option modifiers gives the default.
    public static let defaultFillOrder: [TapModifier] = [.ctrl, .option, .cmd, .fn]
    public var fillOrder: [TapModifier]
    /// W26: `hud` keys this struct does not know, kept as read and written back unchanged.
    public var extra: [String: JSONValue] = [:]
    /// The option key numbers in fill order ([2, 3, 4, 1] by default): the modifier's option number in
    /// `HotkeySet.defaults` (the number is the key).
    public var fillKeys: [Int] { HUDSettings.fillKeys(fillOrder) }
    public static func fillKeys(_ order: [TapModifier]) -> [Int] {
        order.compactMap { m in (1...4).first { HotkeySet.defaults.option($0)?.tap == m } }
    }
    public static func validFillOrder(_ o: [TapModifier]) -> Bool { o.count == 4 && Set(o) == Set(TapModifier.optionTaps) }

    public init(slots: [TapModifier: SlotPosition] = HUDSettings.defaultSlots, colors: HUDColors = HUDColors(),
                longPressMs: Int = HUDSettings.defaultLongPressMs, dwellMs: Int = HUDSettings.defaultDwellMs,
                autoMode: String = HUDSettings.defaultAutoMode, hintTTLms: Int = HUDSettings.defaultHintTTLms,
                hintDotSize: Int = HUDSettings.defaultHintDotSize, fillOrder: [TapModifier] = HUDSettings.defaultFillOrder) {
        self.slots = HUDSettings.valid(slots) ? slots : HUDSettings.defaultSlots
        self.colors = colors
        self.longPressMs = HUDSettings.clampLongPress(longPressMs)
        self.dwellMs = HUDSettings.clampDwell(dwellMs)
        self.autoMode = HUDSettings.autoModes.contains(autoMode) ? autoMode : HUDSettings.defaultAutoMode   // W19
        self.hintTTLms = HUDSettings.clampHintTTL(hintTTLms)                                                  // W19
        self.hintDotSize = HUDSettings.clampHintDotSize(hintDotSize)                                          // W20
        self.fillOrder = HUDSettings.validFillOrder(fillOrder) ? fillOrder : HUDSettings.defaultFillOrder      // W21
    }

    public static func valid(_ m: [TapModifier: SlotPosition]) -> Bool {
        Set(m.keys) == Set(TapModifier.optionTaps) && Set(m.values).count == SlotPosition.allCases.count
    }

    enum CodingKeys: String, CodingKey, CaseIterable { case slots, colors, longPressMs = "long_press_ms", dwellMs = "dwell_ms"
        case autoMode = "auto_mode", hintTTLms = "hint_ttl_ms"   // W19
        case hintDotSize = "hint_dot_size"                       // W20
        case fillOrder = "fill_order" }                          // W21

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let raw = (try? c.decodeIfPresent([String: String].self, forKey: .slots)) ?? nil
        var m: [TapModifier: SlotPosition] = [:]
        for (k, v) in raw ?? [:] {
            guard let mod = TapModifier(name: k), let pos = SlotPosition(rawValue: v.lowercased()) else { m = [:]; break }
            m[mod] = pos
        }
        let colors = (try? c.decodeIfPresent(HUDColors.self, forKey: .colors)) ?? nil
        let lp = (try? c.decodeIfPresent(Double.self, forKey: .longPressMs)) ?? nil
        let dw = (try? c.decodeIfPresent(Double.self, forKey: .dwellMs)) ?? nil
        let am = (try? c.decodeIfPresent(String.self, forKey: .autoMode)) ?? nil      // W19
        let ht = (try? c.decodeIfPresent(Double.self, forKey: .hintTTLms)) ?? nil     // W19
        let ds = (try? c.decodeIfPresent(Double.self, forKey: .hintDotSize)) ?? nil   // W20
        let fo = ((try? c.decodeIfPresent([String].self, forKey: .fillOrder)) ?? nil).map { $0.compactMap(TapModifier.init(name:)) }   // W21
        self.init(slots: m, colors: colors ?? HUDColors(),
                  longPressMs: lp.map { $0.isFinite ? Int($0) : HUDSettings.defaultLongPressMs } ?? HUDSettings.defaultLongPressMs,
                  dwellMs: dw.map { $0.isFinite ? Int($0) : HUDSettings.defaultDwellMs } ?? HUDSettings.defaultDwellMs,
                  autoMode: am ?? HUDSettings.defaultAutoMode,
                  hintTTLms: ht.map { $0.isFinite ? Int($0) : HUDSettings.defaultHintTTLms } ?? HUDSettings.defaultHintTTLms,
                  hintDotSize: ds.map { $0.isFinite ? Int($0) : HUDSettings.defaultHintDotSize } ?? HUDSettings.defaultHintDotSize,
                  fillOrder: fo ?? HUDSettings.defaultFillOrder)
        extra = JSONValue.unknownKeys(decoder, known: Set(CodingKeys.allCases.map(\.rawValue)))   // W26
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(Dictionary(uniqueKeysWithValues: slots.map { ($0.key.rawValue, $0.value.rawValue) }), forKey: .slots)
        try c.encode(colors, forKey: .colors)
        try c.encode(longPressMs, forKey: .longPressMs)
        try c.encode(dwellMs, forKey: .dwellMs)
        try c.encode(autoMode, forKey: .autoMode)      // W19
        try c.encode(hintTTLms, forKey: .hintTTLms)    // W19
        try c.encode(hintDotSize, forKey: .hintDotSize)  // W20
        try c.encode(fillOrder.map(\.rawValue), forKey: .fillOrder)   // W21
        try JSONValue.encodeExtra(extra, to: encoder)   // W26
    }
}

/// An sRGB colour "#RRGGBB" in two variants: `light` for a light background (Light mode, deeper so it
/// reads on white), `dark` for a dark one (Dark mode and the always-dark HUD capsules, paler).
public struct HUDColor: Codable, Equatable, Sendable {
    public var light: String
    public var dark: String

    public init(light: String, dark: String) { self.light = light; self.dark = dark }

    /// "#RRGGBB" (or "RRGGBB") → (r, g, b) in 0...1; nil when malformed.
    public static func rgb(_ hex: String) -> (r: Double, g: Double, b: Double)? {
        var h = hex.trimmingCharacters(in: .whitespaces)
        if h.hasPrefix("#") { h.removeFirst() }
        guard h.count == 6, let v = UInt32(h, radix: 16) else { return nil }
        return (Double((v >> 16) & 0xFF) / 255, Double((v >> 8) & 0xFF) / 255, Double(v & 0xFF) / 255)
    }

    /// Canonical "#RRGGBB" (upper case); nil when malformed.
    public static func normalised(_ hex: String) -> String? {
        guard let c = rgb(hex) else { return nil }
        return String(format: "#%02X%02X%02X", Int((c.r * 255).rounded()), Int((c.g * 255).rounded()), Int((c.b * 255).rounded()))
    }

    /// Accepts `{"light": "#..", "dark": "#.."}` (a missing or bad variant takes the other one) or a bare
    /// "#RRGGBB" for both. Throws when nothing usable is there (the caller keeps the default).
    public init(from decoder: Decoder) throws {
        if let single = try? decoder.singleValueContainer(), let str = try? single.decode(String.self) {
            guard let n = HUDColor.normalised(str) else {
                throw DecodingError.dataCorruptedError(in: single, debugDescription: "bad colour \(str)")
            }
            self.init(light: n, dark: n); return
        }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let l = (try? c.decodeIfPresent(String.self, forKey: .light)).flatMap { $0 }.flatMap(HUDColor.normalised)
        let d = (try? c.decodeIfPresent(String.self, forKey: .dark)).flatMap { $0 }.flatMap(HUDColor.normalised)
        guard let any = l ?? d else {
            throw DecodingError.dataCorruptedError(forKey: .light, in: c, debugDescription: "no usable colour")
        }
        self.init(light: l ?? any, dark: d ?? any)
    }
}

/// settings.json `hud.colors` (W11, the user 2026-10-20 13:39Z: "a colour per key"): option modifier →
/// colour. Defaults are pastels: ⌃ blue, ⌥ yellow, ⌘ green, fn pink. Tolerant: each entry is read on its
/// own; an unknown key name is ignored, a bad value keeps that key's default. Always written in full.
public struct HUDColors: Codable, Equatable, Sendable {
    public static let defaults: [TapModifier: HUDColor] = [
        .ctrl: HUDColor(light: "#4F86E8", dark: "#9CC3FF"),     // pastel blue
        .option: HUDColor(light: "#D9A400", dark: "#FFE08A"),   // pastel yellow
        .cmd: HUDColor(light: "#3FA95A", dark: "#A6E3A1"),      // pastel green
        .fn: HUDColor(light: "#E06A9C", dark: "#F5A9C8"),       // pastel pink
    ]
    public var byModifier: [TapModifier: HUDColor]

    public init(_ m: [TapModifier: HUDColor] = [:]) {
        var out = HUDColors.defaults
        for (k, v) in m where TapModifier.optionTaps.contains(k) { out[k] = v }
        byModifier = out
    }

    public subscript(_ m: TapModifier) -> HUDColor { byModifier[m] ?? HUDColors.defaults[.ctrl]! }

    private struct AnyKey: CodingKey {
        var stringValue: String; var intValue: Int? { nil }
        init(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        var m: [TapModifier: HUDColor] = [:]
        for k in c.allKeys {
            guard let mod = TapModifier(name: k.stringValue), let col = try? c.decode(HUDColor.self, forKey: k) else { continue }
            m[mod] = col
        }
        self.init(m)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: AnyKey.self)
        for m in TapModifier.optionTaps { try c.encode(self[m], forKey: AnyKey(stringValue: m.rawValue)) }
    }
}
