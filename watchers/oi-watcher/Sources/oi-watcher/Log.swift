import Foundation
import OIWatcherCore

/// One-line logs to stderr (launchd sends stderr to the log file). Never logs
/// event contents: no URLs, titles or chords end up in the log file.
enum Log {
    static func info(_ msg: String) {
        let line = "\(AWEvent.isoTimestamp(Date())) oi-watcher: \(msg)\n"
        FileHandle.standardError.write(Data(line.utf8))
    }
}

/// Logs a hint at most once per key for the process lifetime.
@MainActor
enum Hints {
    private static var shown: Set<String> = []
    static func once(_ key: String, _ msg: String) {
        guard !shown.contains(key) else { return }
        shown.insert(key)
        Log.info(msg)
    }
}

func localHostname() -> String {
    // Same call as python's socket.gethostname(), which aw-watcher-* use for bucket ids.
    var buf = [CChar](repeating: 0, count: 256)
    guard gethostname(&buf, buf.count) == 0 else { return "unknown-host" }
    let bytes = buf.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }
    return String(decoding: bytes, as: UTF8.self)
}
