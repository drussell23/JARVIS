"""Slice 188 — proactive supremacy: transport-hedge + cortex-as-cost-optimizer + O(1) DSA +
singleflight."""
from __future__ import annotations

import asyncio
import random
import unittest

from backend.core.ouroboros.governance.dw_streaming_stats import (
    P2Quantile,
    DecayingRate,
    HawkesIntensity,
)
from backend.core.ouroboros.governance.dw_singleflight import Singleflight, payload_key
from backend.core.ouroboros.governance.dw_transport_hedge import (
    hedged_race,
    should_skip_race_for_storm,
)


# ─────────────── Phase 3: DSA ───────────────
class TestP2Quantile(unittest.TestCase):
    def test_approximates_true_p95_no_sort(self):
        random.seed(42)
        data = [random.gauss(50, 10) for _ in range(2000)]
        est = P2Quantile(0.95)
        for x in data:
            est.update(x)
        true_p95 = sorted(data)[int(0.95 * len(data))]
        # P2 is an approximation — within ~5% of the true percentile
        self.assertLess(abs(est.value() - true_p95) / true_p95, 0.05)

    def test_cold_returns_something_or_none(self):
        est = P2Quantile(0.95)
        self.assertIsNone(est.value())
        est.update(1.0)
        self.assertIsNotNone(est.value())


class TestDecayingRate(unittest.TestCase):
    def test_rate_rises_then_decays(self):
        r = DecayingRate(halflife_s=10.0)
        for t in range(5):
            r.observe(float(t))
        hot = r.rate(5.0)
        self.assertGreater(hot, 0)
        cold = r.rate(5.0 + 100.0)  # 10 half-lives later → ~0
        self.assertLess(cold, hot * 0.01)

    def test_snapshot_restore_decays_forward(self):
        r = DecayingRate(halflife_s=10.0)
        r.observe(0.0); r.observe(1.0)
        snap = r.snapshot()
        r2 = DecayingRate()
        r2.restore(snap)
        # decay-forward from the persisted state (dissolves cold-start blindness)
        self.assertAlmostEqual(r2.rate(1.0), r.rate(1.0), places=6)


class TestHawkesIntensity(unittest.TestCase):
    def test_burst_spikes_intensity_then_decays(self):
        h = HawkesIntensity(mu=0.001, alpha=1.0, beta=0.1)
        base = h.intensity(0.0)
        for t in range(6):  # a rupture storm
            h.observe(float(t))
        spiked = h.intensity(6.0)
        self.assertGreater(spiked, base + 1.0)  # self-excited well above baseline
        decayed = h.intensity(6.0 + 200.0)
        self.assertLess(decayed, spiked * 0.05)

    def test_storm_probability_high_after_burst(self):
        h = HawkesIntensity(mu=0.0, alpha=1.0, beta=0.05)
        for t in range(5):
            h.observe(float(t))
        self.assertGreater(h.storm_probability(5.0, horizon_s=30.0), 0.8)


# ─────────────── Phase 2: cost-optimizer ───────────────
class TestStormGate(unittest.TestCase):
    def test_skip_race_when_storm_imminent(self):
        self.assertTrue(should_skip_race_for_storm(0.95))   # storm → batch-only, save credits

    def test_race_when_calm(self):
        self.assertFalse(should_skip_race_for_storm(0.2))   # calm → race RT vs batch


# ─────────────── Phase 1: transport-hedge ───────────────
class TestHedgedRace(unittest.IsolatedAsyncioTestCase):
    async def test_fast_path_wins_and_loser_cancelled(self):
        stable_cancelled = {"v": False}

        async def fast():
            await asyncio.sleep(0.01)
            return "RT"

        async def stable():
            try:
                await asyncio.sleep(10.0)
                return "BATCH"
            except asyncio.CancelledError:
                stable_cancelled["v"] = True
                raise

        result = await hedged_race(fast, stable)
        self.assertEqual(result, "RT")
        await asyncio.sleep(0)  # let cancellation propagate
        self.assertTrue(stable_cancelled["v"])

    async def test_rupture_swallowed_batch_wins(self):
        async def fast():
            await asyncio.sleep(0.01)
            raise RuntimeError("live_transport:RuntimeError")  # RT ruptures

        async def stable():
            await asyncio.sleep(0.05)
            return "BATCH"

        result = await hedged_race(fast, stable, is_rupture=lambda e: "live_transport" in str(e))
        self.assertEqual(result, "BATCH")  # the op NEVER saw the rupture

    async def test_both_fail_raises(self):
        async def fast():
            raise RuntimeError("live_transport:x")

        async def stable():
            raise ValueError("batch broke too")

        with self.assertRaises(BaseException):
            await hedged_race(fast, stable, is_rupture=lambda e: "live_transport" in str(e))


# ─────────────── Phase 4: singleflight ───────────────
class TestSingleflight(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_identical_calls_share_one_execution(self):
        sf = Singleflight()
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            await asyncio.sleep(0.02)
            return "RESULT"

        key = payload_key("same-prompt", "deepseek")
        results = await asyncio.gather(*[sf.do(key, factory) for _ in range(5)])
        self.assertEqual(results, ["RESULT"] * 5)
        self.assertEqual(calls["n"], 1)          # ONE underlying DW call, not 5
        self.assertEqual(sf.inflight_count(), 0)  # registry cleared

    async def test_different_keys_run_independently(self):
        sf = Singleflight()
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            return calls["n"]

        await asyncio.gather(sf.do("a", factory), sf.do("b", factory))
        self.assertEqual(calls["n"], 2)

    async def test_leader_failure_propagates_then_clears(self):
        sf = Singleflight()

        async def boom():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            await sf.do("k", boom)
        self.assertEqual(sf.inflight_count(), 0)  # failed future cleared, not leaked


if __name__ == "__main__":
    unittest.main()
