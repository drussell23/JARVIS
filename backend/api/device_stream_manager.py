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
import json
import logging
import os
import re
import threading
import time
from collections import deque
from typing import (Any, AsyncIterator, Callable, Deque, Dict, List, Optional,
                    Set, Tuple)

logger = logging.getLogger("Jarvis.DeviceStream")

# SSE frame ``id:`` extractor — the seq the Last-Event-ID header echoes.
_ID_RE = re.compile(r"^id:\s*(\d+)", re.MULTILINE)


def _rehydration_cap() -> int:
    try:
        return max(16, int(os.environ.get(
            "JARVIS_SSE_REHYDRATION_BUFFER", "512",
        )))
    except (TypeError, ValueError):
        return 512


def _frame_seq(frame: str) -> Optional[int]:
    """The SSE ``id:`` sequence of a frame, or None (keepalives have
    no id). NEVER raises."""
    try:
        m = _ID_RE.search(frame)
        return int(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


def _adapt_enabled() -> bool:
    return os.environ.get(
        "JARVIS_SSE_JARVISKIT_CONTRACT", "true",
    ).strip().lower() not in ("0", "false", "no", "off")


def _adapt_frame(frame: str) -> str:
    """Translate a local EventStream frame into the JARVISKit SSE dialect
    (``event:<type>`` + flat ``data:``) for the native client. A frame
    that isn't a typed governance/daemon frame passes through unchanged.
    NEVER raises."""
    try:
        if not _adapt_enabled():
            return frame
        from backend.api.sse_contract import eventstream_frame_to_jarviskit
        adapted = eventstream_frame_to_jarviskit(frame)
        return adapted if adapted is not None else frame
    except Exception:  # noqa: BLE001
        return frame


class RehydrationBuffer:
    """Thread-safe circular cache of the last N id-bearing SSE frames.
    Ephemeral, tied to the multiplexer lifecycle (mandate 3 — no DB).
    NEVER raises."""

    def __init__(self, *, cap: Optional[int] = None) -> None:
        self._cap = cap or _rehydration_cap()
        self._buf: Deque[Tuple[int, str]] = deque(maxlen=self._cap)
        self._lock = threading.Lock()

    def append(self, frame: str) -> None:
        try:
            seq = _frame_seq(frame)
            if seq is None:
                return                          # keepalives are not cached
            with self._lock:
                self._buf.append((seq, frame))
        except Exception:  # noqa: BLE001
            pass

    def oldest_seq(self) -> Optional[int]:
        with self._lock:
            return self._buf[0][0] if self._buf else None

    def newest_seq(self) -> Optional[int]:
        with self._lock:
            return self._buf[-1][0] if self._buf else None

    def replay_after(self, last_event_id: int) -> Tuple[List[str], bool]:
        """Frames with seq > ``last_event_id``, and a ``too_old`` flag.

        ``too_old`` is True when the client's cursor has fallen OUT of
        the buffer (the oldest cached seq is already past
        last_event_id+1) — the caller emits STATE_RESET instead of a
        torn partial replay. NEVER raises."""
        try:
            with self._lock:
                if not self._buf:
                    return [], False            # empty buffer: nothing missed
                oldest = self._buf[0][0]
                # The client saw up to last_event_id; it needs
                # last_event_id+1 onward. If that is older than what we
                # still hold, the gap is unrecoverable.
                too_old = (last_event_id + 1) < oldest
                if too_old:
                    return [], True
                return (
                    [f for s, f in self._buf if s > last_event_id],
                    False,
                )
        except Exception:  # noqa: BLE001
            return [], True                     # fail toward a clean reset


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
        #: Shared circular rehydration cache — every routed device's
        #: frames land here so a reconnecting client can catch up.
        self._rehydration = RehydrationBuffer()
        self.stats: Dict[str, int] = {
            "connects": 0, "drops_write_fault": 0,
            "drops_heartbeat": 0, "reconnects": 0,
            "replayed_frames": 0, "state_resets": 0,
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
        last_event_id: Optional[int] = None,
        cold_start_frames: Optional[Callable[[], List[str]]] = None,
    ) -> AsyncIterator[str]:
        """Wrap the EXISTING sse_stream generator with device routing,
        Last-Event-ID catch-up replay, and dead-stream pruning.

        On reconnect (``last_event_id`` set) the missed frames are
        replayed from the circular buffer FIRST, then the client
        attaches to the live inner stream — with no duplicate payloads
        (live frames at/below the replay cursor are skipped). A cursor
        that has fallen out of the buffer emits a single
        ``STATE_RESET`` event. NEVER raises past the generator
        boundary.

        Slice I — SSE Replay Parity: on a COLD BOOT (no ``Last-Event-ID``)
        or a cursor-expired ``STATE_RESET``, the frame ring only holds what
        flowed while a client was attached — the critical DAG-hydration
        lifecycle fired at boot before any HUD connected. So ``cold_start_frames``
        (the TrinityEventBus ``get_replay_snapshot()`` history, formatted as
        HUD daemon frames) is flushed to reconcile state BEFORE live — exact
        parity with the UDS bridge. These reconciliation frames carry no
        ``id:`` (idempotent in the HUD state machine) so they never perturb
        the Last-Event-ID cursor."""
        self.register(device_id)
        replay_cursor = -1
        need_cold_flush = (last_event_id is None)   # fresh client → full state
        # ---- Seamless Network Handoff: catch-up replay (mandate 2) ----
        if last_event_id is not None:
            frames, too_old = self._rehydration.replay_after(last_event_id)
            if too_old:
                self.stats["state_resets"] += 1
                logger.info(
                    "[DeviceStream] %s cursor %d fell out of buffer — "
                    "STATE_RESET", device_id, last_event_id,
                )
                yield ("event: STATE_RESET\ndata: "
                       + json.dumps({"reason": "cursor_expired",
                                     "last_event_id": last_event_id})
                       + "\n\n")
                replay_cursor = last_event_id
                need_cold_flush = True             # cursor gone → full reconcile
            else:
                for f in frames:
                    self.stats["replayed_frames"] += 1
                    seq = _frame_seq(f)
                    if seq is not None:
                        replay_cursor = max(replay_cursor, seq)
                    self.beat(device_id)
                    yield f
        # ---- Slice I: cold-boot / cursor-expired state reconciliation from
        #      the TrinityEventBus replay buffer (parity with the UDS bridge) --
        if need_cold_flush and cold_start_frames is not None:
            try:
                cold = cold_start_frames() or []
            except Exception:  # noqa: BLE001 — never break the stream on it
                cold = []
            for f in cold:
                self.stats["replayed_frames"] += 1
                self.beat(device_id)
                yield f
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
                    # Serialization-contract enforcement (Phase 10): the
                    # native Swift SSEClient needs an ``event:<type>`` line
                    # + a FLAT data payload; the local EventStream emits a
                    # bare ``id:``/``data:{seq,ch,ts,d}`` envelope. Translate
                    # typed frames to the JARVISKit dialect here, at the
                    # native-app boundary (non-typed frames pass through).
                    frame = _adapt_frame(frame)
                    # Cache every id-bearing frame for future
                    # reconnects (mandate 2 — the circular buffer).
                    self._rehydration.append(frame)
                    # No duplicates on catch-up: skip a live frame the
                    # replay already delivered.
                    seq = _frame_seq(frame)
                    if seq is not None and seq <= replay_cursor:
                        continue
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
    "RehydrationBuffer",
    "get_device_manager",
    "reset_device_manager",
]
