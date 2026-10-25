import Foundation
import OISettings

// W12: the permanent "Send a vocal to…" option. Pure parts only:
// the option itself, the agent picker (pages + slot mapping), the flow state machine, the transcript
// guard and the on-disk names; the AppKit/Speech side lives in oi-hud (VocalPanel.swift), the delivery
// in python (`oi vocal-send`, oi/vocal.py).

/// Internal key of the vocal option (never sent by a client: decodeCommand accepts 1-4 only).
public let vocalOptionKey = -1
public let vocalOptionLabel = "Send a vocal to…"
public let vocalOptionIcon = "sf:mic"
/// Carbon id of the vocal hotkey when it is a chord (taps need no registration).
public let vocalHotkeyID: UInt32 = 400
/// The recording stops (and sends) by itself after settings `vocal.max_s` seconds (OISettings.vocalMaxS, default
/// 300: the user 2026-10-21, 60 s cut him mid-sentence). The last `vocalCountdownS` seconds show a countdown.
public let vocalCountdownS: Double = 15
/// Longest transcript sent (characters): a 5-minute French dictation is ~5000; == oi.vocal.MAX_CHARS.
public let vocalMaxChars = 16000

/// The recording panel's limit texts, pure.
public enum VocalCountdown {
    /// "5 min max" / "90 s max" for the hint.
    public static func limitText(_ limitS: Double) -> String {
        let s = Int(limitS.rounded())
        return s >= 60 && s % 60 == 0 ? "\(s / 60) min max" : "\(s) s max"
    }

    /// The countdown shown in the last `vocalCountdownS` seconds ("sends in 12 s"), nil before (the panel then
    /// says "listening…"). Whole seconds, rounded up, never below 1.
    public static func text(elapsed: Double, limitS: Double) -> String? {
        let left = limitS - elapsed
        guard left <= vocalCountdownS, left > -1 else { return nil }
        return "sends in \(max(1, Int(left.rounded(.up)))) s"
    }
}

public struct VocalAgent: Equatable, Hashable, Sendable {
    public let slug: String
    public let name: String
    public init(_ slug: String, _ name: String) { self.slug = slug; self.name = name }
    public var initial: String { String(name.prefix(1)) }
}

/// The agent picker: the HUD turns into up to four agents at the SAME fixed places as the options, each
/// bound to a modifier (fn ⌃ ⌥ ⌘, so fn = left, ⌃ = top, ⌥ = right, ⌘ = bottom by default); the vocal
/// key again shows the next page (wrapping). Page 1: Atlas Nova Orion Lyra; 2: Vega Juno Iris
/// Hera; 3: Echo.
public enum VocalPicker {
    public static let defaultAgents: [VocalAgent] = [
        VocalAgent("atlas", "Atlas"), VocalAgent("nova", "Nova"), VocalAgent("orion", "Orion"), VocalAgent("lyra", "Lyra"),
        VocalAgent("vega", "Vega"), VocalAgent("juno", "Juno"), VocalAgent("iris", "Iris"),
        VocalAgent("hera", "Hera"), VocalAgent("echo", "Echo"),
    ]
    /// Demo mode (`oi demo`): `OI_HUD_AGENTS="atlas:Atlas,nova:Nova,…"` replaces the roster, so a screen recording
    /// shows fictional agents only. Absent or invalid = the fleet roster above.
    public static let agents: [VocalAgent] = parseAgents(ProcessInfo.processInfo.environment["OI_HUD_AGENTS"]) ?? defaultAgents

