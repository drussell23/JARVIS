import XCTest
@testable import JARVISKit

/// Slice F — the Adaptive UI State Machine. Proves the native client respects
/// the fail-soft backend architecture: it reacts deterministically to the
/// `ouroboros.hydration` SSE telemetry and NEVER strands the user behind an
/// infinite spinner when the DoubleWord text loop is still serviceable.
@MainActor
final class SystemLifecycleTests: XCTestCase {

    // MARK: - Wire contract: DaemonEvent decodes the lifecycle discriminator

    func testDaemonEventDecodesLifecycleFromSSEPayload() throws {
        let json = """
        {"command_id":"hydration","narration_text":"Waking Ouroboros…",
         "narration_priority":"normal","source_brain":"supervisor",
         "lifecycle":"SYSTEM_HYDRATING"}
        """.data(using: .utf8)!
        let event = try JSONDecoder().decode(DaemonEvent.self, from: json)
        XCTAssertEqual(event.lifecycle, "SYSTEM_HYDRATING")
        XCTAssertEqual(event.narrationText, "Waking Ouroboros…")
    }

    func testDaemonEventLifecycleIsBackwardCompatible() throws {
        // A legacy daemon frame with NO lifecycle key must still decode.
        let json = """
        {"command_id":"x","narration_text":"O+V: activity",
         "narration_priority":"normal","source_brain":"ouroboros"}
        """.data(using: .utf8)!
        let event = try JSONDecoder().decode(DaemonEvent.self, from: json)
        XCTAssertNil(event.lifecycle)
    }

    // MARK: - MANDATE 4: HYDRATING (locked) → DEGRADED (unlocked) transition

    func testHydratingThenDegradedUnlocksTheCommandInput() {
        let store = SystemStatusStore()

        // 1. On connect the client receives SYSTEM_HYDRATING → non-blocking
        //    initialization overlay, primary input LOCKED.
        store.ingest(makeDaemon(lifecycle: "SYSTEM_HYDRATING",
                                text: "Backend online — hydrating O+V…"))
        XCTAssertEqual(store.lifecycle, .hydrating)
        XCTAssertFalse(store.isInputEnabled, "input must be locked while hydrating")
        XCTAssertTrue(store.showsInitializationOverlay)
        XCTAssertFalse(store.isAutonomyOffline)

        // 2. Then a SYSTEM_DEGRADED frame arrives (a subsystem failed, but the
        //    DoubleWord text loop is alive). The HUD MUST unlock immediately.
        store.ingest(makeDaemon(lifecycle: "SYSTEM_DEGRADED",
                                text: "Organism hydrated (DEGRADED) — command loop live"))
        XCTAssertEqual(store.lifecycle, .degraded)
        XCTAssertTrue(store.isInputEnabled, "DEGRADED must UNLOCK the input (fail-soft)")
        XCTAssertFalse(store.showsInitializationOverlay, "no infinite spinner")
        XCTAssertTrue(store.isAutonomyOffline, "amber indicator: autonomy offline")
    }

    // MARK: - Edge cases

    func testOuroborosFaultAlsoUnlocksAndFlagsAutonomyOffline() {
        let store = SystemStatusStore(lifecycle: .hydrating)
        store.ingest(makeDaemon(lifecycle: "OUROBOROS_FAULT",
                                text: "O+V faulted (RuntimeError) — auto-restarting in 2s"))
        XCTAssertEqual(store.lifecycle, .degraded)
        XCTAssertTrue(store.isInputEnabled)
        XCTAssertTrue(store.isAutonomyOffline)
    }

    func testSystemReadyEnablesFullOnlineState() {
        let store = SystemStatusStore(lifecycle: .hydrating)
        store.ingest(makeDaemon(lifecycle: "SYSTEM_READY", text: "Organism online"))
        XCTAssertEqual(store.lifecycle, .ready)
        XCTAssertTrue(store.isInputEnabled)
        XCTAssertFalse(store.isAutonomyOffline)
        XCTAssertFalse(store.showsInitializationOverlay)
    }

    func testDegradedCanRecoverToReady() {
        let store = SystemStatusStore(lifecycle: .degraded)
        store.ingest(makeDaemon(lifecycle: "SYSTEM_READY", text: "Recovered"))
        XCTAssertEqual(store.lifecycle, .ready)
        XCTAssertFalse(store.isAutonomyOffline)
    }

    func testConnectingDefaultDoesNotLockInput() {
        // Fail-soft: before any telemetry (e.g. backend already ready and not
        // re-emitting), the user is NOT locked out.
        let store = SystemStatusStore()
        XCTAssertEqual(store.lifecycle, .connecting)
        XCTAssertTrue(store.isInputEnabled)
        XCTAssertFalse(store.showsInitializationOverlay)
    }

    func testUnrelatedDaemonNarrationDoesNotChangeLifecycle() {
        let store = SystemStatusStore(lifecycle: .ready)
        // An ordinary O+V activity frame (no lifecycle key) must not perturb
        // the lifecycle — only update the narration.
        store.ingest(makeDaemon(lifecycle: nil, text: "O+V: activity"))
        XCTAssertEqual(store.lifecycle, .ready)
        XCTAssertEqual(store.narration, "O+V: activity")
    }

    func testNonDaemonEventsAreIgnored() {
        let store = SystemStatusStore(lifecycle: .hydrating)
        store.ingest(.heartbeat)
        XCTAssertEqual(store.lifecycle, .hydrating)   // unchanged
    }

    // MARK: - Helper

    private func makeDaemon(lifecycle: String?, text: String) -> JARVISEvent {
        .daemon(DaemonEvent(
            commandId: "test", narrationText: text,
            narrationPriority: "normal", sourceBrain: "supervisor",
            lifecycle: lifecycle))
    }
}
