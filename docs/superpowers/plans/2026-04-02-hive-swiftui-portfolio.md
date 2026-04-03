# Hive SwiftUI HiveView + Portfolio — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Hive visible — native SwiftUI tab in the macOS HUD, public portfolio page, and API bridge.

**Architecture:** HiveStore (@Observable) receives IPC events and maintains thread state. HiveView renders threads with color-coded persona cards. Portfolio polls a public summary API on jarvis-cloud. Three independent sub-projects: SwiftUI (JARVIS-Apple), API bridge (jarvis-cloud), portfolio page (jarvis-portfolio).

**Tech Stack:** Swift 6.0 / SwiftUI / macOS 14+ (JARVISHUD), Next.js 14 / TypeScript (jarvis-portfolio + jarvis-cloud), Upstash Redis

**Spec:** `docs/superpowers/specs/2026-04-02-hive-swiftui-portfolio-design.md`

---

## File Structure

### Sub-Project A: SwiftUI (JARVIS-Apple)

| File | Responsibility |
|------|----------------|
| Create: `JARVISHUD/Models/HiveModels.swift` | HiveThread, HiveMessage, AgentLogData, PersonaReasoningData structs |
| Create: `JARVISHUD/Services/HiveStore.swift` | @Observable store, IPC event handling, thread state management |
| Create: `JARVISHUD/Views/HiveView.swift` | Main Hive tab container with cognitive state header + thread list |
| Create: `JARVISHUD/Views/CognitiveStateBar.swift` | FSM state indicator (BASELINE/REM/FLOW with colors) |
| Create: `JARVISHUD/Views/HiveThreadCard.swift` | Expandable thread card with messages |
| Create: `JARVISHUD/Views/HiveMessageRow.swift` | Individual message renderer (agent_log vs persona_reasoning) |
| Create: `JARVISHUD/Views/ThreadStateBadge.swift` | Color-coded state pill |
| Modify: `JARVISHUD/Views/HUDView.swift` | Add segmented control "Chat \| Hive", swap content |
| Modify: `JARVISHUD/JARVISHUDApp.swift` | Add HiveStore to environment |
| Modify: `JARVISHUD/Services/BrainstemLauncher.swift` | Route hive_* events to HiveStore |

### Sub-Project B: API Bridge (jarvis-cloud)

| File | Responsibility |
|------|----------------|
| Create: `jarvis-cloud/lib/hive/hive-state.ts` | Redis-backed Hive state accumulator |
| Create: `jarvis-cloud/app/api/hive/summary/route.ts` | Public summary endpoint (no auth) |
| Modify: `jarvis-cloud/app/api/stream/[deviceId]/route.ts` | Accumulate Hive events into Redis |

### Sub-Project C: Portfolio Page (jarvis-portfolio)

| File | Responsibility |
|------|----------------|
| Create: `jarvis-portfolio/app/hive/page.tsx` | Public Hive summary page |
| Create: `jarvis-portfolio/components/hive/CognitiveStateIndicator.tsx` | Animated state display |
| Create: `jarvis-portfolio/components/hive/ThreadSummaryCard.tsx` | Thread card (title + outcome) |
| Create: `jarvis-portfolio/components/hive/HiveStats.tsx` | Today's stats bar |
| Create: `jarvis-portfolio/lib/hive-api.ts` | Fetch wrapper for summary endpoint |

---

## Task 1: Hive Data Models (Swift)

**Files:**
- Create: `JARVIS-Apple/JARVISHUD/Models/HiveModels.swift`

**Note:** Swift/SwiftUI doesn't have unit test infrastructure in this project (no XCTest target configured). We verify by building the Xcode project. All SwiftUI tasks use build verification instead of unit tests.

- [ ] **1.1: Create Models directory**

```bash
mkdir -p /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/JARVIS-Apple/JARVISHUD/Models
```

- [ ] **1.2: Create HiveModels.swift**

