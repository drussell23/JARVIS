"""Slice 132 — The Sovereign Shadow Graduation Harness.

Replaces the manual "graduation runbook" with an executable async harness that
runs LIVE bounded integration assertions for a gated cost flag and, on pass,
autonomously flips it FALSE→TRUE — WITHOUT a human toggle.

THE LOAD-BEARING BOUND (recursion safety): the harness composes the existing
Tiered Authority (``graduation_override_ledger`` refuses any non-STANDARD tier).
It auto-flips ONLY non-SAFETY (cost/routing/tuning) substrates; a SAFETY-class
flag is REFUSED (advisory → operator), never auto-granted. An autonomous organism
that could flip its own kill-switches is no longer bounded — so it cannot.

Assertions are injectable → the harness is tested without a live Anthropic key
(the live run is operator-invoked with the funded lane). The ``.env`` persister
is bounded: it updates ONLY the target flag line and NEVER touches credentials.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile
import unittest

from backend.core.ouroboros.governance import graduation_orchestrator as GO


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


async def _pass(flag):
    return GO.AssertionResult(flag=flag, passed=True, detail="ok")


async def _fail(flag):
    return GO.AssertionResult(flag=flag, passed=False, detail="200 not returned")


class TestGate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("JARVIS_GRADUATION_ORCHESTRATOR_ENABLED", None)

    def test_default_false(self):
        self.assertFalse(GO.graduation_orchestrator_enabled())

    def test_disabled_refuses(self):
        out = _run(GO.graduate("JARVIS_SEMANTIC_CACHE_ENABLED", assertion=_pass))
        self.assertEqual(out.action, GO.GraduationAction.REFUSED_DISABLED)
        self.assertFalse(out.flipped)


class TestSafetyBound(unittest.TestCase):
    """The recursion bound: SAFETY-class flags can NEVER auto-graduate."""

    def setUp(self):
        os.environ["JARVIS_GRADUATION_ORCHESTRATOR_ENABLED"] = "1"

    def tearDown(self):
        os.environ.pop("JARVIS_GRADUATION_ORCHESTRATOR_ENABLED", None)
        os.environ.pop("JARVIS_FAKE_SAFETY_FLAG", None)

    def test_safety_flag_refused_even_when_assertion_passes(self):
        # is_safety classifier injected True → refuse regardless of assertion.
        out = _run(GO.graduate(
            "JARVIS_FAKE_SAFETY_FLAG", assertion=_pass,
            is_safety=lambda f: True,
        ))
        self.assertEqual(out.action, GO.GraduationAction.REFUSED_SAFETY)
        self.assertFalse(out.flipped)
        self.assertIsNone(os.environ.get("JARVIS_FAKE_SAFETY_FLAG"))  # never set


class TestGraduateFlip(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_GRADUATION_ORCHESTRATOR_ENABLED"] = "1"
        for f in ("JARVIS_T_COST_FLAG",):
            os.environ.pop(f, None)

    def tearDown(self):
        os.environ.pop("JARVIS_GRADUATION_ORCHESTRATOR_ENABLED", None)
        os.environ.pop("JARVIS_T_COST_FLAG", None)

    def test_assertion_fail_holds(self):
        out = _run(GO.graduate("JARVIS_T_COST_FLAG", assertion=_fail,
                               is_safety=lambda f: False))
        self.assertEqual(out.action, GO.GraduationAction.HELD_ASSERTION_FAILED)
        self.assertFalse(out.flipped)
        self.assertIsNone(os.environ.get("JARVIS_T_COST_FLAG"))

    def test_assertion_pass_flips_env(self):
        out = _run(GO.graduate("JARVIS_T_COST_FLAG", assertion=_pass,
                               is_safety=lambda f: False))
        self.assertEqual(out.action, GO.GraduationAction.GRADUATED)
        self.assertTrue(out.flipped)
        self.assertEqual(os.environ.get("JARVIS_T_COST_FLAG"), "1")  # autonomous flip

    def test_operator_explicit_disable_is_honored(self):
        # Operator env-precedence: an explicit =0 is NOT overridden.
        os.environ["JARVIS_T_COST_FLAG"] = "0"
        out = _run(GO.graduate("JARVIS_T_COST_FLAG", assertion=_pass,
                               is_safety=lambda f: False))
        self.assertEqual(out.action, GO.GraduationAction.HELD_OPERATOR_PRECEDENCE)
        self.assertEqual(os.environ.get("JARVIS_T_COST_FLAG"), "0")  # untouched


class TestBoundedEnvPersist(unittest.TestCase):
    def test_persist_updates_only_flag_line_never_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            env = pathlib.Path(d) / ".env"
            env.write_text(
                "ANTHROPIC_API_KEY=sk-ant-SECRET-do-not-touch\n"
                "DOUBLEWORD_API_KEY=dw-SECRET\n"
                "JARVIS_T_COST_FLAG=0\n"
                "SOME_OTHER=value\n"
            )
            ok = GO.persist_flag_to_env("JARVIS_T_COST_FLAG", "1", env_path=env)
            self.assertTrue(ok)
            text = env.read_text()
            self.assertIn("JARVIS_T_COST_FLAG=1", text)
            # Credentials + unrelated lines preserved byte-for-byte.
            self.assertIn("ANTHROPIC_API_KEY=sk-ant-SECRET-do-not-touch", text)
            self.assertIn("DOUBLEWORD_API_KEY=dw-SECRET", text)
            self.assertIn("SOME_OTHER=value", text)
            self.assertNotIn("JARVIS_T_COST_FLAG=0", text)

    def test_persist_appends_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            env = pathlib.Path(d) / ".env"
            env.write_text("ANTHROPIC_API_KEY=sk-ant-SECRET\n")
            GO.persist_flag_to_env("JARVIS_NEW_FLAG", "1", env_path=env)
            text = env.read_text()
            self.assertIn("JARVIS_NEW_FLAG=1", text)
            self.assertIn("ANTHROPIC_API_KEY=sk-ant-SECRET", text)

    def test_persist_refuses_credential_shaped_key(self):
        # Defense-in-depth: the persister NEVER writes a credential-shaped key.
        with tempfile.TemporaryDirectory() as d:
            env = pathlib.Path(d) / ".env"
            env.write_text("X=1\n")
            self.assertFalse(GO.persist_flag_to_env("ANTHROPIC_API_KEY", "x", env_path=env))
            self.assertFalse(GO.persist_flag_to_env("DOUBLEWORD_API_KEY", "x", env_path=env))


class TestGraduateAll(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_GRADUATION_ORCHESTRATOR_ENABLED"] = "1"

    def tearDown(self):
        os.environ.pop("JARVIS_GRADUATION_ORCHESTRATOR_ENABLED", None)
        for f in ("JARVIS_GA_A", "JARVIS_GA_B"):
            os.environ.pop(f, None)

    def test_graduate_all_returns_outcome_per_flag(self):
        outs = _run(GO.graduate_all(
            ["JARVIS_GA_A", "JARVIS_GA_B"],
            assertion_for=lambda f: (_pass if f == "JARVIS_GA_A" else _fail),
            is_safety=lambda f: False,
        ))
        by = {o.flag: o for o in outs}
        self.assertEqual(by["JARVIS_GA_A"].action, GO.GraduationAction.GRADUATED)
        self.assertEqual(by["JARVIS_GA_B"].action, GO.GraduationAction.HELD_ASSERTION_FAILED)


if __name__ == "__main__":
    unittest.main()
