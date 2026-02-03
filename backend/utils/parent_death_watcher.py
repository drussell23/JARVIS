"""
JARVIS Parent Death Watcher
============================

Monitors the parent process (kernel) and initiates graceful shutdown when
it dies. This prevents orphaned child processes when the supervisor crashes
or is killed unexpectedly.

v211.0: Root cause fix for orphaned processes.

Usage in backend/main.py:
    from backend.utils.parent_death_watcher import start_parent_watcher

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Start parent watcher early
        watcher = await start_parent_watcher()
        try:
            yield
        finally:
            if watcher:
                await watcher.stop()

Architecture:
- Reads JARVIS_KERNEL_PID from environment (set by supervisor)
- Polls parent process liveness at configurable interval (default 2s)
- On parent death: logs, sends SIGTERM to self, then SIGKILL after grace period
- Works on macOS, Linux, and Windows
- Fully async with sync fallback for shutdown

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class ParentWatcherConfig:
    """Configuration for parent death watcher."""

    # How often to check parent liveness (seconds)
    poll_interval: float = 2.0

    # Grace period after parent death before forced exit (seconds)
    grace_period: float = 5.0

    # Signal to send on parent death (default: SIGTERM for graceful shutdown)
    death_signal: int = signal.SIGTERM

    # Whether to forcefully exit after grace period
    force_exit_on_timeout: bool = True

    # Exit code when parent dies
    exit_code: int = 143  # 128 + 15 (SIGTERM)

    # Callback to run before shutdown (optional)
    pre_shutdown_callback: Optional[Callable[[], None]] = None

    # Whether to log verbose status
    verbose: bool = False


class ParentDeathWatcher:
    """
    Watches for parent process death and initiates shutdown.

    This solves the orphaned process problem by having child processes
    automatically exit when their parent (supervisor/kernel) dies.

    Features:
    - Async-native with sync thread fallback
    - Configurable poll interval and grace period
    - Graceful shutdown with SIGTERM, forced with SIGKILL
    - Works across platforms (macOS, Linux, Windows)
    - Pre-shutdown callback support for cleanup
    """

    def __init__(
        self,
        parent_pid: int,
        config: Optional[ParentWatcherConfig] = None,
    ):
        """
        Initialize the parent death watcher.

        Args:
            parent_pid: PID of the parent process to monitor
            config: Configuration options
        """
        self._parent_pid = parent_pid
        self._config = config or ParentWatcherConfig()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None
        self._shutdown_initiated = False
        self._lock = threading.Lock()
        self._my_pid = os.getpid()

        logger.info(
            f"[ParentWatcher] Initialized: monitoring parent PID {parent_pid} "
            f"(my PID: {self._my_pid}, poll: {self._config.poll_interval}s)"
        )

    def _is_parent_alive(self) -> bool:
        """
        Check if the parent process is still alive.

        Returns:
            True if parent is alive, False otherwise.
        """
        try:
            # os.kill with signal 0 checks if process exists
            os.kill(self._parent_pid, 0)
            return True
        except ProcessLookupError:
            # Parent process doesn't exist
            return False
        except PermissionError:
            # Process exists but we can't signal it (still alive)
            return True
        except OSError:
            # Other OS error - assume dead
            return False

    def _is_orphaned(self) -> bool:
        """
        Check if this process has been orphaned (parent is init/1).

        On Unix, orphaned processes get adopted by init (PID 1).

        Returns:
            True if orphaned, False otherwise.
        """
        try:
            current_ppid = os.getppid()
            # PID 1 is init/systemd/launchd - if we're parented to it, we're orphaned
            if current_ppid == 1:
                return True
            # Check if our current parent matches expected parent
            if current_ppid != self._parent_pid:
                # Parent changed - could be orphaned
                return not self._is_parent_alive()
            return False
        except Exception:
            return False

    def _initiate_shutdown(self, reason: str) -> None:
        """
        Initiate graceful shutdown of this process.

        Args:
            reason: Human-readable reason for shutdown
        """
        with self._lock:
            if self._shutdown_initiated:
                return
            self._shutdown_initiated = True

        logger.warning(
            f"[ParentWatcher] Parent death detected (PID {self._parent_pid}). "
            f"Reason: {reason}. Initiating graceful shutdown..."
        )

        # Run pre-shutdown callback if configured
        if self._config.pre_shutdown_callback:
            try:
                self._config.pre_shutdown_callback()
            except Exception as e:
                logger.error(f"[ParentWatcher] Pre-shutdown callback error: {e}")

        # Send death signal to self (graceful shutdown)
        try:
            os.kill(self._my_pid, self._config.death_signal)
        except Exception as e:
            logger.error(f"[ParentWatcher] Failed to send {self._config.death_signal}: {e}")

        # Start a background thread for forced exit after grace period
        if self._config.force_exit_on_timeout:
            def _force_exit():
                time.sleep(self._config.grace_period)
                logger.error(
                    f"[ParentWatcher] Grace period ({self._config.grace_period}s) exceeded. "
                    f"Forcing exit with code {self._config.exit_code}..."
                )
                # Use os._exit for immediate termination
                os._exit(self._config.exit_code)

            exit_thread = threading.Thread(target=_force_exit, daemon=True)
            exit_thread.start()

    async def _watch_loop(self) -> None:
        """Async loop that monitors parent process."""
        logger.debug("[ParentWatcher] Async watch loop started")

        consecutive_failures = 0

        while self._running:
            try:
                # Check parent liveness
                if not self._is_parent_alive():
                    consecutive_failures += 1

                    if consecutive_failures >= 2:  # Require 2 consecutive failures
                        self._initiate_shutdown(
                            f"Parent PID {self._parent_pid} no longer exists"
                        )
                        return
                else:
                    consecutive_failures = 0

                # Check for orphan status
                if self._is_orphaned():
                    self._initiate_shutdown(
                        f"Process orphaned (current ppid: {os.getppid()})"
                    )
                    return

                if self._config.verbose:
                    logger.debug(
                        f"[ParentWatcher] Parent {self._parent_pid} alive, "
                        f"current ppid: {os.getppid()}"
                    )

                await asyncio.sleep(self._config.poll_interval)

            except asyncio.CancelledError:
                logger.debug("[ParentWatcher] Watch loop cancelled")
                break
            except Exception as e:
                logger.error(f"[ParentWatcher] Watch loop error: {e}")
                await asyncio.sleep(self._config.poll_interval)

        logger.debug("[ParentWatcher] Async watch loop stopped")

    def _watch_loop_sync(self) -> None:
        """Sync loop (runs in thread) for environments without event loop."""
        logger.debug("[ParentWatcher] Sync watch thread started")

        consecutive_failures = 0

        while self._running:
            try:
                if not self._is_parent_alive():
                    consecutive_failures += 1

                    if consecutive_failures >= 2:
                        self._initiate_shutdown(
                            f"Parent PID {self._parent_pid} no longer exists"
                        )
                        return
                else:
                    consecutive_failures = 0

                if self._is_orphaned():
                    self._initiate_shutdown(
                        f"Process orphaned (current ppid: {os.getppid()})"
                    )
                    return

                time.sleep(self._config.poll_interval)

            except Exception as e:
                logger.error(f"[ParentWatcher] Sync watch error: {e}")
                time.sleep(self._config.poll_interval)

        logger.debug("[ParentWatcher] Sync watch thread stopped")

    async def start(self) -> bool:
        """
        Start the parent watcher.

        Returns:
            True if started successfully, False otherwise.
        """
        if self._running:
            return True

        self._running = True

        try:
            # Prefer async task
            self._task = asyncio.create_task(
                self._watch_loop(),
                name="parent-death-watcher"
            )
            logger.info("[ParentWatcher] Started async watch task")
            return True
        except RuntimeError:
            # No event loop - use thread
            self._thread = threading.Thread(
                target=self._watch_loop_sync,
                daemon=True,
                name="parent-death-watcher"
            )
            self._thread.start()
            logger.info("[ParentWatcher] Started sync watch thread")
            return True

    def start_sync(self) -> bool:
        """
        Start the parent watcher synchronously (in a background thread).

        Returns:
            True if started successfully, False otherwise.
        """
        if self._running:
            return True

        self._running = True

        self._thread = threading.Thread(
            target=self._watch_loop_sync,
            daemon=True,
            name="parent-death-watcher"
        )
        self._thread.start()
        logger.info("[ParentWatcher] Started sync watch thread")
        return True

    async def stop(self) -> None:
        """Stop the parent watcher."""
        self._running = False

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("[ParentWatcher] Stopped")

    def stop_sync(self) -> None:
        """Stop the parent watcher synchronously."""
        self._running = False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        logger.info("[ParentWatcher] Stopped")


# Global watcher instance
_watcher_instance: Optional[ParentDeathWatcher] = None
_watcher_lock = threading.Lock()


def get_kernel_pid() -> Optional[int]:
    """
    Get the kernel PID from environment.

    Returns:
        Kernel PID if set, None otherwise.
    """
    kernel_pid_str = os.environ.get("JARVIS_KERNEL_PID")
    if kernel_pid_str:
        try:
            return int(kernel_pid_str)
        except ValueError:
            logger.warning(f"[ParentWatcher] Invalid JARVIS_KERNEL_PID: {kernel_pid_str}")
    return None


async def start_parent_watcher(
    config: Optional[ParentWatcherConfig] = None,
) -> Optional[ParentDeathWatcher]:
    """
    Start the global parent death watcher.

    This should be called early in child process startup (e.g., in FastAPI lifespan).

    Args:
        config: Optional configuration

    Returns:
        ParentDeathWatcher instance if started, None if no kernel PID available.

    Example:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            watcher = await start_parent_watcher()
            try:
                yield
            finally:
                if watcher:
                    await watcher.stop()
    """
    global _watcher_instance

    kernel_pid = get_kernel_pid()
    if not kernel_pid:
        logger.debug(
            "[ParentWatcher] JARVIS_KERNEL_PID not set - running standalone, "
            "parent death watcher disabled"
        )
        return None

    with _watcher_lock:
        if _watcher_instance is not None:
            logger.debug("[ParentWatcher] Already running")
            return _watcher_instance

        _watcher_instance = ParentDeathWatcher(kernel_pid, config)
        await _watcher_instance.start()
        return _watcher_instance


def start_parent_watcher_sync(
    config: Optional[ParentWatcherConfig] = None,
) -> Optional[ParentDeathWatcher]:
    """
    Start the global parent death watcher synchronously.

    Args:
        config: Optional configuration

    Returns:
        ParentDeathWatcher instance if started, None if no kernel PID available.
    """
    global _watcher_instance

    kernel_pid = get_kernel_pid()
    if not kernel_pid:
        logger.debug(
            "[ParentWatcher] JARVIS_KERNEL_PID not set - running standalone"
        )
        return None

    with _watcher_lock:
        if _watcher_instance is not None:
            return _watcher_instance

        _watcher_instance = ParentDeathWatcher(kernel_pid, config)
        _watcher_instance.start_sync()
        return _watcher_instance


async def stop_parent_watcher() -> None:
    """Stop the global parent death watcher."""
    global _watcher_instance

    with _watcher_lock:
        if _watcher_instance:
            await _watcher_instance.stop()
            _watcher_instance = None


def stop_parent_watcher_sync() -> None:
    """Stop the global parent death watcher synchronously."""
    global _watcher_instance

    with _watcher_lock:
        if _watcher_instance:
            _watcher_instance.stop_sync()
            _watcher_instance = None


__all__ = [
    "ParentDeathWatcher",
    "ParentWatcherConfig",
    "start_parent_watcher",
    "start_parent_watcher_sync",
    "stop_parent_watcher",
    "stop_parent_watcher_sync",
    "get_kernel_pid",
]
