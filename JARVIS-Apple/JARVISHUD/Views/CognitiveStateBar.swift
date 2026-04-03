// JARVISHUD/Views/CognitiveStateBar.swift
import SwiftUI

struct CognitiveStateBar: View {
    let state: CognitiveStateValue
    let activeThreads: Int

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: state.icon)
                .foregroundColor(stateColor)
                .font(.system(size: 12))

            Text(state.displayName)
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .foregroundColor(stateColor)

            Spacer()

            if activeThreads > 0 {
                Text("\(activeThreads) active")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundColor(.gray)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(stateColor.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(stateColor.opacity(0.2), lineWidth: 1)
        )
    }

    private var stateColor: Color {
        switch state {
        case .baseline: return .cyan
        case .rem: return .purple
        case .flow: return .orange
        }
    }
}
