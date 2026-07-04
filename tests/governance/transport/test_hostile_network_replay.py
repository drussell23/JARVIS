from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.bus_bridge_server import BusBridgeServer
from backend.core.ouroboros.governance.transport.bus_bridge_client import BusBridgeClient
from tests.governance.transport.hostile_network import HostileProxy

pytestmark = pytest.mark.asyncio


def _cfg(**over):
    base = dict(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.05, reconnect_max_s=0.5, reconnect_jitter=0.0,
        queue_maxsize=256, history_maxlen=4096, degrade_after_missed_hb=2,
        tls_enabled=False, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=False, source_id="brain",
    )
    base.update(over)
    return TransportConfig(**base)


async def test_replay_is_exact_across_drop_and_reorder():
    server_broker = StreamEventBroker(history_maxlen=4096)
    # Real upstream server hosting the bridge.
    up_app = web.Application()
    BusBridgeServer(server_broker, _cfg(source_id="brain")).register_routes(up_app)
    up = TestClient(TestServer(up_app))
    await up.start_server()
    upstream_url = str(up.make_url("/ws/trinity-bus")).replace("http", "ws")

    # Hostile proxy in front of it: reorder in windows of 3, drop the
    # connection after 5 frames to force a reconnect + Last-Event-ID replay.
    proxy = HostileProxy(upstream_url, reorder_window=3, drop_after=5,
                         latency_s=0.001, jitter_s=0.003, seed=7)
    px_app = web.Application()
    proxy.register(px_app, "/ws/trinity-bus")
    px = TestClient(TestServer(px_app))
    await px.start_server()
    proxy_url = str(px.make_url("/ws/trinity-bus")).replace("http", "ws")

    client_broker = StreamEventBroker(history_maxlen=4096)
    client = BusBridgeClient(client_broker, _cfg(source_id="mac"), url=proxy_url)
    run_task = asyncio.ensure_future(client.run())

    published = []
    try:
        await asyncio.sleep(0.2)
        # Publish 12 events on the server. The proxy drops mid-stream;
        # the client must reconnect and replay the gap from Last-Event-ID.
        for i in range(12):
            eid = server_broker.publish("task_updated", "op-hostile", {"i": i})
            published.append(eid)
            await asyncio.sleep(0.02)
        # Give reconnect + replay time to converge.
        deadline = asyncio.get_event_loop().time() + 6.0
        while asyncio.get_event_loop().time() < deadline:
            got = {e.event_id for e in client_broker.recent_history(limit=500)
                   if e.op_id == "op-hostile"}
            if set(published).issubset(got):
                break
            await asyncio.sleep(0.1)
        got_ids = [e.event_id for e in client_broker.recent_history(limit=500)
                   if e.op_id == "op-hostile"]
        # EXACT: every published id present, no duplicates.
        assert set(published).issubset(set(got_ids)), (
            f"missing after replay: {set(published) - set(got_ids)}"
        )
        assert len(got_ids) == len(set(got_ids)), "duplicate events after replay"
    finally:
        await client.stop()
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):
            pass
        await px.close()
        await up.close()
