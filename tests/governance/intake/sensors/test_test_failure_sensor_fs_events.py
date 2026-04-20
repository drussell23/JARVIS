"""Phase B Slice 3 — TestFailureSensor FS-event migration (gap #4).

Pins the contract introduced by JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED:
  * Flag off (default): subscribe_to_bus is a logged no-op, poll uses
    TestWatcher.poll_interval_s (legacy behavior preserved exactly).
  * Flag on: subscribe_to_bus registers an fs.changed.* handler on the
    TrinityEventBus, poll demotes to JARVIS_TEST_FAILURE_FALLBACK_INTERVAL_S
    (default 600s), FS events drive the hot path.
  * Event routing (flag on): .jarvis/test_results.json → structured
    consumption; *.py change → debounced subprocess run.
  * Failure invariants: subscription errors don't crash intake; handler
    counters advance in both directions for observability.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from backend.core.ouroboros.governance.intake.sensors import test_failure_sensor as tfm
from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _SpyRouter:
    def __init__(self) -> None:
        self.envelopes: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.envelopes.append(envelope)
        return "enqueued"


class _FakeBus:
    """Records subscribe calls + exposes a ``deliver`` for driving handlers."""

    def __init__(self, fail_subscribe: bool = False) -> None:
        self.subscriptions: List[tuple] = []
        self._fail = fail_subscribe

    async def subscribe(self, pattern: str, handler: Any) -> str:
        if self._fail:
            raise RuntimeError("simulated bus failure")
        self.subscriptions.append((pattern, handler))
        return "fake-sub-id"


class _StubWatcher:
    """Minimal TestWatcher surface: poll_once + poll_interval_s + stop."""

    def __init__(self, poll_interval_s: float = 30.0) -> None:
        self.poll_interval_s = poll_interval_s
        self.poll_calls = 0
        self.stopped = False

    async def poll_once(self) -> list:
        self.poll_calls += 1
        return []

    def stop(self) -> None:
        self.stopped = True


def _sensor(watcher: Optional[_StubWatcher] = None) -> TestFailureSensor:
    return TestFailureSensor(
        repo="jarvis",
        router=_SpyRouter(),
        test_watcher=watcher or _StubWatcher(),
    )


def _fs_event(relative_path: str, extension: str = ".py") -> Any:
    """Build an FS event in the payload shape FileSystemEventBridge emits."""
    return SimpleNamespace(
        payload={
            "relative_path": relative_path,
            "extension": extension,
            "path": f"/abs/{relative_path}",
        },
    )


# ---------------------------------------------------------------------------
# Flag helper
# ---------------------------------------------------------------------------

def test_fs_events_enabled_reads_env_fresh(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    assert tfm.fs_events_enabled() is True

    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "false")
    assert tfm.fs_events_enabled() is False

    # Graduated 2026-04-20 — default is now "true" (FS-events primary).
    monkeypatch.delenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", raising=False)
    assert tfm.fs_events_enabled() is True


# ---------------------------------------------------------------------------
# subscribe_to_bus — gated on flag
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_to_bus_noop_when_flag_off(monkeypatch: Any) -> None:
    """Explicit flag=off: subscribe_to_bus returns without touching bus.

    Graduated 2026-04-20 — default is now "true". This test pins the
    opt-out path: operators who explicitly set the flag to "false" must
    still get pure-poll behavior.
    """
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "false")
    sensor = _sensor()
    bus = _FakeBus()

    await sensor.subscribe_to_bus(bus)

    assert bus.subscriptions == [], (
        "flag off must NOT register a subscription (preserves legacy pure-poll)"
    )


@pytest.mark.asyncio
async def test_subscribe_to_bus_registers_when_flag_on(monkeypatch: Any) -> None:
    """Flag on: subscribe_to_bus registers fs.changed.* handler."""
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    sensor = _sensor()
    # _fs_events_mode is captured at __init__ — rebuild after env flip
    sensor = TestFailureSensor(repo="jarvis", router=_SpyRouter(), test_watcher=_StubWatcher())
    bus = _FakeBus()

    await sensor.subscribe_to_bus(bus)

    assert len(bus.subscriptions) == 1
    pattern, handler = bus.subscriptions[0]
    assert pattern == "fs.changed.*"
    assert handler == sensor._on_fs_event


@pytest.mark.asyncio
async def test_subscribe_to_bus_failure_is_non_fatal(monkeypatch: Any) -> None:
    """Bus.subscribe raising must not propagate (intake boot stays green)."""
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    sensor = TestFailureSensor(repo="jarvis", router=_SpyRouter(), test_watcher=_StubWatcher())
    bus = _FakeBus(fail_subscribe=True)

    # Must not raise
    await sensor.subscribe_to_bus(bus)


# ---------------------------------------------------------------------------
# _on_fs_event — routing + counters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_fs_event_test_results_routes_to_consumer(
    monkeypatch: Any, tmp_path: Any,
) -> None:
    """.jarvis/test_results.json change → structured consumption path."""
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    sensor = TestFailureSensor(repo="jarvis", router=_SpyRouter(), test_watcher=_StubWatcher())
    await sensor.subscribe_to_bus(_FakeBus())  # initializes _last_plugin_ts + _debounce_task

    consumed: List[Any] = []

    async def _fake_consume(ev: Any) -> None:
        consumed.append(ev)

    sensor._on_test_results_changed = _fake_consume  # type: ignore[assignment]

    event = _fs_event(".jarvis/test_results.json", extension=".json")
    await sensor._on_fs_event(event)

    assert len(consumed) == 1
    assert sensor._fs_events_handled == 1
    assert sensor._fs_events_ignored == 0


@pytest.mark.asyncio
async def test_on_fs_event_py_file_schedules_debounced_run(monkeypatch: Any) -> None:
    """.py change -> debounced pytest task scheduled."""
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    sensor = TestFailureSensor(repo="jarvis", router=_SpyRouter(), test_watcher=_StubWatcher())
    await sensor.subscribe_to_bus(_FakeBus())

    sensor._debounced_pytest_run = _never_called  # type: ignore[assignment]

    event = _fs_event("backend/foo.py", extension=".py")
    await sensor._on_fs_event(event)

    assert sensor._debounce_task is not None
    # Cancel so the no-op coroutine doesn't linger in the test harness
    sensor._debounce_task.cancel()
    assert sensor._fs_events_handled == 1


async def _never_called() -> None:
    """Stand-in coroutine for _debounced_pytest_run; never actually runs
    because the test cancels it before the 2s debounce completes."""
    await asyncio.sleep(10.0)


@pytest.mark.asyncio
async def test_on_fs_event_irrelevant_file_increments_ignored(
    monkeypatch: Any,
) -> None:
    """Non-.py / non-results-file events bump the 'ignored' counter."""
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    sensor = TestFailureSensor(repo="jarvis", router=_SpyRouter(), test_watcher=_StubWatcher())
    await sensor.subscribe_to_bus(_FakeBus())

    event = _fs_event("README.md", extension=".md")
    await sensor._on_fs_event(event)

    assert sensor._fs_events_handled == 0
    assert sensor._fs_events_ignored == 1


# ---------------------------------------------------------------------------
# Poll interval demotion
# ---------------------------------------------------------------------------

def test_init_captures_fs_events_mode_from_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    sensor = TestFailureSensor(repo="jarvis", router=_SpyRouter(), test_watcher=_StubWatcher())
    assert sensor._fs_events_mode is True

    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "false")
    sensor = TestFailureSensor(repo="jarvis", router=_SpyRouter(), test_watcher=_StubWatcher())
    assert sensor._fs_events_mode is False


@pytest.mark.asyncio
async def test_poll_loop_uses_fallback_interval_when_flag_on(
    monkeypatch: Any,
) -> None:
    """Flag on -> poll interval = _TEST_FAILURE_FALLBACK_INTERVAL_S (600s default).

    Verifies by capturing the exact value passed to asyncio.sleep from
    inside the loop. Uses a captured-sleep monkeypatch to avoid waiting
    10 minutes in a unit test.
    """
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    monkeypatch.setattr(tfm, "_TEST_FAILURE_FALLBACK_INTERVAL_S", 600.0)

    sensor = TestFailureSensor(
        repo="jarvis", router=_SpyRouter(),
        test_watcher=_StubWatcher(poll_interval_s=30.0),
    )

    captured: List[float] = []

    async def _capture_sleep(delay: float) -> None:
        captured.append(delay)
        # After the first capture, shut down the loop so the test exits.
        sensor._running = False
        raise asyncio.CancelledError()

    monkeypatch.setattr(tfm.asyncio, "sleep", _capture_sleep)
    sensor._running = True
    try:
        await sensor._poll_loop()
    except asyncio.CancelledError:
        pass

    assert captured == [600.0], (
        f"flag on must demote poll to 600s, got {captured!r}"
    )


@pytest.mark.asyncio
async def test_poll_loop_uses_watcher_interval_when_flag_off(
    monkeypatch: Any,
) -> None:
    """Explicit flag=off -> poll interval = TestWatcher.poll_interval_s.

    Graduated 2026-04-20 — test must explicitly set flag off.
    """
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "false")

    sensor = TestFailureSensor(
        repo="jarvis", router=_SpyRouter(),
        test_watcher=_StubWatcher(poll_interval_s=30.0),
    )

    captured: List[float] = []

    async def _capture_sleep(delay: float) -> None:
        captured.append(delay)
        sensor._running = False
        raise asyncio.CancelledError()

    monkeypatch.setattr(tfm.asyncio, "sleep", _capture_sleep)
    sensor._running = True
    try:
        await sensor._poll_loop()
    except asyncio.CancelledError:
        pass

    assert captured == [30.0], (
        f"flag off must keep watcher's 30s interval, got {captured!r}"
    )
