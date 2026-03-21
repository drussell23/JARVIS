# tests/governance/test_mcp_server.py
"""OuroborosMCPServer: inbound MCP tool interface wrapping GovernedLoopService (GAP 10)."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_gls() -> MagicMock:
    """Return a minimal GLS mock with the attrs MCP server touches."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    gls._approval_provider = MagicMock()
    gls._approval_provider.approve = AsyncMock()
    gls._completed_ops = {}
    return gls


# ---------------------------------------------------------------------------
# test_server_has_required_tools
# ---------------------------------------------------------------------------

def test_server_has_required_tools():
    """OuroborosMCPServer exposes submit_intent, get_operation_status, approve_operation."""
    from backend.core.ouroboros.governance.mcp_server import OuroborosMCPServer

    gls = _make_gls()
    server = OuroborosMCPServer(gls=gls)

    assert hasattr(server, "submit_intent"), "submit_intent method missing"
    assert hasattr(server, "get_operation_status"), "get_operation_status method missing"
    assert hasattr(server, "approve_operation"), "approve_operation method missing"

    assert asyncio.iscoroutinefunction(server.submit_intent), "submit_intent must be async"
    assert asyncio.iscoroutinefunction(server.get_operation_status), "get_operation_status must be async"
    assert asyncio.iscoroutinefunction(server.approve_operation), "approve_operation must be async"


# ---------------------------------------------------------------------------
# test_submit_intent_calls_gls
# ---------------------------------------------------------------------------

def test_submit_intent_calls_gls():
    """submit_intent(goal, target_files, repo) calls gls.submit() and returns dict with op_id."""
    from backend.core.ouroboros.governance.mcp_server import OuroborosMCPServer
    from backend.core.ouroboros.governance.governed_loop_service import OperationResult
    from backend.core.ouroboros.governance.op_context import OperationPhase

    gls = _make_gls()
    fake_result = OperationResult(
        op_id="op-mcp-001",
        terminal_phase=OperationPhase.COMPLETE,
        terminal_class="PRIMARY_SUCCESS",
    )
    gls.submit.return_value = fake_result

    server = OuroborosMCPServer(gls=gls)
    result = _run(server.submit_intent(
        goal="add logging to auth module",
        target_files=["backend/auth.py"],
        repo="jarvis",
    ))

    # gls.submit must have been called exactly once
    gls.submit.assert_called_once()

    # result must be a dict with op_id
    assert isinstance(result, dict), "submit_intent must return a dict"
    assert "op_id" in result, "result dict must contain op_id"
    assert result["op_id"] == "op-mcp-001"


# ---------------------------------------------------------------------------
# test_get_operation_status_returns_dict
# ---------------------------------------------------------------------------

def test_get_operation_status_returns_dict():
    """get_operation_status returns dict with status key; None result -> not_found."""
    from backend.core.ouroboros.governance.mcp_server import OuroborosMCPServer

    gls = _make_gls()
    server = OuroborosMCPServer(gls=gls)

    # Case 1: op_id not in _completed_ops -> not_found
    result_missing = _run(server.get_operation_status("op-does-not-exist"))
    assert isinstance(result_missing, dict)
    assert "status" in result_missing
    assert result_missing["status"] == "not_found"

    # Case 2: op_id present in _completed_ops
    from backend.core.ouroboros.governance.governed_loop_service import OperationResult
    from backend.core.ouroboros.governance.op_context import OperationPhase
    fake_result = OperationResult(
        op_id="op-known",
        terminal_phase=OperationPhase.COMPLETE,
        terminal_class="PRIMARY_SUCCESS",
    )
    gls._completed_ops["op-known"] = fake_result

    result_found = _run(server.get_operation_status("op-known"))
    assert isinstance(result_found, dict)
    assert "status" in result_found
    assert result_found["status"] != "not_found"


# ---------------------------------------------------------------------------
# test_approve_operation_delegates
# ---------------------------------------------------------------------------

def test_approve_operation_delegates():
    """approve_operation calls gls._approval_provider.approve() and returns dict."""
    from backend.core.ouroboros.governance.mcp_server import OuroborosMCPServer
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalResult,
        ApprovalStatus,
    )
    from datetime import datetime, timezone

    gls = _make_gls()
    fake_approval = ApprovalResult(
        status=ApprovalStatus.APPROVED,
        approver="mcp_client",
        reason=None,
        decided_at=datetime.now(tz=timezone.utc),
        request_id="op-approve-001",
    )
    gls._approval_provider.approve.return_value = fake_approval

    server = OuroborosMCPServer(gls=gls)
    result = _run(server.approve_operation(
        request_id="op-approve-001",
        approver="mcp_client",
    ))

    gls._approval_provider.approve.assert_called_once_with(
        "op-approve-001", "mcp_client"
    )
    assert isinstance(result, dict)
    assert "status" in result


# ---------------------------------------------------------------------------
# test_submit_intent_error_returns_error_dict
# ---------------------------------------------------------------------------

def test_submit_intent_error_returns_error_dict():
    """When gls.submit raises, submit_intent returns error dict and never raises."""
    from backend.core.ouroboros.governance.mcp_server import OuroborosMCPServer

    gls = _make_gls()
    gls.submit.side_effect = RuntimeError("GLS exploded")

    server = OuroborosMCPServer(gls=gls)

    # Must not raise
    result = _run(server.submit_intent(
        goal="risky operation",
        target_files=["backend/risky.py"],
    ))

    assert isinstance(result, dict)
    assert result.get("status") == "error"
    assert "error" in result
    assert "GLS exploded" in result["error"]
