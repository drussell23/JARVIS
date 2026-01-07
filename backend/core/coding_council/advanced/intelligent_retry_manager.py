"""
v78.0: Intelligent Retry Manager
=================================

Provides context-aware retry strategies with intelligent decision-making.

Features:
- Multiple retry strategies (exponential, linear, fibonacci, adaptive)
- Jitter for thundering herd prevention
- Error classification for retry eligibility
- Circuit breaker integration
- Retry budgets per operation
- Context-aware strategy selection
- Failure pattern recognition
- Automatic strategy adjustment

Architecture:
    Operation Fails → [Classify Error] → [Check Retryable?]
                              ↓                    ↓ No
                        [Check Circuit Breaker] → Fail Fast
                              ↓ Open
                        [Select Strategy] → [Calculate Delay]
                              ↓
                        [Apply Jitter] → [Check Budget] → Retry or Fail

    Error Classification:
    - Transient: Network timeout, rate limit, 5xx → RETRY
    - Permanent: 4xx, validation error, auth failure → DON'T RETRY
    - Unknown: Unexpected errors → RETRY with limit

Usage:
    from backend.core.coding_council.advanced.intelligent_retry_manager import (
        get_retry_manager,
        RetryStrategy,
        with_retry,
    )

    manager = await get_retry_manager()

    # Decorator style
    @with_retry(max_attempts=3, strategy=RetryStrategy.EXPONENTIAL)
    async def my_operation():
        ...

    # Context manager style
    async with manager.retry_context("api_call", max_attempts=5) as ctx:
        result = await some_operation()

Author: JARVIS v78.0
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Enums
# =============================================================================

class RetryStrategy(Enum):
    """Available retry strategies."""
    EXPONENTIAL = "exponential"       # 2^n * base_delay
    LINEAR = "linear"                 # n * base_delay
    FIBONACCI = "fibonacci"           # Fib(n) * base_delay
    CONSTANT = "constant"             # Fixed delay
    ADAPTIVE = "adaptive"             # Learns from history
    DECORRELATED_JITTER = "decorrelated_jitter"  # AWS-style jitter


class ErrorCategory(Enum):
    """Categories of errors for retry decision."""
    TRANSIENT = "transient"           # Temporary, should retry
    RATE_LIMITED = "rate_limited"     # Rate limit, retry with backoff
    TIMEOUT = "timeout"               # Timeout, retry likely to help
    NETWORK = "network"               # Network error, retry
    SERVER_ERROR = "server_error"     # 5xx, server overloaded
    CLIENT_ERROR = "client_error"     # 4xx, don't retry
    AUTH_ERROR = "auth_error"         # Auth failure, don't retry
    VALIDATION = "validation"         # Invalid input, don't retry
    PERMANENT = "permanent"           # Permanent failure, don't retry
    UNKNOWN = "unknown"               # Unknown, retry with caution


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing, reject requests
    HALF_OPEN = "half_open" # Testing if recovered


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    max_attempts: int = 3
    base_delay_ms: float = 1000
    max_delay_ms: float = 60000
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL
    jitter_factor: float = 0.2
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,)
    non_retryable_exceptions: Tuple[Type[Exception], ...] = ()
    retry_on_timeout: bool = True
    retry_on_network: bool = True
    budget_ms: Optional[float] = None  # Total time budget


@dataclass
class RetryAttempt:
    """Record of a single retry attempt."""
    attempt_number: int
    started_at: float
    duration_ms: float
    success: bool
    error: Optional[str] = None
    error_category: Optional[ErrorCategory] = None
    delay_before_ms: float = 0


@dataclass
class RetryResult:
    """Result of a retried operation."""
    success: bool
    result: Any = None
    total_attempts: int = 0
    total_duration_ms: float = 0
    attempts: List[RetryAttempt] = field(default_factory=list)
    final_error: Optional[str] = None
    final_error_category: Optional[ErrorCategory] = None


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    failure_threshold: int = 5      # Failures before opening
    success_threshold: int = 3      # Successes before closing
    timeout_seconds: float = 30.0   # Time before half-open
    half_open_max_calls: int = 3    # Max calls in half-open state


@dataclass
class CircuitBreaker:
    """Circuit breaker for an operation."""
    name: str
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0
    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    half_open_calls: int = 0

    def record_success(self):
        """Record a successful call."""
        self.failure_count = 0

        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.config.success_threshold:
                self.state = CircuitState.CLOSED
                self.success_count = 0
                self.half_open_calls = 0

    def record_failure(self):
        """Record a failed call."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        self.success_count = 0

        if self.state == CircuitState.CLOSED:
            if self.failure_count >= self.config.failure_threshold:
                self.state = CircuitState.OPEN

        elif self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN

    def can_execute(self) -> bool:
        """Check if execution is allowed."""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if timeout has passed
            if time.time() - self.last_failure_time >= self.config.timeout_seconds:
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                return True
            return False

        if self.state == CircuitState.HALF_OPEN:
            if self.half_open_calls < self.config.half_open_max_calls:
                self.half_open_calls += 1
                return True
            return False

        return False


