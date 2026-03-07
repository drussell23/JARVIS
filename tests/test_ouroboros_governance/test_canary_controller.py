# tests/test_ouroboros_governance/test_canary_controller.py
"""Tests for the canary controller with domain slice promotion."""

import time
import pytest

from backend.core.ouroboros.governance.canary_controller import (
    CanaryController,
    DomainSlice,
    SliceMetrics,
    PromotionResult,
    CanaryState,
)


@pytest.fixture
def controller():
    return CanaryController()


class TestDomainSlice:
    def test_slice_fields(self):
        """DomainSlice has path prefix and state."""
        s = DomainSlice(path_prefix="backend/core/ouroboros/")
        assert s.path_prefix == "backend/core/ouroboros/"
        assert s.state == CanaryState.PENDING

    def test_file_matches_prefix(self):
        """matches() returns True for files under the prefix."""
        s = DomainSlice(path_prefix="backend/core/ouroboros/")
        assert s.matches("backend/core/ouroboros/engine.py") is True
        assert s.matches("backend/core/prime_router.py") is False


class TestSliceMetrics:
    def test_initial_metrics(self):
        """Fresh metrics have zero counts."""
        m = SliceMetrics()
        assert m.total_operations == 0
        assert m.successful_operations == 0
        assert m.rollback_count == 0
        assert m.rollback_rate == 0.0

    def test_rollback_rate_calculation(self):
        """Rollback rate is rollbacks / total."""
        m = SliceMetrics()
        m.total_operations = 100
        m.rollback_count = 3
        assert m.rollback_rate == 0.03

    def test_rollback_rate_zero_ops(self):
        """Rollback rate is 0 with zero operations."""
        m = SliceMetrics()
        assert m.rollback_rate == 0.0


class TestCanaryController:
    def test_register_slice(self, controller):
        """Slices can be registered."""
        controller.register_slice("backend/core/ouroboros/")
        assert len(controller.slices) == 1

    def test_record_operation_success(self, controller):
        """Successful operation increments counters."""
        controller.register_slice("backend/core/ouroboros/")
        controller.record_operation(
            file_path="backend/core/ouroboros/engine.py",
            success=True,
            latency_s=5.0,
        )
        metrics = controller.get_metrics("backend/core/ouroboros/")
        assert metrics.total_operations == 1
        assert metrics.successful_operations == 1

    def test_record_operation_rollback(self, controller):
        """Rollback increments rollback counter."""
        controller.register_slice("backend/core/ouroboros/")
        controller.record_operation(
            file_path="backend/core/ouroboros/engine.py",
            success=False,
            latency_s=5.0,
            rolled_back=True,
        )
        metrics = controller.get_metrics("backend/core/ouroboros/")
        assert metrics.rollback_count == 1

    def test_unmatched_file_ignored(self, controller):
        """Operations on unregistered paths are ignored."""
        controller.register_slice("backend/core/ouroboros/")
        controller.record_operation(
            file_path="backend/core/prime_router.py",
            success=True,
            latency_s=5.0,
        )
        metrics = controller.get_metrics("backend/core/ouroboros/")
        assert metrics.total_operations == 0


class TestPromotionCriteria:
    def test_insufficient_operations(self, controller):
        """< 50 operations fails promotion."""
        controller.register_slice("backend/core/ouroboros/")
        for _ in range(30):
            controller.record_operation(
                "backend/core/ouroboros/foo.py", True, 5.0
            )
        result = controller.check_promotion("backend/core/ouroboros/")
        assert result.promoted is False
        assert "50 operations" in result.reason

    def test_high_rollback_rate_fails(self, controller):
        """Rollback rate >= 5% fails promotion."""
        controller.register_slice("backend/core/ouroboros/")
        for _ in range(50):
            controller.record_operation(
                "backend/core/ouroboros/foo.py", True, 5.0
            )
        for _ in range(5):
            controller.record_operation(
                "backend/core/ouroboros/foo.py", False, 5.0, rolled_back=True
            )
        result = controller.check_promotion("backend/core/ouroboros/")
        assert result.promoted is False
        assert "rollback" in result.reason.lower()

    def test_high_latency_fails(self, controller):
        """p95 latency > 120s fails promotion."""
        controller.register_slice("backend/core/ouroboros/")
        for _ in range(50):
            controller.record_operation(
                "backend/core/ouroboros/foo.py", True, 130.0
            )
        result = controller.check_promotion("backend/core/ouroboros/")
        assert result.promoted is False
        assert "latency" in result.reason.lower()

    def test_stability_window_not_met(self, controller):
        """< 72h since first operation fails promotion."""
        controller.register_slice("backend/core/ouroboros/")
        for _ in range(55):
            controller.record_operation(
                "backend/core/ouroboros/foo.py", True, 5.0
            )
        result = controller.check_promotion("backend/core/ouroboros/")
        assert result.promoted is False
        assert "72" in result.reason or "stability" in result.reason.lower()

    def test_all_criteria_met(self, controller):
        """All criteria met -> promotion passes."""
        controller.register_slice("backend/core/ouroboros/")
        metrics = controller.get_metrics("backend/core/ouroboros/")
        # Simulate 55 successful ops with low latency and 72h+ stability
        metrics.total_operations = 55
        metrics.successful_operations = 55
        metrics.rollback_count = 1
        metrics.latencies = [5.0] * 55
        metrics.first_operation_time = time.time() - (73 * 3600)  # 73 hours ago
        result = controller.check_promotion("backend/core/ouroboros/")
        assert result.promoted is True

    def test_promote_changes_state(self, controller):
        """Successful promotion changes slice state to ACTIVE."""
        controller.register_slice("backend/core/ouroboros/")
        metrics = controller.get_metrics("backend/core/ouroboros/")
        metrics.total_operations = 55
        metrics.successful_operations = 55
        metrics.rollback_count = 1
        metrics.latencies = [5.0] * 55
        metrics.first_operation_time = time.time() - (73 * 3600)
        controller.check_promotion("backend/core/ouroboros/")
        s = controller.get_slice("backend/core/ouroboros/")
        assert s.state == CanaryState.ACTIVE

    def test_is_file_allowed(self, controller):
        """is_file_allowed() returns True for promoted slices."""
        controller.register_slice("backend/core/ouroboros/")
        assert controller.is_file_allowed("backend/core/ouroboros/foo.py") is False
        # Promote the slice
        metrics = controller.get_metrics("backend/core/ouroboros/")
        metrics.total_operations = 55
        metrics.successful_operations = 55
        metrics.rollback_count = 0
        metrics.latencies = [5.0] * 55
        metrics.first_operation_time = time.time() - (73 * 3600)
        controller.check_promotion("backend/core/ouroboros/")
        assert controller.is_file_allowed("backend/core/ouroboros/foo.py") is True

    def test_unregistered_file_not_allowed(self, controller):
        """Files not in any slice are not allowed."""
        assert controller.is_file_allowed("random/file.py") is False
