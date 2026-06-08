"""Slice 159 — DW transport sovereignty (force batch when Claude is economically dead).

Root cause (verified live): Slice 36 force-batch only engaged when Claude was
explicitly DISABLED (JARVIS_PROVIDER_CLAUDE_DISABLED). But when Claude is *configured*
yet *credit-dead*, _claude_disabled=False → RT streaming → DW SSE ruptures
(live_transport) → cascades to a dead Claude → terminal_quota exhaustion → the op
dies before GATE. The premise "Claude catches any RT failure" is false when Claude's
circuit breaker is OPEN.

Fix: Claude is "unavailable as fallback" = disabled OR economic/transport breaker OPEN.
In that state STANDARD/COMPLEX ops force the DW batch transport → the organism stays
sovereign on DoubleWord. Composes the existing Slice 146 economic breaker; no hardcode.
"""
from __future__ import annotations

import os
import unittest

from backend.core.ouroboros.governance import doubleword_provider as DW


class _Ctx:
    def __init__(self, route="complex", op_id="op-1"):
        self.provider_route = route
        self.op_id = op_id


class _FakeBreaker:
    def __init__(self, allow):
        self._allow = allow
    def should_allow_request(self):
        return self._allow


class TestBreakerOpenPredicate(unittest.TestCase):
    def test_open_when_breaker_rejects(self):
        self.assertTrue(DW._claude_breaker_open(getter=lambda: _FakeBreaker(allow=False)))

    def test_closed_when_breaker_allows(self):
        self.assertFalse(DW._claude_breaker_open(getter=lambda: _FakeBreaker(allow=True)))

    def test_fail_closed_on_error(self):
        def _boom():
            raise RuntimeError("no breaker")
        self.assertFalse(DW._claude_breaker_open(getter=_boom))  # fail → legacy RT


class TestForceBatchWhenClaudeDead(unittest.TestCase):
    def setUp(self):
        self._saved = DW._claude_breaker_open
        os.environ["JARVIS_PROVIDER_CLAUDE_DISABLED"] = "false"   # Claude NOT disabled
        os.environ["JARVIS_DW_FORCE_BATCH_STANDARD_COMPLEX"] = "1"
        os.environ.pop("JARVIS_DW_FORCE_BATCH_ON_CLAUDE_BREAKER", None)

    def tearDown(self):
        DW._claude_breaker_open = self._saved
        for k in ("JARVIS_PROVIDER_CLAUDE_DISABLED", "JARVIS_DW_FORCE_BATCH_STANDARD_COMPLEX",
                  "JARVIS_DW_FORCE_BATCH_ON_CLAUDE_BREAKER"):
            os.environ.pop(k, None)

    def test_forces_batch_when_breaker_open_even_though_claude_configured(self):
        DW._claude_breaker_open = lambda getter=None: True   # Claude credit-dead
        self.assertTrue(DW._slice36_should_force_batch(_Ctx(route="complex")))

    def test_legacy_rt_when_breaker_closed_and_claude_available(self):
        DW._claude_breaker_open = lambda getter=None: False   # Claude healthy
        self.assertFalse(DW._slice36_should_force_batch(_Ctx(route="complex")))

    def test_gate_off_preserves_legacy_rt_even_if_breaker_open(self):
        os.environ["JARVIS_DW_FORCE_BATCH_ON_CLAUDE_BREAKER"] = "0"
        DW._claude_breaker_open = lambda getter=None: True
        self.assertFalse(DW._slice36_should_force_batch(_Ctx(route="complex")))

    def test_route_gate_still_respected(self):
        DW._claude_breaker_open = lambda getter=None: True
        self.assertFalse(DW._slice36_should_force_batch(_Ctx(route="immediate")))  # not std/complex


if __name__ == "__main__":
    unittest.main()
