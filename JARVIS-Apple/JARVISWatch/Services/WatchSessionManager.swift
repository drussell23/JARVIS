import Foundation
import JARVISKit
import WatchKit

@MainActor
class WatchSessionManager: ObservableObject {
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
              let baseURL = KeychainStore.load(key: "vercel_url") else {
            lastDaemon = "Not paired. Open iPhone app to pair."
            return
        }

        let deviceAuth = DeviceAuth(deviceId: deviceId, deviceType: .watch, deviceSecret: secret)
        auth = deviceAuth
        sender = CommandSender(baseURL: baseURL, auth: deviceAuth)

        let tokenManager = StreamTokenManager(deviceId: deviceId, auth: deviceAuth, baseURL: baseURL)
        sseClient = SSEClient(baseURL: baseURL, deviceId: deviceId, tokenManager: tokenManager)
        sseClient?.onEvent = { [weak self] event in
            Task { @MainActor in self?.handleEvent(event) }
        }
        sseClient?.onConnect = { [weak self] in
            Task { @MainActor in self?.isConnected = true }
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
            lastDaemon = "Connection failed: \(error.localizedDescription)"
        }
    }

    func startVoiceCommand() {
        WKInterfaceDevice.current().play(.click)
        isListening = true

        transcriber.onTranscript = { [weak self] text, isFinal in
            guard isFinal else { return }
            Task { @MainActor in
                self?.isListening = false
                WKInterfaceDevice.current().play(.success)
                await self?.sendCommand(text)
            }
        }

        do {
            try transcriber.startListening()
        } catch {
            isListening = false
            WKInterfaceDevice.current().play(.failure)
        }
    }

    private func sendCommand(_ text: String) async {
        guard let sender else { return }
        do {
            let context = CommandContext(
                batteryLevel: Double(WKInterfaceDevice.current().batteryLevel)
            )
            _ = try await sender.send(text, context: context)
        } catch {
            lastDaemon = "Send failed: \(error.localizedDescription)"
        }
    }

    private func handleEvent(_ event: JARVISEvent) {
        switch event {
        case .token(let data):
            activeResponse = (activeResponse ?? "") + data.token
        case .daemon(let data):
            lastDaemon = data.narrationText
            if data.narrationPriority == "urgent" {
                WKInterfaceDevice.current().play(.notification)
            }
        case .complete:
            // Clear after delay
            Task {
                try? await Task.sleep(for: .seconds(5))
                activeResponse = nil
            }
        case .status(let data):
            lastDaemon = "[\(data.phase)] \(data.message)"
        case .heartbeat, .action:
            break
        }
    }
}
