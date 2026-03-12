"""backend/core/ouroboros/governance/autonomy/execution_monitor.py

Execution Monitoring — Multi-Dimensional Outcome Classification for L3 SafetyNet.

Extracted from legacy simulator.py execution tracking, adapted for the C+
autonomous loop's advisory safety layer. NO references to legacy modules.

Provides:
    - ExecutionStatus: 9-state execution outcome enum
    - ExecutionConstraints: configurable safety limits
    - ExecutionOutcome: per-operation outcome record
    - ExecutionMonitor: bounded ring buffer + aggregate statistics
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.ExecutionMonitor")


# ---------------------------------------------------------------------------
# ExecutionStatus
# ---------------------------------------------------------------------------


class ExecutionStatus(Enum):
    """Multi-dimensional execution outcome classification.

    Goes beyond simple pass/fail to distinguish resource limit violations,
    security violations, and in-progress states.
    """

    PENDING = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    TIMEOUT = auto()
    MEMORY_EXCEEDED = auto()
    DEPTH_EXCEEDED = auto()
    ITERATION_EXCEEDED = auto()
    SECURITY_VIOLATION = auto()


# Pre-computed sets for O(1) membership checks
_NON_TERMINAL_STATUSES = frozenset({ExecutionStatus.PENDING, ExecutionStatus.RUNNING})

_RESOURCE_VIOLATION_STATUSES = frozenset({
    ExecutionStatus.TIMEOUT,
    ExecutionStatus.MEMORY_EXCEEDED,
    ExecutionStatus.DEPTH_EXCEEDED,
    ExecutionStatus.ITERATION_EXCEEDED,
})


# ---------------------------------------------------------------------------
# ExecutionConstraints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionConstraints:
    """Safety constraints for execution monitoring.

    Adapted from legacy SimulatorConfig — keeps the essential bounds
    that SafetyNet needs for constraint checking.
    """

    max_execution_time_s: float = 30.0
    max_memory_mb: int = 512
    max_call_depth: int = 100
    max_iterations: int = 100_000
    max_output_bytes: int = 1_048_576  # 1 MB


# ---------------------------------------------------------------------------
# ExecutionOutcome
# ---------------------------------------------------------------------------


@dataclass
class ExecutionOutcome:
    """Record of a single operation's execution outcome.

    Simplified from legacy ExecutionTrace -- keeps essential monitoring data,
    drops the heavyweight tracing/profiling fields that SafetyNet doesn't need.
    """

    op_id: str
    status: ExecutionStatus
    start_ns: int  # monotonic_ns
    end_ns: int = 0  # monotonic_ns, 0 if still running
    duration_ms: float = 0.0
    error_message: str = ""
    memory_peak_mb: float = 0.0
    call_depth: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """True if execution has ended (not PENDING/RUNNING)."""
        return self.status not in _NON_TERMINAL_STATUSES

    @property
    def is_resource_violation(self) -> bool:
        """True if failed due to resource limit (timeout, memory, depth, iterations)."""
        return self.status in _RESOURCE_VIOLATION_STATUSES


# ---------------------------------------------------------------------------
# ExecutionMonitor
# ---------------------------------------------------------------------------


class ExecutionMonitor:
    """Tracks execution outcomes and detects patterns for L3 SafetyNet.

    Maintains a bounded ring buffer of recent outcomes and computes
    aggregate statistics for SafetyNet decision-making.

    Parameters
    ----------
    max_outcomes:
        Maximum number of outcomes retained in the ring buffer.
        Oldest outcomes are pruned when capacity is reached.
    constraints:
        Safety constraints used by :meth:`check_constraints`.
        Defaults to :class:`ExecutionConstraints` defaults.
    """

    def __init__(
        self,
        max_outcomes: int = 100,
        constraints: Optional[ExecutionConstraints] = None,
    ) -> None:
        self._outcomes: List[ExecutionOutcome] = []
        self._max_outcomes = max_outcomes
        self._constraints = constraints or ExecutionConstraints()
        # Lifetime counts survive ring buffer pruning
        self._status_counts: Dict[ExecutionStatus, int] = defaultdict(int)
        self._total_recorded: int = 0

    @property
    def total_recorded(self) -> int:
        """Lifetime count of recorded outcomes (survives ring buffer pruning)."""
        return self._total_recorded

    # ------------------------------------------------------------------
    # record
    # ------------------------------------------------------------------

    def record(self, outcome: ExecutionOutcome) -> None:
        """Record an execution outcome. Prunes oldest if at capacity."""
        self._outcomes.append(outcome)
        self._status_counts[outcome.status] += 1
        self._total_recorded += 1

        # Prune oldest to maintain bounded ring buffer
        if len(self._outcomes) > self._max_outcomes:
            excess = len(self._outcomes) - self._max_outcomes
            self._outcomes = self._outcomes[excess:]

        logger.debug(
            "ExecutionMonitor: recorded op_id=%s status=%s (buffer=%d, lifetime=%d)",
            outcome.op_id,
            outcome.status.name,
            len(self._outcomes),
            self._total_recorded,
        )

    # ------------------------------------------------------------------
    # classify_from_payload
    # ------------------------------------------------------------------

    def classify_from_payload(self, payload: Dict[str, Any]) -> ExecutionStatus:
        """Classify an execution status from an event payload.

        Maps common payload fields to ExecutionStatus:
        - payload["success"] == True -> COMPLETED
        - "timeout" in error -> TIMEOUT
        - "memory" in error -> MEMORY_EXCEEDED
        - "depth" or "recursion" in error -> DEPTH_EXCEEDED
        - "iteration" or "loop" in error -> ITERATION_EXCEEDED
        - "security" or "permission" in error -> SECURITY_VIOLATION
        - else -> FAILED
        """
        if payload.get("success"):
            return ExecutionStatus.COMPLETED

        error = payload.get("error", "").lower()

        if "timeout" in error:
            return ExecutionStatus.TIMEOUT
        if "memory" in error:
            return ExecutionStatus.MEMORY_EXCEEDED
        if "depth" in error or "recursion" in error:
            return ExecutionStatus.DEPTH_EXCEEDED
        if "iteration" in error or "loop" in error:
            return ExecutionStatus.ITERATION_EXCEEDED
        if "security" in error or "permission" in error:
            return ExecutionStatus.SECURITY_VIOLATION

        return ExecutionStatus.FAILED

    # ------------------------------------------------------------------
    # check_constraints
    # ------------------------------------------------------------------

    def check_constraints(self, outcome: ExecutionOutcome) -> List[str]:
        """Check if outcome violates any safety constraints.

        Returns a list of violation keys (empty if no violations):
        - "execution_time": duration exceeds max_execution_time_s
        - "memory": memory peak exceeds max_memory_mb
        - "call_depth": call depth exceeds max_call_depth
        """
        violations: List[str] = []

        duration_s = outcome.duration_ms / 1000.0
        if duration_s > self._constraints.max_execution_time_s:
            violations.append("execution_time")

        if outcome.memory_peak_mb > self._constraints.max_memory_mb:
            violations.append("memory")

        if outcome.call_depth > self._constraints.max_call_depth:
            violations.append("call_depth")

        return violations

    # ------------------------------------------------------------------
    # get_failure_rate
    # ------------------------------------------------------------------

    def get_failure_rate(self, window: int = 20) -> float:
        """Failure rate across last N outcomes (0.0-1.0).

        An outcome is considered a failure if its status is terminal
        and not COMPLETED.
        """
        recent = self._outcomes[-window:] if self._outcomes else []
        if not recent:
            return 0.0

        failures = sum(
            1
            for o in recent
            if o.is_terminal and o.status != ExecutionStatus.COMPLETED
        )
        return failures / len(recent)

    # ------------------------------------------------------------------
    # get_resource_violation_rate
    # ------------------------------------------------------------------

    def get_resource_violation_rate(self, window: int = 20) -> float:
        """Rate of resource violations in last N outcomes (0.0-1.0)."""
        recent = self._outcomes[-window:] if self._outcomes else []
        if not recent:
            return 0.0

        violations = sum(1 for o in recent if o.is_resource_violation)
        return violations / len(recent)

    # ------------------------------------------------------------------
    # get_status_distribution
    # ------------------------------------------------------------------

    def get_status_distribution(self) -> Dict[str, int]:
        """Lifetime distribution of execution statuses.

        Returns a dict mapping status name strings to counts.
        Only includes statuses that have been observed at least once.
        """
        return {
            status.name: count
            for status, count in self._status_counts.items()
            if count > 0
        }

    # ------------------------------------------------------------------
    # get_recent_outcomes
    # ------------------------------------------------------------------

    def get_recent_outcomes(self, limit: int = 10) -> List[ExecutionOutcome]:
        """Return most recent outcomes, up to *limit*."""
        return self._outcomes[-limit:]

    # ------------------------------------------------------------------
    # to_dict
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Snapshot for telemetry.

        Keys: failure_rate, resource_violation_rate, total_recorded,
        status_distribution.
        """
        return {
            "failure_rate": self.get_failure_rate(),
            "resource_violation_rate": self.get_resource_violation_rate(),
            "total_recorded": self._total_recorded,
            "status_distribution": self.get_status_distribution(),
        }
