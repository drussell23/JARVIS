import SwiftUI

struct PRQueueView: View {
    var body: some View {
        NavigationStack {
            List {
                Text("No active governance jobs")
                    .foregroundColor(.secondary)
            }
            .navigationTitle("Ouroboros")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}
