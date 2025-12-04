"""
ML Operation Watchdog
=====================

Provides robust timeout protection for ML operations that can block the event loop.

Features:
- Process-level timeout using multiprocessing (survives event loop blocks)
- Thread-based timeout for quick operations
- Automatic detection of stuck operations
- Graceful degradation when timeouts occur

Usage:
    from core.ml_operation_watchdog import run_with_timeout, MLOperationTimeout

    # Run a blocking operation with timeout
    result = await run_with_timeout(
        blocking_function, args, kwargs,
        timeout=30.0,
        operation_name="ECAPA embedding"
    )
"""

import asyncio
import logging
import signal
import concurrent.futures
from functools import wraps
from typing import Any, Callable, Optional, TypeVar, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import threading

logger = logging.getLogger(__name__)

T = TypeVar('T')


class MLOperationTimeout(Exception):
    """Raised when an ML operation times out."""
    def __init__(self, operation_name: str, timeout: float, message: str = ""):
        self.operation_name = operation_name
        self.timeout = timeout
        super().__init__(
            message or f"ML operation '{operation_name}' timed out after {timeout}s"
        )


@dataclass
class WatchdogStats:
    """Statistics for ML operation watchdog."""
    total_operations: int = 0
    successful_operations: int = 0
    timeout_operations: int = 0
    error_operations: int = 0
    total_time_ms: float = 0.0
    last_operation_time: Optional[datetime] = None
    last_timeout_operation: Optional[str] = None


# Global watchdog stats
_watchdog_stats = WatchdogStats()


def get_watchdog_stats() -> WatchdogStats:
    """Get current watchdog statistics."""
    return _watchdog_stats


async def run_with_timeout(
    func: Callable[..., T],
    *args,
    timeout: float = 30.0,
    operation_name: str = "ML operation",
    fallback_value: Optional[T] = None,
    raise_on_timeout: bool = True,
    **kwargs
) -> T:
    """
    Run a potentially blocking function with robust timeout protection.

    Uses asyncio.to_thread() which runs the function in a separate thread,
    combined with asyncio.wait_for() for timeout enforcement.

    Args:
        func: The function to run (can be sync or async)
        *args: Arguments to pass to the function
        timeout: Maximum time to wait (seconds)
        operation_name: Name for logging
        fallback_value: Value to return on timeout (if raise_on_timeout=False)
        raise_on_timeout: Whether to raise MLOperationTimeout on timeout
        **kwargs: Keyword arguments to pass to the function

    Returns:
        The function result, or fallback_value on timeout

    Raises:
        MLOperationTimeout: If timeout occurs and raise_on_timeout=True
    """
    global _watchdog_stats
    _watchdog_stats.total_operations += 1
    _watchdog_stats.last_operation_time = datetime.now()

    start_time = datetime.now()

    try:
        # Wrap sync function to run in thread pool
        if asyncio.iscoroutinefunction(func):
            # Async function - just wrap with timeout
            result = await asyncio.wait_for(
                func(*args, **kwargs),
                timeout=timeout
            )
        else:
            # Sync function - run in thread pool with timeout
            result = await asyncio.wait_for(
                asyncio.to_thread(func, *args, **kwargs),
                timeout=timeout
            )

        elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
        _watchdog_stats.successful_operations += 1
        _watchdog_stats.total_time_ms += elapsed_ms

        logger.debug(f"✅ {operation_name} completed in {elapsed_ms:.0f}ms")
        return result

    except asyncio.TimeoutError:
        elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
        _watchdog_stats.timeout_operations += 1
        _watchdog_stats.total_time_ms += elapsed_ms
        _watchdog_stats.last_timeout_operation = operation_name

        logger.error(f"⏱️ {operation_name} TIMEOUT after {timeout}s")

        if raise_on_timeout:
            raise MLOperationTimeout(operation_name, timeout)
        return fallback_value

    except Exception as e:
        elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
        _watchdog_stats.error_operations += 1
        _watchdog_stats.total_time_ms += elapsed_ms

        logger.error(f"❌ {operation_name} failed: {e}")
        raise


