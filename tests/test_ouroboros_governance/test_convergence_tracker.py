"""Tests for ConvergenceTracker and related types.

TDD: tests written first; implementation in
backend/core/ouroboros/governance/convergence_tracker.py.
"""
from __future__ import annotations

import dataclasses
import math
from typing import List

import pytest

from backend.core.ouroboros.governance.convergence_tracker import (
    ConvergenceReport,
    ConvergenceState,
    ConvergenceTracker,
    _linear_regression_slope,
    _log_fit_r_squared,
    _oscillation_ratio,
    _stddev,
)


# ---------------------------------------------------------------------------
# Helper to build score sequences
# ---------------------------------------------------------------------------


def _log_scores(n: int, a: float = -0.1, b: float = 0.8) -> List[float]:
    """Generate scores following S = a*ln(t+1) + b for t in [1..n]."""
    return [a * math.log(t + 1) + b for t in range(1, n + 1)]


# ---------------------------------------------------------------------------
# ConvergenceState enum
# ---------------------------------------------------------------------------


class TestConvergenceState:
    def test_all_states_exist(self):
        states = {s.value for s in ConvergenceState}
        assert states == {
            "IMPROVING",
            "LOGARITHMIC",
            "PLATEAUED",
            "OSCILLATING",
            "DEGRADING",
            "INSUFFICIENT_DATA",
        }

    def test_is_str_enum(self):
        assert isinstance(ConvergenceState.IMPROVING, str)


# ---------------------------------------------------------------------------
# ConvergenceReport dataclass
# ---------------------------------------------------------------------------


class TestConvergenceReport:
    def _make(self, **overrides):
        defaults = dict(
            state=ConvergenceState.PLATEAUED,
            window_size=20,
            slope=0.0,
            r_squared_log=0.0,
            oscillation_ratio=0.0,
            plateau_stddev=0.001,
            scores_analyzed=10,
            recommendation="Hold steady.",
            timestamp=1234567890.0,
        )
        defaults.update(overrides)
        return ConvergenceReport(**defaults)

    def test_creation(self):
        r = self._make()
        assert r.state == ConvergenceState.PLATEAUED

    def test_frozen(self):
        r = self._make()
        with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            r.state = ConvergenceState.IMPROVING  # type: ignore[misc]

    def test_all_fields_present(self):
        r = self._make()
        for field in (
            "state",
            "window_size",
            "slope",
            "r_squared_log",
            "oscillation_ratio",
            "plateau_stddev",
            "scores_analyzed",
            "recommendation",
            "timestamp",
        ):
            assert hasattr(r, field), f"Missing field: {field}"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestLinearRegressionSlope:
    def test_flat_sequence_slope_near_zero(self):
        values = [0.5] * 10
        assert _linear_regression_slope(values) == pytest.approx(0.0, abs=1e-9)

    def test_strictly_increasing_positive_slope(self):
        values = list(range(1, 11))  # [1,2,...,10]
        slope = _linear_regression_slope(values)
        assert slope > 0

    def test_strictly_decreasing_negative_slope(self):
        values = list(range(10, 0, -1))  # [10,9,...,1]
        slope = _linear_regression_slope(values)
        assert slope < 0

    def test_known_slope(self):
        # y = 2x + 1 over x in [0..4]
        values = [1.0, 3.0, 5.0, 7.0, 9.0]
        slope = _linear_regression_slope(values)
        assert slope == pytest.approx(2.0, rel=1e-6)

    def test_single_value_returns_zero(self):
        assert _linear_regression_slope([0.7]) == pytest.approx(0.0, abs=1e-9)

    def test_two_values_slope(self):
        # Indices 0, 1; values 0, 1 => slope = 1
        slope = _linear_regression_slope([0.0, 1.0])
        assert slope == pytest.approx(1.0, rel=1e-6)


class TestLogFitRSquared:
    def test_perfect_log_fit_high_r_squared(self):
        scores = _log_scores(30)
        r2 = _log_fit_r_squared(scores)
        assert r2 > 0.9

    def test_flat_scores_returns_zero(self):
        # Flat -> no decreasing log trend; a >= 0 => return 0.0
        values = [0.5] * 20
        r2 = _log_fit_r_squared(values)
        assert r2 == 0.0

    def test_increasing_scores_returns_zero(self):
        values = [float(i) * 0.05 for i in range(1, 21)]
        r2 = _log_fit_r_squared(values)
        assert r2 == 0.0

    def test_linear_decreasing_not_great_log_fit(self):
        # Linear decrease from 1.0 to 0.0 over 20 points — not a great log fit
        values = [1.0 - i / 19 for i in range(20)]
        r2 = _log_fit_r_squared(values)
        # The fit can be nonzero, but it shouldn't perfectly fit; just confirm [0,1]
        assert 0.0 <= r2 <= 1.0

    def test_r_squared_bounds(self):
        for scores in [_log_scores(10), [0.9] * 15, [float(i) / 20 for i in range(20)]]:
            r2 = _log_fit_r_squared(scores)
            assert 0.0 <= r2 <= 1.0


