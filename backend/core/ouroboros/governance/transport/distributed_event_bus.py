from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.bus_bridge_server import BusBridgeServer
from backend.core.ouroboros.governance.transport.bus_bridge_client import BusBridgeClient

logger = logging.getLogger(__name__)


class DistributedEventBus:
    """The publish()-here-subscribers-there seam. Wraps a local
    StreamEventBroker; a server- or client-role bridge propagates events
    across the socket. TrinityEventBus callers are unchanged -- they still
    publish/subscribe on the local broker."""

    def __init__(
        self,
        broker: StreamEventBroker,
        cfg: TransportConfig,
        *,
        role: str,
        durable_outbound: Optional[Any] = None,
        url_resolver: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
    ) -> None:
        if role not in ("server", "client"):
            raise ValueError(f"role must be server|client, got {role!r}")
        self._broker = broker
        self._cfg = cfg
        self._role = role
        # Stage 3 Task 3 passthroughs (client role). Defaults None =
        # Stage-2-identical. The durable's on_ack is re-wired at EVERY
        # client construction so acks keep trimming the WAL across
        # client recreation (start_client builds a new instance per call).
        self._durable_outbound = durable_outbound
        self._url_resolver = url_resolver
        self._server: Optional[BusBridgeServer] = None
        self._client: Optional[BusBridgeClient] = None
        # Outbound last-sent cursor carried ACROSS client instances: stop() +
        # start_client() builds a NEW BusBridgeClient, and without the carry
        # the severed span is silently lost (live-fire 2026-07-04).
        self._last_sent_cursor: Optional[str] = None

    def publish(self, event_type: str, op_id: str, payload: dict) -> Optional[str]:
        return self._broker.publish(event_type, op_id, payload)

    def register_server_routes(self, app: web.Application) -> None:
        if self._role != "server":
            raise RuntimeError("register_server_routes requires role=server")
        # Inbound peer events republish into the local broker so local
        # subscribers see them (idempotent -- server dedups by qualified id).
        self._server = BusBridgeServer(
            self._broker, self._cfg,
            on_inbound=lambda ev: self._broker.publish(
                ev.event_type, ev.op_id, dict(ev.payload)
            ),
        )
        self._server.register_routes(app)

    async def start_client(self, url: Optional[str] = None) -> None:
        if self._role != "client":
            raise RuntimeError("start_client requires role=client")
        kwargs: dict = {}
        if self._durable_outbound is not None:
            kwargs["durable"] = self._durable_outbound
            kwargs["on_ack"] = self._durable_outbound.on_ack
        if self._url_resolver is not None:
            kwargs["url_resolver"] = self._url_resolver
        self._client = BusBridgeClient(
            self._broker, self._cfg, url=url,
            initial_last_sent_id=self._last_sent_cursor,
            **kwargs,
        )
        await self._client.run()

    async def stop(self) -> None:
        if self._client is not None:
            self._last_sent_cursor = (
                getattr(self._client, "_last_sent_id", None) or self._last_sent_cursor
            )
            await self._client.stop()
