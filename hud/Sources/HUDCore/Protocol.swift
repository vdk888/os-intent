import Foundation
import OISettings   // W16: PageLabel

/// One option shown in the HUD. `key` is the option number (1-4); its place and colour follow the
/// modifier that picks it. `hint` (W11) says what the option would act on, drawn on screen in
/// the option's colour while the HUD is visible.
public struct HUDOption: Codable, Equatable, Sendable {
    public var key: Int
    public var label: String
    public var icon: String?
    public var confidence: Double?
    public var hint: HUDHint?
    /// W14: the option has a Claude-assisted variant (a long-press, a right-click or a ⌥-click runs it).
    public var claude: Bool?
    /// W16: an irreversible page action — the first tap arms (capsule solid + "again"), a second tap
    /// within 3 s fires. `target` ("button:squash-and-merge") = the page element a press may AXPress.
    public var confirm: Bool?
    public var target: String?
    /// a sub-ring live item's own context card (an agent waiting, an unread mail), drawn while hovered.
    public var card: ContextCard?

    public init(key: Int, label: String, icon: String? = nil, confidence: Double? = nil, hint: HUDHint? = nil,
                claude: Bool? = nil, confirm: Bool? = nil, target: String? = nil, card: ContextCard? = nil) {
        self.key = key; self.label = label; self.icon = icon; self.confidence = confidence; self.hint = hint
        self.claude = claude; self.confirm = confirm; self.target = target; self.card = card
    }

    enum CodingKeys: String, CodingKey { case key, label, icon, confidence, hint, claude, confirm, target, card }

    /// key and label are required (as before); a missing, unknown or malformed `hint` is just no hint
    /// (an older or newer driver never makes a show fail).
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decode(Int.self, forKey: .key)
        label = try c.decode(String.self, forKey: .label)
        icon = try c.decodeIfPresent(String.self, forKey: .icon)
        confidence = try c.decodeIfPresent(Double.self, forKey: .confidence)
        hint = (try? c.decodeIfPresent(HUDHint.self, forKey: .hint)) ?? nil
        claude = (try? c.decodeIfPresent(Bool.self, forKey: .claude)) ?? nil
        confirm = (try? c.decodeIfPresent(Bool.self, forKey: .confirm)) ?? nil      // W16
        target = (try? c.decodeIfPresent(String.self, forKey: .target)) ?? nil
        card = (try? c.decodeIfPresent(ContextCard.self, forKey: .card)) ?? nil   // malformed = no card
    }

    public var offersClaude: Bool { claude == true }
    /// W16: an irreversible page action (whatever the driver said): its press needs the outward gate's confirmed
    /// ticket (no two-tap any more: the gate panel is the one confirmation).
    public var needsConfirm: Bool {
        confirm == true || (PageLabel.target(target).map { PageLabel.irreversible(slug: $0.slug) } ?? false)
    }
}

/// W11 target hint, sent by `oi live` per option:
/// `{"kind":"window","app":"Telegram"}` (app.open; url.open → Safari): the app's frontmost window glows;
/// `{"kind":"edge","dir":"right"|"left"}` (space.move +1 / −1): a bar bounces on that screen edge.
public enum HUDHint: Codable, Equatable, Sendable {
    case window(app: String)
    case edge(dir: EdgeDir)
    /// W16: `{"kind":"element","rect":[x,y,w,h]}` (CG global coordinates): outline the page button.
    case element(rect: CGRect)

    public enum EdgeDir: String, Codable, Sendable { case left, right }

