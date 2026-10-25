import Foundation
import HUDCore

var signalSources: [DispatchSourceSignal] = []

/// Sends one command line, waits for one reply line, prints it. Exit codes: 0 reply printed,
/// 2 daemon not running, 3 no reply before timeout.
func sendAndPrint(_ line: String, socket: String, timeout: Double) -> Int32 {
    guard let fd = connectUnix(socket) else {
        FileHandle.standardError.write(Data("oi-hud: no daemon on \(socket) (run `oi-hud serve`)\n".utf8))
        return 2
    }
    defer { close(fd) }
    var tv = timeval(tv_sec: Int(timeout), tv_usec: 0)
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    guard writeAll(fd, line + "\n") else { return 2 }
    var reply: String?
    readLines(fd) { data in
        if reply == nil { reply = String(decoding: data, as: UTF8.self) }
        shutdown(fd, SHUT_RDWR)   // one reply per command: stop reading
    }
    guard let reply else { return 3 }
    print(reply)
    return 0
}

/// Builds a show line from an options JSON array (validated locally first).
func showLine(optionsJSON: String, ttlMs: Int) -> String? {
    let line = "{\"cmd\":\"show\",\"ttl_ms\":\(ttlMs),\"options\":\(optionsJSON)}"
    do { _ = try decodeCommand(Data(line.utf8)) } catch {
        FileHandle.standardError.write(Data("oi-hud: invalid options: \(error)\n".utf8))
        return nil
    }
    return line
}

func runShow(optionsJSON: String, ttlMs: Int, socket: String) -> Int32 {
    guard let line = showLine(optionsJSON: optionsJSON, ttlMs: ttlMs) else { return 64 }
    return sendAndPrint(line, socket: socket, timeout: Double(ttlMs) / 1000 + 5)
}

/// Three example sets, each shown then reordered 2 s later (so the reorder animation is
/// visible), 4 s per set. Each show uses its own connection; replies print as they come.
func runDemo(socket: String) -> Int32 {
    let sets: [(String, String)] = [
        // mail just selected
        ("""
        [{"key":1,"label":"Reply","icon":"sf:arrowshape.turn.up.left","confidence":0.46},
         {"key":2,"label":"Send to Atlas","icon":"app:/Applications/Telegram.app","confidence":0.31},
         {"key":3,"label":"Archive","icon":"sf:archivebox","confidence":0.12}]
        """,
         """
        [{"key":1,"label":"Send to Atlas","icon":"app:/Applications/Telegram.app","confidence":0.52},
         {"key":2,"label":"Reply","icon":"sf:arrowshape.turn.up.left","confidence":0.33},
         {"key":3,"label":"Archive","icon":"sf:archivebox","confidence":0.10}]
        """),
        // app closed: what next
        ("""
        [{"key":1,"label":"Chrome","icon":"app:/Applications/Google Chrome.app","confidence":0.41},
         {"key":2,"label":"Mail","icon":"app:/System/Applications/Mail.app","confidence":0.27},
         {"key":3,"label":"Notes","icon":"app:/System/Applications/Notes.app","confidence":0.15},
         {"key":4,"label":"Calendar","icon":"app:/System/Applications/Calendar.app","confidence":0.09}]
        """,
         """
        [{"key":1,"label":"Mail","icon":"app:/System/Applications/Mail.app","confidence":0.44},
         {"key":2,"label":"Chrome","icon":"app:/Applications/Google Chrome.app","confidence":0.30},
         {"key":3,"label":"Calendar","icon":"app:/System/Applications/Calendar.app","confidence":0.14},
         {"key":4,"label":"Notes","icon":"app:/System/Applications/Notes.app","confidence":0.08}]
        """),
        // Space changed
        ("""
        [{"key":1,"label":"Safari","icon":"app:/Applications/Safari.app","confidence":0.38},
         {"key":2,"label":"New draft","icon":"sf:square.and.pencil","confidence":0.29},
         {"key":3,"label":"Copy link","icon":"sf:link","confidence":0.18}]
        """,
         """
        [{"key":1,"label":"New draft","icon":"sf:square.and.pencil","confidence":0.43},
         {"key":2,"label":"Copy link","icon":"sf:link","confidence":0.26},
         {"key":3,"label":"Safari","icon":"app:/Applications/Safari.app","confidence":0.20}]
        """),
    ]
    let group = DispatchGroup()
    func fire(_ json: String, ttl: Int, tag: String) {
        let compact = json.replacingOccurrences(of: "\n", with: "")
        guard let line = showLine(optionsJSON: compact, ttlMs: ttl) else { return }
        group.enter()
        Thread.detachNewThread {
            defer { group.leave() }
            guard let fd = connectUnix(socket) else { print("\(tag): no daemon on \(socket)"); return }
            defer { close(fd) }
            _ = writeAll(fd, line + "\n")
            readLines(fd) { data in
                print("\(tag): \(String(decoding: data, as: UTF8.self))")
                shutdown(fd, SHUT_RDWR)
            }
        }
    }
    guard connectUnix(socket).map({ close($0); return true }) ?? false else {
        FileHandle.standardError.write(Data("oi-hud: no daemon on \(socket) (run `oi-hud serve`)\n".utf8))
        return 2
    }
    for (i, s) in sets.enumerated() {
        fire(s.0, ttl: 3800, tag: "set\(i + 1)")
        Thread.sleep(forTimeInterval: 2.05)          // > the 2 s rate limit
        fire(s.1, ttl: 1700, tag: "set\(i + 1)-reordered")
        Thread.sleep(forTimeInterval: 2.05)
    }
    _ = group.wait(timeout: .now() + 3)
    return 0
}
