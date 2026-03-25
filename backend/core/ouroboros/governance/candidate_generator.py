"""
Candidate Generator & Failback State Machine
=============================================

Routes code generation requests to a primary provider (GCP J-Prime) or a
fallback provider (local model).  The :class:`FailbackStateMachine` prevents
flapping by requiring N consecutive health probes over a dwell period before
restoring the primary provider.

Key Design Decisions
--------------------

- **Asymmetric transitions**: failover is immediate (one failure triggers
  switch to fallback), but failback requires ``required_probes`` consecutive
  health checks spanning at least ``dwell_time_s`` seconds.
- **Concurrency quotas**: separate :class:`asyncio.Semaphore` instances for
  primary and fallback, preventing thundering-herd overload.
- **Deadline propagation**: every call computes remaining time from the
  caller-supplied deadline and applies it as an ``asyncio.wait_for`` timeout.
- **QUEUE_ONLY**: when both providers are down, the generator raises
  immediately rather than blocking -- the orchestrator is expected to queue
  the operation for later retry.

State Diagram
-------------

.. code-block:: text

    PRIMARY_READY ---[primary_failure]---> FALLBACK_ACTIVE
         ^                                      |     |
         |                                      |     +--[fallback_failure]--> QUEUE_ONLY
         |                              [probe_success]
         |                                      |
         |                                      v
         +---[N probes + dwell met]--- PRIMARY_DEGRADED
                                          |
                                  [probe_failure]
                                          |
                                          v
                                   FALLBACK_ACTIVE

Components
----------

- :class:`CandidateProvider` -- runtime-checkable protocol for generation backends
- :class:`FailbackState` -- 4-state enum
- :class:`FailbackStateMachine` -- asymmetric failover/failback logic
- :class:`CandidateGenerator` -- orchestration layer with concurrency and deadline
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Optional, Protocol, runtime_checkable

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Content failure classification
# ---------------------------------------------------------------------------

# Keywords that identify content/model failures vs infrastructure failures.
# Content failures do NOT trigger FailbackFSM state transitions — the primary
# provider is still alive; it merely produced bad output (stale diff, invalid
# schema, etc.).  Infrastructure failures (timeout, connection error) DO
# trigger state transitions.
_CONTENT_FAILURE_PATTERNS: frozenset = frozenset({
    "diff_apply_failed",
    "stale_diff",
    "schema_invalid",
    "no_candidates",
    "validate_diff",
    "StaleDiffError",
})


def _is_content_failure(exc: BaseException) -> bool:
    """Return True if *exc* is a content/model failure (not infrastructure).

    Content failures: wrong diff, stale context, invalid JSON schema.
    Infrastructure failures: timeout, connection refused, OOM.
    """
    msg = str(exc).lower()
    return any(pattern.lower() in msg for pattern in _CONTENT_FAILURE_PATTERNS)


# ---------------------------------------------------------------------------
# CandidateProvider Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CandidateProvider(Protocol):
    """Runtime-checkable protocol for code generation backends.

    Any class that implements these methods can serve as a primary or
    fallback generation provider.
    """

    @property
    def provider_name(self) -> str:
        """Human-readable name of this provider (e.g. ``"gcp-jprime"``)."""
        ...  # pragma: no cover

    async def generate(
        self, context: OperationContext, deadline: datetime
    ) -> GenerationResult:
        """Generate candidate code changes for the given operation.

        Parameters
        ----------
        context:
            The operation context describing what needs to change.
        deadline:
            Absolute UTC deadline by which generation must complete.

        Returns
        -------
        GenerationResult
            The generated candidates with timing metadata.

        Raises
        ------
        Exception
            Any failure (timeout, OOM, network) should propagate as an exception.
        """
        ...  # pragma: no cover

    async def health_probe(self) -> bool:
        """Quick liveness check.

        Returns
        -------
        bool
            ``True`` if the provider is healthy and ready to serve requests.
        """
        ...  # pragma: no cover

    async def plan(self, prompt: str, deadline: datetime) -> str:
        """Send a lightweight planning prompt; return the raw string response.

        Used by ContextExpander. Planning failures are soft — callers tolerate
        exceptions and skip expansion rounds gracefully.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# FailbackState Enum
