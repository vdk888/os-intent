import AppKit
import ApplicationServices
import OISettings

/// Writes `<data_dir>/status/oi-watcher.json` (OISettings.WatcherStatus) at start and every
/// 30 s, so oi-menubar shows the WATCHER's real grants, not its own ().
/// Only capability booleans; never event contents.
@MainActor
final class StatusReporter {
    static let interval: TimeInterval = 30
    private let key: KeySource
    private let spaceAPI: Bool
    private var lastError: String?

    init(key: KeySource, spaceAPI: Bool) { self.key = key; self.spaceAPI = spaceAPI }

    func start() {
        write()
        Timer.scheduledTimer(withTimeInterval: Self.interval, repeats: true) { _ in
            MainActor.assumeIsolated { self.write() }
        }
    }

    private func write() {
        let tap = key.tapActive
        let ax = AXIsProcessTrusted()
        let space = spaceAPI
        // The Apple Events probe can block while Safari is busy: keep it off the main thread.
        Task.detached {
            let status = WatcherStatus(ts: SettingsStore.isoTimestamp(Date()), keyTap: tap, ax: ax,
                                       safariAutomation: WatcherStatus.automation(fromOSStatus: SafariSource.automationStatus()),
                                       spaceAPI: space, pid: getpid())
            let err: String?
            do { try status.write(); err = nil } catch { err = "\(error)" }
            await MainActor.run { self.report(err) }
        }
    }

    private func report(_ err: String?) {
        guard err != lastError else { return }   // one log line per change, not every 30 s
        lastError = err
        Log.info(err.map { "status: cannot write \(WatcherStatus.defaultURL.path): \($0)" }
                 ?? "status: writing \(WatcherStatus.defaultURL.path) every \(Int(Self.interval)) s")
    }
}
