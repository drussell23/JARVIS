"""Tests for VisionCortex core class — Task 4 (singleton, ActivityLevel, adaptive throttle)."""
import asyncio
import pytest
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch

from backend.vision.realtime.vision_cortex import VisionCortex, ActivityLevel


@pytest.fixture(autouse=True)
def clear_singleton():
    """Ensure clean singleton state between tests."""
    VisionCortex.set_instance(None)
    yield
    VisionCortex.set_instance(None)


def test_singleton_initially_none():
    assert VisionCortex.get_instance() is None


def test_activity_level_default():
    cortex = VisionCortex()
    assert cortex.activity_level == ActivityLevel.NORMAL


def test_compute_interval_normal():
    cortex = VisionCortex()
    cortex._activity_level = ActivityLevel.NORMAL
    assert cortex.perception_interval == 3.0


def test_compute_interval_idle():
    cortex = VisionCortex()
    cortex._activity_level = ActivityLevel.IDLE
    assert cortex.perception_interval == 8.0


def test_compute_interval_high():
    cortex = VisionCortex()
    cortex._activity_level = ActivityLevel.HIGH
    assert cortex.perception_interval == 1.0


def test_compute_activity_rate_empty():
    cortex = VisionCortex()
    assert cortex._compute_activity_rate() == 0.0


def test_compute_activity_rate_with_changes():
    import time
    cortex = VisionCortex()
    now = time.monotonic()
    for i in range(10):
        cortex._change_history.append((now - 60 + i * 6, True))
    for i in range(50):
        cortex._change_history.append((now - 60 + i * 1.2, False))
    rate = cortex._compute_activity_rate()
    assert 0.1 < rate < 0.3


def test_update_activity_level_from_rate():
    cortex = VisionCortex()
    cortex._change_history.clear()
    cortex._update_activity_level()
    assert cortex._activity_level == ActivityLevel.IDLE


@pytest.mark.asyncio
async def test_awaken_and_shutdown():
    cortex = VisionCortex()
    with patch.object(cortex, '_discover_subsystems', new_callable=AsyncMock):
        with patch.object(cortex, '_start_perception_loop'):
            with patch.object(cortex, '_start_monitor', new_callable=AsyncMock):
                await cortex.awaken()
                assert cortex.is_awake
                await cortex.shutdown()
                assert not cortex.is_awake


@pytest.mark.asyncio
async def test_awaken_clears_singleton_on_shutdown():
    cortex = VisionCortex()
    assert VisionCortex.get_instance() is cortex
    with patch.object(cortex, '_discover_subsystems', new_callable=AsyncMock):
        with patch.object(cortex, '_start_perception_loop'):
            with patch.object(cortex, '_start_monitor', new_callable=AsyncMock):
                await cortex.awaken()
    await cortex.shutdown()
    assert VisionCortex.get_instance() is None


@pytest.mark.asyncio
async def test_perception_loop_reads_latest_frame():
    """Verify perception loop uses latest_frame (non-destructive) not get_frame."""
    from backend.vision.realtime.frame_pipeline import FrameData
    cortex = VisionCortex()

    mock_frame = FrameData(
        data=np.zeros((100, 100, 3), dtype=np.uint8),
        width=100, height=100, timestamp=1.0, frame_number=1,
    )
    mock_pipeline = MagicMock()
    mock_pipeline.latest_frame = mock_frame
    cortex._frame_pipeline = mock_pipeline

    mock_analyzer = MagicMock()
    mock_analyzer.inject_frame = AsyncMock()
    cortex._analyzer = mock_analyzer
    cortex._running = True

    await cortex._run_one_perception_cycle()

    mock_analyzer.inject_frame.assert_called_once()
    mock_pipeline.get_frame.assert_not_called()
