from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.distributed_event_bus import DistributedEventBus

pytestmark = pytest.mark.asyncio


def _cfg(**over):
    base = dict(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.1, reconnect_max_s=1.0, reconnect_jitter=0.0,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=False, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=False, source_id="brain-test",
    )
    base.update(over)
    return TransportConfig(**base)


async def test_publish_on_server_reaches_client_broker():
    server_broker = StreamEventBroker(history_maxlen=100)
    client_broker = StreamEventBroker(history_maxlen=100)
    server_bus = DistributedEventBus(server_broker, _cfg(source_id="brain"), role="server")
    app = web.Application()
    server_bus.register_server_routes(app)
    tclient = TestClient(TestServer(app))
    await tclient.start_server()
    url = str(tclient.make_url("/ws/trinity-bus")).replace("http", "ws")

    client_bus = DistributedEventBus(client_broker, _cfg(source_id="mac"), role="client")
    run_task = asyncio.ensure_future(client_bus.start_client(url=url))
    try:
        await asyncio.sleep(0.3)  # allow connect + hello
        # Publish on the SERVER; assert it lands in the CLIENT's broker.
        server_broker.publish("task_started", "op-42", {"hello": "world"})
        deadline = asyncio.get_event_loop().time() + 3.0
        found = False
        while asyncio.get_event_loop().time() < deadline:
            hist = client_broker.recent_history()
            if any(e.op_id == "op-42" and e.event_type == "task_started" for e in hist):
                found = True
                break
            await asyncio.sleep(0.05)
        assert found, "event published on server never reached client broker"
    finally:
        await client_bus.stop()
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):
            pass
        await tclient.close()