    /// `slug:Name` pairs, comma-separated: slug [a-z0-9-], name 1-20 characters, 1-12 agents, unique slugs.
    public static func parseAgents(_ spec: String?) -> [VocalAgent]? {
        guard let spec, !spec.isEmpty else { return nil }
        var out: [VocalAgent] = []
        for pair in spec.split(separator: ",", omittingEmptySubsequences: false) {
            let parts = pair.split(separator: ":", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
            guard parts.count == 2, !parts[0].isEmpty, (1...20).contains(parts[1].count),
                  parts[0].allSatisfy({ ("a"..."z").contains($0) || ("0"..."9").contains($0) || $0 == "-" }),
                  !out.contains(where: { $0.slug == parts[0] }) else { return nil }
            out.append(VocalAgent(parts[0], parts[1]))
        }
        return (1...12).contains(out.count) ? out : nil
    }
    /// Agent order inside a page → modifier.
    public static let slotOrder: [TapModifier] = [.fn, .ctrl, .option, .cmd]
    public static var pageCount: Int { (agents.count + slotOrder.count - 1) / slotOrder.count }

    public static func nextPage(_ p: Int) -> Int { (p + 1) % pageCount }

    /// Page `p` (0-based): modifier → agent.
    public static func page(_ p: Int) -> [TapModifier: VocalAgent] {
        let start = (((p % pageCount) + pageCount) % pageCount) * slotOrder.count
        var out: [TapModifier: VocalAgent] = [:]
        for (i, m) in slotOrder.enumerated() where start + i < agents.count { out[m] = agents[start + i] }
        return out
    }

    /// The option key (1-4) a modifier picks: the option whose tap it is, else (an option set as a chord)
    /// the option whose DEFAULT tap it is, which is also where FixedSlots.positions puts that option.
    public static func optionKey(for m: TapModifier, hotkeys: HotkeySet) -> Int? {
        let set = hotkeys.problems().isEmpty ? hotkeys : HotkeySet.defaults
        if let k = (1...maxOptions).first(where: { set.option($0)?.tap == m }) { return k }
        return (1...maxOptions).first { HotkeySet.defaults.option($0)?.tap == m && set.option($0)?.isTap != true }
    }

    /// The picker as HUD options: key = the option key of the agent's modifier, label = the first name,
    /// icon = the initial (`initial:T`). Sorted by key.
    public static func options(page p: Int, hotkeys: HotkeySet) -> [HUDOption] {
        page(p).compactMap { m, a in
            optionKey(for: m, hotkeys: hotkeys).map { HUDOption(key: $0, label: a.name, icon: "initial:\(a.initial)") }
        }.sorted { $0.key < $1.key }
    }

    /// The modifier an agent sits on (its page's slot order): Atlas fn, Nova ⌃, …
    public static func modifier(for a: VocalAgent) -> TapModifier? {
        agents.firstIndex(of: a).map { slotOrder[$0 % slotOrder.count] }
    }

    public static func agent(slug: String) -> VocalAgent? { agents.first { $0.slug == slug } }

    /// The agent an option key picks on page `p`.
    public static func agent(forOptionKey k: Int, page p: Int, hotkeys: HotkeySet) -> VocalAgent? {
        page(p).first { m, _ in optionKey(for: m, hotkeys: hotkeys) == k }?.value
    }
}

/// The vocal flow: idle → (vocal key) picking page 0 → (vocal key) next page … → (an agent) recording →
/// (vocal key | Enter | time limit, `vocal.max_s`) send → idle. W20: a recogniser final result / error is NOT an end (oi-hud's
/// Dictation starts a new segment), so `.dictationEnded` only comes from the time limit or dictation unavailable. Esc, a click outside or the HUD going away
/// cancel (nothing sent).
public enum VocalState: Equatable, Sendable {
    case idle
    case picking(page: Int)
    case recording(VocalAgent)
}

public enum VocalInput: Equatable, Sendable {
    case vocalKey
    case pick(VocalAgent)
    case enter
    case cancel            // Esc, click outside, the picker HUD dismissed (ttl, pointer, typing)
    case dictationEnded    // time limit (W20: never a recogniser final / error)
    /// The user 2026-10-21 16:32Z: a text key in the recording panel (VocalKeyRule `.startTyping`). The microphone
    /// stops, the words already transcribed stay, the panel becomes a text field ("Saisie").
    case typed
    /// Dictation could not start (permission denied, no on-device model, not the LaunchAgent): typing mode, not a send.
    case dictationUnavailable
}

public enum VocalEffect: Equatable, Sendable {
    case showPicker(page: Int)
    case closePicker
    case startRecording(VocalAgent)
    case send(VocalAgent)          // stop the recording, then deliver (an empty transcript sends nothing)
    case cancelRecording
    case startTyping               // stop listening (keep the transcript), focus the field with the caret at the end
}

public struct VocalFlow: Sendable {
    public private(set) var state: VocalState = .idle
    /// While `.recording`: false = listening ("Écoute…"), true = typing ("Saisie"; the microphone is off). One way:
    /// typing never goes back to listening.
    public private(set) var typing = false
    public init() {}

