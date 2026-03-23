import asyncio
import pytest
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image

from backend.vision.continuous_screen_analyzer import MemoryAwareScreenAnalyzer


@pytest.fixture
def mock_vision_handler():
    handler = MagicMock()
    handler.capture_screen = AsyncMock(return_value=None)
    handler.describe_screen = AsyncMock(return_value={"description": "test"})
    handler.analyze_screen = AsyncMock(return_value={"analysis": "test"})
    return handler


@pytest.fixture
def analyzer(mock_vision_handler):
    return MemoryAwareScreenAnalyzer(mock_vision_handler)


def test_inject_frame_method_exists(analyzer):
    assert hasattr(analyzer, 'inject_frame')
    assert asyncio.iscoroutinefunction(analyzer.inject_frame)


@pytest.mark.asyncio
async def test_inject_frame_skips_capture(analyzer, mock_vision_handler):
    """inject_frame must NOT call vision_handler.capture_screen."""
    img = Image.fromarray(np.zeros((100, 100, 3), dtype=np.uint8))
    await analyzer.inject_frame(img, 1234567890.0)
    mock_vision_handler.capture_screen.assert_not_called()


@pytest.mark.asyncio
async def test_inject_frame_runs_phase1_fingerprinting(analyzer):
    """inject_frame should produce a screen_captured event."""
    events_fired = []
    # Keep a strong reference to the lambda — _CallbackSet uses weakref.ref,
    # so a lambda without a named binding is immediately garbage-collected.
    cb = lambda data: events_fired.append(data)
    analyzer.event_callbacks['screen_captured'].add(cb)
    img = Image.fromarray(np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8))
    await analyzer.inject_frame(img, 1234567890.0)
    assert len(events_fired) >= 1


@pytest.mark.asyncio
async def test_inject_frame_detects_app_change(analyzer):
    """inject_frame should detect app changes via _quick_screen_analysis."""
    events_fired = []
    # Keep a strong reference to the lambda — _CallbackSet uses weakref.ref.
    cb = lambda data: events_fired.append(data)
    analyzer.event_callbacks['app_changed'].add(cb)
    analyzer.current_screen_state['quick_app'] = 'Safari'
    img = Image.fromarray(np.zeros((100, 100, 3), dtype=np.uint8))
    with patch.object(analyzer, '_quick_screen_analysis', new_callable=AsyncMock,
                      return_value={'current_app': 'Terminal'}):
        await analyzer.inject_frame(img, 1234567890.0)
    assert any(e.get('app_name') == 'Terminal' for e in events_fired)
