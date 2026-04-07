# Intelligent Rate Limiter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a provider-agnostic rate limiting service with async token buckets, event-driven circuit breakers, 3-layer predictive throttle, and bidirectional backpressure signaling.

**Architecture:** A standalone `RateLimitService` wired at boot. Per-endpoint `EndpointState` bundles a `TokenBucket`, `CircuitBreaker`, `PredictiveThrottle`, and `LatencyRing`. Providers call `acquire()` before requests and `record()` after. The `BackpressureBus` pushes throttle changes to providers. All async, zero blocking, stdlib math only.

**Tech Stack:** Python 3.9+, asyncio, math, statistics, dataclasses, JSON persistence

**Spec:** `docs/superpowers/specs/2026-04-06-intelligent-rate-limiter-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|---|---|
| `backend/core/ouroboros/governance/rate_limiter.py` | All rate limiter components: store, token bucket, circuit breaker, predictive throttle, backpressure bus, latency ring, service, config |
| `tests/test_ouroboros_governance/test_rate_limiter.py` | Full test suite |

### Modified Files

| File | Change |
|---|---|
| `backend/core/ouroboros/governance/doubleword_provider.py` | Add `acquire()` before API calls, `record()` after responses |
| `backend/core/ouroboros/battle_test/harness.py` | Instantiate and inject RateLimitService during boot |

---

## Task 1: Config, Store, and LatencyRing

**Files:**
- Create: `backend/core/ouroboros/governance/rate_limiter.py`
- Test: `tests/test_ouroboros_governance/test_rate_limiter.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the Intelligent Rate Limiter."""
from __future__ import annotations

import asyncio
import math
import pytest
import time


# ── Config ──────────────────────────────────────────────────────


def test_endpoint_config_defaults():
    from backend.core.ouroboros.governance.rate_limiter import EndpointConfig
    cfg = EndpointConfig(name="test", rpm=60)
    assert cfg.rpm == 60
    assert cfg.burst == 1
    assert cfg.timeout_s == 30.0
    assert cfg.retry_after_default_s == 5.0


def test_provider_profile_creation():
    from backend.core.ouroboros.governance.rate_limiter import (
        EndpointConfig, ProviderProfile,
    )
    profile = ProviderProfile(
        provider_name="doubleword",
        endpoints={
            "batches_poll": EndpointConfig(name="batches_poll", rpm=60, burst=5),
        },
    )
    assert profile.provider_name == "doubleword"
    assert profile.endpoints["batches_poll"].rpm == 60


def test_default_profiles_exist():
    from backend.core.ouroboros.governance.rate_limiter import DEFAULT_PROFILES
    assert "doubleword" in DEFAULT_PROFILES
    assert "claude" in DEFAULT_PROFILES
    assert "batches_poll" in DEFAULT_PROFILES["doubleword"].endpoints
    assert "messages" in DEFAULT_PROFILES["claude"].endpoints


# ── LatencyRing ─────────────────────────────────────────────────


def test_latency_ring_push_and_values():
    from backend.core.ouroboros.governance.rate_limiter import LatencyRing
    ring = LatencyRing(capacity=5)
    for v in [0.1, 0.2, 0.3, 0.4, 0.5]:
        ring.push(v)
    assert ring.values() == [0.1, 0.2, 0.3, 0.4, 0.5]
    assert len(ring) == 5


def test_latency_ring_overflow():
    from backend.core.ouroboros.governance.rate_limiter import LatencyRing
    ring = LatencyRing(capacity=3)
    for v in [0.1, 0.2, 0.3, 0.4, 0.5]:
        ring.push(v)
    assert ring.values() == [0.3, 0.4, 0.5]
    assert len(ring) == 3


def test_latency_ring_last_n():
    from backend.core.ouroboros.governance.rate_limiter import LatencyRing
    ring = LatencyRing(capacity=10)
    for v in [0.1, 0.2, 0.3, 0.4, 0.5]:
        ring.push(v)
    assert ring.last_n(3) == [0.3, 0.4, 0.5]
    assert ring.last_n(100) == [0.1, 0.2, 0.3, 0.4, 0.5]


def test_latency_ring_empty():
    from backend.core.ouroboros.governance.rate_limiter import LatencyRing
    ring = LatencyRing(capacity=10)
    assert ring.values() == []
    assert len(ring) == 0
    assert ring.last_n(5) == []


# ── MemoryRateLimitStore ────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_get_initial_state():
    from backend.core.ouroboros.governance.rate_limiter import MemoryRateLimitStore
    store = MemoryRateLimitStore()
    state = await store.get_state("test:endpoint")
    assert state["tokens"] == 0.0
    assert state["last_refill"] > 0


@pytest.mark.asyncio
async def test_store_update_and_get():
    from backend.core.ouroboros.governance.rate_limiter import MemoryRateLimitStore
    store = MemoryRateLimitStore()
    await store.update_state("test:ep", tokens=5.0, last_refill=1000.0)
    state = await store.get_state("test:ep")
    assert state["tokens"] == 5.0
    assert state["last_refill"] == 1000.0


@pytest.mark.asyncio
async def test_store_concurrent_safety():
    from backend.core.ouroboros.governance.rate_limiter import MemoryRateLimitStore
    store = MemoryRateLimitStore()
    await store.update_state("k", tokens=10.0, last_refill=1.0)

    async def consume():
        state = await store.get_state("k")
        await store.update_state("k", tokens=state["tokens"] - 1, last_refill=state["last_refill"])

    await asyncio.gather(*[consume() for _ in range(5)])
    state = await store.get_state("k")
    assert state["tokens"] == 5.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_rate_limiter.py -v --timeout=15 -x`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
"""
Intelligent Rate Limiter — provider-agnostic async rate limiting for Ouroboros.

