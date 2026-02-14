"""
Cross-Repo State Initialization for JARVIS Enhanced VBIA v6.4
=================================================================

Centralized state initialization module that sets up the cross-repository
communication infrastructure for:
- JARVIS (Main) - Voice Biometric Intelligence with Visual Security
- JARVIS Prime - Task delegation and distributed processing
- Reactor Core - Event analytics and threat monitoring

This module creates and manages the ~/.jarvis/cross_repo/ directory structure
for real-time event sharing, state synchronization, and request/response flows.

Features:
- Async parallel initialization
- Environment-driven configuration (no hardcoding)
- Automatic directory structure creation
- State file versioning and migration
- Health check and heartbeat management
- Event emission and consumption APIs
- Thread-safe file operations with proper locking
- v6.3: Coordinated multi-repo startup with dependency ordering
- v6.3: Health probing for external repos (JARVIS-Prime, Reactor-Core)
- v6.3: Timeout handling with graceful degradation
- v6.3: Cross-repo connection validation
- v6.4: Distributed lock manager with automatic expiration
- v6.4: Stale lock detection and cleanup
- v6.4: Deadlock prevention with TTL-based locks

Architecture:
    ┌──────────────────────────────────────────────────────────┐
    │  ~/.jarvis/cross_repo/                                   │
    │  ├── vbia_events.json       (JARVIS → All)              │
    │  ├── vbia_requests.json     (Prime/Reactor → JARVIS)    │
    │  ├── vbia_results.json      (JARVIS → Prime/Reactor)    │
    │  ├── vbia_state.json        (JARVIS state broadcast)    │
    │  ├── prime_state.json       (JARVIS Prime status)       │
    │  ├── reactor_state.json     (Reactor Core status)       │
    │  ├── heartbeat.json         (Cross-repo health)         │
    │  └── locks/                 (Distributed lock files)    │
    │      ├── vbia_events.lock   (Lock with TTL metadata)    │
    │      ├── prime_state.lock                               │
    │      └── reactor_state.lock                             │
    └──────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 6.4.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set
from uuid import uuid4

import aiofiles
import aiofiles.os

# v6.4: Import distributed lock manager for cross-process locking
from backend.core.distributed_lock_manager import get_lock_manager, DistributedLockManager
from backend.core.robust_file_lock import RobustFileLock

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration (Environment-Driven, No Hardcoding)
# =============================================================================

def _get_env(key: str, default: str = "") -> str:
    """Get environment variable with default."""
    return os.environ.get(key, default)


def _get_env_bool(key: str, default: bool = False) -> bool:
    """Get boolean environment variable."""
    return _get_env(key, str(default)).lower() in ("true", "1", "yes", "on")


def _get_env_int(key: str, default: int) -> int:
    """Get integer environment variable."""
    try:
        return int(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_float(key: str, default: float) -> float:
    """Get float environment variable."""
    try:
        return float(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_path(key: str, default: str) -> Path:
    """Get path environment variable with expansion."""
    return Path(os.path.expanduser(_get_env(key, default)))


@dataclass
class CrossRepoStateConfig:
    """Configuration for cross-repo state management."""
    # Base directory for cross-repo state
    base_dir: Path = field(
        default_factory=lambda: _get_env_path(
            "JARVIS_CROSS_REPO_DIR",
            "~/.jarvis/cross_repo"
        )
    )

    # Event file settings
    max_events_per_file: int = field(
        default_factory=lambda: _get_env_int("JARVIS_MAX_EVENTS_PER_FILE", 1000)
    )
    event_rotation_enabled: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_EVENT_ROTATION", True)
    )

    # State file settings
    state_update_interval_seconds: float = field(
        default_factory=lambda: _get_env_float("JARVIS_STATE_UPDATE_INTERVAL", 5.0)
    )

    # Heartbeat settings
    heartbeat_interval_seconds: float = field(
        default_factory=lambda: _get_env_float("JARVIS_HEARTBEAT_INTERVAL", 10.0)
    )
    heartbeat_timeout_seconds: float = field(
        default_factory=lambda: _get_env_float("JARVIS_HEARTBEAT_TIMEOUT", 30.0)
    )

    # Repository paths
    jarvis_repo: Path = field(
        default_factory=lambda: _get_env_path(
            "JARVIS_REPO_PATH",
            "~/Documents/repos/JARVIS-AI-Agent"
        )
    )
    jarvis_prime_repo: Path = field(
        default_factory=lambda: _get_env_path(
            "JARVIS_PRIME_REPO_PATH",
            "~/Documents/repos/jarvis-prime"
        )
    )
    reactor_core_repo: Path = field(
        default_factory=lambda: _get_env_path(
            "REACTOR_CORE_REPO_PATH",
            "~/Documents/repos/reactor-core"
        )
    )

    # Visual security settings
    visual_security_enabled: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_VISUAL_SECURITY_ENABLED", True)
    )
    visual_security_mode: str = field(
        default_factory=lambda: _get_env("JARVIS_VISUAL_SECURITY_MODE", "omniparser")
    )

    # v6.3: Coordinated multi-repo startup settings
    coordinated_startup_enabled: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_COORDINATED_STARTUP_ENABLED", True)
    )
    repo_startup_timeout_seconds: float = field(
        default_factory=lambda: _get_env_float("JARVIS_REPO_STARTUP_TIMEOUT", 30.0)
    )
    repo_health_probe_timeout_seconds: float = field(
        default_factory=lambda: _get_env_float("JARVIS_REPO_HEALTH_PROBE_TIMEOUT", 5.0)
    )
    repo_health_retry_count: int = field(
        default_factory=lambda: _get_env_int("JARVIS_REPO_HEALTH_RETRY_COUNT", 3)
    )
    repo_health_retry_delay_seconds: float = field(
        default_factory=lambda: _get_env_float("JARVIS_REPO_HEALTH_RETRY_DELAY", 2.0)
    )
    graceful_degradation_enabled: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_GRACEFUL_DEGRADATION_ENABLED", True)
    )


# =============================================================================
# Enums
# =============================================================================

class RepoType(str, Enum):
    """Type of repository in the cross-repo system."""
    JARVIS = "jarvis"
    JARVIS_PRIME = "jarvis_prime"
    REACTOR_CORE = "reactor_core"


class EventType(str, Enum):
    """Types of VBIA events."""
    # Visual security events
    VISUAL_SECURITY_ANALYSIS = "vbia_visual_security"
    VISUAL_THREAT_DETECTED = "vbia_visual_threat"
    VISUAL_SAFE_CONFIRMED = "vbia_visual_safe"

    # Authentication events
    AUTHENTICATION_STARTED = "vbia_auth_started"
    AUTHENTICATION_SUCCESS = "vbia_auth_success"
    AUTHENTICATION_FAILED = "vbia_auth_failed"

    # Evidence collection events
    EVIDENCE_COLLECTED = "vbia_evidence_collected"
    MULTI_FACTOR_FUSION = "vbia_multi_factor_fusion"

    # LangGraph reasoning events
    REASONING_STARTED = "vbia_reasoning_started"
    REASONING_THOUGHT = "vbia_reasoning_thought"
    REASONING_COMPLETED = "vbia_reasoning_completed"

    # Cost tracking events
    COST_TRACKED = "vbia_cost_tracked"
    PATTERN_LEARNED = "vbia_pattern_learned"

    # System events
    SYSTEM_READY = "vbia_system_ready"
    SYSTEM_ERROR = "vbia_system_error"

    # v113.0: Infrastructure dependency events (CloudSQL, proxies, etc.)
    CLOUDSQL_PROXY_STARTING = "infra_cloudsql_proxy_starting"
    CLOUDSQL_PROXY_READY = "infra_cloudsql_proxy_ready"
    CLOUDSQL_PROXY_FAILED = "infra_cloudsql_proxy_failed"
    CLOUDSQL_DB_CONNECTED = "infra_cloudsql_db_connected"
    CLOUDSQL_DB_DISCONNECTED = "infra_cloudsql_db_disconnected"
    DEPENDENCY_READY = "infra_dependency_ready"
    DEPENDENCY_NOT_READY = "infra_dependency_not_ready"

    # v114.0: Credential lifecycle events
    CREDENTIAL_VALIDATED = "infra_credential_validated"
    CREDENTIAL_INVALIDATED = "infra_credential_invalidated"
    CREDENTIAL_REFRESHED = "infra_credential_refreshed"
    CREDENTIAL_SOURCE_CHANGED = "infra_credential_source_changed"


class StateStatus(str, Enum):
    """Status of a repository's state."""
    INITIALIZING = "initializing"
    READY = "ready"
    ACTIVE = "active"
    DEGRADED = "degraded"
    ERROR = "error"
    OFFLINE = "offline"


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class VBIAEvent:
    """A VBIA event to be shared across repositories."""
    event_id: str = field(default_factory=lambda: uuid4().hex)
    event_type: EventType = EventType.SYSTEM_READY
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    source_repo: RepoType = RepoType.JARVIS
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None


