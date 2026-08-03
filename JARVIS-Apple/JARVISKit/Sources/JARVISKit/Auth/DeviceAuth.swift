import Foundation
import CryptoKit

public final class DeviceAuth: Sendable {
    public let deviceId: String
    public let deviceType: DeviceType
    private let secretKey: SymmetricKey

    /// Fails when `deviceSecret` is not a usable hex-encoded key.
    ///
    /// FAILABLE ON PURPOSE. The previous initialiser could not express "that is
    /// not a secret": it took a `String` and had to produce a key, so a
    /// malformed value left it two options — trap, or fabricate. It did both,
    /// depending on the input, and trapping killed the app at launch.
    ///
    /// Returning nil moves the decision to the caller, which is the only place
    /// that knows what to do about it. A HUD can show "credentials are
    /// malformed" and stay up; a paired phone can send the user back to
    /// Settings. Neither outcome is available to an initialiser that can only
    /// succeed or die.
    public init?(deviceId: String, deviceType: DeviceType, deviceSecret: String) {
        guard let bytes = Self.decodeHexSecret(deviceSecret), !bytes.isEmpty else {
            return nil
        }
        self.deviceId = deviceId
        self.deviceType = deviceType
        self.secretKey = SymmetricKey(data: Data(bytes))
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

    /// Decode a hex-encoded shared secret. Returns nil if it is not one.
    ///
    /// THE CRASH THIS REPLACES
    /// -------------------------
    /// The previous version walked the string two characters at a time with
    /// `hex.index(index, offsetBy: 2)`. On an ODD-LENGTH string the final step
    /// runs past `endIndex`, and `String.index(_:offsetBy:)` traps — taking the
    /// whole process with it. The HUD's own local-first default secret was the
    /// literal `"local"`: five characters, so **the app killed itself at boot
    /// with `Fatal error: String index is out of bounds` every single launch**
    /// unless `JARVIS_DEVICE_SECRET` happened to be set.
    ///
    /// A CRLF `.env` file reaches the same place from another direction: the
    /// loader splits on `"\n"` and trims only `.whitespaces`, so a 64-character
    /// secret arrives as 65 characters with a trailing `\r`.
    ///
    /// Striding over an `Array<Character>` by integer index cannot express that
    /// bug — there is no cursor to advance past the end, and the count is
    /// checked before any indexing happens.
    ///
    /// WHY IT REFUSES INSTEAD OF SKIPPING
    /// ------------------------------------
    /// The old loop did `if let byte = UInt8(pair, radix: 16)` and SILENTLY
    /// DROPPED anything that failed to parse. That is worse than the crash it
    /// sat next to: `"local"` would have yielded a single byte (`0xCA`, from
    /// the pair `"ca"`) and produced a perfectly usable `SymmetricKey` that
    /// could never match the server's. Every request would fail authentication
    /// with nothing anywhere saying why.
    ///
    /// The secret is hex by contract on all three implementations — Python uses
    /// `bytes.fromhex`, TypeScript uses `Buffer.from(secret, "hex")` — so
    /// anything that is not hex is a configuration error, and the only honest
    /// response is to say so rather than to improvise a key.
    ///
    /// It DOES normalise what is merely untidy rather than wrong: surrounding
    /// whitespace and newlines, an optional `0x` prefix, and either case. Those
    /// are transport artefacts of a `.env` file or a copy-paste, not corruption,
    /// and rejecting them would send an operator hunting for a bug in a secret
    /// that is perfectly correct.
    public static func decodeHexSecret(_ raw: String) -> [UInt8]? {
        var s = Substring(raw).trimmingCharacters(in: .whitespacesAndNewlines)
        if s.hasPrefix("0x") || s.hasPrefix("0X") { s = String(s.dropFirst(2)) }
        guard !s.isEmpty else { return nil }

        let chars = Array(s)
        guard chars.count % 2 == 0 else { return nil }

        var bytes: [UInt8] = []
        bytes.reserveCapacity(chars.count / 2)
        for i in stride(from: 0, to: chars.count, by: 2) {
            guard let hi = chars[i].hexDigitValue,
                  let lo = chars[i + 1].hexDigitValue else { return nil }
            bytes.append(UInt8(hi << 4 | lo))
        }
        return bytes
    }

    /// A well-formed secret for a loopback-trusted local backend.
    ///
    /// The HUD's local-first path connects to `localhost:8010`, whose
    /// `/api/command` handler performs no signature verification at all
    /// (checked, not assumed). The secret's VALUE is therefore irrelevant
    /// there; only its WELL-FORMEDNESS matters, because `DeviceAuth` must be
    /// constructible.
    ///
    /// Derived rather than hardcoded so there is no magic constant to drift,
    /// and deterministic per device id so it is stable across launches — which
    /// matters the day someone points the local path at a backend that DOES
    /// verify, because both sides can then compute the same value from the same
    /// input instead of one of them inventing a fresh one.
    public static func derivedLocalSecret(forDeviceId deviceId: String) -> String {
        let digest = SHA256.hash(data: Data("jarvis-local:\(deviceId)".utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }
}
