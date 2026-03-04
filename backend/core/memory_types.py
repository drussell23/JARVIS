"""Canonical type definitions for the Memory Control Plane.

This module defines all enums, dataclasses, and constants shared across
the memory broker, loaders, and supervisor.  Every subsequent Memory
Control Plane module imports from here -- never from psutil directly.

Design invariants
-----------------
* ``MemorySnapshot`` is **frozen** (immutable).  Snapshots are created once
  by the broker's sampler and passed read-only to all decision logic.
* ``PressureTier`` is an ``IntEnum`` so tiers can be compared with ``<``.
* ``LeaseState`` carries an ``is_terminal`` property so callers never need
  to maintain their own terminal-set.
* ``MemoryBudgetEventType`` uses *snake_case* string values so they can
  be emitted as-is into structured logs / Langfuse traces.

Public API
----------
Enums:
    PressureTier, KernelPressure, ThrashState, SignalQuality,
    PressureTrend, BudgetPriority, StartupPhase, LeaseState,
    MemoryBudgetEventType, DisplayState, DisplayFailureCode,
    ActuatorAction

Dataclasses:
    MemorySnapshot, DegradationOption, ConfigProof, LoadResult,
    PressurePolicy, DecisionEnvelope

Constants:
    _PRESSURE_FACTORS, _SWAP_HYSTERESIS_THRESHOLD_BPS,
    _ACTUATOR_PRIORITY
"""

from __future__ import annotations

import dataclasses
from enum import Enum, IntEnum
from typing import Any, Dict, Optional


# ===================================================================
# Enums
# ===================================================================

class PressureTier(IntEnum):
    """Coarse memory-pressure classification.

    Values are ordered so that ``ABUNDANT < EMERGENCY``.
    """
    ABUNDANT = 0
    OPTIMAL = 1
    ELEVATED = 2
    CONSTRAINED = 3
    CRITICAL = 4
    EMERGENCY = 5


class DisplayState(str, Enum):
    """Ghost display lifecycle state.

    Transitional states (DEGRADING, RECOVERING, DISCONNECTING) prevent
    overlapping commands and enable deterministic crash recovery.
    """
    INACTIVE = "inactive"
    ACTIVE = "active"
    DEGRADING = "degrading"
    DEGRADED_1 = "degraded_1"
    DEGRADED_2 = "degraded_2"
    MINIMUM = "minimum"
    RECOVERING = "recovering"
    DISCONNECTING = "disconnecting"
    DISCONNECTED = "disconnected"

    @property
    def is_transitional(self) -> bool:
        return self in _TRANSITIONAL_DISPLAY_STATES

    @property
    def is_display_connected(self) -> bool:
        return self in _CONNECTED_DISPLAY_STATES


_TRANSITIONAL_DISPLAY_STATES = frozenset({
    DisplayState.DEGRADING, DisplayState.RECOVERING, DisplayState.DISCONNECTING,
})

_CONNECTED_DISPLAY_STATES = frozenset({
    DisplayState.ACTIVE, DisplayState.DEGRADED_1, DisplayState.DEGRADED_2,
    DisplayState.MINIMUM, DisplayState.DEGRADING, DisplayState.RECOVERING,
})


class DisplayFailureCode(str, Enum):
    """Failure codes for display state transitions."""
    COMMAND_TIMEOUT = "command_timeout"
    VERIFY_MISMATCH = "verify_mismatch"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    PREEMPTED = "preempted"
    QUARANTINED = "quarantined"
    CLI_ERROR = "cli_error"
    COMPOSITOR_MISMATCH = "compositor_mismatch"

    @property
    def failure_class(self) -> str:
        return _FAILURE_CLASSES.get(self, "unknown")

    @property
    def retryable(self) -> bool:
        return _FAILURE_RETRYABLE.get(self, False)


_FAILURE_CLASSES: Dict[DisplayFailureCode, str] = {
    DisplayFailureCode.COMMAND_TIMEOUT: "transient",
    DisplayFailureCode.VERIFY_MISMATCH: "structural",
    DisplayFailureCode.DEPENDENCY_BLOCKED: "operator",
    DisplayFailureCode.PREEMPTED: "transient",
    DisplayFailureCode.QUARANTINED: "structural",
    DisplayFailureCode.CLI_ERROR: "transient",
    DisplayFailureCode.COMPOSITOR_MISMATCH: "structural",
}

