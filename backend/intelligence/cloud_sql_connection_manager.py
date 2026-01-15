#!/usr/bin/env python3
"""
Singleton CloudSQL Connection Manager for JARVIS v3.1
======================================================

Production-grade, fully async, thread-safe connection pool manager with:
- Automatic leak detection and IMMEDIATE recovery
- Smart adaptive cleanup (aggressive mode on leak detection)
- Circuit breaker pattern for failure handling
- Background cleanup tasks for orphan connection termination
- Comprehensive metrics and observability
- Signal-aware graceful shutdown (SIGINT, SIGTERM, atexit)
- Strict connection limits for db-f1-micro (max 3 connections)
- Dynamic configuration via environment variables
- Fully async lock patterns for concurrent safety

v3.1 Changes:
- Fixed singleton initialization race condition with proper double-checked locking
- Added retry logic for asyncpg TLS race condition (InvalidStateError)
- Fixed AsyncLock lazy creation race condition
- Added serialized connection setup to prevent thundering herd during pool creation
- Added specific error handling for TLS upgrade protocol failures

Architecture:
┌─────────────────────────────────────────────────────────────────────────────┐
│                  CloudSQL Connection Manager v3.1                            │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐                 │
│  │ Connection     │  │ Leak Detector  │  │ Circuit        │                 │
│  │ Pool (asyncpg) │  │ & Auto-Cleaner │  │ Breaker        │                 │
│  └───────┬────────┘  └───────┬────────┘  └───────┬────────┘                 │
│          └───────────────────┼───────────────────┘                          │
│                              ▼                                               │
│                 ┌─────────────────────────────┐                             │
│                 │    Connection Coordinator   │                             │
│                 │  • Lifecycle management     │                             │
│                 │  • Immediate leak cleanup   │                             │
│                 │  • Adaptive monitoring      │                             │
│                 │  • Dynamic configuration    │                             │
│                 └─────────────────────────────┘                             │
└─────────────────────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 3.1.0
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import time
import traceback
import threading
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Union, TypeVar

try:
    import asyncpg
    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False

# v9.0: Rate limit manager integration for CloudSQL Admin API
RATE_LIMIT_MANAGER_AVAILABLE = False
try:
    from core.gcp_rate_limit_manager import (
        get_rate_limit_manager_sync,
        GCPService,
        OperationType,
    )
    RATE_LIMIT_MANAGER_AVAILABLE = True
except ImportError:
    pass

# v10.0: Intelligent Rate Orchestrator for ML-powered rate limiting
INTELLIGENT_RATE_ORCHESTRATOR_AVAILABLE = False
try:
    from core.intelligent_rate_orchestrator import (
        get_rate_orchestrator,
        ServiceType as RateServiceType,
        OperationType as RateOpType,
        RequestPriority,
    )
    INTELLIGENT_RATE_ORCHESTRATOR_AVAILABLE = True
except ImportError:
    try:
        from backend.core.intelligent_rate_orchestrator import (
            get_rate_orchestrator,
            ServiceType as RateServiceType,
            OperationType as RateOpType,
            RequestPriority,
        )
        INTELLIGENT_RATE_ORCHESTRATOR_AVAILABLE = True
    except ImportError:
        pass

# v5.0: Cloud SQL Proxy Detector for intelligent proxy availability detection
PROXY_DETECTOR_AVAILABLE = False
try:
    from intelligence.cloud_sql_proxy_detector import (
        get_proxy_detector,
        ProxyStatus,
    )
    PROXY_DETECTOR_AVAILABLE = True
except ImportError:
    try:
        from backend.intelligence.cloud_sql_proxy_detector import (
            get_proxy_detector,
            ProxyStatus,
        )
        PROXY_DETECTOR_AVAILABLE = True
    except ImportError:
        pass

logger = logging.getLogger(__name__)

# Type variable for generic async operations
T = TypeVar('T')


# =============================================================================
# Async-Safe Lock Implementation
# =============================================================================

class AsyncLock:
    """
    A lock that works in both sync and async contexts.

    Provides a unified interface for thread-safe operations that can be
    used from both synchronous and asynchronous code.

    v3.1: Fixed lazy async lock creation race condition by using thread lock
    to protect async lock creation.
    """

    def __init__(self):
        self._thread_lock = threading.RLock()
        self._async_lock: Optional[asyncio.Lock] = None
        self._async_lock_creation_lock = threading.Lock()

    def _get_async_lock(self) -> Optional[asyncio.Lock]:
        """
        Lazily create async lock with thread-safe double-checked locking.

        v3.1: Uses separate lock to prevent race condition where multiple
        coroutines could try to create the async lock simultaneously.
        """
        if self._async_lock is None:
            with self._async_lock_creation_lock:
                # Double-check inside lock
                if self._async_lock is None:
                    try:
                        self._async_lock = asyncio.Lock()
                    except RuntimeError:
                        # No event loop available - will be created later
                        pass
        return self._async_lock

    def __enter__(self):
        self._thread_lock.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._thread_lock.release()
        return False

    async def __aenter__(self):
        self._thread_lock.acquire()
        try:
            async_lock = self._get_async_lock()
            if async_lock:
                await async_lock.acquire()
        except Exception:
            self._thread_lock.release()
            raise
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            async_lock = self._get_async_lock()
            if async_lock and async_lock.locked():
                async_lock.release()
        finally:
            self._thread_lock.release()
        return False


# =============================================================================
# Retry Logic with Exponential Backoff
# =============================================================================

class RetryConfig:
    """Configuration for retry behavior."""
    max_retries: int = 3
    base_delay: float = 0.5  # seconds
    max_delay: float = 10.0  # seconds
    exponential_base: float = 2.0
    jitter: bool = True  # Add random jitter to prevent thundering herd


async def retry_with_backoff(
    operation: Callable,
    *args,
    retry_config: Optional[RetryConfig] = None,
    retryable_exceptions: Tuple[type, ...] = (Exception,),
    on_retry: Optional[Callable[[int, Exception], None]] = None,
    **kwargs
) -> T:
    """
    Execute an async operation with exponential backoff retry.

    Args:
        operation: Async callable to execute
        *args: Positional arguments for operation
        retry_config: Retry configuration (uses defaults if None)
        retryable_exceptions: Tuple of exception types to retry on
        on_retry: Optional callback called on each retry (attempt, exception)
        **kwargs: Keyword arguments for operation

    Returns:
        Result of the operation

    Raises:
        Last exception if all retries exhausted
    """
    import random

    config = retry_config or RetryConfig()
    last_exception = None

    for attempt in range(config.max_retries + 1):
        try:
            return await operation(*args, **kwargs)

        except retryable_exceptions as e:
            last_exception = e

            if attempt >= config.max_retries:
                # Exhausted all retries
                logger.error(f"❌ Operation failed after {attempt + 1} attempts: {e}")
                raise

            # Calculate delay with exponential backoff
            delay = min(
                config.base_delay * (config.exponential_base ** attempt),
                config.max_delay
            )

            # Add jitter to prevent thundering herd
            if config.jitter:
                delay = delay * (0.5 + random.random())

            logger.warning(
                f"⚠️ Attempt {attempt + 1}/{config.max_retries + 1} failed: {e}. "
                f"Retrying in {delay:.2f}s..."
            )

            # Call retry callback if provided
            if on_retry:
                try:
                    on_retry(attempt, e)
                except Exception:
                    pass

            await asyncio.sleep(delay)

    # Should never reach here, but just in case
    if last_exception:
        raise last_exception
    raise RuntimeError("Retry logic error - no exception captured")


def is_connection_error(e: Exception) -> bool:
    """
    Check if an exception is a connection-related error that should be retried.

    v3.1: Added InvalidStateError detection for asyncpg TLS race condition.
    This error occurs when multiple connections attempt TLS upgrade simultaneously,
    corrupting the internal state machine in asyncpg's TLSUpgradeProto.
    """
    # v3.1: Check exception type directly for specific asyncpg errors
    if isinstance(e, asyncio.InvalidStateError):
        return True

    error_msg = str(e).lower()
    connection_error_patterns = [
        "connection has been released",
        "connection is closed",
        "pool is closed",
        "connection refused",
        "connection reset",
        "connection timed out",
        "cannot connect",
        "no connection",
        "pool exhausted",
        "server closed",
        "ssl error",
        "network error",
        "connection lost",
        # v3.1: asyncpg TLS race condition errors
        "invalid state",
        "invalidstateerror",
        "tlsupgradeproto",
        "data_received",
        "set_result",
    ]
    return any(pattern in error_msg for pattern in connection_error_patterns)


# =============================================================================
# Dynamic Configuration
# =============================================================================

def _get_env_int(key: str, default: int) -> int:
    """Get integer from environment variable."""
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _get_env_float(key: str, default: float) -> float:
    """Get float from environment variable."""
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _get_env_bool(key: str, default: bool) -> bool:
    """Get boolean from environment variable."""
    val = os.environ.get(key, str(default)).lower()
    return val in ('true', '1', 'yes', 'on')


@dataclass
class ConnectionConfig:
    """
    Dynamic configuration for connection pool.

    All values can be overridden via environment variables:
    - JARVIS_DB_MIN_CONNECTIONS
    - JARVIS_DB_MAX_CONNECTIONS
    - JARVIS_DB_CONNECTION_TIMEOUT
    - JARVIS_DB_QUERY_TIMEOUT
    - JARVIS_DB_POOL_CREATION_TIMEOUT
    - JARVIS_DB_MAX_QUERIES_PER_CONN
    - JARVIS_DB_MAX_IDLE_TIME
    - JARVIS_DB_CHECKOUT_WARNING
    - JARVIS_DB_CHECKOUT_TIMEOUT
    - JARVIS_DB_LEAK_CHECK_INTERVAL
    - JARVIS_DB_LEAKED_IDLE_MINUTES
    - JARVIS_DB_FAILURE_THRESHOLD
    - JARVIS_DB_RECOVERY_TIMEOUT
    - JARVIS_DB_ENABLE_CLEANUP
    - JARVIS_DB_CLEANUP_INTERVAL
    - JARVIS_DB_AGGRESSIVE_CLEANUP
    """

    def __post_init__(self):
        """Load values from environment variables after initialization."""
        self._load_from_env()

    def _load_from_env(self):
        """Load configuration from environment variables."""
        # Pool sizing (conservative for db-f1-micro)
        self.min_connections = _get_env_int('JARVIS_DB_MIN_CONNECTIONS', self.min_connections)
        self.max_connections = _get_env_int('JARVIS_DB_MAX_CONNECTIONS', self.max_connections)

        # Timeouts
        self.connection_timeout = _get_env_float('JARVIS_DB_CONNECTION_TIMEOUT', self.connection_timeout)
        self.query_timeout = _get_env_float('JARVIS_DB_QUERY_TIMEOUT', self.query_timeout)
        self.pool_creation_timeout = _get_env_float('JARVIS_DB_POOL_CREATION_TIMEOUT', self.pool_creation_timeout)

        # Connection lifecycle
        self.max_queries_per_connection = _get_env_int('JARVIS_DB_MAX_QUERIES_PER_CONN', self.max_queries_per_connection)
        self.max_idle_time_seconds = _get_env_float('JARVIS_DB_MAX_IDLE_TIME', self.max_idle_time_seconds)

        # Leak detection
        self.checkout_warning_seconds = _get_env_float('JARVIS_DB_CHECKOUT_WARNING', self.checkout_warning_seconds)
        self.checkout_timeout_seconds = _get_env_float('JARVIS_DB_CHECKOUT_TIMEOUT', self.checkout_timeout_seconds)
        self.leak_check_interval_seconds = _get_env_float('JARVIS_DB_LEAK_CHECK_INTERVAL', self.leak_check_interval_seconds)
        self.leaked_idle_threshold_minutes = _get_env_int('JARVIS_DB_LEAKED_IDLE_MINUTES', self.leaked_idle_threshold_minutes)

        # Circuit breaker
        self.failure_threshold = _get_env_int('JARVIS_DB_FAILURE_THRESHOLD', self.failure_threshold)
        self.recovery_timeout_seconds = _get_env_float('JARVIS_DB_RECOVERY_TIMEOUT', self.recovery_timeout_seconds)

        # Background tasks
        self.enable_background_cleanup = _get_env_bool('JARVIS_DB_ENABLE_CLEANUP', self.enable_background_cleanup)
        self.cleanup_interval_seconds = _get_env_float('JARVIS_DB_CLEANUP_INTERVAL', self.cleanup_interval_seconds)

        # Aggressive cleanup mode
        self.aggressive_cleanup_on_leak = _get_env_bool('JARVIS_DB_AGGRESSIVE_CLEANUP', self.aggressive_cleanup_on_leak)

    # Pool sizing (conservative for db-f1-micro)
    min_connections: int = 1
    max_connections: int = 3

    # Timeouts (seconds)
    connection_timeout: float = 5.0
    query_timeout: float = 30.0
    pool_creation_timeout: float = 15.0

    # Connection lifecycle
    max_queries_per_connection: int = 10000
    max_idle_time_seconds: float = 180.0  # 3 minutes (more aggressive)

    # Leak detection thresholds (more aggressive)
    checkout_warning_seconds: float = 30.0   # Warn if held > 30s
    checkout_timeout_seconds: float = 120.0  # Force release after 2 min
    leak_check_interval_seconds: float = 15.0  # Check every 15s
    leaked_idle_threshold_minutes: int = 3   # DB connections idle > 3 min (more aggressive)

    # Circuit breaker
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 30.0

    # Background tasks
    enable_background_cleanup: bool = True
    cleanup_interval_seconds: float = 30.0  # Cleanup every 30s (more aggressive)

    # Aggressive cleanup mode - immediately kill leaked connections
    aggressive_cleanup_on_leak: bool = True

    def reload_from_env(self) -> None:
        """Reload configuration from environment variables."""
        self._load_from_env()
        logger.info("🔄 Connection config reloaded from environment")


# =============================================================================
# Connection Tracking
# =============================================================================

@dataclass
class ConnectionCheckout:
    """Tracks a connection checkout for leak detection."""
    checkout_id: int
    checkout_time: datetime
    stack_trace: str
    caller_info: str = ""
    released: bool = False
    release_time: Optional[datetime] = None
    query_count: int = 0

    @property
    def age_seconds(self) -> float:
        return (datetime.now() - self.checkout_time).total_seconds()

    @property
    def is_potentially_leaked(self) -> bool:
        return not self.released and self.age_seconds > 120.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.checkout_id,
            'age_seconds': round(self.age_seconds, 1),
            'released': self.released,
            'caller': self.caller_info,
            'query_count': self.query_count,
        }


@dataclass
class ConnectionMetrics:
    """Comprehensive metrics for monitoring."""
    total_checkouts: int = 0
    total_releases: int = 0
    total_errors: int = 0
    total_timeouts: int = 0
    total_leaks_detected: int = 0
    total_leaks_recovered: int = 0
    total_immediate_cleanups: int = 0
    pool_exhaustion_count: int = 0
    circuit_breaker_trips: int = 0

    avg_checkout_duration_ms: float = 0.0
    max_checkout_duration_ms: float = 0.0
    min_checkout_duration_ms: float = float('inf')

    # Connection health
    healthy_connections: int = 0
    unhealthy_connections: int = 0

    created_at: datetime = field(default_factory=datetime.now)
    last_checkout: Optional[datetime] = None
    last_release: Optional[datetime] = None
    last_error: Optional[datetime] = None
    last_leak_cleanup: Optional[datetime] = None

    def update_checkout_duration(self, duration_ms: float) -> None:
        """Update checkout duration statistics."""
        self.max_checkout_duration_ms = max(self.max_checkout_duration_ms, duration_ms)
        self.min_checkout_duration_ms = min(self.min_checkout_duration_ms, duration_ms)
        n = self.total_releases
        if n > 0:
            self.avg_checkout_duration_ms = (
                (self.avg_checkout_duration_ms * (n - 1) + duration_ms) / n
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'checkouts': self.total_checkouts,
            'releases': self.total_releases,
            'errors': self.total_errors,
            'timeouts': self.total_timeouts,
            'leaks_detected': self.total_leaks_detected,
            'leaks_recovered': self.total_leaks_recovered,
            'immediate_cleanups': self.total_immediate_cleanups,
            'pool_exhaustions': self.pool_exhaustion_count,
            'circuit_breaker_trips': self.circuit_breaker_trips,
            'avg_checkout_ms': round(self.avg_checkout_duration_ms, 2),
            'max_checkout_ms': round(self.max_checkout_duration_ms, 2),
            'min_checkout_ms': round(self.min_checkout_duration_ms, 2) if self.min_checkout_duration_ms != float('inf') else 0,
            'last_checkout': self.last_checkout.isoformat() if self.last_checkout else None,
            'last_error': self.last_error.isoformat() if self.last_error else None,
            'last_leak_cleanup': self.last_leak_cleanup.isoformat() if self.last_leak_cleanup else None,
        }


# =============================================================================
# Circuit Breaker
# =============================================================================

class CircuitState(Enum):
    CLOSED = auto()      # Normal operation
    OPEN = auto()        # Failing, reject requests
    HALF_OPEN = auto()   # Testing recovery


class CircuitBreaker:
    """
    Circuit breaker for connection failures with async support.

    v82.0 Enhancements:
    - Auto-proxy-start: Automatically attempts to start Cloud SQL proxy when
      transitioning from OPEN to HALF_OPEN
    - Connection refused detection: Tracks if failures are due to proxy not running
    - Intelligent recovery: Only attempts proxy start if failures look like proxy issues
    """

    def __init__(self, config: ConnectionConfig):
        self.config = config
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.last_success_time: Optional[datetime] = None
        self._lock = AsyncLock()

        # v82.0: Track connection refused errors for auto-proxy-start
        self._connection_refused_count = 0
        self._last_proxy_start_attempt: Optional[datetime] = None
        self._proxy_start_cooldown_seconds = 60  # Don't spam proxy starts

    async def record_success_async(self) -> None:
        """Record a successful operation (async)."""
        async with self._lock:
            self._record_success_internal()

    def record_success(self) -> None:
        """Record a successful operation (sync)."""
        with self._lock:
            self._record_success_internal()

    def _record_success_internal(self) -> None:
        self.last_success_time = datetime.now()
        self.success_count += 1
        self.failure_count = 0
        if self.state == CircuitState.HALF_OPEN:
            logger.info("🟢 Circuit breaker CLOSED (recovered)")
            self.state = CircuitState.CLOSED

    async def record_failure_async(self) -> None:
        """Record a failed operation (async)."""
        async with self._lock:
            self._record_failure_internal()

    def record_failure(self) -> None:
        """Record a failed operation (sync)."""
        with self._lock:
            self._record_failure_internal()

    def _record_failure_internal(self, error: Optional[Exception] = None) -> None:
        self.failure_count += 1
        self.last_failure_time = datetime.now()

        # v82.0: Track connection refused errors (proxy not running indicator)
        if error:
            error_str = str(error).lower()
            if "connection refused" in error_str or "errno 61" in error_str:
                self._connection_refused_count += 1

        if self.state == CircuitState.HALF_OPEN:
            logger.warning("🔴 Circuit breaker OPEN (recovery failed)")
            self.state = CircuitState.OPEN
        elif self.failure_count >= self.config.failure_threshold:
            if self.state != CircuitState.OPEN:
                logger.warning(f"🔴 Circuit breaker OPEN ({self.failure_count} failures)")
            self.state = CircuitState.OPEN

    def can_execute(self) -> bool:
        """Check if requests are allowed."""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            if self.last_failure_time:
                elapsed = (datetime.now() - self.last_failure_time).total_seconds()
                if elapsed >= self.config.recovery_timeout_seconds:
                    logger.info("🟡 Circuit breaker HALF-OPEN (testing)")
                    self.state = CircuitState.HALF_OPEN

                    # v82.0: Attempt auto-proxy-start if failures were connection refused
                    if self._connection_refused_count > 0:
                        self._try_auto_start_proxy()

                    return True
            return False

        return self.state == CircuitState.HALF_OPEN

    def _try_auto_start_proxy(self) -> None:
        """
        v82.0: Attempt to auto-start Cloud SQL proxy if it appears to be down.

        This is called when transitioning from OPEN to HALF_OPEN and we've
        detected connection refused errors (indicating proxy not running).

        Uses a cooldown to prevent spamming proxy start attempts.
        """
        try:
            # Check cooldown
            if self._last_proxy_start_attempt:
                elapsed = (datetime.now() - self._last_proxy_start_attempt).total_seconds()
                if elapsed < self._proxy_start_cooldown_seconds:
                    logger.debug(
                        f"[v82.0] Proxy start cooldown active ({elapsed:.0f}s / "
                        f"{self._proxy_start_cooldown_seconds}s)"
                    )
                    return

            self._last_proxy_start_attempt = datetime.now()

            # Import and try to start proxy
            from intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager
            from pathlib import Path
            import json

            # Load config
            config_path = Path.home() / ".jarvis" / "config" / "jarvis_config.json"
            if config_path.exists():
                with open(config_path) as f:
                    config = json.load(f)

                proxy_manager = CloudSQLProxyManager(config)

                if not proxy_manager.is_running():
                    logger.info("[v82.0] 🚀 Auto-starting Cloud SQL proxy...")

                    # Start proxy in background (non-blocking)
                    import asyncio
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # Schedule async start
                        asyncio.create_task(self._async_start_proxy(proxy_manager))
                    else:
                        logger.debug("[v82.0] Event loop not running, skipping auto-start")
                else:
                    logger.debug("[v82.0] Proxy already running, no auto-start needed")
                    # Reset connection refused count since proxy is running
                    self._connection_refused_count = 0

        except Exception as e:
            logger.debug(f"[v82.0] Auto-proxy-start failed: {e}")

    async def _async_start_proxy(self, proxy_manager) -> None:
        """Async helper to start proxy without blocking."""
        try:
            success = await proxy_manager.start(force_restart=False, max_retries=2)
            if success:
                logger.info("[v82.0] ✅ Cloud SQL proxy auto-started successfully")
                self._connection_refused_count = 0
            else:
                logger.warning("[v82.0] ⚠️ Cloud SQL proxy auto-start failed")
        except Exception as e:
            logger.debug(f"[v82.0] Async proxy start error: {e}")

    def get_state_info(self) -> Dict[str, Any]:
        """Get circuit breaker state information."""
        return {
            'state': self.state.name,
            'failure_count': self.failure_count,
            'success_count': self.success_count,
            'last_failure': self.last_failure_time.isoformat() if self.last_failure_time else None,
            'last_success': self.last_success_time.isoformat() if self.last_success_time else None,
            # v82.0 metrics
            'connection_refused_count': self._connection_refused_count,
            'last_proxy_start_attempt': self._last_proxy_start_attempt.isoformat() if self._last_proxy_start_attempt else None,
        }


# =============================================================================
# Leak Detector with Immediate Cleanup
# =============================================================================

class LeakDetector:
    """
    Advanced leak detection with immediate cleanup capability.

    Features:
    - Real-time leak monitoring
    - Immediate cleanup on detection
    - Adaptive monitoring intervals
    - Connection health scoring
    """

    def __init__(self, config: ConnectionConfig, manager: 'CloudSQLConnectionManager'):
        self.config = config
        self.manager = weakref.ref(manager)  # Avoid circular reference
        self._lock = AsyncLock()
        self._last_check: Optional[datetime] = None
        self._consecutive_clean_checks: int = 0
        self._adaptive_interval: float = config.leak_check_interval_seconds

    async def check_and_cleanup(self) -> int:
        """
        Check for leaks and immediately clean them up.

        v3.1: Added retry logic for TLS race conditions.

        Returns:
            Number of leaked connections cleaned up
        """
        manager = self.manager()
        if not manager or not manager.db_config:
            return 0

        cleaned = 0
        async with self._lock:
            max_retries = 2
            conn = None

            for attempt in range(max_retries):
                try:
                    self._last_check = datetime.now()

                    # Create temporary connection for cleanup with retry
                    conn = await asyncio.wait_for(
                        asyncpg.connect(
                            host=manager.db_config["host"],
                            port=manager.db_config["port"],
                            database=manager.db_config["database"],
                            user=manager.db_config["user"],
                            password=manager.db_config["password"],
                        ),
                        timeout=5.0
                    )
                    # Connection successful - break out of retry loop
                    break

                except asyncio.InvalidStateError:
                    # v3.1: TLS race condition - retry with small delay
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.3 * (attempt + 1))
                        continue
                    # Final attempt failed
                    return 0

                except Exception:
                    # Other errors - don't retry
                    return 0

            if conn is None:
                return 0

            try:
                threshold_minutes = self.config.leaked_idle_threshold_minutes

                # Find leaked connections
                leaked = await conn.fetch(f"""
                    SELECT pid, usename, application_name, state,
                           state_change, query, backend_start,
                           EXTRACT(EPOCH FROM (NOW() - state_change)) as idle_seconds
                    FROM pg_stat_activity
                    WHERE datname = $1
                      AND pid <> pg_backend_pid()
                      AND usename = $2
                      AND state = 'idle'
                      AND state_change < NOW() - INTERVAL '{threshold_minutes} minutes'
                    ORDER BY state_change ASC
                """, manager.db_config["database"], manager.db_config["user"])

                if leaked:
                    logger.warning(f"⚠️ Found {len(leaked)} leaked connections (idle > {threshold_minutes} min)")
                    manager.metrics.total_leaks_detected += len(leaked)
                    self._consecutive_clean_checks = 0

                    # IMMEDIATE cleanup
                    for row in leaked:
                        idle_mins = row['idle_seconds'] / 60
                        try:
                            await conn.execute("SELECT pg_terminate_backend($1)", row['pid'])
                            logger.info(f"   ✅ Killed PID {row['pid']} (idle {idle_mins:.1f} min)")
                            manager.metrics.total_leaks_recovered += 1
                            manager.metrics.total_immediate_cleanups += 1
                            cleaned += 1
                        except Exception as e:
                            logger.warning(f"   ⚠️ Failed to kill PID {row['pid']}: {e}")

                    manager.metrics.last_leak_cleanup = datetime.now()

                    # Adaptive: decrease interval when leaks found
                    self._adaptive_interval = max(10.0, self.config.leak_check_interval_seconds * 0.5)
                else:
                    self._consecutive_clean_checks += 1
                    # Adaptive: increase interval when clean
                    if self._consecutive_clean_checks > 5:
                        self._adaptive_interval = min(
                            60.0,
                            self.config.leak_check_interval_seconds * 1.5
                        )

            except asyncio.TimeoutError:
                logger.debug("⏱️ Leak check timeout (proxy not running?)")
            except Exception as e:
                logger.debug(f"⚠️ Leak check failed: {e}")
            finally:
                try:
                    await conn.close()
                except Exception:
                    pass

        return cleaned

    @property
    def next_check_interval(self) -> float:
        """Get the adaptive interval for next check."""
        return self._adaptive_interval


# =============================================================================
# Main Connection Manager
# =============================================================================

class CloudSQLConnectionManager:
    """
    Singleton async-safe CloudSQL connection pool manager with leak detection.

    Features:
    - Automatic leak detection with IMMEDIATE cleanup
    - Circuit breaker for failure isolation
    - Adaptive background cleanup
    - Comprehensive metrics and statistics
    - Signal-aware graceful shutdown
    - Fully async-safe operations
    - Dynamic configuration via environment
    """

    _instance: Optional['CloudSQLConnectionManager'] = None
    _class_lock = threading.Lock()
    _initialized = False
    _initializing = False  # v3.1: Prevent concurrent initialization race

    def __new__(cls):
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self):
        # v3.1: Thread-safe singleton initialization with proper double-checked locking
        # This prevents race conditions where multiple threads could start initialization
        # before _initialized is set to True
        with CloudSQLConnectionManager._class_lock:
            if CloudSQLConnectionManager._initialized:
                return
            if CloudSQLConnectionManager._initializing:
                # Another thread is initializing, wait and return
                return
            # Mark as initializing BEFORE releasing lock to prevent races
            CloudSQLConnectionManager._initializing = True

        try:
            # Core state
            self.pool: Optional[asyncpg.Pool] = None
            self.db_config: Dict[str, Any] = {}
            self._conn_config = ConnectionConfig()
            self.is_shutting_down = False
            self.creation_time: Optional[datetime] = None

            # Async-safe locks
            self._pool_lock = AsyncLock()
            self._checkout_lock = AsyncLock()

            # Legacy compatibility
            self.connection_count = 0
            self.error_count = 0

            # Connection tracking for leak detection
            self._checkouts: Dict[int, ConnectionCheckout] = {}
            self._checkout_counter = 0
            self._active_connections: Set[int] = set()

            # Metrics
            self.metrics = ConnectionMetrics()

            # Circuit breaker
            self._circuit_breaker: Optional[CircuitBreaker] = None

            # Leak detector
            self._leak_detector: Optional[LeakDetector] = None

            # Background tasks
            self._cleanup_task: Optional[asyncio.Task] = None
            self._leak_monitor_task: Optional[asyncio.Task] = None
            self._health_check_task: Optional[asyncio.Task] = None

            # Callbacks
            self._on_leak_callbacks: List[Callable] = []
            self._on_error_callbacks: List[Callable] = []

            # Startup mode: suppress connection errors until proxy is confirmed ready
            # This prevents noisy logs during early startup when proxy hasn't started yet
            self._startup_mode = True
            self._proxy_ready = False
            self._start_time = time.time()
            self._startup_grace_period = 60  # seconds before logging connection errors

            # v5.5: Store last error for diagnostic purposes
            self.last_error: Optional[str] = None
            self.last_error_time: Optional[datetime] = None

            # v3.1: Connection initialization lock to prevent TLS race conditions
            # asyncpg can hit InvalidStateError when multiple connections try TLS upgrade simultaneously
            self._conn_init_lock = asyncio.Lock()

            self._register_shutdown_handlers()
            CloudSQLConnectionManager._initialized = True
            logger.info("🔧 CloudSQL Connection Manager v3.1 initialized")
        except Exception as e:
            # Reset initialization flags on failure so retry is possible
            CloudSQLConnectionManager._initializing = False
            raise

    def _register_shutdown_handlers(self):
        """Register cleanup handlers for graceful shutdown."""
        atexit.register(self._sync_shutdown)
        logger.debug("✅ Shutdown handlers registered")

    def _sync_shutdown(self):
        """Synchronous shutdown handler for atexit."""
        if self.pool and not self.is_shutting_down:
            logger.info("🛑 atexit: Synchronous shutdown...")
            self.is_shutting_down = True
            try:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_closed():
                        raise RuntimeError("Loop closed")
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    needs_close = True
                else:
                    needs_close = False

                loop.run_until_complete(self.shutdown())
                if needs_close:
                    loop.close()
            except Exception as e:
                logger.warning(f"⚠️ Async shutdown failed: {e}")
                self.pool = None

    async def initialize(
        self,
        host: str = "127.0.0.1",
        port: int = 5432,
        database: str = "jarvis_learning",
        user: str = "jarvis",
        password: Optional[str] = None,
        max_connections: int = 3,
        force_reinit: bool = False,
        config: Optional[ConnectionConfig] = None
    ) -> bool:
        """
        Initialize connection pool with leak detection and circuit breaker.

        Args:
            host: Database host (127.0.0.1 for proxy)
            port: Database port
            database: Database name
            user: Database user
            password: Database password
            max_connections: Max pool size (default 3 for db-f1-micro)
            force_reinit: Force re-initialization
            config: Optional ConnectionConfig for advanced settings

        Returns:
            True if pool is ready
        """
        async with self._pool_lock:
            if self.pool and not force_reinit:
                logger.info("♻️ Reusing existing connection pool")
                return True

            if self.pool and force_reinit:
                logger.info("🔄 Force re-init: closing existing pool...")
                await self._close_pool_internal()

            if not ASYNCPG_AVAILABLE:
                logger.error("❌ asyncpg not available")
                return False

            if not password:
                logger.error("❌ Database password required")
                return False

            # Apply config
            if config:
                self._conn_config = config
            else:
                # Reload from environment
                self._conn_config.reload_from_env()

            self._conn_config.max_connections = max_connections

            # Store DB config
            self.db_config = {
                "host": host,
                "port": port,
                "database": database,
                "user": user,
                "password": password,
                "max_connections": max_connections
            }

            # Initialize circuit breaker
            self._circuit_breaker = CircuitBreaker(self._conn_config)

            # Initialize leak detector
            self._leak_detector = LeakDetector(self._conn_config, self)

            try:
                logger.info(f"🔌 Creating CloudSQL connection pool (max={max_connections})...")
                logger.info(f"   Host: {host}:{port}, Database: {database}, User: {user}")
                logger.info(f"   Config: idle_timeout={self._conn_config.max_idle_time_seconds}s, "
                           f"leak_threshold={self._conn_config.leaked_idle_threshold_minutes}min")

                # IMMEDIATE cleanup of leaked connections before creating pool
                await self._immediate_leak_cleanup()

                # v3.1: Create pool with retry logic and serialized connection setup
                # This prevents TLS race conditions (InvalidStateError) that occur when
                # multiple connections try to establish TLS simultaneously
                async def create_pool_with_retry():
                    """Create pool with retry on transient errors."""
                    max_retries = 3
                    last_error = None

                    for attempt in range(max_retries):
                        try:
                            # v3.1: Use setup callback to serialize connection initialization
                            # This helps prevent TLS race conditions in asyncpg
                            async def connection_setup(conn):
                                """Called when a new connection is established."""
                                # Small delay to prevent thundering herd during pool init
                                if attempt > 0:
                                    await asyncio.sleep(0.1 * attempt)

                            pool = await asyncpg.create_pool(
                                host=host,
                                port=port,
                                database=database,
                                user=user,
                                password=password,
                                min_size=self._conn_config.min_connections,
                                max_size=max_connections,
                                timeout=self._conn_config.connection_timeout,
                                command_timeout=self._conn_config.query_timeout,
                                max_queries=self._conn_config.max_queries_per_connection,
                                max_inactive_connection_lifetime=self._conn_config.max_idle_time_seconds,
                                setup=connection_setup,
                            )
                            return pool

                        except (asyncio.InvalidStateError, OSError) as e:
                            # v3.1: TLS race condition or network error - retry with backoff
                            last_error = e
                            if attempt < max_retries - 1:
                                delay = (attempt + 1) * 0.5  # 0.5s, 1.0s, 1.5s
                                logger.warning(
                                    f"⚠️ Pool creation attempt {attempt + 1}/{max_retries} failed: {e}. "
                                    f"Retrying in {delay:.1f}s..."
                                )
                                await asyncio.sleep(delay)
                            else:
                                raise

                        except Exception as e:
                            # Non-retryable error
                            raise

                    if last_error:
                        raise last_error

                # Create pool
                self.pool = await asyncio.wait_for(
                    create_pool_with_retry(),
                    timeout=self._conn_config.pool_creation_timeout
                )

                # Validate
                async with self.pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")

                self.creation_time = datetime.now()
                self.error_count = 0
                self.metrics = ConnectionMetrics()
                self.is_shutting_down = False

                logger.info(f"✅ Connection pool created successfully")
                logger.info(f"   Pool: {self.pool.get_size()} total, {self.pool.get_idle_size()} idle")

                # Start background tasks
                if self._conn_config.enable_background_cleanup:
                    await self._start_background_tasks()

                return True

            except asyncio.TimeoutError:
                logger.error("⏱️ Connection pool creation timeout")
                logger.error("   Causes: proxy not running, bad credentials, network issues")
                self.pool = None
                self.last_error = "Connection timeout"
                self.last_error_time = datetime.now()
                return False

            except asyncio.InvalidStateError as e:
                # v3.1: asyncpg TLS race condition - this should have been retried above
                logger.error(f"❌ TLS race condition after retries: {e}")
                logger.error("   This is an asyncpg bug when multiple connections attempt TLS simultaneously")
                logger.error("   Try reducing min_connections or increasing connection timeouts")
                self.pool = None
                self.last_error = f"TLS race condition: {e}"
                self.last_error_time = datetime.now()
                return False

            except Exception as e:
                error_str = str(e)
                self.last_error = error_str
                self.last_error_time = datetime.now()

                # v5.5: Detect specific error types for better diagnostics
                if "invalid state" in error_str.lower():
                    # v3.1: asyncpg TLS race condition
                    logger.error(f"❌ TLS race condition: {e}")
                    logger.error("   asyncpg hit InvalidStateError during connection initialization")
                    self.pool = None
                    self.error_count += 1
                    return False
                elif "password authentication failed" in error_str.lower():
                    logger.error(f"❌ Failed to create pool: {e}")
                    logger.error("")
                    logger.error("   🔐 CREDENTIAL MISMATCH DETECTED")
                    logger.error("   The password from GCP Secret Manager doesn't match Cloud SQL.")
                    logger.error("")
                    logger.error("   To fix this, either:")
                    logger.error("   A) Update Secret Manager with the correct password:")
                    logger.error("      echo 'YOUR_PASSWORD' | gcloud secrets versions add jarvis-db-password --data-file=-")
                    logger.error("")
                    logger.error("   B) Reset the Cloud SQL user password to match:")
                    logger.error(f"      gcloud sql users set-password {user} \\")
                    logger.error(f"        --instance=jarvis-learning-db --password='YOUR_PASSWORD'")
                    logger.error("")
                elif "connection refused" in error_str.lower():
                    logger.error(f"❌ Failed to create pool: Connection refused")
                    logger.error("   Cloud SQL proxy may not be running on port 5432")
                elif "does not exist" in error_str.lower():
                    logger.error(f"❌ Failed to create pool: {e}")
                    logger.error(f"   Database '{database}' may not exist on the Cloud SQL instance")
                else:
                    logger.error(f"❌ Failed to create pool: {e}")

                self.pool = None
                self.error_count += 1
                return False

    async def _immediate_leak_cleanup(self) -> int:
        """
        Immediately cleanup all leaked connections.

        v3.1: Added retry logic to handle TLS race conditions on direct connections.
        """
        if self._leak_detector:
            return await self._leak_detector.check_and_cleanup()

        # Fallback if leak detector not initialized
        if not self.db_config:
            return 0

        cleaned = 0
        max_retries = 2

        for attempt in range(max_retries):
            try:
                conn = await asyncio.wait_for(
                    asyncpg.connect(
                        host=self.db_config["host"],
                        port=self.db_config["port"],
                        database=self.db_config["database"],
                        user=self.db_config["user"],
                        password=self.db_config["password"],
                    ),
                    timeout=5.0
                )

                try:
                    threshold = self._conn_config.leaked_idle_threshold_minutes
                    leaked = await conn.fetch(f"""
                        SELECT pid FROM pg_stat_activity
                        WHERE datname = $1 AND pid <> pg_backend_pid()
                          AND usename = $2 AND state = 'idle'
                          AND state_change < NOW() - INTERVAL '{threshold} minutes'
                    """, self.db_config["database"], self.db_config["user"])

                    for row in leaked:
                        try:
                            await conn.execute("SELECT pg_terminate_backend($1)", row['pid'])
                            cleaned += 1
                        except Exception:
                            pass
                finally:
                    await conn.close()

                # Success - break out of retry loop
                break

            except asyncio.InvalidStateError:
                # v3.1: TLS race condition - retry with small delay
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                # Final attempt failed
                pass

            except Exception:
                # Other errors - don't retry
                pass

        return cleaned

    async def _start_background_tasks(self) -> None:
        """Start background monitoring tasks."""
        # Cancel existing tasks
        for task in [self._cleanup_task, self._leak_monitor_task, self._health_check_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Start new tasks
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="cloudsql_cleanup"
        )
        self._leak_monitor_task = asyncio.create_task(
            self._leak_monitor_loop(),
            name="cloudsql_leak_monitor"
        )
        self._health_check_task = asyncio.create_task(
            self._health_check_loop(),
            name="cloudsql_health_check"
        )

        logger.debug("🔄 Background tasks started")

    async def _cleanup_loop(self) -> None:
        """Background task for periodic cleanup with adaptive intervals."""
        while not self.is_shutting_down:
            try:
                interval = (
                    self._leak_detector.next_check_interval
                    if self._leak_detector
                    else self._conn_config.cleanup_interval_seconds
                )
                await asyncio.sleep(interval)

                if self.is_shutting_down:
                    break

                # Periodic leak cleanup
                if self.pool and self.db_config and self._leak_detector:
                    await self._leak_detector.check_and_cleanup()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Cleanup loop error: {e}")
                await asyncio.sleep(10.0)

    async def _leak_monitor_loop(self) -> None:
        """Monitor tracked checkouts for potential leaks."""
        while not self.is_shutting_down:
            try:
                await asyncio.sleep(self._conn_config.leak_check_interval_seconds)
                if self.is_shutting_down:
                    break

                await self._check_checkout_leaks()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Leak monitor error: {e}")
                await asyncio.sleep(5.0)

    async def _check_checkout_leaks(self) -> None:
        """Check tracked checkouts for leaks and handle them."""
        async with self._checkout_lock:
            leaked_ids = []

            for checkout_id, checkout in self._checkouts.items():
                if checkout.released:
                    continue

                age = checkout.age_seconds

                # Warning for long-held connections
                if age > self._conn_config.checkout_warning_seconds:
                    if age <= self._conn_config.checkout_timeout_seconds:
                        logger.warning(
                            f"⚠️ Connection held {age:.0f}s (checkout #{checkout_id})\n"
                            f"   Location: {checkout.caller_info}"
                        )

                # Force mark as leaked if held too long
                if age > self._conn_config.checkout_timeout_seconds:
                    logger.error(
                        f"🚨 LEAK: Connection #{checkout_id} held {age:.0f}s - forcing release"
                    )
                    leaked_ids.append(checkout_id)
                    self.metrics.total_leaks_detected += 1

            # Handle leaked connections
            for checkout_id in leaked_ids:
                self._checkouts[checkout_id].released = True
                self._checkouts[checkout_id].release_time = datetime.now()
                self.metrics.total_leaks_recovered += 1

                # Fire callbacks
                await self._fire_leak_callbacks(checkout_id)

            # Cleanup tracking for old checkouts
            await self._cleanup_old_checkouts_async()

    async def _fire_leak_callbacks(self, checkout_id: int) -> None:
        """Fire registered leak callbacks."""
        for callback in self._on_leak_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(checkout_id)
                else:
                    callback(checkout_id)
            except Exception as e:
                logger.debug(f"Leak callback error: {e}")

    async def _health_check_loop(self) -> None:
        """Periodic health check of the connection pool with intelligent proxy detection."""
        # v5.0: Get proxy detector for intelligent health checking
        proxy_detector = get_proxy_detector() if PROXY_DETECTOR_AVAILABLE else None

        while not self.is_shutting_down:
            try:
                # v5.0: Use intelligent delay based on proxy status
                if proxy_detector and not self.pool:
                    delay = proxy_detector.get_next_retry_delay()
                else:
                    delay = 60.0  # Standard 60-second health check when pool exists

                await asyncio.sleep(delay)
                if self.is_shutting_down:
                    break

                # v5.0: Check proxy availability before attempting health check
                if proxy_detector:
                    proxy_status, proxy_info = await proxy_detector.detect_proxy()

                    if proxy_status == ProxyStatus.UNAVAILABLE:
                        # Proxy not available - skip health check silently
                        # (already logged by hybrid_database_sync)
                        if not proxy_detector.should_retry():
                            # Local dev mode - stop checking entirely
                            continue
                        # Otherwise, just skip this iteration (will retry with backoff)
                        continue

                if self.pool:
                    try:
                        async with self.pool.acquire() as conn:
                            await asyncio.wait_for(
                                conn.fetchval("SELECT 1"),
                                timeout=5.0
                            )
                        self.metrics.healthy_connections += 1
                    except Exception as e:
                        self.metrics.unhealthy_connections += 1
                        # v5.0: Only log warning if proxy is supposed to be available
                        if proxy_detector:
                            proxy_status, _ = await proxy_detector.detect_proxy()
                            if proxy_status == ProxyStatus.AVAILABLE:
                                logger.warning("⚠️ Health check failed - pool may be degraded")
                            # If proxy unavailable, don't spam warnings
                        else:
                            # No proxy detector - use legacy behavior
                            if not self._should_suppress_error():
                                logger.warning("⚠️ Health check failed - pool may be degraded")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Health check error: {e}")
                await asyncio.sleep(30.0)

    @asynccontextmanager
    async def connection(self):
        """
        Acquire connection with leak tracking, circuit breaker, and ML-powered rate limiting.

        v10.0: Integrates with IntelligentRateOrchestrator for:
        - Predictive rate limit forecasting
        - Adaptive throttling based on usage patterns
        - Priority-based request scheduling

        Usage:
            async with manager.connection() as conn:
                result = await conn.fetchval("SELECT 1")
        """
        if not self.pool:
            raise RuntimeError("Connection pool not initialized")

        if self.is_shutting_down:
            raise RuntimeError("Connection manager is shutting down")

        # Start timing for metrics
        start_time = time.time()

        # Check circuit breaker
        if self._circuit_breaker and not self._circuit_breaker.can_execute():
            raise RuntimeError(
                f"Circuit breaker OPEN - retry in {self._conn_config.recovery_timeout_seconds}s"
            )

        # v10.5: Enhanced intelligent rate limiting with adaptive backoff
        if INTELLIGENT_RATE_ORCHESTRATOR_AVAILABLE:
            try:
                orchestrator = await get_rate_orchestrator()

                # Retry with adaptive exponential backoff when throttled
                max_retries = 10  # Increased from 5 to 10 for better resilience
                retry_count = 0
                base_delay = 0.1  # Start with 100ms (reduced from 200ms for faster initial retries)

                while retry_count < max_retries:
                    acquired, reason = await orchestrator.acquire(
                        RateServiceType.CLOUDSQL_CONNECTIONS,
                        RateOpType.QUERY,
                        RequestPriority.NORMAL,
                    )

                    if acquired:
                        # Successfully acquired rate limit token
                        if retry_count > 0:
                            logger.debug(
                                f"⚡ CloudSQL rate limit acquired after {retry_count} "
                                f"retries ({(time.time() - start_time):.2f}s)"
                            )
                        break

                    # Throttled - calculate adaptive exponential backoff
                    retry_count += 1
                    if retry_count < max_retries:
                        # Adaptive backoff: slower growth for early retries, faster later
                        # 100ms, 200ms, 400ms, 600ms, 900ms, 1.3s, 1.8s, 2.4s, 3.2s, 4.2s
                        if retry_count <= 3:
                            # Linear growth for first 3 retries (fast initial attempts)
                            delay = base_delay * retry_count
                        else:
                            # Exponential growth after 3 retries (back off more aggressively)
                            delay = min(base_delay * (1.5 ** retry_count), 5.0)

                        # Only log every 3rd retry to reduce noise
                        if retry_count % 3 == 0 or retry_count == 1:
                            logger.debug(
                                f"⚡ CloudSQL connection throttled: {reason} "
                                f"(retry {retry_count}/{max_retries}, waiting {delay:.2f}s)"
                            )

                        await asyncio.sleep(delay)
                    else:
                        # Max retries reached - proceed anyway but log as info (not warning)
                        # This is expected behavior during high load, not an error
                        logger.info(
                            f"⚡ CloudSQL rate orchestrator busy after {max_retries} retries. "
                            f"Proceeding with database query (rate limits managed by orchestrator)."
                        )

            except Exception as e:
                logger.debug(f"Rate orchestrator check failed: {e}")

        # Initialize connection tracking
        conn = None
        checkout_id = None

        try:
            # Acquire connection
            conn = await asyncio.wait_for(
                self.pool.acquire(),
                timeout=self._conn_config.connection_timeout
            )

            # Track checkout
            async with self._checkout_lock:
                self._checkout_counter += 1
                checkout_id = self._checkout_counter
                caller_info = self._get_caller_info()
                self._checkouts[checkout_id] = ConnectionCheckout(
                    checkout_id=checkout_id,
                    checkout_time=datetime.now(),
                    stack_trace=traceback.format_stack()[-5:-2],
                    caller_info=caller_info,
                )
                self._active_connections.add(checkout_id)

            # Update metrics
            self.connection_count += 1
            self.metrics.total_checkouts += 1
            self.metrics.last_checkout = datetime.now()

            latency_ms = (time.time() - start_time) * 1000
            logger.debug(f"✅ Connection #{checkout_id} acquired ({latency_ms:.1f}ms) from {caller_info}")

            # Record success for circuit breaker
            if self._circuit_breaker:
                await self._circuit_breaker.record_success_async()

            yield conn

        except asyncio.TimeoutError:
            # Suppress timeout errors during startup mode (before proxy is ready)
            if not self._should_suppress_error():
                logger.error("⏱️ Connection timeout - pool exhausted")
            else:
                logger.debug("⏱️ Connection timeout during startup (proxy not ready yet)")
            self.error_count += 1
            self.metrics.total_timeouts += 1
            self.metrics.pool_exhaustion_count += 1
            if self._circuit_breaker:
                await self._circuit_breaker.record_failure_async()

            # Aggressive cleanup on pool exhaustion
            if self._conn_config.aggressive_cleanup_on_leak:
                asyncio.create_task(self._immediate_leak_cleanup())

            raise

        except Exception as e:
            # Suppress connection errors during startup mode (before proxy is ready)
            if str(e) and str(e) != "0":
                if not self._should_suppress_error():
                    logger.error(f"❌ Connection error: {e}")
                else:
                    logger.debug(f"⏳ Connection error during startup (proxy not ready yet): {e}")
            self.error_count += 1
            self.metrics.total_errors += 1
            self.metrics.last_error = datetime.now()
            if self._circuit_breaker:
                await self._circuit_breaker.record_failure_async()

            # Fire error callbacks
            for callback in self._on_error_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(e)
                    else:
                        callback(e)
                except Exception:
                    pass

            raise

        finally:
            # ALWAYS release and track
            if conn:
                try:
                    duration_ms = (time.time() - start_time) * 1000

                    # Update metrics
                    self.metrics.total_releases += 1
                    self.metrics.last_release = datetime.now()
                    self.metrics.update_checkout_duration(duration_ms)

                    # Mark checkout as released
                    async with self._checkout_lock:
                        if checkout_id:
                            if checkout_id in self._checkouts:
                                self._checkouts[checkout_id].released = True
                                self._checkouts[checkout_id].release_time = datetime.now()
                            self._active_connections.discard(checkout_id)

                    # Release to pool with CancelledError protection
                    # v82.1: During shutdown, asyncio.shield() can be interrupted
                    # causing CancelledError in pool.release(). We catch this and
                    # let the connection be cleaned up by the pool's shutdown.
                    try:
                        await self.pool.release(conn)
                        logger.debug(f"♻️ Connection #{checkout_id} released ({duration_ms:.1f}ms)")
                    except asyncio.CancelledError:
                        # Connection will be cleaned up during pool shutdown
                        logger.debug(f"⚠️ Connection #{checkout_id} release cancelled (shutdown in progress)")

                except asyncio.CancelledError:
                    # v82.1: Outer CancelledError - don't log as error
                    logger.debug(f"⚠️ Connection operation cancelled for #{checkout_id} (shutdown)")
                except Exception as e:
                    logger.error(f"❌ Failed to release connection: {e}")

    def _get_caller_info(self) -> str:
        """Get caller stack trace for debugging."""
        try:
            stack = traceback.extract_stack()
            relevant = [f for f in stack[:-4] if 'cloud_sql' not in f.filename.lower()]
            if relevant:
                frame = relevant[-1]
                return f"{frame.filename.split('/')[-1]}:{frame.lineno} in {frame.name}"
            return "unknown"
        except Exception:
            return "unknown"

    async def _cleanup_old_checkouts_async(self) -> None:
        """Remove old released checkouts from tracking (async)."""
        cutoff = datetime.now() - timedelta(seconds=60)
        to_remove = [
            cid for cid, c in self._checkouts.items()
            if c.released and c.release_time and c.release_time < cutoff
        ]
        for cid in to_remove[:100]:  # Limit cleanup batch size
            del self._checkouts[cid]

    def _cleanup_old_checkouts(self) -> None:
        """Remove old released checkouts from tracking (sync)."""
        cutoff = datetime.now() - timedelta(seconds=60)
        to_remove = [
            cid for cid, c in self._checkouts.items()
            if c.released and c.release_time and c.release_time < cutoff
        ]
        for cid in to_remove[:100]:
            del self._checkouts[cid]

    async def execute(self, query: str, *args, timeout: Optional[float] = None, retry: bool = True) -> str:
        """
        Execute query and return result with automatic retry on connection errors.

        Args:
            query: SQL query string
            *args: Query parameters
            timeout: Query timeout in seconds
            retry: Whether to retry on connection errors (default True)

        Returns:
            Query result status string
        """
        async def _execute():
            timeout_val = timeout or self._conn_config.query_timeout
            async with self.connection() as conn:
                return await asyncio.wait_for(
                    conn.execute(query, *args),
                    timeout=timeout_val
                )

        if retry:
            return await self._execute_with_retry(_execute, "execute")
        return await _execute()

    async def fetch(self, query: str, *args, timeout: Optional[float] = None, retry: bool = True) -> List[asyncpg.Record]:
        """
        Fetch multiple rows with automatic retry on connection errors.

        Args:
            query: SQL query string
            *args: Query parameters
            timeout: Query timeout in seconds
            retry: Whether to retry on connection errors (default True)

        Returns:
            List of database records
        """
        async def _fetch():
            timeout_val = timeout or self._conn_config.query_timeout
            async with self.connection() as conn:
                return await asyncio.wait_for(
                    conn.fetch(query, *args),
                    timeout=timeout_val
                )

        if retry:
            return await self._execute_with_retry(_fetch, "fetch")
        return await _fetch()

    async def fetchrow(self, query: str, *args, timeout: Optional[float] = None, retry: bool = True) -> Optional[asyncpg.Record]:
        """
        Fetch single row with automatic retry on connection errors.

        Args:
            query: SQL query string
            *args: Query parameters
            timeout: Query timeout in seconds
            retry: Whether to retry on connection errors (default True)

        Returns:
            Single database record or None
        """
        async def _fetchrow():
            timeout_val = timeout or self._conn_config.query_timeout
            async with self.connection() as conn:
                return await asyncio.wait_for(
                    conn.fetchrow(query, *args),
                    timeout=timeout_val
                )

        if retry:
            return await self._execute_with_retry(_fetchrow, "fetchrow")
        return await _fetchrow()

    async def fetchval(self, query: str, *args, timeout: Optional[float] = None, retry: bool = True) -> Any:
        """
        Fetch single value with automatic retry on connection errors.

        Args:
            query: SQL query string
            *args: Query parameters
            timeout: Query timeout in seconds
            retry: Whether to retry on connection errors (default True)

        Returns:
            Single value from query result
        """
        async def _fetchval():
            timeout_val = timeout or self._conn_config.query_timeout
            async with self.connection() as conn:
                return await asyncio.wait_for(
                    conn.fetchval(query, *args),
                    timeout=timeout_val
                )

        if retry:
            return await self._execute_with_retry(_fetchval, "fetchval")
        return await _fetchval()

    async def _execute_with_retry(self, operation: Callable, operation_name: str, max_retries: int = 3) -> Any:
        """
        Execute an operation with automatic retry on connection errors.

        This method handles transient connection failures by:
        1. Detecting connection-related errors
        2. Attempting pool recovery if needed
        3. Retrying with exponential backoff

        Args:
            operation: Async callable to execute
            operation_name: Name of operation for logging
            max_retries: Maximum retry attempts

        Returns:
            Result of the operation

        Raises:
            Exception: If all retries exhausted
        """
        import random

        last_exception = None
        base_delay = 0.5

        for attempt in range(max_retries + 1):
            try:
                return await operation()

            except Exception as e:
                last_exception = e
                error_msg = str(e).lower()

                # Check if this is a retryable connection error
                if not is_connection_error(e):
                    # Not a connection error - don't retry
                    raise

                if attempt >= max_retries:
                    logger.error(
                        f"❌ {operation_name} failed after {attempt + 1} attempts: {e}"
                    )
                    raise

                # Calculate exponential backoff with jitter
                delay = min(base_delay * (2 ** attempt), 10.0)
                delay = delay * (0.5 + random.random())

                logger.warning(
                    f"⚠️ {operation_name} attempt {attempt + 1}/{max_retries + 1} failed: {e}. "
                    f"Retrying in {delay:.2f}s..."
                )

                # Try to recover the pool if it seems degraded
                if "released" in error_msg or "closed" in error_msg:
                    await self._try_pool_recovery()

                await asyncio.sleep(delay)

        if last_exception:
            raise last_exception

    async def _try_pool_recovery(self) -> bool:
        """
        Attempt to recover the connection pool after errors.

        This method:
        1. Cleans up leaked connections
        2. Attempts to validate pool health
        3. Recreates pool if necessary

        Returns:
            True if recovery successful
        """
        try:
            logger.info("🔄 Attempting pool recovery...")

            # First, try to clean up leaked connections
            cleaned = await self._immediate_leak_cleanup()
            if cleaned > 0:
                logger.info(f"   Cleaned {cleaned} leaked connections")

            # Check if pool is still valid
            if self.pool:
                try:
                    # Python 3.9 compatible (asyncio.timeout is 3.11+)
                    async def _validate_pool():
                        async with self.pool.acquire() as conn:
                            await conn.fetchval("SELECT 1")
                    await asyncio.wait_for(_validate_pool(), timeout=5.0)
                    logger.info("   ✅ Pool is healthy after cleanup")
                    return True
                except Exception as e:
                    logger.warning(f"   Pool validation failed: {e}")

            # Pool seems broken - attempt reinit
            if self.db_config and self.db_config.get("password"):
                logger.info("   Attempting pool reinitialization...")
                success = await self.initialize(
                    host=self.db_config.get("host", "127.0.0.1"),
                    port=self.db_config.get("port", 5432),
                    database=self.db_config.get("database", "jarvis_learning"),
                    user=self.db_config.get("user", "jarvis"),
                    password=self.db_config["password"],
                    max_connections=self.db_config.get("max_connections", 3),
                    force_reinit=True
                )
                if success:
                    logger.info("   ✅ Pool reinitialized successfully")
                    return True
                else:
                    logger.error("   ❌ Pool reinitialization failed")
                    return False

            return False

        except Exception as e:
            logger.error(f"❌ Pool recovery failed: {e}")
            return False

    async def _close_pool_internal(self) -> None:
        """Close connection pool gracefully (internal, no lock)."""
        if self.pool:
            try:
                logger.info("🔌 Closing connection pool...")

                # Cancel background tasks
                for task in [self._cleanup_task, self._leak_monitor_task, self._health_check_task]:
                    if task and not task.done():
                        task.cancel()
                        try:
                            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass

                # Close pool
                await asyncio.wait_for(self.pool.close(), timeout=10.0)
                logger.info("✅ Connection pool closed")

            except asyncio.TimeoutError:
                logger.warning("⏱️ Pool close timeout - terminating")
                try:
                    self.pool.terminate()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"❌ Error closing pool: {e}")
            finally:
                self.pool = None

    async def shutdown(self) -> None:
        """Graceful shutdown with metrics reporting."""
        if self.is_shutting_down and not self.pool:
            return

        self.is_shutting_down = True
        logger.info("🛑 Shutting down CloudSQL Connection Manager...")

        # Log metrics
        self._log_final_metrics()

        # Check for unreleased connections
        async with self._checkout_lock:
            unreleased = [c for c in self._checkouts.values() if not c.released]
            if unreleased:
                logger.warning(f"⚠️ {len(unreleased)} connections not released at shutdown:")
                for c in unreleased[:5]:
                    logger.warning(f"   - #{c.checkout_id}: {c.caller_info} (held {c.age_seconds:.0f}s)")

        async with self._pool_lock:
            await self._close_pool_internal()

        logger.info("✅ Shutdown complete")

    def _log_final_metrics(self) -> None:
        """Log final metrics summary."""
        logger.info("📊 Connection Pool Metrics:")
        logger.info(f"   Checkouts: {self.metrics.total_checkouts}")
        logger.info(f"   Releases: {self.metrics.total_releases}")
        logger.info(f"   Errors: {self.metrics.total_errors}")
        logger.info(f"   Timeouts: {self.metrics.total_timeouts}")
        logger.info(f"   Leaks detected: {self.metrics.total_leaks_detected}")
        logger.info(f"   Leaks recovered: {self.metrics.total_leaks_recovered}")
        logger.info(f"   Immediate cleanups: {self.metrics.total_immediate_cleanups}")
        if self.metrics.total_releases > 0:
            logger.info(f"   Avg checkout: {self.metrics.avg_checkout_duration_ms:.1f}ms")
            logger.info(f"   Max checkout: {self.metrics.max_checkout_duration_ms:.1f}ms")
        if self.creation_time:
            uptime = (datetime.now() - self.creation_time).total_seconds()
            logger.info(f"   Uptime: {uptime:.1f}s")

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive statistics."""
        active_checkouts = [c.to_dict() for c in self._checkouts.values() if not c.released]

        return {
            "status": "running" if self.pool and not self.is_shutting_down else "stopped",
            "pool_size": self.pool.get_size() if self.pool else 0,
            "idle_size": self.pool.get_idle_size() if self.pool else 0,
            "max_size": self.db_config.get("max_connections", 0),
            "connection_count": self.connection_count,
            "error_count": self.error_count,
            "creation_time": self.creation_time.isoformat() if self.creation_time else None,
            "uptime_seconds": (datetime.now() - self.creation_time).total_seconds() if self.creation_time else 0,
            "active_checkouts": len(active_checkouts),
            "active_checkout_details": active_checkouts[:10],  # Limit for readability
            "metrics": self.metrics.to_dict(),
            "circuit_breaker": self._circuit_breaker.get_state_info() if self._circuit_breaker else None,
            "config": {
                "max_idle_time": self._conn_config.max_idle_time_seconds,
                "leak_threshold_min": self._conn_config.leaked_idle_threshold_minutes,
                "checkout_warning_sec": self._conn_config.checkout_warning_seconds,
                "aggressive_cleanup": self._conn_config.aggressive_cleanup_on_leak,
            },
        }

    async def get_stats_async(self) -> Dict[str, Any]:
        """Get comprehensive statistics (async version)."""
        return self.get_stats()

    async def get_pool(self) -> Optional["asyncpg.Pool"]:
        """
        Get the underlying connection pool.
        
        This method provides async-compatible access to the raw pool for
        advanced use cases like health checks that need direct pool access.
        
        Returns:
            The asyncpg pool if initialized, None otherwise
        """
        if not self.pool:
            logger.warning("get_pool() called but pool is not initialized")
            return None
        if self.is_shutting_down:
            logger.warning("get_pool() called but manager is shutting down")
            return None
        return self.pool

    @property
    def is_initialized(self) -> bool:
        return self.pool is not None and not self.is_shutting_down

    @property
    def config(self) -> Dict[str, Any]:
        """Legacy config dict for backward compatibility."""
        return self.db_config

    @config.setter
    def config(self, value: Dict[str, Any]) -> None:
        """Allow setting config for backward compatibility."""
        if isinstance(value, dict):
            self.db_config = value

    def on_leak_detected(self, callback: Callable) -> None:
        """Register callback for leak detection events."""
        self._on_leak_callbacks.append(callback)

    def on_error(self, callback: Callable) -> None:
        """Register callback for error events."""
        self._on_error_callbacks.append(callback)

    async def force_cleanup_leaks(self) -> int:
        """Force cleanup of all leaked connections. Returns count cleaned."""
        if self._leak_detector:
            return await self._leak_detector.check_and_cleanup()
        return await self._immediate_leak_cleanup()

    async def reload_config(self) -> None:
        """Reload configuration from environment variables."""
        self._conn_config.reload_from_env()
        logger.info("🔄 Configuration reloaded")

    def set_proxy_ready(self, ready: bool = True) -> None:
        """
        Signal that the Cloud SQL proxy is ready for connections.
        
        Call this after the proxy has been started to enable connection
        error logging. During startup mode, connection errors are suppressed
        to avoid noisy logs before the proxy is ready.
        """
        self._proxy_ready = ready
        self._startup_mode = not ready
        if ready:
            logger.info("✅ CloudSQL proxy marked as ready - connection error logging enabled")

    @property
    def is_proxy_ready(self) -> bool:
        """Check if the Cloud SQL proxy has been signaled as ready."""
        return self._proxy_ready

    def _should_suppress_error(self) -> bool:
        """Check if connection errors should be suppressed (during startup)."""
        if not self._startup_mode:
            return False
        if self._proxy_ready:
            return False
        # Check if we've exceeded the grace period
        elapsed = time.time() - self._start_time
        if elapsed >= self._startup_grace_period:
            self._startup_mode = False
            return False
        return True


# =============================================================================
# Global Singleton Accessor
# =============================================================================

_manager: Optional[CloudSQLConnectionManager] = None
_manager_lock = threading.Lock()


def get_connection_manager() -> CloudSQLConnectionManager:
    """Get singleton connection manager instance."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = CloudSQLConnectionManager()
        return _manager


async def get_connection_manager_async() -> CloudSQLConnectionManager:
    """Get singleton connection manager instance (async version)."""
    return get_connection_manager()
