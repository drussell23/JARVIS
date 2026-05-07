"""Move 6.5 Slice 3 — Multi-prior dispatch adapter.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "Slice 3 must be one new call site, not copy-paste.
   Antivenom: each prior's candidate is a full citizen of
   Iron Gate + SemanticGuardian + risk-tier + mutation budget
   before consensus aggregation (same as Move 6 discipline).
   Divergence after gates → NOTIFY_APPLY / operator-visible
   rationale ('which prior chose what') — never auto-apply on
   diverged AST/outcome classes. No hardcoded models — all
   generation routes resolve from existing policy."

This module is the **single composition layer** between
Slices 1+2 and the orchestrator's eventual call site. It
ships:

  1. **Pure decision function** :func:`evaluate_dispatch_decision`
     — gates the dispatch on (route, posture, op_id) without
     importing UrgencyRouter / DirectionInferrer (caller
     passes string values, same Slice 1 discipline).

  2. **Cost-snapshot adapter** :class:`CostGovernorAdapter` —
     binds an :class:`op_id` to the canonical
     :class:`CostGovernor.is_exceeded` accessor and presents
     Slice 2's parameterless :class:`CostBudgetSnapshot`
     Protocol. NEVER raises; missing / disabled governor →
     returns False (no spurious cancellations).

  3. **Pure action recommendation** :func:`recommend_action`
     — maps Move 6's :class:`ConsensusOutcome` to a closed
     4-value :class:`ConsensusActionRecommendation` taxonomy.
     Operator binding's "divergence → NOTIFY_APPLY" hook is
     the ESCALATE_TO_OPERATOR_REVIEW arm; consensus unanimous
     is the only ACCEPT_CANONICAL arm.

  4. **Pure rationale builder** :func:`build_rationale` —
     produces the operator-facing "which prior chose what"
     summary string for ledger rows + Slice 5 canvas surface.

  5. **Async dispatch helper** :func:`dispatch_multi_prior`
     — the **one new call site** the orchestrator will
     invoke. Composes the four above + Slice 1
     (:func:`materialize_priors`) + Slice 2
     (:func:`run_multi_prior_quorum`). Returns a frozen
     :class:`DispatchVerdict` carrying decision + verdict +
     recommendation + rationale.

**Antivenom invariant — load-bearing**: The cage discipline
(Iron Gate + SemanticGuardian + risk-tier + mutation budget
applied **per-candidate** before consensus aggregation) is
the **caller's** responsibility. Slice 3 stays substrate-pure
and does NOT import the cage modules. The orchestrator's call
site wraps its existing per-candidate cage logic into a
:class:`MultiPriorGenerator` and passes that to
:func:`dispatch_multi_prior`. This composition keeps Slice 3
authority-asymmetric while honoring the operator binding's
cage-citizen requirement.

**Authority asymmetry** (AST-pinned): no orchestrator /
iron_gate / providers / candidate_generator / change_engine /
semantic_guardian / plan_generator / urgency_router /
direction_inferrer / policy imports. Pure substrate adapter.

**Master flag** ``JARVIS_MULTI_PRIOR_DISPATCH_ENABLED``
default-FALSE per §33.1. When OFF, :func:`dispatch_multi_prior`
returns the canonical FALL_THROUGH verdict immediately
(zero-cost). When ON, fires only if Slice 1's
``JARVIS_MULTI_PRIOR_PLANNING_ENABLED`` AND Slice 2's
``JARVIS_MULTI_PRIOR_RUNNER_ENABLED`` are also ON — the three
masters compose with logical AND so the operator can stage
graduation independently per slice.

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import (
    Any, Dict, FrozenSet, List, Optional, Tuple,
)

from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
    PriorSet, materialize_priors,
    master_enabled as planning_master_enabled,
    should_fire_for_op,
    should_fire_for_posture,
    should_fire_for_route,
)
from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
    CostBudgetSnapshot,
    MultiPriorGenerator,
    MultiPriorRollOutcome,
    MultiPriorVerdictResult,
    master_enabled as runner_master_enabled,
    run_multi_prior_quorum,
)


logger = logging.getLogger(
    "Ouroboros.MultiPriorDispatch",
)


MULTI_PRIOR_DISPATCH_SCHEMA_VERSION: str = (
    "multi_prior_dispatch.1"
)


_TRUTHY: FrozenSet[str] = frozenset(
    {"1", "true", "yes", "on"},
)


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


class MultiPriorDecision(str, enum.Enum):
    """Closed 5-value taxonomy of dispatch-gate decisions.
    Every (op_id, route, posture) tuple maps to exactly one.

    ``ENABLED``         — All gates pass. Caller proceeds to
                          materialize priors + run multi-prior
                          quorum.
    ``DISABLED``        — Master flag off OR Slice 1 master
                          off OR Slice 2 master off. Caller
                          falls through to Move 6 path.
    ``SKIP_ROUTE``      — Master on but route is not COMPLEX.
                          Operator binding: only COMPLEX
                          fires multi-prior.
    ``SKIP_POSTURE``    — Master on, route ok, but posture is
                          not EXPLORE. Operator binding: only
                          EXPLORE fires multi-prior.
    ``SKIP_OP_BLANK``   — op_id is blank/whitespace.
                          Defensive sentinel."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    SKIP_ROUTE = "skip_route"
    SKIP_POSTURE = "skip_posture"
    SKIP_OP_BLANK = "skip_op_blank"