    enum CodingKeys: String, CodingKey { case kind, app, dir, rect }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        switch try c.decode(String.self, forKey: .kind) {
        case "window":
            let app = try c.decode(String.self, forKey: .app).trimmingCharacters(in: .whitespaces)
            guard !app.isEmpty else { throw DecodingError.dataCorruptedError(forKey: .app, in: c, debugDescription: "empty app") }
            self = .window(app: app)
        case "edge":
            self = .edge(dir: try c.decode(EdgeDir.self, forKey: .dir))
        case "element":   // W16
            let r = try c.decode([Double].self, forKey: .rect)
            guard r.count == 4, r[2] > 0, r[3] > 0, r.allSatisfy(\.isFinite) else {
                throw DecodingError.dataCorruptedError(forKey: .rect, in: c, debugDescription: "bad rect")
            }
            self = .element(rect: CGRect(x: r[0], y: r[1], width: r[2], height: r[3]))
        default:
            throw DecodingError.dataCorruptedError(forKey: .kind, in: c, debugDescription: "unknown hint kind")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .window(let app): try c.encode("window", forKey: .kind); try c.encode(app, forKey: .app)
        case .edge(let dir): try c.encode("edge", forKey: .kind); try c.encode(dir, forKey: .dir)
        case .element(let r):
            try c.encode("element", forKey: .kind)
            try c.encode([Double(r.minX), Double(r.minY), Double(r.width), Double(r.height)], forKey: .rect)
        }
    }
}

/// Where an option's icon comes from: `app:/Applications/X.app` or `sf:symbol.name`.
public enum HUDIcon: Equatable, Sendable {
    case app(path: String)
    case sf(name: String)
    case initial(String)            // W12: an agent's initial in a disc of the key's colour (`initial:T`)
    case none

    public init(_ raw: String?) {
        guard let raw, !raw.isEmpty else { self = .none; return }
        if raw.hasPrefix("app:") { self = .app(path: String(raw.dropFirst(4))) }
        else if raw.hasPrefix("sf:") { self = .sf(name: String(raw.dropFirst(3))) }
        else if raw.hasPrefix("initial:"), let c = raw.dropFirst(8).first { self = .initial(String(c)) }
        else { self = .none }
    }
}

public enum HUDCommand: Equatable, Sendable {
    case show(options: [HUDOption], ttlMs: Int)
    case hide
    case state      // debug: what is on screen + the wish hotkey (no wish text, ever)
    case wish(text: String, source: WishSource)   // save a wish without the panel (scripts, tests)
    case debugSummon   // W10: run the summon path exactly as the summon tap would (tests, demo; no key needed)
    /// W12: the vocal key without a key. `pick` nil = open the picker (or the next page); `pick` + `text`
    /// = pick that agent and send `text` as the transcript (no microphone; tests and demo).
    case debugVocal(pick: String?, text: String?)
    /// W22: "Ask <agent>" from a starter-catalogue option: open the recording panel addressed to that agent
    /// (the microphone, exactly as after a pick in the picker; Enter / the vocal key sends, Esc cancels).
    case vocalTo(slug: String)
    /// W14: upgrade labels in place (label enrichment), see `LabelUpdate`.
    case update([LabelUpdate])
    /// W14: the outcome of a Claude-assisted press: `text` (≤ 4 lines) is shown as a card for `ttlMs`; no text =
    /// hide. Answered `ok` when the HUD still waited, `dropped: cancelled` otherwise (python then does nothing).
    /// `openDraft` = the id of a reply draft saved in place: the card offers « Open in Mail » (on press only).
    case result(key: Int, ok: Bool, text: String?, ttlMs: Int, openDraft: Int? = nil)
    /// W14: press option `key` as its key would (`mode` "claude" = the long-press) — tests and demo, no key needed.
    case debugPress(key: Int, mode: String?)
    /// W15: the follow-ups of the option at `path` (the answer to an `expand` event), drawn as a sub-ring
    /// around that capsule while the ⌥⌘ glide lasts. 0 options = nothing to offer (the path is a leaf).
    case subring(path: [Int], options: [HUDOption])
    /// Context cards: what each top option is about, drawn next to the direction the glide is on (level 2
    /// small, level 3 bigger with the summary / excerpt / why). Merged by key; see ContextCard.swift.
    case context([ContextCard])
    /// W15: a glide without keys (tests, demo): hold, rest on each prefix of `path` until its sub-ring opens,
    /// then release over `path` (`cancel`: release on empty space instead).
    case debugGlide(path: [Int], cancel: Bool)
    /// W18: wish → action (resolver registration, resolutions, debug_wish), see WishResolve.swift.
    case w18(W18Command)
    /// a `show` bound to a LIVE context state (`"live":"shot"`: a screenshot still on screen). Not
    /// rate-limited and not dismissed by typing or pointer travel while that state is live (he is
    /// working on the screenshot); oi live takes it down (`hide`) when the state ends. W17's `shot_show` (the agent
    /// picker) decodes to it too. The editable note panel and its debug submit: ShotOffer.swift.
    case showLive(options: [HUDOption], ttlMs: Int, live: String)
    case shotNote(ShotNoteRequest)
    case debugNote(text: String?)
    // W19: `show` with `"mode":"hint"`: dots only until the summon key develops them (`ttlMs` = the
    // full cross's ttl after development, `hintTTLms` = how long the dots stay).
    // W24: `"marker":"ring"` = a PROACTIVE hint (the state changed: a mail, an agent, a file, an alert):
    // the same dots with a thin ring around them. Absent / unknown = nil (the W19 dots as before).
    case showHint(options: [HUDOption], ttlMs: Int, hintTTLms: Int, marker: HintMarker?)
    /// Outward gate: the confirm panel before a send / an outward page press (OutwardGate.swift).
    case confirmOutward(OutwardConfirmRequest)
    case debugOutward(validate: Bool)
    // end W19
}

