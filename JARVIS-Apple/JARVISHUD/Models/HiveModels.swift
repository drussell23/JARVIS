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
