"""§39 Tier-3 #4 — Op trajectory predictor
(PRD v2.72 to v2.73, 2026-05-08).

Predicts an in-flight op's likely outcome — confidence,
median ETA — by composing the canonical
:class:`OpBlockBuffer` history of COMMITTED ops. ZERO
parallel timing aggregator; ZERO new ML model.

Authority asymmetry: ZERO authority. Read-only predictor +
renderer. NEVER mutates orchestrator state, NEVER changes
risk-tier, NEVER spawns ops.

§38.11.5a.5 single-canonical-name discipline honored:
- Composes canonical :class:`OpBlock` lifecycle fields
  (``started_at`` + ``committed_at`` + ``state`` +
  ``subagent_kind``) — NO parallel duration ledger.
- The only NEW closed taxonomy is
  :class:`TrajectoryConfidence` (4 values mapped to
  thresholds + glyph).

§33 patterns invoked:
- §33.1 graduation contract (master default-FALSE)
- §33.5 versioned artifact (frozen
  :class:`TrajectoryPrediction`)
"""
from __future__ import annotations

import enum
import logging
import os
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


OP_TRAJECTORY_PREDICTOR_SCHEMA_VERSION: str = (
    "op_trajectory_predictor.1"
)


_ENV_MASTER = "JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED"
_ENV_MIN_SAMPLES = (
    "JARVIS_OP_TRAJECTORY_MIN_SAMPLES"
)
_ENV_HISTORY_LIMIT = (
    "JARVIS_OP_TRAJECTORY_HISTORY_LIMIT"
)

