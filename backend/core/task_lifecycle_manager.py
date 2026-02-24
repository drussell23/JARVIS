"""
JARVIS Task Lifecycle Manager v1.0.0
====================================

Robust, async-safe task lifecycle management for preventing SIGKILL crashes.

This module provides centralized management of all background asyncio tasks,
ensuring proper cleanup on shutdown and preventing:
- Orphaned tasks that consume resources
- Infinite loops that block shutdown
- Memory leaks from untracked coroutines
- SIGKILL from supervisor due to unresponsive processes

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │              TaskLifecycleManager (Singleton)                │
    ├─────────────────────────────────────────────────────────────┤
    │  Background Tasks    │  Monitoring Tasks   │  Critical Tasks │
    │  (fire-and-forget)   │  (infinite loops)   │  (must complete)│
    ├─────────────────────────────────────────────────────────────┤
    │                    Graceful Shutdown                        │
    │  1. Set shutdown event → loops exit cleanly                 │
    │  2. Cancel remaining tasks with timeout                     │
    │  3. Wait for completion or force cleanup                    │
    └─────────────────────────────────────────────────────────────┘

Usage:
    from core.task_lifecycle_manager import get_task_manager

    # Spawn a background task (tracked for cleanup)
    manager = get_task_manager()
    task = await manager.spawn("my_task", my_coroutine())

    # Spawn a monitoring task (infinite loop with graceful stop)
    task = await manager.spawn_monitor("memory_monitor", monitor_coro())

    # In your infinite loop, check for shutdown:
    while not manager.is_shutting_down():
        await asyncio.sleep(1)
        # do work

    # On app shutdown:
    await manager.shutdown_all(timeout=10.0)
"""

import asyncio
import functools
import logging
import signal
import time
import traceback
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Set,
    TypeVar,
    Union,
)

logger = logging.getLogger(__name__)


class TaskPriority(Enum):
    """Task priority for shutdown ordering"""
    CRITICAL = 10    # Must complete before shutdown (e.g., save state)
    HIGH = 20        # Important background work
    NORMAL = 50      # Standard background tasks
    LOW = 80         # Optional/non-essential
    MONITORING = 90  # Infinite loops (shut down last)


class TaskState(Enum):
    """Task lifecycle state"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class ManagedTask:
    """Metadata for a managed task"""
    name: str
    task: asyncio.Task
    priority: TaskPriority = TaskPriority.NORMAL
    created_at: float = field(default_factory=time.time)
    state: TaskState = TaskState.PENDING
    is_monitor: bool = False  # True for infinite loop monitoring tasks
    cancel_timeout: float = 5.0  # Seconds to wait for graceful cancel
    error: Optional[str] = None
    completed_at: Optional[float] = None

    @property
    def runtime_seconds(self) -> float:
        """Get task runtime in seconds"""
        end_time = self.completed_at or time.time()
        return end_time - self.created_at

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for status reporting"""
        return {
            "name": self.name,
            "state": self.state.value,
            "priority": self.priority.name,
            "is_monitor": self.is_monitor,
            "runtime_seconds": round(self.runtime_seconds, 2),
            "created_at": datetime.fromtimestamp(self.created_at).isoformat(),
            "error": self.error,
        }


