import Foundation
import Testing
@testable import HUDCore

// context cards on the glide (level 2 = the « what », level 3 = summary + excerpt + why).

private func slot(_ k: Int, _ x: Double, _ y: Double, w: Double = 120, h: Double = 28) -> HUDSlot {
    HUDSlot(key: k, x: x, y: y, width: w, height: h, position: nil)
}

@Suite struct ContextCardTests {
    @Test func decodesTheContextCommandTolerantly() throws {
        let line = #"{"cmd":"context","cards":[{"key":3,"what":"notion.so/prr"},{"key":2,"what":" Sam · Re: devis ","#
            + #""summary":["Asks for the quote","","Deadline Friday","c","d","e"],"excerpt":"pouvez-vous nous envoyer le devis ?","#
            + #""why":{"page":"atlas_rnd/clients/acme.md","snippet":"Acme: active client"},"pending":"yes"}]}"#
        guard case .context(let cards) = try decodeCommand(Data(line.utf8)) else { Issue.record("not context"); return }
        #expect(cards.map(\.key) == [2, 3])
        let c = cards[0]
        #expect(c.what == "Sam · Re: devis")
        #expect(c.summary == ["Asks for the quote", "Deadline Friday", "c", "d"])      // blanks dropped, 4 lines max
        #expect(c.why?.pageName == "acme" && c.why?.snippet == "Acme: active client")
        #expect(c.pending == false)                                                   // malformed = absent
        #expect(cards[1] == ContextCard(key: 3, what: "notion.so/prr"))
    }

