"""Slice 127 P2.1 — fallback-skip gate: IMMEDIATE ops reroute to DW when the
Claude economic lane breaker is OPEN.

The live soak (bt-2026-06-06 23:07) proved P1+P2 (0 `terminal_config` bricks; the
Claude credit-400 reclassifies to recoverable `terminal_quota`; `ECONOMIC TRIP`
isolates the Claude lane + self-heals). But it exposed a routing blind spot:
`_generate_immediate` does "Claude direct, skip DW" — so an IMMEDIATE op keeps
grinding against the depleted Claude lane and exhausts instead of failing over to
the funded DW lane, because the existing `should_allow_request` gate only covers
Claude-as-*primary*.

This gate makes the IMMEDIATE/Claude-direct path consult the Claude lane breaker
first; when it's OPEN (economic/transport), the op reroutes to the DW primary.
Pure decision helper (testable) + gated `JARVIS_FALLBACK_SKIP_GATE_ENABLED`
(default-FALSE → OFF = unchanged Claude-direct).
"""
from __future__ import annotations

import os
import unittest

from backend.core.ouroboros.governance.candidate_generator import (
    fallback_skip_gate_enabled,
    immediate_reroute_to_dw,
)


class TestFallbackSkipGateMaster(unittest.TestCase):
    def test_default_false(self) -> None:
        os.environ.pop("JARVIS_FALLBACK_SKIP_GATE_ENABLED", None)
        self.assertFalse(fallback_skip_gate_enabled())

    def test_enabled_truthy(self) -> None:
        for v in ("1", "true", "yes", "on"):
            os.environ["JARVIS_FALLBACK_SKIP_GATE_ENABLED"] = v
            self.assertTrue(fallback_skip_gate_enabled())
        os.environ.pop("JARVIS_FALLBACK_SKIP_GATE_ENABLED", None)


class TestImmediateRerouteDecision(unittest.TestCase):
    def test_reroutes_when_dw_primary_gate_on_breaker_open(self) -> None:
        # The whole point: DW is primary, gate on, Claude lane breaker enabled
        # and NOT allowing requests (OPEN) → reroute the IMMEDIATE op to DW.
        self.assertTrue(immediate_reroute_to_dw(
            dw_is_primary=True, gate_enabled=True,
            claude_breaker_enabled=True, claude_allows_request=False,
        ))

    def test_no_reroute_when_claude_allows(self) -> None:
        # CLOSED or a HALF_OPEN probe (should_allow_request True) → keep
        # Claude-direct so the lane can self-heal.
        self.assertFalse(immediate_reroute_to_dw(
            dw_is_primary=True, gate_enabled=True,
            claude_breaker_enabled=True, claude_allows_request=True,
        ))

    def test_no_reroute_when_gate_off(self) -> None:
        self.assertFalse(immediate_reroute_to_dw(
            dw_is_primary=True, gate_enabled=False,
            claude_breaker_enabled=True, claude_allows_request=False,
        ))

    def test_no_reroute_when_breaker_disabled(self) -> None:
        self.assertFalse(immediate_reroute_to_dw(
            dw_is_primary=True, gate_enabled=True,
            claude_breaker_enabled=False, claude_allows_request=False,
        ))

    def test_no_reroute_when_claude_is_primary(self) -> None:
        # When Claude IS primary, the existing primary-side gate handles it —
        # this IMMEDIATE/DW-primary reroute does not apply.
        self.assertFalse(immediate_reroute_to_dw(
            dw_is_primary=False, gate_enabled=True,
            claude_breaker_enabled=True, claude_allows_request=False,
        ))


class TestWiringPin(unittest.TestCase):
    """Bytes-pin the _generate_immediate integration (the async dispatch method
    is too coupled to unit-test directly — project convention)."""

    def test_immediate_consults_breaker_before_claude_direct(self) -> None:
        import pathlib
        src = pathlib.Path(
            "backend/core/ouroboros/governance/candidate_generator.py"
        ).read_text()
        # The reroute decision must be wired into _generate_immediate, before
        # the Claude-direct _call_fallback, and route to the DW primary.
        idx_fn = src.find("async def _generate_immediate(")
        idx_next = src.find("async def _try_jprime_primacy(", idx_fn)
        body = src[idx_fn:idx_next]
        self.assertIn("immediate_reroute_to_dw", body)
        self.assertIn("should_allow_request", body)
        self.assertIn("_call_primary", body)


if __name__ == "__main__":
    unittest.main()
