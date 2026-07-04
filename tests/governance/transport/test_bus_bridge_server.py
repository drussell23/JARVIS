from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEventBroker,
    StreamEvent,
)
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport import bus_frame as bf
from backend.core.ouroboros.governance.transport.bus_bridge_server import BusBridgeServer

pytestmark = pytest.mark.asyncio


def _cfg():
    return TransportConfig(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.5, reconnect_max_s=30.0, reconnect_jitter=0.3,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=False, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=False, source_id="brain-test",
    )


async def _mk_client(broker, cfg, inbound_sink):
    server = BusBridgeServer(broker, cfg, on_inbound=lambda ev: inbound_sink.append(ev))
    app = web.Application()
    server.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return server, client


async def test_server_replays_history_since_last_event_id():
    broker = StreamEventBroker(history_maxlen=100)
    # Pre-load three events into history.
    id1 = broker.publish("task_started", "op-1", {"n": 1})
    id2 = broker.publish("task_updated", "op-1", {"n": 2})
    id3 = broker.publish("task_completed", "op-1", {"n": 3})
    sink = []
    server, client = await _mk_client(broker, _cfg(), sink)
    try:
        ws = await client.ws_connect("/ws/trinity-bus")
        await ws.send_bytes(bf.hello_frame("mac-test", last_event_id=id1).encode())
        got = []
        # Expect replay of id2, id3 (everything after id1).
        for _ in range(2):
            msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
            frame = bf.BusFrame.decode(msg.data)
            if frame and frame.kind == bf.FRAME_EVENT:
                got.append(frame.event["event_id"])
        assert got == [id2, id3]
        await ws.close()
    finally:
        await client.close()


async def test_server_dedups_replayed_inbound_events():
    broker = StreamEventBroker(history_maxlen=100)
    sink = []
    server, client = await _mk_client(broker, _cfg(), sink)
    try:
        ws = await client.ws_connect("/ws/trinity-bus")
        await ws.send_bytes(bf.hello_frame("mac-test", last_event_id=None).encode())
        ev = StreamEvent(event_id=format(7, "012x"), event_type="task_started",
                         op_id="op-9", timestamp="2026-07-03T00:00:00.000Z", payload={})
        frame = bf.event_frame(ev, source_id="mac-test").encode()
        await ws.send_bytes(frame)
        await ws.send_bytes(frame)  # duplicate resend
        await asyncio.sleep(0.2)
        assert len(sink) == 1  # deduped by qualified_id
        assert server.seen_count() == 1
        await ws.close()
    finally:
        await client.close()
