import Foundation

/// `settings.json` in the os-intent data dir (0600). Schema (all keys optional on read):
/// `{ "paused": false, "hud_enabled": true, "denylist_apps": [..], "repo_dir": "...",
///    "hotkeys": {"option_1": {"modifiers": ["option"], "key": "1"}, ...}, "wish": {"locale": "fr-FR"},
///    "gate": {"show_min_p": 0.15, ...}, "live": {"poll_s": 2, ...},
///    "hud": {"slots": {"fn": "left", ...}} (W10), "vocal_recipients": {...} (W12), "updated_at": ISO-8601 }`
/// `vocal_recipients` (W12) belongs to the python sender (`oi vocal-send`): kept as raw JSON and
/// written back unchanged.
/// `gate` / `live` belong to the python live driver (`oi live`, W8): kept as flat numbers (and, since
/// W9, booleans; since W13, strings and null, e.g. `live.predictor`, `live.laya_checkpoint`) and
/// written back unchanged, so a menu toggle never drops them. oi-hud reads two
/// `gate` keys itself (W9): `ttl_ms` (client default) and `hide_on_pointer_travel`.
public struct OISettings: Codable, Equatable, Sendable {
    public static let defaultDenylist = ["Passwords", "Keychain Access", "1Password"]
    public static let defaultRepoDir = NSString(string: "~/os-intent").expandingTildeInPath

    public var paused: Bool
    public var hudEnabled: Bool
    public var denylistApps: [String]
    public var repoDir: String
    public var hotkeys: HotkeySet
    public var wish: WishSettings
    public var gate: [String: GateValue]?
    public var live: [String: GateValue]?
    public var hud: HUDSettings
    public var vocalRecipients: JSONValue?
    /// W14: `claude` {enabled, enrich, local_only_domains, debug} belongs to the python side
    /// (oi/claude_actions.py): kept as raw JSON and written back unchanged.
    public var claude: JSONValue?
    /// W17: `shots` {enabled, clipboard, fallback_contact, ttl_ms} and `contact_map` {chat title: agent}
    /// belong to the python side (oi/shots.py, oi/contact.py): kept as raw JSON and written back unchanged.
    /// oi-watcher reads `shots.enabled` / `shots.clipboard` itself (`shotFlags`).
    public var shots: JSONValue?
    public var contactMap: JSONValue?
    public var updatedAt: String?
    /// W26: top-level keys this struct does not know (a newer python key, a hand edit): kept as read
    /// and written back unchanged, so a save from the menu bar never drops them.
    public var extra: [String: JSONValue] = [:]

