"""
JARVIS Service Registry v1.0.0
===============================

Dynamic service discovery and registration for the Trinity ecosystem.

Provides:
1. Service registration with metadata
2. Dynamic port discovery
3. Health endpoint tracking
4. Service dependency mapping
5. Heartbeat-based liveness detection

Architecture:
    The ServiceRegistry maintains a catalog of all Trinity services,
    their current status, and connection information. Services can:
    - Register themselves on startup
    - Be discovered via port scanning
    - Be monitored via heartbeat files

Service Discovery Methods:
    1. Explicit registration (service calls register())
    2. Port-based discovery (scan known ports)
    3. Heartbeat file discovery (~/.jarvis/heartbeats/)
    4. HTTP health endpoint probing

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================

class ServiceStatus(Enum):
    """Service status states."""
    UNKNOWN = "unknown"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"


class ServiceType(Enum):
    """Trinity service types."""
    JARVIS_BODY = "jarvis_body"
    JARVIS_PRIME = "jarvis_prime"
    REACTOR_CORE = "reactor_core"
    LOADING_SERVER = "loading_server"
    FRONTEND = "frontend"
    WEBSOCKET = "websocket"
    EXTERNAL = "external"


# =============================================================================
# SERVICE INFO
# =============================================================================

@dataclass
class ServiceInfo:
    """
    Information about a registered service.
    """
    name: str
    service_type: ServiceType
    host: str = "localhost"
    port: int = 0
    health_path: str = "/health"
    status: ServiceStatus = ServiceStatus.UNKNOWN
    
    # Process info
    pid: Optional[int] = None
    started_at: Optional[datetime] = None
    
    # Health tracking
    last_health_check: Optional[datetime] = None
    last_healthy: Optional[datetime] = None
    consecutive_failures: int = 0
    
    # Metadata
    version: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"
    
    @property
    def health_url(self) -> str:
        return f"{self.base_url}{self.health_path}"
    
    @property
    def is_healthy(self) -> bool:
        return self.status == ServiceStatus.HEALTHY
    
    @property
    def is_running(self) -> bool:
        return self.status in (ServiceStatus.STARTING, ServiceStatus.HEALTHY, ServiceStatus.DEGRADED)
    
    @property
    def uptime_seconds(self) -> float:
        if self.started_at:
            return (datetime.now() - self.started_at).total_seconds()
        return 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "type": self.service_type.value,
            "host": self.host,
            "port": self.port,
            "status": self.status.value,
            "pid": self.pid,
            "base_url": self.base_url,
            "health_url": self.health_url,
            "uptime_seconds": self.uptime_seconds,
            "consecutive_failures": self.consecutive_failures,
            "last_health_check": (
                self.last_health_check.isoformat() if self.last_health_check else None
            ),
            "version": self.version,
        }


# =============================================================================
# DEFAULT SERVICE CONFIGURATION
# =============================================================================

DEFAULT_SERVICES: Dict[str, Dict[str, Any]] = {
    "jarvis-body": {
        "type": ServiceType.JARVIS_BODY,
        "port": 8010,
        "health_path": "/health",
        "required": True,
    },
    "jarvis-prime": {
        "type": ServiceType.JARVIS_PRIME,
        "port": 8001,
        "health_path": "/health",
        "required": False,
    },
    "reactor-core": {
        "type": ServiceType.REACTOR_CORE,
        "port": 8090,
        "health_path": "/health",
        "required": False,
    },
    "loading-server": {
        "type": ServiceType.LOADING_SERVER,
        "port": 3001,
        "health_path": "/health",
        "required": False,
    },
    "frontend": {
        "type": ServiceType.FRONTEND,
        "port": 3000,
        "health_path": "/",
        "required": False,
    },
}


# =============================================================================
# SERVICE REGISTRY
# =============================================================================

class ServiceRegistry:
    """
    Dynamic service registry for Trinity services.
    
    Manages service discovery, registration, and health tracking.
    
    Usage:
        registry = ServiceRegistry()
        
        # Register a service
        registry.register("jarvis-body", ServiceType.JARVIS_BODY, port=8010)
        
        # Get service info
        info = registry.get("jarvis-body")
        
        # Update status
        registry.update_status("jarvis-body", ServiceStatus.HEALTHY)
        
        # Discover running services
        await registry.discover_services()
    """
    
    _instance: Optional["ServiceRegistry"] = None
    _lock = threading.Lock()
    
    def __new__(cls) -> "ServiceRegistry":
        """Singleton pattern."""
        with cls._lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._initialized = False
                cls._instance = instance
            return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._services: Dict[str, ServiceInfo] = {}
        self._callbacks: List[Callable[[str, ServiceStatus], None]] = []
        self._discovery_lock = asyncio.Lock()
        self._heartbeat_dir = Path.home() / ".jarvis" / "heartbeats"
        self._initialized = True
        
        # Ensure heartbeat directory exists
        self._heartbeat_dir.mkdir(parents=True, exist_ok=True)
        
        # Register default services
        self._register_defaults()
        
        logger.debug("[ServiceRegistry] Initialized")
    
    def _register_defaults(self) -> None:
        """Register default Trinity services."""
        for name, config in DEFAULT_SERVICES.items():
            self._services[name] = ServiceInfo(
                name=name,
                service_type=config["type"],
                port=config["port"],
                health_path=config["health_path"],
            )
    
    def register(
        self,
        name: str,
        service_type: ServiceType,
        host: str = "localhost",
        port: int = 0,
        health_path: str = "/health",
        pid: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ServiceInfo:
        """
        Register a service with the registry.
        
        Args:
            name: Service name
            service_type: Type of service
            host: Service host
            port: Service port
            health_path: Health endpoint path
            pid: Process ID
            metadata: Additional metadata
        
        Returns:
            ServiceInfo for the registered service
        """
        info = ServiceInfo(
            name=name,
            service_type=service_type,
            host=host,
            port=port,
            health_path=health_path,
            pid=pid,
            started_at=datetime.now() if pid else None,
            status=ServiceStatus.STARTING if pid else ServiceStatus.UNKNOWN,
            metadata=metadata or {},
        )
        
        self._services[name] = info
        logger.info(f"[ServiceRegistry] Registered {name} on port {port}")
        
        # Write heartbeat file
        self._write_heartbeat(info)
        
        return info
    
    def unregister(self, name: str) -> None:
        """
        Unregister a service from the registry.
        
        Args:
            name: Service name to unregister
        """
        if name in self._services:
            del self._services[name]
            self._remove_heartbeat(name)
            logger.info(f"[ServiceRegistry] Unregistered {name}")
    
    def get(self, name: str) -> Optional[ServiceInfo]:
        """Get service info by name."""
        return self._services.get(name)
    
    def get_all(self) -> Dict[str, ServiceInfo]:
        """Get all registered services."""
        return dict(self._services)
    
    def get_by_type(self, service_type: ServiceType) -> List[ServiceInfo]:
        """Get all services of a specific type."""
        return [s for s in self._services.values() if s.service_type == service_type]
    
    def get_healthy(self) -> List[ServiceInfo]:
        """Get all healthy services."""
        return [s for s in self._services.values() if s.is_healthy]
    
    def update_status(
        self,
        name: str,
        status: ServiceStatus,
        error: Optional[str] = None,
    ) -> None:
        """
        Update service status.
        
        Args:
            name: Service name
            status: New status
            error: Optional error message
        """
        if name not in self._services:
            return
        
        service = self._services[name]
        old_status = service.status
        service.status = status
        service.last_health_check = datetime.now()
        
        if status == ServiceStatus.HEALTHY:
            service.last_healthy = datetime.now()
            service.consecutive_failures = 0
        elif status in (ServiceStatus.UNHEALTHY, ServiceStatus.STOPPED):
            service.consecutive_failures += 1
        
        # Log status change
        if old_status != status:
            logger.info(
                f"[ServiceRegistry] {name}: {old_status.value} -> {status.value}"
            )
            
            # Notify callbacks
            for callback in self._callbacks:
                try:
                    callback(name, status)
                except Exception as e:
                    logger.warning(f"[ServiceRegistry] Callback failed: {e}")
        
        # Update heartbeat
        self._write_heartbeat(service)
    
    def add_status_callback(
        self,
        callback: Callable[[str, ServiceStatus], None],
    ) -> None:
        """Add a callback for status changes."""
        self._callbacks.append(callback)
    
    # =========================================================================
    # DISCOVERY
    # =========================================================================
    
    async def discover_services(self) -> Dict[str, bool]:
        """
        Discover running services by probing known ports.
        
        Returns:
            Dictionary mapping service names to discovery success
        """
        async with self._discovery_lock:
            results = {}
            
            for name, service in self._services.items():
                is_running = await self._probe_service(service)
                results[name] = is_running
                
                if is_running:
                    service.status = ServiceStatus.HEALTHY
                else:
                    service.status = ServiceStatus.STOPPED
            
            return results
    
    async def _probe_service(self, service: ServiceInfo) -> bool:
        """
        Probe a service to check if it's running.
        
        Args:
            service: Service to probe
        
        Returns:
            True if service is responding
        """
        try:
            import aiohttp
            
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5.0)
            ) as session:
                async with session.get(service.health_url) as resp:
                    return resp.status in (200, 204)
        except Exception:
            # Try socket-level check
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                result = sock.connect_ex((service.host, service.port))
                sock.close()
                return result == 0
            except Exception:
                return False
    
    def discover_from_heartbeats(self) -> Dict[str, ServiceInfo]:
        """
        Discover services from heartbeat files.
        
        Returns:
            Dictionary of discovered services
        """
        discovered = {}
        
        if not self._heartbeat_dir.exists():
            return discovered
        
        for heartbeat_file in self._heartbeat_dir.glob("*.heartbeat.json"):
            try:
                data = json.loads(heartbeat_file.read_text())
                name = data.get("name")
                if not name:
                    continue
                
                # Check if heartbeat is recent (within last 5 minutes)
                last_beat = datetime.fromisoformat(data.get("timestamp", ""))
                if datetime.now() - last_beat > timedelta(minutes=5):
                    continue  # Stale heartbeat
                
                info = ServiceInfo(
                    name=name,
                    service_type=ServiceType(data.get("type", "external")),
                    host=data.get("host", "localhost"),
                    port=data.get("port", 0),
                    pid=data.get("pid"),
                    status=ServiceStatus(data.get("status", "unknown")),
                )
                
                discovered[name] = info
                
                # Update registry
                self._services[name] = info
                
            except Exception as e:
                logger.debug(f"[ServiceRegistry] Failed to read heartbeat {heartbeat_file}: {e}")
        
        return discovered
    
    # =========================================================================
    # HEARTBEAT
    # =========================================================================
    
    def _write_heartbeat(self, service: ServiceInfo) -> None:
        """Write heartbeat file for a service."""
        try:
            heartbeat_path = self._heartbeat_dir / f"{service.name}.heartbeat.json"
            data = {
                "name": service.name,
                "type": service.service_type.value,
                "host": service.host,
                "port": service.port,
                "pid": service.pid,
                "status": service.status.value,
                "timestamp": datetime.now().isoformat(),
            }
            heartbeat_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.debug(f"[ServiceRegistry] Failed to write heartbeat: {e}")
    
    def _remove_heartbeat(self, name: str) -> None:
        """Remove heartbeat file for a service."""
        try:
            heartbeat_path = self._heartbeat_dir / f"{name}.heartbeat.json"
            if heartbeat_path.exists():
                heartbeat_path.unlink()
        except Exception:
            pass
    
    # =========================================================================
    # SERIALIZATION
    # =========================================================================
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert registry to dictionary."""
        return {
            name: service.to_dict()
            for name, service in self._services.items()
        }
    
    def get_summary(self) -> Dict[str, Any]:
        """Get summary of service statuses."""
        statuses = {}
        for status in ServiceStatus:
            statuses[status.value] = len([
                s for s in self._services.values()
                if s.status == status
            ])
        
        return {
            "total_services": len(self._services),
            "healthy": statuses.get(ServiceStatus.HEALTHY.value, 0),
            "unhealthy": statuses.get(ServiceStatus.UNHEALTHY.value, 0),
            "stopped": statuses.get(ServiceStatus.STOPPED.value, 0),
            "statuses": statuses,
        }


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_registry_instance: Optional[ServiceRegistry] = None


def get_service_registry() -> ServiceRegistry:
    """Get the singleton ServiceRegistry instance."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = ServiceRegistry()
    return _registry_instance


# =============================================================================
# MODULE INITIALIZATION
# =============================================================================

logger.debug("[ServiceRegistry] Module loaded")
