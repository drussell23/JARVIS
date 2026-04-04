"""Tests for VoiceCommandRouter -- intent classification + routing."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.hud.voice_command_router import VoiceCommandRouter


@pytest.fixture
def mock_doubleword():
    dw = AsyncMock()
    dw.is_available = True
    return dw


@pytest.fixture
def router(mock_doubleword):
    return VoiceCommandRouter(doubleword=mock_doubleword)


class TestClassification:

    async def test_app_action_classified(self, router, mock_doubleword):
        mock_doubleword.prompt_only = AsyncMock(return_value=json.dumps(
            {"category": "app_action", "needs_vision": False, "needs_tools": False}
        ))
        with patch.object(router, "_execute_app_action", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = MagicMock(
                success=True, response_text="Opened Safari",
                category="app_action", steps_completed=1, steps_total=1, error=None,
            )
            result = await router.route("open Safari")
        mock_exec.assert_called_once()

    async def test_navigation_classified(self, router, mock_doubleword):
        mock_doubleword.prompt_only = AsyncMock(return_value=json.dumps(
            {"category": "navigation", "needs_vision": False, "needs_tools": False}
        ))
        with patch.object(router, "_execute_navigation", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = MagicMock(
                success=True, response_text="Opened LinkedIn",
                category="navigation", steps_completed=1, steps_total=1, error=None,
            )
            result = await router.route("go to LinkedIn")
        mock_exec.assert_called_once()

    async def test_composite_routes_to_tool_loop(self, router, mock_doubleword):
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            json.dumps({"category": "composite", "needs_vision": False, "needs_tools": True}),
            json.dumps({"tool_calls": [{"name": "open_app", "args": {"app_name": "Google Chrome"}}]}),
            json.dumps({"done": True, "summary": "Done."}),
        ])
        with patch("backend.hud.tool_use_orchestrator.execute_tool") as mock_exec:
            mock_exec.return_value = MagicMock(
                success=True, output="OK", error=None, name="open_app", call_id="",
            )
            result = await router.route("open chrome and go to LinkedIn")
        assert result.success is True

    async def test_query_returns_answer(self, router, mock_doubleword):
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            json.dumps({"category": "query", "needs_vision": False, "needs_tools": False}),
            "It is currently 2:30 AM.",
        ])
        result = await router.route("what time is it")
        assert result.response_text is not None

    async def test_classification_failure_fallback(self, router, mock_doubleword):
        """If classification fails, fall back to tool-use loop."""
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            "unparseable garbage",
            json.dumps({"done": True, "summary": "Tried my best."}),
        ])
        result = await router.route("do something weird")
        assert result is not None
