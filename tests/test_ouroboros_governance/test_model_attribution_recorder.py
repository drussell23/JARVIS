"""Tests for ModelAttributionRecorder."""
import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.model_attribution_recorder import ModelAttributionRecorder
from backend.core.ouroboros.integration import (
    ModelAttributionRecord,
    PerformanceRecord,
    PerformanceRecordPersistence,
    TaskDifficulty,
)


def _make_record(model_id: str, task_type: str, success: bool, latency_ms: float = 500.0, quality: float = 0.8):
    return PerformanceRecord(
        model_id=model_id,
        task_type=task_type,
        difficulty=TaskDifficulty.MODERATE,
        success=success,
        latency_ms=latency_ms,
        iterations_used=1,
        code_quality_score=quality,
    )


async def _populate(persistence, model_id, task_type, count, success_rate, latency_ms=500.0, quality=0.8):
    for i in range(count):
        rec = _make_record(model_id, task_type, (i / count) < success_rate, latency_ms, quality)
        await persistence._save_to_sqlite([rec])


class TestModelAttributionRecorder:
    async def test_records_written_for_sufficient_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            await _populate(p, "v2", "bug_fix", 10, success_rate=0.9, quality=0.85)
            await _populate(p, "v1", "bug_fix", 10, success_rate=0.6, quality=0.70)
            recorder = ModelAttributionRecorder(persistence=p, lookback_n=20, min_sample_size=3)
            results = await recorder.record_model_transition(
                new_model_id="v2", previous_model_id="v1", training_batch_size=40
            )
            assert len(results) >= 1
            r = results[0]
            assert r.task_type == "bug_fix"
            assert r.model_id == "v2"
            assert r.previous_model_id == "v1"
            assert 0.0 <= r.confidence <= 1.0
            assert "v2" in r.summary

    async def test_skips_task_type_with_insufficient_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            # Only 2 records for v2 (below min_sample_size=3)
            await _populate(p, "v2", "testing", 2, success_rate=1.0)
            await _populate(p, "v1", "testing", 10, success_rate=0.5)
            recorder = ModelAttributionRecorder(persistence=p, lookback_n=20, min_sample_size=3)
            results = await recorder.record_model_transition("v2", "v1", 20)
            assert all(r.task_type != "testing" for r in results)

    async def test_confidence_capped_at_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            await _populate(p, "v2", "code_improvement", 25, success_rate=0.8)
            await _populate(p, "v1", "code_improvement", 25, success_rate=0.5)
            recorder = ModelAttributionRecorder(persistence=p, lookback_n=20)
            results = await recorder.record_model_transition("v2", "v1", 20)
            for r in results:
                assert r.confidence <= 1.0

    async def test_record_persisted_to_database(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            await _populate(p, "v2", "refactoring", 5, success_rate=0.8)
            await _populate(p, "v1", "refactoring", 5, success_rate=0.5)
            recorder = ModelAttributionRecorder(persistence=p, lookback_n=20, min_sample_size=3)
            results = await recorder.record_model_transition("v2", "v1", 10)
            assert len(results) >= 1
            db = Path(tmp) / "performance_records.db"
            with sqlite3.connect(db) as conn:
                count = conn.execute("SELECT COUNT(*) FROM model_attribution").fetchone()[0]
            assert count >= 1
