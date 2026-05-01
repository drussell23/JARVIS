"""Priority #3 Slice 3 — Counterfactual Replay comparator + aggregator.

The empirical-evidence layer for Priority #3 (Counterfactual Replay).

Slice 1 shipped the primitive (closed-taxonomy schema + per-verdict
``compute_replay_outcome``). Slice 2 shipped the engine (recorded
ledger + summary → ``ReplayVerdict``). Slice 3 (this module) ships:

  1. **Structural ``MonotonicTighteningVerdict.PASSED`` stamping**
     of every verdict, replacing Slice 2's stamping into the
     ``detail`` string. Replay is observational not prescriptive
     — so every stamp is canonically PASSED. The cross-stack
     vocabulary (Move 6, Priority #1, #2 also stamp PASSED) lets
     operators correlate via a shared canonical token.

  2. **Recurrence-reduction-pct aggregator** over a stream of
     verdicts. Counts prevention evidence (DIVERGED_BETTER),
     regressions (DIVERGED_WORSE), and equivalents. Computes the
     empirical baseline that retroactively justifies Move 6's
     master flag graduation: "did the policy under test actually
     reduce postmortem recurrence?" — a measurable percentage,
     not just correlation.

  3. **Closed-taxonomy comparison outcomes** so caller code maps
     every aggregate result to exactly one of:
     ESTABLISHED / INSUFFICIENT_DATA / DEGRADED / DISABLED / FAILED.

  4. **Bounded baseline quality** (HIGH / MEDIUM / LOW /
     INSUFFICIENT / FAILED) — operators can tell at a glance how
     much trust to place in the recurrence-reduction-pct number.

Direct-solve principles (per the operator directive):

  * **Asynchronous-ready** — pure-data primitives propagate cleanly
    across async boundaries (Slice 4's history store + SSE event
    publisher will round-trip ``ComparisonReport`` through
    ``asyncio.to_thread`` and SSE serialization).

  * **Dynamic** — every quality threshold (HIGH N / MEDIUM N /
    LOW N / prevention threshold / degradation threshold) is
    env-tunable with floor + ceiling clamps. NO hardcoded magic
    constants in the comparator's decision tree.

  * **Adaptive** — empty input → INSUFFICIENT_DATA; mostly-failed
    input → DEGRADED; sub-flag off → DISABLED; non-iterable input
    → FAILED. Every degenerate case maps to an explicit closed-
    taxonomy outcome rather than an exception.

  * **Intelligent** — recurrence-reduction-pct is computed over the
    set of ACTIONABLE verdicts (DIVERGED_BETTER + DIVERGED_WORSE +
    EQUIVALENT) — not over verdicts that failed to project at all
    (FAILED / PARTIAL / DISABLED). This avoids degrading the
    statistic when a session simply lacked the swap point.

  * **Robust** — every public function NEVER raises. Pure-data
    aggregator can be called from any context, sync or async.
    Garbage in → ``ComparisonOutcome.FAILED`` rather than crash.

  * **No hardcoding** — 5-value + 5-value closed taxonomy enums
    (J.A.R.M.A.T.R.I.X. — every input maps to exactly one). Per-
    knob env helpers with floor + ceiling clamps mirror Slice 1 +
    Slice 2 patterns.

Authority invariants (AST-pinned by Slice 5):

  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.

  * Read-only — never writes a file, never executes code, never
    invokes a model.

  * No async (the aggregator is pure data; Slice 4 wraps via
    ``asyncio.to_thread`` at the history-store layer).

  * No exec / eval / compile (mirrors Slice 1 + 2 critical safety
    pin).

  * Reuses ``adaptation.ledger.MonotonicTighteningVerdict`` —
    canonical vocabulary across the stack. Falls back to the
    literal string ``"passed"`` if the import fails (defensive —
    same vocabulary regardless).

Master flag (Slice 1): ``JARVIS_COUNTERFACTUAL_REPLAY_ENABLED``.
Comparator sub-flag (this module): ``JARVIS_REPLAY_COMPARATOR_ENABLED``
(default-false until Slice 5; gates the aggregator's per-call
enabled-check even if Slice 1's master is on — operators can keep
the schema live while disabling aggregation for a cost-cap rollback).
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Iterable, Optional

# Slice 1 primitives (pure-stdlib reuse) — only the closed-taxonomy
# enums + the BranchSnapshot field shape. NEVER pulls compute_*
# helpers because Slice 3 is an aggregator, not a per-verdict
# computer.
from backend.core.ouroboros.governance.verification.counterfactual_replay import (
    BranchVerdict,
    ReplayOutcome,
    ReplayVerdict,
    counterfactual_replay_enabled,
)

logger = logging.getLogger(__name__)


COUNTERFACTUAL_COMPARATOR_SCHEMA_VERSION: str = (
    "counterfactual_replay_comparator.1"
)


# ---------------------------------------------------------------------------
# Sub-flag — independent rollback knob from Slice 1's master
# ---------------------------------------------------------------------------


def comparator_enabled() -> bool:
    """``JARVIS_REPLAY_COMPARATOR_ENABLED`` — comparator-loader gate.

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit truthy/falsy overrides at call time.

    Default ``false`` until Slice 5 graduation. Independent from
    Slice 1's ``JARVIS_COUNTERFACTUAL_REPLAY_ENABLED`` so operators
    can keep the schema (Slice 1 enums + dataclasses) live in
    serialization paths while disabling the aggregator for a cost-
    cap rollback or empirical-question re-validation.

    Both flags must be ``true`` for ``compare_replay_history`` to
    actually aggregate; if either is off the comparator returns
    ``ComparisonOutcome.DISABLED`` immediately."""
    raw = os.environ.get(
        "JARVIS_REPLAY_COMPARATOR_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-off until Slice 5 graduation
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env knobs — every threshold is operator-tunable with bounded clamps
# ---------------------------------------------------------------------------


def _read_int_knob(
    name: str, default: int, floor: int, ceiling: int,
) -> int:
    """Read an int env knob with floor+ceiling clamping. Mirrors
    Slice 1's pattern. NEVER raises on garbage input — returns
    default."""
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        val = int(raw)
        return max(floor, min(ceiling, val))
    except (TypeError, ValueError):
        return default


def _read_float_knob(
    name: str, default: float, floor: float, ceiling: float,
) -> float:
    """Read a float env knob with floor+ceiling clamping. NEVER
    raises on garbage input — returns default."""
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        val = float(raw)
        return max(floor, min(ceiling, val))
    except (TypeError, ValueError):
        return default


def baseline_high_n_threshold() -> int:
    """``JARVIS_REPLAY_BASELINE_HIGH_N`` — minimum replays for
    BaselineQuality.HIGH. Default 30, clamped [10, 1000]."""
    return _read_int_knob(
        "JARVIS_REPLAY_BASELINE_HIGH_N", 30, 10, 1000,
    )


def baseline_medium_n_threshold() -> int:
    """``JARVIS_REPLAY_BASELINE_MEDIUM_N`` — minimum replays for
    BaselineQuality.MEDIUM. Default 10, clamped [3, 100]."""
    return _read_int_knob(
        "JARVIS_REPLAY_BASELINE_MEDIUM_N", 10, 3, 100,
    )


def baseline_low_n_threshold() -> int:
    """``JARVIS_REPLAY_BASELINE_LOW_N`` — minimum replays for
    BaselineQuality.LOW (anything below → INSUFFICIENT). Default
    3, clamped [1, 50]."""
    return _read_int_knob(
        "JARVIS_REPLAY_BASELINE_LOW_N", 3, 1, 50,
    )


def prevention_threshold_pct() -> float:
    """``JARVIS_REPLAY_PREVENTION_THRESHOLD_PCT`` — minimum
    recurrence-reduction-pct (over actionable verdicts) for
    ComparisonOutcome.ESTABLISHED. Default 50.0, clamped
    [0.0, 100.0]."""
    return _read_float_knob(
        "JARVIS_REPLAY_PREVENTION_THRESHOLD_PCT", 50.0, 0.0, 100.0,
    )


def degradation_threshold_pct() -> float:
    """``JARVIS_REPLAY_DEGRADATION_THRESHOLD_PCT`` — when
    DIVERGED_WORSE / actionable_total exceeds this, the outcome is
    DEGRADED (counterfactuals consistently worse than originals →
    a strong signal that the policy under test should NOT graduate).
    Default 50.0, clamped [0.0, 100.0]."""
    return _read_float_knob(
        "JARVIS_REPLAY_DEGRADATION_THRESHOLD_PCT", 50.0, 0.0, 100.0,
    )


# ---------------------------------------------------------------------------
# Closed-taxonomy enums — J.A.R.M.A.T.R.I.X. discipline
# ---------------------------------------------------------------------------


class ComparisonOutcome(str, enum.Enum):
    """5-value closed taxonomy for the aggregator's verdict.

    Every aggregator call maps to exactly one of these — operators
    branch on the enum value, never on free-form fields.
    """

    ESTABLISHED = "established"
    """Empirical baseline established. Recurrence-reduction-pct
    exceeds the configured threshold; baseline quality is
    sufficient. The policy under test is empirically supported.
    """

    INSUFFICIENT_DATA = "insufficient_data"
    """Not enough replays to establish a baseline. Caller should
    accumulate more sessions before re-asking the empirical
    question. Distinct from FAILED — the input was valid, just
    bounded too tightly."""

    DEGRADED = "degraded"
    """Counterfactuals were consistently worse than originals
    (DIVERGED_WORSE rate exceeds degradation_threshold_pct). A
    strong signal that the policy under test should NOT graduate
    — replay actively contradicts the prevention hypothesis."""

    DISABLED = "disabled"
    """Master flag or comparator sub-flag is off. No aggregation
    performed."""

    FAILED = "failed"
    """Input was non-iterable, raised on iteration, or otherwise
    not a valid verdict stream. Distinct from INSUFFICIENT_DATA
    (which had a valid empty/short stream)."""


class BaselineQuality(str, enum.Enum):
    """5-value closed taxonomy for empirical evidence strength.

    Independent from ComparisonOutcome — a baseline can be HIGH
    quality and still report DEGRADED outcome (lots of solid
    evidence that the policy makes things worse). Operators see
    both axes independently."""

    HIGH = "high"
    """N >= baseline_high_n_threshold(). Recurrence-reduction-pct
    is statistically meaningful at the configured threshold."""

    MEDIUM = "medium"
    """baseline_medium_n_threshold() <= N < HIGH threshold.
    Suggestive but not strongly supported."""

    LOW = "low"
    """baseline_low_n_threshold() <= N < MEDIUM threshold. Early
    signal; treat empirical claims as preliminary."""

    INSUFFICIENT = "insufficient"
    """N < baseline_low_n_threshold(). Not enough data to ground
    any empirical claim."""

    FAILED = "failed"
    """Computation failed (defensive default — should never appear
    on the happy path)."""


# ---------------------------------------------------------------------------
# StampedVerdict — structural MonotonicTighteningVerdict.PASSED stamp
# ---------------------------------------------------------------------------


def _resolve_passed_stamp() -> str:
    """Return the canonical PASSED string from
    ``adaptation.ledger.MonotonicTighteningVerdict``. Falls back
    to literal ``"passed"`` if the import fails. The string is
    stable across the stack — Phase C, Move 6, Priority #1, #2
    all use the same vocabulary so operators can correlate
    cross-file via shared symbols."""
    try:
        from backend.core.ouroboros.governance.adaptation.ledger import (
            MonotonicTighteningVerdict,
        )
        return str(MonotonicTighteningVerdict.PASSED.value)
    except Exception:  # noqa: BLE001 — defensive
        return "passed"


@dataclass(frozen=True)
class StampedVerdict:
    """One ReplayVerdict wrapped with its monotonic-tightening stamp.

    Replay is observational not prescriptive — the stamp is ALWAYS
    PASSED by construction. The structural wrapper (versus a string
    field on ReplayVerdict.detail) lets Slice 4's history store
    serialize the stamp as a typed field and lets operators query
    by stamp value via the IDE GET surfaces (Slice 5b).

    Carries an optional ``cluster_kind`` slot so Slice 4 can
    populate from Causality DAG's cluster_kind heuristic without
    requiring the comparator to import causality_dag (the import
    happens at the call site)."""
    verdict: ReplayVerdict
    tightening: str
    cluster_kind: str = ""
    schema_version: str = COUNTERFACTUAL_COMPARATOR_SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Serialize for Slice 4's history store + SSE event."""
        return {
            "verdict": (
                self.verdict.to_dict()
                if isinstance(self.verdict, ReplayVerdict)
                else None
            ),
            "tightening": str(self.tightening),
            "cluster_kind": str(self.cluster_kind or ""),
            "schema_version": str(self.schema_version),
        }


