/// App state for JARVIS HUD — Cloud-backed via Vercel SSE
/// The nervous system connecting the Mac body to the cloud mind.
/// Zero polling. Pure reflex. — Symbiotic Manifesto, Pillar 3
import SwiftUI
import Combine
import AVFoundation
import JARVISKit

// MARK: - Shared Types (used by both AppState and HUDView)

/// Main HUD state
enum HUDState {
    case offline
    case listening
    case processing
    case speaking
    case idle
}

/// Transcript message
struct TranscriptMessage: Identifiable, Equatable {
    let id = UUID()
    let speaker: String // "YOU" or "JARVIS"
    let text: String
    let timestamp: Date

    static func == (lhs: TranscriptMessage, rhs: TranscriptMessage) -> Bool {
        lhs.id == rhs.id
    }
}

// MARK: - Enums

enum ConnectionStatus: String {
    case connected, connecting, disconnected, error
}

enum SpeechPriority: Int, Comparable {
    case low = 0
    case normal = 1
    case high = 2

    static func < (lhs: SpeechPriority, rhs: SpeechPriority) -> Bool {
        lhs.rawValue < rhs.rawValue
    }
}

// MARK: - Vision Result

struct VisionResult {
    let success: Bool
    let analysis: String?
    let error: String?

    static func ok(_ analysis: String) -> VisionResult {
        VisionResult(success: true, analysis: analysis, error: nil)
    }
    static func fail(_ error: String) -> VisionResult {
        VisionResult(success: false, analysis: nil, error: error)
    }
}

// MARK: - PythonBridge (Cloud SSE consumer — the spinal cord)

/// Persistent bidirectional event stream to Vercel cloud brain.
/// Named PythonBridge for HUDView compatibility — actually a pure Swift SSE consumer.
@MainActor
class PythonBridge: ObservableObject {
    // Connection state
    @Published var connectionStatus: ConnectionStatus = .disconnected
    @Published var detailedConnectionState: String = "Initializing..."
    @Published var serverVersion: String = "unknown"
    @Published var serverCapabilities: [String] = []

    // Loading state (kept for LoadingHUDView compatibility — not used in menu bar mode)
    @Published var loadingProgress: Int = 0
    @Published var loadingMessage: String = "Connecting to JARVIS Cloud..."
    @Published var loadingComplete: Bool = false

    // HUD state
    @Published var hudState: HUDState = .offline
    /// True when JARVIS is actively working (processing command, streaming tokens, or speaking).
    /// The app delegate observes this to auto-summon/dismiss the HUD overlay.
    @Published var isActive: Bool = false
    @Published var lastMessage: String = ""
    @Published var isVisionActive: Bool = false
    @Published var transcriptMessages: [TranscriptMessage] = []
    @Published var voiceState: String = "idle"
    @Published var voiceTranscript: String = ""
    @Published var screenLockTriggered: Bool = false

    // TTS callback — wired by AppState to VoiceManager
    var onSpeak: ((String, SpeechPriority) -> Void)?

    // Internal networking
    private var sseClient: SSEClient?
    private var commandSender: CommandSender?
    private var auth: DeviceAuth?
    private var deviceId: String?
    private var baseURL: String?
    private var consecutiveFailures = 0
    private var isRunning = false
    private var hasGreeted = false

    // Active stream accumulator (token → full response per commandId)
    private var activeStreams: [String: [String]] = [:]

    // MARK: - Boot sequence

