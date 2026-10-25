import Foundation

/// W16 page actions: the label rules shared by oi-hud (page read + press) and oi-watcher (click
/// tokens), so a learned `click:<host>/<role>:<slug>` and the button re-found at press time agree.
/// Mirrors the python side (`oi.vocab.slug`, `oi.page_actions.IRREVERSIBLE`).
public enum PageLabel {
    public static let slugMax = 30

    /// Lower-case ASCII slug with "-" separators, at most `max` chars ("" when nothing survives): NFKD,
    /// non-ASCII dropped, every run of non [a-z0-9] → "-", trimmed. Same output as python `vocab.slug`.
    public static func slug(_ text: String?, max n: Int = slugMax) -> String {
        guard let text, !text.isEmpty else { return "" }
        let ascii = String(String.UnicodeScalarView(text.decomposedStringWithCompatibilityMapping.unicodeScalars
            .filter { $0.isASCII })).lowercased()
        var out = ""
        var dash = false
        for ch in ascii.unicodeScalars {
            if ("a"..."z").contains(ch) || ("0"..."9").contains(ch) {
                if dash, !out.isEmpty { out.append("-") }
                dash = false
                out.unicodeScalars.append(ch)
            } else {
                dash = true
            }
        }
        var cut = String(out.prefix(n))
        while cut.hasSuffix("-") { cut.removeLast() }
        return cut
    }

    /// AX role → token role: AXButton (and its pop-up / menu variants) = "button", AXLink = "link";
    /// anything else (text fields, static text, …) is never a page action and never logged.
    public static func role(ax: String?) -> String? {
        switch ax {
        case "AXButton", "AXPopUpButton", "AXMenuButton": return "button"
        case "AXLink": return "link"
        default: return nil
        }
    }

    /// Irreversible verbs (card: merge, delete, send, pay, publish, close, + siblings), matched on the
    /// slug's whole words: "squash-and-merge" yes, "merged-by" no.
    public static let irreversibleWords: Set<String> = ["merge", "delete", "send", "pay", "publish", "close", "remove",
                                                        "discard", "submit", "archive", "transfer", "purchase", "buy"]

    public static func irreversible(slug: String) -> Bool {
        slug.split(separator: "-").contains { irreversibleWords.contains(String($0)) }
    }

    /// "button:squash-and-merge" → ("button", "squash-and-merge"); nil when malformed.
    public static func target(_ s: String?) -> (role: String, slug: String)? {
        guard let s, let i = s.firstIndex(of: ":") else { return nil }
        let role = String(s[..<i]), sl = String(s[s.index(after: i)...])
        guard role == "button" || role == "link", !sl.isEmpty, slug(sl) == sl else { return nil }
        return (role, sl)
    }
}
