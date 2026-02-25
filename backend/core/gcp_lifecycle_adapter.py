# backend/core/gcp_lifecycle_adapter.py
"""GCP lifecycle side-effect adapter.

Maps state machine transition events to actual GCP operations
(create VM, stop VM, switch routing). All operations include an op_id
for idempotent tracking and journal correlation.

Design doc: docs/plans/2026-02-25-journal-backed-gcp-lifecycle-design.md
Section 5: Lease-Safe Side Effect Protocol.
"""
import logging
import uuid
from typing import Any, Dict

from backend.core.gcp_lifecycle_state_machine import SideEffectAdapter

logger = logging.getLogger("jarvis.gcp_lifecycle_adapter")

# Events that trigger VM creation
_CREATE_EVENTS = frozenset({"budget_approved"})

# Events that trigger VM stop
_STOP_EVENTS = frozenset({
    "cooldown_expired",
    "session_shutdown",
    "budget_exhausted_runtime",
    "manual_force_local",
    "boot_deadline_exceeded",
})

# Events that trigger routing switch to cloud
_ROUTE_TO_CLOUD_EVENTS = frozenset({
    "health_probe_ok",
    "handshake_succeeded",
})

# Events that trigger routing switch to local (+ budget release)
_ROUTE_TO_LOCAL_EVENTS = frozenset({
    "spot_preempted",
    "health_unreachable_consecutive",
    "vm_create_failed",
    "handshake_failed",
})


class GCPLifecycleAdapter(SideEffectAdapter):
    """Adapter wiring state machine transitions to GCP operations.

    Usage:
        adapter = GCPLifecycleAdapter(journal, gcp_vm_manager)
        sm = GCPLifecycleStateMachine(journal, adapter, target="invincible_node")
    """

    def __init__(self, journal: Any, gcp_vm_manager: Any) -> None:
        self._journal = journal
        self._gcp = gcp_vm_manager

    async def execute(self, action: str, op_id: str, **kwargs) -> Dict[str, Any]:
        """Dispatch a side-effect action to the appropriate GCP operation."""
        try:
            if action in _CREATE_EVENTS:
                return await self._gcp.create_vm(op_id=op_id, **kwargs)

            if action in _STOP_EVENTS:
                return await self._gcp.stop_vm(op_id=op_id, **kwargs)

            if action in _ROUTE_TO_CLOUD_EVENTS:
                return await self._gcp.switch_routing(
                    direction="cloud", op_id=op_id, **kwargs,
                )

            if action in _ROUTE_TO_LOCAL_EVENTS:
                return await self._gcp.switch_routing(
                    direction="local", op_id=op_id, **kwargs,
                )

            # Unknown action -- no-op
            logger.debug(
                "No side-effect mapping for action=%s (op_id=%s)", action, op_id,
            )
            return {"status": "no_op", "action": action}

        except Exception as exc:
            logger.error(
                "GCP side-effect failed: action=%s op_id=%s error=%s",
                action, op_id, exc,
                exc_info=True,
            )
            return {"error": str(exc), "action": action, "op_id": op_id}

    async def query_vm_state(self, op_id: str) -> str:
        """Query actual VM state for reconciliation.

        Delegates to the GCP VM manager to check if a VM associated
        with the given op_id exists and what state it's in.

        Returns one of: 'running', 'stopped', 'not_found'.
        """
        try:
            result = await self._gcp.query_vm_state(op_id=op_id)
            if isinstance(result, str):
                return result
            # If the manager returns a dict, extract state
            return result.get("state", "not_found")
        except Exception as exc:
            logger.error(
                "Failed to query VM state for op_id=%s: %s",
                op_id, exc,
                exc_info=True,
            )
            return "not_found"

    @staticmethod
    def generate_op_id(target: str, event: str, epoch: int) -> str:
        """Generate a stable, unique op_id for tracking."""
        short_uuid = uuid.uuid4().hex[:8]
        return f"{target}:{event}:{epoch}:{short_uuid}"
