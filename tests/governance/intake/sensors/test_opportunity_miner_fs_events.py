"""Gap #4 — OpportunityMinerSensor FS-event migration regression spine.

Pins the contract introduced by JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED:
  * Flag off (default): fs_events_enabled() returns False; _fs_events_mode
    False; subscribe_to_bus is a logged no-op; poll at legacy interval.
  * Flag on: subscribe_to_bus registers an fs.changed.* handler; .py file
    changes route to scan_file; skip-dir / non-.py / test / deleted events
    bump the ignored counter; poll demotes to fallback (6h default).
  * Storm-guard: burst above threshold → storm mode; all subsequent events
    counted as storm_dropped until cooldown elapses.
  * Per-file debounce: repeat event on same path within debounce window
    increments debounced counter without invoking scan_file.
  * Subscription failures are caught locally (intake boot never regresses).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    opportunity_miner_sensor as om,
)
from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
    OpportunityMinerSensor,
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


def _sensor(tmp_path: Path | None = None) -> OpportunityMinerSensor:
    return OpportunityMinerSensor(
        repo_root=tmp_path or Path("."),
        router=_SpyRouter(),
        scan_paths=["."],
        repo="jarvis",
        poll_interval_s=3600.0,
    )


def _fs_event(
    relative_path: str,
    extension: str = ".py",
    topic: str = "fs.changed.modified",
    is_test_file: bool = False,
    path_override: str | None = None,
) -> Any:
    """FS event in the shape FileSystemEventBridge emits."""
    return SimpleNamespace(
        topic=topic,
        payload={
            "relative_path": relative_path,
            "extension": extension,
            "is_test_file": is_test_file,
            "path": path_override or f"/abs/{relative_path}",
        },
    )


async def _never_scan(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("scan_file must not be called")


# ---------------------------------------------------------------------------
# Flag helper + init
# ---------------------------------------------------------------------------

def test_fs_events_enabled_reads_env_fresh(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
    assert om.fs_events_enabled() is True

    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "false")
    assert om.fs_events_enabled() is False

    # Shadow default — flag absent = False (pure-poll preserved).
    monkeypatch.delenv(
        "JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", raising=False,
    )
    assert om.fs_events_enabled() is False


def test_init_captures_fs_events_mode(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    assert sensor._fs_events_mode is True
    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 0
    assert sensor._fs_events_debounced == 0
    assert sensor._fs_events_storm_dropped == 0

    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "false")
    sensor = _sensor()
    assert sensor._fs_events_mode is False


# ---------------------------------------------------------------------------
# subscribe_to_bus — flag gated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_to_bus_noop_when_flag_off(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "false")
    sensor = _sensor()
    bus = _FakeBus()

    await sensor.subscribe_to_bus(bus)

    assert bus.subscriptions == [], (
        "flag off must NOT register a subscription"
    )


@pytest.mark.asyncio
async def test_subscribe_to_bus_registers_when_flag_on(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
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
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
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
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path, router=_SpyRouter(),
        scan_paths=["."], repo="jarvis", poll_interval_s=3600.0,
    )

    scanned: List[Path] = []

    async def _fake_scan(p: Path) -> None:
        scanned.append(p)

    sensor.scan_file = _fake_scan  # type: ignore[assignment]

    await sensor._on_fs_event(_fs_event("backend/foo.py"))

    assert len(scanned) == 1
    assert scanned[0] == Path("/abs/backend/foo.py")
    assert sensor._fs_events_handled == 1
    assert sensor._fs_events_ignored == 0
    assert sensor._fs_events_debounced == 0
    assert sensor._fs_events_storm_dropped == 0


@pytest.mark.asyncio
async def test_on_fs_event_non_py_file_ignored(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    sensor.scan_file = _never_scan  # type: ignore[assignment]

    await sensor._on_fs_event(_fs_event("README.md", extension=".md"))

    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 1


@pytest.mark.asyncio
async def test_on_fs_event_test_file_ignored(monkeypatch: Any) -> None:
    """payload.is_test_file=True must bypass the miner entirely."""
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    sensor.scan_file = _never_scan  # type: ignore[assignment]

    await sensor._on_fs_event(_fs_event("tests/test_foo.py", is_test_file=True))

    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 1


@pytest.mark.asyncio
async def test_on_fs_event_deleted_event_cleans_state(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    sensor.scan_file = _never_scan  # type: ignore[assignment]
    # Prime state so deletion is visibly cleaned.
    sensor._seen_file_paths.add("backend/foo.py")
    sensor._cooldown_map["backend/foo.py"] = 42
    sensor._event_debounce["backend/foo.py"] = 99.0

    await sensor._on_fs_event(_fs_event(
        "backend/foo.py", topic="fs.changed.deleted",
    ))

    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 1
    assert "backend/foo.py" not in sensor._seen_file_paths
    assert "backend/foo.py" not in sensor._cooldown_map
    assert "backend/foo.py" not in sensor._event_debounce


@pytest.mark.asyncio
async def test_on_fs_event_skip_dir_ignored(monkeypatch: Any) -> None:
    """Files under venv/__pycache__/.worktrees/etc must not be scanned."""
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    sensor.scan_file = _never_scan  # type: ignore[assignment]

    # /abs/backend/__pycache__/foo.py — __pycache__ is in _OPP_SKIP_DIRS
    await sensor._on_fs_event(_fs_event(
        "backend/__pycache__/foo.py",
        path_override="/abs/backend/__pycache__/foo.py",
    ))

    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 1


@pytest.mark.asyncio
async def test_on_fs_event_missing_path_ignored(monkeypatch: Any) -> None:
    """Payload with no 'path' key must not crash; counted as ignored."""
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    sensor.scan_file = _never_scan  # type: ignore[assignment]

    event = SimpleNamespace(
        topic="fs.changed.modified",
        payload={"relative_path": "backend/foo.py", "extension": ".py"},
    )
    await sensor._on_fs_event(event)

    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 1


# ---------------------------------------------------------------------------
# Debounce (per-file)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_fs_event_debounces_repeat_within_window(
    monkeypatch: Any,
) -> None:
    """Two events on the same path within debounce window → second debounced."""
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()

    call_count = {"n": 0}

    async def _count_scan(_p: Path) -> None:
        call_count["n"] += 1

    sensor.scan_file = _count_scan  # type: ignore[assignment]

    await sensor._on_fs_event(_fs_event("backend/foo.py"))
    await sensor._on_fs_event(_fs_event("backend/foo.py"))

    assert call_count["n"] == 1
    assert sensor._fs_events_handled == 1
    assert sensor._fs_events_debounced == 1


@pytest.mark.asyncio
async def test_on_fs_event_different_files_bypass_debounce(
    monkeypatch: Any,
) -> None:
    """Debounce is per-file, not global — distinct paths each scan."""
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()

    call_count = {"n": 0}

    async def _count_scan(_p: Path) -> None:
        call_count["n"] += 1

    sensor.scan_file = _count_scan  # type: ignore[assignment]

    await sensor._on_fs_event(_fs_event("backend/foo.py"))
    await sensor._on_fs_event(_fs_event("backend/bar.py"))
    await sensor._on_fs_event(_fs_event("backend/baz.py"))

    assert call_count["n"] == 3
    assert sensor._fs_events_handled == 3
    assert sensor._fs_events_debounced == 0


# ---------------------------------------------------------------------------
# Storm guard (global burst circuit breaker)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_fs_event_storm_trips_and_drops(monkeypatch: Any) -> None:
    """Exceeding storm threshold within 1s → storm mode; further events dropped."""
    # Lower threshold so we can trip deterministically without thrashing.
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
    monkeypatch.setattr(om, "_OPP_MINER_STORM_THRESHOLD", 5)
    monkeypatch.setattr(om, "_OPP_MINER_STORM_COOLDOWN_S", 30.0)
    # Also disable debounce so each unique path passes that gate.
    monkeypatch.setattr(om, "_OPP_MINER_DEBOUNCE_S", 0.0)

    sensor = _sensor()

    scanned: List[Path] = []

    async def _fake_scan(p: Path) -> None:
        scanned.append(p)

    sensor.scan_file = _fake_scan  # type: ignore[assignment]

    # Fire 10 events on distinct .py files in rapid succession. Threshold=5
    # means the 6th event trips the circuit; #6 onward are storm_dropped.
    for i in range(10):
        await sensor._on_fs_event(_fs_event(f"backend/mod_{i}.py"))

    assert sensor._fs_events_handled == 5
    assert sensor._fs_events_storm_dropped >= 1
    assert (
        sensor._fs_events_handled + sensor._fs_events_storm_dropped == 10
    )
    assert sensor._storm_active_until > 0.0


# ---------------------------------------------------------------------------
# Interval gate
# ---------------------------------------------------------------------------

def test_poll_interval_legacy_when_flag_off(monkeypatch: Any) -> None:
    """Flag off preserves the constructor-provided poll interval exactly."""
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "false")
    sensor = OpportunityMinerSensor(
        repo_root=Path("."), router=_SpyRouter(),
        scan_paths=["."], repo="jarvis", poll_interval_s=3600.0,
    )
    assert sensor._fs_events_mode is False
    assert sensor._poll_interval_s == 3600.0


def test_init_fs_events_mode_enables_fallback(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_OPPORTUNITY_MINER_FS_EVENTS_ENABLED", "true")
    sensor = OpportunityMinerSensor(
        repo_root=Path("."), router=_SpyRouter(),
        scan_paths=["."], repo="jarvis", poll_interval_s=3600.0,
    )
    assert sensor._fs_events_mode is True
    # The module-level fallback constant takes over at runtime in the loop.
    assert om._OPP_MINER_FALLBACK_INTERVAL_S > 0.0
