"""
UnifiedAgentRegistry v100.0 - Distributed Agent Registry with Redis
====================================================================

Advanced distributed agent registry that provides:
1. Service discovery with capability-based routing
2. Redis-backed state for multi-instance coordination
3. Pub/sub for real-time agent status updates
4. Circuit breaker for failing agents
5. Load balancing with health-aware routing
6. Automatic failover and recovery
7. Full observability with metrics and tracing

Architecture:
    +-----------------------------------------------------------------+
    |                  UnifiedAgentRegistry                            |
    |  +------------------------------------------------------------+ |
    |  |  ServiceDiscovery                                          | |
    |  |  +- Capability-based agent lookup                         | |
    |  |  +- Load-balanced routing                                 | |
    |  |  +- Health-aware selection                                | |
    |  +------------------------------------------------------------+ |
    |  +------------------------------------------------------------+ |
    |  |  RedisStateManager                                         | |
    |  |  +- Distributed agent state                               | |
    |  |  +- Leader election                                       | |
    |  |  +- Cross-instance coordination                           | |
    |  +------------------------------------------------------------+ |
    |  +------------------------------------------------------------+ |
    |  |  PubSubBroker                                              | |
    |  |  +- Agent registration events                             | |
    |  |  +- Health status updates                                 | |
    |  |  +- Capability changes                                    | |
    |  +------------------------------------------------------------+ |
    |  +------------------------------------------------------------+ |
    |  |  CircuitBreaker                                            | |
    |  |  +- Per-agent failure tracking                            | |
    |  |  +- Automatic recovery detection                          | |
    |  |  +- Graceful degradation                                  | |
    |  +------------------------------------------------------------+ |
    +-----------------------------------------------------------------+

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from backend.core.async_safety import LazyAsyncLock

# Environment-driven configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_DB = int(os.getenv("REDIS_AGENT_REGISTRY_DB", "1"))
REGISTRY_KEY_PREFIX = os.getenv("JARVIS_REGISTRY_PREFIX", "jarvis:agents:")
HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("AGENT_HEARTBEAT_INTERVAL", "5.0"))
HEARTBEAT_TIMEOUT_SECONDS = float(os.getenv("AGENT_HEARTBEAT_TIMEOUT", "15.0"))
CIRCUIT_BREAKER_FAILURES = int(os.getenv("AGENT_CIRCUIT_BREAKER_FAILURES", "3"))
CIRCUIT_BREAKER_RECOVERY_SECONDS = float(os.getenv("AGENT_CIRCUIT_BREAKER_RECOVERY", "30.0"))
HEALTH_CHECK_INTERVAL_SECONDS = float(os.getenv("AGENT_HEALTH_CHECK_INTERVAL", "10.0"))
LEADER_ELECTION_TTL_SECONDS = int(os.getenv("LEADER_ELECTION_TTL", "30"))
ENABLE_PERSISTENCE = os.getenv("AGENT_REGISTRY_PERSISTENCE", "true").lower() == "true"
PERSISTENCE_PATH = Path(os.getenv(
    "AGENT_REGISTRY_PERSISTENCE_PATH",
    str(Path.home() / ".jarvis" / "agent_registry.json")
))


class AgentStatus(Enum):
    """Agent lifecycle status."""
    UNKNOWN = "unknown"
    REGISTERING = "registering"
    ONLINE = "online"
    BUSY = "busy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    FAILED = "failed"
    DEREGISTERING = "deregistering"


class AgentType(Enum):
    """Types of agents in the system."""
    COORDINATOR = "coordinator"
    WORKER = "worker"
    SPECIALIST = "specialist"
    MONITOR = "monitor"
    GATEWAY = "gateway"
    ORCHESTRATOR = "orchestrator"


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class AgentInfo:
    """Information about a registered agent."""
    agent_id: str
    name: str
    agent_type: AgentType
    capabilities: List[str]
    host: str
    port: int
    version: str = "1.0.0"

    # Runtime state
    status: AgentStatus = AgentStatus.UNKNOWN
    load: float = 0.0  # 0.0 - 1.0
    task_queue_size: int = 0

    # Health metrics
    health_score: float = 1.0  # 0.0 - 1.0
    last_heartbeat: float = field(default_factory=time.time)
    consecutive_failures: int = 0
    total_requests: int = 0
    successful_requests: int = 0

    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    registered_at: float = field(default_factory=time.time)
    dependencies: List[str] = field(default_factory=list)
    tags: Set[str] = field(default_factory=set)

    # Instance tracking
    instance_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "agent_type": self.agent_type.value,
            "capabilities": self.capabilities,
            "host": self.host,
            "port": self.port,
            "version": self.version,
            "status": self.status.value,
            "load": self.load,
            "task_queue_size": self.task_queue_size,
            "health_score": self.health_score,
            "last_heartbeat": self.last_heartbeat,
            "consecutive_failures": self.consecutive_failures,
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "metadata": self.metadata,
            "registered_at": self.registered_at,
            "dependencies": self.dependencies,
            "tags": list(self.tags),
            "instance_id": self.instance_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AgentInfo:
        """Create from dictionary."""
        return cls(
            agent_id=data["agent_id"],
            name=data["name"],
            agent_type=AgentType(data["agent_type"]),
            capabilities=data["capabilities"],
            host=data["host"],
            port=data["port"],
            version=data.get("version", "1.0.0"),
            status=AgentStatus(data.get("status", "unknown")),
            load=data.get("load", 0.0),
            task_queue_size=data.get("task_queue_size", 0),
            health_score=data.get("health_score", 1.0),
            last_heartbeat=data.get("last_heartbeat", time.time()),
            consecutive_failures=data.get("consecutive_failures", 0),
            total_requests=data.get("total_requests", 0),
            successful_requests=data.get("successful_requests", 0),
            metadata=data.get("metadata", {}),
            registered_at=data.get("registered_at", time.time()),
            dependencies=data.get("dependencies", []),
            tags=set(data.get("tags", [])),
            instance_id=data.get("instance_id", str(uuid.uuid4())[:8]),
        )


@dataclass
class CircuitBreakerState:
    """Per-agent circuit breaker state."""
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = field(default_factory=time.time)
    recovery_attempts: int = 0


@dataclass
class RegistryMetrics:
    """Registry-wide metrics."""
    total_registered: int = 0
    currently_online: int = 0
    currently_degraded: int = 0
    currently_offline: int = 0
    registrations: int = 0
    deregistrations: int = 0
    heartbeats_received: int = 0
    health_checks_performed: int = 0
    capability_queries: int = 0
    routing_decisions: int = 0
    circuit_breaker_trips: int = 0
    leader_elections: int = 0
    failover_events: int = 0


class AgentRegistryEvent:
    """Event types for pub/sub."""
    REGISTERED = "agent.registered"
    DEREGISTERED = "agent.deregistered"
    STATUS_CHANGED = "agent.status_changed"
    HEALTH_UPDATED = "agent.health_updated"
    CAPABILITY_ADDED = "agent.capability_added"
    CAPABILITY_REMOVED = "agent.capability_removed"
    LEADER_CHANGED = "registry.leader_changed"
    CIRCUIT_OPENED = "agent.circuit_opened"
    CIRCUIT_CLOSED = "agent.circuit_closed"


class RedisStateManager:
    """Manages distributed state in Redis."""

    def __init__(self, redis_url: str = REDIS_URL, db: int = REDIS_DB):
        self.logger = logging.getLogger("RedisStateManager")
        self.redis_url = redis_url
        self.db = db
        self._redis = None
        self._pubsub = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._instance_id = str(uuid.uuid4())[:12]

    async def connect(self) -> bool:
        """Connect to Redis."""
        if self._connected:
            return True

        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                self.redis_url,
                db=self.db,
                decode_responses=True,
            )

            # Test connection
            await self._redis.ping()
            self._connected = True
            self.logger.info(f"Connected to Redis at {self.redis_url}")
            return True

        except ImportError:
            self.logger.warning("redis package not installed, using in-memory fallback")
            return False
        except Exception as e:
            self.logger.warning(f"Redis connection failed: {e}, using in-memory fallback")
            return False

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self._redis:
            # v93.14: Use aclose() instead of deprecated close()
            await self._redis.aclose()
            self._redis = None
        self._connected = False

    async def set_agent(self, agent: AgentInfo, ttl: Optional[int] = None) -> bool:
        """Store agent info in Redis."""
        if not self._connected or not self._redis:
            return False

        try:
            key = f"{REGISTRY_KEY_PREFIX}agent:{agent.agent_id}"
            data = json.dumps(agent.to_dict())

            if ttl:
                await self._redis.setex(key, ttl, data)
            else:
                await self._redis.set(key, data)

            # Update indexes
            await self._update_indexes(agent)
            return True

        except Exception as e:
            self.logger.error(f"Failed to set agent in Redis: {e}")
            return False

    async def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """Get agent info from Redis."""
        if not self._connected or not self._redis:
            return None

        try:
            key = f"{REGISTRY_KEY_PREFIX}agent:{agent_id}"
            data = await self._redis.get(key)

            if data:
                return AgentInfo.from_dict(json.loads(data))
            return None

        except Exception as e:
            self.logger.error(f"Failed to get agent from Redis: {e}")
            return None

    async def delete_agent(self, agent_id: str) -> bool:
        """Remove agent from Redis."""
        if not self._connected or not self._redis:
            return False

        try:
            key = f"{REGISTRY_KEY_PREFIX}agent:{agent_id}"

            # Get agent for index cleanup
            agent = await self.get_agent(agent_id)
            if agent:
                await self._remove_from_indexes(agent)

            await self._redis.delete(key)
            return True

        except Exception as e:
            self.logger.error(f"Failed to delete agent from Redis: {e}")
            return False

    async def get_all_agents(self) -> List[AgentInfo]:
        """Get all agents from Redis."""
        if not self._connected or not self._redis:
            return []

        try:
            pattern = f"{REGISTRY_KEY_PREFIX}agent:*"
            keys = await self._redis.keys(pattern)

            agents = []
            for key in keys:
                data = await self._redis.get(key)
                if data:
                    agents.append(AgentInfo.from_dict(json.loads(data)))

            return agents

        except Exception as e:
            self.logger.error(f"Failed to get all agents from Redis: {e}")
            return []

    async def get_agents_by_capability(self, capability: str) -> List[str]:
        """Get agent IDs by capability from index."""
        if not self._connected or not self._redis:
            return []

        try:
            key = f"{REGISTRY_KEY_PREFIX}idx:cap:{capability}"
            agent_ids = await self._redis.smembers(key)
            return list(agent_ids)

        except Exception as e:
            self.logger.error(f"Failed to get agents by capability: {e}")
            return []

    async def get_agents_by_type(self, agent_type: AgentType) -> List[str]:
        """Get agent IDs by type from index."""
        if not self._connected or not self._redis:
            return []

        try:
            key = f"{REGISTRY_KEY_PREFIX}idx:type:{agent_type.value}"
            agent_ids = await self._redis.smembers(key)
            return list(agent_ids)

        except Exception as e:
            self.logger.error(f"Failed to get agents by type: {e}")
            return []

    async def _update_indexes(self, agent: AgentInfo) -> None:
        """Update capability and type indexes."""
        if not self._redis:
            return

        try:
            pipe = self._redis.pipeline()

            # Capability index
            for cap in agent.capabilities:
                pipe.sadd(f"{REGISTRY_KEY_PREFIX}idx:cap:{cap}", agent.agent_id)

            # Type index
            pipe.sadd(f"{REGISTRY_KEY_PREFIX}idx:type:{agent.agent_type.value}", agent.agent_id)

            # Status index
            pipe.sadd(f"{REGISTRY_KEY_PREFIX}idx:status:{agent.status.value}", agent.agent_id)

            await pipe.execute()

        except Exception as e:
            self.logger.error(f"Failed to update indexes: {e}")

    async def _remove_from_indexes(self, agent: AgentInfo) -> None:
        """Remove from all indexes."""
        if not self._redis:
            return

        try:
            pipe = self._redis.pipeline()

            # Capability index
            for cap in agent.capabilities:
                pipe.srem(f"{REGISTRY_KEY_PREFIX}idx:cap:{cap}", agent.agent_id)

            # Type index
            pipe.srem(f"{REGISTRY_KEY_PREFIX}idx:type:{agent.agent_type.value}", agent.agent_id)

            # Status indexes
            for status in AgentStatus:
                pipe.srem(f"{REGISTRY_KEY_PREFIX}idx:status:{status.value}", agent.agent_id)

            await pipe.execute()

        except Exception as e:
            self.logger.error(f"Failed to remove from indexes: {e}")

    async def try_acquire_leadership(self, leader_key: str, ttl: int = LEADER_ELECTION_TTL_SECONDS) -> bool:
        """Try to acquire leadership via Redis lock."""
        if not self._connected or not self._redis:
            return True  # Assume leader if no Redis

        try:
            key = f"{REGISTRY_KEY_PREFIX}leader:{leader_key}"

            # SET NX EX pattern for leader election
            acquired = await self._redis.set(
                key,
                self._instance_id,
                nx=True,
                ex=ttl
            )

            if not acquired:
                # Check if we already hold the lock
                current = await self._redis.get(key)
                if current == self._instance_id:
                    # Refresh TTL
                    await self._redis.expire(key, ttl)
                    return True

            return bool(acquired)

        except Exception as e:
            self.logger.error(f"Leadership acquisition failed: {e}")
            return True  # Fail open

    async def publish_event(self, channel: str, event: Dict[str, Any]) -> None:
        """Publish event to Redis pub/sub."""
        if not self._connected or not self._redis:
            return

        try:
            await self._redis.publish(
                f"{REGISTRY_KEY_PREFIX}events:{channel}",
                json.dumps(event)
            )
        except Exception as e:
            self.logger.error(f"Failed to publish event: {e}")


class PubSubBroker:
    """Handles pub/sub for registry events."""

    def __init__(self, state_manager: RedisStateManager):
        self.logger = logging.getLogger("PubSubBroker")
        self.state_manager = state_manager
        self._callbacks: Dict[str, List[Callable]] = defaultdict(list)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def subscribe(self, event_type: str, callback: Callable) -> None:
        """Subscribe to an event type."""
        self._callbacks[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable) -> None:
        """Unsubscribe from an event type."""
        if callback in self._callbacks[event_type]:
            self._callbacks[event_type].remove(callback)

    async def publish(self, event_type: str, data: Dict[str, Any]) -> None:
        """Publish an event."""
        event = {
            "type": event_type,
            "timestamp": time.time(),
            "data": data,
        }

        # Local callbacks
        for callback in self._callbacks.get(event_type, []):
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                self.logger.error(f"Callback error for {event_type}: {e}")

        # Remote via Redis
        await self.state_manager.publish_event(event_type, event)

    async def start(self) -> None:
        """Start listening for pub/sub events."""
        self._running = True
        self.logger.info("PubSubBroker started")

    async def stop(self) -> None:
        """Stop listening."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.logger.info("PubSubBroker stopped")


