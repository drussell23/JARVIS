"""Tests for StateEventEmitter -- publish + cursor advance."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.reactive_state.event_emitter import StateEventEmitter
from backend.core.reactive_state.journal import AppendOnlyJournal
from backend.core.reactive_state.types import JournalEntry
from backend.core.umf.types import UmfMessage


def _journal_entry(
    *,
    global_revision: int = 1,
    key: str = "gcp.offload_active",
    value: object = True,
    previous_value: object = False,
    version: int = 1,
    epoch: int = 1,
    writer: str = "gcp_controller",
    writer_session_id: str = "sess-1",
    origin: str = "explicit",
    consistency_group: str | None = None,
    timestamp_unix_ms: int = 1700000000000,
    checksum: str = "abc",
) -> JournalEntry:
    return JournalEntry(
        global_revision=global_revision,
        key=key,
        value=value,
        previous_value=previous_value,
        version=version,
        epoch=epoch,
        writer=writer,
        writer_session_id=writer_session_id,
        origin=origin,
        consistency_group=consistency_group,
        timestamp_unix_ms=timestamp_unix_ms,
        checksum=checksum,
    )


@pytest.fixture
def journal(tmp_path: Path) -> AppendOnlyJournal:
    j = AppendOnlyJournal(tmp_path / "emitter.db")
    j.open()
    yield j
    j.close()


class TestStateEventEmitter:
    def test_publish_calls_callback_and_advances_cursor(self, journal) -> None:
        published = []

        async def fake_publish(msg: UmfMessage) -> bool:
            published.append(msg)
            return True

        emitter = StateEventEmitter(
            journal=journal, publish_fn=fake_publish,
            instance_id="i1", session_id="s1",
        )
        je = _journal_entry(global_revision=1)
        asyncio.get_event_loop().run_until_complete(emitter.publish(je))

        assert len(published) == 1
        assert published[0].payload["key"] == "gcp.offload_active"
        assert journal.get_publish_cursor() == 1

    def test_publish_failure_does_not_advance_cursor(self, journal) -> None:
        async def failing_publish(msg: UmfMessage) -> bool:
            return False

        emitter = StateEventEmitter(
            journal=journal, publish_fn=failing_publish,
            instance_id="i1", session_id="s1",
        )
        je = _journal_entry(global_revision=1)
        asyncio.get_event_loop().run_until_complete(emitter.publish(je))
        assert journal.get_publish_cursor() == 0

    def test_publish_exception_does_not_advance_cursor(self, journal) -> None:
        async def exploding_publish(msg: UmfMessage) -> bool:
            raise RuntimeError("network down")

        emitter = StateEventEmitter(
            journal=journal, publish_fn=exploding_publish,
            instance_id="i1", session_id="s1",
        )
        je = _journal_entry(global_revision=1)
        asyncio.get_event_loop().run_until_complete(emitter.publish(je))
        assert journal.get_publish_cursor() == 0

    def test_multiple_publishes_advance_cursor_monotonically(self, journal) -> None:
        published = []

        async def fake_publish(msg: UmfMessage) -> bool:
            published.append(msg)
            return True

        emitter = StateEventEmitter(
            journal=journal, publish_fn=fake_publish,
            instance_id="i1", session_id="s1",
        )
        for rev in [1, 2, 3]:
            je = _journal_entry(global_revision=rev, key=f"k{rev}", version=rev)
            asyncio.get_event_loop().run_until_complete(emitter.publish(je))

        assert len(published) == 3
        assert journal.get_publish_cursor() == 3

    def test_publish_stats(self, journal) -> None:
        async def fake_publish(msg: UmfMessage) -> bool:
            return True

        emitter = StateEventEmitter(
            journal=journal, publish_fn=fake_publish,
            instance_id="i1", session_id="s1",
        )
        je = _journal_entry(global_revision=1)
        asyncio.get_event_loop().run_until_complete(emitter.publish(je))

        stats = emitter.stats()
        assert stats["published"] == 1
        assert stats["failed"] == 0
