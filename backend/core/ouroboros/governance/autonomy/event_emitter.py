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
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Union

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

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[EventHandler]] = defaultdict(list)
        self._last_event_id: Optional[str] = None

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
