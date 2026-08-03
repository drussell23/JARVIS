import Testing
@testable import JARVISKit

@Test func canonicalizeMatchesSpec() throws {
    let auth = try #require(DeviceAuth(deviceId: "watch-ultra2-derek", deviceType: .watch, deviceSecret: String(repeating: "a", count: 64)))
    let payload = CommandPayload(
        commandId: "cmd-001",
        deviceId: "watch-ultra2-derek",
        deviceType: .watch,
        text: "refactor the auth module",
        priority: .realtime,
        responseMode: .stream,
        timestamp: "2026-03-29T18:45:00Z"
    )
    let canonical = auth.canonicalize(payload)
    #expect(canonical == "command_id=cmd-001&device_id=watch-ultra2-derek&device_type=watch&priority=realtime&response_mode=stream&text=refactor the auth module&timestamp=2026-03-29T18:45:00Z")
}

@Test func signProduces64CharHex() throws {
    let auth = try #require(DeviceAuth(deviceId: "mac-m1", deviceType: .mac, deviceSecret: String(repeating: "a", count: 64)))
    let payload = CommandPayload(
        commandId: "cmd-001", deviceId: "mac-m1", deviceType: .mac, text: "hello",
        priority: .realtime, responseMode: .stream, timestamp: "2026-03-29T18:45:00Z"
    )
    let sig = auth.sign(payload)
    #expect(sig.count == 64)
    #expect(sig.allSatisfy { "0123456789abcdef".contains($0) })
}

@Test func signIsDeterministic() throws {
    let auth = try #require(DeviceAuth(deviceId: "mac-m1", deviceType: .mac, deviceSecret: String(repeating: "a", count: 64)))
    let payload = CommandPayload(
        commandId: "cmd-001", deviceId: "mac-m1", deviceType: .mac, text: "hello",
        priority: .realtime, responseMode: .stream, timestamp: "2026-03-29T18:45:00Z"
    )
    #expect(auth.sign(payload) == auth.sign(payload))
}

@Test func signChangesWithDifferentText() throws {
    let auth = try #require(DeviceAuth(deviceId: "mac-m1", deviceType: .mac, deviceSecret: String(repeating: "a", count: 64)))
    let p1 = CommandPayload(commandId: "cmd-001", deviceId: "mac-m1", deviceType: .mac, text: "hello", priority: .realtime, responseMode: .stream, timestamp: "2026-03-29T18:45:00Z")
    let p2 = CommandPayload(commandId: "cmd-001", deviceId: "mac-m1", deviceType: .mac, text: "goodbye", priority: .realtime, responseMode: .stream, timestamp: "2026-03-29T18:45:00Z")
    #expect(auth.sign(p1) != auth.sign(p2))
}

@Test func canonicalizeIncludesIntentHint() throws {
    let auth = try #require(DeviceAuth(deviceId: "mac-m1", deviceType: .mac, deviceSecret: String(repeating: "a", count: 64)))
    let payload = CommandPayload(
        commandId: "cmd-001", deviceId: "mac-m1", deviceType: .mac, text: "scan",
        intentHint: "ouroboros_scan", priority: .background, responseMode: .notify,
        timestamp: "2026-03-29T18:45:00Z"
    )
    let canonical = auth.canonicalize(payload)
    #expect(canonical.contains("intent_hint=ouroboros_scan"))
    let parts = canonical.split(separator: "&").map { String($0.split(separator: "=")[0]) }
    #expect(parts.firstIndex(of: "intent_hint") == 3)
}

// MARK: - Secret decoding
//
// The HUD killed itself at launch with `Fatal error: String index is out of
// bounds` every time `JARVIS_DEVICE_SECRET` was unset, because the local-first
// default secret was the literal `"local"` — five characters. The old decoder
// walked the string with `index(_:offsetBy: 2)`, which traps when the final
// step runs past `endIndex`.
//
// `theLiteralLocalDefaultIsRejected` is the regression. The rest are the
// sources a malformed secret actually arrives from.

@Test func theLiteralLocalDefaultIsRejected() {
    // THE crash. Odd length AND non-hex — it must not trap, and it must not
    // silently yield the single byte 0xCA from the pair "ca", which is what the
    // old decoder did with the characters it could parse.
    #expect(DeviceAuth.decodeHexSecret("local") == nil)
    #expect(DeviceAuth(deviceId: "mac-local", deviceType: .mac,
                       deviceSecret: "local") == nil)
}

@Test func oddLengthIsRejectedRatherThanTrapping() {
    // Valid hex characters, odd count — the exact shape that ran off the end.
    #expect(DeviceAuth.decodeHexSecret("abc") == nil)
    #expect(DeviceAuth.decodeHexSecret("a") == nil)
    #expect(DeviceAuth.decodeHexSecret(String(repeating: "a", count: 65)) == nil)
}

@Test func nonHexIsRejectedNotSilentlySkipped() {
    // The quieter half of the bug: the old decoder dropped unparseable pairs
    // and returned a shorter key that could never match the server's, so every
    // request failed authentication with nothing saying why.
    #expect(DeviceAuth.decodeHexSecret("zzzz") == nil)
    #expect(DeviceAuth.decodeHexSecret("abcdzz01") == nil)
    #expect(DeviceAuth.decodeHexSecret("ab cd") == nil)   // interior space
}

@Test func aCRLFEnvFileStillWorks() {
    // `loadFromBrainstemEnv` splits on "\n" and trims only `.whitespaces`, so a
    // CRLF file hands over a 64-character secret as 65 characters with a
    // trailing "\r" — odd, and previously fatal.
    let good = String(repeating: "ab", count: 32)
    #expect(DeviceAuth.decodeHexSecret(good + "\r")?.count == 32)
    #expect(DeviceAuth.decodeHexSecret("\n " + good + " \n")?.count == 32)
}

@Test func normalisesTransportArtefactsButNotCorruption() {
    let good = String(repeating: "ab", count: 32)
    #expect(DeviceAuth.decodeHexSecret("0x" + good)?.count == 32)
    #expect(DeviceAuth.decodeHexSecret(good.uppercased())
            == DeviceAuth.decodeHexSecret(good))
    // ...but tidying stops there.
    #expect(DeviceAuth.decodeHexSecret("") == nil)
    #expect(DeviceAuth.decodeHexSecret("0x") == nil)
    #expect(DeviceAuth.decodeHexSecret("   ") == nil)
}

@Test func decodesTheBytesThePythonAndTypeScriptSidesWould() {
    // Interop is the whole point: Python does `bytes.fromhex`, TypeScript does
    // `Buffer.from(secret, "hex")`. A decoder that disagreed about the VALUE
    // would produce signatures the server rejects.
    #expect(DeviceAuth.decodeHexSecret("00ff10") == [0x00, 0xFF, 0x10])
    #expect(DeviceAuth.decodeHexSecret("deadbeef") == [0xDE, 0xAD, 0xBE, 0xEF])
}

@Test func theDerivedLocalSecretIsUsableAndStable() throws {
    let a = DeviceAuth.derivedLocalSecret(forDeviceId: "mac-local")
    #expect(a.count == 64)                                   // 32 bytes of hex
    #expect(DeviceAuth.decodeHexSecret(a)?.count == 32)
    #expect(a == DeviceAuth.derivedLocalSecret(forDeviceId: "mac-local"))
    #expect(a != DeviceAuth.derivedLocalSecret(forDeviceId: "other"))
    // ...and the thing it exists for: the HUD can actually boot with it.
    _ = try #require(DeviceAuth(deviceId: "mac-local", deviceType: .mac,
                                deviceSecret: a))
}
