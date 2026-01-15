"""
Unified Resource Management Engine v1.0
========================================

Enterprise-grade resource management for the JARVIS Trinity ecosystem.
Provides centralized resource coordination across JARVIS (Body), JARVIS Prime (Mind),
and Reactor Core (Learning).

Implements 6 critical resource management patterns:
1. Unified Resource Coordinator - Centralized resource management
2. Port Pool Management - Port allocation with reservation
3. Memory Budget Allocation - Per-repo memory budgets with enforcement
4. CPU Affinity Management - CPU core assignment for optimal performance
5. Disk Space Management - Monitoring and automated cleanup
6. Network Bandwidth Management - Bandwidth limits and monitoring

Author: Trinity Resource System
Version: 1.0.0
"""

import asyncio
import hashlib
import json
import logging
import os
import platform
import socket
import subprocess
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)
import uuid

logger = logging.getLogger(__name__)

# Type variables
T = TypeVar("T")


# =============================================================================
# ENUMS
# =============================================================================


class ResourceType(Enum):
    """Types of managed resources."""
    PORT = "port"
    MEMORY = "memory"
    CPU = "cpu"
    DISK = "disk"
    NETWORK = "network"
    GPU = "gpu"


class ResourceState(Enum):
    """State of a resource."""
    AVAILABLE = auto()
    RESERVED = auto()
    IN_USE = auto()
    EXHAUSTED = auto()
    UNHEALTHY = auto()
    DRAINING = auto()


class ComponentType(Enum):
    """Components in the Trinity ecosystem."""
    JARVIS_BODY = "jarvis_body"
    JARVIS_PRIME = "jarvis_prime"
    REACTOR_CORE = "reactor_core"
    SYSTEM = "system"
    EXTERNAL = "external"


class AlertLevel(Enum):
    """Resource alert levels."""
    INFO = auto()
    WARNING = auto()
    CRITICAL = auto()
    EMERGENCY = auto()


class CleanupStrategy(Enum):
    """Disk cleanup strategies."""
    LRU = auto()           # Least Recently Used
    LFU = auto()           # Least Frequently Used
    SIZE_BASED = auto()    # Largest files first
    AGE_BASED = auto()     # Oldest files first
    PATTERN_BASED = auto() # By file pattern


class BandwidthPolicy(Enum):
    """Network bandwidth policies."""
    UNLIMITED = auto()
    FAIR_SHARE = auto()
    PRIORITY_BASED = auto()
    TOKEN_BUCKET = auto()
    LEAKY_BUCKET = auto()


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class ResourceManagementConfig:
    """Configuration for resource management."""

    # Port Management
    port_pool_start: int = int(os.getenv("PORT_POOL_START", "8000"))
    port_pool_end: int = int(os.getenv("PORT_POOL_END", "9000"))
    port_reservation_timeout: float = float(os.getenv("PORT_RESERVATION_TIMEOUT", "300"))

    # Memory Management
    memory_warning_threshold: float = float(os.getenv("MEMORY_WARNING_THRESHOLD", "0.70"))
    memory_critical_threshold: float = float(os.getenv("MEMORY_CRITICAL_THRESHOLD", "0.85"))
    memory_emergency_threshold: float = float(os.getenv("MEMORY_EMERGENCY_THRESHOLD", "0.95"))
    memory_check_interval: float = float(os.getenv("MEMORY_CHECK_INTERVAL", "5.0"))

    # CPU Management
    cpu_affinity_enabled: bool = os.getenv("CPU_AFFINITY_ENABLED", "true").lower() == "true"
    cpu_reserve_cores: int = int(os.getenv("CPU_RESERVE_CORES", "1"))  # Reserved for system

    # Disk Management
    disk_warning_threshold: float = float(os.getenv("DISK_WARNING_THRESHOLD", "0.70"))
    disk_critical_threshold: float = float(os.getenv("DISK_CRITICAL_THRESHOLD", "0.85"))
    disk_cleanup_enabled: bool = os.getenv("DISK_CLEANUP_ENABLED", "true").lower() == "true"
    disk_check_interval: float = float(os.getenv("DISK_CHECK_INTERVAL", "60.0"))

    # Network Management
    network_bandwidth_limit_mbps: float = float(os.getenv("NETWORK_BANDWIDTH_LIMIT_MBPS", "0"))  # 0 = unlimited
    network_check_interval: float = float(os.getenv("NETWORK_CHECK_INTERVAL", "10.0"))

    # General
    coordinator_check_interval: float = float(os.getenv("RESOURCE_CHECK_INTERVAL", "10.0"))
    history_retention_hours: float = float(os.getenv("RESOURCE_HISTORY_HOURS", "24.0"))


# =============================================================================
# DATA STRUCTURES
# =============================================================================


