"""Tests for bounded recovery and runtime escalation."""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


class TestBoundedRecovery:

    @pytest.mark.asyncio
    async def test_no_recovery_when_verification_passes(self):
        from backend.api.unified_command_processor import _attempt_workspace_recovery
        result = {"emails": [{"subject": "Hi", "from": "a@b.com"}], "_verification": {"passed": True}}
        outcome = await _attempt_workspace_recovery(
            action="fetch_unread_emails",
            initial_result=result,
            initial_outcome="verify_passed",
            agent=MagicMock(),
            payload={},
            deadline=time.monotonic() + 30,
            command_text="check my email",
        )
        assert outcome["_verification"]["passed"] is True

    @pytest.mark.asyncio
    async def test_read_action_retries_same_tier(self):
        from backend.api.unified_command_processor import _attempt_workspace_recovery
        mock_agent = MagicMock()
        mock_agent.execute_task = AsyncMock(side_effect=[
            {"emails": [{"subject": "Hi", "from": "a@b.com"}]},
        ])
        result = {"data": "bad"}
        outcome = await _attempt_workspace_recovery(
            action="fetch_unread_emails",
            initial_result=result,
            initial_outcome="verify_schema_fail",
            agent=mock_agent,
            payload={"action": "fetch_unread_emails"},
            deadline=time.monotonic() + 30,
            command_text="check my email",
        )
        assert len(outcome.get("_attempts", [])) >= 1

    @pytest.mark.asyncio
    async def test_write_action_no_retry_without_idempotency(self):
        from backend.api.unified_command_processor import _attempt_workspace_recovery
        mock_agent = MagicMock()
        mock_agent.execute_task = AsyncMock()
        result = {"error": "failed"}
        outcome = await _attempt_workspace_recovery(
            action="send_email",
            initial_result=result,
            initial_outcome="verify_transport_fail",
            agent=mock_agent,
            payload={"action": "send_email"},
            deadline=time.monotonic() + 30,
            command_text="send email",
        )
        attempts = outcome.get("_attempts", [])
        same_tier = [a for a in attempts if a.get("strategy") == "same_tier_retry"]
        assert len(same_tier) == 0

    @pytest.mark.asyncio
    async def test_deadline_exhausted_returns_immediately(self):
        from backend.api.unified_command_processor import _attempt_workspace_recovery
        result = {"data": "bad"}
        outcome = await _attempt_workspace_recovery(
            action="fetch_unread_emails",
            initial_result=result,
            initial_outcome="verify_schema_fail",
            agent=MagicMock(),
            payload={"action": "fetch_unread_emails"},
            deadline=time.monotonic() - 1.0,
            command_text="check my email",
        )
        assert outcome.get("_recovery_reason") == "recovery_deadline_exhausted"
