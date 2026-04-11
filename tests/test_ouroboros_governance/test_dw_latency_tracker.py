"""Unit tests for the rolling p95 DW latency tracker (Phase 1.2)."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.dw_latency_tracker import (
    DwLatencyTracker,
    get_default_tracker,
    reset_default_tracker,
)


@pytest.fixture(autouse=True)
def _reset_default():
    reset_default_tracker()
    yield
    reset_default_tracker()


def _make_hot(tracker: DwLatencyTracker, p95_s: float, count: int = 10) -> None:
    for _ in range(count):
        tracker.record_success(p95_s)


class TestColdStart:
    def test_empty_tracker_returns_ceiling(self) -> None:
        t = DwLatencyTracker(ceiling_s=90.0, floor_s=15.0)
        assert t.recommended_budget() == 90.0
        assert t.p95() is None

    def test_fewer_than_min_samples_returns_ceiling(self) -> None:
        t = DwLatencyTracker(ceiling_s=90.0, floor_s=15.0)
        t.record_success(5.0)
        t.record_success(6.0)
        # only 2 samples, min is 3 → still cold
        assert t.recommended_budget() == 90.0
        assert t.p95() is None

    def test_three_samples_exits_cold(self) -> None:
        t = DwLatencyTracker(ceiling_s=90.0, floor_s=15.0)
        t.record_success(5.0)
        t.record_success(5.0)
        t.record_success(5.0)
        # 5 * 1.5 = 7.5 → clamped to floor 15
        assert t.recommended_budget() == 15.0


class TestHotPath:
    def test_low_p95_clamps_to_floor(self) -> None:
        t = DwLatencyTracker(ceiling_s=90.0, floor_s=15.0, p95_mult=1.5)
        _make_hot(t, 5.0)
        # 5 * 1.5 = 7.5, floor = 15
        assert t.recommended_budget() == 15.0

    def test_moderate_p95_scales_linearly(self) -> None:
        t = DwLatencyTracker(ceiling_s=90.0, floor_s=15.0, p95_mult=1.5)
        _make_hot(t, 20.0)
        # 20 * 1.5 = 30
        assert t.recommended_budget() == 30.0

    def test_high_p95_clamps_to_ceiling(self) -> None:
        t = DwLatencyTracker(ceiling_s=90.0, floor_s=15.0, p95_mult=1.5)
        _make_hot(t, 80.0)
        # 80 * 1.5 = 120, ceiling = 90
        assert t.recommended_budget() == 90.0

    def test_complexity_multiplier_scales_budget(self) -> None:
        t = DwLatencyTracker(ceiling_s=120.0, floor_s=15.0, p95_mult=1.5)
        _make_hot(t, 20.0)
        # 20 * 1.5 * 1.231 ≈ 36.93
        recommended = t.recommended_budget(
            route_ceiling_s=120.0, complexity_multiplier=1.231,
        )
        assert 36.0 < recommended < 38.0


class TestFailureBackoff:
    def test_three_failures_return_ceiling(self) -> None:
        t = DwLatencyTracker(ceiling_s=90.0, floor_s=15.0)
        _make_hot(t, 5.0)
        # Hot: 15s
        assert t.recommended_budget() == 15.0

        t.record_failure()
        t.record_failure()
        # 2 failures: still hot
        assert t.recommended_budget() == 15.0

        t.record_failure()
        # 3 failures: reset to ceiling
        assert t.recommended_budget() == 90.0

    def test_success_resets_failure_counter(self) -> None:
        t = DwLatencyTracker(ceiling_s=90.0, floor_s=15.0)
        _make_hot(t, 5.0)
        t.record_failure()
        t.record_failure()
        t.record_failure()
        # Cold from failures
        assert t.recommended_budget() == 90.0
        t.record_success(5.0)
        # Hot again
        assert t.recommended_budget() == 15.0


class TestRouteCeiling:
    def test_route_ceiling_overrides_tracker_default(self) -> None:
        t = DwLatencyTracker(ceiling_s=90.0, floor_s=15.0, p95_mult=1.5)
        _make_hot(t, 80.0)
        # p95=80, 1.5x = 120. Tracker ceiling 90, route 120 → clamped to 120
        assert t.recommended_budget(route_ceiling_s=120.0) == 120.0

    def test_route_ceiling_below_tracker_ceiling(self) -> None:
        t = DwLatencyTracker(ceiling_s=90.0, floor_s=15.0)
        _make_hot(t, 80.0)
        # Route ceiling 60 forces clamp
        assert t.recommended_budget(route_ceiling_s=60.0) == 60.0


class TestDisabled:
    def test_disabled_returns_ceiling(self) -> None:
        t = DwLatencyTracker(ceiling_s=90.0, floor_s=15.0, enabled=False)
        _make_hot(t, 5.0)
        # Disabled → ignore samples, return ceiling
        assert t.recommended_budget() == 90.0


class TestDefaultTracker:
    def test_default_tracker_is_singleton(self) -> None:
        t1 = get_default_tracker()
        t2 = get_default_tracker()
        assert t1 is t2

    def test_reset_creates_new_instance(self) -> None:
        t1 = get_default_tracker()
        reset_default_tracker()
        t2 = get_default_tracker()
        assert t1 is not t2


class TestSnapshot:
    def test_snapshot_reports_state(self) -> None:
        t = DwLatencyTracker(ceiling_s=90.0, floor_s=15.0)
        _make_hot(t, 10.0)
        snap = t.snapshot()
        assert snap["enabled"] is True
        assert snap["samples"] == 10
        assert snap["total_samples"] == 10
        assert snap["p95_s"] == 10.0
        assert snap["ceiling_s"] == 90.0
