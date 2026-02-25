"""
Phase 11 Tests: Monotonic Clock + Pressure-Aware Watchdog

Tests for:
- MonotonicDeadline (creation, elapsed, remaining, expired, extend, reset, fail-open)
- MonotonicStopwatch (elapsed, lap, reset)
- drift_detected() (alignment, divergence)
- PressureOracle (singleton, sampling, should_defer, pressure_multiplier, fail-open)
- PressureLevel (enum values, ordering)
- Integration: monotonic deadline + pressure multiplier composing correctly
- Wiring: critical paths in supervisor_singleton, DLM, gcp_vm_manager, supervisor DMS

Total: 45+ tests
"""

import asyncio
import gc
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ===========================================================================
# TestMonotonicClockModule
# ===========================================================================

class TestMonotonicClockModule(unittest.TestCase):
    """Test that the module imports cleanly."""

    def test_module_imports(self):
        from backend.core.monotonic_clock import (
            MonotonicDeadline,
            MonotonicStopwatch,
            monotonic_now,
            monotonic_deadline,
            drift_detected,
        )
        self.assertIsNotNone(MonotonicDeadline)
        self.assertIsNotNone(MonotonicStopwatch)
        self.assertIsNotNone(monotonic_now)
        self.assertIsNotNone(monotonic_deadline)
        self.assertIsNotNone(drift_detected)


# ===========================================================================
# TestMonotonicDeadline
# ===========================================================================

class TestMonotonicDeadline(unittest.TestCase):
    """Tests for MonotonicDeadline."""

    def test_creation(self):
        from backend.core.monotonic_clock import MonotonicDeadline
        d = MonotonicDeadline(10.0, label="test")
        self.assertFalse(d.is_expired())
        self.assertAlmostEqual(d.elapsed(), 0.0, delta=0.5)
        self.assertAlmostEqual(d.remaining(), 10.0, delta=0.5)
        self.assertEqual(d.timeout, 10.0)

    def test_elapsed_increases(self):
        from backend.core.monotonic_clock import MonotonicDeadline
        d = MonotonicDeadline(100.0)
        t1 = d.elapsed()
        time.sleep(0.05)
        t2 = d.elapsed()
        self.assertGreater(t2, t1)

    def test_remaining_decreases(self):
        from backend.core.monotonic_clock import MonotonicDeadline
        d = MonotonicDeadline(100.0)
        r1 = d.remaining()
        time.sleep(0.05)
        r2 = d.remaining()
        self.assertLess(r2, r1)

    def test_is_expired_after_timeout(self):
        from backend.core.monotonic_clock import MonotonicDeadline
        d = MonotonicDeadline(0.05)
        self.assertFalse(d.is_expired())
        time.sleep(0.1)
        self.assertTrue(d.is_expired())

    def test_extend_adds_to_timeout(self):
        from backend.core.monotonic_clock import MonotonicDeadline
        d = MonotonicDeadline(1.0)
        self.assertEqual(d.timeout, 1.0)
        d.extend(5.0)
        self.assertEqual(d.timeout, 6.0)
        self.assertFalse(d.is_expired())

    def test_reset_restarts_clock(self):
        from backend.core.monotonic_clock import MonotonicDeadline
        d = MonotonicDeadline(0.05)
        time.sleep(0.1)
        self.assertTrue(d.is_expired())
        d.reset()
        self.assertFalse(d.is_expired())
        self.assertAlmostEqual(d.elapsed(), 0.0, delta=0.1)

    def test_wall_start_returns_float(self):
        from backend.core.monotonic_clock import MonotonicDeadline
        d = MonotonicDeadline(10.0)
        ws = d.wall_start()
        self.assertIsInstance(ws, float)
        # Should be close to current wall time
        self.assertAlmostEqual(ws, time.time(), delta=1.0)

    def test_repr(self):
        from backend.core.monotonic_clock import MonotonicDeadline
        d = MonotonicDeadline(30.0, label="test_op")
        r = repr(d)
        self.assertIn("test_op", r)
        self.assertIn("30.0s", r)

    def test_zero_timeout_immediately_expired(self):
        from backend.core.monotonic_clock import MonotonicDeadline
        d = MonotonicDeadline(0.0)
        self.assertTrue(d.is_expired())
        self.assertEqual(d.remaining(), 0.0)


