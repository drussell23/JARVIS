"""
v77.2: Resilience Module - Circuit Breaker & Error Handling
============================================================

Advanced fault tolerance patterns for robust operation.

Addresses:
    - Gap #66: Circuit breaker pattern
    - Gap #72: Error classification (transient vs permanent)
    - Gap #65: Async lock timeouts
    - Gap #71: Graceful shutdown

Features:
    - Circuit breaker with configurable thresholds
    - Automatic recovery with health checks
    - Error classification (transient/permanent)
    - Exponential backoff with jitter
    - Async lock with timeout
    - Graceful shutdown handling
    - Retry policies with limits

Author: JARVIS v77.2
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import logging
import random
import signal
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Type,
    TypeVar,
    Union,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ============================================================================
# Error Classification (Gap #72)
# ============================================================================


class ErrorCategory(Enum):
    """Classification of errors for retry decisions."""

    TRANSIENT = "transient"  # Temporary, worth retrying
    PERMANENT = "permanent"  # Won't recover, don't retry
    UNKNOWN = "unknown"  # Needs investigation


@dataclass
class ClassifiedError:
    """Error with classification metadata."""

    original_error: Exception
    category: ErrorCategory
    retry_after_ms: Optional[int] = None
    max_retries: int = 3
    message: str = ""

    def __post_init__(self):
        if not self.message:
            self.message = str(self.original_error)


class ErrorClassifier:
    """
    Classifies errors as transient or permanent.

    Uses error type, message patterns, and HTTP status codes.
    """

    # Transient error patterns (worth retrying)
    TRANSIENT_PATTERNS = {
        # Network errors
        "connection refused",
        "connection reset",
        "connection timed out",
        "timeout",
        "temporarily unavailable",
        "too many requests",
        "rate limit",
        "429",  # HTTP Too Many Requests
        "503",  # Service Unavailable
        "502",  # Bad Gateway
        "504",  # Gateway Timeout
        # Resource contention
        "lock",
        "busy",
        "try again",
        "retry",
        # Git specific
        "could not lock",
        "cannot lock ref",
        "remote end hung up",
    }

    # Permanent error patterns (don't retry)
    PERMANENT_PATTERNS = {
        # Authentication
        "authentication failed",
        "invalid credentials",
        "unauthorized",
        "forbidden",
        "401",
        "403",
        # Not found / Invalid
        "not found",
        "404",
        "invalid",
        "malformed",
        "syntax error",
        # Git specific
        "merge conflict",
        "not a git repository",
        "fatal:",
        "permission denied",
    }

    # Exception types that are transient
    TRANSIENT_EXCEPTIONS: Set[Type[Exception]] = {
        TimeoutError,
        asyncio.TimeoutError,
        ConnectionRefusedError,
        ConnectionResetError,
        ConnectionAbortedError,
        BrokenPipeError,
    }

    # Exception types that are permanent
    PERMANENT_EXCEPTIONS: Set[Type[Exception]] = {
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        SyntaxError,
        ImportError,
        PermissionError,
        FileNotFoundError,
    }

    @classmethod
    def classify(cls, error: Exception) -> ClassifiedError:
        """
        Classify an error.

        Args:
            error: The exception to classify

        Returns:
            ClassifiedError with category and retry advice
        """
        error_str = str(error).lower()
        error_type = type(error)

        # Check exception type first
        if error_type in cls.TRANSIENT_EXCEPTIONS:
            return ClassifiedError(
                original_error=error,
                category=ErrorCategory.TRANSIENT,
                max_retries=3,
                retry_after_ms=1000,
            )

        if error_type in cls.PERMANENT_EXCEPTIONS:
            return ClassifiedError(
                original_error=error,
                category=ErrorCategory.PERMANENT,
                max_retries=0,
            )

        # Check message patterns
        for pattern in cls.TRANSIENT_PATTERNS:
            if pattern in error_str:
                # Check for rate limiting with Retry-After
                retry_after = None
                if "retry-after" in error_str or "rate limit" in error_str:
                    retry_after = 5000  # Default 5 second backoff

                return ClassifiedError(
                    original_error=error,
                    category=ErrorCategory.TRANSIENT,
                    max_retries=3,
                    retry_after_ms=retry_after or 1000,
                )

        for pattern in cls.PERMANENT_PATTERNS:
            if pattern in error_str:
                return ClassifiedError(
                    original_error=error,
                    category=ErrorCategory.PERMANENT,
                    max_retries=0,
                )

        # Default: unknown, allow one retry
        return ClassifiedError(
            original_error=error,
            category=ErrorCategory.UNKNOWN,
            max_retries=1,
            retry_after_ms=2000,
        )


# ============================================================================
# Circuit Breaker (Gap #66)
# ============================================================================


class CircuitState(Enum):
    """States of a circuit breaker."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""

    failure_threshold: int = 5  # Failures before opening
    success_threshold: int = 2  # Successes to close from half-open
    timeout_seconds: float = 30.0  # Time before half-open
    half_open_max_calls: int = 3  # Allowed calls in half-open


