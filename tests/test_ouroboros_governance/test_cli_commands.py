# tests/test_ouroboros_governance/test_cli_commands.py
"""Tests for CLI break-glass command functions."""

import pytest

from backend.core.ouroboros.governance.cli_commands import (
    issue_break_glass,
    list_active_tokens,
    revoke_break_glass,
    get_audit_report,
)
from backend.core.ouroboros.governance.break_glass import (
    BreakGlassManager,
)


@pytest.fixture
def manager():
    return BreakGlassManager()


class TestIssueBreakGlass:
    @pytest.mark.asyncio
    async def test_issue_returns_token(self, manager):
        """issue_break_glass() returns a valid token."""
        token = await issue_break_glass(
            manager=manager,
            op_id="op-test-001",
            reason="emergency fix needed",
            ttl=300,
            issuer="derek",
        )
        assert token.op_id == "op-test-001"
        assert token.reason == "emergency fix needed"
        assert token.ttl == 300
        assert token.issuer == "derek"

    @pytest.mark.asyncio
    async def test_issued_token_validates(self, manager):
        """Issued token can be validated."""
        await issue_break_glass(
            manager=manager,
            op_id="op-test-002",
            reason="test",
            ttl=300,
            issuer="derek",
        )
        assert manager.validate("op-test-002") is True


class TestListTokens:
    @pytest.mark.asyncio
    async def test_list_empty(self, manager):
        """No tokens returns empty list."""
        tokens = list_active_tokens(manager)
        assert tokens == []

    @pytest.mark.asyncio
    async def test_list_active(self, manager):
        """Active tokens appear in list."""
        await issue_break_glass(
            manager=manager,
            op_id="op-test-010",
            reason="test",
            ttl=300,
            issuer="derek",
        )
        tokens = list_active_tokens(manager)
        assert len(tokens) == 1
        assert tokens[0].op_id == "op-test-010"


class TestRevokeBreakGlass:
    @pytest.mark.asyncio
    async def test_revoke_removes_token(self, manager):
        """Revoked token no longer validates."""
        await issue_break_glass(
            manager=manager,
            op_id="op-test-020",
            reason="test",
            ttl=300,
            issuer="derek",
        )
        await revoke_break_glass(
            manager=manager,
            op_id="op-test-020",
            reason="no longer needed",
        )
        tokens = list_active_tokens(manager)
        assert len(tokens) == 0


class TestAuditReport:
    @pytest.mark.asyncio
    async def test_audit_report_includes_actions(self, manager):
        """Audit report includes issue and revoke actions."""
        await issue_break_glass(
            manager=manager,
            op_id="op-test-030",
            reason="emergency",
            ttl=300,
            issuer="derek",
        )
        await revoke_break_glass(
            manager=manager,
            op_id="op-test-030",
            reason="resolved",
        )
        report = get_audit_report(manager)
        assert len(report) >= 2
        actions = [entry.action for entry in report]
        assert "issued" in actions
        assert "revoked" in actions
