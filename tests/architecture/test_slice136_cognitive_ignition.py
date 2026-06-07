"""Slice 136 — The Cognitive Graduation Matrix (final synapse + episodic ignition).

The economic-router synapse is wired at the candidate_generator call site (outside
the pure decide()) — its mechanism is exercised by the Slice-135 route synapse
tests (router="economic"). This suite proves the headline: the graduation harness
can run a LIVE episodic integration assertion (write an episode → wait for the
async nowait → assert it renders into the volatile prompt tail) and, on pass,
AUTONOMOUSLY flip + persist JARVIS_EPISODIC_CORE_ENABLED — within the recursion
bound (episodic is an explicitly-vetted non-SAFETY substrate).
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile
import unittest

from backend.core.ouroboros.governance import graduation_orchestrator as GO
from backend.core.ouroboros.governance import episodic_core as EC


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestEpisodicGraduation(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_GRADUATION_ORCHESTRATOR_ENABLED"] = "1"
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
        EC.reset_episodic_ledger()

    def tearDown(self):
        for k in ("JARVIS_GRADUATION_ORCHESTRATOR_ENABLED",
                  "JARVIS_EPISODIC_CORE_ENABLED"):
            os.environ.pop(k, None)
        EC.reset_episodic_ledger()

    def test_episodic_is_vetted_not_safety(self):
        self.assertIn("JARVIS_EPISODIC_CORE_ENABLED", GO._COST_CANDIDATES)
        self.assertFalse(GO.is_safety_flag("JARVIS_EPISODIC_CORE_ENABLED"))

    def test_live_episodic_assertion_passes(self):
        res = _run(GO._default_assertion("JARVIS_EPISODIC_CORE_ENABLED"))
        self.assertTrue(res.passed, res.detail)
        self.assertIn("render=True", res.detail)

    def test_autonomous_ignition_flips_and_persists(self):
        with tempfile.TemporaryDirectory() as d:
            env = pathlib.Path(d) / ".env"
            env.write_text("ANTHROPIC_API_KEY=sk-ant-SECRET\nUNRELATED=keep\n")
            os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
            out = _run(GO.graduate(
                "JARVIS_EPISODIC_CORE_ENABLED", persist=True, env_path=env))
            self.assertEqual(out.action, GO.GraduationAction.GRADUATED)
            self.assertTrue(out.flipped)
            self.assertEqual(os.environ.get("JARVIS_EPISODIC_CORE_ENABLED"), "1")
            text = env.read_text()
            self.assertIn("JARVIS_EPISODIC_CORE_ENABLED=1", text)   # ignited in .env
            self.assertIn("ANTHROPIC_API_KEY=sk-ant-SECRET", text)  # credential untouched
            self.assertIn("UNRELATED=keep", text)

    def test_operator_disable_still_honored(self):
        # Even with a passing assertion, an explicit =0 is never overridden.
        os.environ["JARVIS_EPISODIC_CORE_ENABLED"] = "0"
        out = _run(GO.graduate("JARVIS_EPISODIC_CORE_ENABLED"))
        self.assertEqual(out.action, GO.GraduationAction.HELD_OPERATOR_PRECEDENCE)


class TestEconomicSynapseMechanism(unittest.TestCase):
    """The economic synapse uses the same route-synapse path (router=economic);
    here we confirm the coalesced economic episode shape the call-site emits."""

    def setUp(self):
        os.environ["JARVIS_EPISODIC_CORE_ENABLED"] = "1"
        EC.reset_episodic_ledger()

    def tearDown(self):
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
        EC.reset_episodic_ledger()

    def test_economic_route_episode_shape(self):
        _run(EC.record_route(
            op_id="opE", router="economic",
            summary="economic cascade_cheap → claude-haiku-4-5",
            context={"action": "cascade_cheap", "tier": "claude-haiku-4-5"}))
        eps = [e for e in EC.get_episodic_ledger().recent(10) if e.kind == "route"]
        self.assertTrue(eps)
        self.assertEqual(eps[-1].context.get("router"), "economic")
        self.assertEqual(eps[-1].context.get("action"), "cascade_cheap")


if __name__ == "__main__":
    unittest.main()
