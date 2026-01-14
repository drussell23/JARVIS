"""
Enterprise-Grade Service Registry v3.0
======================================

Dynamic service discovery system eliminating hardcoded ports and enabling
true distributed orchestration across JARVIS, J-Prime, and Reactor-Core.

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │                   Service Registry v3.0                          │
    │  ┌────────────────────────────────────────────────────────────┐  │
    │  │  File-Based Registry: ~/.jarvis/registry/services.json     │  │
    │  │  ┌──────────────┬──────────────┬─────────────────────┐     │  │
    │  │  │   JARVIS     │   J-PRIME    │   REACTOR-CORE      │     │  │
    │  │  │  PID: 12345  │  PID: 12346  │   PID: 12347        │     │  │
    │  │  │  Port: 5001  │  Port: 8002  │   Port: 8003        │     │  │
    │  │  │  Status: ✅  │  Status: ✅  │   Status: ✅         │     │  │
    │  │  └──────────────┴──────────────┴─────────────────────┘     │  │
    │  └────────────────────────────────────────────────────────────┘  │
    │                                                                  │
    │  Features:                                                       │
    │  • Atomic file operations with fcntl locking                     │
    │  • Automatic stale service cleanup (dead PIDs)                   │
    │  • Health heartbeat tracking                                     │
    │  • Zero hardcoded ports or URLs                                  │
    │  • Cross-process safe concurrent access                          │
    └──────────────────────────────────────────────────────────────────┘

Usage:
    # Register service on startup
    registry = ServiceRegistry()
    await registry.register_service(
        service_name="jarvis-prime",
        pid=os.getpid(),
        port=8002,
        health_endpoint="/health"
    )

    # Discover services dynamically
    jprime = await registry.discover_service("jarvis-prime")
    if jprime:
        url = f"http://{jprime.host}:{jprime.port}{jprime.health_endpoint}"

    # Heartbeat to keep alive
    await registry.heartbeat("jarvis-prime")

    # Clean deregistration on shutdown
    await registry.deregister_service("jarvis-prime")

Author: JARVIS AI System
Version: 3.0.0
"""

import asyncio
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set
from datetime import datetime, timedelta
import psutil

logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class ServiceInfo:
    """Information about a registered service."""
    service_name: str
    pid: int
    port: int
    host: str = "localhost"
    health_endpoint: str = "/health"
    status: str = "starting"  # starting, healthy, degraded, failed
    registered_at: float = 0.0
    last_heartbeat: float = 0.0
    metadata: Dict = None

    def __post_init__(self):
        if self.registered_at == 0.0:
            self.registered_at = time.time()
        if self.last_heartbeat == 0.0:
            self.last_heartbeat = time.time()
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "ServiceInfo":
        """Create from dictionary."""
        return cls(**data)

    def is_process_alive(self) -> bool:
        """Check if the service's process is still running."""
        try:
            process = psutil.Process(self.pid)
            return process.is_running()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def is_stale(self, timeout_seconds: float = 60.0) -> bool:
        """Check if service hasn't sent heartbeat in timeout period."""
        return (time.time() - self.last_heartbeat) > timeout_seconds


# =============================================================================
# Service Registry
# =============================================================================