@dataclass
class RetryStats:
    """Statistics for retry operations."""
    total_operations: int = 0
    successful_first_try: int = 0
    successful_after_retry: int = 0
    failed_after_retries: int = 0
    total_retry_attempts: int = 0
    avg_attempts_per_operation: float = 0.0
    by_error_category: Dict[str, int] = field(default_factory=dict)
    by_strategy: Dict[str, int] = field(default_factory=dict)


# =============================================================================
# Error Classifier
# =============================================================================

class ErrorClassifier:
    """Classifies errors for retry decision-making."""

    # Known transient error patterns
    TRANSIENT_PATTERNS = [
        "timeout",
        "timed out",
        "connection reset",
        "connection refused",
        "temporarily unavailable",
        "service unavailable",
        "try again",
        "rate limit",
        "too many requests",
        "overloaded",
        "busy",
        "temporary failure",
        "transient",        # v78.0: Added for semantic error messages
        "retry",            # v78.0: Common indicator of retryable errors
        "intermittent",     # v78.0: Intermittent failures are retryable
    ]

    # Known permanent error patterns
    PERMANENT_PATTERNS = [
        "not found",
        "invalid",
        "unauthorized",
        "forbidden",
        "bad request",
        "validation error",
        "missing required",
        "permission denied",
        "not allowed",
    ]

    @classmethod
    def classify(
        cls,
        error: Exception,
        http_status: Optional[int] = None,
    ) -> ErrorCategory:
        """
        Classify an error into a category.

        Args:
            error: The exception to classify
            http_status: Optional HTTP status code

        Returns:
            ErrorCategory indicating retry eligibility
        """
        error_str = str(error).lower()
        error_type = type(error).__name__

        # Classify by HTTP status
        if http_status:
            if http_status == 429:
                return ErrorCategory.RATE_LIMITED
            if 500 <= http_status < 600:
                return ErrorCategory.SERVER_ERROR
            if http_status == 401 or http_status == 403:
                return ErrorCategory.AUTH_ERROR
            if 400 <= http_status < 500:
                return ErrorCategory.CLIENT_ERROR

        # v78.0: Check message patterns FIRST (allows semantic overrides)
        # This lets "transient error" in message override exception type classification
        for pattern in cls.TRANSIENT_PATTERNS:
            if pattern in error_str:
                return ErrorCategory.TRANSIENT

        for pattern in cls.PERMANENT_PATTERNS:
            if pattern in error_str:
                return ErrorCategory.PERMANENT

        # Classify by exception type
        if isinstance(error, asyncio.TimeoutError):
            return ErrorCategory.TIMEOUT
        if isinstance(error, (ConnectionError, ConnectionRefusedError)):
            return ErrorCategory.NETWORK
        if isinstance(error, (ValueError, TypeError, KeyError)):
            return ErrorCategory.VALIDATION

        # Default to unknown (will retry with caution)
        return ErrorCategory.UNKNOWN

    @classmethod
    def is_retryable(cls, category: ErrorCategory, config: RetryConfig) -> bool:
        """Check if an error category should be retried."""
        if category == ErrorCategory.TIMEOUT:
            return config.retry_on_timeout
        if category == ErrorCategory.NETWORK:
            return config.retry_on_network

        retryable = {
            ErrorCategory.TRANSIENT,
            ErrorCategory.RATE_LIMITED,
            ErrorCategory.SERVER_ERROR,
            ErrorCategory.UNKNOWN,
        }

        return category in retryable


# =============================================================================
# Delay Calculators
# =============================================================================

