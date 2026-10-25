import Foundation
import OISettings

/// W26: the Settings… window's model. Pure (no AppKit): `init(_:)` reads the exposed keys out of
/// settings.json, `apply(to:)` writes ONLY those keys back (everything else, unknown keys included, stays as
/// read), `problems()` is the live validation (a form with problems is never written), `reset(_:)` puts one
/// section back to its defaults. Keys and ranges are the ones the daemons read:
///   hud.auto_mode / hint_ttl_ms / fill_order (oi live) · hud.hint_dot_size / colors (oi-hud)
///   gate.ttl_ms (both) · gate.hint_cooldown_s (oi live) · gate.hide_on_pointer_travel / pointer_grace_ms /
///   pointer_fast_px_s (oi-hud) · claude.enabled / enrich / local_only_domains (oi/claude_actions.py)
///   shots.enabled / clipboard (oi-watcher) / fallback_contact (oi/shots.py) · paused (all) · denylist_apps (watcher)
///   gate.outward (oi/gate.py: the outward gate; an object {"vocal": …} reads as on, a save of the toggle writes a bool)
///   W24: proactive.sources.* / min_p / min_interval_min / quiet_hours / respect_focus (oi/proactive/config.py).
public struct SettingsForm: Equatable, Sendable {
    public enum Section: String, CaseIterable, Sendable, Identifiable {
        case hud, timings, pointer, colors, fillOrder, keys, claude, screenshots, proactive, vocal, laya, privacy
        public var id: String { rawValue }
        public var title: String {
            switch self {
            case .hud: "HUD"; case .timings: "Timings"; case .pointer: "Pointer"; case .colors: "Colours"
            case .fillOrder: "Fill order"; case .keys: "Keys"; case .claude: "Claude"; case .screenshots: "Screenshots"
            case .proactive: "Proactive"   // W24
            case .vocal: "Vocal recipients"; case .laya: "Laya"; case .privacy: "Privacy"
            }
        }
        public var symbol: String {
            switch self {
            case .hud: "circle.grid.cross"; case .timings: "timer"; case .pointer: "cursorarrow.motionlines"
            case .colors: "paintpalette"; case .fillOrder: "list.number"; case .keys: "keyboard"
            case .claude: "sparkles"; case .screenshots: "camera.viewfinder"; case .vocal: "waveform"
            case .laya: "brain"; case .privacy: "hand.raised"; case .proactive: "bell.badge"
            }
        }
        /// Sections with nothing editable have no Reset.
        public var resettable: Bool { ![.keys, .vocal, .laya].contains(self) }
    }

    // Ranges (inclusive) = the clamps the readers apply, so a value accepted here is used as typed.
    public static let hintDotSizeRange = 4...24                       // HUDSettings.clampHintDotSize
    public static let hintTTLRange = 500...10_000                     // HUDSettings.clampHintTTL, oi.settings HINT_TTL_RANGE_MS
    public static let hintCooldownRange = 0...300                     // gate.hint_cooldown_s (python: any >= 0)
    public static let fullTTLRange = 1500...60_000                    // OISettings.gateTTLms, oi.live TTL_RANGE_MS
    public static let pointerGraceRange = 0...10_000                  // gate.pointer_grace_ms
    public static let pointerSpeedRange = 200...5000                  // gate.pointer_fast_px_s
    public static let defaultHintCooldownS = 8                        // oi.settings GATE_DEFAULTS
    public static let claudeModel = "Sonnet"                          // runtime calls (commit c8ad0b0), read-only
    public static let defaultFallbackContact = "atlas"                 // oi.shots SHOT_DEFAULTS
    // W24: oi/proactive/config.py DEFAULTS and RANGES
    public static let proactiveSources = ["mail", "agents", "files", "cockpit", "metrics", "markets", "decisions", "calendar", "prs"]
    public static let proactiveMinPctRange = 0...100                  // min_p × 100
    public static let proactiveIntervalRange = 0...240                // min_interval_min
    public static let defaultProactiveMinPct = 50
    public static let defaultProactiveInterval = 10
    public static let defaultQuietStart = "22:00", defaultQuietEnd = "08:00"