public enum CommandError: Error, Equatable, CustomStringConvertible {
    case badJSON
    case unknownCommand(String)
    case optionCount(Int)
    case badKey(Int)
    case duplicateKey(Int)
    case badTTL(Int)
    case emptyWish
    case unknownAgent(String)
    case badUpdate
    case badPath

    public var description: String {
        switch self {
        case .badJSON: return "bad_json"
        case .unknownCommand(let c): return "unknown_command:\(c)"
        case .optionCount(let n): return "option_count:\(n) (need 0-4)"
        case .badKey(let k): return "bad_key:\(k) (need 1-4)"
        case .duplicateKey(let k): return "duplicate_key:\(k)"
        case .badTTL(let t): return "bad_ttl_ms:\(t)"
        case .emptyWish: return "empty_wish"
        case .unknownAgent(let a): return "unknown_agent:\(a)"
        case .badUpdate: return "bad_update"
        case .badPath: return "bad_path"
        }
    }
}

public let defaultTTLMs = 6000    // W19: was 15000 (W9), the user: disappear faster (= settings gate.ttl_ms default)
public let maxOptions = 4

private struct RawCommand: Decodable {
    let cmd: String
    let options: [HUDOption]?
    let ttl_ms: Int?
    let text: String?
    let source: String?
    let pick: String?
    let key: Int?
    let ok: Bool?
    let mode: String?
    let path: [Int]?
    let cancel: Bool?
    let hint_ttl_ms: Int?   // W19
    let marker: String?     // W24
    let live: String?       // 
    let open_draft: Int?
}

private struct RawUpdate: Decodable { let options: [LabelUpdate] }
private struct RawContext: Decodable { let cards: [ContextCard] }

