"""Move 6.5 PLAN-phase canonical integration seam.

The **one** shared async helper composing the Slice 1–4 substrate
into the orchestrator's PLAN-phase pipeline. Used by BOTH
:mod:`phase_runners.plan_runner` (the Slice 3-default-on path) AND
the inline orchestrator PLAN path (the legacy fall-back used when
``JARVIS_PHASE_RUNNER_SLICE3_FULLY_EXTRACTED=false``). One helper,
two callers — no duplication.

Architecture
------------

When the master flag AND-composition is on AND
:func:`multi_prior_planning.materialize_priors` returns a non-None
:class:`PriorSet` (route=complex + posture=EXPLORE gates), this
helper:

1. Implements the :class:`MultiPriorGenerator` protocol as a
   closure over the supplied :class:`PlanGenerator` instance,
   threading each :class:`Prior`'s ``system_prompt_addendum`` +
   ``seed`` into the per-roll ``PlanGenerator.generate_plan``
   invocation (real prior angles, not cosmetic noise — per
   operator binding 2026-05-10).

2. Awaits :func:`dispatch_multi_prior` (Slice 3) which composes
   the K-roll quorum runner (Slice 2) over the priors.

3. On ``ACCEPT_CANONICAL`` or ``CLAMP_TO_NOTIFY_APPLY``
   recommendation: rehydrates the consensus winner's plan_json
   string through the canonical
   :meth:`PlanGenerator._parse_plan_response` +
   :meth:`PlanGenerator._validate_plan_coherence` paths — same
   field-extraction as a single-shot ``generate_plan`` call (no
   parallel parser). Stamps ``ui_affected`` via the canonical
   :func:`classify_ui_affected` and ``planning_duration_s`` from
   the elapsed wall-clock.

4. Always invokes :func:`record_dispatch_outcome` so the
   graduation ledger accumulates observations when the observer
   master flag is on (the 4th flag — observer-flag-gated at the
   recorder's own master check; this seam invokes the recorder
   unconditionally).

5. On ``ESCALATE_TO_OPERATOR_REVIEW`` (full disagreement) OR
   ``FALL_THROUGH``: returns None so the caller runs the
   single-shot path. Escalation surfaces through the SSE event
   (``EVENT_TYPE_MULTI_PRIOR_DISPATCH``) and the recorded
   verdict; the pipeline continues with a single-shot plan to
   keep the op moving.

Master-flag gate AND-composition (no parallel flag):
  * JARVIS_MULTI_PRIOR_PLANNING_ENABLED (Slice 1 materializer)
  * JARVIS_MULTI_PRIOR_RUNNER_ENABLED (Slice 2 runner)
  * JARVIS_MULTI_PRIOR_DISPATCH_ENABLED (Slice 3 dispatch)
  * JARVIS_MULTI_PRIOR_OBSERVER_ENABLED (Slice 4 ledger — gates
    record_dispatch_outcome internally)

When ANY gate is off, this helper returns None and the caller's
single-shot path runs unchanged — zero behavior change at default.

Authority asymmetry
-------------------

This module is an INTEGRATION ADAPTER. It does NOT:

* Implement consensus math (Move 6 substrate owns that).
* Parse plan JSON (PlanGenerator owns that — we delegate).
* Validate plan coherence (PlanGenerator owns that — we delegate).
* Touch the FSM / orchestrator / iron_gate / providers directly
  (read-only composition of canonical surfaces).

It DOES:

* Implement the MultiPriorGenerator protocol as a closure.
* Invoke dispatch_multi_prior + record_dispatch_outcome.
* Rehydrate the consensus winner via PlanGenerator's own
  parser + validator.
* Stamp the same ui_affected / duration / heartbeat fields the
  single-shot path stamps (matches operator binding 2026-05-10
  clarification #2: "the same path you already run after a
  normal generate_plan").
"""
from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.MultiPriorPlanSeam")


MULTI_PRIOR_PLAN_SEAM_SCHEMA_VERSION: str = (
    "multi_prior_plan_seam.v1"
)


