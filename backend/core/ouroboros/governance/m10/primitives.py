"""M10 Slice 1 — ArchitectureProposer primitives (PRD §32.4).

Frozen-dataclass + closed-enum contract layer for the entire
M10 arc. Slices 2-5 implement against the shape established here
(matches the discipline used for Upgrade 1 / M9 / M11 / Upgrade 2).

This module is **stdlib-only** + reuses the design contracts
lifted from the archived ``graduation_orchestrator.py``:
  * `M10ProposalPhase` — 16-value FSM (renamed `GraduationPhase`)
  * `M10AdaptiveThreshold` — Beta posterior + diversity
    computation (lifted verbatim from
    ``graduation_orchestrator.compute_adaptive_threshold`` lines
    63–85). Beta(1+s, 1+f) posterior mean × diversity-adjusted
    multiplier produces a per-pattern threshold.
  * `M10ProposalRecord` — frozen analog to `GraduationRecord`,
    swapped from file-polling paths to OrangePRReviewer
    references + ledger row IDs
  * `ProposalKind` — 5-value closed enum
    (NEW_SENSOR / NEW_PHASE / NEW_OBSERVER / NEW_FLAG_FAMILY /
    DISABLED) — extends graduation_orchestrator's NEW_AGENT
    single-purpose to M10's broader scope.

Architectural locks (operator mandate, AST-pinned at Slice 5):

  * **Stdlib only** — no orchestrator/iron_gate/providers/
    candidate_generator/semantic_guardian/policy/strategic_-
    direction imports. Pure data primitives.
  * **Frozen dataclasses + atomic-swap mutation** — Slices 2-5
    construct new instances rather than mutating in place
    (matches `EpistemicBudget` / `ActionOutcomeRecord` /
    `FailureModeRecord` / `CuriosityScore` discipline).
  * **Closed-enum dispatch** — every routing decision branches
    on `M10ProposalPhase` / `ProposalKind`, never on freeform
    strings.
  * **Master flag default-FALSE** until 30+ proposal-acceptance
    audit (per §30.5.2). Slice 5 graduates the opt-in surface
    only — production stays default-false.
  * **No hardcoding** — all knobs read at call time via
    `_read_int_knob` / `_read_float_knob` (proven pattern from
    Upgrade 1 / M9 / M11). FlagRegistry seeds at Slice 5.
  * **Adaptive threshold cold-start** — when total_uses=0,
    `compute_threshold` returns INSUFFICIENT_DATA_THRESHOLD
    sentinel; consumers default to fixed threshold 3 (matches
    graduation_orchestrator's `_GRADUATION_THRESHOLD` default).
"""
from __future__ import annotations

import enum
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


M10_PRIMITIVES_SCHEMA_VERSION: str = "m10_primitives.1"


# ---------------------------------------------------------------------------
# Master flag — defaults FALSE; stays default-false at Slice 5
# ---------------------------------------------------------------------------


