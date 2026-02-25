# tests/unit/core/test_uds_event_fabric.py
"""Tests for UDS Event Fabric — wire protocol, server, subscribers."""

import asyncio
import json
import os
import struct
import pytest
from pathlib import Path
from unittest.mock import AsyncMock


class TestEventFabricImport:
    def test_module_imports(self):
        from backend.core.uds_event_fabric import EventFabric
        assert EventFabric is not None

    def test_required_exports(self):
        import backend.core.uds_event_fabric as mod
        assert hasattr(mod, "EventFabric")
        assert hasattr(mod, "send_frame")
        assert hasattr(mod, "recv_frame")


class TestWireProtocol:
    async def test_send_recv_roundtrip(self):
        from backend.core.uds_event_fabric import send_frame, recv_frame
        # Create in-memory stream pair
        reader = asyncio.StreamReader()

        payload = {"type": "event", "seq": 42, "data": "hello"}
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = struct.pack(">I", len(data))

        # Simulate received data
        reader.feed_data(header + data)
        reader.feed_eof()

        result = await recv_frame(reader)
        assert result == payload

    async def test_frame_size_limit(self):
        from backend.core.uds_event_fabric import recv_frame, MAX_FRAME_SIZE
        reader = asyncio.StreamReader()
        # Feed a header claiming a huge payload
        header = struct.pack(">I", MAX_FRAME_SIZE + 1)
        reader.feed_data(header)
        reader.feed_eof()

        with pytest.raises(Exception):  # ProtocolError or ValueError
            await recv_frame(reader)


class TestEventFabricLifecycle:
    async def test_start_creates_socket(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.uds_event_fabric import EventFabric
        sock_path = tmp_path / "control.sock"
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        await journal.acquire_lease(f"test:{os.getpid()}:abc")

        fabric = EventFabric(journal)
        await fabric.start(sock_path)
        assert sock_path.exists()
        await fabric.stop()
        await journal.close()

    async def test_stop_removes_socket(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.uds_event_fabric import EventFabric
        sock_path = tmp_path / "control.sock"
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        await journal.acquire_lease(f"test:{os.getpid()}:abc")

        fabric = EventFabric(journal)
        await fabric.start(sock_path)
        await fabric.stop()
        assert not sock_path.exists()
        await journal.close()

    async def test_stale_socket_cleaned_on_start(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.uds_event_fabric import EventFabric
        sock_path = tmp_path / "control.sock"
        sock_path.touch()  # Stale socket

        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        await journal.acquire_lease(f"test:{os.getpid()}:abc")

        fabric = EventFabric(journal)
        await fabric.start(sock_path)  # Should not raise
        assert sock_path.exists()
        await fabric.stop()
        await journal.close()


class TestEventEmission:
    async def test_emit_to_subscriber(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.uds_event_fabric import EventFabric, send_frame, recv_frame
        sock_path = tmp_path / "control.sock"
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        await journal.acquire_lease(f"test:{os.getpid()}:abc")

        fabric = EventFabric(journal)
        await fabric.start(sock_path)

        # Connect as subscriber
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        await send_frame(writer, {
            "type": "subscribe",
            "subscriber_id": "test_sub_1",
            "last_seen_seq": 0,
        })
        ack = await asyncio.wait_for(recv_frame(reader), timeout=5.0)
        assert ack["type"] == "subscribe_ack"

        # Emit an event
        await fabric.emit(99, "state_transition", "jarvis_prime", {"to": "READY"})

        # Subscriber should receive it
        event = await asyncio.wait_for(recv_frame(reader), timeout=5.0)
        assert event["type"] == "event"
        assert event["seq"] == 99
        assert event["target"] == "jarvis_prime"

        writer.close()
        await fabric.stop()
        await journal.close()
