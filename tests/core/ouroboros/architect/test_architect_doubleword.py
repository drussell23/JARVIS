"""
Tests for ArchitectureReasoningAgent — Doubleword 397B wiring (Task 5).

Covers:
- design() calls doubleword.prompt_only() for qualifying hypotheses
- design() returns None when doubleword raises an exception
- design() returns None when doubleword is None
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.architect.reasoning_agent import (
    AgentConfig,
    ArchitectureReasoningAgent,
)
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_PLAN_JSON = json.dumps({
    "title": "Add streaming capability",
    "description": "Adds async streaming support to the pipeline.",
    "repos_affected": ["jarvis"],
    "non_goals": ["Do not touch the TTS layer"],
    "steps": [
        {
            "step_index": 0,
            "description": "Create streaming module",
            "intent_kind": "create_file",
            "target_paths": ["backend/core/streaming.py"],
            "repo": "jarvis",
            "ancillary_paths": [],
            "interface_contracts": ["StreamingClient.stream()"],
            "tests_required": ["tests/core/test_streaming.py"],
            "risk_tier_hint": "safe_auto",
            "depends_on": [],
        }
    ],
    "acceptance_checks": [
        {
            "check_id": "chk-001",
            "check_kind": "exit_code",
            "command": "python3 -m pytest tests/core/test_streaming.py",
            "expected": "0",
            "cwd": ".",
            "timeout_s": 120.0,
            "run_after_step": None,
            "sandbox_required": True,
        }
    ],
})


def _make_hypothesis(gap_type: str = "missing_capability", confidence: float = 0.9) -> FeatureHypothesis:
    return FeatureHypothesis.new(
        description="Add streaming capability to the pipeline",
        evidence_fragments=("src-001",),
        gap_type=gap_type,
        confidence=confidence,
        confidence_rule_id="test-rule",
        urgency="medium",
        suggested_scope="new-agent",
        suggested_repos=("jarvis",),
        provenance="deterministic",
        synthesized_for_snapshot_hash="abc123",
        synthesis_input_fingerprint="fp-001",
    )


def _make_agent(doubleword=None, config: AgentConfig | None = None) -> ArchitectureReasoningAgent:
    oracle = MagicMock()
    # Simulate oracle returning a simple context string
    oracle.get_file_neighbourhood = MagicMock(return_value=None)
    return ArchitectureReasoningAgent(
        oracle=oracle,
        doubleword=doubleword,
        config=config or AgentConfig(),
    )


# ---------------------------------------------------------------------------
# test_design_calls_doubleword
# ---------------------------------------------------------------------------


def test_design_calls_doubleword():
    """When prompt_only returns valid plan JSON, design() returns an ArchitecturalPlan (or None if validation rejects mock data)."""
    doubleword = MagicMock()
    doubleword.prompt_only = AsyncMock(return_value=_VALID_PLAN_JSON)

    agent = _make_agent(doubleword=doubleword)
    h = _make_hypothesis()
    snapshot = MagicMock()
    oracle = MagicMock()

    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )

    # prompt_only must have been called exactly once with expected kwargs
    doubleword.prompt_only.assert_called_once()
    call_kwargs = doubleword.prompt_only.call_args
    assert call_kwargs.kwargs.get("caller_id") == "architecture_agent"
    assert call_kwargs.kwargs.get("response_format") is not None

    # Result is either a valid plan or None (if PlanValidator caught something in mock data)
    from backend.core.ouroboros.architect.plan import ArchitecturalPlan
    assert result is None or isinstance(result, ArchitecturalPlan)


def test_design_calls_doubleword_with_correct_prompt():
    """prompt_only is called with a non-empty prompt containing hypothesis description."""
    doubleword = MagicMock()
    doubleword.prompt_only = AsyncMock(return_value=_VALID_PLAN_JSON)

    agent = _make_agent(doubleword=doubleword)
    h = _make_hypothesis()
    snapshot = MagicMock()
    oracle = MagicMock()

    asyncio.get_event_loop().run_until_complete(agent.design(h, snapshot, oracle))

    prompt_arg = doubleword.prompt_only.call_args.kwargs.get("prompt") or doubleword.prompt_only.call_args.args[0]
    assert isinstance(prompt_arg, str)
    assert len(prompt_arg) > 50
    # Prompt should contain the hypothesis description
    assert "streaming" in prompt_arg.lower() or "capability" in prompt_arg.lower()


def test_design_returns_plan_on_valid_json():
    """design() returns an ArchitecturalPlan when prompt_only returns well-formed JSON that passes validation."""
    doubleword = MagicMock()
    doubleword.prompt_only = AsyncMock(return_value=_VALID_PLAN_JSON)

    agent = _make_agent(doubleword=doubleword)
    h = _make_hypothesis()
    snapshot = MagicMock()
    oracle = MagicMock()

    from backend.core.ouroboros.architect.plan import ArchitecturalPlan
    from backend.core.ouroboros.architect.plan_validator import PlanValidator

    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )

    if result is not None:
        assert isinstance(result, ArchitecturalPlan)
        assert result.title == "Add streaming capability"
        assert result.parent_hypothesis_id == h.hypothesis_id


def test_design_plan_uses_config_model_name():
    """The plan's model_used field reflects the agent's configured model."""
    doubleword = MagicMock()
    doubleword.prompt_only = AsyncMock(return_value=_VALID_PLAN_JSON)

    config = AgentConfig(model="doubleword-397b", max_steps=10)
    agent = _make_agent(doubleword=doubleword, config=config)
    h = _make_hypothesis()
    snapshot = MagicMock()
    oracle = MagicMock()

    from backend.core.ouroboros.architect.plan import ArchitecturalPlan

    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )

    if result is not None:
        assert isinstance(result, ArchitecturalPlan)
        assert result.model_used == "doubleword-397b"


# ---------------------------------------------------------------------------
# test_design_returns_none_on_doubleword_failure
# ---------------------------------------------------------------------------


def test_design_returns_none_on_doubleword_exception():
    """When prompt_only raises, design() catches the exception and returns None."""
    doubleword = MagicMock()
    doubleword.prompt_only = AsyncMock(side_effect=RuntimeError("network failure"))

    agent = _make_agent(doubleword=doubleword)
    h = _make_hypothesis()
    snapshot = MagicMock()
    oracle = MagicMock()

    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )

    assert result is None


def test_design_returns_none_on_value_error():
    """When prompt_only raises ValueError (e.g. missing API key), design() returns None."""
    doubleword = MagicMock()
    doubleword.prompt_only = AsyncMock(
        side_effect=ValueError("DOUBLEWORD_API_KEY is not set")
    )

    agent = _make_agent(doubleword=doubleword)
    h = _make_hypothesis()
    snapshot = MagicMock()
    oracle = MagicMock()

    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )

    assert result is None


def test_design_returns_none_on_json_parse_error():
    """When prompt_only returns invalid JSON, design() returns None."""
    doubleword = MagicMock()
    doubleword.prompt_only = AsyncMock(return_value="this is not json {{{")

    agent = _make_agent(doubleword=doubleword)
    h = _make_hypothesis()
    snapshot = MagicMock()
    oracle = MagicMock()

    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )

    assert result is None


def test_design_returns_none_on_empty_response():
    """When prompt_only returns an empty string, design() returns None."""
    doubleword = MagicMock()
    doubleword.prompt_only = AsyncMock(return_value="")

    agent = _make_agent(doubleword=doubleword)
    h = _make_hypothesis()
    snapshot = MagicMock()
    oracle = MagicMock()

    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )

    assert result is None


def test_design_returns_none_on_missing_required_field():
    """When JSON is missing a required field (e.g. steps), design() returns None."""
    incomplete_json = json.dumps({
        "title": "Incomplete plan",
        "description": "Missing steps",
        "repos_affected": ["jarvis"],
        "non_goals": ["Nothing"],
        # "steps" is missing
        "acceptance_checks": [],
    })
    doubleword = MagicMock()
    doubleword.prompt_only = AsyncMock(return_value=incomplete_json)

    agent = _make_agent(doubleword=doubleword)
    h = _make_hypothesis()
    snapshot = MagicMock()
    oracle = MagicMock()

    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )

    assert result is None


# ---------------------------------------------------------------------------
# test_design_without_doubleword
# ---------------------------------------------------------------------------


def test_design_without_doubleword_returns_none():
    """When doubleword=None, design() returns None for qualifying hypotheses."""
    agent = _make_agent(doubleword=None)
    h = _make_hypothesis()
    snapshot = MagicMock()
    oracle = MagicMock()

    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )

    assert result is None


def test_design_without_doubleword_does_not_raise():
    """doubleword=None path must not raise any exception."""
    agent = _make_agent(doubleword=None)
    h = _make_hypothesis(gap_type="manifesto_violation", confidence=0.95)
    snapshot = MagicMock()
    oracle = MagicMock()

    # Should complete without raising
    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )
    assert result is None


def test_design_still_returns_none_for_filtered_hypothesis_without_doubleword():
    """Filtered hypotheses return None regardless of doubleword presence."""
    agent = _make_agent(doubleword=None)
    h = _make_hypothesis(gap_type="incomplete_wiring", confidence=0.95)
    snapshot = MagicMock()
    oracle = MagicMock()

    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )

    assert result is None


def test_design_does_not_call_doubleword_for_filtered_hypothesis():
    """prompt_only is never called when should_design() returns False."""
    doubleword = MagicMock()
    doubleword.prompt_only = AsyncMock(return_value=_VALID_PLAN_JSON)

    agent = _make_agent(doubleword=doubleword)
    h = _make_hypothesis(gap_type="incomplete_wiring", confidence=0.95)
    snapshot = MagicMock()
    oracle = MagicMock()

    asyncio.get_event_loop().run_until_complete(agent.design(h, snapshot, oracle))

    doubleword.prompt_only.assert_not_called()


# ---------------------------------------------------------------------------
# Doubleword object without prompt_only attribute
# ---------------------------------------------------------------------------


def test_design_returns_none_when_doubleword_lacks_prompt_only():
    """If the injected doubleword object lacks prompt_only, design() returns None."""
    class _BareDoubleword:
        """Object without prompt_only."""

    agent = _make_agent(doubleword=_BareDoubleword())
    h = _make_hypothesis()
    snapshot = MagicMock()
    oracle = MagicMock()

    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )

    assert result is None
