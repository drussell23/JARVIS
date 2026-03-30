//
//  VoiceManager.swift
//  JARVIS-HUD
//
//  Advanced Voice Management System - Text-to-Speech Integration
//  Supports multiple TTS engines with dynamic fallback and streaming
//

import Foundation
import AVFoundation
import Combine

/// Voice synthesis engine types
enum VoiceEngine: String, Codable {
    case system = "system"              // macOS native TTS
    case backend = "backend"            // Backend TTS service (ElevenLabs, Google, etc.)
    case hybrid = "hybrid"              // Use both based on availability
}

/// Voice priority levels for queue management
enum VoicePriority: Int, Codable, Comparable {
    case low = 0
    case normal = 1
    case high = 2
    case critical = 3

    static func < (lhs: VoicePriority, rhs: VoicePriority) -> Bool {
        return lhs.rawValue < rhs.rawValue
    }
}

/// Voice message model
struct VoiceMessage: Identifiable, Codable {
    let id: UUID
    let text: String
    let priority: VoicePriority
    let engine: VoiceEngine
    let timestamp: Date
    let metadata: [String: String]?

    init(text: String, priority: VoicePriority = .normal, engine: VoiceEngine = .hybrid, metadata: [String: String]? = nil) {
        self.id = UUID()
        self.text = text
        self.priority = priority
        self.engine = engine
        self.timestamp = Date()
        self.metadata = metadata
    }
}

/// Voice playback state
enum VoicePlaybackState: Equatable {
    case idle
    case speaking(message: VoiceMessage)
    case paused
    case cancelled
    case error(message: String)

    static func == (lhs: VoicePlaybackState, rhs: VoicePlaybackState) -> Bool {
        switch (lhs, rhs) {
        case (.idle, .idle), (.paused, .paused), (.cancelled, .cancelled):
            return true
        case (.speaking(let msg1), .speaking(let msg2)):
            return msg1.id == msg2.id
        case (.error(let err1), .error(let err2)):
            return err1 == err2
        default:
            return false
        }
    }
}

/// Advanced Voice Manager with async queue, priority handling, and multi-engine support
@MainActor
class VoiceManager: NSObject, ObservableObject {

    // MARK: - Published State

    @Published var playbackState: VoicePlaybackState = .idle
    @Published var currentMessage: VoiceMessage?
    @Published var queuedMessages: [VoiceMessage] = []
    @Published var isEnabled: Bool = true
    @Published var currentEngine: VoiceEngine = .hybrid

    // MARK: - Configuration

    private let apiBaseURL: URL
    private var speechSynthesizer: AVSpeechSynthesizer?
    private var audioPlayer: AVAudioPlayer?

    // Queue management
    private var messageQueue: [VoiceMessage] = []
    private var processingQueue = false
    private var cancellables = Set<AnyCancellable>()

    // Engine availability
    private var systemEngineAvailable = true
    private var backendEngineAvailable = false

    // Voice configuration (loaded from backend)
    private var voiceConfig: [String: Any] = [:]
    private var preferredVoice: AVSpeechSynthesisVoice?

    // MARK: - Initialization

    init(apiBaseURL: URL) {
        self.apiBaseURL = apiBaseURL
        super.init()

        print("🎤 VoiceManager.init() - Initializing advanced voice system")
        print("   API Base URL: \(apiBaseURL.absoluteString)")

        setupSystemVoice()
        Task {
            await checkBackendAvailability()
            await loadVoiceConfiguration()
        }
    }

    // MARK: - System Voice Setup

    private func setupSystemVoice() {
        speechSynthesizer = AVSpeechSynthesizer()
        speechSynthesizer?.delegate = self

        // Find best JARVIS-like voice (Alex, British male voice)
        if let voice = AVSpeechSynthesisVoice(identifier: "com.apple.ttsbundle.Alex-compact") ??
                        AVSpeechSynthesisVoice(language: "en-GB") {
            preferredVoice = voice
            print("   ✓ System voice configured: \(voice.name)")
        } else {
            preferredVoice = AVSpeechSynthesisVoice(language: "en-US")
            print("   ✓ System voice configured: Default US English")
        }

        systemEngineAvailable = true
    }

