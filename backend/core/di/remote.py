"""
JARVIS Cross-Repo Coordination Module v1.0
===========================================

Enterprise-grade cross-repository coordination for the JARVIS Trinity:
- JARVIS Body (port 8010) - Main agent with voice, screen, actions
- JARVIS Prime (port 8000) - LLM inference and reasoning
- Reactor Core (port 8090) - Training, evolution, learning

Features:
- RemoteServiceProxy: Proxy for calling services in remote repos
- CircuitBreaker: Prevents cascade failures with state machine
- RetryPolicy: Exponential backoff with jitter
- ServiceRegistry: File-based service discovery with TTL
- CrossRepoCoordinator: Main coordinator for Trinity

Architecture:
    +------------------------------------------------------------------+
    |                   CROSS-REPO COORDINATION                         |
    +------------------------------------------------------------------+
    |                                                                    |
    |  +--------------+      +---------------+      +---------------+   |
    |  | JARVIS Body  |<---->| CrossRepoCoord|<---->| JARVIS Prime  |   |
    |  | Port: 8010   |      |     inator    |      | Port: 8000    |   |
    |  +--------------+      +-------+-------+      +---------------+   |
    |                               |                                    |
    |                        +------+------+                             |
    |                        | Reactor Core|                             |
    |                        | Port: 8090  |                             |
    |                        +-------------+                             |
    |                                                                    |
    |  Components:                                                       |
    |  - CircuitBreaker: Prevent cascade failures                        |
    |  - RetryPolicy: Exponential backoff with jitter                    |
    |  - ServiceRegistry: Service discovery with heartbeat               |
    |  - RemoteServiceProxy: HTTP client with resilience                 |
    +------------------------------------------------------------------+

Thread Safety:
    All components use lazy initialization for asyncio primitives.
    Never create asyncio.Lock() at module or __init__ level.

Author: JARVIS AI System
Version: 1.0.0 (January 2026)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    Final,
    Generic,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    TypeVar,
    Union,
)

# Import protocols from the DI system
from .protocols import (
    AsyncService,
    BaseAsyncService,
    DependencySpec,
    HealthReport,
    HealthStatus,
    ServiceState,
)

if TYPE_CHECKING:
    import aiohttp

logger = logging.getLogger(__name__)

# =============================================================================
# TYPE VARIABLES
# =============================================================================

T = TypeVar("T")
ResponseT = TypeVar("ResponseT")

# =============================================================================
# CONSTANTS
# =============================================================================

# Default configuration
DEFAULT_FAILURE_THRESHOLD: Final[int] = 5
DEFAULT_RECOVERY_TIMEOUT: Final[float] = 30.0
DEFAULT_HALF_OPEN_MAX_CALLS: Final[int] = 3
DEFAULT_MAX_RETRIES: Final[int] = 3
DEFAULT_BASE_DELAY: Final[float] = 1.0
DEFAULT_MAX_DELAY: Final[float] = 60.0
DEFAULT_EXPONENTIAL_BASE: Final[float] = 2.0
DEFAULT_JITTER: Final[float] = 0.1
DEFAULT_TTL: Final[float] = 60.0
DEFAULT_REQUEST_TIMEOUT: Final[float] = 30.0
DEFAULT_HEARTBEAT_INTERVAL: Final[float] = 15.0

# Trinity service configuration (from trinity_config)
TRINITY_SERVICES: Final[Dict[str, Dict[str, Any]]] = {
    "jarvis-body": {
        "default_host": "localhost",
        "default_port": 8010,
        "health_path": "/health",
        "env_host": "JARVIS_HOST",
        "env_port": "JARVIS_PORT",
    },
    "jarvis-prime": {
        "default_host": "localhost",
        "default_port": 8000,
        "health_path": "/health",
        "env_host": "JARVIS_PRIME_HOST",
        "env_port": "JARVIS_PRIME_PORT",
    },
    "reactor-core": {
        "default_host": "localhost",
        "default_port": 8090,
        "health_path": "/health",
        "env_host": "REACTOR_CORE_HOST",
        "env_port": "REACTOR_CORE_PORT",
    },
}


def _get_env_int(key: str, default: int) -> int:
    """Get integer from environment with default."""
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _get_env_float(key: str, default: float) -> float:
    """Get float from environment with default."""
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _get_env_str(key: str, default: str) -> str:
    """Get string from environment with default."""
    return os.environ.get(key, default)


# =============================================================================
# ENUMS
# =============================================================================


class CircuitBreakerState(Enum):
    """
    Circuit breaker state machine.

    State Transitions:
        CLOSED -> OPEN: After failure_threshold consecutive failures
        OPEN -> HALF_OPEN: After recovery_timeout seconds
        HALF_OPEN -> CLOSED: After half_open_max_calls successes
        HALF_OPEN -> OPEN: After any failure
    """
    CLOSED = "closed"      # Normal operation - requests pass through
    OPEN = "open"          # Failing - reject calls immediately
    HALF_OPEN = "half_open"  # Testing recovery - limited requests allowed


class RemoteCallState(Enum):
    """State of a remote call."""
    PENDING = auto()
    IN_PROGRESS = auto()
    SUCCESS = auto()
    FAILURE = auto()
    TIMEOUT = auto()
    CIRCUIT_OPEN = auto()


# =============================================================================
# EXCEPTIONS
# =============================================================================


class RemoteServiceError(Exception):
    """Base exception for remote service errors."""

    def __init__(
        self,
        message: str,
        service_name: Optional[str] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.service_name = service_name
        self.cause = cause

    def __str__(self) -> str:
        parts = [self.message]
        if self.service_name:
            parts.append(f"(service: {self.service_name})")
        if self.cause:
            parts.append(f"caused by: {self.cause}")
        return " ".join(parts)


class CircuitOpenError(RemoteServiceError):
    """Raised when circuit breaker is open."""

    def __init__(
        self,
        service_name: str,
        time_until_retry: float,
    ) -> None:
        super().__init__(
            f"Circuit breaker is OPEN for {service_name}. "
            f"Retry in {time_until_retry:.1f}s",
            service_name=service_name,
        )
        self.time_until_retry = time_until_retry


class RemoteCallTimeoutError(RemoteServiceError):
    """Raised when a remote call times out."""

    def __init__(
        self,
        service_name: str,
        timeout: float,
        method: Optional[str] = None,
    ) -> None:
        msg = f"Remote call to {service_name}"
        if method:
            msg += f".{method}"
        msg += f" timed out after {timeout}s"
        super().__init__(msg, service_name=service_name)
        self.timeout = timeout
        self.method = method


class ServiceUnavailableError(RemoteServiceError):
    """Raised when a service is not available."""

    def __init__(
        self,
        service_name: str,
        reason: str = "Service not responding",
    ) -> None:
        super().__init__(
            f"Service {service_name} unavailable: {reason}",
            service_name=service_name,
        )
        self.reason = reason


class RegistryError(RemoteServiceError):
    """Raised for service registry errors."""
    pass


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class RetryPolicy:
    """
    Retry policy with exponential backoff and jitter.

    Implements decorrelated jitter for optimal retry distribution:
    delay = min(max_delay, random(base_delay, previous_delay * 3))

    Reference: AWS Architecture Blog - Exponential Backoff And Jitter
    """
    max_retries: int = DEFAULT_MAX_RETRIES
    base_delay: float = DEFAULT_BASE_DELAY
    max_delay: float = DEFAULT_MAX_DELAY
    exponential_base: float = DEFAULT_EXPONENTIAL_BASE
    jitter: float = DEFAULT_JITTER

    # Retry conditions
    retry_on_timeout: bool = True
    retry_on_connection_error: bool = True
    retry_on_5xx: bool = True
    retry_on_429: bool = True  # Rate limiting

    def get_delay(self, attempt: int, previous_delay: Optional[float] = None) -> float:
        """
        Calculate delay with exponential backoff and decorrelated jitter.

        Args:
            attempt: Current attempt number (0-indexed)
            previous_delay: Previous delay for decorrelated jitter

        Returns:
            Delay in seconds before next retry
        """
        # Calculate base exponential delay
        exp_delay = self.base_delay * (self.exponential_base ** attempt)

        # Apply decorrelated jitter if we have previous delay
        if previous_delay is not None:
            # Decorrelated jitter: random between base and 3x previous
            jitter_min = self.base_delay
            jitter_max = previous_delay * 3
            delay = random.uniform(jitter_min, min(jitter_max, self.max_delay))
        else:
            # Simple jitter for first attempt
            jitter_amount = exp_delay * self.jitter * random.random()
            delay = exp_delay + jitter_amount

        # Cap at max delay
        return min(delay, self.max_delay)

    def should_retry(
        self,
        attempt: int,
        exception: Optional[Exception] = None,
        status_code: Optional[int] = None,
    ) -> bool:
        """
        Determine if we should retry based on attempt and error type.

        Args:
            attempt: Current attempt number
            exception: Exception that occurred (if any)
            status_code: HTTP status code (if any)

        Returns:
            True if should retry, False otherwise
        """
        # Check attempt limit
        if attempt >= self.max_retries:
            return False

        # Check exception type
        if exception is not None:
            if isinstance(exception, asyncio.TimeoutError):
                return self.retry_on_timeout
            if isinstance(exception, (ConnectionError, OSError)):
                return self.retry_on_connection_error

        # Check status code
        if status_code is not None:
            if status_code == 429 and self.retry_on_429:
                return True
            if 500 <= status_code < 600 and self.retry_on_5xx:
                return True

        return False

    @classmethod
    def aggressive(cls) -> RetryPolicy:
        """Create an aggressive retry policy for critical operations."""
        return cls(
            max_retries=5,
            base_delay=0.5,
            max_delay=30.0,
            exponential_base=2.0,
            jitter=0.2,
        )

    @classmethod
    def conservative(cls) -> RetryPolicy:
        """Create a conservative retry policy for non-critical operations."""
        return cls(
            max_retries=2,
            base_delay=2.0,
            max_delay=10.0,
            exponential_base=1.5,
            jitter=0.1,
        )

    @classmethod
    def no_retry(cls) -> RetryPolicy:
        """Create a policy with no retries."""
        return cls(max_retries=0)


@dataclass
class CircuitBreakerMetrics:
    """Metrics for circuit breaker monitoring."""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0  # Calls rejected while OPEN
    state_transitions: int = 0
    time_in_open_state: float = 0.0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None

    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        if self.total_calls == 0:
            return 1.0
        return self.successful_calls / self.total_calls

    @property
    def failure_rate(self) -> float:
        """Calculate failure rate."""
        return 1.0 - self.success_rate

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "total_calls": self.total_calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "rejected_calls": self.rejected_calls,
            "success_rate": self.success_rate,
            "failure_rate": self.failure_rate,
            "state_transitions": self.state_transitions,
            "time_in_open_state": self.time_in_open_state,
            "last_failure_time": self.last_failure_time,
            "last_success_time": self.last_success_time,
        }


@dataclass
class ServiceRegistryEntry:
    """
    Entry in the service registry.

    Contains service metadata for discovery and health tracking.
    """
    service_name: str
    host: str
    port: int
    pid: int
    status: str  # "healthy", "degraded", "unhealthy", "unknown"
    registered_at: float
    last_heartbeat: float
    ttl: float = DEFAULT_TTL
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        """Check if entry has expired based on TTL."""
        return time.time() - self.last_heartbeat > self.ttl

    @property
    def age_seconds(self) -> float:
        """Get age of entry in seconds."""
        return time.time() - self.registered_at

    @property
    def base_url(self) -> str:
        """Get base URL for service."""
        return f"http://{self.host}:{self.port}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "service_name": self.service_name,
            "host": self.host,
            "port": self.port,
            "pid": self.pid,
            "status": self.status,
            "registered_at": self.registered_at,
            "last_heartbeat": self.last_heartbeat,
            "ttl": self.ttl,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ServiceRegistryEntry:
        """Create from dictionary."""
        return cls(
            service_name=data["service_name"],
            host=data["host"],
            port=data["port"],
            pid=data.get("pid", -1),
            status=data.get("status", "unknown"),
            registered_at=data.get("registered_at", time.time()),
            last_heartbeat=data.get("last_heartbeat", time.time()),
            ttl=data.get("ttl", DEFAULT_TTL),
            metadata=data.get("metadata", {}),
        )


@dataclass
class RemoteCallResult(Generic[T]):
    """Result of a remote service call."""
    success: bool
    data: Optional[T] = None
    error: Optional[str] = None
    status_code: Optional[int] = None
    latency_ms: float = 0.0
    attempts: int = 1
    service_name: Optional[str] = None
    method: Optional[str] = None

    @classmethod
    def success_result(
        cls,
        data: T,
        status_code: int = 200,
        latency_ms: float = 0.0,
        attempts: int = 1,
        service_name: Optional[str] = None,
        method: Optional[str] = None,
    ) -> RemoteCallResult[T]:
        """Create a successful result."""
        return cls(
            success=True,
            data=data,
            status_code=status_code,
            latency_ms=latency_ms,
            attempts=attempts,
            service_name=service_name,
            method=method,
        )

    @classmethod
    def failure_result(
        cls,
        error: str,
        status_code: Optional[int] = None,
        latency_ms: float = 0.0,
        attempts: int = 1,
        service_name: Optional[str] = None,
        method: Optional[str] = None,
    ) -> RemoteCallResult[T]:
        """Create a failed result."""
        return cls(
            success=False,
            error=error,
            status_code=status_code,
            latency_ms=latency_ms,
            attempts=attempts,
            service_name=service_name,
            method=method,
        )


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================


class CircuitBreaker:
    """
    Circuit breaker pattern implementation.

    Prevents cascade failures by:
    1. Tracking consecutive failures
    2. Opening circuit after threshold failures
    3. Testing recovery after timeout
    4. Closing circuit on successful recovery

    Thread Safety:
        Uses lazy lock initialization. Never creates locks in __init__.

    Usage:
        cb = CircuitBreaker("my-service")

        try:
            async with cb.call_context():
                result = await make_remote_call()
        except CircuitOpenError:
            # Handle open circuit
            pass
    """

    __slots__ = (
        'name',
        'failure_threshold',
        'recovery_timeout',
        'half_open_max_calls',
        '_state',
        '_failure_count',
        '_success_count',
        '_last_failure_time',
        '_last_state_change',
        '_half_open_calls',
        '_metrics',
        '_lock',
        '_listeners',
    )

    def __init__(
        self,
        name: str,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        recovery_timeout: float = DEFAULT_RECOVERY_TIMEOUT,
        half_open_max_calls: int = DEFAULT_HALF_OPEN_MAX_CALLS,
    ) -> None:
        """
        Initialize circuit breaker.

        Args:
            name: Name of the service being protected
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before testing recovery
            half_open_max_calls: Number of successful calls to close circuit
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        # State (protected by lock)
        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._last_state_change = time.time()
        self._half_open_calls = 0

        # Metrics
        self._metrics = CircuitBreakerMetrics()

        # LAZY INITIALIZATION - No lock created here!
        self._lock: Optional[asyncio.Lock] = None

        # State change listeners
        self._listeners: List[Callable[[CircuitBreakerState, CircuitBreakerState], Awaitable[None]]] = []

    async def _ensure_lock(self) -> asyncio.Lock:
        """Ensure lock exists, creating lazily if needed."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def state(self) -> CircuitBreakerState:
        """Get current state."""
        return self._state

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (normal operation)."""
        return self._state == CircuitBreakerState.CLOSED

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (blocking calls)."""
        return self._state == CircuitBreakerState.OPEN

    @property
    def is_half_open(self) -> bool:
        """Check if circuit is half-open (testing recovery)."""
        return self._state == CircuitBreakerState.HALF_OPEN

    @property
    def time_until_retry(self) -> float:
        """Get time until retry is allowed (when OPEN)."""
        if not self.is_open or self._last_failure_time is None:
            return 0.0
        elapsed = time.time() - self._last_failure_time
        return max(0.0, self.recovery_timeout - elapsed)

    @property
    def metrics(self) -> CircuitBreakerMetrics:
        """Get metrics snapshot."""
        return self._metrics

    def add_state_listener(
        self,
        listener: Callable[[CircuitBreakerState, CircuitBreakerState], Awaitable[None]],
    ) -> None:
        """Add a listener for state changes."""
        self._listeners.append(listener)

    async def _notify_listeners(
        self,
        old_state: CircuitBreakerState,
        new_state: CircuitBreakerState,
    ) -> None:
        """Notify all listeners of state change."""
        for listener in self._listeners:
            try:
                await listener(old_state, new_state)
            except Exception as e:
                logger.warning(
                    f"[CircuitBreaker:{self.name}] Listener error: {e}"
                )

    async def _transition_to(self, new_state: CircuitBreakerState) -> None:
        """
        Transition to a new state.

        NOTE: Caller must hold the lock.
        """
        if self._state == new_state:
            return

        old_state = self._state
        now = time.time()

        # Track time in OPEN state
        if old_state == CircuitBreakerState.OPEN:
            self._metrics.time_in_open_state += now - self._last_state_change

        self._state = new_state
        self._last_state_change = now
        self._metrics.state_transitions += 1

        # Log transition
        if new_state == CircuitBreakerState.CLOSED:
            logger.info(
                f"[CircuitBreaker:{self.name}] CLOSED "
                f"(was {old_state.value} for {now - self._last_state_change:.1f}s)"
            )
        elif new_state == CircuitBreakerState.OPEN:
            logger.warning(
                f"[CircuitBreaker:{self.name}] OPEN "
                f"(failures: {self._failure_count}/{self.failure_threshold})"
            )
        else:
            logger.info(
                f"[CircuitBreaker:{self.name}] HALF_OPEN "
                f"(testing recovery after {self.recovery_timeout}s)"
            )

        # Notify listeners (outside lock)
        await self._notify_listeners(old_state, new_state)

    async def allow_request(self) -> bool:
        """
        Check if a request should be allowed.

        Returns:
            True if request should proceed, False if it should be blocked
        """
        lock = await self._ensure_lock()
        async with lock:
            now = time.time()

            if self._state == CircuitBreakerState.CLOSED:
                return True

            if self._state == CircuitBreakerState.OPEN:
                # Check if recovery timeout has passed
                if self._last_failure_time is not None:
                    elapsed = now - self._last_failure_time
                    if elapsed >= self.recovery_timeout:
                        # Transition to HALF_OPEN
                        await self._transition_to(CircuitBreakerState.HALF_OPEN)
                        self._half_open_calls = 0
                        return True

                # Still in OPEN state
                self._metrics.rejected_calls += 1
                return False

            # HALF_OPEN - allow limited requests
            if self._half_open_calls < self.half_open_max_calls:
                self._half_open_calls += 1
                return True

            return False

    async def record_success(self) -> None:
        """Record a successful call."""
        lock = await self._ensure_lock()
        async with lock:
            self._metrics.total_calls += 1
            self._metrics.successful_calls += 1
            self._metrics.last_success_time = time.time()

            if self._state == CircuitBreakerState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.half_open_max_calls:
                    # Recovery successful - close circuit
                    await self._transition_to(CircuitBreakerState.CLOSED)
                    self._failure_count = 0
                    self._success_count = 0
            elif self._state == CircuitBreakerState.CLOSED:
                # Reset failure count on success
                self._failure_count = 0

    async def record_failure(self, exception: Optional[Exception] = None) -> None:
        """Record a failed call."""
        lock = await self._ensure_lock()
        async with lock:
            now = time.time()
            self._metrics.total_calls += 1
            self._metrics.failed_calls += 1
            self._metrics.last_failure_time = now
            self._last_failure_time = now

            if self._state == CircuitBreakerState.HALF_OPEN:
                # Any failure in HALF_OPEN triggers OPEN
                await self._transition_to(CircuitBreakerState.OPEN)
                self._success_count = 0
            elif self._state == CircuitBreakerState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.failure_threshold:
                    await self._transition_to(CircuitBreakerState.OPEN)

    @asynccontextmanager
    async def call_context(self):
        """
        Context manager for circuit breaker protected calls.

        Usage:
            async with circuit_breaker.call_context():
                result = await make_call()

        Raises:
            CircuitOpenError: If circuit is open
        """
        allowed = await self.allow_request()
        if not allowed:
            raise CircuitOpenError(
                service_name=self.name,
                time_until_retry=self.time_until_retry,
            )

        try:
            yield
            await self.record_success()
        except Exception as e:
            await self.record_failure(e)
            raise

    async def call(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """
        Execute a function with circuit breaker protection.

        Args:
            func: Async function to execute
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Result of the function

        Raises:
            CircuitOpenError: If circuit is open
        """
        async with self.call_context():
            return await func(*args, **kwargs)

    async def reset(self) -> None:
        """Reset circuit breaker to initial state."""
        lock = await self._ensure_lock()
        async with lock:
            old_state = self._state
            self._state = CircuitBreakerState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None
            self._last_state_change = time.time()
            self._half_open_calls = 0

            if old_state != CircuitBreakerState.CLOSED:
                logger.info(f"[CircuitBreaker:{self.name}] Reset to CLOSED")

    def get_status(self) -> Dict[str, Any]:
        """Get current status."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "time_until_retry": self.time_until_retry,
            "metrics": self._metrics.to_dict(),
        }