_FAILURE_RETRYABLE: Dict[DisplayFailureCode, bool] = {
    DisplayFailureCode.COMMAND_TIMEOUT: True,
    DisplayFailureCode.VERIFY_MISMATCH: False,
    DisplayFailureCode.DEPENDENCY_BLOCKED: False,
    DisplayFailureCode.PREEMPTED: True,
    DisplayFailureCode.QUARANTINED: False,
    DisplayFailureCode.CLI_ERROR: True,
    DisplayFailureCode.COMPOSITOR_MISMATCH: False,
}


class KernelPressure(str, Enum):
    """macOS / Linux kernel memory-pressure level."""
    NORMAL = "normal"
    WARN = "warn"
    CRITICAL = "critical"


class ThrashState(str, Enum):
    """Swap-thrash severity."""
    HEALTHY = "healthy"
    THRASHING = "thrashing"
    EMERGENCY = "emergency"


class SignalQuality(str, Enum):
    """Quality of the most recent memory signal sample."""
    GOOD = "good"
    DEGRADED = "degraded"
    FALLBACK = "fallback"


class PressureTrend(str, Enum):
    """Directional trend of memory pressure over recent samples."""
    STABLE = "stable"
    RISING = "rising"
    FALLING = "falling"


class BudgetPriority(IntEnum):
    """Priority class for memory budget requests.

    Lower numeric value = higher priority.
    ``BOOT_CRITICAL`` components are funded first.
    """
    BOOT_CRITICAL = 0
    BOOT_OPTIONAL = 1
    RUNTIME_INTERACTIVE = 2
    BACKGROUND = 3


class StartupPhase(IntEnum):
    """Current startup phase of the system.

    Mirrors ``BudgetPriority`` values so that during each phase the
    corresponding priority class is active.
    """
    BOOT_CRITICAL = 0
    BOOT_OPTIONAL = 1
    RUNTIME_INTERACTIVE = 2
    BACKGROUND = 3


class LeaseState(str, Enum):
    """Lifecycle state of a memory lease.

    Terminal states indicate the lease is no longer active and its
    memory has been (or should be) released.
    """
    PENDING = "pending"
    GRANTED = "granted"
    ACTIVE = "active"
    RELEASED = "released"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"
    PREEMPTED = "preempted"
    DENIED = "denied"

    @property
    def is_terminal(self) -> bool:
        """Return ``True`` if this state represents a final, inactive lease."""
        return self in _TERMINAL_LEASE_STATES


# Pre-computed frozenset for O(1) lookup in ``is_terminal``.
_TERMINAL_LEASE_STATES = frozenset({
    LeaseState.RELEASED,
    LeaseState.ROLLED_BACK,
    LeaseState.EXPIRED,
    LeaseState.PREEMPTED,
    LeaseState.DENIED,
})


class MemoryBudgetEventType(str, Enum):
    """Structured event types emitted by the Memory Control Plane.

    32 distinct events covering the full grant / release / preempt
    lifecycle, display lifecycle, plus observability signals.
    """
    GRANT_REQUESTED = "grant_requested"
    GRANT_ISSUED = "grant_issued"
    GRANT_DENIED = "grant_denied"
    GRANT_DEGRADED = "grant_degraded"
    GRANT_QUEUED = "grant_queued"
    HEARTBEAT = "heartbeat"
    COMMIT = "commit"
    COMMIT_OVERRUN = "commit_overrun"
    ROLLBACK = "rollback"
    RELEASE_REQUESTED = "release_requested"
    RELEASE_VERIFIED = "release_verified"
    RELEASE_FAILED = "release_failed"
    PREEMPT_REQUESTED = "preempt_requested"
    PREEMPT_COOPERATIVE = "preempt_cooperative"
    PREEMPT_FORCED = "preempt_forced"
    LEASE_EXPIRED = "lease_expired"
    RECONCILIATION = "reconciliation"
    PHASE_TRANSITION = "phase_transition"
    SWAP_HYSTERESIS_TRIP = "swap_hysteresis_trip"
    SWAP_HYSTERESIS_RECOVER = "swap_hysteresis_recover"
    LOADER_QUARANTINED = "loader_quarantined"
    LOADER_UNQUARANTINED = "loader_unquarantined"
    ESTIMATE_CALIBRATION = "estimate_calibration"
    SNAPSHOT_STALE_REJECTED = "snapshot_stale_rejected"

    # --- Display lifecycle ---
    DISPLAY_DEGRADE_REQUESTED    = "display_degrade_requested"
    DISPLAY_DEGRADED             = "display_degraded"
    DISPLAY_DISCONNECT_REQUESTED = "display_disconnect_requested"
    DISPLAY_DISCONNECTED         = "display_disconnected"
    DISPLAY_RECOVERY_REQUESTED   = "display_recovery_requested"
    DISPLAY_RECOVERED            = "display_recovered"
    DISPLAY_ACTION_FAILED        = "display_action_failed"
    DISPLAY_ACTION_PHASE         = "display_action_phase"


