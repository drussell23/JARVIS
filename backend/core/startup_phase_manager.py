"""backend/core/startup_phase_manager.py — Diseases 1 + 3: sub-phase manager.

Problems solved
---------------
Disease 1 — Single monolithic phase timeout
    All 20+ startup components share one 360-second deadline.  A single slow
    component consumes the entire budget.  Now each phase and each component
    within it carries an independent timeout.

Disease 3 — No degradation hierarchy (all-or-nothing)
    Either every component succeeds or startup aborts.  Now each phase carries
    a ``PhasePolicy`` that controls how many components must succeed, and
    ``can_proceed()`` returns ``False`` only when the minimum bar is not met.

Architecture
------------

                     StartupPhaseManager
                    ┌───────────────────────────────────────────┐
                    │  execute_phase(config, tasks)             │
                    │  ├─► Phase: "infrastructure"  REQUIRED_ALL│
                    │  ├─► Phase: "voice"     REQUIRED_QUORUM   │
                    │  ├─► Phase: "intelligence"  BEST_EFFORT   │
                    │  └─► Phase: "agentic"   BEST_EFFORT       │
                    └───────────────────────────────────────────┘
                              │
                    can_proceed(result) ──► True | False

Usage::

    manager = StartupPhaseManager()

    result = await manager.execute_phase(
        PhaseConfig("infrastructure", timeout_s=120.0, policy=PhasePolicy.REQUIRED_ALL),
        {"cloud_sql_proxy": proxy_coro, "cloud_ml_router": router_coro},
    )
    if not manager.can_proceed(result):
        raise SystemExit("Infrastructure failed — cannot start JARVIS")
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

__all__ = [
    "PhasePolicy",
    "PhaseConfig",
    "TaskOutcome",
    "ComponentResult",
    "PhaseResult",
    "StartupPhaseManager",
]

logger = logging.getLogger(__name__)

# Callable or plain coroutine/awaitable accepted as a task.
_TaskLike = Union[Callable[[], Awaitable[Any]], Awaitable[Any]]


# ---------------------------------------------------------------------------
# Phase policy
# ---------------------------------------------------------------------------


class PhasePolicy(str, enum.Enum):
    """Success criterion for a startup phase."""

    REQUIRED_ALL = "required_all"
    """Every component must succeed.  Use for infrastructure tier
    (DB proxy, ML router, GCP VM manager)."""

    REQUIRED_QUORUM = "required_quorum"
    """At least ``quorum_pct`` percent must succeed.  Use for voice tier
    where one failing service can be gracefully degraded."""

    BEST_EFFORT = "best_effort"
    """All failures are tolerated and startup always proceeds.
    Use for optional intelligence / agentic tiers."""


# ---------------------------------------------------------------------------
# Task outcome
# ---------------------------------------------------------------------------


class TaskOutcome(str, enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SHED = "shed"  # dropped by MemoryGate under memory pressure


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ComponentResult:
    """Outcome of one component's initialisation within a phase."""

    component: str
    outcome: TaskOutcome
    duration_s: float
    error: Optional[str] = None


@dataclass
class PhaseResult:
    """Aggregated result of an entire startup phase."""

    phase: str
    policy: PhasePolicy
    quorum_pct: float = 75.0  # only meaningful when policy == REQUIRED_QUORUM
    succeeded: List[ComponentResult] = field(default_factory=list)
    failed: List[ComponentResult] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def success_count(self) -> int:
        return len(self.succeeded)

    @property
    def failure_count(self) -> int:
        return len(self.failed)

    @property
    def total_count(self) -> int:
        return self.success_count + self.failure_count

    @property
    def success_pct(self) -> float:
        if self.total_count == 0:
            return 100.0
        return (self.success_count / self.total_count) * 100.0


# ---------------------------------------------------------------------------
# Phase configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseConfig:
    """Immutable configuration for one startup phase.

    Parameters
    ----------
    name:
        Human-readable phase name (e.g. ``"infrastructure"``).
    timeout_s:
        Maximum wall-clock seconds the entire phase may consume.
    policy:
        Success criterion governing ``can_proceed`` evaluation.
    quorum_pct:
        Minimum success percentage for ``REQUIRED_QUORUM`` policy.
    component_timeout_s:
        Per-component timeout.  Defaults to ``timeout_s`` when ``None``.
    """

    name: str
    timeout_s: float = 120.0
    policy: PhasePolicy = PhasePolicy.BEST_EFFORT
    quorum_pct: float = 75.0
    component_timeout_s: Optional[float] = None
    stale_enforcement_s: Optional[float] = None
    """When set, a beacon stale-monitor runs alongside each component task.
    If the component's ``ComponentHealthBeacon`` reports no heartbeat for
    ``stale_enforcement_s`` seconds, the component task is cancelled and
    returned as ``TaskOutcome.TIMED_OUT``.  When ``None`` (default), stale
    detection is advisory only (Nuance 2 fix)."""


