"""Distributed TrinityEventBus transport substrate (Stage 0).

Extends the in-process StreamEventBroker across a WebSocket so
publish() on one host reaches subscribers on the other. Ships dark
(JARVIS_DISTRIBUTED_BUS_ENABLED default false) until Stage 2 wires it
into the live loop.
"""
from __future__ import annotations

__all__ = [
    "TransportConfig",
    "distributed_bus_enabled",
    "DistributedEventBus",
    "BusBridgeServer",
    "BusBridgeClient",
    "build_client_ssl_context",
    "build_server_ssl_context",
]

from backend.core.ouroboros.governance.transport.transport_config import (  # noqa: E402,F401
    TransportConfig,
    distributed_bus_enabled,
)
from backend.core.ouroboros.governance.transport.distributed_event_bus import (  # noqa: E402,F401
    DistributedEventBus,
)
from backend.core.ouroboros.governance.transport.bus_bridge_server import (  # noqa: E402,F401
    BusBridgeServer,
)
from backend.core.ouroboros.governance.transport.bus_bridge_client import (  # noqa: E402,F401
    BusBridgeClient,
)
from backend.core.ouroboros.governance.transport.transport_security import (  # noqa: E402,F401
    build_client_ssl_context,
    build_server_ssl_context,
)
