# [Ouroboros] Modified by Ouroboros (op=op-01a10a6c-) at 2026-10-05 04:58 UTC
# Reason: `backend/autonomy/contracts/reasoning_provider.py` has no corresponding test module. CREATE `tests/test_reasoning_provid

import os
import sys
import pytest
from unittest.mock import MagicMock, AsyncMock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from autonomy.contracts.reasoning_provider import ReasoningProvider
from core.contracts.decision_envelope import (
    DecisionEnvelope,
    DecisionSource,
    DecisionType,
    OriginComponent,
)


class _FakeReasoningProvider:
    def __init__(self):
        self._provider_name = DecisionSource.HEURISTIC

    async def reason(self, prompt: str, context: dict, deadline=None) -> DecisionEnvelope:
        return DecisionEnvelope(
            envelope_id="env-123",
            trace_id="trace-456",
            parent_envelope_id=None,
            decision_type=DecisionType.SCORING,
            source=self._provider_name,
            origin_component=OriginComponent.EMAIL_TRIAGE_SCORING,
            payload={"prompt": prompt, "context": context},
            confidence=0.9,
            created_at_epoch=1234567890.0,
            created_at_monotonic=1234.0,
            causal_seq=1,
            config_version="v1",
        )

    @property
    def provider_name(self) -> DecisionSource:
        return self._provider_name


class TestReasoningProviderProtocol:
    def test_satisfies_protocol(self):
        provider = _FakeReasoningProvider()
        assert isinstance(provider, ReasoningProvider)

    def test_provider_name_property_returns_correct_type(self):
        provider = _FakeReasoningProvider()
        assert provider.provider_name == DecisionSource.HEURISTIC


@pytest.mark.asyncio
async def test_reason_function_calls_and_returns_envelope():
    provider = _FakeReasoningProvider()
    mock_prompt = "Test prompt"
    mock_context = {"test": "data"}
    
    result = await provider.reason(mock_prompt, mock_context)
    
    assert isinstance(result, DecisionEnvelope)
    assert result.decision_type == DecisionType.SCORING
    assert result.source == DecisionSource.HEURISTIC
    assert result.payload["prompt"] == mock_prompt
    assert result.payload["context"] == mock_context


@pytest.mark.asyncio
async def test_reason_with_deadline_parameter():
    provider = _FakeReasoningProvider()
    mock_prompt = "Test prompt"
    mock_context = {"test": "data"}
    
    # Test with deadline parameter passed (should not raise)
    result = await provider.reason(mock_prompt, mock_context, deadline=1234567890.0)
    
    assert isinstance(result, DecisionEnvelope)
    assert result.decision_type == DecisionType.SCORING
    assert result.source == DecisionSource.HEURISTIC