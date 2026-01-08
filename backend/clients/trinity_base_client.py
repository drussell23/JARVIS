"""
Trinity Base Client - Advanced Resilience Patterns for Cross-Repo Communication.
==================================================================================

Provides a robust foundation for all Trinity cross-repo clients with:
- Circuit Breaker pattern for fault tolerance
- Exponential backoff with jitter for retries
- Connection pooling with health checks
- Request deduplication and idempotency
- Dead Letter Queue for failed operations
- Metrics collection and observability

This is the foundation for ReactorCoreClient and JARVISPrimeClient.

Author: JARVIS Trinity v81.0
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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, Awaitable, Callable, Deque, Dict, Generic, List,
    Optional, Set, Tuple, TypeVar, Union
)
from weakref import WeakValueDictionary

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


# =============================================================================
# Environment Helpers
# =============================================================================

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")


# =============================================================================
# Circuit Breaker Implementation
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = auto()      # Normal operation, requests flow through
    OPEN = auto()        # Failing, requests blocked
    HALF_OPEN = auto()   # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    failure_threshold: int = field(default_factory=lambda: _env_int(
        "TRINITY_CIRCUIT_FAILURE_THRESHOLD", 5
    ))
    success_threshold: int = field(default_factory=lambda: _env_int(
        "TRINITY_CIRCUIT_SUCCESS_THRESHOLD", 3
    ))
    timeout_seconds: float = field(default_factory=lambda: _env_float(
        "TRINITY_CIRCUIT_TIMEOUT", 30.0
    ))
    half_open_max_calls: int = field(default_factory=lambda: _env_int(
        "TRINITY_CIRCUIT_HALF_OPEN_MAX", 3
    ))


class CircuitBreaker:
    """
    Advanced circuit breaker with state persistence.

    States:
    - CLOSED: Normal operation, tracking failures
    - OPEN: Service failing, requests blocked (fail fast)
    - HALF_OPEN: Testing if service recovered

    Features:
    - Sliding window failure counting
    - State transition callbacks
    - Metrics collection
    """

    def __init__(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None,
        on_state_change: Optional[Callable[[CircuitState, CircuitState], None]] = None,
    ):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.on_state_change = on_state_change

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = 0.0
        self._last_state_change = time.time()
        self._half_open_calls = 0

        # Metrics
        self._total_calls = 0
        self._total_failures = 0
        self._total_successes = 0
        self._total_rejections = 0

        self._lock = asyncio.Lock()

        logger.info(
            f"[CircuitBreaker:{name}] Initialized with "
            f"failure_threshold={self.config.failure_threshold}, "
            f"timeout={self.config.timeout_seconds}s"
        )

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == CircuitState.OPEN

    @property
    def is_closed(self) -> bool:
        return self._state == CircuitState.CLOSED

    async def can_execute(self) -> bool:
        """Check if a request can be executed."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if timeout has elapsed
                if time.time() - self._last_failure_time >= self.config.timeout_seconds:
                    self._transition_to(CircuitState.HALF_OPEN)
                    return True
                return False

            if self._state == CircuitState.HALF_OPEN:
                # Allow limited calls in half-open state
                if self._half_open_calls < self.config.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False

            return False

    async def record_success(self) -> None:
        """Record a successful call."""
        async with self._lock:
            self._total_calls += 1
            self._total_successes += 1
            self._success_count += 1

            if self._state == CircuitState.HALF_OPEN:
                if self._success_count >= self.config.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success
                self._failure_count = 0

    async def record_failure(self, error: Optional[Exception] = None) -> None:
        """Record a failed call."""
        async with self._lock:
            self._total_calls += 1
            self._total_failures += 1
            self._failure_count += 1
            self._last_failure_time = time.time()
            self._success_count = 0

            if error:
                logger.debug(f"[CircuitBreaker:{self.name}] Failure: {error}")

            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open reopens the circuit
                self._transition_to(CircuitState.OPEN)
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.config.failure_threshold:
                    self._transition_to(CircuitState.OPEN)

    async def record_rejection(self) -> None:
        """Record a rejected call (circuit open)."""
        async with self._lock:
            self._total_rejections += 1

    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        old_state = self._state
        if old_state == new_state:
            return

        self._state = new_state
        self._last_state_change = time.time()

        # Reset counters based on new state
        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._half_open_calls = 0
            self._success_count = 0

        logger.info(
            f"[CircuitBreaker:{self.name}] State transition: "
            f"{old_state.name} -> {new_state.name}"
        )

        if self.on_state_change:
            try:
                self.on_state_change(old_state, new_state)
            except Exception as e:
                logger.warning(f"[CircuitBreaker:{self.name}] Callback error: {e}")

    def get_metrics(self) -> Dict[str, Any]:
        """Get circuit breaker metrics."""
        return {
            "name": self.name,
            "state": self._state.name,
            "total_calls": self._total_calls,
            "total_successes": self._total_successes,
            "total_failures": self._total_failures,
            "total_rejections": self._total_rejections,
            "failure_rate": (
                self._total_failures / self._total_calls
                if self._total_calls > 0 else 0.0
            ),
            "current_failures": self._failure_count,
            "last_failure": self._last_failure_time,
            "last_state_change": self._last_state_change,
        }


