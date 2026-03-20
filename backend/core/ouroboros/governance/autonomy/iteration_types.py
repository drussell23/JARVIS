"""Iteration Mode data types, idempotency keys, and policies.

All dataclasses are frozen (immutable value objects) unless they need
mutable state (IterationBudgetWindow, TaskRejectionTracker).  No hardcoded
magic numbers — every default is a named constant or read from an env var
via ``IterationStopPolicy.from_env()``.
"""
from __future__ import annotations

import enum
import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class IterationState(enum.Enum):
    """10-state FSM for the Autonomy Iteration Mode lifecycle."""

    IDLE = "idle"
    SELECTING = "selecting"
    PLANNING = "planning"
    EXECUTING = "executing"
    RECOVERING = "recovering"
    EVALUATING = "evaluating"
    REVIEW_GATE = "review_gate"
    COOLDOWN = "cooldown"
    PAUSED = "paused"
    STOPPED = "stopped"


class RecoveryDecision(enum.Enum):
    """Outcome of the recovery classification step."""

    EVALUATE = "evaluate"
    RESUME = "resume"
    SKIP = "skip"
    PAUSE_IRRECOVERABLE = "pause_irrecoverable"


class PlannerRejectReason(str, enum.Enum):
    """Reason a planner rejected a task — used in poison tracking and audit."""

    ORACLE_NO_DATA = "oracle_no_data"
    BLAST_RADIUS_EXCEEDED = "blast_radius_exceeded"
    ZERO_ACTIONABLE_UNITS = "zero_actionable_units"
    DAG_CYCLE_DETECTED = "dag_cycle_detected"
    SNAPSHOT_STALE = "snapshot_stale"
    TRUST_GATE_DENIED = "trust_gate_denied"
    TASK_POISONED = "task_poisoned"


# ---------------------------------------------------------------------------
# Frozen dataclasses — value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IterationTask:
    """A unit of work proposed to the Autonomy Iteration planner.

    Parameters
    ----------
    task_id:
        Unique opaque identifier (e.g. ``"task-<uuidv7>"``).
    source:
        Origin sensor or subsystem that generated this task
        (e.g. ``"opportunity_miner"``, ``"test_failure_sensor"``).
    description:
        Human-readable description of the work.
    target_files:
        Tuple of relative file paths expected to be modified.
    repo:
        Repository key (matches ``RepoRegistry`` entry).
    priority:
        Numeric priority; higher values are preferred during selection.
    requires_human_ack:
        When ``True`` the task must not be applied without explicit
        human acknowledgement, regardless of autonomy tier.
    evidence:
        Arbitrary structured evidence from the originating sensor
        (e.g. test failure stack trace, opportunity description).
    """

    task_id: str
    source: str
    description: str
    target_files: Tuple[str, ...]
    repo: str
    priority: int = 0
    requires_human_ack: bool = False
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanningContext:
    """Snapshot of planning environment at plan-generation time.

    Carried alongside the plan so downstream stages can verify that
    the world has not changed since planning occurred.
    """

    repo_commit: str
    oracle_snapshot_id: str
    policy_hash: str
    schema_version: str
    trust_tier: AutonomyTier
    budget_remaining_usd: float


@dataclass(frozen=True)
class PlannedGraphMetadata:
    """Provenance record attached to a generated execution graph."""

    selection_proof: str
    expansion_proof: str
    partition_proof: str
    reject_reason_code: Optional[str]
    planning_context: Optional[PlanningContext]


@dataclass(frozen=True)
class PlannerOutcome:
    """Result returned by the iteration planner.

    Parameters
    ----------
    status:
        ``"accepted"`` or ``"rejected"``.
    graph:
        Execution graph object (type-erased to avoid circular imports).
        ``None`` when *status* is ``"rejected"``.
    reject_reason:
        Populated when *status* is ``"rejected"``.
    metadata:
        Provenance metadata.  May be ``None`` on rejection.
    """

    status: str
    graph: Optional[Any] = None
    reject_reason: Optional[PlannerRejectReason] = None
    metadata: Optional[PlannedGraphMetadata] = None


@dataclass(frozen=True)
class BlastRadiusPolicy:
    """Per-dimension blast radius limits for a planned iteration.

    Each ``check_*`` method returns ``None`` if the supplied value is
    within limits, or a non-empty violation message string if it exceeds
    the configured limit.  Returning ``Optional[str]`` keeps callers
    simple: ``if policy.check_file_count(n): raise ...``.
    """

    max_files_changed: int = 50
    max_public_api_files_touched: int = 10
    max_repos_touched: int = 3
    max_lines_changed: int = 2000

    def check_file_count(self, n: int) -> Optional[str]:
        if n > self.max_files_changed:
            return (
                f"file count {n} exceeds blast radius limit "
                f"{self.max_files_changed}"
            )
        return None

    def check_public_api_count(self, n: int) -> Optional[str]:
        if n > self.max_public_api_files_touched:
            return (
                f"public API file count {n} exceeds blast radius limit "
                f"{self.max_public_api_files_touched}"
            )
        return None

    def check_repos(self, n: int) -> Optional[str]:
        if n > self.max_repos_touched:
            return (
                f"repos touched {n} exceeds blast radius limit "
                f"{self.max_repos_touched}"
            )
        return None

    def check_lines(self, n: int) -> Optional[str]:
        if n > self.max_lines_changed:
            return (
                f"lines changed {n} exceeds blast radius limit "
                f"{self.max_lines_changed}"
            )
        return None


