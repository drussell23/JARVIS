# [Ouroboros] Modified by Ouroboros (op=op-01a12168-) at 2026-10-09 16:06 UTC
# Reason: `backend/autonomy/email_triage/weight_adapter.py` has no corresponding test module. CREATE `tests/test_weight_adapter.py

import os
import sys
from unittest.mock import MagicMock, patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
import pytest
from autonomy.email_triage.weight_adapter import WeightAdapter
from autonomy.email_triage.config import TriageConfig

class TestWeightAdapter:
    """Test the WeightAdapter class."""

    def test_import_smoke(self):
        """Ensure the module can be imported without errors."""
        # This test ensures no syntax or import errors in the module
        assert WeightAdapter is not None

    @pytest.mark.asyncio
    async def test_get_sender_reputation_bonus_no_state_store(self):
        """Test sender reputation bonus with no state store."""
        adapter = WeightAdapter(TriageConfig())
        result = await adapter.get_sender_reputation_bonus("test.com", None)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_get_sender_reputation_bonus_exception(self):
        """Test sender reputation bonus with exception in state store."""
        adapter = WeightAdapter(TriageConfig())
        mock_store = MagicMock()
        mock_store.get_sender_reputation.side_effect = Exception("Test error")
        result = await adapter.get_sender_reputation_bonus("test.com", mock_store)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_get_sender_reputation_bonus_insufficient_data(self):
        """Test sender reputation bonus with insufficient data."""
        adapter = WeightAdapter(TriageConfig())
        mock_store = MagicMock()
        mock_store.get_sender_reputation.return_value = {"total_count": 1, "avg_score": 50}
        result = await adapter.get_sender_reputation_bonus("test.com", mock_store)
        assert result == 0.0



    @pytest.mark.asyncio
    async def test_compute_adapted_weights_not_enough_data(self):
        """Test compute adapted weights with not enough data."""
        adapter = WeightAdapter(TriageConfig())
        # Add fewer than min_outcomes_for_adaptation outcomes
        for i in range(49):
            adapter.record_outcome({"feeds_adaptation": True, "adaptation_weight": 1.0, "outcome": "replied", "tier": 1})
        result = await adapter.compute_adapted_weights()
        assert result is None

    @pytest.mark.asyncio
    async def test_compute_adapted_weights_no_change(self):
        """Test compute adapted weights with no meaningful change."""
        config = TriageConfig()
        config.min_outcomes_for_adaptation = 10
        adapter = WeightAdapter(config)
        # Add outcomes that shouldn't change weights
        for i in range(10):
            adapter.record_outcome({"feeds_adaptation": True, "adaptation_weight": 1.0, "outcome": "replied", "tier": 3})
        result = await adapter.compute_adapted_weights()
        assert result is None

    def test_is_shadow_active_false(self):
        """Test is_shadow_active when not in shadow mode."""
        adapter = WeightAdapter(TriageConfig())
        assert adapter.is_shadow_active is False

    def test_is_shadow_active_true(self):
        """Test is_shadow_active when in shadow mode."""
        adapter = WeightAdapter(TriageConfig())
        adapter._shadow_weights = {"sender": 1.0, "urgency": 1.0, "content": 1.0}
        adapter._shadow_cycles_remaining = 3
        assert adapter.is_shadow_active is True

    def test_active_weights_none(self):
        """Test active_weights when no weights are active."""
        adapter = WeightAdapter(TriageConfig())
        assert adapter.active_weights is None

    def test_active_weights_set(self):
        """Test active_weights when weights are set."""
        adapter = WeightAdapter(TriageConfig())
        adapter._active_weights = {"sender": 1.0, "urgency": 1.0, "content": 1.0}
        assert adapter.active_weights == {"sender": 1.0, "urgency": 1.0, "content": 1.0}

    def test_record_outcome(self):
        """Test record outcome functionality."""
        adapter = WeightAdapter(TriageConfig())
        outcome = {"feeds_adaptation": True, "adaptation_weight": 1.0, "outcome": "replied", "tier": 1}
        adapter.record_outcome(outcome)
        assert len(adapter._outcomes) == 1
        assert adapter._outcomes[0] == outcome

    def test_get_weights_for_scoring_none(self):
        """Test get_weights_for_scoring returns None when no active weights."""
        adapter = WeightAdapter(TriageConfig())
        result = adapter.get_weights_for_scoring()
        assert result is None

    def test_get_weights_for_scoring_active_weights(self):
        """Test get_weights_for_scoring returns active weights."""
        adapter = WeightAdapter(TriageConfig())
        adapter._active_weights = {"sender": 1.0, "urgency": 1.0, "content": 1.0}
        result = adapter.get_weights_for_scoring()
        assert result == {"sender": 1.0, "urgency": 1.0, "content": 1.0}

    def test_record_shadow_comparison(self):
        """Test record shadow comparison."""
        adapter = WeightAdapter(TriageConfig())
        adapter.record_shadow_comparison(1, 2, "replied")
        assert len(adapter._shadow_log) == 1
        assert adapter._total_shadow_entries == 1
        assert adapter._total_disagreements == 1

    def test_rollback_to_defaults(self):
        """Test rollback to defaults."""
        adapter = WeightAdapter(TriageConfig())
        adapter._active_weights = {"sender": 1.0, "urgency": 1.0, "content": 1.0}
        adapter._shadow_weights = {"sender": 1.0, "urgency": 1.0, "content": 1.0}
        adapter._shadow_log.append((1, 2, "replied"))
        adapter.rollback_to_defaults()
        assert adapter._active_weights is None
        assert adapter._shadow_weights is None
        assert len(adapter._shadow_log) == 0
