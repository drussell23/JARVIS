"""Priority #5 Slice 3 — CIGW comparator + aggregator.

The empirical-effectiveness layer for CIGW. Slice 1 shipped pure
data + closed-taxonomy decisions. Slice 2 shipped the async
collectors + on-APPLY hook. Slice 3 (this module) ships:

  1. **Structural ``MonotonicTighteningVerdict.PASSED`` stamping**
    of every GradientReport. CIGW is observational not prescriptive
    — every stamp is canonically PASSED. Cross-stack vocabulary
    integration: 6 modules now stamp PASSED (Move 6 + Priority #1 +
    Priority #2 + Priority #3 + Priority #4 + this).

  2. **Gradient-effectiveness aggregator** over a stream of
     GradientReports. Counts STABLE (no signal) vs DRIFTING (early
     warning) vs BREACHED (operator-action territory) vs FAILED
     (couldn't compute). Produces the empirical signal that
     justifies (or contradicts) CIGW graduation: "is the codebase
     drifting structurally, or holding steady?"

  3. **Closed-taxonomy effectiveness outcomes**: HEALTHY /
     INSUFFICIENT_DATA / DEGRADED / DISABLED / FAILED. DEGRADED
     covers BOTH drift-rate-above-threshold AND breach-rate-above-
     threshold — the per-severity stats in the report give
     operators the fine-grain signal.

  4. **Bounded baseline quality** (HIGH / MEDIUM / LOW /
     INSUFFICIENT / FAILED) — operators see at a glance how much
     trust to place in the rate numbers.

Direct-solve principles:

  * **Asynchronous-ready** — pure-data primitives propagate cleanly
    across async boundaries (Slice 4's history store + observer
    will round-trip ``CIGWComparisonReport`` through
    ``asyncio.to_thread`` + SSE serialization).

  * **Dynamic** — every quality threshold + healthy/degraded
    threshold is env-tunable with floor + ceiling clamps. NO
    hardcoded magic constants.

  * **Adaptive** — empty input → INSUFFICIENT_DATA; high-breach
    input → DEGRADED; sub-flag off → DISABLED; non-iterable input
    → FAILED. Every degenerate case maps to an explicit closed-
    taxonomy outcome rather than an exception.

  * **Intelligent** — per-severity counts + per-MeasurementKind
    drift summary surface the WHICH (which metrics are drifting,
    at what severity) — not just the WHETHER. Operators see the
    structural drift signature, not just an aggregate %.

  * **Robust** — every public function NEVER raises. Pure-data
    aggregator can be called from any context, sync or async.

  * **No hardcoding** — 5-value × 2 closed-taxonomy enums (matching
    Priority #3/#4 Slice 3 vocabulary for cross-module operator
    consistency on the wire format). Per-knob env helpers with
    floor + ceiling clamps mirror Slice 1 + Priority #3/#4 Slice 3
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
    ``asyncio.to_thread``).

  * No exec / eval / compile (mirrors Slice 1+2 + Move 6 +
    Priority #1-4 critical safety pin).

  * Reuses ``adaptation.ledger.MonotonicTighteningVerdict`` —
    canonical vocabulary across the stack.

Master flag (Slice 1): ``JARVIS_CIGW_ENABLED``. Comparator sub-flag
(this module): ``JARVIS_CIGW_COMPARATOR_ENABLED`` (default-false
until Slice 5 graduation; gates the aggregator's per-call enabled-
check even if Slice 1's master is on)."""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

# Slice 1 reuse — only the closed-taxonomy enums + the GradientReport
# shape. NEVER pulls compute_* helpers because Slice 3 is an
# aggregator, not a per-report computer.
from backend.core.ouroboros.governance.verification.gradient_watcher import (
    GradientOutcome,
    GradientReport,
    GradientSeverity,
    MeasurementKind,
    cigw_enabled,
)

logger = logging.getLogger(__name__)


