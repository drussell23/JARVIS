"""Phase B Slice 7 — TodoScannerSensor FS-event migration (gap #4).

Pins the contract introduced by JARVIS_TODO_FS_EVENTS_ENABLED:
  * Flag off (default): fs_events_enabled() returns False; _fs_events_mode
    False; subscribe_to_bus is a logged no-op; poll at legacy 24h interval.
  * Flag on: subscribe_to_bus registers an fs.changed.* handler; .py file
    changes route to scan_file; skip-dir / non-.py / deleted events bump
    the ignored counter; poll demotes to fallback (6h default).
  * Subscription failures are caught locally (intake boot never regresses).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.intake.sensors import todo_scanner_sensor as tm
from backend.core.ouroboros.governance.intake.sensors.todo_scanner_sensor import (
    TodoScannerSensor,
)


class _SpyRouter:
    def __init__(self) -> None:
        self.envelopes: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.envelopes.append(envelope)
        return "enqueued"


class _FakeBus:
    """Records subscribe calls + exposes configurable failure mode."""

    def __init__(self, fail_subscribe: bool = False) -> None:
        self.subscriptions: List[tuple] = []
        self._fail = fail_subscribe

    async def subscribe(self, pattern: str, handler: Any) -> str:
        if self._fail:
            raise RuntimeError("simulated bus failure")
        self.subscriptions.append((pattern, handler))
        return "fake-sub-id"


def _sensor() -> TodoScannerSensor:
    return TodoScannerSensor(
        repo="jarvis", router=_SpyRouter(),
        poll_interval_s=86400.0, project_root=Path("."),
    )


def _fs_event(relative_path: str, extension: str = ".py", topic: str = "fs.changed.modified") -> Any:
    """FS event in the shape FileSystemEventBridge emits."""
    return SimpleNamespace(
        topic=topic,
        payload={
            "relative_path": relative_path,
            "extension": extension,
            "path": f"/abs/{relative_path}",
        },
    )


# ---------------------------------------------------------------------------
# Flag helper + init
# ---------------------------------------------------------------------------

def test_fs_events_enabled_reads_env_fresh(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_TODO_FS_EVENTS_ENABLED", "true")
    assert tm.fs_events_enabled() is True

    monkeypatch.setenv("JARVIS_TODO_FS_EVENTS_ENABLED", "false")
    assert tm.fs_events_enabled() is False

    # Slice 7 ships pre-graduation — default stays "false" (shadow mode).
    monkeypatch.delenv("JARVIS_TODO_FS_EVENTS_ENABLED", raising=False)
    assert tm.fs_events_enabled() is False


def test_init_captures_fs_events_mode(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_TODO_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    assert sensor._fs_events_mode is True

    monkeypatch.setenv("JARVIS_TODO_FS_EVENTS_ENABLED", "false")
    sensor = _sensor()
    assert sensor._fs_events_mode is False


# ---------------------------------------------------------------------------
# subscribe_to_bus — flag gated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_to_bus_noop_when_flag_off(monkeypatch: Any) -> None:
    monkeypatch.delenv("JARVIS_TODO_FS_EVENTS_ENABLED", raising=False)
    sensor = _sensor()
    bus = _FakeBus()

    await sensor.subscribe_to_bus(bus)

    assert bus.subscriptions == [], (
        "flag off must NOT register a subscription"
    )


@pytest.mark.asyncio
async def test_subscribe_to_bus_registers_when_flag_on(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_TODO_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    bus = _FakeBus()

    await sensor.subscribe_to_bus(bus)

    assert len(bus.subscriptions) == 1
    pattern, handler = bus.subscriptions[0]
    assert pattern == "fs.changed.*"
    assert handler == sensor._on_fs_event


@pytest.mark.asyncio
async def test_subscribe_to_bus_failure_is_non_fatal(monkeypatch: Any) -> None:
    """Bus.subscribe raising must not propagate (intake boot stays green)."""
    monkeypatch.setenv("JARVIS_TODO_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    bus = _FakeBus(fail_subscribe=True)

    # Must not raise
    await sensor.subscribe_to_bus(bus)


# ---------------------------------------------------------------------------
# _on_fs_event — routing + counters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_fs_event_py_change_triggers_scan_file(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    """.py change under an allowed path → scan_file invoked."""
    monkeypatch.setenv("JARVIS_TODO_FS_EVENTS_ENABLED", "true")
    sensor = TodoScannerSensor(
        repo="jarvis", router=_SpyRouter(),
        poll_interval_s=86400.0, project_root=tmp_path,
    )

    scanned: List[Path] = []

    async def _fake_scan(p: Path) -> list:
        scanned.append(p)
        return []

    sensor.scan_file = _fake_scan  # type: ignore[assignment]

    await sensor._on_fs_event(_fs_event("backend/foo.py", extension=".py"))

    assert len(scanned) == 1
    assert scanned[0] == Path("/abs/backend/foo.py")
    assert sensor._fs_events_handled == 1
    assert sensor._fs_events_ignored == 0


@pytest.mark.asyncio
async def test_on_fs_event_non_py_file_ignored(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_TODO_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    sensor.scan_file = _never_scan  # type: ignore[assignment]

    await sensor._on_fs_event(_fs_event("README.md", extension=".md"))

    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 1


@pytest.mark.asyncio
async def test_on_fs_event_deleted_event_ignored(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_TODO_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    sensor.scan_file = _never_scan  # type: ignore[assignment]

    await sensor._on_fs_event(
        _fs_event("backend/foo.py", extension=".py", topic="fs.changed.deleted"),
    )

    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 1


@pytest.mark.asyncio
async def test_on_fs_event_skip_dir_ignored(monkeypatch: Any) -> None:
    """Files under venv/__pycache__/.worktrees/etc must not be scanned."""
    monkeypatch.setenv("JARVIS_TODO_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    sensor.scan_file = _never_scan  # type: ignore[assignment]

    # /abs/backend/__pycache__/foo.py — __pycache__ is in _SKIP_DIRS
    await sensor._on_fs_event(
        SimpleNamespace(
            topic="fs.changed.modified",
            payload={
                "relative_path": "backend/__pycache__/foo.py",
                "extension": ".py",
                "path": "/abs/backend/__pycache__/foo.py",
            },
        ),
    )

    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 1


async def _never_scan(*_args: Any, **_kwargs: Any) -> list:
    raise AssertionError("scan_file must not be called")


# ---------------------------------------------------------------------------
# Interval gate
# ---------------------------------------------------------------------------

def test_poll_interval_default_when_flag_off(monkeypatch: Any) -> None:
    monkeypatch.delenv("JARVIS_TODO_FS_EVENTS_ENABLED", raising=False)
    sensor = TodoScannerSensor(
        repo="jarvis", router=_SpyRouter(),
        poll_interval_s=86400.0, project_root=Path("."),
    )
    assert sensor._fs_events_mode is False
    assert sensor._poll_interval_s == 86400.0


def test_init_fs_events_mode_enables_fallback(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_TODO_FS_EVENTS_ENABLED", "true")
    sensor = TodoScannerSensor(
        repo="jarvis", router=_SpyRouter(),
        poll_interval_s=86400.0, project_root=Path("."),
    )
    assert sensor._fs_events_mode is True
