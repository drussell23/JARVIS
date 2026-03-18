"""tests/unit/core/test_component_circuit_breaker.py — Disease 4 breaker tests."""
from __future__ import annotations

import pytest

from backend.core.component_circuit_breaker import (
    BreakerConfig,
    BreakerState,
    CircuitBreakerRegistry,
    ComponentCircuitBreaker,
    ComponentState,
    get_circuit_breaker_registry,
)


class TestComponentCircuitBreaker:
    def _make(self, **kwargs) -> ComponentCircuitBreaker:
        return ComponentCircuitBreaker("svc", BreakerConfig(**kwargs))

    # ------------------------------------------------------------------
    # CLOSED state
    # ------------------------------------------------------------------

    def test_initial_state_is_closed(self):
        b = self._make()
        assert b.state == BreakerState.CLOSED

    def test_can_execute_when_closed(self):
        b = self._make()
        allowed, reason = b.can_execute()
        assert allowed
        assert reason == ""

    def test_healthy_when_no_failures(self):
        b = self._make()
        assert b.component_state == ComponentState.HEALTHY

    def test_record_success_keeps_closed(self):
        b = self._make()
        b.record_success()
        assert b.state == BreakerState.CLOSED

    def test_record_success_resets_consecutive_failures(self):
        b = self._make(failure_threshold=5)
        b.record_failure()
        b.record_failure()
        b.record_success()
        assert b.consecutive_failures == 0

    # ------------------------------------------------------------------
    # Failure accumulation
    # ------------------------------------------------------------------

    def test_failure_below_threshold_stays_closed(self):
        b = self._make(failure_threshold=3)
        b.record_failure()
        b.record_failure()
        assert b.state == BreakerState.CLOSED
        assert b.component_state == ComponentState.DEGRADED

    def test_failure_at_threshold_opens_breaker(self):
        b = self._make(failure_threshold=3)
        for _ in range(3):
            b.record_failure()
        assert b.state == BreakerState.OPEN
        assert b.component_state == ComponentState.FAILED

    def test_single_failure_threshold_opens_immediately(self):
        b = self._make(failure_threshold=1)
        b.record_failure(RuntimeError("bang"))
        assert b.state == BreakerState.OPEN

    def test_last_failure_reason_stored(self):
        b = self._make(failure_threshold=1)
        b.record_failure(ValueError("bad input"))
        assert "bad input" in b.last_failure_reason

    # ------------------------------------------------------------------
    # OPEN state
    # ------------------------------------------------------------------

    def test_can_execute_false_when_open(self):
        b = self._make(failure_threshold=1, recovery_timeout_s=9999.0)
        b.record_failure()
        allowed, reason = b.can_execute()
        assert not allowed
        assert "OPEN" in reason

    def test_open_transitions_to_half_open_after_timeout(self):
        b = self._make(failure_threshold=1, recovery_timeout_s=0.0)
        b.record_failure()
        # With 0s timeout, the very next can_execute should promote to HALF_OPEN
        allowed, _ = b.can_execute()
        assert b.state == BreakerState.HALF_OPEN
        assert allowed

    def test_rejected_count_increments_when_open(self):
        b = self._make(failure_threshold=1, recovery_timeout_s=9999.0)
        b.record_failure()
        b.can_execute()
        b.can_execute()
        assert b.total_rejected == 2

    # ------------------------------------------------------------------
    # HALF_OPEN state
    # ------------------------------------------------------------------

    def test_half_open_allows_probe_call(self):
        b = self._make(failure_threshold=1, recovery_timeout_s=0.0,
                       half_open_max_calls=1)
        b.record_failure()
        # Advance to HALF_OPEN
        allowed, _ = b.can_execute()
        assert b.state == BreakerState.HALF_OPEN
        assert allowed

    def test_half_open_success_closes_breaker(self):
        b = self._make(failure_threshold=1, recovery_timeout_s=0.0)
        b.record_failure()
        b.can_execute()  # → HALF_OPEN
        b.record_success()
        assert b.state == BreakerState.CLOSED

    def test_half_open_failure_opens_again(self):
        b = self._make(failure_threshold=1, recovery_timeout_s=0.0)
        b.record_failure()
        b.can_execute()  # → HALF_OPEN
        b.record_failure()
        assert b.state == BreakerState.OPEN

    def test_half_open_probe_limit_enforced(self):
        b = self._make(failure_threshold=1, recovery_timeout_s=0.0,
                       half_open_max_calls=1)
        b.record_failure()
        b.can_execute()        # first call → HALF_OPEN, probe allowed
        allowed, reason = b.can_execute()  # second call → limit reached
        assert not allowed
        assert "HALF_OPEN" in reason

    # ------------------------------------------------------------------
    # Counters
    # ------------------------------------------------------------------

    def test_total_successes_count(self):
        b = self._make()
        b.record_success()
        b.record_success()
        assert b.total_successes == 2

    def test_total_failures_count(self):
        b = self._make(failure_threshold=10)
        b.record_failure()
        b.record_failure()
        assert b.total_failures == 2


class TestCircuitBreakerRegistry:
    def test_get_or_create_returns_breaker(self):
        r = CircuitBreakerRegistry()
        b = r.get_or_create("alpha")
        assert isinstance(b, ComponentCircuitBreaker)

    def test_get_or_create_returns_same_instance(self):
        r = CircuitBreakerRegistry()
        b1 = r.get_or_create("alpha")
        b2 = r.get_or_create("alpha")
        assert b1 is b2

    def test_get_returns_none_for_unknown(self):
        r = CircuitBreakerRegistry()
        assert r.get("unknown") is None

    def test_custom_config_applied(self):
        r = CircuitBreakerRegistry()
        cfg = BreakerConfig(failure_threshold=1)
        b = r.get_or_create("svc", cfg)
        assert b.config.failure_threshold == 1

    def test_all_failed_returns_open_breakers(self):
        r = CircuitBreakerRegistry()
        b = r.get_or_create("svc", BreakerConfig(failure_threshold=1))
        b.record_failure()
        assert b in r.all_failed()

    def test_all_healthy_excludes_failed(self):
        r = CircuitBreakerRegistry()
        r.get_or_create("good")
        b2 = r.get_or_create("bad", BreakerConfig(failure_threshold=1))
        b2.record_failure()
        healthy = r.all_healthy()
        assert all(x.component != "bad" for x in healthy)

    def test_snapshot_returns_component_states(self):
        r = CircuitBreakerRegistry()
        r.get_or_create("a")
        r.get_or_create("b", BreakerConfig(failure_threshold=1)).record_failure()
        snap = r.snapshot()
        assert snap["a"] == ComponentState.HEALTHY
        assert snap["b"] == ComponentState.FAILED

    def test_reset_all_clears_registry(self):
        r = CircuitBreakerRegistry()
        r.get_or_create("svc")
        r.reset_all()
        assert r.get("svc") is None

    def test_default_config_used_when_none_provided(self):
        r = CircuitBreakerRegistry()
        b = r.get_or_create("svc")
        assert b.config == BreakerConfig()


class TestSingleton:
    def test_get_circuit_breaker_registry_is_reused(self):
        r1 = get_circuit_breaker_registry()
        r2 = get_circuit_breaker_registry()
        assert r1 is r2
