"""Integration smoke test: agentic vision loop with real subsystem classes."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from backend.core.runtime_task_orchestrator import (
    RuntimeTaskOrchestrator,
    StopReason,
)
from backend.vision.realtime.frame_pipeline import FrameData
from backend.vision.realtime.vision_action_loop import VisionActionLoop


@pytest.fixture(autouse=True)
def clean_singletons():
    VisionActionLoop.set_instance(None)
    yield
    VisionActionLoop.set_instance(None)


@pytest.mark.asyncio
async def test_full_loop_with_mocked_mind_and_real_val():
    """End-to-end: RTO -> real VisionActionLoop -> mocked J-Prime."""
    # Create real VisionActionLoop (no SCK)
    val = VisionActionLoop(use_sck=False)
    VisionActionLoop.set_instance(val)

    try:
        rto = RuntimeTaskOrchestrator.__new__(RuntimeTaskOrchestrator)
        rto._prime = None  # no URL resolution
        rto.logger = MagicMock()

        # Inject a frame directly into the pipeline's internal slot
        frame = FrameData(
            data=np.zeros((100, 100, 3), dtype=np.uint8),
            width=100,
            height=100,
            timestamp=1.0,
            frame_number=1,
        )
        val.frame_pipeline._latest_frame = frame

        # Mock mind client to report goal achieved on first turn
        mock_mind = MagicMock()
        mock_mind.reason_vision_turn = AsyncMock(return_value={
            "schema": "vision.loop.v1",
            "goal_achieved": True,
            "stop_reason": "goal_satisfied",
            "next_action": None,
            "reasoning": "Page loaded correctly",
            "confidence": 0.95,
        })
        mock_mind._compress_frame_jpeg = MagicMock(return_value={
            "data": "abc",
            "content_type": "image/jpeg",
            "sha256": "test",
            "width": 100,
            "height": 100,
        })
        rto._get_mind_client = MagicMock(return_value=mock_mind)
        rto._get_vision_action_loop = AsyncMock(return_value=val)

        # Patch the page-load sleep and the subprocess open so the test is fast
        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.wait = AsyncMock(return_value=0)
            mock_exec.return_value = mock_proc

            result = await rto._dispatch_to_vision(
                "open LinkedIn", {"url": "https://linkedin.com"}
            )

        assert result["success"] is True
        assert result.get("stop_reason") == "goal_satisfied"

    finally:
        VisionActionLoop.set_instance(None)
