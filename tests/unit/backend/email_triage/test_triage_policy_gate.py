"""Tests for TriagePolicyGate wrapping NotificationPolicy."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest

from core.contracts.decision_envelope import (
    DecisionEnvelope, DecisionType, DecisionSource, OriginComponent,
)
from core.contracts.policy_gate import PolicyGate, VerdictAction
from core.contracts.policy_context import PolicyContext
from autonomy.email_triage.config import TriageConfig


def _make_scoring_envelope(message_id="msg-1", score=90, tier=1):
    return DecisionEnvelope(
        envelope_id="env-score-1", trace_id="trace-1",
        parent_envelope_id="env-extract-1",
        decision_type=DecisionType.SCORING,
        source=DecisionSource.HEURISTIC,
        origin_component=OriginComponent.EMAIL_TRIAGE_SCORING,
        payload={"message_id": message_id, "score": score, "tier": tier},
        confidence=1.0,
        created_at_epoch=time.time(),
        created_at_monotonic=time.monotonic(),
        causal_seq=2, config_version="v1",
    )


def _make_context(message_id="msg-1", tier=1, score=90):
    return PolicyContext(
        tier=tier, score=score, message_id=message_id,
        sender_domain="example.com", is_reply=False,
        has_attachment=False, label_ids=("INBOX",),
        cycle_id="cycle-1", fencing_token=1,
        config_version="v1",
    )


class TestTriagePolicyGateProtocol:
    def test_satisfies_policy_gate_protocol(self):
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy
        config = TriageConfig(enabled=True)
        policy = NotificationPolicy(config)
        gate = TriagePolicyGate(policy, config)
        assert isinstance(gate, PolicyGate)


class TestTriagePolicyGateEvaluation:
    @pytest.mark.asyncio
    async def test_tier1_allows_immediate(self):
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy
        config = TriageConfig(enabled=True, notify_tier1=True)
        policy = NotificationPolicy(config)
        gate = TriagePolicyGate(policy, config)

        envelope = _make_scoring_envelope(score=90, tier=1)
        context = _make_context(tier=1, score=90)
        verdict = await gate.evaluate(envelope, context)
        assert verdict.allowed is True
        assert verdict.action == VerdictAction.ALLOW

    @pytest.mark.asyncio
    async def test_tier3_denies(self):
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy
        config = TriageConfig(enabled=True)
        policy = NotificationPolicy(config)
        gate = TriagePolicyGate(policy, config)

        envelope = _make_scoring_envelope(score=40, tier=3)
        context = _make_context(tier=3, score=40)
        verdict = await gate.evaluate(envelope, context)
        assert verdict.allowed is False
        assert verdict.action == VerdictAction.DENY

    @pytest.mark.asyncio
    async def test_tier4_denies(self):
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy
        config = TriageConfig(enabled=True, quarantine_tier4=False)
        policy = NotificationPolicy(config)
        gate = TriagePolicyGate(policy, config)

        envelope = _make_scoring_envelope(score=10, tier=4)
        context = _make_context(tier=4, score=10)
        verdict = await gate.evaluate(envelope, context)
        assert verdict.allowed is False

    @pytest.mark.asyncio
    async def test_verdict_has_envelope_id(self):
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy
        config = TriageConfig(enabled=True)
        policy = NotificationPolicy(config)
        gate = TriagePolicyGate(policy, config)

        envelope = _make_scoring_envelope()
        context = _make_context()
        verdict = await gate.evaluate(envelope, context)
        assert verdict.envelope_id == envelope.envelope_id
        assert verdict.gate_name == "triage_policy"

    @pytest.mark.asyncio
    async def test_verdict_has_dual_timestamps(self):
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy
        config = TriageConfig(enabled=True)
        policy = NotificationPolicy(config)
        gate = TriagePolicyGate(policy, config)

        before_epoch = time.time()
        before_mono = time.monotonic()
        envelope = _make_scoring_envelope()
        context = _make_context()
        verdict = await gate.evaluate(envelope, context)
        assert verdict.created_at_epoch >= before_epoch
        assert verdict.created_at_monotonic >= before_mono
