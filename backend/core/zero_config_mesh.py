#!/usr/bin/env python3
"""Zero Configuration Mesh Network for JARVIS.

This module provides automatic service discovery and interconnection capabilities
for the JARVIS system. Services can automatically find and connect to each other
without manual configuration, creating a self-organizing mesh network.

The mesh network supports:
- Automatic service registration and discovery
- Health monitoring and stale node removal
- Service type-based filtering
- Broadcast messaging capabilities
- Real-time status monitoring

Example:
    >>> mesh = get_mesh()
    >>> await mesh.start()
    >>> await mesh.join({
    ...     "name": "vision_service",
    ...     "type": "vision",
    ...     "port": 8001,
    ...     "protocol": "http"
    ... })
    >>> services = await mesh.find_services_by_type("vision")
"""

import asyncio
import logging
from typing import Dict, Optional, Any, List
from datetime import datetime
import socket
import json

logger = logging.getLogger(__name__)


class ZeroConfigMesh:
    """Zero-configuration mesh network for service discovery.
    
    Services automatically find and connect to each other without manual
    configuration. The mesh network maintains a registry of active services
    and provides discovery, health monitoring, and communication capabilities.
    
    Attributes:
        nodes: Dictionary mapping service names to their information
        _running: Boolean indicating if the mesh network is active
        _discovery_task: Background task for periodic service discovery
    """

    def __init__(self) -> None:
        """Initialize the zero-configuration mesh network.
        
        Creates an empty mesh network ready to accept service registrations.
        The network is not started until start() is called.
        """
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self._running = False
        self._discovery_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the mesh network.
        
        Initializes the mesh network and begins periodic service discovery.
        This must be called before services can join or be discovered.
        
        Raises:
            RuntimeError: If the mesh network is already running
        """
        if self._running:
            raise RuntimeError("Mesh network is already running")
            
        self._running = True
        logger.info("✅ Zero-config mesh network started")

        # Start discovery background task
        self._discovery_task = asyncio.create_task(self._periodic_discovery())

    async def stop(self) -> None:
        """Stop the mesh network.
        
        Gracefully shuts down the mesh network, canceling background tasks
        and cleaning up resources. Services will no longer be discoverable
        after stopping.
        """
        self._running = False

        if self._discovery_task:
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except asyncio.CancelledError:
                pass

        logger.info("Zero-config mesh network stopped")

    async def join(self, service_info: Dict[str, Any]) -> None:
        """Add a service to the mesh network.

        Registers a service with the mesh network, making it discoverable
        by other services. The service information is timestamped and
        added to the active nodes registry.

        Args:
            service_info: Service information dictionary containing:
                - name (str): Unique service identifier (required)
                - type (str): Service type (e.g., "backend", "vision")
                - port (int): Service port number
                - protocol (str): Communication protocol (e.g., "http", "grpc")
                - Additional custom fields as needed
                
        Raises:
            ValueError: If service_info is missing required 'name' field
            
        Example:
            >>> await mesh.join({
            ...     "name": "voice_service",
            ...     "type": "voice",
            ...     "port": 8000,
            ...     "protocol": "websocket"
            ... })
        """
        name = service_info.get("name")
        if not name:
            raise ValueError("Cannot join mesh: service info missing 'name'")

        service_info["joined_at"] = datetime.now().isoformat()
        service_info["last_seen"] = datetime.now().isoformat()

        self.nodes[name] = service_info
        logger.info(f"✅ Service '{name}' joined mesh network")

    async def leave(self, service_name: str) -> None:
        """Remove a service from the mesh network.
        
        Unregisters a service from the mesh network, making it no longer
        discoverable. This is typically called when a service shuts down.

        Args:
            service_name: Name of the service to remove
            
        Example:
            >>> await mesh.leave("voice_service")
        """
        if service_name in self.nodes:
            del self.nodes[service_name]
            logger.info(f"Service '{service_name}' left mesh network")

    async def find_service(self, name: str) -> Optional[Dict[str, Any]]:
        """Find a service in the mesh network by name.

        Searches for a specific service by its unique name identifier.

        Args:
            name: Service name to find

        Returns:
            Service information dictionary if found, None otherwise.
            The dictionary contains all registered service metadata.
            
        Example:
            >>> service = await mesh.find_service("vision_service")
            >>> if service:
            ...     port = service["port"]
            ...     print(f"Vision service running on port {port}")
        """
        return self.nodes.get(name)

    async def find_services_by_type(self, service_type: str) -> List[Dict[str, Any]]:
        """Find all services of a given type.

        Searches for all services matching a specific service type.
        Useful for finding all instances of a particular service category.

        Args:
            service_type: Type of service to find (e.g., "backend", "vision", "voice")

        Returns:
            List of service information dictionaries for matching services.
            Empty list if no services of the specified type are found.
            
        Example:
            >>> vision_services = await mesh.find_services_by_type("vision")
            >>> for service in vision_services:
            ...     print(f"Found vision service: {service['name']}")
        """
        matching = []
        for service in self.nodes.values():
            if service.get("type") == service_type:
                matching.append(service)
        return matching

    def get_all_services(self) -> Dict[str, Dict[str, Any]]:
        """Get all services in the mesh.
        
        Returns a copy of all registered services in the mesh network.
        
        Returns:
            Dictionary mapping service names to their information dictionaries.
            Returns a copy to prevent external modification of internal state.
        """
        return self.nodes.copy()

    async def _periodic_discovery(self) -> None:
        """Periodically update service discovery.
        
        Background task that runs continuously while the mesh is active.
        Updates service health status and removes stale nodes that are
        no longer reachable.
        
        The task performs the following operations every 60 seconds:
        1. Check reachability of all registered services
        2. Update last_seen timestamps for reachable services
        3. Remove services that haven't been seen for 5 minutes
        
        Raises:
            asyncio.CancelledError: When the task is cancelled during shutdown
        """
        while self._running:
            try:
                await asyncio.sleep(60)  # Check every 60 seconds

                # Update last_seen for active nodes
                now = datetime.now().isoformat()
                for name, service in self.nodes.items():
                    # Check if service is still reachable
                    if await self._check_service_reachable(service):
                        service["last_seen"] = now
                    else:
                        logger.warning(f"Service '{name}' is unreachable")

                # Remove stale nodes (not seen for 5 minutes)
                stale_nodes = []
                for name, service in self.nodes.items():
                    last_seen = datetime.fromisoformat(service.get("last_seen", now))
                    age_seconds = (datetime.now() - last_seen).total_seconds()
                    if age_seconds > 300:  # 5 minutes
                        stale_nodes.append(name)

                for name in stale_nodes:
                    logger.warning(f"Removing stale node: {name}")
                    await self.leave(name)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic discovery: {e}")

    async def _check_service_reachable(self, service: Dict[str, Any]) -> bool:
        """Check if a service is reachable.
        
        Attempts to establish a connection to the service to verify it's
        still active and responding. Uses a simple TCP connection test.

        Args:
            service: Service information dictionary containing port information

        Returns:
            True if the service is reachable, False otherwise
            
        Note:
            Currently only supports localhost connections with a 2-second timeout.
            Services without a port number are considered unreachable.
        """
        try:
            port = service.get("port")
            if not port:
                return False

            # Try to connect to the port
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("localhost", port),
                timeout=2
            )
            writer.close()
            await writer.wait_closed()
            return True

        except Exception:
            return False

    def get_status(self) -> Dict[str, Any]:
        """Get mesh network status.
        
        Provides a summary of the current mesh network state including
        running status, node count, and basic information about each node.
        
        Returns:
            Dictionary containing:
                - running (bool): Whether the mesh network is active
                - node_count (int): Number of registered services
                - nodes (dict): Summary information for each service
                
        Example:
            >>> status = mesh.get_status()
            >>> print(f"Mesh has {status['node_count']} active services")
        """
        return {
            "running": self._running,
            "node_count": len(self.nodes),
            "nodes": {
                name: {
                    "port": service.get("port"),
                    "protocol": service.get("protocol"),
                    "type": service.get("type"),
                    "joined_at": service.get("joined_at"),
                    "last_seen": service.get("last_seen")
                }
                for name, service in self.nodes.items()
            }
        }

    async def get_mesh_config(self) -> Dict[str, Any]:
        """Get mesh configuration and topology.
        
        Provides detailed information about the mesh network configuration,
        topology, and statistics. Useful for monitoring and debugging.
        
        Returns:
            Dictionary containing:
                - enabled (bool): Whether the mesh is running
                - node_count (int): Number of registered nodes
                - topology (str): Network topology type
                - discovery_interval_seconds (int): Discovery check interval
                - stale_timeout_seconds (int): Timeout for stale node removal
                - nodes (dict): Complete node information
                - stats (dict): Network statistics and metrics
                
        Example:
            >>> config = await mesh.get_mesh_config()
            >>> print(f"Mesh topology: {config['topology']}")
            >>> print(f"Total connections: {config['stats']['total_connections']}")
        """
        return {
            "enabled": self._running,
            "node_count": len(self.nodes),
            "topology": "peer-to-peer",
            "discovery_interval_seconds": 60,
            "stale_timeout_seconds": 300,
            "nodes": self.nodes,
            "stats": {
                "total_nodes": len(self.nodes),
                "active_nodes": sum(1 for node in self.nodes.values() if node.get("last_seen")),
                "healthy_nodes": sum(1 for node in self.nodes.values() if node.get("last_seen")),  # Same as active for now
                "service_types": len(set(node.get("type") for node in self.nodes.values() if node.get("type"))),
                "total_connections": len(self.nodes) * (len(self.nodes) - 1) if len(self.nodes) > 1 else 0  # Mesh topology
            }
        }

    async def broadcast(self, message: Dict[str, Any]) -> None:
        """Broadcast a message to all services in the mesh.

        Sends a message to all registered services in the mesh network.
        This is useful for system-wide notifications or coordination.

        Args:
            message: Message dictionary to broadcast to all services.
                    Should contain all necessary information for recipients.
                    
        Note:
            Current implementation only logs the broadcast operation.
            A full implementation would establish connections and send
            the actual message to each service.
            
        Example:
            >>> await mesh.broadcast({
            ...     "type": "system_shutdown",
            ...     "timestamp": datetime.now().isoformat(),
            ...     "message": "System maintenance in 5 minutes"
            ... })
        """
        logger.info(f"Broadcasting message to {len(self.nodes)} nodes")
        for name, service in self.nodes.items():
            try:
                # In a full implementation, this would send the message
                # For now, just log it
                logger.debug(f"Would broadcast to {name}: {message}")
            except Exception as e:
                logger.error(f"Failed to broadcast to {name}: {e}")


# Global mesh instance
_mesh: Optional[ZeroConfigMesh] = None


def get_mesh() -> ZeroConfigMesh:
    """Get or create the global mesh instance.
    
    Provides access to the singleton mesh network instance. Creates a new
    instance if one doesn't exist yet. This ensures all parts of the
    application use the same mesh network.
    
    Returns:
        The global ZeroConfigMesh instance
        
    Example:
        >>> mesh = get_mesh()
        >>> await mesh.start()
        >>> # All other parts of the application will use the same mesh
        >>> other_mesh = get_mesh()  # Returns the same instance
    """
    global _mesh
    if _mesh is None:
        _mesh = ZeroConfigMesh()
    return _mesh