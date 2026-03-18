"""backend/core/cancellation_shield.py — Nuance 1: CancelledError propagation guard.

Problem
-------
``asyncio.CancelledError`` became a direct subclass of ``BaseException`` (not
``Exception``) in Python 3.8.  Component init code written for Python 3.7 may
use bare ``except:`` or ``except BaseException: pass`` patterns that swallow
``CancelledError``.  When ``asyncio.wait_for()`` cancels a component task the
component's init coroutine continues running — outliving the phase that
terminated it — creating an **orphaned async task** with no owner or cleanup.

Fix
---
* ``shield_cancellation(coro, component)`` — after *coro* returns normally,
  checks whether the outer ``asyncio.Task`` is still in a cancelled state
  and re-raises ``CancelledError`` if so.

  * Python 3.11+: uses ``task.cancelling()`` (PEP 682).
  * Python 3.9 / 3.10: best-effort concurrent watcher task.

* ``check_not_cancelled(component)`` — call in component ``finally`` blocks
  that use broad ``except`` clauses to prevent silent swallowing.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Awaitable, Optional, TypeVar

__all__ = [
    "CancellationShieldError",
    "shield_cancellation",
    "check_not_cancelled",
]

logger = logging.getLogger(__name__)

_HAVE_CANCELLING: bool = sys.version_info >= (3, 11)

T = TypeVar("T")


class CancellationShieldError(RuntimeError):
    """Raised when a component coroutine swallowed a ``CancelledError``."""

    def __init__(self, component: str) -> None:
        self.component = component
        super().__init__(
            f"[CancellationShield] '{component}' swallowed CancelledError — "
            "orphaned task re-cancelled"
        )


async def shield_cancellation(coro: Awaitable[T], component: str = "<unknown>") -> T:
    """Run *coro* and re-raise ``CancelledError`` if the outer task was
    cancelled during execution but *coro* swallowed the cancellation.

    Always propagates ``CancelledError`` raised directly by *coro*.
    """
    if _HAVE_CANCELLING:
        return await _shield_311(coro, component)
    return await _shield_39(coro, component)


async def _shield_311(coro: Awaitable[T], component: str) -> T:
    """Python 3.11+ path: use ``Task.cancelling()`` to detect swallowed cancel."""
    task: Optional[asyncio.Task] = asyncio.current_task()
    try:
        result: T = await coro  # type: ignore[assignment]
    except asyncio.CancelledError:
        raise
    else:
        # coro returned normally — check if the task is still being cancelled
        # (meaning coro swallowed the CancelledError).
        cancelling_fn = getattr(task, "cancelling", None) if task is not None else None
        if cancelling_fn is not None and cancelling_fn() > 0:
            err = CancellationShieldError(component)
            logger.error("%s", err)
            raise err from asyncio.CancelledError(f"propagated for '{component}'")
        return result


async def _shield_39(coro: Awaitable[T], component: str) -> T:
    """Python 3.9 / 3.10 path: best-effort out-of-band cancellation detection."""
    cancel_detected = asyncio.Event()
    inner_done = asyncio.Event()
    outer_task: Optional[asyncio.Task] = asyncio.current_task()

    async def _watcher() -> None:
        while not inner_done.is_set():
            await asyncio.sleep(0.02)
            if outer_task is not None and outer_task.cancelled():
                cancel_detected.set()
                return

    watcher = asyncio.ensure_future(_watcher())
    try:
        result: T = await coro  # type: ignore[assignment]
    except asyncio.CancelledError:
        cancel_detected.set()
        raise
    finally:
        inner_done.set()
        if not watcher.done():
            watcher.cancel()

    if cancel_detected.is_set():
        err = CancellationShieldError(component)
        logger.error("%s", err)
        raise err from asyncio.CancelledError(f"propagated for '{component}' (3.9)")

    return result


def check_not_cancelled(component: str = "<unknown>") -> None:
    """Re-raise ``CancelledError`` if the current task is being cancelled.

    Effective only on Python 3.11+.  No-op on 3.9 / 3.10 (best-effort).

    Usage in component code::

        try:
            await heavy_init()
        except BaseException:
            check_not_cancelled("neural_mesh")  # re-raises if cancelling
            # handle non-cancel exceptions
    """
    if not _HAVE_CANCELLING:
        return
    task = asyncio.current_task()
    if task is None:
        return
    cancelling_fn = getattr(task, "cancelling", None)
    if cancelling_fn is not None and cancelling_fn() > 0:
        raise asyncio.CancelledError(
            f"[CancellationShield] re-raised for '{component}'"
        )
