"""Tests for CurriculumPublisher."""
import json
import tempfile
from pathlib import Path

from backend.core.ouroboros.governance.curriculum_publisher import (
    CurriculumPublisher,
)
from backend.core.ouroboros.integration import PerformanceRecord, PerformanceRecordPersistence, TaskDifficulty


def _make_record(model_id="m1", task_type="bug_fix", success=True, latency_ms=500.0):
    from datetime import datetime
    return PerformanceRecord(
        model_id=model_id, task_type=task_type, difficulty=TaskDifficulty.MODERATE,
        success=success, latency_ms=latency_ms, iterations_used=1, code_quality_score=0.8,
        timestamp=datetime.now(),
    )


async def _populate(p, task_type, n_fail, n_pass):
    for _ in range(n_fail):
        await p._save_to_sqlite([_make_record(task_type=task_type, success=False)])
    for _ in range(n_pass):
        await p._save_to_sqlite([_make_record(task_type=task_type, success=True)])


class TestCurriculumPublisher:
    async def test_returns_none_when_no_data(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ev:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            publisher = CurriculumPublisher(persistence=p, event_dir=Path(ev), min_sample_size=3)
            result = await publisher.publish()
            assert result is None

    async def test_writes_json_file_when_data_available(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ev:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            await _populate(p, "bug_fix", n_fail=7, n_pass=3)
            publisher = CurriculumPublisher(persistence=p, event_dir=Path(ev), top_k=5, min_sample_size=3)
            result = await publisher.publish()
            assert result is not None
            files = list(Path(ev).glob("curriculum_*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["schema_version"] == "curriculum.1"
            assert data["event_type"] == "curriculum_signal"
            assert len(data["top_k"]) >= 1

    async def test_top_k_priorities_sum_to_1(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ev:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            await _populate(p, "bug_fix", n_fail=6, n_pass=4)
            await _populate(p, "code_improvement", n_fail=4, n_pass=6)
            await _populate(p, "testing", n_fail=8, n_pass=2)
            publisher = CurriculumPublisher(persistence=p, event_dir=Path(ev), top_k=5, min_sample_size=3)
            result = await publisher.publish()
            assert result is not None
            total = sum(e.priority for e in result.top_k)
            assert abs(total - 1.0) < 1e-6

    async def test_high_failure_rate_gets_higher_priority(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ev:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            await _populate(p, "bug_fix", n_fail=9, n_pass=1)      # 90% failure
            await _populate(p, "code_improvement", n_fail=1, n_pass=9)  # 10% failure
            publisher = CurriculumPublisher(persistence=p, event_dir=Path(ev), top_k=5, min_sample_size=3)
            result = await publisher.publish()
            assert result is not None
            entries = {e.task_type: e for e in result.top_k}
            assert entries["bug_fix"].priority > entries["code_improvement"].priority

    async def test_publish_never_raises_on_empty_db(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ev:
            p = PerformanceRecordPersistence(storage_path=Path(tmp), use_sqlite=True)
            publisher = CurriculumPublisher(persistence=p, event_dir=Path(ev))
            result = await publisher.publish()  # must not raise
            assert result is None
