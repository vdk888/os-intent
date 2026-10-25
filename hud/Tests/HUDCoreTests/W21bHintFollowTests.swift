import Foundation
import Testing
@testable import HUDCore

// W21b: the hint dots follow the pointer while drawn; the developed cross does not.

@Suite struct W21bHintFollowTests {
    let screen = (minX: 0.0, minY: 0.0, maxX: 1000.0, maxY: 800.0)
    let half = (w: 30.0, h: 30.0)
    func opts(_ keys: [Int]) -> [HUDOption] { keys.map { HUDOption(key: $0, label: "o\($0)") } }

    func shown() -> HintLife {
        var h = HintLife()
        h.show(opts([2, 3]), fullTTLms: 6000, hintTTLms: 4000, at: 0)
        return h
    }

    @Test func startsOnHintAndOnlyOnce() {
        var f = HintFollower()
        #expect(f.sync(HintLife(), crossVisible: false, centre: (0, 0)) == nil)     // no hint: nothing runs
        #expect(!f.running)
        let h = shown()
        #expect(f.sync(h, crossVisible: false, centre: (100, 200)) == .start)
        #expect(f.running && f.centre == (100, 200))
        #expect(f.sync(h, crossVisible: false, centre: (5, 5)) == nil)              // no second timer
        #expect(f.centre == (100, 200))
    }

    @Test func stopsOnEveryEndPath() {
        // fade
        var h = shown(), f = HintFollower()
        _ = f.sync(h, crossVisible: false, centre: (0, 0))
        h.fade(at: 4)
        #expect(f.sync(h, crossVisible: false, centre: (0, 0)) == .stop && !f.running)
        // develop
        h = shown(); f = HintFollower()
        _ = f.sync(h, crossVisible: false, centre: (0, 0))
        _ = h.develop(at: 1)
        _ = f.develop(pointer: (1, 1))
        #expect(!f.running)
        #expect(f.sync(h, crossVisible: true, centre: (0, 0)) == nil)               // already off: nothing to start
        // Esc / hint_ttl / replaced: the hint is reset
        h = shown(); f = HintFollower()
        _ = f.sync(h, crossVisible: false, centre: (0, 0))
        h.reset()
        #expect(f.sync(h, crossVisible: false, centre: (0, 0)) == .stop)
        // a full cross coming up over the dots
        h = shown(); f = HintFollower()
        _ = f.sync(h, crossVisible: false, centre: (0, 0))
        #expect(f.sync(h, crossVisible: true, centre: (0, 0)) == .stop)
        // stopped: a step moves nothing
        #expect(f.step(pointer: (500, 500), dt: 0.1, screen: screen, halfSize: half, reduceMotion: false) == (0, 0))
    }

    @Test func followsWithAShortLagAndSettlesWithoutJitter() {
        var f = HintFollower()
        _ = f.sync(shown(), crossVisible: false, centre: (100, 100))
        let first = f.step(pointer: (200, 100), dt: 1.0 / 60, screen: screen, halfSize: half, reduceMotion: false)
        #expect(first.x > 100 && first.x < 140 && first.y == 100)                  // eased, not a jump
        var c = first
        for _ in 0..<6 { c = f.step(pointer: (200, 100), dt: 1.0 / 60, screen: screen, halfSize: half, reduceMotion: false) }
        #expect(c.x > 200 - 100 * exp(-1) - 1)                                       // ~1 lag (≈ 100 ms) in: most of the way
        for _ in 0..<60 { c = f.step(pointer: (200, 100), dt: 1.0 / 60, screen: screen, halfSize: half, reduceMotion: false) }
        #expect(c == (200, 100))                                                     // snapped: exactly on, no creep
        #expect(f.step(pointer: (200, 100), dt: 1.0 / 60, screen: screen, halfSize: half, reduceMotion: false) == (200, 100))
    }

    @Test func aStalledFrameNeverTeleports() {
        var f = HintFollower()
        _ = f.sync(shown(), crossVisible: false, centre: (100, 100))
        let c = f.step(pointer: (900, 100), dt: 5, screen: screen, halfSize: half, reduceMotion: false)
        #expect(c.x < 900 && c.x > 100)
    }

    @Test func reduceMotionFollowsWithoutEasing() {
        var f = HintFollower()
        _ = f.sync(shown(), crossVisible: false, centre: (100, 100))
        #expect(f.step(pointer: (420, 333), dt: 1.0 / 60, screen: screen, halfSize: half, reduceMotion: true) == (420, 333))
    }

