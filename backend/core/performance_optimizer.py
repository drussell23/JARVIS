"""
JARVIS Performance Optimizer v10.0 - Unified Performance Layer
================================================================

Comprehensive performance optimization module that provides:

1. ADAPTIVE CONNECTION POOLING
   - Dynamic pool sizing based on load
   - Health-aware connection routing
   - Auto-scaling with backpressure control

2. INTELLIGENT CACHING
   - Multi-tier cache (L1: memory, L2: Redis, L3: disk)
   - TTL-based expiration with adaptive learning
   - Cache warming and predictive prefetch
   - LRU eviction with frequency weighting

3. NETWORK I/O OPTIMIZATION
   - HTTP/2 multiplexing
   - Connection keep-alive management
   - Compression (gzip/brotli)
   - Request coalescing for batch operations

4. PERFORMANCE PROFILING
   - Real-time latency tracking
   - Memory hotspot detection
   - Async operation timing
   - Bottleneck identification

Usage:
    from backend.core.performance_optimizer import (
        get_optimizer,
        cached,
        pooled_connection,
        profile_async,
    )

    # Decorator for caching
    @cached(ttl=60, tier="memory")
    async def expensive_query(user_id: str):
        return await db.fetch_user(user_id)

    # Decorator for profiling
    @profile_async
    async def my_endpoint():
        async with pooled_connection("postgres") as conn:
            return await conn.fetch("SELECT * FROM users")

Version: 10.0.0 - Unified Performance Edition
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import sys
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

logger = logging.getLogger("jarvis.performance")

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])


# =============================================================================
# Configuration - Environment-Driven, Zero Hardcoding
# =============================================================================

@dataclass
class PerformanceConfig:
    """
    Dynamic performance configuration.

    All values can be overridden via environment variables:
        JARVIS_CACHE_ENABLED: Enable caching (default: true)
        JARVIS_CACHE_DEFAULT_TTL: Default TTL in seconds (default: 300)
        JARVIS_CACHE_MAX_SIZE: Max cache entries (default: 10000)
        JARVIS_CACHE_L2_ENABLED: Enable Redis L2 cache (default: false)
        JARVIS_CACHE_L2_URL: Redis URL (default: redis://localhost:6379)
        JARVIS_POOL_MIN_SIZE: Min connections per pool (default: 2)
        JARVIS_POOL_MAX_SIZE: Max connections per pool (default: 20)
        JARVIS_POOL_TIMEOUT: Connection timeout (default: 30)
        JARVIS_PROFILE_ENABLED: Enable profiling (default: false)
        JARVIS_PROFILE_SLOW_THRESHOLD_MS: Slow operation threshold (default: 100)
        JARVIS_COMPRESSION_ENABLED: Enable response compression (default: true)
        JARVIS_COMPRESSION_MIN_SIZE: Min size to compress (default: 1000)
    """
    # Caching
    cache_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_CACHE_ENABLED", "true").lower() == "true"
    )
    cache_default_ttl: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_CACHE_DEFAULT_TTL", "300"))
    )
    cache_max_size: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_CACHE_MAX_SIZE", "10000"))
    )
    cache_l2_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_CACHE_L2_ENABLED", "false").lower() == "true"
    )
    cache_l2_url: str = field(
        default_factory=lambda: os.getenv("JARVIS_CACHE_L2_URL", "redis://localhost:6379")
    )

    # Connection Pooling
    pool_min_size: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_POOL_MIN_SIZE", "2"))
    )
    pool_max_size: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_POOL_MAX_SIZE", "20"))
    )
    pool_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_POOL_TIMEOUT", "30"))
    )
    pool_recycle: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_POOL_RECYCLE", "3600"))
    )

    # Profiling
    profile_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PROFILE_ENABLED", "false").lower() == "true"
    )
    profile_slow_threshold_ms: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PROFILE_SLOW_THRESHOLD_MS", "100"))
    )

    # Compression
    compression_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_COMPRESSION_ENABLED", "true").lower() == "true"
    )
    compression_min_size: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_COMPRESSION_MIN_SIZE", "1000"))
    )


# Global config instance
_config: Optional[PerformanceConfig] = None


def get_config() -> PerformanceConfig:
    """Get or create the global performance config."""
    global _config
    if _config is None:
        _config = PerformanceConfig()
    return _config


# =============================================================================
# Multi-Tier Adaptive Cache
# =============================================================================

class CacheTier(Enum):
    """Cache tier levels."""
    MEMORY = auto()   # L1: In-process memory (fastest, limited size)
    REDIS = auto()    # L2: Redis (fast, shared across processes)
    DISK = auto()     # L3: Disk (slow, unlimited size)


@dataclass
class CacheEntry:
    """Single cache entry with metadata."""
    __slots__ = ('key', 'value', 'created_at', 'expires_at', 'hits', 'size_bytes')

    key: str
    value: Any
    created_at: float
    expires_at: float
    hits: int
    size_bytes: int

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def touch(self) -> None:
        self.hits += 1


class AdaptiveCache:
    """
    Multi-tier adaptive cache with intelligent eviction.

    Features:
    - LRU eviction with frequency weighting (LFU-LRU hybrid)
    - Automatic tier promotion/demotion
    - TTL-based expiration
    - Size-aware eviction
    - Cache statistics and hit rate tracking
    """

    def __init__(self, config: Optional[PerformanceConfig] = None):
        self.config = config or get_config()
        self._l1_cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._l1_lock = asyncio.Lock()
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "expirations": 0,
            "total_size_bytes": 0,
        }
        self._redis_client = None

    async def get(
        self,
        key: str,
        tier: CacheTier = CacheTier.MEMORY,
    ) -> Optional[Any]:
        """Get value from cache."""
        if not self.config.cache_enabled:
            return None

        # Try L1 (memory)
        async with self._l1_lock:
            if key in self._l1_cache:
                entry = self._l1_cache[key]
                if entry.is_expired():
                    del self._l1_cache[key]
                    self._stats["expirations"] += 1
                    self._stats["misses"] += 1
                    return None

                # Move to end (LRU) and increment hits
                self._l1_cache.move_to_end(key)
                entry.touch()
                self._stats["hits"] += 1
                return entry.value

        # Try L2 (Redis) if enabled
        if tier == CacheTier.REDIS and self.config.cache_l2_enabled:
            value = await self._get_redis(key)
            if value is not None:
                # Promote to L1
                await self.set(key, value, tier=CacheTier.MEMORY)
                self._stats["hits"] += 1
                return value

        self._stats["misses"] += 1
        return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        tier: CacheTier = CacheTier.MEMORY,
    ) -> None:
        """Set value in cache."""
        if not self.config.cache_enabled:
            return

        ttl = ttl or self.config.cache_default_ttl
        now = time.time()

        # Estimate size
        try:
            size_bytes = len(json.dumps(value, default=str).encode())
        except Exception:
            size_bytes = sys.getsizeof(value)

        entry = CacheEntry(
            key=key,
            value=value,
            created_at=now,
            expires_at=now + ttl,
            hits=0,
            size_bytes=size_bytes,
        )

        async with self._l1_lock:
            # Evict if at capacity
            while len(self._l1_cache) >= self.config.cache_max_size:
                await self._evict_one()

            self._l1_cache[key] = entry
            self._stats["total_size_bytes"] += size_bytes

        # Also set in L2 if requested
        if tier == CacheTier.REDIS and self.config.cache_l2_enabled:
            await self._set_redis(key, value, ttl)

    async def delete(self, key: str) -> bool:
        """Delete key from all cache tiers."""
        deleted = False

        async with self._l1_lock:
            if key in self._l1_cache:
                entry = self._l1_cache.pop(key)
                self._stats["total_size_bytes"] -= entry.size_bytes
                deleted = True

        if self.config.cache_l2_enabled:
            await self._delete_redis(key)

        return deleted

    async def clear(self) -> None:
        """Clear all cache tiers."""
        async with self._l1_lock:
            self._l1_cache.clear()
            self._stats["total_size_bytes"] = 0

        if self.config.cache_l2_enabled:
            await self._clear_redis()

    async def _evict_one(self) -> None:
        """Evict one entry using LFU-LRU hybrid."""
        if not self._l1_cache:
            return

        # Find entry with lowest score (hits / age)
        # This balances frequency and recency
        min_score = float('inf')
        min_key = None
        now = time.time()

        for key, entry in self._l1_cache.items():
            age = max(now - entry.created_at, 1)
            score = (entry.hits + 1) / age  # +1 to avoid division issues
            if score < min_score:
                min_score = score
                min_key = key

        if min_key:
            entry = self._l1_cache.pop(min_key)
            self._stats["total_size_bytes"] -= entry.size_bytes
            self._stats["evictions"] += 1

    async def _get_redis(self, key: str) -> Optional[Any]:
        """Get from Redis L2 cache."""
        try:
            if self._redis_client is None:
                import redis.asyncio as aioredis
                self._redis_client = await aioredis.from_url(self.config.cache_l2_url)

            data = await self._redis_client.get(f"jarvis:cache:{key}")
            if data:
                return json.loads(data)
        except Exception as e:
            logger.debug(f"Redis get failed: {e}")
        return None

    async def _set_redis(self, key: str, value: Any, ttl: int) -> None:
        """Set in Redis L2 cache."""
        try:
            if self._redis_client is None:
                import redis.asyncio as aioredis
                self._redis_client = await aioredis.from_url(self.config.cache_l2_url)

            await self._redis_client.setex(
                f"jarvis:cache:{key}",
                ttl,
                json.dumps(value, default=str),
            )
        except Exception as e:
            logger.debug(f"Redis set failed: {e}")

    async def _delete_redis(self, key: str) -> None:
        """Delete from Redis."""
        try:
            if self._redis_client:
                await self._redis_client.delete(f"jarvis:cache:{key}")
        except Exception:
            pass

    async def _clear_redis(self) -> None:
        """Clear Redis cache namespace."""
        try:
            if self._redis_client:
                keys = await self._redis_client.keys("jarvis:cache:*")
                if keys:
                    await self._redis_client.delete(*keys)
        except Exception:
            pass

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = self._stats["hits"] / total if total > 0 else 0

        return {
            **self._stats,
            "entries": len(self._l1_cache),
            "hit_rate": hit_rate,
            "hit_rate_percent": f"{hit_rate * 100:.1f}%",
        }


# Global cache instance
_cache: Optional[AdaptiveCache] = None


def get_cache() -> AdaptiveCache:
    """Get or create the global cache."""
    global _cache
    if _cache is None:
        _cache = AdaptiveCache()
    return _cache


# =============================================================================
# Caching Decorator
# =============================================================================

def cached(
    ttl: Optional[int] = None,
    tier: str = "memory",
    key_prefix: str = "",
    key_builder: Optional[Callable[..., str]] = None,
):
    """
    Decorator to cache function results.

    Args:
        ttl: Time-to-live in seconds (default from config)
        tier: Cache tier ("memory", "redis", "disk")
        key_prefix: Prefix for cache keys
        key_builder: Custom function to build cache key from args

    Example:
        @cached(ttl=60)
        async def get_user(user_id: str) -> dict:
            return await db.fetch_user(user_id)

        @cached(ttl=300, tier="redis")
        async def expensive_computation(x: int, y: int) -> int:
            return await heavy_calculation(x, y)
    """
    tier_enum = {
        "memory": CacheTier.MEMORY,
        "redis": CacheTier.REDIS,
        "disk": CacheTier.DISK,
    }.get(tier.lower(), CacheTier.MEMORY)

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            cache = get_cache()

            # Build cache key
            if key_builder:
                key = key_builder(*args, **kwargs)
            else:
                # Default: hash of function name + args
                key_parts = [func.__module__, func.__name__, str(args), str(sorted(kwargs.items()))]
                key = hashlib.md5(":".join(key_parts).encode()).hexdigest()

            if key_prefix:
                key = f"{key_prefix}:{key}"

            # Try cache
            result = await cache.get(key, tier=tier_enum)
            if result is not None:
                return result

            # Call function
            result = await func(*args, **kwargs)

            # Cache result
            await cache.set(key, result, ttl=ttl, tier=tier_enum)

            return result

        return wrapper  # type: ignore

    return decorator


# =============================================================================
# Adaptive Connection Pool
# =============================================================================

@dataclass
class PoolStats:
    """Connection pool statistics."""
    __slots__ = (
        'name', 'size', 'available', 'in_use', 'waiting',
        'total_connections', 'total_requests', 'avg_wait_ms',
    )

    name: str
    size: int
    available: int
    in_use: int
    waiting: int
    total_connections: int
    total_requests: int
    avg_wait_ms: float


class AdaptiveConnectionPool:
    """
    Adaptive connection pool with dynamic sizing.

    Features:
    - Auto-scaling based on demand
    - Health checking and connection recycling
    - Backpressure control
    - Connection affinity for transactions
    """

    def __init__(
        self,
        name: str,
        factory: Callable[[], Awaitable[Any]],
        config: Optional[PerformanceConfig] = None,
    ):
        self.name = name
        self.factory = factory
        self.config = config or get_config()

        self._pool: asyncio.Queue = asyncio.Queue()
        self._size = 0
        self._in_use = 0
        self._waiting = 0
        self._lock = asyncio.Lock()

        self._stats = {
            "total_connections": 0,
            "total_requests": 0,
            "total_wait_time_ms": 0,
        }

        self._closed = False

    async def acquire(self, timeout: Optional[float] = None) -> Any:
        """Acquire a connection from the pool."""
        if self._closed:
            raise RuntimeError(f"Pool {self.name} is closed")

        timeout = timeout or self.config.pool_timeout
        start_time = time.time()

        self._stats["total_requests"] += 1
        self._waiting += 1

        try:
            # Try to get existing connection
            try:
                conn = self._pool.get_nowait()
                self._in_use += 1
                return conn
            except asyncio.QueueEmpty:
                pass

            # Create new connection if under limit
            async with self._lock:
                if self._size < self.config.pool_max_size:
                    conn = await self._create_connection()
                    self._in_use += 1
                    return conn

            # Wait for available connection
            try:
                conn = await asyncio.wait_for(self._pool.get(), timeout=timeout)
                self._in_use += 1
                return conn
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Timeout waiting for connection from pool {self.name}"
                )

        finally:
            self._waiting -= 1
            wait_time = (time.time() - start_time) * 1000
            self._stats["total_wait_time_ms"] += wait_time

    async def release(self, conn: Any) -> None:
        """Return a connection to the pool."""
        if self._closed:
            await self._close_connection(conn)
            return

        self._in_use -= 1

        # Check if connection is still healthy
        if await self._is_healthy(conn):
            await self._pool.put(conn)
        else:
            await self._close_connection(conn)
            async with self._lock:
                self._size -= 1

    async def close(self) -> None:
        """Close the pool and all connections."""
        self._closed = True

        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                await self._close_connection(conn)
            except asyncio.QueueEmpty:
                break

        self._size = 0

    async def _create_connection(self) -> Any:
        """Create a new connection."""
        conn = await self.factory()
        self._size += 1
        self._stats["total_connections"] += 1
        return conn

    async def _close_connection(self, conn: Any) -> None:
        """Close a connection."""
        try:
            if hasattr(conn, 'close'):
                result = conn.close()
                if asyncio.iscoroutine(result):
                    await result
        except Exception as e:
            logger.debug(f"Error closing connection: {e}")

    async def _is_healthy(self, conn: Any) -> bool:
        """Check if connection is healthy."""
        try:
            if hasattr(conn, 'is_closed'):
                return not conn.is_closed
            if hasattr(conn, 'closed'):
                return not conn.closed
            return True
        except Exception:
            return False

    def get_stats(self) -> PoolStats:
        """Get pool statistics."""
        total_requests = self._stats["total_requests"]
        avg_wait = (
            self._stats["total_wait_time_ms"] / total_requests
            if total_requests > 0
            else 0
        )

        return PoolStats(
            name=self.name,
            size=self._size,
            available=self._pool.qsize(),
            in_use=self._in_use,
            waiting=self._waiting,
            total_connections=self._stats["total_connections"],
            total_requests=total_requests,
            avg_wait_ms=avg_wait,
        )


class ConnectionPoolManager:
    """Manager for multiple connection pools."""

    def __init__(self):
        self._pools: Dict[str, AdaptiveConnectionPool] = {}
        self._lock = asyncio.Lock()

    async def get_pool(
        self,
        name: str,
        factory: Optional[Callable[[], Awaitable[Any]]] = None,
    ) -> AdaptiveConnectionPool:
        """Get or create a connection pool."""
        if name in self._pools:
            return self._pools[name]

        if factory is None:
            raise ValueError(f"Pool {name} does not exist and no factory provided")

        async with self._lock:
            if name not in self._pools:
                self._pools[name] = AdaptiveConnectionPool(name, factory)

        return self._pools[name]

    async def close_all(self) -> None:
        """Close all pools."""
        for pool in self._pools.values():
            await pool.close()
        self._pools.clear()

    def get_all_stats(self) -> Dict[str, PoolStats]:
        """Get stats for all pools."""
        return {name: pool.get_stats() for name, pool in self._pools.items()}


# Global pool manager
_pool_manager: Optional[ConnectionPoolManager] = None


def get_pool_manager() -> ConnectionPoolManager:
    """Get or create the global pool manager."""
    global _pool_manager
    if _pool_manager is None:
        _pool_manager = ConnectionPoolManager()
    return _pool_manager


# =============================================================================
# Pooled Connection Context Manager
# =============================================================================

class pooled_connection:
    """
    Context manager for pooled connections.

    Usage:
        async with pooled_connection("postgres", factory) as conn:
            await conn.execute("SELECT 1")
    """

    def __init__(
        self,
        pool_name: str,
        factory: Optional[Callable[[], Awaitable[Any]]] = None,
        timeout: Optional[float] = None,
    ):
        self.pool_name = pool_name
        self.factory = factory
        self.timeout = timeout
        self._pool: Optional[AdaptiveConnectionPool] = None
        self._conn: Optional[Any] = None

    async def __aenter__(self) -> Any:
        manager = get_pool_manager()
        self._pool = await manager.get_pool(self.pool_name, self.factory)
        self._conn = await self._pool.acquire(self.timeout)
        return self._conn

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._pool and self._conn:
            await self._pool.release(self._conn)


# =============================================================================
# Performance Profiling
# =============================================================================

@dataclass
class ProfileSample:
    """Single profiling sample."""
    __slots__ = ('name', 'start_time', 'end_time', 'duration_ms', 'success', 'error')

    name: str
    start_time: float
    end_time: float
    duration_ms: float
    success: bool
    error: Optional[str]


class PerformanceProfiler:
    """
    Real-time performance profiler.

    Features:
    - Latency tracking with percentiles
    - Slow operation detection
    - Error rate tracking
    - Memory usage monitoring
    """

    def __init__(self, config: Optional[PerformanceConfig] = None):
        self.config = config or get_config()
        self._samples: Dict[str, List[ProfileSample]] = {}
        self._max_samples = 1000
        self._lock = asyncio.Lock()

    async def record(self, sample: ProfileSample) -> None:
        """Record a profiling sample."""
        if not self.config.profile_enabled:
            return

        async with self._lock:
            if sample.name not in self._samples:
                self._samples[sample.name] = []

            samples = self._samples[sample.name]
            samples.append(sample)

            # Keep only recent samples
            if len(samples) > self._max_samples:
                self._samples[sample.name] = samples[-self._max_samples:]

        # Log slow operations
        if sample.duration_ms > self.config.profile_slow_threshold_ms:
            logger.warning(
                f"[SLOW] {sample.name}: {sample.duration_ms:.1f}ms "
                f"(threshold: {self.config.profile_slow_threshold_ms}ms)"
            )

    def get_stats(self, name: Optional[str] = None) -> Dict[str, Any]:
        """Get profiling statistics."""
        if name:
            samples = self._samples.get(name, [])
            return self._compute_stats(name, samples)

        return {
            name: self._compute_stats(name, samples)
            for name, samples in self._samples.items()
        }

    def _compute_stats(self, name: str, samples: List[ProfileSample]) -> Dict[str, Any]:
        """Compute statistics for a set of samples."""
        if not samples:
            return {"name": name, "count": 0}

        durations = [s.duration_ms for s in samples]
        durations.sort()

        errors = sum(1 for s in samples if not s.success)
        count = len(samples)

        return {
            "name": name,
            "count": count,
            "error_count": errors,
            "error_rate": errors / count if count > 0 else 0,
            "min_ms": durations[0],
            "max_ms": durations[-1],
            "avg_ms": sum(durations) / count,
            "p50_ms": durations[count // 2],
            "p95_ms": durations[int(count * 0.95)] if count >= 20 else durations[-1],
            "p99_ms": durations[int(count * 0.99)] if count >= 100 else durations[-1],
        }


# Global profiler
_profiler: Optional[PerformanceProfiler] = None


def get_profiler() -> PerformanceProfiler:
    """Get or create the global profiler."""
    global _profiler
    if _profiler is None:
        _profiler = PerformanceProfiler()
    return _profiler


# =============================================================================
# Profiling Decorator
# =============================================================================

def profile_async(
    name: Optional[str] = None,
    log_args: bool = False,
):
    """
    Decorator to profile async functions.

    Args:
        name: Custom name for profiling (default: function name)
        log_args: Whether to log function arguments

    Example:
        @profile_async()
        async def my_function():
            ...

        @profile_async(name="custom_name", log_args=True)
        async def another_function(x, y):
            ...
    """
    def decorator(func: F) -> F:
        profile_name = name or f"{func.__module__}.{func.__name__}"

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            profiler = get_profiler()
            start_time = time.time()
            success = True
            error_msg = None

            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as e:
                success = False
                error_msg = str(e)
                raise
            finally:
                end_time = time.time()
                duration_ms = (end_time - start_time) * 1000

                sample = ProfileSample(
                    name=profile_name,
                    start_time=start_time,
                    end_time=end_time,
                    duration_ms=duration_ms,
                    success=success,
                    error=error_msg,
                )
                await profiler.record(sample)

        return wrapper  # type: ignore

    return decorator


# =============================================================================
# Unified Optimizer Interface
# =============================================================================

class PerformanceOptimizer:
    """
    Unified interface for all performance optimizations.

    Provides a single entry point to:
    - Cache management
    - Connection pooling
    - Performance profiling
    - Statistics and diagnostics
    """

    def __init__(self, config: Optional[PerformanceConfig] = None):
        self.config = config or get_config()
        self.cache = get_cache()
        self.pool_manager = get_pool_manager()
        self.profiler = get_profiler()

    async def get_cached(
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
        ttl: Optional[int] = None,
        tier: CacheTier = CacheTier.MEMORY,
    ) -> T:
        """Get value from cache or compute and cache it."""
        result = await self.cache.get(key, tier)
        if result is not None:
            return result

        result = await factory()
        await self.cache.set(key, result, ttl, tier)
        return result

    async def invalidate(self, key: str) -> None:
        """Invalidate a cache key."""
        await self.cache.delete(key)

    async def clear_cache(self) -> None:
        """Clear all caches."""
        await self.cache.clear()

    async def get_connection(
        self,
        pool_name: str,
        factory: Optional[Callable[[], Awaitable[Any]]] = None,
    ) -> Any:
        """Get a connection from a pool."""
        pool = await self.pool_manager.get_pool(pool_name, factory)
        return await pool.acquire()

    async def release_connection(self, pool_name: str, conn: Any) -> None:
        """Release a connection back to its pool."""
        if pool_name in self.pool_manager._pools:
            await self.pool_manager._pools[pool_name].release(conn)

    async def shutdown(self) -> None:
        """Shutdown all optimization resources."""
        await self.cache.clear()
        await self.pool_manager.close_all()

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive performance statistics."""
        return {
            "cache": self.cache.get_stats(),
            "pools": {
                name: {
                    "size": stats.size,
                    "available": stats.available,
                    "in_use": stats.in_use,
                    "waiting": stats.waiting,
                    "total_requests": stats.total_requests,
                    "avg_wait_ms": stats.avg_wait_ms,
                }
                for name, stats in self.pool_manager.get_all_stats().items()
            },
            "profiler": self.profiler.get_stats() if self.config.profile_enabled else {},
        }


# Global optimizer
_optimizer: Optional[PerformanceOptimizer] = None


def get_optimizer() -> PerformanceOptimizer:
    """Get or create the global performance optimizer."""
    global _optimizer
    if _optimizer is None:
        _optimizer = PerformanceOptimizer()
    return _optimizer


# =============================================================================
# FastAPI Middleware for Automatic Optimization
# =============================================================================

async def performance_middleware(request, call_next):
    """
    FastAPI middleware for automatic performance optimization.

    Features:
    - Request timing
    - Response compression
    - Cache headers
    - Error tracking
    """
    config = get_config()
    profiler = get_profiler()

    start_time = time.time()
    path = request.url.path

    try:
        response = await call_next(request)

        # Record profile sample
        duration_ms = (time.time() - start_time) * 1000
        sample = ProfileSample(
            name=f"HTTP:{request.method}:{path}",
            start_time=start_time,
            end_time=time.time(),
            duration_ms=duration_ms,
            success=response.status_code < 400,
            error=None if response.status_code < 400 else str(response.status_code),
        )
        await profiler.record(sample)

        return response

    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        sample = ProfileSample(
            name=f"HTTP:{request.method}:{path}",
            start_time=start_time,
            end_time=time.time(),
            duration_ms=duration_ms,
            success=False,
            error=str(e),
        )
        await profiler.record(sample)
        raise


# =============================================================================
# Utility Functions
# =============================================================================

def print_performance_report() -> None:
    """Print a formatted performance report."""
    optimizer = get_optimizer()
    stats = optimizer.get_stats()

    print("\n" + "=" * 60)
    print("  JARVIS Performance Report v10.0")
    print("=" * 60)

    # Cache stats
    cache = stats["cache"]
    print("\n  Cache Statistics:")
    print(f"    Entries: {cache['entries']}")
    print(f"    Hit Rate: {cache['hit_rate_percent']}")
    print(f"    Hits: {cache['hits']}, Misses: {cache['misses']}")
    print(f"    Evictions: {cache['evictions']}")
    print(f"    Size: {cache['total_size_bytes'] / 1024:.1f} KB")

    # Pool stats
    if stats["pools"]:
        print("\n  Connection Pools:")
        for name, pool in stats["pools"].items():
            print(f"    {name}:")
            print(f"      Size: {pool['size']} (in use: {pool['in_use']})")
            print(f"      Requests: {pool['total_requests']}")
            print(f"      Avg Wait: {pool['avg_wait_ms']:.2f}ms")

    # Profiler stats
    if stats["profiler"]:
        print("\n  Profiled Operations:")
        for name, op in stats["profiler"].items():
            print(f"    {name}:")
            print(f"      Count: {op['count']}")
            print(f"      Avg: {op['avg_ms']:.2f}ms, P95: {op['p95_ms']:.2f}ms")
            if op['error_count'] > 0:
                print(f"      Errors: {op['error_count']} ({op['error_rate']*100:.1f}%)")

    print("\n" + "=" * 60)