def stamp_verdict(
    verdict: ReplayVerdict,
    *,
    cluster_kind: str = "",
) -> StampedVerdict:
    """Wrap a ``ReplayVerdict`` with its canonical PASSED stamp.

    Replay is observational — the stamp NEVER varies; it's a
    structural marker that the verdict came from the replay path
    (which by AST-pinned construction cannot loosen any gate).

    NEVER raises. Garbage input → still produces a StampedVerdict
    with stamp=PASSED and the input wrapped as-is — caller can
    detect via ``isinstance(verdict, ReplayVerdict)``."""
    try:
        return StampedVerdict(
            verdict=verdict,
            tightening=_resolve_passed_stamp(),
            cluster_kind=str(cluster_kind or "").strip(),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[replay_comparator] stamp_verdict failed: %s", exc)
        # Fallback — produces a stamped wrapper even on garbage
        return StampedVerdict(
            verdict=verdict,
            tightening="passed",
            cluster_kind="",
        )


# ---------------------------------------------------------------------------
# Per-verdict classification — pure decision over BranchVerdict
# ---------------------------------------------------------------------------


def _classify_verdict(verdict: ReplayVerdict) -> str:
    """Map one ReplayVerdict to one of:

      * ``"prevention"`` — DIVERGED_BETTER (original > counterfactual)
      * ``"regression"`` — DIVERGED_WORSE (counterfactual > original)
      * ``"equivalent"`` — EQUIVALENT (no causal effect)
      * ``"neutral"`` — DIVERGED_NEUTRAL (contradicting axes)
      * ``"non_actionable"`` — outcome is FAILED / PARTIAL / DISABLED
        OR verdict is FAILED → not counted in actionable totals

    NEVER raises. Garbage → ``"non_actionable"``."""
    try:
        if not isinstance(verdict, ReplayVerdict):
            return "non_actionable"
        if verdict.outcome is not ReplayOutcome.SUCCESS:
            return "non_actionable"
        if verdict.verdict is BranchVerdict.DIVERGED_BETTER:
            return "prevention"
        if verdict.verdict is BranchVerdict.DIVERGED_WORSE:
            return "regression"
        if verdict.verdict is BranchVerdict.EQUIVALENT:
            return "equivalent"
        if verdict.verdict is BranchVerdict.DIVERGED_NEUTRAL:
            return "neutral"
        return "non_actionable"
    except Exception:  # noqa: BLE001 — defensive
        return "non_actionable"


# ---------------------------------------------------------------------------
# RecurrenceReductionStats — frozen aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecurrenceReductionStats:
    """Bounded recurrence-reduction statistics over a verdict stream.

    Computed by ``compute_recurrence_reduction_stats``. Pure
    aggregate — no I/O, no observers, no side effects. Slice 4's
    history store will project these onto a JSON-friendly shape
    via ``to_dict()``.

    Field semantics:

      * ``total_replays`` — every ReplayVerdict seen, including
        non-actionable ones.
      * ``actionable_count`` — verdicts where outcome=SUCCESS AND
        verdict is one of {DIVERGED_BETTER, DIVERGED_WORSE,
        EQUIVALENT, DIVERGED_NEUTRAL}. Recurrence-reduction-pct is
        computed over THIS denominator, not total_replays.
      * ``recurrence_reduction_pct`` — (prevention_count /
        actionable_count) * 100, in [0.0, 100.0]. Zero actionables
        → 0.0 (caller should consult baseline_quality).
      * ``prevention_evidence_rate`` — (prevention_count /
        total_replays) — the fraction of all replays that found
        prevention. Useful when caller wants the strict-strict
        signal.
      * ``regression_rate`` — (regression_count / actionable_count).
        High value triggers DEGRADED outcome.
      * ``baseline_quality`` — derived from total_replays vs the
        N thresholds.
      * ``postmortems_in_originals`` / ``postmortems_in_counterfactuals``
        — total postmortem record counts across all branch
        snapshots in the stream. Derived from BranchSnapshot
        ``postmortem_records`` tuples.
      * ``postmortems_prevented`` — max(0, originals -
        counterfactuals). Slice 4 uses this for the "concrete
        prevention evidence" SSE event payload.
    """
    total_replays: int = 0
    actionable_count: int = 0
    prevention_count: int = 0
    regression_count: int = 0
    equivalent_count: int = 0
    neutral_count: int = 0
    non_actionable_count: int = 0

    success_outcome_count: int = 0
    partial_outcome_count: int = 0
    diverged_outcome_count: int = 0
    failed_outcome_count: int = 0
    disabled_outcome_count: int = 0

    postmortems_in_originals: int = 0
    postmortems_in_counterfactuals: int = 0
    postmortems_prevented: int = 0

    recurrence_reduction_pct: float = 0.0
    prevention_evidence_rate: float = 0.0
    regression_rate: float = 0.0

    baseline_quality: BaselineQuality = BaselineQuality.INSUFFICIENT
    schema_version: str = COUNTERFACTUAL_COMPARATOR_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "total_replays": int(self.total_replays),
            "actionable_count": int(self.actionable_count),
            "prevention_count": int(self.prevention_count),
            "regression_count": int(self.regression_count),
            "equivalent_count": int(self.equivalent_count),
            "neutral_count": int(self.neutral_count),
            "non_actionable_count": int(self.non_actionable_count),
            "success_outcome_count": int(self.success_outcome_count),
            "partial_outcome_count": int(self.partial_outcome_count),
            "diverged_outcome_count": int(self.diverged_outcome_count),
            "failed_outcome_count": int(self.failed_outcome_count),
            "disabled_outcome_count": int(self.disabled_outcome_count),
            "postmortems_in_originals": int(self.postmortems_in_originals),
            "postmortems_in_counterfactuals": int(
                self.postmortems_in_counterfactuals,
            ),
            "postmortems_prevented": int(self.postmortems_prevented),
            "recurrence_reduction_pct": float(self.recurrence_reduction_pct),
            "prevention_evidence_rate": float(self.prevention_evidence_rate),
            "regression_rate": float(self.regression_rate),
            "baseline_quality": str(self.baseline_quality.value),
            "schema_version": str(self.schema_version),
        }


