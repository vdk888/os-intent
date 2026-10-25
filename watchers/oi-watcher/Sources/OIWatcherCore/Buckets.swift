// ActivityWatch bucket naming. Convention matches aw-watcher-*: "<client-kind>_<hostname>"
// (docs/DESIGN.md: `oi-watcher-space|key|safari_<host>`).

public enum BucketKind: String, CaseIterable, Sendable {
    case space, key, safari
    case click        // W16: a click on a Safari page button / link (label slug only)
    case shot            // W17: new screenshots (file or clipboard image)
    case file            // oi live's « files » connector: new / gone files of proactive.files.folders

    /// Bucket `type` field (aw-watcher-web uses "web.tab.current"; keep that for Safari).
    public var awType: String {
        switch self {
        case .space: return "os.space.change"
        case .key: return "os.keyboard.chord"
        case .safari: return "web.tab.current"
        case .click: return "ui.click"
        case .shot: return "os.screenshot"   // W17
        case .file: return "os.file.state"
        }
    }

    public func bucketID(hostname: String) -> String {
        "oi-watcher-\(rawValue)_\(hostname)"
    }
}

public enum Buckets {
    public static let client = "oi-watcher"

    /// Body for POST /api/0/buckets/<id>.
    public static func createBody(kind: BucketKind, hostname: String) -> [String: String] {
        ["client": client, "type": kind.awType, "hostname": hostname]
    }
}
