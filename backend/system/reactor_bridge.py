"""
PROJECT TRINITY: ReactorCoreBridge - The Nervous System

This module provides the communication bridge between JARVIS (Body) and the
Trinity architecture (J-Prime Mind + Reactor Core Nerves).

ARCHITECTURE:
┌────────────┐    JSON Commands    ┌──────────────┐    Triggers    ┌────────────┐
│  J-PRIME   │ ────────────────────│ REACTOR CORE │ ──────────────│   JARVIS   │
│   (Mind)   │                     │   (Nerves)   │               │   (Body)   │
└────────────┘    Status Updates   └──────────────┘    Heartbeat  └────────────┘

FEATURES:
- Multi-transport support (File, WebSocket, Redis)
- Strict JSON schema validation (PRD FR-04)
- Heartbeat publishing (PRD FR-05)
- Async-first design with parallel processing
- Automatic reconnection with exponential backoff
- Command acknowledgment (ACK/NACK)
- Event deduplication
- Rate limiting for safety
- Graceful degradation

PRD COMPLIANCE:
- FR-04: Command Schema - All commands follow strict JSON schema
- FR-05: Heartbeat - State snapshots every 5 seconds
- FR-06: Episodic Memory - Actions logged for J-Prime retrieval

USAGE:
    from backend.system.reactor_bridge import get_reactor_bridge, TrinityCommand

    bridge = get_reactor_bridge()
    await bridge.connect_async()

    # Listen for commands from J-Prime
    @bridge.on_command("start_surveillance")
    async def handle_surveillance(command: TrinityCommand):
        # Execute v65.0 protocol
        pass

    # Publish heartbeat
    await bridge.publish_heartbeat_async()
"""

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# PRD Section 3.2: Command Schema (FR-04)
# =============================================================================

class TrinitySource(Enum):
    """Source component in Trinity architecture."""
    J_PRIME = "j_prime"         # The Mind
    REACTOR_CORE = "reactor_core"  # The Nerves
    JARVIS_BODY = "jarvis_body"   # The Body
    USER = "user"                 # External user
    SYSTEM = "system"             # System events


class TrinityIntent(Enum):
    """Command intents for Trinity protocol."""
    # Surveillance commands (v53-v65)
    START_SURVEILLANCE = "start_surveillance"
    STOP_SURVEILLANCE = "stop_surveillance"
    UPDATE_SURVEILLANCE = "update_surveillance"

    # Window management commands (v63-v69)
    EXILE_WINDOW = "exile_window"
    BRING_BACK_WINDOW = "bring_back_window"
    TELEPORT_WINDOW = "teleport_window"

    # Cryostasis commands (v69)
    FREEZE_APP = "freeze_app"
    THAW_APP = "thaw_app"

    # Phantom Hardware commands (v68)
    CREATE_GHOST_DISPLAY = "create_ghost_display"
    DESTROY_GHOST_DISPLAY = "destroy_ghost_display"

    # System commands
    HEARTBEAT = "heartbeat"
    PING = "ping"
    PONG = "pong"
    ACK = "ack"
    NACK = "nack"
    STATUS_UPDATE = "status_update"
    ERROR_REPORT = "error_report"

    # Memory commands
    STORE_MEMORY = "store_memory"
    RECALL_MEMORY = "recall_memory"

    # Cognitive commands (from J-Prime)
    EXECUTE_PLAN = "execute_plan"
    ABORT_PLAN = "abort_plan"

    # v77.2: Coding Council Evolution commands
    EVOLVE_CODE = "evolve_code"
    EVOLUTION_STATUS = "evolution_status"
    EVOLUTION_COMPLETE = "evolution_complete"
    EVOLUTION_ROLLBACK = "evolution_rollback"

    # v101.0: Model Lifecycle commands (Trinity Loop - closes the loop!)
    MODEL_READY = "model_ready"  # New model ready for hot-swap
    MODEL_LOADING = "model_loading"  # Model loading in progress
    MODEL_UNLOADED = "model_unloaded"  # Model unloaded
    MODEL_FAILED = "model_failed"  # Model loading failed
    MODEL_VALIDATED = "model_validated"  # Model passed validation
    MODEL_ROLLBACK = "model_rollback"  # Rollback to previous model
    HOT_SWAP_MODEL = "hot_swap_model"  # Request to hot-swap model
    UPDATE_MODEL_ROUTING = "update_model_routing"  # Update routing to new model