    public init(paused: Bool = false, hudEnabled: Bool = true,
                denylistApps: [String] = OISettings.defaultDenylist,
                repoDir: String = OISettings.defaultRepoDir, hotkeys: HotkeySet = .defaults,
                wish: WishSettings = WishSettings(), hud: HUDSettings = HUDSettings(), updatedAt: String? = nil) {
        self.paused = paused; self.hudEnabled = hudEnabled
        self.denylistApps = denylistApps; self.repoDir = repoDir; self.hotkeys = hotkeys
        self.wish = wish; self.hud = hud; self.updatedAt = updatedAt
    }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case paused, hudEnabled = "hud_enabled", denylistApps = "denylist_apps"
        case repoDir = "repo_dir", hotkeys, wish, gate, live, hud, updatedAt = "updated_at"
        case vocalRecipients = "vocal_recipients", claude
        case shots, contactMap = "contact_map"   // W17
    }

    /// Tolerant: a missing or wrongly typed key falls back to its default.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = OISettings()
        paused = (try? c.decodeIfPresent(Bool.self, forKey: .paused)) ?? d.paused
        hudEnabled = (try? c.decodeIfPresent(Bool.self, forKey: .hudEnabled)) ?? d.hudEnabled
        denylistApps = (try? c.decodeIfPresent([String].self, forKey: .denylistApps)) ?? d.denylistApps
        repoDir = (try? c.decodeIfPresent(String.self, forKey: .repoDir)) ?? d.repoDir
        hotkeys = (try? c.decodeIfPresent(HotkeySet.self, forKey: .hotkeys)) ?? d.hotkeys
        wish = (try? c.decodeIfPresent(WishSettings.self, forKey: .wish)) ?? d.wish
        gate = (try? c.decodeIfPresent([String: GateValue].self, forKey: .gate)) ?? nil
        live = (try? c.decodeIfPresent([String: GateValue].self, forKey: .live)) ?? nil
        hud = (try? c.decodeIfPresent(HUDSettings.self, forKey: .hud)) ?? d.hud
        vocalRecipients = (try? c.decodeIfPresent(JSONValue.self, forKey: .vocalRecipients)) ?? nil
        claude = (try? c.decodeIfPresent(JSONValue.self, forKey: .claude)) ?? nil
        shots = (try? c.decodeIfPresent(JSONValue.self, forKey: .shots)) ?? nil            // W17
        contactMap = (try? c.decodeIfPresent(JSONValue.self, forKey: .contactMap)) ?? nil  // W17
        updatedAt = try? c.decodeIfPresent(String.self, forKey: .updatedAt)
        extra = JSONValue.unknownKeys(decoder, known: Set(CodingKeys.allCases.map(\.rawValue)))   // W26
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(paused, forKey: .paused); try c.encode(hudEnabled, forKey: .hudEnabled)
        try c.encode(denylistApps, forKey: .denylistApps); try c.encode(repoDir, forKey: .repoDir)
        try c.encode(hotkeys, forKey: .hotkeys); try c.encode(wish, forKey: .wish)
        try c.encodeIfPresent(gate, forKey: .gate); try c.encodeIfPresent(live, forKey: .live)
        try c.encode(hud, forKey: .hud)
        try c.encodeIfPresent(vocalRecipients, forKey: .vocalRecipients); try c.encodeIfPresent(claude, forKey: .claude)
        try c.encodeIfPresent(shots, forKey: .shots); try c.encodeIfPresent(contactMap, forKey: .contactMap)
        try c.encodeIfPresent(updatedAt, forKey: .updatedAt)
        try JSONValue.encodeExtra(extra, to: encoder)   // W26
    }

    /// W9: `gate.ttl_ms` — how long the HUD stays (default 6000 since W19, was 15000; clamped 1500-60000).
    public static let defaultTTLms = 6000
    public var gateTTLms: Int {
        guard let v = gate?["ttl_ms"]?.number, v >= 0 else { return OISettings.defaultTTLms }
        return min(max(Int(v), 1500), 60_000)
    }

    /// W9: when fast pointer travel hides the HUD. ON by default, but never in the first
    /// `pointer_grace_ms` (2000) after a show, and then only on SUSTAINED fast travel
    /// (≥ `pointer_fast_px_s` 1200 px/s for ≥ `pointer_fast_ms` 250 ms): slow or short moves never hide it.
    public var pointerHide: PointerHide {
        let d = PointerHide()
        func num(_ k: String, _ def: Double) -> Double {
            guard let v = gate?[k]?.number, v >= 0 else { return def }
            return v
        }
        return PointerHide(enabled: gate?["hide_on_pointer_travel"]?.bool ?? d.enabled,
                           graceMs: num("pointer_grace_ms", d.graceMs), fastPxS: num("pointer_fast_px_s", d.fastPxS),
                           fastMs: num("pointer_fast_ms", d.fastMs))
    }

    /// settings.json `vocal.max_s` (the user 2026-10-21 15:35Z, "il faut augmenter les 60 secondes max parce que ça me
    /// coupe au milieu"): how long a "Send a vocal to…" recording may run before it sends by itself. Default 300 s,
    /// clamped 15-900; missing or wrongly typed = the default. `vocal` is otherwise python's (`vocal.shot_offer`) and
    /// kept unchanged in `extra`.
    public static let defaultVocalMaxS: Double = 300
    public static let vocalMaxSRange: ClosedRange<Double> = 15...900
    public var vocalMaxS: Double {
        guard case .object(let o)? = extra["vocal"], case .number(let n)? = o["max_s"], n.isFinite else {
            return OISettings.defaultVocalMaxS
        }
        return min(max(n, OISettings.vocalMaxSRange.lowerBound), OISettings.vocalMaxSRange.upperBound)
    }

    /// W17: oi-watcher's screenshot sources: (files, clipboard images). `shots.enabled` false turns
    /// both off; `shots.clipboard` false only the clipboard. Missing or wrongly typed = on.
    public var shotFlags: (files: Bool, clipboard: Bool) {
        guard case .object(let o)? = shots else { return (true, true) }
        let enabled: Bool = { if case .bool(let b)? = o["enabled"] { return b }; return true }()
        let clip: Bool = { if case .bool(let b)? = o["clipboard"] { return b }; return true }()
        return (enabled, enabled && clip)
    }

    public func encoded() throws -> Data {
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return try enc.encode(self)
    }

    /// Unreadable or corrupt bytes give the defaults (the watcher must never stop on a bad file).
    public static func decode(_ data: Data) -> OISettings {
        (try? JSONDecoder().decode(OISettings.self, from: data)) ?? OISettings()
    }

    /// Trim, drop empties and case-insensitive duplicates, keep the first spelling and order.
    public static func cleanDenylist<S: Sequence>(_ names: S) -> [String] where S.Element == String {
        var seen = Set<String>(); var out: [String] = []
        for n in names {
            let t = n.trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty, seen.insert(t.lowercased()).inserted { out.append(t) }
        }
        return out
    }
}

