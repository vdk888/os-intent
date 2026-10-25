import Foundation
import Testing
@testable import HUDCore
import OISettings

// W12 "Send a vocal to…"

@Suite struct VocalPickerTests {
    @Test func pagesAndSlotMapping() {
        #expect(VocalPicker.pageCount == 3)
        let p1 = VocalPicker.page(0)
        #expect(p1[.fn]?.slug == "atlas" && p1[.ctrl]?.slug == "nova" && p1[.option]?.slug == "orion" && p1[.cmd]?.slug == "lyra")
        let p2 = VocalPicker.page(1)
        #expect(p2[.fn]?.slug == "vega" && p2[.ctrl]?.slug == "juno" && p2[.option]?.slug == "iris"
                && p2[.cmd]?.name == "Hera")
        let p3 = VocalPicker.page(2)
        #expect(p3.count == 1 && p3[.fn]?.slug == "echo")
        #expect(VocalPicker.nextPage(0) == 1 && VocalPicker.nextPage(1) == 2 && VocalPicker.nextPage(2) == 0)   // wraps
        #expect(Set(VocalPicker.agents.map(\.slug)).count == 9)
    }

    @Test func optionsSitAtTheFixedPlacesOfTheirModifier() {
        // defaults (W21): option 1 fn left, 2 ⌃ top, 3 ⌥ right, 4 ⌘ bottom
        let o = VocalPicker.options(page: 0, hotkeys: .defaults)
        #expect(o.map(\.key) == [1, 2, 3, 4])
        #expect(o.map(\.label) == ["Atlas", "Nova", "Orion", "Lyra"])            // W21: the page reads in key order
        #expect(o.map(\.icon) == ["initial:A", "initial:N", "initial:O", "initial:L"])
        let pos = FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots)
        #expect(pos[o.first { $0.label == "Atlas" }!.key] == .left)       // fn left
        #expect(pos[o.first { $0.label == "Nova" }!.key] == .top)        // ⌃ top
        #expect(pos[o.first { $0.label == "Orion" }!.key] == .right)       // ⌥ right
        #expect(pos[o.first { $0.label == "Lyra" }!.key] == .bottom)     // ⌘ bottom
        #expect(VocalPicker.options(page: 2, hotkeys: .defaults) == [HUDOption(key: 1, label: "Echo", icon: "initial:E")])
        #expect(VocalPicker.agent(forOptionKey: 1, page: 0, hotkeys: .defaults)?.slug == "atlas")
        #expect(VocalPicker.agent(forOptionKey: 2, page: 1, hotkeys: .defaults)?.slug == "juno")
        #expect(VocalPicker.agent(forOptionKey: 2, page: 2, hotkeys: .defaults) == nil)
    }

    @Test func swappedOrChordOptionsFollowTheModifier() {
        var h = HotkeySet.defaults
        h.option1 = .tap(.ctrl); h.option2 = .tap(.fn)                     // ⌃ picks option 1 now
        #expect(VocalPicker.agent(forOptionKey: 1, page: 0, hotkeys: h)?.slug == "nova")
        #expect(VocalPicker.agent(forOptionKey: 2, page: 0, hotkeys: h)?.slug == "atlas")
        h = .defaults
        h.option4 = Hotkey(["option"], "4")                                // ⌘'s option is a chord: its default place
        #expect(VocalPicker.optionKey(for: .cmd, hotkeys: h) == 4)
    }
}

@Suite struct VocalFlowTests {
    let nova = VocalAgent("nova", "Nova")

    @Test func pickRecordSend() {
        var f = VocalFlow()
        #expect(f.handle(.enter).isEmpty && f.state == .idle)            // nothing while idle
        #expect(f.handle(.vocalKey) == [.showPicker(page: 0)] && f.stateName == "picking:1")
        #expect(f.handle(.vocalKey) == [.showPicker(page: 1)])
        #expect(f.handle(.vocalKey) == [.showPicker(page: 2)])
        #expect(f.handle(.vocalKey) == [.showPicker(page: 0)])           // wraps to page 1
        #expect(f.handle(.pick(nova)) == [.closePicker, .startRecording(nova)] && f.stateName == "recording:nova")
        #expect(f.handle(.pick(nova)).isEmpty)                            // no second pick while recording
        #expect(f.handle(.vocalKey) == [.send(nova)] && f.state == .idle)
    }