    @Test func clampedToTheScreen() {
        var f = HintFollower()
        _ = f.sync(shown(), crossVisible: false, centre: (500, 400))
        for (p, want) in [((0.0, 0.0), (30.0, 30.0)), ((1000.0, 800.0), (970.0, 770.0)), ((-50.0, 400.0), (30.0, 400.0))] {
            #expect(f.step(pointer: p, dt: 1, screen: screen, halfSize: half, reduceMotion: true) == want)
        }
        var e = HintFollower()
        _ = e.sync(shown(), crossVisible: false, centre: (500, 400))
        var c = (x: 500.0, y: 400.0)
        for _ in 0..<200 { c = e.step(pointer: (2000, -300), dt: 1.0 / 60, screen: screen, halfSize: half, reduceMotion: false) }
        #expect(c == (970, 30))                                                      // eased into the corner, never past it
    }

    @Test func developAnchorsTheCrossAtThePointerNotTheTrailingDots() {
        var f = HintFollower()
        _ = f.sync(shown(), crossVisible: false, centre: (100, 100))
        let dots = f.step(pointer: (300, 300), dt: 1.0 / 60, screen: screen, halfSize: half, reduceMotion: false)
        #expect(dots != (300, 300))                                                  // the dots still trail
        #expect(f.develop(pointer: (300, 300)) == (300, 300))
        #expect(!f.running)
        // the cross does not follow afterwards
        #expect(f.step(pointer: (700, 700), dt: 1, screen: screen, halfSize: half, reduceMotion: true) == dots)
    }
}

// W21 review note (b): glide sub-ring keys (oi/glide.py: i+1 per child ring) live INSIDE their parent's ring.
@Suite struct W21bGlideChildKeyTests {
    func slot(_ k: Int, _ x: Double, _ y: Double) -> HUDSlot { HUDSlot(key: k, x: x, y: y, width: 60, height: 30, position: nil) }

    @Test func childKeysAreScopedByTheParentPath() throws {
        // root keys 2 ⌃ (top) and 4 ⌘ (bottom); the sub-ring of 4 sends keys 1, 2 (i+1) — 2 is also a root key
        let top = [slot(2, 0, -80), slot(4, 0, 80)]
        let rootOpts = [HUDOption(key: 2, label: "root2"), HUDOption(key: 4, label: "root4")]
        let cmd = try decodeCommand(Data(#"{"cmd":"subring","path":[4],"options":[{"key":1,"label":"c1"},{"key":2,"label":"c2"}]}"#.utf8))
        guard case .subring(let path, let children) = cmd else { Issue.record("not a subring"); return }
        var g = GlideState(dwellMs: 300)
        g.begin(at: 0)
        _ = g.hover([4], at: 0)
        let e1 = g.hover([4], at: 0.4)
        #expect(e1 == [4])
        #expect(g.subring(path, options: children, slots: [slot(1, -70, 130), slot(2, 70, 130)]) == true)
        // the child 2 is reached as [4, 2]; the root 2 stays [2] (no collision)
        #expect(glideHit((70, 130), top: top, rings: g.rings) == [4, 2])
        #expect(glideHit((0, -80), top: top, rings: g.rings) == [2])
        #expect(g.option(at: [4, 2], top: rootOpts)?.label == "c2")
        #expect(g.option(at: [2], top: rootOpts)?.label == "root2")
        #expect(g.option(at: [2, 2], top: rootOpts) == nil)                         // no ring under the root 2
        // a deeper ring keeps its own scope too
        _ = g.hover([4, 2], at: 1)
        let e2 = g.hover([4, 2], at: 1.4)
        #expect(e2 == [4, 2])
        #expect(g.subring([4, 2], options: [HUDOption(key: 1, label: "g1")], slots: [slot(1, 70, 180)]) == true)
        #expect(glideHit((70, 180), top: top, rings: g.rings) == [4, 2, 1])
        #expect(g.option(at: [4, 2, 1], top: rootOpts)?.label == "g1")
        #expect(g.option(at: [4, 1], top: rootOpts)?.label == "c1")
    }

    @Test func childKeysBeyondFourAreRefused() {
        let five = (1...5).map { #"{"key":\#($0),"label":"c\#($0)"}"# }.joined(separator: ",")
        #expect(throws: CommandError.self) {
            try decodeCommand(Data(#"{"cmd":"subring","path":[2],"options":[\#(five)]}"#.utf8))
        }
    }
}