    /// Progressive awakening — connect to the cloud brain.
    func boot() async {
        guard !isRunning else { return }
        isRunning = true

        updateLoading(progress: 5, message: "Loading credentials...")
        connectionStatus = .connecting
        detailedConnectionState = "Loading credentials..."

        // Load credentials: Keychain first, environment fallback
        guard let creds = loadCredentials() else {
            updateLoading(progress: 0, message: "Not paired — credentials missing")
            connectionStatus = .error
            detailedConnectionState = "Not paired — set credentials in Keychain or environment"
            hudState = .offline
            isRunning = false
            return
        }

        deviceId = creds.deviceId
        baseURL = creds.baseURL

        let deviceAuth = DeviceAuth(
            deviceId: creds.deviceId,
            deviceType: .mac,
            deviceSecret: creds.deviceSecret
        )
        auth = deviceAuth
        commandSender = CommandSender(baseURL: creds.baseURL, auth: deviceAuth)

        updateLoading(progress: 20, message: "Credentials loaded. Authenticating...")
        detailedConnectionState = "Authenticating with cloud..."

        // Enter SSE reconnect loop (runs forever with exponential backoff)
        await connectLoop(deviceAuth: deviceAuth, config: creds)
    }

    /// Disconnect and stop the SSE loop.
    func shutdown() {
        isRunning = false
        sseClient?.disconnect()
        sseClient = nil
        connectionStatus = .disconnected
        detailedConnectionState = "Shut down"
        hudState = .offline
    }

    // MARK: - Command sending

    func sendCommand(_ command: String, intentHint: String? = nil) async throws {
        guard let sender = commandSender else {
            throw JARVISError.notPaired
        }
        hudState = .processing
        isActive = true
        let result = try await sender.send(command, intentHint: intentHint)
        if result.status == "streaming" {
            // Tokens arrive via SSE — nothing more to do
        }
    }

    func startVision() { isVisionActive = true }
    func stopVision() { isVisionActive = false }

    // MARK: - SSE connect loop (exponential backoff)

    private func connectLoop(deviceAuth: DeviceAuth, config: HUDCredentials) async {
        while isRunning {
            do {
                try await connectOnce(deviceAuth: deviceAuth, config: config)
                consecutiveFailures = 0
            } catch is CancellationError {
                break
            } catch {
                consecutiveFailures += 1
                let backoff = min(2.0 * pow(2.0, Double(consecutiveFailures)), 60.0)
                connectionStatus = .error
                detailedConnectionState = "Connection lost — reconnecting in \(Int(backoff))s..."
                hudState = .offline

                if !loadingComplete {
                    updateLoading(
                        progress: max(loadingProgress, 15),
                        message: "Reconnecting in \(Int(backoff))s..."
                    )
                }

                try? await Task.sleep(for: .seconds(backoff))
            }
        }
    }

    private func connectOnce(deviceAuth: DeviceAuth, config: HUDCredentials) async throws {
        print("[JARVIS] Requesting stream token...")
        detailedConnectionState = "Requesting stream token..."

        let tokenManager = StreamTokenManager(
            deviceId: config.deviceId,
            auth: deviceAuth,
            baseURL: config.baseURL
        )

        let client = SSEClient(
            baseURL: config.baseURL,
            deviceId: config.deviceId,
            tokenManager: tokenManager
        )

        // SSEClient.connect() returns immediately after starting the HTTP task.
        // We use AsyncStream to block until onDisconnect fires.
        let bridge = Weak(self)
        let disconnectStream = AsyncStream<Void> { continuation in
            client.onEvent = { event in
                Task { @MainActor in bridge.value?.handleEvent(event) }
            }
            client.onDisconnect = {
                Task { @MainActor in bridge.value?.onDisconnected() }
                continuation.finish()
            }
        }

        sseClient = client
        print("[JARVIS] Connecting SSE stream to \(config.baseURL)...")
        detailedConnectionState = "Connecting to event stream..."

        try await client.connect()
        print("[JARVIS] SSE stream started, waiting for events...")
        onConnected()

        // Block here until the SSE stream closes (onDisconnect fires → stream finishes)
        for await _ in disconnectStream { }
        print("[JARVIS] SSE stream ended")

        // Throw so connectLoop knows to reconnect
        throw JARVISError.connectionFailed
    }

    // MARK: - Connection lifecycle

