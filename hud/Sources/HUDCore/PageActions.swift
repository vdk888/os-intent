import Foundation
import CoreGraphics
import OISettings

// W16 page actions, pure logic (the AX reading and pressing is oi-hud PageAX.swift):
// - the page snapshot sent to `oi live` (buttons / links of Safari's front page: role, label, frame);
// - the two socket commands `page` and `ax_press` (handled off the main thread, outside HUDCommand);
// - re-finding an element by role + label slug at press time (never a stored coordinate);
// - irreversible labels press only with the outward gate's confirmed ticket (no two-tap: the panel confirms);
// - the press guard: oi-hud presses only with a fresh, single-use ticket from the user's own tap on that option.

// MARK: - snapshot

/// One actionable element of the page: token role ("button" | "link"), its label (AXTitle / AXDescription /
/// its own text, ≤ 80 chars), the label slug, and its frame in CG global coordinates (y down).
public struct PageElement: Equatable, Sendable {
    public var role: String
    public var label: String
    public var slug: String
    public var frame: CGRect

    public init(role: String, label: String, frame: CGRect) {
        self.role = role
        self.label = String(label.prefix(80))
        self.slug = PageLabel.slug(label)
        self.frame = frame
    }

    var json: [String: Any] {
        ["role": role, "label": label, "slug": slug,
         "frame": [frame.minX, frame.minY, frame.width, frame.height].map { ($0 * 10).rounded() / 10 }]
    }
}

/// The reply to `{"cmd":"page"}`. `trusted` false (no Accessibility for oi-hud) = no elements, reason
/// "not_trusted"; Safari not in front = "not_safari"; no web area = "no_page".
public struct PageSnapshot: Equatable, Sendable {
    public var trusted: Bool
    public var app: String?
    public var host: String?
    public var elements: [PageElement]
    public var reason: String?
    public var ms: Int

    public init(trusted: Bool, app: String? = nil, host: String? = nil, elements: [PageElement] = [],
                reason: String? = nil, ms: Int = 0) {
        self.trusted = trusted; self.app = app; self.host = host; self.elements = elements; self.reason = reason
        self.ms = ms
    }

    public func jsonLine() -> String {
        var o: [String: Any] = ["event": "page", "trusted": trusted, "elements": elements.map(\.json), "ms": ms]
        if let app { o["app"] = app }
        if let host { o["host"] = host }
        if let reason { o["reason"] = reason }
        return pageJSONLine(o)
    }
}

/// Host of a page URL (lower-case, with a non-default port as "host:port"), nil for non-web URLs.
public func pageHost(_ url: URL?) -> String? {
    guard let url, let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
          let h = url.host?.lowercased(), !h.isEmpty else { return nil }
    return url.port.map { "\(h):\($0)" } ?? h
}

// MARK: - commands (outside HUDCommand: answered off the main thread)

public enum PageCommand: Equatable, Sendable {
    case page
    /// Re-find by role + slug in the front page and AXPress it (`dryRun`: find only, never press).
    case press(role: String, slug: String, host: String?, dryRun: Bool)
}

private struct RawPageCommand: Decodable {
    let cmd: String
    let role: String?
    let slug: String?
    let host: String?
    let dry_run: Bool?
}

/// nil = not a page command (the line goes to the normal decoder); a malformed `ax_press` is `.failure`.
public func decodePageCommand(_ line: Data) -> Result<PageCommand, PressOutcome>? {
    guard let raw = try? JSONDecoder().decode(RawPageCommand.self, from: line) else { return nil }
    switch raw.cmd {
    case "page":
        return .success(.page)
    case "ax_press":
        guard let role = raw.role, let slug = raw.slug, let t = PageLabel.target("\(role):\(slug)") else {
            return .failure(PressOutcome(ok: false, reason: "bad_target"))
        }
        return .success(.press(role: t.role, slug: t.slug, host: raw.host?.lowercased(), dryRun: raw.dry_run ?? false))
    default:
        return nil
    }
}

/// The reply to `ax_press`: `{"event":"ax_press","ok":…,"reason":…,"dry_run":…}`. Reasons: not_trusted,
/// not_safari, no_page, host_changed, gone (element not on the page any more), no_tap (no fresh ticket from
/// The user's tap), not_confirmed (irreversible without the second tap), press_failed (AX error), bad_target.
public struct PressOutcome: Error, Equatable, Sendable {
    public var ok: Bool
    public var reason: String?
    public var dryRun: Bool

    public init(ok: Bool, reason: String? = nil, dryRun: Bool = false) {
        self.ok = ok; self.reason = reason; self.dryRun = dryRun
    }

    public func jsonLine() -> String {
        var o: [String: Any] = ["event": "ax_press", "ok": ok, "dry_run": dryRun]
        if let reason { o["reason"] = reason }
        return pageJSONLine(o)
    }

    /// The toast shown when nothing was pressed (nil = say nothing: success or a dry run).
    public var toast: String? {
        guard !ok else { return nil }
        switch reason {
        case "gone", "host_changed", "no_page", "not_safari": return "Page changed — nothing pressed"
        case "not_trusted": return "Needs Accessibility for oi-hud — nothing pressed"
        default: return "Nothing pressed"
        }
    }
}

func pageJSONLine(_ o: [String: Any]) -> String {
    let data = (try? JSONSerialization.data(withJSONObject: o, options: [.sortedKeys, .withoutEscapingSlashes])) ?? Data("{}".utf8)
    return String(decoding: data, as: UTF8.self) + "\n"
}

// MARK: - re-find

/// The element to press: same role and label slug; among several, the first one whose frame is visible in
/// `viewport` (when given), else the first in page order. nil = gone.
public func refind<T>(role: String, slug: String, in items: [T], element: (T) -> PageElement,
                      viewport: CGRect? = nil) -> T? {
    let hits = items.filter { let e = element($0); return e.role == role && e.slug == slug }
    if let vp = viewport, let v = hits.first(where: { let f = element($0).frame; return f.width > 0 && f.height > 0 && vp.intersects(f) }) {
        return v
    }
    return hits.first
}

// MARK: - press guard

/// oi-hud presses a page element only for a fresh ticket issued by the user's own tap on the option that
/// targets it ("button:squash-and-merge"): single use, valid `ttl` (5 s: `oi live` asks right after the
/// `pressed` event); an irreversible label only with a CONFIRMED ticket, which only the outward gate's validate
/// issues.
/// A dry run needs no ticket (it never presses).
public struct PressGuard: Sendable {
    public static let ttl: TimeInterval = 5
    private var ticket: (target: String, confirmed: Bool, at: TimeInterval)?

    public init() {}

    public mutating func issue(target: String, confirmed: Bool, at t: TimeInterval) {
        ticket = (target, confirmed, t)
    }

    /// W18 follow-up: Esc on a confirmed page-action wish still resolving: its ticket must not outlive the cancel.
    public mutating func revoke(target: String) {
        if ticket?.target == target { ticket = nil }
    }

    /// nil = allowed (and the ticket is used up); else the refusal reason.
    public mutating func redeem(role: String, slug: String, at t: TimeInterval) -> String? {
        guard let k = ticket, k.target == "\(role):\(slug)", t - k.at <= Self.ttl, t >= k.at else { return "no_tap" }
        ticket = nil
        if PageLabel.irreversible(slug: slug), !k.confirmed { return "not_confirmed" }
        return nil
    }
}