class ConsensusActionRecommendation(str, enum.Enum):
    """Closed 4-value taxonomy of post-consensus action
    recommendations. Maps Move 6's :class:`ConsensusOutcome`
    (5-value) into the operator-actionable space (4-value):
    DISABLED + FAILED both collapse into FALL_THROUGH because
    the caller's response is identical.

    ``ACCEPT_CANONICAL``           — Consensus unanimous (all
                                     K rolls agreed). Accept
                                     the canonical roll;
                                     existing risk-tier
                                     applies.
    ``CLAMP_TO_NOTIFY_APPLY``      — Majority consensus
                                     (threshold met but
                                     outliers exist). Clamp
                                     risk-tier upward to
                                     NOTIFY_APPLY so operator
                                     reviews before APPLY.
    ``ESCALATE_TO_OPERATOR_REVIEW``— Full disagreement.
                                     Operator MUST review
                                     with the "which prior
                                     chose what" rationale
                                     before any APPLY.
    ``FALL_THROUGH``               — Verdict was DISABLED /
                                     FAILED. Caller falls
                                     through to Move 6 path
                                     unchanged."""

    ACCEPT_CANONICAL = "accept_canonical"
    CLAMP_TO_NOTIFY_APPLY = "clamp_to_notify_apply"
    ESCALATE_TO_OPERATOR_REVIEW = (
        "escalate_to_operator_review"
    )
    FALL_THROUGH = "fall_through"


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_MULTI_PRIOR_DISPATCH_ENABLED`` master switch.
    Default-FALSE per §33.1: when OFF, :func:`dispatch_multi_prior`
    returns FALL_THROUGH (zero-cost). Composes with Slice 1's
    + Slice 2's master flags via logical AND inside
    :func:`evaluate_dispatch_decision`."""
    raw = os.environ.get(
        "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


def all_masters_enabled() -> bool:
    """True iff Slice 3 master AND Slice 1 master AND Slice 2
    master are all on. Pure read; NEVER raises."""
    return (
        master_enabled()
        and planning_master_enabled()
        and runner_master_enabled()
    )


# ---------------------------------------------------------------------------
# Frozen artifacts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchVerdict:
    """Composite dispatch outcome. Frozen §33.5 artifact.

    ``decision`` is the gate decision (always populated).

    ``prior_set`` / ``verdict_result`` are populated only
    when ``decision == ENABLED``; otherwise None.

    ``action_recommendation`` is the operator-actionable
    recommendation (always populated). When decision is not
    ENABLED, defaults to FALL_THROUGH so the caller's
    branch logic is uniform."""

    op_id: str
    decision: MultiPriorDecision
    action_recommendation: ConsensusActionRecommendation
    rationale: str
    prior_set: Optional[PriorSet] = None
    verdict_result: Optional[MultiPriorVerdictResult] = None
    schema_version: str = field(
        default=MULTI_PRIOR_DISPATCH_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": str(self.op_id),
            "decision": self.decision.value,
            "action_recommendation": (
                self.action_recommendation.value
            ),
            "rationale": str(self.rationale)[:2048],
            "prior_set": (
                self.prior_set.to_dict()
                if self.prior_set is not None
                else None
            ),
            "verdict_result": (
                self.verdict_result.to_dict()
                if self.verdict_result is not None
                else None
            ),
            "schema_version": str(self.schema_version),
        }

    @property
    def fired(self) -> bool:
        """True iff the gates passed AND the runner produced a
        verdict result (i.e. dispatch actually ran)."""
        return (
            self.decision is MultiPriorDecision.ENABLED
            and self.verdict_result is not None
        )


# ---------------------------------------------------------------------------
# Pure: dispatch decision
# ---------------------------------------------------------------------------


def evaluate_dispatch_decision(
    *,
    op_id: str,
    route: str,
    posture: str,
) -> MultiPriorDecision:
    """Pure decision function. Composes Slice 1's gate
    primitives + master flags. Returns one of 5 closed
    decisions. Pure; NEVER raises.

    Decision tree (first match wins):
      1. op_id blank → SKIP_OP_BLANK
      2. NOT all_masters_enabled → DISABLED
      3. Route gate fails → SKIP_ROUTE
      4. Posture gate fails → SKIP_POSTURE
      5. All gates pass → ENABLED
    """
    name = str(op_id or "").strip()
    if not name:
        return MultiPriorDecision.SKIP_OP_BLANK
    if not all_masters_enabled():
        return MultiPriorDecision.DISABLED
    if not should_fire_for_route(route):
        return MultiPriorDecision.SKIP_ROUTE
    if not should_fire_for_posture(posture):
        return MultiPriorDecision.SKIP_POSTURE
    # Defensive: re-compose Slice 1's full gate predicate so
    # we get its master-flag check too. Should already be
    # True here per all_masters_enabled, but the defensive
    # double-check is cheap and proves out the composition.
    if not should_fire_for_op(
        op_id=name, route=route, posture=posture,
    ):
        return MultiPriorDecision.DISABLED
    return MultiPriorDecision.ENABLED


# ---------------------------------------------------------------------------
# Pure: action recommendation
# ---------------------------------------------------------------------------


# Mapping from Move 6 ConsensusOutcome.value → recommendation.
# Module-level table (auditable; AST-pinnable). Operator
# binding 2026-05-07: divergence → NOTIFY_APPLY / escalate;
# never auto-apply on diverged outcomes.
_OUTCOME_TO_ACTION: Dict[str, ConsensusActionRecommendation] = {
    "consensus": (
        ConsensusActionRecommendation.ACCEPT_CANONICAL
    ),
    "majority_consensus": (
        ConsensusActionRecommendation.CLAMP_TO_NOTIFY_APPLY
    ),
    "disagreement": (
        ConsensusActionRecommendation.ESCALATE_TO_OPERATOR_REVIEW  # noqa: E501
    ),
    "disabled": ConsensusActionRecommendation.FALL_THROUGH,
    "failed": ConsensusActionRecommendation.FALL_THROUGH,
}


def recommend_action(
    verdict_result: Optional[MultiPriorVerdictResult],
) -> ConsensusActionRecommendation:
    """Pure mapping from a :class:`MultiPriorVerdictResult` to
    an operator-actionable recommendation. Returns
    FALL_THROUGH on None / malformed verdict / unknown
    outcome (defensive). NEVER raises."""
    if verdict_result is None:
        return ConsensusActionRecommendation.FALL_THROUGH
    consensus = getattr(
        verdict_result, "consensus_verdict", None,
    )
    if consensus is None:
        return ConsensusActionRecommendation.FALL_THROUGH
    outcome = getattr(consensus, "outcome", None)
    if outcome is None:
        return ConsensusActionRecommendation.FALL_THROUGH
    try:
        outcome_value = str(outcome.value).strip().lower()
    except (AttributeError, TypeError):
        return ConsensusActionRecommendation.FALL_THROUGH
    return _OUTCOME_TO_ACTION.get(
        outcome_value,
        ConsensusActionRecommendation.FALL_THROUGH,
    )


# ---------------------------------------------------------------------------
# Pure: operator-facing rationale builder
# ---------------------------------------------------------------------------


_RATIONALE_DIFF_PREVIEW_CHARS: int = 120


def build_rationale(
    verdict_result: Optional[MultiPriorVerdictResult],
) -> str:
    """Build the operator-facing "which prior chose what"
    summary. Pure function; NEVER raises. Returns empty
    string on None / malformed verdict.

    Format (one line per roll):
      [outcome] prior_id=<id> sig=<sig8> diff_preview=<...>

    Operator binding requirement: "operator-visible rationale
    ('which prior chose what')" — every prior surfaces in
    the rationale even if its outcome wasn't COMPLETED, so
    the operator can see (e.g.) "defensive prior timed out;
    minimalist prior produced X; composition_first prior
    produced Y; type_strict prior was cancelled at budget
    cap"."""
    if verdict_result is None:
        return ""
    rolls = getattr(verdict_result, "rolls", ())
    if not rolls:
        return ""
    lines: List[str] = []
    consensus = getattr(
        verdict_result, "consensus_verdict", None,
    )
    if consensus is not None:
        try:
            outcome_value = str(consensus.outcome.value)
        except (AttributeError, TypeError):
            outcome_value = "unknown"
        lines.append(
            f"consensus={outcome_value} "
            f"agreement={getattr(consensus, 'agreement_count', 0)}/"  # noqa: E501
            f"{getattr(consensus, 'total_rolls', 0)}"
        )
    for roll in rolls:
        try:
            outcome = (
                getattr(roll, "outcome", None).value
            )
        except (AttributeError, TypeError):
            outcome = "unknown"
        prior_id = getattr(roll, "prior_id", "")
        sig = (
            (getattr(roll, "ast_signature", "") or "")[:8]
        )
        diff_full = getattr(roll, "candidate_diff", "") or ""
        # Single-line preview: replace newlines + truncate.
        preview = " ".join(diff_full.split())[
            :_RATIONALE_DIFF_PREVIEW_CHARS
        ]
        if len(diff_full) > _RATIONALE_DIFF_PREVIEW_CHARS:
            preview = preview + "…"
        lines.append(
            f"[{outcome}] prior_id={prior_id} "
            f"sig={sig or '—'} "
            f"diff_preview={preview!r}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cost-snapshot adapter — composes the canonical CostGovernor
# ---------------------------------------------------------------------------


@dataclass
class CostGovernorAdapter:
    """Adapts :class:`CostGovernor.is_exceeded(op_id)` to
    Slice 2's parameterless :class:`CostBudgetSnapshot`
    protocol. Bound to one ``op_id`` at construction.

    Composition over duplication: NO local copy of cap math;
    NO independent tracking; NO mutation of the governor.
    Read-only adapter — AST-pinned via
    ``multi_prior_dispatch_cost_adapter_read_only``.

    Resolves the governor lazily via
    :func:`get_default_cost_governor` if none injected — same
    discipline as the rest of the codebase's singleton
    accessors. NEVER raises; missing / disabled governor →
    returns False (no spurious cancellations).
    """

    op_id: str
    governor: Optional[Any] = None  # CostGovernor

    def is_exceeded(self) -> bool:
        """Implements :class:`CostBudgetSnapshot`. Defensive
        at every layer."""
        try:
            gov = self.governor
            if gov is None:
                # Lazy-import + lazy-resolve.
                from backend.core.ouroboros.governance.cost_governor import (  # noqa: E501
                    get_default_cost_governor,
                )
                gov = get_default_cost_governor()
            if gov is None:
                return False
            return bool(gov.is_exceeded(str(self.op_id)))
        except Exception:  # noqa: BLE001 — defensive
            return False


# ---------------------------------------------------------------------------
# Async dispatch helper — the one new call site
# ---------------------------------------------------------------------------


async def dispatch_multi_prior(
    generator: MultiPriorGenerator,
    *,
    op_id: str,
    route: str,
    posture: str,
    k: Optional[int] = None,
    cost_governor: Optional[Any] = None,
    timeout_per_roll_s: float = 60.0,
    grace_period_s: float = 5.0,
    cost_check_interval_s: float = 1.0,
    enabled_override: Optional[bool] = None,
    threshold: Optional[int] = None,
) -> DispatchVerdict:
    """The single composition point between Slices 1+2 and
    the orchestrator's call site. NEVER raises (cancellation
    re-raised per asyncio convention).

    Decision tree:
      1. ``enabled_override`` (test override) OR
         :func:`evaluate_dispatch_decision` returns non-
         ENABLED → return DispatchVerdict with
         decision=<value> + recommendation=FALL_THROUGH +
         empty prior_set / verdict_result.
      2. Materialize priors (Slice 1). If None (defensive,
         shouldn't happen because gate already passed) →
         FALL_THROUGH.
      3. Build :class:`CostGovernorAdapter` bound to ``op_id``.
      4. Run multi-prior quorum (Slice 2) with the adapter as
         the cost-budget snapshot.
      5. Map verdict outcome → action recommendation
         (:func:`recommend_action`).
      6. Build operator-facing rationale
         (:func:`build_rationale`).
      7. Wrap into :class:`DispatchVerdict`.

    Operator binding: This is the **one new call site**. The
    orchestrator's ``candidate_generator`` integration in a
    follow-on slice composes this function as ONE adapter
    boundary — never duplicates the K rolls inline."""
    decision = (
        MultiPriorDecision.ENABLED
        if enabled_override is True
        else evaluate_dispatch_decision(
            op_id=op_id, route=route, posture=posture,
        )
    )
    if decision is not MultiPriorDecision.ENABLED:
        return DispatchVerdict(
            op_id=str(op_id),
            decision=decision,
            action_recommendation=(
                ConsensusActionRecommendation.FALL_THROUGH
            ),
            rationale="",
        )

    # Step 2: materialize priors via Slice 1.
    prior_set = materialize_priors(
        op_id=op_id, route=route, posture=posture, k=k,
    )
    if prior_set is None:
        # Should not happen — gate just passed — but
        # defensive against env-flip race conditions.
        return DispatchVerdict(
            op_id=str(op_id),
            decision=MultiPriorDecision.DISABLED,
            action_recommendation=(
                ConsensusActionRecommendation.FALL_THROUGH
            ),
            rationale=(
                "materialize_priors returned None despite "
                "passing gate (env-flip race)"
            ),
        )

    # Step 3: build cost-snapshot adapter bound to op_id.
    cost_adapter = CostGovernorAdapter(
        op_id=str(op_id), governor=cost_governor,
    )

    # Step 4: run multi-prior quorum (Slice 2).
    verdict_result = await run_multi_prior_quorum(
        generator,
        op_id=str(op_id),
        prior_set=prior_set,
        timeout_per_roll_s=timeout_per_roll_s,
        grace_period_s=grace_period_s,
        cost_governor_snapshot=cost_adapter,
        cost_check_interval_s=cost_check_interval_s,
        enabled_override=enabled_override,
        threshold=threshold,
    )

    # Step 5+6: action recommendation + rationale.
    recommendation = recommend_action(verdict_result)
    rationale = build_rationale(verdict_result)

    return DispatchVerdict(
        op_id=str(op_id),
        decision=MultiPriorDecision.ENABLED,
        action_recommendation=recommendation,
        rationale=rationale,
        prior_set=prior_set,
        verdict_result=verdict_result,
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds the master flag this module
    reads."""
    try:
        registry.register(
            name="JARVIS_MULTI_PRIOR_DISPATCH_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for Move 6.5 Slice 3 "
                "dispatch adapter. Default-FALSE per §33.1; "
                "when off, dispatch_multi_prior returns "
                "FALL_THROUGH immediately. Composes with "
                "Slice 1's JARVIS_MULTI_PRIOR_PLANNING_ENABLED "  # noqa: E501
                "AND Slice 2's "
                "JARVIS_MULTI_PRIOR_RUNNER_ENABLED via "
                "logical AND so the operator can stage "
                "graduation independently per slice."
            ),
            category="Generation",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "verification/multi_prior_dispatch.py"
            ),
            example=(
                "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorDispatch] master-flag seeding "
            "failed (non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``multi_prior_dispatch_decision_taxonomy_5_values`` —
         closed enum.
      2. ``multi_prior_dispatch_action_taxonomy_4_values`` —
         closed enum.
      3. ``multi_prior_dispatch_master_default_false`` — §33.1
         producer flag stays default-FALSE.
      4. ``multi_prior_dispatch_authority_asymmetry`` — no
         orchestrator-tier imports (cage application happens
         at the orchestrator call site, not here).
      5. ``multi_prior_dispatch_cost_adapter_read_only`` —
         :class:`CostGovernorAdapter` MUST NOT mutate the
         governor (no setattr / item assignment / method
         calls other than ``is_exceeded``).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/verification/"
        "multi_prior_dispatch.py"
    )

    def _validate_decision_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "ENABLED", "DISABLED",
            "SKIP_ROUTE", "SKIP_POSTURE", "SKIP_OP_BLANK",
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "MultiPriorDecision"
            ):
                seen: set = set()
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for tgt in stmt.targets:
                            if isinstance(tgt, ast.Name):
                                seen.add(tgt.id)
                missing = required - seen
                extra = seen - required
                if missing:
                    violations.append(
                        f"MultiPriorDecision missing "
                        f"{sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"MultiPriorDecision has extra "
                        f"{sorted(extra)} — taxonomy is "
                        f"closed at 5 values"
                    )
                return tuple(violations)
        violations.append(
            "MultiPriorDecision class missing"
        )
        return tuple(violations)

    def _validate_action_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "ACCEPT_CANONICAL",
            "CLAMP_TO_NOTIFY_APPLY",
            "ESCALATE_TO_OPERATOR_REVIEW",
            "FALL_THROUGH",
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and (
                    node.name
                    == "ConsensusActionRecommendation"
                )
            ):
                seen: set = set()
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for tgt in stmt.targets:
                            if isinstance(tgt, ast.Name):
                                seen.add(tgt.id)
                missing = required - seen
                extra = seen - required
                if missing:
                    violations.append(
                        f"ConsensusActionRecommendation "
                        f"missing {sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"ConsensusActionRecommendation has "
                        f"extra {sorted(extra)} — closed at "
                        f"4 values"
                    )
                return tuple(violations)
        violations.append(
            "ConsensusActionRecommendation class missing"
        )
        return tuple(violations)

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "master_enabled":
                    target_func = node
                    break
        if target_func is None:
            violations.append("master_enabled() missing")
            return tuple(violations)
        empty_returns_false = False
        for sub in ast.walk(target_func):
            if not isinstance(sub, ast.If):
                continue
            test = sub.test
            for cmp_node in ast.walk(test):
                if not isinstance(cmp_node, ast.Compare):
                    continue
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], ast.Eq,
                ):
                    continue
                operands_have_empty_str = False
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, ast.Constant)
                        and operand.value == ""
                    ):
                        operands_have_empty_str = True
                        break
                if not operands_have_empty_str:
                    continue
                for body_stmt in sub.body:
                    if isinstance(body_stmt, ast.Return):
                        if (
                            isinstance(
                                body_stmt.value, ast.Constant,
                            )
                            and body_stmt.value.value is False
                        ):
                            empty_returns_false = True
                            break
                if empty_returns_false:
                    break
            if empty_returns_false:
                break
        if not empty_returns_false:
            violations.append(
                "master_enabled() MUST return False on empty "
                "env-var string per §33.1"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_substring = (
            "iron_gate", "providers", "candidate_generator",
            "urgency_router", "change_engine",
            "semantic_guardian", "plan_generator",
            "direction_inferrer",
        )
        forbidden_exact = {"orchestrator", "policy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                if any(
                    "multi_prior_dispatch" in s
                    for s in segments
                ):
                    continue
                # Allow cost_governor — it's the substrate
                # the adapter composes (via lazy import
                # inside CostGovernorAdapter). The cage
                # forbiddens are the orchestrator-tier ones.
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"multi_prior_dispatch.py MUST "
                            f"NOT import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"multi_prior_dispatch.py MUST "
                            f"NOT import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_cost_adapter_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """:class:`CostGovernorAdapter` MUST NOT mutate the
        governor. Allowed: ``self.governor.is_exceeded(...)``
        method call. Forbidden: any :class:`Attribute` write
        target whose object name == ``self.governor`` or
        ``gov``, OR any method call other than
        ``is_exceeded`` on those names.
        """
        violations: list = []
        target_class: Optional[ast.ClassDef] = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CostGovernorAdapter"
            ):
                target_class = node
                break
        if target_class is None:
            violations.append(
                "CostGovernorAdapter class missing"
            )
            return tuple(violations)

        def _is_governor_ref(
            n: ast.expr,
        ) -> bool:
            """Return True iff node references the governor
            (either ``self.governor`` or local ``gov``)."""
            if isinstance(n, ast.Attribute):
                if (
                    isinstance(n.value, ast.Name)
                    and n.value.id == "self"
                    and n.attr == "governor"
                ):
                    return True
            if (
                isinstance(n, ast.Name)
                and n.id == "gov"
            ):
                return True
            return False

        for sub in ast.walk(target_class):
            # Forbid Attribute assignment on governor.
            if isinstance(sub, ast.Assign):
                for tgt in sub.targets:
                    if isinstance(tgt, ast.Attribute):
                        if _is_governor_ref(tgt.value):
                            violations.append(
                                f"CostGovernorAdapter MUST "
                                f"NOT mutate governor via "
                                f"attribute assignment "
                                f"(line {sub.lineno})"
                            )
            # Forbid method calls other than is_exceeded.
            if isinstance(sub, ast.Call):
                func = sub.func
                if isinstance(func, ast.Attribute):
                    if _is_governor_ref(func.value):
                        if func.attr != "is_exceeded":
                            violations.append(
                                f"CostGovernorAdapter MUST "
                                f"only call is_exceeded() "
                                f"on the governor; found "
                                f"{func.attr!r} (line "
                                f"{sub.lineno})"
                            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_dispatch_"
                "decision_taxonomy_5_values"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 3 — MultiPriorDecision is "
                "closed at 5 values."
            ),
            validate=_validate_decision_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_dispatch_"
                "action_taxonomy_4_values"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 3 — "
                "ConsensusActionRecommendation is closed "
                "at 4 values."
            ),
            validate=_validate_action_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_dispatch_master_default_false"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 3 — §33.1 master flag stays "
                "default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_dispatch_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 3 — substrate purity: no "
                "orchestrator-tier imports. Cage application "
                "happens at the orchestrator call site, not "
                "here."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_dispatch_"
                "cost_adapter_read_only"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 3 — CostGovernorAdapter is "
                "read-only over the canonical CostGovernor "
                "(no attribute mutation; only "
                "is_exceeded() method call permitted)."
            ),
            validate=_validate_cost_adapter_read_only,
        ),
    ]


__all__ = [
    "MULTI_PRIOR_DISPATCH_SCHEMA_VERSION",
    "ConsensusActionRecommendation",
    "CostGovernorAdapter",
    "DispatchVerdict",
    "MultiPriorDecision",
    "all_masters_enabled",
    "build_rationale",
    "dispatch_multi_prior",
    "evaluate_dispatch_decision",
    "master_enabled",
    "recommend_action",
    "register_flags",
    "register_shipped_invariants",
]