```swift
// JARVISHUD/Models/HiveModels.swift
// Data models for the Autonomous Engineering Hive feed.
// Mirrors Python backend/hive/thread_models.py for IPC compatibility.

import Foundation

// MARK: - Cognitive State

enum CognitiveStateValue: String, Codable {
    case baseline
    case rem
    case flow
    
    var displayName: String {
        switch self {
        case .baseline: return "BASELINE"
        case .rem: return "REM CYCLE"
        case .flow: return "FLOW STATE"
        }
    }
    
    var color: HiveColor {
        switch self {
        case .baseline: return .cyan
        case .rem: return .purple
        case .flow: return .orange
        }
    }
    
    var icon: String {
        switch self {
        case .baseline: return "circle.fill"
        case .rem: return "moon.fill"
        case .flow: return "flame.fill"
        }
    }
}

// MARK: - Thread State

enum ThreadStateValue: String, Codable {
    case open
    case debating
    case consensus
    case executing
    case resolved
    case stale
    
    var isActive: Bool {
        switch self {
        case .resolved, .stale: return false
        default: return true
        }
    }
    
    var color: HiveColor {
        switch self {
        case .open: return .gray
        case .debating: return .orange
        case .consensus: return .green
        case .executing: return .purple
        case .resolved: return .blue
        case .stale: return .red
        }
    }
}

// MARK: - Hive Colors (hex values matching design spec)

enum HiveColor {
    case cyan, purple, orange, red, blue, green, gray, lightBlue
    
    var hex: String {
        switch self {
        case .cyan: return "#22d3ee"
        case .purple: return "#a78bfa"
        case .orange: return "#f97316"
        case .red: return "#ef4444"
        case .blue: return "#3b82f6"
        case .green: return "#4ade80"
        case .gray: return "#64748b"
        case .lightBlue: return "#38bdf8"
        }
    }
}

// MARK: - Persona

enum Persona: String, Codable {
    case jarvis
    case j_prime
    case reactor
    
    var displayName: String {
        switch self {
        case .jarvis: return "JARVIS"
        case .j_prime: return "J-Prime"
        case .reactor: return "Reactor Core"
        }
    }
    
    var roleName: String {
        switch self {
        case .jarvis: return "The Body / Senses"
        case .j_prime: return "The Mind / Cognition"
        case .reactor: return "The Immune System"
        }
    }
    
    var color: HiveColor {
        switch self {
        case .jarvis: return .cyan
        case .j_prime: return .purple
        case .reactor: return .red
        }
    }
    
    var abbreviation: String {
        switch self {
        case .jarvis: return "J"
        case .j_prime: return "JP"
        case .reactor: return "RC"
        }
    }
}

// MARK: - Messages

struct AgentLogData: Codable, Identifiable {
    let messageId: String
    let threadId: String
    let agentName: String
    let trinityParent: String
    let severity: String
    let category: String
    let payload: [String: CodableValue]
    let ts: String
    
    var id: String { messageId }
    
    enum CodingKeys: String, CodingKey {
        case messageId = "message_id"
        case threadId = "thread_id"
        case agentName = "agent_name"
        case trinityParent = "trinity_parent"
        case severity, category, payload, ts
    }
}

struct PersonaReasoningData: Codable, Identifiable {
    let messageId: String
    let threadId: String
    let persona: String
    let role: String
    let intent: String
    let reasoning: String
    let confidence: Double
    let modelUsed: String
    let tokenCost: Int
    let validateVerdict: String?
    let manifestoPrinciple: String?
    let ts: String
    
    var id: String { messageId }
    
    var personaEnum: Persona? { Persona(rawValue: persona) }
    
    enum CodingKeys: String, CodingKey {
        case messageId = "message_id"
        case threadId = "thread_id"
        case persona, role, intent, reasoning, confidence
        case modelUsed = "model_used"
        case tokenCost = "token_cost"
        case validateVerdict = "validate_verdict"
        case manifestoPrinciple = "manifesto_principle"
        case ts
    }
}

enum HiveMessage: Identifiable {
    case agentLog(AgentLogData)
    case personaReasoning(PersonaReasoningData)
    
    var id: String {
        switch self {
        case .agentLog(let d): return d.messageId
        case .personaReasoning(let d): return d.messageId
        }
    }
    
    var timestamp: String {
        switch self {
        case .agentLog(let d): return d.ts
        case .personaReasoning(let d): return d.ts
        }
    }
}

// MARK: - Thread

struct HiveThread: Identifiable {
    let id: String
    var title: String
    var state: ThreadStateValue
    var messages: [HiveMessage]
    var tokensConsumed: Int
    var tokenBudget: Int
    var linkedOpId: String?
    var lastActivityAt: Date
    
    var isActive: Bool { state.isActive }
    
    var agentLogCount: Int {
        messages.filter { if case .agentLog = $0 { return true }; return false }.count
    }
    
    var personaMessageCount: Int {
        messages.filter { if case .personaReasoning = $0 { return true }; return false }.count
    }
}

// MARK: - Codable helpers

/// Simple JSON value wrapper for payload dictionaries.
enum CodableValue: Codable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case null
    
    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let v = try? container.decode(String.self) { self = .string(v) }
        else if let v = try? container.decode(Int.self) { self = .int(v) }
        else if let v = try? container.decode(Double.self) { self = .double(v) }
        else if let v = try? container.decode(Bool.self) { self = .bool(v) }
        else { self = .null }
    }
    
    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let v): try container.encode(v)
        case .int(let v): try container.encode(v)
        case .double(let v): try container.encode(v)
        case .bool(let v): try container.encode(v)
        case .null: try container.encodeNil()
        }
    }
    
    var stringValue: String {
        switch self {
        case .string(let v): return v
        case .int(let v): return "\(v)"
        case .double(let v): return String(format: "%.1f", v)
        case .bool(let v): return v ? "true" : "false"
        case .null: return "null"
        }
    }
}

// MARK: - IPC Event Parsing

struct HiveEventParser {
    
    static func parseMessage(eventType: String, data: [String: Any]) -> (threadId: String, message: HiveMessage)? {
        guard let jsonData = try? JSONSerialization.data(withJSONObject: data) else { return nil }
        
        switch eventType {
        case "agent_log":
            guard let decoded = try? JSONDecoder().decode(AgentLogData.self, from: jsonData) else { return nil }
            return (decoded.threadId, .agentLog(decoded))
            
        case "persona_reasoning":
            guard let decoded = try? JSONDecoder().decode(PersonaReasoningData.self, from: jsonData) else { return nil }
            return (decoded.threadId, .personaReasoning(decoded))
            
        default:
            return nil
        }
    }
    
    static func parseThreadLifecycle(data: [String: Any]) -> (threadId: String, state: ThreadStateValue)? {
        guard let threadId = data["thread_id"] as? String,
              let stateStr = data["state"] as? String,
              let state = ThreadStateValue(rawValue: stateStr) else { return nil }
        return (threadId, state)
    }
    
    static func parseCognitiveTransition(data: [String: Any]) -> CognitiveStateValue? {
        guard let toState = data["to_state"] as? String else { return nil }
        return CognitiveStateValue(rawValue: toState)
    }
}
```

