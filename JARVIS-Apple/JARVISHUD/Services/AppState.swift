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

    /// Slice F — the Adaptive UI State Machine. Driven purely by daemon SSE
    /// telemetry (no timers). Views gate the command input on
    /// `systemStatus.isInputEnabled`, show the init overlay on
    /// `systemStatus.showsInitializationOverlay`, and render the amber
    /// autonomy-offline indicator on `systemStatus.isAutonomyOffline`.
    let systemStatus = SystemStatusStore()
    private var statusCancellables = Set<AnyCancellable>()

    // TTS callback — wired by AppState to VoiceManager
    var onSpeak: ((String, SpeechPriority) -> Void)?

    init() {
        // Re-publish the nested store's changes so `@ObservedObject` views that
        // observe the bridge also refresh when the lifecycle transitions.
        systemStatus.objectWillChange
            .sink { [weak self] _ in self?.objectWillChange.send() }
            .store(in: &statusCancellables)
    }

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

    // Commands sent by THIS session — only these get spoken aloud.
    // Prevents backlog replay from speaking old responses on reconnect.
    private var sessionCommandIds = Set<String>()

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

        // MALFORMED is a third state, distinct from PRESENT and MISSING.
        //
        // `loadCredentials()` returning nil already lands cleanly on "not
        // paired". Credentials that exist but cannot be used had no handling at
        // all — they went straight into `DeviceAuth`, which trapped, and the
        // app died at launch with `Fatal error: String index is out of bounds`.
        //
        // The message is deliberately NOT the "not paired" one: the remedies
        // are opposite. "Not paired" means go and pair the device. This means
        // the value you already have is corrupt — re-pair or fix the `.env`.
        // Telling an operator to pair a device that IS paired sends them
        // looking in the one place the problem is not.
        guard let deviceAuth = DeviceAuth(
            deviceId: creds.deviceId,
            deviceType: .mac,
            deviceSecret: creds.deviceSecret
        ) else {
            let hint = "JARVIS_DEVICE_SECRET must be an even number of hex "
                     + "characters (got \(creds.deviceSecret.count))"
            print("[JARVIS] Device secret is not usable — \(hint)")
            updateLoading(progress: 0, message: "Device secret is malformed")
            connectionStatus = .error
            detailedConnectionState = "Malformed device secret — \(hint)"
            hudState = .offline
            isRunning = false
            return
        }
        auth = deviceAuth
        commandSender = CommandSender(baseURL: creds.baseURL, auth: deviceAuth)

        updateLoading(progress: 20, message: "Credentials loaded. Authenticating...")
        detailedConnectionState = "Authenticating with cloud..."

        // Enter SSE reconnect loop (runs forever with exponential backoff)
        await connectLoop(deviceAuth: deviceAuth, config: creds)
    }

    /// Called by BrainstemLauncher when IPC connects — backend is fully
    /// booted and ready for commands. This is the definitive "JARVIS Online" moment.
    func onBackendReady() {
        connectionStatus = .connected
        detailedConnectionState = "JARVIS Online — local backend ready"
        hudState = .idle

        if !loadingComplete {
            updateLoading(progress: 100, message: "JARVIS Online")
            loadingComplete = true
        }

        if !hasGreeted {
            hasGreeted = true
            onSpeak?("JARVIS Online. Backend connected.", .normal)
        }
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

    func sendCommand(_ command: String, intentHint: String? = nil, context: CommandContext? = nil) async throws {
        guard let sender = commandSender else {
            throw JARVISError.notPaired
        }
        hudState = .processing
        isActive = true

        // Generate command ID up front and register it BEFORE the network call.
        // This prevents a race: SSE events (tokens + complete) arrive via Redis
        // before the POST response returns, so handleComplete would skip speech
        // if the ID wasn't already in sessionCommandIds.
        let commandId = UUID().uuidString
        sessionCommandIds.insert(commandId)
        print("[JARVIS] Sending command \(commandId): \"\(command)\"")

        // VLA: auto-capture a screenshot and attach it to every command so Claude has
        // full situational awareness. Capture runs async off the main thread.
        // If Screen Recording permission is not yet granted, screenshot is nil and the
        // command is sent as text-only — graceful degradation, no failure path.
        var resolvedContext = context ?? CommandContext()
        if resolvedContext.screenshot == nil {
            print("[JARVIS] Capturing screenshot for VLA...")
            resolvedContext.screenshot = await ScreenCaptureService.shared.captureBase64()
        }
        print("[JARVIS] Screenshot: \(resolvedContext.screenshot != nil ? "\(resolvedContext.screenshot!.count) chars base64" : "nil — sending text-only")")

        let result = try await sender.send(
            command,
            commandId: commandId,
            intentHint: intentHint,
            context: resolvedContext
        )
        print("[JARVIS] Command acknowledged — status: \(result.status)")
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
            } catch let error as JARVISError where error == .cloudDisabled {
                // Vercel deployment disabled (402) — stop retrying cloud.
                // Don't announce "online" here — wait for onBackendReady()
                // which fires when IPC connects (backend fully booted).
                print("[JARVIS] Cloud disabled (Vercel 402) — waiting for local backend IPC")
                detailedConnectionState = "Cloud unavailable — waiting for local backend..."

                if !loadingComplete {
                    updateLoading(
                        progress: max(loadingProgress, 30),
                        message: "Starting local backend..."
                    )
                }

                // Stay alive but don't retry cloud — the brainstem IPC handles everything.
                // Check periodically if cloud recovers.
                while isRunning {
                    try? await Task.sleep(for: .seconds(60))
                    // Quick probe to see if Vercel is back
                    do {
                        let tokenManager = StreamTokenManager(
                            deviceId: config.deviceId,
                            auth: deviceAuth,
                            baseURL: config.baseURL
                        )
                        _ = try await tokenManager.getToken()
                        // Cloud is back — break to reconnect via outer loop
                        print("[JARVIS] Cloud recovered — reconnecting SSE")
                        detailedConnectionState = "Cloud recovered — reconnecting..."
                        break
                    } catch let e as JARVISError where e == .cloudDisabled {
                        continue  // Still disabled, keep local
                    } catch {
                        continue  // Other error, keep local
                    }
                }
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
        // Slice F: a fresh stream starts pre-telemetry. `connecting` keeps the
        // input ENABLED (fail-soft) — a backend that is already ready and thus
        // re-emits nothing can never strand the user behind a spinner.
        systemStatus.reset()

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

        // Slice F: fold the backend lifecycle telemetry into the Adaptive UI
        // State Machine. SYSTEM_HYDRATING → init overlay; SYSTEM_DEGRADED /
        // OUROBOROS_FAULT → unlock the chat input + amber indicator. Purely
        // event-driven — no timers, no polling.
        systemStatus.apply(lifecycleRaw: data.lifecycle, narration: data.narrationText)

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

        // Speak the response ONLY if:
        // 1. This command was sent by THIS session (not backlog replay)
        // 2. We haven't already spoken it (dedup on SSE reconnect)
        let isOurCommand = sessionCommandIds.contains(data.commandId)
        if isOurCommand && !fullResponse.isEmpty && !spokenCommands.contains(data.commandId) {
            spokenCommands.insert(data.commandId)
            let cleaned = stripMarkdownForSpeech(fullResponse)
            print("[JARVIS] Speaking response (\(cleaned.count) chars)")
            onSpeak?(cleaned, .normal)

            // Cap dedup set size
            if spokenCommands.count > 50 {
                spokenCommands.removeFirst()
            }
        } else if !isOurCommand {
            print("[JARVIS] Skipping speech for replayed command: \(data.commandId)")
        }

        // Mark inactive after delay
        Task {
            try? await Task.sleep(for: .seconds(8))
            if hudState == .idle { isActive = false }
        }
    }

    private func handleAction(commandId: String, actionType: String, payload: [String: String]) {
        // Only forward action events for commands issued in THIS session.
        // Prevents replaying old action events from the Redis backlog on reconnect.
        guard sessionCommandIds.contains(commandId) else {
            print("[JARVIS] Skipping replayed action for old command: \(commandId)")
            return
        }

        hudState = .processing
        detailedConnectionState = "Executing: \(actionType)"
        print("[JARVIS] Action event received: \(actionType) (cmd=\(commandId))")

        // Forward action events to the brainstem via stdin pipe.
        // The brainstem can't reach Vercel directly (Python SSL issue),
        // so the HUD acts as the network gateway and forwards events locally.
        let eventData: [String: Any] = [
            "command_id": commandId,
            "action_type": actionType,
            "payload": payload,
        ]
        BrainstemLauncher.shared.sendEvent(eventType: "action", data: eventData)
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
        let env = ProcessInfo.processInfo.environment

        // LOCAL-FIRST (Phase 9, 2026-07-19): the HUD runs on the SAME Mac as
        // unified_supervisor, so by default it connects DIRECTLY to the local
        // backend on localhost:8010 — no Vercel relay (which is blocked). The
        // local /api/stream/token + /api/stream/{id} + /api/command endpoints
        // are loopback-trusted, so a placeholder device id/secret is fine.
        // Set JARVIS_HUD_FORCE_CLOUD=1 to fall back to the cloud path.
        let forceCloud = (env["JARVIS_HUD_FORCE_CLOUD"] ?? "") == "1"
        if !forceCloud {
            // Derived from the launcher's own port — ONE source of truth.
            // This was hardcoded 8010 while BrainstemLauncher binds its spawned
            // backend on 8011 ("separate port from supervisor's 8010"), so the
            // HUD spawned a healthy backend and then knocked on the wrong door
            // forever: every /api/stream/token went to 8010, connection
            // refused. Two hardcoded ports for one conversation is how they
            // drift; the env override remains for pointing at an external
            // supervisor on 8010.
            let localURL = env["JARVIS_LOCAL_BACKEND_URL"]
                ?? "http://localhost:\(BrainstemLauncher.shared.httpPort)"
            let id = env["JARVIS_DEVICE_ID"] ?? "mac-local"
            // The default was the literal string "local", and the comment above
            // called a placeholder secret "fine". It was not: the secret is
            // hex-encoded by contract on all three implementations, and "local"
            // is five characters — odd-length, so `DeviceAuth` trapped and the
            // app died at launch on the DEFAULT path. Running the HUD from
            // Xcode without JARVIS_DEVICE_SECRET set reproduced it every time.
            //
            // Derived rather than replaced with a hex literal: no magic
            // constant to drift, deterministic per device id, and valid input
            // for Python's `bytes.fromhex` and TypeScript's
            // `Buffer.from(_, "hex")` as well as ours — so pointing the local
            // path at a backend that DOES verify signatures becomes a
            // configuration change rather than another crash.
            let secret = env["JARVIS_DEVICE_SECRET"]
                ?? DeviceAuth.derivedLocalSecret(forDeviceId: id)
            print("[JARVIS] LOCAL-FIRST: connecting to \(localURL) (device: \(id))")
            return HUDCredentials(deviceId: id, deviceSecret: secret, baseURL: localURL)
        }

        // Priority: Environment → brainstem/.env file (no Keychain — avoids password prompts)
        if let id = env["JARVIS_DEVICE_ID"],
           let secret = env["JARVIS_DEVICE_SECRET"] {
            let url = env["JARVIS_VERCEL_URL"] ?? "https://jarvis-cloud-five.vercel.app"
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

@MainActor
final class VoiceManager: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    @Published var isSpeaking: Bool = false
    @Published var isListening: Bool = false

    private let synthesizer = AVSpeechSynthesizer()
    private var currentPriority: SpeechPriority = .low

    override init() {
        super.init()
        synthesizer.delegate = self
        SpeechGate.shared.registerReconciler(.voiceManager) { [weak self] in
            self?.synthesizer.isSpeaking ?? false
        }
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
        // v285.0: the SECOND synthesiser in this app, and until now it
        // suppressed NOTHING — `HUDAppDelegate` guarded its own utterances
        // while this one spoke straight into a live microphone. One claim per
        // speaker is the only arrangement where that cannot happen again.
        SpeechGate.shared.claim(.voiceManager,
                                seconds: min(1.5 + Double(text.count) / 12.0, 60.0),
                                reason: "VoiceManager.speak")
        synthesizer.speak(utterance)
    }

    // AVSpeechSynthesizerDelegate callbacks arrive on an unspecified queue, so
    // this stays nonisolated and hops to the MainActor natively (no
    // DispatchQueue) to mutate the isolated UI state.
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in
            self?.isSpeaking = false
            SpeechGate.shared.release(.voiceManager, reason: "didFinish")
        }
    }

    // `speak` calls `stopSpeaking(at: .immediate)` whenever a higher-priority
    // utterance arrives, so cancellation is not an edge case here — it is a
    // designed, routine event. Without this callback every interruption leaked
    // a claim, and the mic stayed shut until the gate's deadline expired it.
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in
            self?.isSpeaking = false
            SpeechGate.shared.release(.voiceManager, reason: "didCancel")
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