    @Test func rejectsBadCards() {
        #expect(throws: CommandError.badJSON) { try decodeCommand(Data(#"{"cmd":"context","cards":[{"key":1,"what":"  "}]}"#.utf8)) }
        #expect(throws: CommandError.optionCount(0)) { try decodeCommand(Data(#"{"cmd":"context","cards":[]}"#.utf8)) }
        #expect(throws: CommandError.badKey(5)) { try decodeCommand(Data(#"{"cmd":"context","cards":[{"key":5,"what":"x"}]}"#.utf8)) }
        #expect(throws: CommandError.duplicateKey(1)) {
            try decodeCommand(Data(#"{"cmd":"context","cards":[{"key":1,"what":"x"},{"key":1,"what":"y"}]}"#.utf8))
        }
    }

    @Test func levelFollowsTheDirectionAndItsDepth() {
        #expect(ContextCardLevel.current(hover: [2], rings: [], dwelt: nil) == nil)            // not rested yet
        #expect(ContextCardLevel.current(hover: [2], rings: [], dwelt: [2])! == (2, 2))         // dwell done: level 2 at once
        let ring2 = SubRing(parent: [2], options: [], slots: [])
        #expect(ContextCardLevel.current(hover: nil, rings: [ring2], dwelt: [2])! == (2, 2))    // off the capsules: kept
        #expect(ContextCardLevel.current(hover: [2, 1], rings: [ring2], dwelt: [2, 1])! == (2, 3))   // one level deeper
        let ring21 = SubRing(parent: [2, 1], options: [], slots: [])
        #expect(ContextCardLevel.current(hover: [2, 1, 3], rings: [ring2, ring21], dwelt: [2, 1, 3])! == (2, 3))   // capped
        #expect(ContextCardLevel.current(hover: [3], rings: [ring2], dwelt: [2]) == nil)        // another direction, not rested
    }

    @Test func glideRecordsTheDwellEvenOnALeafOrADeadEnd() {
        var g = GlideState(dwellMs: 300)
        g.begin(at: 0)
        _ = g.hover([2], at: 0)
        #expect(g.dwelt == nil)
        #expect(g.hover([2], at: 0.31, expandable: { _ in false }) == nil)                   // nothing to expand
        #expect(g.dwelt == [2])                                                              // but the card may grow
        g.end()
        #expect(g.dwelt == nil)
    }

    @Test func level3NeedsDetailAndIsBigger() {
        let small = ContextCard(key: 2, what: "Sam · Re: devis")
        #expect(small.shownLevel(3) == 2 && small.size(level: 3) == small.size(level: 2))
        let big = ContextCard(key: 2, what: "w", summary: ["a", "b", "c"], excerpt: "x", why: ContextWhy(page: "p.md"))
        #expect(big.shownLevel(3) == 3)
        #expect(big.size(level: 3).width > big.size(level: 2).width && big.size(level: 3).height > 100)
        let pending = ContextCard(key: 2, what: "w", pending: true)
        #expect(pending.shownLevel(3) == 3 && pending.size(level: 3).height > pending.size(level: 2).height)
        let merged = ContextCard.merge([2: pending, 3: small], [big])
        #expect(merged[2] == big && merged[3] == small)
    }

    @Test func placedBesideTheCapsuleClearOfTheCrossAndRings() {
        let anchor = slot(2, 160, 0)                                  // the right capsule
        let cross = [slot(1, 0, -80), slot(3, 0, 80), slot(4, -160, 0), slot(Int.min, 0, 0, w: 48, h: 48)]
        let ring = [slot(1, 270, -30), slot(2, 280, 20)]
        let bounds = (minX: -600.0, minY: -450.0, maxX: 600.0, maxY: 450.0)
        for size in [(width: 240.0, height: 30.0), (width: 300.0, height: 140.0)] {
            guard let p = ContextCardLayout.place(anchor: anchor, size: size, avoid: cross + ring, bounds: bounds) else {
                Issue.record("not placed"); return
            }
            let s = slot(0, p.x, p.y, w: size.width, h: size.height)
            #expect(SubRingLayout.inside(s, bounds))
            #expect(!SubRingLayout.overlaps(s, anchor, gap: 1))
            #expect(!(cross + ring).contains { SubRingLayout.overlaps(s, $0, gap: 1) })
            #expect(hypot(p.x - anchor.x, p.y - anchor.y) < 260)        // next to its direction, not across the screen
        }
        #expect(ContextCardLayout.place(anchor: anchor, size: (300, 140), avoid: [],
                                        bounds: (minX: 100, minY: -20, maxX: 220, maxY: 20)) == nil)   // no room: no card
    }

    // an app direction's live items carry their own card in the sub-ring.
    @Test func subringItemsCarryTheirOwnCardTolerantly() throws {
        let line = #"{"cmd":"subring","path":[1],"options":[{"key":1,"label":"Atlas · 8","action":"telegram.chat","#
            + #""card":{"key":1,"what":"Atlas t'attend · 8 messages","why":{"page":"atlas_rnd/hot.md"}}},"#
            + #"{"key":2,"label":"Eric","action":"telegram.chat","card":{"key":2,"what":" "}}]}"#
        guard case .subring(let path, let opts) = try decodeCommand(Data(line.utf8)) else { Issue.record("not subring"); return }
        #expect(path == [1] && opts.count == 2)
        #expect(opts[0].card?.what == "Atlas t'attend · 8 messages" && opts[0].card?.why?.pageName == "hot")
        #expect(opts[1].card == nil)                                   // a malformed card = no card, the option stays
    }

    @Test func hoveredItemCardLevel() {
        let card = ContextCard(key: 1, what: "Atlas t'attend", why: ContextWhy(page: "p.md"))
        let ring = SubRing(parent: [1], options: [HUDOption(key: 1, label: "Atlas · 8", card: card),
                                                  HUDOption(key: 2, label: "Eric")], slots: [])
        #expect(ContextCardLevel.item(hover: [1, 1], rings: [ring], dwelt: nil).map { [$0.path, [$0.level]] } == [[1, 1], [2]])
        #expect(ContextCardLevel.item(hover: [1, 1], rings: [ring], dwelt: [1, 1])?.level == 3)
        #expect(ContextCardLevel.item(hover: [1, 2], rings: [ring], dwelt: nil) == nil)   // no card: the direction's
        #expect(ContextCardLevel.item(hover: [1], rings: [ring], dwelt: nil) == nil)
    }
}
