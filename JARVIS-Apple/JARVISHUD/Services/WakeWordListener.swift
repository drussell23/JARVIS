/// WakeWordListener — Always-on "Hey JARVIS" detection via on-device speech recognition.
/// Uses AVAudioEngine + SFSpeechRecognizer (on-device, no data sent to Apple).
import Foundation
import Speech
import AVFoundation

final class WakeWordListener: ObservableObject, @unchecked Sendable {
    enum State: Equatable {
        case off
        case listening
        case capturing
        case cooldown
    }

    @Published var state: State = .off
    @Published var partialTranscript: String = ""

    var onCommand: ((String) -> Void)?

    private let wakeWords = ["jarvis", "hey jarvis", "yo jarvis"]
    private let audioQueue = DispatchQueue(label: "com.jarvis.hud.audio", qos: .userInitiated)
    private var audioEngine: AVAudioEngine?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var silenceTimer: Timer?
    private var isRunning = false
    private let commandSilenceTimeout: TimeInterval = 2.5
    // Incremented on every teardown. Recognition task callbacks capture the generation
    // value at creation time and bail out if it no longer matches — this prevents the
    // cancelled-task callback from racing with the newly-started task and killing it.
    private var listenerGeneration = 0

    // MARK: - Lifecycle

    func start() {
        guard !isRunning else { return }
        isRunning = true
        SFSpeechRecognizer.requestAuthorization { [weak self] status in
            guard let self, self.isRunning else { return }
            if status == .authorized {
                print("[JARVIS Voice] Authorized")
                self.audioQueue.async { self.beginListening() }
            } else {
                print("[JARVIS Voice] Denied: \(status.rawValue)")
                DispatchQueue.main.async { self.state = .off }
            }
        }
    }

    func stop() {
        isRunning = false
        audioQueue.async { [weak self] in self?.teardown() }
        DispatchQueue.main.async { [weak self] in
            self?.state = .off
            self?.partialTranscript = ""
        }
    }

    // MARK: - Core listening loop (runs on audioQueue)

    private func beginListening() {
        let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
        guard isRunning, let recognizer, recognizer.isAvailable else {
            print("[JARVIS Voice] Recognizer unavailable, retrying...")
            audioQueue.asyncAfter(deadline: .now() + 5) { [weak self] in self?.beginListening() }
            return
        }

        teardown()

        // Snapshot generation AFTER teardown (which incremented it). This task owns
        // this value; any callback firing with a different generation is stale and ignored.
        let generation = listenerGeneration

        let engine = AVAudioEngine()
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true

        let inputNode = engine.inputNode
        let format = inputNode.outputFormat(forBus: 0)

        // kAudioUnitErr_CannotDoInCurrentContext (-10877): hardware reports sampleRate=0 during
        // warmup. Installing a tap with this format and calling engine.start() will always fail.
        // Retry quickly — the hardware is usually ready within ~500ms.
        guard format.sampleRate > 0 else {
            print("[JARVIS Voice] Audio hardware not ready (sampleRate=0), retrying in 0.5s...")
            audioQueue.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                guard let self, self.isRunning else { return }
                self.beginListening()
            }
            return
        }

        print("[JARVIS Voice] Audio format: \(format.sampleRate)Hz, \(format.channelCount)ch")

        // Track state with simple character offset (NOT String.Index)
        var wakeWordFound = false
        var wakeEndOffset = 0

        recognitionTask = recognizer.recognitionTask(with: req) { [weak self] result, error in
            guard let self, self.listenerGeneration == generation else { return }

            // Mic suppression: if the Python backend is speaking via TTS,
            // ignore all audio to prevent voice feedback loops (JARVIS hearing
            // its own speech and treating it as a new command).
            if FileManager.default.fileExists(atPath: "/tmp/jarvis_speaking") {
                return
            }

            if let result {
                let fullText = result.bestTranscription.formattedString
                let lower = fullText.lowercased()
                // Log every partial so we can confirm audio is flowing
                print("[JARVIS Voice] [\(result.isFinal ? "FINAL" : "partial")] \"\(fullText)\"")

                if !wakeWordFound {
                    // Phase 1: scan for wake word
                    for wake in self.wakeWords {
                        if let range = lower.range(of: wake) {
                            wakeWordFound = true
                            wakeEndOffset = lower.distance(from: lower.startIndex, to: range.upperBound)
                            DispatchQueue.main.async { self.state = .capturing }
                            print("[JARVIS Voice] Wake word detected in: \"\(fullText)\"")

                            // Extract any trailing command inline with wake word
                            let afterWake = self.safeSubstring(fullText, fromOffset: wakeEndOffset)
                            if !afterWake.isEmpty {
                                DispatchQueue.main.async { self.partialTranscript = afterWake }
                            }
                            self.scheduleTimeout()
                            break
                        }
                    }

                    // Direct command mode: if no wake word but substantial speech,
                    // capture it anyway. This lets you speak naturally without "Hey JARVIS".
                    if !wakeWordFound {
                        let wordCount = lower.split(separator: " ").count
                        if wordCount >= 2 {
                            DispatchQueue.main.async {
                                self.partialTranscript = fullText
                                self.state = .capturing
                            }
                            self.scheduleTimeout()
                        }
                    }
                } else {
                    // Phase 2: accumulate command after wake word
                    let command = self.safeSubstring(fullText, fromOffset: wakeEndOffset)
                    DispatchQueue.main.async { self.partialTranscript = command }
                    self.scheduleTimeout()
                }

                if result.isFinal {
                    if wakeWordFound {
                        let command = self.safeSubstring(fullText, fromOffset: wakeEndOffset)
                        if !command.isEmpty {
                            self.finalize(command)
                        } else {
                            self.restart(delay: 0.3)
                        }
                    } else {
                        // Direct command: send if substantial (≥ 2 words)
                        let command = self.partialTranscript.isEmpty ? fullText : self.partialTranscript
                        let trimmed = command.trimmingCharacters(in: .whitespacesAndNewlines)
                        if trimmed.split(separator: " ").count >= 2 {
                            self.finalize(trimmed)
                        } else {
                            self.restart(delay: 0.3)
                        }
                    }
                }
            }

            if let error {
                let nsError = error as NSError
                // Only log real errors, not routine speech recognizer timeouts
                let isRoutineTimeout = (nsError.domain == "kAFAssistantErrorDomain" && [203, 216, 1110].contains(nsError.code))
                    || (nsError.domain == "kLSRErrorDomain" && nsError.code == 301) // recognition canceled (normal after finalize)
                if !isRoutineTimeout {
                    print("[JARVIS Voice] Recognition ended: \(nsError.domain) code=\(nsError.code) — \(error.localizedDescription)")
                }

                if wakeWordFound {
                    // Had a wake word, use whatever we captured
                    let cmd = self.safeSubstring(result?.bestTranscription.formattedString ?? "", fromOffset: wakeEndOffset)
                    if !cmd.isEmpty { self.finalize(cmd) } else { self.restart(delay: 0.5) }
                } else {
                    // Normal timeout or no speech — seamless restart (mic stays "on")
                    self.restart(delay: 0.1)
                }
            }
        }