    public var isPicking: Bool { if case .picking = state { return true }; return false }
    /// The recording panel is open (listening OR typing).
    public var isRecording: Bool { if case .recording = state { return true }; return false }
    public var isTyping: Bool { isRecording && typing }

    /// For `oi-hud state`: "idle", "picking:1" (1-based page), "recording:nova", "typing:nova".
    public var stateName: String {
        switch state {
        case .idle: return "idle"
        case .picking(let p): return "picking:\(p + 1)"
        case .recording(let a): return "\(typing ? "typing" : "recording"):\(a.slug)"
        }
    }

    public mutating func handle(_ input: VocalInput) -> [VocalEffect] {
        switch (state, input) {
        case (.idle, .vocalKey):
            state = .picking(page: 0); return [.showPicker(page: 0)]
        case (.picking(let p), .vocalKey):
            let n = VocalPicker.nextPage(p)
            state = .picking(page: n); return [.showPicker(page: n)]
        case (.picking, .pick(let a)):
            state = .recording(a); return [.closePicker, .startRecording(a)]
        case (.picking, .cancel):
            state = .idle; return [.closePicker]
        case (.recording, .typed), (.recording, .dictationUnavailable):
            if typing { return [] }
            typing = true; return [.startTyping]
        case (.recording, .dictationEnded) where typing:
            return []                                      // the limit is the microphone's; typing has none
        case (.recording(let a), .vocalKey), (.recording(let a), .enter), (.recording(let a), .dictationEnded):
            state = .idle; typing = false; return [.send(a)]
        case (.recording, .cancel):
            state = .idle; typing = false; return [.cancelRecording]
        default:
            return []
        }
    }
}

/// What a key pressed in the recording panel does (the user 2026-10-21 16:32Z: « si je commence à taper à la main ça
/// arrête d'écouter »). The rule, the same in both modes for the panel's own keys:
/// - Return / keypad Enter sends, Esc cancels (listening AND typing);
/// - listening: a key that writes something (a printable character, a dead key such as ^, Delete / forward Delete,
///   an arrow) switches to typing and is then given to the field; a ⌘ or ⌃ chord, Tab or a key that writes nothing
///   (F-keys…) does nothing;
/// - typing: every other key goes to the field: C, V… are letters there. The screenshot toggle (C) exists only in
///   the outward gate's confirm panel, which opens AFTER Enter, when this panel is already closed.
/// The vocal key (a modifier SET tap, ⌃+⌥ by default) never types a character: it sends in both modes.
public enum VocalPanelKey: Equatable, Sendable {
    case send, cancel, startTyping, field, ignore
}

public enum VocalKeyRule {
    public static let returnKey: UInt16 = 36, keypadEnter: UInt16 = 76, escape: UInt16 = 53
    /// Delete, forward Delete, ← → ↓ ↑: editing keys that start typing too.
    public static let editingKeys: Set<UInt16> = [51, 117, 123, 124, 125, 126]

    public static func classify(keyCode: UInt16, characters: String?, ignoringModifiers: String? = nil,
                                command: Bool = false, control: Bool = false, typing: Bool) -> VocalPanelKey {
        if keyCode == returnKey || keyCode == keypadEnter { return .send }
        if keyCode == escape { return .cancel }
        if typing { return .field }
        if command || control { return .ignore }
        if editingKeys.contains(keyCode) { return .startTyping }
        // a dead key (^ on a French keyboard) has no characters yet; its unmodified character says it writes
        let chars = (characters?.isEmpty == false ? characters : ignoringModifiers) ?? ""
        return writes(chars) ? .startTyping : .ignore
    }

