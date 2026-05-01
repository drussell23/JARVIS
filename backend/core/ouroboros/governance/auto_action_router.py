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

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence, Tuple

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
    """``JARVIS_AUTO_ACTION_ROUTER_ENABLED`` (default ``false`` in
    Slice 1; graduated to ``true`` in Slice 4 after shadow soak).

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit truthy enables; explicit falsy disables.
    Re-read at call time so monkeypatch + live toggle work.

    Cost contract preservation: even with this flag on, the
    router's structural guard (``_propose_action``) raises
    ``CostContractViolation`` on any BG/SPEC → higher-cost
    proposal. §26.6 four-layer defense-in-depth ensures cost
    contract holds regardless of router state.

    Hot-revert: ``export JARVIS_AUTO_ACTION_ROUTER_ENABLED=false``
    short-circuits ``propose_advisory_action`` to NO_ACTION
    always."""
    raw = os.environ.get(
        "JARVIS_AUTO_ACTION_ROUTER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # Slice 1 default — graduated true in Slice 4
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
class AutoActionContext:
    """Full pre-aggregated input to ``propose_advisory_action``.

    Slice 1 takes pre-aggregated input (no signal-source readers
    yet — those land in Slice 2). The current op's identity
    (family + risk + route) lets the router target the proposal
    at the right scope.
    """

    recent_outcomes: Tuple[RecentOpOutcome, ...] = ()
    recent_verdicts: Tuple[RecentConfidenceVerdict, ...] = ()
    recent_proposals: Tuple[RecentAdaptationProposal, ...] = ()
    current_op_family: str = ""
    current_risk_tier: str = ""
    current_route: str = ""
    posture: str = ""


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

    Slice 2 stub: the confidence-monitor publishes verdict events
    via ``verification.confidence_observability`` to the SSE broker
    but there is currently no persistent ledger of verdicts to read
    after the fact. Slice 3 wires a process-local ring buffer at the
    confidence_monitor's publish seam so downstream consumers can
    poll without re-running the monitor.

    Until Slice 3 ships the ring buffer, this reader returns an
    empty tuple. The dispatcher's decision tree handles
    ``len(recent_verdicts) == 0`` cleanly — no signal → no action.

    NEVER raises.
    """
    # Intentional empty-tuple stub — see docstring.
    _ = limit  # parameter retained for API stability across slices
    return ()


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


def gather_context(
    *,
    current_op_family: str = "",
    current_risk_tier: str = "",
    current_route: str = "",
    posture: str = "",
    session_id: Optional[str] = None,
) -> AutoActionContext:
    """One-shot helper that runs all three readers + assembles an
    ``AutoActionContext``.

    Convenience for Slice 3's orchestrator-hook caller: instead of
    invoking each reader independently and zipping the result, the
    hook can call ``gather_context(...)`` once. The ``current_*``
    fields ride the ctx and parameterize the dispatcher's targeting
    decisions (op family failure rate, risk-tier-specific demote vs
    defer, etc.).

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
    )


__all__ = [
    "AUTO_ACTION_ROUTER_SCHEMA_VERSION",
    "AdvisoryAction",
    "AdvisoryActionType",
    "AutoActionContext",
    "RecentAdaptationProposal",
    "RecentConfidenceVerdict",
    "RecentOpOutcome",
    "auto_action_enforce",
    "auto_action_escalate_verdict_trip",
    "auto_action_failure_rate_trip",
    "auto_action_history_k",
    "auto_action_router_enabled",
    "gather_context",
    "propose_advisory_action",
    "recent_adaptation_proposals",
    "recent_confidence_verdicts",
    "recent_postmortem_outcomes",
]
