"""
End-to-end integration tests for JARVIS-CU.
Tests the full pipeline: goal -> plan -> cascade -> execute -> verify.
"""
import asyncio
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.asyncio
async def test_whatsapp_scenario_mocked():
    """Full WhatsApp scenario with mocked Claude + Doubleword."""
    from backend.vision.cu_task_planner import CUStep
    from backend.vision.jarvis_cu import JarvisCU
    from backend.vision.cu_step_executor import StepResult

    mock_planner = AsyncMock()
    mock_planner.plan_goal.return_value = [
        CUStep(index=0, action="hotkey", description="Open Spotlight", keys=["cmd", "space"]),
        CUStep(index=1, action="type", description="Type WhatsApp", text="WhatsApp"),
        CUStep(index=2, action="key", description="Launch", key="return"),
        CUStep(index=3, action="wait", description="Wait for WhatsApp", condition="app_visible", app="WhatsApp"),
        CUStep(index=4, action="type", description="Search Zach", text="Zach", target="search_field"),
        CUStep(index=5, action="click", description="Click Zach's chat", target="Zach's conversation"),
        CUStep(index=6, action="type", description="Type message", text="what's up!"),
        CUStep(index=7, action="key", description="Send message", key="return"),
    ]

    mock_executor = AsyncMock()

    def make_result(step, frame=None, step_index=0):
        layer = "direct"
        if getattr(step, "action", "") == "click" and getattr(step, "target", None):
            layer = "doubleword"
        return StepResult(
            success=True,
            layer_used=layer,
            step_index=getattr(step, "index", step_index),
            coords=(340, 285) if layer == "doubleword" else None,
            confidence=0.92 if layer == "doubleword" else 1.0,
        )

    mock_executor.execute_step.side_effect = make_result
    mock_executor.get_live_frame.return_value = np.zeros((900, 1440, 3), dtype=np.uint8)

    cu = JarvisCU.__new__(JarvisCU)
    cu._planner = mock_planner
    cu._executor = mock_executor
    cu._shm = None
    cu._timeout_s = 120.0
    cu._max_retries = 1
    cu._step_delay_s = 0.0  # no delay in tests

    result = await cu.run("Open WhatsApp and send Zach 'what's up!'")

    assert result["success"] is True
    assert result["steps_completed"] == 8
    assert result["steps_total"] == 8
    assert "direct" in result["layers_used"]
    assert "doubleword" in result["layers_used"]


@pytest.mark.asyncio
async def test_vision_activator_lazy_start():
    """VisionActivator starts vision pipeline on first action command."""
    from backend.vision.vision_activator import VisionActivator

    activator = VisionActivator()
    assert activator.is_active is False

    # Patch the imports inside ensure_vision (they're lazy imports)
    mock_fp_instance = AsyncMock()
    mock_cu_instance = AsyncMock()
    mock_cu_instance.run.return_value = {
        "success": True,
        "steps_completed": 1,
        "steps_total": 1,
        "step_results": [],
        "elapsed_s": 0.5,
        "error": None,
        "layers_used": {"direct": 1},
    }
    mock_hub_instance = MagicMock()

    with patch.dict("sys.modules", {}):
        with patch(
            "backend.vision.realtime.frame_pipeline.FramePipeline",
            return_value=mock_fp_instance,
        ), patch(
            "backend.vision.jarvis_cu.JarvisCU",
            return_value=mock_cu_instance,
        ), patch(
            "backend.vision.intelligence.vision_intelligence_hub.VisionIntelligenceHub",
            return_value=mock_hub_instance,
        ):
            result = await activator.run_goal("Click the button")

    assert activator.is_active is True
    assert result["success"] is True


@pytest.mark.asyncio
async def test_layer_cascade_accessibility_first():
    """Verify accessibility API is tried before Doubleword."""
    from backend.vision.cu_step_executor import CUStepExecutor, StepResult
    from backend.vision.cu_task_planner import CUStep

    executor = CUStepExecutor.__new__(CUStepExecutor)
    executor._shm_reader = None
    executor._dw_api_key = "test-key"
    executor._anthropic_key = ""
    executor._ax_resolver = MagicMock()

    # Accessibility finds the element
    executor._resolve_accessibility = AsyncMock(
        return_value=((100, 200), 0.95)  # returns (coords, confidence) tuple
    )
    executor._execute_action = AsyncMock(return_value=True)
    executor._verify_with_frame = AsyncMock(return_value=True)
    executor._ask_doubleword_vision = AsyncMock()

    step = CUStep(index=0, action="click", target="Send button", description="Send")
    frame = np.zeros((900, 1440, 3), dtype=np.uint8)

    result = await executor.execute_step(step, frame)

    assert result.layer_used == "accessibility"
    executor._ask_doubleword_vision.assert_not_called()


@pytest.mark.asyncio
async def test_full_cascade_fallthrough():
    """When accessibility fails, Doubleword is tried. When Doubleword fails, Claude is tried."""
    from backend.vision.cu_step_executor import CUStepExecutor, StepResult
    from backend.vision.cu_task_planner import CUStep

    executor = CUStepExecutor.__new__(CUStepExecutor)
    executor._shm_reader = None
    executor._dw_api_key = "test-key"
    executor._anthropic_key = "test-key"
    executor._ax_resolver = MagicMock()

    # All layers fail except Claude
    executor._resolve_accessibility = AsyncMock(return_value=(None, 0.0))
    executor._ask_doubleword_vision = AsyncMock(return_value=None)
    executor._ask_claude_vision = AsyncMock(
        return_value={"x": 500, "y": 300, "confidence": 0.88}
    )
    executor._execute_action = AsyncMock(return_value=True)
    executor._verify_with_frame = AsyncMock(return_value=True)
    executor.get_live_frame = MagicMock(return_value=None)

    step = CUStep(index=0, action="click", target="Complex element", description="Click it")
    frame = np.zeros((900, 1440, 3), dtype=np.uint8)

    result = await executor.execute_step(step, frame)

    assert result.layer_used == "claude"
    assert result.coords == (500, 300)
