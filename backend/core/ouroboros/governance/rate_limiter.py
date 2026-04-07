# backend/core/ouroboros/governance/rate_limiter.py
"""
Intelligent Rate Limiter — Core Components
============================================

Foundation layer for the Ouroboros governance pipeline's rate-limiting
subsystem.  All six components live in this single module:

  1. **Config** — ``EndpointConfig``, ``ProviderProfile``, ``DEFAULT_PROFILES``
  2. **LatencyRing** — circular buffer of observed latencies
  3. **MemoryRateLimitStore** — async in-memory token persistence
  4. **TokenBucket** — async token-bucket with lazy refill
  5. **CircuitBreaker** — 3-state FSM (CLOSED / OPEN / HALF_OPEN)
  6. **PredictiveThrottle** — 3-layer deterministic throttle predictor

All async code uses ``asyncio`` primitives — zero synchronous blocking.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import statistics
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Dict, List

logger = logging.getLogger(__name__)


# ===========================================================================
# Component 1: Config
# ===========================================================================


@dataclass(frozen=True)
class EndpointConfig:
    """Rate-limit configuration for a single API endpoint."""

    name: str
    rpm: int
    tpm: int = 0
    burst: int = 1
    timeout_s: float = 30.0
    retry_after_default_s: float = 5.0


@dataclass(frozen=True)
class ProviderProfile:
    """Collection of endpoint configs for an API provider."""

    provider_name: str
    endpoints: Dict[str, EndpointConfig]


def _env_int(key: str, default: int) -> int:
    """Read an int from env, falling back to *default*."""
    raw = os.environ.get(key)
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            logger.warning("Invalid int for %s=%r, using default %d", key, raw, default)
    return default


def _build_default_profiles() -> Dict[str, ProviderProfile]:
    """Construct DEFAULT_PROFILES, respecting OUROBOROS_RATELIMIT_* env overrides."""

    dw_files_upload_rpm = _env_int("OUROBOROS_RATELIMIT_DOUBLEWORD_FILES_UPLOAD_RPM", 30)
    dw_batches_create_rpm = _env_int("OUROBOROS_RATELIMIT_DOUBLEWORD_BATCHES_CREATE_RPM", 30)
    dw_batches_poll_rpm = _env_int("OUROBOROS_RATELIMIT_DOUBLEWORD_BATCHES_POLL_RPM", 60)
    dw_batches_retrieve_rpm = _env_int("OUROBOROS_RATELIMIT_DOUBLEWORD_BATCHES_RETRIEVE_RPM", 60)

    claude_messages_rpm = _env_int("OUROBOROS_RATELIMIT_CLAUDE_MESSAGES_RPM", 60)
    claude_messages_tpm = _env_int("OUROBOROS_RATELIMIT_CLAUDE_MESSAGES_TPM", 100_000)

    doubleword = ProviderProfile(
        provider_name="doubleword",
        endpoints={
            "files_upload": EndpointConfig(name="files_upload", rpm=dw_files_upload_rpm),
            "batches_create": EndpointConfig(name="batches_create", rpm=dw_batches_create_rpm),
            "batches_poll": EndpointConfig(name="batches_poll", rpm=dw_batches_poll_rpm),
            "batches_retrieve": EndpointConfig(name="batches_retrieve", rpm=dw_batches_retrieve_rpm),
        },
    )

    claude = ProviderProfile(
        provider_name="claude",
        endpoints={
            "messages": EndpointConfig(
                name="messages",
                rpm=claude_messages_rpm,
                tpm=claude_messages_tpm,
            ),
        },
    )

    return {"doubleword": doubleword, "claude": claude}


DEFAULT_PROFILES: Dict[str, ProviderProfile] = _build_default_profiles()


# ===========================================================================
# Component 2: LatencyRing
# ===========================================================================


class LatencyRing:
    """Circular buffer of float latencies backed by ``collections.deque``."""

    __slots__ = ("_buf",)

    def __init__(self, capacity: int = 100) -> None:
        self._buf: deque[float] = deque(maxlen=capacity)

    # -- mutators ----------------------------------------------------------

    def push(self, v: float) -> None:
        """Append a latency observation (oldest dropped on overflow)."""
        self._buf.append(v)

    def seed(self, latencies: List[float]) -> None:
        """Pre-populate the ring from a list of latencies."""
        for v in latencies:
            self._buf.append(v)

    # -- accessors ---------------------------------------------------------

    def values(self) -> List[float]:
        """Return all values in insertion order."""
        return list(self._buf)

    def last_n(self, n: int) -> List[float]:
        """Return the *n* most recent values."""
        buf = self._buf
        if n >= len(buf):
            return list(buf)
        return list(buf)[-n:]

    def __len__(self) -> int:
        return len(self._buf)


# ===========================================================================
# Component 3: RateLimitStore (abstract + memory impl)
# ===========================================================================


class RateLimitStore(ABC):
    """Abstract async store for token-bucket state."""

    @abstractmethod
    async def get_state(self, key: str) -> Dict[str, float]:
        """Return ``{"tokens": float, "last_refill": float}``."""
        ...

    @abstractmethod
    async def update_state(self, key: str, tokens: float, last_refill: float) -> None:
        ...


class MemoryRateLimitStore(RateLimitStore):
    """In-memory implementation with per-key ``asyncio.Lock``."""

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, float]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def get_state(self, key: str) -> Dict[str, float]:
        lock = self._lock_for(key)
        async with lock:
            if key not in self._data:
                return {"tokens": 0.0, "last_refill": 0.0}
            return dict(self._data[key])

    async def update_state(self, key: str, tokens: float, last_refill: float) -> None:
        lock = self._lock_for(key)
        async with lock:
            self._data[key] = {"tokens": tokens, "last_refill": last_refill}


# ===========================================================================
# Component 4: TokenBucket
# ===========================================================================


class TokenBucket:
    """Async token bucket with lazy refill.

    * ``rpm`` — requests per minute (refill rate).
    * ``burst`` — maximum tokens that can accumulate.
    * On first acquire the bucket starts with full *burst* capacity.
    * ``set_throttle(multiplier)`` scales the effective refill rate.
    * When tokens are exhausted, computes exact sleep — no spin loops.
    """

    def __init__(
        self,
        key: str,
        store: RateLimitStore,
        rpm: int,
        burst: int = 1,
    ) -> None:
        self._key = key
        self._store = store
        self._rpm = rpm
        self._burst = burst
        self._throttle: float = 1.0
        self._initialized = False

    # -- public API --------------------------------------------------------

    def set_throttle(self, multiplier: float) -> None:
        """Adjust effective refill rate.  0 < multiplier <= 1."""
        self._throttle = max(0.01, min(1.0, multiplier))

    async def acquire(self, tokens: int = 1) -> float:
        """Acquire *tokens*.  Returns seconds waited (0.0 if immediate)."""
        now = time.monotonic()

        state = await self._store.get_state(self._key)
        current_tokens = state["tokens"]
        last_refill = state["last_refill"]

        # First-ever acquire: seed bucket with full burst capacity
        if not self._initialized:
            self._initialized = True
            current_tokens = float(self._burst)
            last_refill = now

        # Lazy refill: compute tokens accrued since last refill
        effective_rate = (self._rpm / 60.0) * self._throttle  # tokens per second
        elapsed = now - last_refill
        current_tokens = min(
            float(self._burst),
            current_tokens + elapsed * effective_rate,
        )
        last_refill = now

        if current_tokens >= tokens:
            # Immediate — deduct and persist
            current_tokens -= tokens
            await self._store.update_state(self._key, current_tokens, last_refill)
            return 0.0

        # Need to wait for tokens to accumulate
        deficit = tokens - current_tokens
        wait_s = deficit / effective_rate
        await asyncio.sleep(wait_s)

        # After sleeping, bucket has refilled exactly enough
        now2 = time.monotonic()
        current_tokens = 0.0  # we consumed everything we waited for
        await self._store.update_state(self._key, current_tokens, now2)
        return wait_s


# ===========================================================================
# Component 5: CircuitBreaker
# ===========================================================================


class BreakerState(str, enum.Enum):
    """Circuit-breaker states."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpen(Exception):
    """Raised when a call is attempted on an OPEN circuit breaker."""


