#!/usr/bin/env python3
"""Brain-side Stage-0 bus server + acceptance echo responder (Task-5 wiring).

Runs ON the Brain VM as the ``jarvis-brain-bus.service`` sidecar (written by the
ignition driver's runtime startup script -- the golden image needs no re-bake).
It is the piece that makes the cross-host acceptance REAL:

  1. serves the Stage-0 ``BusBridgeServer`` WS endpoint over mTLS (server cert
     SAN = the ``jarvis-brain`` DNS identity; certless clients are rejected
     before the WS upgrade);
  2. answers the driver's ValidationExchange via the pure
     ``exchange_protocol.respond`` rules (echoes prove Mac->Brain observation;
     nonce responses prove Brain->Mac publication);
  3. touches the liveness marker on WS-peer activity so the idle self-shutdown
     timer never reaps a node mid-acceptance (the deferred Task-1 coupling).

Config is 100% env-resolved (``/etc/jarvis/brain.env`` via systemd
``EnvironmentFile``): the ``JARVIS_BRAIN_WS_*`` family. No baked endpoints, no
literals. SIGTERM/SIGINT -> clean shutdown.

STAGE-2 HANDOFF: on Stage-2 nodes the Brain ORGANISM hosts this bus itself
(``backend/core/ouroboros/governance/transport/organism_bus_host.py`` --
``OrganismBusHost``), so this standalone sidecar must stand down there. Set
``JARVIS_BRAIN_BUS_SIDECAR_ENABLED=false`` in ``brain.env`` and ``main()``
early-exits with rc=0 (systemd sees a clean one-shot). Default ``true``
keeps Stage-1 nodes byte-identical.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logger = logging.getLogger("brain_bus_echo_server")

_ENV_LIVENESS_MARKER = "JARVIS_BRAIN_LIVENESS_MARKER"
_DEFAULT_LIVENESS_MARKER = "/run/jarvis/brain_liveness"


def _touch_liveness() -> None:
    """Best-effort liveness touch -- the idle timer reads this marker's mtime."""
    path = os.environ.get(_ENV_LIVENESS_MARKER, _DEFAULT_LIVENESS_MARKER)
    try:
        with open(path, "a", encoding="utf-8"):
            pass
        os.utime(path, None)
    except OSError:
        logger.debug("liveness touch failed path=%s", path, exc_info=True)


async def run_responder(broker, *, touch_fn=None) -> None:
    """Subscribe to the local broker and answer exchange traffic per the pure
    protocol rules. Injectable ``touch_fn`` for tests."""
    import backend.core.ouroboros.governance.transport.exchange_protocol as xp

    touch = touch_fn or _touch_liveness
    sub = broker.subscribe()
    if sub is None:
        raise RuntimeError("broker subscriber cap exceeded -- responder cannot run")
    try:
        while True:
            event = await sub.queue.get()
            touch()
            for etype, op_id, payload in xp.respond(
                event.event_type, event.op_id, event.payload,
            ):
                broker.publish(etype, op_id, payload)
    finally:
        broker.unsubscribe(sub)


async def amain() -> int:
    from aiohttp import web

    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
    )
    from backend.core.ouroboros.governance.transport.distributed_event_bus import (
        DistributedEventBus,
    )
    from backend.core.ouroboros.governance.transport.transport_config import (
        TransportConfig,
    )
    from backend.core.ouroboros.governance.transport.transport_security import (
        build_server_ssl_context,
    )

    cfg = TransportConfig.from_env(role="brain-server")
    if cfg.port <= 0:
        logger.error("JARVIS_BRAIN_WS_PORT unset/0 -- nothing to serve")
        return 2
    ssl_ctx = build_server_ssl_context(cfg)
    if cfg.tls_enabled and ssl_ctx is None:
        logger.error("TLS enabled but server ssl context could not be built "
                     "(cert/key/ca paths in brain.env?) -- refusing plaintext")
        return 3

    broker = StreamEventBroker()
    bus = DistributedEventBus(broker, cfg, role="server")
    app = web.Application()
    bus.register_server_routes(app)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=cfg.host or "0.0.0.0", port=cfg.port,
                       ssl_context=ssl_ctx)
    await site.start()
    _touch_liveness()
    logger.info("brain bus serving host=%s port=%d tls=%s path=%s",
                cfg.host or "0.0.0.0", cfg.port, bool(ssl_ctx), cfg.path)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    responder = asyncio.ensure_future(run_responder(broker))
    try:
        await stop.wait()
    finally:
        responder.cancel()
        try:
            await responder
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await runner.cleanup()
    return 0


def main() -> int:
    # Stage-2 handoff: the organism's OrganismBusHost owns the bus on
    # Stage-2 nodes -- the sidecar stands down cleanly (rc=0) when disabled.
    if os.environ.get("JARVIS_BRAIN_BUS_SIDECAR_ENABLED", "true").strip().lower() in (
        "0", "false", "no", "off",
    ):
        logging.basicConfig(
            level=os.environ.get("JARVIS_BRAIN_BUS_LOG_LEVEL", "INFO"),
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
        logger.info(
            "JARVIS_BRAIN_BUS_SIDECAR_ENABLED=false -- Stage-2 node: the "
            "organism's OrganismBusHost owns the bus; sidecar standing down")
        return 0
    logging.basicConfig(
        level=os.environ.get("JARVIS_BRAIN_BUS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
