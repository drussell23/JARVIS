"""Slice 174 — cognitive self-calibration loop (closes Blindspot C).

Slice 172's rupture-risk threshold was a fixed 0.7 (env-seeded but open-loop). This makes
it self-tuning: the cortex evaluates its OWN past predictions against the actual rupture
record and adjusts the threshold —
  * False Positive (predicted high, stream stayed stable through the window) → RAISE
  * False Negative (predicted low, a rupture occurred in the window)         → LOWER
tracking a Brier score as the quality metric. The calibrated threshold PERSISTS to the
.jarvis volume so a container restart doesn't cause amnesia (cf. the Slice 164 breaker).
Gated default-FALSE (§33.1 — new adaptive behavior); when off the static env baseline is
byte-identical to Slice 172.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from backend.core.ouroboros.governance.dw_failure_predictor import (
    ThresholdCalibrator,
    get_threshold_calibrator,
    rupture_risk_threshold,
    calibration_enabled,
)

_H = 300.0  # horizon used throughout


class TestSingleEvaluation(unittest.TestCase):
    def test_false_positive_raises_threshold(self):
        c = ThresholdCalibrator(initial=0.70, step=0.05, persist_path=None)
        c.record_prediction(now=0.0, prob=0.90)          # 0.90 ≥ 0.70 → predicted HIGH
        n = c.evaluate(now=400.0, rupture_times=[], horizon=_H)  # window [0,300] elapsed, no rupture
        self.assertEqual(n, 1)
        self.assertAlmostEqual(c.threshold(), 0.75)      # FP → raised

    def test_false_negative_lowers_threshold(self):
        c = ThresholdCalibrator(initial=0.70, step=0.05, persist_path=None)
        c.record_prediction(now=0.0, prob=0.40)          # 0.40 < 0.70 → predicted LOW
        c.evaluate(now=400.0, rupture_times=[150.0], horizon=_H)  # rupture in (0,300]
        self.assertAlmostEqual(c.threshold(), 0.65)      # FN → lowered

    def test_true_positive_no_change(self):
        c = ThresholdCalibrator(initial=0.70, step=0.05, persist_path=None)
        c.record_prediction(now=0.0, prob=0.90)          # HIGH
        c.evaluate(now=400.0, rupture_times=[150.0], horizon=_H)  # rupture happened → correct
        self.assertAlmostEqual(c.threshold(), 0.70)

    def test_true_negative_no_change(self):
        c = ThresholdCalibrator(initial=0.70, step=0.05, persist_path=None)
        c.record_prediction(now=0.0, prob=0.40)          # LOW
        c.evaluate(now=400.0, rupture_times=[], horizon=_H)       # stable → correct
        self.assertAlmostEqual(c.threshold(), 0.70)

    def test_not_yet_due_is_not_evaluated(self):
        c = ThresholdCalibrator(initial=0.70, step=0.05, persist_path=None)
        c.record_prediction(now=0.0, prob=0.90)
        n = c.evaluate(now=100.0, rupture_times=[], horizon=_H)   # window not elapsed
        self.assertEqual(n, 0)
        self.assertAlmostEqual(c.threshold(), 0.70)


class TestSyntheticDataset(unittest.TestCase):
    def test_threshold_rises_under_repeated_false_positives(self):
        c = ThresholdCalibrator(initial=0.50, step=0.05, lo=0.3, hi=0.95, persist_path=None)
        t0 = c.threshold()
        for i in range(5):
            c.record_prediction(now=i * 1000.0, prob=0.99)        # always HIGH
            c.evaluate(now=i * 1000.0 + 400.0, rupture_times=[], horizon=_H)  # never ruptures
        self.assertGreater(c.threshold(), t0)

    def test_threshold_falls_under_repeated_false_negatives(self):
        c = ThresholdCalibrator(initial=0.70, step=0.05, lo=0.3, hi=0.95, persist_path=None)
        t0 = c.threshold()
        for i in range(5):
            c.record_prediction(now=i * 1000.0, prob=0.01)        # always LOW
            c.evaluate(now=i * 1000.0 + 400.0, rupture_times=[i * 1000.0 + 150.0], horizon=_H)  # always ruptures
        self.assertLess(c.threshold(), t0)

    def test_threshold_bounded_by_floor_and_ceiling(self):
        c = ThresholdCalibrator(initial=0.90, step=0.10, lo=0.3, hi=0.95, persist_path=None)
        for i in range(20):  # relentless FPs
            c.record_prediction(now=i * 1000.0, prob=0.99)
            c.evaluate(now=i * 1000.0 + 400.0, rupture_times=[], horizon=_H)
        self.assertLessEqual(c.threshold(), 0.95)
        self.assertGreaterEqual(c.threshold(), 0.3)

    def test_brier_score_tracked(self):
        c = ThresholdCalibrator(initial=0.70, step=0.05, persist_path=None)
        c.record_prediction(now=0.0, prob=0.90)
        c.evaluate(now=400.0, rupture_times=[150.0], horizon=_H)  # outcome 1, prob .9 → brier .01
        snap = c.snapshot()
        self.assertIsNotNone(snap["brier"])
        self.assertAlmostEqual(snap["brier"], (0.90 - 1.0) ** 2, places=4)


class TestPersistence(unittest.TestCase):
    def _path(self):
        return os.path.join(tempfile.mkdtemp(), "dw_threshold_calibration.json")

    def test_calibrated_threshold_persists_and_restores(self):
        p = self._path()
        c = ThresholdCalibrator(initial=0.70, step=0.05, persist_path=p)
        c.record_prediction(now=0.0, prob=0.90)
        c.evaluate(now=400.0, rupture_times=[], horizon=_H)       # FP → 0.75 persisted
        self.assertAlmostEqual(c.threshold(), 0.75)
        c2 = ThresholdCalibrator(initial=0.70, step=0.05, persist_path=p)  # fresh instance
        self.assertAlmostEqual(c2.threshold(), 0.75)              # restored, no amnesia

    def test_missing_persist_file_uses_initial(self):
        c = ThresholdCalibrator(initial=0.62, step=0.05, persist_path=self._path())
        self.assertAlmostEqual(c.threshold(), 0.62)


class TestIntegrationGating(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("JARVIS_DW_CALIBRATION_ENABLED", None)
        os.environ.pop("JARVIS_DW_RUPTURE_RISK_THRESHOLD", None)

    def test_disabled_returns_static_baseline(self):
        os.environ.pop("JARVIS_DW_CALIBRATION_ENABLED", None)  # default FALSE
        os.environ["JARVIS_DW_RUPTURE_RISK_THRESHOLD"] = "0.66"
        self.assertFalse(calibration_enabled())
        self.assertAlmostEqual(rupture_risk_threshold(), 0.66)  # static, Slice-172 behavior

    def test_enabled_returns_calibrated(self):
        os.environ["JARVIS_DW_CALIBRATION_ENABLED"] = "1"
        self.assertTrue(calibration_enabled())
        # the live threshold comes from the calibrator singleton (a float in range)
        t = rupture_risk_threshold()
        self.assertGreaterEqual(t, 0.0)
        self.assertLessEqual(t, 1.0)
        self.assertIs(get_threshold_calibrator(), get_threshold_calibrator())


if __name__ == "__main__":
    unittest.main()
