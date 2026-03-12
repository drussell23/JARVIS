"""tests/governance/autonomy/test_saga_messages.py

TDD tests for SagaMessage, SagaMessageBus, and factory functions (Task M3).

Covers:
- SagaMessage auto-id, TTL expiry, serialization roundtrip
- SagaMessageBus pub-sub, filtering, pruning, capacity
- Factory functions for common message patterns
"""
from __future__ import annotations

import time

import pytest


# ============================================================================
# SagaMessage tests
# ============================================================================


class TestSagaMessage:
    def test_message_has_auto_id(self):
        """message_id is populated automatically."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
        )

        msg = SagaMessage(saga_id="saga-1", source_repo="jarvis")
        assert msg.message_id
        assert isinstance(msg.message_id, str)
        assert len(msg.message_id) == 16

    def test_is_expired_false_new_message(self):
        """A freshly created message should not be expired."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
        )

        msg = SagaMessage(saga_id="saga-1", source_repo="jarvis", ttl_s=300.0)
        assert msg.is_expired() is False

    def test_is_expired_true_zero_ttl(self):
        """A message with ttl_s=0 should be expired after a brief sleep."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
        )

        msg = SagaMessage(saga_id="saga-1", source_repo="jarvis", ttl_s=0.0)
        # Small sleep to ensure monotonic_ns advances
        time.sleep(0.001)
        assert msg.is_expired() is True

    def test_to_dict_roundtrip(self):
        """to_dict then from_dict should produce an equivalent message."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            MessagePriority,
            SagaMessage,
            SagaMessageType,
        )

        original = SagaMessage(
            saga_id="saga-42",
            source_repo="jarvis",
            target_repo="prime",
            message_type=SagaMessageType.REPO_APPLY_REQUEST,
            priority=MessagePriority.HIGH,
            payload={"file": "main.py", "diff": "+line"},
            correlation_id="corr-99",
            ttl_s=120.0,
        )

        data = original.to_dict()
        restored = SagaMessage.from_dict(data)

        assert restored.message_id == original.message_id
        assert restored.message_type == original.message_type
        assert restored.saga_id == original.saga_id
        assert restored.source_repo == original.source_repo
        assert restored.target_repo == original.target_repo
        assert restored.priority == original.priority
        assert restored.payload == original.payload
        assert restored.correlation_id == original.correlation_id
        assert restored.ttl_s == original.ttl_s
        assert restored.timestamp_ns == original.timestamp_ns

    def test_to_dict_has_expected_keys(self):
        """Verify all expected keys are present in the serialized dict."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
        )

        msg = SagaMessage(saga_id="saga-1", source_repo="jarvis")
        data = msg.to_dict()

        expected_keys = {
            "message_id",
            "message_type",
            "saga_id",
            "source_repo",
            "target_repo",
            "priority",
            "payload",
            "timestamp_ns",
            "correlation_id",
            "ttl_s",
        }
        assert set(data.keys()) == expected_keys


# ============================================================================
# SagaMessageBus tests
# ============================================================================


class TestSagaMessageBus:
    def test_send_delivers_to_handler(self):
        """subscribe + send should call the handler."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
            SagaMessageBus,
            SagaMessageType,
        )

        bus = SagaMessageBus()
        received = []

        def handler(msg: SagaMessage) -> None:
            received.append(msg)

        bus.subscribe(SagaMessageType.SAGA_CREATED, handler)

        msg = SagaMessage(
            saga_id="saga-1",
            source_repo="jarvis",
            message_type=SagaMessageType.SAGA_CREATED,
        )
        result = bus.send(msg)

        assert result is True
        assert len(received) == 1
        assert received[0].saga_id == "saga-1"

    def test_send_no_handler_returns_false(self):
        """send() with no subscribers should return False."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
            SagaMessageBus,
            SagaMessageType,
        )

        bus = SagaMessageBus()

        msg = SagaMessage(
            saga_id="saga-1",
            source_repo="jarvis",
            message_type=SagaMessageType.SAGA_CREATED,
        )
        result = bus.send(msg)

        assert result is False

    def test_subscribe_multiple_handlers(self):
        """Two handlers subscribed to the same type should both be called."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
            SagaMessageBus,
            SagaMessageType,
        )

        bus = SagaMessageBus()
        calls_a = []
        calls_b = []

        bus.subscribe(SagaMessageType.SAGA_COMPLETED, lambda m: calls_a.append(m))
        bus.subscribe(SagaMessageType.SAGA_COMPLETED, lambda m: calls_b.append(m))

        msg = SagaMessage(
            saga_id="saga-1",
            source_repo="jarvis",
            message_type=SagaMessageType.SAGA_COMPLETED,
        )
        bus.send(msg)

        assert len(calls_a) == 1
        assert len(calls_b) == 1

    def test_get_messages_filter_by_saga(self):
        """Filtering by saga_id returns only matching messages."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
            SagaMessageBus,
            SagaMessageType,
        )

        bus = SagaMessageBus()

        for sid in ["saga-A", "saga-B", "saga-A", "saga-C", "saga-A"]:
            bus.send(
                SagaMessage(
                    saga_id=sid,
                    source_repo="jarvis",
                    message_type=SagaMessageType.SAGA_ADVANCED,
                )
            )

        result = bus.get_messages(saga_id="saga-A")
        assert len(result) == 3
        assert all(m.saga_id == "saga-A" for m in result)

    def test_get_messages_filter_by_type(self):
        """Filtering by message_type returns only matching messages."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
            SagaMessageBus,
            SagaMessageType,
        )

        bus = SagaMessageBus()

        bus.send(
            SagaMessage(
                saga_id="s1",
                source_repo="jarvis",
                message_type=SagaMessageType.SAGA_CREATED,
            )
        )
        bus.send(
            SagaMessage(
                saga_id="s1",
                source_repo="jarvis",
                message_type=SagaMessageType.SAGA_ADVANCED,
            )
        )
        bus.send(
            SagaMessage(
                saga_id="s1",
                source_repo="jarvis",
                message_type=SagaMessageType.SAGA_CREATED,
            )
        )

        result = bus.get_messages(message_type=SagaMessageType.SAGA_CREATED)
        assert len(result) == 2
        assert all(m.message_type == SagaMessageType.SAGA_CREATED for m in result)

    def test_get_messages_limit(self):
        """Limit parameter caps the number of returned messages."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
            SagaMessageBus,
            SagaMessageType,
        )

        bus = SagaMessageBus()

        for i in range(10):
            bus.send(
                SagaMessage(
                    saga_id="s1",
                    source_repo="jarvis",
                    message_type=SagaMessageType.SAGA_ADVANCED,
                    payload={"i": i},
                )
            )

        result = bus.get_messages(limit=3)
        assert len(result) == 3

    def test_get_messages_newest_first(self):
        """get_messages should return newest messages first."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
            SagaMessageBus,
            SagaMessageType,
        )

        bus = SagaMessageBus()

        for i in range(5):
            bus.send(
                SagaMessage(
                    saga_id="s1",
                    source_repo="jarvis",
                    message_type=SagaMessageType.SAGA_ADVANCED,
                    payload={"order": i},
                )
            )

        result = bus.get_messages()
        # Newest first means the last sent message is first in results
        assert result[0].payload["order"] == 4
        assert result[-1].payload["order"] == 0

    def test_get_conversation(self):
        """All messages with the same correlation_id should be returned."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
            SagaMessageBus,
            SagaMessageType,
        )

        bus = SagaMessageBus()

        # 2 messages with correlation_id "conv-1", 1 with "conv-2"
        bus.send(
            SagaMessage(
                saga_id="s1",
                source_repo="jarvis",
                message_type=SagaMessageType.VOTE_REQUEST,
                correlation_id="conv-1",
            )
        )
        bus.send(
            SagaMessage(
                saga_id="s1",
                source_repo="prime",
                message_type=SagaMessageType.VOTE_CAST,
                correlation_id="conv-1",
            )
        )
        bus.send(
            SagaMessage(
                saga_id="s1",
                source_repo="jarvis",
                message_type=SagaMessageType.VOTE_REQUEST,
                correlation_id="conv-2",
            )
        )

        result = bus.get_conversation("conv-1")
        assert len(result) == 2
        assert all(m.correlation_id == "conv-1" for m in result)

        # A third message added to conv-1
        bus.send(
            SagaMessage(
                saga_id="s1",
                source_repo="reactor",
                message_type=SagaMessageType.CONSENSUS_REACHED,
                correlation_id="conv-1",
            )
        )
        result2 = bus.get_conversation("conv-1")
        assert len(result2) == 3

    def test_prune_expired(self):
        """prune_expired should remove expired messages and return count."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
            SagaMessageBus,
            SagaMessageType,
        )

        bus = SagaMessageBus()

        # 5 expired messages
        for i in range(5):
            bus.send(
                SagaMessage(
                    saga_id="s1",
                    source_repo="jarvis",
                    message_type=SagaMessageType.SAGA_ADVANCED,
                    ttl_s=0.0,
                    payload={"expired": True, "i": i},
                )
            )

        # Brief sleep to ensure they expire
        time.sleep(0.002)

        # 5 fresh messages
        for i in range(5):
            bus.send(
                SagaMessage(
                    saga_id="s1",
                    source_repo="jarvis",
                    message_type=SagaMessageType.SAGA_ADVANCED,
                    ttl_s=300.0,
                    payload={"expired": False, "i": i},
                )
            )

        removed = bus.prune_expired()
        assert removed == 5

        # Only fresh messages remain
        remaining = bus.get_messages()
        assert len(remaining) == 5
        assert all(not m.payload.get("expired") for m in remaining)

    def test_max_capacity(self):
        """When bus exceeds max_messages, oldest messages are pruned."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
            SagaMessageBus,
            SagaMessageType,
        )

        bus = SagaMessageBus(max_messages=500)

        for i in range(600):
            bus.send(
                SagaMessage(
                    saga_id="s1",
                    source_repo="jarvis",
                    message_type=SagaMessageType.SAGA_ADVANCED,
                    payload={"i": i},
                )
            )

        remaining = bus.get_messages(limit=600)
        assert len(remaining) <= 500

    def test_to_dict_summary(self):
        """to_dict should include total_messages and handler_count."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessage,
            SagaMessageBus,
            SagaMessageType,
        )

        bus = SagaMessageBus()
        bus.subscribe(SagaMessageType.SAGA_CREATED, lambda m: None)
        bus.subscribe(SagaMessageType.SAGA_COMPLETED, lambda m: None)

        bus.send(
            SagaMessage(
                saga_id="s1",
                source_repo="jarvis",
                message_type=SagaMessageType.SAGA_CREATED,
            )
        )

        summary = bus.to_dict()
        assert "total_messages" in summary
        assert "handler_count" in summary
        assert summary["total_messages"] == 1
        assert summary["handler_count"] == 2


