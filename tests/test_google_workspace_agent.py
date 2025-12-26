"""
Test Suite for Google Workspace Agent
======================================

Tests the GoogleWorkspaceAgent including:
- Intent detection for intelligent routing
- Email operations (fetch, draft, send, search)
- Calendar operations (check, create)
- Contacts operations
- Natural language query handling

Author: JARVIS AI System
"""

import asyncio
import pytest
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

# Add project paths
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))


# =============================================================================
# Intent Detection Tests
# =============================================================================

class TestWorkspaceIntentDetector:
    """Test the WorkspaceIntentDetector class."""

    @pytest.fixture
    def detector(self):
        from backend.neural_mesh.agents.google_workspace_agent import (
            WorkspaceIntentDetector
        )
        return WorkspaceIntentDetector()

    def test_detect_check_calendar(self, detector):
        """Should detect calendar check intents."""
        from backend.neural_mesh.agents.google_workspace_agent import WorkspaceIntent

        queries = [
            "What's on my calendar today?",
            "Check my calendar",
            "What meetings do I have?",
            "Show me my agenda",
            "My schedule for today",
        ]

        for query in queries:
            intent, confidence, metadata = detector.detect(query)
            assert intent == WorkspaceIntent.CHECK_CALENDAR, f"Failed for: {query}"
            assert confidence > 0

    def test_detect_check_email(self, detector):
        """Should detect email check intents."""
        from backend.neural_mesh.agents.google_workspace_agent import WorkspaceIntent

        queries = [
            "Check my email",
            "Any new emails?",
            "Show my inbox",
            "What emails do I have?",
            "Check inbox",
        ]

        for query in queries:
            intent, confidence, metadata = detector.detect(query)
            assert intent == WorkspaceIntent.CHECK_EMAIL, f"Failed for: {query}"
            assert confidence > 0

    def test_detect_send_email(self, detector):
        """Should detect email send intents."""
        from backend.neural_mesh.agents.google_workspace_agent import WorkspaceIntent

        queries = [
            "Send an email to John",
            "Send email to the team",
            "Send a message to Mitra",
        ]

        for query in queries:
            intent, confidence, metadata = detector.detect(query)
            assert intent in [
                WorkspaceIntent.SEND_EMAIL,
                WorkspaceIntent.DRAFT_EMAIL,
            ], f"Failed for: {query}"

    def test_detect_draft_email(self, detector):
        """Should detect email draft intents."""
        from backend.neural_mesh.agents.google_workspace_agent import WorkspaceIntent

        queries = [
            "Draft an email to Mitra",
            "Write an email to the client",
            "Compose email to John",
            "Draft reply to the meeting invite",
        ]

        for query in queries:
            intent, confidence, metadata = detector.detect(query)
            assert intent in [
                WorkspaceIntent.DRAFT_EMAIL,
                WorkspaceIntent.SEND_EMAIL,
            ], f"Failed for: {query}"

    def test_detect_daily_briefing(self, detector):
        """Should detect daily briefing intents."""
        from backend.neural_mesh.agents.google_workspace_agent import WorkspaceIntent

        queries = [
            "Give me my daily briefing",
            "Morning briefing please",
            "Daily summary",
            "Catch me up",
        ]

        for query in queries:
            intent, confidence, metadata = detector.detect(query)
            assert intent in [
                WorkspaceIntent.DAILY_BRIEFING,
                WorkspaceIntent.CHECK_CALENDAR,
            ], f"Failed for: {query}"

    def test_detect_unknown_intent(self, detector):
        """Should return UNKNOWN for non-workspace queries."""
        from backend.neural_mesh.agents.google_workspace_agent import WorkspaceIntent

        queries = [
            "What is the weather like?",
            "Tell me a joke",
            "Write some Python code",
            "Debug this error",
            "How are you today?",
        ]

        for query in queries:
            intent, confidence, metadata = detector.detect(query)
            assert intent == WorkspaceIntent.UNKNOWN, f"False positive for: {query}"

    def test_extract_names(self, detector):
        """Should extract names from queries."""
        queries_with_names = [
            ("Draft an email to Mitra", ["Mitra"]),
            ("Send message to John", ["John"]),
            ("Schedule meeting with Derek", ["Derek"]),
        ]

        for query, expected_names in queries_with_names:
            _, _, metadata = detector.detect(query)
            extracted = metadata.get("extracted_names", [])
            for name in expected_names:
                assert name in extracted, f"Didn't extract {name} from: {query}"

    def test_extract_dates(self, detector):
        """Should extract date references from queries."""
        intent, confidence, metadata = detector.detect("What's on my calendar today?")
        dates = metadata.get("extracted_dates", {})
        assert "today" in dates
        assert dates["today"] == date.today().isoformat()

        intent, confidence, metadata = detector.detect("What meetings tomorrow?")
        dates = metadata.get("extracted_dates", {})
        assert "tomorrow" in dates

    def test_is_workspace_query(self, detector):
        """Should correctly identify workspace queries for routing."""
        workspace_queries = [
            "Check my calendar",
            "Any new emails?",
            "Draft an email",
            "My schedule for today",
        ]

        non_workspace_queries = [
            "Calculate 2 plus 2",
            "Write Python code for me",
            "Debug this error please",
            "What is the capital of France",
        ]

        for query in workspace_queries:
            is_workspace, confidence = detector.is_workspace_query(query)
            assert is_workspace, f"Should be workspace: {query}"

        for query in non_workspace_queries:
            is_workspace, confidence = detector.is_workspace_query(query)
            assert not is_workspace, f"Should NOT be workspace: {query}"