class CircuitBreakerManager:
    """Manages circuit breakers for all agents."""

    def __init__(
        self,
        failure_threshold: int = CIRCUIT_BREAKER_FAILURES,
        recovery_seconds: float = CIRCUIT_BREAKER_RECOVERY_SECONDS
    ):
        self.logger = logging.getLogger("CircuitBreakerManager")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._states: Dict[str, CircuitBreakerState] = {}
        self._lock = asyncio.Lock()

    def get_state(self, agent_id: str) -> CircuitBreakerState:
        """Get circuit state for an agent."""
        if agent_id not in self._states:
            self._states[agent_id] = CircuitBreakerState()
        return self._states[agent_id]

    def can_call(self, agent_id: str) -> bool:
        """Check if requests can be made to this agent."""
        state = self.get_state(agent_id)

        if state.state == CircuitState.CLOSED:
            return True

        if state.state == CircuitState.OPEN:
            # Check if recovery period has passed
            if time.time() - state.last_failure_time >= self.recovery_seconds:
                state.state = CircuitState.HALF_OPEN
                state.recovery_attempts += 1
                self.logger.info(f"Circuit for {agent_id} entering half-open state")
                return True
            return False

        # Half-open: allow one request
        return True

    async def record_success(self, agent_id: str) -> None:
        """Record a successful request."""
        async with self._lock:
            state = self.get_state(agent_id)
            state.failure_count = 0
            state.last_success_time = time.time()

            if state.state == CircuitState.HALF_OPEN:
                state.state = CircuitState.CLOSED
                self.logger.info(f"Circuit for {agent_id} closed (recovered)")

    async def record_failure(self, agent_id: str) -> bool:
        """Record a failed request. Returns True if circuit opened."""
        async with self._lock:
            state = self.get_state(agent_id)
            state.failure_count += 1
            state.last_failure_time = time.time()

            if state.failure_count >= self.failure_threshold:
                if state.state != CircuitState.OPEN:
                    state.state = CircuitState.OPEN
                    self.logger.warning(f"Circuit for {agent_id} opened (too many failures)")
                    return True

            return False

    def reset(self, agent_id: str) -> None:
        """Reset circuit breaker for an agent."""
        if agent_id in self._states:
            self._states[agent_id] = CircuitBreakerState()


