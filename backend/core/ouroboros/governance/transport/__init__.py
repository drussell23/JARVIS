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
]

from backend.core.ouroboros.governance.transport.transport_config import (  # noqa: E402
    TransportConfig,
    distributed_bus_enabled,
)
