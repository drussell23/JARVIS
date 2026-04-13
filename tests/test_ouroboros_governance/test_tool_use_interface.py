"""Tests for Tool-Use Interface: ToolExecutor + provider tool loops."""

from __future__ import annotations

import dataclasses
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

    def test_empty_name_returns_none(self) -> None:
        from backend.core.ouroboros.governance.providers import _parse_tool_call_response
        raw = json.dumps({
            "schema_version": "2b.2-tool",
            "tool_call": {"name": "", "arguments": {}},
        })
        assert _parse_tool_call_response(raw) is None

    def test_wrong_type_arguments_normalizes_to_empty_dict(self) -> None:
        from backend.core.ouroboros.governance.providers import _parse_tool_call_response
        raw = json.dumps({
            "schema_version": "2b.2-tool",
            "tool_call": {"name": "read_file", "arguments": "not-a-dict"},
        })
        tc = _parse_tool_call_response(raw)
        assert tc is not None
        assert tc.name == "read_file"
        assert tc.arguments == {}


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

    def test_voice_origin_tools_prompt_uses_plain_language_mode(self, tmp_path) -> None:
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        ctx = OperationContext.create(
            target_files=("backend/core/utils.py",),
            description="Explain the routing fix",
            signal_source="voice_human",
        )
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path, tools_enabled=True)
        assert "Mode: plain-language, no shared context" in prompt
        assert "Voice-First Prompt Mode" in prompt
        assert "Assume the listener cannot see the screen" in prompt

    def test_lean_prompt_immediate_route_uses_voice_first_guidance(self, tmp_path) -> None:
        from backend.core.ouroboros.governance.providers import _build_lean_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "core.py").write_text("def route():\n    return 'ok'\n")
        ctx = OperationContext.create(
            target_files=("backend/core.py",),
            description="Inspect the immediate route",
        )
        ctx = dataclasses.replace(
            ctx,
            provider_route="immediate",
            provider_route_reason="voice_command:human_waiting",
        )
        prompt = _build_lean_codegen_prompt(ctx, repo_root=tmp_path)
        assert "Mode: plain-language, no shared context" in prompt
        assert "Voice-First Prompt Mode" in prompt
        assert "name the file, subsystem, or action explicitly" in prompt.lower()

    def test_standard_route_tools_prompt_omits_voice_first_guidance(self, tmp_path) -> None:
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        ctx = OperationContext.create(
            target_files=("backend/core/utils.py",),
            description="Explain the routing fix",
            signal_source="backlog",
        )
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path, tools_enabled=True)
        assert "Mode: plain-language, no shared context" not in prompt
        assert "Voice-First Prompt Mode" not in prompt


from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)
from datetime import datetime, timezone


def _make_ctx(op_id: str = "op-test-tool-001") -> OperationContext:
    return OperationContext.create(
        target_files=("tests/test_utils.py",),
        description="Add test for edge case",
        op_id=op_id,
    )


def _prime_response(schema: str = "2b.1", **extra) -> str:
    """Build a minimal valid prime response JSON string."""
    if schema == "2b.2-tool":
        return json.dumps({
            "schema_version": "2b.2-tool",
            "tool_call": extra.get("tool_call", {"name": "search_code", "arguments": {"pattern": "foo"}}),
        })
    return json.dumps({
        "schema_version": "2b.1",
        "candidates": [
            {
                "candidate_id": "c1",
                "file_path": extra.get("file_path", "tests/test_utils.py"),
                "full_content": extra.get("content", "def test_edge():\n    assert True\n"),
                "rationale": "test",
            }
        ],
    })


