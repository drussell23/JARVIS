"""Priority #5 Slice 1 — Continuous Invariant Gradient Watcher primitive.

The long-horizon-drift-closing primitive: extends Move 4's
InvariantDriftAuditor (which takes SNAPSHOTS at discrete moments and
compares them) with PER-APPLY sampling of structural code metrics.

Slice 1 ships the **primitive layer only** — pure data + pure
compute. No I/O, no async, no governance imports. Slice 2 adds the
metric collectors + on-APPLY hook; Slice 3 the comparator; Slice 4
the observer + SSE; Slice 5 graduation.

Closes the §29 long-horizon-drift gap:

  * Move 4 InvariantDriftAuditor compares snapshots — between
    snapshots, individual edits cumulatively shift the codebase.
    A 1% shift × 100 ops = 100% drift with zero alarms.
  * CIGW samples on EVERY APPLY (or every successful COMPLETE),
    computes per-metric delta against a rolling baseline (default
    last 50 samples), classifies severity via closed-taxonomy
    thresholds, raises a GradientBreach when cumulative drift
    exceeds operator-tunable threshold.
  * Watches structural code metrics (line count, function count,
    import count, banned-token count, branch complexity) via stdlib
    ``ast`` + ``file.read`` — zero LLM cost on the detection path.

Direct-solve principles:

  * **Asynchronous-ready** — frozen dataclasses propagate cleanly
    across async boundaries (Slice 2 wraps disk reads via
    ``asyncio.to_thread``).

  * **Dynamic** — every threshold (rolling window size + 4 severity
    threshold pcts) is env-tunable with floor + ceiling clamps. NO
    hardcoded magic constants in severity logic.

  * **Adaptive** — degraded inputs (empty samples, single sample,
    inf/nan deltas) all map to explicit GradientSeverity values
    rather than raises. STABLE / DRIFTING / BREACHED are
    first-class outcomes — Slice 3 comparator records them
    distinct from FAILED.

  * **Intelligent** — severity classification is a closed-taxonomy
    step function over delta_pct (4 thresholds → 5 severity
    values). Operators see at-a-glance which metrics drift fastest;
    Slice 3's per-kind aggregation surfaces structural patterns.

  * **Robust** — every public function NEVER raises out. Garbage
    input → GradientOutcome.FAILED rather than exception. Pure-data
    primitive callable from any context, sync or async.

  * **No hardcoding** — 5-value × 3 closed-taxonomy enums
    (J.A.R.M.A.T.R.I.X. — every input maps to exactly one).
    Per-knob env helpers with floor + ceiling clamps mirror
    Priority #1/#2/#3/#4 Slice 1 patterns.

  * **Observational not prescriptive** — Slice 1 primitives produce
    GradientReport but NEVER propose mutations. The detection path
    is read-only (Slice 2 stat()s files + parses AST; Slice 3
    aggregates; Slice 4 publishes SSE).

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib ONLY. NEVER imports any governance module —
    strongest authority invariant. Slice 3 may import
    ``adaptation.ledger.MonotonicTighteningVerdict``; Slice 1 stays
    pure.
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.
  * No async (Slice 2 wraps via ``asyncio.to_thread``).
  * Read-only — never writes a file, never executes code.
  * No mutation tools.
  * No exec/eval/compile (mirrors Move 6 + Priority #1/#2/#3/#4
    Slice 1 critical safety pin).

Master flag default-false until Slice 5 graduation:
``JARVIS_CIGW_ENABLED``. Asymmetric env semantics — empty/whitespace
= unset = current default; explicit truthy/falsy overrides at call
time.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


CIGW_SCHEMA_VERSION: str = "gradient_watcher.1"


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def cigw_enabled() -> bool:
    """``JARVIS_CIGW_ENABLED`` (default ``true`` — graduated
    2026-05-02 in Priority #5 Slice 5).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default; explicit ``0``/``false``/``no``/``off`` evaluates false;
    explicit truthy values evaluate true. Re-read on every call so
    flips hot-revert without restart.

    Graduated default-true matches Priority #1/#2/#3/#4 discipline
    because CIGW is read-only over source files (zero LLM cost on
    detection path; structural metrics via stdlib ast + file.read;
    observational not prescriptive — every reading stamps PASSED).
    Operator approval still required for any downstream flag-flip
    proposal via MetaAdaptationGovernor."""
    raw = os.environ.get("JARVIS_CIGW_ENABLED", "").strip().lower()
    if raw == "":
        return True  # graduated default (Slice 5, 2026-05-02)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-knob helpers
# ---------------------------------------------------------------------------


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    """Read int env knob with floor+ceiling clamping. NEVER raises."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, int(raw)))
    except (TypeError, ValueError):
        return default


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    """Read float env knob with floor+ceiling clamping. NEVER raises."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, float(raw)))
    except (TypeError, ValueError):
        return default


def cigw_rolling_window_size() -> int:
    """``JARVIS_CIGW_ROLLING_WINDOW`` — number of recent samples
    used to compute the baseline mean. Default 50, clamped
    [10, 1000]. Smaller windows react faster to drift; larger
    windows are more stable to noise."""
    return _env_int_clamped(
        "JARVIS_CIGW_ROLLING_WINDOW", 50, floor=10, ceiling=1000,
    )


def cigw_low_threshold_pct() -> float:
    """``JARVIS_CIGW_LOW_THRESHOLD_PCT`` — delta_pct boundary
    between NONE and LOW severity. Default 5.0, clamped
    [0.0, 100.0]. Per-op deltas under this are noise."""
    return _env_float_clamped(
        "JARVIS_CIGW_LOW_THRESHOLD_PCT", 5.0,
        floor=0.0, ceiling=100.0,
    )


def cigw_medium_threshold_pct() -> float:
    """``JARVIS_CIGW_MEDIUM_THRESHOLD_PCT`` — delta_pct boundary
    between LOW and MEDIUM severity. Default 15.0, clamped
    [0.0, 100.0]."""
    return _env_float_clamped(
        "JARVIS_CIGW_MEDIUM_THRESHOLD_PCT", 15.0,
        floor=0.0, ceiling=100.0,
    )


def cigw_high_threshold_pct() -> float:
    """``JARVIS_CIGW_HIGH_THRESHOLD_PCT`` — delta_pct boundary
    between MEDIUM and HIGH severity. Default 30.0, clamped
    [0.0, 100.0]. Above this threshold is operator-action territory."""
    return _env_float_clamped(
        "JARVIS_CIGW_HIGH_THRESHOLD_PCT", 30.0,
        floor=0.0, ceiling=100.0,
    )


def cigw_critical_threshold_pct() -> float:
    """``JARVIS_CIGW_CRITICAL_THRESHOLD_PCT`` — delta_pct boundary
    between HIGH and CRITICAL severity. Default 50.0, clamped
    [0.0, 1000.0] (allow above 100 because integer metrics like
    line count can shift 200%+ in a single op when a file is
    rewritten)."""
    return _env_float_clamped(
        "JARVIS_CIGW_CRITICAL_THRESHOLD_PCT", 50.0,
        floor=0.0, ceiling=1000.0,
    )


# ---------------------------------------------------------------------------
# Closed-taxonomy enums — J.A.R.M.A.T.R.I.X. (3 × 5 values)
# ---------------------------------------------------------------------------


class MeasurementKind(str, enum.Enum):
    """5-value closed taxonomy for what KIND of structural metric
    is being measured.

    Each kind maps to a specific Slice 2 collector implementation.
    The closed taxonomy keeps observability surfaces (Slice 4 SSE
    events, Slice 3 per-kind aggregates) stable across releases."""

    LINE_COUNT = "line_count"
    """Total lines of source (file.read().count('\\n'))."""

    FUNCTION_COUNT = "function_count"
    """Number of ``def`` + ``async def`` definitions (ast walk)."""

    IMPORT_COUNT = "import_count"
    """Number of import + import-from statements (ast walk)."""

    BANNED_TOKEN_COUNT = "banned_token_count"
    """Number of banned-substring matches in module text. The
    'invariant' is that this should be 0 for primitive modules; a
    drift toward 1 is a gradient-breach signal even before the
    binary AST validator triggers."""

    BRANCH_COMPLEXITY = "branch_complexity"
    """Cyclomatic-ish proxy: count of if/for/while/try/except
    blocks (ast walk). Drift signals control-flow complexity
    growth."""


class GradientSeverity(str, enum.Enum):
    """5-value closed taxonomy for the SEVERITY of a single
    gradient reading.

    Severity is a step function over absolute ``delta_pct``:
      * delta_pct < low_threshold → NONE
      * low_threshold ≤ delta_pct < medium_threshold → LOW
      * medium_threshold ≤ delta_pct < high_threshold → MEDIUM
      * high_threshold ≤ delta_pct < critical_threshold → HIGH
      * delta_pct ≥ critical_threshold → CRITICAL"""

    NONE = "none"
    """Within low threshold — noise. Sample recorded, no advisory."""

    LOW = "low"
    """Above low threshold — early warning. Operator informed."""

    MEDIUM = "medium"
    """Above medium threshold — sustained drift. Operator review
    recommended."""

    HIGH = "high"
    """Above high threshold — likely misconfiguration or drift
    storm. Operator action recommended."""

    CRITICAL = "critical"
    """Above critical threshold — structural shift; binary
    invariant likely already at risk. Hard signal."""


class GradientOutcome(str, enum.Enum):
    """5-value closed taxonomy for the AGGREGATE per-target outcome.

    Resolves over a stream of GradientReadings for one target."""

    STABLE = "stable"
    """All readings have severity == NONE. No drift signal."""

    DRIFTING = "drifting"
    """At least one reading has severity LOW or MEDIUM. Operators
    see early-warning signal but no immediate action required."""

    BREACHED = "breached"
    """At least one reading has severity HIGH or CRITICAL. Hard
    signal — operator action recommended."""

    DISABLED = "disabled"
    """Master flag is off. No computation performed."""

    FAILED = "failed"
    """Garbage input or compute fault. Distinct from STABLE
    (which had valid samples below threshold)."""


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvariantSample:
    """One per-APPLY structural-metric measurement.

    Frozen + hashable so samples can be deduplicated by content
    and stored in a rolling ring buffer.

    Fields:
      * ``target_id`` — caller-supplied identifier (typically a file
        path or module name)
      * ``measurement_kind`` — closed-taxonomy MeasurementKind value
      * ``value`` — the measured number (lines, functions, etc.)
      * ``monotonic_ts`` — wall-clock monotonic timestamp at sample
      * ``op_id`` — optional operation identifier for tracing
      * ``detail`` — bounded operator-readable summary (≤256 chars)
    """
    target_id: str
    measurement_kind: MeasurementKind
    value: float
    monotonic_ts: float = 0.0
    op_id: str = ""
    detail: str = ""
    schema_version: str = CIGW_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_id": str(self.target_id),
            "measurement_kind": self.measurement_kind.value,
            "value": float(self.value),
            "monotonic_ts": float(self.monotonic_ts),
            "op_id": str(self.op_id),
            "detail": str(self.detail)[:256],
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any],
    ) -> Optional["InvariantSample"]:
        try:
            if not isinstance(raw, Mapping):
                return None
            if raw.get("schema_version") != CIGW_SCHEMA_VERSION:
                return None
            kind_raw = raw.get("measurement_kind")
            if not isinstance(kind_raw, str):
                return None
            try:
                kind = MeasurementKind(kind_raw)
            except ValueError:
                return None
            return cls(
                target_id=str(raw.get("target_id", "")),
                measurement_kind=kind,
                value=float(raw.get("value", 0.0)),
                monotonic_ts=float(raw.get("monotonic_ts", 0.0)),
                op_id=str(raw.get("op_id", "")),
                detail=str(raw.get("detail", ""))[:256],
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


@dataclass(frozen=True)
class GradientReading:
    """One gradient computation: how much did the latest sample
    drift from the rolling baseline?

    Fields:
      * ``target_id`` — same as the underlying samples
      * ``measurement_kind`` — same as the underlying samples
      * ``baseline_mean`` — mean of last N-1 samples (excluding
        current)
      * ``current_value`` — value of the latest sample
      * ``delta_abs`` — current_value - baseline_mean (signed)
      * ``delta_pct`` — abs(delta) / baseline * 100 (unsigned %)
      * ``severity`` — closed-taxonomy GradientSeverity
      * ``sample_count`` — number of samples in the baseline
        (information for operators to weight the reading)
    """
    target_id: str
    measurement_kind: MeasurementKind
    baseline_mean: float
    current_value: float
    delta_abs: float
    delta_pct: float
    severity: GradientSeverity
    sample_count: int = 0
    schema_version: str = CIGW_SCHEMA_VERSION

    def is_breach(self) -> bool:
        """True iff severity is HIGH or CRITICAL."""
        return self.severity in (
            GradientSeverity.HIGH, GradientSeverity.CRITICAL,
        )

    def is_drift(self) -> bool:
        """True iff severity is LOW or MEDIUM (early warning)."""
        return self.severity in (
            GradientSeverity.LOW, GradientSeverity.MEDIUM,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_id": str(self.target_id),
            "measurement_kind": self.measurement_kind.value,
            "baseline_mean": float(self.baseline_mean),
            "current_value": float(self.current_value),
            "delta_abs": float(self.delta_abs),
            "delta_pct": float(self.delta_pct),
            "severity": self.severity.value,
            "sample_count": int(self.sample_count),
            "schema_version": str(self.schema_version),
        }


@dataclass(frozen=True)
class GradientBreach:
    """A breach is a HIGH or CRITICAL reading wrapped with a
    deterministic detail string for SSE event payloads.

    Slice 4's observer publishes one EVENT_TYPE_GRADIENT_BREACH_
    DETECTED per breach, dedupe'd via the rolling-window + signature
    discipline."""
    reading: GradientReading
    detail: str = ""
    schema_version: str = CIGW_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reading": self.reading.to_dict(),
            "detail": str(self.detail or ""),
            "schema_version": str(self.schema_version),
        }


@dataclass(frozen=True)
class GradientReport:
    """Aggregate per-target outcome over one or more gradient
    readings.

    Fields:
      * ``outcome`` — closed-taxonomy GradientOutcome
      * ``readings`` — every reading included in the aggregate
        (chronological order)
      * ``breaches`` — subset of readings where severity is HIGH /
        CRITICAL (synthesized by Slice 1)
      * ``total_samples`` — total count of underlying InvariantSamples
        contributing to the readings
      * ``detail`` — operator-readable summary
    """
    outcome: GradientOutcome
    readings: Tuple[GradientReading, ...] = field(default_factory=tuple)
    breaches: Tuple[GradientBreach, ...] = field(default_factory=tuple)
    total_samples: int = 0
    detail: str = ""
    schema_version: str = CIGW_SCHEMA_VERSION

    def has_breach(self) -> bool:
        return self.outcome is GradientOutcome.BREACHED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "readings": [r.to_dict() for r in self.readings],
            "breaches": [b.to_dict() for b in self.breaches],
            "total_samples": int(self.total_samples),
            "detail": str(self.detail or ""),
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# Pure decision functions
# ---------------------------------------------------------------------------


def compute_baseline_mean(
    samples: Sequence[InvariantSample],
    *,
    exclude_last: bool = True,
) -> float:
    """Arithmetic mean of sample values.

    When ``exclude_last`` is True (default), the most recent sample
    is excluded — the typical use case is "compute baseline against
    historical samples and compare current to that baseline".

    NEVER raises. Empty input → 0.0."""
    try:
        if not samples:
            return 0.0
        values = [
            float(s.value) for s in samples
            if isinstance(s, InvariantSample)
        ]
        if not values:
            return 0.0
        if exclude_last and len(values) > 1:
            values = values[:-1]
        if not values:
            return 0.0
        return sum(values) / len(values)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[cigw] compute_baseline_mean: %s", exc)
        return 0.0


def compute_severity(
    delta_pct: float,
    *,
    low_threshold: Optional[float] = None,
    medium_threshold: Optional[float] = None,
    high_threshold: Optional[float] = None,
    critical_threshold: Optional[float] = None,
) -> GradientSeverity:
    """Closed-taxonomy step function over absolute delta_pct.

    Resolution order (env-driven; operator-tunable):
      * delta_pct ≥ critical_threshold → CRITICAL
      * delta_pct ≥ high_threshold → HIGH
      * delta_pct ≥ medium_threshold → MEDIUM
      * delta_pct ≥ low_threshold → LOW
      * else → NONE

    Defensive: NaN / Inf / negative → NONE (treats invalid input as
    no signal). Reversed thresholds (e.g., low > medium) still
    resolve gracefully — the function takes max-of-(threshold,
    severity) over the sorted threshold list.

    NEVER raises."""
    try:
        d = float(delta_pct)
        if d != d or d == float("inf") or d == float("-inf"):
            return GradientSeverity.NONE
        d = abs(d)

        low = (
            float(low_threshold)
            if low_threshold is not None
            else cigw_low_threshold_pct()
        )
        medium = (
            float(medium_threshold)
            if medium_threshold is not None
            else cigw_medium_threshold_pct()
        )
        high = (
            float(high_threshold)
            if high_threshold is not None
            else cigw_high_threshold_pct()
        )
        critical = (
            float(critical_threshold)
            if critical_threshold is not None
            else cigw_critical_threshold_pct()
        )

        # Sort thresholds ascending so reversed env knobs still
        # produce sane resolution.
        ordered = sorted([
            (low, GradientSeverity.LOW),
            (medium, GradientSeverity.MEDIUM),
            (high, GradientSeverity.HIGH),
            (critical, GradientSeverity.CRITICAL),
        ], key=lambda x: x[0])

        result = GradientSeverity.NONE
        for threshold, severity in ordered:
            if d >= threshold:
                result = severity
        return result
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[cigw] compute_severity: %s", exc)
        return GradientSeverity.NONE


def compute_gradient_reading(
    samples: Sequence[InvariantSample],
) -> Optional[GradientReading]:
    """Build a GradientReading from a non-empty sample sequence.

    The latest sample's value is treated as ``current``; all earlier
    samples form the baseline. Two or more samples required —
    single-sample input → None (no baseline to compare against;
    operator interprets as STABLE-by-default).

    NEVER raises."""
    try:
        if not samples or len(samples) < 2:
            return None
        valid = [s for s in samples if isinstance(s, InvariantSample)]
        if len(valid) < 2:
            return None

        # All samples must have the same target_id + measurement_kind
        # for the baseline to be meaningful. Use the latest sample's
        # identifiers; reject if heterogeneous.
        latest = valid[-1]
        target_id = latest.target_id
        kind = latest.measurement_kind
        homogeneous = [
            s for s in valid
            if s.target_id == target_id
            and s.measurement_kind is kind
        ]
        if len(homogeneous) < 2:
            return None

        baseline_mean = compute_baseline_mean(
            homogeneous, exclude_last=True,
        )
        current_value = float(latest.value)

        if baseline_mean == 0.0:
            # Avoid div-by-zero. Use absolute current value as
            # a proxy: if baseline was 0 and current is non-zero,
            # the change is "infinite" → cap at CRITICAL via the
            # current_value-as-pct heuristic.
            if abs(current_value) < 1e-9:
                delta_pct = 0.0
            else:
                # Cap at 1000% so severity computation lands in
                # CRITICAL bucket without overflow.
                delta_pct = 1000.0
        else:
            delta_pct = (
                abs(current_value - baseline_mean) / abs(baseline_mean)
            ) * 100.0

        delta_abs = current_value - baseline_mean
        severity = compute_severity(delta_pct)

        return GradientReading(
            target_id=target_id,
            measurement_kind=kind,
            baseline_mean=round(baseline_mean, 4),
            current_value=round(current_value, 4),
            delta_abs=round(delta_abs, 4),
            delta_pct=round(delta_pct, 4),
            severity=severity,
            sample_count=len(homogeneous),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[cigw] compute_gradient_reading: %s", exc)
        return None


def compute_gradient_outcome(
    readings: Sequence[GradientReading],
    *,
    enabled_override: Optional[bool] = None,
    detail_for_zero_readings: str = "",
) -> GradientReport:
    """Aggregate readings into a GradientReport.

    Decision tree (closed-taxonomy):
      1. Master flag off → DISABLED
      2. Empty / non-Sequence → STABLE with 0 readings (operator
         interprets as "no signal yet")
      3. Any reading is_breach() (HIGH/CRITICAL) → BREACHED
      4. Any reading is_drift() (LOW/MEDIUM) → DRIFTING
      5. All readings NONE → STABLE

    NEVER raises."""
    try:
        is_enabled = (
            enabled_override if enabled_override is not None
            else cigw_enabled()
        )
        if not is_enabled:
            return GradientReport(
                outcome=GradientOutcome.DISABLED,
                detail="cigw_disabled_or_master_flag_off",
            )

        # Strings + bytes are technically Sequences but a string-as-
        # readings-stream is always a caller bug — treat as FAILED so
        # the mistake surfaces immediately. Same discipline as
        # Priority #3/#4 Slice 3.
        if isinstance(readings, (str, bytes, bytearray)):
            return GradientReport(
                outcome=GradientOutcome.FAILED,
                detail=f"string_like_input:{type(readings).__name__}",
            )
        if not isinstance(readings, Sequence):
            return GradientReport(
                outcome=GradientOutcome.FAILED,
                detail="readings not a Sequence",
            )

        valid = tuple(
            r for r in readings if isinstance(r, GradientReading)
        )
        if not valid:
            return GradientReport(
                outcome=GradientOutcome.STABLE,
                readings=(),
                breaches=(),
                total_samples=0,
                detail=detail_for_zero_readings or "no_readings",
            )

        breaches = tuple(
            GradientBreach(
                reading=r,
                detail=_compose_breach_detail(r),
            )
            for r in valid if r.is_breach()
        )
        any_drift = any(r.is_drift() for r in valid)
        any_breach = bool(breaches)

        if any_breach:
            outcome = GradientOutcome.BREACHED
        elif any_drift:
            outcome = GradientOutcome.DRIFTING
        else:
            outcome = GradientOutcome.STABLE

        total_samples = sum(r.sample_count for r in valid)

        detail = _compose_outcome_detail(
            outcome=outcome,
            readings=valid,
            breach_count=len(breaches),
        )

        return GradientReport(
            outcome=outcome,
            readings=valid,
            breaches=breaches,
            total_samples=total_samples,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[cigw] compute_gradient_outcome: %s", exc)
        return GradientReport(
            outcome=GradientOutcome.FAILED,
            detail=f"compute_error:{type(exc).__name__}",
        )


def _compose_breach_detail(reading: GradientReading) -> str:
    """Operator-readable detail for one breach. Same dense-token
    discipline as Priority #3/#4."""
    try:
        return (
            f"target={reading.target_id} "
            f"kind={reading.measurement_kind.value} "
            f"baseline={reading.baseline_mean:.2f} "
            f"current={reading.current_value:.2f} "
            f"delta_pct={reading.delta_pct:.2f} "
            f"severity={reading.severity.value}"
        )
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _compose_outcome_detail(
    *,
    outcome: GradientOutcome,
    readings: Sequence[GradientReading],
    breach_count: int,
) -> str:
    """Operator-readable summary of an aggregate outcome."""
    try:
        n = len(readings)
        per_severity: Dict[str, int] = {}
        for r in readings:
            sev = r.severity.value
            per_severity[sev] = per_severity.get(sev, 0) + 1
        sev_summary = " ".join(
            f"{k}={v}" for k, v in sorted(per_severity.items())
        )
        return (
            f"outcome={outcome.value} readings={n} "
            f"breaches={breach_count} {sev_summary}"
        )
    except Exception:  # noqa: BLE001 — defensive
        return f"outcome={outcome.value}"


# ---------------------------------------------------------------------------
# Cost-contract authority constant (AST-pin target for Slice 5)
# ---------------------------------------------------------------------------


COST_CONTRACT_PRESERVED_BY_CONSTRUCTION: bool = True


__all__ = [
    "CIGW_SCHEMA_VERSION",
    "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
    "GradientBreach",
    "GradientOutcome",
    "GradientReading",
    "GradientReport",
    "GradientSeverity",
    "InvariantSample",
    "MeasurementKind",
    "cigw_critical_threshold_pct",
    "cigw_enabled",
    "cigw_high_threshold_pct",
    "cigw_low_threshold_pct",
    "cigw_medium_threshold_pct",
    "cigw_rolling_window_size",
    "compute_baseline_mean",
    "compute_gradient_outcome",
    "compute_gradient_reading",
    "compute_severity",
]
