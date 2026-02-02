#!/usr/bin/env python3
"""
Orchestrator-Narrator Bridge v1.0 - Real-Time Event-Driven Integration
=======================================================================

Bridges the cross_repo_startup_orchestrator with the startup_narrator to provide
real-time voice feedback during all startup operations.

Features:
- Event-driven architecture with asyncio for real-time updates
- Docker operation announcements (build, start, health checks)
- GCP VM provisioning and routing announcements
- Circuit breaker state change notifications
- Memory-aware routing decision explanations
- Parallel startup coordination
- Timeout and retry announcements
- Error recovery narration
- Cross-repo health synchronization feedback
- Adaptive announcement throttling

Advanced Patterns:
- Observer pattern for event subscription
- Circuit breaker pattern for fault tolerance
- Exponential backoff for retries
- Event debouncing to prevent spam
- Priority-based announcement queuing
- Semantic deduplication

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import (
    Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple, Union
)

logger = logging.getLogger(__name__)


# =============================================================================
# Event Types
# =============================================================================

class OrchestratorEvent(str, Enum):
    """Events emitted by the cross_repo_startup_orchestrator."""
    # Startup lifecycle
    STARTUP_BEGIN = "startup_begin"
    STARTUP_PROGRESS = "startup_progress"
    STARTUP_COMPLETE = "startup_complete"
    STARTUP_FAILED = "startup_failed"
    STARTUP_TIMEOUT = "startup_timeout"

    # Service lifecycle
    SERVICE_SPAWNING = "service_spawning"
    SERVICE_HEALTHY = "service_healthy"
    SERVICE_UNHEALTHY = "service_unhealthy"
    SERVICE_CRASHED = "service_crashed"
    SERVICE_RECOVERED = "service_recovered"
    SERVICE_RESTARTING = "service_restarting"

    # Docker operations
    DOCKER_CHECK = "docker_check"
    DOCKER_FOUND = "docker_found"
    DOCKER_NOT_FOUND = "docker_not_found"
    DOCKER_STARTING = "docker_starting"
    DOCKER_BUILD_START = "docker_build_start"
    DOCKER_BUILD_PROGRESS = "docker_build_progress"
    DOCKER_BUILD_COMPLETE = "docker_build_complete"
    DOCKER_BUILD_FAILED = "docker_build_failed"
    DOCKER_HEALTH_CHECK = "docker_health_check"
    DOCKER_HEALTHY = "docker_healthy"
    DOCKER_UNHEALTHY = "docker_unhealthy"

    # GCP operations
    GCP_ROUTING_DECISION = "gcp_routing_decision"
    GCP_VM_CREATING = "gcp_vm_creating"
    GCP_VM_STARTING = "gcp_vm_starting"
    GCP_VM_READY = "gcp_vm_ready"
    GCP_VM_FAILED = "gcp_vm_failed"
    GCP_SPOT_ALLOCATED = "gcp_spot_allocated"
    GCP_FALLBACK = "gcp_fallback"

    # Memory-aware routing
    MEMORY_CHECK = "memory_check"
    MEMORY_PRESSURE_HIGH = "memory_pressure_high"
    MEMORY_PRESSURE_CRITICAL = "memory_pressure_critical"
    ROUTING_LOCAL = "routing_local"
    ROUTING_DOCKER = "routing_docker"
    ROUTING_GCP = "routing_gcp"
    ROUTING_FALLBACK = "routing_fallback"

    # Circuit breaker
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    CIRCUIT_BREAKER_HALF_OPEN = "circuit_breaker_half_open"
    CIRCUIT_BREAKER_CLOSED = "circuit_breaker_closed"
    CIRCUIT_BREAKER_TRIP = "circuit_breaker_trip"

    # Health monitoring
    HEALTH_CHECK_START = "health_check_start"
    HEALTH_CHECK_PASS = "health_check_pass"
    HEALTH_CHECK_FAIL = "health_check_fail"
    HEALTH_DEGRADED = "health_degraded"
    HEALTH_RECOVERED = "health_recovered"

    # Cross-repo synchronization
    CROSS_REPO_SYNC_START = "cross_repo_sync_start"
    CROSS_REPO_SYNC_PROGRESS = "cross_repo_sync_progress"
    CROSS_REPO_SYNC_COMPLETE = "cross_repo_sync_complete"
    CROSS_REPO_HEARTBEAT = "cross_repo_heartbeat"
    CROSS_REPO_DISCONNECT = "cross_repo_disconnect"
    CROSS_REPO_RECONNECT = "cross_repo_reconnect"

    # v95.10: Cross-Repo Integration Systems
    CROSS_REPO_CONFIG_INIT = "cross_repo_config_init"
    CROSS_REPO_CONFIG_LOADED = "cross_repo_config_loaded"
    CROSS_REPO_CONFIG_SYNCED = "cross_repo_config_synced"
    CROSS_REPO_LOGGING_INIT = "cross_repo_logging_init"
    CROSS_REPO_LOGGING_ACTIVE = "cross_repo_logging_active"
    CROSS_REPO_METRICS_INIT = "cross_repo_metrics_init"
    CROSS_REPO_METRICS_ACTIVE = "cross_repo_metrics_active"
    CROSS_REPO_ERROR_PROPAGATION_INIT = "cross_repo_error_propagation_init"
    CROSS_REPO_ERROR_PROPAGATION_ACTIVE = "cross_repo_error_propagation_active"
    CROSS_REPO_STATE_INIT = "cross_repo_state_init"
    CROSS_REPO_STATE_SYNCED = "cross_repo_state_synced"
    CROSS_REPO_RESOURCE_INIT = "cross_repo_resource_init"
    CROSS_REPO_RESOURCE_ACTIVE = "cross_repo_resource_active"
    CROSS_REPO_VERSION_CHECK = "cross_repo_version_check"
    CROSS_REPO_VERSION_COMPATIBLE = "cross_repo_version_compatible"
    CROSS_REPO_VERSION_INCOMPATIBLE = "cross_repo_version_incompatible"
    CROSS_REPO_SECURITY_INIT = "cross_repo_security_init"
    CROSS_REPO_SECURITY_ACTIVE = "cross_repo_security_active"
    CROSS_REPO_INTEGRATION_COMPLETE = "cross_repo_integration_complete"

    # Retry and recovery
    RETRY_ATTEMPT = "retry_attempt"
    RETRY_BACKOFF = "retry_backoff"
    RETRY_EXHAUSTED = "retry_exhausted"
    RECOVERY_START = "recovery_start"
    RECOVERY_SUCCESS = "recovery_success"
    RECOVERY_FAILED = "recovery_failed"

    # Model loading
    MODEL_LOADING_START = "model_loading_start"
    MODEL_LOADING_PROGRESS = "model_loading_progress"
    MODEL_LOADING_COMPLETE = "model_loading_complete"
    MODEL_LOADING_TIMEOUT = "model_loading_timeout"
    MODEL_LOADING_EXTENDED = "model_loading_extended"

    # Parallel startup
    PARALLEL_STARTUP_BEGIN = "parallel_startup_begin"
    PARALLEL_SERVICE_READY = "parallel_service_ready"
    PARALLEL_STARTUP_COMPLETE = "parallel_startup_complete"


class AnnouncementPriority(Enum):
    """Priority levels for announcements."""
    DEBUG = auto()      # Only log, never speak
    LOW = auto()        # Can be skipped during rapid events
    MEDIUM = auto()     # Standard announcements
    HIGH = auto()       # Important milestones
    CRITICAL = auto()   # Must announce (errors, completion)
    URGENT = auto()     # Interrupt current speech


# =============================================================================
# Event Data Structures
# =============================================================================

@dataclass
class OrchestratorEventData:
    """Data payload for orchestrator events."""
    event: OrchestratorEvent
    timestamp: float = field(default_factory=time.time)
    service_name: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    priority: AnnouncementPriority = AnnouncementPriority.MEDIUM

    # Progress tracking
    progress_percent: Optional[float] = None
    elapsed_seconds: Optional[float] = None
    remaining_seconds: Optional[float] = None

    # Error information
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    retry_count: Optional[int] = None
    max_retries: Optional[int] = None


@dataclass
class BridgeConfig:
    """Configuration for the orchestrator-narrator bridge."""

    # Enable/disable features
    enabled: bool = field(
        default_factory=lambda: os.getenv(
            "ORCHESTRATOR_NARRATOR_BRIDGE_ENABLED", "true"
        ).lower() == "true"
    )
    voice_announcements: bool = field(
        default_factory=lambda: os.getenv(
            "BRIDGE_VOICE_ANNOUNCEMENTS", "true"
        ).lower() == "true"
    )
    console_logging: bool = field(
        default_factory=lambda: os.getenv(
            "BRIDGE_CONSOLE_LOGGING", "true"
        ).lower() == "true"
    )

    # Announcement throttling (seconds)
    min_announcement_interval: float = field(
        default_factory=lambda: float(os.getenv(
            "BRIDGE_MIN_ANNOUNCEMENT_INTERVAL", "2.0"
        ))
    )
    debounce_window: float = field(
        default_factory=lambda: float(os.getenv(
            "BRIDGE_DEBOUNCE_WINDOW", "0.5"
        ))
    )

    # Priority thresholds
    min_voice_priority: AnnouncementPriority = AnnouncementPriority.MEDIUM

    # Event history
    max_event_history: int = 100
    deduplication_window: float = 30.0  # Seconds to check for duplicates


# =============================================================================
# Message Templates
# =============================================================================

EVENT_MESSAGE_TEMPLATES: Dict[OrchestratorEvent, Dict[str, List[str]]] = {
    # Startup lifecycle
    OrchestratorEvent.STARTUP_BEGIN: {
        "default": [
            "Beginning startup sequence.",
            "Initiating system startup.",
            "Starting up all services.",
        ],
    },
    OrchestratorEvent.STARTUP_COMPLETE: {
        "default": [
            "All services started successfully.",
            "Startup complete. All systems operational.",
            "Full system startup achieved.",
        ],
    },
    OrchestratorEvent.STARTUP_FAILED: {
        "default": [
            "Startup encountered failures. Some services may be unavailable.",
            "Startup did not complete successfully. Checking diagnostics.",
        ],
    },
    OrchestratorEvent.STARTUP_TIMEOUT: {
        "default": [
            "Startup is taking longer than expected.",
            "Startup timeout reached. Continuing with available services.",
        ],
    },

    # Service lifecycle
    OrchestratorEvent.SERVICE_SPAWNING: {
        "jarvis-prime": [
            "Spawning JARVIS Prime. The Mind is awakening.",
            "Starting JARVIS Prime service. Cognitive layer initializing.",
        ],
        "reactor-core": [
            "Spawning Reactor Core. The Nerves are activating.",
            "Starting Reactor Core service. Learning layer preparing.",
        ],
        "default": [
            "Spawning {service_name} service.",
            "Starting {service_name}.",
        ],
    },
    OrchestratorEvent.SERVICE_HEALTHY: {
        "jarvis-prime": [
            "JARVIS Prime is healthy and responsive.",
            "The Mind is online. JARVIS Prime ready.",
        ],
        "reactor-core": [
            "Reactor Core is healthy. Learning systems active.",
            "The Nerves are online. Reactor Core ready.",
        ],
        "default": [
            "{service_name} is now healthy.",
            "{service_name} service is operational.",
        ],
    },
    OrchestratorEvent.SERVICE_CRASHED: {
        "default": [
            "{service_name} has crashed unexpectedly.",
            "Service failure detected in {service_name}.",
        ],
    },
    OrchestratorEvent.SERVICE_RESTARTING: {
        "default": [
            "Restarting {service_name} service.",
            "Attempting to recover {service_name}.",
        ],
    },

    # Docker operations
    OrchestratorEvent.DOCKER_CHECK: {
        "default": [
            "Checking for Docker containers.",
            "Scanning for available Docker services.",
        ],
    },
    OrchestratorEvent.DOCKER_FOUND: {
        "default": [
            "Docker container found for {service_name}.",
            "Using Docker for {service_name}.",
        ],
    },
    OrchestratorEvent.DOCKER_BUILD_START: {
        "default": [
            "Building Docker image for {service_name}. This may take a moment.",
            "Starting Docker build. Please wait.",
        ],
    },
    OrchestratorEvent.DOCKER_BUILD_COMPLETE: {
        "default": [
            "Docker build complete for {service_name}.",
            "Docker image ready.",
        ],
    },
    OrchestratorEvent.DOCKER_BUILD_FAILED: {
        "default": [
            "Docker build failed. Falling back to local execution.",
            "Unable to build Docker image. Using alternative method.",
        ],
    },

    # GCP operations
    OrchestratorEvent.GCP_ROUTING_DECISION: {
        "default": [
            "Routing {service_name} to Google Cloud Platform.",
            "Using GCP for {service_name} due to resource requirements.",
        ],
    },
    OrchestratorEvent.GCP_VM_CREATING: {
        "default": [
            "Creating GCP virtual machine for {service_name}.",
            "Provisioning cloud resources. This may take a minute.",
        ],
    },
    OrchestratorEvent.GCP_VM_READY: {
        "default": [
            "GCP virtual machine is ready for {service_name}.",
            "Cloud resources provisioned successfully.",
        ],
    },
    OrchestratorEvent.GCP_SPOT_ALLOCATED: {
        "default": [
            "Using spot instance for cost optimization.",
            "Allocated preemptible VM for {service_name}.",
        ],
    },

    # Memory-aware routing
    OrchestratorEvent.MEMORY_PRESSURE_HIGH: {
        "default": [
            "High memory usage detected. Adjusting resource allocation.",
            "Memory pressure is elevated. Optimizing service routing.",
        ],
    },
    OrchestratorEvent.MEMORY_PRESSURE_CRITICAL: {
        "default": [
            "Critical memory pressure. Some services may be offloaded to cloud.",
            "System memory is constrained. Activating cloud fallback.",
        ],
    },
    OrchestratorEvent.ROUTING_LOCAL: {
        "default": [
            "Running {service_name} locally. Sufficient resources available.",
        ],
    },
    OrchestratorEvent.ROUTING_DOCKER: {
        "default": [
            "Running {service_name} in Docker for isolation.",
        ],
    },
    OrchestratorEvent.ROUTING_GCP: {
        "default": [
            "Routing {service_name} to GCP. Local resources insufficient.",
        ],
    },

    # Circuit breaker
    OrchestratorEvent.CIRCUIT_BREAKER_OPEN: {
        "default": [
            "Circuit breaker open for {service_name}. Preventing cascade failures.",
            "Service protection activated for {service_name}.",
        ],
    },
    OrchestratorEvent.CIRCUIT_BREAKER_HALF_OPEN: {
        "default": [
            "Testing {service_name} recovery. Circuit breaker half-open.",
        ],
    },
    OrchestratorEvent.CIRCUIT_BREAKER_CLOSED: {
        "default": [
            "Circuit breaker closed. {service_name} fully recovered.",
            "{service_name} is stable. Resuming normal operations.",
        ],
    },

    # Health monitoring
    OrchestratorEvent.HEALTH_DEGRADED: {
        "default": [
            "System health degraded. Some services are not responding.",
            "Partial system availability. Checking affected services.",
        ],
    },
    OrchestratorEvent.HEALTH_RECOVERED: {
        "default": [
            "System health restored. All services responding.",
            "Full health recovered. All systems operational.",
        ],
    },

    # Cross-repo synchronization
    OrchestratorEvent.CROSS_REPO_SYNC_START: {
        "default": [
            "Synchronizing state across repositories.",
            "Beginning cross-repo synchronization.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_SYNC_COMPLETE: {
        "default": [
            "Cross-repository synchronization complete.",
            "All repositories are in sync.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_DISCONNECT: {
        "default": [
            "Lost connection to {service_name}. Attempting to reconnect.",
            "Cross-repo link to {service_name} interrupted.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_RECONNECT: {
        "default": [
            "Reconnected to {service_name}.",
            "Cross-repo link to {service_name} restored.",
        ],
    },

    # Retry and recovery
    OrchestratorEvent.RETRY_ATTEMPT: {
        "default": [
            "Retry attempt {retry_count} of {max_retries} for {service_name}.",
            "Retrying {service_name}. Attempt {retry_count}.",
        ],
    },
    OrchestratorEvent.RETRY_BACKOFF: {
        "default": [
            "Waiting before retry. Backoff in progress.",
        ],
    },
    OrchestratorEvent.RETRY_EXHAUSTED: {
        "default": [
            "All retry attempts exhausted for {service_name}.",
            "Unable to recover {service_name} after {max_retries} attempts.",
        ],
    },
    OrchestratorEvent.RECOVERY_SUCCESS: {
        "default": [
            "{service_name} recovered successfully.",
            "Recovery complete for {service_name}.",
        ],
    },

    # Model loading
    OrchestratorEvent.MODEL_LOADING_START: {
        "default": [
            "Loading AI models. This may take a moment.",
            "Model initialization in progress.",
        ],
    },
    OrchestratorEvent.MODEL_LOADING_PROGRESS: {
        "default": [
            "Model loading {progress_percent:.0f}% complete.",
        ],
    },
    OrchestratorEvent.MODEL_LOADING_COMPLETE: {
        "default": [
            "AI models loaded successfully.",
            "Model initialization complete.",
        ],
    },
    OrchestratorEvent.MODEL_LOADING_TIMEOUT: {
        "default": [
            "Model loading is taking longer than expected.",
            "Extended model load time detected.",
        ],
    },
    OrchestratorEvent.MODEL_LOADING_EXTENDED: {
        "default": [
            "Extending model loading timeout. Progress detected.",
            "Model still loading. Patience appreciated.",
        ],
    },

    # Parallel startup
    OrchestratorEvent.PARALLEL_STARTUP_BEGIN: {
        "default": [
            "Starting services in parallel for faster startup.",
            "Parallel initialization beginning.",
        ],
    },
    OrchestratorEvent.PARALLEL_SERVICE_READY: {
        "default": [
            "{service_name} ready. {remaining} services remaining.",
        ],
    },
    OrchestratorEvent.PARALLEL_STARTUP_COMPLETE: {
        "default": [
            "All parallel services are ready.",
            "Parallel startup complete.",
        ],
    },

    # v95.10: Cross-Repo Integration Systems
    OrchestratorEvent.CROSS_REPO_CONFIG_INIT: {
        "default": [
            "Initializing unified configuration system.",
            "Loading cross-repository configuration.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_CONFIG_LOADED: {
        "default": [
            "Configuration loaded from all repositories.",
            "Unified config assembled.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_CONFIG_SYNCED: {
        "default": [
            "Configuration synchronized across repositories.",
            "Config changes propagated.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_LOGGING_INIT: {
        "default": [
            "Initializing unified logging with distributed tracing.",
            "Setting up cross-repo log correlation.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_LOGGING_ACTIVE: {
        "default": [
            "Unified logging active. All logs correlated.",
            "Distributed tracing enabled.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_METRICS_INIT: {
        "default": [
            "Initializing cross-repo metrics collection.",
            "Setting up unified telemetry.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_METRICS_ACTIVE: {
        "default": [
            "Metrics collection active across all systems.",
            "Unified telemetry online.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_ERROR_PROPAGATION_INIT: {
        "default": [
            "Initializing error propagation system.",
            "Setting up error correlation.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_ERROR_PROPAGATION_ACTIVE: {
        "default": [
            "Error propagation active. Failures will be tracked.",
            "Error correlation enabled.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_STATE_INIT: {
        "default": [
            "Initializing distributed state synchronization.",
            "Setting up shared state management.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_STATE_SYNCED: {
        "default": [
            "Shared state synchronized.",
            "Distributed state ready.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_RESOURCE_INIT: {
        "default": [
            "Initializing resource coordination.",
            "Setting up resource allocation.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_RESOURCE_ACTIVE: {
        "default": [
            "Resource coordination active.",
            "Fair allocation enabled.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_VERSION_CHECK: {
        "default": [
            "Checking version compatibility.",
            "Verifying cross-repo versions.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_VERSION_COMPATIBLE: {
        "default": [
            "All versions compatible.",
            "Version check passed.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_VERSION_INCOMPATIBLE: {
        "default": [
            "Version incompatibility detected. Some features may be limited.",
            "Cross-repo version mismatch. Running in compatibility mode.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_SECURITY_INIT: {
        "default": [
            "Initializing cross-repo security context.",
            "Setting up secure inter-service communication.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_SECURITY_ACTIVE: {
        "default": [
            "Security context established. Tokens active.",
            "Cross-repo authentication enabled.",
        ],
    },
    OrchestratorEvent.CROSS_REPO_INTEGRATION_COMPLETE: {
        "default": [
            "Cross-repository integration complete. All systems unified.",
            "Full cross-repo coordination established.",
        ],
    },
}


# =============================================================================
# Event Handlers
# =============================================================================

EventHandler = Callable[[OrchestratorEventData], Coroutine[Any, Any, None]]


class EventBus:
    """
    Async event bus for orchestrator events.

    Implements the Observer pattern with priority-based delivery
    and automatic cleanup of dead references.
    """

    def __init__(self):
        self._handlers: Dict[OrchestratorEvent, List[weakref.ref]] = {}
        self._global_handlers: List[weakref.ref] = []
        self._lock = asyncio.Lock()
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._processing = False
        self._task: Optional[asyncio.Task] = None

    async def subscribe(
        self,
        event: Optional[OrchestratorEvent],
        handler: EventHandler
    ) -> Callable[[], None]:
        """
        Subscribe to an event or all events (if event is None).

        Returns an unsubscribe function.
        """
        async with self._lock:
            ref = weakref.ref(handler)

            if event is None:
                self._global_handlers.append(ref)
            else:
                if event not in self._handlers:
                    self._handlers[event] = []
                self._handlers[event].append(ref)

        def unsubscribe():
            asyncio.create_task(self._unsubscribe(event, ref))

        return unsubscribe

    async def _unsubscribe(
        self,
        event: Optional[OrchestratorEvent],
        ref: weakref.ref
    ) -> None:
        async with self._lock:
            if event is None:
                if ref in self._global_handlers:
                    self._global_handlers.remove(ref)
            elif event in self._handlers:
                if ref in self._handlers[event]:
                    self._handlers[event].remove(ref)

    async def publish(self, event_data: OrchestratorEventData) -> None:
        """Publish an event to all subscribed handlers."""
        await self._event_queue.put(event_data)

        if not self._processing:
            self._task = asyncio.create_task(self._process_events())

    async def _process_events(self) -> None:
        """Process events from the queue."""
        self._processing = True

        try:
            while not self._event_queue.empty():
                event_data = await self._event_queue.get()
                await self._dispatch(event_data)
                self._event_queue.task_done()
        finally:
            self._processing = False

    async def _dispatch(self, event_data: OrchestratorEventData) -> None:
        """Dispatch event to handlers."""
        handlers_to_call = []

        async with self._lock:
            # Collect global handlers
            for ref in self._global_handlers[:]:
                handler = ref()
                if handler is None:
                    self._global_handlers.remove(ref)
                else:
                    handlers_to_call.append(handler)

            # Collect event-specific handlers
            if event_data.event in self._handlers:
                for ref in self._handlers[event_data.event][:]:
                    handler = ref()
                    if handler is None:
                        self._handlers[event_data.event].remove(ref)
                    else:
                        handlers_to_call.append(handler)

        # Call handlers concurrently
        if handlers_to_call:
            await asyncio.gather(
                *[handler(event_data) for handler in handlers_to_call],
                return_exceptions=True
            )


# =============================================================================
# Orchestrator-Narrator Bridge
# =============================================================================

class OrchestratorNarratorBridge:
    """
    Bridge between cross_repo_startup_orchestrator and startup_narrator.

    Provides real-time voice announcements for all orchestrator events.
    """

    _instance: Optional['OrchestratorNarratorBridge'] = None
    # v193.0: Lazy lock initialization to avoid RuntimeError at module load time
    # when no event loop is running. Lock is created on first access.
    _lock: Optional[asyncio.Lock] = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        """Get or create the class lock (lazy initialization for async safety)."""
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    def __init__(self, config: Optional[BridgeConfig] = None):
        self.config = config or BridgeConfig()
        self.event_bus = EventBus()

        # Announcement state
        self._last_announcement_time: float = 0
        self._announcement_history: deque = deque(maxlen=self.config.max_event_history)
        # v193.0: Lazy initialization for asyncio primitives
        self._pending_announcements: Optional[asyncio.Queue] = None
        self._debounce_timers: Dict[str, asyncio.Task] = {}

        # Narrator reference (lazy loaded)
        self._narrator = None
        # v193.0: Lazy lock initialization
        self._narrator_lock: Optional[asyncio.Lock] = None

    def _get_narrator_lock(self) -> asyncio.Lock:
        """Get or create narrator lock (lazy initialization)."""
        if self._narrator_lock is None:
            self._narrator_lock = asyncio.Lock()
        return self._narrator_lock

    def _get_pending_queue(self) -> asyncio.Queue:
        """Get or create pending announcements queue (lazy initialization)."""
        if self._pending_announcements is None:
            self._pending_announcements = asyncio.Queue()
        return self._pending_announcements

        # Statistics
        self._stats = {
            "events_received": 0,
            "announcements_made": 0,
            "announcements_skipped": 0,
            "announcements_debounced": 0,
        }

        # Processing state
        self._running = False
        self._announcement_task: Optional[asyncio.Task] = None

    @classmethod
    async def get_instance(cls) -> 'OrchestratorNarratorBridge':
        """Get or create singleton instance."""
        async with cls._get_lock():
            if cls._instance is None:
                cls._instance = cls()
                await cls._instance.start()
            return cls._instance

    async def start(self) -> None:
        """Start the bridge."""
        if self._running:
            return

        self._running = True

        # Subscribe to all events
        await self.event_bus.subscribe(None, self._handle_event)

        # Start announcement processor
        self._announcement_task = asyncio.create_task(
            self._process_announcements()
        )

        logger.info("[OrchestratorNarratorBridge] Started")

    async def stop(self) -> None:
        """Stop the bridge."""
        self._running = False

        if self._announcement_task:
            self._announcement_task.cancel()
            try:
                await self._announcement_task
            except asyncio.CancelledError:
                pass

        # Cancel debounce timers
        for timer in self._debounce_timers.values():
            timer.cancel()

        logger.info("[OrchestratorNarratorBridge] Stopped")

    async def emit(self, event_data: OrchestratorEventData) -> None:
        """Emit an event from the orchestrator."""
        if not self.config.enabled:
            return

        self._stats["events_received"] += 1
        await self.event_bus.publish(event_data)

    async def _handle_event(self, event_data: OrchestratorEventData) -> None:
        """Handle an orchestrator event."""
        # Generate announcement message
        message = self._generate_message(event_data)

        if not message:
            return

        # Check for deduplication
        if self._is_duplicate(event_data):
            self._stats["announcements_skipped"] += 1
            return

        # Add to history
        self._announcement_history.append(event_data)

        # Debounce similar events
        debounce_key = f"{event_data.event.value}:{event_data.service_name or 'global'}"

        if debounce_key in self._debounce_timers:
            self._debounce_timers[debounce_key].cancel()
            self._stats["announcements_debounced"] += 1

        # Schedule debounced announcement
        self._debounce_timers[debounce_key] = asyncio.create_task(
            self._debounced_announce(debounce_key, event_data, message)
        )

    async def _debounced_announce(
        self,
        key: str,
        event_data: OrchestratorEventData,
        message: str
    ) -> None:
        """Announce after debounce window."""
        try:
            await asyncio.sleep(self.config.debounce_window)
            await self._get_pending_queue().put((event_data, message))
        except asyncio.CancelledError:
            pass
        finally:
            if key in self._debounce_timers:
                del self._debounce_timers[key]

    async def _process_announcements(self) -> None:
        """Process pending announcements with throttling."""
        while self._running:
            try:
                event_data, message = await asyncio.wait_for(
                    self._get_pending_queue().get(),
                    timeout=1.0
                )

                # Throttle announcements
                time_since_last = time.time() - self._last_announcement_time
                if time_since_last < self.config.min_announcement_interval:
                    wait_time = self.config.min_announcement_interval - time_since_last
                    # Skip low-priority announcements if we're throttling
                    if event_data.priority.value < AnnouncementPriority.HIGH.value:
                        self._stats["announcements_skipped"] += 1
                        continue
                    await asyncio.sleep(wait_time)

                await self._announce(event_data, message)
                self._last_announcement_time = time.time()
                self._stats["announcements_made"] += 1

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[OrchestratorNarratorBridge] Announcement error: {e}")

    async def _announce(
        self,
        event_data: OrchestratorEventData,
        message: str
    ) -> None:
        """Make an announcement via the narrator."""
        # Console logging
        if self.config.console_logging:
            level = self._get_log_level(event_data.priority)
            logger.log(level, f"[Orchestrator] {message}")

        # Voice announcement
        if (
            self.config.voice_announcements
            and event_data.priority.value >= self.config.min_voice_priority.value
        ):
            narrator = await self._get_narrator()
            if narrator:
                try:
                    # Route to appropriate narrator method based on event
                    await self._route_to_narrator(narrator, event_data, message)
                except Exception as e:
                    logger.debug(f"[OrchestratorNarratorBridge] Narrator error: {e}")

    async def _get_narrator(self):
        """Get or create narrator instance (lazy loading)."""
        async with self._get_narrator_lock():
            if self._narrator is None:
                try:
                    from .startup_narrator import get_startup_narrator
                    self._narrator = get_startup_narrator()  # Not async
                except Exception as e:
                    logger.debug(f"[OrchestratorNarratorBridge] Could not get narrator: {e}")
                    return None
            return self._narrator

    def _generate_message(self, event_data: OrchestratorEventData) -> Optional[str]:
        """Generate announcement message from event data."""
        import random

        templates = EVENT_MESSAGE_TEMPLATES.get(event_data.event, {})

        # Try service-specific template first
        if event_data.service_name and event_data.service_name in templates:
            message_list = templates[event_data.service_name]
        elif "default" in templates:
            message_list = templates["default"]
        else:
            return None

        if not message_list:
            return None

        # Select random template
        template = random.choice(message_list)

        # Format with event data
        try:
            return template.format(
                service_name=event_data.service_name or "service",
                progress_percent=event_data.progress_percent or 0,
                elapsed_seconds=event_data.elapsed_seconds or 0,
                remaining_seconds=event_data.remaining_seconds or 0,
                retry_count=event_data.retry_count or 0,
                max_retries=event_data.max_retries or 3,
                remaining=event_data.details.get("remaining", 0),
                **event_data.details
            )
        except KeyError:
            return template

    def _is_duplicate(self, event_data: OrchestratorEventData) -> bool:
        """Check if event is a duplicate within deduplication window."""
        cutoff_time = time.time() - self.config.deduplication_window

        for past_event in self._announcement_history:
            if past_event.timestamp < cutoff_time:
                continue

            if (
                past_event.event == event_data.event
                and past_event.service_name == event_data.service_name
            ):
                return True

        return False

    def _get_log_level(self, priority: AnnouncementPriority) -> int:
        """Map priority to log level."""
        mapping = {
            AnnouncementPriority.DEBUG: logging.DEBUG,
            AnnouncementPriority.LOW: logging.DEBUG,
            AnnouncementPriority.MEDIUM: logging.INFO,
            AnnouncementPriority.HIGH: logging.INFO,
            AnnouncementPriority.CRITICAL: logging.WARNING,
            AnnouncementPriority.URGENT: logging.ERROR,
        }
        return mapping.get(priority, logging.INFO)

    def _map_to_startup_phase(self, event: OrchestratorEvent) -> Optional[str]:
        """Map orchestrator event to startup phase for narrator."""
        # Import here to avoid circular imports
        try:
            from .startup_narrator import StartupPhase
        except ImportError:
            return None

        mapping = {
            OrchestratorEvent.SERVICE_SPAWNING: StartupPhase.SPAWNING,
            OrchestratorEvent.SERVICE_HEALTHY: StartupPhase.COMPLETE,
            OrchestratorEvent.DOCKER_CHECK: StartupPhase.DOCKER,
            OrchestratorEvent.DOCKER_FOUND: StartupPhase.DOCKER,
            OrchestratorEvent.DOCKER_STARTING: StartupPhase.DOCKER,
            OrchestratorEvent.MODEL_LOADING_START: StartupPhase.MODELS,
            OrchestratorEvent.MODEL_LOADING_COMPLETE: StartupPhase.MODELS,
            OrchestratorEvent.HEALTH_DEGRADED: StartupPhase.PARTIAL,
            OrchestratorEvent.HEALTH_RECOVERED: StartupPhase.RECOVERY,
            OrchestratorEvent.CROSS_REPO_SYNC_START: StartupPhase.CROSS_REPO_INIT,
            OrchestratorEvent.CROSS_REPO_SYNC_COMPLETE: StartupPhase.TRINITY_SYNC,
        }

        phase = mapping.get(event)
        return phase.value if phase else None

    def _map_priority(self, priority: AnnouncementPriority):
        """Map announcement priority to narrator priority."""
        try:
            from .startup_narrator import NarrationPriority
        except ImportError:
            return None

        mapping = {
            AnnouncementPriority.DEBUG: NarrationPriority.LOW,
            AnnouncementPriority.LOW: NarrationPriority.LOW,
            AnnouncementPriority.MEDIUM: NarrationPriority.MEDIUM,
            AnnouncementPriority.HIGH: NarrationPriority.HIGH,
            AnnouncementPriority.CRITICAL: NarrationPriority.CRITICAL,
            AnnouncementPriority.URGENT: NarrationPriority.CRITICAL,
        }
        return mapping.get(priority, NarrationPriority.MEDIUM)

    async def _route_to_narrator(
        self,
        narrator,
        event_data: OrchestratorEventData,
        message: str
    ) -> None:
        """
        Route event to appropriate narrator method.

        Maps orchestrator events to specific narrator announcement methods
        for rich, contextual voice feedback.
        """
        event = event_data.event
        service = event_data.service_name or ""
        details = event_data.details

        # Trinity component events
        if event == OrchestratorEvent.SERVICE_SPAWNING:
            if "prime" in service.lower():
                await narrator.announce_trinity_mind(mode="start")
            elif "reactor" in service.lower():
                await narrator.announce_trinity_nerves(mode="start")
            else:
                await narrator.announce_trinity_body(mode="start")

        elif event == OrchestratorEvent.SERVICE_HEALTHY:
            if "prime" in service.lower():
                await narrator.announce_trinity_mind(mode="complete")
            elif "reactor" in service.lower():
                await narrator.announce_trinity_nerves(mode="complete")
            else:
                await narrator.announce_trinity_body(mode="complete")

        elif event == OrchestratorEvent.SERVICE_CRASHED:
            await narrator.announce_error(
                error_message=f"{service} crashed",
                phase=None,
                recoverable=True
            )

        elif event == OrchestratorEvent.SERVICE_RESTARTING:
            await narrator.announce_recovery(success=False)

        elif event == OrchestratorEvent.SERVICE_RECOVERED:
            await narrator.announce_recovery(success=True)

        # Docker events
        elif event == OrchestratorEvent.DOCKER_CHECK:
            await narrator.announce_subsystem("docker", "checking")

        elif event == OrchestratorEvent.DOCKER_FOUND:
            await narrator.announce_subsystem("docker", "found")

        elif event == OrchestratorEvent.DOCKER_BUILD_START:
            await narrator.announce_intelligent(
                context="docker",
                event_type="build_start",
                details={"service": service}
            )

        elif event == OrchestratorEvent.DOCKER_BUILD_COMPLETE:
            await narrator.announce_intelligent(
                context="docker",
                event_type="build_complete",
                details={"service": service}
            )

        elif event == OrchestratorEvent.DOCKER_BUILD_FAILED:
            await narrator.announce_warning(
                message="Docker build failed, using fallback",
                context="docker"
            )

        # GCP events
        elif event == OrchestratorEvent.GCP_ROUTING_DECISION:
            await narrator.announce_intelligent(
                context="routing",
                event_type="gcp_selected",
                details={"service": service, "reason": details.get("reason", "resource requirements")}
            )

        elif event == OrchestratorEvent.GCP_VM_CREATING:
            await narrator.announce_intelligent(
                context="gcp",
                event_type="vm_creating",
                details={"service": service}
            )

        elif event == OrchestratorEvent.GCP_VM_READY:
            await narrator.announce_intelligent(
                context="gcp",
                event_type="vm_ready",
                details={"service": service}
            )

        # Memory routing events
        elif event == OrchestratorEvent.MEMORY_PRESSURE_HIGH:
            await narrator.announce_warning(
                message="High memory usage detected",
                context="resource"
            )

        elif event == OrchestratorEvent.MEMORY_PRESSURE_CRITICAL:
            await narrator.announce_warning(
                message="Critical memory pressure, activating cloud fallback",
                context="resource"
            )

        # Circuit breaker events
        elif event == OrchestratorEvent.CIRCUIT_BREAKER_OPEN:
            await narrator.announce_warning(
                message=f"Service protection activated for {service}",
                context="circuit_breaker"
            )

        elif event == OrchestratorEvent.CIRCUIT_BREAKER_CLOSED:
            await narrator.announce_intelligent(
                context="circuit_breaker",
                event_type="recovered",
                details={"service": service}
            )

        # Health events
        elif event == OrchestratorEvent.HEALTH_DEGRADED:
            await narrator.announce_partial_complete(
                services_failed=details.get("unhealthy_services", [])
            )

        elif event == OrchestratorEvent.HEALTH_RECOVERED:
            await narrator.announce_trinity_complete(
                mind_online=True,
                body_online=True,
                nerves_online=True
            )

        # Cross-repo events
        elif event == OrchestratorEvent.CROSS_REPO_SYNC_START:
            await narrator.announce_cross_repo_system("sync", "start")

        elif event == OrchestratorEvent.CROSS_REPO_SYNC_COMPLETE:
            await narrator.announce_cross_repo_system("sync", "complete")

        elif event == OrchestratorEvent.CROSS_REPO_DISCONNECT:
            await narrator.announce_warning(
                message=f"Lost connection to {service}",
                context="cross_repo"
            )

        elif event == OrchestratorEvent.CROSS_REPO_RECONNECT:
            await narrator.announce_intelligent(
                context="cross_repo",
                event_type="reconnected",
                details={"service": service}
            )

        # v95.10: Cross-repo integration system events
        elif event == OrchestratorEvent.CROSS_REPO_CONFIG_INIT:
            await narrator.announce_subsystem("unified_config", "start")

        elif event == OrchestratorEvent.CROSS_REPO_CONFIG_LOADED:
            await narrator.announce_subsystem("unified_config", "loaded",
                                              f"{details.get('repo_count', 3)} repositories configured")

        elif event == OrchestratorEvent.CROSS_REPO_CONFIG_SYNCED:
            await narrator.announce_cross_repo_system("config", "complete")

        elif event == OrchestratorEvent.CROSS_REPO_LOGGING_INIT:
            await narrator.announce_subsystem("distributed_logging", "start")

        elif event == OrchestratorEvent.CROSS_REPO_LOGGING_ACTIVE:
            await narrator.announce_subsystem("distributed_logging", "complete",
                                              "W3C trace context enabled")

        elif event == OrchestratorEvent.CROSS_REPO_METRICS_INIT:
            await narrator.announce_subsystem("metrics_collection", "start")

        elif event == OrchestratorEvent.CROSS_REPO_METRICS_ACTIVE:
            await narrator.announce_subsystem("metrics_collection", "complete",
                                              f"{details.get('metric_count', 0)} metrics registered")

        elif event == OrchestratorEvent.CROSS_REPO_ERROR_PROPAGATION_INIT:
            await narrator.announce_subsystem("error_propagation", "start")

        elif event == OrchestratorEvent.CROSS_REPO_ERROR_PROPAGATION_ACTIVE:
            await narrator.announce_subsystem("error_propagation", "complete",
                                              "Circuit breakers enabled")

        elif event == OrchestratorEvent.CROSS_REPO_STATE_INIT:
            await narrator.announce_subsystem("state_sync", "start")

        elif event == OrchestratorEvent.CROSS_REPO_STATE_SYNCED:
            await narrator.announce_cross_repo_system("sync", "complete")

        elif event == OrchestratorEvent.CROSS_REPO_RESOURCE_INIT:
            await narrator.announce_subsystem("resource_coordinator", "start")

        elif event == OrchestratorEvent.CROSS_REPO_RESOURCE_ACTIVE:
            await narrator.announce_subsystem("resource_coordinator", "complete",
                                              "Fair allocation enabled")

        elif event == OrchestratorEvent.CROSS_REPO_VERSION_CHECK:
            await narrator.announce_subsystem("version_compatibility", "start")

        elif event == OrchestratorEvent.CROSS_REPO_VERSION_COMPATIBLE:
            await narrator.announce_subsystem("version_compatibility", "complete",
                                              "All versions compatible")

        elif event == OrchestratorEvent.CROSS_REPO_VERSION_INCOMPATIBLE:
            incompatible = details.get("incompatible_count", 0)
            await narrator.announce_warning(
                message=f"Version incompatibility detected. {incompatible} issues found.",
                context="version_check"
            )

        elif event == OrchestratorEvent.CROSS_REPO_SECURITY_INIT:
            await narrator.announce_subsystem("security_context", "start")

        elif event == OrchestratorEvent.CROSS_REPO_SECURITY_ACTIVE:
            await narrator.announce_subsystem("security_context", "complete",
                                              "Secure tokens active")

        elif event == OrchestratorEvent.CROSS_REPO_INTEGRATION_COMPLETE:
            systems_online = details.get("systems_online", 8)
            await narrator.announce_cross_repo_system("integration", "complete")
            # This is a major milestone - log it prominently
            logger.info(f"[OrchestratorNarratorBridge] v95.10 Cross-repo integration complete: {systems_online} systems online")

        # Retry events
        elif event == OrchestratorEvent.RETRY_ATTEMPT:
            retry_count = event_data.retry_count or 1
            max_retries = event_data.max_retries or 3
            await narrator.announce_intelligent(
                context="retry",
                event_type="attempt",
                details={
                    "service": service,
                    "attempt": retry_count,
                    "max": max_retries
                }
            )

        elif event == OrchestratorEvent.RETRY_EXHAUSTED:
            await narrator.announce_error(
                error_message=f"All retry attempts exhausted for {service}",
                recoverable=False
            )

        # Model loading events
        elif event == OrchestratorEvent.MODEL_LOADING_START:
            await narrator.announce_jarvis_prime(mode="loading_model")

        elif event == OrchestratorEvent.MODEL_LOADING_PROGRESS:
            progress = event_data.progress_percent or 0
            if progress > 0 and progress % 25 == 0:  # Only announce at 25% intervals
                await narrator.announce_progress(
                    progress=progress,
                    message=f"Model loading {progress:.0f}% complete"
                )

        elif event == OrchestratorEvent.MODEL_LOADING_COMPLETE:
            await narrator.announce_jarvis_prime(mode="complete")

        elif event == OrchestratorEvent.MODEL_LOADING_TIMEOUT:
            await narrator.announce_slow_startup()

        elif event == OrchestratorEvent.MODEL_LOADING_EXTENDED:
            await narrator.announce_intelligent(
                context="model_loading",
                event_type="timeout_extended",
                details={"reason": "progress detected"}
            )

        # Parallel startup events
        elif event == OrchestratorEvent.PARALLEL_STARTUP_BEGIN:
            await narrator.announce_trinity_init()

        elif event == OrchestratorEvent.PARALLEL_STARTUP_COMPLETE:
            await narrator.announce_trinity_complete(
                mind_online=True,
                body_online=True,
                nerves_online=True
            )

        # Startup lifecycle events
        elif event == OrchestratorEvent.STARTUP_BEGIN:
            await narrator.announce_trinity_init()

        elif event == OrchestratorEvent.STARTUP_COMPLETE:
            await narrator.announce_complete(
                duration_seconds=event_data.elapsed_seconds
            )

        elif event == OrchestratorEvent.STARTUP_FAILED:
            await narrator.announce_error(
                error_message="Startup failed",
                recoverable=True
            )

        elif event == OrchestratorEvent.STARTUP_TIMEOUT:
            await narrator.announce_warning(
                message="Startup taking longer than expected",
                context="timeout"
            )

    def get_stats(self) -> Dict[str, Any]:
        """Get bridge statistics."""
        pending_count = 0
        if self._pending_announcements is not None:
            pending_count = self._pending_announcements.qsize()
        return {
            **self._stats,
            "pending_announcements": pending_count,
            "active_debounce_timers": len(self._debounce_timers),
            "history_size": len(self._announcement_history),
        }


# =============================================================================
# Convenience Functions
# =============================================================================

_bridge_instance: Optional[OrchestratorNarratorBridge] = None


async def get_orchestrator_narrator_bridge() -> OrchestratorNarratorBridge:
    """Get the global orchestrator-narrator bridge instance."""
    global _bridge_instance

    if _bridge_instance is None:
        _bridge_instance = await OrchestratorNarratorBridge.get_instance()

    return _bridge_instance


async def emit_orchestrator_event(
    event: OrchestratorEvent,
    service_name: Optional[str] = None,
    priority: AnnouncementPriority = AnnouncementPriority.MEDIUM,
    details: Optional[Dict[str, Any]] = None,
    **kwargs
) -> None:
    """
    Convenience function to emit an orchestrator event.

    Usage:
        await emit_orchestrator_event(
            OrchestratorEvent.SERVICE_SPAWNING,
            service_name="jarvis-prime",
            priority=AnnouncementPriority.HIGH
        )
    """
    bridge = await get_orchestrator_narrator_bridge()

    event_data = OrchestratorEventData(
        event=event,
        service_name=service_name,
        priority=priority,
        details=details or {},
        **kwargs
    )

    await bridge.emit(event_data)


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Event types
    "OrchestratorEvent",
    "AnnouncementPriority",
    "OrchestratorEventData",

    # Configuration
    "BridgeConfig",

    # Bridge class
    "OrchestratorNarratorBridge",
    "EventBus",

    # Convenience functions
    "get_orchestrator_narrator_bridge",
    "emit_orchestrator_event",
]
