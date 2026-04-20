"""Gap #4 — BacklogSensor FS-event migration regression spine.

Pins the contract introduced by JARVIS_BACKLOG_FS_EVENTS_ENABLED:
  * Flag off (default): fs_events_enabled() returns False; _fs_events_mode
    False; subscribe_to_bus is a logged no-op; poll at legacy 60s interval.
  * Flag on: subscribe_to_bus registers an fs.changed.* handler; backlog.json
    changes route to scan_once; non-matching paths bump ignored counter;
    poll demotes to fallback (3600s default).
  * Subscription failures are caught locally (intake boot never regresses).
  * Telemetry distinguishes fs_event vs fallback_poll origins.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    backlog_sensor as bm,
)
from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (
    BacklogSensor,
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


def _sensor(tmp_path: Path | None = None) -> BacklogSensor:
    root = tmp_path or Path(".")
    return BacklogSensor(
        backlog_path=root / ".jarvis" / "backlog.json",
        repo_root=root,
        router=_SpyRouter(),
        poll_interval_s=60.0,
    )


def _fs_event(
    relative_path: str,
    topic: str = "fs.changed.modified",
) -> Any:
    """FS event in the shape FileSystemEventBridge emits."""
    return SimpleNamespace(
        topic=topic,
        payload={
            "relative_path": relative_path,
            "path": f"/abs/{relative_path}",
        },
    )


# ---------------------------------------------------------------------------
# Flag helper + init
# ---------------------------------------------------------------------------

def test_fs_events_enabled_reads_env_fresh(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "true")
    assert bm.fs_events_enabled() is True

    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "false")
    assert bm.fs_events_enabled() is False

    # Graduated 2026-04-20 — default is now "true" (FS-events primary).
    monkeypatch.delenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", raising=False)
    assert bm.fs_events_enabled() is True


def test_init_captures_fs_events_mode(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    assert sensor._fs_events_mode is True
    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 0

    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "false")
    sensor = _sensor()
    assert sensor._fs_events_mode is False


# ---------------------------------------------------------------------------
# subscribe_to_bus — flag gated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_to_bus_noop_when_flag_off(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "false")
    sensor = _sensor()
    bus = _FakeBus()

    await sensor.subscribe_to_bus(bus)

    assert bus.subscriptions == [], (
        "flag off must NOT register a subscription"
    )


@pytest.mark.asyncio
async def test_subscribe_to_bus_registers_when_flag_on(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    bus = _FakeBus()

    await sensor.subscribe_to_bus(bus)

    assert len(bus.subscriptions) == 1
    pattern, handler = bus.subscriptions[0]
    assert pattern == "fs.changed.*"
    assert handler == sensor._on_fs_event


@pytest.mark.asyncio
async def test_subscribe_to_bus_failure_is_non_fatal(
    monkeypatch: Any,
) -> None:
    """Bus.subscribe raising must not propagate (intake boot stays green)."""
    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    bus = _FakeBus(fail_subscribe=True)

    # Must not raise
    await sensor.subscribe_to_bus(bus)


# ---------------------------------------------------------------------------
# _on_fs_event — routing + counters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_fs_event_backlog_change_triggers_scan_once(
    monkeypatch: Any,
) -> None:
    """backlog.json change → scan_once invoked; handled counter bumps."""
    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()

    invocations: List[int] = []

    async def _fake_scan() -> list:
        invocations.append(1)
        return []

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    await sensor._on_fs_event(_fs_event(".jarvis/backlog.json"))

    assert len(invocations) == 1
    assert sensor._fs_events_handled == 1
    assert sensor._fs_events_ignored == 0


@pytest.mark.asyncio
async def test_on_fs_event_non_matching_path_ignored(
    monkeypatch: Any,
) -> None:
    """Files other than backlog.json must bump ignored counter, not scan."""
    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()

    async def _never_scan() -> list:
        raise AssertionError("scan_once must not fire for non-backlog paths")

    sensor.scan_once = _never_scan  # type: ignore[assignment]

    await sensor._on_fs_event(_fs_event("backend/foo.py"))
    await sensor._on_fs_event(_fs_event("README.md"))
    await sensor._on_fs_event(_fs_event(".jarvis/semantic_index.npz"))

    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 3


@pytest.mark.asyncio
async def test_on_fs_event_malformed_payload_is_ignored(
    monkeypatch: Any,
) -> None:
    """An event with no payload attribute must not crash — counted as ignored."""
    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()

    async def _never_scan() -> list:
        raise AssertionError("scan_once must not fire for malformed events")

    sensor.scan_once = _never_scan  # type: ignore[assignment]

    # Simulate an event object with no payload attribute at all.
    class _BrokenEvent:
        topic = "fs.changed.modified"

    await sensor._on_fs_event(_BrokenEvent())

    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 1


# ---------------------------------------------------------------------------
# Telemetry — origin logging (fs_event vs fallback_poll)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_fs_event_logs_fs_event_origin(
    monkeypatch: Any, caplog: Any,
) -> None:
    """The scan-trigger log line distinguishes FS events from polls."""
    import logging as _logging

    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()

    async def _fake_scan() -> list:
        return []

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    caplog.set_level(_logging.INFO, logger=bm.logger.name)
    await sensor._on_fs_event(_fs_event(".jarvis/backlog.json"))

    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "trigger=fs_event" in m and "backlog.json" in m for m in messages
    ), f"expected fs_event origin log line; got {messages!r}"


# ---------------------------------------------------------------------------
# Interval gate
# ---------------------------------------------------------------------------

def test_poll_interval_legacy_when_flag_off(monkeypatch: Any) -> None:
    """Flag off preserves the constructor-provided 60s poll interval."""
    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "false")
    sensor = BacklogSensor(
        backlog_path=Path(".jarvis/backlog.json"),
        repo_root=Path("."),
        router=_SpyRouter(),
        poll_interval_s=60.0,
    )
    assert sensor._fs_events_mode is False
    assert sensor._poll_interval_s == 60.0


def test_init_fs_events_mode_enables_fallback(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_BACKLOG_FS_EVENTS_ENABLED", "true")
    sensor = BacklogSensor(
        backlog_path=Path(".jarvis/backlog.json"),
        repo_root=Path("."),
        router=_SpyRouter(),
        poll_interval_s=60.0,
    )
    assert sensor._fs_events_mode is True
    # The module-level fallback constant takes over at runtime in the loop.
    assert bm._BACKLOG_FALLBACK_INTERVAL_S > 0.0
