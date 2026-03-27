"""
Tests for CU Step Executor — 3-layer cascade (accessibility -> Doubleword -> Claude).

TDD: tests written first, covering:
  - Direct execution (type/key/hotkey without target skip vision)
  - Accessibility layer resolves target
  - Doubleword fallback when accessibility fails
  - Claude fallback when Doubleword fails
  - Keyboard steps skip vision cascade
  - Verification via frame diff
  - Wait-for-condition polling
  - Error handling / graceful degradation
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Minimal CUStep / CUTask stubs (Task 1 may not be committed yet)
# ---------------------------------------------------------------------------

@dataclass
class CUStep:
    """Minimal CUStep for testing — matches the contract from cu_task_planner."""
    action: str  # click, type, key, hotkey, scroll, wait
    target: Optional[str] = None  # UI element description
    value: Optional[str] = None  # text to type, key name, etc.
    description: str = ""
    app_name: str = ""
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# Patch CUStep into the expected module path before importing executor
# ---------------------------------------------------------------------------
_mock_planner_module = type(sys)("backend.vision.cu_task_planner")
_mock_planner_module.CUStep = CUStep  # type: ignore[attr-defined]
sys.modules.setdefault("backend.vision.cu_task_planner", _mock_planner_module)

# Now import the executor
from backend.vision.cu_step_executor import (
    CUStepExecutor,
    StepResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_frame(w: int = 100, h: int = 80, c: int = 3) -> np.ndarray:
    """Create a random RGB frame for testing."""
    rng = np.random.default_rng(42)
    return rng.integers(0, 256, size=(h, w, c), dtype=np.uint8)


def _make_changed_frame(base: np.ndarray) -> np.ndarray:
    """Return a frame that differs significantly from *base*."""
    changed = base.copy()
    changed[:] = (changed.astype(np.int16) + 50).clip(0, 255).astype(np.uint8)
    return changed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def executor():
    """Create executor with all external deps mocked out."""
    with (
        patch("backend.vision.cu_step_executor._get_ax_resolver", return_value=None),
        patch("backend.vision.cu_step_executor._get_shm_reader", return_value=None),
    ):
        ex = CUStepExecutor()
    # Null out the clients so they don't try real API calls
    ex._dw_api_key = ""
    ex._anthropic_key = ""
    return ex


@pytest.fixture
def executor_with_accessibility():
    """Executor with a working mock accessibility resolver."""
    mock_resolver = AsyncMock()
    mock_resolver.resolve = AsyncMock(return_value={
        "x": 500, "y": 300, "width": 120, "height": 30,
    })
    with (
        patch("backend.vision.cu_step_executor._get_ax_resolver", return_value=mock_resolver),
        patch("backend.vision.cu_step_executor._get_shm_reader", return_value=None),
    ):
        ex = CUStepExecutor()
    ex._dw_api_key = ""
    ex._anthropic_key = ""
    return ex


# ---------------------------------------------------------------------------
# Test StepResult dataclass
# ---------------------------------------------------------------------------

class TestStepResult:
    def test_defaults(self):
        r = StepResult(success=True, layer_used="direct", step_index=0)
        assert r.success is True
        assert r.layer_used == "direct"
        assert r.step_index == 0
        assert r.coords is None
        assert r.confidence == 0.0
        assert r.elapsed_ms >= 0.0
        assert r.error is None
        assert r.verified is False

    def test_full_construction(self):
        r = StepResult(
            success=True, layer_used="accessibility", step_index=3,
            coords=(500, 300), confidence=0.95, elapsed_ms=4.2,
            error=None, verified=True,
        )
        assert r.coords == (500, 300)
        assert r.confidence == 0.95
        assert r.verified is True


# ---------------------------------------------------------------------------
# Test direct execution (no vision needed)
# ---------------------------------------------------------------------------

class TestDirectExecution:
    """Steps that don't need a target should execute directly without vision."""

    @pytest.mark.asyncio
    async def test_type_without_target(self, executor):
        """type action with no target -> direct execution via clipboard."""
        step = CUStep(action="type", value="hello world", description="type text")
        with patch("backend.vision.cu_step_executor._execute_action_impl") as mock_exec:
            mock_exec.return_value = None
            result = await executor.execute_step(step, frame=_make_frame(), step_index=0)
        assert result.success is True
        assert result.layer_used == "direct"
        mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_key_without_target(self, executor):
        """key action with no target -> direct execution."""
        step = CUStep(action="key", value="Return", description="press enter")
        with patch("backend.vision.cu_step_executor._execute_action_impl") as mock_exec:
            mock_exec.return_value = None
            result = await executor.execute_step(step, frame=_make_frame(), step_index=1)
        assert result.success is True
        assert result.layer_used == "direct"

    @pytest.mark.asyncio
    async def test_hotkey_without_target(self, executor):
        """hotkey action with no target -> direct execution."""
        step = CUStep(action="hotkey", value="cmd+v", description="paste")
        with patch("backend.vision.cu_step_executor._execute_action_impl") as mock_exec:
            mock_exec.return_value = None
            result = await executor.execute_step(step, frame=_make_frame(), step_index=2)
        assert result.success is True
        assert result.layer_used == "direct"


