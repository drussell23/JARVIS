"""
Thread-Safe Asyncio Utilities for JARVIS
=========================================

Provides utilities for working with asyncio in threaded contexts,
particularly when background threads need to interact with async code.

Key Features:
- LazyAsyncLock: Lazy initialization of asyncio.Lock to avoid "no event loop" errors
- get_or_create_event_loop: Thread-safe event loop getter
- Thread-safe cleanup on shutdown

Usage:
    from core.async_thread_utils import LazyAsyncLock, get_or_create_event_loop

    class MyClass:
        def __init__(self):
            # This is safe to call from any thread
            self._lock = LazyAsyncLock()

        async def my_method(self):
            async with self._lock:
                # Protected code
                pass

Version: 1.0.0
"""
from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Thread-Safe Event Loop Management
# =============================================================================

# Store event loops created for threads
_thread_event_loops: Dict[int, asyncio.AbstractEventLoop] = {}
_thread_loop_lock = threading.Lock()

# Track all LazyAsyncLock instances for cleanup
_lazy_locks: weakref.WeakSet = weakref.WeakSet()


def get_or_create_event_loop() -> asyncio.AbstractEventLoop:
    """
    Get the current event loop or create one for the current thread.

    This is thread-safe and handles the case where code runs in a
    ThreadPoolExecutor thread that doesn't have an event loop.

    Returns:
        An asyncio event loop for the current thread

    Usage:
        # Safe in any context (async, sync, threaded)
        loop = get_or_create_event_loop()
    """
    # First try to get the running loop (Python 3.10+ recommended way)
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        pass

    # Try to get an existing event loop for this thread
    thread_id = threading.get_ident()

    with _thread_loop_lock:
        if thread_id in _thread_event_loops:
            loop = _thread_event_loops[thread_id]
            if not loop.is_closed():
                return loop
            # Loop was closed, remove it
            del _thread_event_loops[thread_id]

        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _thread_event_loops[thread_id] = loop
        logger.debug(f"Created new event loop for thread {thread_id}")
        return loop


def cleanup_thread_event_loops():
    """Clean up event loops created for threads (call on shutdown)."""
    with _thread_loop_lock:
        for thread_id, loop in list(_thread_event_loops.items()):
            if not loop.is_closed():
                try:
                    loop.close()
                except Exception:
                    pass
        _thread_event_loops.clear()
    logger.info("Thread event loops cleaned up")


# =============================================================================
# Lazy Async Lock
# =============================================================================

class LazyAsyncLock:
    """
    A lazy-initialized asyncio.Lock that is safe to create in any context.

    asyncio.Lock() requires an event loop to exist, which fails when created
    in background threads (like ThreadPoolExecutor workers). LazyAsyncLock
    defers the actual Lock creation until it's first used in an async context.

    Usage:
        class MyClass:
            def __init__(self):
                # Safe to call from ANY thread, including ThreadPoolExecutor
                self._lock = LazyAsyncLock()

            async def protected_method(self):
                async with self._lock:
                    # This is where the Lock is actually created (in async context)
                    pass

    Thread Safety:
        - Creation: Thread-safe (uses no asyncio primitives)
        - First use: Thread-safe via threading.Lock
        - Subsequent use: Uses cached asyncio.Lock
    """

    __slots__ = ('_lock', '_sync_lock', '_initialized')

    def __init__(self):
        """Create a LazyAsyncLock. Safe to call from any thread."""
        self._lock: Optional[asyncio.Lock] = None
        self._sync_lock = threading.Lock()
        self._initialized = False

        # Register for tracking (weak reference, won't prevent garbage collection)
        _lazy_locks.add(self)

    def _ensure_lock(self) -> asyncio.Lock:
        """Get or create the underlying asyncio.Lock."""
        if self._lock is None:
            with self._sync_lock:
                if self._lock is None:
                    self._lock = asyncio.Lock()
                    self._initialized = True
        return self._lock

    async def acquire(self) -> bool:
        """Acquire the lock."""
        return await self._ensure_lock().acquire()

    def release(self) -> None:
        """Release the lock."""
        if self._lock is not None:
            self._lock.release()

    def locked(self) -> bool:
        """Check if the lock is held."""
        if self._lock is None:
            return False
        return self._lock.locked()

    async def __aenter__(self):
        """Async context manager entry."""
        await self._ensure_lock().acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        self._lock.release()
        return False

    def __repr__(self) -> str:
        status = "initialized" if self._initialized else "lazy"
        locked = self.locked() if self._initialized else "N/A"
        return f"<LazyAsyncLock status={status} locked={locked}>"


# =============================================================================
# Lazy Async Event
# =============================================================================

class LazyAsyncEvent:
    """
    A lazy-initialized asyncio.Event that is safe to create in any context.

    Similar to LazyAsyncLock, this defers Event creation until first use.
    """

    __slots__ = ('_event', '_sync_lock', '_initialized')

    def __init__(self):
        """Create a LazyAsyncEvent. Safe to call from any thread."""
        self._event: Optional[asyncio.Event] = None
        self._sync_lock = threading.Lock()
        self._initialized = False

    def _ensure_event(self) -> asyncio.Event:
        """Get or create the underlying asyncio.Event."""
        if self._event is None:
            with self._sync_lock:
                if self._event is None:
                    self._event = asyncio.Event()
                    self._initialized = True
        return self._event

    def is_set(self) -> bool:
        """Return True if the event is set."""
        if self._event is None:
            return False
        return self._event.is_set()

    def set(self) -> None:
        """Set the event."""
        self._ensure_event().set()

    def clear(self) -> None:
        """Clear the event."""
        if self._event is not None:
            self._event.clear()

    async def wait(self) -> bool:
        """Wait for the event to be set."""
        return await self._ensure_event().wait()

    def __repr__(self) -> str:
        status = "initialized" if self._initialized else "lazy"
        is_set = self.is_set() if self._initialized else "N/A"
        return f"<LazyAsyncEvent status={status} is_set={is_set}>"


# =============================================================================
# Thread-Safe Coroutine Runner
# =============================================================================

def run_coroutine_threadsafe_sync(
    coro,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    timeout: Optional[float] = None
):
    """
    Run a coroutine from a synchronous thread and wait for the result.

    This is useful when you need to call async code from a synchronous context
    in a background thread.

    Args:
        coro: The coroutine to run
        loop: Optional event loop (will get/create one if not provided)
        timeout: Optional timeout in seconds

    Returns:
        The result of the coroutine

    Raises:
        TimeoutError: If the timeout is exceeded
        RuntimeError: If the coroutine raises an exception
    """
    if loop is None:
        loop = get_or_create_event_loop()

    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)
