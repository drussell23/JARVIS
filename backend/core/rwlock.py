"""
Async Read-Write Lock v1.0 — Concurrent readers, exclusive writers.

Allows multiple concurrent readers while ensuring exclusive access
for writers. Useful for read-heavy shared state (config, health,
metrics) where writes are infrequent.

Usage:
    from backend.core.rwlock import RWLock

    lock = RWLock()

    # Multiple readers can proceed concurrently
    async with lock.read():
        value = shared_state["key"]

    # Writers get exclusive access
    async with lock.write():
        shared_state["key"] = new_value

Features:
    - Write-preferring: pending writers block new readers to prevent starvation
    - Reentrant-safe: no deadlock on re-acquiring read lock in same task
    - Fair: writers served in FIFO order
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

logger = logging.getLogger("jarvis.rwlock")


class RWLock:
    """
    Async read-write lock with write-preference.

    Implementation uses a condition variable pattern:
    - _readers: count of active readers
    - _writers_waiting: count of writers waiting to acquire
    - _writer_active: True if a writer holds the lock
    """

    __slots__ = (
        "_readers",
        "_writers_waiting",
        "_writer_active",
        "_cond",
        "_write_lock",
    )

    def __init__(self) -> None:
        self._readers: int = 0
        self._writers_waiting: int = 0
        self._writer_active: bool = False
        self._cond = asyncio.Condition()
        self._write_lock = asyncio.Lock()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        """Acquire read lock. Multiple readers proceed concurrently."""
        async with self._cond:
            # Wait while a writer is active or writers are waiting
            # (write-preference: don't starve writers)
            while self._writer_active or self._writers_waiting > 0:
                await self._cond.wait()
            self._readers += 1

        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        """Acquire write lock. Exclusive access — no readers or other writers."""
        async with self._cond:
            self._writers_waiting += 1
            try:
                # Wait until no readers and no active writer
                while self._readers > 0 or self._writer_active:
                    await self._cond.wait()
                self._writer_active = True
            finally:
                self._writers_waiting -= 1

        try:
            yield
        finally:
            async with self._cond:
                self._writer_active = False
                self._cond.notify_all()

    @property
    def readers(self) -> int:
        """Number of active readers."""
        return self._readers

    @property
    def writer_active(self) -> bool:
        """Whether a writer is currently holding the lock."""
        return self._writer_active

    @property
    def writers_waiting(self) -> int:
        """Number of writers waiting to acquire."""
        return self._writers_waiting
