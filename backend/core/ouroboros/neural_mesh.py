"""
Neural Mesh - Cross-Repository Connection Layer v1.0
=====================================================

The nervous system that connects JARVIS (Body), JARVIS Prime (Mind),
and Reactor Core (Learning/Evolution). Enables seamless communication
and experience sharing across all three repositories.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                        NEURAL MESH v1.0                                  │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │    ┌─────────────────┐                        ┌─────────────────┐       │
    │    │    JARVIS       │                        │  JARVIS PRIME   │       │
    │    │    (Body)       │◀──────────────────────▶│    (Mind)       │       │
    │    │                 │     Bi-directional     │                 │       │
    │    │  - UI/Voice     │        Neural          │  - Cognition    │       │
    │    │  - Execution    │        Pathways        │  - Reasoning    │       │
    │    │  - Sensors      │                        │  - Memory       │       │
    │    └────────┬────────┘                        └────────┬────────┘       │
    │             │                                          │                │
    │             │                                          │                │
    │             └──────────────┬───────────────────────────┘                │
    │                            │                                            │
    │                            ▼                                            │
    │                   ┌─────────────────┐                                   │
    │                   │  REACTOR CORE   │                                   │
    │                   │  (Learning)     │                                   │
    │                   │                 │                                   │
    │                   │  - Training     │                                   │
    │                   │  - Evolution    │                                   │
    │                   │  - Experience   │                                   │
    │                   └─────────────────┘                                   │
    │                                                                          │
    │    Connection Types:                                                     │
    │    ────────────────                                                      │
    │    [WebSocket]  Real-time bidirectional (low latency)                   │
    │    [HTTP/REST]  Request-response (reliable, stateless)                  │
    │    [File-Based] Async events (persistence, batch processing)            │
    │    [Memory]     In-process (zero latency, same runtime)                 │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

try:
    import aiofiles
except ImportError:
    aiofiles = None

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = logging.getLogger("Ouroboros.NeuralMesh")


# =============================================================================
# CONFIGURATION
# =============================================================================

class MeshConfig:
    """Neural mesh configuration - all dynamic from environment."""

    # Repository paths
    JARVIS_ROOT = Path(os.getenv(
        "JARVIS_ROOT",
        Path(__file__).parent.parent.parent.parent
    ))
    PRIME_ROOT = Path(os.getenv(
        "JARVIS_PRIME_ROOT",
        Path.home() / "Documents/repos/JARVIS-Prime"
    ))
    REACTOR_ROOT = Path(os.getenv(
        "REACTOR_CORE_ROOT",
        Path.home() / "Documents/repos/reactor-core"
    ))

    # Connection endpoints
    JARVIS_WS_URL = os.getenv("JARVIS_WS_URL", "ws://localhost:8010/ws")
    PRIME_API_URL = os.getenv("JARVIS_PRIME_API_URL", "http://localhost:8000/v1")
    REACTOR_API_URL = os.getenv("REACTOR_CORE_API_URL", "http://localhost:8020/api")

    # Event directories for file-based communication
    @staticmethod
    def get_event_dir(repo: str) -> Path:
        dirs = {
            "jarvis": MeshConfig.JARVIS_ROOT / ".mesh_events",
            "prime": MeshConfig.PRIME_ROOT / ".mesh_events",
            "reactor": MeshConfig.REACTOR_ROOT / ".mesh_events",
        }
        path = dirs.get(repo, MeshConfig.JARVIS_ROOT / ".mesh_events")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # Timeouts (validated)
    @staticmethod
    def get_connection_timeout() -> float:
        value = float(os.getenv("MESH_CONNECTION_TIMEOUT", "10.0"))
        return max(1.0, min(60.0, value))

    @staticmethod
    def get_request_timeout() -> float:
        value = float(os.getenv("MESH_REQUEST_TIMEOUT", "30.0"))
        return max(5.0, min(300.0, value))

    # Retry configuration
    @staticmethod
    def get_max_retries() -> int:
        value = int(os.getenv("MESH_MAX_RETRIES", "3"))
        return max(1, min(10, value))

    @staticmethod
    def get_retry_delay() -> float:
        value = float(os.getenv("MESH_RETRY_DELAY", "1.0"))
        return max(0.1, min(30.0, value))


# =============================================================================
# ENUMS AND TYPES
# =============================================================================

class NodeType(str, Enum):
    """Type of node in the neural mesh."""
    JARVIS = "jarvis"       # Body - UI, execution, sensors
    PRIME = "prime"         # Mind - cognition, reasoning
    REACTOR = "reactor"     # Learning - training, evolution


class ConnectionType(str, Enum):
    """Type of connection between nodes."""
    WEBSOCKET = "websocket"
    HTTP = "http"
    FILE = "file"
    MEMORY = "memory"


class MessageType(str, Enum):
    """Type of message in the neural mesh."""
    # Control messages
    PING = "ping"
    PONG = "pong"
    HANDSHAKE = "handshake"
    DISCONNECT = "disconnect"

    # Data messages
    REQUEST = "request"
    RESPONSE = "response"
    EVENT = "event"
    EXPERIENCE = "experience"

    # Self-improvement messages
    IMPROVEMENT_REQUEST = "improvement_request"
    IMPROVEMENT_PROGRESS = "improvement_progress"
    IMPROVEMENT_RESULT = "improvement_result"

    # Learning messages
    TRAINING_SIGNAL = "training_signal"
    MODEL_UPDATE = "model_update"
    FEEDBACK = "feedback"


class MessagePriority(Enum):
    """Priority levels for messages."""
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class MeshMessage:
    """A message in the neural mesh."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    type: MessageType = MessageType.EVENT
    source: NodeType = NodeType.JARVIS
    target: Optional[NodeType] = None  # None = broadcast
    priority: MessagePriority = MessagePriority.NORMAL
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    correlation_id: Optional[str] = None  # For request/response pairing
    ttl: float = 300.0  # Time to live in seconds

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "source": self.source.value,
            "target": self.target.value if self.target else None,
            "priority": self.priority.value,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
            "ttl": self.ttl,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MeshMessage":
        return cls(
            id=data.get("id", uuid.uuid4().hex[:16]),
            type=MessageType(data.get("type", "event")),
            source=NodeType(data.get("source", "jarvis")),
            target=NodeType(data["target"]) if data.get("target") else None,
            priority=MessagePriority(data.get("priority", 1)),
            payload=data.get("payload", {}),
            timestamp=data.get("timestamp", time.time()),
            correlation_id=data.get("correlation_id"),
            ttl=data.get("ttl", 300.0),
        )

    def is_expired(self) -> bool:
        return time.time() - self.timestamp > self.ttl


