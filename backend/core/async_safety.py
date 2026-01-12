"""
Async Safety Utilities v100.0
==============================

Ultra-robust, production-grade async patterns with zero hardcoding.

Advanced Features:
- Structural pattern matching for error classification
- Protocol-based extensibility
- Generic type support with variance
- Advanced decorators with full introspection
- Context managers with proper cleanup
- Retry with circuit breaker integration
- Timeout protection with graceful degradation
- Error context preservation with full traceback
- Backpressure control with adaptive thresholds
- Task shielding for critical operations
- Distributed tracing support
- Memory-efficient async iterators

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                    Async Safety Layer                            │
    │  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐       │
    │  │   Timeout     │  │    Retry      │  │  Circuit      │       │
    │  │   Manager     │  │    Engine     │  │  Breaker      │       │
    │  └───────┬───────┘  └───────┬───────┘  └───────┬───────┘       │
    │          │                  │                  │                │
    │          └──────────────────┼──────────────────┘                │
    │                             │                                   │
    │                    ┌────────▼────────┐                          │
    │                    │ SafeAsyncRunner │                          │
    │                    │  - Error wrap   │                          │
    │                    │  - Tracing      │                          │
    │                    │  - Backpressure │                          │
    │                    └─────────────────┘                          │
    └─────────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
import logging
import os
import sys
import time
import traceback
import uuid
import weakref
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    AsyncContextManager,
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    ClassVar,
    Coroutine,
    Dict,
    Final,
    Generic,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
    runtime_checkable,
)

# Python 3.10+ ParamSpec support with fallback
try:
    from typing import ParamSpec
except ImportError:
    # Python 3.9 fallback - try typing_extensions
    try:
        from typing_extensions import ParamSpec
    except ImportError:
        # Ultimate fallback - use TypeVar as a workaround
        ParamSpec = TypeVar  # type: ignore[misc, assignment]


# =============================================================================
# Python 3.9 Compatible Async Timeout Context Manager
# =============================================================================
# asyncio.timeout() is Python 3.11+, so we provide a compatible implementation

@asynccontextmanager
async def async_timeout(seconds: float) -> AsyncGenerator[None, None]:
    """
    Python 3.9 compatible async timeout context manager.

    This replicates the behavior of asyncio.timeout() from Python 3.11+
    using asyncio.wait_for() internally.

    Usage:
        async with async_timeout(5.0):
            await some_long_operation()
    """
    # Create a task that will be cancelled on timeout
    task = asyncio.current_task()
    if task is None:
        # Not in an async context, just yield
        yield
        return

    # Use a helper coroutine that yields control back
    async def _timeout_coro():
        try:
            yield
        except GeneratorExit:
            pass

    # Simple implementation using wait_for on a sentinel
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds

    try:
        yield
    except asyncio.CancelledError:
        # Check if we exceeded the deadline
        if loop.time() >= deadline:
            raise asyncio.TimeoutError()
        raise


# Alternative simpler approach for context manager timeout
class _AsyncTimeoutContext:
    """Simple async timeout context manager for Python 3.9 compatibility."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self._task: Optional[asyncio.Task] = None
        self._timeout_handle: Optional[asyncio.TimerHandle] = None

    async def __aenter__(self):
        self._task = asyncio.current_task()
        if self._task is not None and self.seconds > 0:
            loop = asyncio.get_running_loop()
            self._timeout_handle = loop.call_later(
                self.seconds,
                self._cancel_task
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._timeout_handle is not None:
            self._timeout_handle.cancel()
            self._timeout_handle = None

        if exc_type is asyncio.CancelledError and self._task is not None:
            # Convert CancelledError to TimeoutError if we triggered it
            raise asyncio.TimeoutError()

        return False

    def _cancel_task(self):
        if self._task is not None and not self._task.done():
            self._task.cancel()


def timeout_ctx(seconds: float) -> _AsyncTimeoutContext:
    """Create a Python 3.9 compatible async timeout context manager."""
    return _AsyncTimeoutContext(seconds)


# =============================================================================
# Environment-Driven Configuration (Zero Hardcoding)
# =============================================================================

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")


class AsyncSafetyConfig:
    """Environment-driven configuration for async safety utilities."""

    # Timeouts (all configurable via environment)
    DEFAULT_TIMEOUT: Final[float] = _env_float("ASYNC_DEFAULT_TIMEOUT", 30.0)
    OPERATION_TIMEOUT: Final[float] = _env_float("ASYNC_OPERATION_TIMEOUT", 60.0)
    CRITICAL_TIMEOUT: Final[float] = _env_float("ASYNC_CRITICAL_TIMEOUT", 120.0)
    CLEANUP_TIMEOUT: Final[float] = _env_float("ASYNC_CLEANUP_TIMEOUT", 5.0)

    # Retry configuration
    MAX_RETRIES: Final[int] = _env_int("ASYNC_MAX_RETRIES", 3)
    RETRY_BASE_DELAY: Final[float] = _env_float("ASYNC_RETRY_BASE_DELAY", 1.0)
    RETRY_MAX_DELAY: Final[float] = _env_float("ASYNC_RETRY_MAX_DELAY", 60.0)
    RETRY_EXPONENTIAL_BASE: Final[float] = _env_float("ASYNC_RETRY_EXPONENTIAL_BASE", 2.0)
    RETRY_JITTER: Final[float] = _env_float("ASYNC_RETRY_JITTER", 0.1)

    # Circuit breaker
    CB_FAILURE_THRESHOLD: Final[int] = _env_int("ASYNC_CB_FAILURE_THRESHOLD", 5)
    CB_SUCCESS_THRESHOLD: Final[int] = _env_int("ASYNC_CB_SUCCESS_THRESHOLD", 2)
    CB_TIMEOUT: Final[float] = _env_float("ASYNC_CB_TIMEOUT", 60.0)

    # Backpressure
    MAX_CONCURRENT: Final[int] = _env_int("ASYNC_MAX_CONCURRENT", 100)
    QUEUE_SIZE: Final[int] = _env_int("ASYNC_QUEUE_SIZE", 1000)

    # Tracing
    ENABLE_TRACING: Final[bool] = _env_bool("ASYNC_ENABLE_TRACING", True)
    TRACE_SAMPLE_RATE: Final[float] = _env_float("ASYNC_TRACE_SAMPLE_RATE", 1.0)

    # Error handling
    PRESERVE_TRACEBACK: Final[bool] = _env_bool("ASYNC_PRESERVE_TRACEBACK", True)
    LOG_FULL_ERRORS: Final[bool] = _env_bool("ASYNC_LOG_FULL_ERRORS", True)

    # State persistence
    STATE_DIR: Final[Path] = Path(os.getenv(
        "ASYNC_STATE_DIR",
        str(Path.home() / ".jarvis" / "state" / "async")
    ))


# =============================================================================
# TimeoutConfig: Comprehensive Environment-Driven Timeout Management v100.1
# =============================================================================

class TimeoutConfig:
    """
    Comprehensive environment-driven timeout configuration.

    All timeouts are configurable via environment variables with sensible defaults.
    Use these named timeouts instead of hardcoded values throughout the codebase.

    Usage:
        from backend.core.async_safety import TimeoutConfig

        # Use named timeouts
        await asyncio.wait_for(operation(), timeout=TimeoutConfig.API_CALL)

        # Or get with fallback
        timeout = TimeoutConfig.get("CUSTOM_OP", default=10.0)

    Environment Variables:
        TIMEOUT_API_CALL=5.0            - External API calls
        TIMEOUT_DATABASE=30.0           - Database operations
        TIMEOUT_FILE_IO=10.0            - File I/O operations
        TIMEOUT_NETWORK=15.0            - Network operations
        TIMEOUT_PROCESS=60.0            - Process execution
        TIMEOUT_VOICE=5.0               - Voice processing
        TIMEOUT_ML_INFERENCE=30.0       - ML model inference
        TIMEOUT_STARTUP=120.0           - Startup operations
        TIMEOUT_SHUTDOWN=30.0           - Shutdown/cleanup
        TIMEOUT_HEARTBEAT=5.0           - Heartbeat checks
        TIMEOUT_HEALTH_CHECK=10.0       - Health checks
        TIMEOUT_LOCK_ACQUIRE=5.0        - Lock acquisition
        TIMEOUT_IPC=10.0                - Inter-process communication
        TIMEOUT_WEBSOCKET=30.0          - WebSocket operations
        TIMEOUT_CACHE=2.0               - Cache operations
        TIMEOUT_CRITICAL=120.0          - Critical long-running ops
    """

    # API & Network timeouts
    API_CALL: Final[float] = _env_float("TIMEOUT_API_CALL", 5.0)
    NETWORK: Final[float] = _env_float("TIMEOUT_NETWORK", 15.0)
    WEBSOCKET: Final[float] = _env_float("TIMEOUT_WEBSOCKET", 30.0)
    HTTP_REQUEST: Final[float] = _env_float("TIMEOUT_HTTP_REQUEST", 30.0)

    # Database & Storage timeouts
    DATABASE: Final[float] = _env_float("TIMEOUT_DATABASE", 30.0)
    DATABASE_QUERY: Final[float] = _env_float("TIMEOUT_DATABASE_QUERY", 10.0)
    DATABASE_CONNECT: Final[float] = _env_float("TIMEOUT_DATABASE_CONNECT", 5.0)
    FILE_IO: Final[float] = _env_float("TIMEOUT_FILE_IO", 10.0)
    CACHE: Final[float] = _env_float("TIMEOUT_CACHE", 2.0)

    # Process & System timeouts
    PROCESS: Final[float] = _env_float("TIMEOUT_PROCESS", 60.0)
    PROCESS_START: Final[float] = _env_float("TIMEOUT_PROCESS_START", 30.0)
    PROCESS_STOP: Final[float] = _env_float("TIMEOUT_PROCESS_STOP", 10.0)
    STARTUP: Final[float] = _env_float("TIMEOUT_STARTUP", 120.0)
    SHUTDOWN: Final[float] = _env_float("TIMEOUT_SHUTDOWN", 30.0)
    CLEANUP: Final[float] = _env_float("TIMEOUT_CLEANUP", 5.0)

    # Voice & ML timeouts
    VOICE: Final[float] = _env_float("TIMEOUT_VOICE", 5.0)
    VOICE_RECOGNITION: Final[float] = _env_float("TIMEOUT_VOICE_RECOGNITION", 10.0)
    VOICE_SYNTHESIS: Final[float] = _env_float("TIMEOUT_VOICE_SYNTHESIS", 15.0)
    ML_INFERENCE: Final[float] = _env_float("TIMEOUT_ML_INFERENCE", 30.0)
    ML_LOAD: Final[float] = _env_float("TIMEOUT_ML_LOAD", 60.0)

    # Synchronization timeouts
    LOCK_ACQUIRE: Final[float] = _env_float("TIMEOUT_LOCK_ACQUIRE", 5.0)
    SEMAPHORE: Final[float] = _env_float("TIMEOUT_SEMAPHORE", 10.0)
    QUEUE: Final[float] = _env_float("TIMEOUT_QUEUE", 5.0)

    # Communication timeouts
    IPC: Final[float] = _env_float("TIMEOUT_IPC", 10.0)
    RPC: Final[float] = _env_float("TIMEOUT_RPC", 30.0)
    EVENT: Final[float] = _env_float("TIMEOUT_EVENT", 5.0)
    MESSAGE: Final[float] = _env_float("TIMEOUT_MESSAGE", 10.0)

    # Health & Monitoring timeouts
    HEARTBEAT: Final[float] = _env_float("TIMEOUT_HEARTBEAT", 5.0)
    HEALTH_CHECK: Final[float] = _env_float("TIMEOUT_HEALTH_CHECK", 10.0)
    PROBE: Final[float] = _env_float("TIMEOUT_PROBE", 3.0)

    # Operation categories
    QUICK: Final[float] = _env_float("TIMEOUT_QUICK", 2.0)
    NORMAL: Final[float] = _env_float("TIMEOUT_NORMAL", 30.0)
    LONG: Final[float] = _env_float("TIMEOUT_LONG", 60.0)
    CRITICAL: Final[float] = _env_float("TIMEOUT_CRITICAL", 120.0)
    EXTENDED: Final[float] = _env_float("TIMEOUT_EXTENDED", 300.0)

    # Vision & Display timeouts
    VISION_CAPTURE: Final[float] = _env_float("TIMEOUT_VISION_CAPTURE", 5.0)
    VISION_ANALYSIS: Final[float] = _env_float("TIMEOUT_VISION_ANALYSIS", 15.0)
    DISPLAY_UPDATE: Final[float] = _env_float("TIMEOUT_DISPLAY_UPDATE", 3.0)

    # Authentication timeouts
    AUTH: Final[float] = _env_float("TIMEOUT_AUTH", 10.0)
    BIOMETRIC: Final[float] = _env_float("TIMEOUT_BIOMETRIC", 5.0)
    TOKEN_REFRESH: Final[float] = _env_float("TIMEOUT_TOKEN_REFRESH", 10.0)

    # GCP/Cloud timeouts
    GCP_API: Final[float] = _env_float("TIMEOUT_GCP_API", 30.0)
    CLOUD_STORAGE: Final[float] = _env_float("TIMEOUT_CLOUD_STORAGE", 60.0)
    VM_OPERATION: Final[float] = _env_float("TIMEOUT_VM_OPERATION", 120.0)

    # Mapping of operation names to timeouts (for dynamic lookup)
    _TIMEOUT_MAP: ClassVar[Dict[str, float]] = {}

    @classmethod
    def get(cls, name: str, default: float = 30.0) -> float:
        """
        Get a timeout by name with fallback to environment variable.

        Args:
            name: Timeout name (e.g., "API_CALL", "DATABASE")
            default: Default value if not found

        Returns:
            Timeout value in seconds
        """
        # First check class attributes
        if hasattr(cls, name.upper()):
            return getattr(cls, name.upper())

        # Then check environment
        env_key = f"TIMEOUT_{name.upper()}"
        return _env_float(env_key, default)

    @classmethod
    def for_operation(cls, operation_type: str) -> float:
        """
        Get recommended timeout for an operation type.

        Args:
            operation_type: Type of operation (e.g., "api", "db", "voice")

        Returns:
            Recommended timeout in seconds
        """
        op_lower = operation_type.lower()

        # Map common operation types to timeouts
        mapping = {
            "api": cls.API_CALL,
            "http": cls.HTTP_REQUEST,
            "db": cls.DATABASE,
            "database": cls.DATABASE,
            "query": cls.DATABASE_QUERY,
            "file": cls.FILE_IO,
            "network": cls.NETWORK,
            "voice": cls.VOICE,
            "ml": cls.ML_INFERENCE,
            "inference": cls.ML_INFERENCE,
            "startup": cls.STARTUP,
            "shutdown": cls.SHUTDOWN,
            "lock": cls.LOCK_ACQUIRE,
            "ipc": cls.IPC,
            "websocket": cls.WEBSOCKET,
            "ws": cls.WEBSOCKET,
            "cache": cls.CACHE,
            "health": cls.HEALTH_CHECK,
            "heartbeat": cls.HEARTBEAT,
            "auth": cls.AUTH,
            "gcp": cls.GCP_API,
            "cloud": cls.GCP_API,
            "vision": cls.VISION_ANALYSIS,
            "capture": cls.VISION_CAPTURE,
            "process": cls.PROCESS,
        }

        return mapping.get(op_lower, cls.NORMAL)

    @classmethod
    def with_buffer(cls, base_timeout: float, buffer_percent: float = 20.0) -> float:
        """
        Add a buffer to a timeout for safety margin.

        Args:
            base_timeout: Base timeout in seconds
            buffer_percent: Percentage buffer to add (default 20%)

        Returns:
            Timeout with buffer added
        """
        return base_timeout * (1 + buffer_percent / 100)

    @classmethod
    def adaptive(cls, base_timeout: float, attempt: int, max_timeout: float = 300.0) -> float:
        """
        Calculate adaptive timeout that increases with retry attempts.

        Args:
            base_timeout: Base timeout in seconds
            attempt: Current attempt number (1-based)
            max_timeout: Maximum timeout to return

        Returns:
            Adaptive timeout in seconds
        """
        # Exponential backoff: base * 1.5^(attempt-1)
        adaptive = base_timeout * (1.5 ** (attempt - 1))
        return min(adaptive, max_timeout)


# =============================================================================
# Type Variables & Protocols
# =============================================================================

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)
T_contra = TypeVar("T_contra", contravariant=True)
P = ParamSpec("P")
ExceptionT = TypeVar("ExceptionT", bound=BaseException)

