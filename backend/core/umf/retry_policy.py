"""Circuit breaker and retry budget for UMF delivery resilience.

Stdlib-only module -- no JARVIS imports.

CircuitBreaker implements the three-state pattern:
    CLOSED  -->  OPEN  -->  HALF_OPEN  -->  CLOSED
             (failures)   (timeout)      (success)

RetryBudget provides bounded retries with exponential backoff and jitter.
"""
from __future__ import annotations

import random
import time


class CircuitBreaker:
    """Three-state circuit breaker: closed -> open -> half_open -> closed.

    Parameters
    ----------
    failure_threshold:
        Number of consecutive failures before the breaker opens.
    recovery_timeout_s:
        Seconds to wait in the open state before transitioning to half_open.
    """

    __slots__ = (
        "_failure_threshold",
        "_recovery_timeout_s",
        "_failure_count",
        "_last_failure_time",
        "_state",
    )

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 30.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout_s = recovery_timeout_s
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._state: str = "closed"

    @property
    def state(self) -> str:
        """Return the effective state, promoting open -> half_open when timeout elapses."""
        if self._state == "open":
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._recovery_timeout_s:
                return "half_open"
        return self._state

    def can_execute(self) -> bool:
        """Return True if a request is allowed through the breaker."""
        return self.state != "open"

    def record_success(self) -> None:
        """Record a successful execution, resetting the breaker to closed."""
        self._failure_count = 0
        self._state = "closed"

    def record_failure(self) -> None:
        """Record a failed execution, potentially opening the breaker."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self._failure_threshold:
            self._state = "open"


class RetryBudget:
    """Bounded retry budget with exponential backoff and optional jitter.

    Parameters
    ----------
    max_retries:
        Maximum number of retry attempts allowed (attempt 0 is the first retry).
    base_delay_s:
        Base delay in seconds for the first retry.
    max_delay_s:
        Upper bound on computed delay.
    jitter_factor:
        Fraction of the delay to randomize (0.0 = deterministic, 0.3 = +/-30%).
    """

    __slots__ = ("_max_retries", "_base_delay_s", "_max_delay_s", "_jitter_factor")

    def __init__(
        self,
        max_retries: int = 3,
        base_delay_s: float = 0.5,
        max_delay_s: float = 30.0,
        jitter_factor: float = 0.3,
    ) -> None:
        self._max_retries = max_retries
        self._base_delay_s = base_delay_s
        self._max_delay_s = max_delay_s
        self._jitter_factor = jitter_factor

    def should_retry(self, attempt: int) -> bool:
        """Return True if *attempt* is within the retry budget."""
        return attempt < self._max_retries

    def compute_delay(self, attempt: int) -> float:
        """Compute delay for the given attempt with exponential backoff and jitter.

        The raw delay is ``base_delay_s * 2 ** attempt``, capped at *max_delay_s*.
        If *jitter_factor* > 0, a uniform random offset in
        ``[-jitter_factor * delay, +jitter_factor * delay]`` is applied (clamped >= 0).
        """
        raw = self._base_delay_s * (2 ** attempt)
        capped = min(raw, self._max_delay_s)
        if self._jitter_factor > 0:
            jitter = capped * self._jitter_factor
            capped = max(0.0, capped + random.uniform(-jitter, jitter))
        return capped
