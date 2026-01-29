"""
Neural Mesh Registry Bridge v100.0
===================================

Bidirectional bridge connecting UnifiedAgentRegistry with Neural Mesh AgentRegistry.

Features:
1. Automatic synchronization between registries
2. Data model translation (AgentInfo ↔ NeuralMeshAgentInfo)
3. Unified capability lookup across both systems
4. Cross-system event propagation
5. Health status harmonization
6. Graceful degradation when one system is unavailable

Architecture:
    +---------------------+                    +---------------------+
    | UnifiedAgentRegistry|                    | NeuralMeshCoordinator|
    |  (Redis-backed)     |                    |  (In-memory)        |
    +----------+----------+                    +----------+----------+
               |                                          |
               |         +--------------------+           |
               +-------> | NeuralMeshBridge  | <---------+
                         |                    |
                         | - Sync agents     |
                         | - Translate models|
                         | - Unified queries |
                         | - Event routing   |
                         +--------------------+

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from backend.core.async_safety import LazyAsyncLock

# Environment configuration
SYNC_INTERVAL_SECONDS = float(os.getenv("REGISTRY_SYNC_INTERVAL", "10.0"))
ENABLE_BIDIRECTIONAL_SYNC = os.getenv("REGISTRY_BIDIRECTIONAL_SYNC", "true").lower() == "true"
SYNC_ON_REGISTER = os.getenv("REGISTRY_SYNC_ON_REGISTER", "true").lower() == "true"
BRIDGE_ENABLED = os.getenv("NEURAL_MESH_BRIDGE_ENABLED", "true").lower() == "true"

logger = logging.getLogger("NeuralMeshBridge")


class SyncDirection(Enum):
    """Direction of registry synchronization."""
    UNIFIED_TO_MESH = "unified_to_mesh"
    MESH_TO_UNIFIED = "mesh_to_unified"
    BIDIRECTIONAL = "bidirectional"


@dataclass
class BridgeMetrics:
    """Metrics for the registry bridge."""
    syncs_unified_to_mesh: int = 0
    syncs_mesh_to_unified: int = 0
    agents_synced: int = 0
    translation_errors: int = 0
    sync_errors: int = 0
    capability_queries_unified: int = 0
    capability_queries_mesh: int = 0
    event_propagations: int = 0
    heartbeats_forwarded: int = 0  # v118.0: Track forwarded heartbeats
    last_sync_time: Optional[float] = 0.0  # v118.0: Initialize to 0 for first sync check
    last_sync_duration_ms: float = 0.0


@dataclass
class SyncResult:
    """Result of a sync operation."""
    success: bool
    direction: SyncDirection
    agents_synced: int
    errors: List[str] = field(default_factory=list)
    duration_ms: float = 0.0


class AgentTranslator:
    """
    Translates between UnifiedAgentRegistry and Neural Mesh agent formats.

    Handles the differences in:
    - Field names (agent_id vs agent_name)
    - Status enums (different enum classes)
    - Health representation (health_score float vs HealthStatus enum)
    - Capability storage (List vs Set)
    - Timestamps (float vs datetime)
    """

    @staticmethod
    def unified_to_mesh(unified_agent: Any) -> Optional[Dict[str, Any]]:
        """
        Convert UnifiedAgentRegistry AgentInfo to Neural Mesh format.

        Returns a dict suitable for Neural Mesh AgentRegistry.register()
        """
        try:
            # Map status values
            status_map = {
                "online": "online",
                "busy": "busy",
                "degraded": "online",  # Mesh doesn't have degraded
                "offline": "offline",
                "failed": "error",
                "unknown": "initializing",
                "registering": "initializing",
                "deregistering": "shutting_down",
            }

            # Map health score to health status
            def health_score_to_status(score: float) -> str:
                if score >= 0.8:
                    return "healthy"
                elif score >= 0.5:
                    return "degraded"
                elif score >= 0.2:
                    return "unhealthy"
                else:
                    return "unknown"

            unified_status = unified_agent.status.value if hasattr(unified_agent.status, 'value') else str(unified_agent.status)
            mesh_status = status_map.get(unified_status.lower(), "initializing")

            return {
                "agent_name": unified_agent.name or unified_agent.agent_id,
                "agent_type": unified_agent.agent_type.value if hasattr(unified_agent.agent_type, 'value') else str(unified_agent.agent_type),
                "capabilities": set(unified_agent.capabilities),
                "backend": unified_agent.metadata.get("backend", "distributed"),
                "version": unified_agent.version,
                "status": mesh_status,
                "load": unified_agent.load,
                "health": health_score_to_status(unified_agent.health_score),
                "task_queue_size": unified_agent.task_queue_size,
                "metadata": {
                    **unified_agent.metadata,
                    "unified_agent_id": unified_agent.agent_id,
                    "unified_host": unified_agent.host,
                    "unified_port": unified_agent.port,
                    "synced_from": "unified_registry",
                    "sync_time": time.time(),
                },
                "dependencies": set(unified_agent.dependencies or []),
            }
        except Exception as e:
            logger.error(f"Error translating unified agent to mesh: {e}")
            return None

    @staticmethod
    def mesh_to_unified(mesh_agent: Any) -> Optional[Dict[str, Any]]:
        """
        Convert Neural Mesh AgentInfo to UnifiedAgentRegistry format.

        Returns a dict suitable for UnifiedAgentRegistry.register()
        """
        try:
            from backend.core.registry.unified_agent_registry import AgentType, AgentStatus

            # Map agent type
            type_map = {
                "vision": AgentType.SPECIALIST,
                "voice": AgentType.SPECIALIST,
                "context": AgentType.WORKER,
                "memory": AgentType.WORKER,
                "coordinator": AgentType.COORDINATOR,
                "orchestrator": AgentType.ORCHESTRATOR,
                "monitor": AgentType.MONITOR,
                "health": AgentType.MONITOR,
                "specialized": AgentType.SPECIALIST,
                "system": AgentType.GATEWAY,
                "external": AgentType.GATEWAY,
            }

            # Map status
            status_map = {
                "initializing": AgentStatus.REGISTERING,
                "online": AgentStatus.ONLINE,
                "busy": AgentStatus.BUSY,
                "paused": AgentStatus.DEGRADED,
                "offline": AgentStatus.OFFLINE,
                "error": AgentStatus.FAILED,
                "shutting_down": AgentStatus.DEREGISTERING,
            }

            # Map health to score
            health_map = {
                "healthy": 1.0,
                "degraded": 0.6,
                "unhealthy": 0.3,
                "unknown": 0.5,
            }

            mesh_type = mesh_agent.agent_type if isinstance(mesh_agent.agent_type, str) else mesh_agent.agent_type
            mesh_status = mesh_agent.status.value if hasattr(mesh_agent.status, 'value') else str(mesh_agent.status)
            mesh_health = mesh_agent.health.value if hasattr(mesh_agent.health, 'value') else str(mesh_agent.health)

            agent_type = type_map.get(mesh_type.lower(), AgentType.WORKER)
            agent_status = status_map.get(mesh_status.lower(), AgentStatus.UNKNOWN)
            health_score = health_map.get(mesh_health.lower(), 0.5)

            # Extract host/port from metadata or use defaults
            host = mesh_agent.metadata.get("unified_host", "localhost")
            port = mesh_agent.metadata.get("unified_port", 0)

            return {
                "name": mesh_agent.agent_name,
                "agent_type": agent_type,
                "capabilities": list(mesh_agent.capabilities),
                "host": host,
                "port": port,
                "version": mesh_agent.version,
                "metadata": {
                    **mesh_agent.metadata,
                    "mesh_backend": mesh_agent.backend,
                    "synced_from": "neural_mesh",
                    "sync_time": time.time(),
                },
                "dependencies": list(mesh_agent.dependencies or set()),
                "tags": set(mesh_agent.metadata.get("tags", [])),
                # These will be set after registration
                "_initial_load": mesh_agent.load,
                "_initial_health": health_score,
                "_initial_queue": mesh_agent.task_queue_size,
            }
        except Exception as e:
            logger.error(f"Error translating mesh agent to unified: {e}")
            return None


class NeuralMeshBridge:
    """
    Bidirectional bridge between UnifiedAgentRegistry and Neural Mesh.

    Provides:
    - Automatic agent synchronization
    - Unified capability queries
    - Cross-system event propagation
    - Health status harmonization
    """

    def __init__(self):
        self.logger = logging.getLogger("NeuralMeshBridge")

        # Registry references (lazy-loaded)
        self._unified_registry = None
        self._mesh_coordinator = None

        # Sync state
        self._running = False
        self._sync_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Track synced agents to avoid loops
        self._synced_from_unified: Set[str] = set()  # agent_ids synced to mesh
        self._synced_from_mesh: Set[str] = set()  # agent_names synced to unified

        # Metrics
        self._metrics = BridgeMetrics()

        # Event callbacks
        self._on_sync_callbacks: List[Callable] = []

    async def start(self) -> bool:
        """Start the bridge."""
        if self._running:
            return True

        if not BRIDGE_ENABLED:
            self.logger.info("Neural Mesh Bridge is disabled via NEURAL_MESH_BRIDGE_ENABLED")
            return False

        self._running = True
        self.logger.info("NeuralMeshBridge starting...")

        # Try to connect to both registries
        unified_ok = await self._connect_unified_registry()
        mesh_ok = await self._connect_mesh_coordinator()

        if not unified_ok and not mesh_ok:
            self.logger.warning("Neither registry available - bridge inactive")
            self._running = False
            return False

        if unified_ok:
            self.logger.info("  Connected to UnifiedAgentRegistry")
        if mesh_ok:
            self.logger.info("  Connected to NeuralMeshCoordinator")

        # Register event handlers
        if unified_ok and SYNC_ON_REGISTER:
            await self._setup_unified_handlers()
        if mesh_ok and SYNC_ON_REGISTER:
            await self._setup_mesh_handlers()

        # Start sync loop
        if ENABLE_BIDIRECTIONAL_SYNC:
            self._sync_task = asyncio.create_task(self._sync_loop())

        # Initial sync
        await self.sync_all()

        self.logger.info("NeuralMeshBridge ready")
        return True

    async def stop(self) -> None:
        """Stop the bridge."""
        self._running = False

        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

        self.logger.info("NeuralMeshBridge stopped")

    async def sync_all(self) -> List[SyncResult]:
        """
        Perform full bidirectional sync with verification.

        Returns detailed SyncResult for each direction with:
        - Number of agents synced
        - Any errors encountered
        - Verification of sync success
        """
        results = []
        start_time = time.time()

        async with self._lock:
            # Pre-sync counts for verification
            pre_sync_unified = 0
            pre_sync_mesh = 0

            if self._unified_registry:
                try:
                    pre_sync_unified = len(self._unified_registry.get_all_agents())
                except Exception:
                    pass

            if self._mesh_coordinator and hasattr(self._mesh_coordinator, 'registry'):
                try:
                    mesh_agents = await self._mesh_coordinator.registry.get_all_agents()
                    pre_sync_mesh = len(mesh_agents) if mesh_agents else 0
                except Exception:
                    pass

            # Sync unified → mesh
            if self._unified_registry and self._mesh_coordinator:
                result = await self._sync_unified_to_mesh()
                results.append(result)

            # Sync mesh → unified
            if self._mesh_coordinator and self._unified_registry:
                result = await self._sync_mesh_to_unified()
                results.append(result)

            # Post-sync verification
            post_sync_unified = 0
            post_sync_mesh = 0

            if self._unified_registry:
                try:
                    post_sync_unified = len(self._unified_registry.get_all_agents())
                except Exception:
                    pass

            if self._mesh_coordinator and hasattr(self._mesh_coordinator, 'registry'):
                try:
                    mesh_agents = await self._mesh_coordinator.registry.get_all_agents()
                    post_sync_mesh = len(mesh_agents) if mesh_agents else 0
                except Exception:
                    pass

            # Log verification results
            total_synced = sum(r.agents_synced for r in results)
            total_errors = sum(len(r.errors) for r in results)
            duration_ms = (time.time() - start_time) * 1000

            if total_synced > 0 or total_errors > 0:
                self.logger.info(
                    f"[v100.0] Sync complete: {total_synced} agents synced, "
                    f"{total_errors} errors, {duration_ms:.1f}ms | "
                    f"Unified: {pre_sync_unified}→{post_sync_unified}, "
                    f"Mesh: {pre_sync_mesh}→{post_sync_mesh}"
                )

        return results

    async def find_by_capability_unified(
        self,
        capability: str,
        include_mesh: bool = True,
    ) -> List[Any]:
        """
        Find agents by capability, preferring UnifiedAgentRegistry.

        Searches both registries and merges results.
        """
        results = []

        # Search unified registry first
        if self._unified_registry:
            try:
                unified_agents = await self._unified_registry.find_by_capability(capability)
                results.extend(unified_agents)
                self._metrics.capability_queries_unified += 1
            except Exception as e:
                self.logger.error(f"Unified registry capability query failed: {e}")

        # Search mesh registry if enabled and unified didn't find enough
        if include_mesh and self._mesh_coordinator and len(results) < 3:
            try:
                mesh_agents = await self._mesh_coordinator.registry.find_by_capability(capability)
                # Filter out already-synced agents
                for agent in mesh_agents:
                    if agent.agent_name not in self._synced_from_mesh:
                        # Translate to unified format for consistency
                        results.append(agent)
                self._metrics.capability_queries_mesh += 1
            except Exception as e:
                self.logger.error(f"Mesh registry capability query failed: {e}")

        return results

    async def get_best_agent_unified(
        self,
        capability: str,
        min_health: float = 0.5,
    ) -> Optional[Any]:
        """Get best agent for a capability across both registries."""
        # Try unified first
        if self._unified_registry:
            try:
                agent = await self._unified_registry.get_best_agent(capability, min_health)
                if agent:
                    return agent
            except Exception as e:
                self.logger.error(f"Unified get_best_agent failed: {e}")

        # Fall back to mesh
        if self._mesh_coordinator:
            try:
                agent = await self._mesh_coordinator.registry.get_best_agent(capability)
                if agent:
                    return agent
            except Exception as e:
                self.logger.error(f"Mesh get_best_agent failed: {e}")

        return None

    def get_metrics(self) -> Dict[str, Any]:
        """Get bridge metrics."""
        return {
            "syncs_unified_to_mesh": self._metrics.syncs_unified_to_mesh,
            "syncs_mesh_to_unified": self._metrics.syncs_mesh_to_unified,
            "agents_synced": self._metrics.agents_synced,
            "translation_errors": self._metrics.translation_errors,
            "sync_errors": self._metrics.sync_errors,
            "capability_queries_unified": self._metrics.capability_queries_unified,
            "capability_queries_mesh": self._metrics.capability_queries_mesh,
            "event_propagations": self._metrics.event_propagations,
            "last_sync_time": self._metrics.last_sync_time,
            "last_sync_duration_ms": self._metrics.last_sync_duration_ms,
            "unified_connected": self._unified_registry is not None,
            "mesh_connected": self._mesh_coordinator is not None,
            "synced_from_unified_count": len(self._synced_from_unified),
            "synced_from_mesh_count": len(self._synced_from_mesh),
        }

    def on_sync(self, callback: Callable) -> None:
        """Register callback for sync events."""
        self._on_sync_callbacks.append(callback)

    # Private methods

    async def _connect_unified_registry(self) -> bool:
        """Connect to UnifiedAgentRegistry."""
        try:
            from backend.core.registry.unified_agent_registry import get_agent_registry
            self._unified_registry = await get_agent_registry()
            return True
        except ImportError:
            self.logger.warning("UnifiedAgentRegistry not available")
            return False
        except Exception as e:
            self.logger.error(f"Failed to connect to UnifiedAgentRegistry: {e}")
            return False

    async def _connect_mesh_coordinator(self) -> bool:
        """Connect to NeuralMeshCoordinator."""
        try:
            from backend.neural_mesh.neural_mesh_coordinator import get_neural_mesh
            self._mesh_coordinator = await get_neural_mesh()
            return True
        except ImportError:
            self.logger.warning("NeuralMeshCoordinator not available")
            return False
        except Exception as e:
            self.logger.error(f"Failed to connect to NeuralMeshCoordinator: {e}")
            return False

    async def _setup_unified_handlers(self) -> None:
        """Set up event handlers for unified registry."""
        if not self._unified_registry:
            return

        async def on_register(agent):
            await self._sync_single_unified_to_mesh(agent)

        async def on_deregister(agent):
            await self._remove_from_mesh(agent.agent_id)

        self._unified_registry.on_register(on_register)
        self._unified_registry.on_deregister(on_deregister)

    async def _setup_mesh_handlers(self) -> None:
        """Set up event handlers for mesh registry."""
        if not self._mesh_coordinator:
            return

        async def on_register(agent):
            await self._sync_single_mesh_to_unified(agent)

        async def on_unregister(agent):
            await self._remove_from_unified(agent.agent_name)

        self._mesh_coordinator.registry.on_register(on_register)
        self._mesh_coordinator.registry.on_unregister(on_unregister)

    async def _sync_loop(self) -> None:
        """
        Background sync loop.

        v118.0: Now also syncs heartbeats to prevent agents from going offline.
        """
        heartbeat_interval = 5.0  # v118.0: Forward heartbeats every 5 seconds

        while self._running:
            try:
                # v118.0: Use shorter sleep for heartbeat forwarding
                await asyncio.sleep(heartbeat_interval)

                if not self._running:
                    break

                # v118.0: CRITICAL - Forward heartbeats first (more frequent)
                # This prevents agents from timing out in unified registry
                hb_count = await self._sync_heartbeats_mesh_to_unified()
                if hb_count > 0:
                    self.logger.debug(f"[v118.0] Forwarded {hb_count} heartbeats mesh→unified")

                # Full sync less frequently (every SYNC_INTERVAL_SECONDS)
                last_sync = self._metrics.last_sync_time or 0.0
                if time.time() - last_sync >= SYNC_INTERVAL_SECONDS:
                    await self.sync_all()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Sync loop error: {e}")
                self._metrics.sync_errors += 1

    async def _sync_unified_to_mesh(self) -> SyncResult:
        """Sync agents from unified registry to mesh."""
        start_time = time.time()
        result = SyncResult(
            success=True,
            direction=SyncDirection.UNIFIED_TO_MESH,
            agents_synced=0,
        )

        if not self._unified_registry or not self._mesh_coordinator:
            result.success = False
            result.errors.append("Missing registry connection")
            return result

        try:
            unified_agents = self._unified_registry.get_all_agents()

            for agent in unified_agents:
                # Skip if already synced
                if agent.agent_id in self._synced_from_unified:
                    continue

                # Skip if originally from mesh
                if agent.metadata.get("synced_from") == "neural_mesh":
                    continue

                try:
                    await self._sync_single_unified_to_mesh(agent)
                    result.agents_synced += 1
                except Exception as e:
                    result.errors.append(f"Failed to sync {agent.agent_id}: {e}")

            self._metrics.syncs_unified_to_mesh += 1
            self._metrics.agents_synced += result.agents_synced

        except Exception as e:
            result.success = False
            result.errors.append(str(e))
            self._metrics.sync_errors += 1

        result.duration_ms = (time.time() - start_time) * 1000
        self._metrics.last_sync_time = time.time()
        self._metrics.last_sync_duration_ms = result.duration_ms

        return result

    async def _sync_mesh_to_unified(self) -> SyncResult:
        """Sync agents from mesh to unified registry."""
        start_time = time.time()
        result = SyncResult(
            success=True,
            direction=SyncDirection.MESH_TO_UNIFIED,
            agents_synced=0,
        )

        if not self._mesh_coordinator or not self._unified_registry:
            result.success = False
            result.errors.append("Missing registry connection")
            return result

        try:
            mesh_agents = await self._mesh_coordinator.registry.get_all_agents()

            for agent in mesh_agents:
                # Skip if already synced
                if agent.agent_name in self._synced_from_mesh:
                    continue

                # Skip if originally from unified
                if agent.metadata.get("synced_from") == "unified_registry":
                    continue

                try:
                    await self._sync_single_mesh_to_unified(agent)
                    result.agents_synced += 1
                except Exception as e:
                    result.errors.append(f"Failed to sync {agent.agent_name}: {e}")

            self._metrics.syncs_mesh_to_unified += 1
            self._metrics.agents_synced += result.agents_synced

        except Exception as e:
            result.success = False
            result.errors.append(str(e))
            self._metrics.sync_errors += 1

        result.duration_ms = (time.time() - start_time) * 1000

        return result

    async def _sync_heartbeats_mesh_to_unified(self) -> int:
        """
        v118.0: CRITICAL FIX - Forward heartbeats from mesh to unified registry.

        This ensures that agents registered in the mesh also maintain heartbeats
        in the unified registry, preventing them from being marked as offline.

        Returns:
            Number of heartbeats successfully forwarded
        """
        if not self._mesh_coordinator or not self._unified_registry:
            return 0

        heartbeats_forwarded = 0

        try:
            mesh_agents = await self._mesh_coordinator.registry.get_all_agents()

            for agent in mesh_agents:
                try:
                    # Get the corresponding unified agent ID
                    # Agents synced from mesh use agent_name as the ID
                    unified_id = agent.metadata.get("unified_agent_id", agent.agent_name)

                    # Forward heartbeat to unified registry
                    # This keeps the agent alive in both registries
                    success = await self._unified_registry.heartbeat(
                        unified_id,
                        load=getattr(agent, "load", 0.0),
                        task_queue_size=getattr(agent, "task_queue_size", 0),
                        health_score=getattr(agent, "health_score", 1.0),
                    )

                    if success:
                        heartbeats_forwarded += 1
                    else:
                        # Agent might not exist in unified registry - sync it
                        if agent.agent_name not in self._synced_from_mesh:
                            await self._sync_single_mesh_to_unified(agent)
                            heartbeats_forwarded += 1

                except Exception as e:
                    self.logger.debug(f"Heartbeat forward failed for {agent.agent_name}: {e}")

            if heartbeats_forwarded > 0:
                self._metrics.heartbeats_forwarded += heartbeats_forwarded

        except Exception as e:
            self.logger.error(f"Heartbeat sync error: {e}")

        return heartbeats_forwarded

    async def _sync_single_unified_to_mesh(self, unified_agent: Any) -> bool:
        """Sync a single agent from unified to mesh."""
        if not self._mesh_coordinator:
            return False

        # Translate
        mesh_data = AgentTranslator.unified_to_mesh(unified_agent)
        if not mesh_data:
            self._metrics.translation_errors += 1
            return False

        try:
            # Register with mesh
            await self._mesh_coordinator.registry.register(
                agent_name=mesh_data["agent_name"],
                agent_type=mesh_data["agent_type"],
                capabilities=mesh_data["capabilities"],
                backend=mesh_data["backend"],
                version=mesh_data["version"],
                dependencies=mesh_data["dependencies"],
                metadata=mesh_data["metadata"],
            )

            # Track sync
            self._synced_from_unified.add(unified_agent.agent_id)
            self._metrics.event_propagations += 1

            self.logger.debug(f"Synced unified agent '{unified_agent.name}' to mesh")
            return True

        except Exception as e:
            self.logger.error(f"Failed to sync unified agent to mesh: {e}")
            return False

    async def _sync_single_mesh_to_unified(self, mesh_agent: Any) -> bool:
        """Sync a single agent from mesh to unified."""
        if not self._unified_registry:
            return False

        # Translate
        unified_data = AgentTranslator.mesh_to_unified(mesh_agent)
        if not unified_data:
            self._metrics.translation_errors += 1
            return False

        try:
            # Extract initial state
            initial_load = unified_data.pop("_initial_load", 0.0)
            initial_health = unified_data.pop("_initial_health", 1.0)
            initial_queue = unified_data.pop("_initial_queue", 0)

            # Register with unified
            agent = await self._unified_registry.register(**unified_data)

            # Update initial state
            await self._unified_registry.heartbeat(
                agent.agent_id,
                load=initial_load,
                task_queue_size=initial_queue,
                health_score=initial_health,
            )

            # Track sync
            self._synced_from_mesh.add(mesh_agent.agent_name)
            self._metrics.event_propagations += 1

            self.logger.debug(f"Synced mesh agent '{mesh_agent.agent_name}' to unified")
            return True

        except Exception as e:
            self.logger.error(f"Failed to sync mesh agent to unified: {e}")
            return False

    async def _remove_from_mesh(self, unified_agent_id: str) -> bool:
        """Remove an agent from mesh when deregistered from unified."""
        if not self._mesh_coordinator:
            return False

        # Find corresponding mesh agent
        try:
            mesh_agents = await self._mesh_coordinator.registry.get_all_agents()
            for agent in mesh_agents:
                if agent.metadata.get("unified_agent_id") == unified_agent_id:
                    await self._mesh_coordinator.registry.unregister(agent.agent_name)
                    self._synced_from_unified.discard(unified_agent_id)
                    self.logger.debug(f"Removed synced agent from mesh: {agent.agent_name}")
                    return True
        except Exception as e:
            self.logger.error(f"Failed to remove from mesh: {e}")

        return False

    async def _remove_from_unified(self, mesh_agent_name: str) -> bool:
        """Remove an agent from unified when deregistered from mesh."""
        if not self._unified_registry:
            return False

        # Find corresponding unified agent
        try:
            unified_agents = self._unified_registry.get_all_agents()
            for agent in unified_agents:
                if agent.name == mesh_agent_name and agent.metadata.get("synced_from") == "neural_mesh":
                    await self._unified_registry.deregister(agent.agent_id)
                    self._synced_from_mesh.discard(mesh_agent_name)
                    self.logger.debug(f"Removed synced agent from unified: {mesh_agent_name}")
                    return True
        except Exception as e:
            self.logger.error(f"Failed to remove from unified: {e}")

        return False


# Global instance
_bridge: Optional[NeuralMeshBridge] = None
_bridge_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_registry_bridge() -> NeuralMeshBridge:
    """Get the global registry bridge instance."""
    global _bridge

    async with _bridge_lock:
        if _bridge is None:
            _bridge = NeuralMeshBridge()
            await _bridge.start()

        return _bridge


async def shutdown_registry_bridge() -> None:
    """Shutdown the global registry bridge."""
    global _bridge

    if _bridge:
        await _bridge.stop()
        _bridge = None