    private func onConnected() {
        connectionStatus = .connected
        detailedConnectionState = "Connected to JARVIS Cloud"
        hudState = .idle
        consecutiveFailures = 0

        if !hasGreeted {
            hasGreeted = true
            onSpeak?("JARVIS Online.", .normal)
        }
    }

    private func onDisconnected() {
        if connectionStatus == .connected {
            connectionStatus = .disconnected
            detailedConnectionState = "Stream ended — reconnecting..."
        }
    }

    // MARK: - Event dispatch (the reflex arc)

    private func handleEvent(_ event: JARVISEvent) {
        switch event {
        case .token(let data):
            handleToken(data)
        case .daemon(let data):
            handleDaemon(data)
        case .status(let data):
            handleStatus(data)
        case .complete(let data):
            handleComplete(data)
        case .action(let commandId, let actionType, let payload):
            handleAction(commandId: commandId, actionType: actionType, payload: payload)
        case .heartbeat:
            break // keepalive — SSEClient handles internally
        }
    }

    // Track which commands we've already spoken (prevent repeats on SSE reconnect)
    private var spokenCommands = Set<String>()

    private func handleToken(_ data: TokenEvent) {
        hudState = .processing
        isActive = true

        // First token of a new command — log it
        if activeStreams[data.commandId] == nil {
            print("[JARVIS] Receiving response for: \(data.commandId) via \(data.sourceBrain)")
        }

        // Accumulate tokens per command
        if activeStreams[data.commandId] == nil {
            activeStreams[data.commandId] = []
        }
        activeStreams[data.commandId]?.append(data.token)

        // Update the last JARVIS message in transcript (streaming append)
        let fullText = activeStreams[data.commandId]?.joined() ?? data.token
        if let lastIdx = transcriptMessages.lastIndex(where: { $0.speaker == "JARVIS" }),
           transcriptMessages[lastIdx].text != fullText {
            // Replace last JARVIS message with updated text
            transcriptMessages[lastIdx] = TranscriptMessage(
                speaker: "JARVIS",
                text: fullText,
                timestamp: Date()
            )
        } else if activeStreams[data.commandId]?.count == 1 {
            // First token — create new JARVIS message
            transcriptMessages.append(TranscriptMessage(
                speaker: "JARVIS",
                text: data.token,
                timestamp: Date()
            ))
        }
    }

    private func handleDaemon(_ data: DaemonEvent) {
        lastMessage = data.narrationText
        detailedConnectionState = "[\(data.sourceBrain)] \(data.narrationText)"

        // Daemon narrations are logged but NOT spoken — JARVIS only speaks
        // in response to user commands. This prevents unsolicited chatter
        // like repeated "online" announcements and status narrations.
        print("[JARVIS] Daemon [\(data.narrationPriority)]: \(data.narrationText)")
    }

    private func handleStatus(_ data: StatusEvent) {
        detailedConnectionState = "[\(data.phase)] \(data.message)"

        // During loading, map status progress
        if !loadingComplete, let progress = data.progress {
            updateLoading(progress: min(progress, 95), message: data.message)
        }
    }

    private func handleComplete(_ data: CompleteEvent) {
        hudState = .idle
        let tokens = activeStreams.removeValue(forKey: data.commandId)
        let fullResponse = tokens?.joined() ?? ""

        print("[JARVIS] Response complete: \(data.commandId) — \(data.sourceBrain) \(data.latencyMs)ms, \(fullResponse.count) chars")

        // Finalize the transcript message
        if let lastIdx = transcriptMessages.lastIndex(where: { $0.speaker == "JARVIS" }) {
            transcriptMessages[lastIdx] = TranscriptMessage(
                speaker: "JARVIS",
                text: fullResponse,
                timestamp: Date()
            )
        }

        detailedConnectionState = "Ready — \(data.sourceBrain) (\(data.latencyMs)ms)"

        // Speak the response — but ONLY ONCE per command (dedup on SSE reconnect)
        if !fullResponse.isEmpty && !spokenCommands.contains(data.commandId) {
            spokenCommands.insert(data.commandId)
            let cleaned = stripMarkdownForSpeech(fullResponse)
            print("[JARVIS] Speaking response (\(cleaned.count) chars)")
            onSpeak?(cleaned, .normal)

            // Cap dedup set size
            if spokenCommands.count > 50 {
                spokenCommands.removeFirst()
            }
        }

        // Mark inactive after delay
        Task {
            try? await Task.sleep(for: .seconds(8))
            if hudState == .idle { isActive = false }
        }
    }

