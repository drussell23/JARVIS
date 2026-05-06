"""backend/core/ouroboros/governance/autonomy/event_emitter.py

Event Emitter — Pub-Sub for L1 Outcome Events (Task 3, C+ Autonomous Loop).

L1 (GLS) emits events downward to advisory layers (L2/L3/L4).  Each layer
subscribes to the event types it cares about and reacts accordingly.

Design:
    - Pub-sub keyed by EventType (str enum).
    - Multiple subscribers per event type, stored in registration order.
    - emit() delivers to all matching subscribers concurrently.
    - Subscriber errors are fault-isolated: logged, never propagated,
      and never block delivery to other subscribers.
    - Supports both async and sync handler callables.
    - last_event_id property tracks the most recently emitted event
      for cursor-based consumption patterns.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import weakref
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Union

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    EventEnvelope,
    EventType,
)

logger = logging.getLogger(__name__)

# Handler type: accepts an EventEnvelope, returns nothing.
# Can be either sync or async.
EventHandler = Callable[[EventEnvelope], None]


class EventEmitter:
    """Pub-sub event emitter for L1 outcome events.

    Parameters
    ----------
    None — the emitter is stateless beyond its subscriber registry
    and cursor.

    Usage::

        emitter = EventEmitter()

        async def on_op_completed(event: EventEnvelope) -> None:
            print(f"Op {event.op_id} completed: {event.payload}")

        emitter.subscribe(EventType.OP_COMPLETED, on_op_completed)

        await emitter.emit(EventEnvelope(
            source_layer="L1",
            event_type=EventType.OP_COMPLETED,
            payload={"result": "success"},
            op_id="abc-123",
        ))
    """

    # Path D.3 (PRD §36.6, 2026-05-05) — class-level weak-ref
    # registry of live EventEmitter instances. EventEmitter has
    # no global singleton (multiple callers construct their own:
    # governed_loop_service, safety_net, execution_graph_progress).
    # The Class-level Instance Registry pattern lets operator
    # surfaces (`/events` + `/observability/events`) aggregate
    # metrics across ALL live emitters without forcing a single-
    # instance contract.
    #
    # WeakSet keeps refs without preventing GC — orphaned emitters
    # drop out automatically. Class-level so it shares across all
    # subclasses + instances.
    _INSTANCES: "weakref.WeakSet[EventEmitter]" = weakref.WeakSet()

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[EventHandler]] = defaultdict(list)
        self._last_event_id: Optional[str] = None
        # Path D.3 — per-event-type emission counter.
        # Lightweight Dict[str, int] incremented inside emit().
        # NEVER mutated by readers (snapshot deep-copies).
        self._event_counts: Dict[str, int] = defaultdict(int)
        # Register self for cross-instance aggregation.
        EventEmitter._INSTANCES.add(self)

    # ------------------------------------------------------------------
    # subscribe
    # ------------------------------------------------------------------

    def subscribe(self, event_type: Union[EventType, str], handler: EventHandler) -> None:
        """Register *handler* to be called whenever an event of *event_type* is emitted.

        Multiple handlers can be registered for the same event type.
        Handlers are invoked in registration order.

        Parameters
        ----------
        event_type:
            The event type to subscribe to (EventType enum or its str value).
        handler:
            A callable accepting a single ``EventEnvelope`` argument.
            May be sync or async.
        """
        key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        self._subscribers[key].append(handler)
        logger.debug(
            "EventEmitter: subscribed handler %s to event_type=%s (total=%d)",
            getattr(handler, "__name__", repr(handler)),
            key,
            len(self._subscribers[key]),
        )

    # ------------------------------------------------------------------
    # emit
    # ------------------------------------------------------------------

    async def emit(self, event: EventEnvelope) -> None:
        """Emit *event* to all subscribers registered for its event type.

        - Delivery is concurrent (all handlers are gathered).
        - Each handler is fault-isolated: if a handler raises, the error
          is logged but does not propagate or block other handlers.
        - The ``last_event_id`` cursor is updated regardless of handler
          outcomes.

        Parameters
        ----------
        event:
            The event envelope to deliver.
        """
        key = (
            event.event_type.value
            if isinstance(event.event_type, EventType)
            else str(event.event_type)
        )
        handlers = self._subscribers.get(key, [])

        if handlers:
            logger.debug(
                "EventEmitter: emitting event_id=%s type=%s to %d subscriber(s)",
                event.event_id,
                key,
                len(handlers),
            )
            # Fan out to all handlers concurrently with fault isolation
            await asyncio.gather(
                *(self._invoke_handler(handler, event) for handler in handlers),
            )
        else:
            logger.debug(
                "EventEmitter: emitting event_id=%s type=%s (no subscribers)",
                event.event_id,
                key,
            )

        # Always update cursor, even if no subscribers or if handlers failed
        self._last_event_id = event.event_id
        # Path D.3 — increment per-event-type emission counter
        # AFTER fan-out so the count reflects "delivered" rather
        # than "attempted." Counter increment is constant-time +
        # never raises (defaultdict + int).
        self._event_counts[key] += 1

        # Phase 4 Event Spine: bridge to TrinityEventBus for unified visibility
        await self._bridge_to_spine(event)

    async def _bridge_to_spine(self, event: EventEnvelope) -> None:
        """Forward autonomy event to TrinityEventBus (non-fatal)."""
        try:
            from backend.core.trinity_event_bus import get_event_bus_if_exists
            bus = get_event_bus_if_exists()
            if bus is None:
                return
            key = (
                event.event_type.value
                if isinstance(event.event_type, EventType)
                else str(event.event_type)
            )
            await bus.publish_raw(
                topic=f"autonomy.{key}",
                data=event.payload if hasattr(event, "payload") else {},
                persist=False,  # High-volume autonomy events skip WAL
            )
        except Exception:
            pass  # Bridge failures are non-fatal

    # ------------------------------------------------------------------
    # last_event_id
    # ------------------------------------------------------------------

    @property
    def last_event_id(self) -> Optional[str]:
        """Return the event_id of the most recently emitted event, or None if
        no events have been emitted yet.

        Useful for cursor-based tracking by consumers that want to know
        which events they have already processed.
        """
        return self._last_event_id

    # ------------------------------------------------------------------
    # subscriber_count
    # ------------------------------------------------------------------

    def subscriber_count(self, event_type: Union[EventType, str]) -> int:
        """Return the number of handlers subscribed to *event_type*."""
        key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        return len(self._subscribers.get(key, []))

    # ------------------------------------------------------------------
    # Path D.3 — operator-visibility read API
    # ------------------------------------------------------------------

    def metrics_snapshot(self) -> Dict[str, Any]:
        """Return a per-event-type metrics snapshot. Pure-read;
        deep-copies counters so the caller cannot mutate
        instance state. NEVER raises.

        Shape::

            {
                "last_event_id": "<uuid>" | None,
                "total_emissions": <int>,
                "total_subscribers": <int>,
                "by_event_type": {
                    "op_completed": {
                        "subscriber_count": 2,
                        "emission_count": 47,
                    },
                    ...
                },
            }
        """
        try:
            event_types = (
                set(self._subscribers.keys())
                | set(self._event_counts.keys())
            )
            by_type: Dict[str, Dict[str, int]] = {}
            for et in sorted(event_types):
                by_type[et] = {
                    "subscriber_count": len(
                        self._subscribers.get(et, []),
                    ),
                    "emission_count": int(
                        self._event_counts.get(et, 0),
                    ),
                }
            total_emissions = sum(
                int(v) for v in self._event_counts.values()
            )
            total_subscribers = sum(
                len(v) for v in self._subscribers.values()
            )
            return {
                "last_event_id": self._last_event_id,
                "total_emissions": total_emissions,
                "total_subscribers": total_subscribers,
                "by_event_type": by_type,
            }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "last_event_id": None,
                "total_emissions": 0,
                "total_subscribers": 0,
                "by_event_type": {},
            }

    @classmethod
    def snapshot_all(cls) -> Dict[str, Any]:
        """Aggregate metrics across ALL live EventEmitter
        instances. Composes :meth:`metrics_snapshot` per
        instance + merges. NEVER raises.

        EventEmitter has no global singleton (multiple callers
        construct their own); this aggregate snapshot is the
        operator-surface alternative. Live instances are tracked
        via the class-level :data:`_INSTANCES` WeakSet — orphaned
        emitters drop out automatically.

        Shape::

            {
                "instance_count": <int>,
                "total_emissions": <int>,
                "total_subscribers": <int>,
                "by_event_type": {
                    "op_completed": {
                        "subscriber_count": <sum>,
                        "emission_count": <sum>,
                    },
                    ...
                },
            }
        """
        try:
            instances = list(cls._INSTANCES)
            if not instances:
                return {
                    "instance_count": 0,
                    "total_emissions": 0,
                    "total_subscribers": 0,
                    "by_event_type": {},
                }
            agg_by_type: Dict[str, Dict[str, int]] = defaultdict(
                lambda: {"subscriber_count": 0, "emission_count": 0},
            )
            total_emissions = 0
            total_subscribers = 0
            for inst in instances:
                try:
                    snap = inst.metrics_snapshot()
                except Exception:  # noqa: BLE001 — defensive
                    continue
                total_emissions += int(
                    snap.get("total_emissions", 0),
                )
                total_subscribers += int(
                    snap.get("total_subscribers", 0),
                )
                for et, m in snap.get(
                    "by_event_type", {},
                ).items():
                    agg_by_type[et]["subscriber_count"] += int(
                        m.get("subscriber_count", 0),
                    )
                    agg_by_type[et]["emission_count"] += int(
                        m.get("emission_count", 0),
                    )
            return {
                "instance_count": len(instances),
                "total_emissions": total_emissions,
                "total_subscribers": total_subscribers,
                "by_event_type": dict(agg_by_type),
            }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "instance_count": 0,
                "total_emissions": 0,
                "total_subscribers": 0,
                "by_event_type": {},
            }

    @classmethod
    def reset_instance_registry_for_tests(cls) -> None:
        """Test-only — clear the WeakSet of live instances.
        Production code MUST NOT call this (would orphan
        all live emitters from operator surfaces). Mirrors
        the reset_default_observer_for_tests / reset_default_
        monitor_for_tests pattern."""
        cls._INSTANCES = weakref.WeakSet()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    async def _invoke_handler(handler: EventHandler, event: EventEnvelope) -> None:
        """Invoke a single handler with full fault isolation.

        If the handler is a coroutine function, it is awaited.
        If it is a regular function, it is called directly.
        Any exception is logged and swallowed.
        """
        try:
            if inspect.iscoroutinefunction(handler):
                await handler(event)
            else:
                handler(event)
        except Exception:
            logger.exception(
                "EventEmitter: subscriber %s raised for event_id=%s type=%s — "
                "error isolated, delivery continues",
                getattr(handler, "__name__", repr(handler)),
                event.event_id,
                event.event_type.value
                if isinstance(event.event_type, EventType)
                else str(event.event_type),
            )
