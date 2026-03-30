//
//  HUDView.swift
//  JARVIS-HUD
//
//  Main HUD interface matching JARVIS web app design exactly
//  Based on web app screenshots - full-screen layout with arc reactor
//

import SwiftUI

/// Main HUD state
enum HUDState {
    case offline
    case listening
    case processing
    case speaking
    case idle
}

/// Voice interaction state
enum VoiceState {
    case inactive
    case waitingForWakeWord
    case listeningForCommand
    case processing
}

/// Voice status message
struct VoiceStatus {
    let state: VoiceState
    let message: String

    static let inactive = VoiceStatus(state: .inactive, message: "Voice detection inactive")
    static let waitingForWakeWord = VoiceStatus(state: .waitingForWakeWord, message: "Say \"Hey JARVIS\"")
    static let listeningForCommand = VoiceStatus(state: .listeningForCommand, message: "Listening...")
    static let processing = VoiceStatus(state: .processing, message: "Processing...")
}

/// Transcript message
struct TranscriptMessage: Identifiable, Equatable {
    let id = UUID()
    let speaker: String // "YOU" or "JARVIS"
    let text: String
    let timestamp: Date

    // Equatable conformance (required for onChange)
    static func == (lhs: TranscriptMessage, rhs: TranscriptMessage) -> Bool {
        lhs.id == rhs.id
    }
}

/// Main JARVIS HUD View - Matches web app exactly
struct HUDView: View {

    @State private var hudState: HUDState = .offline
    @State private var transcriptMessages: [TranscriptMessage] = []
    @State private var statusText: String = "SYSTEM OFFLINE - START BACKEND"
    @State private var commandText: String = ""
    @State private var voiceStatus: VoiceStatus = .inactive
    @State private var isWakeWordActive: Bool = false
    @State private var currentTranscript: String = ""  // Real-time voice transcript
    @State private var showScreenLockAnimation: Bool = false  // Screen lock animation overlay
    @State private var screenLockCountdown: Int = 3  // Countdown timer for screen lock
    @State private var showVisionPrompt: Bool = false  // Vision command prompt
    @State private var visionCommandText: String = ""  // Vision command input
    @State private var visionAnalyzing: Bool = false  // Vision analysis in progress
    @State private var visionResult: String? = nil  // Last vision result
    var onQuit: (() -> Void)? = nil  // Callback to quit HUD

    // Use shared PythonBridge from AppState (persisted from LoadingHUDView)
    @EnvironmentObject var appState: AppState

    // Convenience accessor for cleaner code
    private var pythonBridge: PythonBridge {
        appState.pythonBridge
    }

