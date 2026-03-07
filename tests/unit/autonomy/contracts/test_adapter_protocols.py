"""Tests for ReasoningProvider and ActionExecutor thin adapter protocols."""

import os
import sys
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend")
)

import pytest

from autonomy.contracts.reasoning_provider import ReasoningProvider
from autonomy.contracts.action_executor import ActionExecutor, ActionOutcome


def _make_envelope():
    from core.contracts.decision_envelope import (
        DecisionEnvelope,
        DecisionType,
        DecisionSource,
        OriginComponent,
    )

    return DecisionEnvelope(
        envelope_id="env-1",
        trace_id="t-1",
        parent_envelope_id=None,
        decision_type=DecisionType.EXTRACTION,
        source=DecisionSource.HEURISTIC,
        origin_component=OriginComponent.EMAIL_TRIAGE_EXTRACTION,
        payload={},
        confidence=0.9,
        created_at_epoch=time.time(),
        created_at_monotonic=time.monotonic(),
        causal_seq=1,
        config_version="v1",
    )


def _make_verdict():
    from core.contracts.policy_gate import PolicyVerdict, VerdictAction

    return PolicyVerdict(
        allowed=True,
        action=VerdictAction.ALLOW,
        reason="test",
        conditions=(),
        envelope_id="env-1",
        gate_name="test",
        created_at_epoch=time.time(),
        created_at_monotonic=time.monotonic(),
    )


class TestReasoningProviderProtocol:
    @pytest.mark.asyncio
    async def test_conforming_class_passes(self):
        """A class with reason() and provider_name satisfies the protocol."""
        from core.contracts.decision_envelope import DecisionSource

        class MockProvider:
            @property
            def provider_name(self) -> DecisionSource:
                return DecisionSource.HEURISTIC

            async def reason(self, prompt, context, deadline=None):
                return _make_envelope()

        provider = MockProvider()
        assert isinstance(provider, ReasoningProvider)

        envelope = await provider.reason("test prompt", {"key": "value"})
        assert envelope.envelope_id == "env-1"

    def test_non_conforming_class_fails(self):
        """An empty class does NOT satisfy the ReasoningProvider protocol."""

        class Empty:
            pass

        assert not isinstance(Empty(), ReasoningProvider)


class TestActionExecutorProtocol:
    @pytest.mark.asyncio
    async def test_conforming_class_passes(self):
        """A class with execute() satisfies the ActionExecutor protocol."""

        class MockExecutor:
            async def execute(self, envelope, verdict, commit_id):
                return ActionOutcome.SUCCESS

        executor = MockExecutor()
        assert isinstance(executor, ActionExecutor)

        outcome = await executor.execute(
            _make_envelope(), _make_verdict(), "commit-1"
        )
        assert outcome is ActionOutcome.SUCCESS

    def test_non_conforming_class_fails(self):
        """An empty class does NOT satisfy the ActionExecutor protocol."""

        class Empty:
            pass

        assert not isinstance(Empty(), ActionExecutor)

    def test_action_outcome_members(self):
        """ActionOutcome has exactly SUCCESS, PARTIAL, FAILED, SKIPPED with correct string values."""
        assert ActionOutcome.SUCCESS.value == "success"
        assert ActionOutcome.PARTIAL.value == "partial"
        assert ActionOutcome.FAILED.value == "failed"
        assert ActionOutcome.SKIPPED.value == "skipped"