class DelayCalculator:
    """Calculates retry delays based on strategy."""

    # Fibonacci cache
    _fib_cache = [1, 1]

    @classmethod
    def calculate(
        cls,
        attempt: int,
        strategy: RetryStrategy,
        base_delay_ms: float,
        max_delay_ms: float,
        jitter_factor: float = 0.2,
        prev_delay_ms: float = 0,
    ) -> float:
        """
        Calculate delay before next retry.

        Args:
            attempt: Attempt number (0-indexed)
            strategy: Retry strategy
            base_delay_ms: Base delay
            max_delay_ms: Maximum delay
            jitter_factor: Random jitter factor (0-1)
            prev_delay_ms: Previous delay (for some strategies)

        Returns:
            Delay in milliseconds
        """
        if strategy == RetryStrategy.CONSTANT:
            delay = base_delay_ms

        elif strategy == RetryStrategy.LINEAR:
            delay = base_delay_ms * (attempt + 1)

        elif strategy == RetryStrategy.EXPONENTIAL:
            delay = base_delay_ms * (2 ** attempt)

        elif strategy == RetryStrategy.FIBONACCI:
            delay = base_delay_ms * cls._fibonacci(attempt)

        elif strategy == RetryStrategy.DECORRELATED_JITTER:
            # AWS-style: sleep = min(cap, random(base, prev * 3))
            if prev_delay_ms == 0:
                prev_delay_ms = base_delay_ms
            delay = random.uniform(base_delay_ms, prev_delay_ms * 3)

        elif strategy == RetryStrategy.ADAPTIVE:
            # Start aggressive, slow down
            if attempt < 2:
                delay = base_delay_ms * (1.5 ** attempt)
            else:
                delay = base_delay_ms * (2 ** (attempt - 1))

        else:
            delay = base_delay_ms

        # Apply jitter (except for decorrelated which has built-in)
        if strategy != RetryStrategy.DECORRELATED_JITTER and jitter_factor > 0:
            jitter = delay * jitter_factor * (random.random() * 2 - 1)
            delay += jitter

        # Clamp to max
        return min(delay, max_delay_ms)

    @classmethod
    def _fibonacci(cls, n: int) -> int:
        """Get nth Fibonacci number."""
        while len(cls._fib_cache) <= n:
            cls._fib_cache.append(
                cls._fib_cache[-1] + cls._fib_cache[-2]
            )
        return cls._fib_cache[n]


# =============================================================================
# Intelligent Retry Manager
# =============================================================================

