"""Tests for SpinalCord — Phase 2 bidirectional event wiring (TDD).

All tests are pure-asyncio and inject mock event streams.
No model calls, no network, no file I/O except the local-buffer fallback
tests which use pytest's tmp_path fixture.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.spinal_cord import SpinalCord, SpinalStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event_stream(*, broadcast_return: int = 1, broadcast_side_effect=None):
    """Return a mock with an async broadcast_event method."""
    mock = MagicMock()
    if broadcast_side_effect is not None:
        mock.broadcast_event = AsyncMock(side_effect=broadcast_side_effect)
    else:
        mock.broadcast_event = AsyncMock(return_value=broadcast_return)
    return mock


def _make_cord(event_stream, *, local_buffer_path: str | None = None) -> SpinalCord:
    """Construct a SpinalCord with an optional explicit buffer path."""
    if local_buffer_path is not None:
        return SpinalCord(event_stream, local_buffer_path=local_buffer_path)
    return SpinalCord(event_stream)


# ---------------------------------------------------------------------------
# test_spinal_gate_starts_unset
# ---------------------------------------------------------------------------


class TestSpinalGateStartsUnset:
    def test_spinal_gate_starts_unset(self):
        """Gate must not be set before wire() is called."""
        cord = _make_cord(_make_event_stream())
        assert cord.gate_is_set is False


# ---------------------------------------------------------------------------
# test_spinal_liveness_starts_false
# ---------------------------------------------------------------------------


class TestSpinalLivenessStartsFalse:
    def test_spinal_liveness_starts_false(self):
        """is_live must be False before wire() succeeds."""
        cord = _make_cord(_make_event_stream())
        assert cord.is_live is False


# ---------------------------------------------------------------------------
# test_wire_sets_gate_on_success
# ---------------------------------------------------------------------------


class TestWireSetsGateOnSuccess:
    @pytest.mark.asyncio
    async def test_wire_sets_gate_on_success(self):
        """Successful wire: CONNECTED status, gate set, is_live True."""
        stream = _make_event_stream(broadcast_return=1)
        cord = _make_cord(stream)

        status = await cord.wire(timeout_s=5.0)

        assert status is SpinalStatus.CONNECTED
        assert cord.gate_is_set is True
        assert cord.is_live is True


# ---------------------------------------------------------------------------
# test_wire_degraded_on_timeout
# ---------------------------------------------------------------------------


class TestWireDegradedOnTimeout:
    @pytest.mark.asyncio
    async def test_wire_degraded_on_timeout(self):
        """TimeoutError in broadcast_event → DEGRADED, gate still set, not live."""
        stream = _make_event_stream(broadcast_side_effect=asyncio.TimeoutError())
        cord = _make_cord(stream)

        status = await cord.wire(timeout_s=5.0)

        assert status is SpinalStatus.DEGRADED
        # Gate is ALWAYS set after wire() — even degraded (Phase 3 starts in local mode)
        assert cord.gate_is_set is True
        assert cord.is_live is False


# ---------------------------------------------------------------------------
# test_stream_up_uses_broadcast
# ---------------------------------------------------------------------------


class TestStreamUpUsesBroadcast:
    @pytest.mark.asyncio
    async def test_stream_up_uses_broadcast(self):
        """When live, stream_up delegates to broadcast_event on 'governance' channel."""
        stream = _make_event_stream(broadcast_return=1)
        cord = _make_cord(stream)
        await cord.wire(timeout_s=5.0)

        # Reset call count after wire() so we can isolate stream_up
        stream.broadcast_event.reset_mock()

        payload = {"event_type": "finding", "data": "test"}
        await cord.stream_up("finding", payload)

        stream.broadcast_event.assert_awaited_once()
        call_kwargs = stream.broadcast_event.call_args
        channel_arg = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("channel")
        assert channel_arg == "governance"


# ---------------------------------------------------------------------------
# test_stream_up_falls_back_to_local_when_not_live
# ---------------------------------------------------------------------------


class TestStreamUpFallsBackToLocal:
    @pytest.mark.asyncio
    async def test_stream_up_falls_back_to_local_when_not_live(self, tmp_path: Path):
        """When not live, stream_up writes a JSON line to the local buffer file."""
        stream = _make_event_stream(broadcast_side_effect=asyncio.TimeoutError())
        buf_path = str(tmp_path / "pending_findings.jsonl")
        cord = _make_cord(stream, local_buffer_path=buf_path)
        await cord.wire(timeout_s=5.0)  # DEGRADED → not live

        payload = {"finding": "dead_code", "file": "backend/foo.py"}
        await cord.stream_up("finding", payload)

        lines = Path(buf_path).read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event_type"] == "finding"
        assert record["payload"] == payload

    @pytest.mark.asyncio
    async def test_stream_down_falls_back_to_local_when_not_live(self, tmp_path: Path):
        """When not live, stream_down also writes a JSON line to the local buffer."""
        stream = _make_event_stream(broadcast_side_effect=asyncio.TimeoutError())
        buf_path = str(tmp_path / "pending_findings.jsonl")
        cord = _make_cord(stream, local_buffer_path=buf_path)
        await cord.wire(timeout_s=5.0)  # DEGRADED → not live

        payload = {"decision": "approve", "op_id": "abc123"}
        await cord.stream_down("governance_decision", payload)

        lines = Path(buf_path).read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event_type"] == "governance_decision"
        assert record["payload"] == payload


# ---------------------------------------------------------------------------
# test_wire_is_idempotent
# ---------------------------------------------------------------------------


class TestWireIsIdempotent:
    @pytest.mark.asyncio
    async def test_wire_is_idempotent(self):
        """Calling wire() twice is safe and both calls return CONNECTED."""
        stream = _make_event_stream(broadcast_return=1)
        cord = _make_cord(stream)

        status1 = await cord.wire(timeout_s=5.0)
        status2 = await cord.wire(timeout_s=5.0)

        assert status1 is SpinalStatus.CONNECTED
        assert status2 is SpinalStatus.CONNECTED
        # Gate remains set
        assert cord.gate_is_set is True
        # broadcast_event called only once (second wire() is a no-op)
        assert stream.broadcast_event.await_count == 1


# ---------------------------------------------------------------------------
# test_on_disconnect_clears_liveness
# ---------------------------------------------------------------------------


class TestOnDisconnectClearsLiveness:
    @pytest.mark.asyncio
    async def test_on_disconnect_clears_liveness(self):
        """on_disconnect() must set is_live to False."""
        stream = _make_event_stream(broadcast_return=1)
        cord = _make_cord(stream)
        await cord.wire(timeout_s=5.0)
        assert cord.is_live is True

        cord.on_disconnect()

        assert cord.is_live is False


# ---------------------------------------------------------------------------
# test_on_reconnect_restores_liveness
# ---------------------------------------------------------------------------


class TestOnReconnectRestoresLiveness:
    @pytest.mark.asyncio
    async def test_on_reconnect_restores_liveness(self):
        """on_reconnect() must set is_live to True."""
        stream = _make_event_stream(broadcast_return=1)
        cord = _make_cord(stream)
        await cord.wire(timeout_s=5.0)
        cord.on_disconnect()
        assert cord.is_live is False

        cord.on_reconnect()

        assert cord.is_live is True

    @pytest.mark.asyncio
    async def test_on_reconnect_without_wire_sets_live(self):
        """on_reconnect() works even without a prior wire() call."""
        stream = _make_event_stream()
        cord = _make_cord(stream)
        cord.on_reconnect()
        assert cord.is_live is True


# ---------------------------------------------------------------------------
# test_wait_for_gate
# ---------------------------------------------------------------------------


class TestWaitForGate:
    @pytest.mark.asyncio
    async def test_wait_for_gate_returns_after_wire(self):
        """wait_for_gate() should return promptly after wire() sets the gate."""
        stream = _make_event_stream(broadcast_return=1)
        cord = _make_cord(stream)
        await cord.wire(timeout_s=5.0)

        # Should resolve immediately since gate is already set
        await asyncio.wait_for(cord.wait_for_gate(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_wait_for_gate_blocks_until_wire(self):
        """wait_for_gate() blocks until wire() is eventually called."""
        stream = _make_event_stream(broadcast_return=1)
        cord = _make_cord(stream)

        async def wire_soon():
            await asyncio.sleep(0.05)
            await cord.wire(timeout_s=5.0)

        asyncio.get_event_loop().create_task(wire_soon())
        await asyncio.wait_for(cord.wait_for_gate(), timeout=2.0)
        assert cord.gate_is_set is True

    @pytest.mark.asyncio
    async def test_wait_for_gate_returns_even_on_degraded(self):
        """Gate is set even when wire() results in DEGRADED status."""
        stream = _make_event_stream(broadcast_side_effect=asyncio.TimeoutError())
        cord = _make_cord(stream)
        await cord.wire(timeout_s=5.0)

        # Gate must be set even in degraded mode
        await asyncio.wait_for(cord.wait_for_gate(), timeout=1.0)
        assert cord.gate_is_set is True


# ---------------------------------------------------------------------------
# test_multiple_buffer_writes_append_correctly
# ---------------------------------------------------------------------------


class TestMultipleBufferWritesAppend:
    @pytest.mark.asyncio
    async def test_multiple_writes_produce_multiple_lines(self, tmp_path: Path):
        """Each offline stream_up call appends one JSON line to the buffer."""
        stream = _make_event_stream(broadcast_side_effect=asyncio.TimeoutError())
        buf_path = str(tmp_path / "pending_findings.jsonl")
        cord = _make_cord(stream, local_buffer_path=buf_path)
        await cord.wire(timeout_s=5.0)

        await cord.stream_up("finding", {"id": 1})
        await cord.stream_up("finding", {"id": 2})
        await cord.stream_up("finding", {"id": 3})

        lines = Path(buf_path).read_text().splitlines()
        assert len(lines) == 3
        ids = [json.loads(l)["payload"]["id"] for l in lines]
        assert ids == [1, 2, 3]