# =============================================================================
# Google Workspace Agent Tests
# =============================================================================

class TestGoogleWorkspaceAgent:
    """Test the GoogleWorkspaceAgent class."""

    @pytest.fixture
    def agent(self):
        from backend.neural_mesh.agents.google_workspace_agent import (
            GoogleWorkspaceAgent,
            GoogleWorkspaceConfig,
        )

        config = GoogleWorkspaceConfig(
            credentials_path="/tmp/test_creds.json",
            token_path="/tmp/test_token.json",
        )
        return GoogleWorkspaceAgent(config=config)

    def test_agent_initialization(self, agent):
        """Should initialize with correct capabilities."""
        assert agent.agent_name == "google_workspace_agent"
        assert agent.agent_type == "admin"
        assert "fetch_unread_emails" in agent.capabilities
        assert "check_calendar_events" in agent.capabilities
        assert "draft_email_reply" in agent.capabilities
        assert "send_email" in agent.capabilities
        assert "handle_workspace_query" in agent.capabilities

    def test_is_workspace_query(self, agent):
        """Should detect workspace queries via agent method."""
        is_workspace, confidence = agent.is_workspace_query("Check my calendar")
        assert is_workspace
        assert confidence > 0

        is_workspace, confidence = agent.is_workspace_query("Calculate 2 plus 2")
        assert not is_workspace

    def test_get_stats(self, agent):
        """Should return stats dictionary."""
        stats = agent.get_stats()
        assert "email_queries" in stats
        assert "calendar_queries" in stats
        assert "capabilities" in stats