_RETRIABLE_STATUS_CODES = frozenset({429, 500, 502, 503})


class CircuitBreaker:
    """3-state FSM: CLOSED -> OPEN -> HALF_OPEN.

    * ``failure_threshold`` consecutive failures trip CLOSED -> OPEN.
    * After ``recovery_timeout_s`` seconds, OPEN -> HALF_OPEN on next check.
    * ``record_success()`` in HALF_OPEN -> CLOSED.
    * ``record_failure()`` in HALF_OPEN -> OPEN.
    * ``state_changed`` — ``asyncio.Event`` set on every transition.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_s: float = 30.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout_s = recovery_timeout_s
        self._state = BreakerState.CLOSED
        self._failure_count = 0
        self._opened_at: float = 0.0
        self.state_changed: asyncio.Event = asyncio.Event()

    # -- properties --------------------------------------------------------

    @property
    def state(self) -> BreakerState:
        return self._state

    # -- public API --------------------------------------------------------

    def check(self) -> None:
        """Raise :class:`CircuitBreakerOpen` if the breaker is OPEN.

        If the recovery timeout has elapsed, auto-transition to HALF_OPEN
        (lazy check via ``time.monotonic()``).
        """
        if self._state == BreakerState.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._recovery_timeout_s:
                self._transition(BreakerState.HALF_OPEN)
            else:
                raise CircuitBreakerOpen(
                    f"Circuit breaker is OPEN (opened {elapsed:.1f}s ago)"
                )

    def record_success(self) -> None:
        """Record a successful call."""
        if self._state == BreakerState.HALF_OPEN:
            self._failure_count = 0
            self._transition(BreakerState.CLOSED)
        elif self._state == BreakerState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed call."""
        if self._state == BreakerState.HALF_OPEN:
            # Single failure in HALF_OPEN re-opens
            self._opened_at = time.monotonic()
            self._transition(BreakerState.OPEN)
        elif self._state == BreakerState.CLOSED:
            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self._opened_at = time.monotonic()
                self._transition(BreakerState.OPEN)

    @staticmethod
    def is_retriable_failure(status: int) -> bool:
        """Return True if *status* is a retriable failure (429/500/502/503)."""
        return status in _RETRIABLE_STATUS_CODES

    # -- internals ---------------------------------------------------------

    def _transition(self, new_state: BreakerState) -> None:
        old = self._state
        self._state = new_state
        logger.info("CircuitBreaker %s -> %s", old.value, new_state.value)
        self.state_changed.set()


