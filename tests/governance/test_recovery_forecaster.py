"""tests/governance/test_recovery_forecaster.py

TDD suite for recovery_forecaster.py (Phase 2 — Sovereign Provider Failover).

Covers:
  - EWMA correct over seeded closed-outage durations
  - N < 5 -> LOW_CONFIDENCE; N >= 5 -> HIGH
  - p50 < p90 (with sufficient spread)
  - velocity_hint < 1.0 on a falling trajectory
  - empty ledger -> LOW_CONFIDENCE conservative default
  - fail-soft on corrupt ledger
  - OFF (JARVIS_RECOVERY_FORECAST_ENABLED=false) -> LOW_CONFIDENCE
"""
from __future__ import annotations

import os
import time
import uuid
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from backend.core.ouroboros.governance.recovery_forecaster import (
    RecoveryForecast,
    RecoveryForecaster,
    _compute_ewma,
    _compute_percentile,
    _conservative_default,
    _velocity_hint_from_trajectory,
    get_recovery_forecaster,
)
from backend.core.ouroboros.governance.outage_ledger import OutageRecord


# ---------------------------------------------------------------------------
# Helpers to build fake OutageRecord lists
# ---------------------------------------------------------------------------

def _make_closed(duration_s: float) -> OutageRecord:
    """Create a minimal closed OutageRecord with the given duration."""
    now = time.time()
    rec = OutageRecord(
        outage_id=str(uuid.uuid4()),
        started_ts=now - duration_s,
        ended_ts=now,
        duration_s=duration_s,
    )
    return rec


def _make_open() -> OutageRecord:
    """Create an open (not yet closed) OutageRecord."""
    return OutageRecord(
        outage_id=str(uuid.uuid4()),
        started_ts=time.time(),
        ended_ts=None,
        duration_s=None,
    )


def _patch_ledger(records: List[OutageRecord]):
    """Context manager that patches get_outage_ledger().recent() to return records.

    The forecaster lazy-imports get_outage_ledger from outage_ledger inside
    _compute(), so we patch the function in the outage_ledger module directly.
    """
    mock_ledger = MagicMock()
    mock_ledger.recent.return_value = records
    return patch(
        "backend.core.ouroboros.governance.outage_ledger.get_outage_ledger",
        return_value=mock_ledger,
    )


# ---------------------------------------------------------------------------
# Unit tests: internal helpers
# ---------------------------------------------------------------------------

class TestComputeEWMA:
    def test_single_element(self):
        assert _compute_ewma([100.0], 0.4) == 100.0

    def test_two_elements(self):
        # alpha=0.4: ewma = 0.4*200 + 0.6*100 = 80 + 60 = 140
        result = _compute_ewma([100.0, 200.0], 0.4)
        assert abs(result - 140.0) < 1e-9

    def test_empty(self):
        assert _compute_ewma([], 0.4) == 0.0

    def test_high_alpha_recent_dominant(self):
        """alpha=1.0: EWMA equals the last value."""
        result = _compute_ewma([50.0, 200.0, 300.0], 1.0)
        assert abs(result - 300.0) < 1e-9

    def test_low_alpha_slow_update(self):
        """alpha near 0: EWMA barely moves from first value."""
        result = _compute_ewma([100.0, 1000.0], 0.001)
        # ewma = 0.001*1000 + 0.999*100 = 1.0 + 99.9 = 100.9
        assert abs(result - 100.9) < 0.1


class TestComputePercentile:
    def test_single(self):
        assert _compute_percentile([42.0], 50.0) == 42.0

    def test_p50_two_values(self):
        result = _compute_percentile([100.0, 200.0], 50.0)
        assert abs(result - 150.0) < 1e-9

    def test_p90_five_values(self):
        vals = sorted([10.0, 20.0, 30.0, 40.0, 50.0])
        result = _compute_percentile(vals, 90.0)
        # rank = 0.9 * 4 = 3.6; lo=3(40), hi=4(50); 40 + 0.6*(50-40) = 46
        assert abs(result - 46.0) < 1e-9

    def test_p0_is_minimum(self):
        vals = sorted([10.0, 20.0, 30.0])
        assert _compute_percentile(vals, 0.0) == 10.0

    def test_p100_is_maximum(self):
        vals = sorted([10.0, 20.0, 30.0])
        assert _compute_percentile(vals, 100.0) == 30.0

    def test_empty(self):
        assert _compute_percentile([], 50.0) == 0.0


