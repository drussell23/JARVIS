"""Slice 127 Phase 2 — economic-exhaustion trip on the Claude lane breaker.

The EXISTING ``ClaudeCircuitBreaker`` is a per-Anthropic-lane self-healing
breaker (CLOSED/OPEN/HALF_OPEN with a recovery window) — but it only trips on
TRANSPORT exhaustion. A Claude "credit balance too low" 400 leaves it CLOSED,
so every subsequent op keeps cascading into the broke lane (the
bt-2026-06-07-040933 pattern).

Slice 127 composes this breaker: ``record_economic_exhaustion`` trips it OPEN
on an economic block (default threshold 1 — an economic refusal is immediately
actionable) so ``should_allow_request()`` routes future ops AROUND Claude, then
the existing recovery-window → HALF_OPEN → ``record_success`` path self-heals
it. Gated ``JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED`` (default-FALSE) → OFF is a
no-op (byte-identical).
"""
from __future__ import annotations

import os
import unittest

from backend.core.ouroboros.governance.claude_circuit_breaker import (
    ClaudeCircuitBreaker,
    CircuitState,
    claude_economic_breaker_enabled,
)


class TestClaudeEconomicBreaker(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED")
        os.environ["JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED"] = "true"

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED", None)
        else:
            os.environ["JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED"] = self._prev

    def test_economic_exhaustion_trips_open_and_blocks(self) -> None:
        b = ClaudeCircuitBreaker(recovery_window_s=900.0)
        self.assertEqual(b.state, CircuitState.CLOSED)
        b.record_economic_exhaustion("credit balance too low")
        self.assertEqual(b.state, CircuitState.OPEN)
        # Future ops route AROUND Claude while OPEN within the window.
        self.assertFalse(b.should_allow_request())

    def test_self_heals_after_recovery_window(self) -> None:
        # recovery_window_s=0 → the very next should_allow_request probes.
        b = ClaudeCircuitBreaker(recovery_window_s=0.0)
        b.record_economic_exhaustion("insufficient funds")
        self.assertEqual(b.state, CircuitState.OPEN)
        # Window elapsed → one probe allowed (HALF_OPEN).
        self.assertTrue(b.should_allow_request())
        self.assertEqual(b.state, CircuitState.HALF_OPEN)
        # Probe succeeds (lane funded again) → CLOSED.
        b.record_success()
        self.assertEqual(b.state, CircuitState.CLOSED)

    def test_disabled_is_noop(self) -> None:
        os.environ["JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED"] = "false"
        b = ClaudeCircuitBreaker()
        b.record_economic_exhaustion("credit balance too low")
        self.assertEqual(b.state, CircuitState.CLOSED)
        self.assertTrue(b.should_allow_request())

    def test_threshold_env_respected(self) -> None:
        os.environ["JARVIS_CLAUDE_ECONOMIC_BREAKER_THRESHOLD"] = "2"
        try:
            b = ClaudeCircuitBreaker()
            b.record_economic_exhaustion("402")
            self.assertEqual(b.state, CircuitState.CLOSED)  # 1 < 2
            b.record_economic_exhaustion("402")
            self.assertEqual(b.state, CircuitState.OPEN)    # 2 >= 2
        finally:
            os.environ.pop("JARVIS_CLAUDE_ECONOMIC_BREAKER_THRESHOLD", None)

    def test_success_resets_economic_counter(self) -> None:
        os.environ["JARVIS_CLAUDE_ECONOMIC_BREAKER_THRESHOLD"] = "2"
        try:
            b = ClaudeCircuitBreaker()
            b.record_economic_exhaustion("402")
            b.record_success()  # lane recovered
            b.record_economic_exhaustion("402")
            # Counter reset by success → still 1 < 2, stays CLOSED.
            self.assertEqual(b.state, CircuitState.CLOSED)
        finally:
            os.environ.pop("JARVIS_CLAUDE_ECONOMIC_BREAKER_THRESHOLD", None)

    def test_master_default_true_graduated(self) -> None:
        # Slice 146: graduated default-TRUE — economic trips quarantine the Claude
        # lane by default so future ops route to DW (live-proven).
        os.environ.pop("JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED", None)
        self.assertTrue(claude_economic_breaker_enabled())


class TestEconomicBreakerWiringPin(unittest.TestCase):
    """Bytes-pin on the candidate_generator integration site (the fallback
    cascade is too large to unit-test directly — same convention as
    ``test_dispatcher_consults_breaker_before_primary``). The fallback
    exception handler must record an economic exhaustion on the Claude lane
    breaker, gated on the economic detector."""

    def test_fallback_site_records_economic_exhaustion(self) -> None:
        import pathlib
        src = pathlib.Path(
            "backend/core/ouroboros/governance/candidate_generator.py"
        ).read_text()
        self.assertIn("record_economic_exhaustion", src)
        self.assertIn("is_hard_economic_block", src)
        # The trip must be downstream of the economic gate, not unconditional.
        gate_idx = src.find("_s127_econ_reclassify_enabled")
        trip_idx = src.find("record_economic_exhaustion")
        self.assertGreater(gate_idx, -1)
        self.assertGreater(trip_idx, gate_idx)


if __name__ == "__main__":
    unittest.main()