@dataclass
class RepoState:
    """State of a repository in the cross-repo system."""
    repo_type: RepoType
    status: StateStatus
    last_update: str = field(default_factory=lambda: datetime.now().isoformat())
    last_heartbeat: str = field(default_factory=lambda: datetime.now().isoformat())
    version: str = "6.2.0"
    capabilities: Dict[str, bool] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


@dataclass
class VBIARequest:
    """A request to JARVIS VBIA system from another repository."""
    request_id: str = field(default_factory=lambda: uuid4().hex)
    source_repo: RepoType = RepoType.JARVIS_PRIME
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    request_type: str = "authenticate"  # authenticate, analyze_visual, check_threat
    payload: Dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    correlation_id: Optional[str] = None


@dataclass
class VBIAResult:
    """A result from JARVIS VBIA system to another repository."""
    result_id: str = field(default_factory=lambda: uuid4().hex)
    request_id: str = ""  # Corresponding request ID
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    success: bool = True
    payload: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    processing_time_ms: float = 0.0


@dataclass
class Heartbeat:
    """Heartbeat from a repository."""
    repo_type: RepoType
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    status: StateStatus = StateStatus.READY
    uptime_seconds: float = 0.0
    active_sessions: int = 0


# =============================================================================
# Cross-Repo State Initializer
# =============================================================================

