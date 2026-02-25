"""Context-propagating async task creation.

Wraps asyncio.create_task() to copy the caller's contextvars snapshot
into the child task, preserving CorrelationContext, TraceEnvelope, and
any other ContextVar-based state across task boundaries.

Usage:
    from backend.core.context_task import create_traced_task

    task = create_traced_task(some_coro(), name="my-task")
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)


def create_traced_task(
    coro: Coroutine[Any, Any, Any],
    *,
    name: Optional[str] = None,
    on_error: Optional[Callable[[str, BaseException], None]] = None,
) -> asyncio.Task:
    """Create an asyncio task that inherits the caller's contextvars.

    Args:
        coro: The coroutine to schedule.
        name: Optional task name for debugging.
        on_error: Optional callback(name, exception) invoked on failure.

    Returns:
        The created asyncio.Task.
    """
    ctx = contextvars.copy_context()
    task_name = name or getattr(coro, "__qualname__", "anonymous")

    # Run the coroutine inside the copied context
    task = asyncio.ensure_future(ctx.run(_create_awaitable, coro))

    try:
        task.set_name(task_name)
    except AttributeError:
        pass  # Python < 3.8

    if on_error is not None:
        def _done_cb(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                try:
                    on_error(task_name, exc)
                except Exception:
                    logger.debug("on_error callback failed", exc_info=True)

        task.add_done_callback(_done_cb)

    return task


async def _create_awaitable(coro: Coroutine[Any, Any, Any]) -> Any:
    """Thin async wrapper so ctx.run() can schedule the coroutine."""
    return await coro
