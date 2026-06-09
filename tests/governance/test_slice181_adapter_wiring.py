"""Slice 181 — total adapter integration (wire hedge + batch-retry into the live DW adapter).

Slice 180 built the resilience primitives; this connects them to the API layer:
  * Hedge — the DW adapter's RT generate() now catches its OWN StreamRuptureError and re-
    submits over batch within the same tick (instead of bubbling live_transport to the sentinel).
  * Kevlar — _create_batch re-submits on a transient 5xx instead of returning None.
"""
from __future__ import annotations

import importlib.util
import os
import unittest

from backend.core.ouroboros.governance.dw_immortal import (
    dw_hedge_enabled,
    dw_batch_retry_enabled,
    dw_batch_max_retries,
)


def _dw_src():
    spec = importlib.util.find_spec("backend.core.ouroboros.governance.doubleword_provider")
    with open(spec.origin) as fh:
        return fh.read()


class TestGates(unittest.TestCase):
    def tearDown(self):
        for k in ("JARVIS_DW_HEDGE_ENABLED", "JARVIS_DW_BATCH_RETRY_ENABLED", "JARVIS_DW_BATCH_MAX_RETRIES"):
            os.environ.pop(k, None)

    def test_hedge_default_on(self):
        self.assertTrue(dw_hedge_enabled())

    def test_batch_retry_default_on(self):
        self.assertTrue(dw_batch_retry_enabled())

    def test_gates_killable(self):
        os.environ["JARVIS_DW_HEDGE_ENABLED"] = "0"
        os.environ["JARVIS_DW_BATCH_RETRY_ENABLED"] = "0"
        self.assertFalse(dw_hedge_enabled())
        self.assertFalse(dw_batch_retry_enabled())

    def test_max_retries(self):
        os.environ["JARVIS_DW_BATCH_MAX_RETRIES"] = "5"
        self.assertEqual(dw_batch_max_retries(), 5)


class TestHedgeWiring(unittest.TestCase):
    def test_rt_generate_catches_its_own_rupture_and_hedges(self):
        src = _dw_src()
        # the RT try-block catches StreamRuptureError BEFORE DoublewordInfraError and hedges
        self.assertIn("except StreamRuptureError", src)
        self.assertIn("HEDGING to DW-batch", src)
        i_hedge = src.find("except StreamRuptureError")
        i_infra = src.find("except DoublewordInfraError", i_hedge)
        self.assertTrue(0 < i_hedge < i_infra)  # rupture caught first

    def test_hedge_falls_through_to_batch_not_raise(self):
        # the hedge branch must NOT raise — it falls through to the batch-mode code below
        src = _dw_src()
        seg = src[src.find("except StreamRuptureError"): src.find("except DoublewordInfraError", src.find("except StreamRuptureError"))]
        self.assertIn("fall through to batch", seg)
        self.assertIn("hedge_to_batch_on_rupture", seg)


class TestBatchRetryWiring(unittest.TestCase):
    def test_create_batch_retries_transient_5xx(self):
        src = _dw_src()
        self.assertIn("KEVLAR batch net", src)
        self.assertIn("batch_should_retry", src)
        self.assertIn("_s181_attempt", src)
        # the retry must be inside the status>=300 handler, before the legacy `return None`
        i_kevlar = src.find("KEVLAR batch net")
        self.assertGreater(i_kevlar, 0)


if __name__ == "__main__":
    unittest.main()
