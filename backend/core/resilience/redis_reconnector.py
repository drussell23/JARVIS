"""
Auto-Reconnecting Redis Client
==============================

Resilient Redis client with automatic reconnection and graceful degradation.

Features:
    - Automatic reconnection with exponential backoff
    - Connection health monitoring with background pings
    - Graceful degradation mode (operations fail fast when disconnected)
    - Connection pooling with proper cleanup
    - Pub/Sub reconnection with subscription restore
    - Comprehensive metrics and health status
    - v93.0: Redis is optional - graceful no-op when not installed

Author: JARVIS Cross-Repo Resilience
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# v93.0: Check if Redis is available at module load time
REDIS_AVAILABLE = False
_redis_import_error: Optional[str] = None

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError as e:
    _redis_import_error = str(e)
    aioredis = None  # type: ignore


class RedisNotAvailableError(Exception):
    """Raised when Redis is not available and operation cannot proceed."""

    def __init__(self, reason: str, retry_in: Optional[float] = None):
        self.reason = reason
        self.retry_in = retry_in
        super().__init__(f"Redis not available: {reason}")


class ConnectionState(Enum):
    """State of Redis connection."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"  # Permanently failed (e.g., auth error)


@dataclass
class RedisConnectionConfig:
    """Configuration for Redis connection."""

    # Connection
    url: str = "redis://localhost:6379"
    db: int = 0
    password: Optional[str] = None
    ssl: bool = False

    # Pool settings
    max_connections: int = 10
    socket_timeout: float = 5.0
    socket_connect_timeout: float = 5.0

    # Reconnection
    auto_reconnect: bool = True
    initial_reconnect_delay: float = 0.5
    max_reconnect_delay: float = 30.0
    reconnect_backoff_factor: float = 2.0
    max_reconnect_attempts: int = 0  # 0 = infinite

    # Health check
    health_check_interval: float = 10.0
    ping_timeout: float = 2.0

    # Behavior
    fail_fast_when_disconnected: bool = False
    operation_timeout: float = 10.0


@dataclass
class ConnectionMetrics:
    """Metrics for Redis connection."""

    connects: int = 0
    disconnects: int = 0
    reconnects: int = 0
    reconnect_failures: int = 0
    operations: int = 0
    operation_failures: int = 0
    pubsub_reconnects: int = 0
    last_ping_latency_ms: float = 0.0
    avg_operation_latency_ms: float = 0.0
    connected_since: Optional[float] = None


