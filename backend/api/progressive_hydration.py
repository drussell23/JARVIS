"""Progressive Daemon Hydration state machine (Phase 12, Slice B).

The full JARVIS body is heavy (CoreML, PyTorch, the OuroborosDaemon,
16-sensor intake). Loading them synchronously at boot blocks the ASGI
event loop, so the Swift client's TCP handshakes time out before port
8010 accepts. This orchestrator makes the body come up PROGRESSIVELY:

  1. The ASGI app binds + serves the router INSTANTLY (mandate 1 — no
     sleep, no blocking loop; the caller schedules ``hydrate()`` via
     ``asyncio.create_task`` in the FastAPI lifespan).
  2. On start it broadcasts ``SYSTEM_HYDRATING`` over the TrinityEventBus
     (mandate 2 — telemetry), then loads each subsystem one at a time,
     emitting incremental progress the ``governance_sse_bridge`` forwards
     to the HUD.
  3. **OOM Guard (fail-soft):** a subsystem loader that throws
     ``MemoryError``/``RuntimeError``/anything is caught — it emits
     ``SYSTEM_DEGRADED`` and CONTINUES. The ASGI server never crashes, and
     the UCP command endpoints (with the DoubleWord failover) stay live.

DRY (mandate 3): telemetry rides the EXISTING TrinityEventBus (topic the
``governance_sse_bridge`` already subscribes to) — no new event plane. The
subsystem list is declarative + injectable, so real loaders (OuroborosDaemon
…) plug in without touching this machine.

Every public entry point NEVER raises.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("Jarvis.ProgressiveHydration")

#: TrinityEventBus topic — under ``ouroboros.#`` so the existing
#: governance_sse_bridge forwards it to the HUD without a new subscription.
HYDRATION_TOPIC = "ouroboros.hydration"


class HydrationState(str, Enum):
    BOOTING = "booting"        # app binding, not yet hydrating
    HYDRATING = "hydrating"    # background subsystems loading
    READY = "ready"            # all subsystems up
    DEGRADED = "degraded"      # some subsystem failed; command loop still live


@dataclass
class Subsystem:
    """One heavy component to hydrate in the background. ``loader`` is an
    async no-arg callable; a raise is caught fail-soft."""
    name: str
    loader: Callable[[], Awaitable[None]]
    label: str = ""


BusPublish = Callable[[str, Dict[str, Any]], Awaitable[Any]]


class HydrationOrchestrator:
    """Runs the subsystem hydration off the request path + streams state.
    NEVER raises out of ``hydrate()``."""

    def __init__(
        self,
        subsystems: List[Subsystem],
        *,
        bus_publish: Optional[BusPublish] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._subsystems = subsystems
        self._bus_publish = bus_publish
        self._clock = clock
        self._state = HydrationState.BOOTING
        self.results: Dict[str, str] = {}
        self.started_at: float = 0.0

    @property
    def state(self) -> HydrationState:
        return self._state

    def snapshot(self) -> Dict[str, Any]:
        return {
            "state": self._state.value,
            "subsystems": dict(self.results),
            "total": len(self._subsystems),
            "loaded": sum(1 for v in self.results.values() if v == "ok"),
        }

    async def _publish(self, topic: str, data: Dict[str, Any]) -> None:
        """Publish telemetry to the TrinityEventBus (reuse — mandate 3).
        Resolves the real bus lazily if no publisher injected. NEVER
        raises."""
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
            logger.debug("[Hydration] telemetry publish degraded", exc_info=True)

    async def _emit(self, kind: str, **payload: Any) -> None:
        """Emit a hydration telemetry event. ``kind`` ∈ SYSTEM_HYDRATING /
        SYSTEM_DEGRADED / SYSTEM_READY. Rendered by governance_sse_bridge as
        a daemon narration on the HUD."""
        data = {
            "type": kind,
            "op_id": "hydration",
            "narration_text": payload.get("message", kind),
            "narration_priority": "high" if kind == "SYSTEM_DEGRADED" else "normal",
            "source_brain": "supervisor",
            "state": self._state.value,
        }
        data.update(payload)
        await self._publish(HYDRATION_TOPIC, data)

    async def hydrate(self) -> HydrationState:
        """Load every subsystem off the event loop's request path, fail-
        soft, streaming state. Runs as a background task; NEVER raises."""
        self.started_at = self._clock()
        self._state = HydrationState.HYDRATING
        total = len(self._subsystems)
        await self._emit(
            "SYSTEM_HYDRATING",
            message="Backend online — hydrating O+V + heavy subsystems…",
            progress=0, total=total)

        degraded = False
        for i, sub in enumerate(self._subsystems, start=1):
            await self._emit(
                "SYSTEM_HYDRATING", subsystem=sub.name, progress=i, total=total,
                message=f"Loading {sub.label or sub.name} ({i}/{total})…")
            try:
                await sub.loader()
                self.results[sub.name] = "ok"
                logger.info("[Hydration] %s ready (%d/%d)", sub.name, i, total)
            except BaseException as exc:  # noqa: BLE001 — OOM guard: catch ALL
                # MemoryError/RuntimeError on a 16GB M1 must NOT crash the
                # server. Degrade this subsystem, keep the command loop live.
                degraded = True
                self.results[sub.name] = f"error: {type(exc).__name__}: {exc}"
                logger.warning(
                    "[Hydration] %s FAILED (%s) — degraded, command loop "
                    "stays live: %s", sub.name, type(exc).__name__, exc)
                await self._emit(
                    "SYSTEM_DEGRADED", subsystem=sub.name,
                    error=f"{type(exc).__name__}: {exc}", progress=i, total=total,
                    message=f"{sub.label or sub.name} degraded "
                            f"({type(exc).__name__}) — commands still work")

        self._state = HydrationState.DEGRADED if degraded else HydrationState.READY
        elapsed = self._clock() - self.started_at
        await self._emit(
            "SYSTEM_DEGRADED" if degraded else "SYSTEM_READY",
            elapsed_s=round(elapsed, 2),
            message=("Organism hydrated (DEGRADED — some subsystems offline; "
                     "command loop live via DoubleWord failover)" if degraded
                     else "Organism fully hydrated — O+V online"))
        return self._state


__all__ = [
    "HYDRATION_TOPIC", "HydrationState", "Subsystem",
    "HydrationOrchestrator",
]
