"""Core data types for the cross-repo integration test harness.

All domain objects are frozen dataclasses to guarantee immutability
throughout a scenario run.  Enums use lowercase string values so
they round-trip cleanly through JSON event logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, FrozenSet, List, Literal, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ComponentStatus(Enum):
    """Lifecycle status of a managed component (11 values)."""

    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    LOST = "LOST"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    REGISTERED = "REGISTERED"
    HANDSHAKING = "HANDSHAKING"
    DRAINING = "DRAINING"
    STOPPING = "STOPPING"
    UNKNOWN = "UNKNOWN"


class FaultScope(Enum):
    """Where a fault is injected (5 scopes)."""

    COMPONENT = "component"
    TRANSPORT = "transport"
    CONTRACT = "contract"
    CLOCK = "clock"
    PROCESS = "process"


class FaultComposition(Enum):
    """Policy when a new fault overlaps an active one (3 policies)."""

    REJECT = "reject"
    STACK = "stack"
    REPLACE = "replace"


class ContractReasonCode(Enum):
    """Reason codes for contract compatibility checks (6 codes)."""

    OK = "ok"
    VERSION_WINDOW = "version_window"
    SCHEMA_HASH = "schema_hash"
    MISSING_CAPABILITY = "missing_capability"
    HANDSHAKE_MISSING = "handshake_missing"
    HANDSHAKE_EXPIRED = "handshake_expired"


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ObservedEvent:
    """A single event observed by the test oracle during a scenario run."""

    oracle_event_seq: int
    timestamp_mono: float
    source: str
    event_type: str
    component: Optional[str]
    old_value: Optional[str]
    new_value: str
    epoch: int
    scenario_phase: str
    trace_root_id: str
    trace_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OracleObservation:
    """A point-in-time observation with quality classification."""

    value: Any
    observed_at_mono: float
    observation_quality: Literal["fresh", "stale", "timeout", "divergent"]
    source: str


@dataclass(frozen=True)
class ContractStatus:
    """Result of a contract compatibility check."""

    compatible: bool
    reason_code: ContractReasonCode
    detail: Optional[str] = None


@dataclass(frozen=True)
class FaultHandle:
    """Handle returned when a fault is successfully injected."""

    fault_id: str
    scope: FaultScope
    target: str
    affected_components: FrozenSet[str]
    unaffected_components: FrozenSet[str]
    pre_fault_baseline: Dict[str, str]
    convergence_deadline_s: float
    revert: Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class PhaseFailure:
    """A typed failure that occurred during a scenario phase."""

    phase: str
    failure_type: str  # "phase_timeout" | "oracle_stale" | "invariant_violation" | "divergence_error"
    detail: str


@dataclass(frozen=True)
class PhaseResult:
    """Outcome of a single scenario phase."""

    duration_s: float
    violations: List[Any] = field(default_factory=list)


@dataclass(frozen=True)
class ScenarioResult:
    """Aggregated outcome of a complete scenario run."""

    scenario_name: str
    trace_root_id: str
    passed: bool
    violations: List[Any]
    phases: Dict[str, PhaseResult]
    event_log: List[Any]
