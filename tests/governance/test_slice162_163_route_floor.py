"""Slice 162 + 163 — IMMEDIATE-route proactive-DW + fail-closed governance floor.

162: the IMMEDIATE reroute (immediate_reroute_to_dw) fed claude_allows_request from
should_allow_request(), which flickers True during a HALF_OPEN probe + has a side
effect — so an IMMEDIATE op kept Claude-direct on a dead-but-probing Claude → exhausted
before reaching the gate. Fix: feed it the read-only state-based predicate (reuse
Slice 161's _claude_breaker_open) so HALF_OPEN reroutes to funded DW.

163: the gate's MIN_RISK_TIER floor was wrapped in a try/except that silently swallowed
to DEBUG — so an error in the floor computation silently BYPASSED the operator's
governance floor (op auto-applied). Fix: apply_floor_to_name is fail-closed — if the
recommendation errors, it still applies the configured MIN_RISK_TIER.
"""
from __future__ import annotations

import os
import unittest

from backend.core.ouroboros.governance.candidate_generator import immediate_reroute_to_dw
from backend.core.ouroboros.governance.doubleword_provider import _claude_breaker_open
from backend.core.ouroboros.governance import risk_tier_floor as RTF
from backend.core.ouroboros.governance.claude_circuit_breaker import CircuitState


class _StateBreaker:
    def __init__(self, state):
        self._state = state
    @property
    def state(self):
        return self._state


class TestImmediateProactiveDW(unittest.TestCase):
    """162 — IMMEDIATE reroutes to DW whenever the breaker is non-CLOSED (incl. the
    HALF_OPEN probe window), composing Slice 161's state-based predicate."""

    def _allows(self, state):
        # this is exactly what the patched call site computes
        return not _claude_breaker_open(getter=lambda: _StateBreaker(state))

    def test_half_open_reroutes_to_dw(self):
        reroute = immediate_reroute_to_dw(
            dw_is_primary=True, gate_enabled=True, claude_breaker_enabled=True,
            claude_allows_request=self._allows(CircuitState.HALF_OPEN),
        )
        self.assertTrue(reroute)  # the flicker fix — was False before

    def test_open_reroutes_to_dw(self):
        self.assertTrue(immediate_reroute_to_dw(
            dw_is_primary=True, gate_enabled=True, claude_breaker_enabled=True,
            claude_allows_request=self._allows(CircuitState.OPEN)))

    def test_closed_keeps_claude_direct(self):
        self.assertFalse(immediate_reroute_to_dw(
            dw_is_primary=True, gate_enabled=True, claude_breaker_enabled=True,
            claude_allows_request=self._allows(CircuitState.CLOSED)))

    def test_call_site_uses_state_predicate_not_should_allow(self):
        # Lock the call-site fix: the IMMEDIATE reroute must feed the read-only
        # state-based predicate, NOT the flickering/side-effecting should_allow_request.
        import backend.core.ouroboros.governance.candidate_generator as CG
        src = open(CG.__file__).read()
        self.assertIn("_p21_allows = not _p21_breaker_open(getter=_p21_ccb)", src)
        self.assertNotIn("_p21_allows = _p21_ccb().should_allow_request()", src)


class TestFailClosedFloor(unittest.TestCase):
    """163 — the governance floor is never silently bypassed by a computation error."""

    def tearDown(self):
        os.environ.pop("JARVIS_MIN_RISK_TIER", None)
        # restore if monkeypatched
        if hasattr(self, "_saved_rf"):
            RTF.recommended_floor = self._saved_rf

    def test_floor_applies_when_recommendation_errors(self):
        os.environ["JARVIS_MIN_RISK_TIER"] = "approval_required"
        self._saved_rf = RTF.recommended_floor
        def _boom(*a, **k):
            raise RuntimeError("recommendation backend down")
        RTF.recommended_floor = _boom
        effective, applied = RTF.apply_floor_to_name("safe_auto")
        # fail-closed: still escalates to the configured MIN_RISK_TIER
        self.assertEqual(effective, "approval_required")
        self.assertEqual(applied, "approval_required")

    def test_no_floor_configured_and_error_passes_through(self):
        os.environ.pop("JARVIS_MIN_RISK_TIER", None)
        self._saved_rf = RTF.recommended_floor
        def _boom(*a, **k):
            raise RuntimeError("down")
        RTF.recommended_floor = _boom
        effective, applied = RTF.apply_floor_to_name("safe_auto")
        self.assertEqual(effective, "safe_auto")   # nothing configured → unchanged
        self.assertIsNone(applied)


if __name__ == "__main__":
    unittest.main()
