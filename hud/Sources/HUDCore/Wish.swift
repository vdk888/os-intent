import Foundation
import OISettings

// The wish quick action (W6, ): "tell me what you wanted", typed or dictated, at any
// moment. Pure parts only (row encoding, the file store, the hotkey registration rule, the panel
// state machine, the HUD slot of the wish option); the AppKit/Speech side lives in oi-hud.

/// Carbon hotkey id of the wish hotkey (option ids are 1-4, dismiss is 100).
public let wishHotkeyID: UInt32 = 200
/// Internal key of the "Tell me what you wanted" HUD option. Clients can never send key 0
/// (decodeCommand accepts 1-4 only), so it never clashes with a predicted option.
public let wishOptionKey = 0
public let wishOptionLabel = "Tell me what you wanted"
public let wishOptionIcon = "sf:text.bubble"
/// Longest wish kept (characters); the field is single-line, this only bounds a runaway paste.
public let wishMaxChars = 500
/// Dictation stops by itself after this many seconds.
public let dictationLimitS: Double = 20

// MARK: - row

/// What was on screen when the wish was made. `hud_options` = the labels of the predicted options
/// if the HUD was visible (the keys alone mean nothing once the options change), else [].
public struct WishContext: Codable, Equatable, Sendable {
    public var app: String?
    public var space: Int?
    public var hudVisible: Bool
    public var hudOptions: [String]

    public init(app: String?, space: Int?, hudVisible: Bool, hudOptions: [String]) {
        self.app = app; self.space = space; self.hudVisible = hudVisible; self.hudOptions = hudOptions
    }

    enum CodingKeys: String, CodingKey { case app, space, hudVisible = "hud_visible", hudOptions = "hud_options" }

    // explicit nulls for app/space (the synthesised encoder would drop the keys)
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(app, forKey: .app)
        try c.encode(space, forKey: .space)
        try c.encode(hudVisible, forKey: .hudVisible)
        try c.encode(hudOptions, forKey: .hudOptions)
    }
}

public enum WishSource: String, Codable, Sendable { case typed, dictated }

/// One line of `wishes/YYYY-MM-DD.jsonl`:
/// `{"context":{...},"source":"typed|dictated","text":"...","ts":"2026-10-20T11:00:00.123Z"}`.
public struct WishRow: Codable, Equatable, Sendable {
    public var ts: String
    public var text: String
    public var source: WishSource
    public var context: WishContext
    /// W18: the action a resolved wish ran (absent for a wish that was only noted).
    public var action: WishAction? = nil

    public init(ts: String, text: String, source: WishSource, context: WishContext) {
        self.ts = ts; self.text = text; self.source = source; self.context = context
    }

    /// nil when nothing is left after cleaning (an empty wish is a cancel, never a row).
    public init?(date: Date, rawText: String, source: WishSource, context: WishContext) {
        guard let t = WishRow.clean(rawText) else { return nil }
        self.init(ts: WishRow.timestamp(date), text: t, source: source, context: context)
    }

    /// Trim, fold newlines/tabs to spaces, cap at `wishMaxChars`; nil if empty.
    public static func clean(_ s: String) -> String? {
        let one = s.components(separatedBy: .newlines).joined(separator: " ").replacingOccurrences(of: "\t", with: " ")
        let t = one.trimmingCharacters(in: .whitespaces)
        return t.isEmpty ? nil : String(t.prefix(wishMaxChars))
    }

    /// ISO-8601 UTC with milliseconds and a Z, like the python token rows.
    public static func timestamp(_ d: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        f.timeZone = TimeZone(identifier: "UTC")
        return f.string(from: d)
    }

    public func jsonLine() throws -> String {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return String(decoding: try enc.encode(self), as: UTF8.self) + "\n"
    }
}

// MARK: - store

/// `<data dir>/wishes/YYYY-MM-DD.jsonl` (UTC day, like tokens/), dir 0700, file 0600, append-only.
public enum WishStore {
    public static func dir(dataDir: URL = SettingsStore.dataDir()) -> URL {
        dataDir.appendingPathComponent("wishes", isDirectory: true)
    }

    public static func day(_ d: Date) -> String {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let c = cal.dateComponents([.year, .month, .day], from: d)
        return String(format: "%04d-%02d-%02d", c.year!, c.month!, c.day!)
    }

    public static func fileURL(for d: Date, dataDir: URL = SettingsStore.dataDir()) -> URL {
        dir(dataDir: dataDir).appendingPathComponent("\(day(d)).jsonl")
    }

