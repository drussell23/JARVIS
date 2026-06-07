"""Slice 135 — Mid-Cycle Cognitive Synapse Matrix (routing decisions).

Wires routing decisions (CAI tier cascade, economic failover, brain selection)
into the episodic ledger as ``kind=route`` episodes — with a structural
COALESCING spam-guard so a flurry of mid-cycle micro-routing decisions cannot
flush the high-signal terminal episodes out of the short-term window.
"""
from __future__ import annotations

import asyncio
import os
import unittest

from backend.core.ouroboros.governance import episodic_core as EC


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestCoalesceGuard(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_EPISODIC_CORE_ENABLED"] = "1"
        EC.reset_episodic_ledger()

    def tearDown(self):
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
        EC.reset_episodic_ledger()

    def _led(self, window=8):
        return EC.EpisodicLedger(window=window, embedder=_NoEmb())

    def test_same_key_coalesces_into_one_episode(self):
        led = self._led()
        for i in range(5):
            _run(led.record(kind="route", op_id="X", summary=f"tier decision {i}",
                            context={"tier": "doubleword", "i": i}, coalesce_key="route:X"))
        eps = led.recent(10)
        route_eps = [e for e in eps if e.kind == "route"]
        self.assertEqual(len(route_eps), 1)                       # 5 → 1
        self.assertEqual(route_eps[0].summary, "tier decision 4") # latest wins
        self.assertEqual(route_eps[0].context.get("coalesced_count"), 5)

    def test_coalesce_preserves_high_signal_terminals(self):
        # window=3. Without coalescing, 5 route appends between two terminals
        # would flush terminal A. With the guard, A survives.
        led = self._led(window=3)
        _run(led.record(kind="transition", op_id="A", summary="A COMPLETE"))
        for i in range(5):
            _run(led.record(kind="route", op_id="X", summary=f"r{i}",
                            coalesce_key="route:X"))
        _run(led.record(kind="transition", op_id="B", summary="B COMPLETE"))
        summaries = [e.summary for e in led.recent(10)]
        self.assertIn("A COMPLETE", summaries)   # high-signal terminal preserved
        self.assertIn("B COMPLETE", summaries)
        self.assertEqual(len([e for e in led.recent(10) if e.kind == "route"]), 1)

    def test_different_keys_stay_separate(self):
        led = self._led()
        _run(led.record(kind="route", op_id="X", summary="rx", coalesce_key="route:X"))
        _run(led.record(kind="route", op_id="Y", summary="ry", coalesce_key="route:Y"))
        self.assertEqual(len([e for e in led.recent(10) if e.kind == "route"]), 2)

    def test_no_key_appends_normally(self):
        led = self._led()
        _run(led.record(kind="route", op_id="X", summary="a"))
        _run(led.record(kind="route", op_id="X", summary="b"))
        self.assertEqual(len(led.recent(10)), 2)  # no coalesce_key → 2 episodes


class _NoEmb:
    def embed(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


class TestRouteSynapse(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_EPISODIC_CORE_ENABLED"] = "1"
        EC.reset_episodic_ledger()

    def tearDown(self):
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
        EC.reset_episodic_ledger()

    def test_record_route_gated(self):
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
        self.assertIsNone(_run(EC.record_route(
            op_id="o", router="cai", summary="x")))

    def test_record_route_coalesces_by_op(self):
        for i in range(3):
            _run(EC.record_route(op_id="o9", router="cai",
                                 summary=f"cascade {i}", context={"tier": "claude_heavy"}))
        eps = EC.get_episodic_ledger().recent(10)
        route_eps = [e for e in eps if e.kind == "route"]
        self.assertEqual(len(route_eps), 1)
        self.assertEqual(route_eps[0].context.get("router"), "cai")
        self.assertEqual(route_eps[0].context.get("coalesced_count"), 3)

    def test_note_route_nowait_nonblocking(self):
        async def go():
            EC.note_route_nowait(op_id="oN", router="economic",
                                 summary="failover→haiku", context={"tier": "claude_low_cost"})
            self.assertEqual(EC.get_episodic_ledger().recent(10), [])  # not yet run
            await asyncio.sleep(0.05)
            eps = EC.get_episodic_ledger().recent(10)
            self.assertTrue(eps)
            self.assertEqual(eps[-1].kind, "route")
            self.assertEqual(eps[-1].context.get("router"), "economic")
        _run(go())


class TestRouteDecisionWiring(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_EPISODIC_CORE_ENABLED"] = "1"
        os.environ["JARVIS_CAI_ROUTER_ENABLED"] = "1"
        EC.reset_episodic_ledger()

    def tearDown(self):
        for k in ("JARVIS_EPISODIC_CORE_ENABLED", "JARVIS_CAI_ROUTER_ENABLED"):
            os.environ.pop(k, None)
        EC.reset_episodic_ledger()

    def test_cai_tier_advisory_records_route_episode(self):
        from backend.core.ouroboros.governance.route_decision_service import (
            RouteDecisionService,
        )
        from backend.core.ouroboros.governance.brain_selector import BrainSelector
        svc = RouteDecisionService(BrainSelector())
        _run(svc.cai_tier_advisory("refactor the module", "heavy_code"))
        eps = EC.get_episodic_ledger().recent(10)
        route_eps = [e for e in eps if e.kind == "route"]
        self.assertTrue(route_eps)
        self.assertEqual(route_eps[-1].context.get("router"), "cai")
        self.assertIn("tier", route_eps[-1].context)


if __name__ == "__main__":
    unittest.main()
