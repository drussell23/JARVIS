"""tests/governance/autonomy/test_autonomy_types.py

TDD tests for Command & Event Envelopes (Task 1: Shared Infrastructure).

Covers:
- CommandEnvelope: auto UUID, deterministic idempotency key, is_expired, frozen
- EventEnvelope: auto UUID, frozen
- ContractGate (envelope-level): validates current version, rejects unknown
- IdempotencyLRU: first-seen false, second-seen true, eviction at capacity
"""
from __future__ import annotations

import time

import pytest


# ---------------------------------------------------------------------------
# CommandEnvelope
# ---------------------------------------------------------------------------


class TestCommandEnvelope:
    def test_auto_generates_command_id(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandEnvelope,
            CommandType,
        )

        env = CommandEnvelope(
            source_layer="L2",
            target_layer="L1",
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload={"key": "value"},
            ttl_s=30.0,
        )
        assert env.command_id  # non-empty string
        # UUID4 format: 8-4-4-4-12 hex chars
        parts = env.command_id.split("-")
        assert len(parts) == 5

    def test_auto_generates_idempotency_key(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandEnvelope,
            CommandType,
        )

        env = CommandEnvelope(
            source_layer="L2",
            target_layer="L1",
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload={"key": "value"},
            ttl_s=30.0,
        )
        assert env.idempotency_key  # non-empty string

    def test_same_payload_same_idempotency_key(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandEnvelope,
            CommandType,
        )

        kwargs = dict(
            source_layer="L2",
            target_layer="L1",
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload={"key": "value"},
            ttl_s=30.0,
        )
        env_a = CommandEnvelope(**kwargs)
        env_b = CommandEnvelope(**kwargs)
        assert env_a.idempotency_key == env_b.idempotency_key

    def test_different_payload_different_idempotency_key(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandEnvelope,
            CommandType,
        )

        base = dict(
            source_layer="L2",
            target_layer="L1",
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            ttl_s=30.0,
        )
        env_a = CommandEnvelope(payload={"key": "alpha"}, **base)
        env_b = CommandEnvelope(payload={"key": "beta"}, **base)
        assert env_a.idempotency_key != env_b.idempotency_key

    def test_is_expired_false_when_fresh(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandEnvelope,
            CommandType,
        )

        env = CommandEnvelope(
            source_layer="L2",
            target_layer="L1",
            command_type=CommandType.ADJUST_BRAIN_HINT,
            payload={},
            ttl_s=60.0,
        )
        assert env.is_expired() is False

    def test_is_expired_true_when_stale(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandEnvelope,
            CommandType,
        )

        env = CommandEnvelope(
            source_layer="L2",
            target_layer="L1",
            command_type=CommandType.ADJUST_BRAIN_HINT,
            payload={},
            ttl_s=0.0,  # Immediately expired
        )
        # Even with ttl_s=0, monotonic_ns advances by at least a few ns
        # between construction and this call.
        assert env.is_expired() is True

    def test_frozen(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandEnvelope,
            CommandType,
        )

        env = CommandEnvelope(
            source_layer="L2",
            target_layer="L1",
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload={"key": "value"},
            ttl_s=30.0,
        )
        with pytest.raises(AttributeError):
            env.source_layer = "L3"  # type: ignore[misc]

    def test_priority_property(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandEnvelope,
            CommandType,
        )

        safety_cmd = CommandEnvelope(
            source_layer="L3",
            target_layer="L1",
            command_type=CommandType.REPORT_ROLLBACK_CAUSE,
            payload={},
            ttl_s=30.0,
        )
        learning_cmd = CommandEnvelope(
            source_layer="L2",
            target_layer="L1",
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload={},
            ttl_s=30.0,
        )
        # Lower priority number = higher urgency
        assert safety_cmd.priority <= learning_cmd.priority

    def test_schema_version_default(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandEnvelope,
            CommandType,
        )

        env = CommandEnvelope(
            source_layer="L2",
            target_layer="L1",
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload={},
            ttl_s=30.0,
        )
        assert env.schema_version  # non-empty default

    def test_issued_at_ns_auto_set(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandEnvelope,
            CommandType,
        )

        before = time.monotonic_ns()
        env = CommandEnvelope(
            source_layer="L2",
            target_layer="L1",
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload={},
            ttl_s=30.0,
        )
        after = time.monotonic_ns()
        assert before <= env.issued_at_ns <= after


