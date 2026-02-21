#!/usr/bin/env python3
"""
JARVIS VBI Health Monitor v2.0.0
================================

Advanced hybrid health monitoring system for Voice Biometric Intelligence (VBI).
Provides comprehensive health tracking, timeout detection, circuit breakers,
and automatic fallback orchestration for the voice authentication pipeline.

Features:
- Operation tracking with automatic timeout detection
- Application-level heartbeat system (not just TCP ping/pong)
- Database connection pool health with circuit breakers
- CloudECAPAClient and ECAPA-TDNN model monitoring
- GCP infrastructure health (Cloud Run, VM Spot instances)
- Fallback chain orchestration with automatic recovery
- Integration with startup progress broadcaster
- Bayesian fusion for multi-signal health assessment

Architecture:
    VBIHealthMonitor
        |
        +-- OperationTracker (tracks in-flight operations)
        |       +-- VoiceUnlockOperation
        |       +-- EmbeddingExtractionOperation
        |       +-- DatabaseQueryOperation
        |
        +-- HeartbeatManager (application-level heartbeats)
        |       +-- ECAPAClientHeartbeat
        |       +-- DatabaseHeartbeat
        |       +-- GCPInfraHeartbeat
        |
        +-- CircuitBreakerRegistry
        |       +-- DatabaseCircuitBreaker
        |       +-- CloudRunCircuitBreaker
        |       +-- VMSpotCircuitBreaker
        |
        +-- FallbackOrchestrator
                +-- EmbeddingFallbackChain
                +-- DatabaseFallbackChain
                +-- AuthenticationFallbackChain

Author: JARVIS AI System
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    Tuple,
    TypeVar,
    Union,
)
from weakref import WeakSet

# v116.0: Use fixed logger name to avoid duplicate loggers from different import paths
logger = logging.getLogger("jarvis.vbi_health_monitor")


# =============================================================================
# Env-var configurable constants (v2.1.0)
# =============================================================================

def _env_float(key: str, default: float) -> float:
    """Get float from environment variable with fallback."""
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    """Get int from environment variable with fallback."""
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


# v2.1.0: Startup grace period for component health assessment.
# During this window, initialization failures (network not ready, model loading, etc.)
# are capped at DEGRADED instead of escalating to UNHEALTHY/CRITICAL.
# v253.4: Increased from 60s to 180s. At 60s, grace expired at the exact same
# moment as ECAPA's 60s timeout — causing immediate healthy → unhealthy.
# 180s covers: ECAPA timeout (60s) + model loading + normal startup margin.
VBI_COMPONENT_GRACE_PERIOD = _env_float("VBI_COMPONENT_GRACE_PERIOD", 180.0)

# v2.2.0: Stabilized overall-health transitions to avoid healthy↔degraded flapping
# from transient component spikes while still surfacing sustained failures.
VBI_OVERALL_DEGRADE_STREAK = max(1, _env_int("VBI_OVERALL_DEGRADE_STREAK", 2))
VBI_OVERALL_RECOVERY_STREAK = max(1, _env_int("VBI_OVERALL_RECOVERY_STREAK", 2))
VBI_OVERALL_STICKY_SECONDS = max(0.0, _env_float("VBI_OVERALL_STICKY_SECONDS", 15.0))


# Type variables for generic components
T = TypeVar("T")
R = TypeVar("R")


# =============================================================================
# Enums and Constants
# =============================================================================

class OperationStatus(str, Enum):
    """Status of a tracked operation."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class HealthLevel(str, Enum):
    """Health level for components."""
    OPTIMAL = "optimal"      # All systems nominal
    HEALTHY = "healthy"      # Minor issues but functional
    DEGRADED = "degraded"    # Reduced functionality
    UNHEALTHY = "unhealthy"  # Major issues
    CRITICAL = "critical"    # System failing
    UNKNOWN = "unknown"      # No data available