# ===================================================================
# Module-level constants
# ===================================================================

_PRESSURE_FACTORS: Dict[PressureTier, float] = {
    PressureTier.ABUNDANT: 1.0,
    PressureTier.OPTIMAL: 0.95,
    PressureTier.ELEVATED: 0.85,
    PressureTier.CONSTRAINED: 0.7,
    PressureTier.CRITICAL: 0.5,
    PressureTier.EMERGENCY: 0.3,
}

_SWAP_HYSTERESIS_THRESHOLD_BPS: int = 50 * 1024 * 1024  # 50 MB/s

assert set(_PRESSURE_FACTORS.keys()) == set(PressureTier), \
    "_PRESSURE_FACTORS must cover all PressureTier values"


# ===================================================================
# Dataclasses
# ===================================================================

@dataclasses.dataclass(frozen=True)
class MemorySnapshot:
    """Immutable point-in-time snapshot of system memory state.

    Created by the broker's sampler and passed to all decision logic.
    Replaces raw ``psutil`` calls throughout the codebase.

    All 27 fields are required and represent a complete picture of system
    memory at a single point in time.

    Computed properties
    -------------------
    * ``headroom_bytes`` -- budget minus safety floor, floored at 0.
    * ``pressure_factor`` -- multiplier (0.0-1.0) derived from tier.
    * ``swap_hysteresis_active`` -- True when swap growth exceeds threshold.
    """

    # --- Physical truth (bytes) ---
    physical_total: int
    physical_wired: int
    physical_active: int
    physical_inactive: int
    physical_compressed: int
    physical_free: int

    # --- Swap state ---
    swap_total: int
    swap_used: int
    swap_growth_rate_bps: float  # bytes per second

    # --- Derived budget fields ---
    usable_bytes: int
    committed_bytes: int
    available_budget_bytes: int

    # --- Pressure signals ---
    kernel_pressure: KernelPressure  # typed enum, NOT str
    pressure_tier: PressureTier
    thrash_state: ThrashState  # typed enum
    pageins_per_sec: float

    # --- Trend derivatives (30s window) ---
    host_rss_slope_bps: float
    jarvis_tree_rss_slope_bps: float
    swap_slope_bps: float
    pressure_trend: PressureTrend

    # --- Safety ---
    safety_floor_bytes: int
    compressed_trend_bytes: int

    # --- Signal quality ---
    signal_quality: SignalQuality

    # --- Metadata ---
    timestamp: float
    max_age_ms: int
    epoch: int
    snapshot_id: str

    # --- Computed properties ---

    @property
    def headroom_bytes(self) -> int:
        """Available budget minus safety floor, never negative."""
        return max(0, self.available_budget_bytes - self.safety_floor_bytes)

    @property
    def pressure_factor(self) -> float:
        """Multiplier (0.0 -- 1.0) governing how aggressively to grant memory.

        Higher tiers yield lower factors, throttling new grants.
        """
        return _PRESSURE_FACTORS[self.pressure_tier]

    @property
    def swap_hysteresis_active(self) -> bool:
        """True when swap growth rate exceeds the hysteresis threshold.

        Uses strict greater-than so that exactly-at-threshold is *not*
        considered active (avoids flapping at boundary).
        """
        return self.swap_growth_rate_bps > _SWAP_HYSTERESIS_THRESHOLD_BPS


@dataclasses.dataclass
class DegradationOption:
    """A possible degradation a loader can accept to fit within budget.

    Presented by the loader to the broker during grant negotiation.
    """
    name: str
    bytes_required: int
    quality_impact: float  # 0.0 = no impact, 1.0 = unusable
    constraints: Dict[str, Any]