- [ ] **1.3: Verify build**

Open Xcode, build the JARVISHUD target. The file should compile without errors. If the project uses XcodeGen, run `xcodegen generate` first.

- [ ] **1.4: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
git add JARVIS-Apple/JARVISHUD/Models/HiveModels.swift
git commit -m "feat(hive-hud): add Swift data models for Hive feed"
```

---

## Task 2: HiveStore (@Observable)

**Files:**
- Create: `JARVIS-Apple/JARVISHUD/Services/HiveStore.swift`

- [ ] **2.1: Create HiveStore.swift**

```swift
// JARVISHUD/Services/HiveStore.swift
// @Observable store for Hive events received via IPC.
// Fans out from BrainstemLauncher alongside AppState (PythonBridge).

import Foundation
import Observation

@Observable
class HiveStore {
    
    // MARK: - Published State
    
    var cognitiveState: CognitiveStateValue = .baseline
    var threads: [HiveThread] = []
    
    var activeThreadCount: Int {
        threads.filter(\.isActive).count
    }
    
    var sortedThreads: [HiveThread] {
        threads.sorted { $0.lastActivityAt > $1.lastActivityAt }
    }
    
    // MARK: - Event Handling (called from BrainstemLauncher)
    
    @MainActor
    func handleEvent(eventType: String, data: [String: Any]) {
        switch eventType {
        case "agent_log", "persona_reasoning":
            handleMessage(eventType: eventType, data: data)
        case "thread_lifecycle":
            handleThreadLifecycle(data: data)
        case "cognitive_transition":
            handleCognitiveTransition(data: data)
        default:
            break
        }
    }
    
    // MARK: - Private Handlers
    
    @MainActor
    private func handleMessage(eventType: String, data: [String: Any]) {
        guard let (threadId, message) = HiveEventParser.parseMessage(eventType: eventType, data: data) else { return }
        
        if let index = threads.firstIndex(where: { $0.id == threadId }) {
            threads[index].messages.append(message)
            threads[index].lastActivityAt = Date()
            if case .personaReasoning(let pr) = message {
                threads[index].tokensConsumed += pr.tokenCost
            }
        } else {
            // Thread not seen yet — create placeholder from first message
            let title = (data["category"] as? String)?.replacingOccurrences(of: "_", with: " ").capitalized ?? "Thread \(threadId.suffix(6))"
            var thread = HiveThread(
                id: threadId,
                title: title,
                state: .open,
                messages: [message],
                tokensConsumed: 0,
                tokenBudget: 50000,
                linkedOpId: nil,
                lastActivityAt: Date()
            )
            if case .personaReasoning(let pr) = message {
                thread.tokensConsumed = pr.tokenCost
            }
            threads.append(thread)
        }
    }
    
