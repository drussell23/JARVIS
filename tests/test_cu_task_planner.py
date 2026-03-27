"""
Tests for CU Task Planner — Claude Vision goal decomposition.

Covers:
- CUStep dataclass construction and validation
- needs_visual_grounding property logic
- _parse_steps conversion from raw dicts
- _frame_to_b64 encoding
- plan_goal end-to-end (Claude API mocked)
- _call_claude_vision prompt construction and JSON extraction (mocked)
- Edge cases: empty steps, markdown-wrapped JSON, malformed responses
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from dataclasses import asdict
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.vision.cu_task_planner import CUStep, CUTaskPlanner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def planner():
    """Create a CUTaskPlanner with a dummy API key."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key-000"}):
        return CUTaskPlanner()


@pytest.fixture
def sample_frame():
    """A small 100x100 RGB numpy array simulating a screenshot."""
    return np.zeros((100, 100, 3), dtype=np.uint8)


@pytest.fixture
def sample_steps_raw() -> List[Dict[str, Any]]:
    """Raw step dicts as Claude would return them."""
    return [
        {
            "action": "hotkey",
            "description": "Open Spotlight search",
            "keys": ["command", "space"],
        },
        {
            "action": "type",
            "description": "Type 'WhatsApp' into Spotlight",
            "text": "WhatsApp",
        },
        {
            "action": "key",
            "description": "Press Return to launch WhatsApp",
            "key": "Return",
        },
        {
            "action": "wait",
            "description": "Wait for WhatsApp to open",
            "condition": "WhatsApp main window is visible",
            "app": "WhatsApp",
        },
        {
            "action": "click",
            "description": "Click the search bar in WhatsApp",
            "target": "search bar at top of contacts list",
        },
        {
            "action": "type",
            "description": "Search for Zach",
            "text": "Zach",
        },
        {
            "action": "click",
            "description": "Click on Zach's conversation",
            "target": "Zach's name in search results",
        },
        {
            "action": "click",
            "description": "Click the message input field",
            "target": "message input field at bottom of chat",
        },
        {
            "action": "type",
            "description": "Type the message",
            "text": "what's up!",
        },
        {
            "action": "key",
            "description": "Send the message",
            "key": "Return",
        },
    ]


# ---------------------------------------------------------------------------
# CUStep dataclass tests
# ---------------------------------------------------------------------------

class TestCUStep:
    """Test CUStep dataclass construction and properties."""

    def test_minimal_step(self):
        """Step with only required fields."""
        step = CUStep(index=0, action="click", description="Click button")
        assert step.index == 0
        assert step.action == "click"
        assert step.description == "Click button"
        assert step.target is None
        assert step.text is None
        assert step.keys is None
        assert step.key is None
        assert step.condition is None
        assert step.app is None
        assert step.direction is None
        assert step.amount is None

    def test_click_step_with_target(self):
        step = CUStep(
            index=0,
            action="click",
            description="Click search bar",
            target="search bar at top",
        )
        assert step.target == "search bar at top"

    def test_type_step_with_text(self):
        step = CUStep(
            index=1,
            action="type",
            description="Type greeting",
            text="hello world",
        )
        assert step.text == "hello world"

    def test_hotkey_step_with_keys(self):
        step = CUStep(
            index=2,
            action="hotkey",
            description="Open Spotlight",
            keys=["command", "space"],
        )
        assert step.keys == ["command", "space"]

    def test_key_step(self):
        step = CUStep(
            index=3,
            action="key",
            description="Press Return",
            key="Return",
        )
        assert step.key == "Return"

    def test_scroll_step(self):
        step = CUStep(
            index=4,
            action="scroll",
            description="Scroll down",
            direction="down",
            amount=3,
        )
        assert step.direction == "down"
        assert step.amount == 3

    def test_wait_step(self):
        step = CUStep(
            index=5,
            action="wait",
            description="Wait for app",
            condition="App is visible",
            app="WhatsApp",
        )
        assert step.condition == "App is visible"
        assert step.app == "WhatsApp"

    def test_asdict_roundtrip(self):
        """Ensure dataclass serializes cleanly."""
        step = CUStep(
            index=0,
            action="click",
            description="Click target",
            target="the button",
        )
        d = asdict(step)
        assert d["index"] == 0
        assert d["action"] == "click"
        assert d["target"] == "the button"


