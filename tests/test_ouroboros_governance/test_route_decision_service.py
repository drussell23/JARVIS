"""
Tests for Task 2: Centralize Routing Authority — RouteDecisionService

Covers:
1. test_intent_routes_to_correct_brain — known intents map to expected brain_ids
2. test_fallback_to_brain_selector_on_unknown_intent — unmapped intent defers to BrainSelector
3. test_cai_exception_falls_back — exception in _classify_intent defers to BrainSelector
4. test_daily_spend_and_record_cost_proxy — proxies delegate to the underlying BrainSelector
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.brain_selector import (
    BrainSelectionResult,
    BrainSelector,
    TaskComplexity,
)
from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot
from backend.core.ouroboros.governance.route_decision_service import (
    RouteDecisionService,
    _INTENT_TO_BRAIN,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normal_snap() -> ResourceSnapshot:
    """Low-pressure snapshot so resource/cost gates don't fire."""
    return ResourceSnapshot(
        ram_percent=50.0,
        cpu_percent=30.0,
        event_loop_latency_ms=2.0,
        disk_io_busy=False,
    )


def _make_service(mock_classify_return: str) -> tuple[RouteDecisionService, MagicMock]:
    """
    Build a RouteDecisionService with IntelligentModelSelector stubbed out.

    Returns (service, mock_selector).
    The mock_selector._classify_intent is an AsyncMock that returns mock_classify_return.
    """
    mock_selector = MagicMock()
    mock_selector._classify_intent = AsyncMock(return_value=mock_classify_return)

    brain_selector = BrainSelector()
    service = RouteDecisionService(brain_selector=brain_selector)
    # Bypass lazy init — inject mock directly
    service._selector = mock_selector
    return service, mock_selector


# ---------------------------------------------------------------------------
# Test 1 — Intent maps to the correct brain_id
# ---------------------------------------------------------------------------

