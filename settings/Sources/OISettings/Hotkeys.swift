import Foundation

/// A bare modifier TAP (W9): the modifier goes down and up within 350 ms with no other key,
/// click, scroll or modifier in between. For the HUD options 1-4 (only while the HUD is visible: oi-hud
/// listens with a passive event tap, created on show and removed on hide) and, W10, for the
/// SUMMON key (a permanent passive tap). W11: the summon key is a modifier
/// SET tap, default ⌥+⌘ pressed together (both down, then released, no other key, ≤ 500 ms from the
/// first down to the last up). ⇧ is a summon-only single tap (`optionTaps` excludes it).
public enum TapModifier: String, CaseIterable, Codable, Sendable {
    case ctrl, option, cmd, fn, shift

    /// The taps offered for options 1-4 (⇧ is reserved for the summon key).
    public static let optionTaps: [TapModifier] = [.ctrl, .option, .cmd, .fn]

    /// Accepts the canonical names plus the usual aliases ("control", "alt", "command", "globe", ...).
    public init?(name: String) {
        switch name.trimmingCharacters(in: .whitespaces).lowercased() {
        case "ctrl", "control", "⌃": self = .ctrl
        case "option", "opt", "alt", "⌥": self = .option
        case "cmd", "command", "⌘": self = .cmd
        case "fn", "globe", "🌐": self = .fn
        case "shift", "maj", "⇧": self = .shift
        default: return nil
        }
    }

    public var symbol: String {
        switch self { case .ctrl: "⌃"; case .option: "⌥"; case .cmd: "⌘"; case .fn: "fn"; case .shift: "⇧" }
    }
}

/// One HUD hotkey, stored in settings.json either as a key combination (a CHORD)
/// `{"modifiers": ["option"], "key": "1"}` or, for options 1-4 and summon only, as a bare modifier tap
/// `{"tap": "ctrl"}` (`ctrl|option|cmd|fn`, + `shift` for summon; the string form `"tap:ctrl"` is
/// accepted on read). W11: summon may be a modifier SET tap `{"tap": ["option", "cmd"]}`
/// (2-3 modifiers pressed together; the string form `"tap:option+cmd"` is accepted on read).
///
/// - `modifiers`: any of `command`, `option`, `control`, `shift` (order-free, stored sorted).
/// - `key`: a name from `Hotkey.keyCodes` ("a"-"z", "0"-"9", "`", "-", "=", "[", "]", ";", "'",
///   ",", ".", "/", "\\", "Esc", "Space", "Return", "Tab", "Delete", "F1"-"F12", arrows).
///   Names map to macOS *virtual key codes* (kVK_ANSI_*), which are PHYSICAL key positions on
///   the US layout: "1" is the key left of "2" on every layout (AZERTY included), as before.
///   An unlisted key can be given as a raw virtual key code: `"#50"`.
public struct Hotkey: Codable, Equatable, Hashable, Sendable {
    public var modifiers: [String]
    public var key: String
    /// Non-empty = a bare modifier tap (then `modifiers` is empty and `key` is ""): one modifier (W9),
    /// or a SET pressed together (W11, summon only). Kept in `TapModifier.allCases` order.
    public private(set) var tapSet: [TapModifier]

    public init(_ modifiers: [String], _ key: String) {
        self.modifiers = Hotkey.normalise(modifiers); self.key = key; self.tapSet = []
    }

    /// A bare modifier tap (W9).
    public static func tap(_ m: TapModifier) -> Hotkey { tapSet([m]) }

    /// A modifier set tap (W11): every modifier of the set down together, then all released.
    public static func tapSet(_ ms: [TapModifier]) -> Hotkey {
        var h = Hotkey([], ""); h.tapSet = TapModifier.allCases.filter { Set(ms).contains($0) }; return h
    }

    /// The single tapped modifier (nil for a chord and for a set tap).
    public var tap: TapModifier? { tapSet.count == 1 ? tapSet[0] : nil }
    public var isTap: Bool { !tapSet.isEmpty }
    public var isSetTap: Bool { tapSet.count > 1 }

    /// "option+cmd": the stable name of a tap (collision signature, `tap:` text form).
    public var tapName: String { tapSet.map(\.rawValue).joined(separator: "+") }