# =============================================================================
# SERVICE REGISTRY
# =============================================================================


class ServiceRegistry:
    """
    File-based service registry for service discovery.

    Features:
    - File-based persistence for crash recovery
    - TTL-based automatic cleanup of stale entries
    - Heartbeat support for health monitoring
    - Thread-safe with lazy lock initialization

    Registry Path:
        ~/.jarvis/registry/services.json

    Usage:
        registry = ServiceRegistry()
        await registry.register(entry)
        service = await registry.discover("jarvis-prime")
    """

    __slots__ = (
        '_registry_path',
        '_lock',
        '_cache',
        '_cache_timestamp',
        '_cache_ttl',
        '_cleanup_interval',
        '_cleanup_task',
    )

    def __init__(
        self,
        registry_path: Optional[Path] = None,
        cache_ttl: float = 5.0,
        cleanup_interval: float = 30.0,
    ) -> None:
        """
        Initialize service registry.

        Args:
            registry_path: Path to registry file (default: ~/.jarvis/registry/services.json)
            cache_ttl: Cache TTL in seconds
            cleanup_interval: Interval for automatic cleanup
        """
        if registry_path is None:
            registry_path = Path.home() / ".jarvis" / "registry" / "services.json"

        self._registry_path = registry_path
        self._cache: Dict[str, ServiceRegistryEntry] = {}
        self._cache_timestamp: float = 0.0
        self._cache_ttl = cache_ttl
        self._cleanup_interval = cleanup_interval

        # LAZY INITIALIZATION
        self._lock: Optional[asyncio.Lock] = None
        self._cleanup_task: Optional[asyncio.Task] = None

        # Ensure directory exists
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)

    async def _ensure_lock(self) -> asyncio.Lock:
        """Ensure lock exists, creating lazily if needed."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _load_registry(self) -> Dict[str, ServiceRegistryEntry]:
        """Load registry from disk."""
        if not self._registry_path.exists():
            return {}

        try:
            content = await asyncio.to_thread(self._registry_path.read_text)
            data = json.loads(content)
            return {
                name: ServiceRegistryEntry.from_dict(entry)
                for name, entry in data.items()
            }
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"[ServiceRegistry] Failed to load registry: {e}")
            return {}

    async def _save_registry(self, entries: Dict[str, ServiceRegistryEntry]) -> None:
        """Save registry to disk."""
        try:
            data = {
                name: entry.to_dict()
                for name, entry in entries.items()
            }
            content = json.dumps(data, indent=2)
            await asyncio.to_thread(self._registry_path.write_text, content)
        except IOError as e:
            logger.error(f"[ServiceRegistry] Failed to save registry: {e}")

    async def _get_cache(self, force_refresh: bool = False) -> Dict[str, ServiceRegistryEntry]:
        """Get cached registry, refreshing if stale."""
        now = time.time()

        if force_refresh or now - self._cache_timestamp > self._cache_ttl:
            self._cache = await self._load_registry()
            self._cache_timestamp = now

        return self._cache

    async def register(self, entry: ServiceRegistryEntry) -> None:
        """
        Register a service in the registry.

        Args:
            entry: Service registry entry
        """
        lock = await self._ensure_lock()
        async with lock:
            entries = await self._get_cache(force_refresh=True)
            entries[entry.service_name] = entry
            await self._save_registry(entries)
            self._cache = entries

            logger.info(
                f"[ServiceRegistry] Registered {entry.service_name} "
                f"at {entry.host}:{entry.port} (pid={entry.pid})"
            )

    async def deregister(self, service_name: str) -> None:
        """
        Remove a service from the registry.

        Args:
            service_name: Name of service to remove
        """
        lock = await self._ensure_lock()
        async with lock:
            entries = await self._get_cache(force_refresh=True)
            if service_name in entries:
                del entries[service_name]
                await self._save_registry(entries)
                self._cache = entries
                logger.info(f"[ServiceRegistry] Deregistered {service_name}")

    async def discover(self, service_name: str) -> Optional[ServiceRegistryEntry]:
        """
        Discover a service by name.

        Args:
            service_name: Name of service to find

        Returns:
            Service entry if found and not expired, None otherwise
        """
        lock = await self._ensure_lock()
        async with lock:
            entries = await self._get_cache()
            entry = entries.get(service_name)

            if entry is not None and not entry.is_expired:
                return entry

            # Try to find from trinity config if not in registry
            if service_name in TRINITY_SERVICES:
                config = TRINITY_SERVICES[service_name]
                return ServiceRegistryEntry(
                    service_name=service_name,
                    host=_get_env_str(config["env_host"], config["default_host"]),
                    port=_get_env_int(config["env_port"], config["default_port"]),
                    pid=-1,  # Unknown
                    status="unknown",
                    registered_at=time.time(),
                    last_heartbeat=time.time(),
                    metadata={"source": "trinity_config"},
                )

            return None

    async def discover_all(self) -> List[ServiceRegistryEntry]:
        """
        Discover all registered services.

        Returns:
            List of all non-expired service entries
        """
        lock = await self._ensure_lock()
        async with lock:
            entries = await self._get_cache()
            return [
                entry for entry in entries.values()
                if not entry.is_expired
            ]

    async def heartbeat(self, service_name: str) -> None:
        """
        Update heartbeat for a service.

        Args:
            service_name: Name of service
        """
        lock = await self._ensure_lock()
        async with lock:
            entries = await self._get_cache(force_refresh=True)
            if service_name in entries:
                entries[service_name].last_heartbeat = time.time()
                await self._save_registry(entries)
                self._cache = entries

    async def update_status(
        self,
        service_name: str,
        status: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Update status for a service.

        Args:
            service_name: Name of service
            status: New status
            metadata: Additional metadata to merge
        """
        lock = await self._ensure_lock()
        async with lock:
            entries = await self._get_cache(force_refresh=True)
            if service_name in entries:
                entries[service_name].status = status
                entries[service_name].last_heartbeat = time.time()
                if metadata:
                    entries[service_name].metadata.update(metadata)
                await self._save_registry(entries)
                self._cache = entries

    async def cleanup_stale(self) -> int:
        """
        Remove entries that have exceeded their TTL.

        Returns:
            Number of entries removed
        """
        lock = await self._ensure_lock()
        async with lock:
            entries = await self._get_cache(force_refresh=True)
            expired = [
                name for name, entry in entries.items()
                if entry.is_expired
            ]

            for name in expired:
                del entries[name]
                logger.info(f"[ServiceRegistry] Removed stale entry: {name}")

            if expired:
                await self._save_registry(entries)
                self._cache = entries

            return len(expired)

    async def start_cleanup_task(self) -> None:
        """Start background cleanup task."""
        if self._cleanup_task is not None:
            return

        async def cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(self._cleanup_interval)
                    count = await self.cleanup_stale()
                    if count > 0:
                        logger.debug(
                            f"[ServiceRegistry] Cleaned up {count} stale entries"
                        )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[ServiceRegistry] Cleanup error: {e}")

        self._cleanup_task = asyncio.create_task(cleanup_loop())

    async def stop_cleanup_task(self) -> None:
        """Stop background cleanup task."""
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None


