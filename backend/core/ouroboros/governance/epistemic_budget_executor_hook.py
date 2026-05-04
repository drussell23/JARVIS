"""Upgrade 1 Slice 3 — Bounded Epistemic Loop integration hook.

The integration point that connects Slice 1's contract layer +
Slice 2's tracker to the actual Venom round-loop in
:mod:`tool_executor`. Pure dispatch — converts each
:class:`BudgetOutcome` into side-effect flags returned in a
frozen :class:`BudgetDispatchResult`. Tool_executor reads the
result + applies side effects (ctx.risk_tier mutation, Orange
queue insertion, round-loop break).

**One-way data flow**: hook reads tracker state + ctx fields;
hook never mutates ctx. Tool_executor is the imperative shell.

**Decision B1** (synchronous probe await): :func:`apply_budget_-
decision` is ``async``. PROBE_TRIGGERED → ``await probe_runner.
run(...)``; SBT_TRIGGERED → ``await sbt_runner.run(...)``.
Round-loop blocks on probe completion before advancing
``round_index``. Predictable cost-cap (we KNOW when probe
consumed its budget); no race surface; bounded by HypothesisProbe
three-termination contract (K-call cap + monotonic-clock + sha256
diminishing-returns) inside ConfidenceProbeRunner.

**Decision C1** (escalation via existing primitives): two stages —
(1) honor environment-driven floor via
:func:`risk_tier_floor.apply_floor_to_name`,
(2) bump to explicit target via
:func:`risk_tier_floor.get_active_tier_order` rank comparison.
NEVER invents a new tier ranking; reads the canonical order from
the existing primitive. Pinned by Slice 5 AST invariant
``epistemic_budget_escalation_uses_canonical_tier_order``.

**Cost contract preservation**: PROBE and SBT invocation are
gated by Slice 1's :func:`compute_budget_action` decision tree —
cost-gated routes (BG / SPECULATIVE) cannot return PROBE_TRIGGERED
/ SBT_TRIGGERED structurally. The hook trusts that contract; it
does NOT re-check the cost gate (single source of truth).

**Testability via Protocol injection**: the runners
(ProbeRunnerProtocol, SBTRunnerProtocol, OrangeQueueProtocol) are
caller-supplied. Production code injects the real implementations
from confidence_probe_runner / speculative_branch_runner /
orange_pr_reviewer at the tool_executor wire-up site. Tests
inject mock instances + verify the dispatch logic in isolation.

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib + ``epistemic_budget`` (Slice 1+2) +
    ``risk_tier_floor`` (existing primitive) ONLY.
  * NEVER imports orchestrator / tool_executor /
    candidate_generator / iron_gate / providers / strategic_-
    direction / confidence_probe_runner / speculative_branch_-
    runner / orange_pr_reviewer (those are caller-injected via
    Protocol — keeps the hook unit-testable + decouples from
    runner implementations).
  * Pure dispatch — never mutates :class:`EpistemicBudget`
    (Slice 2's tracker is the only mutation point) and never
    mutates the supplied ctx.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

from backend.core.ouroboros.governance.epistemic_budget import (
    BudgetAction,
    BudgetOutcome,
    EpistemicBudgetTracker,
    epistemic_budget_enabled,
)
from backend.core.ouroboros.governance.risk_tier_floor import (
    apply_floor_to_name,
    get_active_tier_order,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runner protocols — caller-supplied (production: real runners;
# tests: mocks). Decoupling lets the hook stay pure-dispatch.
# ---------------------------------------------------------------------------


class ProbeRunnerProtocol(Protocol):
    """Minimal contract for the ConfidenceProbeRunner factory.
    Production: :class:`confidence_probe_runner.ConfidenceProbe-
    Runner`. Tests: mock.

    Returns the probe verdict (string or enum-like) which Slice 2
    tracker normalizes via :func:`_normalize_verdict_value`."""

    async def run(self, *, payload: Any) -> Any:  # pragma: no cover
        ...


class SBTRunnerProtocol(Protocol):
    """Minimal contract for the SpeculativeBranchTree runner."""

    async def run(self, *, payload: Any) -> Any:  # pragma: no cover
        ...


class OrangeQueueProtocol(Protocol):
    """Minimal contract for the OrangePRReviewer async queue."""

    async def queue(
        self, *, op_id: str, reason: str,
    ) -> Any:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# Tier-rank helpers — read canonical order from risk_tier_floor
# ---------------------------------------------------------------------------


def _tier_rank(tier_name: str) -> int:
    """Rank lookup via :func:`risk_tier_floor.get_active_tier_-
    order`. Returns -1 for unknown tiers (treated as "below
    everything" for fail-safe escalation). NEVER raises."""
    try:
        order = get_active_tier_order()
        return int(
            order.get((tier_name or "").strip().lower(), -1),
        )
    except Exception:  # noqa: BLE001 — defensive
        return -1


def _escalate_to_target(
    current_tier: str, target_tier: str,
    *, signal_source: str = "epistemic_budget",
) -> Optional[str]:
    """Compute the new tier name, honoring (1) the existing env-
    driven floor via :func:`apply_floor_to_name`, then (2) the
    target tier via canonical-order rank comparison. Returns the
    new tier name when an escalation actually applies (stricter
    than ``current_tier``); returns None when current is already
    at or above the effective target.

    Two-stage discipline:
      * Stage 1 honors any operator-set environment floor (e.g.,
        ``JARVIS_AUTO_APPLY_QUIET_HOURS`` or vision-sensor floor).
      * Stage 2 bumps to the explicit target. The stricter of
        the two wins.

    NEVER raises."""
    try:
        # Stage 1: env-driven floor (if any).
        after_env, applied_env_floor = apply_floor_to_name(
            current_tier, signal_source=signal_source,
        )
        # Stage 2: explicit target bump via canonical rank.
        after_env_rank = _tier_rank(after_env)
        target_rank = _tier_rank(target_tier)
        current_rank = _tier_rank(current_tier)
        # Effective new tier = stricter of (env-applied,
        # explicit target).
        if target_rank > after_env_rank:
            new_tier = target_tier
            new_rank = target_rank
        else:
            new_tier = after_env
            new_rank = after_env_rank
        # Only return a new tier when it's actually stricter
        # than the input. Idempotent on repeated calls.
        if new_rank > current_rank:
            return (new_tier or "").strip().lower()
        return None
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[epistemic_budget_executor_hook] _escalate_to_"
            "target raised: %s", exc,
        )
        return None


