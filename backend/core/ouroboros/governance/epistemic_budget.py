"""Upgrade 1 Slice 1 — Bounded Epistemic Loop primitive (PRD §31.2).

Per-op information budget enforced at every Venom tool round.
Closes both **infinite curiosity** (probe loops that won't
terminate) AND **silent fail-poor-quality** (op completes with
low confidence without escalating) in a single architectural move.

This Slice 1 ships the **full contract layer** for the entire
5-slice arc — every field Slice 2's tracker will populate +
every outcome branch Slice 3's tool_executor hook will route
must already be named here. No "we'll add it later" scope
creep; tracker + tool_executor integrate against this contract,
they don't redefine it.

Authoritative state shape (`EpistemicBudget` frozen dataclass):

  * ``op_id`` / ``route`` / ``risk_tier`` — keying + cost-gate
    + escalation-target inputs
  * ``rounds_consumed`` / ``max_rounds`` — Venom tool-round counter
    + cap (env-driven)
  * ``confidence_trajectory`` — explicit nested
    :class:`ConfidenceTrajectory` structure (NOT a free-form
    list). Slice 2's ``ConfidenceMonitor`` subscriber populates
    this; Slice 3's tool_executor hook reads
    ``trajectory.dropped_in_window`` to route PROBE_TRIGGERED.
  * ``probe_calls_consumed`` / ``probe_call_cap`` — counter +
    cap. Cap **defers to** :func:`hypothesis_probe.get_max_calls_-
    per_probe` (env: ``JARVIS_HYPOTHESIS_PROBE_MAX_CALLS``,
    default 5). Upgrade 1 NEVER duplicates this cap — AST
    invariant pinned in Slice 5.
  * ``branch_calls_consumed`` / ``sbt_branch_cap`` — counter
    + cap (env: ``JARVIS_EPISTEMIC_SBT_BRANCH_CAP``, default 3)
  * ``confidence_drop_threshold`` — drop magnitude that triggers
    PROBE (env: ``JARVIS_EPISTEMIC_CONFIDENCE_DROP_THRESHOLD``,
    default 0.25)
  * ``last_probe_verdict`` / ``last_sbt_verdict`` — most recent
    outcomes from ConfidenceProbeRunner / SpeculativeBranchTree
    (string-typed for cross-arc compat with their respective
    closed enums; Slice 2 normalizes via the closed-enum value
    fields)
  * ``created_at_unix`` / ``last_updated_at_unix`` — for TTL
    orphan cleanup (Slice 2's tracker dict TTL pattern,
    Decision A1)

Authoritative outcome shape (:class:`BudgetOutcome` 7-value
closed enum) — covers every branch Slice 3 routes:

  * ``WITHIN_BUDGET`` — continue normally (no-op for tool_executor)
  * ``CONVERGED`` — confidence stable + last_probe_verdict
    CONFIRMED/REFUTED; round-loop may exit cleanly
  * ``PROBE_TRIGGERED`` — confidence dropped > threshold; auto-
    engage ConfidenceProbeRunner (Decision B1: synchronous
    ``await``-block before next round)
  * ``SBT_TRIGGERED`` — probe was DIVERGENT + risk_tier ≥
    NOTIFY_APPLY (cost gate); spawn SpeculativeBranchTree
  * ``EXHAUSTED_NOTIFY_APPLY`` — rounds_consumed >= max AND not
    converged AND risk_tier < NOTIFY_APPLY; escalate via
    ``risk_tier_floor.apply_floor_to_name`` (Decision C1)
  * ``EXHAUSTED_APPROVAL_REQUIRED`` — already at NOTIFY_APPLY+
    AND still exhausted; route through OrangePRReviewer
  * ``DISABLED`` — master-off sentinel; never persisted, never
    triggers action

Authoritative decision function (:func:`compute_budget_action`)
— pure; same inputs → same output; never raises. Decision tree
implements the full routing matrix Slice 3 will use unchanged.

Cost-gated routes:

  * ``BACKGROUND`` / ``SPECULATIVE`` (per
    :data:`cost_contract_assertion.COST_GATED_ROUTES`) refuse
    PROBE + SBT structurally — :func:`compute_budget_action`
    cannot return PROBE_TRIGGERED / SBT_TRIGGERED for these
    routes regardless of trajectory state. Mirrors Move 6's
    ``COST_GATED_ROUTES`` AST pin discipline.

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib + ``cost_contract_assertion`` (for the
    ``COST_GATED_ROUTES`` symbol) + ``hypothesis_probe`` (for
    the ``MAX_CALLS_PER_PROBE_DEFAULT`` constant + env reader,
    no-duplication contract) ONLY.
  * NEVER imports orchestrator / tool_executor /
    candidate_generator / iron_gate / providers / urgency_router
    / strategic_direction. Slice 3 reverses the
    ``tool_executor → epistemic_budget`` direction (lazy import
    inside the round loop); never the reverse.
  * Pure data + pure decision function — never mutates external
    state, never raises out of any public function.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

# Cost-gate symbol from the canonical source — no duplication.
from backend.core.ouroboros.governance.cost_contract_assertion import (
    BG_ROUTE,
    COST_GATED_ROUTES,
    SPEC_ROUTE,
)

# Probe-call cap defers to HypothesisProbe's existing env reader.
# Upgrade 1 NEVER duplicates this cap. AST invariant in Slice 5
# pins that ``epistemic_budget.py`` references
# ``MAX_CALLS_PER_PROBE_DEFAULT`` (or ``get_max_calls_per_probe``)
# from hypothesis_probe rather than re-defining the constant.
from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (  # noqa: E501
    MAX_CALLS_PER_PROBE_DEFAULT,
    get_max_calls_per_probe,
)

logger = logging.getLogger(__name__)


EPISTEMIC_BUDGET_SCHEMA_VERSION: str = "epistemic_budget.1"


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics, default-FALSE for Slice 1
# ---------------------------------------------------------------------------


def epistemic_budget_enabled() -> bool:
    """``JARVIS_EPISTEMIC_BUDGET_ENABLED`` (default ``false``
    until Slice 5 graduation per PRD §31.2).

    Asymmetric env semantics — empty/whitespace = unset = current
    default (false for Slice 1); explicit ``1``/``true``/``yes``/
    ``on`` flips on. Same shape as
    :func:`failure_mode_memory_enabled` /
    :func:`action_outcome_memory_enabled` /
    :func:`coherence_auditor_enabled` / :func:`cigw_enabled` /
    :func:`quorum_enabled` graduated flags so the Slice 5
    graduation flip is a one-character edit.

    Re-read on every call so flips hot-revert without restart."""
    raw = os.environ.get(
        "JARVIS_EPISTEMIC_BUDGET_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # Slice 1 default; flips to True at Slice 5
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-knob accessors — bounded clamping, defaults documented
# ---------------------------------------------------------------------------


def _read_int_knob(
    name: str, default: int, floor: int, ceiling: int,
) -> int:
    """Bounded integer env-knob read. NEVER raises."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
        if n < floor:
            return floor
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


