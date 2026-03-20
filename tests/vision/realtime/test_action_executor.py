"""Tests for vision action executor."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from backend.vision.realtime.action_executor import ActionExecutor, ActionType, ActionRequest, ActionResult


class TestActionTypes:
    def test_click_type(self):
        assert ActionType.CLICK == "click"
    def test_type_type(self):
        assert ActionType.TYPE == "type"
    def test_scroll_type(self):
        assert ActionType.SCROLL == "scroll"


class TestActionRequest:
    def test_click_request(self):
        req = ActionRequest(
            action_id="act-001",
            action_type=ActionType.CLICK,
            coords=(523, 187),
        )
        assert req.coords == (523, 187)

    def test_type_request(self):
        req = ActionRequest(
            action_id="act-002",
            action_type=ActionType.TYPE,
            text="hello world",
        )
        assert req.text == "hello world"


class TestExecutor:
    @pytest.fixture
    def executor(self):
        return ActionExecutor()

    @pytest.mark.asyncio
    async def test_click_calls_pyautogui(self, executor):
        req = ActionRequest(action_id="act-001", action_type=ActionType.CLICK, coords=(100, 200))
        with patch("backend.vision.realtime.action_executor.pyautogui") as mock_pag:
            result = await executor.execute(req)
            mock_pag.click.assert_called_once_with(100, 200)
            assert result.success is True

    @pytest.mark.asyncio
    async def test_type_calls_pyautogui(self, executor):
        req = ActionRequest(action_id="act-002", action_type=ActionType.TYPE, text="hello")
        with patch("backend.vision.realtime.action_executor.pyautogui") as mock_pag:
            result = await executor.execute(req)
            mock_pag.typewrite.assert_called_once()
            assert result.success is True

    @pytest.mark.asyncio
    async def test_scroll_calls_pyautogui(self, executor):
        req = ActionRequest(action_id="act-003", action_type=ActionType.SCROLL, scroll_amount=-3)
        with patch("backend.vision.realtime.action_executor.pyautogui") as mock_pag:
            result = await executor.execute(req)
            mock_pag.scroll.assert_called_once_with(-3)
            assert result.success is True

    @pytest.mark.asyncio
    async def test_tracks_committed_action_id(self, executor):
        req = ActionRequest(action_id="act-004", action_type=ActionType.CLICK, coords=(100, 100))
        with patch("backend.vision.realtime.action_executor.pyautogui"):
            await executor.execute(req)
        assert "act-004" in executor.committed_actions

    @pytest.mark.asyncio
    async def test_failure_returns_error(self, executor):
        req = ActionRequest(action_id="act-005", action_type=ActionType.CLICK, coords=(100, 100))
        with patch("backend.vision.realtime.action_executor.pyautogui") as mock_pag:
            mock_pag.click.side_effect = Exception("click failed")
            result = await executor.execute(req)
            assert result.success is False
            assert "click failed" in result.error

    @pytest.mark.asyncio
    async def test_result_has_latency(self, executor):
        req = ActionRequest(action_id="act-006", action_type=ActionType.CLICK, coords=(100, 100))
        with patch("backend.vision.realtime.action_executor.pyautogui"):
            result = await executor.execute(req)
            assert result.latency_ms >= 0
