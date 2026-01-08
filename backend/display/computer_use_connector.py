"""
Claude Computer Use API Connector for JARVIS

This module provides a robust, async, and dynamic integration with Claude's
Computer Use API for vision-based UI automation. It replaces hardcoded
workflows with intelligent, adaptive action chains.

Key Features:
- Vision-based element detection (no hardcoded coordinates)
- Dynamic action chain execution with real-time reasoning
- Voice narration integration for transparency
- Automatic failure recovery and alternative approach generation
- Learning from successful interactions
- Full async support throughout

Architecture:
    Screenshot -> Claude Vision Analysis -> Action Decision -> Execution -> Verification
         ^                                                                      |
         |______________________________________________________________________|
                              (Loop until goal achieved or max attempts)

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import gc
import hashlib
import io
import json
import logging
import os
import random
import signal
import time
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import partial
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Deque,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)
from uuid import uuid4

import pyautogui
from PIL import Image


# =============================================================================
# Environment Configuration (Zero Hardcoding)
# =============================================================================

def _env_int(key: str, default: int) -> int:
    """Get integer from environment."""
    return int(os.getenv(key, str(default)))


def _env_float(key: str, default: float) -> float:
    """Get float from environment."""
    return float(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool) -> bool:
    """Get boolean from environment."""
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


# Connection pool settings
CU_POOL_SIZE = _env_int("CU_POOL_SIZE", 3)
CU_POOL_MAX_RETRIES = _env_int("CU_POOL_MAX_RETRIES", 5)
CU_POOL_BACKOFF_BASE = _env_float("CU_POOL_BACKOFF_BASE", 2.0)
CU_POOL_BACKOFF_MAX = _env_float("CU_POOL_BACKOFF_MAX", 60.0)
CU_POOL_HEALTH_INTERVAL = _env_float("CU_POOL_HEALTH_INTERVAL", 30.0)

# Rate limiting
CU_RATE_LIMIT_RPM = _env_int("CU_RATE_LIMIT_RPM", 50)  # Requests per minute
CU_RATE_LIMIT_BURST = _env_int("CU_RATE_LIMIT_BURST", 10)

# Screenshot management
CU_SCREENSHOT_CACHE_SIZE = _env_int("CU_SCREENSHOT_CACHE_SIZE", 5)
CU_SCREENSHOT_MAX_MEMORY_MB = _env_int("CU_SCREENSHOT_MAX_MEMORY_MB", 100)

# Parallel execution
CU_PARALLEL_MAX_ACTIONS = _env_int("CU_PARALLEL_MAX_ACTIONS", 5)
CU_PARALLEL_CONFLICT_DELAY = _env_float("CU_PARALLEL_CONFLICT_DELAY", 0.05)

# Edge case handling
CU_ANIMATION_WAIT_MS = _env_int("CU_ANIMATION_WAIT_MS", 300)
CU_FOCUS_VERIFY_RETRIES = _env_int("CU_FOCUS_VERIFY_RETRIES", 3)
CU_MULTI_MONITOR_ENABLED = _env_bool("CU_MULTI_MONITOR_ENABLED", True)

T = TypeVar("T")

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

# ============================================================================
# Async Utilities - Thread Pool for Blocking Operations
# ============================================================================

# Global thread pool for PyAutoGUI operations (max 2 workers to prevent conflicts)
_executor: Optional[ThreadPoolExecutor] = None

def _get_executor() -> ThreadPoolExecutor:
    """Get or create the global thread pool executor."""
    global _executor
    if _executor is None:
        if _HAS_MANAGED_EXECUTOR:
            _executor = ManagedThreadPoolExecutor(max_workers=2, thread_name_prefix="pyautogui_", name="pyautogui")
        else:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pyautogui_")
    return _executor

async def run_blocking(func: Callable, *args, timeout: float = 30.0, **kwargs) -> Any:
    """
    Run a blocking function in a thread pool with timeout protection.

    Args:
        func: The blocking function to run
        *args: Positional arguments for the function
        timeout: Maximum time to wait (default 30s)
        **kwargs: Keyword arguments for the function

    Returns:
        Result of the function

    Raises:
        asyncio.TimeoutError: If operation times out
    """
    loop = asyncio.get_event_loop()
    executor = _get_executor()

    # Create partial function with kwargs
    if kwargs:
        func = partial(func, **kwargs)

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, func, *args),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        logging.getLogger(__name__).error(f"Blocking operation timed out after {timeout}s: {func}")
        raise


class CircuitBreaker:
    """
    Circuit breaker pattern for API calls.
    Prevents cascading failures by failing fast when service is unhealthy.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._state = "closed"  # closed, open, half-open
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self._state == "open"

    async def can_execute(self) -> bool:
        """Check if execution is allowed."""
        async with self._lock:
            if self._state == "closed":
                return True

            if self._state == "open":
                # Check if recovery timeout has passed
                if self._last_failure_time and \
                   (time.time() - self._last_failure_time) >= self.recovery_timeout:
                    self._state = "half-open"
                    self._half_open_calls = 0
                    logging.getLogger(__name__).info("[CIRCUIT] Transitioning to half-open state")
                    return True
                return False

            if self._state == "half-open":
                if self._half_open_calls < self.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False

            return False

    async def record_success(self) -> None:
        """Record a successful call."""
        async with self._lock:
            if self._state == "half-open":
                self._state = "closed"
                self._failure_count = 0
                logging.getLogger(__name__).info("[CIRCUIT] Circuit closed - service recovered")
            elif self._state == "closed":
                self._failure_count = 0

    async def record_failure(self) -> None:
        """Record a failed call."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == "half-open":
                self._state = "open"
                logging.getLogger(__name__).warning("[CIRCUIT] Circuit opened - half-open test failed")
            elif self._failure_count >= self.failure_threshold:
                self._state = "open"
                logging.getLogger(__name__).warning(
                    f"[CIRCUIT] Circuit opened after {self._failure_count} failures"
                )

    def get_state(self) -> Dict[str, Any]:
        """Get circuit breaker state for debugging."""
        return {
            "state": self._state,
            "failure_count": self._failure_count,
            "last_failure": self._last_failure_time,
            "threshold": self.failure_threshold
        }


# =============================================================================
# Token Bucket Rate Limiter
# =============================================================================


class TokenBucketRateLimiter:
    """
    Token bucket rate limiter with burst support.

    Features:
    - Configurable requests per minute
    - Burst allowance for spiky traffic
    - Async-safe with proper locking
    - Automatic token replenishment
    """

    def __init__(
        self,
        rpm: int = CU_RATE_LIMIT_RPM,
        burst: int = CU_RATE_LIMIT_BURST,
    ):
        self._rpm = rpm
        self._burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._refill_rate = rpm / 60.0  # Tokens per second

    async def acquire(self, timeout: float = 30.0) -> bool:
        """
        Acquire a token, waiting if necessary.

        Args:
            timeout: Maximum time to wait for a token

        Returns:
            True if token acquired, False if timeout
        """
        start = time.monotonic()

        while True:
            async with self._lock:
                self._refill_tokens()

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

            # Calculate wait time
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                return False

            # Wait for next token
            wait_time = min(1.0 / self._refill_rate, timeout - elapsed)
            await asyncio.sleep(wait_time)

    def _refill_tokens(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._burst,
            self._tokens + elapsed * self._refill_rate
        )
        self._last_refill = now

    def get_stats(self) -> Dict[str, Any]:
        """Get rate limiter statistics."""
        return {
            "rpm": self._rpm,
            "burst": self._burst,
            "current_tokens": self._tokens,
            "refill_rate": self._refill_rate,
        }


# =============================================================================
# Error Classification for Adaptive Retry
# =============================================================================


class ErrorCategory(Enum):
    """Categories of errors for adaptive retry."""
    TRANSIENT = "transient"  # Retry immediately with backoff
    RATE_LIMITED = "rate_limited"  # Wait longer, then retry
    AUTH_ERROR = "auth_error"  # Don't retry, fail fast
    TIMEOUT = "timeout"  # Retry with longer timeout
    SERVER_ERROR = "server_error"  # Retry with exponential backoff
    NETWORK_ERROR = "network_error"  # Retry with jitter
    UNKNOWN = "unknown"  # Conservative retry


@dataclass
class ErrorClassification:
    """Classification result for an error."""
    category: ErrorCategory
    should_retry: bool
    wait_seconds: float
    message: str
    retry_with_longer_timeout: bool = False


class ErrorClassifier:
    """
    Classifies errors for intelligent retry decisions.

    Analyzes exception types and messages to determine:
    - Whether to retry
    - How long to wait
    - Whether to adjust parameters
    """

    # Error patterns and their classifications
    PATTERNS = {
        "rate_limit": ErrorCategory.RATE_LIMITED,
        "429": ErrorCategory.RATE_LIMITED,
        "too many requests": ErrorCategory.RATE_LIMITED,
        "authentication_error": ErrorCategory.AUTH_ERROR,
        "invalid x-api-key": ErrorCategory.AUTH_ERROR,
        "401": ErrorCategory.AUTH_ERROR,
        "unauthorized": ErrorCategory.AUTH_ERROR,
        "timeout": ErrorCategory.TIMEOUT,
        "timed out": ErrorCategory.TIMEOUT,
        "deadline exceeded": ErrorCategory.TIMEOUT,
        "500": ErrorCategory.SERVER_ERROR,
        "502": ErrorCategory.SERVER_ERROR,
        "503": ErrorCategory.SERVER_ERROR,
        "504": ErrorCategory.SERVER_ERROR,
        "internal server error": ErrorCategory.SERVER_ERROR,
        "connection reset": ErrorCategory.NETWORK_ERROR,
        "connection refused": ErrorCategory.NETWORK_ERROR,
        "network unreachable": ErrorCategory.NETWORK_ERROR,
        "dns": ErrorCategory.NETWORK_ERROR,
    }

    def classify(
        self,
        error: Exception,
        attempt: int = 0,
        base_wait: float = CU_POOL_BACKOFF_BASE,
    ) -> ErrorClassification:
        """
        Classify an error and determine retry strategy.

        Args:
            error: The exception to classify
            attempt: Current attempt number (0-indexed)
            base_wait: Base wait time for backoff

        Returns:
            ErrorClassification with retry decision
        """
        error_str = str(error).lower()
        error_type = type(error).__name__.lower()

        # Check patterns
        category = ErrorCategory.UNKNOWN
        for pattern, cat in self.PATTERNS.items():
            if pattern in error_str or pattern in error_type:
                category = cat
                break

        # Determine retry strategy based on category
        if category == ErrorCategory.AUTH_ERROR:
            return ErrorClassification(
                category=category,
                should_retry=False,
                wait_seconds=0,
                message="Authentication failed - check API key",
            )

        if category == ErrorCategory.RATE_LIMITED:
            # Extract retry-after header if present
            wait = self._extract_retry_after(error) or (base_wait * (2 ** attempt))
            # Add jitter to prevent thundering herd
            wait *= (1 + random.random() * 0.5)
            wait = min(wait, CU_POOL_BACKOFF_MAX)
            return ErrorClassification(
                category=category,
                should_retry=attempt < CU_POOL_MAX_RETRIES,
                wait_seconds=wait,
                message=f"Rate limited - waiting {wait:.1f}s",
            )

        if category == ErrorCategory.TIMEOUT:
            wait = base_wait * (1.5 ** attempt)  # Slower backoff for timeouts
            return ErrorClassification(
                category=category,
                should_retry=attempt < CU_POOL_MAX_RETRIES,
                wait_seconds=wait,
                message=f"Timeout - retrying with longer wait",
                retry_with_longer_timeout=True,
            )

        if category == ErrorCategory.SERVER_ERROR:
            wait = base_wait * (2 ** attempt)
            wait = min(wait, CU_POOL_BACKOFF_MAX)
            return ErrorClassification(
                category=category,
                should_retry=attempt < CU_POOL_MAX_RETRIES,
                wait_seconds=wait,
                message=f"Server error - backing off {wait:.1f}s",
            )

        if category == ErrorCategory.NETWORK_ERROR:
            # Network errors get jitter to spread out retries
            wait = base_wait * (2 ** attempt) * (0.5 + random.random())
            wait = min(wait, CU_POOL_BACKOFF_MAX)
            return ErrorClassification(
                category=category,
                should_retry=attempt < CU_POOL_MAX_RETRIES,
                wait_seconds=wait,
                message=f"Network error - retrying with jitter",
            )

        # Unknown/transient - conservative retry
        wait = base_wait * (2 ** attempt)
        wait = min(wait, CU_POOL_BACKOFF_MAX)
        return ErrorClassification(
            category=category,
            should_retry=attempt < CU_POOL_MAX_RETRIES - 1,  # More conservative
            wait_seconds=wait,
            message=f"Unknown error - conservative retry",
        )

    def _extract_retry_after(self, error: Exception) -> Optional[float]:
        """Extract retry-after value from error if present."""
        error_str = str(error)
        import re
        # Look for "retry after X seconds" or "retry-after: X"
        match = re.search(r'retry.?after[:\s]+(\d+(?:\.\d+)?)', error_str, re.I)
        if match:
            return float(match.group(1))
        return None


# =============================================================================
# Anthropic Connection Pool
# =============================================================================


@dataclass
class ConnectionHealth:
    """Health status for a pooled connection."""
    index: int
    is_healthy: bool = True
    last_used: float = field(default_factory=time.time)
    last_error: Optional[str] = None
    request_count: int = 0
    error_count: int = 0
    avg_latency_ms: float = 0.0
    latency_samples: Deque[float] = field(default_factory=lambda: deque(maxlen=20))

    def record_success(self, latency_ms: float) -> None:
        """Record successful request."""
        self.last_used = time.time()
        self.request_count += 1
        self.latency_samples.append(latency_ms)
        self.avg_latency_ms = sum(self.latency_samples) / len(self.latency_samples)
        self.is_healthy = True

    def record_failure(self, error: str) -> None:
        """Record failed request."""
        self.last_used = time.time()
        self.error_count += 1
        self.last_error = error
        # Mark unhealthy if too many recent errors
        if self.error_count > 3:
            self.is_healthy = False


class AnthropicConnectionPool:
    """
    Advanced connection pool for Anthropic API.

    Features:
    - Multiple connections for parallel requests (70% overhead reduction)
    - Automatic connection health monitoring
    - Load-based connection selection (least-loaded)
    - Rate limiting per pool (not per connection)
    - Adaptive retry with error classification
    - Graceful degradation when connections fail
    """

    def __init__(
        self,
        api_key: str,
        pool_size: int = CU_POOL_SIZE,
        health_check_interval: float = CU_POOL_HEALTH_INTERVAL,
    ):
        self._api_key = api_key
        self._pool_size = pool_size
        self._health_check_interval = health_check_interval

        # Connection pool
        self._connections: List[AsyncAnthropic] = []
        self._health: Dict[int, ConnectionHealth] = {}
        self._load: Dict[int, int] = {}  # Connection index -> active requests

        # Synchronization
        self._pool_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(pool_size * 2)  # Allow 2x concurrency

        # Rate limiting
        self._rate_limiter = TokenBucketRateLimiter()

        # Error handling
        self._error_classifier = ErrorClassifier()

        # Health monitoring
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._shutdown = False

        # Logger
        self._logger = logging.getLogger("jarvis.cu.pool")

    async def initialize(self) -> None:
        """Initialize the connection pool."""
        async with self._pool_lock:
            if self._connections:
                return  # Already initialized

            self._logger.info(f"[POOL] Initializing {self._pool_size} connections...")

            for i in range(self._pool_size):
                try:
                    conn = AsyncAnthropic(api_key=self._api_key)
                    self._connections.append(conn)
                    self._health[i] = ConnectionHealth(index=i)
                    self._load[i] = 0
                    self._logger.debug(f"[POOL] Connection {i} created")
                except Exception as e:
                    self._logger.error(f"[POOL] Failed to create connection {i}: {e}")

            # Start health monitor
            self._health_monitor_task = asyncio.create_task(
                self._health_monitor_loop()
            )

            self._logger.info(
                f"[POOL] Initialized with {len(self._connections)} connections"
            )

    async def shutdown(self) -> None:
        """Shutdown the pool gracefully."""
        self._shutdown = True
        if self._health_monitor_task:
            self._health_monitor_task.cancel()
            try:
                await self._health_monitor_task
            except asyncio.CancelledError:
                pass
        self._logger.info("[POOL] Shutdown complete")

    @asynccontextmanager
    async def acquire(self):
        """
        Acquire a connection from the pool.

        Yields:
            AsyncAnthropic client
        """
        await self._semaphore.acquire()
        conn_idx = -1

        try:
            async with self._pool_lock:
                # Select least-loaded healthy connection
                healthy = [
                    (i, self._load.get(i, 0))
                    for i, h in self._health.items()
                    if h.is_healthy and i < len(self._connections)
                ]

                if not healthy:
                    # Fall back to any connection
                    healthy = [
                        (i, self._load.get(i, 0))
                        for i in range(len(self._connections))
                    ]

                if not healthy:
                    raise RuntimeError("No connections available in pool")

                # Select least loaded
                conn_idx, _ = min(healthy, key=lambda x: x[1])
                self._load[conn_idx] = self._load.get(conn_idx, 0) + 1

            yield self._connections[conn_idx]

        finally:
            self._semaphore.release()
            if conn_idx >= 0:
                async with self._pool_lock:
                    self._load[conn_idx] = max(0, self._load.get(conn_idx, 1) - 1)

    async def execute_with_retry(
        self,
        method: Callable[..., Awaitable[T]],
        *args,
        timeout: float = 60.0,
        **kwargs,
    ) -> T:
        """
        Execute API call with adaptive retry and rate limiting.

        Args:
            method: Async method to call (receives connection as first arg)
            *args: Arguments for method
            timeout: Request timeout
            **kwargs: Keyword arguments for method

        Returns:
            Result from API call

        Raises:
            Exception: If all retries exhausted
        """
        # Wait for rate limit
        if not await self._rate_limiter.acquire(timeout=timeout):
            raise RuntimeError("Rate limit timeout - too many requests")

        last_error: Optional[Exception] = None
        current_timeout = timeout

        for attempt in range(CU_POOL_MAX_RETRIES + 1):
            start_time = time.time()
            conn_idx = -1

            try:
                async with self.acquire() as client:
                    # Find which connection we got
                    try:
                        conn_idx = self._connections.index(client)
                    except ValueError:
                        conn_idx = 0

                    # Execute with timeout
                    result = await asyncio.wait_for(
                        method(client, *args, **kwargs),
                        timeout=current_timeout,
                    )

                    # Record success
                    latency = (time.time() - start_time) * 1000
                    if conn_idx in self._health:
                        self._health[conn_idx].record_success(latency)

                    return result

            except Exception as e:
                last_error = e
                error_str = str(e)

                # Record failure
                if conn_idx >= 0 and conn_idx in self._health:
                    self._health[conn_idx].record_failure(error_str)

                # Classify error
                classification = self._error_classifier.classify(
                    e, attempt=attempt
                )

                self._logger.warning(
                    f"[POOL] Attempt {attempt + 1} failed: "
                    f"{classification.category.value} - {classification.message}"
                )

                if not classification.should_retry:
                    raise

                # Adjust timeout if needed
                if classification.retry_with_longer_timeout:
                    current_timeout = min(current_timeout * 1.5, 120.0)

                # Wait before retry
                if classification.wait_seconds > 0:
                    await asyncio.sleep(classification.wait_seconds)

        raise last_error or RuntimeError("Max retries exceeded")

    async def _health_monitor_loop(self) -> None:
        """Background task to monitor connection health."""
        while not self._shutdown:
            try:
                await asyncio.sleep(self._health_check_interval)

                async with self._pool_lock:
                    for idx, health in self._health.items():
                        # Mark idle connections as healthy
                        if time.time() - health.last_used > 60:
                            health.is_healthy = True
                            health.error_count = 0

                        # Log connection stats
                        self._logger.debug(
                            f"[POOL] Connection {idx}: "
                            f"healthy={health.is_healthy}, "
                            f"requests={health.request_count}, "
                            f"errors={health.error_count}, "
                            f"avg_latency={health.avg_latency_ms:.0f}ms"
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"[POOL] Health monitor error: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get pool statistics."""
        healthy_count = sum(1 for h in self._health.values() if h.is_healthy)
        total_requests = sum(h.request_count for h in self._health.values())
        total_errors = sum(h.error_count for h in self._health.values())

        return {
            "pool_size": self._pool_size,
            "connections": len(self._connections),
            "healthy_connections": healthy_count,
            "total_requests": total_requests,
            "total_errors": total_errors,
            "error_rate": total_errors / max(total_requests, 1),
            "rate_limiter": self._rate_limiter.get_stats(),
        }