# ---------------------------------------------------------------------------
# needs_visual_grounding property
# ---------------------------------------------------------------------------

class TestNeedsVisualGrounding:
    """Test the needs_visual_grounding property for various step types."""

    def test_click_with_target_needs_grounding(self):
        step = CUStep(index=0, action="click", description="Click X", target="the X button")
        assert step.needs_visual_grounding is True

    def test_click_without_target_no_grounding(self):
        step = CUStep(index=0, action="click", description="Click at known pos")
        assert step.needs_visual_grounding is False

    def test_type_with_target_needs_grounding(self):
        step = CUStep(index=0, action="type", description="Type in field", target="input field", text="hello")
        assert step.needs_visual_grounding is True

    def test_type_without_target_no_grounding(self):
        step = CUStep(index=0, action="type", description="Type text", text="hello")
        assert step.needs_visual_grounding is False

    def test_wait_with_app_needs_grounding(self):
        step = CUStep(index=0, action="wait", description="Wait for app", condition="visible", app="Safari")
        assert step.needs_visual_grounding is True

    def test_wait_without_app_no_grounding(self):
        step = CUStep(index=0, action="wait", description="Wait 2 seconds")
        assert step.needs_visual_grounding is False

    def test_hotkey_no_grounding(self):
        step = CUStep(index=0, action="hotkey", description="Press keys", keys=["cmd", "c"])
        assert step.needs_visual_grounding is False

    def test_key_no_grounding(self):
        step = CUStep(index=0, action="key", description="Press Return", key="Return")
        assert step.needs_visual_grounding is False

    def test_scroll_no_grounding(self):
        step = CUStep(index=0, action="scroll", description="Scroll down", direction="down", amount=3)
        assert step.needs_visual_grounding is False


# ---------------------------------------------------------------------------
# _parse_steps
# ---------------------------------------------------------------------------

class TestParseSteps:
    """Test conversion from raw dicts to CUStep objects."""

    def test_parse_full_plan(self, planner, sample_steps_raw):
        steps = planner._parse_steps(sample_steps_raw)
        assert len(steps) == 10
        # Indices are assigned sequentially
        for i, step in enumerate(steps):
            assert step.index == i
            assert isinstance(step, CUStep)

    def test_parse_preserves_action(self, planner):
        raw = [{"action": "click", "description": "Click X", "target": "button"}]
        steps = planner._parse_steps(raw)
        assert steps[0].action == "click"
        assert steps[0].target == "button"

    def test_parse_preserves_keys_list(self, planner):
        raw = [{"action": "hotkey", "description": "Hotkey", "keys": ["cmd", "v"]}]
        steps = planner._parse_steps(raw)
        assert steps[0].keys == ["cmd", "v"]

    def test_parse_empty_list(self, planner):
        steps = planner._parse_steps([])
        assert steps == []

    def test_parse_ignores_unknown_fields(self, planner):
        raw = [{"action": "click", "description": "Click", "extra_field": "ignored"}]
        steps = planner._parse_steps(raw)
        assert len(steps) == 1
        assert steps[0].action == "click"
        assert not hasattr(steps[0], "extra_field") or "extra_field" not in asdict(steps[0])

    def test_parse_missing_optional_fields(self, planner):
        raw = [{"action": "click", "description": "Click something"}]
        steps = planner._parse_steps(raw)
        assert steps[0].target is None
        assert steps[0].text is None

    def test_parse_scroll_with_amount(self, planner):
        raw = [{"action": "scroll", "description": "Scroll down", "direction": "down", "amount": 5}]
        steps = planner._parse_steps(raw)
        assert steps[0].direction == "down"
        assert steps[0].amount == 5


# ---------------------------------------------------------------------------
# _frame_to_b64
# ---------------------------------------------------------------------------

