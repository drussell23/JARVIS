"""Tests for IntentEnvelope schema 2c.1."""
import time
import pytest
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    EnvelopeValidationError,
    make_envelope,
    SCHEMA_VERSION,
)


def _valid_kwargs(**overrides):
    base = dict(
        source="backlog",
        description="fix the auth module",
        target_files=("backend/core/auth.py",),
        repo="jarvis",
        confidence=0.8,
        urgency="normal",
        evidence={"task_id": "t-001"},
        requires_human_ack=False,
    )
    base.update(overrides)
    return base


def test_schema_version_constant():
    assert SCHEMA_VERSION == "2c.1"


def test_make_envelope_happy_path():
    env = make_envelope(**_valid_kwargs())
    assert env.schema_version == "2c.1"
    assert env.source == "backlog"
    assert env.target_files == ("backend/core/auth.py",)
    assert env.confidence == 0.8
    assert isinstance(env.causal_id, str) and len(env.causal_id) > 0
    assert isinstance(env.signal_id, str) and len(env.signal_id) > 0
    assert isinstance(env.idempotency_key, str) and len(env.idempotency_key) > 0
    assert env.lease_id == ""  # set by router at enqueue
    assert env.submitted_at > 0.0


def test_make_envelope_auto_dedup_key():
    env1 = make_envelope(**_valid_kwargs())
    env2 = make_envelope(**_valid_kwargs())
    # Same source/target/evidence → same dedup_key
    assert env1.dedup_key == env2.dedup_key
    # But different signal_id / causal_id / idempotency_key
    assert env1.signal_id != env2.signal_id


def test_envelope_immutable():
    env = make_envelope(**_valid_kwargs())
    with pytest.raises((AttributeError, TypeError)):
        env.source = "voice_human"  # type: ignore


def test_invalid_schema_version_rejected():
    with pytest.raises(EnvelopeValidationError, match="schema_version"):
        IntentEnvelope(
            schema_version="1.0",
            source="backlog",
            description="x",
            target_files=("a.py",),
            repo="jarvis",
            confidence=0.5,
            urgency="normal",
            dedup_key="abc",
            causal_id="cid",
            signal_id="sid",
            idempotency_key="ikey",
            lease_id="",
            evidence={},
            requires_human_ack=False,
            submitted_at=1.0,
        )


def test_invalid_source_rejected():
    with pytest.raises(EnvelopeValidationError, match="source"):
        make_envelope(**_valid_kwargs(source="unknown_sensor"))


def test_invalid_urgency_rejected():
    with pytest.raises(EnvelopeValidationError, match="urgency"):
        make_envelope(**_valid_kwargs(urgency="emergency"))


def test_confidence_out_of_range_rejected():
    with pytest.raises(EnvelopeValidationError, match="confidence"):
        make_envelope(**_valid_kwargs(confidence=1.5))


def test_empty_target_files_rejected():
    with pytest.raises(EnvelopeValidationError, match="target_files"):
        make_envelope(**_valid_kwargs(target_files=()))


def test_roundtrip_to_from_dict():
    env = make_envelope(**_valid_kwargs())
    d = env.to_dict()
    env2 = IntentEnvelope.from_dict(d)
    assert env2.schema_version == env.schema_version
    assert env2.source == env.source
    assert env2.target_files == env.target_files
    assert env2.causal_id == env.causal_id
    assert env2.dedup_key == env.dedup_key


def test_from_dict_rejects_unknown_schema_version():
    env = make_envelope(**_valid_kwargs())
    d = env.to_dict()
    d["schema_version"] = "9.9"
    with pytest.raises(EnvelopeValidationError):
        IntentEnvelope.from_dict(d)
