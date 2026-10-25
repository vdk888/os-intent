import Foundation
import Testing
@testable import HUDCore
import OISettings

@Suite struct LayoutTests {
    // W10: fixed places keyed to the modifiers, clockwise from the left in keyboard order:
    // fn left, ⌃ top, ⌥ right, ⌘ bottom (option 1 = ⌃ top, 2 = ⌥ right, 3 = ⌘ bottom, 4 = fn left).
    let defaultPositions = FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots)
    func opts(_ keys: [Int]) -> [HUDOption] { keys.map { HUDOption(key: $0, label: "o\($0)") } }
    func place(_ keys: [Int], _ pos: [Int: SlotPosition]? = nil) -> [Int: SlotPosition?] {
        Dictionary(uniqueKeysWithValues: hudSlots(options: opts(keys), positions: pos ?? defaultPositions, radius: 100,
                                                  capsule: (168, 40), wishWidth: 262).map { ($0.key, $0.position) })
    }

    @Test func defaultPlacesFollowTheModifiers() {
        #expect(defaultPositions == [1: .left, 2: .top, 3: .right, 4: .bottom])   // W21: 1 fn, 2 ⌃, 3 ⌥, 4 ⌘ clockwise
        #expect(FixedSlots.offset(.left, radius: 100) == (-100, 0) && FixedSlots.offset(.top, radius: 100) == (0, -100))
        #expect(FixedSlots.offset(.right, radius: 100) == (100, 0) && FixedSlots.offset(.bottom, radius: 100) == (0, 100))
    }
    @Test func placesDoNotDependOnHowManyOptions() {
        #expect(place([2]) == [2: .top])
        #expect(place([2, 3]) == [2: .top, 3: .right])
        #expect(place([2, 3, 4]) == [2: .top, 3: .right, 4: .bottom])          // W21: 3 options, fn's place empty
        #expect(place([1, 2, 3, 4]) == [1: .left, 2: .top, 3: .right, 4: .bottom])
    }
    @Test func missingOptionTwoLeavesTheRightEmpty() {
        #expect(place([1, 2, 4]) == [1: .left, 2: .top, 4: .bottom])        // no re-flow into the right
        #expect(place([4, 1, 2]) == [4: .bottom, 1: .left, 2: .top])        // input order does not matter
    }
    @Test func placesFollowCustomTapsAndSlots() {
        var h = HotkeySet.defaults
        h.option2 = .tap(.cmd); h.option4 = .tap(.ctrl)                    // option 2 on ⌘: it sits at the bottom
        #expect(FixedSlots.positions(hotkeys: h, slots: HUDSettings.defaultSlots) == [1: .left, 2: .bottom, 3: .right, 4: .top])
        let custom: [TapModifier: SlotPosition] = [.ctrl: .top, .option: .left, .cmd: .bottom, .fn: .right]
        #expect(FixedSlots.positions(hotkeys: .defaults, slots: custom) == [1: .right, 2: .top, 3: .left, 4: .bottom])
        h = .defaults
        h.option3 = Hotkey(["option"], "3")                                // a chord keeps its default place
        #expect(FixedSlots.positions(hotkeys: h, slots: HUDSettings.defaultSlots)[3] == .right)
        h.option3 = Hotkey(["option"], "3"); h.option2 = Hotkey(["control"], "2"); h.option1 = .tap(.ctrl)
        let p = FixedSlots.positions(hotkeys: h, slots: HUDSettings.defaultSlots)   // ⌃ tap taken by option 1
        #expect(p[1] == .top && p[3] == .right && p[2] == .left && Set(p.values).count == 4)
    }
    @Test func leashHoldsInsideAndFollowsOutside() {
        let c = (x: 0.0, y: 0.0)
        let inside = leashStep(centre: c, pointer: (100, 0), leash: 200, factor: 0.25)
        #expect(inside.x == 0 && inside.y == 0)
        let out = leashStep(centre: c, pointer: (300, 0), leash: 200, factor: 0.25)
        #expect(abs(out.x - 25) < 1e-9 && out.y == 0)
    }
    @Test func clampKeepsPanelOnScreen() {
        let c = clampCentre((5, 1000), screen: (0, 0, 1000, 800), halfSize: (230, 160))
        #expect(c.x == 230 && c.y == 640)
    }
}

