"""IdleWatchdog — fires an asyncio.Event after N seconds of no activity.

The Battle Test harness pokes the watchdog on every operation completion.
If no poke arrives within `timeout_s` seconds the `idle_event` is set,
which can be awaited alongside budget_event and shutdown_event so that the
first to fire stops the session.
"""
from __future__ import annotations

import asyncio
import time


class IdleWatchdog:
    """Fires `idle_event` after `timeout_s` seconds of inactivity.

    Usage::

        watchdog = IdleWatchdog(timeout_s=600.0)
        await watchdog.start()

        # … in harness loop:
        watchdog.poke()

        # stop without firing:
        watchdog.stop()

        # or wait for idle:
        await watchdog.idle_event.wait()
    """

    def __init__(self, timeout_s: float = 600.0) -> None:
        self._timeout_s = timeout_s
        self._last_poke: float = time.monotonic()
        self._poke_count: int = 0
        self.idle_event: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def poke_count(self) -> int:
        """Number of times poke() has been called."""
        return self._poke_count

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def poke(self) -> None:
        """Reset the idle timer and increment the poke counter."""
        self._last_poke = time.monotonic()
        self._poke_count += 1

    async def start(self) -> None:
        """Start the background watchdog task."""
        self._last_poke = time.monotonic()
        self._task = asyncio.ensure_future(self._watch())

    def stop(self) -> None:
        """Cancel the background task without firing the idle event."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            self._task = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _watch(self) -> None:
        """Internal loop: check elapsed time, sleep, fire event when idle."""
        try:
            while True:
                elapsed = time.monotonic() - self._last_poke
                remaining = self._timeout_s - elapsed
                if remaining <= 0:
                    self.idle_event.set()
                    return
                await asyncio.sleep(min(remaining, 1.0))
        except asyncio.CancelledError:
            # Cancelled by stop() — do not fire the event.
            pass