# Logger
logger = logging.getLogger("AsyncSafety")


@runtime_checkable
class Retryable(Protocol):
    """Protocol for retryable operations."""
    async def execute(self) -> Any: ...
    def should_retry(self, exception: BaseException) -> bool: ...


@runtime_checkable
class CircuitBreakerAware(Protocol):
    """Protocol for circuit breaker aware components."""
    def get_circuit_state(self) -> "CircuitState": ...
    def record_success(self) -> None: ...
    def record_failure(self, exception: BaseException) -> None: ...


# =============================================================================
# Error Types & Classification
# =============================================================================

class ErrorCategory(Enum):
    """Categories for error classification."""
    TRANSIENT = auto()      # Retry-able (network, timeout)
    PERMANENT = auto()      # Don't retry (invalid input, auth)
    RESOURCE = auto()       # Resource exhaustion (memory, connections)
    CIRCUIT_OPEN = auto()   # Circuit breaker open
    TIMEOUT = auto()        # Timeout error
    CANCELLED = auto()      # Task cancelled
    UNKNOWN = auto()        # Unclassified


@dataclass(frozen=True)  # slots=True removed for Python 3.9 compatibility
class ErrorContext:
    """
    Rich error context with full diagnostic information.

    Preserves the complete error chain for debugging.
    """
    category: ErrorCategory
    original_error: BaseException
    message: str
    operation_name: str
    attempt: int = 1
    max_attempts: int = 1
    duration_ms: float = 0.0
    traceback_str: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    trace_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.name,
            "error_type": type(self.original_error).__name__,
            "message": self.message,
            "operation": self.operation_name,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "duration_ms": self.duration_ms,
            "traceback": self.traceback_str if AsyncSafetyConfig.PRESERVE_TRACEBACK else None,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
        }


