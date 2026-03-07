"""Tests for GoogleWorkspaceAgent.get_message_labels()."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


def _make_agent():
    """Build a minimal GoogleWorkspaceClient mock for testing."""
    from neural_mesh.agents.google_workspace_agent import GoogleWorkspaceClient

    agent = GoogleWorkspaceClient.__new__(GoogleWorkspaceClient)
    agent._gmail_service = MagicMock()
    return agent


class TestGetMessageLabels:
    def test_sync_method_extracts_labels(self):
        """The sync method should call Gmail API and return label set."""
        agent = _make_agent()

        mock_msg = {"id": "msg-1", "labelIds": ["INBOX", "SENT"]}
        agent._gmail_service.users.return_value.messages.return_value.get.return_value.execute.return_value = mock_msg

        result = agent._get_message_labels_sync("msg-1")
        assert result == {"INBOX", "SENT"}

    def test_sync_method_returns_empty_on_no_labels(self):
        """Should return empty set if message has no labelIds."""
        agent = _make_agent()

        mock_msg = {"id": "msg-1"}
        agent._gmail_service.users.return_value.messages.return_value.get.return_value.execute.return_value = mock_msg

        result = agent._get_message_labels_sync("msg-1")
        assert result == set()

    @pytest.mark.asyncio
    async def test_async_delegates_to_retry_wrapper(self):
        """get_message_labels should call _execute_with_retry."""
        agent = _make_agent()

        with patch.object(agent, "_execute_with_retry", new_callable=AsyncMock,
                          return_value={"INBOX", "IMPORTANT"}) as mock_retry:
            result = await agent.get_message_labels("msg-1")

        assert result == {"INBOX", "IMPORTANT"}
        mock_retry.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_returns_empty_on_error(self):
        """Should return empty set on API error, not crash."""
        agent = _make_agent()

        with patch.object(agent, "_execute_with_retry", new_callable=AsyncMock,
                          side_effect=Exception("Gmail 503")):
            result = await agent.get_message_labels("msg-1")

        assert result == set()
