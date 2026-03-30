import Testing
@testable import JARVISKit

@Test func canonicalizeMatchesSpec() {
    let auth = DeviceAuth(deviceId: "watch-ultra2-derek", deviceType: .watch, deviceSecret: String(repeating: "a", count: 64))
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

@Test func signProduces64CharHex() {
    let auth = DeviceAuth(deviceId: "mac-m1", deviceType: .mac, deviceSecret: String(repeating: "a", count: 64))
    let payload = CommandPayload(
        commandId: "cmd-001", deviceId: "mac-m1", deviceType: .mac, text: "hello",
        priority: .realtime, responseMode: .stream, timestamp: "2026-03-29T18:45:00Z"
    )
    let sig = auth.sign(payload)
    #expect(sig.count == 64)
    #expect(sig.allSatisfy { "0123456789abcdef".contains($0) })
}

@Test func signIsDeterministic() {
    let auth = DeviceAuth(deviceId: "mac-m1", deviceType: .mac, deviceSecret: String(repeating: "a", count: 64))
    let payload = CommandPayload(
        commandId: "cmd-001", deviceId: "mac-m1", deviceType: .mac, text: "hello",
        priority: .realtime, responseMode: .stream, timestamp: "2026-03-29T18:45:00Z"
    )
    #expect(auth.sign(payload) == auth.sign(payload))
}

@Test func signChangesWithDifferentText() {
    let auth = DeviceAuth(deviceId: "mac-m1", deviceType: .mac, deviceSecret: String(repeating: "a", count: 64))
    let p1 = CommandPayload(commandId: "cmd-001", deviceId: "mac-m1", deviceType: .mac, text: "hello", priority: .realtime, responseMode: .stream, timestamp: "2026-03-29T18:45:00Z")
    let p2 = CommandPayload(commandId: "cmd-001", deviceId: "mac-m1", deviceType: .mac, text: "goodbye", priority: .realtime, responseMode: .stream, timestamp: "2026-03-29T18:45:00Z")
    #expect(auth.sign(p1) != auth.sign(p2))
}

@Test func canonicalizeIncludesIntentHint() {
    let auth = DeviceAuth(deviceId: "mac-m1", deviceType: .mac, deviceSecret: String(repeating: "a", count: 64))
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
