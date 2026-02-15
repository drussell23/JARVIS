"""
v77.0: Coding Council Trinity Integration
==========================================

Connects the Unified Coding Council to Project Trinity for cross-repo
code evolution and intelligent task distribution.

This module enables:
- Evolution commands from J-Prime (Mind) to JARVIS (Body)
- Status broadcasting to Reactor Core (Nerves)
- Cross-repo code modifications
- Distributed task execution

Trinity Architecture Integration:
    ┌────────────┐    Evolution Commands    ┌──────────────────┐
    │  J-PRIME   │ ────────────────────────>│  CODING COUNCIL  │
    │   (Mind)   │                          │   (Orchestrator) │
    └────────────┘    Status Updates        └──────────────────┘
           │                                         │
           │         ┌──────────────┐               │
           └────────>│ REACTOR CORE │<──────────────┘
                     │   (Nerves)   │    Task Metrics
                     └──────────────┘

Author: JARVIS v77.0
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
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .orchestrator import UnifiedCodingCouncil
    from .types import EvolutionResult, EvolutionTask

logger = logging.getLogger(__name__)

# =============================================================================
# Trinity Module Imports (v77.0 Enhanced)
# =============================================================================

TRINITY_MODULE_AVAILABLE = False
try:
    from .trinity import (
        MultiTransport,
        TransportType,
        TransportStatus,
        TransportMessage,
        PersistentMessageQueue,
        QueueMessage,
        MessagePriority,
        HeartbeatValidator,
        HeartbeatStatus,
        ComponentHealth,
        CrossRepoSync,
        RepoState,
        SyncStatus,
    )
    TRINITY_MODULE_AVAILABLE = True
except ImportError as e:
    logger.debug(f"Trinity module not available: {e}")
    # Stub classes for graceful degradation
    MultiTransport = None
    PersistentMessageQueue = None
    HeartbeatValidator = None
    CrossRepoSync = None


# =============================================================================
# Configuration (Unified Config Integration)
# =============================================================================

def _get_unified_config():
    """Get unified configuration."""
    try:
        from .config import get_config
        return get_config()
    except ImportError:
        return None


def _get_trinity_repos() -> Dict[str, Path]:
    """Get Trinity repos from unified config."""
    config = _get_unified_config()
    if config:
        return {name: repo.path for name, repo in config.repos.items()}
    return {
        "jarvis": Path(os.getenv("JARVIS_REPO", str(Path.home() / "Documents/repos/JARVIS-AI-Agent"))),
        "j_prime": Path(os.getenv("JARVIS_PRIME_REPO", str(Path.home() / "Documents/repos/jarvis-prime"))),
        "reactor_core": Path(os.getenv("REACTOR_CORE_REPO", str(Path.home() / "Documents/repos/reactor-core"))),
    }


def _is_enabled() -> bool:
    """Check if Coding Council is enabled."""
    config = _get_unified_config()
    if config:
        return config.enabled
    return os.getenv("CODING_COUNCIL_ENABLED", "true").lower() == "true"


def _is_cross_repo_enabled() -> bool:
    """Check if cross-repo operations are enabled."""
    config = _get_unified_config()
    if config:
        return config.trinity_sync_enabled
    return os.getenv("CODING_COUNCIL_CROSS_REPO", "true").lower() == "true"


def _get_trinity_dir() -> Path:
    """Get Trinity directory."""
    config = _get_unified_config()
    if config:
        return config.trinity_dir
    return Path.home() / ".jarvis" / "trinity"


def _get_state_file() -> Path:
    """Get Coding Council state file path."""
    return _get_trinity_dir() / "components" / "coding_council.json"


def _is_auto_approve_enabled() -> bool:
    """Check if auto-approve is enabled for evolutions."""
    config = _get_unified_config()
    if config and hasattr(config, 'coding_council_auto_approve'):
        return config.coding_council_auto_approve
    return os.getenv("CODING_COUNCIL_AUTO_APPROVE", "false").lower() == "true"


def _get_status_broadcast_interval() -> float:
    """Get the status broadcast interval in seconds."""
    config = _get_unified_config()
    if config and hasattr(config, 'heartbeat_interval'):
        return config.heartbeat_interval
    return float(os.getenv("CODING_COUNCIL_STATUS_INTERVAL", "10.0"))


def _ensure_trinity_dirs() -> None:
    """Ensure all Trinity directories exist."""
    trinity_dir = _get_trinity_dir()
    for subdir in ["components", "commands", "evolutions", "messages", "sync"]:
        (trinity_dir / subdir).mkdir(parents=True, exist_ok=True)


# =============================================================================
# DEPRECATED: Legacy Constants (DO NOT USE - Use dynamic functions above)
# =============================================================================
# These constants are kept ONLY for backward compatibility with external code.
# All internal code now uses dynamic functions:
#   - _is_enabled() instead of CODING_COUNCIL_ENABLED
#   - _is_cross_repo_enabled() instead of CODING_COUNCIL_CROSS_REPO
#   - _is_auto_approve_enabled() instead of CODING_COUNCIL_AUTO_APPROVE
#   - _get_status_broadcast_interval() instead of STATUS_BROADCAST_INTERVAL
#   - _get_trinity_repos() instead of JARVIS_REPO/JARVIS_PRIME_REPO/REACTOR_CORE_REPO
#   - _get_trinity_dir() instead of TRINITY_DIR
#   - _get_state_file() instead of CODING_COUNCIL_STATE_FILE
# =============================================================================
CODING_COUNCIL_ENABLED = os.getenv("CODING_COUNCIL_ENABLED", "true").lower() == "true"
CODING_COUNCIL_CROSS_REPO = os.getenv("CODING_COUNCIL_CROSS_REPO", "true").lower() == "true"
CODING_COUNCIL_AUTO_APPROVE = os.getenv("CODING_COUNCIL_AUTO_APPROVE", "false").lower() == "true"
STATUS_BROADCAST_INTERVAL = float(os.getenv("CODING_COUNCIL_STATUS_INTERVAL", "10.0"))
JARVIS_REPO = Path(os.getenv("JARVIS_REPO", str(Path.home() / "Documents/repos/JARVIS-AI-Agent")))
JARVIS_PRIME_REPO = Path(os.getenv("JARVIS_PRIME_REPO", str(Path.home() / "Documents/repos/jarvis-prime")))
REACTOR_CORE_REPO = Path(os.getenv("REACTOR_CORE_REPO", str(Path.home() / "Documents/repos/reactor-core")))
TRINITY_DIR = Path.home() / ".jarvis" / "trinity"
CODING_COUNCIL_STATE_FILE = TRINITY_DIR / "components" / "coding_council.json"


# =============================================================================
# Coding Council Intent Extensions for Trinity Protocol
# =============================================================================

class CodingCouncilIntent:
    """
    Intent types for Coding Council commands via Trinity.

    These extend the base TrinityIntent for evolution-specific operations.
    """
    # Evolution commands (from J-Prime)
    EVOLVE_CODE = "evolve_code"
    ABORT_EVOLUTION = "abort_evolution"
    ROLLBACK_EVOLUTION = "rollback_evolution"

    # Query commands
    GET_EVOLUTION_STATUS = "get_evolution_status"
    GET_ACTIVE_TASKS = "get_active_tasks"
    GET_FRAMEWORK_STATUS = "get_framework_status"

    # Cross-repo commands
    CROSS_REPO_EVOLVE = "cross_repo_evolve"
    SYNC_REPOS = "sync_repos"

    # Configuration commands
    UPDATE_CONFIG = "update_council_config"
    ENABLE_FRAMEWORK = "enable_framework"
    DISABLE_FRAMEWORK = "disable_framework"


@dataclass
class CodingCouncilCommand:
    """
    Command schema for Coding Council operations.

    This integrates with TrinityCommand for routing through Reactor Core.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    intent: str = CodingCouncilIntent.EVOLVE_CODE
    payload: Dict[str, Any] = field(default_factory=dict)

    # Task specification
    description: Optional[str] = None
    target_files: List[str] = field(default_factory=list)
    target_repos: List[str] = field(default_factory=list)  # For cross-repo

    # Execution options
    require_approval: bool = True
    require_sandbox: bool = False
    require_planning: bool = False
    timeout_seconds: float = 300.0

    # Correlation
    correlation_id: Optional[str] = None
    response_to: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for Trinity transport."""
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "intent": self.intent,
            "payload": self.payload,
            "description": self.description,
            "target_files": self.target_files,
            "target_repos": self.target_repos,
            "require_approval": self.require_approval,
            "require_sandbox": self.require_sandbox,
            "require_planning": self.require_planning,
            "timeout_seconds": self.timeout_seconds,
            "correlation_id": self.correlation_id,
            "response_to": self.response_to,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CodingCouncilCommand":
        """Deserialize from dict."""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            timestamp=data.get("timestamp", time.time()),
            intent=data.get("intent", CodingCouncilIntent.EVOLVE_CODE),
            payload=data.get("payload", {}),
            description=data.get("description"),
            target_files=data.get("target_files", []),
            target_repos=data.get("target_repos", []),
            require_approval=data.get("require_approval", True),
            require_sandbox=data.get("require_sandbox", False),
            require_planning=data.get("require_planning", False),
            timeout_seconds=data.get("timeout_seconds", 300.0),
            correlation_id=data.get("correlation_id"),
            response_to=data.get("response_to"),
        )


# =============================================================================
# Trinity Integration Bridge
# =============================================================================

class CodingCouncilTrinityBridge:
    """
    Bridge between Coding Council and Trinity network.

    Handles:
    - Command reception from J-Prime
    - Status broadcasting to Reactor Core
    - Cross-repo coordination
    - Evolution result reporting
    """

    def __init__(self):
        self._council: Optional["UnifiedCodingCouncil"] = None
        self._reactor_bridge = None
        self._initialized = False
        self._status_task: Optional[asyncio.Task] = None
        self._message_processor_task: Optional[asyncio.Task] = None
        self._command_handlers: Dict[str, Callable] = {}
        self._active_evolutions: Dict[str, "EvolutionTask"] = {}

        # v77.0 Trinity Module Instances
        self._multi_transport: Optional[MultiTransport] = None
        self._message_queue: Optional[PersistentMessageQueue] = None
        self._heartbeat_validator: Optional[HeartbeatValidator] = None
        self._cross_repo_sync: Optional[CrossRepoSync] = None

        # Message subscriptions
        self._message_callbacks: List[Callable[[Dict[str, Any]], Coroutine]] = []

        # Metrics
        self._total_commands_received = 0
        self._total_evolutions_completed = 0
        self._total_evolutions_failed = 0
        self._total_messages_sent = 0
        self._total_messages_received = 0
        self._heartbeat_failures = 0

    async def initialize(self, council: "UnifiedCodingCouncil") -> bool:
        """
        Initialize the Trinity bridge.

        Args:
            council: The UnifiedCodingCouncil instance to connect

        Returns:
            True if initialization succeeded
        """
        if self._initialized:
            logger.debug("[CodingCouncilTrinity] Already initialized")
            return True

        if not _is_enabled():
            logger.info("[CodingCouncilTrinity] Coding Council disabled via configuration")
            return False

        self._council = council

        logger.info("=" * 60)
        logger.info("CODING COUNCIL: Initializing Trinity Bridge")
        logger.info("=" * 60)

        try:
            # Step 1: Get Reactor Bridge
            self._reactor_bridge = await self._get_reactor_bridge()
            if self._reactor_bridge is None:
                logger.warning("[CodingCouncilTrinity] ReactorCoreBridge not available")
                # Continue without bridge - local-only mode

            # Step 2: Register command handlers
            self._register_command_handlers()

            # Step 3: Ensure Trinity directories exist
            self._ensure_directories()

            # Step 4: Initialize v77.0 Trinity Modules
            await self._initialize_trinity_modules()

            # Step 5: Write initial state
            await self._write_state()

            # Step 6: Start status broadcast loop
            self._status_task = asyncio.create_task(self._status_broadcast_loop())

            # Step 7: Start message processor
            self._message_processor_task = asyncio.create_task(self._message_processor_loop())

            # Step 8: Register with Reactor Bridge if available
            if self._reactor_bridge:
                await self._register_with_reactor()

            self._initialized = True

            logger.info("=" * 60)
            logger.info("CODING COUNCIL: Trinity Bridge Online")
            logger.info(f"  Cross-Repo: {_is_cross_repo_enabled()}")
            logger.info(f"  Auto-Approve: {_is_auto_approve_enabled()}")
            logger.info(f"  Trinity Modules: {TRINITY_MODULE_AVAILABLE}")
            logger.info("=" * 60)

            return True

        except Exception as e:
            logger.error(f"[CodingCouncilTrinity] Initialization failed: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown the Trinity bridge."""
        if not self._initialized:
            return

        logger.info("[CodingCouncilTrinity] Shutting down...")

        # Cancel status broadcast
        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except asyncio.CancelledError:
                pass

        # Cancel message processor
        if self._message_processor_task:
            self._message_processor_task.cancel()
            try:
                await self._message_processor_task
            except asyncio.CancelledError:
                pass

        # Shutdown Trinity modules in reverse order
        await self._shutdown_trinity_modules()

        # Write final state
        await self._write_state(status="offline")

        self._initialized = False
        logger.info("[CodingCouncilTrinity] Shutdown complete")

    async def _get_reactor_bridge(self):
        """Lazy import of ReactorCoreBridge."""
        try:
            from backend.system.reactor_bridge import get_reactor_bridge
            return get_reactor_bridge()
        except ImportError:
            try:
                from system.reactor_bridge import get_reactor_bridge
                return get_reactor_bridge()
            except ImportError:
                return None

    def _ensure_directories(self) -> None:
        """Ensure required directories exist."""
        _ensure_trinity_dirs()

    # =========================================================================
    # v77.0 Trinity Module Management
    # =========================================================================

    async def _initialize_trinity_modules(self) -> None:
        """Initialize all v77.0 Trinity modules.

        v253.7: Added per-step timeouts to prevent startup stall. Each module
        init is individually timed so a single hanging module doesn't block
        the rest.
        """
        if not TRINITY_MODULE_AVAILABLE:
            logger.info("[CodingCouncilTrinity] Trinity modules not available, using fallback mode")
            return

        logger.info("[CodingCouncilTrinity] Initializing v77.0 Trinity modules...")
        _step_timeout = float(os.getenv("JARVIS_CC_TRINITY_STEP_TIMEOUT", "15"))

        try:
            # 1. Initialize Multi-Transport (Gap #1)
            # v90.0: Fixed parameter names to match MultiTransport.__init__ signature
            trinity_dir = _get_trinity_dir()
            self._multi_transport = MultiTransport(
                redis_url=os.getenv("REDIS_URL"),
                websocket_url=os.getenv("TRINITY_WEBSOCKET_URL"),
                file_base_dir=trinity_dir / "messages",  # v90.0: Fixed from file_transport_dir
            )
            try:
                await asyncio.wait_for(self._multi_transport.start(), timeout=_step_timeout)
                logger.info("[CodingCouncilTrinity] MultiTransport started")
            except asyncio.TimeoutError:
                logger.warning(f"[CodingCouncilTrinity] MultiTransport start timed out ({_step_timeout}s)")

            # 2. Initialize Message Queue (Gap #4)
            # v90.0: Fixed parameters to match PersistentMessageQueue.__init__ signature
            # PersistentMessageQueue only takes db_path
            queue_db = trinity_dir / "messages" / "coding_council_queue.db"
            self._message_queue = PersistentMessageQueue(
                db_path=queue_db,
            )
            try:
                await asyncio.wait_for(self._message_queue.start(), timeout=_step_timeout)
                logger.info("[CodingCouncilTrinity] MessageQueue started")
            except asyncio.TimeoutError:
                logger.warning(f"[CodingCouncilTrinity] MessageQueue start timed out ({_step_timeout}s)")

            # 3. Initialize Heartbeat Validator (Gaps #2, #3)
            # v90.0: Fixed parameter to match HeartbeatValidator.__init__ signature
            # HeartbeatValidator only takes heartbeat_dir, other config is internal
            heartbeat_dir = trinity_dir / "heartbeats"
            self._heartbeat_validator = HeartbeatValidator(
                heartbeat_dir=heartbeat_dir,
            )
            try:
                await asyncio.wait_for(self._heartbeat_validator.start(), timeout=_step_timeout)
                self._heartbeat_validator.on_staleness(self._on_component_stale)
                logger.info("[CodingCouncilTrinity] HeartbeatValidator started")
            except asyncio.TimeoutError:
                logger.warning(f"[CodingCouncilTrinity] HeartbeatValidator start timed out ({_step_timeout}s)")

            # 4. Initialize Cross-Repo Sync (Gaps #5, #6, #7)
            # v90.0: Fixed - CrossRepoSync.__init__ takes no parameters
            # It uses DEFAULT_PATHS and can be configured via environment variables
            if _is_cross_repo_enabled():
                self._cross_repo_sync = CrossRepoSync()

                # Wire up sync events to Trinity network
                self._cross_repo_sync.on_event(self._on_sync_event)

                _cross_repo_timeout = float(os.getenv("JARVIS_CC_CROSS_REPO_TIMEOUT", "35"))
                try:
                    await asyncio.wait_for(self._cross_repo_sync.start(), timeout=_cross_repo_timeout)
                    logger.info("[CodingCouncilTrinity] CrossRepoSync started")
                except asyncio.TimeoutError:
                    logger.warning(f"[CodingCouncilTrinity] CrossRepoSync start timed out ({_cross_repo_timeout}s)")

            logger.info("[CodingCouncilTrinity] All Trinity modules initialized")

        except Exception as e:
            logger.error(f"[CodingCouncilTrinity] Trinity module init failed: {e}")
            # Graceful degradation - continue without failed modules

    async def _on_sync_event(self, event: SyncEvent) -> None:
        """
        Handle events from CrossRepoSync.
        
        This forwards critical sync events (like recovery triggers) to the Trinity network.
        
        v93.1: Fixed to use TransportMessage object instead of keyword arguments.
        """
        try:
            logger.debug(f"[CodingCouncilTrinity] Sync event: {event.event_type} from {event.source_repo}")
            
            # Forward recovery requests to supervisor via MultiTransport
            if event.event_type == "recovery_triggered":
                logger.info(f"[CodingCouncilTrinity] Forwarding recovery request for {event.target_repo}")
                
                # Send high-priority message using TransportMessage object
                if self._multi_transport and TRINITY_MODULE_AVAILABLE:
                    message = TransportMessage(
                        channel="system.recovery",
                        payload={
                            "component": event.target_repo,
                            "reason": event.payload.get("reason", "unknown"),
                            "timestamp": event.timestamp,
                            "source": "coding_council"
                        },
                        priority=1,  # Lower number = higher priority (1 = HIGH)
                    )
                    await self._multi_transport.send(message)
                    self._total_messages_sent += 1
                    logger.debug(f"[CodingCouncilTrinity] Recovery message sent: {message.id}")
                    
        except Exception as e:
            logger.error(f"[CodingCouncilTrinity] Failed to handle sync event: {e}")

    async def _shutdown_trinity_modules(self) -> None:
        """Shutdown Trinity modules in reverse order."""
        if not TRINITY_MODULE_AVAILABLE:
            return

        logger.info("[CodingCouncilTrinity] Shutting down Trinity modules...")

        # Shutdown in reverse order
        if self._cross_repo_sync:
            try:
                await self._cross_repo_sync.stop()
            except Exception as e:
                logger.error(f"[CodingCouncilTrinity] CrossRepoSync shutdown error: {e}")
            self._cross_repo_sync = None

        if self._heartbeat_validator:
            try:
                await self._heartbeat_validator.stop()
            except Exception as e:
                logger.error(f"[CodingCouncilTrinity] HeartbeatValidator shutdown error: {e}")
            self._heartbeat_validator = None

        if self._message_queue:
            try:
                await self._message_queue.stop()
            except Exception as e:
                logger.error(f"[CodingCouncilTrinity] MessageQueue shutdown error: {e}")
            self._message_queue = None

        if self._multi_transport:
            try:
                await self._multi_transport.stop()
            except Exception as e:
                logger.error(f"[CodingCouncilTrinity] MultiTransport shutdown error: {e}")
            self._multi_transport = None

        logger.info("[CodingCouncilTrinity] Trinity modules shutdown complete")

    async def _on_component_stale(self, component: str, last_seen: float) -> None:
        """Handle stale component detection."""
        self._heartbeat_failures += 1
        staleness_seconds = time.time() - last_seen
        logger.warning(
            f"[CodingCouncilTrinity] Component '{component}' is stale "
            f"(last seen {staleness_seconds:.1f}s ago)"
        )

        # If it's one of our repos, attempt to recover
        if self._cross_repo_sync and component in ["jarvis_prime", "reactor_core"]:
            await self._cross_repo_sync.trigger_recovery(component)

    async def _message_processor_loop(self) -> None:
        """Process incoming messages from the queue."""
        if not self._message_queue:
            return

        logger.info("[CodingCouncilTrinity] Message processor started")

        while True:
            try:
                # Dequeue next message
                message = await self._message_queue.dequeue(timeout=5.0)
                if message is None:
                    continue

                self._total_messages_received += 1

                # Parse and route the message
                try:
                    payload = message.payload
                    if isinstance(payload, str):
                        payload = json.loads(payload)

                    # Check if it's a Coding Council command
                    if "intent" in payload:
                        command = CodingCouncilCommand.from_dict(payload)
                        result = await self.handle_command(command)

                        # Send response if correlation_id present
                        if command.correlation_id:
                            await self._send_response(command.correlation_id, result)

                    # Notify callbacks
                    for callback in self._message_callbacks:
                        try:
                            await callback(payload)
                        except Exception as e:
                            logger.error(f"[CodingCouncilTrinity] Callback error: {e}")

                    # Mark as processed
                    await self._message_queue.ack(message.id)

                except Exception as e:
                    logger.error(f"[CodingCouncilTrinity] Message processing error: {e}")
                    await self._message_queue.nack(message.id, requeue=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CodingCouncilTrinity] Message loop error: {e}")
                await asyncio.sleep(1.0)

    async def _send_response(self, correlation_id: str, result: Dict[str, Any]) -> None:
        """Send a response message."""
        if not self._multi_transport:
            return

        try:
            response = {
                "type": "response",
                "correlation_id": correlation_id,
                "timestamp": time.time(),
                "result": result,
            }
            await self._multi_transport.send(
                topic="coding_council_responses",
                payload=response,
            )
            self._total_messages_sent += 1
        except Exception as e:
            logger.error(f"[CodingCouncilTrinity] Failed to send response: {e}")

    # =========================================================================
    # Public API for Trinity Communication
    # =========================================================================

    async def send_evolution_event(
        self,
        event_type: str,
        task_id: str,
        data: Dict[str, Any],
    ) -> bool:
        """
        Send an evolution event to the Trinity network.

        Args:
            event_type: Type of event (started, completed, failed, etc.)
            task_id: The evolution task ID
            data: Event payload data

        Returns:
            True if sent successfully
        """
        if not self._multi_transport:
            return False

        try:
            event = {
                "type": "evolution_event",
                "event_type": event_type,
                "task_id": task_id,
                "component": "coding_council",
                "timestamp": time.time(),
                "data": data,
            }
            await self._multi_transport.send(
                topic="coding_council_events",
                payload=event,
            )
            self._total_messages_sent += 1
            return True
        except Exception as e:
            logger.error(f"[CodingCouncilTrinity] Failed to send event: {e}")
            return False

    async def queue_command(
        self,
        command: CodingCouncilCommand,
        priority: Optional[int] = None,
    ) -> bool:
        """
        Queue a command for processing.

        Args:
            command: The command to queue
            priority: Optional priority (lower = higher priority)

        Returns:
            True if queued successfully
        """
        if not self._message_queue:
            # Process immediately if no queue
            await self.handle_command(command)
            return True

        try:
            await self._message_queue.enqueue(
                payload=command.to_dict(),
                priority=priority or 50,
                correlation_id=command.correlation_id,
            )
            return True
        except Exception as e:
            logger.error(f"[CodingCouncilTrinity] Failed to queue command: {e}")
            return False

    def on_message(self, callback: Callable[[Dict[str, Any]], Coroutine]) -> None:
        """Register a message callback."""
        self._message_callbacks.append(callback)

    async def get_repo_status(self, repo_name: str) -> Optional[Dict[str, Any]]:
        """
        Get the current status of a repository.

        Args:
            repo_name: Name of the repo (jarvis, jarvis_prime, reactor_core)

        Returns:
            Status dict or None if not available
        """
        if not self._cross_repo_sync:
            return None

        return await self._cross_repo_sync.get_repo_state(repo_name)

    async def get_component_health(self, component: str) -> Optional[Dict[str, Any]]:
        """
        Get the health status of a Trinity component.

        Args:
            component: Component name

        Returns:
            Health dict or None if not available
        """
        if not self._heartbeat_validator:
            return None

        return await self._heartbeat_validator.get_component_health(component)

    async def get_transport_status(self) -> Dict[str, Any]:
        """Get the current transport status."""
        if not self._multi_transport:
            return {"available": False, "fallback_mode": True}

        return await self._multi_transport.get_status()

    def _register_command_handlers(self) -> None:
        """Register handlers for all Coding Council intents."""
        self._command_handlers = {
            CodingCouncilIntent.EVOLVE_CODE: self._handle_evolve_code,
            CodingCouncilIntent.ABORT_EVOLUTION: self._handle_abort_evolution,
            CodingCouncilIntent.ROLLBACK_EVOLUTION: self._handle_rollback_evolution,
            CodingCouncilIntent.GET_EVOLUTION_STATUS: self._handle_get_status,
            CodingCouncilIntent.GET_ACTIVE_TASKS: self._handle_get_active_tasks,
            CodingCouncilIntent.GET_FRAMEWORK_STATUS: self._handle_get_framework_status,
            CodingCouncilIntent.CROSS_REPO_EVOLVE: self._handle_cross_repo_evolve,
            CodingCouncilIntent.SYNC_REPOS: self._handle_sync_repos,
            CodingCouncilIntent.UPDATE_CONFIG: self._handle_update_config,
            CodingCouncilIntent.ENABLE_FRAMEWORK: self._handle_enable_framework,
            CodingCouncilIntent.DISABLE_FRAMEWORK: self._handle_disable_framework,
        }

    async def _register_with_reactor(self) -> None:
        """Register Coding Council command handlers with Reactor Bridge."""
        if not self._reactor_bridge:
            return

        try:
            # Import TrinityIntent to extend it
            from backend.system.reactor_bridge import TrinityIntent

            # Register a general handler for Coding Council commands
            @self._reactor_bridge.on_command("execute_plan")
            async def handle_execute_plan(command):
                """Handle EXECUTE_PLAN commands that may be evolution tasks."""
                payload = command.payload
                if payload.get("type") == "evolution":
                    council_cmd = CodingCouncilCommand.from_dict(payload)
                    return await self.handle_command(council_cmd)

            logger.info("[CodingCouncilTrinity] Registered with Reactor Bridge")

        except Exception as e:
            logger.warning(f"[CodingCouncilTrinity] Could not register with Reactor: {e}")

    async def handle_command(self, command: CodingCouncilCommand) -> Dict[str, Any]:
        """
        Handle a Coding Council command.

        Args:
            command: The command to handle

        Returns:
            Result dict with success status and data
        """
        self._total_commands_received += 1
        intent = command.intent

        handler = self._command_handlers.get(intent)
        if handler is None:
            return {
                "success": False,
                "error": f"Unknown intent: {intent}",
                "command_id": command.id,
            }

        try:
            result = await handler(command)
            return {
                "success": True,
                "data": result,
                "command_id": command.id,
            }
        except Exception as e:
            logger.error(f"[CodingCouncilTrinity] Command {command.id} failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "command_id": command.id,
            }

    # =========================================================================
    # Command Handlers
    # =========================================================================

    async def _handle_evolve_code(self, command: CodingCouncilCommand) -> Dict[str, Any]:
        """Handle EVOLVE_CODE command."""
        if not self._council:
            raise RuntimeError("Coding Council not initialized")

        description = command.description or command.payload.get("description", "")
        if not description:
            raise ValueError("Evolution description required")

        # Execute evolution
        result = await self._council.evolve(
            description=description,
            target_files=command.target_files or None,
            require_sandbox=command.require_sandbox,
            require_planning=command.require_planning,
            require_approval=command.require_approval and not _is_auto_approve_enabled(),
            correlation_id=command.correlation_id,
            timeout=command.timeout_seconds,
        )

        # Update metrics
        if result.success:
            self._total_evolutions_completed += 1
        else:
            self._total_evolutions_failed += 1

        return self._evolution_result_to_dict(result)

    async def _handle_abort_evolution(self, command: CodingCouncilCommand) -> Dict[str, Any]:
        """Handle ABORT_EVOLUTION command."""
        task_id = command.payload.get("task_id")
        if not task_id:
            raise ValueError("task_id required for abort")

        if not self._council:
            raise RuntimeError("Coding Council not initialized")

        # Get the task and abort it
        task = self._active_evolutions.get(task_id)
        if not task:
            return {"aborted": False, "reason": "Task not found or already completed"}

        # Mark as aborted (actual abortion happens in orchestrator)
        return {"aborted": True, "task_id": task_id}

    async def _handle_rollback_evolution(self, command: CodingCouncilCommand) -> Dict[str, Any]:
        """Handle ROLLBACK_EVOLUTION command."""
        task_id = command.payload.get("task_id")
        if not task_id:
            raise ValueError("task_id required for rollback")

        if not self._council:
            raise RuntimeError("Coding Council not initialized")

        # Trigger rollback
        success = await self._council._rollback_manager.rollback(task_id, "User requested")

        return {"rolled_back": success, "task_id": task_id}

    async def _handle_get_status(self, command: CodingCouncilCommand) -> Dict[str, Any]:
        """Handle GET_EVOLUTION_STATUS command."""
        task_id = command.payload.get("task_id")

        if not self._council:
            return {"status": "not_initialized"}

        status = self._council.get_status()

        if task_id:
            # Get specific task status
            task = self._active_evolutions.get(task_id)
            if task:
                status["task"] = {
                    "id": task.task_id,
                    "status": task.status.value,
                    "description": task.description[:100],
                }

        return status

    async def _handle_get_active_tasks(self, command: CodingCouncilCommand) -> Dict[str, Any]:
        """Handle GET_ACTIVE_TASKS command."""
        return {
            "active_tasks": [
                {
                    "id": task.task_id,
                    "description": task.description[:100],
                    "status": task.status.value,
                    "target_files": task.target_files[:5],
                }
                for task in self._active_evolutions.values()
            ],
            "count": len(self._active_evolutions),
        }

    async def _handle_get_framework_status(self, command: CodingCouncilCommand) -> Dict[str, Any]:
        """Handle GET_FRAMEWORK_STATUS command."""
        if not self._council:
            return {"frameworks": {}}

        framework_status = {}
        for name, adapter in [
            ("aider", self._council._aider),
            ("repomaster", self._council._repomaster),
            ("metagpt", self._council._metagpt),
            ("openhands", self._council._openhands),
            ("continue", self._council._continue),
        ]:
            if adapter:
                available = await adapter.is_available()
                framework_status[name] = {
                    "available": available,
                    "enabled": self._council.config.enabled_frameworks.get(name, True),
                }

        return {"frameworks": framework_status}

    async def _handle_cross_repo_evolve(self, command: CodingCouncilCommand) -> Dict[str, Any]:
        """Handle CROSS_REPO_EVOLVE command for multi-repo evolution."""
        if not _is_cross_repo_enabled():
            raise RuntimeError("Cross-repo evolution disabled")

        if not self._council:
            raise RuntimeError("Coding Council not initialized")

        description = command.description or command.payload.get("description", "")
        target_repos = command.target_repos or ["jarvis"]

        results = {}

        # Map repo names to paths (dynamic from unified config)
        repos = _get_trinity_repos()
        repo_map = {
            "jarvis": repos.get("jarvis"),
            "jarvis_prime": repos.get("j_prime"),
            "jprime": repos.get("j_prime"),
            "reactor_core": repos.get("reactor_core"),
            "reactor": repos.get("reactor_core"),
        }

        for repo_name in target_repos:
            repo_path = repo_map.get(repo_name.lower())
            if not repo_path or not repo_path.exists():
                results[repo_name] = {"success": False, "error": f"Repo not found: {repo_name}"}
                continue

            # Create a task for this repo
            # Note: This is a simplified version - full implementation would
            # create separate CodingCouncil instances for each repo
            logger.info(f"[CodingCouncilTrinity] Cross-repo evolve: {repo_name}")

            result = await self._council.evolve(
                description=f"[{repo_name}] {description}",
                target_files=command.target_files,
                require_approval=not _is_auto_approve_enabled(),
                correlation_id=f"{command.correlation_id}-{repo_name}" if command.correlation_id else None,
            )

            results[repo_name] = self._evolution_result_to_dict(result)

        return {"results": results}

    async def _handle_sync_repos(self, command: CodingCouncilCommand) -> Dict[str, Any]:
        """Handle SYNC_REPOS command to synchronize Trinity repos."""
        if not _is_cross_repo_enabled():
            raise RuntimeError("Cross-repo operations disabled")

        # Use CrossRepoSync if available
        if self._cross_repo_sync:
            # Trigger sync across all repos
            sync_status = await self._cross_repo_sync.sync_all()
            return {
                "repos": {
                    name: {
                        "exists": state.exists,
                        "healthy": state.healthy,
                        "branch": state.branch,
                        "has_uncommitted_changes": state.has_uncommitted_changes,
                        "last_commit": state.last_commit_hash[:8] if state.last_commit_hash else None,
                        "path": str(state.path),
                    }
                    for name, state in sync_status.items()
                },
                "sync_timestamp": time.time(),
            }

        # Fallback to basic sync
        sync_results = {}

        # Check each repo's status (from dynamic config)
        repos = _get_trinity_repos()

        for name, path in repos.items():
            if path.exists():
                # Check git status
                try:
                    import subprocess
                    result = subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=str(path),
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )

                    has_changes = bool(result.stdout.strip())
                    sync_results[name] = {
                        "exists": True,
                        "has_uncommitted_changes": has_changes,
                        "path": str(path),
                    }
                except Exception as e:
                    sync_results[name] = {"exists": True, "error": str(e)}
            else:
                sync_results[name] = {"exists": False, "path": str(path)}

        return {"repos": sync_results}

    async def _handle_update_config(self, command: CodingCouncilCommand) -> Dict[str, Any]:
        """Handle UPDATE_CONFIG command."""
        if not self._council:
            raise RuntimeError("Coding Council not initialized")

        config_updates = command.payload.get("config", {})

        # Apply allowed updates (sanitized)
        if "execution_timeout" in config_updates:
            self._council.config.execution_timeout = float(config_updates["execution_timeout"])
        if "max_retries" in config_updates:
            self._council.config.max_retries = int(config_updates["max_retries"])
        if "require_validation" in config_updates:
            self._council.config.require_validation = bool(config_updates["require_validation"])

        return {"updated": True, "config": self._council.config.__dict__}

    async def _handle_enable_framework(self, command: CodingCouncilCommand) -> Dict[str, Any]:
        """Handle ENABLE_FRAMEWORK command."""
        framework = command.payload.get("framework", "").lower()
        if not framework:
            raise ValueError("framework name required")

        if not self._council:
            raise RuntimeError("Coding Council not initialized")

        self._council.config.enabled_frameworks[framework] = True

        return {"framework": framework, "enabled": True}

    async def _handle_disable_framework(self, command: CodingCouncilCommand) -> Dict[str, Any]:
        """Handle DISABLE_FRAMEWORK command."""
        framework = command.payload.get("framework", "").lower()
        if not framework:
            raise ValueError("framework name required")

        if not self._council:
            raise RuntimeError("Coding Council not initialized")

        self._council.config.enabled_frameworks[framework] = False

        return {"framework": framework, "enabled": False}

    # =========================================================================
    # State Management
    # =========================================================================

    async def _write_state(self, status: str = "online") -> None:
        """Write current state to Trinity directory."""
        try:
            # Use atomic write to prevent corruption
            from backend.system.trinity_initializer import write_json_atomic
        except ImportError:
            try:
                from system.trinity_initializer import write_json_atomic
            except ImportError:
                # Fallback to standard write
                import json
                def write_json_atomic(path, data):
                    with open(path, 'w') as f:
                        json.dump(data, f, indent=2)
                    return True

        # Get dynamic config values
        repos = _get_trinity_repos()

        state = {
            "component": "coding_council",
            "version": "77.0",
            "status": status,
            "timestamp": time.time(),
            "pid": os.getpid(),
            "metrics": {
                "commands_received": self._total_commands_received,
                "evolutions_completed": self._total_evolutions_completed,
                "evolutions_failed": self._total_evolutions_failed,
                "active_tasks": len(self._active_evolutions),
                "messages_sent": self._total_messages_sent,
                "messages_received": self._total_messages_received,
                "heartbeat_failures": self._heartbeat_failures,
            },
            "config": {
                "cross_repo_enabled": _is_cross_repo_enabled(),
                "auto_approve": _is_auto_approve_enabled(),
                "status_broadcast_interval": _get_status_broadcast_interval(),
            },
            "trinity_modules": {
                "available": TRINITY_MODULE_AVAILABLE,
                "multi_transport": self._multi_transport is not None,
                "message_queue": self._message_queue is not None,
                "heartbeat_validator": self._heartbeat_validator is not None,
                "cross_repo_sync": self._cross_repo_sync is not None,
            },
            "repos": {name: str(path) for name, path in repos.items()},
        }

        write_json_atomic(_get_state_file(), state)

    async def _status_broadcast_loop(self) -> None:
        """Periodically broadcast status to Trinity network.

        v78.0: Enhanced with automatic recovery and error tracking.
        - Logs errors at warning level (not debug) for visibility
        - Tracks consecutive failures
        - Continues operation despite errors
        - Writes heartbeat even if broadcast fails
        """
        consecutive_failures = 0
        max_consecutive_failures = 10

        while True:
            try:
                await asyncio.sleep(_get_status_broadcast_interval())

                # ALWAYS write state file first - this is what health checks look for
                try:
                    await self._write_state()
                    consecutive_failures = 0  # Reset on success
                except Exception as write_error:
                    consecutive_failures += 1
                    logger.warning(
                        f"[CodingCouncilTrinity] State write failed "
                        f"(attempt {consecutive_failures}): {write_error}"
                    )

                # Broadcast via multi-transport (primary) or Reactor Bridge (fallback)
                # This can fail without affecting health status
                try:
                    await self._broadcast_status()
                except Exception as broadcast_error:
                    logger.debug(f"[CodingCouncilTrinity] Broadcast failed: {broadcast_error}")

                # Update heartbeat validator
                if self._heartbeat_validator:
                    try:
                        await self._heartbeat_validator.send_heartbeat()
                    except Exception as hb_error:
                        logger.debug(f"[CodingCouncilTrinity] Heartbeat validator failed: {hb_error}")

                # Log warning if we're having persistent issues
                if consecutive_failures >= max_consecutive_failures:
                    logger.error(
                        f"[CodingCouncilTrinity] {consecutive_failures} consecutive state write failures! "
                        f"Health check may mark CodingCouncil as unhealthy."
                    )
                    # Reset counter to avoid log spam
                    consecutive_failures = 0

            except asyncio.CancelledError:
                # Write final state before shutdown
                try:
                    await self._write_state(status="shutting_down")
                except Exception:
                    pass
                break
            except Exception as e:
                # Catch-all for any unexpected errors - keep the loop running
                consecutive_failures += 1
                logger.error(
                    f"[CodingCouncilTrinity] Unexpected error in status loop "
                    f"(attempt {consecutive_failures}): {e}"
                )

    async def _broadcast_status(self) -> None:
        """Broadcast status update to Trinity network."""
        if not self._council:
            return

        try:
            status = self._council.get_status()
            status["component"] = "coding_council"
            status["timestamp"] = time.time()
            status["pid"] = os.getpid()

            # Add Trinity module status
            status["trinity_modules"] = {
                "multi_transport": self._multi_transport is not None,
                "message_queue": self._message_queue is not None,
                "heartbeat_validator": self._heartbeat_validator is not None,
                "cross_repo_sync": self._cross_repo_sync is not None,
            }

            # Primary: Use multi-transport
            if self._multi_transport:
                await self._multi_transport.send(
                    topic="coding_council_status",
                    payload=status,
                )
                self._total_messages_sent += 1
                return

            # Fallback: Use Reactor Bridge
            if self._reactor_bridge:
                await self._reactor_bridge.publish_heartbeat_async(
                    extra_data={"coding_council": status}
                )

        except Exception as e:
            logger.debug(f"[CodingCouncilTrinity] Broadcast failed: {e}")

    def _evolution_result_to_dict(self, result: "EvolutionResult") -> Dict[str, Any]:
        """Convert EvolutionResult to serializable dict."""
        return {
            "success": result.success,
            "task_id": result.task_id,
            "execution_time_ms": result.execution_time_ms,
            "changes_made": result.changes_made,
            "files_modified": result.files_modified,
            "validation_passed": result.validation_report.passed if result.validation_report else True,
            "error": result.error,
            "framework_used": result.framework_used.value if result.framework_used else None,
            "rollback_available": result.rollback_id is not None,
        }