class TestGoogleWorkspaceAgentExecution:
    """Test agent task execution (mocked)."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Google Workspace client."""
        from backend.neural_mesh.agents.google_workspace_agent import (
            GoogleWorkspaceClient,
            GoogleWorkspaceConfig,
        )

        client = GoogleWorkspaceClient(GoogleWorkspaceConfig())
        client.authenticate = AsyncMock(return_value=True)
        client.fetch_unread_emails = AsyncMock(return_value={
            "emails": [
                {"id": "1", "from": "test@example.com", "subject": "Test", "snippet": "Hello"},
            ],
            "count": 1,
            "total_unread": 5,
        })
        client.get_calendar_events = AsyncMock(return_value={
            "events": [
                {"id": "1", "title": "Team Meeting", "start": "2025-12-25T10:00:00"},
            ],
            "count": 1,
        })
        client.draft_email = AsyncMock(return_value={
            "status": "created",
            "draft_id": "draft123",
            "message_id": "msg123",
        })
        client.send_email = AsyncMock(return_value={
            "status": "sent",
            "message_id": "msg456",
        })
        client.get_contacts = AsyncMock(return_value={
            "contacts": [
                {"name": "John Doe", "emails": ["john@example.com"]},
            ],
            "count": 1,
        })
        return client

    @pytest.fixture
    def agent_with_mock(self, mock_client):
        from backend.neural_mesh.agents.google_workspace_agent import (
            GoogleWorkspaceAgent,
            GoogleWorkspaceConfig,
        )

        agent = GoogleWorkspaceAgent(
            config=GoogleWorkspaceConfig(
                credentials_path="/tmp/test.json",
                token_path="/tmp/token.json",
            )
        )
        agent._client = mock_client
        return agent

    @pytest.mark.asyncio
    async def test_fetch_unread_emails(self, agent_with_mock, mock_client):
        """Should fetch unread emails."""
        result = await agent_with_mock.execute_task({
            "action": "fetch_unread_emails",
            "limit": 5,
        })

        assert result["count"] == 1
        assert len(result["emails"]) == 1
        assert result["emails"][0]["from"] == "test@example.com"
        mock_client.fetch_unread_emails.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_calendar_events(self, agent_with_mock, mock_client):
        """Should check calendar events."""
        result = await agent_with_mock.execute_task({
            "action": "check_calendar_events",
            "date": "today",
        })

        assert result["count"] == 1
        assert result["events"][0]["title"] == "Team Meeting"
        mock_client.get_calendar_events.assert_called_once()

    @pytest.mark.asyncio
    async def test_draft_email(self, agent_with_mock, mock_client):
        """Should create email draft."""
        result = await agent_with_mock.execute_task({
            "action": "draft_email_reply",
            "to": "mitra@example.com",
            "subject": "Re: Project Update",
            "body": "Thanks for the update!",
        })

        assert result["status"] == "created"
        assert "draft_id" in result
        mock_client.draft_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_email(self, agent_with_mock, mock_client):
        """Should send email."""
        result = await agent_with_mock.execute_task({
            "action": "send_email",
            "to": "team@example.com",
            "subject": "Meeting Notes",
            "body": "Here are the notes from today's meeting.",
        })

        assert result["status"] == "sent"
        mock_client.send_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_validation_errors(self, agent_with_mock):
        """Should return errors for missing required fields."""
        # Missing recipient
        result = await agent_with_mock.execute_task({
            "action": "send_email",
            "subject": "Test",
            "body": "Test body",
        })
        assert "error" in result

        # Missing subject
        result = await agent_with_mock.execute_task({
            "action": "draft_email_reply",
            "to": "test@example.com",
            "body": "Test body",
        })
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_natural_query_calendar(self, agent_with_mock, mock_client):
        """Should handle natural language calendar query."""
        result = await agent_with_mock.execute_task({
            "action": "handle_workspace_query",
            "query": "What's on my calendar today?",
        })

        # Should route to calendar check
        mock_client.get_calendar_events.assert_called()

    @pytest.mark.asyncio
    async def test_handle_natural_query_email(self, agent_with_mock, mock_client):
        """Should handle natural language email query."""
        result = await agent_with_mock.execute_task({
            "action": "handle_workspace_query",
            "query": "Check my email",
        })

        # Should route to email check
        mock_client.fetch_unread_emails.assert_called()


# =============================================================================
# Orchestrator Routing Tests
# =============================================================================

