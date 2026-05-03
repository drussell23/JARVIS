"""Move 3 — Auto-action advisory router (PRD §27.4.3).

Closes the verification → action loop gap diagnosed by the §27 v6
brutal review. Pass C surfaces emit *proposals*, verification emits
*pass/fail per claim*, and Priority 1's confidence-collapse pipeline
emits *verdicts*. None of those signals currently auto-modify the
NEXT op when a recent claim failed. This router is the missing
teeth — a thin advisory primitive that consumes those existing
signals and produces an explicit ``AdvisoryAction`` proposal.

The router is **always advisory**. It NEVER mutates ``ctx``
directly; it NEVER drives an automatic risk-tier or route change.
Operators review proposals in the IDE stream + ``/auto-action``
REPL surface (Slice 4); any actual modification crosses the
existing operator-approval-bound surfaces. Sub-flag
``JARVIS_AUTO_ACTION_ENFORCE`` is the mutation boundary and is
**locked off** until the operator has reviewed sufficient shadow
proposals.

Mirror of ``verification.confidence_route_advisor``
---------------------------------------------------
Same architectural pattern, deliberately copied to avoid
re-inventing the wheel:

  * Pure-data advisor — frozen dataclass, no side effects
    beyond logging.
  * Master flag default-false in Slice 1 (this slice). Graduated
    to default-true in Slice 4 after a single clean shadow soak.
  * Env-driven thresholds, all clamped defensively, all
    re-read at call time (monkeypatch-friendly, hot-revert).
  * AST-pinned authority invariants — no orchestrator, no
    candidate_generator, no providers, no urgency_router imports.
  * Cost-contract structural guard wired through
    ``cost_contract_assertion.CostContractViolation``.

Operator directive (J.A.R.M.A.T.R.I.X.)
---------------------------------------
``no_action`` is an EXPLICIT return value on the happy path —
never ``None``, never an implicit fall-through. The 5-value
``AdvisoryActionType`` enum is the entire decision space; every
input maps to exactly one of these five outcomes.

Cost contract preservation (PRD §26.6, load-bearing)
----------------------------------------------------
The router cannot propose any action that would escalate a
BG/SPEC op to a higher-cost provider. Since the router's action
types do not directly carry a route field, this is naturally
satisfied today, but the structural guard is encoded in
``_propose_action`` to future-proof against any downstream
caller that might misinterpret an action as carrying a route
change.

Authority invariants (AST-pinned by tests)
------------------------------------------

  * No imports of orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / semantic_firewall / providers /
    doubleword_provider / urgency_router (cost-contract isolation).
  * Pure stdlib + the ``cost_contract_assertion`` exception class.
  * NEVER raises out of the public dispatcher EXCEPT
    ``CostContractViolation`` from the structural guard — and
    that is intentional: a violation is fatal, not recoverable.
  * ``_propose_action`` body MUST contain the cost-contract
    guard literal pattern (AST-pinned).
"""
from __future__ import annotations

import collections
import enum
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence, Tuple

from backend.core.ouroboros.governance.cost_contract_assertion import (
    COST_GATED_ROUTES,
    CostContractViolation,
)


logger = logging.getLogger(__name__)


AUTO_ACTION_ROUTER_SCHEMA_VERSION: str = "auto_action_router.1"


# Routes considered "higher cost" than BG/SPEC. Mirror of
# confidence_route_advisor — duplicated as plain strings to keep the
# urgency_router import out of this module's authority cone.
_ROUTE_BACKGROUND: str = "background"
_ROUTE_SPECULATIVE: str = "speculative"
_ROUTE_STANDARD: str = "standard"
_ROUTE_COMPLEX: str = "complex"
_ROUTE_IMMEDIATE: str = "immediate"

_HIGHER_COST_ROUTES: frozenset = frozenset({
    _ROUTE_STANDARD, _ROUTE_COMPLEX, _ROUTE_IMMEDIATE,
})


# Risk tiers — mirror the risk-tier ladder from the orchestrator
# without importing it. String comparison via lowercased equality.
_RISK_SAFE_AUTO: str = "safe_auto"
_RISK_NOTIFY_APPLY: str = "notify_apply"
_RISK_APPROVAL_REQUIRED: str = "approval_required"
_RISK_BLOCKED: str = "blocked"

_RISK_LADDER: Tuple[str, ...] = (
    _RISK_SAFE_AUTO,
    _RISK_NOTIFY_APPLY,
    _RISK_APPROVAL_REQUIRED,
    _RISK_BLOCKED,
)


# ---------------------------------------------------------------------------
# Master flags
# ---------------------------------------------------------------------------


