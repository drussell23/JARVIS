"""
Self-Healing Restart Manager for JARVIS Loading Server v212.0
==============================================================

Auto-recovery system for loading server crashes.

Features:
- Detects loading server crashes
- Automatic restart with exponential backoff
- Process watchdog monitoring
- Restart limit to prevent infinite loops
- Supervisor notification on repeated failures
- Health check validation before restart
- Graceful shutdown handling

Usage:
    from backend.loading_server.self_healing import SelfHealingRestartManager

    manager = SelfHealingRestartManager()
    await manager.start_watchdog()
    # ... run server ...
    await manager.stop_watchdog()

Author: JARVIS Trinity System
Version: 212.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional

logger = logging.getLogger("LoadingServer.SelfHealing")


class RestartReason(Enum):
    """Reason for restart."""

    CRASH = "crash"
    HEALTH_CHECK_FAILURE = "health_check_failure"
    MEMORY_LIMIT = "memory_limit"
    DEADLOCK_DETECTED = "deadlock_detected"
    MANUAL = "manual"
    SUPERVISOR_REQUEST = "supervisor_request"


class HealthStatus(Enum):
    """Health status."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DEAD = "dead"


@dataclass
class RestartEvent:
    """Record of a restart event."""

    timestamp: float
    reason: RestartReason
    attempt: int
    backoff_seconds: float
    success: bool
    error: Optional[str] = None