    var body: some View {
        ZStack {
            // FULLY TRANSPARENT GLASS - NO BLUR
            // Pure transparency so desktop shows through completely
            Color.clear
                .ignoresSafeArea()

            // 🔒 Screen Lock Animation Overlay
            if showScreenLockAnimation {
                ScreenLockAnimationView(countdown: $screenLockCountdown)
                    .transition(.opacity)
                    .zIndex(1000)  // Above everything
            }

            // 👁️ Vision Command Prompt Overlay
            if showVisionPrompt {
                VisionCommandPrompt(
                    commandText: $visionCommandText,
                    isAnalyzing: $visionAnalyzing,
                    result: $visionResult,
                    onSubmit: { executeVisionCommand() },
                    onClose: { withAnimation { showVisionPrompt = false } }
                )
                .transition(.opacity.combined(with: .scale))
                .zIndex(999)  // Below screen lock, above everything else
            }

            VStack(spacing: 0) {

                // Top section: J.A.R.V.I.S. title
                VStack(spacing: 8) {
                    Text("J.A.R.V.I.S.")
                        .font(.system(size: 72, weight: .bold, design: .monospaced))
                        .tracking(20) // Letter spacing
                        .foregroundColor(.jarvisGreen)
                        .shadow(color: Color.jarvisGreenGlow(opacity: 0.8), radius: 20)
                        .shadow(color: Color.jarvisGreenGlow(opacity: 0.6), radius: 40)

                    Text("JUST A RATHER VERY INTELLIGENT SYSTEM")
                        .font(.system(size: 12, weight: .medium, design: .monospaced))
                        .tracking(4)
                        .foregroundColor(.jarvisGreen)
                        .shadow(color: Color.jarvisGreenGlow(opacity: 0.6), radius: 10)
                }
                .padding(.top, 60)

                Spacer()

                // Center: Arc Reactor (matches web app 420px container)
                ArcReactorView(state: hudState, onQuit: onQuit)
                    .frame(width: 440, height: 440)

                Spacer()

                // Status message below reactor
                VStack(spacing: 8) {
                    Text(statusText)
                        .font(.system(size: 14, weight: .bold, design: .monospaced))
                        .tracking(3)
                        .foregroundColor(.jarvisGreen)
                        .shadow(color: Color.jarvisGreenGlow(opacity: 0.6), radius: 10)

                    // 🚀 Detailed connection state from Universal Client
                    if !pythonBridge.detailedConnectionState.isEmpty && pythonBridge.detailedConnectionState != statusText {
                        HStack(spacing: 6) {
                            ConnectionStateIndicator(state: pythonBridge.connectionStatus)

                            Text(pythonBridge.detailedConnectionState)
                                .font(.system(size: 10, weight: .medium, design: .monospaced))
                                .foregroundColor(.white.opacity(0.5))
                                .tracking(1)
                        }
                    }

                    // Server version and capabilities (when connected)
                    if pythonBridge.connectionStatus == .connected, !pythonBridge.serverVersion.isEmpty, pythonBridge.serverVersion != "unknown" {
                        Text("v\(pythonBridge.serverVersion) • \(pythonBridge.serverCapabilities.joined(separator: ", "))")
                            .font(.system(size: 9, weight: .regular, design: .monospaced))
                            .foregroundColor(.white.opacity(0.3))
                            .tracking(1)
                    }
                }
                .padding(.bottom, 30)

                // Voice Status Indicator (matching web app "Say 'Hey JARVIS'" banner)
                VoiceStatusBanner(
                    voiceStatus: voiceStatus,
                    currentTranscript: currentTranscript
                )
                .padding(.horizontal, 60)
                .padding(.bottom, 10)

                // Transcript section - Scrollable (matching web app)
                ScrollView {
                    TranscriptView(messages: transcriptMessages)
                        .padding(.vertical, 20)
                }
                .frame(height: 250)
                .frame(maxWidth: 1000)
                .padding(.horizontal, 60)
                .padding(.bottom, 20)

                // Bottom: Command input (matching web app styling)
                HStack(spacing: 12) {
                    TextField("Type a command to JARVIS...", text: $commandText, onCommit: sendCommand)
                        .textFieldStyle(PlainTextFieldStyle())
                        .font(.system(size: 14, design: .default))
                        .foregroundColor(.white)
                        .padding(.horizontal, 20)
                        .padding(.vertical, 14)
                        .background(Color.clear)

                    Button(action: sendCommand) {
                        Text("SEND")
                            .font(.system(size: 13, weight: .bold, design: .monospaced))
                            .foregroundColor(.black)
                            .padding(.horizontal, 24)
                            .padding(.vertical, 12)
                            .background(
                                RoundedRectangle(cornerRadius: 25)
                                    .fill(
                                        LinearGradient(
                                            colors: [Color(red: 0, green: 1, blue: 0.255), Color(red: 0, green: 0.667, blue: 0.180)],
                                            startPoint: .topLeading,
                                            endPoint: .bottomTrailing
                                        )
                                    )
                                    .shadow(color: Color.jarvisGreenGlow(opacity: 0.4), radius: 20)
                            )
                    }
                    .buttonStyle(PlainButtonStyle())
                    .disabled(commandText.isEmpty)
                }
                .frame(maxWidth: 800)
                .padding(8)
                .background(
                    RoundedRectangle(cornerRadius: 50)
                        .fill(Color.black.opacity(0.3))
                        .overlay(
                            RoundedRectangle(cornerRadius: 50)
                                .stroke(Color.jarvisGreen.opacity(0.2), lineWidth: 1)
                        )
                        .shadow(color: Color.jarvisGreen.opacity(0.1), radius: 20)
                )
                .padding(.horizontal, 60)
                .padding(.bottom, 40)
            }

            // 👁️ Floating Vision Button - Bottom Right Corner
            VStack {
                Spacer()
                HStack {
                    Spacer()

                    Button(action: {
                        withAnimation(.spring()) {
                            showVisionPrompt.toggle()
                        }
                    }) {
                        ZStack {
                            Circle()
                                .fill(
                                    LinearGradient(
                                        colors: [.jarvisGreen.opacity(0.8), .jarvisGreenDark],
                                        startPoint: .topLeading,
                                        endPoint: .bottomTrailing
                                    )
                                )
                                .frame(width: 60, height: 60)
                                .shadow(color: Color.jarvisGreenGlow(), radius: 15)

                            Image(systemName: "eye.fill")
                                .font(.system(size: 28))
                                .foregroundColor(.black)
                        }
                    }
                    .buttonStyle(PlainButtonStyle())
                    .help("Open Vision Analysis")
                    .padding(.trailing, 40)
                    .padding(.bottom, 40)
                }
            }
            .zIndex(500)  // Above main content, below overlays
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear {
            print("✅ HUDView appeared - using shared PythonBridge")
            print("   Connection status: \(pythonBridge.connectionStatus)")
            updateStatusFromConnection()
        }
        .onChange(of: pythonBridge.connectionStatus) { newStatus in
            updateStatusFromConnection()
        }
        .onChange(of: pythonBridge.hudState) { newState in
            hudState = newState
        }
        .onChange(of: pythonBridge.transcriptMessages) { newMessages in
            transcriptMessages = newMessages
        }
        .onChange(of: pythonBridge.voiceState) { newState in
            updateVoiceStatus(from: newState)
        }
        .onChange(of: pythonBridge.voiceTranscript) { newTranscript in
            currentTranscript = newTranscript
        }
        .onChange(of: pythonBridge.screenLockTriggered) { isTriggered in
            if isTriggered {
                triggerScreenLockAnimation()
            }
        }
    }

    private func triggerScreenLockAnimation() {
        withAnimation(.easeIn(duration: 0.3)) {
            showScreenLockAnimation = true
        }

        // Start countdown timer
        screenLockCountdown = 3
        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { timer in
            if screenLockCountdown > 0 {
                screenLockCountdown -= 1
            } else {
                timer.invalidate()
                // Hide animation after countdown
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                    withAnimation(.easeOut(duration: 0.5)) {
                        showScreenLockAnimation = false
                    }
                }
            }
        }
    }

