"""tests/governance/autonomy/test_rate_limiter.py

TDD tests for TokenBucketRateLimiter, RetryState, ResourceUsage (Task H1).

Covers:
- TokenBucketRateLimiter: acquire, refill, status, timeout waiting
- RetryState: exponential backoff, jitter, retry limit
- ResourceUsage: dataclass fields
- CommandBus integration: rate limiter on put(), try_put() bypass
"""
from __future__ import annotations

import asyncio
import time

import pytest


# ---------------------------------------------------------------------------
# TokenBucketRateLimiter tests
# ---------------------------------------------------------------------------


class TestTokenBucketRateLimiterAcquire:
    @pytest.mark.asyncio
    async def test_acquire_succeeds_with_available_tokens(self):
        """Acquiring up to burst capacity should all return True."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import (
            RateLimiterConfig,
            TokenBucketRateLimiter,
        )

        cfg = RateLimiterConfig(rate=10.0, burst=3)
        limiter = TokenBucketRateLimiter(config=cfg)

        assert await limiter.acquire() is True
        assert await limiter.acquire() is True
        assert await limiter.acquire() is True

    @pytest.mark.asyncio
    async def test_acquire_fails_after_burst_exhausted(self):
        """After exhausting burst capacity, acquire with short timeout returns False."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import (
            RateLimiterConfig,
            TokenBucketRateLimiter,
        )

        cfg = RateLimiterConfig(rate=0.1, burst=2)
        limiter = TokenBucketRateLimiter(config=cfg)

        assert await limiter.acquire() is True
        assert await limiter.acquire() is True
        # Burst exhausted, very slow refill, short timeout
        assert await limiter.acquire(timeout=0.01) is False

    @pytest.mark.asyncio
    async def test_tokens_refill_over_time(self):
        """After exhaustion, tokens refill based on rate and elapsed time."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import (
            RateLimiterConfig,
            TokenBucketRateLimiter,
        )

        cfg = RateLimiterConfig(rate=100.0, burst=1)
        limiter = TokenBucketRateLimiter(config=cfg)

        assert await limiter.acquire() is True
        # At rate=100/s, 0.02s = 2 tokens refilled (capped to burst=1)
        await asyncio.sleep(0.02)
        assert await limiter.acquire() is True

    @pytest.mark.asyncio
    async def test_acquire_with_timeout_waits(self):
        """acquire() with sufficient timeout should wait for refill and succeed."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import (
            RateLimiterConfig,
            TokenBucketRateLimiter,
        )

        cfg = RateLimiterConfig(rate=100.0, burst=1)
        limiter = TokenBucketRateLimiter(config=cfg)

        # Exhaust
        assert await limiter.acquire() is True
        # With timeout=0.1, rate=100/s => refill in 0.01s, should succeed
        assert await limiter.acquire(timeout=0.1) is True


class TestTokenBucketRateLimiterStatus:
    @pytest.mark.asyncio
    async def test_get_status_returns_correct_fields(self):
        """get_status() must return tokens_available, rate_per_second, burst_capacity."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import (
            RateLimiterConfig,
            TokenBucketRateLimiter,
        )

        cfg = RateLimiterConfig(rate=5.0, burst=10)
        limiter = TokenBucketRateLimiter(config=cfg)

        status = limiter.get_status()
        assert "tokens_available" in status
        assert "rate_per_second" in status
        assert "burst_capacity" in status
        assert status["rate_per_second"] == 5.0
        assert status["burst_capacity"] == 10
        assert status["tokens_available"] <= 10.0

    @pytest.mark.asyncio
    async def test_get_status_reflects_consumption(self):
        """After acquiring tokens, status should reflect fewer available."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import (
            RateLimiterConfig,
            TokenBucketRateLimiter,
        )

        cfg = RateLimiterConfig(rate=0.1, burst=5)
        limiter = TokenBucketRateLimiter(config=cfg)

        await limiter.acquire()
        await limiter.acquire()

        status = limiter.get_status()
        # Started at 5, used 2, slow refill => should be around 3
        assert status["tokens_available"] < 4.0


# ---------------------------------------------------------------------------
# RetryState tests
# ---------------------------------------------------------------------------


class TestRetryState:
    def test_get_next_delay_exponential_backoff(self):
        """Delays should increase exponentially with attempt number."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import RetryState

        state = RetryState(attempt=0)
        d0 = state.get_next_delay(base_delay=1.0, max_delay=30.0, jitter_factor=0.0)

        state_1 = RetryState(attempt=1)
        d1 = state_1.get_next_delay(base_delay=1.0, max_delay=30.0, jitter_factor=0.0)

        state_2 = RetryState(attempt=2)
        d2 = state_2.get_next_delay(base_delay=1.0, max_delay=30.0, jitter_factor=0.0)

        # With jitter_factor=0, delays are deterministic: 1, 2, 4
        assert d0 == pytest.approx(1.0)
        assert d1 == pytest.approx(2.0)
        assert d2 == pytest.approx(4.0)

    def test_get_next_delay_respects_max_delay(self):
        """Delay should be capped at max_delay."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import RetryState

        state = RetryState(attempt=20)  # 2^20 * 1.0 = huge, but capped
        delay = state.get_next_delay(base_delay=1.0, max_delay=30.0, jitter_factor=0.0)
        assert delay == pytest.approx(30.0)

    def test_get_next_delay_with_jitter(self):
        """With jitter_factor > 0, delay should be >= base and vary."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import RetryState

        state = RetryState(attempt=0)
        delays = set()
        for _ in range(20):
            d = state.get_next_delay(base_delay=1.0, max_delay=30.0, jitter_factor=0.5)
            assert d >= 1.0
            assert d <= 1.0 + 0.5 * 1.0  # base + jitter * base
            delays.add(round(d, 6))
        # With 20 samples and jitter, we should see some variation
        assert len(delays) > 1

    def test_should_retry_within_limit(self):
        """should_retry returns True when attempt < max_retries."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import RetryState

        state = RetryState(attempt=2)
        assert state.should_retry(max_retries=5) is True

    def test_should_retry_at_limit(self):
        """should_retry returns False when attempt >= max_retries."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import RetryState

        state = RetryState(attempt=5)
        assert state.should_retry(max_retries=5) is False

    def test_should_retry_over_limit(self):
        """should_retry returns False when attempt > max_retries."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import RetryState

        state = RetryState(attempt=10)
        assert state.should_retry(max_retries=3) is False


