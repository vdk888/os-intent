import Foundation

// W18 wish → action: the wish panel's text is resolved to an action by the live driver (`oi live`,
// oi/intent_resolve.py) while the user types (a preview "→ Telegram", local tier only, debounced 150 ms) and on
// Enter (all tiers: local → Laya → haiku). Pure parts: the protocol additions, the stored action of a resolved
// wish, and the panel's resolve state machine. The AppKit side is oi-hud/WishResolveController.swift.
//
// Protocol (oi-hud is the server):
//   resolver -> HUD  {"cmd":"resolver"}                       register this connection (answered `ok`); only it
//                                                              ever receives wish text
//   HUD -> resolver  {"event":"resolve","id":7,"text":"telegram","final":false,"confirmed":false,
//                     "context":{"app":"Mail","space":2}}
//   resolver -> HUD  {"cmd":"resolution","id":7,"state":"preview|confirm|done|noted|failed","label":"→ Telegram",
//                     "token":"activate:Telegram","tier":"local","target":"Telegram"}
//   tests / demo     {"cmd":"debug_wish","text":"telegram","app":"Mail","enter":true,"confirm":false}
//                    opens the panel (front app overridable), types `text`, then presses Enter (and ⇧ to confirm)

/// Debounce of the live preview while typing.
public let resolvePreviewDebounceMs = 150
/// Give up waiting for a final resolution (Claude + a Mail draft can take seconds) — the wish is then noted.
public let resolveFinalTimeoutS: Double = 45

public enum ResolveState: String, Codable, Sendable { case preview, confirm, done, noted, failed }

/// The resolver's answer (`resolution` command).
public struct Resolution: Codable, Equatable, Sendable {
    public var id: Int
    public var state: ResolveState
    public var label: String
    public var token: String?
    public var tier: String?
    public var target: String?
    /// W16: a `confirm` for a page action carries its element ("button:squash-and-merge"): the HUD issues
    /// the press ticket (PressGuard) when the user confirms, so oi-hud may AXPress it.
    public var press: String?

    public init(id: Int, state: ResolveState, label: String, token: String? = nil, tier: String? = nil,
                target: String? = nil, press: String? = nil) {
        self.id = id; self.state = state; self.label = label; self.token = token; self.tier = tier; self.target = target
        self.press = press
    }
}

/// What a resolved, executed wish did — stored in its wish row (`action`), the strong label of that moment.
public struct WishAction: Codable, Equatable, Sendable {
    public var token: String
    public var tier: String
    public var target: String?

    public init(token: String, tier: String, target: String?) { self.token = token; self.tier = tier; self.target = target }
}

/// The context sent with a request: the front app and desk when the panel opened.
public struct ResolveContext: Equatable, Sendable {
    public var app: String?
    public var space: Int?
    public init(app: String?, space: Int?) { self.app = app; self.space = space }
}

/// HUD → resolver.
public struct ResolveRequest: Equatable, Sendable {
    public var id: Int
    public var text: String
    public var final: Bool
    public var confirmed: Bool
    public var context: ResolveContext

    public init(id: Int, text: String, final: Bool, confirmed: Bool = false, context: ResolveContext) {
        self.id = id; self.text = text; self.final = final; self.confirmed = confirmed; self.context = context
    }

    public var json: [String: Any] {
        var ctx: [String: Any] = [:]
        ctx["app"] = context.app ?? NSNull()
        ctx["space"] = context.space ?? NSNull()
        return ["event": "resolve", "id": id, "text": text, "final": final, "confirmed": confirmed, "context": ctx]
    }
}

public enum W18Command: Equatable, Sendable {
    case resolver
    case resolution(Resolution)
    case debugWish(text: String, app: String?, enter: Bool, confirm: Bool)
}

private struct RawW18: Decodable {
    let text: String?
    let app: String?
    let enter: Bool?
    let confirm: Bool?
}

/// The W18 commands; nil for any other command name (the caller then reports `unknown_command`).
public func decodeW18Command(_ cmd: String, _ line: Data) throws -> W18Command? {
    switch cmd {
    case "resolver":
        return .resolver
    case "resolution":
        guard let r = try? JSONDecoder().decode(Resolution.self, from: line) else { throw CommandError.badJSON }
        return .resolution(r)
    case "debug_wish":
        guard let raw = try? JSONDecoder().decode(RawW18.self, from: line), let t = raw.text,
              WishRow.clean(t) != nil else { throw CommandError.emptyWish }
        return .debugWish(text: t, app: raw.app, enter: raw.enter ?? false, confirm: raw.confirm ?? false)
    default:
        return nil
    }
}

// MARK: - the panel's resolve state machine