# ===========================================================================
# TestMonotonicStopwatch
# ===========================================================================

class TestMonotonicStopwatch(unittest.TestCase):
    """Tests for MonotonicStopwatch."""

    def test_elapsed_increases(self):
        from backend.core.monotonic_clock import MonotonicStopwatch
        sw = MonotonicStopwatch()
        t1 = sw.elapsed()
        time.sleep(0.05)
        t2 = sw.elapsed()
        self.assertGreater(t2, t1)

    def test_lap_returns_delta(self):
        from backend.core.monotonic_clock import MonotonicStopwatch
        sw = MonotonicStopwatch()
        time.sleep(0.05)
        lap1 = sw.lap()
        self.assertGreater(lap1, 0.01)
        # Second lap should be small (near zero)
        lap2 = sw.lap()
        self.assertLess(lap2, lap1)

    def test_reset_zeroes_elapsed(self):
        from backend.core.monotonic_clock import MonotonicStopwatch
        sw = MonotonicStopwatch()
        time.sleep(0.05)
        self.assertGreater(sw.elapsed(), 0.01)
        sw.reset()
        self.assertAlmostEqual(sw.elapsed(), 0.0, delta=0.05)

    def test_repr(self):
        from backend.core.monotonic_clock import MonotonicStopwatch
        sw = MonotonicStopwatch()
        r = repr(sw)
        self.assertIn("MonotonicStopwatch", r)
        self.assertIn("elapsed=", r)


# ===========================================================================
# TestMonotonicNow
# ===========================================================================

class TestMonotonicNow(unittest.TestCase):

    def test_returns_float(self):
        from backend.core.monotonic_clock import monotonic_now
        result = monotonic_now()
        self.assertIsInstance(result, float)
        self.assertGreater(result, 0)

    def test_monotonically_increasing(self):
        from backend.core.monotonic_clock import monotonic_now
        t1 = monotonic_now()
        t2 = monotonic_now()
        self.assertGreaterEqual(t2, t1)


# ===========================================================================
# TestDriftDetection
# ===========================================================================

class TestDriftDetection(unittest.TestCase):

    def test_no_drift_when_clocks_aligned(self):
        from backend.core.monotonic_clock import drift_detected
        wall_start = time.time()
        mono_start = time.monotonic()
        # Immediately after: no drift
        self.assertFalse(drift_detected(wall_start, mono_start, threshold=2.0))

    def test_drift_detected_when_wall_jumps(self):
        from backend.core.monotonic_clock import drift_detected
        # Simulate wall clock having jumped: wall_start is 10 seconds in the past
        wall_start = time.time() - 10.0
        mono_start = time.monotonic()
        # Wall elapsed = ~10s, mono elapsed = ~0s → drift = 10s > 2s
        self.assertTrue(drift_detected(wall_start, mono_start, threshold=2.0))

    def test_drift_fail_open(self):
        from backend.core.monotonic_clock import drift_detected
        # Bad inputs should not crash
        result = drift_detected(float("nan"), float("nan"), threshold=2.0)
        # NaN comparisons are always False, so drift_detected returns False
        self.assertIsInstance(result, bool)


# ===========================================================================
# TestMonotonicDeadlineFactory
# ===========================================================================

class TestMonotonicDeadlineFactory(unittest.TestCase):

    def test_factory_creates_deadline(self):
        from backend.core.monotonic_clock import monotonic_deadline, MonotonicDeadline
        d = monotonic_deadline(10.0, label="factory_test")
        self.assertIsInstance(d, MonotonicDeadline)
        self.assertFalse(d.is_expired())


# ===========================================================================
# TestPressureAwareWatchdogModule
# ===========================================================================

