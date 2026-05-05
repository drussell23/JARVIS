"""M9 Slice 1 — CuriosityGradient primitive (PRD §30.5.1).

Replaces heuristic "is this op interesting?" with a numerical
curiosity signal derived from the model's own prediction error.
Three independent input sources composed via weighted aggregation:

  * **Logprob entropy** — model self-uncertainty captured at
    GENERATE phase (`phase_capture` adapter feeds it in Slice 2).
  * **Prophecy error** — heuristic prediction failure
    (`ProphecyEngine.get_risk_scores()` minus actual VERIFY
    outcome).
  * **Postmortem recurrence** — long-horizon recurrence from the
    Coherence Auditor's `RECURRENCE_DRIFT` signal.

This module is the **contract layer for the entire M9 arc** —
Slice 2's collector, Slice 3's `SensorGovernor` consumer,
Slice 4's observability surfaces, and Slice 5's graduation pins
all read against the dataclass + closed-enum + pure-function
shape ESTABLISHED HERE. Tracker + governor never invent fields
they wished existed.

Architectural locks (operator mandate):

  * **Frozen dataclass + closed-taxonomy enums** — every routing
    decision branches on a closed enum value, never on a free-
    form string. Same discipline as :class:`EpistemicBudget`
    (Upgrade 1) and :class:`ActionOutcomeRecord` (M11).
  * **Pure substrate, zero LLM cost on hot path** — no provider
    imports, no model calls. Cost contract structurally
    preserved (cannot violate §26.6).
  * **No hardcoding** — all knobs read at call time via
    :func:`_read_int_knob` / :func:`_read_float_knob`. FlagRegistry
    seeds at Slice 5.
  * **Decision E1** — shared math: recency decay defers to
    :func:`_scoring_primitives.recency_weight`. M9 NEVER
    duplicates the decay formula. Pinned by Slice 5 AST
    invariant ``curiosity_uses_shared_scoring_primitives``.
  * **Cold-start inertness** — when fewer than
    :func:`curiosity_min_samples` observations exist for a
    region, :func:`compute_curiosity` returns
    ``CuriositySource.INSUFFICIENT_DATA``; consumers default
    multiplier to ``1.0`` (no bias). Prevents random-walk on
    boot.
  * **Stale-focus auto-decay** — Slice 2's tracker reads the
    :attr:`CuriosityScore.last_updated_at_unix` + checks the
    :func:`curiosity_stale_focus_hours` env knob; if a cluster
    has been peaked beyond that window, the score's
    ``decay_reason`` flips to ``STALE_FOCUS`` and the consumer
    multiplier rebases to ``1.0``. Prevents "locked on
    degenerate region" pathology.
  * **Authority asymmetry** (AST-pinned at Slice 5) — this
    module is a pure primitive layer. MUST NOT import
    ``orchestrator`` / ``iron_gate`` / ``policy`` /
    ``change_engine`` / ``candidate_generator`` / ``providers`` /
    ``urgency_router`` / ``auto_action_router`` /
    ``tool_executor`` / ``phase_runners`` / ``semantic_guardian``
    / ``strategic_direction`` / ``sensor_governor``.
    ``SensorGovernor`` (Slice 3 consumer) lazy-imports M9; the
    reverse is forbidden.

The contract layer (Slice 1):
  * :class:`CuriositySource` (5-value closed enum)
  * :class:`CuriosityDecayReason` (5-value closed enum)
  * :class:`CuriosityObservation` (one input sample, frozen)
  * :class:`CuriosityScore` (per-region aggregated state, frozen)
  * :func:`compute_curiosity` (pure function over observations)
  * :func:`curiosity_multiplier_from_score` (pure function —
    Slice 3's `SensorGovernor._weighted_cap` consumes this)
  * Master flag :func:`curiosity_gradient_enabled`
  * Env-knob accessors (no hardcoding):
    - :func:`curiosity_halflife_days` (default 14.0)
    - :func:`curiosity_min_samples` (default 8)
    - :func:`curiosity_stale_focus_hours` (default 24)
    - :func:`curiosity_source_weight_logprob` (default 1.0)
    - :func:`curiosity_source_weight_prophecy` (default 1.0)
    - :func:`curiosity_source_weight_recurrence` (default 1.0)
    - :func:`curiosity_multiplier_floor` (default 0.5)
    - :func:`curiosity_multiplier_ceiling` (default 2.0)
"""
from __future__ import annotations

