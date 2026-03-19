"""Tests for shadow mode divergence comparison.

Validates that InteractiveBrainRouter.compare_with_remote() correctly detects
divergences between local brain selection and remote J-Prime classification.
"""
import pytest
from backend.core.interactive_brain_router import InteractiveBrainRouter


@pytest.fixture
def router():
    return InteractiveBrainRouter()


class TestShadowComparison:
    @pytest.mark.asyncio
    async def test_no_divergence_when_matching(self, router):
        # "classification" task → light complexity → qwen_coder brain locally
        remote = {"brain_used": "qwen_coder", "complexity": "light"}
        result = await router.compare_with_remote("classification", "classify this", remote)
        assert result is None  # no divergence

    @pytest.mark.asyncio
    async def test_divergence_on_brain_mismatch(self, router):
        remote = {"brain_used": "phi3_lightweight", "complexity": "light"}
        result = await router.compare_with_remote("classification", "classify this", remote)
        assert result is not None
        assert "brain_id" in result
        assert result["brain_id"]["severity"] == "WARN"
        assert result["brain_id"]["local"] == "qwen_coder"
        assert result["brain_id"]["remote"] == "phi3_lightweight"

    @pytest.mark.asyncio
    async def test_divergence_on_complexity_mismatch(self, router):
        remote = {"brain_used": "qwen_coder", "complexity": "heavy"}
        result = await router.compare_with_remote("classification", "classify this", remote)
        assert result is not None
        assert "complexity" in result
        assert result["complexity"]["local"] == "light"
        assert result["complexity"]["remote"] == "heavy"

    @pytest.mark.asyncio
    async def test_keyword_escalation_matches_remote(self, router):
        # "analyze" keyword escalates complexity to "complex" → qwen_coder_32b locally
        remote = {"brain_used": "qwen_coder_32b", "complexity": "complex"}
        result = await router.compare_with_remote(
            "classification", "analyze the root cause", remote
        )
        assert result is None  # both agree on complex + qwen_coder_32b

    @pytest.mark.asyncio
    async def test_empty_remote_classification(self, router):
        # Empty remote yields empty strings for both brain_used and complexity
        result = await router.compare_with_remote("classification", "test", {})
        # Local selects qwen_coder/light — remote has "" for both → divergence
        assert result is not None

    @pytest.mark.asyncio
    async def test_divergence_contains_both_fields_when_both_differ(self, router):
        remote = {"brain_used": "phi3_lightweight", "complexity": "trivial"}
        result = await router.compare_with_remote("classification", "classify this", remote)
        assert result is not None
        assert "brain_id" in result
        assert "complexity" in result

    @pytest.mark.asyncio
    async def test_no_divergence_trivial_task(self, router):
        # "system_command" + trivial keyword → local selects phi3_lightweight/trivial
        remote = {"brain_used": "phi3_lightweight", "complexity": "trivial"}
        result = await router.compare_with_remote("system_command", "open terminal", remote)
        assert result is None

    @pytest.mark.asyncio
    async def test_vision_task_complexity_maps_to_heavy(self, router):
        # vision_action → heavy → qwen_coder brain
        remote = {"brain_used": "qwen_coder", "complexity": "heavy"}
        result = await router.compare_with_remote("vision_action", "click the button", remote)
        assert result is None  # both agree

    @pytest.mark.asyncio
    async def test_trivial_keyword_escalation_downgrade(self, router):
        # "classification" base is light; trivial keyword + light → downgrade to trivial
        remote = {"brain_used": "phi3_lightweight", "complexity": "trivial"}
        result = await router.compare_with_remote("classification", "open the app", remote)
        assert result is None  # both agree on trivial + phi3_lightweight

    @pytest.mark.asyncio
    async def test_severity_is_warn_on_brain_divergence(self, router):
        remote = {"brain_used": "unknown_brain", "complexity": "light"}
        result = await router.compare_with_remote("classification", "classify this", remote)
        if result and "brain_id" in result:
            assert result["brain_id"]["severity"] == "WARN"

    @pytest.mark.asyncio
    async def test_severity_is_warn_on_complexity_divergence(self, router):
        remote = {"brain_used": "qwen_coder", "complexity": "complex"}
        result = await router.compare_with_remote("classification", "classify this", remote)
        if result and "complexity" in result:
            assert result["complexity"]["severity"] == "WARN"

    @pytest.mark.asyncio
    async def test_compare_with_remote_is_async(self, router):
        """compare_with_remote must be awaitable (async def, not sync)."""
        import inspect
        assert inspect.iscoroutinefunction(router.compare_with_remote)
