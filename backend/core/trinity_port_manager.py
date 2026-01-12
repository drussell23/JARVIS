"""
Trinity Port Manager - Cross-Component Port Allocation with Fallback.
=====================================================================

Manages port allocation for ALL Trinity components (JARVIS Body, J-Prime, Reactor-Core)
with intelligent fallback, conflict detection, and IPC-based coordination.

Key Features:
1. Environment-driven port configuration (zero hardcoding)
2. Cross-component conflict detection
3. Fallback allocation with socket verification
4. IPC integration for port announcements
5. Hot reload support for port re-allocation

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  TrinityPortManager                                                 │
    │  ├── PortAllocation (per-component port config with fallbacks)      │
    │  ├── PortReservation (tracks allocated ports across components)     │
    │  ├── ConflictDetector (prevents port collision between Trinity)     │
    │  └── IPCPortAnnouncer (broadcasts port assignments via IPC)         │
    └─────────────────────────────────────────────────────────────────────┘

Author: JARVIS Trinity v81.0 - Cross-Component Port Coordination
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


# =============================================================================
# Component Types
# =============================================================================

class ComponentType(enum.Enum):
    """Trinity component types."""
    JARVIS_BODY = "jarvis_body"
    JARVIS_PRIME = "jarvis_prime"
    REACTOR_CORE = "reactor_core"


# =============================================================================
# Configuration (Environment-Driven)
# =============================================================================

@dataclass(frozen=True)
class PortAllocation:
    """Port allocation configuration for a component."""
    primary: int
    fallbacks: Tuple[int, ...]
    name: str
    env_primary: str
    env_fallbacks: str

    @classmethod
    def from_env(
        cls,
        name: str,
        env_primary: str,
        default_primary: int,
        env_fallbacks: str,
        default_fallbacks: Tuple[int, ...],
    ) -> "PortAllocation":
        """Create PortAllocation from environment variables."""
        # Get primary port
        primary_str = os.environ.get(env_primary, "")
        try:
            primary = int(primary_str) if primary_str else default_primary
        except ValueError:
            logger.warning(
                f"Invalid {env_primary}={primary_str}, using default {default_primary}"
            )
            primary = default_primary

        # Get fallback ports
        fallbacks_str = os.environ.get(env_fallbacks, "")
        if fallbacks_str:
            try:
                fallbacks = tuple(int(p.strip()) for p in fallbacks_str.split(",") if p.strip())
            except ValueError:
                logger.warning(
                    f"Invalid {env_fallbacks}={fallbacks_str}, using defaults"
                )
                fallbacks = default_fallbacks
        else:
            fallbacks = default_fallbacks

        return cls(
            primary=primary,
            fallbacks=fallbacks,
            name=name,
            env_primary=env_primary,
            env_fallbacks=env_fallbacks,
        )

    @property
    def all_ports(self) -> Tuple[int, ...]:
        """Get all possible ports (primary + fallbacks)."""
        return (self.primary,) + self.fallbacks


@dataclass
class TrinityPortConfig:
    """Configuration for all Trinity component ports."""

    # Port configurations per component (loaded from environment)
    allocations: Dict[ComponentType, PortAllocation] = field(default_factory=dict)

    # IPC configuration
    trinity_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("TRINITY_DIR", str(Path.home() / ".jarvis" / "trinity"))
    ))

    # Timing configuration
    port_check_timeout: float = field(default_factory=lambda: float(
        os.environ.get("TRINITY_PORT_CHECK_TIMEOUT", "2.0")
    ))
    socket_bind_timeout: float = field(default_factory=lambda: float(
        os.environ.get("TRINITY_SOCKET_BIND_TIMEOUT", "1.0")
    ))

    def __post_init__(self) -> None:
        """Load port configurations from environment."""
        if not self.allocations:
            self.allocations = {
                ComponentType.JARVIS_BODY: PortAllocation.from_env(
                    name="JARVIS Body",
                    env_primary="JARVIS_PORT",
                    default_primary=8010,
                    env_fallbacks="JARVIS_PORT_FALLBACK",
                    default_fallbacks=(8011, 8012, 8013),
                ),
                ComponentType.JARVIS_PRIME: PortAllocation.from_env(
                    name="JARVIS Prime",
                    env_primary="JARVIS_PRIME_PORT",
                    default_primary=8002,
                    env_fallbacks="JARVIS_PRIME_PORT_FALLBACK",
                    default_fallbacks=(8004, 8005, 8006),
                ),
                ComponentType.REACTOR_CORE: PortAllocation.from_env(
                    name="Reactor Core",
                    env_primary="REACTOR_CORE_PORT",
                    default_primary=8003,
                    env_fallbacks="REACTOR_CORE_PORT_FALLBACK",
                    default_fallbacks=(8007, 8008, 8009),
                ),
            }


# =============================================================================
# Port Reservation Tracking
# =============================================================================

@dataclass
class PortReservation:
    """A reserved port for a component."""
    component: ComponentType
    port: int
    is_primary: bool
    reserved_at: float = field(default_factory=time.time)
    pid: Optional[int] = None
    verified: bool = False

    @property
    def age_seconds(self) -> float:
        """How long this reservation has existed."""
        return time.time() - self.reserved_at


@dataclass
class AllocationResult:
    """Result of a port allocation attempt."""
    success: bool
    component: ComponentType
    port: int
    is_primary: bool
    elapsed_ms: float
    fallback_reason: Optional[str] = None
    error: Optional[str] = None

    @property
    def used_fallback(self) -> bool:
        """Check if fallback was used."""
        return not self.is_primary and self.success


# =============================================================================
# Trinity Port Manager
# =============================================================================

class TrinityPortManager:
    """
    Cross-component port allocation manager for Trinity architecture.

    Coordinates port allocation across JARVIS Body, Prime, and Reactor Core
    to prevent conflicts and enable graceful fallback.

    Features:
    - Environment-driven configuration (zero hardcoding)
    - Cross-component conflict detection
    - Parallel port checking for speed
    - IPC-based port announcements
    - Hot reload support
    """

    def __init__(
        self,
        config: Optional[TrinityPortConfig] = None,
        host: str = "127.0.0.1",
        ipc_bus: Optional[Any] = None,  # TrinityIPCBus from trinity_ipc.py
    ):
        """
        Initialize the port manager.

        Args:
            config: Port configuration (loads from env if not provided)
            host: Host to bind ports on
            ipc_bus: Optional IPC bus for port announcements
        """
        self.config = config or TrinityPortConfig()
        self.host = host
        self.ipc_bus = ipc_bus

        # Reservation tracking
        self._reservations: Dict[ComponentType, PortReservation] = {}
        self._reserved_ports: Set[int] = set()
        self._lock = asyncio.Lock()

        # Callbacks for port changes
        self._port_change_callbacks: List[Callable[[ComponentType, int, int], None]] = []

        # Ensure IPC directory exists
        self.config.trinity_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"[TrinityPortManager] Initialized with config: "
            f"Body={self.config.allocations[ComponentType.JARVIS_BODY].primary}, "
            f"Prime={self.config.allocations[ComponentType.JARVIS_PRIME].primary}, "
            f"Reactor={self.config.allocations[ComponentType.REACTOR_CORE].primary}"
        )

    # =========================================================================
    # Main API
    # =========================================================================

    async def allocate_port(
        self,
        component: ComponentType,
        pid: Optional[int] = None,
    ) -> AllocationResult:
        """
        Allocate a port for a component.

        Tries primary port first, then fallbacks if primary is in use.
        Prevents allocation of ports reserved by other Trinity components.

        Args:
            component: The component to allocate a port for
            pid: Optional PID of the process that will use this port

        Returns:
            AllocationResult with success status and allocated port
        """
        async with self._lock:
            start_time = time.perf_counter()
            allocation = self.config.allocations[component]

            logger.info(
                f"[TrinityPortManager] Allocating port for {component.value} "
                f"(primary={allocation.primary}, fallbacks={allocation.fallbacks})"
            )

            # Check for existing reservation
            existing = self._reservations.get(component)
            if existing and existing.verified:
                # Verify still valid
                if await self._verify_port_usable(existing.port, component):
                    elapsed = (time.perf_counter() - start_time) * 1000
                    logger.info(
                        f"[TrinityPortManager] ✅ Reusing existing reservation: "
                        f"{component.value} → port {existing.port}"
                    )
                    return AllocationResult(
                        success=True,
                        component=component,
                        port=existing.port,
                        is_primary=existing.is_primary,
                        elapsed_ms=elapsed,
                    )
                else:
                    # Reservation is stale, remove it
                    self._release_reservation(component)

            # Try primary port first
            primary_result = await self._try_allocate_port(
                component, allocation.primary, is_primary=True, pid=pid
            )

            if primary_result.success:
                elapsed = (time.perf_counter() - start_time) * 1000
                primary_result = AllocationResult(
                    success=True,
                    component=component,
                    port=allocation.primary,
                    is_primary=True,
                    elapsed_ms=elapsed,
                )
                await self._announce_port_allocation(component, allocation.primary)
                return primary_result

            # Try fallback ports
            fallback_reason = primary_result.error or "primary port unavailable"

            for fallback_port in allocation.fallbacks:
                fallback_result = await self._try_allocate_port(
                    component, fallback_port, is_primary=False, pid=pid
                )

                if fallback_result.success:
                    elapsed = (time.perf_counter() - start_time) * 1000
                    logger.info(
                        f"[TrinityPortManager] ✅ Using fallback port: "
                        f"{component.value} → port {fallback_port}"
                    )
                    await self._announce_port_allocation(component, fallback_port)
                    return AllocationResult(
                        success=True,
                        component=component,
                        port=fallback_port,
                        is_primary=False,
                        elapsed_ms=elapsed,
                        fallback_reason=fallback_reason,
                    )

            # All ports failed
            elapsed = (time.perf_counter() - start_time) * 1000
            error_msg = (
                f"No available ports for {component.value}. "
                f"Tried: {allocation.primary} and fallbacks {allocation.fallbacks}"
            )
            logger.error(f"[TrinityPortManager] ❌ {error_msg}")

            return AllocationResult(
                success=False,
                component=component,
                port=0,
                is_primary=False,
                elapsed_ms=elapsed,
                error=error_msg,
            )

    async def allocate_all_ports(
        self,
        order: Optional[List[ComponentType]] = None,
    ) -> Dict[ComponentType, AllocationResult]:
        """
        Allocate ports for all Trinity components in order.

        Args:
            order: Order to allocate ports (default: Body, Prime, Reactor)

        Returns:
            Dict mapping each component to its allocation result
        """
        if order is None:
            order = [
                ComponentType.JARVIS_BODY,
                ComponentType.JARVIS_PRIME,
                ComponentType.REACTOR_CORE,
            ]

        results: Dict[ComponentType, AllocationResult] = {}

        for component in order:
            result = await self.allocate_port(component)
            results[component] = result

            if not result.success:
                logger.warning(
                    f"[TrinityPortManager] Failed to allocate port for {component.value}, "
                    f"continuing with remaining components..."
                )

        return results

    def get_port(self, component: ComponentType) -> Optional[int]:
        """Get the currently allocated port for a component."""
        reservation = self._reservations.get(component)
        return reservation.port if reservation else None

    def get_all_ports(self) -> Dict[ComponentType, Optional[int]]:
        """Get all currently allocated ports."""
        return {
            component: self.get_port(component)
            for component in ComponentType
        }

    async def release_port(self, component: ComponentType) -> None:
        """Release a port reservation for a component."""
        async with self._lock:
            self._release_reservation(component)
            logger.info(f"[TrinityPortManager] Released port for {component.value}")

    async def release_all_ports(self) -> None:
        """Release all port reservations."""
        async with self._lock:
            components = list(self._reservations.keys())
            for component in components:
                self._release_reservation(component)
            logger.info("[TrinityPortManager] Released all port reservations")

    # =========================================================================
    # Port Checking
    # =========================================================================

    async def _try_allocate_port(
        self,
        component: ComponentType,
        port: int,
        is_primary: bool,
        pid: Optional[int] = None,
    ) -> AllocationResult:
        """Try to allocate a specific port for a component."""
        start_time = time.perf_counter()

        # Check if port is reserved by another Trinity component
        if port in self._reserved_ports:
            for other_component, reservation in self._reservations.items():
                if reservation.port == port and other_component != component:
                    elapsed = (time.perf_counter() - start_time) * 1000
                    return AllocationResult(
                        success=False,
                        component=component,
                        port=port,
                        is_primary=is_primary,
                        elapsed_ms=elapsed,
                        error=f"Port {port} reserved by {other_component.value}",
                    )

        # Check if port is usable
        if not await self._verify_port_usable(port, component):
            elapsed = (time.perf_counter() - start_time) * 1000
            return AllocationResult(
                success=False,
                component=component,
                port=port,
                is_primary=is_primary,
                elapsed_ms=elapsed,
                error=f"Port {port} is in use or not bindable",
            )

        # Reserve the port
        reservation = PortReservation(
            component=component,
            port=port,
            is_primary=is_primary,
            pid=pid,
            verified=True,
        )
        self._reservations[component] = reservation
        self._reserved_ports.add(port)

        elapsed = (time.perf_counter() - start_time) * 1000
        return AllocationResult(
            success=True,
            component=component,
            port=port,
            is_primary=is_primary,
            elapsed_ms=elapsed,
        )

    async def _verify_port_usable(
        self,
        port: int,
        component: ComponentType,
    ) -> bool:
        """
        Verify a port is usable (not in use by external process).

        Performs both lsof check and socket bind test.
        """
        # Check if port is in use via lsof
        pid = await self._get_pid_on_port(port)
        if pid is not None:
            # Port is in use - check if it's our own process
            our_reservation = self._reservations.get(component)
            if our_reservation and our_reservation.pid == pid:
                return True  # Our own process

            logger.debug(
                f"[TrinityPortManager] Port {port} in use by PID {pid} "
                f"(not ours for {component.value})"
            )
            return False

        # Verify with socket bind
        return await self._verify_socket_bindable(port)

    async def _get_pid_on_port(self, port: int) -> Optional[int]:
        """Get PID of process using a port."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-t", "-i", f":{port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.port_check_timeout,
            )

            if stdout:
                pids = stdout.decode().strip().split("\n")
                if pids and pids[0]:
                    return int(pids[0])

            return None

        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            return None
        except Exception as e:
            logger.debug(f"[TrinityPortManager] lsof error: {e}")
            return None

    async def _verify_socket_bindable(self, port: int) -> bool:
        """Verify port is bindable by attempting socket bind."""
        try:
            # Run in executor to avoid blocking
            loop = asyncio.get_event_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._sync_socket_bind_test, port),
                timeout=self.config.socket_bind_timeout,
            )
        except asyncio.TimeoutError:
            logger.debug(f"[TrinityPortManager] Socket bind test timed out for port {port}")
            return False
        except Exception as e:
            logger.debug(f"[TrinityPortManager] Socket bind test error: {e}")
            return False

    def _sync_socket_bind_test(self, port: int) -> bool:
        """Synchronous socket bind test."""
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(self.config.socket_bind_timeout)
            sock.bind((self.host, port))
            return True
        except OSError:
            return False
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    # =========================================================================
    # Reservation Management
    # =========================================================================

    def _release_reservation(self, component: ComponentType) -> None:
        """Release a reservation (internal, assumes lock is held)."""
        reservation = self._reservations.pop(component, None)
        if reservation:
            self._reserved_ports.discard(reservation.port)
            logger.debug(
                f"[TrinityPortManager] Released reservation: "
                f"{component.value} → port {reservation.port}"
            )

    # =========================================================================
    # IPC Integration
    # =========================================================================

    async def _announce_port_allocation(
        self,
        component: ComponentType,
        port: int,
    ) -> None:
        """Announce port allocation via IPC and environment."""
        # Update environment variable for child processes
        allocation = self.config.allocations[component]
        os.environ[allocation.env_primary] = str(port)

        # Write to IPC file for cross-process visibility
        port_file = self.config.trinity_dir / "ports" / f"{component.value}.port"
        port_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            port_file.write_text(str(port))
            logger.debug(
                f"[TrinityPortManager] Announced port allocation: "
                f"{component.value} → {port} (env={allocation.env_primary})"
            )
        except Exception as e:
            logger.warning(f"[TrinityPortManager] Failed to write port file: {e}")

        # Notify via IPC bus if available
        if self.ipc_bus:
            try:
                await self.ipc_bus.publish_heartbeat(
                    component=component.value,
                    status="port_allocated",
                    pid=os.getpid(),
                    metrics={"allocated_port": port},
                )
            except Exception as e:
                logger.debug(f"[TrinityPortManager] IPC announcement failed: {e}")

        # Call registered callbacks
        for callback in self._port_change_callbacks:
            try:
                callback(component, 0, port)
            except Exception as e:
                logger.debug(f"[TrinityPortManager] Callback error: {e}")

    def read_component_port(self, component: ComponentType) -> Optional[int]:
        """Read a component's allocated port from IPC file."""
        port_file = self.config.trinity_dir / "ports" / f"{component.value}.port"

        try:
            if port_file.exists():
                return int(port_file.read_text().strip())
        except (ValueError, IOError) as e:
            logger.debug(f"[TrinityPortManager] Failed to read port file: {e}")

        return None

    # =========================================================================
    # Callbacks
    # =========================================================================

    def on_port_change(
        self,
        callback: Callable[[ComponentType, int, int], None],
    ) -> None:
        """
        Register a callback for port changes.

        Callback receives: (component, old_port, new_port)
        """
        self._port_change_callbacks.append(callback)

    # =========================================================================
    # Health Checks
    # =========================================================================

    async def verify_all_reservations(self) -> Dict[ComponentType, bool]:
        """Verify all current reservations are still valid."""
        results: Dict[ComponentType, bool] = {}

        for component, reservation in list(self._reservations.items()):
            is_valid = await self._verify_port_usable(reservation.port, component)
            results[component] = is_valid

            if not is_valid:
                logger.warning(
                    f"[TrinityPortManager] Reservation for {component.value} "
                    f"on port {reservation.port} is no longer valid"
                )
                reservation.verified = False

        return results

    async def get_status(self) -> Dict[str, Any]:
        """Get current status of port manager."""
        return {
            "reservations": {
                component.value: {
                    "port": reservation.port,
                    "is_primary": reservation.is_primary,
                    "age_seconds": reservation.age_seconds,
                    "pid": reservation.pid,
                    "verified": reservation.verified,
                }
                for component, reservation in self._reservations.items()
            },
            "reserved_ports": list(self._reserved_ports),
            "config": {
                component.value: {
                    "primary": alloc.primary,
                    "fallbacks": list(alloc.fallbacks),
                }
                for component, alloc in self.config.allocations.items()
            },
        }