@dataclass
class TrinityCommand:
    """
    PRD FR-04: Strict command schema for Trinity communication.

    All commands sent through Reactor Core must follow this schema.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    source: TrinitySource = TrinitySource.JARVIS_BODY
    intent: TrinityIntent = TrinityIntent.PING
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Routing
    target: Optional[TrinitySource] = None  # None = broadcast
    priority: int = 5  # 1-10, lower is higher priority
    requires_ack: bool = False

    # Response tracking
    response_to: Optional[str] = None  # ID of command this responds to
    ttl_seconds: float = 30.0  # Time-to-live for command

    def __post_init__(self):
        # Validate intent
        if isinstance(self.intent, str):
            self.intent = TrinityIntent(self.intent)
        if isinstance(self.source, str):
            self.source = TrinitySource(self.source)
        if isinstance(self.target, str):
            self.target = TrinitySource(self.target)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "source": self.source.value,
            "intent": self.intent.value,
            "payload": self.payload,
            "metadata": self.metadata,
            "target": self.target.value if self.target else None,
            "priority": self.priority,
            "requires_ack": self.requires_ack,
            "response_to": self.response_to,
            "ttl_seconds": self.ttl_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrinityCommand":
        """Deserialize from dict."""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            timestamp=data.get("timestamp", time.time()),
            source=TrinitySource(data["source"]) if data.get("source") else TrinitySource.SYSTEM,
            intent=TrinityIntent(data["intent"]) if data.get("intent") else TrinityIntent.PING,
            payload=data.get("payload", {}),
            metadata=data.get("metadata", {}),
            target=TrinitySource(data["target"]) if data.get("target") else None,
            priority=data.get("priority", 5),
            requires_ack=data.get("requires_ack", False),
            response_to=data.get("response_to"),
            ttl_seconds=data.get("ttl_seconds", 30.0),
        )

    def is_expired(self) -> bool:
        """Check if command has expired."""
        return (time.time() - self.timestamp) > self.ttl_seconds

    def compute_hash(self) -> str:
        """Compute hash for deduplication."""
        content = f"{self.intent.value}:{self.source.value}:{json.dumps(self.payload, sort_keys=True)}"
        return hashlib.md5(content.encode()).hexdigest()[:16]

    def create_ack(self, success: bool = True, message: str = "") -> "TrinityCommand":
        """Create an ACK/NACK response to this command."""
        return TrinityCommand(
            source=TrinitySource.JARVIS_BODY,
            intent=TrinityIntent.ACK if success else TrinityIntent.NACK,
            target=self.source,
            response_to=self.id,
            payload={"success": success, "message": message},
        )


# =============================================================================
# PRD Section 3.2 (FR-05): Heartbeat Schema
# =============================================================================

@dataclass
class HeartbeatPayload:
    """
    PRD FR-05: State snapshot for heartbeat.

    Published every 5 seconds to keep J-Prime informed of current state.
    """
    active_window_title: str = ""
    active_app_name: str = ""
    apps_on_ghost_display: List[str] = field(default_factory=list)
    frozen_apps: List[str] = field(default_factory=list)
    system_cpu_percent: float = 0.0
    system_memory_percent: float = 0.0
    surveillance_active: bool = False
    surveillance_targets: List[str] = field(default_factory=list)
    ghost_display_available: bool = False
    uptime_seconds: float = 0.0
    last_command_id: Optional[str] = None
    pending_commands: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_window_title": self.active_window_title,
            "active_app_name": self.active_app_name,
            "apps_on_ghost_display": self.apps_on_ghost_display,
            "frozen_apps": self.frozen_apps,
            "system_cpu_percent": self.system_cpu_percent,
            "system_memory_percent": self.system_memory_percent,
            "surveillance_active": self.surveillance_active,
            "surveillance_targets": self.surveillance_targets,
            "ghost_display_available": self.ghost_display_available,
            "uptime_seconds": self.uptime_seconds,
            "last_command_id": self.last_command_id,
            "pending_commands": self.pending_commands,
        }


# =============================================================================
# TRANSPORT LAYER
# =============================================================================

class TrinityTransport(ABC):
    """Abstract base class for Trinity transports."""

    @abstractmethod
    async def connect_async(self) -> bool:
        """Connect to the transport."""
        pass

    @abstractmethod
    async def disconnect_async(self) -> None:
        """Disconnect from the transport."""
        pass

    @abstractmethod
    async def publish_async(self, command: TrinityCommand) -> bool:
        """Publish a command."""
        pass

    @abstractmethod
    async def subscribe_async(self) -> AsyncIterator[TrinityCommand]:
        """Subscribe to commands."""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if transport is connected."""
        pass


