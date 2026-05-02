"""SBT-Probe Escalation Runner — Slice 2 async wrapper.

The async layer that runs the actual escalation decision + SBT
invocation + result mapping. Composes Slice 1's pure-stdlib
primitive (``sbt_escalation_bridge``) with the existing
``run_speculative_tree`` runner and the ``ConfidenceCollapseVerdict``
shape from ``hypothesis_consumers``.

Architectural reuse — three existing surfaces compose with ZERO
duplication:

  * Slice 1 ``compute_escalation_decision`` — total decision
    function (probe outcome × budget × flag → EscalationDecision).
    NEVER raises.
  * Slice 1 ``tree_verdict_to_collapse_action`` — deterministic
    5→3 mapping (TreeVerdict string → ConfidenceCollapseAction
    string). NEVER raises.
  * Existing ``run_speculative_tree`` (Priority #4 graduated
    2026-05-02) — the K-way parallel-with-early-stop runner with
    its own wall-clock cap, sub-flag gates, and TreeVerdictResult
    shape.

The only NEW code in Slice 2:

  * The :func:`escalate_via_sbt` async wrapper that wires the
    above pieces together with ``asyncio.wait_for`` budget
    enforcement.
  * ``Optional[ConfidenceCollapseVerdict]`` return shape — None
    means "caller, fall through to your existing logic"; non-None
    means "use this verdict instead".

Backward-compat by construction
-------------------------------

  * Master flag default-FALSE through Slices 1-2 → wrapper
    always returns None → caller's existing behavior preserved.
  * On enable, only probe ``EXHAUSTED`` triggers escalation;
    CONVERGED / DIVERGED / DISABLED / FAILED all return None
    (caller's existing executor handles).
  * Default ``prober=None`` falls back to SBT's
    ``_NullBranchProber`` → tree returns INCONCLUSIVE → we map
    to INCONCLUSIVE (same as caller's existing EXHAUSTED branch).
    Safe degraded path; production prober wire-up is Slice 3.
  * NEVER raises out — all errors mapped to None (fall through)
    or INCONCLUSIVE (defensive non-None).

Direct-solve principles
-----------------------

* **Asynchronous-ready** — single ``await asyncio.wait_for(...)``
  on ``run_speculative_tree``. Caller may race this via
  ``asyncio.wait`` for concurrent cancellation; wrapper is the
  inner await.
* **Dynamic** — every numeric (timeout) flows from Slice 1's
  env-knob helpers. Caller may override per-call.
* **Adaptive** — degraded paths (asyncio timeout, runner raise,
  garbage tree result) all map to closed-vocabulary outcomes
  rather than raises.
* **Intelligent** — escalation decision happens BEFORE SBT spawn
  (Slice 1 primitive call). When the gate says SKIP/DISABLED/
  BUDGET_EXHAUSTED/FAILED, we never even create SBT tasks. Cost
  saved by construction.
* **Robust** — every public function NEVER raises. Wrapper is
  callable from any async context (executor, REPL test fixture,
  integration test).
* **No hardcoding** — reuses Slice 1's env-knobs; sentinel
  constants (timeout grace, default ambiguity_kind) exposed as
  module-level symbols.

Authority invariants (AST-pinned by Slice 3 graduation)
-------------------------------------------------------

* MAY import: ``sbt_escalation_bridge`` (Slice 1 primitive),
  ``speculative_branch`` (BranchTreeTarget), ``speculative_branch_runner``
  (run_speculative_tree + BranchProber Protocol),
  ``confidence_probe_bridge`` (ConvergenceVerdict + ProbeOutcome),
  ``hypothesis_consumers`` (ConfidenceCollapseAction + ConfidenceCollapseVerdict).
* MUST NOT import: orchestrator / phase_runner / iron_gate /
  change_engine / candidate_generator / providers /
  doubleword_provider / urgency_router / auto_action_router /
  subagent_scheduler / tool_executor / semantic_guardian /
  semantic_firewall / risk_engine.
* No exec/eval/compile (mirrors Slice 1 + InlinePromptGate
  Slices 1-4 critical safety pin).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Optional

from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (
    ConvergenceVerdict,
    ProbeOutcome,
)
from backend.core.ouroboros.governance.verification.hypothesis_consumers import (
    ConfidenceCollapseAction,
    ConfidenceCollapseVerdict,
)
from backend.core.ouroboros.governance.verification.sbt_escalation_bridge import (
    EscalationContext,
    EscalationDecision,
    compute_escalation_decision,
    max_escalation_time_s,
    tree_verdict_to_collapse_action,
)
from backend.core.ouroboros.governance.verification.speculative_branch import (
    BranchTreeTarget,
    TreeVerdict,
    TreeVerdictResult,
)
from backend.core.ouroboros.governance.verification.speculative_branch_runner import (
    BranchProber,
    run_speculative_tree,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sentinel constants — Slice 3 will AST-pin
# ---------------------------------------------------------------------------

#: Default ``ambiguity_kind`` stamped on the BranchTreeTarget when
#: caller doesn't supply one. Distinguishes escalation-via-this-bridge
#: from any direct caller of run_speculative_tree in audit / SSE.
DEFAULT_AMBIGUITY_KIND: str = "probe_exhausted"

#: Confidence posterior values for each terminal SBT-derived
#: collapse action. Mirrors the executor's
#: ``_CONFIDENCE_AFTER_*`` constants by intention but kept here so
#: this wrapper composes cleanly without reaching into executor
#: internals.
_CONFIDENCE_AFTER_SBT_CONVERGED: float = 0.85
_CONFIDENCE_AFTER_SBT_DIVERGED: float = 0.15
_CONFIDENCE_AFTER_SBT_INCONCLUSIVE: float = 0.5

#: Inconclusive paths reduce the next round's thinking budget so
#: the model has less rope to spin uncertain reasoning. Mirrors
#: the executor's existing ``_INCONCLUSIVE_BUDGET_REDUCTION``.
_INCONCLUSIVE_BUDGET_REDUCTION: float = 0.5

#: Wall-clock grace for asyncio.wait_for around run_speculative_tree.
#: SBT has its own internal cap; this is defense-in-depth so a
#: runaway runner can't pin the wrapper indefinitely. 5s grace on
#: top of SBT's 60s default → 65s effective.
_WAIT_FOR_GRACE_S: float = 5.0


# ---------------------------------------------------------------------------
# Verdict construction helpers — pure data transformation
# ---------------------------------------------------------------------------


def _build_collapse_verdict_from_tree(
    tree_result: TreeVerdictResult,
    *,
    confidence_prior: float,
) -> ConfidenceCollapseVerdict:
    """Map an SBT :class:`TreeVerdictResult` to a
    :class:`ConfidenceCollapseVerdict` via the Slice 1 5→3 mapping.

    Pure function — no I/O. Composes:
      * Slice 1's ``tree_verdict_to_collapse_action`` for the
        action string.
      * Per-action confidence/budget reduction tuned to mirror
        the executor's existing constants.
      * detail string carried from the tree result for operator
        visibility.

    NEVER raises."""
    try:
        action_str = tree_verdict_to_collapse_action(
            tree_result.outcome.value if isinstance(
                tree_result.outcome, TreeVerdict,
            ) else None,
        )
        try:
            action = ConfidenceCollapseAction(action_str)
        except ValueError:
            action = ConfidenceCollapseAction.INCONCLUSIVE

        detail = str(tree_result.detail or "")[:500]
        winning_fp = str(tree_result.winning_fingerprint or "")
        agg_conf = float(tree_result.aggregate_confidence or 0.0)

        if action is ConfidenceCollapseAction.RETRY_WITH_FEEDBACK:
            # SBT CONVERGED — thread the tree's evidence into the
            # next GENERATE round.
            feedback = (
                f"Speculative branch tree converged: "
                f"fingerprint={winning_fp[:16]} "
                f"avg_confidence={agg_conf:.2f}. "
                f"{detail}"
            )[:1000]
            return ConfidenceCollapseVerdict(
                action=action,
                confidence_posterior=_CONFIDENCE_AFTER_SBT_CONVERGED,
                convergence_state="sbt_escalation_converged",
                observation_summary=detail,
                cost_usd=0.0,
                feedback_text=feedback,
            )
        if action is ConfidenceCollapseAction.ESCALATE_TO_OPERATOR:
            # SBT DIVERGED — tree confirms genuine ambiguity.
            return ConfidenceCollapseVerdict(
                action=action,
                confidence_posterior=_CONFIDENCE_AFTER_SBT_DIVERGED,
                convergence_state="sbt_escalation_diverged",
                observation_summary=detail,
                cost_usd=0.0,
                feedback_text=(
                    "Speculative branch tree confirmed genuine "
                    "ambiguity (no majority across branches) — "
                    "operator resolution required."
                ),
            )
        # action is INCONCLUSIVE — covers SBT INCONCLUSIVE /
        # TRUNCATED / FAILED + defensive default. Mid-band collapse;
        # reduce the next round's thinking budget.
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.INCONCLUSIVE,
            confidence_posterior=max(0.0, min(1.0, confidence_prior)),
            convergence_state="sbt_escalation_inconclusive",
            observation_summary=detail,
            cost_usd=0.0,
            thinking_budget_reduction_factor=(
                _INCONCLUSIVE_BUDGET_REDUCTION
            ),
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.warning(
            "[SBTEscalationRunner] _build_collapse_verdict_from_tree "
            "degraded: %s", exc,
        )
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.INCONCLUSIVE,
            confidence_posterior=max(0.0, min(1.0, confidence_prior)),
            convergence_state="sbt_escalation_internal_error",
            observation_summary=f"wrapper error: {exc!r}",
            cost_usd=0.0,
            thinking_budget_reduction_factor=(
                _INCONCLUSIVE_BUDGET_REDUCTION
            ),
        )


# ---------------------------------------------------------------------------
# Public async wrapper — orchestrator-callable surface
# ---------------------------------------------------------------------------


async def escalate_via_sbt(
    probe_verdict: ConvergenceVerdict,
    *,
    op_id: str = "",
    target_descriptor: str = "",
    ambiguity_kind: str = DEFAULT_AMBIGUITY_KIND,
    ambiguity_payload: Optional[Mapping[str, Any]] = None,
    prober: Optional[BranchProber] = None,
    cost_so_far_usd: float = 0.0,
    time_so_far_s: float = 0.0,
    enabled: Optional[bool] = None,
    max_cost_usd: Optional[float] = None,
    max_time_s: Optional[float] = None,
    confidence_prior: float = 0.5,
) -> Optional[ConfidenceCollapseVerdict]:
    """Conditionally escalate a probe outcome to SBT.

    Returns ``None`` when escalation is NOT warranted (master flag
    off, probe was conclusive, budget exhausted, or wrapper
    error) — the caller falls through to its existing executor
    logic. Returns a :class:`ConfidenceCollapseVerdict` when SBT
    ran and produced a result.

    NEVER raises out. asyncio cancellation propagates per asyncio
    convention (caller catches).

    Decision flow:
      1. Slice 1 primitive: compute_escalation_decision against
         probe_verdict.outcome.value + cost/time/enabled inputs.
         If decision is anything OTHER than ESCALATE, return None
         immediately (no SBT spawn).
      2. Construct BranchTreeTarget from caller context.
      3. Wrap run_speculative_tree in asyncio.wait_for with budget
         grace; on timeout / runner raise → return INCONCLUSIVE
         verdict (non-None — escalation fired but produced no
         signal; mid-band collapse is the safe answer).
      4. Map TreeVerdictResult → ConfidenceCollapseVerdict via
         pure helper.

    Args:
      probe_verdict: The Move 5 probe loop's terminal verdict.
        Required.
      op_id: Originating op id — flows into BranchTreeTarget +
        audit. Default empty string.
      target_descriptor: Free-form descriptor of what was being
        probed (file:line, symbol name, etc.). Default empty.
      ambiguity_kind: Classifier for the ambiguity. Defaults to
        ``"probe_exhausted"`` (the trigger-state name).
      ambiguity_payload: Opaque map carrying ambiguity-specific
        context for the SBT prober.
      prober: Optional BranchProber. None → SBT's NullProber
        (tree returns INCONCLUSIVE → we return INCONCLUSIVE,
        equivalent to current executor behavior on EXHAUSTED).
        Production wire-up is Slice 3.
      cost_so_far_usd / time_so_far_s: Cumulative probe-path
        burn — flows into the Slice 1 budget gate.
      enabled: Optional explicit enable override (test injection).
        Defaults to env via Slice 1.
      max_cost_usd / max_time_s: Optional caller overrides of the
        budget caps. Defaults to env via Slice 1.
      confidence_prior: Prior confidence for INCONCLUSIVE collapse
        verdicts. Defaults to 0.5.

    Returns:
      ``None`` when escalation skipped; ``ConfidenceCollapseVerdict``
      when escalation fired.
    """
    # 1. Validate inputs at boundary.
    if not isinstance(probe_verdict, ConvergenceVerdict):
        logger.warning(
            "[SBTEscalationRunner] non-ConvergenceVerdict input "
            "type=%s — returning None (caller falls through)",
            type(probe_verdict).__name__,
        )
        return None

    try:
        probe_outcome_str = (
            probe_verdict.outcome.value
            if isinstance(probe_verdict.outcome, ProbeOutcome)
            else ""
        )
    except Exception:  # noqa: BLE001 — defensive
        return None

    # 2. Slice 1 primitive: should we escalate?
    context = EscalationContext(
        probe_outcome=probe_outcome_str,
        cost_so_far_usd=float(cost_so_far_usd or 0.0),
        time_so_far_s=float(time_so_far_s or 0.0),
        op_id=str(op_id or ""),
        target=str(target_descriptor or ""),
    )
    decision_verdict = compute_escalation_decision(
        context,
        enabled=enabled,
        max_cost_usd=max_cost_usd,
        max_time_s=max_time_s,
    )
    if decision_verdict.decision is not EscalationDecision.ESCALATE:
        # SKIP / BUDGET_EXHAUSTED / DISABLED / FAILED — caller
        # falls through to its existing logic.
        logger.debug(
            "[SBTEscalationRunner] not escalating: decision=%s "
            "detail=%s op=%s",
            decision_verdict.decision.value,
            decision_verdict.detail, op_id,
        )
        return None

    # 3. Construct BranchTreeTarget.
    try:
        target = BranchTreeTarget(
            decision_id=(
                f"{op_id}|{target_descriptor}"
                if op_id or target_descriptor
                else "sbt-escalation-anonymous"
            ),
            ambiguity_kind=str(ambiguity_kind or DEFAULT_AMBIGUITY_KIND),
            ambiguity_payload=dict(ambiguity_payload or {}),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[SBTEscalationRunner] BranchTreeTarget construction "
            "failed: %s — returning INCONCLUSIVE", exc,
        )
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.INCONCLUSIVE,
            confidence_posterior=max(0.0, min(1.0, confidence_prior)),
            convergence_state="sbt_escalation_target_construction_error",
            observation_summary=f"target ctor: {exc!r}",
            cost_usd=0.0,
            thinking_budget_reduction_factor=(
                _INCONCLUSIVE_BUDGET_REDUCTION
            ),
        )

    # 4. Compute effective wait budget — defense-in-depth on top
    #    of SBT's own internal cap.
    eff_wait = (
        max_time_s
        if max_time_s is not None and max_time_s > 0
        else max_escalation_time_s()
    )
    # The wait budget should leave room for SBT's internal cap;
    # we add grace so the internal cap fires first under normal
    # conditions and the asyncio wait is the secondary safety.
    eff_wait_with_grace = eff_wait + _WAIT_FOR_GRACE_S

    # 5. Run SBT with wait_for guard.
    tree_result: Optional[TreeVerdictResult] = None
    try:
        tree_result = await asyncio.wait_for(
            run_speculative_tree(target, prober=prober),
            timeout=eff_wait_with_grace,
        )
    except asyncio.TimeoutError:
        logger.info(
            "[SBTEscalationRunner] asyncio wait_for fired before "
            "SBT internal cap (defense-in-depth) wait=%.1fs op=%s",
            eff_wait_with_grace, op_id,
        )
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.INCONCLUSIVE,
            confidence_posterior=max(0.0, min(1.0, confidence_prior)),
            convergence_state="sbt_escalation_secondary_timeout",
            observation_summary=(
                f"wait_for timeout after {eff_wait_with_grace:.1f}s"
            ),
            cost_usd=0.0,
            thinking_budget_reduction_factor=(
                _INCONCLUSIVE_BUDGET_REDUCTION
            ),
        )
    except asyncio.CancelledError:
        # Caller-initiated cancellation — propagate per asyncio
        # convention.
        raise
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[SBTEscalationRunner] run_speculative_tree raised "
            "(should not happen): %s — INCONCLUSIVE", exc,
        )
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.INCONCLUSIVE,
            confidence_posterior=max(0.0, min(1.0, confidence_prior)),
            convergence_state="sbt_escalation_runner_error",
            observation_summary=f"runner raised: {exc!r}",
            cost_usd=0.0,
            thinking_budget_reduction_factor=(
                _INCONCLUSIVE_BUDGET_REDUCTION
            ),
        )

    # 6. Map TreeVerdictResult → ConfidenceCollapseVerdict.
    if tree_result is None:
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.INCONCLUSIVE,
            confidence_posterior=max(0.0, min(1.0, confidence_prior)),
            convergence_state="sbt_escalation_no_result",
            observation_summary="run_speculative_tree returned None",
            cost_usd=0.0,
            thinking_budget_reduction_factor=(
                _INCONCLUSIVE_BUDGET_REDUCTION
            ),
        )
    return _build_collapse_verdict_from_tree(
        tree_result, confidence_prior=confidence_prior,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "DEFAULT_AMBIGUITY_KIND",
    "escalate_via_sbt",
]