class IntelligentRetryManager:
    """
    Intelligent retry management with context-aware decisions.

    Provides:
    - Multiple retry strategies
    - Error classification
    - Circuit breaker integration
    - Adaptive learning
    - Budget tracking

    Thread-safe and async-compatible.
    """

    def __init__(
        self,
        logger_instance: Optional[logging.Logger] = None,
        default_config: Optional[RetryConfig] = None,
    ):
        self.log = logger_instance or logger
        self.default_config = default_config or RetryConfig()

        # Circuit breakers per operation
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}

        # Stats
        self._stats = RetryStats()

        # Operation-specific configs
        self._configs: Dict[str, RetryConfig] = {}

        # Strategy effectiveness tracking
        self._strategy_success: Dict[RetryStrategy, List[bool]] = {
            s: [] for s in RetryStrategy
        }

        # Lock
        self._lock = asyncio.Lock()

        # Persistence
        self._persist_file = Path.home() / ".jarvis" / "trinity" / "retry_stats.json"
        self._persist_file.parent.mkdir(parents=True, exist_ok=True)

    def get_circuit_breaker(
        self,
        operation: str,
        config: Optional[CircuitBreakerConfig] = None,
    ) -> CircuitBreaker:
        """Get or create circuit breaker for an operation."""
        if operation not in self._circuit_breakers:
            self._circuit_breakers[operation] = CircuitBreaker(
                name=operation,
                config=config or CircuitBreakerConfig(),
            )
        return self._circuit_breakers[operation]

    def get_config(self, operation: str) -> RetryConfig:
        """Get retry config for an operation."""
        return self._configs.get(operation, self.default_config)

    def set_config(self, operation: str, config: RetryConfig):
        """Set retry config for an operation."""
        self._configs[operation] = config

    async def execute_with_retry(
        self,
        func: Callable[..., Awaitable[T]],
        *args,
        operation: str = "default",
        config: Optional[RetryConfig] = None,
        **kwargs,
    ) -> RetryResult:
        """
        Execute a function with intelligent retry.

        Args:
            func: Async function to execute
            *args: Arguments to pass to function
            operation: Operation name for circuit breaker
            config: Optional retry config
            **kwargs: Keyword arguments to pass to function

        Returns:
            RetryResult with outcome and statistics
        """
        config = config or self.get_config(operation)
        circuit = self.get_circuit_breaker(operation)

        result = RetryResult(success=False)
        start_time = time.time()
        prev_delay_ms = 0

        for attempt in range(config.max_attempts):
            # Check circuit breaker
            if not circuit.can_execute():
                result.final_error = "Circuit breaker open"
                result.final_error_category = ErrorCategory.PERMANENT
                break

            # Check budget
            if config.budget_ms:
                elapsed_ms = (time.time() - start_time) * 1000
                if elapsed_ms >= config.budget_ms:
                    result.final_error = "Retry budget exhausted"
                    break

            # Calculate delay (skip for first attempt)
            if attempt > 0:
                delay_ms = DelayCalculator.calculate(
                    attempt=attempt - 1,
                    strategy=config.strategy,
                    base_delay_ms=config.base_delay_ms,
                    max_delay_ms=config.max_delay_ms,
                    jitter_factor=config.jitter_factor,
                    prev_delay_ms=prev_delay_ms,
                )
                prev_delay_ms = delay_ms
                await asyncio.sleep(delay_ms / 1000)
            else:
                delay_ms = 0

            # Execute
            attempt_record = RetryAttempt(
                attempt_number=attempt,
                started_at=time.time(),
                duration_ms=0,
                success=False,
                delay_before_ms=delay_ms,
            )

            try:
                attempt_start = time.time()
                value = await func(*args, **kwargs)
                attempt_record.duration_ms = (time.time() - attempt_start) * 1000
                attempt_record.success = True
                result.attempts.append(attempt_record)

                # Success!
                result.success = True
                result.result = value
                result.total_attempts = attempt + 1

                circuit.record_success()
                self._record_strategy_result(config.strategy, True)

                async with self._lock:
                    self._stats.total_operations += 1
                    if attempt == 0:
                        self._stats.successful_first_try += 1
                    else:
                        self._stats.successful_after_retry += 1
                        self._stats.total_retry_attempts += attempt

                break

            except Exception as e:
                attempt_record.duration_ms = (time.time() - attempt_start) * 1000
                attempt_record.error = str(e)

                # Classify error
                category = ErrorClassifier.classify(e)
                attempt_record.error_category = category
                result.attempts.append(attempt_record)
                result.final_error = str(e)
                result.final_error_category = category

                circuit.record_failure()

                # Check if retryable
                if not ErrorClassifier.is_retryable(category, config):
                    self.log.debug(
                        f"[Retry] Non-retryable error ({category.value}): {e}"
                    )
                    break

                # Check if exception type is excluded
                if config.non_retryable_exceptions:
                    if isinstance(e, config.non_retryable_exceptions):
                        break

                # Log retry
                if attempt < config.max_attempts - 1:
                    self.log.debug(
                        f"[Retry] Attempt {attempt + 1}/{config.max_attempts} failed "
                        f"({category.value}): {e}. Retrying..."
                    )

        # Final stats
        result.total_duration_ms = (time.time() - start_time) * 1000
        result.total_attempts = len(result.attempts)

        if not result.success:
            self._record_strategy_result(config.strategy, False)
            async with self._lock:
                self._stats.total_operations += 1
                self._stats.failed_after_retries += 1
                self._stats.total_retry_attempts += result.total_attempts - 1

                if result.final_error_category:
                    cat = result.final_error_category.value
                    self._stats.by_error_category[cat] = (
                        self._stats.by_error_category.get(cat, 0) + 1
                    )

        return result

    def _record_strategy_result(self, strategy: RetryStrategy, success: bool):
        """Record strategy effectiveness."""
        history = self._strategy_success[strategy]
        history.append(success)
        # Keep last 100
        if len(history) > 100:
            history.pop(0)

    def get_best_strategy(self) -> RetryStrategy:
        """Get the most effective strategy based on history."""
        best_rate = 0.0
        best_strategy = RetryStrategy.EXPONENTIAL

        for strategy, history in self._strategy_success.items():
            if len(history) >= 10:
                success_rate = sum(history) / len(history)
                if success_rate > best_rate:
                    best_rate = success_rate
                    best_strategy = strategy

        return best_strategy

    @asynccontextmanager
    async def retry_context(
        self,
        operation: str = "default",
        config: Optional[RetryConfig] = None,
    ):
        """
        Context manager for retry operations.

        Usage:
            async with manager.retry_context("api_call") as ctx:
                result = await some_operation()
        """
        config = config or self.get_config(operation)
        circuit = self.get_circuit_breaker(operation)

        if not circuit.can_execute():
            raise RuntimeError(f"Circuit breaker open for {operation}")

        try:
            yield
            circuit.record_success()
        except Exception as e:
            circuit.record_failure()
            raise

    def get_stats(self) -> RetryStats:
        """Get retry statistics."""
        if self._stats.total_operations > 0:
            self._stats.avg_attempts_per_operation = (
                (self._stats.total_retry_attempts + self._stats.total_operations) /
                self._stats.total_operations
            )
        return self._stats

    async def persist(self):
        """Persist statistics to disk."""
        try:
            data = {
                "total_operations": self._stats.total_operations,
                "successful_first_try": self._stats.successful_first_try,
                "successful_after_retry": self._stats.successful_after_retry,
                "failed_after_retries": self._stats.failed_after_retries,
                "by_error_category": self._stats.by_error_category,
                "circuit_breakers": {
                    name: {
                        "state": cb.state.value,
                        "failure_count": cb.failure_count,
                    }
                    for name, cb in self._circuit_breakers.items()
                },
            }
            self._persist_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            self.log.debug(f"[Retry] Failed to persist: {e}")

    def visualize(self) -> str:
        """Generate visualization of retry state."""
        stats = self.get_stats()
        lines = [
            "[Intelligent Retry Manager]",
            f"  Total operations: {stats.total_operations}",
            f"  Successful first try: {stats.successful_first_try}",
            f"  Successful after retry: {stats.successful_after_retry}",
            f"  Failed after retries: {stats.failed_after_retries}",
            f"  Avg attempts/operation: {stats.avg_attempts_per_operation:.2f}",
            "",
            "  Circuit Breakers:",
        ]

        for name, cb in self._circuit_breakers.items():
            lines.append(f"    {name}: {cb.state.value} (failures={cb.failure_count})")

        lines.append("")
        lines.append("  Strategy Effectiveness:")
        for strategy, history in self._strategy_success.items():
            if history:
                rate = sum(history) / len(history)
                lines.append(f"    {strategy.value}: {rate:.1%} (n={len(history)})")

        return "\n".join(lines)


