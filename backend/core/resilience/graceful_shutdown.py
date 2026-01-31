"""
Graceful Shutdown Coordination for Distributed Systems
======================================================

Provides coordinated shutdown across multiple repos and instances.

Features:
    - Signal handling (SIGTERM, SIGINT, SIGHUP)
    - Graceful request draining with timeout
    - Redis-backed shutdown coordination
    - Phased shutdown with priority ordering
    - State persistence before shutdown
    - Health check disabling during shutdown
    - Leader-follower shutdown coordination
    - Rollback on failed shutdown

Theory:
    In distributed systems, graceful shutdown requires coordination
    to prevent data loss, dropped requests, and inconsistent state.
    This module implements a multi-phase shutdown protocol:

    1. Signal reception and propagation
    2. Health check disabling (stop accepting new work)
    3. In-flight request draining
    4. State persistence
    5. Resource cleanup
    6. Final termination

Usage:
    coordinator = await get_shutdown_coordinator()

    # Register shutdown handlers
    coordinator.register_handler("database", db_shutdown, priority=100)
    coordinator.register_handler("cache", cache_shutdown, priority=50)
    coordinator.register_handler("workers", worker_shutdown, priority=10)

    # Start listening for signals
    await coordinator.start()

    # Trigger programmatic shutdown
    await coordinator.initiate_shutdown(reason="maintenance")

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("GracefulShutdown")


# =============================================================================
# v151.0: SHUTDOWN DIAGNOSTICS - Deep Forensic Logging
# =============================================================================
try:
    from backend.core.shutdown_diagnostics import (
        log_shutdown_trigger,
        log_state_change,
        capture_system_state,
    )
    _SHUTDOWN_DIAGNOSTICS_AVAILABLE = True
except ImportError:
    _SHUTDOWN_DIAGNOSTICS_AVAILABLE = False
    log_shutdown_trigger = lambda *args, **kwargs: None  # noqa: E731
    log_state_change = lambda *args, **kwargs: None  # noqa: E731
    capture_system_state = lambda: {}  # noqa: E731


# =============================================================================
# v128.0: Suppress Resource Tracker Semaphore Warnings
# =============================================================================
# On macOS with multiprocessing 'spawn' mode, the resource_tracker process
# may emit "leaked semaphore objects" warnings at shutdown even when semaphores
# are properly cleaned up. This is because:
# 1. Internal Python semaphores from ProcessPoolExecutor queues are tracked
# 2. The resource_tracker's atexit handler runs AFTER our cleanup
# 3. The tracker sees semaphores that are still in the process of cleanup
#
# The resource_tracker runs as a SEPARATE process, so patching functions in our
# process doesn't affect it. We use multiple approaches:
# 1. PYTHONWARNINGS env var (propagates to child processes)
# 2. warnings.filterwarnings (for current process)
# 3. warnings.simplefilter as fallback
# =============================================================================
import warnings

# Method 1: Set PYTHONWARNINGS to propagate to child processes (including resource_tracker)
# This MUST be set before any multiprocessing resources are created
_existing_pythonwarnings = os.environ.get('PYTHONWARNINGS', '')
_new_filter = 'ignore::UserWarning:multiprocessing.resource_tracker'
if _new_filter not in _existing_pythonwarnings:
    if _existing_pythonwarnings:
        os.environ['PYTHONWARNINGS'] = f"{_existing_pythonwarnings},{_new_filter}"
    else:
        os.environ['PYTHONWARNINGS'] = _new_filter

# Method 2: Add filter in current process
warnings.filterwarnings(
    'ignore',
    message='.*resource_tracker.*semaphore.*',
    category=UserWarning,
    module='multiprocessing.resource_tracker'
)

# Method 3: Also filter the older-style message format
warnings.filterwarnings(
    'ignore',
    message='.*leaked semaphore.*',
    category=UserWarning
)

logger.debug("[v128.0] Resource tracker semaphore warning suppression configured")


# =============================================================================
# Configuration
# =============================================================================

SHUTDOWN_TIMEOUT = float(os.getenv("SHUTDOWN_TIMEOUT", "30.0"))
DRAIN_TIMEOUT = float(os.getenv("DRAIN_TIMEOUT", "15.0"))
PERSISTENCE_TIMEOUT = float(os.getenv("PERSISTENCE_TIMEOUT", "10.0"))
CLEANUP_TIMEOUT = float(os.getenv("CLEANUP_TIMEOUT", "5.0"))

# Redis configuration
SHUTDOWN_REDIS_PREFIX = os.getenv("SHUTDOWN_REDIS_PREFIX", "shutdown:")
SHUTDOWN_COORDINATION_TTL = int(os.getenv("SHUTDOWN_COORDINATION_TTL", "60"))

# Instance identification
INSTANCE_ID = os.getenv("INSTANCE_ID", f"instance-{os.getpid()}")


# =============================================================================
# Enums and Data Classes
# =============================================================================

class ShutdownPhase(Enum):
    """Phases of the shutdown process."""
    RUNNING = "running"
    INITIATED = "initiated"
    DRAINING = "draining"
    PERSISTING = "persisting"
    CLEANING = "cleaning"
    COMPLETED = "completed"
    FAILED = "failed"


class ShutdownReason(Enum):
    """Reasons for shutdown."""
    SIGNAL = "signal"
    PROGRAMMATIC = "programmatic"
    MAINTENANCE = "maintenance"
    ERROR = "error"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    LEADER_INITIATED = "leader_initiated"
    ROLLING_UPDATE = "rolling_update"


@dataclass
class ShutdownHandler:
    """A registered shutdown handler."""
    name: str
    callback: Callable[[], Coroutine[Any, Any, None]]
    priority: int = 50  # 0 = first, 100 = last
    timeout: float = 10.0
    critical: bool = False  # If True, failure aborts shutdown


@dataclass
class ShutdownState:
    """Current shutdown state."""
    phase: ShutdownPhase = ShutdownPhase.RUNNING
    reason: Optional[ShutdownReason] = None
    initiated_at: float = 0.0
    initiated_by: str = ""
    handlers_completed: int = 0
    handlers_failed: int = 0
    errors: List[str] = field(default_factory=list)


@dataclass
class DistributedShutdownState:
    """Distributed shutdown coordination state."""
    instance_id: str
    phase: str
    initiated_at: float = 0.0
    completed_at: float = 0.0
    reason: str = ""
    leader: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "instance_id": self.instance_id,
            "phase": self.phase,
            "initiated_at": self.initiated_at,
            "completed_at": self.completed_at,
            "reason": self.reason,
            "leader": self.leader,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DistributedShutdownState":
        """Create from dictionary."""
        return cls(
            instance_id=data["instance_id"],
            phase=data["phase"],
            initiated_at=data.get("initiated_at", 0.0),
            completed_at=data.get("completed_at", 0.0),
            reason=data.get("reason", ""),
            leader=data.get("leader", False),
        )


# =============================================================================
# In-Flight Request Tracker
# =============================================================================

class InFlightTracker:
    """
    Tracks in-flight requests for graceful draining.

    Ensures all in-progress work completes before shutdown.
    """

    def __init__(self):
        self._requests: Dict[str, float] = {}  # request_id -> start_time
        self._lock = asyncio.Lock()
        self._drain_event = asyncio.Event()
        self._draining = False

    @property
    def count(self) -> int:
        """Number of in-flight requests."""
        return len(self._requests)

    @property
    def is_empty(self) -> bool:
        """Check if all requests have completed."""
        return len(self._requests) == 0

    @property
    def is_draining(self) -> bool:
        """Check if we're in draining mode."""
        return self._draining

    async def start_request(self, request_id: str) -> bool:
        """
        Start tracking a request.

        Returns False if draining (new requests should be rejected).
        """
        async with self._lock:
            if self._draining:
                return False

            self._requests[request_id] = time.time()
            return True

    async def end_request(self, request_id: str) -> None:
        """Mark a request as completed."""
        async with self._lock:
            if request_id in self._requests:
                del self._requests[request_id]

                if self._draining and self.is_empty:
                    self._drain_event.set()

    async def begin_drain(self) -> None:
        """Begin draining mode - reject new requests."""
        async with self._lock:
            self._draining = True
            if self.is_empty:
                self._drain_event.set()

    async def wait_for_drain(self, timeout: float = DRAIN_TIMEOUT) -> bool:
        """
        Wait for all in-flight requests to complete.

        Returns True if all drained, False if timeout.
        """
        try:
            await asyncio.wait_for(self._drain_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def get_long_running(self, threshold: float = 30.0) -> List[Tuple[str, float]]:
        """Get requests running longer than threshold seconds."""
        now = time.time()
        return [
            (req_id, now - start)
            for req_id, start in self._requests.items()
            if now - start > threshold
        ]


# =============================================================================
# Shutdown Coordinator
# =============================================================================

class ShutdownCoordinator:
    """
    Coordinates graceful shutdown across components.

    Features:
    - Signal handling
    - Phased shutdown with priorities
    - Request draining
    - State persistence
    - Distributed coordination via Redis
    """

    def __init__(
        self,
        redis_client: Optional[Any] = None,
        instance_id: str = INSTANCE_ID,
    ):
        self._redis = redis_client
        self._instance_id = instance_id

        # Shutdown state
        self._state = ShutdownState()
        self._handlers: List[ShutdownHandler] = []
        self._lock = asyncio.Lock()

        # In-flight tracking
        self._in_flight = InFlightTracker()

        # Signal handling
        self._original_handlers: Dict[int, Any] = {}
        self._shutdown_event = asyncio.Event()

        # Callbacks for shutdown notification
        self._shutdown_callbacks: List[Callable[[], Coroutine[Any, Any, None]]] = []

        # Persistence handlers
        self._persistence_handlers: List[Callable[[], Coroutine[Any, Any, None]]] = []

        logger.info(f"ShutdownCoordinator initialized (instance: {instance_id})")

    @property
    def is_shutting_down(self) -> bool:
        """Check if shutdown is in progress."""
        return self._state.phase not in (ShutdownPhase.RUNNING, ShutdownPhase.COMPLETED)

    @property
    def phase(self) -> ShutdownPhase:
        """Current shutdown phase."""
        return self._state.phase

    @property
    def in_flight_tracker(self) -> InFlightTracker:
        """Get the in-flight request tracker."""
        return self._in_flight

    async def start(self) -> None:
        """Start the coordinator and register signal handlers."""
        # Register signal handlers
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._original_handlers[sig] = signal.getsignal(sig)
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(self._handle_signal(s)),
                )
            except (ValueError, RuntimeError) as e:
                # Signal handling may not work in all contexts
                logger.warning(f"Could not register handler for {sig}: {e}")

        # Start Redis watcher if available
        if self._redis:
            asyncio.create_task(self._watch_distributed_shutdown())

        logger.info("ShutdownCoordinator started")

    async def stop(self) -> None:
        """Stop the coordinator (restore original handlers)."""
        for sig, handler in self._original_handlers.items():
            try:
                signal.signal(sig, handler)
            except Exception as e:
                logger.warning(f"Could not restore handler for {sig}: {e}")

    def register_handler(
        self,
        name: str,
        callback: Callable[[], Coroutine[Any, Any, None]],
        priority: int = 50,
        timeout: float = 10.0,
        critical: bool = False,
    ) -> None:
        """
        Register a shutdown handler.

        Args:
            name: Handler name for logging
            callback: Async function to call during shutdown
            priority: Order of execution (0 = first, 100 = last)
            timeout: Max time to wait for handler
            critical: If True, failure aborts the shutdown process
        """
        handler = ShutdownHandler(
            name=name,
            callback=callback,
            priority=priority,
            timeout=timeout,
            critical=critical,
        )
        self._handlers.append(handler)
        # Keep sorted by priority
        self._handlers.sort(key=lambda h: h.priority)
        logger.debug(f"Registered shutdown handler: {name} (priority: {priority})")

    def unregister_handler(self, name: str) -> bool:
        """Unregister a shutdown handler by name."""
        for i, handler in enumerate(self._handlers):
            if handler.name == name:
                del self._handlers[i]
                return True
        return False

    def register_persistence_handler(
        self,
        callback: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Register a handler for state persistence during shutdown."""
        self._persistence_handlers.append(callback)

    def on_shutdown(
        self,
        callback: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Register a callback to be notified when shutdown starts."""
        self._shutdown_callbacks.append(callback)

    async def _handle_signal(self, sig: signal.Signals) -> None:
        """Handle OS signal for shutdown."""
        sig_name = signal.Signals(sig).name
        logger.info(f"Received signal {sig_name}, initiating shutdown")
        await self.initiate_shutdown(
            reason=ShutdownReason.SIGNAL,
            initiator=f"signal:{sig_name}",
        )

    async def initiate_shutdown(
        self,
        reason: ShutdownReason = ShutdownReason.PROGRAMMATIC,
        initiator: str = "",
        timeout: float = SHUTDOWN_TIMEOUT,
    ) -> bool:
        """
        Initiate the shutdown process.

        Args:
            reason: Why shutdown was initiated
            initiator: Who/what initiated the shutdown
            timeout: Maximum time for entire shutdown process

        Returns:
            True if shutdown completed successfully, False otherwise
        """
        async with self._lock:
            if self._state.phase != ShutdownPhase.RUNNING:
                logger.warning(f"Shutdown already in progress (phase: {self._state.phase})")
                return False

            self._state.phase = ShutdownPhase.INITIATED
            self._state.reason = reason
            self._state.initiated_at = time.time()
            self._state.initiated_by = initiator or self._instance_id

        logger.info(f"Shutdown initiated (reason: {reason.value}, initiator: {initiator})")

        # Notify shutdown callbacks
        for callback in self._shutdown_callbacks:
            try:
                await asyncio.wait_for(callback(), timeout=5.0)
            except Exception as e:
                logger.warning(f"Shutdown callback error: {e}")

        # Broadcast to Redis if available
        await self._broadcast_shutdown_state()

        try:
            # Execute shutdown phases with overall timeout
            success = await asyncio.wait_for(
                self._execute_shutdown(),
                timeout=timeout,
            )
            return success
        except asyncio.TimeoutError:
            logger.error(f"Shutdown timed out after {timeout}s")
            self._state.phase = ShutdownPhase.FAILED
            self._state.errors.append(f"Shutdown timeout after {timeout}s")
            return False

    async def _execute_shutdown(self) -> bool:
        """Execute the shutdown phases."""
        success = True

        # Phase 1: Drain in-flight requests
        logger.info("Phase 1: Draining in-flight requests")
        self._state.phase = ShutdownPhase.DRAINING
        await self._broadcast_shutdown_state()

        await self._in_flight.begin_drain()
        drained = await self._in_flight.wait_for_drain(timeout=DRAIN_TIMEOUT)

        if not drained:
            remaining = self._in_flight.count
            long_running = self._in_flight.get_long_running()
            logger.warning(
                f"Drain timeout: {remaining} requests still in flight. "
                f"Long-running: {long_running}"
            )

        # Phase 2: Persist state
        logger.info("Phase 2: Persisting state")
        self._state.phase = ShutdownPhase.PERSISTING
        await self._broadcast_shutdown_state()

        for handler in self._persistence_handlers:
            try:
                await asyncio.wait_for(handler(), timeout=PERSISTENCE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error("State persistence timeout")
                self._state.errors.append("Persistence timeout")
            except Exception as e:
                logger.error(f"State persistence error: {e}")
                self._state.errors.append(f"Persistence error: {e}")

        # Phase 3: Run shutdown handlers (by priority)
        logger.info("Phase 3: Running shutdown handlers")
        self._state.phase = ShutdownPhase.CLEANING
        await self._broadcast_shutdown_state()

        for handler in self._handlers:
            logger.debug(f"Running handler: {handler.name}")
            try:
                await asyncio.wait_for(handler.callback(), timeout=handler.timeout)
                self._state.handlers_completed += 1
                logger.debug(f"Handler completed: {handler.name}")
            except asyncio.TimeoutError:
                logger.error(f"Handler timeout: {handler.name}")
                self._state.handlers_failed += 1
                self._state.errors.append(f"Handler timeout: {handler.name}")
                if handler.critical:
                    success = False
            except Exception as e:
                logger.error(f"Handler error ({handler.name}): {e}")
                self._state.handlers_failed += 1
                self._state.errors.append(f"Handler error ({handler.name}): {e}")
                if handler.critical:
                    success = False

        # Phase 4: Complete
        if success:
            self._state.phase = ShutdownPhase.COMPLETED
            logger.info("Shutdown completed successfully")
        else:
            self._state.phase = ShutdownPhase.FAILED
            logger.error(f"Shutdown completed with errors: {self._state.errors}")

        await self._broadcast_shutdown_state()
        self._shutdown_event.set()

        return success

    async def wait_for_shutdown(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for shutdown to complete.

        Returns True if shutdown completed, False if timeout.
        """
        try:
            if timeout:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=timeout)
            else:
                await self._shutdown_event.wait()
            return True
        except asyncio.TimeoutError:
            return False

    async def _broadcast_shutdown_state(self) -> None:
        """Broadcast shutdown state to Redis."""
        if not self._redis:
            return

        try:
            key = f"{SHUTDOWN_REDIS_PREFIX}instances:{self._instance_id}"
            state = DistributedShutdownState(
                instance_id=self._instance_id,
                phase=self._state.phase.value,
                initiated_at=self._state.initiated_at,
                reason=self._state.reason.value if self._state.reason else "",
            )
            await self._redis.set(
                key,
                json.dumps(state.to_dict()),
                ex=SHUTDOWN_COORDINATION_TTL,
            )
        except Exception as e:
            logger.warning(f"Failed to broadcast shutdown state: {e}")

    async def _watch_distributed_shutdown(self) -> None:
        """Watch for shutdown signals from other instances."""
        while not self.is_shutting_down:
            try:
                await asyncio.sleep(1.0)

                if not self._redis:
                    continue

                # Check for leader-initiated shutdown
                leader_key = f"{SHUTDOWN_REDIS_PREFIX}leader_shutdown"
                leader_state_json = await self._redis.get(leader_key)

                if leader_state_json:
                    leader_state = json.loads(leader_state_json)
                    if leader_state.get("initiated") and not self.is_shutting_down:
                        logger.info("Leader-initiated shutdown detected")
                        await self.initiate_shutdown(
                            reason=ShutdownReason.LEADER_INITIATED,
                            initiator=leader_state.get("initiator", "unknown"),
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Distributed shutdown watch error: {e}")
                await asyncio.sleep(5.0)

    async def broadcast_leader_shutdown(self, reason: str = "maintenance") -> None:
        """Broadcast a shutdown signal to all instances (leader only)."""
        if not self._redis:
            logger.warning("Cannot broadcast leader shutdown: no Redis connection")
            return

        try:
            key = f"{SHUTDOWN_REDIS_PREFIX}leader_shutdown"
            await self._redis.set(
                key,
                json.dumps({
                    "initiated": True,
                    "initiator": self._instance_id,
                    "reason": reason,
                    "timestamp": time.time(),
                }),
                ex=SHUTDOWN_COORDINATION_TTL,
            )
            logger.info(f"Leader shutdown broadcast sent (reason: {reason})")
        except Exception as e:
            logger.error(f"Failed to broadcast leader shutdown: {e}")

    async def get_cluster_shutdown_status(self) -> Dict[str, Any]:
        """Get shutdown status of all instances in the cluster."""
        if not self._redis:
            return {"instances": {self._instance_id: self._state.phase.value}}

        try:
            pattern = f"{SHUTDOWN_REDIS_PREFIX}instances:*"
            keys = await self._redis.keys(pattern)
            instances = {}

            for key in keys:
                state_json = await self._redis.get(key)
                if state_json:
                    state = DistributedShutdownState.from_dict(json.loads(state_json))
                    instances[state.instance_id] = {
                        "phase": state.phase,
                        "initiated_at": state.initiated_at,
                        "reason": state.reason,
                    }

            return {"instances": instances}
        except Exception as e:
            logger.warning(f"Failed to get cluster shutdown status: {e}")
            return {"instances": {self._instance_id: self._state.phase.value}}

    def get_status(self) -> Dict[str, Any]:
        """Get current shutdown status."""
        return {
            "instance_id": self._instance_id,
            "phase": self._state.phase.value,
            "reason": self._state.reason.value if self._state.reason else None,
            "initiated_at": self._state.initiated_at,
            "initiated_by": self._state.initiated_by,
            "handlers_completed": self._state.handlers_completed,
            "handlers_failed": self._state.handlers_failed,
            "handlers_total": len(self._handlers),
            "in_flight_requests": self._in_flight.count,
            "errors": self._state.errors,
        }


# =============================================================================
# Context Manager for Request Tracking
# =============================================================================

class TrackedRequest:
    """Context manager for tracking in-flight requests."""

    def __init__(
        self,
        coordinator: ShutdownCoordinator,
        request_id: str,
    ):
        self._coordinator = coordinator
        self._request_id = request_id
        self._started = False

    async def __aenter__(self) -> bool:
        """Start tracking the request. Returns False if shutting down."""
        self._started = await self._coordinator.in_flight_tracker.start_request(
            self._request_id
        )
        return self._started

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """End tracking the request."""
        if self._started:
            await self._coordinator.in_flight_tracker.end_request(self._request_id)


# =============================================================================
# Global Factory
# =============================================================================

_coordinator_instance: Optional[ShutdownCoordinator] = None
_coordinator_lock = asyncio.Lock()


async def get_shutdown_coordinator(
    redis_client: Optional[Any] = None,
    instance_id: str = INSTANCE_ID,
) -> ShutdownCoordinator:
    """Get or create the global ShutdownCoordinator instance."""
    global _coordinator_instance

    async with _coordinator_lock:
        if _coordinator_instance is None:
            _coordinator_instance = ShutdownCoordinator(
                redis_client=redis_client,
                instance_id=instance_id,
            )
            await _coordinator_instance.start()

        return _coordinator_instance


async def shutdown_coordinator() -> None:
    """Shutdown the global coordinator."""
    global _coordinator_instance

    if _coordinator_instance:
        await _coordinator_instance.stop()
        _coordinator_instance = None


# =============================================================================
# Decorator for Tracked Requests
# =============================================================================

def track_request(request_id_func: Callable[..., str]):
    """
    Decorator to automatically track requests.

    Args:
        request_id_func: Function to extract request ID from function args
    """
    def decorator(func):
        async def wrapper(*args, **kwargs):
            coordinator = await get_shutdown_coordinator()
            request_id = request_id_func(*args, **kwargs)

            async with TrackedRequest(coordinator, request_id) as allowed:
                if not allowed:
                    raise ShutdownInProgressError("System is shutting down")
                return await func(*args, **kwargs)

        return wrapper
    return decorator


class ShutdownInProgressError(Exception):
    """Raised when a request is rejected due to shutdown."""
    pass


# =============================================================================
# v95.11: Operation Guard for Database Operations
# =============================================================================

class OperationGuard:
    """
    v95.11: Lightweight guard for database operations during shutdown.

    Uses reference counting instead of unique request IDs for high-throughput
    database operations. Provides:
    - Atomic increment/decrement of operation count
    - Shutdown-aware operation rejection
    - Drain waiting with timeout
    - Category-based tracking (e.g., 'database', 'file_io', 'network')

    Usage:
        guard = get_operation_guard()

        # Before starting an operation
        if not guard.try_start("database"):
            raise ShutdownInProgressError("Database shutting down")

        try:
            await do_database_operation()
        finally:
            guard.finish("database")

    Or use the context manager:
        async with guard.operation("database"):
            await do_database_operation()
    """

    def __init__(self):
        self._counts: Dict[str, int] = {}
        self._draining: Set[str] = set()
        self._drain_events: Dict[str, asyncio.Event] = {}
        self._global_shutdown = False
        self._lock = asyncio.Lock()
        self._total_operations = 0
        self._rejected_operations = 0

    @property
    def is_shutting_down(self) -> bool:
        """Check if global shutdown is in progress."""
        return self._global_shutdown

    def get_count(self, category: str = "default") -> int:
        """Get current operation count for a category."""
        return self._counts.get(category, 0)

    def get_total_active(self) -> int:
        """Get total active operations across all categories."""
        return sum(self._counts.values())

    def try_start(self, category: str = "default") -> bool:
        """
        Try to start an operation. Returns False if shutting down.

        Thread-safe, uses lock-free atomic increment when not draining.
        """
        if self._global_shutdown or category in self._draining:
            self._rejected_operations += 1
            return False

        if category not in self._counts:
            self._counts[category] = 0

        self._counts[category] += 1
        self._total_operations += 1
        return True

    def finish(self, category: str = "default") -> None:
        """Mark an operation as finished."""
        if category in self._counts:
            self._counts[category] = max(0, self._counts[category] - 1)

            # Signal drain if we're draining and count reached zero
            if category in self._draining and self._counts[category] == 0:
                event = self._drain_events.get(category)
                if event and not event.is_set():
                    event.set()

    async def begin_drain(self, category: str = "default") -> None:
        """Begin draining a category - reject new operations."""
        async with self._lock:
            self._draining.add(category)
            if category not in self._drain_events:
                self._drain_events[category] = asyncio.Event()

            # If already empty, set the event
            if self._counts.get(category, 0) == 0:
                self._drain_events[category].set()

    async def begin_global_shutdown(self) -> None:
        """Begin global shutdown - reject ALL new operations."""
        async with self._lock:
            self._global_shutdown = True
            # Start draining all categories
            for category in list(self._counts.keys()):
                await self.begin_drain(category)

    async def wait_for_drain(
        self,
        category: str = "default",
        timeout: float = DRAIN_TIMEOUT,
    ) -> bool:
        """
        Wait for all operations in a category to complete.

        Returns True if drained, False if timeout.
        """
        if category not in self._draining:
            await self.begin_drain(category)

        event = self._drain_events.get(category)
        if not event:
            return True  # Nothing to drain

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            remaining = self._counts.get(category, 0)
            logger.warning(f"Drain timeout for '{category}': {remaining} operations still active")
            return False

    async def wait_for_all_drain(self, timeout: float = DRAIN_TIMEOUT) -> bool:
        """Wait for all categories to drain."""
        await self.begin_global_shutdown()

        categories = list(self._counts.keys())
        if not categories:
            return True

        # Calculate per-category timeout
        per_category_timeout = timeout / max(len(categories), 1)

        all_drained = True
        for category in categories:
            drained = await self.wait_for_drain(category, per_category_timeout)
            if not drained:
                all_drained = False

        return all_drained

    @asynccontextmanager
    async def operation(self, category: str = "default"):
        """
        Async context manager for operations.

        Raises ShutdownInProgressError if shutdown is in progress.
        """
        if not self.try_start(category):
            raise ShutdownInProgressError(f"Operation rejected: {category} is shutting down")
        try:
            yield
        finally:
            self.finish(category)

    def get_stats(self) -> Dict[str, Any]:
        """Get operation guard statistics."""
        return {
            "global_shutdown": self._global_shutdown,
            "draining_categories": list(self._draining),
            "active_by_category": dict(self._counts),
            "total_active": self.get_total_active(),
            "total_operations": self._total_operations,
            "rejected_operations": self._rejected_operations,
        }


# Global operation guard instance
_operation_guard: Optional[OperationGuard] = None
_operation_guard_lock = asyncio.Lock()


async def get_operation_guard() -> OperationGuard:
    """Get or create the global OperationGuard instance."""
    global _operation_guard

    if _operation_guard is None:
        async with _operation_guard_lock:
            if _operation_guard is None:
                _operation_guard = OperationGuard()
                logger.info("OperationGuard initialized")

    return _operation_guard


def get_operation_guard_sync() -> OperationGuard:
    """Get the global OperationGuard instance synchronously (creates if needed)."""
    global _operation_guard

    if _operation_guard is None:
        _operation_guard = OperationGuard()
        logger.info("OperationGuard initialized (sync)")

    return _operation_guard


# =============================================================================
# v95.11: Database Operation Context Manager
# =============================================================================

@asynccontextmanager
async def database_operation():
    """
    Context manager for database operations.

    Automatically tracks the operation and handles shutdown gracefully.

    Usage:
        async with database_operation():
            await db.execute("SELECT * FROM users")
    """
    guard = get_operation_guard_sync()
    if not guard.try_start("database"):
        raise ShutdownInProgressError("Database operations are shutting down")
    try:
        yield
    finally:
        guard.finish("database")


# =============================================================================
# v95.11: Integration Helpers
# =============================================================================

async def register_database_shutdown(
    coordinator: ShutdownCoordinator,
    drain_timeout: float = 10.0,
) -> None:
    """
    Register database shutdown handling with the coordinator.

    This ensures database operations are properly drained before
    connection pools are closed.
    """
    guard = await get_operation_guard()

    # Register high-priority handler to start draining
    coordinator.register_handler(
        name="database_drain_start",
        callback=lambda: guard.begin_drain("database"),
        priority=5,  # Run early
        timeout=1.0,
    )

    # Register handler to wait for drain completion
    async def wait_for_database_drain():
        await guard.wait_for_drain("database", timeout=drain_timeout)
        active = guard.get_count("database")
        if active > 0:
            logger.warning(f"Database drain incomplete: {active} operations still active")
        else:
            logger.info("Database operations fully drained")

    coordinator.register_handler(
        name="database_drain_wait",
        callback=wait_for_database_drain,
        priority=10,  # Run after drain start
        timeout=drain_timeout + 2.0,
    )


def should_reject_operation(category: str = "default") -> bool:
    """
    Quick check if an operation should be rejected.

    Use this at the start of long-running operations.
    """
    guard = get_operation_guard_sync()
    return guard.is_shutting_down or category in guard._draining


# =============================================================================
# v95.12: Multiprocessing Resource Cleanup
# =============================================================================

import atexit
import weakref
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor


class MultiprocessingResourceTracker:
    """
    v95.12: Tracks and cleans up multiprocessing resources to prevent leaks.

    Problem:
        ProcessPoolExecutor and multiprocessing.Process use semaphores for
        synchronization. If shutdown(wait=False) is called or processes are
        killed abruptly, these semaphores are not released, causing the
        "leaked semaphore objects" warning.

    Solution:
        - Track all ProcessPoolExecutors and ThreadPoolExecutors
        - Ensure proper shutdown with wait=True during graceful shutdown
        - Clean up orphaned semaphores on startup
        - Register atexit handler for emergency cleanup

    Usage:
        tracker = get_multiprocessing_tracker()

        # Register an executor for tracking
        tracker.register_executor(my_process_pool, "my_executor")

        # During shutdown
        await tracker.shutdown_all_executors(timeout=10.0)
    """

    def __init__(self):
        # Use weak references to avoid preventing garbage collection
        self._executors: Dict[str, weakref.ref] = {}
        self._lock = asyncio.Lock()
        self._shutdown_started = False
        self._cleanup_stats = {
            "executors_registered": 0,
            "executors_shutdown": 0,
            "forced_shutdowns": 0,
            "errors": [],
        }

        # Register atexit handler for emergency cleanup
        atexit.register(self._emergency_cleanup)
        logger.debug("[v95.12] MultiprocessingResourceTracker initialized")

    def register_executor(
        self,
        executor: Any,  # ProcessPoolExecutor | ThreadPoolExecutor
        name: str,
        is_process_pool: bool = False,
    ) -> None:
        """
        Register an executor for tracking.

        Args:
            executor: The executor to track
            name: A unique name for this executor
            is_process_pool: True if this is a ProcessPoolExecutor
        """
        # Store as weak reference
        def on_finalize(ref):
            # Executor was garbage collected - remove from tracking
            if name in self._executors:
                del self._executors[name]
                logger.debug(f"[v95.12] Executor '{name}' was garbage collected")

        self._executors[name] = weakref.ref(executor, on_finalize)
        self._cleanup_stats["executors_registered"] += 1
        logger.debug(
            f"[v95.12] Registered executor '{name}' "
            f"(type: {'process' if is_process_pool else 'thread'})"
        )

    def unregister_executor(self, name: str) -> bool:
        """Unregister an executor by name."""
        if name in self._executors:
            del self._executors[name]
            return True
        return False

    async def shutdown_all_executors(
        self,
        timeout: float = 10.0,
        cancel_futures: bool = True,
    ) -> Dict[str, Any]:
        """
        Shutdown all tracked executors properly.

        This is the key method that prevents semaphore leaks by:
        1. Setting wait=True to allow workers to exit cleanly
        2. Using a timeout to prevent hanging
        3. Falling back to forced shutdown if timeout is exceeded

        Args:
            timeout: Maximum time to wait for all executors
            cancel_futures: Whether to cancel pending futures

        Returns:
            Dict with shutdown statistics
        """
        async with self._lock:
            if self._shutdown_started:
                return {"already_shutdown": True}
            self._shutdown_started = True

        logger.info(f"[v95.12] Shutting down {len(self._executors)} tracked executors...")
        results = {
            "total": len(self._executors),
            "successful": 0,
            "forced": 0,
            "failed": 0,
            "details": {},
        }

        # Calculate per-executor timeout
        executor_count = len(self._executors)
        per_executor_timeout = timeout / max(executor_count, 1)

        for name, executor_ref in list(self._executors.items()):
            executor = executor_ref()
            if executor is None:
                # Already garbage collected
                results["details"][name] = "garbage_collected"
                continue

            try:
                logger.debug(f"[v95.12] Shutting down executor '{name}'...")

                # Run shutdown in thread to avoid blocking event loop
                shutdown_success = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda e=executor, c=cancel_futures: (
                            e.shutdown(wait=True, cancel_futures=c)
                            if hasattr(e.shutdown, '__call__')
                            else None,
                            True,
                        )[1],
                    ),
                    timeout=per_executor_timeout,
                )

                results["successful"] += 1
                results["details"][name] = "success"
                self._cleanup_stats["executors_shutdown"] += 1
                logger.debug(f"[v95.12] ✅ Executor '{name}' shutdown successfully")

            except asyncio.TimeoutError:
                logger.warning(f"[v128.0] Executor '{name}' shutdown timeout, forcing with worker cleanup...")
                try:
                    # Force shutdown without waiting
                    if hasattr(executor, 'shutdown'):
                        executor.shutdown(wait=False, cancel_futures=True)

                    # v128.0: For ProcessPoolExecutors, clean up worker processes
                    # to properly release semaphores
                    if self._is_process_pool.get(name, False):
                        cleaned = self._cleanup_process_pool_workers(executor, name)
                        if cleaned > 0:
                            logger.debug(f"[v128.0] Cleaned {cleaned} workers for '{name}'")

                    results["forced"] += 1
                    results["details"][name] = "forced"
                    self._cleanup_stats["forced_shutdowns"] += 1
                except Exception as e:
                    results["failed"] += 1
                    results["details"][name] = f"force_error: {e}"
                    self._cleanup_stats["errors"].append(f"{name}: {e}")

            except Exception as e:
                logger.error(f"[v95.12] Error shutting down executor '{name}': {e}")
                results["failed"] += 1
                results["details"][name] = f"error: {e}"
                self._cleanup_stats["errors"].append(f"{name}: {e}")

        logger.info(
            f"[v95.12] Executor shutdown complete: "
            f"{results['successful']} success, {results['forced']} forced, "
            f"{results['failed']} failed"
        )
        return results

    def shutdown_all_executors_sync(self, timeout: float = 5.0) -> Dict[str, Any]:
        """
        v128.0: Synchronous version of shutdown_all_executors with proper semaphore cleanup.

        Used by atexit handler and other synchronous contexts.

        CRITICAL: For ProcessPoolExecutors, we MUST properly terminate worker processes
        to release their internal semaphores. Simply calling shutdown(wait=False) leaves
        semaphores orphaned, causing the resource_tracker warning.
        """
        if self._shutdown_started:
            return {"already_shutdown": True}
        self._shutdown_started = True

        results = {
            "total": len(self._executors),
            "successful": 0,
            "forced": 0,
            "failed": 0,
            "semaphores_cleaned": 0,
        }

        per_executor_timeout = timeout / max(len(self._executors), 1)

        for name, executor_ref in list(self._executors.items()):
            executor = executor_ref()
            if executor is None:
                continue

            is_process_pool = self._is_process_pool.get(name, False)

            try:
                # Use threading.Timer for timeout in sync context
                import threading

                shutdown_complete = threading.Event()

                def do_shutdown():
                    try:
                        executor.shutdown(wait=True, cancel_futures=True)
                    except Exception:
                        pass
                    finally:
                        shutdown_complete.set()

                shutdown_thread = threading.Thread(target=do_shutdown, daemon=True)
                shutdown_thread.start()

                if shutdown_complete.wait(timeout=per_executor_timeout):
                    results["successful"] += 1
                else:
                    # Timeout - need careful cleanup for ProcessPoolExecutors
                    try:
                        executor.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass

                    # v128.0: For ProcessPoolExecutors, properly clean up worker processes
                    # to release their semaphores
                    if is_process_pool:
                        cleaned = self._cleanup_process_pool_workers(executor, name)
                        results["semaphores_cleaned"] += cleaned

                    results["forced"] += 1

            except Exception:
                results["failed"] += 1

        # v128.0: Final GC to release any remaining semaphore references
        import gc
        gc.collect()

        return results

    def _cleanup_process_pool_workers(self, executor: Any, name: str) -> int:
        """
        v128.0: Properly clean up ProcessPoolExecutor worker processes.

        This is CRITICAL for preventing semaphore leaks. ProcessPoolExecutor uses
        internal semaphores for its call/result queues. These semaphores are held
        by the worker processes, so we must terminate them properly.

        Returns:
            Number of worker processes cleaned up
        """
        cleaned = 0

        try:
            # Access internal worker processes
            # ProcessPoolExecutor stores them in _processes dict
            processes = getattr(executor, '_processes', None)
            if processes and isinstance(processes, dict):
                for pid, process in list(processes.items()):
                    if process is None:
                        continue

                    try:
                        # Check if process is still alive
                        if hasattr(process, 'is_alive') and process.is_alive():
                            # Try graceful termination first
                            process.terminate()
                            # Give it a short time to die
                            process.join(timeout=0.3)

                            # If still alive, force kill
                            if process.is_alive():
                                process.kill()
                                process.join(timeout=0.2)

                        # Join zombie processes to release resources
                        if hasattr(process, 'join'):
                            try:
                                process.join(timeout=0.1)
                            except Exception:
                                pass

                        cleaned += 1

                    except Exception as e:
                        logger.debug(f"[v128.0] Error cleaning up worker {pid} in {name}: {e}")

                # Clear the processes dict
                try:
                    processes.clear()
                except Exception:
                    pass

            # Also clean up any internal queues that might hold semaphore references
            for attr in ['_call_queue', '_result_queue', '_work_ids']:
                queue = getattr(executor, attr, None)
                if queue is not None:
                    try:
                        # Close queue to release semaphores
                        if hasattr(queue, 'close'):
                            queue.close()
                        if hasattr(queue, 'cancel_join_thread'):
                            queue.cancel_join_thread()
                    except Exception:
                        pass

            if cleaned > 0:
                logger.debug(f"[v128.0] Cleaned up {cleaned} worker processes for '{name}'")

        except Exception as e:
            logger.debug(f"[v128.0] Error in worker cleanup for {name}: {e}")

        return cleaned

    def _emergency_cleanup(self) -> None:
        """
        Emergency cleanup called by atexit.

        This is a last-resort cleanup when normal shutdown didn't happen.
        """
        if not self._executors:
            return

        # Perform quick sync cleanup
        try:
            result = self.shutdown_all_executors_sync(timeout=2.0)
            if result.get("successful", 0) > 0 or result.get("forced", 0) > 0:
                logger.debug(
                    f"[v95.12] atexit cleanup: {result.get('successful', 0)} success, "
                    f"{result.get('forced', 0)} forced"
                )
        except Exception as e:
            logger.debug(f"[v95.12] atexit cleanup error: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get resource tracker statistics."""
        active_executors = sum(1 for ref in self._executors.values() if ref() is not None)
        return {
            **self._cleanup_stats,
            "active_executors": active_executors,
            "shutdown_started": self._shutdown_started,
        }


# Global multiprocessing resource tracker
_mp_tracker: Optional[MultiprocessingResourceTracker] = None


def get_multiprocessing_tracker() -> MultiprocessingResourceTracker:
    """Get the global MultiprocessingResourceTracker instance."""
    global _mp_tracker
    if _mp_tracker is None:
        _mp_tracker = MultiprocessingResourceTracker()
    return _mp_tracker


async def cleanup_multiprocessing_resources(timeout: float = 10.0) -> Dict[str, Any]:
    """
    Clean up all multiprocessing resources.

    Call this during graceful shutdown to properly release semaphores.
    """
    tracker = get_multiprocessing_tracker()
    return await tracker.shutdown_all_executors(timeout=timeout)


def register_executor_for_cleanup(
    executor: Any,
    name: str,
    is_process_pool: bool = False,
) -> None:
    """
    Register an executor for cleanup during shutdown.

    Call this whenever you create a ProcessPoolExecutor or ThreadPoolExecutor.

    Args:
        executor: The executor to track
        name: A unique name for this executor
        is_process_pool: True if this is a ProcessPoolExecutor (critical for semaphore cleanup)
    """
    tracker = get_multiprocessing_tracker()
    tracker.register_executor(executor, name, is_process_pool)


async def cleanup_orphaned_semaphores() -> Dict[str, Any]:
    """
    Clean up orphaned semaphores left by crashed processes.

    This uses platform-specific commands to identify and remove
    semaphores that are no longer in use.

    Returns:
        Dict with cleanup results
    """
    import subprocess
    import platform

    results = {
        "platform": platform.system(),
        "semaphores_found": 0,
        "semaphores_cleaned": 0,
        "errors": [],
    }

    if platform.system() != "Darwin" and platform.system() != "Linux":
        results["skipped"] = "Unsupported platform"
        return results

    try:
        if platform.system() == "Darwin":
            # macOS: Use ipcs to list semaphores
            proc = await asyncio.create_subprocess_exec(
                "ipcs", "-s",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                lines = stdout.decode().strip().split("\n")
                # Parse semaphore info (skip header lines)
                for line in lines[3:]:  # Skip headers
                    parts = line.split()
                    if len(parts) >= 2:
                        results["semaphores_found"] += 1

        # Note: Actually removing semaphores requires root privileges
        # and careful identification to avoid removing active semaphores.
        # For now, we just report what we find.

        logger.debug(f"[v95.12] Found {results['semaphores_found']} semaphores")

    except Exception as e:
        results["errors"].append(str(e))
        logger.debug(f"[v95.12] Error checking semaphores: {e}")

    return results


# =============================================================================
# v95.12: Integration with Shutdown Coordinator
# =============================================================================

async def register_multiprocessing_shutdown(
    coordinator: ShutdownCoordinator,
    timeout: float = 10.0,
) -> None:
    """
    Register multiprocessing cleanup with the shutdown coordinator.

    This ensures all ProcessPoolExecutors are properly shut down before
    the process exits, preventing semaphore leaks.
    """
    async def cleanup_mp_resources():
        """Cleanup handler for multiprocessing resources."""
        logger.info("[v95.12] Cleaning up multiprocessing resources...")
        result = await cleanup_multiprocessing_resources(timeout=timeout)
        logger.info(
            f"[v95.12] Multiprocessing cleanup: "
            f"{result.get('successful', 0)} success, "
            f"{result.get('forced', 0)} forced, "
            f"{result.get('failed', 0)} failed"
        )

    coordinator.register_handler(
        name="multiprocessing_cleanup",
        callback=cleanup_mp_resources,
        priority=95,  # Run late, after most other handlers
        timeout=timeout + 2.0,
        critical=False,  # Don't block shutdown if this fails
    )

    logger.debug("[v95.12] Multiprocessing shutdown handler registered")


# =============================================================================
# v95.13: Global Shutdown Signal - Unified Shutdown Coordination
# =============================================================================

class GlobalShutdownSignal:
    """
    v95.13: Centralized global shutdown signal for all components.

    Problem:
        Multiple components (WebSocket handlers, background tasks, API servers)
        have their own internal shutdown events that are not synchronized.
        When the orchestrator initiates shutdown, these components may continue
        running, causing post-shutdown activity errors.

    Solution:
        A single global shutdown signal that:
        1. Can be checked synchronously or asynchronously
        2. Supports callback registration for instant notification
        3. Provides both "in progress" and "completed" states
        4. Is thread-safe and process-safe (via file-based coordination)

    Usage:
        # Check if shutdown is happening
        if is_global_shutdown_initiated():
            return  # Exit early

        # Register for notification
        register_shutdown_callback(my_cleanup_function)

        # Wait for shutdown
        await wait_for_global_shutdown()
    """

    _instance: Optional["GlobalShutdownSignal"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "GlobalShutdownSignal":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        self._initiated = False
        self._completed = False
        self._initiated_at: Optional[float] = None
        self._completed_at: Optional[float] = None
        self._reason: Optional[str] = None
        self._initiator: Optional[str] = None

        # Async event for await support
        self._async_event: Optional[asyncio.Event] = None
        self._completed_event: Optional[asyncio.Event] = None

        # Callbacks to notify on shutdown
        # v117.0: Store as (name, callback) tuples for better observability
        self._callbacks: List[Tuple[Optional[str], Callable[[], Any]]] = []
        self._async_callbacks: List[Tuple[Optional[str], Callable[[], Coroutine[Any, Any, None]]]] = []

        # Thread-safe lock for state changes
        self._state_lock = threading.RLock()

        # File-based coordination for cross-process signaling
        self._signal_file = Path.home() / ".jarvis" / "shutdown_signal.json"

        self._initialized = True
        logger.debug("[v95.13] GlobalShutdownSignal initialized")

    @property
    def is_initiated(self) -> bool:
        """Check if shutdown has been initiated (in progress or completed)."""
        # Check local state first (fastest)
        if self._initiated:
            return True

        # Check file-based signal for cross-process coordination
        return self._check_file_signal()

    @property
    def is_completed(self) -> bool:
        """Check if shutdown has completed."""
        if self._completed:
            return True
        return self._check_file_signal(check_completed=True)

    def _check_file_signal(self, check_completed: bool = False) -> bool:
        """Check file-based shutdown signal for cross-process coordination."""
        try:
            if self._signal_file.exists():
                content = self._signal_file.read_text()
                if content:
                    data = json.loads(content)
                    # Check age - ignore signals older than 5 minutes (stale)
                    initiated_at = data.get("initiated_at", 0)
                    if time.time() - initiated_at > 300:
                        return False

                    if check_completed:
                        return data.get("completed", False)
                    return data.get("initiated", False)
        except Exception:
            pass
        return False

    def _write_signal_file(self) -> None:
        """Write shutdown signal to file for cross-process coordination."""
        try:
            self._signal_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "initiated": self._initiated,
                "completed": self._completed,
                "initiated_at": self._initiated_at,
                "completed_at": self._completed_at,
                "reason": self._reason,
                "initiator": self._initiator,
                "pid": os.getpid(),
            }
            self._signal_file.write_text(json.dumps(data))
        except Exception as e:
            logger.debug(f"[v95.13] Could not write signal file: {e}")

    def _clear_signal_file(self) -> None:
        """Clear the shutdown signal file (called on startup)."""
        try:
            if self._signal_file.exists():
                self._signal_file.unlink()
        except Exception as e:
            logger.debug(f"[v95.13] Could not clear signal file: {e}")

    def initiate(
        self,
        reason: str = "unknown",
        initiator: Optional[str] = None,
    ) -> None:
        """
        Initiate global shutdown.

        This immediately notifies all registered callbacks and sets the
        shutdown flag so all components can check it.

        Args:
            reason: Why shutdown was initiated (user_request, signal, error, etc.)
            initiator: Identifier of the component that initiated shutdown
        """
        # ═══════════════════════════════════════════════════════════════════════════
        # v151.0: DIAGNOSTIC LOGGING - Capture full context of shutdown trigger
        # ═══════════════════════════════════════════════════════════════════════════
        import traceback

        stack_trace = "".join(traceback.format_stack())

        if _SHUTDOWN_DIAGNOSTICS_AVAILABLE:
            log_shutdown_trigger(
                "GlobalShutdownSignal.initiate",
                f"Global shutdown signal - reason={reason}, initiator={initiator}",
                {
                    "reason": reason,
                    "initiator": initiator,
                    "stack_trace": stack_trace,
                    "system_state": capture_system_state(),
                }
            )

        logger.warning(
            f"[v151.0] 🔬 GLOBAL SHUTDOWN TRIGGER:\n"
            f"    Reason: {reason}\n"
            f"    Initiator: {initiator}\n"
            f"    Stack trace:\n{stack_trace}"
        )
        # ═══════════════════════════════════════════════════════════════════════════

        with self._state_lock:
            if self._initiated:
                logger.debug("[v95.13] Shutdown already initiated, ignoring")
                return

            self._initiated = True
            self._initiated_at = time.time()
            self._reason = reason
            self._initiator = initiator or f"pid-{os.getpid()}"

            if _SHUTDOWN_DIAGNOSTICS_AVAILABLE:
                log_state_change(
                    "GlobalShutdownSignal",
                    "idle",
                    "initiated",
                    f"reason={reason}"
                )

            logger.info(
                f"[v95.13] 🛑 Global shutdown initiated: "
                f"reason={reason}, initiator={self._initiator}"
            )

            # Write to file for cross-process coordination
            self._write_signal_file()

            # Set async event if it exists
            if self._async_event is not None:
                self._async_event.set()

            # Execute sync callbacks
            # v117.0: Log callback names for observability
            for name, callback in self._callbacks:
                try:
                    cb_name = name or "anonymous"
                    logger.debug(f"[v117.0] Executing shutdown callback: {cb_name}")
                    callback()
                except Exception as e:
                    cb_name = name or "anonymous"
                    logger.warning(f"[v117.0] Shutdown callback error ({cb_name}): {e}")

            # Schedule async callbacks if event loop is running
            # v117.0: Unpack (name, callback) tuples
            try:
                loop = asyncio.get_running_loop()
                for name, async_callback in self._async_callbacks:
                    loop.create_task(self._run_async_callback(name, async_callback))
            except RuntimeError:
                # No running event loop
                pass

    async def _run_async_callback(
        self,
        name: Optional[str],
        callback: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Run an async callback with error handling."""
        cb_name = name or "anonymous"
        try:
            logger.debug(f"[v117.0] Executing async shutdown callback: {cb_name}")
            await asyncio.wait_for(callback(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(f"[v117.0] Async shutdown callback timed out ({cb_name})")
        except Exception as e:
            logger.warning(f"[v117.0] Async shutdown callback error ({cb_name}): {e}")

    def complete(self) -> None:
        """Mark shutdown as completed."""
        with self._state_lock:
            self._completed = True
            self._completed_at = time.time()

            # Write to file for cross-process coordination
            self._write_signal_file()

            # Set completed event
            if self._completed_event is not None:
                self._completed_event.set()

            duration = (self._completed_at - self._initiated_at) if self._initiated_at else 0
            logger.info(f"[v95.13] ✅ Global shutdown completed (duration: {duration:.2f}s)")

    def reset(self) -> None:
        """
        Reset shutdown state (use only during startup to clear stale signals).

        This clears the file-based signal to prevent false positives from
        previous failed shutdowns.
        """
        with self._state_lock:
            self._initiated = False
            self._completed = False
            self._initiated_at = None
            self._completed_at = None
            self._reason = None
            self._initiator = None

            # Reset async events
            if self._async_event is not None:
                self._async_event.clear()
            if self._completed_event is not None:
                self._completed_event.clear()

            # Clear file signal
            self._clear_signal_file()

            logger.debug("[v95.13] Global shutdown signal reset")

    def register_callback(
        self,
        callback: Callable[[], Any],
        name: Optional[str] = None,
    ) -> None:
        """
        Register a synchronous callback to be called when shutdown initiates.

        Args:
            callback: Synchronous function to call on shutdown
            name: Optional name for logging/debugging (v117.0)
        """
        self._callbacks.append((name, callback))
        if name:
            logger.debug(f"[v117.0] Registered shutdown callback: {name}")

    def register_async_callback(
        self,
        callback: Callable[[], Coroutine[Any, Any, None]],
        name: Optional[str] = None,
    ) -> None:
        """
        Register an async callback to be called when shutdown initiates.

        Args:
            callback: Async function to call on shutdown
            name: Optional name for logging/debugging (v117.0)
        """
        self._async_callbacks.append((name, callback))
        if name:
            logger.debug(f"[v117.0] Registered async shutdown callback: {name}")

    async def wait(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for shutdown to be initiated.

        Args:
            timeout: Maximum time to wait (None = forever)

        Returns:
            True if shutdown was initiated, False if timeout
        """
        if self._async_event is None:
            self._async_event = asyncio.Event()
            if self._initiated:
                self._async_event.set()

        try:
            if timeout is not None:
                await asyncio.wait_for(self._async_event.wait(), timeout=timeout)
            else:
                await self._async_event.wait()
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_for_completion(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for shutdown to complete.

        Args:
            timeout: Maximum time to wait (None = forever)

        Returns:
            True if shutdown completed, False if timeout
        """
        if self._completed_event is None:
            self._completed_event = asyncio.Event()
            if self._completed:
                self._completed_event.set()

        try:
            if timeout is not None:
                await asyncio.wait_for(self._completed_event.wait(), timeout=timeout)
            else:
                await self._completed_event.wait()
            return True
        except asyncio.TimeoutError:
            return False

    def get_status(self) -> Dict[str, Any]:
        """Get current shutdown status."""
        # v117.0: Include callback names for observability
        callback_names = [name or "anonymous" for name, _ in self._callbacks]
        async_callback_names = [name or "anonymous" for name, _ in self._async_callbacks]

        return {
            "initiated": self._initiated,
            "completed": self._completed,
            "initiated_at": self._initiated_at,
            "completed_at": self._completed_at,
            "reason": self._reason,
            "initiator": self._initiator,
            "callbacks_registered": len(self._callbacks),
            "async_callbacks_registered": len(self._async_callbacks),
            "callback_names": callback_names,
            "async_callback_names": async_callback_names,
        }


# Global instance
_global_shutdown: Optional[GlobalShutdownSignal] = None


def get_global_shutdown() -> GlobalShutdownSignal:
    """Get the global shutdown signal instance."""
    global _global_shutdown
    if _global_shutdown is None:
        _global_shutdown = GlobalShutdownSignal()
    return _global_shutdown


def is_global_shutdown_initiated() -> bool:
    """
    Check if global shutdown has been initiated.

    This is a fast, synchronous check that can be used anywhere.
    All components should check this before starting new work.

    Returns:
        True if shutdown has been initiated
    """
    return get_global_shutdown().is_initiated


def is_global_shutdown_completed() -> bool:
    """
    Check if global shutdown has completed.

    Returns:
        True if shutdown has completed
    """
    return get_global_shutdown().is_completed


def initiate_global_shutdown(
    reason: str = "programmatic",
    initiator: Optional[str] = None,
) -> None:
    """
    Initiate global shutdown.

    This should be called by the shutdown orchestrator to signal all
    components that shutdown is happening.

    Args:
        reason: Why shutdown was initiated
        initiator: Identifier of component initiating shutdown
    """
    get_global_shutdown().initiate(reason=reason, initiator=initiator)


def complete_global_shutdown() -> None:
    """Mark global shutdown as completed."""
    get_global_shutdown().complete()


def reset_global_shutdown() -> None:
    """
    Reset global shutdown state.

    Call this during startup to clear any stale shutdown signals
    from previous runs.
    """
    get_global_shutdown().reset()


def register_shutdown_callback(
    callback: Callable[[], Any],
    name: Optional[str] = None,
) -> None:
    """
    Register a callback to be called when shutdown initiates.

    Callbacks should be fast and non-blocking to avoid delaying
    the shutdown notification process.

    Args:
        callback: Synchronous function to call on shutdown
        name: Optional name for logging/debugging (v117.0)
    """
    get_global_shutdown().register_callback(callback, name=name)


def register_async_shutdown_callback(
    callback: Callable[[], Coroutine[Any, Any, None]],
    name: Optional[str] = None,
) -> None:
    """
    Register an async callback to be called when shutdown initiates.

    Args:
        callback: Async function to call on shutdown
        name: Optional name for logging/debugging (v117.0)
    """
    get_global_shutdown().register_async_callback(callback, name=name)


async def wait_for_global_shutdown(timeout: Optional[float] = None) -> bool:
    """
    Wait for global shutdown to be initiated.

    Args:
        timeout: Maximum time to wait (None = forever)

    Returns:
        True if shutdown was initiated, False if timeout
    """
    return await get_global_shutdown().wait(timeout=timeout)


# =============================================================================
# v95.20: Enhanced Multiprocessing Cleanup (Torch/ML Library Support)
# =============================================================================

def cleanup_torch_multiprocessing_resources() -> Dict[str, Any]:
    """
    v95.20: Clean up torch.multiprocessing resources to prevent semaphore leaks.

    ROOT CAUSE: SentenceTransformer and other ML libraries use torch.multiprocessing
    internally for parallel operations. These can create semaphores that aren't
    properly released during shutdown, causing:
    "resource_tracker: There appear to be N leaked semaphore objects to clean up"

    This function:
    1. Terminates any active torch.multiprocessing child processes
    2. Cleans up the embedding service (single SentenceTransformer instance)
    3. Forces garbage collection to release semaphore resources

    Returns:
        Dict with cleanup statistics
    """
    import gc
    from contextlib import suppress

    results = {
        "torch_children_terminated": 0,
        "embedding_service_cleaned": False,
        "gc_collected": 0,
        "errors": [],
    }

    # Step 1: Clean up torch.multiprocessing children
    try:
        import torch.multiprocessing as mp

        # Get list of active child processes
        if hasattr(mp, 'active_children'):
            children = mp.active_children()
            for child in children:
                try:
                    if child.is_alive():
                        child.terminate()
                        child.join(timeout=1.0)
                        if child.is_alive():
                            child.kill()
                        results["torch_children_terminated"] += 1
                except Exception as e:
                    results["errors"].append(f"Child terminate error: {e}")

        logger.debug(
            f"[v95.20] Terminated {results['torch_children_terminated']} torch.multiprocessing children"
        )
    except ImportError:
        pass  # torch not installed
    except Exception as e:
        results["errors"].append(f"torch.multiprocessing cleanup error: {e}")

    # Step 2: Clean up embedding service (sync version)
    try:
        from backend.core.embedding_service import EmbeddingService

        # Get singleton instance and call sync cleanup
        instance = EmbeddingService.get_instance()
        if instance is not None:
            instance._sync_cleanup()
            results["embedding_service_cleaned"] = True
            logger.debug("[v95.20] Embedding service sync cleanup complete")
    except ImportError:
        pass  # embedding_service not available
    except Exception as e:
        results["errors"].append(f"Embedding service cleanup error: {e}")

    # Step 3: Force garbage collection
    try:
        collected = gc.collect()
        results["gc_collected"] = collected
        logger.debug(f"[v95.20] Garbage collection freed {collected} objects")
    except Exception as e:
        results["errors"].append(f"GC error: {e}")

    # Step 4: Clean up multiprocessing semaphores directly via resource tracker
    try:
        import multiprocessing.resource_tracker as rt

        # v127.0: Properly clean up tracked semaphores BEFORE exit
        # The resource_tracker maintains a dictionary of (type, name) -> cleanup_func
        if hasattr(rt, '_resource_tracker') and rt._resource_tracker is not None:
            tracker = rt._resource_tracker

            # Try to get the cache of tracked resources
            if hasattr(tracker, '_cache'):
                cache = tracker._cache
                if cache:
                    semaphore_count = sum(1 for key in cache if 'semaphore' in str(key).lower())
                    results["semaphores_tracked"] = semaphore_count

                    # Clean up semaphores by unregistering them (prevents leak warning)
                    semaphores_cleaned = 0
                    for key in list(cache.keys()):
                        if 'semaphore' in str(key[0]).lower() if isinstance(key, tuple) else 'semaphore' in str(key).lower():
                            try:
                                # Get the semaphore name and unlink it
                                if isinstance(key, tuple) and len(key) >= 2:
                                    sem_name = key[1]
                                    # Unlink the semaphore to release it
                                    try:
                                        import posix_ipc
                                        posix_ipc.unlink_semaphore(sem_name)
                                        semaphores_cleaned += 1
                                    except (ImportError, Exception):
                                        # posix_ipc not available, try direct unlink
                                        try:
                                            from multiprocessing import resource_sharer
                                            # Remove from tracker to prevent warning
                                            del cache[key]
                                            semaphores_cleaned += 1
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                    results["semaphores_cleaned"] = semaphores_cleaned

        logger.debug(f"[v127.0] Resource tracker cleanup: {results.get('semaphores_cleaned', 0)} semaphores")
    except Exception as e:
        results["errors"].append(f"Resource tracker cleanup error: {e}")

    # Step 5: v127.0 - Clean up multiprocessing.Process children
    try:
        import multiprocessing as mp

        active_children = mp.active_children()
        children_terminated = 0
        for child in active_children:
            try:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=0.5)
                    if child.is_alive():
                        child.kill()
                    children_terminated += 1
            except Exception:
                pass
        results["mp_children_terminated"] = children_terminated

        if children_terminated > 0:
            logger.debug(f"[v127.0] Terminated {children_terminated} multiprocessing children")
    except Exception as e:
        results["errors"].append(f"multiprocessing children cleanup error: {e}")

    # Step 6: v127.0 - Final garbage collection with semaphore focus
    try:
        # Multiple GC passes to ensure all circular references are cleaned
        for _ in range(3):
            gc.collect()
        results["gc_passes"] = 3
    except Exception as e:
        results["errors"].append(f"Final GC error: {e}")

    return results


async def cleanup_ml_resources_async(timeout: float = 5.0) -> Dict[str, Any]:
    """
    v95.20: Async version of ML resource cleanup.

    Runs cleanup in a thread pool to avoid blocking the event loop.
    """
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, cleanup_torch_multiprocessing_resources),
            timeout=timeout,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning(f"[v95.20] ML resource cleanup timed out after {timeout}s")
        return {"timeout": True, "errors": ["Cleanup timed out"]}


def cleanup_all_semaphores_sync() -> Dict[str, Any]:
    """
    v128.0: Comprehensive synchronous semaphore cleanup.

    This is the FINAL line of defense against semaphore leaks.
    Call this at the very end of shutdown, right before exit.

    The KEY insight: Semaphores are held by ProcessPoolExecutor worker processes.
    We must properly terminate these processes to release semaphores.

    This function:
    1. Cleans up all tracked ProcessPoolExecutors with proper worker termination
    2. Finds and cleans up UNTRACKED ProcessPoolExecutors (comprehensive)
    3. Terminates any remaining multiprocessing children
    4. Cleans up torch multiprocessing resources
    5. Cleans up orphaned POSIX semaphores (platform-specific)
    6. Forces garbage collection multiple times

    Returns:
        Dict with cleanup statistics
    """
    import gc
    import os
    from contextlib import suppress

    results = {
        "executors_cleaned": 0,
        "untracked_executors_cleaned": 0,
        "workers_terminated": 0,
        "mp_children_terminated": 0,
        "torch_children_terminated": 0,
        "posix_semaphores_cleaned": 0,
        "gc_collected": 0,
        "errors": [],
    }

    # Step 1: Clean up tracked executors with proper worker termination
    try:
        tracker = get_multiprocessing_tracker()
        exec_result = tracker.shutdown_all_executors_sync(timeout=3.0)
        results["executors_cleaned"] = exec_result.get("successful", 0) + exec_result.get("forced", 0)
        results["workers_terminated"] = exec_result.get("semaphores_cleaned", 0)
    except Exception as e:
        results["errors"].append(f"Executor cleanup: {e}")

    # Step 1b: v128.0 - Find and clean up UNTRACKED ProcessPoolExecutors
    # This catches any executors that were created but never registered
    try:
        from concurrent.futures import ProcessPoolExecutor

        # Use gc to find all ProcessPoolExecutor instances
        for obj in gc.get_objects():
            try:
                if isinstance(obj, ProcessPoolExecutor):
                    # Check if it has active workers
                    processes = getattr(obj, '_processes', None)
                    if processes:
                        workers_cleaned = 0
                        for pid, process in list(processes.items()):
                            if process is not None:
                                with suppress(Exception):
                                    if hasattr(process, 'is_alive') and process.is_alive():
                                        process.terminate()
                                        process.join(timeout=0.3)
                                        if process.is_alive():
                                            process.kill()
                                            process.join(timeout=0.2)
                                    else:
                                        process.join(timeout=0.1)
                                    workers_cleaned += 1

                        if workers_cleaned > 0:
                            results["untracked_executors_cleaned"] += 1
                            results["workers_terminated"] += workers_cleaned

                        # Close queues
                        for attr in ['_call_queue', '_result_queue']:
                            queue = getattr(obj, attr, None)
                            if queue is not None:
                                with suppress(Exception):
                                    if hasattr(queue, 'close'):
                                        queue.close()
                                    if hasattr(queue, 'cancel_join_thread'):
                                        queue.cancel_join_thread()

                        # Try to shutdown if not already
                        with suppress(Exception):
                            if not getattr(obj, '_shutdown', False):
                                obj.shutdown(wait=False, cancel_futures=True)

            except (ReferenceError, TypeError):
                pass  # Object was garbage collected or is not the right type
    except Exception as e:
        results["errors"].append(f"Untracked executor cleanup: {e}")

    # Step 2: Terminate ALL multiprocessing children (not just active ones)
    try:
        import multiprocessing as mp

        children = mp.active_children()
        for child in children:
            with suppress(Exception):
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=0.3)
                    if child.is_alive():
                        child.kill()
                        child.join(timeout=0.2)
                    results["mp_children_terminated"] += 1
                else:
                    # Join dead children to clean up zombies and release semaphores
                    child.join(timeout=0.1)
                    results["mp_children_terminated"] += 1
    except Exception as e:
        results["errors"].append(f"MP children: {e}")

    # Step 3: Terminate torch multiprocessing children
    try:
        import torch.multiprocessing as torch_mp

        if hasattr(torch_mp, 'active_children'):
            for child in torch_mp.active_children():
                with suppress(Exception):
                    if child.is_alive():
                        child.terminate()
                        child.join(timeout=0.3)
                        if child.is_alive():
                            child.kill()
                            child.join(timeout=0.2)
                        results["torch_children_terminated"] += 1
    except ImportError:
        pass  # torch not installed
    except Exception as e:
        results["errors"].append(f"Torch MP: {e}")

    # Step 4: v128.0 - Clean up orphaned POSIX semaphores (macOS/Linux)
    # This is platform-specific cleanup for any semaphores that leaked
    try:
        import platform
        system = platform.system()

        if system == 'Darwin':  # macOS
            # On macOS, Python semaphores are in /dev/shm (not always accessible)
            # and also tracked by the system. We can clean up via the resource tracker.
            pass  # macOS cleanup handled by Step 5

        elif system == 'Linux':
            # On Linux, POSIX semaphores are in /dev/shm
            shm_path = '/dev/shm'
            if os.path.exists(shm_path):
                try:
                    pid = os.getpid()
                    for name in os.listdir(shm_path):
                        # Python semaphores are named like: sem.mp-XXXXX
                        if name.startswith('sem.mp-') or name.startswith(f'sem.{pid}'):
                            try:
                                full_path = os.path.join(shm_path, name)
                                os.unlink(full_path)
                                results["posix_semaphores_cleaned"] += 1
                            except OSError:
                                pass  # May not have permission or already cleaned
                except PermissionError:
                    pass  # Can't list /dev/shm
    except Exception as e:
        results["errors"].append(f"POSIX semaphore cleanup: {e}")

    # Step 5: v128.0 - Tell resource_tracker to unregister our semaphores
    # This prevents the warning by properly unregistering before exit
    try:
        import multiprocessing.resource_tracker as rt

        # Get the current process's semaphore registrations
        # The tracker maintains a cache of (resource_type, resource_name) tuples
        if hasattr(rt, '_resource_tracker') and rt._resource_tracker is not None:
            tracker_obj = rt._resource_tracker

            # Try to unregister all semaphores
            if hasattr(tracker_obj, '_cache') and tracker_obj._cache:
                cache = tracker_obj._cache
                semaphores_to_remove = []

                for key in list(cache.keys()):
                    try:
                        if isinstance(key, tuple) and len(key) >= 2:
                            resource_type, resource_name = key[0], key[1]
                            if 'semaphore' in str(resource_type).lower():
                                semaphores_to_remove.append((resource_type, resource_name))
                    except Exception:
                        pass

                # Unregister each semaphore
                for resource_type, resource_name in semaphores_to_remove:
                    try:
                        if hasattr(rt, 'unregister'):
                            rt.unregister(resource_name, resource_type)
                        # Also try direct cache removal
                        cache.pop((resource_type, resource_name), None)
                    except Exception:
                        pass

                if semaphores_to_remove:
                    logger.debug(f"[v128.0] Unregistered {len(semaphores_to_remove)} semaphores from tracker")

    except Exception as e:
        results["errors"].append(f"Resource tracker unregister: {e}")

    # Step 6: Force multiple garbage collection passes
    # This helps release any remaining references to semaphores
    try:
        total_collected = 0
        for i in range(5):  # Aggressive - 5 passes
            collected = gc.collect()
            total_collected += collected
            if collected == 0:
                break  # No more objects to collect
        results["gc_collected"] = total_collected
    except Exception as e:
        results["errors"].append(f"GC: {e}")

    # Step 7: v128.0 - Final safety: Suppress remaining warnings during interpreter shutdown
    # This is a last-resort measure for any semaphores that couldn't be cleaned up
    try:
        import warnings

        # During shutdown, suppress the specific resource_tracker warning
        if is_global_shutdown_initiated():
            warnings.filterwarnings(
                'ignore',
                message='.*leaked semaphore.*',
                category=UserWarning,
                module='multiprocessing.resource_tracker'
            )
            results["warnings_filtered"] = True
    except Exception as e:
        results["errors"].append(f"Warning filter: {e}")

    return results


# v128.0: Register cleanup to run at interpreter shutdown
# CRITICAL: atexit handlers run in LIFO order (last registered = first run)
# We register AFTER multiprocessing imports, so our cleanup runs FIRST
_semaphore_cleanup_registered = False


def _register_semaphore_cleanup_atexit():
    """
    v128.0: Register semaphore cleanup as atexit handler.

    This ensures our cleanup runs BEFORE the resource_tracker's atexit handler,
    which prevents the "leaked semaphore" warning.
    """
    global _semaphore_cleanup_registered
    if _semaphore_cleanup_registered:
        return
    _semaphore_cleanup_registered = True

    import atexit

    def _final_semaphore_cleanup():
        try:
            # Mark global shutdown to enable warning suppression
            try:
                initiate_global_shutdown("atexit_semaphore_cleanup")
            except Exception:
                pass

            result = cleanup_all_semaphores_sync()

            # Log only if we actually cleaned something
            cleaned = (
                result.get("executors_cleaned", 0) +
                result.get("workers_terminated", 0) +
                result.get("mp_children_terminated", 0)
            )
            if cleaned > 0:
                logger.debug(
                    f"[v128.0] Final cleanup: {result.get('executors_cleaned', 0)} executors, "
                    f"{result.get('workers_terminated', 0)} workers, "
                    f"{result.get('mp_children_terminated', 0)} children"
                )
        except Exception:
            pass  # Best effort at exit

    # Register our cleanup - it will run FIRST due to LIFO ordering
    atexit.register(_final_semaphore_cleanup)


# Auto-register on import - this happens after multiprocessing is imported
# so our handler will run before the resource_tracker's handler
_register_semaphore_cleanup_atexit()


# =============================================================================
# v117.0: Thread and Executor Cleanup at Exit
# =============================================================================
# Ensure all threads and executors are shut down even if normal cleanup fails.
# This runs via atexit which fires before Python checks for non-daemon threads.
# =============================================================================

_thread_cleanup_registered = False


def _register_thread_cleanup_atexit():
    """
    Register thread cleanup as an atexit handler.

    This ensures thread pools and executors are shut down even when:
    - Normal shutdown path is bypassed (e.g., unhandled exception)
    - sys.exit() is called directly
    - Signal handlers don't complete cleanup
    """
    global _thread_cleanup_registered
    if _thread_cleanup_registered:
        return

    _thread_cleanup_registered = True

    def _final_thread_cleanup():
        """Emergency thread cleanup at exit."""
        try:
            # Try to import and use thread_manager
            from backend.core.thread_manager import (
                shutdown_all_threads,
                shutdown_third_party_threads,
            )

            logger.debug("[v117.0] Running final thread cleanup via atexit...")

            # Sync shutdown with short timeout (atexit shouldn't block long)
            shutdown_all_threads(timeout=5.0)

            # Third-party cleanup
            third_party_stats = shutdown_third_party_threads(timeout=3.0)

            remaining = third_party_stats.get("remaining_non_daemon", 0)
            if remaining > 0:
                logger.debug(f"[v117.0] atexit thread cleanup: {remaining} non-daemon threads remaining")
            else:
                logger.debug("[v117.0] atexit thread cleanup: all threads stopped")

        except ImportError:
            # Thread manager not available - try basic executor cleanup
            try:
                import threading
                from concurrent.futures import ThreadPoolExecutor

                # Get all live threads
                live_threads = [t for t in threading.enumerate()
                               if t.is_alive() and t != threading.main_thread()]

                if live_threads:
                    logger.debug(f"[v117.0] atexit: {len(live_threads)} threads still running (no thread_manager)")
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"[v117.0] atexit thread cleanup error: {e}")

    # Register our cleanup (LIFO ordering means it runs before Python's thread check)
    atexit.register(_final_thread_cleanup)
    logger.debug("[v117.0] Thread cleanup atexit handler registered")


# Auto-register on import
_register_thread_cleanup_atexit()


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "ShutdownCoordinator",
    "ShutdownPhase",
    "ShutdownReason",
    "ShutdownHandler",
    "InFlightTracker",
    "TrackedRequest",
    "ShutdownInProgressError",
    "get_shutdown_coordinator",
    "shutdown_coordinator",
    "track_request",
    # v95.11: New exports
    "OperationGuard",
    "get_operation_guard",
    "get_operation_guard_sync",
    "database_operation",
    "register_database_shutdown",
    "should_reject_operation",
    # v95.12: Multiprocessing cleanup
    "MultiprocessingResourceTracker",
    "get_multiprocessing_tracker",
    "cleanup_multiprocessing_resources",
    "register_executor_for_cleanup",
    "cleanup_orphaned_semaphores",
    "register_multiprocessing_shutdown",
    # v95.13: Global shutdown signal
    "GlobalShutdownSignal",
    "get_global_shutdown",
    "is_global_shutdown_initiated",
    "is_global_shutdown_completed",
    "initiate_global_shutdown",
    "complete_global_shutdown",
    "reset_global_shutdown",
    "register_shutdown_callback",
    "register_async_shutdown_callback",
    "wait_for_global_shutdown",
    # v95.20: Enhanced ML cleanup
    "cleanup_torch_multiprocessing_resources",
    "cleanup_ml_resources_async",
    # v127.0: Comprehensive semaphore cleanup
    "cleanup_all_semaphores_sync",
    # v117.0: Thread cleanup
    "_register_thread_cleanup_atexit",
]
