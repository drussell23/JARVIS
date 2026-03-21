# backend/core/ouroboros/governance/user_signal_bus.py
"""UserSignalBus — asyncio.Event wrapper for user-initiated stop signals.

GAP 6: provides the missing link between voice/CLI input and the
GovernedLoopService orchestrator race.  One bus per GLS instance.
"""
from __future__ import annotations

import asyncio


class UserSignalBus:
    """Thread-safe (event-loop-safe) stop signal for in-flight operations.

    Usage:
        bus = UserSignalBus()
        # In voice sensor / CLI handler:
        bus.request_stop()
        # In submit() race:
        await asyncio.wait([op_task, asyncio.create_task(bus.wait_for_stop())], ...)
        # After stop detected:
        bus.reset()  # clear for next op
    """

    def __init__(self) -> None:
        self._stop: asyncio.Event = asyncio.Event()

    def request_stop(self) -> None:
        """Signal all waiters that a stop has been requested."""
        self._stop.set()

    def is_stop_requested(self) -> bool:
        """Non-blocking check — True if stop has been requested since last reset."""
        return self._stop.is_set()

    async def wait_for_stop(self) -> None:
        """Await until request_stop() is called."""
        await self._stop.wait()

    def reset(self) -> None:
        """Clear the stop signal so future operations are not immediately stopped."""
        self._stop.clear()