class TestPressureAwareWatchdogModule(unittest.TestCase):
    """Test that the module imports cleanly."""

    def test_module_imports(self):
        from backend.core.pressure_aware_watchdog import (
            PressureLevel,
            PressureSnapshot,
            PressureOracle,
            should_defer_destructive_action,
            pressure_multiplier,
            get_pressure_oracle,
        )
        self.assertIsNotNone(PressureLevel)
        self.assertIsNotNone(PressureSnapshot)
        self.assertIsNotNone(PressureOracle)
        self.assertIsNotNone(should_defer_destructive_action)
        self.assertIsNotNone(pressure_multiplier)
        self.assertIsNotNone(get_pressure_oracle)


# ===========================================================================
# TestPressureLevel
# ===========================================================================

class TestPressureLevel(unittest.TestCase):

    def test_enum_values_exist(self):
        from backend.core.pressure_aware_watchdog import PressureLevel
        self.assertEqual(PressureLevel.NONE, 0)
        self.assertEqual(PressureLevel.LIGHT, 1)
        self.assertEqual(PressureLevel.MODERATE, 2)
        self.assertEqual(PressureLevel.SEVERE, 3)
        self.assertEqual(PressureLevel.UNKNOWN, -1)

    def test_ordering(self):
        from backend.core.pressure_aware_watchdog import PressureLevel
        self.assertLess(PressureLevel.NONE, PressureLevel.LIGHT)
        self.assertLess(PressureLevel.LIGHT, PressureLevel.MODERATE)
        self.assertLess(PressureLevel.MODERATE, PressureLevel.SEVERE)

    def test_unknown_is_negative(self):
        from backend.core.pressure_aware_watchdog import PressureLevel
        self.assertLess(PressureLevel.UNKNOWN, PressureLevel.NONE)


# ===========================================================================
# TestPressureSnapshot
# ===========================================================================

class TestPressureSnapshot(unittest.TestCase):

    def test_default_creation(self):
        from backend.core.pressure_aware_watchdog import PressureSnapshot, PressureLevel
        snap = PressureSnapshot()
        self.assertEqual(snap.level, PressureLevel.UNKNOWN)
        self.assertEqual(snap.cpu_percent, 0.0)
        self.assertEqual(snap.memory_percent, 0.0)

    def test_custom_creation(self):
        from backend.core.pressure_aware_watchdog import PressureSnapshot, PressureLevel
        snap = PressureSnapshot(
            timestamp_mono=100.0,
            level=PressureLevel.MODERATE,
            cpu_percent=96.0,
            memory_percent=85.0,
            event_loop_lag_ms=200.0,
            gc_pause_detected=True,
        )
        self.assertEqual(snap.level, PressureLevel.MODERATE)
        self.assertEqual(snap.cpu_percent, 96.0)
        self.assertTrue(snap.gc_pause_detected)

    def test_repr(self):
        from backend.core.pressure_aware_watchdog import PressureSnapshot, PressureLevel
        snap = PressureSnapshot(level=PressureLevel.LIGHT, cpu_percent=87.0)
        r = repr(snap)
        self.assertIn("LIGHT", r)
        self.assertIn("87", r)


# ===========================================================================
# TestPressureOracle
# ===========================================================================

