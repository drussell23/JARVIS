"""
Tests for ArchitectureReasoningAgent
=====================================

Covers:
- Threshold filtering via should_design()
- design() returns None for filtered hypotheses
- design() returns None for qualifying hypotheses (v1 — model pending)
- health() returns expected dict shape
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.architect.reasoning_agent import (
    AgentConfig,
    ArchitectureReasoningAgent,
    _ARCHITECTURAL_GAP_TYPES,
)
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_agent(config: AgentConfig | None = None) -> ArchitectureReasoningAgent:
    oracle = MagicMock()
    doubleword = MagicMock()
    return ArchitectureReasoningAgent(
        oracle=oracle,
        doubleword=doubleword,
        config=config or AgentConfig(),
    )


def _make_hypothesis(gap_type: str, confidence: float) -> FeatureHypothesis:
    return FeatureHypothesis.new(
        description=f"Test hypothesis for {gap_type}",
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


# ---------------------------------------------------------------------------
# _ARCHITECTURAL_GAP_TYPES constant
# ---------------------------------------------------------------------------


def test_architectural_gap_types_contains_expected():
    assert "missing_capability" in _ARCHITECTURAL_GAP_TYPES
    assert "manifesto_violation" in _ARCHITECTURAL_GAP_TYPES
    assert "incomplete_wiring" not in _ARCHITECTURAL_GAP_TYPES
    assert "stale_implementation" not in _ARCHITECTURAL_GAP_TYPES


# ---------------------------------------------------------------------------
# should_design — gap type filtering
# ---------------------------------------------------------------------------


def test_filters_non_architectural_gap_types_incomplete_wiring():
    agent = _make_agent()
    h = _make_hypothesis(gap_type="incomplete_wiring", confidence=0.9)
    assert agent.should_design(h) is False


def test_filters_non_architectural_gap_types_missing_capability():
    agent = _make_agent()
    h = _make_hypothesis(gap_type="missing_capability", confidence=0.9)
    assert agent.should_design(h) is True


def test_filters_non_architectural_gap_types_manifesto_violation():
    agent = _make_agent()
    h = _make_hypothesis(gap_type="manifesto_violation", confidence=0.9)
    assert agent.should_design(h) is True


def test_filters_non_architectural_gap_type_stale_implementation():
    agent = _make_agent()
    h = _make_hypothesis(gap_type="stale_implementation", confidence=0.95)
    assert agent.should_design(h) is False


# ---------------------------------------------------------------------------
# should_design — confidence filtering
# ---------------------------------------------------------------------------


def test_filters_low_confidence_below_threshold():
    config = AgentConfig(min_confidence=0.8)
    agent = _make_agent(config)
    h = _make_hypothesis(gap_type="missing_capability", confidence=0.5)
    assert agent.should_design(h) is False


def test_filters_low_confidence_above_threshold():
    config = AgentConfig(min_confidence=0.8)
    agent = _make_agent(config)
    h = _make_hypothesis(gap_type="missing_capability", confidence=0.9)
    assert agent.should_design(h) is True


def test_filters_confidence_at_exact_threshold():
    config = AgentConfig(min_confidence=0.7)
    agent = _make_agent(config)
    h = _make_hypothesis(gap_type="missing_capability", confidence=0.7)
    assert agent.should_design(h) is True


def test_filters_confidence_just_below_threshold():
    config = AgentConfig(min_confidence=0.7)
    agent = _make_agent(config)
    h = _make_hypothesis(gap_type="missing_capability", confidence=0.699)
    assert agent.should_design(h) is False


# ---------------------------------------------------------------------------
# should_design — mock hypothesis (duck typing)
# ---------------------------------------------------------------------------


def test_should_design_works_with_mock_hypothesis():
    """should_design uses only .gap_type and .confidence — accepts duck types."""
    agent = _make_agent()
    mock_h = MagicMock()
    mock_h.gap_type = "missing_capability"
    mock_h.confidence = 0.85
    assert agent.should_design(mock_h) is True


def test_should_design_rejects_mock_with_wrong_gap_type():
    agent = _make_agent()
    mock_h = MagicMock()
    mock_h.gap_type = "incomplete_wiring"
    mock_h.confidence = 0.95
    assert agent.should_design(mock_h) is False


# ---------------------------------------------------------------------------
# design() — filtered → None
# ---------------------------------------------------------------------------


def test_design_returns_none_for_filtered():
    agent = _make_agent()
    h = _make_hypothesis(gap_type="incomplete_wiring", confidence=0.95)
    snapshot = MagicMock()
    oracle = MagicMock()
    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )
    assert result is None


def test_design_returns_none_for_low_confidence():
    config = AgentConfig(min_confidence=0.8)
    agent = _make_agent(config)
    h = _make_hypothesis(gap_type="missing_capability", confidence=0.6)
    snapshot = MagicMock()
    oracle = MagicMock()
    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )
    assert result is None


# ---------------------------------------------------------------------------
# design() — qualifying → None in v1 (model integration pending)
# ---------------------------------------------------------------------------


def test_design_returns_none_v1():
    """v1: model bridge not yet built, design() logs and returns None."""
    agent = _make_agent()
    h = _make_hypothesis(gap_type="missing_capability", confidence=0.9)
    snapshot = MagicMock()
    oracle = MagicMock()
    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )
    assert result is None


def test_design_returns_none_v1_manifesto_violation():
    agent = _make_agent()
    h = _make_hypothesis(gap_type="manifesto_violation", confidence=0.75)
    snapshot = MagicMock()
    oracle = MagicMock()
    result = asyncio.get_event_loop().run_until_complete(
        agent.design(h, snapshot, oracle)
    )
    assert result is None


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


def test_health_returns_dict():
    agent = _make_agent()
    result = agent.health()
    assert isinstance(result, dict)


def test_health_contains_model_integration_pending(monkeypatch):
    """model_integration is 'pending' when doubleword is None and no Claude key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    oracle = MagicMock()
    agent = ArchitectureReasoningAgent(oracle=oracle, doubleword=None)
    result = agent.health()
    assert result.get("model_integration") == "pending"


def test_health_contains_model_integration_active():
    """model_integration is 'active' when doubleword exposes prompt_only."""
    oracle = MagicMock()
    doubleword = MagicMock()  # MagicMock auto-creates prompt_only attribute
    agent = ArchitectureReasoningAgent(oracle=oracle, doubleword=doubleword)
    result = agent.health()
    assert result.get("model_integration") == "active"


def test_health_reflects_config():
    config = AgentConfig(min_confidence=0.85, model="custom-model", max_steps=5)
    agent = _make_agent(config)
    result = agent.health()
    assert result["min_confidence"] == 0.85
    assert result["model"] == "custom-model"
    assert result["max_steps"] == 5


def test_health_contains_architectural_gap_types():
    agent = _make_agent()
    result = agent.health()
    assert "architectural_gap_types" in result
    assert set(result["architectural_gap_types"]) == _ARCHITECTURAL_GAP_TYPES
