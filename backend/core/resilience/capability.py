"""
CapabilityUpgrade - Hot-Swapping Between Degraded and Full Modes
================================================================

This module provides a capability upgrade mechanism for managing graceful
degradation and automatic recovery. It enables hot-swapping between degraded
(fallback) and full capability modes with monitoring for regression detection.

Features:
- State machine: DEGRADED -> UPGRADING -> FULL -> MONITORING
- Manual upgrade via try_upgrade() and downgrade()
- Background monitoring for automatic upgrade attempts and regression detection
- Async callbacks for upgrade and downgrade events
- Thread-safe state transitions with asyncio.Lock
- Clean cancellation of monitoring tasks

The capability upgrade pattern helps with:
- Graceful degradation when primary services are unavailable
- Automatic recovery when services become available
- Detecting regressions and reverting to fallback
- Hot-swapping between local/cached and cloud/primary implementations

Example usage:
    from backend.core.resilience.capability import CapabilityUpgrade

    # Define capability functions
    async def check_cloud_available():
        try:
            response = await http_client.get("https://api.example.com/health")
            return response.status_code == 200
        except Exception:
            return False

    async def activate_cloud():
        await cloud_client.connect()

    async def deactivate_cloud():
        await cloud_client.disconnect()

    # Create capability upgrade
    upgrade = CapabilityUpgrade(
        name="cloud_api",
        check_available=check_cloud_available,
        activate=activate_cloud,
        deactivate=deactivate_cloud,
        on_upgrade=lambda: logger.info("Switched to cloud API"),
        on_downgrade=lambda: logger.info("Fell back to local cache"),
    )

    # Manual upgrade attempt
    if await upgrade.try_upgrade():
        print("Using cloud API")
    else:
        print("Using local fallback")

    # Start background monitoring
    await upgrade.start_monitoring(interval=30.0)

    # Later, clean shutdown
    await upgrade.stop_monitoring()
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import (
    Awaitable,
    Callable,
)

from backend.core.resilience.types import CapabilityState


@dataclass
class CapabilityUpgrade:
    """
    Hot-swapping mechanism for degraded and full capability modes.

    This class manages transitions between degraded (fallback) and full
    capability states. It provides manual upgrade/downgrade methods and
    background monitoring for automatic upgrade attempts and regression
    detection.

    State Machine:
        DEGRADED -> UPGRADING: When try_upgrade() is called
        UPGRADING -> FULL: When check_available() and activate() succeed
        UPGRADING -> DEGRADED: When check_available() returns False or activate() fails
        FULL -> MONITORING: During background monitoring after successful upgrade
        MONITORING -> DEGRADED: When regression detected (check_available() returns False)
        FULL/MONITORING -> DEGRADED: When downgrade() is called

    Attributes:
        name: Identifier for this capability (for logging/debugging).
        check_available: Async function that returns True if full capability is available.
        activate: Async function to activate the full capability.
        deactivate: Async function to deactivate the full capability.
        on_upgrade: Optional async callback called when upgrading to full mode.
        on_downgrade: Optional async callback called when downgrading to degraded mode.

    Example:
        # Basic usage
        upgrade = CapabilityUpgrade(
            name="database",
            check_available=lambda: db.ping(),
            activate=lambda: db.connect(),
            deactivate=lambda: db.disconnect(),
        )

        # With callbacks
        upgrade = CapabilityUpgrade(
            name="cloud_service",
            check_available=check_cloud,
            activate=connect_cloud,
            deactivate=disconnect_cloud,
            on_upgrade=lambda: notify("Cloud restored"),
            on_downgrade=lambda: notify("Using fallback"),
        )
    """

    name: str
    check_available: Callable[[], Awaitable[bool]]
    activate: Callable[[], Awaitable[None]]
    deactivate: Callable[[], Awaitable[None]]
    on_upgrade: Callable[[], Awaitable[None]] | None = None
    on_downgrade: Callable[[], Awaitable[None]] | None = None

    # Internal state (not exposed as constructor params)
    _state: CapabilityState = field(default=CapabilityState.DEGRADED, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _monitoring_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    @property
    def state(self) -> CapabilityState:
        """
        Get the current capability state.

        Returns:
            The current CapabilityState (DEGRADED, UPGRADING, FULL, or MONITORING).
        """
        return self._state

    @property
    def is_full(self) -> bool:
        """
        Check if currently in full capability mode.

        Returns:
            True if state is FULL or MONITORING, False otherwise.
        """
        return self._state in (CapabilityState.FULL, CapabilityState.MONITORING)

    async def try_upgrade(self) -> bool:
        """
        Attempt to upgrade from DEGRADED to FULL mode.

        This method:
        1. Checks if full capability is available via check_available()
        2. If available, calls activate() to enable full capability
        3. Transitions to FULL state and calls on_upgrade callback if successful

        If already in FULL or MONITORING state, returns True without re-activating.
        If check fails or activation fails, remains in DEGRADED state.

        Returns:
            True if upgrade succeeded (or already in FULL mode), False otherwise.

        Example:
            if await upgrade.try_upgrade():
                # Using full capability
                result = await cloud_api.call()
            else:
                # Using fallback
                result = await local_cache.get()
        """
        async with self._lock:
            # Already in full mode - nothing to do
            if self._state in (CapabilityState.FULL, CapabilityState.MONITORING):
                return True

            # Transition to UPGRADING
            self._state = CapabilityState.UPGRADING

            try:
                # Check if full capability is available
                available = await self.check_available()
                if not available:
                    self._state = CapabilityState.DEGRADED
                    return False

                # Activate full capability
                await self.activate()

                # Success - transition to FULL
                self._state = CapabilityState.FULL

                # Call on_upgrade callback (don't let it affect state)
                if self.on_upgrade is not None:
                    try:
                        await self.on_upgrade()
                    except Exception:
                        pass  # Callback errors don't affect upgrade success

                return True

            except Exception:
                # Any exception during upgrade - revert to DEGRADED
                self._state = CapabilityState.DEGRADED
                return False

    async def downgrade(self) -> None:
        """
        Downgrade from FULL/MONITORING to DEGRADED mode.

        This method:
        1. Calls deactivate() to disable the full capability
        2. Transitions to DEGRADED state
        3. Calls on_downgrade callback

        If already in DEGRADED state, this is a no-op.

        Example:
            # Manual downgrade when issues detected
            if error_rate > threshold:
                await upgrade.downgrade()
        """
        async with self._lock:
            # Already degraded - nothing to do
            if self._state == CapabilityState.DEGRADED:
                return

            was_full = self._state in (CapabilityState.FULL, CapabilityState.MONITORING)

            # Transition to DEGRADED
            self._state = CapabilityState.DEGRADED

            # Deactivate full capability (don't let errors affect state)
            try:
                await self.deactivate()
            except Exception:
                pass  # Deactivation errors don't prevent downgrade

            # Call on_downgrade callback if we were in full mode
            if was_full and self.on_downgrade is not None:
                try:
                    await self.on_downgrade()
                except Exception:
                    pass  # Callback errors don't affect downgrade

    async def start_monitoring(self, interval: float) -> None:
        """
        Start background monitoring for upgrade attempts and regression detection.

        When monitoring is active:
        - If in DEGRADED state, periodically attempts upgrade
        - If in FULL/MONITORING state, periodically checks for regression
        - On regression (check_available returns False), automatically downgrades

        Args:
            interval: Seconds between monitoring checks.

        Example:
            # Start monitoring every 30 seconds
            await upgrade.start_monitoring(interval=30.0)

            # ... later, clean shutdown
            await upgrade.stop_monitoring()
        """
        async with self._lock:
            # Already monitoring - nothing to do
            if self._monitoring_task is not None and not self._monitoring_task.done():
                return

            # Reset shutdown event
            self._shutdown_event.clear()

            # Start monitoring task
            self._monitoring_task = asyncio.create_task(
                self._monitoring_loop(interval)
            )

    async def stop_monitoring(self) -> None:
        """
        Stop background monitoring cleanly.

        Signals the monitoring loop to stop and waits for it to complete.
        Safe to call multiple times or if monitoring was never started.

        Example:
            await upgrade.stop_monitoring()
        """
        async with self._lock:
            if self._monitoring_task is None:
                return

            # Signal shutdown
            self._shutdown_event.set()

        # Wait for task to complete (outside lock to avoid deadlock)
        if self._monitoring_task is not None:
            try:
                await asyncio.wait_for(self._monitoring_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._monitoring_task.cancel()
                try:
                    await self._monitoring_task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
            finally:
                self._monitoring_task = None

    async def _monitoring_loop(self, interval: float) -> None:
        """
        Main monitoring loop.

        Periodically checks capability availability and manages state transitions:
        - DEGRADED: Attempts upgrade
        - FULL/MONITORING: Checks for regression, downgrades if detected

        Args:
            interval: Seconds between checks.
        """
        try:
            while not self._shutdown_event.is_set():
                # Perform monitoring check based on current state
                await self._monitoring_check()

                # Wait for next interval (interruptible)
                await self._interruptible_sleep(interval)

        except asyncio.CancelledError:
            # Clean cancellation
            raise

    async def _monitoring_check(self) -> None:
        """
        Perform a single monitoring check.

        If in DEGRADED state, attempts upgrade.
        If in FULL/MONITORING state, checks for regression.
        """
        async with self._lock:
            current_state = self._state

        if current_state == CapabilityState.DEGRADED:
            # Try to upgrade
            await self.try_upgrade()
            # If successful, transition to MONITORING for regression detection
            async with self._lock:
                if self._state == CapabilityState.FULL:
                    self._state = CapabilityState.MONITORING

        elif current_state in (CapabilityState.FULL, CapabilityState.MONITORING):
            # Check for regression
            try:
                available = await self.check_available()
            except Exception:
                available = False

            if not available:
                # Regression detected - downgrade
                await self.downgrade()

    async def _interruptible_sleep(self, duration: float) -> None:
        """
        Sleep for duration seconds, interruptible by shutdown event.

        Uses a polling approach with short sleeps to allow checking
        for the shutdown event. This works well with mocked asyncio.sleep
        in tests.

        Args:
            duration: Maximum seconds to sleep.
        """
        interval = min(0.1, duration / 10) if duration > 0 else 0.01
        elapsed = 0.0

        while elapsed < duration:
            if self._shutdown_event.is_set():
                return

            sleep_time = min(interval, duration - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            elapsed += sleep_time


__all__ = [
    "CapabilityUpgrade",
]
