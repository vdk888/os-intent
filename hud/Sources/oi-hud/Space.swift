import Foundation

/// Current Space index (1-based on its display) for the wish context, or nil.
/// AMBER: the same private CoreGraphics/SkyLight read-only calls as oi-watcher's SpaceSource
/// (resolved with dlsym so a macOS that drops them degrades to nil instead of failing to launch).
enum SpaceIndex {
    private typealias MainConnectionID = @convention(c) () -> Int32
    private typealias GetActiveSpace = @convention(c) (Int32) -> UInt64
    private typealias CopyManagedDisplaySpaces = @convention(c) (Int32) -> Unmanaged<CFArray>?

    static func current() -> Int? {
        let h = UnsafeMutableRawPointer(bitPattern: -2)  // RTLD_DEFAULT
        guard let a = dlsym(h, "CGSMainConnectionID"), let b = dlsym(h, "CGSGetActiveSpace"),
              let c = dlsym(h, "CGSCopyManagedDisplaySpaces") else { return nil }
        let cid = unsafeBitCast(a, to: MainConnectionID.self)()
        let active = unsafeBitCast(b, to: GetActiveSpace.self)(cid)
        guard active != 0,
              let arr = unsafeBitCast(c, to: CopyManagedDisplaySpaces.self)(cid)?.takeRetainedValue() as? [[String: Any]]
        else { return nil }
        for display in arr {
            let ids = (display["Spaces"] as? [[String: Any]] ?? []).compactMap { s -> UInt64? in
                guard let v = (s["id64"] as? Int) ?? (s["ManagedSpaceID"] as? Int), v >= 0 else { return nil }
                return UInt64(v)
            }
            if let i = ids.firstIndex(of: active) { return i + 1 }
        }
        return nil
    }
}
