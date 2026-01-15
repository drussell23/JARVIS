"""
Cross-Repository Resource Bridge v1.0
======================================

Provides resource coordination and synchronization across the Trinity ecosystem:
- JARVIS (Body) - Primary interface and execution
- JARVIS Prime (Mind) - Intelligence and decision making
- Reactor Core (Learning) - Training and model updates

Features:
- Cross-repo resource event bus
- Resource inventory synchronization
- Conflict resolution
- Priority-based arbitration
- Emergency resource reallocation

Author: Trinity Resource System
Version: 1.0.0
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
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
import uuid

from backend.core.resource_management.unified_engine import (
    ComponentType,
    ResourceType,
    ResourceState,
    AlertLevel,
    ResourceAlert,
    ResourceMetrics,
    UnifiedResourceCoordinator,
    get_resource_coordinator,
)

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================


class ResourceEventType(Enum):
    """Types of resource events."""
    ALLOCATION_REQUEST = auto()
    ALLOCATION_GRANTED = auto()
    ALLOCATION_DENIED = auto()
    RESOURCE_RELEASED = auto()
    RESOURCE_EXHAUSTED = auto()
    RESOURCE_RECOVERED = auto()
    THRESHOLD_WARNING = auto()
    THRESHOLD_CRITICAL = auto()
    EMERGENCY_REALLOCATION = auto()
    SYNC_REQUEST = auto()
    SYNC_COMPLETE = auto()
    HEARTBEAT = auto()


class SyncDirection(Enum):
    """Direction of resource synchronization."""
    JARVIS_TO_PRIME = auto()
    PRIME_TO_JARVIS = auto()
    JARVIS_TO_REACTOR = auto()
    REACTOR_TO_JARVIS = auto()
    PRIME_TO_REACTOR = auto()
    REACTOR_TO_PRIME = auto()
    BIDIRECTIONAL = auto()


class ConflictResolution(Enum):
    """Strategies for resolving resource conflicts."""
    PRIORITY_BASED = auto()
    FIRST_COME = auto()
    FAIR_SHARE = auto()
    EMERGENCY_PREEMPT = auto()


class RepoStatus(Enum):
    """Status of a repository in the ecosystem."""
    ONLINE = auto()
    OFFLINE = auto()
    DEGRADED = auto()
    SYNCING = auto()
    MAINTENANCE = auto()


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class CrossRepoResourceConfig:
    """Configuration for cross-repo resource coordination."""

    # Sync settings
    sync_interval: float = float(os.getenv("RESOURCE_SYNC_INTERVAL", "10.0"))
    sync_timeout: float = float(os.getenv("RESOURCE_SYNC_TIMEOUT", "30.0"))

    # Event bus settings
    event_queue_size: int = int(os.getenv("RESOURCE_EVENT_QUEUE_SIZE", "10000"))
    event_retention_hours: float = float(os.getenv("RESOURCE_EVENT_RETENTION_HOURS", "24.0"))

    # Heartbeat settings
    heartbeat_interval: float = float(os.getenv("RESOURCE_HEARTBEAT_INTERVAL", "5.0"))
    heartbeat_timeout: float = float(os.getenv("RESOURCE_HEARTBEAT_TIMEOUT", "15.0"))

    # Conflict resolution
    conflict_resolution: str = os.getenv("RESOURCE_CONFLICT_RESOLUTION", "PRIORITY_BASED")

    # Emergency settings
    emergency_threshold_memory: float = float(os.getenv("RESOURCE_EMERGENCY_MEMORY", "0.95"))
    emergency_threshold_disk: float = float(os.getenv("RESOURCE_EMERGENCY_DISK", "0.95"))

    # Repo paths
    jarvis_path: str = os.getenv("JARVIS_PATH", str(Path.home() / "Documents/repos/JARVIS-AI-Agent"))
    prime_path: str = os.getenv("PRIME_PATH", str(Path.home() / "Documents/repos/JARVIS-Prime"))
    reactor_path: str = os.getenv("REACTOR_PATH", str(Path.home() / "Documents/repos/Reactor-Core"))


# =============================================================================
# DATA STRUCTURES
# =============================================================================


@dataclass
class ResourceEvent:
    """A resource event for cross-repo communication."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: ResourceEventType = ResourceEventType.HEARTBEAT
    source_repo: ComponentType = ComponentType.JARVIS_BODY
    target_repo: Optional[ComponentType] = None
    resource_type: ResourceType = ResourceType.MEMORY
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    priority: int = 1
    correlation_id: Optional[str] = None


