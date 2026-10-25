import Foundation

/// The « Laya » switch.
/// State comes from `oi laya-switch status --json` (oi/laya_switch.py); the click runs `oi laya-switch on|off`,
/// which persists settings `laya.enabled` and stops the server, or starts it under the memory rule.
public struct LayaSwitchStatus: Decodable, Equatable, Sendable {
    public var enabled: Bool
    public var state: String      // disabled | running | loading | starting | refused | stopped
    public var detail: String
    public var up: Bool

    public init(enabled: Bool, state: String, detail: String = "", up: Bool = false) {
        self.enabled = enabled; self.state = state; self.detail = detail; self.up = up
    }

    public static func decode(_ data: Data) -> LayaSwitchStatus? {
        try? JSONDecoder().decode(LayaSwitchStatus.self, from: data)
    }

    /// The status line under the item.
    public static func text(_ s: LayaSwitchStatus?, busy: Bool = false) -> String {
        guard let s else { return "n/a (oi venv missing)" }
        if busy { return s.enabled ? "arrêt…" : "démarrage…" }     // the click flips `enabled`
        switch s.state {
        case "running": return "en marche · modèle chargé"
        case "loading": return "en marche · chargement du modèle…"
        case "starting": return "démarrage…"
        case "refused": return "refusé : mémoire"
        case "disabled": return s.up ? "arrêt en cours…" : "arrêté"
        default: return "arrêté"
        }
    }
}

extension MenuModel {
    /// The « Laya » item (checkmark = laya.enabled) + its status line.
    public static func layaEntries(_ s: Status) -> [MenuEntry] {
        let st = s.laya
        let tip = "Off: Markov only, the Laya server (~3 GB) is stopped and stays off after a reboot. "
            + "On: started under the memory rule (refused when swap ≥ 4096 MB)."
        var line = LayaSwitchStatus.text(st, busy: s.layaBusy)
        if st?.state == "refused", let d = st?.detail, !d.isEmpty, !s.layaBusy { line += " (\(d))" }
        return [
            .init(id: "laya", title: "Laya", kind: .action(.toggleLaya), checked: st?.enabled ?? false,
                  enabled: st != nil && !s.layaBusy, tooltip: tip),
            .init(id: "laya.state", title: line, kind: .info, enabled: false, indent: 1),
        ]
    }
}