/// Decode one newline-delimited JSON command line. Options are returned sorted by key.
public func decodeCommand(_ line: Data) throws -> HUDCommand {
    let raw: RawCommand
    do { raw = try JSONDecoder().decode(RawCommand.self, from: line) } catch { throw CommandError.badJSON }
    switch raw.cmd {
    case "hide":
        return .hide
    case "shot_show", "shot_note", "debug_note":   // W17
        return try decodeShotCommand(raw.cmd, line)
    case "confirm_outward", "debug_outward":       // outward gate
        return try decodeOutwardCommand(raw.cmd, line)
    case "update":
        guard let u = try? JSONDecoder().decode(RawUpdate.self, from: line), !u.options.isEmpty else { throw CommandError.badUpdate }
        return .update(u.options)
    case "result":
        guard let k = raw.key, (1...maxOptions).contains(k) else { throw CommandError.badKey(raw.key ?? 0) }
        let ttl = raw.ttl_ms ?? 8000
        guard (100...60_000).contains(ttl) else { throw CommandError.badTTL(ttl) }
        return .result(key: k, ok: raw.ok ?? true, text: raw.text, ttlMs: ttl,
                       openDraft: raw.open_draft.flatMap { $0 > 0 ? $0 : nil })
    case "debug_press":
        guard let k = raw.key, (1...maxOptions).contains(k) else { throw CommandError.badKey(raw.key ?? 0) }
        return .debugPress(key: k, mode: raw.mode)
    case "subring":
        // the expanded path: 1-3 keys (top option 1-4, then sub-option keys 1-4)
        guard let p = raw.path, (1...GlideState.maxDepth).contains(p.count), p.allSatisfy({ (1...maxOptions).contains($0) })
        else { throw CommandError.badPath }
        let opts = raw.options ?? []
        guard opts.count <= maxOptions else { throw CommandError.optionCount(opts.count) }
        var seen = Set<Int>()
        for o in opts {
            guard (1...maxOptions).contains(o.key) else { throw CommandError.badKey(o.key) }
            guard seen.insert(o.key).inserted else { throw CommandError.duplicateKey(o.key) }
        }
        return .subring(path: p, options: opts.sorted { $0.key < $1.key })
    case "context":   // 1-4 cards, one per top option key
        guard let c = try? JSONDecoder().decode(RawContext.self, from: line) else { throw CommandError.badJSON }
        guard (1...maxOptions).contains(c.cards.count) else { throw CommandError.optionCount(c.cards.count) }
        var seen = Set<Int>()
        for card in c.cards {
            guard (1...maxOptions).contains(card.key) else { throw CommandError.badKey(card.key) }
            guard seen.insert(card.key).inserted else { throw CommandError.duplicateKey(card.key) }
        }
        return .context(c.cards.sorted { $0.key < $1.key })
    case "debug_glide":
        guard let p = raw.path, (1...GlideState.maxDepth + 1).contains(p.count), p.allSatisfy({ (1...maxOptions).contains($0) })
        else { throw CommandError.badPath }
        return .debugGlide(path: p, cancel: raw.cancel ?? false)
    case "state":
        return .state
    case "debug_summon":
        return .debugSummon
    case "debug_vocal":
        guard let p = raw.pick else { return .debugVocal(pick: nil, text: nil) }
        guard VocalPicker.agents.contains(where: { $0.slug == p }) else { throw CommandError.unknownAgent(p) }
        return .debugVocal(pick: p, text: raw.text)
    case "vocal_to":   // W22
        guard let p = raw.pick, VocalPicker.agents.contains(where: { $0.slug == p }) else {
            throw CommandError.unknownAgent(raw.pick ?? "")
        }
        return .vocalTo(slug: p)
    case "wish":
        guard let t = raw.text, WishRow.clean(t) != nil else { throw CommandError.emptyWish }
        return .wish(text: t, source: WishSource(rawValue: raw.source ?? "typed") ?? .typed)
    case "show":
        // W10: 0 options is allowed (a summon the model has nothing for: the wish option alone).
        guard let opts = raw.options else { throw CommandError.optionCount(0) }
        guard (0...maxOptions).contains(opts.count) else { throw CommandError.optionCount(opts.count) }
        var seen = Set<Int>()
        for o in opts {
            guard (1...maxOptions).contains(o.key) else { throw CommandError.badKey(o.key) }
            guard seen.insert(o.key).inserted else { throw CommandError.duplicateKey(o.key) }
        }
        let ttl = raw.ttl_ms ?? defaultTTLMs
        guard (100...60_000).contains(ttl) else { throw CommandError.badTTL(ttl) }
        // W19: "mode":"hint" = dots only; "full", absent or unknown = the cross (as before)
        if raw.mode == "hint" {
            let h = raw.hint_ttl_ms ?? defaultHintTTLMs
            guard (100...60_000).contains(h) else { throw CommandError.badTTL(h) }
            return .showHint(options: opts.sorted { $0.key < $1.key }, ttlMs: ttl, hintTTLms: h,
                             marker: raw.marker.flatMap(HintMarker.init(rawValue:)))   // W24
        }
        // end W19
        if let live = raw.live, !live.isEmpty, live.count <= 32 {   // bound to a live context state
            return .showLive(options: opts.sorted { $0.key < $1.key }, ttlMs: ttl, live: live)
        }
        return .show(options: opts.sorted { $0.key < $1.key }, ttlMs: ttl)
    default:
        if let c = try decodeW18Command(raw.cmd, line) { return .w18(c) }   // W18
        throw CommandError.unknownCommand(raw.cmd)
    }
}