def auto_action_router_enabled() -> bool:
    """``JARVIS_AUTO_ACTION_ROUTER_ENABLED`` (default ``true`` —
    GRADUATED in Move 3 Slice 4, 2026-04-30).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy disables.
    Re-read at call time so monkeypatch + live toggle work.

    Cost contract preservation: even with this flag on, the
    router's structural guard (``_propose_action``) raises
    ``CostContractViolation`` on any BG/SPEC → higher-cost
    proposal. §26.6 four-layer defense-in-depth ensures cost
    contract holds regardless of router state.

    Slice 4 graduation rationale: shadow-mode (Slice 3) verified
    the observer + ledger + verdict-buffer wiring is best-effort
    and never propagates failures into the publish path. Master-on
    triggers the dispatcher's decision tree on every terminal
    postmortem, but ``JARVIS_AUTO_ACTION_ENFORCE`` stays locked
    off — the system is producing advisory ledger rows for
    operator review, NOT modifying ctx. The mutation boundary is
    a separate later authorization.

    Hot-revert: ``export JARVIS_AUTO_ACTION_ROUTER_ENABLED=false``
    short-circuits ``propose_advisory_action`` to NO_ACTION
    always."""
    raw = os.environ.get(
        "JARVIS_AUTO_ACTION_ROUTER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Move 3 Slice 4 cadence)
    return raw in ("1", "true", "yes", "on")


def auto_action_enforce() -> bool:
    """``JARVIS_AUTO_ACTION_ENFORCE`` (default ``false`` —
    LOCKED OFF UNTIL OPERATOR REVIEW per Move 3 directive).

    Independent from the master flag. When the master is on but
    enforce is off (the only state we ship in this arc), the
    router emits advisory proposals to the ledger but the
    orchestrator NEVER mutates ``ctx`` based on them. The
    enforce gate is the mutation boundary — it crosses from
    advisory to authoritative.

    Operator binding: this flag stays false until the operator
    has reviewed shadow proposals from a real soak and
    explicitly authorizes graduation to enforce mode. This is
    NOT a Slice 4 graduation; it is a separate later arc.

    Asymmetric env semantics — empty/whitespace = unset =
    default-false; only explicit truthy turns enforce on."""
    raw = os.environ.get(
        "JARVIS_AUTO_ACTION_ENFORCE", "",
    ).strip().lower()
    if raw == "":
        return False  # locked off
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Knobs (FlagRegistry-typed; values clamped defensively)
# ---------------------------------------------------------------------------


_DEFAULT_HISTORY_K: int = 8
_DEFAULT_FAILURE_RATE_TRIP: float = 0.5
_DEFAULT_ESCALATE_VERDICT_TRIP: float = 0.5


def auto_action_history_k() -> int:
    """``JARVIS_AUTO_ACTION_HISTORY_K`` (default 8). Number of recent
    op outcomes / verdicts to consult per family. Floored at 2.

    NEVER raises."""
    raw = os.environ.get("JARVIS_AUTO_ACTION_HISTORY_K", "").strip()
    if not raw:
        return _DEFAULT_HISTORY_K
    try:
        return max(2, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_HISTORY_K


def auto_action_oracle_veto_enabled() -> bool:
    """``JARVIS_AUTO_ACTION_ORACLE_VETO_ENABLED`` (default true).
    Tier 2 #6 follow-up Arc 1: when on, the router consults the
    most-recent Production Oracle observation FIRST in the
    decision precedence; FAILED -> ROUTE_TO_NOTIFY_APPLY (or
    DEMOTE_RISK_TIER for SAFE_AUTO ops); DEGRADED ->
    RAISE_EXPLORATION_FLOOR. When off, oracle observations in
    AutoActionContext are ignored.

    NEVER raises."""
    raw = os.environ.get(
        "JARVIS_AUTO_ACTION_ORACLE_VETO_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated 2026-05-03
    return raw in ("1", "true", "yes", "on")


def auto_action_failure_rate_trip() -> float:
    """``JARVIS_AUTO_ACTION_FAILURE_RATE_TRIP`` (default 0.5).
    Fraction of failed outcomes in the history window required to
    trigger DEFER_OP_FAMILY or DEMOTE_RISK_TIER. Clamped [0.0, 1.0].

    NEVER raises."""
    raw = os.environ.get(
        "JARVIS_AUTO_ACTION_FAILURE_RATE_TRIP", "",
    ).strip()
    if not raw:
        return _DEFAULT_FAILURE_RATE_TRIP
    try:
        v = float(raw)
        if v != v:  # NaN
            return _DEFAULT_FAILURE_RATE_TRIP
        return max(0.0, min(1.0, v))
    except (TypeError, ValueError):
        return _DEFAULT_FAILURE_RATE_TRIP


def auto_action_escalate_verdict_trip() -> float:
    """``JARVIS_AUTO_ACTION_ESCALATE_VERDICT_TRIP`` (default 0.5).
    Fraction of ESCALATE verdicts in the recent confidence
    window required to trigger ROUTE_TO_NOTIFY_APPLY.
    Clamped [0.0, 1.0].

    NEVER raises."""
    raw = os.environ.get(
        "JARVIS_AUTO_ACTION_ESCALATE_VERDICT_TRIP", "",
    ).strip()
    if not raw:
        return _DEFAULT_ESCALATE_VERDICT_TRIP
    try:
        v = float(raw)
        if v != v:
            return _DEFAULT_ESCALATE_VERDICT_TRIP
        return max(0.0, min(1.0, v))
    except (TypeError, ValueError):
        return _DEFAULT_ESCALATE_VERDICT_TRIP


# ---------------------------------------------------------------------------
# Action type — explicit 5-value enum (J.A.R.M.A.T.R.I.X. modeling)
# ---------------------------------------------------------------------------


class AdvisoryActionType(str, enum.Enum):
    """The five-value decision space for the router.

    ``NO_ACTION`` is an EXPLICIT happy-path return value — never
    ``None``, never an implicit fall-through. Every input must map
    to exactly one of these values. Operator directive: explicit
    state modeling is a strict requirement as the architecture
    evolves toward J.A.R.M.A.T.R.I.X.
    """

    NO_ACTION = "no_action"
    DEFER_OP_FAMILY = "defer_op_family"
    DEMOTE_RISK_TIER = "demote_risk_tier"
    ROUTE_TO_NOTIFY_APPLY = "route_to_notify_apply"
    RAISE_EXPLORATION_FLOOR = "raise_exploration_floor"


# ---------------------------------------------------------------------------
# Input shapes — frozen dataclasses for the three signal feeds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecentOpOutcome:
    """One postmortem outcome observation. Frozen — input only."""

    op_id: str
    op_family: str  # e.g. "doc_staleness", "github_issue", "test_failure"
    success: bool
    risk_tier: str
    failure_phase: Optional[str] = None
    failure_reason: Optional[str] = None
    failed_category: Optional[str] = None  # e.g. "read_file", "search_code"
    cost_usd: float = 0.0
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class RecentConfidenceVerdict:
    """One confidence-monitor verdict observation."""

    op_id: str
    verdict: str  # "RETRY" | "ESCALATE" | "INCONCLUSIVE"
    rolling_margin: float = 0.0


@dataclass(frozen=True)
class RecentAdaptationProposal:
    """One Pass C operator-decided adaptation proposal."""

    proposal_id: str
    surface: str  # one of the 5 Pass C surfaces
    operator_outcome: str  # "approved" | "rejected" | "pending"


@dataclass(frozen=True)
class RecentOracleObservation:
    """Most-recent Production Oracle aggregate verdict + provenance.

    Tier 2 #6 follow-up Arc 1 (2026-05-03): the auto_action_router
    consumes external-reality signals via this slot. Populated by
    :func:`gather_context` reading the production_oracle_observer's
    bounded ring buffer; ``None`` when no observation has happened
    yet OR the master flag is off (cold-boot/disabled paths).

    Frozen because the router treats observations as immutable
    snapshots; the observer's own ring buffer holds the live state.

    Fields mirror :class:`production_oracle_observer.OracleObservation`
    projection (no direct import to avoid circular dep at type-level
    -- the bridge in :func:`gather_context` does the conversion).
    """

    aggregate_verdict: str  # OracleVerdict.value
    observed_at_ts: float
    signal_count: int = 0
    adapters_queried: int = 0
    adapters_failed: int = 0
    posture: str = ""


@dataclass(frozen=True)
class AutoActionContext:
    """Full pre-aggregated input to ``propose_advisory_action``.

    Slice 1 takes pre-aggregated input (no signal-source readers
    yet — those land in Slice 2). The current op's identity
    (family + risk + route) lets the router target the proposal
    at the right scope.

    ``recent_oracle_observation`` is OPTIONAL (default None); when
    populated, the router consults it FIRST (production reality
    overrides internal observability). Tier 2 #6 follow-up.
    """

    recent_outcomes: Tuple[RecentOpOutcome, ...] = ()
    recent_verdicts: Tuple[RecentConfidenceVerdict, ...] = ()
    recent_proposals: Tuple[RecentAdaptationProposal, ...] = ()
    current_op_family: str = ""
    current_risk_tier: str = ""
    current_route: str = ""
    posture: str = ""
    recent_oracle_observation: Optional[RecentOracleObservation] = None


# ---------------------------------------------------------------------------
# AdvisoryAction — frozen advisory record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdvisoryAction:
    """An advisory action proposal.

    ALWAYS advisory. Operators review via the ``/auto-action``
    REPL surface (Slice 4). The proposal is NEVER auto-applied
    while ``JARVIS_AUTO_ACTION_ENFORCE=false`` (the only state
    this arc ships in).
    """

    action_type: AdvisoryActionType
    reason_code: str
    evidence: str
    target_op_family: str = ""
    target_category: str = ""  # for RAISE_EXPLORATION_FLOOR
    proposed_risk_tier: str = ""  # for DEMOTE_RISK_TIER / ROUTE_TO_NOTIFY_APPLY
    rolling_failure_rate: float = 0.0
    rolling_escalate_rate: float = 0.0
    history_size: int = 0
    posture: str = ""
    op_id: str = ""
    schema_version: str = AUTO_ACTION_ROUTER_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Cost-contract structural guard
# ---------------------------------------------------------------------------


def _propose_action(
    *,
    action_type: AdvisoryActionType,
    reason_code: str,
    evidence: str,
    current_route: str,
    target_op_family: str = "",
    target_category: str = "",
    proposed_risk_tier: str = "",
    rolling_failure_rate: float = 0.0,
    rolling_escalate_rate: float = 0.0,
    history_size: int = 0,
    posture: str = "",
    op_id: str = "",
) -> AdvisoryAction:
    """Construct an ``AdvisoryAction`` with the structural
    cost-contract guard.

    Cost contract structural pin (PRD §26.6 + Move 3 scope):
    if ``current_route in {"background", "speculative"}``, the
    router MUST NOT propose any action whose downstream effect
    would route the op to a higher-cost provider tier. None of
    the current 5 action types directly carry a route field, so
    this guard is naturally satisfied today — but encoding it
    structurally future-proofs against later additions. Concretely:

      * ``current_route in COST_GATED_ROUTES`` AND
        ``proposed_risk_tier`` reflects an escalation that would
        re-route to a higher-cost path → ``CostContractViolation``.

    The guard is AST-pinned (Slice 1 test
    ``test_cost_contract_guard_ast_pin``)."""
    cur_norm = (current_route or "").strip().lower()
    prop_risk_norm = (proposed_risk_tier or "").strip().lower()
    # Even though no current action type carries a route, the
    # structural guard is encoded against any future field that
    # could bridge a risk-tier upgrade into a route escalation.
    # Specifically: if current_route is BG/SPEC and the proposed
    # risk_tier would push to APPROVAL_REQUIRED+ (which today only
    # IMMEDIATE/STANDARD routes carry), refuse.
    if (
        cur_norm in COST_GATED_ROUTES
        and prop_risk_norm in (
            _RISK_APPROVAL_REQUIRED, _RISK_BLOCKED,
        )
    ):
        raise CostContractViolation(
            op_id=op_id or "",
            provider_route=cur_norm,
            provider_tier="auto_action_router",
            is_read_only=False,
            provider_name="auto_action_router",
            detail=(
                f"structural guard: current_route={cur_norm!r} "
                f"(BG/SPEC) cannot have "
                f"proposed_risk_tier={prop_risk_norm!r} — would "
                f"imply a route escalation that violates §26.6"
            ),
        )
    return AdvisoryAction(
        action_type=action_type,
        reason_code=reason_code,
        evidence=evidence,
        target_op_family=target_op_family,
        target_category=target_category,
        proposed_risk_tier=proposed_risk_tier,
        rolling_failure_rate=rolling_failure_rate,
        rolling_escalate_rate=rolling_escalate_rate,
        history_size=history_size,
        posture=posture,
        op_id=op_id,
    )


# ---------------------------------------------------------------------------
# Decision math — pure functions over the input context
# ---------------------------------------------------------------------------


def _failure_rate_for_family(
    outcomes: Sequence[RecentOpOutcome],
    op_family: str,
) -> Tuple[float, int]:
    """Returns (failure_rate, history_size) for ``op_family``.
    history_size is the number of outcomes that matched the
    family; failure_rate is failed/total in that subset.
    Returns (0.0, 0) when no outcomes match."""
    matched = [o for o in outcomes if o.op_family == op_family]
    if not matched:
        return (0.0, 0)
    failed = sum(1 for o in matched if not o.success)
    return (failed / len(matched), len(matched))


def _escalate_rate(
    verdicts: Sequence[RecentConfidenceVerdict],
) -> Tuple[float, int]:
    """Returns (escalate_rate, history_size) over the given
    verdicts. ESCALATE verdicts are the trigger; RETRY and
    INCONCLUSIVE do not contribute."""
    if not verdicts:
        return (0.0, 0)
    escalates = sum(
        1 for v in verdicts if v.verdict.upper() == "ESCALATE"
    )
    return (escalates / len(verdicts), len(verdicts))


def _most_failed_category(
    outcomes: Sequence[RecentOpOutcome],
) -> Optional[str]:
    """Returns the category that appears most often in the
    ``failed_category`` field of failed outcomes. None when no
    failed outcome has a category set."""
    counts: dict = {}
    for o in outcomes:
        if not o.success and o.failed_category:
            counts[o.failed_category] = counts.get(
                o.failed_category, 0,
            ) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


def propose_advisory_action(
    context: AutoActionContext,
) -> AdvisoryAction:
    """Produce the advisory action proposal for the next op.

    ALWAYS returns an ``AdvisoryAction``. The happy path returns
    ``AdvisoryAction(action_type=AdvisoryActionType.NO_ACTION,
    ...)`` — never ``None``, never raises (except
    ``CostContractViolation`` from the structural guard, which
    is fatal-by-design).

    Decision precedence (first match wins):
      1. Master flag off → NO_ACTION (short-circuit).
      2. Recent ESCALATE verdict rate ≥ trip → ROUTE_TO_NOTIFY_APPLY
      3. Op-family failure rate ≥ trip:
         a. risk_tier == SAFE_AUTO → DEMOTE_RISK_TIER (to NOTIFY_APPLY)
         b. else → DEFER_OP_FAMILY
      4. Failed-category surface available → RAISE_EXPLORATION_FLOOR
      5. Otherwise → NO_ACTION

    Cost contract preservation: ``_propose_action`` raises
    ``CostContractViolation`` on any structural violation.
    """
    if not auto_action_router_enabled():
        return _propose_action(
            action_type=AdvisoryActionType.NO_ACTION,
            reason_code="master_flag_off",
            evidence="JARVIS_AUTO_ACTION_ROUTER_ENABLED is false",
            current_route=context.current_route,
            posture=context.posture,
        )

    history_k = auto_action_history_k()
    failure_trip = auto_action_failure_rate_trip()
    escalate_trip = auto_action_escalate_verdict_trip()

    # Trim signal windows to history_k most recent (caller-supplied
    # tuples may be longer or shorter; we only consult the trailing
    # window).
    recent_verdicts = context.recent_verdicts[-history_k:]
    recent_outcomes = context.recent_outcomes[-history_k:]

    # ── Rule 1.5 (Tier 2 #6 follow-up): Production Oracle veto ──
    # Production reality wins over internal observability. When the
    # most-recent oracle observation is FAILED, the router proposes
    # an action targeted at the current op even if internal signals
    # haven't caught up yet. DEGRADED -> raise exploration floor for
    # the op family. Cost contract is preserved structurally:
    # _propose_action's BG/SPEC guard prevents route escalation.
    obs = context.recent_oracle_observation
    if (
        auto_action_oracle_veto_enabled()
        and obs is not None
    ):
        verdict = (obs.aggregate_verdict or "").strip().lower()
        if verdict == "failed":
            current_risk = (
                context.current_risk_tier or ""
            ).strip().lower()
            evidence = (
                f"Production Oracle aggregate verdict=FAILED "
                f"(signals={obs.signal_count} "
                f"adapters={obs.adapters_queried} "
                f"failed_adapters={obs.adapters_failed} "
                f"posture={obs.posture!r})"
            )
            if current_risk == _RISK_SAFE_AUTO:
                return _propose_action(
                    action_type=AdvisoryActionType.DEMOTE_RISK_TIER,
                    reason_code="production_oracle_failed",
                    evidence=evidence,
                    current_route=context.current_route,
                    target_op_family=context.current_op_family,
                    proposed_risk_tier=_RISK_NOTIFY_APPLY,
                    posture=context.posture,
                )
            return _propose_action(
                action_type=AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY,
                reason_code="production_oracle_failed",
                evidence=evidence,
                current_route=context.current_route,
                target_op_family=context.current_op_family,
                proposed_risk_tier=_RISK_NOTIFY_APPLY,
                posture=context.posture,
            )
        if verdict == "degraded" and context.current_op_family:
            return _propose_action(
                action_type=(
                    AdvisoryActionType.RAISE_EXPLORATION_FLOOR
                ),
                reason_code="production_oracle_degraded",
                evidence=(
                    f"Production Oracle aggregate verdict=DEGRADED "
                    f"(signals={obs.signal_count}); raising "
                    f"exploration floor for op family"
                ),
                current_route=context.current_route,
                target_op_family=context.current_op_family,
                target_category=context.current_op_family,
                posture=context.posture,
            )

    # ── Rule 2: ESCALATE verdict rate → ROUTE_TO_NOTIFY_APPLY ──
    esc_rate, esc_history = _escalate_rate(recent_verdicts)
    if esc_history >= 2 and esc_rate >= escalate_trip:
        return _propose_action(
            action_type=AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY,
            reason_code="recurring_confidence_escalation",
            evidence=(
                f"{int(esc_rate * esc_history)}/{esc_history} recent "
                f"confidence verdicts were ESCALATE "
                f"(trip={escalate_trip:.2f})"
            ),
            current_route=context.current_route,
            target_op_family=context.current_op_family,
            proposed_risk_tier=_RISK_NOTIFY_APPLY,
            rolling_escalate_rate=esc_rate,
            history_size=esc_history,
            posture=context.posture,
        )

    # ── Rule 3: op-family failure rate → DEMOTE / DEFER ──
    if context.current_op_family:
        fail_rate, fail_history = _failure_rate_for_family(
            recent_outcomes, context.current_op_family,
        )
        if fail_history >= 2 and fail_rate >= failure_trip:
            current_risk = (context.current_risk_tier or "").strip().lower()
            if current_risk == _RISK_SAFE_AUTO:
                return _propose_action(
                    action_type=AdvisoryActionType.DEMOTE_RISK_TIER,
                    reason_code="op_family_failure_rate_safe_auto",
                    evidence=(
                        f"family={context.current_op_family!r} failure "
                        f"rate {fail_rate:.2f} ≥ trip {failure_trip:.2f} "
                        f"over {fail_history} ops; recommending "
                        f"NOTIFY_APPLY for human review"
                    ),
                    current_route=context.current_route,
                    target_op_family=context.current_op_family,
                    proposed_risk_tier=_RISK_NOTIFY_APPLY,
                    rolling_failure_rate=fail_rate,
                    history_size=fail_history,
                    posture=context.posture,
                )
            return _propose_action(
                action_type=AdvisoryActionType.DEFER_OP_FAMILY,
                reason_code="op_family_failure_rate",
                evidence=(
                    f"family={context.current_op_family!r} failure "
                    f"rate {fail_rate:.2f} ≥ trip {failure_trip:.2f} "
                    f"over {fail_history} ops"
                ),
                current_route=context.current_route,
                target_op_family=context.current_op_family,
                rolling_failure_rate=fail_rate,
                history_size=fail_history,
                posture=context.posture,
            )

    # ── Rule 4: failed-category surface → RAISE_EXPLORATION_FLOOR ──
    cat = _most_failed_category(recent_outcomes)
    if cat:
        return _propose_action(
            action_type=AdvisoryActionType.RAISE_EXPLORATION_FLOOR,
            reason_code="failed_category_recurring",
            evidence=(
                f"category={cat!r} surfaced in recent failed "
                f"outcomes — Iron Gate floor candidate"
            ),
            current_route=context.current_route,
            target_category=cat,
            history_size=len(recent_outcomes),
            posture=context.posture,
        )

    # ── Rule 5: happy path — explicit NO_ACTION ──
    return _propose_action(
        action_type=AdvisoryActionType.NO_ACTION,
        reason_code="no_signal",
        evidence=(
            f"no trip thresholds crossed (esc_history={esc_history}, "
            f"outcome_history={len(recent_outcomes)})"
        ),
        current_route=context.current_route,
        posture=context.posture,
    )


# ---------------------------------------------------------------------------
# Move 3 Slice 2 — signal source readers
# ---------------------------------------------------------------------------
#
# Three read-only helpers that consume EXISTING ledger surfaces and produce
# the input dataclasses Slice 1 defined. Per operator binding ("do not
# duplicate state-gathering"), every reader either:
#   (a) wraps a public reader on an existing ledger and maps the result
#       into the input dataclass, OR
#   (b) returns an empty tuple with an explicit TODO when no persistent
#       surface exists yet. Slice 3 wires the producer side for case (b)
#       at the orchestrator hook seam.
#
# Best-effort everywhere — readers NEVER raise. A missing ledger / parse
# failure / module-not-importable returns an empty tuple. Defensive
# composition: callers chain these into ``AutoActionContext`` without
# branch-on-error.


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        if v != v:  # NaN
            return default
        return v
    except (TypeError, ValueError):
        return default


def recent_postmortem_outcomes(
    *,
    limit: Optional[int] = None,
    session_id: Optional[str] = None,
) -> Tuple[RecentOpOutcome, ...]:
    """Read the most recent postmortems and project them into
    ``RecentOpOutcome`` records.

    Wraps ``verification.postmortem.list_recent_postmortems`` —
    no duplicated state-gathering. Maps each ``VerificationPostmortem``:

      * ``op_id``, ``elapsed_s = completed_unix - started_unix``
      * ``success = (total_failed == 0 AND not has_blocking_failures
        AND error_count == 0)``
      * ``failure_phase`` — derived from kind: ``"VERIFY"`` for
        ``verification_postmortem``, ``"TERMINAL"`` for
        ``terminal_postmortem``, empty when success.
      * ``op_family``, ``risk_tier``, ``failed_category`` left empty —
        these are populated at the orchestrator hook seam in Slice 3
        from ``ctx.task_complexity`` / ``ctx.risk_tier`` /
        ``ctx.failed_exploration_category``. The reader returns the
        bare projection; the caller enriches.

    Newest-last (matches ``list_recent_postmortems`` ordering).

    Parameters
    ----------
    limit:
        Number of records to fetch. Defaults to
        ``auto_action_history_k()`` so the reader is in lockstep with
        the dispatcher's window.
    session_id:
        Optional session override; defaults to the ambient session
        per the postmortem ledger's path resolution.

    NEVER raises."""
    safe_limit = (
        _safe_int(limit, default=auto_action_history_k())
        if limit is not None
        else auto_action_history_k()
    )
    if safe_limit <= 0:
        return ()
    try:
        from backend.core.ouroboros.governance.verification.postmortem import (
            list_recent_postmortems,
        )
    except Exception:  # noqa: BLE001
        return ()
    try:
        records = list_recent_postmortems(
            limit=safe_limit, session_id=session_id,
        )
    except Exception:  # noqa: BLE001
        return ()

    outcomes: list = []
    for pm in records:
        try:
            failed = (
                _safe_int(getattr(pm, "must_hold_failed", 0))
                + _safe_int(getattr(pm, "should_hold_failed", 0))
                + _safe_int(getattr(pm, "ideal_failed", 0))
            )
            err = _safe_int(getattr(pm, "error_count", 0))
            blocking = bool(getattr(pm, "has_blocking_failures", False))
            success = (failed == 0) and (err == 0) and (not blocking)
            failure_phase: Optional[str] = None
            if not success:
                # Best-effort: distinguish VERIFY-phase failures from
                # TERMINAL postmortems. We don't have direct access to
                # the kind field on the dataclass, so default to
                # "VERIFY" when there are claim failures and "TERMINAL"
                # when only error_count is non-zero.
                failure_phase = (
                    "VERIFY" if failed > 0 or blocking
                    else "TERMINAL"
                )
            started = _safe_float(getattr(pm, "started_unix", 0.0))
            completed = _safe_float(getattr(pm, "completed_unix", 0.0))
            elapsed = max(0.0, completed - started)
            outcomes.append(
                RecentOpOutcome(
                    op_id=str(getattr(pm, "op_id", "") or ""),
                    op_family="",  # populated by Slice 3 hook
                    success=success,
                    risk_tier="",  # populated by Slice 3 hook
                    failure_phase=failure_phase,
                    failure_reason=None,
                    failed_category=None,  # populated by Slice 3 hook
                    cost_usd=0.0,
                    elapsed_s=elapsed,
                )
            )
        except Exception:  # noqa: BLE001 — defensive: skip malformed
            continue
    return tuple(outcomes)


def recent_confidence_verdicts(
    *,
    limit: Optional[int] = None,
) -> Tuple[RecentConfidenceVerdict, ...]:
    """Read recent confidence-monitor verdicts.

    Slice 3 (2026-04-30): wired against the process-local
    ``_VerdictRingBuffer`` populated by
    ``record_confidence_verdict`` at the confidence_observability
    publish seam. No persistent ledger — verdicts are
    inherently per-session signals (next session boots with a
    fresh window). The buffer is bounded at
    ``_VERDICT_BUFFER_MAXLEN`` (32 by default; env-tunable for
    future expansion) so memory is O(1) regardless of session
    length.

    The dispatcher's decision tree handles
    ``len(recent_verdicts) == 0`` cleanly — no signal → no action.

    NEVER raises.
    """
    safe_limit = (
        _safe_int(limit, default=auto_action_history_k())
        if limit is not None
        else auto_action_history_k()
    )
    return _verdict_buffer.snapshot(limit=safe_limit)


def recent_adaptation_proposals(
    *,
    limit: Optional[int] = None,
) -> Tuple[RecentAdaptationProposal, ...]:
    """Read the most recent Pass C adaptation proposals + their
    operator decisions.

    Wraps ``adaptation.ledger.get_default_ledger().history(limit=N)``
    — no duplicated state-gathering. Maps each ``AdaptationProposal``:

      * ``proposal_id`` — direct
      * ``surface`` — the AdaptationSurface enum value (string)
      * ``operator_outcome`` — derived from the proposal's
        ``operator_decision``: ``"approved"`` /  ``"rejected"`` /
        ``"pending"``.

    Newest-first per ``ledger.history`` ordering.

    NEVER raises."""
    safe_limit = (
        _safe_int(limit, default=auto_action_history_k())
        if limit is not None
        else auto_action_history_k()
    )
    if safe_limit <= 0:
        return ()
    try:
        from backend.core.ouroboros.governance.adaptation.ledger import (
            get_default_ledger,
        )
    except Exception:  # noqa: BLE001
        return ()
    try:
        ledger = get_default_ledger()
        records = ledger.history(limit=safe_limit)
    except Exception:  # noqa: BLE001
        return ()

    out: list = []
    for proposal in records:
        try:
            surface = getattr(proposal, "surface", None)
            surface_str = (
                surface.value if hasattr(surface, "value")
                else str(surface or "")
            )
            decision = getattr(proposal, "operator_decision", None)
            decision_str = (
                decision.value if hasattr(decision, "value")
                else str(decision or "pending")
            ).strip().lower()
            # Normalize to the 3-value vocabulary documented on
            # RecentAdaptationProposal.operator_outcome.
            if decision_str in ("approved", "applied"):
                outcome = "approved"
            elif decision_str in ("rejected", "denied"):
                outcome = "rejected"
            else:
                outcome = "pending"
            out.append(
                RecentAdaptationProposal(
                    proposal_id=str(
                        getattr(proposal, "proposal_id", "") or "",
                    ),
                    surface=surface_str,
                    operator_outcome=outcome,
                )
            )
        except Exception:  # noqa: BLE001 — defensive: skip malformed
            continue
    return tuple(out)


def _read_recent_oracle_observation() -> Optional[RecentOracleObservation]:
    """Read the most-recent OracleObservation from the production
    oracle observer's ring buffer + project into the router's frozen
    shape. Returns ``None`` when the observer hasn't ticked yet OR
    the master flag is off OR any error occurs (NEVER raises)."""
    try:
        from backend.core.ouroboros.governance.production_oracle_observer import (  # noqa: E501
            get_default_observer,
            production_oracle_enabled,
        )
        if not production_oracle_enabled():
            return None
        obs = get_default_observer()
        current = obs.current()
        if current is None:
            return None
        return RecentOracleObservation(
            aggregate_verdict=current.aggregate_verdict.value,
            observed_at_ts=current.observed_at_ts,
            signal_count=len(current.signals),
            adapters_queried=current.adapters_queried,
            adapters_failed=current.adapters_failed,
            posture=current.posture,
        )
    except Exception:  # noqa: BLE001 -- defensive
        return None


def gather_context(
    *,
    current_op_family: str = "",
    current_risk_tier: str = "",
    current_route: str = "",
    posture: str = "",
    session_id: Optional[str] = None,
    include_oracle: bool = True,
) -> AutoActionContext:
    """One-shot helper that runs all three readers + assembles an
    ``AutoActionContext``.

    Convenience for Slice 3's orchestrator-hook caller: instead of
    invoking each reader independently and zipping the result, the
    hook can call ``gather_context(...)`` once. The ``current_*``
    fields ride the ctx and parameterize the dispatcher's targeting
    decisions (op family failure rate, risk-tier-specific demote vs
    defer, etc.).

    Tier 2 #6 follow-up Arc 1 (2026-05-03): when ``include_oracle``
    is True (default) AND the oracle veto is enabled, the helper
    also reads the most-recent OracleObservation from the production
    oracle observer's ring buffer and populates
    ``recent_oracle_observation`` on the context.

    NEVER raises — each reader independently swallows failure."""
    return AutoActionContext(
        recent_outcomes=recent_postmortem_outcomes(
            session_id=session_id,
        ),
        recent_verdicts=recent_confidence_verdicts(),
        recent_proposals=recent_adaptation_proposals(),
        current_op_family=current_op_family,
        current_risk_tier=current_risk_tier,
        current_route=current_route,
        posture=posture,
        recent_oracle_observation=(
            _read_recent_oracle_observation() if include_oracle else None
        ),
    )


# ---------------------------------------------------------------------------
# Move 3 Slice 3 — confidence-verdict ring buffer
# ---------------------------------------------------------------------------


def _verdict_buffer_maxlen() -> int:
    """``JARVIS_AUTO_ACTION_VERDICT_BUFFER_MAXLEN`` (default 32,
    floor 8). Bounded ring buffer for in-flight confidence verdicts
    so memory stays O(1) regardless of session length.

    NEVER raises."""
    raw = os.environ.get(
        "JARVIS_AUTO_ACTION_VERDICT_BUFFER_MAXLEN", "",
    ).strip()
    if not raw:
        return 32
    try:
        return max(8, int(raw))
    except (TypeError, ValueError):
        return 32


class _VerdictRingBuffer:
    """Thread-safe bounded ring buffer for confidence-monitor
    verdicts.

    Producer: ``record_confidence_verdict`` (called by
    ``confidence_observability.publish_*_event``).
    Consumer: ``recent_confidence_verdicts`` (called by
    ``gather_context``).

    Bounded by ``_verdict_buffer_maxlen()`` — drop-oldest semantics
    via ``collections.deque(maxlen=...)``. Snapshot returns a tuple
    of frozen ``RecentConfidenceVerdict`` so consumers can iterate
    without holding the lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._deque: collections.deque = collections.deque(
            maxlen=_verdict_buffer_maxlen(),
        )

    def append(self, verdict: RecentConfidenceVerdict) -> None:
        """Append a verdict. Drop-oldest when at maxlen.

        NEVER raises — input validation done at the call site."""
        if not isinstance(verdict, RecentConfidenceVerdict):
            return
        with self._lock:
            self._deque.append(verdict)

    def snapshot(
        self, *, limit: Optional[int] = None,
    ) -> Tuple[RecentConfidenceVerdict, ...]:
        """Return up to ``limit`` most-recent verdicts (newest-last).

        Always returns a fresh tuple — caller can iterate without
        any lock concern."""
        with self._lock:
            items = tuple(self._deque)
        if limit is None or limit <= 0 or limit >= len(items):
            return items
        return items[-limit:]

    def clear(self) -> None:
        """Drop all buffered verdicts. Test hook + boot reset."""
        with self._lock:
            self._deque.clear()

    def reset_maxlen(self) -> None:
        """Re-read the env knob and rebuild the deque with the new
        maxlen. Drops any in-flight items. Test hook for env-knob
        changes."""
        with self._lock:
            self._deque = collections.deque(
                maxlen=_verdict_buffer_maxlen(),
            )


# Module-level singleton — single producer/consumer flow.
_verdict_buffer: _VerdictRingBuffer = _VerdictRingBuffer()


def record_confidence_verdict(
    *,
    op_id: str,
    verdict: str,
    rolling_margin: float = 0.0,
) -> None:
    """Producer-side hook for the verdict ring buffer.

    Called by ``confidence_observability.publish_*_event`` after
    each verdict publish. Always best-effort — input validation +
    swallow-failure semantics so a misbehaving caller cannot
    derail the publish path.

    Verdict strings expected (case-insensitive):
      * ``ok`` / ``approaching_floor`` / ``below_floor`` —
        ``ConfidenceVerdict`` enum values
      * Or the higher-level dispatcher verdicts:
        ``RETRY`` / ``ESCALATE`` / ``INCONCLUSIVE``

    The router's decision tree (``_escalate_rate``) accepts the
    upper-case dispatcher form; lower-case enum values are
    promoted via mapping below.
    """
    if not isinstance(op_id, str) or not op_id:
        return
    raw = (verdict or "").strip()
    if not raw:
        return
    upper = raw.upper()
    # Map ConfidenceVerdict enum values to dispatcher verdicts.
    # below_floor is the strongest signal -> ESCALATE.
    # approaching_floor is the early warning -> RETRY.
    # ok is the happy path -> RETRY (no escalation, but a verdict
    # was emitted, so it's still in the rolling window).
    if upper == "BELOW_FLOOR":
        upper = "ESCALATE"
    elif upper == "APPROACHING_FLOOR":
        upper = "RETRY"
    elif upper == "OK":
        upper = "RETRY"
    margin = _safe_float(rolling_margin, default=0.0)
    _verdict_buffer.append(
        RecentConfidenceVerdict(
            op_id=op_id,
            verdict=upper,
            rolling_margin=margin,
        )
    )


# ---------------------------------------------------------------------------
# Move 3 Slice 3 — advisory proposal ledger
# ---------------------------------------------------------------------------


_LEDGER_FILENAME = "auto_action_proposals.jsonl"


def _ledger_path() -> Path:
    """Resolve the on-disk path for the advisory proposal ledger.

    Default ``.jarvis/auto_action_proposals.jsonl`` under the
    repo root. Env override
    ``JARVIS_AUTO_ACTION_LEDGER_PATH`` for tests + custom layouts.
    """
    explicit = os.environ.get(
        "JARVIS_AUTO_ACTION_LEDGER_PATH", "",
    ).strip()
    if explicit:
        return Path(explicit)
    repo_root = Path(os.environ.get("JARVIS_REPO_PATH", ".")).resolve()
    return repo_root / ".jarvis" / _LEDGER_FILENAME


def _action_to_jsonl_record(action: AdvisoryAction) -> str:
    """Serialize an AdvisoryAction to a single JSONL row.

    Schema captures the full advisory record + timestamps for
    correlation with the operator review surface (Slice 4
    ``/auto-action`` REPL command). NEVER raises — fields are
    coerced to strings/numbers."""
    record = {
        "schema_version": action.schema_version,
        "recorded_at_unix": time.time(),
        "action_type": action.action_type.value,
        "reason_code": str(action.reason_code or ""),
        "evidence": str(action.evidence or ""),
        "target_op_family": str(action.target_op_family or ""),
        "target_category": str(action.target_category or ""),
        "proposed_risk_tier": str(action.proposed_risk_tier or ""),
        "rolling_failure_rate": _safe_float(
            action.rolling_failure_rate, default=0.0,
        ),
        "rolling_escalate_rate": _safe_float(
            action.rolling_escalate_rate, default=0.0,
        ),
        "history_size": _safe_int(action.history_size, default=0),
        "posture": str(action.posture or ""),
        "op_id": str(action.op_id or ""),
    }
    return json.dumps(record, separators=(",", ":"))


class AutoActionProposalLedger:
    """Append-only JSONL ledger for advisory action proposals.

    Mirror of ``adaptation.ledger.AdaptationLedger`` (simpler — no
    state-machine, no monotonic-tightening verdict, just append +
    read_recent for the operator review surface). Threading-safe
    via process-local lock; cross-process atomicity comes from
    line-oriented append semantics on POSIX append-mode writes.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path if path is not None else _ledger_path()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, action: AdvisoryAction) -> bool:
        """Append a proposal as a JSONL row. Returns True on
        success, False on any failure (parent missing, disk full,
        etc.) — NEVER raises. NO_ACTION proposals are skipped to
        keep the ledger focused on operator-relevant signal.

        Tier 1 #3 — uses cross-process flock helper so multiple
        processes (e.g., concurrent battle-test runs) cannot
        interleave partial writes."""
        if action.action_type is AdvisoryActionType.NO_ACTION:
            return False
        line = _action_to_jsonl_record(action)
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
                flock_append_line,
            )
            return flock_append_line(self._path, line)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[auto_action_router] ledger append failed at %s: %s",
                self._path, exc,
            )
            return False

    def read_recent(
        self, limit: int = 100,
    ) -> Tuple[dict, ...]:
        """Read the last ``limit`` proposals as raw dict records,
        newest-last. Used by Slice 4's operator surfaces.
        NEVER raises."""
        safe_limit = max(1, int(limit))
        if not self._path.exists():
            return ()
        try:
            with self._lock:
                with self._path.open("r", encoding="utf-8") as fh:
                    rows = []
                    for raw_line in fh:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            rows.append(json.loads(raw_line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            return ()
        if len(rows) > safe_limit:
            rows = rows[-safe_limit:]
        return tuple(rows)


# Module-level singleton with lazy path resolution.
_ledger_singleton: Optional[AutoActionProposalLedger] = None
_ledger_singleton_lock = threading.Lock()


def get_default_ledger() -> AutoActionProposalLedger:
    """Process-wide singleton. Path is resolved lazily so env
    overrides at test time take effect without module reload."""
    global _ledger_singleton
    if _ledger_singleton is None:
        with _ledger_singleton_lock:
            if _ledger_singleton is None:
                _ledger_singleton = AutoActionProposalLedger()
    return _ledger_singleton


def reset_default_ledger_for_tests() -> None:
    """Drop the singleton so a fresh path is resolved on next
    access. Test isolation hook."""
    global _ledger_singleton
    with _ledger_singleton_lock:
        _ledger_singleton = None


# ---------------------------------------------------------------------------
# Move 3 Slice 3 — Post-postmortem observer (mirrors OpsDigestObserver)
# ---------------------------------------------------------------------------


class PostPostmortemObserver(Protocol):
    """Stable contract for the post-postmortem hook.

    Called by ``postmortem_observability`` after a terminal
    postmortem record persists. The observer is the integration
    point where the auto-action router runs its decision tree
    against the freshly-updated signal surfaces.

    All methods are best-effort fire-and-forget. Implementations
    MUST NOT raise; callers wrap in try/except defensively."""

    def on_terminal_postmortem_persisted(
        self,
        *,
        op_id: str,
        terminal_phase: str,
        has_blocking_failures: bool,
    ) -> None:
        """A terminal postmortem record has been persisted to the
        JSONL ledger. The observer can now read recent ledger state
        and emit advisory output without racing the producer."""


class _NoopPostPostmortemObserver:
    """Default observer — silently drops every call. Used when no
    auto-action router has registered (cold boot, master flag off,
    test isolation)."""

    def on_terminal_postmortem_persisted(
        self,
        *,
        op_id: str,
        terminal_phase: str,
        has_blocking_failures: bool,
    ) -> None:
        return


_POSTMORTEM_OBSERVER_LOCK = threading.Lock()
_POSTMORTEM_OBSERVER: PostPostmortemObserver = _NoopPostPostmortemObserver()


def register_post_postmortem_observer(
    observer: Optional[PostPostmortemObserver],
) -> None:
    """Install (or clear) the process-global post-postmortem
    observer. Passing None restores the default no-op observer."""
    global _POSTMORTEM_OBSERVER
    with _POSTMORTEM_OBSERVER_LOCK:
        _POSTMORTEM_OBSERVER = (
            observer if observer is not None
            else _NoopPostPostmortemObserver()
        )


def get_post_postmortem_observer() -> PostPostmortemObserver:
    """Return the currently-registered observer (never None)."""
    with _POSTMORTEM_OBSERVER_LOCK:
        return _POSTMORTEM_OBSERVER


def reset_post_postmortem_observer() -> None:
    """Restore the default no-op observer. Test hook."""
    register_post_postmortem_observer(None)


# ---------------------------------------------------------------------------
# Move 3 Slice 3 — Concrete observer that runs the dispatcher
# ---------------------------------------------------------------------------


@dataclass
class _CtxEnrichment:
    """Per-op context the observer needs to enrich the dispatcher
    input. Provided by the orchestrator at hook-registration time
    via ``register_op_context``. Sliced 3 keeps this deliberately
    minimal — Slice 4 may extend it as the dispatcher's decision
    tree grows."""

    op_family: str = ""
    risk_tier: str = ""
    route: str = ""
    posture: str = ""
    failed_category: str = ""


class AutoActionShadowObserver:
    """Concrete post-postmortem observer for shadow-mode operation.

    On each terminal-postmortem-persisted event:
      1. Pulls the freshly-updated signal surfaces via
         ``gather_context`` (no duplicated state-gathering).
      2. Enriches the ``current_*`` fields from
         ``_pending_ctx_enrichments[op_id]`` if registered by the
         orchestrator hook (otherwise empty).
      3. Runs ``propose_advisory_action`` against the master flag.
      4. Persists the resulting advisory action to the ledger
         (NO_ACTION skipped).

    ENFORCE flag is NEVER consulted here — the observer is
    advisory-only by construction. The mutation boundary lives at
    the orchestrator hook seam (Slice 4+), not in the observer.
    """

    def __init__(
        self,
        *,
        ledger: Optional[AutoActionProposalLedger] = None,
        ctx_lookup: Optional[Callable[[str], _CtxEnrichment]] = None,
    ) -> None:
        self._ledger = ledger if ledger is not None else get_default_ledger()
        self._ctx_lookup = ctx_lookup or (lambda _op_id: _CtxEnrichment())

    def on_terminal_postmortem_persisted(
        self,
        *,
        op_id: str,
        terminal_phase: str,
        has_blocking_failures: bool,
    ) -> None:
        """Observer entry point. NEVER raises."""
        if not auto_action_router_enabled():
            return
        try:
            enrichment = self._ctx_lookup(op_id) or _CtxEnrichment()
            ctx = gather_context(
                current_op_family=enrichment.op_family,
                current_risk_tier=enrichment.risk_tier,
                current_route=enrichment.route,
                posture=enrichment.posture,
            )
            action = propose_advisory_action(ctx)
            # Stamp the op_id onto the proposal post-hoc so the
            # ledger row carries the trigger correlation.
            from dataclasses import replace
            stamped = replace(action, op_id=op_id)
            appended = self._ledger.append(stamped)
            # Slice 4 — SSE event for actionable proposals.
            # ledger.append already filters NO_ACTION; we publish
            # only when a row was actually written so operator
            # consumers see a 1:1 relationship between SSE events
            # and ledger rows.
            if appended:
                publish_auto_action_proposal_emitted(stamped)
        except CostContractViolation:
            # Cost-contract violation is fatal-by-design: re-raise.
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[auto_action_router] shadow observer swallowed "
                "exception for op_id=%s: %s", op_id, exc,
            )


# ---------------------------------------------------------------------------
# Move 3 Slice 3 — Per-op ctx enrichment registry
# ---------------------------------------------------------------------------
#
# Orchestrator hook seam: at op-start the orchestrator registers the
# op's identity (family, risk_tier, route, posture); at op-terminal the
# postmortem observer looks it up. Bounded LRU prevents unbounded
# growth across long sessions.

_CTX_ENRICHMENT_LOCK = threading.Lock()
_CTX_ENRICHMENTS: "collections.OrderedDict[str, _CtxEnrichment]" = (
    collections.OrderedDict()
)
_CTX_ENRICHMENT_MAXLEN = 256


def register_op_context(
    op_id: str,
    *,
    op_family: str = "",
    risk_tier: str = "",
    route: str = "",
    posture: str = "",
    failed_category: str = "",
) -> None:
    """Orchestrator hook — register ctx fields the observer needs.

    Called at op-start (or whenever any of these fields are
    finalized) so the post-postmortem observer can enrich the
    dispatcher input from this registry rather than reaching into
    the orchestrator's internal state. Bounded LRU; older entries
    drop on overflow."""
    if not isinstance(op_id, str) or not op_id:
        return
    with _CTX_ENRICHMENT_LOCK:
        _CTX_ENRICHMENTS[op_id] = _CtxEnrichment(
            op_family=str(op_family or ""),
            risk_tier=str(risk_tier or ""),
            route=str(route or ""),
            posture=str(posture or ""),
            failed_category=str(failed_category or ""),
        )
        # LRU eviction.
        while len(_CTX_ENRICHMENTS) > _CTX_ENRICHMENT_MAXLEN:
            _CTX_ENRICHMENTS.popitem(last=False)


def lookup_op_context(op_id: str) -> _CtxEnrichment:
    """Observer-side lookup. Returns empty enrichment when no
    registration exists for ``op_id`` — the dispatcher's decision
    tree handles empty fields cleanly."""
    if not isinstance(op_id, str) or not op_id:
        return _CtxEnrichment()
    with _CTX_ENRICHMENT_LOCK:
        return _CTX_ENRICHMENTS.get(op_id, _CtxEnrichment())


def clear_op_context_registry() -> None:
    """Drop all registrations. Boot reset + test isolation hook."""
    with _CTX_ENRICHMENT_LOCK:
        _CTX_ENRICHMENTS.clear()


# ---------------------------------------------------------------------------
# Move 3 Slice 3 — Boot wiring helper
# ---------------------------------------------------------------------------


def install_shadow_observer(
    *,
    ctx_lookup: Optional[Callable[[str], _CtxEnrichment]] = None,
) -> None:
    """Idempotent install of the shadow-mode observer.

    Called by ``governed_loop_service.start()`` after the harness
    boots. When the master flag is off, this is still a no-op at
    runtime (the observer's first action is a master-flag check),
    so it is safe to call regardless. The default ``ctx_lookup``
    falls through to the in-process registry."""
    register_post_postmortem_observer(
        AutoActionShadowObserver(
            ctx_lookup=ctx_lookup or lookup_op_context,
        )
    )


# ---------------------------------------------------------------------------
# Move 3 Slice 4 — SSE event + observability routes + REPL surface
# ---------------------------------------------------------------------------


EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED: str = "auto_action_proposal_emitted"


def publish_auto_action_proposal_emitted(action: AdvisoryAction) -> Optional[str]:
    """Fire the ``auto_action_proposal_emitted`` SSE event after the
    shadow observer persists an actionable proposal.

    Best-effort — broker-missing / publish-error / observability-disabled
    all return None silently. NEVER raises.

    Operator surfaces consume this via the IDE stream; the
    ``GET /observability/auto-action`` GET endpoints serve the
    ledger directly for backfill/replay scenarios."""
    if action.action_type is AdvisoryActionType.NO_ACTION:
        return None  # NO_ACTION never warrants a publish
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            get_default_broker,
        )
    except Exception:  # noqa: BLE001
        return None
    try:
        broker = get_default_broker()
        return broker.publish(
            event_type=EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED,
            op_id=str(action.op_id or ""),
            payload={
                "schema_version": action.schema_version,
                "wall_ts": time.time(),
                "op_id": str(action.op_id or ""),
                "action_type": action.action_type.value,
                "reason_code": str(action.reason_code or ""),
                "evidence": str(action.evidence or "")[:200],
                "target_op_family": str(action.target_op_family or ""),
                "target_category": str(action.target_category or ""),
                "proposed_risk_tier": str(action.proposed_risk_tier or ""),
                "rolling_failure_rate": _safe_float(
                    action.rolling_failure_rate, default=0.0,
                ),
                "rolling_escalate_rate": _safe_float(
                    action.rolling_escalate_rate, default=0.0,
                ),
                "history_size": _safe_int(action.history_size, default=0),
                "posture": str(action.posture or ""),
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[auto_action_router] SSE publish swallowed exception",
            exc_info=True,
        )
        return None


def proposal_stats(rows: Sequence[dict]) -> dict:
    """Aggregate ledger rows into a stats summary.

    Used by the ``/auto-action stats`` REPL subcommand and the
    ``GET /observability/auto-action/stats`` endpoint. Pure function
    over the dict shape produced by ``AutoActionProposalLedger.read_recent``.

    Returns a dict with:
      * ``total`` — total row count
      * ``by_action_type`` — dict keyed by action_type value, count
      * ``by_op_family`` — dict keyed by target_op_family (skips empty)
      * ``by_category`` — dict keyed by target_category (skips empty)

    NEVER raises."""
    from collections import Counter
    by_type: Counter = Counter()
    by_family: Counter = Counter()
    by_category: Counter = Counter()
    total = 0
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        total += 1
        atype = str(row.get("action_type", ""))
        if atype:
            by_type[atype] += 1
        fam = str(row.get("target_op_family", ""))
        if fam:
            by_family[fam] += 1
        cat = str(row.get("target_category", ""))
        if cat:
            by_category[cat] += 1
    return {
        "total": total,
        "by_action_type": dict(by_type),
        "by_op_family": dict(by_family),
        "by_category": dict(by_category),
    }


# ---------------------------------------------------------------------------
# Move 3 Slice 4 — observability route handler
# ---------------------------------------------------------------------------


_OBSERVABILITY_DEFAULT_LIMIT: int = 100
_OBSERVABILITY_MAX_LIMIT: int = 500


def _json_response(payload: dict, *, status: int = 200) -> Any:
    """Build a Cache-Control: no-store JSON aiohttp Response.

    Lazy import of aiohttp.web — keeps the auto_action_router module
    importable in environments that don't have aiohttp installed
    (CI tests without the web stack)."""
    from aiohttp import web
    return web.json_response(
        payload,
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def _parse_limit(query: Any) -> int:
    """Parse + clamp the ``limit`` query param. Defaults to 100,
    capped at 500."""
    raw = (query or {}).get("limit", "")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _OBSERVABILITY_DEFAULT_LIMIT
    return max(1, min(_OBSERVABILITY_MAX_LIMIT, v))


class _AutoActionRoutesHandler:
    """aiohttp route handler for the ``/observability/auto-action``
    family. Mirror of ``_PostmortemRoutesHandler`` shape."""

    def __init__(
        self,
        *,
        ledger: Optional[AutoActionProposalLedger] = None,
        rate_limit_check: Optional[Callable[[Any], bool]] = None,
        cors_headers: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._ledger = ledger or get_default_ledger()
        self._rate_limit_check = rate_limit_check
        self._cors_headers = cors_headers

    def _gate(self, request: Any) -> Optional[Any]:
        """Run the master-flag + rate-limit gate. Returns a
        Response when the request should be rejected, None when
        the handler should proceed."""
        if not auto_action_router_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": AUTO_ACTION_ROUTER_SCHEMA_VERSION,
                },
                status=503,
            )
        if self._rate_limit_check is not None:
            try:
                if not self._rate_limit_check(request):
                    return _json_response(
                        {"error": "rate_limited"},
                        status=429,
                    )
            except Exception:  # noqa: BLE001
                pass  # rate-limit failure is non-fatal
        return None

    async def handle_recent(self, request: Any) -> Any:
        """``GET /observability/auto-action`` — most recent N
        advisory proposals. Default 100, capped at 500."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(getattr(request, "query", {}))
        try:
            rows = self._ledger.read_recent(limit=limit)
        except Exception:  # noqa: BLE001
            rows = ()
        return _json_response(
            {
                "schema_version": AUTO_ACTION_ROUTER_SCHEMA_VERSION,
                "limit": limit,
                "count": len(rows),
                "rows": list(rows),
            }
        )

    async def handle_stats(self, request: Any) -> Any:
        """``GET /observability/auto-action/stats`` — aggregate
        counts (by_action_type, by_op_family, by_category) over the
        most recent N rows."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(getattr(request, "query", {}))
        try:
            rows = self._ledger.read_recent(limit=limit)
            stats = proposal_stats(rows)
        except Exception:  # noqa: BLE001
            stats = proposal_stats(())
        stats["schema_version"] = AUTO_ACTION_ROUTER_SCHEMA_VERSION
        stats["limit"] = limit
        return _json_response(stats)


def register_auto_action_routes(
    app: Any,
    *,
    ledger: Optional[AutoActionProposalLedger] = None,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Mount the auto-action GET routes on a caller-supplied
    aiohttp Application. Mirrors ``register_postmortem_routes``.

    Routes:
      * ``GET /observability/auto-action``         — recent proposals
      * ``GET /observability/auto-action/stats``   — aggregate stats

    Master flag check is done in the handler (per-request),
    so the route mounting itself is safe to call regardless of
    flag state (allows live toggle without re-mounting)."""
    handler = _AutoActionRoutesHandler(
        ledger=ledger,
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/auto-action", handler.handle_recent,
    )
    app.router.add_get(
        "/observability/auto-action/stats", handler.handle_stats,
    )


__all__ = [
    "AUTO_ACTION_ROUTER_SCHEMA_VERSION",
    "AdvisoryAction",
    "AdvisoryActionType",
    "AutoActionContext",
    "AutoActionProposalLedger",
    "AutoActionShadowObserver",
    "EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED",
    "PostPostmortemObserver",
    "RecentAdaptationProposal",
    "RecentConfidenceVerdict",
    "RecentOpOutcome",
    "auto_action_enforce",
    "auto_action_escalate_verdict_trip",
    "auto_action_failure_rate_trip",
    "auto_action_history_k",
    "auto_action_router_enabled",
    "clear_op_context_registry",
    "gather_context",
    "get_default_ledger",
    "get_post_postmortem_observer",
    "install_shadow_observer",
    "lookup_op_context",
    "proposal_stats",
    "propose_advisory_action",
    "publish_auto_action_proposal_emitted",
    "recent_adaptation_proposals",
    "recent_confidence_verdicts",
    "recent_postmortem_outcomes",
    "record_confidence_verdict",
    "register_auto_action_routes",
    "register_op_context",
    "register_post_postmortem_observer",
    "reset_default_ledger_for_tests",
    "reset_post_postmortem_observer",
]
