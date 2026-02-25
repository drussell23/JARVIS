# tests/unit/core/test_journal_outbox_schema.py
"""Tests for journal outbox and budget reservation schema."""
import sqlite3
import pytest
from backend.core.orchestration_journal import OrchestrationJournal


@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease("test_leader")
    yield j
    await j.close()


class TestOutboxSchema:
    @pytest.mark.asyncio
    async def test_event_outbox_table_exists(self, journal, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='event_outbox'"
        ).fetchone()
        conn.close()
        assert row is not None, "event_outbox table not created"

    @pytest.mark.asyncio
    async def test_event_outbox_columns(self, journal, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        rows = conn.execute("PRAGMA table_info(event_outbox)").fetchall()
        col_names = [r[1] for r in rows]
        conn.close()
        for col in ["seq", "event_type", "target", "payload", "published", "published_at"]:
            assert col in col_names, f"Missing column: {col}"

    @pytest.mark.asyncio
    async def test_outbox_fk_references_journal(self, journal, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        fks = conn.execute("PRAGMA foreign_key_list(event_outbox)").fetchall()
        conn.close()
        journal_refs = [fk for fk in fks if fk[2] == "journal"]
        assert len(journal_refs) > 0, "event_outbox has no FK to journal"


class TestComponentStateExtensions:
    @pytest.mark.asyncio
    async def test_component_state_has_start_timestamp(self, journal, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        rows = conn.execute("PRAGMA table_info(component_state)").fetchall()
        col_names = [r[1] for r in rows]
        conn.close()
        assert "start_timestamp" in col_names, "Missing start_timestamp column"

    @pytest.mark.asyncio
    async def test_component_state_has_consecutive_failures(self, journal, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        rows = conn.execute("PRAGMA table_info(component_state)").fetchall()
        col_names = [r[1] for r in rows]
        conn.close()
        assert "consecutive_failures" in col_names, "Missing consecutive_failures column"

    @pytest.mark.asyncio
    async def test_component_state_has_last_probe_category(self, journal, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        rows = conn.execute("PRAGMA table_info(component_state)").fetchall()
        col_names = [r[1] for r in rows]
        conn.close()
        assert "last_probe_category" in col_names, "Missing last_probe_category column"
