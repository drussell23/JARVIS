"""Slice 175 — per-model rupture forecasting (closes Blindspot B).

The global rupture ring (172) penalized a STABLE model for a VOLATILE model's failures. This
decentralizes the cortex: a separate rupture ring AND a separate self-calibrating threshold
per DW model. Flooding deepseek-v4-pro with ruptures must NOT raise qwen3.5-397b's risk —
qwen keeps streaming on the real-time lane.
"""
from __future__ import annotations

import time
import unittest

from backend.core.ouroboros.governance.dw_failure_predictor import (
    DWFailurePredictor,
    get_threshold_calibrator,
)
from backend.core.ouroboros.governance import doubleword_provider as DW


_KW = dict(horizon_s=300, lookback_s=600, halflife_s=120)


class TestPerModelIsolation(unittest.TestCase):
    def test_flooding_one_model_does_not_penalize_another(self):
        p = DWFailurePredictor()
        for _ in range(5):
            p.record_rupture(now=1000.0, model_id="deepseek-v4-pro")
        deepseek = p.rupture_probability(now=1000.0, model_id="deepseek-v4-pro", **_KW)
        qwen = p.rupture_probability(now=1000.0, model_id="qwen3.5-397b", **_KW)
        self.assertGreater(deepseek, 0.7)   # volatile model is hot
        self.assertEqual(qwen, 0.0)         # stable model is PRISTINE — no cross-contamination

    def test_model_id_normalized_case_insensitive(self):
        p = DWFailurePredictor()
        p.record_rupture(now=1000.0, model_id="DeepSeek-AI/DeepSeek-V4-Pro")
        risk = p.rupture_probability(now=1000.0, model_id="deepseek-ai/deepseek-v4-pro", **_KW)
        self.assertGreater(risk, 0.0)       # same bucket regardless of case

    def test_unknown_model_bucket_isolated_from_named(self):
        p = DWFailurePredictor()
        p.record_rupture(now=1000.0)        # no model → "" bucket
        self.assertGreater(p.rupture_probability(now=1000.0, **_KW), 0.0)
        self.assertEqual(p.rupture_probability(now=1000.0, model_id="qwen", **_KW), 0.0)

    def test_ring_is_per_model(self):
        p = DWFailurePredictor()
        p.record_rupture(now=1.0, model_id="a")
        p.record_rupture(now=2.0, model_id="b")
        p.record_rupture(now=3.0, model_id="b")
        self.assertEqual(len(p._rings["a"]), 1)
        self.assertEqual(len(p._rings["b"]), 2)


class TestPerModelCalibrators(unittest.TestCase):
    def test_distinct_calibrator_per_model(self):
        c1 = get_threshold_calibrator("deepseek-v4-pro")
        c2 = get_threshold_calibrator("qwen3.5-397b")
        self.assertIsNot(c1, c2)
        self.assertIs(get_threshold_calibrator("deepseek-v4-pro"), c1)  # stable per model
        self.assertIs(get_threshold_calibrator("DEEPSEEK-V4-PRO"), c1)  # normalized


class TestRouterModelAware(unittest.TestCase):
    def tearDown(self):
        import os
        os.environ.pop("JARVIS_DW_CALIBRATION_ENABLED", None)

    def test_dw_rupture_risk_high_is_model_specific(self):
        import os
        os.environ.pop("JARVIS_DW_CALIBRATION_ENABLED", None)  # static threshold path
        pred = DW.get_dw_failure_predictor() if hasattr(DW, "get_dw_failure_predictor") else None
        from backend.core.ouroboros.governance.dw_failure_predictor import get_dw_failure_predictor
        pred = get_dw_failure_predictor()
        now = time.monotonic()
        for _ in range(6):
            pred.record_rupture(now=now, model_id="slice175-volatile")
        self.assertTrue(DW._dw_rupture_risk_high(model_id="slice175-volatile"))
        self.assertFalse(DW._dw_rupture_risk_high(model_id="slice175-stable"))

    def test_router_threads_model_id(self):
        import backend.core.ouroboros.governance.doubleword_provider as M
        src = open(M.__file__).read()
        # the DW dispatch site passes the resolved model into the router
        self.assertIn("_slice36_should_force_batch(context, model_id=_effective_model)", src)


if __name__ == "__main__":
    unittest.main()
