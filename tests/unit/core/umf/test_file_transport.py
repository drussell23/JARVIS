"""Tests for the UMF file-based transport adapter.

Covers atomic writes, message round-trips, ordering by filename,
cleanup of stale files, and absence of partial (.tmp) files.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.umf.transport_adapters.file_transport import FileTransport
from backend.core.umf.types import (
    Kind,
    MessageSource,
    MessageTarget,
    Stream,
    UmfMessage,
)


def _make_source() -> MessageSource:
    return MessageSource(
        repo="jarvis-ai-agent",
        component="supervisor",
        instance_id="inst-001",
        session_id="sess-abc",
    )


def _make_target() -> MessageTarget:
    return MessageTarget(repo="reactor-core", component="event_bus")


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


class TestFileTransport:
    """Five async tests for the file transport adapter."""

    @pytest.mark.asyncio
    async def test_send_creates_file(self, tmp_path):
        transport = FileTransport(base_dir=tmp_path)
        await transport.start()

        msg = _make_msg()
        result = await transport.send(msg)

        assert result is True

        stream_dir = tmp_path / "command"
        assert stream_dir.is_dir()

        files = list(stream_dir.glob("*.json"))
        assert len(files) == 1
        assert msg.message_id in files[0].name

        await transport.stop()

    @pytest.mark.asyncio
    async def test_receive_reads_files(self, tmp_path):
        transport = FileTransport(base_dir=tmp_path)
        await transport.start()

        original = _make_msg()
        await transport.send(original)

        received = []
        async for msg in transport.receive("command"):
            received.append(msg)

        assert len(received) == 1
        assert received[0].message_id == original.message_id
        assert received[0].stream == original.stream
        assert received[0].kind == original.kind
        assert received[0].payload == original.payload

        await transport.stop()

    @pytest.mark.asyncio
    async def test_files_sorted_by_name_for_ordering(self, tmp_path):
        transport = FileTransport(base_dir=tmp_path)
        await transport.start()

        msg1 = _make_msg(payload={"seq": 1})
        await transport.send(msg1)

        # Small sleep to ensure different observed_at timestamps in filenames
        await asyncio.sleep(0.01)

        msg2 = _make_msg(payload={"seq": 2})
        await transport.send(msg2)

        received = []
        async for msg in transport.receive("command"):
            received.append(msg)

        assert len(received) == 2
        assert received[0].message_id == msg1.message_id
        assert received[1].message_id == msg2.message_id

        await transport.stop()

    @pytest.mark.asyncio
    async def test_cleanup_removes_old_files(self, tmp_path):
        transport = FileTransport(base_dir=tmp_path, cleanup_age_s=0.01)
        await transport.start()

        await transport.send(_make_msg())

        stream_dir = tmp_path / "command"
        assert len(list(stream_dir.glob("*.json"))) == 1

        # Wait for files to age past the cleanup threshold
        await asyncio.sleep(0.05)

        removed = await transport.cleanup()
        assert removed >= 1
        assert len(list(stream_dir.glob("*.json"))) == 0

        await transport.stop()

    @pytest.mark.asyncio
    async def test_atomic_write_no_partial_files(self, tmp_path):
        transport = FileTransport(base_dir=tmp_path)
        await transport.start()

        await transport.send(_make_msg())

        stream_dir = tmp_path / "command"
        tmp_files = list(stream_dir.glob("*.tmp"))
        assert len(tmp_files) == 0

        json_files = list(stream_dir.glob("*.json"))
        assert len(json_files) == 1

        await transport.stop()
