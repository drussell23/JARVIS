"""
Advanced Async Primitives v1.0
==============================

Production-grade async patterns for the Trinity Ecosystem.
These primitives prevent deadlocks, manage backpressure, isolate failures,
and provide intelligent resource management across JARVIS, J-Prime, and Reactor-Core.

COMPONENTS:
    1. TimeoutProtectedLock      - Deadlock-proof async lock with timeout and tracking
    2. AdaptiveBackpressure      - Memory-aware request throttling
    3. ResourceBulkhead          - Failure isolation between components
    4. TrinityRateLimiter        - Token bucket rate limiter for message floods
    5. AdaptiveResourceManager   - Dynamic concurrency based on system capacity
    6. DeepHealthVerifier        - Functional verification beyond HTTP 200
    7. PriorityMessageQueue      - Priority-based message ordering
    8. AtomicFileIPC             - Race-condition-free file-based IPC

DESIGN PRINCIPLES:
    - Zero hardcoding: All thresholds from environment variables
    - Graceful degradation: Never crash, always fall back
    - Observable: Full metrics and logging
    - Non-blocking: All operations have timeouts
    - Self-healing: Automatic recovery from failures

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import fcntl
import gc
import hashlib
import json
import logging
import os
import platform
import struct
import sys
import tempfile
import time
import uuid
import weakref
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum, IntEnum, auto
from functools import wraps
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Deque,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from backend.core.async_safety import LazyAsyncLock

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


# =============================================================================
# CONFIGURATION FROM ENVIRONMENT
# =============================================================================


def _env_float(key: str, default: float) -> float:
    """Get float from environment with default."""
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    """Get int from environment with default."""
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    """Get bool from environment with default."""
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


# =============================================================================
# 1. TIMEOUT PROTECTED LOCK
# =============================================================================


@dataclass
class LockAcquisitionMetrics:
    """Metrics for lock acquisition."""
    total_acquisitions: int = 0
    total_timeouts: int = 0
    total_wait_time_ms: float = 0.0
    max_hold_time_ms: float = 0.0
    current_holder: Optional[str] = None
    acquire_time: Optional[float] = None

    def record_acquisition(self, wait_time_ms: float) -> None:
        self.total_acquisitions += 1
        self.total_wait_time_ms += wait_time_ms

    def record_timeout(self) -> None:
        self.total_timeouts += 1

    def record_release(self, hold_time_ms: float) -> None:
        if hold_time_ms > self.max_hold_time_ms:
            self.max_hold_time_ms = hold_time_ms


class TimeoutProtectedLock:
    """
    Async lock with timeout protection to prevent infinite deadlocks.

    Features:
        - Configurable acquisition timeout
        - Caller tracking for debugging
        - Hold time monitoring
        - Metrics collection
        - Automatic stale lock detection

    Environment Variables:
        ASYNC_LOCK_TIMEOUT: Default acquisition timeout (seconds)
        ASYNC_LOCK_WARN_HOLD_TIME: Warn if lock held longer than this (seconds)
        ASYNC_LOCK_STALE_THRESHOLD: Consider lock stale after this time (seconds)

    Example:
        lock = TimeoutProtectedLock(timeout=10.0)
        async with lock.acquire_context("my_operation"):
            # Critical section
            pass
    """

    __slots__ = (
        "_lock", "_timeout", "_warn_hold_time", "_stale_threshold",
        "_held_by", "_acquire_time", "_acquire_stack", "_metrics",
        "_lock_id", "_contention_waiters"
    )

    def __init__(
        self,
        timeout: Optional[float] = None,
        warn_hold_time: Optional[float] = None,
        lock_id: Optional[str] = None,
    ):
        self._lock = asyncio.Lock()
        self._timeout = timeout or _env_float("ASYNC_LOCK_TIMEOUT", 30.0)
        self._warn_hold_time = warn_hold_time or _env_float("ASYNC_LOCK_WARN_HOLD_TIME", 10.0)
        self._stale_threshold = _env_float("ASYNC_LOCK_STALE_THRESHOLD", 60.0)
        self._held_by: Optional[str] = None
        self._acquire_time: Optional[float] = None
        self._acquire_stack: Optional[str] = None
        self._metrics = LockAcquisitionMetrics()
        self._lock_id = lock_id or f"lock_{id(self):x}"
        self._contention_waiters: int = 0

    @property
    def is_locked(self) -> bool:
        return self._lock.locked()

    @property
    def is_stale(self) -> bool:
        """Check if lock appears to be stale (held too long)."""
        if self._acquire_time is None:
            return False
        return (time.time() - self._acquire_time) > self._stale_threshold

    @property
    def metrics(self) -> LockAcquisitionMetrics:
        return self._metrics

    @property
    def contention_level(self) -> int:
        """Number of coroutines waiting for this lock."""
        return self._contention_waiters

    async def acquire(
        self,
        caller: str = "unknown",
        timeout: Optional[float] = None,
        capture_stack: bool = False,
    ) -> bool:
        """
        Acquire lock with timeout and caller tracking.

        Args:
            caller: Identifier for the caller (for debugging)
            timeout: Override default timeout for this acquisition
            capture_stack: Whether to capture stack trace (expensive)

        Returns:
            True if lock acquired, False if timeout

        Raises:
            TimeoutError: If acquisition times out (when timeout > 0)
        """
        effective_timeout = timeout if timeout is not None else self._timeout
        start_time = time.time()

        # Track contention
        self._contention_waiters += 1

        try:
            try:
                await asyncio.wait_for(
                    self._lock.acquire(),
                    timeout=effective_timeout if effective_timeout > 0 else None
                )
            except asyncio.TimeoutError:
                self._metrics.record_timeout()
                wait_time = (time.time() - start_time) * 1000
                logger.error(
                    f"[{self._lock_id}] Lock timeout after {effective_timeout}s "
                    f"(caller={caller}, held_by={self._held_by}, "
                    f"hold_time={(time.time() - self._acquire_time):.1f}s)"
                )
                raise TimeoutError(
                    f"Lock acquisition timeout for {caller} after {effective_timeout}s. "
                    f"Lock held by: {self._held_by}"
                )

            # Record acquisition
            wait_time_ms = (time.time() - start_time) * 1000
            self._metrics.record_acquisition(wait_time_ms)
            self._held_by = caller
            self._acquire_time = time.time()
            self._metrics.current_holder = caller
            self._metrics.acquire_time = self._acquire_time

            if capture_stack:
                import traceback
                self._acquire_stack = "".join(traceback.format_stack())

            if wait_time_ms > 1000:
                logger.warning(
                    f"[{self._lock_id}] High contention - waited {wait_time_ms:.0f}ms "
                    f"(caller={caller})"
                )

            return True

        finally:
            self._contention_waiters -= 1

    def release(self) -> None:
        """Release lock with duration tracking."""
        if not self._lock.locked():
            logger.warning(f"[{self._lock_id}] Attempted to release unlocked lock")
            return

        hold_time_ms = 0.0
        if self._acquire_time:
            hold_time_ms = (time.time() - self._acquire_time) * 1000
            self._metrics.record_release(hold_time_ms)

            if hold_time_ms > self._warn_hold_time * 1000:
                logger.warning(
                    f"[{self._lock_id}] Lock held for {hold_time_ms:.0f}ms by {self._held_by}"
                )

        # Clear tracking
        self._held_by = None
        self._acquire_time = None
        self._acquire_stack = None
        self._metrics.current_holder = None
        self._metrics.acquire_time = None

        self._lock.release()

    @asynccontextmanager
    async def acquire_context(
        self,
        caller: str = "unknown",
        timeout: Optional[float] = None,
    ):
        """
        Context manager for lock acquisition with automatic release.

        Example:
            async with lock.acquire_context("my_operation"):
                # Critical section
                pass
        """
        await self.acquire(caller=caller, timeout=timeout)
        try:
            yield
        finally:
            self.release()

    def force_release(self) -> bool:
        """
        Force release a stale lock. Use with caution!

        Returns:
            True if lock was released, False if not locked
        """
        if not self._lock.locked():
            return False

        logger.warning(
            f"[{self._lock_id}] FORCE RELEASE - was held by {self._held_by} "
            f"for {(time.time() - (self._acquire_time or 0)):.1f}s"
        )

        self._held_by = None
        self._acquire_time = None
        self._acquire_stack = None
        self._metrics.current_holder = None

        # Force unlock by directly manipulating internal state
        # This is a recovery mechanism for truly stuck locks
        try:
            self._lock.release()
            return True
        except RuntimeError:
            # Already unlocked somehow
            return False

    def get_diagnostics(self) -> Dict[str, Any]:
        """Get diagnostic information about the lock."""
        return {
            "lock_id": self._lock_id,
            "is_locked": self.is_locked,
            "is_stale": self.is_stale,
            "held_by": self._held_by,
            "hold_time_s": (
                (time.time() - self._acquire_time) if self._acquire_time else None
            ),
            "contention_waiters": self._contention_waiters,
            "metrics": {
                "total_acquisitions": self._metrics.total_acquisitions,
                "total_timeouts": self._metrics.total_timeouts,
                "avg_wait_time_ms": (
                    self._metrics.total_wait_time_ms / max(1, self._metrics.total_acquisitions)
                ),
                "max_hold_time_ms": self._metrics.max_hold_time_ms,
            },
            "config": {
                "timeout": self._timeout,
                "warn_hold_time": self._warn_hold_time,
                "stale_threshold": self._stale_threshold,
            },
        }


# =============================================================================
# 2. ADAPTIVE BACKPRESSURE
# =============================================================================


class BackpressureState(Enum):
    """Current backpressure state."""
    NORMAL = "normal"           # Accept all requests
    ELEVATED = "elevated"       # Slightly throttled
    HIGH = "high"               # Significant throttling
    CRITICAL = "critical"       # Rejecting most requests
    OVERLOADED = "overloaded"   # Rejecting all new requests


@dataclass
class BackpressureMetrics:
    """Metrics for backpressure monitoring."""
    requests_accepted: int = 0
    requests_rejected: int = 0
    current_queue_size: int = 0
    peak_queue_size: int = 0
    memory_pressure: float = 0.0
    cpu_pressure: float = 0.0
    state: BackpressureState = BackpressureState.NORMAL
    last_state_change: float = field(default_factory=time.time)


class AdaptiveBackpressure:
    """
    Adaptive backpressure control based on system resources.

    Prevents memory exhaustion and maintains system stability under load
    by dynamically adjusting admission control based on:
        - Memory pressure
        - CPU usage
        - Queue depth
        - Request rate

    Environment Variables:
        MAX_REQUEST_QUEUE: Maximum queue size
        BACKPRESSURE_MEMORY_THRESHOLD: Memory usage threshold (0.0-1.0)
        BACKPRESSURE_CPU_THRESHOLD: CPU usage threshold (0.0-1.0)
        BACKPRESSURE_RATE_LIMIT: Max requests per second
        BACKPRESSURE_SAMPLE_INTERVAL: Sampling interval in seconds

    Example:
        backpressure = AdaptiveBackpressure()

        if await backpressure.try_acquire():
            try:
                await process_request()
            finally:
                await backpressure.release()
        else:
            raise ServiceOverloadedError()
    """

    def __init__(self):
        self._max_queue = _env_int("MAX_REQUEST_QUEUE", 1000)
        self._memory_threshold = _env_float("BACKPRESSURE_MEMORY_THRESHOLD", 0.85)
        self._cpu_threshold = _env_float("BACKPRESSURE_CPU_THRESHOLD", 0.90)
        self._rate_limit = _env_float("BACKPRESSURE_RATE_LIMIT", 100.0)
        self._sample_interval = _env_float("BACKPRESSURE_SAMPLE_INTERVAL", 1.0)

        self._current_queue = 0
        self._lock = asyncio.Lock()
        self._metrics = BackpressureMetrics()

        # Rate limiting using sliding window
        self._request_times: Deque[float] = deque(maxlen=1000)

        # State thresholds (fraction of max_queue)
        self._elevated_threshold = 0.5
        self._high_threshold = 0.75
        self._critical_threshold = 0.90

        # Cached pressure values
        self._last_memory_check = 0.0
        self._cached_memory_pressure = 0.0
        self._last_cpu_check = 0.0
        self._cached_cpu_pressure = 0.0

        # Monitor task
        self._monitor_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start background monitoring."""
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info("[Backpressure] Started adaptive monitoring")

    async def stop(self) -> None:
        """Stop background monitoring."""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

    async def _monitor_loop(self) -> None:
        """Background monitoring loop."""
        while True:
            try:
                await asyncio.sleep(self._sample_interval)
                await self._update_pressure_readings()
                self._update_state()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Backpressure] Monitor error: {e}")

    async def _update_pressure_readings(self) -> None:
        """Update cached pressure readings."""
        now = time.time()

        # Memory pressure
        if now - self._last_memory_check > self._sample_interval:
            self._cached_memory_pressure = self._get_memory_pressure()
            self._last_memory_check = now
            self._metrics.memory_pressure = self._cached_memory_pressure

        # CPU pressure (only if psutil available)
        if PSUTIL_AVAILABLE and now - self._last_cpu_check > self._sample_interval:
            try:
                # Non-blocking CPU check (returns immediately with last reading)
                self._cached_cpu_pressure = psutil.cpu_percent(interval=None) / 100.0
                self._last_cpu_check = now
                self._metrics.cpu_pressure = self._cached_cpu_pressure
            except Exception:
                pass

    def _get_memory_pressure(self) -> float:
        """Get current memory pressure (0.0 to 1.0)."""
        if PSUTIL_AVAILABLE:
            try:
                mem = psutil.virtual_memory()
                return mem.percent / 100.0
            except Exception:
                pass

        # Fallback: Use gc stats for approximation
        gc_stats = gc.get_stats()
        if gc_stats:
            # Rough heuristic based on GC pressure
            collections = sum(s.get("collections", 0) for s in gc_stats)
            # If lots of GC happening, assume memory pressure
            if collections > 100:
                return 0.7

        return 0.5  # Unknown, assume moderate

    def _get_current_rate(self) -> float:
        """Get current request rate (requests per second)."""
        now = time.time()
        # Remove old entries
        while self._request_times and now - self._request_times[0] > 1.0:
            self._request_times.popleft()
        return len(self._request_times)

    def _update_state(self) -> None:
        """Update backpressure state based on current conditions."""
        old_state = self._metrics.state

        # Check memory first (most critical)
        if self._cached_memory_pressure > 0.95:
            new_state = BackpressureState.OVERLOADED
        elif self._cached_memory_pressure > self._memory_threshold:
            new_state = BackpressureState.CRITICAL
        elif self._cached_cpu_pressure > self._cpu_threshold:
            new_state = BackpressureState.HIGH
        else:
            # Check queue-based state
            queue_ratio = self._current_queue / max(1, self._max_queue)
            if queue_ratio >= self._critical_threshold:
                new_state = BackpressureState.CRITICAL
            elif queue_ratio >= self._high_threshold:
                new_state = BackpressureState.HIGH
            elif queue_ratio >= self._elevated_threshold:
                new_state = BackpressureState.ELEVATED
            else:
                new_state = BackpressureState.NORMAL

        if new_state != old_state:
            self._metrics.state = new_state
            self._metrics.last_state_change = time.time()
            logger.info(f"[Backpressure] State changed: {old_state.value} → {new_state.value}")

    def _calculate_admission_probability(self) -> float:
        """Calculate probability of admitting a new request."""
        state = self._metrics.state

        if state == BackpressureState.NORMAL:
            return 1.0
        elif state == BackpressureState.ELEVATED:
            return 0.9
        elif state == BackpressureState.HIGH:
            return 0.7
        elif state == BackpressureState.CRITICAL:
            return 0.3
        else:  # OVERLOADED
            return 0.0

    async def try_acquire(self, request_weight: int = 1) -> bool:
        """
        Try to acquire capacity for a request.

        Args:
            request_weight: Weight of this request (default 1)

        Returns:
            True if request admitted, False if rejected
        """
        async with self._lock:
            # Update state
            await self._update_pressure_readings()
            self._update_state()

            # Check rate limit
            current_rate = self._get_current_rate()
            if current_rate >= self._rate_limit:
                self._metrics.requests_rejected += 1
                return False

            # Check queue capacity
            if self._current_queue + request_weight > self._max_queue:
                self._metrics.requests_rejected += 1
                return False

            # Probabilistic admission based on state
            probability = self._calculate_admission_probability()
            if probability < 1.0:
                import random
                if random.random() > probability:
                    self._metrics.requests_rejected += 1
                    return False

            # Admit request
            self._current_queue += request_weight
            self._request_times.append(time.time())
            self._metrics.requests_accepted += 1
            self._metrics.current_queue_size = self._current_queue

            if self._current_queue > self._metrics.peak_queue_size:
                self._metrics.peak_queue_size = self._current_queue

            return True

    async def release(self, request_weight: int = 1) -> None:
        """Release capacity."""
        async with self._lock:
            self._current_queue = max(0, self._current_queue - request_weight)
            self._metrics.current_queue_size = self._current_queue

    @asynccontextmanager
    async def acquire_context(self, request_weight: int = 1):
        """Context manager for backpressure acquisition."""
        if not await self.try_acquire(request_weight):
            raise BackpressureRejection(
                f"Request rejected due to backpressure (state={self._metrics.state.value})"
            )
        try:
            yield
        finally:
            await self.release(request_weight)

    def get_state(self) -> BackpressureState:
        """Get current backpressure state."""
        return self._metrics.state

    def get_metrics(self) -> Dict[str, Any]:
        """Get backpressure metrics."""
        return {
            "state": self._metrics.state.value,
            "current_queue": self._metrics.current_queue_size,
            "max_queue": self._max_queue,
            "peak_queue": self._metrics.peak_queue_size,
            "requests_accepted": self._metrics.requests_accepted,
            "requests_rejected": self._metrics.requests_rejected,
            "rejection_rate": (
                self._metrics.requests_rejected /
                max(1, self._metrics.requests_accepted + self._metrics.requests_rejected)
            ),
            "memory_pressure": self._metrics.memory_pressure,
            "cpu_pressure": self._metrics.cpu_pressure,
            "current_rate": self._get_current_rate(),
            "rate_limit": self._rate_limit,
        }


