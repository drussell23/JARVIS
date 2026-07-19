"""Device-Aware SSE Multiplexer — the JARVIS-Apple connection layer.

Operator authorization 2026-07-19. The native Swift client calls
``GET /api/stream/{device_id}``; the backend previously exposed only a
singleton ``/api/stream/sse``. This layer transitions that singleton
into a device-aware multiplexer so multiple native clients (desktop +
mobile + watch) each get their own routed session — and abruptly-
dropped native connections (OS backgrounding / network handoff, no TCP
FIN) are pruned so 24/7 residency never leaks RAM.

Mandate 2:
  * **Device-Aware Multiplexing** — a ``device_id → registration`` map;
    a per-device stream is created on connect and torn down on drop.
  * **Dead-Stream Pruning (Zombie Guard)** — a write fault
    (``ConnectionResetError`` / ``BrokenPipeError`` / any send error)
    OR a heartbeat-ack timeout instantly DEREGISTERS the device and
    releases its generator. No orphaned streams accumulate.

DRY (mandate 3): the per-device generator COMPOSES the EXISTING
``EventStream.sse_stream`` (same replay/channel semantics the web
client uses); state still flows through the established event stream /
TrinityEventBus. This module only adds the device routing + lifecycle.

NEVER raises out of the registry operations.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Dict, Optional, Set

logger = logging.getLogger("Jarvis.DeviceStream")


class DeviceRegistration:
    __slots__ = ("device_id", "connected_at", "last_beat", "alive")

    def __init__(self, device_id: str, now: float) -> None:
        self.device_id = device_id
        self.connected_at = now
        self.last_beat = now
        self.alive = True


class DeviceStreamManager:
    """Registry of live native SSE sessions, keyed by ``device_id``.
    Thread-safe under the asyncio loop (single-loop mutation); every
    public method NEVER raises."""

    def __init__(
        self,
        *,
        clock=time.monotonic,
        heartbeat_timeout_s: float = 45.0,
    ) -> None:
        self._clock = clock
        self._hb_timeout = heartbeat_timeout_s
        self._devices: Dict[str, DeviceRegistration] = {}
        self.stats: Dict[str, int] = {
            "connects": 0, "drops_write_fault": 0,
            "drops_heartbeat": 0, "reconnects": 0,
        }

    # ---- lifecycle ----

    def register(self, device_id: str) -> DeviceRegistration:
        """Register (or re-register) a device. A reconnect replaces the
        stale registration — the OLD generator is already being pruned
        by its own drop path. NEVER raises."""
        now = self._clock()
        if device_id in self._devices:
            self.stats["reconnects"] += 1
        reg = DeviceRegistration(device_id, now)
        self._devices[device_id] = reg
        self.stats["connects"] += 1
        logger.info("[DeviceStream] %s connected (active=%d)",
                    device_id, len(self._devices))
        return reg

    def deregister(self, device_id: str, *, reason: str = "closed") -> None:
        """Evict a device + release its slot. Idempotent. NEVER
        raises."""
        reg = self._devices.pop(device_id, None)
        if reg is not None:
            reg.alive = False
            logger.info("[DeviceStream] %s deregistered (%s, active=%d)",
                        device_id, reason, len(self._devices))

    def beat(self, device_id: str) -> None:
        reg = self._devices.get(device_id)
        if reg is not None:
            reg.last_beat = self._clock()

    def is_alive(self, device_id: str) -> bool:
        reg = self._devices.get(device_id)
        return reg is not None and reg.alive

    def heartbeat_expired(self, device_id: str) -> bool:
        reg = self._devices.get(device_id)
        if reg is None:
            return True
        return (self._clock() - reg.last_beat) > self._hb_timeout

    @property
    def active_devices(self) -> Set[str]:
        return set(self._devices)

    # ---- the per-device stream (composes the existing sse_stream) ----

    async def device_stream(
        self,
        device_id: str,
        inner_stream: AsyncIterator[str],
        *,
        heartbeat_interval_s: float = 15.0,
    ) -> AsyncIterator[str]:
        """Wrap the EXISTING sse_stream generator with device routing +
        dead-stream pruning. A write fault propagating out of the inner
        stream, or a heartbeat gap, DEREGISTERS the device and stops
        the generator — the orphaned stream is released. NEVER raises
        past the generator boundary (a fault ends the stream cleanly)."""
        self.register(device_id)
        # Background pump: drain the inner stream into a queue so a
        # heartbeat-window timeout NEVER cancels the inner generator
        # (cancelling it repeatedly would corrupt it — the root cause
        # of the earlier StopAsyncIteration-instead-of-prune bug). A
        # write fault surfaces through the queue sentinel.
        _SENTINEL_END = object()
        queue: "asyncio.Queue" = asyncio.Queue(maxsize=256)
        pump_exc: Dict[str, BaseException] = {}

        async def _pump() -> None:
            try:
                async for frame in inner_stream:
                    await queue.put(frame)
                await queue.put(_SENTINEL_END)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 — surface the fault
                pump_exc["exc"] = exc
                await queue.put(_SENTINEL_END)

        pump = asyncio.get_event_loop().create_task(_pump())
        try:
            while self.is_alive(device_id):
                try:
                    frame = await asyncio.wait_for(
                        queue.get(), timeout=heartbeat_interval_s,
                    )
                    if frame is _SENTINEL_END:
                        exc = pump_exc.get("exc")
                        if isinstance(exc, (ConnectionResetError,
                                            BrokenPipeError)):
                            raise exc              # → fault-prune path
                        break                      # clean end of inner
                    # Real data resets the liveness clock (a keepalive
                    # is US probing, not the client acking).
                    self.beat(device_id)
                    yield frame
                except asyncio.TimeoutError:
                    # No data this window. Staleness past the timeout →
                    # the client is gone (half-open socket) → prune.
                    if self.heartbeat_expired(device_id):
                        self.stats["drops_heartbeat"] += 1
                        self.deregister(device_id, reason="heartbeat_timeout")
                        break
                    # Else keepalive — a dead native socket raises on
                    # THIS write and prunes via the fault path.
                    yield ": keepalive\n\n"
        except (ConnectionResetError, BrokenPipeError) as exc:
            # Silent native drop — no FIN. Prune instantly.
            self.stats["drops_write_fault"] += 1
            self.deregister(device_id, reason=f"write_fault:{type(exc).__name__}")
        except asyncio.CancelledError:
            self.deregister(device_id, reason="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — any send fault = drop
            self.stats["drops_write_fault"] += 1
            self.deregister(device_id, reason=f"error:{type(exc).__name__}")
        finally:
            # Reap the pump task so the inner generator is released —
            # zero orphaned streams over 24/7 residency.
            if not pump.done():
                pump.cancel()
                try:
                    await pump
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            self.deregister(device_id, reason="stream_end")


# Process-wide manager (one registry per backend).
_MANAGER: Optional[DeviceStreamManager] = None


def get_device_manager() -> DeviceStreamManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = DeviceStreamManager()
    return _MANAGER


def reset_device_manager() -> None:
    global _MANAGER
    _MANAGER = None


__all__ = [
    "DeviceRegistration",
    "DeviceStreamManager",
    "get_device_manager",
    "reset_device_manager",
]
