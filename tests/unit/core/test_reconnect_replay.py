# tests/unit/core/test_reconnect_replay.py
"""HARD GATE tests for reconnect+replay functionality.

All 3 tests must pass for the gate to be satisfied.
"""

import asyncio
import os
import time

import pytest

from backend.core.orchestration_journal import OrchestrationJournal
from backend.core.uds_event_fabric import (
    EventFabric,
    send_frame,
    recv_frame,
)
from backend.core.control_plane_client import ControlPlaneSubscriber


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


async def _send_pong_frame(writer, ping_msg):
    """Send a pong frame matching a received ping."""
    pong = {
        "type": "pong",
        "ping_id": ping_msg.get("ping_id", ""),
        "ts": ping_msg.get("ts"),
    }
    await send_frame(writer, pong)


async def _read_events_draining(reader, timeout=1.0):
    """Read all available event frames from reader until timeout."""
    events = []
    try:
        while True:
            frame = await asyncio.wait_for(recv_frame(reader), timeout=timeout)
            if frame.get("type") == "event":
                events.append(frame)
            # Skip pings and other non-event frames
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    return events


async def _read_events_responding_pongs(reader, writer, count, timeout=5.0):
    """Read `count` event frames, responding to pings along the way."""
    events = []
    deadline = time.monotonic() + timeout
    while len(events) < count and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            frame = await asyncio.wait_for(
                recv_frame(reader), timeout=min(remaining, 2.0)
            )
            if frame.get("type") == "event":
                events.append(frame)
            elif frame.get("type") == "ping":
                await _send_pong_frame(writer, frame)
        except asyncio.TimeoutError:
            continue
        except asyncio.IncompleteReadError:
            break
    return events


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease(f"test-reconnect:{os.getpid()}")
    yield j
    await j.close()


# ── HARD GATE Tests ──────────────────────────────────────────────────────

