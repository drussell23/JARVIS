"""
v77.0: Multi-Transport Communication - Gap #1
==============================================

Robust multi-transport communication with automatic fallback:
- Primary: Redis (fastest, requires Redis server)
- Secondary: WebSocket (real-time, requires server)
- Tertiary: File-based (always available, slowest)

Features:
- Automatic failover on connection loss
- Health monitoring for each transport
- Reconnection with exponential backoff
- Message deduplication
- Priority-based transport selection

Author: JARVIS v77.0
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
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class TransportType(Enum):
    """Transport types in priority order."""
    REDIS = "redis"
    WEBSOCKET = "websocket"
    FILE = "file"


class TransportStatus(Enum):
    """Status of a transport."""
    CONNECTED = "connected"
    CONNECTING = "connecting"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    DEGRADED = "degraded"


@dataclass
class TransportMessage:
    """Message for transport layer."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    channel: str = "default"
    payload: Dict[str, Any] = field(default_factory=dict)
    source: str = ""
    target: Optional[str] = None
    priority: int = 5  # 1-10, lower is higher priority
    ttl_seconds: float = 60.0
    require_ack: bool = False

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id,
            "timestamp": self.timestamp,
            "channel": self.channel,
            "payload": self.payload,
            "source": self.source,
            "target": self.target,
            "priority": self.priority,
            "ttl_seconds": self.ttl_seconds,
            "require_ack": self.require_ack,
        })

    @classmethod
    def from_json(cls, data: str) -> "TransportMessage":
        d = json.loads(data)
        return cls(**d)

    def is_expired(self) -> bool:
        return (time.time() - self.timestamp) > self.ttl_seconds

    def compute_hash(self) -> str:
        content = f"{self.channel}:{self.source}:{json.dumps(self.payload, sort_keys=True)}"
        return hashlib.md5(content.encode()).hexdigest()[:16]


class BaseTransport(ABC):
    """Abstract base class for transports."""

    def __init__(self):
        self.status = TransportStatus.DISCONNECTED
        self._connected = False
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5
        self._reconnect_delay = 1.0
        self._message_handlers: List[Callable] = []

    @property
    @abstractmethod
    def transport_type(self) -> TransportType:
        pass

    @abstractmethod
    async def connect(self) -> bool:
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        pass

    @abstractmethod
    async def send(self, message: TransportMessage) -> bool:
        pass

    @abstractmethod
    async def receive(self) -> Optional[TransportMessage]:
        pass

    @abstractmethod
    async def subscribe(self, channel: str) -> bool:
        pass

    def add_handler(self, handler: Callable[[TransportMessage], Coroutine]) -> None:
        self._message_handlers.append(handler)

    async def _notify_handlers(self, message: TransportMessage) -> None:
        for handler in self._message_handlers:
            try:
                await handler(message)
            except Exception as e:
                logger.error(f"[Transport] Handler error: {e}")

    async def reconnect(self) -> bool:
        """Attempt reconnection with exponential backoff."""
        while self._reconnect_attempts < self._max_reconnect_attempts:
            self._reconnect_attempts += 1
            delay = self._reconnect_delay * (2 ** (self._reconnect_attempts - 1))

            logger.info(f"[Transport] Reconnect attempt {self._reconnect_attempts}/{self._max_reconnect_attempts}")
            await asyncio.sleep(delay)

            if await self.connect():
                self._reconnect_attempts = 0
                return True

        return False


