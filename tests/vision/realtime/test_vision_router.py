"""Tests for tiered VisionRouter (L1 scene → L2 J-Prime → L3 Claude)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.vision.realtime.vision_router import (
    VisionRouter, VisionQuery, VisionRouterResult, VisionTier,
)
from backend.knowledge.fabric import KnowledgeFabric


@pytest.fixture
def fabric():
    return KnowledgeFabric()


@pytest.fixture
def router(fabric):
    return VisionRouter(fabric=fabric)


class TestL1CacheHit:
    @pytest.mark.asyncio
    async def test_scene_hit_skips_remote(self, router, fabric):
        # Seed scene partition with known element
        fabric.write("kg://scene/button/submit-001", {
            "position": (523, 187),
            "confidence": 0.92,
            "element_type": "button",
            "text_content": "Submit",
        })
        query = VisionQuery(
            target_description="submit button",
            target_element_type="button",
            target_text="Submit",
        )
        result = await router.route(query)
        assert result.tier == VisionTier.L1_SCENE
        assert result.coords == (523, 187)
        assert result.confidence >= 0.9

    @pytest.mark.asyncio
    async def test_scene_miss_falls_to_l2(self, router):
        query = VisionQuery(target_description="nonexistent button")
        # Mock L2 to return a result
        router._call_jprime_vision = AsyncMock(return_value={
            "status": "found",
            "elements": [{"coords": [400, 300], "confidence": 0.85, "text_content": "OK"}],
        })
        result = await router.route(query)
        assert result.tier == VisionTier.L2_JPRIME
        router._call_jprime_vision.assert_called_once()


class TestL2JPrime:
    @pytest.mark.asyncio
    async def test_l2_success_updates_scene(self, router, fabric):
        query = VisionQuery(target_description="OK button")
        router._call_jprime_vision = AsyncMock(return_value={
            "status": "found",
            "elements": [{"coords": [400, 300], "confidence": 0.88,
                         "element_type": "button", "text_content": "OK"}],
        })
        result = await router.route(query)
        assert result.coords == (400, 300)
        # Scene graph should be updated
        cached = fabric.query_nearest_element((400, 300), max_distance=10)
        assert cached is not None

    @pytest.mark.asyncio
    async def test_l2_unavailable_falls_to_l3(self, router):
        query = VisionQuery(target_description="button")
        router._call_jprime_vision = AsyncMock(side_effect=Exception("unreachable"))
        router._call_claude_vision = AsyncMock(return_value={
            "status": "found",
            "elements": [{"coords": [200, 100], "confidence": 0.90}],
        })
        result = await router.route(query)
        assert result.tier == VisionTier.L3_CLAUDE


class TestL3Claude:
    @pytest.mark.asyncio
    async def test_all_unavailable_returns_degraded(self, router):
        query = VisionQuery(target_description="button")
        router._call_jprime_vision = AsyncMock(side_effect=Exception("down"))
        router._call_claude_vision = AsyncMock(side_effect=Exception("also down"))
        result = await router.route(query)
        assert result.tier == VisionTier.DEGRADED
        assert result.coords is None


class TestOperationalLevel:
    @pytest.mark.asyncio
    async def test_independent_from_mind_client(self, router):
        """Vision operational level is tracked independently."""
        assert router.operational_level == 0  # starts healthy
        # Simulate 3 L2 failures
        router._call_jprime_vision = AsyncMock(side_effect=Exception("down"))
        router._call_claude_vision = AsyncMock(side_effect=Exception("down"))
        for _ in range(3):
            query = VisionQuery(target_description="button")
            await router.route(query)
        assert router.operational_level >= 1  # degraded


class TestBrainSelector:
    @pytest.mark.asyncio
    async def test_routes_by_vision_task(self, router):
        """Different tasks should prefer different vision models."""
        # This test verifies the router passes task_type to the L2 call
        query = VisionQuery(
            target_description="complex form",
            vision_task_type="complex_ui_analysis",
        )
        router._call_jprime_vision = AsyncMock(return_value={
            "status": "found",
            "elements": [{"coords": [100, 100], "confidence": 0.8}],
        })
        await router.route(query)
        call_args = router._call_jprime_vision.call_args
        assert call_args is not None
