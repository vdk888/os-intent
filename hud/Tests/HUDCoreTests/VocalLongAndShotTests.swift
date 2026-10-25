import Foundation
import Testing
@testable import HUDCore
import OISettings

// The user 2026-10-21 15:35Z (vocal): "il faut augmenter les 60 secondes max parce que ça me coupe au milieu" and
// "ce serait bien que ça propose d'envoyer un screenshot". The recording limit (settings `vocal.max_s`, default
// 300 s), its countdown, and the screenshot toggle of the outward gate (offered for a vocal only, OFF by default,
// carried by a validate only).

@Suite struct VocalLongAndShotTests {
    private func settings(_ json: String) -> OISettings { OISettings.decode(Data(json.utf8)) }

    @Test func recordingLimitDefaultsToFiveMinutesAndIsClamped() {
        #expect(OISettings().vocalMaxS == 300)
        #expect(settings(#"{"vocal":{"max_s":120}}"#).vocalMaxS == 120)
        #expect(settings(#"{"vocal":{"max_s":3}}"#).vocalMaxS == 15)
        #expect(settings(#"{"vocal":{"max_s":99999}}"#).vocalMaxS == 900)
        #expect(settings(#"{"vocal":{"max_s":"long"}}"#).vocalMaxS == 300)        // wrongly typed = default
        #expect(settings(#"{"vocal":{"shot_offer":false}}"#).vocalMaxS == 300)
    }

    @Test func vocalSettingsSurviveASave() throws {
        let s = settings(#"{"vocal":{"max_s":240,"shot_offer":false}}"#)
        let back = OISettings.decode(try s.encoded())                                  // a menu bar save keeps it
        #expect(back.vocalMaxS == 240)
        #expect(back.extra["vocal"] == .object(["max_s": .number(240), "shot_offer": .bool(false)]))
    }

    @Test func limitTextAndCountdown() {
        #expect(VocalCountdown.limitText(300) == "5 min max")
        #expect(VocalCountdown.limitText(90) == "90 s max")
        #expect(VocalCountdown.text(elapsed: 10, limitS: 300) == nil)                  // "listening…" until the end
        #expect(VocalCountdown.text(elapsed: 284.9, limitS: 300) == nil)
        #expect(VocalCountdown.text(elapsed: 285, limitS: 300) == "sends in 15 s")
        #expect(VocalCountdown.text(elapsed: 288.2, limitS: 300) == "sends in 12 s")
        #expect(VocalCountdown.text(elapsed: 299.9, limitS: 300) == "sends in 1 s")
        #expect(VocalCountdown.text(elapsed: 300.2, limitS: 300) == "sends in 1 s")    // never 0 / negative
    }

    @Test func aFiveMinuteTranscriptIsNotCut() {
        let five = Array(repeating: "mot", count: 1500).joined(separator: " ")          // ~6000 chars
        #expect(VocalText.clean(five) == five)
    }

    @Test func offerShotDecodesForAVocalOnly() throws {
        let v = try decodeCommand(Data(#"{"cmd":"confirm_outward","id":"a","cls":"vocal","title":"Envoyer ce message à Nova ?","offer_shot":true}"#.utf8))
        guard case .confirmOutward(let r) = v else { Issue.record("not a confirm"); return }
        #expect(r.offerShot && r.shotLabel == "Joindre une capture de l'écran (C)")
        let p = try decodeCommand(Data(#"{"cmd":"confirm_outward","id":"b","cls":"page","title":"Cliquer ?","offer_shot":true}"#.utf8))
        guard case .confirmOutward(let q) = p else { Issue.record("not a confirm"); return }
        #expect(!q.offerShot)                                                           // never on a page press
        let none = try decodeCommand(Data(#"{"cmd":"confirm_outward","id":"c","cls":"vocal","title":"t"}"#.utf8))
        guard case .confirmOutward(let n) = none else { Issue.record("not a confirm"); return }
        #expect(!n.offerShot)
        #expect(OutwardConfirmRequest(id: "e", cls: "vocal", title: "t", lang: "en", offerShot: true).shotLabel
                == "Attach a screenshot (C)")
    }

    @Test func toggleIsOffByDefaultAndOnlyAValidateCarriesIt() {
        var d = OutwardDecider()
        d.open(at: 0, offerShot: true)
        #expect(!d.attachShot)
        #expect(d.input(.validate("return"), at: 1) == OutwardDecision(validated: true, reason: "return"))   // off

        d.open(at: 10, offerShot: true)                       // every open starts OFF again
        #expect(!d.attachShot)
        let on = d.toggleShot()
        #expect(on && d.attachShot)
        let off = d.toggleShot()                              // C again: off
        #expect(!off)
        let again = d.toggleShot()
        #expect(again)
        let ok = d.input(.validate("return"), at: 11)
        #expect(ok?.attachShot == true && ok?.validated == true)
        let late = d.toggleShot()                             // decided: no more toggling
        #expect(!late && d.attachShot)

        d.open(at: 20, offerShot: true)
        d.toggleShot()
        #expect(d.input(.cancel("esc"), at: 20.1)?.attachShot == false)   // a cancel never carries it

        d.open(at: 30)                                        // not offered: C does nothing
        let notOffered = d.toggleShot()
        #expect(!notOffered)
        #expect(d.input(.validate("return"), at: 31)?.attachShot == false)
    }

    @Test func eventCarriesAttachAndDisplayOnlyWhenValidated() throws {
        let yes = HUDEvent.outward(id: "a", decision: OutwardDecision(validated: true, reason: "return",
                                                                     attachShot: true, display: 2)).jsonLine()
        let o = try #require(try JSONSerialization.jsonObject(with: Data(yes.utf8)) as? [String: Any])
        #expect(o["attach_shot"] as? Bool == true && o["display"] as? Int == 2)
        let main = HUDEvent.outward(id: "b", decision: OutwardDecision(validated: true, reason: "c", attachShot: true)).jsonLine()
        #expect(main.contains(#""attach_shot":true"#) && !main.contains("display"))
        let no = HUDEvent.outward(id: "c", decision: OutwardDecision(validated: false, reason: "esc",
                                                                    attachShot: true, display: 1)).jsonLine()
        #expect(!no.contains("attach_shot") && !no.contains("display"))
        let plain = HUDEvent.outward(id: "d", decision: OutwardDecision(validated: true, reason: "return")).jsonLine()
        #expect(!plain.contains("attach_shot"))
    }
}
