"""Tests for F2 Slice 1 — BacklogSensor per-entry ``urgency_hint`` schema.

Scope: `memory/project_followup_f2_backlog_urgency_hint_schema.md` Slice 1.
Operator-authorized 2026-04-23 as Wave 3 (6) Slice 5a follow-up arc.

Contract (binding per F2 scope doc):

- Default unset master flag `JARVIS_BACKLOG_URGENCY_HINT_ENABLED=false` →
  per-entry `urgency_hint` is parsed into `BacklogTask` but NOT consumed
  for envelope urgency. Byte-identical to pre-F2 for default-off.
- Master flag on + entry has valid `urgency_hint` → envelope's urgency
  field reflects the hint, winning over BOTH the F3 session env override
  AND the priority-map default (most-specific wins).
- Master flag on + entry hint invalid → WARNING logged once per scan,
  affected entry falls back to priority-map / F3 override.
- Authority invariants: BacklogSensor stays grep-clean on the
  orchestrator/policy/iron_gate/risk_tier/change_engine/candidate_generator/gate
  ban list.
- No `routing_hint` consumption in Slice 1; that's Slice 2.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (
    BacklogSensor,
    BacklogTask,
    _urgency_hint_enabled,
    _validate_urgency_hint,
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


def _base_task(**overrides) -> dict:
    """Minimal valid backlog entry; overrides merge on top."""
    base = {
        "task_id": "t-001",
        "description": "F2 Slice 1 test task",
        "target_files": ["a.py"],
        "priority": 3,
        "repo": "jarvis",
        "status": "pending",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# (1) _urgency_hint_enabled helper — unit-level flag parsing
# ---------------------------------------------------------------------------


def test_hint_flag_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", raising=False)
    assert _urgency_hint_enabled() is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "YES"])
def test_hint_flag_truthy_values(monkeypatch, value):
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", value)
    assert _urgency_hint_enabled() is True


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "", "bogus", "  "])
def test_hint_flag_falsy_or_unknown_values(monkeypatch, value):
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", value)
    assert _urgency_hint_enabled() is False


# ---------------------------------------------------------------------------
# (2) _validate_urgency_hint — unit-level hint validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("critical", "critical"),
    ("high", "high"),
    ("normal", "normal"),
    ("low", "low"),
    # Case + whitespace normalization
    ("CRITICAL", "critical"),
    ("High", "high"),
    ("  normal  ", "normal"),
    ("LOW", "low"),
])
def test_validate_hint_accepts_valid(value, expected):
    assert _validate_urgency_hint(value) == expected


@pytest.mark.parametrize("value", [
    None, "", "  ", "urgent", "medium", "bogus", 3, 3.14, [], {}, True,
])
def test_validate_hint_rejects_invalid(value):
    assert _validate_urgency_hint(value) is None


# ---------------------------------------------------------------------------
# (3) BacklogTask dataclass — urgency_hint field
# ---------------------------------------------------------------------------


def test_backlog_task_default_urgency_hint_is_none():
    task = BacklogTask(
        task_id="t", description="d", target_files=["a"],
        priority=3, repo="jarvis", status="pending",
    )
    assert task.urgency_hint is None


def test_backlog_task_accepts_urgency_hint():
    task = BacklogTask(
        task_id="t", description="d", target_files=["a"],
        priority=3, repo="jarvis", status="pending",
        urgency_hint="critical",
    )
    assert task.urgency_hint == "critical"


def test_backlog_task_priority_map_unchanged_by_hint():
    """urgency_hint does NOT affect the .urgency property (priority-map);
    the sensor decides precedence at envelope-stamp time."""
    task = BacklogTask(
        task_id="t", description="d", target_files=["a"],
        priority=5, repo="jarvis", status="pending",
        urgency_hint="low",
    )
    assert task.urgency == "high"  # priority 5 → high, not "low"
    assert task.urgency_hint == "low"


# ---------------------------------------------------------------------------
# (4) scan_once — hint absent path (pre-F2 parity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hint_absent_flag_off_priority_map_wins(tmp_path, monkeypatch):
    """Default-off + no hint → byte-identical to pre-F2."""
    monkeypatch.delenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", raising=False)
    sensor, router = _make_sensor(tmp_path, [_base_task(priority=3)])
    produced = await sensor.scan_once()
    assert len(produced) == 1
    assert produced[0].urgency == "normal"  # priority 3 → normal


@pytest.mark.asyncio
async def test_hint_absent_flag_on_priority_map_wins(tmp_path, monkeypatch):
    """Flag on + no hint → priority-map default (hint absence is not error)."""
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    monkeypatch.delenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", raising=False)
    sensor, router = _make_sensor(tmp_path, [_base_task(priority=5)])
    produced = await sensor.scan_once()
    assert len(produced) == 1
    assert produced[0].urgency == "high"  # priority 5 → high


# ---------------------------------------------------------------------------
# (5) scan_once — hint present but flag off (parse-only, no consume)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hint_present_flag_off_is_ignored(tmp_path, monkeypatch):
    """Parse-only in default-off: envelope stays on priority-map."""
    monkeypatch.delenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", raising=False)
    sensor, router = _make_sensor(tmp_path, [
        _base_task(priority=1, urgency_hint="critical"),
    ])
    produced = await sensor.scan_once()
    assert len(produced) == 1
    # Priority 1 → low; hint was "critical" but flag off → not consumed.
    assert produced[0].urgency == "low"


# ---------------------------------------------------------------------------
# (6) scan_once — hint present + flag on (hint wins)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("hint", ["critical", "high", "normal", "low"])
async def test_hint_present_flag_on_overrides_priority_map(
    tmp_path, monkeypatch, hint,
):
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    monkeypatch.delenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", raising=False)
    sensor, router = _make_sensor(tmp_path, [
        _base_task(priority=3, urgency_hint=hint),  # priority 3 would be normal
    ])
    produced = await sensor.scan_once()
    assert len(produced) == 1
    assert produced[0].urgency == hint


@pytest.mark.asyncio
async def test_hint_present_flag_on_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    sensor, router = _make_sensor(tmp_path, [
        _base_task(priority=3, urgency_hint="CRITICAL"),
    ])
    produced = await sensor.scan_once()
    assert produced[0].urgency == "critical"


# ---------------------------------------------------------------------------
# (7) Precedence — per-entry hint > F3 env override > priority-map
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_entry_hint_beats_f3_env_override(tmp_path, monkeypatch):
    """Most-specific wins: per-entry hint > F3 env > priority-map."""
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "normal")
    sensor, router = _make_sensor(tmp_path, [
        _base_task(priority=1, urgency_hint="critical"),
    ])
    produced = await sensor.scan_once()
    assert produced[0].urgency == "critical"  # beats both F3 "normal" and priority 1 "low"


@pytest.mark.asyncio
async def test_f3_env_fallback_when_hint_flag_off(tmp_path, monkeypatch):
    """Flag off → F3 still applies to all emissions (byte-identical to pre-F2)."""
    monkeypatch.delenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "critical")
    sensor, router = _make_sensor(tmp_path, [
        _base_task(priority=1, urgency_hint="low"),  # hint ignored (flag off)
    ])
    produced = await sensor.scan_once()
    assert produced[0].urgency == "critical"  # F3 wins when hint unconsumed


@pytest.mark.asyncio
async def test_f3_env_fallback_when_hint_absent_but_flag_on(tmp_path, monkeypatch):
    """Flag on, no per-entry hint → F3 still takes precedence over priority-map."""
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "high")
    sensor, router = _make_sensor(tmp_path, [_base_task(priority=3)])  # no hint
    produced = await sensor.scan_once()
    assert produced[0].urgency == "high"  # F3 wins over priority-map "normal"


# ---------------------------------------------------------------------------
# (8) Invalid-hint path — WARNING + fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_hint_warns_and_falls_back(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    monkeypatch.delenv("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", raising=False)
    caplog.set_level(
        logging.WARNING,
        logger="backend.core.ouroboros.governance.intake.sensors.backlog_sensor",
    )
    sensor, router = _make_sensor(tmp_path, [
        _base_task(priority=3, urgency_hint="URGENT_NOW"),  # invalid
    ])
    produced = await sensor.scan_once()
    # Fallback to priority-map (priority 3 → normal).
    assert produced[0].urgency == "normal"
    # One WARNING emitted this scan.
    warning_lines = [
        r.message for r in caplog.records
        if r.levelname == "WARNING" and "invalid urgency_hint" in r.message
    ]
    assert len(warning_lines) == 1, (
        f"expected 1 WARNING for invalid hint, got {warning_lines}"
    )


@pytest.mark.asyncio
async def test_invalid_hints_collapse_to_one_warning_per_scan(
    tmp_path, monkeypatch, caplog,
):
    """Multiple invalid hints in one scan → one WARNING (not per-entry spam)."""
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    caplog.set_level(
        logging.WARNING,
        logger="backend.core.ouroboros.governance.intake.sensors.backlog_sensor",
    )
    sensor, router = _make_sensor(tmp_path, [
        _base_task(task_id="t-1", priority=3, urgency_hint="bogus"),
        _base_task(task_id="t-2", priority=3, urgency_hint="alsobad"),
    ])
    await sensor.scan_once()
    warning_lines = [
        r.message for r in caplog.records
        if r.levelname == "WARNING" and "invalid urgency_hint" in r.message
    ]
    assert len(warning_lines) == 1


# ---------------------------------------------------------------------------
# (9) Telemetry — "hint consumed" INFO log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hint_applied_emits_one_info_per_scan(
    tmp_path, monkeypatch, caplog,
):
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    caplog.set_level(
        logging.INFO,
        logger="backend.core.ouroboros.governance.intake.sensors.backlog_sensor",
    )
    sensor, router = _make_sensor(tmp_path, [
        _base_task(task_id="t-1", priority=3, urgency_hint="critical"),
        _base_task(task_id="t-2", priority=3, urgency_hint="high"),
    ])
    await sensor.scan_once()
    info_lines = [
        r.message for r in caplog.records
        if "JARVIS_BACKLOG_URGENCY_HINT_ENABLED active" in r.message
    ]
    assert len(info_lines) == 1, (
        f"expected 1 INFO for hint consumption, got {info_lines}"
    )
    # Ledger-parseable marker content.
    assert "per-entry urgency_hint consumed" in info_lines[0]


@pytest.mark.asyncio
async def test_no_info_emitted_when_no_hint_consumed(
    tmp_path, monkeypatch, caplog,
):
    """Flag on but no entries have hints → no "hint consumed" INFO."""
    monkeypatch.setenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "true")
    caplog.set_level(
        logging.INFO,
        logger="backend.core.ouroboros.governance.intake.sensors.backlog_sensor",
    )
    sensor, router = _make_sensor(tmp_path, [_base_task(priority=3)])
    await sensor.scan_once()
    info_lines = [
        r.message for r in caplog.records
        if "JARVIS_BACKLOG_URGENCY_HINT_ENABLED active" in r.message
    ]
    assert info_lines == []


# ---------------------------------------------------------------------------
# (10) Authority invariant — BacklogSensor stays grep-clean
# ---------------------------------------------------------------------------


def test_backlog_sensor_authority_invariant():
    """F2 Slice 1 additions must NOT introduce imports of orchestrator /
    policy / iron_gate / risk_tier / change_engine / candidate_generator /
    gate. Same rule every Wave 1 arc file respects.
    """
    sensor_path = (
        Path(__file__).resolve().parents[4]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "intake"
        / "sensors"
        / "backlog_sensor.py"
    )
    source = sensor_path.read_text(encoding="utf-8")
    banned = [
        r"from backend\.core\.ouroboros\.governance\.orchestrator\b",
        r"import backend\.core\.ouroboros\.governance\.orchestrator\b",
        r"from backend\.core\.ouroboros\.governance\.policy\b",
        r"from backend\.core\.ouroboros\.governance\.iron_gate\b",
        r"from backend\.core\.ouroboros\.governance\.risk_tier\b",
        r"from backend\.core\.ouroboros\.governance\.change_engine\b",
        r"from backend\.core\.ouroboros\.governance\.candidate_generator\b",
        r"from backend\.core\.ouroboros\.governance\.gate\b",
    ]
    for pattern in banned:
        assert not re.search(pattern, source), (
            f"BacklogSensor imports banned authority module: {pattern}"
        )
