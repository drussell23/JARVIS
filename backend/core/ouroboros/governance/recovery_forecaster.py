"""recovery_forecaster.py -- EWMA-MTTR + velocity gradient recovery forecaster.

Phase 2 of the Sovereign Provider Failover Lifecycle.

Design
------
- Reads CLOSED outage records (duration_s not None) from the OutageLedger.
- Computes EWMA-MTTR + p50/p90 percentile bands from a bounded recent window.
- DATA POVERTY OVERRIDE (load-bearing): if N < JARVIS_FORECAST_MIN_SAMPLES
  the confidence is "LOW_CONFIDENCE" regardless of the computed bands.
  Throttle consumers MUST ignore p50/p90 on LOW_CONFIDENCE and use the safe
  polling interval instead.
- Within-outage velocity gradient: a falling probe-latency trajectory biases
  the velocity_hint below 1.0 (recovery looks imminent).
- Fail-soft: any internal error returns a conservative LOW_CONFIDENCE forecast.
- Pure: stdlib + math only. No I/O beyond reading the ledger.

Env gates
---------
JARVIS_RECOVERY_FORECAST_ENABLED   default "true"
    OFF -> always returns LOW_CONFIDENCE conservative default.
JARVIS_FORECAST_EWMA_ALPHA         default "0.4"
    EWMA smoothing factor (0 < alpha <= 1).
JARVIS_FORECAST_MIN_SAMPLES        default "5"
    Minimum closed-outage records required for HIGH confidence.
JARVIS_FORECAST_WINDOW             default "20"
    Maximum recent closed-outage records examined for percentile bands.
JARVIS_FORECAST_DEFAULT_P50_S      default "300"
    Conservative p50 returned when LOW_CONFIDENCE.
JARVIS_FORECAST_DEFAULT_P90_S      default "600"
    Conservative p90 returned when LOW_CONFIDENCE.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: str = "true") -> bool:
    val = os.environ.get(name, default).strip().lower()
    return val not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _forecast_enabled() -> bool:
    return _env_bool("JARVIS_RECOVERY_FORECAST_ENABLED", "true")


def _ewma_alpha() -> float:
    v = _env_float("JARVIS_FORECAST_EWMA_ALPHA", 0.4)
    # Clamp to (0, 1] -- alpha=0 means "no update" which is degenerate.
    return max(1e-4, min(1.0, v))


def _min_samples() -> int:
    return max(1, _env_int("JARVIS_FORECAST_MIN_SAMPLES", 5))


def _window_size() -> int:
    return max(1, _env_int("JARVIS_FORECAST_WINDOW", 20))


def _default_p50() -> float:
    return max(1.0, _env_float("JARVIS_FORECAST_DEFAULT_P50_S", 300.0))


def _default_p90() -> float:
    return max(1.0, _env_float("JARVIS_FORECAST_DEFAULT_P90_S", 600.0))


# ---------------------------------------------------------------------------
# RecoveryForecast dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecoveryForecast:
    """Immutable forecast value object consumed by the recovery throttle.

    Fields
    ------
    p50_s:
        Estimated 50th-percentile outage duration (seconds).
    p90_s:
        Estimated 90th-percentile outage duration (seconds). Always >= p50_s.
    samples:
        Number of CLOSED outage records used to compute this forecast.
    confidence:
        "HIGH" when samples >= JARVIS_FORECAST_MIN_SAMPLES,
        "LOW_CONFIDENCE" otherwise.  The throttle MUST ignore p50/p90 on
        LOW_CONFIDENCE and fall back to the safe polling interval.
    velocity_hint:
        1.0 = neutral (no live trajectory or flat).
        < 1.0 = recovery looks imminent (falling latency / sporadic successes).
        The throttle multiplies the raw interval by velocity_hint to bias
        probing more aggressively when the gradient turns positive.
    """

    p50_s: float
    p90_s: float
    samples: int
    confidence: str  # "HIGH" | "LOW_CONFIDENCE"
    velocity_hint: float  # 1.0 = neutral; <1.0 = recovery imminent

    def to_dict(self) -> dict:
        return {
            "p50_s": self.p50_s,
            "p90_s": self.p90_s,
            "samples": self.samples,
            "confidence": self.confidence,
            "velocity_hint": self.velocity_hint,
        }


def _conservative_default() -> RecoveryForecast:
    """Conservative LOW_CONFIDENCE default (fail-soft fallback)."""
    return RecoveryForecast(
        p50_s=_default_p50(),
        p90_s=_default_p90(),
        samples=0,
        confidence="LOW_CONFIDENCE",
        velocity_hint=1.0,
    )


# ---------------------------------------------------------------------------
# Internal computation helpers
# ---------------------------------------------------------------------------

def _compute_percentile(sorted_values: List[float], pct: float) -> float:
    """Return the p-th percentile (0..100) from a sorted list.

    Uses linear interpolation (nearest-rank inclusive).  Requires len >= 1.
    """
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_values[0]
    # Clamp percentile to [0, 100]
    pct = max(0.0, min(100.0, pct))
    # Index into the sorted list (0-based, linear interpolation)
    rank = pct / 100.0 * (n - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return sorted_values[lo]
    # Linear interpolation
    frac = rank - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _compute_ewma(durations: List[float], alpha: float) -> float:
    """Compute EWMA of *durations* (oldest first).  Returns the final value."""
    if not durations:
        return 0.0
    ewma = durations[0]
    for d in durations[1:]:
        ewma = alpha * d + (1.0 - alpha) * ewma
    return ewma


def _velocity_hint_from_trajectory(trajectory: List[float]) -> float:
    """Compute velocity_hint from a list of recent probe latencies.

    A falling trajectory (mean of second half < mean of first half) indicates
    recovery is imminent -> return < 1.0.

    Returns 1.0 if the trajectory is absent, too short to split, or flat/rising.
    The hint is clamped to [0.1, 1.0] to avoid absurdly short intervals.

    Algorithm: split the trajectory in half; compare means.  If the second
    half is meaningfully lower (by >= VELOCITY_DROP_THRESHOLD relative to the
    first half), interpolate a hint in [0.5, 1.0].
    """
    _VELOCITY_DROP_THRESHOLD = 0.05  # 5% relative drop is "meaningful"
    _MIN_HINT = 0.5  # floor: never compress by more than 50%

    if not trajectory or len(trajectory) < 2:
        return 1.0

    mid = len(trajectory) // 2
    first_half = trajectory[:mid]
    second_half = trajectory[mid:]

    mean_first = sum(first_half) / len(first_half)
    mean_second = sum(second_half) / len(second_half)

    if mean_first <= 0.0:
        return 1.0

    relative_drop = (mean_first - mean_second) / mean_first

    if relative_drop <= _VELOCITY_DROP_THRESHOLD:
        # Flat or rising trajectory -> neutral
        return 1.0

    # Map relative_drop in [threshold, 1.0] -> hint in [1.0, _MIN_HINT]
    # Higher drop -> lower hint (more aggressive polling)
    # relative_drop capped at 1.0 for safety
    clamped = min(1.0, relative_drop)
    # Linear interpolation: drop=threshold -> 1.0, drop=1.0 -> _MIN_HINT
    t = (clamped - _VELOCITY_DROP_THRESHOLD) / (1.0 - _VELOCITY_DROP_THRESHOLD)
    hint = 1.0 - t * (1.0 - _MIN_HINT)
    return max(_MIN_HINT, min(1.0, hint))


# ---------------------------------------------------------------------------
# RecoveryForecaster
# ---------------------------------------------------------------------------

class RecoveryForecaster:
    """EWMA-MTTR + percentile band recovery forecaster.

    Reads the OutageLedger for CLOSED outage records (duration_s not None).
    Applies the DATA POVERTY OVERRIDE: fewer than min_samples -> LOW_CONFIDENCE.
    Optionally incorporates a live probe trajectory for velocity biasing.
    """

    def forecast(
        self,
        *,
        live_probe_trajectory: Optional[List[float]] = None,
    ) -> RecoveryForecast:
        """Compute a RecoveryForecast from ledger history + optional trajectory.

        Parameters
        ----------
        live_probe_trajectory:
            Recent probe latencies (seconds) from the *current* live outage,
            oldest-first.  None or empty -> velocity_hint = 1.0.

        Returns
        -------
        RecoveryForecast
            Always returns a forecast (fail-soft, never raises).
        """
        if not _forecast_enabled():
            return _conservative_default()

        try:
            return self._compute(live_probe_trajectory=live_probe_trajectory)
        except Exception as exc:
            logger.warning(
                "[RecoveryForecaster] forecast fail-soft err=%r", exc
            )
            return _conservative_default()

    def _compute(
        self,
        *,
        live_probe_trajectory: Optional[List[float]],
    ) -> RecoveryForecast:
        from backend.core.ouroboros.governance.outage_ledger import (  # noqa: PLC0415
            get_outage_ledger,
        )

        ledger = get_outage_ledger()
        all_records = ledger.recent(_window_size())

        # Filter to CLOSED records only (duration_s is not None)
        closed = [r for r in all_records if r.duration_s is not None]

        n = len(closed)
        alpha = _ewma_alpha()
        min_n = _min_samples()

        # DATA POVERTY OVERRIDE: insufficient samples -> LOW_CONFIDENCE
        confidence = "HIGH" if n >= min_n else "LOW_CONFIDENCE"

        # Compute EWMA-MTTR and percentile bands from closed durations
        if n == 0:
            # No data at all -> conservative default, LOW_CONFIDENCE
            return _conservative_default()

        durations = [r.duration_s for r in closed]  # type: ignore[misc]

        # EWMA (informational; drives the mean estimate)
        _ewma_val = _compute_ewma(durations, alpha)  # noqa: F841 -- used in future v2

        # Percentile bands
        sorted_d = sorted(durations)
        p50 = _compute_percentile(sorted_d, 50.0)
        p90 = _compute_percentile(sorted_d, 90.0)

        # Ensure p90 >= p50 (can be equal with small N)
        p90 = max(p90, p50)

        # Velocity hint from live probe trajectory
        velocity_hint = _velocity_hint_from_trajectory(
            live_probe_trajectory or []
        )

        return RecoveryForecast(
            p50_s=p50,
            p90_s=p90,
            samples=n,
            confidence=confidence,
            velocity_hint=velocity_hint,
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_singleton: Optional[RecoveryForecaster] = None


def get_recovery_forecaster() -> RecoveryForecaster:
    """Return (or lazily create) the process-wide RecoveryForecaster singleton."""
    global _singleton  # noqa: PLW0603
    if _singleton is None:
        _singleton = RecoveryForecaster()
    return _singleton
