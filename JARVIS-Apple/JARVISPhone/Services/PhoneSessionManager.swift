import Foundation
import JARVISKit

@MainActor
class PhoneSessionManager: ObservableObject {
    @Published var isConnected = false
    @Published var isListening = false
    @Published var activeResponse: String?
    @Published var lastDaemon: String?

    private var auth: DeviceAuth?
    private var sender: CommandSender?
    private var sseClient: SSEClient?

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
        do {
            let result = try await sender.send(text)
            if result.status == "streaming" {
                // Tokens will arrive via SSE
                lastDaemon = nil
            } else {
                lastDaemon = "Job queued: \(result.jobId ?? "?")"
            }
        } catch {
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
            if activeResponse == nil {
                lastDaemon = "Done (\(data.latencyMs)ms)"
            }
        case .status(let data):
            lastDaemon = "[\(data.phase)] \(data.message)"
        case .heartbeat, .action:
            break
        }
    }
}

/// Helper to avoid capturing `self` strongly in @Sendable closures
private final class Weak<T: AnyObject>: @unchecked Sendable {
    weak var value: T?
    init(_ value: T) { self.value = value }
}
