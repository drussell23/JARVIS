from __future__ import annotations

import asyncio
import os
import ssl
import subprocess
import sys
import tempfile
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.bus_bridge_server import BusBridgeServer
from backend.core.ouroboros.governance.transport.transport_security import (
    build_server_ssl_context,
    build_client_ssl_context,
)

pytestmark = pytest.mark.asyncio


def _tls_cfg(**over):
    base = dict(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.05, reconnect_max_s=0.5, reconnect_jitter=0.0,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=True, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=True, source_id="brain",
    )
    base.update(over)
    return TransportConfig(**base)


async def test_mtls_handshake_succeeds_and_is_required():
    # Shared ephemeral material for both ends (loopback stands in for a
    # shared CA). Resolve once, feed both contexts the same cert/key.
    from backend.core.ouroboros.governance.transport import transport_security as ts
    cert, key = ts.generate_ephemeral_material()
    cfg = _tls_cfg(tls_ephemeral=False, tls_cert=cert, tls_key=key, tls_ca=cert)

    broker = StreamEventBroker(history_maxlen=100)
    app = web.Application()
    BusBridgeServer(broker, cfg).register_routes(app)
    server_ssl = build_server_ssl_context(cfg)
    tserver = TestServer(app)
    await tserver.start_server(ssl=server_ssl)
    host, port = tserver.host, tserver.port

    import aiohttp
    client_ssl = build_client_ssl_context(cfg)
    # (a) mTLS client connects.
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"wss://{host}:{port}/ws/trinity-bus", ssl=client_ssl) as ws:
            assert not ws.closed
    # (b) a plaintext / no-cert client is rejected (mTLS required).
    with pytest.raises((aiohttp.ClientConnectorError, ssl.SSLError, aiohttp.ClientError,
                        ConnectionResetError, aiohttp.WSServerHandshakeError)):
        no_verify = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        no_verify.check_hostname = False
        no_verify.verify_mode = ssl.CERT_NONE
        async with aiohttp.ClientSession() as s:
            # No client cert loaded -> server's CERT_REQUIRED rejects.
            async with s.ws_connect(f"wss://{host}:{port}/ws/trinity-bus", ssl=no_verify) as ws:
                await ws.receive()
    await tserver.close()


async def test_two_os_process_server_exchanges_events():
    portfile = os.path.join(tempfile.mkdtemp(prefix="jarvis-smoke-"), "port")
    env = dict(os.environ)
    env.update({
        "JARVIS_BRAIN_WS_HOST": "127.0.0.1",
        "JARVIS_BRAIN_WS_PORT": "0",
        "JARVIS_BRAIN_WS_TLS_ENABLED": "false",
        "JARVIS_BRAIN_WS_PORTFILE": portfile,
        "JARVIS_BRAIN_WS_SOURCE_ID": "brain-proc",
    })
    repo_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
    ).stdout.strip()
    proc = subprocess.Popen(
        [sys.executable, "-m",
         "backend.core.ouroboros.governance.transport._smoke_server"],
        env=env, cwd=repo_root,
    )
    try:
        # Wait for the child to write its bound port.
        deadline = time.time() + 10.0
        while time.time() < deadline and not os.path.exists(portfile):
            await asyncio.sleep(0.1)
        assert os.path.exists(portfile), "smoke server never bound"
        with open(portfile) as fh:
            port = int(fh.read().strip())

        client_broker = StreamEventBroker(history_maxlen=1024)
        from backend.core.ouroboros.governance.transport.bus_bridge_client import BusBridgeClient
        cfg = _tls_cfg(tls_enabled=False, tls_ephemeral=False)
        url = f"ws://127.0.0.1:{port}/ws/trinity-bus"
        client = BusBridgeClient(client_broker, cfg, url=url)
        run_task = asyncio.ensure_future(client.run())
        try:
            deadline = asyncio.get_event_loop().time() + 8.0
            got = False
            while asyncio.get_event_loop().time() < deadline:
                if any(e.op_id == "op-smoke" for e in client_broker.recent_history(limit=200)):
                    got = True
                    break
                await asyncio.sleep(0.1)
            assert got, "no events crossed the two-process boundary"
        finally:
            await client.stop()
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