/// Reply to `{"cmd":"state"}`: the options currently drawn (predicted + the wish option),
/// whether the wish hotkey is registered, and whether the wish panel is open.
public struct HUDStateInfo: Equatable, Sendable {
    public var visible: Bool
    public var options: [HUDOption]
    public var hudEnabled: Bool
    public var wishHotkey: String
    public var wishRegistered: Bool
    public var wishPanel: String          // "closed" | "typing" | "dictating"
    public var tapsActive: Bool           // W9 per-show option tap exists (must be false whenever !visible)
    public var summonKey: String          // W10: "⌥⌘" (W11 default), "⇧" or the chord
    public var summonArmed: Bool          // W10: the permanent summon tap / chord is live
    public var hints: [DrawnHint]         // W11: the target hints drawn now (empty whenever !visible)
    public var vocalKey: String           // W12: "⌃⌥" (default) or the chord
    public var vocal: String              // W12: "idle" | "picking:<page 1-3>" | "recording:<slug>"
    public var claude: String             // W14: "idle" | "working:<key>" | "card:<key>"
    public var glide: String              // W15: "idle" | "gliding:<hovered path, e.g. 2.1 or ->"; rings <n>"
    public var hint: String = "idle"      // W19: "idle" | "dots:<keys>" | "faded" (see HintLife.stateName)

    public init(visible: Bool, options: [HUDOption], hudEnabled: Bool, wishHotkey: String,
                wishRegistered: Bool, wishPanel: String, tapsActive: Bool = false,
                summonKey: String = "", summonArmed: Bool = false, hints: [DrawnHint] = [],
                vocalKey: String = "", vocal: String = "idle", claude: String = "idle", glide: String = "idle") {
        self.visible = visible; self.options = options; self.hudEnabled = hudEnabled
        self.wishHotkey = wishHotkey; self.wishRegistered = wishRegistered; self.wishPanel = wishPanel
        self.tapsActive = tapsActive; self.summonKey = summonKey; self.summonArmed = summonArmed
        self.hints = hints; self.vocalKey = vocalKey; self.vocal = vocal; self.claude = claude; self.glide = glide
    }
}

