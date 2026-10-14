"""
Docker Cross-Repo Coordinator v1.0

Coordinates Docker state across JARVIS Trinity ecosystem:
- JARVIS-AI-Agent (Body) - Main supervisor
- JARVIS-Prime (Mind) - Inference routing
- Reactor-Core (Nerves) - Training orchestration

Uses Trinity Protocol for file-based IPC with atomic operations.

Author: JARVIS AGI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class CrossRepoConfig:
    """Cross-repo coordination configuration"""

    # Trinity state directory
    trinity_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv('TRINITY_STATE_DIR', str(Path.home() / '.jarvis' / 'trinity'))
        )
    )

    # Docker-specific subdirectory
    docker_state_dir: Path = field(default=None)  # type: ignore

    # Event retention
    max_events: int = field(
        default_factory=lambda: int(os.getenv('DOCKER_MAX_EVENTS', '100'))
    )
    event_ttl_hours: int = field(
        default_factory=lambda: int(os.getenv('DOCKER_EVENT_TTL_HOURS', '24'))
    )

    # Heartbeat settings
    heartbeat_interval_seconds: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_HEARTBEAT_INTERVAL', '10.0'))
    )
    heartbeat_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv('DOCKER_HEARTBEAT_TIMEOUT', '30.0'))
    )

    # Enable/disable
    enabled: bool = field(
        default_factory=lambda: os.getenv('DOCKER_CROSS_REPO_ENABLED', 'true').lower() == 'true'
    )

    def __post_init__(self):
        if self.docker_state_dir is None:
            self.docker_state_dir = self.trinity_dir / 'docker'

        # Ensure directories exist
        self.trinity_dir.mkdir(parents=True, exist_ok=True)
        self.docker_state_dir.mkdir(parents=True, exist_ok=True)


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class DockerEventType(Enum):
    """Docker state events for cross-repo coordination"""

    # Lifecycle
    STARTING = "docker.starting"
    STARTED = "docker.started"
    STOPPING = "docker.stopping"
    STOPPED = "docker.stopped"

    # Health
    HEALTHY = "docker.healthy"
    UNHEALTHY = "docker.unhealthy"
    DEGRADED = "docker.degraded"

    # Recovery
    RECOVERY_STARTED = "docker.recovery.started"
    RECOVERY_LEVEL_ESCALATED = "docker.recovery.escalated"
    RECOVERY_SUCCEEDED = "docker.recovery.succeeded"
    RECOVERY_FAILED = "docker.recovery.failed"

    # Resources
    RESOURCE_WARNING = "docker.resource.warning"
    RESOURCE_CRITICAL = "docker.resource.critical"

    # Requests (from other repos)
    REQUEST_START = "docker.request.start"
    REQUEST_STOP = "docker.request.stop"
    REQUEST_STATUS = "docker.request.status"
    REQUEST_HEALTH = "docker.request.health"


class ComponentType(Enum):
    """Trinity component types"""
    JARVIS_AGENT = "jarvis_agent"      # Body
    JARVIS_PRIME = "jarvis_prime"      # Mind
    REACTOR_CORE = "reactor_core"      # Nerves


@dataclass
class DockerEvent:
    """Docker event for cross-repo communication"""
    event_id: str
    event_type: DockerEventType
    source: ComponentType
    timestamp: float
    payload: Dict[str, Any]
    correlation_id: Optional[str] = None
    requires_ack: bool = False
    ack_received: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'event_id': self.event_id,
            'event_type': self.event_type.value,
            'source': self.source.value,
            'timestamp': self.timestamp,
            'payload': self.payload,
            'correlation_id': self.correlation_id,
            'requires_ack': self.requires_ack,
            'ack_received': self.ack_received,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DockerEvent':
        return cls(
            event_id=data['event_id'],
            event_type=DockerEventType(data['event_type']),
            source=ComponentType(data['source']),
            timestamp=data['timestamp'],
            payload=data.get('payload', {}),
            correlation_id=data.get('correlation_id'),
            requires_ack=data.get('requires_ack', False),
            ack_received=data.get('ack_received', False),
        )


@dataclass
class DockerState:
    """Current Docker state for cross-repo sharing"""
    status: str
    healthy: bool
    last_check_timestamp: float
    health_score: float
    startup_time_ms: Optional[int]
    error_message: Optional[str]
    recovery_in_progress: bool
    recovery_level: Optional[int]
    circuit_breaker_state: str
    resource_status: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'healthy': self.healthy,
            'last_check_timestamp': self.last_check_timestamp,
            'health_score': self.health_score,
            'startup_time_ms': self.startup_time_ms,
            'error_message': self.error_message,
            'recovery_in_progress': self.recovery_in_progress,
            'recovery_level': self.recovery_level,
            'circuit_breaker_state': self.circuit_breaker_state,
            'resource_status': self.resource_status,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DockerState':
        return cls(
            status=data.get('status', 'unknown'),
            healthy=data.get('healthy', False),
            last_check_timestamp=data.get('last_check_timestamp', 0),
            health_score=data.get('health_score', 0.0),
            startup_time_ms=data.get('startup_time_ms'),
            error_message=data.get('error_message'),
            recovery_in_progress=data.get('recovery_in_progress', False),
            recovery_level=data.get('recovery_level'),
            circuit_breaker_state=data.get('circuit_breaker_state', 'unknown'),
            resource_status=data.get('resource_status', {}),
        )


# =============================================================================
# CROSS-REPO COORDINATOR
# =============================================================================

class DockerCrossRepoCoordinator:
    """
    Coordinates Docker state across JARVIS Trinity ecosystem.

    Responsibilities:
    - Publish Docker state changes via file-based IPC
    - Subscribe to Docker requests from other repos
    - Maintain heartbeat for health monitoring
    - Handle request-response patterns
    """

    def __init__(
        self,
        component: ComponentType = ComponentType.JARVIS_AGENT,
        config: Optional[CrossRepoConfig] = None
    ):
        self.component = component
        self.config = config or CrossRepoConfig()

        # State
        self._current_state: Optional[DockerState] = None
        self._event_handlers: Dict[DockerEventType, List[Callable]] = {}
        self._pending_requests: Dict[str, asyncio.Future] = {}

        # Files
        self._state_file = self.config.docker_state_dir / 'state.json'
        self._events_file = self.config.docker_state_dir / 'events.json'
        self._heartbeat_file = self.config.docker_state_dir / f'heartbeat_{component.value}.json'
        self._requests_dir = self.config.docker_state_dir / 'requests'
        self._responses_dir = self.config.docker_state_dir / 'responses'

        # Ensure directories
        self._requests_dir.mkdir(parents=True, exist_ok=True)
        self._responses_dir.mkdir(parents=True, exist_ok=True)

        # Locks
        self._state_lock = asyncio.Lock()
        self._event_lock = asyncio.Lock()

        # Background tasks
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._request_watcher_task: Optional[asyncio.Task] = None
        self._running = False

        logger.info(f"DockerCrossRepoCoordinator initialized for {component.value}")

    async def start(self):
        """Start background tasks"""
        if not self.config.enabled:
            logger.info("Cross-repo coordination disabled")
            return

        self._running = True

        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="docker_heartbeat"
        )

        # Start request watcher (only for JARVIS_AGENT which owns Docker)
        if self.component == ComponentType.JARVIS_AGENT:
            self._request_watcher_task = asyncio.create_task(
                self._watch_requests(),
                name="docker_request_watcher"
            )

        logger.info("DockerCrossRepoCoordinator started")

    async def stop(self):
        """Stop background tasks"""
        self._running = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._request_watcher_task:
            self._request_watcher_task.cancel()
            try:
                await self._request_watcher_task
            except asyncio.CancelledError:
                pass

        logger.info("DockerCrossRepoCoordinator stopped")

    # =========================================================================
    # STATE MANAGEMENT
    # =========================================================================

    async def update_state(self, state: DockerState):
        """Update and publish Docker state"""
        if not self.config.enabled:
            return

        async with self._state_lock:
            self._current_state = state

            # Atomic write
            state_data = {
                'component': self.component.value,
                'timestamp': time.time(),
                'state': state.to_dict(),
            }

            await self._atomic_write(self._state_file, state_data)

        logger.debug(f"Docker state updated: {state.status}")

    async def get_state(self) -> Optional[DockerState]:
        """Get current Docker state"""
        if self._current_state:
            return self._current_state

        # Try to load from file
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                return DockerState.from_dict(data.get('state', {}))
        except Exception as e:
            logger.debug(f"Failed to load Docker state: {e}")

        return None

    # =========================================================================
    # EVENT PUBLISHING
    # =========================================================================

    async def publish_event(
        self,
        event_type: DockerEventType,
        payload: Dict[str, Any],
        correlation_id: Optional[str] = None,
        requires_ack: bool = False
    ) -> str:
        """Publish Docker event to all repos"""
        if not self.config.enabled:
            return ""

        event = DockerEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            source=self.component,
            timestamp=time.time(),
            payload=payload,
            correlation_id=correlation_id,
            requires_ack=requires_ack,
        )

        async with self._event_lock:
            # Load existing events
            events = []
            try:
                if self._events_file.exists():
                    events = json.loads(self._events_file.read_text())
                    if not isinstance(events, list):
                        events = []
            except Exception:
                events = []

            # Append and trim
            events.append(event.to_dict())
            events = events[-self.config.max_events:]

            # Atomic write
            await self._atomic_write(self._events_file, events)

        logger.debug(f"Published Docker event: {event_type.value}")
        return event.event_id

    async def subscribe(
        self,
        event_types: List[DockerEventType],
        handler: Callable[[DockerEvent], Any]
    ):
        """Subscribe to Docker events"""
        for event_type in event_types:
            if event_type not in self._event_handlers:
                self._event_handlers[event_type] = []
            self._event_handlers[event_type].append(handler)

    async def get_recent_events(
        self,
        event_types: Optional[List[DockerEventType]] = None,
        since_timestamp: Optional[float] = None,
        limit: int = 50
    ) -> List[DockerEvent]:
        """Get recent Docker events"""
        try:
            if not self._events_file.exists():
                return []

            events_data = json.loads(self._events_file.read_text())
            if not isinstance(events_data, list):
                return []

            events = [DockerEvent.from_dict(e) for e in events_data]

            # Filter by type
            if event_types:
                events = [e for e in events if e.event_type in event_types]

            # Filter by time
            if since_timestamp:
                events = [e for e in events if e.timestamp > since_timestamp]

            # Sort and limit
            events.sort(key=lambda e: e.timestamp, reverse=True)
            return events[:limit]

        except Exception as e:
            logger.debug(f"Failed to get recent events: {e}")
            return []

    # =========================================================================
    # REQUEST-RESPONSE PATTERN
    # =========================================================================

    async def send_request(
        self,
        request_type: DockerEventType,
        payload: Dict[str, Any],
        timeout: float = 30.0
    ) -> Optional[Dict[str, Any]]:
        """
        Send request to Docker owner (JARVIS_AGENT) and wait for response.

        Used by JARVIS_PRIME and REACTOR_CORE to request Docker operations.
        """
        if not self.config.enabled:
            return None

        request_id = str(uuid.uuid4())

        request = {
            'request_id': request_id,
            'request_type': request_type.value,
            'source': self.component.value,
            'timestamp': time.time(),
            'payload': payload,
        }

        # Write request file
        request_file = self._requests_dir / f'{request_id}.json'
        await self._atomic_write(request_file, request)

        # Wait for response
        response_file = self._responses_dir / f'{request_id}.json'

        try:
            start_time = time.time()
            while (time.time() - start_time) < timeout:
                if response_file.exists():
                    try:
                        response = json.loads(response_file.read_text())
                        response_file.unlink()  # Clean up
                        return response
                    except Exception:
                        pass

                await asyncio.sleep(0.5)

            logger.warning(f"Request {request_id} timed out after {timeout}s")
            return None

        finally:
            # Clean up request file
            try:
                if request_file.exists():
                    request_file.unlink()
            except Exception:
                pass

    async def _watch_requests(self):
        """Watch for incoming requests (runs on JARVIS_AGENT only)"""
        while self._running:
            try:
                # Check for request files
                for request_file in self._requests_dir.glob('*.json'):
                    try:
                        request = json.loads(request_file.read_text())
                        request_id = request.get('request_id')
                        request_type = request.get('request_type')

                        if request_id and request_type:
                            # Handle request
                            response = await self._handle_request(request)

                            # Write response
                            response_file = self._responses_dir / f'{request_id}.json'
                            await self._atomic_write(response_file, response)

                            # Clean up request
                            request_file.unlink()

                    except Exception as e:
                        logger.debug(f"Error processing request: {e}")
                        try:
                            request_file.unlink()
                        except Exception:
                            pass

                await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Request watcher error: {e}")
                await asyncio.sleep(1.0)

    async def _handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming request"""
        request_type = request.get('request_type', '')
        payload = request.get('payload', {})

        try:
            if request_type == DockerEventType.REQUEST_STATUS.value:
                state = await self.get_state()
                return {
                    'success': True,
                    'state': state.to_dict() if state else None,
                }

            elif request_type == DockerEventType.REQUEST_HEALTH.value:
                state = await self.get_state()
                return {
                    'success': True,
                    'healthy': state.healthy if state else False,
                    'health_score': state.health_score if state else 0.0,
                }

            elif request_type == DockerEventType.REQUEST_START.value:
                # This would be handled by the Docker manager
                # We just acknowledge the request here
                return {
                    'success': True,
                    'message': 'Start request acknowledged',
                    'note': 'Docker manager will handle startup',
                }

            elif request_type == DockerEventType.REQUEST_STOP.value:
                return {
                    'success': True,
                    'message': 'Stop request acknowledged',
                }

            else:
                return {
                    'success': False,
                    'error': f'Unknown request type: {request_type}',
                }

        except Exception as e:
            return {
                'success': False,
                'error': str(e),
            }

    # =========================================================================
    # HEARTBEAT
    # =========================================================================

    async def _heartbeat_loop(self):
        """Emit heartbeat periodically"""
        while self._running:
            try:
                heartbeat = {
                    'component': self.component.value,
                    'timestamp': time.time(),
                    'state': self._current_state.to_dict() if self._current_state else None,
                }

                await self._atomic_write(self._heartbeat_file, heartbeat)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Heartbeat error: {e}")

            await asyncio.sleep(self.config.heartbeat_interval_seconds)

    async def check_component_health(self, component: ComponentType) -> bool:
        """Check if a component is alive via heartbeat"""
        heartbeat_file = self.config.docker_state_dir / f'heartbeat_{component.value}.json'

        try:
            if not heartbeat_file.exists():
                return False

            data = json.loads(heartbeat_file.read_text())
            last_heartbeat = data.get('timestamp', 0)

            age = time.time() - last_heartbeat
            return age < self.config.heartbeat_timeout_seconds

        except Exception:
            return False

    # =========================================================================
    # UTILITIES
    # =========================================================================

    async def _atomic_write(self, path: Path, data: Any):
        """Atomic file write (write to temp, then rename)"""
        temp_path = path.with_suffix('.tmp')
        try:
            temp_path.write_text(json.dumps(data, indent=2, default=str))
            temp_path.replace(path)
        except Exception as e:
            logger.debug(f"Atomic write failed: {e}")
            try:
                temp_path.unlink()
            except Exception:
                pass
            raise


