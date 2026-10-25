import Foundation
import Testing
@testable import MenubarCore

/// W8: the "Live predictor" row read from status/oi-live.json.
@Suite struct LiveStatusTests {
    let now = ISO8601DateFormatter().date(from: "2026-10-20T12:00:10Z")!
    let utc = TimeZone(identifier: "UTC")!

    @Test func decodesThePythonFile() {
        let json = #"{"ts": "2026-10-20T12:00:05.123Z", "state": "running", "pid": 9, "dry_run": false, "shows_today": 3, "pressed_today": 1, "last_show_ts": "2026-10-20T11:58:00.000Z", "model_trained_at": null, "moments_today": 40}"#
        let s = LiveStatus.decode(Data(json.utf8))
        #expect(s?.shows_today == 3 && s?.pressed_today == 1 && s?.moments_today == 40)
        #expect(LiveStatus.text(s, now: now, tz: utc) == "Live predictor: running · shows today 3 · pressed 1 · last 11:58")
    }
    @Test func staleMeansStopped() {
        let s = LiveStatus(ts: "2026-10-20T11:00:00Z", state: "running")
        #expect(LiveStatus.text(s, now: now, tz: utc) == "Live predictor: stopped · shows today 0 · pressed 0")
    }
    @Test func pausedAndHudOff() {
        #expect(LiveStatus.text(LiveStatus(ts: "2026-10-20T12:00:09Z", state: "paused"), now: now, tz: utc)
                .hasPrefix("Live predictor: paused"))
        #expect(LiveStatus.text(LiveStatus(ts: "2026-10-20T12:00:09Z", state: "hud_disabled"), now: now, tz: utc)
                .hasPrefix("Live predictor: running — HUD disabled"))
    }
    @Test func missingFileAndMenuRows() {
        #expect(LiveStatus.text(nil) == "Live predictor: stopped (no status yet)")
        #expect(LiveStatus.decode(Data("{}".utf8)) == nil)
        let e = MenuModel.entries(Status())
        let i = e.firstIndex { $0.id == "live" }
        #expect(i != nil && e[i! - 1].id == "hud")
        #expect(e.first { $0.id == "restart.live" }?.kind == .action(.restartLive))
        #expect(Set(e.map(\.id)).count == e.count)
    }
}
