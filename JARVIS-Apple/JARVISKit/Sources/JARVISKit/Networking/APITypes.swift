import Foundation

// MARK: - Device Types

public enum DeviceType: String, Codable, Sendable {
    case watch, iphone, mac, browser
}

public enum Priority: String, Codable, Sendable {
    case realtime, background, deferred
}

public enum ResponseMode: String, Codable, Sendable {
    case stream, notify
}

// MARK: - Command Payload

public struct CommandPayload: Codable, Sendable {
    public let commandId: String
    public let deviceId: String
    public let deviceType: DeviceType
    public let text: String
    public var intentHint: String?
    public var context: CommandContext?
    public let priority: Priority
    public let responseMode: ResponseMode
    public let timestamp: String
    public var signature: String

    public init(
        commandId: String = UUID().uuidString,
        deviceId: String,
        deviceType: DeviceType,
        text: String,
        intentHint: String? = nil,
        context: CommandContext? = nil,
        priority: Priority = .realtime,
        responseMode: ResponseMode = .stream,
        timestamp: String = ISO8601DateFormatter().string(from: Date()),
        signature: String = ""
    ) {
        self.commandId = commandId
        self.deviceId = deviceId
        self.deviceType = deviceType
        self.text = text
        self.intentHint = intentHint
        self.context = context
        self.priority = priority
        self.responseMode = responseMode
        self.timestamp = timestamp
        self.signature = signature
    }

    enum CodingKeys: String, CodingKey {
        case commandId = "command_id"
        case deviceId = "device_id"
        case deviceType = "device_type"
        case text
        case intentHint = "intent_hint"
        case context
        case priority
        case responseMode = "response_mode"
        case timestamp
        case signature
    }
}

public struct CommandContext: Codable, Sendable {
    public var activeApp: String?
    public var activeFile: String?
    public var screenSummary: String?
    public var location: String?
    public var batteryLevel: Double?

    public init(activeApp: String? = nil, activeFile: String? = nil, screenSummary: String? = nil, location: String? = nil, batteryLevel: Double? = nil) {
        self.activeApp = activeApp
        self.activeFile = activeFile
        self.screenSummary = screenSummary
        self.location = location
        self.batteryLevel = batteryLevel
    }

    enum CodingKeys: String, CodingKey {
        case activeApp = "active_app"
        case activeFile = "active_file"
        case screenSummary = "screen_summary"
        case location
        case batteryLevel = "battery_level"
    }
}

// MARK: - SSE Events

public enum SSEEventType: String, Sendable {
    case token, action, daemon, status, complete, heartbeat, disconnect
}

public struct TokenEvent: Codable, Sendable {
    public let commandId: String
    public let token: String
    public let sourceBrain: String
    public let sequence: Int

    enum CodingKeys: String, CodingKey {
        case commandId = "command_id"
        case token
        case sourceBrain = "source_brain"
        case sequence
    }
}

public struct DaemonEvent: Codable, Sendable {
    public let commandId: String
    public let narrationText: String
    public let narrationPriority: String
    public let sourceBrain: String

    enum CodingKeys: String, CodingKey {
        case commandId = "command_id"
        case narrationText = "narration_text"
        case narrationPriority = "narration_priority"
        case sourceBrain = "source_brain"
    }
}

public struct StatusEvent: Codable, Sendable {
    public let commandId: String
    public let phase: String
    public let progress: Int?
    public let message: String

    enum CodingKeys: String, CodingKey {
        case commandId = "command_id"
        case phase, progress, message
    }
}

public struct CompleteEvent: Codable, Sendable {
    public let commandId: String
    public let sourceBrain: String
    public let tokenCount: Int?
    public let latencyMs: Int

    enum CodingKeys: String, CodingKey {
        case commandId = "command_id"
        case sourceBrain = "source_brain"
        case tokenCount = "token_count"
        case latencyMs = "latency_ms"
    }
}

// MARK: - Parsed SSE Event

public enum JARVISEvent: Sendable {
    case token(TokenEvent)
    case daemon(DaemonEvent)
    case status(StatusEvent)
    case complete(CompleteEvent)
    case action(commandId: String, actionType: String, payload: [String: String])
    case heartbeat
}