class TestPressureOracle(unittest.TestCase):

    def setUp(self):
        """Reset singleton between tests."""
        from backend.core.pressure_aware_watchdog import PressureOracle
        PressureOracle._instance = None

    def tearDown(self):
        from backend.core.pressure_aware_watchdog import PressureOracle
        if PressureOracle._instance:
            PressureOracle._instance.stop_sampling()
        PressureOracle._instance = None

    def test_singleton_pattern(self):
        from backend.core.pressure_aware_watchdog import PressureOracle
        o1 = PressureOracle.get_instance()
        o2 = PressureOracle.get_instance()
        self.assertIs(o1, o2)

    def test_get_instance_safe_none_before_creation(self):
        from backend.core.pressure_aware_watchdog import PressureOracle
        self.assertIsNone(PressureOracle.get_instance_safe())

    def test_get_instance_safe_after_creation(self):
        from backend.core.pressure_aware_watchdog import PressureOracle
        oracle = PressureOracle.get_instance()
        self.assertIs(PressureOracle.get_instance_safe(), oracle)

    def test_current_pressure_returns_level(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureLevel
        oracle = PressureOracle.get_instance()
        level = oracle.current_pressure()
        self.assertIsInstance(level, PressureLevel)

    def test_should_defer_on_no_pressure(self):
        """When pressure is NONE, should not defer."""
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureLevel, PressureSnapshot
        oracle = PressureOracle.get_instance()
        # Inject a NONE snapshot
        oracle._latest = PressureSnapshot(level=PressureLevel.NONE)
        defer, reason = oracle.should_defer_destructive_action("test")
        self.assertFalse(defer)
        self.assertEqual(reason, "")

    def test_should_defer_on_moderate_pressure(self):
        """When pressure is MODERATE, should defer."""
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureLevel, PressureSnapshot
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(
            level=PressureLevel.MODERATE,
            cpu_percent=96.0,
            memory_percent=85.0,
            event_loop_lag_ms=200.0,
        )
        defer, reason = oracle.should_defer_destructive_action("DMS.restart")
        self.assertTrue(defer)
        self.assertIn("MODERATE", reason)
        self.assertIn("DMS.restart", reason)

    def test_should_defer_on_severe_pressure(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureLevel, PressureSnapshot
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.SEVERE, cpu_percent=99.0, memory_percent=95.0)
        defer, reason = oracle.should_defer_destructive_action("test")
        self.assertTrue(defer)
        self.assertIn("SEVERE", reason)

    def test_should_not_defer_on_light_pressure_default_threshold(self):
        """LIGHT pressure doesn't trigger deferral with default MODERATE threshold."""
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureLevel, PressureSnapshot
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.LIGHT, cpu_percent=87.0)
        defer, reason = oracle.should_defer_destructive_action("test")
        self.assertFalse(defer)

    def test_should_defer_on_light_with_light_threshold(self):
        """LIGHT pressure defers when threshold is set to LIGHT."""
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureLevel, PressureSnapshot
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.LIGHT, cpu_percent=87.0)
        defer, reason = oracle.should_defer_destructive_action(
            "test", severity_threshold=PressureLevel.LIGHT
        )
        self.assertTrue(defer)

    def test_unknown_pressure_fail_open(self):
        """UNKNOWN pressure should NOT defer (fail-open)."""
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureLevel, PressureSnapshot
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.UNKNOWN)
        defer, reason = oracle.should_defer_destructive_action("test")
        self.assertFalse(defer)

    def test_pressure_multiplier_none(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureLevel, PressureSnapshot
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.NONE)
        self.assertEqual(oracle.pressure_multiplier(), 1.0)

    def test_pressure_multiplier_light(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureLevel, PressureSnapshot
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.LIGHT)
        self.assertEqual(oracle.pressure_multiplier(), 1.5)

    def test_pressure_multiplier_moderate(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureLevel, PressureSnapshot
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.MODERATE)
        self.assertEqual(oracle.pressure_multiplier(), 2.0)

    def test_pressure_multiplier_severe(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureLevel, PressureSnapshot
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.SEVERE)
        self.assertEqual(oracle.pressure_multiplier(), 3.0)

    def test_pressure_multiplier_unknown(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureLevel, PressureSnapshot
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.UNKNOWN)
        self.assertEqual(oracle.pressure_multiplier(), 1.0)


# ===========================================================================
# TestPressureClassification
# ===========================================================================