@dataclass
class CircuitStats:
    """Statistics for a circuit breaker."""

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    total_failures: int = 0
    total_successes: int = 0
    last_failure_time: float = 0.0
    last_state_change: float = field(default_factory=time.time)
    half_open_calls: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "last_failure_time": self.last_failure_time,
        }


class CircuitBreaker:
    """
    Circuit breaker for fault tolerance.

    Prevents cascade failures by stopping requests to failing services.

    States:
        CLOSED: Normal operation, requests pass through
        OPEN: Service is failing, requests are rejected immediately
        HALF_OPEN: Testing if service recovered, limited requests allowed

    Usage:
        breaker = CircuitBreaker("git_service")

        async with breaker:
            result = await git_operation()

        # Or as decorator
        @breaker
        async def my_operation():
            ...
    """

    def __init__(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None,
        on_state_change: Optional[Callable[[str, CircuitState, CircuitState], None]] = None,
    ):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.on_state_change = on_state_change

        self._stats = CircuitStats()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._stats.state

    @property
    def is_closed(self) -> bool:
        return self._stats.state == CircuitState.CLOSED

    async def __aenter__(self):
        await self._before_call()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            await self._on_success()
        else:
            await self._on_failure(exc_val)
        return False  # Don't suppress exception

    def __call__(self, func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        """Decorator for circuit-protected functions."""

        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            async with self:
                return await func(*args, **kwargs)

        return wrapper

    async def call(
        self,
        func: Callable[..., Awaitable[T]],
        *args,
        **kwargs,
    ) -> T:
        """
        Execute function with circuit breaker protection.

        Args:
            func: Async function to call
            *args: Function arguments
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            CircuitOpenError: If circuit is open
            Exception: Original exception if function fails
        """
        async with self:
            return await func(*args, **kwargs)

    async def _before_call(self) -> None:
        """Check if call is allowed."""
        async with self._lock:
            state = self._stats.state

            if state == CircuitState.OPEN:
                # Check if timeout expired
                elapsed = time.time() - self._stats.last_state_change
                if elapsed >= self.config.timeout_seconds:
                    await self._transition_to(CircuitState.HALF_OPEN)
                else:
                    raise CircuitOpenError(
                        f"Circuit '{self.name}' is OPEN. "
                        f"Retry in {self.config.timeout_seconds - elapsed:.1f}s"
                    )

            elif state == CircuitState.HALF_OPEN:
                if self._stats.half_open_calls >= self.config.half_open_max_calls:
                    raise CircuitOpenError(
                        f"Circuit '{self.name}' is HALF_OPEN with max calls reached"
                    )
                self._stats.half_open_calls += 1

    async def _on_success(self) -> None:
        """Record successful call."""
        async with self._lock:
            self._stats.consecutive_successes += 1
            self._stats.consecutive_failures = 0
            self._stats.total_successes += 1

            if self._stats.state == CircuitState.HALF_OPEN:
                if self._stats.consecutive_successes >= self.config.success_threshold:
                    await self._transition_to(CircuitState.CLOSED)

    async def _on_failure(self, error: Exception) -> None:
        """Record failed call."""
        async with self._lock:
            # Classify the error
            classified = ErrorClassifier.classify(error)

            # Only permanent errors count toward circuit opening
            if classified.category == ErrorCategory.PERMANENT:
                return

            self._stats.consecutive_failures += 1
            self._stats.consecutive_successes = 0
            self._stats.total_failures += 1
            self._stats.last_failure_time = time.time()

            if self._stats.state == CircuitState.CLOSED:
                if self._stats.consecutive_failures >= self.config.failure_threshold:
                    await self._transition_to(CircuitState.OPEN)

            elif self._stats.state == CircuitState.HALF_OPEN:
                # Any failure in half-open reopens the circuit
                await self._transition_to(CircuitState.OPEN)

    async def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to new state."""
        old_state = self._stats.state
        if old_state == new_state:
            return

        self._stats.state = new_state
        self._stats.last_state_change = time.time()
        self._stats.half_open_calls = 0

        if new_state == CircuitState.CLOSED:
            self._stats.consecutive_failures = 0
            self._stats.consecutive_successes = 0

        logger.info(f"[CircuitBreaker:{self.name}] {old_state.value} -> {new_state.value}")

        if self.on_state_change:
            try:
                self.on_state_change(self.name, old_state, new_state)
            except Exception as e:
                logger.error(f"[CircuitBreaker] State change callback error: {e}")

    async def force_open(self) -> None:
        """Manually open the circuit."""
        async with self._lock:
            await self._transition_to(CircuitState.OPEN)

    async def force_close(self) -> None:
        """Manually close the circuit."""
        async with self._lock:
            await self._transition_to(CircuitState.CLOSED)

    async def reset(self) -> None:
        """Reset circuit to initial state."""
        async with self._lock:
            self._stats = CircuitStats()

    def get_stats(self) -> Dict[str, Any]:
        """Get circuit breaker statistics."""
        return {
            "name": self.name,
            **self._stats.to_dict(),
            "config": {
                "failure_threshold": self.config.failure_threshold,
                "success_threshold": self.config.success_threshold,
                "timeout_seconds": self.config.timeout_seconds,
            },
        }


class CircuitOpenError(Exception):
    """Raised when circuit is open and call is rejected."""

    pass


# ============================================================================
# Async Lock with Timeout (Gap #65)
# ============================================================================


class AsyncLockWithTimeout:
    """
    Async lock that supports timeout to prevent deadlocks.

    Usage:
        lock = AsyncLockWithTimeout(timeout=10.0)

        async with lock:
            # Critical section

        # Or with explicit timeout
        try:
            async with lock.acquire_with_timeout(5.0):
                # Critical section
        except asyncio.TimeoutError:
            # Handle timeout
    """

    def __init__(self, timeout: float = 30.0, name: str = ""):
        self.default_timeout = timeout
        self.name = name
        self._lock = asyncio.Lock()
        self._owner: Optional[str] = None
        self._acquired_at: float = 0.0

    async def __aenter__(self):
        await self.acquire(self.default_timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        Acquire lock with timeout.

        Args:
            timeout: Timeout in seconds (None = use default)

        Returns:
            True if acquired

        Raises:
            asyncio.TimeoutError: If timeout exceeded
        """
        timeout = timeout if timeout is not None else self.default_timeout

        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=timeout)
            self._acquired_at = time.time()
            return True
        except asyncio.TimeoutError:
            logger.warning(
                f"[AsyncLock:{self.name}] Timeout after {timeout}s "
                f"(held for {time.time() - self._acquired_at:.1f}s)"
            )
            raise

    def release(self) -> None:
        """Release the lock."""
        if self._lock.locked():
            self._lock.release()
            self._acquired_at = 0.0

    @property
    def locked(self) -> bool:
        return self._lock.locked()

    @property
    def held_duration(self) -> float:
        """How long the lock has been held."""
        if not self._acquired_at:
            return 0.0
        return time.time() - self._acquired_at


