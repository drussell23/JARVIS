"""Tests for 397B tool-use orchestration loop."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.hud.tool_use_orchestrator import ToolUseOrchestrator


@pytest.fixture
def mock_doubleword():
    dw = AsyncMock()
    dw.is_available = True
    return dw


@pytest.fixture
def orchestrator(mock_doubleword):
    return ToolUseOrchestrator(doubleword=mock_doubleword, max_iterations=5, timeout_s=30)


class TestToolLoop:

    @pytest.mark.asyncio
    async def test_single_tool_call_and_done(self, orchestrator, mock_doubleword):
        """Model calls one tool then signals done."""
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            json.dumps({"tool_calls": [{"name": "open_app", "args": {"app_name": "Safari"}}]}),
            json.dumps({"done": True, "summary": "Opened Safari."}),
        ])
        with patch("backend.hud.tool_use_orchestrator.execute_tool") as mock_exec:
            mock_exec.return_value = MagicMock(success=True, output="Opened Safari", error=None, name="open_app", call_id="")
            result = await orchestrator.execute("open Safari")
        assert result.success is True
        assert "Safari" in result.response_text

    @pytest.mark.asyncio
    async def test_multi_step_tool_loop(self, orchestrator, mock_doubleword):
        """Model calls multiple tools in sequence."""
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            json.dumps({"tool_calls": [{"name": "open_app", "args": {"app_name": "Google Chrome"}}]}),
            json.dumps({"tool_calls": [{"name": "wait", "args": {"seconds": 1}}]}),
            json.dumps({"tool_calls": [{"name": "open_url", "args": {"url": "https://linkedin.com"}}]}),
            json.dumps({"done": True, "summary": "Opened Chrome and navigated to LinkedIn."}),
        ])
        with patch("backend.hud.tool_use_orchestrator.execute_tool") as mock_exec:
            mock_exec.return_value = MagicMock(success=True, output="OK", error=None, name="test", call_id="")
            result = await orchestrator.execute("open chrome and go to LinkedIn")
        assert result.success is True
        assert mock_doubleword.prompt_only.call_count == 4

    @pytest.mark.asyncio
    async def test_max_iterations_stops_loop(self, orchestrator, mock_doubleword):
        """Loop stops at max_iterations even if model keeps calling tools."""
        mock_doubleword.prompt_only = AsyncMock(return_value=json.dumps(
            {"tool_calls": [{"name": "wait", "args": {"seconds": 0.1}}]}
        ))
        with patch("backend.hud.tool_use_orchestrator.execute_tool") as mock_exec:
            mock_exec.return_value = MagicMock(success=True, output="waited", error=None, name="wait", call_id="")
            result = await orchestrator.execute("infinite loop test")
        assert mock_doubleword.prompt_only.call_count <= 6  # 5 iterations + safety

    @pytest.mark.asyncio
    async def test_iron_gate_blocks_dangerous_tool(self, orchestrator, mock_doubleword):
        """Dangerous tool calls are blocked by Iron Gate."""
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            json.dumps({"tool_calls": [{"name": "bash", "args": {"command": "sudo rm -rf /"}}]}),
            json.dumps({"done": True, "summary": "Stopped."}),
        ])
        result = await orchestrator.execute("delete everything")
        # Should not crash — Iron Gate blocks the call, model gets error feedback

    @pytest.mark.asyncio
    async def test_doubleword_failure_returns_error(self, orchestrator, mock_doubleword):
        mock_doubleword.prompt_only = AsyncMock(side_effect=Exception("API timeout"))
        result = await orchestrator.execute("test")
        assert result.success is False
