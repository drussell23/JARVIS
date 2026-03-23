"""Tests for VisionActionLoop -- the real-time vision orchestrator."""
import asyncio
import time
import pytest
import numpy as np
from unittest.mock import AsyncMock, patch, MagicMock

from backend.vision.realtime.vision_action_loop import VisionActionLoop, VisionActionResult
from backend.vision.realtime.states import VisionState
from backend.vision.realtime.fusion import VisionResult


@pytest.fixture
def loop():
    """Create loop without real SCK stream."""
    return VisionActionLoop(use_sck=False)


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_starts_in_idle(self, loop):
        assert loop.state == VisionState.IDLE

    @pytest.mark.asyncio
    async def test_start_moves_to_watching(self, loop):
        await loop.start()
        assert loop.state == VisionState.WATCHING
        await loop.stop()

    @pytest.mark.asyncio
    async def test_stop_returns_to_idle(self, loop):
        await loop.start()
        await loop.stop()
        assert loop.state == VisionState.IDLE


class TestExecuteAction:
    @pytest.mark.asyncio
    async def test_execute_returns_result(self, loop):
        """Full cycle: target -> precheck -> act -> verify."""
        # Mock the vision router to return coords
        loop._vision_router.route = AsyncMock(return_value=MagicMock(
            tier="L1_SCENE",
            coords=(523, 187),
            confidence=0.92,
            element_data={"text_content": "Submit"},
            backend_used="scene_graph",
            latency_ms=2.0,
        ))
        # Mock action executor
        loop._action_executor.execute = AsyncMock(return_value=MagicMock(
            success=True, action_id="act-001", latency_ms=50.0, error=None,
        ))
        # Mock verification to succeed
        loop._verifier.verify_click = MagicMock(return_value=MagicMock(
            status="SUCCESS", confidence=0.9, diff_magnitude=0.5,
        ))
        # Mock frame capture for verification
        loop._capture_verification_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )
        loop._capture_pre_action_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )

        result = await loop.execute_action(
            target_description="submit button",
            action_type="click",
        )
        assert result.success is True
        assert result.coords == (523, 187)

    @pytest.mark.asyncio
    async def test_low_confidence_triggers_approval(self, loop):
        """Low confidence -> PRECHECK blocks -> needs approval."""
        loop._vision_router.route = AsyncMock(return_value=MagicMock(
            tier="L2_JPRIME",
            coords=(100, 100),
            confidence=0.50,  # below threshold
            element_data={},
            backend_used="jprime_llava",
            latency_ms=200.0,
        ))
        result = await loop.execute_action(
            target_description="ambiguous element",
            action_type="click",
        )
        assert result.success is False
        assert "LOW_CONFIDENCE" in str(result.failed_guards) or "CONFIDENCE" in str(result.failed_guards)

    @pytest.mark.asyncio
    async def test_execute_type_action(self, loop):
        """Full cycle for a TYPE action."""
        loop._vision_router.route = AsyncMock(return_value=MagicMock(
            tier="L1_SCENE",
            coords=(300, 200),
            confidence=0.95,
            element_data={"text_content": "Search", "bbox": (280, 190, 320, 210)},
            backend_used="scene_graph",
            latency_ms=1.5,
        ))
        loop._action_executor.execute = AsyncMock(return_value=MagicMock(
            success=True, action_id="act-002", latency_ms=80.0, error=None,
        ))
        loop._verifier.verify_type = MagicMock(return_value=MagicMock(
            status="SUCCESS", confidence=0.88, diff_magnitude=4.2,
        ))
        loop._capture_verification_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )
        loop._capture_pre_action_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )

        result = await loop.execute_action(
            target_description="search box",
            action_type="type",
            target_text="hello",
        )
        assert result.success is True
        assert result.action_type == "type"

    @pytest.mark.asyncio
    async def test_execute_scroll_action(self, loop):
        """Full cycle for a SCROLL action."""
        loop._vision_router.route = AsyncMock(return_value=MagicMock(
            tier="L1_SCENE",
            coords=(500, 400),
            confidence=0.90,
            element_data={},
            backend_used="scene_graph",
            latency_ms=1.0,
        ))
        loop._action_executor.execute = AsyncMock(return_value=MagicMock(
            success=True, action_id="act-003", latency_ms=20.0, error=None,
        ))
        loop._verifier.verify_scroll = MagicMock(return_value=MagicMock(
            status="SUCCESS", confidence=0.85, diff_magnitude=3.0,
        ))
        loop._capture_verification_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )
        loop._capture_pre_action_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )

        result = await loop.execute_action(
            target_description="page content",
            action_type="scroll",
        )
        assert result.success is True
        assert result.action_type == "scroll"


