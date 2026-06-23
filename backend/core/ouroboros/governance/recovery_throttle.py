"""recovery_throttle.py -- Adaptive DW-recovery probe interval (Phase 2).

Design
------
Computes the probe interval for DW-recovery probing using the RecoveryForecast.
Reuses ``circuit_breaker.full_jitter_delay`` for the post-p90 exponential
backoff -- no reimplementation of backoff/jitter.

DATA POVERTY OVERRIDE (load-bearing, first gate):
    If forecast.confidence == "LOW_CONFIDENCE" -> return the safe polling
    interval (env JARVIS_SAFE_POLLING_INTERVAL_S, default 60.0) IMMEDIATELY,
    ignoring all EWMA / p50 / p90 math.  This prevents wild intervals on N=1.
    This check fires BEFORE any percentile math.

HIGH confidence interval curve (spec §3):
    t < p50  -> decelerate toward I_max  (sparse probing; unlikely to recover)
    p50<=t<=p90 -> I_min  (dense probing; statistically likely window)
    t > p90  -> exponential backoff via full_jitter_delay (anomalous outage)

The raw interval is multiplied by forecast.velocity_hint to bias probing more
aggressively when the live trajectory shows recovery is imminent.

Result is clamped to [I_min, I_max].

Env gates
---------
JARVIS_RECOVERY_THROTTLE_ENABLED      default "true"
    OFF -> always returns JARVIS_SAFE_POLLING_INTERVAL_S.
JARVIS_SAFE_POLLING_INTERVAL_S        default "60.0"
    Returned on LOW_CONFIDENCE or when throttle is OFF.
JARVIS_PROBE_INTERVAL_MIN_S           default "15.0"
    Lower clamp: densest probe interval.
JARVIS_PROBE_INTERVAL_MAX_S           default "300.0"
    Upper clamp: sparsest probe interval.
JARVIS_THROTTLE_BACKOFF_BASE_S        default "15.0"
    Base delay for full_jitter_delay in the post-p90 backoff regime.

Pure: stdlib + math + reuses full_jitter_delay.  No I/O.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

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


def _throttle_enabled() -> bool:
    return _env_bool("JARVIS_RECOVERY_THROTTLE_ENABLED", "true")


def _safe_interval() -> float:
    return max(1.0, _env_float("JARVIS_SAFE_POLLING_INTERVAL_S", 60.0))


def _i_min() -> float:
    return max(1.0, _env_float("JARVIS_PROBE_INTERVAL_MIN_S", 15.0))


def _i_max() -> float:
    return max(1.0, _env_float("JARVIS_PROBE_INTERVAL_MAX_S", 300.0))


def _backoff_base() -> float:
    return max(1.0, _env_float("JARVIS_THROTTLE_BACKOFF_BASE_S", 15.0))


# ---------------------------------------------------------------------------
# ThrottleConfig (injectable in tests)
# ---------------------------------------------------------------------------

class ThrottleConfig:
    """Optional injectable config for probe_interval.

    Reads env defaults if not provided.  All attributes are read lazily.
    """

    def __init__(
        self,
        *,
        safe_interval_s: Optional[float] = None,
        i_min_s: Optional[float] = None,
        i_max_s: Optional[float] = None,
        backoff_base_s: Optional[float] = None,
    ) -> None:
        self._safe = safe_interval_s
        self._min = i_min_s
        self._max = i_max_s
        self._base = backoff_base_s

    @property
    def safe_s(self) -> float:
        return self._safe if self._safe is not None else _safe_interval()

    @property
    def min_s(self) -> float:
        return self._min if self._min is not None else _i_min()

    @property
    def max_s(self) -> float:
        return self._max if self._max is not None else _i_max()

    @property
    def base_s(self) -> float:
        return self._base if self._base is not None else _backoff_base()


# ---------------------------------------------------------------------------
# Lazy import of full_jitter_delay
# ---------------------------------------------------------------------------

def _get_full_jitter_delay():  # type: ignore[return]
    """Lazy import of full_jitter_delay to avoid import cycles."""
    from backend.core.ouroboros.governance.circuit_breaker import (  # noqa: PLC0415
        full_jitter_delay,
    )
    return full_jitter_delay


# ---------------------------------------------------------------------------
# probe_interval -- the public API
# ---------------------------------------------------------------------------

def probe_interval(
    t_outage_s: float,
    forecast: Any,  # RecoveryForecast (typed loosely to keep pure module)
    *,
    cfg: Optional[ThrottleConfig] = None,
) -> float:
    """Compute the DW-recovery probe interval.

    Parameters
    ----------
    t_outage_s:
        Current outage elapsed time in seconds (monotonic clock delta).
    forecast:
        A RecoveryForecast (or duck-typed equivalent with .confidence,
        .p50_s, .p90_s, .velocity_hint attributes).
    cfg:
        Optional ThrottleConfig; reads env defaults when None.

    Returns
    -------
    float
        Probe interval in seconds, clamped to [I_min, I_max].
        Returns safe_interval on LOW_CONFIDENCE or when throttle is OFF.

    Never raises (fail-soft).
    """
    if not _throttle_enabled():
        return _safe_interval()

    try:
        return _compute(t_outage_s, forecast, cfg=cfg)
    except Exception as exc:
        logger.warning("[RecoveryThrottle] probe_interval fail-soft err=%r", exc)
        return _safe_interval()


def _compute(
    t_outage_s: float,
    forecast: Any,
    *,
    cfg: Optional[ThrottleConfig],
) -> float:
    c = cfg if cfg is not None else ThrottleConfig()

    # ------------------------------------------------------------------
    # DATA POVERTY OVERRIDE -- must be the very first gate
    # ------------------------------------------------------------------
    if getattr(forecast, "confidence", "LOW_CONFIDENCE") == "LOW_CONFIDENCE":
        return c.safe_s

    # ------------------------------------------------------------------
    # Extract HIGH-confidence forecast values
    # ------------------------------------------------------------------
    p50: float = float(getattr(forecast, "p50_s", c.safe_s))
    p90: float = float(getattr(forecast, "p90_s", c.safe_s))
    velocity_hint: float = float(getattr(forecast, "velocity_hint", 1.0))

    # Clamp velocity_hint to (0, 1] -- never negative, never amplifying
    velocity_hint = max(1e-3, min(1.0, velocity_hint))

    i_min = c.min_s
    i_max = c.max_s
    # Ensure i_max >= i_min (defensive)
    if i_max < i_min:
        i_max = i_min

    # ------------------------------------------------------------------
    # Three-region curve (spec §3)
    # ------------------------------------------------------------------
    t = max(0.0, float(t_outage_s))

    if t < p50:
        # Pre-p50: decelerate toward I_max. Larger delta -> larger interval.
        # Scale linearly: when t=0 the interval approaches i_max;
        # as t->p50 the interval approaches i_min.
        if p50 <= 0.0:
            raw = i_max
        else:
            # k_pre * (p50 - t): proportional to remaining time to p50
            # We want: t=0 -> i_max, t=p50 -> i_min
            frac = (p50 - t) / p50  # 1.0 at t=0, 0.0 at t=p50
            raw = i_min + frac * (i_max - i_min)

    elif t <= p90:
        # In [p50, p90]: dense probing at I_min
        raw = i_min

    else:
        # Post-p90: exponential backoff via full_jitter_delay.
        # attempt = how many p90-sized steps past p90 we are.
        # Use p90 as the unit; attempt 0 = just past p90, +1 per p90-length.
        step = p90 if p90 > 0.0 else 60.0
        overshoot = t - p90
        attempt = int(overshoot / step)

        try:
            full_jitter = _get_full_jitter_delay()
            raw = full_jitter(
                attempt,
                base_s=c.base_s,
                cap_s=i_max,
            )
        except Exception as exc:
            logger.warning(
                "[RecoveryThrottle] full_jitter_delay fail-soft err=%r", exc
            )
            # Fallback: geometric growth capped at i_max
            raw = min(i_max, c.base_s * (2 ** attempt))

        # Ensure post-p90 result is at least i_min (jitter can produce near-0)
        raw = max(i_min, raw)

    # ------------------------------------------------------------------
    # Apply velocity_hint and clamp
    # ------------------------------------------------------------------
    compressed = raw * velocity_hint
    return max(i_min, min(i_max, compressed))
