import Foundation
import Testing
@testable import HUDCore

// The user 2026-10-21 16:32Z (vocal): « Quand j'envoie un vocal en sélectionnant l'agent, ce serait bien de pouvoir
// taper à la main : si je commence à taper à la main ça arrête d'écouter, comme ça c'est l'un ou l'autre. »
// The flow (listening -> typing, one way), the key rule of the recording panel, and its texts.

@Suite struct VocalTypingFlowTests {
    let nova = VocalAgent("nova", "Nova")

    private func recording() -> VocalFlow {
        var f = VocalFlow()
        _ = f.handle(.vocalKey); _ = f.handle(.pick(nova))
        return f
    }

    @Test func aTypedKeyStopsListeningOnce() {
        var f = recording()
        #expect(!f.isTyping && f.stateName == "recording:nova")
        #expect(f.handle(.typed) == [.startTyping])
        #expect(f.isTyping && f.isRecording && f.stateName == "typing:nova")
        #expect(f.handle(.typed).isEmpty)                                 // already typing: nothing more
    }

    @Test func typingSendsWithEnterOrTheVocalKeyAndEscCancels() {
        var f = recording()
        _ = f.handle(.typed)
        #expect(f.handle(.enter) == [.send(nova)] && f.state == .idle && !f.typing)
        f = recording(); _ = f.handle(.typed)
        #expect(f.handle(.vocalKey) == [.send(nova)] && !f.typing)
        f = recording(); _ = f.handle(.typed)
        #expect(f.handle(.cancel) == [.cancelRecording] && f.state == .idle && !f.typing)
    }

    @Test func theTimeLimitNeverSendsWhileTyping() {
        var f = recording()
        _ = f.handle(.typed)
        #expect(f.handle(.dictationEnded).isEmpty && f.isTyping)          // the limit is the microphone's
        #expect(f.handle(.enter) == [.send(nova)])
    }

    @Test func dictationUnavailableBecomesTypingNotASend() {
        var f = recording()
        #expect(f.handle(.dictationUnavailable) == [.startTyping] && f.isTyping)
        #expect(f.handle(.enter) == [.send(nova)])
    }

    @Test func typedOutsideTheRecordingDoesNothing() {
        var f = VocalFlow()
        #expect(f.handle(.typed).isEmpty && f.state == .idle && !f.isTyping)
        _ = f.handle(.vocalKey)
        #expect(f.handle(.typed).isEmpty && f.isPicking && !f.typing)
        #expect(f.handle(.dictationUnavailable).isEmpty)
    }

    @Test func aNewRecordingStartsListeningAgain() {
        var f = recording()
        _ = f.handle(.typed); _ = f.handle(.enter)
        f = { var g = f; _ = g.handle(.vocalKey); _ = g.handle(.pick(nova)); return g }()
        #expect(f.isRecording && !f.isTyping && f.stateName == "recording:nova")
    }
}

@Suite struct VocalKeyRuleTests {
    private func listening(_ code: UInt16, _ chars: String?, ign: String? = nil, cmd: Bool = false, ctrl: Bool = false) -> VocalPanelKey {
        VocalKeyRule.classify(keyCode: code, characters: chars, ignoringModifiers: ign, command: cmd, control: ctrl, typing: false)
    }
    private func typing(_ code: UInt16, _ chars: String?, cmd: Bool = false) -> VocalPanelKey {
        VocalKeyRule.classify(keyCode: code, characters: chars, command: cmd, typing: true)
    }

    @Test func returnSendsAndEscCancelsInBothModes() {
        for t in [false, true] {
            #expect(VocalKeyRule.classify(keyCode: 36, characters: "\r", typing: t) == .send)
            #expect(VocalKeyRule.classify(keyCode: 76, characters: "\u{3}", typing: t) == .send)   // keypad Enter
            #expect(VocalKeyRule.classify(keyCode: 53, characters: "\u{1B}", typing: t) == .cancel)
        }
    }

    @Test func writingKeysStartTypingWhileListening() {
        #expect(listening(0, "a") == .startTyping)
        #expect(listening(8, "c") == .startTyping)                      // C is a letter here, never the capture toggle
        #expect(listening(8, "C") == .startTyping)
        #expect(listening(18, "&") == .startTyping)                     // AZERTY 1 row
        #expect(listening(49, " ") == .startTyping)                     // space
        #expect(listening(14, "é") == .startTyping)
        #expect(listening(33, "", ign: "^") == .startTyping)             // a dead key (^): nothing yet, it writes
        #expect(listening(51, "\u{7F}") == .startTyping)                // Delete: edit the transcript
        #expect(listening(123, "\u{F702}") == .startTyping)             // ←
    }

    @Test func chordsAndSilentKeysDoNothingWhileListening() {
        #expect(listening(8, "c", cmd: true) == .ignore)                 // ⌘C
        #expect(listening(9, "v", cmd: true) == .ignore)                 // ⌘V
        #expect(listening(0, "\u{1}", ctrl: true) == .ignore)            // ⌃A
        #expect(listening(48, "\t") == .ignore)                          // Tab
        #expect(listening(122, "\u{F704}") == .ignore)                   // F1
        #expect(listening(200, nil) == .ignore)
        #expect(listening(200, "") == .ignore)
    }

    @Test func everyOtherKeyGoesToTheFieldWhileTyping() {
        #expect(typing(8, "c") == .field)                                // C types a c: no screenshot toggle
        #expect(typing(51, "\u{7F}") == .field)
        #expect(typing(9, "v", cmd: true) == .field)
        #expect(typing(48, "\t") == .field)
    }
}

@Suite struct VocalModeTextTests {
    let nova = VocalAgent("nova", "Nova")

    @Test func indicatorFollowsTheDictationLocale() {
        #expect(VocalModeText.indicator(typing: false, locale: "fr-FR") == "Écoute…")
        #expect(VocalModeText.indicator(typing: true, locale: "fr-FR") == "Saisie")
        #expect(VocalModeText.indicator(typing: false, locale: "en-US") == "listening…")
        #expect(VocalModeText.indicator(typing: true, locale: "en-US") == "typing")
    }

    @Test func hintNamesTheRuleAndTheLimitOnlyWhileListening() {
        let l = VocalModeText.hint(typing: false, key: "⌃⌥", limitS: 300, locale: "fr-FR")
        #expect(l == "⌃⌥ ou Entrée envoie · Échap annule · tapez pour écrire · 5 min max")
        #expect(VocalModeText.hint(typing: true, key: "⌃⌥", limitS: 300, locale: "fr-FR") == "Entrée ou ⌃⌥ envoie · Échap annule")
        #expect(VocalModeText.hint(typing: false, key: "⌃⌥", limitS: 90, locale: "en-US")
                == "⌃⌥ or Enter sends · Esc cancels · type to write · 90 s max")
        #expect(VocalModeText.hint(typing: true, key: "⌃⌥", limitS: 90, locale: "en-US") == "Enter or ⌃⌥ sends · Esc cancels")
    }

    @Test func fieldHasAVoiceOverLabel() {
        #expect(VocalModeText.fieldLabel(agent: nova, locale: "fr-FR") == "Message à Nova")
        #expect(VocalModeText.fieldLabel(agent: nova, locale: "en-US") == "Message to Nova")
    }
}
