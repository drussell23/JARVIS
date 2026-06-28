"""Tests for PR D — DW native tool forcing (JARVIS_DW_NATIVE_TOOL_FORCING_ENABLED).

Covers:
  C1 — Schema arg names match real handler arg names (KeyError if wrong)
  C2 — 400 rejection from endpoint falls back to legacy (no tool injection)
  Schema shape and gating behavior
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestDwExplorationToolSchemas(unittest.TestCase):
    """Tests for _dw_exploration_only_tool_schemas() shape and correctness."""

    def setUp(self):
        from backend.core.ouroboros.governance.doubleword_provider import (
            _dw_exploration_only_tool_schemas,
        )
        self._schemas = _dw_exploration_only_tool_schemas()

    def test_returns_three_tools(self):
        self.assertEqual(len(self._schemas), 3)

    def test_tool_names(self):
        names = {s["function"]["name"] for s in self._schemas}
        self.assertEqual(names, {"read_file", "search_code", "get_callers"})

    def test_read_file_required_param(self):
        rf = next(s for s in self._schemas if s["function"]["name"] == "read_file")
        self.assertIn("path", rf["function"]["parameters"]["required"])
        self.assertIn("path", rf["function"]["parameters"]["properties"])

    def test_search_code_required_param_is_pattern(self):
        """C1 regression — must be 'pattern', NOT 'query'."""
        sc = next(s for s in self._schemas if s["function"]["name"] == "search_code")
        self.assertIn("pattern", sc["function"]["parameters"]["required"])
        self.assertIn("pattern", sc["function"]["parameters"]["properties"])
        # Must NOT have old wrong name
        self.assertNotIn("query", sc["function"]["parameters"]["properties"])

    def test_search_code_optional_param_is_file_glob(self):
        """C1 regression — must be 'file_glob', NOT 'path'."""
        sc = next(s for s in self._schemas if s["function"]["name"] == "search_code")
        props = sc["function"]["parameters"]["properties"]
        self.assertIn("file_glob", props)
        # Must NOT have old wrong name
        self.assertNotIn("path", props)

    def test_get_callers_required_param_is_function_name(self):
        """C1 regression — must be 'function_name', NOT 'symbol'."""
        gc = next(s for s in self._schemas if s["function"]["name"] == "get_callers")
        self.assertIn("function_name", gc["function"]["parameters"]["required"])
        self.assertIn("function_name", gc["function"]["parameters"]["properties"])
        # Must NOT have old wrong name
        self.assertNotIn("symbol", gc["function"]["parameters"]["properties"])

    def test_all_type_function(self):
        for s in self._schemas:
            self.assertEqual(s["type"], "function")


class TestHandlerArgNamesMatchSchemas(unittest.TestCase):
    """C1 regression — schema arg names must match tool handler arg names.

    These tests invoke the ACTUAL arg-reading code path so wrong names
    (e.g. 'query' for 'pattern') cause KeyError, not a passing test.
    """

    def setUp(self):
        from backend.core.ouroboros.governance.tool_executor import ToolExecutor
        self._te = ToolExecutor.__new__(ToolExecutor)
        self._te._repo_root = Path("/nonexistent_test_repo")

    def test_search_code_correct_arg_name(self):
        """schema: pattern (required) — handler reads args['pattern']."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="foo.py:1: match", returncode=0)
            result = self._te._search_code({"pattern": "my_pattern"})
        self.assertIsInstance(result, str)

    def test_search_code_wrong_arg_raises_keyerror(self):
        """Passing 'query' (old wrong name) must raise KeyError."""
        with self.assertRaises(KeyError):
            self._te._search_code({"query": "my_pattern"})

    def test_get_callers_correct_arg_name(self):
        """schema: function_name (required) — handler reads args['function_name']."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            result = self._te._get_callers({"function_name": "my_func"})
        self.assertIsInstance(result, str)

    def test_get_callers_wrong_arg_raises_keyerror(self):
        """Passing 'symbol' (old wrong name) must raise KeyError."""
        with self.assertRaises(KeyError):
            self._te._get_callers({"symbol": "my_func"})


class TestDwNativeToolForcingEnabled(unittest.TestCase):
    """Tests for the _dw_native_tool_forcing_enabled() gate."""

    def test_disabled_by_default(self):
        from backend.core.ouroboros.governance.doubleword_provider import (
            _dw_native_tool_forcing_enabled,
        )
        env = {k: v for k, v in os.environ.items() if "JARVIS_DW_NATIVE" not in k}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(_dw_native_tool_forcing_enabled())

    def test_enabled_by_env(self):
        from backend.core.ouroboros.governance.doubleword_provider import (
            _dw_native_tool_forcing_enabled,
        )
        with patch.dict(os.environ, {"JARVIS_DW_NATIVE_TOOL_FORCING_ENABLED": "true"}):
            self.assertTrue(_dw_native_tool_forcing_enabled())

    def test_enabled_by_1(self):
        from backend.core.ouroboros.governance.doubleword_provider import (
            _dw_native_tool_forcing_enabled,
        )
        with patch.dict(os.environ, {"JARVIS_DW_NATIVE_TOOL_FORCING_ENABLED": "1"}):
            self.assertTrue(_dw_native_tool_forcing_enabled())


class TestDwToolForcingRejectionCache(unittest.TestCase):
    """C2 regression — 400 rejection caches per model ID."""

    def setUp(self):
        from backend.core.ouroboros.governance import doubleword_provider as _dp
        # Reset the cache before each test
        _dp._DW_TOOL_FORCING_REJECTED.clear()

    def tearDown(self):
        from backend.core.ouroboros.governance import doubleword_provider as _dp
        _dp._DW_TOOL_FORCING_REJECTED.clear()

    def test_unknown_model_not_rejected(self):
        from backend.core.ouroboros.governance.doubleword_provider import (
            _dw_tool_forcing_known_unsupported,
        )
        self.assertFalse(_dw_tool_forcing_known_unsupported("some-model"))

    def test_mark_and_check(self):
        from backend.core.ouroboros.governance.doubleword_provider import (
            _dw_mark_tool_forcing_unsupported,
            _dw_tool_forcing_known_unsupported,
        )
        _dw_mark_tool_forcing_unsupported("qwen3-397b")
        self.assertTrue(_dw_tool_forcing_known_unsupported("qwen3-397b"))
        self.assertFalse(_dw_tool_forcing_known_unsupported("other-model"))

    def test_empty_model_id(self):
        from backend.core.ouroboros.governance.doubleword_provider import (
            _dw_mark_tool_forcing_unsupported,
            _dw_tool_forcing_known_unsupported,
        )
        _dw_mark_tool_forcing_unsupported("")
        self.assertTrue(_dw_tool_forcing_known_unsupported(""))
        self.assertFalse(_dw_tool_forcing_known_unsupported("other"))


class TestNativeTcSentinel(unittest.TestCase):
    """Sentinel constant must be non-empty and start with newline."""

    def test_sentinel_shape(self):
        from backend.core.ouroboros.governance.doubleword_provider import (
            _NATIVE_TC_SENTINEL,
        )
        self.assertTrue(_NATIVE_TC_SENTINEL.startswith("\n"))
        self.assertGreater(len(_NATIVE_TC_SENTINEL), 1)


if __name__ == "__main__":
    unittest.main()
