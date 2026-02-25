# tests/unit/core/test_outbox_ordering.py
"""Tests for outbox-based event ordering (commit-before-publish)."""
import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path
import pytest
from backend.core.orchestration_journal import OrchestrationJournal
from backend.core.uds_event_fabric import EventFabric


@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease(f"test:{os.getpid()}")
    yield j
    await j.close()


class TestOutboxWrite:
    @pytest.mark.asyncio
    async def test_write_to_outbox(self, journal, tmp_path):
        seq = journal.fenced_write("gcp_lifecycle", "invincible_node",
                                    payload={"event": "pressure_triggered"})
        journal.write_outbox(seq, "gcp_lifecycle", "invincible_node",
                             payload={"event": "pressure_triggered"})
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute("SELECT * FROM event_outbox WHERE seq = ?", (seq,)).fetchone()
        conn.close()
        assert row is not None
        assert row[4] == 0  # published = false

    @pytest.mark.asyncio
    async def test_outbox_fk_enforced(self, journal, tmp_path):
        """Cannot write outbox entry for nonexistent journal seq."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO event_outbox (seq, event_type, target, published) VALUES (99999, 'x', 'y', 0)"
            )
        conn.close()


class TestOutboxPublisher:
    @pytest.mark.asyncio
    async def test_unpublished_entries_emitted(self, journal, tmp_path):
        _td = tempfile.mkdtemp(prefix="jt_")
        sock_path = Path(os.path.join(_td, "c.sock"))
        fabric = EventFabric(journal, keepalive_interval_s=5.0, keepalive_timeout_s=30.0)
        await fabric.start(sock_path)

        try:
            # Write journal + outbox
            seq = journal.fenced_write("gcp_lifecycle", "invincible_node",
                                        payload={"event": "vm_ready"})
            journal.write_outbox(seq, "gcp_lifecycle", "invincible_node",
                                 payload={"event": "vm_ready"})

            # Run one cycle of outbox publisher
            published = await fabric.publish_outbox_once()
            assert published >= 1

            # Verify marked as published
            conn = sqlite3.connect(str(tmp_path / "test.db"))
            row = conn.execute("SELECT published FROM event_outbox WHERE seq = ?", (seq,)).fetchone()
            conn.close()
            assert row[0] == 1
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_already_published_not_re_emitted(self, journal, tmp_path):
        _td = tempfile.mkdtemp(prefix="jt_")
        sock_path = Path(os.path.join(_td, "c.sock"))
        fabric = EventFabric(journal, keepalive_interval_s=5.0, keepalive_timeout_s=30.0)
        await fabric.start(sock_path)

        try:
            seq = journal.fenced_write("gcp_lifecycle", "invincible_node",
                                        payload={"event": "vm_ready"})
            journal.write_outbox(seq, "gcp_lifecycle", "invincible_node",
                                 payload={"event": "vm_ready"})

            await fabric.publish_outbox_once()
            count = await fabric.publish_outbox_once()
            assert count == 0  # Nothing new to publish
        finally:
            await fabric.stop()