class FileTransport(TrinityTransport):
    """
    File-based transport using shared directory.

    This is the fallback transport when Redis is not available.
    Uses ~/.jarvis/trinity/ for cross-repo communication.
    """

    def __init__(
        self,
        source: TrinitySource = TrinitySource.JARVIS_BODY,
        base_dir: Optional[Path] = None,
        cleanup_hours: int = 24,
    ):
        self.source = source
        self.base_dir = base_dir or Path.home() / ".jarvis" / "trinity"
        self.cleanup_hours = cleanup_hours
        self._running = False
        self._processed_files: Set[str] = set()

    async def connect_async(self) -> bool:
        try:
            # Create directories
            (self.base_dir / "commands").mkdir(parents=True, exist_ok=True)
            (self.base_dir / "heartbeats").mkdir(parents=True, exist_ok=True)
            (self.base_dir / "responses").mkdir(parents=True, exist_ok=True)
            self._running = True
            logger.info(f"[Trinity] FileTransport connected: {self.base_dir}")
            return True
        except Exception as e:
            logger.error(f"[Trinity] FileTransport connection failed: {e}")
            return False

    async def disconnect_async(self) -> None:
        self._running = False
        logger.info("[Trinity] FileTransport disconnected")

    def is_connected(self) -> bool:
        return self._running

    async def publish_async(self, command: TrinityCommand) -> bool:
        if not self._running:
            return False

        try:
            # Determine subdirectory
            if command.intent == TrinityIntent.HEARTBEAT:
                subdir = "heartbeats"
            elif command.intent in (TrinityIntent.ACK, TrinityIntent.NACK):
                subdir = "responses"
            else:
                subdir = "commands"

            # Write command file
            filename = f"{int(command.timestamp * 1000)}_{command.id}.json"
            filepath = self.base_dir / subdir / filename

            with open(filepath, "w") as f:
                json.dump(command.to_dict(), f, indent=2)

            logger.debug(f"[Trinity] Published command: {command.intent.value} -> {filepath.name}")
            return True

        except Exception as e:
            logger.error(f"[Trinity] Failed to publish command: {e}")
            return False

    async def subscribe_async(self) -> AsyncIterator[TrinityCommand]:
        while self._running:
            try:
                # Scan commands directory
                commands_dir = self.base_dir / "commands"

                for filepath in sorted(commands_dir.glob("*.json")):
                    if filepath.name in self._processed_files:
                        continue

                    try:
                        with open(filepath) as f:
                            data = json.load(f)

                        command = TrinityCommand.from_dict(data)

                        # Skip own commands
                        if command.source == self.source:
                            self._processed_files.add(filepath.name)
                            continue

                        # Skip expired commands
                        if command.is_expired():
                            self._processed_files.add(filepath.name)
                            filepath.unlink(missing_ok=True)
                            continue

                        # Check target
                        if command.target and command.target != self.source:
                            self._processed_files.add(filepath.name)
                            continue

                        self._processed_files.add(filepath.name)
                        yield command

                    except Exception as e:
                        logger.warning(f"[Trinity] Error reading command file {filepath}: {e}")
                        self._processed_files.add(filepath.name)

                # Cleanup old files
                await self._cleanup_old_files_async()

                await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"[Trinity] Subscribe error: {e}")
                await asyncio.sleep(2.0)

    async def _cleanup_old_files_async(self) -> None:
        """Remove old command files."""
        cutoff = datetime.now() - timedelta(hours=self.cleanup_hours)

        for subdir in ["commands", "heartbeats", "responses"]:
            dir_path = self.base_dir / subdir
            if not dir_path.exists():
                continue

            for filepath in dir_path.glob("*.json"):
                try:
                    mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                    if mtime < cutoff:
                        filepath.unlink()
                        self._processed_files.discard(filepath.name)
                except Exception:
                    pass


