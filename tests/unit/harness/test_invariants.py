"""Tests for InvariantRegistry and MVP invariant factory functions.

TDD tests for Task 4 of the Disease 9 cross-repo integration test harness.
asyncio_mode = auto in pytest.ini -- no @pytest.mark.asyncio required.
"""

from __future__ import annotations

import time

import pytest

from tests.harness.invariants import (
    InvariantRegistry,
    epoch_monotonic,
    fault_isolation,
    single_routing_target,
    terminal_is_final,
)
from tests.harness.state_oracle import MockStateOracle
from tests.harness.types import ComponentStatus


# ---------------------------------------------------------------------------
# TestInvariantRegistry (6 tests)
# ---------------------------------------------------------------------------
class TestInvariantRegistry:
    """Core registry: registration, evaluation, and flapping suppression."""

    def test_no_invariants_no_violations(self) -> None:
        registry = InvariantRegistry()
        oracle = MockStateOracle()
        violations = registry.check_all(oracle)
        assert violations == []

    def test_passing_invariant(self) -> None:
        registry = InvariantRegistry()
        registry.register("always_ok", lambda oracle: None)
        oracle = MockStateOracle()
        violations = registry.check_all(oracle)
        assert violations == []

    def test_failing_invariant(self) -> None:
        registry = InvariantRegistry()
        registry.register("bad_thing", lambda oracle: "something broke")
        oracle = MockStateOracle()
        violations = registry.check_all(oracle)
        assert len(violations) == 1
        assert "[bad_thing]" in violations[0]
        assert "something broke" in violations[0]

    def test_flapping_suppression_on(self) -> None:
        registry = InvariantRegistry(debounce_window_s=10.0)
        registry.register("flapper", lambda oracle: "still broken", suppress_flapping=True)
        oracle = MockStateOracle()

        # First check: reported
        v1 = registry.check_all(oracle)
        assert len(v1) == 1

        # Second check within window: suppressed
        v2 = registry.check_all(oracle)
        assert len(v2) == 0

    def test_flapping_suppression_off_for_critical(self) -> None:
        registry = InvariantRegistry(debounce_window_s=10.0)
        registry.register("critical", lambda oracle: "on fire", suppress_flapping=False)
        oracle = MockStateOracle()

        # First check: reported
        v1 = registry.check_all(oracle)
        assert len(v1) == 1

        # Second check: also reported (no suppression)
        v2 = registry.check_all(oracle)
        assert len(v2) == 1

    def test_suppressed_count_tracked(self) -> None:
        registry = InvariantRegistry(debounce_window_s=10.0)
        registry.register("noisy", lambda oracle: "bzzzt", suppress_flapping=True)
        oracle = MockStateOracle()

        registry.check_all(oracle)  # 1st: reported
        registry.check_all(oracle)  # 2nd: suppressed
        registry.check_all(oracle)  # 3rd: suppressed

        assert registry.suppressed_counts["noisy"] == 2


# ---------------------------------------------------------------------------
# TestMVPInvariants (4 tests)
# ---------------------------------------------------------------------------
class TestMVPInvariants:
    """MVP invariant factory functions: epoch_monotonic, single_routing_target."""

    def test_epoch_monotonic_passes(self) -> None:
        check = epoch_monotonic()
        oracle = MockStateOracle()
        oracle.set_epoch(1)
        assert check(oracle) is None
        oracle.set_epoch(3)
        assert check(oracle) is None
        oracle.set_epoch(3)  # same epoch is fine
        assert check(oracle) is None

    def test_epoch_monotonic_fails_on_decrease(self) -> None:
        check = epoch_monotonic()
        oracle = MockStateOracle()
        oracle.set_epoch(5)
        assert check(oracle) is None

        oracle.set_epoch(3)
        result = check(oracle)
        assert result is not None
        assert "decreased" in result.lower()

    def test_single_routing_target_passes(self) -> None:
        check = single_routing_target()
        oracle = MockStateOracle()
        oracle.set_routing_decision("CLOUD_CLAUDE")
        assert check(oracle) is None

    def test_single_routing_target_fails_on_unknown(self) -> None:
        check = single_routing_target()
        oracle = MockStateOracle()
        # Default routing is None (not set), which is not in valid set
        result = check(oracle)
        assert result is not None
