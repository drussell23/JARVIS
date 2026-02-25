# tests/unit/core/test_journal_compaction.py
"""Tests for journal compaction -- archival of old entries."""

import asyncio
import sqlite3
import time

import pytest

from backend.core.orchestration_journal import (
    OrchestrationJournal,
    CompactionResult,
    COMPACTION_RETAIN_PRIOR_EPOCHS,
)


# -- Helpers -------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_journal(db_path, holder="leader1"):
    """Create and initialize a journal with a lease."""
    j = OrchestrationJournal()
    await j.initialize(db_path)
    await j.acquire_lease(holder)
    return j


# -- Tests ---------------------------------------------------------------------

class TestJournalCompactionSchema:
    def test_archive_table_created(self, tmp_path):
        """Verify journal_archive table exists after initialization."""
        j = _run(_make_journal(tmp_path / "test.db"))
        try:
            conn = sqlite3.connect(str(tmp_path / "test.db"))
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='journal_archive'"
            ).fetchone()
            conn.close()
            assert row is not None, "journal_archive table not created"
        finally:
            _run(j.close())

    def test_archive_table_columns(self, tmp_path):
        """Verify journal_archive has correct columns including archived_at."""
        j = _run(_make_journal(tmp_path / "test.db"))
        try:
            conn = sqlite3.connect(str(tmp_path / "test.db"))
            rows = conn.execute("PRAGMA table_info(journal_archive)").fetchall()
            col_names = [r[1] for r in rows]
            conn.close()
            expected = [
                "seq", "epoch", "timestamp", "wall_clock", "actor",
                "action", "target", "idempotency_key", "payload",
                "result", "fence_token", "archived_at",
            ]
            for col in expected:
                assert col in col_names, f"Missing column: {col}"
        finally:
            _run(j.close())