@Suite struct CommandTests {
    @Test func decodesShowSortedWithDefaults() throws {
        let line = #"{"cmd":"show","options":[{"key":2,"label":"Mail","icon":"app:/System/Applications/Mail.app"},{"key":1,"label":"Back","icon":"sf:arrowshape.turn.up.left","confidence":0.41}]}"#
        let cmd = try decodeCommand(Data(line.utf8))
        guard case .show(let opts, let ttl) = cmd else { Issue.record("not show"); return }
        #expect(ttl == 6000)    // W19: default 6 s (W9 was 15 s)
        #expect(opts.map(\.key) == [1, 2])
        #expect(opts[0].confidence == 0.41)
        #expect(HUDIcon(opts[1].icon) == .app(path: "/System/Applications/Mail.app"))
        #expect(HUDIcon(opts[0].icon) == .sf(name: "arrowshape.turn.up.left"))
        #expect(HUDIcon(nil) == .none)
    }
    @Test func decodesHide() throws {
        #expect(try decodeCommand(Data(#"{"cmd":"hide"}"#.utf8)) == .hide)
    }
    @Test func rejectsBadInput() {
        #expect(throws: CommandError.badJSON) { try decodeCommand(Data("nope".utf8)) }
        #expect(throws: CommandError.unknownCommand("dance")) { try decodeCommand(Data(#"{"cmd":"dance"}"#.utf8)) }
        #expect(throws: CommandError.optionCount(0)) { try decodeCommand(Data(#"{"cmd":"show"}"#.utf8)) }
        let five = (1...5).map { #"{"key":\#($0),"label":"x"}"# }.joined(separator: ",")
        #expect(throws: CommandError.optionCount(5)) { try decodeCommand(Data(#"{"cmd":"show","options":[\#(five)]}"#.utf8)) }
        #expect(throws: CommandError.badKey(7)) { try decodeCommand(Data(#"{"cmd":"show","options":[{"key":7,"label":"x"}]}"#.utf8)) }
        #expect(throws: CommandError.duplicateKey(1)) { try decodeCommand(Data(#"{"cmd":"show","options":[{"key":1,"label":"a"},{"key":1,"label":"b"}]}"#.utf8)) }
        #expect(throws: CommandError.badTTL(5)) { try decodeCommand(Data(#"{"cmd":"show","ttl_ms":5,"options":[{"key":1,"label":"a"}]}"#.utf8)) }
    }
    @Test func eventsAreOneJSONLine() {
        #expect(HUDEvent.pressed(key: 2).jsonLine() == #"{"event":"pressed","key":2}"# + "\n")
        #expect(HUDEvent.dismissed(reason: "ttl").jsonLine() == #"{"event":"dismissed","reason":"ttl"}"# + "\n")
        #expect(HUDEvent.dropped(reason: "rate_limited").jsonLine() == #"{"event":"dropped","reason":"rate_limited"}"# + "\n")
    }
}

@Suite struct GateTests {
    @Test func rateLimiterOnePerInterval() {
        var r = RateLimiter(minInterval: 2)
        let got = [10, 11.9, 12.0, 13].map { r.allow(now: $0) }
        #expect(got == [true, false, true, false])
    }
    @Test func velocityGateNeedsSustainedSpeed() {
        var g = VelocityGate(speedThreshold: 600, holdTime: 0.1)
        let dt = 1.0 / 60
        // 1000 px/s: trips once fast for >= 100 ms (7th sample), not before.
        var tripped: [Bool] = []
        for i in 0...8 { tripped.append(g.feed(t: Double(i) * dt, x: Double(i) * 1000 * dt, y: 0)) }
        #expect(tripped[0...5].allSatisfy { !$0 })
        #expect(tripped[6] && tripped[7])
    }
    @Test func w9DefaultsAndGrace() {
        let g0 = VelocityGate(PointerHide())
        #expect(g0.speedThreshold == 1200 && g0.holdTime == 0.25 && g0.grace == 2.0)
        let dt = 1.0 / 60
        // 1000 px/s never trips the default gate
        var a = VelocityGate(PointerHide()); a.reset(shownAt: 0)
        #expect(!(0..<600).map { a.feed(t: Double($0) * dt, x: Double($0) * 1000 * dt, y: 0) }.contains(true))
        // 3000 px/s from the show on: nothing before 2 s, trips 250 ms after the grace ends
        var g = VelocityGate(PointerHide()); g.reset(shownAt: 0)
        var firstTrip: Double?
        for i in 0..<300 {
            let t = Double(i) * dt
            if g.feed(t: t, x: t * 3000, y: 0), firstTrip == nil { firstTrip = t }
        }
        let ft = try! #require(firstTrip)
        #expect(ft >= 2.0 + 0.25 && ft < 2.0 + 0.25 + 3 * dt)
        // a short fast flick (200 ms at 3000 px/s) after the grace never hides
        var f = VelocityGate(PointerHide()); f.reset(shownAt: 0)
        var x = 0.0, flicked = false
        for i in 0..<400 {
            let t = Double(i) * dt
            let fast = t >= 3.0 && t < 3.2
            x += fast ? 3000 * dt : 2
            flicked = flicked || f.feed(t: t, x: x, y: 0)
        }
        #expect(!flicked)
        #expect(effectiveTTL(ms: 15000) == 15 && effectiveTTL(ms: 300) == 1.5)
    }
    @Test func velocityGateResetsOnSlowSample() {
        var g = VelocityGate(speedThreshold: 600, holdTime: 0.1)
        let dt = 1.0 / 60
        var x = 0.0, any = false
        for i in 0..<30 {
            x += (i % 5 == 4) ? 1 : 1000 * dt   // a slow frame every 5th sample breaks the run
            any = any || g.feed(t: Double(i) * dt, x: x, y: 0)
        }
        #expect(!any)
        // a slow pointer never trips
        var s = VelocityGate()
        let slow = (0..<60).map { s.feed(t: Double($0) * dt, x: Double($0) * 300 * dt, y: 0) }
        #expect(!slow.contains(true))
    }
}

@Suite struct EnabledGateTests {
    @Test func disabledDropsWithReason() {
        #expect(EnabledGate.check(hudEnabled: true) == nil)
        #expect(EnabledGate.check(hudEnabled: false) == .dropped(reason: "disabled"))
        #expect(EnabledGate.check(hudEnabled: false)!.jsonLine() == #"{"event":"dropped","reason":"disabled"}"# + "\n")
    }
}

@Suite struct HotkeyPlanTests {
    @Test func defaultsAreTapsPlusEsc() {
        let p = hotkeyPlan(optionKeys: [1, 3], hotkeys: .defaults)
        #expect(!p.fellBack)
        #expect(p.regs == [HotkeyRegistration(code: 0x35, mods: 0, id: dismissHotkeyID)])   // Esc only
        #expect(p.taps == [.fn: 1, .option: 3, .shift: wishOptionKey])   // W21: 1 = fn, 3 = ⌥
    }
    @Test func tapsFallBackToOptionDigitsWithoutTheEventTap() {
        let p = hotkeyPlan(optionKeys: [1, 3], hotkeys: .defaults, tapsAvailable: false)
        #expect(p.taps.isEmpty)
        #expect(p.regs == [HotkeyRegistration(code: 0x12, mods: 0x0800, id: 1),   // ⌥1 (kVK_ANSI_1)
                           HotkeyRegistration(code: 0x14, mods: 0x0800, id: 3),   // ⌥3
                           HotkeyRegistration(code: 0x35, mods: 0, id: dismissHotkeyID)])
    }
    @Test func fallbackChordNeverShadowsAnotherAction() {
        var h = HotkeySet.defaults
        h.dismiss = Hotkey(["option"], "2")                    // ⌥2 is taken by dismiss
        let p = hotkeyPlan(optionKeys: [1, 2], hotkeys: h, tapsAvailable: false)
        #expect(p.regs.map(\.id) == [1, dismissHotkeyID])
    }
    @Test func customFromSettingsJSON() {
        let json = #"{"hotkeys":{"option_1":{"modifiers":["control","shift"],"key":"j"},"dismiss":{"modifiers":["option"],"key":"q"}}}"#
        let s = OISettings.decode(Data(json.utf8))
        let p = hotkeyPlan(optionKeys: [1, 2], hotkeys: s.hotkeys)
        #expect(!p.fellBack)
        #expect(p.regs == [HotkeyRegistration(code: 0x26, mods: 0x1200, id: 1),   // ⌃⇧J
                           HotkeyRegistration(code: 0x0C, mods: 0x0800, id: dismissHotkeyID)])
        #expect(p.taps == [.ctrl: 2, .shift: wishOptionKey])                                           // option 2 = default ⌃ tap (W21)
        #expect(!p.regs.contains { $0.code == 0x32 })   // wish (⌥`) reserved, not registered
    }
    @Test func collisionFallsBackToDefaults() {
        var h = HotkeySet.defaults
        h.option1 = Hotkey(["option"], "1"); h.option2 = Hotkey(["option"], "1")
        let p = hotkeyPlan(optionKeys: [1, 2], hotkeys: h)
        #expect(p.fellBack)
        #expect(p.regs.map(\.code) == [0x35])
        #expect(p.taps == [.fn: 1, .ctrl: 2, .shift: wishOptionKey])
    }
}

