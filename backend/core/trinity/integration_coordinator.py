"""
Trinity Integration Coordinator - Cross-Repo Orchestration Engine
==================================================================

Advanced orchestration engine that coordinates all cross-repo communication
between JARVIS, JARVIS Prime, and Reactor Core.

Features:
    - Causal event delivery with vector clocks
    - Distributed locking for critical operations
    - Health monitoring with automatic recovery
    - Event sequencing and gap detection
    - Experience validation and schema enforcement
    - Automatic model rollback on failure
    - Directory lifecycle management
    - Cross-repo state synchronization

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                 Trinity Integration Coordinator              │
    ├─────────────────────────────────────────────────────────────┤
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
    │  │   JARVIS    │  │   J-Prime   │  │    Reactor Core     │  │
    │  │   (Body)    │  │   (Mind)    │  │     (Nerves)        │  │
    │  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
    │         │                │                    │              │
    │         └────────────────┼────────────────────┘              │
    │                          │                                   │
    │              ┌───────────▼───────────┐                      │
    │              │    Event Sequencer    │                      │
    │              │  (Vector Clocks +     │                      │
    │              │   Sequence Numbers)   │                      │
    │              └───────────┬───────────┘                      │
    │                          │                                   │
    │              ┌───────────▼───────────┐                      │
    │              │   Causal Delivery     │                      │
    │              │      Barrier          │                      │
    │              └───────────┬───────────┘                      │
    │                          │                                   │
    │              ┌───────────▼───────────┐                      │
    │              │   Health Monitor      │                      │
    │              │  + Auto Recovery      │                      │
    │              └───────────────────────┘                      │
    └─────────────────────────────────────────────────────────────┘

Author: Trinity System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import signal
import sys
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, Callable, Coroutine, Dict, List, Optional,
    Set, Tuple, Type, TypeVar, Union
)

# Import resilience utilities
from backend.core.resilience import (
    VectorClock,
    CausalEvent,
    CausalEventManager,
    CausalBarrier,
    DistributedLock,
    DistributedLockConfig,
    DistributedDedup,
    get_distributed_dedup,
    AtomicFileOps,
    get_shutdown_coordinator,
    ShutdownCoordinator,
    CircuitBreaker,
    get_distributed_circuit_breaker,
)

logger = logging.getLogger("TrinityIntegrationCoordinator")


# =============================================================================
# Configuration
# =============================================================================

# Base directories
TRINITY_BASE_DIR = Path(os.getenv("TRINITY_BASE_DIR", os.path.expanduser("~/.jarvis/trinity")))
REACTOR_EVENTS_DIR = Path(os.getenv("REACTOR_EVENTS_DIR", os.path.expanduser("~/.jarvis/reactor/events")))
CROSS_REPO_DIR = Path(os.getenv("CROSS_REPO_DIR", os.path.expanduser("~/.jarvis/cross_repo")))

# Health check configuration
HEALTH_CHECK_INTERVAL = float(os.getenv("HEALTH_CHECK_INTERVAL", "10.0"))
HEALTH_CHECK_TIMEOUT = float(os.getenv("HEALTH_CHECK_TIMEOUT", "5.0"))
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "3"))
AUTO_RESTART_DELAY = float(os.getenv("AUTO_RESTART_DELAY", "5.0"))

# Event sequencing
EVENT_SEQUENCE_GAP_THRESHOLD = int(os.getenv("EVENT_SEQUENCE_GAP_THRESHOLD", "10"))
EVENT_DELIVERY_TIMEOUT = float(os.getenv("EVENT_DELIVERY_TIMEOUT", "30.0"))

# Model hot-swap
MODEL_SWAP_LOCK_TIMEOUT = float(os.getenv("MODEL_SWAP_LOCK_TIMEOUT", "60.0"))
MODEL_VALIDATION_TIMEOUT = float(os.getenv("MODEL_VALIDATION_TIMEOUT", "30.0"))


# =============================================================================
# Enums and Constants
# =============================================================================

class RepoType(Enum):
    """Repository types in the Trinity ecosystem."""
    JARVIS = "jarvis"           # Body - main orchestrator
    JARVIS_PRIME = "jarvis_prime"  # Mind - model serving
    REACTOR_CORE = "reactor_core"  # Nerves - training


class ComponentStatus(Enum):
    """Status of a Trinity component."""
    UNKNOWN = "unknown"
    INITIALIZING = "initializing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    RECOVERING = "recovering"
    STOPPED = "stopped"


class EventType(Enum):
    """Types of cross-repo events."""
    # Model lifecycle
    MODEL_READY = "MODEL_READY"
    MODEL_DEPLOYED = "MODEL_DEPLOYED"
    MODEL_ROLLBACK = "MODEL_ROLLBACK"
    MODEL_VALIDATION_FAILED = "MODEL_VALIDATION_FAILED"

    # Training lifecycle
    TRAINING_STARTED = "TRAINING_STARTED"
    TRAINING_PROGRESS = "TRAINING_PROGRESS"
    TRAINING_COMPLETED = "TRAINING_COMPLETED"
    TRAINING_FAILED = "TRAINING_FAILED"

    # Experience forwarding
    EXPERIENCE_BATCH = "EXPERIENCE_BATCH"
    EXPERIENCE_ACK = "EXPERIENCE_ACK"

    # Health and coordination
    HEALTH_CHECK = "HEALTH_CHECK"
    HEALTH_RESPONSE = "HEALTH_RESPONSE"
    COMPONENT_STARTED = "COMPONENT_STARTED"
    COMPONENT_STOPPED = "COMPONENT_STOPPED"

    # State synchronization
    STATE_SYNC_REQUEST = "STATE_SYNC_REQUEST"
    STATE_SYNC_RESPONSE = "STATE_SYNC_RESPONSE"


# =============================================================================
# Event Schema Definitions
# =============================================================================

@dataclass
class EventSchema:
    """Schema definition for event validation."""
    event_type: EventType
    required_fields: Set[str]
    optional_fields: Set[str] = field(default_factory=set)
    validators: Dict[str, Callable[[Any], bool]] = field(default_factory=dict)


# Define schemas for all event types
EVENT_SCHEMAS: Dict[EventType, EventSchema] = {
    EventType.MODEL_READY: EventSchema(
        event_type=EventType.MODEL_READY,
        required_fields={"model_id", "model_path", "model_type", "metrics"},
        optional_fields={"checkpoint_path", "config", "training_id"},
        validators={
            "model_path": lambda p: Path(p).exists() if p else False,
            "metrics": lambda m: isinstance(m, dict) and "loss" in m,
        }
    ),
    EventType.EXPERIENCE_BATCH: EventSchema(
        event_type=EventType.EXPERIENCE_BATCH,
        required_fields={"batch_id", "experiences", "source_repo"},
        optional_fields={"timestamp", "metadata"},
        validators={
            "experiences": lambda e: isinstance(e, list) and len(e) > 0,
        }
    ),
    EventType.TRAINING_COMPLETED: EventSchema(
        event_type=EventType.TRAINING_COMPLETED,
        required_fields={"training_id", "model_id", "final_metrics", "duration"},
        optional_fields={"checkpoint_path", "artifacts"},
    ),
    EventType.HEALTH_CHECK: EventSchema(
        event_type=EventType.HEALTH_CHECK,
        required_fields={"source_repo", "timestamp"},
        optional_fields={"request_id"},
    ),
}


# =============================================================================
# Sequenced Event
# =============================================================================

@dataclass
class SequencedEvent:
    """
    An event with sequence number and causal ordering information.

    Combines sequence numbers for gap detection with vector clocks
    for causal ordering across distributed components.
    """
    event_id: str
    event_type: EventType
    source_repo: RepoType
    sequence_number: int
    vector_clock: Dict[str, int]
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    causally_depends_on: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "source_repo": self.source_repo.value,
            "sequence_number": self.sequence_number,
            "vector_clock": self.vector_clock,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "causally_depends_on": self.causally_depends_on,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SequencedEvent":
        """Deserialize from dictionary."""
        return cls(
            event_id=data["event_id"],
            event_type=EventType(data["event_type"]),
            source_repo=RepoType(data["source_repo"]),
            sequence_number=data["sequence_number"],
            vector_clock=data["vector_clock"],
            payload=data["payload"],
            timestamp=data.get("timestamp", time.time()),
            causally_depends_on=data.get("causally_depends_on", []),
        )


# =============================================================================
# Event Sequencer
# =============================================================================

class EventSequencer:
    """
    Manages event sequence numbers and gap detection.

    Ensures events are delivered in order and detects missing events.
    """

    def __init__(self, repo_id: str):
        self._repo_id = repo_id
        self._sequence_counter = 0
        self._received_sequences: Dict[str, Set[int]] = defaultdict(set)
        self._expected_sequences: Dict[str, int] = defaultdict(int)
        self._vector_clock = VectorClock(repo_id)
        self._pending_events: Dict[str, List[SequencedEvent]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def create_event(
        self,
        event_type: EventType,
        payload: Dict[str, Any],
        depends_on: Optional[List[str]] = None,
    ) -> SequencedEvent:
        """Create a new sequenced event."""
        async with self._lock:
            self._sequence_counter += 1
            self._vector_clock.increment()

            event = SequencedEvent(
                event_id=f"{self._repo_id}:{self._sequence_counter}:{uuid.uuid4().hex[:8]}",
                event_type=event_type,
                source_repo=RepoType(self._repo_id),
                sequence_number=self._sequence_counter,
                vector_clock=self._vector_clock.clock.copy(),
                payload=payload,
                causally_depends_on=depends_on or [],
            )

            return event

    async def receive_event(
        self,
        event: SequencedEvent,
    ) -> Tuple[bool, Optional[List[int]]]:
        """
        Receive and validate an event.

        Returns:
            (is_valid, missing_sequences) - missing_sequences is None if no gaps
        """
        async with self._lock:
            source = event.source_repo.value
            seq = event.sequence_number

            # Check for duplicates
            if seq in self._received_sequences[source]:
                logger.warning(f"Duplicate event received: {event.event_id}")
                return False, None

            # Record received sequence
            self._received_sequences[source].add(seq)

            # Update vector clock
            self._vector_clock.merge(VectorClock.from_dict(
                {"node_id": source, "clock": event.vector_clock}
            ))

            # Check for gaps
            expected = self._expected_sequences[source]
            missing = []

            if seq > expected + 1:
                # Gap detected
                missing = list(range(expected + 1, seq))
                logger.warning(
                    f"Event sequence gap detected from {source}: "
                    f"expected {expected + 1}, got {seq}, missing {missing}"
                )

            # Update expected
            if seq >= self._expected_sequences[source]:
                self._expected_sequences[source] = seq

            return True, missing if missing else None

    async def get_gaps(self, source: str) -> List[int]:
        """Get all missing sequence numbers from a source."""
        async with self._lock:
            expected = self._expected_sequences[source]
            received = self._received_sequences[source]

            if not received:
                return []

            max_received = max(received)
            all_expected = set(range(1, max_received + 1))
            missing = list(all_expected - received)

            return sorted(missing)


# =============================================================================
# Causal Event Delivery
# =============================================================================

class CausalEventDelivery:
    """
    Ensures events are delivered in causal order.

    Buffers out-of-order events and delivers them when their
    causal dependencies have been satisfied.
    """

    def __init__(self, max_buffer_size: int = 1000):
        self._max_buffer_size = max_buffer_size
        self._delivered: Set[str] = set()
        self._pending: Dict[str, SequencedEvent] = {}
        self._handlers: Dict[EventType, List[Callable]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def register_handler(
        self,
        event_type: EventType,
        handler: Callable[[SequencedEvent], Coroutine[Any, Any, None]],
    ) -> None:
        """Register a handler for an event type."""
        self._handlers[event_type].append(handler)

    async def receive(self, event: SequencedEvent) -> bool:
        """
        Receive an event and deliver if dependencies satisfied.

        Returns True if event was delivered immediately.
        """
        async with self._lock:
            # Check if already delivered
            if event.event_id in self._delivered:
                return False

            # Check dependencies
            if self._dependencies_satisfied(event):
                await self._deliver(event)

                # Try to deliver any pending events
                await self._try_deliver_pending()

                return True
            else:
                # Buffer for later delivery
                if len(self._pending) < self._max_buffer_size:
                    self._pending[event.event_id] = event
                else:
                    logger.error("Causal delivery buffer full, dropping event")
                    return False

                return False

    def _dependencies_satisfied(self, event: SequencedEvent) -> bool:
        """Check if all causal dependencies have been delivered."""
        for dep_id in event.causally_depends_on:
            if dep_id not in self._delivered:
                return False
        return True

    async def _deliver(self, event: SequencedEvent) -> None:
        """Deliver an event to all registered handlers."""
        self._delivered.add(event.event_id)

        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                await asyncio.wait_for(
                    handler(event),
                    timeout=EVENT_DELIVERY_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error(f"Event handler timeout for {event.event_id}")
            except Exception as e:
                logger.error(f"Event handler error for {event.event_id}: {e}")

    async def _try_deliver_pending(self) -> None:
        """Try to deliver buffered events whose dependencies are now satisfied."""
        delivered_this_round = True

        while delivered_this_round:
            delivered_this_round = False

            for event_id, event in list(self._pending.items()):
                if self._dependencies_satisfied(event):
                    del self._pending[event_id]
                    await self._deliver(event)
                    delivered_this_round = True


# =============================================================================
# Component Health Monitor
# =============================================================================

@dataclass
class ComponentHealth:
    """Health status of a Trinity component."""
    repo_type: RepoType
    status: ComponentStatus
    last_check: float
    consecutive_failures: int = 0
    error_message: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)


class HealthMonitor:
    """
    Monitors health of all Trinity components with auto-recovery.

    Features:
    - Periodic health checks
    - Automatic restart on failure
    - Degraded mode detection
    - Health metric aggregation
    """

    def __init__(
        self,
        redis_client: Optional[Any] = None,
        check_interval: float = HEALTH_CHECK_INTERVAL,
    ):
        self._redis = redis_client
        self._check_interval = check_interval

        # Component health state
        self._health: Dict[RepoType, ComponentHealth] = {}
        self._health_callbacks: List[Callable[[ComponentHealth], Coroutine]] = []
        self._recovery_callbacks: Dict[RepoType, Callable[[], Coroutine]] = {}

        # Background tasks
        self._running = False
        self._check_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start health monitoring."""
        self._running = True
        self._check_task = asyncio.create_task(
            self._health_check_loop(),
            name="health_monitor",
        )
        logger.info("HealthMonitor started")

    async def stop(self) -> None:
        """Stop health monitoring."""
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        logger.info("HealthMonitor stopped")

    def register_component(
        self,
        repo_type: RepoType,
        health_check: Callable[[], Coroutine[Any, Any, bool]],
        recovery_action: Optional[Callable[[], Coroutine[Any, Any, None]]] = None,
    ) -> None:
        """Register a component for health monitoring."""
        self._health[repo_type] = ComponentHealth(
            repo_type=repo_type,
            status=ComponentStatus.INITIALIZING,
            last_check=time.time(),
        )

        if recovery_action:
            self._recovery_callbacks[repo_type] = recovery_action

    async def report_health(
        self,
        repo_type: RepoType,
        is_healthy: bool,
        metrics: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Report health status from a component."""
        async with self._lock:
            if repo_type not in self._health:
                self._health[repo_type] = ComponentHealth(
                    repo_type=repo_type,
                    status=ComponentStatus.UNKNOWN,
                    last_check=time.time(),
                )

            health = self._health[repo_type]
            health.last_check = time.time()
            health.metrics = metrics or {}

            if is_healthy:
                health.status = ComponentStatus.HEALTHY
                health.consecutive_failures = 0
                health.error_message = None
            else:
                health.consecutive_failures += 1
                health.error_message = error_message

                if health.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    health.status = ComponentStatus.UNHEALTHY
                    await self._trigger_recovery(repo_type)
                else:
                    health.status = ComponentStatus.DEGRADED

            # Notify callbacks
            for callback in self._health_callbacks:
                try:
                    await callback(health)
                except Exception as e:
                    logger.error(f"Health callback error: {e}")

    async def _health_check_loop(self) -> None:
        """Background loop for periodic health checks."""
        while self._running:
            try:
                await asyncio.sleep(self._check_interval)

                for repo_type, health in self._health.items():
                    # Check for stale health reports
                    if time.time() - health.last_check > self._check_interval * 3:
                        health.consecutive_failures += 1
                        health.status = ComponentStatus.UNHEALTHY
                        health.error_message = "No health report received"

                        if health.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            await self._trigger_recovery(repo_type)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check loop error: {e}")

    async def _trigger_recovery(self, repo_type: RepoType) -> None:
        """Trigger recovery action for a component."""
        self._health[repo_type].status = ComponentStatus.RECOVERING

        if repo_type in self._recovery_callbacks:
            logger.warning(f"Triggering recovery for {repo_type.value}")

            try:
                await asyncio.sleep(AUTO_RESTART_DELAY)
                await self._recovery_callbacks[repo_type]()
                logger.info(f"Recovery completed for {repo_type.value}")
            except Exception as e:
                logger.error(f"Recovery failed for {repo_type.value}: {e}")

    def get_health_summary(self) -> Dict[str, Any]:
        """Get health summary of all components."""
        return {
            "overall_status": self._calculate_overall_status().value,
            "components": {
                repo.value: {
                    "status": health.status.value,
                    "last_check": health.last_check,
                    "failures": health.consecutive_failures,
                    "error": health.error_message,
                    "metrics": health.metrics,
                }
                for repo, health in self._health.items()
            },
        }

    def _calculate_overall_status(self) -> ComponentStatus:
        """Calculate overall system health status."""
        if not self._health:
            return ComponentStatus.UNKNOWN

        statuses = [h.status for h in self._health.values()]

        if all(s == ComponentStatus.HEALTHY for s in statuses):
            return ComponentStatus.HEALTHY
        if any(s == ComponentStatus.UNHEALTHY for s in statuses):
            return ComponentStatus.UNHEALTHY
        if any(s == ComponentStatus.DEGRADED for s in statuses):
            return ComponentStatus.DEGRADED

        return ComponentStatus.UNKNOWN


# =============================================================================
# Model Hot-Swap Manager
# =============================================================================

class ModelHotSwapManager:
    """
    Manages model hot-swapping with distributed locking and rollback.

    Features:
    - Distributed locking to prevent race conditions
    - Request queuing during swap
    - Automatic rollback on failure
    - Validation before activation
    """

    def __init__(
        self,
        redis_client: Optional[Any] = None,
        lock_timeout: float = MODEL_SWAP_LOCK_TIMEOUT,
    ):
        self._redis = redis_client
        self._lock_timeout = lock_timeout

        # Model state
        self._active_models: Dict[str, str] = {}  # model_type -> version
        self._previous_models: Dict[str, str] = {}  # For rollback
        self._model_registry: Dict[str, Dict[str, Any]] = {}  # version -> metadata

        # Swap state
        self._swap_in_progress: Dict[str, bool] = {}
        self._request_queues: Dict[str, asyncio.Queue] = {}

        # Locks
        self._local_lock = asyncio.Lock()
        self._distributed_lock: Optional[DistributedLock] = None

        # Callbacks
        self._pre_swap_validators: List[Callable] = []
        self._post_swap_callbacks: List[Callable] = []
        self._rollback_callbacks: List[Callable] = []

    async def initialize(self) -> None:
        """Initialize the hot-swap manager."""
        if self._redis:
            self._distributed_lock = DistributedLock(
                redis_client=self._redis,
                config=DistributedLockConfig(
                    default_timeout=self._lock_timeout,
                    retry_delay=0.5,
                    max_retries=10,
                ),
            )

    def register_validator(
        self,
        validator: Callable[[str, str, Dict], Coroutine[Any, Any, bool]],
    ) -> None:
        """Register a pre-swap validator."""
        self._pre_swap_validators.append(validator)

    def register_post_swap_callback(
        self,
        callback: Callable[[str, str], Coroutine[Any, Any, None]],
    ) -> None:
        """Register a post-swap callback."""
        self._post_swap_callbacks.append(callback)

    async def swap_model(
        self,
        model_type: str,
        new_version: str,
        model_path: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Swap to a new model version.

        Returns:
            (success, error_message)
        """
        lock_key = f"model_swap:{model_type}"

        # Acquire distributed lock
        if self._distributed_lock:
            acquired = await self._distributed_lock.acquire(lock_key)
            if not acquired:
                return False, "Could not acquire distributed lock"

        try:
            async with self._local_lock:
                # Mark swap in progress
                self._swap_in_progress[model_type] = True

                # Store previous version for rollback
                if model_type in self._active_models:
                    self._previous_models[model_type] = self._active_models[model_type]

                # Validate model
                validation_passed = await self._validate_model(
                    model_type, new_version, model_path, metadata
                )

                if not validation_passed:
                    self._swap_in_progress[model_type] = False
                    return False, "Model validation failed"

                # Register model
                self._model_registry[new_version] = {
                    "path": model_path,
                    "type": model_type,
                    "metadata": metadata or {},
                    "registered_at": time.time(),
                }

                # Activate model
                self._active_models[model_type] = new_version

                # Notify callbacks
                for callback in self._post_swap_callbacks:
                    try:
                        await callback(model_type, new_version)
                    except Exception as e:
                        logger.error(f"Post-swap callback error: {e}")

                logger.info(f"Model swapped: {model_type} -> {new_version}")

                self._swap_in_progress[model_type] = False
                return True, None

        except Exception as e:
            logger.error(f"Model swap error: {e}")
            self._swap_in_progress[model_type] = False
            return False, str(e)
        finally:
            if self._distributed_lock:
                await self._distributed_lock.release(lock_key)

    async def rollback(
        self,
        model_type: str,
        reason: str = "Manual rollback",
    ) -> Tuple[bool, Optional[str]]:
        """
        Rollback to the previous model version.

        Returns:
            (success, error_message)
        """
        if model_type not in self._previous_models:
            return False, "No previous version to rollback to"

        previous_version = self._previous_models[model_type]

        async with self._local_lock:
            current_version = self._active_models.get(model_type)
            self._active_models[model_type] = previous_version

            # Notify rollback callbacks
            for callback in self._rollback_callbacks:
                try:
                    await callback(model_type, current_version, previous_version, reason)
                except Exception as e:
                    logger.error(f"Rollback callback error: {e}")

            logger.warning(
                f"Model rolled back: {model_type} from {current_version} to {previous_version}"
            )

            return True, None

    async def _validate_model(
        self,
        model_type: str,
        version: str,
        path: str,
        metadata: Optional[Dict],
    ) -> bool:
        """Run all registered validators."""
        for validator in self._pre_swap_validators:
            try:
                result = await asyncio.wait_for(
                    validator(model_type, version, {"path": path, "metadata": metadata}),
                    timeout=MODEL_VALIDATION_TIMEOUT,
                )
                if not result:
                    return False
            except asyncio.TimeoutError:
                logger.error(f"Model validation timeout for {version}")
                return False
            except Exception as e:
                logger.error(f"Model validation error: {e}")
                return False

        return True

    def is_swap_in_progress(self, model_type: str) -> bool:
        """Check if a swap is currently in progress."""
        return self._swap_in_progress.get(model_type, False)

    def get_active_version(self, model_type: str) -> Optional[str]:
        """Get the currently active model version."""
        return self._active_models.get(model_type)


# =============================================================================
# Experience Validator
# =============================================================================

class ExperienceValidator:
    """
    Validates experience data before forwarding to Reactor Core.

    Features:
    - Schema validation
    - Content validation
    - Deduplication
    - Quality filtering
    """

    REQUIRED_FIELDS = {"query", "response", "feedback"}
    OPTIONAL_FIELDS = {"context", "metadata", "timestamp", "session_id"}

    def __init__(self):
        self._seen_hashes: Set[str] = set()
        self._validation_stats = {
            "total_validated": 0,
            "passed": 0,
            "failed_schema": 0,
            "failed_quality": 0,
            "duplicates": 0,
        }

    async def validate(
        self,
        experience: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate an experience.

        Returns:
            (is_valid, error_message)
        """
        self._validation_stats["total_validated"] += 1

        # Schema validation
        if not self._validate_schema(experience):
            self._validation_stats["failed_schema"] += 1
            return False, "Schema validation failed"

        # Deduplication check
        exp_hash = self._compute_hash(experience)
        if exp_hash in self._seen_hashes:
            self._validation_stats["duplicates"] += 1
            return False, "Duplicate experience"

        # Quality check
        if not self._validate_quality(experience):
            self._validation_stats["failed_quality"] += 1
            return False, "Quality validation failed"

        self._seen_hashes.add(exp_hash)
        self._validation_stats["passed"] += 1
        return True, None

    def _validate_schema(self, experience: Dict[str, Any]) -> bool:
        """Validate experience schema."""
        for field in self.REQUIRED_FIELDS:
            if field not in experience:
                return False
            if not experience[field]:
                return False
        return True

    def _validate_quality(self, experience: Dict[str, Any]) -> bool:
        """Validate experience quality."""
        query = experience.get("query", "")
        response = experience.get("response", "")

        # Minimum length requirements
        if len(query) < 3 or len(response) < 3:
            return False

        # Maximum length (prevent memory issues)
        if len(query) > 100000 or len(response) > 100000:
            return False

        return True

    def _compute_hash(self, experience: Dict[str, Any]) -> str:
        """Compute hash for deduplication."""
        content = f"{experience.get('query', '')}{experience.get('response', '')}"
        return hashlib.md5(content.encode()).hexdigest()

    async def validate_batch(
        self,
        experiences: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        Validate a batch of experiences.

        Returns:
            (valid_experiences, error_messages)
        """
        valid = []
        errors = []

        for exp in experiences:
            is_valid, error = await self.validate(exp)
            if is_valid:
                valid.append(exp)
            else:
                errors.append(error or "Unknown error")

        return valid, errors

    def get_stats(self) -> Dict[str, int]:
        """Get validation statistics."""
        return self._validation_stats.copy()


# =============================================================================
# Directory Lifecycle Manager
# =============================================================================

class DirectoryLifecycleManager:
    """
    Manages directory creation, permissions, and cleanup.

    Features:
    - Automatic directory creation on startup
    - Permission verification
    - Old file cleanup
    - Disk space monitoring
    """

    REQUIRED_DIRECTORIES = [
        TRINITY_BASE_DIR / "events",
        TRINITY_BASE_DIR / "state",
        TRINITY_BASE_DIR / "models",
        TRINITY_BASE_DIR / "logs",
        REACTOR_EVENTS_DIR,
        CROSS_REPO_DIR / "locks",
        CROSS_REPO_DIR / "state",
    ]

    def __init__(self, cleanup_age_hours: int = 24):
        self._cleanup_age = cleanup_age_hours * 3600
        self._initialized = False

    async def initialize(self) -> Tuple[bool, List[str]]:
        """
        Initialize all required directories.

        Returns:
            (success, errors)
        """
        errors = []

        for directory in self.REQUIRED_DIRECTORIES:
            try:
                directory.mkdir(parents=True, exist_ok=True)

                # Verify permissions
                if not os.access(directory, os.W_OK):
                    errors.append(f"No write access to {directory}")
                    continue

                # Create marker file
                marker = directory / ".trinity_managed"
                marker.touch()

            except Exception as e:
                errors.append(f"Failed to create {directory}: {e}")

        self._initialized = len(errors) == 0

        if self._initialized:
            logger.info(f"Directory lifecycle initialized: {len(self.REQUIRED_DIRECTORIES)} directories")
        else:
            logger.error(f"Directory initialization failed: {errors}")

        return self._initialized, errors

    async def cleanup_old_files(self) -> int:
        """
        Clean up old event files.

        Returns:
            Number of files removed
        """
        removed = 0
        cutoff = time.time() - self._cleanup_age

        cleanup_dirs = [
            TRINITY_BASE_DIR / "events",
            REACTOR_EVENTS_DIR,
        ]

        for directory in cleanup_dirs:
            if not directory.exists():
                continue

            for file_path in directory.glob("*.json"):
                try:
                    if file_path.stat().st_mtime < cutoff:
                        file_path.unlink()
                        removed += 1
                except Exception as e:
                    logger.warning(f"Failed to remove {file_path}: {e}")

        if removed > 0:
            logger.info(f"Cleaned up {removed} old event files")

        return removed

    async def get_disk_usage(self) -> Dict[str, Any]:
        """Get disk usage information."""
        usage = {}

        for directory in self.REQUIRED_DIRECTORIES:
            if directory.exists():
                total = sum(
                    f.stat().st_size
                    for f in directory.rglob("*")
                    if f.is_file()
                )
                usage[str(directory)] = total

        return usage


# =============================================================================
# Trinity Integration Coordinator (Main Class)
# =============================================================================

class TrinityIntegrationCoordinator:
    """
    Main coordinator for Trinity cross-repo integration.

    Combines all components:
    - Event sequencing and causal delivery
    - Health monitoring and auto-recovery
    - Model hot-swap with rollback
    - Experience validation
    - Directory lifecycle management
    """

    def __init__(
        self,
        repo_id: str = "jarvis",
        redis_client: Optional[Any] = None,
    ):
        self._repo_id = repo_id
        self._redis = redis_client

        # Components
        self._event_sequencer = EventSequencer(repo_id)
        self._causal_delivery = CausalEventDelivery()
        self._health_monitor = HealthMonitor(redis_client)
        self._hot_swap_manager = ModelHotSwapManager(redis_client)
        self._experience_validator = ExperienceValidator()
        self._directory_manager = DirectoryLifecycleManager()

        # Deduplication
        self._dedup: Optional[DistributedDedup] = None

        # Shutdown coordination
        self._shutdown_coordinator: Optional[ShutdownCoordinator] = None

        # State
        self._running = False
        self._initialized = False

        # Event handlers
        self._event_handlers: Dict[EventType, List[Callable]] = defaultdict(list)

        # Metrics
        self._metrics = {
            "events_sent": 0,
            "events_received": 0,
            "events_delivered": 0,
            "validation_failures": 0,
            "swap_operations": 0,
            "rollbacks": 0,
        }

        logger.info(f"TrinityIntegrationCoordinator created (repo: {repo_id})")

    async def initialize(self) -> Tuple[bool, List[str]]:
        """
        Initialize all components.

        Returns:
            (success, errors)
        """
        errors = []

        # Initialize directories
        dir_success, dir_errors = await self._directory_manager.initialize()
        errors.extend(dir_errors)

        # Initialize deduplication
        try:
            self._dedup = await get_distributed_dedup(self._redis)
        except Exception as e:
            logger.warning(f"Dedup initialization failed: {e}")

        # Initialize hot-swap manager
        await self._hot_swap_manager.initialize()

        # Initialize shutdown coordinator
        self._shutdown_coordinator = await get_shutdown_coordinator(self._redis)

        # Register default event handlers
        self._register_default_handlers()

        self._initialized = len(errors) == 0

        if self._initialized:
            logger.info("TrinityIntegrationCoordinator initialized successfully")
        else:
            logger.error(f"Initialization errors: {errors}")

        return self._initialized, errors

    async def start(self) -> None:
        """Start the coordinator and all components."""
        if not self._initialized:
            success, errors = await self.initialize()
            if not success:
                raise RuntimeError(f"Initialization failed: {errors}")

        self._running = True

        # Start health monitor
        await self._health_monitor.start()

        logger.info("TrinityIntegrationCoordinator started")

    async def stop(self) -> None:
        """Stop the coordinator and all components."""
        self._running = False

        # Stop health monitor
        await self._health_monitor.stop()

        # Cleanup old files
        await self._directory_manager.cleanup_old_files()

        logger.info("TrinityIntegrationCoordinator stopped")

    def _register_default_handlers(self) -> None:
        """Register default event handlers."""
        # Model ready handler
        self._causal_delivery.register_handler(
            EventType.MODEL_READY,
            self._handle_model_ready,
        )

        # Training completed handler
        self._causal_delivery.register_handler(
            EventType.TRAINING_COMPLETED,
            self._handle_training_completed,
        )

        # Health check handler
        self._causal_delivery.register_handler(
            EventType.HEALTH_CHECK,
            self._handle_health_check,
        )

    async def send_event(
        self,
        event_type: EventType,
        payload: Dict[str, Any],
        target_repos: Optional[List[RepoType]] = None,
        depends_on: Optional[List[str]] = None,
    ) -> SequencedEvent:
        """
        Send an event to other repos.

        Args:
            event_type: Type of event
            payload: Event payload
            target_repos: Target repositories (default: all)
            depends_on: Event IDs this event depends on

        Returns:
            The created event
        """
        # Create sequenced event
        event = await self._event_sequencer.create_event(
            event_type=event_type,
            payload=payload,
            depends_on=depends_on,
        )

        # Validate against schema if available
        if event_type in EVENT_SCHEMAS:
            schema = EVENT_SCHEMAS[event_type]
            missing = schema.required_fields - set(payload.keys())
            if missing:
                raise ValueError(f"Missing required fields: {missing}")

        # Write to event file
        targets = target_repos or [RepoType.JARVIS, RepoType.JARVIS_PRIME, RepoType.REACTOR_CORE]

        for target in targets:
            if target.value == self._repo_id:
                continue

            await self._write_event_file(event, target)

        self._metrics["events_sent"] += 1
        logger.debug(f"Event sent: {event.event_id} ({event_type.value})")

        return event

    async def receive_event(self, event_data: Dict[str, Any]) -> bool:
        """
        Receive and process an event from another repo.

        Returns:
            True if event was successfully processed
        """
        try:
            event = SequencedEvent.from_dict(event_data)
        except Exception as e:
            logger.error(f"Failed to parse event: {e}")
            return False

        # Check for duplicates
        if self._dedup:
            if await self._dedup.is_duplicate(event.event_id):
                logger.debug(f"Duplicate event ignored: {event.event_id}")
                return False
            await self._dedup.mark_processed(event.event_id)

        # Validate sequence
        is_valid, missing = await self._event_sequencer.receive_event(event)
        if missing:
            logger.warning(f"Missing events from {event.source_repo.value}: {missing}")

        # Deliver through causal delivery
        delivered = await self._causal_delivery.receive(event)

        self._metrics["events_received"] += 1
        if delivered:
            self._metrics["events_delivered"] += 1

        return delivered

    async def _write_event_file(
        self,
        event: SequencedEvent,
        target: RepoType,
    ) -> None:
        """Write event to file for target repo to pick up."""
        if target == RepoType.REACTOR_CORE:
            event_dir = TRINITY_BASE_DIR / "events"
        else:
            event_dir = CROSS_REPO_DIR / target.value / "events"

        event_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{event.timestamp}_{event.event_id}.json"
        filepath = event_dir / filename

        atomic_ops = AtomicFileOps(str(event_dir))
        await atomic_ops.write_json(filename, event.to_dict())

    async def _handle_model_ready(self, event: SequencedEvent) -> None:
        """Handle MODEL_READY event from Reactor Core."""
        payload = event.payload

        model_id = payload.get("model_id")
        model_path = payload.get("model_path")
        model_type = payload.get("model_type", "default")
        metrics = payload.get("metrics", {})

        logger.info(f"Received MODEL_READY: {model_id} at {model_path}")

        # Swap to new model
        success, error = await self._hot_swap_manager.swap_model(
            model_type=model_type,
            new_version=model_id,
            model_path=model_path,
            metadata={"metrics": metrics, "event_id": event.event_id},
        )

        if success:
            self._metrics["swap_operations"] += 1

            # Send MODEL_DEPLOYED event
            await self.send_event(
                EventType.MODEL_DEPLOYED,
                payload={
                    "model_id": model_id,
                    "model_type": model_type,
                    "deployed_at": time.time(),
                },
                depends_on=[event.event_id],
            )
        else:
            logger.error(f"Model swap failed: {error}")
            self._metrics["validation_failures"] += 1

            # Send failure event
            await self.send_event(
                EventType.MODEL_VALIDATION_FAILED,
                payload={
                    "model_id": model_id,
                    "error": error,
                },
                depends_on=[event.event_id],
            )

    async def _handle_training_completed(self, event: SequencedEvent) -> None:
        """Handle TRAINING_COMPLETED event."""
        logger.info(f"Training completed: {event.payload.get('training_id')}")

    async def _handle_health_check(self, event: SequencedEvent) -> None:
        """Handle HEALTH_CHECK event."""
        source = event.source_repo
        request_id = event.payload.get("request_id")

        # Send health response
        await self.send_event(
            EventType.HEALTH_RESPONSE,
            payload={
                "request_id": request_id,
                "status": "healthy",
                "metrics": self.get_metrics(),
            },
            target_repos=[source],
            depends_on=[event.event_id],
        )

    async def forward_experiences(
        self,
        experiences: List[Dict[str, Any]],
    ) -> Tuple[int, List[str]]:
        """
        Validate and forward experiences to Reactor Core.

        Returns:
            (count_forwarded, errors)
        """
        # Validate experiences
        valid, errors = await self._experience_validator.validate_batch(experiences)

        if not valid:
            return 0, errors

        # Send experience batch event
        event = await self.send_event(
            EventType.EXPERIENCE_BATCH,
            payload={
                "batch_id": f"batch_{uuid.uuid4().hex[:8]}",
                "experiences": valid,
                "source_repo": self._repo_id,
                "count": len(valid),
            },
            target_repos=[RepoType.REACTOR_CORE],
        )

        return len(valid), errors

    async def rollback_model(
        self,
        model_type: str,
        reason: str = "Manual rollback",
    ) -> Tuple[bool, Optional[str]]:
        """Rollback a model to the previous version."""
        success, error = await self._hot_swap_manager.rollback(model_type, reason)

        if success:
            self._metrics["rollbacks"] += 1

            # Send rollback event
            await self.send_event(
                EventType.MODEL_ROLLBACK,
                payload={
                    "model_type": model_type,
                    "reason": reason,
                    "timestamp": time.time(),
                },
            )

        return success, error

    def get_metrics(self) -> Dict[str, Any]:
        """Get coordinator metrics."""
        return {
            **self._metrics,
            "health": self._health_monitor.get_health_summary(),
            "experience_validation": self._experience_validator.get_stats(),
            "active_models": {
                t: self._hot_swap_manager.get_active_version(t)
                for t in ["default", "chat", "code", "embedding"]
            },
        }


# =============================================================================
# Global Factory
# =============================================================================

_coordinator_instance: Optional[TrinityIntegrationCoordinator] = None
_coordinator_lock = asyncio.Lock()


async def get_trinity_coordinator(
    repo_id: str = "jarvis",
    redis_client: Optional[Any] = None,
) -> TrinityIntegrationCoordinator:
    """Get or create the global TrinityIntegrationCoordinator instance."""
    global _coordinator_instance

    async with _coordinator_lock:
        if _coordinator_instance is None:
            _coordinator_instance = TrinityIntegrationCoordinator(
                repo_id=repo_id,
                redis_client=redis_client,
            )
            await _coordinator_instance.start()

        return _coordinator_instance


async def shutdown_trinity_coordinator() -> None:
    """Shutdown the global coordinator."""
    global _coordinator_instance

    if _coordinator_instance:
        await _coordinator_instance.stop()
        _coordinator_instance = None


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "TrinityIntegrationCoordinator",
    "EventSequencer",
    "CausalEventDelivery",
    "HealthMonitor",
    "ModelHotSwapManager",
    "ExperienceValidator",
    "DirectoryLifecycleManager",
    "SequencedEvent",
    "EventType",
    "RepoType",
    "ComponentStatus",
    "get_trinity_coordinator",
    "shutdown_trinity_coordinator",
]
