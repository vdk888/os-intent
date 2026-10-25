import Foundation
import Testing
@testable import HUDCore
import OISettings

// W15: hold ⌥⌘ + glide + dwell = a Claude-enriched sub-ring marking menu.

private let optCmd: HeldModifiers = [.option, .command]
private let summonSet: Set<TapModifier> = [.option, .cmd]

@Suite struct HoldVsTapTests {
    @Test func holdFiresAfter250msAndItsReleaseIsNotATap() {
        var h = SetHoldDetector(want: summonSet)
        var tap = TapDetector()
        #expect(h.flagsChanged(.option, at: 10.0) == nil); _ = tap.flagsChanged(.option, at: 10.0)
        #expect(h.flagsChanged(optCmd, at: 10.02) == .armed); _ = tap.flagsChanged(optCmd, at: 10.02)
        #expect(h.fire(at: 10.2) == false)                               // 180 ms: not yet
        #expect(h.fire(at: 10.27) == true)                               // 250 ms: the glide
        #expect(h.fire(at: 10.3) == false)                               // once
        tap.otherInput()                                         // what the monitor does on a hold: no tap on release
        #expect(h.flagsChanged(.command, at: 10.4) == .released(downAt: 10.02))
        _ = tap.flagsChanged(.command, at: 10.4)
        #expect(tap.flagsChanged([], at: 10.45) == nil)
        #expect(h.flagsChanged([], at: 10.45) == nil)
    }
    @Test func aQuickTapStaysTheSummonTap() {
        var h = SetHoldDetector(want: summonSet)
        var tap = TapDetector()
        _ = h.flagsChanged(.command, at: 0); _ = tap.flagsChanged(.command, at: 0)
        #expect(h.flagsChanged(optCmd, at: 0.03) == .armed); _ = tap.flagsChanged(optCmd, at: 0.03)
        #expect(h.flagsChanged(.option, at: 0.15) == nil)        // released at 120 ms: never held
        #expect(tap.flagsChanged(.option, at: 0.15) == nil)
        #expect(tap.flagsChanged([], at: 0.18) == summonSet)     // the W11 tap, unchanged
        _ = h.flagsChanged([], at: 0.18)
        #expect(h.fire(at: 0.28) == false)                               // the late timer finds nothing
    }
    @Test func chordBeforeTheThresholdIsNeverAHold() {
        var h = SetHoldDetector(want: summonSet)
        _ = h.flagsChanged(optCmd, at: 0)                         // (both at once: still armed by the set)
        _ = h.otherInput(isKey: true)                             // ⌥⌘C
        #expect(h.fire(at: 0.3) == false)
        _ = h.flagsChanged([], at: 0.5)
        _ = h.flagsChanged(.option, at: 1); _ = h.flagsChanged(optCmd, at: 1.01)
        _ = h.otherInput(isKey: false)                            // ⌥⌘-click / scroll before 250 ms
        #expect(h.fire(at: 1.5) == false)
    }
    @Test func duringAHoldAKeyInterruptsAndAClickDoesNot() {
        var h = SetHoldDetector(want: summonSet)
        _ = h.flagsChanged(.option, at: 0); _ = h.flagsChanged(optCmd, at: 0.01)
        #expect(h.fire(at: 0.3) == true)
        #expect(h.otherInput(isKey: false) == nil)               // a click on a capsule = the click
        #expect(h.flagsChanged([.option, .command, .shift], at: 0.4) == nil)   // ⇧ joining does not end it
        #expect(h.otherInput(isKey: true) == .interrupted)       // Esc / any key: cancel
    }
    @Test func singleModifierSummonAndThirdModifierReleaseNeverHold() {
        var single = SetHoldDetector(want: [.shift])
        #expect(!single.enabled)
        #expect(single.flagsChanged(.shift, at: 0) == nil)
        #expect(single.fire(at: 1) == false)
        var h = SetHoldDetector(want: summonSet)
        _ = h.flagsChanged(.control, at: 0); _ = h.flagsChanged([.control, .option], at: 0.01)
        _ = h.flagsChanged([.control, .option, .command], at: 0.02)
        #expect(h.flagsChanged(optCmd, at: 0.05) == nil)         // reached by releasing ⌃: not a hold
        #expect(h.fire(at: 0.5) == false)
    }
    @Test func primedWithKeysDownNothingFires() {
        var h = SetHoldDetector(want: summonSet)
        h.prime(optCmd)
        #expect(h.flagsChanged(.option, at: 0.1) == nil)
        #expect(h.fire(at: 1) == false)
        var tap = TapDetector()
        tap.prime(.command)                                       // ⌘ still down when the option taps start
        #expect(tap.flagsChanged([], at: 0.1) == nil)            // its release is not option 3
        #expect(tap.flagsChanged(.command, at: 1) == nil)
        #expect(tap.flagsChanged([], at: 1.1) == [.cmd])          // the next real tap works
        var lp = LongPressDetector(thresholdMs: 600)
        lp.prime(.option)
        #expect(lp.flagsChanged([], at: 2) == nil)
    }
}

