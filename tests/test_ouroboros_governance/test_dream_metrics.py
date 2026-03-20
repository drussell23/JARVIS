"""Tests for DreamMetricsTracker.

TDD Red->Green cycle for TC22 and supporting cases.

TC22: compute_minutes + hit_rate tracked correctly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.consciousness.dream_metrics import DreamMetricsTracker
from backend.core.ouroboros.consciousness.types import DreamMetrics


# ---------------------------------------------------------------------------
# Test 1: record_compute_time increments opportunistic_compute_minutes
# ---------------------------------------------------------------------------

class TestRecordComputeTime:
    def test_single_increment(self):
        tracker = DreamMetricsTracker()
        tracker.record_compute_time(5.0)
        metrics = tracker.get_metrics()
        assert metrics.opportunistic_compute_minutes == pytest.approx(5.0)

    def test_multiple_increments_accumulate(self):
        tracker = DreamMetricsTracker()
        tracker.record_compute_time(3.5)
        tracker.record_compute_time(1.5)
        metrics = tracker.get_metrics()
        assert metrics.opportunistic_compute_minutes == pytest.approx(5.0)

    def test_zero_increment_has_no_effect(self):
        tracker = DreamMetricsTracker()
        tracker.record_compute_time(0.0)
        metrics = tracker.get_metrics()
        assert metrics.opportunistic_compute_minutes == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test 2: record_preemption increments preemptions_count
# ---------------------------------------------------------------------------

class TestRecordPreemption:
    def test_single_preemption(self):
        tracker = DreamMetricsTracker()
        tracker.record_preemption()
        metrics = tracker.get_metrics()
        assert metrics.preemptions_count == 1

    def test_multiple_preemptions(self):
        tracker = DreamMetricsTracker()
        for _ in range(4):
            tracker.record_preemption()
        metrics = tracker.get_metrics()
        assert metrics.preemptions_count == 4

    def test_no_preemption_means_zero(self):
        tracker = DreamMetricsTracker()
        metrics = tracker.get_metrics()
        assert metrics.preemptions_count == 0


# ---------------------------------------------------------------------------
# Test 3 (TC22): record_blueprint_computed + record_blueprint_hit -> hit_rate
# ---------------------------------------------------------------------------

class TestBlueprintHitRate:
    def test_tc22_all_hits(self):
        """TC22: 3 computed, 3 hits => hit_rate = 1.0."""
        tracker = DreamMetricsTracker()
        tracker.record_blueprint_computed()
        tracker.record_blueprint_computed()
        tracker.record_blueprint_computed()
        tracker.record_blueprint_hit()
        tracker.record_blueprint_hit()
        tracker.record_blueprint_hit()
        metrics = tracker.get_metrics()
        assert metrics.blueprints_computed == 3
        assert metrics.blueprint_hit_rate == pytest.approx(1.0)

    def test_tc22_mixed_hits_and_misses(self):
        """TC22: 4 computed, 1 hit => hit_rate = 0.25."""
        tracker = DreamMetricsTracker()
        for _ in range(4):
            tracker.record_blueprint_computed()
        tracker.record_blueprint_hit()
        metrics = tracker.get_metrics()
        assert metrics.blueprints_computed == 4
        assert metrics.blueprint_hit_rate == pytest.approx(0.25)

    def test_tc22_no_hits(self):
        """TC22: 2 computed, 0 hits => hit_rate = 0.0."""
        tracker = DreamMetricsTracker()
        tracker.record_blueprint_computed()
        tracker.record_blueprint_computed()
        metrics = tracker.get_metrics()
        assert metrics.blueprint_hit_rate == pytest.approx(0.0)

    def test_compute_minutes_and_hit_rate_tracked_together(self):
        """TC22 composite: compute_minutes and hit_rate both tracked correctly."""
        tracker = DreamMetricsTracker()
        tracker.record_compute_time(10.0)
        tracker.record_blueprint_computed()
        tracker.record_blueprint_computed()
        tracker.record_blueprint_hit()
        metrics = tracker.get_metrics()
        assert metrics.opportunistic_compute_minutes == pytest.approx(10.0)
        assert metrics.blueprint_hit_rate == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Test 4: persist saves to JSON, load restores
# ---------------------------------------------------------------------------

class TestPersistAndLoad:
    def test_persist_creates_file(self, tmp_path: Path):
        tracker = DreamMetricsTracker()
        tracker.record_compute_time(7.5)
        tracker.record_preemption()
        path = tmp_path / "metrics.json"
        tracker.persist(path)
        assert path.exists()

    def test_persist_writes_valid_json(self, tmp_path: Path):
        tracker = DreamMetricsTracker()
        tracker.record_compute_time(2.0)
        path = tmp_path / "metrics.json"
        tracker.persist(path)
        data = json.loads(path.read_text())
        assert isinstance(data, dict)
        assert "opportunistic_compute_minutes" in data

    def test_load_restores_all_fields(self, tmp_path: Path):
        tracker = DreamMetricsTracker()
        tracker.record_compute_time(12.5)
        tracker.record_preemption()
        tracker.record_blueprint_computed()
        tracker.record_blueprint_computed()
        tracker.record_blueprint_hit()
        tracker.record_blueprint_discarded()
        tracker.record_dedup()
        tracker.record_cost_saved(0.042)
        path = tmp_path / "metrics.json"
        tracker.persist(path)

        restored = DreamMetricsTracker.load(path)
        m = restored.get_metrics()
        assert m.opportunistic_compute_minutes == pytest.approx(12.5)
        assert m.preemptions_count == 1
        assert m.blueprints_computed == 2
        assert m.blueprints_discarded_stale == 1
        assert m.jobs_deduplicated == 1
        assert m.estimated_cost_saved_usd == pytest.approx(0.042)

    def test_load_restores_hit_rate(self, tmp_path: Path):
        tracker = DreamMetricsTracker()
        tracker.record_blueprint_computed()
        tracker.record_blueprint_computed()
        tracker.record_blueprint_hit()
        path = tmp_path / "metrics.json"
        tracker.persist(path)

        restored = DreamMetricsTracker.load(path)
        m = restored.get_metrics()
        assert m.blueprint_hit_rate == pytest.approx(0.5)

    def test_load_nonexistent_raises(self, tmp_path: Path):
        with pytest.raises((FileNotFoundError, OSError)):
            DreamMetricsTracker.load(tmp_path / "does_not_exist.json")


# ---------------------------------------------------------------------------
# Test 5: reset zeroes all counters
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_zeroes_compute_minutes(self):
        tracker = DreamMetricsTracker()
        tracker.record_compute_time(99.0)
        tracker.reset()
        assert tracker.get_metrics().opportunistic_compute_minutes == pytest.approx(0.0)

    def test_reset_zeroes_preemptions(self):
        tracker = DreamMetricsTracker()
        for _ in range(5):
            tracker.record_preemption()
        tracker.reset()
        assert tracker.get_metrics().preemptions_count == 0

    def test_reset_zeroes_blueprints_and_hit_rate(self):
        tracker = DreamMetricsTracker()
        tracker.record_blueprint_computed()
        tracker.record_blueprint_hit()
        tracker.reset()
        m = tracker.get_metrics()
        assert m.blueprints_computed == 0
        assert m.blueprint_hit_rate == pytest.approx(0.0)

    def test_reset_zeroes_all_fields(self):
        tracker = DreamMetricsTracker()
        tracker.record_compute_time(5.0)
        tracker.record_preemption()
        tracker.record_blueprint_computed()
        tracker.record_blueprint_hit()
        tracker.record_blueprint_discarded()
        tracker.record_dedup()
        tracker.record_cost_saved(1.23)
        tracker.reset()
        m = tracker.get_metrics()
        assert m.opportunistic_compute_minutes == pytest.approx(0.0)
        assert m.preemptions_count == 0
        assert m.blueprints_computed == 0
        assert m.blueprints_discarded_stale == 0
        assert m.blueprint_hit_rate == pytest.approx(0.0)
        assert m.jobs_deduplicated == 0
        assert m.estimated_cost_saved_usd == pytest.approx(0.0)

    def test_accumulates_normally_after_reset(self):
        tracker = DreamMetricsTracker()
        tracker.record_compute_time(10.0)
        tracker.reset()
        tracker.record_compute_time(3.0)
        assert tracker.get_metrics().opportunistic_compute_minutes == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Test 6: hit_rate is 0.0 when no blueprints computed (division by zero guard)
# ---------------------------------------------------------------------------

class TestHitRateDivisionByZeroGuard:
    def test_zero_blueprints_computed_returns_zero_hit_rate(self):
        """Guard: no blueprints computed => hit_rate = 0.0, no ZeroDivisionError."""
        tracker = DreamMetricsTracker()
        m = tracker.get_metrics()
        assert m.blueprint_hit_rate == pytest.approx(0.0)

    def test_hits_without_computed_returns_zero_hit_rate(self):
        """record_blueprint_hit called before any computed: still 0.0, no crash."""
        tracker = DreamMetricsTracker()
        tracker.record_blueprint_hit()
        m = tracker.get_metrics()
        assert m.blueprint_hit_rate == pytest.approx(0.0)

    def test_after_reset_no_division_by_zero(self):
        tracker = DreamMetricsTracker()
        tracker.record_blueprint_computed()
        tracker.record_blueprint_hit()
        tracker.reset()
        m = tracker.get_metrics()
        assert m.blueprint_hit_rate == pytest.approx(0.0)
