# tests/test_ouroboros_governance/test_routing_policy.py
"""Tests for the deterministic routing policy."""

import pytest

from backend.core.ouroboros.governance.routing_policy import (
    RoutingPolicy,
    RoutingDecision,
    TaskCategory,
    CostGuardrail,
)
from backend.core.ouroboros.governance.resource_monitor import (
    ResourceSnapshot,
    PressureLevel,
)
from backend.core.ouroboros.governance.degradation import DegradationMode


@pytest.fixture
def policy():
    return RoutingPolicy()


def _normal_snap():
    return ResourceSnapshot(ram_percent=50.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)

def _elevated_snap():
    return ResourceSnapshot(ram_percent=82.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)

def _critical_snap():
    return ResourceSnapshot(ram_percent=87.0, cpu_percent=85.0, event_loop_latency_ms=5.0, disk_io_busy=False)


class TestTaskCategories:
    def test_all_categories_defined(self):
        """Six task categories exist."""
        expected = [
            "SINGLE_FILE_FIX", "MULTI_FILE_ANALYSIS", "CROSS_REPO_PLANNING",
            "CANDIDATE_GENERATION", "TEST_EXECUTION", "BLAST_RADIUS_CALC",
        ]
        assert [c.name for c in TaskCategory] == expected


class TestRoutingDecisions:
    def test_all_decisions_defined(self):
        """Three routing decisions: LOCAL, GCP_PRIME, QUEUE."""
        assert len(RoutingDecision) == 3


class TestNormalConditions:
    def test_single_file_routes_local(self, policy):
        """Single-file fix always routes LOCAL."""
        decision = policy.route(
            TaskCategory.SINGLE_FILE_FIX,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.LOCAL

    def test_multi_file_analysis_routes_local_normally(self, policy):
        """Multi-file analysis routes LOCAL under normal conditions."""
        decision = policy.route(
            TaskCategory.MULTI_FILE_ANALYSIS,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.LOCAL

    def test_cross_repo_routes_gcp(self, policy):
        """Cross-repo planning routes to GCP_PRIME."""
        decision = policy.route(
            TaskCategory.CROSS_REPO_PLANNING,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.GCP_PRIME

    def test_candidate_gen_routes_gcp(self, policy):
        """Candidate generation routes to GCP_PRIME."""
        decision = policy.route(
            TaskCategory.CANDIDATE_GENERATION,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.GCP_PRIME

    def test_test_execution_routes_local(self, policy):
        """Test execution always routes LOCAL."""
        decision = policy.route(
            TaskCategory.TEST_EXECUTION,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.LOCAL

    def test_blast_radius_routes_local(self, policy):
        """Blast radius calculation always routes LOCAL."""
        decision = policy.route(
            TaskCategory.BLAST_RADIUS_CALC,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.LOCAL


class TestPressureRouting:
    def test_elevated_heavy_task_routes_gcp(self, policy):
        """Under elevated pressure, heavy tasks route to GCP."""
        decision = policy.route(
            TaskCategory.MULTI_FILE_ANALYSIS,
            _elevated_snap(),
            DegradationMode.REDUCED_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.GCP_PRIME

    def test_single_file_stays_local_under_pressure(self, policy):
        """Single-file fix stays LOCAL even under elevated pressure."""
        decision = policy.route(
            TaskCategory.SINGLE_FILE_FIX,
            _elevated_snap(),
            DegradationMode.REDUCED_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.LOCAL


class TestGCPDown:
    def test_gcp_down_queues_heavy_tasks(self, policy):
        """GCP unavailable queues heavy tasks."""
        decision = policy.route(
            TaskCategory.CROSS_REPO_PLANNING,
            _normal_snap(),
            DegradationMode.REDUCED_AUTONOMY,
            gcp_available=False,
        )
        assert decision == RoutingDecision.QUEUE

    def test_gcp_down_local_tasks_continue(self, policy):
        """GCP unavailable doesn't affect local-only tasks."""
        decision = policy.route(
            TaskCategory.SINGLE_FILE_FIX,
            _normal_snap(),
            DegradationMode.REDUCED_AUTONOMY,
            gcp_available=False,
        )
        assert decision == RoutingDecision.LOCAL


class TestCostGuardrail:
    def test_budget_tracking(self, policy):
        """Cost guardrail tracks GCP usage."""
        guardrail = policy.cost_guardrail
        guardrail.record_gcp_usage(0.50)
        guardrail.record_gcp_usage(0.25)
        assert guardrail.daily_usage == 0.75

    def test_over_budget_queues_gcp(self, policy):
        """Over daily budget queues GCP-bound tasks."""
        policy.cost_guardrail.record_gcp_usage(100.0)  # Over any budget
        decision = policy.route(
            TaskCategory.CANDIDATE_GENERATION,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.QUEUE


class TestDeterminism:
    def test_same_inputs_same_output_1000x(self, policy):
        """Same inputs always produce same routing decision."""
        snap = _normal_snap()
        first = policy.route(TaskCategory.CROSS_REPO_PLANNING, snap, DegradationMode.FULL_AUTONOMY, True)
        for _ in range(1000):
            result = policy.route(TaskCategory.CROSS_REPO_PLANNING, snap, DegradationMode.FULL_AUTONOMY, True)
            assert result == first
