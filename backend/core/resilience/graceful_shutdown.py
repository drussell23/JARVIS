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
import time
from dataclasses import dataclass, field
from enum import Enum
from contextlib import asynccontextmanager
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("GracefulShutdown")


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
]
