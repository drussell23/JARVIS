import Foundation

public actor StreamTokenManager {
    private let deviceId: String
    private let auth: DeviceAuth
    private let baseURL: String
    private var currentToken: String?

    public init(deviceId: String, auth: DeviceAuth, baseURL: String) {
        self.deviceId = deviceId
        self.auth = auth
        self.baseURL = baseURL
    }

    public func getToken() async throws -> String {
        let timestamp = ISO8601DateFormatter().string(from: Date())
        var payload = CommandPayload(
            commandId: "stream-token",
            deviceId: deviceId,
            deviceType: auth.deviceType,
            text: "stream-token-request",
            priority: .realtime,
            responseMode: .stream,
            timestamp: timestamp
        )
        payload.signature = auth.sign(payload)

        let url = URL(string: "\(baseURL)/api/stream/token")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(payload)

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw JARVISError.tokenRequestFailed
        }

        let result = try JSONDecoder().decode(StreamTokenResponse.self, from: data)
        currentToken = result.token
        return result.token
    }
}

struct StreamTokenResponse: Codable {
    let token: String
    let streamUrl: String

    enum CodingKeys: String, CodingKey {
        case token
        case streamUrl = "stream_url"
    }
}

public enum JARVISError: Error, Sendable {
    case notPaired
    case tokenRequestFailed
    case connectionFailed
    case authFailed
}