// MARK: - W9 modifier taps

@Suite struct ModifierTapTests {
    let ctrl = HeldModifiers(cgEventFlags: 0x40000)
    let cmd = HeldModifiers(cgEventFlags: 0x100000)
    let opt = HeldModifiers(cgEventFlags: 0x80000)
    let fn = HeldModifiers(cgEventFlags: 0x800000)

    @Test func flagsMapping() {
        #expect(ctrl == .control && cmd == .command && opt == .option && fn == .fn)
        #expect(HeldModifiers(cgEventFlags: 0x20000) == .shift)
        #expect(HeldModifiers(cgEventFlags: 0x10000 | 0x200000) == [])        // caps lock, numpad ignored
        #expect(HeldModifiers(cgEventFlags: 0x100000 | 0x80000) == [.command, .option])
    }
    @Test func cleanTapsMapToTheirModifier() {
        for (m, want) in [(ctrl, TapModifier.ctrl), (opt, .option), (cmd, .cmd), (fn, .fn)] {
            var d = TapDetector()
            #expect(d.flagsChanged(m, at: 10.0) == nil)
            #expect(d.flagsChanged([], at: 10.12) == [want])
        }
    }
    @Test func timingBoundary() {
        var d = TapDetector()
        _ = d.flagsChanged(ctrl, at: 1.0)
        #expect(d.flagsChanged([], at: 1.35) == [.ctrl])          // 350 ms: still a tap
        _ = d.flagsChanged(ctrl, at: 2.0)
        #expect(d.flagsChanged([], at: 2.351) == nil)           // held too long
        _ = d.flagsChanged(ctrl, at: 3.0)                        // ... and the next one is clean again
        #expect(d.flagsChanged([], at: 3.1) == [.ctrl])
    }
    @Test func chordIsNotATap() {
        var d = TapDetector()
        _ = d.flagsChanged(cmd, at: 1.0)
        d.otherInput()                                           // ⌘C: the C keyDown
        #expect(d.flagsChanged([], at: 1.1) == nil)
        _ = d.flagsChanged(opt, at: 2.0)
        d.otherInput()                                           // ⌥` (wish) is a chord
        #expect(d.flagsChanged([], at: 2.05) == nil)
        _ = d.flagsChanged(cmd, at: 3.0)
        d.otherInput()                                           // ⌘-click
        #expect(d.flagsChanged([], at: 3.1) == nil)
        d.otherInput()                                           // typing with nothing held: no effect
        _ = d.flagsChanged(ctrl, at: 4.0)
        #expect(d.flagsChanged([], at: 4.1) == [.ctrl])
    }
    @Test func multiModifierIsNotASingleTap() {
        var d = TapDetector()
        _ = d.flagsChanged(ctrl, at: 1.0)
        _ = d.flagsChanged([.control, .option], at: 1.05)        // ⌃ then ⌥
        _ = d.flagsChanged(ctrl, at: 1.1)                        // ⌥ released first
        #expect(d.flagsChanged([], at: 1.15) == [.ctrl, .option]) // W11: a SET, never option 1 or 2
        _ = d.flagsChanged([.command, .shift], at: 2.0)          // pressed together
        #expect(d.flagsChanged([], at: 2.1) == nil)
        _ = d.flagsChanged([.shift, .control], at: 3.0)           // ⇧⌃ together: not a tap
        #expect(d.flagsChanged([], at: 3.1) == nil)
        _ = d.flagsChanged(opt, at: 4.0)                         // clean again afterwards
        #expect(d.flagsChanged([], at: 4.1) == [.option])
    }
    @Test func releaseOfAModifierHeldBeforeShowIsIgnored() {
        var d = TapDetector()                            // created on show while ⌘ is already down
        #expect(d.flagsChanged([], at: 1.0) == nil)
        d = TapDetector()
        _ = d.flagsChanged([.command, .control], at: 1.0)        // ⌘ held before show, ⌃ added
        _ = d.flagsChanged(ctrl, at: 1.05)
        #expect(d.flagsChanged([], at: 1.1) == nil)
    }
}

// MARK: - W6 wish quick action

private func tmpDataDir() -> URL {
    let d = FileManager.default.temporaryDirectory.appendingPathComponent("oi-hud-wish-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
    return d
}

@Suite struct WishRowTests {
    let date = Date(timeIntervalSince1970: 1_792_486_800.25)   // 2026-10-20T09:00:00.250Z

    @Test func encodesTheContractRow() throws {
        let ctx = WishContext(app: "Safari", space: 2, hudVisible: true, hudOptions: ["Reply", "Archive"])
        let row = try #require(WishRow(date: date, rawText: "  ouvrir le mail de Paul \n", source: .dictated, context: ctx))
        let line = try row.jsonLine()
        #expect(line.hasSuffix("\n"))
        #expect(line == #"{"context":{"app":"Safari","hud_options":["Reply","Archive"],"hud_visible":true,"space":2},"source":"dictated","text":"ouvrir le mail de Paul","ts":"2026-10-20T09:00:00.250Z"}"# + "\n")
    }

    @Test func nullsAreExplicit() throws {
        let row = try #require(WishRow(date: date, rawText: "x", source: .typed,
                                       context: WishContext(app: nil, space: nil, hudVisible: false, hudOptions: [])))
        let line = try row.jsonLine()
        #expect(line.contains(#""app":null"#) && line.contains(#""space":null"#) && line.contains(#""hud_options":[]"#))
    }

    @Test func emptyIsNotAWish() {
        #expect(WishRow(date: date, rawText: " \n\t ", source: .typed,
                        context: WishContext(app: nil, space: nil, hudVisible: false, hudOptions: [])) == nil)
        #expect(WishRow.clean("a\nb") == "a b")
        #expect(WishRow.clean(String(repeating: "x", count: 900))?.count == wishMaxChars)
    }

    @Test func storeAppendsPrivateDayFile() throws {
        let dataDir = tmpDataDir()
        let ctx = WishContext(app: nil, space: nil, hudVisible: false, hudOptions: [])
        let r1 = try #require(WishRow(date: date, rawText: "one", source: .typed, context: ctx))
        let r2 = try #require(WishRow(date: date, rawText: "two", source: .typed, context: ctx))
        let url = try WishStore.append(r1, date: date, dataDir: dataDir)
        try WishStore.append(r2, date: date, dataDir: dataDir)
        #expect(url.lastPathComponent == "2026-10-20.jsonl")
        #expect(url.deletingLastPathComponent().lastPathComponent == "wishes")
        let lines = try String(contentsOf: url, encoding: .utf8).split(separator: "\n")
        #expect(lines.count == 2)
        let back = try JSONDecoder().decode(WishRow.self, from: Data(lines[1].utf8))
        #expect(back == r2)
        let fileMode = (try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as? NSNumber)?.intValue
        let dirMode = (try FileManager.default.attributesOfItem(atPath: url.deletingLastPathComponent().path)[.posixPermissions] as? NSNumber)?.intValue
        #expect(fileMode == 0o600 && dirMode == 0o700)
    }

    @Test func dayIsUTC() {
        // 23:30 UTC on the 20th is already the 21st in Paris: the file follows UTC (like tokens/)
        #expect(WishStore.day(Date(timeIntervalSince1970: 1_792_539_000)) == "2026-10-20")
    }

    @Test func wishEventCarriesLengthOnly() {
        #expect(HUDEvent.wish(textLen: 12).jsonLine() == #"{"event":"wish","text_len":12}"# + "\n")
    }
}

