from __future__ import annotations

import asyncio
import logging
import random
from collections import OrderedDict, deque
from dataclasses import fields as dataclass_fields
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

import aiohttp

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_HEARTBEAT,
    EVENT_TYPE_REPLAY_END,
    EVENT_TYPE_REPLAY_START,
    EVENT_TYPE_STREAM_LAG,
    StreamEvent,
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

# StreamEvent constructor surface, computed once. WAL envelopes are
# StreamEvent.to_dict() outputs (whose keys match the dataclass fields
# exactly today) -- the filter is defensive against a future to_dict()
# gaining extra serialization-only keys.
_STREAM_EVENT_FIELDS = frozenset(f.name for f in dataclass_fields(StreamEvent))


def _rebuild_stream_event(envelope: Dict[str, Any]) -> Optional[StreamEvent]:
    """Rebuild a StreamEvent from a journaled ``to_dict()`` envelope.

    Extra keys are stripped defensively; a malformed envelope (missing
    required fields, wrong types) returns None -- the caller skips it
    (fail-soft per entry, Stage 3 Task 3)."""
    try:
        kwargs = {k: v for k, v in dict(envelope).items()
                  if k in _STREAM_EVENT_FIELDS}
        ev = StreamEvent(**kwargs)
    except Exception:  # noqa: BLE001 -- malformed journal entry
        logger.debug(
            "[BusBridgeClient] malformed WAL envelope skipped: %r",
            envelope, exc_info=True)
        return None
    if not ev.event_id or not ev.event_type:
        return None
    return ev


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
        on_ack: Optional[Callable[[str], None]] = None,
        url_resolver: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
        durable: Optional[Any] = None,
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
        self._on_ack = on_ack
        # Stage 3 WAL-trim cursor: the last event_id the SERVER has
        # confirmed ingesting on this connection. Stage-4 Task 2:
        # monotonicity is tracked by SEND-SEQUENCE POSITION (below), not
        # numeric id -- priority replay sends ids out of numeric order,
        # so a numerically-lower id acked LATER is forward progress.
        self._last_acked_id: Optional[str] = None
        # Stage-4 Task 2: per-connection SEND SEQUENCE -- event ids in
        # the order actually sent (WAL-seeded replay first, then the
        # live pump). The server's ack names its last-INGESTED id on
        # this TCP-ordered connection; locating it in this sequence
        # confirms EVERY id sent at or before that position
        # (send-order-cumulative). Reset on every new connection.
        #   _send_index: id -> FIRST-send position (conservative when a
        #       replay/live overlap re-sends an id: mapping the ack to
        #       the earliest position can only under-confirm, never
        #       over-confirm -- a later ack or the idle-flush catches
        #       the tail).
        #   _send_unconfirmed: (position, id) FIFO of not-yet-confirmed
        #       sends; confirmation always consumes a send-order prefix.
        #   _acked_pos: high-water confirmed position (send-space
        #       monotonic guard).
        # Bounded: past _DEDUP_MAX unconfirmed sends the oldest entry is
        # evicted; its eventual ack degrades to the exact-single-id
        # fallback path in _apply_ack (safe: the durable trim is
        # exact-set).
        self._send_index: Dict[str, int] = {}
        self._send_unconfirmed: Deque[Tuple[int, str]] = deque()
        self._send_count = 0
        self._acked_pos = -1
        # Stage 3 Task 3: per-attempt discovery re-race + WAL-seeded replay.
        # Both default None = Stage-2-identical behavior.
        self._url_resolver = url_resolver
        self._durable = durable
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

    # --- Stage-4 Task 2: per-connection send sequence ----------------------

    def _reset_send_sequence(self) -> None:
        """New connection = new send sequence. The server's ingest cursor
        (and therefore its acks) is per-connection state; positions from
        a previous link must never confirm sends on this one."""
        self._send_index = {}
        self._send_unconfirmed = deque()
        self._send_count = 0
        self._acked_pos = -1

    def _record_sent(self, event_id: str) -> None:
        """Record an event frame actually WRITTEN to the socket, in send
        order. First-send position wins for duplicates (see __init__
        comment: conservative -- under-confirmation is safe, over-
        confirmation is the over-trim class)."""
        if not event_id or event_id in self._send_index:
            return
        pos = self._send_count
        self._send_count += 1
        self._send_index[event_id] = pos
        self._send_unconfirmed.append((pos, event_id))
        if len(self._send_unconfirmed) > _DEDUP_MAX:
            old_pos, old_id = self._send_unconfirmed.popleft()
            self._send_index.pop(old_id, None)
            logger.debug(
                "[BusBridgeClient] send-sequence bound reached; evicted "
                "oldest unconfirmed send pos=%d id=%s (its ack degrades "
                "to exact-single-id confirmation)", old_pos, old_id)

    def _fire_on_ack(self, event_id: str) -> None:
        """Invoke the on_ack callback fail-soft -- a raising callback
        must never take down the inbound pump (nor abort the remaining
        ids of a cumulative confirmation)."""
        if self._on_ack is None:
            return
        try:
            self._on_ack(event_id)
        except Exception:  # noqa: BLE001 -- fail-soft, never crash the pump
            logger.debug(
                "[BusBridgeClient] on_ack callback raised", exc_info=True)

    @property
    def last_event_id(self) -> Optional[str]:
        return self._last_event_id

    @property
    def last_acked_id(self) -> Optional[str]:
        """The last event_id the server has confirmed ingesting on this
        connection (Stage 3 WAL-trim cursor). None until the first ack."""
        return self._last_acked_id

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

    async def _resolve_attempt_url(self) -> str:
        """Per-attempt discovery re-race (Stage 3 Task 3): consult the
        resolver on EVERY connect attempt -- a peer that came back on a
        different address is found without restarting the client. A
        resolver failure (or a None/empty answer) fails soft to the
        static url; no resolver = legacy static behavior."""
        if self._url_resolver is not None:
            try:
                resolved = await self._url_resolver()
            except Exception:  # noqa: BLE001 -- discovery down != loop down
                logger.debug(
                    "[BusBridgeClient] url_resolver failed; falling back "
                    "to static url", exc_info=True)
                resolved = None
            if resolved:
                return resolved
        return self._resolve_url()

    async def run(self) -> None:
        attempt = 0
        while not self._stopped:
            try:
                await self._connect_once(await self._resolve_attempt_url())
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

    async def _connect_once(self, url: Optional[str] = None) -> None:
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
                url or self._resolve_url(), ssl=ssl_ctx, heartbeat=None, **connect_kwargs,
            ) as ws:
                await ws.send_bytes(
                    bf.hello_frame(self._cfg.source_id, self._last_event_id).encode()
                )
                self._connected = True
                self._active_ws = ws
                # Fresh connection -> fresh send sequence (Stage-4 Task 2):
                # ack positions are only meaningful against the sends of
                # THIS link.
                self._reset_send_sequence()
                # WAL-seeded replay FIRST (the oldest truth), then the live
                # pump's broker-cursor replay -- overlap dedup is the
                # SERVER's job (qualified-id _mark_seen).
                await self._replay_durable(ws)
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

    async def _replay_durable(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """WAL-seeded replay (Stage 3 Task 3; PRIORITY-ordered by Stage-4
        Task 2): send every pending journal entry BEFORE the live
        outbound pump starts. The WAL outlives the broker's bounded history
        ring, so this recovers spans the broker-cursor replay physically
        cannot. When the durable exposes ``pending_prioritized`` the
        backlog is replayed in ``(urgency_rank, event_id)`` order --
        IMMEDIATE-class signals cross the reborn link first -- with the
        sort computed off-loop; older durables fall back to the
        id-ordered ``pending()`` (backward compatible).

        No trim on send -- trim is exclusively ack-driven (Task 1 ack lane
        -> exact-set ``on_ack``); an entry stays journaled until the server
        confirms ingesting it, so a mid-replay drop just resends on
        the next attempt. Every sent id is recorded in the per-connection
        send sequence, which is what makes the server's acks cumulative
        in SEND order. Malformed entries are skipped per-entry
        (fail-soft); a dead socket propagates to the reconnect loop."""
        if self._durable is None:
            return
        try:
            prioritized = getattr(self._durable, "pending_prioritized", None)
            if prioritized is not None:
                entries = await prioritized()
            else:
                entries = self._durable.pending()
        except Exception:  # noqa: BLE001 -- a broken WAL must not block connect
            logger.debug(
                "[BusBridgeClient] durable pending read failed; skipping "
                "WAL replay", exc_info=True)
            return
        if not entries:
            return
        sent = 0
        for envelope in entries:
            ev = _rebuild_stream_event(envelope)
            if ev is None:
                continue  # malformed journal entry -- skip, keep replaying
            await ws.send_bytes(
                bf.event_frame(ev, source_id=self._cfg.source_id).encode())
            self._record_sent(ev.event_id)
            sent += 1
        logger.debug(
            "[BusBridgeClient] WAL replay: sent %d/%d pending entries "
            "(priority order: %s)", sent, len(entries),
            prioritized is not None)

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
                self._record_sent(event.event_id)
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
            if frame is None:
                continue
            if frame.kind == bf.FRAME_ACK:
                self._apply_ack(frame)
                continue
            if frame.kind != bf.FRAME_EVENT:
                continue
            self._apply_inbound(frame)

    def _apply_ack(self, frame: bf.BusFrame) -> None:
        """SEND-ORDER-CUMULATIVE ack application (Stage-4 Task 2).

        The server acks the last event id it INGESTED on this connection
        (unchanged, TCP-ordered). Replay is priority-ordered now, so the
        old numeric-id cumulation is wrong: an ack for a high IMMEDIATE
        id sent FIRST must not imply anything about numerically-lower
        ids that were never sent. New interpretation: locate the acked
        id in this connection's SEND sequence -- TCP ordering guarantees
        every frame sent at or before that position was received -- and
        confirm each not-yet-confirmed id in that prefix, in send order,
        via ``on_ack`` (fail-soft per id; the durable's trim is
        exact-set). ``last_acked_id`` monotonicity is positional
        (send-space), not numeric.

        An acked id NOT in this connection's send sequence (nothing was
        sent on this link yet, a pre-reconnect ack raced the sever, or
        the bounded index evicted it) never expands cumulatively --
        that would be the over-trim class. It degrades to a SINGLE
        exact-id confirmation under the legacy numeric-monotonic guard,
        which is safe by construction: the server attested ingesting
        exactly that id, and the durable's exact-set ``on_ack`` trims
        only it."""
        new_id = frame.last_event_id
        if not new_id:
            return
        pos = self._send_index.get(new_id)
        if pos is None:
            current = self._last_acked_id
            if current is not None and new_id <= current:
                return  # regressive or duplicate -- ignored
            logger.debug(
                "[BusBridgeClient] ack for id outside this connection's "
                "send sequence: %s -- exact-single-id confirmation only",
                new_id)
            self._last_acked_id = new_id
            self._fire_on_ack(new_id)
            return
        if pos <= self._acked_pos:
            return  # regressive or duplicate in send-space -- ignored
        self._acked_pos = pos
        self._last_acked_id = new_id
        while self._send_unconfirmed and self._send_unconfirmed[0][0] <= pos:
            _, eid = self._send_unconfirmed.popleft()
            self._send_index.pop(eid, None)
            self._fire_on_ack(eid)

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