# =============================================================================
# Memory-Efficient Screenshot Manager
# =============================================================================


@dataclass
class CachedScreenshot:
    """A cached screenshot with metadata."""
    image_hash: str
    base64_data: str
    timestamp: float
    size_bytes: int
    width: int
    height: int
    # Use weakref to allow GC when memory pressure is high
    _image_ref: Optional[weakref.ref] = None

    @property
    def image(self) -> Optional[Image.Image]:
        """Get image if still in memory."""
        if self._image_ref:
            return self._image_ref()
        return None

    def set_image(self, img: Image.Image) -> None:
        """Set image with weak reference."""
        self._image_ref = weakref.ref(img)


class MemoryEfficientScreenshotManager:
    """
    Memory-efficient screenshot management with LRU cache.

    Features:
    - LRU cache with configurable size
    - Memory pressure detection and cleanup
    - Weak references to allow GC
    - Hash-based deduplication
    - Automatic resource cleanup
    """

    def __init__(
        self,
        cache_size: int = CU_SCREENSHOT_CACHE_SIZE,
        max_memory_mb: int = CU_SCREENSHOT_MAX_MEMORY_MB,
    ):
        self._cache_size = cache_size
        self._max_memory_bytes = max_memory_mb * 1024 * 1024
        self._cache: Dict[str, CachedScreenshot] = {}
        self._access_order: Deque[str] = deque()
        self._lock = asyncio.Lock()
        self._total_bytes = 0
        self._logger = logging.getLogger("jarvis.cu.screenshots")

    async def capture_and_cache(
        self,
        region: Optional[Tuple[int, int, int, int]] = None,
        resize_for_api: bool = True,
        max_dimension: int = 1568,
    ) -> Tuple[Optional[Image.Image], str]:
        """
        Capture screenshot with caching and memory management.

        Args:
            region: Optional region to capture
            resize_for_api: Whether to resize for API limits
            max_dimension: Maximum dimension after resize

        Returns:
            Tuple of (PIL Image or None, base64 string)
        """
        async with self._lock:
            # Capture screenshot
            try:
                screenshot = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: pyautogui.screenshot(region=region) if region else pyautogui.screenshot()
                )
            except Exception as e:
                self._logger.error(f"[SCREENSHOT] Capture failed: {e}")
                raise

            # Process and resize
            if resize_for_api:
                width, height = screenshot.size
                if width > max_dimension or height > max_dimension:
                    ratio = min(max_dimension / width, max_dimension / height)
                    new_size = (int(width * ratio), int(height * ratio))
                    screenshot = screenshot.resize(new_size, Image.Resampling.LANCZOS)

            # Convert to base64
            buffer = io.BytesIO()
            screenshot.save(buffer, format="PNG", optimize=True)
            png_data = buffer.getvalue()
            base64_data = base64.standard_b64encode(png_data).decode("utf-8")

            # Calculate hash for deduplication
            image_hash = hashlib.md5(png_data[:10000]).hexdigest()[:16]

            # Check cache hit
            if image_hash in self._cache:
                self._logger.debug(f"[SCREENSHOT] Cache hit: {image_hash}")
                cached = self._cache[image_hash]
                # Update access order
                if image_hash in self._access_order:
                    self._access_order.remove(image_hash)
                self._access_order.append(image_hash)
                return cached.image, cached.base64_data

            # Create cached entry
            cached = CachedScreenshot(
                image_hash=image_hash,
                base64_data=base64_data,
                timestamp=time.time(),
                size_bytes=len(png_data),
                width=screenshot.width,
                height=screenshot.height,
            )
            cached.set_image(screenshot)

            # Ensure we have space
            await self._ensure_cache_space(len(png_data))

            # Add to cache
            self._cache[image_hash] = cached
            self._access_order.append(image_hash)
            self._total_bytes += len(png_data)

            self._logger.debug(
                f"[SCREENSHOT] Cached: {image_hash} "
                f"({len(png_data) / 1024:.1f}KB, "
                f"{screenshot.width}x{screenshot.height})"
            )

            return screenshot, base64_data

    async def _ensure_cache_space(self, needed_bytes: int) -> None:
        """Ensure cache has space, evicting LRU entries if needed."""
        # Check memory pressure
        while (
            self._total_bytes + needed_bytes > self._max_memory_bytes
            or len(self._cache) >= self._cache_size
        ):
            if not self._access_order:
                break

            # Evict LRU
            oldest_hash = self._access_order.popleft()
            if oldest_hash in self._cache:
                evicted = self._cache.pop(oldest_hash)
                self._total_bytes -= evicted.size_bytes
                self._logger.debug(f"[SCREENSHOT] Evicted: {oldest_hash}")

        # Force GC if memory is high
        if self._total_bytes > self._max_memory_bytes * 0.8:
            gc.collect()

    def clear_cache(self) -> None:
        """Clear all cached screenshots."""
        self._cache.clear()
        self._access_order.clear()
        self._total_bytes = 0
        gc.collect()
        self._logger.info("[SCREENSHOT] Cache cleared")

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return {
            "cache_size": len(self._cache),
            "max_cache_size": self._cache_size,
            "total_bytes": self._total_bytes,
            "max_bytes": self._max_memory_bytes,
            "memory_usage_percent": self._total_bytes / self._max_memory_bytes * 100,
        }