/// Events written back on the connection that sent the command (one JSON object per line).
/// `wish` is broadcast to EVERY connected client when a wish is saved (its length, never the text);
/// `summon` (W10) is broadcast to every client when the summon key is tapped (`ts` = ISO-8601 UTC).
public enum HUDEvent: Equatable, Sendable {
    /// `via` (the user 20/10 16:5x): "key" (a tap / chord) or "click" (the capsule clicked); nil = not said.
    /// `mode` (W14): "claude" = the long-press / right-click / ⌥-click (the Claude-assisted variant); nil = plain.
    /// `path` + `glide` (W15): the option executed by a glide release (or a click on a sub-ring), top key first;
    /// `glide` = the glide log (visited paths and dwell times, sub-rings opened, depth). `key` = path[0].
    case pressed(key: Int, via: String? = nil, mode: String? = nil, path: [Int]? = nil, glide: GlideLog? = nil)
    case dismissed(reason: String, glide: GlideLog? = nil)
    /// W15: to the show's owner: the pointer rested `dwell_ms` on the option at `path`: send its `subring`.
    case expand(path: [Int])
    /// W15: to the show's owner: a ⌥⌘ glide started over its options (the driver prefetches level 1).
    case glideStart
    /// W19: to the show's owner: its hint was developed into the full cross by the summon key.
    case hintExpanded
    case dropped(reason: String)
    case ok
    case error(String)
    case wish(textLen: Int)
    case summon(ts: String)
    case state(HUDStateInfo)
    /// W18: a wish resolve request, sent to the registered resolver connection only (never broadcast).
    case resolve(ResolveRequest)
    /// W17: the note panel sent with Enter (the reply to `shot_note`).
    case note(text: String)
    /// W18 follow-up: `{"event":"cancel","id":n}` to the resolver — Esc while "Working…" / confirming.
    case resolveCancel(id: Int)
    /// Outward gate: the answer to `confirm_outward` (exactly one per request).
    case outward(id: String, decision: OutwardDecision)

    public func jsonLine() -> String {
        let obj: [String: Any]
        switch self {
        case .pressed(let k, let via, let mode, let path, let glide):
            var o: [String: Any] = ["event": "pressed", "key": k]
            if let via { o["via"] = via }
            if let mode { o["mode"] = mode }
            if let path { o["path"] = path }
            if let glide { o["glide"] = glide.json }
            obj = o
        case .dismissed(let r, let glide):
            var o: [String: Any] = ["event": "dismissed", "reason": r]
            if let glide { o["glide"] = glide.json }
            obj = o
        case .expand(let path): obj = ["event": "expand", "path": path]
        case .glideStart: obj = ["event": "glide", "phase": "start"]
        case .hintExpanded: obj = ["event": "hint", "phase": "expanded"]   // W19
        case .dropped(let r): obj = ["event": "dropped", "reason": r]
        case .ok: obj = ["event": "ok"]
        case .error(let m): obj = ["event": "error", "message": m]
        case .wish(let n): obj = ["event": "wish", "text_len": n]
        case .summon(let ts): obj = ["event": "summon", "ts": ts]
        case .resolve(let r): obj = r.json                                 // W18
        case .note(let t): obj = ["event": "note", "text": t]   // W17
        case .resolveCancel(let id): obj = ["event": "cancel", "id": id]   // W18 follow-up
        case .outward(let id, let d):                                      // outward gate
            var o: [String: Any] = ["event": "outward", "id": id, "decision": d.validated ? "validated" : "cancelled",
                                    "reason": d.reason]
            if d.attachShot {
                o["attach_shot"] = true
                if let n = d.display { o["display"] = n }
            }
            obj = o
        case .state(let i):
            obj = ["event": "state", "visible": i.visible, "hud_enabled": i.hudEnabled,
                   "wish_hotkey": i.wishHotkey, "wish_registered": i.wishRegistered, "wish_panel": i.wishPanel,
                   "taps_active": i.tapsActive, "summon_key": i.summonKey, "summon_armed": i.summonArmed,
                   "options": i.options.map { ["key": $0.key, "label": $0.label] as [String: Any] },
                   "hints": i.hints.map { ["key": $0.key, "drawn": $0.drawn.rawValue] as [String: Any] },
                   "vocal_key": i.vocalKey, "vocal": i.vocal, "claude": i.claude, "glide": i.glide,
                   "hint": i.hint,   // W19
                   "agents": VocalPicker.agents.map(\.name)]   // demo mode: the picker's roster (names only)
        }
        let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
        return String(decoding: data, as: UTF8.self) + "\n"
    }
}