/// settings.json `gate` pointer keys, resolved with their defaults (see `OISettings.pointerHide`).
public struct PointerHide: Equatable, Sendable {
    public var enabled = true
    public var graceMs: Double = 2000
    public var fastPxS: Double = 1200
    public var fastMs: Double = 250
    public init(enabled: Bool = true, graceMs: Double = 2000, fastPxS: Double = 1200, fastMs: Double = 250) {
        self.enabled = enabled; self.graceMs = graceMs; self.fastPxS = fastPxS; self.fastMs = fastMs
    }
}

/// One flat value of `gate` / `live`: a number, (W9) a boolean, or (W13) a string or null — the
/// Laya layer's `live.predictor` / `live.laya_url` / `live.laya_checkpoint`, written by `oi laya-promote`; or an
/// object (the outward gate's per-class `gate.outward` {"vocal": false, …}: kept, never a reason to drop `gate`).
public indirect enum GateValue: Codable, Equatable, Sendable, ExpressibleByFloatLiteral, ExpressibleByIntegerLiteral,
                       ExpressibleByBooleanLiteral, ExpressibleByStringLiteral {
    case number(Double)
    case bool(Bool)
    case string(String)
    case null
    case object([String: GateValue])

    public init(floatLiteral v: Double) { self = .number(v) }
    public init(integerLiteral v: Int) { self = .number(Double(v)) }
    public init(booleanLiteral v: Bool) { self = .bool(v) }
    public init(stringLiteral v: String) { self = .string(v) }

    public var number: Double? { if case .number(let d) = self { return d }; return nil }
    public var bool: Bool? { if case .bool(let b) = self { return b }; return nil }
    public var string: String? { if case .string(let s) = self { return s }; return nil }

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let b = try? c.decode(Bool.self) { self = .bool(b); return }   // JSON true/false (never 0/1)
        if let d = try? c.decode(Double.self) { self = .number(d); return }
        if let o = try? c.decode([String: GateValue].self) { self = .object(o); return }
        self = .string(try c.decode(String.self))
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .number(let d): try c.encode(d)
        case .bool(let b): try c.encode(b)
        case .string(let s): try c.encode(s)
        case .null: try c.encodeNil()
        case .object(let o): try c.encode(o)
        }
    }
}

/// settings.json `wish`: the wish quick action (W6). `locale` = the on-device dictation
/// language (BCP-47, default "fr-FR"; oi-hud falls back to "en-US" when it has no on-device model).
public struct WishSettings: Codable, Equatable, Sendable {
    public static let defaultLocale = "fr-FR"
    public var locale: String

    public init(locale: String = WishSettings.defaultLocale) { self.locale = locale }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let l = (try? c.decodeIfPresent(String.self, forKey: .locale)) ?? nil
        locale = (l?.isEmpty == false) ? l! : WishSettings.defaultLocale
    }
}