# ---------------------------------------------------------------------------
# StartupPhaseManager
# ---------------------------------------------------------------------------


class StartupPhaseManager:
    """Executes startup phases with independent timeouts and degradation logic.

    ``execute_phase`` runs all provided tasks concurrently, applies per-
    component timeouts, and returns a ``PhaseResult``.  ``can_proceed``
    evaluates the result against the phase policy.

    State
    -----
    ``degradation_level``
        Accumulated failure count across all phases (0 = nominal).
    ``phase_history``
        Ordered list of every ``PhaseResult`` produced in this cycle.
    """

    def __init__(self) -> None:
        self._degradation_level: int = 0
        self._phase_history: List[PhaseResult] = []

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def execute_phase(
        self,
        config: PhaseConfig,
        tasks: Dict[str, _TaskLike],
    ) -> PhaseResult:
        """Run all tasks concurrently inside the phase timeout.

        Parameters
        ----------
        config:
            Phase policy, timeouts, and name.
        tasks:
            ``{component_name: coroutine_or_factory}``.
            A zero-arg callable is invoked to produce the coroutine;
            a plain awaitable is used directly.

        Returns
        -------
        PhaseResult
            All per-component outcomes.  This method never raises —
            all errors are captured into ``ComponentResult.error``.
        """
        comp_timeout = config.component_timeout_s or config.timeout_s
        phase_start = time.monotonic()

        logger.info(
            "[PhaseManager] starting phase '%s': %d component(s), "
            "phase_timeout=%.0fs, comp_timeout=%.0fs, policy=%s",
            config.name, len(tasks),
            config.timeout_s, comp_timeout,
            config.policy.value,
        )

        async def _run_one(name: str, task: _TaskLike) -> ComponentResult:
            start = time.monotonic()

            # Stale-enforcement monitor (Nuance 2).
            # Runs concurrently; cancels comp_task if no beacon heartbeat.
            stale_cancelled = False

            async def _stale_monitor(comp_task: "asyncio.Task[ComponentResult]") -> None:
                nonlocal stale_cancelled
                # Lazy import avoids circular dependency.
                from backend.core.component_health_beacon import get_beacon_registry  # noqa: PLC0415
                beacon = get_beacon_registry().get_or_create(name)
                poll = max(1.0, config.stale_enforcement_s / 10.0)  # type: ignore[operator]
                while not comp_task.done():
                    await asyncio.sleep(poll)
                    if not comp_task.done() and beacon.is_stalled(
                        config.stale_enforcement_s  # type: ignore[arg-type]
                    ):
                        logger.error(
                            "[PhaseManager] [%s] '%s' STALE — cancelling "
                            "(no beacon heartbeat for %.0fs)",
                            config.name, name, config.stale_enforcement_s,
                        )
                        stale_cancelled = True
                        comp_task.cancel()
                        return

            try:
                coro = task() if callable(task) else task
                comp_future = asyncio.ensure_future(
                    asyncio.wait_for(coro, timeout=comp_timeout)
                )

                monitor: Optional["asyncio.Task[None]"] = None
                if config.stale_enforcement_s is not None:
                    monitor = asyncio.ensure_future(_stale_monitor(comp_future))

                try:
                    await comp_future
                finally:
                    if monitor is not None and not monitor.done():
                        monitor.cancel()

                dur = time.monotonic() - start
                logger.info(
                    "[PhaseManager] [%s] '%s' OK in %.3fs",
                    config.name, name, dur,
                )
                return ComponentResult(name, TaskOutcome.SUCCESS, dur)
            except asyncio.TimeoutError:
                dur = time.monotonic() - start
                logger.error(
                    "[PhaseManager] [%s] '%s' TIMED OUT after %.3fs",
                    config.name, name, dur,
                )
                return ComponentResult(
                    name, TaskOutcome.TIMED_OUT, dur,
                    error=f"component timeout after {comp_timeout:.0f}s",
                )
            except asyncio.CancelledError:
                dur = time.monotonic() - start
                # If stale monitor cancelled us, return TIMED_OUT (not CANCELLED).
                if stale_cancelled:
                    return ComponentResult(
                        name, TaskOutcome.TIMED_OUT, dur,
                        error=f"stale enforcement: no beacon for {config.stale_enforcement_s:.0f}s",
                    )
                return ComponentResult(
                    name, TaskOutcome.CANCELLED, dur, error="cancelled",
                )
            except Exception as exc:
                dur = time.monotonic() - start
                # Detect MemoryGateRefused without importing (avoid circular)
                outcome = (
                    TaskOutcome.SHED
                    if type(exc).__name__ == "MemoryGateRefused"
                    else TaskOutcome.FAILED
                )
                logger.error(
                    "[PhaseManager] [%s] '%s' %s: %s",
                    config.name, name, outcome.value, exc,
                )
                return ComponentResult(name, outcome, dur, error=str(exc))

        gather_aws = [_run_one(name, task) for name, task in tasks.items()]

        try:
            results: List[ComponentResult] = await asyncio.wait_for(
                asyncio.gather(*gather_aws),
                timeout=config.timeout_s,
            )
        except asyncio.TimeoutError:
            # Phase-level deadline expired.  asyncio.gather has been cancelled.
            # Build a synthetic PhaseResult marking all components timed-out.
            phase_dur = time.monotonic() - phase_start
            logger.error(
                "[PhaseManager] phase '%s' DEADLINE EXCEEDED after %.0fs — "
                "all remaining components marked TIMED_OUT",
                config.name, phase_dur,
            )
            phase_result = PhaseResult(
                phase=config.name,
                policy=config.policy,
                quorum_pct=config.quorum_pct,
                failed=[
                    ComponentResult(
                        name, TaskOutcome.TIMED_OUT, phase_dur,
                        error="phase-level deadline",
                    )
                    for name in tasks
                ],
                duration_s=phase_dur,
            )
            self._phase_history.append(phase_result)
            return phase_result

        phase_dur = time.monotonic() - phase_start
        succeeded = [r for r in results if r.outcome == TaskOutcome.SUCCESS]
        failed = [r for r in results if r.outcome != TaskOutcome.SUCCESS]

        phase_result = PhaseResult(
            phase=config.name,
            policy=config.policy,
            quorum_pct=config.quorum_pct,
            succeeded=succeeded,
            failed=failed,
            duration_s=phase_dur,
        )
        self._phase_history.append(phase_result)

        logger.info(
            "[PhaseManager] phase '%s' done in %.3fs: %d ok / %d failed "
            "(%.0f%% success)",
            config.name, phase_dur,
            phase_result.success_count, phase_result.failure_count,
            phase_result.success_pct,
        )
        return phase_result

    def can_proceed(self, result: PhaseResult) -> bool:
        """Evaluate *result* against its policy.

        Returns ``True``  — startup may proceed (possibly degraded).
        Returns ``False`` — hard failure, startup must abort.

        Also increments ``degradation_level`` whenever failures are present.
        """
        policy = result.policy

        if policy == PhasePolicy.BEST_EFFORT:
            if result.failure_count:
                self._degradation_level += result.failure_count
            return True

        if policy == PhasePolicy.REQUIRED_ALL:
            if result.failure_count == 0:
                return True
            self._degradation_level += result.failure_count
            logger.error(
                "[PhaseManager] phase '%s': REQUIRED_ALL — %d failure(s): %s",
                result.phase,
                result.failure_count,
                [r.component for r in result.failed],
            )
            return False

        # REQUIRED_QUORUM
        if result.success_pct >= result.quorum_pct:
            if result.failure_count:
                self._degradation_level += result.failure_count
            return True

        self._degradation_level += result.failure_count
        logger.error(
            "[PhaseManager] phase '%s': REQUIRED_QUORUM %.0f%% not met — "
            "only %.0f%% succeeded (%d/%d)",
            result.phase,
            result.quorum_pct,
            result.success_pct,
            result.success_count,
            result.total_count,
        )
        return False

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def degradation_level(self) -> int:
        """Accumulated failure count across all phases.  0 = nominal."""
        return self._degradation_level

    @property
    def phase_history(self) -> List[PhaseResult]:
        """Ordered list of all completed PhaseResult objects (copy)."""
        return list(self._phase_history)

    def reset(self) -> None:
        """Reset manager state — call at the start of each DMS restart cycle."""
        self._degradation_level = 0
        self._phase_history.clear()
        logger.info("[PhaseManager] reset for restart cycle")
