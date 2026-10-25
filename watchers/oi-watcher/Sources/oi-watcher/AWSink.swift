import Foundation
import OIWatcherCore

/// Posts events to the local aw-server REST API (127.0.0.1 only, hard-coded).
/// Creates buckets lazily; keeps a small bounded retry queue if aw-server is down.
actor AWSink {
    static let base = URL(string: "http://127.0.0.1:5600/api/0")!
    static let maxPending = 1000

    let hostname: String
    let dryRun: Bool
    private var created: Set<BucketKind> = []
    private var pending: [(BucketKind, AWEvent)] = []
    private var downLogged = false
    /// settings.json `paused`: when true, `post` drops every event (nothing reaches aw-server).
    private(set) var paused = false
    private let session: URLSession

    init(hostname: String, dryRun: Bool) {
        self.hostname = hostname
        self.dryRun = dryRun
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 5
        cfg.connectionProxyDictionary = [:]   // never route via a proxy
        self.session = URLSession(configuration: cfg)
    }

    func setPaused(_ p: Bool) { paused = p }

    func post(_ kind: BucketKind, _ event: AWEvent) async {
        if paused { return }
        if dryRun {
            let body = (try? AWEvent.encodeBatch([event])).map { String(decoding: $0, as: UTF8.self) } ?? "?"
            print("\(kind.bucketID(hostname: hostname)) \(body)")
            fflush(stdout)
            return
        }
        pending.append((kind, event))
        if pending.count > Self.maxPending { pending.removeFirst(pending.count - Self.maxPending) }
        await flush()
    }

    /// Send everything queued, in order; stop at the first failure (retry later).
    func flush() async {
        while let (kind, event) = pending.first {
            guard await ensureBucket(kind), await send(kind, [event]) else {
                if !downLogged {
                    downLogged = true
                    Log.info("aw-server at 127.0.0.1:5600 unreachable or refusing; queueing (max \(Self.maxPending)) and retrying")
                }
                return
            }
            pending.removeFirst()
            if downLogged { downLogged = false; Log.info("aw-server reachable again; queue flushed") }
        }
    }

    private func ensureBucket(_ kind: BucketKind) async -> Bool {
        if created.contains(kind) { return true }
        let url = Self.base.appendingPathComponent("buckets").appendingPathComponent(kind.bucketID(hostname: hostname))
        guard let body = try? JSONSerialization.data(withJSONObject: Buckets.createBody(kind: kind, hostname: hostname), options: [.sortedKeys]) else { return false }
        guard let code = await request(url, method: "POST", body: body) else { return false }
        // 200 created; 304 (python aw-server) / 409 (rust) = already exists.
        if code == 200 || code == 304 || code == 409 { created.insert(kind); return true }
        Log.info("bucket create \(kind.bucketID(hostname: hostname)) -> HTTP \(code)")
        return false
    }

    private func send(_ kind: BucketKind, _ events: [AWEvent]) async -> Bool {
        let url = Self.base.appendingPathComponent("buckets").appendingPathComponent(kind.bucketID(hostname: hostname)).appendingPathComponent("events")
        guard let body = try? AWEvent.encodeBatch(events) else { return true }  // unencodable: drop
        guard let code = await request(url, method: "POST", body: body) else { return false }
        if code == 404 { created.remove(kind) }   // bucket deleted under us: recreate next time
        return (200..<300).contains(code)
    }

    /// Returns the HTTP status, or nil on a transport error.
    func request(_ url: URL, method: String, body: Data?) async -> Int? {
        var req = URLRequest(url: url)
        req.httpMethod = method
        if let body { req.httpBody = body; req.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        do {
            let (_, resp) = try await session.data(for: req)
            return (resp as? HTTPURLResponse)?.statusCode
        } catch {
            return nil
        }
    }

    /// GET a path under /api/0 and return (status, body). Used by --once and --aw-selftest.
    func get(_ path: String) async -> (Int, Data)? {
        var req = URLRequest(url: Self.base.appendingPathComponent(path))
        req.httpMethod = "GET"
        do {
            let (data, resp) = try await session.data(for: req)
            return ((resp as? HTTPURLResponse)?.statusCode ?? 0, data)
        } catch { return nil }
    }

    /// Round-trip on a throwaway bucket: create, post one event, read it back, delete.
    /// Touches none of the three real buckets.
    func selfTest() async -> String {
        let id = "oi-watcher-selftest_\(hostname)"
        let bucket = Self.base.appendingPathComponent("buckets").appendingPathComponent(id)
        let createBody = try! JSONSerialization.data(withJSONObject: ["client": Buckets.client, "type": "selftest", "hostname": hostname])
        let c = await request(bucket, method: "POST", body: createBody)
        let ev = AWEvent(timestamp: Date(), data: ["selftest": .string("ok")])
        let p = await request(bucket.appendingPathComponent("events"), method: "POST", body: try! AWEvent.encodeBatch([ev]))
        let r = await get("buckets/\(id)/events")
        let readBack = r.map { String(decoding: $0.1, as: UTF8.self).contains("\"selftest\"") } ?? false
        var del = URLComponents(url: bucket, resolvingAgainstBaseURL: false)!
        del.queryItems = [URLQueryItem(name: "force", value: "1")]   // aw-server requires force=1 outside testing mode
        let d = await request(del.url!, method: "DELETE", body: nil)
        return "aw selftest: create=\(c.map(String.init) ?? "err") post=\(p.map(String.init) ?? "err") readback=\(readBack ? "ok" : "FAIL") delete=\(d.map(String.init) ?? "err")"
    }
}
