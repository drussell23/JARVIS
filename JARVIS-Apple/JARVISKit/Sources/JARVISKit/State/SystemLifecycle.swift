import Foundation
import Combine

// MARK: - Adaptive UI State Machine (Phase 12, Slice F)
//
// The native HUD's view of the backend organism's boot lifecycle, driven
// DETERMINISTICALLY by the `ouroboros.hydration` SSE telemetry flowing through
// the governance SSE bridge — no hardcoded timers, pure reaction to the
// TrinityEventBus daemon frames the client already consumes (mandate 1 + 3).

/// The visible lifecycle state of the JARVIS body behind the HUD.
public enum SystemLifecycle: String, Sendable, Equatable {
    /// SSE is up but no lifecycle telemetry has arrived yet. Fail-soft: the
    /// input is NOT locked here, so a backend that is already `ready` (and
    /// therefore re-emits nothing on connect) can never strand the user.
    case connecting
    /// `SYSTEM_HYDRATING` — heavy subsystems / O+V loading in the background.
    case hydrating
    /// `SYSTEM_READY` — the organism is fully online, autonomy included.
    case ready
    /// `SYSTEM_DEGRADED` / `OUROBOROS_FAULT` — background autonomous functions
    /// are offline, but the DoubleWord-failover text command loop is ALIVE.
    case degraded

    /// The primary command input is locked ONLY while actively hydrating.
    /// `degraded`/fault MUST unlock — the client never shows an infinite
    /// spinner when the text loop is still serviceable (mandate 2, the edge case).
    public var isInputLocked: Bool { self == .hydrating }
    public var isInputEnabled: Bool { !isInputLocked }

    /// Render the non-blocking "Waking Ouroboros" initialization overlay.
    public var showsInitializationOverlay: Bool { self == .hydrating }

    /// Background autonomy (O+V) is offline but text commands still work —
    /// the HUD shows the discrete amber warning indicator.
    public var isAutonomyOffline: Bool { self == .degraded }
}

/// The backend lifecycle discriminators emitted on the daemon channel
/// (`DaemonEvent.lifecycle`). Unknown/absent discriminators are ignored, so
/// ordinary narration frames never perturb the lifecycle.
public enum SystemLifecycleSignal: String, Sendable {
    case hydrating = "SYSTEM_HYDRATING"
    case ready = "SYSTEM_READY"
    case degraded = "SYSTEM_DEGRADED"
    case ouroborosFault = "OUROBOROS_FAULT"

    public var lifecycle: SystemLifecycle {
        switch self {
        case .hydrating: return .hydrating
        case .ready: return .ready
        // A crashed O+V (even mid auto-restart) degrades autonomy but the
        // command loop survives — do NOT lock the user out.
        case .degraded, .ouroborosFault: return .degraded
        }
    }
}

/// The Adaptive UI State Machine. An `ObservableObject` the HUD binds to; it
/// folds daemon SSE telemetry into a deterministic UI lifecycle. DRY (mandate
/// 3): consumes the EXISTING `JARVISEvent.daemon` stream — it opens no second
/// websocket and runs no status-polling loop.
@MainActor
public final class SystemStatusStore: ObservableObject {
    /// The current lifecycle — the single source of truth the views react to.
    @Published public private(set) var lifecycle: SystemLifecycle
    /// The most recent human-readable narration (e.g. "Waking Ouroboros…",
    /// "Organism hydrated (DEGRADED …)"). Drives the overlay/indicator label.
    @Published public private(set) var narration: String = ""

    public init(lifecycle: SystemLifecycle = .connecting) {
        self.lifecycle = lifecycle
    }

    // Derived UI affordances — one source of truth for every view.
    public var isInputEnabled: Bool { lifecycle.isInputEnabled }
    public var isInputLocked: Bool { lifecycle.isInputLocked }
    public var showsInitializationOverlay: Bool { lifecycle.showsInitializationOverlay }
    public var isAutonomyOffline: Bool { lifecycle.isAutonomyOffline }

    /// A concise message for the current phase — prefers the live backend
    /// narration, falls back to a deterministic default per state.
    public var statusMessage: String {
        if !narration.isEmpty { return narration }
        switch lifecycle {
        case .connecting: return "Connecting…"
        case .hydrating:  return "Waking Ouroboros…"
        case .ready:      return "Organism online"
        case .degraded:   return "Autonomy offline — command loop live"
        }
    }

    /// Deterministically fold ONE SSE event into the lifecycle. Only daemon
    /// frames carrying a recognized `lifecycle` discriminator transition state;
    /// every other event is ignored here. NEVER throws.
    public func ingest(_ event: JARVISEvent) {
        guard case let .daemon(daemon) = event else { return }
        apply(lifecycleRaw: daemon.lifecycle, narration: daemon.narrationText)
    }

    /// Apply a raw discriminator (exactly as it arrives on the wire) plus the
    /// narration text. An unrecognized/absent discriminator only updates the
    /// narration; it never changes the lifecycle.
    public func apply(lifecycleRaw: String?, narration: String = "") {
        if !narration.isEmpty { self.narration = narration }
        guard let raw = lifecycleRaw,
              let signal = SystemLifecycleSignal(rawValue: raw) else { return }
        lifecycle = signal.lifecycle
    }

    /// Reset to the pre-telemetry state (e.g. on a fresh SSE reconnect).
    public func reset() {
        lifecycle = .connecting
        narration = ""
    }
}
