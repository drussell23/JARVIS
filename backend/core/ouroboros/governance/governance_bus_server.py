"""The `ov` end of the HUD link — mounted by declaring it.

`ov` owns the governed loop and publishes O+V activity onto its local
``StreamEventBroker`` as ``governance_forward`` frames. This is the socket
that lets the supervisor — where the EventStream and the Swift HUD live —
subscribe to them.

MOUNTED BY DECLARATION
----------------------
Exposing a module-level ``register_routes(app, **kw)`` under
``backend.core.ouroboros.governance`` is the whole registration:
``observability_route_registry`` walks the package at EventChannelServer
boot and mounts every module that declares one. A live `ov` daemon logs
``auto-mounted 12 observability surface(s) via registry``; this makes it 13.

That matters more than the line count it saves. The last three capabilities
that shipped unreachable in this repo were each one forgotten edit away from
a boot seam, and `audit_ratchet`'s own watchdog was among them. A surface
that mounts because it EXISTS cannot be forgotten.

WHY NOTHING HERE IMPLEMENTS A TRANSPORT
---------------------------------------
``DistributedEventBus`` is the seam, ``BusBridgeServer`` is the socket, and
``TransportConfig.from_env`` already carries the bounded queue, the
heartbeat, and the TLS posture. Inbound peer frames republish into the local
broker; outbound frames are drained from it. This module chooses a role and
hands over — a second transport would be a second thing to keep correct
while the first is load-bearing.

THE TLS POSTURE IS DERIVED, AND NOT INHERITED
---------------------------------------------
``TransportConfig.from_env`` reads ``JARVIS_BRAIN_WS_*`` — the same knobs
``brain_keeper`` and ``organism_bus_host`` read for the CROSS-HOST brain
link. Turning TLS off there to make a loopback telemetry socket convenient
would have silently downgraded that link too: a security regression bought
with an unrelated convenience.

So this link decides for itself, from the only fact that matters — whether
the socket can leave the machine. Bound to loopback, it runs plaintext;
reachable off-host, it requires TLS; and reachable off-host WITHOUT TLS is
refused outright rather than served. That is the rule
``ide_observability`` already enforces for its router, applied to a
transport. ``JARVIS_GOVERNANCE_BUS_TLS_ENABLED`` overrides the derivation in
either direction; it cannot override the refusal.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("Ouroboros.GovernanceBusServer")

GOVERNANCE_BUS_SERVER_SCHEMA_VERSION: str = "governance_bus_server.1"

__all__ = [
    "GOVERNANCE_BUS_SERVER_SCHEMA_VERSION",
    "get_server_bus",
    "register_routes",
    "reset_for_tests",
]

#: One bus per process. Held so ``/channel/health`` style surfaces and tests
#: can see whether the link was mounted at all.
_bus: Optional[Any] = None


def get_server_bus() -> Optional[Any]:
    """The mounted server-role bus, or None. Never constructs one."""
    return _bus


def register_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Auto-mount entry point. NEVER raises.

    The keyword arguments are part of the registry's contract and are
    deliberately unused: this surface is a WebSocket upgrade, not a JSON GET.
    Rate limiting a persistent link per-request would throttle the heartbeat,
    and CORS has no meaning for a socket that no browser opens. Accepting and
    ignoring them keeps the signature the registry validates without
    pretending to honour semantics that do not apply.
    """
    global _bus  # noqa: PLW0603
    try:
        from backend.api.governance_cross_process import bridge_enabled
        if not bridge_enabled():
            logger.debug("[GovBusServer] not mounted — bridge disabled")
            return
        if _bus is not None:
            return                      # idempotent; registry may re-walk

        from backend.api.governance_cross_process import (
            link_host, link_refusal, link_transport_config,
        )
        # Fails CLOSED on exactly one combination — reachable off-host AND
        # plaintext. Checked at BOTH ends, so neither can be talked into the
        # unsafe posture by the other's configuration.
        refusal = link_refusal()
        if refusal:
            logger.warning("[GovBusServer] %s", refusal)
            return

        from backend.core.ouroboros.governance.ide_observability_stream import (
            get_default_broker,
        )
        from backend.core.ouroboros.governance.transport.distributed_event_bus import (  # noqa: E501
            DistributedEventBus,
        )

        cfg = link_transport_config("server")
        bus = DistributedEventBus(get_default_broker(), cfg, role="server")
        bus.register_server_routes(app)
        _bus = bus
        logger.info(
            "[GovBusServer] mounted at %s on %s — O+V activity is now "
            "reachable by the supervisor's HUD bridge (tls=%s, derived from "
            "reachability, NOT inherited from the brain link's shared knob)",
            cfg.path, link_host(), cfg.tls_enabled,
        )
    except Exception:  # noqa: BLE001 — a telemetry link never blocks a boot
        logger.debug("[GovBusServer] mount degraded", exc_info=True)


def reset_for_tests() -> None:
    """Test-only."""
    global _bus  # noqa: PLW0603
    _bus = None
