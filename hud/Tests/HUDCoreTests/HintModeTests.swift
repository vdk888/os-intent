import Foundation
import Testing
@testable import HUDCore
import OISettings

// W19: automatic shows become hints (dots); the summon key develops them into the full cross.

private func opts(_ keys: [Int]) -> [HUDOption] { keys.map { HUDOption(key: $0, label: "o\($0)", icon: "sf:app") } }

@Suite struct HintCommandTests {
    @Test func showModeHintDecodesToShowHint() throws {
        let line = #"{"cmd":"show","mode":"hint","hint_ttl_ms":2500,"ttl_ms":6000,"options":[{"key":2,"label":"B"},{"key":1,"label":"A"}]}"#
        guard case .showHint(let o, let ttl, let h, _) = try decodeCommand(Data(line.utf8)) else { Issue.record("not a hint"); return }
        #expect(o.map(\.key) == [1, 2] && ttl == 6000 && h == 2500)
    }
    @Test func hintDefaultsAndFullMode() throws {
        guard case .showHint(_, let ttl, let h, _) = try decodeCommand(Data(#"{"cmd":"show","mode":"hint","options":[]}"#.utf8))
        else { Issue.record("not a hint"); return }
        #expect(ttl == defaultTTLMs && ttl == 6000 && h == defaultHintTTLMs && h == 4000)
        // "full", absent or unknown = the cross as before (an older / newer driver never fails a show)
        #expect(try decodeCommand(Data(#"{"cmd":"show","mode":"full","options":[],"ttl_ms":6000}"#.utf8)) == .show(options: [], ttlMs: 6000))
        #expect(try decodeCommand(Data(#"{"cmd":"show","mode":"sparkle","options":[],"ttl_ms":6000}"#.utf8)) == .show(options: [], ttlMs: 6000))
        #expect(throws: CommandError.badTTL(5)) { try decodeCommand(Data(#"{"cmd":"show","mode":"hint","hint_ttl_ms":5,"options":[]}"#.utf8)) }
        #expect(throws: CommandError.badKey(7)) { try decodeCommand(Data(#"{"cmd":"show","mode":"hint","options":[{"key":7,"label":"x"}]}"#.utf8)) }
    }
    @Test func expandedEventAndStateField() {
        #expect(HUDEvent.hintExpanded.jsonLine() == #"{"event":"hint","phase":"expanded"}"# + "\n")
        var info = HUDStateInfo(visible: false, options: [], hudEnabled: true, wishHotkey: "⌥`", wishRegistered: true, wishPanel: "closed")
        #expect(HUDEvent.state(info).jsonLine().contains(#""hint":"idle""#))
        info.hint = "dots:1,2"
        #expect(HUDEvent.state(info).jsonLine().contains(#""hint":"dots:1,2""#))
    }
}

@Suite struct HintLifeTests {
    @Test func lifecycleDotsFadeGraceIdle() {
        var h = HintLife()
        #expect(h.isIdle && h.stateName == "idle" && h.fadeDue == nil && h.graceDue == nil)
        h.show(opts([3, 1, 2]), fullTTLms: 6000, hintTTLms: 2500, at: 100)
        #expect(h.isDrawn && h.stateName == "dots:1,2,3")
        #expect(h.fadeDue == 102.5 && h.graceDue == nil)            // the fade timer: hint_ttl after the show
        #expect(h.fade(at: 102.5) && !h.isDrawn && h.stateName == "faded")
        #expect(h.fade(at: 103) == false)                             // once
        #expect(h.graceDue == 105.0 && h.fadeDue == nil)              // still developable for another hint_ttl
        #expect(h.canDevelop(at: 104.9) && !h.canDevelop(at: 105.1))
        h.reset()
        #expect(h.isIdle && h.options.isEmpty)
    }
    @Test func developReturnsSameOptionsAndFullTTL() {
        var h = HintLife()
        h.show(opts([1, 4]), fullTTLms: 6000, hintTTLms: 2500, at: 0)
        let d = h.develop(at: 1.0)
        #expect(d?.options.map(\.key) == [1, 4] && d?.ttlMs == 6000)
        #expect(h.isIdle)                                             // a hint develops once
        #expect(h.develop(at: 1.1) == nil)
    }
    @Test func developAfterFadeWithinGraceOnly() {
        var h = HintLife()
        h.show(opts([2]), fullTTLms: 6000, hintTTLms: 2000, at: 0)
        h.fade(at: 2)
        #expect(h.develop(at: 3.5)?.options.map(\.key) == [2])
        h.show(opts([2]), fullTTLms: 6000, hintTTLms: 2000, at: 10)
        h.fade(at: 12)
        #expect(h.develop(at: 14.5) == nil)                           // too late: the summon goes on as today
        var none = HintLife()
        #expect(none.develop(at: 0) == nil)                           // no hint: summon as today
    }
    @Test func relabelWhileHinted() throws {
        var h = HintLife()
        let u = try JSONDecoder().decode([LabelUpdate].self, from: Data(#"[{"key":1,"expect":"o1","label":"Reply to Ana"}]"#.utf8))
        #expect(h.relabel(u) == 0)                                    // no hint
        h.show(opts([1, 2]), fullTTLms: 6000, hintTTLms: 2500, at: 0)
        #expect(h.relabel(u) == 1)
        #expect(h.develop(at: 1)?.options.first?.label == "Reply to Ana")
    }
    @Test func onlyOptionKeysGetDotsNothingArmed() {
        var h = HintLife()
        // the wish (0) / vocal (-1) options never come as dots; dots carry no label, no icon, no key: nothing to arm
        h.show(opts([1, wishOptionKey, vocalOptionKey]), fullTTLms: 6000, hintTTLms: 2500, at: 0)
        #expect(h.options.map(\.key) == [1])
        let dots = HintDotLayout.offsets(keys: h.options.map(\.key),
                                         positions: FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots))
        #expect(dots.count == 1)
    }
    @Test func dotLayoutFollowsFixedPlacesNearPointer() {
        let pos = FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots)
        let d = HintDotLayout.offsets(keys: [1, 2, 3, 4], positions: pos)
        #expect(d.count == 4)
        for x in d {
            #expect(abs(hypot(x.x, x.y) - HintDotLayout.radius) < 0.001)       // all on a small ring
            let p = pos[x.key]!
            let o = FixedSlots.offset(p, radius: 1)
            #expect(x.x * o.x >= 0 && x.y * o.y >= 0)                         // in the direction of the key's place
        }
        #expect(HintDotLayout.dotSize == 10 && HintDotLayout.radius == 22)       // W20 (was 6 pt at 16 pt)
    }
    // W20: noticeable yet calm
    @Test func w20DotStyleContrastAndMotion() {
        let normal = HintDotLayout.style(increaseContrast: false)
        #expect(abs(normal.alpha - 0.85) < 0.001 && normal.whiteRing > 0 && normal.blackRing > 0)
        let hc = HintDotLayout.style(increaseContrast: true)
        #expect(hc.alpha == 1 && hc.whiteRing > normal.whiteRing && hc.blackRing > normal.blackRing)
        #expect(HintDotLayout.pulses(reduceMotion: false) == 2 && HintDotLayout.pulses(reduceMotion: true) == 0)
        #expect(defaultHintTTLMs == 4000)
        // the clamp box holds a pulsed dot at the radius
        #expect(HintDotLayout.halfExtent(dotSize: 10) >= HintDotLayout.radius + 10 * HintDotLayout.pulseScale / 2)
    }
    @Test func w20LayoutRadiusOverride() {
        let pos = FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots)
        for x in HintDotLayout.offsets(keys: [1, 2, 3, 4], positions: pos, radius: 30) {
            #expect(abs(hypot(x.x, x.y) - 30) < 0.001)
        }
    }
}

@Suite struct OptionBadgeTests {   // W19: number first
    @Test func numberThenSymbol() {
        let d = HotkeySet.defaults
        #expect(optionBadge(key: 1, symbol: d.option1.display) == "1 fn")   // W21: the number is the key
        #expect(optionBadge(key: 2, symbol: d.option2.display) == "2 ⌃")
        #expect(optionBadge(key: 3, symbol: d.option3.display) == "3 ⌥")
        #expect(optionBadge(key: 4, symbol: d.option4.display) == "4 ⌘")
    }
    @Test func chordsWishAndVocalUnchanged() {
        #expect(optionBadge(key: 2, symbol: HotkeySet.legacyChord(2).display) == "⌥2")    // the number is already there
        #expect(optionBadge(key: wishOptionKey, symbol: "⇧") == "⇧")
        #expect(optionBadge(key: vocalOptionKey, symbol: "⌃⌥") == "⌃⌥")
        #expect(optionBadge(key: 1, symbol: "") == "")
    }
    @Test func splitForTwoWeights() {
        #expect(splitOptionBadge("2 ⌃")! == ("2", "⌃"))
        #expect(splitOptionBadge("1 fn")! == ("1", "fn"))
        #expect(splitOptionBadge("⇧") == nil && splitOptionBadge("⌥2") == nil && splitOptionBadge("9 x") == nil)
    }
    @Test func hotkeysWindowLabels() {
        #expect(HotkeySet.windowLabel("option_1") == "Option 1 (fn)")              // W21
        #expect(HotkeySet.windowLabel("option_2") == "Option 2 (⌃ control)")
        #expect(HotkeySet.windowLabel("option_3") == "Option 3 (⌥ option)")
        #expect(HotkeySet.windowLabel("option_4") == "Option 4 (⌘ command)")
        #expect(HotkeySet.windowLabel("wish") == "Wish (tell me what you wanted)")
    }
}
