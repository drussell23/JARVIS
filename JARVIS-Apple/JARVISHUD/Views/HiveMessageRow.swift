// JARVISHUD/Views/HiveMessageRow.swift
import SwiftUI

struct HiveMessageRow: View {
    let message: HiveMessage

    var body: some View {
        switch message {
        case .agentLog(let data):
            agentLogRow(data)
        case .personaReasoning(let data):
            personaReasoningRow(data)
        }
    }

    @ViewBuilder
    private func agentLogRow(_ data: AgentLogData) -> some View {
        HStack(alignment: .top, spacing: 8) {
            // Agent initials badge
            Text(initials(data.agentName))
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundColor(Color(hex: HiveColor.lightBlue.hex))
                .frame(width: 28, height: 28)
                .background(Color(hex: HiveColor.lightBlue.hex).opacity(0.15))
                .clipShape(RoundedRectangle(cornerRadius: 6))

            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(data.agentName)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(.gray)

                    Text(severityIcon(data.severity))
                        .font(.system(size: 10))
                }

                // Show key payload values
                let payloadText = data.payload.map { "\($0.key): \($0.value.stringValue)" }.joined(separator: ", ")
                if !payloadText.isEmpty {
                    Text(payloadText)
                        .font(.system(size: 11))
                        .foregroundColor(.secondary)
                        .lineLimit(2)
                }
            }
        }
        .padding(.vertical, 4)
        .padding(.horizontal, 8)
        .background(Color(hex: HiveColor.lightBlue.hex).opacity(0.03))
    }

    @ViewBuilder
    private func personaReasoningRow(_ data: PersonaReasoningData) -> some View {
        let persona = data.personaEnum ?? .jarvis

        HStack(alignment: .top, spacing: 8) {
            // Persona badge
            Text(persona.abbreviation)
                .font(.system(size: 11, weight: .bold))
                .foregroundColor(.white)
                .frame(width: 32, height: 32)
                .background(
                    LinearGradient(
                        colors: [Color(hex: persona.color.hex).opacity(0.8), Color(hex: persona.color.hex)],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
                .clipShape(RoundedRectangle(cornerRadius: 8))

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 4) {
                    Text(persona.displayName)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundColor(Color(hex: persona.color.hex))

                    Text(persona.roleName)
                        .font(.system(size: 10))
                        .foregroundColor(.gray)
                }

                Text(data.reasoning)
                    .font(.system(size: 12))
                    .foregroundColor(.primary.opacity(0.85))
                    .lineLimit(5)

                HStack(spacing: 8) {
                    if let verdict = data.validateVerdict {
                        Text(verdict == "approve" ? "APPROVED" : "REJECTED")
                            .font(.system(size: 9, weight: .bold, design: .monospaced))
                            .foregroundColor(verdict == "approve" ? .green : .red)
                    }

                    Text("conf: \(String(format: "%.0f%%", data.confidence * 100))")
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundColor(.gray)

                    Text("\(data.tokenCost) tok")
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundColor(.gray)
                }
            }
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 8)
        .background(Color(hex: persona.color.hex).opacity(0.05))
        .overlay(
            Rectangle()
                .fill(Color(hex: persona.color.hex))
                .frame(width: 3),
            alignment: .leading
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func initials(_ name: String) -> String {
        name.split(separator: "_").compactMap(\.first).map(String.init).prefix(2).joined().uppercased()
    }

    private func severityIcon(_ severity: String) -> String {
        switch severity {
        case "error", "critical": return "🔴"
        case "warning": return "🟡"
        default: return "🔵"
        }
    }
}

// Color(hex:) is defined in Shared/JARVISColors.swift — no duplicate needed here.
