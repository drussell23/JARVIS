"""Tests for real Gmail outcome detection in OutcomeCollector."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.outcome_collector import OutcomeCollector
from autonomy.email_triage.schemas import EmailFeatures, TriagedEmail


def _make_features(message_id="msg-1", label_ids=("INBOX",)):
    return EmailFeatures(
        message_id=message_id, sender="user@example.com",
        sender_domain="example.com", subject="Test",
        snippet="hi", is_reply=False, has_attachment=False,
        label_ids=label_ids, keywords=(), sender_frequency="occasional",
        urgency_signals=(), extraction_confidence=0.8,
        extraction_source="heuristic",
    )


def _make_triaged(message_id="msg-1", tier=2, label_ids=("INBOX",)):
    scoring = MagicMock(tier=tier, score=70, tier_label="jarvis/tier2_high")
    return TriagedEmail(
        features=_make_features(message_id, label_ids),
        scoring=scoring,
        notification_action="immediate",
        processed_at=1000.0,
    )


def _mock_workspace_agent(label_responses):
    """Create a workspace agent mock that returns specified labels per message_id."""
    agent = AsyncMock()
    async def get_message_labels(message_id):
        return label_responses.get(message_id, set())
    agent.get_message_labels = get_message_labels
    return agent


class TestOutcomeDetection:
    @pytest.mark.asyncio
    async def test_replied_detected(self):
        """If SENT label appears, outcome should be 'replied'."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1", label_ids=("INBOX",))}

        ws = _mock_workspace_agent({"msg-1": {"INBOX", "SENT"}})
        outcomes = await collector.check_outcomes_for_cycle(ws, prior)
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "replied"

    @pytest.mark.asyncio
    async def test_deleted_detected(self):
        """If TRASH label appears, outcome should be 'deleted'."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1", label_ids=("INBOX",))}

        ws = _mock_workspace_agent({"msg-1": {"TRASH"}})
        outcomes = await collector.check_outcomes_for_cycle(ws, prior)
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "deleted"

    @pytest.mark.asyncio
    async def test_archived_detected(self):
        """If INBOX removed but not trashed, outcome should be 'archived'."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1", label_ids=("INBOX",))}

        ws = _mock_workspace_agent({"msg-1": {"IMPORTANT"}})
        outcomes = await collector.check_outcomes_for_cycle(ws, prior)
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "archived"

    @pytest.mark.asyncio
    async def test_relabeled_detected(self):
        """If labels changed (not trash, not archived), outcome should be 'relabeled'."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1", label_ids=("INBOX", "jarvis/tier3_review"))}

        ws = _mock_workspace_agent({"msg-1": {"INBOX", "jarvis/tier1_critical"}})
        outcomes = await collector.check_outcomes_for_cycle(ws, prior)
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "relabeled"

    @pytest.mark.asyncio
    async def test_no_change_no_outcome(self):
        """If labels unchanged, no outcome recorded."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1", label_ids=("INBOX",))}

        ws = _mock_workspace_agent({"msg-1": {"INBOX"}})
        outcomes = await collector.check_outcomes_for_cycle(ws, prior)
        assert len(outcomes) == 0

    @pytest.mark.asyncio
    async def test_api_error_returns_empty(self):
        """Gmail API errors should not crash — return empty list."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1")}

        ws = AsyncMock()
        ws.get_message_labels = AsyncMock(side_effect=Exception("Gmail 503"))
        outcomes = await collector.check_outcomes_for_cycle(ws, prior)
        assert outcomes == []

    @pytest.mark.asyncio
    async def test_reactor_enqueue_called_for_high_confidence(self):
        """Replied/deleted/relabeled outcomes should enqueue to reactor-core."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1", label_ids=("INBOX",))}

        ws = _mock_workspace_agent({"msg-1": {"TRASH"}})

        with patch.object(collector, "_enqueue_to_reactor_core", new_callable=AsyncMock) as mock_enqueue:
            await collector.check_outcomes_for_cycle(ws, prior)
            mock_enqueue.assert_called_once()
            record = mock_enqueue.call_args[0][0]
            assert record["outcome"] == "deleted"
            assert record["confidence"] == "high"
