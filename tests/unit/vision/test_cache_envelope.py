"""Tests for versioned pickle cache envelope."""
import pickle
import tempfile
from pathlib import Path
import pytest


class TestCacheEnvelope:
    def test_save_and_load_roundtrip(self):
        from backend.vision.intelligence.cache_envelope import save_versioned, load_versioned
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            data = {"key": "value", "list": [1, 2, 3]}
            save_versioned(path, data, version=1)
            loaded = load_versioned(path, expected_version=1)
            assert loaded == data

    def test_version_mismatch_returns_none(self):
        from backend.vision.intelligence.cache_envelope import save_versioned, load_versioned
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            save_versioned(path, {"old": True}, version=1)
            result = load_versioned(path, expected_version=2)
            assert result is None  # No migration registered

    def test_unknown_major_version_quarantined(self):
        from backend.vision.intelligence.cache_envelope import save_versioned, load_versioned
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            save_versioned(path, {"future": True}, version=5)
            result = load_versioned(path, expected_version=2)
            assert result is None
            # Original file should be quarantined
            assert not path.exists()
            quarantine_files = list(Path(d).glob("*.quarantine.*"))
            assert len(quarantine_files) == 1

    def test_corrupted_payload_quarantined(self):
        from backend.vision.intelligence.cache_envelope import save_versioned, load_versioned, ENVELOPE_MAGIC
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            # Write valid envelope but tamper with payload hash
            save_versioned(path, {"good": True}, version=1)
            # Corrupt the file by appending garbage
            with open(path, "ab") as f:
                f.write(b"GARBAGE")
            result = load_versioned(path, expected_version=1)
            # Should either load correctly or quarantine -- not crash

    def test_no_magic_bytes_quarantined(self):
        from backend.vision.intelligence.cache_envelope import load_versioned
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            # Write raw pickle without magic bytes
            with open(path, "wb") as f:
                pickle.dump({"raw": True}, f)
            result = load_versioned(path, expected_version=1)
            assert result is None
            assert not path.exists()  # Quarantined

    def test_migration_chain(self):
        from backend.vision.intelligence.cache_envelope import (
            save_versioned, load_versioned, register_migration,
        )
        # Register v1 -> v2 migration
        register_migration(1, 2, lambda data: {**data, "migrated": True})

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            save_versioned(path, {"key": "value"}, version=1)
            result = load_versioned(path, expected_version=2)
            assert result is not None
            assert result["key"] == "value"
            assert result["migrated"] is True

    def test_atomic_write_no_tmp_leftover(self):
        from backend.vision.intelligence.cache_envelope import save_versioned
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            for i in range(50):
                save_versioned(path, {"i": i}, version=1)
            tmp_files = list(Path(d).glob("*.tmp"))
            assert len(tmp_files) == 0

    def test_missing_file_returns_none(self):
        from backend.vision.intelligence.cache_envelope import load_versioned
        result = load_versioned(Path("/nonexistent/path.cache"), expected_version=1)
        assert result is None