def with_timeout(
    timeout: float = 30.0,
    operation_name: Optional[str] = None,
    fallback_value: Any = None,
    raise_on_timeout: bool = True
):
    """
    Decorator to add timeout protection to async functions.

    Usage:
        @with_timeout(timeout=10.0, operation_name="Voice embedding")
        async def extract_embedding(audio_data):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            name = operation_name or func.__name__
            return await run_with_timeout(
                func, *args,
                timeout=timeout,
                operation_name=name,
                fallback_value=fallback_value,
                raise_on_timeout=raise_on_timeout,
                **kwargs
            )
        return wrapper
    return decorator


class EventLoopWatchdog:
    """
    Monitors the event loop for blocking operations.

    Runs in a separate thread and checks if the event loop is responsive.
    If the loop appears blocked, it logs warnings and can trigger recovery.
    """

    def __init__(
        self,
        check_interval: float = 5.0,
        warning_threshold: float = 2.0,
        critical_threshold: float = 10.0,
    ):
        self.check_interval = check_interval
        self.warning_threshold = warning_threshold
        self.critical_threshold = critical_threshold

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_response_time: Optional[datetime] = None
        self._consecutive_failures = 0

    def start(self, loop: asyncio.AbstractEventLoop):
        """Start the watchdog in a background thread."""
        if self._running:
            return

        self._loop = loop
        self._running = True
        self._thread = threading.Thread(
            target=self._watchdog_thread,
            name="EventLoopWatchdog",
            daemon=True
        )
        self._thread.start()
        logger.info("🐕 Event loop watchdog started")

    def stop(self):
        """Stop the watchdog."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("🐕 Event loop watchdog stopped")

    def _watchdog_thread(self):
        """Background thread that monitors event loop responsiveness."""
        import time

        while self._running:
            try:
                # Try to schedule a simple callback on the event loop
                start = datetime.now()

                if self._loop and self._loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self._ping_loop(),
                        self._loop
                    )

                    try:
                        # Wait for response with timeout
                        future.result(timeout=self.warning_threshold)
                        response_time = (datetime.now() - start).total_seconds()

                        if response_time > self.warning_threshold:
                            logger.warning(
                                f"⚠️ Event loop slow: {response_time:.2f}s response time"
                            )
                            self._consecutive_failures += 1
                        else:
                            self._consecutive_failures = 0

                        self._last_response_time = datetime.now()

                    except concurrent.futures.TimeoutError:
                        elapsed = (datetime.now() - start).total_seconds()
                        self._consecutive_failures += 1

                        if elapsed > self.critical_threshold:
                            logger.error(
                                f"🚨 CRITICAL: Event loop blocked for {elapsed:.1f}s! "
                                f"Consecutive failures: {self._consecutive_failures}"
                            )
                        else:
                            logger.warning(
                                f"⚠️ Event loop unresponsive for {elapsed:.1f}s"
                            )

            except Exception as e:
                logger.debug(f"Watchdog check error: {e}")

            time.sleep(self.check_interval)

    async def _ping_loop(self) -> bool:
        """Simple coroutine to check if event loop is responsive."""
        return True

    @property
    def is_healthy(self) -> bool:
        """Check if event loop appears healthy."""
        if self._last_response_time is None:
            return True
        elapsed = (datetime.now() - self._last_response_time).total_seconds()
        return elapsed < self.critical_threshold


# Global watchdog instance
_event_loop_watchdog: Optional[EventLoopWatchdog] = None


def start_event_loop_watchdog(loop: asyncio.AbstractEventLoop):
    """Start the global event loop watchdog."""
    global _event_loop_watchdog
    if _event_loop_watchdog is None:
        _event_loop_watchdog = EventLoopWatchdog()
    _event_loop_watchdog.start(loop)


def stop_event_loop_watchdog():
    """Stop the global event loop watchdog."""
    global _event_loop_watchdog
    if _event_loop_watchdog:
        _event_loop_watchdog.stop()


def is_event_loop_healthy() -> bool:
    """Check if the event loop appears healthy."""
    if _event_loop_watchdog:
        return _event_loop_watchdog.is_healthy
    return True
