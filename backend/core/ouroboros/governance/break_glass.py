"""
Break-Glass Governance Tokens
==============================

Time-limited, scoped tokens that allow a human operator to temporarily promote
a BLOCKED operation to APPROVAL_REQUIRED.  Every issuance, validation, and
revocation is recorded in an audit trail.

Flow:
1. Derek: ``jarvis break-glass --scope <op_id> --ttl 300``
2. Token stored with audit (who, when, why, scope)
3. Operation proceeds under APPROVAL_REQUIRED rules (NOT unguarded)
4. Token auto-expires after TTL
5. Postmortem auto-generated for any break-glass usage
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("Ouroboros.BreakGlass")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BreakGlassExpired(RuntimeError):
    """Raised when a break-glass token has expired."""


class BreakGlassScopeMismatch(RuntimeError):
    """Raised when validating a token against the wrong op_id."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BreakGlassToken:
    """A time-limited break-glass token.

    Parameters
    ----------
    op_id:
        The operation this token is scoped to.
    reason:
        Human-provided justification for issuing the token.
    ttl:
        Time-to-live in seconds from issuance.
    issuer:
        Identity of the person who issued the token.
    issued_at:
        Wall-clock timestamp when the token was created.
    """

    op_id: str
    reason: str
    ttl: int
    issuer: str
    issued_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        """Check if this token has expired."""
        return time.time() >= self.issued_at + self.ttl


@dataclass
class BreakGlassAuditEntry:
    """A single audit trail record for break-glass activity.

    Parameters
    ----------
    op_id:
        The operation this entry relates to.
    action:
        What happened: ``issued``, ``validated``, ``revoked``, ``expired``.
    reason:
        Context for the action.
    issuer:
        Who performed the action (if applicable).
    timestamp:
        Wall-clock time of the action.
    """

    op_id: str
    action: str
    reason: str
    issuer: str = ""
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# BreakGlassManager
# ---------------------------------------------------------------------------


class BreakGlassManager:
    """Manages break-glass token lifecycle with full audit trail."""

    def __init__(self) -> None:
        self._tokens: Dict[str, BreakGlassToken] = {}
        self._audit: List[BreakGlassAuditEntry] = []

    async def issue(
        self,
        op_id: str,
        reason: str,
        ttl: int,
        issuer: str,
    ) -> BreakGlassToken:
        """Issue a new break-glass token.

        Parameters
        ----------
        op_id:
            The operation to scope this token to.
        reason:
            Human justification for the break-glass.
        ttl:
            Seconds until the token auto-expires.
        issuer:
            Identity of the person issuing.

        Returns
        -------
        BreakGlassToken
            The newly created token.
        """
        token = BreakGlassToken(
            op_id=op_id,
            reason=reason,
            ttl=ttl,
            issuer=issuer,
        )
        self._tokens[op_id] = token
        self._audit.append(
            BreakGlassAuditEntry(
                op_id=op_id,
                action="issued",
                reason=reason,
                issuer=issuer,
            )
        )
        logger.info(
            "Break-glass token issued: op=%s ttl=%ds issuer=%s reason=%s",
            op_id, ttl, issuer, reason,
        )
        return token

    def validate(self, op_id: str) -> bool:
        """Validate a break-glass token for the given op_id.

        Returns
        -------
        bool
            ``True`` if a valid, non-expired token exists for this op_id.
            ``False`` if no token exists.

        Raises
        ------
        BreakGlassExpired
            If the token exists but has expired.
        BreakGlassScopeMismatch
            If no token matches this op_id but tokens exist for other ops.
        """
        if op_id not in self._tokens:
            # Check if any tokens exist at all (scope mismatch detection)
            if self._tokens:
                raise BreakGlassScopeMismatch(
                    f"No break-glass token for op_id={op_id}. "
                    f"Active tokens exist for: {list(self._tokens.keys())}"
                )
            return False

        token = self._tokens[op_id]
        if token.is_expired():
            # Clean up and record
            self._tokens.pop(op_id, None)
            self._audit.append(
                BreakGlassAuditEntry(
                    op_id=op_id,
                    action="expired",
                    reason="token TTL exceeded",
                )
            )
            raise BreakGlassExpired(
                f"Break-glass token for op_id={op_id} has expired"
            )

        # Record validation
        self._audit.append(
            BreakGlassAuditEntry(
                op_id=op_id,
                action="validated",
                reason="token checked and valid",
                issuer=token.issuer,
            )
        )
        return True

    async def revoke(self, op_id: str, reason: str) -> None:
        """Revoke a break-glass token.

        Parameters
        ----------
        op_id:
            The operation whose token to revoke.
        reason:
            Why the token is being revoked.
        """
        token = self._tokens.pop(op_id, None)
        issuer = token.issuer if token else ""
        self._audit.append(
            BreakGlassAuditEntry(
                op_id=op_id,
                action="revoked",
                reason=reason,
                issuer=issuer,
            )
        )
        logger.info(
            "Break-glass token revoked: op=%s reason=%s",
            op_id, reason,
        )

    def get_promoted_tier(self, op_id: str) -> Optional[str]:
        """Get the promoted risk tier if a valid break-glass token exists.

        Break-glass always promotes to APPROVAL_REQUIRED, never unguarded.

        Returns
        -------
        Optional[str]
            ``"APPROVAL_REQUIRED"`` if valid token exists, ``None`` otherwise.
        """
        if op_id not in self._tokens:
            return None
        token = self._tokens[op_id]
        if token.is_expired():
            self._tokens.pop(op_id, None)
            return None
        return "APPROVAL_REQUIRED"

    def get_audit_trail(self) -> List[BreakGlassAuditEntry]:
        """Return the complete audit trail."""
        return list(self._audit)
