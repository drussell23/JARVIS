"""Slice 172 — DW failure-risk predictor (the predictive cortex).

Slice 170 is REACTIVE: it fails over to DW-batch once the stream is CONFIRMED degraded.
This forecasts P(rupture in next N minutes) from the clustering of recent rupture events
and preemptively routes standard/complex ops to batch BEFORE the stream breaks — so a
rupture never throws, never panics, never wakes Claude.

Pure-Python (no torch/tf): a recency-weighted Poisson interval estimator. Recent rupture
events (the same stream the surface-health ledger ingests) decay with a half-life; the
weighted rate λ gives P = 1 - exp(-λ·horizon). New cognitive behavior (acts on a forecast,
not a confirmed failure) → default-FALSE per §33.1.
"""
from __future__ import annotations

import math
import threading
import unittest

from backend.core.ouroboros.governance.dw_failure_predictor import (
    DWFailurePredictor,
    get_dw_failure_predictor,
    render_rupture_risk,
)
from backend.core.ouroboros.governance import doubleword_provider as DW


class _Ctx:
    def __init__(self, route="standard"):
        self.provider_route = route


class TestPoissonModel(unittest.TestCase):
    def test_no_ruptures_is_zero_risk(self):
        p = DWFailurePredictor()
        self.assertEqual(p.rupture_probability(now=1000.0), 0.0)

    def test_cluster_of_recent_ruptures_is_high_risk(self):
        p = DWFailurePredictor()
        for ts in (1000.0, 1000.0, 1000.0):  # 3 ruptures at "now"
            p.record_rupture(now=ts)
        # λ = 3/600, horizon 300 → P = 1 - e^-1.5 ≈ 0.777
        risk = p.rupture_probability(now=1000.0, horizon_s=300, lookback_s=600, halflife_s=120)
        self.assertAlmostEqual(risk, 1.0 - math.exp(-1.5), places=3)
        self.assertGreater(risk, 0.7)

    def test_single_blip_is_below_threshold(self):
        p = DWFailurePredictor()
        p.record_rupture(now=1000.0)
        risk = p.rupture_probability(now=1000.0, horizon_s=300, lookback_s=600, halflife_s=120)
        self.assertAlmostEqual(risk, 1.0 - math.exp(-0.5), places=3)  # ≈0.393
        self.assertLess(risk, 0.7)

    def test_old_ruptures_decay_to_low_risk(self):
        p = DWFailurePredictor()
        for _ in range(5):
            p.record_rupture(now=400.0)  # 600s before "now=1000", + decayed
        risk = p.rupture_probability(now=1000.0, horizon_s=300, lookback_s=600, halflife_s=120)
        self.assertLess(risk, 0.2)

    def test_probability_is_bounded_0_1(self):
        p = DWFailurePredictor()
        for _ in range(200):
            p.record_rupture(now=1000.0)
        risk = p.rupture_probability(now=1000.0)
        self.assertGreaterEqual(risk, 0.0)
        self.assertLessEqual(risk, 1.0)

    def test_ring_is_bounded(self):
        p = DWFailurePredictor(max_ring=16)
        for i in range(100):
            p.record_rupture(now=float(i))
        self.assertLessEqual(len(p._ring), 16)

    def test_thread_safe(self):
        p = DWFailurePredictor(max_ring=10000)

        def w():
            for _ in range(500):
                p.record_rupture(now=1000.0)

        ts = [threading.Thread(target=w) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(p._ring), 4000)

    def test_never_raises_on_garbage(self):
        p = DWFailurePredictor()
        p.record_rupture(now=None)  # uses monotonic clock
        self.assertIsInstance(p.rupture_probability(), float)

    def test_singleton(self):
        self.assertIs(get_dw_failure_predictor(), get_dw_failure_predictor())


class TestPreemptiveRouting(unittest.TestCase):
    """Saves + RESTORES every monkeypatched module function (no cross-test pollution)."""

    def setUp(self):
        import os
        self._os = os
        os.environ.pop("JARVIS_PROVIDER_CLAUDE_DISABLED", None)
        self._orig = {
            "_claude_breaker_open": DW._claude_breaker_open,
            "_dw_rupture_risk_high": DW._dw_rupture_risk_high,
            "_slice41_ledger_force_batch": DW._slice41_ledger_force_batch,
            "_dw_batch_lane_healthy": DW._dw_batch_lane_healthy,
        }
        DW._claude_breaker_open = lambda *a, **k: False  # type: ignore
        DW._slice41_ledger_force_batch = lambda: False  # no confirmed degradation
        DW._dw_batch_lane_healthy = lambda: True  # Slice 173 guard: batch healthy by default

    def tearDown(self):
        self._os.environ.pop("JARVIS_DW_PREDICTIVE_ROUTING_ENABLED", None)
        for name, fn in self._orig.items():
            setattr(DW, name, fn)

    def test_high_risk_preempts_to_batch_when_enabled(self):
        self._os.environ["JARVIS_DW_PREDICTIVE_ROUTING_ENABLED"] = "1"
        DW._dw_rupture_risk_high = lambda: True  # type: ignore
        self.assertTrue(DW._slice36_should_force_batch(_Ctx("standard")))

    def test_disabled_by_default_no_preempt(self):
        self._os.environ.pop("JARVIS_DW_PREDICTIVE_ROUTING_ENABLED", None)
        DW._dw_rupture_risk_high = lambda: True  # type: ignore
        self.assertFalse(DW._slice36_should_force_batch(_Ctx("standard")))

    def test_low_risk_no_preempt(self):
        self._os.environ["JARVIS_DW_PREDICTIVE_ROUTING_ENABLED"] = "1"
        DW._dw_rupture_risk_high = lambda: False  # type: ignore
        self.assertFalse(DW._slice36_should_force_batch(_Ctx("standard")))


class TestRender(unittest.TestCase):
    def test_render_shows_percent(self):
        out = render_rupture_risk(0.75)
        self.assertIn("75", out)

    def test_render_zero(self):
        self.assertTrue(render_rupture_risk(0.0))


class TestDiscordWiring(unittest.TestCase):
    """Source-pinned (discord.py not installed in this env)."""

    def _src(self):
        import importlib.util
        spec = importlib.util.find_spec(
            "backend.core.ouroboros.governance.discord_gateway"
        )
        with open(spec.origin) as fh:
            return fh.read()

    def test_gateway_renders_rupture_risk_on_the_spine(self):
        src = self._src()
        self.assertIn("def rupture_risk_line", src)
        self.assertIn("rupture_risk_line()", src)
        self.assertIn("🔮 forecast", src)


if __name__ == "__main__":
    unittest.main()