@dataclasses.dataclass
class ConfigProof:
    """Evidence that a loader applied the agreed-upon configuration.

    Returned by the loader after loading; the broker verifies compliance.
    """
    component_id: str
    requested_constraints: Dict[str, Any]
    applied_config: Dict[str, Any]
    compliant: bool
    evidence: str


@dataclasses.dataclass
class LoadResult:
    """Outcome of a loader's attempt to load a component.

    Includes the config proof (if loading succeeded) and optional error
    message (if it failed).
    """
    success: bool
    actual_bytes: int
    config_proof: Optional[ConfigProof]
    model_handle: Optional[Any]
    load_duration_ms: float
    error: Optional[str]


# ===================================================================
# Actuator Action Enum
# ===================================================================

class ActuatorAction(str, Enum):
    """Priority-ordered actions that memory actuators can request.

    Lower priority number = less disruptive, preferred first.
    Use the ``priority`` property to compare action severity.
    """
    DISPLAY_SHED = "display_shed"
    DEFCON_ESCALATE = "defcon_escalate"
    MODEL_EVICT = "model_evict"
    CLOUD_OFFLOAD = "cloud_offload"
    CLOUD_SCALE = "cloud_scale"
    CLEANUP = "cleanup"

    @property
    def priority(self) -> int:
        """Return numeric priority (0 = least disruptive)."""
        return _ACTUATOR_PRIORITY[self]


_ACTUATOR_PRIORITY: Dict[ActuatorAction, int] = {
    ActuatorAction.DISPLAY_SHED: 0,
    ActuatorAction.DEFCON_ESCALATE: 1,
    ActuatorAction.MODEL_EVICT: 2,
    ActuatorAction.CLOUD_OFFLOAD: 3,
    ActuatorAction.CLOUD_SCALE: 4,
    ActuatorAction.CLEANUP: 5,
}

assert set(_ACTUATOR_PRIORITY.keys()) == set(ActuatorAction), \
    "_ACTUATOR_PRIORITY must cover all ActuatorAction values"


# ===================================================================
# Cloud Capacity Action Enum
# ===================================================================

class CloudCapacityAction(str, Enum):
    """Cloud capacity decisions made by CloudCapacityController."""

    STAY_LOCAL = "stay_local"
    DEGRADE_LOCAL = "degrade_local"
    OFFLOAD_PARTIAL = "offload_partial"
    SPIN_SPOT = "spin_spot"
    FALLBACK_ONDEMAND = "fallback_ondemand"


# ===================================================================
# PressurePolicy (versioned threshold rules with hysteresis)
# ===================================================================

