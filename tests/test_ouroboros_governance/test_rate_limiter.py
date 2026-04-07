# tests/test_ouroboros_governance/test_rate_limiter.py
"""Tests for the intelligent rate limiter core components."""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ouroboros.governance.rate_limiter import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerOpen,
    EndpointConfig,
    LatencyRing,
    MemoryRateLimitStore,
    PredictiveThrottle,
    ProviderProfile,
    TokenBucket,
    DEFAULT_PROFILES,
)


# ---------------------------------------------------------------------------
# Component 1: Config
# ---------------------------------------------------------------------------


class TestEndpointConfig:
    def test_defaults(self):
        """EndpointConfig has correct defaults for optional fields."""
        cfg = EndpointConfig(name="test", rpm=60)
        assert cfg.name == "test"
        assert cfg.rpm == 60
        assert cfg.tpm == 0
        assert cfg.burst == 1
        assert cfg.timeout_s == 30.0
        assert cfg.retry_after_default_s == 5.0

    def test_creation_with_all_fields(self):
        """EndpointConfig accepts all explicit fields."""
        cfg = EndpointConfig(
            name="messages",
            rpm=100,
            tpm=200_000,
            burst=5,
            timeout_s=60.0,
            retry_after_default_s=10.0,
        )
        assert cfg.name == "messages"
        assert cfg.rpm == 100
        assert cfg.tpm == 200_000
        assert cfg.burst == 5
        assert cfg.timeout_s == 60.0
        assert cfg.retry_after_default_s == 10.0

    def test_frozen(self):
        """EndpointConfig is immutable."""
        cfg = EndpointConfig(name="test", rpm=60)
        with pytest.raises(AttributeError):
            cfg.rpm = 120  # type: ignore[misc]


class TestProviderProfile:
    def test_creation(self):
        """ProviderProfile stores provider name and endpoints dict."""
        ep = EndpointConfig(name="messages", rpm=60)
        profile = ProviderProfile(
            provider_name="claude", endpoints={"messages": ep}
        )
        assert profile.provider_name == "claude"
        assert "messages" in profile.endpoints
        assert profile.endpoints["messages"].rpm == 60


class TestDefaultProfiles:
    def test_doubleword_exists(self):
        """DEFAULT_PROFILES includes a doubleword provider."""
        assert "doubleword" in DEFAULT_PROFILES

    def test_doubleword_endpoints(self):
        """Doubleword profile has expected endpoints with correct RPMs."""
        dw = DEFAULT_PROFILES["doubleword"]
        assert dw.provider_name == "doubleword"
        assert "files_upload" in dw.endpoints
        assert dw.endpoints["files_upload"].rpm == 30
        assert "batches_create" in dw.endpoints
        assert dw.endpoints["batches_create"].rpm == 30
        assert "batches_poll" in dw.endpoints
        assert dw.endpoints["batches_poll"].rpm == 60
        assert "batches_retrieve" in dw.endpoints
        assert dw.endpoints["batches_retrieve"].rpm == 60

    def test_claude_exists(self):
        """DEFAULT_PROFILES includes a claude provider."""
        assert "claude" in DEFAULT_PROFILES

    def test_claude_messages_endpoint(self):
        """Claude profile has messages endpoint with RPM and TPM."""
        cl = DEFAULT_PROFILES["claude"]
        assert cl.provider_name == "claude"
        assert "messages" in cl.endpoints
        assert cl.endpoints["messages"].rpm == 60
        assert cl.endpoints["messages"].tpm == 100_000


# ---------------------------------------------------------------------------
# Component 2: LatencyRing
# ---------------------------------------------------------------------------


class TestLatencyRing:
    def test_push_and_values(self):
        """Push adds values and values() returns them in order."""
        ring = LatencyRing(capacity=5)
        ring.push(1.0)
        ring.push(2.0)
        assert ring.values() == [1.0, 2.0]

    def test_overflow(self):
        """Ring drops oldest values when capacity is exceeded."""
        ring = LatencyRing(capacity=3)
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            ring.push(v)
        assert ring.values() == [3.0, 4.0, 5.0]
        assert len(ring) == 3

    def test_last_n(self):
        """last_n returns the N most recent values."""
        ring = LatencyRing(capacity=10)
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            ring.push(v)
        assert ring.last_n(3) == [3.0, 4.0, 5.0]

    def test_last_n_more_than_available(self):
        """last_n with n > length returns all values."""
        ring = LatencyRing(capacity=10)
        ring.push(1.0)
        assert ring.last_n(5) == [1.0]

    def test_empty(self):
        """Empty ring has length 0 and returns empty lists."""
        ring = LatencyRing(capacity=10)
        assert len(ring) == 0
        assert ring.values() == []
        assert ring.last_n(5) == []

    def test_seed(self):
        """seed() pre-populates the ring."""
        ring = LatencyRing(capacity=5)
        ring.seed([1.0, 2.0, 3.0])
        assert ring.values() == [1.0, 2.0, 3.0]
        assert len(ring) == 3

    def test_seed_overflow(self):
        """seed() respects capacity — oldest items dropped."""
        ring = LatencyRing(capacity=3)
        ring.seed([1.0, 2.0, 3.0, 4.0, 5.0])
        assert ring.values() == [3.0, 4.0, 5.0]


