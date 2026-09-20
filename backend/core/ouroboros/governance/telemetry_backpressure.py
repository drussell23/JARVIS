"""A bounded queue between a telemetry producer and a disk it may outrun.

Why this exists before the switches flip
----------------------------------------

``REPAIR_TRAJECTORY_EMIT`` and ``SHADOW_HARNESS`` are both high-rate disk
producers, and this control plane has just been dug out of exactly that
hole: measured starvation of p50 2,050ms, p90 4,721ms, max 43.7s, whose top
attributed causes were a memory monitor and an embedder doing synchronous
I/O on the loop. Arming two more writers without a buffer would hand that
back, and the arming is the easy half.

The contract
------------

A producer's ``offer`` NEVER awaits a disk. It puts into a bounded queue and
returns immediately; a single consumer task drains to the sink in a worker
thread. When the queue is full the OLDEST payload is dropped, not the
newest: telemetry is a sampled record of a live process, and the freshest
sample is the one worth keeping when the choice is forced.

Dropping is COUNTED, never silent. A gap in the record that is not itself in
the record is indistinguishable from a period when nothing happened -- and
this project has already lost weeks to subsystems that looked idle because
they reported nothing. ``stats()`` names every drop.

What it will not do
-------------------

It will not block the FSM. It will not retry a failing sink forever -- a
sink that raises repeatedly is quarantined, because a broken endpoint that
is retried at emission rate is a busy loop with extra steps. It will not
preserve ordering across a drop, which is the cost of not blocking and is
stated rather than discovered.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("Ouroboros.TelemetryBackpressure")

_DEFAULT_MAXSIZE = 512
_DEFAULT_QUARANTINE_AFTER = 5
_DEFAULT_QUARANTINE_S = 60.0


def _env_int(name: str, default: int) -> int:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        value = int(raw) if raw else default
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        value = float(raw) if raw else default
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass
class BufferStats:
    """What the buffer did, including what it threw away."""

    offered: int = 0
    written: int = 0
    dropped_full: int = 0
    dropped_closed: int = 0
    sink_faults: int = 0
    quarantined: bool = False

    def render(self) -> str:
        return (
            f"offered={self.offered} written={self.written} "
            f"dropped_full={self.dropped_full} "
            f"dropped_closed={self.dropped_closed} "
            f"faults={self.sink_faults}"
            + (" QUARANTINED" if self.quarantined else "")
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "offered": self.offered, "written": self.written,
            "dropped_full": self.dropped_full,
            "dropped_closed": self.dropped_closed,
            "sink_faults": self.sink_faults, "quarantined": self.quarantined,
        }


class TelemetryBuffer:
    """Bounded queue + one draining consumer.

    The sink may be sync or async. A sync sink is run in a worker thread, so
    a blocking ``write()`` cannot reach the event loop -- which is the whole
    point, and the mistake that produced the starvation this buffer exists
    to avoid repeating.
    """

    def __init__(
        self,
        name: str,
        sink: Callable[[Any], Any],
        *,
        maxsize: Optional[int] = None,
        quarantine_after: Optional[int] = None,
        quarantine_s: Optional[float] = None,
    ) -> None:
        self._name = name
        self._sink = sink
        self._maxsize = maxsize if maxsize is not None else _env_int(
            "JARVIS_TELEMETRY_BUFFER_MAXSIZE", _DEFAULT_MAXSIZE,
        )
        self._quarantine_after = (
            quarantine_after if quarantine_after is not None
            else _env_int(
                "JARVIS_TELEMETRY_QUARANTINE_AFTER", _DEFAULT_QUARANTINE_AFTER,
            )
        )
        self._quarantine_s = (
            quarantine_s if quarantine_s is not None
            else _env_float("JARVIS_TELEMETRY_QUARANTINE_S", _DEFAULT_QUARANTINE_S)
        )
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self._consecutive_faults = 0
        self._quarantined_until = 0.0
        self._stats = BufferStats()

    # -- producer side ---------------------------------------------------

    def offer(self, payload: Any) -> bool:
        """Hand over a payload without ever awaiting. NEVER raises.

        Returns whether it was accepted, so a caller can tell "buffered"
        from "dropped" -- the distinction that makes a gap in the record
        visible instead of merely absent.
        """
        if self._closed:
            self._stats.dropped_closed += 1
            return False
        self._stats.offered += 1
        try:
            self._queue.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            pass
        # Full: discard the OLDEST and take the newest. A sampled record of
        # a live process is most useful at its freshest, and the alternative
        # -- refusing the new one -- freezes the record at the moment the
        # system got interesting.
        try:
            self._queue.get_nowait()
            self._queue.task_done()
            self._stats.dropped_full += 1
        except Exception:  # noqa: BLE001
            self._stats.dropped_full += 1
            return False
        try:
            self._queue.put_nowait(payload)
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- lifecycle -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        """Spawn the drain task. Idempotent. NEVER raises."""
        if self.running or self._closed:
            return False
        try:
            self._task = asyncio.create_task(
                self._drain(), name=f"telemetry_buffer:{self._name}",
            )
            logger.info(
                "[TelemetryBuffer] %s armed: maxsize=%d quarantine_after=%d",
                self._name, self._maxsize, self._quarantine_after,
            )
            return True
        except RuntimeError:
            logger.debug("[TelemetryBuffer] %s: no running loop", self._name)
            return False

    async def aclose(self, *, drain: bool = True, timeout_s: float = 2.0) -> None:
        """Stop accepting, optionally finish what is queued. NEVER raises."""
        self._closed = True
        if drain and self.running:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=timeout_s)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None

    def stats(self) -> BufferStats:
        return self._stats

    # -- consumer --------------------------------------------------------

    async def _drain(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                if self._quarantined():
                    self._stats.dropped_closed += 1
                    continue
                await self._write(payload)
                self._stats.written += 1
                self._consecutive_faults = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._note_fault(exc)
            finally:
                self._queue.task_done()

    async def _write(self, payload: Any) -> None:
        """Run the sink off the loop when it is synchronous.

        A sync sink is assumed to touch a disk, and a disk write on the
        event loop is the exact fault this module was built after.
        """
        result = self._sink(payload)
        if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
            await result
        elif callable(getattr(result, "__await__", None)):
            await result

    def _quarantined(self) -> bool:
        if self._quarantined_until and time.monotonic() < self._quarantined_until:
            return True
        if self._stats.quarantined:
            self._stats.quarantined = False
            self._consecutive_faults = 0
            logger.info(
                "[TelemetryBuffer] %s leaving quarantine — retrying the sink",
                self._name,
            )
        return False

    def _note_fault(self, exc: BaseException) -> None:
        self._stats.sink_faults += 1
        self._consecutive_faults += 1
        logger.debug(
            "[TelemetryBuffer] %s sink fault (%s): %s",
            self._name, type(exc).__name__, exc,
        )
        if self._consecutive_faults >= self._quarantine_after:
            self._stats.quarantined = True
            self._quarantined_until = time.monotonic() + self._quarantine_s
            logger.warning(
                "[TelemetryBuffer] %s sink failed %d times in a row — "
                "quarantining for %.0fs. Retrying a broken endpoint at "
                "emission rate is a busy loop with extra steps; %s",
                self._name, self._consecutive_faults, self._quarantine_s,
                self._stats.render(),
            )


_buffers: Dict[str, TelemetryBuffer] = {}


def get_buffer(
    name: str,
    sink: Callable[[Any], Any],
    **kwargs: Any,
) -> TelemetryBuffer:
    """One buffer per named stream, started on first use. NEVER raises."""
    buf = _buffers.get(name)
    if buf is None:
        buf = TelemetryBuffer(name, sink, **kwargs)
        _buffers[name] = buf
    if not buf.running:
        buf.start()
    return buf


def all_stats() -> Dict[str, Dict[str, Any]]:
    """Every stream's standing, drops included."""
    return {name: buf.stats().as_dict() for name, buf in _buffers.items()}


def reset_buffers() -> None:
    """Test seam. Does not drain."""
    _buffers.clear()


__all__ = [
    "BufferStats",
    "TelemetryBuffer",
    "all_stats",
    "get_buffer",
    "reset_buffers",
]
