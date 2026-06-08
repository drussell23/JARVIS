"""Slice 169 — dynamic reasoning_effort capability resolver (DW /v1/models).

Slice 168 added a per-model effort floor via a static env map (with deepseek-v4-pro
hardcoded as the one known case). This makes it ADAPTIVE: resolve each model's supported
reasoning_effort values from DW's live /v1/models capability metadata (parsed by the
existing dw_catalog_client) and derive the minimum dynamically — so when DW exposes the
metadata, no static map is needed. Falls back to the Slice-168 static floor when the
metadata isn't present (DW hasn't exposed it yet) → zero behaviour change today,
self-updating tomorrow.
"""
from __future__ import annotations

import types
import unittest

from backend.core.ouroboros.governance.dw_catalog_client import (
    ModelCard,
    parse_supported_reasoning_efforts,
    catalog_min_reasoning_effort,
)
from backend.core.ouroboros.governance import doubleword_provider as DW


def _snap(*raw_dicts):
    cards = tuple(c for c in (ModelCard.from_api_dict(r) for r in raw_dicts) if c)
    return types.SimpleNamespace(models=cards)


class TestParseCapabilities(unittest.TestCase):
    def test_top_level_list(self):
        self.assertEqual(
            parse_supported_reasoning_efforts({"supported_reasoning_efforts": ["low", "medium", "high"]}),
            ("low", "medium", "high"),
        )

    def test_capabilities_subdict(self):
        self.assertEqual(
            parse_supported_reasoning_efforts({"capabilities": {"reasoning_effort": ["none", "low"]}}),
            ("none", "low"),
        )

    def test_absent_returns_empty(self):
        self.assertEqual(parse_supported_reasoning_efforts({"id": "x"}), ())


class TestCatalogMinEffort(unittest.TestCase):
    def test_min_from_supported_efforts(self):
        snap = _snap({"id": "deepseek-v4-pro", "supported_reasoning_efforts": ["low", "medium", "high"]})
        # none not supported → lowest supported is "low"
        self.assertEqual(catalog_min_reasoning_effort("deepseek-v4-pro", snapshot=snap), "low")

    def test_model_supporting_none(self):
        snap = _snap({"id": "qwen3.5-397b", "supported_reasoning_efforts": ["none", "low", "medium"]})
        self.assertEqual(catalog_min_reasoning_effort("qwen3.5-397b", snapshot=snap), "none")

    def test_no_metadata_returns_none(self):
        snap = _snap({"id": "deepseek-v4-pro"})  # no capability metadata
        self.assertIsNone(catalog_min_reasoning_effort("deepseek-v4-pro", snapshot=snap))

    def test_model_absent_returns_none(self):
        snap = _snap({"id": "other-model", "supported_reasoning_efforts": ["low"]})
        self.assertIsNone(catalog_min_reasoning_effort("deepseek-v4-pro", snapshot=snap))


class TestProviderUsesDynamicFirst(unittest.TestCase):
    def tearDown(self):
        DW._catalog_min_reasoning_effort_override = None  # type: ignore[attr-defined]

    def test_dynamic_wins_over_static(self):
        # inject a dynamic resolver that says deepseek supports down to medium
        DW._catalog_min_reasoning_effort_override = lambda mid: "medium" if "deepseek" in mid else None  # type: ignore[attr-defined]
        self.assertEqual(DW._dw_model_min_effort("deepseek-v4-pro"), "medium")

    def test_static_fallback_when_dynamic_none(self):
        DW._catalog_min_reasoning_effort_override = lambda mid: None  # type: ignore[attr-defined]
        self.assertEqual(DW._dw_model_min_effort("deepseek-v4-pro"), "low")   # Slice 168 static
        self.assertEqual(DW._dw_model_min_effort("qwen3.5-397b"), "none")


if __name__ == "__main__":
    unittest.main()