    /// Appends one row; returns the file written. The day comes from `date` (= the row's ts).
    @discardableResult
    public static func append(_ row: WishRow, date: Date, dataDir: URL = SettingsStore.dataDir()) throws -> URL {
        let fm = FileManager.default
        try fm.createDirectory(at: dataDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let d = dir(dataDir: dataDir)
        try fm.createDirectory(at: d, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        chmod(d.path, 0o700)
        let url = fileURL(for: date, dataDir: dataDir)
        let fd = open(url.path, O_WRONLY | O_APPEND | O_CREAT, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(fd) }
        fchmod(fd, 0o600)
        let bytes = Array(try row.jsonLine().utf8)
        let n = bytes.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        guard n == bytes.count else { throw POSIXError(.EIO) }
        return url
    }
}

// MARK: - hotkey

/// The wish hotkey registration. It depends ONLY on `hotkeys` (never on `hud_enabled` or
/// `paused`): the wish is the one action that is always available while the daemon runs.
/// An invalid set falls back to the defaults as a whole (same rule as hotkeyPlan), so ⌥`.
public func wishRegistration(hotkeys: HotkeySet) -> HotkeyRegistration? {
    let set = hotkeys.problems().isEmpty ? hotkeys : HotkeySet.defaults
    guard let code = set.wish.keyCode else { return nil }
    return HotkeyRegistration(code: UInt32(code), mods: set.wish.carbonModifiers, id: wishHotkeyID)
}

/// Keeps the permanent wish hotkey in step with settings.json (polled by the daemon).
public struct WishHotkeyTracker: Sendable {
    public private(set) var current: HotkeyRegistration?
    public init() {}

    /// Returns the registration to (re-)install when it changed, nil when nothing to do.
    public mutating func sync(_ settings: OISettings) -> HotkeyRegistration? {
        let want = wishRegistration(hotkeys: settings.hotkeys)
        guard want != current else { return nil }
        current = want
        return want
    }

    /// The hotkey label to show on the HUD badge and in the panel hint.
    public static func display(_ settings: OISettings) -> String {
        (settings.hotkeys.problems().isEmpty ? settings.hotkeys : HotkeySet.defaults).wish.display
    }
}

// MARK: - panel state machine

public enum WishPanelState: Equatable, Sendable { case closed, typing, dictating }

public enum WishPanelInput: Equatable, Sendable {
    case hotkey            // the wish hotkey (or the HUD's wish option)
    case mic               // the mic button
    case enter, escape, clickOutside
    case dictationEnded    // 20 s limit or dictation unavailable (W20: never a recogniser final / error)
}

public enum WishPanelEffect: Equatable, Sendable { case open, startDictation, stopDictation, save, cancel }

/// closed --hotkey--> typing --hotkey|mic--> dictating --hotkey|mic|ended--> typing;
/// Enter saves, Esc / click outside cancel (both stop a running dictation first).
public struct WishPanelMachine: Sendable {
    public private(set) var state: WishPanelState = .closed
    public init() {}

    public mutating func handle(_ input: WishPanelInput) -> [WishPanelEffect] {
        switch (state, input) {
        case (.closed, .hotkey):
            state = .typing; return [.open]
        case (.closed, _):
            return []
        case (.typing, .hotkey), (.typing, .mic):
            state = .dictating; return [.startDictation]
        case (.dictating, .hotkey), (.dictating, .mic), (.dictating, .dictationEnded):
            state = .typing; return [.stopDictation]
        case (.typing, .dictationEnded):
            return []
        case (_, .enter):
            let stop: [WishPanelEffect] = state == .dictating ? [.stopDictation] : []
            state = .closed; return stop + [.save]
        case (_, .escape), (_, .clickOutside):
            let stop: [WishPanelEffect] = state == .dictating ? [.stopDictation] : []
            state = .closed; return stop + [.cancel]
        }
    }
}

// MARK: - HUD slot

/// The options the HUD draws: the predicted ones (≤ 4) plus the permanent wish option, last.
public func optionsWithWish(_ predicted: [HUDOption]) -> [HUDOption] {
    predicted.filter { $0.key != wishOptionKey }.prefix(maxOptions)
        + [HUDOption(key: wishOptionKey, label: wishOptionLabel, icon: wishOptionIcon)]
}

/// Where each option sits, relative to the centre (y-down): each predicted option at the FIXED place
/// of its key (`positions`, FixedSlots.positions — W10: no re-flow, an absent option leaves its
/// place empty), the wish option at its own fixed place below-right (4:30) and (W12) the vocal option
/// below-left (7:30), so nothing overlaps and the pointer spot in the middle stays empty. The order of
/// `options` does not matter.
public struct HUDSlot: Equatable, Sendable {
    public var key: Int
    public var x: Double
    public var y: Double
    public var width: Double
    public var height: Double
    public var position: SlotPosition?        // nil = the wish or the vocal option

    public init(key: Int, x: Double, y: Double, width: Double, height: Double, position: SlotPosition?) {
        self.key = key; self.x = x; self.y = y; self.width = width; self.height = height; self.position = position
    }
}

/// Fixed widths: every option `capsule.w`, the wish and vocal options `wishWidth`.
public func hudSlots(options: [HUDOption], positions: [Int: SlotPosition], radius: Double,
                     capsule: (w: Double, h: Double), wishWidth: Double) -> [HUDSlot] {
    hudSlots(options: options, positions: positions, radius: radius, capsuleHeight: capsule.h) {
        $0.key == wishOptionKey || $0.key == vocalOptionKey ? wishWidth : capsule.w
    }
}

/// Per-capsule widths (the user 20/10 16:40: the width follows the content, `capsuleWidth`): the side
/// capsules keep their inner edge 16 px from the centre whatever their width; each stays centred on its
/// fixed place otherwise.
public func hudSlots(options: [HUDOption], positions: [Int: SlotPosition], radius: Double,
                     capsuleHeight h: Double, width: (HUDOption) -> Double) -> [HUDSlot] {
    options.compactMap { o in
        let w = width(o)
        if o.key == wishOptionKey {
            let p = FixedSlots.wishOffset(radius: radius, capsuleHeight: h, wishWidth: w)
            return HUDSlot(key: o.key, x: p.x, y: p.y, width: w, height: h, position: nil)
        }
        if o.key == vocalOptionKey {
            let p = FixedSlots.vocalOffset(radius: radius, capsuleHeight: h, vocalWidth: w)
            return HUDSlot(key: o.key, x: p.x, y: p.y, width: w, height: h, position: nil)
        }
        guard let pos = positions[o.key] else { return nil }
        let off = FixedSlots.offset(pos, radius: radius, capsuleWidth: w)
        return HUDSlot(key: o.key, x: off.x, y: off.y, width: w, height: h, position: pos)
    }
}
