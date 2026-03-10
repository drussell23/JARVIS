"""Tests for Tool-Use Interface: ToolExecutor + provider tool loops."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.tool_executor import (
    ToolCall,
    ToolExecutor,
    ToolResult,
)


class TestToolExecutor:
    def test_read_file_returns_content(self, tmp_path):
        (tmp_path / "sample.py").write_text("def foo():\n    pass\n")
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(name="read_file", arguments={"path": "sample.py"}))
        assert result.error is None
        assert "def foo" in result.output

    def test_read_file_with_line_range(self, tmp_path):
        lines = "\n".join(f"line_{i}" for i in range(1, 21))
        (tmp_path / "big.py").write_text(lines)
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(
            name="read_file",
            arguments={"path": "big.py", "lines_from": 5, "lines_to": 10},
        ))
        assert result.error is None
        assert "line_5" in result.output
        assert "line_11" not in result.output

    def test_read_file_blocked_path(self, tmp_path):
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(
            name="read_file",
            arguments={"path": "../../etc/passwd"},
        ))
        assert result.error is not None
        assert "blocked" in result.error.lower()

    def test_list_symbols_returns_functions_and_classes(self, tmp_path):
        (tmp_path / "mod.py").write_text(
            "class Foo:\n    def bar(self): pass\n\ndef standalone(): pass\n"
        )
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(
            name="list_symbols",
            arguments={"module_path": "mod.py"},
        ))
        assert result.error is None
        assert "Foo" in result.output
        assert "standalone" in result.output

    def test_unknown_tool_returns_error(self, tmp_path):
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(name="nonexistent_tool", arguments={}))
        assert result.error is not None
        assert "unknown tool" in result.error.lower()

    def test_search_code_finds_pattern(self, tmp_path):
        (tmp_path / "utils.py").write_text("def score_formula(x):\n    return x * 0.55\n")
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(
            name="search_code",
            arguments={"pattern": "score_formula"},
        ))
        assert result.error is None
        assert "score_formula" in result.output

    def test_run_tests_returns_string_output(self, tmp_path):
        # Pass a nonexistent path — pytest will report an error, but output is still a string
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(
            name="run_tests",
            arguments={"paths": ["nonexistent_test.py"]},
        ))
        # Output is a string (may contain error message from pytest)
        assert isinstance(result.output, str)

    def test_get_callers_finds_call_sites(self, tmp_path):
        (tmp_path / "caller.py").write_text("result = my_function(42)\n")
        executor = ToolExecutor(repo_root=tmp_path)
        result = executor.execute(ToolCall(
            name="get_callers",
            arguments={"function_name": "my_function"},
        ))
        assert result.error is None
        assert "my_function" in result.output


class TestParseToolCallResponse:
    """Parsing 2b.2-tool schema from raw model output."""

    def test_valid_tool_call_parsed(self) -> None:
        from backend.core.ouroboros.governance.providers import _parse_tool_call_response
        raw = json.dumps({
            "schema_version": "2b.2-tool",
            "tool_call": {"name": "read_file", "arguments": {"path": "utils.py"}},
        })
        tc = _parse_tool_call_response(raw)
        assert tc is not None
        assert tc.name == "read_file"
        assert tc.arguments == {"path": "utils.py"}

    def test_patch_response_returns_none(self) -> None:
        from backend.core.ouroboros.governance.providers import _parse_tool_call_response
        raw = json.dumps({
            "schema_version": "2b.1",
            "candidates": [
                {"candidate_id": "c1", "file_path": "x.py", "full_content": "pass\n", "rationale": "ok"}
            ],
        })
        assert _parse_tool_call_response(raw) is None

    def test_invalid_json_returns_none(self) -> None:
        from backend.core.ouroboros.governance.providers import _parse_tool_call_response
        assert _parse_tool_call_response("not json") is None

    def test_tool_call_missing_name_returns_none(self) -> None:
        from backend.core.ouroboros.governance.providers import _parse_tool_call_response
        raw = json.dumps({
            "schema_version": "2b.2-tool",
            "tool_call": {"arguments": {"path": "x.py"}},
        })
        assert _parse_tool_call_response(raw) is None


class TestToolPromptInjection:
    """_build_codegen_prompt with tools_enabled=True."""

    def _make_ctx(self):
        from backend.core.ouroboros.governance.op_context import OperationContext
        return OperationContext.create(
            target_files=("backend/core/utils.py",),
            description="Add helper function",
        )

    def test_tools_section_present_when_enabled(self, tmp_path) -> None:
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        ctx = self._make_ctx()
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path, tools_enabled=True)
        assert "Available Tools" in prompt
        assert "search_code" in prompt
        assert "2b.2-tool" in prompt

    def test_tools_section_absent_when_disabled(self, tmp_path) -> None:
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        ctx = self._make_ctx()
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path, tools_enabled=False)
        assert "Available Tools" not in prompt

    def test_tools_section_absent_by_default(self, tmp_path) -> None:
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        ctx = self._make_ctx()
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "Available Tools" not in prompt