class ServiceRegistry:
    """
    Enterprise-grade service registry with atomic operations.

    Features:
    - File-based persistence with fcntl locking
    - Automatic stale service cleanup
    - Health tracking with heartbeats
    - Zero hardcoded configuration
    """

    def __init__(
        self,
        registry_dir: Optional[Path] = None,
        heartbeat_timeout: float = 60.0,
        cleanup_interval: float = 30.0
    ):
        """
        Initialize service registry.

        Args:
            registry_dir: Directory for registry file (default: ~/.jarvis/registry)
            heartbeat_timeout: Seconds before service considered stale
            cleanup_interval: Seconds between cleanup cycles
        """
        self.registry_dir = registry_dir or Path.home() / ".jarvis" / "registry"
        self.registry_file = self.registry_dir / "services.json"
        self.heartbeat_timeout = heartbeat_timeout
        self.cleanup_interval = cleanup_interval
        self._cleanup_task: Optional[asyncio.Task] = None

        # Ensure directory exists
        self.registry_dir.mkdir(parents=True, exist_ok=True)

        # Initialize registry file if doesn't exist
        if not self.registry_file.exists():
            self._write_registry({})

    def _acquire_lock(self, file_handle) -> None:
        """Acquire exclusive lock on registry file (blocking)."""
        fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX)

    def _release_lock(self, file_handle) -> None:
        """Release lock on registry file."""
        fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)

    def _read_registry(self) -> Dict[str, ServiceInfo]:
        """
        Read registry with file locking (thread/process safe).

        Returns:
            Dict mapping service names to ServiceInfo
        """
        try:
            with open(self.registry_file, 'r+') as f:
                self._acquire_lock(f)
                try:
                    data = json.load(f)
                    return {
                        name: ServiceInfo.from_dict(info)
                        for name, info in data.items()
                    }
                finally:
                    self._release_lock(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_registry(self, services: Dict[str, ServiceInfo]) -> None:
        """
        Write registry with atomic file operations.

        Args:
            services: Dict mapping service names to ServiceInfo
        """
        # Convert to serializable dict
        data = {
            name: service.to_dict()
            for name, service in services.items()
        }

        # Atomic write with file locking
        temp_file = self.registry_file.with_suffix('.tmp')
        try:
            with open(temp_file, 'w') as f:
                self._acquire_lock(f)
                try:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())  # Force write to disk
                finally:
                    self._release_lock(f)

            # Atomic rename
            temp_file.replace(self.registry_file)

        except Exception as e:
            logger.error(f"Failed to write registry: {e}")
            if temp_file.exists():
                temp_file.unlink()
            raise

    async def register_service(
        self,
        service_name: str,
        pid: int,
        port: int,
        host: str = "localhost",
        health_endpoint: str = "/health",
        metadata: Optional[Dict] = None
    ) -> ServiceInfo:
        """
        Register a service in the registry.

        Args:
            service_name: Unique service identifier
            pid: Process ID
            port: Port number service is listening on
            host: Hostname (default: localhost)
            health_endpoint: Health check endpoint path
            metadata: Optional additional metadata

        Returns:
            ServiceInfo for the registered service
        """
        service = ServiceInfo(
            service_name=service_name,
            pid=pid,
            port=port,
            host=host,
            health_endpoint=health_endpoint,
            status="starting",
            metadata=metadata or {}
        )

        # Read existing registry
        services = await asyncio.to_thread(self._read_registry)

        # Add/update service
        services[service_name] = service

        # Write back atomically
        await asyncio.to_thread(self._write_registry, services)

        logger.info(
            f"📝 Service registered: {service_name} "
            f"(PID: {pid}, Port: {port}, Host: {host})"
        )

        return service

    async def deregister_service(self, service_name: str) -> bool:
        """
        Remove service from registry.

        Args:
            service_name: Service to deregister

        Returns:
            True if service was found and removed
        """
        services = await asyncio.to_thread(self._read_registry)

        if service_name in services:
            del services[service_name]
            await asyncio.to_thread(self._write_registry, services)
            logger.info(f"❌ Service deregistered: {service_name}")
            return True

        return False

    async def discover_service(self, service_name: str) -> Optional[ServiceInfo]:
        """
        Discover a service by name.

        Args:
            service_name: Service to find

        Returns:
            ServiceInfo if found and healthy, None otherwise
        """
        services = await asyncio.to_thread(self._read_registry)
        service = services.get(service_name)

        if not service:
            return None

        # Check if process is still alive
        if not service.is_process_alive():
            logger.warning(
                f"⚠️  Service {service_name} has dead PID {service.pid}, cleaning up"
            )
            await self.deregister_service(service_name)
            return None

        # Check if stale (no recent heartbeat)
        if service.is_stale(self.heartbeat_timeout):
            logger.warning(
                f"⚠️  Service {service_name} is stale "
                f"(last heartbeat {time.time() - service.last_heartbeat:.0f}s ago)"
            )
            return None

        return service

    async def list_services(self, healthy_only: bool = True) -> List[ServiceInfo]:
        """
        List all registered services.

        Args:
            healthy_only: If True, only return healthy services

        Returns:
            List of ServiceInfo objects
        """
        services = await asyncio.to_thread(self._read_registry)

        if not healthy_only:
            return list(services.values())

        # Filter to only healthy services
        healthy = []
        for service in services.values():
            if service.is_process_alive() and not service.is_stale(self.heartbeat_timeout):
                healthy.append(service)

        return healthy

    async def heartbeat(
        self,
        service_name: str,
        status: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> bool:
        """
        Update service heartbeat and optionally status/metadata.

        Args:
            service_name: Service to update
            status: Optional new status (healthy, degraded, etc.)
            metadata: Optional metadata to merge

        Returns:
            True if service found and updated
        """
        services = await asyncio.to_thread(self._read_registry)

        if service_name not in services:
            logger.warning(f"⚠️  Heartbeat for unregistered service: {service_name}")
            return False

        service = services[service_name]
        service.last_heartbeat = time.time()

        if status:
            service.status = status

        if metadata:
            service.metadata.update(metadata)

        await asyncio.to_thread(self._write_registry, services)

        return True

    async def cleanup_stale_services(self) -> int:
        """
        Remove services with dead PIDs or stale heartbeats.

        Returns:
            Number of services cleaned up
        """
        services = await asyncio.to_thread(self._read_registry)
        cleaned = 0

        for service_name, service in list(services.items()):
            should_remove = False

            # Check if process is dead
            if not service.is_process_alive():
                logger.info(
                    f"🧹 Cleaning dead service: {service_name} (PID {service.pid} not found)"
                )
                should_remove = True

            # Check if stale
            elif service.is_stale(self.heartbeat_timeout):
                logger.info(
                    f"🧹 Cleaning stale service: {service_name} "
                    f"(last heartbeat {time.time() - service.last_heartbeat:.0f}s ago)"
                )
                should_remove = True

            if should_remove:
                del services[service_name]
                cleaned += 1

        if cleaned > 0:
            await asyncio.to_thread(self._write_registry, services)

        return cleaned

    async def _cleanup_loop(self) -> None:
        """Background task to periodically clean up stale services."""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                cleaned = await self.cleanup_stale_services()
                if cleaned > 0:
                    logger.debug(f"🧹 Cleaned {cleaned} stale services")

            except asyncio.CancelledError:
                logger.info("🛑 Service registry cleanup loop stopped")
                break

            except Exception as e:
                logger.error(f"❌ Error in cleanup loop: {e}", exc_info=True)

    async def start_cleanup_task(self) -> None:
        """Start background cleanup task."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info(
                f"🧹 Service registry cleanup started "
                f"(interval: {self.cleanup_interval}s, timeout: {self.heartbeat_timeout}s)"
            )

    async def stop_cleanup_task(self) -> None:
        """Stop background cleanup task."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("🛑 Service registry cleanup stopped")

    async def wait_for_service(
        self,
        service_name: str,
        timeout: float = 30.0,
        check_interval: float = 1.0
    ) -> Optional[ServiceInfo]:
        """
        Wait for a service to become available.

        Args:
            service_name: Service to wait for
            timeout: Maximum time to wait (seconds)
            check_interval: How often to check (seconds)

        Returns:
            ServiceInfo if service becomes available, None if timeout
        """
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            service = await self.discover_service(service_name)
            if service:
                logger.info(f"✅ Service discovered: {service_name}")
                return service

            await asyncio.sleep(check_interval)

        logger.warning(
            f"⏱️  Timeout waiting for service: {service_name} (after {timeout}s)"
        )
        return None


# =============================================================================
# Convenience Functions
# =============================================================================

_global_registry: Optional[ServiceRegistry] = None


def get_service_registry() -> ServiceRegistry:
    """Get global service registry instance (singleton)."""
    global _global_registry
    if _global_registry is None:
        _global_registry = ServiceRegistry()
    return _global_registry


async def register_current_service(
    service_name: str,
    port: int,
    health_endpoint: str = "/health",
    metadata: Optional[Dict] = None
) -> ServiceInfo:
    """
    Convenience function to register current process as a service.

    Args:
        service_name: Name for this service
        port: Port this service listens on
        health_endpoint: Health check endpoint
        metadata: Optional metadata

    Returns:
        ServiceInfo for registered service
    """
    registry = get_service_registry()
    return await registry.register_service(
        service_name=service_name,
        pid=os.getpid(),
        port=port,
        health_endpoint=health_endpoint,
        metadata=metadata
    )


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    "ServiceRegistry",
    "ServiceInfo",
    "get_service_registry",
    "register_current_service",
]
