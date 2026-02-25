"""Tests for DiskGuard and compaction."""
import gzip
import json
import unittest
from pathlib import Path
import tempfile


class TestDiskGuard(unittest.TestCase):
    def test_reports_usage(self):
        from backend.core.trace_store import DiskGuard
        with tempfile.TemporaryDirectory() as tmp:
            guard = DiskGuard(base_dir=Path(tmp))
            usage = guard.check_disk_usage()
            assert 0.0 <= usage <= 1.0

    def test_rotation_priority(self):
        from backend.core.trace_store import DiskGuard
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Force rotation by setting critical threshold to 0.0
            guard = DiskGuard(base_dir=tmp_path, critical_threshold=0.0)
            # Create old files in each stream
            for stream in ["spans", "decisions", "lifecycle"]:
                d = tmp_path / stream
                d.mkdir(parents=True, exist_ok=True)
                (d / "old_file.jsonl").write_text('{"old": true}\n')
            rotated = guard.rotate_if_needed(current_epoch="current-epoch")
            # Spans should be rotated first
            assert not (tmp_path / "spans" / "old_file.jsonl").exists()
            assert len(rotated) > 0

    def test_preserves_current_epoch(self):
        from backend.core.trace_store import DiskGuard
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            guard = DiskGuard(base_dir=tmp_path, critical_threshold=0.0)
            spans_dir = tmp_path / "spans"
            spans_dir.mkdir(parents=True, exist_ok=True)
            (spans_dir / "epoch_current-epoch_spans.jsonl").write_text('{"data": 1}\n')
            (spans_dir / "old_data.jsonl").write_text('{"data": 2}\n')
            guard.rotate_if_needed(current_epoch="current-epoch")
            # Current epoch file preserved
            assert (spans_dir / "epoch_current-epoch_spans.jsonl").exists()
            # Old file deleted
            assert not (spans_dir / "old_data.jsonl").exists()

    def test_no_rotation_below_threshold(self):
        from backend.core.trace_store import DiskGuard
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # High threshold = no rotation needed
            guard = DiskGuard(base_dir=tmp_path, critical_threshold=1.0)
            spans_dir = tmp_path / "spans"
            spans_dir.mkdir(parents=True, exist_ok=True)
            (spans_dir / "old_file.jsonl").write_text('{"old": true}\n')
            rotated = guard.rotate_if_needed()
            assert len(rotated) == 0
            assert (spans_dir / "old_file.jsonl").exists()


class TestCompaction(unittest.TestCase):
    def test_compress_old_files(self):
        from backend.core.trace_store import compact_old_files
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            spans_dir = tmp_path / "spans"
            spans_dir.mkdir()
            (spans_dir / "old.jsonl").write_text('{"data": "test"}\n')
            compact_old_files(spans_dir, max_age_days=0)  # Compress everything
            assert (spans_dir / "old.jsonl.gz").exists()
            assert not (spans_dir / "old.jsonl").exists()
            # Verify content is valid
            with gzip.open(spans_dir / "old.jsonl.gz", "rt") as f:
                data = json.loads(f.read().strip())
                assert data["data"] == "test"

    def test_preserves_recent_files(self):
        from backend.core.trace_store import compact_old_files
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            spans_dir = tmp_path / "spans"
            spans_dir.mkdir()
            (spans_dir / "recent.jsonl").write_text('{"data": "fresh"}\n')
            compact_old_files(spans_dir, max_age_days=30)  # Only very old
            # Recent file should still be there
            assert (spans_dir / "recent.jsonl").exists()
            assert not (spans_dir / "recent.jsonl.gz").exists()


if __name__ == "__main__":
    unittest.main()