# ---------------------------------------------------------------------------
# Test accessibility layer (Layer 1)
# ---------------------------------------------------------------------------

class TestAccessibilityLayer:
    """Accessibility resolver handles ~80% of steps instantly."""

    @pytest.mark.asyncio
    async def test_accessibility_resolves_target(self, executor_with_accessibility):
        """When AX resolver finds the element, no further layers are needed."""
        step = CUStep(
            action="click", target="Search field",
            app_name="Safari", description="click search",
        )
        with patch("backend.vision.cu_step_executor._execute_action_impl") as mock_exec:
            mock_exec.return_value = None
            result = await executor_with_accessibility.execute_step(
                step, frame=_make_frame(), step_index=0,
            )
        assert result.success is True
        assert result.layer_used == "accessibility"
        assert result.coords == (500, 300)
        assert result.confidence >= 0.95

    @pytest.mark.asyncio
    async def test_accessibility_returns_none_falls_through(self, executor):
        """When AX resolver returns None, cascade should try next layer."""
        mock_resolver = AsyncMock()
        mock_resolver.resolve = AsyncMock(return_value=None)
        executor._ax_resolver = mock_resolver

        step = CUStep(
            action="click", target="Invisible button",
            app_name="Finder", description="click invisible",
        )
        # No DW or Claude keys => all layers fail => result.success is False
        result = await executor.execute_step(step, frame=_make_frame(), step_index=0)
        assert result.success is False
        assert result.layer_used == "none"


# ---------------------------------------------------------------------------
# Test Doubleword layer (Layer 2)
# ---------------------------------------------------------------------------

class TestDoublewordLayer:
    """Doubleword visual grounding as second layer."""

    @pytest.mark.asyncio
    async def test_doubleword_fallback_success(self, executor):
        """When accessibility fails but Doubleword succeeds."""
        # AX returns None
        executor._ax_resolver = AsyncMock()
        executor._ax_resolver.resolve = AsyncMock(return_value=None)

        # Enable DW
        executor._dw_api_key = "test-dw-key"

        dw_response = {
            "x": 640, "y": 480, "confidence": 0.88, "element": "button",
        }

        with (
            patch.object(executor, "_ask_doubleword_vision", new_callable=AsyncMock) as mock_dw,
            patch("backend.vision.cu_step_executor._execute_action_impl") as mock_exec,
        ):
            mock_dw.return_value = dw_response
            mock_exec.return_value = None
            step = CUStep(
                action="click", target="Submit button",
                app_name="Chrome", description="click submit",
            )
            result = await executor.execute_step(step, frame=_make_frame(), step_index=0)

        assert result.success is True
        assert result.layer_used == "doubleword"
        assert result.coords == (640, 480)
        assert result.confidence == 0.88

    @pytest.mark.asyncio
    async def test_doubleword_failure_falls_to_claude(self, executor):
        """When both AX and DW fail, cascade falls to Claude."""
        executor._ax_resolver = AsyncMock()
        executor._ax_resolver.resolve = AsyncMock(return_value=None)
        executor._dw_api_key = "test-dw-key"
        executor._anthropic_key = "test-anthropic-key"

        claude_response = {
            "x": 700, "y": 500, "confidence": 0.82, "element": "div.submit",
        }

        with (
            patch.object(executor, "_ask_doubleword_vision", new_callable=AsyncMock) as mock_dw,
            patch.object(executor, "_ask_claude_vision", new_callable=AsyncMock) as mock_claude,
            patch("backend.vision.cu_step_executor._execute_action_impl") as mock_exec,
        ):
            mock_dw.return_value = None  # DW fails
            mock_claude.return_value = claude_response
            mock_exec.return_value = None
            step = CUStep(
                action="click", target="Submit button",
                app_name="Chrome", description="click submit",
            )
            result = await executor.execute_step(step, frame=_make_frame(), step_index=0)

        assert result.success is True
        assert result.layer_used == "claude"
        assert result.coords == (700, 500)


