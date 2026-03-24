"""Integration smoke test: VisionCortex wires real subsystems together.

Verifies the full perception pipeline using real (not mocked) class
instances for VisionCortex, VisionActionLoop, and FramePipeline.
Only inject_frame on the analyzer is mocked to keep tests fast and
hermetic (avoid real screen capture / GCP calls).
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest
from unittest.mock import AsyncMock, patch

from backend.vision.realtime.vision_cortex import VisionCortex, ActivityLevel
from backend.vision.realtime.frame_pipeline import FramePipeline, FrameData
from backend.vision.realtime.vision_action_loop import VisionActionLoop


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_singletons():
    """Guarantee isolated singleton state for every test."""
    VisionCortex.set_instance(None)
    VisionActionLoop.set_instance(None)
    yield
    VisionCortex.set_instance(None)
    VisionActionLoop.set_instance(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_frame(n: int) -> FrameData:
    """Create a deterministic test frame with a unique frame_number."""
    return FrameData(
        data=np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8),
        width=100,
        height=100,
        timestamp=float(n),
        frame_number=n,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cortex_discovers_vision_action_loop():
    """VisionCortex._discover_subsystems must wire to VisionActionLoop.frame_pipeline."""
    val = VisionActionLoop(use_sck=False)
    assert VisionActionLoop.get_instance() is val

    cortex = VisionCortex()
    await cortex._discover_subsystems()

    # VisionCortex must have picked up the same FramePipeline instance
    assert cortex._frame_pipeline is val.frame_pipeline


@pytest.mark.asyncio
async def test_full_perception_cycle():
    """Frame flows: FramePipeline.latest_frame -> VisionCortex -> analyzer.inject_frame.

    Confirms the real subsystem wiring: VisionActionLoop is registered,
    its frame_pipeline is discovered by VisionCortex, and a call to
    _run_one_perception_cycle delivers that frame to the analyzer.
    """
    val = VisionActionLoop(use_sck=False)
    frame = _make_frame(1)
    # Inject a frame directly into the pipeline (no SCK hardware needed)
    val.frame_pipeline._latest_frame = frame

    cortex = VisionCortex()
    await cortex._discover_subsystems()

    # Analyzer is optional -- skip inject assertion if not available in env
    if cortex._analyzer is None:
        pytest.skip("MemoryAwareScreenAnalyzer not available in this environment")

    with patch.object(cortex._analyzer, "inject_frame", new_callable=AsyncMock) as mock_inject:
        await cortex._run_one_perception_cycle()

    # inject_frame must have been called exactly once with the frame data
    mock_inject.assert_called_once()
    # First positional arg should be a PIL Image (not the raw ndarray)
    from PIL import Image
    call_args = mock_inject.call_args
    assert isinstance(call_args[0][0], Image.Image), (
        f"Expected PIL.Image as first arg to inject_frame, got {type(call_args[0][0])}"
    )
    # Second positional arg should be the frame timestamp
    assert call_args[0][1] == frame.timestamp


@pytest.mark.asyncio
async def test_graceful_degradation_no_vision_loop():
    """VisionCortex starts cleanly and is a no-op when VisionActionLoop is absent."""
    # Precondition: no VAL instance registered
    assert VisionActionLoop.get_instance() is None

    cortex = VisionCortex()
    await cortex._discover_subsystems()

    # No Ferrari Engine -- frame_pipeline must be None
    assert cortex._frame_pipeline is None

    # Perception cycle must be a silent no-op, not raise
    await cortex._run_one_perception_cycle()