# =============================================================================
# Global Instance Management
# =============================================================================

_trinity_bridge: Optional[CodingCouncilTrinityBridge] = None


async def get_coding_council_bridge() -> CodingCouncilTrinityBridge:
    """Get or create the global Trinity bridge instance."""
    global _trinity_bridge

    if _trinity_bridge is None:
        _trinity_bridge = CodingCouncilTrinityBridge()

    return _trinity_bridge


async def initialize_coding_council_trinity(council: "UnifiedCodingCouncil") -> bool:
    """
    Initialize the Coding Council Trinity integration.

    This should be called after UnifiedCodingCouncil is initialized.

    v253.7: Added overall timeout to prevent startup stall.

    Args:
        council: The initialized UnifiedCodingCouncil instance

    Returns:
        True if initialization succeeded
    """
    _init_timeout = float(os.getenv("JARVIS_CC_TRINITY_INIT_TIMEOUT", "60"))
    bridge = await get_coding_council_bridge()
    try:
        return await asyncio.wait_for(bridge.initialize(council), timeout=_init_timeout)
    except asyncio.TimeoutError:
        logger.warning(
            f"[CodingCouncilTrinity] bridge.initialize() timed out after {_init_timeout}s "
            "— continuing without full Trinity integration"
        )
        return False


async def shutdown_coding_council_trinity() -> None:
    """Shutdown the Coding Council Trinity integration."""
    global _trinity_bridge

    if _trinity_bridge is not None:
        await _trinity_bridge.shutdown()
        _trinity_bridge = None