# =============================================================================
# REMOTE SERVICE PROXY
# =============================================================================


class RemoteServiceProxy(BaseAsyncService):
    """
    Proxy for calling services in remote repos.

    Features:
    - HTTP client with connection pooling
    - Circuit breaker protection
    - Retry with exponential backoff
    - Request/response logging
    - Automatic JSON serialization

    Usage:
        proxy = RemoteServiceProxy(
            service_name="jarvis-prime",
            remote_host="localhost",
            remote_port=8000,
        )
        await proxy.initialize()

        result = await proxy.call("generate", prompt="Hello")
    """

    __slots__ = (
        '_remote_host',
        '_remote_port',
        '_circuit_breaker',
        '_retry_policy',
        '_timeout',
        '_session',
        '_health_path',
        '_base_url',
    )

    def __init__(
        self,
        service_name: str,
        remote_host: str,
        remote_port: int,
        circuit_breaker: Optional[CircuitBreaker] = None,
        retry_policy: Optional[RetryPolicy] = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        health_path: str = "/health",
    ) -> None:
        """
        Initialize remote service proxy.

        Args:
            service_name: Name of the remote service
            remote_host: Host of the remote service
            remote_port: Port of the remote service
            circuit_breaker: Circuit breaker for this service
            retry_policy: Retry policy for failed requests
            timeout: Default request timeout in seconds
            health_path: Health check endpoint path
        """
        super().__init__(service_name=service_name)

        self._remote_host = remote_host
        self._remote_port = remote_port
        self._circuit_breaker = circuit_breaker or CircuitBreaker(service_name)
        self._retry_policy = retry_policy or RetryPolicy()
        self._timeout = timeout
        self._health_path = health_path
        self._base_url = f"http://{remote_host}:{remote_port}"

        # LAZY INITIALIZATION - session created in _do_initialize
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def base_url(self) -> str:
        """Get base URL for service."""
        return self._base_url

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Get circuit breaker."""
        return self._circuit_breaker

    async def _ensure_session(self) -> "aiohttp.ClientSession":
        """Ensure HTTP session exists."""
        if self._session is None or self._session.closed:
            import aiohttp

            # Configure connection pooling
            connector = aiohttp.TCPConnector(
                limit=100,  # Max connections
                limit_per_host=10,  # Max connections per host
                ttl_dns_cache=300,  # DNS cache TTL
                enable_cleanup_closed=True,
            )

            # Configure timeout
            timeout = aiohttp.ClientTimeout(total=self._timeout)

            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            )

        return self._session

    async def _do_initialize(self) -> None:
        """Initialize the proxy."""
        # Create session
        await self._ensure_session()
        logger.info(
            f"[{self.service_name}] Proxy initialized "
            f"for {self._base_url}"
        )

    async def _do_start(self) -> None:
        """Start the proxy."""
        pass  # No background tasks

    async def _do_stop(self) -> None:
        """Stop the proxy."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _do_health_check(self) -> HealthReport:
        """Check remote service health."""
        start = time.time()

        try:
            result = await self.call_get(self._health_path, timeout=5.0)
            latency_ms = (time.time() - start) * 1000

            if result.success:
                return HealthReport.healthy(
                    service_name=self.service_name,
                    latency_ms=latency_ms,
                    metadata={"remote": self._base_url},
                )
            else:
                return HealthReport.unhealthy(
                    service_name=self.service_name,
                    error=result.error or "Unknown error",
                    latency_ms=latency_ms,
                )
        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            return HealthReport.unhealthy(
                service_name=self.service_name,
                error=str(e),
                latency_ms=latency_ms,
            )

    async def _make_request(
        self,
        method: str,
        path: str,
        timeout: Optional[float] = None,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Any, float]:
        """
        Make HTTP request to remote service.

        Returns:
            (status_code, response_data, latency_ms)
        """
        import aiohttp

        session = await self._ensure_session()
        url = f"{self._base_url}{path}"
        timeout_val = aiohttp.ClientTimeout(total=timeout or self._timeout)

        start = time.time()

        async with session.request(
            method,
            url,
            json=json_data,
            params=params,
            timeout=timeout_val,
        ) as response:
            latency_ms = (time.time() - start) * 1000

            # Try to parse JSON response
            try:
                data = await response.json()
            except Exception:
                data = await response.text()

            return response.status, data, latency_ms

    async def call(
        self,
        method: str,
        path: Optional[str] = None,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> RemoteCallResult[Any]:
        """
        Call a method on the remote service with retry.

        Args:
            method: HTTP method or API method name
            path: URL path (default: /{method})
            timeout: Request timeout
            **kwargs: Request data

        Returns:
            RemoteCallResult with response or error
        """
        if path is None:
            path = f"/{method}"

        http_method = kwargs.pop("http_method", "POST")
        attempts = 0
        last_error: Optional[str] = None
        previous_delay: Optional[float] = None

        while True:
            attempts += 1

            try:
                # Check circuit breaker
                async with self._circuit_breaker.call_context():
                    status_code, data, latency_ms = await self._make_request(
                        http_method,
                        path,
                        timeout=timeout,
                        json_data=kwargs if kwargs else None,
                    )

                    if 200 <= status_code < 300:
                        return RemoteCallResult.success_result(
                            data=data,
                            status_code=status_code,
                            latency_ms=latency_ms,
                            attempts=attempts,
                            service_name=self.service_name,
                            method=method,
                        )

                    # Non-2xx response
                    error_msg = str(data) if data else f"HTTP {status_code}"

                    # Check if should retry
                    if self._retry_policy.should_retry(
                        attempts - 1,
                        status_code=status_code,
                    ):
                        delay = self._retry_policy.get_delay(
                            attempts - 1,
                            previous_delay,
                        )
                        previous_delay = delay
                        logger.warning(
                            f"[{self.service_name}] Request failed ({status_code}), "
                            f"retrying in {delay:.2f}s (attempt {attempts}/{self._retry_policy.max_retries})"
                        )
                        await asyncio.sleep(delay)
                        continue

                    return RemoteCallResult.failure_result(
                        error=error_msg,
                        status_code=status_code,
                        latency_ms=latency_ms,
                        attempts=attempts,
                        service_name=self.service_name,
                        method=method,
                    )

            except CircuitOpenError as e:
                return RemoteCallResult.failure_result(
                    error=f"Circuit breaker open: {e.time_until_retry:.1f}s until retry",
                    attempts=attempts,
                    service_name=self.service_name,
                    method=method,
                )

            except asyncio.TimeoutError as e:
                last_error = f"Timeout after {timeout or self._timeout}s"
                if self._retry_policy.should_retry(attempts - 1, exception=e):
                    delay = self._retry_policy.get_delay(attempts - 1, previous_delay)
                    previous_delay = delay
                    logger.warning(
                        f"[{self.service_name}] Request timed out, "
                        f"retrying in {delay:.2f}s"
                    )
                    await asyncio.sleep(delay)
                    continue

                return RemoteCallResult.failure_result(
                    error=last_error,
                    attempts=attempts,
                    service_name=self.service_name,
                    method=method,
                )

            except Exception as e:
                last_error = str(e)
                if self._retry_policy.should_retry(attempts - 1, exception=e):
                    delay = self._retry_policy.get_delay(attempts - 1, previous_delay)
                    previous_delay = delay
                    logger.warning(
                        f"[{self.service_name}] Request failed ({e}), "
                        f"retrying in {delay:.2f}s"
                    )
                    await asyncio.sleep(delay)
                    continue

                return RemoteCallResult.failure_result(
                    error=last_error,
                    attempts=attempts,
                    service_name=self.service_name,
                    method=method,
                )

    async def call_get(
        self,
        path: str,
        timeout: Optional[float] = None,
        **params: Any,
    ) -> RemoteCallResult[Any]:
        """Make a GET request."""
        return await self.call(
            "GET",
            path=path,
            timeout=timeout,
            http_method="GET",
            **params,
        )

    async def call_post(
        self,
        path: str,
        timeout: Optional[float] = None,
        **data: Any,
    ) -> RemoteCallResult[Any]:
        """Make a POST request."""
        return await self.call(
            "POST",
            path=path,
            timeout=timeout,
            http_method="POST",
            **data,
        )


# =============================================================================
# CROSS-REPO COORDINATOR
# =============================================================================


class CrossRepoCoordinator(BaseAsyncService):
    """
    Main coordinator for Trinity (JARVIS, Prime, Reactor).

    Features:
    - Service discovery via registry
    - Circuit breaker per service
    - Proxy pooling and lifecycle management
    - Health aggregation across all repos
    - Graceful degradation

    Trinity Services:
    - jarvis-body: Port 8010 - Main JARVIS agent
    - jarvis-prime: Port 8000 - LLM inference
    - reactor-core: Port 8090 - Training and evolution

    Usage:
        coordinator = CrossRepoCoordinator(
            local_service_name="jarvis-body",
            local_port=8010,
        )
        await coordinator.initialize()

        # Call remote service
        result = await coordinator.call_remote(
            "jarvis-prime",
            "generate",
            prompt="Hello",
        )
    """

    __slots__ = (
        '_local_port',
        '_registry',
        '_proxies',
        '_circuit_breakers',
        '_retry_policy',
        '_heartbeat_task',
        '_heartbeat_interval',
    )

    def __init__(
        self,
        local_service_name: str,
        local_port: int,
        registry: Optional[ServiceRegistry] = None,
        retry_policy: Optional[RetryPolicy] = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        """
        Initialize cross-repo coordinator.

        Args:
            local_service_name: Name of the local service
            local_port: Port of the local service
            registry: Service registry (created if not provided)
            retry_policy: Default retry policy for remote calls
            heartbeat_interval: Interval for heartbeat updates
        """
        super().__init__(service_name=f"CrossRepoCoordinator:{local_service_name}")

        self._local_port = local_port
        self._registry = registry or ServiceRegistry()
        self._proxies: Dict[str, RemoteServiceProxy] = {}
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}
        self._retry_policy = retry_policy or RetryPolicy()
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_task: Optional[asyncio.Task] = None

    @property
    def registry(self) -> ServiceRegistry:
        """Get service registry."""
        return self._registry

    async def _do_initialize(self) -> None:
        """Initialize the coordinator."""
        # Register ourselves
        await self._registry.register(ServiceRegistryEntry(
            service_name=self.service_name.split(":")[1],  # Local service name
            host="localhost",
            port=self._local_port,
            pid=os.getpid(),
            status="healthy",
            registered_at=time.time(),
            last_heartbeat=time.time(),
        ))

        # Start registry cleanup
        await self._registry.start_cleanup_task()

        # Discover and create proxies for all Trinity services
        for service_name, config in TRINITY_SERVICES.items():
            if service_name == self.service_name.split(":")[1]:
                continue  # Skip self

            await self._ensure_proxy(service_name)

        logger.info(
            f"[{self.service_name}] Initialized with "
            f"{len(self._proxies)} remote service proxies"
        )

    async def _do_start(self) -> None:
        """Start the coordinator."""
        # Start heartbeat task
        async def heartbeat_loop():
            while True:
                try:
                    await asyncio.sleep(self._heartbeat_interval)
                    await self._registry.heartbeat(
                        self.service_name.split(":")[1]
                    )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"[{self.service_name}] Heartbeat error: {e}")

        self._heartbeat_task = asyncio.create_task(heartbeat_loop())

        # Initialize all proxies
        for proxy in self._proxies.values():
            try:
                await proxy.initialize()
            except Exception as e:
                logger.warning(
                    f"[{self.service_name}] Failed to initialize proxy "
                    f"{proxy.service_name}: {e}"
                )

    async def _do_stop(self) -> None:
        """Stop the coordinator."""
        # Stop heartbeat
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None

        # Stop all proxies
        for proxy in self._proxies.values():
            try:
                await proxy.stop()
            except Exception as e:
                logger.warning(
                    f"[{self.service_name}] Error stopping proxy "
                    f"{proxy.service_name}: {e}"
                )

        # Deregister ourselves
        await self._registry.deregister(self.service_name.split(":")[1])

        # Stop registry cleanup
        await self._registry.stop_cleanup_task()

    async def _do_health_check(self) -> HealthReport:
        """Check health of all remote services."""
        start = time.time()

        checks: Dict[str, bool] = {}
        metadata: Dict[str, Any] = {}

        for name, proxy in self._proxies.items():
            try:
                report = await proxy.health_check()
                checks[name] = report.is_healthy
                metadata[name] = {
                    "status": report.status.name,
                    "latency_ms": report.latency_ms,
                }
            except Exception as e:
                checks[name] = False
                metadata[name] = {"error": str(e)}

        latency_ms = (time.time() - start) * 1000

        # Determine overall status
        if all(checks.values()):
            status = HealthStatus.HEALTHY
        elif any(checks.values()):
            status = HealthStatus.DEGRADED
        else:
            status = HealthStatus.UNHEALTHY

        return HealthReport(
            service_name=self.service_name,
            status=status,
            latency_ms=latency_ms,
            timestamp=datetime.utcnow(),
            checks=checks,
            metadata=metadata,
        )

    async def _ensure_proxy(self, service_name: str) -> RemoteServiceProxy:
        """Ensure proxy exists for a service."""
        if service_name not in self._proxies:
            # Get service info
            entry = await self._registry.discover(service_name)

            if entry is None:
                # Fall back to trinity config
                if service_name in TRINITY_SERVICES:
                    config = TRINITY_SERVICES[service_name]
                    host = _get_env_str(config["env_host"], config["default_host"])
                    port = _get_env_int(config["env_port"], config["default_port"])
                    health_path = config.get("health_path", "/health")
                else:
                    raise ServiceUnavailableError(
                        service_name,
                        "Service not found in registry or config",
                    )
            else:
                host = entry.host
                port = entry.port
                health_path = entry.metadata.get("health_path", "/health")

            # Create circuit breaker
            if service_name not in self._circuit_breakers:
                self._circuit_breakers[service_name] = CircuitBreaker(
                    name=service_name,
                    failure_threshold=_get_env_int(
                        "CIRCUIT_FAILURE_THRESHOLD",
                        DEFAULT_FAILURE_THRESHOLD,
                    ),
                    recovery_timeout=_get_env_float(
                        "CIRCUIT_RECOVERY_TIMEOUT",
                        DEFAULT_RECOVERY_TIMEOUT,
                    ),
                )

            # Create proxy
            self._proxies[service_name] = RemoteServiceProxy(
                service_name=service_name,
                remote_host=host,
                remote_port=port,
                circuit_breaker=self._circuit_breakers[service_name],
                retry_policy=self._retry_policy,
                health_path=health_path,
            )

        return self._proxies[service_name]

    async def get_proxy(self, remote_service: str) -> RemoteServiceProxy:
        """
        Get or create proxy for remote service.

        Args:
            remote_service: Name of remote service

        Returns:
            RemoteServiceProxy for the service
        """
        return await self._ensure_proxy(remote_service)

    async def call_remote(
        self,
        service_name: str,
        method: str,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        **kwargs: Any,
    ) -> RemoteCallResult[Any]:
        """
        Call a method on a remote service.

        Args:
            service_name: Name of remote service
            method: Method/endpoint to call
            timeout: Request timeout
            **kwargs: Method arguments

        Returns:
            RemoteCallResult with response or error
        """
        try:
            proxy = await self._ensure_proxy(service_name)

            if not proxy.is_initialized:
                await proxy.initialize()

            return await proxy.call(method, timeout=timeout, **kwargs)

        except Exception as e:
            return RemoteCallResult.failure_result(
                error=str(e),
                service_name=service_name,
                method=method,
            )

    async def health_check_remotes(self) -> Dict[str, HealthReport]:
        """
        Check health of all remote services.

        Returns:
            Dict mapping service name to health report
        """
        reports: Dict[str, HealthReport] = {}

        for name, proxy in self._proxies.items():
            try:
                reports[name] = await proxy.health_check()
            except Exception as e:
                reports[name] = HealthReport.unhealthy(
                    service_name=name,
                    error=str(e),
                )

        return reports

    async def broadcast(
        self,
        method: str,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        **kwargs: Any,
    ) -> Dict[str, RemoteCallResult[Any]]:
        """
        Broadcast a call to all remote services.

        Args:
            method: Method/endpoint to call
            timeout: Request timeout
            **kwargs: Method arguments

        Returns:
            Dict mapping service name to result
        """
        results: Dict[str, RemoteCallResult[Any]] = {}

        async def call_service(name: str, proxy: RemoteServiceProxy):
            results[name] = await proxy.call(method, timeout=timeout, **kwargs)

        tasks = [
            call_service(name, proxy)
            for name, proxy in self._proxies.items()
        ]

        await asyncio.gather(*tasks, return_exceptions=True)

        return results

    def get_status(self) -> Dict[str, Any]:
        """Get coordinator status."""
        return {
            "service_name": self.service_name,
            "local_port": self._local_port,
            "state": self._state.name,
            "proxies": {
                name: {
                    "base_url": proxy.base_url,
                    "state": proxy.state.name,
                    "circuit_breaker": proxy.circuit_breaker.get_status(),
                }
                for name, proxy in self._proxies.items()
            },
        }


