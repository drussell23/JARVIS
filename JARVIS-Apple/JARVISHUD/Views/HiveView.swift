// JARVISHUD/Views/HiveView.swift
// Main Hive tab — shows cognitive state and thread list.

import SwiftUI

struct HiveView: View {
    let hiveStore: HiveStore

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Cognitive state bar
            CognitiveStateBar(
                state: hiveStore.cognitiveState,
                activeThreads: hiveStore.activeThreadCount
            )
            .padding(.horizontal, 16)

            // Thread list
            if hiveStore.threads.isEmpty {
                Spacer()
                VStack(spacing: 8) {
                    Image(systemName: "bubble.left.and.bubble.right")
                        .font(.system(size: 24))
                        .foregroundColor(.gray.opacity(0.4))
                    Text("No Hive activity yet")
                        .font(.system(size: 12))
                        .foregroundColor(.gray)
                    Text("Agents will appear here when the system enters REM or FLOW")
                        .font(.system(size: 10))
                        .foregroundColor(.gray.opacity(0.6))
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity)
                Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 6) {
                        ForEach(hiveStore.sortedThreads) { thread in
                            HiveThreadCard(thread: thread)
                        }
                    }
                    .padding(.horizontal, 16)
                }
            }
        }
        .padding(.top, 8)
    }
}