# ============================================================================
# Factory function tests
# ============================================================================


class TestFactoryFunctions:
    def test_create_apply_request(self):
        """create_apply_request should produce correct type, saga_id, repos, payload."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessageType,
            create_apply_request,
        )

        msg = create_apply_request(
            saga_id="saga-7",
            source_repo="jarvis",
            target_repo="prime",
            patch_data={"file": "x.py", "diff": "+hello"},
        )

        assert msg.message_type == SagaMessageType.REPO_APPLY_REQUEST
        assert msg.saga_id == "saga-7"
        assert msg.source_repo == "jarvis"
        assert msg.target_repo == "prime"
        assert msg.payload["file"] == "x.py"
        assert msg.payload["diff"] == "+hello"

    def test_create_vote_request(self):
        """create_vote_request should produce correct type and have a correlation_id."""
        from backend.core.ouroboros.governance.autonomy.saga_messages import (
            SagaMessageType,
            create_vote_request,
        )

        msg = create_vote_request(
            saga_id="saga-8",
            source_repo="jarvis",
            op_id="op-42",
        )

        assert msg.message_type == SagaMessageType.VOTE_REQUEST
        assert msg.saga_id == "saga-8"
        assert msg.source_repo == "jarvis"
        assert msg.target_repo is None  # broadcast
        assert msg.correlation_id is not None
        assert len(msg.correlation_id) > 0
        assert msg.payload["op_id"] == "op-42"
