// JARVISHUD/Views/CompactHeaderBar.swift
// Compact header bar shown when HUD is collapsed.
// Contains: mini reactor dot, JARVIS text, status, segmented tab control.

import SwiftUI

struct CompactHeaderBar: View {
    let hudState: HUDState
    let connectionStatus: ConnectionStatus
    let detailedState: String
    let serverVersion: String
    @Binding var hudTab: HUDTab
    var onExpandReactor: () -> Void

    var body: some View {
        HStack(spacing: 0) {
            // Left: Mini reactor + JARVIS name
            HStack(spacing: 10) {
                // Mini reactor dot (20px) — double-tap to expand
                Circle()
                    .fill(
                        RadialGradient(
                            colors: [reactorCenterColor, reactorEdgeColor, reactorEdgeColor.opacity(0)],
                            center: .center,
                            startRadius: 0,
                            endRadius: 10
                        )
                    )
                    .frame(width: 20, height: 20)
                    .shadow(color: reactorGlowColor.opacity(0.5), radius: 6)
                    .overlay(
                        Circle()
                            .fill(reactorCenterColor.opacity(0.8))
                            .frame(width: 6, height: 6)
                            .opacity(pulseOpacity)
                            .animation(.easeInOut(duration: 1.5).repeatForever(autoreverses: true), value: pulseOpacity)
                    )
                    .onTapGesture(count: 2) {
                        onExpandReactor()
                    }

                Text("JARVIS")
                    .font(.system(size: 14, weight: .bold, design: .monospaced))
                    .foregroundColor(.jarvisGreen)
                    .tracking(2)

                // Connection status indicator
                HStack(spacing: 4) {
                    Circle()
                        .fill(statusDotColor)
                        .frame(width: 6, height: 6)

                    Text(statusLabel)
                        .font(.system(size: 9, weight: .medium, design: .monospaced))
                        .foregroundColor(statusLabelColor)
                        .tracking(1)
                        .lineLimit(1)
                }
            }
            .padding(.leading, 20)

            Spacer()

            // Right: Segmented control
            Picker("", selection: $hudTab) {
                ForEach(HUDTab.allCases, id: \.self) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 140)
            .padding(.trailing, 20)
        }
        .frame(height: 44)
        .background(Color.black.opacity(0.2))
        .overlay(
            Rectangle()
                .fill(Color.jarvisGreen.opacity(0.1))
                .frame(height: 1),
            alignment: .bottom
        )
    }

    // MARK: - Connection Status Display

    private var statusDotColor: Color {
        switch connectionStatus {
        case .connected: return .green
        case .connecting: return .yellow
        case .disconnected: return .gray
        case .error: return .red
        }
    }

    private var statusLabel: String {
        switch connectionStatus {
        case .connected:
            if !serverVersion.isEmpty && serverVersion != "unknown" {
                return "v\(serverVersion) ONLINE"
            }
            return "ONLINE"
        case .connecting:
            return "CONNECTING..."
        case .disconnected:
            return "OFFLINE"
        case .error:
            return "ERROR"
        }
    }

    private var statusLabelColor: Color {
        switch connectionStatus {
        case .connected: return .green.opacity(0.7)
        case .connecting: return .yellow.opacity(0.7)
        case .disconnected: return .gray.opacity(0.5)
        case .error: return .red.opacity(0.7)
        }
    }

    // MARK: - Reactor Colors

    private var reactorCenterColor: Color {
        switch hudState {
        case .idle, .listening, .processing, .speaking:
            return .jarvisGreen
        case .offline:
            return .gray
        }
    }

    private var reactorEdgeColor: Color {
        reactorCenterColor.opacity(0.3)
    }

    private var reactorGlowColor: Color {
        reactorCenterColor
    }

    private var pulseOpacity: Double {
        switch hudState {
        case .idle, .listening:
            return 1.0
        case .processing, .speaking:
            return 0.6
        case .offline:
            return 0.2
        }
    }
}
