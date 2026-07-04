from __future__ import annotations

import asyncio
import logging
import random
from collections import OrderedDict
from typing import Callable, Optional

import aiohttp

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_HEARTBEAT,
    EVENT_TYPE_REPLAY_END,
    EVENT_TYPE_REPLAY_START,
    EVENT_TYPE_STREAM_LAG,
    StreamEventBroker,
)
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.transport_security import (
    build_client_ssl_context,
)
from backend.core.ouroboros.governance.transport import bus_frame as bf

logger = logging.getLogger(__name__)

_DEDUP_MAX = 8192  # bounded seen-id memory (mirrors bus_bridge_server.py)

# Broker-internal bookkeeping event types. Same rationale as
# bus_bridge_server.py: these are produced by
# StreamEventBroker.subscribe()/stream_iter() for SSE-side replay
# framing and are not part of the peer-to-peer bus wire protocol
# (bus_frame.py only knows hello/event/heartbeat/ack). replay_start
# and replay_end are dropped; heartbeat is translated to the wire
# heartbeat frame kind rather than forwarded as a generic event.
_CONTROL_EVENT_TYPES = frozenset({EVENT_TYPE_REPLAY_START, EVENT_TYPE_REPLAY_END, EVENT_TYPE_STREAM_LAG})


class BusBridgeClient:
    """Connects to a BusBridgeServer, resumes via Last-Event-ID, and
    mirrors the two brokers. Reconnect is exp-backoff + jitter; a
    heartbeat-window miss flips degraded mode deterministically."""

    def __init__(
        self,
        broker: StreamEventBroker,
        cfg: TransportConfig,
        *,
        url: Optional[str] = None,
        session_factory: Optional[Callable[[], aiohttp.ClientSession]] = None,
        initial_last_sent_id: Optional[str] = None,
    ) -> None:
        self._broker = broker
        self._cfg = cfg
        self._url = url
        self._session_factory = session_factory
        self._stopped = False
        self._last_event_id: Optional[str] = None
        # Outbound replay cursor: the last local event actually SENT to the peer.
        # A reconnect re-subscribes from here so events published while severed
        # cross client->server via broker replay (the server dedups overlaps).
        # None = first connect (live-only, legacy behavior). A recreated client
        # (DistributedEventBus.start_client after stop) inherits the previous
        # instance's cursor via ``initial_last_sent_id`` -- otherwise the
        # severed span is silently lost (live-fire attempt 2, 2026-07-04).
        self._last_sent_id: Optional[str] = initial_last_sent_id
        self._high_water: int = 0
        self._degraded = False
        self._missed_hb = 0
        self._connected = False
        self._active_ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        # Local event ids WE minted by republishing peer events. The outbound
        # pump must skip these or they bounce back with fresh ids forever
        # (reflection storm: 408x amplification observed live).
        self._republished: "OrderedDict[str, None]" = OrderedDict()

    @property
    def connected(self) -> bool:
        """True while a WS link is established (hello sent, pumps running)."""
        return self._connected

    def _mark_republished(self, eid: str) -> None:
        self._republished[eid] = None
        if len(self._republished) > _DEDUP_MAX:
            self._republished.popitem(last=False)

    @property
    def last_event_id(self) -> Optional[str]:
        return self._last_event_id

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _next_backoff(self, attempt: int) -> float:
        base = min(self._cfg.reconnect_base_s * (2 ** attempt), self._cfg.reconnect_max_s)
        if self._cfg.reconnect_jitter <= 0:
            return base
        span = base * self._cfg.reconnect_jitter
        return base + random.uniform(-span, span)

    def _advance_contiguous(self, event_id: str) -> None:
        """Advance the contiguous high-water mark ONLY when event_id is
        exactly one past the current high-water. A gap is left
        unfilled -- the next reconnect's ``hello`` replays the missing
        span via Last-Event-ID. Do NOT simplify to "track the last id
        seen"; that would defeat gap resolution."""
        try:
            seq = int(event_id, 16)
        except (ValueError, TypeError):
            return
        if seq == self._high_water + 1:
            self._high_water = seq
            self._last_event_id = event_id

    def _mark_seen(self, qid: str) -> bool:
        """Return True if NEW (not seen before). Bounded FIFO eviction --
        mirrors BusBridgeServer._mark_seen so the client's dedup memory
        cannot grow unbounded across a long-lived session."""
        if qid in self._seen:
            return False
        self._seen[qid] = None
        if len(self._seen) > _DEDUP_MAX:
            self._seen.popitem(last=False)
        return True

    def _resolve_url(self) -> str:
        if self._url:
            return self._url
        scheme = "wss" if self._cfg.tls_enabled else "ws"
        host = self._cfg.host or "127.0.0.1"
        return f"{scheme}://{host}:{self._cfg.port}{self._cfg.path}"

    async def stop(self) -> None:
        """Stop AND sever promptly: close the live WS so the pumps end now --
        a flag-only stop leaves the link flushing until the next receive
        timeout, which is not a sever (live-fire honesty gate, 2026-07-04)."""
        self._stopped = True
        ws = self._active_ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._connected = False

    async def run(self) -> None:
        attempt = 0
        while not self._stopped:
            try:
                await self._connect_once()
                attempt = 0  # clean disconnect resets backoff
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- any drop -> reconnect
                logger.debug("[BusBridgeClient] connection ended", exc_info=True)
            if self._stopped:
                break
            delay = self._next_backoff(attempt)
            attempt += 1
            await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        ssl_ctx = build_client_ssl_context(self._cfg)
        session = (self._session_factory() if self._session_factory
                   else aiohttp.ClientSession())
        # When discovery dialed a raw IP, the server cert's SAN is the DNS
        # identity (e.g. jarvis-brain) -- verify against it explicitly. None
        # preserves the legacy call shape (verify against the dialed host).
        connect_kwargs: dict = {}
        if self._cfg.tls_server_hostname:
            connect_kwargs["server_hostname"] = self._cfg.tls_server_hostname
        try:
            async with session.ws_connect(
                self._resolve_url(), ssl=ssl_ctx, heartbeat=None, **connect_kwargs,
            ) as ws:
                await ws.send_bytes(
                    bf.hello_frame(self._cfg.source_id, self._last_event_id).encode()
                )
                self._connected = True
                self._active_ws = ws
                out_task = asyncio.ensure_future(self._pump_outbound(ws))
                try:
                    await self._pump_inbound(ws)
                finally:
                    self._connected = False
                    self._active_ws = None
                    out_task.cancel()
                    try:
                        await out_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
        finally:
            await session.close()

    async def _pump_outbound(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Subscribe to the local broker and stream events (with
        replay) to the peer. Replay seeding, heartbeat cadence, and
        drop-oldest backpressure all come from
        StreamEventBroker.subscribe()/stream_iter() -- this method
        only filters broker-internal control events and translates
        StreamEvent -> BusFrame, writing bytes to the socket."""
        sub = self._broker.subscribe(op_id_filter=None, last_event_id=self._last_sent_id)
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
                    # An event WE republished from the peer -- sending it back
                    # would re-mint ids on their side forever (reflection storm).
                    self._last_sent_id = event.event_id
                    continue
                frame = bf.event_frame(event, source_id=self._cfg.source_id)
                await ws.send_bytes(frame.encode())
                self._last_sent_id = event.event_id
        except (asyncio.CancelledError, ConnectionResetError):
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[BusBridgeClient] outbound pump ended", exc_info=True)

    async def _pump_inbound(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        hb = self._cfg.heartbeat_s
        while not ws.closed and not self._stopped:
            try:
                if hb > 0:
                    msg = await asyncio.wait_for(ws.receive(), timeout=hb * 1.5)
                else:
                    msg = await ws.receive()
            except asyncio.TimeoutError:
                self._missed_hb += 1
                if self._missed_hb >= self._cfg.degrade_after_missed_hb:
                    self._degraded = True
                continue
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
            self._missed_hb = 0
            self._degraded = False
            frame = bf.BusFrame.decode(msg.data)
            if frame is None or frame.kind != bf.FRAME_EVENT:
                continue
            self._apply_inbound(frame)

    def _apply_inbound(self, frame: bf.BusFrame) -> None:
        ev_dict = frame.event or {}
        event_id = ev_dict.get("event_id", "")
        qid = bf.qualified_id(frame.source_id, event_id)
        if not self._mark_seen(qid):
            # Already republished locally, but the contiguous high-water
            # mark tracks what has been RECEIVED on the wire (for
            # Last-Event-ID replay resumption), independent of local
            # republish dedup. A replay that re-delivers already-seen
            # events MUST still advance the mark across them -- otherwise
            # the mark stalls, every reconnect replays the same prefix
            # from high_water+1, and a drop_after fault starves the still
            # -missing tail forever (zero-drop guarantee broken).
            self._advance_contiguous(event_id)
            return
        try:
            local_eid = self._broker.publish(
                ev_dict.get("event_type", ""),
                ev_dict.get("op_id", ""),
                ev_dict.get("payload", {}) or {},
            )
            if local_eid:
                self._mark_republished(local_eid)
        except Exception:  # noqa: BLE001
            logger.debug("[BusBridgeClient] local republish failed", exc_info=True)
        self._advance_contiguous(event_id)
