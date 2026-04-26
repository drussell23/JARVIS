"""P4 Slice 1 — MetricsEngine primitive (un-stranding wrapper + 5 net-new
calculators).

Per OUROBOROS_VENOM_PRD.md §9 Phase 4 P4 ("Convergence metrics
suite"):

  > Replace ``convergence_state: "INSUFFICIENT_DATA"`` with metrics
  > that move when O+V gets smarter.

This module is the **pure-data engine** that computes the seven
metrics the PRD calls out as the substrate for the RSI claim:

  1. **composite_score** — weighted sum (test 40%, coverage 20%,
     complexity 15%, lint 10%, semantic-drift 15%) — wraps the
     existing :class:`CompositeScoreFunction` (305 LOC, currently
     un-surfaced; un-stranding pattern mirrors Phase 4 P3
     ``cognitive_metrics`` for OraclePreScorer / VindicationReflector).
  2. **convergence_state** — IMPROVING / LOGARITHMIC / PLATEAUED /
     OSCILLATING / DEGRADING / INSUFFICIENT_DATA — wraps the
     existing :class:`ConvergenceTracker` (354 LOC, also un-surfaced).
  3. **session_completion_rate** — net-new — % sessions with
     ``stop_reason ∈ {idle, budget, wall}`` AND
     (``commits ≥ 1`` OR ``acknowledged_noops ≥ 1``).
  4. **self_formation_ratio** — net-new — self-formed backlog
     entries / total ops per session.
  5. **postmortem_recall_rate** — net-new — % subsequent ops that
     consulted ≥ 1 prior postmortem.
  6. **cost_per_successful_apply** — net-new — total session cost /
     commits (∞ if commits == 0; sentinel ``None`` for caller).
  7. **posture_stability** — net-new — mean dwell time per posture
     (secondary signal of operator-arc tracking).

Slice 1 ships the engine + a frozen :class:`MetricsSnapshot` only —
no JSONL persistence, no REPL, no IDE wiring (those are Slices 2-4).
Slice 5 graduates the master flag.

Authority invariants (PRD §12.2):
  * Pure data — no I/O, no subprocess, no env mutation.
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
    Allowed: ``composite_score`` + ``convergence_tracker`` (the two
    un-stranded primitives this slice exists to surface).
  * Best-effort — every per-metric computation is wrapped in
    ``try / except``; one bad input yields ``None`` for that
    metric, never crashes the snapshot.
  * Bounded — input session-data is consumed via typed-and-clamped
    helpers; ``MAX_OPS_INSPECTED`` caps per-session iteration.
  * **Authority-clean cognitive layer** — the engine is observability
    only; never gates an operation. Iron Gate / risk_tier_floor /
    SemanticGuardian remain authoritative.

Default-off behind ``JARVIS_METRICS_SUITE_ENABLED`` until Slice 5
graduation. Module is importable + callable so future slices can
build on top without flag flips.
"""
from __future__ import annotations

