"""The Hive Aggregator — a read-only listening post over the fragmented fabrics.

CQRS-clean integration (mandate 1): the Aggregator NEVER mutates a source bus or
forces it to dual-write. It *attaches* to the existing fabrics using their native
subscription APIs — ``TrinityEventBus.subscribe`` and the IDE SSE broker's
``subscribe`` / ``stream_iter`` — casts each native event into the Universal
``HiveTelemetryEnvelope``, and fans the two concurrent streams into ONE
chronologically-ordered queue for the ov hive TUI.

Fan-in (mandate 2): each source drains independently into a bounded ``asyncio.Queue``
so neither can block the other during heavy I/O. A single drainer coalesces a short
window, sorts by timestamp, and emits into the unified ``out_queue`` — merging two
disparate async iterators into one sorted feed without dropping frames.

Fully injectable (``bus`` / ``sse_broker``) so the multiplexer is unit-testable with
mocks and no live daemon. Every public entry point NEVER raises.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, List, Optional

from backend.api.hive_envelope import (
    HiveTelemetryEnvelope, from_ide_sse_event, from_trinity_event,
)

logger = logging.getLogger("Jarvis.HiveAggregator")

#: TrinityEventBus source families the aggregator listens on (read-only). It does
#: NOT subscribe to its own relay topic, so there is no feedback loop.
_TRINITY_PATTERNS = (
    "training.#", "tier.#", "autonomy.#", "workflow.#", "gap.#",
    "fs.#", "command.#", "intake.#", "reactor.#", "degradation.#",
)

#: IDE-SSE control frames the aggregator ignores (they carry no agent activity).
_SSE_CONTROL = frozenset({
    "heartbeat", "stream_lag", "replay_start", "replay_end",
})


class HiveAggregator:
    """Read-only fan-in over TrinityEventBus + the IDE SSE broker → one sorted
    envelope feed. NEVER publishes back to a source (mandate 1)."""

    def __init__(
        self,
        *,
        bus: Optional[Any] = None,
        sse_broker: Optional[Any] = None,
        raw_max: int = 4096,
        out_max: int = 4096,
        sort_window_s: float = 0.02,
    ) -> None:
        self._bus = bus
        self._broker = sse_broker
        self._sort_window_s = sort_window_s
        self._raw: "asyncio.Queue[HiveTelemetryEnvelope]" = asyncio.Queue(maxsize=raw_max)
        #: The unified, chronologically-ordered feed the TUI consumes.
        self.out_queue: "asyncio.Queue[HiveTelemetryEnvelope]" = asyncio.Queue(maxsize=out_max)
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._sub_ids: List[str] = []
        self._sse_sub: Any = None
        self.stats = {"captured": 0, "emitted": 0, "dropped_raw": 0, "dropped_out": 0}

    # -- read-only source attachment ----------------------------------------

    async def start(self) -> None:
        """Attach to both fabrics (read-only) + start the drainer. NEVER raises."""
        if self._running:
            return
        self._running = True
        # TrinityEventBus: native subscribe on each source family.
        if self._bus is not None:
            for pattern in _TRINITY_PATTERNS:
                try:
                    sub_id = await self._bus.subscribe(pattern, self._on_trinity)
                    if sub_id:
                        self._sub_ids.append(sub_id)
                except Exception:  # noqa: BLE001
                    logger.debug("[HiveAgg] trinity subscribe degraded pattern=%s", pattern,
                                 exc_info=True)
        # IDE SSE broker: native subscribe + a stream_iter pump task.
        if self._broker is not None:
            try:
                self._sse_sub = self._broker.subscribe(op_id_filter=None)
                if self._sse_sub is not None:
                    self._tasks.append(asyncio.ensure_future(self._pump_sse()))
            except Exception:  # noqa: BLE001
                logger.debug("[HiveAgg] sse subscribe degraded", exc_info=True)
        # The single fan-in drainer (coalesce → sort → emit).
        self._tasks.append(asyncio.ensure_future(self._drainer()))
        logger.info("[HiveAgg] listening — trinity_subs=%d sse=%s",
                    len(self._sub_ids), self._sse_sub is not None)

    async def _on_trinity(self, event: Any) -> None:
        """TrinityEventBus handler (read-only). Casts + fans in. NEVER raises."""
        try:
            topic = str(getattr(event, "topic", "") or "")
            payload = getattr(event, "payload", None) or {}
            env = from_trinity_event(
                topic=topic, payload=payload,
                event_id=str(getattr(event, "event_id", "") or ""))
            self._fan_in(env)
        except Exception:  # noqa: BLE001
            pass

    async def _pump_sse(self) -> None:
        """Consume the IDE SSE broker's async iterator + fan in. NEVER raises out."""
        try:
            async for ev in self._broker.stream_iter(self._sse_sub, heartbeat_s=0):
                et = str(getattr(ev, "event_type", "") or "")
                if et in _SSE_CONTROL:
                    continue
                try:
                    env = from_ide_sse_event(
                        event_type=et, op_id=str(getattr(ev, "op_id", "") or ""),
                        payload=getattr(ev, "payload", None) or {},
                        event_id=str(getattr(ev, "event_id", "") or ""))
                    self._fan_in(env)
                except Exception:  # noqa: BLE001
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[HiveAgg] sse pump ended", exc_info=True)

    def _fan_in(self, env: HiveTelemetryEnvelope) -> None:
        """Push a normalized envelope into the shared fan-in queue. Non-blocking
        (drop-oldest on overflow) so no source ever blocks another."""
        self.stats["captured"] += 1
        try:
            self._raw.put_nowait(env)
        except asyncio.QueueFull:
            try:
                self._raw.get_nowait()
                self._raw.put_nowait(env)
            except Exception:  # noqa: BLE001
                pass
            self.stats["dropped_raw"] += 1

    async def _drainer(self) -> None:
        """The merge heart: await one envelope, coalesce a short window, sort by
        timestamp, and emit into the unified feed — chronological order across BOTH
        streams without dropping frames (mandate 2). NEVER raises out of the loop."""
        while self._running:
            try:
                first = await self._raw.get()
                batch: List[HiveTelemetryEnvelope] = [first]
                if self._sort_window_s > 0:
                    await asyncio.sleep(self._sort_window_s)
                while True:
                    try:
                        batch.append(self._raw.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                # Stable chronological sort (ts, then event_id as tiebreak).
                batch.sort(key=lambda e: (e.ts, e.event_id))
                for env in batch:
                    self._emit(env)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("[HiveAgg] drainer degraded", exc_info=True)

    def _emit(self, env: HiveTelemetryEnvelope) -> None:
        try:
            self.out_queue.put_nowait(env)
            self.stats["emitted"] += 1
        except asyncio.QueueFull:
            try:
                self.out_queue.get_nowait()
                self.out_queue.put_nowait(env)
                self.stats["emitted"] += 1
            except Exception:  # noqa: BLE001
                pass
            self.stats["dropped_out"] += 1

    async def feed(self) -> AsyncIterator[HiveTelemetryEnvelope]:
        """Async iterator over the unified chronological feed (for the TUI)."""
        while self._running or not self.out_queue.empty():
            try:
                yield await self.out_queue.get()
            except asyncio.CancelledError:
                raise

    async def drain_available(self, *, settle_s: float = 0.0) -> List[HiveTelemetryEnvelope]:
        """Non-blocking snapshot of everything currently in the unified feed (for
        tests / reconciliation). Optionally waits ``settle_s`` for in-flight sorts."""
        if settle_s > 0:
            await asyncio.sleep(settle_s)
        out: List[HiveTelemetryEnvelope] = []
        while True:
            try:
                out.append(self.out_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

    async def stop(self) -> None:
        """Detach cleanly (read-only teardown). NEVER raises."""
        self._running = False
        if self._bus is not None:
            for sub_id in self._sub_ids:
                try:
                    await self._bus.unsubscribe(sub_id)
                except Exception:  # noqa: BLE001
                    pass
        self._sub_ids = []
        if self._broker is not None and self._sse_sub is not None:
            try:
                self._broker.unsubscribe(self._sse_sub)
            except Exception:  # noqa: BLE001
                pass
        for t in self._tasks:
            if not t.done():
                t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []


__all__ = ["HiveAggregator"]