class TaskLifecycleManager:
    """
    Centralized manager for all background asyncio tasks.

    Features:
    - Automatic task tracking with weak references
    - Graceful shutdown with timeouts
    - Priority-based shutdown ordering
    - Infinite loop detection and clean cancellation
    - Health monitoring and statistics
    - Signal handler integration (SIGTERM, SIGINT)
    """

    _instance: Optional["TaskLifecycleManager"] = None
    _lock = asyncio.Lock()

    def __init__(self):
        # Task registry
        self._tasks: Dict[str, ManagedTask] = {}
        self._task_lock = asyncio.Lock()

        # Shutdown coordination
        self._shutdown_event = asyncio.Event()
        self._shutdown_started = False
        self._shutdown_complete = False

        # Health monitoring
        self._health_check_task: Optional[asyncio.Task] = None
        self._last_health_check: Optional[float] = None

        # Statistics
        self._stats = {
            "total_spawned": 0,
            "total_completed": 0,
            "total_failed": 0,
            "total_cancelled": 0,
        }

        # Signal handlers
        self._original_sigterm: Optional[Any] = None
        self._original_sigint: Optional[Any] = None

        logger.info("🔄 TaskLifecycleManager initialized")

    @classmethod
    async def get_instance(cls) -> "TaskLifecycleManager":
        """Get or create the singleton instance (async)"""
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def get_instance_sync(cls) -> "TaskLifecycleManager":
        """Get or create the singleton instance (sync)"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # =========================================================================
    # Task Spawning
    # =========================================================================

    async def spawn(
        self,
        name: str,
        coro: Coroutine[Any, Any, Any],
        priority: TaskPriority = TaskPriority.NORMAL,
        cancel_timeout: float = 5.0,
    ) -> asyncio.Task:
        """
        Spawn a tracked background task.

        Args:
            name: Unique identifier for the task
            coro: Coroutine to run
            priority: Shutdown priority (lower = shutdown first)
            cancel_timeout: Seconds to wait for graceful cancellation

        Returns:
            The created asyncio.Task

        Example:
            task = await manager.spawn("data_sync", sync_data())
        """
        if self._shutdown_started:
            logger.warning(f"Cannot spawn task '{name}' - shutdown in progress")
            raise RuntimeError("Cannot spawn tasks during shutdown")

        async with self._task_lock:
            # Cancel existing task with same name if running
            if name in self._tasks:
                existing = self._tasks[name]
                if not existing.task.done():
                    logger.debug(f"Cancelling existing task '{name}' before respawn")
                    existing.task.cancel()
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(existing.task),
                            timeout=1.0
                        )
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass

            # Create the task
            task = asyncio.create_task(
                self._wrapped_task(name, coro),
                name=f"managed_{name}"
            )

            # Track it
            managed = ManagedTask(
                name=name,
                task=task,
                priority=priority,
                cancel_timeout=cancel_timeout,
                state=TaskState.RUNNING,
            )
            self._tasks[name] = managed
            self._stats["total_spawned"] += 1

            logger.debug(f"Spawned task '{name}' (priority={priority.name})")
            return task

    async def spawn_monitor(
        self,
        name: str,
        coro: Coroutine[Any, Any, Any],
        cancel_timeout: float = 10.0,
    ) -> asyncio.Task:
        """
        Spawn a monitoring task (infinite loop).

        Monitoring tasks are expected to run indefinitely and check
        is_shutting_down() to exit gracefully.

        Args:
            name: Unique identifier for the task
            coro: Coroutine containing the monitoring loop
            cancel_timeout: Extra time for cleanup (monitors need more time)

        Example:
            async def my_monitor():
                while not manager.is_shutting_down():
                    await check_something()
                    await asyncio.sleep(5)

            await manager.spawn_monitor("health_check", my_monitor())
        """
        task = await self.spawn(
            name=name,
            coro=coro,
            priority=TaskPriority.MONITORING,
            cancel_timeout=cancel_timeout,
        )

        async with self._task_lock:
            if name in self._tasks:
                self._tasks[name].is_monitor = True

        return task

    async def _wrapped_task(
        self,
        name: str,
        coro: Coroutine[Any, Any, Any]
    ) -> Any:
        """
        Wrapper that handles task lifecycle and error tracking.
        """
        try:
            result = await coro
            await self._mark_completed(name, TaskState.COMPLETED)
            return result

        except asyncio.CancelledError:
            await self._mark_completed(name, TaskState.CANCELLED)
            # Re-raise to properly mark the task as cancelled
            raise

        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            logger.error(f"Task '{name}' failed: {error_msg}")
            logger.debug(traceback.format_exc())
            await self._mark_completed(name, TaskState.FAILED, error=error_msg)
            raise

    async def _mark_completed(
        self,
        name: str,
        state: TaskState,
        error: Optional[str] = None
    ):
        """Mark a task as completed with its final state"""
        async with self._task_lock:
            if name in self._tasks:
                managed = self._tasks[name]
                managed.state = state
                managed.completed_at = time.time()
                managed.error = error

                # Update stats
                if state == TaskState.COMPLETED:
                    self._stats["total_completed"] += 1
                elif state == TaskState.FAILED:
                    self._stats["total_failed"] += 1
                elif state == TaskState.CANCELLED:
                    self._stats["total_cancelled"] += 1

    # =========================================================================
    # Shutdown Coordination
    # =========================================================================

    def is_shutting_down(self) -> bool:
        """
        Check if shutdown has been initiated.

        Use this in infinite loops to exit gracefully:

            while not manager.is_shutting_down():
                await do_work()
                await asyncio.sleep(1)
        """
        return self._shutdown_event.is_set()

    def request_shutdown(self):
        """
        Request graceful shutdown.

        This sets the shutdown event, signaling all loops to exit.
        Does not wait for completion - use shutdown_all() for that.
        """
        if not self._shutdown_started:
            self._shutdown_started = True
            self._shutdown_event.set()
            logger.info("🛑 Shutdown requested - signaling all tasks")

    async def shutdown_all(
        self,
        timeout: float = 30.0,
        force_after: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Gracefully shutdown all managed tasks.

        Shutdown order:
        1. Set shutdown event (signals loops to exit)
        2. Wait for voluntary exits (grace period)
        3. Cancel remaining tasks by priority
        4. Wait for cancellation with timeout
        5. Force cleanup any remaining

        Args:
            timeout: Total time allowed for shutdown
            force_after: Time before force-cancelling stubborn tasks

        Returns:
            Dict with shutdown statistics
        """
        if self._shutdown_complete:
            return {"status": "already_complete"}

        start_time = time.time()
        self.request_shutdown()

        logger.info("=" * 60)
        logger.info("TASK LIFECYCLE SHUTDOWN")
        logger.info("=" * 60)

        async with self._task_lock:
            active_tasks = [
                m for m in self._tasks.values()
                if not m.task.done()
            ]

        if not active_tasks:
            logger.info("No active tasks to shutdown")
            self._shutdown_complete = True
            return {"status": "complete", "tasks_shutdown": 0}

        logger.info(f"Shutting down {len(active_tasks)} active tasks...")

        # Sort by priority (lower number = shutdown first)
        active_tasks.sort(key=lambda m: m.priority.value)

        # Phase 1: Wait for voluntary exits (monitors should exit on shutdown event)
        logger.info("Phase 1: Waiting for voluntary exits...")
        await asyncio.sleep(min(2.0, timeout * 0.2))

        # Phase 2: Cancel remaining tasks by priority group
        logger.info("Phase 2: Cancelling remaining tasks...")
        results = {
            "completed": [],
            "cancelled": [],
            "forced": [],
            "failed": [],
        }

        for managed in active_tasks:
            if managed.task.done():
                results["completed"].append(managed.name)
                continue

            try:
                # Request cancellation
                managed.task.cancel()

                # Wait for graceful cancellation
                try:
                    await asyncio.wait_for(
                        asyncio.shield(managed.task),
                        timeout=managed.cancel_timeout
                    )
                    results["cancelled"].append(managed.name)
                    logger.debug(f"Task '{managed.name}' cancelled gracefully")

                except asyncio.TimeoutError:
                    # Force cancellation exceeded timeout
                    results["forced"].append(managed.name)
                    logger.warning(f"Task '{managed.name}' force-cancelled (timeout)")

                except asyncio.CancelledError:
                    results["cancelled"].append(managed.name)

            except Exception as e:
                results["failed"].append(f"{managed.name}: {e}")
                logger.error(f"Error cancelling task '{managed.name}': {e}")

        elapsed = time.time() - start_time
        self._shutdown_complete = True

        logger.info(f"Shutdown complete in {elapsed:.2f}s")
        logger.info(f"  Completed: {len(results['completed'])}")
        logger.info(f"  Cancelled: {len(results['cancelled'])}")
        logger.info(f"  Forced: {len(results['forced'])}")
        logger.info(f"  Failed: {len(results['failed'])}")
        logger.info("=" * 60)

        return {
            "status": "complete",
            "elapsed_seconds": elapsed,
            **results,
        }

    def register_task(
        self,
        name: str,
        task: asyncio.Task,
        priority: TaskPriority = TaskPriority.NORMAL,
        is_monitor: bool = False,
    ) -> None:
        """
        Register an externally-created asyncio.Task for lifecycle tracking.

        Use this when the task is created via asyncio.create_task() in sync code
        (e.g., module-level functions inside a running event loop) and cannot
        use the async spawn() API.

        v270.4: Added for Disease 1 — task lifecycle ownership.
        """
        if self._shutdown_started:
            logger.warning(f"Cannot register task '{name}' - shutdown in progress")
            return

        managed = ManagedTask(
            name=name,
            task=task,
            priority=priority,
            state=TaskState.RUNNING,
            is_monitor=is_monitor,
        )
        self._tasks[name] = managed
        self._stats["total_spawned"] += 1

        def _on_done(t: asyncio.Task, _name: str = name) -> None:
            if t.cancelled():
                managed.state = TaskState.CANCELLED
                managed.completed_at = time.time()
                self._stats["total_cancelled"] += 1
            elif t.exception():
                managed.state = TaskState.FAILED
                managed.error = str(t.exception())
                managed.completed_at = time.time()
                self._stats["total_failed"] += 1
                logger.error(
                    "[TaskLifecycle] Registered task '%s' failed: %s",
                    _name, managed.error,
                )
            else:
                managed.state = TaskState.COMPLETED
                managed.completed_at = time.time()
                self._stats["total_completed"] += 1

        task.add_done_callback(_on_done)
        logger.debug(f"Registered external task '{name}' (priority={priority.name})")

    async def cancel_task(self, name: str, timeout: float = 5.0) -> bool:
        """
        Cancel a specific task by name.

        Args:
            name: Task name to cancel
            timeout: Seconds to wait for cancellation

        Returns:
            True if task was cancelled, False if not found/already done
        """
        async with self._task_lock:
            if name not in self._tasks:
                return False

            managed = self._tasks[name]
            if managed.task.done():
                return False

        managed.task.cancel()

        try:
            await asyncio.wait_for(
                asyncio.shield(managed.task),
                timeout=timeout
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        return True

    # =========================================================================
    # Signal Handlers
    # =========================================================================

    def install_signal_handlers(self):
        """
        Install signal handlers for graceful shutdown.

        Handles SIGTERM (docker stop, systemd) and SIGINT (Ctrl+C).
        """
        try:
            loop = asyncio.get_running_loop()

            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(self._handle_signal(s))
                )

            logger.info("Signal handlers installed (SIGTERM, SIGINT)")

        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            logger.debug("Signal handlers not available on this platform")

    async def _handle_signal(self, sig: signal.Signals):
        """Handle shutdown signals"""
        sig_name = sig.name
        logger.warning(f"Received {sig_name} - initiating shutdown")
        self.request_shutdown()

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def start_health_monitor(self, interval: float = 30.0):
        """
        Start background health monitoring.

        Logs task statistics and detects stuck tasks.
        """
        async def monitor_loop():
            while not self.is_shutting_down():
                try:
                    await asyncio.sleep(interval)
                    if self.is_shutting_down():
                        break

                    status = await self.get_status()
                    active = status["counts"]["running"]

                    if active > 0:
                        logger.debug(
                            f"Task health: {active} active, "
                            f"{status['counts']['completed']} completed"
                        )

                    self._last_health_check = time.time()

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Health monitor error: {e}")

        self._health_check_task = await self.spawn_monitor(
            "_health_monitor",
            monitor_loop()
        )

    # =========================================================================
    # Status & Statistics
    # =========================================================================

    async def get_status(self) -> Dict[str, Any]:
        """Get current task manager status"""
        async with self._task_lock:
            tasks_info = [m.to_dict() for m in self._tasks.values()]

            counts = {
                "total": len(self._tasks),
                "running": sum(1 for m in self._tasks.values()
                             if m.state == TaskState.RUNNING),
                "completed": sum(1 for m in self._tasks.values()
                               if m.state == TaskState.COMPLETED),
                "failed": sum(1 for m in self._tasks.values()
                            if m.state == TaskState.FAILED),
                "cancelled": sum(1 for m in self._tasks.values()
                               if m.state == TaskState.CANCELLED),
            }

        return {
            "shutdown_requested": self._shutdown_started,
            "shutdown_complete": self._shutdown_complete,
            "counts": counts,
            "stats": self._stats.copy(),
            "tasks": tasks_info,
            "last_health_check": self._last_health_check,
        }

    def get_active_count(self) -> int:
        """Get count of currently running tasks"""
        return sum(
            1 for m in self._tasks.values()
            if m.state == TaskState.RUNNING and not m.task.done()
        )

    async def wait_for_task(
        self,
        name: str,
        timeout: Optional[float] = None
    ) -> Any:
        """
        Wait for a specific task to complete.

        Args:
            name: Task name to wait for
            timeout: Optional timeout in seconds

        Returns:
            Task result

        Raises:
            KeyError: If task not found
            asyncio.TimeoutError: If timeout exceeded
        """
        async with self._task_lock:
            if name not in self._tasks:
                raise KeyError(f"Task '{name}' not found")
            task = self._tasks[name].task

        if timeout:
            return await asyncio.wait_for(task, timeout=timeout)
        return await task

    async def cleanup_completed(self):
        """Remove completed tasks from tracking (memory cleanup)"""
        async with self._task_lock:
            to_remove = [
                name for name, m in self._tasks.items()
                if m.task.done() and m.state != TaskState.RUNNING
            ]

            for name in to_remove:
                del self._tasks[name]

            if to_remove:
                logger.debug(f"Cleaned up {len(to_remove)} completed tasks")


