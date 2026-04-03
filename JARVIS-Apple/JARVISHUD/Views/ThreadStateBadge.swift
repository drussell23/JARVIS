// JARVISHUD/Views/ThreadStateBadge.swift
import SwiftUI

struct ThreadStateBadge: View {
    let state: ThreadStateValue

    var body: some View {
        Text(state.rawValue.uppercased())
            .font(.system(size: 10, weight: .bold, design: .monospaced))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(backgroundColor)
            .foregroundColor(foregroundColor)
            .clipShape(RoundedRectangle(cornerRadius: 4))
    }

    private var backgroundColor: Color {
        switch state {
        case .open: return Color.gray.opacity(0.3)
        case .debating: return Color.orange.opacity(0.2)
        case .consensus: return Color.green.opacity(0.2)
        case .executing: return Color.purple.opacity(0.2)
        case .resolved: return Color.blue.opacity(0.2)
        case .stale: return Color.red.opacity(0.2)
        }
    }

    private var foregroundColor: Color {
        switch state {
        case .open: return .gray
        case .debating: return .orange
        case .consensus: return .green
        case .executing: return .purple
        case .resolved: return .blue
        case .stale: return .red
        }
    }
}
