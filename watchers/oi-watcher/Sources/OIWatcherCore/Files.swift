import Foundation
import OISettings

/// oi live's « files » connector, sensed by the watcher (health check 2026-10-21: the launchd Python process is
/// denied ~/Desktop by TCC, 148 failed polls in a row; the signed oi-watcher.app already reads the screenshot folder
/// there). The folders of settings.json `proactive.files.folders` (default ~/Desktop, a Drive folder under
/// ~/Library/CloudStorage works too), only while `proactive.sources.files` is true: the same keys, the same rules
/// as oi/proactive/config.py. Pure logic (no file system), unit-tested; FolderSource does the reading.
/// Metadata only: a path, its folder class, mtime and size. Never a file's content.
public enum FileFolders {
    public static let maxFolders = 12            // config.py keeps the first 12
    public static let maxLive = 20               // sensors.FILES_LIVE: new files followed at once
    public static let skipPrefixes = [".", "~$", "Screenshot", "Capture d’écran", "Capture d'écran"]   // W17 owns screenshots
    public static let partialExtensions = [".download", ".crdownload", ".part", ".partial", ".tmp"]

    /// sensors.FolderSensor.wanted: hidden files, Office lock files, screenshots and partial downloads are skipped.
    public static func wanted(_ name: String) -> Bool {
        let n = name.precomposedStringWithCanonicalMapping
        if skipPrefixes.contains(where: { n.hasPrefix($0.precomposedStringWithCanonicalMapping) }) { return false }
        let lower = n.lowercased()
        return !partialExtensions.contains { lower.hasSuffix($0) }
    }

    /// The folders to watch from settings.json `proactive` (OISettings keeps it as raw JSON in `extra`): nil when
    /// the files source is off (`sources.files` must be JSON true, like config.enabled_sources), else the
    /// configured strings, `~` expanded like python's expanduser, at most 12; a missing / wrongly typed list =
    /// the default ~/Desktop.
    public static func folders(proactive: JSONValue?, home: String) -> [String]? {
        guard case .object(let p)? = proactive, case .object(let src)? = p["sources"],
              case .bool(true)? = src["files"] else { return nil }
        var list = ["~/Desktop"]
        if case .object(let f)? = p["files"], case .array(let a)? = f["folders"] {
            list = a.compactMap { if case .string(let s) = $0 { let t = s.trimmingCharacters(in: .whitespaces); return t.isEmpty ? nil : t }; return nil }
        }
        return Array(list.prefix(maxFolders)).map { expand($0, home: home) }
    }

    static func expand(_ p: String, home: String) -> String {
        p == "~" ? home : p.hasPrefix("~/") ? home + p.dropFirst() : p
    }

    /// sensors.folder_class: drive (anything under ~/Library/CloudStorage), desktop, downloads, documents, else
    /// the folder name as a slug (≤ 24), "folder" when nothing survives. Informative for bucket readers; oi live
    /// keeps its own folder_class as the event class.
    public static func folderClass(_ path: String, home: String) -> String {
        if path.contains("/Library/CloudStorage/") { return "drive" }
        for name in ["Desktop", "Downloads", "Documents"] {
            let base = home + "/" + name
            if path == base || path.hasPrefix(base + "/") { return name.lowercased() }
        }
        return slug((path as NSString).lastPathComponent, max: 24)
    }

    static func slug(_ text: String, max: Int) -> String {
        let ascii = text.applyingTransform(.stripDiacritics, reverse: false)?.lowercased() ?? text.lowercased()
        var out = ""
        var dash = false
        for ch in ascii.unicodeScalars {
            if ("a"..."z").contains(ch) || ("0"..."9").contains(ch) {
                if dash && !out.isEmpty { out.append("-") }
                out.unicodeScalars.append(ch)
                dash = false
            } else { dash = true }
        }
        let cut = String(out.prefix(max))
        let s = cut.hasSuffix("-") ? String(cut.dropLast()) : cut
        return s.isEmpty ? "folder" : s
    }
}

/// The new entries of one folder listing against the previous one. The FIRST listing primes: what is there when
/// the watcher starts (or starts watching that folder) is a backlog, never news (docs/DESIGN.md contract rule 4).
public struct FolderDiff: Sendable {
    public private(set) var known: Set<String>?
    public init() {}