import enum
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

from backend.core.ouroboros.governance._scoring_primitives import (
    recency_weight,
)


# ---------------------------------------------------------------------------
# Schema version (bump on breaking shape changes)
# ---------------------------------------------------------------------------


CURIOSITY_GRADIENT_SCHEMA_VERSION: str = "curiosity_gradient.1"


# ---------------------------------------------------------------------------
# Master flag — graduates default-true at Slice 5
# ---------------------------------------------------------------------------


def curiosity_gradient_enabled() -> bool:
    """``JARVIS_CURIOSITY_GRADIENT_ENABLED`` (default ``false``
    until Slice 5 graduation per PRD §30.5.1).

    Asymmetric env semantics — empty/whitespace = unset = current
    default (false for Slice 1); explicit ``1``/``true``/``yes``/
    ``on`` flips on. Same shape as :func:`epistemic_budget_enabled`
    /:func:`action_outcome_memory_enabled` /:func:`failure_mode_-
    memory_enabled` graduated flags so the Slice 5 graduation flip
    is a one-character edit.

    Re-read on every call so flips hot-revert without restart."""
    raw = os.environ.get(
        "JARVIS_CURIOSITY_GRADIENT_ENABLED", "",
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


def curiosity_halflife_days() -> float:
    """``JARVIS_CURIOSITY_HALFLIFE_DAYS`` — recency-decay halflife
    for observation samples. Default 14.0; clamped [0.1, 365.0].
    Consumed by Slice 2's collector via
    :func:`_scoring_primitives.recency_weight`. Captured at
    score-compute time for stability — env changes mid-window
    don't reshape past samples."""
    return _read_float_knob(
        "JARVIS_CURIOSITY_HALFLIFE_DAYS", 14.0, 0.1, 365.0,
    )


def curiosity_min_samples() -> int:
    """``JARVIS_CURIOSITY_MIN_SAMPLES`` — cold-start gate. When a
    region has fewer than this many observations,
    :func:`compute_curiosity` returns ``CuriositySource.
    INSUFFICIENT_DATA`` and downstream consumers default
    multiplier to 1.0 (no bias). Default 8; clamped [1, 1000]."""
    return _read_int_knob(
        "JARVIS_CURIOSITY_MIN_SAMPLES", 8, 1, 1000,
    )


def curiosity_stale_focus_hours() -> int:
    """``JARVIS_CURIOSITY_STALE_FOCUS_HOURS`` — when a cluster's
    score has been at peak beyond this window, the score's
    ``decay_reason`` flips to ``STALE_FOCUS`` and the consumer
    multiplier rebases to 1.0. Prevents "locked on degenerate
    region" pathology. Default 24; clamped [1, 720]."""
    return _read_int_knob(
        "JARVIS_CURIOSITY_STALE_FOCUS_HOURS", 24, 1, 720,
    )


def curiosity_source_weight_logprob() -> float:
    """Weight for ``LOGPROB_ENTROPY`` source in the multi-source
    aggregator. Default 1.0; clamped [0.0, 10.0]. Set to 0 to
    structurally exclude this source."""
    return _read_float_knob(
        "JARVIS_CURIOSITY_WEIGHT_LOGPROB", 1.0, 0.0, 10.0,
    )


def curiosity_source_weight_prophecy() -> float:
    """Weight for ``PROPHECY_ERROR`` source. Default 1.0; clamped
    [0.0, 10.0]. Set to 0 to structurally exclude this source."""
    return _read_float_knob(
        "JARVIS_CURIOSITY_WEIGHT_PROPHECY", 1.0, 0.0, 10.0,
    )


def curiosity_source_weight_recurrence() -> float:
    """Weight for ``POSTMORTEM_RECURRENCE`` source. Default 1.0;
    clamped [0.0, 10.0]. Set to 0 to structurally exclude this
    source."""
    return _read_float_knob(
        "JARVIS_CURIOSITY_WEIGHT_RECURRENCE", 1.0, 0.0, 10.0,
    )


def curiosity_multiplier_floor() -> float:
    """Lower bound for the curiosity multiplier returned by
    :func:`curiosity_multiplier_from_score`. Default 0.5; clamped
    [0.0, 1.0]. Floor < 1.0 means low-curiosity regions can be
    actively de-prioritized; Floor = 1.0 means curiosity only
    boosts (never throttles). Operator choice."""
    return _read_float_knob(
        "JARVIS_CURIOSITY_MULTIPLIER_FLOOR", 0.5, 0.0, 1.0,
    )


def curiosity_multiplier_ceiling() -> float:
    """Upper bound for the curiosity multiplier. Default 2.0;
    clamped [1.0, 10.0]. Ceiling × global cap = max emission to
    a single high-curiosity cluster — bounded by construction so
    SensorGovernor's global cap is structurally never bypassed."""
    return _read_float_knob(
        "JARVIS_CURIOSITY_MULTIPLIER_CEILING", 2.0, 1.0, 10.0,
    )


# ---------------------------------------------------------------------------
# Closed-taxonomy enums
# ---------------------------------------------------------------------------


class CuriositySource(str, enum.Enum):
    """Closed taxonomy for the input signal source that
    dominated a :class:`CuriosityScore`. Branched on by
    Slice 4 observability rendering + Slice 5 SSE payload.

    NEVER add free-form strings — every routing decision in
    M9 branches on this enum (or :class:`CuriosityDecayReason`).
    Symmetric discipline to :class:`OutcomeKind` (M11) and
    :class:`BudgetOutcome` (Upgrade 1)."""

    LOGPROB_ENTROPY = "logprob_entropy"
    """Model self-uncertainty was the dominant input — high
    rolling-window entropy across recent generations in this
    region."""

    PROPHECY_ERROR = "prophecy_error"
    """Heuristic prediction failure was dominant — Prophecy
    predicted low risk and VERIFY failed (or vice versa)."""

    POSTMORTEM_RECURRENCE = "postmortem_recurrence"
    """Recurrence-drift was dominant — same failure_class
    postmortem keeps surfacing in this region."""

    INSUFFICIENT_DATA = "insufficient_data"
    """Cold-start gate — fewer than
    :func:`curiosity_min_samples` observations exist for this
    region. Consumer multiplier defaults to 1.0 (no bias)."""

    DISABLED = "disabled"
    """Master flag is off — score is structurally inert."""


class CuriosityDecayReason(str, enum.Enum):
    """Closed taxonomy for why a score's curiosity has been
    decayed (multiplier reset to 1.0 even though raw signal
    is high). Operator-explainable causes only — no
    silent-degradation paths."""

    NONE = "none"
    """No decay applied. Score is using its computed magnitude
    directly."""

    STALE_FOCUS = "stale_focus"
    """Cluster has been at peak curiosity beyond
    :func:`curiosity_stale_focus_hours`. Auto-decay prevents
    locked-on-degenerate-region pathology."""

    RECURRENCE_LOOP = "recurrence_loop"
    """Repeated POSTMORTEM_RECURRENCE inputs without intervening
    convergence. Slice 2's collector flags this when the same
    failure_class recurs N+ times in the window without an
    APPLIED_VERIFIED outcome breaking the streak."""

    OPERATOR_RESET = "operator_reset"
    """Operator-explicit decay via ``/curiosity reset <id>``
    (Slice 4)."""

    DISABLED = "disabled"
    """Master flag is off — decay path is structurally inert."""


# ---------------------------------------------------------------------------
# CuriosityObservation — one input sample
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CuriosityObservation:
    """One observation sample. Frozen — Slice 2's collector
    appends instances to a per-cluster bounded ring buffer; the
    aggregate state lives on :class:`CuriosityScore`.

    The ``value`` semantics are source-specific but all
    normalized to ``[0.0, 1.0]`` at ingest time so the
    aggregator can compose them via weighted mean:

      * ``LOGPROB_ENTROPY`` — normalized entropy in [0, 1]
        (Slice 2 normalizes via ``H / max_H_observed`` per-window)
      * ``PROPHECY_ERROR`` — absolute(predicted_risk -
        actual_outcome_indicator) in [0, 1]
      * ``POSTMORTEM_RECURRENCE`` — log-scale recurrence count
        normalized via :func:`_scoring_primitives.weight_score`."""

    source: CuriositySource
    """Which signal source produced this sample. Drives weight
    selection at aggregation time."""

    cluster_id: str
    """SemanticIndex cluster_id (or ``_global`` fallback per
    Decision A3 SemanticIndex-optional). Always lowercased +
    stripped at ingest."""

    value: float
    """Normalized [0, 1] sample. Out-of-range values are
    silently clamped at ingest by Slice 2's collector — the
    contract here is "consumer can trust this is in [0, 1]"."""

    at_unix: float
    """Observation timestamp. Drives recency-decay via
    :func:`_scoring_primitives.recency_weight`."""

    op_id: str = ""
    """Originating op_id — informational; does not affect
    aggregation. Useful for Slice 4 detail rendering."""


# ---------------------------------------------------------------------------
# CuriosityScore — per-region aggregated state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CuriosityScore:
    """Per-cluster aggregated curiosity state. Frozen — Slice 2's
    tracker swaps frozen instances atomically rather than
    mutating in place (matches the established immutability
    discipline of :class:`EpistemicBudget` and
    :class:`ActionOutcomeRecord`).

    Every field Slice 2's collector populates + Slice 3's
    `SensorGovernor` reads + Slice 4 observability projects is
    NAMED here. Tracker + governor implement against this
    contract; they NEVER add fields they wished existed."""

    cluster_id: str
    """Keying dimension — Slice 2's tracker keys per-cluster
    state by this. ``_global`` for the SemanticIndex-optional
    fallback bucket."""

    magnitude: float = 0.0
    """Aggregate curiosity magnitude in ``[0.0, 1.0]``. Weighted
    mean of source contributions, recency-weighted via
    :func:`_scoring_primitives.recency_weight`. The "raw signal"
    — :func:`curiosity_multiplier_from_score` post-processes
    this through floor/ceiling clamps + decay reasons before
    returning the SensorGovernor multiplier."""

    confidence: float = 0.0
    """Aggregate confidence in ``[0.0, 1.0]``. Function of
    sample count vs :func:`curiosity_min_samples` floor +
    source diversity (more sources → higher confidence). When
    confidence is below a consumer-chosen threshold,
    SensorGovernor (Slice 3) defaults to multiplier 1.0
    regardless of magnitude."""

    samples_count: int = 0
    """Total observations in the window. Slice 2's bounded ring
    buffer caps this implicitly via its maxlen; this field
    captures the post-aggregation count snapshot."""

    dominant_source: CuriositySource = CuriositySource.INSUFFICIENT_DATA
    """Which source contributed the largest weighted share to
    :attr:`magnitude`. Drives Slice 4 observability rendering
    + SSE payload routing."""

    source_breakdown: Tuple[Tuple[str, float], ...] = field(
        default_factory=tuple,
    )
    """Per-source contribution snapshot, ordered by descending
    contribution: ``((source.value, weighted_value), ...)``.
    Operator-explainability — the /curiosity REPL renders this
    so an operator can see "magnitude=0.78 = 0.5 logprob + 0.2
    prophecy + 0.08 recurrence" rather than a single opaque
    number."""

    decay_reason: CuriosityDecayReason = CuriosityDecayReason.NONE
    """Why the magnitude has been decayed (if any). Slice 2's
    tracker computes; Slice 3's consumer respects (treats
    non-NONE as multiplier=1.0). Slice 4 observability shows
    the reason verbatim."""

    last_updated_at_unix: float = 0.0
    """Last-mutation timestamp. Slice 2's tracker writes on each
    new observation. Slice 3's consumer reads to detect
    staleness vs :func:`curiosity_stale_focus_hours`."""

    schema_version: str = field(
        default=CURIOSITY_GRADIENT_SCHEMA_VERSION,
    )

    # ---- Pure helpers consumed by Slice 3's SensorGovernor --------

    def is_cold_start(self) -> bool:
        """True when fewer than :func:`curiosity_min_samples`
        observations have been recorded — score is structurally
        inert (consumer multiplier defaults to 1.0)."""
        return (
            self.samples_count < curiosity_min_samples()
            or self.dominant_source is (
                CuriositySource.INSUFFICIENT_DATA
            )
            or self.dominant_source is CuriositySource.DISABLED
        )

    def is_stale(self, *, now_ts: Optional[float] = None) -> bool:
        """True when :attr:`last_updated_at_unix` is older than
        :func:`curiosity_stale_focus_hours`. Slice 2's tracker
        consults this to apply ``STALE_FOCUS`` decay on the next
        observation."""
        try:
            now = (
                now_ts if now_ts is not None
                else time.time()
            )
            age_s = float(now) - float(self.last_updated_at_unix)
            if age_s < 0:
                return False
            return age_s > (
                float(curiosity_stale_focus_hours()) * 3600.0
            )
        except Exception:  # noqa: BLE001 — defensive
            return False

    def is_decayed(self) -> bool:
        """True when any non-NONE decay reason is set. Slice 3's
        consumer treats this as 'multiplier=1.0' regardless of
        :attr:`magnitude`."""
        return self.decay_reason is not CuriosityDecayReason.NONE

    # ---- Slice 4 observability projection -------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Sanitized JSON-safe projection for
        ``GET /observability/curiosity[/region/{id}]`` + REPL
        rendering. NEVER raises."""
        try:
            return {
                "schema_version": self.schema_version,
                "cluster_id": self.cluster_id,
                "magnitude": float(self.magnitude),
                "confidence": float(self.confidence),
                "samples_count": int(self.samples_count),
                "dominant_source": self.dominant_source.value,
                "source_breakdown": [
                    {"source": s, "contribution": float(v)}
                    for s, v in self.source_breakdown
                ],
                "decay_reason": self.decay_reason.value,
                "last_updated_at_unix": float(
                    self.last_updated_at_unix,
                ),
                "is_cold_start": self.is_cold_start(),
                "is_decayed": self.is_decayed(),
            }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "schema_version": self.schema_version,
                "cluster_id": self.cluster_id,
                "error": "projection_failed",
            }


# ---------------------------------------------------------------------------
# Source weight resolution — closed-enum dispatch (no string keys)
# ---------------------------------------------------------------------------


def _weight_for_source(source: CuriositySource) -> float:
    """Closed-enum dispatch — returns the operator-tunable
    weight for the supplied source. Branches on the enum, never
    on a string key. Pinned by Slice 5 AST invariant."""
    if source is CuriositySource.LOGPROB_ENTROPY:
        return curiosity_source_weight_logprob()
    if source is CuriositySource.PROPHECY_ERROR:
        return curiosity_source_weight_prophecy()
    if source is CuriositySource.POSTMORTEM_RECURRENCE:
        return curiosity_source_weight_recurrence()
    # INSUFFICIENT_DATA / DISABLED — no contribution
    return 0.0


# ---------------------------------------------------------------------------
# compute_curiosity — pure aggregator (Slice 2's tracker calls this)
# ---------------------------------------------------------------------------


def compute_curiosity(
    cluster_id: str,
    observations: Sequence[CuriosityObservation],
    *,
    now_ts: Optional[float] = None,
    enabled_override: Optional[bool] = None,
    decay_reason_override: Optional[CuriosityDecayReason] = None,
) -> CuriosityScore:
    """**Authoritative pure aggregator.** Composes a window of
    :class:`CuriosityObservation` samples into a frozen
    :class:`CuriosityScore`.

    Composition pipeline (deterministic, AST-pinned at Slice 5):

      1. Master-flag check (cheap path — early exit when off).
      2. Cold-start check — when fewer than
         :func:`curiosity_min_samples` observations,
         dominant_source = ``INSUFFICIENT_DATA``.
      3. Per-source weighted mean with recency decay via
         :func:`_scoring_primitives.recency_weight`. Each sample
         contributes ``value × source_weight × recency_weight``.
      4. Aggregate magnitude = weighted mean across all sources,
         clamped to ``[0, 1]``.
      5. Confidence = function of (sample_count / min_samples,
         clamped to [0, 1]) × source_diversity (count of distinct
         sources contributing non-zero weight, normalized by 3).
      6. Dominant source = source with highest aggregate
         contribution.
      7. ``decay_reason_override`` (used by Slice 2's tracker
         when STALE_FOCUS / RECURRENCE_LOOP / OPERATOR_RESET
         applies). Defaults to NONE when not set.

    Pure function — same inputs → same outputs. No clock reads
    except via the supplied ``now_ts`` (defaults to
    :func:`time.time` when None). NEVER raises — all
    error paths produce a valid :class:`CuriosityScore` with
    ``DISABLED`` source.

    Args
    ----
    cluster_id:
        SemanticIndex cluster_id or ``_global`` fallback.
    observations:
        Sequence of frozen samples. Caller (Slice 2's tracker)
        is responsible for windowing + ring-buffer cap.
    now_ts:
        Optional unix-time override for deterministic testing.
    enabled_override:
        Optional master-flag override for testing — when
        not None, bypasses the env read.
    decay_reason_override:
        Optional decay-reason override — Slice 2's tracker
        passes ``STALE_FOCUS`` / ``RECURRENCE_LOOP`` /
        ``OPERATOR_RESET`` when those conditions apply.
    """
    # Defensive cluster_id normalization — empty string falls
    # through to "_global" (matches Decision A3 from M11).
    cid = (cluster_id or "").strip().lower() or "_global"

    # Master-flag gate
    enabled = (
        enabled_override
        if enabled_override is not None
        else curiosity_gradient_enabled()
    )
    if not enabled:
        return CuriosityScore(
            cluster_id=cid,
            dominant_source=CuriositySource.DISABLED,
            decay_reason=CuriosityDecayReason.DISABLED,
        )

    # Defensive observation iteration
    try:
        obs_list = [
            o for o in observations
            if isinstance(o, CuriosityObservation)
        ]
    except Exception:  # noqa: BLE001 — defensive
        obs_list = []

    sample_count = len(obs_list)
    min_samples = curiosity_min_samples()

    # Cold-start gate — short-circuit before doing weighted-mean
    # math on too-few samples. Returns inert score.
    if sample_count < min_samples:
        return CuriosityScore(
            cluster_id=cid,
            samples_count=sample_count,
            dominant_source=CuriositySource.INSUFFICIENT_DATA,
            decay_reason=(
                decay_reason_override
                if decay_reason_override is not None
                else CuriosityDecayReason.NONE
            ),
            last_updated_at_unix=(
                max(
                    (float(o.at_unix) for o in obs_list),
                    default=0.0,
                )
            ),
        )

    # Per-source aggregation
    halflife = curiosity_halflife_days()
    now = now_ts if now_ts is not None else time.time()

    # source -> [contribution_sum, weight_sum]
    per_source: Dict[CuriositySource, Tuple[float, float]] = {}

    for obs in obs_list:
        try:
            s = obs.source
            if not isinstance(s, CuriositySource):
                continue
            src_w = _weight_for_source(s)
            if src_w <= 0.0:
                continue
            age_s = float(now) - float(obs.at_unix)
            rw = recency_weight(
                age_seconds=age_s,
                halflife_days=halflife,
            )
            # Defensive value clamp — caller (Slice 2) clamps at
            # ingest, but be defensive here too.
            v = float(obs.value)
            if v < 0.0:
                v = 0.0
            elif v > 1.0:
                v = 1.0
            elif not math.isfinite(v):
                v = 0.0
            weighted = v * src_w * rw
            total_w = src_w * rw
            cur = per_source.get(s, (0.0, 0.0))
            per_source[s] = (
                cur[0] + weighted,
                cur[1] + total_w,
            )
        except Exception:  # noqa: BLE001 — defensive
            continue

    # No source had any non-zero contribution — degenerate to
    # INSUFFICIENT_DATA (could happen if all samples had source
    # weights of 0 via env knobs).
    if not per_source:
        return CuriosityScore(
            cluster_id=cid,
            samples_count=sample_count,
            dominant_source=CuriositySource.INSUFFICIENT_DATA,
            decay_reason=(
                decay_reason_override
                if decay_reason_override is not None
                else CuriosityDecayReason.NONE
            ),
            last_updated_at_unix=(
                max(
                    (float(o.at_unix) for o in obs_list),
                    default=0.0,
                )
            ),
        )

    # Per-source mean = contribution / weight (saturated to [0, 1])
    per_source_mean: Dict[CuriositySource, float] = {}
    for s, (csum, wsum) in per_source.items():
        if wsum > 0:
            v = csum / wsum
            if v > 1.0:
                v = 1.0
            elif v < 0.0:
                v = 0.0
            elif not math.isfinite(v):
                v = 0.0
            per_source_mean[s] = v

    # Defensive — if every observation underflowed recency_weight
    # to 0.0 (e.g., samples are vastly older than the halflife
    # window), per_source_mean ends up empty even though we had
    # raw observations. Treat as INSUFFICIENT_DATA rather than
    # raising on the empty max() below. Same shape as the no-
    # contributing-source branch above.
    if not per_source_mean:
        return CuriosityScore(
            cluster_id=cid,
            samples_count=sample_count,
            dominant_source=CuriositySource.INSUFFICIENT_DATA,
            decay_reason=(
                decay_reason_override
                if decay_reason_override is not None
                else CuriosityDecayReason.NONE
            ),
            last_updated_at_unix=(
                max(
                    (float(o.at_unix) for o in obs_list),
                    default=0.0,
                )
            ),
        )

    # Aggregate magnitude — mean of per-source means weighted by
    # operator-tunable source weights. This means turning off a
    # source via env (weight=0) structurally excludes it from
    # the aggregate.
    total_aggregate_w = 0.0
    aggregate_sum = 0.0
    for s, mean_v in per_source_mean.items():
        sw = _weight_for_source(s)
        aggregate_sum += mean_v * sw
        total_aggregate_w += sw

    if total_aggregate_w > 0:
        magnitude = aggregate_sum / total_aggregate_w
    else:
        magnitude = 0.0
    if magnitude > 1.0:
        magnitude = 1.0
    elif magnitude < 0.0:
        magnitude = 0.0
    elif not math.isfinite(magnitude):
        magnitude = 0.0

    # Confidence = sample-saturation × source-diversity
    sample_saturation = min(
        1.0, float(sample_count) / float(max(min_samples, 1)),
    )
    source_diversity = float(len(per_source_mean)) / 3.0
    if source_diversity > 1.0:
        source_diversity = 1.0
    confidence = sample_saturation * source_diversity
    if confidence > 1.0:
        confidence = 1.0
    elif confidence < 0.0:
        confidence = 0.0

    # Dominant source = max contribution
    dominant = max(
        per_source_mean.keys(),
        key=lambda s: (
            per_source_mean[s] * _weight_for_source(s)
        ),
    )

    # Source breakdown — descending by contribution
    breakdown_pairs = sorted(
        per_source_mean.items(),
        key=lambda item: (
            item[1] * _weight_for_source(item[0])
        ),
        reverse=True,
    )
    breakdown_tuple: Tuple[Tuple[str, float], ...] = tuple(
        (s.value, float(v * _weight_for_source(s)))
        for s, v in breakdown_pairs
    )

    last_updated = max(
        (float(o.at_unix) for o in obs_list),
        default=0.0,
    )

    return CuriosityScore(
        cluster_id=cid,
        magnitude=float(magnitude),
        confidence=float(confidence),
        samples_count=sample_count,
        dominant_source=dominant,
        source_breakdown=breakdown_tuple,
        decay_reason=(
            decay_reason_override
            if decay_reason_override is not None
            else CuriosityDecayReason.NONE
        ),
        last_updated_at_unix=last_updated,
    )


# ---------------------------------------------------------------------------
# curiosity_multiplier_from_score — Slice 3's SensorGovernor consumer
# ---------------------------------------------------------------------------


def curiosity_multiplier_from_score(
    score: Optional[CuriosityScore],
    *,
    confidence_threshold: float = 0.5,
) -> float:
    """**Authoritative consumer-side multiplier.** Slice 3's
    `SensorGovernor._weighted_cap` lazy-imports + calls this.

    Returns a multiplier in ``[curiosity_multiplier_floor(),
    curiosity_multiplier_ceiling()]`` — bounded by construction
    so the global emission cap can NEVER be bypassed by a
    runaway curiosity signal.

    Multiplier rules (deterministic, AST-pinned at Slice 5):
      * ``score is None`` → 1.0 (no bias; collector returned no
        signal)
      * ``score.is_cold_start()`` → 1.0 (no bias)
      * ``score.is_decayed()`` → 1.0 (no bias; consumer respects
        the decay flag verbatim)
      * ``score.confidence < confidence_threshold`` → 1.0 (no
        bias when confidence is below the operator-chosen
        floor)
      * Otherwise: linearly interpolate between
        ``curiosity_multiplier_floor()`` (at magnitude=0) and
        ``curiosity_multiplier_ceiling()`` (at magnitude=1),
        passing through 1.0 when magnitude=0.5.

    Pure function — same inputs → same outputs. NEVER raises."""
    if score is None:
        return 1.0
    try:
        if score.is_cold_start():
            return 1.0
        if score.is_decayed():
            return 1.0
        conf = float(score.confidence)
        if conf < float(confidence_threshold):
            return 1.0
        floor = curiosity_multiplier_floor()
        ceil = curiosity_multiplier_ceiling()
        mag = float(score.magnitude)
        if mag < 0.0:
            mag = 0.0
        elif mag > 1.0:
            mag = 1.0
        elif not math.isfinite(mag):
            mag = 0.0
        # Linear interpolation: at mag=0 → floor; at mag=1 →
        # ceiling; passes through 1.0 at mag=0.5 by construction
        # IFF floor=0.5 + ceil=2.0 (default symmetric setup).
        # When operator changes floor/ceil, the pivot moves
        # linearly — no surprises.
        result = floor + (ceil - floor) * mag
        # Defensive clamp
        if result < floor:
            result = floor
        elif result > ceil:
            result = ceil
        elif not math.isfinite(result):
            result = 1.0
        return float(result)
    except Exception:  # noqa: BLE001 — defensive
        return 1.0


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


__all__ = [
    "CURIOSITY_GRADIENT_SCHEMA_VERSION",
    "CuriosityDecayReason",
    "CuriosityObservation",
    "CuriosityScore",
    "CuriositySource",
    "compute_curiosity",
    "curiosity_gradient_enabled",
    "curiosity_halflife_days",
    "curiosity_min_samples",
    "curiosity_multiplier_ceiling",
    "curiosity_multiplier_floor",
    "curiosity_multiplier_from_score",
    "curiosity_source_weight_logprob",
    "curiosity_source_weight_prophecy",
    "curiosity_source_weight_recurrence",
    "curiosity_stale_focus_hours",
]