    private func handleAction(commandId: String, actionType: String, payload: [String: String]) {
        hudState = .processing
        detailedConnectionState = "Executing: \(actionType)"
        // Actions (ghost hands, file edits) are handled by the brainstem, not the HUD
        // HUD just shows the status
    }

    // MARK: - Speech helpers

    private func stripMarkdownForSpeech(_ text: String) -> String {
        var result = text
        // Remove code blocks
        while let start = result.range(of: "```"),
              let end = result.range(of: "```", range: start.upperBound..<result.endIndex) {
            result.removeSubrange(start.lowerBound..<end.upperBound)
        }
        result = result.replacingOccurrences(of: "`", with: "")
        result = result.replacingOccurrences(of: "**", with: "")
        result = result.replacingOccurrences(of: "__", with: "")
        result = result.replacingOccurrences(of: "### ", with: "")
        result = result.replacingOccurrences(of: "## ", with: "")
        result = result.replacingOccurrences(of: "# ", with: "")
        result = result.replacingOccurrences(of: "\n- ", with: "\n")
        result = result.replacingOccurrences(of: "\n* ", with: "\n")
        // Truncate long responses
        if result.count > 500 {
            result = String(result.prefix(500)) + "... and more."
        }
        return result.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Loading helpers

    private func updateLoading(progress: Int, message: String) {
        loadingProgress = progress
        loadingMessage = message
    }

    // MARK: - Credential loading

    private struct HUDCredentials {
        let deviceId: String
        let deviceSecret: String
        let baseURL: String
    }

    private func loadCredentials() -> HUDCredentials? {
        // Priority: Environment → brainstem/.env file (no Keychain — avoids password prompts)
        if let id = ProcessInfo.processInfo.environment["JARVIS_DEVICE_ID"],
           let secret = ProcessInfo.processInfo.environment["JARVIS_DEVICE_SECRET"] {
            let url = ProcessInfo.processInfo.environment["JARVIS_VERCEL_URL"] ?? "https://jarvis-cloud-five.vercel.app"
            print("[JARVIS] Credentials from environment for device: \(id)")
            return HUDCredentials(deviceId: id, deviceSecret: secret, baseURL: url)
        }

        // Auto-discover from brainstem/.env
        if let creds = loadFromBrainstemEnv() {
            print("[JARVIS] Credentials from brainstem/.env for device: \(creds.deviceId)")
            return creds
        }

        print("[JARVIS] No credentials found. Create brainstem/.env with JARVIS_DEVICE_ID and JARVIS_DEVICE_SECRET.")
        return nil
    }

    /// Reads credentials directly from brainstem/.env — no Keychain, no prompts.
    private func loadFromBrainstemEnv() -> HUDCredentials? {
        let candidates = [
            NSHomeDirectory() + "/Documents/repos/JARVIS-AI-Agent/brainstem/.env",
            FileManager.default.currentDirectoryPath + "/brainstem/.env",
            FileManager.default.currentDirectoryPath + "/../brainstem/.env",
        ]

        var envPath: String?
        for path in candidates {
            if FileManager.default.fileExists(atPath: path) {
                envPath = path
                break
            }
        }

        guard let path = envPath,
              let contents = try? String(contentsOfFile: path, encoding: .utf8) else {
            return nil
        }

        var env: [String: String] = [:]
        for line in contents.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty, !trimmed.hasPrefix("#"),
                  let eqIdx = trimmed.firstIndex(of: "=") else { continue }
            let key = String(trimmed[trimmed.startIndex..<eqIdx])
            let value = String(trimmed[trimmed.index(after: eqIdx)...])
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            env[key] = value
        }

        guard let id = env["JARVIS_DEVICE_ID"],
              let secret = env["JARVIS_DEVICE_SECRET"] else {
            return nil
        }

        return HUDCredentials(
            deviceId: id,
            deviceSecret: secret,
            baseURL: env["JARVIS_VERCEL_URL"] ?? "https://jarvis-cloud-five.vercel.app"
        )
    }
}