# ---------------------------------------------------------------------------
# EventEnvelope
# ---------------------------------------------------------------------------


class TestEventEnvelope:
    def test_auto_generates_event_id(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventEnvelope,
            EventType,
        )

        env = EventEnvelope(
            source_layer="L1",
            event_type=EventType.OP_COMPLETED,
            payload={"op_id": "abc"},
        )
        assert env.event_id
        parts = env.event_id.split("-")
        assert len(parts) == 5

    def test_frozen(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventEnvelope,
            EventType,
        )

        env = EventEnvelope(
            source_layer="L1",
            event_type=EventType.OP_COMPLETED,
            payload={"op_id": "abc"},
        )
        with pytest.raises(AttributeError):
            env.source_layer = "L2"  # type: ignore[misc]

    def test_emitted_at_ns_auto_set(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventEnvelope,
            EventType,
        )

        before = time.monotonic_ns()
        env = EventEnvelope(
            source_layer="L1",
            event_type=EventType.OP_COMPLETED,
            payload={},
        )
        after = time.monotonic_ns()
        assert before <= env.emitted_at_ns <= after

    def test_op_id_optional(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventEnvelope,
            EventType,
        )

        env_no_op = EventEnvelope(
            source_layer="L1",
            event_type=EventType.HEALTH_PROBE_RESULT,
            payload={},
        )
        assert env_no_op.op_id is None

        env_with_op = EventEnvelope(
            source_layer="L1",
            event_type=EventType.OP_COMPLETED,
            payload={},
            op_id="op-123",
        )
        assert env_with_op.op_id == "op-123"

    def test_schema_version_default(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EventEnvelope,
            EventType,
        )

        env = EventEnvelope(
            source_layer="L1",
            event_type=EventType.OP_COMPLETED,
            payload={},
        )
        assert env.schema_version  # non-empty default


# ---------------------------------------------------------------------------
# CommandType enum
# ---------------------------------------------------------------------------