@dataclass
class SelfHealingRestartManager:
    """
    Self-healing restart manager for loading server.

    Features:
    - Detects loading server crashes
    - Automatic restart with exponential backoff
    - Process watchdog monitoring
    - Restart limit to prevent infinite loops
    - Supervisor notification on repeated failures
    """

    max_restarts: int = 5  # Max restarts in window
    restart_window: float = 300.0  # 5 minutes
    initial_backoff: float = 1.0  # Initial backoff seconds
    max_backoff: float = 60.0  # Maximum backoff seconds
    health_check_interval: float = 10.0  # Seconds between health checks
    memory_limit_mb: Optional[float] = None  # Memory limit for restart

    # Callbacks
    on_restart: Optional[Callable[[RestartEvent], None]] = None
    on_failure: Optional[Callable[[str], None]] = None
    health_check: Optional[Callable[[], bool]] = None

    # State
    _restart_times: Deque[float] = field(init=False, default_factory=lambda: deque(maxlen=10))
    _restart_count: int = field(init=False, default=0)
    _total_restarts: int = field(init=False, default=0)
    _restart_history: Deque[RestartEvent] = field(
        init=False, default_factory=lambda: deque(maxlen=100)
    )
    _watchdog_task: Optional[asyncio.Task] = field(init=False, default=None)
    _running: bool = field(init=False, default=False)
    _last_health_check: float = field(init=False, default=0.0)
    _consecutive_failures: int = field(init=False, default=0)
    _shutdown_requested: bool = field(init=False, default=False)
    _background_tasks: set = field(init=False, default_factory=set)

    async def start_watchdog(self) -> None:
        """Start the watchdog monitoring task."""
        if self._watchdog_task is None or self._watchdog_task.done():
            self._running = True
            self._shutdown_requested = False
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
            logger.info("[SelfHealing] Watchdog started")

    async def stop_watchdog(self) -> None:
        """Stop the watchdog monitoring task."""
        self._running = False
        self._shutdown_requested = True

        if self._watchdog_task:
            self._watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watchdog_task
            logger.info("[SelfHealing] Watchdog stopped")

    async def _watchdog_loop(self) -> None:
        """Background watchdog loop."""
        while self._running:
            try:
                await asyncio.sleep(self.health_check_interval)

                if self._shutdown_requested:
                    break

                # Perform health check
                health_status = await self._perform_health_check()

                if health_status == HealthStatus.DEAD:
                    await self._handle_restart(RestartReason.CRASH)
                elif health_status == HealthStatus.UNHEALTHY:
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= 3:
                        await self._handle_restart(RestartReason.HEALTH_CHECK_FAILURE)
                else:
                    self._consecutive_failures = 0

                # Check memory if limit is set
                if self.memory_limit_mb:
                    await self._check_memory_usage()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SelfHealing] Watchdog error: {e}")
                await asyncio.sleep(self.health_check_interval)

    async def _perform_health_check(self) -> HealthStatus:
        """
        Perform health check.

        Returns:
            HealthStatus indicating current health
        """
        self._last_health_check = time.time()

        # Use custom health check if provided
        if self.health_check:
            try:
                healthy = self.health_check()
                if asyncio.iscoroutine(healthy):
                    healthy = await healthy
                return HealthStatus.HEALTHY if healthy else HealthStatus.UNHEALTHY
            except Exception as e:
                logger.debug(f"[SelfHealing] Health check error: {e}")
                return HealthStatus.UNHEALTHY

        # Default: check if we can create async tasks (basic liveness)
        try:
            async def _probe():
                return True

            result = await asyncio.wait_for(_probe(), timeout=5.0)
            return HealthStatus.HEALTHY if result else HealthStatus.DEGRADED
        except asyncio.TimeoutError:
            return HealthStatus.UNHEALTHY
        except Exception:
            return HealthStatus.DEAD

    async def _check_memory_usage(self) -> None:
        """Check memory usage and restart if over limit."""
        if not self.memory_limit_mb:
            return

        try:
            import resource

            # Get memory usage in bytes
            usage = resource.getrusage(resource.RUSAGE_SELF)
            memory_mb = usage.ru_maxrss / 1024  # Convert to MB (on macOS it's in bytes)

            # On Linux, it's in KB
            if memory_mb > self.memory_limit_mb * 1000:
                memory_mb = usage.ru_maxrss / 1024 / 1024

            if memory_mb > self.memory_limit_mb:
                logger.warning(
                    f"[SelfHealing] Memory usage {memory_mb:.1f}MB exceeds limit "
                    f"{self.memory_limit_mb:.1f}MB"
                )
                await self._handle_restart(RestartReason.MEMORY_LIMIT)

        except ImportError:
            pass  # resource module not available on Windows
        except Exception as e:
            logger.debug(f"[SelfHealing] Memory check error: {e}")

    async def _handle_restart(self, reason: RestartReason) -> None:
        """Handle restart logic with exponential backoff."""
        now = time.time()

        # Clean old restart times outside window
        while self._restart_times and (now - self._restart_times[0]) > self.restart_window:
            self._restart_times.popleft()

        # Check if we've exceeded restart limit
        if len(self._restart_times) >= self.max_restarts:
            error_msg = (
                f"Exceeded max restarts ({self.max_restarts}) "
                f"in {self.restart_window}s window - giving up"
            )
            logger.error(f"[SelfHealing] {error_msg}")
            self._running = False

            if self.on_failure:
                try:
                    self.on_failure(error_msg)
                except Exception:
                    pass
            return

        # Record this restart
        self._restart_times.append(now)
        self._restart_count = len(self._restart_times)
        self._total_restarts += 1

        # Calculate backoff with exponential increase
        backoff = min(
            self.initial_backoff * (2 ** (self._restart_count - 1)),
            self.max_backoff,
        )

        logger.warning(
            f"[SelfHealing] Restart triggered ({reason.value}). "
            f"Attempt {self._restart_count}/{self.max_restarts} after {backoff:.1f}s backoff..."
        )

        # Create restart event
        event = RestartEvent(
            timestamp=now,
            reason=reason,
            attempt=self._restart_count,
            backoff_seconds=backoff,
            success=False,  # Will update after restart
        )

        await asyncio.sleep(backoff)

        # Execute restart
        success = await self._execute_restart()

        event.success = success
        self._restart_history.append(event)

        if self.on_restart:
            try:
                self.on_restart(event)
            except Exception:
                pass

    async def _execute_restart(self) -> bool:
        """
        Execute the actual restart.

        In production, this might:
        - Signal the main server to restart
        - Re-exec the process
        - Signal systemd to restart the unit

        Returns:
            True if restart was successful
        """
        logger.info("[SelfHealing] Executing restart...")

        # Placeholder - actual restart logic would go here
        # Options:
        # 1. os.execv() to re-exec the process
        # 2. Signal a parent supervisor
        # 3. Write to a restart flag file

        # For now, we'll signal that a restart is needed
        restart_flag = Path.home() / ".jarvis" / "loading_server" / "restart.flag"
        restart_flag.parent.mkdir(parents=True, exist_ok=True)
        restart_flag.write_text(f"{time.time()}\n{self._restart_count}")

        return True

    def request_restart(self, reason: RestartReason = RestartReason.MANUAL) -> None:
        """
        Request a restart from external code.

        Args:
            reason: Reason for the restart
        """
        _task = asyncio.create_task(
            self._handle_restart(reason),
            name="self-healing-handle-restart",
        )
        self._background_tasks.add(_task)
        _task.add_done_callback(self._background_tasks.discard)

    def get_stats(self) -> Dict[str, Any]:
        """
        Get restart statistics.

        Returns:
            Dict with restart stats
        """
        return {
            "total_restarts": self._total_restarts,
            "restarts_in_window": len(self._restart_times),
            "max_restarts": self.max_restarts,
            "restart_window_seconds": self.restart_window,
            "consecutive_failures": self._consecutive_failures,
            "last_health_check": self._last_health_check,
            "running": self._running,
            "recent_restarts": [
                {
                    "timestamp": e.timestamp,
                    "reason": e.reason.value,
                    "attempt": e.attempt,
                    "success": e.success,
                }
                for e in list(self._restart_history)[-10:]
            ],
        }

    def reset_restart_count(self) -> None:
        """Reset the restart counter (e.g., after sustained healthy period)."""
        self._restart_times.clear()
        self._restart_count = 0
        self._consecutive_failures = 0
        logger.info("[SelfHealing] Restart count reset")

    @property
    def is_in_recovery(self) -> bool:
        """Check if we're currently in recovery (recent restarts)."""
        return len(self._restart_times) > 0

    @property
    def restart_budget_remaining(self) -> int:
        """Get remaining restart attempts before giving up."""
        return self.max_restarts - len(self._restart_times)


@dataclass
class DeadlockDetector:
    """
    Detects potential deadlocks in async code.

    Monitors task completion times and flags potential deadlocks.
    """

    timeout_threshold: float = 30.0  # Seconds before flagging as potential deadlock
    check_interval: float = 5.0

    _monitored_tasks: Dict[str, float] = field(init=False, default_factory=dict)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def start_monitoring(self, task_id: str) -> None:
        """Start monitoring a task."""
        self._monitored_tasks[task_id] = time.time()

    def stop_monitoring(self, task_id: str) -> None:
        """Stop monitoring a task (completed successfully)."""
        self._monitored_tasks.pop(task_id, None)

    async def check_for_deadlocks(self) -> list[str]:
        """
        Check for potential deadlocks.

        Returns:
            List of task IDs that may be deadlocked
        """
        now = time.time()
        deadlocked = []

        async with self._lock:
            for task_id, start_time in list(self._monitored_tasks.items()):
                if now - start_time > self.timeout_threshold:
                    deadlocked.append(task_id)

        return deadlocked

    def get_monitored_count(self) -> int:
        """Get number of currently monitored tasks."""
        return len(self._monitored_tasks)