class TestVelocityHint:
    def test_empty_trajectory(self):
        assert _velocity_hint_from_trajectory([]) == 1.0

    def test_single_element(self):
        assert _velocity_hint_from_trajectory([100.0]) == 1.0

    def test_flat_trajectory(self):
        traj = [100.0, 100.0, 100.0, 100.0]
        assert _velocity_hint_from_trajectory(traj) == 1.0

    def test_rising_trajectory(self):
        # Latencies going up -> no recovery bias
        traj = [50.0, 60.0, 70.0, 80.0]
        assert _velocity_hint_from_trajectory(traj) == 1.0

    def test_falling_trajectory_returns_below_1(self):
        # Strong drop: first half [200, 200], second half [50, 50]
        traj = [200.0, 200.0, 50.0, 50.0]
        hint = _velocity_hint_from_trajectory(traj)
        assert hint < 1.0, f"Expected hint < 1.0, got {hint}"
        assert hint >= 0.5, f"Expected hint >= 0.5 (floor), got {hint}"

    def test_small_drop_not_significant(self):
        # < 5% relative drop -> neutral
        traj = [100.0, 100.0, 96.0, 96.0]
        hint = _velocity_hint_from_trajectory(traj)
        # 4% drop < threshold (5%) -> should remain 1.0
        assert hint == 1.0

    def test_large_drop_compresses_strongly(self):
        # 90% drop -> hint should be well below 1.0
        traj = [1000.0, 1000.0, 10.0, 10.0]
        hint = _velocity_hint_from_trajectory(traj)
        assert hint < 0.6


# ---------------------------------------------------------------------------
# RecoveryForecast dataclass
# ---------------------------------------------------------------------------

class TestRecoveryForecastDataclass:
    def test_frozen(self):
        fc = RecoveryForecast(p50_s=60.0, p90_s=120.0, samples=5, confidence="HIGH", velocity_hint=1.0)
        with pytest.raises((AttributeError, TypeError)):
            fc.p50_s = 99.0  # type: ignore[misc]

    def test_to_dict(self):
        fc = RecoveryForecast(p50_s=60.0, p90_s=120.0, samples=5, confidence="HIGH", velocity_hint=0.8)
        d = fc.to_dict()
        assert d["p50_s"] == 60.0
        assert d["p90_s"] == 120.0
        assert d["samples"] == 5
        assert d["confidence"] == "HIGH"
        assert d["velocity_hint"] == 0.8

    def test_confidence_values(self):
        fc = RecoveryForecast(p50_s=60.0, p90_s=120.0, samples=0, confidence="LOW_CONFIDENCE", velocity_hint=1.0)
        assert fc.confidence == "LOW_CONFIDENCE"


# ---------------------------------------------------------------------------
# RecoveryForecaster integration tests (ledger patched)
# ---------------------------------------------------------------------------

class TestRecoveryForecasterEmptyLedger:
    def test_empty_ledger_returns_low_confidence(self):
        with _patch_ledger([]):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        assert fc.confidence == "LOW_CONFIDENCE"
        assert fc.samples == 0
        assert fc.velocity_hint == 1.0

    def test_empty_ledger_conservative_defaults(self):
        with _patch_ledger([]):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        assert fc.p50_s > 0.0
        assert fc.p90_s >= fc.p50_s


class TestRecoveryForecasterDataPovertyOverride:
    """N < 5 must always yield LOW_CONFIDENCE, never HIGH."""

    @pytest.mark.parametrize("n", [1, 2, 3, 4])
    def test_below_min_samples_low_confidence(self, n: int):
        records = [_make_closed(60.0 * (i + 1)) for i in range(n)]
        with _patch_ledger(records), patch.dict(os.environ, {"JARVIS_FORECAST_MIN_SAMPLES": "5"}):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        assert fc.confidence == "LOW_CONFIDENCE", (
            f"Expected LOW_CONFIDENCE with N={n}, got {fc.confidence}"
        )
        assert fc.samples == n

    def test_n_equals_min_samples_is_high(self):
        records = [_make_closed(60.0) for _ in range(5)]
        with _patch_ledger(records), patch.dict(os.environ, {"JARVIS_FORECAST_MIN_SAMPLES": "5"}):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        assert fc.confidence == "HIGH"
        assert fc.samples == 5

    def test_n_above_min_samples_is_high(self):
        records = [_make_closed(60.0) for _ in range(10)]
        with _patch_ledger(records), patch.dict(os.environ, {"JARVIS_FORECAST_MIN_SAMPLES": "5"}):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        assert fc.confidence == "HIGH"
        assert fc.samples == 10