// MARK: - VoiceManager (TTS via AVSpeechSynthesizer — Daniel voice)

final class VoiceManager: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    @Published var isSpeaking: Bool = false
    @Published var isListening: Bool = false

    private let synthesizer = AVSpeechSynthesizer()
    private var currentPriority: SpeechPriority = .low

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func speak(_ text: String, priority: SpeechPriority = .normal) {
        // Higher priority interrupts lower
        if synthesizer.isSpeaking && priority <= currentPriority { return }
        if synthesizer.isSpeaking { synthesizer.stopSpeaking(at: .immediate) }

        currentPriority = priority
        let utterance = AVSpeechUtterance(string: text)
        // Daniel = British English male (JARVIS canonical voice)
        utterance.voice = AVSpeechSynthesisVoice(identifier: "com.apple.voice.compact.en-GB.Daniel")
            ?? AVSpeechSynthesisVoice(language: "en-GB")
        utterance.rate = 0.52
        utterance.pitchMultiplier = 1.0
        utterance.volume = 0.9

        isSpeaking = true
        synthesizer.speak(utterance)
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        DispatchQueue.main.async { [weak self] in
            self?.isSpeaking = false
        }
    }

    func startListening() {}
    func stopListening() {}
}

// MARK: - VisionManager

@MainActor
class VisionManager: ObservableObject {
    @Published var isAnalyzing: Bool = false
    @Published var lastAnalysis: String = ""

    /// Set by AppState to route vision commands through the cloud
    weak var commandSender: PythonBridge?

    func captureAndAnalyze() {}

    func executeVisionCommand(_ command: String) async throws -> VisionResult {
        guard let sender = commandSender else {
            return .fail("Not connected to cloud")
        }

        // Route vision command through Vercel with intent_hint for Tier 0 fast-path
        do {
            try await sender.sendCommand(command, intentHint: "vision")
            // Response will arrive via SSE tokens — return success to dismiss the analyzing state
            // The actual analysis text streams into the transcript
            return .ok("Analyzing via cloud vision pipeline...")
        } catch {
            return .fail("Vision request failed: \(error.localizedDescription)")
        }
    }
}

// MARK: - AppState

@MainActor
class AppState: ObservableObject {
    @Published var isLoadingComplete: Bool = false
    @Published var pythonBridge: PythonBridge
    @Published var voiceManager: VoiceManager
    @Published var visionManager: VisionManager

    init() {
        let bridge = PythonBridge()
        let voice = VoiceManager()
        let vision = VisionManager()

        self.pythonBridge = bridge
        self.voiceManager = voice
        self.visionManager = vision

        // TTS disabled by default — user controls when JARVIS speaks
        // To enable: bridge.onSpeak = { [weak voice] text, priority in voice?.speak(text, priority: priority) }

        // Give VisionManager the command sender for cloud routing
        vision.commandSender = bridge
    }

    /// Boot the cloud connection. Call from app delegate after window creation.
    func boot() {
        Task { @MainActor in
            await pythonBridge.boot()
        }
    }
}

// MARK: - Helpers

/// Weak reference wrapper to avoid capturing `self` in @Sendable closures
private final class Weak<T: AnyObject>: @unchecked Sendable {
    weak var value: T?
    init(_ value: T) { self.value = value }
}