    @MainActor
    private func handleThreadLifecycle(data: [String: Any]) {
        guard let (threadId, state) = HiveEventParser.parseThreadLifecycle(data: data) else { return }
        
        if let index = threads.firstIndex(where: { $0.id == threadId }) {
            threads[index].state = state
            threads[index].lastActivityAt = Date()
            if let opId = data["linked_op_id"] as? String {
                threads[index].linkedOpId = opId
            }
        } else {
            // Thread lifecycle arrived before any messages — create placeholder
            let title = (data["title"] as? String) ?? "Thread \(threadId.suffix(6))"
            threads.append(HiveThread(
                id: threadId,
                title: title,
                state: state,
                messages: [],
                tokensConsumed: 0,
                tokenBudget: 50000,
                linkedOpId: data["linked_op_id"] as? String,
                lastActivityAt: Date()
            ))
        }
    }
    
    @MainActor
    private func handleCognitiveTransition(data: [String: Any]) {
        if let newState = HiveEventParser.parseCognitiveTransition(data: data) {
            cognitiveState = newState
        }
    }
}
```

- [ ] **2.2: Commit**

```bash
git add JARVIS-Apple/JARVISHUD/Services/HiveStore.swift
git commit -m "feat(hive-hud): add HiveStore @Observable for IPC event handling"
```

---

## Task 3: HiveView Components (SwiftUI)

**Files:**
- Create: `JARVIS-Apple/JARVISHUD/Views/ThreadStateBadge.swift`
- Create: `JARVIS-Apple/JARVISHUD/Views/CognitiveStateBar.swift`
- Create: `JARVIS-Apple/JARVISHUD/Views/HiveMessageRow.swift`
- Create: `JARVIS-Apple/JARVISHUD/Views/HiveThreadCard.swift`
- Create: `JARVIS-Apple/JARVISHUD/Views/HiveView.swift`

- [ ] **3.1: Create ThreadStateBadge.swift**

```swift
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
```

- [ ] **3.2: Create CognitiveStateBar.swift**

```swift
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
```

- [ ] **3.3: Create HiveMessageRow.swift**

```swift
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

// MARK: - Color Extension

extension Color {
    init(hex: String) {
        let hex = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        let scanner = Scanner(string: hex)
        var rgbValue: UInt64 = 0
        scanner.scanHexInt64(&rgbValue)
        self.init(
            red: Double((rgbValue & 0xFF0000) >> 16) / 255.0,
            green: Double((rgbValue & 0x00FF00) >> 8) / 255.0,
            blue: Double(rgbValue & 0x0000FF) / 255.0
        )
    }
}
```

- [ ] **3.4: Create HiveThreadCard.swift**

```swift
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
```

- [ ] **3.5: Create HiveView.swift**

```swift
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
```

- [ ] **3.6: Commit**

```bash
git add JARVIS-Apple/JARVISHUD/Views/ThreadStateBadge.swift \
        JARVIS-Apple/JARVISHUD/Views/CognitiveStateBar.swift \
        JARVIS-Apple/JARVISHUD/Views/HiveMessageRow.swift \
        JARVIS-Apple/JARVISHUD/Views/HiveThreadCard.swift \
        JARVIS-Apple/JARVISHUD/Views/HiveView.swift
git commit -m "feat(hive-hud): add SwiftUI HiveView components (thread cards, message rows, state bar)"
```

---

## Task 4: Wire HiveView into HUD

**Files:**
- Modify: `JARVIS-Apple/JARVISHUD/Views/HUDView.swift`
- Modify: `JARVIS-Apple/JARVISHUD/JARVISHUDApp.swift`
- Modify: `JARVIS-Apple/JARVISHUD/Services/BrainstemLauncher.swift`

This task modifies existing files. The implementer MUST read these files first to find exact insertion points.

- [ ] **4.1: Add segmented control to HUDView**

Read `HUDView.swift`. Find the `@State` variable declarations (around line 35-48). Add:

```swift
@State private var hudTab: HUDTab = .chat

enum HUDTab: String, CaseIterable {
    case chat = "Chat"
    case hive = "Hive"
}
```

Find where the transcript/content area is rendered (the ScrollView with transcript messages). Wrap it in a conditional based on `hudTab`, and add a Picker above it:

```swift
// Add segmented picker after the title/subtitle section, before Arc Reactor or content
Picker("", selection: $hudTab) {
    ForEach(HUDTab.allCases, id: \.self) { tab in
        Text(tab.rawValue).tag(tab)
    }
}
.pickerStyle(.segmented)
.padding(.horizontal, 20)
.frame(maxWidth: 200)

