"""Tests for WAL (Write-Ahead Log) append/replay/compaction."""
import json
import time
from pathlib import Path
import pytest

from backend.core.ouroboros.governance.intake.wal import WAL, WALEntry


def _entry(lease_id: str = "l1", ts: float = 0.0) -> WALEntry:
    return WALEntry(
        lease_id=lease_id,
        envelope_dict={"source": "backlog", "description": "test"},
        status="pending",
        ts_monotonic=ts or time.monotonic(),
        ts_utc="2026-03-08T00:00:00Z",
    )


def test_append_creates_file(tmp_path):
    wal = WAL(tmp_path / "test.jsonl")
    wal.append(_entry("l1"))
    assert (tmp_path / "test.jsonl").exists()


def test_append_produces_valid_json(tmp_path):
    wal = WAL(tmp_path / "test.jsonl")
    wal.append(_entry("l1"))
    lines = (tmp_path / "test.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["lease_id"] == "l1"
    assert record["status"] == "pending"


def test_pending_entries_returns_pending(tmp_path):
    wal = WAL(tmp_path / "test.jsonl")
    wal.append(_entry("l1"))
    wal.append(_entry("l2"))
    pending = wal.pending_entries()
    assert {e.lease_id for e in pending} == {"l1", "l2"}


def test_update_status_removes_from_pending(tmp_path):
    wal = WAL(tmp_path / "test.jsonl")
    wal.append(_entry("l1"))
    wal.append(_entry("l2"))
    wal.update_status("l1", "acked")
    pending = wal.pending_entries()
    assert len(pending) == 1
    assert pending[0].lease_id == "l2"


def test_dead_letter_not_in_pending(tmp_path):
    wal = WAL(tmp_path / "test.jsonl")
    wal.append(_entry("l1"))
    wal.update_status("l1", "dead_letter")
    assert wal.pending_entries() == []


def test_compact_removes_old_entries(tmp_path):
    wal = WAL(tmp_path / "test.jsonl", max_age_days=0)
    # Write entry with very old monotonic (simulate age)
    old_entry = WALEntry(
        lease_id="old",
        envelope_dict={},
        status="acked",
        ts_monotonic=0.0,  # effectively ancient
        ts_utc="2020-01-01T00:00:00Z",
    )
    wal.append(old_entry)
    wal.append(_entry("new"))
    removed = wal.compact()
    assert removed >= 1
    lines = (tmp_path / "test.jsonl").read_text().strip().splitlines()
    lease_ids = [json.loads(l)["lease_id"] for l in lines if l]
    assert "old" not in lease_ids


def test_corrupt_line_skipped_gracefully(tmp_path):
    p = tmp_path / "test.jsonl"
    p.write_text('{"lease_id":"l1","envelope":{},"status":"pending","ts_monotonic":1.0,"ts_utc":""}\nNOT_JSON\n')
    wal = WAL(p)
    pending = wal.pending_entries()
    assert len(pending) == 1
    assert pending[0].lease_id == "l1"
