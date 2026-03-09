"""Tests for BacklogSensor (Sensor A)."""
import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (
    BacklogSensor,
    BacklogTask,
)


def _write_backlog(path: Path, tasks: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tasks))


def test_backlog_task_urgency_low_priority():
    task = BacklogTask(
        task_id="t1",
        description="improve caching",
        target_files=["backend/core/cache.py"],
        priority=1,
        repo="jarvis",
    )
    assert task.urgency == "low"


def test_backlog_task_urgency_high_priority():
    task = BacklogTask(
        task_id="t2",
        description="fix critical bug",
        target_files=["backend/core/auth.py"],
        priority=5,
        repo="jarvis",
    )
    assert task.urgency == "high"


async def test_sensor_produces_envelope_for_pending_task(tmp_path):
    backlog_path = tmp_path / ".jarvis" / "backlog.json"
    _write_backlog(backlog_path, [
        {
            "task_id": "t1",
            "description": "fix auth",
            "target_files": ["backend/core/auth.py"],
            "priority": 4,
            "repo": "jarvis",
            "status": "pending",
        }
    ])
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = BacklogSensor(
        backlog_path=backlog_path,
        repo_root=tmp_path,
        router=router,
        poll_interval_s=0.01,
    )
    envelopes = await sensor.scan_once()
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.source == "backlog"
    assert env.target_files == ("backend/core/auth.py",)
    assert env.urgency == "high"
    router.ingest.assert_called_once_with(env)


async def test_sensor_skips_completed_tasks(tmp_path):
    backlog_path = tmp_path / ".jarvis" / "backlog.json"
    _write_backlog(backlog_path, [
        {
            "task_id": "t1",
            "description": "done task",
            "target_files": ["backend/core/foo.py"],
            "priority": 3,
            "repo": "jarvis",
            "status": "completed",
        }
    ])
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = BacklogSensor(
        backlog_path=backlog_path,
        repo_root=tmp_path,
        router=router,
    )
    envelopes = await sensor.scan_once()
    assert envelopes == []
    router.ingest.assert_not_called()


async def test_sensor_missing_backlog_returns_empty(tmp_path):
    router = MagicMock()
    router.ingest = AsyncMock()
    sensor = BacklogSensor(
        backlog_path=tmp_path / "nonexistent.json",
        repo_root=tmp_path,
        router=router,
    )
    envelopes = await sensor.scan_once()
    assert envelopes == []


async def test_sensor_start_stop(tmp_path):
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = BacklogSensor(
        backlog_path=tmp_path / ".jarvis" / "backlog.json",
        repo_root=tmp_path,
        router=router,
        poll_interval_s=0.05,
    )
    await sensor.start()
    await asyncio.sleep(0.1)
    sensor.stop()
    # No crash = pass
