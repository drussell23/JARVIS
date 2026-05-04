"""Upgrade 1 Slice 4 — Provider integration bridge (PRD §31.2).

Single-import-point wire-up between the EpistemicBudget hook
(:mod:`epistemic_budget_executor_hook`) and the provider call
sites (Claude provider in :mod:`providers`, DW provider in
:mod:`doubleword_provider`). Both providers wrap
``self._tool_loop.run(...)`` and have a context object with
``provider_route`` + ``op_id`` + ``risk_tier``. This bridge:

  1. **Idempotent op-start**: opens the tracker for the op_id at
     call time (no-op when master flag off; safe to call
     repeatedly because :class:`EpistemicBudgetTracker` reopens
     atomically).

  2. **Per-round callback factory**: returns a
     ``Callable[[int], Awaitable[bool]]`` bound to the supplied
     ``op_id`` + ``current_risk_tier``. The callback is suitable
     for passing directly to a hypothetical ``tool_loop.run(...,
     pre_round_callback=...)`` extension OR for invocation by a
     provider-side wrapper that loops through rounds explicitly.

     **The callback returns True when the round-loop should
     break** (CONVERGED / EXHAUSTED_APPROVAL_REQUIRED). Callers
     that don't yet expose round-boundary callbacks can ignore
     the factory — Slice 4 ships infra-first; the provider-side
     loop expansion lands in Slice 5 graduation alongside the
     master flag flip.

  3. **SSE publication**: every non-WITHIN_BUDGET / non-DISABLED
     dispatch fires :func:`publish_budget_action_event` so
     operators see a live trail.

This module owns the ONLY production-side entry to the
EpistemicBudget machinery — providers import this, NOT the
hook directly. Keeps the hook a pure dispatch primitive and
keeps wire-up policy (when to publish SSE / what to log)
centralized.

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib + ``epistemic_budget`` +
    ``epistemic_budget_executor_hook`` +
    ``ide_observability_stream`` ONLY.
  * NEVER imports orchestrator / iron_gate /
    candidate_generator / providers / urgency_router /
    semantic_guardian / tool_executor / change_engine /
    subagent_scheduler / auto_action_router / policy.
  * Never mutates ctx — providers extract route + op_id +
    risk_tier and pass them as scalars.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from backend.core.ouroboros.governance.epistemic_budget import (
    EpistemicBudgetTracker,
    epistemic_budget_enabled,
    get_default_tracker,
)
from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (
    BudgetDispatchResult,
    OrangeQueueProtocol,
    ProbeRunnerProtocol,
    SBTRunnerProtocol,
    apply_budget_decision,
    note_round_complete,
    open_op_tracker,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    publish_budget_action_event,
)

logger = logging.getLogger(__name__)


# Callback signature — receives the round_index (post-increment)
# and returns True when the round loop should break (e.g.,
# CONVERGED / EXHAUSTED_APPROVAL_REQUIRED). False / None means
# continue. Mirrors the ``tool_executor`` cancel_token check
# pattern.
PreRoundCallback = Callable[[int], Awaitable[bool]]


def _publish_if_significant(
    result: BudgetDispatchResult, *, op_id: str,
) -> None:
    """Emit ``budget_action_taken`` SSE for any outcome that
    isn't a no-op (WITHIN_BUDGET / DISABLED). NEVER raises."""
    try:
        outcome_value = (
            result.action.outcome.value
            if hasattr(result.action.outcome, "value")
            else str(result.action.outcome)
        )
    except Exception:  # noqa: BLE001 — defensive
        outcome_value = "unknown"
    if outcome_value in ("within_budget", "disabled"):
        return
    try:
        publish_budget_action_event(
            outcome=outcome_value,
            reason=result.action.reason or "",
            op_id=op_id,
            new_risk_tier=result.new_risk_tier,
            extra_telemetry=dict(result.extra_telemetry or {}),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[epistemic_budget_provider_bridge] publish "
            "raised", exc_info=True,
        )


def attach_to_provider_run(
    *,
    op_id: str,
    route: str,
    risk_tier: str,
    tracker: Optional[EpistemicBudgetTracker] = None,
    probe_runner: Optional[ProbeRunnerProtocol] = None,
    sbt_runner: Optional[SBTRunnerProtocol] = None,
    orange_queue: Optional[OrangeQueueProtocol] = None,
) -> Optional[PreRoundCallback]:
    """**Slice 4 production wire-up entry point.**

    Called by Claude/DW providers immediately before
    ``self._tool_loop.run(...)``. When the master flag is on,
    opens the tracker and returns a per-round callback. When
    off, returns None and the caller proceeds without budget
    consultation (zero behavior change).

    Args mirror the data already in scope at the provider call
    site (``ctx.op_id`` / ``ctx.provider_route`` /
    ``ctx.risk_tier``). Runners are caller-injected via
    Protocol — providers wire real :class:`Confidence-
    ProbeRunner` / :class:`SpeculativeBranchRunner` /
    :class:`OrangePRReviewer` instances.

    NEVER raises."""
    try:
        if not epistemic_budget_enabled():
            return None
    except Exception:  # noqa: BLE001 — defensive
        return None

    resolved_tracker = (
        tracker
        if tracker is not None
        else get_default_tracker()
    )

    # Idempotent op-start. Returns False on garbage input or
    # tracker error — degrade gracefully (no callback).
    try:
        opened = open_op_tracker(
            resolved_tracker,
            op_id=op_id,
            route=route,
            risk_tier=risk_tier,
        )
    except Exception:  # noqa: BLE001 — defensive
        opened = False
    if not opened:
        return None

    # Closure capturing the tracker + runners. tool_executor
    # invokes this after each completed round; the closure
    # increments the round counter, dispatches the budget
    # decision, publishes SSE, and returns True to break the
    # round loop on terminal outcomes.
    async def _per_round_callback(round_index: int) -> bool:
        try:
            note_round_complete(
                resolved_tracker, op_id=op_id,
            )
            result = await apply_budget_decision(
                tracker=resolved_tracker,
                op_id=op_id,
                current_risk_tier=risk_tier,
                probe_runner=probe_runner,
                sbt_runner=sbt_runner,
                orange_queue=orange_queue,
            )
            _publish_if_significant(result, op_id=op_id)
            return bool(result.break_round_loop)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[epistemic_budget_provider_bridge] callback "
                "raised at round=%d op=%s", round_index, op_id,
                exc_info=True,
            )
            return False

    return _per_round_callback


def close_op(
    *,
    op_id: str,
    tracker: Optional[EpistemicBudgetTracker] = None,
) -> None:
    """Idempotent tracker close — providers call this in their
    ``finally`` block after ``tool_loop.run(...)``. NEVER
    raises."""
    try:
        if not epistemic_budget_enabled():
            return
        resolved_tracker = (
            tracker
            if tracker is not None
            else get_default_tracker()
        )
        resolved_tracker.close(op_id)
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[epistemic_budget_provider_bridge] close raised",
            exc_info=True,
        )


__all__ = [
    "PreRoundCallback",
    "attach_to_provider_run",
    "close_op",
]