# ---------------------------------------------------------------------------
# ResourceUsage tests
# ---------------------------------------------------------------------------


class TestResourceUsage:
    def test_resource_usage_fields(self):
        """ResourceUsage should have all expected fields."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import (
            ResourceUsage,
        )

        usage = ResourceUsage(
            memory_mb=1024.0,
            disk_free_mb=50000.0,
            cpu_percent=45.2,
            active_tasks=3,
        )
        assert usage.memory_mb == 1024.0
        assert usage.disk_free_mb == 50000.0
        assert usage.cpu_percent == 45.2
        assert usage.active_tasks == 3
        assert usage.timestamp_ns > 0

    def test_resource_usage_auto_timestamp(self):
        """Timestamp should be auto-populated with monotonic_ns."""
        from backend.core.ouroboros.governance.autonomy.rate_limiter import (
            ResourceUsage,
        )

        before = time.monotonic_ns()
        usage = ResourceUsage(
            memory_mb=0.0, disk_free_mb=0.0, cpu_percent=0.0, active_tasks=0
        )
        after = time.monotonic_ns()
        assert before <= usage.timestamp_ns <= after


# ---------------------------------------------------------------------------
# CommandBus + rate limiter integration tests
# ---------------------------------------------------------------------------


def _make_cmd(
    command_type=None,
    payload=None,
    ttl_s: float = 30.0,
    source_layer: str = "L2",
    target_layer: str = "L1",
    idempotency_key: str = "",
):
    """Helper to build CommandEnvelope with minimal boilerplate."""
    from backend.core.ouroboros.governance.autonomy.autonomy_types import (
        CommandEnvelope,
        CommandType,
    )

    return CommandEnvelope(
        source_layer=source_layer,
        target_layer=target_layer,
        command_type=command_type or CommandType.GENERATE_BACKLOG_ENTRY,
        payload=payload or {},
        ttl_s=ttl_s,
        idempotency_key=idempotency_key,
    )


class TestCommandBusWithRateLimiter:
    @pytest.mark.asyncio
    async def test_put_respects_rate_limiter(self):
        """put() should return False when rate limiter denies the request."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.rate_limiter import (
            RateLimiterConfig,
            TokenBucketRateLimiter,
        )

        # burst=1, very slow refill => second put should be rate-limited
        cfg = RateLimiterConfig(rate=0.1, burst=1)
        limiter = TokenBucketRateLimiter(config=cfg)
        bus = CommandBus(maxsize=256, rate_limiter=limiter)

        cmd1 = _make_cmd(payload={"id": "first"})
        cmd2 = _make_cmd(payload={"id": "second"})

        assert await bus.put(cmd1) is True
        assert await bus.put(cmd2) is False

    @pytest.mark.asyncio
    async def test_try_put_ignores_rate_limiter(self):
        """try_put() is sync fast path and should NOT use rate limiter."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.rate_limiter import (
            RateLimiterConfig,
            TokenBucketRateLimiter,
        )

        # Even with rate limiter that would block, try_put bypasses it
        cfg = RateLimiterConfig(rate=0.001, burst=1)
        limiter = TokenBucketRateLimiter(config=cfg)
        bus = CommandBus(maxsize=256, rate_limiter=limiter)

        cmd1 = _make_cmd(payload={"id": "a"})
        cmd2 = _make_cmd(payload={"id": "b"})

        assert bus.try_put(cmd1) is True
        assert bus.try_put(cmd2) is True  # bypasses rate limiter

    @pytest.mark.asyncio
    async def test_put_without_rate_limiter_unchanged(self):
        """CommandBus without rate limiter should work exactly as before."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=256)  # no rate_limiter

        cmd1 = _make_cmd(payload={"id": "x"})
        cmd2 = _make_cmd(payload={"id": "y"})

        assert await bus.put(cmd1) is True
        assert await bus.put(cmd2) is True
        assert bus.qsize() == 2

    @pytest.mark.asyncio
    async def test_get_rate_limiter_status_with_limiter(self):
        """get_rate_limiter_status() returns dict when limiter is set."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.rate_limiter import (
            RateLimiterConfig,
            TokenBucketRateLimiter,
        )

        cfg = RateLimiterConfig(rate=10.0, burst=5)
        limiter = TokenBucketRateLimiter(config=cfg)
        bus = CommandBus(maxsize=256, rate_limiter=limiter)

        status = bus.get_rate_limiter_status()
        assert status is not None
        assert "tokens_available" in status

    @pytest.mark.asyncio
    async def test_get_rate_limiter_status_without_limiter(self):
        """get_rate_limiter_status() returns None when no limiter is set."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=256)
        assert bus.get_rate_limiter_status() is None
