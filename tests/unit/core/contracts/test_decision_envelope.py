"""Tests for DecisionEnvelope, typed enums, and IdempotencyKey builder."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import time

import pytest

from core.contracts.decision_envelope import (
    DecisionEnvelope,
    DecisionSource,
    DecisionType,
    EnvelopeFactory,
    IdempotencyKey,
    OriginComponent,
)


# ---------------------------------------------------------------------------
# DecisionType
# ---------------------------------------------------------------------------


class TestDecisionType:
    def test_all_members_exist(self):
        expected = {"EXTRACTION", "SCORING", "POLICY", "ACTION"}
        actual = {m.name for m in DecisionType}
        assert actual == expected

    def test_is_str_enum(self):
        assert isinstance(DecisionType.EXTRACTION, str)
        assert DecisionType.EXTRACTION == "extraction"
        assert DecisionType.SCORING == "scoring"
        assert DecisionType.POLICY == "policy"
        assert DecisionType.ACTION == "action"


# ---------------------------------------------------------------------------
# DecisionSource
# ---------------------------------------------------------------------------


class TestDecisionSource:
    def test_all_members_exist(self):
        expected = {
            "JPRIME_V1",
            "JPRIME_DEGRADED",
            "HEURISTIC",
            "CLOUD_CLAUDE",
            "LOCAL_PRIME",
            "ADAPTIVE",
        }
        actual = {m.name for m in DecisionSource}
        assert actual == expected


# ---------------------------------------------------------------------------
# OriginComponent
# ---------------------------------------------------------------------------


class TestOriginComponent:
    def test_all_members_exist(self):
        expected = {
            "EMAIL_TRIAGE_RUNNER",
            "EMAIL_TRIAGE_EXTRACTION",
            "EMAIL_TRIAGE_SCORING",
            "EMAIL_TRIAGE_POLICY",
            "EMAIL_TRIAGE_LABELER",
            "EMAIL_TRIAGE_NOTIFIER",
        }
        actual = {m.name for m in OriginComponent}
        assert actual == expected


# ---------------------------------------------------------------------------
# DecisionEnvelope
# ---------------------------------------------------------------------------


class TestDecisionEnvelope:
    @pytest.fixture()
    def sample_envelope(self):
        return DecisionEnvelope(
            envelope_id="env-1",
            trace_id="trace-1",
            parent_envelope_id=None,
            decision_type=DecisionType.SCORING,
            source=DecisionSource.JPRIME_V1,
            origin_component=OriginComponent.EMAIL_TRIAGE_SCORING,
            payload={"score": 0.9},
            confidence=0.95,
            created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(),
            causal_seq=1,
            config_version="v1",
        )

    def test_frozen(self, sample_envelope):
        with pytest.raises(AttributeError):
            sample_envelope.confidence = 0.5  # type: ignore[misc]

    def test_dual_timestamps(self, sample_envelope):
        assert sample_envelope.created_at_epoch > 0
        assert sample_envelope.created_at_monotonic > 0
        # epoch is wall-clock, monotonic is process-relative; both positive
        assert isinstance(sample_envelope.created_at_epoch, float)
        assert isinstance(sample_envelope.created_at_monotonic, float)

    def test_schema_version_defaults(self):
        env = DecisionEnvelope(
            envelope_id="env-2",
            trace_id="trace-2",
            parent_envelope_id=None,
            decision_type=DecisionType.EXTRACTION,
            source=DecisionSource.HEURISTIC,
            origin_component=OriginComponent.EMAIL_TRIAGE_EXTRACTION,
            payload={},
            confidence=0.5,
            created_at_epoch=1.0,
            created_at_monotonic=1.0,
            causal_seq=0,
            config_version="v0",
        )
        assert env.schema_version == 1
        assert env.producer_version == "1.0.0"
        assert env.compat_min_version == 1

    def test_metadata_default_empty(self):
        env = DecisionEnvelope(
            envelope_id="env-3",
            trace_id="trace-3",
            parent_envelope_id=None,
            decision_type=DecisionType.ACTION,
            source=DecisionSource.ADAPTIVE,
            origin_component=OriginComponent.EMAIL_TRIAGE_RUNNER,
            payload={},
            confidence=0.8,
            created_at_epoch=1.0,
            created_at_monotonic=1.0,
            causal_seq=0,
            config_version="v0",
        )
        assert env.metadata == {}

    def test_causal_chaining(self):
        """Parent -> child envelopes have monotonically increasing causal_seq."""
        parent = DecisionEnvelope(
            envelope_id="parent",
            trace_id="trace-chain",
            parent_envelope_id=None,
            decision_type=DecisionType.EXTRACTION,
            source=DecisionSource.JPRIME_V1,
            origin_component=OriginComponent.EMAIL_TRIAGE_EXTRACTION,
            payload={"raw": "data"},
            confidence=0.7,
            created_at_epoch=1.0,
            created_at_monotonic=1.0,
            causal_seq=1,
            config_version="v1",
        )
        child = DecisionEnvelope(
            envelope_id="child",
            trace_id="trace-chain",
            parent_envelope_id="parent",
            decision_type=DecisionType.SCORING,
            source=DecisionSource.JPRIME_V1,
            origin_component=OriginComponent.EMAIL_TRIAGE_SCORING,
            payload={"score": 0.8},
            confidence=0.85,
            created_at_epoch=2.0,
            created_at_monotonic=2.0,
            causal_seq=2,
            config_version="v1",
        )
        assert child.parent_envelope_id == parent.envelope_id
        assert child.causal_seq > parent.causal_seq

    def test_typed_enums_not_strings(self, sample_envelope):
        """Enum fields must be actual enum instances, not bare strings."""
        assert isinstance(sample_envelope.decision_type, DecisionType)
        assert isinstance(sample_envelope.source, DecisionSource)
        assert isinstance(sample_envelope.origin_component, OriginComponent)


# ---------------------------------------------------------------------------
# IdempotencyKey
# ---------------------------------------------------------------------------


class TestIdempotencyKey:
    def test_deterministic(self):
        k1 = IdempotencyKey.build(
            DecisionType.SCORING, "msg-123", "apply_label", "v2"
        )
        k2 = IdempotencyKey.build(
            DecisionType.SCORING, "msg-123", "apply_label", "v2"
        )
        assert k1.key == k2.key

    def test_different_inputs_different_keys(self):
        k1 = IdempotencyKey.build(
            DecisionType.SCORING, "msg-123", "apply_label", "v2"
        )
        k2 = IdempotencyKey.build(
            DecisionType.ACTION, "msg-123", "apply_label", "v2"
        )
        assert k1.key != k2.key

    def test_key_length(self):
        k = IdempotencyKey.build(
            DecisionType.POLICY, "target-abc", "notify", "v1"
        )
        assert len(k.key) == 32

    def test_frozen(self):
        k = IdempotencyKey.build(
            DecisionType.EXTRACTION, "t-1", "extract", "v1"
        )
        with pytest.raises(AttributeError):
            k.key = "tampered"  # type: ignore[misc]
