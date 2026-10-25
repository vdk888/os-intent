import Foundation
import Testing
@testable import HUDCore
import OISettings

// W21: the option NUMBER is the KEY, never the rank — fn 1, ⌃ 2, ⌥ 3, ⌘ 4,
// clockwise from the left. Everything that names an option by number follows the key.

@Suite struct W21KeyNumberTests {
    let pos = FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots)
    func opts(_ keys: [Int]) -> [HUDOption] { keys.map { HUDOption(key: $0, label: "o\($0)") } }

    @Test func numbersAreKeysClockwiseFromTheLeft() {
        #expect(pos == [1: .left, 2: .top, 3: .right, 4: .bottom])
        let d = HotkeySet.defaults
        #expect((1...4).map { d.option($0)!.tap! } == [.fn, .ctrl, .option, .cmd])
        #expect((1...4).map { optionBadge(key: $0, symbol: d.option($0)!.display) } == ["1 fn", "2 ⌃", "3 ⌥", "4 ⌘"])
        let taps = hotkeyPlan(optionKeys: [1, 2, 3, 4], hotkeys: d).taps
        #expect(taps[.fn] == 1 && taps[.ctrl] == 2 && taps[.option] == 3 && taps[.cmd] == 4)
    }
    @Test func threeOptionsLeaveTheMissingKeysPlaceEmpty() {
        // the driver's default fill (⌃ ⌥ ⌘): fn's place (left) stays empty, ⌘ keeps its 4
        let slots = hudSlots(options: optionsWithWishAndVocal(opts([2, 3, 4])), positions: pos, radius: 80, capsuleHeight: 32) { _ in 100 }
        #expect(Set(slots.compactMap(\.position)) == [.top, .right, .bottom])
        #expect(slots.first { $0.key == 4 }?.position == .bottom)
        #expect(capsuleHit((-80, 0), slots: slots) == nil)                // nothing at fn's place
        #expect(capsuleHit((0, 80), slots: slots) == 4)                   // ⌘ bottom is still 4
    }
    @Test func coloursStayWithTheKey() {
        let hud = HUDSettings()
        let c = (1...4).map { OptionColors.color(forOption: $0, positions: pos, hud: hud) }
        #expect(c == [hud.colors[.fn], hud.colors[.ctrl], hud.colors[.option], hud.colors[.cmd]])   // pink blue yellow green
    }
    @Test func hintDotsSitTowardTheirKeysPlace() {
        let d = Dictionary(uniqueKeysWithValues: HintDotLayout.offsets(keys: [2, 3, 4], positions: pos).map { ($0.key, ($0.x, $0.y)) })
        #expect(d.count == 3 && d[1] == nil)
        #expect(d[2]!.0 == 0 && d[2]!.1 < 0)      // ⌃ 2 up
        #expect(d[3]!.0 > 0 && d[3]!.1 == 0)      // ⌥ 3 right
        #expect(d[4]!.0 == 0 && d[4]!.1 > 0)      // ⌘ 4 down
    }
    @Test func protocolCarriesKeyNumbers() throws {
        let show = try decodeCommand(Data(#"{"cmd":"show","options":[{"key":4,"label":"c"},{"key":2,"label":"a"},{"key":3,"label":"b"}]}"#.utf8))
        guard case .show(let o, _) = show else { Issue.record("not a show"); return }
        #expect(o.map(\.key) == [2, 3, 4])
        #expect(HUDEvent.pressed(key: 4, via: "key").jsonLine() == #"{"event":"pressed","key":4,"via":"key"}"# + "\n")
        #expect(HUDEvent.pressed(key: 3, via: "glide", path: [3, 1]).jsonLine().contains(#""key":3,"path":[3,1]"#))
        #expect(try decodeCommand(Data(#"{"cmd":"debug_press","key":1}"#.utf8)) == .debugPress(key: 1, mode: nil))
    }
    @Test func glidePathStartsWithTheKey() {
        let g = GlideState()
        let top = opts([2, 3, 4])
        #expect(g.option(at: [4], top: top)?.label == "o4")
        #expect(g.option(at: [1], top: top) == nil)                       // fn empty: nothing there
    }
}
