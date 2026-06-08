"""Slice 147 — Adaptive Economic Quarantine.

A persistently economically-dead Claude lane (credit-too-low until the operator
funds it — could be hours/days) shouldn't be re-probed every fixed recovery
window, sacrificing one op each time. This adds an EXPONENTIAL re-probe backoff:
each failed economic HALF_OPEN probe doubles the effective recovery window (capped);
a successful probe (funding returned) resets it. Gated default-FALSE per §33.1 →
OFF is byte-identical fixed-window behavior. Transport trips are unaffected.
"""
from __future__ import annotations

import os
import unittest

from backend.core.ouroboros.governance import claude_circuit_breaker as CB
from backend.core.ouroboros.governance.claude_circuit_breaker import (
    ClaudeCircuitBreaker,
    CircuitState,
)


class TestAdaptiveGate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("JARVIS_CLAUDE_ECONOMIC_ADAPTIVE_REPROBE_ENABLED", None)

    def test_adaptive_default_false(self):
        self.assertFalse(CB.adaptive_reprobe_enabled())

    def test_off_window_is_fixed_regardless_of_exponent(self):
        b = ClaudeCircuitBreaker(recovery_window_s=900.0)
        b._economic_reprobe_exponent = 3  # even if set, OFF must ignore it
        self.assertEqual(b._effective_recovery_window_s(), 900.0)


class TestAdaptiveBackoff(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_CLAUDE_ECONOMIC_ADAPTIVE_REPROBE_ENABLED"] = "1"
        os.environ["JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED"] = "1"

    def tearDown(self):
        for k in ("JARVIS_CLAUDE_ECONOMIC_ADAPTIVE_REPROBE_ENABLED",
                  "JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED",
                  "JARVIS_CLAUDE_ECONOMIC_REPROBE_MAX_EXPONENT"):
            os.environ.pop(k, None)

    def test_window_doubles_per_failed_economic_probe(self):
        b = ClaudeCircuitBreaker(recovery_window_s=900.0)
        self.assertEqual(b._effective_recovery_window_s(), 900.0)   # exp 0
        # Simulate a failed economic HALF_OPEN probe → exponent grows.
        b._state = CircuitState.HALF_OPEN
        b.record_economic_exhaustion("402")
        self.assertEqual(b._economic_reprobe_exponent, 1)
        self.assertEqual(b._effective_recovery_window_s(), 1800.0)  # 900*2^1
        b._state = CircuitState.HALF_OPEN
        b.record_economic_exhaustion("402")
        self.assertEqual(b._effective_recovery_window_s(), 3600.0)  # 900*2^2

    def test_exponent_capped(self):
        os.environ["JARVIS_CLAUDE_ECONOMIC_REPROBE_MAX_EXPONENT"] = "2"
        b = ClaudeCircuitBreaker(recovery_window_s=100.0)
        for _ in range(6):
            b._state = CircuitState.HALF_OPEN
            b.record_economic_exhaustion("402")
        self.assertLessEqual(b._economic_reprobe_exponent, 2)
        self.assertEqual(b._effective_recovery_window_s(), 400.0)   # 100*2^2 (capped)

    def test_success_resets_backoff(self):
        b = ClaudeCircuitBreaker(recovery_window_s=900.0)
        b._state = CircuitState.HALF_OPEN
        b.record_economic_exhaustion("402")
        b._state = CircuitState.HALF_OPEN
        b.record_economic_exhaustion("402")
        self.assertGreater(b._economic_reprobe_exponent, 0)
        b.record_success()  # funding returned → probe succeeds
        self.assertEqual(b._economic_reprobe_exponent, 0)
        self.assertEqual(b._effective_recovery_window_s(), 900.0)

    def test_transport_trip_unaffected_by_economic_exponent(self):
        # A pure transport breaker (exponent 0) keeps the fixed window.
        b = ClaudeCircuitBreaker(recovery_window_s=900.0)
        b._state = CircuitState.HALF_OPEN
        b.record_transport_exhaustion("ReadTimeout")
        self.assertEqual(b._economic_reprobe_exponent, 0)
        self.assertEqual(b._effective_recovery_window_s(), 900.0)

    def test_reset_clears_exponent(self):
        b = ClaudeCircuitBreaker(recovery_window_s=900.0)
        b._state = CircuitState.HALF_OPEN
        b.record_economic_exhaustion("402")
        b.reset()
        self.assertEqual(b._economic_reprobe_exponent, 0)

    def test_snapshot_exposes_adaptive_fields(self):
        b = ClaudeCircuitBreaker(recovery_window_s=900.0)
        snap = b.snapshot()
        self.assertIn("economic_reprobe_exponent", snap)
        self.assertIn("effective_recovery_window_s", snap)


if __name__ == "__main__":
    unittest.main()