class TestFrameToB64:
    """Test numpy frame to base64 JPEG conversion."""

    def test_returns_valid_base64(self, planner, sample_frame):
        b64 = planner._frame_to_b64(sample_frame)
        # Must be a non-empty string
        assert isinstance(b64, str)
        assert len(b64) > 0
        # Must decode without error
        decoded = base64.b64decode(b64)
        # JPEG magic bytes
        assert decoded[:2] == b"\xff\xd8"

    def test_handles_large_frame(self, planner):
        """Large frames should be resized and still produce valid JPEG."""
        large_frame = np.zeros((4000, 6000, 3), dtype=np.uint8)
        b64 = planner._frame_to_b64(large_frame)
        decoded = base64.b64decode(b64)
        assert decoded[:2] == b"\xff\xd8"

    def test_handles_grayscale_frame(self, planner):
        """Grayscale 2D arrays should be handled gracefully."""
        gray_frame = np.zeros((100, 100), dtype=np.uint8)
        b64 = planner._frame_to_b64(gray_frame)
        decoded = base64.b64decode(b64)
        assert decoded[:2] == b"\xff\xd8"

    def test_jpeg_quality_env_var(self, sample_frame):
        """JPEG quality should be configurable via env var."""
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-test",
            "JARVIS_CU_PLANNER_JPEG_QUALITY": "30",
        }):
            planner = CUTaskPlanner()
            b64_low = planner._frame_to_b64(sample_frame)

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-test",
            "JARVIS_CU_PLANNER_JPEG_QUALITY": "95",
        }):
            planner = CUTaskPlanner()
            b64_high = planner._frame_to_b64(sample_frame)

        # Higher quality should produce larger base64
        assert len(b64_high) >= len(b64_low)


# ---------------------------------------------------------------------------
# _call_claude_vision (mocked API)
# ---------------------------------------------------------------------------

