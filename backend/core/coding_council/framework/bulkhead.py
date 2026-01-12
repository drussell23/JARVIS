"""
v77.0: Bulkhead Isolation - Gap #13
====================================

Bulkhead pattern for fault isolation:
- Separate execution pools
- Concurrent execution limits
- Queue management
- Timeout per bulkhead
- Resource isolation

Author: JARVIS v77.0
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, Optional, TypeVar

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BulkheadFull(Exception):
    """Raised when bulkhead is at capacity."""
    def __init__(self, name: str, active: int, max_concurrent: int, queued: int):
        self.name = name
        self.active = active
        self.max_concurrent = max_concurrent
        self.queued = queued
        super().__init__(
            f"Bulkhead '{name}' full: {active}/{max_concurrent} active, {queued} queued"
        )


@dataclass
class BulkheadConfig:
    """Configuration for bulkhead."""
    max_concurrent: int = 10      # Max concurrent executions
    max_queue_size: int = 100     # Max waiting in queue
    queue_timeout: float = 30.0   # Max time to wait in queue
    execution_timeout: float = 60.0  # Max execution time


@dataclass
class BulkheadStats:
    """Statistics for a bulkhead."""
    total_calls: int = 0
    successful_calls: int = 0
    rejected_calls: int = 0
    timed_out_calls: int = 0
    current_active: int = 0
    current_queued: int = 0
    max_active_seen: int = 0
    total_wait_time: float = 0.0
    total_execution_time: float = 0.0


class Bulkhead:
    """
    Bulkhead for isolating execution pools.

    Prevents one failing component from consuming
    all resources and affecting others.

    Features:
    - Concurrent execution limit
    - Queue with timeout
    - Per-bulkhead isolation
    - Statistics tracking
    """

    def __init__(
        self,
        name: str,
        config: Optional[BulkheadConfig] = None,
    ):
        self.name = name
        self.config = config or BulkheadConfig()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent)
        self._queue_semaphore = asyncio.Semaphore(self.config.max_queue_size)
        self._stats = BulkheadStats()
        self._active = 0
        self._queued = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, timeout: Optional[float] = None):
        """
        Acquire a slot in the bulkhead.

        Usage:
            async with bulkhead.acquire():
                await do_work()
        """
        queue_timeout = timeout if timeout is not None else self.config.queue_timeout
        start_time = time.time()

        # Try to enter queue
        if not self._queue_semaphore.locked():
            acquired_queue = self._queue_semaphore.acquire_nowait()
        else:
            acquired_queue = False

        if not acquired_queue:
            # Queue is full
            async with self._lock:
                self._stats.rejected_calls += 1
            raise BulkheadFull(
                self.name,
                self._active,
                self.config.max_concurrent,
                self._queued,
            )

        try:
            async with self._lock:
                self._queued += 1
                self._stats.total_calls += 1

            # Wait for execution slot
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=queue_timeout,
                )
            except asyncio.TimeoutError:
                async with self._lock:
                    self._stats.timed_out_calls += 1
                    self._queued -= 1
                raise TimeoutError(
                    f"Bulkhead '{self.name}' queue timeout after {queue_timeout}s"
                )

            # Got execution slot
            wait_time = time.time() - start_time

            async with self._lock:
                self._queued -= 1
                self._active += 1
                self._stats.total_wait_time += wait_time
                self._stats.max_active_seen = max(
                    self._stats.max_active_seen,
                    self._active
                )

            execution_start = time.time()

            try:
                yield
                async with self._lock:
                    self._stats.successful_calls += 1
            finally:
                execution_time = time.time() - execution_start
                async with self._lock:
                    self._active -= 1
                    self._stats.total_execution_time += execution_time

                self._semaphore.release()

        finally:
            self._queue_semaphore.release()

    async def execute(
        self,
        func: Callable[..., Coroutine],
        *args,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> Any:
        """
        Execute a function within the bulkhead.

        Applies both queue timeout and execution timeout.
        """
        exec_timeout = timeout if timeout is not None else self.config.execution_timeout

        async with self.acquire():
            try:
                return await asyncio.wait_for(
                    func(*args, **kwargs),
                    timeout=exec_timeout,
                )
            except asyncio.TimeoutError:
                async with self._lock:
                    self._stats.timed_out_calls += 1
                raise TimeoutError(
                    f"Bulkhead '{self.name}' execution timeout after {exec_timeout}s"
                )

    @property
    def active_count(self) -> int:
        """Get current active executions."""
        return self._active

    @property
    def queued_count(self) -> int:
        """Get current queued requests."""
        return self._queued

    @property
    def available_slots(self) -> int:
        """Get available execution slots."""
        return self.config.max_concurrent - self._active

    def get_stats(self) -> Dict[str, Any]:
        """Get bulkhead statistics."""
        avg_wait = (
            self._stats.total_wait_time / self._stats.total_calls
            if self._stats.total_calls > 0 else 0
        )
        avg_exec = (
            self._stats.total_execution_time / self._stats.successful_calls
            if self._stats.successful_calls > 0 else 0
        )

        return {
            "name": self.name,
            "config": {
                "max_concurrent": self.config.max_concurrent,
                "max_queue": self.config.max_queue_size,
            },
            "current": {
                "active": self._active,
                "queued": self._queued,
                "available": self.available_slots,
            },
            "stats": {
                "total_calls": self._stats.total_calls,
                "successful": self._stats.successful_calls,
                "rejected": self._stats.rejected_calls,
                "timed_out": self._stats.timed_out_calls,
                "max_active_seen": self._stats.max_active_seen,
                "avg_wait_time": avg_wait,
                "avg_execution_time": avg_exec,
            },
        }

    async def reset_stats(self) -> None:
        """Reset statistics."""
        async with self._lock:
            self._stats = BulkheadStats()


# Global registry
_bulkheads: Dict[str, Bulkhead] = {}
_registry_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_bulkhead(
    name: str,
    config: Optional[BulkheadConfig] = None,
) -> Bulkhead:
    """Get or create a bulkhead by name."""
    async with _registry_lock:
        if name not in _bulkheads:
            _bulkheads[name] = Bulkhead(name, config)
        return _bulkheads[name]


def bulkhead(
    name: str,
    max_concurrent: int = 10,
    max_queue: int = 100,
    timeout: Optional[float] = None,
):
    """
    Decorator to execute function within a bulkhead.

    Usage:
        @bulkhead("api_calls", max_concurrent=5)
        async def call_api():
            ...
    """
    config = BulkheadConfig(
        max_concurrent=max_concurrent,
        max_queue_size=max_queue,
    )

    def decorator(func: Callable[..., Coroutine]) -> Callable[..., Coroutine]:
        bh: Optional[Bulkhead] = None

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            nonlocal bh
            if bh is None:
                bh = await get_bulkhead(name, config)

            return await bh.execute(func, *args, timeout=timeout, **kwargs)

        return wrapper
    return decorator


class BulkheadCoordinator:
    """
    Coordinator for managing multiple bulkheads.

    Provides global view and management of all bulkheads.
    """

    def __init__(self):
        self._bulkheads: Dict[str, Bulkhead] = {}

    def register(self, bulkhead: Bulkhead) -> None:
        """Register a bulkhead."""
        self._bulkheads[bulkhead.name] = bulkhead

    def get(self, name: str) -> Optional[Bulkhead]:
        """Get bulkhead by name."""
        return self._bulkheads.get(name)

    def get_all_stats(self) -> Dict[str, Any]:
        """Get stats for all bulkheads."""
        return {
            name: bh.get_stats()
            for name, bh in self._bulkheads.items()
        }

    async def reset_all_stats(self) -> None:
        """Reset stats for all bulkheads."""
        for bh in self._bulkheads.values():
            await bh.reset_stats()


# Global coordinator
_coordinator: Optional[BulkheadCoordinator] = None


def get_bulkhead_coordinator() -> BulkheadCoordinator:
    """Get global bulkhead coordinator."""
    global _coordinator
    if _coordinator is None:
        _coordinator = BulkheadCoordinator()
    return _coordinator
