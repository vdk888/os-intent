import Foundation

/// The vocal recipients the Swift side shows (oi-hud's agent picker, oi-menubar's Settings): settings.json
/// `vocal_recipients` per slug (`null` removes one, a new slug is added, `name` renames) over `defaults`, which ship
/// EMPTY like `oi.vocal.DEFAULT_RECIPIENTS` (keep the two in step): no agent configured = no agent shown, never an
/// invented one. The python side owns delivery and validation.
public enum VocalRecipients {
    public static let defaults: [(slug: String, name: String)] = []

    public static func list(_ s: OISettings) -> [(slug: String, name: String)] {
        var out = defaults
        guard case .object(let over)? = s.vocalRecipients else { return out }
        for (slug, spec) in over.sorted(by: { $0.key < $1.key }) {
            if case .null = spec { out.removeAll { $0.slug == slug }; continue }
            guard case .object(let o) = spec else { continue }
            let name: String? = { if case .string(let n)? = o["name"], !n.isEmpty { return n }; return nil }()
            if let i = out.firstIndex(where: { $0.slug == slug }) { if let name { out[i].name = name } }
            else { out.append((slug, name ?? slug.capitalized)) }
        }
        return out
    }
}