CIGW_COMPARATOR_SCHEMA_VERSION: str = "gradient_comparator.1"


# ---------------------------------------------------------------------------
# Sub-flag
# ---------------------------------------------------------------------------


def comparator_enabled() -> bool:
    """``JARVIS_CIGW_COMPARATOR_ENABLED`` — comparator-loader gate.

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit truthy/falsy overrides at call time.

    Default ``false`` until Slice 5 graduation. Both flags must be
    ``true`` for ``compare_gradient_history`` to actually aggregate;
    if either is off the comparator returns
    ``CIGWEffectivenessOutcome.DISABLED`` immediately."""
    raw = os.environ.get(
        "JARVIS_CIGW_COMPARATOR_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env knobs
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
    """``JARVIS_CIGW_BASELINE_HIGH_N`` — minimum reports for
    CIGWBaselineQuality.HIGH. Default 30, clamped [10, 1000]."""
    return _read_int_knob(
        "JARVIS_CIGW_BASELINE_HIGH_N", 30, 10, 1000,
    )


def baseline_medium_n_threshold() -> int:
    """``JARVIS_CIGW_BASELINE_MEDIUM_N`` — minimum reports for
    CIGWBaselineQuality.MEDIUM. Default 10, clamped [3, 100]."""
    return _read_int_knob(
        "JARVIS_CIGW_BASELINE_MEDIUM_N", 10, 3, 100,
    )


def baseline_low_n_threshold() -> int:
    """``JARVIS_CIGW_BASELINE_LOW_N`` — minimum reports for
    CIGWBaselineQuality.LOW. Default 3, clamped [1, 50]."""
    return _read_int_knob(
        "JARVIS_CIGW_BASELINE_LOW_N", 3, 1, 50,
    )


def healthy_threshold_pct() -> float:
    """``JARVIS_CIGW_HEALTHY_THRESHOLD_PCT`` — minimum stable_rate
    (STABLE_count / total) for CIGWEffectivenessOutcome.HEALTHY.
    Default 80.0, clamped [0.0, 100.0]. Operators tighten upward
    (e.g., 95.0) to demand near-zero drift before claiming the
    codebase is structurally healthy."""
    return _read_float_knob(
        "JARVIS_CIGW_HEALTHY_THRESHOLD_PCT", 80.0, 0.0, 100.0,
    )


def degraded_threshold_pct() -> float:
    """``JARVIS_CIGW_DEGRADED_THRESHOLD_PCT`` — when (BREACHED +
    DRIFTING) / total exceeds this, the outcome is DEGRADED
    (operator-action signal). Default 30.0, clamped [0.0, 100.0]."""
    return _read_float_knob(
        "JARVIS_CIGW_DEGRADED_THRESHOLD_PCT", 30.0, 0.0, 100.0,
    )


# ---------------------------------------------------------------------------
# Closed-taxonomy enums
# ---------------------------------------------------------------------------


class CIGWEffectivenessOutcome(str, enum.Enum):
    """5-value closed taxonomy for the aggregator's verdict.

    Caller branches on the enum; never on free-form fields. Use
    the per-severity stats in the report for fine-grain operator
    routing."""

    HEALTHY = "healthy"
    """stable_rate ≥ healthy_threshold AND baseline quality
    sufficient. Codebase is structurally holding steady; no
    drift signal worth operator attention."""

    INSUFFICIENT_DATA = "insufficient_data"
    """Not enough reports to establish a baseline (empty stream OR
    below low_n OR drift signals exist but stable_rate hasn't
    crossed the healthy threshold)."""

    DEGRADED = "degraded"
    """drift_rate ≥ degraded_threshold OR any BREACHED reports
    present. Structural shift signal — operator review recommended.
    Per-severity stats distinguish DRIFTING (early-warning) from
    BREACHED (operator-action) within this single outcome."""

    DISABLED = "disabled"
    """Master flag or comparator sub-flag is off. No aggregation
    performed."""

    FAILED = "failed"
    """Input was non-iterable, raised on iteration, or otherwise
    not a valid report stream. Distinct from INSUFFICIENT_DATA
    (which had a valid empty/short stream)."""


class CIGWBaselineQuality(str, enum.Enum):
    """5-value closed taxonomy for empirical evidence strength.

    Same value vocabulary as Priority #3/#4 Slice 3's
    BaselineQuality so operators see consistent strings across the
    wire format."""

    HIGH = "high"
    """N >= baseline_high_n_threshold(). Rate metrics
    statistically meaningful at configured thresholds."""

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
    """Computation failed (defensive default)."""


# ---------------------------------------------------------------------------
# Phase C cross-stack PASSED resolution
# ---------------------------------------------------------------------------


def _resolve_passed_stamp() -> str:
    """Return the canonical PASSED string from
    ``adaptation.ledger.MonotonicTighteningVerdict``. Falls back to
    literal ``"passed"`` if the import fails. The string is stable
    across the stack — Phase C, Move 6, Priority #1/#2/#3/#4, and
    now Priority #5 all use the same vocabulary so operators
    correlate cross-file via shared symbols."""
    try:
        from backend.core.ouroboros.governance.adaptation.ledger import (
            MonotonicTighteningVerdict,
        )
        return str(MonotonicTighteningVerdict.PASSED.value)
    except Exception:  # noqa: BLE001 — defensive
        return "passed"


# ---------------------------------------------------------------------------
# StampedGradientReport — structural PASSED stamp
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StampedGradientReport:
    """One GradientReport wrapped with its monotonic-tightening
    stamp.

    CIGW is observational — the stamp is ALWAYS PASSED by AST-
    pinned construction. The structural wrapper (versus a string
    field on GradientReport.detail) lets Slice 4's history store
    serialize the stamp as a typed field and lets operators query
    by stamp value via the IDE GET surfaces (Slice 5b).

    Carries an optional ``cluster_kind`` slot — Slice 4 may
    populate from causality_dag's cluster_kind heuristic without
    requiring the comparator to import causality_dag."""
    report: GradientReport
    tightening: str
    cluster_kind: str = ""
    schema_version: str = CIGW_COMPARATOR_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "report": (
                self.report.to_dict()
                if isinstance(self.report, GradientReport)
                else None
            ),
            "tightening": str(self.tightening),
            "cluster_kind": str(self.cluster_kind or ""),
            "schema_version": str(self.schema_version),
        }