    /// At least one character that is neither a control character nor an AppKit function-key code (U+F700-U+F8FF).
    static func writes(_ s: String) -> Bool {
        s.unicodeScalars.contains { u in
            !CharacterSet.controlCharacters.contains(u) && !(0xF700...0xF8FF).contains(u.value)
        }
    }
}

/// The panel's indicator and hint, in the dictation locale's language (settings `wish.locale`, fr-FR by default).
public enum VocalModeText {
    static func french(_ locale: String) -> Bool { locale.lowercased().hasPrefix("fr") }

    public static func indicator(typing: Bool, locale: String) -> String {
        french(locale) ? (typing ? "Saisie" : "Écoute…") : (typing ? "typing" : "listening…")
    }

    /// `key` = the vocal key display (⌃⌥); `limitS` = settings `vocal.max_s`.
    public static func hint(typing: Bool, key: String, limitS: Double, locale: String) -> String {
        if french(locale) {
            return typing ? "Entrée ou \(key) envoie · Échap annule"
                : "\(key) ou Entrée envoie · Échap annule · tapez pour écrire · \(VocalCountdown.limitText(limitS))"
        }
        return typing ? "Enter or \(key) sends · Esc cancels"
            : "\(key) or Enter sends · Esc cancels · type to write · \(VocalCountdown.limitText(limitS))"
    }

    /// The field's VoiceOver label.
    public static func fieldLabel(agent: VocalAgent, locale: String) -> String {
        french(locale) ? "Message à \(agent.name)" : "Message to \(agent.name)"
    }
}

public enum VocalText {
    /// Trim, fold newlines/tabs to spaces (one inject line = one turn), cap at `vocalMaxChars`; nil if empty.
    public static func clean(_ s: String) -> String? {
        let one = s.components(separatedBy: .newlines).joined(separator: " ").replacingOccurrences(of: "\t", with: " ")
        let t = one.trimmingCharacters(in: .whitespaces)
        return t.isEmpty ? nil : String(t.prefix(vocalMaxChars))
    }
}

/// `<data dir>/vocals/<YYYYMMDDTHHMMSSZ>-<slug>.caf|.txt` (dir 0700, files 0600). The audio stays here
/// (never transferred); the transcript file is what `oi vocal-send --text-file` reads.
public enum VocalStore {
    public static func dir(dataDir: URL = SettingsStore.dataDir()) -> URL {
        dataDir.appendingPathComponent("vocals", isDirectory: true)
    }

    public static func stem(_ d: Date, slug: String) -> String {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let c = cal.dateComponents([.year, .month, .day, .hour, .minute, .second], from: d)
        return String(format: "%04d%02d%02dT%02d%02d%02dZ-%@", c.year!, c.month!, c.day!, c.hour!, c.minute!, c.second!, slug)
    }