# =============================================================================
# FACTORY FUNCTIONS
# =============================================================================


def create_circuit_breaker(
    name: str,
    failure_threshold: Optional[int] = None,
    recovery_timeout: Optional[float] = None,
    half_open_max_calls: Optional[int] = None,
) -> CircuitBreaker:
    """
    Create a circuit breaker with environment-driven defaults.

    Args:
        name: Service name
        failure_threshold: Override failure threshold
        recovery_timeout: Override recovery timeout
        half_open_max_calls: Override half-open max calls

    Returns:
        Configured CircuitBreaker
    """
    return CircuitBreaker(
        name=name,
        failure_threshold=failure_threshold or _get_env_int(
            "CIRCUIT_FAILURE_THRESHOLD",
            DEFAULT_FAILURE_THRESHOLD,
        ),
        recovery_timeout=recovery_timeout or _get_env_float(
            "CIRCUIT_RECOVERY_TIMEOUT",
            DEFAULT_RECOVERY_TIMEOUT,
        ),
        half_open_max_calls=half_open_max_calls or _get_env_int(
            "CIRCUIT_HALF_OPEN_MAX_CALLS",
            DEFAULT_HALF_OPEN_MAX_CALLS,
        ),
    )


def create_retry_policy(
    max_retries: Optional[int] = None,
    base_delay: Optional[float] = None,
    max_delay: Optional[float] = None,
) -> RetryPolicy:
    """
    Create a retry policy with environment-driven defaults.

    Args:
        max_retries: Override max retries
        base_delay: Override base delay
        max_delay: Override max delay

    Returns:
        Configured RetryPolicy
    """
    return RetryPolicy(
        max_retries=max_retries or _get_env_int(
            "RETRY_MAX_RETRIES",
            DEFAULT_MAX_RETRIES,
        ),
        base_delay=base_delay or _get_env_float(
            "RETRY_BASE_DELAY",
            DEFAULT_BASE_DELAY,
        ),
        max_delay=max_delay or _get_env_float(
            "RETRY_MAX_DELAY",
            DEFAULT_MAX_DELAY,
        ),
    )