def compute_baseline_quality(
    total_replays: int,
) -> BaselineQuality:
    """Map a replay count to its quality bucket.

    Resolution order (env-driven, no hardcoded magic):
      * total >= HIGH_N → HIGH
      * total >= MEDIUM_N → MEDIUM
      * total >= LOW_N → LOW
      * else → INSUFFICIENT

    NEVER raises. Negative counts → INSUFFICIENT."""
    try:
        n = max(0, int(total_replays))
        high = baseline_high_n_threshold()
        medium = baseline_medium_n_threshold()
        low = baseline_low_n_threshold()

        # Order-resolve in case operator misconfigures the
        # thresholds (HIGH < MEDIUM, etc.) — comparator behaves
        # gracefully even with reversed env knobs.
        sorted_thresholds = sorted(
            [(high, BaselineQuality.HIGH),
             (medium, BaselineQuality.MEDIUM),
             (low, BaselineQuality.LOW)],
            key=lambda x: x[0], reverse=True,
        )
        for threshold, quality in sorted_thresholds:
            if n >= threshold:
                return quality
        return BaselineQuality.INSUFFICIENT
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_comparator] compute_baseline_quality failed: %s",
            exc,
        )
        return BaselineQuality.FAILED


def compute_recurrence_reduction_stats(
    verdicts: Iterable[ReplayVerdict],
) -> RecurrenceReductionStats:
    """Walk a verdict stream and produce one aggregate.

    Pure — no I/O, no side effects. Generator-friendly (consumed
    once). NEVER raises — exceptions during iteration are caught
    per-item and the offending verdict is counted as
    non_actionable.

    Iteration is bounded by the caller (the comparator does not
    impose a global cap; Slice 4's history store applies its own
    rotation/truncation discipline)."""

    # Initialize all counters at zero.
    total = 0
    success = 0
    partial = 0
    diverged_outcome = 0
    failed = 0
    disabled = 0
    actionable = 0
    prevention = 0
    regression = 0
    equivalent = 0
    neutral = 0
    non_actionable = 0
    postmortems_orig = 0
    postmortems_cf = 0

    try:
        if verdicts is None:
            return RecurrenceReductionStats(
                baseline_quality=compute_baseline_quality(0),
            )
        for raw in verdicts:
            try:
                total += 1
                if not isinstance(raw, ReplayVerdict):
                    non_actionable += 1
                    failed += 1
                    continue

                # Outcome bucket
                outcome = raw.outcome
                if outcome is ReplayOutcome.SUCCESS:
                    success += 1
                elif outcome is ReplayOutcome.PARTIAL:
                    partial += 1
                elif outcome is ReplayOutcome.DIVERGED:
                    diverged_outcome += 1
                elif outcome is ReplayOutcome.FAILED:
                    failed += 1
                elif outcome is ReplayOutcome.DISABLED:
                    disabled += 1

                # Actionability
                cls = _classify_verdict(raw)
                if cls == "prevention":
                    prevention += 1
                    actionable += 1
                elif cls == "regression":
                    regression += 1
                    actionable += 1
                elif cls == "equivalent":
                    equivalent += 1
                    actionable += 1
                elif cls == "neutral":
                    neutral += 1
                    actionable += 1
                else:
                    non_actionable += 1

                # Postmortem accounting — sum counts across both
                # branches. Defensive iteration over potentially
                # missing branch slots.
                try:
                    if raw.original_branch is not None:
                        pm = raw.original_branch.postmortem_records
                        if pm:
                            postmortems_orig += len(pm)
                except Exception:  # noqa: BLE001 — defensive
                    pass
                try:
                    if raw.counterfactual_branch is not None:
                        pm = raw.counterfactual_branch.postmortem_records
                        if pm:
                            postmortems_cf += len(pm)
                except Exception:  # noqa: BLE001 — defensive
                    pass
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[replay_comparator] item iteration failed: %s",
                    exc,
                )
                non_actionable += 1
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_comparator] iteration failed: %s — partial stats",
            exc,
        )

    # Derived metrics.
    rec_red_pct = 0.0
    prev_rate = 0.0
    reg_rate = 0.0
    if actionable > 0:
        try:
            rec_red_pct = (prevention / actionable) * 100.0
            reg_rate = (regression / actionable) * 100.0
        except Exception:  # noqa: BLE001 — defensive
            rec_red_pct = 0.0
            reg_rate = 0.0
    if total > 0:
        try:
            prev_rate = prevention / total
        except Exception:  # noqa: BLE001 — defensive
            prev_rate = 0.0

    pm_prevented = max(0, postmortems_orig - postmortems_cf)
    quality = compute_baseline_quality(total)

    return RecurrenceReductionStats(
        total_replays=total,
        actionable_count=actionable,
        prevention_count=prevention,
        regression_count=regression,
        equivalent_count=equivalent,
        neutral_count=neutral,
        non_actionable_count=non_actionable,
        success_outcome_count=success,
        partial_outcome_count=partial,
        diverged_outcome_count=diverged_outcome,
        failed_outcome_count=failed,
        disabled_outcome_count=disabled,
        postmortems_in_originals=postmortems_orig,
        postmortems_in_counterfactuals=postmortems_cf,
        postmortems_prevented=pm_prevented,
        recurrence_reduction_pct=round(rec_red_pct, 4),
        prevention_evidence_rate=round(prev_rate, 4),
        regression_rate=round(reg_rate, 4),
        baseline_quality=quality,
    )


