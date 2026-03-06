"""Tests for the append-only journal backed by SQLite WAL.

Covers append/read-back, monotonic revisions, filtering by key and
revision, persistence across close/reopen, deterministic checksums,
and gap detection.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from backend.core.reactive_state.journal import AppendOnlyJournal, _compute_checksum
from backend.core.reactive_state.types import JournalEntry


# ── Helpers ────────────────────────────────────────────────────────────


def _append_sample(
    journal: AppendOnlyJournal,
    *,
    key: str = "gcp.vm_ready",
    value: object = True,
    previous_value: object = None,
    version: int = 1,
    epoch: int = 0,
    writer: str = "supervisor",
    writer_session_id: str = "sess-abc-123",
    origin: str = "explicit",
    consistency_group: str | None = None,
) -> JournalEntry:
    """Append a sample entry with sensible defaults."""
    return journal.append(
        key=key,
        value=value,
        previous_value=previous_value,
        version=version,
        epoch=epoch,
        writer=writer,
        writer_session_id=writer_session_id,
        origin=origin,
        consistency_group=consistency_group,
    )


# ── Tests ──────────────────────────────────────────────────────────────


class TestAppendAndReadBack:
    """append() persists an entry and read_since() retrieves it."""

    def test_append_and_read_back_fields_correct(self, tmp_path):
        db = tmp_path / "journal.db"
        j = AppendOnlyJournal(db)
        j.open()
        try:
            entry = _append_sample(
                j,
                key="audio.active",
                value={"level": 42},
                previous_value={"level": 0},
                version=3,
                epoch=1,
                writer="voice_orch",
                writer_session_id="sess-vo-001",
                origin="derived",
                consistency_group="audio_group",
            )

            assert isinstance(entry, JournalEntry)
            assert entry.global_revision == 1
            assert entry.key == "audio.active"
            assert entry.value == {"level": 42}
            assert entry.previous_value == {"level": 0}
            assert entry.version == 3
            assert entry.epoch == 1
            assert entry.writer == "voice_orch"
            assert entry.writer_session_id == "sess-vo-001"
            assert entry.origin == "derived"
            assert entry.consistency_group == "audio_group"
            assert entry.timestamp_unix_ms > 0
            assert isinstance(entry.checksum, str)
            assert len(entry.checksum) == 64  # SHA-256 hex digest

            # read back
            rows = j.read_since(1)
            assert len(rows) == 1
            assert rows[0] == entry
        finally:
            j.close()


class TestMonotonicRevisions:
    """Consecutive appends produce strictly increasing revisions."""

    def test_revisions_are_monotonic(self, tmp_path):
        db = tmp_path / "journal.db"
        j = AppendOnlyJournal(db)
        j.open()
        try:
            e1 = _append_sample(j, key="a", version=1)
            e2 = _append_sample(j, key="b", version=1)
            e3 = _append_sample(j, key="a", version=2)

            assert e1.global_revision == 1
            assert e2.global_revision == 2
            assert e3.global_revision == 3
        finally:
            j.close()


class TestReadSince:
    """read_since(from_revision) filters correctly."""

    def test_read_since_filters_correctly(self, tmp_path):
        db = tmp_path / "journal.db"
        j = AppendOnlyJournal(db)
        j.open()
        try:
            _append_sample(j, key="a", version=1)
            _append_sample(j, key="b", version=1)
            _append_sample(j, key="c", version=1)

            # from_revision=2 should return revisions 2 and 3
            rows = j.read_since(2)
            assert len(rows) == 2
            assert rows[0].global_revision == 2
            assert rows[1].global_revision == 3

            # from_revision=4 should return nothing
            assert j.read_since(4) == []
        finally:
            j.close()


class TestReadKeyHistory:
    """read_key_history(key) filters by key."""

    def test_read_key_history_filters_by_key(self, tmp_path):
        db = tmp_path / "journal.db"
        j = AppendOnlyJournal(db)
        j.open()
        try:
            _append_sample(j, key="gcp.vm_ready", version=1)
            _append_sample(j, key="audio.active", version=1)
            _append_sample(j, key="gcp.vm_ready", version=2)

            history = j.read_key_history("gcp.vm_ready")
            assert len(history) == 2
            assert all(e.key == "gcp.vm_ready" for e in history)
            assert history[0].global_revision == 1
            assert history[1].global_revision == 3

            # non-existent key returns empty
            assert j.read_key_history("nonexistent") == []
        finally:
            j.close()


class TestLatestRevision:
    """latest_revision() starts at 0 and increments."""

    def test_latest_revision_starts_at_zero_and_increments(self, tmp_path):
        db = tmp_path / "journal.db"
        j = AppendOnlyJournal(db)
        j.open()
        try:
            assert j.latest_revision() == 0

            _append_sample(j, key="a", version=1)
            assert j.latest_revision() == 1

            _append_sample(j, key="b", version=1)
            assert j.latest_revision() == 2
        finally:
            j.close()


class TestDeterministicChecksum:
    """Same inputs in two journals produce the same checksum."""

    def test_checksum_is_deterministic(self, tmp_path):
        db1 = tmp_path / "journal1.db"
        db2 = tmp_path / "journal2.db"

        j1 = AppendOnlyJournal(db1)
        j2 = AppendOnlyJournal(db2)
        j1.open()
        j2.open()
        try:
            kwargs = dict(
                key="gcp.vm_ready",
                value=True,
                previous_value=False,
                version=1,
                epoch=0,
                writer="supervisor",
                writer_session_id="sess-abc-123",
                origin="explicit",
                consistency_group=None,
            )
            e1 = _append_sample(j1, **kwargs)
            e2 = _append_sample(j2, **kwargs)

            # Both should get global_revision=1 and identical inputs
            assert e1.checksum == e2.checksum
            assert len(e1.checksum) == 64
        finally:
            j1.close()
            j2.close()

    def test_compute_checksum_is_pure(self):
        """Calling _compute_checksum with identical args returns same hash."""
        c1 = _compute_checksum(1, "k", "v", "pv", 1, 0, "sess", None)
        c2 = _compute_checksum(1, "k", "v", "pv", 1, 0, "sess", None)
        assert c1 == c2
        assert len(c1) == 64


class TestPersistenceAcrossReopen:
    """Data survives close/reopen cycle."""

    def test_persistence_across_close_reopen(self, tmp_path):
        db = tmp_path / "journal.db"

        j = AppendOnlyJournal(db)
        j.open()
        e1 = _append_sample(j, key="gcp.vm_ready", version=1)
        e2 = _append_sample(j, key="audio.active", version=1)
        j.close()

        # Reopen
        j2 = AppendOnlyJournal(db)
        j2.open()
        try:
            assert j2.latest_revision() == 2

            rows = j2.read_since(1)
            assert len(rows) == 2
            assert rows[0].key == "gcp.vm_ready"
            assert rows[1].key == "audio.active"
            assert rows[0].checksum == e1.checksum
            assert rows[1].checksum == e2.checksum

            # New appends continue from revision 3
            e3 = _append_sample(j2, key="prime.endpoint", version=1)
            assert e3.global_revision == 3
        finally:
            j2.close()


class TestGapDetection:
    """validate_no_gaps() detects missing revisions."""

    def test_gap_detection_with_injected_gap(self, tmp_path):
        db = tmp_path / "journal.db"

        j = AppendOnlyJournal(db)
        j.open()
        _append_sample(j, key="a", version=1)  # revision 1
        j.close()

        # Manually inject a row with revision 3, skipping 2
        conn = sqlite3.connect(str(db))
        value_json = json.dumps(True, sort_keys=True, separators=(",", ":"))
        prev_json = json.dumps(None, sort_keys=True, separators=(",", ":"))
        checksum = _compute_checksum(3, "b", value_json, prev_json, 1, 0, "sess", None)
        conn.execute(
            "INSERT INTO state_journal "
            "(global_revision, key, value, previous_value, version, epoch, "
            "writer, writer_session_id, origin, consistency_group, "
            "timestamp_unix_ms, checksum) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (3, "b", value_json, prev_json, 1, 0, "test", "sess", "explicit", None, 0, checksum),
        )
        conn.commit()
        conn.close()

        j2 = AppendOnlyJournal(db)
        j2.open()
        try:
            gaps = j2.validate_no_gaps()
            assert len(gaps) >= 1
            assert any("2" in g and "3" in g for g in gaps), (
                f"Expected gap description mentioning revisions 2 and 3, got: {gaps}"
            )
        finally:
            j2.close()

    def test_no_gaps_returns_empty(self, tmp_path):
        db = tmp_path / "journal.db"
        j = AppendOnlyJournal(db)
        j.open()
        try:
            # Empty journal -- no gaps
            assert j.validate_no_gaps() == []

            _append_sample(j, key="a", version=1)
            _append_sample(j, key="b", version=1)
            _append_sample(j, key="c", version=1)

            assert j.validate_no_gaps() == []
        finally:
            j.close()
