"""IdleWatchdog — fires an asyncio.Event after N seconds of no activity.

The Battle Test harness pokes the watchdog on every operation completion.
If no poke arrives within `timeout_s` seconds the `idle_event` is set,
which can be awaited alongside budget_event and shutdown_event so that the
first to fire stops the session.

The watchdog also tracks *why* it fired (genuine idle vs. stale ops) so
the harness can produce actionable stop-reason diagnostics.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class StaleOpInfo:
    """Forensic snapshot of an operation that exceeded the staleness threshold."""
    op_id: str
    phase: str
    elapsed_s: float
    last_transition_utc: str


@dataclass
class WatchdogDiagnostics:
    """Diagnostics attached when the idle event fires."""
    reason: str  # "genuine_idle" | "all_ops_stale"
    stale_ops: list = field(default_factory=list)
    total_pokes: int = 0
    seconds_since_last_poke: float = 0.0


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
        print(watchdog.diagnostics)  # why it fired
    """

    def __init__(self, timeout_s: float = 600.0) -> None:
        self._timeout_s = timeout_s
        self._last_poke: float = time.monotonic()
        self._poke_count: int = 0
        self.idle_event: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.diagnostics: Optional[WatchdogDiagnostics] = None
        # HIBERNATION_MODE: while frozen, _watch() does not count elapsed
        # time and fire_stale() is a no-op. unfreeze() resets the clock so
        # a long outage does not immediately trigger idle on wake.
        self._frozen: bool = False
        self._freeze_count: int = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def poke_count(self) -> int:
        """Number of times poke() has been called."""
        return self._poke_count

    @property
    def is_frozen(self) -> bool:
        """True if freeze() has been called and unfreeze() has not yet fired."""
        return self._frozen

    @property
    def freeze_count(self) -> int:
        """Number of freeze() transitions — observability for hibernation cycles."""
        return self._freeze_count

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def poke(self) -> None:
        """Reset the idle timer and increment the poke counter."""
        self._last_poke = time.monotonic()
        self._poke_count += 1

    def fire_stale(self, stale_ops: list) -> None:
        """Immediately fire the idle event due to stale operations.

        Called by the ActivityMonitor when all in-flight ops have exceeded
        the staleness threshold — the system is alive but not progressing.

        No-op while frozen: during HIBERNATION the staleness signal is
        expected (nothing is progressing because providers are down) and
        must not tear the session down.
        """
        if self._frozen:
            logger.debug(
                "[IdleWatchdog] fire_stale suppressed — frozen (hibernation)"
            )
            return
        self.diagnostics = WatchdogDiagnostics(
            reason="all_ops_stale",
            stale_ops=stale_ops,
            total_pokes=self._poke_count,
            seconds_since_last_poke=time.monotonic() - self._last_poke,
        )
        logger.warning(
            "[IdleWatchdog] Firing: all %d in-flight ops are stale (threshold exceeded)",
            len(stale_ops),
        )
        self.idle_event.set()

    def freeze(self, *, reason: str = "") -> bool:
        """Freeze the idle clock — used during HIBERNATION_MODE outages.

        While frozen:
          - _watch() does not accumulate elapsed time against _last_poke
          - fire_stale() is a no-op
          - idle_event is never set

        Idempotent. Returns True on transition, False if already frozen.
        """
        if self._frozen:
            return False
        self._frozen = True
        self._freeze_count += 1
        logger.info(
            "[IdleWatchdog] FROZEN (reason=%r, poke_count=%d)",
            reason or "unspecified",
            self._poke_count,
        )
        return True

    def unfreeze(self, *, reason: str = "") -> bool:
        """Release the idle clock and reset it to the current monotonic time.

        The reset is critical: without it, a 30-minute hibernation would
        fire idle immediately on wake because elapsed time > timeout_s.
        After unfreeze the organism gets a full fresh idle window.

        Idempotent. Returns True on transition, False if not frozen.
        """
        if not self._frozen:
            return False
        self._frozen = False
        self._last_poke = time.monotonic()
        logger.info(
            "[IdleWatchdog] UNFROZEN — idle clock reset (reason=%r)",
            reason or "unspecified",
        )
        return True

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
                # HIBERNATION_MODE: while frozen the clock does not advance.
                # Poll at 100ms so unfreeze() is observed within a tick — the
                # sleep is cheap and keeps the idle window from silently
                # over-running when we wake.
                if self._frozen:
                    await asyncio.sleep(0.1)
                    continue
                elapsed = time.monotonic() - self._last_poke
                remaining = self._timeout_s - elapsed
                if remaining <= 0:
                    self.diagnostics = WatchdogDiagnostics(
                        reason="genuine_idle",
                        total_pokes=self._poke_count,
                        seconds_since_last_poke=elapsed,
                    )
                    self.idle_event.set()
                    return
                await asyncio.sleep(min(remaining, 1.0))
        except asyncio.CancelledError:
            # Cancelled by stop() — do not fire the event.
            pass