# ---------------------------------------------------------------------------
# ComparisonReport — top-level frozen aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComparisonReport:
    """Top-level aggregate produced by ``compare_replay_history``.

    The comparator's ONE public output. Caller branches on
    ``outcome`` (closed taxonomy), inspects ``stats`` for raw
    counts + percentages, persists / publishes via Slice 4's
    history store + SSE event."""
    outcome: ComparisonOutcome
    stats: RecurrenceReductionStats
    tightening: str
    detail: str = ""
    schema_version: str = COUNTERFACTUAL_COMPARATOR_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "outcome": str(self.outcome.value),
            "stats": self.stats.to_dict(),
            "tightening": str(self.tightening),
            "detail": str(self.detail or ""),
            "schema_version": str(self.schema_version),
        }


def compose_aggregated_detail(
    stats: RecurrenceReductionStats,
) -> str:
    """Render a one-line operator-readable summary of the stats.

    Same dense-token shape as LastSessionSummary's render path so
    operators can grep the same tokens across observability
    artifacts. NEVER raises — garbage stats → empty string."""
    try:
        return (
            f"replays={stats.total_replays} "
            f"actionable={stats.actionable_count} "
            f"prev={stats.prevention_count} "
            f"reg={stats.regression_count} "
            f"eq={stats.equivalent_count} "
            f"neutral={stats.neutral_count} "
            f"rec_red={stats.recurrence_reduction_pct:.2f}% "
            f"reg_rate={stats.regression_rate:.2f}% "
            f"pm_prevented={stats.postmortems_prevented} "
            f"quality={stats.baseline_quality.value}"
        )
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# Public surface — compare_replay_history
# ---------------------------------------------------------------------------


