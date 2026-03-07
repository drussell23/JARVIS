# backend/core/ouroboros/governance/cli_commands.py
"""
CLI Break-Glass Commands -- Importable Functions for Supervisor CLI
====================================================================

Provides standalone async functions for break-glass operations that
can be wired into the supervisor's argparse CLI.

Usage from supervisor CLI::

    jarvis break-glass issue --op-id <op_id> --reason <reason> --ttl 300
    jarvis break-glass list
    jarvis break-glass revoke --op-id <op_id> --reason <reason>
    jarvis break-glass audit

These functions wrap :class:`BreakGlassManager` with CLI-friendly
signatures and return values.
"""

from __future__ import annotations

import logging
from typing import List

from backend.core.ouroboros.governance.break_glass import (
    BreakGlassAuditEntry,
    BreakGlassManager,
    BreakGlassToken,
)

logger = logging.getLogger("Ouroboros.CLI")


async def issue_break_glass(
    manager: BreakGlassManager,
    op_id: str,
    reason: str,
    ttl: int = 300,
    issuer: str = "cli",
) -> BreakGlassToken:
    """Issue a break-glass token for a blocked operation."""
    token = await manager.issue(
        op_id=op_id,
        reason=reason,
        ttl=ttl,
        issuer=issuer,
    )
    logger.info(
        "Break-glass issued: op=%s, ttl=%ds, issuer=%s",
        op_id, ttl, issuer,
    )
    return token


def list_active_tokens(manager: BreakGlassManager) -> List[BreakGlassToken]:
    """List all active (non-expired) break-glass tokens."""
    return [
        token for token in manager._tokens.values()
        if not token.is_expired()
    ]


async def revoke_break_glass(
    manager: BreakGlassManager,
    op_id: str,
    reason: str = "revoked via CLI",
) -> None:
    """Revoke a break-glass token."""
    await manager.revoke(op_id=op_id, reason=reason)
    logger.info("Break-glass revoked: op=%s, reason=%s", op_id, reason)


def get_audit_report(manager: BreakGlassManager) -> List[BreakGlassAuditEntry]:
    """Get the full break-glass audit trail."""
    return manager.get_audit_trail()