    @Test func enterAndLimitSendEscCancels() {
        var f = VocalFlow()
        _ = f.handle(.vocalKey); _ = f.handle(.pick(nova))
        #expect(f.handle(.enter) == [.send(nova)])
        _ = f.handle(.vocalKey); _ = f.handle(.pick(nova))
        #expect(f.handle(.dictationEnded) == [.send(nova)])
        _ = f.handle(.vocalKey); _ = f.handle(.pick(nova))
        #expect(f.handle(.cancel) == [.cancelRecording] && f.state == .idle)
        _ = f.handle(.vocalKey)
        #expect(f.handle(.cancel) == [.closePicker] && !f.isPicking)     // the picker HUD went away
        #expect(f.handle(.dictationEnded).isEmpty)
    }
}

@Suite struct VocalTextAndStoreTests {
    @Test func emptyTranscriptIsNeverSent() {
        #expect(VocalText.clean("") == nil && VocalText.clean("  \n\t ") == nil)
        #expect(VocalText.clean(" bonjour\nNova ") == "bonjour Nova")
        #expect(VocalText.clean(String(repeating: "a", count: 20000))?.count == vocalMaxChars)
    }

    @Test func filesArePrivate() throws {
        let d = FileManager.default.temporaryDirectory.appendingPathComponent("oi-vocal-\(UUID().uuidString)")
        let date = Date(timeIntervalSince1970: 1_792_500_000)            // 2026-10-20T…Z
        #expect(VocalStore.stem(date, slug: "nova").hasSuffix("Z-nova"))
        let u = try VocalStore.urls(date, slug: "nova", dataDir: d)
        #expect(u.audio.pathExtension == "caf" && u.text.pathExtension == "txt")
        #expect(u.audio.deletingPathExtension().lastPathComponent == u.text.deletingPathExtension().lastPathComponent)
        try VocalStore.writeText("salut", to: u.text)
        #expect(try String(contentsOf: u.text, encoding: .utf8) == "salut")
        let fm = FileManager.default
        #expect((try fm.attributesOfItem(atPath: u.text.path)[.posixPermissions] as? Int) == 0o600)
        #expect((try fm.attributesOfItem(atPath: VocalStore.dir(dataDir: d).path)[.posixPermissions] as? Int) == 0o700)
    }

    @Test func resultParsingAndToast() {
        let nova = VocalAgent("nova", "Nova")
        let ok = VocalResult.parse(stdout: #"{"ok": true, "to": "nova"}"# + "\n", exitCode: 0)
        #expect(ok.ok && ok.toast(agent: nova) == "Sent to Nova")
        let bad = VocalResult.parse(stdout: #"{"ok": false, "error": "ssh exit 255"}"#, exitCode: 1)
        #expect(!bad.ok && bad.toast(agent: nova) == "Failed: ssh exit 255")
        #expect(VocalResult.parse(stdout: "", exitCode: 127).reason == "exit 127")
        #expect(VocalResult.parse(stdout: #"{"ok": true, "dry_run": true}"#, exitCode: 0).toast(agent: nova)
                == "Dry run: Nova, nothing sent")
        #expect(!VocalResult.parse(stdout: #"{"ok": true}"#, exitCode: 2).ok)   // ok but a bad exit: failed
    }
}

@Suite struct VocalHUDTests {
    @Test func vocalOptionAtSevenThirtyMirrorsTheWish() {
        let opts = optionsWithWishAndVocal([HUDOption(key: 1, label: "Mail")])
        #expect(opts.map(\.key) == [1, wishOptionKey, vocalOptionKey])
        #expect(optionsWithWishAndVocal(opts).map(\.key) == [1, wishOptionKey, vocalOptionKey])   // idempotent
        let pos = FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots)
        let all = hudSlots(options: optionsWithWishAndVocal((1...4).map { HUDOption(key: $0, label: "x") }),
                           positions: pos, radius: 80, capsuleHeight: 32) { o in
            o.key == wishOptionKey || o.key == vocalOptionKey ? 72 : 120
        }
        let wish = all.first { $0.key == wishOptionKey }!, vocal = all.first { $0.key == vocalOptionKey }!
        #expect(vocal.x == -wish.x && vocal.y == wish.y && vocal.x < 0 && vocal.y > 80)
        for (i, a) in all.enumerated() {
            for b in all[(i + 1)...] {
                #expect(abs(a.x - b.x) >= (a.width + b.width) / 2 || abs(a.y - b.y) >= (a.height + b.height) / 2)
            }
        }
    }