@dataclass
class NodeStatus:
    """Status of a node in the mesh."""
    node_type: NodeType
    connected: bool = False
    healthy: bool = False
    last_seen: float = 0.0
    latency_ms: float = float("inf")
    connection_type: Optional[ConnectionType] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# CONNECTION INTERFACES
# =============================================================================

class MeshConnection(ABC):
    """Abstract base class for mesh connections."""

    def __init__(self, target: NodeType):
        self.target = target
        self.connected = False
        self.logger = logging.getLogger(f"NeuralMesh.Connection.{target.value}")

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection."""
        pass

    @abstractmethod
    async def send(self, message: MeshMessage) -> bool:
        """Send a message."""
        pass

    @abstractmethod
    async def receive(self, timeout: Optional[float] = None) -> Optional[MeshMessage]:
        """Receive a message."""
        pass

    @abstractmethod
    async def ping(self) -> float:
        """Ping the connection and return latency in ms."""
        pass


class WebSocketConnection(MeshConnection):
    """WebSocket-based mesh connection."""

    def __init__(self, target: NodeType, url: str):
        super().__init__(target)
        self.url = url
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._receive_queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> bool:
        if not aiohttp:
            self.logger.warning("aiohttp not available for WebSocket")
            return False

        try:
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(
                self.url,
                timeout=aiohttp.ClientTimeout(
                    total=MeshConfig.get_connection_timeout()
                ),
            )
            self.connected = True
            self.logger.info(f"WebSocket connected to {self.target.value} at {self.url}")

            # Start receive loop
            asyncio.create_task(self._receive_loop())
            return True

        except Exception as e:
            self.logger.error(f"WebSocket connection failed: {e}")
            await self.disconnect()
            return False

    async def disconnect(self) -> None:
        self.connected = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._session:
            await self._session.close()
            self._session = None

    async def send(self, message: MeshMessage) -> bool:
        if not self._ws or self._ws.closed:
            return False

        try:
            await self._ws.send_json(message.to_dict())
            return True
        except Exception as e:
            self.logger.error(f"WebSocket send failed: {e}")
            return False

    async def receive(self, timeout: Optional[float] = None) -> Optional[MeshMessage]:
        try:
            data = await asyncio.wait_for(
                self._receive_queue.get(),
                timeout=timeout or MeshConfig.get_request_timeout(),
            )
            return MeshMessage.from_dict(data)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            self.logger.error(f"WebSocket receive failed: {e}")
            return None

    async def ping(self) -> float:
        if not self._ws or self._ws.closed:
            return float("inf")

        start = time.time()
        try:
            await self._ws.ping()
            return (time.time() - start) * 1000
        except Exception:
            return float("inf")

    async def _receive_loop(self) -> None:
        """Background loop to receive WebSocket messages."""
        while self.connected and self._ws and not self._ws.closed:
            try:
                msg = await self._ws.receive(timeout=1.0)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    await self._receive_queue.put(data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                self.logger.error(f"WebSocket receive loop error: {e}")
                break

        self.connected = False


class HTTPConnection(MeshConnection):
    """HTTP-based mesh connection (stateless)."""

    def __init__(self, target: NodeType, base_url: str):
        super().__init__(target)
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def connect(self) -> bool:
        if not aiohttp:
            self.logger.warning("aiohttp not available for HTTP")
            return False

        try:
            self._session = aiohttp.ClientSession()
            # Test connection with a ping
            latency = await self.ping()
            self.connected = latency < float("inf")
            return self.connected
        except Exception as e:
            self.logger.error(f"HTTP connection failed: {e}")
            return False

    async def disconnect(self) -> None:
        self.connected = False
        if self._session:
            await self._session.close()
            self._session = None

    async def send(self, message: MeshMessage) -> bool:
        if not self._session:
            return False

        try:
            url = f"{self.base_url}/mesh/message"
            async with self._session.post(
                url,
                json=message.to_dict(),
                timeout=aiohttp.ClientTimeout(
                    total=MeshConfig.get_request_timeout()
                ),
            ) as resp:
                return resp.status == 200
        except Exception as e:
            self.logger.error(f"HTTP send failed: {e}")
            return False

    async def receive(self, timeout: Optional[float] = None) -> Optional[MeshMessage]:
        # HTTP is stateless - no persistent receive
        return None

    async def ping(self) -> float:
        if not self._session:
            return float("inf")

        start = time.time()
        try:
            url = f"{self.base_url}/health"
            async with self._session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as resp:
                if resp.status == 200:
                    return (time.time() - start) * 1000
        except Exception:
            pass
        return float("inf")


class FileConnection(MeshConnection):
    """File-based mesh connection (async, persistent)."""

    def __init__(self, target: NodeType, event_dir: Path):
        super().__init__(target)
        self.event_dir = event_dir
        self.outbox = event_dir / "outbox"
        self.inbox = event_dir / "inbox"
        self._watcher_task: Optional[asyncio.Task] = None
        self._receive_queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> bool:
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
            self.inbox.mkdir(parents=True, exist_ok=True)
            self.connected = True

            # Start file watcher
            self._watcher_task = asyncio.create_task(self._watch_inbox())
            return True
        except Exception as e:
            self.logger.error(f"File connection failed: {e}")
            return False

    async def disconnect(self) -> None:
        self.connected = False
        if self._watcher_task:
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except asyncio.CancelledError:
                pass

    async def send(self, message: MeshMessage) -> bool:
        try:
            filename = f"{message.timestamp:.6f}_{message.id}.json"
            filepath = self.outbox / filename

            content = json.dumps(message.to_dict(), indent=2)
            if aiofiles:
                async with aiofiles.open(filepath, "w") as f:
                    await f.write(content)
            else:
                filepath.write_text(content)

            return True
        except Exception as e:
            self.logger.error(f"File send failed: {e}")
            return False

    async def receive(self, timeout: Optional[float] = None) -> Optional[MeshMessage]:
        try:
            data = await asyncio.wait_for(
                self._receive_queue.get(),
                timeout=timeout or 30.0,
            )
            return MeshMessage.from_dict(data)
        except asyncio.TimeoutError:
            return None

    async def ping(self) -> float:
        # File-based connections don't really have latency
        return 0.0 if self.connected else float("inf")

    async def _watch_inbox(self) -> None:
        """Watch inbox directory for new messages."""
        processed: Set[str] = set()

        while self.connected:
            try:
                if self.inbox.exists():
                    for filepath in sorted(self.inbox.glob("*.json")):
                        if filepath.name in processed:
                            continue

                        try:
                            if aiofiles:
                                async with aiofiles.open(filepath) as f:
                                    content = await f.read()
                            else:
                                content = filepath.read_text()

                            data = json.loads(content)
                            await self._receive_queue.put(data)
                            processed.add(filepath.name)

                            # Clean up old file
                            filepath.unlink(missing_ok=True)

                        except Exception as e:
                            self.logger.warning(f"Failed to process inbox file: {e}")

                await asyncio.sleep(0.5)  # Poll interval

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Inbox watcher error: {e}")
                await asyncio.sleep(1.0)


# =============================================================================
# NEURAL MESH CORE
# =============================================================================

class NeuralMesh:
    """
    The central nervous system connecting all JARVIS components.

    Manages connections to:
    - JARVIS (Body) - UI, execution, sensors
    - JARVIS Prime (Mind) - Cognition, reasoning
    - Reactor Core (Learning) - Training, evolution
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.NeuralMesh")
        self.node_type = NodeType.JARVIS  # This instance runs in JARVIS

        # Connections to other nodes
        self._connections: Dict[NodeType, MeshConnection] = {}
        self._node_status: Dict[NodeType, NodeStatus] = {}

        # Message handlers
        self._handlers: Dict[MessageType, List[Callable]] = {}

        # State
        self._running = False
        self._health_task: Optional[asyncio.Task] = None
        self._message_task: Optional[asyncio.Task] = None

        # Pending requests (for request/response pattern)
        self._pending_requests: Dict[str, asyncio.Future] = {}

    async def initialize(self) -> bool:
        """Initialize the neural mesh and connect to other nodes."""
        self.logger.info("=" * 60)
        self.logger.info("🧠 NEURAL MESH - Initializing Connections")
        self.logger.info("=" * 60)

        self._running = True

        # Initialize connections
        await self._setup_connections()

        # Start health monitoring
        self._health_task = asyncio.create_task(self._health_monitor_loop())

        # Start message processing
        self._message_task = asyncio.create_task(self._message_processor_loop())

        # Report status
        connected_count = sum(
            1 for status in self._node_status.values() if status.connected
        )
        self.logger.info(f"Neural mesh initialized: {connected_count} nodes connected")

        return connected_count > 0

    async def shutdown(self) -> None:
        """Shutdown the neural mesh."""
        self.logger.info("Shutting down neural mesh...")
        self._running = False

        # Cancel background tasks
        for task in [self._health_task, self._message_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Disconnect all connections
        for conn in self._connections.values():
            await conn.disconnect()

        self._connections.clear()
        self._node_status.clear()
        self.logger.info("Neural mesh shutdown complete")

    async def _setup_connections(self) -> None:
        """Setup connections to other nodes."""
        # JARVIS Prime - try WebSocket first, then HTTP
        prime_connected = False
        if MeshConfig.PRIME_ROOT.exists():
            # File-based for local development
            conn = FileConnection(
                NodeType.PRIME,
                MeshConfig.get_event_dir("prime"),
            )
            if await conn.connect():
                self._connections[NodeType.PRIME] = conn
                prime_connected = True
                self.logger.info("Connected to JARVIS Prime (file-based)")

        if not prime_connected:
            conn = HTTPConnection(NodeType.PRIME, MeshConfig.PRIME_API_URL)
            if await conn.connect():
                self._connections[NodeType.PRIME] = conn
                self.logger.info("Connected to JARVIS Prime (HTTP)")

        # Reactor Core - try HTTP, then file
        reactor_connected = False
        if MeshConfig.REACTOR_ROOT.exists():
            conn = FileConnection(
                NodeType.REACTOR,
                MeshConfig.get_event_dir("reactor"),
            )
            if await conn.connect():
                self._connections[NodeType.REACTOR] = conn
                reactor_connected = True
                self.logger.info("Connected to Reactor Core (file-based)")

        if not reactor_connected:
            conn = HTTPConnection(NodeType.REACTOR, MeshConfig.REACTOR_API_URL)
            if await conn.connect():
                self._connections[NodeType.REACTOR] = conn
                self.logger.info("Connected to Reactor Core (HTTP)")

        # Initialize node status
        for node_type in NodeType:
            if node_type == self.node_type:
                continue

            conn = self._connections.get(node_type)
            self._node_status[node_type] = NodeStatus(
                node_type=node_type,
                connected=conn is not None and conn.connected,
                healthy=conn is not None and conn.connected,
                connection_type=type(conn).__name__.replace("Connection", "").lower()
                if conn else None,
            )

    async def send(
        self,
        target: NodeType,
        message_type: MessageType,
        payload: Dict[str, Any],
        priority: MessagePriority = MessagePriority.NORMAL,
        wait_response: bool = False,
        timeout: Optional[float] = None,
    ) -> Optional[MeshMessage]:
        """
        Send a message to another node.

        Args:
            target: Target node
            message_type: Type of message
            payload: Message payload
            priority: Message priority
            wait_response: If True, wait for response
            timeout: Response timeout

        Returns:
            Response message if wait_response=True, else None
        """
        conn = self._connections.get(target)
        if not conn or not conn.connected:
            self.logger.warning(f"No connection to {target.value}")
            return None

        message = MeshMessage(
            type=message_type,
            source=self.node_type,
            target=target,
            priority=priority,
            payload=payload,
        )

        if wait_response:
            # Create future for response
            future: asyncio.Future = asyncio.Future()
            self._pending_requests[message.id] = future

            # Send message
            if not await conn.send(message):
                del self._pending_requests[message.id]
                return None

            # Wait for response
            try:
                response = await asyncio.wait_for(
                    future,
                    timeout=timeout or MeshConfig.get_request_timeout(),
                )
                return response
            except asyncio.TimeoutError:
                self._pending_requests.pop(message.id, None)
                return None
        else:
            # Fire and forget
            await conn.send(message)
            return None

    async def broadcast(
        self,
        message_type: MessageType,
        payload: Dict[str, Any],
        priority: MessagePriority = MessagePriority.NORMAL,
    ) -> int:
        """
        Broadcast a message to all connected nodes.

        Returns:
            Number of nodes message was sent to
        """
        message = MeshMessage(
            type=message_type,
            source=self.node_type,
            target=None,  # Broadcast
            priority=priority,
            payload=payload,
        )

        sent_count = 0
        for target, conn in self._connections.items():
            if conn.connected:
                if await conn.send(message):
                    sent_count += 1

        return sent_count

    def register_handler(
        self,
        message_type: MessageType,
        handler: Callable[[MeshMessage], Awaitable[Optional[Dict[str, Any]]]],
    ) -> Callable[[], None]:
        """
        Register a handler for a message type.

        Returns:
            Function to unregister the handler
        """
        if message_type not in self._handlers:
            self._handlers[message_type] = []
        self._handlers[message_type].append(handler)

        def unregister():
            if handler in self._handlers.get(message_type, []):
                self._handlers[message_type].remove(handler)

        return unregister

    async def _health_monitor_loop(self) -> None:
        """Monitor connection health."""
        while self._running:
            try:
                for node_type, conn in self._connections.items():
                    latency = await conn.ping()
                    status = self._node_status.get(node_type)
                    if status:
                        status.connected = conn.connected
                        status.healthy = latency < 5000  # 5s threshold
                        status.latency_ms = latency
                        status.last_seen = time.time()

                await asyncio.sleep(30.0)  # Check every 30 seconds

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Health monitor error: {e}")
                await asyncio.sleep(5.0)

    async def _message_processor_loop(self) -> None:
        """Process incoming messages from all connections."""
        while self._running:
            try:
                # Poll all connections for messages
                for node_type, conn in self._connections.items():
                    message = await conn.receive(timeout=0.1)
                    if message:
                        await self._handle_message(message)

                await asyncio.sleep(0.01)  # Small yield

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Message processor error: {e}")
                await asyncio.sleep(1.0)

    async def _handle_message(self, message: MeshMessage) -> None:
        """Handle an incoming message."""
        # Check if expired
        if message.is_expired():
            self.logger.debug(f"Dropped expired message: {message.id}")
            return

        # Check if this is a response to a pending request
        if message.correlation_id and message.correlation_id in self._pending_requests:
            future = self._pending_requests.pop(message.correlation_id)
            if not future.done():
                future.set_result(message)
            return

        # Call registered handlers
        handlers = self._handlers.get(message.type, [])
        for handler in handlers:
            try:
                response_payload = await handler(message)

                # Send response if handler returned a payload
                if response_payload is not None:
                    response = MeshMessage(
                        type=MessageType.RESPONSE,
                        source=self.node_type,
                        target=message.source,
                        priority=message.priority,
                        payload=response_payload,
                        correlation_id=message.id,
                    )
                    conn = self._connections.get(message.source)
                    if conn:
                        await conn.send(response)

            except Exception as e:
                self.logger.error(f"Handler error: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get neural mesh status."""
        return {
            "running": self._running,
            "node_type": self.node_type.value,
            "connections": {
                node.value: {
                    "connected": status.connected,
                    "healthy": status.healthy,
                    "latency_ms": status.latency_ms,
                    "connection_type": status.connection_type,
                    "last_seen": status.last_seen,
                }
                for node, status in self._node_status.items()
            },
        }

    # Convenience methods for common operations

    async def request_improvement(
        self,
        target_file: str,
        goal: str,
        context: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Request self-improvement from JARVIS Prime."""
        response = await self.send(
            target=NodeType.PRIME,
            message_type=MessageType.IMPROVEMENT_REQUEST,
            payload={
                "target_file": target_file,
                "goal": goal,
                "context": context,
            },
            wait_response=True,
            timeout=300.0,  # 5 minutes for improvement
        )
        return response.payload if response else None

    async def publish_experience(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
        iterations: int,
    ) -> bool:
        """Publish improvement experience to Reactor Core."""
        response = await self.send(
            target=NodeType.REACTOR,
            message_type=MessageType.EXPERIENCE,
            payload={
                "original_code": original_code[:5000],
                "improved_code": improved_code[:5000],
                "goal": goal,
                "success": success,
                "iterations": iterations,
                "timestamp": time.time(),
            },
            wait_response=False,
        )
        return response is not None

    async def request_training(
        self,
        training_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Request model training from Reactor Core."""
        response = await self.send(
            target=NodeType.REACTOR,
            message_type=MessageType.TRAINING_SIGNAL,
            payload=training_data,
            wait_response=True,
            timeout=600.0,  # 10 minutes for training
        )
        return response.payload if response else None


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_neural_mesh: Optional[NeuralMesh] = None


def get_neural_mesh() -> NeuralMesh:
    """Get the global neural mesh instance."""
    global _neural_mesh
    if _neural_mesh is None:
        _neural_mesh = NeuralMesh()
    return _neural_mesh


async def initialize_neural_mesh() -> bool:
    """Initialize the neural mesh."""
    mesh = get_neural_mesh()
    return await mesh.initialize()


async def shutdown_neural_mesh() -> None:
    """Shutdown the neural mesh."""
    global _neural_mesh
    if _neural_mesh:
        await _neural_mesh.shutdown()
        _neural_mesh = None


def is_neural_mesh_running() -> bool:
    """
    Check if the neural mesh exists and is running WITHOUT creating a new instance.

    This is critical for probes/health checks that need to verify state
    without side effects.

    Returns:
        True if mesh exists and is running, False otherwise
    """
    return _neural_mesh is not None and getattr(_neural_mesh, '_running', False)


def get_neural_mesh_if_exists() -> Optional[NeuralMesh]:
    """
    Get the neural mesh instance if it exists, without creating a new one.

    Returns:
        The existing mesh instance or None if not initialized
    """
    return _neural_mesh
