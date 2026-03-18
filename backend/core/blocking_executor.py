"""backend/core/blocking_executor.py — Nuance 7: blocking sync code in async init.

Problem
-------
Python's ``asyncio`` event loop runs on a single thread.  Component ``async
def`` init functions that call synchronous blocking code (CPU-bound model
weight loading, file I/O, synchronous imports) stall the event loop for the
duration of the blocking call.  The DMS watchdog loop (``asyncio.sleep(5)``)
cannot fire during a blocking import — making the watchdog **blind** for the
entire duration.  Two components that simultaneously perform blocking CPU work
also mean neither can be individually timed out by the async
``asyncio.wait_for()`` wrapper.

Fix
---
A shared ``ThreadPoolExecutor`` with a capped worker count (read from env so
nothing is hardcoded).  Two public utilities:

* ``run_blocking(fn, *args, **kwargs)`` — run a sync function in the executor;
  non-blocking to the event loop.  ``CancelledError`` from the outer task
  correctly cancels the *future* returned by ``run_in_executor``.
* ``@blocking_init`` — decorator that converts a sync init function to an async
  one using the shared executor.  The async wrapper preserves the original
  function's metadata via ``functools.wraps``.

Usage::

    @blocking_init
    def _load_weights(path: str) -> Model:
        # This is CPU/IO-bound; now safe to await in an async context.
        return torch.load(path)

    model = await _load_weights("/opt/models/7b.gguf")  # non-blocking

The executor is created lazily on first use (not at import time) so it binds to
the running event loop's thread pool on Python versions where that matters.
Worker count defaults to ``min(4, cpu_count)`` but is overridden by
``JARVIS_BLOCKING_EXECUTOR_WORKERS`` for resource-constrained environments.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable, Optional, TypeVar

__all__ = [
    "run_blocking",
    "blocking_init",
    "get_blocking_executor",
    "shutdown_executor",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Executor singleton
# ---------------------------------------------------------------------------

_g_executor: Optional[ThreadPoolExecutor] = None
_g_max_workers: Optional[int] = None  # set on first access


def _resolve_max_workers() -> int:
    """Resolve worker count from env or compute default (never hardcoded)."""
    env_val = os.getenv("JARVIS_BLOCKING_EXECUTOR_WORKERS")
    if env_val is not None:
        try:
            return max(1, int(env_val))
        except ValueError:
            logger.warning(
                "[BlockingExecutor] invalid JARVIS_BLOCKING_EXECUTOR_WORKERS=%r, "
                "using default",
                env_val,
            )
    cpu_count = os.cpu_count() or 1
    return min(4, cpu_count)


def get_blocking_executor() -> ThreadPoolExecutor:
    """Return (lazily creating) the process-wide blocking executor."""
    global _g_executor, _g_max_workers
    if _g_executor is None:
        _g_max_workers = _resolve_max_workers()
        _g_executor = ThreadPoolExecutor(
            max_workers=_g_max_workers,
            thread_name_prefix="jarvis-blocking",
        )
        logger.info(
            "[BlockingExecutor] created ThreadPoolExecutor(max_workers=%d)",
            _g_max_workers,
        )
    return _g_executor


def shutdown_executor(wait: bool = True) -> None:
    """Shut down the shared executor.  Call during process cleanup."""
    global _g_executor
    if _g_executor is not None:
        logger.info("[BlockingExecutor] shutting down (wait=%s)", wait)
        _g_executor.shutdown(wait=wait)
        _g_executor = None


# ---------------------------------------------------------------------------
# Core utility
# ---------------------------------------------------------------------------


async def run_blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run *fn* in the shared ``ThreadPoolExecutor``, non-blocking to the loop.

    ``CancelledError`` from the outer task correctly propagates — when the outer
    task is cancelled, the future is cancelled and ``CancelledError`` is raised
    here.  The executor thread itself continues to completion (Python cannot
    interrupt a running thread), but its result is discarded.

    Parameters
    ----------
    fn:
        Synchronous callable (e.g., ``torch.load``, a blocking file read).
    *args, **kwargs:
        Passed to *fn*.

    Returns
    -------
    The return value of ``fn(*args, **kwargs)``.

    Raises
    ------
    CancelledError
        If the outer asyncio.Task is cancelled while waiting.
    Any exception raised by *fn*.
    """
    loop = asyncio.get_running_loop()
    wrapped = functools.partial(fn, *args, **kwargs) if (args or kwargs) else fn
    return await loop.run_in_executor(get_blocking_executor(), wrapped)


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def blocking_init(fn: Callable[..., T]) -> Callable[..., Awaitable[T]]:
    """Decorator: convert a synchronous init function into an async one.

    The decorated function runs in the shared ``ThreadPoolExecutor`` when
    awaited, keeping the event loop free during blocking work.

    Usage::

        @blocking_init
        def load_model_weights(path: str) -> Model:
            return torch.load(path)

        # In an async context:
        model = await load_model_weights("/opt/models/7b.gguf")

    The ``@functools.wraps(fn)`` wrapper preserves ``__name__``, ``__doc__``,
    and ``__module__`` so the function appears normal in logs and tracebacks.
    """

    @functools.wraps(fn)
    async def _async_wrapper(*args: Any, **kwargs: Any) -> T:
        return await run_blocking(fn, *args, **kwargs)

    return _async_wrapper  # type: ignore[return-value]
