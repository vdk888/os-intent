import Foundation

/// W17: screenshot → "send to who the user is talking with". `oi live` drives it over the socket:
///  - `{"cmd":"shot_show","options":[…],"ttl_ms":15000}` (the agent picker after "→ other"): `show` with
///    `"live":"shot"` (same validation, same wish + vocal options, not rate-limited, kept while typing);
///  - `{"cmd":"shot_note","title":"→ Atlas · Claude note","text":"…","placeholder":"…"}`: the editable note
///    panel (the Annotate field / the tier-2 Claude note). Answered on the same connection by
///    `{"event":"note","text":"…"}` (Enter) or `{"event":"dismissed","reason":"esc"|"click_outside"|"replaced"}`.
///    The text is never logged (only its length);
///  - `{"cmd":"debug_note","text":"…"}` (tests, demo): submit the open note panel as Enter would, `text` nil =
///    keep what is in it.
public struct ShotNoteRequest: Equatable, Sendable {
    public var title: String
    public var text: String
    public var placeholder: String

    public static let maxText = 4000
    public static let maxTitle = 60

    public init(title: String, text: String = "", placeholder: String = "") {
        self.title = String(title.prefix(Self.maxTitle))
        self.text = String(text.prefix(Self.maxText))
        self.placeholder = String(placeholder.prefix(200))
    }

    /// The text the user sends: trimmed, capped. Empty is allowed (tier 1 with the context line only).
    public static func clean(_ text: String) -> String {
        String(text.trimmingCharacters(in: .whitespacesAndNewlines).prefix(maxText))
    }
}

/// The note panel's keys: Enter sends, ⇧/⌥-Enter = a new line, Esc cancels.
public enum ShotNoteKey: Equatable, Sendable {
    case send, newline, cancel, other

    public static func classify(isReturn: Bool, isEscape: Bool, shift: Bool, option: Bool) -> ShotNoteKey {
        if isEscape { return .cancel }
        if isReturn { return (shift || option) ? .newline : .send }
        return .other
    }
}

private struct RawShotNote: Decodable {
    let title: String?
    let text: String?
    let placeholder: String?
}

/// W17 decoding of `shot_show` / `shot_note` / `debug_note` (called by `decodeCommand`).
public func decodeShotCommand(_ cmd: String, _ line: Data) throws -> HUDCommand {
    switch cmd {
    case "shot_show":
        guard var obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else { throw CommandError.badJSON }
        obj["cmd"] = "show"
        obj["live"] = "shot"
        obj["mode"] = nil
        guard let asShow = try? JSONSerialization.data(withJSONObject: obj),
              case .showLive(let options, let ttl, let live) = try decodeCommand(asShow) else { throw CommandError.badJSON }
        return .showLive(options: options, ttlMs: ttl, live: live)
    case "shot_note":
        guard let raw = try? JSONDecoder().decode(RawShotNote.self, from: line) else { throw CommandError.badJSON }
        let title = (raw.title ?? "").trimmingCharacters(in: .whitespaces)
        return .shotNote(ShotNoteRequest(title: title.isEmpty ? "Screenshot note" : title, text: raw.text ?? "",
                                         placeholder: raw.placeholder ?? ""))
    case "debug_note":
        let raw = try? JSONDecoder().decode(RawShotNote.self, from: line)
        return .debugNote(text: raw?.text)
    default:
        throw CommandError.unknownCommand(cmd)
    }
}

/// what may take down a show bound to a live context state. Typing and pointer travel are the user working on
/// the screenshot (renaming it, dragging the thumbnail, marking it up): they never dismiss it; Esc, a press, the
/// ttl (a display setting), `hide` (oi live: the state ended) and the rest do.
public enum LiveBound {
    public static let kept: Set<String> = ["typing", "pointer_fast"]

    public static func dismisses(_ reason: String, live: String?) -> Bool {
        guard let live, !live.isEmpty else { return true }
        return !kept.contains(reason)
    }
}
