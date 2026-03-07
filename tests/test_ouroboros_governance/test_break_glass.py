"""Tests for break-glass governance tokens."""

import asyncio
import time
import pytest

from backend.core.ouroboros.governance.break_glass import (
    BreakGlassManager,
    BreakGlassToken,
    BreakGlassAuditEntry,
    BreakGlassExpired,
    BreakGlassScopeMismatch,
)


@pytest.fixture
def manager():
    return BreakGlassManager()


class TestTokenIssuance:
    @pytest.mark.asyncio
    async def test_issue_creates_valid_token(self, manager):
        """Issuing a break-glass token returns a valid, non-expired token."""
        token = await manager.issue(
            op_id="op-test-123",
            reason="emergency hotfix for prod outage",
            ttl=300,
            issuer="derek",
        )
        assert token.op_id == "op-test-123"
        assert token.ttl == 300
        assert token.issuer == "derek"
        assert not token.is_expired()

    @pytest.mark.asyncio
    async def test_issue_records_audit_entry(self, manager):
        """Every issuance creates an audit trail entry."""
        await manager.issue(
            op_id="op-test-456",
            reason="schema migration",
            ttl=60,
            issuer="derek",
        )
        audit = manager.get_audit_trail()
        assert len(audit) == 1
        assert audit[0].op_id == "op-test-456"
        assert audit[0].action == "issued"
        assert audit[0].reason == "schema migration"


class TestTokenValidation:
    @pytest.mark.asyncio
    async def test_validate_active_token_succeeds(self, manager):
        """Validating an active, non-expired token succeeds."""
        await manager.issue(
            op_id="op-valid",
            reason="testing",
            ttl=300,
            issuer="derek",
        )
        result = manager.validate(op_id="op-valid")
        assert result is True

    @pytest.mark.asyncio
    async def test_validate_expired_token_raises(self, manager):
        """Validating an expired token raises BreakGlassExpired."""
        await manager.issue(
            op_id="op-expired",
            reason="testing",
            ttl=0,  # Expires immediately
            issuer="derek",
        )
        # Token with ttl=0 is expired at creation
        with pytest.raises(BreakGlassExpired):
            manager.validate(op_id="op-expired")

    @pytest.mark.asyncio
    async def test_validate_wrong_scope_raises(self, manager):
        """Validating against wrong op_id raises BreakGlassScopeMismatch."""
        await manager.issue(
            op_id="op-scoped",
            reason="testing",
            ttl=300,
            issuer="derek",
        )
        with pytest.raises(BreakGlassScopeMismatch):
            manager.validate(op_id="op-different")

    @pytest.mark.asyncio
    async def test_validate_no_token_returns_false(self, manager):
        """Validating when no token exists returns False."""
        result = manager.validate(op_id="op-none")
        assert result is False


class TestTokenRevocation:
    @pytest.mark.asyncio
    async def test_revoke_removes_token(self, manager):
        """Revoking a token makes subsequent validation return False."""
        await manager.issue(
            op_id="op-revoke",
            reason="testing",
            ttl=300,
            issuer="derek",
        )
        await manager.revoke(op_id="op-revoke", reason="no longer needed")
        result = manager.validate(op_id="op-revoke")
        assert result is False

    @pytest.mark.asyncio
    async def test_revoke_creates_audit_entry(self, manager):
        """Revocation creates an audit trail entry."""
        await manager.issue(
            op_id="op-audit-revoke",
            reason="testing",
            ttl=300,
            issuer="derek",
        )
        await manager.revoke(op_id="op-audit-revoke", reason="done")
        audit = manager.get_audit_trail()
        revoke_entries = [e for e in audit if e.action == "revoked"]
        assert len(revoke_entries) == 1
        assert revoke_entries[0].reason == "done"


class TestPromotion:
    @pytest.mark.asyncio
    async def test_blocked_becomes_approval_required(self, manager):
        """Break-glass promotes BLOCKED to APPROVAL_REQUIRED, not unguarded."""
        await manager.issue(
            op_id="op-promote",
            reason="emergency",
            ttl=300,
            issuer="derek",
        )
        promoted_tier = manager.get_promoted_tier(op_id="op-promote")
        assert promoted_tier == "APPROVAL_REQUIRED"

    @pytest.mark.asyncio
    async def test_no_token_returns_none(self, manager):
        """No active token returns None for promoted tier."""
        result = manager.get_promoted_tier(op_id="op-missing")
        assert result is None


class TestAuditCompleteness:
    @pytest.mark.asyncio
    async def test_full_lifecycle_audit(self, manager):
        """Issue -> use -> revoke produces 3 audit entries."""
        await manager.issue(
            op_id="op-lifecycle",
            reason="prod fix",
            ttl=300,
            issuer="derek",
        )
        manager.validate(op_id="op-lifecycle")  # Records "validated" entry
        await manager.revoke(op_id="op-lifecycle", reason="complete")

        audit = manager.get_audit_trail()
        actions = [e.action for e in audit]
        assert "issued" in actions
        assert "validated" in actions
        assert "revoked" in actions