    @Test func capsuleWidthFollowsTheContent() {
        #expect(capsuleWidth(iconSize: 20, labelWidth: 0, badgeWidth: 10) == 72)           // icon + key: the minimum
        #expect(capsuleWidth(iconSize: 20, labelWidth: 50, badgeWidth: 12) == 6 + 20 + 6 + 50 + 6 + 12 + 10 + 6)
        #expect(capsuleWidth(iconSize: 20, labelWidth: 400, badgeWidth: 12) == 200)        // the maximum
        // side capsules keep their inner edge 16 px from the centre whatever their width
        let s = hudSlots(options: [HUDOption(key: 2, label: "x"), HUDOption(key: 4, label: "y")],
                         positions: [2: .right, 4: .left], radius: 80, capsuleHeight: 32) { $0.key == 2 ? 200 : 72 }
        #expect(s[0].x == 116 && s[1].x == -80)
    }

    @Test func vocalArmAndPlan() {
        #expect(VocalArm(hotkeys: .defaults) == .tap([.ctrl, .option]) && VocalArm.display(.defaults) == "⌃⌥")
        var h = HotkeySet.defaults
        h.vocal = Hotkey(["control", "option"], "v")
        guard case .chord(let reg) = VocalArm(hotkeys: h) else { Issue.record("not a chord"); return }
        #expect(reg.id == vocalHotkeyID)
        #expect(hotkeyPlan(optionKeys: [1], hotkeys: h).regs.contains { $0.id == vocalHotkeyID })
        #expect(!hotkeyPlan(optionKeys: [1], hotkeys: .defaults).regs.contains { $0.id == vocalHotkeyID })
    }

    @Test func vocalToCommand() throws {   // W22
        #expect(try decodeCommand(Data(#"{"cmd":"vocal_to","pick":"nova"}"#.utf8)) == .vocalTo(slug: "nova"))
        #expect(throws: CommandError.unknownAgent("bob")) { try decodeCommand(Data(#"{"cmd":"vocal_to","pick":"bob"}"#.utf8)) }
        #expect(throws: CommandError.unknownAgent("")) { try decodeCommand(Data(#"{"cmd":"vocal_to"}"#.utf8)) }
    }

    @Test func debugVocalCommandAndState() throws {
        #expect(try decodeCommand(Data(#"{"cmd":"debug_vocal"}"#.utf8)) == .debugVocal(pick: nil, text: nil))
        #expect(try decodeCommand(Data(#"{"cmd":"debug_vocal","pick":"nova","text":"salut"}"#.utf8))
                == .debugVocal(pick: "nova", text: "salut"))
        #expect(throws: CommandError.unknownAgent("bob")) {
            try decodeCommand(Data(#"{"cmd":"debug_vocal","pick":"bob"}"#.utf8))
        }
        let info = HUDStateInfo(visible: true, options: [], hudEnabled: true, wishHotkey: "⌥`", wishRegistered: true,
                                wishPanel: "closed", vocalKey: "⌃⌥", vocal: "picking:2")
        let line = HUDEvent.state(info).jsonLine()
        #expect(line.contains(#""vocal":"picking:2""#) && line.contains(#""vocal_key":"⌃⌥""#))
    }
}

