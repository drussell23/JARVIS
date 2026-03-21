"""Tests for ConsciousnessBridge — Consciousness ↔ Governance integration."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass

import pytest

from backend.core.ouroboros.governance.consciousness_bridge import ConsciousnessBridge


@dataclass
class FakeProphecyReport:
    change_id: str = "test"
    risk_level: str = "low"
    confidence: float = 0.5
    reasoning: str = "test reasoning"
    predicted_failures: tuple = ()
    recommended_tests: tuple = ()


@dataclass
class FakeMemoryInsight:
    file_path: str = "test.py"
    summary: str = "historically fragile"
    recommendation: str = "add more tests"


@dataclass
class FakeReputation:
    fragility_score: float = 0.8
    success_rate: float = 0.3


class TestConsciousnessBridgeInactive:
    def test_is_active_when_none(self):
        bridge = ConsciousnessBridge(consciousness=None)
        assert bridge.is_active is False

    @pytest.mark.asyncio
    async def test_assess_risk_returns_none(self):
        bridge = ConsciousnessBridge(consciousness=None)
        result = await bridge.assess_regression_risk(["test.py"])
        assert result is None

    def test_get_fragile_context_returns_empty(self):
        bridge = ConsciousnessBridge(consciousness=None)
        assert bridge.get_fragile_file_context(("test.py",)) == ""

    def test_health_check_defaults_healthy(self):
        bridge = ConsciousnessBridge(consciousness=None)
        healthy, reason = bridge.is_system_healthy_for_exploration()
        assert healthy is True
        assert "unavailable" in reason

    @pytest.mark.asyncio
    async def test_record_outcome_noop(self):
        bridge = ConsciousnessBridge(consciousness=None)
        await bridge.record_operation_outcome("op-1", ["test.py"], True)


class TestConsciousnessBridgeActive:
    def _make_consciousness(self, risk_level="low", fragile=False):
        consciousness = MagicMock()
        consciousness.detect_regression = AsyncMock(return_value=FakeProphecyReport(
            risk_level=risk_level,
        ))
        consciousness.get_memory_for_planner = MagicMock(
            return_value=[FakeMemoryInsight()] if fragile else []
        )
        consciousness.health = MagicMock(return_value={
            "running": True,
            "cortex": {"status": "ok"},
            "memory": {"status": "ok"},
        })
        consciousness._memory = MagicMock()
        consciousness._memory.ingest_outcome = AsyncMock()
        return consciousness

    def test_is_active(self):
        bridge = ConsciousnessBridge(self._make_consciousness())
        assert bridge.is_active is True

    @pytest.mark.asyncio
    async def test_assess_risk_low(self):
        bridge = ConsciousnessBridge(self._make_consciousness(risk_level="low"))
        result = await bridge.assess_regression_risk(["test.py"])
        assert result is not None
        assert result["risk_level"] == "low"

    @pytest.mark.asyncio
    async def test_assess_risk_high(self):
        bridge = ConsciousnessBridge(self._make_consciousness(risk_level="high"))
        result = await bridge.assess_regression_risk(["test.py"])
        assert result["risk_level"] == "high"

    def test_fragile_file_context(self):
        bridge = ConsciousnessBridge(self._make_consciousness(fragile=True))
        context = bridge.get_fragile_file_context(("test.py",))
        assert "Fragile File" in context
        assert "historically fragile" in context

    def test_no_fragile_files(self):
        bridge = ConsciousnessBridge(self._make_consciousness(fragile=False))
        assert bridge.get_fragile_file_context(("test.py",)) == ""

    def test_health_check_healthy(self):
        bridge = ConsciousnessBridge(self._make_consciousness())
        healthy, reason = bridge.is_system_healthy_for_exploration()
        assert healthy is True

    def test_health_check_cortex_failed(self):
        consciousness = self._make_consciousness()
        consciousness.health = MagicMock(return_value={
            "running": True,
            "cortex": {"status": "failed"},
            "memory": {"status": "ok"},
        })
        bridge = ConsciousnessBridge(consciousness)
        healthy, reason = bridge.is_system_healthy_for_exploration()
        assert healthy is False
        assert "cortex" in reason

    def test_health_check_not_running(self):
        consciousness = self._make_consciousness()
        consciousness.health = MagicMock(return_value={"running": False})
        bridge = ConsciousnessBridge(consciousness)
        healthy, reason = bridge.is_system_healthy_for_exploration()
        assert healthy is False

    @pytest.mark.asyncio
    async def test_record_outcome_calls_memory(self):
        consciousness = self._make_consciousness()
        bridge = ConsciousnessBridge(consciousness)
        await bridge.record_operation_outcome("op-1", ["test.py"], True)
        consciousness._memory.ingest_outcome.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_in_assess_returns_none(self):
        consciousness = self._make_consciousness()
        consciousness.detect_regression = AsyncMock(side_effect=RuntimeError("boom"))
        bridge = ConsciousnessBridge(consciousness)
        result = await bridge.assess_regression_risk(["test.py"])
        assert result is None
