"""Slice 168 — per-model reasoning_effort floor (DoubleWord capability).

Root cause (Seb @ Doubleword, 2026-06-08, "Cancelled DeepSeek-v4-pro batch"):
_reasoning_request_params derives reasoning_effort='none' for trivial/simple ops and
sends it to whatever model the route picks — but deepseek-v4-pro REJECTS 'none', so DW
cancels the batch. Some DW models simply don't support 'none' as a reasoning_effort.

Fix: a per-model effort FLOOR. After deriving + clamping DOWN to the streaming
ceiling, clamp UP to the model's minimum supported effort, so we never send an effort a
model rejects. Env-driven map (JARVIS_DW_MODEL_MIN_EFFORT, substring:effort) — generic
matching logic, no hardcoded model in the algorithm; default carries the one known case.
"""
from __future__ import annotations

import os
import unittest

from backend.core.ouroboros.governance import doubleword_provider as DW


class TestModelEffortFloor(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("JARVIS_DW_MODEL_MIN_EFFORT", None)
        os.environ.pop("JARVIS_DW_REASONING_EFFORT", None)

    def test_deepseek_v4_pro_floors_none_to_low(self):
        self.assertEqual(DW._dw_model_min_effort("deepseek-ai/DeepSeek-V4-Pro"), "low")

    def test_unknown_model_supports_none(self):
        self.assertEqual(DW._dw_model_min_effort("Qwen/Qwen3.5-397B"), "none")

    def test_empty_model_supports_none(self):
        self.assertEqual(DW._dw_model_min_effort(""), "none")

    def test_env_override_map(self):
        os.environ["JARVIS_DW_MODEL_MIN_EFFORT"] = "kimi:medium"
        self.assertEqual(DW._dw_model_min_effort("moonshotai/Kimi-K2.6"), "medium")
        self.assertEqual(DW._dw_model_min_effort("deepseek-v4-pro"), "none")  # overridden away

    def test_clamp_up_to_min(self):
        self.assertEqual(DW._clamp_up_to_min("none", "low"), "low")
        self.assertEqual(DW._clamp_up_to_min("medium", "low"), "medium")  # never lowers
        self.assertEqual(DW._clamp_up_to_min("none", "none"), "none")


class TestEffortResolutionWithFloor(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("JARVIS_DW_REASONING_EFFORT", None)

    def test_trivial_on_none_rejecting_model_becomes_low(self):
        # the live bug: trivial → 'none' → DeepSeek rejects → cancel. Now → 'low'.
        self.assertEqual(DW._reasoning_effort_for("trivial", model="deepseek-v4-pro"), "low")

    def test_trivial_on_normal_model_stays_none(self):
        self.assertEqual(DW._reasoning_effort_for("trivial", model="qwen3.5-397b"), "none")

    def test_request_params_apply_floor(self):
        p = DW._reasoning_request_params(complexity="trivial", model="deepseek-v4-pro")
        self.assertEqual(p["reasoning_effort"], "low")
        self.assertNotIn("chat_template_kwargs", p)  # only added when effort == 'none'

    def test_explicit_effort_still_floored_for_model(self):
        # even an explicit 'none' must be floored for a model that rejects it
        p = DW._reasoning_request_params("none", model="deepseek-v4-pro")
        self.assertEqual(p["reasoning_effort"], "low")


if __name__ == "__main__":
    unittest.main()