// Clicks (the user 16:5x): every capsule is a click target, hit-tested by its own shape.

@Suite struct ClickTests {
    let slots = hudSlots(options: optionsWithWishAndVocal((1...4).map { HUDOption(key: $0, label: "x") }),
                         positions: FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots),
                         radius: 80, capsuleHeight: 32) { _ in 100 }

    @Test func hitTestPerCapsuleShape() {
        #expect(capsuleHit((0, -80), slots: slots) == 2)              // ⌃ top (W21: 2)
        #expect(capsuleHit((80, 0), slots: slots) == 3)               // ⌥ right (side = max(80, 66))
        #expect(capsuleHit((0, 80), slots: slots) == 4)               // ⌘ bottom
        #expect(capsuleHit((-80, 0), slots: slots) == 1)              // fn left
        let w = slots.first { $0.key == wishOptionKey }!, v = slots.first { $0.key == vocalOptionKey }!
        #expect(capsuleHit((w.x, w.y), slots: slots) == wishOptionKey)
        #expect(capsuleHit((v.x, v.y), slots: slots) == vocalOptionKey)
        #expect(capsuleHit((0, 0), slots: slots) == nil)              // the pointer spot: clicks go through
        #expect(capsuleHit((0 + 49, -80 + 15), slots: slots) == nil)  // the capsule's rounded corner
        #expect(capsuleHit((0 + 34, -80 + 15), slots: slots) == 2)    // the flat part, same height
        #expect(capsuleHit((300, 300), slots: slots) == nil)
    }

    @Test func crossBoxDisablesThePointerHide() {
        #expect(insideCross((0, 0), slots: slots))
        #expect(insideCross((-140, 0), slots: slots))                 // just past the left capsule, in the margin
        #expect(!insideCross((400, 0), slots: slots) && !insideCross((0, 250), slots: slots))
        #expect(!insideCross((0, 0), slots: []))
    }

    @Test func clickRepliesPressedViaClick() {
        #expect(HUDEvent.pressed(key: 2, via: "click").jsonLine() == #"{"event":"pressed","key":2,"via":"click"}"# + "\n")
        #expect(HUDEvent.pressed(key: 1, via: "key").jsonLine().contains(#""via":"key""#))
        #expect(HUDEvent.pressed(key: 1).jsonLine() == #"{"event":"pressed","key":1}"# + "\n")
    }

    @Test func pickerInitialIcon() {
        #expect(HUDIcon("initial:T") == .initial("T") && HUDIcon("initial:") == .none)
    }
}

