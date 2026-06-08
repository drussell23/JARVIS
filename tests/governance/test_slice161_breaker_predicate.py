"""Slice 161 — read-only, non-flickering Claude-breaker predicate (universal floor).

Slice 159's force-batch used should_allow_request() to detect a dead Claude. But that
method (a) has a SIDE EFFECT — it transitions OPEN->HALF_OPEN and consumes the single
probe slot — and (b) returns True during the probe window. So in the live soak a
complex op's force-batch DISENGAGED exactly when Claude was unreliable → DW SSE
ruptured → the op died at generate and never reached the gate/approval floor.

Fix: read the breaker STATE directly (read-only). Claude is unreliable-as-fallback
whenever the breaker is NOT CLOSED (OPEN or HALF_OPEN) → force DW batch. No mutation,
no flicker. This lets complex ops survive generate on DW and reach the governance floor.
"""
from __future__ import annotations

import unittest

from backend.core.ouroboros.governance import doubleword_provider as DW
from backend.core.ouroboros.governance.claude_circuit_breaker import CircuitState


class _StateBreaker:
    """Read-only fake exposing .state; should_allow_request must NOT be called."""
    def __init__(self, state):
        self._state = state
        self.allow_calls = 0

    @property
    def state(self):
        return self._state

    def should_allow_request(self):  # pragma: no cover — must not be used by the fix
        self.allow_calls += 1
        return self._state is CircuitState.CLOSED


class TestBreakerPredicate(unittest.TestCase):
    def test_open_is_unreliable_forces_batch(self):
        self.assertTrue(DW._claude_breaker_open(getter=lambda: _StateBreaker(CircuitState.OPEN)))

    def test_half_open_is_unreliable_forces_batch(self):
        # The flicker fix: HALF_OPEN (probe in flight) must still force batch — Claude
        # is NOT healthy yet, so DW must carry the op.
        self.assertTrue(DW._claude_breaker_open(getter=lambda: _StateBreaker(CircuitState.HALF_OPEN)))

    def test_closed_is_healthy_no_force(self):
        self.assertFalse(DW._claude_breaker_open(getter=lambda: _StateBreaker(CircuitState.CLOSED)))

    def test_predicate_has_no_side_effect(self):
        # Must read .state, NOT call the state-mutating should_allow_request().
        b = _StateBreaker(CircuitState.OPEN)
        DW._claude_breaker_open(getter=lambda: b)
        self.assertEqual(b.allow_calls, 0)

    def test_fail_closed_on_error(self):
        def _boom():
            raise RuntimeError("no breaker")
        self.assertFalse(DW._claude_breaker_open(getter=_boom))


if __name__ == "__main__":
    unittest.main()
