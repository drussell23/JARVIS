# backend/core/ouroboros/governance/transport/_smoke_server.py
"""Runnable brain-side WS server for the two-process loopback smoke test.
Host/port/TLS come entirely from env -- no hardcoded endpoint. Writes the
bound port to the file named by JARVIS_BRAIN_WS_PORTFILE so the parent can
discover it (loopback stand-in for Stage 1's Reachability discovery)."""
from __future__ import annotations

import asyncio
import os

from aiohttp import web

from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.distributed_event_bus import DistributedEventBus
from backend.core.ouroboros.governance.transport.transport_security import (
    build_server_ssl_context,
)


async def _main() -> None:
    cfg = TransportConfig.from_env(role="server")
    broker = StreamEventBroker(history_maxlen=cfg.history_maxlen)
    bus = DistributedEventBus(broker, cfg, role="server")
    app = web.Application()
    bus.register_server_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    ssl_ctx = build_server_ssl_context(cfg)
    site = web.TCPSite(runner, cfg.host or "127.0.0.1", cfg.port, ssl_context=ssl_ctx)
    await site.start()
    # Publish one heartbeat event on a timer so the client observes traffic.
    portfile = os.environ.get("JARVIS_BRAIN_WS_PORTFILE")
    bound = site._server.sockets[0].getsockname()[1]  # actual bound port
    if portfile:
        with open(portfile, "w") as fh:
            fh.write(str(bound))
    for i in range(1000):
        broker.publish("task_updated", "op-smoke", {"i": i})
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(_main())
