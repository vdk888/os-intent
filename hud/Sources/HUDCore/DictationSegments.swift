import Foundation

// W20// parler"): the on-device recogniser delivers `isFinal` after a short pause (and errors such as "no speech" or its
// own ~1 min task limit) while the user is still talking. Before W20 that ended the dictation and the vocal flow
// SENT on it (live log: started 18:13:19Z, "vocal: sending" 18:13:30Z, 46 chars). Now a recogniser end is only
// the end of a SEGMENT: the text so far is kept and a new recognition task starts on the same audio engine. The
// recording ends only on the user's stop (vocal key / Enter / send), Esc, or the time limit.
// W21 (W20 review note): after `maxEmptyFailures` empty failures in a row the recogniser is no longer given up
// (speech after a long silence was recorded but never transcribed): it restarts after a backoff, 250 ms, 500 ms,
// 1 s, then 2 s each time, until the user stops or the time limit; any speech resets it to immediate restarts.

/// What the dictation does when the recogniser reports (pure, unit-tested; oi-hud's Dictation applies it).
public enum DictationStep: Equatable, Sendable {
    case keepGoing      // a partial result: show it, the task goes on
    /// The task ended (isFinal or error): keep its text, start a new task on the same engine after `afterMs`
    /// (0 = at once; W21: a backoff after `maxEmptyFailures` empty failures in a row, never a give-up).
    case restart(afterMs: Int)
    public static let restart = DictationStep.restart(afterMs: 0)
}

/// The running transcript of one dictation made of recogniser segments, joined with one space.
public struct DictationSegments: Sendable {
    /// Consecutive segments ending in an error with no text restarted at once; from then on each restart waits
    /// `backoffMs` (no tight loop when on-device recognition is broken, no give-up either: W21).
    public static let maxEmptyFailures = 5
    public static let backoffStartMs = 250
    public static let backoffCapMs = 2000
    /// The wait before the restart that follows the `failures`-th empty failure in a row: 0 below
    /// `maxEmptyFailures`, then 250, 500, 1000, 2000, 2000… ms.
    public static func backoffMs(failures: Int) -> Int {
        guard failures >= maxEmptyFailures else { return 0 }
        let n = min(failures - maxEmptyFailures, 4)   // 250 << 4 > the cap: no overflow, ever
        return min(backoffStartMs << n, backoffCapMs)
    }

    public private(set) var committed: [String] = []
    public private(set) var current = ""
    public private(set) var emptyFailures = 0

    public init() {}

    /// The full transcript: the finished segments then the current one, trimmed, one space between.
    public var text: String {
        (committed + [current])
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: " ")
    }

    /// A recogniser callback for the current segment: `partial` = its best transcription so far (nil = none),
    /// `final` = isFinal, `failed` = an error came with it. Never ends the dictation: that is the user's call.
    public mutating func handle(partial: String?, final: Bool, failed: Bool) -> DictationStep {
        if let partial, !partial.isEmpty { current = partial }
        guard final || failed else { return .keepGoing }
        let hadText = !current.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        commitCurrent()
        if failed && !hadText {
            emptyFailures += 1
            return .restart(afterMs: DictationSegments.backoffMs(failures: emptyFailures))
        }
        emptyFailures = 0
        return .restart
    }

    private mutating func commitCurrent() {
        let t = current.trimmingCharacters(in: .whitespacesAndNewlines)
        if !t.isEmpty { committed.append(t) }
        current = ""
    }
}