class TestCallClaudeVision:
    """Test the Claude Vision API call with mocked responses."""

    @pytest.mark.asyncio
    async def test_returns_parsed_steps(self, planner, sample_frame, sample_steps_raw):
        """Mocked Claude response should be parsed into step dicts."""
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(type="text", text=json.dumps(sample_steps_raw))
        ]

        planner._client = MagicMock()
        planner._client.messages.create = AsyncMock(return_value=mock_response)

        result = await planner._call_claude_vision("Open WhatsApp", sample_frame)
        assert isinstance(result, list)
        assert len(result) == 10
        assert result[0]["action"] == "hotkey"

    @pytest.mark.asyncio
    async def test_handles_markdown_wrapped_json(self, planner, sample_frame):
        """Claude sometimes wraps JSON in ```json ... ``` blocks."""
        steps = [{"action": "click", "description": "Click button", "target": "OK"}]
        wrapped = f"```json\n{json.dumps(steps)}\n```"

        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text=wrapped)]

        planner._client = MagicMock()
        planner._client.messages.create = AsyncMock(return_value=mock_response)

        result = await planner._call_claude_vision("Click OK", sample_frame)
        assert len(result) == 1
        assert result[0]["action"] == "click"

    @pytest.mark.asyncio
    async def test_handles_markdown_no_lang_tag(self, planner, sample_frame):
        """Handle ``` blocks without a language tag."""
        steps = [{"action": "key", "description": "Press Escape", "key": "Escape"}]
        wrapped = f"```\n{json.dumps(steps)}\n```"

        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text=wrapped)]

        planner._client = MagicMock()
        planner._client.messages.create = AsyncMock(return_value=mock_response)

        result = await planner._call_claude_vision("Press Escape", sample_frame)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_sends_image_in_request(self, planner, sample_frame):
        """Verify the API call includes the image as base64 JPEG."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="[]")]

        planner._client = MagicMock()
        planner._client.messages.create = AsyncMock(return_value=mock_response)

        await planner._call_claude_vision("Do something", sample_frame)

        call_kwargs = planner._client.messages.create.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        assert messages is not None
        # First message should contain image content
        user_msg = messages[0]
        assert user_msg["role"] == "user"
        # Should have both image and text content blocks
        content_types = [block["type"] for block in user_msg["content"]]
        assert "image" in content_types
        assert "text" in content_types

    @pytest.mark.asyncio
    async def test_uses_configured_model(self, sample_frame):
        """Model should come from env var."""
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-test",
            "JARVIS_CU_PLANNER_MODEL": "claude-test-model",
        }):
            planner = CUTaskPlanner()

        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="[]")]
        planner._client = MagicMock()
        planner._client.messages.create = AsyncMock(return_value=mock_response)

        await planner._call_claude_vision("test", sample_frame)

        call_kwargs = planner._client.messages.create.call_args
        model = call_kwargs.kwargs.get("model") or call_kwargs[1].get("model")
        assert model == "claude-test-model"


# ---------------------------------------------------------------------------
# plan_goal (end-to-end with mocked Claude)
# ---------------------------------------------------------------------------

class TestPlanGoal:
    """Integration test: plan_goal -> _call_claude_vision -> _parse_steps."""

    @pytest.mark.asyncio
    async def test_returns_cu_steps(self, planner, sample_frame, sample_steps_raw):
        """plan_goal should return a list of CUStep objects."""
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(type="text", text=json.dumps(sample_steps_raw))
        ]

        planner._client = MagicMock()
        planner._client.messages.create = AsyncMock(return_value=mock_response)

        steps = await planner.plan_goal("Open WhatsApp and send Zach 'what's up!'", sample_frame)

        assert isinstance(steps, list)
        assert all(isinstance(s, CUStep) for s in steps)
        assert len(steps) == 10
        # First step: hotkey to open Spotlight
        assert steps[0].action == "hotkey"
        assert steps[0].keys == ["command", "space"]
        # Last step: press Return to send
        assert steps[-1].action == "key"
        assert steps[-1].key == "Return"

    @pytest.mark.asyncio
    async def test_empty_plan(self, planner, sample_frame):
        """Empty response should return empty list."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="[]")]

        planner._client = MagicMock()
        planner._client.messages.create = AsyncMock(return_value=mock_response)

        steps = await planner.plan_goal("Do nothing", sample_frame)
        assert steps == []

    @pytest.mark.asyncio
    async def test_api_error_raises(self, planner, sample_frame):
        """API errors should propagate so callers can handle them."""
        planner._client = MagicMock()
        planner._client.messages.create = AsyncMock(
            side_effect=Exception("API rate limit")
        )

        with pytest.raises(Exception, match="API rate limit"):
            await planner.plan_goal("Fail", sample_frame)

    @pytest.mark.asyncio
    async def test_indices_are_sequential(self, planner, sample_frame):
        """Step indices must be 0, 1, 2, ... regardless of raw data."""
        raw = [
            {"action": "click", "description": "A"},
            {"action": "type", "description": "B", "text": "x"},
            {"action": "key", "description": "C", "key": "Return"},
        ]
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text=json.dumps(raw))]

        planner._client = MagicMock()
        planner._client.messages.create = AsyncMock(return_value=mock_response)

        steps = await planner.plan_goal("3 steps", sample_frame)
        assert [s.index for s in steps] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_visual_grounding_flags(self, planner, sample_frame):
        """Verify grounding flags are correct on parsed plan."""
        raw = [
            {"action": "click", "description": "Click target", "target": "button"},
            {"action": "type", "description": "Type text", "text": "hello"},
            {"action": "wait", "description": "Wait", "condition": "visible", "app": "Safari"},
        ]
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text=json.dumps(raw))]

        planner._client = MagicMock()
        planner._client.messages.create = AsyncMock(return_value=mock_response)

        steps = await planner.plan_goal("test grounding", sample_frame)
        assert steps[0].needs_visual_grounding is True   # click + target
        assert steps[1].needs_visual_grounding is False   # type without target
        assert steps[2].needs_visual_grounding is True    # wait + app


# ---------------------------------------------------------------------------
# Environment variable configuration
# ---------------------------------------------------------------------------

class TestConfiguration:
    """Test that env vars properly configure the planner."""

    def test_default_model(self, planner):
        assert planner._model == "claude-sonnet-4-6-20250514"

    def test_default_max_tokens(self, planner):
        assert planner._max_tokens == 2048

    def test_default_jpeg_quality(self, planner):
        assert planner._jpeg_quality == 80

    def test_custom_model(self):
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-test",
            "JARVIS_CU_PLANNER_MODEL": "claude-opus-4-20250514",
        }):
            p = CUTaskPlanner()
            assert p._model == "claude-opus-4-20250514"

    def test_custom_max_tokens(self):
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-test",
            "JARVIS_CU_PLANNER_MAX_TOKENS": "4096",
        }):
            p = CUTaskPlanner()
            assert p._max_tokens == 4096

    def test_custom_jpeg_quality(self):
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-test",
            "JARVIS_CU_PLANNER_JPEG_QUALITY": "50",
        }):
            p = CUTaskPlanner()
            assert p._jpeg_quality == 50