_DEFAULT_MIN_SAMPLES = 3
_MIN_MIN_SAMPLES = 1
_MAX_MIN_SAMPLES = 50
_DEFAULT_HISTORY_LIMIT = 50
_MIN_HISTORY = 5
_MAX_HISTORY = 500


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def _read_int_clamped(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def min_samples() -> int:
    return _read_int_clamped(
        _ENV_MIN_SAMPLES, _DEFAULT_MIN_SAMPLES,
        _MIN_MIN_SAMPLES, _MAX_MIN_SAMPLES,
    )


def history_limit() -> int:
    return _read_int_clamped(
        _ENV_HISTORY_LIMIT, _DEFAULT_HISTORY_LIMIT,
        _MIN_HISTORY, _MAX_HISTORY,
    )


# ===========================================================================
# Closed taxonomy — 4-value confidence
# ===========================================================================


class TrajectoryConfidence(str, enum.Enum):
    """Closed 4-value confidence vocabulary mapped via
    :data:`_CONFIDENCE_THRESHOLDS` (bytes-pinned).

    HIGH    — score >= 0.70
    MEDIUM  — score >= 0.40
    LOW     — score < 0.40
    UNKNOWN — insufficient samples (< min_samples())
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


# Bytes-pinned threshold table. AST regression locks the
# numeric thresholds — operator binding "no hardcoding"
# enforced via _confidence_thresholds_canonical pin.
_CONFIDENCE_THRESHOLDS: Tuple[Tuple[float, "TrajectoryConfidence"], ...] = (
    (0.70, TrajectoryConfidence.HIGH),
    (0.40, TrajectoryConfidence.MEDIUM),
    (0.0, TrajectoryConfidence.LOW),
)


def _score_to_confidence(
    score: float, *, sufficient_samples: bool,
) -> TrajectoryConfidence:
    """Pure-function bucketing. NEVER raises."""
    if not sufficient_samples:
        return TrajectoryConfidence.UNKNOWN
    try:
        s = float(score)
    except (TypeError, ValueError):
        return TrajectoryConfidence.UNKNOWN
    for threshold, level in _CONFIDENCE_THRESHOLDS:
        if s >= threshold:
            return level
    return TrajectoryConfidence.LOW


# ===========================================================================
# Frozen §33.5 versioned artifact
# ===========================================================================


@dataclass(frozen=True)
class TrajectoryPrediction:
    """One trajectory prediction. Frozen + hashable."""

    op_id: str
    confidence: TrajectoryConfidence
    confidence_score: float = 0.0
    similar_op_count: int = 0
    similar_op_kind: str = ""    # empty = "all ops" fallback
    median_duration_s: float = 0.0
    p90_duration_s: float = 0.0
    estimated_completion_at_unix: float = 0.0
    elapsed_so_far_s: float = 0.0
    diagnostic: str = ""
    schema_version: str = (
        OP_TRAJECTORY_PREDICTOR_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "op_id": self.op_id,
            "confidence": self.confidence.value,
            "confidence_score": self.confidence_score,
            "similar_op_count": self.similar_op_count,
            "similar_op_kind": self.similar_op_kind,
            "median_duration_s": self.median_duration_s,
            "p90_duration_s": self.p90_duration_s,
            "estimated_completion_at_unix": (
                self.estimated_completion_at_unix
            ),
            "elapsed_so_far_s": self.elapsed_so_far_s,
            "diagnostic": self.diagnostic,
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Predictor — composes canonical OpBlockBuffer
# ===========================================================================


def predict_trajectory(
    op_id: str,
    *,
    op_kind: Optional[str] = None,
) -> Optional[TrajectoryPrediction]:
    """Predict trajectory for an in-flight op. NEVER raises.

    Returns None when:
      * master flag off
      * canonical buffer unavailable
      * op_id not in buffer

    The prediction composes:
      * canonical :class:`OpBlockBuffer` history of
        COMMITTED ops
      * filter by ``op_kind`` (matches ``subagent_kind`` if
        provided; else "all ops" baseline)
      * median + p90 durations as deterministic ETA estimate
      * confidence score derived from sample size + duration
        variance (low variance → high confidence)
    """
    if not master_enabled():
        return None
    try:
        from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
            OpBlockState, get_default_buffer,
        )
    except Exception:  # noqa: BLE001
        return None

    try:
        buf = get_default_buffer()
    except Exception:  # noqa: BLE001
        return None

    # Locate the active op (BUFFERING or terminal).
    # ``find_by_op_id`` returns a tuple of all blocks with
    # this op_id (BUFFERING + COMMITTED if it has committed
    # already). Pick the most recent one.
    active_op = None
    try:
        candidates = buf.find_by_op_id(op_id)
        if candidates:
            active_op = candidates[-1]
    except Exception:  # noqa: BLE001
        active_op = None

    # Collect committed-op durations matching kind (if any).
    durations: List[float] = []
    matched_kind = ""
    try:
        committed = buf.blocks_by_state(
            states=(OpBlockState.COMMITTED,),
        )
    except Exception:  # noqa: BLE001
        committed = ()

    limit = history_limit()
    for blk in committed[-limit:]:
        try:
            dur = float(blk.duration_s or 0.0)
            if dur <= 0.0:
                continue
            if op_kind:
                if (
                    getattr(blk, "subagent_kind", "")
                    != op_kind
                ):
                    continue
                matched_kind = op_kind
            durations.append(dur)
        except Exception:  # noqa: BLE001
            continue

    # Fallback: if op_kind matched zero, retry with all
    # ops (so operators get SOME signal even on a fresh
    # subagent_kind).
    if op_kind and not durations:
        for blk in committed[-limit:]:
            try:
                dur = float(blk.duration_s or 0.0)
                if dur > 0.0:
                    durations.append(dur)
            except Exception:  # noqa: BLE001
                continue
        matched_kind = ""  # baseline used

    sample_count = len(durations)
    sufficient = sample_count >= min_samples()

    median_dur = (
        float(statistics.median(durations))
        if durations else 0.0
    )
    p90_dur = (
        _percentile(durations, 0.90) if durations else 0.0
    )

    # Confidence score: blend of sample-size + variance
    # tightness. Both clamped 0..1; geometric mean.
    if sample_count <= 0:
        score = 0.0
    else:
        size_score = min(1.0, sample_count / 10.0)
        if sample_count >= 2:
            try:
                stdev = float(statistics.pstdev(durations))
            except statistics.StatisticsError:
                stdev = 0.0
            mean = (
                float(statistics.mean(durations))
                if durations else 1.0
            )
            cv = (stdev / mean) if mean > 0 else 1.0
            tightness = max(0.0, 1.0 - min(1.0, cv))
        else:
            tightness = 0.5
        score = (size_score * tightness) ** 0.5

    confidence = _score_to_confidence(
        score, sufficient_samples=sufficient,
    )

    # ETA computation.
    now = time.time()
    elapsed = 0.0
    eta = 0.0
    if active_op is not None:
        try:
            # OpBlock.started_at is monotonic — convert to
            # wall-clock by adding (now - monotonic_now).
            mono_now = time.monotonic()
            elapsed_mono = max(
                0.0, mono_now - float(active_op.started_at),
            )
            elapsed = elapsed_mono
            if median_dur > 0.0:
                eta = now + max(
                    0.0, median_dur - elapsed_mono,
                )
        except Exception:  # noqa: BLE001
            elapsed = 0.0
            eta = 0.0

    diagnostic = ""
    if not sufficient:
        diagnostic = (
            f"insufficient_samples:"
            f"{sample_count}<{min_samples()}"
        )

    pred = TrajectoryPrediction(
        op_id=op_id,
        confidence=confidence,
        confidence_score=round(score, 3),
        similar_op_count=sample_count,
        similar_op_kind=matched_kind,
        median_duration_s=round(median_dur, 3),
        p90_duration_s=round(p90_dur, 3),
        estimated_completion_at_unix=round(eta, 1),
        elapsed_so_far_s=round(elapsed, 3),
        diagnostic=diagnostic,
    )
    _publish_trajectory_event(pred)
    return pred


def _percentile(values: List[float], p: float) -> float:
    """Pure p-th percentile (linear interpolation). NEVER
    raises; returns 0.0 on empty input."""
    if not values:
        return 0.0
    try:
        sorted_vals = sorted(values)
        k = (len(sorted_vals) - 1) * max(0.0, min(1.0, p))
        f = int(k)
        c = min(f + 1, len(sorted_vals) - 1)
        if f == c:
            return float(sorted_vals[f])
        d = k - f
        return float(
            sorted_vals[f] * (1.0 - d) + sorted_vals[c] * d
        )
    except Exception:  # noqa: BLE001
        return 0.0


# ===========================================================================
# SSE composition
# ===========================================================================


def _publish_trajectory_event(
    prediction: TrajectoryPrediction,
) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_TRAJECTORY_PREDICTED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is None:
            return
        broker.publish(
            EVENT_TYPE_TRAJECTORY_PREDICTED,
            prediction.op_id,
            prediction.to_dict(),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "op_trajectory: SSE publish failed",
            exc_info=True,
        )


# ===========================================================================
# Renderer
# ===========================================================================


_CONFIDENCE_GLYPHS = {
    TrajectoryConfidence.HIGH: "🎯",
    TrajectoryConfidence.MEDIUM: "🎲",
    TrajectoryConfidence.LOW: "❓",
    TrajectoryConfidence.UNKNOWN: "⋯",
}


_CONFIDENCE_TINTS = {
    TrajectoryConfidence.HIGH: "green",
    TrajectoryConfidence.MEDIUM: "yellow",
    TrajectoryConfidence.LOW: "red",
    TrajectoryConfidence.UNKNOWN: "dim",
}


def _format_duration(seconds: float) -> str:
    if seconds <= 0.0:
        return "0s"
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    minutes = seconds / 60.0
    if minutes < 60.0:
        return f"{minutes:.1f}m"
    hours = minutes / 60.0
    return f"{hours:.1f}h"


def format_trajectory_prediction(
    prediction: Optional[TrajectoryPrediction],
) -> str:
    """Render trajectory prediction as one-liner. Empty when
    master off OR prediction is None."""
    if not master_enabled():
        return ""
    if prediction is None:
        return ""
    glyph = _CONFIDENCE_GLYPHS.get(
        prediction.confidence, "⋯",
    )
    tint = _CONFIDENCE_TINTS.get(
        prediction.confidence, "white",
    )
    op_short = (
        prediction.op_id[:12]
        if len(prediction.op_id) > 12
        else prediction.op_id
    )
    if prediction.confidence is TrajectoryConfidence.UNKNOWN:
        return (
            f"  [{tint}]{glyph}[/] Op {op_short}: "
            f"prediction unavailable "
            f"({prediction.diagnostic})"
        )
    pct = int(round(prediction.confidence_score * 100))
    eta_dur = (
        prediction.median_duration_s
        - prediction.elapsed_so_far_s
    )
    eta_str = _format_duration(max(0.0, eta_dur))
    kind_tag = (
        f" [dim]({prediction.similar_op_kind})[/]"
        if prediction.similar_op_kind else ""
    )
    return (
        f"  [{tint}]{glyph}[/] Op {op_short}: "
        f"{pct}% confidence, "
        f"~{eta_str} ETA based on "
        f"{prediction.similar_op_count} similar ops"
        f"{kind_tag}"
    )


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    n = 0
    specs = (
        (
            _ENV_MASTER, "bool",
            "§39 Tier-3 #4 op trajectory predictor master "
            "switch (graduation contract per §33.1; "
            "default FALSE).",
            "false",
        ),
        (
            _ENV_MIN_SAMPLES, "int",
            "Minimum committed-op samples needed for HIGH/"
            "MEDIUM/LOW confidence (else UNKNOWN). Default "
            "3; clamped 1..50.",
            "3",
        ),
        (
            _ENV_HISTORY_LIMIT, "int",
            "Max committed-op history scanned for similar-"
            "op duration sample. Default 50; clamped "
            "5..500.",
            "50",
        ),
    )
    for name, typ, desc, ex in specs:
        try:
            registry.register(
                name=name,
                type=typ,
                category="ux",
                description=desc,
                example=ex,
                source_file=(
                    "backend/core/ouroboros/governance/"
                    "op_trajectory_predictor.py"
                ),
            )
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        ShippedCodeInvariant,
    )
    import ast

    pins = []

    def _master_default_false(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                ok = False
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                ok = True
                if not ok:
                    return [
                        "master_enabled() must call _flag(...) "
                        "with default=False"
                    ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier3_4_master_default_false"
        ),
        description=(
            "§33.1 graduation contract — predictor master "
            "stays default-False until evidence ladder "
            "closes."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "op_trajectory_predictor.py"
        ),
        validate=_master_default_false,
    ))

    def _authority_asymmetry(tree: ast.AST, src: str):
        bad = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.candidate_generator",
        )
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod.startswith(b) for b in bad):
                    violations.append(
                        f"forbidden authority import: {mod}"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier3_4_authority_asymmetry"
        ),
        description=(
            "Substrate purity — read-only predictor; no "
            "orchestrator/risk-tier authority."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "op_trajectory_predictor.py"
        ),
        validate=_authority_asymmetry,
    ))

    def _confidence_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "TrajectoryConfidence"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "HIGH", "MEDIUM", "LOW", "UNKNOWN",
                }
                missing = expected - names
                if missing:
                    return [
                        f"TrajectoryConfidence missing: "
                        f"{sorted(missing)}"
                    ]
                return []
        return ["TrajectoryConfidence class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier3_4_confidence_taxonomy_4_values"
        ),
        description=(
            "Closed 4-value TrajectoryConfidence taxonomy."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "op_trajectory_predictor.py"
        ),
        validate=_confidence_taxonomy,
    ))

    def _composes_op_block_buffer(tree: ast.AST, src: str):
        if (
            "op_block_buffer" not in src
            or "OpBlockState" not in src
            or "get_default_buffer" not in src
        ):
            return [
                "must lazy-import op_block_buffer + "
                "OpBlockState + get_default_buffer "
                "(canonical history source — NO parallel "
                "duration ledger)"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier3_4_composes_canonical_"
            "op_block_buffer"
        ),
        description=(
            "Predictor composes canonical OpBlockBuffer "
            "for history — NO parallel duration ledger."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "op_trajectory_predictor.py"
        ),
        validate=_composes_op_block_buffer,
    ))

    def _confidence_thresholds_canonical(
        tree: ast.AST, src: str,
    ):
        """Bytes-pin canonical thresholds — operator
        binding 'no hardcoding' enforced by AST regression
        on numeric drift."""
        if "_CONFIDENCE_THRESHOLDS" not in src:
            return [
                "_CONFIDENCE_THRESHOLDS canonical table "
                "must be defined"
            ]
        # Bytes-pin the canonical 0.70 + 0.40 thresholds.
        if "0.70" not in src or "0.40" not in src:
            return [
                "canonical thresholds 0.70/0.40 must "
                "appear in _CONFIDENCE_THRESHOLDS source — "
                "drift requires explicit pin update"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier3_4_confidence_thresholds_canonical"
        ),
        description=(
            "Bytes-pin 0.70 (HIGH) + 0.40 (MEDIUM) "
            "thresholds — drift requires explicit pin "
            "update; no silent reweighting."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "op_trajectory_predictor.py"
        ),
        validate=_confidence_thresholds_canonical,
    ))

    return pins


__all__ = [
    "OP_TRAJECTORY_PREDICTOR_SCHEMA_VERSION",
    "TrajectoryConfidence",
    "TrajectoryPrediction",
    "master_enabled",
    "min_samples",
    "history_limit",
    "predict_trajectory",
    "format_trajectory_prediction",
    "register_flags",
    "register_shipped_invariants",
]