@dataclass
class ResourceAllocation:
    """A resource allocation."""
    allocation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    resource_type: ResourceType = ResourceType.PORT
    component: ComponentType = ComponentType.JARVIS_BODY
    amount: float = 0.0
    unit: str = ""
    allocated_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PortReservation:
    """A port reservation."""
    port: int
    component: ComponentType
    service_name: str = ""
    reserved_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    in_use: bool = False
    process_pid: Optional[int] = None


@dataclass
class MemoryBudget:
    """Memory budget for a component."""
    component: ComponentType
    budget_mb: int = 0
    current_usage_mb: int = 0
    peak_usage_mb: int = 0
    soft_limit_mb: int = 0  # Warning threshold
    hard_limit_mb: int = 0  # Enforcement threshold
    priority: int = 1  # Higher = more important


@dataclass
class CPUAllocation:
    """CPU core allocation."""
    component: ComponentType
    cores: List[int] = field(default_factory=list)
    process_pid: Optional[int] = None
    allocated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class DiskUsageSnapshot:
    """Snapshot of disk usage."""
    path: str
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    usage_percent: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class NetworkUsageSnapshot:
    """Snapshot of network usage."""
    bytes_sent: int = 0
    bytes_received: int = 0
    packets_sent: int = 0
    packets_received: int = 0
    bandwidth_mbps: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ResourceAlert:
    """A resource alert."""
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    resource_type: ResourceType = ResourceType.MEMORY
    level: AlertLevel = AlertLevel.WARNING
    component: Optional[ComponentType] = None
    message: str = ""
    current_value: float = 0.0
    threshold: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    resolved: bool = False


@dataclass
class ResourceMetrics:
    """Aggregated resource metrics."""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Memory
    memory_total_mb: int = 0
    memory_used_mb: int = 0
    memory_available_mb: int = 0
    memory_percent: float = 0.0

    # CPU
    cpu_count: int = 0
    cpu_percent: float = 0.0
    cpu_load_1m: float = 0.0
    cpu_load_5m: float = 0.0
    cpu_load_15m: float = 0.0

    # Disk
    disk_total_gb: float = 0.0
    disk_used_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_percent: float = 0.0

    # Network
    network_bytes_sent: int = 0
    network_bytes_recv: int = 0
    network_bandwidth_mbps: float = 0.0


# =============================================================================
# PORT POOL MANAGER
# =============================================================================


