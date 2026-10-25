import Foundation
import Testing
@testable import MenubarCore
import OISettings

/// W26: the Settings… window model: every exposed key read and written under the name its reader
/// uses, ranges, round-trip with unknown keys kept, reset per section.
@Suite struct SettingsFormTests {
    typealias F = SettingsForm

    private func json(_ s: OISettings) throws -> [String: Any] {
        try #require(JSONSerialization.jsonObject(with: s.encoded()) as? [String: Any])
    }
    private func sub(_ o: [String: Any], _ k: String) -> [String: Any] { o[k] as? [String: Any] ?? [:] }

    static let full = #"""
    {"paused": true, "denylist_apps": ["Signal", "Mail"],
     "hud": {"auto_mode": "full", "hint_dot_size": 14, "hint_ttl_ms": 3000, "fill_order": ["fn", "cmd", "option", "ctrl"],
             "colors": {"ctrl": {"light": "#112233", "dark": "#445566"}}, "zz_hud": 1},
     "gate": {"hint_cooldown_s": 12, "ttl_ms": 9000, "hide_on_pointer_travel": false, "pointer_grace_ms": 1500,
              "pointer_fast_px_s": 900, "show_min_p": 0.2},
     "claude": {"enabled": false, "enrich": false, "local_only_domains": ["@Client.EXAMPLE", "acme.example"], "debug": true},
     "shots": {"enabled": true, "clipboard": false, "fallback_contact": null, "ttl_ms": 15000},
     "live": {"predictor": "blend", "laya_checkpoint": "/x/ck"}, "future": [1]}
    """#

    @Test func defaultsFromEmptyFile() {
        let f = F(OISettings.decode(Data("{}".utf8)))
        #expect(f.autoMode == "hint" && f.hintDotSize == 10 && f.hintTTLms == 4000)
        #expect(f.hintCooldownS == 8 && f.fullTTLms == 6000)
        #expect(f.pointerHide && f.pointerGraceMs == 2000 && f.pointerFastPxS == 1200)
        #expect(f.colors == HUDColors.defaults && f.fillOrder == [.ctrl, .option, .cmd, .fn])
        #expect(f.claudeEnabled && f.claudeEnrich && f.localOnlyDomains.isEmpty)
        #expect(f.shotsEnabled && f.shotsClipboard && f.fallbackContact == "atlas")
        #expect(!f.paused && f.denylistApps == OISettings.defaultDenylist)
        #expect(f.isValid)
    }

    @Test func readsEveryExposedKey() {
        let f = F(OISettings.decode(Data(Self.full.utf8)))
        #expect(f.autoMode == "full" && f.hintDotSize == 14 && f.hintTTLms == 3000)
        #expect(f.hintCooldownS == 12 && f.fullTTLms == 9000)
        #expect(!f.pointerHide && f.pointerGraceMs == 1500 && f.pointerFastPxS == 900)
        #expect(f.colors[.ctrl] == HUDColor(light: "#112233", dark: "#445566"))
        #expect(f.colors[.option] == HUDColors.defaults[.option])
        #expect(f.fillOrder == [.fn, .cmd, .option, .ctrl])
        #expect(!f.claudeEnabled && !f.claudeEnrich && f.localOnlyDomains == ["client.example", "acme.example"])
        #expect(f.shotsEnabled && !f.shotsClipboard && f.fallbackContact == "")
        #expect(f.paused && f.denylistApps == ["Signal", "Mail"])
    }

    @Test func writesEveryExposedKeyUnderItsReaderName() throws {
        var f = F()
        f.autoMode = "off"; f.hintDotSize = 20; f.hintTTLms = 7000; f.hintCooldownS = 30; f.fullTTLms = 12000
        f.pointerHide = false; f.pointerGraceMs = 500; f.pointerFastPxS = 2500
        f.colors[.fn] = HUDColor(light: "#000001", dark: "#FFFFFE"); f.fillOrder = [.cmd, .fn, .ctrl, .option]
        f.claudeEnabled = false; f.claudeEnrich = false; f.localOnlyDomains = ["Client.example", " .bank.example "]
        f.shotsEnabled = false; f.shotsClipboard = false; f.fallbackContact = "nova"
        f.paused = true; f.denylistApps = ["Signal", " signal ", ""]
        var s = OISettings(); f.apply(to: &s)
        let o = try json(s)
        let hud = sub(o, "hud"), gate = sub(o, "gate"), claude = sub(o, "claude"), shots = sub(o, "shots")
        #expect(hud["auto_mode"] as? String == "off" && hud["hint_dot_size"] as? Int == 20 && hud["hint_ttl_ms"] as? Int == 7000)
        #expect(hud["fill_order"] as? [String] == ["cmd", "fn", "ctrl", "option"])
        #expect(sub(sub(hud, "colors"), "fn")["light"] as? String == "#000001")
        #expect(gate["hint_cooldown_s"] as? Double == 30 && gate["ttl_ms"] as? Double == 12000)
        #expect(gate["hide_on_pointer_travel"] as? Bool == false)
        #expect(gate["pointer_grace_ms"] as? Double == 500 && gate["pointer_fast_px_s"] as? Double == 2500)
        #expect(claude["enabled"] as? Bool == false && claude["enrich"] as? Bool == false)
        #expect(claude["local_only_domains"] as? [String] == ["client.example", "bank.example"])
        #expect(shots["enabled"] as? Bool == false && shots["clipboard"] as? Bool == false && shots["fallback_contact"] as? String == "nova")
        #expect(o["paused"] as? Bool == true && o["denylist_apps"] as? [String] == ["Signal"])
        // the readers see it: oi-hud / oi-watcher through OISettings
        let back = OISettings.decode(try s.encoded())
        #expect(back.gateTTLms == 12000 && back.pointerHide == PointerHide(enabled: false, graceMs: 500, fastPxS: 2500, fastMs: 250))
        #expect(back.shotFlags.files == false && back.hud.hintDotSize == 20)
    }

    @Test func roundTripKeepsUnknownKeys() throws {
        // (domains already clean: "@Client.EXAMPLE" would be rewritten "client.example", like the python reader reads it)
        let s0 = OISettings.decode(Data(Self.full.replacingOccurrences(of: "@Client.EXAMPLE", with: "client.example").utf8))
        let f = F(s0)
        var s = s0; f.apply(to: &s)
        #expect(s == s0)                                       // nothing changed = nothing rewritten differently
        let o = try json(s)
        #expect(o["future"] as? [Int] == [1])
        #expect(sub(o, "hud")["zz_hud"] as? Int == 1)
        #expect(sub(o, "gate")["show_min_p"] as? Double == 0.2)
        #expect(sub(o, "claude")["debug"] as? Bool == true)
        #expect(sub(o, "shots")["ttl_ms"] as? Double == 15000)
        #expect(o["shots"].map { ($0 as? [String: Any])?["fallback_contact"] is NSNull } == true)
        #expect(sub(o, "live")["predictor"] as? String == "blend")
        #expect(F(OISettings.decode(try s.encoded())) == f)
    }

    @Test func untouchedDefaultsAreNotPinned() throws {
        var s = OISettings(); F().apply(to: &s)
        let o = try json(s)
        #expect(o["gate"] == nil && o["claude"] == nil && o["shots"] == nil)
        var f = F(); f.fallbackContact = ""; f.apply(to: &s)
        #expect(Set(sub(try json(s), "shots").keys) == ["fallback_contact"])
    }

    @Test func ranges() {
        let cases: [(WritableKeyPath<F, Int>, ClosedRange<Int>, F.Section)] = [
            (\F.hintDotSize, F.hintDotSizeRange, .hud), (\F.hintTTLms, F.hintTTLRange, .timings),
            (\F.hintCooldownS, F.hintCooldownRange, .timings), (\F.fullTTLms, F.fullTTLRange, .timings),
            (\F.pointerGraceMs, F.pointerGraceRange, .pointer), (\F.pointerFastPxS, F.pointerSpeedRange, .pointer),
        ]
        for (kp, r, sec) in cases {
            var f = F()
            for v in [r.lowerBound, r.upperBound] { f[keyPath: kp] = v; #expect(f.isValid) }
            for v in [r.lowerBound - 1, r.upperBound + 1] {
                f[keyPath: kp] = v
                #expect(Array(f.problems().keys) == [sec])
            }
        }
    }

    @Test func rangesMatchTheReadersClamps() {
        #expect(HUDSettings.clampHintDotSize(F.hintDotSizeRange.lowerBound - 1) == F.hintDotSizeRange.lowerBound)
        #expect(HUDSettings.clampHintDotSize(F.hintDotSizeRange.upperBound + 1) == F.hintDotSizeRange.upperBound)
        #expect(HUDSettings.clampHintTTL(F.hintTTLRange.lowerBound - 1) == F.hintTTLRange.lowerBound)
        #expect(HUDSettings.clampHintTTL(F.hintTTLRange.upperBound + 1) == F.hintTTLRange.upperBound)
        var s = OISettings(); s.gate = ["ttl_ms": .number(Double(F.fullTTLRange.upperBound + 1))]
        #expect(s.gateTTLms == F.fullTTLRange.upperBound)
        s.gate = ["ttl_ms": .number(Double(F.fullTTLRange.lowerBound - 1))]
        #expect(s.gateTTLms == F.fullTTLRange.lowerBound)
    }

    @Test func otherValidation() {
        var f = F(); f.autoMode = "sometimes"; #expect(f.problems()[.hud] != nil)
        f = F(); f.colors[.cmd] = HUDColor(light: "blue", dark: "#000000"); #expect(f.problems()[.colors] != nil)
        f = F(); f.fillOrder = [.ctrl, .ctrl, .cmd, .fn]; #expect(f.problems()[.fillOrder] != nil)
        f = F(); f.localOnlyDomains = ["ok.example", "not a domain", "nodot"]; #expect(f.problems()[.claude]?.first?.contains("not a domain, nodot") == true)
        f = F(); f.fallbackContact = "Not A Slug"; #expect(f.problems()[.screenshots] != nil)
        f = F(); f.fallbackContact = ""; #expect(f.isValid)
    }

    @Test func fillOrderSwapStaysAPermutation() {
        var f = F()
        f.setFill(rank: 0, to: .fn)
        #expect(f.fillOrder == [.fn, .option, .cmd, .ctrl])
        f.setFill(rank: 3, to: .option)
        #expect(f.fillOrder == [.fn, .ctrl, .cmd, .option])
        #expect(HUDSettings.validFillOrder(f.fillOrder))
    }

    @Test func domainsCleanedLikePython() {
        let r = F.cleanDomains(["  @Client.EXAMPLE ", ".sub.acme.example", "client.example", "", "x y"])
        #expect(r.domains == ["client.example", "sub.acme.example"] && r.invalid == ["x y"])
    }

    @Test func resetTouchesOneSectionOnly() {
        var f = F(OISettings.decode(Data(Self.full.utf8)))
        f.reset(.timings)
        #expect(f.hintTTLms == 4000 && f.hintCooldownS == 8 && f.fullTTLms == 6000)
        #expect(f.autoMode == "full" && !f.pointerHide && !f.claudeEnabled && f.paused)
        for s in F.Section.allCases where s.resettable { f.reset(s) }
        #expect(f == F())
        f.colors[.ctrl] = HUDColor(light: "#000000", dark: "#000000"); f.resetColor(.ctrl)
        #expect(f.colors == HUDColors.defaults)
    }

    @Test func readOnlyRecaps() {
        var s = OISettings.decode(Data(Self.full.utf8))
        let l = F.laya(s, live: LiveStatus(ts: "2026-10-20T20:00:00Z", state: "running", model_trained_at: "2026-10-20T18:05:00Z"),
                       tz: TimeZone(identifier: "Europe/Paris")!)
        #expect(l.predictor == "blend" && l.checkpoint == "/x/ck" && l.trained == "2026-10-20 20:05")
        #expect(F.laya(OISettings(), live: nil).predictor == "markov")
        s.vocalRecipients = .object(["lyra": .null, "nova": .object(["name": .string("Nova B")]), "zed": .object([:])])
        let r = F.recipients(s)
        #expect(!r.contains { $0.slug == "lyra" } && r.first { $0.slug == "nova" }?.name == "Nova B" && r.last?.slug == "zed")
        #expect(F.recipients(OISettings()).count == 9)
        let k = F.keyMap(.defaults)
        #expect(k.count == HotkeySet.actions.count && k[0].label == "Option 1 (fn)" && k[0].value.hasSuffix("tap"))
    }

    @Test func menuHasSettingsEntry() {
        let e = MenuModel.entries(Status())
        let i = e.firstIndex { $0.id == "settings" }, j = e.firstIndex { $0.id == "denylist" }
        #expect(i != nil && j != nil && i! < j! && e[i!].kind == .action(.openSettings) && e[i!].title == "Settings…")
    }

    // W26 review: the window writes only what the user changed, merged into the CURRENT file.
    @Test func menuPauseWhileWindowOpenSurvivesWindowEdit() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("oi-w26-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("settings.json")
        try SettingsStore.save(OISettings.decode(Data(#"{"paused": false, "denylist_apps": ["Signal"]}"#.utf8)), to: url)
        let baseline = F(SettingsStore.load(url))                       // window opened
        try SettingsStore.update(at: url) { $0.paused = true }           // menu: Pause
        try SettingsStore.update(at: url) { $0.denylistApps.append("Mail") }   // denylist window
        var form = baseline; form.hintDotSize = 16                       // window: edit another field
        let s = try SettingsStore.update(at: url) { form.apply(to: &$0, since: baseline) }
        #expect(s.paused && s.denylistApps == ["Signal", "Mail"] && s.hud.hintDotSize == 16)
        #expect(SettingsStore.load(url).paused)
        // a colour edited elsewhere stays when the window edits another colour
        try SettingsStore.update(at: url) { $0.hud.colors = HUDColors([.ctrl: HUDColor(light: "#010203", dark: "#040506")]) }
        let b2 = form; form.colors[.fn] = HUDColor(light: "#0A0B0C", dark: "#0D0E0F")
        let s2 = try SettingsStore.update(at: url) { form.apply(to: &$0, since: b2) }
        #expect(s2.hud.colors.byModifier[.ctrl] == HUDColor(light: "#010203", dark: "#040506"))
        #expect(s2.hud.colors.byModifier[.fn] == HUDColor(light: "#0A0B0C", dark: "#0D0E0F"))
        #expect(s2.paused && s2.hud.hintDotSize == 16)
        // and the window itself can still un-pause
        let b3 = F(s2); var f3 = b3; f3.paused = false
        #expect(!(try SettingsStore.update(at: url) { f3.apply(to: &$0, since: b3) }).paused)
    }

    @Test func boundaryValuesRoundTripThroughTheReaders() throws {
        for lo in [true, false] {
            var f = F()
            func pick(_ r: ClosedRange<Int>) -> Int { lo ? r.lowerBound : r.upperBound }
            f.hintDotSize = pick(F.hintDotSizeRange); f.hintTTLms = pick(F.hintTTLRange)
            f.hintCooldownS = pick(F.hintCooldownRange); f.fullTTLms = pick(F.fullTTLRange)
            f.pointerGraceMs = pick(F.pointerGraceRange); f.pointerFastPxS = pick(F.pointerSpeedRange)
            f.autoMode = lo ? "off" : "full"; f.fallbackContact = lo ? "" : "a-b_9"
            #expect(f.isValid)
            var s = OISettings(); f.apply(to: &s)
            #expect(F(OISettings.decode(try s.encoded())) == f)        // oi-hud / oi-watcher read it back as typed
        }
    }
}

