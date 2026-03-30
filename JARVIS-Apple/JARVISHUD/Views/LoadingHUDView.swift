//
//  LoadingHUDView.swift
//  JARVIS-HUD
//
//  Loading screen matching loading.html exactly
//  With matrix transition effect from loading-manager.js
//

import SwiftUI

/// Loading screen state
enum LoadingState {
    case initializing
    case loading(progress: Int, message: String)
    case complete
    case failed(error: String)
}

/// Loading HUD View matching loading.html
struct LoadingHUDView: View {

    @State private var loadingState: LoadingState = .initializing
    @State private var progress: CGFloat = 0
    @State private var logoScale: CGFloat = 1.0
    @State private var showMatrixTransition: Bool = false
    @State private var progressMessage: String = "Initializing JARVIS..."
    @State private var isConnected: Bool = false
    @State private var connectionAttempts: Int = 0

    // Use shared PythonBridge from AppState instead of creating our own
    // This ensures WebSocket connection persists when transitioning to HUDView
    @EnvironmentObject var appState: AppState

    var onComplete: () -> Void

    var body: some View {
        ZStack {
            // FULLY TRANSPARENT GLASS - NO BLUR
            // Pure transparency so desktop shows through completely
            Color.clear
                .ignoresSafeArea()

            if !showMatrixTransition {
                // Main loading content
                VStack(spacing: 60) {

                    Spacer()

                    // Logo
                    VStack(spacing: 10) {
                        Text("J.A.R.V.I.S.")
                            .font(.system(size: 80, weight: .black, design: .monospaced))
                            .foregroundStyle(
                                LinearGradient(
                                    colors: [.jarvisGreen, .jarvisGreenDark],
                                    startPoint: .topLeading,
                                    endPoint: .bottomTrailing
                                )
                            )
                            .shadow(color: Color.jarvisGreenGlow(opacity: 0.5), radius: 30)
                            .scaleEffect(logoScale)

                        Text(statusSubtitle)
                            .font(.system(size: 21, weight: .medium, design: .monospaced))
                            .foregroundColor(.jarvisGreenDark)
                            .tracking(4.8) // 0.3em letter spacing
                            .textCase(.uppercase)
                            .opacity(0.9)
                    }

                    // Arc Reactor
                    ArcReactorView(state: .idle)
                        .frame(width: 300, height: 300)

                    // Status message
                    Text(statusMessage)
                        .font(.system(size: 16, weight: .semibold, design: .monospaced))
                        .foregroundColor(.jarvisGreen)
                        .tracking(1.2)
                        .textCase(.uppercase)

                    // Progress bar
                    VStack(spacing: 10) {
                        GeometryReader { geometry in
                            ZStack(alignment: .leading) {
                                // Background
                                RoundedRectangle(cornerRadius: 10)
                                    .fill(Color.white.opacity(0.1))
                                    .frame(height: 8)

                                // Progress
                                RoundedRectangle(cornerRadius: 10)
                                    .fill(
                                        LinearGradient(
                                            colors: progressGradient,
                                            startPoint: .leading,
                                            endPoint: .trailing
                                        )
                                    )
                                    .frame(width: geometry.size.width * (progress / 100), height: 8)
                                    .shadow(color: progressShadowColor, radius: progressShadowRadius)
                            }
                        }
                        .frame(height: 8)

                        // Percentage
                        Text("\(Int(progress))%")
                            .font(.system(size: 18, weight: .bold, design: .monospaced))
                            .foregroundColor(.jarvisGreen)
                    }
                    .frame(maxWidth: 500)
                    .padding(.horizontal, 40)

                    Spacer()
                }
                .padding()
            } else {
                // Matrix transition
                MatrixTransitionView()
            }
        }
        .onAppear {
            startLoadingAnimation()
            setupRealtimeObservers()
            print("🚀 LoadingHUDView initialized with reactive progress tracking")
        }
        .onReceive(appState.pythonBridge.$loadingProgress) { newProgress in
            // 🚀 REAL-TIME: React immediately to any progress change
            updateProgress(newProgress)
        }
        .onReceive(appState.pythonBridge.$loadingMessage) { newMessage in
            // 🚀 REAL-TIME: Update message immediately
            withAnimation(.easeInOut(duration: 0.2)) {
                progressMessage = newMessage
            }
        }
        .onReceive(appState.pythonBridge.$loadingComplete) { isComplete in
            // Only transition when backend explicitly signals completion
            if isComplete && progress >= 100 {
                print("🎯 Backend signaled completion - starting transition animation")
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                    playCompletionAnimation()
                }
            }
        }
    }

    // MARK: - Computed Properties

    private var statusSubtitle: String {
        switch loadingState {
        case .initializing:
            return "INITIALIZING"
        case .loading:
            return "LOADING"
        case .complete:
            return "SYSTEM READY"
        case .failed:
            return "INITIALIZATION FAILED"
        }
    }

    private var statusMessage: String {
        // Use real-time message from PythonBridge if available
        if !progressMessage.isEmpty && progressMessage != "Initializing JARVIS..." {
            return progressMessage
        }

        // Fallback to state-based messages
        switch loadingState {
        case .initializing:
            return isConnected ? "Connected, waiting for backend..." : "Connecting to backend..."
        case .loading(_, let message):
            return message
        case .complete:
            return "JARVIS is online!"
        case .failed(let error):
            return error
        }
    }

    private var progressGradient: [Color] {
        if progress >= 75 {
            return [.jarvisGreen, .jarvisGreen]
        } else if progress >= 50 {
            return [.jarvisGreenDark, .jarvisGreen]
        } else if progress >= 25 {
            return [.jarvisGreen, .jarvisGreenDark]
        } else {
            return [.jarvisGreenDark, .jarvisGreenDark]
        }
    }

    private var progressShadowColor: Color {
        progress >= 75 ? Color.jarvisGreenGlow(opacity: 0.8) : Color.jarvisGreenGlow(opacity: 0.4)
    }

    private var progressShadowRadius: CGFloat {
        progress >= 75 ? 20 : 10
    }

    // MARK: - Animations & Updates

    private func startLoadingAnimation() {
        // Logo pulse
        withAnimation(
            Animation.easeInOut(duration: 2.0)
                .repeatForever(autoreverses: true)
        ) {
            logoScale = 1.05
        }

        print("ℹ️ Waiting for backend to connect and send real progress updates...")
    }

    private func updateProgress(_ newProgress: Int) {
        // 🚀 DYNAMIC: Smoothly animate progress changes
        let targetProgress = CGFloat(newProgress)

        // Only update if actually different
        if abs(progress - targetProgress) > 0.1 {
            withAnimation(.spring(response: 0.5, dampingFraction: 0.8)) {
                progress = targetProgress

                // Update state based on progress
                if targetProgress > 0 && targetProgress < 100 {
                    loadingState = .loading(
                        progress: newProgress,
                        message: progressMessage
                    )
                } else if targetProgress >= 100 {
                    loadingState = .complete
                }
            }

            print("📊 UI Progress updated: \(Int(progress))%")
        }
    }

    private func setupRealtimeObservers() {
        // 🚀 ROBUST: Set up a timer to poll bridge properties if needed
        Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { timer in
            // Force UI update if progress changed but didn't trigger
            if CGFloat(appState.pythonBridge.loadingProgress) != progress {
                updateProgress(appState.pythonBridge.loadingProgress)
            }

            // Stop timer when complete
            if appState.pythonBridge.loadingComplete {
                timer.invalidate()
            }
        }
    }

    private func playCompletionAnimation() {
        loadingState = .complete

        // Wait a moment then show matrix transition
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
            withAnimation(.easeOut(duration: 0.8)) {
                showMatrixTransition = true
            }

            // Complete after matrix animation
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
                onComplete()
            }
        }
    }
}

