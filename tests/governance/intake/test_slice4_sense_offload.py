from __future__ import annotations

import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_resolve_scoped_targets_does_not_block_loop(tmp_path, monkeypatch):
    """The Run #14 tombstone: main thread wedged in pathlib.resolve inside
    _resolve_scoped_targets. Prove the loop keeps ticking (<250ms gaps)
    while resolution runs against a slow filesystem walk."""
    from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
        TestFailureSensor,
    )
    sensor = TestFailureSensor.__new__(TestFailureSensor)
    monkeypatch.setattr(sensor, "_repo_root", lambda: tmp_path, raising=False)

    real_resolve = __import__("pathlib").Path.resolve

    def slow_resolve(self, *a, **k):
        time.sleep(0.4)  # simulated deep _joinrealpath walk
        return real_resolve(self, *a, **k)

    monkeypatch.setattr("pathlib.Path.resolve", slow_resolve)

    gaps: list[float] = []

    async def ticker():
        prev = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - prev)
            prev = now

    t = asyncio.ensure_future(ticker())
    try:
        await sensor._resolve_scoped_targets("backend/some/module.py")
    finally:
        t.cancel()
    assert gaps, "ticker never ran"
    assert max(gaps) < 0.25, f"loop starved: max gap {max(gaps):.3f}s"


@pytest.mark.asyncio
async def test_resolver_failure_degrades_to_none(monkeypatch, tmp_path):
    """Offload failure -> same neutral value the sync path returns (None),
    never an exception (Global Constraint: fail-soft parity)."""
    from backend.core.ouroboros.governance.intake.sensors import test_failure_sensor as tfs
    sensor = tfs.TestFailureSensor.__new__(tfs.TestFailureSensor)
    monkeypatch.setattr(sensor, "_repo_root", lambda: tmp_path, raising=False)

    async def broken_offload(fn, *a, **k):
        raise RuntimeError("executor down")

    monkeypatch.setattr(tfs, "_offload_fs", broken_offload, raising=False)
    assert await sensor._resolve_scoped_targets("backend/x.py") is None


def test_watcher_derates_when_event_lane_armed(monkeypatch):
    """Gap #4 Strangler Fig: with the event-primary TestFailure lane armed,
    the legacy poller must not run whole-suite sweeps.

    NOTE: the brief's draft assumed the env var name
    ``JARVIS_TESTFAILURE_FS_EVENTS_ENABLED``. The real name (verified via
    ``grep -n fs_events_enabled backend/.../test_failure_sensor.py``) is
    ``JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED`` — used here and everywhere
    else in this module.
    """
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    monkeypatch.delenv("JARVIS_INTENT_POLL_WHEN_EVENT_PRIMARY", raising=False)
    w = TestWatcher.__new__(TestWatcher)
    assert w._event_primary_derate() is True


def test_watcher_polls_when_event_lane_off(monkeypatch):
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "false")
    w = TestWatcher.__new__(TestWatcher)
    assert w._event_primary_derate() is False


def test_watcher_escape_hatch(monkeypatch):
    """Operators can force legacy polling even under event-primary."""
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_INTENT_POLL_WHEN_EVENT_PRIMARY", "true")
    w = TestWatcher.__new__(TestWatcher)
    assert w._event_primary_derate() is False