// Swap content based on tab
if hudTab == .chat {
    // ... existing transcript content ...
} else {
    HiveView(hiveStore: hiveStore)
}
```

Add `let hiveStore: HiveStore` as a parameter or environment object to HUDView. The exact wiring depends on how HUDView currently receives AppState — follow the same pattern.

- [ ] **4.2: Add HiveStore to app environment**

Read `JARVISHUDApp.swift`. Find where `AppState` or `PythonBridge` is created and injected. Create a `HiveStore` instance alongside it and pass it to HUDView.

```swift
// In JARVISHUDApp or wherever the window content is created:
let hiveStore = HiveStore()
// Pass to HUDView
HUDView(..., hiveStore: hiveStore)
```

- [ ] **4.3: Route IPC events to HiveStore**

Read `BrainstemLauncher.swift`. Find where IPC events are received and dispatched (the NWConnection data handler that parses JSON `{"event_type": ..., "data": ...}`). Add routing for Hive event types:

```swift
// After parsing event_type and data from IPC JSON:
let hiveEventTypes = ["agent_log", "persona_reasoning", "thread_lifecycle", "cognitive_transition"]
if hiveEventTypes.contains(eventType) {
    Task { @MainActor in
        hiveStore.handleEvent(eventType: eventType, data: data)
    }
    return  // Don't pass to AppState
}
// ... existing event dispatch to AppState ...
```

The `hiveStore` reference needs to be accessible from BrainstemLauncher. Since BrainstemLauncher is a singleton (`BrainstemLauncher.shared`), add a `var hiveStore: HiveStore?` property and set it during app initialization.

- [ ] **4.4: Verify build in Xcode**

Build and run JARVISHUD target. The segmented control should appear. Switching to "Hive" tab should show the empty state. If the brainstem is running with Hive enabled, events should populate.

- [ ] **4.5: Commit**

```bash
git add JARVIS-Apple/JARVISHUD/Views/HUDView.swift \
        JARVIS-Apple/JARVISHUD/JARVISHUDApp.swift \
        JARVIS-Apple/JARVISHUD/Services/BrainstemLauncher.swift
git commit -m "feat(hive-hud): wire HiveView into HUD with segmented control + IPC routing"
```

---

## Task 5: API Bridge (jarvis-cloud)

**Files:**
- Create: `jarvis-cloud/lib/hive/hive-state.ts`
- Create: `jarvis-cloud/app/api/hive/summary/route.ts`
- Modify: `jarvis-cloud/app/api/stream/[deviceId]/route.ts`

- [ ] **5.1: Create hive-state.ts**

```typescript
// jarvis-cloud/lib/hive/hive-state.ts
// Redis-backed Hive state accumulator.
// Tracks cognitive state, active threads, and resolved threads from SSE events.

import { Redis } from "@upstash/redis";

const redis = Redis.fromEnv();

const HIVE_STATE_KEY = "hive:state";
const HIVE_THREADS_KEY = "hive:threads";
const HIVE_RESOLVED_KEY = "hive:resolved";

export interface HiveThreadSummary {
  title: string;
  state: string;
  resolved_at?: string;
  outcome?: string;
}

export interface HiveSummary {
  cognitive_state: string;
  active_threads: HiveThreadSummary[];
  recent_resolved: HiveThreadSummary[];
  stats: {
    total_threads_today: number;
    tokens_consumed_today: number;
    debates_resolved_today: number;
  };
}

export async function getHiveSummary(): Promise<HiveSummary> {
  const [stateRaw, threadsRaw, resolvedRaw] = await Promise.all([
    redis.get<string>(HIVE_STATE_KEY),
    redis.hgetall<Record<string, string>>(HIVE_THREADS_KEY),
    redis.lrange(HIVE_RESOLVED_KEY, 0, 4),
  ]);

  const cognitiveState = stateRaw || "baseline";

  const activeThreads: HiveThreadSummary[] = [];
  let totalToday = 0;
  let tokensToday = 0;

  if (threadsRaw) {
    for (const [, value] of Object.entries(threadsRaw)) {
      try {
        const thread = JSON.parse(value) as HiveThreadSummary & { tokens_consumed?: number };
        if (thread.state && !["resolved", "stale"].includes(thread.state)) {
          activeThreads.push({ title: thread.title, state: thread.state });
        }
        totalToday++;
        tokensToday += thread.tokens_consumed || 0;
      } catch { /* skip corrupt entries */ }
    }
  }

  const recentResolved: HiveThreadSummary[] = [];
  let debatesResolved = 0;
  if (resolvedRaw) {
    for (const item of resolvedRaw) {
      try {
        const thread = (typeof item === "string" ? JSON.parse(item) : item) as HiveThreadSummary;
        recentResolved.push(thread);
        debatesResolved++;
      } catch { /* skip */ }
    }
  }

  return {
    cognitive_state: cognitiveState,
    active_threads: activeThreads,
    recent_resolved: recentResolved,
    stats: {
      total_threads_today: totalToday,
      tokens_consumed_today: tokensToday,
      debates_resolved_today: debatesResolved,
    },
  };
}