class RedisTransport(BaseTransport):
    """Redis-based transport (fastest)."""

    def __init__(self, url: Optional[str] = None):
        super().__init__()
        self.url = url or os.getenv("REDIS_URL", "redis://localhost:6379")
        self._client = None
        self._pubsub = None
        self._subscriptions: Set[str] = set()
        self._listener_task: Optional[asyncio.Task] = None

    @property
    def transport_type(self) -> TransportType:
        return TransportType.REDIS

    async def connect(self) -> bool:
        try:
            import redis.asyncio as redis
        except ImportError:
            # v109.2: Redis is optional - not a warning
            logger.info("[RedisTransport] redis package not installed (optional)")
            self.status = TransportStatus.FAILED
            return False

        try:
            self._client = redis.from_url(self.url, decode_responses=True)
            await self._client.ping()
            self._pubsub = self._client.pubsub()
            self._connected = True
            self.status = TransportStatus.CONNECTED
            logger.info("[RedisTransport] Connected")
            return True
        except Exception as e:
            # v109.2: Connection failures during startup are expected
            logger.info(f"[RedisTransport] Not available: {e}")
            self.status = TransportStatus.FAILED
            return False

    async def disconnect(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        if self._pubsub:
            await self._pubsub.close()

        if self._client:
            await self._client.close()

        self._connected = False
        self.status = TransportStatus.DISCONNECTED

    async def send(self, message: TransportMessage) -> bool:
        if not self._connected or not self._client:
            return False

        try:
            await self._client.publish(message.channel, message.to_json())
            return True
        except Exception as e:
            logger.error(f"[RedisTransport] Send failed: {e}")
            self.status = TransportStatus.DEGRADED
            return False

    async def receive(self) -> Optional[TransportMessage]:
        if not self._pubsub:
            return None

        try:
            msg = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if msg and msg.get("type") == "message":
                return TransportMessage.from_json(msg["data"])
        except Exception:
            pass
        return None

    async def subscribe(self, channel: str) -> bool:
        if not self._pubsub:
            return False

        try:
            await self._pubsub.subscribe(channel)
            self._subscriptions.add(channel)

            # Start listener if not running
            if not self._listener_task or self._listener_task.done():
                self._listener_task = asyncio.create_task(self._listen_loop())

            return True
        except Exception as e:
            logger.error(f"[RedisTransport] Subscribe failed: {e}")
            return False

    async def _listen_loop(self) -> None:
        """Background listener for subscribed channels."""
        while self._connected:
            try:
                message = await self.receive()
                if message:
                    await self._notify_handlers(message)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RedisTransport] Listener error: {e}")
            await asyncio.sleep(0.01)


