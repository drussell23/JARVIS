"""OrganismBusHost -- the Brain organism's in-process mTLS WS bus (Stage-2).

Hosts the Stage-0 ``DistributedEventBus`` server endpoint INSIDE the organism
process and wires the Task-1 ``TrinityBusBridge`` to the organism's own
``TrinityEventBus`` -- so the trinity bus becomes reachable across hosts
without the Stage-1 standalone sidecar (``scripts/brain_bus_echo_server.py``,
which now early-exits on Stage-2 nodes via
``JARVIS_BRAIN_BUS_SIDECAR_ENABLED=false``).

Dark by default (Manifesto: env-gated, byte-identical off):

  * ``JARVIS_DISTRIBUTED_BUS_ENABLED`` master flag off  -> ``start()`` is
    False and touches nothing (no aiohttp import, no sockets, no bus).
  * ``JARVIS_BRAIN_WS_PORT`` unset/0                    -> False, dark.
  * TLS enabled (default) but no material resolvable    -> False -- REFUSES
    to fall through to a plaintext listener.

Env knobs: the existing ``JARVIS_BRAIN_WS_*`` family (port/host/TLS/identity,
see ``transport_config.py``) plus ``JARVIS_BRAIN_OUTBOUND_TOPICS`` (comma
list of TrinityEventBus patterns mirrored outbound; default
``actuation.*,telemetry.posture.*``).
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from backend.core.ouroboros.governance.transport.transport_config import (
    distributed_bus_enabled,
)

logger = logging.getLogger(__name__)

_ENV_OUTBOUND_TOPICS = "JARVIS_BRAIN_OUTBOUND_TOPICS"
_DEFAULT_OUTBOUND_TOPICS = "actuation.*,telemetry.posture.*"


def bus_host_enabled() -> bool:
    """Master switch for the organism-owned bus host.

    Re-export seam over ``distributed_bus_enabled()`` so the intake layer
    (and any future boot seam) imports exactly ONE module for both the
    flag check and the host class.
    """
    return distributed_bus_enabled()


def resolve_outbound_topics() -> List[str]:
    """Comma-list env knob; whitespace-tolerant; empty/blank -> default."""
    raw = (os.environ.get(_ENV_OUTBOUND_TOPICS) or "").strip()
    if not raw:
        raw = _DEFAULT_OUTBOUND_TOPICS
    topics = [t.strip() for t in raw.split(",") if t.strip()]
    return topics or [t.strip() for t in _DEFAULT_OUTBOUND_TOPICS.split(",")]


class OrganismBusHost:
    """Owns the WS listener + Stage-0 server bus + Task-1 trinity bridge.

    Lifecycle: ``await start() -> bool`` (False = stayed dark / refused),
    ``await stop()`` unwinds in reverse and never raises. All heavy imports
    (aiohttp, trinity bus, transport internals) happen INSIDE ``start()``
    after the flag/port/TLS gates -- master-OFF pays zero import cost.
    """

    def __init__(self, router: Any = None) -> None:
        # ``router`` is the UnifiedIntakeRouter handle, reserved for Task 3
        # (remote intake signals arriving over this bus get ingested into
        # the local intake pipeline). Stored but deliberately unused in
        # Task 2 so the Task-3 wiring lands without a constructor change.
        self._router = router
        self._broker: Optional[Any] = None
        self._bus: Optional[Any] = None  # DistributedEventBus (server role)
        self._bridge: Optional[Any] = None  # TrinityBusBridge
        self._runner: Optional[Any] = None  # aiohttp AppRunner
        self._site: Optional[Any] = None  # aiohttp TCPSite
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> bool:
        """Serve the organism bus. Returns False (dark, touches nothing)
        when the master flag is off, no port is configured, or TLS is
        enabled but material cannot be resolved (plaintext refusal)."""
        if self._started:
            return True
        if not bus_host_enabled():
            logger.debug("[OrganismBusHost] master flag off -- staying dark")
            return False

        from backend.core.ouroboros.governance.transport.transport_config import (
            TransportConfig,
        )

        cfg = TransportConfig.from_env(role="brain-server")
        if cfg.port <= 0:
            logger.info(
                "[OrganismBusHost] JARVIS_BRAIN_WS_PORT unset/0 -- staying dark")
            return False

        from backend.core.ouroboros.governance.transport.transport_security import (
            build_server_ssl_context,
        )

        try:
            ssl_ctx = build_server_ssl_context(cfg)
        except Exception as exc:  # noqa: BLE001 -- material unresolvable
            ssl_ctx = None
            logger.error(
                "[OrganismBusHost] TLS material could not be resolved: %s", exc)
        if cfg.tls_enabled and ssl_ctx is None:
            logger.error(
                "[OrganismBusHost] TLS enabled but no server ssl context "
                "(JARVIS_BRAIN_WS_TLS_CERT/_KEY/_CA?) -- refusing plaintext")
            return False

        try:
            from aiohttp import web

            from backend.core.ouroboros.governance.ide_observability_stream import (
                StreamEventBroker,
            )
            from backend.core.ouroboros.governance.transport.distributed_event_bus import (
                DistributedEventBus,
            )
            from backend.core.ouroboros.governance.transport.trinity_bus_bridge import (
                TrinityBusBridge,
            )
            from backend.core.trinity_event_bus import get_trinity_event_bus

            self._broker = StreamEventBroker()
            self._bus = DistributedEventBus(self._broker, cfg, role="server")
            app = web.Application()
            self._bus.register_server_routes(app)

            self._runner = web.AppRunner(app)
            await self._runner.setup()
            self._site = web.TCPSite(
                self._runner,
                host=cfg.host or "0.0.0.0",
                port=cfg.port,
                ssl_context=ssl_ctx,
            )
            await self._site.start()

            trinity_bus = await get_trinity_event_bus()
            self._bridge = TrinityBusBridge(
                trinity_bus,
                self._broker,
                outbound_topics=resolve_outbound_topics(),
                source_id=cfg.source_id,
            )
            await self._bridge.start()
        except Exception as exc:  # noqa: BLE001 -- fail-soft: unwind + dark
            logger.warning(
                "[OrganismBusHost] start failed (staying dark): %s", exc,
                exc_info=True,
            )
            await self.stop()
            return False

        self._started = True
        logger.info(
            "[OrganismBusHost] serving host=%s port=%d tls=%s path=%s "
            "outbound=%s source_id=%s",
            cfg.host or "0.0.0.0", cfg.port, bool(ssl_ctx), cfg.path,
            ",".join(resolve_outbound_topics()), cfg.source_id,
        )
        return True

    async def stop(self) -> None:
        """Unwind in reverse order. Never raises (fail-soft teardown);
        no-op on a never-started host."""
        self._started = False
        if self._bridge is not None:
            try:
                await self._bridge.stop()
            except Exception:  # noqa: BLE001
                logger.debug("[OrganismBusHost] bridge stop failed",
                             exc_info=True)
            self._bridge = None
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:  # noqa: BLE001
                logger.debug("[OrganismBusHost] site stop failed",
                             exc_info=True)
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:  # noqa: BLE001
                logger.debug("[OrganismBusHost] runner cleanup failed",
                             exc_info=True)
            self._runner = None
        self._bus = None
        self._broker = None
