import SwiftUI
import JARVISKit

struct CommandCenterView: View {
    @EnvironmentObject var session: PhoneSessionManager
    @State private var inputText = ""

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                // Response area
                ScrollViewReader { proxy in
                    ScrollView {
                        if let response = session.activeResponse {
                            MarkdownResponseView(text: response)
                                .padding()
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id("response-bottom")
                        } else {
                            VStack(spacing: 12) {
                                Image(systemName: "sparkles")
                                    .font(.system(size: 32))
                                    .foregroundStyle(.tertiary)
                                Text("Ready for commands")
                                    .foregroundColor(.secondary)
                            }
                            .padding(.top, 60)
                        }
                    }
                    .onChange(of: session.activeResponse) { _, _ in
                        withAnimation {
                            proxy.scrollTo("response-bottom", anchor: .bottom)
                        }
                    }
                }

                // Daemon status bar
                if let daemon = session.lastDaemon {
                    Text(daemon)
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .padding(.horizontal)
                        .padding(.vertical, 4)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(.ultraThinMaterial)
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
                    .disabled(inputText.isEmpty || session.isStreaming)
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

// MARK: - Markdown Response Renderer

/// Renders streamed markdown from JARVIS responses with proper formatting.
/// Handles: headings, bold, italic, code spans, code blocks, lists, and paragraphs.
struct MarkdownResponseView: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(parseBlocks(text).enumerated()), id: \.offset) { _, block in
                renderBlock(block)
            }
        }
    }

    // MARK: - Block parsing

    private enum Block {
        case heading(level: Int, text: String)
        case codeBlock(language: String, code: String)
        case listItem(indent: Int, text: String)
        case paragraph(text: String)
    }

    private func parseBlocks(_ input: String) -> [Block] {
        var blocks: [Block] = []
        let lines = input.components(separatedBy: "\n")
        var i = 0

        while i < lines.count {
            let line = lines[i]

            // Code block (``` fence)
            if line.hasPrefix("```") {
                let lang = String(line.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                var codeLines: [String] = []
                i += 1
                while i < lines.count && !lines[i].hasPrefix("```") {
                    codeLines.append(lines[i])
                    i += 1
                }
                if i < lines.count { i += 1 } // skip closing ```
                blocks.append(.codeBlock(language: lang, code: codeLines.joined(separator: "\n")))
                continue
            }

            // Heading (# ## ###)
            if let heading = parseHeading(line) {
                blocks.append(heading)
                i += 1
                continue
            }

            // List item (- or * or numbered)
            if let listItem = parseListItem(line) {
                blocks.append(listItem)
                i += 1
                continue
            }

            // Empty line — skip
            if line.trimmingCharacters(in: .whitespaces).isEmpty {
                i += 1
                continue
            }

            // Paragraph (accumulate consecutive non-empty lines)
            var paraLines: [String] = [line]
            i += 1
            while i < lines.count {
                let nextLine = lines[i]
                if nextLine.trimmingCharacters(in: .whitespaces).isEmpty
                    || nextLine.hasPrefix("```")
                    || nextLine.hasPrefix("#")
                    || isListItem(nextLine) {
                    break
                }
                paraLines.append(nextLine)
                i += 1
            }
            blocks.append(.paragraph(text: paraLines.joined(separator: " ")))
        }

        return blocks
    }

    private func parseHeading(_ line: String) -> Block? {
        var level = 0
        for ch in line {
            if ch == "#" { level += 1 } else { break }
        }
        guard level >= 1 && level <= 3 else { return nil }
        let rest = line.dropFirst(level)
        guard rest.first == " " else { return nil }
        let text = String(rest.dropFirst()).trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return nil }
        return .heading(level: level, text: text)
    }

    private func parseListItem(_ line: String) -> Block? {
        let trimmed = line.drop(while: { $0 == " " })
        let indent = line.count - trimmed.count
        guard let first = trimmed.first else { return nil }

        if (first == "-" || first == "*") && trimmed.dropFirst().first == " " {
            let text = String(trimmed.dropFirst(2))
            return .listItem(indent: indent / 2, text: text)
        }
        // Numbered list: "1. text"
        if first.isNumber {
            if let dotIdx = trimmed.firstIndex(of: "."),
               trimmed.index(after: dotIdx) < trimmed.endIndex,
               trimmed[trimmed.index(after: dotIdx)] == " " {
                let text = String(trimmed[trimmed.index(dotIdx, offsetBy: 2)...])
                return .listItem(indent: indent / 2, text: text)
            }
        }
        return nil
    }

    private func isListItem(_ line: String) -> Bool {
        parseListItem(line) != nil
    }

    // MARK: - Block rendering

    @ViewBuilder
    private func renderBlock(_ block: Block) -> some View {
        switch block {
        case .heading(let level, let text):
            inlineMarkdown(text)
                .font(level == 1 ? .title2.bold() : level == 2 ? .title3.bold() : .headline)
                .padding(.top, level == 1 ? 8 : 4)

        case .codeBlock(_, let code):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundColor(.primary)
                    .padding(12)
            }
            .background(Color(.systemGray6))
            .clipShape(RoundedRectangle(cornerRadius: 8))

        case .listItem(let indent, let text):
            HStack(alignment: .top, spacing: 6) {
                Text("\u{2022}")
                    .foregroundColor(.secondary)
                inlineMarkdown(text)
                    .font(.body)
            }
            .padding(.leading, CGFloat(indent) * 16)

        case .paragraph(let text):
            inlineMarkdown(text)
                .font(.body)
        }
    }

    /// Renders inline markdown (bold, italic, code, links) using AttributedString.
    private func inlineMarkdown(_ input: String) -> Text {
        if let attributed = try? AttributedString(
            markdown: input,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        ) {
            return Text(attributed)
        }
        return Text(input)
    }
}