class BackpressureRejection(Exception):
    """Exception raised when request is rejected due to backpressure."""
    pass


# =============================================================================
# 3. RESOURCE BULKHEAD
# =============================================================================


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"     # Normal operation
    OPEN = "open"         # Failing, rejecting requests
    HALF_OPEN = "half_open"  # Testing if recovered


@dataclass
class CircuitBreakerMetrics:
    """Metrics for circuit breaker."""
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    successes: int = 0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    last_state_change: float = field(default_factory=time.time)


class PoolCircuitBreaker:
    """
    Circuit breaker for a resource pool.

    Protects against cascading failures by temporarily stopping
    requests to failing resources.

    States:
        CLOSED: Normal operation, requests pass through
        OPEN: Resource failing, requests rejected immediately
        HALF_OPEN: Testing recovery, limited requests allowed

    Environment Variables:
        CIRCUIT_FAILURE_THRESHOLD: Failures before opening
        CIRCUIT_SUCCESS_THRESHOLD: Successes to close
        CIRCUIT_RESET_TIMEOUT: Seconds before half-open
    """

    def __init__(
        self,
        failure_threshold: Optional[int] = None,
        success_threshold: Optional[int] = None,
        reset_timeout: Optional[float] = None,
        name: str = "default",
    ):
        self._failure_threshold = failure_threshold or _env_int("CIRCUIT_FAILURE_THRESHOLD", 5)
        self._success_threshold = success_threshold or _env_int("CIRCUIT_SUCCESS_THRESHOLD", 3)
        self._reset_timeout = reset_timeout or _env_float("CIRCUIT_RESET_TIMEOUT", 30.0)
        self._name = name
        self._metrics = CircuitBreakerMetrics()
        self._lock = asyncio.Lock()
        self._half_open_permits = 1  # Limit concurrent half-open tests
        self._half_open_in_use = 0

    @property
    def state(self) -> CircuitState:
        return self._metrics.state

    def is_open(self) -> bool:
        """Check if circuit is open (blocking requests)."""
        return self._metrics.state == CircuitState.OPEN

    async def can_execute(self) -> bool:
        """
        Check if execution is allowed and acquire permit if half-open.

        Returns:
            True if execution allowed, False otherwise
        """
        async with self._lock:
            if self._metrics.state == CircuitState.CLOSED:
                return True

            if self._metrics.state == CircuitState.OPEN:
                # Check if we should transition to half-open
                if self._metrics.last_failure_time:
                    elapsed = time.time() - self._metrics.last_failure_time
                    if elapsed >= self._reset_timeout:
                        self._transition_to(CircuitState.HALF_OPEN)
                        return await self._try_acquire_half_open_permit()
                return False

            if self._metrics.state == CircuitState.HALF_OPEN:
                return await self._try_acquire_half_open_permit()

            return False

    async def _try_acquire_half_open_permit(self) -> bool:
        """Try to acquire a half-open permit for testing recovery."""
        if self._half_open_in_use < self._half_open_permits:
            self._half_open_in_use += 1
            return True
        return False

    async def record_success(self) -> None:
        """Record a successful execution."""
        async with self._lock:
            self._metrics.successes += 1
            self._metrics.consecutive_successes += 1
            self._metrics.consecutive_failures = 0
            self._metrics.last_success_time = time.time()

            if self._metrics.state == CircuitState.HALF_OPEN:
                self._half_open_in_use = max(0, self._half_open_in_use - 1)

                if self._metrics.consecutive_successes >= self._success_threshold:
                    self._transition_to(CircuitState.CLOSED)

    async def record_failure(self, error: Optional[Exception] = None) -> None:
        """Record a failed execution."""
        async with self._lock:
            self._metrics.failures += 1
            self._metrics.consecutive_failures += 1
            self._metrics.consecutive_successes = 0
            self._metrics.last_failure_time = time.time()

            if self._metrics.state == CircuitState.HALF_OPEN:
                self._half_open_in_use = max(0, self._half_open_in_use - 1)
                self._transition_to(CircuitState.OPEN)

            elif self._metrics.state == CircuitState.CLOSED:
                if self._metrics.consecutive_failures >= self._failure_threshold:
                    self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        old_state = self._metrics.state
        if old_state != new_state:
            self._metrics.state = new_state
            self._metrics.last_state_change = time.time()

            if new_state == CircuitState.CLOSED:
                self._metrics.consecutive_failures = 0
                self._half_open_in_use = 0
            elif new_state == CircuitState.HALF_OPEN:
                self._metrics.consecutive_successes = 0

            logger.info(
                f"[Circuit:{self._name}] State transition: {old_state.value} → {new_state.value}"
            )

    async def force_reset(self) -> None:
        """Force reset the circuit breaker to closed state."""
        async with self._lock:
            self._metrics = CircuitBreakerMetrics()
            self._half_open_in_use = 0
            logger.info(f"[Circuit:{self._name}] Force reset to CLOSED")

    def get_metrics(self) -> Dict[str, Any]:
        """Get circuit breaker metrics."""
        return {
            "name": self._name,
            "state": self._metrics.state.value,
            "failures": self._metrics.failures,
            "successes": self._metrics.successes,
            "consecutive_failures": self._metrics.consecutive_failures,
            "consecutive_successes": self._metrics.consecutive_successes,
            "failure_threshold": self._failure_threshold,
            "success_threshold": self._success_threshold,
            "reset_timeout": self._reset_timeout,
            "half_open_in_use": self._half_open_in_use,
        }