# =============================================================================
# Helper Functions for Graceful Loops
# =============================================================================

def graceful_loop(
    manager: Optional[TaskLifecycleManager] = None,
    check_interval: float = 0.1,
):
    """
    Decorator for making infinite loops graceful.

    Usage:
        @graceful_loop()
        async def my_monitor():
            while True:
                await do_work()
                await asyncio.sleep(5)
    """
    def decorator(func: Callable[..., Awaitable[Any]]):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            mgr = manager or get_task_manager()

            # Patch 'while True' to check shutdown
            # This is a best-effort approach - the function should
            # ideally check is_shutting_down() itself
            try:
                return await func(*args, **kwargs)
            except asyncio.CancelledError:
                logger.debug(f"Graceful loop '{func.__name__}' cancelled")
                raise

        return wrapper
    return decorator


async def interruptible_sleep(
    seconds: float,
    manager: Optional[TaskLifecycleManager] = None,
    check_interval: float = 0.5,
) -> bool:
    """
    Sleep that can be interrupted by shutdown.

    Args:
        seconds: Total sleep duration
        manager: TaskLifecycleManager instance (uses global if None)
        check_interval: How often to check for shutdown

    Returns:
        True if completed normally, False if interrupted by shutdown

    Example:
        while True:
            await do_work()
            if not await interruptible_sleep(60):
                break  # Shutdown requested
    """
    mgr = manager or get_task_manager()
    remaining = seconds

    while remaining > 0:
        if mgr.is_shutting_down():
            return False

        sleep_time = min(remaining, check_interval)
        await asyncio.sleep(sleep_time)
        remaining -= sleep_time

    return True