# ---------------------------------------------------------------------------
# Dispatch result — frozen side-effect flags
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetDispatchResult:
    """Frozen output of :func:`apply_budget_decision`. Tool_-
    executor reads this and applies side effects — assigning
    ``ctx.risk_tier`` when ``new_risk_tier`` is set, calling
    ``orange.queue(...)`` when ``enqueue_for_orange_review`` is
    True, breaking the round loop when ``break_round_loop`` is
    True. Hook NEVER mutates ctx itself.

    ``action`` carries the canonical :class:`BudgetAction` from
    Slice 1's :func:`compute_budget_action` so callers have full
    explainability + observability + SSE payload available."""

    action: BudgetAction
    """The :class:`BudgetAction` returned by tracker.next_action()
    — operator-readable reason + outcome + invocation kwargs."""

    new_risk_tier: Optional[str] = None
    """Set on EXHAUSTED_NOTIFY_APPLY / EXHAUSTED_APPROVAL_-
    REQUIRED when an escalation applies. Tool_executor assigns
    ``ctx.risk_tier = result.new_risk_tier``. None means no
    escalation needed (already at or above target, or action is
    not an exhaustion outcome)."""

    enqueue_for_orange_review: bool = False
    """True on EXHAUSTED_APPROVAL_REQUIRED (after the orange
    queue protocol's ``queue`` method has been awaited
    successfully). Tool_executor uses this for telemetry +
    confirms the queue insertion completed."""

    break_round_loop: bool = False
    """True on CONVERGED + EXHAUSTED_APPROVAL_REQUIRED.
    Tool_executor breaks the Venom round loop cleanly when
    set. WITHIN_BUDGET / PROBE_TRIGGERED / SBT_TRIGGERED /
    EXHAUSTED_NOTIFY_APPLY all leave the loop running."""

    probe_invocation_failed: bool = False
    """True if PROBE_TRIGGERED was reached but probe_runner was
    None or raised. Tool_executor logs but otherwise treats
    as no-op."""

    sbt_invocation_failed: bool = False
    """True if SBT_TRIGGERED was reached but sbt_runner was
    None or raised. Tool_executor logs but otherwise treats
    as no-op."""

    extra_telemetry: dict = field(default_factory=dict)
    """Extensible payload for Slice 4 observability (raw probe
    verdict string, raw SBT verdict, escalation_target_tier_-
    requested vs effective, etc.). Slice 5 SSE event payload
    consumes this verbatim."""


