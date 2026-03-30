import Foundation
import CryptoKit

public final class DeviceAuth: Sendable {
    public let deviceId: String
    public let deviceType: DeviceType
    private let secretKey: SymmetricKey

    public init(deviceId: String, deviceType: DeviceType, deviceSecret: String) {
        self.deviceId = deviceId
        self.deviceType = deviceType
        self.secretKey = SymmetricKey(data: Self.hexToBytes(deviceSecret))
    }

    /// Canonical field order — MUST match TypeScript server and Python brainstem
    private static let canonicalFields = [
        "command_id", "device_id", "device_type",
        "priority", "response_mode", "text", "timestamp",
    ]

    public func sign(_ payload: CommandPayload) -> String {
        let canonical = canonicalize(payload)
        let data = Data(canonical.utf8)
        let mac = HMAC<SHA256>.authenticationCode(for: data, using: secretKey)
        return Data(mac).map { String(format: "%02x", $0) }.joined()
    }

    public func canonicalize(_ payload: CommandPayload) -> String {
        var parts = Self.canonicalFields.map { field -> String in
            let value: String
            switch field {
            case "command_id": value = payload.commandId
            case "device_id": value = payload.deviceId
            case "device_type": value = payload.deviceType.rawValue
            case "priority": value = payload.priority.rawValue
            case "response_mode": value = payload.responseMode.rawValue
            case "text": value = payload.text
            case "timestamp": value = payload.timestamp
            default: value = ""
            }
            return "\(field)=\(value)"
        }

        // intent_hint at index 3 (between device_type and priority)
        if let hint = payload.intentHint, !hint.isEmpty {
            parts.insert("intent_hint=\(hint)", at: 3)
        }

        // context as sorted-key JSON — exclude screenshot from HMAC to avoid
        // serialization mismatches between Swift JSONEncoder and TypeScript JSON.stringify
        // on large binary payloads. Screenshot is visual context, not command identity.
        if var context = payload.context {
            context.screenshot = nil
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            if let data = try? encoder.encode(context),
               let json = String(data: data, encoding: .utf8) {
                parts.append("context=\(json)")
            }
        }

        return parts.joined(separator: "&")
    }

    private static func hexToBytes(_ hex: String) -> [UInt8] {
        var bytes: [UInt8] = []
        var index = hex.startIndex
        while index < hex.endIndex {
            let nextIndex = hex.index(index, offsetBy: 2)
            if let byte = UInt8(hex[index..<nextIndex], radix: 16) {
                bytes.append(byte)
            }
            index = nextIndex
        }
        return bytes
    }
}
