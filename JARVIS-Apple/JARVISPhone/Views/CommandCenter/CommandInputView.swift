import SwiftUI
import JARVISKit

struct CommandCenterView: View {
    @EnvironmentObject var session: PhoneSessionManager
    @State private var inputText = ""

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                // Response area
                ScrollView {
                    if let response = session.activeResponse {
                        Text(response)
                            .font(.body)
                            .padding()
                            .frame(maxWidth: .infinity, alignment: .leading)
                    } else {
                        Text("Ready for commands")
                            .foregroundColor(.secondary)
                            .padding(.top, 40)
                    }
                }

                Divider()

                // Input bar
                HStack(spacing: 12) {
                    // Voice button
                    Button(action: { session.startVoiceCommand() }) {
                        Image(systemName: session.isListening ? "waveform.circle.fill" : "mic.circle")
                            .font(.title2)
                            .foregroundColor(session.isListening ? .red : .accentColor)
                    }

                    // Text field
                    TextField("Type a command...", text: $inputText)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { sendText() }

                    // Send button
                    Button(action: sendText) {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.title2)
                    }
                    .disabled(inputText.isEmpty)
                }
                .padding()
            }
            .navigationTitle("JARVIS")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private func sendText() {
        guard !inputText.isEmpty else { return }
        let text = inputText
        inputText = ""
        Task { await session.sendCommand(text) }
    }
}