def _read_float_knob(
    name: str, default: float, floor: float, ceiling: float,
) -> float:
    """Bounded float env-knob read. NEVER raises."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        if v < floor:
            return floor
        if v > ceiling:
            return ceiling
        return v
    except (TypeError, ValueError):
        return default


def epistemic_max_rounds() -> int:
    """``JARVIS_EPISTEMIC_MAX_ROUNDS`` — default 12 (PRD §31.2.2).
    Worst-case probe + SBT activations multiplied by max_rounds
    yield the ≤20 LLM calls/op cost-cap envelope. Clamped
    [1, 100]."""
    return _read_int_knob(
        "JARVIS_EPISTEMIC_MAX_ROUNDS", 12, 1, 100,
    )


def epistemic_confidence_drop_threshold() -> float:
    """``JARVIS_EPISTEMIC_CONFIDENCE_DROP_THRESHOLD`` — default
    0.25 (PRD §31.2.2). Drop magnitude (peak − latest in window)
    that triggers PROBE_TRIGGERED. Clamped [0.0, 1.0]."""
    return _read_float_knob(
        "JARVIS_EPISTEMIC_CONFIDENCE_DROP_THRESHOLD",
        0.25, 0.0, 1.0,
    )


def epistemic_sbt_branch_cap() -> int:
    """``JARVIS_EPISTEMIC_SBT_BRANCH_CAP`` — default 3 (PRD
    §31.2.2). Maximum SBT branch invocations per op. Clamped
    [1, 10]."""
    return _read_int_knob(
        "JARVIS_EPISTEMIC_SBT_BRANCH_CAP", 3, 1, 10,
    )


def epistemic_tracker_ttl_s() -> int:
    """``JARVIS_EPISTEMIC_TRACKER_TTL_S`` — orphan-cleanup TTL
    for the per-op tracker dict (Decision A1 from scope, Slice 2
    consumer). Default 3600 (1h). Clamped [60, 86400]."""
    return _read_int_knob(
        "JARVIS_EPISTEMIC_TRACKER_TTL_S", 3600, 60, 86400,
    )


# ---------------------------------------------------------------------------
# ConfidenceSample — primitive frozen ring-buffer entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceSample:
    """One confidence reading observed at a specific tool round.
    Frozen for safe propagation across async boundaries.

    Slice 2's tracker observes via :class:`ConfidenceMonitor`
    callbacks and appends one ``ConfidenceSample`` per round to
    :class:`ConfidenceTrajectory.samples`."""

    confidence: float
    """Monitor reading in [0, 1]. Defensively clamped at
    ingest time by Slice 2; values outside [0, 1] are
    permitted in storage but the trajectory math floors
    negatives to 0.0 and ceilings >1.0 to 1.0."""

    at_round_index: int
    """Venom tool round index when this reading was observed.
    Mirrors :data:`tool_executor.RoundContext.round_index`."""

    at_unix: float
    """Unix timestamp at ingest. Used by Slice 2 for staleness
    detection + (optionally) recency-decay weighting via
    :func:`_scoring_primitives.recency_weight`."""


# ---------------------------------------------------------------------------
# ConfidenceTrajectory — explicit nested structure
# ---------------------------------------------------------------------------


# Bounded ring-buffer cap — keeps the trajectory tail-bounded
# even on long-running ops with many tool rounds. Slice 2's
# tracker truncates to this cap on each ConfidenceMonitor update.
_TRAJECTORY_MAX_SAMPLES: int = 32


@dataclass(frozen=True)
class ConfidenceTrajectory:
    """Bounded recency-ordered confidence-reading history.
    Frozen + slot-explicit (NOT a free-form list).

    Slice 1 ships the SHAPE; Slice 2 populates via the
    :class:`ConfidenceMonitor` subscription. Slice 3's
    :func:`compute_budget_action` consumes ``dropped_in_window``
    + ``latest`` for routing. The shape is the contract; tracker
    + tool_executor never invent fields here."""

    samples: Tuple[ConfidenceSample, ...] = field(
        default_factory=tuple,
    )
    """Bounded ring (capped at :data:`_TRAJECTORY_MAX_SAMPLES`,
    32) of recent readings, oldest-first. Empty on cold-boot."""

    latest: float = 0.0
    """Most recent confidence reading (or 0.0 when no samples).
    Cached for O(1) read by :func:`compute_budget_action`."""

    peak: float = 0.0
    """Highest reading observed across the bounded window.
    Cached for O(1) drop-magnitude computation."""

    nadir: float = 0.0
    """Lowest reading observed across the bounded window."""

    dropped_in_window: bool = False
    """``peak - latest >= confidence_drop_threshold`` flag.
    Slice 1 stores; Slice 2 computes on each
    :class:`ConfidenceMonitor` update + freezes a new
    trajectory. Slice 3's
    :func:`compute_budget_action` reads this for
    PROBE_TRIGGERED routing."""

    @classmethod
    def empty(cls) -> "ConfidenceTrajectory":
        """Cold-boot factory — used when a tracker is created
        for an op that hasn't yet emitted a confidence reading.
        Returns the canonical zero-state."""
        return cls()


# ---------------------------------------------------------------------------
# BudgetOutcome — 7-value closed enum (PRD §31.2.2 Slice 1 spec)
# ---------------------------------------------------------------------------


class BudgetOutcome(str, enum.Enum):
    """Closed routing taxonomy for :func:`compute_budget_action`.
    Slice 3's tool_executor integration branches on the enum,
    never on free-form fields. Adding a new outcome requires a
    PRD update + tool_executor branch — intentional friction."""

    WITHIN_BUDGET = "within_budget"
    """Continue normally — no probe, no SBT, no escalation,
    no exit. Most rounds are WITHIN_BUDGET."""

    CONVERGED = "converged"
    """Confidence stable + last probe CONFIRMED/REFUTED;
    round-loop may exit cleanly. Slice 3 may treat this as
    a clean-exit signal."""

    PROBE_TRIGGERED = "probe_triggered"
    """Confidence dropped > threshold AND probe budget
    available (probe_calls_consumed < probe_call_cap) AND
    route is not cost-gated. Slice 3 invokes
    ``ConfidenceProbeRunner`` synchronously (Decision B1)."""

    SBT_TRIGGERED = "sbt_triggered"
    """Last probe DIVERGENT (or equivalent multi-attempt
    signal) AND SBT budget available AND risk_tier ≥
    NOTIFY_APPLY (structural cost gate) AND route is not
    cost-gated. Slice 3 spawns SBT."""

    EXHAUSTED_NOTIFY_APPLY = "exhausted_notify_apply"
    """rounds_consumed ≥ max_rounds AND not converged AND
    current risk_tier < NOTIFY_APPLY. Slice 3 calls
    :func:`risk_tier_floor.apply_floor_to_name(ctx.risk_tier,
    'notify_apply')` (Decision C1 — single source of truth
    for tier escalation)."""

    EXHAUSTED_APPROVAL_REQUIRED = "exhausted_approval_required"
    """rounds_consumed ≥ max_rounds AND not converged AND
    current risk_tier ≥ NOTIFY_APPLY. Slice 3 routes through
    :class:`OrangePRReviewer` async approval queue."""

    DISABLED = "disabled"
    """Master flag off. Slice 3 treats as no-op (legacy
    pre-Upgrade-1 behavior). Records with outcome=DISABLED
    are never persisted by Slice 4 observability."""


# ---------------------------------------------------------------------------
# EpistemicBudget — authoritative state shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EpistemicBudget:
    """Per-op information budget. Frozen — Slice 2's tracker
    swaps frozen instances atomically rather than mutating in
    place (matches the established immutability discipline of
    :class:`FailureModeRecord` + :class:`ActionOutcomeRecord`).

    Every field Slice 2 will populate + Slice 3 will read is
    NAMED here. Tracker + tool_executor implement against this
    contract; they NEVER add fields they wished existed."""

    op_id: str
    """Originating op_id — keying dimension for Slice 2's
    per-op-tracker dict (Decision A1)."""

    route: str
    """Operation route (immediate / standard / complex /
    background / speculative). Used for cost-gate routing —
    background/speculative refuse PROBE/SBT structurally per
    :data:`COST_GATED_ROUTES`."""

    risk_tier: str
    """Current risk tier (safe_auto / notify_apply /
    approval_required / blocked). Input to:
    (a) SBT cost-gate (SBT only at notify_apply+),
    (b) escalation-target selection
    (EXHAUSTED_NOTIFY_APPLY vs EXHAUSTED_APPROVAL_REQUIRED).
    Tracker reads, never mutates — escalation goes through
    :func:`risk_tier_floor.apply_floor_to_name` only."""

    rounds_consumed: int = 0
    """Counter incremented per Venom tool round. Slice 2's
    tracker increments at the round-boundary callback Slice 3
    wires into ``tool_executor``."""

    max_rounds: int = field(
        default_factory=epistemic_max_rounds,
    )
    """Cap from :func:`epistemic_max_rounds` (env, default 12).
    Captured at op-start for stability — env changes mid-op
    don't shift the cap underneath an active tracker."""

    confidence_trajectory: ConfidenceTrajectory = field(
        default_factory=ConfidenceTrajectory.empty,
    )
    """Bounded recency-ordered confidence history. Slice 2
    swaps a new frozen trajectory on each
    :class:`ConfidenceMonitor` update."""

    probe_calls_consumed: int = 0
    """Counter — incremented on each ConfidenceProbeRunner
    invocation."""

    probe_call_cap: int = field(
        default_factory=get_max_calls_per_probe,
    )
    """Cap **deferred to** :func:`hypothesis_probe.get_max_-
    calls_per_probe` (env: ``JARVIS_HYPOTHESIS_PROBE_MAX_CALLS``,
    default :data:`MAX_CALLS_PER_PROBE_DEFAULT` = 5).
    Upgrade 1 NEVER duplicates this cap — AST invariant pinned
    in Slice 5. Captured at op-start for stability."""

    branch_calls_consumed: int = 0
    """Counter — incremented on each SBT branch invocation."""

    sbt_branch_cap: int = field(
        default_factory=epistemic_sbt_branch_cap,
    )
    """Cap from :func:`epistemic_sbt_branch_cap` (env, default
    3). Captured at op-start for stability."""

    confidence_drop_threshold: float = field(
        default_factory=epistemic_confidence_drop_threshold,
    )
    """Drop magnitude (peak − latest) that triggers
    PROBE_TRIGGERED. Default 0.25 (env). Captured at op-start
    for stability."""

    last_probe_verdict: Optional[str] = None
    """Most recent :class:`ProbeVerdict` value (string) — None
    when no probe has run yet. Slice 2 normalizes via the closed
    enum's ``.value`` attribute. Drives CONVERGED detection
    (CONFIRMED/REFUTED → CONVERGED) and SBT trigger
    (DIVERGENT-equivalent → SBT_TRIGGERED)."""

    last_sbt_verdict: Optional[str] = None
    """Most recent SBT verdict (string). Surface for
    observability + future routing extensions."""

    created_at_unix: float = 0.0
    """Tracker creation timestamp. Used by Slice 2 for TTL
    orphan cleanup (Decision A1)."""

    last_updated_at_unix: float = 0.0
    """Last mutation timestamp. Used by Slice 2 for staleness
    detection."""

    schema_version: str = field(
        default=EPISTEMIC_BUDGET_SCHEMA_VERSION,
    )

    # ---- Pure helpers consumed by compute_budget_action -----------

    def is_route_cost_gated(self) -> bool:
        """True iff :attr:`route` is in
        :data:`COST_GATED_ROUTES`. Cost-gated routes refuse
        PROBE + SBT structurally."""
        return (self.route or "").strip().lower() in (
            COST_GATED_ROUTES
        )

    def has_probe_budget(self) -> bool:
        """True iff probe_calls_consumed < probe_call_cap."""
        return int(self.probe_calls_consumed) < int(
            self.probe_call_cap,
        )

    def has_sbt_budget(self) -> bool:
        """True iff branch_calls_consumed < sbt_branch_cap."""
        return int(self.branch_calls_consumed) < int(
            self.sbt_branch_cap,
        )

    def is_rounds_exhausted(self) -> bool:
        """True iff rounds_consumed >= max_rounds."""
        return int(self.rounds_consumed) >= int(self.max_rounds)

    def is_at_or_above_notify_apply(self) -> bool:
        """True iff current risk_tier is NOTIFY_APPLY or above
        (APPROVAL_REQUIRED, BLOCKED). Used for SBT cost-gate +
        EXHAUSTED escalation-target selection. Comparison is
        case-insensitive and tolerant of unknown tier strings
        (returns False — fail-safe to "below threshold")."""
        tier = (self.risk_tier or "").strip().lower()
        return tier in (
            "notify_apply", "approval_required", "blocked",
        )