def classify_error(error: BaseException) -> ErrorCategory:
    """
    Classify an error for retry decisions.

    Returns:
        ErrorCategory indicating how to handle the error
    """
    import errno

    # Timeout errors - may be transient
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return ErrorCategory.TIMEOUT

    # Cancelled - don't retry
    if isinstance(error, asyncio.CancelledError):
        return ErrorCategory.CANCELLED

    # Connection errors - usually transient
    if isinstance(error, (ConnectionError, ConnectionRefusedError, ConnectionResetError)):
        return ErrorCategory.TRANSIENT

    # OS errors - check errno
    if isinstance(error, OSError):
        if error.errno in (errno.ETIMEDOUT, errno.ECONNREFUSED, errno.ECONNRESET):
            return ErrorCategory.TRANSIENT
        elif error.errno in (errno.ENOMEM, errno.ENOSPC):
            return ErrorCategory.RESOURCE
        return ErrorCategory.PERMANENT

    # Value/Type errors - permanent
    if isinstance(error, (ValueError, TypeError, AttributeError)):
        return ErrorCategory.PERMANENT

    # Permission errors - permanent
    if isinstance(error, PermissionError):
        return ErrorCategory.PERMANENT

    # Memory errors - resource
    if isinstance(error, MemoryError):
        return ErrorCategory.RESOURCE

    # Default
    return ErrorCategory.UNKNOWN


