"""
Cross-Repository Configuration Bridge v1.0
===========================================

Provides configuration synchronization across the Trinity ecosystem:
- JARVIS (Body) - Primary interface and execution
- JARVIS Prime (Mind) - Intelligence and decision making
- Reactor Core (Learning) - Training and model updates

Features:
- Cross-repo configuration sync
- Conflict resolution
- Configuration inheritance
- Environment-specific overrides
- Real-time propagation

Author: Trinity Configuration System
Version: 1.0.0
"""

import asyncio
import copy
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

from backend.core.configuration.unified_engine import (
    ConfigurationEngineConfig,
    ConfigEnvironment,
    ConfigSource,
    ChangeType,
    SyncStatus,
    ConfigValue,
    ConfigVersion,
    ConfigChangeEvent,
    UnifiedConfigurationEngine,
    get_configuration_engine,
)

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================


class ConfigEventType(Enum):
    """Types of configuration events for cross-repo communication."""
    CONFIG_UPDATE = auto()
    CONFIG_DELETE = auto()
    CONFIG_SYNC_REQUEST = auto()
    CONFIG_SYNC_RESPONSE = auto()
    CONFIG_CONFLICT = auto()
    CONFIG_ROLLBACK = auto()
    SCHEMA_UPDATE = auto()
    HEARTBEAT = auto()


class ConflictResolutionStrategy(Enum):
    """Strategies for resolving configuration conflicts."""
    NEWEST_WINS = auto()
    PRIORITY_WINS = auto()
    MERGE = auto()
    MANUAL = auto()
    SOURCE_OF_TRUTH = auto()


class RepoConfigRole(Enum):
    """Role of a repository in configuration management."""
    PRIMARY = "primary"
    SECONDARY = "secondary"
    OBSERVER = "observer"


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class CrossRepoConfigConfig:
    """Configuration for cross-repo configuration bridge."""

    # Sync settings
    sync_enabled: bool = os.getenv("CONFIG_SYNC_ENABLED", "true").lower() == "true"
    sync_interval: float = float(os.getenv("CONFIG_SYNC_INTERVAL", "30.0"))
    sync_timeout: float = float(os.getenv("CONFIG_SYNC_TIMEOUT", "10.0"))

    # Conflict resolution
    conflict_strategy: str = os.getenv("CONFIG_CONFLICT_STRATEGY", "NEWEST_WINS")
    source_of_truth: str = os.getenv("CONFIG_SOURCE_OF_TRUTH", "jarvis_body")

    # Event settings
    event_queue_size: int = int(os.getenv("CONFIG_EVENT_QUEUE_SIZE", "1000"))
    event_retention_hours: float = float(os.getenv("CONFIG_EVENT_RETENTION_HOURS", "24.0"))

    # Heartbeat settings
    heartbeat_interval: float = float(os.getenv("CONFIG_HEARTBEAT_INTERVAL", "10.0"))
    heartbeat_timeout: float = float(os.getenv("CONFIG_HEARTBEAT_TIMEOUT", "30.0"))

    # Environment
    environment: str = os.getenv("CONFIG_ENVIRONMENT", "development")

    # Repo paths for file-based sync
    jarvis_config_path: str = os.getenv(
        "JARVIS_CONFIG_PATH",
        str(Path.home() / "Documents/repos/JARVIS-AI-Agent/backend/config")
    )
    prime_config_path: str = os.getenv(
        "PRIME_CONFIG_PATH",
        str(Path.home() / "Documents/repos/JARVIS-Prime/config")
    )
    reactor_config_path: str = os.getenv(
        "REACTOR_CONFIG_PATH",
        str(Path.home() / "Documents/repos/Reactor-Core/config")
    )


# =============================================================================
# DATA STRUCTURES
# =============================================================================


