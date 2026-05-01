"""Move 6 Slice 4 — Risk-tier gate + orchestrator hook.

Decides whether a given op should fire Generative Quorum based on:

  1. Master flag (``JARVIS_GENERATIVE_QUORUM_ENABLED`` from Slice 1)
  2. Sub-gate flag (``JARVIS_QUORUM_GATE_ENABLED`` introduced here)
  3. Provider route (refuses BG/SPEC via ``COST_GATED_ROUTES``
     constant from ``cost_contract_assertion`` — STRUCTURAL cost-
     contract preservation per PRD §26.6, AST-pinned by Slice 5)
  4. Risk tier (only fires for APPROVAL_REQUIRED+ where K× cost
     is justified by stakes)

When all four green: combines the gate check with Slice 3's
``run_quorum`` + a 5-value action mapping that translates
``ConsensusOutcome`` into orchestrator-facing actions:

  * CONSENSUS         → PROCEED_WITH_CANDIDATE (accept canonical roll)
  * MAJORITY_CONSENSUS → PROCEED_NOTIFY_APPLY (accept + bump risk_tier
                          to NOTIFY_APPLY so operator sees it)
  * DISAGREEMENT      → ESCALATE_BLOCKED (route through existing
                          BLOCKED-tier path; no new escalation surface)
  * DISABLED          → FALL_THROUGH_SINGLE (orchestrator falls back
                          to single-candidate behavior — byte-for-byte
                          equivalent to pre-Quorum baseline)
  * FAILED            → FALL_THROUGH_SINGLE (defensive — never block
                          on Quorum failure)

Direct-solve principles:

  * **Asynchronous-ready** — ``invoke_quorum_for_op`` is async and
    awaits Slice 3's ``run_quorum`` directly.

  * **Dynamic** — sub-gate + tier-threshold + master all env-tunable
    with asymmetric semantics (empty/whitespace = unset = current
    default; explicit truthy/falsy hot-reverts).

  * **Adaptive** — accepts both enum and string risk-tier inputs
    (orchestrator may pass ``RiskTier.APPROVAL_REQUIRED`` enum or
    ``"approval_required"`` string).

  * **Intelligent** — gate decision is structured
    (``QuorumGateDecision`` with reason field) so orchestrator
    logs every decision for §8 observability without re-deriving.

  * **Robust** — ``should_invoke_quorum`` and ``invoke_quorum_for_
    op`` are total: every input maps to exactly one verdict.
    NEVER raises.

  * **No hardcoding** — tier eligibility is a frozenset constant;
    cost-gated routes consumed from ``cost_contract_assertion``
    (single source of truth for the structural cost guard).

Authority invariants (AST-pinned by companion tests + Slice 5):

  * Imports stdlib + Slice 1 (generative_quorum) + Slice 2
    (ast_canonical, transitively via Slice 3) + Slice 3
    (generative_quorum_runner) + ``cost_contract_assertion``
    (for ``COST_GATED_ROUTES``).
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / semantic_guardian / semantic_firewall
    / providers / doubleword_provider / urgency_router /
    auto_action_router / subagent_scheduler / tool_executor /
    risk_engine.
  * AST-pinned: gate MUST reference ``COST_GATED_ROUTES`` symbol —
    catches a refactor that drops the cost guard.
  * No mutation tools.
  * No exec/eval/compile.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from backend.core.ouroboros.governance.cost_contract_assertion import (
    COST_GATED_ROUTES,
)
from backend.core.ouroboros.governance.verification.generative_quorum import (
    ConsensusOutcome,
    ConsensusVerdict,
    quorum_enabled,
)
from backend.core.ouroboros.governance.verification.generative_quorum_runner import (
    QuorumRunResult,
    RollGenerator,
    run_quorum,
)

logger = logging.getLogger(__name__)


GENERATIVE_QUORUM_GATE_SCHEMA_VERSION: str = (
    "generative_quorum_gate.1"
)


# ---------------------------------------------------------------------------
# Risk-tier eligibility — string-based to keep the gate decoupled
# from risk_engine.RiskTier enum (which lives elsewhere; this module
# is intentionally agnostic so refactors in risk_engine don't ripple
# through Move 6).
# ---------------------------------------------------------------------------


RISK_TIER_SAFE_AUTO: str = "safe_auto"
RISK_TIER_NOTIFY_APPLY: str = "notify_apply"
RISK_TIER_APPROVAL_REQUIRED: str = "approval_required"
RISK_TIER_BLOCKED: str = "blocked"


# Tiers that justify Quorum's K× cost. Mirrors the scope doc's
# "APPROVAL_REQUIRED+ tier" rule. BLOCKED is included for defense
# in depth — if an op surfaces at BLOCKED tier mid-pipeline, Quorum
# still adds signal even though the op cannot auto-apply.
QUORUM_ELIGIBLE_TIERS: frozenset = frozenset({
    RISK_TIER_APPROVAL_REQUIRED,
    RISK_TIER_BLOCKED,
})


# ---------------------------------------------------------------------------
# Sub-gate env knob
# ---------------------------------------------------------------------------


def quorum_gate_enabled() -> bool:
    """``JARVIS_QUORUM_GATE_ENABLED`` (default ``false`` until
    Slice 5 graduation).

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit ``0``/``false``/``no``/``off`` evaluates
    false; explicit truthy values evaluate true. Re-read on every
    call so flips hot-revert without restart."""
    raw = os.environ.get(
        "JARVIS_QUORUM_GATE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-false until Slice 5 graduation
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of orchestrator actions
# (J.A.R.M.A.T.R.I.X. — every input maps to exactly one)
# ---------------------------------------------------------------------------


class QuorumActionMapping(str, enum.Enum):
    """Closed 5-value taxonomy of post-Quorum orchestrator actions.

    ``PROCEED_WITH_CANDIDATE``  — CONSENSUS reached. Use the
                                  canonical roll's candidate; no
                                  tier escalation.
    ``PROCEED_NOTIFY_APPLY``    — MAJORITY_CONSENSUS reached. Use
                                  the majority roll's candidate
                                  but bump risk_tier to
                                  NOTIFY_APPLY so operator sees
                                  the discrepancy.
    ``ESCALATE_BLOCKED``        — DISAGREEMENT. Route through
                                  existing BLOCKED-tier escalation
                                  path (no new escalation surface).
    ``FALL_THROUGH_SINGLE``     — DISABLED OR FAILED. Fall through
                                  to existing single-candidate
                                  behavior. Byte-for-byte
                                  equivalent to no-Quorum baseline.
    ``INVALID``                 — Defensive sentinel for inputs
                                  outside the closed verdict
                                  taxonomy. Should never fire if
                                  Slice 1's ConsensusOutcome enum
                                  remains exhaustive."""

    PROCEED_WITH_CANDIDATE = "proceed_with_candidate"
    PROCEED_NOTIFY_APPLY = "proceed_notify_apply"
    ESCALATE_BLOCKED = "escalate_blocked"
    FALL_THROUGH_SINGLE = "fall_through_single"
    INVALID = "invalid"


# ---------------------------------------------------------------------------
# Frozen dataclasses — propagation-safe across async boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuorumGateDecision:
    """Structured gate decision for §8 observability. Orchestrator
    logs every decision regardless of outcome — operator can audit
    why Quorum did or didn't fire.

    ``reason`` is one of a closed string set:
      ``master_disabled`` / ``gate_disabled`` /
      ``cost_gated_route`` / ``tier_below_threshold`` /
      ``invalid_input`` / ``ok``"""

    should_invoke: bool
    reason: str
    risk_tier: str
    current_route: str
    schema_version: str = GENERATIVE_QUORUM_GATE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "should_invoke": self.should_invoke,
            "reason": self.reason,
            "risk_tier": self.risk_tier,
            "current_route": self.current_route,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class QuorumGateResult:
    """Aggregate result of one ``invoke_quorum_for_op`` call.
    Frozen for safe propagation. ``run_result`` is None when the
    gate refused to fire (orchestrator falls through to single-
    candidate path)."""

    decision: QuorumGateDecision
    action: QuorumActionMapping
    run_result: Optional[QuorumRunResult] = None
    schema_version: str = GENERATIVE_QUORUM_GATE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.to_dict(),
            "action": self.action.value,
            "run_result": (
                self.run_result.to_dict()
                if self.run_result is not None else None
            ),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Internal: input normalization (string/enum tolerant)
# ---------------------------------------------------------------------------


def _normalize_tier(tier: Any) -> str:
    """Normalize a risk-tier input to canonical lowercase string.
    Accepts enum (with ``.name`` or ``.value`` attribute), string,
    or anything stringifiable. NEVER raises — returns empty string
    on garbage input (gate refuses empty as ``invalid_input``)."""
    try:
        if tier is None:
            return ""
        # Enum with .name (preferred — RiskTier.SAFE_AUTO.name ==
        # "SAFE_AUTO")
        name = getattr(tier, "name", None)
        if isinstance(name, str) and name:
            return name.strip().lower()
        # Enum with .value (string-valued enum like ConsensusOutcome)
        value = getattr(tier, "value", None)
        if isinstance(value, str) and value:
            return value.strip().lower()
        # Plain string
        if isinstance(tier, str):
            return tier.strip().lower()
        # Last resort
        return str(tier).strip().lower()
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _normalize_route(route: Any) -> str:
    """Normalize a provider-route input to canonical lowercase
    string. NEVER raises."""
    try:
        if route is None:
            return ""
        if isinstance(route, str):
            return route.strip().lower()
        value = getattr(route, "value", None)
        if isinstance(value, str) and value:
            return value.strip().lower()
        name = getattr(route, "name", None)
        if isinstance(name, str) and name:
            return name.strip().lower()
        return str(route).strip().lower()
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# Public: gate decision
# ---------------------------------------------------------------------------


def should_invoke_quorum(
    *,
    risk_tier: Any,
    current_route: Any,
    master_override: Optional[bool] = None,
    gate_override: Optional[bool] = None,
) -> QuorumGateDecision:
    """Pure decision function. Returns ``QuorumGateDecision`` with
    structured reason. NEVER raises.

    Decision tree (every input maps to exactly one decision):

      1. Master flag off → ``master_disabled``
      2. Sub-gate off → ``gate_disabled``
      3. ``current_route`` is in ``COST_GATED_ROUTES`` →
         ``cost_gated_route`` (STRUCTURAL cost-contract guard)
      4. Risk tier not in ``QUORUM_ELIGIBLE_TIERS`` →
         ``tier_below_threshold``
      5. Risk tier OR route is empty/garbage → ``invalid_input``
      6. Otherwise → ``ok``"""
    norm_tier = _normalize_tier(risk_tier)
    norm_route = _normalize_route(current_route)

    # Step 1: master flag
    is_master_on = (
        master_override if master_override is not None
        else quorum_enabled()
    )
    if not is_master_on:
        return QuorumGateDecision(
            should_invoke=False,
            reason="master_disabled",
            risk_tier=norm_tier,
            current_route=norm_route,
        )

    # Step 2: sub-gate
    is_gate_on = (
        gate_override if gate_override is not None
        else quorum_gate_enabled()
    )
    if not is_gate_on:
        return QuorumGateDecision(
            should_invoke=False,
            reason="gate_disabled",
            risk_tier=norm_tier,
            current_route=norm_route,
        )

    # Step 5 (early): garbage input
    if not norm_tier or not norm_route:
        return QuorumGateDecision(
            should_invoke=False,
            reason="invalid_input",
            risk_tier=norm_tier,
            current_route=norm_route,
        )

    # Step 3: cost-gated route refusal — STRUCTURAL guard via
    # COST_GATED_ROUTES from cost_contract_assertion. AST-pinned
    # by Slice 5 graduation: any refactor that drops this check
    # gets caught structurally.
    if norm_route in COST_GATED_ROUTES:
        return QuorumGateDecision(
            should_invoke=False,
            reason="cost_gated_route",
            risk_tier=norm_tier,
            current_route=norm_route,
        )

    # Step 4: tier eligibility
    if norm_tier not in QUORUM_ELIGIBLE_TIERS:
        return QuorumGateDecision(
            should_invoke=False,
            reason="tier_below_threshold",
            risk_tier=norm_tier,
            current_route=norm_route,
        )

    # Step 6: green-light
    return QuorumGateDecision(
        should_invoke=True,
        reason="ok",
        risk_tier=norm_tier,
        current_route=norm_route,
    )


# ---------------------------------------------------------------------------
# Public: ConsensusOutcome → action mapping
# ---------------------------------------------------------------------------


def map_consensus_to_action(
    verdict: ConsensusVerdict,
) -> QuorumActionMapping:
    """Map ``ConsensusOutcome`` → orchestrator action. Total —
    every input maps to exactly one action. NEVER raises.

    The mapping pins the closed 5-value taxonomy:
      CONSENSUS           → PROCEED_WITH_CANDIDATE
      MAJORITY_CONSENSUS  → PROCEED_NOTIFY_APPLY
      DISAGREEMENT        → ESCALATE_BLOCKED
      DISABLED            → FALL_THROUGH_SINGLE
      FAILED              → FALL_THROUGH_SINGLE"""
    try:
        if not isinstance(verdict, ConsensusVerdict):
            return QuorumActionMapping.INVALID
        if verdict.outcome is ConsensusOutcome.CONSENSUS:
            return QuorumActionMapping.PROCEED_WITH_CANDIDATE
        if verdict.outcome is ConsensusOutcome.MAJORITY_CONSENSUS:
            return QuorumActionMapping.PROCEED_NOTIFY_APPLY
        if verdict.outcome is ConsensusOutcome.DISAGREEMENT:
            return QuorumActionMapping.ESCALATE_BLOCKED
        if verdict.outcome in (
            ConsensusOutcome.DISABLED,
            ConsensusOutcome.FAILED,
        ):
            return QuorumActionMapping.FALL_THROUGH_SINGLE
        return QuorumActionMapping.INVALID
    except Exception:  # noqa: BLE001 — defensive
        return QuorumActionMapping.INVALID


# ---------------------------------------------------------------------------
# Public: orchestrator-facing entry point
# ---------------------------------------------------------------------------


async def invoke_quorum_for_op(
    *,
    risk_tier: Any,
    current_route: Any,
    generator: RollGenerator,
    k: Optional[int] = None,
    threshold: Optional[int] = None,
    timeout_per_roll_s: float = 60.0,
    is_multi_file: bool = False,
    seed_base: int = 0,
    cost_estimate_per_roll_usd: float = 0.0,
    master_override: Optional[bool] = None,
    gate_override: Optional[bool] = None,
) -> QuorumGateResult:
    """Combine gate check + Slice 3 runner + action mapping.
    Orchestrator-facing entry point. NEVER raises.

    Decision tree:

      1. ``should_invoke_quorum`` → if not, return
         FALL_THROUGH_SINGLE with no run_result.
      2. ``run_quorum`` (Slice 3) fires K rolls in parallel.
      3. ``map_consensus_to_action`` translates verdict → action."""
    try:
        decision = should_invoke_quorum(
            risk_tier=risk_tier,
            current_route=current_route,
            master_override=master_override,
            gate_override=gate_override,
        )

        if not decision.should_invoke:
            return QuorumGateResult(
                decision=decision,
                action=QuorumActionMapping.FALL_THROUGH_SINGLE,
                run_result=None,
            )

        # When we invoke run_quorum we pass enabled_override=True
        # because the gate's master_override has already validated
        # the master flag (Step 1 above). This keeps the runner
        # from re-reading the env mid-flight (defends against an
        # operator flip between gate-eval and runner-fire).
        run_result = await run_quorum(
            generator,
            k=k,
            threshold=threshold,
            timeout_per_roll_s=timeout_per_roll_s,
            is_multi_file=is_multi_file,
            seed_base=seed_base,
            cost_estimate_per_roll_usd=(
                cost_estimate_per_roll_usd
            ),
            enabled_override=True,
        )

        action = map_consensus_to_action(run_result.verdict)

        return QuorumGateResult(
            decision=decision,
            action=action,
            run_result=run_result,
        )
    except asyncio.CancelledError:
        # Surface cancellation — orchestrator is shutting down
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[QuorumGate] invoke_quorum_for_op raised: %s", exc,
        )
        # Defensive fall-through — orchestrator continues with
        # single-candidate path on any unexpected failure
        return QuorumGateResult(
            decision=QuorumGateDecision(
                should_invoke=False,
                reason="invalid_input",
                risk_tier=_normalize_tier(risk_tier),
                current_route=_normalize_route(current_route),
            ),
            action=QuorumActionMapping.FALL_THROUGH_SINGLE,
            run_result=None,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "GENERATIVE_QUORUM_GATE_SCHEMA_VERSION",
    "QUORUM_ELIGIBLE_TIERS",
    "QuorumActionMapping",
    "QuorumGateDecision",
    "QuorumGateResult",
    "RISK_TIER_APPROVAL_REQUIRED",
    "RISK_TIER_BLOCKED",
    "RISK_TIER_NOTIFY_APPLY",
    "RISK_TIER_SAFE_AUTO",
    "invoke_quorum_for_op",
    "map_consensus_to_action",
    "quorum_gate_enabled",
    "should_invoke_quorum",
]
