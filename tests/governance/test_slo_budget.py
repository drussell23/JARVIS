"""tests/governance/test_slo_budget.py — P3-1 SLO-backed health model tests."""
from __future__ import annotations

import time
import pytest

from backend.core.slo_budget import (
    SLOMetric,
    SLOStatus,
    SLOTarget,
    SLOBudget,
    SLOHealthModel,
    SLORegistry,
    SLOWindow,
    get_slo_registry,
)


# ---------------------------------------------------------------------------
# SLOWindow
# ---------------------------------------------------------------------------


class TestSLOWindow:
    def test_empty_window_returns_zero_violation_rate(self):
        w = SLOWindow(window_s=60.0)
        assert w.violation_rate(threshold=1.0) == 0.0

    def test_single_violation(self):
        w = SLOWindow(window_s=60.0)
        w.record(2.0)   # above threshold 1.0
        assert w.violation_rate(threshold=1.0) == 1.0

    def test_no_violation_below_threshold(self):
        w = SLOWindow(window_s=60.0)
        for _ in range(10):
            w.record(0.5)
        assert w.violation_rate(threshold=1.0) == 0.0

    def test_partial_violations(self):
        w = SLOWindow(window_s=60.0)
        for _ in range(8):
            w.record(0.5)   # within budget
        for _ in range(2):
            w.record(2.0)   # violations
        assert abs(w.violation_rate(threshold=1.0) - 0.2) < 1e-9

    def test_eviction_removes_old_entries(self):
        w = SLOWindow(window_s=0.01)   # 10 ms window
        w.record(5.0)   # should be evicted
        time.sleep(0.02)
        w.record(0.5)   # fresh
        # After eviction, only the fresh entry remains → no violations for threshold=1.0
        assert w.violation_rate(threshold=1.0) == 0.0
        assert w.count() == 1

    def test_count_reflects_window(self):
        w = SLOWindow(window_s=0.01)
        w.record(1.0)
        w.record(2.0)
        time.sleep(0.02)
        assert w.count() == 0


# ---------------------------------------------------------------------------
# SLOBudget
# ---------------------------------------------------------------------------


class TestSLOBudget:
    def test_unknown_below_min_observations(self):
        target = SLOTarget(SLOMetric.ERROR_RATE, threshold=0.05)
        budget = SLOBudget(target)
        budget.record(0.10)   # violation but only 1 observation
        assert budget.status() == SLOStatus.UNKNOWN

    def test_healthy_when_no_violations(self):
        target = SLOTarget(SLOMetric.ERROR_RATE, threshold=0.05, budget_fraction=0.01)
        budget = SLOBudget(target)
        for _ in range(10):
            budget.record(0.02)
        assert budget.status() == SLOStatus.HEALTHY

    def test_unhealthy_when_budget_exhausted(self):
        # budget_fraction=0.01 means 1% of observations may violate
        # We'll record 10 violations out of 10 → 100% > 1%
        target = SLOTarget(SLOMetric.ERROR_RATE, threshold=0.05, budget_fraction=0.01)
        budget = SLOBudget(target)
        for _ in range(10):
            budget.record(0.10)   # all above 0.05
        assert budget.status() == SLOStatus.UNHEALTHY

    def test_degraded_when_burn_rate_high(self):
        # budget_fraction=0.05, degraded_burn_multiplier=2.0
        # Degraded threshold = 0.05 * 2.0 = 0.10 → need >10% violations
        # Fill 5 violations out of 10 = 50% violation rate → UNHEALTHY
        # So let's use 1 violation out of 10 = 10% → hit UNHEALTHY boundary
        # For DEGRADED: need violation_rate > 0.10 but <= 0.05 (impossible)
        # Let's rethink: budget=0.10, burn_multiplier=2 → degraded at 0.20
        # 3 violations / 10 obs = 0.30 → UNHEALTHY
        # 1.5 violations / 10 obs = 0.15 → DEGRADED (between 0.10 and 0.20)
        target = SLOTarget(
            SLOMetric.ERROR_RATE,
            threshold=0.05,
            budget_fraction=0.10,       # 10% budget
            degraded_burn_multiplier=2.0,  # degraded at 20%
        )
        budget = SLOBudget(target)
        # 1 violation + 9 ok = 10% violation rate (exceeds budget_fraction=0.10? No, exactly equal)
        # Let's do 2 violations out of 10 = 20% → UNHEALTHY (>0.10)
        # Do 1 violation out of 10 = 10% → exactly at budget → UNHEALTHY
        # Need violation between 0.10*2.0=0.20 and 0.10 to get DEGRADED
        # That requires violation_rate > budget_fraction AND <= burn threshold
        # budget_fraction=0.10, burn_threshold=0.20
        # So: violation_rate > 0.10 and <= 0.20 → UNHEALTHY (because 0.10 < vr triggers UNHEALTHY)
        # Actually: UNHEALTHY fires when vr > budget; DEGRADED fires when vr > burn_threshold
        # The condition ordering in SLOBudget: check UNHEALTHY first, then DEGRADED
        # So DEGRADED is NEVER reachable when burn_threshold < budget
        # For DEGRADED to be reachable: burn_threshold < budget
        # degraded_burn_multiplier < 1.0 achieves this:
        target2 = SLOTarget(
            SLOMetric.ERROR_RATE,
            threshold=0.05,
            budget_fraction=0.10,          # budget: 10%
            degraded_burn_multiplier=0.5,  # degraded_threshold = 0.05 → DEGRADED fires first
        )
        budget2 = SLOBudget(target2)
        # 1 violation out of 10 = 10% → hits UNHEALTHY (> 0.10? No! 0.10 is NOT > 0.10)
        # 11 violations out of 100 = 11% → hits UNHEALTHY
        # With burn_multiplier=0.5, degraded_threshold = 0.10 * 0.5 = 0.05
        # vr > 0.05 but <= 0.10 → DEGRADED
        for _ in range(9):
            budget2.record(0.02)   # within threshold
        budget2.record(0.10)       # 1 violation → 10% rate; 10% > 5% → check UNHEALTHY: 10% > 10%? No
        # So 10% is exactly at budget_fraction, not > it → DEGRADED check: 10% > 5%? Yes
        assert budget2.status() == SLOStatus.DEGRADED

    def test_remaining_budget_positive_when_healthy(self):
        target = SLOTarget(SLOMetric.LATENCY_P95_S, threshold=2.0, budget_fraction=0.05)
        budget = SLOBudget(target)
        for _ in range(10):
            budget.record(1.0)
        assert budget.remaining_budget() > 0

    def test_remaining_budget_negative_when_over(self):
        target = SLOTarget(SLOMetric.LATENCY_P95_S, threshold=2.0, budget_fraction=0.01)
        budget = SLOBudget(target)
        for _ in range(10):
            budget.record(3.0)   # all over threshold
        assert budget.remaining_budget() < 0


