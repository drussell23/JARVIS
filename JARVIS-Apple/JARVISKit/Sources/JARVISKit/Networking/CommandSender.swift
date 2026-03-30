import Foundation

public final class CommandSender: Sendable {
    private let baseURL: String
    private let auth: DeviceAuth

    public init(baseURL: String, auth: DeviceAuth) {
        self.baseURL = baseURL
        self.auth = auth
    }

    public func send(_ text: String, priority: Priority = .realtime, responseMode: ResponseMode = .stream, intentHint: String? = nil, context: CommandContext? = nil) async throws -> CommandResponse {
        var payload = CommandPayload(
            deviceId: auth.deviceId,
            deviceType: auth.deviceType,
            text: text,
            intentHint: intentHint,
            context: context,
            priority: priority,
            responseMode: responseMode
        )
        payload.signature = auth.sign(payload)

        let url = URL(string: "\(baseURL)/api/command")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let encoder = JSONEncoder()
        request.httpBody = try encoder.encode(payload)

        let (data, response) = try await URLSession.shared.data(for: request)
        let httpResponse = response as! HTTPURLResponse

        if httpResponse.statusCode == 401 {
            throw JARVISError.authFailed
        }

        let contentType = httpResponse.value(forHTTPHeaderField: "Content-Type") ?? ""
        if contentType.contains("text/event-stream") {
            return CommandResponse(status: "streaming", commandId: payload.commandId)
        }

        return try JSONDecoder().decode(CommandResponse.self, from: data)
    }
}

public struct CommandResponse: Codable, Sendable {
    public let status: String
    public let commandId: String?
    public let jobId: String?
    public let brain: String?

    public init(status: String, commandId: String? = nil, jobId: String? = nil, brain: String? = nil) {
        self.status = status
        self.commandId = commandId
        self.jobId = jobId
        self.brain = brain
    }

    enum CodingKeys: String, CodingKey {
        case status
        case commandId = "command_id"
        case jobId = "job_id"
        case brain
    }
}