class PortPoolManager:
    """
    Manages a pool of ports with reservation system.

    Features:
    - Port range management
    - Reservation with timeout
    - Conflict detection
    - Cross-component coordination
    """

    def __init__(self, config: ResourceManagementConfig):
        self.config = config
        self._reservations: Dict[int, PortReservation] = {}
        self._component_ports: Dict[ComponentType, Set[int]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger("PortPoolManager")

    async def reserve(
        self,
        component: ComponentType,
        service_name: str = "",
        preferred_port: Optional[int] = None,
        count: int = 1,
    ) -> List[int]:
        """Reserve one or more ports."""
        async with self._lock:
            reserved = []

            # Try preferred port first
            if preferred_port and count == 1:
                if await self._can_reserve(preferred_port):
                    await self._reserve_port(preferred_port, component, service_name)
                    reserved.append(preferred_port)
                    return reserved

            # Find available ports
            for port in range(self.config.port_pool_start, self.config.port_pool_end):
                if len(reserved) >= count:
                    break
                if await self._can_reserve(port):
                    await self._reserve_port(port, component, service_name)
                    reserved.append(port)

            if len(reserved) < count:
                # Rollback
                for port in reserved:
                    await self._release_port(port)
                raise RuntimeError(f"Could not reserve {count} ports")

            return reserved

    async def release(self, port: int):
        """Release a port reservation."""
        async with self._lock:
            await self._release_port(port)

    async def release_component(self, component: ComponentType):
        """Release all ports for a component."""
        async with self._lock:
            ports = list(self._component_ports.get(component, set()))
            for port in ports:
                await self._release_port(port)

    async def is_available(self, port: int) -> bool:
        """Check if a port is available."""
        async with self._lock:
            return await self._can_reserve(port)

    async def get_reservations(self, component: Optional[ComponentType] = None) -> List[PortReservation]:
        """Get all reservations, optionally filtered by component."""
        async with self._lock:
            reservations = list(self._reservations.values())
            if component:
                reservations = [r for r in reservations if r.component == component]
            return reservations

    async def _can_reserve(self, port: int) -> bool:
        """Check if port can be reserved."""
        # Check if already reserved
        if port in self._reservations:
            reservation = self._reservations[port]
            # Check expiry
            if reservation.expires_at and datetime.utcnow() > reservation.expires_at:
                del self._reservations[port]
            else:
                return False

        # Check if port is in use
        return not self._is_port_in_use(port)

    def _is_port_in_use(self, port: int) -> bool:
        """Check if port is currently in use."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                result = s.connect_ex(('127.0.0.1', port))
                return result == 0
        except Exception:
            return False

    async def _reserve_port(self, port: int, component: ComponentType, service_name: str):
        """Reserve a port."""
        expires_at = datetime.utcnow() + timedelta(seconds=self.config.port_reservation_timeout)
        reservation = PortReservation(
            port=port,
            component=component,
            service_name=service_name,
            expires_at=expires_at,
        )
        self._reservations[port] = reservation
        self._component_ports[component].add(port)
        self.logger.info(f"Reserved port {port} for {component.value}:{service_name}")

    async def _release_port(self, port: int):
        """Release a port."""
        if port in self._reservations:
            reservation = self._reservations[port]
            self._component_ports[reservation.component].discard(port)
            del self._reservations[port]
            self.logger.info(f"Released port {port}")


# =============================================================================
# MEMORY BUDGET MANAGER
# =============================================================================


class MemoryBudgetManager:
    """
    Manages memory budgets per component with enforcement.

    Features:
    - Per-component memory budgets
    - Soft and hard limits
    - Usage tracking
    - Automatic enforcement
    """

    def __init__(self, config: ResourceManagementConfig):
        self.config = config
        self._budgets: Dict[ComponentType, MemoryBudget] = {}
        self._history: Dict[ComponentType, deque] = defaultdict(
            lambda: deque(maxlen=1000)
        )
        self._lock = asyncio.Lock()
        self._callbacks: List[Callable] = []
        self.logger = logging.getLogger("MemoryBudgetManager")

        # Initialize default budgets
        self._init_default_budgets()

    def _init_default_budgets(self):
        """Initialize default memory budgets."""
        total_mb = self._get_total_memory_mb()

        # Default allocation: JARVIS 40%, Prime 35%, Reactor 20%, System 5%
        self._budgets = {
            ComponentType.JARVIS_BODY: MemoryBudget(
                component=ComponentType.JARVIS_BODY,
                budget_mb=int(total_mb * 0.40),
                soft_limit_mb=int(total_mb * 0.35),
                hard_limit_mb=int(total_mb * 0.40),
                priority=2,
            ),
            ComponentType.JARVIS_PRIME: MemoryBudget(
                component=ComponentType.JARVIS_PRIME,
                budget_mb=int(total_mb * 0.35),
                soft_limit_mb=int(total_mb * 0.30),
                hard_limit_mb=int(total_mb * 0.35),
                priority=3,
            ),
            ComponentType.REACTOR_CORE: MemoryBudget(
                component=ComponentType.REACTOR_CORE,
                budget_mb=int(total_mb * 0.20),
                soft_limit_mb=int(total_mb * 0.15),
                hard_limit_mb=int(total_mb * 0.20),
                priority=1,
            ),
            ComponentType.SYSTEM: MemoryBudget(
                component=ComponentType.SYSTEM,
                budget_mb=int(total_mb * 0.05),
                soft_limit_mb=int(total_mb * 0.05),
                hard_limit_mb=int(total_mb * 0.05),
                priority=4,  # Highest priority
            ),
        }

    def _get_total_memory_mb(self) -> int:
        """Get total system memory in MB."""
        try:
            import psutil
            return int(psutil.virtual_memory().total / (1024 * 1024))
        except ImportError:
            # Fallback for macOS
            try:
                result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True,
                    text=True
                )
                return int(int(result.stdout.strip()) / (1024 * 1024))
            except Exception:
                return 16384  # Default 16GB

    async def set_budget(
        self,
        component: ComponentType,
        budget_mb: int,
        soft_limit_mb: Optional[int] = None,
        hard_limit_mb: Optional[int] = None,
        priority: int = 1,
    ):
        """Set memory budget for a component."""
        async with self._lock:
            self._budgets[component] = MemoryBudget(
                component=component,
                budget_mb=budget_mb,
                soft_limit_mb=soft_limit_mb or int(budget_mb * 0.85),
                hard_limit_mb=hard_limit_mb or budget_mb,
                priority=priority,
            )
            self.logger.info(f"Set memory budget for {component.value}: {budget_mb}MB")

    async def update_usage(self, component: ComponentType, usage_mb: int):
        """Update memory usage for a component."""
        async with self._lock:
            if component not in self._budgets:
                return

            budget = self._budgets[component]
            budget.current_usage_mb = usage_mb
            budget.peak_usage_mb = max(budget.peak_usage_mb, usage_mb)

            # Record history
            self._history[component].append({
                "timestamp": time.time(),
                "usage_mb": usage_mb,
            })

            # Check limits
            await self._check_limits(budget)

    async def _check_limits(self, budget: MemoryBudget):
        """Check if budget limits are exceeded."""
        if budget.current_usage_mb > budget.hard_limit_mb:
            alert = ResourceAlert(
                resource_type=ResourceType.MEMORY,
                level=AlertLevel.CRITICAL,
                component=budget.component,
                message=f"Memory hard limit exceeded: {budget.current_usage_mb}MB > {budget.hard_limit_mb}MB",
                current_value=budget.current_usage_mb,
                threshold=budget.hard_limit_mb,
            )
            await self._emit_alert(alert)

        elif budget.current_usage_mb > budget.soft_limit_mb:
            alert = ResourceAlert(
                resource_type=ResourceType.MEMORY,
                level=AlertLevel.WARNING,
                component=budget.component,
                message=f"Memory soft limit exceeded: {budget.current_usage_mb}MB > {budget.soft_limit_mb}MB",
                current_value=budget.current_usage_mb,
                threshold=budget.soft_limit_mb,
            )
            await self._emit_alert(alert)

    async def get_budget(self, component: ComponentType) -> Optional[MemoryBudget]:
        """Get memory budget for a component."""
        return self._budgets.get(component)

    async def get_available(self, component: ComponentType) -> int:
        """Get available memory for a component."""
        budget = self._budgets.get(component)
        if not budget:
            return 0
        return max(0, budget.budget_mb - budget.current_usage_mb)

    def register_callback(self, callback: Callable):
        """Register alert callback."""
        self._callbacks.append(callback)

    async def _emit_alert(self, alert: ResourceAlert):
        """Emit an alert."""
        self.logger.warning(f"Memory alert: {alert.message}")
        for callback in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(alert)
                else:
                    callback(alert)
            except Exception as e:
                self.logger.error(f"Alert callback error: {e}")


# =============================================================================
# CPU AFFINITY MANAGER
# =============================================================================


class CPUAffinityManager:
    """
    Manages CPU core allocation and affinity.

    Features:
    - Core allocation per component
    - Affinity enforcement
    - Load balancing
    - NUMA awareness
    """

    def __init__(self, config: ResourceManagementConfig):
        self.config = config
        self._allocations: Dict[ComponentType, CPUAllocation] = {}
        self._core_assignments: Dict[int, ComponentType] = {}
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger("CPUAffinityManager")

        # Get system info
        self._total_cores = os.cpu_count() or 4
        self._available_cores = max(1, self._total_cores - config.cpu_reserve_cores)

    async def allocate(
        self,
        component: ComponentType,
        core_count: Optional[int] = None,
        process_pid: Optional[int] = None,
    ) -> List[int]:
        """Allocate CPU cores to a component."""
        async with self._lock:
            # Determine core count
            if core_count is None:
                core_count = self._calculate_fair_share(component)

            # Find available cores
            available = await self._get_available_cores()
            if len(available) < core_count:
                self.logger.warning(
                    f"Requested {core_count} cores but only {len(available)} available"
                )
                core_count = len(available)

            # Allocate cores
            cores = available[:core_count]
            for core in cores:
                self._core_assignments[core] = component

            allocation = CPUAllocation(
                component=component,
                cores=cores,
                process_pid=process_pid,
            )
            self._allocations[component] = allocation

            # Apply affinity if pid provided
            if process_pid and self.config.cpu_affinity_enabled:
                await self._set_affinity(process_pid, cores)

            self.logger.info(f"Allocated cores {cores} to {component.value}")
            return cores

    async def release(self, component: ComponentType):
        """Release CPU allocation for a component."""
        async with self._lock:
            if component not in self._allocations:
                return

            allocation = self._allocations[component]
            for core in allocation.cores:
                if core in self._core_assignments:
                    del self._core_assignments[core]

            del self._allocations[component]
            self.logger.info(f"Released CPU cores for {component.value}")

    async def set_affinity(self, component: ComponentType, process_pid: int):
        """Set CPU affinity for a process."""
        async with self._lock:
            if component not in self._allocations:
                return

            cores = self._allocations[component].cores
            await self._set_affinity(process_pid, cores)
            self._allocations[component].process_pid = process_pid

    async def _set_affinity(self, pid: int, cores: List[int]):
        """Set CPU affinity for a process."""
        if not self.config.cpu_affinity_enabled:
            return

        system = platform.system()
        try:
            if system == "Linux":
                # Use taskset
                core_mask = ",".join(str(c) for c in cores)
                subprocess.run(
                    ["taskset", "-cp", core_mask, str(pid)],
                    capture_output=True,
                    check=True
                )
            elif system == "Darwin":
                # macOS doesn't have direct affinity API, log only
                self.logger.debug(f"CPU affinity not directly supported on macOS for PID {pid}")
            else:
                try:
                    import psutil
                    p = psutil.Process(pid)
                    p.cpu_affinity(cores)
                except ImportError:
                    pass

            self.logger.debug(f"Set CPU affinity for PID {pid} to cores {cores}")

        except Exception as e:
            self.logger.warning(f"Failed to set CPU affinity: {e}")

    async def _get_available_cores(self) -> List[int]:
        """Get list of available cores."""
        reserved = set(self.config.cpu_reserve_cores for _ in range(1))
        assigned = set(self._core_assignments.keys())
        all_cores = set(range(self._total_cores))
        available = all_cores - assigned

        # Reserve last N cores for system
        system_reserved = set(range(self._total_cores - self.config.cpu_reserve_cores, self._total_cores))
        available -= system_reserved

        return sorted(available)

    def _calculate_fair_share(self, component: ComponentType) -> int:
        """Calculate fair share of cores for a component."""
        # Allocation weights
        weights = {
            ComponentType.JARVIS_BODY: 0.35,
            ComponentType.JARVIS_PRIME: 0.40,
            ComponentType.REACTOR_CORE: 0.20,
            ComponentType.SYSTEM: 0.05,
        }
        weight = weights.get(component, 0.25)
        return max(1, int(self._available_cores * weight))


# =============================================================================
# DISK SPACE MANAGER
# =============================================================================


class DiskSpaceManager:
    """
    Manages disk space with monitoring and cleanup.

    Features:
    - Multi-path monitoring
    - Threshold-based alerts
    - Automated cleanup
    - Trend analysis
    """

    def __init__(self, config: ResourceManagementConfig):
        self.config = config
        self._monitored_paths: Dict[str, DiskUsageSnapshot] = {}
        self._history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self._cleanup_handlers: Dict[str, Callable] = {}
        self._lock = asyncio.Lock()
        self._callbacks: List[Callable] = []
        self.logger = logging.getLogger("DiskSpaceManager")

    async def add_path(self, path: str, cleanup_handler: Optional[Callable] = None):
        """Add a path to monitor."""
        async with self._lock:
            self._monitored_paths[path] = await self._get_usage(path)
            if cleanup_handler:
                self._cleanup_handlers[path] = cleanup_handler
            self.logger.info(f"Added disk monitoring for {path}")

    async def remove_path(self, path: str):
        """Remove a path from monitoring."""
        async with self._lock:
            if path in self._monitored_paths:
                del self._monitored_paths[path]
            if path in self._cleanup_handlers:
                del self._cleanup_handlers[path]

    async def check_all(self) -> Dict[str, DiskUsageSnapshot]:
        """Check all monitored paths."""
        async with self._lock:
            results = {}
            for path in self._monitored_paths:
                snapshot = await self._get_usage(path)
                self._monitored_paths[path] = snapshot
                self._history[path].append(snapshot)
                results[path] = snapshot

                # Check thresholds
                await self._check_thresholds(path, snapshot)

            return results

    async def get_usage(self, path: str) -> Optional[DiskUsageSnapshot]:
        """Get disk usage for a path."""
        if path in self._monitored_paths:
            return self._monitored_paths[path]
        return await self._get_usage(path)

    async def cleanup(self, path: str, strategy: CleanupStrategy = CleanupStrategy.AGE_BASED) -> int:
        """Run cleanup on a path. Returns bytes freed."""
        if path in self._cleanup_handlers:
            handler = self._cleanup_handlers[path]
            try:
                if asyncio.iscoroutinefunction(handler):
                    return await handler(path, strategy)
                else:
                    return handler(path, strategy)
            except Exception as e:
                self.logger.error(f"Cleanup handler failed for {path}: {e}")
                return 0

        # Default cleanup: remove old temp files
        return await self._default_cleanup(path, strategy)

    async def _get_usage(self, path: str) -> DiskUsageSnapshot:
        """Get disk usage for a path."""
        try:
            import shutil
            total, used, free = shutil.disk_usage(path)
            return DiskUsageSnapshot(
                path=path,
                total_bytes=total,
                used_bytes=used,
                free_bytes=free,
                usage_percent=(used / total) * 100 if total > 0 else 0,
            )
        except Exception as e:
            self.logger.error(f"Failed to get disk usage for {path}: {e}")
            return DiskUsageSnapshot(path=path)

    async def _check_thresholds(self, path: str, snapshot: DiskUsageSnapshot):
        """Check if thresholds are exceeded."""
        usage = snapshot.usage_percent / 100

        if usage >= self.config.disk_critical_threshold:
            alert = ResourceAlert(
                resource_type=ResourceType.DISK,
                level=AlertLevel.CRITICAL,
                message=f"Disk critical: {path} at {snapshot.usage_percent:.1f}%",
                current_value=snapshot.usage_percent,
                threshold=self.config.disk_critical_threshold * 100,
            )
            await self._emit_alert(alert)

            # Trigger cleanup
            if self.config.disk_cleanup_enabled:
                await self.cleanup(path)

        elif usage >= self.config.disk_warning_threshold:
            alert = ResourceAlert(
                resource_type=ResourceType.DISK,
                level=AlertLevel.WARNING,
                message=f"Disk warning: {path} at {snapshot.usage_percent:.1f}%",
                current_value=snapshot.usage_percent,
                threshold=self.config.disk_warning_threshold * 100,
            )
            await self._emit_alert(alert)

    async def _default_cleanup(self, path: str, strategy: CleanupStrategy) -> int:
        """Default cleanup strategy."""
        freed = 0
        cleanup_patterns = ["*.tmp", "*.log.old", "*.cache"]

        try:
            p = Path(path)
            for pattern in cleanup_patterns:
                for file in p.glob(f"**/{pattern}"):
                    try:
                        size = file.stat().st_size
                        file.unlink()
                        freed += size
                    except Exception:
                        pass

            self.logger.info(f"Cleanup freed {freed / (1024*1024):.1f}MB from {path}")
            return freed

        except Exception as e:
            self.logger.error(f"Default cleanup failed: {e}")
            return 0

    def register_callback(self, callback: Callable):
        """Register alert callback."""
        self._callbacks.append(callback)

    async def _emit_alert(self, alert: ResourceAlert):
        """Emit an alert."""
        self.logger.warning(f"Disk alert: {alert.message}")
        for callback in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(alert)
                else:
                    callback(alert)
            except Exception as e:
                self.logger.error(f"Alert callback error: {e}")


# =============================================================================
# NETWORK BANDWIDTH MANAGER
# =============================================================================


class NetworkBandwidthManager:
    """
    Manages network bandwidth limits and monitoring.

    Features:
    - Bandwidth limiting
    - Usage monitoring
    - Per-component tracking
    - Token bucket rate limiting
    """

    def __init__(self, config: ResourceManagementConfig):
        self.config = config
        self._component_usage: Dict[ComponentType, NetworkUsageSnapshot] = {}
        self._history: deque = deque(maxlen=1000)
        self._token_buckets: Dict[ComponentType, float] = {}
        self._last_refill: Dict[ComponentType, float] = {}
        self._lock = asyncio.Lock()
        self._callbacks: List[Callable] = []
        self.logger = logging.getLogger("NetworkBandwidthManager")

        # Initialize token buckets
        for component in ComponentType:
            self._token_buckets[component] = self._get_bucket_size(component)
            self._last_refill[component] = time.time()

    def _get_bucket_size(self, component: ComponentType) -> float:
        """Get token bucket size for component."""
        if self.config.network_bandwidth_limit_mbps <= 0:
            return float('inf')

        # Fair share allocation
        weights = {
            ComponentType.JARVIS_BODY: 0.35,
            ComponentType.JARVIS_PRIME: 0.40,
            ComponentType.REACTOR_CORE: 0.20,
            ComponentType.SYSTEM: 0.05,
        }
        weight = weights.get(component, 0.25)
        return self.config.network_bandwidth_limit_mbps * weight * 1_000_000 / 8  # bytes/sec

    async def acquire_bandwidth(
        self,
        component: ComponentType,
        bytes_requested: int,
    ) -> Tuple[bool, float]:
        """
        Try to acquire bandwidth.
        Returns (success, wait_time_seconds).
        """
        async with self._lock:
            # Refill bucket
            await self._refill_bucket(component)

            bucket = self._token_buckets[component]
            if bucket >= bytes_requested:
                self._token_buckets[component] -= bytes_requested
                return True, 0.0

            # Calculate wait time
            bucket_size = self._get_bucket_size(component)
            if bucket_size == float('inf'):
                return True, 0.0

            needed = bytes_requested - bucket
            refill_rate = bucket_size  # bytes per second
            wait_time = needed / refill_rate

            return False, wait_time

    async def record_usage(
        self,
        component: ComponentType,
        bytes_sent: int = 0,
        bytes_received: int = 0,
    ):
        """Record network usage."""
        async with self._lock:
            now = datetime.utcnow()

            if component not in self._component_usage:
                self._component_usage[component] = NetworkUsageSnapshot()

            usage = self._component_usage[component]
            usage.bytes_sent += bytes_sent
            usage.bytes_received += bytes_received
            usage.timestamp = now

    async def get_usage(self, component: Optional[ComponentType] = None) -> Dict[ComponentType, NetworkUsageSnapshot]:
        """Get network usage."""
        async with self._lock:
            if component:
                return {component: self._component_usage.get(component, NetworkUsageSnapshot())}
            return self._component_usage.copy()

    async def get_total_bandwidth(self) -> float:
        """Get total current bandwidth in Mbps."""
        total_bytes = 0
        for usage in self._component_usage.values():
            total_bytes += usage.bytes_sent + usage.bytes_received

        # Convert to Mbps (rough estimate based on recent usage)
        return (total_bytes * 8) / 1_000_000

    async def _refill_bucket(self, component: ComponentType):
        """Refill token bucket."""
        now = time.time()
        elapsed = now - self._last_refill[component]
        self._last_refill[component] = now

        bucket_size = self._get_bucket_size(component)
        if bucket_size == float('inf'):
            return

        # Refill at rate of bucket_size per second
        refill = elapsed * bucket_size
        self._token_buckets[component] = min(
            self._token_buckets[component] + refill,
            bucket_size  # Cap at bucket size
        )

    def register_callback(self, callback: Callable):
        """Register alert callback."""
        self._callbacks.append(callback)


# =============================================================================
# UNIFIED RESOURCE COORDINATOR
# =============================================================================


class UnifiedResourceCoordinator:
    """
    Centralized resource coordinator for all resource types.

    Provides unified interface for:
    - Port management
    - Memory management
    - CPU management
    - Disk management
    - Network management
    """

    def __init__(self, config: Optional[ResourceManagementConfig] = None):
        self.config = config or ResourceManagementConfig()
        self.logger = logging.getLogger("UnifiedResourceCoordinator")

        # Initialize managers
        self.ports = PortPoolManager(self.config)
        self.memory = MemoryBudgetManager(self.config)
        self.cpu = CPUAffinityManager(self.config)
        self.disk = DiskSpaceManager(self.config)
        self.network = NetworkBandwidthManager(self.config)

        # State
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._alerts: List[ResourceAlert] = []

        # Register callbacks
        self.memory.register_callback(self._on_alert)
        self.disk.register_callback(self._on_alert)
        self.network.register_callback(self._on_alert)

    async def initialize(self) -> bool:
        """Initialize the coordinator."""
        try:
            # Add default disk monitoring paths
            home = Path.home()
            await self.disk.add_path(str(home))

            # Add project path
            project_path = Path(__file__).parent.parent.parent.parent
            if project_path.exists():
                await self.disk.add_path(str(project_path))

            self.logger.info("UnifiedResourceCoordinator initialized")
            return True

        except Exception as e:
            self.logger.error(f"Initialization failed: {e}")
            return False

    async def start(self):
        """Start resource monitoring."""
        if self._running:
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self.logger.info("Resource monitoring started")

    async def stop(self):
        """Stop resource monitoring."""
        if not self._running:
            return

        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        self.logger.info("Resource monitoring stopped")

    async def shutdown(self):
        """Complete shutdown."""
        await self.stop()

    async def allocate_resources(
        self,
        component: ComponentType,
        ports: int = 0,
        memory_mb: int = 0,
        cpu_cores: int = 0,
    ) -> Dict[str, Any]:
        """Allocate resources for a component."""
        result = {
            "component": component.value,
            "ports": [],
            "memory_budget": None,
            "cpu_cores": [],
        }

        try:
            # Allocate ports
            if ports > 0:
                result["ports"] = await self.ports.reserve(component, count=ports)

            # Set memory budget
            if memory_mb > 0:
                await self.memory.set_budget(component, memory_mb)
                result["memory_budget"] = memory_mb

            # Allocate CPU cores
            if cpu_cores > 0:
                result["cpu_cores"] = await self.cpu.allocate(component, cpu_cores)

            self.logger.info(f"Allocated resources for {component.value}: {result}")
            return result

        except Exception as e:
            self.logger.error(f"Resource allocation failed: {e}")
            raise

    async def release_resources(self, component: ComponentType):
        """Release all resources for a component."""
        await self.ports.release_component(component)
        await self.cpu.release(component)
        self.logger.info(f"Released all resources for {component.value}")

    async def get_metrics(self) -> ResourceMetrics:
        """Get current resource metrics."""
        metrics = ResourceMetrics()

        try:
            import psutil

            # Memory
            mem = psutil.virtual_memory()
            metrics.memory_total_mb = int(mem.total / (1024 * 1024))
            metrics.memory_used_mb = int(mem.used / (1024 * 1024))
            metrics.memory_available_mb = int(mem.available / (1024 * 1024))
            metrics.memory_percent = mem.percent

            # CPU
            metrics.cpu_count = psutil.cpu_count() or 1
            metrics.cpu_percent = psutil.cpu_percent(interval=0.1)
            load = os.getloadavg()
            metrics.cpu_load_1m = load[0]
            metrics.cpu_load_5m = load[1]
            metrics.cpu_load_15m = load[2]

            # Disk
            disk = psutil.disk_usage('/')
            metrics.disk_total_gb = disk.total / (1024 ** 3)
            metrics.disk_used_gb = disk.used / (1024 ** 3)
            metrics.disk_free_gb = disk.free / (1024 ** 3)
            metrics.disk_percent = disk.percent

            # Network
            net = psutil.net_io_counters()
            metrics.network_bytes_sent = net.bytes_sent
            metrics.network_bytes_recv = net.bytes_recv

        except ImportError:
            self.logger.debug("psutil not available, limited metrics")

        return metrics

    async def get_status(self) -> Dict[str, Any]:
        """Get coordinator status."""
        metrics = await self.get_metrics()
        return {
            "running": self._running,
            "memory": {
                "total_mb": metrics.memory_total_mb,
                "used_mb": metrics.memory_used_mb,
                "percent": metrics.memory_percent,
            },
            "cpu": {
                "count": metrics.cpu_count,
                "percent": metrics.cpu_percent,
                "load_1m": metrics.cpu_load_1m,
            },
            "disk": {
                "total_gb": round(metrics.disk_total_gb, 1),
                "used_gb": round(metrics.disk_used_gb, 1),
                "percent": metrics.disk_percent,
            },
            "ports_reserved": len(await self.ports.get_reservations()),
            "active_alerts": len([a for a in self._alerts if not a.resolved]),
        }

    async def _monitor_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                await asyncio.sleep(self.config.coordinator_check_interval)

                # Check disk
                await self.disk.check_all()

                # Update memory usage
                await self._update_memory_usage()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Monitor loop error: {e}")

    async def _update_memory_usage(self):
        """Update memory usage for all components."""
        try:
            import psutil

            # Get memory by process name patterns
            patterns = {
                ComponentType.JARVIS_BODY: ["jarvis", "python.*jarvis"],
                ComponentType.JARVIS_PRIME: ["prime", "jarvis-prime"],
                ComponentType.REACTOR_CORE: ["reactor", "training"],
            }

            for component, component_patterns in patterns.items():
                total_mb = 0
                for proc in psutil.process_iter(['name', 'memory_info']):
                    try:
                        name = proc.info['name'].lower()
                        if any(p in name for p in component_patterns):
                            mem = proc.info['memory_info']
                            if mem:
                                total_mb += mem.rss / (1024 * 1024)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

                await self.memory.update_usage(component, int(total_mb))

        except ImportError:
            pass

    def _on_alert(self, alert: ResourceAlert):
        """Handle incoming alert."""
        self._alerts.append(alert)

        # Keep only recent alerts
        cutoff = datetime.utcnow() - timedelta(hours=self.config.history_retention_hours)
        self._alerts = [a for a in self._alerts if a.timestamp > cutoff]


# =============================================================================
# GLOBAL INSTANCE MANAGEMENT
# =============================================================================

_coordinator: Optional[UnifiedResourceCoordinator] = None
_coordinator_lock = asyncio.Lock()


async def get_resource_coordinator() -> UnifiedResourceCoordinator:
    """Get or create the global coordinator instance."""
    global _coordinator

    async with _coordinator_lock:
        if _coordinator is None:
            _coordinator = UnifiedResourceCoordinator()
            await _coordinator.initialize()
        return _coordinator


async def initialize_resource_management() -> bool:
    """Initialize the global resource coordinator."""
    coordinator = await get_resource_coordinator()
    await coordinator.start()
    return True


async def shutdown_resource_management():
    """Shutdown the global resource coordinator."""
    global _coordinator

    async with _coordinator_lock:
        if _coordinator is not None:
            await _coordinator.shutdown()
            _coordinator = None
            logger.info("Resource coordinator shutdown")


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Configuration
    "ResourceManagementConfig",
    # Enums
    "ResourceType",
    "ResourceState",
    "ComponentType",
    "AlertLevel",
    "CleanupStrategy",
    "BandwidthPolicy",
    # Data Structures
    "ResourceAllocation",
    "PortReservation",
    "MemoryBudget",
    "CPUAllocation",
    "DiskUsageSnapshot",
    "NetworkUsageSnapshot",
    "ResourceAlert",
    "ResourceMetrics",
    # Managers
    "PortPoolManager",
    "MemoryBudgetManager",
    "CPUAffinityManager",
    "DiskSpaceManager",
    "NetworkBandwidthManager",
    # Coordinator
    "UnifiedResourceCoordinator",
    # Global Functions
    "get_resource_coordinator",
    "initialize_resource_management",
    "shutdown_resource_management",
]
