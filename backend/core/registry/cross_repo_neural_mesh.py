"""
Cross-Repo Neural Mesh Bridge v2.0
==================================

Integrates JARVIS Prime and Reactor Core as Neural Mesh agents.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                       Neural Mesh                                │
    │  ┌──────────────────────────────────────────────────────────┐  │
    │  │              CrossRepoNeuralMeshBridge                   │  │
    │  └──────────────────────────────────────────────────────────┘  │
    │           │                    │                    │           │
    │           ▼                    ▼                    ▼           │
    │  ┌────────────┐       ┌────────────┐       ┌────────────┐      │
    │  │ JARVIS     │       │ JARVIS     │       │ Reactor    │      │
    │  │ Agents     │       │ Prime      │       │ Core       │      │
    │  │ (local)    │       │ (remote)   │       │ (remote)   │      │
    │  └────────────┘       └────────────┘       └────────────┘      │
    └─────────────────────────────────────────────────────────────────┘

JARVIS Prime provides:
- Local model inference (Llama, Mistral, etc.)
- GPU-accelerated processing
- Cost-effective local alternatives to cloud APIs

Reactor Core provides:
- Model fine-tuning
- Experience ingestion
- Continuous learning pipeline

This bridge:
1. Registers external repos as agents in Neural Mesh
2. Monitors their health via heartbeat files (with FileWatchGuard)
3. Routes tasks to appropriate repos (with circuit breakers)
4. Handles failover when repos are unavailable
5. Provides health probes and observability

v2.0 Changes:
- Added FileWatchGuard for robust heartbeat monitoring
- Added CrossRepoCircuitBreaker for external operations
- Added AtomicFileOps for safe file operations
- Added CorrelationContext for distributed tracing
- Added detailed health probes and metrics

Author: Trinity System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("CrossRepoNeuralMesh")

# =============================================================================
# Resilience Utilities (optional but recommended)
# =============================================================================

RESILIENCE_AVAILABLE = False
try:
    from backend.core.resilience import (
        AtomicFileOps,
        AtomicFileOpsConfig,
        CrossRepoCircuitBreaker,
        CircuitBreakerConfig,
        FileWatchGuard,
        FileWatchConfig,
        CorrelationContext,
        get_correlation_context,
    )
    RESILIENCE_AVAILABLE = True
except ImportError:
    AtomicFileOps = None
    AtomicFileOpsConfig = None
    CrossRepoCircuitBreaker = None
    CircuitBreakerConfig = None
    FileWatchGuard = None
    FileWatchConfig = None
    CorrelationContext = None
    get_correlation_context = lambda: None

# =============================================================================
# Configuration
# =============================================================================

CROSS_REPO_DIR = Path(os.getenv(
    "CROSS_REPO_DIR",
    str(Path.home() / ".jarvis" / "cross_repo")
))

TRINITY_COMPONENTS_DIR = Path(os.getenv(
    "TRINITY_COMPONENTS_DIR",
    str(Path.home() / ".jarvis" / "trinity" / "components")
))

# Heartbeat monitoring
HEARTBEAT_TIMEOUT = float(os.getenv("CROSS_REPO_HEARTBEAT_TIMEOUT", "30.0"))
HEALTH_CHECK_INTERVAL = float(os.getenv("CROSS_REPO_HEALTH_INTERVAL", "10.0"))

# External repo endpoints
JARVIS_PRIME_ENDPOINT = os.getenv("JARVIS_PRIME_ENDPOINT", "http://localhost:8001")
REACTOR_CORE_ENDPOINT = os.getenv("REACTOR_CORE_ENDPOINT", "http://localhost:8002")


class ExternalRepoStatus(Enum):
    """Status of external repo."""
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    STARTING = "starting"
    UNKNOWN = "unknown"


class ExternalRepoCapability(Enum):
    """Capabilities provided by external repos."""
    # JARVIS Prime capabilities
    LOCAL_INFERENCE = "local_inference"
    LOCAL_LLM = "local_llm"
    GPU_COMPUTE = "gpu_compute"
    EMBEDDINGS = "embeddings"
    COST_FREE_INFERENCE = "cost_free_inference"

    # Reactor Core capabilities
    MODEL_TRAINING = "model_training"
    EXPERIENCE_INGESTION = "experience_ingestion"
    FINE_TUNING = "fine_tuning"
    MODEL_VALIDATION = "model_validation"
    CONTINUOUS_LEARNING = "continuous_learning"


@dataclass
class ExternalRepoAgent:
    """Represents an external repo as a Neural Mesh agent."""
    repo_name: str
    agent_id: str
    endpoint: str
    capabilities: List[str]
    status: ExternalRepoStatus = ExternalRepoStatus.UNKNOWN
    last_heartbeat: float = 0.0
    health_score: float = 0.0
    load: float = 0.0
    version: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_healthy(self) -> bool:
        """Check if repo is healthy."""
        now = time.time()
        heartbeat_fresh = (now - self.last_heartbeat) < HEARTBEAT_TIMEOUT
        return heartbeat_fresh and self.status == ExternalRepoStatus.ONLINE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_name": self.repo_name,
            "agent_id": self.agent_id,
            "endpoint": self.endpoint,
            "capabilities": self.capabilities,
            "status": self.status.value,
            "last_heartbeat": self.last_heartbeat,
            "health_score": self.health_score,
            "load": self.load,
            "version": self.version,
            "metadata": self.metadata,
            "is_healthy": self.is_healthy(),
        }


@dataclass
class CrossRepoMetrics:
    """Metrics for cross-repo bridge."""
    health_checks: int = 0
    tasks_routed_to_prime: int = 0
    tasks_routed_to_reactor: int = 0
    prime_failures: int = 0
    reactor_failures: int = 0
    heartbeat_timeouts: int = 0
    capabilities_queries: int = 0
    last_health_check: float = 0.0
    # v2.0 additions
    circuit_breaker_trips: int = 0
    file_watch_restarts: int = 0
    atomic_write_failures: int = 0
    health_probe_failures: int = 0
    consecutive_prime_failures: int = 0
    consecutive_reactor_failures: int = 0


@dataclass
class HealthProbeResult:
    """Result of a health probe."""
    healthy: bool
    latency_ms: float
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    error: Optional[str] = None


# =============================================================================
# Cross-Repo Neural Mesh Bridge
# =============================================================================

class CrossRepoNeuralMeshBridge:
    """
    Bridges external repos (JARVIS Prime, Reactor Core) with Neural Mesh.

    Features:
    - Registers external repos as Neural Mesh agents
    - Monitors health via heartbeat files (with FileWatchGuard)
    - Routes tasks to appropriate repos (with circuit breakers)
    - Handles failover when repos unavailable
    - Provides unified capability lookup
    - Health probes and observability (v2.0)
    """

    def __init__(self, use_resilience: bool = True):
        self.logger = logging.getLogger("CrossRepoNeuralMesh")
        self._use_resilience = use_resilience and RESILIENCE_AVAILABLE

        # External repo agents
        self._prime_agent = ExternalRepoAgent(
            repo_name="jarvis_prime",
            agent_id="jarvis_prime_inference",
            endpoint=JARVIS_PRIME_ENDPOINT,
            capabilities=[
                ExternalRepoCapability.LOCAL_INFERENCE.value,
                ExternalRepoCapability.LOCAL_LLM.value,
                ExternalRepoCapability.GPU_COMPUTE.value,
                ExternalRepoCapability.EMBEDDINGS.value,
                ExternalRepoCapability.COST_FREE_INFERENCE.value,
            ],
        )

        self._reactor_agent = ExternalRepoAgent(
            repo_name="reactor_core",
            agent_id="reactor_core_training",
            endpoint=REACTOR_CORE_ENDPOINT,
            capabilities=[
                ExternalRepoCapability.MODEL_TRAINING.value,
                ExternalRepoCapability.EXPERIENCE_INGESTION.value,
                ExternalRepoCapability.FINE_TUNING.value,
                ExternalRepoCapability.MODEL_VALIDATION.value,
                ExternalRepoCapability.CONTINUOUS_LEARNING.value,
            ],
        )

        # State
        self._running = False
        self._health_task: Optional[asyncio.Task] = None
        self._mesh_registered = False

        # Metrics
        self._metrics = CrossRepoMetrics()

        # Neural Mesh reference (lazy)
        self._neural_mesh = None
        self._unified_registry = None

        # Callbacks
        self._status_change_callbacks: Set[Callable] = set()

        # Ensure directories exist
        CROSS_REPO_DIR.mkdir(parents=True, exist_ok=True)
        TRINITY_COMPONENTS_DIR.mkdir(parents=True, exist_ok=True)

        # v2.0: Resilience components
        self._file_ops: Optional[AtomicFileOps] = None
        self._circuit_breaker: Optional[CrossRepoCircuitBreaker] = None
        self._heartbeat_watchers: Dict[str, FileWatchGuard] = {}
        self._health_probe_results: Dict[str, HealthProbeResult] = {}

        if self._use_resilience:
            self._init_resilience_components()

    def _init_resilience_components(self) -> None:
        """Initialize resilience components (v2.0)."""
        if not RESILIENCE_AVAILABLE:
            return

        # Atomic file operations for safe reads/writes
        self._file_ops = AtomicFileOps(
            config=AtomicFileOpsConfig(
                temp_dir=CROSS_REPO_DIR / ".tmp",
                backup_count=2,
                sync_writes=True,
            )
        )

        # Circuit breaker for external operations
        self._circuit_breaker = CrossRepoCircuitBreaker(
            name="cross_repo_neural_mesh",
            config=CircuitBreakerConfig(
                failure_threshold=5,
                timeout_seconds=30.0,  # v2.1: Fixed - was 'recovery_timeout' which doesn't exist
                half_open_max_calls=2,
                # v93.0: Startup-aware configuration for ML model loading
                startup_grace_period_seconds=180.0,  # 3 minutes for ML models
                startup_failure_threshold=30,
                startup_network_failure_threshold=20,
            )
        )

        self.logger.debug("Resilience components initialized")

    async def _start_heartbeat_watchers(self) -> None:
        """Start FileWatchGuard for heartbeat directories (v2.0)."""
        if not self._use_resilience or not FileWatchGuard:
            return

        # Watch for Prime heartbeats
        prime_watch = FileWatchGuard(
            config=FileWatchConfig(
                watch_paths=[
                    TRINITY_COMPONENTS_DIR / "jarvis_prime.json",
                    CROSS_REPO_DIR / "jarvis_prime_heartbeat.json",
                ],
                patterns=["*.json"],
                debounce_seconds=1.0,
            ),
            on_event=lambda e: self._on_heartbeat_event("jarvis_prime", e),
            on_error=lambda e: self._on_watch_error("jarvis_prime", e),
        )

        # Watch for Reactor heartbeats
        reactor_watch = FileWatchGuard(
            config=FileWatchConfig(
                watch_paths=[
                    TRINITY_COMPONENTS_DIR / "reactor_core.json",
                    CROSS_REPO_DIR / "reactor_core_heartbeat.json",
                ],
                patterns=["*.json"],
                debounce_seconds=1.0,
            ),
            on_event=lambda e: self._on_heartbeat_event("reactor_core", e),
            on_error=lambda e: self._on_watch_error("reactor_core", e),
        )

        # Start watchers
        for name, watcher in [("jarvis_prime", prime_watch), ("reactor_core", reactor_watch)]:
            started = await watcher.start()
            if started:
                self._heartbeat_watchers[name] = watcher
                self.logger.debug(f"Heartbeat watcher started for {name}")
            else:
                self.logger.warning(f"Failed to start heartbeat watcher for {name}")

    async def _on_heartbeat_event(self, repo_name: str, event: Dict[str, Any]) -> None:
        """Handle heartbeat file change event (v2.0)."""
        agent = self._prime_agent if repo_name == "jarvis_prime" else self._reactor_agent

        # Re-check health immediately on heartbeat change
        if repo_name == "jarvis_prime":
            await self._check_repo_health(agent, "jarvis_prime.json")
        else:
            await self._check_repo_health(agent, "reactor_core.json")

    def _on_watch_error(self, repo_name: str, error: Exception) -> None:
        """Handle watcher error (v2.0)."""
        self.logger.warning(f"Heartbeat watcher error for {repo_name}: {error}")
        self._metrics.file_watch_restarts += 1

    async def start(self) -> bool:
        """Start the cross-repo bridge."""
        if self._running:
            return True

        self._running = True
        self.logger.info("CrossRepoNeuralMeshBridge v2.0 starting...")

        # Connect to Neural Mesh
        await self._connect_neural_mesh()

        # Initial health check
        await self._check_all_repos()

        # Register agents with Neural Mesh
        await self._register_agents_with_mesh()

        # Start heartbeat watchers (v2.0)
        await self._start_heartbeat_watchers()

        # Start health monitoring
        self._health_task = asyncio.create_task(
            self._health_loop(),
            name="cross_repo_health_monitor",
        )

        self.logger.info(
            f"CrossRepoNeuralMeshBridge ready "
            f"(Prime: {self._prime_agent.status.value}, "
            f"Reactor: {self._reactor_agent.status.value}, "
            f"resilience: {self._use_resilience})"
        )
        return True

    async def stop(self) -> None:
        """Stop the cross-repo bridge."""
        self._running = False

        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # Stop heartbeat watchers (v2.0)
        for name, watcher in self._heartbeat_watchers.items():
            try:
                await watcher.stop()
                self.logger.debug(f"Stopped heartbeat watcher for {name}")
            except Exception as e:
                self.logger.debug(f"Error stopping watcher {name}: {e}")
        self._heartbeat_watchers.clear()

        # Deregister from Neural Mesh
        await self._deregister_from_mesh()

        self.logger.info("CrossRepoNeuralMeshBridge stopped")

    async def _connect_neural_mesh(self) -> bool:
        """Connect to Neural Mesh coordinator."""
        try:
            from backend.neural_mesh.neural_mesh_coordinator import get_neural_mesh
            self._neural_mesh = await get_neural_mesh()
            self.logger.debug("Connected to Neural Mesh")
            return True
        except ImportError:
            self.logger.warning("Neural Mesh not available")
            return False
        except Exception as e:
            self.logger.warning(f"Neural Mesh connection failed: {e}")
            return False

    async def _register_agents_with_mesh(self) -> None:
        """Register external repos as Neural Mesh agents."""
        if not self._neural_mesh:
            return

        try:
            # Register JARVIS Prime if healthy
            if self._prime_agent.is_healthy():
                await self._register_single_agent(self._prime_agent)

            # Register Reactor Core if healthy
            if self._reactor_agent.is_healthy():
                await self._register_single_agent(self._reactor_agent)

            self._mesh_registered = True
            self.logger.info("Registered external repos with Neural Mesh")

        except Exception as e:
            self.logger.warning(f"Failed to register with Neural Mesh: {e}")

    async def _register_single_agent(self, agent: ExternalRepoAgent) -> None:
        """Register a single external agent."""
        if not self._neural_mesh:
            return

        try:
            # v93.0: Fixed registration to match AgentRegistry.register() API
            # status, load, health go in metadata, not as top-level params
            agent_data = {
                "agent_name": agent.agent_id,
                "agent_type": "external",
                "capabilities": set(agent.capabilities),
                "backend": "cross_repo",
                "version": agent.version,
                "metadata": {
                    **agent.metadata,
                    "repo_name": agent.repo_name,
                    "endpoint": agent.endpoint,
                    "is_external": True,
                    "cross_repo_bridge": True,
                    # v93.0: Moved these to metadata (not register() params)
                    "initial_status": "online" if agent.is_healthy() else "offline",
                    "load": agent.load,
                    "health_score": agent.health_score,
                    "health_status": "healthy" if agent.health_score >= 0.8 else "degraded",
                },
            }

            # Register with Neural Mesh
            if hasattr(self._neural_mesh, 'registry'):
                await self._neural_mesh.registry.register(**agent_data)
                self.logger.debug(f"Registered {agent.agent_id} with Neural Mesh")

        except Exception as e:
            self.logger.warning(f"Failed to register {agent.agent_id}: {e}")

    async def _deregister_from_mesh(self) -> None:
        """Deregister external repos from Neural Mesh."""
        if not self._neural_mesh or not self._mesh_registered:
            return

        try:
            if hasattr(self._neural_mesh, 'registry'):
                await self._neural_mesh.registry.deregister(self._prime_agent.agent_id)
                await self._neural_mesh.registry.deregister(self._reactor_agent.agent_id)
        except Exception as e:
            self.logger.debug(f"Deregistration error: {e}")

    async def _health_loop(self) -> None:
        """Periodic health check loop."""
        while self._running:
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)

                if not self._running:
                    break

                await self._check_all_repos()
                await self._update_mesh_status()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Health check error: {e}")
                await asyncio.sleep(5.0)

    async def _check_all_repos(self) -> None:
        """Check health of all external repos."""
        self._metrics.health_checks += 1
        self._metrics.last_health_check = time.time()

        await self._check_repo_health(self._prime_agent, "jarvis_prime.json")
        await self._check_repo_health(self._reactor_agent, "reactor_core.json")

    async def _check_repo_health(
        self,
        agent: ExternalRepoAgent,
        heartbeat_filename: str,
    ) -> None:
        """Check health of a single repo."""
        old_status = agent.status

        # Try multiple heartbeat locations
        heartbeat_paths = [
            TRINITY_COMPONENTS_DIR / heartbeat_filename,
            CROSS_REPO_DIR / f"{agent.repo_name}_heartbeat.json",
            CROSS_REPO_DIR / "heartbeat.json",
        ]

        heartbeat_found = False
        for heartbeat_file in heartbeat_paths:
            if heartbeat_file.exists():
                try:
                    data = json.loads(heartbeat_file.read_text())

                    # Update agent from heartbeat
                    timestamp = data.get("timestamp", 0)
                    if time.time() - timestamp < HEARTBEAT_TIMEOUT:
                        agent.last_heartbeat = timestamp
                        agent.status = ExternalRepoStatus.ONLINE
                        agent.health_score = data.get("health_score", 1.0)
                        agent.load = data.get("load", data.get("cpu_percent", 0.0)) / 100
                        agent.version = data.get("version", "unknown")
                        agent.metadata.update({
                            "pid": data.get("pid"),
                            "uptime": data.get("uptime_seconds"),
                            "memory_percent": data.get("memory_percent"),
                        })
                        heartbeat_found = True
                        break
                    else:
                        self._metrics.heartbeat_timeouts += 1

                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    self.logger.debug(f"Heartbeat read error: {e}")
                    continue

        if not heartbeat_found:
            agent.status = ExternalRepoStatus.OFFLINE
            agent.health_score = 0.0

        # Notify callbacks on status change
        if old_status != agent.status:
            self.logger.info(
                f"{agent.repo_name} status changed: {old_status.value} -> {agent.status.value}"
            )
            await self._notify_status_change(agent)

    async def _update_mesh_status(self) -> None:
        """
        Update status of external agents in Neural Mesh.

        v93.16: Now sends heartbeats to allow offline agents to recover.
        The registry requires explicit heartbeats to recover from OFFLINE status.
        """
        if not self._neural_mesh or not hasattr(self._neural_mesh, 'registry'):
            return

        for agent in [self._prime_agent, self._reactor_agent]:
            try:
                is_healthy = agent.is_healthy()

                # v93.16: CRITICAL - Send heartbeat for healthy agents
                # This allows agents to recover from OFFLINE status.
                # The registry won't auto-recover without explicit heartbeats.
                if is_healthy:
                    try:
                        await self._neural_mesh.registry.heartbeat(
                            agent.agent_id,
                            load=agent.load,
                        )
                    except Exception as hb_err:
                        self.logger.debug(f"Heartbeat send error for {agent.agent_id}: {hb_err}")

                # Update status in mesh
                new_status = "online" if is_healthy else "offline"
                await self._neural_mesh.registry.update_status(
                    agent.agent_id,
                    new_status,
                )

                # Update health/load
                await self._neural_mesh.registry.update_health(
                    agent.agent_id,
                    "healthy" if agent.health_score >= 0.8 else "degraded",
                )

            except Exception as e:
                self.logger.debug(f"Mesh status update error: {e}")

    async def _notify_status_change(self, agent: ExternalRepoAgent) -> None:
        """Notify callbacks of status change."""
        for callback in self._status_change_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(agent)
                else:
                    callback(agent)
            except Exception as e:
                self.logger.debug(f"Status callback error: {e}")

    # =========================================================================
    # Public API
    # =========================================================================

    def get_prime_agent(self) -> ExternalRepoAgent:
        """Get JARVIS Prime agent."""
        return self._prime_agent

    def get_reactor_agent(self) -> ExternalRepoAgent:
        """Get Reactor Core agent."""
        return self._reactor_agent

    def is_prime_available(self) -> bool:
        """Check if JARVIS Prime is available."""
        return self._prime_agent.is_healthy()

    def is_reactor_available(self) -> bool:
        """Check if Reactor Core is available."""
        return self._reactor_agent.is_healthy()

    def has_capability(self, capability: str) -> bool:
        """Check if any external repo has capability."""
        if capability in self._prime_agent.capabilities and self._prime_agent.is_healthy():
            return True
        if capability in self._reactor_agent.capabilities and self._reactor_agent.is_healthy():
            return True
        return False

    def get_agent_for_capability(
        self,
        capability: str,
    ) -> Optional[ExternalRepoAgent]:
        """Get best external agent for capability."""
        self._metrics.capabilities_queries += 1

        # Check JARVIS Prime first (usually preferred for inference)
        if capability in self._prime_agent.capabilities and self._prime_agent.is_healthy():
            return self._prime_agent

        # Check Reactor Core
        if capability in self._reactor_agent.capabilities and self._reactor_agent.is_healthy():
            return self._reactor_agent

        return None

    async def route_to_prime(
        self,
        task_type: str,
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Route task to JARVIS Prime.

        Uses file-based RPC for cross-repo communication.
        v2.0: Uses circuit breaker and atomic file operations.
        """
        if not self._prime_agent.is_healthy():
            self._metrics.prime_failures += 1
            self._metrics.consecutive_prime_failures += 1
            return None

        # Reset consecutive failures on healthy agent
        self._metrics.consecutive_prime_failures = 0
        self._metrics.tasks_routed_to_prime += 1

        # Write request to cross-repo directory
        request_file = CROSS_REPO_DIR / "prime_requests" / f"{task_type}_{int(time.time()*1000)}.json"
        request_file.parent.mkdir(parents=True, exist_ok=True)

        # Add correlation context if available
        ctx = get_correlation_context() if get_correlation_context else None
        request_data = {
            "task_type": task_type,
            "payload": payload,
            "timestamp": time.time(),
            "source": "jarvis_neural_mesh",
            "correlation_id": ctx.correlation_id if ctx else None,
        }

        # v2.0: Use circuit breaker for the write operation
        async def _do_write() -> Dict[str, Any]:
            if self._file_ops and self._use_resilience:
                # Use atomic write
                checksum = await self._file_ops.write_json(request_file, request_data)
                return {"status": "routed", "request_file": str(request_file), "checksum": checksum}
            else:
                # Fallback to basic write
                request_file.write_text(json.dumps(request_data))
                return {"status": "routed", "request_file": str(request_file)}

        try:
            if self._circuit_breaker and self._use_resilience:
                result = await self._circuit_breaker.execute(
                    tier="jarvis_prime",
                    func=_do_write,
                    timeout=5.0,
                )
            else:
                result = await _do_write()

            self.logger.debug(f"Routed task to Prime: {task_type}")
            return result

        except Exception as e:
            self.logger.error(f"Failed to route to Prime: {e}")
            self._metrics.prime_failures += 1
            self._metrics.consecutive_prime_failures += 1
            if self._circuit_breaker:
                self._metrics.circuit_breaker_trips += 1
            return None

    async def route_to_reactor(
        self,
        task_type: str,
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Route task to Reactor Core.

        Uses file-based RPC for cross-repo communication.
        v2.0: Uses circuit breaker and atomic file operations.
        """
        if not self._reactor_agent.is_healthy():
            self._metrics.reactor_failures += 1
            self._metrics.consecutive_reactor_failures += 1
            return None

        # Reset consecutive failures on healthy agent
        self._metrics.consecutive_reactor_failures = 0
        self._metrics.tasks_routed_to_reactor += 1

        # Write request to cross-repo directory
        request_file = CROSS_REPO_DIR / "reactor_requests" / f"{task_type}_{int(time.time()*1000)}.json"
        request_file.parent.mkdir(parents=True, exist_ok=True)

        # Add correlation context if available
        ctx = get_correlation_context() if get_correlation_context else None
        request_data = {
            "task_type": task_type,
            "payload": payload,
            "timestamp": time.time(),
            "source": "jarvis_neural_mesh",
            "correlation_id": ctx.correlation_id if ctx else None,
        }

        # v2.0: Use circuit breaker for the write operation
        async def _do_write() -> Dict[str, Any]:
            if self._file_ops and self._use_resilience:
                # Use atomic write
                checksum = await self._file_ops.write_json(request_file, request_data)
                return {"status": "routed", "request_file": str(request_file), "checksum": checksum}
            else:
                # Fallback to basic write
                request_file.write_text(json.dumps(request_data))
                return {"status": "routed", "request_file": str(request_file)}

        try:
            if self._circuit_breaker and self._use_resilience:
                result = await self._circuit_breaker.execute(
                    tier="reactor_core",
                    func=_do_write,
                    timeout=5.0,
                )
            else:
                result = await _do_write()

            self.logger.debug(f"Routed task to Reactor: {task_type}")
            return result

        except Exception as e:
            self.logger.error(f"Failed to route to Reactor: {e}")
            self._metrics.reactor_failures += 1
            self._metrics.consecutive_reactor_failures += 1
            if self._circuit_breaker:
                self._metrics.circuit_breaker_trips += 1
            return None

    def on_status_change(self, callback: Callable) -> None:
        """Register callback for status changes."""
        self._status_change_callbacks.add(callback)

    # =========================================================================
    # Health Probes (v2.0)
    # =========================================================================

    async def probe_health(self, repo_name: str) -> HealthProbeResult:
        """
        Execute health probe for a repo.

        Args:
            repo_name: "jarvis_prime" or "reactor_core"

        Returns:
            HealthProbeResult with latency and status details
        """
        start = time.time()
        agent = self._prime_agent if repo_name == "jarvis_prime" else self._reactor_agent

        try:
            # Check heartbeat file freshness
            heartbeat_file = TRINITY_COMPONENTS_DIR / f"{repo_name}.json"
            if not heartbeat_file.exists():
                heartbeat_file = CROSS_REPO_DIR / f"{repo_name}_heartbeat.json"

            if not heartbeat_file.exists():
                return HealthProbeResult(
                    healthy=False,
                    latency_ms=(time.time() - start) * 1000,
                    error="No heartbeat file found",
                    details={"repo_name": repo_name},
                )

            # Read heartbeat
            data = json.loads(heartbeat_file.read_text())
            timestamp = data.get("timestamp", 0)
            age = time.time() - timestamp

            if age > HEARTBEAT_TIMEOUT:
                return HealthProbeResult(
                    healthy=False,
                    latency_ms=(time.time() - start) * 1000,
                    error=f"Heartbeat stale: {age:.1f}s old",
                    details={"repo_name": repo_name, "heartbeat_age": age},
                )

            # Check circuit breaker state if available
            cb_state = "unknown"
            if self._circuit_breaker:
                tier_health = self._circuit_breaker.get_tier_health(repo_name)
                cb_state = tier_health.state.value if tier_health else "unknown"

            result = HealthProbeResult(
                healthy=True,
                latency_ms=(time.time() - start) * 1000,
                details={
                    "repo_name": repo_name,
                    "heartbeat_age": age,
                    "health_score": data.get("health_score", 1.0),
                    "load": data.get("load", 0.0),
                    "version": data.get("version", "unknown"),
                    "circuit_breaker_state": cb_state,
                },
            )
            self._health_probe_results[repo_name] = result
            return result

        except Exception as e:
            self._metrics.health_probe_failures += 1
            result = HealthProbeResult(
                healthy=False,
                latency_ms=(time.time() - start) * 1000,
                error=str(e),
                details={"repo_name": repo_name},
            )
            self._health_probe_results[repo_name] = result
            return result

    async def probe_all(self) -> Dict[str, HealthProbeResult]:
        """Execute health probes for all repos."""
        results = {}
        for repo_name in ["jarvis_prime", "reactor_core"]:
            results[repo_name] = await self.probe_health(repo_name)
        return results

    def get_last_probe_result(self, repo_name: str) -> Optional[HealthProbeResult]:
        """Get last health probe result for a repo."""
        return self._health_probe_results.get(repo_name)

    def get_metrics(self) -> Dict[str, Any]:
        """Get bridge metrics (v2.0 extended)."""
        # Get circuit breaker status if available
        cb_status = {}
        if self._circuit_breaker:
            for tier in ["jarvis_prime", "reactor_core"]:
                health = self._circuit_breaker.get_tier_health(tier)
                if health:
                    cb_status[tier] = {
                        "state": health.state.value,
                        "failure_count": health.failure_count,
                        "success_rate": health.success_rate,
                    }

        return {
            "running": self._running,
            "mesh_registered": self._mesh_registered,
            "resilience_enabled": self._use_resilience,
            "health_checks": self._metrics.health_checks,
            "tasks_routed_to_prime": self._metrics.tasks_routed_to_prime,
            "tasks_routed_to_reactor": self._metrics.tasks_routed_to_reactor,
            "prime_failures": self._metrics.prime_failures,
            "reactor_failures": self._metrics.reactor_failures,
            "heartbeat_timeouts": self._metrics.heartbeat_timeouts,
            "capabilities_queries": self._metrics.capabilities_queries,
            "last_health_check": self._metrics.last_health_check,
            # v2.0 additions
            "circuit_breaker_trips": self._metrics.circuit_breaker_trips,
            "file_watch_restarts": self._metrics.file_watch_restarts,
            "atomic_write_failures": self._metrics.atomic_write_failures,
            "health_probe_failures": self._metrics.health_probe_failures,
            "consecutive_prime_failures": self._metrics.consecutive_prime_failures,
            "consecutive_reactor_failures": self._metrics.consecutive_reactor_failures,
            "circuit_breaker_status": cb_status,
            "heartbeat_watchers_active": len(self._heartbeat_watchers),
            "prime": self._prime_agent.to_dict(),
            "reactor": self._reactor_agent.to_dict(),
        }


# =============================================================================
# Global Instance Management
# =============================================================================

_cross_repo_mesh: Optional[CrossRepoNeuralMeshBridge] = None
_mesh_lock: Optional[asyncio.Lock] = None


def _get_mesh_lock() -> asyncio.Lock:
    """Get or create the mesh lock."""
    global _mesh_lock
    if _mesh_lock is None:
        _mesh_lock = asyncio.Lock()
    return _mesh_lock


async def get_cross_repo_neural_mesh() -> CrossRepoNeuralMeshBridge:
    """Get the global CrossRepoNeuralMeshBridge instance."""
    global _cross_repo_mesh

    lock = _get_mesh_lock()
    async with lock:
        if _cross_repo_mesh is None:
            _cross_repo_mesh = CrossRepoNeuralMeshBridge()
            await _cross_repo_mesh.start()

        return _cross_repo_mesh


async def shutdown_cross_repo_neural_mesh() -> None:
    """Shutdown the global CrossRepoNeuralMeshBridge."""
    global _cross_repo_mesh

    if _cross_repo_mesh:
        await _cross_repo_mesh.stop()
        _cross_repo_mesh = None


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "CrossRepoNeuralMeshBridge",
    "ExternalRepoAgent",
    "ExternalRepoStatus",
    "ExternalRepoCapability",
    "CrossRepoMetrics",
    "HealthProbeResult",
    "get_cross_repo_neural_mesh",
    "shutdown_cross_repo_neural_mesh",
]
