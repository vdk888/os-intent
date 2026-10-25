import Foundation

/// The Safari front tab as logged. Normalisation keeps scheme + host + path only:
/// user:password@, the query string and the fragment are dropped (they can carry
/// tokens; W1 only needs host + first path segment). Non-web pages (start page,
/// about:blank, favorites://, bookmarks) are not logged.
public struct SafariTab: Equatable, Sendable {
    public let url: String
    public let title: String

    public static let maxTitleLength = 300

    public init(url: String, title: String) {
        self.url = url
        self.title = title
    }

    /// nil when there is nothing worth logging. `rawURL`/`rawTitle` are what
    /// AppleScript returned ("missing value" is AppleScript's null).
    public static func normalise(rawURL: String?, rawTitle: String?) -> SafariTab? {
        guard let u = clean(rawURL), let comps = URLComponents(string: u),
              let scheme = comps.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = comps.host?.lowercased(), !host.isEmpty else { return nil }
        var out = URLComponents()
        out.scheme = scheme
        out.host = host
        out.port = comps.port
        out.percentEncodedPath = comps.percentEncodedPath.isEmpty ? "/" : comps.percentEncodedPath
        guard let url = out.string else { return nil }
        var title = clean(rawTitle) ?? ""
        if title.count > maxTitleLength { title = String(title.prefix(maxTitleLength)) }
        return SafariTab(url: url, title: title)
    }

    static func clean(_ s: String?) -> String? {
        guard let t = s?.trimmingWhitespace(), !t.isEmpty, t != "missing value" else { return nil }
        return t
    }
}
