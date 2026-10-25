import AppKit
import Foundation
import HUDCore
import OISettings

let usage = """
usage:
  oi-hud serve [--socket PATH] [--min-interval-ms 2000] [--debug-open-wish [--debug-delay-ms 500]]
  oi-hud show --options '<json array>' [--ttl-ms <settings gate.ttl_ms, default 15000>] [--socket PATH]
  oi-hud hide [--socket PATH]
  oi-hud state [--socket PATH]          what is on screen + wish/summon/taps state (debug)
  oi-hud debug-summon [--socket PATH]   run the summon path as the ⌥⌘ tap would (no key needed)
  oi-hud debug-vocal [--pick SLUG --text "…"] [--socket PATH]
                                        the vocal key (⌃⌥) without a key: open the agent picker / next page;
                                        with --pick + --text, pick that agent and send --text as the transcript
                                        (no microphone; serve with OI_VOCAL_DRY_RUN=1 to send nothing)
  oi-hud debug-press --key K [--claude] [--socket PATH]
                                        W14: press option K as its key would (--claude = the long-press variant)
  oi-hud debug-glide --path 2,1 [--cancel] [--socket PATH]
                                        W15: a ⌥⌘ glide without keys: hold, rest on 2 until its sub-ring opens,
                                        then on 2.1, release (= execute it; --cancel: release on empty space)
  oi-hud page [--socket PATH]           W16: the daemon's snapshot of Safari's front page (buttons/links, frames)
  oi-hud page-local                     W16: the same read in this process (uses ITS Accessibility grant)
  oi-hud ax-press --role button|link --label "…" [--host H] [--dry-run] [--socket PATH]
                                        W16: re-find + AXPress through the daemon (needs a fresh tap on that option)
  oi-hud demo [--socket PATH]
  oi-hud speech-check                   dictation availability + permission status (never prompts)
  oi-hud --debug-wish "<text>"          write one wish row without the panel (tests), then exit
"""

var args = Array(CommandLine.arguments.dropFirst())
func flag(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    let v = args[i + 1]; args.removeSubrange(i...i + 1); return v
}
func boolFlag(_ name: String) -> Bool {
    guard let i = args.firstIndex(of: name) else { return false }
    args.remove(at: i); return true
}
let socketPath = flag("--socket") ?? defaultSocketPath

if let text = flag("--debug-wish") {
    // Same row as the panel would write (source "typed", HUD not visible); prints no text.
    let now = Date()
    let ctx = WishContext(app: NSWorkspace.shared.frontmostApplication?.localizedName, space: SpaceIndex.current(),
                          hudVisible: false, hudOptions: [])
    guard let row = WishRow(date: now, rawText: text, source: .typed, context: ctx) else {
        FileHandle.standardError.write(Data("oi-hud: empty wish, nothing written\n".utf8)); exit(64)
    }
    do {
        let url = try WishStore.append(row, date: now)
        print("{\"event\":\"wish\",\"text_len\":\(row.text.count),\"file\":\"\(url.path)\"}")
        exit(0)
    } catch {
        FileHandle.standardError.write(Data("oi-hud: \(error)\n".utf8)); exit(1)
    }
}

switch args.first {
case "serve":
    let minMs = Int(flag("--min-interval-ms") ?? "") ?? 2000
    let openWish = boolFlag("--debug-open-wish")
    let openDelayMs = Int(flag("--debug-delay-ms") ?? "") ?? 500
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)          // no Dock icon, never activates
    let controller = HUDController(minInterval: Double(minMs) / 1000)
    let server = SocketServer(path: socketPath) { result, conn in controller.handle(result, from: conn) }
    controller.onWishSaved = { n in server.broadcast(.wish(textLen: n)) }
    controller.onSummon = { ts in server.broadcast(.summon(ts: ts)) }
    server.onPageCommand = { cmd, conn in controller.handlePage(cmd, from: conn) }   // W16
    let hudStatus = HUDStatusWriter(); hudStatus.start()                            // W16: status/oi-hud.json
    do { try server.start() } catch { log("\(error)"); exit(1) }
    for sig in [SIGINT, SIGTERM] {
        signal(sig, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        src.setEventHandler { server.stop(); exit(0) }
        src.resume()
        signalSources.append(src)
    }
    if openWish {   // demo/tests: open the wish panel once, as the hotkey would, without touching the keyboard
        DispatchQueue.main.asyncAfter(deadline: .now() + Double(openDelayMs) / 1000) { controller.debugOpenWish() }
    }
    app.run()

case "show":
    guard let opts = flag("--options") else { print(usage); exit(64) }
    let ttl = Int(flag("--ttl-ms") ?? "") ?? SettingsStore.load().gateTTLms
    exit(runShow(optionsJSON: opts, ttlMs: ttl, socket: socketPath))

case "hide":
    exit(sendAndPrint("{\"cmd\":\"hide\"}", socket: socketPath, timeout: 5))

case "state":
    exit(sendAndPrint("{\"cmd\":\"state\"}", socket: socketPath, timeout: 5))

case "debug-summon":
    exit(sendAndPrint("{\"cmd\":\"debug_summon\"}", socket: socketPath, timeout: 5))

case "debug-vocal":
    var cmd: [String: String] = ["cmd": "debug_vocal"]
    if let p = flag("--pick") { cmd["pick"] = p; cmd["text"] = flag("--text") ?? "" }
    let line = String(decoding: try! JSONSerialization.data(withJSONObject: cmd, options: [.sortedKeys]), as: UTF8.self)
    exit(sendAndPrint(line, socket: socketPath, timeout: 5))

case "debug-press":
    guard let k = Int(flag("--key") ?? "") else { print(usage); exit(64) }
    let mode = boolFlag("--claude") ? ",\"mode\":\"claude\"" : ""
    exit(sendAndPrint("{\"cmd\":\"debug_press\",\"key\":\(k)\(mode)}", socket: socketPath, timeout: 5))

case "debug-glide":
    let path = (flag("--path") ?? "").split(separator: ",").compactMap { Int($0.trimmingCharacters(in: .whitespaces)) }
    guard !path.isEmpty else { print(usage); exit(64) }
    let cancel = boolFlag("--cancel") ? ",\"cancel\":true" : ""
    exit(sendAndPrint("{\"cmd\":\"debug_glide\",\"path\":[\(path.map(String.init).joined(separator: ","))]\(cancel)}",
                      socket: socketPath, timeout: 5))
case "page":            // W16: the page snapshot as the daemon sees it (labels + frames, never page text)
    exit(sendAndPrint("{\"cmd\":\"page\"}", socket: socketPath, timeout: 5))

case "page-local":      // W16: the same read in THIS process (its own Accessibility grant), no daemon
    print(PageAX.read().snap.jsonLine(), terminator: "")
    exit(0)

case "ax-press":        // W16: through the daemon; without --dry-run it needs a fresh tap ticket
    guard let role = flag("--role"), let label = flag("--label") else { print(usage); exit(64) }
    var cmd: [String: Any] = ["cmd": "ax_press", "role": role, "slug": PageLabel.slug(label),
                              "dry_run": boolFlag("--dry-run")]
    if let h = flag("--host") { cmd["host"] = h }
    let line = String(decoding: try! JSONSerialization.data(withJSONObject: cmd, options: [.sortedKeys]), as: UTF8.self)
    exit(sendAndPrint(line, socket: socketPath, timeout: 5))

case "demo":
    exit(runDemo(socket: socketPath))

case "speech-check":
    for line in Dictation.report(preferred: SettingsStore.load().wish.locale) { print(line) }
    exit(0)

default:
    print(usage); exit(64)
}