@Suite struct WishHotkeyTests {
    @Test func defaultIsOptionBacktickWithItsOwnID() throws {
        let r = try #require(wishRegistration(hotkeys: .defaults))
        #expect(r.code == 0x32 && r.mods == 0x0800 && r.id == wishHotkeyID)
        #expect(r.id != dismissHotkeyID && !(1...4).contains(Int(r.id)))
    }

    @Test func staysRegisteredWhenHUDDisabledOrPaused() throws {
        var t = WishHotkeyTracker()
        let synced = t.sync(OISettings())
        let first = try #require(synced)
        #expect(t.sync(OISettings(hudEnabled: false)) == nil)            // no change: still registered
        #expect(t.sync(OISettings(paused: true, hudEnabled: false)) == nil)
        #expect(t.current == first)
    }

    @Test func followsACustomComboAndFallsBackOnCollision() throws {
        var t = WishHotkeyTracker()
        _ = t.sync(OISettings())
        var h = HotkeySet.defaults
        h.wish = Hotkey(["control", "option"], "w")
        let c1 = t.sync(OISettings(hotkeys: h))
        let custom = try #require(c1)
        #expect(custom.code == 0x0D && custom.mods == 0x1800)
        h.option1 = Hotkey(["option"], "1")
        h.wish = Hotkey(["option"], "1")                                   // collides with option 1
        let c2 = t.sync(OISettings(hotkeys: h))
        let back = try #require(c2)
        #expect(back == wishRegistration(hotkeys: .defaults))
        #expect(WishHotkeyTracker.display(OISettings(hotkeys: h)) == "⌥`")
    }

    @Test func hudPlanNeverContainsTheWishKey() {
        let plan = hotkeyPlan(optionKeys: [1, 2], hotkeys: .defaults)
        #expect(!plan.regs.contains { $0.id == wishHotkeyID })
    }
}

@Suite struct WishPanelMachineTests {
    @Test func typedPath() {
        var m = WishPanelMachine()
        #expect(m.handle(.enter) == [])                 // closed: ignored
        #expect(m.handle(.hotkey) == [.open] && m.state == .typing)
        #expect(m.handle(.enter) == [.save] && m.state == .closed)
    }

    @Test func dictationToggles() {
        var m = WishPanelMachine()
        _ = m.handle(.hotkey)
        #expect(m.handle(.hotkey) == [.startDictation] && m.state == .dictating)   // 2nd press
        #expect(m.handle(.mic) == [.stopDictation] && m.state == .typing)
        #expect(m.handle(.mic) == [.startDictation])
        #expect(m.handle(.dictationEnded) == [.stopDictation] && m.state == .typing)   // 20 s / final / denied
        #expect(m.handle(.dictationEnded) == [])
    }

    @Test func enterWhileDictatingStopsThenSaves() {
        var m = WishPanelMachine()
        _ = m.handle(.hotkey); _ = m.handle(.mic)
        #expect(m.handle(.enter) == [.stopDictation, .save] && m.state == .closed)
    }

    @Test func escAndClickOutsideCancel() {
        var m = WishPanelMachine()
        _ = m.handle(.hotkey)
        #expect(m.handle(.clickOutside) == [.cancel] && m.state == .closed)
        _ = m.handle(.hotkey); _ = m.handle(.hotkey)
        #expect(m.handle(.escape) == [.stopDictation, .cancel] && m.state == .closed)
        #expect(m.handle(.escape) == [])
    }
}

@Suite struct WishHUDOptionTests {
    let four = (1...4).map { HUDOption(key: $0, label: "O\($0)") }

    @Test func wishOptionIsAppendedLast() {
        let o = optionsWithWish(four)
        #expect(o.count == 5 && o.last?.key == wishOptionKey && o.last?.label == "Tell me what you wanted")
        #expect(optionsWithWish([HUDOption(key: 2, label: "X")]).map(\.key) == [2, wishOptionKey])
        #expect(optionsWithWish(o).count == 5)          // idempotent
    }

    @Test func clientsCannotSendKeyZero() {
        #expect(throws: CommandError.badKey(0)) {
            try decodeCommand(Data(#"{"cmd":"show","options":[{"key":0,"label":"x"}]}"#.utf8))
        }
    }

    @Test func wishHasItsOwnFixedPlaceWithoutOverlap() {
        let all = hudSlots(options: optionsWithWish(four), positions: FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots),
                           radius: 100, capsule: (168, 40), wishWidth: 262)
        #expect(all.count == 5)
        let wish = all.last!
        #expect(wish.key == wishOptionKey && wish.position == nil && wish.x == 139 && wish.y == 156)   // ≈ 4:30
        // the 7:30 place (W12 vocal option) mirrors it
        let v = FixedSlots.vocalOffset(radius: 100, capsuleHeight: 40, vocalWidth: 262)
        #expect(v.x == -139 && v.y == 156)
        let vocal = HUDSlot(key: vocalOptionKey, x: v.x, y: v.y, width: 262, height: 40, position: nil)
        let boxes = all + [vocal]
        for (i, a) in boxes.enumerated() {   // no two rectangles intersect, the reserved one included
            for b in boxes[(i + 1)...] {
                #expect(abs(a.x - b.x) >= (a.width + b.width) / 2 || abs(a.y - b.y) >= (a.height + b.height) / 2)
            }
            #expect(abs(a.x) + a.width / 2 <= 280 && abs(a.y) + a.height / 2 <= 200)   // inside the 560×400 panel
        }
        let alone = hudSlots(options: optionsWithWish([]), positions: [:], radius: 100, capsule: (168, 40), wishWidth: 262)
        #expect(alone.count == 1 && alone[0].x == 139 && alone[0].y == 156)   // same place when alone
    }

