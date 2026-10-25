import Foundation
import Testing
@testable import HUDCore
import OISettings

// Outward gate: the confirm panel before every send / outward page press.

@Suite struct OutwardGateTests {
    private func cmd(_ s: String) throws -> HUDCommand { try decodeCommand(Data(s.utf8)) }

    @Test func decodesTheConfirmRequest() throws {
        let c = try cmd(#"{"cmd":"confirm_outward","id":"ab12","cls":"vocal","title":"Envoyer ce message à Orion ?","preview":["l1","l2","l3","l4"],"ttl_ms":20000,"lang":"fr","action_key":"vocal"}"#)
        guard case .confirmOutward(let r) = c else { Issue.record("not a confirm"); return }
        #expect(r.id == "ab12" && r.cls == "vocal" && r.title == "Envoyer ce message à Orion ?")
        #expect(r.preview == ["l1", "l2", "l3"])                     // at most 3 lines
        #expect(r.ttlMs == 20000 && r.actionKey == "vocal" && r.target == nil)
        #expect(r.hint.hasPrefix("↩ Valider · Esc Annuler"))
        let en = try cmd(#"{"cmd":"confirm_outward","id":"x","cls":"page","title":"Press “Squash & merge”?","lang":"en","target":"button:squash-and-merge","ttl_ms":999999}"#)
        guard case .confirmOutward(let p) = en else { Issue.record("not a confirm"); return }
        #expect(p.target == "button:squash-and-merge" && p.hint.hasPrefix("↩ Validate · Esc Cancel"))
        #expect(p.ttlMs == OutwardConfirmRequest.ttlRange.upperBound)  // clamped: never an endless panel
        // W24 proactive `outward` options (reply / forward / draft): class "reply"
        let rp = try cmd(#"{"cmd":"confirm_outward","id":"r1","cls":"reply","title":"Reply to Lena ?"}"#)
        guard case .confirmOutward(let q) = rp else { Issue.record("not a confirm"); return }
        #expect(q.cls == "reply" && q.target == nil)
    }

    @Test func rejectsAMalformedRequest() {
        for bad in [#"{"cmd":"confirm_outward","cls":"vocal","title":"t"}"#,             // no id
                    #"{"cmd":"confirm_outward","id":"1","cls":"mail","title":"t"}"#,      // unknown class
                    #"{"cmd":"confirm_outward","id":"1","cls":"shot","title":"  "}"#] {   // no title
            #expect(throws: CommandError.self) { try decodeCommand(Data(bad.utf8)) }
        }
    }

    @Test func debugAnswer() throws {
        #expect(try cmd(#"{"cmd":"debug_outward","decision":"validate"}"#) == .debugOutward(validate: true))
        #expect(try cmd(#"{"cmd":"debug_outward","decision":"cancel"}"#) == .debugOutward(validate: false))
        #expect(try cmd(#"{"cmd":"debug_outward"}"#) == .debugOutward(validate: false))   // nothing said = cancel
    }

    @Test func eventLine() throws {
        let line = HUDEvent.outward(id: "ab12", decision: OutwardDecision(validated: true, reason: "return")).jsonLine()
        let o = try #require(try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: String])
        #expect(o == ["event": "outward", "id": "ab12", "decision": "validated", "reason": "return"])
        let no = HUDEvent.outward(id: "z", decision: OutwardDecision(validated: false, reason: "timeout")).jsonLine()
        #expect(no.contains(#""decision":"cancelled""#) && no.contains(#""reason":"timeout""#))
    }

    @Test func validateNeedsAnExplicitInputAfterTheArmDelay() {
        var d = OutwardDecider()
        #expect(d.input(.validate("return"), at: 1) == nil)          // not open: nothing
        d.open(at: 10)
        #expect(d.input(.validate("return"), at: 10.1) == nil)       // a bouncing Return never validates
        #expect(d.decided == nil)
        #expect(d.input(.validate("return"), at: 10.4) == OutwardDecision(validated: true, reason: "return"))
        #expect(d.input(.cancel("esc"), at: 10.5) == nil)            // the first decision wins
    }

    @Test func escClickAndTimeoutCancel() {
        for (i, why) in [(OutwardDecider.Input.cancel("esc"), "esc"), (.cancel("click"), "click"),
                         (.cancel("click_outside"), "click_outside"), (.timeout, "timeout")] {
            var d = OutwardDecider()
            d.open(at: 0)
            #expect(d.input(i, at: 0.05) == OutwardDecision(validated: false, reason: why))   // cancel: no delay
            #expect(d.input(.validate("return"), at: 5) == nil)
        }
    }

    @Test func actionKeyTaps() {
        let r = { (k: String?) in OutwardConfirmRequest(id: "1", cls: "vocal", title: "t", actionKey: k) }
        let h = HotkeySet.defaults
        #expect(r("1").actionTaps(h) == [.fn] && r("2").actionTaps(h) == [.ctrl] && r("4").actionTaps(h) == [.cmd])
        #expect(r("vocal").actionTaps(h) == [.ctrl, .option])
        #expect(r(nil).actionTaps(h) == nil && r("9").actionTaps(h) == nil)
    }

    @Test func cancelledVocalToast() {
        let nova = VocalAgent("nova", "Nova")
        let r = VocalResult.parse(stdout: #"{"ok": false, "to": "nova", "error": "cancelled (esc)"}"#, exitCode: 1)
        #expect(r.toast(agent: nova) == "Not sent to Nova")
    }
}