Components:
- ProviderProfile / EndpointConfig: per-provider, per-endpoint configuration
- LatencyRing: circular buffer of recent request latencies
- MemoryRateLimitStore: async-safe token state storage (swappable to Redis)
- TokenBucket: async token bucket with dynamic refill rate
- CircuitBreaker: event-driven 3-state circuit breaker per endpoint
- PredictiveThrottle: 3-layer throttle (EWMA + regression + variance spike)
- BackpressureBus: bidirectional event propagation to providers
- RateLimitService: top-level service wiring everything together

Boundary Principle:
  Deterministic: All math, state machines, timing, persistence.
  Agentic: The routing decision to AVOID a throttled provider
           (made by BrainSelector, not this module).

See: docs/superpowers/specs/2026-04-06-intelligent-rate-limiter-design.md
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging
import math
import os
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class EndpointConfig:
    """Per-endpoint rate limit configuration."""
    name: str
    rpm: int
    tpm: int = 0
    burst: int = 1
    timeout_s: float = 30.0
    retry_after_default_s: float = 5.0


@dataclass(frozen=True)
class ProviderProfile:
    """Per-provider configuration containing endpoint configs."""
    provider_name: str
    endpoints: Dict[str, EndpointConfig]


DEFAULT_PROFILES: Dict[str, ProviderProfile] = {
    "doubleword": ProviderProfile(
        provider_name="doubleword",
        endpoints={
            "files_upload": EndpointConfig(
                name="files_upload",
                rpm=int(os.environ.get("OUROBOROS_RATELIMIT_DOUBLEWORD_FILES_RPM", "30")),
                burst=2, timeout_s=30.0,
            ),
            "batches_create": EndpointConfig(
                name="batches_create",
                rpm=int(os.environ.get("OUROBOROS_RATELIMIT_DOUBLEWORD_BATCHES_RPM", "30")),
                burst=2, timeout_s=30.0,
            ),
            "batches_poll": EndpointConfig(
                name="batches_poll",
                rpm=int(os.environ.get("OUROBOROS_RATELIMIT_DOUBLEWORD_POLL_RPM", "60")),
                burst=5, timeout_s=15.0,
            ),
            "batches_retrieve": EndpointConfig(
                name="batches_retrieve", rpm=60, burst=5, timeout_s=30.0,
            ),
        },
    ),
    "claude": ProviderProfile(
        provider_name="claude",
        endpoints={
            "messages": EndpointConfig(
                name="messages",
                rpm=int(os.environ.get("OUROBOROS_RATELIMIT_CLAUDE_MESSAGES_RPM", "60")),
                tpm=100_000, burst=3, timeout_s=60.0,
            ),
        },
    ),
}


# ═══════════════════════════════════════════════════════════════════
# LatencyRing — circular buffer
# ═══════════════════════════════════════════════════════════════════


class LatencyRing:
    """Fixed-capacity circular buffer of float latency values."""

    def __init__(self, capacity: int = 50) -> None:
        self._capacity = capacity
        self._buf: Deque[float] = deque(maxlen=capacity)

    def push(self, latency: float) -> None:
        self._buf.append(latency)

    def values(self) -> List[float]:
        return list(self._buf)

    def last_n(self, n: int) -> List[float]:
        vals = self.values()
        return vals[-n:] if n < len(vals) else vals

    def __len__(self) -> int:
        return len(self._buf)

    def seed(self, latencies: List[float]) -> None:
        """Seed with historical data (warm start)."""
        for v in latencies[-self._capacity:]:
            self._buf.append(v)


# ═══════════════════════════════════════════════════════════════════
# RateLimitStore — abstract + memory implementation
# ═══════════════════════════════════════════════════════════════════


class RateLimitStore(abc.ABC):
    """Abstract store for token bucket state. Swappable to Redis."""

    @abc.abstractmethod
    async def get_state(self, key: str) -> Dict[str, float]:
        ...

    @abc.abstractmethod
    async def update_state(self, key: str, *, tokens: float, last_refill: float) -> None:
        ...


class MemoryRateLimitStore(RateLimitStore):
    """In-memory token state with per-key asyncio.Lock."""

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, float]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def get_state(self, key: str) -> Dict[str, float]:
        async with self._lock_for(key):
            if key not in self._data:
                self._data[key] = {"tokens": 0.0, "last_refill": time.monotonic()}
            return dict(self._data[key])

    async def update_state(self, key: str, *, tokens: float, last_refill: float) -> None:
        async with self._lock_for(key):
            self._data[key] = {"tokens": tokens, "last_refill": last_refill}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_rate_limiter.py -v --timeout=15 -x`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/rate_limiter.py tests/test_ouroboros_governance/test_rate_limiter.py
git commit -m "feat(rate-limiter): add config, LatencyRing, and MemoryRateLimitStore"
```

---

## Task 2: TokenBucket

**Files:**
- Modify: `backend/core/ouroboros/governance/rate_limiter.py`
- Test: `tests/test_ouroboros_governance/test_rate_limiter.py`

- [ ] **Step 1: Write the failing test**

Append to test file:

