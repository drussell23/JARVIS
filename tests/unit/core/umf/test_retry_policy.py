"""Tests for the UMF circuit breaker and retry budget.

Covers three-state circuit breaker transitions (closed -> open -> half_open -> closed)
and bounded retry budget with exponential backoff.
"""
from __future__ import annotations

import time

import pytest

from backend.core.umf.retry_policy import CircuitBreaker, RetryBudget


class TestCircuitBreaker:
    """Five tests covering circuit breaker state transitions."""

    def test_starts_closed(self):
        """A fresh circuit breaker is in the closed state and allows execution."""
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout_s=30.0)
        assert cb.state == "closed"
        assert cb.can_execute() is True

    def test_opens_after_threshold_failures(self):
        """After recording failure_threshold failures, the breaker opens."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_s=30.0)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "open"
        assert cb.can_execute() is False

    def test_success_resets_failure_count(self):
        """Recording a success after partial failures keeps the breaker closed."""
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout_s=30.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.state == "closed"
        # Two more failures should not open (count was reset)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"

    def test_half_open_after_recovery_timeout(self):
        """After recovery timeout elapses, the breaker transitions to half_open."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_s=0.01)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        time.sleep(0.02)
        assert cb.state == "half_open"
        assert cb.can_execute() is True

    def test_half_open_success_closes(self):
        """Recording a success while half_open transitions back to closed."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_s=0.01)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == "half_open"
        cb.record_success()
        assert cb.state == "closed"


class TestRetryBudget:
    """Four tests covering retry budget decisions and delay computation."""

    def test_allows_within_budget(self):
        """Attempts below max_retries are allowed."""
        rb = RetryBudget(max_retries=3)
        assert rb.should_retry(0) is True
        assert rb.should_retry(2) is True

    def test_rejects_over_budget(self):
        """Attempts at or above max_retries are rejected."""
        rb = RetryBudget(max_retries=3)
        assert rb.should_retry(3) is False
        assert rb.should_retry(5) is False

    def test_delay_increases_exponentially(self):
        """With jitter_factor=0, delay doubles each attempt."""
        rb = RetryBudget(base_delay_s=1.0, max_delay_s=100.0, jitter_factor=0.0)
        assert rb.compute_delay(0) == pytest.approx(1.0)
        assert rb.compute_delay(1) == pytest.approx(2.0)
        assert rb.compute_delay(2) == pytest.approx(4.0)

    def test_delay_capped_at_max(self):
        """Delay never exceeds max_delay_s regardless of attempt number."""
        rb = RetryBudget(base_delay_s=1.0, max_delay_s=5.0, jitter_factor=0.0)
        assert rb.compute_delay(10) == pytest.approx(5.0)
