"""Slice 192 — the proactive hierarchy (the hedge supersedes reactive force-batch).

The clean-container soak proved it: the cold-start seal (184) → sentinel batch-enforce (182)
forced ops to batch-only BEFORE the hedge could race, so RT-rupture-SWALLOWED never fired. The
fix is a precedence hierarchy:
  1. confirmed STORM  → batch-only (cost-optimizer; RT doomed)
  2. hedge + no storm → RACE (overrides cold-start timer + warm-boot ledger flags)
  3. hedge disabled   → legacy 182/184 reactive force-batch
"""
from __future__ import annotations

import time
import unittest

from backend.core.ouroboros.governance import doubleword_provider as DW


class _Ctx:
    def __init__(self, route="standard"):
        self.provider_route = route
        self.op_id = "op-test"


class TestProactiveHierarchy(unittest.TestCase):
    def setUp(self):
        import backend.core.ouroboros.governance.dw_failure_predictor as P
        # save originals so the module is never permanently polluted (no cross-test leak)
        self._orig_start = DW._PROCESS_START
        self._orig_batch = DW._dw_batch_lane_healthy
        self._orig_rupture = P.DWFailurePredictor.rupture_probability
        self._P = P
        DW._PROCESS_START = time.monotonic()  # in cold-start window

    def tearDown(self):
        import os
        DW._PROCESS_START = self._orig_start
        DW._dw_batch_lane_healthy = self._orig_batch
        self._P.DWFailurePredictor.rupture_probability = self._orig_rupture
        for k in ("JARVIS_DW_TRANSPORT_HEDGE_ENABLED", "JARVIS_DW_COLDSTART_WINDOW_S",
                  "JARVIS_DW_STORM_SKIP_THRESHOLD"):
            os.environ.pop(k, None)

    def _arm(self, monkey_storm_p, *, hedge):
        import os
        os.environ["JARVIS_DW_COLDSTART_WINDOW_S"] = "90"
        if hedge:
            os.environ["JARVIS_DW_TRANSPORT_HEDGE_ENABLED"] = "1"
        else:
            os.environ.pop("JARVIS_DW_TRANSPORT_HEDGE_ENABLED", None)
        # batch lane healthy; predictor returns our storm probability (restored in tearDown)
        DW._dw_batch_lane_healthy = lambda: True
        self._P.DWFailurePredictor.rupture_probability = lambda *a, **k: monkey_storm_p

    def test_hedge_overrides_cold_start_when_no_storm(self):
        # COLD-START active + hedge ON + NO storm → must NOT force batch (let the hedge RACE)
        self._arm(monkey_storm_p=0.1, hedge=True)
        self.assertFalse(
            DW._slice36_should_force_batch(_Ctx("standard"), model_id="deepseek-v4-pro"),
            "hedge must override the cold-start seal and race (return False)",
        )

    def test_confirmed_storm_overrides_hedge(self):
        # STORM forecast above threshold → batch-only even with hedge ON (save the doomed spend)
        self._arm(monkey_storm_p=0.95, hedge=True)
        self.assertTrue(
            DW._slice36_should_force_batch(_Ctx("standard"), model_id="deepseek-v4-pro"),
            "a confirmed storm must override the hedge and force batch",
        )

    def test_hedge_disabled_falls_back_to_cold_start_seal(self):
        # hedge OFF → the legacy 184 cold-start seal still forces batch
        self._arm(monkey_storm_p=0.1, hedge=False)
        self.assertTrue(
            DW._slice36_should_force_batch(_Ctx("standard"), model_id="deepseek-v4-pro"),
            "with the hedge disabled, the legacy cold-start seal forces batch",
        )

    def test_helper_confirmed_storm(self):
        self._arm(monkey_storm_p=0.95, hedge=True)
        self.assertTrue(DW._dw_confirmed_storm("deepseek-v4-pro"))
        self._arm(monkey_storm_p=0.1, hedge=True)
        self.assertFalse(DW._dw_confirmed_storm("deepseek-v4-pro"))

    def test_helper_hedge_supersedes(self):
        self._arm(monkey_storm_p=0.1, hedge=True)
        self.assertTrue(DW._dw_hedge_supersedes(_Ctx("standard"), "deepseek-v4-pro"))
        # storm → no supersede
        self._arm(monkey_storm_p=0.95, hedge=True)
        self.assertFalse(DW._dw_hedge_supersedes(_Ctx("standard"), "deepseek-v4-pro"))
        # wrong route → no supersede
        self._arm(monkey_storm_p=0.1, hedge=True)
        self.assertFalse(DW._dw_hedge_supersedes(_Ctx("immediate"), "deepseek-v4-pro"))


class TestDynamicThinkingInjection(unittest.TestCase):
    """Phase 2 — Seb's enable_thinking:False injected via extra_body, catalog-gated (no
    hardcoded model names)."""

    def setUp(self):
        self._orig_supports = DW._dw_supports_reasoning_control

    def tearDown(self):
        import os
        DW._dw_supports_reasoning_control = self._orig_supports
        os.environ.pop("JARVIS_DW_DISABLE_THINKING_ENABLED", None)

    def test_extra_body_injected_for_catalog_confirmed_model(self):
        DW._dw_supports_reasoning_control = lambda m="": True   # catalog says: supports control
        params = DW._reasoning_request_params(effort="none", model="any-reasoning-model")
        self.assertEqual(
            params.get("extra_body"),
            {"chat_template_kwargs": {"enable_thinking": False}},
        )
        self.assertNotIn("chat_template_kwargs", params)  # nested in extra_body, NOT top-level

    def test_legacy_toplevel_fallback_when_catalog_unknown(self):
        DW._dw_supports_reasoning_control = lambda m="": False  # catalog can't confirm
        params = DW._reasoning_request_params(effort="none", model="unknown-model")
        self.assertNotIn("extra_body", params)
        self.assertEqual(params.get("chat_template_kwargs"), {"enable_thinking": False})

    def test_no_injection_when_effort_above_none(self):
        DW._dw_supports_reasoning_control = lambda m="": True
        params = DW._reasoning_request_params(effort="medium", model="any")
        self.assertNotIn("extra_body", params)       # thinking wanted → no suppression
        self.assertNotIn("chat_template_kwargs", params)

    def test_kill_switch(self):
        import os
        os.environ["JARVIS_DW_DISABLE_THINKING_ENABLED"] = "0"
        DW._dw_supports_reasoning_control = lambda m="": True
        params = DW._reasoning_request_params(effort="none", model="any")
        self.assertNotIn("extra_body", params)        # master off → legacy path only
        self.assertIn("chat_template_kwargs", params)

    def test_no_hardcoded_model_names_in_helpers(self):
        import inspect
        src = inspect.getsource(DW._dw_thinking_extra_body) + inspect.getsource(
            DW._dw_supports_reasoning_control)
        for name in ("nemotron", "deepseek", "qwen", "kimi", "glm"):
            self.assertNotIn(name, src.lower())       # capability is catalog-driven, not name-driven


class TestSentinelDefersToHedge(unittest.TestCase):
    def test_sentinel_enforce_yields_to_hedge(self):
        import importlib.util
        spec = importlib.util.find_spec("backend.core.ouroboros.governance.candidate_generator")
        with open(spec.origin) as fh:
            src = fh.read()
        # the sentinel batch-enforce must defer to the hedge (not force batch when hedge supersedes)
        self.assertIn("_dw_hedge_supersedes", src)


if __name__ == "__main__":
    unittest.main()