# ---------------------------------------------------------------------------
# Convenience helper — open tracker if not yet open
# ---------------------------------------------------------------------------


def open_op_tracker(
    tracker: EpistemicBudgetTracker,
    *,
    op_id: str,
    route: str,
    risk_tier: str,
) -> bool:
    """Idempotent open. Returns True on successful open / reopen,
    False on garbage input or master-off. Tool_executor calls
    this once at op start."""
    try:
        if not epistemic_budget_enabled():
            return False
        budget = tracker.open(
            op_id=op_id, route=route, risk_tier=risk_tier,
        )
        return budget is not None
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[epistemic_budget_executor_hook] open_op_tracker "
            "raised: %s", exc,
        )
        return False


def note_round_complete(
    tracker: EpistemicBudgetTracker,
    *,
    op_id: str,
    confidence: Optional[float] = None,
) -> bool:
    """Increment round counter + optionally update trajectory.
    Tool_executor calls this after each Venom tool round.
    Returns True on successful update, False on garbage input
    or master-off."""
    try:
        if not epistemic_budget_enabled():
            return False
        budget = tracker.note_round_complete(
            op_id, confidence=confidence,
        )
        return budget is not None
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[epistemic_budget_executor_hook] note_round_"
            "complete raised: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Authoritative dispatch — Slice 3's load-bearing entry point
# ---------------------------------------------------------------------------


