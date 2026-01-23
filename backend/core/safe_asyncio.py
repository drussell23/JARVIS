"""
Safe Asyncio Utilities v1.0 - Defensive Asyncio Access

This module provides guaranteed-safe access to asyncio functionality,
preventing "local variable 'asyncio' referenced before assignment" errors
that can occur from shadowing or conditional imports.

Usage:
    from core.safe_asyncio import safe_asyncio, get_event_loop_safe, sleep_safe

    # Instead of: import asyncio; asyncio.sleep(1)
    await sleep_safe(1)

    # Instead of: loop = asyncio.get_event_loop()
    loop = get_event_loop_safe()

Author: JARVIS Development Team
Version: 1.0.0 (January 2026)
"""

from __future__ import annotations

# Guaranteed import - this happens at module load time, before any function execution
import asyncio as _asyncio_module
import logging
from typing import Any, Coroutine, Optional, TypeVar, Union

logger = logging.getLogger(__name__)

# Type variable for generic coroutine return types
T = TypeVar('T')

# Store the module reference to prevent any possible shadowing
_ASYNCIO = _asyncio_module


def get_asyncio_module():
    """
    Get the asyncio module safely.

    This function provides guaranteed access to the asyncio module,
    regardless of any local variable shadowing that might occur.

    Returns:
        The asyncio module
    """
    return _ASYNCIO


# Alias for convenience
safe_asyncio = _ASYNCIO


def get_event_loop_safe() -> _asyncio_module.AbstractEventLoop:
    """
    Get the current event loop safely.

    Returns:
        The current event loop
    """
    try:
        return _ASYNCIO.get_running_loop()
    except RuntimeError:
        # No running loop, try to get or create one
        try:
            return _ASYNCIO.get_event_loop()
        except RuntimeError:
            # Create a new event loop
            loop = _ASYNCIO.new_event_loop()
            _ASYNCIO.set_event_loop(loop)
            return loop


def new_event_loop_safe() -> _asyncio_module.AbstractEventLoop:
    """Create a new event loop safely."""
    return _ASYNCIO.new_event_loop()


def set_event_loop_safe(loop: Optional[_asyncio_module.AbstractEventLoop]) -> None:
    """Set the current event loop safely."""
    _ASYNCIO.set_event_loop(loop)


async def sleep_safe(delay: float, result: Any = None) -> Any:
    """
    Sleep safely using asyncio.

    Args:
        delay: Time to sleep in seconds
        result: Optional result to return after sleeping

    Returns:
        The result parameter
    """
    return await _ASYNCIO.sleep(delay, result)


def create_task_safe(
    coro: Coroutine[Any, Any, T],
    *,
    name: Optional[str] = None
) -> _asyncio_module.Task[T]:
    """
    Create a task safely.

    Args:
        coro: The coroutine to wrap in a task
        name: Optional name for the task

    Returns:
        The created task
    """
    return _ASYNCIO.create_task(coro, name=name)


async def gather_safe(*coros_or_futures, return_exceptions: bool = False):
    """
    Gather coroutines or futures safely.

    Args:
        coros_or_futures: Coroutines or futures to gather
        return_exceptions: If True, exceptions are returned instead of raised

    Returns:
        List of results
    """
    return await _ASYNCIO.gather(*coros_or_futures, return_exceptions=return_exceptions)


async def wait_for_safe(
    fut: Union[Coroutine[Any, Any, T], _asyncio_module.Future[T]],
    timeout: Optional[float]
) -> T:
    """
    Wait for a future with timeout safely.

    Args:
        fut: The future or coroutine to wait for
        timeout: Timeout in seconds (None for no timeout)

    Returns:
        The result of the future

    Raises:
        asyncio.TimeoutError: If timeout is exceeded
    """
    return await _ASYNCIO.wait_for(fut, timeout=timeout)


async def shield_safe(arg: Union[Coroutine[Any, Any, T], _asyncio_module.Future[T]]) -> T:
    """
    Shield a coroutine from cancellation safely.

    Args:
        arg: The coroutine or future to shield

    Returns:
        The result of the shielded operation
    """
    return await _ASYNCIO.shield(arg)


def run_safe(main: Coroutine[Any, Any, T], *, debug: bool = False) -> T:
    """
    Run a coroutine in a new event loop safely.

    Args:
        main: The coroutine to run
        debug: If True, enable debug mode

    Returns:
        The result of the coroutine
    """
    return _ASYNCIO.run(main, debug=debug)


def iscoroutine_safe(obj: Any) -> bool:
    """Check if object is a coroutine safely."""
    return _ASYNCIO.iscoroutine(obj)


def iscoroutinefunction_safe(obj: Any) -> bool:
    """Check if object is a coroutine function safely."""
    return _ASYNCIO.iscoroutinefunction(obj)


def all_tasks_safe(loop: Optional[_asyncio_module.AbstractEventLoop] = None):
    """Get all tasks for an event loop safely."""
    return _ASYNCIO.all_tasks(loop)


def current_task_safe(loop: Optional[_asyncio_module.AbstractEventLoop] = None):
    """Get the current task safely."""
    return _ASYNCIO.current_task(loop)


class Event:
    """Safe wrapper around asyncio.Event."""

    def __init__(self):
        self._event = _ASYNCIO.Event()

    def set(self):
        self._event.set()

    def clear(self):
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self):
        await self._event.wait()


class Lock:
    """Safe wrapper around asyncio.Lock."""

    def __init__(self):
        self._lock = _ASYNCIO.Lock()

    async def acquire(self):
        await self._lock.acquire()

    def release(self):
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self):
        await self._lock.acquire()
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb):
        self._lock.release()


class Semaphore:
    """Safe wrapper around asyncio.Semaphore."""

    def __init__(self, value: int = 1):
        self._semaphore = _ASYNCIO.Semaphore(value)

    async def acquire(self):
        await self._semaphore.acquire()

    def release(self):
        self._semaphore.release()

    def locked(self) -> bool:
        return self._semaphore.locked()

    async def __aenter__(self):
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb):
        self._semaphore.release()


class Queue:
    """Safe wrapper around asyncio.Queue."""

    def __init__(self, maxsize: int = 0):
        self._queue = _ASYNCIO.Queue(maxsize=maxsize)

    async def put(self, item: Any):
        await self._queue.put(item)

    def put_nowait(self, item: Any):
        self._queue.put_nowait(item)

    async def get(self) -> Any:
        return await self._queue.get()

    def get_nowait(self) -> Any:
        return self._queue.get_nowait()

    def task_done(self):
        self._queue.task_done()

    async def join(self):
        await self._queue.join()

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    def full(self) -> bool:
        return self._queue.full()


# Exception aliases for convenience
TimeoutError = _ASYNCIO.TimeoutError
CancelledError = _ASYNCIO.CancelledError


def ensure_asyncio_imported() -> bool:
    """
    Verify that asyncio is properly imported and available.

    This can be called at the start of any monitor function to ensure
    asyncio is available before any operations.

    Returns:
        True if asyncio is properly available
    """
    try:
        _ = _ASYNCIO.get_event_loop_policy()
        return True
    except Exception as e:
        logger.error(f"[safe_asyncio] asyncio not properly available: {e}")
        return False


# Module-level verification at import time
if not ensure_asyncio_imported():
    logger.critical("[safe_asyncio] Failed to verify asyncio availability!")
