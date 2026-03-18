"""tests/unit/core/test_entropy_monitor.py — P3-4 long-uptime entropy tests."""
from __future__ import annotations

import pytest

from backend.core.entropy_monitor import (
    CompactionAdvice,
    EntropyMetric,
    EntropyMonitor,
    EntropySnapshot,
    EntropyThreshold,
    get_entropy_monitor,
)


class TestEntropyThreshold:
    def test_in_budget_returns_empty_advice(self):
        t = EntropyThreshold(EntropyMetric.QUEUE_DEPTH, warn_at=400, critical_at=800)
        advice = t.evaluate(100)
        assert advice.in_budget is True
        assert advice.required is False

    def test_warn_at_boundary(self):
        t = EntropyThreshold(EntropyMetric.QUEUE_DEPTH, warn_at=400, critical_at=800)
        advice = t.evaluate(400)
        assert advice.required is False
        assert advice.reason  # non-empty

    def test_critical_at_boundary(self):
        t = EntropyThreshold(EntropyMetric.QUEUE_DEPTH, warn_at=400, critical_at=800)
        advice = t.evaluate(800)
        assert advice.required is True

    def test_above_critical(self):
        t = EntropyThreshold(EntropyMetric.FD_COUNT, warn_at=512, critical_at=1024)
        advice = t.evaluate(2000)
        assert advice.required is True
        assert "2000" in advice.reason or "fd_count" in advice.reason


class TestCompactionAdvice:
    def test_in_budget_property(self):
        a = CompactionAdvice(required=False, reason="")
        assert a.in_budget is True

    def test_not_in_budget_when_reason_set(self):
        a = CompactionAdvice(required=False, reason="warn")
        assert a.in_budget is False

    def test_not_in_budget_when_required(self):
        a = CompactionAdvice(required=True, reason="critical")
        assert a.in_budget is False


class TestEntropyMonitor:
    def test_initial_readings_zero(self):
        m = EntropyMonitor()
        snap = m.snapshot()
        assert snap.get(EntropyMetric.QUEUE_DEPTH) == 0.0

    def test_record_updates_current(self):
        m = EntropyMonitor()
        m.record(EntropyMetric.QUEUE_DEPTH, 250)
        assert m.snapshot().get(EntropyMetric.QUEUE_DEPTH) == 250

    def test_should_compact_returns_in_budget_when_below_warn(self):
        m = EntropyMonitor()
        m.record(EntropyMetric.QUEUE_DEPTH, 10)
        assert m.should_compact(EntropyMetric.QUEUE_DEPTH).in_budget is True

    def test_should_compact_warns_above_warn_threshold(self):
        m = EntropyMonitor()
        m.record(EntropyMetric.QUEUE_DEPTH, 500)   # above default warn=400
        advice = m.should_compact(EntropyMetric.QUEUE_DEPTH)
        assert advice.required is False
        assert advice.reason  # non-empty warning

    def test_should_compact_required_above_critical(self):
        m = EntropyMonitor()
        m.record(EntropyMetric.QUEUE_DEPTH, 1000)  # above default critical=800
        assert m.should_compact(EntropyMetric.QUEUE_DEPTH).required is True

    def test_compact_calls_registered_handler(self):
        m = EntropyMonitor()
        m.record(EntropyMetric.CACHE_SIZE, 30_000)
        compacted = []

        def handler(metric):
            m.record(metric, 100)   # simulate cleanup
            compacted.append(metric)

        m.register_handler(EntropyMetric.CACHE_SIZE, handler)
        m.compact(EntropyMetric.CACHE_SIZE)
        assert compacted == [EntropyMetric.CACHE_SIZE]
        assert m.snapshot().get(EntropyMetric.CACHE_SIZE) == 100

    def test_compact_no_handler_does_not_raise(self):
        m = EntropyMonitor()
        m.record(EntropyMetric.LOG_BYTES, 999_999_999)
        m.compact(EntropyMetric.LOG_BYTES)   # no handler registered → log warning only

    def test_compact_if_needed_auto_runs_when_critical(self):
        m = EntropyMonitor()
        m.record(EntropyMetric.FD_COUNT, 2000)   # above critical=1024
        compacted = []
        m.register_handler(EntropyMetric.FD_COUNT, lambda _: compacted.append(True))
        advice = m.compact_if_needed(EntropyMetric.FD_COUNT)
        assert advice.required is True
        assert compacted  # handler ran

    def test_compact_if_needed_no_action_below_threshold(self):
        m = EntropyMonitor()
        m.record(EntropyMetric.FD_COUNT, 10)
        compacted = []
        m.register_handler(EntropyMetric.FD_COUNT, lambda _: compacted.append(True))
        advice = m.compact_if_needed(EntropyMetric.FD_COUNT)
        assert advice.in_budget is True
        assert not compacted

    def test_compact_all_needed_returns_dict(self):
        m = EntropyMonitor()
        result = m.compact_all_needed()
        assert set(result.keys()) == {metric.value for metric in EntropyMetric}

    def test_seconds_since_last_compact_is_inf_before_any_compaction(self):
        m = EntropyMonitor()
        assert m.seconds_since_last_compact(EntropyMetric.QUEUE_DEPTH) == float("inf")

    def test_seconds_since_last_compact_is_small_after_compaction(self):
        m = EntropyMonitor()
        m.record(EntropyMetric.QUEUE_DEPTH, 1000)
        m.register_handler(EntropyMetric.QUEUE_DEPTH, lambda _: None)
        m.compact(EntropyMetric.QUEUE_DEPTH)
        assert m.seconds_since_last_compact(EntropyMetric.QUEUE_DEPTH) < 1.0

    def test_handler_exception_does_not_abort_other_handlers(self):
        m = EntropyMonitor()
        ran = []

        def bad_handler(_):
            raise RuntimeError("boom")

        def good_handler(_):
            ran.append(True)

        m.register_handler(EntropyMetric.LOG_BYTES, bad_handler)
        m.register_handler(EntropyMetric.LOG_BYTES, good_handler)
        m.compact(EntropyMetric.LOG_BYTES)
        assert ran  # second handler still ran


class TestEntropySnapshot:
    def test_capture_creates_immutable_snapshot(self):
        import dataclasses
        snap = EntropySnapshot.capture({EntropyMetric.QUEUE_DEPTH: 42.0})
        assert dataclasses.is_dataclass(snap)
        with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            snap.timestamp_mono = 0.0  # type: ignore[misc]

    def test_get_returns_default_for_missing_metric(self):
        snap = EntropySnapshot.capture({})
        assert snap.get(EntropyMetric.QUEUE_DEPTH, default=99.0) == 99.0

    def test_get_returns_recorded_value(self):
        snap = EntropySnapshot.capture({EntropyMetric.CACHE_SIZE: 1234.0})
        assert snap.get(EntropyMetric.CACHE_SIZE) == 1234.0


class TestCustomThresholds:
    def test_custom_thresholds_override_defaults(self):
        m = EntropyMonitor(thresholds={
            EntropyMetric.QUEUE_DEPTH: (10, 20),
        })
        m.record(EntropyMetric.QUEUE_DEPTH, 15)
        advice = m.should_compact(EntropyMetric.QUEUE_DEPTH)
        assert advice.required is False
        assert advice.reason   # warn range 10–20

        m.record(EntropyMetric.QUEUE_DEPTH, 25)
        assert m.should_compact(EntropyMetric.QUEUE_DEPTH).required is True


class TestSingleton:
    def test_module_singleton_is_reused(self):
        m1 = get_entropy_monitor()
        m2 = get_entropy_monitor()
        assert m1 is m2