async def apply_budget_decision(
    *,
    tracker: EpistemicBudgetTracker,
    op_id: str,
    current_risk_tier: str,
    probe_runner: Optional[ProbeRunnerProtocol] = None,
    sbt_runner: Optional[SBTRunnerProtocol] = None,
    orange_queue: Optional[OrangeQueueProtocol] = None,
    probe_payload: Any = None,
    sbt_payload: Any = None,
    orange_reason_template: str = "epistemic_budget_exhausted",
) -> BudgetDispatchResult:
    """**Slice 3's authoritative integration point.** Tool_executor
    awaits this after each Venom tool round at the
    ``round_index`` boundary.

    Flow:

      1. Master-flag check (cheap path — early exit when off).
      2. Read tracker.next_action(op_id) for the routing decision.
      3. Branch by BudgetOutcome:
         * WITHIN_BUDGET → no-op return
         * CONVERGED → break_round_loop=True
         * PROBE_TRIGGERED → ``await probe_runner.run(payload)``,
           ``tracker.note_probe_completed(verdict)``. NEVER
           re-checks cost-gate (Slice 1's compute_budget_action
           is single source of truth — cost-gated routes cannot
           reach PROBE_TRIGGERED structurally).
         * SBT_TRIGGERED → ``await sbt_runner.run(payload)``,
           ``tracker.note_sbt_completed(verdict)``.
         * EXHAUSTED_NOTIFY_APPLY → :func:`_escalate_to_target`
           computes new tier; result carries
           ``new_risk_tier`` for tool_executor to assign.
         * EXHAUSTED_APPROVAL_REQUIRED → escalation +
           ``await orange_queue.queue(...)`` +
           ``break_round_loop=True``.
         * DISABLED → no-op return.

    Returns a frozen :class:`BudgetDispatchResult`. NEVER raises
    out — all faults map to a degraded result with
    probe_invocation_failed / sbt_invocation_failed flags."""
    try:
        if not epistemic_budget_enabled():
            return BudgetDispatchResult(
                action=BudgetAction(
                    outcome=BudgetOutcome.DISABLED,
                    reason="master_flag_off",
                ),
            )

        action = tracker.next_action(op_id)
        outcome = action.outcome

        # WITHIN_BUDGET — no-op continue
        if outcome is BudgetOutcome.WITHIN_BUDGET:
            return BudgetDispatchResult(action=action)

        # CONVERGED — clean exit signal
        if outcome is BudgetOutcome.CONVERGED:
            return BudgetDispatchResult(
                action=action,
                break_round_loop=True,
            )

        # DISABLED — no-op (master off, op not tracked, etc.)
        if outcome is BudgetOutcome.DISABLED:
            return BudgetDispatchResult(action=action)

        # PROBE_TRIGGERED — synchronous probe await + tracker
        # update. Decision B1.
        if outcome is BudgetOutcome.PROBE_TRIGGERED:
            if probe_runner is None:
                return BudgetDispatchResult(
                    action=action,
                    probe_invocation_failed=True,
                    extra_telemetry={
                        "probe_skipped_reason": (
                            "no_probe_runner_injected"
                        ),
                    },
                )
            try:
                payload = (
                    probe_payload
                    if probe_payload is not None
                    else (action.probe_invocation_kw or {})
                )
                verdict = await probe_runner.run(payload=payload)
                tracker.note_probe_completed(
                    op_id, verdict=verdict,
                )
                return BudgetDispatchResult(
                    action=action,
                    extra_telemetry={
                        "probe_verdict_raw": str(verdict),
                    },
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[epistemic_budget_executor_hook] "
                    "probe_runner raised: %s", exc,
                )
                return BudgetDispatchResult(
                    action=action,
                    probe_invocation_failed=True,
                    extra_telemetry={
                        "probe_error": type(exc).__name__,
                    },
                )

        # SBT_TRIGGERED — synchronous SBT spawn + tracker update.
        if outcome is BudgetOutcome.SBT_TRIGGERED:
            if sbt_runner is None:
                return BudgetDispatchResult(
                    action=action,
                    sbt_invocation_failed=True,
                    extra_telemetry={
                        "sbt_skipped_reason": (
                            "no_sbt_runner_injected"
                        ),
                    },
                )
            try:
                payload = (
                    sbt_payload
                    if sbt_payload is not None
                    else (action.sbt_invocation_kw or {})
                )
                verdict = await sbt_runner.run(payload=payload)
                tracker.note_sbt_completed(
                    op_id, verdict=verdict,
                )
                return BudgetDispatchResult(
                    action=action,
                    extra_telemetry={
                        "sbt_verdict_raw": str(verdict),
                    },
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[epistemic_budget_executor_hook] "
                    "sbt_runner raised: %s", exc,
                )
                return BudgetDispatchResult(
                    action=action,
                    sbt_invocation_failed=True,
                    extra_telemetry={
                        "sbt_error": type(exc).__name__,
                    },
                )

        # EXHAUSTED_NOTIFY_APPLY — escalate via canonical
        # primitives (Decision C1). Result carries new_risk_tier
        # for tool_executor to assign.
        if outcome is BudgetOutcome.EXHAUSTED_NOTIFY_APPLY:
            target = (
                action.escalation_target_tier or "notify_apply"
            )
            new_tier = _escalate_to_target(
                current_risk_tier, target,
            )
            return BudgetDispatchResult(
                action=action,
                new_risk_tier=new_tier,
                extra_telemetry={
                    "escalation_requested": target,
                    "escalation_effective": new_tier,
                },
            )

        # EXHAUSTED_APPROVAL_REQUIRED — escalate to approval +
        # async-queue via OrangePRReviewer + break round loop.
        if outcome is BudgetOutcome.EXHAUSTED_APPROVAL_REQUIRED:
            target = (
                action.escalation_target_tier
                or "approval_required"
            )
            new_tier = _escalate_to_target(
                current_risk_tier, target,
            )
            enqueued = False
            if orange_queue is not None:
                try:
                    await orange_queue.queue(
                        op_id=op_id,
                        reason=(
                            f"{orange_reason_template}: "
                            f"{action.reason}"
                        ),
                    )
                    enqueued = True
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[epistemic_budget_executor_hook] "
                        "orange_queue raised: %s", exc,
                    )
            return BudgetDispatchResult(
                action=action,
                new_risk_tier=new_tier,
                enqueue_for_orange_review=enqueued,
                break_round_loop=True,
                extra_telemetry={
                    "escalation_requested": target,
                    "escalation_effective": new_tier,
                    "orange_queue_attempted": (
                        orange_queue is not None
                    ),
                    "orange_queue_succeeded": enqueued,
                },
            )

        # Unknown outcome — defensive fallthrough (shouldn't
        # happen given the closed enum, but defensive)
        logger.debug(
            "[epistemic_budget_executor_hook] unknown outcome: "
            "%s", outcome,
        )
        return BudgetDispatchResult(action=action)
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[epistemic_budget_executor_hook] apply_budget_"
            "decision raised: %s", exc,
        )
        return BudgetDispatchResult(
            action=BudgetAction(
                outcome=BudgetOutcome.DISABLED,
                reason=f"dispatch_failed: {type(exc).__name__}",
            ),
        )


__all__ = [
    "BudgetDispatchResult",
    "OrangeQueueProtocol",
    "ProbeRunnerProtocol",
    "SBTRunnerProtocol",
    "apply_budget_decision",
    "note_round_complete",
    "open_op_tracker",
]