INTENT_TO_EXPECTED_BRAIN = [
    ("code_generation",     "qwen_coder"),
    ("bug_fix",             "mistral_planning"),
    ("segfault_analysis",   "deepseek_r1"),
    ("heavy_refactor",      "qwen_coder"),
    ("architecture_design", "deepseek_r1"),
    ("single_line_change",  "phi3_lightweight"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("intent,expected_brain", INTENT_TO_EXPECTED_BRAIN)
async def test_intent_routes_to_correct_brain(intent: str, expected_brain: str):
    """
    RouteDecisionService.select() must return the expected brain_id for each
    known codegen intent when _classify_intent() is mocked to return that intent.
    """
    service, _ = _make_service(intent)
    snap = _normal_snap()

    result = await service.select(
        description="some task description",
        target_files=("backend/core/foo.py",),
        snapshot=snap,
        blast_radius=1,
    )

    assert isinstance(result, BrainSelectionResult), "Result must be a BrainSelectionResult"
    assert result.brain_id == expected_brain, (
        f"intent={intent!r}: expected brain_id={expected_brain!r}, got {result.brain_id!r}"
    )
    # Routing reason must carry the CAI intent prefix
    assert result.routing_reason.startswith("cai_intent_"), (
        f"routing_reason should start with 'cai_intent_', got {result.routing_reason!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Unknown intent falls back to BrainSelector.select()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fallback_to_brain_selector_on_unknown_intent():
    """
    When _classify_intent() returns an intent not in _INTENT_TO_BRAIN,
    RouteDecisionService must fall back to BrainSelector.select().
    """
    # Verify the intent really is absent from the table
    assert "unknown_intent" not in _INTENT_TO_BRAIN

    mock_brain_selector = MagicMock(spec=BrainSelector)
    fallback_result = BrainSelectionResult(
        brain_id="mistral_planning",
        model_name="mistral-7b",
        fallback_model="mistral-7b",
        routing_reason="fallback_test",
        task_complexity=TaskComplexity.LIGHT.value,
        estimated_prompt_tokens=200,
        provider_tier="gcp_prime",
    )
    mock_brain_selector.select.return_value = fallback_result
    mock_brain_selector.daily_spend = 0.0
    mock_brain_selector.daily_spend_breakdown = {"gcp_usd": 0.0, "claude_usd": 0.0, "total_usd": 0.0}

    service = RouteDecisionService(brain_selector=mock_brain_selector)

    mock_selector = MagicMock()
    mock_selector._classify_intent = AsyncMock(return_value="unknown_intent")
    service._selector = mock_selector

    snap = _normal_snap()
    result = await service.select(
        description="do something obscure",
        target_files=("some/file.py",),
        snapshot=snap,
        blast_radius=1,
    )

    assert result is fallback_result, "Result must be the fallback_result from BrainSelector.select()"
    mock_brain_selector.select.assert_called_once_with(
        description="do something obscure",
        target_files=("some/file.py",),
        snapshot=snap,
        blast_radius=1,
    )


# ---------------------------------------------------------------------------
# Test 3 — Exception in _classify_intent falls back to BrainSelector.select()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cai_exception_falls_back():
    """
    When _classify_intent() raises an exception, RouteDecisionService must
    silently catch it and fall back to BrainSelector.select().
    """
    mock_brain_selector = MagicMock(spec=BrainSelector)
    fallback_result = BrainSelectionResult(
        brain_id="mistral_planning",
        model_name="mistral-7b",
        fallback_model="mistral-7b",
        routing_reason="exception_fallback",
        task_complexity=TaskComplexity.LIGHT.value,
        estimated_prompt_tokens=150,
        provider_tier="gcp_prime",
    )
    mock_brain_selector.select.return_value = fallback_result
    mock_brain_selector.daily_spend = 0.0
    mock_brain_selector.daily_spend_breakdown = {"gcp_usd": 0.0, "claude_usd": 0.0, "total_usd": 0.0}

    service = RouteDecisionService(brain_selector=mock_brain_selector)

    mock_selector = MagicMock()
    mock_selector._classify_intent = AsyncMock(side_effect=RuntimeError("CAI blew up"))
    service._selector = mock_selector

    snap = _normal_snap()
    result = await service.select(
        description="fix the crash in auth module",
        target_files=("backend/auth.py",),
        snapshot=snap,
        blast_radius=1,
    )

    assert result is fallback_result, "On exception, result must be the fallback from BrainSelector"
    mock_brain_selector.select.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4 — daily_spend and record_cost proxy to BrainSelector
# ---------------------------------------------------------------------------

def test_daily_spend_and_record_cost_proxy():
    """
    RouteDecisionService.daily_spend and record_cost() must delegate to the
    underlying BrainSelector instance unchanged.
    """
    mock_brain_selector = MagicMock(spec=BrainSelector)
    mock_brain_selector.daily_spend = 0.1234
    mock_brain_selector.daily_spend_breakdown = {
        "gcp_usd": 0.05, "claude_usd": 0.07, "total_usd": 0.12
    }

    service = RouteDecisionService(brain_selector=mock_brain_selector)

    # daily_spend proxies the underlying value
    assert service.daily_spend == 0.1234

    # record_cost delegates to BrainSelector.record_cost
    service.record_cost("gcp_prime", 0.005)
    mock_brain_selector.record_cost.assert_called_once_with("gcp_prime", 0.005)


# ---------------------------------------------------------------------------
# Test 5 — _classify_intent table completeness guard
# ---------------------------------------------------------------------------

def test_intent_table_maps_to_valid_complexity_and_brain():
    """Every entry in _INTENT_TO_BRAIN must have a valid TaskComplexity and non-empty brain_id."""
    valid_brains = {"phi3_lightweight", "mistral_planning", "qwen_coder", "deepseek_r1"}
    for intent, (complexity, brain_id) in _INTENT_TO_BRAIN.items():
        assert isinstance(complexity, TaskComplexity), (
            f"intent={intent!r}: complexity must be TaskComplexity, got {type(complexity)}"
        )
        assert brain_id in valid_brains, (
            f"intent={intent!r}: brain_id={brain_id!r} not in known brains {valid_brains}"
        )
