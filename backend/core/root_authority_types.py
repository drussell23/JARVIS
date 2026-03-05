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
from typing import Dict, Optional, Sequence, Tuple

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
