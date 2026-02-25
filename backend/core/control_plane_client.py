"""Control Plane Client Library.

Provides the client-side components for external repos (JARVIS Prime,
Reactor Core) to interact with the supervisor control plane:

- ControlPlaneSubscriber: UDS-based event subscriber that receives
  control plane events via Unix Domain Socket.
- HandshakeResponder: Handles lifecycle handshake proposals from the
  supervisor, reporting component capabilities and identity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["ControlPlaneSubscriber", "HandshakeResponder"]

PONG_WRITE_TIMEOUT_S = 2.0


class HandshakeResponder:
    """Responds to supervisor handshake proposals.

    The component always accepts the handshake and reports its own
    capabilities. The supervisor is responsible for evaluating
    compatibility and deciding whether to proceed.
    """

    def __init__(
        self,
        api_version: str,
        capabilities: List[str],
        instance_id: str,
        health_schema_hash: str = "",
    ) -> None:
        self.api_version = api_version
        self.capabilities = list(capabilities)
        self.instance_id = instance_id
        self.health_schema_hash = health_schema_hash

    def handle_handshake(self, proposal: dict) -> dict:
        """Process a handshake proposal and return a response.

        The response always has ``accepted=True`` because the component
        reports what it has; the supervisor evaluates compatibility.

        Args:
            proposal: Handshake proposal from the supervisor containing
                ``supervisor_epoch``, ``required_capabilities``,
                ``heartbeat_interval_s``, and ``heartbeat_ttl_s``.

        Returns:
            Response dict with component identity, capabilities, and
            acceptance status.
        """
        return {
            "accepted": True,
            "component_instance_id": self.instance_id,
            "api_version": self.api_version,
            "capabilities": list(self.capabilities),
            "health_schema_hash": self.health_schema_hash,
            "rejection_reason": None,
            "metadata": None,
        }


class ControlPlaneSubscriber:
    """UDS-based subscriber for control plane events.

    Connects to the supervisor's Unix Domain Socket, subscribes with a
    unique subscriber ID, and dispatches received events to registered
    callbacks.
    """

    def __init__(
        self,
        subscriber_id: str,
        sock_path: str,
        last_seen_seq: int = 0,
    ) -> None:
        self._subscriber_id = subscriber_id
        self._sock_path = sock_path
        self._last_seen_seq = last_seen_seq
        self._callbacks: List[Callable[[Dict[str, Any]], Any]] = []
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._connected = False
        self._last_subscribe_ack: Optional[Dict[str, Any]] = None

    # -- Properties ----------------------------------------------------------

    @property
    def subscriber_id(self) -> str:
        return self._subscriber_id

    @property
    def last_seen_seq(self) -> int:
        return self._last_seen_seq

    # -- Callback registration -----------------------------------------------

    def on_event(self, callback: Callable[[Dict[str, Any]], Any]) -> None:
        """Register a callback to be invoked for each received event."""
        self._callbacks.append(callback)

    # -- Event dispatch (internal) -------------------------------------------

    def _dispatch_event(self, event: Dict[str, Any]) -> None:
        """Dispatch an event to all registered callbacks.

        Updates ``last_seen_seq`` if the event contains a ``seq`` field.
        """
        seq = event.get("seq")
        if seq is not None and isinstance(seq, int):
            self._last_seen_seq = max(self._last_seen_seq, seq)

        for cb in self._callbacks:
            try:
                cb(event)
            except Exception:
                logger.exception(
                    "Error in event callback for subscriber %s",
                    self._subscriber_id,
                )

    # -- Connection lifecycle ------------------------------------------------

    async def connect(self) -> None:
        """Connect to the control plane UDS and start receiving events.

        Sends a subscribe message with the subscriber ID and the last
        seen sequence number so the server can replay missed events.
        """
        if self._connected:
            logger.warning(
                "Subscriber %s already connected", self._subscriber_id
            )
            return

        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                self._sock_path
            )
            # Use length-prefixed JSON wire protocol (matching EventFabric)
            await self._send_frame({
                "type": "subscribe",
                "subscriber_id": self._subscriber_id,
                "last_seen_seq": self._last_seen_seq,
            })
            self._connected = True
            self._receive_task = asyncio.create_task(self._receive_loop())
            logger.info(
                "Subscriber %s connected to %s",
                self._subscriber_id,
                self._sock_path,
            )
        except Exception:
            logger.exception(
                "Failed to connect subscriber %s to %s",
                self._subscriber_id,
                self._sock_path,
            )
            self._connected = False
            raise

    async def disconnect(self) -> None:
        """Close the UDS connection and stop the receive loop."""
        self._connected = False
        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

        logger.info("Subscriber %s disconnected", self._subscriber_id)

    # -- Wire protocol helpers ------------------------------------------------

    async def _send_frame(self, payload: dict) -> None:
        """Send a length-prefixed JSON frame."""
        assert self._writer is not None
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = struct.pack(">I", len(data))
        self._writer.write(header + data)
        await self._writer.drain()

    async def _recv_frame(self) -> dict:
        """Read a length-prefixed JSON frame."""
        assert self._reader is not None
        header = await self._reader.readexactly(4)
        (length,) = struct.unpack(">I", header)
        data = await self._reader.readexactly(length)
        return json.loads(data)

    async def _send_pong(self, ping_msg: Dict[str, Any]) -> None:
        """Send a pong frame in response to a server ping, with bounded timeout."""
        if self._writer is None or self._writer.is_closing():
            return
        pong_frame = {
            "type": "pong",
            "ping_id": ping_msg.get("ping_id", ""),
            "ts": ping_msg.get("ts"),
        }
        try:
            data = json.dumps(pong_frame, separators=(",", ":")).encode("utf-8")
            header = struct.pack(">I", len(data))
            self._writer.write(header + data)
            await asyncio.wait_for(
                self._writer.drain(), timeout=PONG_WRITE_TIMEOUT_S
            )
        except (
            ConnectionResetError,
            BrokenPipeError,
            OSError,
            asyncio.TimeoutError,
        ) as exc:
            logger.warning(
                "Failed to send pong for subscriber %s: %s",
                self._subscriber_id, exc,
            )

    async def _receive_loop(self) -> None:
        """Background task that reads events from the UDS connection."""
        assert self._reader is not None
        try:
            while self._connected:
                try:
                    event = await self._recv_frame()
                    msg_type = event.get("type", "")
                    if msg_type == "ping":
                        await self._send_pong(event)
                    elif msg_type == "subscribe_ack":
                        self._last_subscribe_ack = event
                    else:
                        self._dispatch_event(event)
                except asyncio.IncompleteReadError:
                    logger.info(
                        "UDS connection closed for subscriber %s",
                        self._subscriber_id,
                    )
                    break
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Received malformed event on subscriber %s: %s",
                        self._subscriber_id,
                        exc,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Error in receive loop for subscriber %s",
                self._subscriber_id,
            )
        finally:
            self._connected = False