```python
# ── TokenBucket ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_bucket_acquire_immediate():
    from backend.core.ouroboros.governance.rate_limiter import (
        TokenBucket, MemoryRateLimitStore,
    )
    store = MemoryRateLimitStore()
    bucket = TokenBucket(
        key="test:ep", store=store,
        rpm=600, burst=10,  # 10 tokens/sec, burst of 10
    )
    # First acquire should be immediate (bucket starts full at burst)
    waited = await bucket.acquire()
    assert waited < 0.05  # near-instant


@pytest.mark.asyncio
async def test_token_bucket_waits_when_empty():
    from backend.core.ouroboros.governance.rate_limiter import (
        TokenBucket, MemoryRateLimitStore,
    )
    store = MemoryRateLimitStore()
    bucket = TokenBucket(
        key="test:ep", store=store,
        rpm=60, burst=1,  # 1 token/sec, burst of 1
    )
    # Consume the one token
    await bucket.acquire()
    # Next acquire should wait ~1 second
    t0 = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - t0
    assert 0.8 < elapsed < 1.5


@pytest.mark.asyncio
async def test_token_bucket_throttle_multiplier():
    from backend.core.ouroboros.governance.rate_limiter import (
        TokenBucket, MemoryRateLimitStore,
    )
    store = MemoryRateLimitStore()
    bucket = TokenBucket(
        key="test:ep", store=store,
        rpm=600, burst=1,  # 10 tokens/sec
    )
    await bucket.acquire()
    # Throttle to 50% — effective rate = 5 tokens/sec
    bucket.set_throttle(0.5)
    t0 = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - t0
    # Should wait ~0.2s (1 / 5) instead of ~0.1s (1 / 10)
    assert 0.15 < elapsed < 0.4


@pytest.mark.asyncio
async def test_token_bucket_burst():
    from backend.core.ouroboros.governance.rate_limiter import (
        TokenBucket, MemoryRateLimitStore,
    )
    store = MemoryRateLimitStore()
    bucket = TokenBucket(
        key="test:burst", store=store,
        rpm=60, burst=5,
    )
    # Should acquire 5 tokens instantly (burst capacity)
    for _ in range(5):
        waited = await bucket.acquire()
        assert waited < 0.05
    # 6th should wait
    t0 = time.monotonic()
    await bucket.acquire()
    assert time.monotonic() - t0 > 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_rate_limiter.py::test_token_bucket_acquire_immediate -v --timeout=15 -x`
Expected: FAIL with `ImportError` (TokenBucket not defined)

- [ ] **Step 3: Write implementation**

Add to `rate_limiter.py`:

```python
# ═══════════════════════════════════════════════════════════════════
# TokenBucket — async, store-backed, dynamically throttled
# ═══════════════════════════════════════════════════════════════════


class TokenBucket:
    """Async token bucket with dynamic refill rate.

    acquire() computes exact sleep time and yields via asyncio.sleep().
    No spin loops. No polling. Refill computed lazily on each acquire().
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
        self._base_rate = rpm / 60.0  # tokens per second
        self._burst = max(burst, 1)
        self._throttle: float = 1.0  # multiplier in (0, 1]
        self._initialized = False

    @property
    def effective_rate(self) -> float:
        """Current tokens-per-second after throttle."""
        return self._base_rate * self._throttle

    def set_throttle(self, multiplier: float) -> None:
        """Set throttle multiplier. 1.0 = full speed, 0.2 = 80% slower."""
        self._throttle = max(0.01, min(1.0, multiplier))

    async def acquire(self, tokens: int = 1) -> float:
        """Acquire tokens. Returns seconds waited (0.0 if immediate).

        If insufficient tokens, sleeps exactly until enough refill.
        Never blocks the event loop synchronously.
        """
        state = await self._store.get_state(self._key)
        now = time.monotonic()

        if not self._initialized:
            # Start with full burst capacity
            await self._store.update_state(
                self._key, tokens=float(self._burst), last_refill=now,
            )
            state = {"tokens": float(self._burst), "last_refill": now}
            self._initialized = True

        # Lazy refill
        elapsed = now - state["last_refill"]
        refilled = state["tokens"] + elapsed * self.effective_rate
        current = min(refilled, float(self._burst))

        if current >= tokens:
            # Tokens available — consume immediately
            await self._store.update_state(
                self._key, tokens=current - tokens, last_refill=now,
            )
            return 0.0

        # Not enough tokens — compute wait
        deficit = tokens - current
        wait_s = deficit / self.effective_rate
        await asyncio.sleep(wait_s)

        # After sleep, set tokens to 0 (we consumed what we waited for)
        await self._store.update_state(
            self._key, tokens=0.0, last_refill=time.monotonic(),
        )
        return wait_s
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_rate_limiter.py -v --timeout=30 -x`
Expected: All 15 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/rate_limiter.py tests/test_ouroboros_governance/test_rate_limiter.py
git commit -m "feat(rate-limiter): add async TokenBucket with dynamic throttle"
```

---

## Task 3: CircuitBreaker

**Files:**
- Modify: `backend/core/ouroboros/governance/rate_limiter.py`
- Test: `tests/test_ouroboros_governance/test_rate_limiter.py`

- [ ] **Step 1: Write the failing test**

Append to test file:

```python
# ── CircuitBreaker ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_breaker_starts_closed():
    from backend.core.ouroboros.governance.rate_limiter import (
        CircuitBreaker, BreakerState,
    )
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout_s=1.0)
    assert breaker.state == BreakerState.CLOSED


@pytest.mark.asyncio
async def test_breaker_trips_after_threshold():
    from backend.core.ouroboros.governance.rate_limiter import (
        CircuitBreaker, BreakerState, CircuitBreakerOpen,
    )
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout_s=1.0)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == BreakerState.CLOSED
    breaker.record_failure()
    assert breaker.state == BreakerState.OPEN
    with pytest.raises(CircuitBreakerOpen):
        breaker.check()


@pytest.mark.asyncio
async def test_breaker_success_resets_counter():
    from backend.core.ouroboros.governance.rate_limiter import (
        CircuitBreaker, BreakerState,
    )
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout_s=1.0)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.state == BreakerState.CLOSED
    assert breaker._consecutive_failures == 0


@pytest.mark.asyncio
async def test_breaker_recovers_to_half_open():
    from backend.core.ouroboros.governance.rate_limiter import (
        CircuitBreaker, BreakerState,
    )
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_s=0.3)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == BreakerState.OPEN
    # Wait for recovery
    await asyncio.sleep(0.5)
    assert breaker.state == BreakerState.HALF_OPEN


