"""Tests for PolicyGate protocol, PolicyVerdict, and VerdictAction enum."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import time

import pytest

from core.contracts.decision_envelope import (
    DecisionEnvelope,
    DecisionSource,
    DecisionType,
    OriginComponent,
)
from core.contracts.policy_gate import PolicyGate, PolicyVerdict, VerdictAction


# ---------------------------------------------------------------------------
# VerdictAction
# ---------------------------------------------------------------------------


class TestVerdictAction:
    def test_all_members_exist(self):
        expected = {"ALLOW", "DENY", "DEFER"}
        actual = {m.name for m in VerdictAction}
        assert actual == expected

    def test_is_str_enum(self):
        assert isinstance(VerdictAction.ALLOW, str)
        assert VerdictAction.ALLOW == "allow"
        assert VerdictAction.DENY == "deny"
        assert VerdictAction.DEFER == "defer"


# ---------------------------------------------------------------------------
# PolicyVerdict
# ---------------------------------------------------------------------------


class TestPolicyVerdict:
    def test_frozen(self):
        verdict = PolicyVerdict(
            allowed=True,
            action=VerdictAction.ALLOW,
            reason="passed all checks",
            conditions=(),
            envelope_id="env-1",
            gate_name="test_gate",
            created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(),
        )
        with pytest.raises(AttributeError):
            verdict.allowed = False  # type: ignore[misc]

    def test_deny_verdict(self):
        now_epoch = time.time()
        now_mono = time.monotonic()
        verdict = PolicyVerdict(
            allowed=False,
            action=VerdictAction.DENY,
            reason="quota exceeded",
            conditions=(),
            envelope_id="env-deny",
            gate_name="quota_gate",
            created_at_epoch=now_epoch,
            created_at_monotonic=now_mono,
        )
        assert verdict.allowed is False
        assert verdict.action is VerdictAction.DENY
        assert verdict.reason == "quota exceeded"
        assert verdict.conditions == ()
        assert verdict.envelope_id == "env-deny"
        assert verdict.gate_name == "quota_gate"
        assert verdict.created_at_epoch == now_epoch
        assert verdict.created_at_monotonic == now_mono

    def test_defer_verdict_with_conditions(self):
        conditions = ("needs_human_review", "rate_limit_approaching")
        verdict = PolicyVerdict(
            allowed=False,
            action=VerdictAction.DEFER,
            reason="action requires human approval",
            conditions=conditions,
            envelope_id="env-defer",
            gate_name="approval_gate",
            created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(),
        )
        assert verdict.allowed is False
        assert verdict.action is VerdictAction.DEFER
        assert verdict.conditions == ("needs_human_review", "rate_limit_approaching")
        assert len(verdict.conditions) == 2


# ---------------------------------------------------------------------------
# PolicyGate Protocol
# ---------------------------------------------------------------------------


class TestPolicyGateProtocol:
    @pytest.mark.asyncio
    async def test_protocol_conformance(self):
        """A class implementing evaluate() satisfies the PolicyGate protocol."""

        class MockGate:
            async def evaluate(self, envelope, context):
                return PolicyVerdict(
                    allowed=True,
                    action=VerdictAction.ALLOW,
                    reason="mock pass",
                    conditions=(),
                    envelope_id=envelope.envelope_id,
                    gate_name="mock_gate",
                    created_at_epoch=time.time(),
                    created_at_monotonic=time.monotonic(),
                )

        gate = MockGate()
        assert isinstance(gate, PolicyGate)

        envelope = DecisionEnvelope(
            envelope_id="env-proto",
            trace_id="trace-proto",
            parent_envelope_id=None,
            decision_type=DecisionType.POLICY,
            source=DecisionSource.HEURISTIC,
            origin_component=OriginComponent.EMAIL_TRIAGE_POLICY,
            payload={"action": "send_email"},
            confidence=0.9,
            created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(),
            causal_seq=1,
            config_version="v1",
        )

        result = await gate.evaluate(envelope, {"user": "derek"})
        assert isinstance(result, PolicyVerdict)
        assert result.allowed is True
        assert result.envelope_id == "env-proto"

    def test_non_conforming_class_fails(self):
        """A class without evaluate() must NOT satisfy PolicyGate."""

        class NotAGate:
            def wrong_method(self):
                pass

        obj = NotAGate()
        assert not isinstance(obj, PolicyGate)
