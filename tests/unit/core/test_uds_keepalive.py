# tests/unit/core/test_uds_keepalive.py
"""Tests for UDS keepalive — server-side ping/pong mechanism."""

import asyncio
import os
import time

import pytest  # noqa: F401 — used by pytest collection

from backend.core.orchestration_journal import OrchestrationJournal
from backend.core.uds_event_fabric import (
    EventFabric,
    KEEPALIVE_INTERVAL_S,
    KEEPALIVE_TIMEOUT_S,
    send_frame,
    recv_frame,
)


# ── Helpers ──────────────────────────────────────────────────────────────

async def _raw_subscribe(sock_path, subscriber_id, last_seen_seq=0):
    """Low-level subscribe: connect, send handshake, read ack. Returns (reader, writer, ack)."""
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    await send_frame(writer, {
        "type": "subscribe",
        "subscriber_id": subscriber_id,
        "last_seen_seq": last_seen_seq,
    })
    ack = await asyncio.wait_for(recv_frame(reader), timeout=5.0)
    assert ack["type"] == "subscribe_ack"
    return reader, writer, ack


async def _send_pong(writer, ping_msg):
    """Send a pong frame matching a received ping."""
    pong = {
        "type": "pong",
        "ping_id": ping_msg.get("ping_id", ""),
        "ts": ping_msg.get("ts"),
    }
    await send_frame(writer, pong)


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease(f"test-keepalive:{os.getpid()}")
    yield j
    await j.close()


# ── Tests ────────────────────────────────────────────────────────────────

class TestKeepaliveConstants:
    def test_keepalive_constants_exist(self):
        """Verify KEEPALIVE_INTERVAL_S and KEEPALIVE_TIMEOUT_S are exported."""
        import backend.core.uds_event_fabric as mod
        assert hasattr(mod, "KEEPALIVE_INTERVAL_S")
        assert hasattr(mod, "KEEPALIVE_TIMEOUT_S")
        assert isinstance(KEEPALIVE_INTERVAL_S, float)
        assert isinstance(KEEPALIVE_TIMEOUT_S, float)
        assert KEEPALIVE_INTERVAL_S > 0
        assert KEEPALIVE_TIMEOUT_S > KEEPALIVE_INTERVAL_S


class TestKeepalivePingPong:
    @pytest.mark.asyncio
    async def test_subscriber_receives_ping(self, tmp_path, journal):
        """Connect subscriber, wait, verify ping frame received with ping_id and ts."""
        sock_path = tmp_path / "ctrl.sock"
        fabric = EventFabric(
            journal,
            keepalive_interval_s=0.3,
            keepalive_timeout_s=5.0,
        )
        await fabric.start(sock_path)

        try:
            reader, writer, _ack = await _raw_subscribe(sock_path, "ping_test_sub")

            # Read frames until we get a ping (skip replayed events)
            ping = None
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                frame = await asyncio.wait_for(recv_frame(reader), timeout=3.0)
                if frame.get("type") == "ping":
                    ping = frame
                    break

            assert ping is not None, "Never received a ping frame"
            assert "ping_id" in ping
            assert len(ping["ping_id"]) == 12
            assert "ts" in ping
            assert isinstance(ping["ts"], float)

            writer.close()
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_subscriber_pong_accepted(self, tmp_path, journal):
        """Send pong after receiving ping, verify subscriber stays connected."""
        sock_path = tmp_path / "ctrl.sock"
        fabric = EventFabric(
            journal,
            keepalive_interval_s=0.3,
            keepalive_timeout_s=2.0,
        )
        await fabric.start(sock_path)

        try:
            reader, writer, _ack = await _raw_subscribe(sock_path, "pong_test_sub")

            # Read frames until we get the first ping (skip replayed events)
            ping = None
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                frame = await asyncio.wait_for(recv_frame(reader), timeout=3.0)
                if frame.get("type") == "ping":
                    ping = frame
                    break
            assert ping is not None, "Never received a ping frame"
            await _send_pong(writer, ping)

            # Wait a bit, subscriber should still be registered
            await asyncio.sleep(0.5)
            assert "pong_test_sub" in fabric._subscribers

            # Should receive another ping (still alive) - skip any events
            ping2 = None
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                frame = await asyncio.wait_for(recv_frame(reader), timeout=3.0)
                if frame.get("type") == "ping":
                    ping2 = frame
                    break
            assert ping2 is not None, "Never received second ping frame"

            writer.close()
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_dead_subscriber_removed_on_timeout(self, tmp_path, journal):
        """Connect, don't send pongs, verify removed after timeout."""
        sock_path = tmp_path / "ctrl.sock"
        fabric = EventFabric(
            journal,
            keepalive_interval_s=0.3,
            keepalive_timeout_s=1.0,
        )
        await fabric.start(sock_path)

        try:
            reader, writer, _ack = await _raw_subscribe(sock_path, "dead_sub")

            # Subscriber is initially registered
            assert "dead_sub" in fabric._subscribers

            # Read pings but do NOT send pongs - just discard them
            try:
                while True:
                    frame = await asyncio.wait_for(recv_frame(reader), timeout=0.5)
                    # Intentionally do NOT respond with pong
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                pass

            # Wait for timeout to trigger removal
            await asyncio.sleep(1.5)

            # Subscriber should have been removed due to keepalive timeout
            assert "dead_sub" not in fabric._subscribers

            writer.close()
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_active_subscriber_survives_with_pong(self, tmp_path, journal):
        """Send pongs continuously, verify subscriber stays connected beyond timeout window."""
        sock_path = tmp_path / "ctrl.sock"
        fabric = EventFabric(
            journal,
            keepalive_interval_s=0.3,
            keepalive_timeout_s=1.5,
        )
        await fabric.start(sock_path)

        try:
            reader, writer, _ack = await _raw_subscribe(sock_path, "alive_sub")

            # Respond to pings for a duration longer than the timeout
            start = time.monotonic()
            pong_count = 0
            while time.monotonic() - start < 2.5:
                try:
                    frame = await asyncio.wait_for(recv_frame(reader), timeout=1.0)
                    if frame.get("type") == "ping":
                        await _send_pong(writer, frame)
                        pong_count += 1
                except asyncio.TimeoutError:
                    break

            # Should have sent multiple pongs
            assert pong_count >= 3

            # Subscriber should still be alive
            assert "alive_sub" in fabric._subscribers

            writer.close()
        finally:
            await fabric.stop()