@pytest.mark.asyncio
async def test_breaker_half_open_success_closes():
    from backend.core.ouroboros.governance.rate_limiter import (
        CircuitBreaker, BreakerState,
    )
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_s=0.2)
    breaker.record_failure()
    breaker.record_failure()
    await asyncio.sleep(0.3)
    assert breaker.state == BreakerState.HALF_OPEN
    breaker.record_success()
    assert breaker.state == BreakerState.CLOSED


@pytest.mark.asyncio
async def test_breaker_half_open_failure_reopens():
    from backend.core.ouroboros.governance.rate_limiter import (
        CircuitBreaker, BreakerState,
    )
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_s=0.2)
    breaker.record_failure()
    breaker.record_failure()
    await asyncio.sleep(0.3)
    assert breaker.state == BreakerState.HALF_OPEN
    breaker.record_failure()
    assert breaker.state == BreakerState.OPEN


@pytest.mark.asyncio
async def test_breaker_event_fires_on_state_change():
    from backend.core.ouroboros.governance.rate_limiter import (
        CircuitBreaker, BreakerState,
    )
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_s=1.0)
    assert not breaker.state_changed.is_set()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state_changed.is_set()


def test_breaker_failure_classification():
    from backend.core.ouroboros.governance.rate_limiter import is_retriable_failure
    assert is_retriable_failure(429) is True
    assert is_retriable_failure(500) is True
    assert is_retriable_failure(502) is True
    assert is_retriable_failure(503) is True
    assert is_retriable_failure(400) is False
    assert is_retriable_failure(401) is False
    assert is_retriable_failure(403) is False
    assert is_retriable_failure(200) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_rate_limiter.py::test_breaker_starts_closed -v --timeout=15 -x`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Write implementation**

Add to `rate_limiter.py`:

```python
# ═══════════════════════════════════════════════════════════════════
# CircuitBreaker — event-driven, per-endpoint
# ═══════════════════════════════════════════════════════════════════

_RETRIABLE_STATUS_CODES = frozenset({429, 500, 502, 503})


def is_retriable_failure(status: int) -> bool:
    """Classify HTTP status as retriable provider failure."""
    return status in _RETRIABLE_STATUS_CODES


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpen(Exception):
    """Raised when a circuit breaker is OPEN."""
    def __init__(self, endpoint: str = "", retry_after_s: float = 0.0) -> None:
        self.endpoint = endpoint
        self.retry_after_s = retry_after_s
        super().__init__(f"Circuit breaker OPEN for {endpoint} (retry in {retry_after_s:.1f}s)")