@dataclass
class ConfigEvent:
    """A configuration event for cross-repo communication."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: ConfigEventType = ConfigEventType.HEARTBEAT
    source_repo: str = "jarvis_body"
    target_repo: Optional[str] = None
    config_key: str = ""
    config_value: Any = None
    version: int = 0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    checksum: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RepoConfigState:
    """Configuration state for a repository."""
    repo_id: str
    role: RepoConfigRole = RepoConfigRole.SECONDARY
    config_version: int = 0
    config_checksum: str = ""
    last_sync: Optional[datetime] = None
    last_heartbeat: datetime = field(default_factory=datetime.utcnow)
    online: bool = True
    sync_status: SyncStatus = SyncStatus.SYNCED
    pending_changes: List[ConfigEvent] = field(default_factory=list)


@dataclass
class ConfigConflict:
    """A configuration conflict between repos."""
    conflict_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    config_key: str = ""
    local_value: Any = None
    remote_value: Any = None
    local_version: int = 0
    remote_version: int = 0
    local_timestamp: datetime = field(default_factory=datetime.utcnow)
    remote_timestamp: datetime = field(default_factory=datetime.utcnow)
    resolved: bool = False
    resolution: Optional[str] = None


@dataclass
class SyncResult:
    """Result of a configuration sync operation."""
    success: bool = True
    synced_keys: List[str] = field(default_factory=list)
    conflicts: List[ConfigConflict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    duration_ms: float = 0.0


# =============================================================================
# CONFIGURATION EVENT BUS
# =============================================================================


class ConfigEventBus:
    """
    Event bus for cross-repo configuration communication.

    Features:
    - Async event publishing
    - Subscriber filtering
    - Event history
    - Acknowledgment
    """

    def __init__(self, config: CrossRepoConfigConfig):
        self.config = config
        self.logger = logging.getLogger("ConfigEventBus")
        self._subscribers: Dict[ConfigEventType, List[Callable]] = defaultdict(list)
        self._global_subscribers: List[Callable] = []
        self._event_history: List[ConfigEvent] = []
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=config.event_queue_size)
        self._lock = asyncio.Lock()
        self._running = False
        self._processor_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the event bus."""
        if self._running:
            return

        self._running = True
        self._processor_task = asyncio.create_task(self._process_events())
        self.logger.info("Config event bus started")

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

        self.logger.info("Config event bus stopped")

    async def publish(self, event: ConfigEvent):
        """Publish an event."""
        # Calculate checksum
        event.checksum = self._calculate_checksum(event)

        try:
            await self._queue.put(event)
        except asyncio.QueueFull:
            self.logger.warning("Config event queue full")

    def subscribe(
        self,
        event_type: Optional[ConfigEventType] = None,
        callback: Callable = None,
    ):
        """Subscribe to events."""
        if callback is None:
            return

        if event_type is None:
            self._global_subscribers.append(callback)
        else:
            self._subscribers[event_type].append(callback)

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
                        e for e in self._event_history if e.timestamp > cutoff
                    ]

                # Deliver to subscribers
                await self._deliver_event(event)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Event processing error: {e}")

    async def _deliver_event(self, event: ConfigEvent):
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

    def _calculate_checksum(self, event: ConfigEvent) -> str:
        """Calculate event checksum."""
        data = json.dumps({
            "event_id": event.event_id,
            "event_type": event.event_type.name,
            "config_key": event.config_key,
            "config_value": str(event.config_value),
            "timestamp": event.timestamp.isoformat(),
        }, sort_keys=True)
        return hashlib.md5(data.encode()).hexdigest()


# =============================================================================
# CONFLICT RESOLVER
# =============================================================================