# ---------------------------------------------------------------------------


class FailbackState(Enum):
    """States in the failover/failback state machine."""

    PRIMARY_READY = auto()
    FALLBACK_ACTIVE = auto()
    PRIMARY_DEGRADED = auto()
    QUEUE_ONLY = auto()


# ---------------------------------------------------------------------------
# FailbackStateMachine
# ---------------------------------------------------------------------------


class FailbackStateMachine:
    """Asymmetric failover/failback state machine.

    Failover is immediate (one failure), but failback requires
    ``required_probes`` consecutive health probes spanning at least
    ``dwell_time_s`` seconds.

    Parameters
    ----------
    required_probes:
        Number of consecutive successful health probes needed before
        promoting from PRIMARY_DEGRADED to PRIMARY_READY.
    dwell_time_s:
        Minimum wall-clock seconds that must elapse between the first
        successful probe and the promotion to PRIMARY_READY.
    """

    def __init__(
        self,
        required_probes: int = 3,
        dwell_time_s: float = 45.0,
    ) -> None:
        self._state: FailbackState = FailbackState.PRIMARY_READY
        self._required_probes: int = required_probes
        self._dwell_time_s: float = dwell_time_s
        self._consecutive_probes: int = 0
        self._first_probe_at: Optional[float] = None  # monotonic timestamp
        self.content_failure_count: int = 0  # content/model failures (not infra)

    @property
    def state(self) -> FailbackState:
        """Current FSM state."""
        return self._state

    def record_primary_failure(self) -> None:
        """Record a primary provider failure.

        Transitions immediately to FALLBACK_ACTIVE from any non-QUEUE_ONLY state.
        """
        if self._state is FailbackState.QUEUE_ONLY:
            return
        if self._state in (
            FailbackState.PRIMARY_READY,
            FailbackState.FALLBACK_ACTIVE,
            FailbackState.PRIMARY_DEGRADED,
        ):
            self._state = FailbackState.FALLBACK_ACTIVE
            self._reset_probe_counters()
            logger.warning(
                "[FailbackFSM] Primary failure -> FALLBACK_ACTIVE"
            )

    def record_fallback_failure(self) -> None:
        """Record a fallback provider failure.

        FALLBACK_ACTIVE -> QUEUE_ONLY.
        """
        if self._state is FailbackState.FALLBACK_ACTIVE:
            self._state = FailbackState.QUEUE_ONLY
            self._reset_probe_counters()
            logger.error(
                "[FailbackFSM] Fallback failure -> QUEUE_ONLY (all providers exhausted)"
            )

    def record_probe_success(self) -> None:
        """Record a successful health probe of the primary provider.

        FALLBACK_ACTIVE -> PRIMARY_DEGRADED (first probe).
        PRIMARY_DEGRADED stays until required_probes AND dwell_time_s met,
        then -> PRIMARY_READY.
        PRIMARY_READY -> no-op.
        QUEUE_ONLY -> no-op.
        """
        if self._state is FailbackState.PRIMARY_READY:
            return
        if self._state is FailbackState.QUEUE_ONLY:
            return

        now = time.monotonic()

        if self._state is FailbackState.FALLBACK_ACTIVE:
            # First probe: transition to PRIMARY_DEGRADED
            self._state = FailbackState.PRIMARY_DEGRADED
            self._consecutive_probes = 1
            self._first_probe_at = now
            logger.info(
                "[FailbackFSM] First probe success -> PRIMARY_DEGRADED (1/%d)",
                self._required_probes,
            )
            self._maybe_promote(now)
            return

        if self._state is FailbackState.PRIMARY_DEGRADED:
            self._consecutive_probes += 1
            logger.info(
                "[FailbackFSM] Probe success (%d/%d)",
                self._consecutive_probes,
                self._required_probes,
            )
            self._maybe_promote(now)

    def record_probe_failure(self) -> None:
        """Record a failed health probe of the primary provider.

        PRIMARY_DEGRADED -> FALLBACK_ACTIVE (resets probe counters).
        Other states: no-op.
        """
        if self._state is FailbackState.PRIMARY_DEGRADED:
            self._state = FailbackState.FALLBACK_ACTIVE
            self._reset_probe_counters()
            logger.warning(
                "[FailbackFSM] Probe failure -> FALLBACK_ACTIVE (reset)"
            )

    def _maybe_promote(self, now: float) -> None:
        """Check if promotion criteria (probes + dwell) are met."""
        if self._state is not FailbackState.PRIMARY_DEGRADED:
            return
        if self._consecutive_probes < self._required_probes:
            return
        if self._first_probe_at is not None:
            elapsed = now - self._first_probe_at
            if elapsed < self._dwell_time_s:
                logger.info(
                    "[FailbackFSM] Probes met (%d/%d) but dwell not satisfied "
                    "(%.1fs / %.1fs)",
                    self._consecutive_probes,
                    self._required_probes,
                    elapsed,
                    self._dwell_time_s,
                )
                return
        # All criteria met
        self._state = FailbackState.PRIMARY_READY
        self._reset_probe_counters()
        logger.info("[FailbackFSM] Promoted -> PRIMARY_READY")

    def _reset_probe_counters(self) -> None:
        """Reset probe tracking state."""
        self._consecutive_probes = 0
        self._first_probe_at = None