    @Test func stateCommandAndReply() throws {
        #expect(try decodeCommand(Data(#"{"cmd":"state"}"#.utf8)) == .state)
        let info = HUDStateInfo(visible: true, options: optionsWithWish([HUDOption(key: 1, label: "Mail")]),
                                hudEnabled: true, wishHotkey: "⌥`", wishRegistered: true, wishPanel: "closed")
        let line = HUDEvent.state(info).jsonLine()
        #expect(line.contains(#"{"key":0,"label":"Tell me what you wanted"}"#))
        #expect(line.contains(#""wish_registered":true"#))
    }
}

@Suite struct WishCommandTests {
    @Test func decodesWishCommand() throws {
        #expect(try decodeCommand(Data(#"{"cmd":"wish","text":"open the invoice"}"#.utf8)) == .wish(text: "open the invoice", source: .typed))
        #expect(try decodeCommand(Data(#"{"cmd":"wish","text":"x","source":"dictated"}"#.utf8)) == .wish(text: "x", source: .dictated))
        #expect(throws: CommandError.emptyWish) { try decodeCommand(Data(#"{"cmd":"wish","text":"  "}"#.utf8)) }
        #expect(throws: CommandError.emptyWish) { try decodeCommand(Data(#"{"cmd":"wish"}"#.utf8)) }
    }
}

// MARK: - W10 summon key

@Suite struct SummonTests {
    let shift = HeldModifiers(cgEventFlags: 0x20000)

    @Test func sharedDetectorSeesABareShiftTap() {
        var d = TapDetector()                                    // the same type as the option taps
        _ = d.flagsChanged(shift, at: 1.0)
        var t = d.flagsChanged([], at: 1.1); #expect(t == [.shift])
        _ = d.flagsChanged(shift, at: 2.0)
        t = d.flagsChanged([], at: 2.4); #expect(t == nil)       // held 400 ms: not a tap
    }
    @Test func shiftLetterIsAChordNotASummon() {
        var d = TapDetector()
        _ = d.flagsChanged(shift, at: 1.0)
        d.otherInput()                                           // ⇧A: the A keyDown
        var t = d.flagsChanged([], at: 1.1); #expect(t == nil)
        _ = d.flagsChanged(shift, at: 2.0)
        d.otherInput()                                           // ⇧-click
        t = d.flagsChanged([], at: 2.05); #expect(t == nil)
        _ = d.flagsChanged(shift, at: 3.0)
        _ = d.flagsChanged([.shift, .command], at: 3.05)         // ⇧⌘…
        _ = d.flagsChanged(shift, at: 3.1)
        t = d.flagsChanged([], at: 3.15); #expect(t == [.shift, .cmd])   // a ⇧⌘ set, not a ⇧ tap
        _ = d.flagsChanged(shift, at: 4.0)                       // clean again afterwards
        t = d.flagsChanged([], at: 4.2); #expect(t == [.shift])
    }
    @Test func ignoreRules() {
        #expect(SummonGate.check(hudEnabled: true, paused: false, visible: false, wishOpen: false) == nil)
        #expect(SummonGate.check(hudEnabled: false, paused: false, visible: false, wishOpen: false) == "hud_disabled")
        #expect(SummonGate.check(hudEnabled: true, paused: true, visible: false, wishOpen: false) == "paused")
        #expect(SummonGate.check(hudEnabled: true, paused: false, visible: true, wishOpen: false) == "visible")
        #expect(SummonGate.check(hudEnabled: true, paused: false, visible: false, wishOpen: true) == "wish_open")
        #expect(SummonGate.check(hudEnabled: false, paused: true, visible: true, wishOpen: true) == "hud_disabled")
    }
    @Test func fallbackTimer() {
        var w = SummonWindow()
        #expect(w.delay == 0.4 && !w.isOpen)
        var r = w.answer(); #expect(!r)                          // nothing pending
        r = w.fallbackDue(at: 5); #expect(!r)
        w.begin(at: 10)
        r = w.fallbackDue(at: 10.2); #expect(!r)                 // too early: still waiting for the show
        r = w.fallbackDue(at: 10.4); #expect(r)                  // no show in 400 ms: fallback
        r = w.fallbackDue(at: 10.5); #expect(!r && !w.isOpen)    // once
        r = w.answer(); #expect(!r)                              // a late show is an ordinary show
        w.begin(at: 20)
        r = w.answer(); #expect(r)                               // the live driver answered in time
        r = w.fallbackDue(at: 20.4); #expect(!r)                 // ... so no fallback
    }
    @Test func armFollowsSettings() {
        #expect(SummonArm(hotkeys: .defaults) == .tap([.option, .cmd]))
        var h = HotkeySet.defaults
        h.summon = Hotkey(["option", "shift"], "Space")
        #expect(SummonArm(hotkeys: h) == .chord(HotkeyRegistration(code: 0x31, mods: 0x0A00, id: summonHotkeyID)))
        #expect(SummonArm.display(h) == "⌥⇧Space")
        h.summon = .tap(.ctrl)                                   // collides with option 1: defaults
        #expect(SummonArm(hotkeys: h) == .tap([.option, .cmd]) && SummonArm.display(h) == "⌥⌘")
        // a summon chord is "taken": the ⌥n fallback for a tap slot never shadows it
        var g = HotkeySet.defaults
        g.summon = Hotkey(["option"], "2")
        let p = hotkeyPlan(optionKeys: [1, 2], hotkeys: g, tapsAvailable: false)
        #expect(p.regs.map(\.id) == [1, dismissHotkeyID])
    }
    @Test func protocolSummonAndEmptyShow() throws {
        #expect(HUDEvent.summon(ts: "2026-10-20T13:40:00.000Z").jsonLine()
                == #"{"event":"summon","ts":"2026-10-20T13:40:00.000Z"}"# + "\n")
        #expect(try decodeCommand(Data(#"{"cmd":"debug_summon"}"#.utf8)) == .debugSummon)
        // a summon the model has nothing for: 0 options = the wish option alone
        #expect(try decodeCommand(Data(#"{"cmd":"show","options":[],"ttl_ms":15000}"#.utf8)) == .show(options: [], ttlMs: 15000))
        #expect(optionsWithWish([]).map(\.key) == [wishOptionKey])
        let info = HUDStateInfo(visible: false, options: [], hudEnabled: true, wishHotkey: "⌥`", wishRegistered: true,
                                wishPanel: "closed", tapsActive: false, summonKey: "⇧", summonArmed: true)
        let line = HUDEvent.state(info).jsonLine()
        #expect(line.contains(#""taps_active":false"#) && line.contains(#""summon_key":"⇧""#) && line.contains(#""summon_armed":true"#))
    }
}

