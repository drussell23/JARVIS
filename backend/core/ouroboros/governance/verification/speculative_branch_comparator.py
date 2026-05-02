"""Priority #4 Slice 3 — Speculative Branch Tree comparator + aggregator.

The empirical-effectiveness layer for SBT. Slice 1 shipped pure data
+ closed-taxonomy decisions. Slice 2 shipped the async runner. Slice 3
(this module) ships:

  1. **Structural ``MonotonicTighteningVerdict.PASSED`` stamping** of
     every TreeVerdictResult. Replay is observational not prescriptive
     — every stamp is canonically PASSED. Cross-stack vocabulary
     integration: 5 modules now stamp PASSED (Move 6 + Priority #1 +
     Priority #2 + Priority #3 + this).

  2. **Ambiguity-resolution-rate aggregator** over a stream of
     TreeVerdictResults. Counts CONVERGED (resolved) vs DIVERGED
     (genuine ambiguity → escalate) vs INCONCLUSIVE (weak evidence)
     vs TRUNCATED (budget exhausted) vs FAILED (couldn't run).
     Produces the empirical signal that justifies (or contradicts)
     SBT graduation: "does SBT actually resolve ambiguity, or does
     it just burn cost without converging?"

  3. **Closed-taxonomy effectiveness outcomes** so caller code
     branches on the enum:
     ESTABLISHED / INSUFFICIENT_DATA / INEFFECTIVE / DISABLED / FAILED.

  4. **Bounded baseline quality** (HIGH / MEDIUM / LOW / INSUFFICIENT /
     FAILED) — operators see at a glance how much trust to place in
     the resolution-rate number.

Direct-solve principles:

  * **Asynchronous-ready** — pure-data primitives propagate cleanly
    across async boundaries (Slice 4's history store + observer will
    round-trip ``SBTComparisonReport`` through ``asyncio.to_thread``
    + SSE serialization).

  * **Dynamic** — every quality threshold (HIGH/MEDIUM/LOW N,
    resolution + ineffective threshold pct) is env-tunable with
    floor + ceiling clamps. NO hardcoded magic constants.

  * **Adaptive** — empty input → INSUFFICIENT_DATA; mostly-truncated
    input → INEFFECTIVE; sub-flag off → DISABLED; non-iterable input
    → FAILED. Every degenerate case maps to an explicit closed-
    taxonomy outcome rather than an exception.

  * **Intelligent** — ambiguity-resolution-rate is computed over the
    set of ACTIONABLE verdicts (CONVERGED + DIVERGED + INCONCLUSIVE)
    — not over verdicts that couldn't run (TRUNCATED / FAILED).
    This avoids degrading the statistic when a session simply lacked
    the budget. The INEFFECTIVE outcome catches the budget-exhaustion
    case directly.

  * **Robust** — every public function NEVER raises. Pure-data
    aggregator can be called from any context, sync or async.
    Garbage in → ``EffectivenessOutcome.FAILED`` rather than crash.

  * **No hardcoding** — 5-value × 2 closed-taxonomy enums (matching
    Priority #3 Slice 3 vocabulary for cross-module operator
    consistency on the wire format). Per-knob env helpers with
    floor + ceiling clamps mirror Slice 1 + Priority #3 Slice 3
    patterns exactly.

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

  * No exec / eval / compile (mirrors Slice 1 + Slice 2 + Move 6
    + Priority #1/#2/#3 critical safety pin).

  * Reuses ``adaptation.ledger.MonotonicTighteningVerdict`` —
    canonical vocabulary across the stack. Falls back to literal
    string ``"passed"`` if the import fails (defensive — same
    vocabulary regardless).

Master flag (Slice 1): ``JARVIS_SBT_ENABLED``. Comparator sub-flag
(this module): ``JARVIS_SBT_COMPARATOR_ENABLED`` (default-false until
Slice 5 graduation; gates the aggregator's per-call enabled-check
even if Slice 1's master is on — operators can keep schemas live
while disabling aggregation for cost-cap rollback).
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Iterable, Optional

# Slice 1 primitives (pure-stdlib reuse) — only the closed-taxonomy
# enums + the TreeVerdictResult shape. NEVER pulls compute_*
# helpers because Slice 3 is an aggregator, not a per-tree computer.
from backend.core.ouroboros.governance.verification.speculative_branch import (
    BranchOutcome,
    BranchResult,
    TreeVerdict,
    TreeVerdictResult,
    sbt_enabled,
)

logger = logging.getLogger(__name__)


SBT_COMPARATOR_SCHEMA_VERSION: str = "speculative_branch_comparator.1"


# ---------------------------------------------------------------------------
# Sub-flag — independent rollback knob from Slice 1's master
# ---------------------------------------------------------------------------


def comparator_enabled() -> bool:
    """``JARVIS_SBT_COMPARATOR_ENABLED`` — comparator-loader gate.

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit truthy/falsy overrides at call time.

    Default ``false`` until Slice 5 graduation. Independent from
    Slice 1's master so operators can keep the schema live while
    disabling aggregation for a cost-cap rollback or empirical-
    question re-validation.

    Both flags must be ``true`` for ``compare_tree_history`` to
    actually aggregate; if either is off the comparator returns
    ``EffectivenessOutcome.DISABLED`` immediately."""
    raw = os.environ.get(
        "JARVIS_SBT_COMPARATOR_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-off until Slice 5 graduation
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env knobs — every threshold operator-tunable with bounded clamps
# ---------------------------------------------------------------------------


def _read_int_knob(
    name: str, default: int, floor: int, ceiling: int,
) -> int:
    """Read int env knob with floor+ceiling clamping. NEVER raises."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, int(raw)))
    except (TypeError, ValueError):
        return default


