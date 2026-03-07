"""Tests for brain_id scoping on experience entries."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.outcome_collector import OutcomeCollector
from autonomy.email_triage.config import TriageConfig


class TestBrainScopedOutcomes:
    @pytest.mark.asyncio
    async def test_enqueue_includes_brain_id(self):
        """Reactor-core experience entry must include brain_id=email_triage."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)

        record = {
            "outcome": "replied",
            "confidence": "high",
            "tier": 2,
            "sender_domain": "example.com",
        }

        with patch("core.experience_queue.enqueue_experience",
                    new_callable=AsyncMock) as mock_enqueue:
            await collector._enqueue_to_reactor_core(record)

            mock_enqueue.assert_called_once()
            call_kwargs = mock_enqueue.call_args
            # Get the data parameter - could be positional or keyword
            data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data") or call_kwargs[0][1]
            assert data["brain_id"] == "email_triage"
            assert data["source"] == "email_triage"