def compare_replay_history(
    verdicts: Iterable[ReplayVerdict],
    *,
    enabled_override: Optional[bool] = None,
) -> ComparisonReport:
    """Aggregate a stream of ReplayVerdicts into one ComparisonReport.

    Decision tree (deterministic, closed-taxonomy):

      1. ``enabled_override is False`` OR
         ``not counterfactual_replay_enabled()`` OR
         ``not comparator_enabled()`` (when override is None) →
         ``DISABLED``.
      2. ``verdicts is None`` or non-iterable → ``FAILED``.
      3. Compute stats over the stream.
      4. ``stats.total_replays == 0`` → ``INSUFFICIENT_DATA``.
      5. ``stats.baseline_quality == INSUFFICIENT`` →
         ``INSUFFICIENT_DATA``.
      6. ``regression_rate >= degradation_threshold_pct`` →
         ``DEGRADED``.
      7. ``recurrence_reduction_pct >= prevention_threshold_pct`` →
         ``ESTABLISHED``.
      8. Else → ``INSUFFICIENT_DATA`` (sufficient quality but
         prevention rate didn't clear the bar — caller knows the
         policy didn't establish under current threshold).

    Steps 4-8 each compose a deterministic ``detail`` string for
    observability.

    NEVER raises."""
    try:
        # 1. Flag resolution.
        if enabled_override is False:
            return _disabled_report(
                detail="enabled_override=false",
            )
        if enabled_override is None:
            if not counterfactual_replay_enabled():
                return _disabled_report(
                    detail="counterfactual_replay_master_flag_off",
                )
            if not comparator_enabled():
                return _disabled_report(
                    detail="replay_comparator_sub_flag_off",
                )

        # 2. Validate iterability. Strings + bytes are technically
        # iterable but a string-as-verdict-stream is always a caller
        # bug — treat as FAILED so the mistake surfaces immediately
        # rather than silently iterating chars.
        if verdicts is None:
            return _failed_report(detail="verdicts=None")
        if isinstance(verdicts, (str, bytes, bytearray)):
            return _failed_report(
                detail=f"string_like_input:{type(verdicts).__name__}",
            )
        try:
            iter(verdicts)
        except TypeError:
            return _failed_report(
                detail=f"non_iterable:{type(verdicts).__name__}",
            )

        # 3. Compute stats.
        stats = compute_recurrence_reduction_stats(verdicts)

        # 4-8. Outcome resolution.
        if stats.total_replays == 0:
            return _report_with_detail(
                outcome=ComparisonOutcome.INSUFFICIENT_DATA,
                stats=stats,
                detail="empty_verdict_stream",
            )

        if stats.baseline_quality is BaselineQuality.INSUFFICIENT:
            return _report_with_detail(
                outcome=ComparisonOutcome.INSUFFICIENT_DATA,
                stats=stats,
                detail=(
                    f"baseline_quality=insufficient "
                    f"total_replays={stats.total_replays} "
                    f"low_n={baseline_low_n_threshold()}"
                ),
            )

        deg_thr = degradation_threshold_pct()
        if stats.regression_rate >= deg_thr:
            return _report_with_detail(
                outcome=ComparisonOutcome.DEGRADED,
                stats=stats,
                detail=(
                    f"regression_rate={stats.regression_rate:.2f} "
                    f">= threshold={deg_thr:.2f}"
                ),
            )

        prev_thr = prevention_threshold_pct()
        if stats.recurrence_reduction_pct >= prev_thr:
            return _report_with_detail(
                outcome=ComparisonOutcome.ESTABLISHED,
                stats=stats,
                detail=(
                    f"recurrence_reduction_pct="
                    f"{stats.recurrence_reduction_pct:.2f} "
                    f">= threshold={prev_thr:.2f} "
                    f"baseline={stats.baseline_quality.value}"
                ),
            )

        return _report_with_detail(
            outcome=ComparisonOutcome.INSUFFICIENT_DATA,
            stats=stats,
            detail=(
                f"prevention_below_threshold "
                f"rec_red={stats.recurrence_reduction_pct:.2f} "
                f"< prev_thr={prev_thr:.2f}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_comparator] compare_replay_history failed: %s",
            exc,
        )
        return _failed_report(detail=f"comparator_error:{type(exc).__name__}")


# ---------------------------------------------------------------------------
# Private report constructors — keep stamping consistent
# ---------------------------------------------------------------------------


def _empty_stats() -> RecurrenceReductionStats:
    """Empty stats with INSUFFICIENT baseline. Used by error paths."""
    return RecurrenceReductionStats(
        baseline_quality=BaselineQuality.INSUFFICIENT,
    )


def _disabled_report(*, detail: str) -> ComparisonReport:
    return ComparisonReport(
        outcome=ComparisonOutcome.DISABLED,
        stats=_empty_stats(),
        tightening=_resolve_passed_stamp(),
        detail=str(detail),
    )


def _failed_report(*, detail: str) -> ComparisonReport:
    return ComparisonReport(
        outcome=ComparisonOutcome.FAILED,
        stats=_empty_stats(),
        tightening=_resolve_passed_stamp(),
        detail=str(detail),
    )


def _report_with_detail(
    *,
    outcome: ComparisonOutcome,
    stats: RecurrenceReductionStats,
    detail: str,
) -> ComparisonReport:
    """Compose a final report. Always stamps PASSED — replay is
    observational by AST-pinned construction."""
    summary = compose_aggregated_detail(stats)
    full_detail = f"{detail} | {summary}" if summary else str(detail)
    return ComparisonReport(
        outcome=outcome,
        stats=stats,
        tightening=_resolve_passed_stamp(),
        detail=full_detail,
    )


# ---------------------------------------------------------------------------
# Cost-contract authority constant (AST-pin target for Slice 5)
# ---------------------------------------------------------------------------


# Surfaced symbol so the AST validator can pin its presence in
# ``shipped_code_invariants``. The token name carries the contract.
COST_CONTRACT_PRESERVED_BY_CONSTRUCTION: bool = True


__all__ = [
    "BaselineQuality",
    "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
    "COUNTERFACTUAL_COMPARATOR_SCHEMA_VERSION",
    "ComparisonOutcome",
    "ComparisonReport",
    "RecurrenceReductionStats",
    "StampedVerdict",
    "baseline_high_n_threshold",
    "baseline_low_n_threshold",
    "baseline_medium_n_threshold",
    "comparator_enabled",
    "compare_replay_history",
    "compose_aggregated_detail",
    "compute_baseline_quality",
    "compute_recurrence_reduction_stats",
    "degradation_threshold_pct",
    "prevention_threshold_pct",
    "stamp_verdict",
]
