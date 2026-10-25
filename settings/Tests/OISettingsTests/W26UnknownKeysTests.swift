import Foundation
import Testing
@testable import OISettings

/// W26: a save from the menu bar keeps the keys OISettings does not know (top level and `hud`).
@Suite struct W26UnknownKeysTests {
    private func object(_ d: Data) throws -> [String: Any] {
        try #require(JSONSerialization.jsonObject(with: d) as? [String: Any])
    }

    @Test func topLevelAndHUDUnknownKeysRoundTrip() throws {
        let raw = #"{"paused": true, "future_key": {"a": [1, "b", null]}, "flag": false,"#
            + #" "hud": {"auto_mode": "full", "new_hud_key": 3.5}}"#
        let s = OISettings.decode(Data(raw.utf8))
        #expect(s.extra["future_key"] == .object(["a": .array([.number(1), .string("b"), .null])]))
        #expect(s.extra["flag"] == .bool(false))
        #expect(s.hud.extra == ["new_hud_key": .number(3.5)])
        #expect(s.extra["paused"] == nil && s.extra["hud"] == nil)   // known keys are not extras
        let o = try object(s.encoded())
        #expect((o["future_key"] as? [String: Any])?["a"] != nil)
        #expect(o["flag"] as? Bool == false)
        #expect((o["hud"] as? [String: Any])?["new_hud_key"] as? Double == 3.5)
        #expect((o["hud"] as? [String: Any])?["auto_mode"] as? String == "full")
        #expect(OISettings.decode(try s.encoded()) == s)
    }

    @Test func gateOutwardPerClassObjectKeepsTheGateBlock() throws {
        // outward gate: `gate.outward` may be {"vocal": false, …}; the rest of `gate` must survive a save
        let raw = #"{"gate": {"ttl_ms": 9000, "show_min_p": 0.2, "outward": {"vocal": false, "page": true}}}"#
        let s = OISettings.decode(Data(raw.utf8))
        #expect(s.gateTTLms == 9000 && s.gate?["outward"] == .object(["vocal": .bool(false), "page": .bool(true)]))
        let g = try #require(try object(s.encoded())["gate"] as? [String: Any])
        #expect((g["outward"] as? [String: Any])?["vocal"] as? Bool == false && g["show_min_p"] as? Double == 0.2)
    }

    @Test func updateKeepsUnknownKeys() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("oi-w26-\(UUID().uuidString)")
        let url = dir.appendingPathComponent("settings.json")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try Data(#"{"x_python": {"k": 1}, "hud": {"y": "z"}}"#.utf8).write(to: url)
        try SettingsStore.update(at: url) { $0.paused = true }
        let o = try object(Data(contentsOf: url))
        #expect(o["paused"] as? Bool == true)
        #expect((o["x_python"] as? [String: Any])?["k"] as? Double == 1)
        #expect((o["hud"] as? [String: Any])?["y"] as? String == "z")
    }

    @Test func noExtrasWritesNoExtraKeys() throws {
        let o = try object(OISettings().encoded())
        #expect(Set(o.keys) == ["paused", "hud_enabled", "denylist_apps", "repo_dir", "hotkeys", "wish", "hud"])
    }
}
