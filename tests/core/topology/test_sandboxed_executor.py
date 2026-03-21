"""Tests for SandboxedExecutor — blast chamber for synthesized code."""
import asyncio

import pytest

from backend.core.topology.sandboxed_executor import (
    SandboxedExecutor,
    ExecutionResult,
    ExecutionOutcome,
    ExecutionMode,
    _extract_python_code,
    _hash_code,
    _build_safe_namespace,
)


# ---------------------------------------------------------------------------
# Code extraction tests
# ---------------------------------------------------------------------------


class TestExtractPythonCode:
    def test_strips_markdown_fence(self):
        raw = '```python\ndef execute(ctx):\n    return {"success": True}\n```'
        assert "def execute" in _extract_python_code(raw)
        assert "```" not in _extract_python_code(raw)

    def test_strips_plain_fence(self):
        raw = '```\ndef execute(ctx):\n    return {"success": True}\n```'
        assert "def execute" in _extract_python_code(raw)

    def test_no_fence_returns_as_is(self):
        raw = 'def execute(ctx):\n    return {"success": True}'
        assert _extract_python_code(raw) == raw.strip()

    def test_multiple_blocks_takes_first(self):
        raw = '```python\nblock1\n```\n```python\nblock2\n```'
        assert _extract_python_code(raw) == "block1"


class TestHashCode:
    def test_deterministic(self):
        assert _hash_code("hello") == _hash_code("hello")

    def test_different_input_different_hash(self):
        assert _hash_code("hello") != _hash_code("world")

    def test_returns_16_chars(self):
        assert len(_hash_code("test")) == 16


class TestBuildSafeNamespace:
    def test_has_builtins(self):
        ns = _build_safe_namespace("test", {})
        assert "__builtins__" in ns
        assert "print" in ns["__builtins__"]
        assert "len" in ns["__builtins__"]

    def test_has_asyncio(self):
        ns = _build_safe_namespace("test", {})
        assert "asyncio" in ns

    def test_has_json(self):
        ns = _build_safe_namespace("test", {})
        assert "json" in ns

    def test_has_goal_and_context(self):
        ns = _build_safe_namespace("my goal", {"key": "val"})
        assert ns["__goal__"] == "my goal"
        assert ns["__context__"] == {"key": "val"}

    def test_no_os_module(self):
        ns = _build_safe_namespace("test", {})
        assert "os" not in ns
        assert "subprocess" not in ns
        assert "shutil" not in ns


# ---------------------------------------------------------------------------
# ExecutionResult tests
# ---------------------------------------------------------------------------


class TestExecutionResult:
    def test_frozen(self):
        result = ExecutionResult(
            outcome=ExecutionOutcome.SUCCESS,
            mode=ExecutionMode.LOCAL,
            return_value={"success": True},
            stdout="",
            stderr="",
            elapsed_seconds=0.1,
            code_hash="abc123",
        )
        with pytest.raises(AttributeError):
            result.outcome = ExecutionOutcome.TIMEOUT

    def test_default_error_message(self):
        result = ExecutionResult(
            outcome=ExecutionOutcome.SUCCESS,
            mode=ExecutionMode.LOCAL,
            return_value=None,
            stdout="",
            stderr="",
            elapsed_seconds=0.0,
            code_hash="test",
        )
        assert result.error_message == ""


# ---------------------------------------------------------------------------
# SandboxedExecutor tests
# ---------------------------------------------------------------------------


