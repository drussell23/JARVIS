# -*- coding: utf-8 -*-
"""Live-fire attempt-2 defects (2026-07-04), proven against a REAL localhost
client<->server WS pair with EXACT-count assertions (the in-proc fake bridge
had modeled dedup-by-original-id; the real bridges re-mint ids on republish,
so the fake masked all three):

  (A) REFLECTION SUPPRESSION: a bridge must never re-send an event it itself
      republished from the peer -- republish mints a NEW local event id, so
      qualified-id dedup never fires and events ping-pong forever (observed
      408x amplification of 5 events, live).
  (B) CONNECT GATE: events published before the client's first successful
      connect are lost (live-only first subscribe); the client must expose
      ``connected`` so callers can gate.
  (C) CURSOR ACROSS RECONNECT: DistributedEventBus builds a NEW BusBridgeClient
      per start_client(); the outbound last-sent cursor must carry over or the
      severed span is lost (all 5 post-sever events gapped, live).
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any, List, Optional

import pytest
from aiohttp import web

import backend.core.ouroboros.governance.transport.exchange_protocol as xp
from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEventBroker,
)
from backend.core.ouroboros.governance.transport.transport_config import (
    TransportConfig,
)
from backend.core.ouroboros.governance.transport.distributed_event_bus import (
    DistributedEventBus,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cfg(monkeypatch, port: int, role: str) -> TransportConfig:
    for key in ("JARVIS_BRAIN_WS_TLS_ENABLED", "JARVIS_BRAIN_WS_HOST",
                "JARVIS_BRAIN_WS_PORT", "JARVIS_BRAIN_WS_HEARTBEAT_S"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "false")
    monkeypatch.setenv("JARVIS_BRAIN_WS_HOST", "127.0.0.1")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", str(port))
    monkeypatch.setenv("JARVIS_BRAIN_WS_HEARTBEAT_S", "1.0")
    return TransportConfig.from_env(role=role)


class _Pair:
    """A REAL server + client DistributedEventBus pair on localhost."""

    def __init__(self, monkeypatch) -> None:
        self.port = _free_port()
        self.server_cfg = _cfg(monkeypatch, self.port, "server")
        self.client_cfg = _cfg(monkeypatch, self.port, "client")
        self.server_broker = StreamEventBroker()
        self.client_broker = StreamEventBroker()
        self.server_bus = DistributedEventBus(
            self.server_broker, self.server_cfg, role="server")
        self.client_bus = DistributedEventBus(
            self.client_broker, self.client_cfg, role="client")
        self._runner: Optional[web.AppRunner] = None
        self._client_task: Optional[asyncio.Task] = None

    @property
    def url(self) -> str:
        return "ws://127.0.0.1:%d%s" % (self.port, self.client_cfg.path)

    async def start(self) -> None:
        app = web.Application()
        self.server_bus.register_server_routes(app)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="127.0.0.1", port=self.port)
        await site.start()
        self._client_task = asyncio.ensure_future(
            self.client_bus.start_client(self.url))

    async def await_connected(self, timeout: float = 5.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            client = getattr(self.client_bus, "_client", None)
            if client is not None and getattr(client, "connected", False):
                return
            await asyncio.sleep(0.02)
        raise AssertionError("client never reported connected")

    async def stop(self) -> None:
        try:
            await self.client_bus.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._client_task:
            self._client_task.cancel()
            try:
                await self._client_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._runner:
            await self._runner.cleanup()


def _exchange_events(broker: StreamEventBroker) -> List[Any]:
    return [ev for ev in broker.recent_history(limit=1000)
            if xp.is_exchange_op(ev.op_id)]


OP = xp.EXCHANGE_OP_PREFIX + "reflect-1"


# --------------------------------------------------------------------------- #
# (A) no ping-pong: publish 1 event each side; each broker ends with EXACTLY 2.
# --------------------------------------------------------------------------- #
def test_no_reflection_storm_exact_counts(monkeypatch):
    async def scenario():
        pair = _Pair(monkeypatch)
        await pair.start()
        await pair.await_connected()

        pair.client_broker.publish(xp.EXCHANGE_EVENT_TYPE, OP + "-c", {"n": 1})
        pair.server_broker.publish(xp.EXCHANGE_EVENT_TYPE, OP + "-s", {"n": 2})
        await asyncio.sleep(1.5)  # plenty of time for any storm to develop

        client_n = len(_exchange_events(pair.client_broker))
        server_n = len(_exchange_events(pair.server_broker))
        await pair.stop()
        return client_n, server_n

    client_n, server_n = asyncio.get_event_loop().run_until_complete(scenario())
    assert client_n == 2, f"client broker must hold exactly 2 (own + peer), got {client_n}"
    assert server_n == 2, f"server broker must hold exactly 2 (own + peer), got {server_n}"


# --------------------------------------------------------------------------- #
# (B) connected property gates publishes.
# --------------------------------------------------------------------------- #
def test_client_connected_property_reflects_link(monkeypatch):
    async def scenario():
        pair = _Pair(monkeypatch)
        await pair.start()
        await pair.await_connected()  # asserts the property flips True
        client = pair.client_bus._client
        await pair.stop()
        return getattr(client, "connected", True)

    connected_after_stop = asyncio.get_event_loop().run_until_complete(scenario())
    assert connected_after_stop is False, "stop must clear connected"


# --------------------------------------------------------------------------- #
# (C) the outbound cursor survives client recreation: sever (bus.stop),
#     publish while severed, start_client again -> severed span crosses.
# --------------------------------------------------------------------------- #
def test_severed_span_crosses_after_client_recreation(monkeypatch):
    async def scenario():
        pair = _Pair(monkeypatch)
        await pair.start()
        await pair.await_connected()

        pair.client_broker.publish(xp.EXCHANGE_EVENT_TYPE, OP + "-pre", {"i": 0})
        await asyncio.sleep(0.5)

        await pair.client_bus.stop()  # SEVER (kills the client instance)
        await asyncio.sleep(0.3)  # let the link fully die -- no flush race
        severed_ids = [
            pair.client_broker.publish(
                xp.EXCHANGE_EVENT_TYPE, OP + "-sev-%d" % i, {"i": i})
            for i in range(3)
        ]
        assert all(severed_ids)
        await asyncio.sleep(0.3)
        # HONESTY GATE: while severed, the span must NOT have crossed -- if it
        # did, the "sever" is a flush race and this test proves nothing.
        mid_ops = {ev.op_id for ev in _exchange_events(pair.server_broker)}
        assert not any((OP + "-sev-%d" % i) in mid_ops for i in range(3)), (
            "severed events crossed BEFORE reconnect -- sever is not real")

        task = asyncio.ensure_future(pair.client_bus.start_client(pair.url))
        await pair.await_connected()
        await asyncio.sleep(1.0)

        server_ops = {ev.op_id for ev in _exchange_events(pair.server_broker)}
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await pair.stop()
        return server_ops

    server_ops = asyncio.get_event_loop().run_until_complete(scenario())
    for i in range(3):
        assert (OP + "-sev-%d" % i) in server_ops, (
            "severed-span event %d must cross via the carried cursor: %r"
            % (i, server_ops))
