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
    MemoryBudgetEventType

Dataclasses:
    MemorySnapshot, DegradationOption, ConfigProof, LoadResult

Constants:
    _PRESSURE_FACTORS, _SWAP_HYSTERESIS_THRESHOLD_BPS
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

    24 distinct events covering the full grant / release / preempt
    lifecycle plus observability signals.
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


# ===================================================================
# Module-level constants
# ===================================================================

_PRESSURE_FACTORS: Dict[PressureTier, float] = {
    PressureTier.ABUNDANT: 1.0,
    PressureTier.OPTIMAL: 0.9,
    PressureTier.ELEVATED: 0.75,
    PressureTier.CONSTRAINED: 0.6,
    PressureTier.CRITICAL: 0.5,
    PressureTier.EMERGENCY: 0.3,
}

_SWAP_HYSTERESIS_THRESHOLD_BPS: int = 50 * 1024 * 1024  # 50 MB/s


# ===================================================================
# Dataclasses
# ===================================================================

@dataclasses.dataclass(frozen=True)
class MemorySnapshot:
    """Immutable point-in-time snapshot of system memory state.

    Created by the broker's sampler and passed to all decision logic.
    Replaces raw ``psutil`` calls throughout the codebase.

    Computed properties
    -------------------
    * ``headroom_bytes`` -- budget minus safety floor, floored at 0.
    * ``pressure_factor`` -- multiplier (0.0-1.0) derived from tier.
    * ``swap_hysteresis_active`` -- True when swap growth exceeds threshold.
    """

    # --- Physical memory ---
    total_bytes: int
    available_bytes: int
    used_bytes: int

    # --- Swap ---
    swap_used_bytes: int
    swap_growth_rate_bps: int  # bytes per second

    # --- Kernel signal ---
    kernel_pressure_level: str

    # --- Pressure classification ---
    pressure_tier: PressureTier

    # --- Budget accounting ---
    available_budget_bytes: int
    safety_floor_bytes: int
    active_leases_bytes: int
    pending_grants_bytes: int

    # --- Timestamp ---
    timestamp_ns: int

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
