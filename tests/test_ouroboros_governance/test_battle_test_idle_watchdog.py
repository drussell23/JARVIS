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


# ---------------------------------------------------------------------------
# HIBERNATION_MODE: freeze() / unfreeze() tests
# ---------------------------------------------------------------------------


class TestIdleWatchdogFreeze:
    @pytest.mark.asyncio
    async def test_freeze_prevents_fire_past_timeout(self):
        """While frozen, idle_event must NOT fire even well past timeout_s."""
        watchdog = IdleWatchdog(timeout_s=0.2)
        await watchdog.start()
        try:
            assert watchdog.freeze(reason="test") is True
            assert watchdog.is_frozen is True
            # Sleep far past the timeout.
            await asyncio.sleep(0.6)
            assert not watchdog.idle_event.is_set(), (
                "idle_event must not fire while frozen"
            )
        finally:
            watchdog.stop()

    @pytest.mark.asyncio
    async def test_unfreeze_resets_clock(self):
        """unfreeze() must reset _last_poke so the idle window starts fresh.

        Otherwise a long hibernation would make elapsed > timeout and fire
        immediately on wake.
        """
        watchdog = IdleWatchdog(timeout_s=0.3)
        await watchdog.start()
        try:
            watchdog.freeze()
            await asyncio.sleep(0.5)  # Would-be idle time.
            assert not watchdog.idle_event.is_set()
            watchdog.unfreeze(reason="test")
            # Immediately after unfreeze we should have a full fresh window.
            await asyncio.sleep(0.15)
            assert not watchdog.idle_event.is_set(), (
                "idle_event fired too early — clock not reset on unfreeze"
            )
            # Past the new window, it should fire.
            await asyncio.sleep(0.3)
            assert watchdog.idle_event.is_set()
        finally:
            watchdog.stop()

    @pytest.mark.asyncio
    async def test_freeze_unfreeze_are_idempotent(self):
        """Repeated freeze()/unfreeze() return False on second call."""
        watchdog = IdleWatchdog(timeout_s=60.0)
        await watchdog.start()
        try:
            assert watchdog.freeze() is True
            assert watchdog.freeze() is False  # already frozen
            assert watchdog.is_frozen is True
            assert watchdog.unfreeze() is True
            assert watchdog.unfreeze() is False  # already running
            assert watchdog.is_frozen is False
        finally:
            watchdog.stop()

    @pytest.mark.asyncio
    async def test_fire_stale_suppressed_while_frozen(self):
        """fire_stale() must be a no-op during hibernation — stale ops are expected."""
        watchdog = IdleWatchdog(timeout_s=60.0)
        await watchdog.start()
        try:
            watchdog.freeze()
            watchdog.fire_stale(stale_ops=[{"op_id": "test-1"}])
            assert not watchdog.idle_event.is_set()
            assert watchdog.diagnostics is None
        finally:
            watchdog.stop()

    @pytest.mark.asyncio
    async def test_fire_stale_fires_when_not_frozen(self):
        """Baseline: fire_stale() still works normally when not frozen."""
        watchdog = IdleWatchdog(timeout_s=60.0)
        await watchdog.start()
        try:
            watchdog.fire_stale(stale_ops=[{"op_id": "test-1"}])
            assert watchdog.idle_event.is_set()
            assert watchdog.diagnostics is not None
            assert watchdog.diagnostics.reason == "all_ops_stale"
        finally:
            watchdog.stop()

    @pytest.mark.asyncio
    async def test_freeze_count_tracks_cycles(self):
        """freeze_count increments on each fresh freeze() transition."""
        watchdog = IdleWatchdog(timeout_s=60.0)
        await watchdog.start()
        try:
            assert watchdog.freeze_count == 0
            for _ in range(3):
                watchdog.freeze()
                watchdog.unfreeze()
            assert watchdog.freeze_count == 3
        finally:
            watchdog.stop()

    @pytest.mark.asyncio
    async def test_unfreeze_without_prior_freeze_is_noop(self):
        """unfreeze() on a running watchdog returns False, no state change."""
        watchdog = IdleWatchdog(timeout_s=60.0)
        await watchdog.start()
        try:
            assert watchdog.unfreeze() is False
            assert watchdog.is_frozen is False
        finally:
            watchdog.stop()