# =============================================================================
# Parallel Action Executor
# =============================================================================


@dataclass
class ActionDependency:
    """Dependency between actions."""
    action_id: str
    depends_on: List[str] = field(default_factory=list)
    conflicts_with: Set[str] = field(default_factory=set)


class ParallelActionExecutor:
    """
    Executes actions in parallel when safe.

    Features:
    - Intelligent parallelization (non-conflicting actions run together)
    - Dependency resolution (ordered execution when needed)
    - Conflict detection (keyboard is single resource)
    - Adaptive batching (groups compatible actions)
    - Real-time progress tracking
    """

    # Action conflict groups - actions in same group can't run in parallel
    CONFLICT_GROUPS = {
        "keyboard": {"type", "key"},  # Keyboard is single resource
        "mouse": {"click", "double_click", "right_click", "drag"},  # Mouse is single
        "scroll": {"scroll"},  # Scroll can conflict with mouse
    }

    def __init__(
        self,
        max_parallel: int = CU_PARALLEL_MAX_ACTIONS,
        conflict_delay: float = CU_PARALLEL_CONFLICT_DELAY,
    ):
        self._max_parallel = max_parallel
        self._conflict_delay = conflict_delay
        self._logger = logging.getLogger("jarvis.cu.parallel")
        self._execution_lock = asyncio.Lock()

    def _get_action_groups(self, action_type: str) -> Set[str]:
        """Get conflict groups for an action type."""
        groups = set()
        for group, types in self.CONFLICT_GROUPS.items():
            if action_type.lower() in types:
                groups.add(group)
        return groups

    def _can_run_parallel(
        self,
        action1_type: str,
        action2_type: str,
    ) -> bool:
        """Check if two actions can run in parallel."""
        groups1 = self._get_action_groups(action1_type)
        groups2 = self._get_action_groups(action2_type)
        return groups1.isdisjoint(groups2)

    def group_by_parallelism(
        self,
        actions: List["ComputerAction"],
    ) -> List[List["ComputerAction"]]:
        """
        Group actions by parallelism tiers.

        Returns list of action groups where actions within a group
        can run in parallel, but groups must run sequentially.
        """
        if not actions:
            return []

        tiers: List[List["ComputerAction"]] = []
        remaining = list(actions)

        while remaining:
            current_tier: List["ComputerAction"] = []
            used_groups: Set[str] = set()
            still_remaining = []

            for action in remaining:
                action_groups = self._get_action_groups(action.action_type.value)

                # Check if conflicts with current tier
                if action_groups.isdisjoint(used_groups):
                    current_tier.append(action)
                    used_groups.update(action_groups)

                    # Limit tier size
                    if len(current_tier) >= self._max_parallel:
                        still_remaining.extend(remaining[remaining.index(action) + 1:])
                        break
                else:
                    still_remaining.append(action)

            tiers.append(current_tier)
            remaining = still_remaining

        return tiers

    async def execute_batch_parallel(
        self,
        actions: List["ComputerAction"],
        executor: "ActionExecutor",
    ) -> List["ActionResult"]:
        """
        Execute actions with intelligent parallelization.

        Args:
            actions: List of actions to execute
            executor: ActionExecutor instance

        Returns:
            List of ActionResults
        """
        if not actions:
            return []

        # Group by parallelism
        tiers = self.group_by_parallelism(actions)
        results: List["ActionResult"] = []

        self._logger.info(
            f"[PARALLEL] Executing {len(actions)} actions in {len(tiers)} tiers"
        )

        for tier_idx, tier in enumerate(tiers):
            tier_start = time.time()

            if len(tier) == 1:
                # Single action - execute directly
                result = await executor.execute(tier[0])
                results.append(result)
            else:
                # Parallel execution with small stagger to avoid exact simultaneous
                async def execute_with_stagger(action, stagger):
                    if stagger > 0:
                        await asyncio.sleep(stagger)
                    return await executor.execute(action)

                tier_results = await asyncio.gather(*[
                    execute_with_stagger(action, i * self._conflict_delay)
                    for i, action in enumerate(tier)
                ], return_exceptions=True)

                # Process results
                for i, result in enumerate(tier_results):
                    if isinstance(result, Exception):
                        results.append(ActionResult(
                            action_id=tier[i].action_id,
                            success=False,
                            error=str(result),
                        ))
                    else:
                        results.append(result)

            tier_time = (time.time() - tier_start) * 1000
            self._logger.debug(
                f"[PARALLEL] Tier {tier_idx + 1}: {len(tier)} actions in {tier_time:.0f}ms"
            )

        return results


# =============================================================================
# Edge Case Handler
# =============================================================================


class EdgeCaseHandler:
    """
    Handles edge cases in Computer Use automation.

    Features:
    - Animation detection and adaptive delays
    - Multi-monitor coordinate mapping
    - Window focus verification
    - Theme awareness (dark/light mode)
    - Network resilience
    """

    def __init__(self):
        self._logger = logging.getLogger("jarvis.cu.edge")
        self._animation_wait_ms = CU_ANIMATION_WAIT_MS
        self._focus_retries = CU_FOCUS_VERIFY_RETRIES
        self._multi_monitor = CU_MULTI_MONITOR_ENABLED
        self._monitor_info: Optional[List[Dict]] = None

    async def wait_for_animation(
        self,
        base_wait_ms: Optional[int] = None,
    ) -> None:
        """Wait for UI animation to complete."""
        wait_ms = base_wait_ms or self._animation_wait_ms
        await asyncio.sleep(wait_ms / 1000)

    async def verify_window_focus(
        self,
        expected_app: Optional[str] = None,
    ) -> bool:
        """
        Verify window focus is correct.

        Args:
            expected_app: Expected app name (optional)

        Returns:
            True if focus is correct
        """
        for attempt in range(self._focus_retries):
            try:
                # Get focused window info via AppleScript
                script = '''
                tell application "System Events"
                    set frontApp to first application process whose frontmost is true
                    return name of frontApp
                end tell
                '''

                proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e", script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()

                if proc.returncode == 0:
                    focused_app = stdout.decode().strip()

                    if expected_app is None:
                        return True

                    if expected_app.lower() in focused_app.lower():
                        return True

                    self._logger.warning(
                        f"[EDGE] Focus mismatch: expected {expected_app}, got {focused_app}"
                    )

            except Exception as e:
                self._logger.debug(f"[EDGE] Focus check failed: {e}")

            if attempt < self._focus_retries - 1:
                await asyncio.sleep(0.2)

        return False

    def map_coordinates_to_monitor(
        self,
        x: int,
        y: int,
        target_monitor: int = 0,
    ) -> Tuple[int, int]:
        """
        Map coordinates to specific monitor.

        Args:
            x: X coordinate
            y: Y coordinate
            target_monitor: Monitor index (0 = primary)

        Returns:
            Adjusted (x, y) coordinates
        """
        if not self._multi_monitor:
            return x, y

        # Get monitor info if not cached
        if self._monitor_info is None:
            try:
                # Get monitor layout from PyAutoGUI
                screens = pyautogui.getAllWindows()
                # For now, return as-is - proper multi-monitor handling
                # would require AppKit or Quartz
                return x, y
            except Exception:
                return x, y

        return x, y

    async def detect_dark_mode(self) -> bool:
        """Detect if macOS is in dark mode."""
        try:
            script = '''
            tell application "System Events"
                tell appearance preferences
                    return dark mode
                end tell
            end tell
            '''

            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                return stdout.decode().strip().lower() == "true"

        except Exception as e:
            self._logger.debug(f"[EDGE] Dark mode detection failed: {e}")

        return False


try:
    from anthropic import Anthropic, AsyncAnthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    Anthropic = None
    AsyncAnthropic = None

logger = logging.getLogger(__name__)