class CircuitState(str, Enum):
    """Circuit breaker state."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Blocking requests
    HALF_OPEN = "half_open"  # Testing recovery


class ComponentType(str, Enum):
    """Type of VBI component."""
    ECAPA_CLIENT = "ecapa_client"
    CLOUD_RUN = "cloud_run"
    VM_SPOT = "vm_spot"
    CLOUDSQL = "cloudsql"
    SQLITE = "sqlite"
    VBI_ENGINE = "vbi_engine"
    PAVA = "pava"
    VIBA = "viba"
    WEBSOCKET = "websocket"


# Dynamic timeout configuration based on operation type
DEFAULT_TIMEOUTS: Dict[str, float] = {
    "voice_unlock": 30.0,
    "embedding_extraction": 15.0,
    "speaker_verification": 10.0,
    "database_query": 5.0,
    "database_write": 10.0,
    "cloud_run_inference": 20.0,
    "vm_spot_inference": 25.0,
    "model_load": 60.0,
    "websocket_message": 5.0,
    "heartbeat": 3.0,
}


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class TrackedOperation:
    """A tracked in-flight operation."""
    operation_id: str
    operation_type: str
    component: ComponentType
    started_at: float = field(default_factory=time.time)
    timeout_seconds: float = 30.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: OperationStatus = OperationStatus.IN_PROGRESS
    completed_at: Optional[float] = None
    error: Optional[str] = None
    result: Optional[Any] = None

    @property
    def elapsed_seconds(self) -> float:
        """Get elapsed time in seconds."""
        end_time = self.completed_at or time.time()
        return end_time - self.started_at

    @property
    def elapsed_ms(self) -> float:
        """Get elapsed time in milliseconds."""
        return self.elapsed_seconds * 1000

    @property
    def is_timed_out(self) -> bool:
        """Check if operation has timed out."""
        if self.status != OperationStatus.IN_PROGRESS:
            return self.status == OperationStatus.TIMEOUT
        return time.time() - self.started_at > self.timeout_seconds

    @property
    def remaining_seconds(self) -> float:
        """Get remaining time before timeout."""
        return max(0, self.timeout_seconds - (time.time() - self.started_at))

    def complete(self, result: Any = None) -> None:
        """Mark operation as completed."""
        self.status = OperationStatus.COMPLETED
        self.completed_at = time.time()
        self.result = result

    def fail(self, error: str) -> None:
        """Mark operation as failed."""
        self.status = OperationStatus.FAILED
        self.completed_at = time.time()
        self.error = error

    def timeout(self) -> None:
        """Mark operation as timed out."""
        self.status = OperationStatus.TIMEOUT
        self.completed_at = time.time()
        self.error = f"Operation timed out after {self.timeout_seconds}s"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "operation_id": self.operation_id,
            "operation_type": self.operation_type,
            "component": self.component.value,
            "started_at": self.started_at,
            "elapsed_ms": self.elapsed_ms,
            "timeout_seconds": self.timeout_seconds,
            "remaining_seconds": self.remaining_seconds,
            "status": self.status.value,
            "is_timed_out": self.is_timed_out,
            "error": self.error,
            "metadata": self.metadata,
        }


@dataclass
class Heartbeat:
    """A heartbeat signal from a component."""
    component: ComponentType
    timestamp: float = field(default_factory=time.time)
    sequence: int = 0
    health_level: HealthLevel = HealthLevel.HEALTHY
    latency_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        """Get age of heartbeat in seconds."""
        return time.time() - self.timestamp

    @property
    def is_stale(self) -> bool:
        """Check if heartbeat is stale (older than 30s)."""
        return self.age_seconds > 30.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "component": self.component.value,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "health_level": self.health_level.value,
            "latency_ms": self.latency_ms,
            "age_seconds": self.age_seconds,
            "is_stale": self.is_stale,
            "metadata": self.metadata,
        }


@dataclass
class ComponentHealthState:
    """Health state of a VBI component.

    v2.1.0: Added startup grace period. During the grace window, initialization
    failures (network not ready, model loading, DB connection establishing) are
    capped at DEGRADED instead of escalating to UNHEALTHY/CRITICAL. When grace
    expires, if the component demonstrated functionality (at least one success),
    consecutive_failures are reset so startup failures don't cascade into normal
    operation health assessment.
    """
    component: ComponentType
    health_level: HealthLevel = HealthLevel.UNKNOWN
    last_heartbeat: Optional[Heartbeat] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    total_operations: int = 0
    failed_operations: int = 0
    timeout_operations: int = 0
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    latency_history: deque = field(default_factory=lambda: deque(maxlen=100))
    error_history: deque = field(default_factory=lambda: deque(maxlen=50))
    circuit_state: CircuitState = CircuitState.CLOSED
    last_state_change: float = field(default_factory=time.time)
    # v2.1.0: Startup grace period support
    created_at: float = field(default_factory=time.time)
    grace_period_seconds: float = 60.0
    _grace_expired_handled: bool = field(default=False, repr=False)
    _had_success_during_grace: bool = field(default=False, repr=False)

    @property
    def in_grace_period(self) -> bool:
        """Check if component is still in startup grace period."""
        if self.grace_period_seconds <= 0:
            return False
        return (time.time() - self.created_at) < self.grace_period_seconds

    def _maybe_handle_grace_expiry(self) -> None:
        """One-time transition when grace period ends.

        If the component demonstrated functionality during grace (at least one
        success), reset consecutive_failures so startup init failures don't
        cascade into normal operation health assessment.
        """
        if self._grace_expired_handled or self.in_grace_period:
            return
        if self.grace_period_seconds <= 0:
            return

        self._grace_expired_handled = True

        if self._had_success_during_grace and self.consecutive_failures > 0:
            logger.info(
                f"[ComponentHealth:{self.component.value}] Grace period expired — "
                f"resetting {self.consecutive_failures} startup failures "
                f"(component demonstrated functionality during grace)"
            )
            self.consecutive_failures = 0
        elif self.consecutive_failures > 0:
            logger.warning(
                f"[ComponentHealth:{self.component.value}] Grace period expired — "
                f"{self.consecutive_failures} consecutive failures persist "
                f"(component never succeeded during grace)"
            )

    def record_success(self, latency_ms: float) -> None:
        """Record a successful operation."""
        # v2.1.0: Track that component works during grace
        if self.in_grace_period:
            self._had_success_during_grace = True
        # v2.1.0: Handle grace expiry BEFORE counting — resets startup failures
        self._maybe_handle_grace_expiry()
        self.consecutive_successes += 1
        self.consecutive_failures = 0
        self.total_operations += 1
        self.latency_history.append(latency_ms)
        self._update_latency_stats()
        self._update_health_level()

    def record_failure(self, error: str) -> None:
        """Record a failed operation."""
        # v2.1.0: Handle grace expiry BEFORE counting — resets startup failures
        self._maybe_handle_grace_expiry()
        self.consecutive_failures += 1
        self.consecutive_successes = 0
        self.total_operations += 1
        self.failed_operations += 1
        self.error_history.append({
            "timestamp": time.time(),
            "error": error,
        })
        self._update_health_level()

    def record_timeout(self) -> None:
        """Record a timed out operation."""
        # v2.1.0: Handle grace expiry BEFORE counting — resets startup failures
        self._maybe_handle_grace_expiry()
        self.consecutive_failures += 1
        self.consecutive_successes = 0
        self.total_operations += 1
        self.timeout_operations += 1
        self._update_health_level()

    def _update_latency_stats(self) -> None:
        """Update latency statistics."""
        if not self.latency_history:
            return

        latencies = list(self.latency_history)
        self.avg_latency_ms = sum(latencies) / len(latencies)

        if len(latencies) >= 20:
            sorted_latencies = sorted(latencies)
            p95_idx = int(len(latencies) * 0.95)
            self.p95_latency_ms = sorted_latencies[p95_idx]
        else:
            self.p95_latency_ms = max(latencies) if latencies else 0

    def _update_health_level(self) -> None:
        """Update health level based on metrics.

        v2.1.0: During startup grace period, health is capped at DEGRADED.
        Initialization failures (network not ready, model loading, etc.) should
        not trigger UNHEALTHY/CRITICAL during the expected startup window.
        After grace expires, normal thresholds apply.
        """
        previous = self.health_level

        # v2.1.0: During grace period, use relaxed thresholds — cap at DEGRADED
        if self.in_grace_period:
            if self.consecutive_failures >= 1:
                self.health_level = HealthLevel.DEGRADED  # Cap — never UNHEALTHY/CRITICAL
            elif self.consecutive_successes >= 3:
                self.health_level = HealthLevel.OPTIMAL
            elif self.consecutive_successes >= 1:
                self.health_level = HealthLevel.HEALTHY
            if self.health_level != previous:
                self.last_state_change = time.time()
            return

        # Normal operation thresholds (post-grace)
        if self.consecutive_failures >= 5:
            self.health_level = HealthLevel.CRITICAL
        elif self.consecutive_failures >= 3:
            self.health_level = HealthLevel.UNHEALTHY
        elif self.consecutive_failures >= 1:
            self.health_level = HealthLevel.DEGRADED
        elif self.consecutive_successes >= 3:
            self.health_level = HealthLevel.OPTIMAL
        elif self.consecutive_successes >= 1:
            self.health_level = HealthLevel.HEALTHY

        if self.health_level != previous:
            self.last_state_change = time.time()

    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        if self.total_operations == 0:
            return 1.0
        return (self.total_operations - self.failed_operations - self.timeout_operations) / self.total_operations

    @property
    def is_active(self) -> bool:
        """Check if component has been exercised (has operations or heartbeats).

        v2.1.0: Dormant components that haven't recorded any operations or
        heartbeats should not drag down overall system health.
        """
        return self.total_operations > 0 or self.last_heartbeat is not None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "component": self.component.value,
            "health_level": self.health_level.value,
            "last_heartbeat": self.last_heartbeat.to_dict() if self.last_heartbeat else None,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "total_operations": self.total_operations,
            "failed_operations": self.failed_operations,
            "timeout_operations": self.timeout_operations,
            "success_rate": round(self.success_rate, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "p95_latency_ms": round(self.p95_latency_ms, 2),
            "circuit_state": self.circuit_state.value,
            "recent_errors": list(self.error_history)[-5:],
            "in_grace_period": self.in_grace_period,
            "is_active": self.is_active,
        }


# =============================================================================
# Circuit Breaker
# =============================================================================

class CircuitBreaker:
    """
    Advanced circuit breaker with adaptive thresholds.

    Implements the circuit breaker pattern to prevent cascading failures
    in the VBI system. Automatically adapts thresholds based on
    historical performance patterns.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
        adaptive: bool = True,
    ):
        """
        Initialize the circuit breaker.

        Args:
            name: Circuit breaker identifier
            failure_threshold: Failures before opening circuit
            recovery_timeout: Seconds before attempting recovery
            half_open_max_calls: Max calls in half-open state
            adaptive: Enable adaptive threshold adjustment
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.adaptive = adaptive

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._last_failure_time: Optional[float] = None
        self._last_state_change = time.time()
        self._lock = asyncio.Lock()

        # Adaptive parameters
        self._failure_history: deque = deque(maxlen=100)
        self._success_rate_history: deque = deque(maxlen=50)

    @property
    def state(self) -> CircuitState:
        """Get current circuit state."""
        return self._state

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (blocking requests)."""
        return self._state == CircuitState.OPEN

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (normal operation)."""
        return self._state == CircuitState.CLOSED

    async def can_execute(self) -> bool:
        """Check if request can be executed."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if recovery timeout has passed
                if self._last_failure_time and \
                   time.time() - self._last_failure_time > self.recovery_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
                    return True
                return False

            # HALF_OPEN state
            if self._half_open_calls < self.half_open_max_calls:
                self._half_open_calls += 1
                return True
            return False

    async def record_success(self) -> None:
        """Record successful execution."""
        async with self._lock:
            self._success_count += 1

            if self._state == CircuitState.HALF_OPEN:
                if self._success_count >= self.half_open_max_calls:
                    self._transition_to(CircuitState.CLOSED)
                    logger.info(f"[CircuitBreaker:{self.name}] Circuit CLOSED after successful recovery")

            # Adaptive: increase threshold on sustained success
            if self.adaptive and self._success_count % 10 == 0:
                success_rate = self._calculate_success_rate()
                self._success_rate_history.append(success_rate)

                if success_rate > 0.95 and self.failure_threshold < 15:
                    self.failure_threshold += 1
                    logger.debug(f"[CircuitBreaker:{self.name}] Increased threshold to {self.failure_threshold}")

    async def record_failure(self, error: Optional[str] = None) -> None:
        """Record failed execution."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            self._failure_history.append(time.time())

            if self._state == CircuitState.HALF_OPEN:
                self._transition_to(CircuitState.OPEN)
                logger.warning(f"[CircuitBreaker:{self.name}] Circuit OPEN after half-open failure")
            elif self._failure_count >= self.failure_threshold:
                self._transition_to(CircuitState.OPEN)
                logger.warning(
                    f"[CircuitBreaker:{self.name}] Circuit OPEN after {self._failure_count} failures"
                )

            # Adaptive: decrease threshold on rapid failures
            if self.adaptive:
                recent_failures = sum(
                    1 for t in self._failure_history
                    if time.time() - t < 60
                )
                if recent_failures > 8 and self.failure_threshold > 3:
                    self.failure_threshold -= 1
                    logger.debug(f"[CircuitBreaker:{self.name}] Decreased threshold to {self.failure_threshold}")

    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        old_state = self._state
        self._state = new_state
        self._last_state_change = time.time()

        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._half_open_calls = 0
            self._success_count = 0

        logger.info(f"[CircuitBreaker:{self.name}] State transition: {old_state.value} -> {new_state.value}")

    def _calculate_success_rate(self) -> float:
        """Calculate recent success rate."""
        total = self._success_count + self._failure_count
        if total == 0:
            return 1.0
        return self._success_count / total

    async def execute(
        self,
        func: Callable[[], Awaitable[T]],
        fallback: Optional[Callable[[], Awaitable[T]]] = None,
    ) -> T:
        """
        Execute function with circuit breaker protection.

        Args:
            func: Async function to execute
            fallback: Optional fallback function if circuit is open

        Returns:
            Result of func or fallback

        Raises:
            CircuitBreakerOpenError: If circuit is open and no fallback
        """
        if not await self.can_execute():
            if fallback:
                logger.info(f"[CircuitBreaker:{self.name}] Using fallback (circuit open)")
                return await fallback()
            raise CircuitBreakerOpenError(
                f"Circuit breaker '{self.name}' is OPEN. "
                f"Recovery in {self.recovery_timeout - (time.time() - (self._last_failure_time or 0)):.1f}s"
            )

        try:
            result = await func()
            await self.record_success()
            return result
        except Exception as e:
            await self.record_failure(str(e))
            raise

    def get_status(self) -> Dict[str, Any]:
        """Get circuit breaker status."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "last_failure_time": self._last_failure_time,
            "seconds_since_state_change": time.time() - self._last_state_change,
        }


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


