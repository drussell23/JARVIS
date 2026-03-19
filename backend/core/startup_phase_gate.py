"""Dependency-aware phase gate coordinator for Disease 10 Startup Sequencing.

Provides a :class:`PhaseGateCoordinator` that enforces a strict dependency
graph across startup phases.  Each phase must have all predecessor phases
in a terminal state (PASSED or SKIPPED) before it can resolve.

The coordinator is fully async-aware: callers can ``await wait_for(phase)``
and will be unblocked as soon as the gate transitions out of PENDING.

Observability is built-in via an append-only event log and a point-in-time
snapshot helper.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

__all__ = [
    "StartupPhase",
    "GateStatus",
    "GateFailureReason",
    "GateResult",
    "GateSnapshot",
    "GateEvent",
    "PhaseGateCoordinator",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class StartupPhase(Enum):
    """Ordered startup phases with dependency metadata."""

    PREWARM_GCP = "prewarm_gcp"
    CORE_SERVICES = "core_services"
    BOOT_CONTRACT_VALIDATION = "boot_contract_validation"
    CORE_READY = "core_ready"
    DEFERRED_COMPONENTS = "deferred_components"

    @property
    def dependencies(self) -> Tuple[StartupPhase, ...]:
        """Return the phases that must be PASSED or SKIPPED before this one."""
        return _PHASE_DEPS[self]


# Dependency graph — kept outside the enum body for clarity.
_PHASE_DEPS: Dict[StartupPhase, Tuple[StartupPhase, ...]] = {
    StartupPhase.PREWARM_GCP:              (),
    StartupPhase.CORE_SERVICES:            (StartupPhase.PREWARM_GCP,),
    StartupPhase.BOOT_CONTRACT_VALIDATION: (StartupPhase.CORE_SERVICES,),
    StartupPhase.CORE_READY:               (StartupPhase.BOOT_CONTRACT_VALIDATION,),
    StartupPhase.DEFERRED_COMPONENTS:      (StartupPhase.CORE_READY,),
}


class GateStatus(str, Enum):
    """Lifecycle status of a single phase gate."""

    PENDING = "pending"
    PASSED = "passed"
    SKIPPED = "skipped"
    FAILED = "failed"


class GateFailureReason(str, Enum):
    """Machine-readable reason for a gate failure."""

    DEPENDENCY_UNMET = "dependency_unmet"
    TIMEOUT = "timeout"
    QUOTA_EXCEEDED = "quota_exceeded"
    NETWORK_ERROR = "network_error"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    PREEMPTION = "preemption"
    HEALTH_CHECK_FAILED = "health_check_failed"
    MANUAL = "manual"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    """Immutable result of a gate transition attempt."""

    phase: StartupPhase
    status: GateStatus
    failure_reason: Optional[GateFailureReason] = None
    detail: str = ""
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class GateSnapshot:
    """Point-in-time view of a single gate."""

    status: GateStatus
    timestamp: Optional[float] = None
    failure_reason: Optional[GateFailureReason] = None


@dataclass(frozen=True)
class GateEvent:
    """Append-only audit entry for a gate transition."""

    phase: StartupPhase
    new_status: GateStatus
    failure_reason: Optional[GateFailureReason] = None
    detail: str = ""
    timestamp: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class PhaseGateCoordinator:
    """Enforces a dependency-aware phase gate protocol.

    All phases start as :attr:`GateStatus.PENDING`.  Callers advance gates
    via :meth:`resolve`, :meth:`skip`, or :meth:`fail`.  An async
    :meth:`wait_for` blocks until the gate leaves PENDING (or times out).
    """

    def __init__(self) -> None:
        self._statuses: Dict[StartupPhase, GateStatus] = {
            phase: GateStatus.PENDING for phase in StartupPhase
        }
        self._timestamps: Dict[StartupPhase, Optional[float]] = {
            phase: None for phase in StartupPhase
        }
        self._failure_reasons: Dict[StartupPhase, Optional[GateFailureReason]] = {
            phase: None for phase in StartupPhase
        }
        self._details: Dict[StartupPhase, str] = {
            phase: "" for phase in StartupPhase
        }
        # Lazily-created asyncio.Event per phase — avoids binding to the
        # wrong event loop when the coordinator is constructed before the
        # loop that will actually drive it (common with pytest-asyncio).
        self._async_events: Dict[StartupPhase, asyncio.Event] = {}
        self._log: List[GateEvent] = []

    def _get_event(self, phase: StartupPhase) -> asyncio.Event:
        """Return (or lazily create) the asyncio.Event for *phase*.

        Events are created on first access so they bind to the currently
        running event loop rather than whatever loop existed at
        ``__init__`` time.
        """
        try:
            return self._async_events[phase]
        except KeyError:
            evt = asyncio.Event()
            self._async_events[phase] = evt
            # If the gate has already been set (via a sync call before any
            # async waiter appeared), mark the event immediately.
            if self._statuses[phase] != GateStatus.PENDING:
                evt.set()
            return evt

    # -- Public queries -----------------------------------------------------

    def status(self, phase: StartupPhase) -> GateStatus:
        """Return the current status of *phase*."""
        return self._statuses[phase]

    @property
    def event_log(self) -> List[GateEvent]:
        """Return a *copy* of the event log."""
        return list(self._log)

    def snapshot(self) -> Dict[StartupPhase, GateSnapshot]:
        """Return a point-in-time snapshot of every gate."""
        return {
            phase: GateSnapshot(
                status=self._statuses[phase],
                timestamp=self._timestamps[phase],
                failure_reason=self._failure_reasons[phase],
            )
            for phase in StartupPhase
        }

    # -- Public mutations ---------------------------------------------------

    def resolve(self, phase: StartupPhase, detail: str = "") -> GateResult:
        """Attempt to mark *phase* as PASSED.

        All dependencies must already be PASSED or SKIPPED.  If any
        dependency is still PENDING or FAILED the gate is marked FAILED
        with :attr:`GateFailureReason.DEPENDENCY_UNMET`.
        """
        unmet = [
            dep
            for dep in phase.dependencies
            if self._statuses[dep] not in (GateStatus.PASSED, GateStatus.SKIPPED)
        ]
        if unmet:
            unmet_names = ", ".join(dep.value for dep in unmet)
            fail_detail = f"unmet dependencies: {unmet_names}"
            if detail:
                fail_detail = f"{detail} ({fail_detail})"
            logger.warning(
                "Phase %s cannot resolve — %s", phase.value, fail_detail,
            )
            return self._set(
                phase,
                GateStatus.FAILED,
                failure_reason=GateFailureReason.DEPENDENCY_UNMET,
                detail=fail_detail,
            )

        logger.info("Phase %s resolved: %s", phase.value, detail or "(no detail)")
        return self._set(phase, GateStatus.PASSED, detail=detail)

    def skip(self, phase: StartupPhase, reason: str = "") -> GateResult:
        """Mark *phase* as SKIPPED.  Dependents may still proceed."""
        logger.info("Phase %s skipped: %s", phase.value, reason or "(no reason)")
        return self._set(phase, GateStatus.SKIPPED, detail=reason)

    def fail(
        self,
        phase: StartupPhase,
        reason: GateFailureReason,
        detail: str = "",
    ) -> GateResult:
        """Explicitly fail *phase* with a categorised reason."""
        logger.warning(
            "Phase %s failed (%s): %s", phase.value, reason.value, detail,
        )
        return self._set(phase, GateStatus.FAILED, failure_reason=reason, detail=detail)

    async def wait_for(
        self,
        phase: StartupPhase,
        timeout: float,
    ) -> GateResult:
        """Block until *phase* leaves PENDING, or until *timeout* seconds elapse.

        On timeout the gate is set to FAILED with
        :attr:`GateFailureReason.TIMEOUT`.
        """
        evt = self._get_event(phase)
        if evt.is_set():
            # Already resolved — return immediately.
            return GateResult(
                phase=phase,
                status=self._statuses[phase],
                failure_reason=self._failure_reasons[phase],
                detail=self._details[phase],
            )
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Phase %s timed out after %.2fs", phase.value, timeout,
            )
            return self._set(
                phase,
                GateStatus.FAILED,
                failure_reason=GateFailureReason.TIMEOUT,
                detail=f"timed out after {timeout:.2f}s",
            )
        return GateResult(
            phase=phase,
            status=self._statuses[phase],
            failure_reason=self._failure_reasons[phase],
            detail=self._details[phase],
        )

    # -- Internal helpers ---------------------------------------------------

    def _set(
        self,
        phase: StartupPhase,
        status: GateStatus,
        *,
        failure_reason: Optional[GateFailureReason] = None,
        detail: str = "",
    ) -> GateResult:
        """Atomically mutate gate state, signal waiters, and record an event."""
        current = self._statuses[phase]
        if current != GateStatus.PENDING:
            logger.warning(
                "Phase %s already in terminal state %s; ignoring transition to %s",
                phase.value, current.value, status.value,
            )
            return GateResult(
                phase=phase, status=current,
                failure_reason=self._failure_reasons[phase],
                detail=self._details[phase],
            )
        now = time.monotonic()
        self._statuses[phase] = status
        self._timestamps[phase] = now
        self._failure_reasons[phase] = failure_reason
        self._details[phase] = detail

        # Signal anyone awaiting this gate — lazy-init safe.
        if phase in self._async_events:
            self._async_events[phase].set()

        result = GateResult(
            phase=phase,
            status=status,
            failure_reason=failure_reason,
            detail=detail,
            timestamp=now,
        )
        self._record(
            GateEvent(
                phase=phase,
                new_status=status,
                failure_reason=failure_reason,
                detail=detail,
                timestamp=now,
            )
        )
        return result

    def _record(self, event: GateEvent) -> None:
        """Append an event to the internal log."""
        self._log.append(event)