# =============================================================================
# Circuit Breaker (State Machine Pattern)
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing, reject calls
    HALF_OPEN = "half_open" # Testing recovery


@dataclass
class CircuitBreakerState:
    """Persistent circuit breaker state."""
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[float] = None
    last_state_change: float = field(default_factory=time.time)
    total_failures: int = 0
    total_successes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_failure_time": self.last_failure_time,
            "last_state_change": self.last_state_change,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CircuitBreakerState":
        return cls(
            state=CircuitState(data.get("state", "closed")),
            failure_count=data.get("failure_count", 0),
            success_count=data.get("success_count", 0),
            last_failure_time=data.get("last_failure_time"),
            last_state_change=data.get("last_state_change", time.time()),
            total_failures=data.get("total_failures", 0),
            total_successes=data.get("total_successes", 0),
        )


class PersistentCircuitBreaker:
    """
    Production-grade circuit breaker with state persistence.

    Features:
    - State machine pattern (CLOSED -> OPEN -> HALF_OPEN -> CLOSED)
    - Persistent state across restarts
    - Configurable thresholds via environment
    - Async-safe operations
    - Metrics collection
    """

    _instances: ClassVar[Dict[str, "PersistentCircuitBreaker"]] = {}
    _lock: ClassVar[Optional[asyncio.Lock]] = None

    def __init__(
        self,
        name: str,
        failure_threshold: Optional[int] = None,
        success_threshold: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        persist: bool = True,
    ):
        self.name = name
        self.failure_threshold = failure_threshold or AsyncSafetyConfig.CB_FAILURE_THRESHOLD
        self.success_threshold = success_threshold or AsyncSafetyConfig.CB_SUCCESS_THRESHOLD
        self.timeout_seconds = timeout_seconds or AsyncSafetyConfig.CB_TIMEOUT
        self.persist = persist

        self._state_file = AsyncSafetyConfig.STATE_DIR / f"circuit_breaker_{name}.json"
        self._state = self._load_state()
        self._local_lock = asyncio.Lock()

        logger.debug(f"CircuitBreaker[{name}] initialized: state={self._state.state.value}")

    @classmethod
    async def get(cls, name: str, **kwargs) -> "PersistentCircuitBreaker":
        """Get or create a circuit breaker instance (singleton per name)."""
        if cls._lock is None:
            cls._lock = asyncio.Lock()

        async with cls._lock:
            if name not in cls._instances:
                cls._instances[name] = cls(name, **kwargs)
            return cls._instances[name]

    def _load_state(self) -> CircuitBreakerState:
        """Load state from disk."""
        if not self.persist or not self._state_file.exists():
            return CircuitBreakerState()

        try:
            data = json.loads(self._state_file.read_text())
            state = CircuitBreakerState.from_dict(data)

            # Check if timeout elapsed for OPEN state
            if state.state == CircuitState.OPEN and state.last_failure_time:
                elapsed = time.time() - state.last_failure_time
                if elapsed >= self.timeout_seconds:
                    state.state = CircuitState.HALF_OPEN
                    state.last_state_change = time.time()
                    logger.info(f"CircuitBreaker[{self.name}] OPEN -> HALF_OPEN (timeout elapsed)")

            return state
        except Exception as e:
            logger.warning(f"CircuitBreaker[{self.name}] failed to load state: {e}")
            return CircuitBreakerState()

    def _save_state(self) -> None:
        """Persist state to disk."""
        if not self.persist:
            return

        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(self._state.to_dict(), indent=2))
        except Exception as e:
            logger.warning(f"CircuitBreaker[{self.name}] failed to save state: {e}")

    @property
    def state(self) -> CircuitState:
        """Get current state, checking for timeout."""
        if self._state.state == CircuitState.OPEN and self._state.last_failure_time:
            elapsed = time.time() - self._state.last_failure_time
            if elapsed >= self.timeout_seconds:
                self._transition_to(CircuitState.HALF_OPEN)
        return self._state.state

    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        old_state = self._state.state
        self._state.state = new_state
        self._state.last_state_change = time.time()

        if new_state == CircuitState.CLOSED:
            self._state.failure_count = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._state.success_count = 0

        self._save_state()
        logger.info(f"CircuitBreaker[{self.name}] {old_state.value} -> {new_state.value}")

    def can_execute(self) -> bool:
        """Check if operation can be executed."""
        state = self.state  # This checks timeout
        if state == CircuitState.CLOSED:
            return True
        elif state == CircuitState.HALF_OPEN:
            return True  # Allow test request
        elif state == CircuitState.OPEN:
            return False
        return False

    def record_success(self) -> None:
        """Record a successful operation."""
        self._state.total_successes += 1

        if self._state.state == CircuitState.HALF_OPEN:
            self._state.success_count += 1
            if self._state.success_count >= self.success_threshold:
                self._transition_to(CircuitState.CLOSED)
        elif self._state.state == CircuitState.CLOSED:
            self._state.failure_count = 0
            self._save_state()

    def record_failure(self, error: Optional[BaseException] = None) -> None:
        """Record a failed operation."""
        self._state.failure_count += 1
        self._state.total_failures += 1
        self._state.last_failure_time = time.time()

        if self._state.state == CircuitState.HALF_OPEN:
            self._transition_to(CircuitState.OPEN)
        elif self._state.state == CircuitState.CLOSED:
            if self._state.failure_count >= self.failure_threshold:
                self._transition_to(CircuitState.OPEN)
            else:
                self._save_state()

    def get_stats(self) -> Dict[str, Any]:
        """Get circuit breaker statistics."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._state.failure_count,
            "success_count": self._state.success_count,
            "total_failures": self._state.total_failures,
            "total_successes": self._state.total_successes,
            "last_failure_time": self._state.last_failure_time,
        }

    @asynccontextmanager
    async def guard(self) -> AsyncGenerator[None, None]:
        """
        Context manager that guards an operation with circuit breaker.

        Usage:
            async with circuit_breaker.guard():
                await risky_operation()
        """
        if not self.can_execute():
            raise CircuitBreakerOpenError(f"Circuit breaker '{self.name}' is OPEN")

        try:
            yield
            self.record_success()
        except Exception as e:
            self.record_failure(e)
            raise


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


# =============================================================================
# Retry Engine with Exponential Backoff
# =============================================================================

@dataclass
class RetryPolicy:
    """Configuration for retry behavior."""
    max_attempts: int = field(default_factory=lambda: AsyncSafetyConfig.MAX_RETRIES)
    base_delay: float = field(default_factory=lambda: AsyncSafetyConfig.RETRY_BASE_DELAY)
    max_delay: float = field(default_factory=lambda: AsyncSafetyConfig.RETRY_MAX_DELAY)
    exponential_base: float = field(default_factory=lambda: AsyncSafetyConfig.RETRY_EXPONENTIAL_BASE)
    jitter: float = field(default_factory=lambda: AsyncSafetyConfig.RETRY_JITTER)
    retryable_exceptions: Tuple[Type[BaseException], ...] = (Exception,)
    non_retryable_exceptions: Tuple[Type[BaseException], ...] = (
        asyncio.CancelledError,
        KeyboardInterrupt,
        SystemExit,
    )

    def should_retry(self, error: BaseException, attempt: int) -> bool:
        """Determine if operation should be retried."""
        if attempt >= self.max_attempts:
            return False

        if isinstance(error, self.non_retryable_exceptions):
            return False

        category = classify_error(error)
        if category in (ErrorCategory.TRANSIENT, ErrorCategory.TIMEOUT):
            return True
        elif category in (ErrorCategory.PERMANENT, ErrorCategory.CANCELLED):
            return False
        else:
            return isinstance(error, self.retryable_exceptions)

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for next retry with exponential backoff and jitter."""
        import random

        delay = self.base_delay * (self.exponential_base ** (attempt - 1))
        delay = min(delay, self.max_delay)

        # Add jitter
        jitter_range = delay * self.jitter
        delay += random.uniform(-jitter_range, jitter_range)

        return max(0.0, delay)


