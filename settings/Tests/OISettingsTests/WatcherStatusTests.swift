import Foundation
import Testing
@testable import OISettings

@Suite struct WatcherStatusTests {
    let sample = WatcherStatus(ts: "2026-10-20T11:00:00Z", keyTap: true, ax: false,
                               safariAutomation: .notAsked, spaceAPI: true, pid: 4242)

    @Test func snakeCaseRoundTrip() throws {
        let data = try sample.encoded()
        let text = String(decoding: data, as: UTF8.self)
        #expect(text == #"{"ax":false,"key_tap":true,"pid":4242,"safari_automation":"not_asked","space_api":true,"ts":"2026-10-20T11:00:00Z"}"# + "\n")
        #expect(WatcherStatus.decode(data) == sample)
    }
    @Test func tolerantOfUnknownAutomationAndMissingOptionals() {
        let s = WatcherStatus.decode(Data(#"{"ts":"2026-10-20T11:00:00Z","key_tap":false,"ax":true,"safari_automation":"maybe"}"#.utf8))
        #expect(s?.safariAutomation == .unknown && s?.spaceAPI == false && s?.pid == 0 && s?.ax == true)
    }
    @Test func rejectsGarbageAndMissingRequired() {
        #expect(WatcherStatus.decode(Data("nope".utf8)) == nil)
        #expect(WatcherStatus.decode(Data(#"{"ts":"x"}"#.utf8)) == nil)
    }
    @Test func automationMapping() {
        #expect(WatcherStatus.automation(fromOSStatus: 0) == .granted)
        #expect(WatcherStatus.automation(fromOSStatus: -1743) == .denied)
        #expect(WatcherStatus.automation(fromOSStatus: -1744) == .notAsked)
        #expect(WatcherStatus.automation(fromOSStatus: -600) == .unknown)
    }
    @Test func age() {
        let now = ISO8601DateFormatter().date(from: "2026-10-20T11:01:40Z")!
        #expect(sample.age(now: now) == 100)
        var bad = sample; bad.ts = "yesterday"
        #expect(bad.age(now: now) == nil)
    }
    @Test func atomicWriteAndLoad() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("oi-ws-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("status/oi-watcher.json")
        try sample.write(to: url)
        #expect(WatcherStatus.load(url) == sample)
        let mode = (try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as? Int) ?? 0
        #expect(mode == 0o600)
        let left = try FileManager.default.contentsOfDirectory(atPath: url.deletingLastPathComponent().path)
        #expect(left == ["oi-watcher.json"])
        #expect(WatcherStatus.load(dir.appendingPathComponent("absent.json")) == nil)
    }
}