# ---------------------------------------------------------------------------
# Component 3: MemoryRateLimitStore
# ---------------------------------------------------------------------------


class TestMemoryRateLimitStore:
    @pytest.mark.asyncio
    async def test_initial_state(self):
        """get_state returns default state for unknown key."""
        store = MemoryRateLimitStore()
        state = await store.get_state("unknown")
        assert state["tokens"] == 0.0
        assert state["last_refill"] == 0.0

    @pytest.mark.asyncio
    async def test_update_and_get(self):
        """update_state persists and get_state retrieves."""
        store = MemoryRateLimitStore()
        await store.update_state("k1", tokens=5.5, last_refill=100.0)
        state = await store.get_state("k1")
        assert state["tokens"] == 5.5
        assert state["last_refill"] == 100.0

    @pytest.mark.asyncio
    async def test_concurrent_safety(self):
        """Concurrent updates don't corrupt state."""
        store = MemoryRateLimitStore()

        async def writer(i: int):
            for _ in range(50):
                state = await store.get_state("shared")
                new_tokens = state["tokens"] + 1
                await store.update_state("shared", tokens=new_tokens, last_refill=0.0)

        await asyncio.gather(*[writer(i) for i in range(5)])
        state = await store.get_state("shared")
        # With proper locking, exactly 250 increments
        assert state["tokens"] == 250.0


# ---------------------------------------------------------------------------
# Component 4: TokenBucket
# ---------------------------------------------------------------------------


class TestTokenBucket:
    @pytest.mark.asyncio
    async def test_immediate_acquire(self):
        """First acquire from a fresh bucket returns 0.0 (no wait)."""
        store = MemoryRateLimitStore()
        bucket = TokenBucket(key="test", store=store, rpm=60, burst=1)
        waited = await bucket.acquire()
        assert waited == 0.0

    @pytest.mark.asyncio
    async def test_wait_when_empty(self):
        """After burst exhausted, acquire waits ~1s for 60rpm."""
        store = MemoryRateLimitStore()
        bucket = TokenBucket(key="test", store=store, rpm=60, burst=1)
        # Exhaust the single burst token
        await bucket.acquire()
        t0 = time.monotonic()
        waited = await bucket.acquire()
        elapsed = time.monotonic() - t0
        # 60 rpm = 1 token/sec, so should wait ~1s
        assert 0.8 <= elapsed <= 1.5
        assert 0.8 <= waited <= 1.5

    @pytest.mark.asyncio
    async def test_throttle_multiplier_slows(self):
        """set_throttle(0.5) halves refill rate, doubling wait."""
        store = MemoryRateLimitStore()
        bucket = TokenBucket(key="test", store=store, rpm=60, burst=1)
        bucket.set_throttle(0.5)
        await bucket.acquire()
        t0 = time.monotonic()
        await bucket.acquire()
        elapsed = time.monotonic() - t0
        # 60rpm * 0.5 = 30rpm effective = 2s per token
        assert 1.5 <= elapsed <= 2.8

    @pytest.mark.asyncio
    async def test_burst_capacity(self):
        """Burst=3 allows 3 immediate acquires."""
        store = MemoryRateLimitStore()
        bucket = TokenBucket(key="test", store=store, rpm=60, burst=3)
        for _ in range(3):
            waited = await bucket.acquire()
            assert waited == 0.0
        # 4th should require a wait
        t0 = time.monotonic()
        await bucket.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.5

    @pytest.mark.asyncio
    async def test_acquire_multiple_tokens(self):
        """Acquiring multiple tokens at once works correctly."""
        store = MemoryRateLimitStore()
        bucket = TokenBucket(key="test", store=store, rpm=60, burst=5)
        waited = await bucket.acquire(tokens=3)
        assert waited == 0.0
        # 2 tokens remain in burst, acquiring 3 should wait
        t0 = time.monotonic()
        await bucket.acquire(tokens=3)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.5