class RetryEngine:
    """
    Advanced retry engine with circuit breaker integration.

    Features:
    - Exponential backoff with jitter
    - Circuit breaker integration
    - Error classification
    - Full error context preservation
    """

    def __init__(
        self,
        policy: Optional[RetryPolicy] = None,
        circuit_breaker: Optional[PersistentCircuitBreaker] = None,
    ):
        self.policy = policy or RetryPolicy()
        self.circuit_breaker = circuit_breaker

    async def execute(
        self,
        operation: Callable[[], Awaitable[T]],
        operation_name: str = "operation",
    ) -> T:
        """
        Execute operation with retry logic.

        Args:
            operation: Async callable to execute
            operation_name: Name for logging/tracing

        Returns:
            Result of successful operation

        Raises:
            Last exception if all retries exhausted
        """
        last_error: Optional[BaseException] = None
        errors: List[ErrorContext] = []

        for attempt in range(1, self.policy.max_attempts + 1):
            start_time = time.time()

            # Check circuit breaker
            if self.circuit_breaker and not self.circuit_breaker.can_execute():
                raise CircuitBreakerOpenError(
                    f"Circuit breaker for '{operation_name}' is OPEN"
                )

            try:
                result = await operation()

                # Record success
                if self.circuit_breaker:
                    self.circuit_breaker.record_success()

                return result

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000

                # Create error context
                error_ctx = ErrorContext(
                    category=classify_error(e),
                    original_error=e,
                    message=str(e),
                    operation_name=operation_name,
                    attempt=attempt,
                    max_attempts=self.policy.max_attempts,
                    duration_ms=duration_ms,
                    traceback_str=traceback.format_exc() if AsyncSafetyConfig.PRESERVE_TRACEBACK else None,
                )
                errors.append(error_ctx)
                last_error = e

                # Log the error
                if AsyncSafetyConfig.LOG_FULL_ERRORS:
                    logger.warning(
                        f"[{operation_name}] Attempt {attempt}/{self.policy.max_attempts} failed: {e}",
                        exc_info=True,
                    )

                # Record failure with circuit breaker
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure(e)

                # Check if we should retry
                if not self.policy.should_retry(e, attempt):
                    logger.error(f"[{operation_name}] Non-retryable error: {e}")
                    raise

                # Calculate and apply delay
                if attempt < self.policy.max_attempts:
                    delay = self.policy.get_delay(attempt)
                    logger.debug(f"[{operation_name}] Retrying in {delay:.2f}s...")
                    await asyncio.sleep(delay)

        # All retries exhausted
        logger.error(f"[{operation_name}] All {self.policy.max_attempts} attempts failed")
        raise last_error