# ===========================================================================
# Component 6: PredictiveThrottle
# ===========================================================================


class PredictiveThrottle:
    """3-layer deterministic throttle predictor.

    Layers:
      1. **EWMA** — exponential moving average vs. baseline median.
      2. **Linear regression** — slope of last 20 projects timeout breach.
      3. **Variance spike** — variance of last 5 vs. last 20.

    Output = ``min(layer1, layer2, layer3)`` clamped to ``[0.05, 1.0]``.
    """

    def __init__(
        self,
        timeout_s: float,
        ewma_alpha: float = 0.3,
        variance_spike_ratio: float = 5.0,
    ) -> None:
        self._timeout_s = timeout_s
        self._ewma_alpha = ewma_alpha
        self._variance_spike_ratio = variance_spike_ratio

    def compute(self, ring: LatencyRing) -> float:
        """Return throttle multiplier in [0.05, 1.0]."""
        vals = ring.values()
        if len(vals) < 2:
            return 1.0

        layer1 = self._layer_ewma(vals)
        layer2 = self._layer_regression(vals)
        layer3 = self._layer_variance_spike(ring)

        result = min(layer1, layer2, layer3)
        return max(0.05, min(1.0, result))

    # -- Layer 1: EWMA ----------------------------------------------------

    def _layer_ewma(self, vals: List[float]) -> float:
        """Compare EWMA to baseline (median of first 10)."""
        if len(vals) < 10:
            return 1.0

        baseline = statistics.median(vals[:10])
        if baseline <= 0:
            return 1.0

        # Compute EWMA over all values
        alpha = self._ewma_alpha
        ewma = vals[0]
        for v in vals[1:]:
            ewma = alpha * v + (1 - alpha) * ewma

        ratio = ewma / baseline
        if ratio > 3.0:
            return 0.3
        elif ratio > 2.0:
            return 0.6
        return 1.0

    # -- Layer 2: Linear regression ----------------------------------------

    def _layer_regression(self, vals: List[float]) -> float:
        """Fit slope of last 20; project time to timeout breach."""
        window = vals[-20:] if len(vals) >= 20 else vals
        n = len(window)
        if n < 3:
            return 1.0

        # Simple linear regression: y = a + b*x  where x = 0..n-1
        x_mean = (n - 1) / 2.0
        y_mean = sum(window) / n

        num = 0.0
        den = 0.0
        for i, y in enumerate(window):
            dx = i - x_mean
            num += dx * (y - y_mean)
            den += dx * dx

        if den == 0:
            return 1.0

        slope = num / den
        if slope <= 0:
            return 1.0  # latency decreasing — no concern

        # Project from latest value: how many samples until timeout?
        current = window[-1]
        if current >= self._timeout_s:
            return 0.2

        samples_to_timeout = (self._timeout_s - current) / slope
        # Convert samples to approximate seconds (assume 1 sample/sec)
        seconds_to_breach = samples_to_timeout

        if seconds_to_breach < 10:
            return 0.2
        elif seconds_to_breach < 30:
            return 0.4
        return 1.0

    # -- Layer 3: Variance spike -------------------------------------------

    def _layer_variance_spike(self, ring: LatencyRing) -> float:
        """Compare variance of last 5 vs. last 20."""
        recent_5 = ring.last_n(5)
        recent_20 = ring.last_n(20)

        if len(recent_5) < 2 or len(recent_20) < 2:
            return 1.0

        var_5 = statistics.variance(recent_5)
        var_20 = statistics.variance(recent_20)

        if var_20 <= 0:
            # If the background has zero variance but recent has some, that's a spike
            if var_5 > 0:
                return 0.2
            return 1.0

        ratio = var_5 / var_20
        if ratio > self._variance_spike_ratio:
            return 0.2
        return 1.0