def stamp_gradient_report(
    report: GradientReport,
    *,
    cluster_kind: str = "",
) -> StampedGradientReport:
    """Wrap a GradientReport with its canonical PASSED stamp.

    CIGW is observational — the stamp NEVER varies; it's a
    structural marker that the report came from the CIGW path
    (which by AST-pinned construction cannot loosen any gate).

    NEVER raises."""
    try:
        return StampedGradientReport(
            report=report,
            tightening=_resolve_passed_stamp(),
            cluster_kind=str(cluster_kind or "").strip(),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[cigw_comparator] stamp_gradient_report: %s", exc)
        return StampedGradientReport(
            report=report,
            tightening="passed",
            cluster_kind="",
        )


# ---------------------------------------------------------------------------
# Per-report classification
# ---------------------------------------------------------------------------


def _classify_report(report: GradientReport) -> str:
    """Map one GradientReport to one of:
      * ``"stable"`` — STABLE outcome
      * ``"drifting"`` — DRIFTING outcome (LOW/MEDIUM severity)
      * ``"breached"`` — BREACHED outcome (HIGH/CRITICAL severity)
      * ``"disabled"`` — DISABLED outcome (per-report master off)
      * ``"failed"`` — FAILED outcome OR garbage input

    NEVER raises."""
    try:
        if not isinstance(report, GradientReport):
            return "failed"
        outcome = report.outcome
        if outcome is GradientOutcome.STABLE:
            return "stable"
        if outcome is GradientOutcome.DRIFTING:
            return "drifting"
        if outcome is GradientOutcome.BREACHED:
            return "breached"
        if outcome is GradientOutcome.DISABLED:
            return "disabled"
        return "failed"
    except Exception:  # noqa: BLE001 — defensive
        return "failed"


# ---------------------------------------------------------------------------
# CIGWAggregateStats
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CIGWAggregateStats:
    """Bounded gradient-aggregation statistics over a GradientReport
    stream.

    Computed by ``compute_cigw_aggregate_stats``. Pure aggregate —
    no I/O, no observers, no side effects. Slice 4's history store
    will project these onto a JSON-friendly shape via ``to_dict()``.

    Field semantics:
      * ``total_reports`` — every GradientReport seen
      * ``actionable_count`` — reports where outcome is one of
        {STABLE, DRIFTING, BREACHED}. DISABLED + FAILED reports
        are excluded from rate denominators because they don't
        reflect drift state.
      * ``stable_rate`` — STABLE / actionable * 100
      * ``drift_rate`` — (DRIFTING + BREACHED) / actionable * 100
      * ``breach_rate`` — BREACHED / actionable * 100
      * ``total_breaches`` — sum of len(report.breaches) across all
        reports (one report can carry multiple breaches)
      * ``severity_counts`` — per-GradientSeverity.value count
        across every reading in every report
      * ``kind_drift_counts`` — per-MeasurementKind count of
        readings with severity ≥ LOW (drift signal)
    """
    total_reports: int = 0
    actionable_count: int = 0

    stable_count: int = 0
    drifting_count: int = 0
    breached_count: int = 0
    disabled_count: int = 0
    failed_count: int = 0

    total_breaches: int = 0

    severity_counts: Dict[str, int] = None  # type: ignore[assignment]
    kind_drift_counts: Dict[str, int] = None  # type: ignore[assignment]

    stable_rate: float = 0.0
    drift_rate: float = 0.0
    breach_rate: float = 0.0

    baseline_quality: CIGWBaselineQuality = CIGWBaselineQuality.INSUFFICIENT
    schema_version: str = CIGW_COMPARATOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Frozen dataclass: must set defaults via object.__setattr__
        if self.severity_counts is None:
            object.__setattr__(self, "severity_counts", {})
        if self.kind_drift_counts is None:
            object.__setattr__(self, "kind_drift_counts", {})

    def to_dict(self) -> dict:
        return {
            "total_reports": int(self.total_reports),
            "actionable_count": int(self.actionable_count),
            "stable_count": int(self.stable_count),
            "drifting_count": int(self.drifting_count),
            "breached_count": int(self.breached_count),
            "disabled_count": int(self.disabled_count),
            "failed_count": int(self.failed_count),
            "total_breaches": int(self.total_breaches),
            "severity_counts": dict(self.severity_counts),
            "kind_drift_counts": dict(self.kind_drift_counts),
            "stable_rate": float(self.stable_rate),
            "drift_rate": float(self.drift_rate),
            "breach_rate": float(self.breach_rate),
            "baseline_quality": str(self.baseline_quality.value),
            "schema_version": str(self.schema_version),
        }


def compute_baseline_quality(
    total_reports: int,
) -> CIGWBaselineQuality:
    """Map a report count to its quality bucket.

    Resolution order (env-driven, no hardcoded magic):
      * total >= HIGH_N → HIGH
      * total >= MEDIUM_N → MEDIUM
      * total >= LOW_N → LOW
      * else → INSUFFICIENT

    NEVER raises. Negative counts → INSUFFICIENT."""
    try:
        n = max(0, int(total_reports))
        high = baseline_high_n_threshold()
        medium = baseline_medium_n_threshold()
        low = baseline_low_n_threshold()

        sorted_thresholds = sorted(
            [(high, CIGWBaselineQuality.HIGH),
             (medium, CIGWBaselineQuality.MEDIUM),
             (low, CIGWBaselineQuality.LOW)],
            key=lambda x: x[0], reverse=True,
        )
        for threshold, quality in sorted_thresholds:
            if n >= threshold:
                return quality
        return CIGWBaselineQuality.INSUFFICIENT
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[cigw_comparator] compute_baseline_quality: %s", exc,
        )
        return CIGWBaselineQuality.FAILED


