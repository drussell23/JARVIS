"""backend/core/shutdown_event.py — Nuance 4: eagerly initialised shutdown event.

Problem
-------
``ParallelInitializer._shutdown_event: Optional[asyncio.Event] = None`` is
created lazily — the first time the object needs it.  If an OS signal (SIGTERM,
SIGINT) fires between process start and the first access of ``_shutdown_event``,
the registered signal handler calls ``self._shutdown_event.set()`` on ``None``
and raises an ``AttributeError`` that is silently swallowed by the signal
delivery mechanism.  The process then fails to shut down cleanly.

Fix
---
Back the shutdown flag with a ``threading.Event`` that is created at **module
import time** (before any event loop, before any signal handler is registered).
``threading.Event.set()`` is safe to call from signal handlers, threads, and
any async context.

The ``ShutdownEvent`` class exposes:

* ``set()``       — signal-safe; callable from signal handlers and threads.
* ``is_set()``    — immediate non-blocking check.
* ``clear()``     — reset (used in DMS restart cycles).
* ``async wait(poll_interval_s)`` — asyncio-compatible wait without blocking
                                    the event loop (polls the threading.Event).
* ``wait_sync(timeout_s)``        — blocking wait (for pre-loop code paths).

The process-wide singleton ``_g_shutdown`` is created unconditionally when this
module is imported, so signal handlers just need::

    from backend.core.shutdown_event import get_shutdown_event
    signal.signal(signal.SIGTERM, lambda *_: get_shutdown_event().set())

No lazy initialisation, no ``Optional`` field, no race window.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

__all__ = [
    "ShutdownEvent",
    "get_shutdown_event",
]

logger = logging.getLogger(__name__)

# Default polling interval for async wait().  Can be overridden per-call.
_DEFAULT_POLL_S: float = float(os.getenv("JARVIS_SHUTDOWN_POLL_S", "0.05"))


class ShutdownEvent:
    """Process-wide shutdown flag backed by ``threading.Event``.

    Created once at module import time — completely safe to access from signal
    handlers, threads, or async code at any point in the process lifecycle.

    Usage::

        # In signal handler setup (before event loop):
        signal.signal(signal.SIGTERM, lambda *_: get_shutdown_event().set())

        # In async startup code:
        await get_shutdown_event().wait()  # suspends until shutdown signalled
    """

    def __init__(self) -> None:
        self._flag = threading.Event()

    def set(self) -> None:
        """Signal shutdown.  Thread-safe; callable from signal handlers."""
        if not self._flag.is_set():
            logger.info("[ShutdownEvent] shutdown signalled")
        self._flag.set()

    def is_set(self) -> bool:
        """Return ``True`` if shutdown has been signalled."""
        return self._flag.is_set()

    def clear(self) -> None:
        """Reset the flag.  Call between DMS restart cycles."""
        self._flag.clear()
        logger.debug("[ShutdownEvent] cleared for restart cycle")

    async def wait(self, poll_interval_s: float = _DEFAULT_POLL_S) -> None:
        """Async-compatible wait.  Suspends until ``set()`` is called.

        Polls the underlying ``threading.Event`` at *poll_interval_s* intervals
        using ``asyncio.sleep()`` so the event loop is never blocked.

        Parameters
        ----------
        poll_interval_s:
            How often to poll.  Smaller values = lower latency, higher CPU.
            Default comes from ``JARVIS_SHUTDOWN_POLL_S`` env (0.05 s).
        """
        while not self._flag.is_set():
            await asyncio.sleep(poll_interval_s)

    def wait_sync(self, timeout_s: Optional[float] = None) -> bool:
        """Blocking wait (for pre-event-loop code paths).

        Parameters
        ----------
        timeout_s:
            Maximum seconds to wait.  ``None`` = wait forever.

        Returns
        -------
        ``True`` if the event was set, ``False`` if the timeout expired.
        """
        return self._flag.wait(timeout=timeout_s)


# ---------------------------------------------------------------------------
# Module-level singleton — created at import time, never None
# ---------------------------------------------------------------------------

_g_shutdown: ShutdownEvent = ShutdownEvent()


def get_shutdown_event() -> ShutdownEvent:
    """Return the process-wide ``ShutdownEvent``.

    This function always returns the same object.  It is safe to call before
    the asyncio event loop is running (e.g., from signal handler setup code).
    """
    return _g_shutdown
