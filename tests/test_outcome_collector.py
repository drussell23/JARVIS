# [Ouroboros] Modified by Ouroboros (op=op-01a10a83-) at 2026-10-05 05:23 UTC
# Reason: `backend/autonomy/email_triage/outcome_collector.py` has no corresponding test module. CREATE `tests/test_outcome_collec

# Copyright (c) 2024-2025, The Jarvis Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.outcome_collector import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    adaptation_weight,
    feeds_adaptation,
    outcome_confidence,
    OutcomeCollector,
)


class TestOutcomeConfidence:
    def test_outcome_confidence_high(self):
        assert outcome_confidence("replied") == CONFIDENCE_HIGH
        assert outcome_confidence("relabeled") == CONFIDENCE_HIGH
        assert outcome_confidence("deleted") == CONFIDENCE_HIGH

    def test_outcome_confidence_medium(self):
        assert outcome_confidence("archived") == CONFIDENCE_MEDIUM

    def test_outcome_confidence_low(self):
        assert outcome_confidence("opened") == CONFIDENCE_LOW
        assert outcome_confidence("ignored") == CONFIDENCE_LOW
        assert outcome_confidence("unknown") == CONFIDENCE_LOW  # default case


class TestFeedsAdaptation:
    def test_feeds_adaptation_high(self):
        assert feeds_adaptation("replied") is True
        assert feeds_adaptation("relabeled") is True
        assert feeds_adaptation("deleted") is True

    def test_feeds_adaptation_medium(self):
        assert feeds_adaptation("archived") is True

    def test_feeds_adaptation_low(self):
        assert feeds_adaptation("opened") is False
        assert feeds_adaptation("ignored") is False
        assert feeds_adaptation("unknown") is False  # default case


class TestAdaptationWeight:
    def test_adaptation_weight_high(self):
        assert adaptation_weight("replied") == 1.0
        assert adaptation_weight("relabeled") == 1.0
        assert adaptation_weight("deleted") == 1.0

    def test_adaptation_weight_medium(self):
        assert adaptation_weight("archived") == 0.5

    def test_adaptation_weight_low(self):
        assert adaptation_weight("opened") == 0.0
        assert adaptation_weight("ignored") == 0.0
        assert adaptation_weight("unknown") == 0.0  # default case


class TestOutcomeCollector:
    @pytest.mark.asyncio
    async def test_record_outcome_basic(self):
        config = TriageConfig()
        state_store = AsyncMock()
        collector = OutcomeCollector(config, state_store)

        await collector.record_outcome(
            message_id="msg-1",
            outcome="replied",
            sender_domain="example.com",
            tier=1,
            score=90,
        )

        # Check that outcome was recorded
        assert len(collector.get_all_outcomes()) == 1
        record = collector.get_all_outcomes()[0]
        assert record["message_id"] == "msg-1"
        assert record["outcome"] == "replied"
        assert record["confidence"] == CONFIDENCE_HIGH
        assert record["sender_domain"] == "example.com"
        assert record["tier"] == 1
        assert record["score"] == 90
        assert record["feeds_adaptation"] is True
        assert record["adaptation_weight"] == 1.0

        # Check that state store was updated
        state_store.update_sender_reputation.assert_awaited_once_with(
            "example.com", 1, 90
        )

    @pytest.mark.asyncio
    async def test_record_outcome_with_metadata(self):
        config = TriageConfig()
        state_store = AsyncMock()
        collector = OutcomeCollector(config, state_store)

        await collector.record_outcome(
            message_id="msg-2",
            outcome="archived",
            sender_domain="test.com",
            tier=3,
            score=40,
            metadata={"extra": "data"},
        )

        record = collector.get_all_outcomes()[0]
        assert record["metadata"] == {"extra": "data"}

    @pytest.mark.asyncio
    async def test_record_outcome_error_handling(self):
        config = TriageConfig()
        state_store = MagicMock()  # Simulate error in update_sender_reputation
        state_store.update_sender_reputation = AsyncMock(side_effect=Exception("DB Error"))
        collector = OutcomeCollector(config, state_store)

        await collector.record_outcome(
            message_id="msg-3",
            outcome="deleted",
            sender_domain="error.com",
            tier=2,
            score=60,
        )

        # Should still record the outcome despite error
        assert len(collector.get_all_outcomes()) == 1

    def test_get_adaptation_outcomes(self):
        config = TriageConfig()
        collector = OutcomeCollector(config)

        # Add some outcomes
        collector._recorded_outcomes = [
            {"outcome": "replied", "feeds_adaptation": True},
            {"outcome": "archived", "feeds_adaptation": True},
            {"outcome": "opened", "feeds_adaptation": False},
        ]

        adaptation_outcomes = collector.get_adaptation_outcomes()
        assert len(adaptation_outcomes) == 2
        assert adaptation_outcomes[0]["outcome"] == "replied"
        assert adaptation_outcomes[1]["outcome"] == "archived"

    def test_get_all_outcomes(self):
        config = TriageConfig()
        collector = OutcomeCollector(config)

        # Add some outcomes
        collector._recorded_outcomes = [
            {"outcome": "replied", "feeds_adaptation": True},
            {"outcome": "ignored", "feeds_adaptation": False},
        ]

        all_outcomes = collector.get_all_outcomes()
        assert len(all_outcomes) == 2

    def test_clear(self):
        config = TriageConfig()
        collector = OutcomeCollector(config)

        # Add some outcomes
        collector._recorded_outcomes = [
            {"outcome": "replied", "feeds_adaptation": True},
        ]

        assert len(collector.get_all_outcomes()) == 1
        collector.clear()
        assert len(collector.get_all_outcomes()) == 0

    @pytest.mark.asyncio
    async def test_check_outcomes_for_cycle_empty(self):
        config = TriageConfig()
        workspace_agent = AsyncMock()
        prior_triaged = {}

        collector = OutcomeCollector(config)
        result = await collector.check_outcomes_for_cycle(
            workspace_agent, prior_triaged
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_check_outcomes_for_cycle_no_workspace(self):
        config = TriageConfig()
        workspace_agent = None
        prior_triaged = {"msg-1": MagicMock()}

        collector = OutcomeCollector(config)
        result = await collector.check_outcomes_for_cycle(
            workspace_agent, prior_triaged
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_check_outcomes_for_cycle_no_get_message_labels(self):
        config = TriageConfig()
        workspace_agent = MagicMock()  # No get_message_labels method
        prior_triaged = {"msg-1": MagicMock()}

        collector = OutcomeCollector(config)
        result = await collector.check_outcomes_for_cycle(
            workspace_agent, prior_triaged
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_check_outcomes_for_cycle_timeout(self):
        config = TriageConfig()
        workspace_agent = AsyncMock()
        workspace_agent.get_message_labels = AsyncMock(side_effect=asyncio.TimeoutError)
        prior_triaged = {"msg-1": MagicMock()}

        collector = OutcomeCollector(config)
        # This should not raise an exception, but return empty list due to timeout
        result = await collector.check_outcomes_for_cycle(
            workspace_agent, prior_triaged, deadline=time.monotonic() + 0.1
        )
        assert result == []