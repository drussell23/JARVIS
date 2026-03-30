import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from brainstem.vision_bridge import VisionBridge


def test_vision_bridge_starts_inactive():
    bridge = VisionBridge()
    assert not bridge.is_active
    assert bridge._frame_pipeline is None
    assert bridge._jarvis_cu is None


@pytest.mark.asyncio
async def test_activate_sets_active():
    bridge = VisionBridge()
    # Mock the backend imports
    with patch("brainstem.vision_bridge.VisionBridge._start_pipeline", new_callable=AsyncMock) as mock_start:
        mock_start.return_value = True
        await bridge.activate()
        assert bridge.is_active
        mock_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_deactivate_cleans_up():
    bridge = VisionBridge()
    bridge._active = True
    mock_pipeline = MagicMock()
    mock_pipeline.stop = AsyncMock()
    bridge._frame_pipeline = mock_pipeline
    await bridge.deactivate()
    assert not bridge.is_active
    mock_pipeline.stop.assert_awaited_once()
