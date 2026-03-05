"""Tests for UmfClient -- thin SDK wrapper over DeliveryEngine.

Covers command publishing, heartbeat sending, ack/nack helpers,
and client health delegation.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from backend.core.umf.client import UmfClient
from backend.core.umf.types import Kind, Stream, UmfMessage


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client(tmp_path):
    """Create and start a UmfClient backed by a temp SQLite dedup file."""
    c = UmfClient(
        repo="jarvis",
        component="test-runner",
        instance_id="inst-1",
        session_id="sess-1",
        dedup_db_path=tmp_path / "dedup.db",
    )
    await c.start()
    yield c
    await c.stop()


# ── Tests ─────────────────────────────────────────────────────────────


class TestUmfClient:
    """Four async tests covering command, heartbeat, ack, and health."""

    @pytest.mark.asyncio
    async def test_publish_command(self, client: UmfClient):
        """publish_command creates a message with stream=command, kind=command."""
        received: list[UmfMessage] = []

        async def handler(msg: UmfMessage) -> None:
            received.append(msg)

        await client.subscribe(Stream.command, handler)

        result = await client.publish_command(
            target_repo="jarvis",
            target_component="reactor-core",
            payload={"action": "restart"},
        )

        assert result.delivered is True
        assert len(received) == 1
        assert received[0].stream is Stream.command
        assert received[0].kind is Kind.command
        assert received[0].payload == {"action": "restart"}
        assert received[0].target.repo == "jarvis"
        assert received[0].target.component == "reactor-core"

    @pytest.mark.asyncio
    async def test_send_heartbeat(self, client: UmfClient):
        """send_heartbeat creates a lifecycle message with heartbeat kind and correct payload."""
        received: list[UmfMessage] = []

        async def handler(msg: UmfMessage) -> None:
            received.append(msg)

        await client.subscribe(Stream.lifecycle, handler)

        result = await client.send_heartbeat(state="ready")

        assert result.delivered is True
        assert len(received) == 1

        msg = received[0]
        assert msg.stream is Stream.lifecycle
        assert msg.kind is Kind.heartbeat
        assert msg.payload["state"] == "ready"
        assert msg.payload["liveness"] is True
        assert msg.payload["readiness"] is True
        assert msg.payload["subsystem_role"] == "test-runner"
        assert msg.payload["last_error_code"] == ""
        assert msg.payload["queue_depth"] == 0
        assert msg.payload["resource_pressure"] == 0.0

    @pytest.mark.asyncio
    async def test_send_ack(self, client: UmfClient):
        """send_ack sets causality_parent_message_id to the original message ID."""
        received: list[UmfMessage] = []

        async def handler(msg: UmfMessage) -> None:
            received.append(msg)

        await client.subscribe(Stream.command, handler)

        original_id = "abc123deadbeef"
        result = await client.send_ack(
            original_message_id=original_id,
            target_repo="jarvis",
            target_component="supervisor",
            success=True,
            message="done",
        )

        assert result.delivered is True
        assert len(received) == 1

        msg = received[0]
        assert msg.kind is Kind.ack
        assert msg.causality_parent_message_id == original_id
        assert msg.payload["message"] == "done"

    @pytest.mark.asyncio
    async def test_client_health(self, client: UmfClient):
        """health() delegates to engine and reports running=True."""
        health = await client.health()

        assert health["running"] is True
        assert "stats" in health