/// Matrix code rain transition effect
struct MatrixTransitionView: View {

    @State private var columns: [[MatrixCharacter]] = []
    @State private var opacity: Double = 0.3
    @State private var windowSize: CGSize = .zero

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                Color.black.ignoresSafeArea()

                Canvas { context, size in
                    for column in columns {
                        for char in column {
                            let point = CGPoint(x: char.x, y: char.y)
                            var text = Text(char.character)
                                .font(.system(size: 16, design: .monospaced))
                                .foregroundColor(char.isLead ? .jarvisGreen : Color.jarvisGreen.opacity(0.5))

                            context.draw(text, at: point)
                        }
                    }
                }
                .opacity(opacity)
            }
            .onAppear {
                // Capture window size for centering calculations
                windowSize = geometry.size
                initializeMatrix(windowSize: geometry.size)
                startMatrixAnimation(windowSize: geometry.size)

                withAnimation(.easeOut(duration: 1.0)) {
                    opacity = 1.0
                }

                // Fade out
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                    withAnimation(.easeOut(duration: 0.5)) {
                        opacity = 0
                    }
                }
            }
        }
    }

    private func initializeMatrix(windowSize: CGSize) {
        let columnWidth: CGFloat = 30
        let columnCount = Int(windowSize.width / columnWidth) + 2 // Extra columns for smooth edges
        let characters = "JARVIS01アイウエオカキクケコ"

        // Calculate starting X to center the matrix
        let totalMatrixWidth = CGFloat(columnCount) * columnWidth
        let startX = (windowSize.width - totalMatrixWidth) / 2

        for i in 0..<columnCount {
            var column: [MatrixCharacter] = []
            let rowCount = Int(windowSize.height / 20) + 10 // Rows based on window height

            for j in 0..<rowCount {
                let char = MatrixCharacter(
                    character: String(characters.randomElement()!),
                    x: startX + (CGFloat(i) * columnWidth),
                    y: CGFloat(j * 20) - 200, // Start above visible area
                    isLead: j == 0
                )
                column.append(char)
            }
            columns.append(column)
        }
    }

    private func startMatrixAnimation(windowSize: CGSize) {
        let maxY = windowSize.height + 100

        Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { timer in
            for i in 0..<columns.count {
                for j in 0..<columns[i].count {
                    columns[i][j].y += 2

                    // Reset to top when off-screen
                    if columns[i][j].y > maxY {
                        columns[i][j].y = -20
                    }
                }
            }
        }
    }
}

struct MatrixCharacter {
    var character: String
    var x: CGFloat
    var y: CGFloat
    var isLead: Bool
}

#Preview {
    LoadingHUDView {
        print("Loading complete!")
    }
}