class TestPrimeProviderToolLoop:
    """PrimeProvider: multi-turn tool-call loop."""

    def _mock_prime_client(self, responses: list[str]) -> MagicMock:
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.model = "prime-7b"
        mock_resp.latency_ms = 100.0
        mock_resp.tokens_used = 100
        mock_resp.metadata = {}
        # Cycle through responses on each generate() call
        call_count = [0]
        async def _generate(**kwargs):
            i = min(call_count[0], len(responses) - 1)
            call_count[0] += 1
            mock_resp.content = responses[i]
            return mock_resp
        client.generate = _generate
        # Expose counter so tests can assert exact call counts
        client._call_count = call_count
        return client

    async def test_tool_loop_disabled_by_default(self, tmp_path: Path) -> None:
        """With tools_enabled=False (default), generate() returns first response."""
        from backend.core.ouroboros.governance.providers import PrimeProvider
        client = self._mock_prime_client([_prime_response()])
        provider = PrimeProvider(client, repo_root=tmp_path)
        ctx = _make_ctx()
        deadline = datetime(2026, 3, 9, 12, 5, 0, tzinfo=timezone.utc)
        result = await provider.generate(ctx, deadline)
        assert isinstance(result, GenerationResult)
        assert len(result.candidates) == 1

    async def test_tool_loop_single_tool_then_patch(self, tmp_path: Path) -> None:
        """One tool call then patch: generate() calls client twice, returns patch."""
        from backend.core.ouroboros.governance.providers import PrimeProvider
        responses = [
            _prime_response("2b.2-tool", tool_call={"name": "read_file", "arguments": {"path": "tests/test_utils.py"}}),
            _prime_response("2b.1"),
        ]
        client = self._mock_prime_client(responses)
        provider = PrimeProvider(client, repo_root=tmp_path, tools_enabled=True)
        ctx = _make_ctx()
        deadline = datetime(2026, 3, 9, 12, 30, 0, tzinfo=timezone.utc)
        result = await provider.generate(ctx, deadline)
        assert isinstance(result, GenerationResult)
        assert len(result.candidates) == 1

    async def test_tool_loop_exhausts_max_iterations(self, tmp_path: Path) -> None:
        """If model keeps calling tools past MAX_TOOL_ITERATIONS, raise RuntimeError."""
        from backend.core.ouroboros.governance.providers import PrimeProvider, MAX_TOOL_ITERATIONS
        # All responses are tool calls (never produces a patch)
        responses = [_prime_response("2b.2-tool")] * (MAX_TOOL_ITERATIONS + 2)
        client = self._mock_prime_client(responses)
        provider = PrimeProvider(client, repo_root=tmp_path, tools_enabled=True)
        ctx = _make_ctx()
        deadline = datetime(2026, 3, 9, 12, 30, 0, tzinfo=timezone.utc)
        with pytest.raises(RuntimeError, match="tool_loop_max_iterations"):
            await provider.generate(ctx, deadline)
        # The guard fires after the (MAX_TOOL_ITERATIONS+1)th client call:
        # rounds 0..4 each call client then execute; round 5 calls client, sees
        # tool_rounds==5 >= MAX_TOOL_ITERATIONS, raises before any execution.
        assert client._call_count[0] == MAX_TOOL_ITERATIONS + 1

    async def test_tool_loop_token_budget_wall(self, tmp_path: Path) -> None:
        """When accumulated prompt exceeds MAX_TOOL_LOOP_CHARS, raise RuntimeError."""
        from backend.core.ouroboros.governance.providers import PrimeProvider
        # First response is a tool call for search_code
        responses = [
            _prime_response("2b.2-tool", tool_call={"name": "search_code", "arguments": {"pattern": "foo"}}),
        ]
        client = self._mock_prime_client(responses)
        provider = PrimeProvider(client, repo_root=tmp_path, tools_enabled=True)
        # Mock executor to return huge output
        from backend.core.ouroboros.governance.tool_executor import ToolExecutor, ToolResult, ToolCall as TC
        with patch.object(ToolExecutor, "execute", return_value=ToolResult(
            tool_call=TC(name="search_code", arguments={"pattern": "foo"}),
            output="x" * 40_000,
        )):
            ctx = _make_ctx()
            deadline = datetime(2026, 3, 9, 12, 30, 0, tzinfo=timezone.utc)
            with pytest.raises(RuntimeError, match="tool_loop_budget_exceeded"):
                await provider.generate(ctx, deadline)


class TestClaudeProviderToolLoop:
    """ClaudeProvider: multi-turn tool-call loop using messages API."""

    def _mock_claude_client(self, responses: list[str]) -> MagicMock:
        """Build a mock anthropic client cycling through response texts."""
        call_count = [0]
        async def _create(**kwargs):
            i = min(call_count[0], len(responses) - 1)
            call_count[0] += 1
            msg = MagicMock()
            msg.content = [MagicMock(text=responses[i])]
            msg.usage = MagicMock(input_tokens=100, output_tokens=100)
            msg.model = "claude-sonnet-4-6"
            return msg
        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = _create
        client._call_count = call_count
        return client

    async def test_tool_loop_disabled_by_default(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = self._mock_claude_client([_prime_response()])
        ctx = _make_ctx("op-claude-001")
        deadline = datetime(2026, 3, 9, 12, 5, 0, tzinfo=timezone.utc)
        result = await provider.generate(ctx, deadline)
        assert isinstance(result, GenerationResult)

    async def test_tool_loop_single_tool_then_patch(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        responses = [
            _prime_response("2b.2-tool", tool_call={"name": "list_symbols", "arguments": {"module_path": "utils.py"}}),
            _prime_response("2b.1"),
        ]
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path, tools_enabled=True)
        provider._client = self._mock_claude_client(responses)
        ctx = _make_ctx("op-claude-002")
        deadline = datetime(2026, 3, 9, 12, 30, 0, tzinfo=timezone.utc)
        result = await provider.generate(ctx, deadline)
        assert isinstance(result, GenerationResult)
        assert len(result.candidates) == 1

    async def test_tool_loop_exhausts_max_iterations(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider, MAX_TOOL_ITERATIONS
        responses = [_prime_response("2b.2-tool")] * (MAX_TOOL_ITERATIONS + 2)
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path, tools_enabled=True)
        provider._client = self._mock_claude_client(responses)
        ctx = _make_ctx("op-claude-003")
        deadline = datetime(2026, 3, 9, 12, 30, 0, tzinfo=timezone.utc)
        with pytest.raises(RuntimeError, match="tool_loop_max_iterations"):
            await provider.generate(ctx, deadline)
        # guard fires on iteration MAX_TOOL_ITERATIONS (0-indexed), after the client
        # is called but before any tool execution — total API calls == MAX + 1
        assert provider._client._call_count[0] == MAX_TOOL_ITERATIONS + 1

    async def test_tool_loop_budget_exceeded(self, tmp_path: Path) -> None:
        """When accumulated prompt exceeds MAX_TOOL_LOOP_CHARS, raise RuntimeError."""
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        responses = [
            _prime_response("2b.2-tool", tool_call={"name": "search_code", "arguments": {"pattern": "foo"}}),
        ]
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path, tools_enabled=True)
        provider._client = self._mock_claude_client(responses)
        from backend.core.ouroboros.governance.tool_executor import ToolExecutor, ToolResult, ToolCall as TC
        with patch.object(ToolExecutor, "execute", return_value=ToolResult(
            tool_call=TC(name="search_code", arguments={"pattern": "foo"}),
            output="x" * 40_000,
        )):
            ctx = _make_ctx("op-claude-budget")
            deadline = datetime(2026, 3, 9, 12, 30, 0, tzinfo=timezone.utc)
            with pytest.raises(RuntimeError, match="tool_loop_budget_exceeded"):
                await provider.generate(ctx, deadline)