class TestOscillationRatio:
    def test_perfect_alternation_ratio_one(self):
        # [1, -1, 1, -1, ...] diffs all alternate sign
        values = [1.0 if i % 2 == 0 else -1.0 for i in range(10)]
        ratio = _oscillation_ratio(values)
        assert ratio == pytest.approx(1.0, abs=1e-9)

    def test_monotone_ratio_zero(self):
        values = list(range(1, 11))
        ratio = _oscillation_ratio(values)
        assert ratio == pytest.approx(0.0, abs=1e-9)

    def test_flat_returns_zero(self):
        values = [0.5] * 10
        ratio = _oscillation_ratio(values)
        assert ratio == pytest.approx(0.0, abs=1e-9)

    def test_ratio_in_unit_interval(self):
        values = [0.3, 0.5, 0.2, 0.6, 0.1, 0.7]
        ratio = _oscillation_ratio(values)
        assert 0.0 <= ratio <= 1.0

    def test_too_few_points_returns_zero(self):
        assert _oscillation_ratio([]) == pytest.approx(0.0, abs=1e-9)
        assert _oscillation_ratio([0.5]) == pytest.approx(0.0, abs=1e-9)


class TestStddev:
    def test_constant_series_stddev_zero(self):
        assert _stddev([5.0] * 10) == pytest.approx(0.0, abs=1e-9)

    def test_known_stddev(self):
        # Population stddev of [2,4,4,4,5,5,7,9] = 2.0
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        assert _stddev(values) == pytest.approx(2.0, rel=1e-6)

    def test_single_value_returns_zero(self):
        assert _stddev([3.14]) == pytest.approx(0.0, abs=1e-9)

    def test_two_equal_values_returns_zero(self):
        assert _stddev([1.0, 1.0]) == pytest.approx(0.0, abs=1e-9)

    def test_two_different_values(self):
        # population stddev of [0, 2] = 1.0
        assert _stddev([0.0, 2.0]) == pytest.approx(1.0, rel=1e-6)


# ---------------------------------------------------------------------------
# ConvergenceTracker.analyze()
# ---------------------------------------------------------------------------


