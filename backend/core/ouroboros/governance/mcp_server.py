"""
OuroborosMCPServer — Inbound MCP Tool Interface
=================================================

Wraps :class:`GovernedLoopService` (GLS) and exposes three async tool methods
that an external MCP transport can call:

- :meth:`submit_intent`      — Forward a goal + file list to GLS.submit()
- :meth:`get_operation_status` — Query a completed operation by op_id
- :meth:`approve_operation`  — Delegate to the GLS approval provider

Design constraints
------------------
- **Standalone class** — MCP transport wiring is done externally.  This class
  has zero opinion about JSON-RPC, stdio, SSE, or HTTP framing.
- **Never raises** — all three public methods catch all exceptions and return
  structured error dicts so that a transport can always serialise the response.
- **No hardcoding** — repo, approver identity, and op_id are all passed in;
  defaults are permissive but overridable.

Trigger source
--------------
Operations submitted via this server are tagged ``trigger_source="mcp_server"``
so that governance telemetry can distinguish MCP-initiated work from CLI or
sensor-initiated work.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.op_context import OperationContext

logger = logging.getLogger("Ouroboros.MCPServer")

_TRIGGER_SOURCE = "mcp_server"


class OuroborosMCPServer:
    """Inbound MCP tool interface over :class:`GovernedLoopService`.

    Parameters
    ----------
    gls:
        A live (or mock) :class:`GovernedLoopService` instance.  The server
        reads ``gls._completed_ops`` and ``gls._approval_provider`` directly
        so it can function without requiring GLS to expose extra public API.
    """

    def __init__(self, gls: Any) -> None:
        self._gls = gls

    # ------------------------------------------------------------------
    # submit_intent
    # ------------------------------------------------------------------

    async def submit_intent(
        self,
        goal: str,
        target_files: Optional[List[str]] = None,
        repo: str = "jarvis",
    ) -> Dict[str, Any]:
        """Submit a goal to the governed pipeline.

        Creates an :class:`OperationContext` from *goal* and *target_files*,
        then calls ``gls.submit()`` with ``trigger_source="mcp_server"``.

        Parameters
        ----------
        goal:
            Human-readable description of the desired change.
        target_files:
            List of file paths this operation targets.  Defaults to an empty
            list when omitted.
        repo:
            Primary repository scope for the operation (default ``"jarvis"``).

        Returns
        -------
        dict
            On success: ``{"op_id": ..., "status": ..., "terminal_phase": ...,
            "terminal_class": ..., "reason_code": ...}``

            On error: ``{"status": "error", "error": "<message>"}``
        """
        files: List[str] = target_files or []
        try:
            ctx = OperationContext.create(
                target_files=tuple(files),
                description=goal,
                primary_repo=repo,
            )
            result = await self._gls.submit(ctx, trigger_source=_TRIGGER_SOURCE)
            return {
                "op_id": result.op_id,
                "status": result.terminal_phase.name.lower(),
                "terminal_phase": result.terminal_phase.name,
                "terminal_class": result.terminal_class,
                "reason_code": result.reason_code,
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("[MCPServer] submit_intent failed: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc)}

    # ------------------------------------------------------------------
    # get_operation_status
    # ------------------------------------------------------------------

    async def get_operation_status(self, op_id: str) -> Dict[str, Any]:
        """Return the status of a previously submitted operation.

        Looks up *op_id* in ``gls._completed_ops``.

        Parameters
        ----------
        op_id:
            The operation identifier returned by a prior :meth:`submit_intent`
            call.

        Returns
        -------
        dict
            When found: ``{"op_id": ..., "status": ..., "terminal_phase": ...,
            "terminal_class": ..., "reason_code": ..., "provider_used": ...,
            "total_duration_s": ...}``

            When not found: ``{"op_id": ..., "status": "not_found"}``

            On error: ``{"op_id": ..., "status": "error", "error": "..."}``
        """
        try:
            completed_ops: Dict[str, Any] = self._gls._completed_ops
            result = completed_ops.get(op_id)
            if result is None:
                return {"op_id": op_id, "status": "not_found"}
            return {
                "op_id": result.op_id,
                "status": result.terminal_phase.name.lower(),
                "terminal_phase": result.terminal_phase.name,
                "terminal_class": result.terminal_class,
                "reason_code": result.reason_code,
                "provider_used": result.provider_used,
                "total_duration_s": result.total_duration_s,
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("[MCPServer] get_operation_status failed: %s", exc, exc_info=True)
            return {"op_id": op_id, "status": "error", "error": str(exc)}

    # ------------------------------------------------------------------
    # approve_operation
    # ------------------------------------------------------------------

    async def approve_operation(
        self,
        request_id: str,
        approver: str = "mcp_client",
    ) -> Dict[str, Any]:
        """Approve a pending operation via the GLS approval provider.

        Delegates to ``gls._approval_provider.approve(request_id, approver)``.

        Parameters
        ----------
        request_id:
            The approval request identifier (same as the operation's ``op_id``
            for the built-in :class:`CLIApprovalProvider`).
        approver:
            Identity of the approver.  Defaults to ``"mcp_client"``.

        Returns
        -------
        dict
            On success: ``{"request_id": ..., "status": ..., "approver": ...,
            "decided_at": ...}``

            On error: ``{"request_id": ..., "status": "error", "error": "..."}``
        """
        try:
            approval_result = await self._gls._approval_provider.approve(
                request_id, approver
            )
            decided_at_str = (
                approval_result.decided_at.isoformat()
                if approval_result.decided_at is not None
                else None
            )
            return {
                "request_id": approval_result.request_id,
                "status": approval_result.status.name.lower(),
                "approver": approval_result.approver,
                "decided_at": decided_at_str,
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("[MCPServer] approve_operation failed: %s", exc, exc_info=True)
            return {"request_id": request_id, "status": "error", "error": str(exc)}

    # ------------------------------------------------------------------
    # reject_operation (GAP 5: completes approval lifecycle)
    # ------------------------------------------------------------------

    async def reject_operation(
        self,
        request_id: str,
        approver: str = "mcp_client",
        reason: str = "",
    ) -> Dict[str, Any]:
        """Reject a pending operation with an optional correction reason.

        The reason is persisted to OUROBOROS.md via CorrectionWriter so the
        brain learns from the rejection on subsequent operations.
        """
        try:
            rejection = await self._gls._approval_provider.reject(
                request_id, approver, reason
            )
            decided_at_str = (
                rejection.decided_at.isoformat()
                if rejection.decided_at is not None
                else None
            )
            return {
                "request_id": rejection.request_id,
                "status": rejection.status.name.lower(),
                "approver": rejection.approver,
                "reason": reason,
                "decided_at": decided_at_str,
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("[MCPServer] reject_operation failed: %s", exc, exc_info=True)
            return {"request_id": request_id, "status": "error", "error": str(exc)}

    # ------------------------------------------------------------------
    # elicit_answer (GAP 5: structured mid-operation input)
    # ------------------------------------------------------------------

    async def elicit_answer(
        self,
        request_id: str,
        answer: str,
    ) -> Dict[str, Any]:
        """Deliver an answer to a pending structured elicitation.

        Called by external systems (voice, TUI, web UI) when the pipeline
        has paused to ask the user a question via elicit().
        """
        try:
            provider = self._gls._approval_provider
            if hasattr(provider, "_set_elicitation_answer"):
                provider._set_elicitation_answer(request_id, answer)
                return {
                    "request_id": request_id,
                    "status": "answered",
                    "answer": answer,
                }
            return {
                "request_id": request_id,
                "status": "error",
                "error": "Approval provider does not support elicitation",
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("[MCPServer] elicit_answer failed: %s", exc, exc_info=True)
            return {"request_id": request_id, "status": "error", "error": str(exc)}
