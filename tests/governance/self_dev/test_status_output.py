"""Tests for self-dev status command output."""
import argparse
import pytest
from unittest.mock import MagicMock

from backend.core.ouroboros.governance.loop_cli import handle_status
from backend.core.ouroboros.governance.integration import register_self_dev_commands


@pytest.mark.asyncio
async def test_handle_status_returns_string_with_key_fields():
    """handle_status returns a string containing State and Active ops."""
    mock_service = MagicMock()
    mock_service.health.return_value = {
        "state": "ACTIVE",
        "active_ops": 0,
        "completed_ops": 5,
        "uptime_s": 120.5,
        "provider_fsm_state": "PRIME_ACTIVE",
    }
    result = await handle_status(mock_service)
    assert "State:" in result
    assert "Active ops:" in result


@pytest.mark.asyncio
async def test_handle_status_returns_not_active_when_none():
    """handle_status returns degraded message when service is None."""
    result = await handle_status(None)
    assert "not active" in result.lower() or "not_active" in result.lower()


def test_register_self_dev_commands_adds_subparser():
    """register_self_dev_commands adds a self-dev subparser."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_self_dev_commands(subparsers)
    # Parse known self-dev commands
    ns = parser.parse_args(["self-modify", "--target", "foo.py", "--goal", "fix it"])
    assert ns.command == "self-modify"
    assert ns.target == "foo.py"
    assert ns.goal == "fix it"
