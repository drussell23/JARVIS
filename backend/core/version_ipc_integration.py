"""
Version-Aware IPC Integration v1.0
===================================

Integrates the version negotiation system with Trinity IPC, enabling:
1. Automatic version handshake during component registration
2. Version-aware heartbeat validation
3. Protocol version selection for cross-repo communication
4. Graceful degradation when versions mismatch

This module bridges:
- version_negotiation.py (version and capability negotiation)
- trinity_ipc.py (file-based IPC)
- service_registry.py (service discovery)
- trinity_event_bus.py (event streaming)

Usage:
    from backend.core.version_ipc_integration import (
        VersionAwareIPCManager,
        initialize_version_aware_ipc,
    )

    # Initialize during component startup
    manager = await initialize_version_aware_ipc(
        component_type=ComponentType.JARVIS_BODY,
        component_version="13.4.0",
    )

    # Automatically negotiates versions with discovered peers
    await manager.connect_to_peers()

    # Send version-aware messages
    await manager.send_command(target, action, payload)

Author: JARVIS Trinity v96.0 - Version-Aware IPC Integration
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Lazy Imports to Avoid Circular Dependencies
# =============================================================================

def _get_version_negotiation():
    """Lazy import of version negotiation module."""
    try:
        from backend.core.version_negotiation import (  # type: ignore[import]
            Capability,
            CapabilitySet,
            ComponentIdentity,
            ComponentType,
            NegotiationResult,
            SemanticVersion,
            VersionAwareConnectionManager,
            VersionDeclaration,
            VersionNegotiator,
            get_protocol_handler,
            initialize_version_negotiation,
        )
        return {
            "Capability": Capability,
            "CapabilitySet": CapabilitySet,
            "ComponentIdentity": ComponentIdentity,
            "ComponentType": ComponentType,
            "NegotiationResult": NegotiationResult,
            "SemanticVersion": SemanticVersion,
            "VersionAwareConnectionManager": VersionAwareConnectionManager,
            "VersionDeclaration": VersionDeclaration,
            "VersionNegotiator": VersionNegotiator,
            "get_protocol_handler": get_protocol_handler,
            "initialize_version_negotiation": initialize_version_negotiation,
        }
    except ImportError as e:
        logger.warning(f"[VersionIPCIntegration] Version negotiation not available: {e}")
        return None


def _get_trinity_ipc():
    """Lazy import of Trinity IPC module."""
    try:
        from backend.core.trinity_ipc import (
            ComponentType as IPCComponentType,
            HeartbeatData,
            TrinityIPCConfig,
        )
        return {
            "ComponentType": IPCComponentType,
            "HeartbeatData": HeartbeatData,
            "TrinityIPCConfig": TrinityIPCConfig,
        }
    except ImportError as e:
        logger.warning(f"[VersionIPCIntegration] Trinity IPC not available: {e}")
        return None


def _get_service_registry():
    """Lazy import of service registry for version-aware service discovery."""
    try:
        from backend.core.service_registry import (
            ServiceRegistry,
            get_service_registry,
        )
        return {
            "ServiceRegistry": ServiceRegistry,
            "get_service_registry": get_service_registry,
        }
    except ImportError as e:
        logger.warning(f"[VersionIPCIntegration] Service registry not available: {e}")
        return None


# Export service registry getter for external use
def get_version_aware_service_registry():
    """Get service registry with version awareness."""
    return _get_service_registry()


def _get_event_bus():
    """Lazy import of event bus."""
    try:
        from backend.core.trinity_event_bus import (
            TrinityEvent,
            TrinityEventBus,
            get_trinity_event_bus,
            EventPriority,
            RepoType,
        )
        return {
            "TrinityEvent": TrinityEvent,
            "TrinityEventBus": TrinityEventBus,
            "get_trinity_event_bus": get_trinity_event_bus,
            "EventPriority": EventPriority,
            "RepoType": RepoType,
        }
    except ImportError as e:
        logger.warning(f"[VersionIPCIntegration] Event bus not available: {e}")
        return None


# =============================================================================
# Configuration
# =============================================================================

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")


def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)


class IntegrationConfig:
    """Configuration for version-aware IPC integration."""

    # Auto-negotiation settings
    AUTO_NEGOTIATE_ON_CONNECT = _env_bool("VERSION_AUTO_NEGOTIATE", True)
    NEGOTIATE_TIMEOUT_MS = _env_int("VERSION_NEGOTIATE_TIMEOUT_MS", 10000)
    PEER_DISCOVERY_INTERVAL_MS = _env_int("VERSION_PEER_DISCOVERY_MS", 5000)

    # Compatibility settings
    ALLOW_DEGRADED_MODE = _env_bool("VERSION_ALLOW_DEGRADED", True)
    STRICT_VERSION_CHECK = _env_bool("VERSION_STRICT_CHECK", False)

    # Event publishing
    PUBLISH_VERSION_EVENTS = _env_bool("VERSION_PUBLISH_EVENTS", True)

    # Integration directory
    INTEGRATION_DIR = Path(_env_str(
        "VERSION_INTEGRATION_DIR",
        str(Path.home() / ".jarvis" / "trinity" / "version_ipc")
    ))


# Ensure directories exist
IntegrationConfig.INTEGRATION_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Enhanced Heartbeat with Version Information
# =============================================================================

@dataclass
class VersionAwareHeartbeat:
    """
    Extended heartbeat that includes version negotiation information.

    Extends the standard HeartbeatData with:
    - Full semantic version
    - Protocol version range
    - Capability list
    - Negotiation status with peers
    """
    # Standard heartbeat fields
    component_type: str
    component_id: str
    timestamp: float
    pid: int
    host: str
    status: str
    uptime_seconds: float

    # Version fields
    component_version: str
    protocol_version_min: int
    protocol_version_max: int
    capabilities: List[str]

    # Negotiation status
    negotiated_peers: Dict[str, int] = field(default_factory=dict)  # peer_key -> protocol_version
    failed_negotiations: Dict[str, str] = field(default_factory=dict)  # peer_key -> error

    # Legacy compatibility
    version: str = "96.0"  # For backwards compatibility with older components

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component_type": self.component_type,
            "component_id": self.component_id,
            "timestamp": self.timestamp,
            "pid": self.pid,
            "host": self.host,
            "status": self.status,
            "uptime_seconds": self.uptime_seconds,
            "component_version": self.component_version,
            "protocol_version_min": self.protocol_version_min,
            "protocol_version_max": self.protocol_version_max,
            "capabilities": self.capabilities,
            "negotiated_peers": self.negotiated_peers,
            "failed_negotiations": self.failed_negotiations,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VersionAwareHeartbeat":
        return cls(
            component_type=data.get("component_type", "unknown"),
            component_id=data.get("component_id", "unknown"),
            timestamp=data.get("timestamp", 0.0),
            pid=data.get("pid", 0),
            host=data.get("host", "unknown"),
            status=data.get("status", "unknown"),
            uptime_seconds=data.get("uptime_seconds", 0.0),
            component_version=data.get("component_version", "0.0.0"),
            protocol_version_min=data.get("protocol_version_min", 1),
            protocol_version_max=data.get("protocol_version_max", 1),
            capabilities=data.get("capabilities", []),
            negotiated_peers=data.get("negotiated_peers", {}),
            failed_negotiations=data.get("failed_negotiations", {}),
            version=data.get("version", "96.0"),
        )


# =============================================================================
# Version-Aware IPC Manager
# =============================================================================

class VersionAwareIPCManager:
    """
    Manages IPC with version negotiation integrated.

    This class wraps the standard Trinity IPC with version awareness,
    ensuring that all cross-component communication is version-compatible.
    """

    def __init__(
        self,
        component_type: str,
        component_version: str,
        protocol_version_min: int = 2,
        protocol_version_max: int = 3,
        capabilities: Optional[List[str]] = None,
    ):
        self._component_type = component_type
        self._component_version = component_version
        self._protocol_min = protocol_version_min
        self._protocol_max = protocol_version_max
        self._capabilities = capabilities or []

        self._negotiator: Optional[Any] = None
        self._connection_manager: Optional[Any] = None
        self._ipc_manager: Optional[Any] = None
        self._event_bus: Optional[Any] = None

        self._running = False
        self._peer_discovery_task: Optional[asyncio.Task] = None
        self._negotiation_results: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

        self._start_time = time.time()
        self._component_id = f"{component_type}_{os.getpid()}"

        logger.info(
            f"[VersionAwareIPC] Created manager for {component_type} v{component_version}"
        )

    async def initialize(self) -> bool:
        """
        Initialize the version-aware IPC system.

        Returns:
            True if initialization successful
        """
        if self._running:
            return True

        try:
            # Initialize version negotiation
            vn = _get_version_negotiation()
            if vn:
                # Map component type string to enum
                comp_type = vn["ComponentType"](self._component_type)

                self._negotiator = await vn["initialize_version_negotiation"](
                    component_type=comp_type,
                    component_version=self._component_version,
                    protocol_version_min=self._protocol_min,
                    protocol_version_max=self._protocol_max,
                    capabilities=self._capabilities,
                )

                self._connection_manager = vn["VersionAwareConnectionManager"](
                    self._negotiator
                )

            # Initialize Trinity IPC if available
            ipc = _get_trinity_ipc()
            if ipc:
                # Note: TrinityIPCManager may need to be instantiated differently
                # This is a placeholder for the actual initialization
                pass

            # Get event bus if available
            eb = _get_event_bus()
            if eb:
                try:
                    self._event_bus = await eb["get_trinity_event_bus"]()
                except Exception as e:
                    logger.debug(f"[VersionAwareIPC] Event bus not ready: {e}")

            self._running = True

            # Start peer discovery task
            if IntegrationConfig.AUTO_NEGOTIATE_ON_CONNECT:
                self._peer_discovery_task = asyncio.create_task(
                    self._peer_discovery_loop()
                )

            # Publish initialization event
            await self._publish_version_event(
                event_type="version.component.initialized",
                payload={
                    "component_type": self._component_type,
                    "component_version": self._component_version,
                    "protocol_range": [self._protocol_min, self._protocol_max],
                    "capabilities": self._capabilities,
                },
            )

            logger.info(f"[VersionAwareIPC] Initialized successfully")
            return True

        except Exception as e:
            logger.error(f"[VersionAwareIPC] Initialization failed: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown the version-aware IPC system."""
        if not self._running:
            return

        self._running = False

        # Cancel peer discovery
        if self._peer_discovery_task:
            self._peer_discovery_task.cancel()
            try:
                await self._peer_discovery_task
            except asyncio.CancelledError:
                pass

        # Publish shutdown event
        await self._publish_version_event(
            event_type="version.component.shutdown",
            payload={"component_type": self._component_type},
        )

        logger.info(f"[VersionAwareIPC] Shutdown complete")

    async def _peer_discovery_loop(self) -> None:
        """Background loop for discovering and negotiating with peers."""
        interval = IntegrationConfig.PEER_DISCOVERY_INTERVAL_MS / 1000

        while self._running:
            try:
                await self._discover_and_negotiate()
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[VersionAwareIPC] Peer discovery error: {e}")
                await asyncio.sleep(5.0)

    async def _discover_and_negotiate(self) -> None:
        """Discover peers and negotiate versions."""
        if not self._negotiator:
            return

        try:
            # Discover peers
            peers = await self._negotiator.discover_peers()

            for peer in peers:
                peer_key = peer.identity.unique_key

                # Skip if already negotiated
                if peer_key in self._negotiation_results:
                    continue

                # Negotiate
                result = await self._negotiator.negotiate_with_peer(peer)

                async with self._lock:
                    self._negotiation_results[peer_key] = result

                # Publish event
                if result.success:
                    await self._publish_version_event(
                        event_type="version.negotiation.success",
                        payload={
                            "peer_type": peer.identity.component_type.value,
                            "peer_version": str(peer.component_version),
                            "protocol_version": result.negotiated_protocol_version,
                            "common_capabilities": (
                                result.common_capabilities.to_list()
                                if result.common_capabilities else []
                            ),
                        },
                    )
                else:
                    await self._publish_version_event(
                        event_type="version.negotiation.failed",
                        payload={
                            "peer_type": peer.identity.component_type.value,
                            "error": result.error,
                        },
                    )

        except Exception as e:
            logger.debug(f"[VersionAwareIPC] Negotiate error: {e}")

    async def _publish_version_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        """Publish a version-related event."""
        if not IntegrationConfig.PUBLISH_VERSION_EVENTS:
            return

        if not self._event_bus:
            return

        try:
            eb = _get_event_bus()
            if not eb:
                return

            event = eb["TrinityEvent"](
                topic=event_type,
                source=eb["RepoType"].JARVIS,
                target=eb["RepoType"].BROADCAST,
                priority=eb["EventPriority"].NORMAL,
                payload=payload,
            )

            await self._event_bus.publish(event)

        except Exception as e:
            logger.debug(f"[VersionAwareIPC] Event publish error: {e}")

    async def get_negotiated_protocol(self, peer_key: str) -> Optional[int]:
        """
        Get the negotiated protocol version for a peer.

        Returns:
            Protocol version or None if not negotiated
        """
        async with self._lock:
            result = self._negotiation_results.get(peer_key)
            if result and result.success:
                return result.negotiated_protocol_version
        return None

    async def has_capability(
        self,
        peer_key: str,
        capability: str,
    ) -> bool:
        """
        Check if a negotiated peer has a specific capability.

        Args:
            peer_key: The peer's unique key
            capability: Capability name to check

        Returns:
            True if peer has the capability
        """
        async with self._lock:
            result = self._negotiation_results.get(peer_key)
            if result and result.success and result.common_capabilities:
                return capability.upper() in [
                    c.upper() for c in result.common_capabilities.to_list()
                ]
        return False

    async def send_version_aware_message(
        self,
        target_peer: str,
        action: str,
        payload: Dict[str, Any],
        require_capability: Optional[str] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Send a message with version awareness.

        Automatically selects the correct protocol handler based on
        negotiated version with the target peer.

        Args:
            target_peer: Target peer's unique key
            action: Action/command name
            payload: Message payload
            require_capability: Required capability (fails if not present)

        Returns:
            Tuple of (success, response)
        """
        # Check capability if required
        if require_capability:
            has_cap = await self.has_capability(target_peer, require_capability)
            if not has_cap:
                logger.warning(
                    f"[VersionAwareIPC] Peer {target_peer} lacks required "
                    f"capability: {require_capability}"
                )
                return False, {"error": f"Missing capability: {require_capability}"}

        # Get protocol version
        protocol_version = await self.get_negotiated_protocol(target_peer)

        if protocol_version is None:
            if IntegrationConfig.ALLOW_DEGRADED_MODE:
                # Use minimum protocol in degraded mode
                protocol_version = self._protocol_min
                logger.debug(
                    f"[VersionAwareIPC] Using degraded mode (protocol v{protocol_version}) "
                    f"for {target_peer}"
                )
            else:
                return False, {"error": "No negotiated protocol version"}

        # Get protocol handler
        vn = _get_version_negotiation()
        if not vn:
            return False, {"error": "Version negotiation not available"}

        handler = vn["get_protocol_handler"](protocol_version)

        # Encode message
        message = {
            "action": action,
            "payload": payload,
            "timestamp": time.time(),
            "source": self._component_id,
        }

        try:
            encoded = await handler.encode_message(message)
            # In a real implementation, this would send via IPC
            # For now, we just return the encoded message info
            return True, {
                "protocol_version": protocol_version,
                "message_size": len(encoded),
            }
        except Exception as e:
            return False, {"error": str(e)}

    def get_heartbeat(self) -> VersionAwareHeartbeat:
        """
        Get current version-aware heartbeat.

        Returns:
            VersionAwareHeartbeat with current state
        """
        negotiated_peers = {}
        failed_negotiations = {}

        for peer_key, result in self._negotiation_results.items():
            if result.success and result.negotiated_protocol_version:
                negotiated_peers[peer_key] = result.negotiated_protocol_version
            elif result.error:
                failed_negotiations[peer_key] = result.error

        import socket
        return VersionAwareHeartbeat(
            component_type=self._component_type,
            component_id=self._component_id,
            timestamp=time.time(),
            pid=os.getpid(),
            host=socket.gethostname(),
            status="ready" if self._running else "stopped",
            uptime_seconds=time.time() - self._start_time,
            component_version=self._component_version,
            protocol_version_min=self._protocol_min,
            protocol_version_max=self._protocol_max,
            capabilities=self._capabilities,
            negotiated_peers=negotiated_peers,
            failed_negotiations=failed_negotiations,
        )

    def get_status(self) -> Dict[str, Any]:
        """Get current status of the version-aware IPC manager."""
        return {
            "running": self._running,
            "component_type": self._component_type,
            "component_version": self._component_version,
            "protocol_range": [self._protocol_min, self._protocol_max],
            "capabilities": self._capabilities,
            "uptime_seconds": time.time() - self._start_time,
            "negotiated_peers": len([
                r for r in self._negotiation_results.values() if r.success
            ]),
            "failed_negotiations": len([
                r for r in self._negotiation_results.values() if not r.success
            ]),
        }


# =============================================================================
# Version Compatibility Validator
# =============================================================================

class VersionCompatibilityValidator:
    """
    Validates version compatibility across the Trinity system.

    Use this to check if components are compatible before attempting
    to connect or send messages.
    """

    # Known breaking changes between versions
    BREAKING_CHANGES: Dict[str, List[Tuple[str, str]]] = {
        "jarvis_body": [
            ("12.0.0", "Changed API response format"),
            ("10.0.0", "Removed legacy endpoints"),
        ],
        "jarvis_prime": [
            ("2.0.0", "Changed inference protocol"),
        ],
        "reactor_core": [
            ("1.0.0", "Initial release"),
        ],
    }

    # Minimum required versions for each component type
    MINIMUM_VERSIONS: Dict[str, str] = {
        "jarvis_body": "12.0.0",
        "jarvis_prime": "1.5.0",
        "reactor_core": "1.0.0",
    }

    @classmethod
    def is_version_compatible(
        cls,
        component_type: str,
        version: str,
        strict: bool = False,
    ) -> Tuple[bool, List[str]]:
        """
        Check if a component version is compatible.

        Args:
            component_type: Type of component
            version: Version string to check
            strict: If True, require exact minimum version match

        Returns:
            Tuple of (is_compatible, list of warnings)
        """
        warnings = []

        vn = _get_version_negotiation()
        if not vn:
            return True, ["Version negotiation not available"]

        try:
            parsed_version = vn["SemanticVersion"].parse(version)
        except ValueError:
            return False, [f"Invalid version format: {version}"]

        # Check minimum version
        min_version_str = cls.MINIMUM_VERSIONS.get(component_type)
        if min_version_str:
            min_version = vn["SemanticVersion"].parse(min_version_str)

            if parsed_version < min_version:
                return False, [
                    f"Version {version} below minimum {min_version_str}"
                ]

        # Check for breaking changes
        breaking = cls.BREAKING_CHANGES.get(component_type, [])
        for break_version, description in breaking:
            break_v = vn["SemanticVersion"].parse(break_version)
            if parsed_version < break_v:
                warnings.append(
                    f"Version {version} is before breaking change {break_version}: "
                    f"{description}"
                )

        return len(warnings) == 0 or not strict, warnings

    @classmethod
    def check_protocol_compatibility(
        cls,
        local_range: Tuple[int, int],
        remote_range: Tuple[int, int],
    ) -> Tuple[bool, Optional[int], str]:
        """
        Check if protocol version ranges are compatible.

        Args:
            local_range: (min, max) for local component
            remote_range: (min, max) for remote component

        Returns:
            Tuple of (is_compatible, common_version, message)
        """
        local_min, local_max = local_range
        remote_min, remote_max = remote_range

        # Find overlap
        common_min = max(local_min, remote_min)
        common_max = min(local_max, remote_max)

        if common_min > common_max:
            return False, None, (
                f"No protocol overlap. Local: {local_min}-{local_max}, "
                f"Remote: {remote_min}-{remote_max}"
            )

        # Use highest common version
        return True, common_max, f"Compatible at protocol v{common_max}"


# =============================================================================
# Global Instance Management
# =============================================================================

_ipc_manager: Optional[VersionAwareIPCManager] = None


async def initialize_version_aware_ipc(
    component_type: str,
    component_version: str,
    protocol_version_min: int = 2,
    protocol_version_max: int = 3,
    capabilities: Optional[List[str]] = None,
) -> VersionAwareIPCManager:
    """
    Initialize the global version-aware IPC manager.

    Args:
        component_type: Type of this component (e.g., "jarvis_body")
        component_version: Semantic version string
        protocol_version_min: Minimum supported protocol version
        protocol_version_max: Maximum supported protocol version
        capabilities: List of capability names

    Returns:
        Initialized VersionAwareIPCManager
    """
    global _ipc_manager

    _ipc_manager = VersionAwareIPCManager(
        component_type=component_type,
        component_version=component_version,
        protocol_version_min=protocol_version_min,
        protocol_version_max=protocol_version_max,
        capabilities=capabilities,
    )

    await _ipc_manager.initialize()
    return _ipc_manager


async def get_version_aware_ipc() -> VersionAwareIPCManager:
    """Get the global version-aware IPC manager."""
    if _ipc_manager is None:
        raise RuntimeError("Version-aware IPC not initialized")
    return _ipc_manager


async def shutdown_version_aware_ipc() -> None:
    """Shutdown the global version-aware IPC manager."""
    global _ipc_manager

    if _ipc_manager:
        await _ipc_manager.shutdown()
        _ipc_manager = None


# =============================================================================
# Convenience Decorator for Version-Aware Functions
# =============================================================================

def require_version(
    min_version: str,
    capability: Optional[str] = None,
):
    """
    Decorator that enforces version requirements for a function.

    Usage:
        @require_version("2.0.0", capability="STREAMING")
        async def stream_response(peer_key: str, data: dict):
            ...
    """
    def decorator(func: Callable) -> Callable:
        async def wrapper(*args, **kwargs):
            # Extract peer_key from args/kwargs
            peer_key = kwargs.get("peer_key") or (args[0] if args else None)

            if not peer_key:
                raise ValueError("peer_key required for version-checked function")

            if _ipc_manager is None:
                raise RuntimeError("Version-aware IPC not initialized")

            # Check capability if required
            if capability:
                has_cap = await _ipc_manager.has_capability(peer_key, capability)
                if not has_cap:
                    raise RuntimeError(
                        f"Peer {peer_key} lacks required capability: {capability}"
                    )

            # Check version
            is_compatible, warnings = VersionCompatibilityValidator.is_version_compatible(
                _ipc_manager._component_type,
                min_version,
            )

            if not is_compatible:
                raise RuntimeError(
                    f"Version incompatibility: {'; '.join(warnings)}"
                )

            return await func(*args, **kwargs)

        return wrapper
    return decorator