    // HUD
    public var autoMode: String
    public var hintDotSize: Int
    // Timings
    public var hintTTLms: Int
    public var hintCooldownS: Int
    public var fullTTLms: Int
    // Pointer
    public var pointerHide: Bool
    public var pointerGraceMs: Int
    public var pointerFastPxS: Int
    // Colours ("#RRGGBB" per option modifier)
    public var colors: [TapModifier: HUDColor]
    // Fill order (rank 1 first)
    public var fillOrder: [TapModifier]
    // Claude
    public var claudeEnabled: Bool
    public var claudeEnrich: Bool
    public var localOnlyDomains: [String]
    // Screenshots
    public var shotsEnabled: Bool
    public var shotsClipboard: Bool
    public var fallbackContact: String      // "" = none (written as null)
    // Privacy
    public var paused: Bool
    public var denylistApps: [String]
    public var outwardGate: Bool            // gate.outward: ask before anything leaves the Mac (default on)
    // Proactive (W24): every source off by default; quiet hours "" + "" = none (written as null)
    public var proactiveSources: [String: Bool]
    public var proactiveMinPct: Int
    public var proactiveIntervalMin: Int
    public var quietStart: String
    public var quietEnd: String
    public var respectFocus: Bool

    public init(_ s: OISettings = OISettings()) {
        autoMode = s.hud.autoMode
        hintDotSize = s.hud.hintDotSize
        hintTTLms = s.hud.hintTTLms
        let hc = s.gate?["hint_cooldown_s"]?.number ?? -1     // python: a number >= 0, else the default
        hintCooldownS = hc >= 0 && hc.isFinite ? Int(hc) : SettingsForm.defaultHintCooldownS
        fullTTLms = s.gateTTLms
        let ph = s.pointerHide
        pointerHide = ph.enabled; pointerGraceMs = Int(ph.graceMs); pointerFastPxS = Int(ph.fastPxS)
        colors = s.hud.colors.byModifier
        fillOrder = s.hud.fillOrder
        let c = SettingsForm.object(s.claude)
        claudeEnabled = c["enabled"]?.boolValue ?? true
        claudeEnrich = c["enrich"]?.boolValue ?? true
        localOnlyDomains = SettingsForm.cleanDomains(c["local_only_domains"]?.stringsValue ?? []).domains
        shotsEnabled = s.shotFlags.files
        shotsClipboard = SettingsForm.object(s.shots)["clipboard"]?.boolValue ?? true
        let sh = SettingsForm.object(s.shots)
        if let fc = sh["fallback_contact"] {
            if case .string(let v) = fc { fallbackContact = v } else { fallbackContact = "" }   // null = none
        } else { fallbackContact = SettingsForm.defaultFallbackContact }
        paused = s.paused
        denylistApps = s.denylistApps
        if case .bool(let b)? = s.gate?["outward"] { outwardGate = b } else { outwardGate = true }   // python: else on
        // W24: `proactive` is python-owned (kept in OISettings.extra); wrongly typed = the reader's default
        let pr = SettingsForm.object(s.extra["proactive"])
        let src = SettingsForm.object(pr["sources"])
        proactiveSources = Dictionary(uniqueKeysWithValues: SettingsForm.proactiveSources.map { ($0, src[$0]?.boolValue ?? false) })
        if case .number(let v)? = pr["min_p"], v >= 0, v <= 1 { proactiveMinPct = Int((v * 100).rounded()) }
        else { proactiveMinPct = SettingsForm.defaultProactiveMinPct }
        if case .number(let v)? = pr["min_interval_min"], v >= 0, v <= 240 { proactiveIntervalMin = Int(v) }
        else { proactiveIntervalMin = SettingsForm.defaultProactiveInterval }
        if let q = pr["quiet_hours"], case .null = q { quietStart = ""; quietEnd = "" }
        else {
            let qo = SettingsForm.object(pr["quiet_hours"])
            if case .string(let a)? = qo["start"], case .string(let b)? = qo["end"], SettingsForm.isHHMM(a), SettingsForm.isHHMM(b) {
                quietStart = a; quietEnd = b
            } else { quietStart = SettingsForm.defaultQuietStart; quietEnd = SettingsForm.defaultQuietEnd }
        }
        respectFocus = pr["respect_focus"]?.boolValue ?? true
    }