        // 4096 frames ≈ 85ms at 48kHz — large enough to prevent HALC I/O overload.
        // Guard against zero-byte buffers produced during audio hardware warmup.
        inputNode.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
            guard buffer.frameLength > 0 else { return }
            req.append(buffer)
        }

        do {
            engine.prepare()
            try engine.start()
            audioEngine = engine
            request = req
            DispatchQueue.main.async { self.state = .listening }
            print("[JARVIS Voice] Listening...")
        } catch {
            // self.audioEngine was never assigned, so teardown() won't clean up the local engine.
            // Manually remove the tap and cancel the recognition task to avoid leaking resources
            // into the next attempt.
            inputNode.removeTap(onBus: 0)
            req.endAudio()
            recognitionTask?.cancel()
            recognitionTask = nil

            let nsError = error as NSError
            // -10877 = kAudioUnitErr_CannotDoInCurrentContext — hardware transitioning (common
            // during AirPods swap, wake-from-sleep, audio route change). Retry quickly.
            if nsError.code == -10877 {
                print("[JARVIS Voice] Hardware context unavailable (-10877), retrying in 1s...")
                audioQueue.asyncAfter(deadline: .now() + 1) { [weak self] in
                    guard let self, self.isRunning else { return }
                    self.beginListening()
                }
            } else {
                print("[JARVIS Voice] Engine start failed: \(error)")
                restart(delay: 2)
            }
        }
    }

    // MARK: - Safe string operations (avoids String.Index crash)

    /// Extracts substring starting at a character offset. Returns "" if offset is out of range.
    private func safeSubstring(_ str: String, fromOffset offset: Int) -> String {
        guard offset >= 0, offset < str.count else { return "" }
        let idx = str.index(str.startIndex, offsetBy: offset)
        return String(str[idx...]).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Silence timer

    private func scheduleTimeout() {
        DispatchQueue.main.async { [weak self] in
            self?.silenceTimer?.invalidate()
            self?.silenceTimer = Timer.scheduledTimer(withTimeInterval: self?.commandSilenceTimeout ?? 2.5, repeats: false) { [weak self] _ in
                guard let self else { return }
                let cmd = self.partialTranscript
                if !cmd.isEmpty {
                    self.finalize(cmd)
                } else {
                    self.restart(delay: 0.3)
                }
            }
        }
    }

    // MARK: - Finalize + restart

    private func finalize(_ command: String) {
        print("[JARVIS Voice] >>> \"\(command)\"")
        DispatchQueue.main.async { [weak self] in
            self?.silenceTimer?.invalidate()
            self?.silenceTimer = nil
            self?.state = .cooldown
            self?.partialTranscript = ""
            self?.onCommand?(command)
        }
        audioQueue.async { [weak self] in
            self?.teardown()
        }
        audioQueue.asyncAfter(deadline: .now() + 3) { [weak self] in
            guard let self, self.isRunning else { return }
            self.beginListening()
        }
    }

    private func restart(delay: TimeInterval) {
        audioQueue.async { [weak self] in self?.teardown() }
        DispatchQueue.main.async { [weak self] in
            // Stay in .listening during normal restarts — no visible flicker.
            // Only clear transcript and timers, don't transition to .cooldown.
            self?.partialTranscript = ""
            self?.silenceTimer?.invalidate()
            self?.silenceTimer = nil
        }
        guard isRunning else { return }
        audioQueue.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, self.isRunning else { return }
            self.beginListening()
        }
    }

    private func teardown() {
        listenerGeneration += 1     // Must be first — any in-flight callback reads this
        audioEngine?.stop()
        audioEngine?.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        recognitionTask?.cancel()
        audioEngine = nil
        request = nil
        recognitionTask = nil
    }
}