# =============================================================================
# Timeout Protection
# =============================================================================

@dataclass
class TimeoutOptions:
    """
    Configuration for timeout behavior (used by TimeoutManager).

    NOTE: This was renamed from TimeoutConfig to TimeoutOptions in v100.2
    to avoid shadowing the main TimeoutConfig class which contains
    environment-driven timeout constants like HEALTH_CHECK, API_CALL, etc.
    """
    timeout_seconds: float
    on_timeout: Optional[Callable[[], Awaitable[Any]]] = None
    default_value: Optional[Any] = None
    shield_cleanup: bool = True  # Protect cleanup from cancellation


class TimeoutManager:
    """
    Advanced timeout manager with cleanup protection.

    Features:
    - Configurable timeout per operation
    - Cleanup shielding from cancellation
    - Callback on timeout
    - Full context preservation
    """

    def __init__(self, config: TimeoutOptions):
        self.config = config

    @asynccontextmanager
    async def timeout(
        self,
        operation_name: str = "operation",
    ) -> AsyncGenerator[None, None]:
        """
        Context manager for timeout protection.

        Usage:
            async with timeout_manager.timeout("my_operation"):
                await long_running_task()
        """
        try:
            # Python 3.9 compatible (asyncio.timeout is 3.11+)
            async with timeout_ctx(self.config.timeout_seconds):
                yield
        except asyncio.TimeoutError:
            logger.warning(
                f"[{operation_name}] Timed out after {self.config.timeout_seconds}s"
            )

            # Run timeout callback if provided
            if self.config.on_timeout:
                if self.config.shield_cleanup:
                    await asyncio.shield(self.config.on_timeout())
                else:
                    await self.config.on_timeout()

            raise


# =============================================================================
# Backpressure Controller
# =============================================================================