# ---------------------------------------------------------------------------
# SLOHealthModel
# ---------------------------------------------------------------------------


class TestSLOHealthModel:
    def _healthy_model(self) -> SLOHealthModel:
        return SLOHealthModel("svc", [
            SLOTarget(SLOMetric.ERROR_RATE, threshold=0.05, budget_fraction=0.01),
            SLOTarget(SLOMetric.LATENCY_P95_S, threshold=2.0, budget_fraction=0.01),
        ])

    def test_unknown_with_no_observations(self):
        model = self._healthy_model()
        assert model.status() == SLOStatus.UNKNOWN

    def test_healthy_when_all_within_budget(self):
        model = self._healthy_model()
        for _ in range(10):
            model.record(SLOMetric.ERROR_RATE, 0.01)
            model.record(SLOMetric.LATENCY_P95_S, 1.0)
        assert model.status() == SLOStatus.HEALTHY

    def test_unhealthy_propagates_from_one_metric(self):
        model = self._healthy_model()
        for _ in range(10):
            model.record(SLOMetric.ERROR_RATE, 0.01)     # healthy
            model.record(SLOMetric.LATENCY_P95_S, 5.0)   # all violations
        assert model.status() == SLOStatus.UNHEALTHY

    def test_unknown_metric_silently_ignored(self):
        model = self._healthy_model()
        model.record(SLOMetric.SATURATION, 0.99)   # not registered
        for _ in range(10):
            model.record(SLOMetric.ERROR_RATE, 0.01)
            model.record(SLOMetric.LATENCY_P95_S, 1.0)
        assert model.status() == SLOStatus.HEALTHY

    def test_per_metric_status_returns_dict(self):
        model = self._healthy_model()
        for _ in range(10):
            model.record(SLOMetric.ERROR_RATE, 0.01)
            model.record(SLOMetric.LATENCY_P95_S, 1.0)
        result = model.per_metric_status()
        assert SLOMetric.ERROR_RATE.value in result
        assert SLOMetric.LATENCY_P95_S.value in result

    def test_remaining_budgets_returns_dict(self):
        model = self._healthy_model()
        rb = model.remaining_budgets()
        assert SLOMetric.ERROR_RATE.value in rb


# ---------------------------------------------------------------------------
# SLORegistry
# ---------------------------------------------------------------------------


class TestSLORegistry:
    def test_register_and_retrieve(self):
        registry = SLORegistry()
        targets = [SLOTarget(SLOMetric.ERROR_RATE, threshold=0.05)]
        model = registry.register("svc-a", targets)
        assert registry.get("svc-a") is model

    def test_get_missing_returns_none(self):
        registry = SLORegistry()
        assert registry.get("nonexistent") is None

    def test_all_components_snapshot(self):
        registry = SLORegistry()
        registry.register("svc-a", [SLOTarget(SLOMetric.ERROR_RATE, threshold=0.05)])
        registry.register("svc-b", [SLOTarget(SLOMetric.SATURATION, threshold=0.80)])
        snap = registry.all_components()
        assert set(snap.keys()) == {"svc-a", "svc-b"}

    def test_aggregate_unknown_when_no_observations(self):
        registry = SLORegistry()
        registry.register("svc", [SLOTarget(SLOMetric.ERROR_RATE, threshold=0.05)])
        assert registry.aggregate_status() == SLOStatus.UNKNOWN

    def test_aggregate_worst_wins(self):
        registry = SLORegistry()
        healthy = registry.register("svc-h", [
            SLOTarget(SLOMetric.ERROR_RATE, threshold=0.05, budget_fraction=0.01)
        ])
        for _ in range(10):
            healthy.record(SLOMetric.ERROR_RATE, 0.01)

        unhealthy = registry.register("svc-u", [
            SLOTarget(SLOMetric.ERROR_RATE, threshold=0.05, budget_fraction=0.01)
        ])
        for _ in range(10):
            unhealthy.record(SLOMetric.ERROR_RATE, 0.99)   # all violations

        assert registry.aggregate_status() == SLOStatus.UNHEALTHY

    def test_module_singleton_is_reused(self):
        r1 = get_slo_registry()
        r2 = get_slo_registry()
        assert r1 is r2
