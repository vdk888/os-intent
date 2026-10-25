import Foundation
import CoreServices
import OIWatcherCore
import OISettings

/// oi live's « files » connector, sensed here (health check 2026-10-21: TCC denies ~/Desktop to oi live's launchd
/// Python, 148 failed polls in a row; this signed app already reads the screenshot folder there; 
/// « via oi-watcher »). Every `interval` s, for each folder of settings.json `proactive.files.folders` (only while
/// `proactive.sources.files` is true): list it (names only), post each NEW top-level entry to bucket
/// `oi-watcher-file_<host>` as {"kind": "file", "state": "live", "path", "class", "mtime", "size"} and a
/// {"state": "gone", "reason": removed | opened | dropped} when a live one is removed, opened (Spotlight's
/// kMDItemLastUsedDate changed) or pushed out by 20 newer ones. The first listing of a folder primes (files
/// already there are never events). An unreadable / missing folder is unknown (nothing appears, nothing ends) and
/// its read state goes to `status/oi-watcher-files.json` (FilesStatus), which oi live counts as a failed poll.
/// Never a file's content; logs carry counts only, never a name or a path. While logging is paused nothing is
/// posted: files that appear meanwhile are seen (never posted later), live files keep their state until resume.
@MainActor
final class FolderSource {
    static let interval: TimeInterval = 3
    static let statusEvery: TimeInterval = 30

    private let sink: AWSink
    private let settings = SettingsCache()
    private let home = NSHomeDirectory()
    private var folders: [String] = []
    private var enabled = false
    private var diffs: [String: FolderDiff] = [:]
    private var live = LiveFiles()
    private var read: [String: FolderReadState] = [:]
    private var lastStatus: FilesStatus?
    private var statusAt = Date.distantPast
    private var timer: Timer?

    init(sink: AWSink) { self.sink = sink }

    func start() {
        tick()
        timer = Timer.scheduledTimer(withTimeInterval: Self.interval, repeats: true) { _ in
            MainActor.assumeIsolated { self.tick() }
        }
    }

    private func tick() {
        let s = settings.current
        let want = FileFolders.folders(proactive: s.extra["proactive"], home: home)
        if want != (enabled ? folders : nil) { reconfigure(want) }
        for f in folders { scan(f, paused: s.paused) }
        writeStatus()
    }

    private func reconfigure(_ want: [String]?) {
        let next = want ?? []
        for f in folders where !next.contains(f) {
            diffs[f] = nil; read[f] = nil
            live.forget(folder: f)
        }
        for f in next where diffs[f] == nil { diffs[f] = FolderDiff() }   // a new folder primes on its first listing
        folders = next
        enabled = want != nil
        Log.info(enabled ? "files: watching \(next.count) folder(s) every \(Int(Self.interval)) s" : "files: source off")
    }

    private func scan(_ folder: String, paused: Bool) {
        let names: [String]
        do {
            names = try FileManager.default.contentsOfDirectory(atPath: folder)
        } catch {
            var isDir: ObjCBool = false
            let state: FolderReadState = FileManager.default.fileExists(atPath: folder, isDirectory: &isDir) && isDir.boolValue
                ? .unreadable : .missing
            if read[folder] != state { Log.info("files: a watched folder is \(state.rawValue)") }
            read[folder] = state
            return                                                    // unknown: nothing appears, nothing ends
        }
        if read[folder] != .ok, read[folder] != nil { Log.info("files: a watched folder is readable again") }
        read[folder] = .ok
        var diff = diffs[folder] ?? FolderDiff()
        let fresh = diff.appeared(names)
        diffs[folder] = diff
        guard !paused else { return }                                 // paused: seen, never posted later
        let cls = FileFolders.folderClass(folder, home: home)
        let now = Date()
        for (path, reason) in live.ended(in: folder, listing: Set(names), used: Self.lastUsed) {
            post(.fileGone(at: now, path: path, cls: cls, reason: reason))
        }
        for name in fresh {
            let path = (folder as NSString).appendingPathComponent(name)
            let attrs = try? FileManager.default.attributesOfItem(atPath: path)
            let mtime = (attrs?[.modificationDate] as? Date).map { Int($0.timeIntervalSince1970) } ?? 0
            let size = (attrs?[.size] as? NSNumber)?.intValue ?? 0
            post(.fileLive(at: now, path: path, cls: cls, mtime: mtime, size: size))
            for old in live.appear(path, used: Self.lastUsed(path)) {
                let oc = FileFolders.folderClass((old as NSString).deletingLastPathComponent, home: home)
                post(.fileGone(at: now, path: old, cls: oc, reason: .dropped))
            }
        }
        if !fresh.isEmpty { Log.info("files: \(fresh.count) new file(s)") }
    }

    /// Spotlight's kMDItemLastUsedDate (set when the file is opened), nil when never opened / not indexed.
    nonisolated static func lastUsed(_ path: String) -> String? {
        guard let item = MDItemCreate(kCFAllocatorDefault, path as CFString),
              let d = MDItemCopyAttribute(item, kMDItemLastUsedDate) as? Date else { return nil }
        return SettingsStore.isoTimestamp(d)
    }

    private func post(_ ev: AWEvent) {
        let sink = self.sink
        Task { await sink.post(.file, ev) }
    }

    private func writeStatus() {
        let now = Date()
        var st = FilesStatus(ts: SettingsStore.isoTimestamp(now), pid: getpid(), enabled: enabled,
                             folders: read.filter { folders.contains($0.key) })
        let changed = st.enabled != lastStatus?.enabled || st.folders != lastStatus?.folders
        guard changed || now.timeIntervalSince(statusAt) >= Self.statusEvery else { return }
        st.ts = SettingsStore.isoTimestamp(now)
        do {
            try SettingsStore.atomicWrite(st.encoded(), to: FilesStatus.defaultURL)
            lastStatus = st; statusAt = now
        } catch {
            Hints.once("files-status", "files: cannot write status/oi-watcher-files.json")
        }
    }

    /// For `--once`: the folders the watcher would watch now (nil = the source is off).
    nonisolated static func configured() -> [String]? {
        FileFolders.folders(proactive: SettingsCache().current.extra["proactive"], home: NSHomeDirectory())
    }
}
