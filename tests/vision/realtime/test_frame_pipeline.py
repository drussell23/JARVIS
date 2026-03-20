"""Tests for frame pipeline with motion detection."""
import asyncio
import time
import pytest
import numpy as np
from backend.vision.realtime.frame_pipeline import (
    MotionDetector,
    FramePipeline,
    FrameData,
)


class TestMotionDetector:
    def test_detects_change(self):
        detector = MotionDetector()
        frame_a = np.zeros((100, 100, 3), dtype=np.uint8)
        frame_b = np.ones((100, 100, 3), dtype=np.uint8) * 255
        assert detector.detect_change(frame_a) is True  # first frame always "changed"
        assert detector.detect_change(frame_b) is True  # drastically different

    def test_ignores_identical(self):
        detector = MotionDetector()
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        detector.detect_change(frame)  # first
        assert detector.detect_change(frame.copy()) is False  # identical

    def test_threshold_configurable(self):
        detector = MotionDetector(threshold=0.0)  # any change triggers
        frame_a = np.zeros((100, 100, 3), dtype=np.uint8)
        frame_b = frame_a.copy()
        frame_b[0, 0] = [1, 1, 1]  # tiny change
        detector.detect_change(frame_a)
        assert detector.detect_change(frame_b) is True

    def test_debounce_suppresses_rapid(self):
        detector = MotionDetector(debounce_ms=200)
        frame_a = np.zeros((100, 100, 3), dtype=np.uint8)
        frame_b = np.ones((100, 100, 3), dtype=np.uint8) * 255
        detector.detect_change(frame_a)
        # Change within debounce window should be suppressed
        assert detector.detect_change(frame_b) is False  # within 200ms


class TestFrameData:
    def test_frame_data_fields(self):
        frame = FrameData(
            data=np.zeros((100, 100, 3), dtype=np.uint8),
            width=100,
            height=100,
            timestamp=time.time(),
            frame_number=1,
        )
        assert frame.width == 100
        assert frame.frame_number == 1


class TestFramePipeline:
    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self):
        pipeline = FramePipeline(use_sck=False)  # mock capture
        await pipeline.start()
        assert pipeline.is_running
        await pipeline.stop()
        assert not pipeline.is_running

    @pytest.mark.asyncio
    async def test_get_frame_returns_frame_data(self):
        pipeline = FramePipeline(use_sck=False)
        # Inject a mock frame
        frame = FrameData(
            data=np.zeros((100, 100, 3), dtype=np.uint8),
            width=100, height=100,
            timestamp=time.time(), frame_number=1,
        )
        await pipeline._frame_queue.put(frame)
        result = await asyncio.wait_for(pipeline.get_frame(), timeout=1.0)
        assert result is not None
        assert result.width == 100

    @pytest.mark.asyncio
    async def test_bounded_queue_drops_oldest(self):
        pipeline = FramePipeline(use_sck=False, max_queue_size=3)
        # Push 5 frames into queue of size 3
        for i in range(5):
            frame = FrameData(
                data=np.zeros((10, 10, 3), dtype=np.uint8),
                width=10, height=10,
                timestamp=time.time(), frame_number=i,
            )
            pipeline._enqueue_frame(frame)
        # Queue should have at most 3 frames
        assert pipeline._frame_queue.qsize() <= 3
        # Oldest frames should have been dropped
        first = await pipeline.get_frame()
        assert first.frame_number >= 2  # frames 0, 1 dropped

    @pytest.mark.asyncio
    async def test_motion_filter_skips_unchanged(self):
        pipeline = FramePipeline(use_sck=False, motion_detect=True)
        frame = FrameData(
            data=np.zeros((100, 100, 3), dtype=np.uint8),
            width=100, height=100,
            timestamp=time.time(), frame_number=1,
        )
        # First frame passes
        assert pipeline._should_process(frame) is True
        # Identical frame does not pass
        frame2 = FrameData(
            data=np.zeros((100, 100, 3), dtype=np.uint8),
            width=100, height=100,
            timestamp=time.time(), frame_number=2,
        )
        assert pipeline._should_process(frame2) is False
