"""Actor-Model Fault Isolation for the OuroborosDaemon (Phase 12, Slice C).

Booting O+V asynchronously behind the HUD risks cascading failure: an
unhandled fault in an autonomous loop (file IO, subprocess panic, OOM)
must NOT propagate to the ASGI event loop and take the whole daemon down.

This is an ACTOR SHELL (mandate 2b): the OuroborosDaemon runs inside an
isolated ``asyncio`` supervisor task. Any exception is caught at the
actor boundary — it NEVER reaches the server — an ``OUROBOROS_FAULT`` is
emitted to the SSE bridge, and the actor restarts O+V with EXPONENTIAL
BACKOFF (no ``sleep`` hack: ``asyncio.sleep`` off the request path). After
a bounded restart budget it settles into a FAULTED state (still not
crashing the server).

DRY (mandate 3): telemetry rides the EXISTING TrinityEventBus (topic
``ouroboros.fault`` → the governance_sse_bridge already forwards
``ouroboros.#`` to the HUD). The daemon itself is supplied by a factory so
the real supervisor-constructed OuroborosDaemon plugs in unchanged.

Injectable ``bus_publish`` / ``sleeper`` seams make it fully unit-testable
without a real daemon. NEVER raises out of ``run()``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("Jarvis.OuroborosActor")

FAULT_TOPIC = "ouroboros.fault"


class ActorState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    AWAKE = "awake"
    RESTARTING = "restarting"      # backing off before a retry
    FAULTED = "faulted"            # restart budget exhausted


def _max_restarts() -> int:
    import os
    try:
        return max(0, int(os.environ.get("JARVIS_OUROBOROS_ACTOR_MAX_RESTARTS", "5")))
    except (TypeError, ValueError):
        return 5


def _base_backoff_s() -> float:
    import os
    try:
        return max(0.05, float(os.environ.get("JARVIS_OUROBOROS_ACTOR_BACKOFF_S", "1.0")))
    except (TypeError, ValueError):
        return 1.0


def _cap_backoff_s() -> float:
    import os
    try:
        return max(1.0, float(os.environ.get("JARVIS_OUROBOROS_ACTOR_BACKOFF_CAP_S", "60.0")))
    except (TypeError, ValueError):
        return 60.0


class OuroborosActor:
    """Supervises the OuroborosDaemon in isolation. ``daemon_factory`` is a
    (possibly async) no-arg callable returning an object with an async
    ``awaken()``. NEVER lets a fault reach the event loop."""

    def __init__(
        self,
        daemon_factory: Callable[[], Any],
        *,
        bus_publish: Optional[Callable[[str, Dict[str, Any]], Awaitable[Any]]] = None,
        max_restarts: Optional[int] = None,
        base_backoff_s: Optional[float] = None,
        cap_backoff_s: Optional[float] = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = daemon_factory
        self._bus_publish = bus_publish
        self._max_restarts = max_restarts if max_restarts is not None else _max_restarts()
        self._base = base_backoff_s if base_backoff_s is not None else _base_backoff_s()
        self._cap = cap_backoff_s if cap_backoff_s is not None else _cap_backoff_s()
        self._sleeper = sleeper
        self._clock = clock
        self.state = ActorState.IDLE
        self.restart_count = 0
        self.last_error = ""
        self._task: Optional[asyncio.Task] = None

    async def _publish(self, topic: str, data: Dict[str, Any]) -> None:
        try:
            pub = self._bus_publish
            if pub is None:
                from backend.core.trinity_event_bus import get_event_bus_if_exists
                bus = get_event_bus_if_exists()
                if bus is None:
                    return
                pub = lambda t, d: bus.publish_raw(topic=t, data=d, persist=False)
            await pub(topic, data)
        except Exception:  # noqa: BLE001
            logger.debug("[OuroborosActor] telemetry degraded", exc_info=True)

    async def _emit_fault(self, exc: BaseException, restart_num: int,
                          next_backoff_s: float, exhausted: bool) -> None:
        await self._publish(FAULT_TOPIC, {
            "type": "OUROBOROS_FAULT",
            "op_id": "ouroboros_actor",
            "fault": f"{type(exc).__name__}: {exc}",
            "restart_num": restart_num,
            "next_backoff_s": round(next_backoff_s, 2),
            "exhausted": exhausted,
            "source_brain": "ouroboros",
            "narration_priority": "high",
            "narration_text": (
                f"O+V faulted ({type(exc).__name__}) — "
                + ("restart budget exhausted; command loop stays live"
                   if exhausted else
                   f"auto-restarting in {next_backoff_s:.0f}s")),
        })

    async def _make_daemon(self) -> Any:
        d = self._factory()
        if asyncio.iscoroutine(d):
            d = await d
        return d

    async def run(self) -> ActorState:
        """The actor loop: awaken O+V; on any fault, isolate it, emit
        OUROBOROS_FAULT, and restart with exponential backoff up to the
        budget. NEVER raises (except CancelledError for clean shutdown)."""
        while True:
            self.state = ActorState.STARTING
            try:
                daemon = await self._make_daemon()
                await daemon.awaken()
                self.state = ActorState.AWAKE
                logger.info("[OuroborosActor] O+V awake")
                return self.state
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 — FAULT ISOLATION
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.restart_count += 1
                exhausted = self.restart_count > self._max_restarts
                backoff = 0.0 if exhausted else min(
                    self._cap, self._base * (2 ** (self.restart_count - 1)))
                logger.warning(
                    "[OuroborosActor] O+V fault #%d (%s) — %s",
                    self.restart_count, self.last_error,
                    "EXHAUSTED" if exhausted else f"retry in {backoff:.1f}s")
                await self._emit_fault(exc, self.restart_count, backoff, exhausted)
                if exhausted:
                    self.state = ActorState.FAULTED
                    return self.state
                self.state = ActorState.RESTARTING
                await self._sleeper(backoff)     # exponential backoff retry

    def start(self) -> asyncio.Task:
        """Launch the actor as an isolated background task (mandate 2b —
        a fault in here never reaches the ASGI loop)."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_event_loop().create_task(
                self.run(), name="ouroboros-actor")
        return self._task

    async def stop(self) -> None:
        t, self._task = self._task, None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


__all__ = ["FAULT_TOPIC", "ActorState", "OuroborosActor"]