def m10_arch_proposer_enabled() -> bool:
    """``JARVIS_M10_ARCH_PROPOSER_ENABLED`` — three-state resolution
    (Slice 197 supersedes the static §30.5.2 binding via the
    operator-delegated autonomous graduation contract):

      1. explicit ``1``/``true``/``yes``/``on`` → True (operator-on)
      2. any other explicit value (incl. ``0``/``false``) → False —
         the operator KILL SWITCH, supreme over any autonomous state
         (Slice 136 precedent: operator =0 precedence honored)
      3. unset/empty → consult the autonomous graduation contract
         (``m10_autonomous_graduation.is_autonomously_unlocked`` —
         criteria proven against the durable mmap registry; sticky
         once persisted; fail-soft → False)

    Re-read on every call so operator flips hot-revert without
    restart."""
    raw = os.environ.get(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        # Slice 197 — operator-delegated autonomous graduation.
        try:
            from backend.core.ouroboros.governance.m10_autonomous_graduation import (  # noqa: E501
                is_autonomously_unlocked,
            )
            return bool(is_autonomously_unlocked())
        except Exception:  # noqa: BLE001 — contract unavailable → locked
            return False
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env knobs — bounded clamping
# ---------------------------------------------------------------------------


def _read_int_knob(
    name: str, default: int, floor: int, ceiling: int,
) -> int:
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
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        f = float(raw)
        if not math.isfinite(f):
            return default
        if f < floor:
            return floor
        if f > ceiling:
            return ceiling
        return f
    except (TypeError, ValueError):
        return default


def m10_adaptive_min_threshold() -> int:
    """``JARVIS_M10_ADAPTIVE_MIN_THRESHOLD`` — minimum
    threshold floor. Default 2; clamped [1, 100]. Lifted from
    graduation_orchestrator's ``_ADAPTIVE_MIN_THRESHOLD``."""
    return _read_int_knob(
        "JARVIS_M10_ADAPTIVE_MIN_THRESHOLD", 2, 1, 100,
    )


def m10_adaptive_confidence() -> float:
    """``JARVIS_M10_ADAPTIVE_CONFIDENCE`` — Bayesian confidence
    multiplier (higher = more conservative threshold). Default
    2.0; clamped [0.1, 100.0]. Lifted from graduation_-
    orchestrator's ``_ADAPTIVE_CONFIDENCE``."""
    return _read_float_knob(
        "JARVIS_M10_ADAPTIVE_CONFIDENCE", 2.0, 0.1, 100.0,
    )


def m10_max_daily_proposals() -> int:
    """``JARVIS_M10_MAX_DAILY`` — hard cap on proposals per
    day (cost contract). Default 5; clamped [1, 100]. Per
    §32.4.3 cost analysis: 5 proposals × $0.015 each =
    $0.075/day max."""
    return _read_int_knob(
        "JARVIS_M10_MAX_DAILY", 5, 1, 100,
    )


def m10_approval_timeout_s() -> int:
    """``JARVIS_M10_APPROVAL_TIMEOUT_S`` — approval timeout
    before phase → EXPIRED + worktree cleanup. Default 86400
    (24h); clamped [60, 604800]. Per §32.4.4 H4 inheritance."""
    return _read_int_knob(
        "JARVIS_M10_APPROVAL_TIMEOUT_S",
        86400, 60, 604800,
    )


def m10_acceptance_rate_floor() -> float:
    """``JARVIS_M10_ACCEPTANCE_RATE_FLOOR`` — operator-fatigue
    auto-pause threshold. When proposal acceptance rate over
    last 20 proposals drops below this, miner auto-paused for
    one posture cycle. Default 0.30 (30%); clamped [0.0, 1.0].
    Per §30.5.2 + §32.4.4 MetaAdaptationGovernor inheritance."""
    return _read_float_knob(
        "JARVIS_M10_ACCEPTANCE_RATE_FLOOR", 0.30, 0.0, 1.0,
    )


# ---------------------------------------------------------------------------
# M10ProposalPhase — 16-value FSM (lifted from GraduationPhase)
# ---------------------------------------------------------------------------


class M10ProposalPhase(str, enum.Enum):
    """Closed FSM for M10 proposal lifecycle. 16 values
    inherited verbatim from graduation_orchestrator's
    ``GraduationPhase`` (renamed for M10 scope) — ensures the
    H1-H6 hard-won lessons compose by construction (PUSH_FAILED
    explicit phase, post-merge readiness probe, etc.).

    Transitions (per §32.4.1):
      DETECTING → EVALUATING → (DECIDED_SKIP | WORKTREE_CREATING)
      WORKTREE_CREATING → GENERATING → VALIDATING → COMMITTING
      COMMITTING → AWAITING_APPROVAL
      AWAITING_APPROVAL → (PUSHING | REJECTED | EXPIRED)
      PUSHING → (AWAITING_MERGE | PUSH_FAILED)
      AWAITING_MERGE → REGISTERING → (GRADUATED | FAILED)
    """

    DETECTING = "detecting"
    EVALUATING = "evaluating"
    DECIDED_SKIP = "decided_skip"
    WORKTREE_CREATING = "worktree_creating"
    GENERATING = "generating"
    VALIDATING = "validating"
    COMMITTING = "committing"
    AWAITING_APPROVAL = "awaiting_approval"
    PUSHING = "pushing"
    PUSH_FAILED = "push_failed"
    AWAITING_MERGE = "awaiting_merge"
    REGISTERING = "registering"
    GRADUATED = "graduated"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# ProposalKind — 5-value closed taxonomy
# ---------------------------------------------------------------------------


class ProposalKind(str, enum.Enum):
    """Closed taxonomy of architecture-extension proposal
    kinds. Extends graduation_orchestrator's NEW_AGENT
    single-purpose to M10's broader scope. New kinds added
    here MUST be paired with a corresponding generator path in
    Slice 3 — closed-enum dispatch enforces no orphan kinds."""

    NEW_SENSOR = "new_sensor"
    """A new ``IntakeSensor`` Protocol-conforming sensor
    class. Generated body satisfies ``scan_once`` async
    method + ``signal_kind`` class attr."""

    NEW_PHASE = "new_phase"
    """A new phase runner — must conform to the
    ``phase_runners`` Protocol shape. Higher-risk than sensors
    since it sits on the FSM happy path; Slice 4 validation
    forces extra checks."""

    NEW_OBSERVER = "new_observer"
    """A new async observer (analogous to ClosureLoopObserver
    / TrajectoryAuditorObserver / etc.). Pure-substrate
    consumer; no FSM authority."""

    NEW_FLAG_FAMILY = "new_flag_family"
    """A new family of `JARVIS_*` env flags (FlagRegistry seed
    + AST-pinned no-hardcoding). Lowest-risk kind; no executable
    code path until consumers wire."""

    DISABLED = "disabled"
    """Sentinel — master flag is off OR proposer was paused.
    Returned by `compute_threshold` cold-start path."""


# ---------------------------------------------------------------------------
# M10AdaptiveThreshold — frozen dataclass + pure compute fn
# ---------------------------------------------------------------------------


# Cold-start sentinel — when total_uses=0 OR insufficient data,
# this constant is returned. Consumers see is_cold_start() and
# fall back to the hardcoded ``_FALLBACK_THRESHOLD`` (matches
# graduation_orchestrator's `_GRADUATION_THRESHOLD` default of 3).
_FALLBACK_THRESHOLD: int = 3


@dataclass(frozen=True)
class M10AdaptiveThreshold:
    """Bayesian adaptive threshold result. Lifted verbatim
    from graduation_orchestrator's ``AdaptiveThresholdResult``
    (lines 56-60) with field shape preserved + cold-start
    sentinel added.

    Beta(1+s, 1+f) posterior mean for success probability,
    adjusted by goal-diversity ratio (unique/total). Higher
    diversity = stronger evidence = lower threshold needed.
    """

    threshold: int
    """Number of successes required before this proposal class
    graduates. Floor = :func:`m10_adaptive_min_threshold`
    (default 2)."""

    p_success: float
    """Beta posterior mean for success probability. Range
    [0, 1]. Computed as (1+s) / (2+s+f)."""

    diversity: float
    """Goal-diversity ratio in [0, 1]. Computed as
    min(1.0, unique_goals / total_uses) when total_uses > 0,
    else 0.0."""

    effective_p: float
    """Diversity-adjusted success probability:
    ``p_success × (0.5 + 0.5 × diversity)``. Drives the
    threshold formula:
    ``threshold = max(min_floor, ceil(confidence / effective_p))``."""

    is_cold_start: bool = False
    """True when total_uses=0 OR threshold computation cannot
    proceed (NaN inputs, etc.). Consumers default to fixed
    threshold ``_FALLBACK_THRESHOLD`` (3) when this is True."""

    schema_version: str = field(
        default=M10_PRIMITIVES_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        """Sanitized JSON-safe projection. NEVER raises."""
        try:
            return {
                "schema_version": self.schema_version,
                "threshold": int(self.threshold),
                "p_success": float(self.p_success),
                "diversity": float(self.diversity),
                "effective_p": float(self.effective_p),
                "is_cold_start": bool(self.is_cold_start),
            }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "schema_version": self.schema_version,
                "error": "projection_failed",
            }


def compute_threshold(
    *,
    successes: int,
    failures: int,
    unique_goals: int,
    total_uses: int,
) -> M10AdaptiveThreshold:
    """**Authoritative pure aggregator.** Bayesian adaptive
    threshold. Lifted verbatim from
    graduation_orchestrator's ``compute_adaptive_threshold``
    lines 63-85.

    Returns a frozen :class:`M10AdaptiveThreshold`. Pure —
    same inputs → same outputs. NEVER raises (defensive
    fallbacks for negative inputs, NaN, divide-by-zero)."""
    # Defensive input sanitization
    try:
        s = max(0, int(successes))
        f = max(0, int(failures))
        u = max(0, int(unique_goals))
        t = max(0, int(total_uses))
    except (TypeError, ValueError):
        # Cold-start sentinel — caller-supplied non-int
        return M10AdaptiveThreshold(
            threshold=_FALLBACK_THRESHOLD,
            p_success=0.0, diversity=0.0,
            effective_p=0.0, is_cold_start=True,
        )

    if t == 0:
        # Cold-start — no usage history yet
        return M10AdaptiveThreshold(
            threshold=_FALLBACK_THRESHOLD,
            p_success=0.0, diversity=0.0,
            effective_p=0.0, is_cold_start=True,
        )

    # Beta(1+s, 1+f) posterior mean
    p_success = (1.0 + s) / (2.0 + s + f)
    diversity = min(1.0, float(u) / float(t)) if t > 0 else 0.0
    effective_p = p_success * (0.5 + 0.5 * diversity)

    min_floor = m10_adaptive_min_threshold()
    confidence = m10_adaptive_confidence()

    if effective_p > 0:
        try:
            threshold = max(
                min_floor,
                int(math.ceil(confidence / effective_p)),
            )
        except (ValueError, OverflowError):
            threshold = max(
                min_floor,
                int(math.ceil(confidence / 0.1)),
            )
    else:
        threshold = max(
            min_floor,
            int(math.ceil(confidence / 0.1)),
        )

    return M10AdaptiveThreshold(
        threshold=threshold,
        p_success=round(p_success, 4),
        diversity=round(diversity, 4),
        effective_p=round(effective_p, 4),
        is_cold_start=False,
    )


# ---------------------------------------------------------------------------
# M10ProposalRecord — frozen lifecycle record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class M10ProposalRecord:
    """One proposal lifecycle record. Frozen — Slice 2's miner
    constructs new instances rather than mutating in place
    (matches the immutability discipline of all other Upgrade
    arcs).

    Analog to graduation_orchestrator's ``GraduationRecord``
    but swaps the file-polling approval-path fields for
    OrangePRReviewer references + ledger row IDs (per §32.4.1
    Path C composition).

    Every field that Slice 2's miner populates + Slice 3's
    synthesizer reads + Slice 4's validation pipeline writes
    + Slice 5 observability projects is NAMED here. No invented
    fields downstream."""

    # ---- Identity ------------------------------------------------

    proposal_id: str
    """Stable identifier — typically ``m10-{kind}-{ts_unix}``.
    Used as the OrangePRReviewer review_pr branch suffix +
    JSONL ledger row primary key + observability lookup
    keying dimension."""

    kind: ProposalKind
    """Closed-enum dispatch. Determines which Slice 3
    generator path runs + which Slice 4 validation extras
    apply (e.g., NEW_PHASE forces a Protocol-conformance check
    that NEW_OBSERVER skips)."""

    phase: M10ProposalPhase = M10ProposalPhase.DETECTING
    """Current FSM phase. Slice 2 emits at DETECTING;
    Slice 3 advances through GENERATING; Slice 4 advances to
    VALIDATING / COMMITTING / PUSHING; Slice 5's observability
    REPL surfaces the phase transition history."""

    # ---- Detection inputs (set at DETECTING / EVALUATING) -------

    pattern_signature: str = ""
    """Stable hash of the unhandled-pattern bundle that
    triggered this proposal. Slice 2's miner aggregates
    intake_router.jsonl + coherence_history.jsonl tuples into
    this. Used for dedup — same signature within a window =
    same proposal."""

    detection_evidence: Tuple[str, ...] = field(
        default_factory=tuple,
    )
    """Operator-readable evidence strings (signal source / op
    kind / recurrence count). Slice 5 REPL renders these
    verbatim so operators see WHY a proposal fired."""

    # ---- Threshold evaluation ----------------------------------

    threshold: Optional[M10AdaptiveThreshold] = None
    """Set at EVALUATING phase. None at DETECTING. Drives the
    DECIDED_SKIP fork — when current observed pattern count <
    threshold.threshold, proposal goes to DECIDED_SKIP without
    consuming a generator slot."""

    # ---- Synthesis output (set at GENERATING) ------------------

    proposed_module_path: str = ""
    """Repository-relative path the synthesizer wrote.
    e.g., ``backend/core/ouroboros/governance/intake/sensors/
    new_pattern_sensor.py``. Empty until Slice 3 runs."""

    proposed_class_name: str = ""
    """Class name within the proposed module."""

    proposed_ast_pin_name: str = ""
    """The AST invariant the synthesizer self-pinned. Mandatory
    per Slice 3 contract (§32.4.4): proposals MUST include a
    self-pin or are rejected at Iron Gate. Empty before
    GENERATING; populated by Slice 3."""

    # ---- Validation result (set at VALIDATING) -----------------

    validation_passed: bool = False
    """5-layer validation outcome (SideEffectFirewall + Protocol
    conformance + SemanticGuardian + SecurityScanner + pytest).
    Set by Slice 4."""

    validation_failures: Tuple[str, ...] = field(
        default_factory=tuple,
    )
    """Per-layer failure messages. Empty when
    ``validation_passed=True``. Operator-explainability for
    Slice 5 REPL."""

    # ---- Approval / PR (set at COMMITTING / AWAITING_APPROVAL) -

    worktree_path: str = ""
    """Filesystem path of the WorktreeManager-created
    worktree. Empty when phase < WORKTREE_CREATING. Cleaned
    up at REJECTED / EXPIRED / FAILED transitions per §32.4.4
    H4 inheritance."""

    review_pr_url: str = ""
    """OrangePRReviewer-created GitHub PR URL. Empty when
    phase < AWAITING_APPROVAL."""

    review_pr_branch: str = ""
    """Branch name (typically ``ouroboros/m10/<proposal_id>``).
    Preserved on PUSH_FAILED for retry."""

    # ---- Cost accounting (per §32.4.3) -------------------------

    total_cost_usd: float = 0.0
    """Cumulative cost across all model calls for this
    proposal. Auto-accumulates via existing route ledger
    (NO parallel cost system — H6 inheritance via
    composition). Slice 3's synthesizer adds the Quorum K=3
    cost (~$0.015/proposal)."""

    # ---- Outcome (set at terminal phases) ----------------------

    failure_reason: str = ""
    """Set when phase ∈ {FAILED, REJECTED, EXPIRED, PUSH_FAILED}.
    Operator-readable — Slice 5 REPL renders verbatim. Empty
    on happy-path GRADUATED."""

    # ---- Timestamps --------------------------------------------

    created_at_unix: float = field(default_factory=time.time)
    last_updated_at_unix: float = field(default_factory=time.time)

    schema_version: str = field(
        default=M10_PRIMITIVES_SCHEMA_VERSION,
    )

    # ---- Pure helpers consumed by Slices 2-5 -------------------

    def is_terminal(self) -> bool:
        """True iff the proposal has reached a terminal
        FSM state (GRADUATED / FAILED / REJECTED / EXPIRED /
        DECIDED_SKIP). Slice 2's miner skips re-emission for
        terminal proposals; Slice 5 archives them."""
        return self.phase in (
            M10ProposalPhase.GRADUATED,
            M10ProposalPhase.FAILED,
            M10ProposalPhase.REJECTED,
            M10ProposalPhase.EXPIRED,
            M10ProposalPhase.DECIDED_SKIP,
        )

    def is_awaiting_human(self) -> bool:
        """True iff the proposal is blocked on operator
        action (AWAITING_APPROVAL / AWAITING_MERGE). Slice 5
        REPL surfaces these as "operator queue"."""
        return self.phase in (
            M10ProposalPhase.AWAITING_APPROVAL,
            M10ProposalPhase.AWAITING_MERGE,
        )

    def has_required_self_pin(self) -> bool:
        """True iff the proposal carries a self-AST-pin name.
        Mandatory at Slice 3 — Iron Gate rejects synthesis
        outputs that don't self-pin (per §32.4.4)."""
        return bool(self.proposed_ast_pin_name.strip())

    # ---- Slice 5 observability projection ----------------------

    def to_dict(self) -> Dict[str, Any]:
        """Sanitized JSON-safe projection for
        ``GET /observability/m10/{proposals,proposal/{id}}``
        + REPL rendering. NEVER raises."""
        try:
            return {
                "schema_version": self.schema_version,
                "proposal_id": self.proposal_id,
                "kind": self.kind.value,
                "phase": self.phase.value,
                "pattern_signature": self.pattern_signature,
                "detection_evidence": list(
                    self.detection_evidence,
                ),
                "threshold": (
                    self.threshold.to_dict()
                    if self.threshold is not None
                    else None
                ),
                "proposed_module_path": (
                    self.proposed_module_path
                ),
                "proposed_class_name": self.proposed_class_name,
                "proposed_ast_pin_name": (
                    self.proposed_ast_pin_name
                ),
                "validation_passed": bool(
                    self.validation_passed,
                ),
                "validation_failures": list(
                    self.validation_failures,
                ),
                "worktree_path": self.worktree_path,
                "review_pr_url": self.review_pr_url,
                "review_pr_branch": self.review_pr_branch,
                "total_cost_usd": float(self.total_cost_usd),
                "failure_reason": self.failure_reason,
                "created_at_unix": float(self.created_at_unix),
                "last_updated_at_unix": float(
                    self.last_updated_at_unix,
                ),
                "is_terminal": self.is_terminal(),
                "is_awaiting_human": self.is_awaiting_human(),
                "has_required_self_pin": (
                    self.has_required_self_pin()
                ),
            }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "schema_version": self.schema_version,
                "proposal_id": self.proposal_id,
                "error": "projection_failed",
            }


__all__ = [
    "M10_PRIMITIVES_SCHEMA_VERSION",
    "M10AdaptiveThreshold",
    "M10ProposalPhase",
    "M10ProposalRecord",
    "ProposalKind",
    "compute_threshold",
    "m10_acceptance_rate_floor",
    "m10_adaptive_confidence",
    "m10_adaptive_min_threshold",
    "m10_approval_timeout_s",
    "m10_arch_proposer_enabled",
    "m10_max_daily_proposals",
]
