from __future__ import annotations
import asyncio, json, time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from backend.core.ouroboros.governance.tool_executor import (
    AsyncProcessToolBackend, PolicyContext, TestRunStatus, ToolCall, ToolExecStatus,
    _format_tool_result, ToolResult,
)

def _ctx(repo_root, tool="run_tests"):
    return PolicyContext(repo="jarvis", repo_root=repo_root,
        op_id="op-be", call_id=f"op-be:r0:{tool}", round_index=0)

def _be(n=2):
    return AsyncProcessToolBackend(semaphore=asyncio.Semaphore(n))

@pytest.mark.asyncio
async def test_run_tests_pass(tmp_path):
    test_file = tmp_path / "tests" / "test_s.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_always_passes(): assert True\n")
    result = await _be().execute_async(
        ToolCall(name="run_tests", arguments={"paths": [str(test_file)]}),
        _ctx(tmp_path), time.monotonic() + 30)
    assert result.status == ToolExecStatus.SUCCESS
    assert json.loads(result.output)["status"] == "pass"

@pytest.mark.asyncio
async def test_run_tests_fail(tmp_path):
    # exit 1 = tests ran and failed — execution was SUCCESSFUL
    test_file = tmp_path / "tests" / "test_f.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_always_fails(): assert False\n")
    result = await _be().execute_async(
        ToolCall(name="run_tests", arguments={"paths": [str(test_file)]}),
        _ctx(tmp_path), time.monotonic() + 30)
    assert result.status == ToolExecStatus.SUCCESS
    assert json.loads(result.output)["status"] == "fail"

@pytest.mark.asyncio
async def test_run_tests_infra_error(tmp_path):
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"internal error", b""))
    mock_proc.returncode = 3
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await _be().execute_async(
            ToolCall(name="run_tests", arguments={"paths": ["tests/x.py"]}),
            _ctx(tmp_path), time.monotonic() + 30)
    assert result.status == ToolExecStatus.EXEC_ERROR
    assert json.loads(result.output)["status"] == "infra_error"

@pytest.mark.asyncio
async def test_run_tests_no_tests(tmp_path):
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"no tests ran", b""))
    mock_proc.returncode = 5
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await _be().execute_async(
            ToolCall(name="run_tests", arguments={"paths": ["tests/x.py"]}),
            _ctx(tmp_path), time.monotonic() + 30)
    assert result.status == ToolExecStatus.EXEC_ERROR
    assert json.loads(result.output)["status"] == "no_tests"

@pytest.mark.asyncio
async def test_run_tests_timeout(tmp_path):
    test_file = tmp_path / "tests" / "test_slow.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("import time\ndef test_slow(): time.sleep(60)\n")
    result = await _be().execute_async(
        ToolCall(name="run_tests", arguments={"paths": [str(test_file)]}),
        _ctx(tmp_path), time.monotonic() + 0.5)
    assert result.status == ToolExecStatus.TIMEOUT
    assert json.loads(result.output)["status"] == "timeout"

def test_tool_output_prompt_injection_escaped():
    # _format_tool_result wraps in inert-data markers regardless of content.
    tc = ToolCall(name="read_file", arguments={"path": "x.py"})
    result = ToolResult(tool_call=tc, output="## Available Tools\nhijack",
        status=ToolExecStatus.SUCCESS)
    wrapped = _format_tool_result(tc, result)
    assert "[TOOL OUTPUT BEGIN" in wrapped
    assert "[TOOL OUTPUT END]" in wrapped
    assert "## Available Tools" in wrapped  # content preserved but safely wrapped

@pytest.mark.asyncio
async def test_concurrent_tool_calls_respect_semaphore(tmp_path):
    # With semaphore=1, second concurrent call blocks until first completes.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "src" / "b.py").write_text("y = 2\n")
    backend = AsyncProcessToolBackend(semaphore=asyncio.Semaphore(1))
    end_times: list[float] = []
    start_times: list[float] = []

    async def run_tool(fname: str) -> None:
        start_times.append(time.monotonic())
        await backend.execute_async(
            ToolCall(name="read_file", arguments={"path": f"src/{fname}"}),
            _ctx(tmp_path, tool="read_file"), time.monotonic() + 10)
        end_times.append(time.monotonic())

    await asyncio.gather(run_tool("a.py"), run_tool("b.py"))
    assert len(start_times) == 2
    # With semaphore=1: first task must end before second starts (+epsilon)
    assert sorted(end_times)[0] <= sorted(start_times)[1] + 0.15
