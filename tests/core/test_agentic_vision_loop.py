import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
import numpy as np

from backend.core.runtime_task_orchestrator import (
    ActionOutcome, VerifyTier, StopReason, RuntimeTaskOrchestrator,
)


def test_action_outcome_values():
    assert ActionOutcome.SUCCESS == "success"
    assert ActionOutcome.FAILURE == "failure"
    assert ActionOutcome.UNKNOWN == "unknown"


def test_verify_tier_values():
    assert VerifyTier.EXECUTOR == "executor"
    assert VerifyTier.FRAME_DELTA == "frame_delta"
    assert VerifyTier.MODEL_VERIFY == "model_verify"


def test_stop_reason_values():
    assert StopReason.GOAL_SATISFIED == "goal_satisfied"
    assert StopReason.STAGNATION == "stagnation"
    assert StopReason.MAX_TURNS == "max_turns"
    assert StopReason.MODEL_REFUSAL == "model_refusal"
    assert StopReason.ERROR == "error"


class _FakeAction:
    def __init__(self, action_type, target, text=None):
        self.action_type = action_type
        self.target = target
        self.text = text


def test_stagnation_detects_repeated_successful_action():
    log = [
        {"action_type": "click", "target": "search bar", "text": None, "result": "success", "frame_hash": "a1"},
        {"action_type": "click", "target": "search bar", "text": None, "result": "success", "frame_hash": "a2"},
        {"action_type": "click", "target": "search bar", "text": None, "result": "success", "frame_hash": "a3"},
    ]
    proposed = _FakeAction("click", "search bar")
    assert RuntimeTaskOrchestrator._is_stagnant_static(log, proposed, stagnation_window=3, frame_stagnation=3)


def test_stagnation_ignores_failed_repeats():
    log = [
        {"action_type": "click", "target": "button", "text": None, "result": "failure", "frame_hash": "a1"},
        {"action_type": "click", "target": "button", "text": None, "result": "failure", "frame_hash": "a2"},
        {"action_type": "click", "target": "button", "text": None, "result": "failure", "frame_hash": "a3"},
    ]
    proposed = _FakeAction("click", "button")
    assert not RuntimeTaskOrchestrator._is_stagnant_static(log, proposed, stagnation_window=3, frame_stagnation=3)


def test_stagnation_detects_frozen_frames():
    log = [
        {"action_type": "click", "target": "a", "text": None, "result": "success", "frame_hash": "same"},
        {"action_type": "type", "target": "b", "text": "x", "result": "success", "frame_hash": "same"},
        {"action_type": "scroll", "target": "c", "text": None, "result": "success", "frame_hash": "same"},
    ]
    proposed = _FakeAction("click", "d")
    assert RuntimeTaskOrchestrator._is_stagnant_static(log, proposed, stagnation_window=3, frame_stagnation=3)


def test_stagnation_no_false_positive_on_short_log():
    log = [
        {"action_type": "click", "target": "search bar", "text": None, "result": "success", "frame_hash": "a1"},
    ]
    proposed = _FakeAction("click", "search bar")
    assert not RuntimeTaskOrchestrator._is_stagnant_static(log, proposed, stagnation_window=3, frame_stagnation=3)


# ---------------------------------------------------------------------------
# Task 3: Agentic vision loop integration tests
# ---------------------------------------------------------------------------


def _make_mock_rto():
    """Create a RuntimeTaskOrchestrator with mocked dependencies."""
    rto = RuntimeTaskOrchestrator.__new__(RuntimeTaskOrchestrator)
    rto._prime = MagicMock()
    rto._resolve_url_via_prime = AsyncMock(return_value="https://example.com")
    rto.logger = MagicMock()

    # Mock VisionActionLoop
    mock_val = MagicMock()
    mock_val.execute_action = AsyncMock(return_value=MagicMock(
        success=True, verification_status="SUCCESS", confidence=0.9,
        tier_used="L2", coords=(100, 200), action_type="click",
        action_id="act-1", latency_ms=50, error=None,
    ))
    mock_pipeline = MagicMock()
    mock_frame = MagicMock()
    mock_frame.data = np.zeros((100, 100, 3), dtype=np.uint8)
    mock_frame.timestamp = 1234567890.0
    mock_frame.scale_factor = 1.0
    mock_pipeline.latest_frame = mock_frame
    mock_val.frame_pipeline = mock_pipeline
    rto._get_vision_action_loop = AsyncMock(return_value=mock_val)

    # Mock MindClient
    mock_mind = MagicMock()
    mock_mind.reason_vision_turn = AsyncMock(return_value={
        "schema": "vision.loop.v1",
        "goal_achieved": True,
        "stop_reason": "goal_satisfied",
        "next_action": None,
        "reasoning": "Goal achieved",
        "confidence": 0.95,
        "scene_summary": "Done",
    })
    mock_mind._compress_frame_jpeg = MagicMock(return_value={
        "data": "abc123", "content_type": "image/jpeg",
        "sha256": "test", "width": 100, "height": 100,
    })
    rto._get_mind_client = MagicMock(return_value=mock_mind)

    return rto