# =============================================================================
# HELPER FUNCTIONS FOR OTHER REPOS
# =============================================================================

class JarvisPrimeDockerClient:
    """
    Docker client for JARVIS-Prime.

    Provides simplified interface for checking Docker status
    and requesting Docker operations.
    """

    def __init__(self, config: Optional[CrossRepoConfig] = None):
        self.coordinator = DockerCrossRepoCoordinator(
            component=ComponentType.JARVIS_PRIME,
            config=config
        )

    async def start(self):
        """Start the client"""
        await self.coordinator.start()

    async def stop(self):
        """Stop the client"""
        await self.coordinator.stop()

    async def is_docker_available(self) -> bool:
        """Check if Docker is available for container-based inference"""
        state = await self.coordinator.get_state()
        return state is not None and state.healthy

    async def get_docker_status(self) -> Optional[DockerState]:
        """Get current Docker status"""
        return await self.coordinator.get_state()

    async def request_docker_start(self, timeout: float = 60.0) -> bool:
        """Request Docker to be started"""
        response = await self.coordinator.send_request(
            DockerEventType.REQUEST_START,
            {},
            timeout=timeout
        )
        return response is not None and response.get('success', False)

    async def subscribe_to_docker_events(
        self,
        handler: Callable[[DockerEvent], Any]
    ):
        """Subscribe to Docker state changes"""
        await self.coordinator.subscribe(
            [
                DockerEventType.STARTED,
                DockerEventType.STOPPED,
                DockerEventType.HEALTHY,
                DockerEventType.UNHEALTHY,
            ],
            handler
        )


