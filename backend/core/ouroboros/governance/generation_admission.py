"""Foreground reservation on the local generation lane.

Why not preemption
------------------
A request already running in llama.cpp cannot be checkpointed and resumed:
stopping it discards its tokens, and the op's retry ladder would re-run
GENERATE -- the ghosting and pipeline-state churn this must avoid. Nor is
there VRAM to "yield": J-Prime allocates every slot's KV cache at launch.
What CAN be scheduled is ADMISSION: who gets the next free slot.

The defect this fixes
---------------------
A /session turn is a tool loop -- up to ``JARVIS_SESSION_MAX_ROUNDS``
generations. Its first request already gets the next free slot (llama.cpp
serves queued requests in arrival order). But when that round ends, its slot
frees, a background generation is admitted into it, and round 2 waits for a
whole background generation -- minutes on the 30B -- before the operator sees
the next step. So:

  * while any foreground turn is active, BACKGROUND admission stops at
    ``capacity - 1`` in-flight generations, keeping one slot for the turn
    across all its rounds (capacity 1: background waits for the turn to end);
  * a background generation already running is never touched;
  * foreground generations are never gated (the reservation is theirs).

``capacity`` is the engine's REPORTED slot count (``autonomy.engine_slots``),
re-read on every decision and on every change the engine announces -- never a
constant. Priority rides a ContextVar the session sets around its turn, so it
follows the turn's own tasks (tool rounds included) and nothing else.

Every waiter wakes on the events that can admit it (a generation finishing,
a turn ending, the engine's slot count changing), not on a poll. Off the
event loop that owns the condition (a thread, another loop) admission is not
gated: the safe direction is "proceed", never "wedge".
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import time
from typing import AsyncIterator, Optional

logger = logging.getLogger("Ouroboros.GenerationAdmission")

FOREGROUND = "foreground"
BACKGROUND = "background"

_priority: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ov_generation_priority", default=BACKGROUND)


def enabled() -> bool:
    return os.environ.get("JARVIS_FOREGROUND_RESERVATION_ENABLED", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def current_priority() -> str:
    return _priority.get()


class _Admission:
    def __init__(self) -> None:
        self._turns = 0
        self._bg_inflight = 0
        self._cond: Optional[asyncio.Condition] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._endpoint = ""
        self.waits = 0                 # background admissions that had to wait
        self.waited_s = 0.0
        self._listening = False

    # ---- state the gate reads
    def capacity(self) -> int:
        """The engine's reported slots for the local endpoint; the J-Prime
        state's limit when the engine has not reported; 1 when nothing is
        known (the conservative reading -- one slot is always there)."""
        try:
            from backend.core.ouroboros.governance.autonomy import engine_slots
            n = engine_slots.slots(self._endpoint) if self._endpoint else None
            if n:
                return max(1, int(n))
            known = engine_slots.known()
            if known:
                return max(1, int(max(known.values())))
        except Exception:  # noqa: BLE001
            pass
        try:
            from backend.core.ouroboros.governance._governance_state import get_jprime_state
            return max(1, int(get_jprime_state().slot_limit))
        except Exception:  # noqa: BLE001
            return 1

    def _bg_limit(self) -> int:
        cap = self.capacity()
        return cap if self._turns == 0 else max(0, cap - 1)

    def snapshot(self) -> dict:
        return {"turns": self._turns, "background_inflight": self._bg_inflight,
                "capacity": self.capacity(), "background_limit": self._bg_limit(),
                "waits": self.waits, "waited_s": round(self.waited_s, 1)}

    # ---- loop binding
    def _condition(self) -> Optional[asyncio.Condition]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if self._cond is None or self._loop is not loop:
            if self._cond is not None and (self._turns or self._bg_inflight):
                return None            # another loop owns live state: do not gate
            self._cond, self._loop = asyncio.Condition(), loop
            self._listen()
        return self._cond

    def _listen(self) -> None:
        """Wake waiters when the engine's slot count changes."""
        if self._listening:
            return
        try:
            from backend.core.ouroboros.governance.autonomy import engine_slots

            def _changed(_endpoint: str, _n: int) -> None:
                self._wake_threadsafe()

            engine_slots.add_listener(_changed)
            self._listening = True
        except Exception:  # noqa: BLE001
            pass

    def _wake_threadsafe(self) -> None:
        loop, cond = self._loop, self._cond
        if loop is None or cond is None or loop.is_closed():
            return

        async def _notify() -> None:
            async with cond:
                cond.notify_all()

        try:
            if asyncio.get_running_loop() is loop:
                loop.create_task(_notify())
                return
        except RuntimeError:
            pass
        asyncio.run_coroutine_threadsafe(_notify(), loop)

    async def _notify(self) -> None:
        cond = self._cond
        if cond is not None:
            async with cond:
                cond.notify_all()

    # ---- the two brackets
    @contextlib.asynccontextmanager
    async def foreground_turn(self) -> AsyncIterator[None]:
        """Held for the WHOLE turn (every round). Marks the turn's context
        foreground and reserves a slot against background admission."""
        token = _priority.set(FOREGROUND)
        self._condition()
        self._turns += 1
        try:
            yield
        finally:
            self._turns = max(0, self._turns - 1)
            _priority.reset(token)
            with contextlib.suppress(Exception):
                await self._notify()

    @contextlib.asynccontextmanager
    async def generation(self, endpoint: str = "") -> AsyncIterator[None]:
        """Around ONE request to the local engine."""
        if endpoint:
            self._endpoint = endpoint
        if not enabled() or _priority.get() == FOREGROUND:
            yield
            return
        cond = self._condition()
        if cond is None:
            yield
            return
        started = time.monotonic()
        async with cond:
            if self._bg_inflight >= self._bg_limit():
                self.waits += 1
                logger.info("[Admission] background generation waits: %s", self.snapshot())
                await cond.wait_for(lambda: self._bg_inflight < self._bg_limit())
                self.waited_s += time.monotonic() - started
            self._bg_inflight += 1
        try:
            yield
        finally:
            self._bg_inflight = max(0, self._bg_inflight - 1)
            with contextlib.suppress(Exception):
                await self._notify()


_ADMISSION = _Admission()


def admission() -> _Admission:
    return _ADMISSION


def reset_for_tests() -> None:
    global _ADMISSION
    _ADMISSION = _Admission()


__all__ = ["BACKGROUND", "FOREGROUND", "admission", "current_priority", "enabled",
           "reset_for_tests"]