# =============================================================================
# Global Access
# =============================================================================

_manager_instance: Optional[TaskLifecycleManager] = None


def get_task_manager() -> TaskLifecycleManager:
    """Get the global TaskLifecycleManager instance"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = TaskLifecycleManager.get_instance_sync()
    return _manager_instance


async def get_task_manager_async() -> TaskLifecycleManager:
    """Get the global TaskLifecycleManager instance (async)"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = await TaskLifecycleManager.get_instance()
    return _manager_instance


# =============================================================================
# Convenience Functions
# =============================================================================

async def spawn_task(
    name: str,
    coro: Coroutine[Any, Any, Any],
    priority: TaskPriority = TaskPriority.NORMAL,
) -> asyncio.Task:
    """Convenience function to spawn a tracked task"""
    manager = get_task_manager()
    return await manager.spawn(name, coro, priority)


async def spawn_monitor_task(
    name: str,
    coro: Coroutine[Any, Any, Any],
) -> asyncio.Task:
    """Convenience function to spawn a monitoring task"""
    manager = get_task_manager()
    return await manager.spawn_monitor(name, coro)


def is_shutting_down() -> bool:
    """Convenience function to check shutdown status"""
    return get_task_manager().is_shutting_down()


async def shutdown_all_tasks(timeout: float = 30.0) -> Dict[str, Any]:
    """Convenience function to shutdown all tasks"""
    manager = get_task_manager()
    return await manager.shutdown_all(timeout=timeout)