private func opt(_ k: Int, _ l: String = "x", claude: Bool? = nil) -> HUDOption { HUDOption(key: k, label: l, claude: claude) }

private func slot(_ k: Int, _ x: Double, _ y: Double, w: Double = 90) -> HUDSlot {
    HUDSlot(key: k, x: x, y: y, width: w, height: 32, position: nil)
}

@Suite struct GlideStateTests {
    @Test func dwellExpandsOnceAndTheAnswerOpensTheRing() {
        var g = GlideState(dwellMs: 500)
        g.begin(at: 0)
        #expect(g.hover([2], at: 0.1) == nil)
        #expect(g.hover([2], at: 0.4) == nil)                    // 300 ms: not yet
        #expect(g.hover([2], at: 0.6) == [2])                    // 500 ms: expand
        #expect(g.pending == [2])
        #expect(g.hover([2], at: 0.9) == nil)                    // asked once
        #expect(g.thinking(at: 0.7) == nil)                      // < 150 ms: no arc
        #expect(g.thinking(at: 0.9)?.path == [2])
        #expect(g.subring([2], options: [opt(1), opt(2)], slots: [slot(1, 200, -40), slot(2, 200, 40)]) == true)
        #expect(g.rings.count == 1 && g.rings[0].parent == [2] && g.expanded == 1)
        #expect(g.thinking(at: 1.0) == nil)
        #expect(g.hover([2], at: 2.0) == nil)                    // its ring is open: not again
    }
    @Test func movingRestartsTheDwellAndRecordsVisits() {
        var g = GlideState(dwellMs: 500)
        g.begin(at: 0)
        _ = g.hover([1], at: 0)
        _ = g.hover([3], at: 0.3)                                // left [1] after 300 ms
        #expect(g.hover([3], at: 0.6) == nil)                    // only 300 ms on [3]
        #expect(g.hover([3], at: 0.8) == [3])
        _ = g.hover(nil, at: 0.82)                               // left [3] after 520 ms
        _ = g.hover([4], at: 0.85); _ = g.hover(nil, at: 0.88)   // 30 ms on [4]: a crossing, not a visit
        #expect(g.visits == [GlideVisit(path: [1], ms: 300), GlideVisit(path: [3], ms: 520)])
    }
    @Test func emptyAnswerIsALeafAndStaleAnswersAreDropped() {
        var g = GlideState(dwellMs: 300)
        g.begin(at: 0)
        _ = g.hover([1], at: 0); #expect(g.hover([1], at: 0.3) == [1])
        #expect(g.subring([2], options: [opt(1)], slots: [slot(1, 0, 0)]) == false)   // not the pending path
        #expect(g.subring([1], options: [], slots: []) == true)           // nothing to offer
        #expect(g.rings.isEmpty)
        _ = g.hover(nil, at: 0.4); _ = g.hover([1], at: 0.5)
        #expect(g.hover([1], at: 1.0) == nil)                    // a leaf is not asked again
        _ = g.hover([3], at: 1.1); #expect(g.hover([3], at: 1.4) == [3])
        _ = g.hover([4], at: 1.5); #expect(g.hover([4], at: 1.8) == [4])   // supersedes [3]
        #expect(g.subring([3], options: [opt(1)], slots: [slot(1, 0, 0)]) == false)
        #expect(g.subring([4], options: [opt(1)], slots: [slot(1, -200, 0)]) == true)
    }
    @Test func unansweredRequestExpires() {
        var g = GlideState(dwellMs: 500)
        g.begin(at: 0)
        _ = g.hover([2], at: 0); _ = g.hover([2], at: 0.5)
        g.expire(at: 5); #expect(g.pending == [2])
        g.expire(at: 0.5 + GlideState.expandTimeout); #expect(g.pending == nil)
        #expect(g.thinking(at: 20) == nil)
    }
    @Test func chainToDepthThreeAndNoDeeper() {
        var g = GlideState(dwellMs: 250)
        g.begin(at: 0)
        var t = 0.0
        var path = [2]
        for depth in 1...GlideState.maxDepth {
            _ = g.hover(path, at: t); t += 0.3
            #expect(g.hover(path, at: t) == path)
            #expect(g.subring(path, options: [opt(1), opt(2)], slots: [slot(1, Double(depth) * 100, 0), slot(2, Double(depth) * 100, 50)]) == true)
            path.append(1); t += 0.1
        }
        #expect(g.rings.map(\.parent) == [[2], [2, 1], [2, 1, 1]])
        #expect(path.count == 4)
        _ = g.hover(path, at: t)
        #expect(g.hover(path, at: t + 2) == nil)                 // depth cap: a 4-key path never expands
        #expect(g.option(at: path, top: [opt(2)]) != nil)
        // a sibling at level 1 closes the deeper rings when its ring opens
        _ = g.hover([2, 2], at: t + 3); #expect(g.hover([2, 2], at: t + 3.3) == [2, 2])
        #expect(g.subring([2, 2], options: [opt(1)], slots: [slot(1, 300, 300)]) == true)
        #expect(g.rings.map(\.parent) == [[2], [2, 2]])
    }
    @Test func releaseExecutesTheHoveredOptionOrCancels() {
        var g = GlideState(dwellMs: 500)
        g.begin(at: 0)
        _ = g.hover([2], at: 0); _ = g.hover([2], at: 0.5)
        _ = g.subring([2], options: [opt(1, claude: true)], slots: [slot(1, 220, -40)])
        _ = g.hover([2, 1], at: 0.8)
        #expect(g.release(at: 1.0, tapWindow: false) == .execute([2, 1]))
        let log = g.log(final: [2, 1])
        #expect(log.depth == 1 && log.expanded == 1 && log.final == [2, 1])
        #expect(log.visited == [GlideVisit(path: [2], ms: 800), GlideVisit(path: [2, 1], ms: 200)])

        var c = GlideState(dwellMs: 500)
        c.begin(at: 0)
        _ = c.hover([1], at: 0); _ = c.hover(nil, at: 0.2)
        #expect(c.release(at: 0.3, tapWindow: true) == .cancel)  // visited something: empty space = cancel
        #expect(c.log(final: nil).final == nil)

        var tap = GlideState(dwellMs: 500)
        tap.begin(at: 0)
        #expect(tap.release(at: 0.1, tapWindow: true) == .keepAsTap)   // a slow tap stays a tap
        var late = GlideState(dwellMs: 500)
        late.begin(at: 0)
        #expect(late.release(at: 2, tapWindow: false) == .cancel)
    }
    @Test func endClearsEverything() {
        var g = GlideState(dwellMs: 500)
        g.begin(at: 0)
        _ = g.hover([1], at: 0); _ = g.hover([1], at: 0.6)
        g.end()
        #expect(!g.active && g.rings.isEmpty && g.pending == nil && g.hover == nil)
        #expect(g.hover([1], at: 2) == nil)
    }
    @Test func dwellComesFromSettingsClamped() {
        #expect(GlideState(dwellMs: 500).dwell == 0.5)
        #expect(GlideState(dwellMs: 50).dwell == 0.25)
        #expect(GlideState(dwellMs: 99_000).dwell == 2.0)
    }
}