# ============================================================================
# Enums and Types
# ============================================================================

class ActionType(str, Enum):
    """Types of computer actions Claude can execute."""
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    DRAG = "drag"
    TYPE = "type"
    KEY = "key"
    SCROLL = "scroll"
    SCREENSHOT = "screenshot"
    WAIT = "wait"
    CURSOR_POSITION = "cursor_position"


class TaskStatus(str, Enum):
    """Status of a task execution."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    NEEDS_RETRY = "needs_retry"
    NEEDS_HUMAN = "needs_human"


class NarrationEvent(str, Enum):
    """Events for voice narration."""
    STARTING = "starting"
    ANALYZING = "analyzing"
    CLICKING = "clicking"
    TYPING = "typing"
    WAITING = "waiting"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"
    LEARNING = "learning"


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class ComputerAction:
    """A single computer action to execute."""
    action_id: str
    action_type: ActionType
    coordinates: Optional[Tuple[int, int]] = None
    text: Optional[str] = None
    key: Optional[str] = None
    scroll_amount: Optional[int] = None
    duration: float = 0.1
    reasoning: str = ""
    confidence: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "coordinates": self.coordinates,
            "text": self.text,
            "key": self.key,
            "reasoning": self.reasoning,
            "confidence": self.confidence
        }


@dataclass
class ActionResult:
    """Result of an action execution."""
    action_id: str
    success: bool
    result_data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    screenshot_after: Optional[str] = None
    duration_ms: float = 0
    verification_passed: bool = True


@dataclass
class TaskResult:
    """Result of a complete task execution."""
    task_id: str
    goal: str
    status: TaskStatus
    actions_executed: List[ActionResult]
    total_duration_ms: float
    narration_log: List[Dict[str, Any]]
    learning_insights: List[str]
    final_message: str
    confidence: float = 0.0


@dataclass
class VisionAnalysis:
    """Analysis of a screenshot by Claude."""
    analysis_id: str
    description: str
    detected_elements: List[Dict[str, Any]]
    suggested_action: Optional[ComputerAction]
    suggested_actions: List[ComputerAction] = field(default_factory=list)  # NEW: Batch support
    goal_progress: float = 0.0  # 0.0 to 1.0
    is_goal_achieved: bool = False
    reasoning_chain: List[str] = field(default_factory=list)
    confidence: float = 0.0
    is_auth_error: bool = False  # True if API authentication failed
    is_static_interface: bool = False  # NEW: Indicates if interface is static (can batch)


# ============================================================================
# Voice Narration Handler
# ============================================================================

class VoiceNarrationHandler:
    """Handles voice narration for computer use actions."""

    def __init__(
        self,
        tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        enabled: bool = True
    ):
        self.tts_callback = tts_callback
        self.enabled = enabled
        self._narration_log: List[Dict[str, Any]] = []
        self._templates = {
            NarrationEvent.STARTING: "Starting task: {goal}",
            NarrationEvent.ANALYZING: "Analyzing the screen to find {target}",
            NarrationEvent.CLICKING: "Clicking on {target}",
            NarrationEvent.TYPING: "Typing: {text}",
            NarrationEvent.WAITING: "Waiting for {description}",
            NarrationEvent.SUCCESS: "Successfully completed: {description}",
            NarrationEvent.FAILED: "Action failed: {reason}. Let me try another approach.",
            NarrationEvent.RETRYING: "Retrying with alternative approach: {approach}",
            NarrationEvent.LEARNING: "Learning from this: {insight}"
        }

    async def narrate(
        self,
        event: NarrationEvent,
        context: Optional[Dict[str, Any]] = None,
        custom_message: Optional[str] = None
    ) -> None:
        """Generate and speak narration."""
        if not self.enabled:
            return

        context = context or {}

        if custom_message:
            message = custom_message
        else:
            template = self._templates.get(event, "{event}")
            try:
                message = template.format(**context, event=event.value)
            except KeyError:
                message = template

        # Log narration
        log_entry = {
            "event": event.value,
            "message": message,
            "context": context,
            "timestamp": datetime.utcnow().isoformat()
        }
        self._narration_log.append(log_entry)

        logger.info(f"[NARRATION] {message}")

        # Speak if TTS available
        if self.tts_callback:
            try:
                await self.tts_callback(message)
            except Exception as e:
                logger.warning(f"TTS failed: {e}")

    def get_log(self) -> List[Dict[str, Any]]:
        """Get narration log."""
        return self._narration_log.copy()

    def clear_log(self) -> None:
        """Clear narration log."""
        self._narration_log.clear()


# ============================================================================
# Screen Capture Handler
# ============================================================================

class ScreenCaptureHandler:
    """Handles screen capture for Claude Computer Use with async support."""

    def __init__(self, scale_factor: float = 1.0, capture_timeout: float = 10.0):
        self.scale_factor = scale_factor
        self.capture_timeout = capture_timeout
        self._last_screenshot: Optional[Image.Image] = None
        self._screenshot_cache: Dict[str, str] = {}
        self._capture_lock = asyncio.Lock()

    def _capture_sync(
        self,
        region: Optional[Tuple[int, int, int, int]] = None
    ) -> Image.Image:
        """Synchronous screenshot capture (runs in thread pool)."""
        if region:
            return pyautogui.screenshot(region=region)
        return pyautogui.screenshot()

    def _process_image_sync(
        self,
        screenshot: Image.Image,
        resize_for_api: bool = True,
        max_dimension: int = 1568
    ) -> Tuple[Image.Image, str]:
        """Synchronous image processing (runs in thread pool)."""
        # Resize for API if needed (Claude has dimension limits)
        if resize_for_api:
            width, height = screenshot.size
            if width > max_dimension or height > max_dimension:
                ratio = min(max_dimension / width, max_dimension / height)
                new_size = (int(width * ratio), int(height * ratio))
                screenshot = screenshot.resize(new_size, Image.Resampling.LANCZOS)

        # Convert to base64
        buffer = io.BytesIO()
        screenshot.save(buffer, format="PNG", optimize=True)
        base64_image = base64.standard_b64encode(buffer.getvalue()).decode("utf-8")

        return screenshot, base64_image

    async def capture(
        self,
        region: Optional[Tuple[int, int, int, int]] = None,
        resize_for_api: bool = True,
        max_dimension: int = 1568
    ) -> Tuple[Image.Image, str]:
        """
        Capture screenshot and prepare for Claude API.
        Now fully async with timeout protection.

        Returns:
            Tuple of (PIL Image, base64-encoded string)

        Raises:
            asyncio.TimeoutError: If capture times out
        """
        async with self._capture_lock:  # Prevent concurrent captures
            try:
                # Capture screenshot in thread pool with timeout
                screenshot = await run_blocking(
                    self._capture_sync,
                    region,
                    timeout=self.capture_timeout
                )

                self._last_screenshot = screenshot

                # Process image in thread pool (resize + base64 encode)
                processed_screenshot, base64_image = await run_blocking(
                    self._process_image_sync,
                    screenshot,
                    resize_for_api,
                    max_dimension,
                    timeout=self.capture_timeout
                )

                logger.debug(f"[CAPTURE] Screenshot captured and processed successfully")
                return processed_screenshot, base64_image

            except asyncio.TimeoutError:
                logger.error(f"[CAPTURE] Screenshot capture timed out after {self.capture_timeout}s")
                raise
            except Exception as e:
                logger.error(f"[CAPTURE] Screenshot capture failed: {e}")
                raise

    def get_last_screenshot(self) -> Optional[Image.Image]:
        """Get the last captured screenshot."""
        return self._last_screenshot


# ============================================================================
# Action Executor
# ============================================================================

class ActionExecutor:
    """Executes computer actions using PyAutoGUI with full async support."""

    def __init__(
        self,
        scale_factor: float = 1.0,
        safety_pause: float = 0.3,
        movement_duration: float = 0.2,
        action_timeout: float = 10.0
    ):
        self.scale_factor = scale_factor
        self.safety_pause = safety_pause
        self.movement_duration = movement_duration
        self.action_timeout = action_timeout
        self._action_lock = asyncio.Lock()

        # Configure PyAutoGUI
        pyautogui.PAUSE = safety_pause
        pyautogui.FAILSAFE = True

    # ========================================================================
    # Synchronous action methods (run in thread pool)
    # ========================================================================

    def _click_sync(self, x: int, y: int, duration: float) -> None:
        """Synchronous click (runs in thread pool)."""
        pyautogui.moveTo(x, y, duration=duration)
        time.sleep(0.05)
        pyautogui.click(x, y)

    def _double_click_sync(self, x: int, y: int, duration: float) -> None:
        """Synchronous double click (runs in thread pool)."""
        pyautogui.moveTo(x, y, duration=duration)
        pyautogui.doubleClick(x, y)

    def _right_click_sync(self, x: int, y: int, duration: float) -> None:
        """Synchronous right click (runs in thread pool)."""
        pyautogui.moveTo(x, y, duration=duration)
        pyautogui.rightClick(x, y)

    def _type_sync(self, text: str, interval: float = 0.02) -> None:
        """Synchronous typing (runs in thread pool)."""
        pyautogui.write(text, interval=interval)

    def _key_sync(self, key: str) -> None:
        """Synchronous key press (runs in thread pool)."""
        if "+" in key:
            keys = key.split("+")
            pyautogui.hotkey(*keys)
        else:
            pyautogui.press(key)

    def _scroll_sync(self, amount: int, x: Optional[int] = None, y: Optional[int] = None) -> None:
        """Synchronous scroll (runs in thread pool)."""
        if x is not None and y is not None:
            pyautogui.moveTo(x, y)
        pyautogui.scroll(amount)

    def _move_sync(self, x: int, y: int, duration: float) -> None:
        """Synchronous cursor move (runs in thread pool)."""
        pyautogui.moveTo(x, y, duration=duration)

    # ========================================================================
    # Async action methods
    # ========================================================================

    async def execute(self, action: ComputerAction) -> ActionResult:
        """Execute a computer action asynchronously with timeout protection."""
        start_time = time.time()

        async with self._action_lock:  # Prevent concurrent actions
            try:
                if action.action_type == ActionType.CLICK:
                    await self._execute_click(action)
                elif action.action_type == ActionType.DOUBLE_CLICK:
                    await self._execute_double_click(action)
                elif action.action_type == ActionType.RIGHT_CLICK:
                    await self._execute_right_click(action)
                elif action.action_type == ActionType.TYPE:
                    await self._execute_type(action)
                elif action.action_type == ActionType.KEY:
                    await self._execute_key(action)
                elif action.action_type == ActionType.SCROLL:
                    await self._execute_scroll(action)
                elif action.action_type == ActionType.WAIT:
                    await self._execute_wait(action)
                elif action.action_type == ActionType.CURSOR_POSITION:
                    await self._execute_move(action)
                elif action.action_type == ActionType.SCREENSHOT:
                    pass  # Screenshot is handled separately

                duration_ms = (time.time() - start_time) * 1000

                return ActionResult(
                    action_id=action.action_id,
                    success=True,
                    duration_ms=duration_ms
                )

            except asyncio.TimeoutError:
                logger.error(f"Action timed out: {action.action_type.value}")
                return ActionResult(
                    action_id=action.action_id,
                    success=False,
                    error=f"Action timed out after {self.action_timeout}s",
                    duration_ms=(time.time() - start_time) * 1000
                )
            except Exception as e:
                logger.error(f"Action execution failed: {e}")
                return ActionResult(
                    action_id=action.action_id,
                    success=False,
                    error=str(e),
                    duration_ms=(time.time() - start_time) * 1000
                )

    async def _execute_click(self, action: ComputerAction) -> None:
        """Execute click action asynchronously."""
        if not action.coordinates:
            raise ValueError("Click action requires coordinates")

        x, y = self._scale_coordinates(action.coordinates)
        await run_blocking(
            self._click_sync, x, y, self.movement_duration,
            timeout=self.action_timeout
        )

    async def _execute_double_click(self, action: ComputerAction) -> None:
        """Execute double click action asynchronously."""
        if not action.coordinates:
            raise ValueError("Double click action requires coordinates")

        x, y = self._scale_coordinates(action.coordinates)
        await run_blocking(
            self._double_click_sync, x, y, self.movement_duration,
            timeout=self.action_timeout
        )

    async def _execute_right_click(self, action: ComputerAction) -> None:
        """Execute right click action asynchronously."""
        if not action.coordinates:
            raise ValueError("Right click action requires coordinates")

        x, y = self._scale_coordinates(action.coordinates)
        await run_blocking(
            self._right_click_sync, x, y, self.movement_duration,
            timeout=self.action_timeout
        )

    async def _execute_type(self, action: ComputerAction) -> None:
        """Execute type action asynchronously."""
        if not action.text:
            raise ValueError("Type action requires text")

        # Typing can take a while for long text, extend timeout
        text_timeout = max(self.action_timeout, len(action.text) * 0.05)
        await run_blocking(
            self._type_sync, action.text, 0.02,
            timeout=text_timeout
        )

    async def _execute_key(self, action: ComputerAction) -> None:
        """Execute key press action asynchronously."""
        if not action.key:
            raise ValueError("Key action requires key")

        await run_blocking(
            self._key_sync, action.key,
            timeout=self.action_timeout
        )

    async def _execute_scroll(self, action: ComputerAction) -> None:
        """Execute scroll action asynchronously."""
        amount = action.scroll_amount or 3
        x, y = None, None

        if action.coordinates:
            x, y = self._scale_coordinates(action.coordinates)

        await run_blocking(
            self._scroll_sync, amount, x, y,
            timeout=self.action_timeout
        )

    async def _execute_wait(self, action: ComputerAction) -> None:
        """Execute wait action (truly async, no thread pool needed)."""
        await asyncio.sleep(action.duration)

    async def _execute_move(self, action: ComputerAction) -> None:
        """Execute cursor move action asynchronously."""
        if not action.coordinates:
            raise ValueError("Move action requires coordinates")

        x, y = self._scale_coordinates(action.coordinates)
        await run_blocking(
            self._move_sync, x, y, self.movement_duration,
            timeout=self.action_timeout
        )

    def _scale_coordinates(self, coords: Tuple[int, int]) -> Tuple[int, int]:
        """Scale coordinates for Retina displays if needed."""
        x, y = coords
        return (int(x * self.scale_factor), int(y * self.scale_factor))


# ============================================================================
# Claude Computer Use Connector
# ============================================================================

class ClaudeComputerUseConnector:
    """
    Main connector for Claude Computer Use API.

    Provides vision-based, dynamic UI automation without hardcoded coordinates.
    Integrates with voice narration for transparency.
    """

    # Claude Computer Use model
    COMPUTER_USE_MODEL = "claude-sonnet-4-20250514"

    # System prompt for computer use - Enhanced with Open Interpreter patterns
    SYSTEM_PROMPT = """You are JARVIS, an AI assistant helping to control a macOS computer.