class ResilientRedisClient:
    """
    Auto-reconnecting Redis client with health monitoring.

    Wraps redis.asyncio.Redis with automatic reconnection,
    health checks, and graceful degradation.

    Usage:
        config = RedisConnectionConfig(url="redis://localhost:6379")
        client = ResilientRedisClient(config)

        await client.connect()

        # Use like normal Redis client
        await client.set("key", "value")
        value = await client.get("key")

        # Check health
        if client.is_healthy:
            # Do critical operations

        await client.disconnect()
    """

    def __init__(
        self,
        config: Optional[RedisConnectionConfig] = None,
        on_connect: Optional[Callable[[], Any]] = None,
        on_disconnect: Optional[Callable[[Exception], Any]] = None,
    ):
        self.config = config or RedisConnectionConfig()
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

        self._redis: Optional[Any] = None
        self._state = ConnectionState.DISCONNECTED
        self._state_lock = asyncio.Lock()
        self._reconnect_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._reconnect_attempts = 0
        self._last_error: Optional[Exception] = None

        # Pub/Sub state for reconnection
        self._subscriptions: Set[Tuple[str, Callable]] = set()
        self._psubscriptions: Set[Tuple[str, Callable]] = set()
        self._pubsub: Optional[Any] = None
        self._pubsub_task: Optional[asyncio.Task] = None

        self.metrics = ConnectionMetrics()

    @property
    def state(self) -> ConnectionState:
        """Get current connection state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Check if connected to Redis."""
        return self._state == ConnectionState.CONNECTED

    @property
    def is_healthy(self) -> bool:
        """Check if connection is healthy and ready."""
        return self._state == ConnectionState.CONNECTED and self._redis is not None

    async def connect(self) -> bool:
        """
        Establish Redis connection.

        v93.0: Gracefully handles missing Redis module - returns False without error spam.

        Returns:
            True if connected successfully
        """
        # v93.0: Early exit if Redis module not installed
        if not REDIS_AVAILABLE:
            async with self._state_lock:
                self._state = ConnectionState.FAILED
                self._last_error = ModuleNotFoundError(_redis_import_error or "redis module not installed")

            # Only log once at INFO level (not ERROR)
            if not hasattr(self, '_redis_unavailable_logged'):
                self._redis_unavailable_logged = True
                logger.info(
                    "[ResilientRedis] Redis module not installed - running without Redis. "
                    "Install with: pip install redis"
                )
            return False

        async with self._state_lock:
            if self._state == ConnectionState.CONNECTED:
                return True

            self._state = ConnectionState.CONNECTING

        try:
            self._redis = await aioredis.from_url(
                self.config.url,
                db=self.config.db,
                password=self.config.password,
                max_connections=self.config.max_connections,
                socket_timeout=self.config.socket_timeout,
                socket_connect_timeout=self.config.socket_connect_timeout,
                decode_responses=False,
            )

            # Test connection
            await asyncio.wait_for(
                self._redis.ping(),
                timeout=self.config.ping_timeout,
            )

            async with self._state_lock:
                self._state = ConnectionState.CONNECTED
                self._reconnect_attempts = 0
                self._last_error = None
                self.metrics.connects += 1
                self.metrics.connected_since = time.time()

            # Start health check
            self._start_health_check()

            # Call connect callback
            if self._on_connect:
                try:
                    result = self._on_connect()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.error(f"[ResilientRedis] Connect callback error: {e}")

            logger.info(f"[ResilientRedis] Connected to {self.config.url}")
            return True

        except Exception as e:
            async with self._state_lock:
                self._state = ConnectionState.DISCONNECTED
                self._last_error = e

            # Determine if error is permanent
            if self._is_permanent_error(e):
                async with self._state_lock:
                    self._state = ConnectionState.FAILED
                logger.error(f"[ResilientRedis] Permanent connection failure: {e}")
                return False

            logger.warning(f"[ResilientRedis] Connection failed: {e}")

            # Schedule reconnection
            if self.config.auto_reconnect:
                self._schedule_reconnect()

            return False

    async def disconnect(self) -> None:
        """Disconnect from Redis gracefully."""
        # Stop background tasks
        self._stop_health_check()
        self._stop_reconnect()

        # Close pub/sub
        if self._pubsub_task:
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass

        if self._pubsub:
            try:
                await self._pubsub.close()
            except Exception:
                pass

        # Close main connection
        if self._redis:
            try:
                await self._redis.aclose()  # v93.14
            except Exception:
                pass
            self._redis = None

        async with self._state_lock:
            self._state = ConnectionState.DISCONNECTED
            self.metrics.disconnects += 1
            self.metrics.connected_since = None

        logger.info("[ResilientRedis] Disconnected")

    def _is_permanent_error(self, error: Exception) -> bool:
        """
        Check if error is permanent (shouldn't retry).

        v92.0: Added detection for missing redis module to prevent
        infinite reconnection spam when redis isn't installed.
        """
        error_str = str(error).lower()

        permanent_patterns = [
            "auth",
            "invalid password",
            "noauth",
            "wrongpass",
            "invalid database",
            # v92.0: Module not installed - permanent failure
            "no module named",
            "modulenotfounderror",
            "cannot import",
        ]

        # Also check exception type for ImportError/ModuleNotFoundError
        if isinstance(error, (ImportError, ModuleNotFoundError)):
            return True

        return any(p in error_str for p in permanent_patterns)

    def _schedule_reconnect(self) -> None:
        """Schedule reconnection attempt."""
        if self._reconnect_task and not self._reconnect_task.done():
            return

        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    def _stop_reconnect(self) -> None:
        """Stop reconnection attempts."""
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

    async def _reconnect_loop(self) -> None:
        """Background reconnection loop."""
        async with self._state_lock:
            self._state = ConnectionState.RECONNECTING

        while True:
            if self.config.max_reconnect_attempts > 0:
                if self._reconnect_attempts >= self.config.max_reconnect_attempts:
                    logger.error(
                        f"[ResilientRedis] Max reconnect attempts "
                        f"({self.config.max_reconnect_attempts}) reached"
                    )
                    async with self._state_lock:
                        self._state = ConnectionState.FAILED
                    return

            self._reconnect_attempts += 1

            # Calculate backoff delay
            delay = min(
                self.config.initial_reconnect_delay
                * (self.config.reconnect_backoff_factor ** (self._reconnect_attempts - 1)),
                self.config.max_reconnect_delay,
            )

            logger.info(
                f"[ResilientRedis] Reconnect attempt {self._reconnect_attempts} "
                f"in {delay:.1f}s"
            )

            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

            try:
                # Close existing connection if any
                if self._redis:
                    try:
                        await self._redis.aclose()  # v93.14
                    except Exception:
                        pass
                    self._redis = None

                # Try to connect
                if await self.connect():
                    self.metrics.reconnects += 1

                    # Restore pub/sub subscriptions
                    await self._restore_subscriptions()

                    return

            except asyncio.CancelledError:
                return
            except Exception as e:
                self.metrics.reconnect_failures += 1
                logger.warning(f"[ResilientRedis] Reconnect failed: {e}")

    def _start_health_check(self) -> None:
        """Start background health check."""
        if self._health_task and not self._health_task.done():
            return

        self._health_task = asyncio.create_task(self._health_check_loop())

    def _stop_health_check(self) -> None:
        """Stop background health check."""
        if self._health_task:
            self._health_task.cancel()
            self._health_task = None

    async def _health_check_loop(self) -> None:
        """Background health check loop."""
        while True:
            try:
                await asyncio.sleep(self.config.health_check_interval)

                if self._state != ConnectionState.CONNECTED or not self._redis:
                    continue

                # Ping Redis
                start = time.time()
                await asyncio.wait_for(
                    self._redis.ping(),
                    timeout=self.config.ping_timeout,
                )
                latency = (time.time() - start) * 1000
                self.metrics.last_ping_latency_ms = latency

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[ResilientRedis] Health check failed: {e}")
                await self._handle_connection_error(e)

    async def _handle_connection_error(self, error: Exception) -> None:
        """Handle a connection error."""
        async with self._state_lock:
            if self._state == ConnectionState.CONNECTED:
                self._state = ConnectionState.DISCONNECTED
                self._last_error = error
                self.metrics.disconnects += 1

        # Call disconnect callback
        if self._on_disconnect:
            try:
                result = self._on_disconnect(error)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"[ResilientRedis] Disconnect callback error: {e}")

        # Schedule reconnection
        if self.config.auto_reconnect:
            self._schedule_reconnect()

    async def _execute(
        self,
        method: str,
        *args,
        **kwargs,
    ) -> Any:
        """Execute a Redis command with error handling."""
        if not self.is_healthy:
            if self.config.fail_fast_when_disconnected:
                raise RedisNotAvailableError(
                    "Not connected",
                    retry_in=self.config.initial_reconnect_delay,
                )

            # Wait for connection
            for _ in range(int(self.config.operation_timeout / 0.1)):
                if self.is_healthy:
                    break
                await asyncio.sleep(0.1)
            else:
                raise RedisNotAvailableError("Connection timeout")

        start = time.time()
        self.metrics.operations += 1

        try:
            redis_method = getattr(self._redis, method)
            result = await asyncio.wait_for(
                redis_method(*args, **kwargs),
                timeout=self.config.operation_timeout,
            )

            # Update latency metrics
            latency_ms = (time.time() - start) * 1000
            total = (
                self.metrics.avg_operation_latency_ms * (self.metrics.operations - 1)
                + latency_ms
            )
            self.metrics.avg_operation_latency_ms = total / self.metrics.operations

            return result

        except asyncio.TimeoutError as e:
            self.metrics.operation_failures += 1
            raise RedisNotAvailableError(f"Operation timeout ({method})")

        except Exception as e:
            self.metrics.operation_failures += 1

            # Check if this is a connection error
            error_str = str(e).lower()
            if any(
                p in error_str
                for p in ["connection", "closed", "reset", "refused", "unavailable"]
            ):
                await self._handle_connection_error(e)
                raise RedisNotAvailableError(f"Connection lost: {e}")

            raise

    # =========================================================================
    # Redis Command Proxies
    # =========================================================================

    async def get(self, key: str) -> Optional[bytes]:
        return await self._execute("get", key)

    async def set(
        self,
        key: str,
        value: Any,
        ex: Optional[int] = None,
        px: Optional[int] = None,
        nx: bool = False,
        xx: bool = False,
    ) -> bool:
        return await self._execute("set", key, value, ex=ex, px=px, nx=nx, xx=xx)

    async def delete(self, *keys: str) -> int:
        return await self._execute("delete", *keys)

    async def exists(self, *keys: str) -> int:
        return await self._execute("exists", *keys)

    async def expire(self, key: str, seconds: int) -> bool:
        return await self._execute("expire", key, seconds)

    async def hget(self, key: str, field: str) -> Optional[bytes]:
        return await self._execute("hget", key, field)

    async def hset(self, key: str, field: str, value: Any) -> int:
        return await self._execute("hset", key, field, value)

    async def hgetall(self, key: str) -> Dict[bytes, bytes]:
        return await self._execute("hgetall", key)

    async def hdel(self, key: str, *fields: str) -> int:
        return await self._execute("hdel", key, *fields)

    async def lpush(self, key: str, *values: Any) -> int:
        return await self._execute("lpush", key, *values)

    async def rpush(self, key: str, *values: Any) -> int:
        return await self._execute("rpush", key, *values)

    async def lpop(self, key: str, count: Optional[int] = None) -> Any:
        return await self._execute("lpop", key, count)

    async def rpop(self, key: str, count: Optional[int] = None) -> Any:
        return await self._execute("rpop", key, count)

    async def lrange(self, key: str, start: int, stop: int) -> List[bytes]:
        return await self._execute("lrange", key, start, stop)

    async def sadd(self, key: str, *members: Any) -> int:
        return await self._execute("sadd", key, *members)

    async def srem(self, key: str, *members: Any) -> int:
        return await self._execute("srem", key, *members)

    async def smembers(self, key: str) -> Set[bytes]:
        return await self._execute("smembers", key)

    async def zadd(self, key: str, mapping: Dict[str, float]) -> int:
        return await self._execute("zadd", key, mapping)

    async def zrange(self, key: str, start: int, stop: int) -> List[bytes]:
        return await self._execute("zrange", key, start, stop)

    async def zrevrange(self, key: str, start: int, stop: int) -> List[bytes]:
        return await self._execute("zrevrange", key, start, stop)

    async def zrem(self, key: str, *members: str) -> int:
        return await self._execute("zrem", key, *members)

    async def zcard(self, key: str) -> int:
        return await self._execute("zcard", key)

    async def publish(self, channel: str, message: Any) -> int:
        return await self._execute("publish", channel, message)

    async def ping(self) -> bool:
        return await self._execute("ping")

    async def script_load(self, script: str) -> str:
        return await self._execute("script_load", script)

    async def evalsha(self, sha: str, numkeys: int, *args) -> Any:
        return await self._execute("evalsha", sha, numkeys, *args)

    async def eval(self, script: str, numkeys: int, *args) -> Any:
        return await self._execute("eval", script, numkeys, *args)

    def pipeline(self):
        """Get a pipeline for batched operations."""
        if not self._redis:
            raise RedisNotAvailableError("Not connected")
        return self._redis.pipeline()

    # =========================================================================
    # Pub/Sub with Reconnection
    # =========================================================================

    async def subscribe(
        self,
        channel: str,
        handler: Callable[[str, bytes], Any],
    ) -> None:
        """
        Subscribe to a channel with automatic reconnection.

        Args:
            channel: Channel name
            handler: Callback function (channel, message)
        """
        self._subscriptions.add((channel, handler))

        if not self._pubsub:
            await self._setup_pubsub()

        await self._pubsub.subscribe(channel)
        logger.info(f"[ResilientRedis] Subscribed to {channel}")

    async def psubscribe(
        self,
        pattern: str,
        handler: Callable[[str, bytes], Any],
    ) -> None:
        """Subscribe to a pattern with automatic reconnection."""
        self._psubscriptions.add((pattern, handler))

        if not self._pubsub:
            await self._setup_pubsub()

        await self._pubsub.psubscribe(pattern)
        logger.info(f"[ResilientRedis] Pattern subscribed to {pattern}")

    async def _setup_pubsub(self) -> None:
        """Set up pub/sub connection."""
        if not self._redis:
            raise RedisNotAvailableError("Not connected")

        self._pubsub = self._redis.pubsub()
        self._pubsub_task = asyncio.create_task(self._pubsub_listener())

    async def _pubsub_listener(self) -> None:
        """Background pub/sub message listener."""
        while True:
            try:
                async for message in self._pubsub.listen():
                    if message["type"] in ("message", "pmessage"):
                        channel = message.get("channel", b"").decode()
                        data = message.get("data", b"")
                        pattern = message.get("pattern")

                        # Find and call handler
                        if pattern:
                            pattern_str = pattern.decode() if isinstance(pattern, bytes) else pattern
                            for p, handler in self._psubscriptions:
                                if p == pattern_str:
                                    try:
                                        result = handler(channel, data)
                                        if asyncio.iscoroutine(result):
                                            await result
                                    except Exception as e:
                                        logger.error(f"[ResilientRedis] Handler error: {e}")
                        else:
                            for c, handler in self._subscriptions:
                                if c == channel:
                                    try:
                                        result = handler(channel, data)
                                        if asyncio.iscoroutine(result):
                                            await result
                                    except Exception as e:
                                        logger.error(f"[ResilientRedis] Handler error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[ResilientRedis] Pub/sub error: {e}")
                # Will be restored on reconnection
                await asyncio.sleep(1)

    async def _restore_subscriptions(self) -> None:
        """Restore pub/sub subscriptions after reconnection."""
        if not (self._subscriptions or self._psubscriptions):
            return

        logger.info("[ResilientRedis] Restoring subscriptions")
        self.metrics.pubsub_reconnects += 1

        # Close old pubsub
        if self._pubsub_task:
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass

        if self._pubsub:
            try:
                await self._pubsub.close()
            except Exception:
                pass

        # Set up new pubsub
        self._pubsub = self._redis.pubsub()

        # Re-subscribe
        for channel, _ in self._subscriptions:
            await self._pubsub.subscribe(channel)

        for pattern, _ in self._psubscriptions:
            await self._pubsub.psubscribe(pattern)

        # Start new listener
        self._pubsub_task = asyncio.create_task(self._pubsub_listener())

        logger.info(
            f"[ResilientRedis] Restored {len(self._subscriptions)} subscriptions, "
            f"{len(self._psubscriptions)} pattern subscriptions"
        )

    def get_metrics(self) -> Dict[str, Any]:
        """Get connection metrics."""
        return {
            "state": self._state.value,
            "is_healthy": self.is_healthy,
            "url": self.config.url,
            "connects": self.metrics.connects,
            "disconnects": self.metrics.disconnects,
            "reconnects": self.metrics.reconnects,
            "reconnect_failures": self.metrics.reconnect_failures,
            "reconnect_attempts": self._reconnect_attempts,
            "operations": self.metrics.operations,
            "operation_failures": self.metrics.operation_failures,
            "pubsub_reconnects": self.metrics.pubsub_reconnects,
            "subscriptions": len(self._subscriptions),
            "pattern_subscriptions": len(self._psubscriptions),
            "last_ping_latency_ms": round(self.metrics.last_ping_latency_ms, 2),
            "avg_operation_latency_ms": round(self.metrics.avg_operation_latency_ms, 2),
            "connected_since": self.metrics.connected_since,
            "last_error": str(self._last_error) if self._last_error else None,
        }


# Global instance
_redis_client: Optional[ResilientRedisClient] = None


async def get_resilient_redis(
    config: Optional[RedisConnectionConfig] = None,
) -> ResilientRedisClient:
    """Get or create global resilient Redis client."""
    global _redis_client

    if _redis_client is None:
        _redis_client = ResilientRedisClient(config)
        await _redis_client.connect()

    return _redis_client