class CrossRepoStateInitializer:
    """
    Initializes and manages cross-repository state for Enhanced VBIA v6.2.

    This class handles:
    - Directory structure creation
    - State file initialization
    - Event emission and consumption
    - Heartbeat management
    - State synchronization
    """

    def __init__(self, config: Optional[CrossRepoStateConfig] = None):
        self.config = config or CrossRepoStateConfig()
        self._initialized = False
        self._start_time = time.time()

        # State files
        self._state_files = {
            "vbia_events": self.config.base_dir / "vbia_events.json",
            "vbia_requests": self.config.base_dir / "vbia_requests.json",
            "vbia_results": self.config.base_dir / "vbia_results.json",
            "vbia_state": self.config.base_dir / "vbia_state.json",
            "prime_state": self.config.base_dir / "prime_state.json",
            "reactor_state": self.config.base_dir / "reactor_state.json",
            "heartbeat": self.config.base_dir / "heartbeat.json",
        }

        # v6.4: Use distributed lock manager for cross-process locking
        # Replaces in-memory asyncio.Lock with file-based locks that work across processes
        # Prevents deadlock scenarios where crashed processes leave locks hanging
        self._lock_manager: Optional[DistributedLockManager] = None
        self._state_write_lock = asyncio.Lock()

        # Background tasks
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._state_update_task: Optional[asyncio.Task] = None
        self._running = False

        # State cache
        self._jarvis_state = RepoState(
            repo_type=RepoType.JARVIS,
            status=StateStatus.INITIALIZING,
            capabilities={
                "visual_security": self.config.visual_security_enabled,
                "vbia_authentication": True,
                "langgraph_reasoning": True,
                "chromadb_memory": True,
                "helicone_tracking": True,
            }
        )

    async def initialize(self) -> bool:
        """
        Initialize the cross-repo state system.

        Returns:
            True if initialization succeeded, False otherwise
        """
        if self._initialized:
            logger.info("[CrossRepoState] Already initialized")
            return True

        try:
            logger.info("[CrossRepoState] Starting initialization...")

            # v6.4: Initialize distributed lock manager
            self._lock_manager = await get_lock_manager()
            logger.info("[CrossRepoState] Distributed lock manager initialized")

            # Create directory structure
            await self._create_directory_structure()

            # Initialize all state files in parallel
            await asyncio.gather(
                self._initialize_vbia_events(),
                self._initialize_vbia_requests(),
                self._initialize_vbia_results(),
                self._initialize_vbia_state(),
                self._initialize_prime_state(),
                self._initialize_reactor_state(),
                self._initialize_heartbeat(),
            )

            # Update JARVIS state to ready
            self._jarvis_state.status = StateStatus.READY
            await self._write_jarvis_state()

            # Start background tasks
            self._running = True
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._state_update_task = asyncio.create_task(self._state_update_loop())

            self._initialized = True
            logger.info("[CrossRepoState] ✅ Initialization complete")
            logger.info(f"[CrossRepoState]    Directory: {self.config.base_dir}")
            logger.info(f"[CrossRepoState]    Visual Security: {self.config.visual_security_enabled}")
            logger.info(f"[CrossRepoState]    Heartbeat Interval: {self.config.heartbeat_interval_seconds}s")

            # Emit system ready event
            await self.emit_event(VBIAEvent(
                event_type=EventType.SYSTEM_READY,
                source_repo=RepoType.JARVIS,
                payload={
                    "version": "6.2.0",
                    "visual_security_enabled": self.config.visual_security_enabled,
                    "visual_security_mode": self.config.visual_security_mode,
                    "capabilities": self._jarvis_state.capabilities,
                }
            ))

            return True

        except Exception as e:
            logger.error(f"[CrossRepoState] ❌ Initialization failed: {e}", exc_info=True)
            self._jarvis_state.status = StateStatus.ERROR
            self._jarvis_state.errors.append(str(e))
            return False

    async def shutdown(self) -> None:
        """Shutdown the cross-repo state system."""
        logger.info("[CrossRepoState] Shutting down...")

        self._running = False

        # Cancel background tasks
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._state_update_task:
            self._state_update_task.cancel()
            try:
                await self._state_update_task
            except asyncio.CancelledError:
                pass

        # Update state to offline
        self._jarvis_state.status = StateStatus.OFFLINE
        await self._write_jarvis_state()

        logger.info("[CrossRepoState] ✅ Shutdown complete")

    # =========================================================================
    # Directory and File Initialization
    # =========================================================================

    async def _create_directory_structure(self) -> None:
        """Create the ~/.jarvis/cross_repo/ directory structure."""
        try:
            self.config.base_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"[CrossRepoState] Directory created: {self.config.base_dir}")
        except Exception as e:
            logger.error(f"[CrossRepoState] Failed to create directory: {e}")
            raise

    async def _initialize_vbia_events(self) -> None:
        """Initialize vbia_events.json file."""
        events_file = self._state_files["vbia_events"]
        if not events_file.exists():
            await self._write_json_file(events_file, [])
            logger.info("[CrossRepoState] ✓ vbia_events.json initialized")

    async def _initialize_vbia_requests(self) -> None:
        """Initialize vbia_requests.json file."""
        requests_file = self._state_files["vbia_requests"]
        if not requests_file.exists():
            await self._write_json_file(requests_file, [])
            logger.info("[CrossRepoState] ✓ vbia_requests.json initialized")

    async def _initialize_vbia_results(self) -> None:
        """Initialize vbia_results.json file."""
        results_file = self._state_files["vbia_results"]
        if not results_file.exists():
            await self._write_json_file(results_file, [])
            logger.info("[CrossRepoState] ✓ vbia_results.json initialized")

    async def _initialize_vbia_state(self) -> None:
        """Initialize vbia_state.json file."""
        state_file = self._state_files["vbia_state"]
        await self._write_jarvis_state()
        logger.info("[CrossRepoState] ✓ vbia_state.json initialized")

    async def _initialize_prime_state(self) -> None:
        """Initialize prime_state.json file (placeholder for JARVIS Prime)."""
        prime_file = self._state_files["prime_state"]
        if not prime_file.exists():
            prime_state = RepoState(
                repo_type=RepoType.JARVIS_PRIME,
                status=StateStatus.OFFLINE,
                capabilities={"vbia_delegation": False}
            )
            await self._write_json_file(prime_file, asdict(prime_state))
            logger.info("[CrossRepoState] ✓ prime_state.json initialized (offline)")

    async def _initialize_reactor_state(self) -> None:
        """Initialize reactor_state.json file (placeholder for Reactor Core)."""
        reactor_file = self._state_files["reactor_state"]
        if not reactor_file.exists():
            reactor_state = RepoState(
                repo_type=RepoType.REACTOR_CORE,
                status=StateStatus.OFFLINE,
                capabilities={"vbia_analytics": False}
            )
            await self._write_json_file(reactor_file, asdict(reactor_state))
            logger.info("[CrossRepoState] ✓ reactor_state.json initialized (offline)")

    async def _initialize_heartbeat(self) -> None:
        """Initialize heartbeat.json file."""
        heartbeat_file = self._state_files["heartbeat"]
        heartbeats = {
            "jarvis": asdict(Heartbeat(repo_type=RepoType.JARVIS, status=StateStatus.INITIALIZING)),
            "jarvis_prime": asdict(Heartbeat(repo_type=RepoType.JARVIS_PRIME, status=StateStatus.OFFLINE)),
            "reactor_core": asdict(Heartbeat(repo_type=RepoType.REACTOR_CORE, status=StateStatus.OFFLINE)),
        }
        await self._write_json_file(heartbeat_file, heartbeats)
        logger.info("[CrossRepoState] ✓ heartbeat.json initialized")

    # =========================================================================
    # Event Emission API
    # =========================================================================

    async def emit_event(self, event: VBIAEvent) -> None:
        """
        Emit a VBIA event to the cross-repo system (thread-safe).

        Uses file-level locking to prevent race conditions in the
        read-modify-write operation.

        Args:
            event: The event to emit
        """
        try:
            events_file = self._state_files["vbia_events"]

            # v6.4: Acquire distributed lock for atomic read-modify-write
            # This lock works across processes (not just coroutines)
            # v6.5: Guard against None lock manager (graceful degradation)
            if self._lock_manager is None:
                logger.warning("[CrossRepoState] Lock manager not initialized, emitting without lock")
                events = await self._read_json_file(events_file, default=[])
                events.append(asdict(event))
                if self.config.event_rotation_enabled and len(events) > self.config.max_events_per_file:
                    events = events[-self.config.max_events_per_file:]
                await self._write_json_file(events_file, events)
                logger.debug(f"[CrossRepoState] Event emitted (unlocked): {event.event_type.value}")
                return

            async with RobustFileLock("vbia_events", source="jarvis") as acquired:
                if not acquired:
                    logger.warning("Could not acquire vbia_events lock, skipping emit")
                    return
                # Read existing events
                events = await self._read_json_file(events_file, default=[])

                # Add new event
                events.append(asdict(event))

                # Rotate if needed
                if self.config.event_rotation_enabled and len(events) > self.config.max_events_per_file:
                    events = events[-self.config.max_events_per_file:]

                # Write back
                await self._write_json_file(events_file, events)

            logger.debug(f"[CrossRepoState] Event emitted: {event.event_type.value}")

        except Exception as e:
            logger.error(f"[CrossRepoState] Failed to emit event: {e}")

    # =========================================================================
    # v113.0: CloudSQL Readiness Broadcasting API
    # =========================================================================

    async def broadcast_cloudsql_ready(
        self,
        ready: bool,
        latency_ms: Optional[float] = None,
        failure_reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        v113.0: Broadcast CloudSQL readiness state to all cross-repo components.

        This enables JARVIS Prime and Reactor Core to:
        - Know when CloudSQL is ready before attempting DB operations
        - Gracefully degrade if CloudSQL is unavailable
        - Coordinate startup timing with database availability

        Args:
            ready: Whether CloudSQL proxy is ready and DB-level connectivity confirmed
            latency_ms: DB connection latency in milliseconds (if ready)
            failure_reason: Reason for failure (if not ready)
            metadata: Additional metadata to include in the event
        """
        event_type = EventType.CLOUDSQL_PROXY_READY if ready else EventType.CLOUDSQL_PROXY_FAILED

        payload: Dict[str, Any] = {
            "ready": ready,
            "timestamp": time.time(),
            "version": "113.0",
        }

        if latency_ms is not None:
            payload["latency_ms"] = latency_ms

        if failure_reason:
            payload["failure_reason"] = failure_reason

        if metadata:
            payload.update(metadata)

        event = VBIAEvent(
            event_type=event_type,
            source_repo=RepoType.JARVIS,
            payload=payload,
        )

        await self.emit_event(event)

        # Also write to a dedicated cloudsql_state.json file for quick lookup
        await self._write_cloudsql_state(ready, latency_ms, failure_reason, metadata)

        logger.info(
            f"[CrossRepoState v113.0] CloudSQL readiness broadcast: "
            f"ready={ready}, latency_ms={latency_ms}"
        )

    async def _write_cloudsql_state(
        self,
        ready: bool,
        latency_ms: Optional[float] = None,
        failure_reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        v113.0: Write CloudSQL state to a dedicated state file for quick lookup.

        Other repos can read this file to check CloudSQL readiness without
        parsing the event log.
        """
        try:
            state_file = self.config.base_dir / "cloudsql_state.json"

            state = {
                "ready": ready,
                "last_update": datetime.now().isoformat(),
                "last_update_timestamp": time.time(),
                "latency_ms": latency_ms,
                "failure_reason": failure_reason,
                "version": "113.0",
                "source_repo": RepoType.JARVIS.value,
            }

            if metadata:
                state["metadata"] = metadata

            await self._write_json_file(state_file, state)

        except Exception as e:
            logger.warning(f"[CrossRepoState v113.0] Failed to write CloudSQL state: {e}")

    async def get_cloudsql_state(self) -> Dict[str, Any]:
        """
        v113.0: Get the current CloudSQL readiness state.

        Returns:
            Dict with ready status, latency, failure_reason, and metadata
        """
        try:
            state_file = self.config.base_dir / "cloudsql_state.json"
            return await self._read_json_file(state_file, default={"ready": False})
        except Exception as e:
            logger.debug(f"[CrossRepoState v113.0] Failed to read CloudSQL state: {e}")
            return {"ready": False, "error": str(e)}

    async def wait_for_cloudsql_ready(
        self,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 1.0,
    ) -> bool:
        """
        v113.0: Wait for CloudSQL to become ready (for use by other repos).

        This is useful for JARVIS Prime and Reactor Core to wait for CloudSQL
        before starting database-dependent components.

        Args:
            timeout_seconds: Maximum time to wait
            poll_interval_seconds: How often to check the state

        Returns:
            True if CloudSQL became ready, False if timeout
        """
        start_time = time.time()

        while (time.time() - start_time) < timeout_seconds:
            state = await self.get_cloudsql_state()

            if state.get("ready", False):
                logger.info(
                    f"[CrossRepoState v113.0] CloudSQL ready "
                    f"(waited {time.time() - start_time:.1f}s)"
                )
                return True

            await asyncio.sleep(poll_interval_seconds)

        logger.warning(
            f"[CrossRepoState v113.0] Timeout waiting for CloudSQL "
            f"(waited {timeout_seconds}s)"
        )
        return False

    # =========================================================================
    # v114.0: Cross-Repo Credential Synchronization API
    # =========================================================================

    async def broadcast_credential_event(
        self,
        event_type: EventType,
        source: str,
        success: bool,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        v114.0: Broadcast credential lifecycle events across JARVIS Trinity repos.

        This enables JARVIS, JARVIS Prime, and Reactor Core to:
        - Synchronize credential validation state
        - Invalidate caches when credentials change
        - Coordinate credential refresh across all repos

        Args:
            event_type: The credential event type (CREDENTIAL_VALIDATED, etc.)
            source: The credential source (e.g., "gcp_secret_manager", "environment")
            success: Whether the credential operation succeeded
            metadata: Additional metadata to include in the event
        """
        payload: Dict[str, Any] = {
            "source": source,
            "success": success,
            "timestamp": time.time(),
            "version": "114.0",
        }

        if metadata:
            payload.update(metadata)

        event = VBIAEvent(
            event_type=event_type,
            source_repo=RepoType.JARVIS,
            payload=payload,
        )

        await self.emit_event(event)

        # Write to dedicated credential state file for quick lookup
        await self._write_credential_state(event_type, source, success, metadata)

        logger.info(
            f"[CrossRepoState v114.0] Credential event broadcast: "
            f"type={event_type.value}, source={source}, success={success}"
        )

    async def _write_credential_state(
        self,
        event_type: EventType,
        source: str,
        success: bool,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        v114.0: Write credential state to a dedicated state file for quick lookup.

        Other repos can read this file to check credential status without
        parsing the event log.
        """
        try:
            state_file = self.config.base_dir / "credential_state.json"

            state = {
                "last_event": event_type.value,
                "source": source,
                "success": success,
                "last_update": datetime.now().isoformat(),
                "last_update_timestamp": time.time(),
                "version": "114.0",
                "source_repo": RepoType.JARVIS.value,
            }

            if metadata:
                state["metadata"] = metadata

            await self._write_json_file(state_file, state)

        except Exception as e:
            logger.warning(f"[CrossRepoState v114.0] Failed to write credential state: {e}")

    async def get_credential_state(self) -> Dict[str, Any]:
        """
        v114.0: Get the current credential state.

        Returns:
            Dict with credential status, source, and metadata
        """
        try:
            state_file = self.config.base_dir / "credential_state.json"
            return await self._read_json_file(state_file, default={"success": False})
        except Exception as e:
            logger.debug(f"[CrossRepoState v114.0] Failed to read credential state: {e}")
            return {"success": False, "error": str(e)}

    async def broadcast_credential_validated(
        self,
        source: str,
        user: str = "jarvis",
        latency_ms: Optional[float] = None,
    ) -> None:
        """
        v114.0: Broadcast that credentials have been successfully validated.

        Call this after a successful database connection to inform other repos.
        """
        await self.broadcast_credential_event(
            event_type=EventType.CREDENTIAL_VALIDATED,
            source=source,
            success=True,
            metadata={
                "user": user,
                "latency_ms": latency_ms,
                "validated_at": time.time(),
            }
        )

    async def broadcast_credential_invalidated(
        self,
        source: str,
        reason: str,
        user: str = "jarvis",
    ) -> None:
        """
        v114.0: Broadcast that credentials have been invalidated.

        Call this when authentication fails to inform other repos to refresh.
        """
        await self.broadcast_credential_event(
            event_type=EventType.CREDENTIAL_INVALIDATED,
            source=source,
            success=False,
            metadata={
                "user": user,
                "reason": reason,
                "invalidated_at": time.time(),
            }
        )

    async def broadcast_credential_refreshed(
        self,
        old_source: str,
        new_source: str,
        user: str = "jarvis",
    ) -> None:
        """
        v114.0: Broadcast that credentials have been refreshed from a new source.

        Call this when credentials are re-resolved from an alternate source.
        """
        await self.broadcast_credential_event(
            event_type=EventType.CREDENTIAL_REFRESHED,
            source=new_source,
            success=True,
            metadata={
                "user": user,
                "old_source": old_source,
                "new_source": new_source,
                "refreshed_at": time.time(),
            }
        )

    async def get_recent_events(self, limit: int = 100, event_type: Optional[EventType] = None) -> List[VBIAEvent]:
        """
        Get recent events from the event log.

        Args:
            limit: Maximum number of events to return
            event_type: Optional filter by event type

        Returns:
            List of recent events
        """
        try:
            events_file = self._state_files["vbia_events"]
            events_data = await self._read_json_file(events_file, default=[])

            # Filter by type if specified
            if event_type:
                events_data = [e for e in events_data if e.get("event_type") == event_type.value]

            # Get most recent events
            recent_events = events_data[-limit:]

            # Convert to VBIAEvent objects
            return [
                VBIAEvent(
                    event_id=e.get("event_id", ""),
                    event_type=EventType(e.get("event_type", "vbia_system_ready")),
                    timestamp=e.get("timestamp", ""),
                    source_repo=RepoType(e.get("source_repo", "jarvis")),
                    session_id=e.get("session_id"),
                    user_id=e.get("user_id"),
                    payload=e.get("payload", {}),
                    correlation_id=e.get("correlation_id"),
                )
                for e in recent_events
            ]

        except Exception as e:
            logger.error(f"[CrossRepoState] Failed to get recent events: {e}")
            return []

    # =========================================================================
    # State Management
    # =========================================================================

    async def _write_jarvis_state(self) -> None:
        """Write JARVIS state to vbia_state.json (thread-safe)."""
        state_file = self._state_files["vbia_state"]

        # v236.3: Pre-serialize OUTSIDE lock to minimize hold time.
        # During startup, event loop saturation delays aiofiles completion,
        # keeping the lock held for tens of seconds. Pre-serializing and using
        # a synchronous write reduces lock scope to just the I/O.
        self._jarvis_state.last_update = datetime.now().isoformat()
        _serialized = json.dumps(asdict(self._jarvis_state), indent=2)

        # v6.5: Guard against None lock manager
        # Serialize local writes to prevent same-process lock storms and timeouts.
        async with self._state_write_lock:
            loop = asyncio.get_running_loop()
            if self._lock_manager is None:
                logger.debug("[CrossRepoState] Writing state without lock (manager not initialized)")
                await loop.run_in_executor(None, self._write_file_sync, str(state_file), _serialized)
                return
            # v6.4: Use RobustFileLock (fcntl.flock-based, fixes temp file race conditions)
            async with RobustFileLock("vbia_state", source="jarvis") as acquired:
                if not acquired:
                    logger.warning("Could not acquire vbia_state lock")
                    return
                # v236.3: Synchronous write in executor — bypasses aiofiles thread pool
                await loop.run_in_executor(None, self._write_file_sync, str(state_file), _serialized)

    async def update_jarvis_status(self, status: StateStatus, metrics: Optional[Dict[str, Any]] = None) -> None:
        """
        Update JARVIS status (thread-safe).

        Args:
            status: New status
            metrics: Optional metrics to update
        """
        # Lock is handled inside _write_jarvis_state
        self._jarvis_state.status = status
        if metrics:
            self._jarvis_state.metrics.update(metrics)
        await self._write_jarvis_state()

    async def get_repo_states(self) -> Dict[str, RepoState]:
        """
        Get current state of all repositories.

        Returns:
            Dictionary mapping repo type to state
        """
        try:
            states = {
                "jarvis": self._jarvis_state,
            }

            # Read JARVIS Prime state
            prime_data = await self._read_json_file(self._state_files["prime_state"], default={})
            if prime_data:
                states["jarvis_prime"] = RepoState(**prime_data)

            # Read Reactor Core state
            reactor_data = await self._read_json_file(self._state_files["reactor_state"], default={})
            if reactor_data:
                states["reactor_core"] = RepoState(**reactor_data)

            return states

        except Exception as e:
            logger.error(f"[CrossRepoState] Failed to get repo states: {e}")
            return {"jarvis": self._jarvis_state}

    # =========================================================================
    # Background Tasks
    # =========================================================================

    async def _heartbeat_loop(self) -> None:
        """Background task that emits heartbeats (thread-safe)."""
        logger.info("[CrossRepoState] Heartbeat loop started")

        while self._running:
            try:
                # v6.4: Update heartbeat file with distributed lock for atomic read-modify-write
                heartbeat_file = self._state_files["heartbeat"]

                # v6.5: Guard against None lock manager
                if self._lock_manager is None:
                    logger.debug("[CrossRepoState] Heartbeat without lock (manager not initialized)")
                    heartbeats = await self._read_json_file(heartbeat_file, default={})
                    heartbeats["jarvis"] = asdict(Heartbeat(
                        repo_type=RepoType.JARVIS,
                        status=self._jarvis_state.status,
                        uptime_seconds=time.time() - self._start_time,
                        active_sessions=self._jarvis_state.metrics.get("active_sessions", 0),
                    ))
                    await self._write_json_file(heartbeat_file, heartbeats)
                    self._jarvis_state.last_heartbeat = datetime.now().isoformat()
                    await asyncio.sleep(self.config.heartbeat_interval_seconds)
                    continue

                # v236.0: Env-var configurable heartbeat lock timeout
                _hb_timeout = float(os.environ.get("JARVIS_HEARTBEAT_LOCK_TIMEOUT", "5.0"))
                # v236.2: Env-var configurable TTL — 20s default gives 17s stall tolerance
                # with 3s keepalive interval, while keeping crash recovery under 20s.
                _cr_ttl = float(os.environ.get("JARVIS_CROSS_REPO_LOCK_TTL", "20.0"))
                async with self._lock_manager.acquire(
                    "heartbeat",
                    timeout=_hb_timeout,
                    ttl=_cr_ttl,
                    enable_keepalive=False,
                ) as acquired:
                    if not acquired:
                        logger.debug("Could not acquire heartbeat lock, skipping update")
                        continue

                    heartbeats = await self._read_json_file(heartbeat_file, default={})

                    heartbeats["jarvis"] = asdict(Heartbeat(
                        repo_type=RepoType.JARVIS,
                        status=self._jarvis_state.status,
                        uptime_seconds=time.time() - self._start_time,
                        active_sessions=self._jarvis_state.metrics.get("active_sessions", 0),
                    ))

                    await self._write_json_file(heartbeat_file, heartbeats)

                # Update last heartbeat timestamp
                self._jarvis_state.last_heartbeat = datetime.now().isoformat()

                await asyncio.sleep(self.config.heartbeat_interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CrossRepoState] Heartbeat loop error: {e}")
                await asyncio.sleep(self.config.heartbeat_interval_seconds)

    async def _state_update_loop(self) -> None:
        """Background task that periodically updates JARVIS state."""
        logger.info("[CrossRepoState] State update loop started")

        while self._running:
            try:
                await self._write_jarvis_state()
                await asyncio.sleep(self.config.state_update_interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CrossRepoState] State update loop error: {e}")
                await asyncio.sleep(self.config.state_update_interval_seconds)

    # =========================================================================
    # v6.3: Coordinated Multi-Repo Startup
    # =========================================================================

    async def initialize_all_repos(self) -> Dict[RepoType, bool]:
        """
        v6.3: Coordinated initialization of all repos with dependency ordering.

        This method initializes repos in the correct order:
        1. JARVIS (local) - Required, must succeed
        2. JARVIS Prime - Optional, can degrade gracefully
        3. Reactor Core - Optional, can degrade gracefully

        Returns:
            Dict mapping repo type to initialization success status
        """
        if not self.config.coordinated_startup_enabled:
            logger.info("[CrossRepoState] Coordinated startup disabled, skipping")
            return {RepoType.JARVIS: True}

        logger.info("[CrossRepoState] ═══════════════════════════════════════════════")
        logger.info("[CrossRepoState] v6.3: Starting coordinated multi-repo initialization")
        logger.info("[CrossRepoState] ═══════════════════════════════════════════════")

        results: Dict[RepoType, bool] = {}
        startup_start = time.time()

        # Phase 1: Initialize JARVIS (local) - This is required
        # v211.0: Use asyncio.wait_for for Python 3.9 compatibility
        try:
            jarvis_success = await asyncio.wait_for(
                self._initialize_jarvis_local(),
                timeout=self.config.repo_startup_timeout_seconds
            )
            results[RepoType.JARVIS] = jarvis_success

            if not jarvis_success:
                logger.error("[CrossRepoState] JARVIS local initialization failed - aborting")
                return results
        except asyncio.TimeoutError:
            logger.error("[CrossRepoState] JARVIS local initialization timed out")
            results[RepoType.JARVIS] = False
            return results

        # Phase 2: Initialize external repos in parallel (with individual timeouts)
        external_init_tasks = [
            self._initialize_jarvis_prime_with_retry(),
            self._initialize_reactor_core_with_retry(),
        ]

        # v211.0: Use asyncio.wait_for for Python 3.9 compatibility
        try:
            external_results = await asyncio.wait_for(
                asyncio.gather(*external_init_tasks, return_exceptions=True),
                timeout=self.config.repo_startup_timeout_seconds * 2
            )

            # Process results
            for i, result in enumerate(external_results):
                repo_type = RepoType.JARVIS_PRIME if i == 0 else RepoType.REACTOR_CORE
                if isinstance(result, Exception):
                    logger.warning(f"[CrossRepoState] {repo_type.value} initialization failed: {result}")
                    results[repo_type] = False
                else:
                    results[repo_type] = result

        except asyncio.TimeoutError:
            logger.warning("[CrossRepoState] External repo initialization timed out")
            for repo_type in [RepoType.JARVIS_PRIME, RepoType.REACTOR_CORE]:
                if repo_type not in results:
                    results[repo_type] = False

        # Log summary
        elapsed = time.time() - startup_start
        success_count = sum(1 for v in results.values() if v)
        total_count = len(results)

        logger.info("[CrossRepoState] ═══════════════════════════════════════════════")
        logger.info(f"[CrossRepoState] v6.3: Coordinated startup complete ({elapsed:.2f}s)")
        logger.info(f"[CrossRepoState]   • Success: {success_count}/{total_count} repos")
        for repo_type, success in results.items():
            status = "✅ Ready" if success else "⚠️ Degraded"
            logger.info(f"[CrossRepoState]   • {repo_type.value}: {status}")
        logger.info("[CrossRepoState] ═══════════════════════════════════════════════")

        # Emit coordinated startup event
        await self.emit_event(VBIAEvent(
            event_type=EventType.SYSTEM_READY,
            source_repo=RepoType.JARVIS,
            payload={
                "coordinated_startup": True,
                "repos_initialized": {k.value: v for k, v in results.items()},
                "elapsed_seconds": elapsed,
                "graceful_degradation": self.config.graceful_degradation_enabled,
            }
        ))

        return results

    async def _initialize_jarvis_local(self) -> bool:
        """Initialize JARVIS local state (required)."""
        logger.info("[CrossRepoState] Phase 1: Initializing JARVIS local...")
        try:
            # This reuses the existing initialize() logic if not already initialized
            if not self._initialized:
                return await self.initialize()
            return True
        except Exception as e:
            logger.error(f"[CrossRepoState] JARVIS local init failed: {e}")
            return False

    async def _initialize_jarvis_prime_with_retry(self) -> bool:
        """Initialize JARVIS Prime with retry and health probing."""
        logger.info("[CrossRepoState] Phase 2a: Initializing JARVIS Prime...")

        for attempt in range(self.config.repo_health_retry_count):
            try:
                success = await self._probe_jarvis_prime_health()
                if success:
                    # Update prime state to ready
                    await self._update_prime_state(StateStatus.READY)
                    logger.info("[CrossRepoState] ✅ JARVIS Prime initialized")
                    return True

                logger.debug(f"[CrossRepoState] JARVIS Prime probe attempt {attempt + 1} failed")

            except Exception as e:
                logger.debug(f"[CrossRepoState] JARVIS Prime probe error: {e}")

            if attempt < self.config.repo_health_retry_count - 1:
                await asyncio.sleep(self.config.repo_health_retry_delay_seconds)

        # All retries failed - graceful degradation
        if self.config.graceful_degradation_enabled:
            logger.warning("[CrossRepoState] ⚠️ JARVIS Prime unavailable - degraded mode")
            await self._update_prime_state(StateStatus.DEGRADED)
            return False
        else:
            raise RuntimeError("JARVIS Prime initialization failed and graceful degradation disabled")

    async def _initialize_reactor_core_with_retry(self) -> bool:
        """Initialize Reactor Core with retry and health probing."""
        logger.info("[CrossRepoState] Phase 2b: Initializing Reactor Core...")

        for attempt in range(self.config.repo_health_retry_count):
            try:
                success = await self._probe_reactor_core_health()
                if success:
                    # Update reactor state to ready
                    await self._update_reactor_state(StateStatus.READY)
                    logger.info("[CrossRepoState] ✅ Reactor Core initialized")
                    return True

                logger.debug(f"[CrossRepoState] Reactor Core probe attempt {attempt + 1} failed")

            except Exception as e:
                logger.debug(f"[CrossRepoState] Reactor Core probe error: {e}")

            if attempt < self.config.repo_health_retry_count - 1:
                await asyncio.sleep(self.config.repo_health_retry_delay_seconds)

        # All retries failed - graceful degradation
        if self.config.graceful_degradation_enabled:
            logger.warning("[CrossRepoState] ⚠️ Reactor Core unavailable - degraded mode")
            await self._update_reactor_state(StateStatus.DEGRADED)
            return False
        else:
            raise RuntimeError("Reactor Core initialization failed and graceful degradation disabled")

    async def _probe_jarvis_prime_health(self) -> bool:
        """
        Probe JARVIS Prime health.

        Checks:
        1. Prime state file exists and has recent heartbeat
        2. (Optional) HTTP health check if endpoint configured
        """
        # v211.0: Use asyncio.wait_for for Python 3.9 compatibility
        async def _do_health_probe() -> bool:
            # Method 1: Check heartbeat file
            heartbeat_data = await self._read_json_file(self._state_files["heartbeat"], default={})
            prime_heartbeat = heartbeat_data.get("jarvis_prime", {})

            if prime_heartbeat:
                last_heartbeat = prime_heartbeat.get("timestamp", "")
                if last_heartbeat:
                    try:
                        heartbeat_time = datetime.fromisoformat(last_heartbeat)
                        age = datetime.now() - heartbeat_time
                        if age < timedelta(seconds=self.config.heartbeat_timeout_seconds):
                            logger.debug("[CrossRepoState] JARVIS Prime heartbeat valid")
                            return True
                    except (ValueError, TypeError):
                        pass

            # Method 2: Check Prime state file
            prime_state = await self._read_json_file(self._state_files["prime_state"], default={})
            if prime_state.get("status") in ("ready", "active"):
                logger.debug("[CrossRepoState] JARVIS Prime state valid")
                return True

            # Method 3: Try HTTP health check if configured
            prime_url = _get_env("JARVIS_PRIME_HEALTH_URL", "")
            if prime_url:
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            prime_url,
                            timeout=aiohttp.ClientTimeout(total=self.config.repo_health_probe_timeout_seconds)
                        ) as resp:
                            if resp.status == 200:
                                logger.debug("[CrossRepoState] JARVIS Prime HTTP health check passed")
                                return True
                except Exception:
                    pass

            return False

        try:
            return await asyncio.wait_for(
                _do_health_probe(),
                timeout=self.config.repo_health_probe_timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.debug("[CrossRepoState] JARVIS Prime health probe timed out")
            return False
        except Exception as e:
            logger.debug(f"[CrossRepoState] JARVIS Prime health probe error: {e}")
            return False

    async def _probe_reactor_core_health(self) -> bool:
        """
        Probe Reactor Core health.

        Checks:
        1. Reactor state file exists and has recent heartbeat
        2. (Optional) HTTP health check if endpoint configured
        """
        # v211.0: Use asyncio.wait_for for Python 3.9 compatibility
        async def _do_health_probe() -> bool:
            # Method 1: Check heartbeat file
            heartbeat_data = await self._read_json_file(self._state_files["heartbeat"], default={})
            reactor_heartbeat = heartbeat_data.get("reactor_core", {})

            if reactor_heartbeat:
                last_heartbeat = reactor_heartbeat.get("timestamp", "")
                if last_heartbeat:
                    try:
                        heartbeat_time = datetime.fromisoformat(last_heartbeat)
                        age = datetime.now() - heartbeat_time
                        if age < timedelta(seconds=self.config.heartbeat_timeout_seconds):
                            logger.debug("[CrossRepoState] Reactor Core heartbeat valid")
                            return True
                    except (ValueError, TypeError):
                        pass

            # Method 2: Check Reactor state file
            reactor_state = await self._read_json_file(self._state_files["reactor_state"], default={})
            if reactor_state.get("status") in ("ready", "active"):
                logger.debug("[CrossRepoState] Reactor Core state valid")
                return True

            # Method 3: Try HTTP health check if configured
            reactor_url = _get_env("REACTOR_CORE_HEALTH_URL", "")
            if reactor_url:
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            reactor_url,
                            timeout=aiohttp.ClientTimeout(total=self.config.repo_health_probe_timeout_seconds)
                        ) as resp:
                            if resp.status == 200:
                                logger.debug("[CrossRepoState] Reactor Core HTTP health check passed")
                                return True
                except Exception:
                    pass

            return False

        try:
            return await asyncio.wait_for(
                _do_health_probe(),
                timeout=self.config.repo_health_probe_timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.debug("[CrossRepoState] Reactor Core health probe timed out")
            return False
        except Exception as e:
            logger.debug(f"[CrossRepoState] Reactor Core health probe error: {e}")
            return False

    async def _update_prime_state(self, status: StateStatus) -> None:
        """Update JARVIS Prime state file."""
        prime_file = self._state_files["prime_state"]
        # v6.5: Guard against None lock manager
        if self._lock_manager is None:
            logger.debug("[CrossRepoState] Updating prime state without lock")
            prime_state = await self._read_json_file(prime_file, default={})
            prime_state["status"] = status.value
            prime_state["last_update"] = datetime.now().isoformat()
            await self._write_json_file(prime_file, prime_state)
            return
        # v6.4: Use distributed lock
        _cr_ttl = float(os.environ.get("JARVIS_CROSS_REPO_LOCK_TTL", "20.0"))
        async with self._lock_manager.acquire(
            "prime_state",
            timeout=5.0,
            ttl=_cr_ttl,
            enable_keepalive=False,
        ) as acquired:
            if not acquired:
                logger.warning("Could not acquire prime_state lock")
                return
            prime_state = await self._read_json_file(prime_file, default={})
            prime_state["status"] = status.value
            prime_state["last_update"] = datetime.now().isoformat()
            await self._write_json_file(prime_file, prime_state)

    async def _update_reactor_state(self, status: StateStatus) -> None:
        """Update Reactor Core state file."""
        reactor_file = self._state_files["reactor_state"]
        # v6.5: Guard against None lock manager
        if self._lock_manager is None:
            logger.debug("[CrossRepoState] Updating reactor state without lock")
            reactor_state = await self._read_json_file(reactor_file, default={})
            reactor_state["status"] = status.value
            reactor_state["last_update"] = datetime.now().isoformat()
            await self._write_json_file(reactor_file, reactor_state)
            return
        # v6.4: Use distributed lock
        _cr_ttl = float(os.environ.get("JARVIS_CROSS_REPO_LOCK_TTL", "20.0"))
        async with self._lock_manager.acquire(
            "reactor_state",
            timeout=5.0,
            ttl=_cr_ttl,
            enable_keepalive=False,
        ) as acquired:
            if not acquired:
                logger.warning("Could not acquire reactor_state lock")
                return
            reactor_state = await self._read_json_file(reactor_file, default={})
            reactor_state["status"] = status.value
            reactor_state["last_update"] = datetime.now().isoformat()
            await self._write_json_file(reactor_file, reactor_state)

    async def get_initialization_status(self) -> Dict[str, Any]:
        """
        v6.3: Get current initialization status of all repos.

        Returns:
            Dict with status information for all repos
        """
        states = await self.get_repo_states()

        return {
            "coordinated_startup_enabled": self.config.coordinated_startup_enabled,
            "graceful_degradation_enabled": self.config.graceful_degradation_enabled,
            "repos": {
                repo_name: {
                    "status": state.status.value if hasattr(state, "status") else "unknown",
                    "last_update": getattr(state, "last_update", "unknown"),
                    "capabilities": getattr(state, "capabilities", {}),
                }
                for repo_name, state in states.items()
            },
            "jarvis_initialized": self._initialized,
            "uptime_seconds": time.time() - self._start_time,
        }

    # =========================================================================
    # File I/O Utilities
    # =========================================================================

    async def _read_json_file(self, file_path: Path, default: Any = None) -> Any:
        """Read JSON file asynchronously."""
        try:
            if not file_path.exists():
                return default

            async with aiofiles.open(file_path, "r") as f:
                content = await f.read()
                return json.loads(content) if content else default

        except Exception as e:
            logger.error(f"[CrossRepoState] Failed to read {file_path}: {e}")
            return default

    @staticmethod
    def _write_file_sync(path: str, data: str) -> None:
        """v236.3: Synchronous atomic write — called inside executor.
        Uses temp file + rename for crash safety on POSIX."""
        tmp_path = path + ".tmp"
        with open(tmp_path, 'w') as f:
            f.write(data)
        os.rename(tmp_path, path)  # Atomic on POSIX same-filesystem

    async def _write_json_file(self, file_path: Path, data: Any) -> None:
        """Write JSON file asynchronously."""
        try:
            async with aiofiles.open(file_path, "w") as f:
                await f.write(json.dumps(data, indent=2))

        except Exception as e:
            logger.error(f"[CrossRepoState] Failed to write {file_path}: {e}")
            raise


# =============================================================================
# Global Singleton
# =============================================================================

_cross_repo_initializer: Optional[CrossRepoStateInitializer] = None


async def get_cross_repo_initializer(
    config: Optional[CrossRepoStateConfig] = None
) -> CrossRepoStateInitializer:
    """
    Get or create the global cross-repo state initializer.

    Args:
        config: Optional configuration (only used on first call)

    Returns:
        The cross-repo state initializer instance
    """
    global _cross_repo_initializer

    if _cross_repo_initializer is None:
        _cross_repo_initializer = CrossRepoStateInitializer(config)

    return _cross_repo_initializer


async def initialize_cross_repo_state(
    config: Optional[CrossRepoStateConfig] = None
) -> bool:
    """
    Initialize the cross-repo state system.

    This is the main entry point for initializing the cross-repo communication
    infrastructure during JARVIS startup.

    Args:
        config: Optional configuration

    Returns:
        True if initialization succeeded, False otherwise
    """
    initializer = await get_cross_repo_initializer(config)
    return await initializer.initialize()


async def shutdown_cross_repo_state() -> None:
    """Shutdown the cross-repo state system."""
    global _cross_repo_initializer

    if _cross_repo_initializer:
        await _cross_repo_initializer.shutdown()
        _cross_repo_initializer = None


async def initialize_all_repos_coordinated(
    config: Optional[CrossRepoStateConfig] = None
) -> Dict[RepoType, bool]:
    """
    v6.3: Initialize all repos with coordinated startup.

    This is the main entry point for coordinated multi-repo initialization.
    It initializes JARVIS first (required), then JARVIS Prime and Reactor Core
    in parallel with graceful degradation if they're unavailable.

    Args:
        config: Optional configuration

    Returns:
        Dict mapping repo type to initialization success status

    Example:
        results = await initialize_all_repos_coordinated()
        if results[RepoType.JARVIS]:
            print("JARVIS initialized successfully")
        if results.get(RepoType.JARVIS_PRIME, False):
            print("JARVIS Prime connected")
    """
    initializer = await get_cross_repo_initializer(config)
    return await initializer.initialize_all_repos()


async def get_initialization_status() -> Dict[str, Any]:
    """
    v6.3: Get current initialization status of all repos.

    Returns:
        Dict with status information for all repos
    """
    initializer = await get_cross_repo_initializer()
    return await initializer.get_initialization_status()