    /// Parse "ctrl", "option+cmd", "⌥⌘" into a tap set (nil when a name is unknown or it is empty).
    public static func tapModifiers(_ text: String) -> [TapModifier]? {
        let t = text.trimmingCharacters(in: .whitespaces)
        var parts = t.split(whereSeparator: { $0 == "+" || $0 == "," || $0 == " " }).map(String.init)
        if parts.count == 1, TapModifier(name: parts[0]) == nil {   // "⌥⌘": symbols without separator
            parts = t.map(String.init)
        }
        let ms = parts.compactMap { TapModifier(name: $0) }
        guard !ms.isEmpty, ms.count == parts.count else { return nil }
        return ms
    }

    enum CodingKeys: String, CodingKey { case modifiers, key, tap }

    public init(from decoder: Decoder) throws {
        if let single = try? decoder.singleValueContainer(), let str = try? single.decode(String.self) {
            guard str.lowercased().hasPrefix("tap:"), let ms = Hotkey.tapModifiers(String(str.dropFirst(4))) else {
                throw DecodingError.dataCorruptedError(in: single, debugDescription: "bad hotkey \(str)")
            }
            self = .tapSet(ms); return
        }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if c.contains(.tap) {
            // "ctrl" (W9) or ["option", "cmd"] (W11 set tap)
            let names: [String]
            if let list = try? c.decode([String].self, forKey: .tap) { names = list }
            else { names = [try c.decode(String.self, forKey: .tap)] }
            let ms = names.compactMap { TapModifier(name: $0) }
            guard !ms.isEmpty, ms.count == names.count else {
                throw DecodingError.dataCorruptedError(forKey: .tap, in: c, debugDescription: "unknown tap modifier \(names)")
            }
            self = .tapSet(ms); return
        }
        self.init(try c.decode([String].self, forKey: .modifiers), try c.decode(String.self, forKey: .key))
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        if let tap { try c.encode(tap.rawValue, forKey: .tap); return }
        if isSetTap { try c.encode(tapSet.map(\.rawValue), forKey: .tap); return }
        try c.encode(modifiers, forKey: .modifiers)
        try c.encode(key, forKey: .key)
    }

    public static let modifierOrder = ["control", "option", "shift", "command"]
    public static let modifierSymbols = ["control": "⌃", "option": "⌥", "shift": "⇧", "command": "⌘"]
    /// Carbon modifier masks (cmdKey, shiftKey, optionKey, controlKey).
    public static let carbonMasks: [String: UInt32] = ["command": 0x0100, "shift": 0x0200, "option": 0x0800, "control": 0x1000]

    static func normalise(_ mods: [String]) -> [String] {
        let set = Set(mods.map { $0.lowercased() })
        return modifierOrder.filter { set.contains($0) }
    }

