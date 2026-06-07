"""IdleWatchdog: timeout_s <= 0 must DISABLE the watchdog (never fire).

The --production-soak profile sets --idle-timeout 0 to mean "no idle stop". Before
this fix, timeout_s=0 made `remaining = 0 - elapsed` immediately negative → the
watchdog fired idle on its first tick, which would kill a 12-month unattended soak
the moment it went quiet. These tests pin the disabled semantics + that a normal
positive timeout still fires.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.battle_test.idle_watchdog import IdleWatchdog


@pytest.mark.asyncio
async def test_zero_timeout_never_fires():
    wd = IdleWatchdog(timeout_s=0)
    await wd.start()
    # Give the watch loop ample chances to (wrongly) fire.
    await asyncio.sleep(0.3)
    assert wd.idle_event.is_set() is False, "timeout_s=0 must DISABLE the watchdog"
    wd.stop()


@pytest.mark.asyncio
async def test_negative_timeout_never_fires():
    wd = IdleWatchdog(timeout_s=-1)
    await wd.start()
    await asyncio.sleep(0.3)
    assert wd.idle_event.is_set() is False
    wd.stop()


@pytest.mark.asyncio
async def test_positive_timeout_still_fires():
    wd = IdleWatchdog(timeout_s=0.1)
    await wd.start()
    await asyncio.sleep(0.5)  # well past the 0.1s window with no poke
    assert wd.idle_event.is_set() is True, "a positive timeout must still fire on idle"
    assert wd.diagnostics is not None and wd.diagnostics.reason == "genuine_idle"
    wd.stop()


@pytest.mark.asyncio
async def test_positive_timeout_poke_prevents_fire():
    wd = IdleWatchdog(timeout_s=0.3)
    await wd.start()
    for _ in range(5):
        await asyncio.sleep(0.1)
        wd.poke()
    assert wd.idle_event.is_set() is False  # constant pokes keep it alive
    wd.stop()