class ResourceBulkhead:
    """
    Isolates resource pools to prevent cascading failures.

    Each pool has its own semaphore and circuit breaker, ensuring that
    failure in one pool doesn't affect others.

    Environment Variables:
        BULKHEAD_POOL_SIZE_{POOL_NAME}: Size for specific pool
        BULKHEAD_DEFAULT_POOL_SIZE: Default pool size
        BULKHEAD_TIMEOUT: Default execution timeout

    Example:
        bulkhead = ResourceBulkhead({
            "prime": 10,      # 10 concurrent Prime requests
            "claude": 5,      # 5 concurrent Claude requests
            "reactor": 3,     # 3 concurrent Reactor requests
        })

        async with bulkhead.execute("prime") as permit:
            result = await call_prime()
    """

    def __init__(
        self,
        pools: Optional[Dict[str, int]] = None,
        default_timeout: Optional[float] = None,
    ):
        default_size = _env_int("BULKHEAD_DEFAULT_POOL_SIZE", 10)
        self._default_timeout = default_timeout or _env_float("BULKHEAD_TIMEOUT", 30.0)

        # Initialize pools from config or environment
        self._pool_sizes: Dict[str, int] = {}
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
        self._circuit_breakers: Dict[str, PoolCircuitBreaker] = {}
        self._active_counts: Dict[str, int] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

        if pools:
            for name, size in pools.items():
                # Check for environment override
                env_size = _env_int(f"BULKHEAD_POOL_SIZE_{name.upper()}", size)
                self._add_pool(name, env_size)

        # Add default pools for Trinity
        for name in ["prime", "claude", "reactor"]:
            if name not in self._pool_sizes:
                env_size = _env_int(f"BULKHEAD_POOL_SIZE_{name.upper()}", default_size)
                self._add_pool(name, env_size)

    def _add_pool(self, name: str, size: int) -> None:
        """Add a resource pool."""
        self._pool_sizes[name] = size
        self._semaphores[name] = asyncio.Semaphore(size)
        self._circuit_breakers[name] = PoolCircuitBreaker(name=name)
        self._active_counts[name] = 0
        self._locks[name] = asyncio.Lock()

    def get_pool_names(self) -> List[str]:
        """Get list of pool names."""
        return list(self._pool_sizes.keys())

    def get_pool_status(self, name: str) -> Optional[Dict[str, Any]]:
        """Get status of a specific pool."""
        if name not in self._pool_sizes:
            return None

        return {
            "size": self._pool_sizes[name],
            "active": self._active_counts[name],
            "available": self._pool_sizes[name] - self._active_counts[name],
            "circuit": self._circuit_breakers[name].get_metrics(),
        }

    @asynccontextmanager
    async def execute(
        self,
        pool_name: str,
        timeout: Optional[float] = None,
    ):
        """
        Execute within a bulkhead pool.

        Args:
            pool_name: Name of the pool
            timeout: Execution timeout (overrides default)

        Yields:
            Permit for execution

        Raises:
            BulkheadRejection: If pool circuit is open
            BulkheadTimeout: If semaphore acquisition times out
        """
        if pool_name not in self._semaphores:
            # Create pool on demand
            self._add_pool(pool_name, _env_int("BULKHEAD_DEFAULT_POOL_SIZE", 10))

        circuit = self._circuit_breakers[pool_name]
        semaphore = self._semaphores[pool_name]
        effective_timeout = timeout if timeout is not None else self._default_timeout

        # Check circuit breaker first
        if not await circuit.can_execute():
            raise BulkheadCircuitOpen(
                f"Pool '{pool_name}' circuit is open: "
                f"{circuit.get_metrics()}"
            )

        # Try to acquire semaphore
        try:
            acquired = await asyncio.wait_for(
                semaphore.acquire(),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            await circuit.record_failure()
            raise BulkheadTimeout(
                f"Pool '{pool_name}' acquisition timeout after {effective_timeout}s"
            )

        # Track active count
        async with self._locks[pool_name]:
            self._active_counts[pool_name] += 1

        try:
            yield BulkheadPermit(pool_name)
            await circuit.record_success()
        except Exception as e:
            await circuit.record_failure(e)
            raise
        finally:
            semaphore.release()
            async with self._locks[pool_name]:
                self._active_counts[pool_name] = max(0, self._active_counts[pool_name] - 1)

    async def execute_with_fallback(
        self,
        pool_name: str,
        primary_fn: Callable[[], Awaitable[T]],
        fallback_fn: Optional[Callable[[], Awaitable[T]]] = None,
        fallback_pool: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> T:
        """
        Execute with automatic fallback on bulkhead rejection.

        Args:
            pool_name: Primary pool name
            primary_fn: Primary function to execute
            fallback_fn: Fallback function if primary fails
            fallback_pool: Pool for fallback execution
            timeout: Execution timeout

        Returns:
            Result from primary or fallback function
        """
        try:
            async with self.execute(pool_name, timeout):
                return await primary_fn()
        except (BulkheadCircuitOpen, BulkheadTimeout) as e:
            if fallback_fn is None:
                raise

            logger.warning(f"[Bulkhead] Primary pool '{pool_name}' unavailable: {e}")

            if fallback_pool:
                async with self.execute(fallback_pool, timeout):
                    return await fallback_fn()
            else:
                return await fallback_fn()

    def get_all_status(self) -> Dict[str, Any]:
        """Get status of all pools."""
        return {
            name: self.get_pool_status(name)
            for name in self._pool_sizes
        }

    async def reset_circuit(self, pool_name: str) -> bool:
        """Reset circuit breaker for a pool."""
        if pool_name not in self._circuit_breakers:
            return False
        await self._circuit_breakers[pool_name].force_reset()
        return True


@dataclass
class BulkheadPermit:
    """Permit for bulkhead execution."""
    pool_name: str


class BulkheadCircuitOpen(Exception):
    """Exception raised when bulkhead circuit is open."""
    pass


class BulkheadTimeout(Exception):
    """Exception raised when bulkhead acquisition times out."""
    pass


# =============================================================================
# 4. TRINITY RATE LIMITER
# =============================================================================


class TrinityRateLimiter:
    """
    Token bucket rate limiter for Trinity message flows.

    Prevents message floods from overwhelming components while allowing
    short bursts of activity.

    Environment Variables:
        TRINITY_RATE_LIMIT: Messages per second
        TRINITY_RATE_BURST: Maximum burst size
        TRINITY_RATE_REFILL_INTERVAL: Token refill interval in seconds

    Example:
        limiter = TrinityRateLimiter(rate=100.0, burst=50)

        if await limiter.acquire():
            await send_message()
        else:
            # Rate limited, wait or reject
            pass
    """

    def __init__(
        self,
        rate: Optional[float] = None,
        burst: Optional[int] = None,
        name: str = "default",
    ):
        self._rate = rate or _env_float("TRINITY_RATE_LIMIT", 100.0)
        self._burst = burst or _env_int("TRINITY_RATE_BURST", 50)
        self._name = name

        self._tokens = float(self._burst)
        self._last_update = time.time()
        self._lock = asyncio.Lock()

        # Metrics
        self._total_acquired = 0
        self._total_rejected = 0
        self._total_waited = 0

    async def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self._last_update
        self._tokens = min(
            float(self._burst),
            self._tokens + elapsed * self._rate
        )
        self._last_update = now

    async def acquire(self, tokens: int = 1, wait: bool = False) -> bool:
        """
        Try to acquire tokens.

        Args:
            tokens: Number of tokens to acquire
            wait: If True, wait until tokens available

        Returns:
            True if tokens acquired, False if rate limited
        """
        async with self._lock:
            await self._refill()

            if self._tokens >= tokens:
                self._tokens -= tokens
                self._total_acquired += tokens
                return True

            if not wait:
                self._total_rejected += tokens
                return False

            # Calculate wait time
            tokens_needed = tokens - self._tokens
            wait_time = tokens_needed / self._rate

        # Wait outside lock
        await asyncio.sleep(wait_time)
        self._total_waited += 1

        # Retry acquisition
        async with self._lock:
            await self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                self._total_acquired += tokens
                return True

            self._total_rejected += tokens
            return False

    async def acquire_or_wait(self, tokens: int = 1, max_wait: float = 10.0) -> bool:
        """
        Acquire tokens, waiting up to max_wait seconds.

        Args:
            tokens: Number of tokens to acquire
            max_wait: Maximum time to wait

        Returns:
            True if acquired, False if timeout
        """
        try:
            return await asyncio.wait_for(
                self._acquire_with_wait(tokens),
                timeout=max_wait
            )
        except asyncio.TimeoutError:
            self._total_rejected += tokens
            return False

    async def _acquire_with_wait(self, tokens: int) -> bool:
        """Internal acquire with wait."""
        while True:
            async with self._lock:
                await self._refill()

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    self._total_acquired += tokens
                    return True

                # Calculate wait time
                tokens_needed = tokens - self._tokens
                wait_time = tokens_needed / self._rate

            await asyncio.sleep(min(wait_time, 0.1))

    def get_status(self) -> Dict[str, Any]:
        """Get rate limiter status."""
        return {
            "name": self._name,
            "rate": self._rate,
            "burst": self._burst,
            "current_tokens": self._tokens,
            "total_acquired": self._total_acquired,
            "total_rejected": self._total_rejected,
            "total_waited": self._total_waited,
            "rejection_rate": (
                self._total_rejected /
                max(1, self._total_acquired + self._total_rejected)
            ),
        }


# =============================================================================
# 5. ADAPTIVE RESOURCE MANAGER
# =============================================================================


class AdaptiveResourceManager:
    """
    Dynamically adjusts resource limits based on system capacity.

    Features:
        - CPU-based concurrency scaling
        - Memory-aware resource allocation
        - Dynamic semaphore resizing
        - Load-based optimization

    Environment Variables:
        ADAPTIVE_MIN_CONCURRENCY: Minimum concurrent operations
        ADAPTIVE_MAX_CONCURRENCY: Maximum concurrent operations
        ADAPTIVE_CPU_SCALE_FACTOR: CPU cores to concurrency ratio
        ADAPTIVE_MEMORY_THRESHOLD: Memory threshold for scaling
        ADAPTIVE_SAMPLE_INTERVAL: Sampling interval in seconds

    Example:
        manager = AdaptiveResourceManager()
        await manager.start()

        concurrency = manager.get_optimal_concurrency()
        async with manager.acquire_resource("compute"):
            await heavy_computation()
    """

    def __init__(self):
        self._min_concurrency = _env_int("ADAPTIVE_MIN_CONCURRENCY", 2)
        self._max_concurrency = _env_int("ADAPTIVE_MAX_CONCURRENCY", 100)
        self._cpu_scale_factor = _env_float("ADAPTIVE_CPU_SCALE_FACTOR", 2.0)
        self._memory_threshold = _env_float("ADAPTIVE_MEMORY_THRESHOLD", 0.85)
        self._sample_interval = _env_float("ADAPTIVE_SAMPLE_INTERVAL", 5.0)

        self._cpu_cores = os.cpu_count() or 4
        self._base_concurrency = int(self._cpu_cores * self._cpu_scale_factor)

        # Dynamic semaphores
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
        self._semaphore_sizes: Dict[str, int] = {}
        self._active_counts: Dict[str, int] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

        # Current optimal concurrency
        self._current_concurrency = min(
            self._max_concurrency,
            max(self._min_concurrency, self._base_concurrency)
        )

        # Monitoring
        self._monitor_task: Optional[asyncio.Task] = None
        self._last_cpu_usage = 0.0
        self._last_memory_usage = 0.0

        # History for trend analysis
        self._cpu_history: Deque[float] = deque(maxlen=12)
        self._memory_history: Deque[float] = deque(maxlen=12)

    async def start(self) -> None:
        """Start background monitoring."""
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info(
                f"[AdaptiveRM] Started with base concurrency {self._base_concurrency} "
                f"(cores={self._cpu_cores}, scale={self._cpu_scale_factor})"
            )

    async def stop(self) -> None:
        """Stop background monitoring."""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

    async def _monitor_loop(self) -> None:
        """Background monitoring loop."""
        while True:
            try:
                await asyncio.sleep(self._sample_interval)
                await self._update_metrics()
                self._adjust_concurrency()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[AdaptiveRM] Monitor error: {e}")

    async def _update_metrics(self) -> None:
        """Update system metrics."""
        if PSUTIL_AVAILABLE:
            try:
                self._last_cpu_usage = psutil.cpu_percent(interval=None) / 100.0
                self._last_memory_usage = psutil.virtual_memory().percent / 100.0

                self._cpu_history.append(self._last_cpu_usage)
                self._memory_history.append(self._last_memory_usage)
            except Exception:
                pass

    def _adjust_concurrency(self) -> None:
        """Adjust concurrency based on system load."""
        old_concurrency = self._current_concurrency

        # Calculate optimal concurrency
        if self._last_memory_usage > self._memory_threshold:
            # Memory pressure - reduce concurrency
            factor = max(0.5, 1.0 - (self._last_memory_usage - self._memory_threshold) * 2)
            new_concurrency = int(self._base_concurrency * factor)
        elif self._last_cpu_usage > 0.9:
            # High CPU - reduce concurrency
            factor = max(0.5, 1.0 - (self._last_cpu_usage - 0.8))
            new_concurrency = int(self._base_concurrency * factor)
        elif self._last_cpu_usage < 0.3 and self._last_memory_usage < 0.5:
            # Low load - can increase concurrency
            new_concurrency = int(self._base_concurrency * 1.5)
        else:
            # Normal operation
            new_concurrency = self._base_concurrency

        # Apply limits
        self._current_concurrency = min(
            self._max_concurrency,
            max(self._min_concurrency, new_concurrency)
        )

        if self._current_concurrency != old_concurrency:
            logger.info(
                f"[AdaptiveRM] Concurrency adjusted: {old_concurrency} → "
                f"{self._current_concurrency} (cpu={self._last_cpu_usage:.1%}, "
                f"mem={self._last_memory_usage:.1%})"
            )

    def get_optimal_concurrency(self) -> int:
        """Get current optimal concurrency level."""
        return self._current_concurrency

    def get_semaphore(self, name: str, size: Optional[int] = None) -> asyncio.Semaphore:
        """
        Get or create a named semaphore.

        Args:
            name: Semaphore name
            size: Optional specific size (otherwise uses optimal concurrency)

        Returns:
            Semaphore for the resource
        """
        if name not in self._semaphores:
            effective_size = size or self._current_concurrency
            self._semaphores[name] = asyncio.Semaphore(effective_size)
            self._semaphore_sizes[name] = effective_size
            self._active_counts[name] = 0
            self._locks[name] = asyncio.Lock()
        return self._semaphores[name]

    @asynccontextmanager
    async def acquire_resource(
        self,
        name: str,
        timeout: Optional[float] = None,
    ):
        """
        Acquire a resource with adaptive concurrency.

        Args:
            name: Resource name
            timeout: Acquisition timeout

        Yields:
            Resource permit
        """
        semaphore = self.get_semaphore(name)
        effective_timeout = timeout or 30.0

        try:
            acquired = await asyncio.wait_for(
                semaphore.acquire(),
                timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"Resource '{name}' acquisition timeout after {effective_timeout}s")

        async with self._locks[name]:
            self._active_counts[name] = self._active_counts.get(name, 0) + 1

        try:
            yield ResourcePermit(name)
        finally:
            semaphore.release()
            async with self._locks[name]:
                self._active_counts[name] = max(0, self._active_counts.get(name, 1) - 1)

    def get_status(self) -> Dict[str, Any]:
        """Get resource manager status."""
        return {
            "optimal_concurrency": self._current_concurrency,
            "base_concurrency": self._base_concurrency,
            "cpu_cores": self._cpu_cores,
            "cpu_scale_factor": self._cpu_scale_factor,
            "last_cpu_usage": self._last_cpu_usage,
            "last_memory_usage": self._last_memory_usage,
            "min_concurrency": self._min_concurrency,
            "max_concurrency": self._max_concurrency,
            "resources": {
                name: {
                    "size": self._semaphore_sizes.get(name, 0),
                    "active": self._active_counts.get(name, 0),
                }
                for name in self._semaphores
            },
        }


@dataclass
class ResourcePermit:
    """Permit for resource acquisition."""
    resource_name: str


# =============================================================================
# 6. DEEP HEALTH VERIFIER
# =============================================================================


class HealthCheckResult(Enum):
    """Health check result."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ComponentHealth:
    """Health status of a component."""
    name: str
    result: HealthCheckResult
    latency_ms: float
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class DeepHealthVerifier:
    """
    Verifies actual functionality beyond HTTP 200 responses.

    Features:
        - Functional verification (can component actually do work)
        - Latency measurement
        - Capability testing
        - Trend analysis

    Environment Variables:
        HEALTH_CHECK_TIMEOUT: Default timeout for health checks
        HEALTH_FUNCTIONAL_TEST: Whether to run functional tests

    Example:
        verifier = DeepHealthVerifier()

        health = await verifier.verify_component(
            "prime",
            url="http://localhost:8000",
            functional_test=async () => await prime.generate("test")
        )
    """

    def __init__(self):
        self._default_timeout = _env_float("HEALTH_CHECK_TIMEOUT", 10.0)
        self._functional_test_enabled = _env_bool("HEALTH_FUNCTIONAL_TEST", True)

        # Cache session for connection pooling
        self._session: Optional[aiohttp.ClientSession] = None

        # Health history for trend analysis
        self._health_history: Dict[str, Deque[ComponentHealth]] = {}

    async def _get_session(self) -> Optional[aiohttp.ClientSession]:
        """Get or create aiohttp session."""
        if not AIOHTTP_AVAILABLE:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._default_timeout)
            )
        return self._session

    async def close(self) -> None:
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def verify_http(
        self,
        name: str,
        url: str,
        expected_status: int = 200,
        timeout: Optional[float] = None,
    ) -> ComponentHealth:
        """
        Verify HTTP endpoint is responding.

        Args:
            name: Component name
            url: Health check URL
            expected_status: Expected HTTP status code
            timeout: Request timeout

        Returns:
            ComponentHealth with result
        """
        start_time = time.time()
        effective_timeout = timeout or self._default_timeout

        try:
            session = await self._get_session()
            if session is None:
                return ComponentHealth(
                    name=name,
                    result=HealthCheckResult.UNKNOWN,
                    latency_ms=0,
                    error="aiohttp not available",
                )

            async with session.get(url, timeout=aiohttp.ClientTimeout(total=effective_timeout)) as response:
                latency_ms = (time.time() - start_time) * 1000

                if response.status == expected_status:
                    result = HealthCheckResult.HEALTHY
                elif response.status < 500:
                    result = HealthCheckResult.DEGRADED
                else:
                    result = HealthCheckResult.UNHEALTHY

                return ComponentHealth(
                    name=name,
                    result=result,
                    latency_ms=latency_ms,
                    details={"status_code": response.status},
                )

        except asyncio.TimeoutError:
            return ComponentHealth(
                name=name,
                result=HealthCheckResult.UNHEALTHY,
                latency_ms=(time.time() - start_time) * 1000,
                error=f"Timeout after {effective_timeout}s",
            )
        except Exception as e:
            return ComponentHealth(
                name=name,
                result=HealthCheckResult.UNHEALTHY,
                latency_ms=(time.time() - start_time) * 1000,
                error=str(e),
            )

    async def verify_functional(
        self,
        name: str,
        test_fn: Callable[[], Awaitable[Any]],
        validate_fn: Optional[Callable[[Any], bool]] = None,
        timeout: Optional[float] = None,
    ) -> ComponentHealth:
        """
        Verify component can actually perform work.

        Args:
            name: Component name
            test_fn: Async function to test functionality
            validate_fn: Optional function to validate result
            timeout: Test timeout

        Returns:
            ComponentHealth with result
        """
        if not self._functional_test_enabled:
            return ComponentHealth(
                name=name,
                result=HealthCheckResult.UNKNOWN,
                latency_ms=0,
                details={"reason": "functional tests disabled"},
            )

        start_time = time.time()
        effective_timeout = timeout or self._default_timeout

        try:
            result = await asyncio.wait_for(test_fn(), timeout=effective_timeout)
            latency_ms = (time.time() - start_time) * 1000

            # Validate result if validator provided
            if validate_fn:
                is_valid = validate_fn(result)
                if not is_valid:
                    return ComponentHealth(
                        name=name,
                        result=HealthCheckResult.DEGRADED,
                        latency_ms=latency_ms,
                        details={"reason": "validation failed"},
                    )

            return ComponentHealth(
                name=name,
                result=HealthCheckResult.HEALTHY,
                latency_ms=latency_ms,
                details={"functional_test": "passed"},
            )

        except asyncio.TimeoutError:
            return ComponentHealth(
                name=name,
                result=HealthCheckResult.UNHEALTHY,
                latency_ms=(time.time() - start_time) * 1000,
                error=f"Functional test timeout after {effective_timeout}s",
            )
        except Exception as e:
            return ComponentHealth(
                name=name,
                result=HealthCheckResult.UNHEALTHY,
                latency_ms=(time.time() - start_time) * 1000,
                error=f"Functional test failed: {e}",
            )

    async def verify_component(
        self,
        name: str,
        url: Optional[str] = None,
        functional_test: Optional[Callable[[], Awaitable[Any]]] = None,
        validate_fn: Optional[Callable[[Any], bool]] = None,
        timeout: Optional[float] = None,
    ) -> ComponentHealth:
        """
        Comprehensive component verification.

        Args:
            name: Component name
            url: Optional HTTP health URL
            functional_test: Optional functional test
            validate_fn: Optional result validator
            timeout: Verification timeout

        Returns:
            ComponentHealth with combined result
        """
        results: List[ComponentHealth] = []

        # HTTP check
        if url:
            http_health = await self.verify_http(name, url, timeout=timeout)
            results.append(http_health)

            # If HTTP fails, skip functional test
            if http_health.result == HealthCheckResult.UNHEALTHY:
                self._record_health(name, http_health)
                return http_health

        # Functional test
        if functional_test:
            func_health = await self.verify_functional(
                name, functional_test, validate_fn, timeout
            )
            results.append(func_health)

        # Combine results
        if not results:
            combined = ComponentHealth(
                name=name,
                result=HealthCheckResult.UNKNOWN,
                latency_ms=0,
                details={"reason": "no checks performed"},
            )
        else:
            # Use worst result
            worst = min(results, key=lambda h: list(HealthCheckResult).index(h.result))
            total_latency = sum(r.latency_ms for r in results)
            combined = ComponentHealth(
                name=name,
                result=worst.result,
                latency_ms=total_latency,
                details={
                    "checks": len(results),
                    "results": [
                        {"check": r.name, "result": r.result.value, "latency_ms": r.latency_ms}
                        for r in results
                    ],
                },
                error=worst.error,
            )

        self._record_health(name, combined)
        return combined

    def _record_health(self, name: str, health: ComponentHealth) -> None:
        """Record health for trend analysis."""
        if name not in self._health_history:
            self._health_history[name] = deque(maxlen=100)
        self._health_history[name].append(health)

    def get_health_trend(self, name: str) -> Dict[str, Any]:
        """Get health trend for a component."""
        if name not in self._health_history:
            return {"error": "no history"}

        history = list(self._health_history[name])
        if not history:
            return {"error": "empty history"}

        # Calculate stats
        healthy_count = sum(1 for h in history if h.result == HealthCheckResult.HEALTHY)
        avg_latency = sum(h.latency_ms for h in history) / len(history)

        return {
            "samples": len(history),
            "healthy_percentage": healthy_count / len(history) * 100,
            "avg_latency_ms": avg_latency,
            "latest": {
                "result": history[-1].result.value,
                "latency_ms": history[-1].latency_ms,
                "timestamp": history[-1].timestamp,
            },
        }


# =============================================================================
# 7. PRIORITY MESSAGE QUEUE
# =============================================================================


class MessagePriority(IntEnum):
    """Message priority levels."""
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass
class PriorityMessage(Generic[T]):
    """Message with priority."""
    data: T
    priority: MessagePriority = MessagePriority.NORMAL
    timestamp: float = field(default_factory=time.time)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class PriorityMessageQueue(Generic[T]):
    """
    Priority-based message queue with fair scheduling.

    Features:
        - Multiple priority levels
        - Fair scheduling (prevent starvation)
        - Bounded queues per priority
        - Metrics and monitoring

    Environment Variables:
        PRIORITY_QUEUE_MAX_SIZE: Maximum total queue size
        PRIORITY_QUEUE_FAIR_INTERVAL: Interval for fairness adjustment

    Example:
        queue = PriorityMessageQueue[str]()

        await queue.put("urgent", MessagePriority.CRITICAL)
        await queue.put("normal", MessagePriority.NORMAL)

        msg = await queue.get()  # Returns "urgent" first
    """

    def __init__(
        self,
        max_size: Optional[int] = None,
        fair_interval: Optional[int] = None,
    ):
        self._max_size = max_size or _env_int("PRIORITY_QUEUE_MAX_SIZE", 10000)
        self._fair_interval = fair_interval or _env_int("PRIORITY_QUEUE_FAIR_INTERVAL", 10)

        # One queue per priority level
        self._queues: Dict[MessagePriority, asyncio.Queue] = {
            priority: asyncio.Queue(maxsize=self._max_size // 4)
            for priority in MessagePriority
        }

        self._total_size = 0
        self._lock = asyncio.Lock()

        # Fairness: track low-priority starvation
        self._gets_since_low_priority = 0

        # Metrics
        self._total_put = 0
        self._total_get = 0
        self._total_dropped = 0

    async def put(
        self,
        data: T,
        priority: MessagePriority = MessagePriority.NORMAL,
        nowait: bool = False,
    ) -> bool:
        """
        Put an item in the queue.

        Args:
            data: Data to queue
            priority: Message priority
            nowait: If True, don't wait if full

        Returns:
            True if queued, False if dropped
        """
        message = PriorityMessage(data=data, priority=priority)
        queue = self._queues[priority]

        try:
            if nowait:
                queue.put_nowait(message)
            else:
                await queue.put(message)

            async with self._lock:
                self._total_size += 1
                self._total_put += 1

            return True

        except asyncio.QueueFull:
            self._total_dropped += 1
            return False

    async def get(self, timeout: Optional[float] = None) -> T:
        """
        Get the highest priority message.

        Implements fair scheduling to prevent low-priority starvation.

        Args:
            timeout: Maximum wait time

        Returns:
            Message data

        Raises:
            asyncio.TimeoutError: If timeout exceeded
        """
        async with self._lock:
            # Fairness check: occasionally check low priority
            if self._gets_since_low_priority >= self._fair_interval:
                for priority in reversed(list(MessagePriority)):
                    queue = self._queues[priority]
                    if not queue.empty():
                        try:
                            message = queue.get_nowait()
                            self._total_size -= 1
                            self._total_get += 1
                            self._gets_since_low_priority = 0
                            return message.data
                        except asyncio.QueueEmpty:
                            continue

        # Normal priority-based get
        while True:
            for priority in MessagePriority:
                queue = self._queues[priority]
                if not queue.empty():
                    try:
                        message = queue.get_nowait()
                        async with self._lock:
                            self._total_size -= 1
                            self._total_get += 1
                            self._gets_since_low_priority += 1
                        return message.data
                    except asyncio.QueueEmpty:
                        continue

            # All empty, wait on critical queue
            try:
                if timeout:
                    message = await asyncio.wait_for(
                        self._queues[MessagePriority.CRITICAL].get(),
                        timeout=timeout
                    )
                else:
                    message = await self._queues[MessagePriority.CRITICAL].get()

                async with self._lock:
                    self._total_size -= 1
                    self._total_get += 1
                    self._gets_since_low_priority += 1

                return message.data

            except asyncio.TimeoutError:
                raise

    def size(self) -> int:
        """Get total queue size."""
        return self._total_size

    def size_by_priority(self) -> Dict[str, int]:
        """Get size by priority level."""
        return {
            priority.name: queue.qsize()
            for priority, queue in self._queues.items()
        }

    def get_metrics(self) -> Dict[str, Any]:
        """Get queue metrics."""
        return {
            "total_size": self._total_size,
            "max_size": self._max_size,
            "by_priority": self.size_by_priority(),
            "total_put": self._total_put,
            "total_get": self._total_get,
            "total_dropped": self._total_dropped,
            "drop_rate": (
                self._total_dropped / max(1, self._total_put)
            ),
        }


# =============================================================================
# 8. ATOMIC FILE IPC
# =============================================================================


class AtomicFileIPC:
    """
    Race-condition-free file-based IPC for Trinity Protocol.

    Uses fcntl file locking with atomic operations to prevent
    TOCTOU (time-of-check-time-of-use) race conditions.

    Features:
        - Atomic read-modify-write operations
        - fcntl-based file locking
        - Automatic lock cleanup
        - Corruption detection

    Environment Variables:
        TRINITY_IPC_DIR: Base directory for IPC files
        TRINITY_IPC_TIMEOUT: Lock acquisition timeout
        TRINITY_IPC_MAX_SIZE: Maximum message size

    Example:
        ipc = AtomicFileIPC("/path/to/trinity")

        # Atomic write
        await ipc.write_message("component", {"type": "heartbeat"})

        # Atomic read
        messages = await ipc.read_messages("component")
    """

    def __init__(self, base_dir: Optional[str] = None):
        self._base_dir = Path(
            base_dir or os.getenv("TRINITY_IPC_DIR", str(Path.home() / ".jarvis" / "trinity"))
        )
        self._timeout = _env_float("TRINITY_IPC_TIMEOUT", 5.0)
        self._max_size = _env_int("TRINITY_IPC_MAX_SIZE", 1024 * 1024)  # 1MB

        # Ensure directory exists
        self._base_dir.mkdir(parents=True, exist_ok=True)

        # Lock tracking
        self._held_locks: Dict[str, int] = {}  # path -> fd

    def _get_queue_path(self, component: str) -> Path:
        """Get queue file path for component."""
        return self._base_dir / f"{component}.queue.json"

    def _get_lock_path(self, component: str) -> Path:
        """Get lock file path for component."""
        return self._base_dir / f"{component}.lock"

    @asynccontextmanager
    async def _acquire_lock(self, component: str):
        """
        Acquire exclusive lock for component with atomic operation.

        This eliminates TOCTOU by acquiring lock BEFORE any file operations.
        """
        lock_path = self._get_lock_path(component)

        # Create lock file if needed (atomic operation)
        lock_path.touch(exist_ok=True)

        # Open for locking
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)

        try:
            # Acquire exclusive lock with timeout
            start_time = time.time()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (IOError, OSError) as e:
                    if time.time() - start_time > self._timeout:
                        raise TimeoutError(
                            f"Lock acquisition timeout for {component} after {self._timeout}s"
                        )
                    await asyncio.sleep(0.01)

            self._held_locks[component] = fd

            yield

        finally:
            # Release lock
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass

            os.close(fd)
            self._held_locks.pop(component, None)

    async def write_message(
        self,
        component: str,
        message: Dict[str, Any],
        append: bool = True,
    ) -> bool:
        """
        Write message atomically.

        Args:
            component: Target component
            message: Message to write
            append: If True, append to queue; if False, replace

        Returns:
            True if successful
        """
        async with self._acquire_lock(component):
            queue_path = self._get_queue_path(component)

            # Read existing messages
            messages = []
            if append and queue_path.exists():
                try:
                    content = queue_path.read_text()
                    if content.strip():
                        messages = json.loads(content)
                except (json.JSONDecodeError, IOError):
                    # Corrupted, start fresh
                    messages = []

            # Add new message with metadata
            messages.append({
                **message,
                "_id": str(uuid.uuid4()),
                "_timestamp": time.time(),
            })

            # Check size limit
            content = json.dumps(messages, default=str)
            if len(content) > self._max_size:
                # Trim oldest messages
                while len(content) > self._max_size and len(messages) > 1:
                    messages.pop(0)
                    content = json.dumps(messages, default=str)

            # Write atomically using temp file
            temp_path = queue_path.with_suffix(".tmp")
            temp_path.write_text(content)
            temp_path.rename(queue_path)

            return True

    async def read_messages(
        self,
        component: str,
        clear: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Read messages atomically.

        Args:
            component: Source component
            clear: If True, clear after reading

        Returns:
            List of messages
        """
        async with self._acquire_lock(component):
            queue_path = self._get_queue_path(component)

            if not queue_path.exists():
                return []

            try:
                content = queue_path.read_text()
                if not content.strip():
                    return []

                messages = json.loads(content)

                if clear:
                    queue_path.write_text("[]")

                return messages

            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"[AtomicIPC] Read error for {component}: {e}")
                # Clear corrupted file
                queue_path.write_text("[]")
                return []

    async def write_heartbeat(
        self,
        component: str,
        status: str = "healthy",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Write heartbeat for component.

        Args:
            component: Component name
            status: Health status
            metadata: Optional additional metadata

        Returns:
            True if successful
        """
        heartbeat_path = self._base_dir / f"{component}.heartbeat.json"

        heartbeat = {
            "component": component,
            "status": status,
            "timestamp": time.time(),
            "pid": os.getpid(),
            **(metadata or {}),
        }

        # Write atomically
        temp_path = heartbeat_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(heartbeat))
        temp_path.rename(heartbeat_path)

        return True

    async def read_heartbeat(self, component: str) -> Optional[Dict[str, Any]]:
        """
        Read heartbeat for component.

        Args:
            component: Component name

        Returns:
            Heartbeat data or None if not found/stale
        """
        heartbeat_path = self._base_dir / f"{component}.heartbeat.json"

        if not heartbeat_path.exists():
            return None

        try:
            content = heartbeat_path.read_text()
            return json.loads(content)
        except (json.JSONDecodeError, IOError):
            return None

    async def is_component_alive(
        self,
        component: str,
        stale_threshold: float = 30.0,
    ) -> bool:
        """
        Check if component is alive based on heartbeat.

        Args:
            component: Component name
            stale_threshold: Seconds before heartbeat considered stale

        Returns:
            True if component is alive
        """
        heartbeat = await self.read_heartbeat(component)
        if heartbeat is None:
            return False

        age = time.time() - heartbeat.get("timestamp", 0)
        return age < stale_threshold


# =============================================================================
# SINGLETON INSTANCES
# =============================================================================

_backpressure: Optional[AdaptiveBackpressure] = None
_bulkhead: Optional[ResourceBulkhead] = None
_resource_manager: Optional[AdaptiveResourceManager] = None
_health_verifier: Optional[DeepHealthVerifier] = None
_rate_limiter: Optional[TrinityRateLimiter] = None
_atomic_ipc: Optional[AtomicFileIPC] = None

_init_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_backpressure() -> AdaptiveBackpressure:
    """Get singleton AdaptiveBackpressure instance."""
    global _backpressure
    if _backpressure is None:
        async with _init_lock:
            if _backpressure is None:
                _backpressure = AdaptiveBackpressure()
                await _backpressure.start()
    return _backpressure


async def get_bulkhead(pools: Optional[Dict[str, int]] = None) -> ResourceBulkhead:
    """Get singleton ResourceBulkhead instance."""
    global _bulkhead
    if _bulkhead is None:
        async with _init_lock:
            if _bulkhead is None:
                _bulkhead = ResourceBulkhead(pools)
    return _bulkhead


async def get_resource_manager() -> AdaptiveResourceManager:
    """Get singleton AdaptiveResourceManager instance."""
    global _resource_manager
    if _resource_manager is None:
        async with _init_lock:
            if _resource_manager is None:
                _resource_manager = AdaptiveResourceManager()
                await _resource_manager.start()
    return _resource_manager


async def get_health_verifier() -> DeepHealthVerifier:
    """Get singleton DeepHealthVerifier instance."""
    global _health_verifier
    if _health_verifier is None:
        async with _init_lock:
            if _health_verifier is None:
                _health_verifier = DeepHealthVerifier()
    return _health_verifier


def get_rate_limiter(
    name: str = "default",
    rate: Optional[float] = None,
    burst: Optional[int] = None,
) -> TrinityRateLimiter:
    """Get or create a TrinityRateLimiter instance."""
    global _rate_limiter
    if _rate_limiter is None or name != "default":
        return TrinityRateLimiter(rate=rate, burst=burst, name=name)
    if _rate_limiter is None:
        _rate_limiter = TrinityRateLimiter(rate=rate, burst=burst, name=name)
    return _rate_limiter


async def get_atomic_ipc(base_dir: Optional[str] = None) -> AtomicFileIPC:
    """Get singleton AtomicFileIPC instance."""
    global _atomic_ipc
    if _atomic_ipc is None:
        async with _init_lock:
            if _atomic_ipc is None:
                _atomic_ipc = AtomicFileIPC(base_dir)
    return _atomic_ipc


async def shutdown_all() -> None:
    """Shutdown all singleton instances."""
    global _backpressure, _resource_manager, _health_verifier

    if _backpressure:
        await _backpressure.stop()
        _backpressure = None

    if _resource_manager:
        await _resource_manager.stop()
        _resource_manager = None

    if _health_verifier:
        await _health_verifier.close()
        _health_verifier = None


# =============================================================================
# DECORATORS
# =============================================================================


def with_timeout(
    timeout: Optional[float] = None,
    timeout_error_message: str = "Operation timed out",
):
    """
    Decorator to add timeout to async function.

    Args:
        timeout: Timeout in seconds (from env if None)
        timeout_error_message: Error message on timeout
    """
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            effective_timeout = timeout or _env_float("DEFAULT_ASYNC_TIMEOUT", 30.0)
            try:
                return await asyncio.wait_for(
                    func(*args, **kwargs),
                    timeout=effective_timeout
                )
            except asyncio.TimeoutError:
                raise TimeoutError(f"{timeout_error_message} after {effective_timeout}s")
        return wrapper
    return decorator


def with_bulkhead(
    pool_name: str,
    fallback: Optional[Callable[..., Awaitable[T]]] = None,
):
    """
    Decorator to execute function within a bulkhead pool.

    Args:
        pool_name: Name of the bulkhead pool
        fallback: Optional fallback function
    """
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            bulkhead = await get_bulkhead()
            try:
                async with bulkhead.execute(pool_name):
                    return await func(*args, **kwargs)
            except (BulkheadCircuitOpen, BulkheadTimeout):
                if fallback:
                    return await fallback(*args, **kwargs)
                raise
        return wrapper
    return decorator


def with_backpressure():
    """Decorator to apply backpressure to async function."""
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            bp = await get_backpressure()
            async with bp.acquire_context():
                return await func(*args, **kwargs)
        return wrapper
    return decorator


def with_rate_limit(
    rate: Optional[float] = None,
    burst: Optional[int] = None,
):
    """
    Decorator to rate limit async function.

    Args:
        rate: Max calls per second
        burst: Maximum burst size
    """
    limiter = TrinityRateLimiter(rate=rate, burst=burst)

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            if not await limiter.acquire_or_wait():
                raise BackpressureRejection("Rate limit exceeded")
            return await func(*args, **kwargs)
        return wrapper
    return decorator


# =============================================================================
# 9. DISTRIBUTED COMMAND LOCK (CRITICAL FIX)
# =============================================================================


@dataclass
class CommandLockEntry:
    """Entry for tracking command lock ownership."""
    command_id: str
    owner_pid: int
    owner_cookie: str  # Unique per-process cookie for PID reuse protection
    acquired_at: float
    expires_at: float


class DistributedCommandLock:
    """
    Distributed lock for Trinity command writes.

    Prevents multiple processes from writing to the same command file
    simultaneously, which can cause data corruption.

    Features:
        - fcntl-based exclusive locks
        - Unique command IDs with microsecond timestamp
        - PID + cookie validation for reuse protection
        - Automatic lock expiration
        - Dead lock detection

    Environment Variables:
        TRINITY_CMD_LOCK_TIMEOUT: Lock acquisition timeout (default 10s)
        TRINITY_CMD_LOCK_EXPIRY: Lock expiration time (default 60s)
    """

    def __init__(self, base_dir: Optional[str] = None):
        self._base_dir = Path(
            base_dir or os.getenv("TRINITY_IPC_DIR", str(Path.home() / ".jarvis" / "trinity"))
        )
        self._lock_timeout = _env_float("TRINITY_CMD_LOCK_TIMEOUT", 10.0)
        self._lock_expiry = _env_float("TRINITY_CMD_LOCK_EXPIRY", 60.0)

        # Unique cookie for this process (survives across PID reuse)
        self._process_cookie = f"{os.getpid()}_{uuid.uuid4().hex[:8]}_{time.time()}"

        # Lock directory
        self._lock_dir = self._base_dir / "locks"
        self._lock_dir.mkdir(parents=True, exist_ok=True)

        # Active locks held by this process
        self._active_locks: Dict[str, int] = {}  # command_id -> fd

    def generate_unique_command_id(self) -> str:
        """
        Generate unique command ID with microsecond precision.

        Format: {timestamp_micros}_{uuid_short}_{pid}
        This prevents ID collisions even with simultaneous writes.
        """
        timestamp_micros = int(time.time() * 1_000_000)
        unique_part = uuid.uuid4().hex[:8]
        return f"{timestamp_micros}_{unique_part}_{os.getpid()}"

    @asynccontextmanager
    async def acquire(self, command_id: str):
        """
        Acquire exclusive lock for a command.

        Args:
            command_id: Unique command identifier

        Yields:
            CommandLockEntry with lock details
        """
        lock_file = self._lock_dir / f"{command_id}.lock"
        lock_meta = self._lock_dir / f"{command_id}.meta"

        fd = None
        try:
            # Create lock file atomically
            fd = os.open(
                str(lock_file),
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )

            # Try to acquire exclusive lock with timeout
            start_time = time.time()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (IOError, OSError):
                    if time.time() - start_time > self._lock_timeout:
                        os.close(fd)
                        try:
                            os.unlink(str(lock_file))
                        except OSError:
                            pass
                        raise TimeoutError(
                            f"Command lock timeout for {command_id} after {self._lock_timeout}s"
                        )
                    await asyncio.sleep(0.01)

            # Write lock metadata
            now = time.time()
            entry = CommandLockEntry(
                command_id=command_id,
                owner_pid=os.getpid(),
                owner_cookie=self._process_cookie,
                acquired_at=now,
                expires_at=now + self._lock_expiry,
            )

            # Write metadata atomically
            meta_content = json.dumps({
                "command_id": entry.command_id,
                "owner_pid": entry.owner_pid,
                "owner_cookie": entry.owner_cookie,
                "acquired_at": entry.acquired_at,
                "expires_at": entry.expires_at,
            })
            temp_meta = lock_meta.with_suffix(".tmp")
            temp_meta.write_text(meta_content)
            temp_meta.rename(lock_meta)

            self._active_locks[command_id] = fd

            yield entry

        except FileExistsError:
            # Lock file already exists - check if stale
            if lock_meta.exists():
                try:
                    meta = json.loads(lock_meta.read_text())
                    if time.time() > meta.get("expires_at", 0):
                        # Stale lock - clean up and retry
                        logger.warning(f"Cleaning stale lock for {command_id}")
                        try:
                            os.unlink(str(lock_file))
                            os.unlink(str(lock_meta))
                        except OSError:
                            pass
                        # Recursive retry
                        async with self.acquire(command_id) as entry:
                            yield entry
                            return
                except (json.JSONDecodeError, OSError):
                    pass
            raise RuntimeError(f"Command {command_id} is already locked by another process")

        finally:
            # Release lock
            if fd is not None and command_id in self._active_locks:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    os.close(fd)
                except Exception:
                    pass
                try:
                    os.unlink(str(lock_file))
                    os.unlink(str(lock_meta))
                except OSError:
                    pass
                self._active_locks.pop(command_id, None)

    async def cleanup_stale_locks(self) -> int:
        """Clean up expired locks from dead processes."""
        cleaned = 0
        now = time.time()

        for meta_file in self._lock_dir.glob("*.meta"):
            try:
                meta = json.loads(meta_file.read_text())

                # Check if expired
                if now > meta.get("expires_at", 0):
                    command_id = meta.get("command_id", meta_file.stem)
                    lock_file = self._lock_dir / f"{command_id}.lock"

                    try:
                        os.unlink(str(lock_file))
                    except OSError:
                        pass
                    os.unlink(str(meta_file))
                    cleaned += 1
                    logger.debug(f"Cleaned stale lock: {command_id}")

            except (json.JSONDecodeError, OSError) as e:
                logger.debug(f"Error cleaning lock {meta_file}: {e}")

        return cleaned


# =============================================================================
# 10. PROCESS COOKIE VALIDATOR (PID REUSE PROTECTION)
# =============================================================================


class ProcessCookieValidator:
    """
    Validates process identity to prevent PID reuse attacks.

    When a process starts, it registers a unique cookie.
    Before sending signals, we verify the target PID still has
    the expected cookie (hasn't been replaced by a new process).

    Features:
        - Unique per-process cookie generation
        - Atomic cookie file operations
        - Race-safe signal sending
        - Automatic cleanup of stale cookies

    Environment Variables:
        TRINITY_COOKIE_DIR: Directory for cookie files
        TRINITY_COOKIE_TTL: Time-to-live for cookies
    """

    def __init__(self, base_dir: Optional[str] = None):
        self._base_dir = Path(
            base_dir or os.getenv("TRINITY_COOKIE_DIR", str(Path.home() / ".jarvis" / "trinity" / "cookies"))
        )
        self._ttl = _env_float("TRINITY_COOKIE_TTL", 86400.0)  # 24 hours

        self._base_dir.mkdir(parents=True, exist_ok=True)

        # Generate unique cookie for this process
        self._cookie = f"{uuid.uuid4().hex}_{time.time()}"
        self._registered = False

    def get_cookie_path(self, pid: int) -> Path:
        """Get cookie file path for a PID."""
        return self._base_dir / f"pid_{pid}.cookie"

    async def register(self) -> str:
        """
        Register this process's cookie.

        Returns:
            The unique cookie for this process
        """
        pid = os.getpid()
        cookie_path = self.get_cookie_path(pid)

        cookie_data = {
            "pid": pid,
            "cookie": self._cookie,
            "start_time": time.time(),
            "expires_at": time.time() + self._ttl,
            "cmdline": " ".join(sys.argv[:3]),  # First 3 args for identification
        }

        # Write atomically
        temp_path = cookie_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(cookie_data))
        temp_path.rename(cookie_path)

        self._registered = True
        logger.debug(f"Registered process cookie for PID {pid}")

        return self._cookie

    async def validate(self, pid: int, expected_cookie: str) -> bool:
        """
        Validate that a PID still has the expected cookie.

        Args:
            pid: Process ID to validate
            expected_cookie: Expected cookie value

        Returns:
            True if PID has expected cookie, False otherwise
        """
        cookie_path = self.get_cookie_path(pid)

        if not cookie_path.exists():
            return False

        try:
            data = json.loads(cookie_path.read_text())

            # Check cookie match
            if data.get("cookie") != expected_cookie:
                return False

            # Check not expired
            if time.time() > data.get("expires_at", 0):
                return False

            # Check PID match
            if data.get("pid") != pid:
                return False

            return True

        except (json.JSONDecodeError, OSError):
            return False

    async def send_signal_safe(
        self,
        pid: int,
        expected_cookie: str,
        signal: int,
    ) -> Tuple[bool, str]:
        """
        Send signal to process only if cookie validates.

        This prevents PID reuse attacks where a new process
        takes over the PID of a terminated process.

        Args:
            pid: Target PID
            expected_cookie: Expected cookie
            signal: Signal to send

        Returns:
            (success, message)
        """
        # First validate cookie
        if not await self.validate(pid, expected_cookie):
            return False, f"Cookie validation failed for PID {pid} (possible PID reuse)"

        # Then send signal
        try:
            os.kill(pid, signal)
            return True, f"Signal {signal} sent to PID {pid}"
        except ProcessLookupError:
            return False, f"Process {pid} no longer exists"
        except PermissionError:
            return False, f"Permission denied sending signal to PID {pid}"
        except OSError as e:
            return False, f"OS error sending signal: {e}"

    async def cleanup_stale_cookies(self) -> int:
        """Clean up expired or orphaned cookie files."""
        cleaned = 0
        now = time.time()

        for cookie_file in self._base_dir.glob("pid_*.cookie"):
            try:
                data = json.loads(cookie_file.read_text())
                pid = data.get("pid", 0)

                # Check if expired
                if now > data.get("expires_at", 0):
                    os.unlink(str(cookie_file))
                    cleaned += 1
                    continue

                # Check if process is dead
                if pid > 0:
                    try:
                        os.kill(pid, 0)  # Check if process exists
                    except ProcessLookupError:
                        os.unlink(str(cookie_file))
                        cleaned += 1
                    except PermissionError:
                        pass  # Process exists but we can't signal it

            except (json.JSONDecodeError, OSError):
                try:
                    os.unlink(str(cookie_file))
                    cleaned += 1
                except OSError:
                    pass

        return cleaned


# =============================================================================
# 11. STARTUP BARRIER (COORDINATED INITIALIZATION)
# =============================================================================


@dataclass
class ComponentReadiness:
    """Readiness state for a component."""
    name: str
    ready: bool
    timestamp: float
    health_check_passed: bool
    message: str = ""


class StartupBarrier:
    """
    Coordinated startup barrier for Trinity components.

    Ensures all required components are ready before proceeding.
    Implements a distributed barrier using file-based signaling.

    Features:
        - Wait for N components to be ready
        - Timeout with detailed status
        - Health check integration
        - Graceful degradation option

    Environment Variables:
        TRINITY_STARTUP_TIMEOUT: Maximum wait time
        TRINITY_STARTUP_POLL_INTERVAL: Poll interval
        TRINITY_STARTUP_REQUIRED: Comma-separated required components
    """

    def __init__(self, base_dir: Optional[str] = None):
        self._base_dir = Path(
            base_dir or os.getenv("TRINITY_IPC_DIR", str(Path.home() / ".jarvis" / "trinity"))
        )
        self._timeout = _env_float("TRINITY_STARTUP_TIMEOUT", 120.0)
        self._poll_interval = _env_float("TRINITY_STARTUP_POLL_INTERVAL", 0.5)

        required_str = os.getenv("TRINITY_STARTUP_REQUIRED", "jarvis_prime,reactor_core")
        self._required_components = set(c.strip() for c in required_str.split(",") if c.strip())

        self._ready_dir = self._base_dir / "ready"
        self._ready_dir.mkdir(parents=True, exist_ok=True)

    async def signal_ready(self, component_name: str, health_check_passed: bool = True) -> None:
        """
        Signal that a component is ready.

        Args:
            component_name: Name of the ready component
            health_check_passed: Whether health check passed
        """
        ready_file = self._ready_dir / f"{component_name}.ready"

        data = {
            "name": component_name,
            "ready": True,
            "timestamp": time.time(),
            "health_check_passed": health_check_passed,
            "pid": os.getpid(),
        }

        # Write atomically
        temp_file = ready_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(data))
        temp_file.rename(ready_file)

        logger.info(f"[StartupBarrier] {component_name} signaled ready")

    async def signal_not_ready(self, component_name: str, reason: str = "") -> None:
        """Signal that a component is not ready (for shutdown or failure)."""
        ready_file = self._ready_dir / f"{component_name}.ready"
        try:
            os.unlink(str(ready_file))
        except OSError:
            pass
        logger.info(f"[StartupBarrier] {component_name} signaled not ready: {reason}")

    async def wait_for_all(
        self,
        required: Optional[Set[str]] = None,
        timeout: Optional[float] = None,
        require_health: bool = True,
    ) -> Tuple[bool, Dict[str, ComponentReadiness]]:
        """
        Wait for all required components to be ready.

        Args:
            required: Set of required component names (defaults to config)
            timeout: Timeout in seconds (defaults to config)
            require_health: Require health checks to pass

        Returns:
            (all_ready, component_states)
        """
        required = required or self._required_components
        timeout = timeout or self._timeout
        start_time = time.time()

        logger.info(f"[StartupBarrier] Waiting for components: {required}")

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                # Timeout - return current state
                states = await self._get_component_states(required, require_health)
                missing = [n for n, s in states.items() if not s.ready]
                logger.warning(
                    f"[StartupBarrier] Timeout after {elapsed:.1f}s. Missing: {missing}"
                )
                return False, states

            # Check all components
            states = await self._get_component_states(required, require_health)
            all_ready = all(s.ready and (not require_health or s.health_check_passed) for s in states.values())

            if all_ready:
                logger.info(f"[StartupBarrier] All components ready after {elapsed:.1f}s")
                return True, states

            # Log progress periodically
            if int(elapsed) % 10 == 0 and elapsed > 0:
                ready_count = sum(1 for s in states.values() if s.ready)
                logger.debug(
                    f"[StartupBarrier] {ready_count}/{len(required)} ready after {elapsed:.1f}s"
                )

            await asyncio.sleep(self._poll_interval)

    async def _get_component_states(
        self,
        required: Set[str],
        require_health: bool,
    ) -> Dict[str, ComponentReadiness]:
        """Get readiness state for all required components."""
        states = {}
        now = time.time()

        for name in required:
            ready_file = self._ready_dir / f"{name}.ready"

            if not ready_file.exists():
                states[name] = ComponentReadiness(
                    name=name,
                    ready=False,
                    timestamp=0,
                    health_check_passed=False,
                    message="Not yet signaled ready",
                )
                continue

            try:
                data = json.loads(ready_file.read_text())

                # Check staleness (> 30 seconds old is considered stale)
                age = now - data.get("timestamp", 0)
                is_stale = age > 30.0

                states[name] = ComponentReadiness(
                    name=name,
                    ready=data.get("ready", False) and not is_stale,
                    timestamp=data.get("timestamp", 0),
                    health_check_passed=data.get("health_check_passed", False),
                    message=f"Stale ({age:.1f}s)" if is_stale else "OK",
                )

            except (json.JSONDecodeError, OSError) as e:
                states[name] = ComponentReadiness(
                    name=name,
                    ready=False,
                    timestamp=0,
                    health_check_passed=False,
                    message=f"Read error: {e}",
                )

        return states

    async def cleanup(self) -> None:
        """Clean up all ready signals (for shutdown)."""
        for ready_file in self._ready_dir.glob("*.ready"):
            try:
                os.unlink(str(ready_file))
            except OSError:
                pass


# =============================================================================
# 12. HEARTBEAT CACHE MANAGER (CACHE INVALIDATION)
# =============================================================================


@dataclass
class HeartbeatEntry:
    """Cached heartbeat entry with validity tracking."""
    component: str
    data: Dict[str, Any]
    fetched_at: float
    file_mtime: float
    is_valid: bool = True


class HeartbeatCacheManager:
    """
    Intelligent heartbeat cache with automatic invalidation.

    Solves the stale cache problem by:
    1. Tracking file modification times
    2. Automatic cache invalidation on file changes
    3. TTL-based expiration
    4. Proactive refresh on access

    Features:
        - File-based cache invalidation (mtime tracking)
        - TTL-based expiration
        - Automatic refresh
        - Thread-safe access

    Environment Variables:
        TRINITY_HEARTBEAT_TTL: Cache TTL in seconds
        TRINITY_HEARTBEAT_DIR: Directory for heartbeat files
    """

    def __init__(self, base_dir: Optional[str] = None):
        self._base_dir = Path(
            base_dir or os.getenv("TRINITY_IPC_DIR", str(Path.home() / ".jarvis" / "trinity"))
        )
        self._ttl = _env_float("TRINITY_HEARTBEAT_TTL", 5.0)  # 5 seconds

        self._components_dir = self._base_dir / "components"
        self._components_dir.mkdir(parents=True, exist_ok=True)

        # Cache storage
        self._cache: Dict[str, HeartbeatEntry] = {}
        self._lock = asyncio.Lock()

    def _get_heartbeat_path(self, component: str) -> Path:
        """Get heartbeat file path for component."""
        return self._components_dir / f"{component}.json"

    async def get(self, component: str, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        """
        Get heartbeat data for a component.

        Automatically refreshes cache if:
        - Cache entry doesn't exist
        - Cache entry is expired (TTL)
        - File modification time changed
        - force_refresh is True

        Args:
            component: Component name
            force_refresh: Force cache refresh

        Returns:
            Heartbeat data or None if not available
        """
        async with self._lock:
            heartbeat_path = self._get_heartbeat_path(component)

            # Check if file exists
            if not heartbeat_path.exists():
                self._cache.pop(component, None)
                return None

            # Get file mtime
            try:
                file_mtime = heartbeat_path.stat().st_mtime
            except OSError:
                self._cache.pop(component, None)
                return None

            # Check cache
            cached = self._cache.get(component)
            now = time.time()

            need_refresh = (
                force_refresh
                or cached is None
                or not cached.is_valid
                or (now - cached.fetched_at) > self._ttl
                or cached.file_mtime != file_mtime  # File changed!
            )

            if need_refresh:
                # Read fresh data
                try:
                    data = json.loads(heartbeat_path.read_text())

                    self._cache[component] = HeartbeatEntry(
                        component=component,
                        data=data,
                        fetched_at=now,
                        file_mtime=file_mtime,
                        is_valid=True,
                    )

                    return data

                except (json.JSONDecodeError, OSError) as e:
                    logger.debug(f"Error reading heartbeat for {component}: {e}")
                    if cached:
                        cached.is_valid = False
                    return None

            return cached.data if cached else None

    async def is_component_online(
        self,
        component: str,
        max_age: float = 15.0,
    ) -> bool:
        """
        Check if a component is online based on heartbeat.

        Uses fresh data (cache-invalidated) to avoid stale reads.

        Args:
            component: Component name
            max_age: Maximum heartbeat age in seconds

        Returns:
            True if component is online (recent heartbeat)
        """
        data = await self.get(component, force_refresh=True)

        if not data:
            return False

        timestamp = data.get("timestamp", 0)
        age = time.time() - timestamp

        return age < max_age

    async def invalidate(self, component: str) -> None:
        """Invalidate cache for a component."""
        async with self._lock:
            if component in self._cache:
                self._cache[component].is_valid = False

    async def invalidate_all(self) -> None:
        """Invalidate all cached entries."""
        async with self._lock:
            for entry in self._cache.values():
                entry.is_valid = False

    async def get_all_components(self) -> Dict[str, Dict[str, Any]]:
        """Get heartbeat data for all components."""
        result = {}

        for heartbeat_file in self._components_dir.glob("*.json"):
            component = heartbeat_file.stem
            data = await self.get(component)
            if data:
                result[component] = data

        return result

    async def get_health_summary(self, max_age: float = 15.0) -> Dict[str, Any]:
        """
        Get health summary for all components.

        Returns:
            Dict with component statuses and overall health
        """
        components = await self.get_all_components()
        now = time.time()

        statuses = {}
        for name, data in components.items():
            timestamp = data.get("timestamp", 0)
            age = now - timestamp
            statuses[name] = {
                "online": age < max_age,
                "age_seconds": age,
                "status": data.get("status", "unknown"),
            }

        online_count = sum(1 for s in statuses.values() if s["online"])
        total_count = len(statuses)

        return {
            "components": statuses,
            "online_count": online_count,
            "total_count": total_count,
            "all_healthy": online_count == total_count and total_count > 0,
            "timestamp": now,
        }


# =============================================================================
# SINGLETON ACCESSORS FOR NEW COMPONENTS
# =============================================================================

_distributed_lock: Optional[DistributedCommandLock] = None
_process_cookie: Optional[ProcessCookieValidator] = None
_startup_barrier: Optional[StartupBarrier] = None
_heartbeat_cache: Optional[HeartbeatCacheManager] = None


async def get_distributed_lock() -> DistributedCommandLock:
    """Get singleton DistributedCommandLock instance."""
    global _distributed_lock
    if _distributed_lock is None:
        _distributed_lock = DistributedCommandLock()
    return _distributed_lock


async def get_process_cookie() -> ProcessCookieValidator:
    """Get singleton ProcessCookieValidator instance."""
    global _process_cookie
    if _process_cookie is None:
        _process_cookie = ProcessCookieValidator()
        await _process_cookie.register()
    return _process_cookie


async def get_startup_barrier() -> StartupBarrier:
    """Get singleton StartupBarrier instance."""
    global _startup_barrier
    if _startup_barrier is None:
        _startup_barrier = StartupBarrier()
    return _startup_barrier


async def get_heartbeat_cache() -> HeartbeatCacheManager:
    """Get singleton HeartbeatCacheManager instance."""
    global _heartbeat_cache
    if _heartbeat_cache is None:
        _heartbeat_cache = HeartbeatCacheManager()
    return _heartbeat_cache


# =============================================================================
# GRACEFUL SHUTDOWN COORDINATOR
# =============================================================================


class ShutdownPhase(str, Enum):
    """Phases of graceful shutdown."""
    NOT_STARTED = "not_started"
    DRAINING = "draining"
    WAITING_INFLIGHT = "waiting_inflight"
    STOPPING_COMPONENTS = "stopping_components"
    FLUSHING_DLQ = "flushing_dlq"
    CLEANUP = "cleanup"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ShutdownState:
    """State tracking for shutdown process."""
    phase: ShutdownPhase = ShutdownPhase.NOT_STARTED
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    components_stopped: List[str] = field(default_factory=list)
    inflight_commands_drained: int = 0
    dlq_items_flushed: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def duration(self) -> Optional[float]:
        """Get shutdown duration in seconds."""
        if self.started_at is None:
            return None
        end = self.completed_at or time.time()
        return end - self.started_at

    @property
    def is_complete(self) -> bool:
        """Check if shutdown completed."""
        return self.phase in (ShutdownPhase.COMPLETED, ShutdownPhase.FAILED)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "phase": self.phase.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration,
            "components_stopped": self.components_stopped,
            "inflight_commands_drained": self.inflight_commands_drained,
            "dlq_items_flushed": self.dlq_items_flushed,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class GracefulShutdownCoordinator:
    """
    Coordinates graceful shutdown across Trinity components.

    Shutdown order:
    1. Signal all components to stop accepting new commands (DRAINING)
    2. Wait for in-flight commands to complete with timeout (WAITING_INFLIGHT)
    3. Stop Reactor-Core first (downstream)
    4. Stop J-Prime (middle tier)
    5. Stop JARVIS-AI-Agent (upstream/coordinator)
    6. Flush DLQ to persistent storage
    7. Clean up resources

    Environment Variables:
        SHUTDOWN_DRAIN_TIMEOUT: Max time to wait for drain (default: 5s)
        SHUTDOWN_INFLIGHT_TIMEOUT: Max time to wait for in-flight (default: 30s)
        SHUTDOWN_COMPONENT_TIMEOUT: Max time per component stop (default: 10s)
        SHUTDOWN_DLQ_FLUSH_TIMEOUT: Max time for DLQ flush (default: 15s)
        SHUTDOWN_SIGNAL_FILE_DIR: Directory for shutdown signals
        SHUTDOWN_FORCE_AFTER: Force kill after this total time (default: 120s)
    """

    # Component shutdown order (downstream to upstream)
    SHUTDOWN_ORDER = ["reactor_core", "jarvis_prime", "jarvis_agent"]

    def __init__(
        self,
        signal_dir: Optional[Path] = None,
        heartbeat_manager: Optional[HeartbeatCacheManager] = None,
        process_validator: Optional[ProcessCookieValidator] = None,
    ):
        """
        Initialize shutdown coordinator.

        Args:
            signal_dir: Directory for shutdown signal files
            heartbeat_manager: Optional heartbeat cache manager
            process_validator: Optional process cookie validator
        """
        env_dir = os.getenv("SHUTDOWN_SIGNAL_FILE_DIR")
        if signal_dir:
            self._signal_dir = signal_dir
        elif env_dir:
            self._signal_dir = Path(env_dir)
        else:
            jarvis_base = os.getenv("JARVIS_BASE_DIR", os.path.expanduser("~/.jarvis"))
            self._signal_dir = Path(jarvis_base) / "trinity" / "shutdown"

        self._signal_dir.mkdir(parents=True, exist_ok=True)

        # Timeouts from environment
        self._drain_timeout = _env_float("SHUTDOWN_DRAIN_TIMEOUT", 5.0)
        self._inflight_timeout = _env_float("SHUTDOWN_INFLIGHT_TIMEOUT", 30.0)
        self._component_timeout = _env_float("SHUTDOWN_COMPONENT_TIMEOUT", 10.0)
        self._dlq_flush_timeout = _env_float("SHUTDOWN_DLQ_FLUSH_TIMEOUT", 15.0)
        self._force_after = _env_float("SHUTDOWN_FORCE_AFTER", 120.0)

        # Managers
        self._heartbeat_manager = heartbeat_manager
        self._process_validator = process_validator

        # State
        self._state = ShutdownState()
        self._lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        self._inflight_counter = 0
        self._inflight_lock = asyncio.Lock()
        self._component_callbacks: Dict[str, Callable[[], Awaitable[bool]]] = {}
        self._dlq_flush_callback: Optional[Callable[[], Awaitable[int]]] = None

        # Logger
        self._logger = logging.getLogger("jarvis.shutdown")

    def register_component_callback(
        self,
        component: str,
        callback: Callable[[], Awaitable[bool]],
    ) -> None:
        """
        Register a shutdown callback for a component.

        Args:
            component: Component name
            callback: Async function that returns True on success
        """
        self._component_callbacks[component] = callback
        self._logger.info(f"Registered shutdown callback for {component}")

    def register_dlq_flush_callback(
        self,
        callback: Callable[[], Awaitable[int]],
    ) -> None:
        """
        Register callback to flush DLQ.

        Args:
            callback: Async function that returns count of items flushed
        """
        self._dlq_flush_callback = callback
        self._logger.info("Registered DLQ flush callback")

    async def register_inflight(self) -> str:
        """
        Register an in-flight command.

        Returns:
            Token to use when completing the command

        Raises:
            RuntimeError: If shutdown is in progress
        """
        async with self._inflight_lock:
            if self._state.phase != ShutdownPhase.NOT_STARTED:
                raise RuntimeError(
                    f"Cannot register new command during shutdown "
                    f"(phase: {self._state.phase.value})"
                )
            self._inflight_counter += 1
            token = f"inflight_{time.time()}_{self._inflight_counter}"
            return token

    async def complete_inflight(self, token: str) -> None:
        """
        Mark an in-flight command as complete.

        Args:
            token: Token from register_inflight
        """
        async with self._inflight_lock:
            self._inflight_counter = max(0, self._inflight_counter - 1)
            if (
                self._state.phase == ShutdownPhase.WAITING_INFLIGHT
                and self._inflight_counter == 0
            ):
                self._state.inflight_commands_drained += 1

    async def get_inflight_count(self) -> int:
        """Get current in-flight command count."""
        async with self._inflight_lock:
            return self._inflight_counter

    def is_shutting_down(self) -> bool:
        """Check if shutdown is in progress."""
        return self._state.phase != ShutdownPhase.NOT_STARTED

    def get_state(self) -> ShutdownState:
        """Get current shutdown state."""
        return self._state

    async def _signal_drain(self) -> bool:
        """
        Signal all components to stop accepting new commands.

        Returns:
            True if all components acknowledged
        """
        self._state.phase = ShutdownPhase.DRAINING
        self._logger.info("Signaling all components to drain...")

        # Write drain signal file
        drain_file = self._signal_dir / "drain.signal"
        drain_data = {
            "initiated_by": os.getpid(),
            "timestamp": time.time(),
            "timeout": self._drain_timeout,
        }

        try:
            async with aiofiles.open(drain_file, 'w') as f:
                await f.write(json.dumps(drain_data, indent=2))
        except Exception as e:
            self._state.errors.append(f"Failed to write drain signal: {e}")
            self._logger.error(f"Failed to write drain signal: {e}")
            return False

        # Wait for acknowledgments with timeout
        start = time.time()
        acknowledged = set()

        while time.time() - start < self._drain_timeout:
            for component in self.SHUTDOWN_ORDER:
                ack_file = self._signal_dir / f"{component}.drain_ack"
                if ack_file.exists() and component not in acknowledged:
                    acknowledged.add(component)
                    self._logger.info(f"Component {component} acknowledged drain")

            if len(acknowledged) >= len(self.SHUTDOWN_ORDER):
                break

            await asyncio.sleep(0.1)

        missing = set(self.SHUTDOWN_ORDER) - acknowledged
        if missing:
            self._state.warnings.append(
                f"Components did not acknowledge drain: {missing}"
            )
            self._logger.warning(f"Missing drain acks from: {missing}")

        return len(acknowledged) == len(self.SHUTDOWN_ORDER)

    async def _wait_inflight(self) -> bool:
        """
        Wait for all in-flight commands to complete.

        Returns:
            True if all completed within timeout
        """
        self._state.phase = ShutdownPhase.WAITING_INFLIGHT
        self._logger.info("Waiting for in-flight commands to complete...")

        start = time.time()
        initial_count = await self.get_inflight_count()

        while time.time() - start < self._inflight_timeout:
            count = await self.get_inflight_count()
            if count == 0:
                elapsed = time.time() - start
                self._state.inflight_commands_drained = initial_count
                self._logger.info(
                    f"All {initial_count} in-flight commands completed "
                    f"in {elapsed:.2f}s"
                )
                return True

            # Log progress
            if int(time.time() - start) % 5 == 0:
                self._logger.info(
                    f"Waiting for {count} in-flight commands "
                    f"({time.time() - start:.1f}s elapsed)"
                )

            await asyncio.sleep(0.1)

        remaining = await self.get_inflight_count()
        self._state.warnings.append(
            f"Timeout waiting for in-flight commands: {remaining} remaining"
        )
        self._logger.warning(
            f"Timeout: {remaining} commands still in-flight after "
            f"{self._inflight_timeout}s"
        )
        return False

    async def _stop_component(self, component: str) -> bool:
        """
        Stop a single component.

        Args:
            component: Component name

        Returns:
            True if stopped successfully
        """
        self._logger.info(f"Stopping component: {component}")

        # Try registered callback first
        if component in self._component_callbacks:
            try:
                success = await asyncio.wait_for(
                    self._component_callbacks[component](),
                    timeout=self._component_timeout,
                )
                if success:
                    self._state.components_stopped.append(component)
                    self._logger.info(f"Component {component} stopped via callback")
                    return True
            except asyncio.TimeoutError:
                self._state.warnings.append(
                    f"Callback timeout for {component}"
                )
                self._logger.warning(f"Callback timeout for {component}")
            except Exception as e:
                self._state.errors.append(
                    f"Callback error for {component}: {e}"
                )
                self._logger.error(f"Callback error for {component}: {e}")

        # Try signal-based shutdown
        shutdown_file = self._signal_dir / f"{component}.shutdown"
        shutdown_data = {
            "initiated_by": os.getpid(),
            "timestamp": time.time(),
            "component": component,
        }

        try:
            async with aiofiles.open(shutdown_file, 'w') as f:
                await f.write(json.dumps(shutdown_data, indent=2))
        except Exception as e:
            self._state.errors.append(f"Failed to write shutdown signal: {e}")
            self._logger.error(f"Failed to write shutdown signal for {component}: {e}")
            return False

        # Try sending SIGTERM if we have process info
        if self._process_validator and self._heartbeat_manager:
            try:
                heartbeat = await self._heartbeat_manager.get(component)
                if heartbeat:
                    pid = heartbeat.get("pid")
                    cookie = heartbeat.get("process_cookie")
                    if pid and cookie:
                        success, msg = await self._process_validator.send_signal_safe(
                            pid, cookie, signal.SIGTERM
                        )
                        if success:
                            self._logger.info(f"Sent SIGTERM to {component} (PID {pid})")
            except Exception as e:
                self._logger.warning(f"Could not send signal to {component}: {e}")

        # Wait for confirmation
        start = time.time()
        while time.time() - start < self._component_timeout:
            ack_file = self._signal_dir / f"{component}.shutdown_ack"
            if ack_file.exists():
                self._state.components_stopped.append(component)
                self._logger.info(f"Component {component} confirmed shutdown")
                return True

            # Check if heartbeat stopped
            if self._heartbeat_manager:
                is_online = await self._heartbeat_manager.is_component_online(
                    component, max_age=5.0
                )
                if not is_online:
                    self._state.components_stopped.append(component)
                    self._logger.info(
                        f"Component {component} stopped (heartbeat expired)"
                    )
                    return True

            await asyncio.sleep(0.2)

        self._state.warnings.append(
            f"Component {component} did not confirm shutdown"
        )
        self._logger.warning(f"Timeout waiting for {component} to confirm shutdown")
        return False

    async def _stop_all_components(self) -> bool:
        """
        Stop all components in order.

        Returns:
            True if all components stopped
        """
        self._state.phase = ShutdownPhase.STOPPING_COMPONENTS
        self._logger.info(f"Stopping components in order: {self.SHUTDOWN_ORDER}")

        all_success = True
        for component in self.SHUTDOWN_ORDER:
            success = await self._stop_component(component)
            if not success:
                all_success = False

        return all_success

    async def _flush_dlq(self) -> int:
        """
        Flush DLQ to persistent storage.

        Returns:
            Number of items flushed
        """
        self._state.phase = ShutdownPhase.FLUSHING_DLQ
        self._logger.info("Flushing DLQ to persistent storage...")

        if not self._dlq_flush_callback:
            self._logger.info("No DLQ flush callback registered")
            return 0

        try:
            count = await asyncio.wait_for(
                self._dlq_flush_callback(),
                timeout=self._dlq_flush_timeout,
            )
            self._state.dlq_items_flushed = count
            self._logger.info(f"Flushed {count} items from DLQ")
            return count
        except asyncio.TimeoutError:
            self._state.errors.append("DLQ flush timeout")
            self._logger.error(f"DLQ flush timeout after {self._dlq_flush_timeout}s")
            return 0
        except Exception as e:
            self._state.errors.append(f"DLQ flush error: {e}")
            self._logger.error(f"DLQ flush error: {e}")
            return 0

    async def _cleanup(self) -> None:
        """Clean up shutdown signals and resources."""
        self._state.phase = ShutdownPhase.CLEANUP
        self._logger.info("Cleaning up shutdown signals...")

        # Remove signal files
        try:
            for f in self._signal_dir.glob("*.signal"):
                f.unlink(missing_ok=True)
            for f in self._signal_dir.glob("*.shutdown"):
                f.unlink(missing_ok=True)
            for f in self._signal_dir.glob("*_ack"):
                f.unlink(missing_ok=True)
        except Exception as e:
            self._state.warnings.append(f"Cleanup error: {e}")
            self._logger.warning(f"Cleanup error: {e}")

        # Signal completion
        self._shutdown_event.set()

    async def initiate_shutdown(self, reason: str = "requested") -> ShutdownState:
        """
        Initiate graceful shutdown of all Trinity components.

        Args:
            reason: Reason for shutdown

        Returns:
            Final shutdown state
        """
        async with self._lock:
            if self._state.phase != ShutdownPhase.NOT_STARTED:
                self._logger.warning(
                    f"Shutdown already in progress (phase: {self._state.phase.value})"
                )
                return self._state

            self._state.started_at = time.time()
            self._logger.info(f"Initiating graceful shutdown: {reason}")

            try:
                # Set up force timeout
                async def force_shutdown():
                    await asyncio.sleep(self._force_after)
                    if not self._state.is_complete:
                        self._logger.critical(
                            f"Force shutdown after {self._force_after}s"
                        )
                        self._state.phase = ShutdownPhase.FAILED
                        self._state.errors.append("Force shutdown timeout")
                        self._state.completed_at = time.time()
                        self._shutdown_event.set()

                force_task = asyncio.create_task(force_shutdown())

                # Phase 1: Signal drain
                await self._signal_drain()

                # Phase 2: Wait for in-flight
                await self._wait_inflight()

                # Phase 3: Stop components in order
                await self._stop_all_components()

                # Phase 4: Flush DLQ
                await self._flush_dlq()

                # Phase 5: Cleanup
                await self._cleanup()

                # Mark complete
                self._state.phase = ShutdownPhase.COMPLETED
                self._state.completed_at = time.time()

                # Cancel force timeout
                force_task.cancel()
                try:
                    await force_task
                except asyncio.CancelledError:
                    pass

                self._logger.info(
                    f"Graceful shutdown completed in {self._state.duration:.2f}s. "
                    f"Components stopped: {len(self._state.components_stopped)}, "
                    f"DLQ items flushed: {self._state.dlq_items_flushed}"
                )

            except Exception as e:
                self._state.phase = ShutdownPhase.FAILED
                self._state.errors.append(f"Shutdown error: {e}")
                self._state.completed_at = time.time()
                self._logger.error(f"Shutdown failed: {e}")
                self._logger.exception("Shutdown exception details:")

            return self._state

    async def wait_for_shutdown(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for shutdown to complete.

        Args:
            timeout: Optional timeout in seconds

        Returns:
            True if shutdown completed, False if timeout
        """
        try:
            await asyncio.wait_for(
                self._shutdown_event.wait(),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            return False

    async def acknowledge_drain(self, component: str) -> bool:
        """
        Acknowledge drain signal (called by components).

        Args:
            component: Component name

        Returns:
            True if acknowledgment written
        """
        ack_file = self._signal_dir / f"{component}.drain_ack"
        ack_data = {
            "component": component,
            "acknowledged_at": time.time(),
            "pid": os.getpid(),
        }

        try:
            async with aiofiles.open(ack_file, 'w') as f:
                await f.write(json.dumps(ack_data, indent=2))
            return True
        except Exception as e:
            self._logger.error(f"Failed to write drain ack for {component}: {e}")
            return False

    async def acknowledge_shutdown(self, component: str) -> bool:
        """
        Acknowledge shutdown signal (called by components).

        Args:
            component: Component name

        Returns:
            True if acknowledgment written
        """
        ack_file = self._signal_dir / f"{component}.shutdown_ack"
        ack_data = {
            "component": component,
            "shutdown_at": time.time(),
            "pid": os.getpid(),
        }

        try:
            async with aiofiles.open(ack_file, 'w') as f:
                await f.write(json.dumps(ack_data, indent=2))
            return True
        except Exception as e:
            self._logger.error(f"Failed to write shutdown ack for {component}: {e}")
            return False

    async def check_drain_signal(self) -> bool:
        """
        Check if drain signal is active (called by components).

        Returns:
            True if drain signal present
        """
        drain_file = self._signal_dir / "drain.signal"
        return drain_file.exists()

    async def check_shutdown_signal(self, component: str) -> bool:
        """
        Check if shutdown signal is active for component.

        Args:
            component: Component name

        Returns:
            True if shutdown signal present
        """
        shutdown_file = self._signal_dir / f"{component}.shutdown"
        return shutdown_file.exists()


# Singleton accessor for shutdown coordinator
_shutdown_coordinator: Optional[GracefulShutdownCoordinator] = None


async def get_shutdown_coordinator(
    heartbeat_manager: Optional[HeartbeatCacheManager] = None,
    process_validator: Optional[ProcessCookieValidator] = None,
) -> GracefulShutdownCoordinator:
    """
    Get singleton GracefulShutdownCoordinator instance.

    Args:
        heartbeat_manager: Optional heartbeat cache manager
        process_validator: Optional process cookie validator

    Returns:
        GracefulShutdownCoordinator instance
    """
    global _shutdown_coordinator
    if _shutdown_coordinator is None:
        hb = heartbeat_manager or await get_heartbeat_cache()
        pv = process_validator or await get_process_cookie()
        _shutdown_coordinator = GracefulShutdownCoordinator(
            heartbeat_manager=hb,
            process_validator=pv,
        )
    return _shutdown_coordinator


# Signal handlers for graceful shutdown
def setup_shutdown_signal_handlers(
    coordinator: GracefulShutdownCoordinator,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> None:
    """
    Set up signal handlers for graceful shutdown.

    Args:
        coordinator: Shutdown coordinator instance
        loop: Event loop (defaults to running loop)
    """
    import signal as sig

    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()

    def handle_signal(signum, frame):
        sig_name = sig.Signals(signum).name
        logging.getLogger("jarvis.shutdown").info(
            f"Received {sig_name}, initiating graceful shutdown..."
        )
        asyncio.run_coroutine_threadsafe(
            coordinator.initiate_shutdown(reason=f"signal:{sig_name}"),
            loop,
        )

    # Register handlers
    for s in (sig.SIGTERM, sig.SIGINT):
        try:
            sig.signal(s, handle_signal)
        except Exception as e:
            logging.getLogger("jarvis.shutdown").warning(
                f"Could not register handler for {s}: {e}"
            )


# =============================================================================
# GUARANTEED EVENT DELIVERY SYSTEM
# =============================================================================


@dataclass
class PendingEvent:
    """A pending event awaiting acknowledgment."""
    event_id: str
    event_data: Dict[str, Any]
    target_component: str
    created_at: float
    retry_count: int = 0
    last_attempt: Optional[float] = None
    next_retry: Optional[float] = None
    ack_timeout: float = 30.0


class GuaranteedEventDelivery:
    """
    Guaranteed event delivery with acknowledgment and retry.

    Features:
    - Acknowledgment-based delivery
    - Automatic retry with exponential backoff
    - Persistent event queue (SQLite-backed)
    - At-least-once delivery guarantee
    - Configurable via environment variables

    Environment Variables:
        GED_MAX_RETRIES: Maximum retry attempts (default: 5)
        GED_RETRY_BACKOFF: Base backoff time in seconds (default: 1.0)
        GED_MAX_BACKOFF: Maximum backoff time (default: 60.0)
        GED_ACK_TIMEOUT: Default ACK timeout (default: 30.0)
        GED_CLEANUP_INTERVAL: Cleanup interval for old events (default: 300.0)
    """

    def __init__(
        self,
        store_path: Optional[Path] = None,
        send_callback: Optional[Callable[[str, Dict[str, Any]], Awaitable[bool]]] = None,
    ):
        """
        Initialize guaranteed event delivery.

        Args:
            store_path: Path to SQLite store
            send_callback: Async function to actually send events
        """
        env_dir = os.getenv("GED_STORE_DIR")
        if store_path:
            self._store_path = store_path
        elif env_dir:
            self._store_path = Path(env_dir) / "events.db"
        else:
            jarvis_base = os.getenv("JARVIS_BASE_DIR", os.path.expanduser("~/.jarvis"))
            self._store_path = Path(jarvis_base) / "trinity" / "events.db"

        self._store_path.parent.mkdir(parents=True, exist_ok=True)

        # Configuration from environment
        self._max_retries = _env_int("GED_MAX_RETRIES", 5)
        self._retry_backoff = _env_float("GED_RETRY_BACKOFF", 1.0)
        self._max_backoff = _env_float("GED_MAX_BACKOFF", 60.0)
        self._default_ack_timeout = _env_float("GED_ACK_TIMEOUT", 30.0)
        self._cleanup_interval = _env_float("GED_CLEANUP_INTERVAL", 300.0)

        # Send callback
        self._send_callback = send_callback

        # In-memory tracking
        self._pending_events: Dict[str, PendingEvent] = {}
        self._ack_futures: Dict[str, asyncio.Future] = {}
        self._retry_tasks: Dict[str, asyncio.Task] = {}

        # Database
        self._db_conn: Optional[sqlite3.Connection] = None
        self._db_lock = asyncio.Lock()

        # Background tasks
        self._cleanup_task: Optional[asyncio.Task] = None
        self._retry_processor_task: Optional[asyncio.Task] = None
        self._shutdown = False

        self._logger = logging.getLogger("jarvis.ged")

    async def initialize(self) -> None:
        """Initialize the event delivery system."""
        async with self._db_lock:
            self._db_conn = sqlite3.connect(
                str(self._store_path),
                check_same_thread=False,
                timeout=30.0,
            )
            # Enable WAL mode for better concurrency
            self._db_conn.execute("PRAGMA journal_mode=WAL")
            self._db_conn.execute("PRAGMA busy_timeout=30000")
            self._db_conn.execute("PRAGMA synchronous=NORMAL")

            # Create tables
            self._db_conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_events (
                    event_id TEXT PRIMARY KEY,
                    event_data TEXT NOT NULL,
                    target_component TEXT NOT NULL,
                    retry_count INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    last_attempt REAL,
                    next_retry REAL,
                    ack_timeout REAL DEFAULT 30.0
                )
            """)

            self._db_conn.execute("""
                CREATE TABLE IF NOT EXISTS delivered_events (
                    event_id TEXT PRIMARY KEY,
                    target_component TEXT NOT NULL,
                    delivered_at REAL NOT NULL,
                    retry_count INTEGER DEFAULT 0
                )
            """)

            self._db_conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pending_next_retry
                ON pending_events(next_retry)
            """)

            self._db_conn.commit()

        # Load pending events
        await self._load_pending_events()

        # Start background tasks
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._retry_processor_task = asyncio.create_task(self._retry_processor_loop())

        self._logger.info(
            f"[GED] Initialized with {len(self._pending_events)} pending events"
        )

    async def shutdown(self) -> None:
        """Shutdown the event delivery system."""
        self._shutdown = True

        # Cancel background tasks
        for task in [self._cleanup_task, self._retry_processor_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Cancel retry tasks
        for task in self._retry_tasks.values():
            task.cancel()

        # Close database
        if self._db_conn:
            self._db_conn.close()

        self._logger.info("[GED] Shutdown complete")

    def set_send_callback(
        self,
        callback: Callable[[str, Dict[str, Any]], Awaitable[bool]],
    ) -> None:
        """Set the callback used to send events."""
        self._send_callback = callback

    async def send_with_ack(
        self,
        event_id: str,
        event_data: Dict[str, Any],
        target_component: str,
        ack_timeout: Optional[float] = None,
    ) -> bool:
        """
        Send event with acknowledgment guarantee.

        Args:
            event_id: Unique event ID
            event_data: Event payload
            target_component: Target component ID
            ack_timeout: Acknowledgment timeout

        Returns:
            True if acknowledged within timeout, False if will retry
        """
        timeout = ack_timeout or self._default_ack_timeout

        # Create pending event
        pending = PendingEvent(
            event_id=event_id,
            event_data=event_data,
            target_component=target_component,
            created_at=time.time(),
            ack_timeout=timeout,
        )

        # Store in database
        await self._store_pending_event(pending)

        # Track in memory
        self._pending_events[event_id] = pending

        # Send and wait for ACK
        ack_received = await self._send_and_wait_ack(pending)

        if ack_received:
            await self._mark_delivered(event_id, target_component)
            return True
        else:
            # Will be retried by background task
            return False

    async def _send_and_wait_ack(self, pending: PendingEvent) -> bool:
        """Send event and wait for acknowledgment."""
        if not self._send_callback:
            self._logger.warning("[GED] No send callback configured")
            return False

        event_id = pending.event_id

        # Create ACK future
        ack_future: asyncio.Future = asyncio.Future()
        self._ack_futures[event_id] = ack_future

        try:
            # Update last attempt
            pending.last_attempt = time.time()

            # Send event
            send_success = await self._send_callback(
                pending.target_component,
                pending.event_data,
            )

            if not send_success:
                self._logger.warning(f"[GED] Send failed for {event_id}")
                return False

            # Wait for ACK
            try:
                result = await asyncio.wait_for(
                    ack_future,
                    timeout=pending.ack_timeout,
                )
                return result
            except asyncio.TimeoutError:
                self._logger.warning(f"[GED] ACK timeout for {event_id}")
                return False

        except Exception as e:
            self._logger.error(f"[GED] Error sending {event_id}: {e}")
            return False
        finally:
            self._ack_futures.pop(event_id, None)

    async def acknowledge(self, event_id: str) -> bool:
        """
        Acknowledge receipt of event.

        Called by the recipient to confirm delivery.

        Args:
            event_id: Event ID to acknowledge

        Returns:
            True if event was pending
        """
        if event_id in self._ack_futures:
            future = self._ack_futures[event_id]
            if not future.done():
                future.set_result(True)
            return True

        # Event might have already been processed
        return event_id in self._pending_events

    async def _store_pending_event(self, pending: PendingEvent) -> None:
        """Store pending event in database."""
        async with self._db_lock:
            self._db_conn.execute(
                """
                INSERT OR REPLACE INTO pending_events
                (event_id, event_data, target_component, retry_count,
                 created_at, last_attempt, next_retry, ack_timeout)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending.event_id,
                    json.dumps(pending.event_data),
                    pending.target_component,
                    pending.retry_count,
                    pending.created_at,
                    pending.last_attempt,
                    pending.next_retry or time.time(),
                    pending.ack_timeout,
                ),
            )
            self._db_conn.commit()

    async def _mark_delivered(self, event_id: str, target: str) -> None:
        """Mark event as delivered."""
        pending = self._pending_events.pop(event_id, None)

        # Cancel any retry task
        if event_id in self._retry_tasks:
            self._retry_tasks[event_id].cancel()
            del self._retry_tasks[event_id]

        async with self._db_lock:
            # Remove from pending
            self._db_conn.execute(
                "DELETE FROM pending_events WHERE event_id = ?",
                (event_id,),
            )

            # Record delivery
            self._db_conn.execute(
                """
                INSERT INTO delivered_events
                (event_id, target_component, delivered_at, retry_count)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event_id,
                    target,
                    time.time(),
                    pending.retry_count if pending else 0,
                ),
            )

            self._db_conn.commit()

        self._logger.debug(f"[GED] Delivered: {event_id}")

    async def _load_pending_events(self) -> None:
        """Load pending events from database."""
        async with self._db_lock:
            cursor = self._db_conn.execute(
                """
                SELECT event_id, event_data, target_component, retry_count,
                       created_at, last_attempt, next_retry, ack_timeout
                FROM pending_events
                """
            )

            for row in cursor.fetchall():
                try:
                    event_data = json.loads(row[1])
                    pending = PendingEvent(
                        event_id=row[0],
                        event_data=event_data,
                        target_component=row[2],
                        retry_count=row[3],
                        created_at=row[4],
                        last_attempt=row[5],
                        next_retry=row[6],
                        ack_timeout=row[7],
                    )
                    self._pending_events[pending.event_id] = pending
                except Exception as e:
                    self._logger.error(f"[GED] Error loading event: {e}")

    async def _retry_processor_loop(self) -> None:
        """Process pending events that need retry."""
        while not self._shutdown:
            try:
                now = time.time()

                for event_id, pending in list(self._pending_events.items()):
                    # Skip if already being retried
                    if event_id in self._retry_tasks:
                        continue

                    # Check if retry is due
                    if pending.next_retry and pending.next_retry <= now:
                        if pending.retry_count >= self._max_retries:
                            self._logger.error(
                                f"[GED] Event {event_id} failed after "
                                f"{pending.retry_count} retries"
                            )
                            # Remove from pending
                            await self._remove_failed_event(event_id)
                        else:
                            # Schedule retry
                            self._retry_tasks[event_id] = asyncio.create_task(
                                self._retry_event(pending)
                            )

                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"[GED] Retry processor error: {e}")
                await asyncio.sleep(1.0)

    async def _retry_event(self, pending: PendingEvent) -> None:
        """Retry sending an event."""
        event_id = pending.event_id

        try:
            pending.retry_count += 1

            # Calculate next retry with exponential backoff
            backoff = min(
                self._retry_backoff * (2 ** pending.retry_count),
                self._max_backoff,
            )
            # Add jitter
            backoff *= (0.5 + random.random())
            pending.next_retry = time.time() + backoff

            # Update database
            await self._store_pending_event(pending)

            self._logger.info(
                f"[GED] Retrying {event_id} (attempt {pending.retry_count})"
            )

            # Send and wait for ACK
            ack_received = await self._send_and_wait_ack(pending)

            if ack_received:
                await self._mark_delivered(event_id, pending.target_component)

        except Exception as e:
            self._logger.error(f"[GED] Retry error for {event_id}: {e}")
        finally:
            self._retry_tasks.pop(event_id, None)

    async def _remove_failed_event(self, event_id: str) -> None:
        """Remove a permanently failed event."""
        self._pending_events.pop(event_id, None)

        if event_id in self._retry_tasks:
            self._retry_tasks[event_id].cancel()
            del self._retry_tasks[event_id]

        async with self._db_lock:
            self._db_conn.execute(
                "DELETE FROM pending_events WHERE event_id = ?",
                (event_id,),
            )
            self._db_conn.commit()

    async def _cleanup_loop(self) -> None:
        """Periodic cleanup of old events."""
        while not self._shutdown:
            try:
                await asyncio.sleep(self._cleanup_interval)

                # Clean up old delivered events (keep 24 hours)
                cutoff = time.time() - 86400
                async with self._db_lock:
                    self._db_conn.execute(
                        "DELETE FROM delivered_events WHERE delivered_at < ?",
                        (cutoff,),
                    )
                    self._db_conn.commit()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"[GED] Cleanup error: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get delivery statistics."""
        return {
            "pending_events": len(self._pending_events),
            "active_retries": len(self._retry_tasks),
            "max_retries": self._max_retries,
            "retry_backoff": self._retry_backoff,
        }


# Singleton
_event_delivery: Optional[GuaranteedEventDelivery] = None


async def get_event_delivery() -> GuaranteedEventDelivery:
    """Get singleton GuaranteedEventDelivery instance."""
    global _event_delivery
    if _event_delivery is None:
        _event_delivery = GuaranteedEventDelivery()
        await _event_delivery.initialize()
    return _event_delivery


# =============================================================================
# OOM PROTECTION
# =============================================================================


class MemoryPressureLevel(Enum):
    """Memory pressure levels."""
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


@dataclass
class MemoryStats:
    """Memory statistics."""
    rss_mb: float
    vms_mb: float
    percent: float
    limit_mb: float
    pressure_level: MemoryPressureLevel
    timestamp: float = field(default_factory=time.time)


class OOMProtector:
    """
    Out-of-memory protection with real-time monitoring.

    Features:
    - Real-time memory monitoring
    - Configurable pressure thresholds
    - Automatic eviction callbacks
    - Pre-emptive memory checks
    - Graceful degradation

    Environment Variables:
        OOM_MEMORY_LIMIT_MB: Memory limit in MB (default: 4096)
        OOM_WARNING_THRESHOLD: Warning level (default: 0.7)
        OOM_CRITICAL_THRESHOLD: Critical level (default: 0.85)
        OOM_EMERGENCY_THRESHOLD: Emergency level (default: 0.95)
        OOM_CHECK_INTERVAL: Check interval in seconds (default: 1.0)
    """

    def __init__(
        self,
        eviction_callback: Optional[Callable[[MemoryPressureLevel], Awaitable[int]]] = None,
    ):
        """
        Initialize OOM protector.

        Args:
            eviction_callback: Async function to evict resources, returns bytes freed
        """
        # Configuration
        self._memory_limit_mb = _env_float("OOM_MEMORY_LIMIT_MB", 4096.0)
        self._warning_threshold = _env_float("OOM_WARNING_THRESHOLD", 0.7)
        self._critical_threshold = _env_float("OOM_CRITICAL_THRESHOLD", 0.85)
        self._emergency_threshold = _env_float("OOM_EMERGENCY_THRESHOLD", 0.95)
        self._check_interval = _env_float("OOM_CHECK_INTERVAL", 1.0)

        # Callbacks
        self._eviction_callback = eviction_callback
        self._pressure_callbacks: List[Callable[[MemoryPressureLevel], Awaitable[None]]] = []

        # State
        self._current_level = MemoryPressureLevel.NORMAL
        self._last_stats: Optional[MemoryStats] = None
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None

        # History for trend analysis
        self._memory_history: Deque[float] = deque(maxlen=60)

        self._logger = logging.getLogger("jarvis.oom")

    async def start(self) -> None:
        """Start memory monitoring."""
        if self._running:
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._logger.info(
            f"[OOM] Started (limit={self._memory_limit_mb}MB, "
            f"warning={self._warning_threshold*100:.0f}%, "
            f"critical={self._critical_threshold*100:.0f}%)"
        )

    async def stop(self) -> None:
        """Stop memory monitoring."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self._logger.info("[OOM] Stopped")

    def register_pressure_callback(
        self,
        callback: Callable[[MemoryPressureLevel], Awaitable[None]],
    ) -> None:
        """Register callback for pressure level changes."""
        self._pressure_callbacks.append(callback)

    def set_eviction_callback(
        self,
        callback: Callable[[MemoryPressureLevel], Awaitable[int]],
    ) -> None:
        """Set the eviction callback."""
        self._eviction_callback = callback

    async def check_can_allocate(self, size_mb: float) -> bool:
        """
        Check if allocation of given size is safe.

        Args:
            size_mb: Size to allocate in MB

        Returns:
            True if safe to allocate
        """
        stats = await self.get_stats()
        projected = stats.rss_mb + size_mb
        projected_percent = projected / self._memory_limit_mb

        if projected_percent >= self._critical_threshold:
            self._logger.warning(
                f"[OOM] Cannot allocate {size_mb}MB: would exceed critical threshold"
            )
            return False

        return True

    async def get_stats(self) -> MemoryStats:
        """Get current memory statistics."""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()

            rss_mb = memory_info.rss / (1024 * 1024)
            vms_mb = memory_info.vms / (1024 * 1024)
            percent = rss_mb / self._memory_limit_mb

            if percent >= self._emergency_threshold:
                level = MemoryPressureLevel.EMERGENCY
            elif percent >= self._critical_threshold:
                level = MemoryPressureLevel.CRITICAL
            elif percent >= self._warning_threshold:
                level = MemoryPressureLevel.WARNING
            else:
                level = MemoryPressureLevel.NORMAL

            stats = MemoryStats(
                rss_mb=rss_mb,
                vms_mb=vms_mb,
                percent=percent,
                limit_mb=self._memory_limit_mb,
                pressure_level=level,
            )

            self._last_stats = stats
            return stats

        except Exception as e:
            self._logger.error(f"[OOM] Error getting stats: {e}")
            # Return last known stats or defaults
            if self._last_stats:
                return self._last_stats
            return MemoryStats(
                rss_mb=0,
                vms_mb=0,
                percent=0,
                limit_mb=self._memory_limit_mb,
                pressure_level=MemoryPressureLevel.NORMAL,
            )

    async def _monitor_loop(self) -> None:
        """Monitor memory usage continuously."""
        while self._running:
            try:
                stats = await self.get_stats()

                # Record history
                self._memory_history.append(stats.rss_mb)

                # Check for level change
                if stats.pressure_level != self._current_level:
                    old_level = self._current_level
                    self._current_level = stats.pressure_level

                    self._logger.info(
                        f"[OOM] Pressure level changed: {old_level.value} -> "
                        f"{stats.pressure_level.value} "
                        f"({stats.rss_mb:.0f}MB / {stats.limit_mb:.0f}MB)"
                    )

                    # Notify callbacks
                    for callback in self._pressure_callbacks:
                        try:
                            await callback(stats.pressure_level)
                        except Exception as e:
                            self._logger.error(f"[OOM] Callback error: {e}")

                # Take action based on level
                if stats.pressure_level == MemoryPressureLevel.EMERGENCY:
                    await self._handle_emergency()
                elif stats.pressure_level == MemoryPressureLevel.CRITICAL:
                    await self._handle_critical()
                elif stats.pressure_level == MemoryPressureLevel.WARNING:
                    await self._handle_warning()

                await asyncio.sleep(self._check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"[OOM] Monitor error: {e}")
                await asyncio.sleep(self._check_interval)

    async def _handle_emergency(self) -> None:
        """Handle emergency memory pressure."""
        self._logger.critical("[OOM] 🚨 EMERGENCY: Forcing garbage collection")

        # Force GC
        import gc
        gc.collect()

        # Evict if callback available
        if self._eviction_callback:
            try:
                freed = await self._eviction_callback(MemoryPressureLevel.EMERGENCY)
                self._logger.info(f"[OOM] Emergency eviction freed {freed / 1024 / 1024:.0f}MB")
            except Exception as e:
                self._logger.error(f"[OOM] Emergency eviction failed: {e}")

    async def _handle_critical(self) -> None:
        """Handle critical memory pressure."""
        self._logger.warning("[OOM] ⚠️ CRITICAL: Triggering eviction")

        if self._eviction_callback:
            try:
                freed = await self._eviction_callback(MemoryPressureLevel.CRITICAL)
                self._logger.info(f"[OOM] Critical eviction freed {freed / 1024 / 1024:.0f}MB")
            except Exception as e:
                self._logger.error(f"[OOM] Critical eviction failed: {e}")

    async def _handle_warning(self) -> None:
        """Handle warning memory pressure."""
        # Log trend
        if len(self._memory_history) >= 10:
            recent = list(self._memory_history)[-10:]
            trend = recent[-1] - recent[0]
            if trend > 0:
                self._logger.warning(
                    f"[OOM] Memory increasing: +{trend:.0f}MB in last 10 checks"
                )


# Singleton
_oom_protector: Optional[OOMProtector] = None


async def get_oom_protector() -> OOMProtector:
    """Get singleton OOMProtector instance."""
    global _oom_protector
    if _oom_protector is None:
        _oom_protector = OOMProtector()
        await _oom_protector.start()
    return _oom_protector


# =============================================================================
# DEADLOCK-SAFE LOCK
# =============================================================================


class DeadlockSafeLock:
    """
    Lock with timeout to prevent deadlocks.

    Features:
    - Configurable timeout
    - Deadlock detection
    - Lock ownership tracking
    - Debug logging

    Environment Variables:
        DSL_DEFAULT_TIMEOUT: Default lock timeout (default: 30.0)
        DSL_WARN_AFTER: Warn if held longer than this (default: 10.0)
    """

    def __init__(
        self,
        name: str = "unnamed",
        timeout: Optional[float] = None,
    ):
        """
        Initialize deadlock-safe lock.

        Args:
            name: Lock name for debugging
            timeout: Lock acquisition timeout
        """
        self._name = name
        self._timeout = timeout or _env_float("DSL_DEFAULT_TIMEOUT", 30.0)
        self._warn_after = _env_float("DSL_WARN_AFTER", 10.0)

        self._lock = asyncio.Lock()
        self._owner: Optional[str] = None
        self._acquired_at: Optional[float] = None
        self._acquisition_stack: Optional[str] = None

        self._logger = logging.getLogger("jarvis.lock")

    @asynccontextmanager
    async def acquire(
        self,
        timeout: Optional[float] = None,
        caller: Optional[str] = None,
    ):
        """
        Acquire lock with timeout.

        Args:
            timeout: Override timeout
            caller: Caller identifier for debugging

        Raises:
            asyncio.TimeoutError: If lock cannot be acquired within timeout
        """
        effective_timeout = timeout or self._timeout

        try:
            await asyncio.wait_for(
                self._lock.acquire(),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            # Log deadlock info
            self._logger.error(
                f"[LOCK] Deadlock detected on '{self._name}'! "
                f"Current owner: {self._owner}, "
                f"held for {time.time() - (self._acquired_at or 0):.1f}s"
            )
            if self._acquisition_stack:
                self._logger.error(f"[LOCK] Owner stack:\n{self._acquisition_stack}")
            raise

        # Track ownership
        self._owner = caller or "unknown"
        self._acquired_at = time.time()

        # Capture stack for debugging
        import traceback
        self._acquisition_stack = "".join(traceback.format_stack()[-5:-1])

        # Start warning task
        warn_task = asyncio.create_task(
            self._warn_if_held_too_long()
        )

        try:
            yield
        finally:
            # Release lock
            warn_task.cancel()
            self._owner = None
            self._acquired_at = None
            self._acquisition_stack = None
            self._lock.release()

    async def _warn_if_held_too_long(self) -> None:
        """Warn if lock is held too long."""
        try:
            await asyncio.sleep(self._warn_after)
            if self._owner:
                self._logger.warning(
                    f"[LOCK] '{self._name}' held for >{self._warn_after}s "
                    f"by {self._owner}"
                )
        except asyncio.CancelledError:
            pass

    @property
    def locked(self) -> bool:
        """Check if lock is held."""
        return self._lock.locked()

    @property
    def owner(self) -> Optional[str]:
        """Get current lock owner."""
        return self._owner


# =============================================================================
# NETWORK PARTITION DETECTOR
# =============================================================================


@dataclass
class ComponentStatus:
    """Status of a network component."""
    component_id: str
    last_heartbeat: float
    consecutive_failures: int = 0
    is_reachable: bool = True
    latency_ms: float = 0.0


class NetworkPartitionDetector:
    """
    Detects network partitions between Trinity components.

    Features:
    - Heartbeat-based detection
    - Configurable timeouts
    - Partition event callbacks
    - Auto-recovery detection

    Environment Variables:
        NPD_HEARTBEAT_INTERVAL: Heartbeat interval (default: 5.0)
        NPD_TIMEOUT: Component timeout (default: 15.0)
        NPD_FAILURE_THRESHOLD: Failures before partition (default: 3)
    """

    def __init__(
        self,
        component_id: str,
        heartbeat_callback: Optional[Callable[[str], Awaitable[float]]] = None,
    ):
        """
        Initialize partition detector.

        Args:
            component_id: This component's ID
            heartbeat_callback: Async function to ping component, returns latency_ms
        """
        self._component_id = component_id
        self._heartbeat_callback = heartbeat_callback

        # Configuration
        self._heartbeat_interval = _env_float("NPD_HEARTBEAT_INTERVAL", 5.0)
        self._timeout = _env_float("NPD_TIMEOUT", 15.0)
        self._failure_threshold = _env_int("NPD_FAILURE_THRESHOLD", 3)

        # State
        self._components: Dict[str, ComponentStatus] = {}
        self._partition_callbacks: List[Callable[[str, bool], Awaitable[None]]] = []
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None

        self._logger = logging.getLogger("jarvis.npd")

    async def start(self) -> None:
        """Start partition detection."""
        if self._running:
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._logger.info(f"[NPD] Started for {self._component_id}")

    async def stop(self) -> None:
        """Stop partition detection."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    def register_component(self, component_id: str) -> None:
        """Register a component to monitor."""
        if component_id not in self._components:
            self._components[component_id] = ComponentStatus(
                component_id=component_id,
                last_heartbeat=time.time(),
            )

    def register_partition_callback(
        self,
        callback: Callable[[str, bool], Awaitable[None]],
    ) -> None:
        """
        Register callback for partition events.

        Args:
            callback: Async function(component_id, is_partitioned)
        """
        self._partition_callbacks.append(callback)

    async def record_heartbeat(
        self,
        component_id: str,
        latency_ms: float = 0.0,
    ) -> None:
        """Record successful heartbeat from component."""
        if component_id not in self._components:
            self.register_component(component_id)

        status = self._components[component_id]
        was_partitioned = not status.is_reachable

        status.last_heartbeat = time.time()
        status.consecutive_failures = 0
        status.is_reachable = True
        status.latency_ms = latency_ms

        # Notify if recovered from partition
        if was_partitioned:
            self._logger.info(f"[NPD] Component {component_id} recovered")
            for callback in self._partition_callbacks:
                try:
                    await callback(component_id, False)
                except Exception as e:
                    self._logger.error(f"[NPD] Callback error: {e}")

    async def _monitor_loop(self) -> None:
        """Monitor components for partitions."""
        while self._running:
            try:
                now = time.time()

                for component_id, status in self._components.items():
                    # Check if heartbeat is stale
                    age = now - status.last_heartbeat

                    if age > self._timeout:
                        status.consecutive_failures += 1

                        if (
                            status.consecutive_failures >= self._failure_threshold
                            and status.is_reachable
                        ):
                            # Partition detected
                            status.is_reachable = False
                            self._logger.warning(
                                f"[NPD] PARTITION DETECTED: {component_id} "
                                f"(no heartbeat for {age:.1f}s)"
                            )

                            for callback in self._partition_callbacks:
                                try:
                                    await callback(component_id, True)
                                except Exception as e:
                                    self._logger.error(f"[NPD] Callback error: {e}")

                    # Try active heartbeat if callback available
                    if self._heartbeat_callback and age > self._heartbeat_interval:
                        try:
                            latency = await asyncio.wait_for(
                                self._heartbeat_callback(component_id),
                                timeout=self._timeout,
                            )
                            await self.record_heartbeat(component_id, latency)
                        except asyncio.TimeoutError:
                            status.consecutive_failures += 1
                        except Exception as e:
                            self._logger.debug(f"[NPD] Heartbeat failed: {e}")
                            status.consecutive_failures += 1

                await asyncio.sleep(self._heartbeat_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"[NPD] Monitor error: {e}")
                await asyncio.sleep(self._heartbeat_interval)

    def get_status(self, component_id: Optional[str] = None) -> Dict[str, Any]:
        """Get component status."""
        if component_id:
            status = self._components.get(component_id)
            if status:
                return {
                    "component_id": status.component_id,
                    "is_reachable": status.is_reachable,
                    "last_heartbeat": status.last_heartbeat,
                    "consecutive_failures": status.consecutive_failures,
                    "latency_ms": status.latency_ms,
                }
            return {}

        return {
            cid: {
                "is_reachable": s.is_reachable,
                "last_heartbeat": s.last_heartbeat,
                "failures": s.consecutive_failures,
            }
            for cid, s in self._components.items()
        }


# =============================================================================
# SQLITE RETRY WRAPPER
# =============================================================================


class SQLiteRetryWrapper:
    """
    SQLite connection wrapper with automatic retry on lock contention.

    Features:
    - Automatic retry with exponential backoff
    - WAL mode for better concurrency
    - Configurable timeouts
    - Connection pooling friendly

    Environment Variables:
        SQLITE_MAX_RETRIES: Maximum retries (default: 5)
        SQLITE_RETRY_DELAY: Base retry delay (default: 0.1)
        SQLITE_BUSY_TIMEOUT: SQLite busy timeout ms (default: 30000)
    """

    def __init__(
        self,
        db_path: Path,
        check_same_thread: bool = False,
    ):
        """
        Initialize SQLite wrapper.

        Args:
            db_path: Path to database file
            check_same_thread: SQLite check_same_thread setting
        """
        self._db_path = db_path
        self._check_same_thread = check_same_thread

        # Configuration
        self._max_retries = _env_int("SQLITE_MAX_RETRIES", 5)
        self._retry_delay = _env_float("SQLITE_RETRY_DELAY", 0.1)
        self._busy_timeout = _env_int("SQLITE_BUSY_TIMEOUT", 30000)

        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

        self._logger = logging.getLogger("jarvis.sqlite")

    async def connect(self) -> None:
        """Connect to database."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=self._check_same_thread,
            timeout=self._busy_timeout / 1000.0,
        )

        # Configure for concurrency
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={self._busy_timeout}")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-64000")  # 64MB cache

        self._logger.debug(f"[SQLITE] Connected to {self._db_path}")

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self):
        """Execute within a transaction with retry."""
        if not self._conn:
            await self.connect()

        async with self._lock:
            for attempt in range(self._max_retries + 1):
                try:
                    yield self._conn
                    self._conn.commit()
                    return

                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower() and attempt < self._max_retries:
                        self._conn.rollback()
                        delay = self._retry_delay * (2 ** attempt)
                        delay *= (0.5 + random.random())
                        self._logger.warning(
                            f"[SQLITE] Lock contention, retry {attempt + 1} "
                            f"in {delay:.2f}s"
                        )
                        await asyncio.sleep(delay)
                    else:
                        self._conn.rollback()
                        raise

                except Exception:
                    self._conn.rollback()
                    raise

    async def execute(
        self,
        sql: str,
        params: tuple = (),
    ) -> sqlite3.Cursor:
        """Execute SQL with retry."""
        if not self._conn:
            await self.connect()

        for attempt in range(self._max_retries + 1):
            try:
                async with self._lock:
                    return self._conn.execute(sql, params)

            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < self._max_retries:
                    delay = self._retry_delay * (2 ** attempt)
                    await asyncio.sleep(delay)
                else:
                    raise

    async def executemany(
        self,
        sql: str,
        params_list: List[tuple],
    ) -> None:
        """Execute many with retry."""
        if not self._conn:
            await self.connect()

        for attempt in range(self._max_retries + 1):
            try:
                async with self._lock:
                    self._conn.executemany(sql, params_list)
                    self._conn.commit()
                return

            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < self._max_retries:
                    delay = self._retry_delay * (2 ** attempt)
                    await asyncio.sleep(delay)
                else:
                    raise


# =============================================================================
# WEBSOCKET MESSAGE BUFFER
# =============================================================================


@dataclass
class BufferedMessage:
    """A buffered WebSocket message."""
    message: Dict[str, Any]
    timestamp: float
    target: Optional[str] = None
    priority: int = 0


class WebSocketMessageBuffer:
    """
    Buffer for WebSocket messages during disconnects.

    Features:
    - Priority-based buffering
    - TTL-based expiration
    - Size limits
    - Ordered delivery

    Environment Variables:
        WS_BUFFER_SIZE: Maximum buffer size (default: 1000)
        WS_BUFFER_TTL: Message TTL in seconds (default: 300)
        WS_BUFFER_PRIORITY_LEVELS: Number of priority levels (default: 3)
    """

    def __init__(self):
        """Initialize message buffer."""
        self._max_size = _env_int("WS_BUFFER_SIZE", 1000)
        self._ttl = _env_float("WS_BUFFER_TTL", 300.0)
        self._priority_levels = _env_int("WS_BUFFER_PRIORITY_LEVELS", 3)

        # Priority queues
        self._buffers: Dict[int, Deque[BufferedMessage]] = {
            i: deque() for i in range(self._priority_levels)
        }

        self._lock = asyncio.Lock()
        self._total_size = 0

        self._logger = logging.getLogger("jarvis.wsbuf")

    async def add(
        self,
        message: Dict[str, Any],
        target: Optional[str] = None,
        priority: int = 1,
    ) -> bool:
        """
        Add message to buffer.

        Args:
            message: Message to buffer
            target: Target component
            priority: Message priority (0=highest)

        Returns:
            True if buffered, False if buffer full
        """
        async with self._lock:
            # Check size limit
            if self._total_size >= self._max_size:
                # Try to evict expired or low-priority messages
                evicted = await self._evict_if_needed()
                if not evicted:
                    self._logger.warning("[WSBUF] Buffer full, dropping message")
                    return False

            priority = min(priority, self._priority_levels - 1)

            buffered = BufferedMessage(
                message=message,
                timestamp=time.time(),
                target=target,
                priority=priority,
            )

            self._buffers[priority].append(buffered)
            self._total_size += 1

            return True

    async def flush(
        self,
        target: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Flush buffered messages.

        Args:
            target: Only flush messages for this target

        Returns:
            List of messages in priority order
        """
        async with self._lock:
            now = time.time()
            messages = []

            # Process in priority order (0 = highest)
            for priority in range(self._priority_levels):
                buffer = self._buffers[priority]
                remaining = deque()

                while buffer:
                    msg = buffer.popleft()
                    self._total_size -= 1

                    # Check TTL
                    if now - msg.timestamp > self._ttl:
                        continue

                    # Check target filter
                    if target and msg.target and msg.target != target:
                        remaining.append(msg)
                        self._total_size += 1
                        continue

                    messages.append(msg.message)

                # Put back remaining
                self._buffers[priority] = remaining

            if messages:
                self._logger.debug(f"[WSBUF] Flushed {len(messages)} messages")

            return messages

    async def _evict_if_needed(self) -> bool:
        """Evict messages to make room."""
        now = time.time()
        evicted = False

        # First, evict expired messages
        for priority in range(self._priority_levels - 1, -1, -1):
            buffer = self._buffers[priority]
            while buffer and now - buffer[0].timestamp > self._ttl:
                buffer.popleft()
                self._total_size -= 1
                evicted = True

        # If still full, evict lowest priority
        if self._total_size >= self._max_size:
            for priority in range(self._priority_levels - 1, -1, -1):
                if self._buffers[priority]:
                    self._buffers[priority].popleft()
                    self._total_size -= 1
                    evicted = True
                    break

        return evicted

    @property
    def size(self) -> int:
        """Get current buffer size."""
        return self._total_size

    def get_stats(self) -> Dict[str, Any]:
        """Get buffer statistics."""
        return {
            "total_size": self._total_size,
            "max_size": self._max_size,
            "by_priority": {
                p: len(self._buffers[p])
                for p in range(self._priority_levels)
            },
        }
