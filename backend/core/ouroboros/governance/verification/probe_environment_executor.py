"""Move 5 Slice 4 — PROBE_ENVIRONMENT executor + integration.

Translates Slice 3's async probe loop output (``ConvergenceVerdict``)
into Slice 1's existing ``ConfidenceCollapseVerdict`` shape so the
existing collapse-handling pipeline (callers of
``probe_confidence_collapse``) consume probe results without API
break.

Decision sequence (every ConvergenceVerdict outcome maps to exactly
one ConfidenceCollapseAction — J.A.R.M.A.T.R.I.X. discipline):

  * ``CONVERGED``  → reset monitor window + return RETRY_WITH_
                     FEEDBACK with feedback_text="probe converged:
                     <canonical_answer>". Confidence elevated; op
                     proceeds with the canonical answer threaded
                     into the next prompt.

  * ``DIVERGED``   → return ESCALATE_TO_OPERATOR. Probes returned
                     distinct answers — genuine ambiguity that
                     needs human resolution. Cost-contract
                     preserved (escalation does NOT cascade BG/SPEC
                     to Claude — §26.6 invariants enforce).

  * ``EXHAUSTED``  → return INCONCLUSIVE with thinking_budget_
                     reduction_factor=0.5. Probes consumed budget
                     without quorum (partial agreement). Caller
                     retries with reduced thinking budget so model
                     has less rope to spin uncertain reasoning.

  * ``DISABLED``   → return RETRY_WITH_FEEDBACK (master off — safe
                     legacy default). No probe was actually run.

  * ``FAILED``     → return INCONCLUSIVE. Defensive sentinel; the
                     runner's exception was already swallowed.

Direct-solve principles:

  * **Asynchronous** — async wrapper around Slice 3's async
    ``run_probe_loop``. Sync resolver wrapped via to_thread at
    Slice 3 boundary; this layer is pure async glue.

  * **Dynamic** — every threshold inherited from Slice 1+2+3
    env knobs. No new knobs introduced.

  * **Adaptive** — confidence_posterior set based on outcome
    (CONVERGED → 0.85, DIVERGED → 0.15, otherwise → prior).
    Caller can use posterior for ledger persistence.

  * **Intelligent** — feedback text on CONVERGED includes the
    canonical answer so the retry has the disambiguation result
    in-prompt (closes the cognitive loop autonomously).

  * **Robust** — never raises out of execute_probe_environment.
    Monitor.reset_window() exception swallowed (defensive). Probe
    loop exception swallowed at runner boundary; this layer just
    handles the outcome.

  * **No hardcoding** — confidence posteriors expressed as
    module-level constants (operator-tunable would be premature;
    Slice 5 graduation can env-knob if needed).

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + verification.confidence_monitor
    (ConfidenceMonitor + ConfidenceCollapseError types) +
    verification.confidence_probe_bridge (ProbeOutcome) +
    verification.confidence_probe_runner (run_probe_loop) +
    verification.confidence_probe_generator (AmbiguityContext) +
    verification.hypothesis_consumers (ConfidenceCollapseAction +
    ConfidenceCollapseVerdict) ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / semantic_firewall / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor.
  * NEVER references mutation tool names in code.
  * Async function present (intentional — this is the
    integration layer).
  * No disk writes.

Master flag inherited from Slice 1's ``bridge_enabled()``. When
off, ``execute_probe_environment`` returns the RETRY_WITH_FEEDBACK
safe default without invoking the runner — zero cost.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (  # noqa: E501
    ProbeOutcome,
    bridge_enabled,
)
from backend.core.ouroboros.governance.verification.confidence_probe_generator import (  # noqa: E501
    AmbiguityContext,
)
from backend.core.ouroboros.governance.verification.confidence_probe_runner import (  # noqa: E501
    run_probe_loop,
)
from backend.core.ouroboros.governance.verification.hypothesis_consumers import (  # noqa: E501
    ConfidenceCollapseAction,
    ConfidenceCollapseVerdict,
)

logger = logging.getLogger(__name__)


PROBE_ENVIRONMENT_EXECUTOR_SCHEMA_VERSION: str = (
    "probe_environment_executor.1"
)


# ---------------------------------------------------------------------------
# Confidence-posterior constants (closed taxonomy of post-probe
# confidence values). These are not env-knobs — Slice 5 graduation
# can add knobs if operator usage shows need.
# ---------------------------------------------------------------------------


_CONFIDENCE_AFTER_CONVERGED: float = 0.85
"""Confidence elevated when K-1 probes agree on canonical answer."""

_CONFIDENCE_AFTER_DIVERGED: float = 0.15
"""Confidence collapsed when probes return distinct answers."""

_INCONCLUSIVE_BUDGET_REDUCTION: float = 0.5
"""Caller retries with halved thinking budget when probe exhausted
(less rope to spin uncertain reasoning)."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def execute_probe_environment(
    *,
    monitor: Any,
    ambiguity_context: AmbiguityContext,
    op_id: str = "",
    prior: float = 0.5,
    resolver: Optional[Any] = None,
    quorum: Optional[int] = None,
    max_probes: Optional[int] = None,
    wall_clock_s: Optional[float] = None,
) -> ConfidenceCollapseVerdict:
    """Run the autonomous probe loop and translate the outcome into
    a ``ConfidenceCollapseVerdict``. NEVER raises.

    Decision sequence:

      1. ``bridge_enabled()`` returns False → return safe legacy
         default (``RETRY_WITH_FEEDBACK``) without invoking the
         runner. Zero cost.
      2. Call ``run_probe_loop(ambiguity_context, ...)`` —
         async parallel-with-early-stop probe execution.
      3. Translate ``ConvergenceVerdict.outcome``:
           CONVERGED → reset monitor window + RETRY_WITH_FEEDBACK
                       with feedback="probe converged: <answer>"
           DIVERGED  → ESCALATE_TO_OPERATOR
           EXHAUSTED → INCONCLUSIVE with budget reduction
           DISABLED  → RETRY_WITH_FEEDBACK (master-off safe default)
           FAILED    → INCONCLUSIVE (defensive sentinel)

    ``monitor`` is the ``ConfidenceMonitor`` instance. On
    CONVERGED, this layer calls ``monitor.reset_window()`` so the
    next ``evaluate()`` call returns OK regardless of prior low-
    confidence signal. The reset is best-effort; exception
    swallowed."""
    # Step 1: master flag off → safe legacy default
    if not bridge_enabled():
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.RETRY_WITH_FEEDBACK,
            confidence_posterior=prior,
            convergence_state="probe_disabled",
            observation_summary="bridge master flag off",
            cost_usd=0.0,
            feedback_text="",
        )

    # Step 2: run the probe loop
    try:
        verdict = await run_probe_loop(
            ambiguity_context,
            resolver=resolver,
            quorum=quorum,
            max_probes=max_probes,
            wall_clock_s=wall_clock_s,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        # Runner is supposed to never raise, but defense-in-depth
        logger.debug(
            "[ProbeEnvironmentExecutor] run_probe_loop raised "
            "(should not happen): %s", exc,
        )
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.INCONCLUSIVE,
            confidence_posterior=prior,
            convergence_state="probe_runner_error",
            observation_summary=f"runner raised: {exc!r}",
            cost_usd=0.0,
            thinking_budget_reduction_factor=(
                _INCONCLUSIVE_BUDGET_REDUCTION
            ),
        )

    # Step 3: translate outcome
    if verdict.outcome is ProbeOutcome.CONVERGED:
        # Reset monitor window so next evaluate() returns OK
        try:
            if hasattr(monitor, "reset_window"):
                monitor.reset_window()
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[ProbeEnvironmentExecutor] monitor.reset_window "
                "raised: %s", exc,
            )
        canonical = verdict.canonical_answer or "(unknown)"
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.RETRY_WITH_FEEDBACK,
            confidence_posterior=_CONFIDENCE_AFTER_CONVERGED,
            convergence_state="probe_converged",
            observation_summary=verdict.detail,
            cost_usd=0.0,
            feedback_text=(
                f"Probe converged on autonomous answer: "
                f"{canonical}"
            ),
        )

    if verdict.outcome is ProbeOutcome.DIVERGED:
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.ESCALATE_TO_OPERATOR,
            confidence_posterior=_CONFIDENCE_AFTER_DIVERGED,
            convergence_state="probe_diverged",
            observation_summary=verdict.detail,
            cost_usd=0.0,
            feedback_text=(
                "Autonomous probes returned distinct answers — "
                "genuine ambiguity needs operator resolution."
            ),
        )

    if verdict.outcome is ProbeOutcome.EXHAUSTED:
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.INCONCLUSIVE,
            confidence_posterior=prior,
            convergence_state="probe_exhausted",
            observation_summary=verdict.detail,
            cost_usd=0.0,
            thinking_budget_reduction_factor=(
                _INCONCLUSIVE_BUDGET_REDUCTION
            ),
        )

    if verdict.outcome is ProbeOutcome.DISABLED:
        # Bridge said disabled despite our master-flag check —
        # could happen if a sub-flag flips between checks. Safe
        # default.
        return ConfidenceCollapseVerdict(
            action=ConfidenceCollapseAction.RETRY_WITH_FEEDBACK,
            confidence_posterior=prior,
            convergence_state="probe_disabled",
            observation_summary=verdict.detail,
            cost_usd=0.0,
            feedback_text="",
        )

    # ProbeOutcome.FAILED → defensive sentinel
    return ConfidenceCollapseVerdict(
        action=ConfidenceCollapseAction.INCONCLUSIVE,
        confidence_posterior=prior,
        convergence_state="probe_failed",
        observation_summary=verdict.detail,
        cost_usd=0.0,
        thinking_budget_reduction_factor=(
            _INCONCLUSIVE_BUDGET_REDUCTION
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "PROBE_ENVIRONMENT_EXECUTOR_SCHEMA_VERSION",
    "execute_probe_environment",
]