# =============================================================================
# Decorator
# =============================================================================

def with_retry(
    max_attempts: int = 3,
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL,
    base_delay_ms: float = 1000,
    max_delay_ms: float = 60000,
    operation: Optional[str] = None,
):
    """
    Decorator for retry with intelligent backoff.

    Usage:
        @with_retry(max_attempts=3, strategy=RetryStrategy.EXPONENTIAL)
        async def my_api_call():
            ...
    """
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            manager = await get_retry_manager()
            config = RetryConfig(
                max_attempts=max_attempts,
                strategy=strategy,
                base_delay_ms=base_delay_ms,
                max_delay_ms=max_delay_ms,
            )
            op_name = operation or func.__name__

            result = await manager.execute_with_retry(
                func, *args,
                operation=op_name,
                config=config,
                **kwargs,
            )

            if result.success:
                return result.result
            else:
                raise RuntimeError(
                    f"Operation {op_name} failed after {result.total_attempts} attempts: "
                    f"{result.final_error}"
                )

        return wrapper
    return decorator


# =============================================================================
# Singleton Instance
# =============================================================================

_retry_manager: Optional[IntelligentRetryManager] = None
_manager_lock: Optional[asyncio.Lock] = None  # v78.1: Lazy init for Python 3.9 compat


def _get_manager_lock() -> asyncio.Lock:
    """v78.1: Lazy lock initialization to avoid 'no running event loop' error on import."""
    global _manager_lock
    if _manager_lock is None:
        _manager_lock = asyncio.Lock()
    return _manager_lock


async def get_retry_manager() -> IntelligentRetryManager:
    """Get or create the singleton retry manager."""
    global _retry_manager

    async with _get_manager_lock():
        if _retry_manager is None:
            _retry_manager = IntelligentRetryManager()
        return _retry_manager


def get_retry_manager_sync() -> Optional[IntelligentRetryManager]:
    """Get the retry manager synchronously (may be None)."""
    return _retry_manager
