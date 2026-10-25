import AppKit
import ApplicationServices
import OIWatcherCore

let usage = """
usage: oi-watcher [--once | --dry-run [--shots-only] | --aw-selftest | --help]
  (no flag)      run: Space changes, ⌘ chords, Safari front tab, Safari page clicks (W16), screenshots (W17), new files of proactive.files.folders → ActivityWatch buckets
  --once         print the three capabilities' status (no prompts) and exit
  --dry-run      run, but print events to stdout instead of posting them
  --aw-selftest  create/post/read/delete a throwaway bucket on aw-server and exit
  --shots-only   W17: run only the screenshot source (tests: with --dry-run and $OI_SHOT_DIR)
"""

let args = Set(CommandLine.arguments.dropFirst())
let known: Set<String> = ["--once", "--dry-run", "--aw-selftest", "--help", "-h", "--shots-only"]   // W17: --shots-only
if !args.isSubset(of: known) || args.contains("--help") || args.contains("-h") {
    print(usage)
    exit(args.isSubset(of: known) ? 0 : 2)
}

let hostname = localHostname()
let sink = AWSink(hostname: hostname, dryRun: args.contains("--dry-run"))

/// Wait for async work from top-level code. The body must not hop to the main actor
/// (the main thread is blocked here); AWSink is its own actor, so that is fine.
func runBlocking(_ body: @escaping @Sendable () async -> Void) {
    let done = DispatchSemaphore(value: 0)
    Task.detached { await body(); done.signal() }
    done.wait()
}

if args.contains("--once") {
    nonisolated(unsafe) var aw: (Int, Data)?
    runBlocking { aw = await sink.get("info") }
    print("oi-watcher --once  host=\(hostname)")
    if let cgs = CGSPrivate.load() {
        if let cur = cgs.current() {
            print("space : private CGS API available — active space id \(cur.id), index \(cur.index.map(String.init) ?? "null") (amber)")
        } else {
            print("space : private CGS API loaded but returned no active space — space=null, delta=0")
        }
    } else {
        print("space : private CGS API unavailable — space=null, delta=0 (notification still works)")
    }
    let k = KeySource.probe()
    print("key   : Input Monitoring \(k.preflight ? "granted" : "NOT granted"); tap \(k.created ? "created" : "NOT created")\(k.created ? "" : " — enable oi-watcher in Privacy & Security → Input Monitoring")")
    print("ax    : Accessibility \(AXIsProcessTrusted() ? "granted" : "NOT granted") (optional: cheap Safari window-title check)")
    print("safari: Automation → Safari: \(SafariSource.probeAutomation())")
    print("shots : folder \(ScreenshotSource.currentFolder() == NSHomeDirectory() + "/Desktop" ? "~/Desktop" : "custom")\(ScreenshotSource.thumbnailDelaysFiles() ? " (floating thumbnail ON: files land ~5 s late; clipboard shots are instant)" : ""); clipboard poll 1 s")   // W17
    print("files : \(FolderSource.configured().map { "\($0.count) folder(s) (proactive.files.folders)" } ?? "source off (proactive.sources.files)")")
    print("aw    : \(aw.map { $0.0 == 200 ? "aw-server reachable at 127.0.0.1:5600" : "aw-server HTTP \($0.0)" } ?? "aw-server UNREACHABLE at 127.0.0.1:5600")")
    for kind in BucketKind.allCases { print("bucket: \(kind.bucketID(hostname: hostname))") }
    exit(0)
}

if args.contains("--aw-selftest") {
    runBlocking { print(await sink.selfTest()) }
    exit(0)
}

// Daemon mode.
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)   // no Dock icon, no menu bar
MainActor.assumeIsolated {
    Log.info("starting (host=\(hostname), dry-run=\(args.contains("--dry-run")))")
    let space = SpaceSource(sink: sink)
    let key = KeySource(sink: sink)
    let safari = SafariSource(sink: sink)
    let click = ClickSource(sink: sink)     // W16: page button / link clicks in Safari
    let gate = PauseGate(sink: sink)
    gate.start()            // settings.json `paused`, re-read every 5 s
    let shots = ScreenshotSource(sink: sink)   // W17: screenshots (folder + clipboard)
    shots.start()
    if args.contains("--shots-only") {
        withExtendedLifetime((shots, gate)) { app.run() }
    }
    space.start()
    key.start()
    safari.start()
    click.start()
    let files = FolderSource(sink: sink)    // oi live's « files » connector (TCC: the watcher reads ~/Desktop, oi live cannot)
    files.start()
    let status = StatusReporter(key: key, spaceAPI: CGSPrivate.load() != nil)
    status.start()          // status/oi-watcher.json for oi-menubar, every 30 s
    // Retry queued events (aw-server restarts) every 10 s.
    Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { _ in
        Task { await sink.flush() }
    }
    // Keep the sources alive for the process lifetime.
    withExtendedLifetime((space, key, safari, gate, status, click, shots, files)) { app.run() }
}
