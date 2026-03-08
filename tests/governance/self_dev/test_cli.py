"""tests/governance/self_dev/test_cli.py

Unit tests for self-dev CLI entry points.
"""
import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.loop_cli import (
    handle_self_modify,
    handle_approve,
    handle_reject,
    handle_status,
)


def test_self_modify_sets_cli_manual_trigger():
    """handle_self_modify passes trigger_source='cli_manual'."""
    mock_service = MagicMock()
    mock_result = MagicMock()
    mock_result.op_id = "op-test"
    mock_result.terminal_phase = MagicMock(name="COMPLETE")
    mock_result.provider_used = "prime"
    mock_result.total_duration_s = 1.5
    mock_service.submit = AsyncMock(return_value=mock_result)

    asyncio.get_event_loop().run_until_complete(
        handle_self_modify(
            service=mock_service,
            target="tests/test_foo.py",
            goal="fix failing import",
        )
    )
    call_args = mock_service.submit.call_args
    # trigger_source should be 'cli_manual'
    assert call_args[1].get("trigger_source") == "cli_manual" or \
           (len(call_args[0]) > 1 and call_args[0][1] == "cli_manual")


def test_self_modify_raises_if_service_none():
    """handle_self_modify raises RuntimeError if service is None."""
    with pytest.raises(RuntimeError, match="not_active"):
        asyncio.get_event_loop().run_until_complete(
            handle_self_modify(service=None, target="foo.py", goal="fix")
        )


def test_approve_calls_provider():
    """handle_approve calls approval_provider.approve()."""
    mock_service = MagicMock()
    mock_service._approval_provider = MagicMock()
    mock_service._approval_provider.approve = AsyncMock(
        return_value=MagicMock(status=MagicMock(name="APPROVED"))
    )
    result = asyncio.get_event_loop().run_until_complete(
        handle_approve(service=mock_service, op_id="op-123")
    )
    mock_service._approval_provider.approve.assert_called_once()


def test_reject_raises_if_service_none():
    """handle_reject raises RuntimeError if service is None."""
    with pytest.raises(RuntimeError, match="not_active"):
        asyncio.get_event_loop().run_until_complete(
            handle_reject(service=None, op_id="op-123", reason="bad")
        )


def test_status_returns_string():
    """handle_status returns a formatted string summary."""
    mock_service = MagicMock()
    mock_service.health = MagicMock(return_value={
        "state": "ACTIVE",
        "active_ops": 0,
        "completed_ops": 3,
        "uptime_s": 120.0,
        "provider_fsm_state": "PRIMARY_ACTIVE",
    })

    result = asyncio.get_event_loop().run_until_complete(
        handle_status(service=mock_service, op_id=None)
    )
    assert isinstance(result, str)
    assert "ACTIVE" in result


def test_status_returns_inactive_when_service_none():
    """handle_status returns inactive message when service is None."""
    result = asyncio.get_event_loop().run_until_complete(
        handle_status(service=None, op_id=None)
    )
    assert "not active" in result.lower()


def test_self_modify_has_expected_params():
    """CLI function signature has service, target, goal params."""
    sig = inspect.signature(handle_self_modify)
    param_names = list(sig.parameters.keys())
    assert "service" in param_names
    assert "target" in param_names
    assert "goal" in param_names