class ReactorCoreDockerClient:
    """
    Docker client for Reactor-Core.

    Provides interface for checking Docker availability
    before scheduling training jobs that need Docker.
    """

    def __init__(self, config: Optional[CrossRepoConfig] = None):
        self.coordinator = DockerCrossRepoCoordinator(
            component=ComponentType.REACTOR_CORE,
            config=config
        )

    async def start(self):
        """Start the client"""
        await self.coordinator.start()

    async def stop(self):
        """Stop the client"""
        await self.coordinator.stop()

    async def is_docker_available(self) -> bool:
        """Check if Docker is available for training containers"""
        state = await self.coordinator.get_state()
        return state is not None and state.healthy

    async def get_resource_status(self) -> Dict[str, Any]:
        """Get Docker resource status for training planning"""
        state = await self.coordinator.get_state()
        if state:
            return state.resource_status
        return {}

    async def wait_for_docker(self, timeout: float = 120.0) -> bool:
        """Wait for Docker to become available"""
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            if await self.is_docker_available():
                return True
            await asyncio.sleep(2.0)

        return False

    async def subscribe_to_docker_events(
        self,
        handler: Callable[[DockerEvent], Any]
    ):
        """Subscribe to Docker state changes for job scheduling"""
        await self.coordinator.subscribe(
            [
                DockerEventType.STARTED,
                DockerEventType.STOPPED,
                DockerEventType.HEALTHY,
                DockerEventType.UNHEALTHY,
                DockerEventType.RESOURCE_WARNING,
                DockerEventType.RESOURCE_CRITICAL,
            ],
            handler
        )


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_coordinator_instance: Optional[DockerCrossRepoCoordinator] = None


def get_docker_coordinator(
    component: ComponentType = ComponentType.JARVIS_AGENT
) -> DockerCrossRepoCoordinator:
    """Get or create the Docker cross-repo coordinator singleton"""
    global _coordinator_instance

    if _coordinator_instance is None:
        _coordinator_instance = DockerCrossRepoCoordinator(component=component)

    return _coordinator_instance


async def initialize_docker_coordinator(
    component: ComponentType = ComponentType.JARVIS_AGENT
) -> DockerCrossRepoCoordinator:
    """Initialize and start the Docker coordinator"""
    coordinator = get_docker_coordinator(component)
    await coordinator.start()
    return coordinator