# ---------------------------------------------------------------------------
# Test keyboard steps skip vision
# ---------------------------------------------------------------------------

class TestKeyboardSkipsVision:
    """Keyboard-only steps (type, key, hotkey) with no target skip the cascade."""

    @pytest.mark.asyncio
    async def test_type_with_value_only(self, executor):
        step = CUStep(action="type", value="pytest rocks", description="type text")
        with patch("backend.vision.cu_step_executor._execute_action_impl") as mock_exec:
            mock_exec.return_value = None
            result = await executor.execute_step(step, frame=_make_frame(), step_index=5)
        assert result.layer_used == "direct"
        assert result.success is True

    @pytest.mark.asyncio
    async def test_key_press(self, executor):
        step = CUStep(action="key", value="Escape", description="press escape")
        with patch("backend.vision.cu_step_executor._execute_action_impl") as mock_exec:
            mock_exec.return_value = None
            result = await executor.execute_step(step, frame=_make_frame(), step_index=6)
        assert result.layer_used == "direct"

    @pytest.mark.asyncio
    async def test_hotkey_combo(self, executor):
        step = CUStep(action="hotkey", value="cmd+shift+t", description="reopen tab")
        with patch("backend.vision.cu_step_executor._execute_action_impl") as mock_exec:
            mock_exec.return_value = None
            result = await executor.execute_step(step, frame=_make_frame(), step_index=7)
        assert result.layer_used == "direct"


# ---------------------------------------------------------------------------
# Test verification via frame diff
# ---------------------------------------------------------------------------

class TestVerification:
    """Post-action verification by comparing pre/post frames."""

    @pytest.mark.asyncio
    async def test_verify_detects_change(self, executor):
        pre = _make_frame()
        post = _make_changed_frame(pre)
        verified = executor._verify_frames_changed(pre, post)
        assert verified is True

    @pytest.mark.asyncio
    async def test_verify_detects_no_change(self, executor):
        pre = _make_frame()
        post = pre.copy()
        verified = executor._verify_frames_changed(pre, post)
        assert verified is False


# ---------------------------------------------------------------------------
# Test frame-to-base64 conversion
# ---------------------------------------------------------------------------

class TestFrameToBase64:
    def test_encodes_jpeg(self, executor):
        frame = _make_frame(w=64, h=48)
        b64 = executor._frame_to_b64(frame)
        assert isinstance(b64, str)
        # Should be valid base64
        raw = base64.b64decode(b64)
        assert len(raw) > 0
        # JPEG magic bytes
        assert raw[:2] == b"\xff\xd8"


# ---------------------------------------------------------------------------
# Test get_live_frame
# ---------------------------------------------------------------------------

