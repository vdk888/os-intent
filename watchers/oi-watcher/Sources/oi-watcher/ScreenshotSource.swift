import AppKit
import OIWatcherCore
import OISettings

/// W17: new screenshots → bucket `oi-watcher-shot_<host>` as {"kind": "shot", "src", "path"}.
///  - files: a DispatchSource on the screenshot folder (`defaults read com.apple.screencapture location`,
///    default ~/Desktop; $OI_SHOT_DIR overrides it for tests), plus a 10 s safety rescan; the folder is
///    re-resolved every 60 s (a location changed in the Screenshot app). Files present at start are never
///    reported, nor files older than 60 s (a folder moved in).
///  - clipboard: NSPasteboard.changeCount polled every 1 s; on a change whose types hold public.png /
///    public.tiff (and no concealed/transient marker), the image is saved as PNG to
///    <data dir>/shots/clip-<stamp>.png (0600, the 30 newest kept) and that path is posted.
/// settings.json `shots.enabled` / `shots.clipboard` (OISettings.shotFlags) are honoured on each event; while
/// logging is `paused` the clipboard image is never read nor saved, and no screenshot is posted.
/// Logs carry counts and the source only: never a file name or a path.
/// (context is state, not timers): the screenshot's LIVE state, posted to the same bucket as
/// {"kind": "shot_state", "state": "live" | "gone", "src"}:
///  - "thumbnail": the on-screen window list (CGWindowListCopyWindowInfo, owner names and bounds only, no window
///    title, no pixels) polled every 0.3 s: live while macOS's floating thumbnail or the screenshot / markup window
///    is on screen (ShotWindows), gone when it goes (dragged out, clicked away, dismissed, saved);
///  - "clipboard": a clipboard screenshot is live while it is still the clipboard's content (gone at the next
///    clipboard change).
/// A new file is posted after a fresh window poll, so the bucket always says "live" before the file it shows.
@MainActor
final class ScreenshotSource {
    static let rescanInterval: TimeInterval = 10
    static let locationInterval: TimeInterval = 60
    static let clipboardInterval: TimeInterval = 1
    static let maxAge: TimeInterval = 60
    static let keepClips = 30
    static let windowInterval: TimeInterval = 0.3     // poll cadence of the window list (not a lifetime)

    private let sink: AWSink
    private let settings = SettingsCache()
    private var folder = ""
    private var customName: String?
    private var seen: Set<String> = []
    private var dirSource: DispatchSourceFileSystemObject?
    private var timers: [Timer] = []
    private var clip = ChangeCounter()
    private var windowEdge = LiveEdge()
    private var clipLive = false

    init(sink: AWSink) { self.sink = sink }

    func start() {
        resolveFolder(initial: true)
        clip = ChangeCounter(NSPasteboard.general.changeCount)
        timers.append(Timer.scheduledTimer(withTimeInterval: Self.rescanInterval, repeats: true) { _ in
            MainActor.assumeIsolated { self.scan() }
        })
        timers.append(Timer.scheduledTimer(withTimeInterval: Self.locationInterval, repeats: true) { _ in
            MainActor.assumeIsolated { self.resolveFolder(initial: false) }
        })
        timers.append(Timer.scheduledTimer(withTimeInterval: Self.clipboardInterval, repeats: true) { _ in
            MainActor.assumeIsolated { self.pollClipboard() }
        })
        windowEdge = LiveEdge(Self.shotOnScreen())        // a thumbnail already up at start is not announced
        timers.append(Timer.scheduledTimer(withTimeInterval: Self.windowInterval, repeats: true) { _ in
            MainActor.assumeIsolated { self.pollWindows() }
        })
        Log.info("shots: watching the screenshot folder (\(folder == NSHomeDirectory() + "/Desktop" ? "Desktop" : "custom location")) and the clipboard (poll 1 s)")
        if Self.thumbnailDelaysFiles() {
            Hints.once("shot-thumbnail", "shots: the floating screenshot thumbnail is on: macOS writes the file only when it slides away (~5 s); clipboard shots (⌘⇧⌃4) are instant")
        }
    }

    // MARK: folder

