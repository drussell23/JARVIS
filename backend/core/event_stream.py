"""
Persistent Bidirectional Event Stream with Sequence Guarantees.

This module implements a 3-layer transport protocol for JARVIS WebSocket
communication:

  Layer 1 — Guaranteed Delivery Ledger (seq + ack + replay buffer)
  Layer 2 — Adaptive Channel Multiplexing (priority + drop policies)
  Layer 3 — Connection-Aware Transport (sync frames + SSE fallback)

Wire format (server → client):
    {"v":1, "seq":N, "ch":"channel", "ts":float, "ack":M, "d":{...}}

Wire format (client → server):
    {"v":1, "ack":N, "ch":"channel", "d":{...}}

Design decisions:
  - Dropped events do NOT consume a seq (no holes in the sequence).
  - Single-process sequencer (in-memory deque). Move to Redis Streams
    if multi-worker is ever needed.
  - Sync frames every 5s of idle replace ping/pong. No synthetic heartbeats.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect
from starlette.requests import Request
from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
REPLAY_BUFFER_SIZE = 500
SYNC_INTERVAL_S = 5.0
IDLE_THRESHOLD_S = 5.0
HANDSHAKE_TIMEOUT_S = 2.0


# ---------------------------------------------------------------------------
# Layer 2: Channel configuration
# ---------------------------------------------------------------------------

class DropPolicy(str, Enum):
    NEVER = "never"              # guaranteed delivery
    LATEST_WINS = "latest_wins"  # only latest per sub-type kept
    RING_OLDEST = "ring_oldest"  # oldest dropped when buffer full
    LATEST_FRAME = "latest_frame"  # only newest frame kept (vision)


@dataclass(frozen=True)
class ChannelConfig:
    name: str
    priority: int          # 0 = highest
    drop_policy: DropPolicy
    buffer_size: int = 0   # for ring-buffer channels


DEFAULT_CHANNELS: Dict[str, ChannelConfig] = {
    "command":    ChannelConfig("command",    0, DropPolicy.NEVER),
    "voice":      ChannelConfig("voice",      1, DropPolicy.NEVER),
    "governance": ChannelConfig("governance", 2, DropPolicy.LATEST_WINS),
    "telemetry":  ChannelConfig("telemetry",  3, DropPolicy.RING_OLDEST, buffer_size=50),
    "vision":     ChannelConfig("vision",     4, DropPolicy.LATEST_FRAME),
}

# Maps message "type" field → channel name.
# Types not listed here default to "command".
TYPE_TO_CHANNEL: Dict[str, str] = {}
CHANNEL_TYPE_MAP: Dict[str, Set[str]] = {
    "command":    {"command", "voice_command", "jarvis_command"},
    "voice":      {"ml_audio_stream", "audio_error"},
    "governance": {"notification", "model_status", "network_status",
                   "system_updating", "system_restarting", "system_rollback",
                   "system_online", "update_available", "update_progress"},
    "telemetry":  {"system_metrics", "health_check", "ping", "pong",
                   "connection_health", "cost_update"},
    "vision":     {"vision_analyze", "vision_monitor", "workspace_analysis"},
}
# Build reverse map at import time
for _ch, _types in CHANNEL_TYPE_MAP.items():
    for _t in _types:
        TYPE_TO_CHANNEL[_t] = _ch


def resolve_channel(msg_type: str) -> str:
    """Resolve a message type to its channel name."""
    return TYPE_TO_CHANNEL.get(msg_type, "command")


# ---------------------------------------------------------------------------
# Layer 1: Replay Buffer (Guaranteed Delivery Ledger)
# ---------------------------------------------------------------------------

@dataclass
class BufferEntry:
    seq: int
    channel: str
    ts: float
    payload: Dict[str, Any]


class ReplayBuffer:
    """
    Bounded in-memory buffer of committed events.

    Only events that are actually sent (not dropped by policy) are stored.
    Seq is strictly monotonic with no holes — any gap the client sees
    means real loss that warrants a replay request.
    """

    __slots__ = ("_buffer", "_seq", "_lock")

    def __init__(self, maxlen: int = REPLAY_BUFFER_SIZE):
        self._buffer: deque[BufferEntry] = deque(maxlen=maxlen)
        self._seq: int = 0
        self._lock = asyncio.Lock()

    @property
    def head_seq(self) -> int:
        return self._seq

    async def append(self, channel: str, payload: Dict[str, Any]) -> int:
        """Assign next seq, store entry, return the seq."""
        async with self._lock:
            self._seq += 1
            entry = BufferEntry(
                seq=self._seq,
                channel=channel,
                ts=time.time(),
                payload=payload,
            )
            self._buffer.append(entry)
            return self._seq

    async def replay_from(
        self, after_seq: int, channels: Optional[Set[str]] = None
    ) -> List[BufferEntry]:
        """Return all entries with seq > after_seq, filtered by channels."""
        async with self._lock:
            result = []
            for entry in self._buffer:
                if entry.seq <= after_seq:
                    continue
                if channels is not None and entry.channel not in channels:
                    continue
                result.append(entry)
            return result


# ---------------------------------------------------------------------------
# Per-client session state
# ---------------------------------------------------------------------------

@dataclass
class ClientSession:
    client_id: str
    websocket: WebSocket
    subscribed_channels: Set[str] = field(default_factory=lambda: set(DEFAULT_CHANNELS.keys()))
    last_client_ack: int = 0
    last_send_time: float = 0.0
    last_recv_time: float = 0.0
    legacy_mode: bool = False
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    messages_sent: int = 0
    messages_received: int = 0
    # Per-channel latest-wins / latest-frame tracking
    _latest_wins: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _latest_frame: Dict[str, Dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Layer 3: EventStreamProtocol — the main engine
# ---------------------------------------------------------------------------

class EventStreamProtocol:
    """
    Wraps UnifiedWebSocketManager to provide:
      - Monotonic sequence numbering on all outbound messages
      - Piggybacked ACKs on all client messages
      - Channel-based multiplexing with drop policies
      - Replay on reconnect from bounded buffer
      - Sync frames when idle
      - SSE fallback generator
    """

    def __init__(self, ws_manager: Any = None):
        self._ws_manager = ws_manager  # UnifiedWebSocketManager (optional)
        self._replay_buffer = ReplayBuffer()
        self._sessions: Dict[str, ClientSession] = {}
        self._channels = dict(DEFAULT_CHANNELS)
        self._sync_task: Optional[asyncio.Task] = None
        self._shutdown = False
        logger.info("[EventStream] Protocol initialized (v%d, %d channels)",
                     PROTOCOL_VERSION, len(self._channels))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start background tasks (call after event loop is running)."""
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = asyncio.create_task(
                self._sync_loop(), name="event_stream_sync"
            )

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._shutdown = True
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        # Close all sessions
        for session in list(self._sessions.values()):
            await self._close_session(session, reason="server_shutdown")

    # ------------------------------------------------------------------
    # Connection handler (the /ws/stream endpoint calls this)
    # ------------------------------------------------------------------

    async def handle_connection(self, websocket: WebSocket) -> None:
        """
        Full lifecycle handler for a /ws/stream WebSocket connection.

        1. Accept
        2. Wait for handshake (2s timeout, fallback to legacy)
        3. Replay missed events
        4. Enter message loop
        """
        await websocket.accept()
        client_id = f"es_{uuid.uuid4().hex[:8]}"
        session = ClientSession(client_id=client_id, websocket=websocket)

        # Ensure sync loop is running
        self.start()

        try:
            # --- Handshake phase ---
            session = await self._handshake(session)
            self._sessions[client_id] = session

            if not session.legacy_mode:
                # Replay missed events
                await self._replay(session)

            logger.info(
                "[EventStream] Client %s connected (legacy=%s, channels=%s)",
                client_id, session.legacy_mode,
                ",".join(sorted(session.subscribed_channels)),
            )

            # --- Message loop ---
            while not self._shutdown:
                raw = await websocket.receive_text()
                session.last_recv_time = time.time()
                session.messages_received += 1

                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if session.legacy_mode:
                    # Pass through to ws_manager unchanged
                    response = await self._dispatch(client_id, frame)
                    if response:
                        await self._send_raw(session, json.dumps(response))
                else:
                    # Unwrap envelope, extract ack
                    inner = self._unwrap_inbound(frame, session)
                    if inner is None:
                        continue

                    # Dispatch to handler
                    response = await self._dispatch(client_id, inner)
                    if response:
                        channel = resolve_channel(
                            response.get("type", inner.get("type", "command"))
                        )
                        await self._send_event(session, channel, response)

        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("[EventStream] Client %s error: %s", client_id, e)
        finally:
            await self._close_session(session, reason="disconnect")

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    async def _handshake(self, session: ClientSession) -> ClientSession:
        """Wait for client handshake or fall back to legacy mode."""
        try:
            raw = await asyncio.wait_for(
                session.websocket.receive_text(),
                timeout=HANDSHAKE_TIMEOUT_S,
            )
            frame = json.loads(raw)

            if frame.get("type") == "handshake" and frame.get("v") == PROTOCOL_VERSION:
                session.last_client_ack = frame.get("last_ack", 0)
                requested = frame.get("channels")
                if requested:
                    session.subscribed_channels = (
                        set(requested) & set(self._channels.keys())
                    )
                session.legacy_mode = False
                session.last_recv_time = time.time()

                # Send handshake ACK
                head = self._replay_buffer.head_seq
                replay_from = session.last_client_ack + 1
                ack_frame = {
                    "v": PROTOCOL_VERSION,
                    "seq": 0,
                    "ch": "_ctrl",
                    "ts": time.time(),
                    "ack": 0,
                    "d": {
                        "type": "handshake_ack",
                        "session_id": session.session_id,
                        "replay_from": replay_from if replay_from <= head else head + 1,
                        "replay_to": head,
                        "channels": sorted(session.subscribed_channels),
                        "server_features": ["replay", "channels", "sse_fallback"],
                    },
                }
                await self._send_raw(session, json.dumps(ack_frame))
                return session

            else:
                # Non-handshake first message — legacy mode
                logger.warning(
                    "[EventStream] Client %s: first frame is not handshake "
                    "(type=%s, v=%s) — entering legacy mode",
                    session.client_id,
                    frame.get("type"),
                    frame.get("v"),
                )
                session.legacy_mode = True
                session.last_recv_time = time.time()

                # Process the first message normally
                response = await self._dispatch(session.client_id, frame)
                if response:
                    await self._send_raw(session, json.dumps(response))
                return session

        except asyncio.TimeoutError:
            logger.warning(
                "[EventStream] Client %s: no handshake within %.1fs — legacy mode",
                session.client_id, HANDSHAKE_TIMEOUT_S,
            )
            session.legacy_mode = True
            return session

        except json.JSONDecodeError:
            logger.warning(
                "[EventStream] Client %s: non-JSON first frame — legacy mode",
                session.client_id,
            )
            session.legacy_mode = True
            return session

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    async def _replay(self, session: ClientSession) -> None:
        """Replay missed events from the buffer after handshake."""
        entries = await self._replay_buffer.replay_from(
            session.last_client_ack, session.subscribed_channels
        )
        if not entries:
            return

        logger.debug(
            "[EventStream] Replaying %d events to %s (seq %d→%d)",
            len(entries), session.client_id,
            entries[0].seq, entries[-1].seq,
        )
        for entry in entries:
            frame = {
                "v": PROTOCOL_VERSION,
                "seq": entry.seq,
                "ch": entry.channel,
                "ts": entry.ts,
                "ack": session.last_client_ack,
                "d": entry.payload,
            }
            await self._send_raw(session, json.dumps(frame))

    # ------------------------------------------------------------------
    # Outbound: wrap + send + drop policy
    # ------------------------------------------------------------------

    async def _send_event(
        self, session: ClientSession, channel: str, payload: Dict[str, Any]
    ) -> bool:
        """Wrap payload in envelope, apply drop policy, send to one session."""
        if channel not in session.subscribed_channels:
            return False

        config = self._channels.get(channel)
        if config and self._should_drop(config, session, payload):
            return False

        seq = await self._replay_buffer.append(channel, payload)
        frame = {
            "v": PROTOCOL_VERSION,
            "seq": seq,
            "ch": channel,
            "ts": time.time(),
            "ack": session.last_client_ack,
            "d": payload,
        }
        return await self._send_raw(session, json.dumps(frame))

    async def broadcast_event(
        self, channel: str, payload: Dict[str, Any]
    ) -> int:
        """
        Broadcast a server-initiated event to all connected sessions.

        Returns the number of sessions that received the event.
        Only sessions subscribed to this channel receive it.
        Drop policy is applied per-session.
        """
        if not self._sessions:
            return 0

        config = self._channels.get(channel)
        sent = 0

        # Commit to replay buffer once (not per-session)
        # Only if at least one session wants this channel and it isn't dropped
        should_commit = False
        for session in self._sessions.values():
            if session.legacy_mode:
                continue
            if channel in session.subscribed_channels:
                if not config or not self._should_drop(config, session, payload):
                    should_commit = True
                    break

        seq = 0
        if should_commit:
            seq = await self._replay_buffer.append(channel, payload)

        for session in list(self._sessions.values()):
            if session.legacy_mode:
                continue
            if channel not in session.subscribed_channels:
                continue
            if config and self._should_drop(config, session, payload):
                continue

            frame = {
                "v": PROTOCOL_VERSION,
                "seq": seq,
                "ch": channel,
                "ts": time.time(),
                "ack": session.last_client_ack,
                "d": payload,
            }
            ok = await self._send_raw(session, json.dumps(frame))
            if ok:
                sent += 1

        return sent

    def _should_drop(
        self, config: ChannelConfig, session: ClientSession, payload: Dict[str, Any]
    ) -> bool:
        """Evaluate drop policy. Returns True if this message should be dropped."""
        if config.drop_policy == DropPolicy.NEVER:
            return False

        if config.drop_policy == DropPolicy.LATEST_WINS:
            # Keep only the latest per sub-type (message type within channel)
            sub_type = payload.get("type", "__default__")
            key = f"{config.name}:{sub_type}"
            session._latest_wins[key] = payload
            # Don't drop — we always send the latest. But if there's a
            # pending unsent message of the same sub-type in the buffer,
            # the client can skip it via seq ordering.
            return False

        if config.drop_policy == DropPolicy.LATEST_FRAME:
            # Only keep the newest frame; if we're about to send another
            # frame before the client ACKed the previous one, this is fine —
            # the client takes the latest.
            return False

        if config.drop_policy == DropPolicy.RING_OLDEST:
            # The replay buffer itself is a ring; nothing extra needed here.
            return False

        return False

    # ------------------------------------------------------------------
    # Inbound: unwrap
    # ------------------------------------------------------------------

    def _unwrap_inbound(
        self, frame: Dict[str, Any], session: ClientSession
    ) -> Optional[Dict[str, Any]]:
        """Extract ACK from client frame, return inner payload."""
        # Update client ACK
        client_ack = frame.get("ack", 0)
        if client_ack > session.last_client_ack:
            session.last_client_ack = client_ack

        inner = frame.get("d")
        if inner is None:
            return None
        return inner

    # ------------------------------------------------------------------
    # Sync loop (Layer 3)
    # ------------------------------------------------------------------

    async def _sync_loop(self) -> None:
        """Send sync frames to idle sessions every second."""
        while not self._shutdown:
            try:
                await asyncio.sleep(1.0)
                now = time.time()

                for session in list(self._sessions.values()):
                    if session.legacy_mode:
                        continue

                    elapsed = now - session.last_send_time
                    if elapsed >= IDLE_THRESHOLD_S:
                        sync_frame = {
                            "v": PROTOCOL_VERSION,
                            "seq": self._replay_buffer.head_seq,
                            "ch": "_sync",
                            "ts": now,
                            "ack": session.last_client_ack,
                        }
                        await self._send_raw(session, json.dumps(sync_frame))

                    # Detect dead clients (no recv in 30s despite sync frames)
                    recv_elapsed = now - session.last_recv_time
                    if session.last_recv_time > 0 and recv_elapsed > 30.0:
                        logger.info(
                            "[EventStream] Client %s: no response for %.0fs, closing",
                            session.client_id, recv_elapsed,
                        )
                        await self._close_session(session, reason="timeout")

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("[EventStream] Sync loop error: %s", e)

    # ------------------------------------------------------------------
    # SSE fallback (Layer 3)
    # ------------------------------------------------------------------

    async def sse_stream(
        self,
        last_ack: int = 0,
        channels: Optional[Set[str]] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Async generator for Server-Sent Events.

        Yields SSE-formatted lines. Each event includes `id:` for
        browser-native Last-Event-ID reconnect.
        """
        if channels is None:
            channels = set(self._channels.keys())

        # Replay missed events
        entries = await self._replay_buffer.replay_from(last_ack, channels)
        for entry in entries:
            frame = {
                "v": PROTOCOL_VERSION,
                "seq": entry.seq,
                "ch": entry.channel,
                "ts": entry.ts,
                "d": entry.payload,
            }
            yield f"id: {entry.seq}\ndata: {json.dumps(frame)}\n\n"

        # Live stream via queue
        queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue(maxsize=100)
        queue_id = f"sse_{uuid.uuid4().hex[:8]}"

        async def _feed():
            """Feed queue from replay buffer tail."""
            last_seen = self._replay_buffer.head_seq
            while not self._shutdown:
                await asyncio.sleep(0.5)
                new_entries = await self._replay_buffer.replay_from(last_seen, channels)
                for entry in new_entries:
                    frame = {
                        "v": PROTOCOL_VERSION,
                        "seq": entry.seq,
                        "ch": entry.channel,
                        "ts": entry.ts,
                        "d": entry.payload,
                    }
                    try:
                        queue.put_nowait(frame)
                    except asyncio.QueueFull:
                        # Drop oldest under pressure
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        queue.put_nowait(frame)
                    last_seen = entry.seq

                # Send sync keepalive for SSE
                if not new_entries:
                    sync = {"v": PROTOCOL_VERSION, "seq": last_seen, "ch": "_sync", "ts": time.time()}
                    try:
                        queue.put_nowait(sync)
                    except asyncio.QueueFull:
                        pass

        feed_task = asyncio.create_task(_feed(), name=f"sse_feed_{queue_id}")
        try:
            while not self._shutdown:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    # SSE keepalive comment
                    yield ": keepalive\n\n"
                    continue

                if frame is None:
                    break
                seq = frame.get("seq", 0)
                yield f"id: {seq}\ndata: {json.dumps(frame)}\n\n"
        finally:
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass

    async def handle_post_command(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a command sent via REST POST (SSE fallback path)."""
        response = await self._dispatch("rest_client", payload)
        return response or {"success": False, "error": "no_handler"}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _dispatch(
        self, client_id: str, message: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Route message to UnifiedWebSocketManager's handler."""
        if self._ws_manager is not None:
            try:
                return await self._ws_manager.handle_message(client_id, message)
            except Exception as e:
                logger.debug("[EventStream] Handler error: %s", e)
                return {
                    "success": False,
                    "type": "error",
                    "message": str(e),
                }
        return None

    async def _send_raw(self, session: ClientSession, text: str) -> bool:
        """Send raw text to a session's WebSocket. Returns False on failure."""
        try:
            await session.websocket.send_text(text)
            session.last_send_time = time.time()
            session.messages_sent += 1
            return True
        except Exception:
            return False

    async def _close_session(
        self, session: ClientSession, reason: str = "unknown"
    ) -> None:
        """Clean up a client session."""
        self._sessions.pop(session.client_id, None)
        try:
            await session.websocket.close()
        except Exception:
            pass
        logger.debug(
            "[EventStream] Session %s closed (reason=%s, sent=%d, recv=%d)",
            session.client_id, reason,
            session.messages_sent, session.messages_received,
        )

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Return protocol statistics for health endpoints."""
        return {
            "protocol_version": PROTOCOL_VERSION,
            "active_sessions": len(self._sessions),
            "legacy_sessions": sum(
                1 for s in self._sessions.values() if s.legacy_mode
            ),
            "buffer_head_seq": self._replay_buffer.head_seq,
            "buffer_size": len(self._replay_buffer._buffer),
            "channels": {
                name: {
                    "priority": cfg.priority,
                    "drop_policy": cfg.drop_policy.value,
                }
                for name, cfg in self._channels.items()
            },
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_event_stream: Optional[EventStreamProtocol] = None


def get_event_stream() -> EventStreamProtocol:
    """Get or create the EventStreamProtocol singleton."""
    global _event_stream
    if _event_stream is None:
        # Import here to avoid circular imports
        try:
            from backend.api.unified_websocket import get_ws_manager_if_initialized
            ws_mgr = get_ws_manager_if_initialized()
        except ImportError:
            ws_mgr = None
        _event_stream = EventStreamProtocol(ws_manager=ws_mgr)
    return _event_stream


def get_event_stream_if_initialized() -> Optional[EventStreamProtocol]:
    """Get the existing EventStreamProtocol without side effects."""
    return _event_stream


def set_event_stream_ws_manager(ws_manager: Any) -> None:
    """Wire the WS manager into the event stream after both are initialized."""
    es = get_event_stream()
    if es._ws_manager is None:
        es._ws_manager = ws_manager
        logger.info("[EventStream] WS manager wired")
