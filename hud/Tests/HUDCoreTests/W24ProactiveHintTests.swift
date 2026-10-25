import Foundation
import Testing
@testable import HUDCore

// W24: a proactive hint = the W19 dots with `"marker":"ring"` (a thin ring = "something changed").

private func opts(_ keys: [Int]) -> [HUDOption] { keys.map { HUDOption(key: $0, label: "o\($0)", icon: "sf:app") } }

@Suite struct W24ProactiveHintTests {
    @Test func ringMarkerDecodes() throws {
        let line = #"{"cmd":"show","mode":"hint","marker":"ring","hint_ttl_ms":4000,"ttl_ms":6000,"options":[{"key":2,"label":"Reply to G"}]}"#
        guard case .showHint(let o, _, _, let m) = try decodeCommand(Data(line.utf8)) else { Issue.record("not a hint"); return }
        #expect(o.map(\.key) == [2] && m == .ring)
    }
    @Test func noOrUnknownMarkerIsAPlainHint() throws {
        guard case .showHint(_, _, _, let m) = try decodeCommand(Data(#"{"cmd":"show","mode":"hint","options":[]}"#.utf8)) else {
            Issue.record("not a hint"); return
        }
        #expect(m == nil)
        guard case .showHint(_, _, _, let u) = try decodeCommand(Data(#"{"cmd":"show","mode":"hint","marker":"star","options":[]}"#.utf8)) else {
            Issue.record("not a hint"); return
        }
        #expect(u == nil)
        // a full show ignores the marker (never fails)
        #expect(try decodeCommand(Data(#"{"cmd":"show","marker":"ring","options":[],"ttl_ms":6000}"#.utf8)) == .show(options: [], ttlMs: 6000))
    }
    @Test func lifeKeepsTheMarkerUntilReset() {
        var h = HintLife()
        h.show(opts([1, 3]), fullTTLms: 6000, hintTTLms: 4000, at: 10, marker: .ring)
        #expect(h.marker == .ring && h.stateName == "dots:1,3:ring")
        #expect(h.fade(at: 14) && h.stateName == "faded" && h.marker == .ring)
        _ = h.develop(at: 15)
        #expect(h.isIdle && h.marker == nil)
        h.show(opts([2]), fullTTLms: 6000, hintTTLms: 4000, at: 20)
        #expect(h.marker == nil && h.stateName == "dots:2")      // a prediction hint after a proactive one: no ring
    }
    @Test func hideEndsTheHintWhenNoCrossIsUp() throws {   // the event's state ended: the ring hint goes
        #expect(try decodeCommand(Data(#"{"cmd":"hide"}"#.utf8)) == .hide)
        var h = HintLife()
        #expect(h.hideTarget(crossVisible: false) == .nothing)
        h.show(opts([1]), fullTTLms: 6000, hintTTLms: 4000, at: 0, marker: .ring)
        #expect(h.hideTarget(crossVisible: false) == .hint && h.hideTarget(crossVisible: true) == .cross)
        _ = h.fade(at: 4)
        #expect(h.hideTarget(crossVisible: false) == .hint)       // faded but still developable: withdrawn too
        h.reset()
        #expect(h.hideTarget(crossVisible: false) == .nothing)
    }
    @Test func ringSitsOutsideTheDotsAndFitsTheClamp() {
        for size in [4.0, 10.0, 24.0] {
            let r = HintDotLayout.ringRadius(dotSize: size)
            #expect(r > HintDotLayout.radius + size * HintDotLayout.pulseScale / 2)       // never over a pulsing dot
            #expect(HintDotLayout.halfExtent(dotSize: size, marker: .ring) >= r + HintDotLayout.ringWidth)
            #expect(HintDotLayout.halfExtent(dotSize: size) <= HintDotLayout.halfExtent(dotSize: size, marker: .ring))
        }
    }
    @Test func importantEventOpensTheFullCross() throws {   // ADDENDUM 2: no mode/marker = the cross, at once
        let line = #"{"cmd":"show","ttl_ms":6000,"options":[{"key":1,"label":"Reply to Sam"},{"key":2,"label":"Open the mail"},{"key":3,"label":"Ask Orion"}]}"#
        guard case .show(let o, let ttl) = try decodeCommand(Data(line.utf8)) else { Issue.record("not a full show"); return }
        #expect(o.map(\.label) == ["Reply to Sam", "Open the mail", "Ask Orion"] && ttl == 6000)
        #expect(HintLife().hideTarget(crossVisible: true) == .cross)   // the mail read: oi live's hide ends the cross
    }
}
