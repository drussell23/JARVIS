"""Tests for F3 — BacklogSensor JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY override.

Scope: backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py
F3 block. Env knob added 2026-04-23 as a Wave 3 (6) Slice 5a side-arc
to unblock graduation reachability when BACKGROUND-class backlog ops
die upstream of pctx.generation.

Contract (binding per operator F3 contract 2026-04-23):

- Default unset → priority→urgency map preserved byte-identical.
- Set to one of {critical, high, normal, low} → overrides emitted
  envelope urgency for ALL BacklogSensor tasks this scan.
- Invalid value → falls back to default (no crash, no override).
- One INFO log per scan cycle that produces envelopes with override
  active (not per task — keeps telemetry concise).
- Only affects this sensor's emissions. No UrgencyRouter or intake
  router changes; those are F1 (non-blocking follow-up).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (
    BacklogSensor,
    _default_urgency_override,
)


def _write_backlog(path: Path, tasks: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tasks))


def _make_sensor(tmp_path: Path, tasks: list) -> tuple[BacklogSensor, MagicMock]:
    backlog_path = tmp_path / ".jarvis" / "backlog.json"
    _write_backlog(backlog_path, tasks)
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = BacklogSensor(
        backlog_path=backlog_path,
        repo_root=tmp_path,
        router=router,
        poll_interval_s=0.01,
    )
    return sensor, router


# ---------------------------------------------------------------------------
# (1) _default_urgency_override helper — unit-level
# ---------------------------------------------------------------------------


def test_override_unset_returns_none(monkeypatch):
    monkeypatch.delenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", raising=False)
    assert _default_urgency_override() is None


def test_override_empty_string_returns_none(monkeypatch):
    monkeypatch.setenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "")
    assert _default_urgency_override() is None


def test_override_whitespace_only_returns_none(monkeypatch):
    monkeypatch.setenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "   ")
    assert _default_urgency_override() is None


@pytest.mark.parametrize("value", ["critical", "high", "normal", "low"])
def test_override_accepts_valid_urgencies(monkeypatch, value):
    monkeypatch.setenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", value)
    assert _default_urgency_override() == value


@pytest.mark.parametrize("value", ["CRITICAL", "High", "Normal", "LOW"])
def test_override_is_case_insensitive(monkeypatch, value):
    monkeypatch.setenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", value)
    assert _default_urgency_override() == value.lower()


@pytest.mark.parametrize("value", ["urgent", "extreme", "foo", "100", "none"])
def test_override_invalid_values_fall_back_to_none(monkeypatch, value):
    """Invalid urgency strings MUST NOT raise — graceful fallback to
    priority-based default (preserves behavioral parity)."""
    monkeypatch.setenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", value)
    assert _default_urgency_override() is None


# ---------------------------------------------------------------------------
# (2) Default behavior byte-identical to pre-F3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_unset_preserves_priority_urgency_mapping(tmp_path, monkeypatch):
    """With env unset, emitted envelope urgency must match the
    pre-F3 priority→urgency map exactly."""
    monkeypatch.delenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", raising=False)
    # priority 5 → high; priority 3 → normal; priority 1 → low
    sensor, router = _make_sensor(tmp_path, [
        {"task_id": "t-high", "description": "d", "target_files": ["a.py"],
         "priority": 5, "repo": "jarvis", "status": "pending"},
        {"task_id": "t-normal", "description": "d", "target_files": ["b.py"],
         "priority": 3, "repo": "jarvis", "status": "pending"},
        {"task_id": "t-low", "description": "d", "target_files": ["c.py"],
         "priority": 1, "repo": "jarvis", "status": "pending"},
    ])
    envelopes = await sensor.scan_once()
    urgencies = {
        e.evidence["task_id"]: e.urgency for e in envelopes
    }
    assert urgencies == {"t-high": "high", "t-normal": "normal", "t-low": "low"}


@pytest.mark.asyncio
async def test_invalid_override_falls_back_to_priority_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "urgent-please")
    sensor, _ = _make_sensor(tmp_path, [
        {"task_id": "t5", "description": "d", "target_files": ["a.py"],
         "priority": 5, "repo": "jarvis", "status": "pending"},
    ])
    envelopes = await sensor.scan_once()
    assert envelopes[0].urgency == "high"  # priority 5 → high; override ignored


# ---------------------------------------------------------------------------
# (3) Override path applies the override urgency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("override", ["critical", "high", "normal", "low"])
@pytest.mark.asyncio
async def test_override_replaces_priority_mapping(tmp_path, monkeypatch, override):
    """When the override is set, emitted envelope urgency = override
    regardless of the task's priority."""
    monkeypatch.setenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", override)
    sensor, _ = _make_sensor(tmp_path, [
        {"task_id": "t-p1", "description": "d", "target_files": ["a.py"],
         "priority": 1, "repo": "jarvis", "status": "pending"},  # priority 1 → "low" under default
        {"task_id": "t-p5", "description": "d", "target_files": ["b.py"],
         "priority": 5, "repo": "jarvis", "status": "pending"},  # priority 5 → "high" under default
    ])
    envelopes = await sensor.scan_once()
    for env in envelopes:
        assert env.urgency == override