class TestGetLiveFrame:
    def test_no_shm_returns_none(self, executor):
        assert executor._shm_reader is None
        frame = executor.get_live_frame()
        assert frame is None

    def test_shm_returns_frame(self, executor):
        mock_reader = MagicMock()
        mock_reader.read_latest.return_value = (_make_frame(), 42)
        executor._shm_reader = mock_reader
        frame = executor.get_live_frame()
        assert frame is not None
        assert isinstance(frame, np.ndarray)

    def test_shm_bgra_to_rgb_conversion(self, executor):
        """BGRA frames from SHM should be converted to RGB."""
        bgra = np.zeros((48, 64, 4), dtype=np.uint8)
        bgra[:, :, 0] = 255  # B channel
        bgra[:, :, 3] = 255  # A channel
        mock_reader = MagicMock()
        mock_reader.read_latest.return_value = (bgra, 43)
        executor._shm_reader = mock_reader
        frame = executor.get_live_frame()
        assert frame is not None
        assert frame.shape[2] == 3  # RGB, not BGRA
        # Blue channel should now be in position 2 (RGB format)
        assert frame[0, 0, 2] == 255


# ---------------------------------------------------------------------------
# Test wait-for-condition
# ---------------------------------------------------------------------------

class TestWaitForCondition:
    @pytest.mark.asyncio
    async def test_wait_step(self, executor):
        """Wait steps should return direct success after sleeping."""
        step = CUStep(action="wait", value="1", description="wait 1 sec")
        # Patch sleep to be instant
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await executor.execute_step(step, frame=_make_frame(), step_index=0)
        assert result.success is True
        assert result.layer_used == "direct"


# ---------------------------------------------------------------------------
# Test error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_action_exception_caught(self, executor):
        """If _execute_action_impl raises, result should capture the error."""
        step = CUStep(action="type", value="x", description="type x")
        with patch(
            "backend.vision.cu_step_executor._execute_action_impl",
            side_effect=RuntimeError("pyautogui failed"),
        ):
            result = await executor.execute_step(step, frame=_make_frame(), step_index=0)
        assert result.success is False
        assert "pyautogui failed" in (result.error or "")

    @pytest.mark.asyncio
    async def test_all_layers_fail(self, executor):
        """When all vision layers fail, return success=False, layer_used='none'."""
        executor._ax_resolver = AsyncMock()
        executor._ax_resolver.resolve = AsyncMock(return_value=None)
        # No DW or Claude keys
        step = CUStep(
            action="click", target="Nonexistent",
            app_name="Nothing", description="click nothing",
        )
        result = await executor.execute_step(step, frame=_make_frame(), step_index=0)
        assert result.success is False
        assert result.layer_used == "none"

    @pytest.mark.asyncio
    async def test_accessibility_exception_handled(self, executor):
        """If accessibility resolver throws, cascade continues to next layer."""
        mock_resolver = AsyncMock()
        mock_resolver.resolve = AsyncMock(side_effect=Exception("AX crash"))
        executor._ax_resolver = mock_resolver
        # No DW/Claude keys => falls through all layers
        step = CUStep(
            action="click", target="Button",
            app_name="App", description="click button",
        )
        result = await executor.execute_step(step, frame=_make_frame(), step_index=0)
        assert result.success is False
        # Should not have crashed
        assert result.layer_used == "none"


# ---------------------------------------------------------------------------
# Test scroll action
# ---------------------------------------------------------------------------

class TestScrollAction:
    @pytest.mark.asyncio
    async def test_scroll_with_target(self, executor_with_accessibility):
        """Scroll at a target location uses accessibility to find coords."""
        step = CUStep(
            action="scroll", target="Content area", value="-3",
            app_name="Chrome", description="scroll down",
        )
        with patch("backend.vision.cu_step_executor._execute_action_impl") as mock_exec:
            mock_exec.return_value = None
            result = await executor_with_accessibility.execute_step(
                step, frame=_make_frame(), step_index=0,
            )
        assert result.success is True
        assert result.coords == (500, 300)

    @pytest.mark.asyncio
    async def test_scroll_without_target(self, executor):
        """Scroll without target -> direct execution at current position."""
        step = CUStep(action="scroll", value="-5", description="scroll down")
        with patch("backend.vision.cu_step_executor._execute_action_impl") as mock_exec:
            mock_exec.return_value = None
            result = await executor.execute_step(step, frame=_make_frame(), step_index=0)
        assert result.success is True
        assert result.layer_used == "direct"
