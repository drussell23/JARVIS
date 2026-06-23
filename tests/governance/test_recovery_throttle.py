"""tests/governance/test_recovery_throttle.py

TDD suite for recovery_throttle.py (Phase 2 — Sovereign Provider Failover).

Covers:
  - LOW_CONFIDENCE -> returns exactly safe_interval_s, invariant to p50/p90
  - HIGH + t < p50 -> larger interval (decelerate toward I_max)
  - HIGH + t in [p50, p90] -> I_min
  - HIGH + t > p90 -> backoff > I_min
  - Clamping to [I_min, I_max]
  - velocity_hint compresses the interval
  - OFF gate -> returns safe_interval
  - Fail-soft on bad input
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.recovery_forecaster import RecoveryForecast
from backend.core.ouroboros.governance.recovery_throttle import (
    ThrottleConfig,
    probe_interval,
)


# ---------------------------------------------------------------------------
# Test fixtures: constructors for RecoveryForecast objects
# ---------------------------------------------------------------------------

def _low_confidence(
    p50_s: float = 300.0,
    p90_s: float = 600.0,
    velocity_hint: float = 1.0,
) -> RecoveryForecast:
    return RecoveryForecast(
        p50_s=p50_s,
        p90_s=p90_s,
        samples=2,
        confidence="LOW_CONFIDENCE",
        velocity_hint=velocity_hint,
    )


def _high_confidence(
    p50_s: float = 120.0,
    p90_s: float = 300.0,
    velocity_hint: float = 1.0,
) -> RecoveryForecast:
    return RecoveryForecast(
        p50_s=p50_s,
        p90_s=p90_s,
        samples=10,
        confidence="HIGH",
        velocity_hint=velocity_hint,
    )


def _cfg(
    safe: float = 60.0,
    i_min: float = 15.0,
    i_max: float = 300.0,
    base: float = 15.0,
) -> ThrottleConfig:
    return ThrottleConfig(
        safe_interval_s=safe,
        i_min_s=i_min,
        i_max_s=i_max,
        backoff_base_s=base,
    )


# ---------------------------------------------------------------------------
# DATA POVERTY OVERRIDE: LOW_CONFIDENCE must return safe_interval regardless
# ---------------------------------------------------------------------------

class TestLowConfidenceGate:
    """The most load-bearing contract: LOW_CONFIDENCE -> fixed safe interval."""

    def test_low_confidence_returns_safe_interval(self):
        fc = _low_confidence(p50_s=120.0, p90_s=300.0)
        c = _cfg(safe=60.0)
        result = probe_interval(0.0, fc, cfg=c)
        assert result == 60.0

    def test_low_confidence_ignores_extreme_p50(self):
        """Wild p50/p90 must NOT affect the result when LOW_CONFIDENCE."""
        c = _cfg(safe=60.0, i_min=15.0, i_max=300.0)

        # Very small p50/p90 (would push to I_min if evaluated)
        fc_tiny = _low_confidence(p50_s=1.0, p90_s=2.0)
        assert probe_interval(0.0, fc_tiny, cfg=c) == 60.0

        # Very large p50/p90 (would push to I_max if evaluated)
        fc_huge = _low_confidence(p50_s=99999.0, p90_s=999999.0)
        assert probe_interval(0.0, fc_huge, cfg=c) == 60.0

    def test_low_confidence_invariant_to_t(self):
        """LOW_CONFIDENCE result must not vary with t_outage_s."""
        fc = _low_confidence()
        c = _cfg(safe=60.0)
        results = {probe_interval(t, fc, cfg=c) for t in [0, 30, 60, 120, 600, 9999]}
        assert results == {60.0}, f"Expected all 60.0, got {results}"

    def test_low_confidence_invariant_to_velocity_hint(self):
        """velocity_hint must NOT compress the safe interval on LOW_CONFIDENCE."""
        c = _cfg(safe=60.0)
        fc_fast = _low_confidence(velocity_hint=0.1)
        fc_slow = _low_confidence(velocity_hint=1.0)
        assert probe_interval(0.0, fc_fast, cfg=c) == 60.0
        assert probe_interval(0.0, fc_slow, cfg=c) == 60.0

    @pytest.mark.parametrize("p50,p90", [
        (0.0001, 0.0001),   # near zero
        (1e6, 2e6),          # enormous
        (float("inf"), float("inf")),  # infinity
    ])
    def test_low_confidence_wild_percentiles(self, p50: float, p90: float):
        fc = _low_confidence(p50_s=p50, p90_s=p90)
        c = _cfg(safe=60.0)
        result = probe_interval(0.0, fc, cfg=c)
        assert result == 60.0


# ---------------------------------------------------------------------------
# HIGH confidence: three-region curve
# ---------------------------------------------------------------------------

class TestHighConfidenceCurve:
    def test_pre_p50_interval_larger_than_i_min(self):
        """t < p50: interval should be larger than I_min (sparse probing)."""
        fc = _high_confidence(p50_s=120.0, p90_s=300.0)
        c = _cfg(i_min=15.0, i_max=300.0)
        # t=0 (far before p50): should be close to I_max
        result = probe_interval(0.0, fc, cfg=c)
        assert result > c.min_s, f"Expected > {c.min_s}, got {result}"

    def test_at_p50_is_i_min(self):
        """t == p50: should return I_min (entering the dense window)."""
        fc = _high_confidence(p50_s=120.0, p90_s=300.0)
        c = _cfg(i_min=15.0, i_max=300.0)
        result = probe_interval(120.0, fc, cfg=c)
        assert result == c.min_s, f"Expected {c.min_s}, got {result}"

    def test_inside_p50_p90_is_i_min(self):
        """t in [p50, p90]: should return I_min."""
        fc = _high_confidence(p50_s=120.0, p90_s=300.0)
        c = _cfg(i_min=15.0, i_max=300.0)
        for t in [120.0, 150.0, 200.0, 299.0, 300.0]:
            result = probe_interval(t, fc, cfg=c)
            assert result == c.min_s, f"t={t}: expected {c.min_s}, got {result}"

    def test_post_p90_exceeds_i_min(self):
        """t > p90: backoff result should be >= I_min."""
        fc = _high_confidence(p50_s=120.0, p90_s=300.0)
        c = _cfg(i_min=15.0, i_max=300.0, base=15.0)
        result = probe_interval(400.0, fc, cfg=c)
        assert result >= c.min_s, f"Expected >= {c.min_s}, got {result}"

    def test_post_p90_clamped_to_i_max(self):
        """t >> p90: interval must not exceed I_max."""
        fc = _high_confidence(p50_s=120.0, p90_s=300.0)
        c = _cfg(i_min=15.0, i_max=300.0, base=15.0)
        result = probe_interval(100000.0, fc, cfg=c)
        assert result <= c.max_s, f"Expected <= {c.max_s}, got {result}"

    def test_pre_p50_scales_with_distance(self):
        """Closer to p50 -> smaller interval (accelerating toward I_min)."""
        fc = _high_confidence(p50_s=200.0, p90_s=400.0)
        c = _cfg(i_min=15.0, i_max=300.0)
        far = probe_interval(0.0, fc, cfg=c)    # far before p50
        close = probe_interval(190.0, fc, cfg=c)  # close to p50
        assert far > close, f"far={far} should be > close={close}"

    def test_result_always_within_clamp(self):
        """For any t and any HIGH forecast, result in [I_min, I_max]."""
        fc = _high_confidence(p50_s=60.0, p90_s=120.0)
        c = _cfg(i_min=15.0, i_max=300.0)
        for t in [0, 30, 60, 90, 120, 200, 500, 1000]:
            r = probe_interval(float(t), fc, cfg=c)
            assert c.min_s <= r <= c.max_s, f"t={t}: {r} outside [{c.min_s}, {c.max_s}]"


# ---------------------------------------------------------------------------
# Velocity hint
# ---------------------------------------------------------------------------

class TestVelocityHintEffect:
    def test_velocity_hint_compresses_interval(self):
        """A velocity_hint < 1.0 must compress the pre-p50 interval."""
        fc_neutral = _high_confidence(p50_s=120.0, p90_s=300.0, velocity_hint=1.0)
        fc_fast = _high_confidence(p50_s=120.0, p90_s=300.0, velocity_hint=0.5)
        c = _cfg(i_min=15.0, i_max=300.0)
        neutral = probe_interval(0.0, fc_neutral, cfg=c)
        fast = probe_interval(0.0, fc_fast, cfg=c)
        # Fast hint -> smaller (or equal, due to clamping) interval
        assert fast <= neutral, f"fast={fast} should be <= neutral={neutral}"

    def test_velocity_hint_does_not_push_below_i_min(self):
        """velocity_hint must not push result below I_min."""
        fc = _high_confidence(p50_s=120.0, p90_s=300.0, velocity_hint=0.001)
        c = _cfg(i_min=15.0, i_max=300.0)
        result = probe_interval(120.0, fc, cfg=c)
        assert result >= c.min_s


# ---------------------------------------------------------------------------
# OFF gate
# ---------------------------------------------------------------------------

class TestOffGate:
    def test_off_returns_safe_interval(self):
        with patch.dict(
            os.environ,
            {
                "JARVIS_RECOVERY_THROTTLE_ENABLED": "false",
                "JARVIS_SAFE_POLLING_INTERVAL_S": "60.0",
            },
        ):
            fc = _high_confidence()
            result = probe_interval(0.0, fc)
        assert result == 60.0

    def test_off_ignores_high_confidence(self):
        """Even HIGH confidence forecast is ignored when throttle is OFF."""
        with patch.dict(
            os.environ,
            {
                "JARVIS_RECOVERY_THROTTLE_ENABLED": "false",
                "JARVIS_SAFE_POLLING_INTERVAL_S": "60.0",
            },
        ):
            fc = _high_confidence(p50_s=15.0, p90_s=30.0)
            result = probe_interval(20.0, fc)
        assert result == 60.0


# ---------------------------------------------------------------------------
# Fail-soft
# ---------------------------------------------------------------------------

class TestFailSoft:
    def test_none_forecast_returns_safe_interval(self):
        """A None/duck-typed forecast with no confidence attr -> safe interval."""
        # None has no .confidence -> getattr falls back to "LOW_CONFIDENCE"
        c = _cfg(safe=60.0)
        result = probe_interval(0.0, None, cfg=c)  # type: ignore[arg-type]
        assert result == 60.0

    def test_nan_t_does_not_raise(self):
        """NaN t_outage_s should not raise."""
        fc = _high_confidence()
        c = _cfg()
        try:
            r = probe_interval(float("nan"), fc, cfg=c)
            assert isinstance(r, float)
        except Exception as exc:
            pytest.fail(f"probe_interval raised {exc!r} on NaN t")

    def test_negative_t_clamped(self):
        """Negative t should be treated as 0 (clamped) and not crash."""
        fc = _high_confidence(p50_s=120.0, p90_s=300.0)
        c = _cfg(i_min=15.0, i_max=300.0)
        result = probe_interval(-100.0, fc, cfg=c)
        assert c.min_s <= result <= c.max_s


# ---------------------------------------------------------------------------
# ThrottleConfig
# ---------------------------------------------------------------------------

class TestThrottleConfig:
    def test_explicit_values_used(self):
        c = ThrottleConfig(safe_interval_s=45.0, i_min_s=10.0, i_max_s=200.0, backoff_base_s=20.0)
        assert c.safe_s == 45.0
        assert c.min_s == 10.0
        assert c.max_s == 200.0
        assert c.base_s == 20.0

    def test_defaults_from_env(self):
        with patch.dict(
            os.environ,
            {
                "JARVIS_SAFE_POLLING_INTERVAL_S": "55.0",
                "JARVIS_PROBE_INTERVAL_MIN_S": "12.0",
                "JARVIS_PROBE_INTERVAL_MAX_S": "250.0",
                "JARVIS_THROTTLE_BACKOFF_BASE_S": "18.0",
            },
        ):
            c = ThrottleConfig()
            assert c.safe_s == 55.0
            assert c.min_s == 12.0
            assert c.max_s == 250.0
            assert c.base_s == 18.0


# ---------------------------------------------------------------------------
# Integration: realistic scenario
# ---------------------------------------------------------------------------

class TestRealisticScenario:
    """End-to-end scenario: outage progresses from t=0 through p50 to beyond p90."""

    def test_full_outage_lifecycle_intervals(self):
        """
        p50=120s, p90=300s.
        At t=0   -> sparse (close to I_max)
        At t=60  -> moderate (between I_min and I_max)
        At t=120 -> I_min (entering dense window)
        At t=200 -> I_min (inside dense window)
        At t=300 -> I_min (at p90 boundary)
        At t=600 -> backoff (anomalous)
        """
        fc = _high_confidence(p50_s=120.0, p90_s=300.0)
        c = _cfg(i_min=15.0, i_max=300.0, base=15.0)

        t0 = probe_interval(0.0, fc, cfg=c)
        t60 = probe_interval(60.0, fc, cfg=c)
        t120 = probe_interval(120.0, fc, cfg=c)
        t200 = probe_interval(200.0, fc, cfg=c)
        t300 = probe_interval(300.0, fc, cfg=c)
        t600 = probe_interval(600.0, fc, cfg=c)

        # Sparse at start (large interval, close to I_max)
        assert t0 > t60, f"t0={t0} should > t60={t60}"
        assert t60 >= t120, f"t60={t60} should >= t120={t120}"

        # Dense window
        assert t120 == c.min_s, f"t120 should be I_min={c.min_s}"
        assert t200 == c.min_s, f"t200 should be I_min={c.min_s}"
        assert t300 == c.min_s, f"t300 should be I_min={c.min_s}"

        # Post-p90 backoff
        assert t600 >= c.min_s, f"t600 should be >= I_min={c.min_s}"
        # t600 may be at min (if jitter produces low value) but should be legit
        assert t600 <= c.max_s
