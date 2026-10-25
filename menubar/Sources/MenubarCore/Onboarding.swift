import Foundation

/// W22: the "Onboarding…" menu row and the first-launch offer. The onboarding itself is the
/// `oi onboard` session (Python, oi/onboard_agent — W22b: a Claude agent reads the opted-in sources), opened in Terminal through a small `.command` script in the
/// data dir; it writes `profile.yaml` + `starter_catalogue.yaml` there. Done = profile.yaml exists.
public enum Onboarding {
    public static let profile = "profile.yaml"
    public static let script = "onboard.command"
    /// Written once the first-launch offer was made (accepted or not): the offer never comes back by itself.
    public static let offeredMarker = "status/onboarding-offered"

    public static func done(dataDir: URL) -> Bool {
        FileManager.default.fileExists(atPath: dataDir.appendingPathComponent(profile).path)
    }

    /// Offer at launch only when never onboarded and never offered.
    public static func shouldOffer(dataDir: URL) -> Bool {
        !done(dataDir: dataDir)
            && !FileManager.default.fileExists(atPath: dataDir.appendingPathComponent(offeredMarker).path)
    }

    public static func entry(onboarded: Bool) -> MenuEntry {
        MenuEntry(id: "onboarding", title: onboarded ? "Onboarding… (redo)" : "Onboarding… (not done yet)",
                  kind: .action(.openOnboarding),
                  tooltip: "Claude onboards you in Terminal (`oi onboard`, W22b): it reads the sources you opt into "
                      + "(Claude memory, wiki, recent mails, Notion, apps) on your subscription, asks a few questions, "
                      + "and you check the starter catalogue → profile.yaml so the HUD proposes useful actions from "
                      + "day one. Local-only domains are never read; `oi onboard --offline` stays on this Mac.")
    }

    /// The Terminal script: run `oi onboard` from the repo's venv; the window stays open on the result.
    public static func commandScript(oiBinary: String, dataDir: String) -> String {
        """
        #!/bin/zsh
        # Written by oi-menubar (W22): the OS-intent onboarding conversation.
        export OI_DATA_DIR=\(shellQuote(dataDir))
        clear
        \(shellQuote(oiBinary)) onboard
        echo
        echo "Done — you can close this window."

        """
    }

    public static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}
