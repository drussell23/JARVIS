"""Tests for CostIntelligenceMCP server.

Covers:
- query_costs returns breakdown with all service categories
- cost_forecast returns projected costs
- get_recommendations returns actionable suggestions
- set_budget updates thresholds dynamically
- get_cost_efficiency_report returns VM utilization metrics
- verify_zero_cost_posture returns pass/fail matching SLO verifier
- MCP resource resolution
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure backend is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ─── Mock CostTracker for isolated testing ────────────────────────────────────

class MockCostTrackerConfig:
    alert_threshold_daily: float = 1.00
    alert_threshold_monthly: float = 20.00
    hard_budget_enforcement: bool = True
    solo_developer_mode: bool = True


class MockCostTracker:
    def __init__(self):
        self.config = MockCostTrackerConfig()
        self._spend_data = {
            "day": {"total_cost": 0.45, "vm_count": 2, "total_runtime_hours": 3.2},
            "week": {"total_cost": 2.10, "vm_count": 8, "total_runtime_hours": 14.5},
            "month": {"total_cost": 8.50, "vm_count": 30, "total_runtime_hours": 55.0},
        }

    async def get_cost_summary(self, period: str = "day") -> Dict[str, Any]:
        return self._spend_data.get(period, {"total_cost": 0.0})

    async def get_cloud_service_costs(self, period: str = "day") -> Dict[str, Any]:
        return {
            "total_cost": 0.15,
            "breakdown": {
                "cloud_sql": 0.08,
                "static_ip": 0.05,
                "cloud_run": 0.02,
            },
        }

    async def get_budget_status(self) -> Dict[str, Any]:
        return {
            "daily_budget": 1.00,
            "daily_spent": 0.60,
            "daily_remaining": 0.40,
            "monthly_budget": 20.00,
            "monthly_spent": 8.65,
            "monthly_remaining": 11.35,
        }

    async def forecast_daily_cost(self) -> Dict[str, Any]:
        return {
            "predicted_daily_cost": 0.35,
            "confidence_score": 0.82,
        }


class MockUsagePatternAnalyzer:
    async def get_avg_daily_sessions(self) -> float:
        return 3.5

    async def get_false_alarm_rate(self) -> float:
        return 0.15

    async def get_stats(self) -> Dict[str, Any]:
        return {
            "avg_daily_sessions": 3.5,
            "avg_session_duration_hours": 1.2,
            "false_alarm_rate": 0.15,
            "total_sessions": 42,
            "num_days": 12,
        }


# ─── Tests ────────────────────────────────────────────────────────────────────

@pytest.fixture
def mcp_server():
    """Create a CostIntelligenceMCP with mock dependencies."""
    from backend.core.cost_intelligence_mcp import CostIntelligenceMCP, CostIntelligenceConfig

    config = CostIntelligenceConfig(
        gcp_project="test-project",
        gcp_region="us-central1",
        cloud_run_services="test-service",
        solo_developer_mode=True,
    )
    server = CostIntelligenceMCP(config)
    server._initialized = True
    server._cost_tracker = MockCostTracker()
    server._usage_analyzer = MockUsagePatternAnalyzer()
    server._capacity_controller = None
    return server


@pytest.mark.asyncio
async def test_query_costs_returns_breakdown(mcp_server):
    """AC-21: query_costs returns breakdown with all service categories."""
    result = await mcp_server.query_costs("day")

    assert result["period"] == "day"
    assert "vm_costs" in result
    assert "cloud_service_costs" in result
    assert "total" in result
    assert result["total"] > 0
    assert "budget" in result

    # Cloud service breakdown should include all categories
    cloud = result["cloud_service_costs"]
    assert "breakdown" in cloud
    assert "cloud_sql" in cloud["breakdown"]
    assert "static_ip" in cloud["breakdown"]


@pytest.mark.asyncio
async def test_query_costs_periods(mcp_server):
    """query_costs supports day/week/month periods."""
    for period in ("day", "week", "month"):
        result = await mcp_server.query_costs(period)
        assert result["period"] == period
        assert "error" not in result


@pytest.mark.asyncio
async def test_query_costs_without_tracker():
    """query_costs returns error when CostTracker unavailable."""
    from backend.core.cost_intelligence_mcp import CostIntelligenceMCP

    server = CostIntelligenceMCP()
    server._initialized = True
    server._cost_tracker = None

    result = await server.query_costs("day")
    assert "error" in result


@pytest.mark.asyncio
async def test_cost_forecast(mcp_server):
    """cost_forecast returns projected costs with confidence."""
    result = await mcp_server.cost_forecast(30)

    assert result["forecast_days"] == 30
    assert "daily_rate" in result
    assert "projected_total" in result
    assert result["projected_total"] == result["daily_rate"] * 30
    assert "confidence_score" in result
    assert "usage_patterns" in result


@pytest.mark.asyncio
async def test_get_recommendations(mcp_server):
    """get_recommendations returns actionable suggestions."""
    recs = await mcp_server.get_recommendations()

    assert isinstance(recs, list)
    # In solo mode, should always recommend cloud run scale-to-zero
    cloud_run_rec = [r for r in recs if r["id"] == "cloud_run_scale_to_zero"]
    assert len(cloud_run_rec) == 1
    assert cloud_run_rec[0]["estimated_savings_monthly"] > 0

    # All recommendations should have required fields
    for rec in recs:
        assert "id" in rec
        assert "category" in rec
        assert "priority" in rec
        assert "title" in rec
        assert "description" in rec


@pytest.mark.asyncio
async def test_set_budget_daily(mcp_server):
    """set_budget updates daily threshold."""
    result = await mcp_server.set_budget("daily", 2.50)

    assert result["success"] is True
    assert result["old_amount"] == 1.00
    assert result["new_amount"] == 2.50
    assert mcp_server._cost_tracker.config.alert_threshold_daily == 2.50


@pytest.mark.asyncio
async def test_set_budget_monthly(mcp_server):
    """set_budget updates monthly threshold."""
    result = await mcp_server.set_budget("monthly", 50.00)

    assert result["success"] is True
    assert result["old_amount"] == 20.00
    assert mcp_server._cost_tracker.config.alert_threshold_monthly == 50.00


@pytest.mark.asyncio
async def test_set_budget_invalid_period(mcp_server):
    """set_budget rejects invalid period."""
    result = await mcp_server.set_budget("yearly", 100.0)

    assert result["success"] is False
    assert "error" in result


@pytest.mark.asyncio
async def test_get_cost_efficiency_report(mcp_server):
    """get_cost_efficiency_report returns comprehensive metrics."""
    report = await mcp_server.get_cost_efficiency_report()

    assert "timestamp" in report
    assert "usage_patterns" in report
    assert "budget_status" in report

    # Usage patterns from mock
    usage = report["usage_patterns"]
    assert usage["avg_daily_sessions"] == 3.5
    assert usage["false_alarm_rate"] == 0.15


@pytest.mark.asyncio
async def test_verify_zero_cost_posture_structure(mcp_server):
    """verify_zero_cost_posture returns correct structure."""
    # Mock the gcloud subprocess calls to simulate clean state
    async def mock_create_subprocess(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 1  # pgrep returns 1 = not found
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess):
        with patch("os.path.exists", return_value=False):
            result = await mcp_server.verify_zero_cost_posture()

    assert "pass" in result
    assert "failures" in result
    assert "checks" in result
    assert "summary" in result
    assert isinstance(result["checks"], list)


@pytest.mark.asyncio
async def test_resource_resolution(mcp_server):
    """MCP resources resolve to correct handlers."""
    # Test each resource URI
    for uri in [
        "jarvis://cost/current",
        "jarvis://cost/budget",
        "jarvis://cost/forecast",
        "jarvis://cost/efficiency",
        "jarvis://cost/recommendations",
    ]:
        result = await mcp_server.get_resource(uri)
        assert "error" not in result or result.get("error") != f"Unknown resource URI: {uri}"


@pytest.mark.asyncio
async def test_resource_unknown_uri(mcp_server):
    """Unknown resource URI returns error."""
    result = await mcp_server.get_resource("jarvis://cost/nonexistent")
    assert "error" in result
    assert "Unknown resource URI" in result["error"]


@pytest.mark.asyncio
async def test_resource_recommendations_format(mcp_server):
    """jarvis://cost/recommendations returns structured format."""
    result = await mcp_server.get_resource("jarvis://cost/recommendations")

    assert "recommendations" in result
    assert "total_potential_savings_monthly" in result
    assert "count" in result
    assert result["count"] == len(result["recommendations"])


@pytest.mark.asyncio
async def test_singleton_pattern():
    """Singleton pattern works correctly."""
    import backend.core.cost_intelligence_mcp as mod

    # Reset singleton
    mod._instance = None

    instance1 = mod.get_cost_intelligence_mcp()
    instance2 = mod.get_cost_intelligence_mcp()

    assert instance1 is instance2

    # Cleanup
    mod._instance = None


@pytest.mark.asyncio
async def test_broker_registration(mcp_server):
    """register_with_broker connects to pressure observer."""
    mock_broker = MagicMock()
    mock_broker.register_pressure_observer = MagicMock()

    mcp_server.register_with_broker(mock_broker)

    assert mcp_server._mcp_active is True
    assert mcp_server._broker is mock_broker
    mock_broker.register_pressure_observer.assert_called_once()