// MARK: - W11 summon = ⌥+⌘ set tap, scroll spoiler, late show after the fallback

@Suite struct SetTapSummonTests {
    let opt = HeldModifiers(cgEventFlags: 0x80000)
    let cmd = HeldModifiers(cgEventFlags: 0x100000)
    let optCmd = HeldModifiers(cgEventFlags: 0x80000 | 0x100000)
    var want: Set<TapModifier> { guard case .tap(let w) = SummonArm(hotkeys: .defaults) else { return [] }; return w }

    @Test func optionCommandTogetherSummons() {
        #expect(want == [.option, .cmd])
        var d = TapDetector()
        _ = d.flagsChanged(opt, at: 1.0)
        _ = d.flagsChanged(optCmd, at: 1.04)                     // ⌘ joins
        _ = d.flagsChanged(cmd, at: 1.2)                         // ⌥ up first
        #expect(d.flagsChanged([], at: 1.25) == want)             // both up: a ⌥+⌘ tap
        _ = d.flagsChanged(cmd, at: 2.0)                         // the other order works too
        _ = d.flagsChanged(optCmd, at: 2.05)
        _ = d.flagsChanged(opt, at: 2.3)
        #expect(d.flagsChanged([], at: 2.5) == want)              // 500 ms: still a set tap
    }
    @Test func optionAloneDoesNotSummon() {
        var d = TapDetector()
        _ = d.flagsChanged(opt, at: 1.0)
        let t = d.flagsChanged([], at: 1.1)
        #expect(t == [.option] && t != want)                      // an ⌥ tap: option 2 while visible, never summon
    }
    @Test func optionThenCommandSequentialDoesNotSummon() {
        var d = TapDetector()
        _ = d.flagsChanged(opt, at: 1.0)
        let a = d.flagsChanged([], at: 1.08)
        _ = d.flagsChanged(cmd, at: 1.12)
        let b = d.flagsChanged([], at: 1.2)
        #expect(a == [.option] && b == [.cmd])                    // two single taps, never the set
        #expect(a != want && b != want)
    }
    @Test func optionCommandCIsAChord() {
        var d = TapDetector()
        _ = d.flagsChanged(opt, at: 1.0)
        _ = d.flagsChanged(optCmd, at: 1.03)
        d.otherInput()                                           // the C keyDown
        #expect(d.flagsChanged([], at: 1.2) == nil)
    }
    @Test func slowSetIsNotATap() {
        var d = TapDetector()
        _ = d.flagsChanged(opt, at: 1.0)
        _ = d.flagsChanged(optCmd, at: 1.1)
        #expect(d.flagsChanged([], at: 1.501) == nil)             // > 500 ms first down → last up
    }
    @Test func scrollSpoilsATap() {
        #expect(TapDetector.spoilerEventTypes.contains(22))      // kCGEventScrollWheel is listened to
        #expect(Set(TapDetector.spoilerEventTypes) == [10, 1, 3, 25, 22])
        var d = TapDetector()
        _ = d.flagsChanged(HeldModifiers(cgEventFlags: 0x20000), at: 1.0)
        d.otherInput()                                           // ⇧+scroll (horizontal scroll)
        #expect(d.flagsChanged([], at: 1.1) == nil)
        _ = d.flagsChanged(optCmd.subtracting(cmd), at: 2.0)
        _ = d.flagsChanged(optCmd, at: 2.02)
        d.otherInput()                                           // ⌥⌘+scroll
        #expect(d.flagsChanged([], at: 2.1) == nil)
    }
    @Test func lateShowAfterFallbackReplacesItWithoutRateLimit() {
        var w = SummonWindow()
        w.begin(at: 10)
        var r = w.fallbackDue(at: 10.4); #expect(r)
        w.fallbackShown()                                        // the wish-only fallback is on screen
        #expect(w.fallbackOnScreen)
        r = w.answer(); #expect(r)                               // the late live show is still the answer
        #expect(!w.fallbackOnScreen)
        r = w.answer(); #expect(!r)                              // the next one is ordinary again
        w.begin(at: 20)
        r = w.fallbackDue(at: 20.4); #expect(r)
        w.fallbackShown(); w.hidden()                            // fallback dismissed before the show came
        r = w.answer(); #expect(!r)
    }
}

// MARK: - W11 colour per key, labels, hints

@Suite struct ColourAndLabelTests {
    let pos = FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots)

    @Test func defaultColourPerKey() {
        let hud = HUDSettings()
        let c = (1...4).map { OptionColors.color(forOption: $0, positions: pos, hud: hud)?.dark }
        #expect(c == ["#F5A9C8", "#9CC3FF", "#FFE08A", "#A6E3A1"])   // W21: 1 fn pink, 2 ⌃ blue, 3 ⌥ yellow, 4 ⌘ green
        #expect(OptionColors.color(forOption: wishOptionKey, positions: pos, hud: hud) == nil)
        // stable: the same key gets the same colour whatever else is shown
        let two = FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots)
        #expect(OptionColors.color(forOption: 2, positions: two, hud: hud)?.dark == "#9CC3FF")
        #expect(OptionColors.variant(hud.colors[.ctrl], dark: false) == "#4F86E8")
    }
    @Test func colourFollowsThePlaceAndSettings() {
        var hud = HUDSettings(colors: HUDColors([.ctrl: HUDColor(light: "#FF0000", dark: "#FF6666")]))
        #expect(OptionColors.color(forOption: 2, positions: pos, hud: hud)?.dark == "#FF6666")   // ⌃ (option 2 since W21) red
        var h = HotkeySet.defaults
        h.option2 = .tap(.cmd); h.option4 = .tap(.ctrl)                 // option 2 on ⌘: bottom, green
        let p = FixedSlots.positions(hotkeys: h, slots: HUDSettings.defaultSlots)
        hud = HUDSettings()
        #expect(OptionColors.color(forOption: 2, positions: p, hud: hud)?.dark == "#A6E3A1")
    }
    @Test func labelFitting() {
        #expect(fitLabel("Switch to Telegram") == "Switch to Telegram")
        #expect(fitLabel("Open notion.so/acme/roadmap-q4") == "Open notion.so/…")
        #expect(fitLabel("Open docs.google.com/document/d/1") == "Open docs.google.com/…")
        #expect(fitLabel("Open console.cloud.google.com/x") == "console.cloud.google…")
        #expect(fitLabel("Switch to Microsoft PowerPoint") == "Microsoft PowerPoint")
        #expect(fitLabel("Switch to Microsoft PowerPoint Beta") == "Microsoft PowerPoint…")
        #expect(fitLabel("Switch to Supercalifragilisticexpialidocious") == "Supercalifragilistice…")
        for l in ["Switch to Microsoft PowerPoint Beta", "Open notion.so/a/b/c/d/e/f", "Slide to desk 12",
                  "Open a-very-long-host-name.example.com/x"] {
            #expect(fitLabel(l).count <= maxLabelChars)
        }
    }
    @Test func sidePlacesMakeRoomForWideCapsules() {
        #expect(FixedSlots.offset(.right, radius: 100, capsuleWidth: 168) == (100, 0))   // W10 unchanged
        #expect(FixedSlots.offset(.left, radius: 100, capsuleWidth: 252) == (-142, 0))
        #expect(FixedSlots.offset(.top, radius: 100, capsuleWidth: 252) == (0, -100))
        let all = hudSlots(options: optionsWithWish((1...4).map { HUDOption(key: $0, label: "x") }), positions: pos,
                           radius: 100, capsule: (252, 44), wishWidth: 262)
        for a in all { for b in all where a.key < b.key {               // nothing overlaps
            let apart = abs(a.x - b.x) >= (a.width + b.width) / 2 || abs(a.y - b.y) >= (a.height + b.height) / 2
            #expect(apart, "\(a.key) overlaps \(b.key)")
        } }
        for a in all { #expect(abs(a.x) + a.width / 2 <= 300 - 10 && abs(a.y) + a.height / 2 <= 200 - 10) }   // in a 600×400 panel
    }
}

