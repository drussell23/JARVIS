/// WakeWordListener — Always-on "Hey JARVIS" detection via on-device speech recognition.
/// Listens continuously for the wake word, then captures the command that follows.
/// Uses AVAudioEngine + SFSpeechRecognizer (on-device, no data sent to Apple).
///
/// Threading: Audio engine and speech callbacks run on background threads.
/// Only @Published properties are dispatched to main via DispatchQueue.main.async.
import Foundation
import Speech
import AVFoundation

final class WakeWordListener: ObservableObject, @unchecked Sendable {
    enum State: Equatable {
        case off
        case listening          // Waiting for wake word
        case capturing          // Wake word heard, capturing command
        case cooldown           // Brief pause before restarting
    }

    @Published var state: State = .off
    @Published var partialTranscript: String = ""

    /// Called on the main thread when a full command is captured after the wake word.
    var onCommand: ((String) -> Void)?

    private let wakeWords = ["jarvis", "hey jarvis", "yo jarvis"]
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))

    // Audio runs on a dedicated background queue — never on main
    private let audioQueue = DispatchQueue(label: "com.jarvis.hud.audio", qos: .userInitiated)
    private var audioEngine: AVAudioEngine?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var silenceTimer: Timer?
    private var isRunning = false

    private let commandSilenceTimeout: TimeInterval = 2.0

    // MARK: - Lifecycle

    func start() {
        guard !isRunning else { return }
        isRunning = true
        requestPermissionAndListen()
    }

    func stop() {
        isRunning = false
        audioQueue.async { [weak self] in
            self?.teardownAudio()
        }
        DispatchQueue.main.async { [weak self] in
            self?.state = .off
            self?.partialTranscript = ""
        }
    }

    // MARK: - Permission

    private func requestPermissionAndListen() {
        SFSpeechRecognizer.requestAuthorization { [weak self] status in
            guard let self, self.isRunning else { return }
            if status == .authorized {
                print("[JARVIS Voice] Speech recognition authorized")
                self.audioQueue.async { self.startListeningCycle() }
            } else {
                print("[JARVIS Voice] Speech recognition denied (status: \(status.rawValue))")
                DispatchQueue.main.async { self.state = .off }
            }
        }
    }

    // MARK: - Listening cycle (runs on audioQueue, restarts automatically)

    private func startListeningCycle() {
        guard isRunning, let recognizer, recognizer.isAvailable else {
            print("[JARVIS Voice] Recognizer not available, retrying in 5s...")
            audioQueue.asyncAfter(deadline: .now() + 5) { [weak self] in
                self?.startListeningCycle()
            }
            return
        }

        teardownAudio()

        let engine = AVAudioEngine()
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        if #available(macOS 15, *) {
            req.requiresOnDeviceRecognition = true
        }

        let inputNode = engine.inputNode
        let format = inputNode.outputFormat(forBus: 0)

        var wakeWordFound = false
        var commandStartIndex: String.Index?

        let recognitionTask = recognizer.recognitionTask(with: req) { [weak self] result, error in
            guard let self else { return }

            if let result {
                let text = result.bestTranscription.formattedString.lowercased()

                if !wakeWordFound {
                    // Phase 1: Scanning for wake word
                    for wake in self.wakeWords {
                        if let range = text.range(of: wake) {
                            wakeWordFound = true
                            commandStartIndex = range.upperBound
                            DispatchQueue.main.async { self.state = .capturing }
                            self.resetSilenceTimer()
                            print("[JARVIS Voice] Wake word detected!")

                            let after = String(text[range.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
                            if !after.isEmpty {
                                DispatchQueue.main.async { self.partialTranscript = after }
                                self.resetSilenceTimer()
                            }
                            break
                        }
                    }
                } else if let startIdx = commandStartIndex {
                    // Phase 2: Capturing command after wake word
                    let command = String(text[startIdx...]).trimmingCharacters(in: .whitespacesAndNewlines)
                    DispatchQueue.main.async { self.partialTranscript = command }
                    self.resetSilenceTimer()
                }

                if result.isFinal && wakeWordFound {
                    DispatchQueue.main.async {
                        let command = self.partialTranscript
                        if !command.isEmpty {
                            self.finalizeCommand(command)
                        } else {
                            self.restartAfterCooldown()
                        }
                    }
                }
            }

            if error != nil {
                self.audioQueue.async { self.restartAfterCooldown() }
            }
        }

        inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            req.append(buffer)
        }

        do {
            engine.prepare()
            try engine.start()
            audioEngine = engine
            request = req
            task = recognitionTask
            DispatchQueue.main.async { self.state = .listening }
            print("[JARVIS Voice] Listening for wake word...")
        } catch {
            print("[JARVIS Voice] Audio engine failed: \(error)")
            restartAfterCooldown()
        }
    }

    // MARK: - Command finalization

    private func resetSilenceTimer() {
        DispatchQueue.main.async { [weak self] in
            self?.silenceTimer?.invalidate()
            self?.silenceTimer = Timer.scheduledTimer(withTimeInterval: self?.commandSilenceTimeout ?? 2.0, repeats: false) { [weak self] _ in
                guard let self else { return }
                let command = self.partialTranscript
                if !command.isEmpty {
                    self.finalizeCommand(command)
                } else {
                    self.restartAfterCooldown()
                }
            }
        }
    }

    private func finalizeCommand(_ command: String) {
        print("[JARVIS Voice] Command: \"\(command)\"")
        silenceTimer?.invalidate()
        silenceTimer = nil

        audioQueue.async { [weak self] in
            self?.teardownAudio()
        }

        DispatchQueue.main.async { [weak self] in
            self?.state = .cooldown
            self?.partialTranscript = ""
            self?.onCommand?(command)
        }

        // Restart listening after cooldown
        audioQueue.asyncAfter(deadline: .now() + 3.0) { [weak self] in
            guard let self, self.isRunning else { return }
            self.startListeningCycle()
        }
    }

    private func restartAfterCooldown(delay: TimeInterval = 0.5) {
        teardownAudio()
        DispatchQueue.main.async { [weak self] in
            self?.state = .cooldown
            self?.partialTranscript = ""
            self?.silenceTimer?.invalidate()
            self?.silenceTimer = nil
        }

        guard isRunning else { return }
        audioQueue.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, self.isRunning else { return }
            self.startListeningCycle()
        }
    }

    // MARK: - Cleanup (call from audioQueue only)

    private func teardownAudio() {
        audioEngine?.stop()
        audioEngine?.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.cancel()
        audioEngine = nil
        request = nil
        task = nil
    }
}