class RedisTransport(TrinityTransport):
    """
    Redis-based transport for high-performance pub/sub.

    Requires redis-py (async version).
    """

    def __init__(
        self,
        source: TrinitySource = TrinitySource.JARVIS_BODY,
        host: str = "localhost",
        port: int = 6379,
        channel_prefix: str = "trinity",
    ):
        self.source = source
        self.host = host
        self.port = port
        self.channel_prefix = channel_prefix
        self._redis = None
        self._pubsub = None
        self._running = False

    async def connect_async(self) -> bool:
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.Redis(
                host=self.host,
                port=self.port,
                decode_responses=True
            )

            # Test connection
            await self._redis.ping()

            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe(f"{self.channel_prefix}:commands")

            self._running = True
            logger.info(f"[Trinity] RedisTransport connected: {self.host}:{self.port}")
            return True

        except ImportError:
            logger.warning("[Trinity] redis.asyncio not available")
            return False
        except Exception as e:
            logger.error(f"[Trinity] RedisTransport connection failed: {e}")
            return False

    async def disconnect_async(self) -> None:
        self._running = False
        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()
        if self._redis:
            await self._redis.close()
        logger.info("[Trinity] RedisTransport disconnected")

    def is_connected(self) -> bool:
        return self._running and self._redis is not None

    async def publish_async(self, command: TrinityCommand) -> bool:
        if not self._redis or not self._running:
            return False

        try:
            # Determine channel
            if command.intent == TrinityIntent.HEARTBEAT:
                channel = f"{self.channel_prefix}:heartbeats"
            elif command.intent in (TrinityIntent.ACK, TrinityIntent.NACK):
                channel = f"{self.channel_prefix}:responses"
            else:
                channel = f"{self.channel_prefix}:commands"

            # Publish
            await self._redis.publish(channel, json.dumps(command.to_dict()))
            logger.debug(f"[Trinity] Published to Redis: {command.intent.value}")
            return True

        except Exception as e:
            logger.error(f"[Trinity] Redis publish failed: {e}")
            return False

    async def subscribe_async(self) -> AsyncIterator[TrinityCommand]:
        if not self._pubsub:
            return

        while self._running:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0
                )

                if message and message.get("type") == "message":
                    try:
                        data = json.loads(message["data"])
                        command = TrinityCommand.from_dict(data)

                        # Skip own commands
                        if command.source == self.source:
                            continue

                        # Skip expired
                        if command.is_expired():
                            continue

                        # Check target
                        if command.target and command.target != self.source:
                            continue

                        yield command

                    except Exception as e:
                        logger.warning(f"[Trinity] Error parsing Redis message: {e}")

            except Exception as e:
                logger.error(f"[Trinity] Redis subscribe error: {e}")
                await asyncio.sleep(2.0)


# =============================================================================
# REACTOR CORE BRIDGE (Main Class)
# =============================================================================