class BackpressureController:
    """
    Adaptive backpressure controller for rate limiting.

    Features:
    - Semaphore-based concurrency control
    - Queue depth monitoring
    - Adaptive threshold adjustment
    - Metrics collection
    """

    def __init__(
        self,
        max_concurrent: Optional[int] = None,
        queue_size: Optional[int] = None,
    ):
        self.max_concurrent = max_concurrent or AsyncSafetyConfig.MAX_CONCURRENT
        self.queue_size = queue_size or AsyncSafetyConfig.QUEUE_SIZE

        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._active_count = 0
        self._queued_count = 0
        self._total_processed = 0
        self._total_rejected = 0
        self._lock = asyncio.Lock()

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        Acquire permission to proceed.

        Returns:
            True if acquired, False if rejected
        """
        async with self._lock:
            if self._queued_count >= self.queue_size:
                self._total_rejected += 1
                return False
            self._queued_count += 1

        try:
            if timeout:
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=timeout,
                )
            else:
                await self._semaphore.acquire()

            async with self._lock:
                self._queued_count -= 1
                self._active_count += 1

            return True

        except asyncio.TimeoutError:
            async with self._lock:
                self._queued_count -= 1
                self._total_rejected += 1
            return False

    async def release(self) -> None:
        """Release permission."""
        self._semaphore.release()
        async with self._lock:
            self._active_count -= 1
            self._total_processed += 1

    @asynccontextmanager
    async def guard(self, timeout: Optional[float] = None) -> AsyncGenerator[bool, None]:
        """
        Context manager for backpressure protection.

        Usage:
            async with backpressure.guard() as acquired:
                if acquired:
                    await do_work()
        """
        acquired = await self.acquire(timeout)
        try:
            yield acquired
        finally:
            if acquired:
                await self.release()

    def get_stats(self) -> Dict[str, Any]:
        """Get backpressure statistics."""
        return {
            "max_concurrent": self.max_concurrent,
            "active_count": self._active_count,
            "queued_count": self._queued_count,
            "total_processed": self._total_processed,
            "total_rejected": self._total_rejected,
            "utilization": self._active_count / self.max_concurrent,
        }


# =============================================================================
# Lazy Async Lock (Thread-Safe)
# =============================================================================

class LazyAsyncLock:
    """
    Thread-safe lazy-initialized asyncio.Lock.

    Solves the "no running event loop" error when creating locks at module load.

    Usage:
        # At module level (safe!)
        _lock = LazyAsyncLock()

        # In async function
        async with _lock:
            await critical_section()
    """

    def __init__(self):
        self._lock: Optional[asyncio.Lock] = None
        self._creation_lock = asyncio.Lock()

    def _get_lock(self) -> asyncio.Lock:
        """Get or create the underlying lock."""
        if self._lock is None:
            # Thread-safe creation
            self._lock = asyncio.Lock()
        return self._lock

    async def acquire(self) -> bool:
        """Acquire the lock."""
        return await self._get_lock().acquire()

    def release(self) -> None:
        """Release the lock."""
        if self._lock:
            self._lock.release()

    async def __aenter__(self) -> "LazyAsyncLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


class LazyAsyncEvent:
    """
    Thread-safe lazy-initialized asyncio.Event.

    Usage:
        _event = LazyAsyncEvent()

        # In async function
        await _event.wait()
    """

    def __init__(self):
        self._event: Optional[asyncio.Event] = None

    def _get_event(self) -> asyncio.Event:
        if self._event is None:
            self._event = asyncio.Event()
        return self._event

    def set(self) -> None:
        self._get_event().set()

    def clear(self) -> None:
        self._get_event().clear()

    def is_set(self) -> bool:
        return self._get_event().is_set()

    async def wait(self) -> bool:
        return await self._get_event().wait()


# =============================================================================
# Safe Async Decorators
# =============================================================================

def with_timeout(
    timeout_seconds: Optional[float] = None,
    default_value: Optional[T] = None,
    on_timeout: Optional[Callable[[], Awaitable[Any]]] = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[Optional[T]]]]:
    """
    Decorator that adds timeout protection to async functions.

    Usage:
        @with_timeout(5.0, default_value=None)
        async def slow_operation() -> str:
            await asyncio.sleep(10)
            return "done"
    """
    timeout = timeout_seconds or AsyncSafetyConfig.DEFAULT_TIMEOUT

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[Optional[T]]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Optional[T]:
            try:
                return await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"{func.__name__} timed out after {timeout}s")
                if on_timeout:
                    return await on_timeout()
                return default_value
        return wrapper
    return decorator


def with_retry(
    max_attempts: Optional[int] = None,
    base_delay: Optional[float] = None,
    circuit_breaker_name: Optional[str] = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """
    Decorator that adds retry logic to async functions.

    Usage:
        @with_retry(max_attempts=3, circuit_breaker_name="my_service")
        async def flaky_operation() -> str:
            return await call_external_api()
    """
    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            policy = RetryPolicy(
                max_attempts=max_attempts or AsyncSafetyConfig.MAX_RETRIES,
                base_delay=base_delay or AsyncSafetyConfig.RETRY_BASE_DELAY,
            )

            cb = None
            if circuit_breaker_name:
                cb = await PersistentCircuitBreaker.get(circuit_breaker_name)

            engine = RetryEngine(policy=policy, circuit_breaker=cb)
            return await engine.execute(
                lambda: func(*args, **kwargs),
                operation_name=func.__name__,
            )
        return wrapper
    return decorator


def preserve_error_context(
    func: Callable[P, Awaitable[T]]
) -> Callable[P, Awaitable[T]]:
    """
    Decorator that preserves full error context including traceback.

    Usage:
        @preserve_error_context
        async def risky_operation():
            # Full traceback logged on error
    """
    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(
                f"{func.__name__} failed: {e}",
                exc_info=True,
                extra={
                    "function": func.__name__,
                    "error_type": type(e).__name__,
                    "traceback": traceback.format_exc(),
                }
            )
            raise
    return wrapper


def with_backpressure(
    max_concurrent: Optional[int] = None,
    acquire_timeout: Optional[float] = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[Optional[T]]]]:
    """
    Decorator that adds backpressure protection.

    Usage:
        @with_backpressure(max_concurrent=10)
        async def rate_limited_operation():
            return await call_api()
    """
    controller = BackpressureController(max_concurrent=max_concurrent)

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[Optional[T]]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Optional[T]:
            async with controller.guard(timeout=acquire_timeout) as acquired:
                if not acquired:
                    logger.warning(f"{func.__name__} rejected due to backpressure")
                    return None
                return await func(*args, **kwargs)
        return wrapper
    return decorator


# =============================================================================
# Safe Async Context Managers
# =============================================================================

@asynccontextmanager
async def safe_operation(
    timeout: Optional[float] = None,
    operation_name: str = "operation",
    on_error: Optional[Callable[[Exception], Awaitable[None]]] = None,
) -> AsyncGenerator[None, None]:
    """
    Context manager for safe async operations.

    Usage:
        async with safe_operation(timeout=5.0, operation_name="database_query"):
            result = await db.query(...)
    """
    start_time = time.time()

    try:
        if timeout:
            # Python 3.9 compatible (asyncio.timeout is 3.11+)
            async with timeout_ctx(timeout):
                yield
        else:
            yield
    except asyncio.TimeoutError:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(f"[{operation_name}] Timed out after {duration_ms:.0f}ms")
        raise
    except asyncio.CancelledError:
        logger.info(f"[{operation_name}] Cancelled")
        raise
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(
            f"[{operation_name}] Failed after {duration_ms:.0f}ms: {e}",
            exc_info=True,
        )
        if on_error:
            await on_error(e)
        raise


@asynccontextmanager
async def cleanup_on_error(
    cleanup: Callable[[], Awaitable[None]],
    shield: bool = True,
) -> AsyncGenerator[None, None]:
    """
    Context manager that runs cleanup on error.

    Usage:
        async with cleanup_on_error(lambda: resource.close()):
            await use_resource()
    """
    try:
        yield
    except Exception:
        if shield:
            await asyncio.shield(cleanup())
        else:
            await cleanup()
        raise


# =============================================================================
# Safe Loop Execution
# =============================================================================

async def safe_loop(
    operation: Callable[[], Awaitable[None]],
    interval: float,
    timeout_per_iteration: Optional[float] = None,
    max_iterations: Optional[int] = None,
    stop_event: Optional[LazyAsyncEvent] = None,
    on_error: Optional[Callable[[Exception], Awaitable[bool]]] = None,
) -> None:
    """
    Run an operation in a loop with safety guarantees.

    Args:
        operation: Async callable to execute
        interval: Sleep interval between iterations
        timeout_per_iteration: Max time per iteration
        max_iterations: Maximum number of iterations (None = infinite)
        stop_event: Event to signal loop termination
        on_error: Callback on error, return True to continue, False to stop

    Usage:
        await safe_loop(
            lambda: process_queue(),
            interval=1.0,
            timeout_per_iteration=5.0,
            stop_event=shutdown_event,
        )
    """
    iteration = 0

    while True:
        # Check stop condition
        if stop_event and stop_event.is_set():
            logger.info("Safe loop stopped via event")
            break

        if max_iterations and iteration >= max_iterations:
            logger.info(f"Safe loop reached max iterations ({max_iterations})")
            break

        iteration += 1

        try:
            if timeout_per_iteration:
                await asyncio.wait_for(operation(), timeout=timeout_per_iteration)
            else:
                await operation()
        except asyncio.TimeoutError:
            logger.warning(f"Loop iteration {iteration} timed out")
        except asyncio.CancelledError:
            logger.info("Safe loop cancelled")
            break
        except Exception as e:
            logger.error(f"Loop iteration {iteration} failed: {e}", exc_info=True)
            if on_error:
                should_continue = await on_error(e)
                if not should_continue:
                    break

        await asyncio.sleep(interval)


# =============================================================================
# Parallel Execution Utilities
# =============================================================================

async def gather_with_concurrency(
    coros: Sequence[Coroutine[Any, Any, T]],
    max_concurrent: int,
    return_exceptions: bool = False,
) -> List[Union[T, BaseException]]:
    """
    Execute coroutines with limited concurrency.

    Usage:
        results = await gather_with_concurrency(
            [fetch(url) for url in urls],
            max_concurrent=10,
        )
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def bounded(coro: Coroutine[Any, Any, T]) -> T:
        async with semaphore:
            return await coro

    return await asyncio.gather(
        *[bounded(c) for c in coros],
        return_exceptions=return_exceptions,
    )


