"""TrinityBusBridge -- the Stage-2 adapter the Stage-0 docstring promised.

Mirrors allowlisted TrinityEventBus topics onto a StreamEventBroker (which the
Stage-0/1 DistributedEventBus carries across the mTLS WS) and republishes
imported events into the local TrinityEventBus. Loop safety is ORIGIN-BASED:
imported events carry ``metadata.bridge_origin`` and are never re-forwarded --
the Stage-1 reflection-storm class, closed at this layer by construction.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TRINITY_OP_PREFIX = "trinity:"
_BROKER_EVENT_TYPE = "task_started"  # a valid StreamEventBroker type
_META_ORIGIN = "bridge_origin"


class TrinityBusBridge:
    """Origin-tagged adapter between a real ``TrinityEventBus`` and a
    ``StreamEventBroker``.

    Outbound: subscribes the trinity bus for each pattern in
    ``outbound_topics`` and mirrors matching events onto the broker (unless
    the event was itself imported from the far side -- origin check).

    Inbound: drains the broker's subscriber queue for ``trinity:``-prefixed
    op_ids not originated locally, and republishes them into the local
    trinity bus tagged with ``metadata.bridge_origin`` so the outbound side
    never bounces them back.
    """

    def __init__(self, trinity_bus: Any, broker: Any, *,
                 outbound_topics: List[str], source_id: str) -> None:
        self._bus = trinity_bus
        self._broker = broker
        self._outbound_topics = list(outbound_topics)
        self._source_id = source_id
        self._sub_ids: List[str] = []
        self._drain_task: Optional[asyncio.Task] = None
        self._broker_sub = None

    async def start(self) -> None:
        for pattern in self._outbound_topics:
            sid = await self._bus.subscribe(pattern, self._on_outbound)
            self._sub_ids.append(sid)
        self._broker_sub = self._broker.subscribe()
        if self._broker_sub is None:
            raise RuntimeError("broker subscriber cap exceeded")
        self._drain_task = asyncio.ensure_future(self._drain_inbound())

    async def stop(self) -> None:
        for sid in self._sub_ids:
            try:
                await self._bus.unsubscribe(sid)
            except Exception:  # noqa: BLE001 -- fail-soft, never crash on teardown
                logger.debug("[TrinityBusBridge] unsubscribe failed", exc_info=True)
        self._sub_ids.clear()
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.debug("[TrinityBusBridge] drain task teardown error", exc_info=True)
            self._drain_task = None
        if self._broker_sub is not None:
            try:
                self._broker.unsubscribe(self._broker_sub)
            except Exception:  # noqa: BLE001
                logger.debug("[TrinityBusBridge] broker unsubscribe failed", exc_info=True)
            self._broker_sub = None

    async def _on_outbound(self, event: Any) -> None:
        origin = (getattr(event, "metadata", None) or {}).get(_META_ORIGIN)
        if origin and origin != self._source_id:
            return  # imported -- NEVER re-forward (loop safety)
        try:
            self._broker.publish(
                _BROKER_EVENT_TYPE,
                TRINITY_OP_PREFIX + str(event.topic),
                {"topic": str(event.topic),
                 "data": dict(event.payload or {}),
                 "origin": self._source_id},
            )
        except Exception:  # noqa: BLE001 -- fail-soft, never crash the bus loop
            logger.debug("[TrinityBusBridge] outbound publish failed", exc_info=True)

    async def _drain_inbound(self) -> None:
        from backend.core.trinity_event_bus import TrinityEvent  # noqa: PLC0415

        while True:
            ev = await self._broker_sub.queue.get()
            op_id = getattr(ev, "op_id", "") or ""
            if not op_id.startswith(TRINITY_OP_PREFIX):
                continue
            payload: Dict[str, Any] = dict(getattr(ev, "payload", None) or {})
            if payload.get("origin") == self._source_id:
                continue  # our own outbound reflected by the broker -- skip
            try:
                tev = TrinityEvent(
                    topic=str(payload.get("topic", "")),
                    payload=dict(payload.get("data", {}) or {}),
                    metadata={_META_ORIGIN: str(payload.get("origin", ""))},
                )
                await self._bus.publish(tev)
            except Exception:  # noqa: BLE001 -- fail-soft, never crash the bus loop
                logger.debug("[TrinityBusBridge] inbound republish failed",
                             exc_info=True)
