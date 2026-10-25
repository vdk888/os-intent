import Foundation
import Testing
@testable import MenubarCore
import OISettings

/// W24: the Settings window's Proactive section: settings.json `proactive` (python-owned), every source
/// off by default, only the edited keys written, the rest of `proactive` (folders, cockpit, feeds) kept.
@Suite struct W24ProactiveSettingsTests {
    typealias F = SettingsForm

    private func proactive(_ s: OISettings) throws -> [String: Any] {
        let o = try #require(JSONSerialization.jsonObject(with: s.encoded()) as? [String: Any])
        return o["proactive"] as? [String: Any] ?? [:]
    }

    @Test func defaultsAllOff() {
        let f = F(OISettings.decode(Data("{}".utf8)))
        #expect(f.proactiveSources.values.allSatisfy { !$0 } && f.proactiveSources.count == 9)
        #expect(f.proactiveMinPct == 50 && f.proactiveIntervalMin == 10 && f.quietStart == "22:00" && f.quietEnd == "08:00")
        #expect(f.respectFocus && f.isValid)
        var s = OISettings.decode(Data("{}".utf8))
        f.apply(to: &s)
        #expect(s.extra["proactive"] == nil)                 // untouched defaults are not written
    }

    @Test func readsAndWritesOnlyEditedKeys() throws {
        let raw = #"""
        {"proactive": {"sources": {"mail": true}, "min_p": 0.65, "min_interval_min": 15, "quiet_hours": null,
                       "respect_focus": false, "files": {"folders": ["~/Desktop"]}, "cockpit": {"poll_s": 30}}}
        """#
        let s0 = OISettings.decode(Data(raw.utf8))
        let base = F(s0)
        #expect(base.proactiveSources["mail"] == true && base.proactiveSources["agents"] == false)
        #expect(base.proactiveMinPct == 65 && base.proactiveIntervalMin == 15 && base.quietStart.isEmpty && !base.respectFocus)
        var f = base
        f.proactiveSources["agents"] = true
        f.proactiveMinPct = 40
        var s = s0
        f.apply(to: &s, since: base)
        let p = try proactive(s)
        #expect((p["sources"] as? [String: Bool]) == ["mail": true, "agents": true])
        #expect(p["min_p"] as? Double == 0.4 && p["min_interval_min"] as? Double == 15)
        #expect(p["quiet_hours"] is NSNull && p["respect_focus"] as? Bool == false)
        #expect((p["files"] as? [String: Any])?["folders"] as? [String] == ["~/Desktop"])   // kept as is
        #expect((p["cockpit"] as? [String: Any])?["poll_s"] as? Double == 30)
    }

    @Test func quietHoursValidationAndReset() {
        var f = F()
        f.quietStart = "25:00"
        #expect(f.problems()[.proactive]?.isEmpty == false)
        f.quietStart = ""; f.quietEnd = ""
        #expect(f.isValid)                                    // none
        f.quietStart = "23:00"
        #expect(!f.isValid)                                   // one side only
        f.proactiveMinPct = 140
        f.reset(.proactive)
        #expect(f == F())
        #expect(F.Section.proactive.resettable && F.Section.proactive.title == "Proactive")
    }
}

/// Outward gate: the `gate.outward` toggle in Settings → Privacy.
@Suite struct OutwardGateSettingsTests {
    @Test func readsWritesAndKeepsTheOtherGateKeys() throws {
        #expect(SettingsForm().outwardGate)                                                       // default on
        let off = OISettings.decode(Data(#"{"gate": {"outward": false, "show_min_p": 0.2}}"#.utf8))
        #expect(!SettingsForm(off).outwardGate)
        let perClass = OISettings.decode(Data(#"{"gate": {"outward": {"vocal": false}}}"#.utf8))
        #expect(SettingsForm(perClass).outwardGate)                                               // an object: on
        var s = off, f = SettingsForm(off)
        let base = f
        f.outwardGate = true
        f.apply(to: &s, since: base)
        #expect(s.gate?["outward"] == .bool(true) && s.gate?["show_min_p"] == .number(0.2))
        var untouched = perClass
        SettingsForm(perClass).apply(to: &untouched, since: SettingsForm(perClass))
        #expect(untouched.gate?["outward"] == .object(["vocal": .bool(false)]))                    // not rewritten
        f.reset(.privacy)
        #expect(f.outwardGate)
    }
}