class TestRecoveryForecasterEWMAAndPercentiles:
    def test_p50_less_than_p90_with_spread(self):
        """With spread in durations, p50 must be < p90."""
        durations = [60.0, 120.0, 180.0, 240.0, 300.0, 360.0, 420.0]
        records = [_make_closed(d) for d in durations]
        with _patch_ledger(records), patch.dict(os.environ, {"JARVIS_FORECAST_MIN_SAMPLES": "5"}):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        assert fc.confidence == "HIGH"
        assert fc.p50_s < fc.p90_s, f"p50={fc.p50_s} should be < p90={fc.p90_s}"

    def test_p50_and_p90_positive(self):
        records = [_make_closed(d) for d in [100.0, 200.0, 300.0, 400.0, 500.0]]
        with _patch_ledger(records), patch.dict(os.environ, {"JARVIS_FORECAST_MIN_SAMPLES": "5"}):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        assert fc.p50_s > 0.0
        assert fc.p90_s > 0.0

    def test_ewma_reflects_recent_history(self):
        """With monotonically increasing durations, p50 should be in mid-range."""
        durations = [60.0, 120.0, 180.0, 240.0, 300.0]
        records = [_make_closed(d) for d in durations]
        with _patch_ledger(records), patch.dict(os.environ, {"JARVIS_FORECAST_MIN_SAMPLES": "5"}):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        # p50 of [60,120,180,240,300] = 180
        assert abs(fc.p50_s - 180.0) < 5.0

    def test_open_records_excluded(self):
        """Open outage records (duration_s=None) must not count toward N."""
        closed = [_make_closed(60.0) for _ in range(4)]
        open_rec = _make_open()
        records = closed + [open_rec]
        with _patch_ledger(records), patch.dict(os.environ, {"JARVIS_FORECAST_MIN_SAMPLES": "5"}):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        # 4 closed < 5 min_samples -> LOW_CONFIDENCE
        assert fc.confidence == "LOW_CONFIDENCE"
        assert fc.samples == 4


class TestRecoveryForecasterVelocityHint:
    def test_falling_trajectory_hint_below_1(self):
        records = [_make_closed(120.0) for _ in range(5)]
        # Strong falling trajectory: first half high latency, second half low
        trajectory = [500.0, 500.0, 500.0, 50.0, 50.0, 50.0]
        with _patch_ledger(records), patch.dict(os.environ, {"JARVIS_FORECAST_MIN_SAMPLES": "5"}):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast(live_probe_trajectory=trajectory)
        assert fc.velocity_hint < 1.0, f"Expected hint < 1.0, got {fc.velocity_hint}"

    def test_no_trajectory_neutral_hint(self):
        records = [_make_closed(120.0) for _ in range(5)]
        with _patch_ledger(records), patch.dict(os.environ, {"JARVIS_FORECAST_MIN_SAMPLES": "5"}):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        assert fc.velocity_hint == 1.0

    def test_empty_trajectory_neutral_hint(self):
        records = [_make_closed(120.0) for _ in range(5)]
        with _patch_ledger(records), patch.dict(os.environ, {"JARVIS_FORECAST_MIN_SAMPLES": "5"}):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast(live_probe_trajectory=[])
        assert fc.velocity_hint == 1.0


class TestRecoveryForecasterFailSoft:
    def test_corrupt_ledger_returns_low_confidence(self):
        """If ledger.recent raises, forecaster returns conservative default."""
        mock_ledger = MagicMock()
        mock_ledger.recent.side_effect = RuntimeError("disk corrupted")
        with patch(
            "backend.core.ouroboros.governance.outage_ledger.get_outage_ledger",
            return_value=mock_ledger,
        ):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        assert fc.confidence == "LOW_CONFIDENCE"
        assert fc.samples == 0
        assert fc.p50_s > 0.0

    def test_returns_forecast_not_raises(self):
        """forecast() must never raise, even on extreme inputs."""
        with _patch_ledger([]):
            forecaster = RecoveryForecaster()
            # Should not raise
            fc = forecaster.forecast(live_probe_trajectory=[float("nan"), float("inf")])
        assert fc is not None


class TestRecoveryForecasterOffGate:
    def test_off_returns_low_confidence(self):
        records = [_make_closed(120.0) for _ in range(10)]
        with _patch_ledger(records), patch.dict(
            os.environ, {"JARVIS_RECOVERY_FORECAST_ENABLED": "false"}
        ):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        assert fc.confidence == "LOW_CONFIDENCE"

    def test_off_returns_conservative_defaults(self):
        records = [_make_closed(120.0) for _ in range(10)]
        with _patch_ledger(records), patch.dict(
            os.environ,
            {
                "JARVIS_RECOVERY_FORECAST_ENABLED": "false",
                "JARVIS_FORECAST_DEFAULT_P50_S": "300",
                "JARVIS_FORECAST_DEFAULT_P90_S": "600",
            },
        ):
            forecaster = RecoveryForecaster()
            fc = forecaster.forecast()
        assert fc.p50_s == 300.0
        assert fc.p90_s == 600.0
        assert fc.samples == 0


class TestGetRecoveryForecasterSingleton:
    def test_returns_same_instance(self):
        # Reset singleton to ensure clean state
        import backend.core.ouroboros.governance.recovery_forecaster as mod
        mod._singleton = None
        a = get_recovery_forecaster()
        b = get_recovery_forecaster()
        assert a is b
        mod._singleton = None  # cleanup
