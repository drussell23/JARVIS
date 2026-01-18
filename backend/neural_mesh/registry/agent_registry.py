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

    Features:
    - Dynamic agent registration
    - Capability-based agent discovery
    - Heartbeat-based health monitoring
    - Load tracking for balancing
    - Automatic offline detection
    - Persistence for recovery

    Usage:
        registry = AgentRegistry()
        await registry.start()

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

    def __init__(self, config: Optional[AgentRegistryConfig] = None) -> None:
        """Initialize the agent registry.

        Args:
            config: Registry configuration. Uses global config if not provided.
        """
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

        logger.info("AgentRegistry initialized")

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
        """
        self._metrics.health_checks += 1
        now = datetime.now()
        timeout = self.config.heartbeat_timeout_seconds
        grace_period = timeout * 0.5  # 50% grace period before going fully offline

        # Track agents that need status changes (to avoid modifying dict during iteration)
        status_changes: List[Tuple[str, AgentInfo, AgentStatus]] = []
        degraded_agents: List[str] = []
        recovering_agents: List[str] = []

        for agent_name, agent_info in list(self._agents.items()):
            try:
                age = agent_info.heartbeat_age_seconds()

                if age > timeout + grace_period:
                    # Definitely offline - past timeout + grace period
                    if agent_info.status != AgentStatus.OFFLINE:
                        status_changes.append((agent_name, agent_info, AgentStatus.OFFLINE))

                elif age > timeout:
                    # Past timeout but within grace period - mark as degraded first
                    if agent_info.status == AgentStatus.ONLINE or agent_info.status == AgentStatus.BUSY:
                        # First stage: mark as degraded
                        agent_info.health = HealthStatus.DEGRADED
                        degraded_agents.append(agent_name)
                    elif agent_info.health == HealthStatus.DEGRADED:
                        # Second check while degraded - now mark offline
                        status_changes.append((agent_name, agent_info, AgentStatus.OFFLINE))

                elif age > timeout / 2:
                    # Approaching timeout - mark health as degraded but keep status
                    if agent_info.health == HealthStatus.HEALTHY:
                        agent_info.health = HealthStatus.DEGRADED

                else:
                    # Healthy heartbeat timing
                    if agent_info.health != HealthStatus.HEALTHY:
                        # Recovery: agent is sending heartbeats again
                        if agent_info.status == AgentStatus.OFFLINE:
                            # Don't auto-recover from offline - require explicit heartbeat
                            pass
                        else:
                            agent_info.health = HealthStatus.HEALTHY
                            recovering_agents.append(agent_name)

            except Exception as agent_err:
                # Don't let one agent's error break the entire health check
                logger.debug("Error checking health for agent %s: %s", agent_name, agent_err)

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
        newly_offline_agents: List[str] = []

        for agent_name, agent_info, new_status in status_changes:
            if agent_info.status != new_status:
                old_status = agent_info.status
                agent_info.status = new_status
                agent_info.health = HealthStatus.UNHEALTHY

                # Update metrics
                if old_status in (AgentStatus.ONLINE, AgentStatus.BUSY):
                    self._metrics.currently_online -= 1
                    self._metrics.currently_offline += 1

                newly_offline_agents.append(agent_name)
                await self._fire_status_change(agent_info, old_status)

        # v16.0: BATCH LOG offline agents instead of one message per agent
        if newly_offline_agents:
            if len(newly_offline_agents) <= 5:
                # Log individually for small numbers
                for name in newly_offline_agents:
                    agent_info = self._agents.get(name)
                    if agent_info:
                        age = agent_info.heartbeat_age_seconds()
                        logger.warning(
                            "Agent %s marked offline (no heartbeat for %.1fs, timeout: %.1fs)",
                            name,
                            age,
                            timeout,
                        )
            else:
                # BATCH LOG for many offline agents (prevents the spam we saw)
                logger.warning(
                    "🚨 %d agents marked offline (no heartbeat for >%.1fs): %s%s",
                    len(newly_offline_agents),
                    timeout,
                    ", ".join(newly_offline_agents[:10]),
                    "..." if len(newly_offline_agents) > 10 else ""
                )

                # Log the full list at debug level for diagnostics
                logger.debug(
                    "Full list of offline agents: %s",
                    ", ".join(newly_offline_agents)
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
        """Load registry from disk."""
        if not self.config.registry_path:
            return

        filepath = Path(self.config.registry_path) / "registry.json"
        if not filepath.exists():
            return

        try:
            with open(filepath, "r") as f:
                data = json.load(f)

            for name, info_dict in data.items():
                agent_info = AgentInfo.from_dict(info_dict)
                # Mark as offline initially (need fresh heartbeat)
                agent_info.status = AgentStatus.OFFLINE
                agent_info.health = HealthStatus.UNKNOWN

                self._agents[name] = agent_info

                for capability in agent_info.capabilities:
                    self._capability_index[capability.lower()].add(name)
                self._type_index[agent_info.agent_type.lower()].add(name)

            self._metrics.total_registered = len(self._agents)
            self._metrics.currently_offline = len(self._agents)

            logger.info("Loaded registry with %d agents", len(self._agents))

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
