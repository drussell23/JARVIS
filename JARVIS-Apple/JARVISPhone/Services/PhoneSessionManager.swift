import Foundation
import AVFoundation
import JARVISKit

@MainActor
class PhoneSessionManager: ObservableObject {
    @Published var isConnected = false
    @Published var isListening = false
    @Published var isStreaming = false
    @Published var activeResponse: String?
    @Published var lastDaemon: String?

    private var auth: DeviceAuth?
    private var sender: CommandSender?
    private var sseClient: SSEClient?
    private let speechSynthesizer = AVSpeechSynthesizer()

    func connect() async {
        guard let deviceId = KeychainStore.load(key: "device_id"),
              let secret = KeychainStore.load(key: "device_secret"),
              let baseURL = KeychainStore.load(key: "vercel_url") else {
            lastDaemon = "Not paired — go to Settings"
            return
        }

        let deviceAuth = DeviceAuth(deviceId: deviceId, deviceType: .iphone, deviceSecret: secret)
        auth = deviceAuth
        sender = CommandSender(baseURL: baseURL, auth: deviceAuth)

        // SSE connection for receiving events
        let tokenManager = StreamTokenManager(deviceId: deviceId, auth: deviceAuth, baseURL: baseURL)
        let client = SSEClient(baseURL: baseURL, deviceId: deviceId, tokenManager: tokenManager)

        // Use nonisolated closures to avoid Swift 6 Sendable issues
        let weakSelf = Weak(self)
        client.onEvent = { event in
            Task { @MainActor in
                weakSelf.value?.handleEvent(event)
            }
        }
        client.onDisconnect = {
            Task { @MainActor in
                weakSelf.value?.isConnected = false
                try? await Task.sleep(for: .seconds(3))
                await weakSelf.value?.connect()
            }
        }

        sseClient = client

        do {
            try await client.connect()
            isConnected = true
            lastDaemon = nil
        } catch {
            lastDaemon = "Connection failed: \(error.localizedDescription)"
            isConnected = false
        }
    }

    func sendCommand(_ text: String) async {
        guard let sender else {
            lastDaemon = "Not connected"
            return
        }
        activeResponse = nil
        isStreaming = true
        do {
            let result = try await sender.send(text)
            if result.status == "streaming" {
                // Tokens will arrive via SSE
                lastDaemon = nil
            } else {
                isStreaming = false
                lastDaemon = "Job queued: \(result.jobId ?? "?")"
            }
        } catch {
            isStreaming = false
            lastDaemon = "Failed: \(error.localizedDescription)"
        }
    }

    func startVoiceCommand() {
        // Voice requires microphone permission — skip in simulator
        lastDaemon = "Voice input not available in simulator. Use text input."
    }

    private func handleEvent(_ event: JARVISEvent) {
        switch event {
        case .token(let data):
            activeResponse = (activeResponse ?? "") + data.token
        case .daemon(let data):
            lastDaemon = data.narrationText
        case .complete(let data):
            isStreaming = false
            if let response = activeResponse {
                speakResponse(response, latencyMs: data.latencyMs)
            } else {
                lastDaemon = "Done (\(data.latencyMs)ms)"
            }
        case .status(let data):
            lastDaemon = "[\(data.phase)] \(data.message)"
        case .heartbeat, .action:
            break
        }
    }

    // MARK: - TTS (Daniel voice — British English, canonical JARVIS)

    private func speakResponse(_ text: String, latencyMs: Int) {
        // Strip markdown formatting for cleaner speech
        let cleaned = stripMarkdown(text)

        // Truncate very long responses for speech
        let maxSpeechLength = 400
        let speechText: String
        if cleaned.count > maxSpeechLength {
            let truncated = String(cleaned.prefix(maxSpeechLength))
            speechText = truncated + "... and more."
        } else {
            speechText = cleaned
        }

        guard !speechText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }

        let utterance = AVSpeechUtterance(string: speechText)
        // Daniel = British English male voice (JARVIS canonical)
        utterance.voice = AVSpeechSynthesisVoice(identifier: "com.apple.voice.compact.en-GB.Daniel")
            ?? AVSpeechSynthesisVoice(language: "en-GB")
        utterance.rate = 0.52  // Slightly faster than default for natural cadence
        utterance.pitchMultiplier = 1.0
        utterance.volume = 0.85
        utterance.preUtteranceDelay = 0.2

        speechSynthesizer.speak(utterance)
    }

    /// Strips markdown syntax for cleaner TTS output.
    private func stripMarkdown(_ text: String) -> String {
        var result = text
        // Remove code blocks
        while let start = result.range(of: "```"),
              let end = result.range(of: "```", range: start.upperBound..<result.endIndex) {
            result.removeSubrange(start.lowerBound..<end.upperBound)
        }
        // Remove inline code
        result = result.replacingOccurrences(of: "`", with: "")
        // Remove bold/italic markers
        result = result.replacingOccurrences(of: "**", with: "")
        result = result.replacingOccurrences(of: "__", with: "")
        result = result.replacingOccurrences(of: "*", with: "")
        result = result.replacingOccurrences(of: "_", with: "")
        // Remove headings
        result = result.replacingOccurrences(of: "### ", with: "")
        result = result.replacingOccurrences(of: "## ", with: "")
        result = result.replacingOccurrences(of: "# ", with: "")
        // Remove list markers
        result = result.replacingOccurrences(of: "\n- ", with: "\n")
        result = result.replacingOccurrences(of: "\n* ", with: "\n")
        // Clean up extra whitespace
        while result.contains("  ") {
            result = result.replacingOccurrences(of: "  ", with: " ")
        }
        return result.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// Helper to avoid capturing `self` strongly in @Sendable closures
private final class Weak<T: AnyObject>: @unchecked Sendable {
    weak var value: T?
    init(_ value: T) { self.value = value }
}