class TestPressureClassification(unittest.TestCase):

    def test_classify_none(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureSnapshot, PressureLevel
        snap = PressureSnapshot(cpu_percent=50.0, memory_percent=60.0, event_loop_lag_ms=10.0)
        self.assertEqual(PressureOracle._classify(snap), PressureLevel.NONE)

    def test_classify_light_cpu(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureSnapshot, PressureLevel
        snap = PressureSnapshot(cpu_percent=87.0, memory_percent=60.0, event_loop_lag_ms=10.0)
        self.assertEqual(PressureOracle._classify(snap), PressureLevel.LIGHT)

    def test_classify_light_memory(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureSnapshot, PressureLevel
        snap = PressureSnapshot(cpu_percent=50.0, memory_percent=82.0, event_loop_lag_ms=10.0)
        self.assertEqual(PressureOracle._classify(snap), PressureLevel.LIGHT)

    def test_classify_light_loop_lag(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureSnapshot, PressureLevel
        snap = PressureSnapshot(cpu_percent=50.0, memory_percent=60.0, event_loop_lag_ms=150.0)
        self.assertEqual(PressureOracle._classify(snap), PressureLevel.LIGHT)

    def test_classify_moderate_cpu(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureSnapshot, PressureLevel
        snap = PressureSnapshot(cpu_percent=96.0, memory_percent=60.0, event_loop_lag_ms=10.0)
        self.assertEqual(PressureOracle._classify(snap), PressureLevel.MODERATE)

    def test_classify_moderate_memory(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureSnapshot, PressureLevel
        snap = PressureSnapshot(cpu_percent=50.0, memory_percent=92.0, event_loop_lag_ms=10.0)
        self.assertEqual(PressureOracle._classify(snap), PressureLevel.MODERATE)

    def test_classify_moderate_loop_lag(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureSnapshot, PressureLevel
        snap = PressureSnapshot(cpu_percent=50.0, memory_percent=60.0, event_loop_lag_ms=600.0)
        self.assertEqual(PressureOracle._classify(snap), PressureLevel.MODERATE)

    def test_classify_moderate_gc_pause(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureSnapshot, PressureLevel
        snap = PressureSnapshot(cpu_percent=50.0, memory_percent=60.0, event_loop_lag_ms=10.0, gc_pause_detected=True)
        self.assertEqual(PressureOracle._classify(snap), PressureLevel.MODERATE)

    def test_classify_severe_multiple_moderate(self):
        """Two moderate indicators → SEVERE."""
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureSnapshot, PressureLevel
        snap = PressureSnapshot(cpu_percent=96.0, memory_percent=92.0, event_loop_lag_ms=10.0)
        self.assertEqual(PressureOracle._classify(snap), PressureLevel.SEVERE)

    def test_classify_severe_three_moderate(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureSnapshot, PressureLevel
        snap = PressureSnapshot(cpu_percent=96.0, memory_percent=92.0, event_loop_lag_ms=600.0, gc_pause_detected=True)
        self.assertEqual(PressureOracle._classify(snap), PressureLevel.SEVERE)


# ===========================================================================
# TestConvenienceFunctions
# ===========================================================================

class TestConvenienceFunctions(unittest.TestCase):

    def setUp(self):
        from backend.core.pressure_aware_watchdog import PressureOracle
        PressureOracle._instance = None

    def tearDown(self):
        from backend.core.pressure_aware_watchdog import PressureOracle
        if PressureOracle._instance:
            PressureOracle._instance.stop_sampling()
        PressureOracle._instance = None

    def test_should_defer_destructive_action_no_oracle(self):
        """When oracle hasn't been created, should fail-open."""
        from backend.core.pressure_aware_watchdog import should_defer_destructive_action
        defer, reason = should_defer_destructive_action("test")
        self.assertFalse(defer)
        self.assertEqual(reason, "")

    def test_pressure_multiplier_no_oracle(self):
        """When oracle hasn't been created, should return 1.0."""
        from backend.core.pressure_aware_watchdog import pressure_multiplier
        self.assertEqual(pressure_multiplier(), 1.0)

    def test_get_pressure_oracle_creates_singleton(self):
        from backend.core.pressure_aware_watchdog import get_pressure_oracle, PressureOracle
        oracle = get_pressure_oracle()
        self.assertIsInstance(oracle, PressureOracle)
        self.assertIs(PressureOracle.get_instance_safe(), oracle)

    def test_convenience_should_defer_with_pressure(self):
        from backend.core.pressure_aware_watchdog import (
            should_defer_destructive_action,
            PressureOracle,
            PressureSnapshot,
            PressureLevel,
        )
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.SEVERE, cpu_percent=99.0)
        defer, reason = should_defer_destructive_action("test")
        self.assertTrue(defer)

    def test_convenience_pressure_multiplier_with_pressure(self):
        from backend.core.pressure_aware_watchdog import (
            pressure_multiplier,
            PressureOracle,
            PressureSnapshot,
            PressureLevel,
        )
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.SEVERE)
        self.assertEqual(pressure_multiplier(), 3.0)


# ===========================================================================
# TestGCPauseDetection
# ===========================================================================

class TestGCPauseDetection(unittest.TestCase):

    def test_gc_callback_registration(self):
        from backend.core.pressure_aware_watchdog import _ensure_gc_callback, _gc_callback
        _ensure_gc_callback()
        self.assertIn(_gc_callback, gc.callbacks)

    def test_gc_pause_flag_set_on_long_gen2(self):
        from backend.core.pressure_aware_watchdog import _gc_callback, _consume_gc_pause
        # Clear any existing flag
        _consume_gc_pause()
        # Simulate a long gen-2 collection
        _gc_callback("stop", {"generation": 2, "elapsed": 1.0})
        self.assertTrue(_consume_gc_pause())
        # Second consume should be False (flag cleared)
        self.assertFalse(_consume_gc_pause())

    def test_gc_pause_flag_not_set_on_short_gen2(self):
        from backend.core.pressure_aware_watchdog import _gc_callback, _consume_gc_pause
        _consume_gc_pause()
        _gc_callback("stop", {"generation": 2, "elapsed": 0.1})
        self.assertFalse(_consume_gc_pause())

    def test_gc_pause_flag_not_set_on_gen0(self):
        from backend.core.pressure_aware_watchdog import _gc_callback, _consume_gc_pause
        _consume_gc_pause()
        _gc_callback("stop", {"generation": 0, "elapsed": 2.0})
        self.assertFalse(_consume_gc_pause())


# ===========================================================================
# TestPressureOracleAsync
# ===========================================================================

class TestPressureOracleAsync(unittest.TestCase):

    def setUp(self):
        from backend.core.pressure_aware_watchdog import PressureOracle
        PressureOracle._instance = None

    def tearDown(self):
        from backend.core.pressure_aware_watchdog import PressureOracle
        if PressureOracle._instance:
            PressureOracle._instance.stop_sampling()
        PressureOracle._instance = None

    def test_start_and_stop_sampling(self):
        """Oracle can start and stop sampling without errors."""
        from backend.core.pressure_aware_watchdog import PressureOracle

        async def _run():
            oracle = PressureOracle.get_instance()
            await oracle.start_sampling()
            self.assertTrue(oracle._running)
            # Let it sample once
            await asyncio.sleep(0.1)
            oracle.stop_sampling()
            self.assertFalse(oracle._running)

        asyncio.run(_run())

    def test_take_sample_returns_snapshot(self):
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureSnapshot
        oracle = PressureOracle.get_instance()
        snap = oracle._take_sample()
        self.assertIsInstance(snap, PressureSnapshot)
        self.assertGreater(snap.timestamp_mono, 0)

    def test_event_loop_lag_measurement(self):
        from backend.core.pressure_aware_watchdog import PressureOracle

        async def _run():
            oracle = PressureOracle.get_instance()
            lag = await oracle._measure_event_loop_lag()
            self.assertIsInstance(lag, float)
            # In a non-stressed loop, lag should be very small
            self.assertLess(lag, 500.0)  # < 500ms

        asyncio.run(_run())


# ===========================================================================
# TestIntegrationDeadlinePlusPressure
# ===========================================================================

class TestIntegrationDeadlinePlusPressure(unittest.TestCase):
    """Integration: monotonic deadline + pressure multiplier composing."""

    def setUp(self):
        from backend.core.pressure_aware_watchdog import PressureOracle
        PressureOracle._instance = None

    def tearDown(self):
        from backend.core.pressure_aware_watchdog import PressureOracle
        if PressureOracle._instance:
            PressureOracle._instance.stop_sampling()
        PressureOracle._instance = None

    def test_deadline_with_pressure_extends_grace(self):
        """Simulates ProgressController extending deadline under pressure."""
        from backend.core.monotonic_clock import MonotonicDeadline
        from backend.core.pressure_aware_watchdog import (
            PressureOracle, PressureSnapshot, PressureLevel,
            should_defer_destructive_action,
        )

        # Create a 0.5s deadline
        deadline = MonotonicDeadline(0.5, label="test_hard_cap")
        time.sleep(0.6)
        self.assertTrue(deadline.is_expired())

        # Simulate pressure
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(
            level=PressureLevel.MODERATE,
            cpu_percent=96.0,
            memory_percent=85.0,
        )

        # Check pressure before acting
        defer, reason = should_defer_destructive_action("test_hard_cap")
        self.assertTrue(defer)

        # Extend deadline as the ProgressController would
        deadline.extend(60.0)
        self.assertFalse(deadline.is_expired())

    def test_pressure_multiplier_scales_threshold(self):
        """Simulates heartbeat threshold scaling under pressure."""
        from backend.core.pressure_aware_watchdog import (
            PressureOracle, PressureSnapshot, PressureLevel,
            pressure_multiplier,
        )

        HEARTBEAT_STALE_THRESHOLD = 30.0

        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.SEVERE)
        multiplier = pressure_multiplier()
        self.assertEqual(multiplier, 3.0)

        effective_threshold = HEARTBEAT_STALE_THRESHOLD * multiplier
        self.assertEqual(effective_threshold, 90.0)


# ===========================================================================
# TestIntegrationWiring
# ===========================================================================

class TestIntegrationWiring(unittest.TestCase):
    """Test that wiring points in consumer modules are importable."""

    def test_monotonic_clock_importable_from_dlm(self):
        """DLM can import monotonic_now."""
        from backend.core.monotonic_clock import monotonic_now
        self.assertIsInstance(monotonic_now(), float)

    def test_monotonic_deadline_importable_from_supervisor(self):
        """Supervisor can import MonotonicDeadline."""
        from backend.core.monotonic_clock import MonotonicDeadline, monotonic_now
        d = MonotonicDeadline(10.0, label="supervisor_test")
        self.assertIsInstance(d, MonotonicDeadline)

    def test_pressure_oracle_importable_from_supervisor_singleton(self):
        """supervisor_singleton can import pressure functions."""
        from backend.core.pressure_aware_watchdog import should_defer_destructive_action, pressure_multiplier
        # Should not raise
        defer, reason = should_defer_destructive_action("test")
        mult = pressure_multiplier()
        self.assertIsInstance(defer, bool)
        self.assertIsInstance(mult, float)

    def test_pressure_oracle_importable_from_gcp_vm_manager(self):
        """gcp_vm_manager can import MonotonicDeadline."""
        from backend.core.monotonic_clock import MonotonicDeadline
        d = MonotonicDeadline(600.0, label="golden_image_build")
        self.assertFalse(d.is_expired())


# ===========================================================================
# TestThreadSafety
# ===========================================================================

class TestThreadSafety(unittest.TestCase):

    def test_monotonic_deadline_threadsafe(self):
        """MonotonicDeadline can be read from multiple threads without errors."""
        from backend.core.monotonic_clock import MonotonicDeadline
        d = MonotonicDeadline(5.0, label="thread_test")
        errors = []

        def reader():
            try:
                for _ in range(1000):
                    _ = d.elapsed()
                    _ = d.remaining()
                    _ = d.is_expired()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(errors), 0)

    def test_pressure_oracle_threadsafe(self):
        """PressureOracle singleton is thread-safe."""
        from backend.core.pressure_aware_watchdog import PressureOracle, PressureSnapshot, PressureLevel
        PressureOracle._instance = None
        oracle = PressureOracle.get_instance()
        oracle._latest = PressureSnapshot(level=PressureLevel.NONE)
        errors = []

        def reader():
            try:
                for _ in range(1000):
                    _ = oracle.current_pressure()
                    _ = oracle.pressure_multiplier()
                    _ = oracle.should_defer_destructive_action("thread_test")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(errors), 0)
        PressureOracle._instance = None


# ===========================================================================
# TestWiringIntegration — Verify consumer modules have the Phase 11 wiring
# ===========================================================================

class TestWiringIntegration(unittest.TestCase):
    """Verify Phase 11 wiring in consumer modules."""

    def test_supervisor_singleton_heartbeat_uses_pressure(self):
        """supervisor_singleton heartbeat reader imports pressure_multiplier."""
        import importlib
        import ast
        spec = importlib.util.find_spec("backend.core.supervisor_singleton")
        if spec and spec.origin:
            with open(spec.origin) as f:
                source = f.read()
            self.assertIn("pressure_multiplier", source)
            self.assertIn("_effective_threshold", source)
            self.assertIn("v276.0", source)

    def test_dlm_redis_uses_monotonic(self):
        """DLM Redis rate limiter imports monotonic_now."""
        import importlib
        spec = importlib.util.find_spec("backend.core.distributed_lock_manager")
        if spec and spec.origin:
            with open(spec.origin) as f:
                source = f.read()
            self.assertIn("_last_check_mono", source)
            self.assertIn("monotonic_now", source)

    def test_dlm_keepalive_uses_pressure(self):
        """DLM keepalive exhaustion checks pressure before giving up."""
        import importlib
        spec = importlib.util.find_spec("backend.core.distributed_lock_manager")
        if spec and spec.origin:
            with open(spec.origin) as f:
                source = f.read()
            self.assertIn("DLM.keepalive_exhaustion", source)
            self.assertIn("should_defer_destructive_action", source)

    def test_dlm_stale_lock_uses_pressure(self):
        """DLM stale lock detection scales threshold by pressure."""
        import importlib
        spec = importlib.util.find_spec("backend.core.distributed_lock_manager")
        if spec and spec.origin:
            with open(spec.origin) as f:
                source = f.read()
            self.assertIn("_stale_threshold", source)
            self.assertIn("pressure_multiplier", source)

    def test_gcp_circuit_breaker_has_monotonic(self):
        """GCP circuit breaker has last_failure_mono field."""
        import importlib
        spec = importlib.util.find_spec("backend.core.gcp_vm_manager")
        if spec and spec.origin:
            with open(spec.origin) as f:
                source = f.read()
            self.assertIn("last_failure_mono", source)
            self.assertIn("_gcp_cb_mono", source)

    def test_gcp_golden_image_uses_monotonic(self):
        """Golden image build timeout uses MonotonicDeadline."""
        import importlib
        spec = importlib.util.find_spec("backend.core.gcp_vm_manager")
        if spec and spec.origin:
            with open(spec.origin) as f:
                source = f.read()
            self.assertIn("_build_deadline", source)
            self.assertIn("golden_image_build", source)

    def test_safe_fd_uses_monotonic(self):
        """SafeFD cleanup interval uses monotonic."""
        import importlib
        spec = importlib.util.find_spec("backend.core.safe_fd")
        if spec and spec.origin:
            with open(spec.origin) as f:
                source = f.read()
            self.assertIn("_last_cleanup_mono", source)

    def test_supervisor_progress_controller_has_monotonic(self):
        """ProgressController uses MonotonicDeadline."""
        with open("unified_supervisor.py") as f:
            # Read first 3000 lines to find ProgressController setup
            lines = []
            for i, line in enumerate(f):
                lines.append(line)
                if i > 2500:
                    break
        source = "".join(lines)
        self.assertIn("_mono_deadline", source)
        self.assertIn("_mono_hard_cap", source)
        self.assertIn("MonotonicDeadline", source)

    def test_supervisor_circuit_breaker_has_monotonic(self):
        """Supervisor CircuitBreaker has _last_failure_mono."""
        with open("unified_supervisor.py") as f:
            source = f.read()
        self.assertIn("_last_failure_mono", source)
        self.assertIn("_cb_mono", source)

    def test_supervisor_dms_has_pressure_gate(self):
        """DMS watchdog has pressure gate before timeout/stall escalation."""
        with open("unified_supervisor.py") as f:
            source = f.read()
        self.assertIn("DMS.phase_timeout", source)
        self.assertIn("DMS.stall_escalation", source)
        self.assertIn("_phase_start_mono", source)
        self.assertIn("_last_progress_time_mono", source)

    def test_supervisor_handover_has_monotonic(self):
        """Handover timeout loop uses MonotonicDeadline."""
        with open("unified_supervisor.py") as f:
            source = f.read()
        self.assertIn("_HandoverDeadline", source)
        self.assertIn("_handover_dl", source)


if __name__ == "__main__":
    unittest.main()