def compute_cigw_aggregate_stats(
    reports: Iterable[GradientReport],
) -> CIGWAggregateStats:
    """Walk a report stream and produce one aggregate.

    Pure — no I/O, no side effects. Generator-friendly (consumed
    once). NEVER raises — exceptions during iteration caught
    per-item; offending report counted as FAILED."""
    total = 0
    stable = 0
    drifting = 0
    breached = 0
    disabled = 0
    failed = 0
    breach_total = 0

    severity_counts: Dict[str, int] = {
        sev.value: 0 for sev in GradientSeverity
    }
    kind_drift_counts: Dict[str, int] = {
        kind.value: 0 for kind in MeasurementKind
    }

    try:
        if reports is None:
            return CIGWAggregateStats(
                baseline_quality=compute_baseline_quality(0),
            )
        for raw in reports:
            try:
                total += 1
                if not isinstance(raw, GradientReport):
                    failed += 1
                    continue

                cls = _classify_report(raw)
                if cls == "stable":
                    stable += 1
                elif cls == "drifting":
                    drifting += 1
                elif cls == "breached":
                    breached += 1
                elif cls == "disabled":
                    disabled += 1
                else:
                    failed += 1

                # Per-severity + per-kind counters across every
                # reading in this report.
                try:
                    for reading in raw.readings or ():
                        sev = reading.severity.value
                        severity_counts[sev] = (
                            severity_counts.get(sev, 0) + 1
                        )
                        if reading.severity is not GradientSeverity.NONE:
                            kind = reading.measurement_kind.value
                            kind_drift_counts[kind] = (
                                kind_drift_counts.get(kind, 0) + 1
                            )
                except Exception:  # noqa: BLE001 — defensive
                    pass

                # Total-breach accumulation.
                try:
                    breach_total += len(raw.breaches or ())
                except Exception:  # noqa: BLE001 — defensive
                    pass
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[cigw_comparator] item iteration: %s", exc,
                )
                failed += 1
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[cigw_comparator] iteration: %s — partial stats", exc,
        )

    actionable = stable + drifting + breached
    stable_rate = 0.0
    drift_rate = 0.0
    breach_rate = 0.0
    if actionable > 0:
        try:
            stable_rate = (stable / actionable) * 100.0
            drift_rate = ((drifting + breached) / actionable) * 100.0
            breach_rate = (breached / actionable) * 100.0
        except Exception:  # noqa: BLE001 — defensive
            pass

    quality = compute_baseline_quality(total)

    return CIGWAggregateStats(
        total_reports=total,
        actionable_count=actionable,
        stable_count=stable,
        drifting_count=drifting,
        breached_count=breached,
        disabled_count=disabled,
        failed_count=failed,
        total_breaches=breach_total,
        severity_counts=severity_counts,
        kind_drift_counts=kind_drift_counts,
        stable_rate=round(stable_rate, 4),
        drift_rate=round(drift_rate, 4),
        breach_rate=round(breach_rate, 4),
        baseline_quality=quality,
    )