# ---------------------------------------------------------------------------
# BudgetAction — authoritative result shape
# ---------------------------------------------------------------------------


# Probe verdict values that correspond to the "converged" state
# for round-loop exit. Imported from hypothesis_probe at module
# load via the value strings; comparing strings (not enum members)
# keeps the surface stable if hypothesis_probe extends its enum.
_CONVERGED_PROBE_VERDICTS: frozenset = frozenset(
    {"confirmed", "refuted"},
)
# Probe verdicts that signal "ambiguity remains" — drives SBT
# trigger when SBT budget is available + risk tier is high enough.
_SBT_TRIGGER_PROBE_VERDICTS: frozenset = frozenset(
    {
        "inconclusive_diminishing",
        "inconclusive_budget",
        "inconclusive_timeout",
    },
)


@dataclass(frozen=True)
class BudgetAction:
    """Result of :func:`compute_budget_action`. Frozen for safe
    propagation. Slice 3's tool_executor branches on
    :attr:`outcome`; :attr:`reason` is operator-readable
    explainability for Slice 4 observability surfaces; the
    optional ``*_invocation_kw`` dicts let Slice 3 pass kwargs
    to :class:`ConfidenceProbeRunner` / SBT spawn without
    reaching back into the tracker for them.

    NEVER mutates :class:`EpistemicBudget` — the budget is
    input-only; Slice 2's tracker is the single mutation point
    (it swaps frozen instances atomically based on
    :attr:`outcome`)."""

    outcome: BudgetOutcome
    """Closed-enum routing decision."""

    reason: str
    """Operator-readable explanation. Surfaced in Slice 4's
    ``/budget`` REPL + ``GET /observability/budget/op/{id}`` +
    ``budget_action_taken`` SSE event payload."""

    escalation_target_tier: Optional[str] = None
    """Present on EXHAUSTED_NOTIFY_APPLY ("notify_apply") +
    EXHAUSTED_APPROVAL_REQUIRED ("approval_required"). Slice 3
    passes this to
    :func:`risk_tier_floor.apply_floor_to_name(ctx.risk_tier,
    target_tier)`. None for non-escalation outcomes."""

    probe_invocation_kw: Optional[Dict[str, Any]] = None
    """Present on PROBE_TRIGGERED. Empty dict by default;
    Slice 3 may extend with ConfidenceProbeRunner-specific
    kwargs without re-running compute_budget_action. None for
    other outcomes."""

    sbt_invocation_kw: Optional[Dict[str, Any]] = None
    """Present on SBT_TRIGGERED. Empty dict by default. None
    for other outcomes."""

    schema_version: str = field(
        default=EPISTEMIC_BUDGET_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly projection for Slice 4 observability +
        Slice 5 SSE event payload."""
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "escalation_target_tier": (
                self.escalation_target_tier
            ),
            "probe_invocation_kw": (
                dict(self.probe_invocation_kw)
                if self.probe_invocation_kw is not None else None
            ),
            "sbt_invocation_kw": (
                dict(self.sbt_invocation_kw)
                if self.sbt_invocation_kw is not None else None
            ),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Decision function — pure, total, never raises
# ---------------------------------------------------------------------------


def compute_budget_action(
    state: EpistemicBudget,
    *,
    enabled_override: Optional[bool] = None,
) -> BudgetAction:
    """Authoritative routing decision for the current
    :class:`EpistemicBudget`. Pure: same inputs → same output.

    Decision tree (order is load-bearing — exhaustion precedes
    triggers; cost-gate precedes everything):

      1. Master flag check (``enabled_override`` OR
         :func:`epistemic_budget_enabled`) → DISABLED.
      2. Garbage / non-EpistemicBudget input → DISABLED + reason.
      3. Cost-gated route (BG / SPECULATIVE) restricts the
         outcome surface — PROBE_TRIGGERED + SBT_TRIGGERED are
         structurally impossible. Only WITHIN_BUDGET / CONVERGED
         / EXHAUSTED_* paths can fire.
      4. Rounds exhausted (rounds_consumed >= max_rounds) AND
         not converged → EXHAUSTED_APPROVAL_REQUIRED if
         already at notify_apply+, else
         EXHAUSTED_NOTIFY_APPLY.
      5. Last probe converged (CONFIRMED / REFUTED) → CONVERGED.
      6. Confidence dropped in window AND probe budget
         available AND route not cost-gated → PROBE_TRIGGERED.
      7. Last probe inconclusive (DIVERGENT-equivalent) AND SBT
         budget available AND risk_tier ≥ notify_apply AND
         route not cost-gated → SBT_TRIGGERED.
      8. Else → WITHIN_BUDGET.

    NEVER raises. All faults map to DISABLED + reason."""
    try:
        # 1. Master flag
        if enabled_override is False:
            return BudgetAction(
                outcome=BudgetOutcome.DISABLED,
                reason="master_flag_off_via_override",
            )
        if enabled_override is None:
            if not epistemic_budget_enabled():
                return BudgetAction(
                    outcome=BudgetOutcome.DISABLED,
                    reason=(
                        "master_flag_off "
                        "(JARVIS_EPISTEMIC_BUDGET_ENABLED)"
                    ),
                )

        # 2. Type check
        if not isinstance(state, EpistemicBudget):
            return BudgetAction(
                outcome=BudgetOutcome.DISABLED,
                reason="invalid_state_type",
            )

        cost_gated = state.is_route_cost_gated()
        verdict_lower = (
            (state.last_probe_verdict or "").strip().lower()
        )

        # 4. Rounds exhausted (precedes trigger checks — once
        # the cap is hit, escalate; do not start new probes).
        if state.is_rounds_exhausted():
            if verdict_lower in _CONVERGED_PROBE_VERDICTS:
                # Edge: rounds exhausted exactly when the most
                # recent probe converged. Treat as CONVERGED so
                # the round-loop exits cleanly without a phantom
                # escalation.
                return BudgetAction(
                    outcome=BudgetOutcome.CONVERGED,
                    reason=(
                        f"rounds_exhausted_at_{state.rounds_consumed}"
                        f"_with_converged_probe"
                    ),
                )
            if state.is_at_or_above_notify_apply():
                return BudgetAction(
                    outcome=(
                        BudgetOutcome.EXHAUSTED_APPROVAL_REQUIRED
                    ),
                    reason=(
                        f"rounds_exhausted_at_"
                        f"{state.rounds_consumed}_already_at_"
                        f"notify_apply"
                    ),
                    escalation_target_tier="approval_required",
                )
            return BudgetAction(
                outcome=BudgetOutcome.EXHAUSTED_NOTIFY_APPLY,
                reason=(
                    f"rounds_exhausted_at_"
                    f"{state.rounds_consumed}_below_"
                    f"notify_apply"
                ),
                escalation_target_tier="notify_apply",
            )

        # 5. Converged (probe verdict CONFIRMED/REFUTED) — clean
        # exit before checking new triggers.
        if verdict_lower in _CONVERGED_PROBE_VERDICTS:
            return BudgetAction(
                outcome=BudgetOutcome.CONVERGED,
                reason=(
                    f"probe_verdict_{verdict_lower}_at_"
                    f"round_{state.rounds_consumed}"
                ),
            )

        # 3 + 6. Confidence drop → PROBE_TRIGGERED. Cost-gated
        # routes refuse the trigger structurally.
        traj = state.confidence_trajectory
        if (
            traj is not None
            and getattr(traj, "dropped_in_window", False)
            and state.has_probe_budget()
            and not cost_gated
        ):
            return BudgetAction(
                outcome=BudgetOutcome.PROBE_TRIGGERED,
                reason=(
                    f"confidence_drop_threshold_exceeded_"
                    f"peak_{traj.peak:.2f}_latest_"
                    f"{traj.latest:.2f}"
                ),
                probe_invocation_kw={},
            )

        # 7. SBT trigger — probe inconclusive AND budget AND
        # risk gate AND not cost-gated.
        if (
            verdict_lower in _SBT_TRIGGER_PROBE_VERDICTS
            and state.has_sbt_budget()
            and state.is_at_or_above_notify_apply()
            and not cost_gated
        ):
            return BudgetAction(
                outcome=BudgetOutcome.SBT_TRIGGERED,
                reason=(
                    f"probe_verdict_{verdict_lower}_at_"
                    f"round_{state.rounds_consumed}_with_sbt_"
                    f"budget_remaining"
                ),
                sbt_invocation_kw={},
            )

        # 8. Default — within budget, continue.
        return BudgetAction(
            outcome=BudgetOutcome.WITHIN_BUDGET,
            reason=(
                f"round_{state.rounds_consumed}_of_"
                f"{state.max_rounds}_no_trigger"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[epistemic_budget] compute_budget_action raised: "
            "%s", exc,
        )
        return BudgetAction(
            outcome=BudgetOutcome.DISABLED,
            reason=f"compute_failed: {type(exc).__name__}",
        )


# ===========================================================================
# Slice 2 — EpistemicBudgetTracker (per-op state machine)
#
# PRD §31.2.2 Slice 2: "EpistemicBudgetTracker (one instance per
# op; threadsafe). Subscribes to ConfidenceMonitor.update() +
# tool-round boundaries from tool_executor."
#
# Decision A1 (per-op_id dict + TTL orphan cleanup +
# _INPROCESS_LOCK parity): per-op state lives in a process-global
# dict keyed by op_id; threadsafe via threading.RLock; orphan
# cleanup via :func:`reap_orphans` (called periodically by Slice
# 3's tool_executor or operator REPL).
#
# Decision X (passive consumer, not ConfidenceMonitor subscriber):
# tracker is a pure state machine driven by EXPLICIT events
# pushed by Slice 3's tool_executor:
#
#   * ``open(op_id, route, risk_tier)`` — at op start
#   * ``note_round_complete(op_id, *, confidence)`` — after each
#     Venom tool round; tool_executor reads ConfidenceMonitor
#     and passes the value
#   * ``note_probe_completed(op_id, *, verdict)`` — after a
#     ConfidenceProbeRunner await returns
#   * ``note_sbt_completed(op_id, *, verdict)`` — after an SBT
#     spawn returns
#   * ``next_action(op_id)`` — read-only; consults
#     :func:`compute_budget_action` against the frozen budget
#   * ``close(op_id)`` — at op end (clean removal)
#
# Why passive (Decision X): tool_executor is the single
# orchestrator for the round loop. Hidden ConfidenceMonitor
# subscription would couple two lifecycles (tracker GC + monitor
# observers), introduce ordering bugs, and obscure the data flow.
# Pushing the confidence value as a parameter keeps the data flow
# explicit + testable + replay-safe.
#
# Mutation discipline: every state transition swaps a NEW frozen
# :class:`EpistemicBudget` atomically into the dict — never mutate
# in place. Mirrors :class:`FailureModeRecord` /
# :class:`ActionOutcomeRecord` immutability convention.
# ===========================================================================


import threading
import time as _time

# Process-global tracker registry — one default instance, plus
# test-only reset hook.
_DEFAULT_TRACKER: Optional["EpistemicBudgetTracker"] = None
_DEFAULT_TRACKER_LOCK = threading.Lock()


def _build_trajectory_after_sample(
    old: ConfidenceTrajectory,
    new_sample: ConfidenceSample,
    drop_threshold: float,
) -> ConfidenceTrajectory:
    """Pure: compute the next :class:`ConfidenceTrajectory`
    given the current trajectory + a new confidence sample +
    the drop threshold.

    Math:
      * Append sample; truncate ring at
        :data:`_TRAJECTORY_MAX_SAMPLES` (oldest dropped).
      * ``peak`` = max(confidence) over bounded window.
      * ``nadir`` = min(confidence) over bounded window.
      * ``latest`` = new sample's confidence.
      * ``dropped_in_window`` = (peak - latest) >= drop_threshold.

    Edge cases:
      * Cold-boot (empty old samples) — first sample sets peak +
        nadir + latest to its own confidence; dropped is False.
      * drop_threshold <= 0 — degenerate; dropped is True
        whenever peak > latest by ANY amount (Slice 2 enforces
        clamp at env layer; this function is robust regardless).

    NEVER raises."""
    try:
        new_samples = tuple(
            list(old.samples) + [new_sample],
        )[-_TRAJECTORY_MAX_SAMPLES:]
        if not new_samples:
            return ConfidenceTrajectory.empty()
        confidences = tuple(s.confidence for s in new_samples)
        peak = max(confidences)
        nadir = min(confidences)
        latest = new_sample.confidence
        dropped = (peak - latest) >= float(drop_threshold or 0.0)
        return ConfidenceTrajectory(
            samples=new_samples,
            latest=latest,
            peak=peak,
            nadir=nadir,
            dropped_in_window=bool(dropped),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[epistemic_budget] _build_trajectory_after_sample "
            "raised: %s", exc,
        )
        return old  # fail-safe: return unchanged trajectory


def _normalize_verdict_value(verdict: Any) -> Optional[str]:
    """Coerce a probe / SBT verdict (enum member, string, or
    object with ``.value`` attribute) into a lowercase string.
    Returns None on garbage. NEVER raises."""
    if verdict is None:
        return None
    try:
        if hasattr(verdict, "value"):
            v = getattr(verdict, "value")
        else:
            v = verdict
        return str(v).strip().lower() or None
    except Exception:  # noqa: BLE001 — defensive
        return None


def _now_or(now_ts: Optional[float]) -> float:
    """Return ``now_ts`` if provided (synthetic-time tests), else
    wall clock."""
    return float(now_ts) if now_ts is not None else _time.time()


class EpistemicBudgetTracker:
    """Per-op information-budget tracker. Thread-safe via
    :class:`threading.RLock`; per-op state stored as frozen
    :class:`EpistemicBudget` instances in a process-global dict
    keyed by ``op_id``.

    Mutation pattern: every state transition swaps a NEW frozen
    instance atomically. The dict is the only mutable surface;
    the budgets are immutable.

    Decision A1 (lifecycle): per-op trackers; TTL orphan cleanup
    via :meth:`reap_orphans`.

    Decision X (consumer model): passive state machine driven by
    explicit events from Slice 3's tool_executor. NO direct
    ConfidenceMonitor subscription.

    NEVER raises out of any public method — all faults map to
    no-op + debug log."""

    def __init__(self) -> None:
        self._budgets: Dict[str, EpistemicBudget] = {}
        self._lock = threading.RLock()

    # ---- Lifecycle ----------------------------------------------

    def open(
        self,
        *,
        op_id: str,
        route: str,
        risk_tier: str,
        now_ts: Optional[float] = None,
    ) -> Optional[EpistemicBudget]:
        """Open a tracker for ``op_id``. Idempotent — reopening
        returns the existing budget unchanged (does NOT reset
        rounds/probe/SBT counters; if the operator wants a reset,
        they should :meth:`close` first).

        Returns the current (existing-or-new) frozen budget, or
        None on garbage input."""
        try:
            if not op_id or not isinstance(op_id, str):
                return None
            now = _now_or(now_ts)
            with self._lock:
                existing = self._budgets.get(op_id)
                if existing is not None:
                    return existing
                budget = EpistemicBudget(
                    op_id=str(op_id),
                    route=str(route or "").strip().lower(),
                    risk_tier=str(risk_tier or "").strip().lower(),
                    created_at_unix=now,
                    last_updated_at_unix=now,
                )
                self._budgets[op_id] = budget
                return budget
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[epistemic_budget] open raised: %s", exc,
            )
            return None

    def close(
        self, op_id: str,
    ) -> Optional[EpistemicBudget]:
        """Remove ``op_id`` from the tracker dict. Returns the
        last frozen budget for telemetry / Slice 4 observability;
        returns None if not tracked."""
        try:
            with self._lock:
                return self._budgets.pop(op_id, None)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[epistemic_budget] close raised: %s", exc,
            )
            return None

    def get(self, op_id: str) -> Optional[EpistemicBudget]:
        """Read-only snapshot of the current frozen budget for
        ``op_id``. Returns None if not tracked. NEVER raises."""
        try:
            with self._lock:
                return self._budgets.get(op_id)
        except Exception:  # noqa: BLE001 — defensive
            return None

    # ---- Event ingestion ---------------------------------------

    def note_round_complete(
        self,
        op_id: str,
        *,
        confidence: Optional[float] = None,
        now_ts: Optional[float] = None,
    ) -> Optional[EpistemicBudget]:
        """Tool-round-boundary event from Slice 3's
        tool_executor. Atomically:
          (a) increments :attr:`rounds_consumed`,
          (b) appends a :class:`ConfidenceSample` if
              ``confidence`` provided + recomputes the
              trajectory's ``peak`` / ``nadir`` / ``latest`` /
              ``dropped_in_window``,
          (c) updates :attr:`last_updated_at_unix`.

        Returns the new frozen budget. None if op_id not
        tracked (caller must :meth:`open` first)."""
        try:
            now = _now_or(now_ts)
            with self._lock:
                old = self._budgets.get(op_id)
                if old is None:
                    return None
                if confidence is not None:
                    sample = ConfidenceSample(
                        confidence=float(confidence),
                        at_round_index=int(old.rounds_consumed) + 1,
                        at_unix=now,
                    )
                    new_traj = _build_trajectory_after_sample(
                        old.confidence_trajectory,
                        sample,
                        old.confidence_drop_threshold,
                    )
                else:
                    new_traj = old.confidence_trajectory
                new_budget = EpistemicBudget(
                    op_id=old.op_id,
                    route=old.route,
                    risk_tier=old.risk_tier,
                    rounds_consumed=int(old.rounds_consumed) + 1,
                    max_rounds=old.max_rounds,
                    confidence_trajectory=new_traj,
                    probe_calls_consumed=old.probe_calls_consumed,
                    probe_call_cap=old.probe_call_cap,
                    branch_calls_consumed=(
                        old.branch_calls_consumed
                    ),
                    sbt_branch_cap=old.sbt_branch_cap,
                    confidence_drop_threshold=(
                        old.confidence_drop_threshold
                    ),
                    last_probe_verdict=old.last_probe_verdict,
                    last_sbt_verdict=old.last_sbt_verdict,
                    created_at_unix=old.created_at_unix,
                    last_updated_at_unix=now,
                )
                self._budgets[op_id] = new_budget
                return new_budget
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[epistemic_budget] note_round_complete "
                "raised: %s", exc,
            )
            return None

    def note_probe_completed(
        self,
        op_id: str,
        *,
        verdict: Any,
        now_ts: Optional[float] = None,
    ) -> Optional[EpistemicBudget]:
        """Probe-completion event from Slice 3 after an
        ``await ConfidenceProbeRunner.run(...)`` returns.
        Atomically:
          (a) increments :attr:`probe_calls_consumed`,
          (b) records :attr:`last_probe_verdict` (normalized
              lowercase string from enum/object/str),
          (c) updates :attr:`last_updated_at_unix`.

        Returns the new frozen budget."""
        try:
            now = _now_or(now_ts)
            verdict_str = _normalize_verdict_value(verdict)
            with self._lock:
                old = self._budgets.get(op_id)
                if old is None:
                    return None
                new_budget = EpistemicBudget(
                    op_id=old.op_id,
                    route=old.route,
                    risk_tier=old.risk_tier,
                    rounds_consumed=old.rounds_consumed,
                    max_rounds=old.max_rounds,
                    confidence_trajectory=(
                        old.confidence_trajectory
                    ),
                    probe_calls_consumed=(
                        int(old.probe_calls_consumed) + 1
                    ),
                    probe_call_cap=old.probe_call_cap,
                    branch_calls_consumed=(
                        old.branch_calls_consumed
                    ),
                    sbt_branch_cap=old.sbt_branch_cap,
                    confidence_drop_threshold=(
                        old.confidence_drop_threshold
                    ),
                    last_probe_verdict=verdict_str,
                    last_sbt_verdict=old.last_sbt_verdict,
                    created_at_unix=old.created_at_unix,
                    last_updated_at_unix=now,
                )
                self._budgets[op_id] = new_budget
                return new_budget
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[epistemic_budget] note_probe_completed "
                "raised: %s", exc,
            )
            return None

    def note_sbt_completed(
        self,
        op_id: str,
        *,
        verdict: Any,
        now_ts: Optional[float] = None,
    ) -> Optional[EpistemicBudget]:
        """SBT-completion event from Slice 3 after an SBT spawn
        returns. Atomically:
          (a) increments :attr:`branch_calls_consumed`,
          (b) records :attr:`last_sbt_verdict` (normalized),
          (c) updates :attr:`last_updated_at_unix`.

        Returns the new frozen budget."""
        try:
            now = _now_or(now_ts)
            verdict_str = _normalize_verdict_value(verdict)
            with self._lock:
                old = self._budgets.get(op_id)
                if old is None:
                    return None
                new_budget = EpistemicBudget(
                    op_id=old.op_id,
                    route=old.route,
                    risk_tier=old.risk_tier,
                    rounds_consumed=old.rounds_consumed,
                    max_rounds=old.max_rounds,
                    confidence_trajectory=(
                        old.confidence_trajectory
                    ),
                    probe_calls_consumed=(
                        old.probe_calls_consumed
                    ),
                    probe_call_cap=old.probe_call_cap,
                    branch_calls_consumed=(
                        int(old.branch_calls_consumed) + 1
                    ),
                    sbt_branch_cap=old.sbt_branch_cap,
                    confidence_drop_threshold=(
                        old.confidence_drop_threshold
                    ),
                    last_probe_verdict=old.last_probe_verdict,
                    last_sbt_verdict=verdict_str,
                    created_at_unix=old.created_at_unix,
                    last_updated_at_unix=now,
                )
                self._budgets[op_id] = new_budget
                return new_budget
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[epistemic_budget] note_sbt_completed "
                "raised: %s", exc,
            )
            return None

    # ---- Read API ----------------------------------------------

    def next_action(self, op_id: str) -> BudgetAction:
        """Authoritative routing decision for ``op_id``. Reads
        the current frozen budget + dispatches to
        :func:`compute_budget_action`. Returns DISABLED if op_id
        not tracked (caller forgot to :meth:`open`)."""
        try:
            with self._lock:
                budget = self._budgets.get(op_id)
            if budget is None:
                return BudgetAction(
                    outcome=BudgetOutcome.DISABLED,
                    reason="op_not_tracked",
                )
            return compute_budget_action(budget)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[epistemic_budget] next_action raised: %s",
                exc,
            )
            return BudgetAction(
                outcome=BudgetOutcome.DISABLED,
                reason=f"next_action_failed: {type(exc).__name__}",
            )

    # ---- Maintenance -------------------------------------------

    def reap_orphans(
        self,
        *,
        now_ts: Optional[float] = None,
        ttl_s: Optional[int] = None,
    ) -> int:
        """Sweep tracker dict for entries whose
        ``last_updated_at_unix`` is older than ``ttl_s`` seconds.
        Default TTL via :func:`epistemic_tracker_ttl_s` (env
        ``JARVIS_EPISTEMIC_TRACKER_TTL_S``, default 3600).

        Returns count reaped. NEVER raises."""
        try:
            now = _now_or(now_ts)
            ttl = (
                int(ttl_s) if ttl_s is not None
                else epistemic_tracker_ttl_s()
            )
            if ttl <= 0:
                return 0
            with self._lock:
                stale = [
                    oid for oid, b in self._budgets.items()
                    if (now - b.last_updated_at_unix) > ttl
                ]
                for oid in stale:
                    self._budgets.pop(oid, None)
                return len(stale)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[epistemic_budget] reap_orphans raised: %s", exc,
            )
            return 0

    def all_op_ids(self) -> Tuple[str, ...]:
        """Snapshot of currently-tracked op_ids (for Slice 4
        observability). Order undefined."""
        try:
            with self._lock:
                return tuple(self._budgets.keys())
        except Exception:  # noqa: BLE001 — defensive
            return tuple()

    def __len__(self) -> int:
        try:
            with self._lock:
                return len(self._budgets)
        except Exception:  # noqa: BLE001 — defensive
            return 0


# ---------------------------------------------------------------------------
# Default tracker singleton (process-global)
# ---------------------------------------------------------------------------


def get_default_tracker() -> EpistemicBudgetTracker:
    """Return the process-global default tracker. Lazy-
    constructed; threadsafe."""
    global _DEFAULT_TRACKER  # noqa: PLW0603
    with _DEFAULT_TRACKER_LOCK:
        if _DEFAULT_TRACKER is None:
            _DEFAULT_TRACKER = EpistemicBudgetTracker()
        return _DEFAULT_TRACKER


def reset_default_tracker_for_tests() -> None:
    """Test-only — drop the default tracker. Production code
    NEVER calls this."""
    global _DEFAULT_TRACKER  # noqa: PLW0603
    with _DEFAULT_TRACKER_LOCK:
        _DEFAULT_TRACKER = None


__all__ = [
    "BG_ROUTE",
    "BudgetAction",
    "BudgetOutcome",
    "COST_GATED_ROUTES",
    "ConfidenceSample",
    "ConfidenceTrajectory",
    "EPISTEMIC_BUDGET_SCHEMA_VERSION",
    "EpistemicBudget",
    "EpistemicBudgetTracker",
    "MAX_CALLS_PER_PROBE_DEFAULT",
    "SPEC_ROUTE",
    "compute_budget_action",
    "epistemic_budget_enabled",
    "epistemic_confidence_drop_threshold",
    "epistemic_max_rounds",
    "epistemic_sbt_branch_cap",
    "epistemic_tracker_ttl_s",
    "get_default_tracker",
    "get_max_calls_per_probe",
    "reset_default_tracker_for_tests",
]