# ---------------------------------------------------------------------------
# Component 5: CircuitBreaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_starts_closed(self):
        """CircuitBreaker starts in CLOSED state."""
        cb = CircuitBreaker()
        assert cb.state == BreakerState.CLOSED

    def test_trips_after_threshold(self):
        """3 consecutive failures trip the breaker to OPEN."""
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == BreakerState.OPEN

    def test_check_raises_when_open(self):
        """check() raises CircuitBreakerOpen when state is OPEN."""
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert cb.state == BreakerState.OPEN
        with pytest.raises(CircuitBreakerOpen):
            cb.check()

    def test_success_resets_failure_count(self):
        """record_success() resets the failure counter in CLOSED state."""
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        # After reset, need 3 more failures to trip
        cb.record_failure()
        cb.record_failure()
        assert cb.state == BreakerState.CLOSED

    def test_recovers_to_half_open(self):
        """After recovery_timeout_s, OPEN -> HALF_OPEN on check()."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.0)
        cb.record_failure()
        assert cb.state == BreakerState.OPEN
        # With timeout=0, next check() should transition to HALF_OPEN
        cb.check()  # Should not raise — transitions to HALF_OPEN
        assert cb.state == BreakerState.HALF_OPEN

    def test_half_open_success_closes(self):
        """record_success() in HALF_OPEN transitions to CLOSED."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.0)
        cb.record_failure()
        cb.check()  # -> HALF_OPEN
        cb.record_success()
        assert cb.state == BreakerState.CLOSED

    def test_half_open_failure_reopens(self):
        """record_failure() in HALF_OPEN transitions back to OPEN."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.0)
        cb.record_failure()
        cb.check()  # -> HALF_OPEN
        cb.record_failure()
        assert cb.state == BreakerState.OPEN

    @pytest.mark.asyncio
    async def test_event_fires_on_state_change(self):
        """state_changed event is set on state transitions."""
        cb = CircuitBreaker(failure_threshold=1)
        cb.state_changed.clear()
        cb.record_failure()  # CLOSED -> OPEN
        assert cb.state_changed.is_set()

    def test_failure_classification(self):
        """is_retriable_failure classifies 429/500/502/503 as failures."""
        cb = CircuitBreaker()
        assert cb.is_retriable_failure(429) is True
        assert cb.is_retriable_failure(500) is True
        assert cb.is_retriable_failure(502) is True
        assert cb.is_retriable_failure(503) is True
        assert cb.is_retriable_failure(200) is False
        assert cb.is_retriable_failure(404) is False
        assert cb.is_retriable_failure(401) is False


# ---------------------------------------------------------------------------
# Component 6: PredictiveThrottle
# ---------------------------------------------------------------------------


class TestPredictiveThrottle:
    def test_no_data_returns_one(self):
        """Empty ring yields throttle multiplier of 1.0 (no throttle)."""
        pt = PredictiveThrottle(timeout_s=30.0)
        ring = LatencyRing(capacity=100)
        assert pt.compute(ring) == 1.0

    def test_stable_latency_no_throttle(self):
        """Stable low latency yields 1.0 (no throttle)."""
        pt = PredictiveThrottle(timeout_s=30.0)
        ring = LatencyRing(capacity=100)
        ring.seed([0.5] * 30)
        result = pt.compute(ring)
        assert result == 1.0

    def test_ewma_high_latency_throttles(self):
        """EWMA layer detects high latency and throttles."""
        pt = PredictiveThrottle(timeout_s=30.0, ewma_alpha=0.3)
        ring = LatencyRing(capacity=100)
        # Seed baseline (first 10 values, median used as baseline)
        ring.seed([1.0] * 10)
        # Then push high latencies to raise the EWMA > 2x baseline
        for _ in range(30):
            ring.push(3.5)
        result = pt.compute(ring)
        assert result <= 0.6

    def test_variance_spike_emergency_throttle(self):
        """Variance spike layer triggers emergency throttle."""
        pt = PredictiveThrottle(timeout_s=30.0, variance_spike_ratio=5.0)
        ring = LatencyRing(capacity=100)
        # Stable background
        ring.seed([1.0] * 20)
        # Sudden wild spikes in last 5
        for v in [10.0, 20.0, 30.0, 15.0, 25.0]:
            ring.push(v)
        result = pt.compute(ring)
        assert result <= 0.2

    def test_regression_projects_timeout(self):
        """Linear regression layer detects rising trend toward timeout."""
        pt = PredictiveThrottle(timeout_s=30.0)
        ring = LatencyRing(capacity=100)
        # Baseline
        ring.seed([1.0] * 10)
        # Steadily rising latencies approaching timeout
        for i in range(20):
            ring.push(1.0 + i * 1.3)
        result = pt.compute(ring)
        assert result <= 0.4

    def test_minimum_of_layers_wins(self):
        """Output is the minimum across all three layers."""
        pt = PredictiveThrottle(timeout_s=30.0, variance_spike_ratio=5.0)
        ring = LatencyRing(capacity=100)
        # Seed baseline
        ring.seed([1.0] * 10)
        # Create conditions that trigger both EWMA and variance layers
        ring.seed([1.0] * 20)
        for v in [10.0, 20.0, 30.0, 15.0, 25.0]:
            ring.push(v)
        result = pt.compute(ring)
        # Should be the minimum (most aggressive throttle)
        assert result <= 0.2

    def test_output_clamped_to_range(self):
        """Throttle multiplier is always in [0.05, 1.0]."""
        pt = PredictiveThrottle(timeout_s=0.001)  # Very small timeout
        ring = LatencyRing(capacity=100)
        ring.seed([1.0] * 10)
        for _ in range(20):
            ring.push(100.0)
        result = pt.compute(ring)
        assert 0.05 <= result <= 1.0


# ---------------------------------------------------------------------------
# Component 7: RateLimitService (EndpointState + RateLimitService)
# ---------------------------------------------------------------------------


from backend.core.ouroboros.governance.rate_limiter import (  # noqa: E402
    EndpointState,
    RateLimitService,
    CircuitBreakerOpen,
)


class TestRateLimitService:
    @pytest.mark.asyncio
    async def test_service_acquire_and_record(self):
        """Basic acquire + record flow works without errors."""
        svc = RateLimitService()
        # acquire from a known provider/endpoint
        wait = await svc.acquire("doubleword", "batches_poll")
        assert wait >= 0.0
        # record a successful response
        svc.record("doubleword", "batches_poll", latency_s=0.2, status=200)
        state = svc.get_endpoint_state("doubleword", "batches_poll")
        assert state["breaker_state"] == "CLOSED"
        assert state["latency_count"] >= 1

    @pytest.mark.asyncio
    async def test_service_circuit_breaker_trips(self):
        """3 consecutive retriable failures trip the circuit breaker."""
        svc = RateLimitService()
        for _ in range(3):
            svc.record("doubleword", "files_upload", latency_s=0.5, status=500)
        # Next acquire should raise CircuitBreakerOpen
        with pytest.raises(CircuitBreakerOpen):
            await svc.acquire("doubleword", "files_upload")

    @pytest.mark.asyncio
    async def test_service_unknown_endpoint_uses_defaults(self):
        """Accessing an unknown provider/endpoint uses fallback config."""
        svc = RateLimitService()
        # Should not raise
        wait = await svc.acquire("unknown_provider", "unknown_endpoint")
        assert wait >= 0.0
        state = svc.get_endpoint_state("unknown_provider", "unknown_endpoint")
        assert state["breaker_state"] == "CLOSED"

    @pytest.mark.asyncio
    async def test_service_throttle_updates_on_high_latency(self):
        """After 10 low + 10 high latencies, throttle multiplier drops below 1.0."""
        svc = RateLimitService()
        provider, endpoint = "doubleword", "batches_create"
        # Establish a stable baseline (first 10 form the EWMA baseline)
        for _ in range(10):
            svc.record(provider, endpoint, latency_s=0.5, status=200)
        # Now push latencies much higher than baseline
        for _ in range(10):
            svc.record(provider, endpoint, latency_s=15.0, status=200)
        state = svc.get_endpoint_state(provider, endpoint)
        assert state["throttle_multiplier"] < 1.0

    @pytest.mark.asyncio
    async def test_service_backpressure_event_fires(self):
        """A variance spike sets throttle_changed on the endpoint state."""
        svc = RateLimitService()
        provider, endpoint = "doubleword", "batches_retrieve"
        # Stable background
        for _ in range(20):
            svc.record(provider, endpoint, latency_s=1.0, status=200)
        # Wild variance spike — should cause throttle_changed flag
        for v in [10.0, 30.0, 50.0, 20.0, 40.0]:
            svc.record(provider, endpoint, latency_s=v, status=200)
        state = svc.get_endpoint_state(provider, endpoint)
        assert state["throttle_changed"] is True

    @pytest.mark.asyncio
    async def test_service_persistence(self, tmp_path):
        """save() + reload preserves latency history (latency_count)."""
        svc = RateLimitService(persistence_dir=str(tmp_path))
        provider, endpoint = "claude", "messages"
        for i in range(5):
            svc.record(provider, endpoint, latency_s=float(i + 1), status=200)
        state_before = svc.get_endpoint_state(provider, endpoint)
        assert state_before["latency_count"] == 5

        svc.save()

        # Create a new service instance pointing at same dir — should reload
        svc2 = RateLimitService(persistence_dir=str(tmp_path))
        # Trigger endpoint creation by querying state
        state_after = svc2.get_endpoint_state(provider, endpoint)
        assert state_after["latency_count"] == 5