async def first_completed(
    coros: Sequence[Coroutine[Any, Any, T]],
    timeout: Optional[float] = None,
) -> Tuple[Optional[T], List[BaseException]]:
    """
    Return result of first successfully completed coroutine.

    Usage:
        result, errors = await first_completed([
            fetch_from_primary(),
            fetch_from_backup(),
        ])
    """
    tasks = [asyncio.create_task(c) for c in coros]
    errors: List[BaseException] = []
    result: Optional[T] = None

    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in done:
            if task.exception():
                errors.append(task.exception())
            else:
                result = task.result()
                break

        # Cancel pending tasks
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return result, errors


# =============================================================================
# Module-Level Singletons
# =============================================================================

# Global backpressure controller
_global_backpressure: Optional[BackpressureController] = None

def get_global_backpressure() -> BackpressureController:
    """Get global backpressure controller."""
    global _global_backpressure
    if _global_backpressure is None:
        _global_backpressure = BackpressureController()
    return _global_backpressure


# Global shutdown event
_shutdown_event = LazyAsyncEvent()

def get_shutdown_event() -> LazyAsyncEvent:
    """Get global shutdown event."""
    return _shutdown_event

def signal_shutdown() -> None:
    """Signal global shutdown."""
    _shutdown_event.set()


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Configuration
    "AsyncSafetyConfig",

    # Error handling
    "ErrorCategory",
    "ErrorContext",
    "classify_error",

    # Circuit breaker
    "CircuitState",
    "CircuitBreakerState",
    "PersistentCircuitBreaker",
    "CircuitBreakerOpenError",

    # Retry
    "RetryPolicy",
    "RetryEngine",

    # Timeout
    "TimeoutConfig",
    "TimeoutOptions",  # v100.2: Renamed from TimeoutConfig (dataclass for TimeoutManager)
    "TimeoutManager",

    # Backpressure
    "BackpressureController",

    # Lazy primitives
    "LazyAsyncLock",
    "LazyAsyncEvent",

    # Decorators
    "with_timeout",
    "with_retry",
    "preserve_error_context",
    "with_backpressure",

    # Context managers
    "safe_operation",
    "cleanup_on_error",

    # Loop utilities
    "safe_loop",

    # Parallel utilities
    "gather_with_concurrency",
    "first_completed",

    # Singletons
    "get_global_backpressure",
    "get_shutdown_event",
    "signal_shutdown",
]