class TestDegradedMode:
    @pytest.mark.asyncio
    async def test_vision_unavailable_enters_degraded(self, loop):
        loop._vision_router.route = AsyncMock(return_value=MagicMock(
            tier="DEGRADED",
            coords=None,
            confidence=0.0,
            element_data=None,
            backend_used="none",
            latency_ms=0.0,
        ))
        result = await loop.execute_action(
            target_description="button",
            action_type="click",
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_no_coords_returns_failure(self, loop):
        """Router returns a tier but no coords -- failure."""
        loop._vision_router.route = AsyncMock(return_value=MagicMock(
            tier="L2_JPRIME",
            coords=None,
            confidence=0.0,
            element_data=None,
            backend_used="jprime_llava",
            latency_ms=100.0,
        ))
        result = await loop.execute_action(
            target_description="button",
            action_type="click",
        )
        assert result.success is False
        assert result.coords is None


class TestRetryBehaviour:
    @pytest.mark.asyncio
    async def test_verification_failure_triggers_retry(self, loop):
        """When verification fails, the loop retries up to MAX_RETRIES."""
        call_count = 0

        async def routing_side_effect(query):
            nonlocal call_count
            call_count += 1
            return MagicMock(
                tier="L1_SCENE",
                coords=(200, 200),
                confidence=0.95,
                element_data={},
                backend_used="scene_graph",
                latency_ms=1.0,
            )

        loop._vision_router.route = AsyncMock(side_effect=routing_side_effect)
        loop._action_executor.execute = AsyncMock(return_value=MagicMock(
            success=True, action_id="act-retry", latency_ms=30.0, error=None,
        ))
        # Verification always fails
        loop._verifier.verify_click = MagicMock(return_value=MagicMock(
            status="FAIL", confidence=0.1, diff_magnitude=0.5,
        ))
        loop._capture_verification_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )
        loop._capture_pre_action_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )

        result = await loop.execute_action("button", "click")
        # Should have retried (initial + MAX_RETRIES attempts)
        assert result.success is False
        assert call_count >= 2  # at least one retry

    @pytest.mark.asyncio
    async def test_action_failure_no_retry(self, loop):
        """When the action itself fails, do not retry (executor error)."""
        loop._vision_router.route = AsyncMock(return_value=MagicMock(
            tier="L1_SCENE",
            coords=(200, 200),
            confidence=0.95,
            element_data={},
            backend_used="scene_graph",
            latency_ms=1.0,
        ))
        loop._action_executor.execute = AsyncMock(return_value=MagicMock(
            success=False, action_id="act-fail", latency_ms=10.0, error="pyautogui error",
        ))
        loop._capture_pre_action_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )

        result = await loop.execute_action("button", "click")
        assert result.success is False
        assert result.error is not None


class TestMetrics:
    @pytest.mark.asyncio
    async def test_emits_action_record(self, loop):
        records = []
        loop.on_action_record = lambda r: records.append(r)

        loop._vision_router.route = AsyncMock(return_value=MagicMock(
            tier="L1_SCENE", coords=(100, 100), confidence=0.95,
            element_data={}, backend_used="scene_graph", latency_ms=1.0,
        ))
        loop._action_executor.execute = AsyncMock(return_value=MagicMock(
            success=True, action_id="act-001", latency_ms=30.0, error=None,
        ))
        loop._verifier.verify_click = MagicMock(return_value=MagicMock(
            status="SUCCESS", confidence=0.9, diff_magnitude=0.5,
        ))
        loop._capture_verification_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )
        loop._capture_pre_action_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )

        await loop.execute_action("button", "click")
        assert len(records) >= 1
        assert "action_id" in records[0]
        assert "backend_used" in records[0]

    @pytest.mark.asyncio
    async def test_record_includes_timing(self, loop):
        records = []
        loop.on_action_record = lambda r: records.append(r)

        loop._vision_router.route = AsyncMock(return_value=MagicMock(
            tier="L1_SCENE", coords=(100, 100), confidence=0.95,
            element_data={}, backend_used="scene_graph", latency_ms=1.0,
        ))
        loop._action_executor.execute = AsyncMock(return_value=MagicMock(
            success=True, action_id="act-002", latency_ms=25.0, error=None,
        ))
        loop._verifier.verify_click = MagicMock(return_value=MagicMock(
            status="SUCCESS", confidence=0.9, diff_magnitude=0.5,
        ))
        loop._capture_verification_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )
        loop._capture_pre_action_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )

        await loop.execute_action("button", "click")
        assert len(records) >= 1
        assert "timestamp" in records[0]
        assert "latency_ms" in records[0]


class TestCoordsHint:
    @pytest.mark.asyncio
    async def test_coords_hint_forwarded_to_query(self, loop):
        """coords_hint should appear in the VisionQuery passed to the router."""
        captured_queries = []

        async def capture_route(query):
            captured_queries.append(query)
            return MagicMock(
                tier="L1_SCENE", coords=(50, 50), confidence=0.95,
                element_data={}, backend_used="scene_graph", latency_ms=1.0,
            )

        loop._vision_router.route = AsyncMock(side_effect=capture_route)
        loop._action_executor.execute = AsyncMock(return_value=MagicMock(
            success=True, action_id="act-hint", latency_ms=10.0, error=None,
        ))
        loop._verifier.verify_click = MagicMock(return_value=MagicMock(
            status="SUCCESS", confidence=0.9, diff_magnitude=0.5,
        ))
        loop._capture_verification_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )
        loop._capture_pre_action_frame = AsyncMock(
            return_value=np.zeros((100, 100, 3), dtype=np.uint8)
        )

        await loop.execute_action(
            target_description="button",
            action_type="click",
            coords_hint=(50, 50),
        )
        assert len(captured_queries) >= 1
        assert captured_queries[0].coords_hint == (50, 50)


def test_frame_pipeline_property(loop):
    """VisionCortex needs access to the frame pipeline."""
    assert loop.frame_pipeline is not None


def test_knowledge_fabric_property(loop):
    """VisionCortex needs access to the knowledge fabric for L1 cache updates."""
    assert loop.knowledge_fabric is not None