@pytest.mark.asyncio
async def test_agentic_loop_achieves_goal_in_one_turn():
    rto = _make_mock_rto()
    result = await rto._dispatch_to_vision("open LinkedIn", {"url": "https://linkedin.com"})
    assert result["success"] is True


@pytest.mark.asyncio
async def test_agentic_loop_multi_turn():
    rto = _make_mock_rto()
    mind = rto._get_mind_client()
    mind.reason_vision_turn = AsyncMock(side_effect=[
        {
            "schema": "vision.loop.v1",
            "goal_achieved": False,
            "stop_reason": None,
            "next_action": {
                "action_id": "act-1", "action_type": "click",
                "target": "search bar", "text": None,
                "coords": [245, 52], "settle_ms": 200, "requires_verify": True,
            },
            "reasoning": "Click search bar", "confidence": 0.9, "scene_summary": "Home page",
        },
        {
            "schema": "vision.loop.v1",
            "goal_achieved": True, "stop_reason": "goal_satisfied",
            "next_action": None, "reasoning": "Done", "confidence": 0.95,
        },
    ])
    result = await rto._dispatch_to_vision("search for NBA", {})
    assert result["success"] is True
    val = (await rto._get_vision_action_loop())
    assert val.execute_action.call_count == 1


@pytest.mark.asyncio
async def test_agentic_loop_stagnation_exit():
    rto = _make_mock_rto()
    mind = rto._get_mind_client()
    same_action = {
        "schema": "vision.loop.v1", "goal_achieved": False, "stop_reason": None,
        "next_action": {
            "action_id": "act-x", "action_type": "click", "target": "same button",
            "text": None, "coords": [100, 100], "settle_ms": 200, "requires_verify": False,
        },
        "reasoning": "Click same button again", "confidence": 0.8,
    }
    mind.reason_vision_turn = AsyncMock(return_value=same_action)
    result = await rto._dispatch_to_vision("impossible task", {})
    assert result.get("stop_reason") in ("stagnation", "max_turns")


@pytest.mark.asyncio
async def test_agentic_loop_degraded_no_val():
    rto = _make_mock_rto()
    rto._get_vision_action_loop = AsyncMock(return_value=None)
    result = await rto._dispatch_to_vision("open something", {"url": "https://example.com"})
    assert result["success"] is True


@pytest.mark.asyncio
async def test_agentic_loop_model_refusal():
    rto = _make_mock_rto()
    mind = rto._get_mind_client()
    mind.reason_vision_turn = AsyncMock(return_value={
        "schema": "vision.loop.v1",
        "goal_achieved": False, "next_action": None,
        "stop_reason": "model_refusal",
        "reasoning": "Cannot assist", "confidence": 0.0,
    })
    result = await rto._dispatch_to_vision("something refused", {})
    assert result["success"] is False
    assert result.get("stop_reason") == "model_refusal"


@pytest.mark.asyncio
async def test_agentic_loop_settle_ms_capped():
    rto = _make_mock_rto()
    mind = rto._get_mind_client()
    mind.reason_vision_turn = AsyncMock(side_effect=[
        {
            "schema": "vision.loop.v1", "goal_achieved": False, "stop_reason": None,
            "next_action": {
                "action_id": "act-1", "action_type": "click", "target": "button",
                "text": None, "coords": None, "settle_ms": 999999, "requires_verify": False,
            },
            "reasoning": "Click", "confidence": 0.9,
        },
        {
            "schema": "vision.loop.v1", "goal_achieved": True,
            "stop_reason": "goal_satisfied", "next_action": None,
            "reasoning": "Done", "confidence": 0.95,
        },
    ])
    with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
        await rto._dispatch_to_vision("test settle cap", {})
        # Find the settle sleep call (should be capped at 2.0 seconds = 2000ms)
        settle_calls = [c for c in mock_sleep.call_args_list if c[0][0] <= 2.1 and c[0][0] > 0.1]
        # At least one settle call should exist and be <= 2.0
        assert any(c[0][0] <= 2.0 for c in settle_calls)