@dataclass
class ResourceInventory:
    """Resource inventory for a repository."""
    repo: ComponentType
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Ports
    ports_allocated: List[int] = field(default_factory=list)
    ports_reserved: List[int] = field(default_factory=list)

    # Memory
    memory_budget_mb: int = 0
    memory_used_mb: int = 0
    memory_available_mb: int = 0

    # CPU
    cpu_cores_allocated: List[int] = field(default_factory=list)
    cpu_usage_percent: float = 0.0

    # Disk
    disk_paths: List[str] = field(default_factory=list)
    disk_used_gb: float = 0.0
    disk_available_gb: float = 0.0

    # Network
    network_bandwidth_mbps: float = 0.0
    network_bytes_sent: int = 0
    network_bytes_received: int = 0


@dataclass
class AllocationRequest:
    """A request for resource allocation."""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    requester: ComponentType = ComponentType.JARVIS_BODY
    resource_type: ResourceType = ResourceType.MEMORY
    amount_requested: float = 0.0
    priority: int = 1
    justification: str = ""
    expires_at: Optional[datetime] = None
    callback: Optional[Callable] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class AllocationResponse:
    """Response to an allocation request."""
    request_id: str
    granted: bool = False
    amount_granted: float = 0.0
    reason: str = ""
    expires_at: Optional[datetime] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class RepoHealthStatus:
    """Health status of a repository."""
    repo: ComponentType
    status: RepoStatus = RepoStatus.ONLINE
    last_heartbeat: datetime = field(default_factory=datetime.utcnow)
    latency_ms: float = 0.0
    error_count: int = 0
    resource_health: Dict[ResourceType, bool] = field(default_factory=dict)


# =============================================================================
# RESOURCE EVENT BUS
# =============================================================================


class ResourceEventBus:
    """
    Event bus for cross-repo resource communication.

    Provides:
    - Async event publishing and subscription
    - Event filtering by type and source
    - Event history with retention
    - Priority-based delivery
    """

    def __init__(self, config: CrossRepoResourceConfig):
        self.config = config
        self._subscribers: Dict[ResourceEventType, List[Callable]] = defaultdict(list)
        self._global_subscribers: List[Callable] = []
        self._event_history: List[ResourceEvent] = []
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=config.event_queue_size)
        self._lock = asyncio.Lock()
        self._running = False
        self._processor_task: Optional[asyncio.Task] = None
        self.logger = logging.getLogger("ResourceEventBus")

    async def start(self):
        """Start the event bus."""
        if self._running:
            return

        self._running = True
        self._processor_task = asyncio.create_task(self._process_events())
        self.logger.info("Resource event bus started")

    async def stop(self):
        """Stop the event bus."""
        if not self._running:
            return

        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

        self.logger.info("Resource event bus stopped")

    async def publish(self, event: ResourceEvent):
        """Publish an event to the bus."""
        try:
            await self._queue.put(event)
        except asyncio.QueueFull:
            self.logger.warning("Event queue full, dropping oldest event")
            try:
                self._queue.get_nowait()
                await self._queue.put(event)
            except Exception:
                pass

    def subscribe(
        self,
        event_type: Optional[ResourceEventType] = None,
        callback: Callable = None,
    ):
        """Subscribe to events."""
        if callback is None:
            return

        if event_type is None:
            self._global_subscribers.append(callback)
        else:
            self._subscribers[event_type].append(callback)

    def unsubscribe(
        self,
        event_type: Optional[ResourceEventType] = None,
        callback: Callable = None,
    ):
        """Unsubscribe from events."""
        if callback is None:
            return

        if event_type is None:
            if callback in self._global_subscribers:
                self._global_subscribers.remove(callback)
        else:
            if callback in self._subscribers[event_type]:
                self._subscribers[event_type].remove(callback)

    async def get_history(
        self,
        event_type: Optional[ResourceEventType] = None,
        source_repo: Optional[ComponentType] = None,
        limit: int = 100,
    ) -> List[ResourceEvent]:
        """Get event history with optional filtering."""
        async with self._lock:
            events = self._event_history

            if event_type:
                events = [e for e in events if e.event_type == event_type]

            if source_repo:
                events = [e for e in events if e.source_repo == source_repo]

            return events[-limit:]

    async def _process_events(self):
        """Process events from the queue."""
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0
                )

                # Store in history
                async with self._lock:
                    self._event_history.append(event)

                    # Prune old events
                    cutoff = datetime.utcnow() - timedelta(hours=self.config.event_retention_hours)
                    self._event_history = [
                        e for e in self._event_history
                        if e.timestamp > cutoff
                    ]

                # Deliver to subscribers
                await self._deliver_event(event)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Event processing error: {e}")

    async def _deliver_event(self, event: ResourceEvent):
        """Deliver event to subscribers."""
        # Type-specific subscribers
        for callback in self._subscribers.get(event.event_type, []):
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                self.logger.error(f"Subscriber callback error: {e}")

        # Global subscribers
        for callback in self._global_subscribers:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                self.logger.error(f"Global subscriber callback error: {e}")