public enum WishResolveEffect: Equatable, Sendable {
    case send(ResolveRequest)
    /// The live preview next to the hint ("" = none).
    case preview(String)
    /// Replace the hint line (nil = the default hint).
    case hint(String?)
    /// Show `note` for `holdMs`, then save the wish (with `action` when it was executed) and close the panel.
    case finish(action: WishAction?, note: String, holdMs: Int)
    /// W18 follow-up: the panel closed (Esc, click outside) with request `id` still open: the resolver drops it.
    case cancel(id: Int)
}

/// typing --(debounced text)--> preview request; Enter --> resolving(id) --> done | noted | failed (finish) or
/// confirm --> confirming(id, text) --(⇧ or Enter, same text)--> resolving(id, confirmed) --> finish.
/// Editing the text while confirming drops the confirm. Without a resolver connection `enter` returns nil:
/// the panel saves the wish as before (W6).
public struct WishResolveMachine: Sendable {
    public enum Phase: Equatable, Sendable { case typing, resolving(id: Int, text: String), confirming(id: Int, text: String) }

    public private(set) var phase: Phase = .typing
    private var nextId = 1
    private var lastPreviewId = 0
    private var text = ""

    public init() {}

    public mutating func reset() { phase = .typing; text = ""; lastPreviewId = 0 }

    /// W18 follow-up: the panel closes. A request still resolving (or waiting for its confirm) is cancelled,
    /// so the resolver never executes it; then the machine resets.
    public mutating func closed() -> [WishResolveEffect] {
        defer { reset() }
        switch phase {
        case .resolving(let id, _), .confirming(let id, _): return [.cancel(id: id)]
        case .typing: return []
        }
    }

    /// The field changed (typed or dictated). The caller schedules `previewDue` after the debounce.
    public mutating func textChanged(_ t: String) -> [WishResolveEffect] {
        text = t
        if case .confirming = phase { phase = .typing; return [.hint(nil)] }
        return []
    }

    public mutating func previewDue(context: ResolveContext) -> [WishResolveEffect] {
        guard phase == .typing else { return [] }
        guard let clean = WishRow.clean(text) else { lastPreviewId = 0; return [.preview("")] }
        let id = nextId; nextId += 1; lastPreviewId = id
        return [.send(ResolveRequest(id: id, text: clean, final: false, context: context))]
    }

    /// Enter. nil = not handled here (no resolver, or an empty field): the panel's own Enter applies.
    public mutating func enter(_ t: String, context: ResolveContext, connected: Bool) -> [WishResolveEffect]? {
        guard connected, let clean = WishRow.clean(t) else { return nil }
        switch phase {
        case .resolving:
            return []                                        // already working: a second Enter does nothing
        case .confirming(let id, let pending) where pending == clean:
            phase = .resolving(id: id, text: clean)
            return [.send(ResolveRequest(id: id, text: clean, final: true, confirmed: true, context: context)), .hint("Working…")]
        default:
            let id = nextId; nextId += 1
            phase = .resolving(id: id, text: clean)
            return [.send(ResolveRequest(id: id, text: clean, final: true, context: context)), .preview(""), .hint("Working…")]
        }
    }

    /// The ⇧ tap: confirms a pending irreversible action, nothing otherwise.
    public mutating func confirmKey(context: ResolveContext) -> [WishResolveEffect] {
        guard case .confirming(let id, let pending) = phase else { return [] }
        phase = .resolving(id: id, text: pending)
        return [.send(ResolveRequest(id: id, text: pending, final: true, confirmed: true, context: context)), .hint("Working…")]
    }

    public var isConfirming: Bool { if case .confirming = phase { return true }; return false }

    public mutating func receive(_ r: Resolution) -> [WishResolveEffect] {
        switch phase {
        case .typing:
            guard r.state == .preview, r.id == lastPreviewId else { return [] }
            return [.preview(r.label)]
        case .confirming:
            return []
        case .resolving(let id, let pending):
            guard r.id == id else { return [] }
            switch r.state {
            case .preview:
                return []
            case .confirm:
                phase = .confirming(id: id, text: pending)
                return [.hint(r.label)]
            case .done:
                phase = .typing
                let action = r.token.map { WishAction(token: $0, tier: r.tier ?? "?", target: r.target) }
                let multi = r.label.contains("\n")
                return [.finish(action: action, note: multi ? r.label.replacingOccurrences(of: "\n", with: " · ") : "✓ " + r.label, holdMs: multi ? 6000 : 500)]
            case .noted:
                phase = .typing
                return [.finish(action: nil, note: "noted", holdMs: 600)]
            case .failed:
                phase = .typing
                return [.finish(action: nil, note: r.label, holdMs: 1500)]
            }
        }
    }

    /// The final answer did not come in time: the wish is noted (saved as before).
    public mutating func timedOut(id: Int) -> [WishResolveEffect] {
        guard case .resolving(let rid, _) = phase, rid == id else { return [] }
        phase = .typing
        return [.finish(action: nil, note: "noted", holdMs: 600)]
    }
}