export async function accumulateHiveEvent(eventType: string, data: Record<string, unknown>): Promise<void> {
  switch (eventType) {
    case "cognitive_transition": {
      const toState = data.to_state as string;
      if (toState) {
        await redis.set(HIVE_STATE_KEY, toState, { ex: 86400 });
      }
      break;
    }
    case "thread_lifecycle": {
      const threadId = data.thread_id as string;
      const state = data.state as string;
      const title = (data.title as string) || `Thread ${threadId?.slice(-6)}`;
      if (threadId && state) {
        const summary = JSON.stringify({ title, state, tokens_consumed: 0 });
        await redis.hset(HIVE_THREADS_KEY, { [threadId]: summary });
        if (state === "resolved" || state === "stale") {
          const resolved = JSON.stringify({
            title,
            state,
            outcome: state === "resolved" ? "pr_opened" : "stale",
            resolved_at: new Date().toISOString(),
          });
          await redis.lpush(HIVE_RESOLVED_KEY, resolved);
          await redis.ltrim(HIVE_RESOLVED_KEY, 0, 19);
          await redis.hdel(HIVE_THREADS_KEY, threadId);
        }
      }
      break;
    }
    case "persona_reasoning": {
      const threadId = data.thread_id as string;
      const tokenCost = (data.token_cost as number) || 0;
      if (threadId && tokenCost > 0) {
        const existing = await redis.hget<string>(HIVE_THREADS_KEY, threadId);
        if (existing) {
          try {
            const parsed = JSON.parse(existing) as { tokens_consumed?: number; [k: string]: unknown };
            parsed.tokens_consumed = (parsed.tokens_consumed || 0) + tokenCost;
            await redis.hset(HIVE_THREADS_KEY, { [threadId]: JSON.stringify(parsed) });
          } catch { /* skip */ }
        }
      }
      break;
    }
  }
}
```

- [ ] **5.2: Create summary route**

```typescript
// jarvis-cloud/app/api/hive/summary/route.ts
// Public endpoint — no auth required. Returns sanitized Hive summary.

import { NextResponse } from "next/server";
import { getHiveSummary } from "@/lib/hive/hive-state";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const summary = await getHiveSummary();
    return NextResponse.json(summary, {
      headers: {
        "Cache-Control": "public, s-maxage=10, stale-while-revalidate=20",
        "Access-Control-Allow-Origin": "*",
      },
    });
  } catch (error) {
    console.error("[hive/summary] Error:", error);
    return NextResponse.json(
      { cognitive_state: "baseline", active_threads: [], recent_resolved: [], stats: { total_threads_today: 0, tokens_consumed_today: 0, debates_resolved_today: 0 } },
      { status: 200 }
    );
  }
}
```

- [ ] **5.3: Wire accumulator into SSE stream**

Read `jarvis-cloud/app/api/stream/[deviceId]/route.ts`. Find where events are parsed from Redis XRANGE and sent to the SSE client. After parsing each event, call `accumulateHiveEvent` for Hive event types:

```typescript
import { accumulateHiveEvent } from "@/lib/hive/hive-state";

// Inside the event processing loop, after parsing:
const hiveEventTypes = ["agent_log", "persona_reasoning", "thread_lifecycle", "cognitive_transition"];
if (hiveEventTypes.includes(parsed.event)) {
  accumulateHiveEvent(parsed.event, parsed.data).catch(() => {});  // fire-and-forget
}
```

- [ ] **5.4: Create directories and commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
mkdir -p jarvis-cloud/lib/hive
mkdir -p jarvis-cloud/app/api/hive/summary
git add jarvis-cloud/lib/hive/hive-state.ts \
        jarvis-cloud/app/api/hive/summary/route.ts \
        jarvis-cloud/app/api/stream/\[deviceId\]/route.ts
git commit -m "feat(hive-cloud): add API bridge — Redis state accumulator + public summary endpoint"
```

---

## Task 6: Portfolio Hive Page