public enum SettingsStore {
    /// Same rule as python `oi.paths.data_dir()`: `$OI_DATA_DIR` or `~/.local/share/os-intent`.
    public static func dataDir(env: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        if let d = env["OI_DATA_DIR"], !d.isEmpty {
            return URL(fileURLWithPath: (d as NSString).expandingTildeInPath, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/share/os-intent", isDirectory: true)
    }

    public static var defaultURL: URL { dataDir().appendingPathComponent("settings.json") }

    public static func load(_ url: URL = defaultURL) -> OISettings {
        guard let data = try? Data(contentsOf: url) else { return OISettings() }
        return OISettings.decode(data)
    }

    /// Atomic write: temp file (0600) in the same dir, then rename(2) over the target.
    /// Stamps `updated_at`. Creates the data dir 0700 if missing.
    @discardableResult
    public static func save(_ settings: OISettings, to url: URL = defaultURL, now: Date = Date()) throws -> OISettings {
        var s = settings
        s.updatedAt = isoTimestamp(now)
        try atomicWrite(s.encoded() + Data("\n".utf8), to: url)
        return s
    }

    /// Temp file (0600) in the same dir, then rename(2) over `url`. Creates the dir 0700.
    public static func atomicWrite(_ data: Data, to url: URL) throws {
        let dir = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let tmp = dir.appendingPathComponent(".\(url.lastPathComponent).tmp-\(getpid())-\(UInt32.random(in: 0...UInt32.max))")
        guard FileManager.default.createFile(atPath: tmp.path, contents: data,
                                             attributes: [.posixPermissions: 0o600]) else {
            throw CocoaError(.fileWriteUnknown, userInfo: [NSFilePathErrorKey: tmp.path])
        }
        if rename(tmp.path, url.path) != 0 {
            let err = errno
            unlink(tmp.path)
            throw POSIXError(POSIXErrorCode(rawValue: err) ?? .EIO)
        }
    }

    /// Read-modify-write helper used by the menu bar app.
    @discardableResult
    public static func update(at url: URL = defaultURL, _ change: (inout OISettings) -> Void) throws -> OISettings {
        var s = load(url)
        change(&s)
        return try save(s, to: url)
    }

    public static func isoTimestamp(_ d: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: d)
    }
}

/// Re-reads the file only when its mtime (or existence) changes. Thread-safe.
public final class SettingsCache: @unchecked Sendable {
    public let url: URL
    private let lock = NSLock()
    private var mtime: Date?
    private var exists = false
    private var cached = OISettings()
    private var loaded = false

    public init(url: URL = SettingsStore.defaultURL) { self.url = url }

    /// Current settings; returns `(settings, changed)` where `changed` means the file was re-read.
    @discardableResult
    public func poll() -> (OISettings, Bool) {
        lock.lock(); defer { lock.unlock() }
        let attrs = try? FileManager.default.attributesOfItem(atPath: url.path)
        let m = attrs?[.modificationDate] as? Date
        let e = attrs != nil
        if loaded && e == exists && m == mtime { return (cached, false) }
        loaded = true; exists = e; mtime = m
        cached = e ? SettingsStore.load(url) : OISettings()
        return (cached, true)
    }

    public var current: OISettings { poll().0 }
}

/// Any JSON value, kept as read and written back unchanged (settings keys owned by the python side,
/// W12 `vocal_recipients`).
public indirect enum JSONValue: Codable, Equatable, Sendable {
    case null, bool(Bool), number(Double), string(String), array([JSONValue]), object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .number(let n): try c.encode(n)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    /// W26: any-name coding key, for the keys a typed struct does not know.
    struct AnyKey: CodingKey {
        var stringValue: String; var intValue: Int? { nil }
        init(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    /// The keys of a JSON object outside `known`, as raw JSON (empty when the decoder is not an object).
    static func unknownKeys(_ decoder: Decoder, known: Set<String>) -> [String: JSONValue] {
        guard let c = try? decoder.container(keyedBy: AnyKey.self) else { return [:] }
        var out: [String: JSONValue] = [:]
        for k in c.allKeys where !known.contains(k.stringValue) {
            if let v = try? c.decode(JSONValue.self, forKey: k) { out[k.stringValue] = v }
        }
        return out
    }

    /// Writes `extra` next to the typed keys of the same object.
    static func encodeExtra(_ extra: [String: JSONValue], to encoder: Encoder) throws {
        guard !extra.isEmpty else { return }
        var c = encoder.container(keyedBy: AnyKey.self)
        for (k, v) in extra { try c.encode(v, forKey: AnyKey(stringValue: k)) }
    }
}