class WebSocketTransport(BaseTransport):
    """WebSocket-based transport."""

    def __init__(self, url: Optional[str] = None):
        super().__init__()
        self.url = url or os.getenv("TRINITY_WS_URL", "ws://localhost:8765")
        self._ws = None
        self._listener_task: Optional[asyncio.Task] = None

    @property
    def transport_type(self) -> TransportType:
        return TransportType.WEBSOCKET

    async def connect(self) -> bool:
        try:
            import websockets
        except ImportError:
            # v109.2: WebSockets is optional - not a warning
            logger.info("[WebSocketTransport] websockets package not installed (optional)")
            self.status = TransportStatus.FAILED
            return False

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.url),
                timeout=10.0
            )
            self._connected = True
            self.status = TransportStatus.CONNECTED
            logger.info("[WebSocketTransport] Connected")

            # Start listener
            self._listener_task = asyncio.create_task(self._listen_loop())

            return True
        except Exception as e:
            # v109.2: Connection failures during startup are expected
            logger.info(f"[WebSocketTransport] Not available: {e}")
            self.status = TransportStatus.FAILED
            return False

    async def disconnect(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()

        self._connected = False
        self.status = TransportStatus.DISCONNECTED

    async def send(self, message: TransportMessage) -> bool:
        if not self._connected or not self._ws:
            return False

        try:
            await self._ws.send(message.to_json())
            return True
        except Exception as e:
            logger.error(f"[WebSocketTransport] Send failed: {e}")
            self.status = TransportStatus.DEGRADED
            return False

    async def receive(self) -> Optional[TransportMessage]:
        if not self._ws:
            return None

        try:
            data = await asyncio.wait_for(self._ws.recv(), timeout=0.1)
            return TransportMessage.from_json(data)
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None

    async def subscribe(self, channel: str) -> bool:
        # WebSocket subscription is handled at server level
        return self._connected

    async def _listen_loop(self) -> None:
        """Background listener for WebSocket messages."""
        while self._connected and self._ws:
            try:
                message = await self.receive()
                if message:
                    await self._notify_handlers(message)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[WebSocketTransport] Listener error: {e}")
            await asyncio.sleep(0.01)


class FileTransport(BaseTransport):
    """File-based transport (always available fallback)."""

    def __init__(self, base_dir: Optional[Path] = None):
        super().__init__()
        self.base_dir = base_dir or Path.home() / ".jarvis" / "trinity" / "transport"
        self._poll_interval = 0.5
        self._listener_task: Optional[asyncio.Task] = None
        self._subscriptions: Set[str] = set()
        self._processed_ids: Set[str] = set()
        self._max_processed_cache = 1000

    @property
    def transport_type(self) -> TransportType:
        return TransportType.FILE

    async def connect(self) -> bool:
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            (self.base_dir / "inbox").mkdir(exist_ok=True)
            (self.base_dir / "outbox").mkdir(exist_ok=True)
            self._connected = True
            self.status = TransportStatus.CONNECTED
            logger.info("[FileTransport] Connected")
            return True
        except Exception as e:
            logger.error(f"[FileTransport] Setup failed: {e}")
            self.status = TransportStatus.FAILED
            return False

    async def disconnect(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        self._connected = False
        self.status = TransportStatus.DISCONNECTED

    async def send(self, message: TransportMessage) -> bool:
        if not self._connected:
            return False

        try:
            # Write to outbox with channel-based organization
            channel_dir = self.base_dir / "outbox" / message.channel
            channel_dir.mkdir(parents=True, exist_ok=True)

            msg_file = channel_dir / f"{message.id}.json"

            # Atomic write
            tmp_file = msg_file.with_suffix(".tmp")
            tmp_file.write_text(message.to_json())
            tmp_file.rename(msg_file)

            return True
        except Exception as e:
            logger.error(f"[FileTransport] Send failed: {e}")
            return False

    async def receive(self) -> Optional[TransportMessage]:
        if not self._connected:
            return None

        try:
            # Check inbox for messages
            inbox = self.base_dir / "inbox"
            for channel_dir in inbox.iterdir():
                if not channel_dir.is_dir():
                    continue

                if channel_dir.name not in self._subscriptions:
                    continue

                for msg_file in sorted(channel_dir.glob("*.json")):
                    try:
                        content = msg_file.read_text()
                        message = TransportMessage.from_json(content)

                        # Skip if already processed
                        if message.id in self._processed_ids:
                            msg_file.unlink()
                            continue

                        # Skip if expired
                        if message.is_expired():
                            msg_file.unlink()
                            continue

                        # Mark as processed
                        self._processed_ids.add(message.id)
                        if len(self._processed_ids) > self._max_processed_cache:
                            # Remove oldest entries
                            self._processed_ids = set(list(self._processed_ids)[-500:])

                        # Remove file
                        msg_file.unlink()

                        return message

                    except Exception as e:
                        logger.debug(f"[FileTransport] Error reading message: {e}")
                        continue

        except Exception as e:
            logger.error(f"[FileTransport] Receive error: {e}")

        return None

    async def subscribe(self, channel: str) -> bool:
        self._subscriptions.add(channel)

        # Create inbox channel dir
        (self.base_dir / "inbox" / channel).mkdir(parents=True, exist_ok=True)

        # Start listener if not running
        if not self._listener_task or self._listener_task.done():
            self._listener_task = asyncio.create_task(self._listen_loop())

        return True

    async def _listen_loop(self) -> None:
        """Background listener for file-based messages."""
        while self._connected:
            try:
                message = await self.receive()
                if message:
                    await self._notify_handlers(message)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[FileTransport] Listener error: {e}")
            await asyncio.sleep(self._poll_interval)


class MultiTransport:
    """
    Multi-transport manager with automatic failover.

    Manages Redis → WebSocket → File transport chain with:
    - Automatic failover on failure
    - Health monitoring
    - Reconnection handling
    - Message deduplication
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        websocket_url: Optional[str] = None,
        file_base_dir: Optional[Path] = None
    ):
        self.transports: Dict[TransportType, BaseTransport] = {
            TransportType.REDIS: RedisTransport(redis_url),
            TransportType.WEBSOCKET: WebSocketTransport(websocket_url),
            TransportType.FILE: FileTransport(file_base_dir),
        }

        self._primary: Optional[TransportType] = None
        self._message_handlers: List[Callable] = []
        self._dedup_cache: deque = deque(maxlen=1000)
        self._health_check_task: Optional[asyncio.Task] = None
        self._connected = False
        self._started = False

    async def start(self) -> bool:
        """
        v93.0: Start the multi-transport system.

        This is an alias for connect() that provides a consistent API
        with other transport/service classes.

        Returns:
            True if at least one transport connected successfully
        """
        if self._started:
            logger.debug("[MultiTransport] Already started")
            return self._connected

        result = await self.connect()
        self._started = result
        return result

    async def stop(self) -> None:
        """
        v93.0: Stop the multi-transport system.

        This is an alias for disconnect() that provides a consistent API.
        """
        self._started = False
        await self.disconnect()

    async def connect(self) -> bool:
        """
        Connect to transports in priority order.

        Returns True if at least one transport connects.
        """
        connected = False

        for transport_type in TransportType:
            transport = self.transports[transport_type]
            if await transport.connect():
                if not self._primary:
                    self._primary = transport_type
                    logger.info(f"[MultiTransport] Primary transport: {transport_type.value}")
                connected = True

                # Add message forwarding
                transport.add_handler(self._forward_message)

        if connected:
            self._connected = True
            self._health_check_task = asyncio.create_task(self._health_check_loop())

        return connected

    async def disconnect(self) -> None:
        """Disconnect all transports."""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        for transport in self.transports.values():
            await transport.disconnect()

        self._connected = False
        self._primary = None

    async def send(self, message: TransportMessage) -> bool:
        """
        Send message via best available transport.

        Falls back through transport chain on failure.
        """
        if not self._connected:
            return False

        # Try primary first
        if self._primary:
            transport = self.transports[self._primary]
            if transport.status == TransportStatus.CONNECTED:
                if await transport.send(message):
                    return True

        # Fallback through others
        for transport_type in TransportType:
            if transport_type == self._primary:
                continue

            transport = self.transports[transport_type]
            if transport.status == TransportStatus.CONNECTED:
                if await transport.send(message):
                    # Promote this transport
                    if transport_type != TransportType.FILE:
                        self._primary = transport_type
                        logger.info(f"[MultiTransport] Promoted to primary: {transport_type.value}")
                    return True

        return False

    async def subscribe(self, channel: str) -> bool:
        """Subscribe to channel on all connected transports."""
        success = False
        for transport in self.transports.values():
            if transport.status == TransportStatus.CONNECTED:
                if await transport.subscribe(channel):
                    success = True
        return success

    def add_handler(self, handler: Callable[[TransportMessage], Coroutine]) -> None:
        """Add message handler."""
        self._message_handlers.append(handler)

    async def _forward_message(self, message: TransportMessage) -> None:
        """Forward message to registered handlers with deduplication."""
        msg_hash = message.compute_hash()

        if msg_hash in self._dedup_cache:
            return

        self._dedup_cache.append(msg_hash)

        for handler in self._message_handlers:
            try:
                await handler(message)
            except Exception as e:
                logger.error(f"[MultiTransport] Handler error: {e}")

    async def _health_check_loop(self) -> None:
        """Periodically check transport health and reconnect."""
        while self._connected:
            try:
                for transport_type, transport in self.transports.items():
                    if transport.status == TransportStatus.FAILED:
                        # Attempt reconnection
                        if await transport.connect():
                            # Check if this should be primary
                            if self._primary is None or transport_type.value < self._primary.value:
                                self._primary = transport_type
                                logger.info(f"[MultiTransport] Recovered primary: {transport_type.value}")

                await asyncio.sleep(30)  # Check every 30 seconds

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[MultiTransport] Health check error: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get transport status."""
        return {
            "connected": self._connected,
            "primary": self._primary.value if self._primary else None,
            "transports": {
                t.value: self.transports[t].status.value
                for t in TransportType
            },
        }
