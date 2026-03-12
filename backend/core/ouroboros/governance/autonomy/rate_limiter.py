"""backend/core/ouroboros/governance/autonomy/rate_limiter.py

Token Bucket Rate Limiter, RetryState, and ResourceUsage (Task H1).

Extracted from the deprecated ``advanced_orchestrator.py`` and decoupled from
OrchestratorConfig.  All parameters are explicit constructor/method args —
no hardcoded values.

Design:
    - TokenBucketRateLimiter: async-safe token bucket with configurable rate
      and burst capacity.  ``acquire(timeout)`` blocks until a token is
      available or times out.
    - RateLimiterConfig: immutable configuration dataclass.
    - RetryState: exponential backoff with jitter — all parameters explicit.
    - ResourceUsage: lightweight snapshot of system resource utilisation.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimiterConfig:
    """Immutable configuration for :class:`TokenBucketRateLimiter`.

    Parameters
    ----------
    rate:
        Token replenishment rate (tokens per second).
    burst:
        Maximum number of tokens the bucket can hold (burst capacity).
    """

    rate: float
    burst: int


# ---------------------------------------------------------------------------
# TokenBucketRateLimiter
# ---------------------------------------------------------------------------


class TokenBucketRateLimiter:
    """Async-safe token bucket rate limiter.

    Parameters
    ----------
    config:
        A :class:`RateLimiterConfig` specifying rate and burst.

    Usage::

        cfg = RateLimiterConfig(rate=10.0, burst=5)
        limiter = TokenBucketRateLimiter(config=cfg)

        if await limiter.acquire(timeout=1.0):
            # proceed with operation
            ...
    """

    def __init__(self, config: RateLimiterConfig) -> None:
        self._rate = config.rate
        self._burst = config.burst
        self._tokens = float(config.burst)
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """Acquire a single token, optionally waiting up to *timeout* seconds.

        Returns ``True`` if a token was acquired, ``False`` if the timeout
        elapsed without a token becoming available.  When *timeout* is
        ``None`` the method blocks indefinitely.

        The lock is released during sleep so that other callers can check
        token availability concurrently.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None

        while True:
            async with self._lock:
                self._refill()

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

                if deadline is not None and time.monotonic() >= deadline:
                    return False

                # Calculate wait time for the next token
                wait_time = (1.0 - self._tokens) / self._rate
                if deadline is not None:
                    wait_time = min(wait_time, deadline - time.monotonic())

            # Sleep OUTSIDE the lock so other callers are not blocked
            await asyncio.sleep(max(0.01, wait_time))

    def get_status(self) -> Dict[str, Any]:
        """Return a snapshot of the limiter state.

        Keys:
            tokens_available: current token count (after refill).
            rate_per_second: configured token rate.
            burst_capacity: configured burst cap.
        """
        self._refill()
        return {
            "tokens_available": self._tokens,
            "rate_per_second": self._rate,
            "burst_capacity": self._burst,
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Top up tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_update
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_update = now


# ---------------------------------------------------------------------------
# RetryState
# ---------------------------------------------------------------------------


@dataclass
class RetryState:
    """Tracks retry attempts with exponential backoff and jitter.

    All backoff parameters are passed explicitly to methods — there are no
    hidden configuration dependencies.
    """

    attempt: int = 0
    last_error: Optional[str] = None
    last_attempt_time: float = 0.0
    total_wait_time: float = 0.0

    def get_next_delay(
        self,
        base_delay: float,
        max_delay: float,
        jitter_factor: float = 0.0,
    ) -> float:
        """Compute the next retry delay using exponential backoff + jitter.

        Parameters
        ----------
        base_delay:
            The base delay in seconds (attempt 0 delay before jitter).
        max_delay:
            Maximum delay cap in seconds.
        jitter_factor:
            Fraction of the capped delay to add as uniform random jitter
            (0.0 = no jitter, 0.5 = up to 50% extra).

        Returns
        -------
        float
            The computed delay in seconds.
        """
        raw_delay = base_delay * (2 ** self.attempt)
        capped_delay = min(raw_delay, max_delay)
        jitter = random.uniform(0.0, jitter_factor * capped_delay)
        return capped_delay + jitter

    def should_retry(self, max_retries: int) -> bool:
        """Return True if another retry is allowed.

        Parameters
        ----------
        max_retries:
            Maximum number of retries permitted (attempt must be strictly
            less than this value).
        """
        return self.attempt < max_retries


# ---------------------------------------------------------------------------
# ResourceUsage
# ---------------------------------------------------------------------------


@dataclass
class ResourceUsage:
    """Lightweight snapshot of system resource utilisation.

    Fields
    ------
    memory_mb:
        Current memory usage in megabytes.
    disk_free_mb:
        Free disk space in megabytes.
    cpu_percent:
        CPU utilisation percentage (0–100).
    active_tasks:
        Number of active async tasks / operations.
    timestamp_ns:
        Monotonic timestamp (``time.monotonic_ns()``) — auto-populated.
        Consistent with C+ architecture timestamp convention.
    """

    memory_mb: float
    disk_free_mb: float
    cpu_percent: float
    active_tasks: int
    timestamp_ns: int = field(default_factory=time.monotonic_ns)
