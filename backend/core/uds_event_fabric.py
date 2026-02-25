# backend/core/uds_event_fabric.py
"""
JARVIS UDS Event Fabric v1.0
==============================
Real-time event distribution over Unix Domain Sockets for cross-process
coordination within the JARVIS control plane.

Provides:
  - Length-prefixed JSON wire protocol (4-byte big-endian header + JSON body)
  - Subscriber management with bounded queues (drop-oldest on overflow)
  - Sequence-based replay from the OrchestrationJournal
  - Background per-subscriber sender tasks

Design doc: docs/plans/2026-02-24-cross-repo-control-plane-design.md
"""

import asyncio
import json
import logging
import os
import struct
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from backend.core.orchestration_journal import OrchestrationJournal

logger = logging.getLogger("jarvis.uds_event_fabric")

# ── Constants ────────────────────────────────────────────────────────────

MAX_FRAME_SIZE = 1_048_576  # 1 MB
MAX_SUBSCRIBER_QUEUE = 500

# macOS AF_UNIX sun_path limit is 104 bytes; Linux is 108.
_MAX_UNIX_PATH = 104 if sys.platform == "darwin" else 108

# ── Keepalive constants (configurable via environment) ───────────────────
KEEPALIVE_INTERVAL_S = float(os.environ.get("JARVIS_UDS_KEEPALIVE_INTERVAL", "10.0"))
KEEPALIVE_TIMEOUT_S = float(os.environ.get("JARVIS_UDS_KEEPALIVE_TIMEOUT", "30.0"))
PONG_WRITE_TIMEOUT_S = 2.0


# ── Exceptions ───────────────────────────────────────────────────────────

class ProtocolError(Exception):
    """Raised on wire protocol violations (oversized frames, malformed JSON)."""
    pass


# ── Wire Protocol ────────────────────────────────────────────────────────

async def send_frame(writer: asyncio.StreamWriter, payload: dict) -> None:
    """Serialize *payload* as compact JSON, prepend a 4-byte big-endian
    length header, write to *writer*, and drain."""
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = struct.pack(">I", len(data))
    writer.write(header + data)
    await writer.drain()


async def recv_frame(reader: asyncio.StreamReader) -> dict:
    """Read a length-prefixed JSON frame from *reader*.

    Raises ``ProtocolError`` if the declared frame size exceeds
    ``MAX_FRAME_SIZE`` or the payload is not valid JSON.
    """
    header = await reader.readexactly(4)
    (length,) = struct.unpack(">I", header)

    if length > MAX_FRAME_SIZE:
        raise ProtocolError(
            f"Frame size {length} exceeds MAX_FRAME_SIZE ({MAX_FRAME_SIZE})"
        )

    data = await reader.readexactly(length)
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Invalid JSON payload: {exc}") from exc


# ── Subscriber ───────────────────────────────────────────────────────────

@dataclass
class _Subscriber:
    """Internal bookkeeping for a single connected subscriber."""

    subscriber_id: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_SUBSCRIBER_QUEUE))
    writer: Optional[asyncio.StreamWriter] = None
    task: Optional[asyncio.Task] = None
    keepalive_task: Optional[asyncio.Task] = None
    last_pong_received: float = field(default_factory=time.monotonic)
    last_seen_any: float = field(default_factory=time.monotonic)
    disconnect_reason: str = ""


# ── EventFabric ──────────────────────────────────────────────────────────

