"""
v77.0: Timeout Wrapper - Gap #8
================================

Comprehensive timeout management:
- Configurable per-operation timeouts
- Cascading timeouts for nested operations
- Timeout inheritance
- Progress-aware timeouts
- Graceful timeout handling

Author: JARVIS v77.0
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, Optional, TypeVar

logger = logging.getLogger(__name__)


# Python 3.9 compatible async timeout context manager
class _AsyncTimeoutCtx:
    """Simple async timeout context manager for Python 3.9 compatibility."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self._task: Optional[asyncio.Task] = None
        self._timeout_handle: Optional[asyncio.TimerHandle] = None

    async def __aenter__(self):
        self._task = asyncio.current_task()
        if self._task is not None and self.seconds > 0:
            loop = asyncio.get_running_loop()
            self._timeout_handle = loop.call_later(
                self.seconds,
                self._cancel_task
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._timeout_handle is not None:
            self._timeout_handle.cancel()
            self._timeout_handle = None

        if exc_type is asyncio.CancelledError and self._task is not None:
            raise asyncio.TimeoutError()

        return False

    def _cancel_task(self):
        if self._task is not None and not self._task.done():
            self._task.cancel()


def _timeout_ctx(seconds: float) -> _AsyncTimeoutCtx:
    """Create a Python 3.9 compatible async timeout context manager."""
    return _AsyncTimeoutCtx(seconds)

T = TypeVar("T")


@dataclass
class TimeoutConfig:
    """Configuration for timeout behavior."""
    default_timeout: float = 30.0
    max_timeout: float = 600.0  # 10 minutes
    min_timeout: float = 1.0
    grace_period: float = 5.0  # Extra time for cleanup
    cascade_factor: float = 0.9  # Nested timeout = parent * factor
    enable_progress_extension: bool = True
    progress_extension_threshold: float = 0.1  # Extend if progress > 10%
    extension_amount: float = 10.0  # Seconds to extend


@dataclass
class TimeoutContext:
    """Context for an active timeout."""
    operation_name: str
    timeout_seconds: float
    started_at: float = field(default_factory=time.time)
    extended_count: int = 0
    progress: float = 0.0
    parent_context: Optional["TimeoutContext"] = None

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def remaining(self) -> float:
        return max(0, self.timeout_seconds - self.elapsed)

    @property
    def is_expired(self) -> bool:
        return self.remaining <= 0


class TimeoutWrapper:
    """
    Comprehensive timeout management system.

    Features:
    - Per-operation configurable timeouts
    - Nested/cascading timeouts
    - Progress-aware extensions
    - Graceful cleanup on timeout
    """

    # Thread-local context stack
    _context_stack: contextvars.ContextVar[list] = contextvars.ContextVar(
        "timeout_context_stack",
        default=[]
    )

    def __init__(self, config: Optional[TimeoutConfig] = None):
        self.config = config or TimeoutConfig()
        self._active_timeouts: Dict[str, TimeoutContext] = {}

    @asynccontextmanager
    async def timeout(
        self,
        seconds: Optional[float] = None,
        operation_name: str = "operation",
        cleanup: Optional[Callable[[], Coroutine]] = None,
    ):
        """
        Context manager for timeout-protected operations.

        Usage:
            async with wrapper.timeout(30, "my_operation"):
                await some_async_work()
        """
        # Determine timeout value
        timeout_seconds = self._calculate_timeout(seconds)

        # Create context
        parent = self._get_current_context()
        ctx = TimeoutContext(
            operation_name=operation_name,
            timeout_seconds=timeout_seconds,
            parent_context=parent,
        )

        # Push to stack
        stack = self._context_stack.get().copy()
        stack.append(ctx)
        token = self._context_stack.set(stack)

        self._active_timeouts[operation_name] = ctx

        try:
            async with _timeout_ctx(timeout_seconds):  # Python 3.9 compatible
                yield ctx

        except asyncio.TimeoutError:
            logger.warning(
                f"[TimeoutWrapper] {operation_name} timed out after "
                f"{ctx.elapsed:.2f}s (limit: {timeout_seconds}s)"
            )

            # Run cleanup if provided
            if cleanup:
                try:
                    # Give cleanup a grace period
                    async with _timeout_ctx(self.config.grace_period):  # Python 3.9 compatible
                        await cleanup()
                except asyncio.TimeoutError:
                    logger.error(f"[TimeoutWrapper] Cleanup for {operation_name} also timed out")
                except Exception as e:
                    logger.error(f"[TimeoutWrapper] Cleanup error: {e}")

            raise TimeoutError(f"{operation_name} timed out after {timeout_seconds}s")

        finally:
            # Pop from stack
            stack = self._context_stack.get().copy()
            if stack and stack[-1] is ctx:
                stack.pop()
            self._context_stack.set(stack)

            self._active_timeouts.pop(operation_name, None)

    async def extend_timeout(
        self,
        operation_name: str,
        additional_seconds: Optional[float] = None,
    ) -> bool:
        """
        Extend an active timeout.

        Returns True if extended successfully.
        """
        if operation_name not in self._active_timeouts:
            return False

        ctx = self._active_timeouts[operation_name]

        # Calculate extension
        extension = additional_seconds or self.config.extension_amount

        # Check if we can extend
        new_timeout = ctx.timeout_seconds + extension
        if new_timeout > self.config.max_timeout:
            logger.warning(f"[TimeoutWrapper] Cannot extend {operation_name} beyond max timeout")
            return False

        ctx.timeout_seconds = new_timeout
        ctx.extended_count += 1

        logger.debug(f"[TimeoutWrapper] Extended {operation_name} by {extension}s")
        return True

    async def update_progress(self, operation_name: str, progress: float) -> None:
        """
        Update progress for an operation.

        May trigger automatic timeout extension.
        """
        if operation_name not in self._active_timeouts:
            return

        ctx = self._active_timeouts[operation_name]
        old_progress = ctx.progress
        ctx.progress = max(0.0, min(1.0, progress))

        # Check for progress-based extension
        if self.config.enable_progress_extension:
            progress_delta = ctx.progress - old_progress
            if progress_delta >= self.config.progress_extension_threshold:
                if ctx.remaining < self.config.extension_amount:
                    await self.extend_timeout(operation_name)

    def get_remaining_timeout(self, operation_name: Optional[str] = None) -> float:
        """Get remaining timeout for an operation or current context."""
        if operation_name:
            ctx = self._active_timeouts.get(operation_name)
        else:
            ctx = self._get_current_context()

        if ctx is None:
            return self.config.default_timeout

        return ctx.remaining

    def get_cascaded_timeout(self, requested: Optional[float] = None) -> float:
        """
        Get timeout for nested operation, respecting parent.

        Nested timeouts should be <= parent remaining time.
        """
        parent = self._get_current_context()

        if parent is None:
            return requested or self.config.default_timeout

        # Use cascade factor of parent's remaining time
        max_nested = parent.remaining * self.config.cascade_factor

        if requested is None:
            return max_nested
        else:
            return min(requested, max_nested)

    def _calculate_timeout(self, requested: Optional[float]) -> float:
        """Calculate actual timeout value to use."""
        if requested is None:
            timeout = self.config.default_timeout
        else:
            timeout = requested

        # Clamp to bounds
        timeout = max(self.config.min_timeout, min(timeout, self.config.max_timeout))

        # Consider parent context
        parent = self._get_current_context()
        if parent:
            # Don't exceed parent's remaining time
            timeout = min(timeout, parent.remaining * self.config.cascade_factor)

        return timeout

    def _get_current_context(self) -> Optional[TimeoutContext]:
        """Get the current timeout context from stack."""
        stack = self._context_stack.get()
        return stack[-1] if stack else None


# Global instance
_timeout_wrapper = TimeoutWrapper()


def timeout(
    seconds: Optional[float] = None,
    operation_name: Optional[str] = None,
):
    """
    Decorator to add timeout to async functions.

    Usage:
        @timeout(30)
        async def my_function():
            ...
    """
    def decorator(func: Callable[..., Coroutine]) -> Callable[..., Coroutine]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            name = operation_name or func.__name__
            async with _timeout_wrapper.timeout(seconds, name):
                return await func(*args, **kwargs)
        return wrapper
    return decorator


@asynccontextmanager
async def with_timeout(
    seconds: Optional[float] = None,
    operation_name: str = "operation",
    cleanup: Optional[Callable[[], Coroutine]] = None,
):
    """
    Convenience function for timeout context manager.

    Usage:
        async with with_timeout(30, "my_operation"):
            await do_work()
    """
    async with _timeout_wrapper.timeout(seconds, operation_name, cleanup):
        yield


def get_timeout_wrapper() -> TimeoutWrapper:
    """Get global timeout wrapper instance."""
    return _timeout_wrapper