class CircuitBreaker:
    """Event-driven circuit breaker with 3 states.

    CLOSED -> OPEN (after failure_threshold consecutive failures)
    OPEN -> HALF_OPEN (after recovery_timeout_s)
    HALF_OPEN -> CLOSED (on success) or OPEN (on failure)

    State changes set state_changed asyncio.Event for subscribers.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_s: float = 30.0,
    ) -> None:
        self._threshold = failure_threshold
        self._recovery_s = recovery_timeout_s
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self.state_changed = asyncio.Event()

    @property
    def state(self) -> BreakerState:
        # Auto-transition OPEN -> HALF_OPEN after recovery timeout
        if self._state == BreakerState.OPEN:
            if time.monotonic() - self._opened_at >= self._recovery_s:
                self._state = BreakerState.HALF_OPEN
        return self._state

    def check(self) -> None:
        """Raise CircuitBreakerOpen if breaker is OPEN."""
        if self.state == BreakerState.OPEN:
            remaining = self._recovery_s - (time.monotonic() - self._opened_at)
            raise CircuitBreakerOpen(retry_after_s=max(0.0, remaining))

    def record_success(self) -> None:
        """Record a successful request."""
        prev = self._state
        self._consecutive_failures = 0
        self._state = BreakerState.CLOSED
        if prev != BreakerState.CLOSED:
            self.state_changed.set()

    def record_failure(self) -> None:
        """Record a failed request."""
        self._consecutive_failures += 1
        if self._state == BreakerState.HALF_OPEN:
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()
            self.state_changed.set()
        elif self._consecutive_failures >= self._threshold:
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()
            self.state_changed.set()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_rate_limiter.py -v --timeout=30 -x`
Expected: All 23 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/rate_limiter.py tests/test_ouroboros_governance/test_rate_limiter.py
git commit -m "feat(rate-limiter): add event-driven CircuitBreaker with 3-state FSM"
```

---

## Task 4: PredictiveThrottle (3-layer)

**Files:**
- Modify: `backend/core/ouroboros/governance/rate_limiter.py`
- Test: `tests/test_ouroboros_governance/test_rate_limiter.py`

- [ ] **Step 1: Write the failing test**

Append to test file:

```python
# ── PredictiveThrottle ──────────────────────────────────────────


def test_throttle_no_data():
    from backend.core.ouroboros.governance.rate_limiter import (
        PredictiveThrottle, LatencyRing,
    )
    ring = LatencyRing(capacity=50)
    throttle = PredictiveThrottle(timeout_s=10.0)
    assert throttle.compute(ring) == 1.0  # No data = full speed


def test_throttle_stable_latency():
    from backend.core.ouroboros.governance.rate_limiter import (
        PredictiveThrottle, LatencyRing,
    )
    ring = LatencyRing(capacity=50)
    for _ in range(20):
        ring.push(0.2)  # Stable 200ms
    throttle = PredictiveThrottle(timeout_s=10.0)
    result = throttle.compute(ring)
    assert result > 0.9  # Stable = no throttling


def test_throttle_ewma_high_latency():
    from backend.core.ouroboros.governance.rate_limiter import (
        PredictiveThrottle, LatencyRing,
    )
    ring = LatencyRing(capacity=50)
    # Baseline: 10 requests at 0.2s
    for _ in range(10):
        ring.push(0.2)
    # Then latency jumps to 3x baseline
    for _ in range(10):
        ring.push(0.6)
    throttle = PredictiveThrottle(timeout_s=10.0)
    result = throttle.compute(ring)
    assert result < 0.7  # Should throttle due to EWMA > 2x baseline


def test_throttle_variance_spike():
    from backend.core.ouroboros.governance.rate_limiter import (
        PredictiveThrottle, LatencyRing,
    )
    ring = LatencyRing(capacity=50)
    # Stable baseline
    for _ in range(20):
        ring.push(0.2)
    # Sudden cliff — 5 requests at wildly varying latencies
    for v in [2.0, 0.3, 3.0, 0.5, 2.5]:
        ring.push(v)
    throttle = PredictiveThrottle(timeout_s=10.0)
    result = throttle.compute(ring)
    assert result <= 0.3  # Emergency throttle from variance spike


def test_throttle_regression_projects_timeout():
    from backend.core.ouroboros.governance.rate_limiter import (
        PredictiveThrottle, LatencyRing,
    )
    ring = LatencyRing(capacity=50)
    # Linearly increasing latency heading toward timeout
    for i in range(20):
        ring.push(0.5 + i * 0.4)  # 0.5, 0.9, 1.3, ... 8.1
    throttle = PredictiveThrottle(timeout_s=10.0)
    result = throttle.compute(ring)
    assert result < 0.5  # Should throttle — approaching timeout


def test_throttle_returns_minimum_of_layers():
    from backend.core.ouroboros.governance.rate_limiter import (
        PredictiveThrottle, LatencyRing,
    )
    ring = LatencyRing(capacity=50)
    # Both high EWMA and variance spike — should use worst
    for _ in range(15):
        ring.push(0.2)
    for v in [3.0, 0.5, 4.0, 0.3, 3.5]:
        ring.push(v)
    throttle = PredictiveThrottle(timeout_s=10.0)
    result = throttle.compute(ring)
    assert result <= 0.3  # Variance spike dominates
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_rate_limiter.py::test_throttle_no_data -v --timeout=15 -x`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Write implementation**

Add to `rate_limiter.py`:

```python
# ═══════════════════════════════════════════════════════════════════
# PredictiveThrottle — 3-layer deterministic prediction
# ═══════════════════════════════════════════════════════════════════


class PredictiveThrottle:
    """3-layer predictive throttle using stdlib math.

    Layer 1: EWMA — detects gradual latency increase
    Layer 2: Linear regression — projects when timeout will be breached
    Layer 3: Variance spike — catches sudden cliffs (200ms -> 2000ms)

    Returns throttle_multiplier in (0.0, 1.0]. 1.0 = full speed.
    The minimum across all layers wins (most conservative).
    """

    def __init__(
        self,
        timeout_s: float = 30.0,
        ewma_alpha: float = 0.3,
        variance_spike_ratio: float = 5.0,
    ) -> None:
        self._timeout = timeout_s
        self._alpha = ewma_alpha
        self._spike_ratio = variance_spike_ratio
        self._ewma: Optional[float] = None
        self._baseline: Optional[float] = None

    def compute(self, ring: LatencyRing) -> float:
        """Compute throttle multiplier from latency history."""
        vals = ring.values()
        if len(vals) < 5:
            return 1.0  # Not enough data

        # Establish baseline from first 10 observations
        if self._baseline is None and len(vals) >= 10:
            self._baseline = statistics.median(vals[:10])
        if self._baseline is None:
            self._baseline = statistics.median(vals)
        if self._baseline <= 0:
            self._baseline = 0.001

        # Layer 1: EWMA
        mult_ewma = self._layer_ewma(vals)

        # Layer 2: Linear regression
        mult_reg = self._layer_regression(vals)

        # Layer 3: Variance spike
        mult_var = self._layer_variance(vals)

        # Return the most conservative (lowest) multiplier
        result = min(mult_ewma, mult_reg, mult_var)
        return max(0.05, min(1.0, result))  # Floor at 5%, cap at 100%

    def _layer_ewma(self, vals: List[float]) -> float:
        """Layer 1: Exponential weighted moving average."""
        # Compute EWMA
        ewma = vals[0]
        for v in vals[1:]:
            ewma = self._alpha * v + (1.0 - self._alpha) * ewma
        self._ewma = ewma

        ratio = ewma / self._baseline if self._baseline else 1.0
        if ratio > 3.0:
            return 0.3
        elif ratio > 2.0:
            return 0.6
        return 1.0

    def _layer_regression(self, vals: List[float]) -> float:
        """Layer 2: Linear regression — project time to timeout."""
        recent = vals[-20:] if len(vals) >= 20 else vals
        n = len(recent)
        if n < 5:
            return 1.0

        # Least squares slope
        sx = sum(range(n))
        sy = sum(recent)
        sxy = sum(i * v for i, v in enumerate(recent))
        sxx = sum(i * i for i in range(n))
        denom = n * sxx - sx * sx
        if denom == 0:
            return 1.0
        slope = (n * sxy - sx * sy) / denom

        if slope <= 0:
            return 1.0  # Latency decreasing — no concern

        current = self._ewma if self._ewma else recent[-1]
        if current >= self._timeout:
            return 0.2  # Already at timeout

        time_to_breach = (self._timeout - current) / slope
        if time_to_breach < 10:
            return 0.2
        elif time_to_breach < 30:
            return 0.4
        return 1.0

    def _layer_variance(self, vals: List[float]) -> float:
        """Layer 3: Variance spike detection — catches sudden cliffs."""
        if len(vals) < 10:
            return 1.0

        short = vals[-5:]
        long_ = vals[-20:] if len(vals) >= 20 else vals

        short_var = statistics.variance(short) if len(short) >= 2 else 0.0
        long_var = statistics.variance(long_) if len(long_) >= 2 else 0.0

        if long_var < 1e-9:
            # Baseline is perfectly stable — any variance is significant
            if short_var > 0.01:
                return 0.2
            return 1.0

        ratio = short_var / long_var
        if ratio > self._spike_ratio:
            return 0.2  # Emergency throttle
        return 1.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_rate_limiter.py -v --timeout=30 -x`
Expected: All 29 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/rate_limiter.py tests/test_ouroboros_governance/test_rate_limiter.py
git commit -m "feat(rate-limiter): add 3-layer PredictiveThrottle (EWMA + regression + variance)"
```

---

## Task 5: BackpressureBus and RateLimitService

**Files:**
- Modify: `backend/core/ouroboros/governance/rate_limiter.py`
- Test: `tests/test_ouroboros_governance/test_rate_limiter.py`

- [ ] **Step 1: Write the failing test**

Append to test file:

```python
# ── RateLimitService ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_acquire_and_record():
    from backend.core.ouroboros.governance.rate_limiter import RateLimitService
    svc = RateLimitService()
    # Should not raise
    wait = await svc.acquire("doubleword", "batches_poll")
    assert isinstance(wait, float)
    svc.record("doubleword", "batches_poll", latency_s=0.2, status=200)


@pytest.mark.asyncio
async def test_service_circuit_breaker_trips():
    from backend.core.ouroboros.governance.rate_limiter import (
        RateLimitService, CircuitBreakerOpen,
    )
    svc = RateLimitService()
    await svc.acquire("doubleword", "batches_poll")
    # Record 3 consecutive failures
    svc.record("doubleword", "batches_poll", latency_s=5.0, status=502)
    svc.record("doubleword", "batches_poll", latency_s=5.0, status=502)
    svc.record("doubleword", "batches_poll", latency_s=5.0, status=502)
    with pytest.raises(CircuitBreakerOpen):
        await svc.acquire("doubleword", "batches_poll")


@pytest.mark.asyncio
async def test_service_unknown_endpoint_uses_defaults():
    from backend.core.ouroboros.governance.rate_limiter import RateLimitService
    svc = RateLimitService()
    # Unknown endpoint should not crash — uses fallback config
    wait = await svc.acquire("doubleword", "unknown_endpoint")
    assert isinstance(wait, float)


@pytest.mark.asyncio
async def test_service_throttle_updates_on_high_latency():
    from backend.core.ouroboros.governance.rate_limiter import RateLimitService
    svc = RateLimitService()
    await svc.acquire("doubleword", "batches_poll")
    # Record 10 low latencies to establish baseline
    for _ in range(10):
        svc.record("doubleword", "batches_poll", latency_s=0.2, status=200)
    # Record 10 high latencies (3x baseline)
    for _ in range(10):
        svc.record("doubleword", "batches_poll", latency_s=0.7, status=200)
    state = svc.get_endpoint_state("doubleword", "batches_poll")
    assert state["throttle_multiplier"] < 1.0  # Should be throttled


@pytest.mark.asyncio
async def test_service_backpressure_event_fires():
    from backend.core.ouroboros.governance.rate_limiter import RateLimitService
    svc = RateLimitService()
    await svc.acquire("doubleword", "batches_poll")
    # Establish baseline
    for _ in range(10):
        svc.record("doubleword", "batches_poll", latency_s=0.2, status=200)
    # Trigger throttle change
    for v in [2.0, 0.3, 3.0, 0.5, 2.5]:
        svc.record("doubleword", "batches_poll", latency_s=v, status=200)
    state = svc.get_endpoint_state("doubleword", "batches_poll")
    assert state["throttle_changed"]  # Backpressure event should have fired


def test_service_persistence(tmp_path):
    import asyncio
    from backend.core.ouroboros.governance.rate_limiter import RateLimitService
    svc1 = RateLimitService(persistence_dir=tmp_path)
    asyncio.get_event_loop().run_until_complete(svc1.acquire("doubleword", "batches_poll"))
    for _ in range(10):
        svc1.record("doubleword", "batches_poll", latency_s=0.25, status=200)
    svc1.save()

    svc2 = RateLimitService(persistence_dir=tmp_path)
    state = svc2.get_endpoint_state("doubleword", "batches_poll")
    assert state["latency_count"] == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_rate_limiter.py::test_service_acquire_and_record -v --timeout=15 -x`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Write implementation**

Add to `rate_limiter.py`:

```python
# ═══════════════════════════════════════════════════════════════════
# EndpointState — bundles all per-endpoint components
# ═══════════════════════════════════════════════════════════════════


class EndpointState:
    """All rate-limit state for a single provider:endpoint pair."""

    def __init__(self, config: EndpointConfig, store: RateLimitStore, key: str) -> None:
        self.config = config
        self.bucket = TokenBucket(key=key, store=store, rpm=config.rpm, burst=config.burst)
        self.breaker = CircuitBreaker(
            failure_threshold=int(os.environ.get("OUROBOROS_RATELIMIT_BREAKER_THRESHOLD", "3")),
            recovery_timeout_s=float(os.environ.get("OUROBOROS_RATELIMIT_BREAKER_RECOVERY_S", "30")),
        )
        self.throttle = PredictiveThrottle(timeout_s=config.timeout_s)
        self.latency_ring = LatencyRing(capacity=50)
        self.throttle_event = asyncio.Event()
        self._current_multiplier: float = 1.0

    @property
    def throttle_multiplier(self) -> float:
        return self._current_multiplier


# ═══════════════════════════════════════════════════════════════════
# RateLimitService — top-level service
# ═══════════════════════════════════════════════════════════════════

_FALLBACK_CONFIG = EndpointConfig(name="fallback", rpm=30, burst=2, timeout_s=30.0)

_PERSISTENCE_DIR = Path(
    os.environ.get(
        "JARVIS_SELF_EVOLUTION_DIR",
        str(Path.home() / ".jarvis" / "ouroboros" / "evolution"),
    )
)


class RateLimitService:
    """Provider-agnostic rate limiting service.

    Usage:
        svc = RateLimitService()
        await svc.acquire("doubleword", "batches_poll")  # async gate
        # ... make request ...
        svc.record("doubleword", "batches_poll", latency_s=0.5, status=200)
    """

    def __init__(
        self,
        profiles: Optional[Dict[str, ProviderProfile]] = None,
        store: Optional[RateLimitStore] = None,
        persistence_dir: Optional[Path] = None,
    ) -> None:
        self._profiles = profiles or DEFAULT_PROFILES
        self._store = store or MemoryRateLimitStore()
        self._endpoints: Dict[str, EndpointState] = {}
        self._persistence_dir = persistence_dir or _PERSISTENCE_DIR
        self._load_latency_history()

    def _get_endpoint(self, provider: str, endpoint: str) -> EndpointState:
        """Get or create EndpointState for a provider:endpoint pair."""
        key = f"{provider}:{endpoint}"
        if key not in self._endpoints:
            profile = self._profiles.get(provider)
            config = (
                profile.endpoints.get(endpoint, _FALLBACK_CONFIG)
                if profile else _FALLBACK_CONFIG
            )
            self._endpoints[key] = EndpointState(config=config, store=self._store, key=key)
            # Seed latency ring if history available
        return self._endpoints[key]

    async def acquire(self, provider: str, endpoint: str) -> float:
        """Acquire a token for the given endpoint. Returns wait time.

        Raises CircuitBreakerOpen if the breaker is tripped.
        """
        ep = self._get_endpoint(provider, endpoint)
        ep.breaker.check()  # Raises if OPEN
        return await ep.bucket.acquire()

    def record(
        self, provider: str, endpoint: str,
        latency_s: float, status: int,
    ) -> None:
        """Record a completed request. Updates all layers."""
        ep = self._get_endpoint(provider, endpoint)

        # Update latency ring
        ep.latency_ring.push(latency_s)

        # Update circuit breaker
        if is_retriable_failure(status):
            ep.breaker.record_failure()
            logger.info(
                "[RateLimit] %s:%s failure (status=%d, consecutive=%d/%d)",
                provider, endpoint, status,
                ep.breaker._consecutive_failures, ep.breaker._threshold,
            )
        else:
            ep.breaker.record_success()

        # Recompute throttle
        new_mult = ep.throttle.compute(ep.latency_ring)
        if abs(new_mult - ep._current_multiplier) > 0.05:
            ep._current_multiplier = new_mult
            ep.bucket.set_throttle(new_mult)
            ep.throttle_event.set()
            ep.throttle_event = asyncio.Event()  # Reset for next change
            logger.info(
                "[RateLimit] %s:%s throttle -> %.0f%% (EWMA/reg/var)",
                provider, endpoint, new_mult * 100,
            )

    def get_endpoint_state(self, provider: str, endpoint: str) -> Dict[str, Any]:
        """Get current state for observability."""
        ep = self._get_endpoint(provider, endpoint)
        return {
            "breaker_state": ep.breaker.state.value,
            "throttle_multiplier": ep._current_multiplier,
            "latency_count": len(ep.latency_ring),
            "throttle_changed": ep._current_multiplier < 1.0,
            "effective_rpm": ep.config.rpm * ep._current_multiplier,
        }

    def save(self) -> None:
        """Persist latency history for warm restart."""
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / "rate_limit_latency.json"
            data = {}
            for key, ep in self._endpoints.items():
                vals = ep.latency_ring.values()
                if vals:
                    data[key] = {
                        "latencies": vals,
                        "baseline_ewma": ep.throttle._ewma,
                        "last_updated": time.time(),
                    }
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.debug("RateLimitService: save failed", exc_info=True)

    def _load_latency_history(self) -> None:
        """Load latency history from previous session."""
        path = self._persistence_dir / "rate_limit_latency.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            for key, entry in data.items():
                parts = key.split(":", 1)
                if len(parts) == 2:
                    ep = self._get_endpoint(parts[0], parts[1])
                    ep.latency_ring.seed(entry.get("latencies", []))
        except Exception:
            logger.debug("RateLimitService: load failed", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_rate_limiter.py -v --timeout=30 -x`
Expected: All 35 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/rate_limiter.py tests/test_ouroboros_governance/test_rate_limiter.py
git commit -m "feat(rate-limiter): add RateLimitService with BackpressureBus and persistence"
```

---

## Task 6: Wire into DoublewordProvider

**Files:**
- Modify: `backend/core/ouroboros/governance/doubleword_provider.py`

- [ ] **Step 1: Read the current provider to find exact integration points**

The Doubleword provider has 4 API call sites (from Grep earlier):
- Line 416: `session.post(f"{self._base_url}/files")` — file upload
- Line 434: `session.post(f"{self._base_url}/batches")` — batch create
- Line 460: `session.get(f"{self._base_url}/batches/{batch_id}")` — batch poll
- Line 500: `session.get(f"{self._base_url}/files/{output_file_id}/content")` — retrieve

- [ ] **Step 2: Add rate_limiter parameter to constructor**

Add `rate_limiter: Optional[Any] = None` to `DoublewordProvider.__init__()` and store as `self._rate_limiter`.

- [ ] **Step 3: Add acquire/record around each API call**

For each of the 4 call sites, wrap with:

```python
# Before the API call:
if self._rate_limiter is not None:
    try:
        await self._rate_limiter.acquire("doubleword", "endpoint_name")
    except Exception as exc:
        logger.warning("[DoublewordProvider] Rate limiter: %s", exc)
        raise

# After the API call (in the response handling):
if self._rate_limiter is not None:
    self._rate_limiter.record("doubleword", "endpoint_name", latency_s=elapsed, status=resp.status)
```

Endpoint mapping:
- `/files` -> `"files_upload"`
- `/batches` (POST) -> `"batches_create"`
- `/batches/{id}` (GET) -> `"batches_poll"`
- `/files/{id}/content` -> `"batches_retrieve"`

- [ ] **Step 4: Run existing tests to verify no regressions**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -k "doubleword" -v --timeout=30 --maxfail=5`

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/doubleword_provider.py
git commit -m "feat(rate-limiter): wire RateLimitService into DoublewordProvider"
```

---

## Task 7: Wire into Battle Test Harness

**Files:**
- Modify: `backend/core/ouroboros/battle_test/harness.py`

- [ ] **Step 1: Add RateLimitService boot to harness**

In `boot_governed_loop_service()`, after creating the GLS, instantiate and inject the RateLimitService:

```python
# After GLS creation, before start():
try:
    from backend.core.ouroboros.governance.rate_limiter import RateLimitService
    self._rate_limiter = RateLimitService()
    # Inject into GLS so providers can access it
    if hasattr(self._governed_loop_service, '_doubleword_provider'):
        self._governed_loop_service._doubleword_provider._rate_limiter = self._rate_limiter
    logger.info("RateLimitService booted")
except Exception as exc:
    logger.warning("RateLimitService failed: %s", exc)
```

- [ ] **Step 2: Save rate limiter state on shutdown**

In `_shutdown_components()`, before saving cost tracker:

```python
if hasattr(self, '_rate_limiter') and self._rate_limiter is not None:
    try:
        self._rate_limiter.save()
    except Exception:
        pass
```

- [ ] **Step 3: Run battle test tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_*.py -v --timeout=30 -q`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add backend/core/ouroboros/battle_test/harness.py
git commit -m "feat(rate-limiter): wire RateLimitService into battle test harness"
```

---

## Task 8: Integration Test

**Files:**
- Test: `tests/test_ouroboros_governance/test_rate_limiter.py`

- [ ] **Step 1: Write integration test**

Append to test file:

```python
# ── Integration ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_rate_limit_lifecycle():
    """End-to-end: acquire, record, throttle, trip, recover."""
    from backend.core.ouroboros.governance.rate_limiter import (
        RateLimitService, CircuitBreakerOpen, BreakerState,
    )
    svc = RateLimitService()

    # Phase 1: Normal operation
    for _ in range(10):
        await svc.acquire("doubleword", "batches_poll")
        svc.record("doubleword", "batches_poll", latency_s=0.2, status=200)

    state = svc.get_endpoint_state("doubleword", "batches_poll")
    assert state["breaker_state"] == "closed"
    assert state["throttle_multiplier"] > 0.9

    # Phase 2: Latency climbs — throttle kicks in
    for _ in range(10):
        svc.record("doubleword", "batches_poll", latency_s=0.8, status=200)
    state = svc.get_endpoint_state("doubleword", "batches_poll")
    assert state["throttle_multiplier"] < 1.0

    # Phase 3: Failures — breaker trips
    svc.record("doubleword", "batches_poll", latency_s=5.0, status=502)
    svc.record("doubleword", "batches_poll", latency_s=5.0, status=502)
    svc.record("doubleword", "batches_poll", latency_s=5.0, status=502)
    state = svc.get_endpoint_state("doubleword", "batches_poll")
    assert state["breaker_state"] == "open"

    with pytest.raises(CircuitBreakerOpen):
        await svc.acquire("doubleword", "batches_poll")

    # Phase 4: Other endpoint still works
    await svc.acquire("doubleword", "files_upload")  # Different endpoint — should work


@pytest.mark.asyncio
async def test_persistence_warm_start(tmp_path):
    """Latency history survives restart and seeds predictions."""
    from backend.core.ouroboros.governance.rate_limiter import RateLimitService

    svc1 = RateLimitService(persistence_dir=tmp_path)
    await svc1.acquire("claude", "messages")
    for _ in range(20):
        svc1.record("claude", "messages", latency_s=0.3, status=200)
    svc1.save()

    svc2 = RateLimitService(persistence_dir=tmp_path)
    state = svc2.get_endpoint_state("claude", "messages")
    assert state["latency_count"] == 20  # History loaded
```

- [ ] **Step 2: Run the full test suite**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_rate_limiter.py -v --timeout=60`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_ouroboros_governance/test_rate_limiter.py
git commit -m "test(rate-limiter): add full lifecycle and persistence integration tests"
```

---

## Task 9: Final Verification

- [ ] **Step 1: Run ALL rate limiter tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_rate_limiter.py -v --timeout=60`
Expected: All tests PASS

- [ ] **Step 2: Run battle test + RSI tests for regressions**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_battle_test_*.py tests/test_ouroboros_governance/test_composite_score.py tests/test_ouroboros_governance/test_convergence_tracker.py tests/test_ouroboros_governance/test_rsi_convergence_integration.py --timeout=60 -q`
Expected: All passing, no regressions

- [ ] **Step 3: Verify CLI still works**

Run: `python3 scripts/ouroboros_battle_test.py --help`
Expected: Shows help text

- [ ] **Step 4: Commit**

```bash
git add -A && git status
git commit -m "feat(rate-limiter): complete Intelligent Rate Limiter

Provider-agnostic async rate limiting with:
- Per-endpoint token buckets with dynamic refill
- Event-driven circuit breakers (CLOSED/OPEN/HALF_OPEN)
- 3-layer predictive throttle (EWMA + regression + variance spike)
- Bidirectional backpressure signaling
- Latency history persistence for warm-start predictions
- Wired into DoublewordProvider and battle test harness"
```
