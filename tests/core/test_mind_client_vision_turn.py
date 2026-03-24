import asyncio
import base64
import pytest
import numpy as np
from unittest.mock import AsyncMock, MagicMock
from PIL import Image


def test_compress_frame_jpeg():
    """Frame compression should produce JPEG under max_bytes."""
    from backend.core.mind_client import MindClient
    client = MindClient.__new__(MindClient)
    img = Image.fromarray(np.random.randint(0, 255, (1800, 2880, 3), dtype=np.uint8))
    result = client._compress_frame_jpeg(img, quality=85, max_bytes=500000)
    assert result["content_type"] == "image/jpeg"
    assert len(result["data"]) > 0
    assert "sha256" in result
    raw = base64.b64decode(result["data"])
    assert len(raw) <= 500000


def test_compress_frame_jpeg_downscales_if_too_large():
    """If JPEG exceeds max_bytes, it should downscale and retry."""
    from backend.core.mind_client import MindClient
    client = MindClient.__new__(MindClient)
    img = Image.fromarray(np.random.randint(0, 255, (1800, 2880, 3), dtype=np.uint8))
    result = client._compress_frame_jpeg(img, quality=85, max_bytes=50000)
    assert result["width"] < 2880


@pytest.mark.asyncio
async def test_reason_vision_turn_builds_v1_payload():
    """reason_vision_turn should POST vision.loop.v1 schema."""
    from backend.core.mind_client import MindClient
    client = MindClient.__new__(MindClient)
    client._session_id = "test-session"
    client._level = MagicMock()
    client._level.value = 0
    client._circuit = MagicMock()
    client._circuit.can_execute.return_value = True

    captured_payload = {}

    async def mock_post(path, data=None, timeout=None):
        captured_payload.update(data)
        return {
            "schema": "vision.loop.v1",
            "goal_achieved": True,
            "stop_reason": "goal_satisfied",
            "next_action": None,
            "reasoning": "Done",
            "confidence": 0.95,
            "scene_summary": "test",
        }

    client._http_post = mock_post
    client._record_success = MagicMock()
    client._circuit.record_success = MagicMock()

    result = await client.reason_vision_turn(
        request_id="req-123",
        session_id="sess-456",
        goal="click the button",
        action_log=[],
        frame_jpeg_b64="abc123",
        frame_dims={"width": 1440, "height": 900, "scale": 2.0},
        allowed_action_types=["click", "type", "scroll"],
    )

    assert captured_payload["schema"] == "vision.loop.v1"
    assert captured_payload["goal"] == "click the button"
    assert result["goal_achieved"] is True


@pytest.mark.asyncio
async def test_reason_vision_turn_validates_response():
    """Malformed response should return error-shaped dict, not crash."""
    from backend.core.mind_client import MindClient
    client = MindClient.__new__(MindClient)
    client._session_id = "test"
    client._level = MagicMock()
    client._level.value = 0
    client._circuit = MagicMock()
    client._circuit.can_execute.return_value = True

    async def mock_post(path, data=None, timeout=None):
        return {"schema": "vision.loop.v1", "garbage": True}

    client._http_post = mock_post
    client._record_failure = MagicMock()
    client._circuit.record_failure = MagicMock()

    result = await client.reason_vision_turn(
        request_id="req-123",
        session_id="sess-456",
        goal="test",
        action_log=[],
        frame_jpeg_b64="abc",
        frame_dims={"width": 100, "height": 100, "scale": 1.0},
        allowed_action_types=["click"],
    )
    assert result.get("goal_achieved") is False
    assert result.get("stop_reason") == "error"
