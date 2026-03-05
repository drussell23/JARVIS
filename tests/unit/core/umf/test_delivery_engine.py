"""Tests for the UMF Delivery Engine -- pub/sub routing with dedup and contract gate.

Covers message delivery to subscribers, dedup filtering, TTL rejection,
stream-based routing, and health reporting.
"""
from __future__ import annotations

import time

import pytest
import pytest_asyncio

from backend.core.umf.delivery_engine import DeliveryEngine, PublishResult
from backend.core.umf.types import (
    Kind,
    MessageSource,
    MessageTarget,
    Stream,
    UmfMessage,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _make_source() -> MessageSource:
    return MessageSource(
        repo="jarvis",
        component="test",
        instance_id="inst-1",
        session_id="sess-1",
    )


def _make_target() -> MessageTarget:
    return MessageTarget(repo="jarvis", component="engine")


def _make_msg(
    stream: Stream = Stream.command,
    kind: Kind = Kind.command,
    *,
    message_id: str = "",
    idempotency_key: str = "",
    routing_ttl_ms: int = 30_000,
    observed_at_unix_ms: int = 0,
    payload: dict | None = None,
) -> UmfMessage:
    return UmfMessage(
        stream=stream,
        kind=kind,
        source=_make_source(),
        target=_make_target(),
        payload=payload or {"action": "test"},
        message_id=message_id,
        idempotency_key=idempotency_key,
        routing_ttl_ms=routing_ttl_ms,
        observed_at_unix_ms=observed_at_unix_ms,
    )


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine(tmp_path):
    """Create and start a DeliveryEngine backed by a temp SQLite file."""
    db = tmp_path / "dedup.db"
    eng = DeliveryEngine(dedup_db_path=db)
    await eng.start()
    yield eng
    await eng.stop()


# ── Tests ─────────────────────────────────────────────────────────────


class TestDeliveryEngine:
    """Five tests covering delivery engine pub/sub, dedup, TTL, routing, and health."""

    @pytest.mark.asyncio
    async def test_publish_delivers_to_subscriber(self, engine: DeliveryEngine):
        """Publishing a message delivers it to a matching stream subscriber."""
        received: list[UmfMessage] = []

        async def handler(msg: UmfMessage) -> None:
            received.append(msg)

        await engine.subscribe(Stream.command, handler)

        msg = _make_msg(stream=Stream.command)
        result = await engine.publish(msg)

        assert result.delivered is True
        assert result.reject_reason is None
        assert result.message_id == msg.message_id
        assert len(received) == 1
        assert received[0].message_id == msg.message_id

    @pytest.mark.asyncio
    async def test_duplicate_publish_deduped(self, engine: DeliveryEngine):
        """Publishing the same message twice delivers it only once (dedup)."""
        call_count = 0

        async def handler(msg: UmfMessage) -> None:
            nonlocal call_count
            call_count += 1

        await engine.subscribe(Stream.command, handler)

        msg = _make_msg(stream=Stream.command)
        r1 = await engine.publish(msg)
        r2 = await engine.publish(msg)

        assert r1.delivered is True
        assert r2.delivered is False
        assert r2.reject_reason == "dedup_duplicate"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_expired_message_rejected(self, engine: DeliveryEngine):
        """A message with an expired TTL is rejected before delivery."""
        received: list[UmfMessage] = []

        async def handler(msg: UmfMessage) -> None:
            received.append(msg)

        await engine.subscribe(Stream.command, handler)

        # Create a message with TTL=1ms and observed_at far in the past
        past_ms = int(time.time() * 1000) - 60_000  # 60 seconds ago
        msg = _make_msg(
            stream=Stream.command,
            routing_ttl_ms=1,
            observed_at_unix_ms=past_ms,
        )

        result = await engine.publish(msg)

        assert result.delivered is False
        assert result.reject_reason == "ttl_expired"
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_subscribe_filters_by_stream(self, engine: DeliveryEngine):
        """Subscribers only receive messages matching their stream."""
        command_msgs: list[UmfMessage] = []
        event_msgs: list[UmfMessage] = []

        async def command_handler(msg: UmfMessage) -> None:
            command_msgs.append(msg)

        async def event_handler(msg: UmfMessage) -> None:
            event_msgs.append(msg)

        await engine.subscribe(Stream.command, command_handler)
        await engine.subscribe(Stream.event, event_handler)

        cmd_msg = _make_msg(stream=Stream.command, kind=Kind.command)
        evt_msg = _make_msg(stream=Stream.event, kind=Kind.event)

        await engine.publish(cmd_msg)
        await engine.publish(evt_msg)

        assert len(command_msgs) == 1
        assert command_msgs[0].message_id == cmd_msg.message_id

        assert len(event_msgs) == 1
        assert event_msgs[0].message_id == evt_msg.message_id

    @pytest.mark.asyncio
    async def test_health_returns_status(self, engine: DeliveryEngine):
        """Health check returns running status and stats."""
        health = await engine.health()

        assert health["running"] is True
        assert "stats" in health
        assert health["stats"]["published"] == 0
        assert health["stats"]["rejected"] == 0
        assert health["stats"]["dispatched"] == 0
