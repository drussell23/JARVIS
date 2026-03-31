import Foundation

public final class SSEClient: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let baseURL: String
    private let deviceId: String
    private let tokenManager: StreamTokenManager
    private var task: URLSessionDataTask?
    private var session: URLSession?
    private let lock = NSLock()  // Protect buffer from concurrent delegate callbacks
    private var buffer = ""
    private var lastEventId: String?

    public var onEvent: (@Sendable (JARVISEvent) -> Void)?
    public var onConnect: (@Sendable () -> Void)?
    public var onDisconnect: (@Sendable () -> Void)?

    public init(baseURL: String, deviceId: String, tokenManager: StreamTokenManager) {
        self.baseURL = baseURL
        self.deviceId = deviceId
        self.tokenManager = tokenManager
    }

    public func connect() async throws {
        let token = try await tokenManager.getToken()
        guard var urlComponents = URLComponents(string: "\(baseURL)/api/stream/\(deviceId)") else {
            throw URLError(.badURL)
        }
        urlComponents.queryItems = [URLQueryItem(name: "t", value: token)]

        guard let url = urlComponents.url else {
            throw URLError(.badURL)
        }
        var request = URLRequest(url: url)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 310 // Slightly over Vercel's 300s max

        if let lastId = lastEventId {
            request.setValue(lastId, forHTTPHeaderField: "Last-Event-ID")
        }

        // Invalidate old session
        session?.finishTasksAndInvalidate()

        let newSession = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
        session = newSession
        task = newSession.dataTask(with: request)
        task?.resume()
    }

    public func disconnect() {
        task?.cancel()
        session?.finishTasksAndInvalidate()
        task = nil
        session = nil
    }

    // MARK: - URLSessionDataDelegate

    public func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard let chunk = String(data: data, encoding: .utf8) else { return }

        // Thread-safe: URLSession delegate fires on its own queue
        var events: [JARVISEvent] = []
        lock.lock()
        buffer += chunk
        while let range = buffer.range(of: "\n\n") {
            let block = String(buffer[buffer.startIndex..<range.lowerBound])
            buffer = String(buffer[range.upperBound...])
            if let event = parseBlock(block) {
                events.append(event)
            }
        }
        lock.unlock()

        for event in events {
            onEvent?(event)
        }
    }

    public func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: (any Error)?) {
        onDisconnect?()
    }

    // MARK: - SSE Parsing

    private func parseBlock(_ block: String) -> JARVISEvent? {
        var eventType: String?
        var dataLines: [String] = []
        var eventId: String?

        for line in block.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(line)
            if line.hasPrefix("id:") { eventId = String(line.dropFirst(3)) }
            else if line.hasPrefix("event:") { eventType = String(line.dropFirst(6)) }
            else if line.hasPrefix("data:") { dataLines.append(String(line.dropFirst(5))) }
        }

        if let id = eventId { lastEventId = id }
        guard let type = eventType else { return nil }
        let jsonString = dataLines.joined(separator: "\n")
        guard let jsonData = jsonString.data(using: .utf8) else { return nil }

        switch type {
        case "token":
            guard let event = try? JSONDecoder().decode(TokenEvent.self, from: jsonData) else { return nil }
            return .token(event)
        case "daemon":
            guard let event = try? JSONDecoder().decode(DaemonEvent.self, from: jsonData) else { return nil }
            return .daemon(event)
        case "status":
            guard let event = try? JSONDecoder().decode(StatusEvent.self, from: jsonData) else { return nil }
            return .status(event)
        case "complete":
            guard let event = try? JSONDecoder().decode(CompleteEvent.self, from: jsonData) else { return nil }
            return .complete(event)
        case "heartbeat":
            return .heartbeat
        case "action":
            // Parse action event: { command_id, action_type, payload: { goal, screenshot, ... } }
            guard let json = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any],
                  let commandId = json["command_id"] as? String,
                  let actionType = json["action_type"] as? String else { return nil }
            // Flatten payload dict to [String: String] for the event
            var payload: [String: String] = [:]
            if let p = json["payload"] as? [String: Any] {
                for (k, v) in p {
                    if let s = v as? String { payload[k] = s }
                }
            }
            return .action(commandId: commandId, actionType: actionType, payload: payload)
        default:
            return nil
        }
    }
}
