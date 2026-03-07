"""Tests for outcome idempotency keys in reactor-core enqueue."""

import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.outcome_collector import OutcomeCollector
from autonomy.email_triage.config import TriageConfig


class TestOutcomeIdempotency:
    @pytest.mark.asyncio
    async def test_enqueue_includes_content_hash(self):
        """Each enqueue call must include a deterministic content_hash for dedup."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)

        record = {
            "message_id": "msg-123",
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
            metadata = call_kwargs.kwargs.get("metadata") or {}
            assert "content_hash" in metadata
            assert len(metadata["content_hash"]) == 32  # MD5 hex digest

    @pytest.mark.asyncio
    async def test_same_record_produces_same_hash(self):
        """Identical records must produce the same content_hash (deterministic)."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)

        record = {
            "message_id": "msg-123",
            "outcome": "deleted",
            "confidence": "high",
            "tier": 1,
            "sender_domain": "test.com",
        }

        hashes = []
        with patch("core.experience_queue.enqueue_experience",
                    new_callable=AsyncMock) as mock_enqueue:
            await collector._enqueue_to_reactor_core(record)
            h1 = mock_enqueue.call_args.kwargs.get("metadata", {}).get("content_hash")
            hashes.append(h1)

        with patch("core.experience_queue.enqueue_experience",
                    new_callable=AsyncMock) as mock_enqueue:
            await collector._enqueue_to_reactor_core(record)
            h2 = mock_enqueue.call_args.kwargs.get("metadata", {}).get("content_hash")
            hashes.append(h2)

        assert hashes[0] == hashes[1]

    @pytest.mark.asyncio
    async def test_different_outcomes_produce_different_hashes(self):
        """Different outcomes for the same message produce different hashes."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)

        record_a = {"message_id": "msg-1", "outcome": "replied", "confidence": "high", "tier": 2, "sender_domain": "a.com"}
        record_b = {"message_id": "msg-1", "outcome": "deleted", "confidence": "high", "tier": 2, "sender_domain": "a.com"}

        with patch("core.experience_queue.enqueue_experience",
                    new_callable=AsyncMock) as mock_enqueue:
            await collector._enqueue_to_reactor_core(record_a)
            h1 = mock_enqueue.call_args.kwargs.get("metadata", {}).get("content_hash")

        with patch("core.experience_queue.enqueue_experience",
                    new_callable=AsyncMock) as mock_enqueue:
            await collector._enqueue_to_reactor_core(record_b)
            h2 = mock_enqueue.call_args.kwargs.get("metadata", {}).get("content_hash")

        assert h1 != h2
