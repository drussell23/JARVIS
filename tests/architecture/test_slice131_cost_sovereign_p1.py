"""Slice 131 Phase 1 — activate the dormant cost substrate + no-hardcode failover.

The cost audit found two high-ROI features built but switched OFF:
  * Provider Response Cache (exact-match, repo-state-keyed, fail-closed on git
    diff) — 100% savings on repeat calls.
  * Economic Router (cheap-tier failover on DW 402/429) — 70-80% on micro-ops.

Both are graduated to default-TRUE here (response cache is correctness-fail-closed;
economic router only fires on the provider-failure path), and the economic
failover model is refactored to resolve from ``brain_selection_policy.yaml``
(env override wins) instead of requiring a hardcoded/env-only value (CLAUDE.md
no-hardcoded-models mandate).
"""
from __future__ import annotations

import os
import pathlib
import tempfile
import unittest

from backend.core.ouroboros.governance import economic_router as ER
from backend.core.ouroboros.governance import provider_response_cache as PRC


class TestGraduatedDefaults(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            k: os.environ.get(k)
            for k in ("JARVIS_ECONOMIC_ROUTER_ENABLED",
                      "JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED")
        }
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_economic_router_default_true(self) -> None:
        self.assertTrue(ER.economic_router_enabled())

    def test_provider_response_cache_default_true(self) -> None:
        self.assertTrue(PRC.response_cache_enabled())

    def test_both_still_hot_revertible(self) -> None:
        os.environ["JARVIS_ECONOMIC_ROUTER_ENABLED"] = "false"
        os.environ["JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED"] = "0"
        self.assertFalse(ER.economic_router_enabled())
        self.assertFalse(PRC.response_cache_enabled())


class TestFailoverModelResolution(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("JARVIS_ECONOMIC_FAILOVER_MODEL")
        os.environ.pop("JARVIS_ECONOMIC_FAILOVER_MODEL", None)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("JARVIS_ECONOMIC_FAILOVER_MODEL", None)
        else:
            os.environ["JARVIS_ECONOMIC_FAILOVER_MODEL"] = self._prev

    def test_env_override_wins(self) -> None:
        os.environ["JARVIS_ECONOMIC_FAILOVER_MODEL"] = "claude-opus-4-8"
        self.assertEqual(ER.economic_failover_model(), "claude-opus-4-8")

    def test_resolves_from_policy_when_env_unset(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            policy = pathlib.Path(d) / "brain_selection_policy.yaml"
            policy.write_text(
                "cost_optimization:\n  claude_low_cost_model: claude-haiku-4-5\n"
            )
            self.assertEqual(
                ER.economic_failover_model(policy_path=policy),
                "claude-haiku-4-5",
            )

    def test_no_hardcoded_model_string_in_module(self) -> None:
        # The CLAUDE.md no-hardcode mandate: the .py must not embed a concrete
        # claude-* model id. The default lives in the YAML config, not code.
        src = pathlib.Path(
            "backend/core/ouroboros/governance/economic_router.py"
        ).read_text()
        self.assertNotIn("claude-haiku", src)
        self.assertNotIn("claude-sonnet", src)
        self.assertNotIn("claude-opus", src)
        self.assertNotIn("claude-3-5", src)

    def test_missing_policy_is_safe_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            missing = pathlib.Path(d) / "nope.yaml"
            self.assertEqual(ER.economic_failover_model(policy_path=missing), "")

    def test_live_policy_has_low_cost_model(self) -> None:
        # The real brain_selection_policy.yaml must define the cheap tier so
        # env-unset deployments still get a failover model (not empty).
        self.assertTrue(ER.economic_failover_model())


if __name__ == "__main__":
    unittest.main()