async def create_coordinator(
    local_service_name: str,
    local_port: int,
    registry_path: Optional[Path] = None,
) -> CrossRepoCoordinator:
    """
    Create and initialize a cross-repo coordinator.

    Args:
        local_service_name: Name of local service
        local_port: Port of local service
        registry_path: Optional registry path

    Returns:
        Initialized CrossRepoCoordinator
    """
    registry = ServiceRegistry(registry_path) if registry_path else ServiceRegistry()

    coordinator = CrossRepoCoordinator(
        local_service_name=local_service_name,
        local_port=local_port,
        registry=registry,
    )

    await coordinator.initialize()
    return coordinator


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================


_global_coordinator: Optional[CrossRepoCoordinator] = None
_global_lock: Optional[asyncio.Lock] = None


async def _ensure_global_lock() -> asyncio.Lock:
    """Ensure global lock exists."""
    global _global_lock
    if _global_lock is None:
        _global_lock = asyncio.Lock()
    return _global_lock


async def get_coordinator(
    local_service_name: Optional[str] = None,
    local_port: Optional[int] = None,
) -> CrossRepoCoordinator:
    """
    Get or create the global cross-repo coordinator.

    Args:
        local_service_name: Name of local service (required on first call)
        local_port: Port of local service (required on first call)

    Returns:
        The global CrossRepoCoordinator
    """
    global _global_coordinator

    lock = await _ensure_global_lock()
    async with lock:
        if _global_coordinator is None:
            if local_service_name is None or local_port is None:
                raise ValueError(
                    "local_service_name and local_port required on first call"
                )
            _global_coordinator = await create_coordinator(
                local_service_name,
                local_port,
            )

        return _global_coordinator


