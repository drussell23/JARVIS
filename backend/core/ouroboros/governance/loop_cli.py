"""
Governed Loop CLI Commands
===========================

Importable async functions for governed loop operations, following
the same pattern as cli_commands.py (break-glass).

These functions are wired into the supervisor's argparse CLI layer.
The governance package does not own command parsing.

Commands
--------
- ``handle_self_modify``: trigger a governed code generation pipeline
- ``handle_approve``: approve a pending operation
- ``handle_reject``: reject a pending operation
- ``handle_status``: query service health and operation state
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from backend.core.ouroboros.governance.op_context import OperationContext

logger = logging.getLogger("Ouroboros.CLI")


async def handle_self_modify(
    service: Any,
    target: str,
    goal: str,
    op_id: Optional[str] = None,
    dry_run: bool = False,
) -> Any:
    """Trigger a governed self-modification pipeline.

    Parameters
    ----------
    service:
        GovernedLoopService instance (or None if not started).
    target:
        Target file or directory path.
    goal:
        Description of the desired change.
    op_id:
        Optional explicit operation ID.
    dry_run:
        If True, run CLASSIFY + ROUTE only (no generation/apply).

    Returns
    -------
    OperationResult

    Raises
    ------
    RuntimeError
        If the service is not active.
    """
    if service is None:
        raise RuntimeError(
            "not_active: Governed loop is not active. "
            "Start JARVIS with governance enabled."
        )

    # Resolve target files
    target_files = (target,)

    # Build context
    ctx = OperationContext.create(
        target_files=target_files,
        description=goal,
        op_id=op_id,
    )

    logger.info(
        "[CLI] self-modify: target=%s goal=%r op_id=%s dry_run=%s",
        target,
        goal,
        ctx.op_id,
        dry_run,
    )

    # Submit to service
    result = await service.submit(ctx, trigger_source="cli_manual")

    logger.info(
        "[CLI] self-modify result: op_id=%s phase=%s provider=%s duration=%.1fs",
        result.op_id,
        result.terminal_phase.name,
        result.provider_used,
        result.total_duration_s,
    )

    return result


async def handle_approve(
    service: Any,
    op_id: str,
    approver: str = "cli-operator",
) -> Any:
    """Approve a pending governed operation.

    .. note:: Phase 1 limitation
       This requires an in-process reference to the running
       GovernedLoopService.  Cross-process approve/reject (separate
       CLI invocation) requires an IPC mechanism (Phase 2).

    Parameters
    ----------
    service:
        GovernedLoopService instance.
    op_id:
        The operation ID to approve.
    approver:
        Identity of the approver.

    Returns
    -------
    ApprovalResult

    Raises
    ------
    RuntimeError
        If the service is not active.
    KeyError
        If the op_id is unknown.
    """
    if service is None:
        raise RuntimeError(
            "not_active: Governed loop is not active."
        )

    result = await service._approval_provider.approve(op_id, approver)

    logger.info(
        "[CLI] approve: op_id=%s status=%s approver=%s",
        op_id,
        result.status.name,
        approver,
    )

    return result


async def handle_reject(
    service: Any,
    op_id: str,
    approver: str = "cli-operator",
    reason: str = "rejected via CLI",
) -> Any:
    """Reject a pending governed operation.

    Parameters
    ----------
    service:
        GovernedLoopService instance.
    op_id:
        The operation ID to reject.
    approver:
        Identity of the rejector.
    reason:
        Rejection reason.

    Returns
    -------
    ApprovalResult

    Raises
    ------
    RuntimeError
        If the service is not active.
    KeyError
        If the op_id is unknown.
    """
    if service is None:
        raise RuntimeError(
            "not_active: Governed loop is not active."
        )

    result = await service._approval_provider.reject(op_id, approver, reason)

    logger.info(
        "[CLI] reject: op_id=%s status=%s approver=%s reason=%r",
        op_id,
        result.status.name,
        approver,
        reason,
    )

    return result


async def handle_status(
    service: Any,
    op_id: Optional[str] = None,
) -> str:
    """Query service health and optional op_id state."""
    if service is None:
        return "Governed loop is not active."

    health = service.health()
    lines = [
        f"State: {health.get('state', 'unknown')}",
        f"Active ops: {health.get('active_ops', 0)}",
        f"Completed ops: {health.get('completed_ops', 0)}",
        f"Uptime: {health.get('uptime_s', 0):.1f}s",
        f"Provider: {health.get('provider_fsm_state', 'unknown')}",
    ]

    if op_id and op_id in getattr(service, '_completed_ops', {}):
        result = service._completed_ops[op_id]
        lines.append(f"\nOp {op_id}:")
        lines.append(f"  Phase: {result.terminal_phase.name}")
        lines.append(f"  Provider: {result.provider_used or 'none'}")
        lines.append(f"  Duration: {result.total_duration_s:.1f}s")

    return "\n".join(lines)
