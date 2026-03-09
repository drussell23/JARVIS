"""Tests for extended PerformanceRecord v2 fields and ModelAttributionRecord."""
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from backend.core.ouroboros.integration import (
    ModelAttributionRecord,
    PerformanceRecord,
    PerformanceRecordPersistence,
    TaskDifficulty,
)


class TestPerformanceRecordV2Fields:
    def test_v2_fields_have_safe_defaults(self):
        rec = PerformanceRecord(
            model_id="m1",
            task_type="code_improvement",
            difficulty=TaskDifficulty.MODERATE,
            success=True,
            latency_ms=500.0,
            iterations_used=1,
            code_quality_score=0.9,
        )
        assert rec.op_id == ""
        assert rec.patch_hash == ""
        assert rec.pass_rate == 0.0
        assert rec.lint_violations == 0
        assert rec.coverage_pct == 0.0
        assert rec.complexity_delta == 0.0

    async def test_v2_fields_roundtrip_through_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            rec = PerformanceRecord(
                model_id="m1",
                task_type="bug_fix",
                difficulty=TaskDifficulty.HARD,
                success=True,
                latency_ms=1200.0,
                iterations_used=2,
                code_quality_score=0.75,
                op_id="op-abc123",
                patch_hash="deadbeef",
                pass_rate=0.95,
                lint_violations=3,
                coverage_pct=72.5,
                complexity_delta=-0.8,
            )
            await p._save_to_sqlite([rec])
            results = await p.get_records_by_model_and_task("m1", "bug_fix", limit=10)
            assert len(results) == 1
            r = results[0]
            assert r.op_id == "op-abc123"
            assert r.patch_hash == "deadbeef"
            assert abs(r.pass_rate - 0.95) < 1e-6
            assert r.lint_violations == 3
            assert abs(r.coverage_pct - 72.5) < 1e-6
            assert abs(r.complexity_delta - (-0.8)) < 1e-6


class TestModelAttributionRecord:
    def test_record_dataclass_fields(self):
        rec = ModelAttributionRecord(
            model_id="v2",
            previous_model_id="v1",
            training_batch_size=40,
            task_type="refactoring",
            success_rate_delta=0.12,
            latency_delta_ms=-80.0,
            quality_delta=0.05,
            sample_size=15,
            confidence=0.75,
            summary="v2 improved refactoring by 12%",
            recorded_at=datetime(2026, 3, 9),
        )
        assert rec.model_id == "v2"
        assert rec.confidence == 0.75

    async def test_save_attribution_record_persists_to_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            rec = ModelAttributionRecord(
                model_id="v2",
                previous_model_id="v1",
                training_batch_size=40,
                task_type="refactoring",
                success_rate_delta=0.12,
                latency_delta_ms=-80.0,
                quality_delta=0.05,
                sample_size=15,
                confidence=0.75,
                summary="summary",
                recorded_at=datetime(2026, 3, 9),
            )
            await p.save_attribution_record(rec)
            db_path = Path(tmp) / "performance_records.db"
            with sqlite3.connect(db_path) as conn:
                row = conn.execute("SELECT model_id FROM model_attribution").fetchone()
            assert row is not None
            assert row[0] == "v2"


class TestSchemaMigration:
    def test_v1_database_migrated_to_v2_on_init(self):
        """An existing v1 DB gains the new columns after PerformanceRecordPersistence init."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "performance_records.db"
            # Create a v1 DB manually (no new columns)
            with sqlite3.connect(db_path) as conn:
                conn.execute("""
                    CREATE TABLE performance_records (
                        id INTEGER PRIMARY KEY,
                        model_id TEXT, task_type TEXT, difficulty TEXT,
                        success INTEGER, latency_ms REAL, iterations_used INTEGER,
                        code_quality_score REAL, timestamp TEXT, error_message TEXT,
                        context_tokens INTEGER DEFAULT 0,
                        output_tokens INTEGER DEFAULT 0,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute(
                    "CREATE TABLE schema_version (version INTEGER PRIMARY KEY)"
                )
                conn.execute("INSERT INTO schema_version VALUES (1)")
                conn.commit()

            # Init should detect v1 and migrate
            PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)

            with sqlite3.connect(db_path) as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(performance_records)")}
                assert "op_id" in cols
                assert "patch_hash" in cols
                assert "pass_rate" in cols
                assert "lint_violations" in cols
                assert "coverage_pct" in cols
                assert "complexity_delta" in cols
                # model_attribution table exists
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                assert "model_attribution" in tables
                version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
            assert version == 2
