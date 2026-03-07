"""Root authority contract types for the Triple Authority Resolution system.

This module defines ALL shared types used by the root authority lifecycle
management layer.  It has ZERO imports from the orchestrator, USP, or any
other JARVIS module -- stdlib only.

Schema version follows semver.  Compatibility rule used by ContractGate:
  * Major must match exactly.
  * Minor may differ by at most 1 (N / N-1).
  * Patch is ignored.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: str = "1.0.0"

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LifecycleAction(Enum):
    """Actions the root authority may issue against a subsystem."""

    DRAIN = "drain"
    TERM = "term"
    GROUP_KILL = "group_kill"
    RESTART = "restart"
    ESCALATE_OPERATOR = "escalate_operator"


class SubsystemState(Enum):
    """Observable lifecycle states for a managed subsystem."""

    STARTING = "starting"
    HANDSHAKE = "handshake"
    ALIVE = "alive"
    READY = "ready"
    DEGRADED = "degraded"
    DRAINING = "draining"
    STOPPED = "stopped"
    CRASHED = "crashed"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        """Return True if this state represents a final, non-recoverable state."""
        return self in _TERMINAL_STATES


# Pre-compute the terminal set once at import time.
_TERMINAL_STATES = frozenset({
    SubsystemState.STOPPED,
    SubsystemState.CRASHED,
    SubsystemState.REJECTED,
})


class RequiredTier(Enum):
    """How critical a resource is to system operation."""

    REQUIRED = "required"
    ENHANCEMENT = "enhancement"
    OPTIONAL = "optional"


class RecoveryAction(Enum):
    """Actions the verdict system may recommend for a failed resource."""

    NONE = "none"
    RETRY = "retry"
    ROUTE_TO_GCP = "route_to_gcp"
    ROUTE_TO_LOCAL = "route_to_local"
    MANUAL = "manual"
    RESTART_MANAGER = "restart_manager"
    DEFERRED_RECOVERY = "deferred_recovery"


class VerdictReasonCode(Enum):
    """Controlled vocabulary for resource verdict reasons."""

    HEALTHY = "healthy"
    DISABLED_BY_CONFIG = "disabled_by_config"
    NOT_INSTALLED = "not_installed"
    MEMORY_ADMISSION_CLOUD_FIRST = "memory_admission_cloud_first"
    MEMORY_ADMISSION_CLOUD_ONLY = "memory_admission_cloud_only"
    PREFLIGHT_TIMEOUT = "preflight_timeout"
    INIT_TIMEOUT = "init_timeout"
    INIT_EXCEPTION = "init_exception"
    INIT_RETURNED_FALSE = "init_returned_false"
    PORT_CONFLICT = "port_conflict"
    GCP_CLIENT_UNAVAILABLE = "gcp_client_unavailable"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    DEPENDENCY_MISSING = "dependency_missing"
    STALE_EPOCH = "stale_epoch"
    UNKNOWN = "unknown"


# Severity lattice: maps each SubsystemState to an integer severity level.
# 0 = healthy, 1 = degraded, 2 = stopped/rejected, 3 = crashed.
SEVERITY_MAP: Mapping[SubsystemState, int] = {
    SubsystemState.READY: 0,
    SubsystemState.ALIVE: 0,
    SubsystemState.STARTING: 0,
    SubsystemState.HANDSHAKE: 0,
    SubsystemState.DEGRADED: 1,
    SubsystemState.DRAINING: 1,
    SubsystemState.STOPPED: 2,
    SubsystemState.REJECTED: 2,
    SubsystemState.CRASHED: 3,
}

# ---------------------------------------------------------------------------
# Verdict value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerdictWarning:
    """An advisory warning attached to a ResourceVerdict."""

    code: str
    detail: str
    origin: str


@dataclass(frozen=True)
class ResourceVerdict:
    """Immutable admission decision for a single managed resource.

    Fields are grouped by concern:
      * Identity   – who/when produced the verdict.
      * State      – observed lifecycle state.
      * Admission  – boot / serviceability gates.
      * Reason     – structured explanation.
      * Evidence   – opaque bag of supporting data.
      * Recovery   – recommended next steps.
      * Capabilities – features the resource advertises.

    ``__post_init__`` enforces three cross-field invariants that must
    never be violated at construction time.
    """

    # -- class-level constant (not per-instance) --
    SCHEMA_VERSION: int = field(init=False, default=1, repr=False, compare=False)

    # -- Identity --
    origin: str
    correlation_id: str
    epoch: int
    monotonic_ns: int
    wall_utc: str
    sequence: int

    # -- State --
    state: SubsystemState

    # -- Admission --
    boot_allowed: bool
    serviceable: bool
    required_tier: RequiredTier

    # -- Reason --
    reason_code: VerdictReasonCode
    reason_detail: str
    retryable: bool
    retry_after_s: Optional[float] = None

    # -- Evidence --
    evidence: Mapping[str, object] = field(default_factory=dict)

    # -- Recovery --
    recovery_owner: Optional[str] = None
    next_action: RecoveryAction = RecoveryAction.NONE

    # -- Capabilities --
    capabilities: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Invariant 1: CRASHED resources cannot claim serviceability.
        if self.state is SubsystemState.CRASHED and self.serviceable:
            raise ValueError("CRASHED verdict cannot be serviceable")

        # Invariant 2: Denying boot while claiming READY is contradictory.
        if not self.boot_allowed and self.state is SubsystemState.READY:
            raise ValueError("boot_allowed=False contradicts READY state")

        # Invariant 3: REQUIRED + not serviceable while READY is incoherent.
        if (
            self.required_tier is RequiredTier.REQUIRED
            and not self.serviceable
            and self.state is SubsystemState.READY
        ):
            raise ValueError(
                "REQUIRED + not serviceable contradicts READY state"
            )

    @property
    def severity(self) -> int:
        """Return the integer severity level for the current state."""
        return SEVERITY_MAP.get(self.state, 3)


@dataclass(frozen=True)
class PhaseVerdict:
    """Immutable aggregated verdict for an entire boot phase.

    Produced by :func:`aggregate_verdicts` from a collection of per-manager
    :class:`ResourceVerdict` instances.  The ``state`` reflects the worst
    required-tier manager; non-required managers contribute only warnings.
    """

    # -- class-level constant (not per-instance) --
    SCHEMA_VERSION: int = field(init=False, default=1, repr=False, compare=False)

    # -- Identity --
    phase_name: str
    state: SubsystemState
    boot_allowed: bool
    serviceable: bool
    manager_verdicts: Mapping[str, ResourceVerdict]
    reason_codes: Tuple[VerdictReasonCode, ...]
    warnings: Tuple[VerdictWarning, ...]
    epoch: int
    monotonic_ns: int
    wall_utc: str
    correlation_id: str

    @property
    def severity(self) -> int:
        """Return the integer severity level for the current state."""
        return SEVERITY_MAP.get(self.state, 3)


def aggregate_verdicts(
    phase_name: str,
    verdicts: Mapping[str, ResourceVerdict],
    epoch: int,
    correlation_id: str,
    *,
    allow_empty_required: bool = False,
) -> PhaseVerdict:
    """Aggregate per-manager verdicts into a single :class:`PhaseVerdict`.

    Policy
    ------
    * Required managers gate ``boot_allowed`` and ``serviceable``.
    * Non-required managers with severity > 0 contribute warnings only.
    * If no required managers exist and *allow_empty_required* is False the
      function fails closed (REJECTED, boot_allowed=False).
    * Reason codes are deduplicated and sorted by descending max severity of
      that code, then by enum value for determinism.
    * Worst state among required managers is chosen via a deterministic
      tiebreak: ``(severity, 0 if retryable else 1, monotonic_ns)``.
    """
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    required: list[ResourceVerdict] = []
    non_required: list[tuple[str, ResourceVerdict]] = []

    for name, v in verdicts.items():
        if v.required_tier is RequiredTier.REQUIRED:
            required.append(v)
        else:
            non_required.append((name, v))

    # ------------------------------------------------------------------
    # Fail-closed when no required managers are present
    # ------------------------------------------------------------------
    if not required and not allow_empty_required:
        # Build warnings from non-required managers with severity > 0
        warnings: list[VerdictWarning] = []
        for name, v in non_required:
            sev = SEVERITY_MAP.get(v.state, 3)
            if sev > 0:
                warnings.append(
                    VerdictWarning(
                        code=v.reason_code.value,
                        detail=v.reason_detail,
                        origin=name,
                    )
                )
        return PhaseVerdict(
            phase_name=phase_name,
            state=SubsystemState.REJECTED,
            boot_allowed=False,
            serviceable=False,
            manager_verdicts=verdicts,
            reason_codes=(),
            warnings=tuple(warnings),
            epoch=epoch,
            monotonic_ns=_time.monotonic_ns(),
            wall_utc=_dt.now(_tz.utc).isoformat(),
            correlation_id=correlation_id,
        )

    # ------------------------------------------------------------------
    # Compute boot_allowed / serviceable from required verdicts
    # ------------------------------------------------------------------
    if required:
        boot_allowed = all(v.boot_allowed for v in required)
        serviceable = any(v.serviceable for v in required)
    else:
        # allow_empty_required=True path
        boot_allowed = True
        serviceable = True

    # ------------------------------------------------------------------
    # Worst state among required managers (deterministic tiebreak)
    # ------------------------------------------------------------------
    if required:
        worst = max(
            required,
            key=lambda v: (
                SEVERITY_MAP.get(v.state, 3),
                0 if v.retryable else 1,
                v.monotonic_ns,
            ),
        )
        worst_state = worst.state
    else:
        worst_state = SubsystemState.READY

    # ------------------------------------------------------------------
    # Collect reason codes from required verdicts with severity > 0
    # ------------------------------------------------------------------
    # Map each reason code to the maximum severity it appears with.
    code_max_severity: dict[VerdictReasonCode, int] = {}
    for v in required:
        sev = SEVERITY_MAP.get(v.state, 3)
        if sev > 0:
            cur = code_max_severity.get(v.reason_code, -1)
            if sev > cur:
                code_max_severity[v.reason_code] = sev

    reason_codes = tuple(
        sorted(
            code_max_severity.keys(),
            key=lambda c: (-code_max_severity[c], c.value),
        )
    )

    # ------------------------------------------------------------------
    # Warnings from non-required managers with severity > 0
    # ------------------------------------------------------------------
    warnings_list: list[VerdictWarning] = []
    for name, v in non_required:
        sev = SEVERITY_MAP.get(v.state, 3)
        if sev > 0:
            warnings_list.append(
                VerdictWarning(
                    code=v.reason_code.value,
                    detail=v.reason_detail,
                    origin=name,
                )
            )

    return PhaseVerdict(
        phase_name=phase_name,
        state=worst_state,
        boot_allowed=boot_allowed,
        serviceable=serviceable,
        manager_verdicts=verdicts,
        reason_codes=reason_codes,
        warnings=tuple(warnings_list),
        epoch=epoch,
        monotonic_ns=_time.monotonic_ns(),
        wall_utc=_dt.now(_tz.utc).isoformat(),
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Frozen dataclasses (immutable value objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessIdentity:
    """Uniquely identifies a running process instance."""

    pid: int
    start_time_ns: int
    session_id: str
    exec_fingerprint: str


@dataclass(frozen=True)
class LifecycleVerdict:
    """A decision issued by the root authority about a subsystem."""

    subsystem: str
    identity: ProcessIdentity
    action: LifecycleAction
    reason: str
    reason_code: str
    correlation_id: str
    incident_id: str
    exit_code: Optional[int]
    observed_at_ns: int
    wall_time_utc: str


@dataclass(frozen=True)
class ExecutionResult:
    """Result of executing a LifecycleVerdict."""

    accepted: bool
    executed: bool
    result: str
    new_identity: Optional[ProcessIdentity]
    error_code: Optional[str]
    correlation_id: str


@dataclass(frozen=True)
class ContractGate:
    """Defines the contract a subsystem must satisfy at handshake time."""

    subsystem: str
    expected_schema_version: str
    expected_capability_hash: Optional[str]
    required_health_fields: frozenset
    required_endpoints: frozenset

    def is_schema_compatible(self, actual: str) -> bool:
        """Check N/N-1 minor-version compatibility.

        Rules:
          * Major versions must match exactly.
          * Minor versions may differ by at most 1.
          * Patch versions are ignored.
        """
        expected_parts = self.expected_schema_version.split(".")
        actual_parts = actual.split(".")

        if len(expected_parts) < 2 or len(actual_parts) < 2:
            return False

        expected_major, expected_minor = int(expected_parts[0]), int(expected_parts[1])
        actual_major, actual_minor = int(actual_parts[0]), int(actual_parts[1])

        if expected_major != actual_major:
            return False

        return abs(expected_minor - actual_minor) <= 1


@dataclass(frozen=True)
class LifecycleEvent:
    """An auditable event emitted during lifecycle management."""

    event_type: str
    subsystem: str
    correlation_id: str
    session_id: str
    identity: Optional[ProcessIdentity]
    from_state: Optional[SubsystemState]
    to_state: Optional[SubsystemState]
    verdict_action: Optional[LifecycleAction]
    reason_code: Optional[str]
    exit_code: Optional[int]
    observed_at_ns: int
    wall_time_utc: str
    policy_source: str

# ---------------------------------------------------------------------------
# Mutable policy dataclasses (configurable, not frozen)
# ---------------------------------------------------------------------------


@dataclass
class TimeoutPolicy:
    """Timeout thresholds for subsystem lifecycle management."""

    startup_grace_s: float = 120.0
    health_timeout_s: float = 5.0
    health_poll_interval_s: float = 5.0
    drain_timeout_s: float = 30.0
    term_timeout_s: float = 10.0
    degraded_tolerance_s: float = 60.0
    degraded_recovery_check_s: float = 10.0


def _default_no_restart_codes() -> Tuple[int, ...]:
    return tuple([0] + list(range(100, 110)))


def _default_retry_codes() -> Tuple[int, ...]:
    return tuple(range(200, 210))


@dataclass
class RestartPolicy:
    """Restart strategy with exponential backoff and jitter."""

    max_restarts: int = 3
    window_s: float = 300.0
    base_delay_s: float = 2.0
    max_delay_s: float = 60.0
    jitter_factor: float = 0.3
    no_restart_exit_codes: Tuple[int, ...] = field(default_factory=_default_no_restart_codes)
    retry_exit_codes: Tuple[int, ...] = field(default_factory=_default_retry_codes)

    def compute_delay(self, attempt: int) -> float:
        """Compute the restart delay for the given attempt number.

        Uses exponential backoff: ``base_delay * 2^attempt``, optionally
        jittered by ``jitter_factor``, capped at ``max_delay_s``.
        """
        raw = self.base_delay_s * (2 ** attempt)
        capped = min(raw, self.max_delay_s)
        if self.jitter_factor > 0:
            lo = capped * (1.0 - self.jitter_factor)
            hi = capped * (1.0 + self.jitter_factor)
            return random.uniform(lo, hi)  # noqa: S311 – not crypto
        return capped

    def should_restart(self, exit_code: int) -> bool:
        """Decide whether a process with *exit_code* should be restarted.

        Returns ``False`` for codes in ``no_restart_exit_codes``,
        ``True`` otherwise (including codes in ``retry_exit_codes`` and
        unknown codes).
        """
        if exit_code in self.no_restart_exit_codes:
            return False
        return True

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def compute_exec_fingerprint(binary_path: str, cmdline: Sequence[str]) -> str:
    """Return a 16-hex-char SHA-256 digest of *binary_path* + *cmdline*."""
    h = hashlib.sha256()
    h.update(binary_path.encode("utf-8"))
    for arg in cmdline:
        h.update(b"\x00")
        h.update(arg.encode("utf-8"))
    return h.hexdigest()[:16]


def compute_capability_hash(capabilities: Dict[str, object]) -> str:
    """Return a deterministic 16-hex-char SHA-256 of *capabilities*.

    Keys are sorted to ensure identical output regardless of insertion
    order.
    """
    canonical = json.dumps(capabilities, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def compute_incident_id(
    subsystem: str,
    identity: ProcessIdentity,
    reason_code: str,
    time_ns: int,
) -> str:
    """Compute a dedup key for incidents within a 60-second bucket.

    Two calls with the same (subsystem, identity, reason_code) inside the
    same 60-second window will return the same ID.
    """
    bucket = time_ns // 60_000_000_000  # 60s in nanoseconds
    h = hashlib.sha256()
    h.update(subsystem.encode("utf-8"))
    h.update(str(identity.pid).encode("utf-8"))
    h.update(str(identity.start_time_ns).encode("utf-8"))
    h.update(identity.session_id.encode("utf-8"))
    h.update(reason_code.encode("utf-8"))
    h.update(str(bucket).encode("utf-8"))
    return h.hexdigest()[:24]