    nonisolated static func defaultsRead(_ key: String) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/defaults")
        p.arguments = ["read", "com.apple.screencapture", key]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else { return nil }
        let s = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return s.isEmpty ? nil : s
    }

    /// `show-thumbnail` defaults to on: the file then lands only after the thumbnail goes away.
    nonisolated static func thumbnailDelaysFiles() -> Bool {
        defaultsRead("show-thumbnail").map { $0 != "0" } ?? true
    }

    nonisolated static func currentFolder() -> String {
        ShotFiles.location(defaultsValue: defaultsRead("location"), home: NSHomeDirectory(),
                           override: ProcessInfo.processInfo.environment["OI_SHOT_DIR"]) { path in
            var isDir: ObjCBool = false
            return FileManager.default.fileExists(atPath: path, isDirectory: &isDir) && isDir.boolValue
        }
    }

    private func resolveFolder(initial: Bool) {
        let f = Self.currentFolder()
        customName = Self.defaultsRead("name")
        guard f != folder else { return }
        folder = f
        seen = Set(listing())           // what is there now is never reported
        dirSource?.cancel()
        dirSource = nil
        let fd = open(f, O_EVTONLY)
        guard fd >= 0 else { Log.info("shots: screenshot folder not readable; rescanning every \(Int(Self.rescanInterval)) s"); return }
        let src = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .rename, .delete],
                                                            queue: .main)
        src.setEventHandler { MainActor.assumeIsolated { self.scan() } }
        src.setCancelHandler { close(fd) }
        src.resume()
        dirSource = src
        if !initial { Log.info("shots: screenshot folder changed") }
    }

    private func listing() -> [String] {
        (try? FileManager.default.contentsOfDirectory(atPath: folder)) ?? []
    }

    private func scan() {
        let names = listing()
        let fresh = ShotFiles.newShots(in: names, seen: seen, customName: customName)
        seen.formUnion(fresh)
        if seen.count > 5000 { seen = Set(names) }
        let s = settings.current
        guard !fresh.isEmpty, !s.paused, s.shotFlags.files else { return }   // paused: seen, never posted later
        pollWindows()                    // "live" goes out before the file it shows
        let now = Date()
        for name in fresh {
            let path = (folder as NSString).appendingPathComponent(name)
            let attrs = try? FileManager.default.attributesOfItem(atPath: path)
            let born = (attrs?[.creationDate] as? Date) ?? (attrs?[.modificationDate] as? Date) ?? .distantPast
            guard now.timeIntervalSince(born) <= Self.maxAge else { continue }
            post(path: path, src: "file")
        }
    }

    // MARK: clipboard

    // MARK: live state

    /// The global top-left frames of the on-screen windows (owner, layer, bounds, alpha only) and of the screens.
    nonisolated static func windowList() -> [ShotWindows.Window] {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
                as? [[String: Any]] else { return [] }
        return list.compactMap { w in
            guard let owner = w[kCGWindowOwnerName as String] as? String, ShotWindows.owners.contains(owner),
                  let b = w[kCGWindowBounds as String] as? [String: Any],
                  let r = CGRect(dictionaryRepresentation: b as CFDictionary) else { return nil }
            return ShotWindows.Window(owner: owner, layer: w[kCGWindowLayer as String] as? Int ?? 0,
                                      x: r.minX, y: r.minY, w: r.width, h: r.height,
                                      alpha: w[kCGWindowAlpha as String] as? Double ?? 1)
        }
    }

    static func screens() -> [ShotWindows.Screen] {
        let mainH = NSScreen.screens.first?.frame.height ?? 0     // AppKit is bottom-left, CG top-left
        return NSScreen.screens.map { s in
            ShotWindows.Screen(x: s.frame.minX, y: mainH - s.frame.maxY, w: s.frame.width, h: s.frame.height)
        }
    }

    static func shotOnScreen() -> Bool { ShotWindows.live(windowList(), screens: screens()) }

    private func pollWindows() {
        guard let edge = windowEdge.update(Self.shotOnScreen()) else { return }
        let s = settings.current
        guard !s.paused, s.shotFlags.files || s.shotFlags.clipboard else { return }
        postState(edge, src: "thumbnail")
    }

    private func postState(_ state: String, src: String) {
        let ev = AWEvent.shotState(at: Date(), state: state, src: src)
        Log.info("shots: \(src) \(state)")
        let sink = self.sink
        Task { await sink.post(.shot, ev) }
    }

    private func pollClipboard() {
        let pb = NSPasteboard.general
        guard clip.changed(pb.changeCount) else { return }   // counted even while paused: never read later
        if clipLive {                                         // the clipboard screenshot is no longer the content
            clipLive = false
            postState("gone", src: "clipboard")
        }
        let s = settings.current
        guard !s.paused, s.shotFlags.clipboard else { return }   // paused logging: no read, no save, no post
        // types only (metadata): the data is read only for an image change
        guard let type = ClipboardShot.imageType(in: (pb.types ?? []).map(\.rawValue)),
              let data = pb.data(forType: NSPasteboard.PasteboardType(type)) else { return }
        guard let png = Self.pngData(data, type: type) else { return }
        do {
            let dir = SettingsStore.dataDir().appendingPathComponent("shots", isDirectory: true)
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            let url = dir.appendingPathComponent(ClipboardShot.fileName(at: Date()))
            guard FileManager.default.createFile(atPath: url.path, contents: png, attributes: [.posixPermissions: 0o600]) else {
                Log.info("shots: clipboard image not saved"); return
            }
            let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
            for old in ClipboardShot.prune(names, keep: Self.keepClips) {
                try? FileManager.default.removeItem(at: dir.appendingPathComponent(old))
            }
            post(path: url.path, src: "clipboard")
            clipLive = true
        } catch {
            Log.info("shots: shots/ not writable")
        }
    }

    nonisolated static func pngData(_ data: Data, type: String) -> Data? {
        if type == "public.png" { return data }
        return NSBitmapImageRep(data: data)?.representation(using: .png, properties: [:])
    }

    private func post(path: String, src: String) {
        let ev = AWEvent.shot(at: Date(), path: path, src: src)
        Log.info("shots: new screenshot (\(src))")
        let sink = self.sink
        Task { await sink.post(.shot, ev) }
    }
}