class TestReconnectReplay:
    @pytest.mark.asyncio
    async def test_subscriber_reconnect_and_replay(self, tmp_path, journal):
        """Full lifecycle: connect, receive, disconnect, miss events, reconnect, replay."""
        sock_path = tmp_path / "ctrl.sock"
        # Use longer keepalive timeout so it doesn't interfere
        fabric = EventFabric(
            journal,
            keepalive_interval_s=5.0,
            keepalive_timeout_s=30.0,
        )
        await fabric.start(sock_path)

        received_events = []

        try:
            # -- Phase 1: Connect subscriber via ControlPlaneSubscriber --
            sub = ControlPlaneSubscriber(
                subscriber_id="reconnect_sub",
                sock_path=str(sock_path),
                last_seen_seq=0,
            )
            sub.on_event(lambda ev: received_events.append(ev))
            await sub.connect()

            # Give the server a moment to register the subscriber
            await asyncio.sleep(0.1)

            # Emit events 1, 2, 3 (using journal fenced_write for real seqs)
            seq1 = journal.fenced_write("start", "comp_a", payload={"n": 1})
            await fabric.emit(seq1, "start", "comp_a", {"n": 1})
            seq2 = journal.fenced_write("start", "comp_b", payload={"n": 2})
            await fabric.emit(seq2, "start", "comp_b", {"n": 2})
            seq3 = journal.fenced_write("start", "comp_c", payload={"n": 3})
            await fabric.emit(seq3, "start", "comp_c", {"n": 3})

            # Wait for events to arrive
            deadline = time.monotonic() + 3.0
            while len(received_events) < 3 and time.monotonic() < deadline:
                await asyncio.sleep(0.05)

            assert len(received_events) >= 3, (
                f"Expected 3 events, got {len(received_events)}: {received_events}"
            )
            received_seqs = [e["seq"] for e in received_events]
            assert seq1 in received_seqs
            assert seq2 in received_seqs
            assert seq3 in received_seqs

            # Save last_seen_seq before disconnect
            saved_seq = sub.last_seen_seq

            # -- Phase 2: Disconnect --
            await sub.disconnect()
            await asyncio.sleep(0.1)

            # -- Phase 3: Emit events 4, 5 while disconnected --
            seq4 = journal.fenced_write("start", "comp_d", payload={"n": 4})
            await fabric.emit(seq4, "start", "comp_d", {"n": 4})
            seq5 = journal.fenced_write("start", "comp_e", payload={"n": 5})
            await fabric.emit(seq5, "start", "comp_e", {"n": 5})

            # -- Phase 4: Reconnect with saved last_seen_seq --
            received_events.clear()
            sub2 = ControlPlaneSubscriber(
                subscriber_id="reconnect_sub",
                sock_path=str(sock_path),
                last_seen_seq=saved_seq,
            )
            sub2.on_event(lambda ev: received_events.append(ev))
            await sub2.connect()

            # Wait for replayed events 4, 5
            deadline = time.monotonic() + 3.0
            while len(received_events) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.05)

            replayed_seqs = [e["seq"] for e in received_events]
            assert seq4 in replayed_seqs, f"seq4={seq4} not in {replayed_seqs}"
            assert seq5 in replayed_seqs, f"seq5={seq5} not in {replayed_seqs}"

            # -- Phase 5: Verify live stream still works --
            received_events.clear()
            seq6 = journal.fenced_write("start", "comp_f", payload={"n": 6})
            await fabric.emit(seq6, "start", "comp_f", {"n": 6})

            deadline = time.monotonic() + 3.0
            while len(received_events) < 1 and time.monotonic() < deadline:
                await asyncio.sleep(0.05)

            live_seqs = [e["seq"] for e in received_events]
            assert seq6 in live_seqs, f"seq6={seq6} not in {live_seqs}"

            await sub2.disconnect()

        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_subscriber_detects_keepalive_timeout(self, tmp_path, journal):
        """Start fabric with short keepalive, connect raw subscriber (no auto-pong),
        wait for timeout, verify subscriber removed."""
        sock_path = tmp_path / "ctrl.sock"
        fabric = EventFabric(
            journal,
            keepalive_interval_s=0.3,
            keepalive_timeout_s=1.0,
        )
        await fabric.start(sock_path)

        try:
            # Connect raw subscriber - intentionally do NOT respond to pings
            reader, writer, _ack = await _raw_subscribe(sock_path, "timeout_sub")

            # Verify subscriber is initially registered
            assert "timeout_sub" in fabric._subscribers

            # Wait for keepalive timeout to remove the subscriber
            # The timeout is 1.0s, interval is 0.3s. After ~1.3s the first
            # check after timeout should fire.
            await asyncio.sleep(2.0)

            # Subscriber should have been removed
            assert "timeout_sub" not in fabric._subscribers

            try:
                writer.close()
            except Exception:
                pass
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_subscriber_reconnect_after_keepalive_death(self, tmp_path, journal):
        """Connect, force-kill writer, wait for keepalive death, reconnect with replay."""
        sock_path = tmp_path / "ctrl.sock"
        fabric = EventFabric(
            journal,
            keepalive_interval_s=0.3,
            keepalive_timeout_s=1.5,
        )
        await fabric.start(sock_path)

        try:
            # -- Phase 1: Connect and receive initial events --
            reader, writer, _ack = await _raw_subscribe(sock_path, "death_sub")

            # Emit event 1
            seq1 = journal.fenced_write("start", "comp_x", payload={"n": 1})
            await fabric.emit(seq1, "start", "comp_x", {"n": 1})

            # Read the event (and respond to any pings)
            events = await _read_events_responding_pongs(reader, writer, 1, timeout=3.0)
            assert len(events) >= 1
            assert events[0]["seq"] == seq1

            # -- Phase 2: Force-kill the writer to simulate network death --
            try:
                writer.close()
            except Exception:
                pass

            # -- Phase 3: Wait for keepalive timeout to remove subscriber --
            await asyncio.sleep(2.5)
            assert "death_sub" not in fabric._subscribers

            # -- Phase 4: Emit events during dead window --
            seq2 = journal.fenced_write("start", "comp_y", payload={"n": 2})
            await fabric.emit(seq2, "start", "comp_y", {"n": 2})
            seq3 = journal.fenced_write("start", "comp_z", payload={"n": 3})
            await fabric.emit(seq3, "start", "comp_z", {"n": 3})

            # -- Phase 5: Reconnect with saved last_seen_seq --
            reader2, writer2, ack2 = await _raw_subscribe(
                sock_path, "death_sub", last_seen_seq=seq1
            )

            # Read replayed events (seq2, seq3 should be replayed)
            replayed = await _read_events_responding_pongs(
                reader2, writer2, 2, timeout=3.0
            )
            replayed_seqs = [e["seq"] for e in replayed]
            assert seq2 in replayed_seqs, f"seq2={seq2} not in {replayed_seqs}"
            assert seq3 in replayed_seqs, f"seq3={seq3} not in {replayed_seqs}"

            # -- Phase 6: Verify live stream works --
            seq4 = journal.fenced_write("start", "comp_w", payload={"n": 4})
            await fabric.emit(seq4, "start", "comp_w", {"n": 4})

            live = await _read_events_responding_pongs(
                reader2, writer2, 1, timeout=3.0
            )
            assert len(live) >= 1
            live_seqs = [e["seq"] for e in live]
            assert seq4 in live_seqs, f"seq4={seq4} not in {live_seqs}"

            try:
                writer2.close()
            except Exception:
                pass

        finally:
            await fabric.stop()
