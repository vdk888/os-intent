import Foundation
import HUDCore
import OISettings

func log(_ msg: String) {
    let ts = ISO8601DateFormatter().string(from: Date())
    FileHandle.standardError.write(Data("[oi-hud \(ts)] \(msg)\n".utf8))
}

/// `<data dir>/hud.sock`: `$OI_DATA_DIR` or ~/.local/share/os-intent (same rule as oi-menubar).
let defaultSocketPath: String = SettingsStore.dataDir().appendingPathComponent("hud.sock").path

/// sockaddr_un holds 104 bytes: a deep $OI_DATA_DIR gives a path that does not fit.
func socketPathFits(_ path: String) -> Bool {
    path.utf8CString.count <= MemoryLayout.size(ofValue: sockaddr_un().sun_path)
}

/// Calls `body` with a sockaddr_un for `path`.
func withUnixAddress<T>(_ path: String, _ body: (UnsafePointer<sockaddr>, socklen_t) -> T) -> T {
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8CString)
    precondition(bytes.count <= MemoryLayout.size(ofValue: addr.sun_path), "socket path too long")
    withUnsafeMutableBytes(of: &addr.sun_path) { dst in bytes.withUnsafeBytes { dst.copyMemory(from: $0) } }
    addr.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    return withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { body($0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
    }
}

/// Connect to the daemon socket; nil if nobody listens.
func connectUnix(_ path: String) -> Int32? {
    guard socketPathFits(path) else { return nil }
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { return nil }
    let rc = withUnixAddress(path) { connect(fd, $0, $1) }
    if rc != 0 { close(fd); return nil }
    return fd
}

func writeAll(_ fd: Int32, _ s: String) -> Bool {
    var data = Array(s.utf8)[...]
    while !data.isEmpty {
        let n = data.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        if n <= 0 { return false }
        data = data.dropFirst(n)
    }
    return true
}

/// Reads newline-delimited lines from `fd` until EOF/error.
func readLines(_ fd: Int32, _ onLine: (Data) -> Void) {
    var buf = Data()
    var chunk = [UInt8](repeating: 0, count: 4096)
    while true {
        let n = read(fd, &chunk, chunk.count)
        if n <= 0 { return }
        buf.append(contentsOf: chunk[0..<n])
        while let nl = buf.firstIndex(of: 0x0A) {
            let line = buf[buf.startIndex..<nl]
            buf = Data(buf[buf.index(after: nl)...])
            if !line.isEmpty { onLine(Data(line)) }
        }
    }
}

/// One client connection. Events for a `show` go back on the connection that sent it.
final class Connection {
    let fd: Int32
    private let lock = NSLock()
    private var closed = false

    init(fd: Int32) { self.fd = fd }

    /// W18: the wish resolver connection is dropped once its peer closed.
    var isClosed: Bool { lock.lock(); defer { lock.unlock() }; return closed }

    func send(_ event: HUDEvent) {
        lock.lock(); defer { lock.unlock() }
        guard !closed else { return }
        _ = writeAll(fd, event.jsonLine())
    }

    /// W16: a pre-encoded line (`page` / `ax_press` replies).
    func sendLine(_ line: String) {
        lock.lock(); defer { lock.unlock() }
        guard !closed else { return }
        _ = writeAll(fd, line)
    }

    func close() {
        lock.lock(); defer { lock.unlock() }
        guard !closed else { return }
        closed = true
        Darwin.close(fd)
    }
}

/// Unix-socket server: accept + per-connection reader threads; commands are handed to the
/// main thread (where all AppKit work happens).
final class SocketServer {
    let path: String
    let onCommand: (Result<HUDCommand, CommandError>, Connection) -> Void
    /// W16: `page` / `ax_press` lines go here (main thread), never to the HUDCommand decoder.
    var onPageCommand: ((Result<PageCommand, PressOutcome>, Connection) -> Void)?
    private var listenFD: Int32 = -1
    private let connLock = NSLock()
    private var conns: [ObjectIdentifier: Connection] = [:]

    init(path: String, onCommand: @escaping (Result<HUDCommand, CommandError>, Connection) -> Void) {
        self.path = path; self.onCommand = onCommand
    }

    func start() throws {
        signal(SIGPIPE, SIG_IGN)
        guard socketPathFits(path) else {
            throw ServeError("socket path longer than 103 bytes: \(path) (use --socket with a shorter path)")
        }
        let dir = (path as NSString).deletingLastPathComponent
        if !dir.isEmpty {   // a bare relative name (`--socket hud.sock`) lives in the cwd
            try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
        }
        if FileManager.default.fileExists(atPath: path) {
            if let fd = connectUnix(path) { close(fd); throw ServeError("another oi-hud daemon is listening on \(path)") }
            unlink(path)  // stale socket from a previous run
        }
        listenFD = socket(AF_UNIX, SOCK_STREAM, 0)
        guard listenFD >= 0 else { throw ServeError("socket() failed") }
        guard withUnixAddress(path, { bind(listenFD, $0, $1) }) == 0 else { throw ServeError("bind \(path) failed: errno \(errno)") }
        chmod(path, 0o600)
        guard listen(listenFD, 8) == 0 else { throw ServeError("listen failed") }
        let lfd = listenFD
        Thread.detachNewThread { [weak self] in
            while true {
                let cfd = accept(lfd, nil, nil)
                if cfd < 0 { if errno == EINTR { continue }; return }
                guard let self else { close(cfd); return }
                let c = Connection(fd: cfd)
                self.connLock.lock(); self.conns[ObjectIdentifier(c)] = c; self.connLock.unlock()
                self.serve(c)
            }
        }
        log("listening on \(path)")
    }

    private func serve(_ conn: Connection) {
        Thread.detachNewThread { [weak self, onCommand] in
            readLines(conn.fd) { line in
                if let pc = decodePageCommand(line), let h = self?.onPageCommand {   // W16
                    DispatchQueue.main.async { h(pc, conn) }
                    return
                }
                let result: Result<HUDCommand, CommandError>
                do { result = .success(try decodeCommand(line)) }
                catch let e as CommandError { result = .failure(e) }
                catch { result = .failure(.badJSON) }
                DispatchQueue.main.async { onCommand(result, conn) }
            }
            // Peer closed its side: replies for a still-visible show are dropped silently.
            DispatchQueue.main.async { [weak self] in
                conn.close()
                guard let self else { return }
                self.connLock.lock(); self.conns[ObjectIdentifier(conn)] = nil; self.connLock.unlock()
            }
        }
    }

    /// Sends `event` to every open connection (the `wish` event: the menu bar counts them).
    func broadcast(_ event: HUDEvent) {
        connLock.lock(); let all = Array(conns.values); connLock.unlock()
        for c in all { c.send(event) }
    }

    func stop() { if listenFD >= 0 { close(listenFD) }; unlink(path) }
}

struct ServeError: Error, CustomStringConvertible {
    let description: String
    init(_ d: String) { description = d }
}
