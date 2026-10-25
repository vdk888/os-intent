import Foundation
import OISettings

/// Re-reads settings.json every 5 s (mtime check, see OISettings.SettingsCache) and forwards
/// `paused` to the sink. Observers stay alive while paused; only posting stops.
/// Logs one line per transition (never the file contents beyond the flag).
@MainActor
final class PauseGate {
    static let interval: TimeInterval = 5
    private let sink: AWSink
    private let cache = SettingsCache()
    private var paused: Bool?

    init(sink: AWSink) { self.sink = sink }

    func start() {
        poll()
        Timer.scheduledTimer(withTimeInterval: Self.interval, repeats: true) { _ in
            MainActor.assumeIsolated { self.poll() }
        }
    }

    func poll() {
        let (s, _) = cache.poll()
        guard s.paused != paused else { return }
        let first = paused == nil
        paused = s.paused
        if s.paused || !first {
            Log.info(s.paused ? "logging PAUSED (settings.json paused=true): posting nothing" : "logging RESUMED (settings.json paused=false)")
        }
        let p = s.paused
        let sink = self.sink
        Task { await sink.setPaused(p) }
    }
}
