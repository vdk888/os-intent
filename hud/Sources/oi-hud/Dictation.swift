import AVFoundation
import Foundation
import HUDCore
import Speech

/// On-device dictation for the wish panel: Speech framework with `requiresOnDeviceRecognition`
/// (audio never leaves the Mac), microphone via AVAudioEngine. Locale from settings `wish.locale`
/// (default fr-FR), falling back to en-US when that locale has no on-device model.
/// Permissions: "Speech Recognition" and "Microphone", asked once each, the first time the mic
/// is used. Denied or unavailable → `onEnd(hint)` and the panel stays a typing field.
/// W12: the vocal recording reuses it with settings `vocal.max_s` (default 300 s) and `recordTo` (the microphone buffers
/// are also written to that .caf file, 0600; the audio never leaves the Mac).
/// W20: a recogniser isFinal / error mid-speech is the end of a SEGMENT, never of the dictation: the text so
/// far is kept and a new recognition task starts on the same audio engine (HUDCore.DictationSegments). The
/// session ends only on `stop()` (the user's stop / Esc) or the time limit (`onEnd`). W21: a recogniser that keeps
/// failing with no text restarts after a backoff (250 ms … 2 s), never gives up: speech after a long silence is
/// still transcribed.
/// Main thread only.
final class Dictation {
    var onPartial: ((String) -> Void)?
    /// Called once per session when it stops by itself (time limit, unavailable / denied; W20: never on a
    /// recogniser final result or error);
    /// the hint (if any) is a one-line reason for the field's placeholder.
    var onEnd: ((String?) -> Void)?

    private var engine: AVAudioEngine?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var recognizer: SFSpeechRecognizer?
    private var segments = DictationSegments()        // W20
    private var segment = 0                           // W20: callbacks of an older task are ignored
    private let sink = RequestSink()                  // W20: the tap feeds the current request
    private var limit: Timer?
    private var audioFile: AVAudioFile?
    private var session = 0
    private(set) var running = false

    /// TCC attributes a permission prompt to the *responsible* process. Under launchd (the
    /// LaunchAgent) that is oi-hud itself, whose embedded Info.plist carries the usage strings.
    /// Started from a terminal it is the terminal app, which may lack them: macOS then kills the
    /// process instead of prompting. So dictation runs only under launchd unless
    /// OI_HUD_ALLOW_DICTATION=1 (for a terminal that has the usage strings / grants).
    static var allowedHere: Bool {
        getppid() == 1 || ProcessInfo.processInfo.environment["OI_HUD_ALLOW_DICTATION"] == "1"
    }

    /// The recogniser to use: `preferred` if it supports on-device recognition, else en-US.
    static func recognizer(preferred: String) -> SFSpeechRecognizer? {
        for id in [preferred, "en-US"] {
            if let r = SFSpeechRecognizer(locale: Locale(identifier: id)), r.supportsOnDeviceRecognition { return r }
        }
        return nil
    }

    /// For `oi-hud speech-check`: status lines, never prompts.
    static func report(preferred: String) -> [String] {
        func speech(_ s: SFSpeechRecognizerAuthorizationStatus) -> String {
            switch s {
            case .authorized: return "authorized"
            case .denied: return "denied"
            case .restricted: return "restricted"
            case .notDetermined: return "not yet asked"
            @unknown default: return "unknown"
            }
        }
        func mic(_ s: AVAuthorizationStatus) -> String {
            switch s {
            case .authorized: return "authorized"
            case .denied: return "denied"
            case .restricted: return "restricted"
            case .notDetermined: return "not yet asked"
            @unknown default: return "unknown"
            }
        }
        var out = ["speech recognition permission: \(speech(SFSpeechRecognizer.authorizationStatus()))",
                   "microphone permission: \(mic(AVCaptureDevice.authorizationStatus(for: .audio)))"]
        for id in Array(Set([preferred, "fr-FR", "en-US"])).sorted() {
            let r = SFSpeechRecognizer(locale: Locale(identifier: id))
            let line = r.map { "on-device \($0.supportsOnDeviceRecognition ? "yes" : "no"), available \($0.isAvailable ? "yes" : "no")" } ?? "locale not supported"
            out.append("\(id): \(line)")
        }
        let chosen = recognizer(preferred: preferred)?.locale.identifier ?? "none (typing only)"
        out.append("dictation would use: \(chosen)")
        out.append("dictation allowed in this process: \(allowedHere ? "yes" : "no (not under launchd; set OI_HUD_ALLOW_DICTATION=1 to force)")")
        return out
    }

    func start(preferredLocale: String, limitS: Double = dictationLimitS, recordTo: URL? = nil) {
        guard !running else { return }
        running = true
        session += 1
        let s = session
        guard Dictation.allowedHere else {
            end(s, hint: "Dictation needs the LaunchAgent (hud/install.sh) — type instead"); return
        }
        authorize { [weak self] hint in
            guard let self, self.session == s, self.running else { return }
            if let hint { self.end(s, hint: hint); return }
            self.begin(s, preferredLocale: preferredLocale, limitS: limitS, recordTo: recordTo)
        }
    }