class TestConvergenceTracker:
    def _tracker(self) -> ConvergenceTracker:
        return ConvergenceTracker()

    # --- INSUFFICIENT_DATA ------------------------------------------------

    def test_insufficient_data_zero_scores(self):
        t = self._tracker()
        report = t.analyze([])
        assert report.state == ConvergenceState.INSUFFICIENT_DATA

    def test_insufficient_data_four_scores(self):
        t = self._tracker()
        report = t.analyze([0.5, 0.4, 0.3, 0.2])
        assert report.state == ConvergenceState.INSUFFICIENT_DATA

    def test_insufficient_data_exactly_five_is_ok(self):
        t = self._tracker()
        # Five strictly decreasing scores should not be INSUFFICIENT_DATA
        report = t.analyze([0.5, 0.4, 0.3, 0.2, 0.1])
        assert report.state != ConvergenceState.INSUFFICIENT_DATA

    def test_insufficient_data_scores_analyzed_zero(self):
        t = self._tracker()
        report = t.analyze([0.3, 0.2, 0.1])
        assert report.scores_analyzed == 3

    # --- IMPROVING --------------------------------------------------------

    def test_improving_trend(self):
        """Accelerating (quadratic) decrease: R² of log-fit is low, slope is negative."""
        # S(t) = 1 - (t/19)^2 — concave-down profile that does NOT fit log well (R²~0.66)
        scores = [1.0 - (i / 19) ** 2 for i in range(20)]
        t = self._tracker()
        report = t.analyze(scores)
        assert report.state == ConvergenceState.IMPROVING

    def test_improving_slope_negative(self):
        scores = [1.0 - (i / 19) ** 2 for i in range(20)]
        t = self._tracker()
        report = t.analyze(scores)
        assert report.slope < 0

    # --- DEGRADING --------------------------------------------------------

    def test_degrading_trend(self):
        """Steadily increasing scores should be DEGRADING."""
        scores = [0.1 + i * 0.05 for i in range(15)]  # 0.1 up to 0.8
        t = self._tracker()
        report = t.analyze(scores)
        assert report.state == ConvergenceState.DEGRADING

    def test_degrading_slope_positive(self):
        scores = [0.1 + i * 0.05 for i in range(15)]
        t = self._tracker()
        report = t.analyze(scores)
        assert report.slope > 0

    # --- PLATEAUED --------------------------------------------------------

    def test_plateaued(self):
        """Nearly flat scores with sub-epsilon slope should be PLATEAUED."""
        # Tiny monotone drift: slope ~0.0004 (well below epsilon=0.01), no oscillation
        scores = [0.45 + i * 0.0004 for i in range(20)]
        t = self._tracker()
        report = t.analyze(scores)
        assert report.state == ConvergenceState.PLATEAUED

    def test_plateaued_stddev_low(self):
        scores = [0.45 + i * 0.0004 for i in range(20)]
        t = self._tracker()
        report = t.analyze(scores)
        assert report.plateau_stddev < 0.02

    # --- OSCILLATING ------------------------------------------------------

    def test_oscillating(self):
        """Alternating high/low scores should be OSCILLATING."""
        scores = [0.8 if i % 2 == 0 else 0.2 for i in range(20)]
        t = self._tracker()
        report = t.analyze(scores)
        assert report.state == ConvergenceState.OSCILLATING

    def test_oscillating_ratio_high(self):
        scores = [0.8 if i % 2 == 0 else 0.2 for i in range(20)]
        t = self._tracker()
        report = t.analyze(scores)
        assert report.oscillation_ratio > 0.6

    # --- LOGARITHMIC ------------------------------------------------------

    def test_logarithmic_convergence(self):
        """Scores following S = -0.1*ln(t+1) + 0.8 should be LOGARITHMIC.

        Using 20 points so the window captures the steeper early portion of the
        curve (slope ~ -0.011, well below epsilon=0.01, r² ~ 0.99).
        """
        scores = _log_scores(20)
        t = self._tracker()
        report = t.analyze(scores)
        assert report.state == ConvergenceState.LOGARITHMIC

    def test_logarithmic_r_squared_high(self):
        scores = _log_scores(20)
        t = self._tracker()
        report = t.analyze(scores)
        assert report.r_squared_log > 0.7

    def test_logarithmic_slope_negative(self):
        scores = _log_scores(20)
        t = self._tracker()
        report = t.analyze(scores)
        assert report.slope < 0

    # --- Report fields ----------------------------------------------------

    def test_convergence_report_fields(self):
        """All ConvergenceReport fields exist and have the correct Python types."""
        scores = [0.5 - i * 0.01 for i in range(15)]
        t = self._tracker()
        report = t.analyze(scores)

        assert isinstance(report.state, ConvergenceState)
        assert isinstance(report.window_size, int)
        assert isinstance(report.slope, float)
        assert isinstance(report.r_squared_log, float)
        assert isinstance(report.oscillation_ratio, float)
        assert isinstance(report.plateau_stddev, float)
        assert isinstance(report.scores_analyzed, int)
        assert isinstance(report.recommendation, str)
        assert isinstance(report.timestamp, float)

    def test_report_scores_analyzed_is_capped_at_window(self):
        """When more than window_size scores are given, scores_analyzed == window_size."""
        scores = [0.5] * 50
        t = self._tracker()
        report = t.analyze(scores)
        assert report.scores_analyzed == report.window_size

    def test_report_scores_analyzed_less_than_window(self):
        """When fewer than window_size scores given, scores_analyzed equals len."""
        scores = [0.5, 0.4, 0.3, 0.2, 0.1]
        t = self._tracker()
        report = t.analyze(scores)
        assert report.scores_analyzed == 5

    def test_recommendation_non_empty_string(self):
        """Every state should produce a non-empty recommendation string."""
        t = self._tracker()
        for scores_fn in [
            lambda: [],
            lambda: [1.0 - (i / 19) ** 2 for i in range(20)],  # IMPROVING
            lambda: [0.1 + i * 0.05 for i in range(15)],          # DEGRADING
            lambda: [0.45 + i * 0.0004 for i in range(20)],       # PLATEAUED
            lambda: [0.8 if i % 2 == 0 else 0.2 for i in range(20)],  # OSCILLATING
            lambda: _log_scores(20),                                # LOGARITHMIC
        ]:
            report = t.analyze(scores_fn())
            assert isinstance(report.recommendation, str)
            assert len(report.recommendation) > 0

    # --- Window behaviour -------------------------------------------------

    def test_window_uses_last_n_scores(self):
        """Tracker uses only the last `window_size` scores."""
        t = self._tracker()
        window = t._window_size  # access internal default

        # First part: severely degrading; last window: flat (tiny monotone drift)
        degrading = [0.1 + i * 0.1 for i in range(50)]
        flat_tail = [0.45 + i * 0.0004 for i in range(window)]
        report = t.analyze(degrading + flat_tail)
        # With a flat tail, shouldn't be DEGRADING
        assert report.state != ConvergenceState.DEGRADING

    def test_timestamp_is_recent(self):
        """Timestamp should be a positive float (Unix epoch)."""
        import time
        t = self._tracker()
        before = time.time()
        report = t.analyze([0.5] * 10)
        after = time.time()
        assert before <= report.timestamp <= after