class EventFabric:
    """Unix-domain-socket event bus backed by :class:`OrchestrationJournal`.

    Lifecycle::

        fabric = EventFabric(journal)
        await fabric.start(Path("/tmp/jarvis/control.sock"))
        ...
        await fabric.stop()
    """

    def __init__(
        self,
        journal: OrchestrationJournal,
        keepalive_interval_s: float = KEEPALIVE_INTERVAL_S,
        keepalive_timeout_s: float = KEEPALIVE_TIMEOUT_S,
    ) -> None:
        self._journal = journal
        self._subscribers: Dict[str, _Subscriber] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._sock_path: Optional[Path] = None
        # When the requested path exceeds the OS AF_UNIX limit we bind to
        # a short path under /tmp and symlink the requested path to it.
        self._real_sock_path: Optional[Path] = None
        self._owns_real_sock: bool = False
        self._client_tasks: list[asyncio.Task] = []
        self._keepalive_interval_s = keepalive_interval_s
        self._keepalive_timeout_s = keepalive_timeout_s

    # ── Public API ───────────────────────────────────────────────────

    async def start(self, sock_path: Path) -> None:
        """Begin listening on *sock_path*.

        Any stale socket file at that path is removed first.

        If the path exceeds the OS AF_UNIX length limit (104 bytes on
        macOS, 108 on Linux) the server binds to a short temporary path
        and a symlink is created at *sock_path* so callers can
        ``open_unix_connection(sock_path)`` transparently.
        """
        sock_path = Path(sock_path)
        self._sock_path = sock_path

        # Remove stale socket / symlink if present
        if sock_path.is_symlink() or sock_path.exists():
            logger.info("[EventFabric] Removing stale socket: %s", sock_path)
            sock_path.unlink()

        sock_path.parent.mkdir(parents=True, exist_ok=True)

        bind_path = str(sock_path)

        # If the path is too long for the AF_UNIX sun_path field, bind to
        # a short path under /tmp and symlink the caller-visible path to it.
        if len(bind_path.encode("utf-8")) >= _MAX_UNIX_PATH:
            td = tempfile.mkdtemp(prefix="jarvis_uds_")
            real = Path(td) / "ctrl.sock"
            self._real_sock_path = real
            self._owns_real_sock = True
            bind_path = str(real)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=bind_path
        )

        # Create symlink from requested path → actual bind path
        if self._real_sock_path is not None:
            os.symlink(bind_path, str(sock_path))

        logger.info("[EventFabric] Listening on %s", sock_path)

    async def stop(self) -> None:
        """Shut down the server, close all subscribers, remove socket."""
        # Stop accepting new connections
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Cancel all per-subscriber sender tasks, keepalive tasks, and client handler tasks
        all_tasks: list[asyncio.Task] = list(self._client_tasks)
        for sub in self._subscribers.values():
            if sub.task is not None:
                all_tasks.append(sub.task)
            if sub.keepalive_task is not None:
                all_tasks.append(sub.keepalive_task)
            if sub.writer is not None:
                try:
                    sub.writer.close()
                except Exception:
                    pass

        for t in all_tasks:
            t.cancel()

        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)

        self._subscribers.clear()
        self._client_tasks.clear()

        # Remove socket file (and symlink / temp dir when path-shortening was used)
        if self._sock_path is not None:
            if self._sock_path.is_symlink() or self._sock_path.exists():
                self._sock_path.unlink()
                logger.info("[EventFabric] Removed socket: %s", self._sock_path)
        if self._real_sock_path is not None and self._owns_real_sock:
            try:
                if self._real_sock_path.exists():
                    self._real_sock_path.unlink()
                # Remove the temp directory we created
                parent = self._real_sock_path.parent
                if parent.exists():
                    parent.rmdir()
            except OSError:
                pass
            self._real_sock_path = None
            self._owns_real_sock = False
        self._sock_path = None

    async def emit(
        self, seq: int, action: str, target: str, payload: dict
    ) -> None:
        """Broadcast an event to all connected subscribers.

        If a subscriber's queue is full the oldest entry is discarded
        to make room (bounded-queue, drop-oldest policy).
        """
        event = {
            "type": "event",
            "seq": seq,
            "action": action,
            "target": target,
            "payload": payload,
        }

        dead_subs: list[str] = []
        for sub_id, sub in self._subscribers.items():
            try:
                if sub.queue.full():
                    # Drop-oldest policy
                    try:
                        sub.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                sub.queue.put_nowait(event)
            except Exception:
                dead_subs.append(sub_id)

        for sub_id in dead_subs:
            self._remove_subscriber(sub_id)

    async def publish_outbox_once(self) -> int:
        """Publish all unpublished outbox entries via emit().

        Returns the number of entries published.
        """
        entries = self._journal.get_unpublished_outbox()
        published = 0

        for entry in entries:
            await self.emit(
                entry["seq"],
                entry["event_type"],
                entry["target"],
                entry.get("payload") or {},
            )
            self._journal.mark_outbox_published(entry["seq"])
            published += 1

        return published

    # ── Connection handling ──────────────────────────────────────────

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a newly connected client.

        The first frame MUST be a subscribe request.  After acknowledgement,
        events are pushed via a dedicated sender task while this handler
        stays alive reading pong (and other) frames from the client.
        """
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.append(task)

        subscriber_id: Optional[str] = None
        try:
            # Read the subscribe handshake
            try:
                msg = await asyncio.wait_for(recv_frame(reader), timeout=10.0)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ProtocolError) as exc:
                logger.warning("[EventFabric] Bad handshake from client: %s", exc)
                writer.close()
                return

            if not isinstance(msg, dict) or msg.get("type") != "subscribe":
                logger.warning("[EventFabric] Expected subscribe frame, got: %s", msg)
                writer.close()
                return

            subscriber_id = msg.get("subscriber_id", "")
            last_seen_seq = msg.get("last_seen_seq", 0)

            if not subscriber_id:
                logger.warning("[EventFabric] Empty subscriber_id in handshake")
                writer.close()
                return

            # Remove previous connection for same subscriber_id (reconnect)
            if subscriber_id in self._subscribers:
                self._remove_subscriber(subscriber_id)

            # Build subscriber
            sub = _Subscriber(subscriber_id=subscriber_id, writer=writer)
            self._subscribers[subscriber_id] = sub

            # Determine earliest available journal sequence for the ack
            earliest_seq = 0
            try:
                conn = self._journal._conn
                if conn is not None:
                    earliest_row = conn.execute(
                        "SELECT MIN(seq) FROM journal"
                    ).fetchone()
                    if earliest_row and earliest_row[0] is not None:
                        earliest_seq = earliest_row[0]
            except Exception:
                pass

            # Send ack with earliest_available_seq
            ack_msg = {
                "type": "subscribe_ack",
                "subscriber_id": subscriber_id,
                "status": "ok",
                "earliest_available_seq": earliest_seq,
            }
            await send_frame(writer, ack_msg)

            # Replay missed events from journal
            try:
                missed = await self._journal.replay_from(last_seen_seq)
                for entry in missed:
                    event = {
                        "type": "event",
                        "seq": entry["seq"],
                        "action": entry["action"],
                        "target": entry["target"],
                        "payload": entry.get("payload"),
                    }
                    await sub.queue.put(event)
            except Exception as exc:
                logger.warning(
                    "[EventFabric] Replay failed for subscriber %s: %s",
                    subscriber_id, exc,
                )

            # Start sender task
            sender = asyncio.create_task(
                self._subscriber_sender(sub),
                name=f"fabric-sender-{subscriber_id}",
            )
            sub.task = sender

            # Start keepalive task
            sub.keepalive_task = asyncio.create_task(
                self._keepalive_loop(sub),
                name=f"fabric-keepalive-{subscriber_id}",
            )

            # Read loop: receive pong frames (and any other client→server frames)
            try:
                while True:
                    frame = await recv_frame(reader)
                    sub.last_seen_any = time.monotonic()
                    if frame.get("type") == "pong":
                        self._handle_pong(sub, frame)
            except (asyncio.IncompleteReadError, ProtocolError):
                sub.disconnect_reason = sub.disconnect_reason or "eof"
            except asyncio.CancelledError:
                pass

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[EventFabric] Client handler error: %s", exc, exc_info=True)
        finally:
            if subscriber_id and subscriber_id in self._subscribers:
                self._remove_subscriber(subscriber_id)
            try:
                writer.close()
            except Exception:
                pass
            if task is not None and task in self._client_tasks:
                self._client_tasks.remove(task)

    async def _keepalive_loop(self, sub: _Subscriber) -> None:
        """Send periodic pings and remove subscriber on timeout."""
        try:
            while sub.subscriber_id in self._subscribers:
                await asyncio.sleep(self._keepalive_interval_s)

                # Check if subscriber is still registered
                if sub.subscriber_id not in self._subscribers:
                    break

                # Check liveness deadline
                last_activity = max(sub.last_pong_received, sub.last_seen_any)
                deadline = last_activity + self._keepalive_timeout_s
                now = time.monotonic()

                if now > deadline:
                    logger.info(
                        "[EventFabric] Keepalive timeout for subscriber %s "
                        "(last activity %.1fs ago, timeout %.1fs)",
                        sub.subscriber_id,
                        now - last_activity,
                        self._keepalive_timeout_s,
                    )
                    sub.disconnect_reason = "timeout"
                    self._remove_subscriber(sub.subscriber_id)
                    return

                # Send ping
                ping_frame = {
                    "type": "ping",
                    "ping_id": uuid.uuid4().hex[:12],
                    "ts": time.monotonic(),
                }
                try:
                    if sub.writer is not None and not sub.writer.is_closing():
                        await asyncio.wait_for(
                            send_frame(sub.writer, ping_frame),
                            timeout=PONG_WRITE_TIMEOUT_S,
                        )
                    else:
                        sub.disconnect_reason = "write_error"
                        self._remove_subscriber(sub.subscriber_id)
                        return
                except (
                    ConnectionResetError,
                    BrokenPipeError,
                    OSError,
                    asyncio.TimeoutError,
                ) as exc:
                    logger.info(
                        "[EventFabric] Ping write failed for subscriber %s: %s",
                        sub.subscriber_id, exc,
                    )
                    sub.disconnect_reason = "write_error"
                    self._remove_subscriber(sub.subscriber_id)
                    return

        except asyncio.CancelledError:
            pass

    def _handle_pong(self, sub: _Subscriber, msg: dict) -> None:
        """Update liveness timestamps from a received pong frame."""
        now = time.monotonic()
        sub.last_pong_received = now
        sub.last_seen_any = now
        logger.debug(
            "[EventFabric] Pong received from subscriber %s (ping_id=%s)",
            sub.subscriber_id,
            msg.get("ping_id", "?"),
        )

    async def _subscriber_sender(self, sub: _Subscriber) -> None:
        """Continuously drain *sub.queue* and write frames to the client."""
        try:
            while True:
                event = await sub.queue.get()
                if sub.writer is None or sub.writer.is_closing():
                    break
                try:
                    await send_frame(sub.writer, event)
                except (ConnectionResetError, BrokenPipeError, OSError):
                    logger.info(
                        "[EventFabric] Connection lost for subscriber %s",
                        sub.subscriber_id,
                    )
                    break
        except asyncio.CancelledError:
            pass

    def _remove_subscriber(self, subscriber_id: str) -> None:
        """Clean up a subscriber's resources."""
        sub = self._subscribers.pop(subscriber_id, None)
        if sub is None:
            return
        if sub.task is not None and not sub.task.done():
            sub.task.cancel()
        if sub.keepalive_task is not None and not sub.keepalive_task.done():
            sub.keepalive_task.cancel()
        if sub.writer is not None:
            try:
                sub.writer.close()
            except Exception:
                pass
        logger.debug("[EventFabric] Removed subscriber: %s", subscriber_id)