# =============================================================================
# Singleton Access
# =============================================================================

_manager_instance: Optional[TrinityPortManager] = None
_manager_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_trinity_port_manager(
    config: Optional[TrinityPortConfig] = None,
    **kwargs,
) -> TrinityPortManager:
    """Get or create the global Trinity port manager."""
    global _manager_instance

    async with _manager_lock:
        if _manager_instance is None:
            _manager_instance = TrinityPortManager(config=config, **kwargs)
        return _manager_instance


def get_trinity_port_manager_sync(
    config: Optional[TrinityPortConfig] = None,
    **kwargs,
) -> TrinityPortManager:
    """Synchronous version for non-async contexts."""
    global _manager_instance

    if _manager_instance is None:
        _manager_instance = TrinityPortManager(config=config, **kwargs)
    return _manager_instance


async def allocate_trinity_ports(
    order: Optional[List[ComponentType]] = None,
) -> Dict[ComponentType, AllocationResult]:
    """Convenience function to allocate all Trinity ports."""
    manager = await get_trinity_port_manager()
    return await manager.allocate_all_ports(order)


# =============================================================================
# Integration with IntelligentPortManager
# =============================================================================

async def allocate_with_cleanup(
    component: ComponentType,
    **intelligent_port_manager_kwargs,
) -> Tuple[int, Optional[Any]]:
    """
    Allocate a port using IntelligentPortManager for cleanup.

    This integrates TrinityPortManager with the existing IntelligentPortManager
    to provide both cross-component coordination AND intelligent cleanup.

    Args:
        component: Component to allocate port for
        **intelligent_port_manager_kwargs: Passed to IntelligentPortManager

    Returns:
        Tuple of (allocated_port, adopted_process_info or None)
    """
    try:
        from backend.core.supervisor.intelligent_port_manager import (
            IntelligentPortManager,
        )
    except ImportError:
        logger.warning(
            "[TrinityPortManager] IntelligentPortManager not available, "
            "using simple allocation"
        )
        manager = await get_trinity_port_manager()
        result = await manager.allocate_port(component)
        return (result.port if result.success else 0, None)

    # Get Trinity port manager for coordination
    trinity_manager = await get_trinity_port_manager()
    allocation = trinity_manager.config.allocations[component]

    # Create component-specific IntelligentPortManager
    intelligent_manager = IntelligentPortManager(
        primary_port=allocation.primary,
        fallback_port_start=allocation.fallbacks[0] if allocation.fallbacks else allocation.primary + 1,
        fallback_port_end=(allocation.fallbacks[-1] if allocation.fallbacks else allocation.primary + 10),
        **intelligent_port_manager_kwargs,
    )

    # Use intelligent manager for cleanup and allocation
    port, adopted_info = await intelligent_manager.ensure_port_available()

    # Register the allocation with Trinity manager
    result = await trinity_manager.allocate_port(component, pid=os.getpid())

    if result.success:
        return (result.port, adopted_info)
    else:
        # Fallback to intelligent manager's port
        return (port, adopted_info)
