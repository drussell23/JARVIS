"""Slice 187 — precision TTFT telemetry. Separate pure network latency from local async lag;
exclude loop-starved samples so the routing math never trains on a broken stopwatch."""
from __future__ import annotations

import importlib.util
import os
import unittest

from backend.core.ouroboros.governance.dw_precision_telemetry import (
    network_ttft_ms,
    ttft_sample_is_clean,
    measure_loop_lag_ms,
    now_perf,
)


class TestPureNetworkTTFT(unittest.TestCase):
    def test_pure_ttft_is_request_to_first_byte(self):
        # 66.8s vendor latency, measured via perf_counter deltas
        self.assertAlmostEqual(network_ttft_ms(100.0, 166.8), 66800.0, places=1)

    def test_clamped_non_negative(self):
        self.assertEqual(network_ttft_ms(200.0, 100.0), 0.0)  # never negative

    def test_perf_counter_monotonic(self):
        a = now_perf()
        b = now_perf()
        self.assertGreaterEqual(b, a)


class TestCleanSampleGate(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("JARVIS_DW_TTFT_MAX_LOOP_LAG_MS", None)

    def test_frictionless_loop_sample_is_clean(self):
        self.assertTrue(ttft_sample_is_clean(5.0, max_loop_lag_ms=200.0))

    def test_starved_loop_sample_excluded(self):
        # the 1190ms ControlPlaneStarvation reading must be REJECTED
        self.assertFalse(ttft_sample_is_clean(1190.0, max_loop_lag_ms=200.0))

    def test_threshold_from_env(self):
        os.environ["JARVIS_DW_TTFT_MAX_LOOP_LAG_MS"] = "50"
        self.assertFalse(ttft_sample_is_clean(80.0))
        self.assertTrue(ttft_sample_is_clean(30.0))


class TestLoopLagProbe(unittest.IsolatedAsyncioTestCase):
    async def test_idle_loop_lag_is_small(self):
        lag = await measure_loop_lag_ms()
        self.assertGreaterEqual(lag, 0.0)
        self.assertLess(lag, 200.0)  # an idle test loop shouldn't be starved


class TestWiring(unittest.TestCase):
    def test_rt_path_uses_precision_ttft_and_clean_gate(self):
        spec = importlib.util.find_spec(
            "backend.core.ouroboros.governance.doubleword_provider")
        with open(spec.origin) as fh:
            src = fh.read()
        self.assertIn("network_ttft_ms", src)
        self.assertIn("ttft_sample_is_clean", src)
        self.assertIn("perf_counter", src)  # pure timing, not just monotonic


if __name__ == "__main__":
    unittest.main()