    /// The wanted names that were not in the previous listing (sorted); [] on the priming listing.
    public mutating func appeared(_ names: [String]) -> [String] {
        let now = Set(names.filter(FileFolders.wanted))
        defer { known = now }
        guard let k = known else { return [] }
        return now.subtracting(k).sorted()
    }
}

/// The files that are live: appeared since the watcher started, not opened, not removed. STATE, not timers: a file
/// stays live however long it waits; it ends when it is no longer listed (removed / moved away), when Spotlight's
/// last-used date changes (opened), or when more than `maxLive` newer files push it out ("dropped").
public struct LiveFiles: Sendable {
    public struct Entry: Equatable, Sendable {
        public var path: String
        public var used: String?        // kMDItemLastUsedDate when it appeared (nil = never opened)
    }
    public enum Reason: String, Sendable { case removed, opened, dropped }

    public private(set) var entries: [Entry] = []      // oldest first
    public let max: Int
    public init(max: Int = FileFolders.maxLive) { self.max = max }

    public var paths: [String] { entries.map(\.path) }

    /// A new file: returns the oldest paths pushed out beyond `max` (to end as "dropped").
    public mutating func appear(_ path: String, used: String?) -> [String] {
        guard !entries.contains(where: { $0.path == path }) else { return [] }
        entries.append(Entry(path: path, used: used))
        guard entries.count > max else { return [] }
        let out = entries.prefix(entries.count - max).map(\.path)
        entries.removeFirst(entries.count - max)
        return out
    }

    /// The live files of `folder` that ended, given its current listing (names) and the current last-used dates.
    /// Only called with a listing that was READ: an unreadable folder is unknown, nothing ends.
    public mutating func ended(in folder: String, listing names: Set<String>,
                               used: (String) -> String?) -> [(String, Reason)] {
        var out: [(String, Reason)] = []
        entries.removeAll { e in
            guard (e.path as NSString).deletingLastPathComponent == folder else { return false }
            if !names.contains((e.path as NSString).lastPathComponent) { out.append((e.path, .removed)); return true }
            if used(e.path) != e.used { out.append((e.path, .opened)); return true }
            return false
        }
        return out
    }

    /// A folder no longer watched (settings changed): its files are forgotten without a gone.
    public mutating func forget(folder: String) {
        entries.removeAll { ($0.path as NSString).deletingLastPathComponent == folder }
    }
}

/// The read state of one watched folder, written to `<data dir>/status/oi-watcher-files.json` for oi live's
/// health (a watcher-side read failure is a FAILED poll there, never "nothing").
public enum FolderReadState: String, Sendable, Codable {
    case ok, unreadable, missing
}

/// `status/oi-watcher-files.json`: {"ts", "pid", "enabled", "folders": {path: ok|unreadable|missing}}, written at
/// every state change and every 30 s (oi live: older than 90 s = the watcher is not running).
public struct FilesStatus: Codable, Equatable, Sendable {
    public var ts: String
    public var pid: Int32
    public var enabled: Bool
    public var folders: [String: FolderReadState]

    public init(ts: String, pid: Int32, enabled: Bool, folders: [String: FolderReadState]) {
        self.ts = ts; self.pid = pid; self.enabled = enabled; self.folders = folders
    }

    public static var defaultURL: URL { SettingsStore.dataDir().appendingPathComponent("status/oi-watcher-files.json") }

    public func encoded() throws -> Data {
        let e = JSONEncoder()
        e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try e.encode(self) + Data("\n".utf8)
    }
}

extension AWEvent {
    /// {"kind": "file", "state": "live", "path", "class", "mtime" (epoch s), "size" (bytes)}: a new file.
    public static func fileLive(at t: Date, path: String, cls: String, mtime: Int, size: Int) -> AWEvent {
        AWEvent(timestamp: t, data: ["kind": .string("file"), "state": .string("live"), "path": .string(path),
                                     "class": .string(cls), "mtime": .int(mtime), "size": .int(size)])
    }

    /// {"kind": "file", "state": "gone", "path", "class", "reason": removed | opened | dropped}.
    public static func fileGone(at t: Date, path: String, cls: String, reason: LiveFiles.Reason) -> AWEvent {
        AWEvent(timestamp: t, data: ["kind": .string("file"), "state": .string("gone"), "path": .string(path),
                                     "class": .string(cls), "reason": .string(reason.rawValue)])
    }
}