def _read_float_knob(
    name: str, default: float, floor: float, ceiling: float,
) -> float:
    """Read float env knob with floor+ceiling clamping. NEVER raises."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, float(raw)))
    except (TypeError, ValueError):
        return default


def baseline_high_n_threshold() -> int:
    """``JARVIS_SBT_BASELINE_HIGH_N`` — minimum trees for
    SBTBaselineQuality.HIGH. Default 30, clamped [10, 1000]."""
    return _read_int_knob(
        "JARVIS_SBT_BASELINE_HIGH_N", 30, 10, 1000,
    )


def baseline_medium_n_threshold() -> int:
    """``JARVIS_SBT_BASELINE_MEDIUM_N`` — minimum trees for
    SBTBaselineQuality.MEDIUM. Default 10, clamped [3, 100]."""
    return _read_int_knob(
        "JARVIS_SBT_BASELINE_MEDIUM_N", 10, 3, 100,
    )


def baseline_low_n_threshold() -> int:
    """``JARVIS_SBT_BASELINE_LOW_N`` — minimum trees for
    SBTBaselineQuality.LOW (anything below → INSUFFICIENT). Default
    3, clamped [1, 50]."""
    return _read_int_knob(
        "JARVIS_SBT_BASELINE_LOW_N", 3, 1, 50,
    )


def resolution_threshold_pct() -> float:
    """``JARVIS_SBT_RESOLUTION_THRESHOLD_PCT`` — minimum
    ambiguity-resolution-pct (CONVERGED / actionable_total) for
    EffectivenessOutcome.ESTABLISHED. Default 50.0, clamped
    [0.0, 100.0]. Operators tighten upward to demand stronger
    empirical evidence before claiming SBT resolves ambiguity
    effectively."""
    return _read_float_knob(
        "JARVIS_SBT_RESOLUTION_THRESHOLD_PCT", 50.0, 0.0, 100.0,
    )


def ineffective_threshold_pct() -> float:
    """``JARVIS_SBT_INEFFECTIVE_THRESHOLD_PCT`` — when
    (TRUNCATED + FAILED) / total exceeds this, the outcome is
    INEFFECTIVE (most trees couldn't even run cleanly → strong
    signal that SBT isn't operating at the configured budget).
    Default 50.0, clamped [0.0, 100.0]."""
    return _read_float_knob(
        "JARVIS_SBT_INEFFECTIVE_THRESHOLD_PCT", 50.0, 0.0, 100.0,
    )


# ---------------------------------------------------------------------------
# Closed-taxonomy enums — J.A.R.M.A.T.R.I.X. discipline
# ---------------------------------------------------------------------------


class EffectivenessOutcome(str, enum.Enum):
    """5-value closed taxonomy for the aggregator's verdict.

    Caller branches on the enum; never on free-form fields."""

    ESTABLISHED = "established"
    """Empirical baseline established. Resolution rate exceeds the
    configured threshold; baseline quality is sufficient. SBT is
    empirically supported as an autonomous-ambiguity-resolution
    primitive."""

    INSUFFICIENT_DATA = "insufficient_data"
    """Not enough trees to establish a baseline (empty stream OR
    below low_n threshold OR resolution rate below threshold but
    with sufficient quality). Caller knows the policy didn't
    establish under current threshold."""

    INEFFECTIVE = "ineffective"
    """Most trees were TRUNCATED or FAILED — SBT is burning budget
    without producing actionable verdicts. Strong signal AGAINST
    graduation OR a signal that the budget caps need tuning."""

    DISABLED = "disabled"
    """Master flag or comparator sub-flag is off. No aggregation
    performed."""

    FAILED = "failed"
    """Input was non-iterable, raised on iteration, or otherwise
    not a valid verdict stream. Distinct from INSUFFICIENT_DATA
    (which had a valid empty/short stream)."""


class SBTBaselineQuality(str, enum.Enum):
    """5-value closed taxonomy for empirical evidence strength.

    Same value vocabulary as Priority #3 Slice 3's BaselineQuality
    so operators see consistent strings across the wire format
    (the Python enum types are namespace-isolated to keep modules
    decoupled)."""

    HIGH = "high"
    """N >= baseline_high_n_threshold(). Resolution rate is
    statistically meaningful at the configured threshold."""

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
# Phase C cross-stack PASSED resolution
# ---------------------------------------------------------------------------


def _resolve_passed_stamp() -> str:
    """Return the canonical PASSED string from
    ``adaptation.ledger.MonotonicTighteningVerdict``. Falls back to
    literal ``"passed"`` if the import fails. The string is stable
    across the stack — Phase C, Move 6, Priority #1/#2/#3, and now
    Priority #4 all use the same vocabulary so operators correlate
    cross-file via shared symbols."""
    try:
        from backend.core.ouroboros.governance.adaptation.ledger import (
            MonotonicTighteningVerdict,
        )
        return str(MonotonicTighteningVerdict.PASSED.value)
    except Exception:  # noqa: BLE001 — defensive
        return "passed"


# ---------------------------------------------------------------------------
# StampedTreeVerdict — structural PASSED stamp
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StampedTreeVerdict:
    """One TreeVerdictResult wrapped with its monotonic-tightening
    stamp.

    Replay/observational architecture — the stamp is ALWAYS PASSED
    by AST-pinned construction. The structural wrapper (versus a
    string field on TreeVerdictResult.detail) lets Slice 4's history
    store serialize the stamp as a typed field and lets operators
    query by stamp value via the IDE GET surfaces (Slice 5b).

    Carries an optional ``cluster_kind`` slot so Slice 4 can
    populate from causality_dag's cluster_kind heuristic without
    requiring the comparator to import causality_dag."""
    verdict: TreeVerdictResult
    tightening: str
    cluster_kind: str = ""
    schema_version: str = SBT_COMPARATOR_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "verdict": (
                self.verdict.to_dict()
                if isinstance(self.verdict, TreeVerdictResult)
                else None
            ),
            "tightening": str(self.tightening),
            "cluster_kind": str(self.cluster_kind or ""),
            "schema_version": str(self.schema_version),
        }


def stamp_tree_verdict(
    verdict: TreeVerdictResult,
    *,
    cluster_kind: str = "",
) -> StampedTreeVerdict:
    """Wrap a TreeVerdictResult with its canonical PASSED stamp.

    SBT is observational — the stamp NEVER varies; it's a
    structural marker that the verdict came from the SBT path
    (which by AST-pinned construction cannot loosen any gate).

    NEVER raises. Garbage input → still produces a StampedTreeVerdict
    with stamp=PASSED and the input wrapped as-is — caller can
    detect via ``isinstance(verdict, TreeVerdictResult)``."""
    try:
        return StampedTreeVerdict(
            verdict=verdict,
            tightening=_resolve_passed_stamp(),
            cluster_kind=str(cluster_kind or "").strip(),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[sbt_comparator] stamp_tree_verdict: %s", exc)
        return StampedTreeVerdict(
            verdict=verdict,
            tightening="passed",
            cluster_kind="",
        )


# ---------------------------------------------------------------------------
# Per-tree classification — pure decision over TreeVerdict
# ---------------------------------------------------------------------------


def _classify_tree(verdict: TreeVerdictResult) -> str:
    """Map one TreeVerdictResult to one of:

      * ``"converged"`` — CONVERGED outcome (resolved cleanly)
      * ``"diverged"`` — DIVERGED outcome (genuine ambiguity)
      * ``"inconclusive"`` — INCONCLUSIVE outcome (evidence weak)
      * ``"truncated"`` — TRUNCATED outcome (budget exhausted)
      * ``"failed"`` — FAILED outcome OR garbage input

    NEVER raises."""
    try:
        if not isinstance(verdict, TreeVerdictResult):
            return "failed"
        outcome = verdict.outcome
        if outcome is TreeVerdict.CONVERGED:
            return "converged"
        if outcome is TreeVerdict.DIVERGED:
            return "diverged"
        if outcome is TreeVerdict.INCONCLUSIVE:
            return "inconclusive"
        if outcome is TreeVerdict.TRUNCATED:
            return "truncated"
        return "failed"
    except Exception:  # noqa: BLE001 — defensive
        return "failed"


# ---------------------------------------------------------------------------
# SBTEffectivenessStats — frozen aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SBTEffectivenessStats:
    """Bounded effectiveness statistics over a TreeVerdictResult
    stream.

    Computed by ``compute_sbt_effectiveness_stats``. Pure aggregate
    — no I/O, no observers, no side effects. Slice 4's history
    store will project these onto a JSON-friendly shape via
    ``to_dict()``.

    Field semantics:
      * ``total_trees`` — every TreeVerdictResult seen, including
        FAILED ones.
      * ``actionable_count`` — verdicts where outcome is one of
        {CONVERGED, DIVERGED, INCONCLUSIVE}. Resolution rate is
        computed over THIS denominator, not total_trees.
        (TRUNCATED + FAILED are excluded — those don't reflect
        SBT's resolution capability, only its budget exhaustion.)
      * ``ambiguity_resolution_rate`` — (converged_count /
        actionable_count) * 100, in [0.0, 100.0]. Zero actionables
        → 0.0 (caller consults baseline_quality).
      * ``escalation_rate`` — (diverged_count / actionable_count)
        — fraction of trees that surfaced genuine ambiguity for
        operator escalation. High escalation isn't necessarily bad
        — it can mean SBT correctly identifies hard cases.
      * ``truncated_failed_rate`` — (TRUNCATED + FAILED) /
        total_trees. High value triggers INEFFECTIVE outcome.
      * ``avg_branches_per_tree`` — efficiency metric (lower =
        cheaper resolution; higher = more thorough exploration).
      * ``avg_evidence_per_tree`` — depth metric across all branches.
      * ``avg_aggregate_confidence`` — mean over CONVERGED trees
        only (the trees that actually picked a winner).
    """
    total_trees: int = 0
    actionable_count: int = 0

    converged_count: int = 0
    diverged_count: int = 0
    inconclusive_count: int = 0
    truncated_count: int = 0
    failed_count: int = 0

    avg_branches_per_tree: float = 0.0
    avg_evidence_per_tree: float = 0.0
    avg_aggregate_confidence: float = 0.0

    ambiguity_resolution_rate: float = 0.0
    escalation_rate: float = 0.0
    truncated_failed_rate: float = 0.0

    baseline_quality: SBTBaselineQuality = SBTBaselineQuality.INSUFFICIENT
    schema_version: str = SBT_COMPARATOR_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "total_trees": int(self.total_trees),
            "actionable_count": int(self.actionable_count),
            "converged_count": int(self.converged_count),
            "diverged_count": int(self.diverged_count),
            "inconclusive_count": int(self.inconclusive_count),
            "truncated_count": int(self.truncated_count),
            "failed_count": int(self.failed_count),
            "avg_branches_per_tree": float(self.avg_branches_per_tree),
            "avg_evidence_per_tree": float(self.avg_evidence_per_tree),
            "avg_aggregate_confidence": float(
                self.avg_aggregate_confidence,
            ),
            "ambiguity_resolution_rate": float(
                self.ambiguity_resolution_rate,
            ),
            "escalation_rate": float(self.escalation_rate),
            "truncated_failed_rate": float(self.truncated_failed_rate),
            "baseline_quality": str(self.baseline_quality.value),
            "schema_version": str(self.schema_version),
        }


def compute_baseline_quality(
    total_trees: int,
) -> SBTBaselineQuality:
    """Map a tree count to its quality bucket.

    Resolution order (env-driven, no hardcoded magic):
      * total >= HIGH_N → HIGH
      * total >= MEDIUM_N → MEDIUM
      * total >= LOW_N → LOW
      * else → INSUFFICIENT

    NEVER raises. Negative counts → INSUFFICIENT."""
    try:
        n = max(0, int(total_trees))
        high = baseline_high_n_threshold()
        medium = baseline_medium_n_threshold()
        low = baseline_low_n_threshold()

        # Order-resolve in case operator misconfigures thresholds
        # (HIGH < MEDIUM, etc.) — comparator behaves gracefully.
        sorted_thresholds = sorted(
            [(high, SBTBaselineQuality.HIGH),
             (medium, SBTBaselineQuality.MEDIUM),
             (low, SBTBaselineQuality.LOW)],
            key=lambda x: x[0], reverse=True,
        )
        for threshold, quality in sorted_thresholds:
            if n >= threshold:
                return quality
        return SBTBaselineQuality.INSUFFICIENT
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[sbt_comparator] compute_baseline_quality: %s", exc,
        )
        return SBTBaselineQuality.FAILED


def compute_sbt_effectiveness_stats(
    verdicts: Iterable[TreeVerdictResult],
) -> SBTEffectivenessStats:
    """Walk a verdict stream and produce one aggregate.

    Pure — no I/O, no side effects. Generator-friendly (consumed
    once). NEVER raises — exceptions during iteration are caught
    per-item and the offending verdict is counted as FAILED.

    Iteration is bounded by the caller (the comparator does not
    impose a global cap; Slice 4's history store applies its own
    rotation/truncation discipline)."""
    total = 0
    converged = 0
    diverged = 0
    inconclusive = 0
    truncated = 0
    failed = 0

    total_branches = 0
    total_evidence = 0
    converged_confidence_sum = 0.0
    converged_with_confidence = 0

    try:
        if verdicts is None:
            return SBTEffectivenessStats(
                baseline_quality=compute_baseline_quality(0),
            )
        for raw in verdicts:
            try:
                total += 1
                if not isinstance(raw, TreeVerdictResult):
                    failed += 1
                    continue

                cls = _classify_tree(raw)
                if cls == "converged":
                    converged += 1
                    if raw.aggregate_confidence > 0.0:
                        converged_confidence_sum += float(
                            raw.aggregate_confidence,
                        )
                        converged_with_confidence += 1
                elif cls == "diverged":
                    diverged += 1
                elif cls == "inconclusive":
                    inconclusive += 1
                elif cls == "truncated":
                    truncated += 1
                else:
                    failed += 1

                # Branch + evidence counters across every tree.
                try:
                    branches = raw.branches or ()
                    total_branches += len(branches)
                    for b in branches:
                        if isinstance(b, BranchResult):
                            total_evidence += len(b.evidence)
                except Exception:  # noqa: BLE001 — defensive
                    pass
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[sbt_comparator] item iteration: %s", exc,
                )
                failed += 1
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[sbt_comparator] iteration: %s — partial stats", exc,
        )

    # Derived metrics.
    actionable = converged + diverged + inconclusive
    res_rate = 0.0
    esc_rate = 0.0
    if actionable > 0:
        try:
            res_rate = (converged / actionable) * 100.0
            esc_rate = (diverged / actionable) * 100.0
        except Exception:  # noqa: BLE001 — defensive
            res_rate = 0.0
            esc_rate = 0.0

    tf_rate = 0.0
    if total > 0:
        try:
            tf_rate = ((truncated + failed) / total) * 100.0
        except Exception:  # noqa: BLE001 — defensive
            tf_rate = 0.0

    avg_branches = 0.0
    avg_evidence = 0.0
    if total > 0:
        try:
            avg_branches = total_branches / total
            avg_evidence = total_evidence / total
        except Exception:  # noqa: BLE001 — defensive
            avg_branches = 0.0
            avg_evidence = 0.0

    avg_conf = 0.0
    if converged_with_confidence > 0:
        try:
            avg_conf = (
                converged_confidence_sum / converged_with_confidence
            )
        except Exception:  # noqa: BLE001 — defensive
            avg_conf = 0.0

    quality = compute_baseline_quality(total)

    return SBTEffectivenessStats(
        total_trees=total,
        actionable_count=actionable,
        converged_count=converged,
        diverged_count=diverged,
        inconclusive_count=inconclusive,
        truncated_count=truncated,
        failed_count=failed,
        avg_branches_per_tree=round(avg_branches, 4),
        avg_evidence_per_tree=round(avg_evidence, 4),
        avg_aggregate_confidence=round(avg_conf, 4),
        ambiguity_resolution_rate=round(res_rate, 4),
        escalation_rate=round(esc_rate, 4),
        truncated_failed_rate=round(tf_rate, 4),
        baseline_quality=quality,
    )


# ---------------------------------------------------------------------------
# SBTComparisonReport — top-level frozen aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SBTComparisonReport:
    """Top-level aggregate produced by ``compare_tree_history``.

    The comparator's ONE public output. Caller branches on
    ``outcome`` (closed taxonomy), inspects ``stats`` for raw
    counts + percentages, persists / publishes via Slice 4's
    history store + SSE event."""
    outcome: EffectivenessOutcome
    stats: SBTEffectivenessStats
    tightening: str
    detail: str = ""
    schema_version: str = SBT_COMPARATOR_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "outcome": str(self.outcome.value),
            "stats": self.stats.to_dict(),
            "tightening": str(self.tightening),
            "detail": str(self.detail or ""),
            "schema_version": str(self.schema_version),
        }


def compose_aggregated_detail(
    stats: SBTEffectivenessStats,
) -> str:
    """Render a one-line operator-readable summary of the stats.

    Same dense-token shape as Priority #3's compose_aggregated_detail
    so operators can grep the same tokens across observability
    artifacts. NEVER raises — garbage stats → empty string."""
    try:
        return (
            f"trees={stats.total_trees} "
            f"actionable={stats.actionable_count} "
            f"converged={stats.converged_count} "
            f"diverged={stats.diverged_count} "
            f"inconclusive={stats.inconclusive_count} "
            f"truncated={stats.truncated_count} "
            f"failed={stats.failed_count} "
            f"res_rate={stats.ambiguity_resolution_rate:.2f}% "
            f"esc_rate={stats.escalation_rate:.2f}% "
            f"tf_rate={stats.truncated_failed_rate:.2f}% "
            f"avg_branches={stats.avg_branches_per_tree:.2f} "
            f"avg_evidence={stats.avg_evidence_per_tree:.2f} "
            f"avg_conf={stats.avg_aggregate_confidence:.3f} "
            f"quality={stats.baseline_quality.value}"
        )
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# Public surface — compare_tree_history
# ---------------------------------------------------------------------------


def compare_tree_history(
    verdicts: Iterable[TreeVerdictResult],
    *,
    enabled_override: Optional[bool] = None,
) -> SBTComparisonReport:
    """Aggregate a stream of TreeVerdictResults into one
    SBTComparisonReport.

    Decision tree (deterministic, closed-taxonomy):

      1. ``enabled_override is False`` OR
         ``not sbt_enabled()`` OR
         ``not comparator_enabled()`` (when override is None) →
         ``DISABLED``.
      2. ``verdicts is None`` or string-like or non-iterable →
         ``FAILED``.
      3. Compute stats over the stream.
      4. ``stats.total_trees == 0`` → ``INSUFFICIENT_DATA``.
      5. ``stats.baseline_quality == INSUFFICIENT`` →
         ``INSUFFICIENT_DATA``.
      6. ``truncated_failed_rate >= ineffective_threshold_pct`` →
         ``INEFFECTIVE``.
      7. ``ambiguity_resolution_rate >= resolution_threshold_pct`` →
         ``ESTABLISHED``.
      8. Else → ``INSUFFICIENT_DATA`` (sufficient quality but
         resolution rate didn't clear the bar).

    NEVER raises."""
    try:
        # 1. Flag resolution.
        if enabled_override is False:
            return _disabled_report(detail="enabled_override=false")
        if enabled_override is None:
            if not sbt_enabled():
                return _disabled_report(
                    detail="sbt_master_flag_off",
                )
            if not comparator_enabled():
                return _disabled_report(
                    detail="sbt_comparator_sub_flag_off",
                )

        # 2. Validate iterability. Strings + bytes are technically
        # iterable but a string-as-verdict-stream is always a caller
        # bug — treat as FAILED so the mistake surfaces immediately.
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
        stats = compute_sbt_effectiveness_stats(verdicts)

        # 4-8. Outcome resolution.
        if stats.total_trees == 0:
            return _report_with_detail(
                outcome=EffectivenessOutcome.INSUFFICIENT_DATA,
                stats=stats,
                detail="empty_verdict_stream",
            )

        if stats.baseline_quality is SBTBaselineQuality.INSUFFICIENT:
            return _report_with_detail(
                outcome=EffectivenessOutcome.INSUFFICIENT_DATA,
                stats=stats,
                detail=(
                    f"baseline_quality=insufficient "
                    f"total_trees={stats.total_trees} "
                    f"low_n={baseline_low_n_threshold()}"
                ),
            )

        ineff_thr = ineffective_threshold_pct()
        if stats.truncated_failed_rate >= ineff_thr:
            return _report_with_detail(
                outcome=EffectivenessOutcome.INEFFECTIVE,
                stats=stats,
                detail=(
                    f"truncated_failed_rate="
                    f"{stats.truncated_failed_rate:.2f} "
                    f">= threshold={ineff_thr:.2f}"
                ),
            )

        res_thr = resolution_threshold_pct()
        if stats.ambiguity_resolution_rate >= res_thr:
            return _report_with_detail(
                outcome=EffectivenessOutcome.ESTABLISHED,
                stats=stats,
                detail=(
                    f"ambiguity_resolution_rate="
                    f"{stats.ambiguity_resolution_rate:.2f} "
                    f">= threshold={res_thr:.2f} "
                    f"baseline={stats.baseline_quality.value}"
                ),
            )

        return _report_with_detail(
            outcome=EffectivenessOutcome.INSUFFICIENT_DATA,
            stats=stats,
            detail=(
                f"resolution_below_threshold "
                f"res_rate={stats.ambiguity_resolution_rate:.2f} "
                f"< res_thr={res_thr:.2f}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[sbt_comparator] compare_tree_history: %s", exc,
        )
        return _failed_report(
            detail=f"comparator_error:{type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# Private report constructors — keep stamping consistent
# ---------------------------------------------------------------------------


def _empty_stats() -> SBTEffectivenessStats:
    return SBTEffectivenessStats(
        baseline_quality=SBTBaselineQuality.INSUFFICIENT,
    )


def _disabled_report(*, detail: str) -> SBTComparisonReport:
    return SBTComparisonReport(
        outcome=EffectivenessOutcome.DISABLED,
        stats=_empty_stats(),
        tightening=_resolve_passed_stamp(),
        detail=str(detail),
    )


def _failed_report(*, detail: str) -> SBTComparisonReport:
    return SBTComparisonReport(
        outcome=EffectivenessOutcome.FAILED,
        stats=_empty_stats(),
        tightening=_resolve_passed_stamp(),
        detail=str(detail),
    )


def _report_with_detail(
    *,
    outcome: EffectivenessOutcome,
    stats: SBTEffectivenessStats,
    detail: str,
) -> SBTComparisonReport:
    """Compose a final report. Always stamps PASSED — SBT is
    observational by AST-pinned construction."""
    summary = compose_aggregated_detail(stats)
    full_detail = f"{detail} | {summary}" if summary else str(detail)
    return SBTComparisonReport(
        outcome=outcome,
        stats=stats,
        tightening=_resolve_passed_stamp(),
        detail=full_detail,
    )


# ---------------------------------------------------------------------------
# Cost-contract authority constant (AST-pin target for Slice 5)
# ---------------------------------------------------------------------------


COST_CONTRACT_PRESERVED_BY_CONSTRUCTION: bool = True


__all__ = [
    "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
    "EffectivenessOutcome",
    "SBTBaselineQuality",
    "SBTComparisonReport",
    "SBTEffectivenessStats",
    "SBT_COMPARATOR_SCHEMA_VERSION",
    "StampedTreeVerdict",
    "baseline_high_n_threshold",
    "baseline_low_n_threshold",
    "baseline_medium_n_threshold",
    "comparator_enabled",
    "compare_tree_history",
    "compose_aggregated_detail",
    "compute_baseline_quality",
    "compute_sbt_effectiveness_stats",
    "ineffective_threshold_pct",
    "resolution_threshold_pct",
    "stamp_tree_verdict",
]
