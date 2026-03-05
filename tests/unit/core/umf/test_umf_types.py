"""Tests for the UMF canonical envelope types module.

Covers envelope construction, serialization round-trips, deterministic JSON,
default field values, expiry logic, and enum completeness.
"""
from __future__ import annotations

import json
import time

import pytest

from backend.core.umf.types import (
    Kind,
    MessageSource,
    MessageTarget,
    Priority,
    RejectReason,
    ReserveResult,
    Stream,
    UMF_SCHEMA_VERSION,
    UmfMessage,
)


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
        stream=Stream.event,
        kind=Kind.event,
        source=_make_source(),
        target=_make_target(),
        payload={"action": "test"},
    )
    defaults.update(overrides)
    return UmfMessage(**defaults)


# ── TestUmfEnvelope ─────────────────────────────────────────────────

class TestUmfEnvelope:
    """Eight tests covering envelope construction, serialization, and expiry."""

    def test_envelope_has_all_required_fields(self):
        msg = _make_msg()

        # Required fields
        assert msg.stream == Stream.event
        assert msg.kind == Kind.event
        assert msg.source.repo == "jarvis-ai-agent"
        assert msg.target.component == "event_bus"
        assert msg.payload == {"action": "test"}

        # Auto-generated fields
        assert msg.schema_version == UMF_SCHEMA_VERSION
        assert len(msg.message_id) == 32  # uuid4 hex (no dashes)
        assert msg.idempotency_key == msg.message_id
        assert msg.observed_at_unix_ms > 0

        # Routing defaults
        assert msg.routing_partition_key == "jarvis-ai-agent.supervisor"
        assert msg.routing_priority == Priority.normal
        assert msg.routing_ttl_ms == 30_000
        assert msg.routing_deadline_unix_ms == 0

        # Causality defaults
        assert len(msg.causality_trace_id) == 16
        assert len(msg.causality_span_id) == 8
        assert msg.causality_parent_message_id is None
        assert msg.causality_sequence == 0

        # Contract defaults
        assert msg.contract_capability_hash == ""
        assert msg.contract_schema_hash == ""
        assert msg.contract_compat_window == "N|N-1"

        # Signature defaults
        assert msg.signature_alg == ""
        assert msg.signature_key_id == ""
        assert msg.signature_value == ""

    def test_envelope_serialization_roundtrip(self):
        original = _make_msg()
        d = original.to_dict()
        restored = UmfMessage.from_dict(d)

        assert restored.message_id == original.message_id
        assert restored.stream == original.stream
        assert restored.kind == original.kind
        assert restored.source.repo == original.source.repo
        assert restored.source.component == original.source.component
        assert restored.source.instance_id == original.source.instance_id
        assert restored.source.session_id == original.source.session_id
        assert restored.target.repo == original.target.repo
        assert restored.target.component == original.target.component
        assert restored.payload == original.payload
        assert restored.schema_version == original.schema_version
        assert restored.idempotency_key == original.idempotency_key
        assert restored.routing_partition_key == original.routing_partition_key
        assert restored.routing_priority == original.routing_priority
        assert restored.routing_ttl_ms == original.routing_ttl_ms
        assert restored.routing_deadline_unix_ms == original.routing_deadline_unix_ms
        assert restored.causality_trace_id == original.causality_trace_id
        assert restored.causality_span_id == original.causality_span_id
        assert restored.causality_parent_message_id == original.causality_parent_message_id
        assert restored.causality_sequence == original.causality_sequence
        assert restored.observed_at_unix_ms == original.observed_at_unix_ms

    def test_envelope_json_deterministic(self):
        msg = _make_msg()
        j1 = msg.to_json()
        j2 = msg.to_json()
        assert j1 == j2

        # Verify sorted keys for determinism
        parsed = json.loads(j1)
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_default_routing_fields(self):
        msg = _make_msg()
        assert msg.routing_partition_key == "jarvis-ai-agent.supervisor"
        assert msg.routing_priority == Priority.normal
        assert msg.routing_ttl_ms == 30_000
        assert msg.routing_deadline_unix_ms == 0

    def test_causality_fields_default_none(self):
        msg = _make_msg()
        # trace_id and span_id are auto-generated (not None)
        assert msg.causality_trace_id is not None
        assert len(msg.causality_trace_id) == 16
        assert msg.causality_span_id is not None
        assert len(msg.causality_span_id) == 8
        # parent is None by default
        assert msg.causality_parent_message_id is None

    def test_signature_fields_default_empty(self):
        msg = _make_msg()
        assert msg.signature_alg == ""
        assert msg.signature_key_id == ""
        assert msg.signature_value == ""

    def test_is_expired_respects_ttl(self):
        # Create a message with observed_at far in the past and short TTL
        past_ms = int(time.time() * 1000) - 60_000  # 60 seconds ago
        msg = _make_msg(
            observed_at_unix_ms=past_ms,
            routing_ttl_ms=1_000,  # 1 second TTL
        )
        assert msg.is_expired() is True

    def test_not_expired_within_ttl(self):
        msg = _make_msg()  # created now, 30s TTL
        assert msg.is_expired() is False


# ── TestReasonCode ──────────────────────────────────────────────────

class TestReasonCode:
    """Ensure all ten reject reasons exist."""

    def test_all_reason_codes_exist(self):
        expected = {
            "schema_mismatch",
            "sig_invalid",
            "capability_mismatch",
            "ttl_expired",
            "deadline_expired",
            "dedup_duplicate",
            "route_unavailable",
            "backpressure_drop",
            "circuit_open",
            "handler_timeout",
        }
        actual = {member.name for member in RejectReason}
        assert actual == expected


# ── TestStreamAndKind ───────────────────────────────────────────────

class TestStreamAndKind:
    """Ensure Stream and Kind enums have the correct members."""

    def test_stream_values(self):
        expected = {"lifecycle", "command", "event", "heartbeat", "telemetry"}
        actual = {member.name for member in Stream}
        assert actual == expected

    def test_kind_values(self):
        expected = {"command", "event", "heartbeat", "ack", "nack"}
        actual = {member.name for member in Kind}
        assert actual == expected
