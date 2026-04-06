"""Tests for the Ouroboros Battle Test CostTracker."""

from __future__ import annotations

import asyncio
import json
import pytest
from pathlib import Path

from backend.core.ouroboros.battle_test.cost_tracker import CostTracker


class TestInitialState:
    """test_initial_state: budget=0.50, total=0, not exhausted, remaining=0.50"""

    def test_initial_state(self):
        tracker = CostTracker(budget_usd=0.50)
        assert tracker.total_spent == 0.0
        assert tracker.remaining == pytest.approx(0.50)
        assert tracker.exhausted is False
        assert not tracker.budget_event.is_set()


class TestRecordCost:
    """test_record_cost: record 0.10, verify total=0.10, remaining=0.40"""

    def test_record_cost(self):
        tracker = CostTracker(budget_usd=0.50)
        tracker.record("anthropic", 0.10)
        assert tracker.total_spent == pytest.approx(0.10)
        assert tracker.remaining == pytest.approx(0.40)


class TestBudgetExhaustedFiresEvent:
    """test_budget_exhausted_fires_event: budget=0.05, record 0.03+0.03, verify exhausted and event set"""

    def test_budget_exhausted_fires_event(self):
        tracker = CostTracker(budget_usd=0.05)
        tracker.record("anthropic", 0.03)
        assert not tracker.exhausted
        assert not tracker.budget_event.is_set()
        tracker.record("anthropic", 0.03)
        assert tracker.exhausted
        assert tracker.budget_event.is_set()


class TestBreakdownByProvider:
    """test_breakdown_by_provider: record to 3 providers, verify breakdown dict"""

    def test_breakdown_by_provider(self):
        tracker = CostTracker(budget_usd=1.00)
        tracker.record("anthropic", 0.10)
        tracker.record("openai", 0.20)
        tracker.record("google", 0.05)
        tracker.record("anthropic", 0.15)

        bd = tracker.breakdown
        assert bd["anthropic"] == pytest.approx(0.25)
        assert bd["openai"] == pytest.approx(0.20)
        assert bd["google"] == pytest.approx(0.05)


class TestPersistenceRoundtrip:
    """test_persistence_roundtrip: save then reload from same path, verify state preserved"""

    def test_persistence_roundtrip(self, tmp_path):
        persist_path = tmp_path / "cost_tracker.json"
        tracker = CostTracker(budget_usd=1.00, persist_path=persist_path)
        tracker.record("anthropic", 0.30)
        tracker.record("openai", 0.20)
        tracker.save()

        # Reload from same path
        tracker2 = CostTracker(budget_usd=1.00, persist_path=persist_path)
        assert tracker2.total_spent == pytest.approx(0.50)
        assert tracker2.remaining == pytest.approx(0.50)
        assert tracker2.breakdown["anthropic"] == pytest.approx(0.30)
        assert tracker2.breakdown["openai"] == pytest.approx(0.20)


class TestZeroCostIgnored:
    """test_zero_cost_ignored: record 0.0 and -1.0, verify total stays 0"""

    def test_zero_cost_ignored(self):
        tracker = CostTracker(budget_usd=0.50)
        tracker.record("anthropic", 0.0)
        tracker.record("anthropic", -1.0)
        assert tracker.total_spent == 0.0
        assert not tracker.budget_event.is_set()
