"""
BackgroundRecovery - Adaptive Background Recovery with Safety Valves
====================================================================

This module provides a background recovery mechanism that continuously
attempts to recover a failed component with exponential backoff, safety
valves, and adaptive speedup when conditions change.

Features:
- Exponential backoff with jitter to prevent thundering herd
- Per-attempt timeout to prevent hanging recovery
- Safety valves: max_attempts and max_total_time to prevent infinite loops
- Adaptive speedup: notify_conditions_changed() reduces delay when conditions improve
- Async callbacks for success and pause events
- Clean cancellation via shutdown event
- Thread-safe state management

The recovery pattern helps with:
- Automatically recovering failed services/connections
- Intelligent backoff to reduce load during outages
- Safety limits to prevent runaway retry loops
- Responding quickly when conditions change (e.g., network restored)

Example usage:
    from backend.core.resilience.recovery import RecoveryConfig, BackgroundRecovery

    # Basic usage
    async def reconnect_database():
        try:
            await db.connect()
            return True
        except Exception:
            return False

    recovery = BackgroundRecovery(recover_fn=reconnect_database)
    await recovery.start()

    # With callbacks and safety valves
    async def on_db_recovered():
        await notify_services("Database connection restored")

    async def on_recovery_paused():
        await alert_ops_team("Database recovery paused after max attempts")

    recovery = BackgroundRecovery(
        recover_fn=reconnect_database,
        config=RecoveryConfig(
            base_delay=5.0,
            max_delay=300.0,
            max_attempts=10,
            timeout=30.0,
        ),
        on_success=on_db_recovered,
        on_paused=on_recovery_paused,
    )

    # When network conditions change, speed up recovery
    async def on_network_restored():
        recovery.notify_conditions_changed()
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import (
    Awaitable,
    Callable,
)

from backend.core.resilience.types import RecoveryState


@dataclass
class RecoveryConfig:
    """
    Configuration for BackgroundRecovery.

    Controls the backoff behavior, safety valves, and adaptive speedup
    for the recovery process.

    Delay formula: base_delay * exponential_base^(attempt-1), capped at max_delay
    Jitter: random value between delay*(1-jitter) and delay*(1+jitter)

    Attributes:
        base_delay: Initial delay between recovery attempts in seconds. Default is 5.0.
        max_delay: Maximum delay cap in seconds. Default is 300.0 (5 minutes).
        exponential_base: Multiplier for exponential backoff. Default is 2.0.
        jitter: Random +/- percentage (0.0 to 1.0). Default is 0.1 (10%).
        timeout: Per-attempt timeout in seconds. Default is 30.0.
        max_attempts: Maximum attempts before pausing. None means unlimited. Default is None.
        max_total_time: Maximum total recovery time before pausing. None means unlimited.
        speedup_factor: Fraction to reduce delay when conditions change. Default is 0.25.

    Example:
        # Default configuration
        config = RecoveryConfig()

        # Aggressive recovery with short delays and limits
        config = RecoveryConfig(
            base_delay=1.0,
            max_delay=60.0,
            max_attempts=20,
            timeout=10.0,
        )

        # Conservative recovery with long delays
        config = RecoveryConfig(
            base_delay=30.0,
            max_delay=600.0,
            max_total_time=3600.0,  # 1 hour max
        )
    """

    base_delay: float = 5.0
    max_delay: float = 300.0
    exponential_base: float = 2.0
    jitter: float = 0.1
    timeout: float = 30.0
    max_attempts: int | None = None  # Safety valve: None = unlimited
    max_total_time: float | None = None  # Safety valve: None = unlimited
    speedup_factor: float = 0.25  # Reduce delay to this fraction when conditions change


@dataclass
class BackgroundRecovery:
    """
    Background recovery mechanism with adaptive backoff and safety valves.

    This class provides a continuous recovery loop that attempts to recover
    a failed component. It uses exponential backoff with jitter to prevent
    overwhelming the target service, and provides safety valves to pause
    after too many attempts or too much time.

    The recovery loop can be sped up when conditions change (e.g., network
    restored) by calling notify_conditions_changed(), which wakes up the
    loop early and reduces the next delay.

    State Machine:
        IDLE -> RECOVERING: When start() is called
        RECOVERING -> SUCCEEDED: When recover_fn returns True
        RECOVERING -> PAUSED: When safety valve triggers (max_attempts/max_total_time)
        RECOVERING -> IDLE: When stop() is called
        PAUSED -> RECOVERING: When resume() is called
        PAUSED -> IDLE: When stop() is called

    Attributes:
        recover_fn: Async function that returns True if recovery succeeded, False otherwise.
        config: RecoveryConfig with backoff and safety valve settings.
        on_success: Optional async callback called when recovery succeeds.
        on_paused: Optional async callback called when recovery pauses due to safety valve.

    Example:
        # Create recovery for database connection
        async def reconnect():
            try:
                await db.reconnect()
                return True
            except Exception:
                return False

        recovery = BackgroundRecovery(
            recover_fn=reconnect,
            config=RecoveryConfig(max_attempts=10),
            on_success=lambda: notify("DB recovered"),
            on_paused=lambda: alert("DB recovery paused"),
        )

        # Start recovery
        await recovery.start()

        # Later, when network conditions improve
        recovery.notify_conditions_changed()

        # To stop recovery
        await recovery.stop()
    """

    recover_fn: Callable[[], Awaitable[bool]]
    config: RecoveryConfig = field(default_factory=RecoveryConfig)
    on_success: Callable[[], Awaitable[None]] | None = None
    on_paused: Callable[[], Awaitable[None]] | None = None

    # Internal state (not exposed as constructor params)
    _state: RecoveryState = field(default=RecoveryState.IDLE, init=False, repr=False)
    _task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _conditions_changed: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _attempt_count: int = field(default=0, init=False, repr=False)
    _start_time: float = field(default=0.0, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def state(self) -> RecoveryState:
        """
        Get the current recovery state.

        Returns:
            The current RecoveryState (IDLE, RECOVERING, PAUSED, or SUCCEEDED).
        """
        return self._state

    @property
    def attempt_count(self) -> int:
        """
        Get the current attempt count.

        Returns:
            The number of recovery attempts made since start or last resume.
        """
        return self._attempt_count

    def notify_conditions_changed(self) -> None:
        """
        Notify that conditions have changed and recovery should speed up.

        This wakes up the recovery loop early (if waiting) and reduces
        the next delay by speedup_factor. Use this when external conditions
        improve, such as network connectivity being restored.

        This method is thread-safe and can be called from any context.

        Example:
            # In a network change handler
            def on_network_up():
                recovery.notify_conditions_changed()
        """
        self._conditions_changed.set()

    async def start(self) -> None:
        """
        Start the background recovery loop.

        Creates a background task that continuously attempts recovery until
        success, pause due to safety valve, or stop() is called.

        If already started (RECOVERING state), this method is a no-op.

        Example:
            await recovery.start()
            # Recovery is now running in the background
        """
        async with self._lock:
            if self._state == RecoveryState.RECOVERING:
                # Already running
                return

            # Reset state
            self._state = RecoveryState.RECOVERING
            self._attempt_count = 0
            self._start_time = time.monotonic()
            self._shutdown_event.clear()
            self._conditions_changed.clear()

            # Start the recovery loop task
            self._task = asyncio.create_task(self._recovery_loop())

    async def stop(self) -> None:
        """
        Stop the background recovery loop.

        Signals the recovery loop to stop and waits for it to complete.
        Sets state to IDLE after stopping.

        If already stopped (IDLE state), this method is a no-op.

        Example:
            await recovery.stop()
            assert recovery.state == RecoveryState.IDLE
        """
        async with self._lock:
            if self._task is None:
                # Already stopped
                self._state = RecoveryState.IDLE
                return

            # Signal shutdown
            self._shutdown_event.set()
            self._conditions_changed.set()  # Wake up if waiting

        # Wait for task to complete (outside lock to avoid deadlock)
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None
                self._state = RecoveryState.IDLE

    async def resume(self) -> None:
        """
        Resume recovery after being paused.

        Resets the attempt count and starts a new recovery loop.
        Only works if currently in PAUSED state.

        Example:
            if recovery.state == RecoveryState.PAUSED:
                await recovery.resume()
        """
        async with self._lock:
            if self._state != RecoveryState.PAUSED:
                # Can only resume from PAUSED
                if self._state == RecoveryState.IDLE:
                    # If IDLE, just start
                    pass
                else:
                    return

            # Reset for new recovery cycle
            self._state = RecoveryState.RECOVERING
            self._attempt_count = 0
            self._start_time = time.monotonic()
            self._shutdown_event.clear()
            self._conditions_changed.clear()

            # Start new recovery loop
            self._task = asyncio.create_task(self._recovery_loop())

    def _calculate_delay(self) -> float:
        """
        Calculate the delay before the next recovery attempt.

        Uses exponential backoff: base_delay * exponential_base^(attempt-1)
        with the result capped at max_delay. Jitter is then applied as a
        random multiplier.

        If conditions_changed is set, applies speedup_factor to reduce delay.

        Returns:
            The delay in seconds to wait before the next attempt.
        """
        # Calculate base exponential delay
        exponent = max(0, self._attempt_count - 1)
        delay = self.config.base_delay * (self.config.exponential_base ** exponent)

        # Cap at max_delay before applying jitter
        delay = min(delay, self.config.max_delay)

        # Apply speedup if conditions changed
        if self._conditions_changed.is_set():
            delay *= self.config.speedup_factor

        # Apply jitter as a random multiplier
        if self.config.jitter > 0:
            jitter_range = delay * self.config.jitter
            delay = delay + random.uniform(-jitter_range, jitter_range)
            # Ensure we don't go negative
            delay = max(0.0, delay)

        return delay

    def _check_safety_valve(self) -> bool:
        """
        Check if safety valve conditions are met.

        Returns:
            True if recovery should pause (safety valve triggered), False otherwise.
        """
        # Check max_attempts
        if self.config.max_attempts is not None:
            if self._attempt_count >= self.config.max_attempts:
                return True

        # Check max_total_time
        if self.config.max_total_time is not None:
            elapsed = time.monotonic() - self._start_time
            if elapsed >= self.config.max_total_time:
                return True

        return False

    async def _recovery_loop(self) -> None:
        """
        Main recovery loop.

        Continuously attempts recovery until:
        - recover_fn returns True (success)
        - Safety valve triggers (max_attempts or max_total_time)
        - Shutdown event is set (stop() called)

        On success, calls on_success callback and sets state to SUCCEEDED.
        On safety valve, calls on_paused callback and sets state to PAUSED.
        On shutdown, sets state to IDLE.
        """
        try:
            while not self._shutdown_event.is_set():
                # Attempt recovery
                self._attempt_count += 1

                try:
                    # Call recover_fn with timeout
                    result = await asyncio.wait_for(
                        self.recover_fn(),
                        timeout=self.config.timeout,
                    )
                except asyncio.TimeoutError:
                    result = False
                except asyncio.CancelledError:
                    # Propagate cancellation
                    raise
                except Exception:
                    # Any exception is treated as failure
                    result = False

                if result:
                    # Success!
                    self._state = RecoveryState.SUCCEEDED
                    if self.on_success is not None:
                        try:
                            await self.on_success()
                        except Exception:
                            pass  # Don't let callback errors affect state
                    return

                # Check safety valve
                if self._check_safety_valve():
                    self._state = RecoveryState.PAUSED
                    if self.on_paused is not None:
                        try:
                            await self.on_paused()
                        except Exception:
                            pass  # Don't let callback errors affect state
                    return

                # Calculate delay before next attempt
                delay = self._calculate_delay()

                # Clear conditions_changed for next iteration
                self._conditions_changed.clear()

                # Wait with interruptible sleep
                if delay > 0:
                    await self._interruptible_sleep(delay)

        except asyncio.CancelledError:
            # Clean cancellation
            self._state = RecoveryState.IDLE
            raise
        finally:
            # Ensure state is set on any exit
            if self._state == RecoveryState.RECOVERING:
                self._state = RecoveryState.IDLE

    async def _interruptible_sleep(self, delay: float) -> None:
        """
        Sleep for delay seconds, but can be interrupted by shutdown or conditions_changed events.

        Uses a simple loop with short sleeps to allow checking for interrupts.
        This approach works well with mocked asyncio.sleep in tests.

        Args:
            delay: Maximum seconds to sleep.
        """
        # Use shorter sleep intervals for better responsiveness
        interval = min(0.1, delay / 10) if delay > 0 else 0.01
        start = time.monotonic()

        while True:
            # Check for shutdown or conditions changed
            if self._shutdown_event.is_set() or self._conditions_changed.is_set():
                return

            # Check if we've waited long enough
            elapsed = time.monotonic() - start
            if elapsed >= delay:
                return

            # Sleep for a short interval
            remaining = delay - elapsed
            sleep_time = min(interval, remaining)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)


__all__ = [
    "RecoveryConfig",
    "BackgroundRecovery",
]
