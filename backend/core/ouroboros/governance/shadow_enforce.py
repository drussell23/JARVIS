"""shadow_enforce -- promote the Phase B REVIEW + PLAN subagents from
SHADOW (observe-only) to ENFORCE (hard gating), gated default-OFF.

THE GAP this closes: the REVIEW subagent emits ``[REVIEW-SHADOW]`` verdicts
but the FSM proceeds to GATE regardless; the PLAN subagent stashes an
``execution_graph`` DAG on ``ctx`` but the legacy flat plan stays
authoritative for GENERATE + the post-GENERATE fan-out keys off the GENERATE
candidate-file count, never the PLAN DAG. This module is the **decision
logic** for the two enforce branches -- pure, deterministic, unit-testable
in isolation so the FSM surgery at the call sites stays minimal.

It introduces **NO new gate** and **NO new generation driver**. It maps the
REVIEW aggregate verdict onto the EXISTING ``RiskTier`` escalation (the same
``risk_tier.value < floor.value`` comparison ``SemanticGuardian`` uses) and
decides whether the EXISTING post-GENERATE fan-out
(``parallel_dispatch.enforce_evaluate_fanout`` -> scheduler -> swarm ->
``DAGComposer``) should be engaged because the PLAN DAG is a genuinely
multi-node parallelizable graph.

Gating invariant (both flags default-OFF):

  * ``JARVIS_REVIEW_SUBAGENT_ENFORCE`` (default **false**) -- OFF -> the
    REVIEW verdict is logged + ignored (byte-identical shadow).
  * ``JARVIS_PLAN_SUBAGENT_ENFORCE`` (default **false**) -- OFF -> the PLAN
    DAG is stashed + ignored, the flat plan drives GENERATE (byte-identical
    shadow).

Fail-CLOSED on the verdict: an ambiguous / errored / unknown REVIEW
aggregate escalates (the SAFE direction -- never silently approve). The
*subsystem* fail-soft (a review/plan dispatch crash -> legacy shadow, never
crash the op) lives at the call sites; this module's pure functions never
raise on well-formed inputs and defend defensively on malformed ones.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("Ouroboros.ShadowEnforce")


# ---------------------------------------------------------------------------
# Flag readers -- both default-OFF (byte-identical shadow when off).
# ---------------------------------------------------------------------------


def _env_truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes", "on")


def review_enforce_enabled() -> bool:
    """``JARVIS_REVIEW_SUBAGENT_ENFORCE`` -- default **false**.

    When false, the REVIEW verdict is observer-only (byte-identical shadow).
    """
    return _env_truthy("JARVIS_REVIEW_SUBAGENT_ENFORCE")


def plan_enforce_enabled() -> bool:
    """``JARVIS_PLAN_SUBAGENT_ENFORCE`` -- default **false**.

    When false, the PLAN DAG is stashed but the flat plan stays authoritative
    (byte-identical shadow).
    """
    return _env_truthy("JARVIS_PLAN_SUBAGENT_ENFORCE")


# ---------------------------------------------------------------------------
# REVIEW enforce -- map the aggregate verdict onto a RiskTier floor.
# ---------------------------------------------------------------------------

# String constants the aggregate may carry. These mirror the uppercase
# aggregate emitted by the [REVIEW-SHADOW] telemetry line so the enforce
# path consumes exactly what the shadow already computes -- no re-derivation.
AGG_APPROVE = "APPROVE"
AGG_RESERVATIONS = "APPROVE_WITH_RESERVATIONS"
AGG_REJECT = "REJECT"
AGG_NO_FILES = "NO_FILES"


@dataclass(frozen=True)
class ReviewAggregate:
    """The worst-of-N REVIEW verdict the shadow already computes.

    ``aggregate`` is the uppercase string ("APPROVE" / "REJECT" /
    "APPROVE_WITH_RESERVATIONS" / "NO_FILES"). ``had_failure`` is True when
    one or more per-file reviews failed to complete (status != "completed")
    -- an ambiguous outcome that fails CLOSED (escalate).
    """

    aggregate: str = AGG_NO_FILES
    files_reviewed: int = 0
    rejected: int = 0
    reservations: int = 0
    approved: int = 0
    failed: int = 0
    had_failure: bool = False


def aggregate_to_tier_floor(agg: ReviewAggregate) -> Optional[str]:
    """Map a REVIEW aggregate onto a risk-tier *floor* name, or ``None``.

    Returns one of ``"approval_required"`` / ``"notify_apply"`` / ``None``.
    The orchestrator applies the floor with the SAME stricter-wins clamp
    SemanticGuardian uses (``risk_tier.value < upgrade.value``), so a floor
    only ever *raises* the tier, never lowers it.

    Fail-CLOSED:

      * ``REJECT``                  -> ``approval_required`` (hard human gate).
      * any per-file review FAILED  -> ``approval_required`` (ambiguous ->
        escalate; a review we could not complete is treated as the worst
        case, never silently approved).
      * ``APPROVE_WITH_RESERVATIONS`` -> ``notify_apply`` (surface the diff).
      * ``APPROVE`` (clean, no failures) -> ``None`` (no escalation).
      * ``NO_FILES`` / unknown string -> ``None`` (nothing reviewed; the
        existing classifier + SemanticGuardian + Iron Gate remain
        authoritative -- this branch never *lowers* friction).

    Note the ordering: an aggregate of APPROVE that nonetheless carried a
    FAILED per-file review still escalates, because ``had_failure`` is
    checked before the clean-approve short-circuit.
    """
    if agg.aggregate == AGG_REJECT:
        return "approval_required"
    if agg.had_failure or agg.failed > 0:
        # A review we could not complete is ambiguous -> escalate (CLOSED).
        return "approval_required"
    if agg.aggregate == AGG_RESERVATIONS:
        return "notify_apply"
    # APPROVE / NO_FILES / anything else -> no escalation.
    return None


def escalate_risk_tier(current: Any, floor_name: Optional[str]) -> Any:
    """Apply a tier *floor* to ``current``, stricter-wins. Reuses the exact
    SemanticGuardian pattern: only raise the tier when the floor is strictly
    stricter than the current tier (``current.value < floor.value``).

    Returns the (possibly-upgraded) ``RiskTier``. ``floor_name=None`` ->
    ``current`` unchanged. An unrecognized floor name -> ``current``
    unchanged (defensive; never crashes the FSM).
    """
    if floor_name is None:
        return current
    try:
        from backend.core.ouroboros.governance.risk_engine import RiskTier

        _floor_map = {
            "notify_apply": RiskTier.NOTIFY_APPLY,
            "approval_required": RiskTier.APPROVAL_REQUIRED,
        }
        upgrade = _floor_map.get(floor_name)
        if upgrade is None:
            return current
        # Stricter-wins: only raise. Mirrors semantic_guardian wiring.
        if current is None:
            return upgrade
        if getattr(current, "value", None) is None:
            return current
        if current.value < upgrade.value:
            return upgrade
        return current
    except Exception:  # noqa: BLE001 -- defensive: never crash the FSM on an
        # escalation-table lookup. Conservative: leave the tier unchanged
        # (the verdict-level fail-CLOSED is handled by aggregate_to_tier_floor;
        # an import error here is a subsystem failure -> fail-soft).
        logger.debug("[ShadowEnforce] risk-tier escalation lookup failed", exc_info=True)
        return current


# ---------------------------------------------------------------------------
# PLAN enforce -- decide whether the PLAN DAG is authoritative + multi-node.
# ---------------------------------------------------------------------------


def _payload_to_dict(execution_graph: Any) -> Optional[dict]:
    """Coerce the 2d.1 execution_graph payload (tuple-of-tuple OR dict) into
    a dict, or ``None`` if it is not a recognizable shape. Defensive: never
    raises.
    """
    if execution_graph is None:
        return None
    if isinstance(execution_graph, dict):
        return execution_graph
    try:
        return dict(execution_graph)
    except (TypeError, ValueError):
        return None


def plan_dag_is_multinode(execution_graph: Any) -> bool:
    """True iff the stashed PLAN DAG is genuinely multi-node parallelizable.

    Mirrors ``swarm_invoker.is_graph_parallelizable`` on the 2d.1
    tuple-of-tuple payload (which is what ``_run_plan_shadow`` stashes on
    ``ctx.execution_graph``), so PLAN-enforce engages the fan-out under the
    SAME parallelizability contract the swarm itself uses:

      * ``concurrency_limit > 1``, AND
      * at least 2 dependency-free root units (in-degree 0) that can run in
        the same first wave.

    Fail-CLOSED on a malformed / empty / single-node payload -> ``False`` ->
    the orchestrator keeps the legacy flat-plan GENERATE (no fan-out, no
    behavior change). Never raises.
    """
    payload = _payload_to_dict(execution_graph)
    if payload is None:
        return False
    try:
        concurrency_limit = int(payload.get("concurrency_limit", 1) or 1)
        if concurrency_limit <= 1:
            return False
        units = payload.get("units") or ()
        if len(units) <= 1:
            return False
        roots = 0
        for unit in units:
            unit_dict = _payload_to_dict(unit)
            if unit_dict is None:
                # Defensive: an unrecognizable unit cannot be proven a root.
                continue
            deps = unit_dict.get("dependency_ids") or ()
            if not deps:
                roots += 1
        return roots >= 2
    except Exception:  # noqa: BLE001 -- malformed payload -> not parallelizable.
        logger.debug("[ShadowEnforce] plan DAG parallelizability probe failed", exc_info=True)
        return False


def plan_enforce_should_fanout(ctx: Any) -> bool:
    """Decide whether PLAN-enforce should drive the post-GENERATE fan-out.

    True iff ALL of:

      * ``JARVIS_PLAN_SUBAGENT_ENFORCE`` is on, AND
      * ``ctx.execution_graph`` is a genuinely multi-node parallelizable DAG.

    False (the default + every fail-CLOSED case) -> the legacy flat-plan
    GENERATE drives the op; the post-GENERATE fan-out engages ONLY via its
    own ``parallel_dispatch`` enforce flag, exactly as today. Never raises.
    """
    if not plan_enforce_enabled():
        return False
    try:
        execution_graph = getattr(ctx, "execution_graph", None)
    except Exception:  # noqa: BLE001 -- defensive ctx access.
        return False
    return plan_dag_is_multinode(execution_graph)


__all__ = [
    "review_enforce_enabled",
    "plan_enforce_enabled",
    "ReviewAggregate",
    "aggregate_to_tier_floor",
    "escalate_risk_tier",
    "plan_dag_is_multinode",
    "plan_enforce_should_fanout",
    "AGG_APPROVE",
    "AGG_RESERVATIONS",
    "AGG_REJECT",
    "AGG_NO_FILES",
]
