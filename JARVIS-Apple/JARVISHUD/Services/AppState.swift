/// App state for JARVIS HUD — Cloud-backed via Vercel SSE
/// Replaces the original PythonBridge-based AppState
import SwiftUI
import Combine

enum ConnectionStatus: String {
    case connected, connecting, disconnected, error
}

class PythonBridge: ObservableObject {
    @Published var connectionStatus: ConnectionStatus = .disconnected
    @Published var lastMessage: String = ""
    @Published var isVisionActive: Bool = false
    @Published var loadingProgress: Int = 0
    @Published var loadingMessage: String = "Connecting to JARVIS Cloud..."
    @Published var loadingComplete: Bool = false
    @Published var detailedConnectionState: String = "disconnected"
    @Published var hudState: String = "idle"
    @Published var transcriptMessages: [String] = []
    @Published var voiceState: String = "idle"
    @Published var voiceTranscript: String = ""
    @Published var screenLockTriggered: Bool = false

    func sendCommand(_ command: String) {}
    func startVision() {}
    func stopVision() {}
}

class VoiceManager: ObservableObject {
    @Published var isSpeaking: Bool = false
    @Published var isListening: Bool = false

    func speak(_ text: String) {}
    func startListening() {}
    func stopListening() {}
}

class VisionManager: ObservableObject {
    @Published var isAnalyzing: Bool = false
    @Published var lastAnalysis: String = ""

    func captureAndAnalyze() {}
    func executeVisionCommand(_ command: String) {}
}

class AppState: ObservableObject {
    @Published var isLoadingComplete: Bool = false
    @Published var pythonBridge: PythonBridge
    @Published var voiceManager: VoiceManager
    @Published var visionManager: VisionManager

    init() {
        self.pythonBridge = PythonBridge()
        self.voiceManager = VoiceManager()
        self.visionManager = VisionManager()
    }
}
