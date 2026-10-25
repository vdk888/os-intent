import AppKit
import OIWatcherCore

/// AMBER: private CoreGraphics/SkyLight symbols, resolved at runtime with dlsym so a
/// future macOS that drops them degrades to `space: null, delta: 0` instead of failing
/// to launch. Read-only calls; no window-server state is changed.
struct CGSPrivate {
    typealias MainConnectionID = @convention(c) () -> Int32
    typealias GetActiveSpace = @convention(c) (Int32) -> UInt64
    typealias CopyManagedDisplaySpaces = @convention(c) (Int32) -> Unmanaged<CFArray>?

    let mainConnectionID: MainConnectionID
    let getActiveSpace: GetActiveSpace
    let copyManagedDisplaySpaces: CopyManagedDisplaySpaces

    static func load() -> CGSPrivate? {
        // CoreGraphics re-exports these from SkyLight; AppKit has already loaded both.
        _ = dlopen("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics", RTLD_LAZY)
        let h = UnsafeMutableRawPointer(bitPattern: -2)  // RTLD_DEFAULT
        guard let a = dlsym(h, "CGSMainConnectionID"),
              let b = dlsym(h, "CGSGetActiveSpace"),
              let c = dlsym(h, "CGSCopyManagedDisplaySpaces") else { return nil }
        return CGSPrivate(
            mainConnectionID: unsafeBitCast(a, to: MainConnectionID.self),
            getActiveSpace: unsafeBitCast(b, to: GetActiveSpace.self),
            copyManagedDisplaySpaces: unsafeBitCast(c, to: CopyManagedDisplaySpaces.self))
    }

    /// (active space id, 1-based index on its display) or nil.
    func current() -> (id: UInt64, index: Int?)? {
        let cid = mainConnectionID()
        let active = getActiveSpace(cid)
        guard active != 0 else { return nil }
        guard let arr = copyManagedDisplaySpaces(cid)?.takeRetainedValue() as? [[String: Any]] else {
            return (active, nil)
        }
        return (active, Spaces.index(of: active, inDisplays: Spaces.displaySpaceIDs(from: arr)))
    }
}

@MainActor
final class SpaceSource {
    private let sink: AWSink
    private let cgs = CGSPrivate.load()
    private var lastIndex: Int?
    private var observer: NSObjectProtocol?

    init(sink: AWSink) { self.sink = sink }

    func start() {
        lastIndex = cgs?.current()?.index
        if cgs == nil {
            Log.info("space: private CGS API unavailable; logging space=null delta=0")
        }
        observer = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.activeSpaceDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.changed() }
        }
        Log.info("space: observing activeSpaceDidChange (index via CGS: \(cgs != nil ? "yes" : "no"))")
    }

    private func changed() {
        let idx = cgs?.current()?.index
        let delta = Spaces.delta(previous: lastIndex, current: idx)
        lastIndex = idx
        let ev = AWEvent.space(at: Date(), index: idx, delta: delta)
        let sink = self.sink
        Task { await sink.post(.space, ev) }
    }
}
