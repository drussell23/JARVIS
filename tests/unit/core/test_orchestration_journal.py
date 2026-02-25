# tests/unit/core/test_orchestration_journal.py
"""Tests for OrchestrationJournal — SQLite schema, journal writes, reads, lease, fencing."""

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest


def _import_module():
    try:
        from backend.core.orchestration_journal import OrchestrationJournal
        return OrchestrationJournal
    except ImportError:
        return None


class TestJournalImport:
    def test_module_imports(self):
        cls = _import_module()
        assert cls is not None, "OrchestrationJournal must be importable"

    def test_required_exports(self):
        import backend.core.orchestration_journal as mod
        assert hasattr(mod, "OrchestrationJournal")
        assert hasattr(mod, "StaleEpochError")
        assert hasattr(mod, "SCHEMA_VERSION")


class TestJournalInitialization:
    async def test_creates_db_file(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "control" / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        assert db_path.exists()

    async def test_creates_parent_directories(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "deep" / "nested" / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        assert db_path.parent.exists()

    async def test_wal_mode_enabled(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        conn = sqlite3.connect(str(db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    async def test_schema_version_recorded(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal, SCHEMA_VERSION
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == SCHEMA_VERSION

    async def test_tables_exist(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        conn = sqlite3.connect(str(db_path))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "journal" in tables
        assert "component_state" in tables
        assert "lease" in tables
        assert "contracts" in tables
        assert "schema_version" in tables

    async def test_idempotent_initialization(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        await j1.close()
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)  # Should not raise
        await j2.close()


class TestJournalWrites:
    @pytest.fixture
    async def journal(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        # Acquire lease so we can write
        await j.acquire_lease(f"test:{os.getpid()}:abc123")
        yield j
        await j.close()

    async def test_write_returns_sequence(self, journal):
        seq = journal.fenced_write("start", "jarvis_prime")
        assert isinstance(seq, int)
        assert seq >= 1

    async def test_sequential_sequence_numbers(self, journal):
        seq1 = journal.fenced_write("start", "jarvis_prime")
        seq2 = journal.fenced_write("stop", "jarvis_prime")
        assert seq2 == seq1 + 1

    async def test_write_stores_payload(self, journal):
        payload = {"from": "REGISTERED", "to": "STARTING", "reason": "test"}
        seq = journal.fenced_write("state_transition", "jarvis_prime", payload=payload)
        entries = await journal.replay_from(seq - 1)
        assert len(entries) == 1
        assert entries[0]["payload"] == payload

    async def test_write_stores_epoch(self, journal):
        seq = journal.fenced_write("start", "backend_api")
        entries = await journal.replay_from(seq - 1)
        assert entries[0]["epoch"] == journal.epoch

    async def test_write_stores_wall_clock(self, journal):
        before = time.time()
        seq = journal.fenced_write("start", "backend_api")
        after = time.time()
        entries = await journal.replay_from(seq - 1)
        assert before <= entries[0]["timestamp"] <= after

    async def test_idempotency_key_dedup(self, journal):
        key = "start:jarvis_prime:test_dedup"
        seq1 = journal.fenced_write("start", "jarvis_prime", idempotency_key=key)
        seq2 = journal.fenced_write("start", "jarvis_prime", idempotency_key=key)
        assert seq1 == seq2  # Same entry returned, no duplicate

    async def test_idempotency_allows_different_keys(self, journal):
        seq1 = journal.fenced_write("start", "jarvis_prime", idempotency_key="key_a")
        seq2 = journal.fenced_write("start", "jarvis_prime", idempotency_key="key_b")
        assert seq2 != seq1

    async def test_failed_idempotency_key_allows_retry(self, journal):
        key = "start:jarvis_prime:retry_test"
        seq1 = journal.fenced_write("start", "jarvis_prime", idempotency_key=key)
        journal.mark_result(seq1, "failed")
        seq2 = journal.fenced_write("start", "jarvis_prime", idempotency_key=key)
        assert seq2 != seq1  # New entry because previous was 'failed'


class TestJournalReplay:
    @pytest.fixture
    async def journal(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        await j.acquire_lease(f"test:{os.getpid()}:abc123")
        yield j
        await j.close()

    async def test_replay_from_zero(self, journal):
        journal.fenced_write("start", "a")
        journal.fenced_write("start", "b")
        journal.fenced_write("start", "c")
        entries = await journal.replay_from(0)
        # Includes lease_acquired + 3 writes
        assert len(entries) >= 3

    async def test_replay_from_specific_seq(self, journal):
        seq1 = journal.fenced_write("start", "a")
        seq2 = journal.fenced_write("start", "b")
        seq3 = journal.fenced_write("start", "c")
        entries = await journal.replay_from(seq1)
        targets = [e["target"] for e in entries]
        assert "b" in targets
        assert "c" in targets

    async def test_replay_with_target_filter(self, journal):
        journal.fenced_write("start", "jarvis_prime")
        journal.fenced_write("start", "reactor_core")
        journal.fenced_write("stop", "jarvis_prime")
        entries = await journal.replay_from(0, target_filter=["jarvis_prime"])
        for e in entries:
            assert e["target"] == "jarvis_prime"

    async def test_replay_with_action_filter(self, journal):
        journal.fenced_write("start", "a")
        journal.fenced_write("stop", "a")
        journal.fenced_write("start", "b")
        entries = await journal.replay_from(0, action_filter=["stop"])
        for e in entries:
            assert e["action"] == "stop"

    async def test_replay_capped_at_1000(self, journal):
        for i in range(1100):
            journal.fenced_write("heartbeat", f"comp_{i % 10}")
        entries = await journal.replay_from(0)
        assert len(entries) <= 1000


class TestJournalResultTracking:
    @pytest.fixture
    async def journal(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        await j.acquire_lease(f"test:{os.getpid()}:abc123")
        yield j
        await j.close()

    async def test_default_result_is_pending(self, journal):
        seq = journal.fenced_write("start", "jarvis_prime")
        entries = await journal.replay_from(seq - 1)
        assert entries[0]["result"] == "pending"

    async def test_mark_committed(self, journal):
        seq = journal.fenced_write("start", "jarvis_prime")
        journal.mark_result(seq, "committed")
        entries = await journal.replay_from(seq - 1)
        assert entries[0]["result"] == "committed"

    async def test_mark_failed(self, journal):
        seq = journal.fenced_write("start", "jarvis_prime")
        journal.mark_result(seq, "failed")
        entries = await journal.replay_from(seq - 1)
        assert entries[0]["result"] == "failed"

    async def test_invalid_result_raises(self, journal):
        seq = journal.fenced_write("start", "jarvis_prime")
        with pytest.raises(ValueError):
            journal.mark_result(seq, "invalid_status")


class TestLeaseAcquisition:
    async def test_first_boot_acquires_epoch_1(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        ok = await j.acquire_lease("supervisor:1:aaa")
        assert ok is True
        assert j.epoch == 1
        assert j.lease_held is True
        await j.close()

    async def test_reentrant_acquisition(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        await j.acquire_lease("supervisor:1:aaa")
        # Same holder acquires again — should succeed with same epoch
        ok = await j.acquire_lease("supervisor:1:aaa")
        assert ok is True
        assert j.epoch == 1
        await j.close()

    async def test_second_holder_blocked_while_live(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        await j1.acquire_lease("holder_a")
        await j1.renew_lease()

        # Second holder with short timeout — should fail
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)
        import backend.core.orchestration_journal as mod
        old_timeout = mod.LEASE_ACQUIRE_TIMEOUT_S
        mod.LEASE_ACQUIRE_TIMEOUT_S = 1.0  # Short timeout for test
        try:
            ok = await j2.acquire_lease("holder_b")
            assert ok is False
        finally:
            mod.LEASE_ACQUIRE_TIMEOUT_S = old_timeout
        await j1.close()
        await j2.close()

    async def test_expired_lease_claimed_with_new_epoch(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        import backend.core.orchestration_journal as mod
        db_path = tmp_path / "orchestration.db"

        # First holder acquires
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        await j1.acquire_lease("holder_a")
        epoch_a = j1.epoch
        await j1.close()

        # Simulate time passing beyond TTL by backdating last_renewed
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE lease SET last_renewed = ? WHERE id = 1",
            (time.time() - mod.LEASE_TTL_S - 5.0,),
        )
        conn.commit()
        conn.close()

        # Second holder acquires — should get epoch+1
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)
        ok = await j2.acquire_lease("holder_b")
        assert ok is True
        assert j2.epoch == epoch_a + 1
        await j2.close()


class TestEpochFencing:
    async def test_stale_epoch_raises(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal, StaleEpochError
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        await j.acquire_lease("holder_a")

        # Manually advance epoch in DB (simulating another leader)
        with j._write_lock:
            j._conn.execute(
                "UPDATE lease SET holder='holder_b', epoch=999 WHERE id=1"
            )
            j._conn.commit()

        with pytest.raises(StaleEpochError):
            j.fenced_write("start", "jarvis_prime")

    async def test_renewal_detects_fencing(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        await j.acquire_lease("holder_a")

        # Another leader takes over
        with j._write_lock:
            j._conn.execute(
                "UPDATE lease SET holder='holder_b', epoch=999 WHERE id=1"
            )
            j._conn.commit()

        ok = await j.renew_lease()
        assert ok is False
        assert j.lease_held is False
