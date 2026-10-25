import Foundation

/// Space index/delta logic, independent of the private CGS calls that feed it.
public enum Spaces {
    /// 1-based position of `active` among the spaces of the display that holds it
    /// (Mission Control order, full-screen spaces included). nil if not found.
    public static func index(of active: UInt64, inDisplays displays: [[UInt64]]) -> Int? {
        for spaces in displays {
            if let i = spaces.firstIndex(of: active) { return i + 1 }
        }
        return nil
    }

    /// +1 / -1 from two known indices, 0 when either is unknown or equal.
    public static func delta(previous: Int?, current: Int?) -> Int {
        guard let p = previous, let c = current else { return 0 }
        return c > p ? 1 : (c < p ? -1 : 0)
    }

    /// Parse the array returned by CGSCopyManagedDisplaySpaces (bridged to Swift)
    /// into per-display lists of space ids. Uses "id64", falls back to "ManagedSpaceID".
    public static func displaySpaceIDs(from managed: [[String: Any]]) -> [[UInt64]] {
        managed.map { display in
            let spaces = display["Spaces"] as? [[String: Any]] ?? []
            return spaces.compactMap { s in
                guard let v = (s["id64"] as? Int) ?? (s["ManagedSpaceID"] as? Int), v >= 0 else { return nil }
                return UInt64(v)
            }
        }
    }
}