# ---------------------------------------------------------------------------
# CandidateGenerator
# ---------------------------------------------------------------------------


class CandidateGenerator:
    """Orchestrates candidate generation with failover and concurrency control.

    Routes generation requests to the primary provider when healthy, falling
    back to the fallback provider on failure.  Each provider has its own
    :class:`asyncio.Semaphore` for concurrency limiting.

    Parameters
    ----------
    primary:
        The preferred (typically remote/powerful) generation provider.
    fallback:
        The backup (typically local/smaller) generation provider.
    primary_concurrency:
        Maximum concurrent calls to the primary provider.
    fallback_concurrency:
        Maximum concurrent calls to the fallback provider.
    """

    def __init__(
        self,
        primary: CandidateProvider,
        fallback: CandidateProvider,
        primary_concurrency: int = 4,
        fallback_concurrency: int = 2,
        tier0: Optional[Any] = None,  # DoublewordProvider (batch, async)
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._tier0 = tier0
        self._primary_sem = asyncio.Semaphore(primary_concurrency)
        self._fallback_sem = asyncio.Semaphore(fallback_concurrency)
        self.fsm = FailbackStateMachine()
        # Async Tier 0 tracking: op_id → CompletedBatch
        self._completed_batches: dict[str, Any] = {}
        # Background polling tasks (kept to prevent GC)
        self._background_polls: dict[str, asyncio.Task[Any]] = {}

    async def generate(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Generate candidate code changes, with automatic failover.

        Parameters
        ----------
        context:
            The operation context describing what needs to change.
        deadline:
            Absolute UTC deadline by which generation must complete.

        Returns
        -------
        GenerationResult
            The generated candidates from whichever provider succeeded.

        Raises
        ------
        RuntimeError
            If all providers are exhausted (``"all_providers_exhausted"``).
        asyncio.TimeoutError
            If the deadline is already past and no provider can be tried.
        """
        # Tier 0 (Doubleword batch) — async, non-blocking.
        # Complexity gate: only invoke the batch API for tasks that justify
        # the async latency cost. Simple tasks skip straight to Tier 1.
        #
        # Flow: submit batch (fast, <2s) → fire background poll task →
        #       fall through to Tier 1. If a previous Tier 0 result already
        #       completed for this op, use it directly.
        _TIER0_COMPLEXITY_CLASSES = frozenset({"heavy_code", "complex"})
        _op_id = getattr(context, "operation_id", "")
        if self._tier0 is not None and getattr(self._tier0, "is_available", False):
            # Check if a previous async batch already completed for this op
            _completed = self._completed_batches.pop(_op_id, None)
            if _completed is not None:
                _result = _completed.result
                if _result is not None and len(_result.candidates) > 0:
                    logger.info(
                        "[CandidateGenerator] Tier 0 async result available: "
                        "%d candidates (batch completed %.1fs ago)",
                        len(_result.candidates),
                        time.monotonic() - _completed.completed_at,
                    )
                    return _result

            # Determine if this operation qualifies for Tier 0 routing
            _complexity = ""
            if context.routing is not None:
                _complexity = getattr(context.routing, "task_complexity", "")
            _is_cross_repo = getattr(context, "cross_repo", False)
            _qualifies = _complexity in _TIER0_COMPLEXITY_CLASSES or _is_cross_repo

            if not _qualifies:
                logger.debug(
                    "[CandidateGenerator] Tier 0 skipped: complexity=%r, "
                    "cross_repo=%s — routing to Tier 1",
                    _complexity, _is_cross_repo,
                )
            else:
                # Async submit: fast path (<2s), then background poll
                try:
                    pending = await self._tier0.submit_batch(context)
                    if pending is not None:
                        logger.info(
                            "[CandidateGenerator] Tier 0 batch %s submitted async "
                            "(complexity=%s, cross_repo=%s) — falling through to Tier 1",
                            pending.batch_id, _complexity, _is_cross_repo,
                        )
                        # Fire background poll — result stored when ready
                        task = asyncio.create_task(
                            self._background_poll_tier0(pending, context),
                            name=f"dw-poll-{pending.batch_id[:12]}",
                        )
                        self._background_polls[_op_id] = task
                    else:
                        logger.info(
                            "[CandidateGenerator] Tier 0 batch submission failed, "
                            "falling through to Tier 1"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as t0_exc:
                    logger.warning(
                        "[CandidateGenerator] Tier 0 submit failed (%s), "
                        "falling through to Tier 1",
                        t0_exc,
                    )

        # Tier 1 + Tier 2: Primary (J-Prime) → Fallback (Claude)
        state = self.fsm.state

        if state is FailbackState.QUEUE_ONLY:
            raise RuntimeError("all_providers_exhausted")

        if state is FailbackState.PRIMARY_READY:
            return await self._try_primary_then_fallback(context, deadline)

        # FALLBACK_ACTIVE or PRIMARY_DEGRADED: use fallback directly
        return await self._call_fallback(context, deadline)

    async def _background_poll_tier0(
        self, pending: Any, context: OperationContext,
    ) -> None:
        """Background task: poll Doubleword batch and store result when ready."""
        _op_id = pending.op_id
        try:
            result = await self._tier0.poll_and_retrieve(pending, context)
            if result is not None and len(result.candidates) > 0:
                from backend.core.ouroboros.governance.doubleword_provider import (
                    CompletedBatch,
                )
                self._completed_batches[_op_id] = CompletedBatch(
                    op_id=_op_id,
                    batch_id=pending.batch_id,
                    result=result,
                    completed_at=time.monotonic(),
                )
                logger.info(
                    "[CandidateGenerator] Tier 0 background poll complete: "
                    "batch %s → %d candidates stored for op %s",
                    pending.batch_id, len(result.candidates), _op_id,
                )
            else:
                logger.info(
                    "[CandidateGenerator] Tier 0 background poll: "
                    "batch %s returned no usable candidates",
                    pending.batch_id,
                )
        except asyncio.CancelledError:
            logger.debug(
                "[CandidateGenerator] Tier 0 background poll cancelled: %s",
                pending.batch_id,
            )
        except Exception:
            logger.warning(
                "[CandidateGenerator] Tier 0 background poll failed: %s",
                pending.batch_id,
                exc_info=True,
            )
        finally:
            self._background_polls.pop(_op_id, None)

    def get_completed_batch(self, op_id: str) -> Optional[Any]:
        """Check if a Tier 0 async result is available for the given op_id."""
        return self._completed_batches.get(op_id)

    def pop_completed_batch(self, op_id: str) -> Optional[Any]:
        """Retrieve and remove a completed Tier 0 result for the given op_id."""
        return self._completed_batches.pop(op_id, None)

    async def run_health_probe(self) -> bool:
        """Probe the primary provider and update the FSM.

        Returns
        -------
        bool
            ``True`` if the primary is healthy.
        """
        try:
            healthy = await self._primary.health_probe()
        except Exception:
            logger.warning(
                "[CandidateGenerator] Health probe raised exception, treating as failure",
                exc_info=True,
            )
            healthy = False

        if healthy:
            self.fsm.record_probe_success()
        else:
            self.fsm.record_probe_failure()

        return healthy

    async def plan(self, prompt: str, deadline: datetime) -> str:
        """Send a planning prompt to the active provider, with soft fallback.

        Does NOT update the failback state machine on failure — planning errors
        are non-fatal and the orchestrator continues to GENERATE regardless.

        Raises RuntimeError("all_providers_exhausted") only if QUEUE_ONLY.
        """
        state = self.fsm.state

        if state is FailbackState.QUEUE_ONLY:
            raise RuntimeError("all_providers_exhausted")

        if state is FailbackState.PRIMARY_READY:
            try:
                remaining = self._remaining_seconds(deadline)
                async with self._primary_sem:
                    return await asyncio.wait_for(
                        self._primary.plan(prompt, deadline),
                        timeout=remaining,
                    )
            except (Exception, asyncio.CancelledError) as exc:
                logger.warning(
                    "[CandidateGenerator] Primary plan() failed (%s: %s), trying fallback",
                    type(exc).__name__,
                    exc,
                )

        # FALLBACK_ACTIVE, PRIMARY_DEGRADED, or primary plan() just failed
        remaining = self._remaining_seconds(deadline)
        async with self._fallback_sem:
            return await asyncio.wait_for(
                self._fallback.plan(prompt, deadline),
                timeout=remaining,
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _try_primary_then_fallback(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Try primary, fall back on any failure.

        Note: In Python 3.9, ``CancelledError`` is a ``BaseException`` (not
        ``Exception``), so we must catch it explicitly to handle
        ``asyncio.wait_for`` cancellation of the primary call.
        """
        try:
            return await self._call_primary(context, deadline)
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning(
                "[CandidateGenerator] Primary failed (%s: %s), falling back",
                type(exc).__name__,
                exc,
            )
            if _is_content_failure(exc):
                # Content failure: model produced bad output, but primary infra is healthy.
                # Do NOT penalise the FSM — only count for observability.
                self.fsm.content_failure_count += 1
                logger.info(
                    "[CandidateGenerator] Content failure (count=%d), FSM unchanged",
                    self.fsm.content_failure_count,
                )
            else:
                self.fsm.record_primary_failure()
            return await self._call_fallback(context, deadline)

    async def _call_primary(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Call primary provider with concurrency and deadline enforcement."""
        remaining = self._remaining_seconds(deadline)
        async with self._primary_sem:
            return await asyncio.wait_for(
                self._primary.generate(context, deadline),
                timeout=remaining,
            )

    async def _call_fallback(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Call fallback provider with concurrency and deadline enforcement."""
        try:
            remaining = self._remaining_seconds(deadline)
            async with self._fallback_sem:
                return await asyncio.wait_for(
                    self._fallback.generate(context, deadline),
                    timeout=remaining,
                )
        except (Exception, asyncio.CancelledError) as exc:
            logger.error(
                "[CandidateGenerator] Fallback also failed (%s: %s)",
                type(exc).__name__,
                exc,
            )
            self.fsm.record_fallback_failure()
            raise RuntimeError("all_providers_exhausted") from exc

    @staticmethod
    def _remaining_seconds(deadline: datetime) -> float:
        """Compute seconds remaining until *deadline*.

        Returns a non-negative float.  If the deadline has already passed,
        returns 0.0 (which will cause ``asyncio.wait_for`` to time out
        immediately).
        """
        now = datetime.now(tz=timezone.utc)
        remaining = (deadline - now).total_seconds()
        return max(remaining, 0.0)