# ---------------------------------------------------------------------------
# CIGWComparisonReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CIGWComparisonReport:
    """Top-level aggregate produced by ``compare_gradient_history``."""
    outcome: CIGWEffectivenessOutcome
    stats: CIGWAggregateStats
    tightening: str
    detail: str = ""
    schema_version: str = CIGW_COMPARATOR_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "outcome": str(self.outcome.value),
            "stats": self.stats.to_dict(),
            "tightening": str(self.tightening),
            "detail": str(self.detail or ""),
            "schema_version": str(self.schema_version),
        }


def compose_aggregated_detail(
    stats: CIGWAggregateStats,
) -> str:
    """Render a one-line operator-readable summary of the stats.

    Same dense-token shape as Priority #3/#4 Slice 3 so operators
    grep the same tokens across observability artifacts. NEVER
    raises — garbage stats → empty string."""
    try:
        kind_summary = " ".join(
            f"{k}={v}" for k, v in sorted(stats.kind_drift_counts.items())
            if v > 0
        )
        return (
            f"reports={stats.total_reports} "
            f"actionable={stats.actionable_count} "
            f"stable={stats.stable_count} "
            f"drifting={stats.drifting_count} "
            f"breached={stats.breached_count} "
            f"disabled={stats.disabled_count} "
            f"failed={stats.failed_count} "
            f"breaches={stats.total_breaches} "
            f"stable_rate={stats.stable_rate:.2f}% "
            f"drift_rate={stats.drift_rate:.2f}% "
            f"breach_rate={stats.breach_rate:.2f}% "
            f"quality={stats.baseline_quality.value}"
            + (f" drifts:{kind_summary}" if kind_summary else "")
        )
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# Public surface — compare_gradient_history
# ---------------------------------------------------------------------------