// W20
@Suite struct DictationSegmentsTests {
    @Test func segmentsAreConcatenatedWithASpace() {
        var d = DictationSegments()
        #expect(d.handle(partial: "bonjour", final: false, failed: false) == .keepGoing)
        #expect(d.handle(partial: "bonjour Nova", final: false, failed: false) == .keepGoing)
        #expect(d.text == "bonjour Nova")
        #expect(d.handle(partial: "bonjour Nova", final: true, failed: false) == .restart)   // a pause: new segment
        #expect(d.handle(partial: " il faut ", final: false, failed: false) == .keepGoing)
        #expect(d.text == "bonjour Nova il faut")
        #expect(d.handle(partial: "il faut corriger ça", final: false, failed: true) == .restart)  // an error mid-speech
        #expect(d.handle(partial: "merci", final: false, failed: false) == .keepGoing)
        #expect(d.text == "bonjour Nova il faut corriger ça merci")
        #expect(d.committed == ["bonjour Nova", "il faut corriger ça"])
    }
    @Test func isFinalNeverEndsTheDictation() {
        var d = DictationSegments()
        for i in 0..<20 {   // any number of finals / errors after speech: always a new segment, never an end
            #expect(d.handle(partial: "mot\(i)", final: i % 2 == 0, failed: i % 2 == 1) == .restart)
        }
        #expect(d.committed.count == 20)
        // and a final with no new text keeps what was said
        #expect(d.handle(partial: nil, final: true, failed: false) == .restart)
        #expect(d.text.hasPrefix("mot0 mot1") && d.text.hasSuffix("mot19"))
    }
    @Test func emptyFailuresBackOffButNeverGiveUp() {
        // W21 (W20 review note): after 5 empty failures in a row the restarts wait 250, 500, 1000, then 2000 ms
        // (capped) — for as long as the dictation runs: speech after a long silence is still transcribed
        var d = DictationSegments()
        _ = d.handle(partial: "déjà dit", final: true, failed: false)
        for _ in 1..<DictationSegments.maxEmptyFailures {
            #expect(d.handle(partial: nil, final: false, failed: true) == .restart)
        }
        let waits = (0..<8).map { _ -> Int in
            guard case .restart(let ms) = d.handle(partial: nil, final: false, failed: true) else { return -1 }
            return ms
        }
        #expect(waits == [250, 500, 1000, 2000, 2000, 2000, 2000, 2000])
        for _ in 0..<1000 { _ = d.handle(partial: nil, final: false, failed: true) }   // a minute of silence and more
        #expect(d.handle(partial: nil, final: false, failed: true) == .restart(afterMs: 2000))
        #expect(d.text == "déjà dit")                                   // the text so far is kept
        #expect(d.handle(partial: "après le silence", final: true, failed: false) == .restart)   // speech: at once again
        #expect(d.text == "déjà dit après le silence" && d.emptyFailures == 0)
        #expect(DictationSegments.backoffMs(failures: Int.max) == 2000 && DictationSegments.backoffMs(failures: 0) == 0)
        var e = DictationSegments()                                      // speech resets the failure count
        for _ in 0..<(DictationSegments.maxEmptyFailures - 1) { _ = e.handle(partial: nil, final: false, failed: true) }
        #expect(e.handle(partial: "oui", final: false, failed: true) == .restart && e.emptyFailures == 0)
    }
    @Test func onlyTheUserOrTheLimitSends() {
        // the recogniser's final is absorbed by DictationSegments (never reaches the flow); the flow sends only on
        // the vocal key, Enter or `.dictationEnded` (= the 60 s limit / dictation unavailable), Esc cancels
        let nova = VocalPicker.page(0)[.ctrl]!
        var f = VocalFlow()
        _ = f.handle(.vocalKey); _ = f.handle(.pick(nova))
        var d = DictationSegments()
        #expect(d.handle(partial: "je parle", final: true, failed: false) == .restart)
        #expect(f.isRecording)                                           // still recording after a recogniser final
        #expect(f.handle(.cancel) == [.cancelRecording] && !f.isRecording)
    }
}

// Demo mode (`oi demo`): OI_HUD_AGENTS replaces the roster with fictional agents.
@Suite struct VocalDemoRosterTests {
    @Test func parsesSlugNamePairs() {
        let a = VocalPicker.parseAgents("atlas:Atlas, nova:Nova,orion:Orion,lyra:Lyra")
        #expect(a?.map(\.slug) == ["atlas", "nova", "orion", "lyra"])
        #expect(a?.map(\.name) == ["Atlas", "Nova", "Orion", "Lyra"])
        #expect(a?.first?.initial == "A")
    }

    @Test func invalidSpecsKeepTheFleetRoster() {
        for bad in [nil, "", "atlas", "Atlas:Atlas", "atlas:", "atlas:A,atlas:B", "a b:Atlas", "x:" + String(repeating: "n", count: 21),
                    (1...13).map { "a\($0):A" }.joined(separator: ","), "atlas:Atlas,"] {
            #expect(VocalPicker.parseAgents(bad) == nil)
        }
        #expect(VocalPicker.agents == VocalPicker.defaultAgents)   // tests run without OI_HUD_AGENTS
    }

    @Test func stateCarriesTheRosterNames() {
        let line = HUDEvent.state(HUDStateInfo(visible: false, options: [], hudEnabled: true, wishHotkey: "⌥`",
                                               wishRegistered: true, wishPanel: "closed")).jsonLine()
        #expect(line.contains(#""agents":["Atlas","Nova","Orion","Lyra""#))
    }
}
