import AppKit
import MenubarCore
import OISettings

let usage = """
usage: oi-menubar [--print-menu | --settings | --help]
  (no flag)     run the menu bar app (status item, no Dock icon)
  --print-menu  probe once, print the menu as text and exit (no UI)
  --settings    open only the Settings window (no status item; quits when it closes; for a preview
                against a test data dir: OI_DATA_DIR=/tmp/x oi-menubar --settings;
                OI_SETTINGS_SECTION=claude opens that section)
"""

let args = Set(CommandLine.arguments.dropFirst())
if args.contains("--help") || args.contains("-h") || !args.isSubset(of: ["--print-menu", "--settings", "--help", "-h"]) {
    print(usage)
    exit(args.isSubset(of: ["--print-menu", "--settings", "--help", "-h"]) ? 0 : 2)
}

if args.contains("--print-menu") {
    let s = Probe.collect(SettingsStore.load(), rebuilding: false, lastRebuild: nil)
    print(MenuModel.render(MenuModel.entries(s)))
    exit(0)
}

// W26: the Settings window alone (no status item, so it may run next to the LaunchAgent copy).
if args.contains("--settings") {
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    MainActor.assumeIsolated {
        let w = SettingsWindowController(openHotkeys: { log("settings preview: Hotkeys… opens from the menu bar app") })
        let obs = NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: w.nsWindow, queue: .main) { _ in
            DispatchQueue.main.async { NSApp.terminate(nil) }
        }
        if let sec = ProcessInfo.processInfo.environment["OI_SETTINGS_SECTION"].flatMap(SettingsForm.Section.init(rawValue:)) {
            w.model.section = sec
        }
        w.present()
        log("settings preview: window \(w.windowNumber)")
        withExtendedLifetime((w, obs)) { app.run() }
    }
    exit(0)
}

// One status item only (e.g. the LaunchAgent copy plus one opened from Finder).
if let id = Bundle.main.bundleIdentifier,
   let other = NSRunningApplication.runningApplications(withBundleIdentifier: id)
       .first(where: { $0.processIdentifier != getpid() }) {
    log("another oi-menubar is already running (pid \(other.processIdentifier)); exiting")
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)     // menu bar only, no Dock icon
MainActor.assumeIsolated {
    let controller = AppController()
    app.delegate = controller
    withExtendedLifetime(controller) { app.run() }
}