class TestJournalCompactionAlgorithm:
    def test_noop_on_small_journal(self, tmp_path):
        """Compact is no-op when prior-epoch entries <= retention threshold."""
        j = _run(_make_journal(tmp_path / "test.db"))
        try:
            # Write 50 entries in epoch 1
            for i in range(50):
                j.fenced_write("test", f"comp_{i}", payload={"n": i})

            # Acquire epoch 2
            _run(j.acquire_lease("leader2"))

            result = j.compact()
            assert result.entries_archived == 0
            assert result.entries_remaining > 0
        finally:
            _run(j.close())

    def test_retains_current_epoch_entries(self, tmp_path):
        """All current-epoch entries must survive compaction."""
        j = _run(_make_journal(tmp_path / "test.db"))
        try:
            # Write 1200 entries in epoch 1 (exceeds COMPACTION_RETAIN_PRIOR_EPOCHS if < 1200)
            for i in range(1200):
                j.fenced_write("test", "comp_a", payload={"n": i})

            # Acquire epoch 2
            _run(j.acquire_lease("leader2"))

            # Write 10 entries in epoch 2
            epoch2_seqs = []
            for i in range(10):
                seq = j.fenced_write("test_e2", "comp_b", payload={"e2": i})
                epoch2_seqs.append(seq)

            result = j.compact()

            # Verify all epoch 2 entries survived
            conn = sqlite3.connect(str(tmp_path / "test.db"))
            for seq in epoch2_seqs:
                row = conn.execute("SELECT seq FROM journal WHERE seq = ?", (seq,)).fetchone()
                assert row is not None, f"Epoch 2 entry seq={seq} was deleted!"
            conn.close()
        finally:
            _run(j.close())

    def test_archives_to_journal_archive_table(self, tmp_path):
        """Compacted entries land in journal_archive with archived_at timestamp."""
        j = _run(_make_journal(tmp_path / "test.db"))
        try:
            before = time.time()
            # Write 1200 entries in epoch 1
            for i in range(1200):
                j.fenced_write("test", "comp_a", payload={"n": i})

            # Acquire epoch 2
            _run(j.acquire_lease("leader2"))

            result = j.compact()
            assert result.entries_archived > 0

            # Verify entries exist in archive
            conn = sqlite3.connect(str(tmp_path / "test.db"))
            archive_count = conn.execute("SELECT COUNT(*) FROM journal_archive").fetchone()[0]
            assert archive_count == result.entries_archived

            # Verify archived_at is set
            row = conn.execute("SELECT MIN(archived_at) FROM journal_archive").fetchone()
            assert row[0] is not None
            assert row[0] >= before

            # Verify remaining prior-epoch entries = COMPACTION_RETAIN_PRIOR_EPOCHS
            prior_remaining = conn.execute(
                "SELECT COUNT(*) FROM journal WHERE epoch < ?", (j.epoch,)
            ).fetchone()[0]
            assert prior_remaining == COMPACTION_RETAIN_PRIOR_EPOCHS

            conn.close()
        finally:
            _run(j.close())

    def test_fk_integrity_preserved(self, tmp_path):
        """component_state.last_seq updated when referenced entry is compacted."""
        j = _run(_make_journal(tmp_path / "test.db"))
        try:
            # Write entries in epoch 1
            seqs = []
            for i in range(1200):
                seq = j.fenced_write("test", "comp_a", payload={"n": i})
                seqs.append(seq)

            # Point component_state to an early entry that will be compacted
            early_seq = seqs[10]  # This will be in the compacted range
            j.update_component_state("fk_test_comp", "ready", early_seq)

            # Acquire epoch 2
            _run(j.acquire_lease("leader2"))

            result = j.compact()
            assert result.entries_archived > 0

            # Verify last_seq was updated to a valid seq
            state = j.get_component_state("fk_test_comp")
            assert state is not None
            updated_seq = state["last_seq"]

            # The updated seq must exist in the journal
            conn = sqlite3.connect(str(tmp_path / "test.db"))
            row = conn.execute("SELECT seq FROM journal WHERE seq = ?", (updated_seq,)).fetchone()
            assert row is not None, f"FK broken: last_seq={updated_seq} not in journal"
            conn.close()
        finally:
            _run(j.close())

    def test_compaction_is_atomic(self, tmp_path):
        """If epoch changes mid-compact, no partial deletes occur."""
        j = _run(_make_journal(tmp_path / "test.db"))
        try:
            # Write 1200 entries in epoch 1
            for i in range(1200):
                j.fenced_write("test", "comp_a", payload={"n": i})

            # Acquire epoch 2
            _run(j.acquire_lease("leader2"))

            # Count entries before
            conn = sqlite3.connect(str(tmp_path / "test.db"))
            before_count = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
            conn.close()

            # Tamper with epoch to simulate lease loss during verify
            original_verify = j._verify_epoch
            call_count = [0]

            def fencing_verify():
                call_count[0] += 1
                if call_count[0] == 2:  # Second verify (inside transaction) fails
                    from backend.core.orchestration_journal import StaleEpochError
                    raise StaleEpochError("simulated epoch loss")
                original_verify()

            j._verify_epoch = fencing_verify

            from backend.core.orchestration_journal import StaleEpochError
            with pytest.raises(StaleEpochError):
                j.compact()

            # Verify no entries were deleted
            conn = sqlite3.connect(str(tmp_path / "test.db"))
            after_count = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
            conn.close()

            assert after_count == before_count, (
                f"Atomicity violated: {before_count} entries before, {after_count} after"
            )
        finally:
            _run(j.close())

    def test_compaction_result_dataclass(self, tmp_path):
        """CompactionResult has correct fields and types."""
        r = CompactionResult(entries_archived=100, entries_remaining=500, duration_s=0.5)
        assert r.entries_archived == 100
        assert r.entries_remaining == 500
        assert r.duration_s == 0.5

    def test_replay_after_compaction(self, tmp_path):
        """replay_from works correctly after compaction (returns only retained entries)."""
        j = _run(_make_journal(tmp_path / "test.db"))
        try:
            # Write 1200 entries in epoch 1
            all_seqs = []
            for i in range(1200):
                seq = j.fenced_write("test", "comp_a", payload={"n": i})
                all_seqs.append(seq)

            # Acquire epoch 2
            _run(j.acquire_lease("leader2"))

            result = j.compact()
            assert result.entries_archived > 0

            # replay_from(0) should return remaining entries without error
            entries = _run(j.replay_from(0))
            assert len(entries) > 0

            # All returned entries should have valid seqs that exist in journal
            conn = sqlite3.connect(str(tmp_path / "test.db"))
            for entry in entries:
                row = conn.execute(
                    "SELECT seq FROM journal WHERE seq = ?", (entry["seq"],)
                ).fetchone()
                assert row is not None, f"Replay returned nonexistent seq={entry['seq']}"
            conn.close()
        finally:
            _run(j.close())
