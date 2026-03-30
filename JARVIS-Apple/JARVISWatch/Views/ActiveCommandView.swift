import SwiftUI

struct ActiveCommandView: View {
    let text: String
    let source: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(source)
                .font(.caption2)
                .foregroundColor(.secondary)
                .fontDesign(.monospaced)
            Text(text)
                .font(.caption)
        }
        .padding()
    }
}