@Suite struct HintTests {
    @Test func decodesHintsAndIgnoresBadOnes() throws {
        let line = #"{"cmd":"show","ttl_ms":15000,"options":[{"key":1,"label":"Switch to Finder","hint":{"kind":"window","app":"Finder"}},{"key":2,"label":"Slide","hint":{"kind":"edge","dir":"right"}},{"key":3,"label":"X","hint":{"kind":"laser"}},{"key":4,"label":"Y","hint":{"kind":"edge","dir":"up"}}]}"#
        guard case .show(let o, _) = try decodeCommand(Data(line.utf8)) else { Issue.record("not a show"); return }
        #expect(o.map(\.hint) == [.window(app: "Finder"), .edge(dir: .right), nil, nil])
        #expect(hintedOptions(optionsWithWish(o)).map(\.key) == [1, 2])       // the wish option never has one
        let back = try JSONDecoder().decode(HUDOption.self, from: try JSONEncoder().encode(o[0]))
        #expect(back == o[0])
        #expect(throws: (any Error).self) { try JSONDecoder().decode(HUDHint.self, from: Data(#"{"kind":"window","app":" "}"#.utf8)) }
    }
    @Test func frontmostWindowOnAFakeList() {
        let ws = [
            WindowInfo(ownerName: "Telegram", pid: 7, layer: 25, bounds: CGRect(x: 0, y: 0, width: 400, height: 30)),   // menu-bar extra
            WindowInfo(ownerName: "Safari", pid: 9, layer: 0, bounds: CGRect(x: 50, y: 60, width: 900, height: 700)),
            WindowInfo(ownerName: "Telegram", pid: 7, layer: 0, bounds: CGRect(x: 10, y: 10, width: 20, height: 20)),   // sliver
            WindowInfo(ownerName: "Telegram", pid: 7, layer: 0, bounds: CGRect(x: 0, y: 0, width: 500, height: 400), alpha: 0),
            WindowInfo(ownerName: "Telegram", pid: 7, layer: 0, bounds: CGRect(x: 100, y: 120, width: 600, height: 500)),
            WindowInfo(ownerName: "Telegram", pid: 7, layer: 0, bounds: CGRect(x: 300, y: 300, width: 600, height: 500)),
        ]
        #expect(frontmostWindowFrame(app: "Telegram", pids: [7], windows: ws) == CGRect(x: 100, y: 120, width: 600, height: 500))
        #expect(frontmostWindowFrame(app: "telegram", pids: [], windows: ws) == CGRect(x: 100, y: 120, width: 600, height: 500))
        #expect(frontmostWindowFrame(app: "Notes", pids: [3], windows: ws) == nil)          // not on this desk
        #expect(cocoaRect(fromCG: CGRect(x: 100, y: 120, width: 600, height: 500), mainScreenHeight: 1000)
                == CGRect(x: 100, y: 380, width: 600, height: 500))
        let d: [String: Any] = ["kCGWindowOwnerName": "Finder", "kCGWindowOwnerPID": NSNumber(value: 42), "kCGWindowLayer": NSNumber(value: 0),
                                "kCGWindowBounds": ["X": NSNumber(value: 1), "Y": NSNumber(value: 2), "Width": NSNumber(value: 300), "Height": NSNumber(value: 200)]]
        #expect(WindowInfo(d) == WindowInfo(ownerName: "Finder", pid: 42, layer: 0, bounds: CGRect(x: 1, y: 2, width: 300, height: 200)))
        #expect(WindowInfo(["kCGWindowOwnerName": "X"]) == nil)
    }
    @Test func fallbackChain() {
        let w = HUDHint.window(app: "Telegram")
        #expect(resolveDrawing(w, windowFound: true, dockFound: true) == .window)
        #expect(resolveDrawing(w, windowFound: false, dockFound: true) == .dock)
        #expect(resolveDrawing(w, windowFound: false, dockFound: false) == .halo)
        #expect(resolveDrawing(.edge(dir: .left), windowFound: false, dockFound: false) == .edge)
    }
    @Test func overlayLifecycle() {
        var l = HintOverlayLifecycle()
        #expect(!l.visible && l.drawn.isEmpty)
        #expect(l.finish().isEmpty)                                               // hidden: nothing to tear down
        let a = [DrawnHint(key: 1, drawn: .window), DrawnHint(key: 2, drawn: .edge)]
        #expect(l.show(a) == [.draw(a)] && l.visible && l.drawn == a)             // created on show
        let b = [DrawnHint(key: 3, drawn: .halo)]
        #expect(l.show(b) == [.draw(b)] && l.drawn == b)                          // replaced in place
        #expect(l.show([]) == [.teardown] && l.visible && l.drawn.isEmpty)        // a show without hints
        #expect(l.show(a) == [.draw(a)])
        #expect(l.finish() == [.teardown] && !l.visible && l.drawn.isEmpty)       // torn down in finish
        #expect(l.finish().isEmpty && l.drawn.isEmpty)                            // nothing while hidden
    }
    @Test func stateReportsHints() {
        let info = HUDStateInfo(visible: true, options: [], hudEnabled: true, wishHotkey: "⌥`", wishRegistered: true,
                                wishPanel: "closed", hints: [DrawnHint(key: 2, drawn: .edge)])
        #expect(HUDEvent.state(info).jsonLine().contains(#""hints":[{"drawn":"edge","key":2}]"#))
    }
}

