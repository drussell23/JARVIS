"""fleet_registry.py -- Centralized Fleet Registry (dynamic multi-node topology).

The elastic fleet runs more than one J-Prime node concurrently (a 7B CPU survival
node + an elastic 32B GPU node). The orchestrator can no longer assume a single
SERVING endpoint. This registry holds each node CLASS's resolved endpoint
INDEPENDENTLY; the Reachability Racer registers each external IP as it wins, and
the router queries :meth:`endpoint_for` per-op to pick the exact node. Reaping a
class clears only that entry -- the survivor stays addressable.

Thread-safe (a plain ``threading.Lock`` -- registration happens from async
provision paths AND sync teardown). Process-wide singleton via
:func:`get_fleet_registry`.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class FleetRegistry:
    """A class -> endpoint map for the live fleet. Empty endpoints are ignored
    (never register a half-resolved node)."""

    def __init__(self) -> None:
        self._endpoints: Dict[str, str] = {}
        self._lock = threading.Lock()

    def register(self, node_class: str, endpoint: str) -> None:
        cls = str(node_class or "").strip()
        ep = str(endpoint or "").strip()
        if not cls or not ep:
            return
        with self._lock:
            self._endpoints[cls] = ep
        logger.info("[FleetRegistry] register class=%s endpoint=%s", cls, ep)

    def unregister(self, node_class: str) -> None:
        cls = str(node_class or "").strip()
        with self._lock:
            existed = self._endpoints.pop(cls, None)
        if existed is not None:
            logger.info("[FleetRegistry] unregister class=%s (was %s)", cls, existed)

    def endpoint_for(self, node_class: str) -> Optional[str]:
        with self._lock:
            return self._endpoints.get(str(node_class or "").strip())

    def is_registered(self, node_class: str) -> bool:
        with self._lock:
            return str(node_class or "").strip() in self._endpoints

    def classes(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._endpoints.keys())

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._endpoints)

    def clear(self) -> None:
        with self._lock:
            self._endpoints.clear()


_SINGLETON: Optional[FleetRegistry] = None
_SINGLETON_LOCK = threading.Lock()


def get_fleet_registry() -> FleetRegistry:
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = FleetRegistry()
    return _SINGLETON


def reset_fleet_registry() -> None:
    """Test-only: drop the singleton's state."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = FleetRegistry()


__all__ = ["FleetRegistry", "get_fleet_registry", "reset_fleet_registry"]