    /// kVK_* virtual key codes (Carbon HIToolbox Events.h), ANSI positions.
    public static let keyCodes: [String: Int] = [
        "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06, "x": 0x07,
        "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10,
        "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "=": 0x18,
        "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C, "0": 0x1D, "]": 0x1E, "o": 0x1F, "u": 0x20,
        "[": 0x21, "i": 0x22, "p": 0x23, "l": 0x25, "j": 0x26, "'": 0x27, "k": 0x28, ";": 0x29,
        "\\": 0x2A, ",": 0x2B, "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F, "`": 0x32,
        "Return": 0x24, "Tab": 0x30, "Space": 0x31, "Delete": 0x33, "Esc": 0x35,
        "F1": 0x7A, "F2": 0x78, "F3": 0x63, "F4": 0x76, "F5": 0x60, "F6": 0x61, "F7": 0x62, "F8": 0x64,
        "F9": 0x65, "F10": 0x6D, "F11": 0x67, "F12": 0x6F,
        "Left": 0x7B, "Right": 0x7C, "Down": 0x7D, "Up": 0x7E,
    ]
    static let namesByCode: [Int: String] = Dictionary(keyCodes.map { ($0.value, $0.key) }, uniquingKeysWith: { a, _ in a })

    /// Keys allowed without a modifier (they never type text).
    static let bareAllowed: Set<String> = ["Esc", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12"]

    /// Virtual key code, or nil when the key name is unknown.
    public var keyCode: Int? {
        if tap != nil { return nil }
        if let c = Hotkey.keyCodes[key] ?? Hotkey.keyCodes[key.lowercased()] { return c }
        if key.hasPrefix("#"), let n = Int(key.dropFirst()), (0...127).contains(n) { return n }
        return nil
    }

    public var carbonModifiers: UInt32 { modifiers.reduce(0) { $0 | (Hotkey.carbonMasks[$1] ?? 0) } }

    /// "⌥1", "⌃⇧K", "Esc"; a tap shows its modifier alone: "⌃", "⌥", "⌘", "fn"; a set tap "⌥⌘".
    public var display: String {
        if isTap { return tapSet.map(\.symbol).joined() }
        let m = modifiers.compactMap { Hotkey.modifierSymbols[$0] }.joined()
        let k = key.count == 1 ? key.uppercased() : key
        return m + k
    }

    /// Build from a key press (recorder field): virtual key code + modifier names.
    public static func from(keyCode: Int, modifiers: [String]) -> Hotkey {
        Hotkey(modifiers, namesByCode[keyCode] ?? "#\(keyCode)")
    }

    /// Parse the display form typed in a text field: "⌥1", "opt+1", "ctrl+shift+k", "Esc", "tap:ctrl",
    /// "tap:option+cmd".
    public static func parse(_ text: String) -> Hotkey? {
        var s = text.trimmingCharacters(in: .whitespaces)
        if s.lowercased().hasPrefix("tap:") { return tapModifiers(String(s.dropFirst(4))).map { .tapSet($0) } }
        var mods: [String] = []
        let words: [(String, String)] = [("command+", "command"), ("cmd+", "command"), ("option+", "option"),
                                         ("opt+", "option"), ("alt+", "option"), ("control+", "control"),
                                         ("ctrl+", "control"), ("shift+", "shift")]
        var progress = true
        while progress, !s.isEmpty {
            progress = false
            if let (sym, name) = modifierSymbols.first(where: { s.hasPrefix($0.value) }).map({ ($0.value, $0.key) }) {
                mods.append(name); s.removeFirst(sym.count); progress = true; continue
            }
            if let (w, name) = words.first(where: { s.lowercased().hasPrefix($0.0) }) {
                mods.append(name); s.removeFirst(w.count); progress = true
            }
        }
        guard !s.isEmpty else { return nil }
        let name = keyCodes.keys.first { $0.lowercased() == s.lowercased() } ?? s
        let hk = Hotkey(mods, name.count == 1 ? name.lowercased() : name)
        return hk.keyCode == nil ? nil : hk
    }
}

/// The HUD hotkeys (settings.json `hotkeys`). `wish` opens the "tell me what you wanted" panel
/// (W6): oi-hud keeps it registered for as long as the daemon runs, even with `hud_enabled` false.
/// `wish_visible` (W11; default a bare ⇧ tap): the same panel by ONE key while
/// the HUD is visible (the wish option's badge); a single-modifier tap only, armed only while visible
/// (the same listen-only tap as the option taps). A file without it gets the default.
/// `summon` (W10; W11 default: the ⌥+⌘ SET tap) opens the HUD with the current best
/// options at any time; oi-hud keeps it armed while the daemon runs, and ignores it when `hud_enabled`
/// is false or `paused` is true. A file without `summon` gets the default. `vocal` (W12; default
/// the ⌃+⌥ SET tap) opens the "Send a vocal to…" agent picker while the HUD is visible; a SET tap or a
/// chord only (a single-modifier tap is read as the default). A file without it gets the default. `"v": 3` (W11): a file
/// below v3 whose summon is the W10 default ⇧ tap (oi-menubar wrote it out on every save) reads as the
/// new ⌥+⌘ default.
/// W21: the option NUMBER is the KEY, never the rank:
/// option 1 = fn, 2 = ⌃, 3 = ⌥, 4 = ⌘ (keyboard order, bottom row). A file below v4 whose options are
/// exactly the W9 defaults (1 ⌃, 2 ⌥, 3 ⌘, 4 fn) reads as the W21 defaults (migrated once: the next save
/// writes v4). Which prediction goes on which key is the driver's choice (`hud.fill_order`, see HUDSettings).
/// W9: options 1-4 default to bare modifier taps ctrl / option / cmd / fn. The file carries
/// `"v": 2`; a file without it whose options are exactly the W5 defaults ⌥1-⌥4 (oi-menubar wrote
/// them out on every save) is read as the new tap defaults, so the upgrade needs no manual edit.
public struct HotkeySet: Codable, Equatable, Sendable {
    public var option1: Hotkey
    public var option2: Hotkey
    public var option3: Hotkey
    public var option4: Hotkey
    public var dismiss: Hotkey
    public var wish: Hotkey
    public var summon: Hotkey
    public var wishVisible: Hotkey
    public var vocal: Hotkey

    public static let defaults = HotkeySet(
        option1: .tap(.fn), option2: .tap(.ctrl), option3: .tap(.option), option4: .tap(.cmd),   // W21
        dismiss: Hotkey([], "Esc"), wish: Hotkey(["option"], "`"), summon: HotkeySet.defaultSummon,
        wishVisible: HotkeySet.defaultWishVisible, vocal: HotkeySet.defaultVocal)

    /// W12: the permanent "Send a vocal to…" option (7:30). ⌃ and ⌥ pressed together, then
    /// released (every single modifier is taken by options 1-4 / ⇧ wish; caps lock is not used: see
    /// hud/README.md "Send a vocal to…"). Armed while the HUD is visible and during the vocal flow
    /// (picker: next page; recording: send). A chord works too.
    public static let defaultVocal = Hotkey.tapSet([.ctrl, .option])

    /// W11: ⇧ tapped while the HUD is up opens the wish panel (⇧ is free since summon is ⌥+⌘).
    public static let defaultWishVisible = Hotkey.tap(.shift)

    /// W11: ⌥+⌘ pressed together, then released. A single ⌥ or ⌘ tap stays
    /// an option selector (3, 4 since W21) while the HUD is visible; ⇧ is free again.
    public static let defaultSummon = Hotkey.tapSet([.option, .cmd])
    /// W9-W20 option taps (option 1-4 = ⌃ ⌥ ⌘ fn), read as the W21 defaults from a file below v4.
    public static let w9Options: [Hotkey] = [.tap(.ctrl), .tap(.option), .tap(.cmd), .tap(.fn)]
    /// The W10 default, read as `defaultSummon` from a file below v3.
    public static let w10Summon = Hotkey.tap(.shift)

    /// The single-modifier taps the Hotkeys window offers for an action (empty = chord only).
    public static func tapChoices(_ action: String) -> [TapModifier] {
        if action.hasPrefix("option_") { return TapModifier.optionTaps }
        if action == "summon" || action == "wish_visible" { return [.shift] + TapModifier.optionTaps }
        return []
    }

    /// The tap segments the Hotkeys window shows for an action (W11): options 1-4 the four single taps;
    /// summon ⌥⌘ (the default set) and ⇧, plus a "record tap" segment that captures any modifier set
    /// pressed together (`capturesTapSet`). Empty = chord only.
    public static func tapPresets(_ action: String) -> [Hotkey] {
        if action.hasPrefix("option_") { return TapModifier.optionTaps.map { .tap($0) } }
        if action == "summon" { return [defaultSummon, .tap(.shift)] }
        if action == "vocal" { return [defaultVocal] }
        if action == "wish_visible" { return ([.shift] + TapModifier.optionTaps).map { .tap($0) } }
        return []
    }

    /// Whether the action's row offers a chord (wish_visible is a tap only).
    public static func allowsChord(_ action: String) -> Bool { action != "wish_visible" }

    /// Whether the action's row has a "record tap" segment (any single modifier or a 2-3 modifier set).
    public static func capturesTapSet(_ action: String) -> Bool { action == "summon" || action == "vocal" }

    /// The actions that take a modifier SET tap (W11 summon, W12 vocal).
    public static let setTapActions: Set<String> = ["summon", "vocal"]

    /// Whether `h` (a tap) is allowed on `action`: options 1-4 take one of ⌃ ⌥ ⌘ fn; summon takes any
    /// single tap or a SET of 2-3 modifiers (W11); vocal (W12) a SET of 2-3 modifiers only; dismiss and
    /// wish take no tap.
    public static func allowsTap(_ action: String, _ h: Hotkey) -> Bool {
        if h.isSetTap { return setTapActions.contains(action) && h.tapSet.count <= 3 }
        guard let t = h.tap else { return false }
        return tapChoices(action).contains(t)
    }

    /// The chord a tap row switches to when "chord" is picked and nothing was recorded yet.
    public static func chordFallback(_ action: String) -> Hotkey {
        if action == "summon" { return Hotkey(["option", "shift"], "Space") }
        if action == "vocal" { return Hotkey(["control", "option"], "v") }
        return legacyChord(Int(action.dropFirst("option_".count)) ?? 1)
    }

    /// The W5-W8 option defaults (⌥1-⌥4); also the Carbon fallback for a tap slot when the
    /// event tap cannot be created (Input Monitoring missing).
    public static func legacyChord(_ n: Int) -> Hotkey { Hotkey(["option"], "\(n)") }

    /// Schema version written in `hotkeys.v`.
    public static let version = 4   // W21: option numbers = keys (fn 1, ⌃ 2, ⌥ 3, ⌘ 4)

    public init(option1: Hotkey, option2: Hotkey, option3: Hotkey, option4: Hotkey, dismiss: Hotkey, wish: Hotkey,
                summon: Hotkey = HotkeySet.defaultSummon, wishVisible: Hotkey = HotkeySet.defaultWishVisible,
                vocal: Hotkey = HotkeySet.defaultVocal) {
        self.option1 = option1; self.option2 = option2; self.option3 = option3
        self.option4 = option4; self.dismiss = dismiss; self.wish = wish; self.summon = summon
        self.wishVisible = wishVisible; self.vocal = vocal
    }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case option1 = "option_1", option2 = "option_2", option3 = "option_3", option4 = "option_4", dismiss, wish, summon
        case wishVisible = "wish_visible"
        case vocal
        case v
    }

    /// Settings keys in display order, with a human label.
    public static let actions: [(key: String, label: String)] = [
        ("option_1", "Option 1"), ("option_2", "Option 2"), ("option_3", "Option 3"),
        ("option_4", "Option 4"), ("dismiss", "Dismiss HUD"), ("wish", "Wish (tell me what you wanted)"),
        ("wish_visible", "Wish while the HUD is up"), ("summon", "Summon HUD (show the options now)"),
        ("vocal", "Send a vocal to… (HUD up)"),
    ]

    // W19: the Hotkeys… window names the
    // options with their default modifier spelled out, "Option 2 (⌃ control)"; the other rows as `actions`.
    public static let optionModifierNames: [Int: String] = [1: "fn", 2: "⌃ control", 3: "⌥ option", 4: "⌘ command"]   // W21
    public static func windowLabel(_ action: String) -> String {
        let label = actions.first { $0.key == action }?.label ?? action
        guard action.hasPrefix("option_"), let n = Int(action.dropFirst(7)), let m = optionModifierNames[n] else { return label }
        return "\(label) (\(m))"
    }
    // end W19

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = HotkeySet.defaults
        func k(_ key: CodingKeys, _ def: Hotkey) -> Hotkey {
            guard let h = try? c.decodeIfPresent(Hotkey.self, forKey: key) else { return def }
            if h.isTap { return HotkeySet.allowsTap(key.rawValue, h) || key == .dismiss || key == .wish ? h : def }
            return h.keyCode != nil ? Hotkey(h.modifiers, h.key) : def
        }
        option1 = k(.option1, d.option1); option2 = k(.option2, d.option2); option3 = k(.option3, d.option3)
        option4 = k(.option4, d.option4); dismiss = k(.dismiss, d.dismiss); wish = k(.wish, d.wish)
        summon = k(.summon, d.summon)
        wishVisible = k(.wishVisible, d.wishVisible)
        vocal = k(.vocal, d.vocal)
        if !wishVisible.isTap { wishVisible = d.wishVisible }                  // a tap only
        let v = (try? c.decodeIfPresent(Int.self, forKey: .v)) ?? nil
        if v == nil, (1...4).allSatisfy({ option($0) == HotkeySet.legacyChord($0) }) {
            option1 = d.option1; option2 = d.option2; option3 = d.option3; option4 = d.option4
        }
        if (v ?? 0) < 4, (1...4).allSatisfy({ option($0) == HotkeySet.w9Options[$0 - 1] }) {   // W21: renumber once
            option1 = d.option1; option2 = d.option2; option3 = d.option3; option4 = d.option4
        }
        if (v ?? 0) < 3, summon == HotkeySet.w10Summon { summon = d.summon }   // W10 ⇧ default -> ⌥+⌘
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(option1, forKey: .option1); try c.encode(option2, forKey: .option2)
        try c.encode(option3, forKey: .option3); try c.encode(option4, forKey: .option4)
        try c.encode(dismiss, forKey: .dismiss); try c.encode(wish, forKey: .wish)
        try c.encode(summon, forKey: .summon); try c.encode(wishVisible, forKey: .wishVisible)
        try c.encode(vocal, forKey: .vocal)
        try c.encode(HotkeySet.version, forKey: .v)
    }

    public subscript(action: String) -> Hotkey? {
        get {
            switch action {
            case "option_1": return option1
            case "option_2": return option2
            case "option_3": return option3
            case "option_4": return option4
            case "dismiss": return dismiss
            case "wish": return wish
            case "summon": return summon
            case "wish_visible": return wishVisible
            case "vocal": return vocal
            default: return nil
            }
        }
        set {
            guard let v = newValue else { return }
            switch action {
            case "option_1": option1 = v
            case "option_2": option2 = v
            case "option_3": option3 = v
            case "option_4": option4 = v
            case "dismiss": dismiss = v
            case "wish": wish = v
            case "summon": summon = v
            case "wish_visible": wishVisible = v
            case "vocal": vocal = v
            default: break
            }
        }
    }

    /// Option slot 1-4 → its hotkey.
    public func option(_ n: Int) -> Hotkey? { (1...4).contains(n) ? self["option_\(n)"] : nil }

    /// Problems that make the set unusable: unknown key, a typing key without modifier, a tap on
    /// anything but options 1-4 / summon (⇧ tap: summon only), or two actions on the same physical
    /// combo / the same tap (wish and summon included). The summon tap (armed only while the HUD is
    /// hidden in effect: ignored when visible) and `wish_visible` (armed only while visible) never meet,
    /// so they may share a tap. Empty = valid.
    public func problems() -> [String] {
        var out: [String] = []
        var seen: [String: String] = [:]
        for (action, label) in HotkeySet.actions {
            guard let h = self[action] else { continue }
            if h.isTap {
                let choices = HotkeySet.tapChoices(action)
                if h.isSetTap, HotkeySet.setTapActions.contains(action) {
                    if h.tapSet.count > 3 { out.append("\(label): a set tap takes 2 or 3 modifiers") }
                } else if action == "vocal" {
                    out.append("\(label): a set of 2-3 modifiers pressed together, or a combination")
                } else if choices.isEmpty {
                    out.append("\(label): a modifier tap only works for options 1-4 and summon (use a combination)")
                } else if h.isSetTap {
                    out.append("\(label): a \(h.display) set tap only works for summon and vocal (options take one modifier)")
                } else if let tap = h.tap, !choices.contains(tap) {
                    out.append("\(label): the \(tap.symbol) tap is reserved for summon and the wish option")
                }
                let sig = "tap/\(h.tapName)"
                let pair: Set<String> = ["summon", "wish_visible"]
                if let other = seen[sig] {
                    let otherAction = HotkeySet.actions.first { $0.label == other }?.key ?? ""
                    if !(pair.contains(action) && pair.contains(otherAction)) {
                        out.append("\(label) collides with \(other) (\(h.display) tap)")
                    }
                } else { seen[sig] = label }
                continue
            }
            if action == "wish_visible" { out.append("\(label): a single modifier tap only"); continue }
            guard let code = h.keyCode else { out.append("\(label): unknown key \"\(h.key)\""); continue }
            if h.modifiers.isEmpty && !Hotkey.bareAllowed.contains(h.key) {
                out.append("\(label): \(h.display) needs a modifier (it would swallow typing)")
            }
            let sig = "\(h.carbonModifiers)/\(code)"
            if let other = seen[sig] { out.append("\(label) collides with \(other) (\(h.display))") }
            else { seen[sig] = label }
        }
        return out
    }
}