class UnifiedAgentRegistry:
    """
    Unified distributed agent registry with Redis backing.

    Provides:
    - Service discovery with capability-based routing
    - Distributed state for multi-instance coordination
    - Pub/sub for real-time updates
    - Circuit breaker for failing agents
    - Load balancing with health awareness
    """

    def __init__(self):
        self.logger = logging.getLogger("UnifiedAgentRegistry")

        # State management
        self._state_manager = RedisStateManager()
        self._pubsub = PubSubBroker(self._state_manager)
        self._circuit_breaker = CircuitBreakerManager()

        # In-memory cache
        self._agents: Dict[str, AgentInfo] = {}
        self._capability_index: Dict[str, Set[str]] = defaultdict(set)
        self._type_index: Dict[AgentType, Set[str]] = defaultdict(set)
        self._status_index: Dict[AgentStatus, Set[str]] = defaultdict(set)

        # Callbacks
        self._on_register_callbacks: List[Callable] = []
        self._on_deregister_callbacks: List[Callable] = []
        self._on_status_change_callbacks: List[Callable] = []

        # Metrics
        self._metrics = RegistryMetrics()

        # State
        self._running = False
        self._lock = asyncio.Lock()
        self._health_task: Optional[asyncio.Task] = None
        self._is_leader = False

        # Persistence
        PERSISTENCE_PATH.parent.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the registry."""
        if self._running:
            return

        self._running = True
        self.logger.info("UnifiedAgentRegistry starting...")

        # Connect to Redis
        redis_connected = await self._state_manager.connect()

        if redis_connected:
            self.logger.info("  Redis connected - distributed mode enabled")
            # Load existing agents from Redis
            await self._sync_from_redis()
        else:
            self.logger.info("  Redis not available - local mode only")
            # Load from persistence file
            await self._load_from_persistence()

        # Start pub/sub
        await self._pubsub.start()

        # Try to become leader
        self._is_leader = await self._state_manager.try_acquire_leadership("registry")
        if self._is_leader:
            self.logger.info("  Acquired registry leadership")

        # Start health check loop
        self._health_task = asyncio.create_task(self._health_check_loop())

        self.logger.info(f"UnifiedAgentRegistry ready ({len(self._agents)} agents)")

    async def stop(self) -> None:
        """Stop the registry."""
        self._running = False

        # Stop health check
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # Stop pub/sub
        await self._pubsub.stop()

        # Persist state
        if ENABLE_PERSISTENCE:
            await self._save_to_persistence()

        # Disconnect Redis
        await self._state_manager.disconnect()

        self.logger.info("UnifiedAgentRegistry stopped")

    async def register(
        self,
        name: str,
        agent_type: AgentType,
        capabilities: List[str],
        host: str,
        port: int,
        version: str = "1.0.0",
        metadata: Optional[Dict[str, Any]] = None,
        dependencies: Optional[List[str]] = None,
        tags: Optional[Set[str]] = None,
    ) -> AgentInfo:
        """Register a new agent."""
        async with self._lock:
            # Generate unique ID
            agent_id = hashlib.sha256(
                f"{name}:{host}:{port}:{time.time()}".encode()
            ).hexdigest()[:16]

            # Create agent info
            agent = AgentInfo(
                agent_id=agent_id,
                name=name,
                agent_type=agent_type,
                capabilities=capabilities,
                host=host,
                port=port,
                version=version,
                status=AgentStatus.REGISTERING,
                metadata=metadata or {},
                dependencies=dependencies or [],
                tags=tags or set(),
            )

            # Store locally
            self._agents[agent_id] = agent
            self._update_local_indexes(agent)

            # Store in Redis
            await self._state_manager.set_agent(agent)

            # Update status
            agent.status = AgentStatus.ONLINE
            await self._state_manager.set_agent(agent)
            self._update_status_index(agent_id, AgentStatus.REGISTERING, AgentStatus.ONLINE)

            # Update metrics
            self._metrics.total_registered += 1
            self._metrics.currently_online += 1
            self._metrics.registrations += 1

            # Publish event
            await self._pubsub.publish(
                AgentRegistryEvent.REGISTERED,
                {"agent_id": agent_id, "name": name, "capabilities": capabilities}
            )

            # Fire callbacks
            for callback in self._on_register_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(agent)
                    else:
                        callback(agent)
                except Exception as e:
                    self.logger.error(f"Register callback error: {e}")

            self.logger.info(f"Registered agent: {name} (ID: {agent_id})")
            return agent

    async def deregister(self, agent_id: str) -> bool:
        """Deregister an agent."""
        async with self._lock:
            if agent_id not in self._agents:
                return False

            agent = self._agents[agent_id]
            agent.status = AgentStatus.DEREGISTERING

            # Remove from local storage
            self._remove_from_local_indexes(agent)
            del self._agents[agent_id]

            # Remove from Redis
            await self._state_manager.delete_agent(agent_id)

            # Reset circuit breaker
            self._circuit_breaker.reset(agent_id)

            # Update metrics
            self._metrics.total_registered -= 1
            self._metrics.currently_online = max(0, self._metrics.currently_online - 1)
            self._metrics.deregistrations += 1

            # Publish event
            await self._pubsub.publish(
                AgentRegistryEvent.DEREGISTERED,
                {"agent_id": agent_id, "name": agent.name}
            )

            # Fire callbacks
            for callback in self._on_deregister_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(agent)
                    else:
                        callback(agent)
                except Exception as e:
                    self.logger.error(f"Deregister callback error: {e}")

            self.logger.info(f"Deregistered agent: {agent.name} (ID: {agent_id})")
            return True

    async def heartbeat(
        self,
        agent_id: str,
        load: float = 0.0,
        task_queue_size: int = 0,
        health_score: Optional[float] = None,
    ) -> bool:
        """Update agent heartbeat and metrics."""
        if agent_id not in self._agents:
            return False

        agent = self._agents[agent_id]
        agent.last_heartbeat = time.time()
        agent.load = max(0.0, min(1.0, load))
        agent.task_queue_size = task_queue_size

        if health_score is not None:
            agent.health_score = max(0.0, min(1.0, health_score))

        # Update status based on load
        old_status = agent.status
        if agent.load > 0.9:
            agent.status = AgentStatus.BUSY
        elif agent.health_score < 0.5:
            agent.status = AgentStatus.DEGRADED
        else:
            agent.status = AgentStatus.ONLINE

        # Store in Redis
        await self._state_manager.set_agent(agent)

        # Update metrics
        self._metrics.heartbeats_received += 1

        # Update status index and publish if changed
        if old_status != agent.status:
            self._update_status_index(agent_id, old_status, agent.status)
            await self._pubsub.publish(
                AgentRegistryEvent.STATUS_CHANGED,
                {"agent_id": agent_id, "old_status": old_status.value, "new_status": agent.status.value}
            )

        return True

    async def find_by_capability(
        self,
        capability: str,
        include_degraded: bool = False,
        sort_by_load: bool = True,
    ) -> List[AgentInfo]:
        """Find agents by capability."""
        self._metrics.capability_queries += 1

        agent_ids = self._capability_index.get(capability, set())
        agents = []

        for agent_id in agent_ids:
            agent = self._agents.get(agent_id)
            if not agent:
                continue

            # Check circuit breaker
            if not self._circuit_breaker.can_call(agent_id):
                continue

            # Check status
            if agent.status == AgentStatus.ONLINE:
                agents.append(agent)
            elif include_degraded and agent.status == AgentStatus.DEGRADED:
                agents.append(agent)

        if sort_by_load:
            agents.sort(key=lambda a: (a.load, -a.health_score))

        return agents

    async def find_by_type(
        self,
        agent_type: AgentType,
        status_filter: Optional[List[AgentStatus]] = None,
    ) -> List[AgentInfo]:
        """Find agents by type."""
        agent_ids = self._type_index.get(agent_type, set())
        agents = []

        allowed_statuses = status_filter or [AgentStatus.ONLINE, AgentStatus.BUSY]

        for agent_id in agent_ids:
            agent = self._agents.get(agent_id)
            if agent and agent.status in allowed_statuses:
                agents.append(agent)

        return agents

    async def get_best_agent(
        self,
        capability: str,
        min_health_score: float = 0.5,
    ) -> Optional[AgentInfo]:
        """Get the best available agent for a capability."""
        self._metrics.routing_decisions += 1

        candidates = await self.find_by_capability(capability, include_degraded=True)

        # Filter by health score
        candidates = [a for a in candidates if a.health_score >= min_health_score]

        if not candidates:
            return None

        # Score agents: lower load + higher health = better
        def score(agent: AgentInfo) -> float:
            return (1.0 - agent.load) * 0.6 + agent.health_score * 0.4

        candidates.sort(key=score, reverse=True)
        return candidates[0]

    async def record_request_success(self, agent_id: str) -> None:
        """Record a successful request to an agent."""
        if agent_id in self._agents:
            agent = self._agents[agent_id]
            agent.total_requests += 1
            agent.successful_requests += 1
            agent.consecutive_failures = 0

        await self._circuit_breaker.record_success(agent_id)

    async def record_request_failure(self, agent_id: str) -> None:
        """Record a failed request to an agent."""
        if agent_id in self._agents:
            agent = self._agents[agent_id]
            agent.total_requests += 1
            agent.consecutive_failures += 1

            # Degrade health score on consecutive failures
            if agent.consecutive_failures >= 3:
                agent.health_score = max(0.1, agent.health_score - 0.1)

        circuit_opened = await self._circuit_breaker.record_failure(agent_id)

        if circuit_opened:
            self._metrics.circuit_breaker_trips += 1
            await self._pubsub.publish(
                AgentRegistryEvent.CIRCUIT_OPENED,
                {"agent_id": agent_id}
            )

    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """Get agent by ID."""
        return self._agents.get(agent_id)

    def get_all_agents(self) -> List[AgentInfo]:
        """Get all registered agents."""
        return list(self._agents.values())

    def get_metrics(self) -> Dict[str, Any]:
        """Get registry metrics."""
        return {
            "total_registered": self._metrics.total_registered,
            "currently_online": self._metrics.currently_online,
            "currently_degraded": self._metrics.currently_degraded,
            "currently_offline": self._metrics.currently_offline,
            "registrations": self._metrics.registrations,
            "deregistrations": self._metrics.deregistrations,
            "heartbeats_received": self._metrics.heartbeats_received,
            "health_checks_performed": self._metrics.health_checks_performed,
            "capability_queries": self._metrics.capability_queries,
            "routing_decisions": self._metrics.routing_decisions,
            "circuit_breaker_trips": self._metrics.circuit_breaker_trips,
            "is_leader": self._is_leader,
            "redis_connected": self._state_manager._connected,
        }

    def on_register(self, callback: Callable) -> None:
        """Register callback for agent registration."""
        self._on_register_callbacks.append(callback)

    def on_deregister(self, callback: Callable) -> None:
        """Register callback for agent deregistration."""
        self._on_deregister_callbacks.append(callback)

    def on_status_change(self, callback: Callable) -> None:
        """Register callback for status changes."""
        self._on_status_change_callbacks.append(callback)

    # Private methods

    def _update_local_indexes(self, agent: AgentInfo) -> None:
        """Update local capability and type indexes."""
        for cap in agent.capabilities:
            self._capability_index[cap].add(agent.agent_id)

        self._type_index[agent.agent_type].add(agent.agent_id)
        self._status_index[agent.status].add(agent.agent_id)

    def _remove_from_local_indexes(self, agent: AgentInfo) -> None:
        """Remove from local indexes."""
        for cap in agent.capabilities:
            self._capability_index[cap].discard(agent.agent_id)

        self._type_index[agent.agent_type].discard(agent.agent_id)

        for status in AgentStatus:
            self._status_index[status].discard(agent.agent_id)

    def _update_status_index(
        self,
        agent_id: str,
        old_status: AgentStatus,
        new_status: AgentStatus
    ) -> None:
        """Update status index."""
        self._status_index[old_status].discard(agent_id)
        self._status_index[new_status].add(agent_id)

        # Update metrics
        if old_status == AgentStatus.ONLINE:
            self._metrics.currently_online = max(0, self._metrics.currently_online - 1)
        elif old_status == AgentStatus.DEGRADED:
            self._metrics.currently_degraded = max(0, self._metrics.currently_degraded - 1)
        elif old_status == AgentStatus.OFFLINE:
            self._metrics.currently_offline = max(0, self._metrics.currently_offline - 1)

        if new_status == AgentStatus.ONLINE:
            self._metrics.currently_online += 1
        elif new_status == AgentStatus.DEGRADED:
            self._metrics.currently_degraded += 1
        elif new_status == AgentStatus.OFFLINE:
            self._metrics.currently_offline += 1

    async def _sync_from_redis(self) -> None:
        """Sync local cache from Redis."""
        agents = await self._state_manager.get_all_agents()

        for agent in agents:
            self._agents[agent.agent_id] = agent
            self._update_local_indexes(agent)

        self.logger.info(f"Synced {len(agents)} agents from Redis")

    async def _health_check_loop(self) -> None:
        """Background health check loop."""
        while self._running:
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)

                if not self._running:
                    break

                # Check heartbeat timeouts
                now = time.time()

                for agent_id, agent in list(self._agents.items()):
                    if agent.status in (AgentStatus.OFFLINE, AgentStatus.FAILED):
                        continue

                    time_since_heartbeat = now - agent.last_heartbeat

                    if time_since_heartbeat > HEARTBEAT_TIMEOUT_SECONDS:
                        # Agent missed heartbeat
                        old_status = agent.status

                        if time_since_heartbeat > HEARTBEAT_TIMEOUT_SECONDS * 2:
                            agent.status = AgentStatus.OFFLINE
                        else:
                            agent.status = AgentStatus.DEGRADED

                        if old_status != agent.status:
                            self._update_status_index(agent_id, old_status, agent.status)

                            await self._pubsub.publish(
                                AgentRegistryEvent.STATUS_CHANGED,
                                {
                                    "agent_id": agent_id,
                                    "old_status": old_status.value,
                                    "new_status": agent.status.value,
                                    "reason": "heartbeat_timeout"
                                }
                            )

                            self.logger.warning(
                                f"Agent {agent.name} status: {old_status.value} -> {agent.status.value}"
                            )

                self._metrics.health_checks_performed += 1

                # Re-acquire leadership periodically
                self._is_leader = await self._state_manager.try_acquire_leadership("registry")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Health check error: {e}")

    async def _load_from_persistence(self) -> None:
        """Load registry state from file."""
        if not ENABLE_PERSISTENCE or not PERSISTENCE_PATH.exists():
            return

        try:
            data = json.loads(PERSISTENCE_PATH.read_text())

            for agent_data in data.get("agents", []):
                agent = AgentInfo.from_dict(agent_data)
                # Mark as offline since we're loading from file
                agent.status = AgentStatus.OFFLINE
                agent.last_heartbeat = 0

                self._agents[agent.agent_id] = agent
                self._update_local_indexes(agent)

            self.logger.info(f"Loaded {len(self._agents)} agents from persistence")

        except Exception as e:
            self.logger.error(f"Failed to load from persistence: {e}")

    async def _save_to_persistence(self) -> None:
        """Save registry state to file."""
        if not ENABLE_PERSISTENCE:
            return

        try:
            data = {
                "saved_at": time.time(),
                "agents": [agent.to_dict() for agent in self._agents.values()],
            }

            PERSISTENCE_PATH.write_text(json.dumps(data, indent=2))
            self.logger.info(f"Saved {len(self._agents)} agents to persistence")

        except Exception as e:
            self.logger.error(f"Failed to save to persistence: {e}")


# Global instance
_registry: Optional[UnifiedAgentRegistry] = None
_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_agent_registry() -> UnifiedAgentRegistry:
    """Get the global agent registry instance."""
    global _registry

    async with _lock:
        if _registry is None:
            _registry = UnifiedAgentRegistry()
            await _registry.start()

        return _registry


async def shutdown_agent_registry() -> None:
    """Shutdown the global agent registry."""
    global _registry

    if _registry:
        await _registry.stop()
        _registry = None