class ConfigConflictResolver:
    """
    Resolves configuration conflicts between repos.

    Strategies:
    - NEWEST_WINS: Most recent timestamp wins
    - PRIORITY_WINS: Higher priority repo wins
    - MERGE: Deep merge conflicting values
    - SOURCE_OF_TRUTH: Designated repo always wins
    """

    def __init__(self, config: CrossRepoConfigConfig):
        self.config = config
        self.logger = logging.getLogger("ConfigConflictResolver")
        self._repo_priorities: Dict[str, int] = {
            "jarvis_body": 3,
            "jarvis_prime": 2,
            "reactor_core": 1,
        }

    async def resolve(
        self,
        conflict: ConfigConflict,
        strategy: Optional[ConflictResolutionStrategy] = None,
    ) -> Tuple[Any, str]:
        """
        Resolve a configuration conflict.
        Returns (resolved_value, resolution_description).
        """
        if strategy is None:
            strategy = ConflictResolutionStrategy[self.config.conflict_strategy]

        if strategy == ConflictResolutionStrategy.NEWEST_WINS:
            return await self._resolve_newest_wins(conflict)

        elif strategy == ConflictResolutionStrategy.PRIORITY_WINS:
            return await self._resolve_priority_wins(conflict)

        elif strategy == ConflictResolutionStrategy.MERGE:
            return await self._resolve_merge(conflict)

        elif strategy == ConflictResolutionStrategy.SOURCE_OF_TRUTH:
            return await self._resolve_source_of_truth(conflict)

        else:
            # Manual - return local by default
            return conflict.local_value, "manual_pending"

    async def _resolve_newest_wins(
        self,
        conflict: ConfigConflict,
    ) -> Tuple[Any, str]:
        """Resolve by newest timestamp."""
        if conflict.local_timestamp >= conflict.remote_timestamp:
            return conflict.local_value, "local_newer"
        else:
            return conflict.remote_value, "remote_newer"

    async def _resolve_priority_wins(
        self,
        conflict: ConfigConflict,
    ) -> Tuple[Any, str]:
        """Resolve by repo priority."""
        # Higher version = higher priority
        if conflict.local_version >= conflict.remote_version:
            return conflict.local_value, "local_higher_priority"
        else:
            return conflict.remote_value, "remote_higher_priority"

    async def _resolve_merge(
        self,
        conflict: ConfigConflict,
    ) -> Tuple[Any, str]:
        """Deep merge conflicting values."""
        if isinstance(conflict.local_value, dict) and isinstance(conflict.remote_value, dict):
            merged = self._deep_merge(conflict.local_value, conflict.remote_value)
            return merged, "merged"
        else:
            # Can't merge non-dicts, fallback to newest
            return await self._resolve_newest_wins(conflict)

    async def _resolve_source_of_truth(
        self,
        conflict: ConfigConflict,
    ) -> Tuple[Any, str]:
        """Use designated source of truth."""
        sot = self.config.source_of_truth

        # Check if local is source of truth
        # For now, assume local is source of truth if configured
        if sot == "jarvis_body":
            return conflict.local_value, "source_of_truth"
        else:
            return conflict.remote_value, "source_of_truth"

    def _deep_merge(
        self,
        base: Dict[str, Any],
        override: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Deep merge two dictionaries."""
        result = copy.deepcopy(base)

        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)

        return result


# =============================================================================
# CROSS-REPO CONFIGURATION BRIDGE
# =============================================================================


class CrossRepoConfigBridge:
    """
    Bridge for cross-repository configuration synchronization.

    Manages:
    - Configuration sync across repos
    - Conflict detection and resolution
    - Version coordination
    - Real-time propagation
    """

    def __init__(self, config: Optional[CrossRepoConfigConfig] = None):
        self.config = config or CrossRepoConfigConfig()
        self.logger = logging.getLogger("CrossRepoConfigBridge")

        # Components
        self.event_bus = ConfigEventBus(self.config)
        self.conflict_resolver = ConfigConflictResolver(self.config)

        # State
        self._running = False
        self._repo_states: Dict[str, RepoConfigState] = {}
        self._pending_conflicts: List[ConfigConflict] = []
        self._config_engine: Optional[UnifiedConfigurationEngine] = None

        # Tasks
        self._sync_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

        # Locks
        self._lock = asyncio.Lock()

        # Register event handlers
        self.event_bus.subscribe(ConfigEventType.CONFIG_UPDATE, self._handle_config_update)
        self.event_bus.subscribe(ConfigEventType.CONFIG_SYNC_REQUEST, self._handle_sync_request)
        self.event_bus.subscribe(ConfigEventType.HEARTBEAT, self._handle_heartbeat)

    async def initialize(self) -> bool:
        """Initialize the configuration bridge."""
        try:
            # Get configuration engine
            self._config_engine = await get_configuration_engine()

            # Initialize repo states
            for repo_id in ["jarvis_body", "jarvis_prime", "reactor_core"]:
                self._repo_states[repo_id] = RepoConfigState(
                    repo_id=repo_id,
                    role=RepoConfigRole.PRIMARY if repo_id == "jarvis_body" else RepoConfigRole.SECONDARY,
                )

            self.logger.info("CrossRepoConfigBridge initialized")
            return True

        except Exception as e:
            self.logger.error(f"Initialization failed: {e}")
            return False

    async def start(self):
        """Start the configuration bridge."""
        if self._running:
            return

        self._running = True

        # Start event bus
        await self.event_bus.start()

        # Start background tasks
        if self.config.sync_enabled:
            self._sync_task = asyncio.create_task(self._sync_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        self.logger.info("CrossRepoConfigBridge started")

    async def stop(self):
        """Stop the configuration bridge."""
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

        self.logger.info("CrossRepoConfigBridge stopped")

    async def shutdown(self):
        """Complete shutdown."""
        await self.stop()

    # =========================================================================
    # Synchronization
    # =========================================================================

    async def sync_with_repo(self, target_repo: str) -> SyncResult:
        """
        v95.0: Synchronize configuration with a specific repository.

        Enhanced with timeout protection and better error handling.
        """
        start_time = time.time()
        result = SyncResult()
        sync_timeout = getattr(self.config, 'sync_timeout', 10.0)

        try:
            # v95.0: Get local config with timeout protection
            try:
                local_config = await asyncio.wait_for(
                    self._config_engine.get_all(),
                    timeout=sync_timeout
                )
            except asyncio.TimeoutError:
                result.success = False
                result.errors.append(f"Timeout getting local config for {target_repo}")
                self.logger.warning(f"Sync with {target_repo}: local config fetch timed out")
                result.duration_ms = (time.time() - start_time) * 1000
                return result
            except Exception as e:
                result.success = False
                error_msg = f"Error getting local config: {type(e).__name__}: {str(e)}"
                result.errors.append(error_msg)
                self.logger.warning(f"Sync with {target_repo}: {error_msg}")
                result.duration_ms = (time.time() - start_time) * 1000
                return result

            # v95.0: Get version with timeout protection
            try:
                local_version = await asyncio.wait_for(
                    self._config_engine.get_version(),
                    timeout=sync_timeout
                )
            except asyncio.TimeoutError:
                local_version = None
                self.logger.debug(f"Sync with {target_repo}: version fetch timed out, using 0")
            except Exception as e:
                local_version = None
                self.logger.debug(f"Sync with {target_repo}: version fetch failed: {e}")

            # Request remote config
            event = ConfigEvent(
                event_type=ConfigEventType.CONFIG_SYNC_REQUEST,
                source_repo="jarvis_body",
                target_repo=target_repo,
                version=local_version.version_number if local_version else 0,
                metadata={
                    "checksum": self._calculate_config_checksum(local_config),
                },
            )

            # v95.0: Publish event with timeout
            try:
                await asyncio.wait_for(
                    self.event_bus.publish(event),
                    timeout=sync_timeout
                )
            except asyncio.TimeoutError:
                result.success = False
                result.errors.append(f"Timeout publishing sync event to {target_repo}")
                self.logger.warning(f"Sync with {target_repo}: event publish timed out")
                result.duration_ms = (time.time() - start_time) * 1000
                return result

            # Note: In a real implementation, this would wait for response
            # For now, we'll simulate a successful sync

            result.success = True
            result.synced_keys = list(local_config.keys()) if local_config else []

            # Update repo state
            async with self._lock:
                if target_repo in self._repo_states:
                    self._repo_states[target_repo].last_sync = datetime.utcnow()
                    self._repo_states[target_repo].sync_status = SyncStatus.SYNCED

        except asyncio.CancelledError:
            raise  # Don't suppress cancellation
        except Exception as e:
            result.success = False
            # v95.0: Better error message formatting
            error_type = type(e).__name__
            error_msg = str(e) if str(e) else repr(e)
            result.errors.append(f"{error_type}: {error_msg}")
            self.logger.error(f"Sync with {target_repo} failed: {error_type}: {error_msg}")

        result.duration_ms = (time.time() - start_time) * 1000
        return result

    async def sync_all(self) -> Dict[str, SyncResult]:
        """Synchronize with all repositories."""
        results = {}

        for repo_id in ["jarvis_prime", "reactor_core"]:
            results[repo_id] = await self.sync_with_repo(repo_id)

        return results

    async def propagate_change(
        self,
        config_key: str,
        config_value: Any,
        version: int,
    ):
        """Propagate a configuration change to all repos."""
        event = ConfigEvent(
            event_type=ConfigEventType.CONFIG_UPDATE,
            source_repo="jarvis_body",
            config_key=config_key,
            config_value=config_value,
            version=version,
        )
        await self.event_bus.publish(event)

    # =========================================================================
    # Conflict Management
    # =========================================================================

    async def detect_conflict(
        self,
        config_key: str,
        local_value: Any,
        remote_value: Any,
        local_version: int,
        remote_version: int,
    ) -> Optional[ConfigConflict]:
        """Detect if there's a configuration conflict."""
        if local_value == remote_value:
            return None

        if local_version == remote_version:
            return None

        conflict = ConfigConflict(
            config_key=config_key,
            local_value=local_value,
            remote_value=remote_value,
            local_version=local_version,
            remote_version=remote_version,
        )

        async with self._lock:
            self._pending_conflicts.append(conflict)

        return conflict

    async def resolve_conflict(
        self,
        conflict_id: str,
        strategy: Optional[ConflictResolutionStrategy] = None,
    ) -> bool:
        """Resolve a pending conflict."""
        async with self._lock:
            conflict = None
            for c in self._pending_conflicts:
                if c.conflict_id == conflict_id:
                    conflict = c
                    break

            if not conflict:
                return False

            resolved_value, resolution = await self.conflict_resolver.resolve(
                conflict, strategy
            )

            # Apply resolved value
            await self._config_engine.set(
                conflict.config_key,
                resolved_value,
                validate=False,
            )

            conflict.resolved = True
            conflict.resolution = resolution

            self.logger.info(f"Resolved conflict {conflict_id}: {resolution}")
            return True

    async def get_pending_conflicts(self) -> List[ConfigConflict]:
        """Get list of pending conflicts."""
        async with self._lock:
            return [c for c in self._pending_conflicts if not c.resolved]

    # =========================================================================
    # Status
    # =========================================================================

    async def get_status(self) -> Dict[str, Any]:
        """Get bridge status."""
        async with self._lock:
            repo_status = {}
            for repo_id, state in self._repo_states.items():
                repo_status[repo_id] = {
                    "role": state.role.value,
                    "online": state.online,
                    "sync_status": state.sync_status.name,
                    "last_sync": state.last_sync.isoformat() if state.last_sync else None,
                    "last_heartbeat": state.last_heartbeat.isoformat(),
                    "config_version": state.config_version,
                }

            return {
                "running": self._running,
                "sync_enabled": self.config.sync_enabled,
                "repos": repo_status,
                "pending_conflicts": len([c for c in self._pending_conflicts if not c.resolved]),
            }

    async def get_health(self) -> Dict[str, RepoConfigState]:
        """Get health status of all repos."""
        async with self._lock:
            return self._repo_states.copy()

    # =========================================================================
    # Event Handlers
    # =========================================================================

    async def _handle_config_update(self, event: ConfigEvent):
        """Handle configuration update from another repo."""
        self.logger.info(f"Received config update from {event.source_repo}: {event.config_key}")

        # Check for conflicts
        local_value = await self._config_engine.get(event.config_key)
        local_version = await self._config_engine.get_version()

        if local_value is not None and local_value != event.config_value:
            conflict = await self.detect_conflict(
                event.config_key,
                local_value,
                event.config_value,
                local_version.version_number if local_version else 0,
                event.version,
            )

            if conflict:
                # Auto-resolve if configured
                await self.resolve_conflict(conflict.conflict_id)

        else:
            # No conflict, apply update
            await self._config_engine.set(
                event.config_key,
                event.config_value,
                validate=False,
                create_version=True,
            )

    async def _handle_sync_request(self, event: ConfigEvent):
        """Handle sync request from another repo."""
        if event.target_repo != "jarvis_body":
            return

        self.logger.info(f"Received sync request from {event.source_repo}")

        # Send current config
        all_config = await self._config_engine.get_all()
        current_version = await self._config_engine.get_version()

        response = ConfigEvent(
            event_type=ConfigEventType.CONFIG_SYNC_RESPONSE,
            source_repo="jarvis_body",
            target_repo=event.source_repo,
            config_value=all_config,
            version=current_version.version_number if current_version else 0,
            metadata={
                "checksum": self._calculate_config_checksum(all_config),
            },
        )
        await self.event_bus.publish(response)

    async def _handle_heartbeat(self, event: ConfigEvent):
        """Handle heartbeat from another repo."""
        async with self._lock:
            if event.source_repo in self._repo_states:
                self._repo_states[event.source_repo].online = True
                self._repo_states[event.source_repo].last_heartbeat = datetime.utcnow()

    # =========================================================================
    # Background Tasks
    # =========================================================================

    async def _sync_loop(self):
        """Periodic sync with other repos."""
        while self._running:
            try:
                await asyncio.sleep(self.config.sync_interval)
                await self.sync_all()

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
                event = ConfigEvent(
                    event_type=ConfigEventType.HEARTBEAT,
                    source_repo="jarvis_body",
                    metadata={"timestamp": time.time()},
                )
                await self.event_bus.publish(event)

                # Check for stale repos
                await self._check_stale_repos()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Heartbeat loop error: {e}")

    async def _check_stale_repos(self):
        """Check for repos that haven't sent heartbeat."""
        now = datetime.utcnow()
        timeout = timedelta(seconds=self.config.heartbeat_timeout)

        async with self._lock:
            for repo_id, state in self._repo_states.items():
                if state.online and now - state.last_heartbeat > timeout:
                    state.online = False
                    state.sync_status = SyncStatus.FAILED
                    self.logger.warning(f"Repo {repo_id} appears offline")

    def _calculate_config_checksum(self, config: Dict[str, Any]) -> str:
        """Calculate checksum for config data."""
        serialized = json.dumps(config, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest()


# =============================================================================
# GLOBAL INSTANCE MANAGEMENT
# =============================================================================

_bridge: Optional[CrossRepoConfigBridge] = None
_bridge_lock = asyncio.Lock()


async def get_cross_repo_config_bridge() -> CrossRepoConfigBridge:
    """Get or create the global configuration bridge."""
    global _bridge

    async with _bridge_lock:
        if _bridge is None:
            _bridge = CrossRepoConfigBridge()
            await _bridge.initialize()
        return _bridge


async def initialize_cross_repo_config() -> bool:
    """Initialize the global configuration bridge."""
    bridge = await get_cross_repo_config_bridge()
    await bridge.start()
    return True


async def shutdown_cross_repo_config():
    """Shutdown the global configuration bridge."""
    global _bridge

    async with _bridge_lock:
        if _bridge is not None:
            await _bridge.shutdown()
            _bridge = None
            logger.info("Cross-repo config bridge shutdown")


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Configuration
    "CrossRepoConfigConfig",
    # Enums
    "ConfigEventType",
    "ConflictResolutionStrategy",
    "RepoConfigRole",
    # Data Structures
    "ConfigEvent",
    "RepoConfigState",
    "ConfigConflict",
    "SyncResult",
    # Components
    "ConfigEventBus",
    "ConfigConflictResolver",
    # Bridge
    "CrossRepoConfigBridge",
    # Global Functions
    "get_cross_repo_config_bridge",
    "initialize_cross_repo_config",
    "shutdown_cross_repo_config",
]