    private func updateVoiceStatus(from state: String) {
        switch state {
        case "wake_word_listening":
            voiceStatus = .waitingForWakeWord
            isWakeWordActive = true
        case "listening":
            voiceStatus = .listeningForCommand
        case "processing":
            voiceStatus = .processing
        case "inactive":
            voiceStatus = .inactive
            isWakeWordActive = false
        default:
            break
        }
    }

    private func updateStatusFromConnection() {
        switch pythonBridge.connectionStatus {
        case .connected:
            statusText = "SYSTEM ONLINE - CONNECTED TO BACKEND"
        case .connecting:
            statusText = "CONNECTING TO BACKEND..."
        case .disconnected:
            statusText = "SYSTEM OFFLINE - BACKEND DISCONNECTED"
        case .error:
            statusText = "CONNECTION ERROR - RETRYING..."
        }
    }

    private func sendCommand() {
        // Handle command sending via PythonBridge
        if !commandText.isEmpty {
            // Add to local transcript immediately for responsive UI
            let message = TranscriptMessage(speaker: "YOU", text: commandText, timestamp: Date())
            transcriptMessages.append(message)

            // Send to backend via HTTP API
            Task {
                do {
                    try await pythonBridge.sendCommand(commandText)
                    print("✅ Command sent to backend: \(commandText)")
                } catch {
                    print("❌ Failed to send command: \(error)")
                    statusText = "ERROR: Failed to send command"
                }
            }

            commandText = ""
        }
    }

    // MARK: - Vision Command Execution