@dataclass(frozen=True)
class IterationStopPolicy:
    """Policy governing when an autonomy iteration session should stop.

    All fields have safe defaults.  Use :meth:`from_env` to allow
    runtime overrides without hardcoding.

    Parameters
    ----------
    max_iterations_per_session:
        Hard cap on the number of apply cycles per session.
    max_consecutive_failures:
        Stop after this many consecutive failed iterations.
    max_wall_time_s:
        Stop after this many seconds of elapsed wall time.
    max_spend_usd:
        Stop when estimated API spend exceeds this amount.
    cooldown_base_s:
        Base cooldown duration (seconds) after a failure.
    max_cooldown_s:
        Maximum cooldown duration (seconds) after back-off.
    miner_fairness_interval:
        How often (iterations) to rotate task-source priority to
        prevent a single sensor from monopolising the queue.
    blast_radius:
        Default :class:`BlastRadiusPolicy` applied during planning.
    """

    max_iterations_per_session: int = 25
    max_consecutive_failures: int = 3
    max_wall_time_s: float = 3600.0
    max_spend_usd: float = 5.0
    cooldown_base_s: float = 30.0
    max_cooldown_s: float = 300.0
    miner_fairness_interval: int = 5
    blast_radius: BlastRadiusPolicy = field(default_factory=BlastRadiusPolicy)

    @classmethod
    def from_env(cls) -> "IterationStopPolicy":
        """Build a policy from ``JARVIS_AUTONOMY_*`` environment variables.

        Falls back to class defaults for any variable that is absent or
        cannot be parsed as the expected type.
        """

        def _int(key: str, default: int) -> int:
            try:
                return int(os.environ[key])
            except (KeyError, ValueError):
                return default

        def _float(key: str, default: float) -> float:
            try:
                return float(os.environ[key])
            except (KeyError, ValueError):
                return default

        blast_radius = BlastRadiusPolicy(
            max_files_changed=_int(
                "JARVIS_AUTONOMY_BLAST_MAX_FILES", BlastRadiusPolicy.max_files_changed
            ),
            max_public_api_files_touched=_int(
                "JARVIS_AUTONOMY_BLAST_MAX_API_FILES",
                BlastRadiusPolicy.max_public_api_files_touched,
            ),
            max_repos_touched=_int(
                "JARVIS_AUTONOMY_BLAST_MAX_REPOS", BlastRadiusPolicy.max_repos_touched
            ),
            max_lines_changed=_int(
                "JARVIS_AUTONOMY_BLAST_MAX_LINES", BlastRadiusPolicy.max_lines_changed
            ),
        )

        return cls(
            max_iterations_per_session=_int(
                "JARVIS_AUTONOMY_MAX_ITERATIONS", cls.max_iterations_per_session
            ),
            max_consecutive_failures=_int(
                "JARVIS_AUTONOMY_MAX_CONSECUTIVE_FAILURES",
                cls.max_consecutive_failures,
            ),
            max_wall_time_s=_float(
                "JARVIS_AUTONOMY_MAX_WALL_TIME_S", cls.max_wall_time_s
            ),
            max_spend_usd=_float("JARVIS_AUTONOMY_MAX_SPEND_USD", cls.max_spend_usd),
            cooldown_base_s=_float(
                "JARVIS_AUTONOMY_COOLDOWN_BASE_S", cls.cooldown_base_s
            ),
            max_cooldown_s=_float(
                "JARVIS_AUTONOMY_MAX_COOLDOWN_S", cls.max_cooldown_s
            ),
            miner_fairness_interval=_int(
                "JARVIS_AUTONOMY_MINER_FAIRNESS_INTERVAL",
                cls.miner_fairness_interval,
            ),
            blast_radius=blast_radius,
        )


# ---------------------------------------------------------------------------
# Mutable state classes
# ---------------------------------------------------------------------------


@dataclass
class IterationBudgetWindow:
    """Tracks spend and iteration count within a rolling time window.

    The window resets automatically once it has expired.  The window
    duration is configurable and defaults to 24 hours so that
    ``JARVIS_AUTONOMY_BUDGET_WINDOW_H`` can adjust it without a code
    change.

    Parameters
    ----------
    window_start_utc:
        UTC datetime at which the current window began.
    spend_usd:
        Accumulated API spend (USD) within the current window.
    iterations_count:
        Number of completed iterations within the current window.
    window_hours:
        Duration of each budget window in hours (default 24).
    """

    window_start_utc: datetime
    spend_usd: float = 0.0
    iterations_count: int = 0
    window_hours: float = field(
        default_factory=lambda: float(
            os.environ.get("JARVIS_AUTONOMY_BUDGET_WINDOW_H", "24")
        )
    )

    def is_expired(self) -> bool:
        """Return ``True`` if the window has elapsed."""
        now = datetime.now(timezone.utc)
        window_end = self.window_start_utc + timedelta(hours=self.window_hours)
        return now >= window_end

    def reset_if_expired(self) -> bool:
        """Reset spend and iteration count if the window has expired.

        Returns ``True`` if a reset was performed.
        """
        if self.is_expired():
            self.window_start_utc = datetime.now(timezone.utc)
            self.spend_usd = 0.0
            self.iterations_count = 0
            return True
        return False