    // MARK: - Backend Integration

    private func checkBackendAvailability() async {
        do {
            let healthURL = apiBaseURL.appendingPathComponent("/health")
            let (_, response) = try await URLSession.shared.data(from: healthURL)

            if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 {
                backendEngineAvailable = true
                print("   ✓ Backend voice engine available")
            }
        } catch {
            backendEngineAvailable = false
            print("   ⚠️  Backend voice engine unavailable: \(error.localizedDescription)")
        }
    }

    private func loadVoiceConfiguration() async {
        do {
            let configURL = apiBaseURL.appendingPathComponent("/voice/config")
            let (data, _) = try await URLSession.shared.data(from: configURL)

            if let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                voiceConfig = config
                print("   ✓ Voice configuration loaded: \(config.keys.joined(separator: ", "))")
            }
        } catch {
            print("   ⚠️  Could not load voice config: \(error.localizedDescription)")
        }
    }

    // MARK: - Public API

    /// Speak text with dynamic engine selection and priority handling
    func speak(_ text: String, priority: VoicePriority = .normal, engine: VoiceEngine = .hybrid, metadata: [String: String]? = nil) {
        guard isEnabled else {
            print("🔇 Voice disabled - skipping: \(text.prefix(50))...")
            return
        }

        let message = VoiceMessage(text: text, priority: priority, engine: engine, metadata: metadata)
        enqueueMessage(message)
    }

    /// Stop current speech and clear queue
    func stopSpeaking() {
        print("🛑 Stopping all speech")

        // Stop system voice
        speechSynthesizer?.stopSpeaking(at: .immediate)

        // Stop audio player
        audioPlayer?.stop()
        audioPlayer = nil

        // Clear queue
        messageQueue.removeAll()
        queuedMessages = []
        currentMessage = nil
        playbackState = .idle
    }

    /// Pause current speech
    func pauseSpeaking() {
        speechSynthesizer?.pauseSpeaking(at: .word)
        audioPlayer?.pause()
        playbackState = .paused
    }

    /// Resume paused speech
    func resumeSpeaking() {
        if playbackState == .paused {
            speechSynthesizer?.continueSpeaking()
            audioPlayer?.play()
            if let msg = currentMessage {
                playbackState = .speaking(message: msg)
            }
        }
    }

    /// Clear message queue
    func clearQueue() {
        messageQueue.removeAll()
        queuedMessages = []
    }

    // MARK: - Queue Management

    private func enqueueMessage(_ message: VoiceMessage) {
        print("📝 Enqueueing message (priority: \(message.priority)): \(message.text.prefix(50))...")

        // Insert message in priority order
        if let insertIndex = messageQueue.firstIndex(where: { $0.priority < message.priority }) {
            messageQueue.insert(message, at: insertIndex)
        } else {
            messageQueue.append(message)
        }

        queuedMessages = messageQueue

        // Process queue if not already processing
        if !processingQueue {
            Task {
                await processQueue()
            }
        }
    }

    private func processQueue() async {
        guard !processingQueue else { return }
        processingQueue = true

        while !messageQueue.isEmpty {
            let message = messageQueue.removeFirst()
            queuedMessages = messageQueue
            currentMessage = message

            await speakMessage(message)

            // Wait for completion or timeout
            await waitForCompletion()
        }

        processingQueue = false
        currentMessage = nil
        playbackState = .idle
    }

    private func waitForCompletion() async {
        // Wait until playback completes or times out (30 seconds max)
        let timeout = Date().addingTimeInterval(30)

        while Date() < timeout {
            if case .idle = playbackState {
                break
            }
            if case .cancelled = playbackState {
                break
            }
            if case .error = playbackState {
                break
            }

            try? await Task.sleep(nanoseconds: 100_000_000) // 100ms
        }
    }

    // MARK: - Speech Synthesis

    private func speakMessage(_ message: VoiceMessage) async {
        print("🎤 Speaking: \(message.text.prefix(50))... (engine: \(message.engine))")
        playbackState = .speaking(message: message)

        let engine = determineEngine(for: message)

        switch engine {
        case .system:
            await speakWithSystemVoice(message)
        case .backend:
            await speakWithBackendVoice(message)
        case .hybrid:
            // Try backend first, fallback to system
            if backendEngineAvailable {
                await speakWithBackendVoice(message)
            } else {
                await speakWithSystemVoice(message)
            }
        }
    }

    private func determineEngine(for message: VoiceMessage) -> VoiceEngine {
        switch message.engine {
        case .system:
            return .system
        case .backend:
            return backendEngineAvailable ? .backend : .system
        case .hybrid:
            // Prefer backend for better quality, fallback to system
            return backendEngineAvailable ? .backend : .system
        }
    }

    // MARK: - System Voice

    private func speakWithSystemVoice(_ message: VoiceMessage) async {
        guard let synthesizer = speechSynthesizer else {
            playbackState = .error(message: "System voice unavailable")
            return
        }

        let utterance = AVSpeechUtterance(string: message.text)
        utterance.voice = preferredVoice

        // Configure speech parameters based on priority
        switch message.priority {
        case .critical:
            utterance.rate = 0.52 // Slightly faster for urgency
            utterance.volume = 1.0
        case .high:
            utterance.rate = 0.50
            utterance.volume = 0.9
        default:
            utterance.rate = 0.48 // Natural JARVIS pace
            utterance.volume = 0.8
        }

        synthesizer.speak(utterance)
    }

    // MARK: - Backend Voice (HTTP API)

    private func speakWithBackendVoice(_ message: VoiceMessage) async {
        do {
            // Request TTS from backend
            var request = URLRequest(url: apiBaseURL.appendingPathComponent("/voice/speak"))
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")

            let body: [String: Any] = [
                "text": message.text,
                "priority": message.priority.rawValue,
                "return_audio": true // Request audio data
            ]
            request.httpBody = try JSONSerialization.data(withJSONObject: body)

            let (data, response) = try await URLSession.shared.data(for: request)

            guard let httpResponse = response as? HTTPURLResponse,
                  httpResponse.statusCode == 200 else {
                throw VoiceError.backendFailed
            }

            // Play received audio
            await playAudioData(data)

        } catch {
            print("❌ Backend voice failed: \(error.localizedDescription)")
            // Fallback to system voice
            await speakWithSystemVoice(message)
        }
    }

    private func playAudioData(_ data: Data) async {
        do {
            audioPlayer = try AVAudioPlayer(data: data)
            audioPlayer?.delegate = self
            audioPlayer?.prepareToPlay()
            audioPlayer?.play()

            // Wait for playback to complete
            while audioPlayer?.isPlaying == true {
                try await Task.sleep(nanoseconds: 100_000_000) // 100ms
            }

            playbackState = .idle

        } catch {
            print("❌ Audio playback failed: \(error.localizedDescription)")
            playbackState = .error(message: error.localizedDescription)
        }
    }
}

// MARK: - AVSpeechSynthesizerDelegate

extension VoiceManager: AVSpeechSynthesizerDelegate {

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didStart utterance: AVSpeechUtterance) {
        print("🎤 System voice started speaking")
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in
            print("✅ System voice finished speaking")
            playbackState = .idle
        }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor in
            print("🛑 System voice cancelled")
            playbackState = .cancelled
        }
    }
}

// MARK: - AVAudioPlayerDelegate

extension VoiceManager: AVAudioPlayerDelegate {

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in
            print("✅ Audio playback finished (success: \(flag))")
            playbackState = .idle
        }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        Task { @MainActor in
            print("❌ Audio decode error: \(error?.localizedDescription ?? "unknown")")
            playbackState = .error(message: error?.localizedDescription ?? "Audio decode error")
        }
    }
}

// MARK: - Errors

enum VoiceError: Error {
    case backendFailed
    case audioPlaybackFailed
    case invalidAudioData
}
