// JARVISHUD/Views/HiveThreadCard.swift
import SwiftUI

struct HiveThreadCard: View {
    let thread: HiveThread
    @State private var isExpanded: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header (always visible, tappable)
            Button(action: { withAnimation(.easeInOut(duration: 0.2)) { isExpanded.toggle() } }) {
                HStack(spacing: 8) {
                    Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 10))
                        .foregroundColor(.gray)
                        .frame(width: 12)

                    Text(thread.title)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundColor(.primary)
                        .lineLimit(1)

                    Spacer()

                    ThreadStateBadge(state: thread.state)

                    Text("\(thread.messages.count)")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(.gray)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            // Expanded: show messages
            if isExpanded {
                Divider()
                    .padding(.horizontal, 10)

                VStack(alignment: .leading, spacing: 4) {
                    ForEach(thread.messages) { message in
                        HiveMessageRow(message: message)
                    }

                    // Token usage bar
                    if thread.tokensConsumed > 0 {
                        HStack(spacing: 4) {
                            GeometryReader { geo in
                                ZStack(alignment: .leading) {
                                    RoundedRectangle(cornerRadius: 2)
                                        .fill(Color.gray.opacity(0.2))
                                    RoundedRectangle(cornerRadius: 2)
                                        .fill(tokenColor)
                                        .frame(width: geo.size.width * tokenRatio)
                                }
                            }
                            .frame(height: 4)

                            Text("\(thread.tokensConsumed)/\(thread.tokenBudget)")
                                .font(.system(size: 9, design: .monospaced))
                                .foregroundColor(.gray)
                        }
                        .padding(.horizontal, 10)
                        .padding(.top, 4)
                    }
                }
                .padding(.vertical, 6)
            }
        }
        .background(Color.primary.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Color.primary.opacity(0.08), lineWidth: 1)
        )
    }

    private var tokenRatio: CGFloat {
        guard thread.tokenBudget > 0 else { return 0 }
        return min(1.0, CGFloat(thread.tokensConsumed) / CGFloat(thread.tokenBudget))
    }

    private var tokenColor: Color {
        if tokenRatio > 0.8 { return .red }
        if tokenRatio > 0.5 { return .orange }
        return .green
    }
}