    /// "HH:MM", 00:00-23:59 (python oi.proactive.config.hhmm).
    public static func isHHMM(_ s: String) -> Bool {
        s.range(of: #"^([01][0-9]|2[0-3]):[0-5][0-9]$"#, options: .regularExpression) != nil
    }

    /// Writes the exposed keys into `s`; every other key (and unknown sub-keys of gate / claude / shots / hud)
    /// is kept as it is in `s`. With a `baseline` (the form as loaded / last saved), only the values that differ
    /// from it are written, so `s` = the CURRENT file keeps what another writer (menu Pause, denylist window)
    /// changed while the window was open. Call it inside `SettingsStore.update` (read-modify-write).
    public func apply(to s: inout OISettings, since baseline: SettingsForm? = nil) {
        func changed<T: Equatable>(_ k: KeyPath<SettingsForm, T>) -> Bool { baseline.map { $0[keyPath: k] != self[keyPath: k] } ?? true }
        if changed(\.autoMode) { s.hud.autoMode = autoMode }
        if changed(\.hintDotSize) { s.hud.hintDotSize = hintDotSize }
        if changed(\.hintTTLms) { s.hud.hintTTLms = hintTTLms }
        if changed(\.colors) {
            var cs = s.hud.colors.byModifier                       // per colour: another colour edited elsewhere stays
            for (m, c) in colors where baseline?.colors[m] != c { cs[m] = c }
            s.hud.colors = HUDColors(cs)
        }
        if changed(\.fillOrder) { s.hud.fillOrder = fillOrder }
        // gate / claude / shots: a key absent from the file is written only when it differs from its default,
        // so an untouched default keeps following the reader's default.
        let d = SettingsForm()
        var g = s.gate ?? [:]
        func gset<T: Equatable>(_ k: String, _ p: KeyPath<SettingsForm, T>, _ v: GateValue, _ def: GateValue) {
            if changed(p), g[k] != nil || v != def { g[k] = v }
        }
        func num(_ v: Int) -> GateValue { .number(Double(v)) }
        gset("hint_cooldown_s", \.hintCooldownS, num(hintCooldownS), num(d.hintCooldownS))
        gset("ttl_ms", \.fullTTLms, num(fullTTLms), num(d.fullTTLms))
        gset("hide_on_pointer_travel", \.pointerHide, .bool(pointerHide), .bool(d.pointerHide))
        gset("pointer_grace_ms", \.pointerGraceMs, num(pointerGraceMs), num(d.pointerGraceMs))
        gset("pointer_fast_px_s", \.pointerFastPxS, num(pointerFastPxS), num(d.pointerFastPxS))
        gset("outward", \.outwardGate, .bool(outwardGate), .bool(d.outwardGate))
        if s.gate != nil || !g.isEmpty { s.gate = g }
        func oset<T: Equatable>(_ o: inout [String: JSONValue], _ k: String, _ p: KeyPath<SettingsForm, T>, _ v: JSONValue, _ def: JSONValue) {
            if changed(p), o[k] != nil || v != def { o[k] = v }
        }
        var c = SettingsForm.object(s.claude)
        oset(&c, "enabled", \.claudeEnabled, .bool(claudeEnabled), .bool(d.claudeEnabled))
        oset(&c, "enrich", \.claudeEnrich, .bool(claudeEnrich), .bool(d.claudeEnrich))
        oset(&c, "local_only_domains", \.localOnlyDomains, .array(SettingsForm.cleanDomains(localOnlyDomains).domains.map { .string($0) }), .array([]))
        if s.claude != nil || !c.isEmpty { s.claude = .object(c) }
        var sh = SettingsForm.object(s.shots)
        oset(&sh, "enabled", \.shotsEnabled, .bool(shotsEnabled), .bool(d.shotsEnabled))
        oset(&sh, "clipboard", \.shotsClipboard, .bool(shotsClipboard), .bool(d.shotsClipboard))
        let fc = fallbackContact.trimmingCharacters(in: .whitespaces)
        oset(&sh, "fallback_contact", \.fallbackContact, fc.isEmpty ? .null : .string(fc), .string(SettingsForm.defaultFallbackContact))
        if s.shots != nil || !sh.isEmpty { s.shots = .object(sh) }
        if changed(\.paused) { s.paused = paused }
        if changed(\.denylistApps) { s.denylistApps = OISettings.cleanDenylist(denylistApps) }
        // W24: proactive — per source, then the gate keys; the other keys of `proactive` stay as they are
        var pr = SettingsForm.object(s.extra["proactive"])
        if changed(\.proactiveSources) {
            var src = SettingsForm.object(pr["sources"])
            for k in SettingsForm.proactiveSources where baseline?.proactiveSources[k] != proactiveSources[k] || baseline == nil {
                let v = proactiveSources[k] ?? false
                if src[k] != nil || v { src[k] = .bool(v) }
            }
            if pr["sources"] != nil || !src.isEmpty { pr["sources"] = .object(src) }
        }
        oset(&pr, "min_p", \.proactiveMinPct, .number(Double(proactiveMinPct) / 100), .number(Double(d.proactiveMinPct) / 100))
        oset(&pr, "min_interval_min", \.proactiveIntervalMin, .number(Double(proactiveIntervalMin)), .number(Double(d.proactiveIntervalMin)))
        let qs = quietStart.trimmingCharacters(in: .whitespaces), qe = quietEnd.trimmingCharacters(in: .whitespaces)
        let quiet: JSONValue = qs.isEmpty && qe.isEmpty ? .null : .object(["start": .string(qs), "end": .string(qe)])
        if changed(\.quietStart) || changed(\.quietEnd) {
            let def: JSONValue = .object(["start": .string(d.quietStart), "end": .string(d.quietEnd)])
            if pr["quiet_hours"] != nil || quiet != def { pr["quiet_hours"] = quiet }
        }
        oset(&pr, "respect_focus", \.respectFocus, .bool(respectFocus), .bool(d.respectFocus))
        if s.extra["proactive"] != nil || !pr.isEmpty { s.extra["proactive"] = .object(pr) }
    }

    /// Live validation, per section (empty = can be saved).
    public func problems() -> [Section: [String]] {
        var p: [Section: [String]] = [:]
        func range(_ v: Int, _ r: ClosedRange<Int>, _ what: String, _ unit: String, _ sec: Section) {
            if !r.contains(v) { p[sec, default: []].append("\(what) must be \(r.lowerBound)–\(r.upperBound) \(unit) (is \(v)).") }
        }
        if !HUDSettings.autoModes.contains(autoMode) { p[.hud, default: []].append("HUD mode must be hints, full or off.") }
        range(hintDotSize, Self.hintDotSizeRange, "Hint dot size", "pt", .hud)
        range(hintTTLms, Self.hintTTLRange, "Hint duration", "ms", .timings)
        range(hintCooldownS, Self.hintCooldownRange, "Hint cooldown", "s", .timings)
        range(fullTTLms, Self.fullTTLRange, "Full-cross duration", "ms", .timings)
        range(pointerGraceMs, Self.pointerGraceRange, "Pointer grace", "ms", .pointer)
        range(pointerFastPxS, Self.pointerSpeedRange, "Pointer speed", "px/s", .pointer)
        for m in TapModifier.optionTaps {
            let c = colors[m] ?? HUDColors.defaults[m]!
            if HUDColor.normalised(c.light) == nil || HUDColor.normalised(c.dark) == nil {
                p[.colors, default: []].append("\(m.symbol) colour must be #RRGGBB.")
            }
        }
        if !HUDSettings.validFillOrder(fillOrder) {
            p[.fillOrder, default: []].append("Each of fn ⌃ ⌥ ⌘ must appear exactly once.")
        }
        let bad = SettingsForm.cleanDomains(localOnlyDomains).invalid
        if !bad.isEmpty { p[.claude, default: []].append("Not a domain: \(bad.joined(separator: ", ")).") }
        let fc = fallbackContact.trimmingCharacters(in: .whitespaces)
        if !fc.isEmpty, fc.range(of: #"^[a-z][a-z0-9_-]{0,31}$"#, options: .regularExpression) == nil {
            p[.screenshots, default: []].append("Default contact must be an agent slug (e.g. atlas) or none.")
        }
        range(proactiveMinPct, Self.proactiveMinPctRange, "Hint threshold", "%", .proactive)          // W24
        range(proactiveIntervalMin, Self.proactiveIntervalRange, "At most one hint every", "min", .proactive)
        let qs = quietStart.trimmingCharacters(in: .whitespaces), qe = quietEnd.trimmingCharacters(in: .whitespaces)
        if !(qs.isEmpty && qe.isEmpty) && !(Self.isHHMM(qs) && Self.isHHMM(qe)) {
            p[.proactive, default: []].append("Quiet hours: both HH:MM (e.g. 22:00 and 08:00), or both empty for none.")
        }
        return p
    }

    public var isValid: Bool { problems().isEmpty }

    /// One section back to its defaults (the other sections untouched).
    public mutating func reset(_ section: Section) {
        let d = SettingsForm()
        switch section {
        case .hud: autoMode = d.autoMode; hintDotSize = d.hintDotSize
        case .timings: hintTTLms = d.hintTTLms; hintCooldownS = d.hintCooldownS; fullTTLms = d.fullTTLms
        case .pointer: pointerHide = d.pointerHide; pointerGraceMs = d.pointerGraceMs; pointerFastPxS = d.pointerFastPxS
        case .colors: colors = d.colors
        case .fillOrder: fillOrder = d.fillOrder
        case .claude: claudeEnabled = d.claudeEnabled; claudeEnrich = d.claudeEnrich; localOnlyDomains = d.localOnlyDomains
        case .screenshots: shotsEnabled = d.shotsEnabled; shotsClipboard = d.shotsClipboard; fallbackContact = d.fallbackContact
        case .privacy: paused = d.paused; denylistApps = d.denylistApps; outwardGate = d.outwardGate
        case .proactive:
            proactiveSources = d.proactiveSources; proactiveMinPct = d.proactiveMinPct; proactiveIntervalMin = d.proactiveIntervalMin
            quietStart = d.quietStart; quietEnd = d.quietEnd; respectFocus = d.respectFocus
        case .keys, .vocal, .laya: break
        }
    }

    /// One colour back to its default.
    public mutating func resetColor(_ m: TapModifier) { colors[m] = HUDColors.defaults[m] }

    /// Sets rank `rank` (0-based) to `m`, swapping with the rank that had `m` (the order stays a permutation).
    public mutating func setFill(rank: Int, to m: TapModifier) {
        guard fillOrder.indices.contains(rank) else { return }
        if let j = fillOrder.firstIndex(of: m) { fillOrder.swapAt(rank, j) } else { fillOrder[rank] = m }
    }

    // MARK: helpers

    /// Same normalisation as python `claude_settings` (lower case, strip leading "@" / "."), deduplicated;
    /// `invalid` = the entries that are not a hostname with a dot.
    public static func cleanDomains(_ raw: [String]) -> (domains: [String], invalid: [String]) {
        var seen = Set<String>(); var out: [String] = []; var bad: [String] = []
        for r in raw {
            var d = r.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            while let f = d.first, f == "@" || f == "." { d.removeFirst() }
            if d.isEmpty { continue }
            guard d.range(of: #"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"#, options: .regularExpression) != nil else {
                bad.append(r.trimmingCharacters(in: .whitespacesAndNewlines)); continue
            }
            if seen.insert(d).inserted { out.append(d) }
        }
        return (out, bad)
    }

    static func object(_ v: JSONValue?) -> [String: JSONValue] {
        if case .object(let o)? = v { return o }
        return [:]
    }

    // MARK: read-only recaps

    /// "Option 2 (⌃ control): ⌃ tap" … one line per hotkey, from the configured set.
    public static func keyMap(_ h: HotkeySet) -> [(label: String, value: String)] {
        HotkeySet.actions.map { a in
            let k = h[a.key]
            return (HotkeySet.windowLabel(a.key), k.map { $0.isTap ? "\($0.display) tap" : $0.display } ?? "—")
        }
    }

    /// The vocal recipients oi vocal-send knows: the defaults (oi/vocal.py DEFAULT_RECIPIENTS, the HUD picker
    /// order), overridden per slug by settings `vocal_recipients` (null removes, a new slug is appended).
    public static let defaultRecipients: [(slug: String, name: String)] = [
        ("atlas", "Atlas"), ("nova", "Nova"), ("orion", "Orion"), ("lyra", "Lyra"), ("vega", "Vega"),
        ("juno", "Juno"), ("iris", "Iris"), ("hera", "Hera"), ("echo", "Echo"),
    ]
    public static func recipients(_ s: OISettings) -> [(slug: String, name: String)] {
        var out = defaultRecipients
        for (slug, spec) in object(s.vocalRecipients).sorted(by: { $0.key < $1.key }) {
            if case .null = spec { out.removeAll { $0.slug == slug }; continue }
            guard case .object(let o) = spec else { continue }
            let name: String? = { if case .string(let n)? = o["name"], !n.isEmpty { return n }; return nil }()
            if let i = out.firstIndex(where: { $0.slug == slug }) { if let name { out[i].name = name } }
            else { out.append((slug, name ?? slug.capitalized)) }
        }
        return out
    }

    /// Laya: `live.predictor` (markov | laya | blend, set only by `oi laya-promote`), the promoted checkpoint,
    /// and the last training (`model_trained_at` from status/oi-live.json).
    public static func laya(_ s: OISettings, live: LiveStatus?, tz: TimeZone = .current) -> (predictor: String, checkpoint: String, trained: String) {
        let p = s.live?["predictor"]?.string.flatMap { ["markov", "laya", "blend"].contains($0) ? $0 : nil } ?? "markov"
        let ck = s.live?["laya_checkpoint"]?.string ?? "stock (zero-shot)"
        var trained = "never (no status from oi live)"
        if let t = live?.model_trained_at {
            if let d = LiveStatus.date(t) {
                let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd HH:mm"; f.timeZone = tz
                trained = f.string(from: d)
            } else { trained = t }
        }
        return (p, ck, trained)
    }
}

extension JSONValue {
    var boolValue: Bool? { if case .bool(let b) = self { return b }; return nil }
    var stringsValue: [String]? {
        guard case .array(let a) = self else { return nil }
        return a.compactMap { if case .string(let s) = $0 { return s }; return nil }
    }
}
