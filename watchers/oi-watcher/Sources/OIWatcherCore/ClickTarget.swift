import Foundation
import OISettings

/// W16: what a mouse-up in Safari clicked, from the AX element under the pointer and its ancestors.
/// Logged as `{"host","role","label"}` (label = the SLUG of the button / link label: no page text, no field
/// value), turned into `click:<host>/<role>:<label-slug>` by the normaliser.
public enum ClickTarget {
    /// One node of the chain, from the element under the pointer up to the page (AXWebArea). `label` is
    /// read by the daemon only for button / link roles.
    public struct Node: Equatable, Sendable {
        public var role: String?
        public var label: String?
        public init(role: String?, label: String? = nil) { self.role = role; self.label = label }
    }

    /// Roles whose click is never logged (typing targets).
    public static let fieldRoles: Set<String> = ["AXTextField", "AXTextArea", "AXSecureTextField", "AXComboBox", "AXSearchField"]
    /// How far up from the pointer a button / link may be (a click on the text or icon inside it).
    public static let maxButtonDepth = 6

    /// (role, label slug) of the clicked button / link, or nil: a field, nothing actionable, an empty label,
    /// or a click outside the page (the chain never reaches AXWebArea: Safari's toolbar, tabs, sidebar).
    public static func resolve(_ chain: [Node]) -> (role: String, slug: String)? {
        guard chain.contains(where: { $0.role == "AXWebArea" }) else { return nil }
        for (i, n) in chain.enumerated() {
            if n.role == "AXWebArea" || i > maxButtonDepth { return nil }
            if let r = n.role, fieldRoles.contains(r) { return nil }
            if let role = PageLabel.role(ax: n.role) {
                let s = PageLabel.slug(n.label)
                return s.isEmpty ? nil : (role, s)
            }
        }
        return nil
    }

    /// Whether clicks may be logged: Safari not denylisted in settings.json (`paused` is the sink's job).
    public static func allowed(denylist: [String]) -> Bool {
        !denylist.contains { $0.caseInsensitiveCompare("Safari") == .orderedSame }
    }
}

extension AWEvent {
    public static func click(at t: Date, host: String, role: String, slug: String) -> AWEvent {
        AWEvent(timestamp: t, data: ["host": .string(host), "role": .string(role), "label": .string(slug)])
    }
}
