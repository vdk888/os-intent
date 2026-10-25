import Foundation

// Context cards: while ⌥⌘ glides on a direction, a card says WHAT it is about.
// Level 2 (its sub-ring is open, or the dwell on it completed): a small card, the « what » in one line (local).
// Level 3 (the pointer went one level deeper): a bigger card, + the 3-4 line summary, the key excerpt and the wiki
// page + snippet that made the event important. Sent by `oi live` at hold start: {"cmd":"context","cards":[…]}
// (again with the summary when it is ready). Pure parts here: the card, the level, the size, the placement.

/// Why the proactive judge found the event important: the wiki page and its snippet.
public struct ContextWhy: Codable, Hashable, Sendable {
    public var page: String
    public var snippet: String?
    public init(page: String, snippet: String? = nil) { self.page = page; self.snippet = snippet }

    /// "acme" from "atlas_rnd/clients/acme.md" (the page's name, not its path).
    public var pageName: String {
        let last = page.split(separator: "/").last.map(String.init) ?? page
        return last.hasSuffix(".md") ? String(last.dropLast(3)) : last
    }
}

public struct ContextCard: Codable, Hashable, Sendable {
    public static let maxSummaryLines = 4
    public var key: Int
    public var what: String
    public var summary: [String]
    public var excerpt: String?
    public var why: ContextWhy?
    public var pending: Bool          // the summary is being prepared (level 3 says so)

    public init(key: Int, what: String, summary: [String] = [], excerpt: String? = nil, why: ContextWhy? = nil,
                pending: Bool = false) {
        self.key = key; self.what = what; self.summary = summary; self.excerpt = excerpt; self.why = why
        self.pending = pending
    }

    enum CodingKeys: String, CodingKey { case key, what, summary, excerpt, why, pending }

    /// key + a non-empty `what` are required; the rest is optional and tolerant (a malformed field = absent).
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decode(Int.self, forKey: .key)
        what = try c.decode(String.self, forKey: .what).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !what.isEmpty else { throw DecodingError.dataCorruptedError(forKey: .what, in: c, debugDescription: "empty what") }
        let lines = (try? c.decodeIfPresent([String].self, forKey: .summary)) ?? nil
        summary = Array((lines ?? []).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
            .prefix(ContextCard.maxSummaryLines))
        excerpt = ((try? c.decodeIfPresent(String.self, forKey: .excerpt)) ?? nil).flatMap { $0.isEmpty ? nil : $0 }
        why = ((try? c.decodeIfPresent(ContextWhy.self, forKey: .why)) ?? nil).flatMap { $0.page.isEmpty ? nil : $0 }
        pending = ((try? c.decodeIfPresent(Bool.self, forKey: .pending)) ?? nil) ?? false
    }

    /// Something more than the « what » to show at level 3 (else level 3 stays the small card).
    public var hasDetail: Bool { !summary.isEmpty || excerpt != nil || why != nil || pending }

    /// The level actually drawn: 3 only with detail.
    public func shownLevel(_ level: Int) -> Int { level >= 3 && hasDetail ? 3 : 2 }

    /// The card's size at `level` (an estimate from its lines; the view uses the same width and wraps inside it).
    public func size(level: Int) -> (width: Double, height: Double) {
        if shownLevel(level) == 2 { return (ContextCardLayout.smallWidth, 30) }
        // level 3 is read, not glanced at: wider card, bigger type, lines wrap (estimate by characters)
        func rows(_ t: String, perRow: Double, max: Int) -> Double { Double(min(max, Swift.max(1, Int((Double(t.count) / perRow).rounded(.up))))) }
        var h = 16.0 + 22                                         // padding + the « what » line
        for line in summary { h += rows(line, perRow: 62, max: 2) * 17 }
        if summary.isEmpty, pending { h += 17 }                   // "Preparing the summary…"
        if let ex = excerpt { h += 6 + rows(ex, perRow: 66, max: 4) * 16 }
        if let why { h += 6 + rows(why.pageName + " — " + (why.snippet ?? ""), perRow: 70, max: 3) * 15 }
        return (ContextCardLayout.bigWidth, h)
    }

    /// The cards of a `context` command merged into the ones held (by key; a newer card replaces the older).
    public static func merge(_ held: [Int: ContextCard], _ new: [ContextCard]) -> [Int: ContextCard] {
        var out = held
        for c in new { out[c.key] = c }
        return out
    }
}

public enum ContextCardLevel {
    /// The direction the glide is on and the card level: the hovered top key (else the open ring's root), level =
    /// 1 + the deepest path of that direction whose dwell completed or whose ring is open, at most 3. nil = no card
    /// (nothing rested on yet).
    public static func current(hover: [Int]?, rings: [SubRing], dwelt: [Int]?) -> (key: Int, level: Int)? {
        guard let key = hover?.first ?? rings.last?.parent.first else { return nil }
        var depth = rings.filter { $0.parent.first == key }.map(\.parent.count).max() ?? 0
        if let d = dwelt, d.first == key { depth = max(depth, d.count) }
        guard depth >= 1 else { return nil }
        return (key, min(GlideState.maxDepth, depth + 1))
    }

    /// the hovered sub-ring item that carries its own card (a live item of an app direction): its path and
    /// level (2 on hover; 3 once its dwell completed or its own ring is open). nil = not on such an item.
    public static func item(hover: [Int]?, rings: [SubRing], dwelt: [Int]?) -> (path: [Int], card: ContextCard, level: Int)? {
        guard let h = hover, h.count >= 2, let ring = rings.first(where: { $0.parent == Array(h.dropLast()) }),
              let card = ring.options.first(where: { $0.key == h.last })?.card else { return nil }
        let deeper = dwelt == h || rings.contains { $0.parent == h }
        return (h, card, deeper ? 3 : 2)
    }
}

/// Where the card goes: next to its direction's capsule, as close as it fits, never over the cross, its centre or
/// a sub-ring, inside `bounds` (relative to the HUD centre, y-down). Tried around the capsule relative to the way out of
/// the centre: beside it first (across the direction), then diagonally, outward, backward, at a growing distance.
public enum ContextCardLayout {
    public static let smallWidth = 240.0
    public static let bigWidth = 480.0   // was 300, too small to read
    public static let gap = 8.0

    public static func place(anchor: HUDSlot, size: (width: Double, height: Double), avoid: [HUDSlot],
                             bounds: (minX: Double, minY: Double, maxX: Double, maxY: Double)) -> (x: Double, y: Double)? {
        let base = hypot(anchor.x, anchor.y) < 1e-6 ? 0 : atan2(anchor.y, anchor.x)
        // beside the capsule first (the sub-ring fans out along `base`, the cross lies behind it)
        let q = Double.pi / 4
        let turns: [Double] = [-2 * q, 2 * q, -q, q, 0, -3 * q, 3 * q, 4 * q]
        let angles = turns.map { base + $0 }
        var r = 0.0
        while r <= 320 {
            for a in angles {
                // the card's edge `gap + r` away from the capsule's edge, along `a`
                let dx = (anchor.width + size.width) / 2 + gap + r, dy = (anchor.height + size.height) / 2 + gap + r
                let s = HUDSlot(key: Int.min, x: anchor.x + cos(a) * dx, y: anchor.y + sin(a) * dy,
                                width: size.width, height: size.height, position: nil)
                guard SubRingLayout.inside(s, bounds), !SubRingLayout.overlaps(s, anchor, gap: 2),
                      !avoid.contains(where: { SubRingLayout.overlaps(s, $0, gap: 2) }) else { continue }
                return (s.x, s.y)
            }
            r += 12
        }
        return nil
    }
}