async def dispatch_plan_with_multi_prior(
    *,
    ctx: Any,
    plan_generator: Any,
    deadline: datetime,
    posture_str: Optional[str] = None,
) -> Optional[Any]:
    """Canonical PLAN-phase integration seam for Move 6.5.

    Returns:

      * ``None`` — multi-prior gates are off OR consensus
        non-actionable; caller MUST run the single-shot
        ``plan_generator.generate_plan(ctx, deadline)`` path.

      * :class:`PlanResult` — multi-prior dispatched, consensus
        was actionable, and the helper rehydrated the winner via
        the canonical PlanGenerator parser + validator. Caller
        uses this as the PLAN-phase output unchanged.

    NEVER raises into the caller. Any internal exception logs a
    debug line and returns None (single-shot fallback). The async
    cancellation contract is preserved — asyncio.CancelledError
    re-raised per asyncio convention.

    Operator binding 2026-05-10 (Move 6.5 PLAN-phase seam):
      * Real prior angles via system_prompt_addendum + seed
        threading — no cosmetic noise
      * Rehydration through PlanGenerator._parse_plan_response +
        _validate_plan_coherence — no parallel parser
      * Same ui_affected / duration stamping as single-shot path
      * record_dispatch_outcome called unconditionally
        (observer flag gates internally)
      * Master-flag AND-composition handled by
        dispatch_multi_prior's internal gate
    """
    # Lazy imports — keep the substrate authority-free import-time.
    # Defensive: any ImportError → None (caller does single-shot).
    try:
        from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
            dispatch_multi_prior,
            ConsensusActionRecommendation,
        )
        from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
            record_dispatch_outcome,
        )
    except ImportError as exc:
        logger.debug(
            "[MultiPriorPlanSeam] substrate unavailable: %s; "
            "falling through to single-shot",
            exc,
        )
        return None

    op_id = str(getattr(ctx, "op_id", "") or "")
    if not op_id:
        return None

    route = str(getattr(ctx, "provider_route", "") or "standard")
    posture = (
        str(posture_str) if posture_str is not None
        else str(getattr(ctx, "posture", "") or "MAINTAIN")
    )

    seam_t0 = time.monotonic()

    # Implement the MultiPriorGenerator protocol as a closure
    # over `plan_generator` + `ctx`. Each per-roll invocation
    # threads the Prior's system_prompt_addendum into a freshly
    # replaced ctx (immutable composition), then awaits the
    # canonical generate_plan. Returns the plan_json string —
    # the protocol's diff-shaped artifact (which Slice 2's
    # runner SHA-256-hashes for AST signature equivalence).
    async def _per_roll_plan_generator(
        *, prior: Any, roll_id: str,
    ) -> str:
        # Operator binding clarification #4: real prior angles.
        # The substrate-supplied `system_prompt_addendum` IS the
        # canonical prior-angle signal (empty for SEED_ONLY,
        # non-empty for STYLE_HINT entries from
        # STYLE_HINT_TABLE). Prepend to ctx.description so the
        # PlanGenerator's existing prompt-builder picks it up
        # naturally — no PlanGenerator API change required.
        addendum = str(
            getattr(prior, "system_prompt_addendum", "") or "",
        )
        seed = int(getattr(prior, "seed", 0) or 0)
        existing_desc = str(getattr(ctx, "description", "") or "")
        augmented = (
            f"[prior:{prior.prior_id}] {addendum}\n{existing_desc}"
            if addendum
            else f"[prior:{prior.prior_id} seed={seed}] {existing_desc}"
        )
        # dataclasses.replace preserves the rest of ctx — no
        # mutation of the original.
        try:
            roll_ctx = dataclasses.replace(
                ctx, description=augmented,
            )
        except Exception:  # noqa: BLE001
            # Defensive: if ctx isn't a dataclass, use ctx as-is.
            roll_ctx = ctx
        # Invoke the canonical PlanGenerator — no parallel
        # planner, no parsing here.
        try:
            result = await plan_generator.generate_plan(
                roll_ctx, deadline,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[MultiPriorPlanSeam] per-roll planner failed "
                "op=%s prior_id=%s roll=%s: %s",
                op_id, prior.prior_id, roll_id, exc,
            )
            return ""
        # Protocol return: the plan_json string. Slice 2 runner
        # hashes this for AST signature; identical plan_json → strong
        # unanimity signal. Empty/skipped results return "" which
        # the runner correctly excludes from consensus clustering.
        return str(getattr(result, "plan_json", "") or "")

    # Dispatch — the one composition call. Master-flag AND-gating
    # happens inside dispatch_multi_prior's internal
    # evaluate_dispatch_decision check.
    try:
        verdict = await dispatch_multi_prior(
            generator=_per_roll_plan_generator,
            op_id=op_id,
            route=route,
            posture=posture,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[MultiPriorPlanSeam] dispatch_multi_prior raised "
            "op=%s: %s",
            op_id, exc,
        )
        return None

    # Always record — observer flag gates the recorder internally
    # (returns None when observer master is off). This is what
    # drives graduation ledger growth (Slice 6 readiness).
    try:
        record_dispatch_outcome(verdict)
    except Exception:  # noqa: BLE001
        pass

    # Action recommendation branching — closed 4-value taxonomy
    # from multi_prior_dispatch:
    #   ACCEPT_CANONICAL          — unanimous; use the winner
    #   CLAMP_TO_NOTIFY_APPLY     — majority; use the winner
    #                               (risk-tier clamp is downstream
    #                               GATE phase's concern; we just
    #                               return the plan)
    #   ESCALATE_TO_OPERATOR_REVIEW — full disagreement; fall
    #                               through to single-shot
    #   FALL_THROUGH              — disabled/failed; single-shot
    action = verdict.action_recommendation
    if action not in (
        ConsensusActionRecommendation.ACCEPT_CANONICAL,
        ConsensusActionRecommendation.CLAMP_TO_NOTIFY_APPLY,
    ):
        return None

    # Look up the winning roll's plan_json from verdict_result.
    if not verdict.fired:
        return None
    vr = verdict.verdict_result
    consensus_verdict = getattr(vr, "consensus_verdict", None)
    if consensus_verdict is None:
        return None
    accepted_roll_id = getattr(
        consensus_verdict, "accepted_roll_id", None,
    )
    if not accepted_roll_id:
        return None
    rolls = getattr(vr, "rolls", ()) or ()
    winning_roll = None
    for roll in rolls:
        if getattr(roll, "roll_id", "") == accepted_roll_id:
            winning_roll = roll
            break
    if winning_roll is None:
        return None
    candidate_diff = str(
        getattr(winning_roll, "candidate_diff", "") or "",
    )
    if not candidate_diff:
        return None

    # REHYDRATE via the canonical PlanGenerator parser. Per
    # operator binding clarification #2: "the same path you
    # already run after a normal generate_plan". No parallel
    # field extraction.
    try:
        plan_result = plan_generator._parse_plan_response(
            candidate_diff,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[MultiPriorPlanSeam] rehydration parse failed "
            "op=%s: %s",
            op_id, exc,
        )
        return None

    # Run canonical coherence validation — same path as
    # single-shot. PlanGenerator._validate_plan_coherence
    # mutates the PlanResult (logs warnings; doesn't raise).
    try:
        plan_generator._validate_plan_coherence(plan_result)
    except Exception:  # noqa: BLE001
        # Coherence validation is advisory; don't block on it.
        pass

    # Stamp ui_affected via the canonical classifier (matches
    # single-shot path's discipline).
    try:
        from backend.core.ouroboros.governance.plan_generator import (
            classify_ui_affected,
        )
        plan_result.ui_affected = bool(
            classify_ui_affected(
                getattr(ctx, "target_files", []) or [],
                getattr(plan_result, "approach", "") or "",
            )
        )
    except Exception:  # noqa: BLE001
        # ui_affected defaults to False on classifier failure.
        pass

    # Stamp planning duration with the seam's actual wall-clock
    # (matches single-shot's discipline of measuring real elapsed,
    # not the per-roll sum).
    try:
        plan_result.planning_duration_s = (
            time.monotonic() - seam_t0
        )
    except Exception:  # noqa: BLE001
        pass

    logger.info(
        "[MultiPriorPlanSeam] consensus PLAN accepted op=%s "
        "action=%s prior_id=%s elapsed=%.2fs",
        op_id, action.value,
        getattr(winning_roll, "prior_id", "?"),
        plan_result.planning_duration_s,
    )
    return plan_result


def _snapshot_for_telemetry(verdict: Any) -> Dict[str, Any]:
    """Compact dict for ledger / SSE / log telemetry. NEVER raises."""
    try:
        return {
            "op_id": str(getattr(verdict, "op_id", "")),
            "decision": str(
                getattr(verdict.decision, "value", "")
                if hasattr(verdict, "decision") else "",
            ),
            "action": str(
                getattr(verdict.action_recommendation, "value", "")
                if hasattr(verdict, "action_recommendation") else "",
            ),
            "fired": bool(getattr(verdict, "fired", False)),
            "schema_version": (
                MULTI_PRIOR_PLAN_SEAM_SCHEMA_VERSION
            ),
        }
    except Exception:  # noqa: BLE001
        return {
            "schema_version": MULTI_PRIOR_PLAN_SEAM_SCHEMA_VERSION,
        }


__all__ = [
    "MULTI_PRIOR_PLAN_SEAM_SCHEMA_VERSION",
    "dispatch_plan_with_multi_prior",
]