class TestOrchestratorWorkspaceRouting:
    """Test that the orchestrator correctly routes workspace queries."""

    @pytest.fixture
    def mock_registry(self):
        """Create a mock agent registry."""
        registry = MagicMock()
        registry.get_best_agent = AsyncMock(return_value=MagicMock(
            agent_name="google_workspace_agent",
            capabilities={"handle_workspace_query"},
        ))
        registry.find_by_capability = AsyncMock(return_value=[])
        registry.get_all_agents = AsyncMock(return_value=[])
        return registry

    @pytest.fixture
    def mock_bus(self):
        """Create a mock communication bus."""
        bus = MagicMock()
        bus.request = AsyncMock(return_value={"result": "success"})
        return bus

    @pytest.mark.asyncio
    async def test_calendar_query_routes_to_workspace(self, mock_registry, mock_bus):
        """Calendar queries should route to workspace agent."""
        from backend.neural_mesh.orchestration.multi_agent_orchestrator import (
            MultiAgentOrchestrator
        )

        orchestrator = MultiAgentOrchestrator(
            communication_bus=mock_bus,
            agent_registry=mock_registry,
        )

        tasks = await orchestrator.create_workflow_from_query(
            "What's on my calendar today?"
        )

        assert len(tasks) == 1
        assert tasks[0].required_capability == "handle_workspace_query"
        assert tasks[0].input_data.get("query") == "What's on my calendar today?"

    @pytest.mark.asyncio
    async def test_email_query_routes_to_workspace(self, mock_registry, mock_bus):
        """Email queries should route to workspace agent."""
        from backend.neural_mesh.orchestration.multi_agent_orchestrator import (
            MultiAgentOrchestrator
        )

        orchestrator = MultiAgentOrchestrator(
            communication_bus=mock_bus,
            agent_registry=mock_registry,
        )

        tasks = await orchestrator.create_workflow_from_query(
            "Check my email"
        )

        assert len(tasks) == 1
        assert tasks[0].required_capability == "handle_workspace_query"

    @pytest.mark.asyncio
    async def test_non_workspace_query_routes_elsewhere(self, mock_registry, mock_bus):
        """Non-workspace queries should NOT route to workspace agent."""
        from backend.neural_mesh.orchestration.multi_agent_orchestrator import (
            MultiAgentOrchestrator
        )

        orchestrator = MultiAgentOrchestrator(
            communication_bus=mock_bus,
            agent_registry=mock_registry,
        )

        tasks = await orchestrator.create_workflow_from_query(
            "Debug this Python error"
        )

        # Should route to debug workflow, not workspace
        assert tasks[0].required_capability != "handle_workspace_query"

    @pytest.mark.asyncio
    async def test_multiple_workspace_keywords(self, mock_registry, mock_bus):
        """Queries with multiple workspace keywords should still route correctly."""
        from backend.neural_mesh.orchestration.multi_agent_orchestrator import (
            MultiAgentOrchestrator
        )

        orchestrator = MultiAgentOrchestrator(
            communication_bus=mock_bus,
            agent_registry=mock_registry,
        )

        queries = [
            "Check my calendar and email",
            "What meetings and emails do I have?",
            "Give me my schedule for today",
            "Draft an email about the meeting",
        ]

        for query in queries:
            tasks = await orchestrator.create_workflow_from_query(query)
            assert tasks[0].required_capability == "handle_workspace_query", \
                f"Failed routing for: {query}"


# =============================================================================
# Integration Test
# =============================================================================

class TestGoogleWorkspaceIntegration:
    """Integration tests for the full workspace pipeline."""

    @pytest.mark.asyncio
    async def test_agent_in_production_list(self):
        """GoogleWorkspaceAgent should be in production agents list."""
        from backend.neural_mesh.agents.agent_initializer import PRODUCTION_AGENTS
        from backend.neural_mesh.agents.google_workspace_agent import GoogleWorkspaceAgent

        agent_classes = [a.__name__ for a in PRODUCTION_AGENTS]
        assert "GoogleWorkspaceAgent" in agent_classes

    @pytest.mark.asyncio
    async def test_agent_type_is_admin(self):
        """Agent type should be 'admin' for proper categorization."""
        from backend.neural_mesh.agents.google_workspace_agent import GoogleWorkspaceAgent

        agent = GoogleWorkspaceAgent()
        assert agent.agent_type == "admin"

    @pytest.mark.asyncio
    async def test_convenience_methods(self):
        """Convenience methods should work without errors."""
        from backend.neural_mesh.agents.google_workspace_agent import (
            GoogleWorkspaceAgent,
            GoogleWorkspaceConfig,
        )

        agent = GoogleWorkspaceAgent(
            config=GoogleWorkspaceConfig(
                credentials_path="/tmp/test.json",
                token_path="/tmp/token.json",
            )
        )

        # Mock the client
        agent._client = MagicMock()
        agent._client.authenticate = AsyncMock(return_value=True)
        agent._client.get_calendar_events = AsyncMock(return_value={
            "events": [], "count": 0
        })

        # Test convenience method
        result = await agent.check_schedule("today")
        assert "events" in result or "error" in result


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