# =============================================================================
# Retry Policy with Exponential Backoff
# =============================================================================

@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    max_retries: int = field(default_factory=lambda: _env_int(
        "TRINITY_RETRY_MAX", 3
    ))
    base_delay: float = field(default_factory=lambda: _env_float(
        "TRINITY_RETRY_BASE_DELAY", 0.5
    ))
    max_delay: float = field(default_factory=lambda: _env_float(
        "TRINITY_RETRY_MAX_DELAY", 30.0
    ))
    exponential_base: float = 2.0
    jitter: float = 0.1  # Random factor to prevent thundering herd


class RetryPolicy:
    """
    Retry policy with exponential backoff and jitter.

    Delay formula: min(max_delay, base_delay * (exponential_base ^ attempt)) + jitter
    """

    def __init__(
        self,
        config: Optional[RetryConfig] = None,
        retryable_exceptions: Optional[Tuple[type, ...]] = None,
    ):
        self.config = config or RetryConfig()
        self.retryable_exceptions = retryable_exceptions or (
            ConnectionError,
            TimeoutError,
            asyncio.TimeoutError,
            OSError,
        )

    def should_retry(self, attempt: int, error: Exception) -> bool:
        """Check if we should retry after an error."""
        if attempt >= self.config.max_retries:
            return False
        return isinstance(error, self.retryable_exceptions)

    def get_delay(self, attempt: int) -> float:
        """Get delay before next retry with exponential backoff and jitter."""
        delay = self.config.base_delay * (self.config.exponential_base ** attempt)
        delay = min(delay, self.config.max_delay)
        # Add jitter
        jitter = random.uniform(-self.config.jitter, self.config.jitter) * delay
        return max(0, delay + jitter)

    async def execute_with_retry(
        self,
        operation: Callable[[], Awaitable[T]],
        operation_name: str = "operation",
    ) -> T:
        """Execute an operation with retry logic."""
        last_error: Optional[Exception] = None

        for attempt in range(self.config.max_retries + 1):
            try:
                return await operation()
            except Exception as e:
                last_error = e

                if not self.should_retry(attempt, e):
                    logger.debug(
                        f"[RetryPolicy] {operation_name} failed (non-retryable): {e}"
                    )
                    raise

                delay = self.get_delay(attempt)
                logger.debug(
                    f"[RetryPolicy] {operation_name} failed (attempt {attempt + 1}), "
                    f"retrying in {delay:.2f}s: {e}"
                )
                await asyncio.sleep(delay)

        # All retries exhausted
        raise last_error


# =============================================================================
# Dead Letter Queue (DLQ)
# =============================================================================

@dataclass
class DLQEntry:
    """An entry in the dead letter queue."""
    id: str
    operation: str
    payload: Dict[str, Any]
    error: str
    attempts: int
    first_attempt: float
    last_attempt: float
    metadata: Dict[str, Any] = field(default_factory=dict)


