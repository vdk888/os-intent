import Foundation
import Testing
@testable import HUDCore
import OISettings

// W14: long-press = the Claude-assisted variant; the HUD's work state; the protocol additions.

private let opt: HeldModifiers = .option
private let cmd: HeldModifiers = .command

@Suite struct LongPressTests {
    @Test func holdPastThresholdThenReleaseFires() {
        var d = LongPressDetector(thresholdMs: 600)
        #expect(d.flagsChanged(opt, at: 10.0) == nil)
        #expect(d.progress(at: 10.1) == nil)                        // the ring waits 150 ms (a tap never flashes it)
        let p = d.progress(at: 10.3)
        #expect(p?.mod == .option && abs((p?.fill ?? 0) - 0.5) < 1e-9)
        #expect(d.progress(at: 11.0)?.fill == 1)
        #expect(d.flagsChanged([], at: 10.65) == .option)
        #expect(d.progress(at: 10.7) == nil)
    }
    @Test func shortHoldIsNotALongPress() {
        var d = LongPressDetector(thresholdMs: 600)
        _ = d.flagsChanged(cmd, at: 0)
        #expect(d.flagsChanged([], at: 0.2) == nil)                 // a tap: the TapDetector's
        _ = d.flagsChanged(cmd, at: 1)
        #expect(d.flagsChanged([], at: 1.5) == nil)                 // between tap and long: nothing
    }
    @Test func chordsSetsAndShiftNeverFire() {
        var d = LongPressDetector(thresholdMs: 600)
        _ = d.flagsChanged(opt, at: 0); d.otherInput()               // ⌥ + a key / click / scroll
        #expect(d.flagsChanged([], at: 1) == nil)
        _ = d.flagsChanged(opt, at: 2); _ = d.flagsChanged([.option, .command], at: 2.1)   // ⌥+⌘ (summon set)
        _ = d.flagsChanged(cmd, at: 2.2)
        #expect(d.flagsChanged([], at: 3) == nil)
        _ = d.flagsChanged(.shift, at: 4)                             // ⇧ is the wish key, never Claude
        #expect(d.flagsChanged([], at: 5) == nil)
        _ = d.flagsChanged(.control, at: 6); d.reset()
        #expect(d.flagsChanged([], at: 7) == nil)
    }
    @Test func thresholdFollowsSettingsClamped() {
        #expect(LongPressDetector(thresholdMs: 800).threshold == 0.8)
        #expect(LongPressDetector(thresholdMs: 50).threshold == 0.4)
        #expect(LongPressDetector().threshold == 0.6)
    }
}

@Suite struct ClaudeWorkTests {
    @Test func workThenCardThenReset() {
        var w = ClaudeWork()
        #expect(!w.isBusy && w.allowsDismiss("ttl"))
        w.begin(key: 2, at: 100)
        #expect(w.isWorking && w.workingKey == 2)
        for r in ["ttl", "typing", "pointer_fast", "replaced"] { #expect(!w.allowsDismiss(r)) }
        for r in ["esc", "hide_cmd", "claude_timeout"] { #expect(w.allowsDismiss(r)) }
        #expect(!w.timedOut(at: 140) && w.timedOut(at: 145))
        #expect(w.result(key: 1, text: "x") == .cancelled)          // another option's result: not for us
        #expect(w.result(key: 2, text: "  Draft  ready \n\n in Mail ") == .showCard("Draft ready\nin Mail"))
        #expect(w.cardText == "Draft ready\nin Mail" && w.allowsDismiss("ttl") && !w.isWorking)
        w.reset()
        #expect(w.state == .idle)
    }
    @Test func emptyResultHidesAndLateResultIsCancelled() {
        var w = ClaudeWork()
        w.begin(key: 1, at: 0)
        #expect(w.result(key: 1, text: "") == .hide && w.state == .idle)
        #expect(w.result(key: 1, text: "late") == .cancelled)        // Esc already hid it: python does nothing
    }
    @Test func cardTextLimits() {
        let long = String(repeating: "a", count: 100)
        #expect(CardText.clean("1\n2\n3\n4\n5") == "1\n2\n3\n4")
        #expect(CardText.clean(long)?.count == 64 && CardText.clean(long)?.hasSuffix("…") == true)
        #expect(CardText.clean(" \n ") == nil && CardText.clean(nil) == nil)
    }
}

@Suite struct ClaudeProtocolTests {
    @Test func optionClaudeFlagIsTolerant() throws {
        let c = try decodeCommand(Data(#"{"cmd":"show","options":[{"key":1,"label":"Reply","claude":true},{"key":2,"label":"X","claude":"y"}]}"#.utf8))
        guard case .show(let o, _) = c else { Issue.record("not a show"); return }
        #expect(o[0].offersClaude && !o[1].offersClaude)
    }
    @Test func updateResultDebugPress() throws {
        #expect(try decodeCommand(Data(#"{"cmd":"update","options":[{"key":1,"label":"Reply to G","expect":"Reply"}]}"#.utf8))
                == .update([LabelUpdate(key: 1, label: "Reply to G", expect: "Reply")]))
        #expect(throws: CommandError.badUpdate) { try decodeCommand(Data(#"{"cmd":"update","options":[]}"#.utf8)) }
        #expect(try decodeCommand(Data(#"{"cmd":"result","key":2,"ok":false,"text":"t","ttl_ms":2500}"#.utf8))
                == .result(key: 2, ok: false, text: "t", ttlMs: 2500))
        #expect(try decodeCommand(Data(#"{"cmd":"result","key":3}"#.utf8)) == .result(key: 3, ok: true, text: nil, ttlMs: 8000))
        // a reply draft saved in place: the card offers « Open in Mail » for that draft id (0 / absent = none)
        #expect(try decodeCommand(Data(#"{"cmd":"result","key":2,"text":"t","ttl_ms":15000,"open_draft":42}"#.utf8))
                == .result(key: 2, ok: true, text: "t", ttlMs: 15000, openDraft: 42))
        #expect(try decodeCommand(Data(#"{"cmd":"result","key":2,"open_draft":0}"#.utf8))
                == .result(key: 2, ok: true, text: nil, ttlMs: 8000, openDraft: nil))
        #expect(throws: CommandError.badKey(0)) { try decodeCommand(Data(#"{"cmd":"result"}"#.utf8)) }
        #expect(try decodeCommand(Data(#"{"cmd":"debug_press","key":1,"mode":"claude"}"#.utf8)) == .debugPress(key: 1, mode: "claude"))
    }
    @Test func pressedCarriesTheMode() {
        #expect(HUDEvent.pressed(key: 1, via: "key", mode: "claude").jsonLine()
                == #"{"event":"pressed","key":1,"mode":"claude","via":"key"}"# + "\n")
        #expect(!HUDEvent.pressed(key: 1, via: "key").jsonLine().contains("mode"))
    }
    @Test func labelUpdatesOnlyOnTheSameOption() {
        let opts = [HUDOption(key: 1, label: "Reply"), HUDOption(key: 3, label: "Forward")]
        let (out, n) = LabelUpdate.apply([LabelUpdate(key: 1, label: "Reply to Sam", expect: "Reply"),
                                          LabelUpdate(key: 3, label: "Forward: devis", expect: "Something else"),
                                          LabelUpdate(key: 2, label: "no such key")], to: opts)
        #expect(n == 1 && out.map(\.label) == ["Reply to Sam", "Forward"])
        #expect(LabelUpdate.apply([LabelUpdate(key: 3, label: "  ")], to: opts).1 == 0)
    }
}
