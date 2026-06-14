"""Slice 189 — proactive transport-hedge wired into the live dispatch.

The hedge (Slice 188 ``hedged_race``) now lives in DoublewordProvider.generate(): when active +
no storm forecast, it races RT vs batch concurrently and takes the winner — reusing the existing
``_generate_realtime`` (fast) and a new ``_generate_via_batch`` (stable) that composes the
existing ``submit_batch``/``poll_and_retrieve`` primitives (no duplication).
"""
from __future__ import annotations

import importlib.util
import unittest

from backend.core.ouroboros.governance.doubleword_provider import (
    DoublewordProvider,
    DoublewordInfraError,
)


def _dw_src():
    spec = importlib.util.find_spec("backend.core.ouroboros.governance.doubleword_provider")
    with open(spec.origin) as fh:
        return fh.read()


class TestHedgeWiring(unittest.TestCase):
    def test_generate_races_via_hedged_race(self):
        src = _dw_src()
        self.assertIn("hedged_race", src)
        self.assertIn("PROACTIVE hedge", src)
        # the hedge must race RT (_generate_realtime) against batch (_generate_via_batch)
        i_hedge = src.find("return await hedged_race(")
        seg = src[i_hedge:i_hedge + 600]
        self.assertIn("_generate_realtime", seg)
        self.assertIn("_generate_via_batch", seg)

    def test_storm_cost_optimizer_forces_batch_before_rt(self):
        src = _dw_src()
        self.assertIn("_s189_storm_forces_batch", src)
        # the storm gate must run BEFORE the RT branch and set force_batch
        i_storm = src.find("self._s189_storm_forces_batch(context)")
        i_rt = src.find("if self._realtime_enabled and not _slice36_force_batch:")
        self.assertTrue(0 < i_storm < i_rt)

    def test_no_duplication_single_batch_orchestration(self):
        src = _dw_src()
        # submit_batch + poll_and_retrieve orchestration lives in exactly ONE place
        self.assertEqual(src.count("await self.submit_batch("), 1)
        self.assertEqual(src.count("await self.poll_and_retrieve("), 1)
        self.assertIn("async def _generate_via_batch", src)


class _MockProvider:
    """Minimal stand-in exercising the real _generate_via_batch against fake primitives."""
    _last_error_status = 0
    _realtime_enabled = True

    def __init__(self, pending="batch-123", result="BATCH_RESULT"):
        self._pending = pending
        self._result = result

    async def submit_batch(self, context, prompt_override=None):
        return self._pending

    async def poll_and_retrieve(self, pending, context):
        return self._result


# bind the REAL methods onto the mock to prove they compose the existing primitives
_MockProvider._generate_via_batch = DoublewordProvider._generate_via_batch
_MockProvider._s189_transport_hedge_active = DoublewordProvider._s189_transport_hedge_active


class TestGenerateViaBatch(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_submit_and_poll_returns_result(self):
        p = _MockProvider()
        result = await p._generate_via_batch(object(), None)
        self.assertEqual(result, "BATCH_RESULT")

    async def test_raises_on_submit_failure(self):
        p = _MockProvider(pending=None)
        with self.assertRaises(DoublewordInfraError):
            await p._generate_via_batch(object(), None)

    async def test_raises_on_retrieve_failure(self):
        p = _MockProvider(result=None)
        with self.assertRaises(DoublewordInfraError):
            await p._generate_via_batch(object(), None)

    def test_hedge_inactive_when_flag_off(self):
        # Slice 241 T2 — the hedge is GRADUATED to default-ON, so verifying the
        # flag-off path now requires explicitly setting the kill switch (=0)
        # rather than relying on the default.
        import os
        _prev = os.environ.get("JARVIS_DW_TRANSPORT_HEDGE_ENABLED")
        os.environ["JARVIS_DW_TRANSPORT_HEDGE_ENABLED"] = "0"
        try:
            self.assertFalse(_MockProvider()._s189_transport_hedge_active())
        finally:
            if _prev is None:
                os.environ.pop("JARVIS_DW_TRANSPORT_HEDGE_ENABLED", None)
            else:
                os.environ["JARVIS_DW_TRANSPORT_HEDGE_ENABLED"] = _prev


if __name__ == "__main__":
    unittest.main()
