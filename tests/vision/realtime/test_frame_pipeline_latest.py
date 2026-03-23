import asyncio
import numpy as np
import pytest
from backend.vision.realtime.frame_pipeline import FramePipeline, FrameData


@pytest.fixture
def pipeline():
    return FramePipeline(use_sck=False, motion_detect=False)


def _make_frame(n: int) -> FrameData:
    return FrameData(
        data=np.zeros((100, 100, 3), dtype=np.uint8),
        width=100, height=100,
        timestamp=float(n), frame_number=n,
    )


def test_latest_frame_is_none_initially(pipeline):
    assert pipeline.latest_frame is None


def test_latest_frame_updates_on_enqueue(pipeline):
    frame = _make_frame(1)
    pipeline._enqueue_frame(frame)
    assert pipeline.latest_frame is frame
    assert pipeline.latest_frame.frame_number == 1


def test_latest_frame_is_most_recent(pipeline):
    pipeline._enqueue_frame(_make_frame(1))
    pipeline._enqueue_frame(_make_frame(2))
    pipeline._enqueue_frame(_make_frame(3))
    assert pipeline.latest_frame.frame_number == 3


@pytest.mark.asyncio
async def test_latest_frame_survives_queue_drain(pipeline):
    """latest_frame persists even after get_frame() drains the queue."""
    frame = _make_frame(42)
    pipeline._enqueue_frame(frame)
    got = await pipeline.get_frame(timeout_s=0.1)
    assert got is frame
    assert pipeline.latest_frame is frame


@pytest.mark.asyncio
async def test_no_contention_with_get_frame(pipeline):
    """VisionCortex reads latest_frame while VisionActionLoop uses get_frame."""
    for i in range(5):
        pipeline._enqueue_frame(_make_frame(i))
    latest = pipeline.latest_frame
    assert latest.frame_number == 4
    frames = []
    for _ in range(5):
        f = await pipeline.get_frame(timeout_s=0.1)
        assert f is not None
        frames.append(f)
    assert len(frames) == 5
    assert pipeline.latest_frame.frame_number == 4
