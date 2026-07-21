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
from typing import Any, AsyncIterator, List, Optional, Tuple

from backend.api.hive_envelope import (
    HiveTelemetryEnvelope, from_ide_sse_event, from_trinity_event,
)

logger = logging.getLogger("Jarvis.HiveAggregator")


def _env_float(name: str, default: float) -> float:
    """Env-tunable float with junk-value fallback (repo idiom). NEVER raises."""
    import os
    try:
        raw = os.environ.get(name, "")
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default

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
        bus_resolver: Optional[Any] = None,
        emitter: Optional[Any] = None,
        raw_max: int = 4096,
        out_max: int = 4096,
        sort_window_s: float = 0.02,
    ) -> None:
        self._bus = bus
        self._broker = sse_broker
        #: Third fabric (Step 2): the process-local HiveEmitter edge where the
        #: silent actors (MCP/web/voice/contexts/ghost-hands/vision/memory)
        #: emit. Already-envelope — drained read-only, no cast needed.
        self._emitter = emitter
        #: Bus-storm compression (Step 2): non-actor fabrics coalesce identical
        #: (actor, intent) bursts through the SAME EdgeDebouncer implementation
        #: the emitter uses (mandate 3) — the finalizer amends the FIRST
        #: envelope rather than rebuilding it, so subclass kinds survive.
        self._bus_coalescer: Optional[Any] = None
        import os
        if os.environ.get("JARVIS_HIVE_BUS_COALESCE_ENABLED",
                          "true").strip().lower() == "true":
            try:
                from backend.api.hive_emitter import EdgeDebouncer

                def _bus_finalizer(env: Any, count: int, span_ms: float,
                                   first_ts: float, severity: str) -> Any:
                    if count <= 1:
                        return env
                    try:
                        return env.model_copy(update={
                            "action_summary": (f"{env.action_summary} "
                                               f"(×{count} in {span_ms:.0f}ms)"),
                            "severity": severity,
                        })
                    except Exception:  # noqa: BLE001
                        return env

                self._bus_coalescer = EdgeDebouncer(
                    self._fan_in_direct, finalizer=_bus_finalizer)
            except Exception:  # noqa: BLE001
                self._bus_coalescer = None
        #: Late-binding source resolution: when ``bus`` is None at start (the
        #: TrinityEventBus singleton is created lazily, often AFTER cockpit
        #: boot), a resolver lets the aggregator re-attach the moment the bus
        #: materializes instead of silently losing that fabric forever.
        self._bus_resolver = bus_resolver
        self._sort_window_s = sort_window_s
        self._raw: "asyncio.Queue[HiveTelemetryEnvelope]" = asyncio.Queue(maxsize=raw_max)
        #: The unified, chronologically-ordered feed the TUI consumes.
        self.out_queue: "asyncio.Queue[HiveTelemetryEnvelope]" = asyncio.Queue(maxsize=out_max)
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._sub_ids: List[str] = []
        #: Read-only feed observers (Step 3 layer-2 deliberator). See tap().
        self._taps: List[Any] = []
        #: Owned children stopped with the aggregator (e.g. the council
        #: deliberator) — anything exposing ``async stop()``.
        self._children: List[Any] = []
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
            await self._subscribe_trinity()
        elif self._bus_resolver is not None:
            # The bus doesn't exist yet — poll the resolver with adaptive
            # backoff and attach the moment it materializes (fixes the
            # trinity_subs=0 boot-ordering gap).
            self._tasks.append(asyncio.ensure_future(self._bus_reattach_loop()))
        # IDE SSE broker: native subscribe + a stream_iter pump task.
        if self._broker is not None:
            try:
                self._sse_sub = self._broker.subscribe(op_id_filter=None)
                if self._sse_sub is not None:
                    self._tasks.append(asyncio.ensure_future(self._pump_sse()))
            except Exception:  # noqa: BLE001
                logger.debug("[HiveAgg] sse subscribe degraded", exc_info=True)
        # HiveEmitter edge (Step 2): drain the actor-emission queue read-only.
        if self._emitter is not None:
            try:
                self._emitter.bind_loop()
                self._tasks.append(asyncio.ensure_future(self._pump_emitter()))
            except Exception:  # noqa: BLE001
                logger.debug("[HiveAgg] emitter attach degraded", exc_info=True)
        # The single fan-in drainer (coalesce → sort → emit).
        self._tasks.append(asyncio.ensure_future(self._drainer()))
        logger.info("[HiveAgg] listening — trinity_subs=%d sse=%s emitter=%s",
                    len(self._sub_ids), self._sse_sub is not None,
                    self._emitter is not None)

    async def _subscribe_trinity(self) -> None:
        """Native read-only subscribe on each Trinity source family. NEVER raises."""
        for pattern in _TRINITY_PATTERNS:
            try:
                sub_id = await self._bus.subscribe(pattern, self._on_trinity)
                if sub_id:
                    self._sub_ids.append(sub_id)
            except Exception:  # noqa: BLE001
                logger.debug("[HiveAgg] trinity subscribe degraded pattern=%s", pattern,
                             exc_info=True)

    async def _bus_reattach_loop(self) -> None:
        """Adaptive late-attach: the TrinityEventBus singleton is created lazily
        by whichever subsystem needs it first — often AFTER the cockpit (and this
        aggregator) boots. Poll the injected resolver with exponential backoff
        (cheap: one function call per tick) and subscribe the moment the bus
        exists. Exits after attaching. NEVER raises out."""
        delay = _env_float("JARVIS_HIVE_BUS_REATTACH_MIN_S", 1.0)
        ceiling = _env_float("JARVIS_HIVE_BUS_REATTACH_MAX_S", 30.0)
        while self._running and self._bus is None:
            try:
                bus = self._bus_resolver()
            except Exception:  # noqa: BLE001
                bus = None
            if bus is not None:
                self._bus = bus
                await self._subscribe_trinity()
                logger.info("[HiveAgg] trinity bus materialized — re-attached "
                            "(subs=%d)", len(self._sub_ids))
                return
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, ceiling)

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

    async def _pump_emitter(self) -> None:
        """Drain the HiveEmitter edge (already-envelope) into the fan-in.
        Read-only: the emitter never learns who consumes it. NEVER raises out."""
        try:
            while self._running:
                env = await self._emitter.out_queue.get()
                self._fan_in(env)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[HiveAgg] emitter pump ended", exc_info=True)

    def _fan_in(self, env: HiveTelemetryEnvelope) -> None:
        """Push a normalized envelope into the shared fan-in queue. Non-blocking
        (drop-oldest on overflow) so no source ever blocks another.

        Bus-storm compression: identical (actor, intent) bursts from the
        NON-actor fabrics (the live governor-throttle storm class: 50+
        identical frames in one second) coalesce through the SAME EdgeDebouncer
        windows the emitter uses — actor_edge envelopes were already debounced
        at the edge and pass straight through."""
        self.stats["captured"] += 1
        if (self._bus_coalescer is not None
                and env.source_fabric != "actor_edge"):
            try:
                self._bus_coalescer.accept((env.actor_id, env.intent), env,
                                           severity=env.severity)
                return
            except Exception:  # noqa: BLE001
                pass
        self._fan_in_direct(env)

    def _fan_in_direct(self, env: HiveTelemetryEnvelope) -> None:
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

    def attach_child(self, child: Any) -> None:
        """Adopt a component whose lifecycle ends with this aggregator's
        (``async stop()`` contract). Keeps host teardown single-seam."""
        self._children.append(child)

    def tap(self, callback: Any) -> None:
        """Register a READ-ONLY observer of the unified feed (Step 3: the
        persona-council deliberator listens here). Callbacks are sync,
        bounded by construction (fire-and-forget, exceptions swallowed),
        and can never consume, reorder, or block the primary out_queue —
        the relay stays the sole queue consumer (CQRS intact)."""
        self._taps.append(callback)

    def _emit(self, env: HiveTelemetryEnvelope) -> None:
        for cb in self._taps:
            try:
                cb(env)
            except Exception:  # noqa: BLE001 — a tap can never hurt the feed
                pass
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
        for child in self._children:
            try:
                await child.stop()
            except Exception:  # noqa: BLE001
                pass
        self._children = []
        if self._bus_coalescer is not None:
            try:
                self._bus_coalescer.flush_all()
            except Exception:  # noqa: BLE001
                pass
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