# ============================================================================
# Retry with Exponential Backoff
# ============================================================================


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_retries: int = 3
    initial_delay_ms: int = 100
    max_delay_ms: int = 10000
    exponential_base: float = 2.0
    jitter_factor: float = 0.1  # Random jitter to prevent thundering herd


class RetryPolicy:
    """
    Retry policy with exponential backoff and jitter.

    Features:
        - Exponential backoff
        - Random jitter to prevent thundering herd
        - Error classification for smart retries
        - Configurable limits
    """

    def __init__(self, config: Optional[RetryConfig] = None):
        self.config = config or RetryConfig()

    def calculate_delay(self, attempt: int) -> float:
        """
        Calculate delay before next retry.

        Args:
            attempt: Current attempt number (0-indexed)

        Returns:
            Delay in seconds
        """
        base_delay = self.config.initial_delay_ms * (
            self.config.exponential_base ** attempt
        )
        capped_delay = min(base_delay, self.config.max_delay_ms)

        # Add jitter
        jitter = random.uniform(
            -self.config.jitter_factor * capped_delay,
            self.config.jitter_factor * capped_delay,
        )

        return (capped_delay + jitter) / 1000.0  # Convert to seconds

    def should_retry(self, error: Exception, attempt: int) -> bool:
        """
        Check if we should retry after an error.

        Args:
            error: The exception that occurred
            attempt: Current attempt number

        Returns:
            True if should retry
        """
        if attempt >= self.config.max_retries:
            return False

        classified = ErrorClassifier.classify(error)

        if classified.category == ErrorCategory.PERMANENT:
            return False

        if classified.category == ErrorCategory.TRANSIENT:
            return attempt < classified.max_retries

        # Unknown errors get limited retries
        return attempt < 1

    async def execute(
        self,
        func: Callable[..., Awaitable[T]],
        *args,
        **kwargs,
    ) -> T:
        """
        Execute function with retry policy.

        Args:
            func: Async function to execute
            *args: Function arguments
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            Exception: Last exception if all retries exhausted
        """
        last_error: Optional[Exception] = None

        for attempt in range(self.config.max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                classified = ErrorClassifier.classify(e)

                if not self.should_retry(e, attempt):
                    logger.debug(
                        f"[Retry] Not retrying {classified.category.value} error: {e}"
                    )
                    raise

                delay = self.calculate_delay(attempt)
                logger.info(
                    f"[Retry] Attempt {attempt + 1}/{self.config.max_retries + 1} "
                    f"failed: {e}. Retrying in {delay:.2f}s"
                )
                await asyncio.sleep(delay)

        if last_error:
            raise last_error

        raise RuntimeError("Retry exhausted without result or error")


def with_retry(
    max_retries: int = 3,
    initial_delay_ms: int = 100,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """
    Decorator for adding retry behavior.

    Usage:
        @with_retry(max_retries=3)
        async def my_function():
            ...
    """
    config = RetryConfig(max_retries=max_retries, initial_delay_ms=initial_delay_ms)
    policy = RetryPolicy(config)

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            return await policy.execute(func, *args, **kwargs)

        return wrapper

    return decorator


# ============================================================================
# Graceful Shutdown (Gap #71)
# ============================================================================


class ShutdownHandler:
    """
    Handles graceful shutdown of the system.

    Features:
        - Signal handling (SIGTERM, SIGINT)
        - Cleanup callback registration
        - Timeout for cleanup operations
        - Ordered shutdown phases

    Usage:
        handler = ShutdownHandler()

        # Register cleanup
        handler.register_cleanup(close_database, priority=10)
        handler.register_cleanup(save_state, priority=5)

        # Start handling signals
        await handler.start()
    """

    def __init__(self, cleanup_timeout: float = 30.0):
        self.cleanup_timeout = cleanup_timeout
        self._cleanup_callbacks: List[tuple[int, Callable[[], Awaitable]]] = []
        self._shutdown_event = asyncio.Event()
        self._is_shutting_down = False
        self._started = False

    @property
    def is_shutting_down(self) -> bool:
        return self._is_shutting_down

    def register_cleanup(
        self,
        callback: Callable[[], Awaitable],
        priority: int = 0,
    ) -> None:
        """
        Register a cleanup callback.

        Higher priority = executed first during shutdown.

        Args:
            callback: Async cleanup function
            priority: Execution priority (higher = first)
        """
        self._cleanup_callbacks.append((priority, callback))
        self._cleanup_callbacks.sort(key=lambda x: -x[0])  # Sort descending

    async def start(self) -> None:
        """Start signal handling."""
        if self._started:
            return

        self._started = True

        # Register signal handlers
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(self._handle_signal(s)),
                )
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                signal.signal(sig, lambda s, f: asyncio.create_task(self._handle_signal(s)))

        # Also register atexit for non-signal exits
        # Guarded: KeyboardInterrupt/SystemExit are BaseExceptions, so an
        # `except Exception` inside the handler never catches them and a
        # Ctrl+C landing here prints a traceback over the goodbye. Local
        # import so this can never introduce a cycle.
        from backend.core.ouroboros.governance.exit_guard import (
            guarded_atexit_register,
        )
        guarded_atexit_register(self._sync_cleanup)
        logger.info("[ShutdownHandler] Started signal handling")

    async def _handle_signal(self, sig: signal.Signals) -> None:
        """Handle shutdown signal."""
        if self._is_shutting_down:
            logger.warning("[ShutdownHandler] Already shutting down, forcing exit")
            sys.exit(1)

        logger.info(f"[ShutdownHandler] Received signal {sig.name}, starting cleanup")
        self._is_shutting_down = True
        self._shutdown_event.set()

        await self._run_cleanup()

    async def _run_cleanup(self) -> None:
        """Run all cleanup callbacks."""
        for priority, callback in self._cleanup_callbacks:
            try:
                logger.debug(f"[ShutdownHandler] Running cleanup (priority={priority})")
                await asyncio.wait_for(callback(), timeout=self.cleanup_timeout / len(self._cleanup_callbacks) if self._cleanup_callbacks else self.cleanup_timeout)
            except asyncio.TimeoutError:
                logger.error(f"[ShutdownHandler] Cleanup timed out")
            except Exception as e:
                logger.error(f"[ShutdownHandler] Cleanup error: {e}")

        logger.info("[ShutdownHandler] Cleanup complete")

    def _sync_cleanup(self) -> None:
        """Synchronous cleanup for atexit."""
        if self._is_shutting_down:
            return

        self._is_shutting_down = True
        logger.info("[ShutdownHandler] Running atexit cleanup")

        # Try to run async cleanup in event loop
        try:
            loop = asyncio.get_running_loop()
            # Loop is running - schedule cleanup as a task
            loop.create_task(self._run_cleanup())
        except RuntimeError:
            # No running loop - try to create one and run cleanup
            try:
                asyncio.run(self._run_cleanup())
            except Exception as e:
                logger.error(f"[ShutdownHandler] Sync cleanup error: {e}")

    async def wait_for_shutdown(self) -> None:
        """Wait until shutdown is initiated."""
        await self._shutdown_event.wait()

    async def trigger_shutdown(self) -> None:
        """Programmatically trigger shutdown."""
        if not self._is_shutting_down:
            await self._handle_signal(signal.SIGTERM)


# ============================================================================
# Health Check
# ============================================================================


@dataclass
class HealthStatus:
    """Health status of a component."""

    name: str
    healthy: bool
    message: str = ""
    last_check: float = field(default_factory=time.time)
    details: Dict[str, Any] = field(default_factory=dict)


class HealthChecker:
    """
    Health checker for system components.

    Periodically checks component health and exposes status.
    """

    def __init__(self, check_interval: float = 30.0):
        self.check_interval = check_interval
        self._checks: Dict[str, Callable[[], Awaitable[HealthStatus]]] = {}
        self._status: Dict[str, HealthStatus] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def register(
        self,
        name: str,
        check: Callable[[], Awaitable[HealthStatus]],
    ) -> None:
        """Register a health check."""
        self._checks[name] = check

    async def start(self) -> None:
        """Start periodic health checks."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info(f"[HealthChecker] Started with {len(self._checks)} checks")

    async def stop(self) -> None:
        """Stop health checks."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _check_loop(self) -> None:
        """Periodic check loop."""
        while self._running:
            await self.check_all()
            await asyncio.sleep(self.check_interval)

    async def check_all(self) -> Dict[str, HealthStatus]:
        """Run all health checks."""
        for name, check in self._checks.items():
            try:
                status = await asyncio.wait_for(check(), timeout=10.0)
                self._status[name] = status
            except asyncio.TimeoutError:
                self._status[name] = HealthStatus(
                    name=name,
                    healthy=False,
                    message="Health check timed out",
                )
            except Exception as e:
                self._status[name] = HealthStatus(
                    name=name,
                    healthy=False,
                    message=f"Health check failed: {e}",
                )

        return self._status.copy()

    def get_status(self) -> Dict[str, Any]:
        """Get overall health status."""
        statuses = list(self._status.values())
        all_healthy = all(s.healthy for s in statuses) if statuses else True

        return {
            "healthy": all_healthy,
            "components": {s.name: s.healthy for s in statuses},
            "details": {
                s.name: {
                    "healthy": s.healthy,
                    "message": s.message,
                    "last_check": s.last_check,
                }
                for s in statuses
            },
        }


# ============================================================================
# Circuit Breaker Registry
# ============================================================================


class CircuitBreakerRegistry:
    """
    Registry for circuit breakers.

    Provides centralized management and monitoring.
    """

    _instance: Optional["CircuitBreakerRegistry"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._breakers = {}
        return cls._instance

    def get_or_create(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None,
    ) -> CircuitBreaker:
        """Get existing or create new circuit breaker."""
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name, config)
        return self._breakers[name]

    def get(self, name: str) -> Optional[CircuitBreaker]:
        """Get existing circuit breaker."""
        return self._breakers.get(name)

    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get stats for all circuit breakers."""
        return {name: cb.get_stats() for name, cb in self._breakers.items()}

    async def reset_all(self) -> None:
        """Reset all circuit breakers."""
        for cb in self._breakers.values():
            await cb.reset()


# Global instances
_shutdown_handler: Optional[ShutdownHandler] = None
_health_checker: Optional[HealthChecker] = None


def get_shutdown_handler() -> ShutdownHandler:
    """Get or create global shutdown handler."""
    global _shutdown_handler
    if _shutdown_handler is None:
        _shutdown_handler = ShutdownHandler()
    return _shutdown_handler


def get_health_checker() -> HealthChecker:
    """Get or create global health checker."""
    global _health_checker
    if _health_checker is None:
        _health_checker = HealthChecker()
    return _health_checker


def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    """Get the circuit breaker registry."""
    return CircuitBreakerRegistry()
