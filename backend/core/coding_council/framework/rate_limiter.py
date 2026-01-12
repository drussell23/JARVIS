"""
v77.0: Rate Limiter - Gap #10
==============================

Rate limiting implementations:
- Token bucket algorithm
- Sliding window algorithm
- Per-service rate limits
- Burst handling
- Priority-based limits

Author: JARVIS v77.0
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Coroutine, Deque, Dict, Optional, TypeVar

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RateLimitExceeded(Exception):
    """Raised when rate limit is exceeded."""
    def __init__(self, name: str, retry_after: float):
        self.name = name
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded for '{name}'. Retry after {retry_after:.2f}s")


class RateLimiterBase(ABC):
    """Base class for rate limiters."""

    @abstractmethod
    async def acquire(self, tokens: int = 1) -> bool:
        """Try to acquire tokens. Returns True if allowed."""
        pass

    @abstractmethod
    async def wait(self, tokens: int = 1) -> None:
        """Wait until tokens are available."""
        pass

    @abstractmethod
    def get_wait_time(self, tokens: int = 1) -> float:
        """Get estimated wait time for tokens."""
        pass


@dataclass
class TokenBucketConfig:
    """Configuration for token bucket."""
    capacity: int = 10          # Max tokens in bucket
    refill_rate: float = 1.0    # Tokens per second
    initial_tokens: Optional[int] = None  # Starting tokens (default: capacity)


class TokenBucket(RateLimiterBase):
    """
    Token bucket rate limiter.

    Allows bursts up to capacity, then rate-limits to refill_rate.
    """

    def __init__(self, name: str, config: Optional[TokenBucketConfig] = None):
        self.name = name
        self.config = config or TokenBucketConfig()
        self._tokens = float(
            self.config.initial_tokens
            if self.config.initial_tokens is not None
            else self.config.capacity
        )
        self._last_refill = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> bool:
        """Try to acquire tokens without waiting."""
        async with self._lock:
            self._refill()

            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    async def wait(self, tokens: int = 1) -> None:
        """Wait until tokens are available, then acquire them."""
        while True:
            async with self._lock:
                self._refill()

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return

                # Calculate wait time
                needed = tokens - self._tokens
                wait_time = needed / self.config.refill_rate

            await asyncio.sleep(min(wait_time, 1.0))

    def get_wait_time(self, tokens: int = 1) -> float:
        """Get estimated wait time for tokens."""
        self._refill()

        if self._tokens >= tokens:
            return 0.0

        needed = tokens - self._tokens
        return needed / self.config.refill_rate

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self._last_refill
        self._last_refill = now

        # Add tokens based on time elapsed
        self._tokens = min(
            self.config.capacity,
            self._tokens + elapsed * self.config.refill_rate
        )

    @property
    def available_tokens(self) -> float:
        """Get currently available tokens."""
        self._refill()
        return self._tokens


@dataclass
class SlidingWindowConfig:
    """Configuration for sliding window."""
    window_size: float = 60.0   # Window in seconds
    max_requests: int = 60      # Max requests per window


class SlidingWindow(RateLimiterBase):
    """
    Sliding window rate limiter.

    Provides smoother rate limiting than fixed windows.
    """

    def __init__(self, name: str, config: Optional[SlidingWindowConfig] = None):
        self.name = name
        self.config = config or SlidingWindowConfig()
        self._timestamps: Deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> bool:
        """Try to acquire slots without waiting."""
        async with self._lock:
            self._cleanup()

            if len(self._timestamps) + tokens <= self.config.max_requests:
                now = time.time()
                for _ in range(tokens):
                    self._timestamps.append(now)
                return True
            return False

    async def wait(self, tokens: int = 1) -> None:
        """Wait until slots are available."""
        while True:
            async with self._lock:
                self._cleanup()

                if len(self._timestamps) + tokens <= self.config.max_requests:
                    now = time.time()
                    for _ in range(tokens):
                        self._timestamps.append(now)
                    return

                # Calculate wait time
                wait_time = self.get_wait_time(tokens)

            await asyncio.sleep(min(wait_time, 1.0))

    def get_wait_time(self, tokens: int = 1) -> float:
        """Get estimated wait time."""
        self._cleanup()

        available = self.config.max_requests - len(self._timestamps)
        if available >= tokens:
            return 0.0

        # Need to wait for oldest requests to expire
        needed = tokens - available
        if needed <= 0 or not self._timestamps:
            return 0.0

        # Time until oldest needed request expires
        oldest_needed = list(self._timestamps)[needed - 1]
        expiry = oldest_needed + self.config.window_size
        return max(0.0, expiry - time.time())

    def _cleanup(self) -> None:
        """Remove timestamps outside the window."""
        cutoff = time.time() - self.config.window_size
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    @property
    def current_count(self) -> int:
        """Get current request count in window."""
        self._cleanup()
        return len(self._timestamps)


class RateLimiter:
    """
    Multi-strategy rate limiter with priorities.

    Supports both token bucket and sliding window with
    priority queuing for important requests.
    """

    def __init__(
        self,
        name: str,
        token_bucket_config: Optional[TokenBucketConfig] = None,
        sliding_window_config: Optional[SlidingWindowConfig] = None,
    ):
        self.name = name
        self._token_bucket = TokenBucket(name, token_bucket_config) if token_bucket_config else None
        self._sliding_window = SlidingWindow(name, sliding_window_config) if sliding_window_config else None

        # Default to sliding window if nothing specified
        if not self._token_bucket and not self._sliding_window:
            self._sliding_window = SlidingWindow(name)

        self._priority_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._stats = {
            "allowed": 0,
            "rejected": 0,
            "waited": 0,
        }

    async def acquire(
        self,
        tokens: int = 1,
        wait: bool = True,
        priority: int = 5,
    ) -> bool:
        """
        Acquire rate limit tokens.

        Args:
            tokens: Number of tokens to acquire
            wait: If True, wait for tokens; if False, return immediately
            priority: Lower = higher priority (1-10)

        Returns:
            True if acquired, False if rejected (when wait=False)
        """
        # Try immediate acquisition
        if await self._try_acquire(tokens):
            self._stats["allowed"] += 1
            return True

        if not wait:
            self._stats["rejected"] += 1
            return False

        # Wait for tokens
        self._stats["waited"] += 1
        await self._wait_for_tokens(tokens, priority)
        self._stats["allowed"] += 1
        return True

    async def _try_acquire(self, tokens: int) -> bool:
        """Try to acquire from all configured limiters."""
        # Must pass all configured limiters
        if self._token_bucket:
            if not await self._token_bucket.acquire(tokens):
                return False

        if self._sliding_window:
            if not await self._sliding_window.acquire(tokens):
                # Rollback token bucket if sliding window fails
                if self._token_bucket:
                    self._token_bucket._tokens += tokens
                return False

        return True

    async def _wait_for_tokens(self, tokens: int, priority: int) -> None:
        """Wait for tokens with priority queuing."""
        if self._token_bucket:
            await self._token_bucket.wait(tokens)
        if self._sliding_window:
            await self._sliding_window.wait(tokens)

    def get_wait_time(self, tokens: int = 1) -> float:
        """Get estimated wait time."""
        wait_times = []

        if self._token_bucket:
            wait_times.append(self._token_bucket.get_wait_time(tokens))

        if self._sliding_window:
            wait_times.append(self._sliding_window.get_wait_time(tokens))

        return max(wait_times) if wait_times else 0.0

    def get_info(self) -> Dict:
        """Get rate limiter information."""
        info = {
            "name": self.name,
            "stats": self._stats.copy(),
        }

        if self._token_bucket:
            info["token_bucket"] = {
                "available": self._token_bucket.available_tokens,
                "capacity": self._token_bucket.config.capacity,
                "refill_rate": self._token_bucket.config.refill_rate,
            }

        if self._sliding_window:
            info["sliding_window"] = {
                "current": self._sliding_window.current_count,
                "max": self._sliding_window.config.max_requests,
                "window_size": self._sliding_window.config.window_size,
            }

        return info


# Global registry
_rate_limiters: Dict[str, RateLimiter] = {}
_registry_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_rate_limiter(name: str, **kwargs) -> RateLimiter:
    """Get or create a rate limiter by name."""
    async with _registry_lock:
        if name not in _rate_limiters:
            _rate_limiters[name] = RateLimiter(name, **kwargs)
        return _rate_limiters[name]


def rate_limit(
    name: str,
    tokens: int = 1,
    wait: bool = True,
    raise_on_limit: bool = False,
):
    """
    Decorator to apply rate limiting to a function.

    Usage:
        @rate_limit("api_calls", tokens=1)
        async def call_api():
            ...
    """
    def decorator(func: Callable[..., Coroutine]) -> Callable[..., Coroutine]:
        limiter: Optional[RateLimiter] = None

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            nonlocal limiter
            if limiter is None:
                limiter = await get_rate_limiter(name)

            acquired = await limiter.acquire(tokens, wait=wait)

            if not acquired and raise_on_limit:
                raise RateLimitExceeded(name, limiter.get_wait_time(tokens))

            if not acquired:
                return None

            return await func(*args, **kwargs)

        return wrapper
    return decorator
