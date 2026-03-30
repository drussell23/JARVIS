/// WakeWordListener — Always-on "Hey JARVIS" detection via on-device speech recognition.
/// Listens continuously for the wake word, then captures the command that follows.
/// Uses AVAudioEngine + SFSpeechRecognizer (on-device, no data sent to Apple).
import Foundation
import Speech
import AVFoundation

@MainActor
final class WakeWordListener: ObservableObject {
    enum State: Equatable {
        case off
        case listening          // Waiting for wake word
        case capturing          // Wake word heard, capturing command
        case cooldown           // Brief pause before restarting
    }

    @Published var state: State = .off
    @Published var partialTranscript: String = ""

    /// Called when a full command is captured after the wake word.
    var onCommand: ((String) -> Void)?

    private let wakeWords = ["jarvis", "hey jarvis", "yo jarvis"]
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var audioEngine: AVAudioEngine?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var silenceTimer: Timer?
    private var isRunning = false

    // After wake word detected, how long of silence before we finalize the command
    private let commandSilenceTimeout: TimeInterval = 2.0
    // Max command duration after wake word
    private let maxCommandDuration: TimeInterval = 15.0

    // MARK: - Lifecycle

    func start() {
        guard !isRunning else { return }
        isRunning = true
        requestPermissionAndListen()
    }

    func stop() {
        isRunning = false
        teardownAudio()
        state = .off
        partialTranscript = ""
    }

    // MARK: - Permission

    private func requestPermissionAndListen() {
        SFSpeechRecognizer.requestAuthorization { [weak self] status in
            DispatchQueue.main.async {
                guard let self, self.isRunning else { return }
                if status == .authorized {
                    print("[JARVIS Voice] Speech recognition authorized")
                    self.startListeningCycle()
                } else {
                    print("[JARVIS Voice] Speech recognition denied (status: \(status.rawValue))")
                    self.state = .off
                }
            }
        }
    }

    // MARK: - Listening cycle (restarts automatically)

    private func startListeningCycle() {
        guard isRunning, let recognizer, recognizer.isAvailable else {
            print("[JARVIS Voice] Recognizer not available, retrying in 5s...")
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
                self?.startListeningCycle()
            }
            return
        }

        teardownAudio()

        let engine = AVAudioEngine()
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.requiresOnDeviceRecognition = true // Privacy: never send audio to Apple

        let inputNode = engine.inputNode
        let format = inputNode.outputFormat(forBus: 0)

        // Track whether we've found the wake word in this recognition session
        var wakeWordFound = false
        var commandStartIndex: String.Index?

        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
            guard let self else { return }

            if let result {
                let text = result.bestTranscription.formattedString.lowercased()

                DispatchQueue.main.async {
                    if !wakeWordFound {
                        // Phase 1: Scanning for wake word
                        for wake in self.wakeWords {
                            if let range = text.range(of: wake) {
                                wakeWordFound = true
                                commandStartIndex = range.upperBound
                                self.state = .capturing
                                self.resetSilenceTimer()
                                print("[JARVIS Voice] Wake word detected!")

                                // Extract any command text that came with the wake word
                                let after = String(text[range.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
                                if !after.isEmpty {
                                    self.partialTranscript = after
                                    self.resetSilenceTimer()
                                }
                                break
                            }
                        }
                    } else if let startIdx = commandStartIndex {
                        // Phase 2: Capturing command after wake word
                        let command = String(text[startIdx...]).trimmingCharacters(in: .whitespacesAndNewlines)
                        self.partialTranscript = command
                        self.resetSilenceTimer()
                    }

                    // If recognition is final, process what we have
                    if result.isFinal && wakeWordFound {
                        let command = self.partialTranscript
                        if !command.isEmpty {
                            self.finalizeCommand(command)
                        } else {
                            // Wake word only, no command — restart
                            self.restartAfterCooldown()
                        }
                    }
                }
            }

            if error != nil {
                DispatchQueue.main.async {
                    // Recognition timed out or errored — restart the cycle
                    self.restartAfterCooldown()
                }
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
            state = .listening
            print("[JARVIS Voice] Listening for wake word...")
        } catch {
            print("[JARVIS Voice] Audio engine failed: \(error)")
            restartAfterCooldown()
        }
    }

    // MARK: - Command finalization

    private func resetSilenceTimer() {
        silenceTimer?.invalidate()
        silenceTimer = Timer.scheduledTimer(withTimeInterval: commandSilenceTimeout, repeats: false) { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, self.state == .capturing else { return }
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
        teardownAudio()
        state = .cooldown
        partialTranscript = ""

        onCommand?(command)

        // Restart listening after a brief cooldown (let response play first)
        restartAfterCooldown(delay: 3.0)
    }

    private func restartAfterCooldown(delay: TimeInterval = 0.5) {
        teardownAudio()
        state = .cooldown
        partialTranscript = ""
        silenceTimer?.invalidate()
        silenceTimer = nil

        guard isRunning else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, self.isRunning else { return }
            self.startListeningCycle()
        }
    }

    // MARK: - Cleanup

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