@Suite struct WishVisibleTapTests {
    @Test func shiftTapIsTheWishOptionWhileVisible() {
        let p = hotkeyPlan(optionKeys: [1, 2], hotkeys: .defaults)
        #expect(p.taps == [.fn: 1, .ctrl: 2, .shift: wishOptionKey])            // armed with the per-show tap only
        let none = hotkeyPlan(optionKeys: [1], hotkeys: .defaults, tapsAvailable: false)
        #expect(!none.taps.values.contains(wishOptionKey))                      // no tap: the ⌥` chord stays
        var h = HotkeySet.defaults
        h.wishVisible = .tap(.fn); h.option1 = Hotkey(["option"], "1")
        #expect(hotkeyPlan(optionKeys: [], hotkeys: h).taps == [.fn: wishOptionKey])
    }
    @Test func shiftLetterClickScrollAreNotTaps() {
        let shift = HeldModifiers(cgEventFlags: 0x20000)
        var d = TapDetector()
        _ = d.flagsChanged(shift, at: 1.0)
        var t = d.flagsChanged([], at: 1.1); #expect(t == [.shift])            // a clean ⇧ tap: the wish
        for at in [2.0, 3.0, 4.0] {                                            // ⇧A, ⇧-click, ⇧-scroll
            _ = d.flagsChanged(shift, at: at)
            d.otherInput()
            t = d.flagsChanged([], at: at + 0.1); #expect(t == nil)
        }
    }
}

// MARK: - W17 screenshot offer: shot_show / shot_note / debug_note / note event

@Suite struct ShotOfferTests {
    @Test func shotShowDecodesLikeShow() throws {
        let line = #"{"cmd":"shot_show","options":[{"key":3,"label":"Annotate","icon":"sf:square.and.pencil"},{"key":1,"label":"→ Atlas","icon":"initial:R","claude":true}],"ttl_ms":15000}"#
        guard case .showLive(let opts, let ttl, let live) = try decodeCommand(Data(line.utf8)) else { Issue.record("not showLive"); return }
        #expect(opts.map(\.key) == [1, 3] && ttl == 15000 && opts[0].offersClaude && !opts[1].offersClaude && live == "shot")
        // the same validation as show
        #expect(throws: CommandError.badKey(9)) {
            try decodeCommand(Data(#"{"cmd":"shot_show","options":[{"key":9,"label":"x"}]}"#.utf8))
        }
        #expect(throws: CommandError.optionCount(0)) { try decodeCommand(Data(#"{"cmd":"shot_show"}"#.utf8)) }
    }

    // a show bound to a live state (the screenshot on screen): decoded, and kept while typing
    @Test func showBoundToALiveState() throws {
        let line = #"{"cmd":"show","options":[{"key":2,"label":"→ Atlas","icon":"initial:R"}],"ttl_ms":15000,"live":"shot"}"#
        guard case .showLive(let opts, let ttl, let live) = try decodeCommand(Data(line.utf8)) else { Issue.record("not showLive"); return }
        #expect(opts.map(\.key) == [2] && ttl == 15000 && live == "shot")
        let plain = #"{"cmd":"show","options":[{"key":2,"label":"x"}],"ttl_ms":6000}"#
        guard case .show = try decodeCommand(Data(plain.utf8)) else { Issue.record("not show"); return }
        let hint = #"{"cmd":"show","options":[{"key":2,"label":"x"}],"mode":"hint","live":"shot"}"#
        guard case .showHint = try decodeCommand(Data(hint.utf8)) else { Issue.record("not showHint"); return }
        #expect(!LiveBound.dismisses("typing", live: "shot"))          // a key while the shot is live: the offer stays
        #expect(!LiveBound.dismisses("pointer_fast", live: "shot"))    // dragging the thumbnail
        #expect(LiveBound.dismisses("esc", live: "shot") && LiveBound.dismisses("hide_cmd", live: "shot")
                && LiveBound.dismisses("ttl", live: "shot"))
        #expect(LiveBound.dismisses("typing", live: nil))               // any other show: as before
    }

    @Test func shotNoteDecodes() throws {
        let line = #"{"cmd":"shot_note","title":"→ Atlas · Claude note","text":"a\nb","placeholder":"p"}"#
        #expect(try decodeCommand(Data(line.utf8)) == .shotNote(ShotNoteRequest(title: "→ Atlas · Claude note", text: "a\nb", placeholder: "p")))
        #expect(try decodeCommand(Data(#"{"cmd":"shot_note"}"#.utf8)) == .shotNote(ShotNoteRequest(title: "Screenshot note")))
        let long = String(repeating: "x", count: 5000)
        guard case .shotNote(let r) = try decodeCommand(Data(#"{"cmd":"shot_note","text":"\#(long)"}"#.utf8)) else { return }
        #expect(r.text.count == ShotNoteRequest.maxText)
        #expect(try decodeCommand(Data(#"{"cmd":"debug_note","text":"hi"}"#.utf8)) == .debugNote(text: "hi"))
        #expect(try decodeCommand(Data(#"{"cmd":"debug_note"}"#.utf8)) == .debugNote(text: nil))
    }

    @Test func noteEventAndClean() {
        #expect(HUDEvent.note(text: "line 1\nline 2").jsonLine() == #"{"event":"note","text":"line 1\nline 2"}"# + "\n")
        #expect(ShotNoteRequest.clean("  hello \n\n") == "hello")
        #expect(ShotNoteRequest.clean("") == "")
    }

    @Test func noteKeys() {
        #expect(ShotNoteKey.classify(isReturn: true, isEscape: false, shift: false, option: false) == .send)
        #expect(ShotNoteKey.classify(isReturn: true, isEscape: false, shift: true, option: false) == .newline)
        #expect(ShotNoteKey.classify(isReturn: true, isEscape: false, shift: false, option: true) == .newline)
        #expect(ShotNoteKey.classify(isReturn: false, isEscape: true, shift: false, option: false) == .cancel)
        #expect(ShotNoteKey.classify(isReturn: false, isEscape: false, shift: false, option: false) == .other)
    }
}
