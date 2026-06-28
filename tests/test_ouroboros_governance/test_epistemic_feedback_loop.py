"""Tests for PR D — epistemic feedback loop: BROAD-6 gate tools + floor + ContextVar.

Covers:
  I1 — _EPISTEMIC_GATE_TOOLS is BROAD-6 (not just 3-tool set)
  I1 — _epistemic_exploration_floor() reads JARVIS_MIN_EXPLORATION_CALLS
  I1 — backward compat: JARVIS_TOOL_LOOP_MIN_EXPLORATION still works
  I2 — ContextVar is None after set_exploration_state(None)
  topology_sentinel ContextVar — get/set/clear round-trip
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class TestEpistemicGateToolsIsBroad6(unittest.TestCase):
    """I1 regression — _EPISTEMIC_GATE_TOOLS must equal BROAD-6 from exploration_floor.py."""

    def test_epistemic_gate_tools_is_broad6(self):
        from backend.core.ouroboros.governance.tool_executor import _EPISTEMIC_GATE_TOOLS
        from backend.core.ouroboros.governance.exploration_floor import (
            IRON_GATE_EXPLORATION_TOOLS,
        )
        self.assertEqual(_EPISTEMIC_GATE_TOOLS, IRON_GATE_EXPLORATION_TOOLS)

    def test_epistemic_gate_tools_contains_list_symbols(self):
        from backend.core.ouroboros.governance.tool_executor import _EPISTEMIC_GATE_TOOLS
        self.assertIn("list_symbols", _EPISTEMIC_GATE_TOOLS)

    def test_epistemic_gate_tools_contains_glob_files(self):
        from backend.core.ouroboros.governance.tool_executor import _EPISTEMIC_GATE_TOOLS
        self.assertIn("glob_files", _EPISTEMIC_GATE_TOOLS)

    def test_epistemic_gate_tools_contains_list_dir(self):
        from backend.core.ouroboros.governance.tool_executor import _EPISTEMIC_GATE_TOOLS
        self.assertIn("list_dir", _EPISTEMIC_GATE_TOOLS)

    def test_epistemic_gate_tools_size(self):
        from backend.core.ouroboros.governance.tool_executor import _EPISTEMIC_GATE_TOOLS
        self.assertEqual(len(_EPISTEMIC_GATE_TOOLS), 6)


class TestEpistemicFloor(unittest.TestCase):
    """I1 — floor reads the right env vars and uses correct defaults."""

    def _floor(self):
        from backend.core.ouroboros.governance.tool_executor import _epistemic_exploration_floor
        return _epistemic_exploration_floor()

    def test_default_floor_is_2(self):
        """No env override — defaults to 2 (conservative for unknown complexity)."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("JARVIS_MIN_EXPLORATION_CALLS", "JARVIS_TOOL_LOOP_MIN_EXPLORATION")}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self._floor(), 2)

    def test_primary_env_var(self):
        """JARVIS_MIN_EXPLORATION_CALLS wins."""
        with patch.dict(os.environ, {"JARVIS_MIN_EXPLORATION_CALLS": "3",
                                      "JARVIS_TOOL_LOOP_MIN_EXPLORATION": "1"}):
            self.assertEqual(self._floor(), 3)

    def test_backward_compat_env_var(self):
        """JARVIS_TOOL_LOOP_MIN_EXPLORATION used when primary is absent."""
        env = {k: v for k, v in os.environ.items()
               if "JARVIS_MIN_EXPLORATION_CALLS" not in k}
        env["JARVIS_TOOL_LOOP_MIN_EXPLORATION"] = "1"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self._floor(), 1)

    def test_zero_floor_allowed(self):
        with patch.dict(os.environ, {"JARVIS_MIN_EXPLORATION_CALLS": "0"}):
            self.assertEqual(self._floor(), 0)

    def test_invalid_env_var_falls_back(self):
        with patch.dict(os.environ, {"JARVIS_MIN_EXPLORATION_CALLS": "not_a_number"}):
            # Should fall back to default (2)
            self.assertEqual(self._floor(), 2)


class TestExplorationStateContextVar(unittest.TestCase):
    """topology_sentinel.DW_EXPLORATION_STATE_VAR round-trip."""

    def setUp(self):
        # Always clear before each test
        from backend.core.ouroboros.governance.topology_sentinel import set_exploration_state
        set_exploration_state(None)

    def tearDown(self):
        from backend.core.ouroboros.governance.topology_sentinel import set_exploration_state
        set_exploration_state(None)

    def test_default_is_none(self):
        from backend.core.ouroboros.governance.topology_sentinel import get_exploration_state
        self.assertIsNone(get_exploration_state())

    def test_set_and_get(self):
        from backend.core.ouroboros.governance.topology_sentinel import (
            get_exploration_state,
            set_exploration_state,
        )
        set_exploration_state({"explore_count": 2, "floor": 3})
        state = get_exploration_state()
        self.assertEqual(state["explore_count"], 2)
        self.assertEqual(state["floor"], 3)

    def test_clear_with_none(self):
        from backend.core.ouroboros.governance.topology_sentinel import (
            get_exploration_state,
            set_exploration_state,
        )
        set_exploration_state({"explore_count": 1, "floor": 2})
        set_exploration_state(None)
        self.assertIsNone(get_exploration_state())

    def test_contextvar_none_after_explicit_reset(self):
        """I2 regression — after set_exploration_state(None), var must be None."""
        from backend.core.ouroboros.governance.topology_sentinel import (
            get_exploration_state,
            set_exploration_state,
        )
        set_exploration_state({"explore_count": 5, "floor": 2})
        self.assertIsNotNone(get_exploration_state())
        set_exploration_state(None)
        self.assertIsNone(get_exploration_state())


class TestIronGateExplorationFloor(unittest.TestCase):
    """Tests for exploration_floor.iron_gate_exploration_floor()."""

    def _floor(self, complexity=""):
        from backend.core.ouroboros.governance.exploration_floor import iron_gate_exploration_floor
        return iron_gate_exploration_floor(task_complexity=complexity)

    def test_simple_complexity_returns_1(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JARVIS_MIN_EXPLORATION_CALLS", "JARVIS_TOOL_LOOP_MIN_EXPLORATION")}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self._floor("simple"), 1)

    def test_moderate_complexity_returns_2(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JARVIS_MIN_EXPLORATION_CALLS", "JARVIS_TOOL_LOOP_MIN_EXPLORATION")}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self._floor("moderate"), 2)

    def test_unknown_complexity_returns_2(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JARVIS_MIN_EXPLORATION_CALLS", "JARVIS_TOOL_LOOP_MIN_EXPLORATION")}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self._floor(""), 2)

    def test_env_override_wins_over_complexity(self):
        with patch.dict(os.environ, {"JARVIS_MIN_EXPLORATION_CALLS": "0"}):
            self.assertEqual(self._floor("complex"), 0)


if __name__ == "__main__":
    unittest.main()