_UNSET = object()


async def start_hive_relay(
    publish: Any,
    *,
    bus: Any = _UNSET,
    sse_broker: Any = _UNSET,
) -> Optional[Tuple[HiveAggregator, "asyncio.Task"]]:
    """Start a read-only :class:`HiveAggregator` and relay its unified feed to ONE
    cockpit publish surface (``CockpitAttachBridge.publish_telemetry``) as
    ``hive``-tagged frames.

    This is the shared wiring for EVERY pipeline host — the converged
    ``--headless`` organism AND the battle-test harness — so ``ov hive``
    projects the pipeline from whichever process owns the cockpit socket
    (DRY, mandate 3). Sources default to the process-local fabrics; both are
    injectable for tests. Read-only (mandate 1). NEVER raises — returns
    ``None`` when degraded.
    """
    try:
        resolver = None
        if bus is _UNSET:
            try:
                from backend.core.trinity_event_bus import get_event_bus_if_exists
                bus = get_event_bus_if_exists()
                # Late-binding: the singleton is often created AFTER cockpit
                # boot — hand the aggregator the resolver so it re-attaches
                # the moment the bus materializes (trinity_subs=0 fix).
                resolver = get_event_bus_if_exists
            except Exception:  # noqa: BLE001
                bus = None
        if sse_broker is _UNSET:
            try:
                from backend.core.ouroboros.governance.ide_observability_stream import (
                    get_default_broker,
                )
                sse_broker = get_default_broker()
            except Exception:  # noqa: BLE001
                sse_broker = None
        try:
            from backend.api.hive_emitter import get_default_emitter
            emitter = get_default_emitter()
        except Exception:  # noqa: BLE001
            emitter = None
        agg = HiveAggregator(bus=bus, sse_broker=sse_broker,
                             bus_resolver=resolver, emitter=emitter)
        await agg.start()

        # Step 3 (default-OFF): the layer-2 persona-council deliberator taps
        # the unified feed read-only and speaks back through the emission
        # edge. Wired here so BOTH hosts get it (DRY); a council fault can
        # never touch the feed.
        try:
            from backend.api.hive_council_layer import (
                CouncilDeliberator, council_enabled,
            )
            if council_enabled():
                council = CouncilDeliberator()
                if await council.start():
                    agg.tap(council.on_envelope)
                    agg.attach_child(council)
        except Exception:  # noqa: BLE001
            logger.debug("[HiveAgg] council layer degraded", exc_info=True)

        async def _relay() -> None:
            try:
                async for env in agg.feed():
                    try:
                        publish({"hive": True, **env.to_bus_payload()})
                    except Exception:  # noqa: BLE001
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

        task = asyncio.create_task(_relay(), name="hive-relay")
        logger.info("[HiveAgg] relay up — `ov hive` can project this pipeline")
        return agg, task
    except Exception:  # noqa: BLE001
        logger.debug("[HiveAgg] relay start degraded", exc_info=True)
        return None


__all__ = ["HiveAggregator", "start_hive_relay"]