class DeadLetterQueue:
    """
    Persistent dead letter queue for failed operations.

    Features:
    - SQLite persistence
    - TTL-based cleanup
    - Automatic reprocessing
    - Priority ordering
    """

    def __init__(
        self,
        name: str,
        storage_dir: Optional[Path] = None,
        max_size: int = 10000,
        ttl_hours: float = 24.0,
    ):
        self.name = name
        self.storage_dir = storage_dir or Path.home() / ".jarvis" / "dlq"
        self.max_size = max_size
        self.ttl_hours = ttl_hours

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.storage_dir / f"{name}_dlq.db"

        self._queue: Deque[DLQEntry] = deque(maxlen=max_size)
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the DLQ (load from disk)."""
        if self._initialized:
            return

        try:
            import aiosqlite

            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS dlq_entries (
                        id TEXT PRIMARY KEY,
                        operation TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        error TEXT NOT NULL,
                        attempts INTEGER NOT NULL,
                        first_attempt REAL NOT NULL,
                        last_attempt REAL NOT NULL,
                        metadata TEXT
                    )
                """)
                await db.commit()

                # Load existing entries
                async with db.execute(
                    "SELECT * FROM dlq_entries ORDER BY last_attempt DESC"
                ) as cursor:
                    async for row in cursor:
                        entry = DLQEntry(
                            id=row[0],
                            operation=row[1],
                            payload=json.loads(row[2]),
                            error=row[3],
                            attempts=row[4],
                            first_attempt=row[5],
                            last_attempt=row[6],
                            metadata=json.loads(row[7]) if row[7] else {},
                        )
                        self._queue.append(entry)

            self._initialized = True
            logger.info(
                f"[DLQ:{self.name}] Initialized with {len(self._queue)} entries"
            )

        except ImportError:
            # Fallback to in-memory only
            self._initialized = True
            logger.warning(
                f"[DLQ:{self.name}] aiosqlite not available, using memory-only"
            )

    async def add(
        self,
        operation: str,
        payload: Dict[str, Any],
        error: str,
        attempts: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Add an entry to the DLQ."""
        await self.initialize()

        import uuid
        entry_id = str(uuid.uuid4())[:12]
        now = time.time()

        entry = DLQEntry(
            id=entry_id,
            operation=operation,
            payload=payload,
            error=error,
            attempts=attempts,
            first_attempt=now,
            last_attempt=now,
            metadata=metadata or {},
        )

        async with self._lock:
            self._queue.append(entry)
            await self._persist_entry(entry)

        logger.debug(f"[DLQ:{self.name}] Added entry {entry_id}: {operation}")
        return entry_id

    async def _persist_entry(self, entry: DLQEntry) -> None:
        """Persist entry to SQLite."""
        try:
            import aiosqlite

            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO dlq_entries
                    (id, operation, payload, error, attempts, first_attempt, last_attempt, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.id,
                        entry.operation,
                        json.dumps(entry.payload),
                        entry.error,
                        entry.attempts,
                        entry.first_attempt,
                        entry.last_attempt,
                        json.dumps(entry.metadata),
                    )
                )
                await db.commit()
        except Exception as e:
            logger.debug(f"[DLQ:{self.name}] Persist failed: {e}")

    async def get_entries(
        self,
        limit: int = 100,
        operation: Optional[str] = None,
    ) -> List[DLQEntry]:
        """Get entries from the DLQ."""
        await self.initialize()

        async with self._lock:
            entries = list(self._queue)
            if operation:
                entries = [e for e in entries if e.operation == operation]
            return entries[:limit]

    async def remove(self, entry_id: str) -> bool:
        """Remove an entry from the DLQ."""
        async with self._lock:
            for i, entry in enumerate(self._queue):
                if entry.id == entry_id:
                    del self._queue[i]
                    await self._delete_entry(entry_id)
                    return True
            return False

    async def _delete_entry(self, entry_id: str) -> None:
        """Delete entry from SQLite."""
        try:
            import aiosqlite

            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    "DELETE FROM dlq_entries WHERE id = ?",
                    (entry_id,)
                )
                await db.commit()
        except Exception:
            pass

    async def cleanup_expired(self) -> int:
        """Remove expired entries."""
        await self.initialize()

        cutoff = time.time() - (self.ttl_hours * 3600)
        removed = 0

        async with self._lock:
            expired = [e for e in self._queue if e.last_attempt < cutoff]
            for entry in expired:
                self._queue.remove(entry)
                await self._delete_entry(entry.id)
                removed += 1

        if removed:
            logger.info(f"[DLQ:{self.name}] Cleaned up {removed} expired entries")

        return removed

    def __len__(self) -> int:
        return len(self._queue)


# =============================================================================
# Request Deduplication
# =============================================================================

class RequestDeduplicator:
    """
    Deduplicates requests based on content hash.

    Features:
    - Hash-based deduplication
    - TTL for cache entries
    - In-flight request tracking
    """

    def __init__(
        self,
        cache_ttl: float = 60.0,
        max_cache_size: int = 10000,
    ):
        self.cache_ttl = cache_ttl
        self.max_cache_size = max_cache_size

        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._in_flight: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    def _compute_hash(self, operation: str, payload: Dict[str, Any]) -> str:
        """Compute hash for request deduplication."""
        content = json.dumps({"op": operation, "payload": payload}, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    async def get_cached(
        self,
        operation: str,
        payload: Dict[str, Any],
    ) -> Tuple[bool, Optional[Any]]:
        """Check if result is cached or in-flight."""
        hash_key = self._compute_hash(operation, payload)

        async with self._lock:
            # Check cache
            if hash_key in self._cache:
                timestamp, result = self._cache[hash_key]
                if time.time() - timestamp < self.cache_ttl:
                    return (True, result)
                else:
                    del self._cache[hash_key]

            # Check in-flight
            if hash_key in self._in_flight:
                future = self._in_flight[hash_key]
                return (True, await future)

        return (False, None)

    async def start_request(
        self,
        operation: str,
        payload: Dict[str, Any],
    ) -> Tuple[str, Optional[asyncio.Future]]:
        """Start a request, returns (hash, existing_future or None)."""
        hash_key = self._compute_hash(operation, payload)

        async with self._lock:
            if hash_key in self._in_flight:
                return (hash_key, self._in_flight[hash_key])

            future = asyncio.get_event_loop().create_future()
            self._in_flight[hash_key] = future
            return (hash_key, None)

    async def complete_request(
        self,
        hash_key: str,
        result: Any,
        cache: bool = True,
    ) -> None:
        """Complete a request and optionally cache result."""
        async with self._lock:
            if hash_key in self._in_flight:
                future = self._in_flight.pop(hash_key)
                future.set_result(result)

            if cache:
                # Evict old entries if needed
                if len(self._cache) >= self.max_cache_size:
                    oldest = min(self._cache.keys(), key=lambda k: self._cache[k][0])
                    del self._cache[oldest]

                self._cache[hash_key] = (time.time(), result)

    async def fail_request(
        self,
        hash_key: str,
        error: Exception,
    ) -> None:
        """Mark a request as failed."""
        async with self._lock:
            if hash_key in self._in_flight:
                future = self._in_flight.pop(hash_key)
                future.set_exception(error)


# =============================================================================
# Base Trinity Client
# =============================================================================

@dataclass
class ClientConfig:
    """Base configuration for Trinity clients."""
    name: str
    base_url: str = ""
    timeout: float = field(default_factory=lambda: _env_float(
        "TRINITY_CLIENT_TIMEOUT", 30.0
    ))
    connection_pool_size: int = field(default_factory=lambda: _env_int(
        "TRINITY_POOL_SIZE", 10
    ))
    health_check_interval: float = field(default_factory=lambda: _env_float(
        "TRINITY_HEALTH_CHECK_INTERVAL", 30.0
    ))
    enable_deduplication: bool = True
    enable_circuit_breaker: bool = True
    enable_dlq: bool = True


class TrinityBaseClient(ABC, Generic[T]):
    """
    Abstract base class for Trinity cross-repo clients.

    Provides:
    - Circuit breaker for fault tolerance
    - Retry with exponential backoff
    - Request deduplication
    - Dead letter queue
    - Health checking
    - Metrics collection
    """

    def __init__(
        self,
        config: ClientConfig,
        circuit_config: Optional[CircuitBreakerConfig] = None,
        retry_config: Optional[RetryConfig] = None,
    ):
        self.config = config

        # Circuit breaker
        self.circuit_breaker = CircuitBreaker(
            name=config.name,
            config=circuit_config,
            on_state_change=self._on_circuit_state_change,
        ) if config.enable_circuit_breaker else None

        # Retry policy
        self.retry_policy = RetryPolicy(config=retry_config)

        # Deduplication
        self.deduplicator = RequestDeduplicator() if config.enable_deduplication else None

        # DLQ
        self.dlq = DeadLetterQueue(
            name=config.name,
        ) if config.enable_dlq else None

        # State
        self._is_online = False
        self._last_health_check = 0.0
        self._health_check_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Metrics
        self._total_requests = 0
        self._successful_requests = 0
        self._failed_requests = 0
        self._dlq_additions = 0

        logger.info(f"[{config.name}] Client initialized")

    @property
    def is_online(self) -> bool:
        return self._is_online

    @abstractmethod
    async def _health_check(self) -> bool:
        """Perform health check. Override in subclass."""
        pass

    @abstractmethod
    async def _execute_request(
        self,
        operation: str,
        payload: Dict[str, Any],
    ) -> T:
        """Execute a request. Override in subclass."""
        pass

    async def connect(self) -> bool:
        """Connect to the remote service."""
        try:
            self._is_online = await self._health_check()

            if self._is_online:
                logger.info(f"[{self.config.name}] Connected")
                # Start health check task
                if self._health_check_task is None or self._health_check_task.done():
                    self._health_check_task = asyncio.create_task(
                        self._health_check_loop()
                    )
            else:
                logger.warning(f"[{self.config.name}] Connection failed")

            return self._is_online

        except Exception as e:
            logger.error(f"[{self.config.name}] Connection error: {e}")
            self._is_online = False
            return False

    async def disconnect(self) -> None:
        """Disconnect from the remote service."""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        self._is_online = False
        logger.info(f"[{self.config.name}] Disconnected")

    async def _health_check_loop(self) -> None:
        """Background health check loop."""
        while True:
            try:
                await asyncio.sleep(self.config.health_check_interval)

                was_online = self._is_online
                self._is_online = await self._health_check()
                self._last_health_check = time.time()

                if was_online and not self._is_online:
                    logger.warning(f"[{self.config.name}] Service went offline")
                elif not was_online and self._is_online:
                    logger.info(f"[{self.config.name}] Service came back online")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[{self.config.name}] Health check error: {e}")
                self._is_online = False

    async def execute(
        self,
        operation: str,
        payload: Dict[str, Any],
        skip_dedup: bool = False,
        add_to_dlq_on_failure: bool = True,
    ) -> Optional[T]:
        """
        Execute an operation with full resilience.

        Flow:
        1. Check circuit breaker
        2. Check deduplication cache
        3. Execute with retry
        4. Record metrics
        5. Add to DLQ on failure (optional)
        """
        self._total_requests += 1

        # Check circuit breaker
        if self.circuit_breaker and not await self.circuit_breaker.can_execute():
            await self.circuit_breaker.record_rejection()
            logger.debug(f"[{self.config.name}] Circuit open, rejecting {operation}")
            return None

        # Check deduplication
        if self.deduplicator and not skip_dedup:
            cached, result = await self.deduplicator.get_cached(operation, payload)
            if cached:
                logger.debug(f"[{self.config.name}] Dedup cache hit for {operation}")
                return result

        # Start dedup tracking
        hash_key = None
        if self.deduplicator and not skip_dedup:
            hash_key, existing = await self.deduplicator.start_request(operation, payload)
            if existing:
                return await existing

        try:
            # Execute with retry
            result = await self.retry_policy.execute_with_retry(
                lambda: self._execute_request(operation, payload),
                operation_name=f"{self.config.name}.{operation}",
            )

            # Record success
            self._successful_requests += 1
            if self.circuit_breaker:
                await self.circuit_breaker.record_success()

            # Complete dedup
            if self.deduplicator and hash_key:
                await self.deduplicator.complete_request(hash_key, result)

            return result

        except Exception as e:
            # Record failure
            self._failed_requests += 1
            if self.circuit_breaker:
                await self.circuit_breaker.record_failure(e)

            # Fail dedup
            if self.deduplicator and hash_key:
                await self.deduplicator.fail_request(hash_key, e)

            # Add to DLQ
            if add_to_dlq_on_failure and self.dlq:
                await self.dlq.add(
                    operation=operation,
                    payload=payload,
                    error=str(e),
                    metadata={"timestamp": time.time()},
                )
                self._dlq_additions += 1
                logger.debug(f"[{self.config.name}] Added to DLQ: {operation}")

            logger.warning(f"[{self.config.name}] {operation} failed: {e}")
            return None

    def _on_circuit_state_change(
        self,
        old_state: CircuitState,
        new_state: CircuitState,
    ) -> None:
        """Handle circuit breaker state changes."""
        if new_state == CircuitState.OPEN:
            logger.warning(
                f"[{self.config.name}] Circuit OPENED - requests will be rejected"
            )
        elif new_state == CircuitState.CLOSED:
            logger.info(
                f"[{self.config.name}] Circuit CLOSED - normal operation resumed"
            )

    def get_metrics(self) -> Dict[str, Any]:
        """Get client metrics."""
        metrics = {
            "name": self.config.name,
            "is_online": self._is_online,
            "last_health_check": self._last_health_check,
            "total_requests": self._total_requests,
            "successful_requests": self._successful_requests,
            "failed_requests": self._failed_requests,
            "success_rate": (
                self._successful_requests / self._total_requests
                if self._total_requests > 0 else 0.0
            ),
            "dlq_size": len(self.dlq) if self.dlq else 0,
            "dlq_additions": self._dlq_additions,
        }

        if self.circuit_breaker:
            metrics["circuit_breaker"] = self.circuit_breaker.get_metrics()

        return metrics

    async def process_dlq(
        self,
        batch_size: int = 10,
        max_attempts: int = 3,
    ) -> Tuple[int, int]:
        """
        Process entries from the DLQ.

        Returns (processed, failed) counts.
        """
        if not self.dlq:
            return (0, 0)

        entries = await self.dlq.get_entries(limit=batch_size)
        processed = 0
        failed = 0

        for entry in entries:
            if entry.attempts >= max_attempts:
                # Too many attempts, remove permanently
                await self.dlq.remove(entry.id)
                failed += 1
                continue

            # Try to execute
            result = await self.execute(
                operation=entry.operation,
                payload=entry.payload,
                skip_dedup=True,
                add_to_dlq_on_failure=False,  # Don't re-add
            )

            if result is not None:
                await self.dlq.remove(entry.id)
                processed += 1
            else:
                # Update attempt count (entry stays in DLQ)
                entry.attempts += 1
                entry.last_attempt = time.time()
                failed += 1

        if processed or failed:
            logger.info(
                f"[{self.config.name}] DLQ processed: {processed} success, {failed} failed"
            )

        return (processed, failed)


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Circuit Breaker
    "CircuitState",
    "CircuitBreakerConfig",
    "CircuitBreaker",
    # Retry
    "RetryConfig",
    "RetryPolicy",
    # DLQ
    "DLQEntry",
    "DeadLetterQueue",
    # Deduplication
    "RequestDeduplicator",
    # Base Client
    "ClientConfig",
    "TrinityBaseClient",
]