def compare_gradient_history(
    reports: Iterable[GradientReport],
    *,
    enabled_override: Optional[bool] = None,
) -> CIGWComparisonReport:
    """Aggregate a stream of GradientReports into one
    CIGWComparisonReport.

    Decision tree (deterministic, closed-taxonomy):

      1. ``enabled_override is False`` OR
         ``not cigw_enabled()`` OR
         ``not comparator_enabled()`` (when override is None) →
         ``DISABLED``.
      2. ``reports is None`` or string-like or non-iterable →
         ``FAILED``.
      3. Compute stats over the stream.
      4. ``stats.total_reports == 0`` → ``INSUFFICIENT_DATA``.
      5. ``stats.baseline_quality == INSUFFICIENT`` →
         ``INSUFFICIENT_DATA``.
      6. ``breach_rate > 0`` OR ``drift_rate >= degraded_threshold``
         → ``DEGRADED`` (precedence over HEALTHY — safer default).
      7. ``stable_rate >= healthy_threshold`` → ``HEALTHY``.
      8. Else → ``INSUFFICIENT_DATA`` (drift signals exist but
         stable_rate hasn't crossed threshold).

    NEVER raises."""
    try:
        # 1. Flag resolution.
        if enabled_override is False:
            return _disabled_report(detail="enabled_override=false")
        if enabled_override is None:
            if not cigw_enabled():
                return _disabled_report(
                    detail="cigw_master_flag_off",
                )
            if not comparator_enabled():
                return _disabled_report(
                    detail="cigw_comparator_sub_flag_off",
                )

        # 2. Validate iterability.
        if reports is None:
            return _failed_report(detail="reports=None")
        if isinstance(reports, (str, bytes, bytearray)):
            return _failed_report(
                detail=f"string_like_input:{type(reports).__name__}",
            )
        try:
            iter(reports)
        except TypeError:
            return _failed_report(
                detail=f"non_iterable:{type(reports).__name__}",
            )

        # 3. Compute stats.
        stats = compute_cigw_aggregate_stats(reports)

        # 4-8. Outcome resolution.
        if stats.total_reports == 0:
            return _report_with_detail(
                outcome=CIGWEffectivenessOutcome.INSUFFICIENT_DATA,
                stats=stats,
                detail="empty_report_stream",
            )

        if stats.baseline_quality is CIGWBaselineQuality.INSUFFICIENT:
            return _report_with_detail(
                outcome=CIGWEffectivenessOutcome.INSUFFICIENT_DATA,
                stats=stats,
                detail=(
                    f"baseline_quality=insufficient "
                    f"total_reports={stats.total_reports} "
                    f"low_n={baseline_low_n_threshold()}"
                ),
            )

        # DEGRADED takes precedence over HEALTHY (safer default on
        # tie). Fires when ANY breach exists OR drift_rate crosses
        # threshold.
        deg_thr = degraded_threshold_pct()
        if stats.breach_rate > 0.0 or stats.drift_rate >= deg_thr:
            return _report_with_detail(
                outcome=CIGWEffectivenessOutcome.DEGRADED,
                stats=stats,
                detail=(
                    f"breach_rate={stats.breach_rate:.2f}% "
                    f"drift_rate={stats.drift_rate:.2f}% "
                    f">= deg_thr={deg_thr:.2f}"
                ),
            )

        healthy_thr = healthy_threshold_pct()
        if stats.stable_rate >= healthy_thr:
            return _report_with_detail(
                outcome=CIGWEffectivenessOutcome.HEALTHY,
                stats=stats,
                detail=(
                    f"stable_rate={stats.stable_rate:.2f}% "
                    f">= healthy_thr={healthy_thr:.2f} "
                    f"baseline={stats.baseline_quality.value}"
                ),
            )

        return _report_with_detail(
            outcome=CIGWEffectivenessOutcome.INSUFFICIENT_DATA,
            stats=stats,
            detail=(
                f"stable_below_threshold "
                f"stable_rate={stats.stable_rate:.2f} "
                f"< healthy_thr={healthy_thr:.2f}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[cigw_comparator] compare_gradient_history: %s", exc,
        )
        return _failed_report(
            detail=f"comparator_error:{type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# Private report constructors
# ---------------------------------------------------------------------------


def _empty_stats() -> CIGWAggregateStats:
    return CIGWAggregateStats(
        baseline_quality=CIGWBaselineQuality.INSUFFICIENT,
    )


def _disabled_report(*, detail: str) -> CIGWComparisonReport:
    return CIGWComparisonReport(
        outcome=CIGWEffectivenessOutcome.DISABLED,
        stats=_empty_stats(),
        tightening=_resolve_passed_stamp(),
        detail=str(detail),
    )


def _failed_report(*, detail: str) -> CIGWComparisonReport:
    return CIGWComparisonReport(
        outcome=CIGWEffectivenessOutcome.FAILED,
        stats=_empty_stats(),
        tightening=_resolve_passed_stamp(),
        detail=str(detail),
    )


def _report_with_detail(
    *,
    outcome: CIGWEffectivenessOutcome,
    stats: CIGWAggregateStats,
    detail: str,
) -> CIGWComparisonReport:
    """Compose a final report. Always stamps PASSED — CIGW is
    observational by AST-pinned construction."""
    summary = compose_aggregated_detail(stats)
    full_detail = f"{detail} | {summary}" if summary else str(detail)
    return CIGWComparisonReport(
        outcome=outcome,
        stats=stats,
        tightening=_resolve_passed_stamp(),
        detail=full_detail,
    )


# ---------------------------------------------------------------------------
# Cost-contract authority constant
# ---------------------------------------------------------------------------


COST_CONTRACT_PRESERVED_BY_CONSTRUCTION: bool = True


__all__ = [
    "CIGWAggregateStats",
    "CIGWBaselineQuality",
    "CIGWComparisonReport",
    "CIGWEffectivenessOutcome",
    "CIGW_COMPARATOR_SCHEMA_VERSION",
    "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
    "StampedGradientReport",
    "baseline_high_n_threshold",
    "baseline_low_n_threshold",
    "baseline_medium_n_threshold",
    "comparator_enabled",
    "compare_gradient_history",
    "compose_aggregated_detail",
    "compute_baseline_quality",
    "compute_cigw_aggregate_stats",
    "degraded_threshold_pct",
    "healthy_threshold_pct",
    "stamp_gradient_report",
]