    /// Stops on demand (Enter, Esc, mic/hotkey toggle). Does not call onEnd.
    func stop() {
        guard running else { return }
        session += 1
        teardown()
    }

    // MARK: private

    private func authorize(_ done: @escaping (String?) -> Void) {
        func micStep() {
            switch AVCaptureDevice.authorizationStatus(for: .audio) {
            case .authorized: done(nil)
            case .notDetermined:
                AVCaptureDevice.requestAccess(for: .audio) { ok in
                    DispatchQueue.main.async { done(ok ? nil : "Microphone denied — type instead") }
                }
            default: done("Microphone denied — type instead")
            }
        }
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: micStep()
        case .notDetermined:
            SFSpeechRecognizer.requestAuthorization { st in
                DispatchQueue.main.async { st == .authorized ? micStep() : done("Speech Recognition denied — type instead") }
            }
        default: done("Speech Recognition denied — type instead")
        }
    }

    private func begin(_ s: Int, preferredLocale: String, limitS: Double, recordTo: URL?) {
        guard let rec = Dictation.recognizer(preferred: preferredLocale), rec.isAvailable else {
            end(s, hint: "On-device dictation unavailable — type instead"); return
        }
        let eng = AVAudioEngine()
        let input = eng.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else { end(s, hint: "No microphone input — type instead"); return }
        var file: AVAudioFile?
        if let url = recordTo {
            file = try? AVAudioFile(forWriting: url, settings: format.settings)
            chmod(url.path, 0o600)
            if file == nil { log("vocal: audio file not created (transcript only)") }
        }
        audioFile = file
        let sink = self.sink
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buf, _ in
            sink.append(buf)
            try? file?.write(from: buf)
        }
        eng.prepare()
        do { try eng.start() } catch {
            input.removeTap(onBus: 0)
            end(s, hint: "Microphone could not start — type instead"); return
        }
        engine = eng; recognizer = rec
        segments = DictationSegments()
        startSegment(s)
        limit = Timer.scheduledTimer(withTimeInterval: limitS, repeats: false) { [weak self] _ in
            self?.end(s, hint: nil)
        }
        log("dictation started (\(rec.locale.identifier), on-device)")
    }

    /// W20: a new recognition task (and request) on the running engine; the tap switches to it.
    /// W21: `afterMs` > 0 (the backoff) switches the tap to the new request at once — the audio of the wait is
    /// queued in it — and starts its recognition task only after the wait.
    private func startSegment(_ s: Int, afterMs: Int = 0) {
        guard recognizer != nil else { return }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.requiresOnDeviceRecognition = true      // never the cloud
        req.shouldReportPartialResults = true
        segment += 1
        let seg = segment
        request = req
        sink.set(req)
        task = nil
        guard afterMs > 0 else { startTask(s, req: req, seg: seg); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(afterMs)) { [weak self] in
            guard let self, self.session == s, self.running, self.segment == seg else { return }
            self.startTask(s, req: req, seg: seg)
        }
    }

    private func startTask(_ s: Int, req: SFSpeechAudioBufferRecognitionRequest, seg: Int) {
        guard let rec = recognizer else { return }
        task = rec.recognitionTask(with: req) { [weak self] result, error in
            let text = result?.bestTranscription.formattedString
            let final = result?.isFinal ?? false
            DispatchQueue.main.async {
                guard let self, self.session == s, self.running, self.segment == seg else { return }
                self.recognized(s, text: text, final: final, failed: error != nil)
            }
        }
    }

    private func recognized(_ s: Int, text: String?, final: Bool, failed: Bool) {
        let step = segments.handle(partial: text, final: final, failed: failed)
        let all = segments.text
        if !all.isEmpty { onPartial?(all) }
        switch step {
        case .keepGoing: break
        case .restart(let ms):
            log("dictation: segment \(segment) ended (\(final ? "final" : "error")), next segment"
                + (ms > 0 ? " in \(ms) ms (backoff)" : ""))
            request?.endAudio()
            task?.cancel()
            startSegment(s, afterMs: ms)
        }
    }

    private func end(_ s: Int, hint: String?) {
        guard session == s, running else { return }
        teardown()
        let cb = onEnd
        DispatchQueue.main.async { cb?(hint) }   // never re-entrant with start()
    }

    private func teardown() {
        running = false
        limit?.invalidate(); limit = nil
        if let eng = engine {
            eng.stop()
            eng.inputNode.removeTap(onBus: 0)
        }
        sink.set(nil)
        request?.endAudio()
        task?.finish()
        engine = nil; request = nil; task = nil; recognizer = nil
        audioFile = nil              // closes the .caf
    }
}

/// W20: the request the audio tap (audio thread) appends to; swapped on the main thread at each new segment.
private final class RequestSink: @unchecked Sendable {
    private let lock = NSLock()
    private var request: SFSpeechAudioBufferRecognitionRequest?

    func set(_ r: SFSpeechAudioBufferRecognitionRequest?) { lock.lock(); request = r; lock.unlock() }

    func append(_ buf: AVAudioPCMBuffer) {
        lock.lock(); let r = request; lock.unlock()
        r?.append(buf)
    }
}