**Files:**
- Create: `jarvis-portfolio/lib/hive-api.ts`
- Create: `jarvis-portfolio/components/hive/CognitiveStateIndicator.tsx`
- Create: `jarvis-portfolio/components/hive/ThreadSummaryCard.tsx`
- Create: `jarvis-portfolio/components/hive/HiveStats.tsx`
- Create: `jarvis-portfolio/app/hive/page.tsx`

- [ ] **6.1: Create hive-api.ts**

```typescript
// jarvis-portfolio/lib/hive-api.ts
// Fetch wrapper for the Hive summary API.

const HIVE_API_URL = process.env.NEXT_PUBLIC_JARVIS_CLOUD_URL
  ? `${process.env.NEXT_PUBLIC_JARVIS_CLOUD_URL}/api/hive/summary`
  : "https://jarvis-cloud-five.vercel.app/api/hive/summary";

export interface HiveThreadSummary {
  title: string;
  state: string;
  resolved_at?: string;
  outcome?: string;
}

export interface HiveSummary {
  cognitive_state: string;
  active_threads: HiveThreadSummary[];
  recent_resolved: HiveThreadSummary[];
  stats: {
    total_threads_today: number;
    tokens_consumed_today: number;
    debates_resolved_today: number;
  };
}

export async function fetchHiveSummary(): Promise<HiveSummary> {
  const res = await fetch(HIVE_API_URL, { next: { revalidate: 30 } });
  if (!res.ok) {
    return {
      cognitive_state: "baseline",
      active_threads: [],
      recent_resolved: [],
      stats: { total_threads_today: 0, tokens_consumed_today: 0, debates_resolved_today: 0 },
    };
  }
  return res.json();
}
```

- [ ] **6.2: Create CognitiveStateIndicator.tsx**

```tsx
// jarvis-portfolio/components/hive/CognitiveStateIndicator.tsx
"use client";

const STATE_CONFIG: Record<string, { label: string; color: string; icon: string; glow: string }> = {
  baseline: { label: "BASELINE", color: "#22d3ee", icon: "●", glow: "rgba(34,211,238,0.3)" },
  rem: { label: "REM CYCLE", color: "#a78bfa", icon: "☽", glow: "rgba(167,139,250,0.3)" },
  flow: { label: "FLOW STATE", color: "#f97316", icon: "🔥", glow: "rgba(249,115,22,0.3)" },
};

export function CognitiveStateIndicator({ state }: { state: string }) {
  const config = STATE_CONFIG[state] || STATE_CONFIG.baseline;

  return (
    <div
      className="flex items-center gap-3 px-4 py-3 rounded-lg border"
      style={{
        backgroundColor: `${config.color}08`,
        borderColor: `${config.color}30`,
        boxShadow: `0 0 20px ${config.glow}`,
      }}
    >
      <span className="text-xl animate-pulse">{config.icon}</span>
      <span
        className="font-mono font-bold text-sm tracking-wider"
        style={{ color: config.color }}
      >
        {config.label}
      </span>
    </div>
  );
}
```

- [ ] **6.3: Create ThreadSummaryCard.tsx**

```tsx
// jarvis-portfolio/components/hive/ThreadSummaryCard.tsx

const STATE_COLORS: Record<string, string> = {
  open: "#64748b",
  debating: "#f97316",
  consensus: "#4ade80",
  executing: "#a78bfa",
  resolved: "#3b82f6",
  stale: "#ef4444",
};

interface ThreadSummaryCardProps {
  title: string;
  state: string;
  outcome?: string;
  resolvedAt?: string;
}

export function ThreadSummaryCard({ title, state, outcome, resolvedAt }: ThreadSummaryCardProps) {
  const color = STATE_COLORS[state] || "#64748b";

  return (
    <div
      className="flex items-center justify-between px-4 py-3 rounded-lg border"
      style={{ borderColor: `${color}30`, backgroundColor: `${color}08` }}
    >
      <div className="flex items-center gap-3">
        <span
          className="px-2 py-0.5 rounded text-[10px] font-mono font-bold uppercase"
          style={{ backgroundColor: `${color}20`, color }}
        >
          {state}
        </span>
        <span className="text-sm text-gray-300">{title}</span>
      </div>
      {resolvedAt && (
        <span className="text-xs text-gray-500">
          {new Date(resolvedAt).toLocaleDateString()}
        </span>
      )}
    </div>
  );
}
```

- [ ] **6.4: Create HiveStats.tsx**