You can see the screen through screenshots and execute actions to help the user.

*** ACTION CHAINING OPTIMIZATION (Clinical-Grade Speed) ***
CRITICAL: You can and SHOULD chain multiple actions in a single response for static interfaces.

STATIC INTERFACE DETECTION:
- Calculators, Forms, Dialogs, Menus, Keypads - These do NOT change between actions
- Example: A calculator keypad stays static whether you click '2', '+', or '='
- If the interface is STATIC, send ALL actions as a JSON array in ONE response

EFFICIENCY RULE:
- If you see a calculator and need to compute "2+2", do NOT send one action at a time
- INSTEAD: Send all 4 clicks as a batch: [click(2), click(+), click(2), click(=)]
- This reduces task time from ~10s to ~2s (5x speedup)

WHEN TO BATCH:
✅ Static UI: Calculators, forms, dialogs, control panels, settings screens
✅ Predictable sequences: Type text, navigate menus, fill forms
✅ Same app/window: Actions within one stable interface
❌ Dynamic UI: Web pages that reload, animated transitions, async loading states
❌ Cross-app: Switching between applications
❌ Uncertain outcomes: Actions where next step depends on unknown result

BATCH FORMAT:
- Single action: Return one JSON object as usual
- Multiple actions: Return JSON array with "batch": true flag:
```json
{
    "batch": true,
    "actions": [
        {"action_type": "click", "coordinates": [x1, y1], "reasoning": "..."},
        {"action_type": "click", "coordinates": [x2, y2], "reasoning": "..."},
        {"action_type": "click", "coordinates": [x3, y3], "reasoning": "..."}
    ],
    "interface_type": "static",
    "expected_duration_ms": 500
}
```

*** COORDINATE EXTRACTION (Open Interpreter Grid Pattern) ***
When identifying click targets, use a mental grid overlay for improved accuracy:

1. GRID SYSTEM:
   - Mentally divide the screen into a 10x10 grid
   - Grid position (0,0) = top-left corner
   - Grid position (9,9) = bottom-right corner
   - Each grid cell is approximately 1/10th of the screen dimension

2. COORDINATE CALCULATION:
   - Identify target element's grid position visually
   - Example: A button in the center-right area might be at grid (8, 5)
   - Convert: For 2560x1440 screen, (8,5) ≈ (2176, 792) pixels
   - Formula: pixel_x = (grid_x + 0.5) * (screen_width / 10)
   - Formula: pixel_y = (grid_y + 0.5) * (screen_height / 10)

3. RETINA DISPLAY AWARENESS:
   - macOS often uses Retina scaling (2x)
   - If UI elements appear larger than expected, coordinates may need adjustment
   - pyautogui typically handles this automatically, but be aware of it

*** ACTION EXECUTION (Open Interpreter Refined Pattern) ***
When executing actions:

1. MOUSE MOVEMENT:
   - Always move cursor smoothly (duration: 0.2s minimum)
   - Hover briefly (0.1s) before clicking to verify target
   - This prevents "missed clicks" from too-fast movement

2. CLICK ACTIONS:
   - Single click: Move → Hover (0.1s) → Click → Wait (0.5s for UI response)
   - Double click: Move → Hover → Double click → Wait (0.5s)
   - Right click: Move → Hover → Right click → Wait (0.5s)

3. TYPING ACTIONS:
   - For text fields: Click to focus first, wait 0.2s
   - Clear field if needed: Select all (Cmd+A) → Delete
   - Type with natural intervals (0.05s per character)
   - Wait 0.3s after typing for UI to process

4. VERIFICATION:
   - After EVERY action, wait 0.5s for UI response
   - Check next screenshot to verify action succeeded
   - If verification fails, try with adjusted coordinates (±10px)

*** ERROR RECOVERY (Open Interpreter Pattern) ***
If an action fails:

1. First Retry (immediately):
   - Adjust coordinates by ±10px from original estimate
   - Try again with same method

2. Second Retry (after 0.5s):
   - Try alternative method (e.g., keyboard shortcut instead of click)
   - Use different UI element if available

3. Third Retry (after 1s):
   - Re-analyze screenshot completely (UI may have changed)
   - Generate new plan based on current state
   - Report if consistently failing

*** CRITICAL: FULL SCREEN MODE DETECTION - CHECK THIS FIRST ***
The user often runs applications in FULL SCREEN MODE where the macOS menu bar is HIDDEN.

STEP 1 - ALWAYS CHECK FIRST: Look at the very top of the screenshot:
- If you see a menu bar (clock, wifi icon, Control Center icon in top-right): Menu bar is VISIBLE, proceed normally
- If the top of the screen shows ONLY the application content (no menu bar, no system icons): You are in FULL SCREEN MODE

STEP 2 - IF IN FULL SCREEN MODE (no menu bar visible):
1. IMMEDIATELY move the mouse cursor to y=0 (the very top edge of the screen)
   - Use grid position (5, 0) = approximately (screen_width/2, 0)
   - This action REVEALS the hidden menu bar in macOS full screen mode
2. Wait 0.5-1 second for the menu bar to animate into view
3. Take a NEW screenshot to see the revealed menu bar
4. NOW you can see and click on Control Center

STEP 3 - Once menu bar is visible:
- The Control Center icon is in the top-right, looks like two toggle switches/sliders
- Grid position approximately (9.5, 0) for a 2560-wide screen
- Click it to open Control Center
- Find and click "Screen Mirroring" (two overlapping screens icon)
- Select the target display from the list

When analyzing screenshots:
1. FIRST: Check if menu bar is visible (see above)
2. Describe what you see clearly using grid-based positioning
3. Identify UI elements by visual appearance, NOT memorized positions
4. Locate elements dynamically based on current screen content
5. Use visual landmarks for relative positioning ("100px left of X")

For macOS Control Center:
- The Control Center icon is in the top-right menu bar (grid ~9, 0)
- It looks like two overlapping rectangles (toggle switches)
- After clicking, Control Center panel appears with various controls
- Screen Mirroring shows two overlapping screens icon
- AirPlay devices are listed when Screen Mirroring is expanded

Remember: If you cannot see the menu bar, you MUST first move the cursor to y=0 to reveal it!

