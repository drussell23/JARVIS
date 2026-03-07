"""BehavioralHealthMonitor — anomaly detection on autonomous behavior.

Monitors sliding window of triage cycle metrics for:
- Rate spikes (actions per cycle > threshold * rolling mean)
- Error rate spikes (error ratio > threshold * rolling mean)
- Confidence degradation (mean confidence trending down)

Returns typed recommendations. Does NOT mutate control directly.
All thresholds are env-var configurable.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from core.contracts.decision_envelope import DecisionEnvelope


class ThrottleRecommendation(str, Enum):
    NONE = "none"
    REDUCE_BATCH = "reduce_batch"
    PAUSE_CYCLE = "pause_cycle"
    CIRCUIT_BREAK = "circuit_break"


@dataclass(frozen=True)
class BehavioralHealthReport:
    healthy: bool
    anomalies: Tuple[str, ...]
    recommendation: ThrottleRecommendation
    recommended_max_emails: Optional[int]
    confidence: float
    window_cycles: int
    metrics: Dict[str, float]


@dataclass
class _CycleSnapshot:
    """Internal mutable snapshot of a single triage cycle's metrics."""

    emails_processed: int
    error_count: int
    error_ratio: float
    mean_confidence: float
    tier_counts: Dict[int, int]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


class BehavioralHealthMonitor:
    """Sliding-window anomaly detector for autonomous triage cycles.

    Records cycle metrics and checks for rate spikes, error spikes,
    and confidence degradation. Returns typed health reports and
    throttle recommendations without mutating any control state.
    """

    def __init__(self, window_size: int = 0) -> None:
        if window_size <= 0:
            window_size = _env_int("BEHAVIORAL_HEALTH_WINDOW_SIZE", 10)
        self._window_size: int = window_size
        self._snapshots: Deque[_CycleSnapshot] = deque(maxlen=window_size)

        # Thresholds — all env-var configurable
        self._rate_spike_factor: float = _env_float(
            "BEHAVIORAL_HEALTH_RATE_SPIKE_FACTOR", 3.0
        )
        self._error_spike_factor: float = _env_float(
            "BEHAVIORAL_HEALTH_ERROR_SPIKE_FACTOR", 2.0
        )
        self._confidence_slope_threshold: float = _env_float(
            "BEHAVIORAL_HEALTH_CONFIDENCE_SLOPE", -0.05
        )
        self._min_cycles: int = _env_int("BEHAVIORAL_HEALTH_MIN_CYCLES", 3)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_cycle(
        self,
        report: Any,
        envelopes: Sequence[DecisionEnvelope],
    ) -> None:
        """Extract metrics from *report* (duck-typed) and *envelopes*, then store."""
        emails_processed: int = getattr(report, "emails_processed", 0)
        errors: List[str] = list(getattr(report, "errors", []))
        error_count = len(errors)
        error_ratio = error_count / max(emails_processed, 1)
        tier_counts: Dict[int, int] = dict(getattr(report, "tier_counts", {}))

        # Mean confidence from envelopes
        if envelopes:
            mean_confidence = sum(e.confidence for e in envelopes) / len(envelopes)
        else:
            mean_confidence = 0.0

        snapshot = _CycleSnapshot(
            emails_processed=emails_processed,
            error_count=error_count,
            error_ratio=error_ratio,
            mean_confidence=mean_confidence,
            tier_counts=tier_counts,
        )
        self._snapshots.append(snapshot)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def check_health(self) -> BehavioralHealthReport:
        """Analyse the sliding window and return a typed health report."""
        n = len(self._snapshots)

        if n < self._min_cycles:
            return BehavioralHealthReport(
                healthy=True,
                anomalies=(),
                recommendation=ThrottleRecommendation.NONE,
                recommended_max_emails=None,
                confidence=1.0,
                window_cycles=n,
                metrics={},
            )

        anomalies: List[str] = []
        snapshots = list(self._snapshots)
        latest = snapshots[-1]
        previous = snapshots[:-1]

        # --- 1. Rate spike ------------------------------------------------
        if previous:
            mean_rate = sum(s.emails_processed for s in previous) / len(previous)
            if mean_rate > 0 and latest.emails_processed > self._rate_spike_factor * mean_rate:
                anomalies.append(
                    f"Rate spike: {latest.emails_processed} emails vs "
                    f"rolling mean {mean_rate:.1f} "
                    f"(factor {latest.emails_processed / mean_rate:.1f}x)"
                )

        # --- 2. Error rate spike ------------------------------------------
        if previous:
            baseline_mean_err = sum(s.error_ratio for s in previous) / len(previous)
            effective_baseline = max(baseline_mean_err, 0.05)
            if (
                latest.error_ratio > 0.3
                and latest.error_ratio > self._error_spike_factor * effective_baseline
            ):
                anomalies.append(
                    f"Error rate spike: {latest.error_ratio:.2f} vs "
                    f"baseline {baseline_mean_err:.2f} "
                    f"(effective threshold {self._error_spike_factor * effective_baseline:.2f})"
                )

        # --- 3. Confidence degradation (linear regression slope) ----------
        if n >= self._min_cycles:
            slope = self._linear_regression_slope(
                [s.mean_confidence for s in snapshots]
            )
            if slope < self._confidence_slope_threshold:
                anomalies.append(
                    f"Confidence degradation: slope {slope:.4f} "
                    f"(threshold {self._confidence_slope_threshold})"
                )

        # --- Build report -------------------------------------------------
        recommendation, recommended_max = self._compute_recommendation(
            anomalies, snapshots
        )
        healthy = len(anomalies) == 0

        # Confidence in the health assessment itself
        if healthy:
            health_confidence = 1.0
        else:
            health_confidence = max(0.0, 1.0 - 0.25 * len(anomalies))

        metrics: Dict[str, float] = {
            "latest_emails_processed": float(latest.emails_processed),
            "latest_error_ratio": latest.error_ratio,
            "latest_mean_confidence": latest.mean_confidence,
            "window_size": float(n),
        }

        return BehavioralHealthReport(
            healthy=healthy,
            anomalies=tuple(anomalies),
            recommendation=recommendation,
            recommended_max_emails=recommended_max,
            confidence=health_confidence,
            window_cycles=n,
            metrics=metrics,
        )

    # ------------------------------------------------------------------
    # Throttle interface
    # ------------------------------------------------------------------

    def should_throttle(self) -> Tuple[ThrottleRecommendation, Optional[str]]:
        """Return ``(recommendation, reason_string | None)``."""
        report = self.check_health()
        if report.healthy:
            return (ThrottleRecommendation.NONE, None)
        reason = "; ".join(report.anomalies)
        return (report.recommendation, reason)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_recommendation(
        self,
        anomalies: List[str],
        snapshots: List[_CycleSnapshot],
    ) -> Tuple[ThrottleRecommendation, Optional[int]]:
        """Determine throttle recommendation and optional max-emails cap."""
        if not anomalies:
            return (ThrottleRecommendation.NONE, None)

        if len(anomalies) >= 3:
            return (ThrottleRecommendation.CIRCUIT_BREAK, 0)

        previous = snapshots[:-1] if len(snapshots) > 1 else snapshots
        mean_emails = (
            sum(s.emails_processed for s in previous) / len(previous)
            if previous
            else 0
        )

        # Check for specific anomaly types
        has_error = any("error" in a.lower() for a in anomalies)
        has_rate = any("rate" in a.lower() for a in anomalies)

        if has_error:
            # Reduce to 50% of mean
            cap = max(1, int(mean_emails * 0.5))
            return (ThrottleRecommendation.REDUCE_BATCH, cap)

        if has_rate:
            # Reduce to 100% of mean (cap at the mean)
            cap = max(1, int(mean_emails))
            return (ThrottleRecommendation.REDUCE_BATCH, cap)

        # Default: pause cycle for unrecognised anomalies
        return (ThrottleRecommendation.PAUSE_CYCLE, None)

    @staticmethod
    def _linear_regression_slope(values: List[float]) -> float:
        """Compute OLS slope for *values* indexed 0..n-1."""
        n = len(values)
        if n < 2:
            return 0.0
        x_mean = (n - 1) / 2.0
        y_mean = sum(values) / n
        numerator = 0.0
        denominator = 0.0
        for i, y in enumerate(values):
            dx = i - x_mean
            numerator += dx * (y - y_mean)
            denominator += dx * dx
        if denominator == 0.0:
            return 0.0
        return numerator / denominator