    private func executeVisionCommand() {
        guard !visionCommandText.isEmpty else { return }

        print("👁️ Executing vision command: \(visionCommandText)")
        visionAnalyzing = true
        visionResult = nil

        Task {
            do {
                let result = try await appState.visionManager.executeVisionCommand(visionCommandText)

                await MainActor.run {
                    visionAnalyzing = false

                    if result.success, let analysis = result.analysis {
                        visionResult = analysis
                        print("   ✓ Vision analysis: \(analysis.prefix(100))...")

                        // Speak result if voice is available
                        appState.voiceManager.speak(analysis, priority: .normal)
                    } else {
                        visionResult = result.error ?? "Vision analysis failed"
                        print("   ❌ Vision error: \(visionResult!)")

                        appState.voiceManager.speak("Vision analysis failed, sir.", priority: .high)
                    }
                }
            } catch {
                await MainActor.run {
                    visionAnalyzing = false
                    visionResult = "Error: \(error.localizedDescription)"
                    print("   ❌ Vision exception: \(error.localizedDescription)")

                    appState.voiceManager.speak("Vision system error, sir.", priority: .high)
                }
            }
        }
    }
}

/// Transcript display component - matches web app styling
struct TranscriptView: View {

    let messages: [TranscriptMessage]

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            if messages.isEmpty {
                // Empty state placeholder
                Text("Transcript will appear here...")
                    .font(.system(size: 14, design: .monospaced))
                    .foregroundColor(.white.opacity(0.3))
                    .italic()
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.top, 40)
            } else {
                ForEach(messages) { message in
                    VStack(alignment: .leading, spacing: 8) {
                        // Message label (YOU: or JARVIS:)
                        Text(message.speaker + ":")
                            .font(.system(size: 11, weight: .bold, design: .monospaced))
                            .foregroundColor(message.speaker == "YOU" ? Color(red: 0, green: 1, blue: 1) : Color.jarvisGreen)
                            .tracking(2)
                            .textCase(.uppercase)

                        // Message text
                        Text(message.text)
                            .font(.system(size: 15, design: .default))
                            .foregroundColor(.white.opacity(0.9))
                            .lineSpacing(6)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .animation(.easeOut(duration: 0.5), value: messages.count)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// 🚀 Connection State Indicator - Visual status indicator
struct ConnectionStateIndicator: View {
    let state: ConnectionStatus

    var body: some View {
        Circle()
            .fill(indicatorColor)
            .frame(width: 8, height: 8)
            .shadow(color: indicatorColor.opacity(0.8), radius: 4)
            .overlay(
                Circle()
                    .stroke(indicatorColor.opacity(0.3), lineWidth: 1)
                    .scaleEffect(state == .connecting ? 1.5 : 1.0)
                    .opacity(state == .connecting ? 0 : 0.6)
                    .animation(.easeInOut(duration: 1.0).repeatForever(autoreverses: false), value: state)
            )
    }

    private var indicatorColor: Color {
        switch state {
        case .connected:
            return .jarvisGreen
        case .connecting:
            return .yellow
        case .disconnected:
            return .red.opacity(0.6)
        case .error:
            return .red
        }
    }
}

/// 🔒 Screen Lock Animation - Epic fullscreen countdown overlay
struct ScreenLockAnimationView: View {
    @Binding var countdown: Int
    @State private var pulseAnimation: Bool = false
    @State private var lockIconScale: CGFloat = 0.5
    @State private var backgroundOpacity: Double = 0.0

    var body: some View {
        ZStack {
            // Full screen semi-transparent black background
            Color.black
                .opacity(backgroundOpacity)
                .ignoresSafeArea()
                .onAppear {
                    withAnimation(.easeIn(duration: 0.3)) {
                        backgroundOpacity = 0.9
                    }
                }

            VStack(spacing: 40) {
                // Lock Icon with pulsing animation
                ZStack {
                    // Outer glow rings
                    ForEach(0..<3, id: \.self) { index in
                        Circle()
                            .stroke(Color.jarvisGreen.opacity(0.3), lineWidth: 2)
                            .frame(width: 200 + CGFloat(index * 40), height: 200 + CGFloat(index * 40))
                            .scaleEffect(pulseAnimation ? 1.2 : 1.0)
                            .opacity(pulseAnimation ? 0.0 : 0.6)
                            .animation(
                                Animation.easeOut(duration: 1.5)
                                    .repeatForever(autoreverses: false)
                                    .delay(Double(index) * 0.2),
                                value: pulseAnimation
                            )
                    }

                    // Lock Icon
                    Image(systemName: "lock.fill")
                        .font(.system(size: 100))
                        .foregroundStyle(
                            LinearGradient(
                                colors: [.jarvisGreen, .jarvisGreenDark],
                                startPoint: .topLeading,
                                endPoint: .bottomTrailing
                            )
                        )
                        .shadow(color: Color.jarvisGreenGlow(), radius: 30)
                        .scaleEffect(lockIconScale)
                }
                .onAppear {
                    pulseAnimation = true
                    withAnimation(.spring(response: 0.6, dampingFraction: 0.6)) {
                        lockIconScale = 1.0
                    }
                }

                // "LOCKING SCREEN" text
                Text("LOCKING SCREEN")
                    .font(.system(size: 36, weight: .black, design: .monospaced))
                    .foregroundColor(.jarvisGreen)
                    .tracking(8)
                    .shadow(color: Color.jarvisGreenGlow(), radius: 20)

                // Countdown number
                if countdown > 0 {
                    Text("\(countdown)")
                        .font(.system(size: 120, weight: .black, design: .monospaced))
                        .foregroundStyle(
                            LinearGradient(
                                colors: [.jarvisGreen, .white],
                                startPoint: .top,
                                endPoint: .bottom
                            )
                        )
                        .shadow(color: Color.jarvisGreenGlow(opacity: 0.8), radius: 40)
                        .transition(.scale.combined(with: .opacity))
                        .id(countdown)  // Force SwiftUI to animate each new value
                } else {
                    // Final confirmation
                    VStack(spacing: 10) {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 80))
                            .foregroundColor(.jarvisGreen)
                            .shadow(color: Color.jarvisGreenGlow(), radius: 30)

                        Text("SECURED")
                            .font(.system(size: 32, weight: .bold, design: .monospaced))
                            .foregroundColor(.jarvisGreen)
                            .tracking(6)
                    }
                    .transition(.scale.combined(with: .opacity))
                }
            }
        }
    }
}

/// Voice Status Banner - Shows wake word detection and listening status
struct VoiceStatusBanner: View {
    let voiceStatus: VoiceStatus
    let currentTranscript: String

    var body: some View {
        if voiceStatus.state != .inactive {
            HStack(spacing: 8) {
                // Status indicator dot
                Circle()
                    .fill(indicatorColor)
                    .frame(width: 8, height: 8)
                    .shadow(color: indicatorColor.opacity(0.8), radius: 4)
                    .scaleEffect(voiceStatus.state == .listeningForCommand ? 1.2 : 1.0)
                    .animation(.easeInOut(duration: 0.5).repeatForever(autoreverses: true), value: voiceStatus.state)

                // Status text
                Text(displayMessage)
                    .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    .foregroundColor(.jarvisGreen)
                    .tracking(1)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .fill(Color.jarvisGreen.opacity(0.1))
                    .overlay(
                        RoundedRectangle(cornerRadius: 6)
                            .stroke(Color.jarvisGreen.opacity(0.3), lineWidth: 1)
                    )
            )
            .transition(.opacity.combined(with: .scale))
        }
    }

    private var displayMessage: String {
        // If actively listening and we have a transcript, show it
        if voiceStatus.state == .listeningForCommand && !currentTranscript.isEmpty {
            return currentTranscript
        }
        return voiceStatus.message
    }

    private var indicatorColor: Color {
        switch voiceStatus.state {
        case .inactive:
            return .gray
        case .waitingForWakeWord:
            return .jarvisGreen.opacity(0.6)
        case .listeningForCommand:
            return .jarvisGreen
        case .processing:
            return .yellow
        }
    }
}

/// 👁️ Vision Command Prompt - Floating overlay for vision analysis
struct VisionCommandPrompt: View {
    @Binding var commandText: String
    @Binding var isAnalyzing: Bool
    @Binding var result: String?
    let onSubmit: () -> Void
    let onClose: () -> Void

    var body: some View {
        ZStack {
            // Semi-transparent background
            Color.black.opacity(0.85)
                .ignoresSafeArea()
                .onTapGesture {
                    onClose()
                }

            // Vision prompt card
            VStack(spacing: 20) {
                // Header
                HStack {
                    Image(systemName: "eye.fill")
                        .font(.system(size: 24))
                        .foregroundColor(.jarvisGreen)

                    Text("JARVIS VISION ANALYSIS")
                        .font(.system(size: 18, weight: .bold, design: .monospaced))
                        .foregroundColor(.jarvisGreen)
                        .tracking(3)

                    Spacer()

                    Button(action: onClose) {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 24))
                            .foregroundColor(.white.opacity(0.6))
                    }
                    .buttonStyle(PlainButtonStyle())
                }

                Divider()
                    .background(Color.jarvisGreen.opacity(0.3))

                // Command input
                VStack(alignment: .leading, spacing: 8) {
                    Text("What do you want me to analyze?")
                        .font(.system(size: 12, weight: .medium, design: .monospaced))
                        .foregroundColor(.white.opacity(0.7))

                    TextField("e.g., \"What's on my screen?\" or \"Find the Chrome icon\"", text: $commandText)
                        .textFieldStyle(PlainTextFieldStyle())
                        .font(.system(size: 14, design: .monospaced))
                        .foregroundColor(.white)
                        .padding()
                        .background(
                            RoundedRectangle(cornerRadius: 8)
                                .fill(Color.white.opacity(0.05))
                                .overlay(
                                    RoundedRectangle(cornerRadius: 8)
                                        .stroke(Color.jarvisGreen.opacity(0.3), lineWidth: 1)
                                )
                        )
                        .disabled(isAnalyzing)
                }

                // Analyze button
                Button(action: onSubmit) {
                    HStack {
                        if isAnalyzing {
                            ProgressView()
                                .progressViewStyle(CircularProgressViewStyle(tint: .black))
                                .scaleEffect(0.8)
                        } else {
                            Image(systemName: "eye.trianglebadge.exclamationmark")
                                .font(.system(size: 16))
                        }

                        Text(isAnalyzing ? "ANALYZING..." : "ANALYZE SCREEN")
                            .font(.system(size: 13, weight: .bold, design: .monospaced))
                    }
                    .foregroundColor(.black)
                    .padding(.horizontal, 30)
                    .padding(.vertical, 12)
                    .background(
                        RoundedRectangle(cornerRadius: 8)
                            .fill(isAnalyzing ? Color.jarvisGreen.opacity(0.6) : Color.jarvisGreen)
                            .shadow(color: Color.jarvisGreenGlow(), radius: isAnalyzing ? 0 : 15)
                    )
                }
                .buttonStyle(PlainButtonStyle())
                .disabled(isAnalyzing || commandText.isEmpty)

                // Result display
                if let result = result {
                    Divider()
                        .background(Color.jarvisGreen.opacity(0.3))

                    VStack(alignment: .leading, spacing: 8) {
                        Text("ANALYSIS RESULT:")
                            .font(.system(size: 11, weight: .bold, design: .monospaced))
                            .foregroundColor(.jarvisGreen)
                            .tracking(2)

                        ScrollView {
                            Text(result)
                                .font(.system(size: 12, design: .monospaced))
                                .foregroundColor(.white.opacity(0.9))
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .frame(maxHeight: 150)
                        .padding()
                        .background(
                            RoundedRectangle(cornerRadius: 8)
                                .fill(Color.white.opacity(0.03))
                                .overlay(
                                    RoundedRectangle(cornerRadius: 8)
                                        .stroke(Color.jarvisGreen.opacity(0.2), lineWidth: 1)
                                )
                        )
                    }
                }
            }
            .padding(30)
            .frame(width: 500)
            .background(
                RoundedRectangle(cornerRadius: 16)
                    .fill(Color.black.opacity(0.95))
                    .overlay(
                        RoundedRectangle(cornerRadius: 16)
                            .stroke(
                                LinearGradient(
                                    colors: [.jarvisGreen.opacity(0.5), .jarvisGreenDark.opacity(0.3)],
                                    startPoint: .topLeading,
                                    endPoint: .bottomTrailing
                                ),
                                lineWidth: 2
                            )
                    )
                    .shadow(color: Color.jarvisGreenGlow(opacity: 0.3), radius: 30)
            )
        }
    }
}

#Preview {
    HUDView()
}