# =============================================================================
# CROSS-REPO RESOURCE BRIDGE
# =============================================================================


class CrossRepoResourceBridge:
    """
    Bridge for cross-repository resource coordination.

    Manages:
    - Resource inventory synchronization
    - Cross-repo allocation requests
    - Conflict resolution
    - Emergency reallocation
    """

    def __init__(self, config: Optional[CrossRepoResourceConfig] = None):
        self.config = config or CrossRepoResourceConfig()
        self.logger = logging.getLogger("CrossRepoResourceBridge")

        # Event bus
        self.event_bus = ResourceEventBus(self.config)

        # State
        self._running = False
        self._inventories: Dict[ComponentType, ResourceInventory] = {}
        self._health_status: Dict[ComponentType, RepoHealthStatus] = {}
        self._pending_requests: Dict[str, AllocationRequest] = {}
        self._allocation_callbacks: Dict[str, Callable] = {}

        # Tasks
        self._sync_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

        # Locks
        self._lock = asyncio.Lock()

        # Register event handlers
        self.event_bus.subscribe(ResourceEventType.ALLOCATION_REQUEST, self._handle_allocation_request)
        self.event_bus.subscribe(ResourceEventType.SYNC_REQUEST, self._handle_sync_request)
        self.event_bus.subscribe(ResourceEventType.HEARTBEAT, self._handle_heartbeat)

    async def initialize(self) -> bool:
        """Initialize the bridge."""
        try:
            # Initialize health status for all repos
            for component in [ComponentType.JARVIS_BODY, ComponentType.JARVIS_PRIME, ComponentType.REACTOR_CORE]:
                self._health_status[component] = RepoHealthStatus(
                    repo=component,
                    status=RepoStatus.OFFLINE,
                )
                self._inventories[component] = ResourceInventory(repo=component)

            self.logger.info("CrossRepoResourceBridge initialized")
            return True

        except Exception as e:
            self.logger.error(f"Initialization failed: {e}")
            return False

    async def start(self):
        """Start the bridge."""
        if self._running:
            return

        self._running = True

        # Start event bus
        await self.event_bus.start()

        # Start background tasks
        self._sync_task = asyncio.create_task(self._sync_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        self.logger.info("CrossRepoResourceBridge started")

    async def stop(self):
        """Stop the bridge."""
        if not self._running:
            return

        self._running = False

        # Cancel tasks
        for task in [self._sync_task, self._heartbeat_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Stop event bus
        await self.event_bus.stop()

        self.logger.info("CrossRepoResourceBridge stopped")

    async def shutdown(self):
        """Complete shutdown."""
        await self.stop()

    # =========================================================================
    # Resource Allocation
    # =========================================================================

    async def request_allocation(
        self,
        requester: ComponentType,
        resource_type: ResourceType,
        amount: float,
        priority: int = 1,
        justification: str = "",
    ) -> AllocationResponse:
        """Request resource allocation from another repo."""
        request = AllocationRequest(
            requester=requester,
            resource_type=resource_type,
            amount_requested=amount,
            priority=priority,
            justification=justification,
            expires_at=datetime.utcnow() + timedelta(seconds=60),
        )

        async with self._lock:
            self._pending_requests[request.request_id] = request

        # Publish allocation request event
        event = ResourceEvent(
            event_type=ResourceEventType.ALLOCATION_REQUEST,
            source_repo=requester,
            resource_type=resource_type,
            payload={
                "request_id": request.request_id,
                "amount": amount,
                "priority": priority,
                "justification": justification,
            },
            priority=priority,
        )
        await self.event_bus.publish(event)

        # Wait for response (with timeout)
        response_future = asyncio.get_event_loop().create_future()
        self._allocation_callbacks[request.request_id] = lambda resp: response_future.set_result(resp)

        try:
            response = await asyncio.wait_for(response_future, timeout=30.0)
            return response
        except asyncio.TimeoutError:
            return AllocationResponse(
                request_id=request.request_id,
                granted=False,
                reason="Request timed out",
            )
        finally:
            self._allocation_callbacks.pop(request.request_id, None)
            async with self._lock:
                self._pending_requests.pop(request.request_id, None)

    async def grant_allocation(
        self,
        request_id: str,
        amount_granted: float,
        expires_at: Optional[datetime] = None,
    ):
        """Grant a resource allocation request."""
        response = AllocationResponse(
            request_id=request_id,
            granted=True,
            amount_granted=amount_granted,
            expires_at=expires_at or datetime.utcnow() + timedelta(hours=1),
        )

        # Notify requester
        if request_id in self._allocation_callbacks:
            callback = self._allocation_callbacks[request_id]
            callback(response)

        # Publish grant event
        event = ResourceEvent(
            event_type=ResourceEventType.ALLOCATION_GRANTED,
            source_repo=ComponentType.JARVIS_BODY,
            payload={
                "request_id": request_id,
                "amount_granted": amount_granted,
            },
        )
        await self.event_bus.publish(event)

    async def deny_allocation(self, request_id: str, reason: str = ""):
        """Deny a resource allocation request."""
        response = AllocationResponse(
            request_id=request_id,
            granted=False,
            reason=reason,
        )

        # Notify requester
        if request_id in self._allocation_callbacks:
            callback = self._allocation_callbacks[request_id]
            callback(response)

        # Publish denial event
        event = ResourceEvent(
            event_type=ResourceEventType.ALLOCATION_DENIED,
            source_repo=ComponentType.JARVIS_BODY,
            payload={
                "request_id": request_id,
                "reason": reason,
            },
        )
        await self.event_bus.publish(event)

    # =========================================================================
    # Inventory Management
    # =========================================================================

    async def update_inventory(
        self,
        repo: ComponentType,
        inventory: ResourceInventory,
    ):
        """Update inventory for a repository."""
        async with self._lock:
            self._inventories[repo] = inventory

        # Check for emergency conditions
        await self._check_emergency_conditions(repo, inventory)

    async def get_inventory(
        self,
        repo: Optional[ComponentType] = None,
    ) -> Dict[ComponentType, ResourceInventory]:
        """Get resource inventory."""
        async with self._lock:
            if repo:
                return {repo: self._inventories.get(repo, ResourceInventory(repo=repo))}
            return self._inventories.copy()

    async def get_aggregated_inventory(self) -> Dict[str, Any]:
        """Get aggregated inventory across all repos."""
        async with self._lock:
            total_memory_used = 0
            total_memory_available = 0
            total_disk_used = 0.0
            total_disk_available = 0.0
            all_ports = []

            for inventory in self._inventories.values():
                total_memory_used += inventory.memory_used_mb
                total_memory_available += inventory.memory_available_mb
                total_disk_used += inventory.disk_used_gb
                total_disk_available += inventory.disk_available_gb
                all_ports.extend(inventory.ports_allocated)

            return {
                "total_memory_used_mb": total_memory_used,
                "total_memory_available_mb": total_memory_available,
                "total_disk_used_gb": total_disk_used,
                "total_disk_available_gb": total_disk_available,
                "ports_in_use": len(all_ports),
                "repo_count": len(self._inventories),
            }

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def get_health(
        self,
        repo: Optional[ComponentType] = None,
    ) -> Dict[ComponentType, RepoHealthStatus]:
        """Get health status of repositories."""
        async with self._lock:
            if repo:
                return {repo: self._health_status.get(repo, RepoHealthStatus(repo=repo))}
            return self._health_status.copy()

    async def is_repo_healthy(self, repo: ComponentType) -> bool:
        """Check if a repository is healthy."""
        async with self._lock:
            status = self._health_status.get(repo)
            if not status:
                return False
            return status.status in [RepoStatus.ONLINE, RepoStatus.SYNCING]

    # =========================================================================
    # Event Handlers
    # =========================================================================

    async def _handle_allocation_request(self, event: ResourceEvent):
        """Handle incoming allocation request."""
        request_id = event.payload.get("request_id")
        amount = event.payload.get("amount", 0)
        priority = event.payload.get("priority", 1)

        # Get coordinator and check availability
        try:
            coordinator = await get_resource_coordinator()

            # Simple availability check based on resource type
            if event.resource_type == ResourceType.MEMORY:
                metrics = await coordinator.get_metrics()
                available = metrics.memory_available_mb

                if available >= amount:
                    await self.grant_allocation(request_id, amount)
                else:
                    await self.deny_allocation(
                        request_id,
                        f"Insufficient memory: requested {amount}MB, available {available}MB"
                    )

            elif event.resource_type == ResourceType.PORT:
                # Check port availability
                ports = await coordinator.ports.reserve(
                    event.source_repo,
                    count=int(amount),
                )
                if ports:
                    await self.grant_allocation(request_id, len(ports))
                else:
                    await self.deny_allocation(request_id, "No ports available")

            else:
                # Default: grant if requested
                await self.grant_allocation(request_id, amount)

        except Exception as e:
            self.logger.error(f"Allocation request handling failed: {e}")
            await self.deny_allocation(request_id, str(e))

    async def _handle_sync_request(self, event: ResourceEvent):
        """Handle sync request from another repo."""
        source = event.source_repo

        try:
            # Get current coordinator status
            coordinator = await get_resource_coordinator()
            status = await coordinator.get_status()

            # Build inventory
            inventory = ResourceInventory(
                repo=ComponentType.JARVIS_BODY,
                memory_used_mb=status.get("memory", {}).get("used_mb", 0),
                memory_available_mb=status.get("memory", {}).get("total_mb", 0) - status.get("memory", {}).get("used_mb", 0),
                disk_used_gb=status.get("disk", {}).get("used_gb", 0),
                disk_available_gb=status.get("disk", {}).get("total_gb", 0) - status.get("disk", {}).get("used_gb", 0),
            )

            # Publish sync complete
            event = ResourceEvent(
                event_type=ResourceEventType.SYNC_COMPLETE,
                source_repo=ComponentType.JARVIS_BODY,
                target_repo=source,
                payload={"inventory": inventory.__dict__},
            )
            await self.event_bus.publish(event)

        except Exception as e:
            self.logger.error(f"Sync request handling failed: {e}")

    async def _handle_heartbeat(self, event: ResourceEvent):
        """Handle heartbeat from another repo."""
        source = event.source_repo

        async with self._lock:
            if source not in self._health_status:
                self._health_status[source] = RepoHealthStatus(repo=source)

            self._health_status[source].status = RepoStatus.ONLINE
            self._health_status[source].last_heartbeat = datetime.utcnow()
            self._health_status[source].latency_ms = event.payload.get("latency_ms", 0)

    # =========================================================================
    # Background Tasks
    # =========================================================================

    async def _sync_loop(self):
        """Periodic sync with other repos."""
        while self._running:
            try:
                await asyncio.sleep(self.config.sync_interval)

                # Request sync from all repos
                for component in [ComponentType.JARVIS_PRIME, ComponentType.REACTOR_CORE]:
                    event = ResourceEvent(
                        event_type=ResourceEventType.SYNC_REQUEST,
                        source_repo=ComponentType.JARVIS_BODY,
                        target_repo=component,
                    )
                    await self.event_bus.publish(event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Sync loop error: {e}")

    async def _heartbeat_loop(self):
        """Periodic heartbeat to other repos."""
        while self._running:
            try:
                await asyncio.sleep(self.config.heartbeat_interval)

                # Send heartbeat
                event = ResourceEvent(
                    event_type=ResourceEventType.HEARTBEAT,
                    source_repo=ComponentType.JARVIS_BODY,
                    payload={
                        "timestamp": time.time(),
                    },
                )
                await self.event_bus.publish(event)

                # Check for stale repos
                await self._check_stale_repos()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Heartbeat loop error: {e}")

    async def _check_stale_repos(self):
        """Check for repos that have gone offline."""
        now = datetime.utcnow()
        timeout = timedelta(seconds=self.config.heartbeat_timeout)

        async with self._lock:
            for repo, status in self._health_status.items():
                if status.status == RepoStatus.ONLINE:
                    if now - status.last_heartbeat > timeout:
                        status.status = RepoStatus.OFFLINE
                        self.logger.warning(f"Repository {repo.value} appears offline")

    async def _check_emergency_conditions(self, repo: ComponentType, inventory: ResourceInventory):
        """Check for emergency resource conditions."""
        # Check memory
        if inventory.memory_budget_mb > 0:
            usage = inventory.memory_used_mb / inventory.memory_budget_mb
            if usage >= self.config.emergency_threshold_memory:
                event = ResourceEvent(
                    event_type=ResourceEventType.EMERGENCY_REALLOCATION,
                    source_repo=repo,
                    resource_type=ResourceType.MEMORY,
                    payload={
                        "usage_percent": usage * 100,
                        "used_mb": inventory.memory_used_mb,
                        "budget_mb": inventory.memory_budget_mb,
                    },
                    priority=10,  # High priority
                )
                await self.event_bus.publish(event)

        # Check disk
        if inventory.disk_available_gb + inventory.disk_used_gb > 0:
            total = inventory.disk_available_gb + inventory.disk_used_gb
            usage = inventory.disk_used_gb / total
            if usage >= self.config.emergency_threshold_disk:
                event = ResourceEvent(
                    event_type=ResourceEventType.EMERGENCY_REALLOCATION,
                    source_repo=repo,
                    resource_type=ResourceType.DISK,
                    payload={
                        "usage_percent": usage * 100,
                        "used_gb": inventory.disk_used_gb,
                        "total_gb": total,
                    },
                    priority=10,
                )
                await self.event_bus.publish(event)


# =============================================================================
# GLOBAL INSTANCE MANAGEMENT
# =============================================================================

_bridge: Optional[CrossRepoResourceBridge] = None
_bridge_lock = asyncio.Lock()


async def get_cross_repo_resource_bridge() -> CrossRepoResourceBridge:
    """Get or create the global bridge instance."""
    global _bridge

    async with _bridge_lock:
        if _bridge is None:
            _bridge = CrossRepoResourceBridge()
            await _bridge.initialize()
        return _bridge


async def initialize_cross_repo_resources() -> bool:
    """Initialize the global resource bridge."""
    bridge = await get_cross_repo_resource_bridge()
    await bridge.start()
    return True


async def shutdown_cross_repo_resources():
    """Shutdown the global resource bridge."""
    global _bridge

    async with _bridge_lock:
        if _bridge is not None:
            await _bridge.shutdown()
            _bridge = None
            logger.info("Cross-repo resource bridge shutdown")


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Configuration
    "CrossRepoResourceConfig",
    # Enums
    "ResourceEventType",
    "SyncDirection",
    "ConflictResolution",
    "RepoStatus",
    # Data Structures
    "ResourceEvent",
    "ResourceInventory",
    "AllocationRequest",
    "AllocationResponse",
    "RepoHealthStatus",
    # Event Bus
    "ResourceEventBus",
    # Bridge
    "CrossRepoResourceBridge",
    # Global Functions
    "get_cross_repo_resource_bridge",
    "initialize_cross_repo_resources",
    "shutdown_cross_repo_resources",
]
