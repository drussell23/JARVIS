"""Slice 176 — multi-signal failure predictor (closes Blindspot D).

The cortex (172-175) forecast from TRANSPORT ruptures ONLY — blind to ~80% of DW's failure
spectrum. This fuses the full taxonomy: each failure event carries a KIND with a distinct
predictive WEIGHT, so a quota lockdown (economic, high weight) drives risk far faster than a
localized empty completion (upstream, low weight). The weighted Poisson rate feeds the same
self-calibrating Brier engine. Backward-compatible: a transport-only ring (weight 1.0) is
byte-identical to Slice 172.
"""
from __future__ import annotations

import math
import unittest

from backend.core.ouroboros.governance.dw_failure_predictor import (
    DWFailurePredictor,
    _signal_weight,
)

_KW = dict(horizon_s=300, lookback_s=600, halflife_s=120)


class TestSignalWeights(unittest.TestCase):
    def test_economic_outweighs_upstream(self):
        # a quota lockdown is far more predictive than a single empty completion
        self.assertGreater(_signal_weight("economic"), _signal_weight("upstream"))

    def test_transport_is_baseline(self):
        self.assertEqual(_signal_weight("transport"), 1.0)

    def test_unknown_kind_defaults_to_baseline(self):
        self.assertEqual(_signal_weight("nonsense-kind"), 1.0)

    def test_weight_env_tunable(self):
        import os
        os.environ["JARVIS_DW_SIGNAL_WEIGHT_ECONOMIC"] = "5.0"
        try:
            self.assertEqual(_signal_weight("economic"), 5.0)
        finally:
            os.environ.pop("JARVIS_DW_SIGNAL_WEIGHT_ECONOMIC", None)


class TestWeightedFusion(unittest.TestCase):
    def test_economic_event_riskier_than_transport(self):
        a = DWFailurePredictor()
        a.record_failure(now=1000.0, model_id="m", kind="economic")
        b = DWFailurePredictor()
        b.record_failure(now=1000.0, model_id="m", kind="transport")
        self.assertGreater(
            a.rupture_probability(now=1000.0, model_id="m", **_KW),
            b.rupture_probability(now=1000.0, model_id="m", **_KW),
        )

    def test_non_transport_cluster_drives_above_threshold(self):
        # Phase 4: sequential batch cancellations (non-transport) DO drive risk up
        p = DWFailurePredictor()
        for _ in range(6):
            p.record_failure(now=1000.0, model_id="m", kind="cancel")
        self.assertGreater(p.rupture_probability(now=1000.0, model_id="m", **_KW), 0.7)

    def test_two_economic_events_drive_above_threshold(self):
        p = DWFailurePredictor()
        for _ in range(2):
            p.record_failure(now=1000.0, model_id="m", kind="economic")
        self.assertGreater(p.rupture_probability(now=1000.0, model_id="m", **_KW), 0.7)

    def test_transport_only_is_backward_compatible(self):
        # transport weight 1.0 → identical to the Slice-172 unweighted Poisson
        p = DWFailurePredictor()
        for _ in range(3):
            p.record_rupture(now=1000.0, model_id="m")
        risk = p.rupture_probability(now=1000.0, model_id="m", **_KW)
        self.assertAlmostEqual(risk, 1.0 - math.exp(-1.5), places=3)

    def test_record_rupture_tags_transport(self):
        p = DWFailurePredictor()
        p.record_rupture(now=1.0, model_id="m")
        self.assertEqual(p._rings["m"][0][1], "transport")


class TestPerModelStillIsolated(unittest.TestCase):
    def test_economic_storm_on_one_model_does_not_touch_another(self):
        p = DWFailurePredictor()
        for _ in range(5):
            p.record_failure(now=1000.0, model_id="deepseek-v4-pro", kind="economic")
        self.assertGreater(p.rupture_probability(now=1000.0, model_id="deepseek-v4-pro", **_KW), 0.7)
        self.assertEqual(p.rupture_probability(now=1000.0, model_id="qwen3.5-397b", **_KW), 0.0)


class TestDominantSignal(unittest.TestCase):
    def test_reports_dominant_failure_vector(self):
        p = DWFailurePredictor()
        p.record_failure(now=1000.0, model_id="m", kind="transport")
        for _ in range(3):
            p.record_failure(now=1000.0, model_id="m", kind="economic")
        self.assertEqual(p.dominant_signal(now=1000.0, model_id="m"), "economic")

    def test_no_failures_no_dominant(self):
        p = DWFailurePredictor()
        self.assertEqual(p.dominant_signal(now=1000.0, model_id="m"), "")


class TestRecordingWiring(unittest.TestCase):
    def test_failure_source_maps_to_predictor_kind(self):
        from backend.core.ouroboros.governance.candidate_generator import (
            _record_dw_failure_signal,
        )
        from backend.core.ouroboros.governance.topology_sentinel import FailureSource
        from backend.core.ouroboros.governance.dw_failure_predictor import (
            get_dw_failure_predictor,
        )
        pred = get_dw_failure_predictor()
        _record_dw_failure_signal("slice176-econ", FailureSource.LIVE_HTTP_429)
        ring = pred._rings.get("slice176-econ")
        self.assertTrue(ring)
        self.assertEqual(ring[-1][1], "economic")
        _record_dw_failure_signal("slice176-up", FailureSource.LIVE_HTTP_5XX)
        self.assertEqual(pred._rings["slice176-up"][-1][1], "upstream")


if __name__ == "__main__":
    unittest.main()
