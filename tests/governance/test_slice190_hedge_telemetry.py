"""Slice 190 — hedge-outcome telemetry: PROVE the proactive win.

The hedge now reports which transport won + whether an RT rupture was made INVISIBLE by batch
winning, recorded into the EXISTING economic ledger (Slice 171). This is the instrument that
proves DW-as-primary shrugs off ruptures in the soak.
"""
from __future__ import annotations

import asyncio
import unittest

from backend.core.ouroboros.governance.dw_transport_hedge import hedged_race
from backend.core.ouroboros.governance.economic_telemetry import EconomicTelemetry


class TestHedgeOutcomeReporting(unittest.IsolatedAsyncioTestCase):
    async def test_reports_rt_win_no_rupture(self):
        seen = {}

        async def fast():
            await asyncio.sleep(0.01)
            return "RT"

        async def stable():
            await asyncio.sleep(10.0)
            return "BATCH"

        def on_outcome(winner, rupture_swallowed):
            seen["winner"] = winner
            seen["rupture"] = rupture_swallowed

        r = await hedged_race(fast, stable, on_outcome=on_outcome)
        self.assertEqual(r, "RT")
        self.assertEqual(seen, {"winner": "rt", "rupture": False})

    async def test_reports_batch_win_with_swallowed_rupture(self):
        seen = {}

        async def fast():
            await asyncio.sleep(0.01)
            raise RuntimeError("live_transport:RuntimeError")

        async def stable():
            await asyncio.sleep(0.04)
            return "BATCH"

        r = await hedged_race(
            fast, stable,
            is_rupture=lambda e: "live_transport" in str(e),
            on_outcome=lambda w, s: seen.update(winner=w, rupture=s),
        )
        self.assertEqual(r, "BATCH")
        self.assertEqual(seen["winner"], "batch")
        self.assertTrue(seen["rupture"])  # the op NEVER saw the rupture, telemetry records it


class TestEconomicLedgerHedge(unittest.TestCase):
    def test_rt_win_counts_no_capital(self):
        t = EconomicTelemetry()
        t.record_hedge_outcome("rt", rupture_swallowed=False)
        snap = t.snapshot()
        self.assertEqual(snap["hedge_rt_wins"], 1)
        self.assertEqual(snap["hedge_batch_wins"], 0)
        self.assertEqual(snap["intra_failovers"], 0)  # RT won cleanly, no capital event

    def test_swallowed_rupture_records_capital_save(self):
        t = EconomicTelemetry()
        t.record_hedge_outcome("batch", rupture_swallowed=True)
        snap = t.snapshot()
        self.assertEqual(snap["hedge_batch_wins"], 1)
        self.assertEqual(snap["hedge_ruptures_swallowed"], 1)
        self.assertEqual(snap["intra_failovers"], 1)          # a swallowed rupture = a save
        self.assertGreater(snap["capital_saved_usd"], 0.0)

    def test_batch_win_no_rupture_no_capital(self):
        t = EconomicTelemetry()
        t.record_hedge_outcome("batch", rupture_swallowed=False)  # batch just faster, no rupture
        snap = t.snapshot()
        self.assertEqual(snap["hedge_batch_wins"], 1)
        self.assertEqual(snap["intra_failovers"], 0)


class TestProviderWiring(unittest.TestCase):
    def test_provider_records_hedge_outcome(self):
        import importlib.util
        spec = importlib.util.find_spec("backend.core.ouroboros.governance.doubleword_provider")
        with open(spec.origin) as fh:
            src = fh.read()
        self.assertIn("record_hedge_outcome", src)
        self.assertIn("on_outcome=_s190_hedge_outcome", src)
        self.assertIn("RT rupture SWALLOWED", src)


if __name__ == "__main__":
    unittest.main()