@pytest.mark.asyncio
async def test_override_critical_lets_graduation_seed_escape_background(
    tmp_path, monkeypatch
):
    """Practical Slice 5a verification: override=critical produces
    envelopes with urgency='critical', which UrgencyRouter's Priority-1
    branch routes IMMEDIATE regardless of source=backlog membership in
    _BACKGROUND_SOURCES. This is the escape path F3 exists to unblock."""
    monkeypatch.setenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "critical")
    sensor, _ = _make_sensor(tmp_path, [
        {"task_id": "seed", "description": "forced-reach seed",
         "target_files": ["a.py", "b.py", "c.py"],
         "priority": 1, "repo": "jarvis", "status": "pending"},  # priority 1 would normally be "low"
    ])
    envelopes = await sensor.scan_once()
    assert envelopes[0].urgency == "critical"


# ---------------------------------------------------------------------------
# (4) Telemetry — one INFO log per scan cycle (not per task)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_emits_exactly_one_info_log_per_scan(
    tmp_path, monkeypatch, caplog
):
    """The override info log fires at most once per scan_once call,
    not once per task — keeps telemetry concise under bulk backlogs."""
    monkeypatch.setenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "critical")
    caplog.set_level(
        logging.INFO,
        logger="backend.core.ouroboros.governance.intake.sensors.backlog_sensor",
    )
    sensor, _ = _make_sensor(tmp_path, [
        {"task_id": f"t{i}", "description": "d", "target_files": ["a.py"],
         "priority": 3, "repo": "jarvis", "status": "pending"}
        for i in range(5)
    ])
    await sensor.scan_once()
    override_logs = [
        r for r in caplog.records
        if "JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY" in r.message
        and "override active" in r.message
    ]
    assert len(override_logs) == 1, (
        f"expected exactly 1 override-active log, got {len(override_logs)}: "
        f"{[r.message for r in override_logs]}"
    )
    # The log carries the applied urgency value for §8 auditability.
    assert "critical" in override_logs[0].message


@pytest.mark.asyncio
async def test_default_path_emits_no_override_log(tmp_path, monkeypatch, caplog):
    """When override is unset, the INFO log must NOT fire — otherwise
    operators who don't set the knob would see spurious telemetry."""
    monkeypatch.delenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", raising=False)
    caplog.set_level(
        logging.INFO,
        logger="backend.core.ouroboros.governance.intake.sensors.backlog_sensor",
    )
    sensor, _ = _make_sensor(tmp_path, [
        {"task_id": "t", "description": "d", "target_files": ["a.py"],
         "priority": 3, "repo": "jarvis", "status": "pending"},
    ])
    await sensor.scan_once()
    override_logs = [
        r for r in caplog.records
        if "JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY" in r.message
    ]
    assert override_logs == []