class TaskRejectionTracker:
    """Tracks per-task rejection history and enforces a poison threshold.

    A task is considered *poisoned* once it has been rejected at least
    ``poison_threshold`` times.  Poisoned tasks are excluded from future
    planning rounds until the tracker is reset (e.g. at session start).

    Parameters
    ----------
    poison_threshold:
        Number of rejections required to mark a task as poisoned.
    cooldown_s:
        Informational cooldown associated with a poisoned task.  Not
        enforced here — callers may use it to implement back-off.
    """

    def __init__(
        self,
        poison_threshold: int = 5,
        cooldown_s: float = 300.0,
    ) -> None:
        self.poison_threshold = poison_threshold
        self.cooldown_s = cooldown_s
        self._rejections: Dict[str, List[PlannerRejectReason]] = {}

    def record_rejection(
        self, task_id: str, reason: PlannerRejectReason
    ) -> None:
        """Record a rejection reason for *task_id*."""
        self._rejections.setdefault(task_id, []).append(reason)

    def is_poisoned(self, task_id: str) -> bool:
        """Return ``True`` if *task_id* has reached the poison threshold."""
        return len(self._rejections.get(task_id, [])) >= self.poison_threshold

    def get_reject_history(self, task_id: str) -> List[PlannerRejectReason]:
        """Return the list of rejection reasons for *task_id* (may be empty)."""
        return list(self._rejections.get(task_id, []))

    def reset(self, task_id: Optional[str] = None) -> None:
        """Clear rejection history for *task_id*, or all tasks if ``None``."""
        if task_id is None:
            self._rejections.clear()
        else:
            self._rejections.pop(task_id, None)


# ---------------------------------------------------------------------------
# Idempotency / fingerprint helpers
# ---------------------------------------------------------------------------


def compute_task_fingerprint(
    description: str,
    target_files: Tuple[str, ...],
) -> str:
    """Compute a stable SHA-256 fingerprint for a task description + file set.

    ``target_files`` is sorted before hashing so that order does not affect
    the fingerprint.

    Returns
    -------
    str
        Lowercase hex digest.
    """
    sorted_files = "\n".join(sorted(target_files))
    payload = f"{description}\n{sorted_files}"
    return hashlib.sha256(payload.encode()).hexdigest()


def compute_policy_hash(
    stop_policy: IterationStopPolicy,
    governance_mode: str,
) -> str:
    """Compute a stable SHA-256 hash of a stop policy + governance mode.

    Serialises the policy fields in deterministic order so that any
    change to policy parameters produces a different hash.

    Returns
    -------
    str
        Lowercase hex digest.
    """
    parts = [
        f"governance_mode={governance_mode}",
        f"max_iterations_per_session={stop_policy.max_iterations_per_session}",
        f"max_consecutive_failures={stop_policy.max_consecutive_failures}",
        f"max_wall_time_s={stop_policy.max_wall_time_s}",
        f"max_spend_usd={stop_policy.max_spend_usd}",
        f"cooldown_base_s={stop_policy.cooldown_base_s}",
        f"max_cooldown_s={stop_policy.max_cooldown_s}",
        f"miner_fairness_interval={stop_policy.miner_fairness_interval}",
        f"blast_max_files={stop_policy.blast_radius.max_files_changed}",
        f"blast_max_api_files={stop_policy.blast_radius.max_public_api_files_touched}",
        f"blast_max_repos={stop_policy.blast_radius.max_repos_touched}",
        f"blast_max_lines={stop_policy.blast_radius.max_lines_changed}",
    ]
    payload = "\n".join(parts)
    return hashlib.sha256(payload.encode()).hexdigest()


def compute_plan_id(
    fingerprint: str,
    policy_hash: str,
    repos: Tuple[str, ...],
) -> str:
    """Derive a stable, human-readable plan ID from its constituent inputs.

    The ID is deterministic: the same fingerprint, policy hash, and repo
    set always produce the same ID, enabling idempotent plan storage.

    Parameters
    ----------
    fingerprint:
        Task fingerprint from :func:`compute_task_fingerprint`.
    policy_hash:
        Policy hash from :func:`compute_policy_hash`.
    repos:
        Tuple of repository keys involved in the plan.  Sorted before
        hashing so ordering does not matter.

    Returns
    -------
    str
        A ``"plan-<hex>"`` string where ``<hex>`` is the first 16 chars
        of the combined SHA-256 digest.
    """
    sorted_repos = "\n".join(sorted(repos))
    payload = f"{fingerprint}\n{policy_hash}\n{sorted_repos}"
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"plan-{digest[:16]}"