# =============================================================================
# Operation Tracker
# =============================================================================

class OperationTracker:
    """
    Tracks in-flight operations with automatic timeout detection.

    Provides visibility into all active operations in the VBI system,
    detects stuck operations, and triggers recovery actions.
    """

    def __init__(
        self,
        timeout_check_interval: float = 1.0,
        on_timeout: Optional[Callable[[TrackedOperation], Awaitable[None]]] = None,
    ):
        """
        Initialize the operation tracker.

        Args:
            timeout_check_interval: Seconds between timeout checks
            on_timeout: Callback when operation times out
        """
        self._operations: Dict[str, TrackedOperation] = {}
        self._completed_operations: deque = deque(maxlen=500)
        self._timeout_check_interval = timeout_check_interval
        self._on_timeout = on_timeout
        self._running = False
        self._check_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Statistics
        self._stats = {
            "total_started": 0,
            "total_completed": 0,
            "total_failed": 0,
            "total_timeouts": 0,
        }

    async def start(self) -> None:
        """Start the operation tracker."""
        if self._running:
            return

        self._running = True
        self._check_task = asyncio.create_task(
            self._timeout_check_loop(),
            name="operation_timeout_checker"
        )
        logger.info("[OperationTracker] Started")

    async def stop(self) -> None:
        """Stop the operation tracker."""
        self._running = False

        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

        logger.info("[OperationTracker] Stopped")

    async def start_operation(
        self,
        operation_type: str,
        component: ComponentType,
        timeout_seconds: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TrackedOperation:
        """
        Start tracking a new operation.

        Args:
            operation_type: Type of operation (e.g., "voice_unlock")
            component: Component performing the operation
            timeout_seconds: Custom timeout (uses default if None)
            metadata: Additional metadata to track

        Returns:
            TrackedOperation instance
        """
        operation_id = f"{operation_type}_{uuid.uuid4().hex[:8]}_{int(time.time() * 1000)}"

        if timeout_seconds is None:
            timeout_seconds = DEFAULT_TIMEOUTS.get(operation_type, 30.0)

        operation = TrackedOperation(
            operation_id=operation_id,
            operation_type=operation_type,
            component=component,
            timeout_seconds=timeout_seconds,
            metadata=metadata or {},
        )

        async with self._lock:
            self._operations[operation_id] = operation
            self._stats["total_started"] += 1

        logger.debug(
            f"[OperationTracker] Started: {operation_type} ({operation_id}) "
            f"timeout={timeout_seconds}s component={component.value}"
        )

        return operation

    async def complete_operation(
        self,
        operation: TrackedOperation,
        result: Any = None,
    ) -> None:
        """
        Mark an operation as completed.

        Args:
            operation: The operation to complete
            result: Optional result data
        """
        async with self._lock:
            operation.complete(result)

            if operation.operation_id in self._operations:
                del self._operations[operation.operation_id]
                self._completed_operations.append(operation)
                self._stats["total_completed"] += 1

        logger.debug(
            f"[OperationTracker] Completed: {operation.operation_type} "
            f"({operation.operation_id}) in {operation.elapsed_ms:.1f}ms"
        )

    async def fail_operation(
        self,
        operation: TrackedOperation,
        error: str,
    ) -> None:
        """
        Mark an operation as failed.

        Args:
            operation: The operation that failed
            error: Error message
        """
        async with self._lock:
            operation.fail(error)

            if operation.operation_id in self._operations:
                del self._operations[operation.operation_id]
                self._completed_operations.append(operation)
                self._stats["total_failed"] += 1

        logger.warning(
            f"[OperationTracker] Failed: {operation.operation_type} "
            f"({operation.operation_id}) after {operation.elapsed_ms:.1f}ms - {error}"
        )

    async def _timeout_check_loop(self) -> None:
        """Background loop to check for timed out operations."""
        while self._running:
            try:
                await asyncio.sleep(self._timeout_check_interval)
                await self._check_timeouts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[OperationTracker] Timeout check error: {e}")

    async def _check_timeouts(self) -> None:
        """Check all operations for timeouts."""
        timed_out: List[TrackedOperation] = []

        async with self._lock:
            for operation in list(self._operations.values()):
                if operation.is_timed_out and operation.status == OperationStatus.IN_PROGRESS:
                    operation.timeout()
                    timed_out.append(operation)
                    del self._operations[operation.operation_id]
                    self._completed_operations.append(operation)
                    self._stats["total_timeouts"] += 1

        for operation in timed_out:
            logger.warning(
                f"[OperationTracker] TIMEOUT: {operation.operation_type} "
                f"({operation.operation_id}) after {operation.timeout_seconds}s"
            )

            if self._on_timeout:
                try:
                    await self._on_timeout(operation)
                except Exception as e:
                    logger.error(f"[OperationTracker] Timeout callback error: {e}")

    def get_active_operations(self) -> List[Dict[str, Any]]:
        """Get all active operations."""
        return [op.to_dict() for op in self._operations.values()]

    def get_recent_completed(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recently completed operations."""
        return [op.to_dict() for op in list(self._completed_operations)[-limit:]]

    def get_stats(self) -> Dict[str, Any]:
        """Get operation statistics."""
        return {
            **self._stats,
            "active_count": len(self._operations),
            "timeout_rate": (
                self._stats["total_timeouts"] / max(1, self._stats["total_started"])
            ),
            "failure_rate": (
                self._stats["total_failed"] / max(1, self._stats["total_started"])
            ),
        }


# =============================================================================
# Heartbeat Manager
# =============================================================================

class HeartbeatManager:
    """
    Manages application-level heartbeats for all VBI components.

    Unlike TCP-level ping/pong, this provides actual application health
    signals that indicate whether components are processing correctly.
    """

    def __init__(
        self,
        stale_threshold_seconds: float = 30.0,
        check_interval_seconds: float = 5.0,
    ):
        """
        Initialize the heartbeat manager.

        Args:
            stale_threshold_seconds: Seconds before heartbeat is stale
            check_interval_seconds: Interval between stale checks
        """
        self._heartbeats: Dict[ComponentType, Heartbeat] = {}
        self._sequence_counters: Dict[ComponentType, int] = defaultdict(int)
        self._stale_threshold = stale_threshold_seconds
        self._check_interval = check_interval_seconds
        self._running = False
        self._check_task: Optional[asyncio.Task] = None
        self._on_stale_callbacks: List[Callable[[ComponentType], Awaitable[None]]] = []
        self._lock = asyncio.Lock()
        # Track which components have been logged as stale to avoid spam
        self._stale_logged: Set[ComponentType] = set()
        # Track components that have never sent a heartbeat (don't warn about these)
        self._never_heartbeat: Set[ComponentType] = set()
        # v149.2: Startup grace period - don't mark stale until after this
        # v253.4: Synced with VBI_COMPONENT_GRACE_PERIOD instead of hardcoded 60s.
        # HeartbeatManager grace must be >= ComponentHealthState grace, otherwise
        # stale callbacks fire while component grace is still active — a race.
        self._started_at: Optional[float] = None
        self._startup_grace_seconds: float = _env_float(
            "VBI_HEARTBEAT_STARTUP_GRACE", VBI_COMPONENT_GRACE_PERIOD
        )

    async def start(self) -> None:
        """Start the heartbeat manager."""
        if self._running:
            return

        self._running = True
        self._started_at = time.time()  # v149.2: Track start time for grace period
        self._check_task = asyncio.create_task(
            self._stale_check_loop(),
            name="heartbeat_stale_checker"
        )
        logger.debug("[HeartbeatManager] Started with {:.0f}s grace period".format(self._startup_grace_seconds))

    async def stop(self) -> None:
        """Stop the heartbeat manager."""
        self._running = False

        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

        logger.info("[HeartbeatManager] Stopped")

    async def record_heartbeat(
        self,
        component: ComponentType,
        health_level: HealthLevel = HealthLevel.HEALTHY,
        latency_ms: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Heartbeat:
        """
        Record a heartbeat from a component.

        Args:
            component: Component sending heartbeat
            health_level: Current health level
            latency_ms: Measured latency
            metadata: Additional metadata

        Returns:
            The recorded Heartbeat
        """
        async with self._lock:
            self._sequence_counters[component] += 1

            heartbeat = Heartbeat(
                component=component,
                sequence=self._sequence_counters[component],
                health_level=health_level,
                latency_ms=latency_ms,
                metadata=metadata or {},
            )

            self._heartbeats[component] = heartbeat
            
            # Clear stale state when we receive a heartbeat
            self._stale_logged.discard(component)
            self._never_heartbeat.discard(component)

        logger.debug(
            f"[HeartbeatManager] Heartbeat from {component.value}: "
            f"level={health_level.value} latency={latency_ms:.1f}ms seq={heartbeat.sequence}"
        )

        return heartbeat

    def on_stale(
        self,
        callback: Callable[[ComponentType], Awaitable[None]],
    ) -> None:
        """Register callback for stale heartbeat detection."""
        self._on_stale_callbacks.append(callback)

    async def _stale_check_loop(self) -> None:
        """Background loop to check for stale heartbeats."""
        while self._running:
            try:
                await asyncio.sleep(self._check_interval)
                await self._check_stale_heartbeats()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[HeartbeatManager] Stale check error: {e}")

    async def _check_stale_heartbeats(self) -> None:
        """Check for stale heartbeats and notify.
        
        v149.2: Only logs once per component to avoid log spam. Components that
        have never sent a heartbeat are not considered stale during startup
        grace period.
        """
        stale_components: List[ComponentType] = []
        newly_stale: List[ComponentType] = []
        
        # v149.2: Check if we're still in startup grace period
        in_grace_period = False
        if self._started_at:
            uptime = time.time() - self._started_at
            in_grace_period = uptime < self._startup_grace_seconds

        async with self._lock:
            for component, heartbeat in self._heartbeats.items():
                # v149.2: Use component-specific stale threshold if available
                # Default is 30s, but heavy components might need more
                threshold = self._stale_threshold
                if component in (ComponentType.ECAPA_CLIENT, ComponentType.CLOUDSQL):
                    threshold = max(threshold, 60.0)  # Give 60s for heavy components

                if heartbeat.age_seconds > threshold:
                    # v250.0: During grace period, suppress ALL stale detections.
                    # The previous check (sequence <= 1) was insufficient — components
                    # like ECAPA send an initial heartbeat (sequence=1) then go silent
                    # for 60s while loading models. With sequence=2+ from a registration
                    # heartbeat, the old check let stale callbacks fire during grace,
                    # which then hit _handle_stale_heartbeat() → UNHEALTHY bypass.
                    # The grace period exists precisely for this startup window.
                    if in_grace_period:
                        continue

                    stale_components.append(component)
                    # Only log if this is the first time we're detecting it as stale
                    if component not in self._stale_logged:
                        newly_stale.append(component)
                        self._stale_logged.add(component)

        # Only log newly stale components to avoid spam
        for component in newly_stale:
            # v112.0: Suppress redundant logs - already logged by parallel_initializer
            # logger.warning(f"[HeartbeatManager] Stale heartbeat from {component.value}")

            for callback in self._on_stale_callbacks:
                try:
                    await callback(component)
                except Exception as e:
                    logger.error(f"[HeartbeatManager] Stale callback error: {e}")

    def get_heartbeat(self, component: ComponentType) -> Optional[Heartbeat]:
        """Get most recent heartbeat for a component."""
        return self._heartbeats.get(component)

    def get_all_heartbeats(self) -> Dict[str, Dict[str, Any]]:
        """Get all heartbeats."""
        return {
            component.value: hb.to_dict()
            for component, hb in self._heartbeats.items()
        }

    def get_stale_components(self) -> List[ComponentType]:
        """Get list of components with stale heartbeats."""
        return [
            component for component, hb in self._heartbeats.items()
            if hb.age_seconds > self._stale_threshold
        ]


# =============================================================================
# Fallback Orchestrator
# =============================================================================

@dataclass
class FallbackOption:
    """A single fallback option in a chain."""
    name: str
    component: ComponentType
    executor: Callable[..., Awaitable[Any]]
    priority: int = 0
    health_check: Optional[Callable[[], Awaitable[bool]]] = None
    is_enabled: bool = True

    async def is_available(self) -> bool:
        """Check if this fallback option is available."""
        if not self.is_enabled:
            return False

        if self.health_check:
            try:
                return await self.health_check()
            except Exception:
                return False

        return True


class FallbackChain:
    """
    A chain of fallback options for graceful degradation.

    Automatically tries alternatives when primary options fail,
    providing resilient operation in the face of component failures.
    """

    def __init__(
        self,
        name: str,
        options: Optional[List[FallbackOption]] = None,
    ):
        """
        Initialize the fallback chain.

        Args:
            name: Chain identifier
            options: Initial list of fallback options
        """
        self.name = name
        self._options: List[FallbackOption] = sorted(
            options or [],
            key=lambda o: o.priority,
            reverse=True,
        )
        self._execution_stats: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"attempts": 0, "successes": 0, "failures": 0}
        )

    def add_option(self, option: FallbackOption) -> None:
        """Add a fallback option."""
        self._options.append(option)
        self._options.sort(key=lambda o: o.priority, reverse=True)

    def remove_option(self, name: str) -> bool:
        """Remove a fallback option by name."""
        for i, option in enumerate(self._options):
            if option.name == name:
                self._options.pop(i)
                return True
        return False

    async def execute(
        self,
        *args,
        **kwargs,
    ) -> Tuple[Any, str]:
        """
        Execute the fallback chain.

        Args:
            *args: Arguments to pass to executors
            **kwargs: Keyword arguments to pass to executors

        Returns:
            Tuple of (result, option_name_used)

        Raises:
            AllFallbacksFailedError: If all options fail
        """
        errors: List[Tuple[str, Exception]] = []

        for option in self._options:
            if not await option.is_available():
                logger.debug(f"[FallbackChain:{self.name}] Skipping unavailable: {option.name}")
                continue

            self._execution_stats[option.name]["attempts"] += 1

            try:
                result = await option.executor(*args, **kwargs)
                self._execution_stats[option.name]["successes"] += 1

                logger.info(
                    f"[FallbackChain:{self.name}] Success with {option.name} "
                    f"(priority={option.priority})"
                )

                return result, option.name

            except Exception as e:
                self._execution_stats[option.name]["failures"] += 1
                errors.append((option.name, e))

                logger.warning(
                    f"[FallbackChain:{self.name}] Failed with {option.name}: {e}"
                )

        raise AllFallbacksFailedError(
            f"All {len(self._options)} fallback options failed in chain '{self.name}'",
            errors=errors,
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get execution statistics."""
        return {
            "name": self.name,
            "options_count": len(self._options),
            "options": [
                {
                    "name": o.name,
                    "priority": o.priority,
                    "component": o.component.value,
                    "is_enabled": o.is_enabled,
                    **self._execution_stats[o.name],
                }
                for o in self._options
            ],
        }


class AllFallbacksFailedError(Exception):
    """Raised when all fallback options fail."""

    def __init__(self, message: str, errors: List[Tuple[str, Exception]]):
        super().__init__(message)
        self.errors = errors


class FallbackOrchestrator:
    """
    Orchestrates multiple fallback chains for the VBI system.
    """

    def __init__(self):
        """Initialize the fallback orchestrator."""
        self._chains: Dict[str, FallbackChain] = {}
        self._lock = asyncio.Lock()

    def register_chain(self, chain: FallbackChain) -> None:
        """Register a fallback chain."""
        self._chains[chain.name] = chain
        logger.info(f"[FallbackOrchestrator] Registered chain: {chain.name}")

    def get_chain(self, name: str) -> Optional[FallbackChain]:
        """Get a fallback chain by name."""
        return self._chains.get(name)

    async def execute_chain(
        self,
        chain_name: str,
        *args,
        **kwargs,
    ) -> Tuple[Any, str]:
        """
        Execute a specific fallback chain.

        Args:
            chain_name: Name of the chain to execute
            *args: Arguments for executors
            **kwargs: Keyword arguments for executors

        Returns:
            Tuple of (result, option_name_used)
        """
        chain = self._chains.get(chain_name)
        if not chain:
            raise ValueError(f"Unknown fallback chain: {chain_name}")

        return await chain.execute(*args, **kwargs)

    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all chains."""
        return {name: chain.get_stats() for name, chain in self._chains.items()}


# =============================================================================
# VBI Health Monitor (Main Class)
# =============================================================================

class VBIHealthMonitor:
    """
    Comprehensive health monitoring for Voice Biometric Intelligence.

    Coordinates all health monitoring components:
    - Operation tracking with timeout detection
    - Application-level heartbeats
    - Circuit breakers for fault tolerance
    - Fallback chain orchestration
    - Integration with startup progress broadcaster

    Example:
        monitor = await get_vbi_health_monitor()

        # Track an operation
        op = await monitor.start_operation("voice_unlock", ComponentType.VBI_ENGINE)
        try:
            result = await perform_voice_unlock(audio_data)
            await monitor.complete_operation(op, result)
        except Exception as e:
            await monitor.fail_operation(op, str(e))

        # Get health status
        status = await monitor.get_system_health()
    """

    _instance: Optional["VBIHealthMonitor"] = None

    def __init__(self):
        """Initialize the VBI health monitor."""
        self._operation_tracker = OperationTracker(
            on_timeout=self._handle_operation_timeout,
        )
        self._heartbeat_manager = HeartbeatManager()
        self._fallback_orchestrator = FallbackOrchestrator()

        # Circuit breakers for each component
        self._circuit_breakers: Dict[ComponentType, CircuitBreaker] = {
            ComponentType.ECAPA_CLIENT: CircuitBreaker("ecapa_client", failure_threshold=5),
            ComponentType.CLOUD_RUN: CircuitBreaker("cloud_run", failure_threshold=3),
            ComponentType.VM_SPOT: CircuitBreaker("vm_spot", failure_threshold=3),
            ComponentType.CLOUDSQL: CircuitBreaker("cloudsql", failure_threshold=5),
            ComponentType.SQLITE: CircuitBreaker("sqlite", failure_threshold=10),
            ComponentType.VBI_ENGINE: CircuitBreaker("vbi_engine", failure_threshold=5),
        }

        # Component health states
        # v2.1.0: Set grace period from env var so startup failures don't escalate
        self._component_health: Dict[ComponentType, ComponentHealthState] = {
            ct: ComponentHealthState(
                component=ct,
                grace_period_seconds=VBI_COMPONENT_GRACE_PERIOD,
            )
            for ct in ComponentType
        }

        # Running state
        self._running = False
        self._started_at: Optional[float] = None

        # Integration hooks
        self._startup_broadcaster = None
        self._event_callbacks: List[Callable[[str, Dict[str, Any]], Awaitable[None]]] = []

        # Background tasks
        self._health_broadcast_task: Optional[asyncio.Task] = None

        # v2.2.0: Stable overall-health state with hysteresis.
        self._overall_health: HealthLevel = HealthLevel.UNKNOWN
        self._overall_health_candidate: Optional[HealthLevel] = None
        self._overall_health_candidate_streak: int = 0
        self._overall_last_transition_at: float = time.time()
        self._overall_degrade_streak_required = VBI_OVERALL_DEGRADE_STREAK
        self._overall_recovery_streak_required = VBI_OVERALL_RECOVERY_STREAK
        self._overall_sticky_seconds = VBI_OVERALL_STICKY_SECONDS

        logger.info("[VBIHealthMonitor] Initialized")

    @classmethod
    async def get_instance(cls) -> "VBIHealthMonitor":
        """Get or create the global VBI health monitor instance."""
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance.start()
        return cls._instance

    async def start(self) -> None:
        """Start the health monitor and all sub-components."""
        if self._running:
            return

        self._running = True
        self._started_at = time.time()
        self._overall_health = HealthLevel.UNKNOWN
        self._overall_health_candidate = None
        self._overall_health_candidate_streak = 0
        self._overall_last_transition_at = self._started_at

        # v253.4: Reset component grace period start to NOW.
        # ComponentHealthState.created_at is set at __init__ time (construction),
        # but the monitor may be constructed well before start() is called.
        # Grace period should measure from when monitoring actually begins.
        for health in self._component_health.values():
            health.created_at = self._started_at
            health._grace_expired_handled = False

        # Start sub-components
        await self._operation_tracker.start()
        await self._heartbeat_manager.start()

        # Register stale heartbeat handler
        self._heartbeat_manager.on_stale(self._handle_stale_heartbeat)

        # Initialize default fallback chains
        await self._setup_default_fallback_chains()

        # Start periodic health broadcast
        self._health_broadcast_task = asyncio.create_task(
            self._health_broadcast_loop(),
            name="vbi_health_broadcast"
        )

        # Try to get startup broadcaster integration
        try:
            from core.startup_progress_broadcaster import get_startup_broadcaster
            self._startup_broadcaster = get_startup_broadcaster()
            logger.info("[VBIHealthMonitor] Startup broadcaster integration enabled")
        except ImportError:
            try:
                from backend.core.startup_progress_broadcaster import get_startup_broadcaster
                self._startup_broadcaster = get_startup_broadcaster()
                logger.info("[VBIHealthMonitor] Startup broadcaster integration enabled (backend prefix)")
            except ImportError:
                logger.debug("[VBIHealthMonitor] Startup broadcaster not available")

        logger.info("[VBIHealthMonitor] Started")

    async def stop(self) -> None:
        """Stop the health monitor."""
        self._running = False

        if self._health_broadcast_task:
            self._health_broadcast_task.cancel()
            try:
                await self._health_broadcast_task
            except asyncio.CancelledError:
                pass

        await self._operation_tracker.stop()
        await self._heartbeat_manager.stop()

        logger.info("[VBIHealthMonitor] Stopped")

    async def _setup_default_fallback_chains(self) -> None:
        """Set up default fallback chains for VBI operations."""
        # Embedding extraction fallback chain
        embedding_chain = FallbackChain("embedding_extraction")

        # These will be populated when components register
        self._fallback_orchestrator.register_chain(embedding_chain)

        # Database fallback chain
        db_chain = FallbackChain("database")
        self._fallback_orchestrator.register_chain(db_chain)

        # Authentication fallback chain
        auth_chain = FallbackChain("authentication")
        self._fallback_orchestrator.register_chain(auth_chain)

    async def _health_broadcast_loop(self) -> None:
        """Periodically broadcast health status.

        v2.1.0: The startup grace period is now handled at the ComponentHealthState
        level — each component caps its health at DEGRADED during its grace window.
        This loop no longer needs to suppress state transitions itself. Instead, it
        always tracks `last_logged_health` accurately and logs genuine transitions.

        The per-component grace ensures that during startup:
        - Individual component health never exceeds DEGRADED (not UNHEALTHY/CRITICAL)
        - System overall stays at DEGRADED at worst (since no component is UNHEALTHY)
        - After grace expires, failures reset if component demonstrated functionality
        - The log accurately reflects the real state at all times
        """
        last_logged_health: Optional[str] = None

        # v2.1.0: Brief broadcast-level settling period — give components a few seconds
        # to register their first heartbeats before logging UNKNOWN as initial state
        broadcast_start = time.time()
        BROADCAST_SETTLE_SECONDS = _env_float("VBI_BROADCAST_SETTLE_SECONDS", 15.0)

        while self._running:
            try:
                await asyncio.sleep(10.0)

                health = await self.get_system_health()

                # Emit health event (silently) — always emit events for listeners
                await self._emit_event("health_update", health)

                overall = health.get("overall_health", HealthLevel.UNKNOWN.value)

                # v2.1.0: Brief settling — don't log UNKNOWN in first few seconds
                # while components haven't yet registered their first operations
                if (time.time() - broadcast_start) < BROADCAST_SETTLE_SECONDS:
                    if overall == HealthLevel.UNKNOWN.value:
                        continue  # Skip logging UNKNOWN during settling
                    # But DO log if we immediately got a real state

                # Only log if health state actually changed
                if overall != last_logged_health:
                    if overall in (HealthLevel.DEGRADED.value, HealthLevel.UNHEALTHY.value, HealthLevel.CRITICAL.value):
                        logger.warning(
                            f"[VBIHealthMonitor] System health changed: "
                            f"{last_logged_health or 'initial'} → {overall}"
                        )
                    elif overall in (HealthLevel.OPTIMAL.value, HealthLevel.HEALTHY.value):
                        if last_logged_health in (HealthLevel.DEGRADED.value, HealthLevel.UNHEALTHY.value, HealthLevel.CRITICAL.value):
                            logger.info(
                                f"[VBIHealthMonitor] System health recovered: "
                                f"{last_logged_health} → {overall}"
                            )
                        else:
                            logger.info(
                                f"[VBIHealthMonitor] System health: "
                                f"{last_logged_health or 'initial'} → {overall}"
                            )

                    # v2.1.0: ALWAYS update last_logged_health to prevent
                    # delayed false transitions (was the old grace period bug)
                    last_logged_health = overall

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[VBIHealthMonitor] Health broadcast error: {e}")

    async def _handle_operation_timeout(self, operation: TrackedOperation) -> None:
        """Handle operation timeout."""
        component = operation.component

        # Update component health
        health = self._component_health.get(component)
        if health:
            health.record_timeout()

        # Emit timeout event
        await self._emit_event("operation_timeout", operation.to_dict())

        # Broadcast to startup progress if available
        if self._startup_broadcaster:
            try:
                await self._startup_broadcaster.broadcast_log(
                    f"TIMEOUT: {operation.operation_type} in {component.value}",
                    level="warning",
                )
            except Exception:
                pass

    async def _handle_stale_heartbeat(self, component: ComponentType) -> None:
        """Handle stale heartbeat detection.

        v250.0: Respect ComponentHealthState grace period. During startup,
        stale heartbeats are expected (components loading ML models, connecting
        to databases, etc.). Cap at DEGRADED during grace — never UNHEALTHY.

        v253.4: After grace expires, distinguish startup-stale from runtime-stale.
        A component that NEVER succeeded (e.g. ECAPA timed out during init and
        was skipped) should stay DEGRADED — marking it UNHEALTHY implies it was
        working and then broke, which triggers false alarms. Only components that
        demonstrated functionality and THEN went stale are truly UNHEALTHY.
        """
        health = self._component_health.get(component)
        if health:
            if health.in_grace_period:
                # During grace: cap at DEGRADED — stale heartbeats are expected
                # while heavy components (ECAPA, CloudSQL) are still loading.
                health.health_level = HealthLevel.DEGRADED
            elif not health._had_success_during_grace and health.total_operations == 0:
                # v253.4: Component never succeeded and has no operations recorded.
                # It likely timed out during init and was skipped. This is NOT
                # a runtime failure — it's a startup skip. Cap at DEGRADED.
                if health.health_level != HealthLevel.DEGRADED:
                    logger.info(
                        f"[VBIHealthMonitor] {component.value} stale after grace but "
                        f"never succeeded — capping at DEGRADED (startup skip, not runtime failure)"
                    )
                health.health_level = HealthLevel.DEGRADED
            else:
                # Component was previously operational and went stale — genuine failure
                health.health_level = HealthLevel.UNHEALTHY

        await self._emit_event("heartbeat_stale", {"component": component.value})

    async def _emit_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emit a health event to all registered callbacks."""
        for callback in self._event_callbacks:
            try:
                await callback(event_type, data)
            except Exception as e:
                logger.error(f"[VBIHealthMonitor] Event callback error: {e}")

    # =========================================================================
    # Operation Tracking API
    # =========================================================================

    async def start_operation(
        self,
        operation_type: str,
        component: ComponentType,
        timeout_seconds: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TrackedOperation:
        """Start tracking an operation."""
        return await self._operation_tracker.start_operation(
            operation_type=operation_type,
            component=component,
            timeout_seconds=timeout_seconds,
            metadata=metadata,
        )

    async def complete_operation(
        self,
        operation: TrackedOperation,
        result: Any = None,
    ) -> None:
        """Complete an operation successfully."""
        await self._operation_tracker.complete_operation(operation, result)

        # Update component health
        health = self._component_health.get(operation.component)
        if health:
            health.record_success(operation.elapsed_ms)

    async def fail_operation(
        self,
        operation: TrackedOperation,
        error: str,
    ) -> None:
        """Mark an operation as failed."""
        await self._operation_tracker.fail_operation(operation, error)

        # Update component health
        health = self._component_health.get(operation.component)
        if health:
            health.record_failure(error)

        # Update circuit breaker
        cb = self._circuit_breakers.get(operation.component)
        if cb:
            await cb.record_failure(error)

    # =========================================================================
    # Heartbeat API
    # =========================================================================

    async def record_heartbeat(
        self,
        component: ComponentType,
        health_level: HealthLevel = HealthLevel.HEALTHY,
        latency_ms: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Heartbeat:
        """Record a heartbeat from a component."""
        heartbeat = await self._heartbeat_manager.record_heartbeat(
            component=component,
            health_level=health_level,
            latency_ms=latency_ms,
            metadata=metadata,
        )

        # Update component health
        health = self._component_health.get(component)
        if health:
            health.last_heartbeat = heartbeat
            health.health_level = health_level

        return heartbeat

    # =========================================================================
    # Circuit Breaker API
    # =========================================================================

    def get_circuit_breaker(self, component: ComponentType) -> Optional[CircuitBreaker]:
        """Get circuit breaker for a component."""
        return self._circuit_breakers.get(component)

    async def execute_with_circuit_breaker(
        self,
        component: ComponentType,
        func: Callable[[], Awaitable[T]],
        fallback: Optional[Callable[[], Awaitable[T]]] = None,
    ) -> T:
        """Execute function with circuit breaker protection."""
        cb = self._circuit_breakers.get(component)
        if not cb:
            return await func()

        return await cb.execute(func, fallback)

    # =========================================================================
    # Fallback Chain API
    # =========================================================================

    def register_fallback_option(
        self,
        chain_name: str,
        option: FallbackOption,
    ) -> None:
        """Register a fallback option in a chain."""
        chain = self._fallback_orchestrator.get_chain(chain_name)
        if chain:
            chain.add_option(option)

    async def execute_with_fallback(
        self,
        chain_name: str,
        *args,
        **kwargs,
    ) -> Tuple[Any, str]:
        """Execute with fallback chain."""
        return await self._fallback_orchestrator.execute_chain(
            chain_name,
            *args,
            **kwargs,
        )

    # =========================================================================
    # Event Subscription API
    # =========================================================================

    def on_event(
        self,
        callback: Callable[[str, Dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Subscribe to health events."""
        self._event_callbacks.append(callback)

    # =========================================================================
    # Shared Health State Integration (v258.4)
    # =========================================================================

    def _get_shared_health_state(self) -> Optional[Dict[str, Any]]:
        """v258.4: Read shared health state from Neural Mesh health monitor.

        The health_monitor_agent writes sys._jarvis_health_state with overall
        system health, agent statuses, and resource metrics. This uses
        GIL-atomic sys attribute access (no locks needed).

        Returns:
            Shared health dict if fresh (within 120s TTL), else None.
        """
        import sys as _sys
        state = getattr(_sys, '_jarvis_health_state', None)
        if not isinstance(state, dict):
            return None
        # Check freshness (120s TTL)
        ts = state.get("timestamp", 0)
        if time.time() - ts > 120.0:
            return None
        return state

    def _health_severity(self, level: HealthLevel) -> int:
        """Convert health level to severity rank (higher is worse)."""
        severity = {
            HealthLevel.OPTIMAL: 0,
            HealthLevel.HEALTHY: 1,
            HealthLevel.DEGRADED: 2,
            HealthLevel.UNHEALTHY: 3,
            HealthLevel.CRITICAL: 4,
            HealthLevel.UNKNOWN: 5,
        }
        return severity.get(level, 5)

    def _best_level(self, levels: List[HealthLevel]) -> HealthLevel:
        """Return the best (least severe) level from a non-empty list."""
        return min(levels, key=self._health_severity)

    def _worst_level(self, levels: List[HealthLevel]) -> HealthLevel:
        """Return the worst (most severe) level from a non-empty list."""
        return max(levels, key=self._health_severity)

    def _aggregate_redundant_group(
        self,
        members: Set[ComponentType],
        active: Dict[ComponentType, ComponentHealthState],
    ) -> HealthLevel:
        """Aggregate a redundant capability group by selecting healthiest active member."""
        member_levels = [active[c].health_level for c in members if c in active]
        if not member_levels:
            return HealthLevel.UNKNOWN
        return self._best_level(member_levels)

    def _compute_overall_health_raw(
        self,
        active: Dict[ComponentType, ComponentHealthState],
    ) -> Tuple[HealthLevel, Dict[str, str]]:
        """Compute raw overall health using resilient capability groups."""
        # Redundant capability groups. If any active path is healthy, the
        # capability is considered healthy even if another path is degraded.
        capability_groups: Dict[str, Set[ComponentType]] = {
            "inference_path": {
                ComponentType.VBI_ENGINE,
                ComponentType.ECAPA_CLIENT,
                ComponentType.CLOUD_RUN,
                ComponentType.VM_SPOT,
            },
            "persistence_path": {
                ComponentType.CLOUDSQL,
                ComponentType.SQLITE,
            },
            "auth_runtime_path": {
                ComponentType.PAVA,
                ComponentType.VIBA,
            },
        }

        capability_levels: Dict[str, str] = {}
        effective_levels: List[HealthLevel] = []
        grouped_members: Set[ComponentType] = set()

        for capability_name, members in capability_groups.items():
            level = self._aggregate_redundant_group(members, active)
            capability_levels[capability_name] = level.value
            if level != HealthLevel.UNKNOWN:
                effective_levels.append(level)
            grouped_members.update(members)

        # Non-grouped active components still contribute directly.
        for component, health_state in active.items():
            if component not in grouped_members:
                effective_levels.append(health_state.health_level)

        if not effective_levels:
            return HealthLevel.UNKNOWN, capability_levels

        raw = self._worst_level(effective_levels)
        return raw, capability_levels

    def _apply_overall_hysteresis(self, raw: HealthLevel) -> HealthLevel:
        """Stabilize overall health transitions with streak and sticky windows."""
        now = time.time()

        if self._overall_health == HealthLevel.UNKNOWN:
            self._overall_health = raw
            self._overall_last_transition_at = now
            self._overall_health_candidate = None
            self._overall_health_candidate_streak = 0
            return self._overall_health

        if raw == self._overall_health:
            self._overall_health_candidate = None
            self._overall_health_candidate_streak = 0
            return self._overall_health

        current_severity = self._health_severity(self._overall_health)
        raw_severity = self._health_severity(raw)
        getting_worse = raw_severity > current_severity

        required_streak = (
            self._overall_degrade_streak_required
            if getting_worse
            else self._overall_recovery_streak_required
        )
        if getting_worse and raw in (HealthLevel.UNHEALTHY, HealthLevel.CRITICAL):
            required_streak = 1

        if self._overall_health_candidate != raw:
            self._overall_health_candidate = raw
            self._overall_health_candidate_streak = 1
            return self._overall_health

        self._overall_health_candidate_streak += 1
        if self._overall_health_candidate_streak < required_streak:
            return self._overall_health

        if (
            not getting_worse
            and self._overall_sticky_seconds > 0
            and (now - self._overall_last_transition_at) < self._overall_sticky_seconds
        ):
            return self._overall_health

        self._overall_health = raw
        self._overall_last_transition_at = now
        self._overall_health_candidate = None
        self._overall_health_candidate_streak = 0
        return self._overall_health

    # =========================================================================
    # Health Query API
    # =========================================================================

    async def get_system_health(self) -> Dict[str, Any]:
        """Get comprehensive system health status.

        v2.1.0: Only considers *active* components (those with operations or
        heartbeats) for overall health assessment. Dormant components that
        haven't been exercised yet don't drag down the system.
        """
        active_components = {
            ct: h for ct, h in self._component_health.items()
            if h.is_active
        }
        overall_raw, capability_levels = self._compute_overall_health_raw(active_components)
        overall = self._apply_overall_hysteresis(overall_raw)

        health = {
            "overall_health": overall.value,
            "overall_health_raw": overall_raw.value,
            "is_healthy": overall in (HealthLevel.OPTIMAL, HealthLevel.HEALTHY),
            "uptime_seconds": time.time() - (self._started_at or time.time()),
            "timestamp": datetime.utcnow().isoformat(),
            "active_components_count": len(active_components),
            "capability_health": capability_levels,
            "components": {
                ct.value: health_state.to_dict()
                for ct, health_state in self._component_health.items()
            },
            "circuit_breakers": {
                ct.value: cb.get_status()
                for ct, cb in self._circuit_breakers.items()
            },
            "heartbeats": self._heartbeat_manager.get_all_heartbeats(),
            "stale_components": [c.value for c in self._heartbeat_manager.get_stale_components()],
            "active_operations": self._operation_tracker.get_active_operations(),
            "operation_stats": self._operation_tracker.get_stats(),
            "fallback_chains": self._fallback_orchestrator.get_all_stats(),
        }

        # v258.4: Enrich with shared health state from Neural Mesh health monitor
        _shared = self._get_shared_health_state()
        if _shared:
            health["shared_system_health"] = {
                "overall_status": _shared.get("overall_status", "unknown"),
                "is_startup": _shared.get("is_startup", False),
                "cpu_percent": _shared.get("system", {}).get("cpu_percent", 0),
                "memory_percent": _shared.get("system", {}).get("memory_percent", 0),
                "agent_count": len(_shared.get("agents", {})),
            }
            # If system is degraded/critical, flag stress for consumers
            _sys_status = _shared.get("overall_status", "healthy")
            if _sys_status in ("degraded", "critical"):
                health["system_stress_detected"] = True

        return health

    def get_component_health(self, component: ComponentType) -> Optional[ComponentHealthState]:
        """Get health state for a specific component."""
        return self._component_health.get(component)

    async def run_health_check(self, component: ComponentType) -> HealthCheckResult:
        """Run an active health check on a component."""
        health = self._component_health.get(component)
        if not health:
            return HealthCheckResult(HealthLevel.UNKNOWN, "Component not found")

        # Component-specific health checks
        if component == ComponentType.ECAPA_CLIENT:
            return await self._check_ecapa_health()
        elif component == ComponentType.CLOUDSQL:
            return await self._check_cloudsql_health()
        elif component == ComponentType.CLOUD_RUN:
            return await self._check_cloud_run_health()
        elif component == ComponentType.VM_SPOT:
            return await self._check_vm_spot_health()

        return HealthCheckResult(health.health_level, "Passive health check")

    async def _check_ecapa_health(self) -> HealthCheckResult:
        """Check ECAPA client health."""
        try:
            try:
                from voice_unlock.cloud_ecapa_client import get_cloud_ecapa_client
            except ImportError:
                from backend.voice_unlock.cloud_ecapa_client import get_cloud_ecapa_client

            client = await get_cloud_ecapa_client()
            if not client:
                return HealthCheckResult(HealthLevel.UNHEALTHY, "ECAPA client not available")

            status = client.get_status() if hasattr(client, 'get_status') else {}
            is_healthy = status.get('healthy', False) if isinstance(status, dict) else False

            if is_healthy:
                return HealthCheckResult(HealthLevel.HEALTHY, "ECAPA client operational")
            else:
                return HealthCheckResult(HealthLevel.DEGRADED, "ECAPA client degraded")
        except Exception as e:
            return HealthCheckResult(HealthLevel.UNHEALTHY, f"ECAPA check failed: {e}")

    async def _check_cloudsql_health(self) -> HealthCheckResult:
        """Check CloudSQL health."""
        try:
            try:
                from intelligence.cloud_sql_connection_manager import get_cloud_sql_manager
            except ImportError:
                from backend.intelligence.cloud_sql_connection_manager import get_cloud_sql_manager

            manager = await get_cloud_sql_manager()
            if manager and await manager.is_healthy():
                return HealthCheckResult(HealthLevel.HEALTHY, "CloudSQL operational")
            else:
                return HealthCheckResult(HealthLevel.DEGRADED, "CloudSQL degraded")
        except ImportError:
            return HealthCheckResult(HealthLevel.UNKNOWN, "CloudSQL manager not available")
        except Exception as e:
            return HealthCheckResult(HealthLevel.UNHEALTHY, f"CloudSQL check failed: {e}")

    async def _check_cloud_run_health(self) -> HealthCheckResult:
        """Check Cloud Run health."""
        try:
            try:
                from core.cloud_ml_router import get_cloud_ml_router
            except ImportError:
                from backend.core.cloud_ml_router import get_cloud_ml_router

            router = await get_cloud_ml_router()
            if router:
                status = await router.get_endpoint_health("cloud_run")
                if status.get("healthy", False):
                    return HealthCheckResult(HealthLevel.HEALTHY, "Cloud Run operational")
        except Exception:
            pass

        return HealthCheckResult(HealthLevel.UNKNOWN, "Cloud Run status unknown")

    async def _check_vm_spot_health(self) -> HealthCheckResult:
        """Check VM Spot instance health."""
        try:
            try:
                from backend.core.gcp_vm_manager import get_gcp_vm_manager_safe
            except ImportError:
                from core.gcp_vm_manager import get_gcp_vm_manager_safe

            manager = await get_gcp_vm_manager_safe()
            if manager:
                status = await manager.get_status()
                if status.get("healthy", False):
                    return HealthCheckResult(HealthLevel.HEALTHY, "VM Spot operational")
                elif status.get("preempted", False):
                    return HealthCheckResult(HealthLevel.DEGRADED, "VM Spot preempted")
        except Exception:
            pass

        return HealthCheckResult(HealthLevel.UNKNOWN, "VM Spot status unknown")


@dataclass
class HealthCheckResult:
    """Result of a health check."""
    level: HealthLevel
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Global Instance Access
# =============================================================================

_vbi_health_monitor: Optional[VBIHealthMonitor] = None


async def get_vbi_health_monitor() -> VBIHealthMonitor:
    """Get or create the global VBI health monitor instance."""
    global _vbi_health_monitor

    if _vbi_health_monitor is None:
        _vbi_health_monitor = VBIHealthMonitor()
        await _vbi_health_monitor.start()

    return _vbi_health_monitor


# =============================================================================
# Context Manager for Operation Tracking
# =============================================================================

class TrackedOperationContext:
    """
    Context manager for tracking operations with automatic completion/failure.

    Example:
        async with TrackedOperationContext(monitor, "voice_unlock", ComponentType.VBI_ENGINE) as op:
            result = await process_voice_unlock(audio_data)
            op.set_result(result)
    """

    def __init__(
        self,
        monitor: VBIHealthMonitor,
        operation_type: str,
        component: ComponentType,
        timeout_seconds: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Initialize the context manager."""
        self._monitor = monitor
        self._operation_type = operation_type
        self._component = component
        self._timeout_seconds = timeout_seconds
        self._metadata = metadata
        self._operation: Optional[TrackedOperation] = None
        self._result: Any = None

    async def __aenter__(self) -> "TrackedOperationContext":
        """Enter context and start tracking."""
        self._operation = await self._monitor.start_operation(
            operation_type=self._operation_type,
            component=self._component,
            timeout_seconds=self._timeout_seconds,
            metadata=self._metadata,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Exit context and record result."""
        if self._operation is None:
            return False

        if exc_type is not None:
            await self._monitor.fail_operation(
                self._operation,
                str(exc_val),
            )
        else:
            await self._monitor.complete_operation(
                self._operation,
                self._result,
            )

        return False

    def set_result(self, result: Any) -> None:
        """Set the operation result."""
        self._result = result

    @property
    def operation(self) -> Optional[TrackedOperation]:
        """Get the tracked operation."""
        return self._operation