```tsx
// jarvis-portfolio/components/hive/HiveStats.tsx

interface HiveStatsProps {
  totalThreads: number;
  tokensConsumed: number;
  debatesResolved: number;
}

export function HiveStats({ totalThreads, tokensConsumed, debatesResolved }: HiveStatsProps) {
  return (
    <div className="grid grid-cols-3 gap-4">
      <StatCard label="Threads Today" value={totalThreads} />
      <StatCard label="Tokens Used" value={tokensConsumed.toLocaleString()} />
      <StatCard label="Debates Resolved" value={debatesResolved} />
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="text-center px-4 py-3 rounded-lg bg-white/5 border border-white/10">
      <div className="text-2xl font-mono font-bold text-white">{value}</div>
      <div className="text-xs text-gray-400 mt-1">{label}</div>
    </div>
  );
}
```

- [ ] **6.5: Create portfolio hive page**

```tsx
// jarvis-portfolio/app/hive/page.tsx
import { fetchHiveSummary } from "@/lib/hive-api";
import { CognitiveStateIndicator } from "@/components/hive/CognitiveStateIndicator";
import { ThreadSummaryCard } from "@/components/hive/ThreadSummaryCard";
import { HiveStats } from "@/components/hive/HiveStats";

export const revalidate = 30;

export default async function HivePage() {
  const summary = await fetchHiveSummary();

  return (
    <main className="min-h-screen bg-[#0a0a0f] text-white">
      <div className="max-w-2xl mx-auto px-6 py-16">
        {/* Header */}
        <div className="text-center mb-12">
          <h1 className="text-3xl font-bold mb-2">
            <span className="text-cyan-400">JARVIS</span>{" "}
            <span className="text-purple-400">Autonomous Engineering Hive</span>
          </h1>
          <p className="text-gray-400 text-sm">
            Live view of the Trinity AI ecosystem&apos;s self-evolving intelligence
          </p>
        </div>

        {/* Cognitive State */}
        <div className="mb-8">
          <CognitiveStateIndicator state={summary.cognitive_state} />
        </div>

        {/* Stats */}
        <div className="mb-8">
          <HiveStats
            totalThreads={summary.stats.total_threads_today}
            tokensConsumed={summary.stats.tokens_consumed_today}
            debatesResolved={summary.stats.debates_resolved_today}
          />
        </div>

        {/* Active Threads */}
        {summary.active_threads.length > 0 && (
          <section className="mb-8">
            <h2 className="text-sm font-mono text-gray-400 uppercase tracking-wider mb-3">
              Active Debates
            </h2>
            <div className="space-y-2">
              {summary.active_threads.map((thread, i) => (
                <ThreadSummaryCard key={i} title={thread.title} state={thread.state} />
              ))}
            </div>
          </section>
        )}

        {/* Recent Resolved */}
        {summary.recent_resolved.length > 0 && (
          <section className="mb-8">
            <h2 className="text-sm font-mono text-gray-400 uppercase tracking-wider mb-3">
              Recently Resolved
            </h2>
            <div className="space-y-2">
              {summary.recent_resolved.map((thread, i) => (
                <ThreadSummaryCard
                  key={i}
                  title={thread.title}
                  state={thread.state}
                  outcome={thread.outcome}
                  resolvedAt={thread.resolved_at}
                />
              ))}
            </div>
          </section>
        )}

        {/* Footer */}
        <footer className="text-center text-xs text-gray-500 mt-16">
          Powered by the{" "}
          <span className="text-purple-400">Symbiotic AI-Native Manifesto v4</span>
          {" "}— The Trinity Ecosystem of JARVIS
        </footer>
      </div>
    </main>
  );
}
```

- [ ] **6.6: Create directories and commit**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-portfolio
mkdir -p components/hive lib app/hive
git add lib/hive-api.ts \
        components/hive/CognitiveStateIndicator.tsx \
        components/hive/ThreadSummaryCard.tsx \
        components/hive/HiveStats.tsx \
        app/hive/page.tsx
git commit -m "feat(portfolio): add Hive public summary page"
```

---

## Summary

| Task | Sub-Project | Component | Dependencies |
|------|------------|-----------|-------------|
| 1 | SwiftUI | Hive Data Models (Swift) | None |
| 2 | SwiftUI | HiveStore (@Observable) | Task 1 |
| 3 | SwiftUI | HiveView Components | Task 1 |
| 4 | SwiftUI | Wire into HUD | Tasks 2+3 |
| 5 | jarvis-cloud | API Bridge (Redis + endpoint) | None |
| 6 | jarvis-portfolio | Portfolio Hive Page | Task 5 |

**Parallelization:** Tasks 1-3 (SwiftUI) and Task 5 (API bridge) can run in parallel since they're different repos. Task 4 depends on 2+3. Task 6 depends on 5.
