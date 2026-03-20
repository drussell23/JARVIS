"""Tests for ScheduledTriggerSensor (Sensor E)."""
from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# YAML helper
# ---------------------------------------------------------------------------

_SAMPLE_YAML = """\
schedules:
  - name: security_audit
    cron: "0 2 * * 0"
    goal: "Scan for security vulnerabilities"
    target_files:
      - "backend/core/auth.py"
    repo: jarvis
    source: ai_miner
    urgency: normal
    requires_human_ack: true
    enabled: true

  - name: disabled_task
    cron: "0 0 * * *"
    goal: "This is disabled"
    target_files:
      - "README.md"
    enabled: false

  - name: auto_sweep
    cron: "*/5 * * * *"
    goal: "Quick sweep"
    target_files:
      - "backend/"
      - "tests/"
    repo: jarvis
    requires_human_ack: false
    confidence: 0.65
"""


def _write_config(path: Path, content: str = _SAMPLE_YAML) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_router() -> MagicMock:
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    return router


# ---------------------------------------------------------------------------
# Imports — guarded so the test file itself loads even without croniter
# ---------------------------------------------------------------------------

from backend.core.ouroboros.governance.intake.sensors.scheduled_sensor import (
    ScheduleEntry,
    ScheduledTriggerSensor,
)


# ===================================================================
# 1. Sensor stays inactive when croniter is not installed
# ===================================================================


