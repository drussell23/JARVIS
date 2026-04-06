"""Tests for IdleWatchdog — poke-based idle detection for the Battle Test Runner.

Covers:
- test_fires_after_timeout: event fires after timeout with no poke
- test_poke_resets_timer: poking mid-window resets the countdown
- test_stop_cancels: stopping cancels the task without firing the event
- test_poke_count: poke() increments poke_count correctly
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.battle_test.idle_watchdog import IdleWatchdog


class TestIdleWatchdog:
    @pytest.mark.asyncio
    async def test_fires_after_timeout(self):
        """Event is set after the timeout elapses with no poke."""
        watchdog = IdleWatchdog(timeout_s=0.2)
        await watchdog.start()
        try:
            await asyncio.sleep(0.4)
            assert watchdog.idle_event.is_set(), "idle_event should be set after timeout"
        finally:
            watchdog.stop()

    @pytest.mark.asyncio
    async def test_poke_resets_timer(self):
        """Poking at ~0.15s delays firing until ~0.45s total, not at 0.3s."""
        watchdog = IdleWatchdog(timeout_s=0.3)
        await watchdog.start()
        try:
            # Poke at half-way — resets the 0.3s countdown from this point
            await asyncio.sleep(0.15)
            watchdog.poke()
            # At the original 0.3s mark the event must NOT yet be set
            await asyncio.sleep(0.15)
            assert not watchdog.idle_event.is_set(), (
                "idle_event must not fire at the original deadline after a poke"
            )
            # Wait past the new deadline (0.15 poke + 0.3 timeout = 0.45 from start)
            await asyncio.sleep(0.25)
            assert watchdog.idle_event.is_set(), (
                "idle_event must fire after the reset timeout completes"
            )
        finally:
            watchdog.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels(self):
        """stop() cancels the background task; event is never set."""
        watchdog = IdleWatchdog(timeout_s=0.2)
        await watchdog.start()
        watchdog.stop()
        # Sleep well past the timeout — event must remain unset
        await asyncio.sleep(0.4)
        assert not watchdog.idle_event.is_set(), (
            "idle_event must not fire after stop() is called"
        )

    @pytest.mark.asyncio
    async def test_poke_count(self):
        """poke() increments poke_count for each call."""
        watchdog = IdleWatchdog(timeout_s=60.0)
        await watchdog.start()
        try:
            assert watchdog.poke_count == 0
            watchdog.poke()
            watchdog.poke()
            watchdog.poke()
            assert watchdog.poke_count == 3
        finally:
            watchdog.stop()
