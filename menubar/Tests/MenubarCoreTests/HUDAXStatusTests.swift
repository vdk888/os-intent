import Foundation
import Testing
@testable import MenubarCore

// W16: the menu hint for oi-hud's Accessibility (page actions).

@Suite struct HUDAXStatusTests {
    let now = ISO8601DateFormatter().date(from: "2026-10-20T16:00:00Z")!

    @Test func parseFreshStaleMissing() {
        let ok = Data(#"{"ts":"2026-10-20T15:59:30Z","ax":true,"pid":1}"#.utf8)
        let no = Data(#"{"ts":"2026-10-20T15:59:30.123Z","ax":false}"#.utf8)
        let old = Data(#"{"ts":"2026-10-20T15:00:00Z","ax":true}"#.utf8)
        #expect(HUDAXStatus.parse(ok, now: now) == .granted)
        #expect(HUDAXStatus.parse(no, now: now) == .notGranted)
        #expect(HUDAXStatus.parse(old, now: now) == .unknown("HUD silent"))
        #expect(HUDAXStatus.parse(nil, now: now) == .unknown("HUD silent"))
        #expect(HUDAXStatus.parse(Data("junk".utf8), now: now) == .unknown("HUD silent"))
    }

    @Test func menuRowOpensAccessibility() {
        var s = Status()
        s.hudAX = .notGranted
        let e = MenuModel.entries(s)
        let row = e.first { $0.id == "perm.hudax" }
        #expect(row?.kind == .action(.openAccessibility))
        #expect(row?.title == "✗ Page actions: Accessibility for oi-hud — not granted (needed to offer page buttons) · open pane…")
        s.hudAX = .granted
        #expect(MenuModel.entries(s).first { $0.id == "perm.hudax" }?.title == "✓ Page actions: Accessibility for oi-hud — granted · open pane…")
        #expect(Set(e.map(\.id)).count == e.count)
    }
}
