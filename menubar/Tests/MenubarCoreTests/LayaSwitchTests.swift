import Foundation
import Testing
@testable import MenubarCore

/// 2026-10-21: the « Laya » item (checkmark = laya.enabled) + its status line.
@Suite struct LayaSwitchTests {
    @Test func decodesThePythonStatus() {
        let json = #"{"enabled": true, "state": "refused", "detail": "swap used 5000 MB >= 4096 MB", "up": false, "url": "http://127.0.0.1:8781"}"#
        let s = LayaSwitchStatus.decode(Data(json.utf8))
        #expect(s == LayaSwitchStatus(enabled: true, state: "refused", detail: "swap used 5000 MB >= 4096 MB", up: false))
        #expect(LayaSwitchStatus.decode(Data("{}".utf8)) == nil)
    }

    @Test func statusLines() {
        func t(_ st: String, enabled: Bool = true, up: Bool = false) -> String {
            LayaSwitchStatus.text(LayaSwitchStatus(enabled: enabled, state: st, up: up))
        }
        #expect(t("running", up: true) == "en marche · modèle chargé")
        #expect(t("loading", up: true) == "en marche · chargement du modèle…")
        #expect(t("starting") == "démarrage…")
        #expect(t("refused") == "refusé : mémoire")
        #expect(t("stopped") == "arrêté")
        #expect(t("disabled", enabled: false) == "arrêté")
        #expect(t("disabled", enabled: false, up: true) == "arrêt en cours…")
        #expect(LayaSwitchStatus.text(nil) == "n/a (oi venv missing)")
        #expect(LayaSwitchStatus.text(LayaSwitchStatus(enabled: true, state: "running"), busy: true) == "arrêt…")
        #expect(LayaSwitchStatus.text(LayaSwitchStatus(enabled: false, state: "disabled"), busy: true) == "démarrage…")
    }

    @Test func menuItemChecksFollowEnabled() {
        var s = Status()
        s.laya = LayaSwitchStatus(enabled: true, state: "running", up: true)
        var e = MenuModel.entries(s)
        let i = e.firstIndex { $0.id == "laya" }!
        #expect(e[i - 1].id == "hudtoggle" && e[i].kind == .action(.toggleLaya) && e[i].checked && e[i].enabled)
        #expect(e[i + 1].title == "en marche · modèle chargé" && e[i + 1].kind == .info)
        #expect(MenuModel.render(e).contains("[x] Laya\n  en marche · modèle chargé"))

        s.laya = LayaSwitchStatus(enabled: false, state: "disabled")
        e = MenuModel.entries(s)
        #expect(MenuModel.render(e).contains("[ ] Laya\n  arrêté"))

        s.laya = LayaSwitchStatus(enabled: true, state: "refused", detail: "swap used 5000 MB >= 4096 MB")
        #expect(MenuModel.entries(s).first { $0.id == "laya.state" }?.title == "refusé : mémoire (swap used 5000 MB >= 4096 MB)")

        s.layaBusy = true                                    // a switch is running: greyed, no double click
        #expect(MenuModel.entries(s).first { $0.id == "laya" }?.enabled == false)
        #expect(MenuModel.entries(Status()).first { $0.id == "laya" }?.enabled == false)   // n/a: greyed
        #expect(Set(e.map(\.id)).count == e.count)
    }
}