@Suite struct SubRingLayoutTests {
    /// The default cross: ⌃ top (1), ⌥ right (2), ⌘ bottom (3), fn left (4) + wish / vocal, widths as drawn.
    private func cross() -> [HUDSlot] {
        let pos = FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots)
        let options = (1...4).map { HUDOption(key: $0, label: "Opt \($0)") }
            + [HUDOption(key: wishOptionKey, label: "w"), HUDOption(key: vocalOptionKey, label: "v")]
        return hudSlots(options: options, positions: pos, radius: 80, capsuleHeight: 32) { _ in 110 }
    }
    private let wide = (minX: -600.0, minY: -450.0, maxX: 600.0, maxY: 450.0)
    private let centre = HUDSlot(key: Int.min, x: 0, y: 0, width: 48, height: 48, position: nil)

    @Test func everyCapsuleOfTheCrossGetsANonOverlappingNearbyRing() {
        let top = cross()
        for parent in top where (1...4).contains(parent.key) {
            let placed = SubRingLayout.place(parent: parent, origin: (0, 0), keys: [1, 2, 3, 4], widths: [96, 120, 80, 140],
                                             height: 32, avoid: top + [centre], bounds: wide)
            let got = placed.compactMap { $0 }
            #expect(got.count == 4, "parent \(parent.key)")
            for (i, a) in got.enumerated() {
                #expect(!top.contains { SubRingLayout.overlaps(a, $0) }, "overlaps the cross (parent \(parent.key))")
                #expect(!SubRingLayout.overlaps(a, centre))
                #expect(SubRingLayout.inside(a, wide))
                #expect(hypot(a.x - parent.x, a.y - parent.y) <= 200, "short distance (parent \(parent.key))")
                for b in got[(i + 1)...] { #expect(!SubRingLayout.overlaps(a, b)) }
            }
        }
    }
    @Test func theRingFansOutwardFromTheParent() {
        let top = cross()
        let right = top.first { $0.key == 3 }!                     // W21: ⌥ = 3
        let placed = SubRingLayout.place(parent: right, origin: (0, 0), keys: [1, 2], widths: [90, 90], height: 32,
                                         avoid: top + [centre], bounds: wide).compactMap { $0 }
        #expect(placed.allSatisfy { $0.x > right.x })             // the ⌥ (right) capsule's ring is on its right
        let topSlot = top.first { $0.key == 2 }!                   // W21: ⌃ = 2
        let up = SubRingLayout.place(parent: topSlot, origin: (0, 0), keys: [1, 2], widths: [90, 90], height: 32,
                                     avoid: top + [centre], bounds: wide).compactMap { $0 }
        #expect(up.allSatisfy { $0.y < topSlot.y })               // the ⌃ (top) capsule's ring is above it
    }
    @Test func clampedToTheScreen() {
        let top = cross()
        let right = top.first { $0.key == 3 }!                     // W21: ⌥ = 3
        // the screen ends 60 px right of the ⌥ capsule's outer edge: the ring folds above / below, inside
        let edge = right.x + right.width / 2 + 60
        let tight = (minX: -600.0, minY: -300.0, maxX: edge, maxY: 300.0)
        let placed = SubRingLayout.place(parent: right, origin: (0, 0), keys: [1, 2, 3], widths: [110, 110, 110], height: 32,
                                         avoid: top + [centre], bounds: tight)
        for s in placed.compactMap({ $0 }) {
            #expect(SubRingLayout.inside(s, tight))
            #expect(!top.contains { SubRingLayout.overlaps(s, $0) })
        }
        #expect(placed.compactMap { $0 }.count >= 2)
        // no room at all: nothing placed (never drawn off screen)
        let none = SubRingLayout.place(parent: right, origin: (0, 0), keys: [1], widths: [110], height: 32, avoid: top,
                                       bounds: (minX: right.x - 10, minY: -10, maxX: right.x + 10, maxY: 10))
        #expect(none == [nil])
    }
    @Test func deeperRingsAvoidTheirAncestors() {
        let top = cross()
        let right = top.first { $0.key == 3 }!                     // W21: ⌥ = 3
        let l1 = SubRingLayout.place(parent: right, origin: (0, 0), keys: [1, 2, 3], widths: [100, 100, 100], height: 32,
                                     avoid: top + [centre], bounds: wide).compactMap { $0 }
        let p = l1[0]
        let l2 = SubRingLayout.place(parent: p, origin: (right.x, right.y), keys: [1, 2], widths: [100, 100], height: 32,
                                     avoid: top + l1 + [centre], bounds: wide).compactMap { $0 }
        #expect(l2.count == 2)
        for s in l2 { #expect(!(top + l1).contains { SubRingLayout.overlaps(s, $0) }) }
    }
    @Test func hitTestPrefersTheDeepestRing() {
        let top = [slot(2, 120, 0)]
        let ring = SubRing(parent: [2], options: [opt(1)], slots: [slot(1, 260, 0)])
        let deeper = SubRing(parent: [2, 1], options: [opt(3)], slots: [slot(3, 260, 0)])   // drawn over it
        #expect(glideHit((120, 0), top: top, rings: [ring]) == [2])
        #expect(glideHit((260, 0), top: top, rings: [ring]) == [2, 1])
        #expect(glideHit((260, 0), top: top, rings: [ring, deeper]) == [2, 1, 3])
        #expect(glideHit((0, 0), top: top, rings: [ring]) == nil)
    }
    @Test func tintsAreLighterWithDepthInTheSameFamily() {
        let base = "#9CC3FF"
        let d1 = GlideTint.hex(base: base, depth: 1, index: 0)!
        let d2 = GlideTint.hex(base: base, depth: 2, index: 0)!
        func sum(_ h: String) -> Double { let c = HUDColor.rgb(h)!; return c.r + c.g + c.b }
        #expect(sum(d1) > sum(base) && sum(d2) > sum(d1))
        let c = HUDColor.rgb(d1)!
        #expect(c.b >= c.g && c.g >= c.r)                         // still a blue
        #expect(GlideTint.hex(base: "nope", depth: 1, index: 0) == nil)
        #expect(GlideTint.fraction(depth: 9, index: 9) == 0.75)
    }
    // W23: each branch carries its root key's colour, lighter per depth (any sibling of a level is darker
    // than every node one level deeper), in the same family.
    @Test func branchInheritsTheRootColourLighterPerDepth() {
        func sum(_ h: String) -> Double { let c = HUDColor.rgb(h)!; return c.r + c.g + c.b }
        for base in ["#FF9F0A", "#9CC3FF", "#30D158", "#FFD60A"] {
            #expect(GlideTint.hex(base: base, path: [2]) == base)
            for depth in 1...2 {
                let shallow = (1...4).map { GlideTint.hex(base: base, path: Array(repeating: 1, count: depth) + [$0])! }
                let deep = (1...4).map { GlideTint.hex(base: base, path: Array(repeating: 1, count: depth + 1) + [$0])! }
                #expect(shallow.map(sum).max()! < deep.map(sum).min()!)
            }
            let root = HUDColor.rgb(base)!, d2 = HUDColor.rgb(GlideTint.hex(base: base, path: [3, 2, 4])!)!
            #expect((root.r >= root.b) == (d2.r >= d2.b) && (root.g >= root.b) == (d2.g >= d2.b))   // same family
        }
        #expect(GlideTint.hex(base: "#FF9F0A", path: [3, 9]) == GlideTint.hex(base: "#FF9F0A", path: [3, 4]))
        #expect(GlideTint.hex(base: "#FF9F0A", path: []) == nil)
    }
}

@Suite struct GlideProtocolTests {
    @Test func subringCommandDecodes() throws {
        let line = #"{"cmd":"subring","path":[2,1],"options":[{"key":2,"label":"Archive","icon":"sf:archivebox","action":"mail.archive"},{"key":1,"label":"Reply to Sam","claude":true}]}"#
        guard case .subring(let path, let opts) = try decodeCommand(Data(line.utf8)) else { Issue.record("not subring"); return }
        #expect(path == [2, 1] && opts.map(\.key) == [1, 2] && opts[0].offersClaude)
        #expect(throws: CommandError.badPath) { try decodeCommand(Data(#"{"cmd":"subring","path":[2,1,1,1],"options":[]}"#.utf8)) }
        #expect(throws: CommandError.badPath) { try decodeCommand(Data(#"{"cmd":"subring","path":[],"options":[]}"#.utf8)) }
        #expect(throws: CommandError.badPath) { try decodeCommand(Data(#"{"cmd":"subring","path":[0],"options":[]}"#.utf8)) }
        #expect(throws: CommandError.duplicateKey(1)) {
            try decodeCommand(Data(#"{"cmd":"subring","path":[1],"options":[{"key":1,"label":"a"},{"key":1,"label":"b"}]}"#.utf8))
        }
        guard case .subring(_, let none) = try decodeCommand(Data(#"{"cmd":"subring","path":[3]}"#.utf8)) else { return }
        #expect(none.isEmpty)                                      // a leaf
    }
    @Test func debugGlideDecodes() throws {
        #expect(try decodeCommand(Data(#"{"cmd":"debug_glide","path":[2,1]}"#.utf8)) == .debugGlide(path: [2, 1], cancel: false))
        #expect(try decodeCommand(Data(#"{"cmd":"debug_glide","path":[2,1,1,1],"cancel":true}"#.utf8)) == .debugGlide(path: [2, 1, 1, 1], cancel: true))
        #expect(throws: CommandError.badPath) { try decodeCommand(Data(#"{"cmd":"debug_glide","path":[1,1,1,1,1]}"#.utf8)) }
    }
    @Test func glideEventsCarryPathsAndTimesOnly() {
        #expect(HUDEvent.expand(path: [2, 1]).jsonLine() == #"{"event":"expand","path":[2,1]}"# + "\n")
        #expect(HUDEvent.glideStart.jsonLine() == #"{"event":"glide","phase":"start"}"# + "\n")
        let gl = GlideLog(visited: [GlideVisit(path: [2], ms: 620)], expanded: 1, final: [2, 1], depth: 1)
        let pressed = HUDEvent.pressed(key: 2, via: "glide", path: [2, 1], glide: gl).jsonLine()
        #expect(pressed == #"{"event":"pressed","glide":{"depth":1,"expanded":1,"final":[2,1],"visited":[{"ms":620,"path":[2]}]},"key":2,"path":[2,1],"via":"glide"}"# + "\n")
        let cancelled = HUDEvent.dismissed(reason: "glide_cancel", glide: GlideLog(visited: [], expanded: 0, final: nil, depth: 0)).jsonLine()
        #expect(cancelled == #"{"event":"dismissed","glide":{"depth":0,"expanded":0,"visited":[]},"reason":"glide_cancel"}"# + "\n")
        #expect(HUDEvent.dismissed(reason: "esc").jsonLine() == #"{"event":"dismissed","reason":"esc"}"# + "\n")   // unchanged
    }
    @Test func stateCarriesTheGlide() {
        let i = HUDStateInfo(visible: true, options: [], hudEnabled: true, wishHotkey: "⌥`", wishRegistered: true,
                             wishPanel: "closed", glide: "gliding:2.1;rings:1")
        #expect(HUDEvent.state(i).jsonLine().contains(#""glide":"gliding:2.1;rings:1""#))
        let idle = HUDStateInfo(visible: false, options: [], hudEnabled: true, wishHotkey: "", wishRegistered: false, wishPanel: "closed")
        #expect(HUDEvent.state(idle).jsonLine().contains(#""glide":"idle""#))
    }
}
