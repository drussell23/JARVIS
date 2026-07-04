from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Callable, Optional

from aiohttp import WSMsgType, web

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_HEARTBEAT,
    EVENT_TYPE_REPLAY_END,
    EVENT_TYPE_REPLAY_START,
    EVENT_TYPE_STREAM_LAG,
    StreamEvent,
    StreamEventBroker,
)
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport import bus_frame as bf

logger = logging.getLogger(__name__)

_DEDUP_MAX = 8192  # bounded seen-id memory

# Broker-internal bookkeeping event types. These are produced by
# StreamEventBroker.subscribe()/stream_iter() for SSE-side replay
# framing and are not part of the peer-to-peer bus wire protocol
# (bus_frame.py only knows hello/event/heartbeat/ack). replay_start
# and replay_end are dropped; heartbeat is translated to the wire
# heartbeat frame kind rather than forwarded as a generic event.
_CONTROL_EVENT_TYPES = frozenset({EVENT_TYPE_REPLAY_START, EVENT_TYPE_REPLAY_END, EVENT_TYPE_STREAM_LAG})


class BusBridgeServer:
    """Hosts the WS endpoint that bridges a local StreamEventBroker to a
    remote peer. Server-authoritative history + Last-Event-ID replay come
    from the broker; inbound peer events are deduped by qualified id and
    republished locally."""

    def __init__(
        self,
        broker: StreamEventBroker,
        cfg: TransportConfig,
        *,
        on_inbound: Optional[Callable[[StreamEvent], None]] = None,
    ) -> None:
        self._broker = broker
        self._cfg = cfg
        self._on_inbound = on_inbound
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        # Local event ids minted by the inbound republish (on_inbound returns the
        # broker.publish id). The outbound pump must skip them or peer events
        # bounce back with fresh ids forever (reflection storm, live-fire 2026-07-04).
        self._republished: "OrderedDict[str, None]" = OrderedDict()

    def seen_count(self) -> int:
        return len(self._seen)

    def _mark_republished(self, eid: str) -> None:
        self._republished[eid] = None
        if len(self._republished) > _DEDUP_MAX:
            self._republished.popitem(last=False)

    def register_routes(self, app: web.Application) -> None:
        app.router.add_get(self._cfg.path, self._handle_ws)

    def _mark_seen(self, qid: str) -> bool:
        """Return True if NEW (not seen before). Bounded FIFO eviction."""
        if qid in self._seen:
            return False
        self._seen[qid] = None
        if len(self._seen) > _DEDUP_MAX:
            self._seen.popitem(last=False)
        return True

    def _ingest(self, frame: bf.BusFrame) -> None:
        ev_dict = frame.event or {}
        event_id = ev_dict.get("event_id", "")
        qid = bf.qualified_id(frame.source_id, event_id)
        if not self._mark_seen(qid):
            return  # idempotent -- already applied
        ev = StreamEvent(
            event_id=event_id,
            event_type=ev_dict.get("event_type", ""),
            op_id=ev_dict.get("op_id", ""),
            timestamp=ev_dict.get("timestamp", ""),
            payload=ev_dict.get("payload", {}) or {},
        )
        try:
            if self._on_inbound is not None:
                local_eid = self._on_inbound(ev)
                # DistributedEventBus's on_inbound returns the republish's local
                # event id -- record it so the outbound pump never reflects it.
                if isinstance(local_eid, str) and local_eid:
                    self._mark_republished(local_eid)
        except Exception:  # noqa: BLE001 -- never crash the WS loop
            logger.debug("[BusBridgeServer] on_inbound raised", exc_info=True)

    async def _pump_outbound(self, ws: web.WebSocketResponse, last_event_id: Optional[str]) -> None:
        """Subscribe to the broker and stream events (with replay) to the
        peer. Replay seeding, heartbeat cadence, and drop-oldest
        backpressure all come from StreamEventBroker.subscribe()/
        stream_iter() -- this method only translates StreamEvent ->
        BusFrame and writes bytes to the socket."""
        sub = self._broker.subscribe(op_id_filter=None, last_event_id=last_event_id)
        if sub is None:
            return
        try:
            hb = self._cfg.heartbeat_s
            async for event in self._broker.stream_iter(sub, heartbeat_s=hb):
                if ws.closed:
                    break
                if event.event_type in _CONTROL_EVENT_TYPES:
                    continue  # broker-internal replay/lag bookkeeping, not on-wire
                if event.event_type == EVENT_TYPE_HEARTBEAT:
                    await ws.send_bytes(bf.heartbeat_frame(self._cfg.source_id).encode())
                    continue
                if event.event_id in self._republished:
                    continue  # never reflect a peer's own event back at it
                frame = bf.event_frame(event, source_id=self._cfg.source_id)
                await ws.send_bytes(frame.encode())
        except (asyncio.CancelledError, ConnectionResetError):
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[BusBridgeServer] outbound pump ended", exc_info=True)

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        pump_task: Optional[asyncio.Task] = None
        try:
            async for msg in ws:
                if msg.type != WSMsgType.BINARY and msg.type != WSMsgType.TEXT:
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                        break
                    continue
                frame = bf.BusFrame.decode(msg.data)
                if frame is None:
                    continue
                if frame.kind == bf.FRAME_HELLO and pump_task is None:
                    pump_task = asyncio.ensure_future(
                        self._pump_outbound(ws, frame.last_event_id)
                    )
                elif frame.kind == bf.FRAME_EVENT:
                    self._ingest(frame)
                elif frame.kind == bf.FRAME_HEARTBEAT:
                    await ws.send_bytes(bf.heartbeat_frame(self._cfg.source_id).encode())
                # FRAME_ACK is plumbed for Stage 3 WAL trim; no-op here.
        finally:
            if pump_task is not None:
                pump_task.cancel()
                try:
                    await pump_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        return ws
