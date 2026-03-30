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
    private var transcriber = SpeechTranscriber()

    func connect() async {
        guard let deviceId = KeychainStore.load(key: "device_id"),
              let secret = KeychainStore.load(key: "device_secret"),
              let baseURL = KeychainStore.load(key: "vercel_url") else { return }

        let deviceAuth = DeviceAuth(deviceId: deviceId, deviceType: .iphone, deviceSecret: secret)
        auth = deviceAuth
        sender = CommandSender(baseURL: baseURL, auth: deviceAuth)

        let tokenManager = StreamTokenManager(deviceId: deviceId, auth: deviceAuth, baseURL: baseURL)
        sseClient = SSEClient(baseURL: baseURL, deviceId: deviceId, tokenManager: tokenManager)
        sseClient?.onEvent = { [weak self] event in
            Task { @MainActor in self?.handleEvent(event) }
        }
        sseClient?.onDisconnect = { [weak self] in
            Task { @MainActor in
                self?.isConnected = false
                try? await Task.sleep(for: .seconds(2))
                await self?.connect()
            }
        }

        do {
            try await sseClient?.connect()
            isConnected = true
        } catch {
            lastDaemon = "Connection failed"
        }
    }

    func startVoiceCommand() {
        isListening = true
        transcriber.onTranscript = { [weak self] text, isFinal in
            guard isFinal else { return }
            Task { @MainActor in
                self?.isListening = false
                await self?.sendCommand(text)
            }
        }
        try? transcriber.startListening()
    }

    func sendCommand(_ text: String) async {
        guard let sender else { return }
        activeResponse = nil
        do {
            _ = try await sender.send(text)
        } catch {
            lastDaemon = "Failed: \(error.localizedDescription)"
        }
    }

    private func handleEvent(_ event: JARVISEvent) {
        switch event {
        case .token(let data):
            activeResponse = (activeResponse ?? "") + data.token
        case .daemon(let data):
            lastDaemon = data.narrationText
        case .complete:
            Task {
                try? await Task.sleep(for: .seconds(5))
                activeResponse = nil
            }
        case .status, .heartbeat, .action:
            break
        }
    }
}
