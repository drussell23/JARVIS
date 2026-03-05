"""Tests for the UMF contract gate validation module.

Covers schema-version acceptance, TTL expiry, deadline expiry,
capability-hash mismatch, HMAC signature validation, and result structure.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from backend.core.umf.types import (
    Kind,
    MessageSource,
    MessageTarget,
    RejectReason,
    Stream,
    UMF_SCHEMA_VERSION,
    UmfMessage,
)

from backend.core.umf.contract_gate import (
    ValidationResult,
    validate_message,
    _ACCEPTED_SCHEMAS,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _make_source(**overrides) -> MessageSource:
    defaults = dict(
        repo="jarvis-ai-agent",
        component="supervisor",
        instance_id="inst-001",
        session_id="sess-abc",
    )
    defaults.update(overrides)
    return MessageSource(**defaults)


def _make_target(**overrides) -> MessageTarget:
    defaults = dict(repo="reactor-core", component="event_bus")
    defaults.update(overrides)
    return MessageTarget(**defaults)


def _make_msg(**overrides) -> UmfMessage:
    defaults = dict(
        stream=Stream.command,
        kind=Kind.command,
        source=_make_source(),
        target=_make_target(),
        payload={"action": "test"},
    )
    defaults.update(overrides)
    return UmfMessage(**defaults)


# ── TestUmfContractGate ───────────────────────────────────────────────


class TestUmfContractGate:
    """Ten tests covering contract gate validation logic."""

    def test_valid_message_passes(self):
        """A well-formed message with default schema passes validation."""
        msg = _make_msg()
        result = validate_message(msg)

        assert result.accepted is True
        assert result.reject_reason is None
        assert result.message_id == msg.message_id

    def test_unknown_schema_version_rejected(self):
        """A message with a schema version not in accepted set is rejected."""
        msg = _make_msg(schema_version="umf.v99")
        result = validate_message(msg)

        assert result.accepted is False
        assert result.reject_reason == RejectReason.schema_mismatch.value

    def test_n_minus_1_schema_accepted(self):
        """Schema version 'umf.v1' (the current version) is in the accepted set."""
        # Confirm current schema is accepted
        assert UMF_SCHEMA_VERSION in _ACCEPTED_SCHEMAS
        msg = _make_msg(schema_version=UMF_SCHEMA_VERSION)
        result = validate_message(msg)

        assert result.accepted is True
        assert result.reject_reason is None

    def test_expired_ttl_rejected(self):
        """A message with observed_at far in the past and short TTL is rejected."""
        past_ms = int(time.time() * 1000) - 120_000  # 2 minutes ago
        msg = _make_msg(
            observed_at_unix_ms=past_ms,
            routing_ttl_ms=1_000,  # 1 second TTL -- long expired
        )
        result = validate_message(msg)

        assert result.accepted is False
        assert result.reject_reason == RejectReason.ttl_expired.value

    def test_expired_deadline_rejected(self):
        """A message with a routing deadline in the past is rejected."""
        past_deadline_ms = int(time.time() * 1000) - 60_000  # 1 minute ago
        msg = _make_msg(routing_deadline_unix_ms=past_deadline_ms)
        result = validate_message(msg)

        assert result.accepted is False
        assert result.reject_reason == RejectReason.deadline_expired.value

    def test_capability_hash_mismatch_rejected(self):
        """Message with a capability hash that doesn't match expected is rejected."""
        msg = _make_msg(contract_capability_hash="abc123")
        result = validate_message(msg, expected_capability_hash="xyz789")

        assert result.accepted is False
        assert result.reject_reason == RejectReason.capability_mismatch.value

    def test_capability_hash_not_checked_when_not_required(self):
        """When no expected hash is provided, capability hash is not checked."""
        msg = _make_msg(contract_capability_hash="any-hash-value")
        result = validate_message(msg)

        assert result.accepted is True
        assert result.reject_reason is None

    def test_hmac_invalid_rejected(self):
        """A signed message with a wrong secret is rejected."""
        msg = _make_msg(
            signature_alg="hmac-sha256",
            signature_key_id="key1",
            signature_value="bad-signature-value",
        )
        result = validate_message(
            msg,
            hmac_secret="correct-secret",
            session_id="sess-abc",
        )

        assert result.accepted is False
        assert result.reject_reason == RejectReason.sig_invalid.value

    def test_unsigned_message_passes_when_no_secret(self):
        """An unsigned message passes when no HMAC secret is configured."""
        msg = _make_msg()  # No signature fields set
        result = validate_message(msg)

        assert result.accepted is True
        assert result.reject_reason is None

    def test_result_includes_message_id(self):
        """ValidationResult always contains the message_id from the message."""
        msg = _make_msg(message_id="deadbeef01234567890abcdef0123456")
        result = validate_message(msg)

        assert result.message_id == "deadbeef01234567890abcdef0123456"