class TestCommandType:
    def test_all_members_present(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import CommandType

        expected = {
            "GENERATE_BACKLOG_ENTRY",
            "ADJUST_BRAIN_HINT",
            "REQUEST_MODE_SWITCH",
            "REPORT_ROLLBACK_CAUSE",
            "SIGNAL_HUMAN_PRESENCE",
            "REQUEST_SAGA_SUBMIT",
            "REPORT_CONSENSUS",
            "RECOMMEND_TIER_CHANGE",
        }
        assert set(CommandType.__members__.keys()) == expected

    def test_is_str_enum(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import CommandType

        assert isinstance(CommandType.GENERATE_BACKLOG_ENTRY, str)


# ---------------------------------------------------------------------------
# EventType enum
# ---------------------------------------------------------------------------


class TestEventType:
    def test_all_members_present(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import EventType

        expected = {
            "OP_COMPLETED",
            "OP_ROLLED_BACK",
            "TRUST_TIER_CHANGED",
            "DEGRADATION_MODE_CHANGED",
            "HEALTH_PROBE_RESULT",
            "CURRICULUM_PUBLISHED",
            "ATTRIBUTION_SCORED",
            "ROLLBACK_ANALYZED",
            "INCIDENT_DETECTED",
            "SAGA_STATE_CHANGED",
        }
        assert set(EventType.__members__.keys()) == expected

    def test_is_str_enum(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import EventType

        assert isinstance(EventType.OP_COMPLETED, str)


# ---------------------------------------------------------------------------
# FAILURE_PRECEDENCE
# ---------------------------------------------------------------------------


class TestFailurePrecedence:
    def test_all_command_types_have_precedence(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandType,
            FAILURE_PRECEDENCE,
        )

        for ct in CommandType:
            assert ct in FAILURE_PRECEDENCE, f"Missing precedence for {ct}"

    def test_safety_highest_learning_lowest(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandType,
            FAILURE_PRECEDENCE,
        )

        # REPORT_ROLLBACK_CAUSE is safety-critical (low number = high priority)
        # GENERATE_BACKLOG_ENTRY is learning (high number = low priority)
        assert FAILURE_PRECEDENCE[CommandType.REPORT_ROLLBACK_CAUSE] < FAILURE_PRECEDENCE[
            CommandType.GENERATE_BACKLOG_ENTRY
        ]

    def test_values_are_ints(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import FAILURE_PRECEDENCE

        for val in FAILURE_PRECEDENCE.values():
            assert isinstance(val, int)


# ---------------------------------------------------------------------------
# EnvelopeContractGate (envelope-level schema validation)
# ---------------------------------------------------------------------------


class TestEnvelopeContractGate:
    def test_validates_current_version(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EnvelopeContractGate,
        )

        gate = EnvelopeContractGate()
        # The gate should accept its own current/supported version
        assert gate.validate("1.0") is True

    def test_rejects_unknown_version(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EnvelopeContractGate,
        )

        gate = EnvelopeContractGate()
        assert gate.validate("999.999") is False

    def test_custom_supported_set(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            EnvelopeContractGate,
        )

        gate = EnvelopeContractGate(supported_versions=frozenset({"2.0", "2.1"}))
        assert gate.validate("2.0") is True
        assert gate.validate("2.1") is True
        assert gate.validate("1.0") is False


# ---------------------------------------------------------------------------
# IdempotencyLRU
# ---------------------------------------------------------------------------


class TestIdempotencyLRU:
    def test_first_seen_returns_false(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import IdempotencyLRU

        lru = IdempotencyLRU(capacity=16)
        assert lru.seen("key-1") is False

    def test_second_seen_returns_true(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import IdempotencyLRU

        lru = IdempotencyLRU(capacity=16)
        lru.seen("key-1")
        assert lru.seen("key-1") is True

    def test_eviction_at_capacity(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import IdempotencyLRU

        lru = IdempotencyLRU(capacity=3)
        lru.seen("a")
        lru.seen("b")
        lru.seen("c")
        # Cache is now full: [a, b, c]
        lru.seen("d")  # should evict "a" (oldest) -> [b, c, d]
        # "a" was evicted, so it's "new" again
        assert lru.seen("a") is False  # inserts "a", evicts "b" -> [c, d, a]
        # "c" and "d" should still be there
        assert lru.seen("c") is True
        assert lru.seen("d") is True
        # "b" was evicted by the "a" re-insertion
        assert lru.seen("b") is False

    def test_eviction_respects_access_order(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import IdempotencyLRU

        lru = IdempotencyLRU(capacity=3)
        lru.seen("a")
        lru.seen("b")
        lru.seen("c")
        # Re-access "a" to make it most-recently-used
        lru.seen("a")  # returns True, but also refreshes "a"
        lru.seen("d")  # should evict "b" (now oldest)
        # "a" was refreshed, should still be present
        assert lru.seen("a") is True
        # "b" was evicted
        assert lru.seen("b") is False

    def test_capacity_of_zero_always_unseen(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import IdempotencyLRU

        lru = IdempotencyLRU(capacity=0)
        lru.seen("a")
        # With zero capacity, nothing is retained
        assert lru.seen("a") is False

    def test_len(self):
        from backend.core.ouroboros.governance.autonomy.autonomy_types import IdempotencyLRU

        lru = IdempotencyLRU(capacity=10)
        assert len(lru) == 0
        lru.seen("a")
        assert len(lru) == 1
        lru.seen("a")  # duplicate, no size change
        assert len(lru) == 1
        lru.seen("b")
        assert len(lru) == 2