async def shutdown_coordinator() -> None:
    """Shutdown the global coordinator."""
    global _global_coordinator

    lock = await _ensure_global_lock()
    async with lock:
        if _global_coordinator is not None:
            await _global_coordinator.stop()
            _global_coordinator = None


# =============================================================================
# MODULE EXPORTS
# =============================================================================

__all__ = [
    # Enums
    "CircuitBreakerState",
    "RemoteCallState",

    # Exceptions
    "RemoteServiceError",
    "CircuitOpenError",
    "RemoteCallTimeoutError",
    "ServiceUnavailableError",
    "RegistryError",

    # Data classes
    "RetryPolicy",
    "CircuitBreakerMetrics",
    "ServiceRegistryEntry",
    "RemoteCallResult",

    # Core classes
    "CircuitBreaker",
    "ServiceRegistry",
    "RemoteServiceProxy",
    "CrossRepoCoordinator",

    # Factory functions
    "create_circuit_breaker",
    "create_retry_policy",
    "create_coordinator",

    # Global instance
    "get_coordinator",
    "shutdown_coordinator",

    # Constants
    "TRINITY_SERVICES",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_RECOVERY_TIMEOUT",
    "DEFAULT_HALF_OPEN_MAX_CALLS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_BASE_DELAY",
    "DEFAULT_MAX_DELAY",
    "DEFAULT_REQUEST_TIMEOUT",
]