Always provide your reasoning before taking action, including grid position estimates."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        learning_callback: Optional[Callable[[Dict], Awaitable[None]]] = None,
        scale_factor: float = 1.0,
        max_actions_per_task: int = 20,
        action_timeout: float = 30.0,
        api_timeout: float = 60.0
    ):
        if not ANTHROPIC_AVAILABLE:
            raise ImportError("anthropic package required. Install with: pip install anthropic")

        # v78.2: Use unified SecretManager for multi-backend API key resolution
        # Priority: 1) Explicit param 2) GCP Secret Manager 3) macOS Keychain 4) Environment
        self.api_key = api_key
        if not self.api_key:
            self.api_key = self._resolve_api_key_intelligent()
        if not self.api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY required. Configure via: "
                "1) GCP Secret Manager (anthropic-api-key) "
                "2) macOS Keychain (JARVIS/ANTHROPIC_API_KEY) "
                "3) Environment variable (ANTHROPIC_API_KEY)"
            )

        self.client = AsyncAnthropic(api_key=self.api_key)
        self.max_actions_per_task = max_actions_per_task
        self.action_timeout = action_timeout
        self.api_timeout = api_timeout

        # Initialize components with proper timeout settings
        self.narrator = VoiceNarrationHandler(tts_callback=tts_callback)
        self.screen_capture = ScreenCaptureHandler(
            scale_factor=scale_factor,
            capture_timeout=10.0
        )
        self.action_executor = ActionExecutor(
            scale_factor=scale_factor,
            action_timeout=action_timeout
        )
        self.learning_callback = learning_callback

        # Circuit breaker for API calls
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=60.0
        )

        # State tracking
        self._current_task_id: Optional[str] = None
        self._action_history: List[ComputerAction] = []
        self._learned_positions: Dict[str, Tuple[int, int]] = {}
        self._auth_failed: bool = False  # Track API authentication failures

        # Load learned positions
        self._load_learned_positions()

        # v6.0: Open Interpreter pattern integrations

        # v6.2: OmniParser integration with intelligent fallback (enabled by default)
        self._omniparser_engine: Optional[Any] = None
        self._omniparser_enabled = os.getenv("OMNIPARSER_ENABLED", "true").lower() == "true"

        if self._omniparser_enabled:
            try:
                from backend.vision.omniparser_integration import get_omniparser_engine
                logger.info("[OMNIPARSER] ✅ OmniParser enabled with intelligent fallback modes")
                logger.info("[OMNIPARSER] Modes: OmniParser → Claude Vision → OCR (auto-select)")
                # Initialize in background (don't block startup) - only if event loop is running
                try:
                    loop = asyncio.get_running_loop()
                    asyncio.create_task(self._initialize_omniparser())
                except RuntimeError:
                    # No running event loop - will initialize lazily on first use
                    logger.debug("[OMNIPARSER] No event loop running, will initialize lazily")
            except ImportError:
                logger.info("[OMNIPARSER] Import failed - using standard vision")
                self._omniparser_enabled = False

        # v6.1: Cross-repo Computer Use bridge for action tracking and optimization
        self._computer_use_bridge: Optional[Any] = None
        self._bridge_enabled = os.getenv("COMPUTER_USE_BRIDGE_ENABLED", "true").lower() == "true"

        if self._bridge_enabled:
            try:
                from backend.core.computer_use_bridge import get_computer_use_bridge
                logger.info("[COMPUTER USE BRIDGE] Initializing cross-repo bridge...")
                # Initialize in background - only if event loop is running
                try:
                    loop = asyncio.get_running_loop()
                    asyncio.create_task(self._initialize_bridge())
                except RuntimeError:
                    # No running event loop - will initialize lazily on first use
                    logger.debug("[COMPUTER USE BRIDGE] No event loop running, will initialize lazily")
            except ImportError:
                logger.info("[COMPUTER USE BRIDGE] Bridge not available")
                self._bridge_enabled = False

        self._safe_code_executor = None
        self._coordinate_extractor = None
        self._safety_monitor = None
        self._refinements_initialized = False

        logger.info("[COMPUTER USE] Claude Computer Use Connector initialized with async support")

    @staticmethod
    def _resolve_api_key_intelligent() -> Optional[str]:
        """
        Intelligently resolve ANTHROPIC_API_KEY using multi-backend fallback.

        Resolution order (highest to lowest priority):
        1. GCP Secret Manager (anthropic-api-key) - Production
        2. macOS Keychain (JARVIS/ANTHROPIC_API_KEY) - Local dev
        3. Environment variable (ANTHROPIC_API_KEY) - CI/CD fallback
        4. .env file loading as last resort

        Returns:
            API key string or None if not found in any backend
        """
        import os

        # Fast path: Check environment first (most common case)
        env_key = os.environ.get("ANTHROPIC_API_KEY")
        if env_key:
            logger.debug("[API KEY] Resolved from environment variable")
            return env_key

        # Try unified SecretManager for multi-backend resolution
        try:
            from backend.core.secret_manager import get_secret

            # Try GCP-style secret name first
            secret = get_secret("anthropic-api-key")
            if secret:
                # Also cache in environment for faster subsequent access
                os.environ["ANTHROPIC_API_KEY"] = secret
                logger.info("[API KEY] Resolved from SecretManager (GCP/Keychain)")
                return secret

            # Try environment-style name as fallback
            secret = get_secret("ANTHROPIC_API_KEY")
            if secret:
                os.environ["ANTHROPIC_API_KEY"] = secret
                logger.info("[API KEY] Resolved from SecretManager (env-style)")
                return secret

        except ImportError:
            logger.debug("[API KEY] SecretManager not available")
        except Exception as e:
            logger.debug(f"[API KEY] SecretManager error: {e}")

        # Last resort: Try loading from .env files
        try:
            from pathlib import Path

            # Find .env files in common locations
            search_paths = [
                Path(__file__).parent.parent / ".env",  # backend/.env
                Path(__file__).parent.parent.parent / ".env",  # project root/.env
                Path.home() / ".jarvis" / ".env",  # ~/.jarvis/.env
            ]

            for env_path in search_paths:
                if env_path.exists():
                    # Parse .env file manually (no dotenv dependency required)
                    with open(env_path) as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("ANTHROPIC_API_KEY="):
                                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                                if key:
                                    os.environ["ANTHROPIC_API_KEY"] = key
                                    logger.info(f"[API KEY] Loaded from {env_path}")
                                    return key
        except Exception as e:
            logger.debug(f"[API KEY] .env loading failed: {e}")

        logger.warning("[API KEY] Could not resolve ANTHROPIC_API_KEY from any backend")
        return None

    async def _ensure_refinements_initialized(self) -> bool:
        """Lazily initialize Open Interpreter refinement components."""
        if self._refinements_initialized:
            return self._safe_code_executor is not None

        try:
            from backend.intelligence.computer_use_refinements import (
                SafeCodeExecutor,
                CoordinateExtractor,
                SafetyMonitor,
                ComputerUseConfig,
            )

            config = ComputerUseConfig()
            self._safe_code_executor = SafeCodeExecutor(config)
            self._coordinate_extractor = CoordinateExtractor(config)
            self._safety_monitor = SafetyMonitor(config, strict_mode=True)

            # Calibrate coordinate extractor
            await self._coordinate_extractor.calibrate()

            self._refinements_initialized = True
            logger.info("[COMPUTER USE] Open Interpreter refinements initialized")
            return True

        except ImportError:
            logger.debug("[COMPUTER USE] computer_use_refinements not available")
            self._refinements_initialized = True
            return False
        except Exception as e:
            logger.debug(f"[COMPUTER USE] Refinements initialization failed: {e}")
            self._refinements_initialized = True
            return False

    async def execute_code_safely(
        self,
        code: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute Python code safely using the SafeCodeExecutor.

        This is the Open Interpreter "Safe Execute" pattern that prevents
        JARVIS from accidentally running `rm -rf /`.

        Args:
            code: Python code to execute
            context: Optional context variables

        Returns:
            Dict with execution result
        """
        if not await self._ensure_refinements_initialized():
            return {
                "success": False,
                "error": "Safe code executor not available",
            }

        if not self._safe_code_executor:
            return {
                "success": False,
                "error": "Safe code executor not initialized",
            }

        result = await self._safe_code_executor.execute(code, context)

        return {
            "success": result.success,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "execution_time_ms": result.execution_time_ms,
            "blocked_reason": result.blocked_reason,
        }

    def get_enhanced_system_prompt(self, spatial_context: Optional[str] = None) -> str:
        """
        Get system prompt enhanced with dynamic grid information and spatial context.

        This provides the LLM with:
        1. Accurate screen-specific grid calculations
        2. 3D OS Awareness (which Space, which Window, where apps are)

        Args:
            spatial_context: Optional pre-computed spatial context string

        Returns:
            Enhanced system prompt with all context injected
        """
        base_prompt = self.SYSTEM_PROMPT

        # Add dynamic grid information if coordinate extractor is available
        if self._coordinate_extractor and self._coordinate_extractor._calibrated:
            grid_section = self._coordinate_extractor.get_grid_prompt_section()
            base_prompt = f"{base_prompt}\n\n*** DYNAMIC SCREEN CALIBRATION ***\n{grid_section}"

        # v6.2: Add 3D OS Awareness (Spatial Context / Proprioception)
        if spatial_context:
            spatial_section = f"""
*** 3D OS AWARENESS (Proprioception) ***
You have spatial awareness of the entire macOS desktop. Current context:
{spatial_context}

SPATIAL RULES:
- Before clicking on any app, verify it's on the CURRENT space
- If the target app is on a different space, I will switch to it automatically
- Use this context to understand what's visible vs hidden
- Never try to click on windows that aren't on the current space
"""
            base_prompt = f"{base_prompt}\n{spatial_section}"

        return base_prompt

    async def get_current_spatial_context(self) -> Optional[str]:
        """
        Get current spatial context for prompt injection.

        Returns:
            Formatted spatial context string or None if unavailable
        """
        try:
            from core.computer_use_bridge import get_current_context
            context = await get_current_context()
            if context:
                return context.get_context_prompt()
        except Exception as e:
            logger.debug(f"[COMPUTER USE] Could not get spatial context: {e}")
        return None

    def _load_learned_positions(self) -> None:
        """Load previously learned UI element positions."""
        cache_file = Path.home() / ".jarvis" / "learned_ui_positions.json"
        try:
            if cache_file.exists():
                with open(cache_file) as f:
                    self._learned_positions = json.load(f)
                logger.info(f"[COMPUTER USE] Loaded {len(self._learned_positions)} learned positions")
        except Exception as e:
            logger.warning(f"Could not load learned positions: {e}")

    def _save_learned_position(self, element_name: str, coords: Tuple[int, int]) -> None:
        """Save a learned position for future use."""
        self._learned_positions[element_name] = coords
        cache_file = Path.home() / ".jarvis" / "learned_ui_positions.json"
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w") as f:
                json.dump(self._learned_positions, f)
        except Exception as e:
            logger.warning(f"Could not save learned position: {e}")

    async def _initialize_omniparser(self) -> None:
        """Initialize OmniParser engine in background."""
        try:
            from backend.vision.omniparser_integration import get_omniparser_engine
            from backend.vision.omniparser_core import ParserMode

            self._omniparser_engine = await get_omniparser_engine()
            if self._omniparser_engine and self._omniparser_engine._model:
                mode = self._omniparser_engine._model.get_current_mode()
                stats = self._omniparser_engine._model.get_statistics()

                logger.info(f"[OMNIPARSER] ✅ Engine initialized successfully")
                logger.info(f"[OMNIPARSER] Active Mode: {mode.value}")
                logger.info(f"[OMNIPARSER] Cache: {stats['cache_size']} entries")

                if mode == ParserMode.OMNIPARSER:
                    logger.info("[OMNIPARSER] 🚀 Using OmniParser (fastest, most accurate)")
                elif mode == ParserMode.CLAUDE_VISION:
                    logger.info("[OMNIPARSER] 🔄 Using Claude Vision fallback (good accuracy)")
                elif mode == ParserMode.OCR_TEMPLATE:
                    logger.info("[OMNIPARSER] 📝 Using OCR fallback (basic)")
                else:
                    logger.info("[OMNIPARSER] ⚠️  No parsers available (disabled)")

            else:
                logger.warning("[OMNIPARSER] Engine initialization returned None")
                self._omniparser_enabled = False
        except Exception as e:
            logger.error(f"[OMNIPARSER] Failed to initialize: {e}")
            import traceback
            traceback.print_exc()
            self._omniparser_enabled = False

    async def _initialize_bridge(self) -> None:
        """Initialize Computer Use bridge in background."""
        try:
            from backend.core.computer_use_bridge import get_computer_use_bridge
            self._computer_use_bridge = await get_computer_use_bridge(
                enable_action_chaining=True,
                enable_omniparser=self._omniparser_enabled,
            )
            if self._computer_use_bridge:
                logger.info("[COMPUTER USE BRIDGE] ✅ Cross-repo bridge initialized successfully")
                stats = self._computer_use_bridge.get_statistics()
                logger.info(f"[COMPUTER USE BRIDGE] Statistics: {stats}")
            else:
                logger.warning("[COMPUTER USE BRIDGE] Bridge initialization returned None")
                self._bridge_enabled = False
        except Exception as e:
            logger.error(f"[COMPUTER USE BRIDGE] Failed to initialize: {e}")
            self._bridge_enabled = False

    async def execute_task(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
        narrate: bool = True
    ) -> TaskResult:
        """
        Execute a complete task using Claude Computer Use.

        Args:
            goal: Natural language description of what to accomplish
            context: Additional context for the task
            narrate: Whether to provide voice narration

        Returns:
            TaskResult with complete execution details
        """
        task_id = str(uuid4())
        self._current_task_id = task_id
        start_time = time.time()
        actions_executed: List[ActionResult] = []
        learning_insights: List[str] = []

        # Early return if auth has already failed
        if self._auth_failed:
            logger.warning("[COMPUTER USE] Skipping task - API authentication previously failed")
            return TaskResult(
                task_id=task_id,
                goal=goal,
                status=TaskStatus.FAILED,
                actions_executed=[],
                total_duration_ms=0,
                narration_log=[],
                learning_insights=[],
                final_message="API authentication failed - please check ANTHROPIC_API_KEY",
                confidence=0.0
            )

        self.narrator.enabled = narrate
        self.narrator.clear_log()

        await self.narrator.narrate(
            NarrationEvent.STARTING,
            {"goal": goal}
        )

        try:
            # Main execution loop
            action_count = 0
            goal_achieved = False

            while action_count < self.max_actions_per_task and not goal_achieved:
                action_count += 1

                # Capture current screen state
                await self.narrator.narrate(
                    NarrationEvent.ANALYZING,
                    {"target": "the current screen state"}
                )

                screenshot, base64_screenshot = await self.screen_capture.capture()

                # Get Claude's analysis and suggested action
                analysis = await self._analyze_and_decide(
                    goal=goal,
                    screenshot_base64=base64_screenshot,
                    action_history=self._action_history[-5:],  # Last 5 actions
                    context=context
                )

                # Check for authentication errors - bail out immediately
                if analysis.is_auth_error:
                    logger.error("[COMPUTER USE] ❌ Authentication failed - stopping task")
                    await self.narrator.narrate(
                        NarrationEvent.FAILED,
                        {"reason": "API authentication failed - invalid API key"}
                    )
                    return TaskResult(
                        task_id=task_id,
                        goal=goal,
                        status=TaskStatus.FAILED,
                        actions_executed=actions_executed,
                        total_duration_ms=(time.time() - start_time) * 1000,
                        narration_log=self.narrator.get_log(),
                        learning_insights=learning_insights,
                        final_message="API authentication failed - please check ANTHROPIC_API_KEY",
                        confidence=0.0
                    )

                # Check if goal achieved
                if analysis.is_goal_achieved:
                    goal_achieved = True
                    await self.narrator.narrate(
                        NarrationEvent.SUCCESS,
                        {"description": goal}
                    )
                    break

                # Execute suggested action(s) - NOW WITH BATCH SUPPORT
                if analysis.suggested_actions:
                    # BATCH MODE: Execute all actions in rapid succession
                    actions_to_execute = analysis.suggested_actions
                    batch_size = len(actions_to_execute)

                    if batch_size > 1:
                        logger.info(f"[ACTION CHAINING] Executing batch of {batch_size} actions")
                        await self.narrator.narrate(
                            NarrationEvent.ANALYZING,
                            {"target": f"batch of {batch_size} actions for {goal}"}
                        )

                    # Reset no-action counter since we have actions
                    self._no_action_count = 0

                    # Execute all actions in the batch
                    batch_start_time = time.time()
                    for i, action in enumerate(actions_to_execute):
                        # Narrate the action (but suppress for batches > 3 to avoid spam)
                        if batch_size <= 3 or i == 0 or i == batch_size - 1:
                            await self._narrate_action(action)
                        elif i == 1:
                            logger.info(f"[ACTION CHAINING] Executing actions 2-{batch_size-1} silently...")

                        # Execute
                        result = await self.action_executor.execute(action)
                        actions_executed.append(result)
                        self._action_history.append(action)

                        if result.success:
                            # Learn from successful action
                            if action.coordinates and action.reasoning:
                                element_hint = self._extract_element_name(action.reasoning)
                                if element_hint:
                                    self._save_learned_position(element_hint, action.coordinates)
                                    learning_insights.append(
                                        f"Learned position for '{element_hint}': {action.coordinates}"
                                    )

                            # OPTIMIZATION: Only wait between actions, not after last one
                            if i < batch_size - 1:
                                # Small delay between batch actions for UI responsiveness
                                await asyncio.sleep(0.1)  # 100ms vs 500ms - 5x faster!
                        else:
                            await self.narrator.narrate(
                                NarrationEvent.FAILED,
                                {"reason": result.error or "Unknown error"}
                            )
                            # Don't break batch on single failure - try remaining actions
                            logger.warning(f"[ACTION CHAINING] Action {i+1}/{batch_size} failed, continuing batch")

                    batch_duration_ms = (time.time() - batch_start_time) * 1000

                    if batch_size > 1:
                        logger.info(
                            f"[ACTION CHAINING] ✅ Completed batch of {batch_size} actions in {batch_duration_ms:.0f}ms "
                            f"(~{batch_duration_ms/batch_size:.0f}ms per action)"
                        )

                        # v6.1: Emit batch event to cross-repo bridge
                        if self._bridge_enabled and self._computer_use_bridge:
                            try:
                                from backend.core.computer_use_bridge import ActionBatch, InterfaceType, ExecutionStatus

                                # Calculate savings (assume 2s per Stop-and-Look cycle)
                                stop_and_look_time_ms = batch_size * 2000  # 2s per action
                                time_saved_ms = stop_and_look_time_ms - batch_duration_ms

                                # Token savings: batching reduces tokens by ~70% (single screenshot vs N screenshots)
                                avg_tokens_per_screenshot = 1500  # Approximate
                                tokens_saved = int((batch_size - 1) * avg_tokens_per_screenshot * 0.7)

                                batch = ActionBatch(
                                    batch_id=f"{analysis.analysis_id}-batch",
                                    actions=actions_to_execute,
                                    interface_type=InterfaceType.STATIC if analysis.is_static_interface else InterfaceType.DYNAMIC,
                                    goal=goal,
                                )

                                await self._computer_use_bridge.emit_batch_event(
                                    batch=batch,
                                    status=ExecutionStatus.COMPLETED,
                                    execution_time_ms=batch_duration_ms,
                                    time_saved_ms=time_saved_ms,
                                    tokens_saved=tokens_saved,
                                )
                            except Exception as e:
                                logger.warning(f"[COMPUTER USE BRIDGE] Failed to emit batch event: {e}")

                    # Wait for UI to update ONCE after entire batch
                    await asyncio.sleep(0.3)  # Shorter wait since actions were fast

                elif analysis.suggested_action:
                    # LEGACY: Single action mode (backward compatibility)
                    action = analysis.suggested_action

                    # Reset no-action counter since we have an action
                    self._no_action_count = 0

                    # Narrate the action
                    await self._narrate_action(action)

                    # Execute
                    result = await self.action_executor.execute(action)
                    actions_executed.append(result)
                    self._action_history.append(action)

                    if result.success:
                        # Learn from successful action
                        if action.coordinates and action.reasoning:
                            element_hint = self._extract_element_name(action.reasoning)
                            if element_hint:
                                self._save_learned_position(element_hint, action.coordinates)
                                learning_insights.append(
                                    f"Learned position for '{element_hint}': {action.coordinates}"
                                )

                        # Wait for UI to update
                        await asyncio.sleep(0.5)
                    else:
                        await self.narrator.narrate(
                            NarrationEvent.FAILED,
                            {"reason": result.error or "Unknown error"}
                        )

                else:
                    # No action suggested - task might be complete or Claude is stuck
                    logger.warning("[COMPUTER USE] No action suggested by Claude")
                    # If no action for 2 consecutive turns, assume task is complete
                    if not hasattr(self, '_no_action_count'):
                        self._no_action_count = 0
                    self._no_action_count += 1

                    if self._no_action_count >= 2:
                        logger.info("[COMPUTER USE] No actions for 2 turns - assuming task complete")
                        goal_achieved = True
                        break
                    await asyncio.sleep(0.5)

            # Determine final status
            if goal_achieved:
                status = TaskStatus.SUCCESS
                final_message = f"Successfully completed: {goal}"
            elif action_count >= self.max_actions_per_task:
                status = TaskStatus.NEEDS_HUMAN
                final_message = f"Reached maximum actions ({self.max_actions_per_task}) without completing goal"
            else:
                status = TaskStatus.FAILED
                final_message = "Task failed to complete"

            # Store learning
            if self.learning_callback and learning_insights:
                await self.learning_callback({
                    "task_id": task_id,
                    "goal": goal,
                    "insights": learning_insights,
                    "success": goal_achieved
                })

            total_duration_ms = (time.time() - start_time) * 1000

            return TaskResult(
                task_id=task_id,
                goal=goal,
                status=status,
                actions_executed=actions_executed,
                total_duration_ms=total_duration_ms,
                narration_log=self.narrator.get_log(),
                learning_insights=learning_insights,
                final_message=final_message,
                confidence=0.9 if goal_achieved else 0.3
            )

        except Exception as e:
            logger.error(f"[COMPUTER USE] Task execution failed: {e}")
            await self.narrator.narrate(
                NarrationEvent.FAILED,
                {"reason": str(e)}
            )

            return TaskResult(
                task_id=task_id,
                goal=goal,
                status=TaskStatus.FAILED,
                actions_executed=actions_executed,
                total_duration_ms=(time.time() - start_time) * 1000,
                narration_log=self.narrator.get_log(),
                learning_insights=learning_insights,
                final_message=f"Task failed with error: {str(e)}",
                confidence=0.0
            )

    async def _analyze_and_decide(
        self,
        goal: str,
        screenshot_base64: str,
        action_history: List[ComputerAction],
        context: Optional[Dict[str, Any]] = None
    ) -> VisionAnalysis:
        """
        Analyze screenshot and decide on next action using Claude.
        Now with circuit breaker and timeout protection.
        """
        # Check circuit breaker first
        if not await self._circuit_breaker.can_execute():
            logger.warning("[COMPUTER USE] Circuit breaker open - skipping API call")
            return VisionAnalysis(
                analysis_id=str(uuid4()),
                description="API temporarily unavailable (circuit breaker open)",
                detected_elements=[],
                suggested_action=None,
                goal_progress=0.0,
                is_goal_achieved=False,
                reasoning_chain=["Circuit breaker is open - API calls temporarily blocked"],
                confidence=0.0,
                is_auth_error=False
            )

        # Build conversation history
        history_text = ""
        if action_history:
            history_text = "\n\nPrevious actions in this task:\n"
            for i, action in enumerate(action_history, 1):
                history_text += f"{i}. {action.action_type.value}"
                if action.coordinates:
                    history_text += f" at {action.coordinates}"
                history_text += f" - {action.reasoning}\n"

        context_text = ""
        if context:
            context_text = f"\n\nAdditional context: {json.dumps(context)}"

        # v6.2: Get spatial context for 3D OS Awareness
        spatial_context = await self.get_current_spatial_context()

        # Build the prompt
        user_prompt = f"""Goal: {goal}
{history_text}{context_text}

Please analyze the current screenshot and determine:
1. What do you see on the screen?
2. Is this a STATIC interface (calculator, form, dialog) or DYNAMIC (web page, app that changes)?
3. How close are we to achieving the goal? (0-100%)
4. Is the goal already achieved?
5. What action(s) should we take next?

*** ACTION RESPONSE FORMAT ***

OPTION A - STATIC INTERFACE (Use this when possible for 5x speedup):
If the interface is STATIC and you can see all the buttons/elements needed, send a BATCH of actions:
```json
{{
    "batch": true,
    "interface_type": "static",
    "actions": [
        {{"action_type": "click", "coordinates": [x1, y1], "reasoning": "Click button 2"}},
        {{"action_type": "click", "coordinates": [x2, y2], "reasoning": "Click + operator"}},
        {{"action_type": "click", "coordinates": [x3, y3], "reasoning": "Click button 2"}},
        {{"action_type": "click", "coordinates": [x4, y4], "reasoning": "Click = to get result"}}
    ],
    "expected_duration_ms": 500
}}
```

OPTION B - SINGLE ACTION (Use for dynamic interfaces or uncertain outcomes):
```json
{{
    "action_type": "click|double_click|right_click|type|key|scroll|wait",
    "coordinates": [x, y],  // For click actions, precise pixel coordinates
    "text": "...",  // For type actions
    "key": "...",  // For key actions (e.g., "return", "command+c")
    "scroll_amount": 3,  // For scroll actions
    "duration": 1.0,  // For wait actions (seconds)
    "reasoning": "Why this action will help achieve the goal"
}}
```

**PREFER OPTION A (batch) whenever the interface is static!**

Respond with your analysis followed by the action JSON."""

        try:
            # v6.2: Use enhanced system prompt with 3D OS Awareness
            system_prompt = self.get_enhanced_system_prompt(spatial_context=spatial_context)

            # Call Claude with computer use capability - with timeout
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model=self.COMPUTER_USE_MODEL,
                    max_tokens=1024,
                    system=system_prompt,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": screenshot_base64
                                    }
                                },
                                {
                                    "type": "text",
                                    "text": user_prompt
                                }
                            ]
                        }
                    ]
                ),
                timeout=self.api_timeout
            )

            # Parse response
            response_text = response.content[0].text

            # Record success with circuit breaker
            await self._circuit_breaker.record_success()

            return self._parse_analysis_response(response_text, goal)

        except asyncio.TimeoutError:
            logger.error(f"[COMPUTER USE] Claude API call timed out after {self.api_timeout}s")
            await self._circuit_breaker.record_failure()

            return VisionAnalysis(
                analysis_id=str(uuid4()),
                description=f"API call timed out after {self.api_timeout}s",
                detected_elements=[],
                suggested_action=None,
                goal_progress=0.0,
                is_goal_achieved=False,
                reasoning_chain=["API timeout - Claude took too long to respond"],
                confidence=0.0,
                is_auth_error=False
            )

        except Exception as e:
            error_str = str(e)
            logger.error(f"[COMPUTER USE] Claude analysis failed: {e}")

            # Check for authentication errors - these are fatal
            is_auth_error = (
                "authentication_error" in error_str.lower() or
                "invalid x-api-key" in error_str.lower() or
                "401" in error_str
            )

            if is_auth_error:
                # Mark connector as unavailable to prevent further attempts
                self._auth_failed = True
                logger.error("[COMPUTER USE] ❌ Authentication failed - API key is invalid")
            else:
                # Record failure with circuit breaker
                await self._circuit_breaker.record_failure()

            return VisionAnalysis(
                analysis_id=str(uuid4()),
                description=f"Analysis failed: {error_str}",
                detected_elements=[],
                suggested_action=None,
                goal_progress=0.0,
                is_goal_achieved=False,
                reasoning_chain=[f"Error: {error_str}"],
                confidence=0.0,
                is_auth_error=is_auth_error  # Signal auth failure
            )

    def _parse_analysis_response(self, response_text: str, goal: str) -> VisionAnalysis:
        """
        Parse Claude's response into a VisionAnalysis.
        Now supports BATCH ACTIONS for action chaining optimization.
        """
        analysis_id = str(uuid4())
        suggested_action = None
        suggested_actions: List[ComputerAction] = []
        goal_progress = 0.0
        is_goal_achieved = False
        is_static_interface = False
        reasoning_chain = []

        # Extract reasoning
        reasoning_chain.append(response_text.split("```")[0].strip())

        # Check for goal achievement indicators
        goal_achieved_phrases = [
            "goal is achieved",
            "goal has been achieved",
            "successfully completed",
            "task is complete",
            "connection established",
            "connected to"
        ]
        response_lower = response_text.lower()
        is_goal_achieved = any(phrase in response_lower for phrase in goal_achieved_phrases)

        # Extract progress percentage if mentioned
        import re
        progress_match = re.search(r'(\d{1,3})%', response_text)
        if progress_match:
            goal_progress = min(100, int(progress_match.group(1))) / 100.0

        if is_goal_achieved:
            goal_progress = 1.0

        # Extract JSON action(s) if present
        json_match = re.search(r'```json\s*(.*?)\s*```', response_text, re.DOTALL)
        if json_match:
            try:
                action_data = json.loads(json_match.group(1))

                # Check if this is a BATCH response
                if isinstance(action_data, dict) and action_data.get("batch") is True:
                    # BATCH MODE: Multiple actions
                    is_static_interface = action_data.get("interface_type") == "static"
                    actions_list = action_data.get("actions", [])

                    logger.info(f"[ACTION CHAINING] Detected batch of {len(actions_list)} actions")

                    for i, act_data in enumerate(actions_list):
                        action_type = ActionType(act_data.get("action_type", "click"))
                        coords = act_data.get("coordinates")
                        if coords and isinstance(coords, list) and len(coords) == 2:
                            coords = tuple(coords)
                        else:
                            coords = None

                        action = ComputerAction(
                            action_id=str(uuid4()),
                            action_type=action_type,
                            coordinates=coords,
                            text=act_data.get("text"),
                            key=act_data.get("key"),
                            scroll_amount=act_data.get("scroll_amount"),
                            duration=act_data.get("duration", 0.1),  # Faster for batches
                            reasoning=act_data.get("reasoning", f"Batch action {i+1}"),
                            confidence=0.8
                        )
                        suggested_actions.append(action)

                    # Set first action as suggested_action for backward compatibility
                    if suggested_actions:
                        suggested_action = suggested_actions[0]

                    logger.info(f"[ACTION CHAINING] Parsed {len(suggested_actions)} actions successfully")

                else:
                    # SINGLE ACTION MODE (original behavior)
                    action_type = ActionType(action_data.get("action_type", "click"))
                    coords = action_data.get("coordinates")
                    if coords and isinstance(coords, list) and len(coords) == 2:
                        coords = tuple(coords)
                    else:
                        coords = None

                    suggested_action = ComputerAction(
                        action_id=str(uuid4()),
                        action_type=action_type,
                        coordinates=coords,
                        text=action_data.get("text"),
                        key=action_data.get("key"),
                        scroll_amount=action_data.get("scroll_amount"),
                        duration=action_data.get("duration", 0.5),
                        reasoning=action_data.get("reasoning", ""),
                        confidence=0.8
                    )
                    suggested_actions = [suggested_action] if suggested_action else []

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Could not parse action JSON: {e}")

        return VisionAnalysis(
            analysis_id=analysis_id,
            description=reasoning_chain[0][:200] if reasoning_chain else "Analysis complete",
            detected_elements=[],
            suggested_action=suggested_action,
            suggested_actions=suggested_actions,
            goal_progress=goal_progress,
            is_goal_achieved=is_goal_achieved,
            reasoning_chain=reasoning_chain,
            confidence=0.8 if suggested_action else 0.5,
            is_static_interface=is_static_interface
        )

    async def _narrate_action(self, action: ComputerAction) -> None:
        """Narrate an action before executing it."""
        if action.action_type == ActionType.CLICK:
            await self.narrator.narrate(
                NarrationEvent.CLICKING,
                {"target": action.reasoning or "the element"}
            )
        elif action.action_type == ActionType.TYPE:
            # Don't narrate sensitive text
            display_text = action.text[:20] + "..." if len(action.text or "") > 20 else action.text
            await self.narrator.narrate(
                NarrationEvent.TYPING,
                {"text": display_text}
            )
        elif action.action_type == ActionType.WAIT:
            await self.narrator.narrate(
                NarrationEvent.WAITING,
                {"description": f"{action.duration} seconds"}
            )

    def _extract_element_name(self, reasoning: str) -> Optional[str]:
        """Extract UI element name from reasoning for learning."""
        # Look for quoted element names
        import re
        match = re.search(r"['\"]([^'\"]+)['\"]", reasoning)
        if match:
            return match.group(1).lower().replace(" ", "_")

        # Look for specific UI elements mentioned
        elements = ["control_center", "screen_mirroring", "airplay", "wifi", "bluetooth"]
        for elem in elements:
            if elem.replace("_", " ") in reasoning.lower():
                return elem

        return None

    async def connect_to_display(self, display_name: str) -> TaskResult:
        """
        Connect to a display using Claude Computer Use.

        This is a high-level convenience method for display connection.

        Args:
            display_name: Name of the display to connect to

        Returns:
            TaskResult with connection details
        """
        goal = f"""Connect to the display named "{display_name}" using macOS Screen Mirroring.

Steps to accomplish this:
1. Click on the Control Center icon in the menu bar (top right, looks like two toggle switches)
2. Wait for Control Center to open
3. Click on Screen Mirroring (shows two overlapping screens icon)
4. Wait for the list of available displays
5. Click on "{display_name}" in the list
6. Wait for the connection to establish

The task is complete when you see the display connected (green checkmark or active indicator)."""

        return await self.execute_task(
            goal=goal,
            context={"target_display": display_name}
        )


# ============================================================================
# Factory Functions
# ============================================================================

_default_connector: Optional[ClaudeComputerUseConnector] = None


def get_computer_use_connector(
    tts_callback: Optional[Callable[[str], Awaitable[None]]] = None
) -> ClaudeComputerUseConnector:
    """Get or create the default Computer Use connector."""
    global _default_connector

    if _default_connector is None:
        _default_connector = ClaudeComputerUseConnector(
            tts_callback=tts_callback
        )

    return _default_connector


async def connect_to_display_dynamic(
    display_name: str,
    tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    narrate: bool = True
) -> TaskResult:
    """
    Convenience function to connect to a display using Computer Use.

    Args:
        display_name: Name of display to connect to
        tts_callback: Optional TTS callback for voice narration
        narrate: Whether to enable voice narration

    Returns:
        TaskResult with connection details
    """
    connector = get_computer_use_connector(tts_callback)
    return await connector.connect_to_display(display_name)
