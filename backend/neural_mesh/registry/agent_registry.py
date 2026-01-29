"""
JARVIS Neural Mesh - Agent Registry

Service discovery and health monitoring for all agents with:
- Dynamic registration and deregistration
- Capability-based queries
- Health monitoring with heartbeats
- Load balancing support
- Automatic offline detection
- Persistence for crash recovery

Performance Target: <10ms lookups at p95
Memory Footprint: ~5MB for 60 agents
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

from ..data_models import (
    AgentInfo,
    AgentStatus,
    HealthStatus,
)
from ..config import AgentRegistryConfig, get_config

logger = logging.getLogger(__name__)


@dataclass
class RegistryMetrics:
    """Metrics for the agent registry."""

    total_registered: int = 0
    currently_online: int = 0
    currently_offline: int = 0
    registrations: int = 0
    deregistrations: int = 0
    heartbeats_received: int = 0
    health_checks: int = 0
    capability_queries: int = 0
    average_lookup_time_ms: float = 0.0


class AgentRegistry:
    """
    Service discovery and health monitoring for agents.

    v116.0: Now a proper singleton to ensure all components share the same
    instance and CloudSQL dependency state is consistent across the system.

    Features:
    - Dynamic agent registration
    - Capability-based agent discovery
    - Heartbeat-based health monitoring
    - Load tracking for balancing
    - Automatic offline detection
    - Persistence for recovery
    - Singleton pattern for consistent state

    Usage:
        # Preferred: Use singleton accessor
        registry = get_agent_registry()
        await registry.start()

        # Legacy (still works, returns singleton)
        registry = AgentRegistry()

        # Register agent
        await registry.register(
            agent_name="vision_agent",
            agent_type="vision",
            capabilities={"screen_capture", "error_detection"},
        )

        # Find capable agents
        agents = await registry.find_by_capability("screen_capture")

        # Update heartbeat
        await registry.heartbeat("vision_agent")

        # Check health
        health = await registry.get_health("vision_agent")
    """

    # v116.0: Singleton pattern
    _instance: Optional['AgentRegistry'] = None
    _instance_lock = threading.Lock()

    def __new__(cls, config: Optional[AgentRegistryConfig] = None) -> 'AgentRegistry':
        """
        Singleton pattern - always returns the same instance.

        v116.0: This ensures all components share the same AgentRegistry
        and CloudSQL dependency state is consistent.
        """
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    @classmethod
    def get_instance(cls, config: Optional[AgentRegistryConfig] = None) -> 'AgentRegistry':
        """
        v116.0: Explicit singleton accessor.

        Preferred way to get the AgentRegistry instance.
        """
        return cls(config)

    def __init__(self, config: Optional[AgentRegistryConfig] = None) -> None:
        """Initialize the agent registry.

        v116.0: Only initializes once due to singleton pattern.

        Args:
            config: Registry configuration. Uses global config if not provided.
        """
        # v116.0: Skip re-initialization for singleton
        if getattr(self, '_initialized', False):
            return

        self.config = config or get_config().agent_registry

        # Registered agents: {agent_name: AgentInfo}
        self._agents: Dict[str, AgentInfo] = {}

        # Capability index: {capability: Set[agent_names]}
        self._capability_index: Dict[str, Set[str]] = defaultdict(set)

        # Type index: {agent_type: Set[agent_names]}
        self._type_index: Dict[str, Set[str]] = defaultdict(set)

        # Callbacks for agent events
        self._on_register_callbacks: List[Callable[[AgentInfo], Any]] = []
        self._on_unregister_callbacks: List[Callable[[AgentInfo], Any]] = []
        self._on_status_change_callbacks: List[Callable[[AgentInfo, AgentStatus], Any]] = []

        # Metrics
        self._metrics = RegistryMetrics()

        # State
        self._running = False
        self._health_check_task: Optional[asyncio.Task[None]] = None

        # Locks
        self._lock = asyncio.Lock()

        # v93.0: Startup grace period tracking
        self._startup_time = time.time()
        # v112.0: Reduced default grace period to 60s (was 180s)
        # Long grace periods mask heartbeat failures - agents should send heartbeats quickly
        self._startup_grace_period_seconds = float(
            os.environ.get("AGENT_REGISTRY_STARTUP_GRACE", "60.0")
        )
        self._agent_registration_times: Dict[str, float] = {}  # Track when each agent registered
        # v93.0: Track which agents have transitioned out of grace period
        self._agents_transitioned_from_grace: Set[str] = set()

        # v112.0: Per-agent type grace periods (some agents need longer startup)
        # Configurable via environment variables
        self._agent_type_grace_periods: Dict[str, float] = {
            "coordinator": float(os.environ.get("AGENT_GRACE_COORDINATOR", "90.0")),
            "reactor_core": float(os.environ.get("AGENT_GRACE_REACTOR_CORE", "90.0")),
            "mas-coordinator": float(os.environ.get("AGENT_GRACE_MAS_COORDINATOR", "90.0")),
            "voice_verification": float(os.environ.get("AGENT_GRACE_VOICE", "60.0")),
            "default": float(os.environ.get("AGENT_GRACE_DEFAULT", "45.0")),
        }

        # v112.0: Track dependency readiness states for intelligent health checking
        self._dependency_states: Dict[str, bool] = {
            "cloudsql": False,  # Will be updated by integration with ProxyReadinessGate
            "service_registry": False,
            "neural_mesh": True,  # We're the neural mesh, so we're ready by definition
        }

        # v112.0: Agents that depend on CloudSQL (don't mark offline if CloudSQL is down)
        self._cloudsql_dependent_agents: Set[str] = {
            "learning_agent", "memory_agent", "voice_verification",
            "speaker_verification", "voiceprint_manager"
        }

        # v112.0: Track why agents are being given grace (for debugging)
        self._agent_grace_reasons: Dict[str, str] = {}

        # v112.0: Track last dependency state change for logging
        self._last_dependency_log_time: Dict[str, float] = {}

        # v112.0: Dead confirmation tracking - agents must miss multiple health checks before going offline
        # This prevents transient network issues from causing false offline detections
        self._agent_dead_confirmation_count: Dict[str, int] = {}
        self._dead_confirmation_threshold = int(
            os.environ.get("AGENT_DEAD_CONFIRMATION_THRESHOLD", "3")
        )

        # v116.0: Mark as initialized
        self._initialized = True

        logger.info("AgentRegistry v116.0 initialized (singleton + dependency-aware health checks)")

    async def start(self) -> None:
        """Start the registry and health monitoring."""
        if self._running:
            return

        self._running = True

        # Load persisted registry
        await self._load_registry()

        # Start health check task
        self._health_check_task = asyncio.create_task(
            self._health_check_loop(),
            name="registry_health_check",
        )

        logger.info("AgentRegistry started")

    async def stop(self) -> None:
        """Stop the registry gracefully."""
        if not self._running:
            return

        self._running = False

        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        # Persist registry
        await self._save_registry()

        logger.info("AgentRegistry stopped")

    async def register(
        self,
        agent_name: str,
        agent_type: str,
        capabilities: Set[str],
        backend: str = "local",
        version: str = "1.0.0",
        dependencies: Optional[Set[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AgentInfo:
        """
        Register an agent with the registry.

        Args:
            agent_name: Unique name for the agent
            agent_type: Category of agent (vision, voice, context, etc.)
            capabilities: Set of capabilities this agent provides
            backend: Where this agent runs (local, cloud, hybrid)
            version: Agent version
            dependencies: Other agents this agent depends on
            metadata: Additional agent information

        Returns:
            The registered agent info

        Raises:
            ValueError: If agent is already registered
        """
        async with self._lock:
            if agent_name in self._agents:
                logger.warning("Agent %s already registered, updating", agent_name)
                return await self._update_registration(
                    agent_name, agent_type, capabilities, backend, version, dependencies, metadata
                )

            if len(self._agents) >= self.config.max_agents:
                raise ValueError(
                    f"Maximum agents ({self.config.max_agents}) reached"
                )

            if len(capabilities) > self.config.max_capabilities_per_agent:
                raise ValueError(
                    f"Too many capabilities ({len(capabilities)} > {self.config.max_capabilities_per_agent})"
                )

            agent_info = AgentInfo(
                agent_name=agent_name,
                agent_type=agent_type,
                capabilities=capabilities,
                backend=backend,
                version=version,
                dependencies=dependencies or set(),
                metadata=metadata or {},
                status=AgentStatus.ONLINE,
                health=HealthStatus.HEALTHY,
            )

            # Add to registry
            self._agents[agent_name] = agent_info

            # v93.0: Track registration time for startup grace period
            self._agent_registration_times[agent_name] = time.time()

            # Update indexes
            for capability in capabilities:
                self._capability_index[capability.lower()].add(agent_name)
            self._type_index[agent_type.lower()].add(agent_name)

            # Update metrics
            self._metrics.total_registered += 1
            self._metrics.currently_online += 1
            self._metrics.registrations += 1

            logger.info(
                "Registered agent: %s (type=%s, capabilities=%s)",
                agent_name,
                agent_type,
                ", ".join(capabilities),
            )

            # Fire callbacks
            for callback in self._on_register_callbacks:
                try:
                    result = callback(agent_info)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.exception("Error in register callback: %s", e)

            return agent_info

    async def _update_registration(
        self,
        agent_name: str,
        agent_type: str,
        capabilities: Set[str],
        backend: str,
        version: str,
        dependencies: Optional[Set[str]],
        metadata: Optional[Dict[str, Any]],
    ) -> AgentInfo:
        """Update an existing registration."""
        agent_info = self._agents[agent_name]

        # Remove old capability indexes
        for capability in agent_info.capabilities:
            self._capability_index[capability.lower()].discard(agent_name)
        self._type_index[agent_info.agent_type.lower()].discard(agent_name)

        # Update agent info
        agent_info.agent_type = agent_type
        agent_info.capabilities = capabilities
        agent_info.backend = backend
        agent_info.version = version
        if dependencies:
            agent_info.dependencies = dependencies
        if metadata:
            agent_info.metadata.update(metadata)
        agent_info.status = AgentStatus.ONLINE
        agent_info.update_heartbeat()

        # Add new capability indexes
        for capability in capabilities:
            self._capability_index[capability.lower()].add(agent_name)
        self._type_index[agent_type.lower()].add(agent_name)

        return agent_info

    async def unregister(self, agent_name: str) -> bool:
        """
        Unregister an agent from the registry.

        Args:
            agent_name: Name of the agent to unregister

        Returns:
            True if unregistered, False if not found
        """
        async with self._lock:
            if agent_name not in self._agents:
                return False

            agent_info = self._agents.pop(agent_name)

            # Remove from indexes
            for capability in agent_info.capabilities:
                self._capability_index[capability.lower()].discard(agent_name)
            self._type_index[agent_info.agent_type.lower()].discard(agent_name)

            # Update metrics
            if agent_info.status == AgentStatus.ONLINE:
                self._metrics.currently_online -= 1
            else:
                self._metrics.currently_offline -= 1
            self._metrics.deregistrations += 1

            logger.info("Unregistered agent: %s", agent_name)

            # Fire callbacks
            for callback in self._on_unregister_callbacks:
                try:
                    result = callback(agent_info)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.exception("Error in unregister callback: %s", e)

            return True

    async def heartbeat(
        self,
        agent_name: str,
        load: Optional[float] = None,
        task_queue_size: Optional[int] = None,
    ) -> bool:
        """
        Update agent heartbeat.

        Args:
            agent_name: Name of the agent
            load: Current load (0.0 to 1.0)
            task_queue_size: Number of pending tasks

        Returns:
            True if updated, False if agent not found
        """
        if agent_name not in self._agents:
            return False

        agent_info = self._agents[agent_name]
        old_status = agent_info.status

        agent_info.update_heartbeat()

        if load is not None:
            agent_info.load = max(0.0, min(1.0, load))

        if task_queue_size is not None:
            agent_info.task_queue_size = task_queue_size

        # Update status based on load
        if agent_info.load >= self.config.load_threshold:
            agent_info.status = AgentStatus.BUSY
        else:
            agent_info.status = AgentStatus.ONLINE

        agent_info.health = HealthStatus.HEALTHY

        self._metrics.heartbeats_received += 1

        # v93.16: Update metrics when recovering from OFFLINE
        if old_status == AgentStatus.OFFLINE:
            self._metrics.currently_offline -= 1
            self._metrics.currently_online += 1
            logger.info("Agent %s recovered via heartbeat: offline -> %s", agent_name, agent_info.status.value)

        # Fire status change callback if changed
        if old_status != agent_info.status:
            await self._fire_status_change(agent_info, old_status)

        return True

    async def get_agent(self, agent_name: str) -> Optional[AgentInfo]:
        """Get agent info by name."""
        return self._agents.get(agent_name)

    async def get_all_agents(self) -> List[AgentInfo]:
        """Get all registered agents."""
        return list(self._agents.values())

    async def find_by_capability(
        self,
        capability: str,
        available_only: bool = True,
        sort_by_load: bool = True,
    ) -> List[AgentInfo]:
        """
        Find agents with a specific capability.

        Args:
            capability: The capability to search for
            available_only: Only return available agents
            sort_by_load: Sort by load (lowest first)

        Returns:
            List of agents with the capability
        """
        start_time = time.perf_counter()

        agent_names = self._capability_index.get(capability.lower(), set())
        agents = [
            self._agents[name]
            for name in agent_names
            if name in self._agents
        ]

        if available_only:
            agents = [a for a in agents if a.is_available()]

        if sort_by_load:
            agents.sort(key=lambda a: a.load)

        # Update metrics
        lookup_time_ms = (time.perf_counter() - start_time) * 1000
        self._metrics.capability_queries += 1
        total_time = (
            self._metrics.average_lookup_time_ms
            * (self._metrics.capability_queries - 1)
            + lookup_time_ms
        )
        self._metrics.average_lookup_time_ms = (
            total_time / self._metrics.capability_queries
        )

        return agents

    async def find_by_type(
        self,
        agent_type: str,
        available_only: bool = True,
    ) -> List[AgentInfo]:
        """
        Find agents of a specific type.

        Args:
            agent_type: The agent type to search for
            available_only: Only return available agents

        Returns:
            List of agents of the type
        """
        agent_names = self._type_index.get(agent_type.lower(), set())
        agents = [
            self._agents[name]
            for name in agent_names
            if name in self._agents
        ]

        if available_only:
            agents = [a for a in agents if a.is_available()]

        return agents

    async def get_best_agent(
        self,
        capability: str,
    ) -> Optional[AgentInfo]:
        """
        Get the best available agent for a capability.

        Uses load balancing to select the least busy agent.

        Args:
            capability: Required capability

        Returns:
            Best available agent or None
        """
        agents = await self.find_by_capability(
            capability,
            available_only=True,
            sort_by_load=True,
        )

        if not agents:
            return None

        return agents[0]

    async def get_health(self, agent_name: str) -> Optional[HealthStatus]:
        """Get the health status of an agent."""
        agent = self._agents.get(agent_name)
        if not agent:
            return None
        return agent.health

    async def set_status(
        self,
        agent_name: str,
        status: AgentStatus,
    ) -> bool:
        """
        Set the status of an agent.

        Args:
            agent_name: Name of the agent
            status: New status

        Returns:
            True if updated, False if agent not found
        """
        if agent_name not in self._agents:
            return False

        agent_info = self._agents[agent_name]
        old_status = agent_info.status

        if old_status == status:
            return True

        agent_info.status = status

        # Update metrics
        if old_status == AgentStatus.ONLINE and status != AgentStatus.ONLINE:
            self._metrics.currently_online -= 1
            self._metrics.currently_offline += 1
        elif old_status != AgentStatus.ONLINE and status == AgentStatus.ONLINE:
            self._metrics.currently_online += 1
            self._metrics.currently_offline -= 1

        await self._fire_status_change(agent_info, old_status)

        return True

    async def increment_stat(
        self,
        agent_name: str,
        stat_name: str,
        amount: int = 1,
    ) -> bool:
        """
        Increment a statistic for an agent.

        Args:
            agent_name: Name of the agent
            stat_name: Name of the stat to increment
            amount: Amount to increment by

        Returns:
            True if updated, False if agent not found
        """
        if agent_name not in self._agents:
            return False

        agent_info = self._agents[agent_name]
        current = agent_info.stats.get(stat_name, 0)
        agent_info.stats[stat_name] = current + amount

        return True

    def get_metrics(self) -> RegistryMetrics:
        """Get current registry metrics."""
        return self._metrics

    # ========================================================================
    # v112.0: Dependency-Aware Health Checking
    # ========================================================================

    def set_dependency_ready(
        self,
        dependency_name: str,
        is_ready: bool,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Update the readiness state of a dependency.

        v112.0: This is called by external systems (e.g., ProxyReadinessGate) to signal
        when dependencies become ready or unavailable. Agents that depend on these
        systems won't be marked offline if their dependency is down.

        v117.0: Added optional metadata parameter for enhanced observability.

        Args:
            dependency_name: Name of the dependency (e.g., "cloudsql", "service_registry")
            is_ready: Whether the dependency is ready/healthy
            metadata: Optional metadata about the state change (source, timestamp, reason, etc.)

        Example:
            # Called by ProxyReadinessGate when CloudSQL becomes ready
            agent_registry.set_dependency_ready("cloudsql", True)

            # Called with metadata for enhanced tracking
            agent_registry.set_dependency_ready("cloudsql", True, {
                "source": "distributed_proxy_orchestrator",
                "ready_at": time.time(),
            })

            # Called by CloudSQL health check when connection fails
            agent_registry.set_dependency_ready("cloudsql", False, {
                "reason": "connection_timeout",
                "last_error": "Connection refused",
            })
        """
        old_state = self._dependency_states.get(dependency_name)
        self._dependency_states[dependency_name] = is_ready

        # v117.0: Store metadata for observability
        if metadata:
            if not hasattr(self, '_dependency_metadata'):
                self._dependency_metadata: Dict[str, Dict[str, Any]] = {}
            self._dependency_metadata[dependency_name] = {
                "is_ready": is_ready,
                "updated_at": time.time(),
                **metadata,
            }

        # Rate-limit logging - only log once per 30s per dependency
        current_time = time.time()
        last_log_time = self._last_dependency_log_time.get(dependency_name, 0)

        if old_state != is_ready or (current_time - last_log_time) > 30.0:
            self._last_dependency_log_time[dependency_name] = current_time
            status_str = "READY ✅" if is_ready else "NOT READY ❌"

            # v117.0: Include metadata source in log if available
            source_info = ""
            if metadata and "source" in metadata:
                source_info = f" (source: {metadata['source']})"

            logger.info(
                "[AgentRegistry v117.0] Dependency '%s' is now %s%s",
                dependency_name, status_str, source_info
            )

            # When a dependency becomes unavailable, log which agents are affected
            if not is_ready:
                affected_agents = self._get_agents_depending_on(dependency_name)
                if affected_agents:
                    logger.info(
                        "[AgentRegistry v117.0] Agents affected by %s outage (won't be marked offline): %s",
                        dependency_name,
                        ", ".join(list(affected_agents)[:10]) + ("..." if len(affected_agents) > 10 else "")
                    )

    def get_dependency_state(self, dependency_name: str) -> bool:
        """
        Get the current readiness state of a dependency.

        Args:
            dependency_name: Name of the dependency

        Returns:
            True if ready, False if not ready or unknown
        """
        return self._dependency_states.get(dependency_name, False)

    def get_all_dependency_states(self) -> Dict[str, bool]:
        """Get all dependency states."""
        return dict(self._dependency_states)

    def _get_agents_depending_on(self, dependency_name: str) -> Set[str]:
        """
        Get all agents that depend on a specific dependency.

        Args:
            dependency_name: Name of the dependency

        Returns:
            Set of agent names that depend on this dependency
        """
        if dependency_name == "cloudsql":
            # Return intersection of CloudSQL-dependent agents and registered agents
            return self._cloudsql_dependent_agents & set(self._agents.keys())
        # Add more dependency mappings as needed
        return set()

    def _agent_has_unavailable_dependency(self, agent_name: str) -> Tuple[bool, Optional[str]]:
        """
        Check if an agent has any unavailable dependencies.

        v112.0: Used in health checking to avoid marking agents offline when their
        dependencies are down. This provides more accurate health status.

        Args:
            agent_name: Name of the agent to check

        Returns:
            Tuple of (has_unavailable_dependency, dependency_name or None)
        """
        # Check CloudSQL dependency
        if agent_name in self._cloudsql_dependent_agents:
            if not self._dependency_states.get("cloudsql", False):
                return True, "cloudsql"

        # Check service_registry dependency (all agents depend on this)
        if not self._dependency_states.get("service_registry", False):
            # But only if we're not the one providing service_registry
            if agent_name not in ("jarvis-body", "supervisor"):
                return True, "service_registry"

        return False, None

    def _get_agent_grace_period(self, agent_name: str, agent_type: str) -> float:
        """
        Get the appropriate grace period for an agent.

        v112.0: Returns per-agent-type grace period or default.

        Args:
            agent_name: Name of the agent
            agent_type: Type of the agent

        Returns:
            Grace period in seconds
        """
        # Check for agent-specific grace period first
        if agent_type in self._agent_type_grace_periods:
            return self._agent_type_grace_periods[agent_type]

        # Check if agent name contains a known type pattern
        for type_pattern, grace_seconds in self._agent_type_grace_periods.items():
            if type_pattern in agent_name.lower():
                return grace_seconds

        return self._agent_type_grace_periods.get("default", 45.0)

    def on_register(self, callback: Callable[[AgentInfo], Any]) -> None:
        """Add callback for agent registration events."""
        self._on_register_callbacks.append(callback)

    def on_unregister(self, callback: Callable[[AgentInfo], Any]) -> None:
        """Add callback for agent unregistration events."""
        self._on_unregister_callbacks.append(callback)

    def on_status_change(
        self,
        callback: Callable[[AgentInfo, AgentStatus], Any],
    ) -> None:
        """Add callback for agent status change events."""
        self._on_status_change_callbacks.append(callback)

    async def _fire_status_change(
        self,
        agent_info: AgentInfo,
        old_status: AgentStatus,
    ) -> None:
        """Fire status change callbacks."""
        for callback in self._on_status_change_callbacks:
            try:
                result = callback(agent_info, old_status)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.exception("Error in status change callback: %s", e)

    async def _health_check_loop(self) -> None:
        """
        Periodically check agent health with robust error handling.

        v16.0 Enhancements:
        - Self-healing: Attempts to restart agents that went offline unexpectedly
        - Gradual degradation: Uses multiple health states before marking offline
        - Batch status updates: Prevents log spam from many simultaneous offline events
        - Smart timing: Adaptive check intervals based on system state
        """
        consecutive_errors = 0
        max_consecutive_errors = 10

        while self._running:
            try:
                # Adaptive sleep interval - shorter when things are degraded
                interval = self.config.health_check_interval_seconds
                if consecutive_errors > 0:
                    # Reduce interval when there are issues to detect recovery faster
                    interval = max(1.0, interval / 2)

                await asyncio.sleep(interval)
                await self._check_agent_health()
                consecutive_errors = 0  # Reset on success

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors <= 3 or consecutive_errors % 10 == 0:
                    logger.exception("Error in health check loop (error %d): %s", consecutive_errors, e)

                # Don't crash the loop - continue after a brief pause
                await asyncio.sleep(min(1.0 * consecutive_errors, 10.0))

                # If we're seeing persistent errors, something is very wrong
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(
                        "Health check loop has failed %d times consecutively. "
                        "Registry may be unstable.",
                        consecutive_errors
                    )

    async def _check_agent_health(self) -> None:
        """
        Check health of all agents based on heartbeats.

        v16.0 Enhanced with:
        - Grace period before marking offline (allows for temporary network issues)
        - Progressive degradation (healthy -> degraded -> offline)
        - Automatic cleanup of stale offline agents
        - BATCHED LOGGING to prevent log spam when many agents go offline
        - Rate-limited logging to prevent log spam
        - Self-healing attempt notification

        v93.0 Enhancements:
        - Startup grace period for newly registered agents
        - System-wide startup grace period
        - Better logging with reason codes

        v112.0 Enhancements:
        - Dependency-aware health checks (don't mark offline if dependency is down)
        - Dead confirmation threshold (multiple misses before offline)
        - Removed artificial heartbeat reset on grace transition
        - Per-agent-type grace periods
        """
        self._metrics.health_checks += 1
        current_time = time.time()
        timeout = self.config.heartbeat_timeout_seconds
        grace_period = timeout * 0.5  # 50% grace period before going fully offline

        # v93.0: Check if we're still in system startup grace period
        system_startup_age = current_time - self._startup_time
        in_system_startup = system_startup_age < self._startup_grace_period_seconds

        # Track agents that need status changes (to avoid modifying dict during iteration)
        status_changes: List[Tuple[str, AgentInfo, AgentStatus, str]] = []  # v112.0: Added reason
        degraded_agents: List[str] = []
        recovering_agents: List[str] = []
        skipped_startup_agents: List[str] = []
        dependency_protected_agents: List[Tuple[str, str]] = []  # v112.0: (agent_name, dependency_name)

        for agent_name, agent_info in list(self._agents.items()):
            try:
                age = agent_info.heartbeat_age_seconds()

                # v93.0: Check startup grace period for this agent
                agent_registration_time = self._agent_registration_times.get(agent_name, 0)
                agent_in_startup_grace = False

                # v112.0: Use per-agent-type grace period
                agent_grace = self._get_agent_grace_period(agent_name, agent_info.agent_type)

                if agent_registration_time > 0:
                    agent_age = current_time - agent_registration_time
                    if agent_age < agent_grace:
                        # Agent is within startup grace period - skip timeout checks
                        skipped_startup_agents.append(agent_name)
                        agent_in_startup_grace = True
                        self._agent_grace_reasons[agent_name] = f"startup_grace ({agent_grace:.0f}s)"
                        continue

                # v93.0: Skip if in system startup grace period
                if in_system_startup:
                    skipped_startup_agents.append(agent_name)
                    agent_in_startup_grace = True
                    self._agent_grace_reasons[agent_name] = "system_startup_grace"
                    continue

                # v112.0: REMOVED artificial heartbeat reset on grace transition
                # The old code reset heartbeat when transitioning out of grace, which masked
                # agents that never sent heartbeats. Now we just track the transition.
                if not agent_in_startup_grace and agent_name not in self._agents_transitioned_from_grace:
                    self._agents_transitioned_from_grace.add(agent_name)
                    # v112.0: Clear grace reason since agent is no longer in grace
                    self._agent_grace_reasons.pop(agent_name, None)
                    logger.debug(
                        "[AgentRegistry v112.0] Agent %s exited startup grace period (NOT resetting heartbeat)",
                        agent_name
                    )

                # v112.0: Check dependency availability BEFORE marking offline
                has_unavailable_dep, dep_name = self._agent_has_unavailable_dependency(agent_name)

                if age > timeout + grace_period:
                    # Would normally be offline - but check dependency first
                    if has_unavailable_dep and dep_name:
                        # v112.0: Dependency is down - don't mark agent offline
                        # Instead, mark as DEGRADED due to dependency
                        if agent_info.status != AgentStatus.OFFLINE:
                            agent_info.health = HealthStatus.DEGRADED
                            dependency_protected_agents.append((agent_name, dep_name))
                            self._agent_grace_reasons[agent_name] = f"dependency:{dep_name}"
                            # Reset dead confirmation since this is a dependency issue
                            self._agent_dead_confirmation_count[agent_name] = 0
                    else:
                        # v112.0: Increment dead confirmation counter
                        current_dead_count = self._agent_dead_confirmation_count.get(agent_name, 0)
                        self._agent_dead_confirmation_count[agent_name] = current_dead_count + 1

                        if current_dead_count + 1 >= self._dead_confirmation_threshold:
                            # Confirmed dead - mark offline
                            if agent_info.status != AgentStatus.OFFLINE:
                                status_changes.append((
                                    agent_name, agent_info, AgentStatus.OFFLINE,
                                    f"no_heartbeat_{age:.1f}s (confirmed:{current_dead_count+1})"
                                ))
                        else:
                            # Not yet confirmed - keep as degraded
                            agent_info.health = HealthStatus.DEGRADED
                            degraded_agents.append(agent_name)
                            logger.debug(
                                "[AgentRegistry v112.0] Agent %s pending dead confirmation %d/%d (age: %.1fs)",
                                agent_name, current_dead_count + 1, self._dead_confirmation_threshold, age
                            )

                elif age > timeout:
                    # Past timeout but within grace period - mark as degraded first
                    if agent_info.status == AgentStatus.ONLINE or agent_info.status == AgentStatus.BUSY:
                        # First stage: mark as degraded
                        agent_info.health = HealthStatus.DEGRADED
                        degraded_agents.append(agent_name)
                    elif agent_info.health == HealthStatus.DEGRADED:
                        # v112.0: Check dependency before escalating to offline
                        if has_unavailable_dep and dep_name:
                            dependency_protected_agents.append((agent_name, dep_name))
                        else:
                            # Increment dead confirmation
                            current_dead_count = self._agent_dead_confirmation_count.get(agent_name, 0)
                            self._agent_dead_confirmation_count[agent_name] = current_dead_count + 1

                            if current_dead_count + 1 >= self._dead_confirmation_threshold:
                                status_changes.append((
                                    agent_name, agent_info, AgentStatus.OFFLINE,
                                    f"degraded_timeout_{age:.1f}s (confirmed:{current_dead_count+1})"
                                ))

                elif age > timeout / 2:
                    # Approaching timeout - mark health as degraded but keep status
                    if agent_info.health == HealthStatus.HEALTHY:
                        agent_info.health = HealthStatus.DEGRADED

                else:
                    # Healthy heartbeat timing
                    # v112.0: Reset dead confirmation counter on healthy heartbeat
                    self._agent_dead_confirmation_count[agent_name] = 0
                    self._agent_grace_reasons.pop(agent_name, None)

                    if agent_info.health != HealthStatus.HEALTHY or agent_info.status == AgentStatus.OFFLINE:
                        # v93.16: Allow recovery from OFFLINE when heartbeat is fresh
                        # This enables external repos (jarvis_prime, reactor_core) to recover
                        # when their heartbeat files are updated and the bridge sends heartbeats.
                        if agent_info.status == AgentStatus.OFFLINE:
                            # Recovery from offline - agent is sending heartbeats again
                            agent_info.status = AgentStatus.ONLINE
                            agent_info.health = HealthStatus.HEALTHY
                            recovering_agents.append(agent_name)
                            # Update metrics
                            self._metrics.currently_offline -= 1
                            self._metrics.currently_online += 1
                            logger.info("Agent %s recovered: offline -> online", agent_name)
                        else:
                            agent_info.health = HealthStatus.HEALTHY
                            recovering_agents.append(agent_name)

            except Exception as agent_err:
                # Don't let one agent's error break the entire health check
                logger.debug("Error checking health for agent %s: %s", agent_name, agent_err)

        # v93.0: Log skipped agents at debug level
        if skipped_startup_agents:
            logger.debug(
                "[AgentRegistry] Skipped %d agents in startup grace period: %s",
                len(skipped_startup_agents),
                ", ".join(skipped_startup_agents[:5]) + ("..." if len(skipped_startup_agents) > 5 else "")
            )

        # v112.0: Log dependency-protected agents
        if dependency_protected_agents:
            # Group by dependency for cleaner logging
            deps_to_agents: Dict[str, List[str]] = {}
            for agent_name, dep_name in dependency_protected_agents:
                if dep_name not in deps_to_agents:
                    deps_to_agents[dep_name] = []
                deps_to_agents[dep_name].append(agent_name)

            for dep_name, agents_list in deps_to_agents.items():
                logger.debug(
                    "[AgentRegistry v112.0] %d agents protected due to %s outage: %s",
                    len(agents_list), dep_name,
                    ", ".join(agents_list[:5]) + ("..." if len(agents_list) > 5 else "")
                )

        # v16.0: BATCH LOG degraded agents to prevent spam
        if degraded_agents:
            if len(degraded_agents) <= 3:
                for name in degraded_agents:
                    logger.debug("Agent %s heartbeat delayed, health degraded", name)
            else:
                logger.debug(
                    "%d agents have degraded health: %s...",
                    len(degraded_agents),
                    ", ".join(degraded_agents[:3])
                )

        # v16.0: Log recovering agents
        if recovering_agents:
            logger.info("Agents recovered: %s", ", ".join(recovering_agents))

        # Apply status changes outside of iteration
        # v16.0: BATCH the offline notifications to prevent log spam
        newly_offline_agents: List[Tuple[str, str]] = []  # v112.0: (agent_name, reason)

        for agent_name, agent_info, new_status, reason in status_changes:
            if agent_info.status != new_status:
                old_status = agent_info.status
                agent_info.status = new_status
                agent_info.health = HealthStatus.UNHEALTHY

                # Update metrics
                if old_status in (AgentStatus.ONLINE, AgentStatus.BUSY):
                    self._metrics.currently_online -= 1
                    self._metrics.currently_offline += 1

                newly_offline_agents.append((agent_name, reason))
                await self._fire_status_change(agent_info, old_status)

        # v16.0: BATCH LOG offline agents instead of one message per agent
        # v112.0: Include reason in logging
        if newly_offline_agents:
            if len(newly_offline_agents) <= 5:
                # Log individually for small numbers with reason
                for name, reason in newly_offline_agents:
                    agent_info = self._agents.get(name)
                    if agent_info:
                        age = agent_info.heartbeat_age_seconds()
                        logger.warning(
                            "Agent %s marked offline (reason: %s, age: %.1fs, timeout: %.1fs)",
                            name,
                            reason,
                            age,
                            timeout,
                        )
            else:
                # BATCH LOG for many offline agents (prevents the spam we saw)
                agent_names = [name for name, _ in newly_offline_agents]
                logger.warning(
                    "🚨 %d agents marked offline (no heartbeat for >%.1fs): %s%s",
                    len(newly_offline_agents),
                    timeout,
                    ", ".join(agent_names[:10]),
                    "..." if len(agent_names) > 10 else ""
                )

                # Log the full list with reasons at debug level for diagnostics
                logger.debug(
                    "Full list of offline agents with reasons: %s",
                    ", ".join(f"{name}({reason})" for name, reason in newly_offline_agents)
                )

    async def _save_registry(self) -> None:
        """Save registry to disk."""
        if not self.config.registry_path:
            return

        try:
            Path(self.config.registry_path).mkdir(parents=True, exist_ok=True)
            filepath = Path(self.config.registry_path) / "registry.json"

            data = {
                name: info.to_dict()
                for name, info in self._agents.items()
            }

            with open(filepath, "w") as f:
                json.dump(data, f, indent=2, default=str)

            logger.info("Saved registry with %d agents", len(data))

        except Exception as e:
            logger.exception("Failed to save registry: %s", e)

    async def _load_registry(self) -> None:
        """
        Load registry from disk.

        v93.0 Enhancements:
        - Reset last_heartbeat to current time to prevent false offline detection
        - Track registration times for startup grace period
        - Graceful handling of stale entries
        """
        if not self.config.registry_path:
            return

        filepath = Path(self.config.registry_path) / "registry.json"
        if not filepath.exists():
            return

        try:
            with open(filepath, "r") as f:
                data = json.load(f)

            current_time = time.time()
            loaded_count = 0
            skipped_count = 0

            for name, info_dict in data.items():
                try:
                    agent_info = AgentInfo.from_dict(info_dict)

                    # v93.0: Reset heartbeat to now to give agents a fresh start
                    # This prevents old heartbeat timestamps from causing immediate offline detection
                    agent_info.last_heartbeat = datetime.now()

                    # Mark as offline initially (need fresh heartbeat to come online)
                    agent_info.status = AgentStatus.OFFLINE
                    agent_info.health = HealthStatus.UNKNOWN

                    self._agents[name] = agent_info

                    # v93.0: Track registration time for startup grace period
                    self._agent_registration_times[name] = current_time

                    for capability in agent_info.capabilities:
                        self._capability_index[capability.lower()].add(name)
                    self._type_index[agent_info.agent_type.lower()].add(name)

                    loaded_count += 1

                except Exception as agent_err:
                    logger.warning(
                        "[AgentRegistry] Failed to load agent %s from registry: %s",
                        name, agent_err
                    )
                    skipped_count += 1

            self._metrics.total_registered = len(self._agents)
            self._metrics.currently_offline = len(self._agents)

            logger.info(
                "[AgentRegistry] Loaded registry: %d agents loaded, %d skipped",
                loaded_count, skipped_count
            )

        except Exception as e:
            logger.exception("Failed to load registry: %s", e)

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"AgentRegistry("
            f"agents={len(self._agents)}, "
            f"online={self._metrics.currently_online}, "
            f"offline={self._metrics.currently_offline}"
            f")"
        )


# =============================================================================
# Module-Level Singleton Accessors (v116.0)
# =============================================================================


def get_agent_registry(config: Optional[AgentRegistryConfig] = None) -> AgentRegistry:
    """
    Get the singleton AgentRegistry instance.

    v116.0: This is the preferred way to access the AgentRegistry.
    All callers get the same instance, ensuring consistent dependency
    state (e.g., CloudSQL readiness) across the system.

    Args:
        config: Optional configuration (only used on first call)

    Returns:
        The singleton AgentRegistry instance

    Example:
        from neural_mesh.registry.agent_registry import get_agent_registry

        registry = get_agent_registry()
        await registry.register(agent_name="my_agent", ...)
    """
    return AgentRegistry.get_instance(config)