@dataclasses.dataclass(frozen=True)
class PressurePolicy:
    """Versioned threshold rules with hysteresis deadbands.

    Replaces all hardcoded memory-pressure thresholds.  Each actionable
    ``PressureTier`` (ELEVATED through EMERGENCY) has an *enter* threshold
    and a strictly-lower *exit* threshold, creating a deadband that
    prevents tier flapping.

    Use the ``for_ram_gb`` class method to obtain hardware-appropriate
    defaults.
    """

    version: str = "v1.0"

    enter_thresholds: Dict[PressureTier, float] = dataclasses.field(
        default_factory=lambda: {
            PressureTier.ELEVATED: 70.0,
            PressureTier.CONSTRAINED: 80.0,
            PressureTier.CRITICAL: 90.0,
            PressureTier.EMERGENCY: 95.0,
        },
    )

    exit_thresholds: Dict[PressureTier, float] = dataclasses.field(
        default_factory=lambda: {
            PressureTier.ELEVATED: 65.0,
            PressureTier.CONSTRAINED: 75.0,
            PressureTier.CRITICAL: 85.0,
            PressureTier.EMERGENCY: 90.0,
        },
    )

    min_dwell_seconds: float = 5.0
    cooldown_seconds: float = 30.0
    max_actions_per_hour: int = 12

    def __post_init__(self) -> None:
        _actionable = {
            PressureTier.ELEVATED,
            PressureTier.CONSTRAINED,
            PressureTier.CRITICAL,
            PressureTier.EMERGENCY,
        }
        if not set(self.enter_thresholds.keys()) >= _actionable:
            raise ValueError("enter_thresholds must cover all actionable PressureTiers")
        if not set(self.exit_thresholds.keys()) >= _actionable:
            raise ValueError("exit_thresholds must cover all actionable PressureTiers")
        for tier in _actionable:
            if self.exit_thresholds[tier] >= self.enter_thresholds[tier]:
                raise ValueError(
                    f"Hysteresis violated for {tier}: "
                    f"exit={self.exit_thresholds[tier]} >= enter={self.enter_thresholds[tier]}"
                )
        if self.min_dwell_seconds <= 0.0:
            raise ValueError("min_dwell_seconds must be positive")
        if self.cooldown_seconds <= 0.0:
            raise ValueError("cooldown_seconds must be positive")
        if self.max_actions_per_hour <= 0:
            raise ValueError("max_actions_per_hour must be positive")
        if not self.version.startswith("v"):
            raise ValueError("version must start with 'v'")

    @classmethod
    def for_ram_gb(cls, total_gb: float) -> PressurePolicy:
        """Factory that returns hardware-appropriate thresholds.

        Profiles
        --------
        * ``< 12 GB`` -- *constrained* (aggressive thresholds)
        * ``< 20 GB`` -- *consumer* (16 GB Mac default)
        * ``< 48 GB`` -- *prosumer* (32 GB workstation)
        * ``>= 48 GB`` -- *server* (64 GB+ headroom)
        """
        if total_gb < 12.0:
            version = "v1.0-constrained"
            enter = {
                PressureTier.ELEVATED: 85.0,
                PressureTier.CONSTRAINED: 90.0,
                PressureTier.CRITICAL: 95.0,
                PressureTier.EMERGENCY: 97.0,
            }
            exit_ = {
                PressureTier.ELEVATED: 80.0,
                PressureTier.CONSTRAINED: 87.0,
                PressureTier.CRITICAL: 92.0,
                PressureTier.EMERGENCY: 95.0,
            }
        elif total_gb < 20.0:
            version = "v1.0-consumer"
            enter = {
                PressureTier.ELEVATED: 80.0,
                PressureTier.CONSTRAINED: 88.0,
                PressureTier.CRITICAL: 93.0,
                PressureTier.EMERGENCY: 96.0,
            }
            exit_ = {
                PressureTier.ELEVATED: 75.0,
                PressureTier.CONSTRAINED: 84.0,
                PressureTier.CRITICAL: 90.0,
                PressureTier.EMERGENCY: 93.0,
            }
        elif total_gb < 48.0:
            version = "v1.0-prosumer"
            enter = {
                PressureTier.ELEVATED: 65.0,
                PressureTier.CONSTRAINED: 75.0,
                PressureTier.CRITICAL: 85.0,
                PressureTier.EMERGENCY: 93.0,
            }
            exit_ = {
                PressureTier.ELEVATED: 60.0,
                PressureTier.CONSTRAINED: 70.0,
                PressureTier.CRITICAL: 80.0,
                PressureTier.EMERGENCY: 90.0,
            }
        else:
            version = "v1.0-server"
            enter = {
                PressureTier.ELEVATED: 55.0,
                PressureTier.CONSTRAINED: 65.0,
                PressureTier.CRITICAL: 80.0,
                PressureTier.EMERGENCY: 90.0,
            }
            exit_ = {
                PressureTier.ELEVATED: 50.0,
                PressureTier.CONSTRAINED: 60.0,
                PressureTier.CRITICAL: 75.0,
                PressureTier.EMERGENCY: 85.0,
            }

        return cls(
            version=version,
            enter_thresholds=enter,
            exit_thresholds=exit_,
        )


# ===================================================================
# DecisionEnvelope (provenance wrapper for actuator decisions)
# ===================================================================

@dataclasses.dataclass(frozen=True)
class DecisionEnvelope:
    """Provenance wrapper for every actuator decision.

    Every action submitted by an actuator must be wrapped in a
    ``DecisionEnvelope`` that records the snapshot, epoch, sequence, and
    policy version that produced the decision.  The coordinator uses
    ``is_stale`` to reject decisions that were computed against an
    outdated snapshot.
    """

    snapshot_id: str
    epoch: int
    sequence: int
    policy_version: str
    pressure_tier: PressureTier
    timestamp: float

    def is_stale(self, *, current_epoch: int, current_sequence: int) -> bool:
        """Return ``True`` if this envelope is outdated.

        An envelope is stale when:
        * Its epoch is behind the current epoch, **or**
        * Its epoch matches but its sequence is behind the current sequence.
        """
        if self.epoch < current_epoch:
            return True
        if self.epoch == current_epoch and self.sequence < current_sequence:
            return True
        return False
