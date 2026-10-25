// ⌘-chord formatting. Pure: takes modifier flags + a key code (+ the current
// layout's base character when the caller has one) and returns "⌘⇧4"-style text.
// Returns nil whenever ⌘ is absent: plain typing is never turned into a chord.

public struct ChordModifiers: OptionSet, Sendable, Hashable {
    public let rawValue: UInt8
    public init(rawValue: UInt8) { self.rawValue = rawValue }
    public static let command = ChordModifiers(rawValue: 1 << 0)
    public static let shift = ChordModifiers(rawValue: 1 << 1)
    public static let option = ChordModifiers(rawValue: 1 << 2)
    public static let control = ChordModifiers(rawValue: 1 << 3)

    /// Build from CGEventFlags raw bits (same bit values as NX_* masks).
    public init(cgEventFlags raw: UInt64) {
        var m: ChordModifiers = []
        if raw & 0x0010_0000 != 0 { m.insert(.command) }   // maskCommand
        if raw & 0x0002_0000 != 0 { m.insert(.shift) }     // maskShift
        if raw & 0x0008_0000 != 0 { m.insert(.option) }    // maskAlternate
        if raw & 0x0004_0000 != 0 { m.insert(.control) }   // maskControl
        self = m
    }
}

public enum Chord {
    /// Symbol order follows the token contract examples (`key:⌘C`, `key:⌘⇧4`): ⌘ ⇧ ⌥ ⌃.
    public static func format(modifiers: ChordModifiers, keyName: String) -> String? {
        guard modifiers.contains(.command), !keyName.isEmpty else { return nil }
        var s = "⌘"
        if modifiers.contains(.shift) { s += "⇧" }
        if modifiers.contains(.option) { s += "⌥" }
        if modifiers.contains(.control) { s += "⌃" }
        return s + keyName
    }

    /// Key name for a virtual key code. Order: special keys (F-keys, arrows,
    /// return…) → top digit row (always digits, so AZERTY gives ⌘⇧4 not ⌘⇧') →
    /// the current layout's base character upper-cased → US-ANSI fallback.
    public static func keyName(keyCode: UInt16, layoutCharacter: String?) -> String? {
        if let special = specialKeys[keyCode] { return special }
        if let digit = digitRow[keyCode] { return digit }
        if let c = layoutCharacter?.trimmingWhitespace(), !c.isEmpty,
           c.unicodeScalars.allSatisfy({ $0.value >= 0x20 && $0.value != 0x7F }) {
            return c.uppercased()
        }
        return ansiKeys[keyCode]
    }

    /// One call: nil unless ⌘ is held and the key has a name.
    public static func chord(keyCode: UInt16, cgEventFlags: UInt64, layoutCharacter: String?) -> String? {
        let mods = ChordModifiers(cgEventFlags: cgEventFlags)
        guard mods.contains(.command) else { return nil }
        guard let name = keyName(keyCode: keyCode, layoutCharacter: layoutCharacter) else { return nil }
        return format(modifiers: mods, keyName: name)
    }

    // kVK_* values from Carbon HIToolbox/Events.h.
    static let specialKeys: [UInt16: String] = [
        36: "RETURN", 48: "TAB", 49: "SPACE", 51: "DELETE", 53: "ESC", 117: "FWDDELETE",
        76: "ENTER", 114: "HELP", 115: "HOME", 119: "END", 116: "PAGEUP", 121: "PAGEDOWN",
        123: "←", 124: "→", 125: "↓", 126: "↑",
        122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6", 98: "F7", 100: "F8",
        101: "F9", 109: "F10", 103: "F11", 111: "F12", 105: "F13", 107: "F14", 113: "F15",
        106: "F16", 64: "F17", 79: "F18", 80: "F19", 90: "F20",
    ]
    static let digitRow: [UInt16: String] = [
        18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6", 26: "7", 28: "8", 25: "9", 29: "0",
    ]
    static let ansiKeys: [UInt16: String] = [
        0: "A", 11: "B", 8: "C", 2: "D", 14: "E", 3: "F", 5: "G", 4: "H", 34: "I", 38: "J",
        40: "K", 37: "L", 46: "M", 45: "N", 31: "O", 35: "P", 12: "Q", 15: "R", 1: "S", 17: "T",
        32: "U", 9: "V", 13: "W", 7: "X", 16: "Y", 6: "Z",
        24: "=", 27: "-", 30: "]", 33: "[", 39: "'", 41: ";", 42: "\\", 43: ",", 44: "/",
        47: ".", 50: "`",
    ]
}

extension String {
    func trimmingWhitespace() -> String {
        var s = Substring(self)
        while let f = s.first, f.isWhitespace || f.isNewline { s.removeFirst() }
        while let l = s.last, l.isWhitespace || l.isNewline { s.removeLast() }
        return String(s)
    }
}