    /// Creates the dir (0700) and returns (audio, transcript) URLs for a new vocal.
    public static func urls(_ d: Date, slug: String, dataDir: URL = SettingsStore.dataDir()) throws -> (audio: URL, text: URL) {
        let fm = FileManager.default
        try fm.createDirectory(at: dataDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let v = dir(dataDir: dataDir)
        try fm.createDirectory(at: v, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        chmod(v.path, 0o700)
        let s = stem(d, slug: slug)
        return (v.appendingPathComponent("\(s).caf"), v.appendingPathComponent("\(s).txt"))
    }

    /// Writes the transcript (0600, no trailing newline needed).
    public static func writeText(_ text: String, to url: URL) throws {
        let fd = open(url.path, O_WRONLY | O_CREAT | O_TRUNC, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(fd) }
        fchmod(fd, 0o600)
        let bytes = Array(text.utf8)
        let n = bytes.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        guard n == bytes.count else { throw POSIXError(.EIO) }
    }
}

/// What `oi vocal-send` printed (one JSON line): `{"ok":true,"to":"nova","name":"Nova"}` or
/// `{"ok":false,"to":"nova","error":"ssh exit 255"}`. Anything else is a failure with the raw reason.
public struct VocalResult: Equatable, Sendable {
    public var ok: Bool
    public var reason: String
    public var dryRun = false

    public init(ok: Bool, reason: String, dryRun: Bool = false) { self.ok = ok; self.reason = reason; self.dryRun = dryRun }

    public static func parse(stdout: String, exitCode: Int32) -> VocalResult {
        let line = stdout.split(separator: "\n").last.map(String.init) ?? ""
        if let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] {
            if obj["ok"] as? Bool == true, exitCode == 0 {
                return VocalResult(ok: true, reason: "", dryRun: obj["dry_run"] as? Bool == true)
            }
            if let e = obj["error"] as? String, !e.isEmpty { return VocalResult(ok: false, reason: e) }
        }
        return VocalResult(ok: false, reason: exitCode == 0 ? "no reply" : "exit \(exitCode)")
    }

    /// The toast text: "Sent to Nova" / "Failed: <reason>" ("Dry run: Nova, nothing sent" under OI_VOCAL_DRY_RUN).
    public func toast(agent: VocalAgent) -> String {
        if ok, dryRun { return "Dry run: \(agent.name), nothing sent" }
        if !ok, reason.hasPrefix("cancelled") { return "Not sent to \(agent.name)" }   // outward gate: ✗ / Esc / timeout
        return ok ? "Sent to \(agent.name)" : "Failed: \(reason)"
    }
}

/// What to arm for the vocal key while the HUD is visible / the vocal flow runs: a set tap (matched on
/// the option-tap monitor's sets) or a Carbon chord. An invalid set falls back to the defaults as a whole.
public enum VocalArm: Equatable, Sendable {
    case tap(Set<TapModifier>)
    case chord(HotkeyRegistration)
    case none

    public init(hotkeys: HotkeySet) {
        let set = hotkeys.problems().isEmpty ? hotkeys : HotkeySet.defaults
        if set.vocal.isTap { self = .tap(Set(set.vocal.tapSet)); return }
        guard let code = set.vocal.keyCode else { self = .none; return }
        self = .chord(HotkeyRegistration(code: UInt32(code), mods: set.vocal.carbonModifiers, id: vocalHotkeyID))
    }

    public static func display(_ hotkeys: HotkeySet) -> String {
        (hotkeys.problems().isEmpty ? hotkeys : HotkeySet.defaults).vocal.display
    }
}

/// Capsule width that follows its content (the user 20/10 16:40 Paris, "less text, more symbols"):
/// padding 6 + icon + (spacing 6 + label) + spacing 6 + badge (+ its 10 px padding) + padding 6,
/// clamped to `capsuleMinWidth`...`capsuleMaxWidth`. `labelWidth` 0 = no label (wish / vocal: icon + key).
public let capsuleMinWidth: Double = 72
public let capsuleMaxWidth: Double = 200

public func capsuleWidth(iconSize: Double, labelWidth: Double, badgeWidth: Double) -> Double {
    let w = 6 + iconSize + (labelWidth > 0 ? 6 + labelWidth : 0) + 6 + badgeWidth + 10 + 6
    return min(max(w.rounded(.up), capsuleMinWidth), capsuleMaxWidth)
}

/// The options the HUD draws: the predicted ones (≤ 4), the permanent wish option and (W12) the
/// permanent vocal option.
public func optionsWithWishAndVocal(_ predicted: [HUDOption]) -> [HUDOption] {
    optionsWithWish(predicted.filter { $0.key != vocalOptionKey })
        + [HUDOption(key: vocalOptionKey, label: vocalOptionLabel, icon: vocalOptionIcon)]
}
