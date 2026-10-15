"""A monitor loop whose tool is absent ends; it does not wedge the loop forever.

2026-10-14 20:57 stall dumps: the window poller of backend/macos_helper ran
on Windows and spawned `osascript` -- which does not exist there -- on every
poll: a PATH search and a failed CreateProcess on the event loop each time
(0.5 s stalls), the failure logged at DEBUG and retried forever.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from backend.macos_helper import system_event_monitor as sem


def _monitor(monkeypatch, present=()):
    monitor = sem.SystemEventMonitor()
    lookups = []

    def which(name):
        lookups.append(name)
        return f"/usr/bin/{name}" if name in present else None
    monkeypatch.setattr(sem.shutil, "which", which)
    return monitor, lookups


def test_a_polling_loop_without_its_tool_ends_once_with_one_line(monkeypatch, caplog):
    monitor, lookups = _monitor(monkeypatch)
    monitor._running = True
    spawned = []
    monkeypatch.setattr(sem.asyncio, "create_subprocess_exec",
                        lambda *a, **k: spawned.append(a))
    monitor.config.window_poll_interval_ms = 1

    async def go():
        task = monitor._create_monitoring_loop(monitor._window_monitoring_loop(), "window_monitor")
        await asyncio.wait_for(task, 2)
        return task

    with caplog.at_level(logging.INFO, logger=sem.logger.name):
        task = asyncio.run(go())
    assert task.done() and spawned == [] and lookups == ["osascript"]
    said = [r.getMessage() for r in caplog.records if "not available on this host" in r.getMessage()]
    assert said == ["[SystemEventMonitor] window_monitor stopped: `osascript` is not available on this host"]


def test_a_one_off_probe_without_its_tool_only_gets_the_error(monkeypatch):
    monitor, _ = _monitor(monkeypatch)

    async def probe():
        with pytest.raises(sem.ToolUnavailable):
            await monitor._run_subprocess("yabai", "-v")
        return asyncio.current_task().cancelled()
    assert asyncio.run(probe()) is False                 # the warmup task carries on


def test_a_tool_is_looked_up_once(monkeypatch):
    monitor, lookups = _monitor(monkeypatch, present=("ioreg",))

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def spawn(*a, **k):
        return _Proc()
    monkeypatch.setattr(sem.asyncio, "create_subprocess_exec", spawn)

    async def go():
        for _ in range(3):
            await monitor._run_subprocess("ioreg", "-c", "IOHIDSystem")
    asyncio.run(go())
    assert lookups == ["ioreg"]