import enum
import logging
import os
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.composite_score import (
    CompositeScore,
    CompositeScoreFunction,
)
from backend.core.ouroboros.governance.convergence_tracker import (
    ConvergenceReport,
    ConvergenceState,
    ConvergenceTracker,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Per-session op iteration cap. Defensive against pathologically
# large session payloads — pinned by tests so future slices know what
# to test against.
MAX_OPS_INSPECTED: int = 4096

# Stop-reasons that count as a "completed" session for the
# session_completion_rate calculation. Mirrors the
# ouroboros_battle_test harness's clean-exit set.
COMPLETED_STOP_REASONS = frozenset({
    "idle", "idle_timeout", "budget", "budget_exhausted",
    "wall", "wall_clock_cap", "complete",
})

# Schema version — bumped on any field shape change so Slice 2's
# JSONL ledger can pin a parser version against it.
METRICS_SNAPSHOT_SCHEMA_VERSION: int = 1


def is_enabled() -> bool:
    """Master flag — ``JARVIS_METRICS_SUITE_ENABLED`` (default false
    until Slice 5 graduation).

    When off, the engine remains importable + callable for tests +
    Slice 2-4 builds; gating happens at the Slice 4 caller
    (``OpsDigestObserver`` ``summary.json`` write site)."""
    return os.environ.get(
        "JARVIS_METRICS_SUITE_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Trend classifier sugar (re-exports ConvergenceState so callers don't
# have to reach into the un-stranded primitive directly)
# ---------------------------------------------------------------------------


class TrendDirection(str, enum.Enum):
    """Operator-friendly subset of ConvergenceState used for the
    Slice 3 ``/metrics trend`` REPL surface."""

    IMPROVING = "IMPROVING"
    PLATEAU = "PLATEAU"          # PLATEAUED ∪ LOGARITHMIC for operators
    OSCILLATING = "OSCILLATING"
    DEGRADING = "DEGRADING"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


_CONVERGENCE_TO_TREND: Dict[ConvergenceState, TrendDirection] = {
    ConvergenceState.IMPROVING: TrendDirection.IMPROVING,
    ConvergenceState.LOGARITHMIC: TrendDirection.PLATEAU,
    ConvergenceState.PLATEAUED: TrendDirection.PLATEAU,
    ConvergenceState.OSCILLATING: TrendDirection.OSCILLATING,
    ConvergenceState.DEGRADING: TrendDirection.DEGRADING,
    ConvergenceState.INSUFFICIENT_DATA: TrendDirection.INSUFFICIENT_DATA,
}


def map_convergence_to_trend(state: ConvergenceState) -> TrendDirection:
    """Folds the 6-value ConvergenceState into the 5-value operator
    vocabulary. PLATEAUED + LOGARITHMIC both project to PLATEAU
    (PRD §9 P4 trend column lists 4 buckets + INSUFFICIENT_DATA)."""
    return _CONVERGENCE_TO_TREND.get(state, TrendDirection.INSUFFICIENT_DATA)


# ---------------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricsSnapshot:
    """One per-session metrics computation. Frozen — every field is
    advisory observability that downstream consumers (Slice 2 ledger,
    Slice 3 REPL, Slice 4 IDE / SSE) may persist verbatim.

    Any field can be ``None`` when the input data was insufficient to
    compute it (engine never raises on bad input). Callers should
    treat ``None`` as "metric unavailable" not "metric is zero"."""

    schema_version: int
    session_id: str
    computed_at_unix: float

    # Wang composite — one value per op, plus the session aggregate.
    composite_score_session_mean: Optional[float] = None
    composite_score_session_min: Optional[float] = None  # best (lowest)
    composite_score_session_max: Optional[float] = None  # worst (highest)
    per_op_composite_scores: Tuple[float, ...] = field(default_factory=tuple)

    # Convergence
    trend: TrendDirection = TrendDirection.INSUFFICIENT_DATA
    convergence_slope: Optional[float] = None
    convergence_oscillation_ratio: Optional[float] = None
    convergence_scores_analyzed: int = 0
    convergence_recommendation: str = ""

    # 5 net-new operator metrics
    session_completion_rate: Optional[float] = None  # 0..1
    self_formation_ratio: Optional[float] = None     # 0..1
    postmortem_recall_rate: Optional[float] = None   # 0..1
    cost_per_successful_apply: Optional[float] = None  # USD; None if 0 commits
    posture_stability_seconds: Optional[float] = None  # mean dwell

    # Provenance
    ops_inspected: int = 0
    ops_truncated: bool = False  # True if MAX_OPS_INSPECTED reached
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        """Stable dict serialization for Slice 2 ledger + Slice 4 IDE
        GET. Tuples become lists; enums become their ``.value``."""
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "computed_at_unix": self.computed_at_unix,
            "composite_score_session_mean": self.composite_score_session_mean,
            "composite_score_session_min": self.composite_score_session_min,
            "composite_score_session_max": self.composite_score_session_max,
            "per_op_composite_scores": list(self.per_op_composite_scores),
            "trend": self.trend.value,
            "convergence_slope": self.convergence_slope,
            "convergence_oscillation_ratio": self.convergence_oscillation_ratio,
            "convergence_scores_analyzed": self.convergence_scores_analyzed,
            "convergence_recommendation": self.convergence_recommendation,
            "session_completion_rate": self.session_completion_rate,
            "self_formation_ratio": self.self_formation_ratio,
            "postmortem_recall_rate": self.postmortem_recall_rate,
            "cost_per_successful_apply": self.cost_per_successful_apply,
            "posture_stability_seconds": self.posture_stability_seconds,
            "ops_inspected": self.ops_inspected,
            "ops_truncated": self.ops_truncated,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Per-metric calculators (pure functions; engine composes them)
# ---------------------------------------------------------------------------


def compute_session_completion_rate(
    sessions: Sequence[Mapping[str, Any]],
) -> Optional[float]:
    """Per PRD §9 P4: % sessions with stop_reason ∈ {idle, budget,
    wall} AND (commits ≥ 1 OR acknowledged_noops ≥ 1).

    Returns ``None`` for empty input."""
    if not sessions:
        return None
    counted = 0
    completed = 0
    for s in sessions:
        if not isinstance(s, Mapping):
            continue
        counted += 1
        stop_reason = str(s.get("stop_reason", "")).strip().lower()
        if stop_reason not in COMPLETED_STOP_REASONS:
            continue
        commits = _safe_int(s.get("commits"))
        ack_noops = _safe_int(s.get("acknowledged_noops"))
        if commits >= 1 or ack_noops >= 1:
            completed += 1
    if counted == 0:
        return None
    return completed / counted


def compute_self_formation_ratio(
    ops: Sequence[Mapping[str, Any]],
) -> Optional[float]:
    """Per PRD §9 P4: self-formed backlog entries / total ops per session.

    An op is "self-formed" when its source is one of
    ``{"auto_proposed", "self_formed", "self_formation"}`` (matches
    the BacklogSensor + SelfGoalFormation envelope shapes from
    Phase 2 P1).

    Returns ``None`` for empty op list."""
    if not ops:
        return None
    self_formed_sources = {"auto_proposed", "self_formed", "self_formation"}
    total = 0
    self_formed = 0
    for op in ops:
        if not isinstance(op, Mapping):
            continue
        total += 1
        src = str(op.get("source", "")).strip().lower()
        if src in self_formed_sources:
            self_formed += 1
    if total == 0:
        return None
    return self_formed / total


def compute_postmortem_recall_rate(
    ops: Sequence[Mapping[str, Any]],
) -> Optional[float]:
    """Per PRD §9 P4 ("partial Improvement 6"): % subsequent ops that
    consulted ≥ 1 prior postmortem.

    Counts ops where ``postmortem_recall_count ≥ 1`` (the field
    PostmortemRecallService stamps on the op envelope when a
    pre-pipeline lookup matched at least one prior incident; the
    "subsequent" semantics — first op excluded — is handled here).

    Returns ``None`` for ≤ 1 op (no "subsequent" set defined)."""
    if not ops or len(ops) <= 1:
        return None
    subsequent = ops[1:]
    counted = 0
    consulted = 0
    for op in subsequent:
        if not isinstance(op, Mapping):
            continue
        counted += 1
        n = _safe_int(op.get("postmortem_recall_count"))
        if n >= 1:
            consulted += 1
    if counted == 0:
        return None
    return consulted / counted


def compute_cost_per_successful_apply(
    total_cost_usd: Any,
    commits: Any,
) -> Optional[float]:
    """Per PRD §9 P4: total session cost / commits.

    Returns ``None`` when commits == 0 (sentinel — caller renders as
    "no commits" rather than "infinite cost"). Negative cost or
    commit values yield ``None`` (defensive — should never happen in
    practice but defends against malformed sessions)."""
    cost = _safe_float(total_cost_usd)
    n = _safe_int(commits)
    if cost is None or n is None or cost < 0 or n <= 0:
        return None
    return cost / n


def compute_posture_stability_seconds(
    posture_dwells: Sequence[Mapping[str, Any]],
) -> Optional[float]:
    """Per PRD §9 P4: mean dwell time per posture (seconds).

    Each entry should be a mapping with a ``duration_s`` (float,
    seconds spent in that posture). Missing / malformed entries are
    skipped. Returns ``None`` for empty / all-malformed input."""
    durations: List[float] = []
    for d in posture_dwells:
        if not isinstance(d, Mapping):
            continue
        v = _safe_float(d.get("duration_s"))
        if v is not None and v >= 0:
            durations.append(v)
    if not durations:
        return None
    return statistics.fmean(durations)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class MetricsEngine:
    """Compose-the-7-metrics primitive. Stateless across calls — each
    ``compute_for_session`` is independent. Wraps the un-stranded
    ``CompositeScoreFunction`` + ``ConvergenceTracker`` so the rest
    of the system never reaches into them directly.

    Caller injects the existing primitives or accepts the safe
    defaults (constructed lazily on first use)."""

    def __init__(
        self,
        composite_score_fn: Optional[CompositeScoreFunction] = None,
        convergence_tracker: Optional[ConvergenceTracker] = None,
        clock=time.time,
    ) -> None:
        self._composite = composite_score_fn
        self._convergence = convergence_tracker
        self._clock = clock

    def compute_for_session(
        self,
        *,
        session_id: str,
        ops: Sequence[Mapping[str, Any]] = (),
        sessions_history: Sequence[Mapping[str, Any]] = (),
        posture_dwells: Sequence[Mapping[str, Any]] = (),
        total_cost_usd: Any = 0.0,
        commits: Any = 0,
    ) -> MetricsSnapshot:
        """Build one :class:`MetricsSnapshot` for the given session.

        Inputs are kept structurally simple (mapping-of-primitives)
        so Slice 2's JSONL ledger + Slice 4's IDE GET can persist /
        serve them without coupling to the orchestrator's internal
        types. Per-op fields consumed:

          * ``composite_score`` — pre-computed (already on the op
            via ``OpsDigestObserver``); falls back to recompute via
            the existing ``CompositeScoreFunction`` if absent and
            test/lint/coverage signals are present.
          * ``source`` — for self_formation_ratio.
          * ``postmortem_recall_count`` — for postmortem_recall_rate.

        Per-session inputs (``sessions_history``):
          * ``stop_reason``, ``commits``, ``acknowledged_noops`` —
            session_completion_rate.

        Other:
          * ``posture_dwells`` — list of {``duration_s``: ...} per
            posture stretch this session; posture_stability_seconds.
          * ``total_cost_usd`` + ``commits`` — top-level session
            totals; cost_per_successful_apply.
        """
        notes: List[str] = []

        # Truncate ops at MAX_OPS_INSPECTED defensively.
        ops_truncated = len(ops) > MAX_OPS_INSPECTED
        if ops_truncated:
            ops = ops[:MAX_OPS_INSPECTED]
            notes.append(
                f"ops truncated at MAX_OPS_INSPECTED={MAX_OPS_INSPECTED}",
            )

        per_op_composite = self._extract_composite_scores(ops, notes)

        comp_mean: Optional[float]
        comp_min: Optional[float]
        comp_max: Optional[float]
        if per_op_composite:
            comp_mean = statistics.fmean(per_op_composite)
            comp_min = min(per_op_composite)
            comp_max = max(per_op_composite)
        else:
            comp_mean = comp_min = comp_max = None

        # Convergence — feed per-op scores through the tracker.
        report = self._safe_convergence_analyze(per_op_composite, notes)
        if report is not None:
            trend = map_convergence_to_trend(report.state)
            slope = report.slope
            osc = report.oscillation_ratio
            analyzed = report.scores_analyzed
            recommendation = report.recommendation
        else:
            trend = TrendDirection.INSUFFICIENT_DATA
            slope = None
            osc = None
            analyzed = 0
            recommendation = ""

        # 5 net-new operator metrics.
        session_complete = self._safe_call(
            compute_session_completion_rate, sessions_history,
            label="session_completion_rate", notes=notes,
        )
        self_form = self._safe_call(
            compute_self_formation_ratio, ops,
            label="self_formation_ratio", notes=notes,
        )
        pm_recall = self._safe_call(
            compute_postmortem_recall_rate, ops,
            label="postmortem_recall_rate", notes=notes,
        )
        cost_per_apply = self._safe_call(
            compute_cost_per_successful_apply, total_cost_usd, commits,
            label="cost_per_successful_apply", notes=notes,
        )
        posture_stab = self._safe_call(
            compute_posture_stability_seconds, posture_dwells,
            label="posture_stability_seconds", notes=notes,
        )

        return MetricsSnapshot(
            schema_version=METRICS_SNAPSHOT_SCHEMA_VERSION,
            session_id=str(session_id),
            computed_at_unix=float(self._clock()),
            composite_score_session_mean=comp_mean,
            composite_score_session_min=comp_min,
            composite_score_session_max=comp_max,
            per_op_composite_scores=tuple(per_op_composite),
            trend=trend,
            convergence_slope=slope,
            convergence_oscillation_ratio=osc,
            convergence_scores_analyzed=analyzed,
            convergence_recommendation=recommendation,
            session_completion_rate=session_complete,
            self_formation_ratio=self_form,
            postmortem_recall_rate=pm_recall,
            cost_per_successful_apply=cost_per_apply,
            posture_stability_seconds=posture_stab,
            ops_inspected=len(ops),
            ops_truncated=ops_truncated,
            notes=tuple(notes),
        )

    # ---- internals ----

    def _extract_composite_scores(
        self,
        ops: Sequence[Mapping[str, Any]],
        notes: List[str],
    ) -> List[float]:
        """Per-op composite scores. Prefers a pre-computed
        ``composite_score`` on the op envelope; falls back to
        recompute via :class:`CompositeScoreFunction` when the raw
        signals are present."""
        out: List[float] = []
        for op in ops:
            if not isinstance(op, Mapping):
                continue
            pre = _safe_float(op.get("composite_score"))
            if pre is not None and 0.0 <= pre <= 1.0:
                out.append(pre)
                continue
            recomputed = self._maybe_recompute(op, notes)
            if recomputed is not None:
                out.append(recomputed)
        return out

    def _maybe_recompute(
        self,
        op: Mapping[str, Any],
        notes: List[str],
    ) -> Optional[float]:
        """Best-effort recompute when the op carries the raw
        before/after signals. Skipped silently when any signal is
        missing — caller (this engine) just gets fewer data points."""
        required = (
            "test_pass_rate_before", "test_pass_rate_after",
            "coverage_before", "coverage_after",
            "complexity_before", "complexity_after",
            "lint_violations_before", "lint_violations_after",
            "blast_radius_total",
        )
        if not all(k in op for k in required):
            return None
        try:
            fn = self._composite or self._lazy_composite_fn()
            score: CompositeScore = fn.compute(
                op_id=str(op.get("op_id", "anon")),
                test_pass_rate_before=float(op["test_pass_rate_before"]),
                test_pass_rate_after=float(op["test_pass_rate_after"]),
                coverage_before=float(op["coverage_before"]),
                coverage_after=float(op["coverage_after"]),
                complexity_before=float(op["complexity_before"]),
                complexity_after=float(op["complexity_after"]),
                lint_violations_before=int(op["lint_violations_before"]),
                lint_violations_after=int(op["lint_violations_after"]),
                blast_radius_total=int(op["blast_radius_total"]),
            )
            return score.composite
        except Exception as exc:  # noqa: BLE001
            notes.append(f"composite recompute skipped: {exc}")
            return None

    def _lazy_composite_fn(self) -> CompositeScoreFunction:
        """Construct the wrapped CompositeScoreFunction on first use
        so callers without the raw-signal recompute path don't pay
        for it."""
        if self._composite is None:
            self._composite = CompositeScoreFunction()
        return self._composite

    def _safe_convergence_analyze(
        self,
        scores: List[float],
        notes: List[str],
    ) -> Optional[ConvergenceReport]:
        if not scores:
            return None
        try:
            tracker = self._convergence or self._lazy_tracker()
            return tracker.analyze(scores)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"convergence skipped: {exc}")
            return None

    def _lazy_tracker(self) -> ConvergenceTracker:
        if self._convergence is None:
            self._convergence = ConvergenceTracker()
        return self._convergence

    @staticmethod
    def _safe_call(
        fn,
        *args,
        label: str,
        notes: List[str],
    ) -> Optional[float]:
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{label} skipped: {exc}")
            return None


# ---------------------------------------------------------------------------
# Defensive number coercion (used by per-metric calculators above)
# ---------------------------------------------------------------------------


def _safe_int(v: Any) -> int:
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_default_engine: Optional[MetricsEngine] = None


def get_default_engine() -> MetricsEngine:
    """Process-wide engine. Lazy-construct on first call. No master
    flag on the accessor — engine is callable / queryable even when
    ``JARVIS_METRICS_SUITE_ENABLED`` is off so future slices can
    inspect snapshots after a revert (mirrors P3 + P2 patterns)."""
    global _default_engine
    if _default_engine is None:
        _default_engine = MetricsEngine()
    return _default_engine


def reset_default_engine() -> None:
    """Reset the singleton — for tests."""
    global _default_engine
    _default_engine = None


__all__ = [
    "COMPLETED_STOP_REASONS",
    "MAX_OPS_INSPECTED",
    "METRICS_SNAPSHOT_SCHEMA_VERSION",
    "MetricsEngine",
    "MetricsSnapshot",
    "TrendDirection",
    "compute_cost_per_successful_apply",
    "compute_postmortem_recall_rate",
    "compute_posture_stability_seconds",
    "compute_self_formation_ratio",
    "compute_session_completion_rate",
    "get_default_engine",
    "is_enabled",
    "map_convergence_to_trend",
    "reset_default_engine",
]
