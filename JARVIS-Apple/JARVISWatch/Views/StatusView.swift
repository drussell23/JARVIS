import SwiftUI

struct StatusView: View {
    @EnvironmentObject var session: WatchSessionManager

    var body: some View {
        VStack(spacing: 12) {
            // Connection status
            HStack {
                Circle()
                    .fill(session.isConnected ? .green : .red)
                    .frame(width: 8, height: 8)
                Text(session.isConnected ? "Connected" : "Disconnected")
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }

            // JARVIS logo
            Text("JARVIS")
                .font(.headline)
                .fontDesign(.monospaced)

            // Active response or last daemon
            if let response = session.activeResponse {
                Text(response)
                    .font(.caption)
                    .lineLimit(4)
                    .padding(.horizontal)
            } else if let daemon = session.lastDaemon {
                Text(daemon)
                    .font(.caption2)
                    .foregroundColor(.purple)
                    .lineLimit(3)
            } else {
                Text("Press Action Button to speak")
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }

            // Voice button
            Button(action: { session.startVoiceCommand() }) {
                Image(systemName: session.isListening ? "waveform.circle.fill" : "mic.circle.fill")
                    .font(.title)
                    .foregroundColor(session.isListening ? .red : .blue)
            }
            .buttonStyle(.plain)
        }
        .task { await session.connect() }
    }
}
