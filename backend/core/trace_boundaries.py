"""Trace Boundary Registry — declares which functions are critical boundaries.

Maintains a registry of boundary crossings (HTTP, subprocess, internal) with
their classification (critical, standard). Feeds into ComplianceTracker for
CI gate reporting.

Usage:
    from backend.core.trace_boundaries import get_default_registry

    registry = get_default_registry()
    tracker = ComplianceTracker()
    registry.populate_tracker(tracker)
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BoundaryRegistry:
    """Registry of known boundary crossings that should carry trace context."""

    def __init__(self) -> None:
        self._boundaries: Dict[str, Dict[str, str]] = {}
        self._lock = threading.Lock()

    def register(
        self,
        name: str,
        boundary_type: str = "internal",
        classification: str = "standard",
    ) -> None:
        """Register a boundary crossing point."""
        with self._lock:
            self._boundaries[name] = {
                "name": name,
                "boundary_type": boundary_type,
                "classification": classification,
            }

    def list_boundaries(self) -> List[Dict[str, str]]:
        """List all registered boundaries."""
        with self._lock:
            return list(self._boundaries.values())

    def populate_tracker(self, tracker: Any) -> None:
        """Populate a ComplianceTracker with all registered boundaries."""
        with self._lock:
            for name, info in self._boundaries.items():
                tracker.register_boundary(name, info["classification"])


_default_registry: Optional[BoundaryRegistry] = None
_default_lock = threading.Lock()


def get_default_registry() -> BoundaryRegistry:
    """Get or create the default boundary registry with known boundaries."""
    global _default_registry
    if _default_registry is not None:
        return _default_registry
    with _default_lock:
        if _default_registry is not None:
            return _default_registry
        registry = BoundaryRegistry()

        # HTTP boundaries (outgoing requests)
        registry.register("prime_client.execute_request", "http", "critical")
        registry.register("prime_client.execute_stream_request", "http", "critical")
        registry.register("prime_client.check_health", "http", "standard")

        # Subprocess boundaries (GCP VM)
        registry.register("gcp_vm_manager.create_vm", "subprocess", "critical")
        registry.register("gcp_vm_manager.delete_vm", "subprocess", "standard")

        # File-based RPC boundaries
        registry.register("trinity_bridge.dispatch_event", "file_rpc", "critical")
        registry.register("trinity_bridge.receive_event", "file_rpc", "critical")

        # Internal boundaries (decision points)
        registry.register("decision_log.record", "internal", "standard")
        registry.register("supervisor.phase_transition", "internal", "critical")
        registry.register("supervisor.create_task", "internal", "standard")

        # Recovery boundaries
        registry.register("recovery.start", "internal", "critical")
        registry.register("recovery.complete", "internal", "standard")

        _default_registry = registry
        return _default_registry
