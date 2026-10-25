import Foundation
import Testing
@testable import OISettings

private func tmpDir() -> URL {
    let d = FileManager.default.temporaryDirectory.appendingPathComponent("oi-settings-\(UUID().uuidString)")
    try! FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
    return d
}

@Suite struct CodecTests {
    @Test func defaultsWhenEmptyObject() {
        let s = OISettings.decode(Data("{}".utf8))
        #expect(s == OISettings())
        #expect(s.denylistApps == ["Passwords", "Keychain Access", "1Password"])
    }
    @Test func corruptGivesDefaults() {
        #expect(OISettings.decode(Data("not json".utf8)) == OISettings())
    }
    @Test func snakeCaseKeysAndRoundTrip() throws {
        let s = OISettings(paused: true, hudEnabled: false, denylistApps: ["X"], repoDir: "/r", updatedAt: "2026-10-20T10:00:00Z")
        let json = String(decoding: try s.encoded(), as: UTF8.self)
        #expect(json.contains("\"hud_enabled\" : false"))
        #expect(json.contains("\"denylist_apps\""))
        #expect(json.contains("\"repo_dir\" : \"/r\""))
        #expect(OISettings.decode(try s.encoded()) == s)
    }
    @Test func wrongTypeFallsBackPerKey() {
        let s = OISettings.decode(Data(#"{"paused":"yes","hud_enabled":false}"#.utf8))
        #expect(s.paused == false && s.hudEnabled == false)
    }
    @Test func wishLocaleDefaultAndRoundTrip() throws {
        #expect(OISettings.decode(Data("{}".utf8)).wish.locale == "fr-FR")
        #expect(OISettings.decode(Data(#"{"wish":{"locale":""}}"#.utf8)).wish.locale == "fr-FR")
        #expect(OISettings.decode(Data(#"{"wish":7}"#.utf8)).wish.locale == "fr-FR")
        let s = OISettings(wish: WishSettings(locale: "en-US"))
        #expect(OISettings.decode(try s.encoded()).wish.locale == "en-US")
    }
    @Test func liveGateKeptOnRoundTrip() throws {
        // W8: oi-menubar must write back the python driver's `gate` / `live` sections unchanged
        let s = OISettings.decode(Data(#"{"paused":true,"gate":{"show_min_p":0.3,"max_show":2},"live":{"poll_s":5}}"#.utf8))
        #expect(s.gate?["show_min_p"] == 0.3 && s.gate?["max_show"] == 2 && s.live?["poll_s"] == 5)
        let back = OISettings.decode(try s.encoded())
        #expect(back.gate == s.gate && back.live == s.live)
        #expect(OISettings.decode(Data("{}".utf8)).gate == nil)
        #expect(!String(decoding: try OISettings().encoded(), as: UTF8.self).contains("gate"))
        #expect(OISettings.decode(Data(#"{"gate":"x"}"#.utf8)).gate == nil)
    }

    @Test func laya_live_keys_round_trip() throws {
        // W13: the Laya layer's live keys (bool, string, null) survive a menu-bar write
        let json = #"{"live":{"poll_s":2,"laya_shadow":true,"predictor":"laya","laya_checkpoint":null,"laya_url":"http://127.0.0.1:8784"}}"#
        let s = OISettings.decode(Data(json.utf8))
        #expect(s.live?["laya_shadow"] == true && s.live?["predictor"] == "laya" && s.live?["poll_s"] == 2)
        #expect(s.live?["laya_checkpoint"] == .null && s.live?["laya_url"]?.string == "http://127.0.0.1:8784")
        let back = OISettings.decode(try s.encoded())
        #expect(back.live == s.live)
    }
    @Test func w9GateTTLAndPointerTravel() throws {
        let d = OISettings()
        #expect(d.gateTTLms == 6000)   // W19 (was 15000)
        #expect(d.pointerHide == PointerHide(enabled: true, graceMs: 2000, fastPxS: 1200, fastMs: 250))
        let s = OISettings.decode(Data(#"{"gate":{"show_min_p":0.3,"ttl_ms":20000,"hide_on_pointer_travel":false,"pointer_grace_ms":3000,"pointer_fast_px_s":1500,"pointer_fast_ms":400}}"#.utf8))
        #expect(s.gateTTLms == 20000)
        #expect(s.pointerHide == PointerHide(enabled: false, graceMs: 3000, fastPxS: 1500, fastMs: 400))
        #expect(s.gate?["show_min_p"] == 0.3)
        let back = OISettings.decode(try s.encoded())           // the bool survives a menubar save
        #expect(back.gate == s.gate && back.pointerHide.enabled == false)
        #expect(String(decoding: try s.encoded(), as: UTF8.self).contains("\"hide_on_pointer_travel\" : false"))
        #expect(OISettings.decode(Data(#"{"gate":{"ttl_ms":100}}"#.utf8)).gateTTLms == 1500)
        #expect(OISettings.decode(Data(#"{"gate":{"ttl_ms":999999}}"#.utf8)).gateTTLms == 60000)
        let bad = OISettings.decode(Data(#"{"gate":{"hide_on_pointer_travel":0,"pointer_grace_ms":-5,"pointer_fast_ms":true}}"#.utf8))
        #expect(bad.pointerHide == PointerHide())               // wrong types / negatives → defaults
    }
    @Test func cleanDenylist() {
        #expect(OISettings.cleanDenylist([" Passwords ", "", "passwords", "Signal", "  "]) == ["Passwords", "Signal"])
    }
}

@Suite struct StoreTests {
    @Test func atomicSaveIs0600AndStamped() throws {
        let dir = tmpDir().appendingPathComponent("sub")
        let url = dir.appendingPathComponent("settings.json")
        let saved = try SettingsStore.save(OISettings(paused: true), to: url, now: Date(timeIntervalSince1970: 0))
        #expect(saved.updatedAt == "1970-01-01T00:00:00Z")
        let perms = try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as? Int
        #expect(perms == 0o600)
        let dperms = try FileManager.default.attributesOfItem(atPath: dir.path)[.posixPermissions] as? Int
        #expect(dperms == 0o700)
        #expect(SettingsStore.load(url).paused == true)
        // no temp files left behind
        let left = try FileManager.default.contentsOfDirectory(atPath: dir.path)
        #expect(left == ["settings.json"])
    }
    @Test func updateKeepsOtherKeys() throws {
        let url = tmpDir().appendingPathComponent("settings.json")
        try SettingsStore.save(OISettings(denylistApps: ["A"]), to: url)
        try SettingsStore.update(at: url) { $0.hudEnabled = false }
        let s = SettingsStore.load(url)
        #expect(s.hudEnabled == false && s.denylistApps == ["A"])
    }
    @Test func missingFileGivesDefaults() {
        #expect(SettingsStore.load(tmpDir().appendingPathComponent("nope.json")) == OISettings())
    }
    @Test func dataDirHonoursEnv() {
        #expect(SettingsStore.dataDir(env: ["OI_DATA_DIR": "/tmp/x"]).path == "/tmp/x")
        #expect(SettingsStore.dataDir(env: [:]).path.hasSuffix("/.local/share/os-intent"))
    }
}

@Suite struct CacheTests {
    @Test func rereadsOnlyOnChange() throws {
        let url = tmpDir().appendingPathComponent("settings.json")
        let cache = SettingsCache(url: url)
        let (a, changed1) = cache.poll()
        #expect(changed1 && a == OISettings())
        #expect(cache.poll().1 == false)
        try SettingsStore.save(OISettings(paused: true), to: url)
        // force a distinct mtime even on coarse filesystems
        try FileManager.default.setAttributes([.modificationDate: Date().addingTimeInterval(5)], ofItemAtPath: url.path)
        let (b, changed2) = cache.poll()
        #expect(changed2 && b.paused)
        try FileManager.default.removeItem(at: url)
        let (c, changed3) = cache.poll()
        #expect(changed3 && c.paused == false)
    }
}

@Suite struct HotkeyTests {
    @Test func defaultsAndJSONShape() throws {
        let s = OISettings()
        #expect(s.hotkeys == .defaults)
        #expect(s.hotkeys.problems().isEmpty)
        let json = String(decoding: try s.encoded(), as: UTF8.self)
        #expect(json.contains("\"option_1\" : {"))
        #expect(json.contains("\"modifiers\" : [\n        \"option\"\n      ]"))
        #expect(OISettings.decode(try s.encoded()).hotkeys == .defaults)
    }
    @Test func physicalKeyCodesAndCarbonMasks() {
        #expect(Hotkey(["option"], "1").keyCode == 0x12)      // kVK_ANSI_1
        #expect(Hotkey([], "Esc").keyCode == 0x35)            // kVK_Escape
        #expect(Hotkey(["option"], "`").keyCode == 0x32)      // kVK_ANSI_Grave
        #expect(Hotkey(["option"], "#50").keyCode == 50)
        #expect(Hotkey(["option"], "nope").keyCode == nil)
        #expect(Hotkey(["shift", "option"], "k").carbonModifiers == 0x0A00)
        #expect(Hotkey(["shift", "control"], "k").modifiers == ["control", "shift"])
    }
    @Test func displayAndParseRoundTrip() {
        for h in [Hotkey(["option"], "1"), Hotkey([], "Esc"), Hotkey(["control", "shift"], "k"), Hotkey(["option"], "`")] {
            #expect(Hotkey.parse(h.display) == h)
        }
        #expect(Hotkey(["control", "shift"], "k").display == "⌃⇧K")
        #expect(Hotkey.parse("ctrl+opt+2") == Hotkey(["control", "option"], "2"))
        #expect(Hotkey.parse("cmd+F5") == Hotkey(["command"], "F5"))
        #expect(Hotkey.parse("⌥") == nil)
        #expect(Hotkey.parse("⌥zz") == nil)
    }
    @Test func fromKeyPress() {
        #expect(Hotkey.from(keyCode: 0x13, modifiers: ["option"]) == Hotkey(["option"], "2"))
        #expect(Hotkey.from(keyCode: 0x5A, modifiers: ["option"]) == Hotkey(["option"], "#90"))
    }
    @Test func tolerantPerKeyDecode() {
        let s = OISettings.decode(Data(#"{"hotkeys":{"option_2":{"modifiers":["control"],"key":"j"},"dismiss":{"modifiers":[],"key":"???"},"wish":7}}"#.utf8))
        #expect(s.hotkeys.option2 == Hotkey(["control"], "j"))
        #expect(s.hotkeys.option1 == HotkeySet.defaults.option1)
        #expect(s.hotkeys.dismiss == HotkeySet.defaults.dismiss)   // unknown key → default
        #expect(s.hotkeys.wish == HotkeySet.defaults.wish)         // wrong type → default
    }
    @Test func problemsCatchCollisionsAndBareKeys() {
        var h = HotkeySet.defaults
        h.option3 = Hotkey(["option"], "3")
        h.wish = Hotkey(["option"], "3")
        #expect(h.problems() == ["Wish (tell me what you wanted) collides with Option 3 (⌥3)"])
        h = .defaults
        h.option1 = Hotkey([], "a")
        #expect(h.problems().first?.contains("needs a modifier") == true)
        h = .defaults
        h["dismiss"] = Hotkey([], "F1")
        #expect(h.problems().isEmpty)
        #expect(h.option(4) == .tap(.cmd) && h.option(1) == .tap(.fn) && h.option(5) == nil)   // W21
    }
}

// MARK: - W9 modifier taps

@Suite struct TapHotkeyTests {
    @Test func defaultsAreTheFourTaps() {
        let d = HotkeySet.defaults
        #expect([d.option1, d.option2, d.option3, d.option4] == [.tap(.fn), .tap(.ctrl), .tap(.option), .tap(.cmd)])   // W21
        #expect(d.dismiss == Hotkey([], "Esc") && d.wish == Hotkey(["option"], "`"))
        #expect(d.problems().isEmpty)
        #expect([d.option1, d.option2, d.option3, d.option4].map(\.display) == ["fn", "⌃", "⌥", "⌘"])
        #expect(d.option1.keyCode == nil && d.option1.isTap)
    }
    @Test func tapJSONShapeAndRoundTrip() throws {
        let json = String(decoding: try OISettings().encoded(), as: UTF8.self)
        #expect(json.contains("\"option_1\" : {\n      \"tap\" : \"fn\"\n    }"))   // W21
        #expect(json.contains("\"v\" : 4"))
        #expect(OISettings.decode(try OISettings().encoded()).hotkeys == .defaults)
        var h = HotkeySet.defaults
        h.option2 = Hotkey(["control", "shift"], "j"); h.option4 = .tap(.option); h.option1 = Hotkey(["option"], "1")
        h.option3 = .tap(.ctrl)
        let back = OISettings.decode(try OISettings(hotkeys: h).encoded()).hotkeys
        #expect(back == h)
    }
    @Test func tapAliasesAndStringForm() {
        let s = OISettings.decode(Data(#"{"hotkeys":{"v":4,"option_1":{"tap":"globe"},"option_2":{"tap":"control"},"option_3":"tap:alt","option_4":{"tap":"command"}}}"#.utf8))
        #expect(s.hotkeys.option1 == .tap(.fn) && s.hotkeys.option2 == .tap(.ctrl))
        #expect(s.hotkeys.option3 == .tap(.option) && s.hotkeys.option4 == .tap(.cmd))
        #expect(Hotkey.parse("tap:fn") == .tap(.fn) && Hotkey.parse("tap:nope") == nil)
        #expect(Hotkey.parse("tap:maj") == .tap(.shift) && Hotkey.tap(.shift).display == "⇧")
    }
    @Test func invalidTapFallsBackPerKey() {
        let s = OISettings.decode(Data(#"{"hotkeys":{"v":2,"option_1":{"tap":"shift"},"option_2":"tap:nope","option_3":{"tap":7},"option_4":{"modifiers":["control"],"key":"j"}}}"#.utf8))
        #expect(s.hotkeys.option1 == .tap(.fn) && s.hotkeys.option2 == .tap(.ctrl) && s.hotkeys.option3 == .tap(.option))
        #expect(s.hotkeys.option4 == Hotkey(["control"], "j"))
    }
    @Test func legacyW5DefaultsMigrateToTaps() {
        // what oi-menubar W5-W8 wrote on every save: ⌥1-⌥4 explicit, no "v"
        let legacy = #"{"hotkeys":{"option_1":{"modifiers":["option"],"key":"1"},"option_2":{"modifiers":["option"],"key":"2"},"option_3":{"modifiers":["option"],"key":"3"},"option_4":{"modifiers":["option"],"key":"4"},"dismiss":{"modifiers":[],"key":"Esc"},"wish":{"modifiers":["control","option"],"key":"w"}}}"#
        let s = OISettings.decode(Data(legacy.utf8))
        #expect(s.hotkeys.option1 == .tap(.fn) && s.hotkeys.option4 == .tap(.cmd))   // straight to the W21 taps
        #expect(s.hotkeys.wish == Hotkey(["control", "option"], "w"))      // a custom wish is kept
        // the same chords with "v": 2 are a deliberate choice: kept
        let chosen = legacy.replacingOccurrences(of: #"{"hotkeys":{"#, with: #"{"hotkeys":{"v":2,"#)
        #expect(OISettings.decode(Data(chosen.utf8)).hotkeys.option1 == Hotkey(["option"], "1"))
        // a legacy file with one custom option is not the W5 default set: kept as is
        let custom = legacy.replacingOccurrences(of: #""key":"3""#, with: #""key":"8""#)
        #expect(OISettings.decode(Data(custom.utf8)).hotkeys.option1 == Hotkey(["option"], "1"))
    }
    @Test func tapProblems() {
        var h = HotkeySet.defaults
        h.option2 = .tap(.fn)
        #expect(h.problems() == ["Option 2 collides with Option 1 (fn tap)"])
        h = .defaults
        h.dismiss = .tap(.cmd)
        #expect(h.problems().contains("Dismiss HUD: a modifier tap only works for options 1-4 and summon (use a combination)"))
        h = .defaults
        h.wish = .tap(.fn)
        #expect(h.problems().count == 2)   // not an option + collides with option 1 (W21: fn = 1)
        h = .defaults
        h.option1 = Hotkey(["option"], "1")                   // a chord next to taps is fine
        #expect(h.problems().isEmpty)
    }
}

// MARK: - W10 summon key

@Suite struct SummonHotkeyTests {
    @Test func defaultIsTheOptionCommandSetTap() {
        let d = HotkeySet.defaults
        #expect(d.summon == .tapSet([.cmd, .option]) && d.summon.display == "⌥⌘" && d.summon.isSetTap)
        #expect(d.problems().isEmpty)
        #expect(HotkeySet.actions.map(\.key).suffix(2) == ["summon", "vocal"])     // W12 adds vocal last
        #expect(HotkeySet.tapChoices("summon").first == .shift)
        #expect(!HotkeySet.tapChoices("option_1").contains(.shift) && HotkeySet.tapChoices("wish").isEmpty)
    }
    @Test func missingSummonGetsTheDefaultAndRoundTrips() throws {
        let w9 = #"{"hotkeys":{"v":2,"option_1":{"tap":"ctrl"}}}"#
        #expect(OISettings.decode(Data(w9.utf8)).hotkeys.summon == HotkeySet.defaultSummon)
        var h = HotkeySet.defaults
        h.summon = Hotkey(["option", "shift"], "Space")
        let json = String(decoding: try OISettings(hotkeys: h).encoded(), as: UTF8.self)
        #expect(json.contains("\"summon\""))
        #expect(OISettings.decode(try OISettings(hotkeys: h).encoded()).hotkeys.summon == h.summon)
        h.summon = .tap(.fn); h.option1 = Hotkey(["option"], "1")
        #expect(OISettings.decode(try OISettings(hotkeys: h).encoded()).hotkeys.summon == .tap(.fn))
    }
    @Test func shiftTapIsSummonOnly() {
        // on read: an option slot with a ⇧ tap falls back to its default
        let s = OISettings.decode(Data(#"{"hotkeys":{"v":3,"option_2":{"tap":"shift"},"summon":"tap:maj"}}"#.utf8))
        #expect(s.hotkeys.option2 == .tap(.ctrl) && s.hotkeys.summon == .tap(.shift))
        var h = HotkeySet.defaults
        h.option1 = .tap(.shift)
        #expect(h.problems().contains("Option 1: the ⇧ tap is reserved for summon and the wish option"))
    }
    @Test func summonCollisions() {
        var h = HotkeySet.defaults
        h.summon = .tap(.ctrl)                                  // same tap as option 2 (W21)
        #expect(h.problems() == ["Summon HUD (show the options now) collides with Option 2 (⌃ tap)"])
        h = .defaults
        h.summon = Hotkey(["option"], "`")                     // same chord as wish
        #expect(h.problems() == ["Summon HUD (show the options now) collides with Wish (tell me what you wanted) (⌥`)"])
        h = .defaults
        h.summon = .tap(.cmd); h.option4 = Hotkey(["option"], "4")
        #expect(h.problems().isEmpty)
    }
}

// MARK: - W10 fixed HUD slots

@Suite struct HUDSlotSettingsTests {
    @Test func defaultsAreKeyboardOrder() throws {
        #expect(OISettings().hud.slots == [.fn: .left, .ctrl: .top, .option: .right, .cmd: .bottom])
        let json = String(decoding: try OISettings().encoded(), as: UTF8.self)
        #expect(json.contains("\"hud\"") && json.contains("\"fn\" : \"left\""))
        #expect(OISettings.decode(try OISettings().encoded()) == OISettings())
    }
    @Test func customMapRoundTrips() throws {
        let s = OISettings.decode(Data(#"{"hud":{"slots":{"ctrl":"top","alt":"left","cmd":"bottom","fn":"right"}}}"#.utf8))
        #expect(s.hud.slots == [.ctrl: .top, .option: .left, .cmd: .bottom, .fn: .right])
        #expect(OISettings.decode(try s.encoded()).hud == s.hud)
    }
    @Test func invalidMapGivesDefaults() {
        for bad in [#"{"ctrl":"left","option":"left","cmd":"right","fn":"bottom"}"#,     // two on one place
                    #"{"ctrl":"left","option":"top","cmd":"right"}"#,                     // fn missing
                    #"{"ctrl":"left","option":"top","cmd":"right","fn":"middle"}"#,       // unknown place
                    #"{"ctrl":"left","option":"top","cmd":"right","shift":"bottom"}"#] {  // ⇧ is not an option tap
            let s = OISettings.decode(Data(#"{"hud":{"slots":\#(bad)}}"#.utf8))
            #expect(s.hud.slots == HUDSettings.defaultSlots)
        }
    }
}

// MARK: - W11 summon as a modifier SET tap

@Suite struct SetTapHotkeyTests {
    @Test func jsonShapeAndRoundTrip() throws {
        let json = String(decoding: try OISettings().encoded(), as: UTF8.self)
        #expect(json.contains("\"summon\" : {\n      \"tap\" : [\n        \"option\",\n        \"cmd\"\n      ]"))
        #expect(OISettings.decode(try OISettings().encoded()).hotkeys.summon == HotkeySet.defaultSummon)
        var h = HotkeySet.defaults
        h.summon = .tapSet([.ctrl, .shift])
        #expect(OISettings.decode(try OISettings(hotkeys: h).encoded()).hotkeys.summon == h.summon)
    }
    @Test func readsTheOldSingleFormAndAliases() {
        let a = OISettings.decode(Data(#"{"hotkeys":{"v":3,"summon":{"tap":["alt","command"]}}}"#.utf8))
        #expect(a.hotkeys.summon == HotkeySet.defaultSummon)
        let b = OISettings.decode(Data(#"{"hotkeys":{"v":3,"summon":"tap:option+cmd"}}"#.utf8))
        #expect(b.hotkeys.summon == HotkeySet.defaultSummon)
        let c = OISettings.decode(Data(#"{"hotkeys":{"v":3,"summon":{"tap":"fn"},"option_4":{"modifiers":["option"],"key":"4"}}}"#.utf8))
        #expect(c.hotkeys.summon == .tap(.fn))                       // the single-string form still works
        #expect(Hotkey.parse("tap:⌥⌘") == HotkeySet.defaultSummon && Hotkey.parse("tap:option+nope") == nil)
    }
    @Test func w10ShiftDefaultMigrates() {
        // what oi-menubar W10 wrote on every save: summon ⇧, v 2 → the new ⌥+⌘ default
        let w10 = OISettings.decode(Data(#"{"hotkeys":{"v":2,"summon":{"tap":"shift"}}}"#.utf8))
        #expect(w10.hotkeys.summon == HotkeySet.defaultSummon)
        // a ⇧ summon saved by W11 (v 3) is a deliberate choice: kept
        let kept = OISettings.decode(Data(#"{"hotkeys":{"v":3,"summon":{"tap":"shift"}}}"#.utf8))
        #expect(kept.hotkeys.summon == .tap(.shift))
    }
    @Test func hotkeysWindowSegments() {
        #expect(HotkeySet.tapPresets("summon") == [HotkeySet.defaultSummon, .tap(.shift)])
        #expect(HotkeySet.tapPresets("option_2").map(\.display) == ["⌃", "⌥", "⌘", "fn"])
        #expect(HotkeySet.capturesTapSet("summon") && !HotkeySet.capturesTapSet("option_1"))
        #expect(HotkeySet.tapPresets("wish").isEmpty && HotkeySet.tapPresets("dismiss").isEmpty)
    }
    @Test func setTapIsSummonOnlyAndNeverCollidesWithItsParts() {
        #expect(HotkeySet.defaults.problems().isEmpty)                // ⌥+⌘ next to the ⌥ and ⌘ option taps
        var h = HotkeySet.defaults
        h.option2 = .tapSet([.option, .cmd])
        #expect(h.problems().contains { $0.hasPrefix("Option 2: a ⌥⌘ set tap only works for summon") })
        let s = OISettings.decode(Data(#"{"hotkeys":{"v":3,"option_1":{"tap":["ctrl","fn"]}}}"#.utf8))
        #expect(s.hotkeys.option1 == .tap(.fn))                       // falls back on read
        h = .defaults
        h.summon = .tapSet([.ctrl, .option, .cmd, .fn])
        #expect(h.problems() == ["Summon HUD (show the options now): a set tap takes 2 or 3 modifiers"])
    }
}

// MARK: - W11 colour per key

@Suite struct HUDColorSettingsTests {
    @Test func defaultsArePastelsPerModifier() throws {
        let c = OISettings().hud.colors
        #expect(c[.ctrl].dark == "#9CC3FF" && c[.option].dark == "#FFE08A")
        #expect(c[.cmd].dark == "#A6E3A1" && c[.fn].dark == "#F5A9C8")
        #expect(Set(TapModifier.optionTaps.map { c[$0].light }).count == 4)   // four distinct colours
        let json = String(decoding: try OISettings().encoded(), as: UTF8.self)
        #expect(json.contains("\"colors\"") && json.contains("\"#FFE08A\""))
        #expect(OISettings.decode(try OISettings().encoded()) == OISettings())
    }
    @Test func tolerantPerEntry() throws {
        let s = OISettings.decode(Data(##"{"hud":{"colors":{"ctrl":"#ff0000","option":{"dark":"#112233"},"cmd":"nope","shift":"#000000"}}}"##.utf8))
        #expect(s.hud.colors[.ctrl] == HUDColor(light: "#FF0000", dark: "#FF0000"))   // bare string = both variants
        #expect(s.hud.colors[.option] == HUDColor(light: "#112233", dark: "#112233"))  // missing variant takes the other
        #expect(s.hud.colors[.cmd] == HUDColors.defaults[.cmd])                         // bad value: default
        #expect(s.hud.colors[.fn] == HUDColors.defaults[.fn])                           // missing: default
        #expect(s.hud.slots == HUDSettings.defaultSlots)                                // slots untouched
        #expect(OISettings.decode(try s.encoded()).hud == s.hud)
        #expect(HUDColor.rgb("#FF8000")! == (1.0, 128.0 / 255, 0.0) && HUDColor.rgb("#12345") == nil)
        let junk = OISettings.decode(Data(#"{"hud":{"colors":7,"slots":{"ctrl":"top","alt":"left","cmd":"bottom","fn":"right"}}}"#.utf8))
        #expect(junk.hud.colors == HUDColors() && junk.hud.slots[.option] == .left)
    }
}

// MARK: - W11 wish by one key while the HUD is up

@Suite struct WishVisibleHotkeyTests {
    @Test func defaultIsAShiftTapAndRoundTrips() throws {
        let d = HotkeySet.defaults
        #expect(d.wishVisible == .tap(.shift) && d.wish == Hotkey(["option"], "`") && d.problems().isEmpty)
        let json = String(decoding: try OISettings().encoded(), as: UTF8.self)
        #expect(json.contains("\"wish_visible\" : {\n      \"tap\" : \"shift\""))
        let old = OISettings.decode(Data(#"{"hotkeys":{"v":3,"option_1":{"tap":"ctrl"}}}"#.utf8))
        #expect(old.hotkeys.wishVisible == .tap(.shift))                       // a file without it: default
        var h = HotkeySet.defaults
        h.wishVisible = .tap(.fn); h.option1 = Hotkey(["option"], "1")
        #expect(OISettings.decode(try OISettings(hotkeys: h).encoded()).hotkeys.wishVisible == .tap(.fn))
    }
    @Test func tapOnlyAndCollisions() {
        let chord = OISettings.decode(Data(#"{"hotkeys":{"v":3,"wish_visible":{"modifiers":["option"],"key":"w"}}}"#.utf8))
        #expect(chord.hotkeys.wishVisible == .tap(.shift))                     // a chord is not accepted
        var h = HotkeySet.defaults
        h.wishVisible = .tap(.ctrl)                                            // same tap as option 2 (W21)
        #expect(h.problems() == ["Wish while the HUD is up collides with Option 2 (⌃ tap)"])
        h = .defaults
        h.wishVisible = Hotkey(["option"], "w")
        #expect(h.problems() == ["Wish while the HUD is up: a single modifier tap only"])
        h = .defaults
        h.summon = .tap(.shift)                                                // summon (hidden) + wish (visible) may share ⇧
        #expect(h.problems().isEmpty)
        #expect(!HotkeySet.allowsChord("wish_visible") && HotkeySet.allowsChord("summon"))
        #expect(HotkeySet.tapPresets("wish_visible").first == .tap(.shift))
    }
}

// MARK: - W12 "Send a vocal to…"

@Suite struct VocalHotkeyTests {
    @Test func defaultIsTheCtrlOptionSetTapAndRoundTrips() throws {
        let d = HotkeySet.defaults
        #expect(d.vocal == .tapSet([.ctrl, .option]) && d.vocal.display == "⌃⌥" && d.problems().isEmpty)
        let json = String(decoding: try OISettings().encoded(), as: UTF8.self)
        #expect(json.contains("\"vocal\" : {\n      \"tap\" : [\n        \"ctrl\",\n        \"option\""))
        let old = OISettings.decode(Data(#"{"hotkeys":{"v":3}}"#.utf8))
        #expect(old.hotkeys.vocal == HotkeySet.defaultVocal)                     // a file without it: default
        var h = HotkeySet.defaults
        h.vocal = Hotkey(["control", "option"], "v")
        #expect(h.problems().isEmpty)
        #expect(OISettings.decode(try OISettings(hotkeys: h).encoded()).hotkeys.vocal == h.vocal)
    }
    @Test func singleTapIsRefusedSetsAndChordsAccepted() {
        let single = OISettings.decode(Data(#"{"hotkeys":{"v":3,"vocal":{"tap":"fn"}}}"#.utf8))
        #expect(single.hotkeys.vocal == HotkeySet.defaultVocal)                  // a single tap: default on read
        var h = HotkeySet.defaults
        h.vocal = .tap(.fn)
        #expect(h.problems().contains { $0.hasPrefix("Send a vocal to… (HUD up): a set of 2-3 modifiers") })
        h.vocal = .tapSet([.option, .cmd])                                       // the summon set
        #expect(h.problems() == ["Send a vocal to… (HUD up) collides with Summon HUD (show the options now) (⌥⌘ tap)"])
        h.vocal = .tapSet([.ctrl, .cmd, .shift])
        #expect(h.problems().isEmpty)
        #expect(HotkeySet.allowsTap("vocal", .tapSet([.ctrl, .fn])) && !HotkeySet.allowsTap("vocal", .tap(.ctrl)))
        #expect(HotkeySet.tapPresets("vocal") == [HotkeySet.defaultVocal] && HotkeySet.capturesTapSet("vocal"))
        #expect(HotkeySet.chordFallback("vocal") == Hotkey(["control", "option"], "v"))
        #expect(HotkeySet.actions.last?.key == "vocal")
    }
    @Test func vocalRecipientsAreKeptVerbatim() throws {
        let raw = #"{"vocal_recipients":{"nova":{"kind":"inject","path":"/tmp/x"},"n":[1,true,null]},"paused":true}"#
        let s = OISettings.decode(Data(raw.utf8))
        guard case .object(let o)? = s.vocalRecipients else { Issue.record("not kept"); return }
        #expect(o["n"] == .array([.number(1), .bool(true), .null]))
        let again = OISettings.decode(try s.encoded())
        #expect(again.vocalRecipients == s.vocalRecipients && again.paused)
        #expect(OISettings.decode(Data("{}".utf8)).vocalRecipients == nil)
        #expect(!String(decoding: try OISettings().encoded(), as: UTF8.self).contains("vocal_recipients"))
    }
}

// MARK: - W14 long-press + the python `claude` section

@Suite struct W14SettingsTests {
    @Test func longPressDefaultClampAndRoundTrip() throws {
        #expect(OISettings().hud.longPressMs == 600)
        #expect(OISettings.decode(Data(#"{"hud":{"long_press_ms":800}}"#.utf8)).hud.longPressMs == 800)
        #expect(OISettings.decode(Data(#"{"hud":{"long_press_ms":100}}"#.utf8)).hud.longPressMs == 400)
        #expect(OISettings.decode(Data(#"{"hud":{"long_press_ms":99999}}"#.utf8)).hud.longPressMs == 3000)
        #expect(OISettings.decode(Data(#"{"hud":{"long_press_ms":"x"}}"#.utf8)).hud.longPressMs == 600)
        let s = OISettings.decode(Data(#"{"hud":{"long_press_ms":750}}"#.utf8))
        #expect(OISettings.decode(try s.encoded()).hud.longPressMs == 750)
        // an invalid slot map still keeps the long-press value
        let bad = OISettings.decode(Data(#"{"hud":{"slots":{"ctrl":"left"},"long_press_ms":900}}"#.utf8))
        #expect(bad.hud.slots == HUDSettings.defaultSlots && bad.hud.longPressMs == 900)
    }
    @Test func dwellDefaultClampAndRoundTrip() throws {   // W15
        #expect(OISettings().hud.dwellMs == 500)
        #expect(OISettings.decode(Data(#"{"hud":{"dwell_ms":700}}"#.utf8)).hud.dwellMs == 700)
        #expect(OISettings.decode(Data(#"{"hud":{"dwell_ms":10}}"#.utf8)).hud.dwellMs == 250)
        #expect(OISettings.decode(Data(#"{"hud":{"dwell_ms":9000}}"#.utf8)).hud.dwellMs == 2000)
        #expect(OISettings.decode(Data(#"{"hud":{"dwell_ms":"x","long_press_ms":800}}"#.utf8)).hud.dwellMs == 500)
        let s = OISettings.decode(Data(#"{"hud":{"dwell_ms":650}}"#.utf8))
        let back = OISettings.decode(try s.encoded()).hud
        #expect(back.dwellMs == 650 && back.longPressMs == 600)
    }
    // W19
    @Test func w19AutoModeAndHintTTL() throws {
        let d = OISettings().hud
        #expect(d.autoMode == "hint" && d.hintTTLms == 4000)   // W20: 2500 -> 4000
        #expect(OISettings.decode(Data(#"{"hud":{"auto_mode":"full"}}"#.utf8)).hud.autoMode == "full")
        #expect(OISettings.decode(Data(#"{"hud":{"auto_mode":"off"}}"#.utf8)).hud.autoMode == "off")
        #expect(OISettings.decode(Data(#"{"hud":{"auto_mode":"loud"}}"#.utf8)).hud.autoMode == "hint")
        #expect(OISettings.decode(Data(#"{"hud":{"auto_mode":3}}"#.utf8)).hud.autoMode == "hint")
        #expect(OISettings.decode(Data(#"{"hud":{"hint_ttl_ms":100}}"#.utf8)).hud.hintTTLms == 500)
        #expect(OISettings.decode(Data(#"{"hud":{"hint_ttl_ms":99999}}"#.utf8)).hud.hintTTLms == 10_000)
        // a menu toggle (decode → encode) keeps both
        let s = OISettings.decode(Data(#"{"hud":{"auto_mode":"off","hint_ttl_ms":4000,"dwell_ms":700}}"#.utf8))
        let back = OISettings.decode(try s.encoded()).hud
        #expect(back.autoMode == "off" && back.hintTTLms == 4000 && back.dwellMs == 700)
    }
    // end W19
    // W20
    @Test func w20HintDotSizeAndTTLDefaults() throws {
        let d = OISettings().hud
        #expect(d.hintDotSize == 10 && d.hintTTLms == 4000)
        #expect(OISettings.decode(Data(#"{"hud":{"hint_dot_size":1}}"#.utf8)).hud.hintDotSize == 4)
        #expect(OISettings.decode(Data(#"{"hud":{"hint_dot_size":99}}"#.utf8)).hud.hintDotSize == 24)
        #expect(OISettings.decode(Data(#"{"hud":{"hint_dot_size":"big"}}"#.utf8)).hud.hintDotSize == 10)
        #expect(OISettings.decode(Data(#"{"hud":{"hint_dot_size":12.6}}"#.utf8)).hud.hintDotSize == 12)
        // written in full by a menu toggle
        let obj = try JSONSerialization.jsonObject(with: try OISettings().encoded()) as? [String: Any]
        let hud = obj?["hud"] as? [String: Any]
        #expect(hud?["hint_dot_size"] as? Int == 10 && hud?["hint_ttl_ms"] as? Int == 4000)
    }
    @Test func w20RoundTripKeepsUnknownGateKeysAndDotSize() throws {
        // gate.hint_cooldown_s belongs to the python driver: kept verbatim (with any other unknown gate key)
        let raw = #"{"gate":{"hint_cooldown_s":8,"cooldown_s":20,"future_knob":1.5},"hud":{"hint_dot_size":14,"auto_mode":"full"},"claude":{"x":1}}"#
        let s = OISettings.decode(Data(raw.utf8))
        let back = OISettings.decode(try s.encoded())
        #expect(back.gate?["hint_cooldown_s"]?.number == 8 && back.gate?["future_knob"]?.number == 1.5)
        #expect(back.gate?["cooldown_s"]?.number == 20)
        #expect(back.hud.hintDotSize == 14 && back.hud.autoMode == "full")
        #expect(back.claude == s.claude)
    }
    @Test func claudeSectionKeptVerbatim() throws {
        let json = #"{"claude":{"enabled":true,"local_only_domains":["client.example"],"debug":false}}"#
        let s = OISettings.decode(Data(json.utf8))
        #expect(s.claude == .object(["enabled": .bool(true), "local_only_domains": .array([.string("client.example")]),
                                     "debug": .bool(false)]))
        #expect(OISettings.decode(try s.encoded()).claude == s.claude)
        #expect(!String(decoding: try OISettings().encoded(), as: UTF8.self).contains("\"claude\""))
    }
}

// MARK: - W17 the python `shots` / `contact_map` sections + the watcher's flags

@Suite struct ShotSettingsTests {
    @Test func shotsAndContactMapKeptVerbatim() throws {
        let json = #"{"shots":{"enabled":true,"clipboard":false,"fallback_contact":null},"contact_map":{"Sky":"echo"}}"#
        let s = OISettings.decode(Data(json.utf8))
        #expect(s.contactMap == .object(["Sky": .string("echo")]))
        let back = OISettings.decode(try s.encoded())
        #expect(back.shots == s.shots && back.contactMap == s.contactMap)
        #expect(!String(decoding: try OISettings().encoded(), as: UTF8.self).contains("\"shots\""))
    }

    @Test func shotFlags() {
        #expect(OISettings().shotFlags == (true, true))
        #expect(OISettings.decode(Data(#"{"shots":{"clipboard":false}}"#.utf8)).shotFlags == (true, false))
        #expect(OISettings.decode(Data(#"{"shots":{"enabled":false,"clipboard":true}}"#.utf8)).shotFlags == (false, false))
        #expect(OISettings.decode(Data(#"{"shots":{"enabled":"x"}}"#.utf8)).shotFlags == (true, true))
    }
}

// MARK: - W21 number = key (fn 1, ⌃ 2, ⌥ 3, ⌘ 4)

@Suite struct W21NumberIsKeyTests {
    static let w9File = #"{"hotkeys":{"v":3,"option_1":{"tap":"ctrl"},"option_2":{"tap":"option"},"option_3":{"tap":"cmd"},"option_4":{"tap":"fn"},"summon":{"tap":["option","cmd"]}}}"#

    @Test func oldDefaultsAreMigratedOnceToTheNewNumbers() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("oi-w21-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        try Data(W21NumberIsKeyTests.w9File.utf8).write(to: url)
        let s = SettingsStore.load(url)
        #expect([s.hotkeys.option1, s.hotkeys.option2, s.hotkeys.option3, s.hotkeys.option4] == [.tap(.fn), .tap(.ctrl), .tap(.option), .tap(.cmd)])
        try SettingsStore.save(s, to: url)                                  // the next save writes v4 ...
        let json = String(decoding: try Data(contentsOf: url), as: UTF8.self)
        #expect(json.contains("\"v\" : 4") && json.contains("\"option_1\" : {\n      \"tap\" : \"fn\""))
        // ... so a v4 file is never renumbered again: ⌃ ⌥ ⌘ fn chosen ON PURPOSE in v4 is kept as is
        let chosen = W21NumberIsKeyTests.w9File.replacingOccurrences(of: #""v":3"#, with: #""v":4"#)
        let kept = OISettings.decode(Data(chosen.utf8)).hotkeys
        #expect([kept.option1, kept.option2, kept.option3, kept.option4] == [.tap(.ctrl), .tap(.option), .tap(.cmd), .tap(.fn)])
    }
    @Test func customOptionsAreNotMigrated() {
        // below v4 but not the exact W9 defaults: a deliberate choice, kept
        let custom = W21NumberIsKeyTests.w9File.replacingOccurrences(of: #""option_1":{"tap":"ctrl"},"option_2":{"tap":"option"}"#,
                                                                       with: #""option_1":{"tap":"option"},"option_2":{"tap":"ctrl"}"#)
        let h = OISettings.decode(Data(custom.utf8)).hotkeys
        #expect(h.option1 == .tap(.option) && h.option2 == .tap(.ctrl) && h.option4 == .tap(.fn))
        #expect(OISettings.decode(Data(#"{"hotkeys":{"v":3}}"#.utf8)).hotkeys.option1 == .tap(.fn))   // missing = new default
    }
    @Test func hotkeysWindowLabelsSpellTheKey() {
        #expect(HotkeySet.windowLabel("option_1") == "Option 1 (fn)")
        #expect(HotkeySet.windowLabel("option_2") == "Option 2 (⌃ control)")
        #expect(HotkeySet.windowLabel("option_3") == "Option 3 (⌥ option)")
        #expect(HotkeySet.windowLabel("option_4") == "Option 4 (⌘ command)")
        // each label names the modifier that is that option's default tap
        for n in 1...4 {
            let tap = HotkeySet.defaults.option(n)!.tap!
            #expect(HotkeySet.windowLabel("option_\(n)").contains(tap.symbol))
        }
    }
    @Test func fillOrderDefaultIsControlFirstAndRoundTrips() throws {
        let d = HUDSettings()
        #expect(d.fillOrder == [.ctrl, .option, .cmd, .fn] && d.fillKeys == [2, 3, 4, 1])
        let json = String(decoding: try OISettings().encoded(), as: UTF8.self)
        #expect(json.filter { !$0.isWhitespace }.contains(#""fill_order":["ctrl","option","cmd","fn"]"#))
        let custom = OISettings.decode(Data(#"{"hud":{"fill_order":["fn","command","alt","control"],"dwell_ms":700}}"#.utf8)).hud
        #expect(custom.fillOrder == [.fn, .cmd, .option, .ctrl] && custom.fillKeys == [1, 4, 3, 2] && custom.dwellMs == 700)
        #expect(OISettings.decode(try OISettings(hud: custom).encoded()).hud.fillOrder == [.fn, .cmd, .option, .ctrl])
        for bad in [#"["ctrl","option","cmd"]"#, #"["ctrl","ctrl","cmd","fn"]"#, #"["ctrl","option","cmd","shift"]"#, #""ctrl""#, "3"] {
            let h = OISettings.decode(Data(#"{"hud":{"fill_order":\#(bad)}}"#.utf8)).hud
            #expect(h.fillOrder == HUDSettings.defaultFillOrder)
        }
    }
}