async def test_sensor_inactive_without_croniter(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_config(config_path)
    router = _make_router()
    sensor = ScheduledTriggerSensor(
        router=router, config_path=config_path, check_interval_s=0.05
    )

    # Simulate croniter being missing
    with patch.dict(sys.modules, {"croniter": None}):
        # Force re-import attempt to hit ImportError
        with patch("builtins.__import__", side_effect=_import_blocker("croniter")):
            await sensor.start()

    assert sensor._running is False
    assert sensor._croniter_available is False
    assert sensor._check_task is None
    await sensor.stop()


def _import_blocker(blocked_name: str):
    """Return a side_effect callable that blocks a specific import."""
    _real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def _blocker(name: str, *args: Any, **kwargs: Any):
        if name == blocked_name:
            raise ImportError(f"No module named {blocked_name!r}")
        return _real_import(name, *args, **kwargs)

    return _blocker


# ===================================================================
# 2. Sensor loads config from YAML file
# ===================================================================


async def test_load_config_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_config(config_path)
    router = _make_router()
    sensor = ScheduledTriggerSensor(
        router=router, config_path=config_path, check_interval_s=60.0
    )

    entries = sensor._load_config()
    # disabled_task should be filtered out
    assert len(entries) == 2
    names = [e.name for e in entries]
    assert "security_audit" in names
    assert "auto_sweep" in names
    assert "disabled_task" not in names


# ===================================================================
# 3. Sensor ignores disabled entries
# ===================================================================


async def test_disabled_entries_excluded(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_config(config_path)
    router = _make_router()
    sensor = ScheduledTriggerSensor(
        router=router, config_path=config_path, check_interval_s=60.0
    )
    entries = sensor._load_config()
    for entry in entries:
        assert entry.enabled is True


# ===================================================================
# 4. _should_fire returns True when cron matches
# ===================================================================


async def test_should_fire_returns_true_when_due() -> None:
    """Schedule with last_fired in the past and a cron that has since ticked."""
    croniter = pytest.importorskip("croniter", reason="croniter required")

    router = _make_router()
    sensor = ScheduledTriggerSensor(router=router, check_interval_s=60.0)
    sensor._croniter_available = True

    entry = ScheduleEntry(
        name="test",
        cron="* * * * *",  # every minute
        goal="test goal",
        target_files=("backend/foo.py",),
        last_fired=datetime.now() - timedelta(minutes=2),
    )
    assert sensor._should_fire(entry, datetime.now()) is True


# ===================================================================
# 5. _should_fire returns False when cron does not match
# ===================================================================


async def test_should_fire_returns_false_when_not_due() -> None:
    """Schedule that just fired should not fire again immediately."""
    croniter = pytest.importorskip("croniter", reason="croniter required")

    router = _make_router()
    sensor = ScheduledTriggerSensor(router=router, check_interval_s=60.0)
    sensor._croniter_available = True

    now = datetime.now()
    entry = ScheduleEntry(
        name="test",
        cron="0 0 1 1 *",  # once per year, Jan 1 midnight
        goal="test goal",
        target_files=("backend/foo.py",),
        last_fired=now - timedelta(seconds=30),
    )
    # Unless it happens to be exactly midnight Jan 1, this should be False.
    assert sensor._should_fire(entry, now) is False


# ===================================================================
# 6. _fire creates IntentEnvelope and submits to router
# ===================================================================


async def test_fire_creates_and_submits_envelope() -> None:
    router = _make_router()
    sensor = ScheduledTriggerSensor(router=router, check_interval_s=60.0)

    entry = ScheduleEntry(
        name="security_audit",
        cron="0 2 * * 0",
        goal="Scan for vulnerabilities",
        target_files=("backend/core/auth.py",),
        repo="jarvis",
        source="ai_miner",
        urgency="normal",
        requires_human_ack=True,
        confidence=0.8,
    )

    now = datetime.now()
    await sensor._fire(entry, now)

    router.ingest.assert_called_once()
    envelope = router.ingest.call_args[0][0]
    assert envelope.source == "ai_miner"
    assert envelope.description == "Scan for vulnerabilities"
    assert "backend/core/auth.py" in envelope.target_files
    assert envelope.repo == "jarvis"
    assert envelope.urgency == "normal"
    assert envelope.requires_human_ack is True
    assert envelope.confidence == 0.8
    assert envelope.evidence["trigger"] == "scheduled"
    assert envelope.evidence["schedule_name"] == "security_audit"
    assert envelope.evidence["cron"] == "0 2 * * 0"


# ===================================================================
# 7. _fire sets last_fired on the schedule entry
# ===================================================================


async def test_fire_sets_last_fired() -> None:
    router = _make_router()
    sensor = ScheduledTriggerSensor(router=router, check_interval_s=60.0)

    entry = ScheduleEntry(
        name="test_schedule",
        cron="* * * * *",
        goal="test",
        target_files=("backend/foo.py",),
    )
    assert entry.last_fired is None

    now = datetime.now()
    await sensor._fire(entry, now)

    assert entry.last_fired == now


# ===================================================================
# 8. Invalid cron expressions handled gracefully
# ===================================================================


async def test_invalid_cron_expression_handled() -> None:
    croniter = pytest.importorskip("croniter", reason="croniter required")

    router = _make_router()
    sensor = ScheduledTriggerSensor(router=router, check_interval_s=60.0)
    sensor._croniter_available = True

    entry = ScheduleEntry(
        name="bad_cron",
        cron="not-a-valid-cron",
        goal="test",
        target_files=("backend/foo.py",),
    )
    # Should return False without raising
    result = sensor._should_fire(entry, datetime.now())
    assert result is False


# ===================================================================
# 9. health() returns correct structure
# ===================================================================


async def test_health_returns_correct_structure(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_config(config_path)
    router = _make_router()
    sensor = ScheduledTriggerSensor(
        router=router, config_path=config_path, check_interval_s=60.0
    )
    # Load config to populate schedules
    sensor._schedules = sensor._load_config()

    h = sensor.health()
    assert isinstance(h, dict)
    assert "running" in h
    assert "croniter_available" in h
    assert "schedule_count" in h
    assert "fires_count" in h
    assert "config_path" in h
    assert "schedules" in h
    assert h["running"] is False
    assert h["schedule_count"] == 2  # disabled entry excluded
    assert h["fires_count"] == 0
    assert h["config_path"] == str(config_path)

    for sched in h["schedules"]:
        assert "name" in sched
        assert "cron" in sched
        assert "enabled" in sched
        assert "last_fired" in sched


# ===================================================================
# 10. stop() cleans up properly
# ===================================================================


async def test_stop_cleans_up() -> None:
    croniter = pytest.importorskip("croniter", reason="croniter required")

    router = _make_router()
    sensor = ScheduledTriggerSensor(router=router, check_interval_s=0.05)
    sensor._croniter_available = True
    sensor._schedules = [
        ScheduleEntry(
            name="fast",
            cron="* * * * *",
            goal="test",
            target_files=("backend/foo.py",),
        )
    ]
    sensor._running = True
    sensor._check_task = asyncio.create_task(sensor._check_loop())

    # Let it tick once
    await asyncio.sleep(0.1)
    assert sensor._running is True

    await sensor.stop()
    assert sensor._running is False
    assert sensor._check_task is None


# ===================================================================
# 11. Missing config file returns empty schedules
# ===================================================================


async def test_missing_config_file(tmp_path: Path) -> None:
    router = _make_router()
    sensor = ScheduledTriggerSensor(
        router=router,
        config_path=tmp_path / "nonexistent.yaml",
        check_interval_s=60.0,
    )
    entries = sensor._load_config()
    assert entries == []


# ===================================================================
# 12. Config with empty target_files gets default sentinel
# ===================================================================


async def test_empty_target_files_gets_sentinel(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_config(
        config_path,
        """\
schedules:
  - name: no_targets
    cron: "0 0 * * *"
    goal: "No target files specified"
    target_files: []
""",
    )
    router = _make_router()
    sensor = ScheduledTriggerSensor(
        router=router, config_path=config_path, check_interval_s=60.0
    )
    entries = sensor._load_config()
    assert len(entries) == 1
    # Should have the sentinel "." to satisfy non-empty constraint
    assert entries[0].target_files == (".",)


# ===================================================================
# 13. reload_config hot-reloads the YAML
# ===================================================================


async def test_reload_config(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_config(config_path)
    router = _make_router()
    sensor = ScheduledTriggerSensor(
        router=router, config_path=config_path, check_interval_s=60.0
    )

    count = sensor.reload_config()
    assert count == 2  # security_audit + auto_sweep

    # Overwrite with a single schedule
    _write_config(
        config_path,
        """\
schedules:
  - name: only_one
    cron: "0 0 * * *"
    goal: "Just one schedule"
    target_files:
      - "backend/main.py"
""",
    )
    count = sensor.reload_config()
    assert count == 1
    assert sensor._schedules[0].name == "only_one"


# ===================================================================
# 14. fires_count increments on successful fire
# ===================================================================


async def test_fires_count_increments() -> None:
    router = _make_router()
    sensor = ScheduledTriggerSensor(router=router, check_interval_s=60.0)
    assert sensor._fires_count == 0

    entry = ScheduleEntry(
        name="counter_test",
        cron="* * * * *",
        goal="test counting",
        target_files=("backend/foo.py",),
    )
    await sensor._fire(entry, datetime.now())
    assert sensor._fires_count == 1

    await sensor._fire(entry, datetime.now())
    assert sensor._fires_count == 2


# ===================================================================
# 15. _fire handles router ingest failure gracefully
# ===================================================================


async def test_fire_handles_ingest_error() -> None:
    router = MagicMock()
    router.ingest = AsyncMock(side_effect=RuntimeError("router down"))
    sensor = ScheduledTriggerSensor(router=router, check_interval_s=60.0)

    entry = ScheduleEntry(
        name="error_test",
        cron="* * * * *",
        goal="should fail gracefully",
        target_files=("backend/foo.py",),
    )
    # Should not raise
    await sensor._fire(entry, datetime.now())
    # fires_count should NOT have incremented
    assert sensor._fires_count == 0
    # last_fired should NOT have been set
    assert entry.last_fired is None


# ===================================================================
# 16. Config defaults are applied correctly
# ===================================================================


async def test_config_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_config(
        config_path,
        """\
schedules:
  - name: minimal
    cron: "0 0 * * *"
    goal: "Minimal entry with defaults"
    target_files:
      - "backend/main.py"
""",
    )
    router = _make_router()
    sensor = ScheduledTriggerSensor(
        router=router, config_path=config_path, check_interval_s=60.0
    )
    entries = sensor._load_config()
    assert len(entries) == 1
    e = entries[0]
    assert e.repo == "jarvis"
    assert e.source == "ai_miner"
    assert e.urgency == "normal"
    assert e.requires_human_ack is True
    assert e.enabled is True
    assert e.confidence == 0.8