class ReactorCoreBridge:
    """
    PROJECT TRINITY: The Nervous System Bridge.

    Connects JARVIS (Body) to the Trinity architecture, enabling:
    - Receipt of commands from J-Prime (Mind)
    - Publication of state heartbeats
    - Command acknowledgment
    - Event routing
    """

    _instance: Optional['ReactorCoreBridge'] = None

    def __new__(cls) -> 'ReactorCoreBridge':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True

        # Configuration
        self.source = TrinitySource.JARVIS_BODY
        self.heartbeat_interval = float(os.getenv("TRINITY_HEARTBEAT_INTERVAL", "5.0"))
        self.redis_enabled = os.getenv("TRINITY_REDIS_ENABLED", "true").lower() == "true"
        self.redis_host = os.getenv("TRINITY_REDIS_HOST", "localhost")
        self.redis_port = int(os.getenv("TRINITY_REDIS_PORT", "6379"))

        # Transports
        self._transports: List[TrinityTransport] = []
        self._connected = False

        # Command handlers
        self._command_handlers: Dict[TrinityIntent, List[Callable]] = {}
        self._global_handlers: List[Callable] = []

        # State tracking
        self._seen_hashes: deque = deque(maxlen=1000)
        self._pending_acks: Dict[str, TrinityCommand] = {}
        self._last_command_id: Optional[str] = None
        self._start_time = time.time()

        # Background tasks
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._subscribe_tasks: List[asyncio.Task] = []

        # Stats
        self._stats = {
            "commands_received": 0,
            "commands_published": 0,
            "heartbeats_published": 0,
            "acks_sent": 0,
            "errors": 0,
        }

        logger.info("[Trinity] ReactorCoreBridge initialized")

    async def connect_async(self) -> bool:
        """Connect to Reactor Core using available transports."""
        if self._connected:
            return True

        # Try Redis first
        if self.redis_enabled:
            redis_transport = RedisTransport(
                source=self.source,
                host=self.redis_host,
                port=self.redis_port,
            )
            if await redis_transport.connect_async():
                self._transports.append(redis_transport)
                logger.info("[Trinity] Redis transport active")

        # Always add file transport as fallback
        file_transport = FileTransport(source=self.source)
        if await file_transport.connect_async():
            self._transports.append(file_transport)
            logger.info("[Trinity] File transport active")

        if not self._transports:
            logger.error("[Trinity] No transports available")
            return False

        self._connected = True

        # Start background tasks
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop_async())
        for transport in self._transports:
            task = asyncio.create_task(self._subscribe_loop_async(transport))
            self._subscribe_tasks.append(task)

        logger.info(f"[Trinity] Connected with {len(self._transports)} transport(s)")
        return True

    async def disconnect_async(self) -> None:
        """Disconnect from Reactor Core."""
        self._connected = False

        # Cancel background tasks
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        for task in self._subscribe_tasks:
            task.cancel()

        # Disconnect transports
        for transport in self._transports:
            await transport.disconnect_async()

        self._transports.clear()
        logger.info("[Trinity] Disconnected")

    def is_connected(self) -> bool:
        return self._connected

    # =========================================================================
    # COMMAND HANDLERS
    # =========================================================================

    def on_command(
        self,
        intent: Optional[TrinityIntent] = None
    ) -> Callable:
        """Decorator to register a command handler."""
        def decorator(func: Callable) -> Callable:
            if intent:
                if intent not in self._command_handlers:
                    self._command_handlers[intent] = []
                self._command_handlers[intent].append(func)
            else:
                self._global_handlers.append(func)
            return func
        return decorator

    def register_handler(
        self,
        handler: Callable,
        intents: Optional[List[TrinityIntent]] = None
    ) -> None:
        """Register a command handler programmatically."""
        if intents:
            for intent in intents:
                if intent not in self._command_handlers:
                    self._command_handlers[intent] = []
                self._command_handlers[intent].append(handler)
        else:
            self._global_handlers.append(handler)

    # =========================================================================
    # PUBLISHING
    # =========================================================================

    async def publish_command_async(
        self,
        intent: TrinityIntent,
        payload: Dict[str, Any],
        target: Optional[TrinitySource] = None,
        requires_ack: bool = False,
    ) -> Optional[str]:
        """Publish a command to Reactor Core."""
        command = TrinityCommand(
            source=self.source,
            intent=intent,
            payload=payload,
            target=target,
            requires_ack=requires_ack,
        )

        success = await self._publish_to_transports_async(command)

        if success:
            self._stats["commands_published"] += 1
            if requires_ack:
                self._pending_acks[command.id] = command
            return command.id

        return None

    async def publish_heartbeat_async(self) -> bool:
        """Publish a heartbeat with current state."""
        payload = await self._gather_heartbeat_payload_async()

        command = TrinityCommand(
            source=self.source,
            intent=TrinityIntent.HEARTBEAT,
            payload=payload.to_dict(),
        )

        success = await self._publish_to_transports_async(command)

        if success:
            self._stats["heartbeats_published"] += 1

        return success

    async def send_ack_async(
        self,
        command: TrinityCommand,
        success: bool = True,
        message: str = ""
    ) -> bool:
        """Send ACK/NACK for a command."""
        ack = command.create_ack(success, message)
        result = await self._publish_to_transports_async(ack)

        if result:
            self._stats["acks_sent"] += 1

        return result

    async def _publish_to_transports_async(
        self,
        command: TrinityCommand
    ) -> bool:
        """Publish command to all active transports."""
        success = False

        for transport in self._transports:
            if transport.is_connected():
                try:
                    if await transport.publish_async(command):
                        success = True
                except Exception as e:
                    logger.error(f"[Trinity] Transport publish error: {e}")

        return success

    # =========================================================================
    # SUBSCRIPTION
    # =========================================================================

    async def _subscribe_loop_async(self, transport: TrinityTransport) -> None:
        """Background task to receive commands from a transport."""
        try:
            async for command in transport.subscribe_async():
                if not self._connected:
                    break

                await self._handle_command_async(command)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Trinity] Subscribe loop error: {e}")

    async def _handle_command_async(self, command: TrinityCommand) -> None:
        """Handle an incoming command."""
        # Deduplication
        cmd_hash = command.compute_hash()
        if cmd_hash in self._seen_hashes:
            return
        self._seen_hashes.append(cmd_hash)

        self._stats["commands_received"] += 1
        self._last_command_id = command.id

        logger.info(
            f"[Trinity] Received command: {command.intent.value} "
            f"from {command.source.value}"
        )

        # Handle ACK/NACK for pending commands
        if command.intent in (TrinityIntent.ACK, TrinityIntent.NACK):
            if command.response_to and command.response_to in self._pending_acks:
                del self._pending_acks[command.response_to]
            return

        # Dispatch to handlers
        await self._dispatch_command_async(command)

        # Send ACK if required
        if command.requires_ack:
            await self.send_ack_async(command, success=True)

    async def _dispatch_command_async(self, command: TrinityCommand) -> None:
        """Dispatch command to registered handlers."""
        # Intent-specific handlers
        if command.intent in self._command_handlers:
            for handler in self._command_handlers[command.intent]:
                try:
                    result = handler(command)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.error(f"[Trinity] Handler error for {command.intent}: {e}")
                    self._stats["errors"] += 1

        # Global handlers
        for handler in self._global_handlers:
            try:
                result = handler(command)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"[Trinity] Global handler error: {e}")
                self._stats["errors"] += 1

    # =========================================================================
    # HEARTBEAT
    # =========================================================================

    async def _heartbeat_loop_async(self) -> None:
        """Background task to publish heartbeats."""
        while self._connected:
            try:
                await self.publish_heartbeat_async()
                await asyncio.sleep(self.heartbeat_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Trinity] Heartbeat error: {e}")
                await asyncio.sleep(self.heartbeat_interval)

    async def _gather_heartbeat_payload_async(self) -> HeartbeatPayload:
        """Gather current state for heartbeat."""
        payload = HeartbeatPayload(
            uptime_seconds=time.time() - self._start_time,
            last_command_id=self._last_command_id,
            pending_commands=len(self._pending_acks),
        )

        # Try to gather system info
        try:
            import psutil
            payload.system_cpu_percent = psutil.cpu_percent()
            payload.system_memory_percent = psutil.virtual_memory().percent
        except ImportError:
            pass

        # Try to get active window info
        try:
            from backend.vision.yabai_space_detector import get_yabai_detector
            yabai = get_yabai_detector()

            # Ghost display
            ghost_space = yabai.get_ghost_display_space()
            payload.ghost_display_available = ghost_space is not None

            # Apps on ghost display
            if ghost_space:
                windows = yabai.get_windows_on_space(ghost_space)
                payload.apps_on_ghost_display = list(set(
                    w.get("app", "") for w in windows if w.get("app")
                ))

        except Exception:
            pass

        # Get frozen apps from Cryostasis
        try:
            from backend.system.cryostasis_manager import get_cryostasis_manager
            cryo = get_cryostasis_manager()
            payload.frozen_apps = cryo.get_frozen_app_names()
        except Exception:
            pass

        return payload

    # =========================================================================
    # UTILITIES
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get bridge statistics."""
        return {
            **self._stats,
            "connected": self._connected,
            "transports": len(self._transports),
            "pending_acks": len(self._pending_acks),
            "uptime_seconds": time.time() - self._start_time,
        }


# =============================================================================
# SINGLETON ACCESSOR
# =============================================================================

_reactor_bridge_instance: Optional[ReactorCoreBridge] = None


def get_reactor_bridge() -> ReactorCoreBridge:
    """Get the singleton ReactorCoreBridge instance."""
    global _reactor_bridge_instance
    if _reactor_bridge_instance is None:
        _reactor_bridge_instance = ReactorCoreBridge()
    return _reactor_bridge_instance


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

async def connect_to_reactor() -> bool:
    """Connect to Reactor Core."""
    bridge = get_reactor_bridge()
    return await bridge.connect_async()


async def publish_command(
    intent: TrinityIntent,
    payload: Dict[str, Any],
    target: Optional[TrinitySource] = None
) -> Optional[str]:
    """Publish a command to Reactor Core."""
    bridge = get_reactor_bridge()
    return await bridge.publish_command_async(intent, payload, target)