class TestSandboxedExecutor:
    @pytest.mark.asyncio
    async def test_execute_simple_sync_function(self):
        executor = SandboxedExecutor(timeout_s=5.0)
        result = await executor.execute(
            code='def execute(ctx):\n    return {"success": True, "result": "hello"}',
            goal="test goal",
        )
        assert result.outcome == ExecutionOutcome.SUCCESS
        assert result.mode == ExecutionMode.LOCAL
        assert result.return_value == {"success": True, "result": "hello"}

    @pytest.mark.asyncio
    async def test_execute_async_function(self):
        executor = SandboxedExecutor(timeout_s=5.0)
        code = (
            "async def execute(ctx):\n"
            "    await asyncio.sleep(0.01)\n"
            "    return {'success': True, 'result': 'async works'}\n"
        )
        result = await executor.execute(code=code, goal="async test")
        assert result.outcome == ExecutionOutcome.SUCCESS
        assert result.return_value["result"] == "async works"

    @pytest.mark.asyncio
    async def test_compile_error(self):
        executor = SandboxedExecutor(timeout_s=5.0)
        result = await executor.execute(
            code="def execute(ctx\n    return {}",  # syntax error
            goal="bad code",
        )
        assert result.outcome == ExecutionOutcome.COMPILE_ERROR
        assert "SyntaxError" in result.error_message

    @pytest.mark.asyncio
    async def test_runtime_error(self):
        executor = SandboxedExecutor(timeout_s=5.0)
        result = await executor.execute(
            code='def execute(ctx):\n    raise ValueError("deliberate")',
            goal="error test",
        )
        assert result.outcome == ExecutionOutcome.RUNTIME_ERROR
        assert "ValueError" in result.error_message

    @pytest.mark.asyncio
    async def test_missing_execute_function(self):
        executor = SandboxedExecutor(timeout_s=5.0)
        result = await executor.execute(
            code='def wrong_name(ctx):\n    return {}',
            goal="missing function",
        )
        assert result.outcome == ExecutionOutcome.RUNTIME_ERROR
        assert "execute" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_timeout(self):
        executor = SandboxedExecutor(timeout_s=0.5)
        # asyncio is pre-injected into namespace, no import needed
        code = (
            "async def execute(ctx):\n"
            "    await asyncio.sleep(10)\n"
            "    return {'success': True}\n"
        )
        result = await executor.execute(code=code, goal="timeout test")
        assert result.outcome == ExecutionOutcome.TIMEOUT

    @pytest.mark.asyncio
    async def test_stdout_captured(self):
        executor = SandboxedExecutor(timeout_s=5.0)
        code = (
            'def execute(ctx):\n'
            '    print("hello from sandbox")\n'
            '    return {"success": True}\n'
        )
        result = await executor.execute(code=code, goal="stdout test")
        assert result.outcome == ExecutionOutcome.SUCCESS
        assert "hello from sandbox" in result.stdout

    @pytest.mark.asyncio
    async def test_no_os_access(self):
        executor = SandboxedExecutor(timeout_s=5.0)
        code = (
            'def execute(ctx):\n'
            '    import os\n'
            '    return {"success": True}\n'
        )
        result = await executor.execute(code=code, goal="os blocked")
        # Should fail because 'import' is not in safe builtins
        # The restricted __builtins__ prevents arbitrary imports
        assert result.outcome in (
            ExecutionOutcome.RUNTIME_ERROR,
            ExecutionOutcome.FIREWALL_BLOCKED,
        )

    @pytest.mark.asyncio
    async def test_markdown_fenced_code(self):
        executor = SandboxedExecutor(timeout_s=5.0)
        code = '```python\ndef execute(ctx):\n    return {"success": True, "result": "fenced"}\n```'
        result = await executor.execute(code=code, goal="fenced test")
        assert result.outcome == ExecutionOutcome.SUCCESS
        assert result.return_value["result"] == "fenced"

    @pytest.mark.asyncio
    async def test_non_dict_return_wrapped(self):
        executor = SandboxedExecutor(timeout_s=5.0)
        code = 'def execute(ctx):\n    return "just a string"'
        result = await executor.execute(code=code, goal="wrap test")
        assert result.outcome == ExecutionOutcome.SUCCESS
        assert result.return_value["success"] is True
        assert "just a string" in result.return_value["result"]

    @pytest.mark.asyncio
    async def test_code_hash_in_result(self):
        executor = SandboxedExecutor(timeout_s=5.0)
        code = 'def execute(ctx):\n    return {"success": True}'
        result = await executor.execute(code=code, goal="hash test")
        assert len(result.code_hash) == 16

    @pytest.mark.asyncio
    async def test_context_passed_to_function(self):
        executor = SandboxedExecutor(timeout_s=5.0)
        code = 'def execute(ctx):\n    return {"success": True, "result": ctx.get("key", "missing")}'
        result = await executor.execute(
            code=code, goal="context test", context={"key": "found_it"},
        )
        assert result.outcome == ExecutionOutcome.SUCCESS
        assert result.return_value["result"] == "found_it"

    @pytest.mark.asyncio
    async def test_reactor_offline_falls_back_to_local(self):
        """When Reactor client raises, executor falls back to local."""

        class FakeReactor:
            async def submit_ephemeral(self, envelope):
                raise ConnectionError("Reactor offline")

        executor = SandboxedExecutor(
            timeout_s=5.0,
            reactor_client=FakeReactor(),
        )
        code = 'def execute(ctx):\n    return {"success": True}'
        result = await executor.execute(code=code, goal="fallback test")
        # Should succeed via local fallback
        assert result.outcome == ExecutionOutcome.SUCCESS
        assert result.mode == ExecutionMode.LOCAL

    @pytest.mark.asyncio
    async def test_reactor_success(self):
        """When Reactor is online, uses it."""

        class FakeReactor:
            async def submit_ephemeral(self, envelope):
                return {
                    "success": True,
                    "return_value": {"result": "from reactor"},
                    "stdout": "",
                    "stderr": "",
                }

        executor = SandboxedExecutor(
            timeout_s=5.0,
            reactor_client=FakeReactor(),
        )
        code = 'def execute(ctx):\n    return {"success": True}'
        result = await executor.execute(code=code, goal="reactor test")
        assert result.outcome == ExecutionOutcome.SUCCESS
        assert result.mode == ExecutionMode.REACTOR

    @pytest.mark.asyncio
    async def test_telemetry_without_bus(self):
        """Should not raise when bus is None."""
        executor = SandboxedExecutor(timeout_s=5.0)
        result = ExecutionResult(
            outcome=ExecutionOutcome.SUCCESS,
            mode=ExecutionMode.LOCAL,
            return_value=None,
            stdout="",
            stderr="",
            elapsed_seconds=0.0,
            code_hash="test",
        )
        executor._emit_telemetry(result, "test goal")
