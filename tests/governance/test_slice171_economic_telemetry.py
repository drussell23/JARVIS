"""Slice 171 — economic telemetry for the Slice 170 intra-DW failover.

Slice 170 silently saves capital (a DW rupture fails over to DW-batch instead of cascading
to the expensive Claude). This surfaces it: a thread-safe counter records each intra-DW
failover + estimated capital saved, the DW adapter records on the actual reroute (attributed
by: force_batch True while Claude is AVAILABLE ⟹ the legacy batch path requires Claude
unavailable, so it can only be the Slice 170 reroute), and the Discord spine renders it.

The hot-path hook is a lock-guarded counter increment (no I/O, no GIL contention); the
Discord render reads the snapshot off the hot path.
"""
from __future__ import annotations

import threading
import unittest

from backend.core.ouroboros.governance.economic_telemetry import (
    EconomicTelemetry,
    get_economic_telemetry,
    render_economic_telemetry,
)
from backend.core.ouroboros.governance import doubleword_provider as DW


class _Ctx:
    def __init__(self, route="standard"):
        self.provider_route = route


class TestLedger(unittest.TestCase):
    def test_record_increments_count_and_capital(self):
        t = EconomicTelemetry()
        t.record_intra_failover(saved_usd=0.05)
        snap = t.snapshot()
        self.assertEqual(snap["intra_failovers"], 1)
        self.assertAlmostEqual(snap["capital_saved_usd"], 0.05)

    def test_accumulates(self):
        t = EconomicTelemetry()
        t.record_intra_failover(0.05)
        t.record_intra_failover(0.05)
        self.assertEqual(t.snapshot()["intra_failovers"], 2)
        self.assertAlmostEqual(t.snapshot()["capital_saved_usd"], 0.10)

    def test_default_estimate_when_none(self):
        t = EconomicTelemetry()
        t.record_intra_failover()
        self.assertGreater(t.snapshot()["capital_saved_usd"], 0.0)

    def test_thread_safe_no_lost_increments(self):
        t = EconomicTelemetry()

        def worker():
            for _ in range(100):
                t.record_intra_failover(0.01)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(t.snapshot()["intra_failovers"], 800)

    def test_singleton_is_stable(self):
        self.assertIs(get_economic_telemetry(), get_economic_telemetry())


class TestAttribution(unittest.TestCase):
    """force_batch True + Claude AVAILABLE ⟹ a Slice 170 reroute = capital saved."""

    def setUp(self):
        self._before = get_economic_telemetry().snapshot()["intra_failovers"]

    def tearDown(self):
        import os
        os.environ.pop("JARVIS_PROVIDER_CLAUDE_DISABLED", None)

    def test_records_when_force_batch_and_claude_available(self):
        import os
        os.environ.pop("JARVIS_PROVIDER_CLAUDE_DISABLED", None)
        orig = DW._claude_breaker_open
        DW._claude_breaker_open = lambda *a, **k: False  # type: ignore
        try:
            self.assertTrue(DW._record_intra_failover_telemetry(_Ctx(), force_batch=True))
        finally:
            DW._claude_breaker_open = orig  # type: ignore

    def test_no_record_when_claude_dead(self):
        import os
        os.environ["JARVIS_PROVIDER_CLAUDE_DISABLED"] = "true"
        # Claude dead → batch is the legacy path, NOT a saved-vs-Claude event
        self.assertFalse(DW._record_intra_failover_telemetry(_Ctx(), force_batch=True))

    def test_no_record_when_not_force_batch(self):
        self.assertFalse(DW._record_intra_failover_telemetry(_Ctx(), force_batch=False))


class TestRender(unittest.TestCase):
    def test_render_shows_count_and_capital(self):
        out = render_economic_telemetry({"intra_failovers": 3, "capital_saved_usd": 0.15})
        self.assertIn("3", out)
        self.assertIn("0.15", out)

    def test_render_zero_state_is_safe(self):
        out = render_economic_telemetry({"intra_failovers": 0, "capital_saved_usd": 0.0})
        self.assertTrue(out)


class TestDiscordWiring(unittest.TestCase):
    """The gateway surfaces the metric on every gate (source-pinned — discord.py is not
    installed in this env, so we inspect the source rather than import the module)."""

    def _gateway_src(self):
        import importlib.util
        spec = importlib.util.find_spec(
            "backend.core.ouroboros.governance.discord_gateway"
        )
        with open(spec.origin) as fh:
            return fh.read()

    def test_gateway_defines_economic_line_helper(self):
        src = self._gateway_src()
        self.assertIn("def economic_telemetry_line", src)
        self.assertIn("render_economic_telemetry", src)

    def test_gateway_adds_economic_field_to_the_gate_embed(self):
        src = self._gateway_src()
        # the field is added on the approval embed (the operator's live spine view)
        self.assertIn("economic_telemetry_line()", src)
        i_field = src.find("economic_telemetry_line()")
        i_footer = src.find("set_footer")
        self.assertTrue(0 < i_field < i_footer)


if __name__ == "__main__":
    unittest.main()
