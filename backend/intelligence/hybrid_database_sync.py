#!/usr/bin/env python3
"""
Advanced Hybrid Database Synchronization System for JARVIS Voice Biometrics
===========================================================================

Self-optimizing, cache-first, connection-intelligent hybrid persistence architecture.

Architecture:
- Connection Orchestrator: Dynamic pool management with predictive scaling
- Cache-First Auth: SQLite + FAISS vector cache for <1ms authentication
- Write-Behind Queue: Priority-based batching with backpressure control
- Circuit Breaker: Automatic offline mode with self-healing recovery
- Delta Sync: SHA-256 verification with conflict resolution
- Zero Live Queries: All authentication happens locally
- Metrics & Telemetry: Real-time monitoring with auto-alerting

Tech Stack:
- SQLite (WAL mode): Primary data store with memory-mapped I/O
- FAISS: In-memory vector similarity search for embeddings
- asyncpg: High-performance PostgreSQL async client
- asyncio: Concurrent task orchestration
- Threading: Parallel batch processing

Author: JARVIS System
Version: 2.0.0 (Advanced)
"""

import asyncio
import hashlib
import json
import logging
import mmap
import os
import random
import threading
import time
from collections import deque, defaultdict
from concurrent.futures import ThreadPoolExecutor

# Import daemon executor for clean shutdown
try:
    from core.thread_manager import DaemonThreadPoolExecutor
    DAEMON_EXECUTOR_AVAILABLE = True
except ImportError:
    DAEMON_EXECUTOR_AVAILABLE = False
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Dict, List, Optional, Set, Tuple, Callable, Deque

import aiosqlite
import numpy as np

# Initialize logger early so it's available for import warnings
logger = logging.getLogger(__name__)

try:
    import asyncpg
    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False

# Import singleton connection manager
try:
    from intelligence.cloud_sql_connection_manager import get_connection_manager
    CONNECTION_MANAGER_AVAILABLE = True
except ImportError:
    CONNECTION_MANAGER_AVAILABLE = False

# Import Cloud SQL Proxy detector for intelligent connection management
try:
    from intelligence.cloud_sql_proxy_detector import (
        get_proxy_detector,
        ProxyStatus,
        ProxyDetectionConfig
    )
    PROXY_DETECTOR_AVAILABLE = True
except ImportError:
    PROXY_DETECTOR_AVAILABLE = False

try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False
    logger.warning("FAISS not available - install with: pip install faiss-cpu")

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("Redis not available - install with: pip install redis")

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logger.warning("Prometheus not available - install with: pip install prometheus-client")

try:
    import grpc
    from concurrent import futures
    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False
    logger.warning("gRPC not available - install with: pip install grpcio grpcio-tools")


class SyncStatus(Enum):
    """Synchronization status"""
    SYNCED = "synced"
    PENDING = "pending"
    SYNCING = "syncing"
    FAILED = "failed"
    CONFLICT = "conflict"
    PRIORITY_HIGH = "priority_high"
    PRIORITY_LOW = "priority_low"


class DatabaseType(Enum):
    """Database type"""
    SQLITE = "sqlite"
    CLOUDSQL = "cloudsql"
    CACHE = "cache"  # In-memory FAISS cache


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


class SyncPriority(Enum):
    """Sync operation priority levels"""
    CRITICAL = 0  # Auth-related, process immediately
    HIGH = 1  # User-facing writes
    NORMAL = 2  # Background updates
    LOW = 3  # Housekeeping, analytics
    DEFERRED = 4  # Can be delayed indefinitely


@dataclass
class SyncRecord:
    """Record tracking sync status with priority and metrics"""
    record_id: str
    table_name: str
    operation: str  # insert, update, delete
    timestamp: datetime
    source_db: DatabaseType
    target_db: DatabaseType
    status: SyncStatus
    priority: SyncPriority = SyncPriority.NORMAL
    retry_count: int = 0
    last_error: Optional[str] = None
    data_hash: Optional[str] = None
    size_bytes: int = 0
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class ConnectionMetrics:
    """Real-time connection pool metrics"""
    active_connections: int = 0
    idle_connections: int = 0
    max_connections: int = 10
    connection_errors: int = 0
    avg_latency_ms: float = 0.0
    query_count: int = 0
    moving_avg_load: float = 0.0  # Moving average of load
    last_updated: datetime = field(default_factory=datetime.now)


@dataclass
class SyncMetrics:
    """Comprehensive sync performance metrics"""
    # Latency
    local_read_latency_ms: float = 0.0
    cloud_write_latency_ms: float = 0.0
    cache_hit_latency_ms: float = 0.0

    # Queue stats
    sync_queue_size: int = 0
    priority_queue_sizes: Dict[str, int] = field(default_factory=dict)

    # Sync counts
    total_synced: int = 0
    total_failed: int = 0
    total_deferred: int = 0

    # Cache stats
    cache_hits: int = 0
    cache_misses: int = 0
    cache_size: int = 0

    # Connection health
    cloudsql_available: bool = False
    circuit_state: str = "closed"
    connection_pool_load: float = 0.0

    # Timestamps
    last_sync_time: Optional[datetime] = None
    last_health_check: Optional[datetime] = None
    last_cache_refresh: Optional[datetime] = None
    uptime_seconds: float = 0.0

    # Voice profile cache stats
    voice_profiles_cached: int = 0
    voice_cache_last_updated: Optional[datetime] = None


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker configuration"""
    failure_threshold: int = 5
    success_threshold: int = 2
    timeout_seconds: float = 60.0
    half_open_timeout: float = 30.0


class ConnectionOrchestrator:
    """
    Advanced connection pool orchestrator with predictive scaling and health monitoring.
    """

    def __init__(self, config: Dict[str, Any], max_connections: int = 5):
        self.config = config
        self.max_connections = max_connections
        self.min_connections = max(1, max_connections // 4)

        self.pool: Optional[asyncpg.Pool] = None
        self.metrics = ConnectionMetrics(max_connections=max_connections)
        self.load_history: Deque[float] = deque(maxlen=100)  # Track last 100 measurements
        self.lock = asyncio.Lock()

        logger.info(f"🎛️  Connection Orchestrator initialized (min={self.min_connections}, max={self.max_connections})")

    async def initialize(self):
        """Create connection pool with dynamic sizing"""
        try:
            self.pool = await asyncpg.create_pool(
                host=self.config.get("host", "127.0.0.1"),
                port=self.config.get("port", 5432),
                database=self.config.get("database"),
                user=self.config.get("user"),
                password=self.config.get("password"),
                min_size=self.min_connections,
                max_size=self.max_connections,
                timeout=5.0,
                command_timeout=10.0,
                max_queries=50000,  # Prevent connection exhaustion
                max_inactive_connection_lifetime=300.0  # Close idle connections after 5min
            )
            logger.info("✅ Connection pool created")
            return True
        except Exception as e:
            logger.error(f"❌ Pool creation failed: {e}")
            return False

    async def acquire(self, timeout: float = 5.0):
        """Acquire connection with timeout and metrics tracking"""
        if not self.pool:
            raise RuntimeError("Connection pool not initialized")

        start_time = time.time()
        try:
            conn = await asyncio.wait_for(self.pool.acquire(), timeout=timeout)
            latency = (time.time() - start_time) * 1000
            self.metrics.active_connections += 1
            self.metrics.query_count += 1
            self.metrics.avg_latency_ms = (self.metrics.avg_latency_ms * 0.9) + (latency * 0.1)

            return conn
        except asyncio.TimeoutError:
            self.metrics.connection_errors += 1
            logger.warning(f"⏱️  Connection acquire timeout ({timeout}s)")
            raise

    async def release(self, conn):
        """Release connection back to pool"""
        if self.pool and conn:
            await self.pool.release(conn)
            self.metrics.active_connections = max(0, self.metrics.active_connections - 1)

    def update_load_metrics(self):
        """Update moving average load for predictive scaling"""
        if self.pool:
            current_load = self.pool.get_size() / self.max_connections
            self.load_history.append(current_load)

            # Calculate moving average
            if len(self.load_history) > 0:
                self.metrics.moving_avg_load = sum(self.load_history) / len(self.load_history)

    async def scale_if_needed(self):
        """Dynamically adjust pool size based on load"""
        if not self.pool:
            return

        self.update_load_metrics()

        # If load > 80%, log warning (can't resize asyncpg pool on the fly)
        if self.metrics.moving_avg_load > 0.8:
            logger.warning(f"⚠️  High connection load: {self.metrics.moving_avg_load:.1%}")

    async def close(self):
        """Close all connections"""
        if self.pool:
            await self.pool.close()
            logger.info("✅ Connection pool closed")


class CircuitBreaker:
    """
    Circuit breaker for CloudSQL with automatic offline mode and recovery.
    """

    def __init__(self, config: Optional[CircuitBreakerConfig] = None):
        self.config = config or CircuitBreakerConfig()
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.lock = Lock()

        logger.info("🔌 Circuit Breaker initialized")

    def record_success(self):
        """Record successful operation"""
        with self.lock:
            self.failure_count = 0

            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.config.success_threshold:
                    self._transition_to_closed()

    def record_failure(self):
        """Record failed operation"""
        with self.lock:
            self.failure_count += 1
            self.last_failure_time = datetime.now()

            if self.state == CircuitState.CLOSED:
                if self.failure_count >= self.config.failure_threshold:
                    self._transition_to_open()
            elif self.state == CircuitState.HALF_OPEN:
                self._transition_to_open()

    def can_attempt(self) -> bool:
        """Check if request can be attempted"""
        with self.lock:
            if self.state == CircuitState.CLOSED:
                return True
            elif self.state == CircuitState.OPEN:
                # Check if timeout expired
                if self.last_failure_time:
                    elapsed = (datetime.now() - self.last_failure_time).total_seconds()
                    if elapsed >= self.config.timeout_seconds:
                        self._transition_to_half_open()
                        return True
                return False
            elif self.state == CircuitState.HALF_OPEN:
                return True
        return False

    def _transition_to_closed(self):
        """Transition to CLOSED state (normal operation)"""
        logger.info("✅ Circuit CLOSED - resuming normal operation")
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0

    def _transition_to_open(self):
        """Transition to OPEN state (reject requests)"""
        logger.warning("⚠️  Circuit OPEN - entering offline mode")
        self.state = CircuitState.OPEN
        self.success_count = 0

    def _transition_to_half_open(self):
        """Transition to HALF_OPEN state (testing recovery)"""
        logger.info("🔄 Circuit HALF_OPEN - testing recovery")
        self.state = CircuitState.HALF_OPEN
        self.failure_count = 0
        self.success_count = 0

    def get_state(self) -> CircuitState:
        """Get current circuit state"""
        return self.state


class FAISSVectorCache:
    """
    High-performance in-memory vector cache using FAISS for sub-millisecond similarity search.
    """

    def __init__(self, embedding_dim: int = 192):
        self.embedding_dim = embedding_dim
        self.index: Optional[faiss.Index] = None
        self.id_to_name: Dict[int, str] = {}
        self.name_to_id: Dict[str, int] = {}
        self.metadata: Dict[int, Dict[str, Any]] = {}
        self.lock = RLock()
        self.next_id = 0

        if not FAISS_AVAILABLE:
            logger.warning("⚠️  FAISS not available - vector cache disabled")
            return

        # Create FAISS index (L2 distance for speaker embeddings)
        self.index = faiss.IndexFlatL2(embedding_dim)
        logger.info(f"🚀 FAISS cache initialized ({embedding_dim}D embeddings)")

    def add_embedding(self, speaker_name: str, embedding: np.ndarray, metadata: Optional[Dict] = None):
        """Add embedding to cache"""
        if not self.index or not FAISS_AVAILABLE:
            return

        with self.lock:
            # Check if already exists
            if speaker_name in self.name_to_id:
                # Update existing
                idx = self.name_to_id[speaker_name]
                # FAISS doesn't support update, so we'd need to rebuild
                logger.debug(f"Embedding for {speaker_name} already in cache")
                return

            # Add new embedding
            embedding_vector = embedding.reshape(1, -1).astype('float32')
            self.index.add(embedding_vector)

            self.id_to_name[self.next_id] = speaker_name
            self.name_to_id[speaker_name] = self.next_id
            if metadata:
                self.metadata[self.next_id] = metadata

            self.next_id += 1
            logger.debug(f"✅ Added {speaker_name} to FAISS cache")

    def search_similar(self, embedding: np.ndarray, k: int = 1) -> List[Tuple[str, float, Dict]]:
        """Search for similar embeddings (returns list of (name, distance, metadata))"""
        if not self.index or not FAISS_AVAILABLE or self.index.ntotal == 0:
            return []

        with self.lock:
            embedding_vector = embedding.reshape(1, -1).astype('float32')
            distances, indices = self.index.search(embedding_vector, min(k, self.index.ntotal))

            results = []
            for dist, idx in zip(distances[0], indices[0]):
                if idx in self.id_to_name:
                    name = self.id_to_name[idx]
                    metadata = self.metadata.get(idx, {})
                    results.append((name, float(dist), metadata))

            return results

    def get_by_name(self, speaker_name: str) -> Optional[Dict[str, Any]]:
        """Get metadata by speaker name"""
        if not FAISS_AVAILABLE:
            return None

        with self.lock:
            if speaker_name in self.name_to_id:
                idx = self.name_to_id[speaker_name]
                return self.metadata.get(idx)
        return None

    def size(self) -> int:
        """Get cache size"""
        if self.index and FAISS_AVAILABLE:
            return self.index.ntotal
        return 0

    def clear(self):
        """Clear cache"""
        if self.index and FAISS_AVAILABLE:
            with self.lock:
                self.index.reset()
                self.id_to_name.clear()
                self.name_to_id.clear()
                self.metadata.clear()
                self.next_id = 0
                logger.info("🗑️  FAISS cache cleared")


class PriorityQueue:
    """
    Multi-priority async queue with backpressure control.
    """

    def __init__(self, max_size_per_priority: Optional[Dict[SyncPriority, int]] = None):
        self.queues: Dict[SyncPriority, Deque[SyncRecord]] = {
            priority: deque() for priority in SyncPriority
        }
        self.max_sizes = max_size_per_priority or {
            SyncPriority.CRITICAL: 1000,
            SyncPriority.HIGH: 500,
            SyncPriority.NORMAL: 200,
            SyncPriority.LOW: 100,
            SyncPriority.DEFERRED: 50
        }
        self.lock = asyncio.Lock()
        self.not_empty = asyncio.Event()

    async def put(self, record: SyncRecord, priority: Optional[SyncPriority] = None):
        """Add record with priority (drops if over limit)"""
        if priority:
            record.priority = priority

        async with self.lock:
            queue = self.queues[record.priority]
            max_size = self.max_sizes[record.priority]

            if len(queue) >= max_size:
                # Backpressure: defer or drop
                if record.priority == SyncPriority.CRITICAL:
                    # Never drop critical
                    logger.warning(f"⚠️  Critical queue full, forcing insert")
                    queue.append(record)
                elif record.priority != SyncPriority.DEFERRED:
                    # Downgrade priority
                    logger.debug(f"⬇️  Downgrading {record.priority} to DEFERRED")
                    record.priority = SyncPriority.DEFERRED
                    self.queues[SyncPriority.DEFERRED].append(record)
                else:
                    logger.warning(f"🗑️  Dropping deferred record (queue full)")
                    return
            else:
                queue.append(record)

            self.not_empty.set()

    async def get(self, timeout: Optional[float] = None) -> Optional[SyncRecord]:
        """Get highest priority record"""
        try:
            if timeout:
                await asyncio.wait_for(self.not_empty.wait(), timeout=timeout)
            else:
                await self.not_empty.wait()
        except asyncio.TimeoutError:
            return None

        async with self.lock:
            # Try priorities in order
            for priority in SyncPriority:
                queue = self.queues[priority]
                if queue:
                    record = queue.popleft()

                    # Check if any queues still have items
                    if not any(q for q in self.queues.values() if q):
                        self.not_empty.clear()

                    return record

        return None

    def size(self, priority: Optional[SyncPriority] = None) -> int:
        """Get queue size"""
        if priority:
            return len(self.queues[priority])
        return sum(len(q) for q in self.queues.values())

    def sizes_by_priority(self) -> Dict[str, int]:
        """Get sizes for all priorities"""
        return {p.name: len(self.queues[p]) for p in SyncPriority}


class PrometheusMetrics:
    """
    Prometheus metrics exporter for hybrid sync telemetry.
    Uses singleton registry to prevent duplicate metric registration.
    """

    def __init__(self, port: int = 9090):
        self.port = port
        self.enabled = PROMETHEUS_AVAILABLE
        self.server_started = False

        if not self.enabled:
            return

        # Try to get existing metrics or create new ones
        try:
            from prometheus_client import REGISTRY

            # Try to get existing collectors
            try:
                self.cache_hits = REGISTRY._names_to_collectors.get('jarvis_cache_hits_total')
                self.cache_misses = REGISTRY._names_to_collectors.get('jarvis_cache_misses_total')
                self.syncs_total = REGISTRY._names_to_collectors.get('jarvis_syncs_total')
                self.queries_total = REGISTRY._names_to_collectors.get('jarvis_queries_total')
                self.queue_size = REGISTRY._names_to_collectors.get('jarvis_queue_size')
                self.connection_pool_size = REGISTRY._names_to_collectors.get('jarvis_connection_pool_size')
                self.connection_pool_load = REGISTRY._names_to_collectors.get('jarvis_connection_pool_load')
                self.circuit_state = REGISTRY._names_to_collectors.get('jarvis_circuit_state')
                self.cache_size = REGISTRY._names_to_collectors.get('jarvis_cache_size')
                self.read_latency = REGISTRY._names_to_collectors.get('jarvis_read_latency_seconds')
                self.write_latency = REGISTRY._names_to_collectors.get('jarvis_write_latency_seconds')
                self.sync_latency = REGISTRY._names_to_collectors.get('jarvis_sync_latency_seconds')

                # If any are None, we need to create them
                if not all([self.cache_hits, self.cache_misses, self.syncs_total]):
                    raise ValueError("Metrics not fully registered")

                logger.info("✅ Reusing existing Prometheus metrics")

            except (ValueError, AttributeError):
                # Create new metrics
                self.cache_hits = Counter('jarvis_cache_hits_total', 'Total cache hits')
                self.cache_misses = Counter('jarvis_cache_misses_total', 'Total cache misses')
                self.syncs_total = Counter('jarvis_syncs_total', 'Total sync operations', ['status'])
                self.queries_total = Counter('jarvis_queries_total', 'Total database queries', ['type'])

                # Gauges
                self.queue_size = Gauge('jarvis_queue_size', 'Current sync queue size')
                self.connection_pool_size = Gauge('jarvis_connection_pool_size', 'Current connection pool size')
                self.connection_pool_load = Gauge('jarvis_connection_pool_load', 'Connection pool load percentage')
                self.circuit_state = Gauge('jarvis_circuit_state', 'Circuit breaker state (0=closed, 1=open, 2=half-open)')
                self.cache_size = Gauge('jarvis_cache_size', 'FAISS cache size')

                # Histograms
                self.read_latency = Histogram('jarvis_read_latency_seconds', 'Read operation latency', ['source'])
                self.write_latency = Histogram('jarvis_write_latency_seconds', 'Write operation latency')
                self.sync_latency = Histogram('jarvis_sync_latency_seconds', 'Sync operation latency')

        except Exception as e:
            logger.warning(f"⚠️  Prometheus metrics initialization failed: {e}")
            self.enabled = False

        logger.info(f"📊 Prometheus metrics enabled on port {port}")

    def start_server(self):
        """Start Prometheus metrics HTTP server (only if not already started)"""
        if self.enabled and not self.server_started:
            try:
                start_http_server(self.port)
                self.server_started = True
                logger.info(f"✅ Prometheus server started: http://localhost:{self.port}/metrics")
            except OSError as e:
                if "Address already in use" in str(e):
                    logger.info(f"ℹ️  Prometheus server already running on port {self.port}")
                    self.server_started = True
                else:
                    logger.warning(f"⚠️  Prometheus server failed to start: {e}")
            except Exception as e:
                logger.warning(f"⚠️  Prometheus server failed to start: {e}")

    def record_cache_hit(self):
        if self.enabled:
            self.cache_hits.inc()

    def record_cache_miss(self):
        if self.enabled:
            self.cache_misses.inc()

    def record_sync(self, status: str):
        if self.enabled:
            self.syncs_total.labels(status=status).inc()

    def record_query(self, query_type: str):
        if self.enabled:
            self.queries_total.labels(type=query_type).inc()

    def update_queue_size(self, size: int):
        if self.enabled:
            self.queue_size.set(size)

    def update_connection_pool(self, size: int, load: float):
        if self.enabled:
            self.connection_pool_size.set(size)
            self.connection_pool_load.set(load)

    def update_circuit_state(self, state: CircuitState):
        if self.enabled:
            state_map = {CircuitState.CLOSED: 0, CircuitState.OPEN: 1, CircuitState.HALF_OPEN: 2}
            self.circuit_state.set(state_map.get(state, 0))

    def update_cache_size(self, size: int):
        if self.enabled:
            self.cache_size.set(size)

    def observe_read_latency(self, latency_seconds: float, source: str):
        if self.enabled:
            self.read_latency.labels(source=source).observe(latency_seconds)

    def observe_write_latency(self, latency_seconds: float):
        if self.enabled:
            self.write_latency.observe(latency_seconds)

    def observe_sync_latency(self, latency_seconds: float):
        if self.enabled:
            self.sync_latency.observe(latency_seconds)


class RedisMetrics:
    """
    Redis-based metrics storage and retrieval for distributed monitoring.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url
        self.redis: Optional[aioredis.Redis] = None
        self.enabled = REDIS_AVAILABLE
        self.key_prefix = "jarvis:metrics:"

        if not self.enabled:
            logger.warning("⚠️  Redis not available - distributed metrics disabled")

    async def connect(self):
        """Connect to Redis"""
        if not self.enabled:
            return False

        try:
            self.redis = await aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_timeout=5.0,
                socket_connect_timeout=5.0
            )
            await self.redis.ping()
            logger.info(f"✅ Redis connected: {self.redis_url}")
            return True
        except Exception as e:
            logger.warning(f"⚠️  Redis connection failed: {e}")
            self.enabled = False
            return False

    async def set_metric(self, key: str, value: Any, ttl: int = 3600):
        """Set metric with TTL (default 1 hour)"""
        if not self.enabled or not self.redis:
            return

        try:
            full_key = f"{self.key_prefix}{key}"
            await self.redis.setex(full_key, ttl, json.dumps(value))
        except Exception as e:
            logger.debug(f"Redis set failed: {e}")

    async def get_metric(self, key: str) -> Optional[Any]:
        """Get metric value"""
        if not self.enabled or not self.redis:
            return None

        try:
            full_key = f"{self.key_prefix}{key}"
            value = await self.redis.get(full_key)
            return json.loads(value) if value else None
        except Exception as e:
            logger.debug(f"Redis get failed: {e}")
            return None

    async def increment(self, key: str, amount: int = 1) -> int:
        """Increment counter"""
        if not self.enabled or not self.redis:
            return 0

        try:
            full_key = f"{self.key_prefix}{key}"
            return await self.redis.incrby(full_key, amount)
        except Exception as e:
            logger.debug(f"Redis incr failed: {e}")
            return 0

    async def push_to_timeseries(self, key: str, value: float, timestamp: Optional[float] = None):
        """Push metric to time series (sorted set)"""
        if not self.enabled or not self.redis:
            return

        try:
            full_key = f"{self.key_prefix}ts:{key}"
            score = timestamp or time.time()
            await self.redis.zadd(full_key, {str(value): score})
            # Keep only last 1000 entries
            await self.redis.zremrangebyrank(full_key, 0, -1001)
        except Exception as e:
            logger.debug(f"Redis timeseries push failed: {e}")

    async def get_timeseries(self, key: str, limit: int = 100) -> List[Tuple[float, float]]:
        """Get recent time series data"""
        if not self.enabled or not self.redis:
            return []

        try:
            full_key = f"{self.key_prefix}ts:{key}"
            data = await self.redis.zrevrange(full_key, 0, limit - 1, withscores=True)
            return [(float(val), float(score)) for val, score in data]
        except Exception as e:
            logger.debug(f"Redis timeseries get failed: {e}")
            return []

    async def close(self):
        """Close Redis connection"""
        if self.redis:
            await self.redis.close()
            logger.info("✅ Redis connection closed")


class MLCachePrefetcher:
    """
    ML-based predictive cache warming using usage patterns.
    """

    def __init__(self, faiss_cache: Optional[FAISSVectorCache] = None):
        self.faiss_cache = faiss_cache
        self.access_history: Deque[Tuple[str, float]] = deque(maxlen=1000)  # Last 1000 accesses
        self.access_patterns: Dict[str, List[float]] = defaultdict(list)  # speaker_name -> access times
        self.prediction_threshold = 0.7  # Confidence threshold for prefetching
        self.lock = Lock()

        logger.info("🧠 ML Cache Prefetcher initialized")

    def record_access(self, speaker_name: str):
        """Record cache access for pattern learning"""
        with self.lock:
            current_time = time.time()
            self.access_history.append((speaker_name, current_time))
            self.access_patterns[speaker_name].append(current_time)

            # Keep only last 100 accesses per speaker
            if len(self.access_patterns[speaker_name]) > 100:
                self.access_patterns[speaker_name] = self.access_patterns[speaker_name][-100:]

    def predict_next_accesses(self, window_seconds: float = 300.0) -> List[Tuple[str, float]]:
        """
        Predict which speakers are likely to be accessed in the next time window.
        Returns list of (speaker_name, confidence) tuples.
        """
        with self.lock:
            current_time = time.time()
            predictions = []

            for speaker_name, access_times in self.access_patterns.items():
                if len(access_times) < 3:
                    continue

                # Calculate access frequency (accesses per hour)
                time_range = access_times[-1] - access_times[0]
                if time_range < 60:  # Less than 1 minute of data
                    continue

                access_frequency = len(access_times) / (time_range / 3600.0)

                # Calculate time since last access
                time_since_last = current_time - access_times[-1]

                # Simple heuristic: if accessed frequently and recently, likely to be accessed again
                if access_frequency > 1.0 and time_since_last < window_seconds:
                    confidence = min(0.9, access_frequency / 10.0)
                    predictions.append((speaker_name, confidence))

                # Pattern: repeated accesses at regular intervals
                if len(access_times) >= 5:
                    intervals = [access_times[i] - access_times[i-1] for i in range(1, len(access_times))]
                    avg_interval = sum(intervals) / len(intervals)
                    std_interval = (sum((x - avg_interval) ** 2 for x in intervals) / len(intervals)) ** 0.5

                    # If interval is regular (low std dev) and we're near next expected access
                    if std_interval < avg_interval * 0.3:  # Low variance
                        time_until_next = avg_interval - time_since_last
                        if 0 < time_until_next < window_seconds:
                            confidence = 0.8
                            predictions.append((speaker_name, confidence))

            # Sort by confidence, filter by threshold
            predictions = [(name, conf) for name, conf in predictions if conf >= self.prediction_threshold]
            predictions.sort(key=lambda x: x[1], reverse=True)

            return predictions[:10]  # Top 10 predictions

    async def prefetch_predicted(self, sqlite_conn: aiosqlite.Connection):
        """Prefetch predicted speakers into FAISS cache"""
        if not self.faiss_cache:
            return

        predictions = self.predict_next_accesses()

        if not predictions:
            return

        logger.info(f"🔮 Prefetching {len(predictions)} predicted speakers")

        for speaker_name, confidence in predictions:
            # Check if already in cache
            if self.faiss_cache.get_by_name(speaker_name):
                continue

            # Load from SQLite and add to cache
            try:
                async with sqlite_conn.execute(
                    "SELECT voiceprint_embedding, acoustic_features, total_samples FROM speaker_profiles WHERE speaker_name = ?",
                    (speaker_name,)
                ) as cursor:
                    row = await cursor.fetchone()

                    if row:
                        embedding_bytes, features_json, total_samples = row
                        if embedding_bytes:
                            embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
                            metadata = {
                                "acoustic_features": json.loads(features_json) if features_json else {},
                                "total_samples": total_samples,
                                "prefetched": True,
                                "confidence": confidence
                            }
                            self.faiss_cache.add_embedding(speaker_name, embedding, metadata)
                            logger.debug(f"✅ Prefetched {speaker_name} (confidence: {confidence:.2f})")
            except Exception as e:
                logger.debug(f"Prefetch failed for {speaker_name}: {e}")

    def get_statistics(self) -> Dict[str, Any]:
        """Get prefetcher statistics"""
        with self.lock:
            return {
                "total_accesses": len(self.access_history),
                "unique_speakers": len(self.access_patterns),
                "active_patterns": sum(1 for times in self.access_patterns.values() if len(times) >= 3),
                "prediction_threshold": self.prediction_threshold
            }


class HybridDatabaseSync:
    """
    Advanced hybrid database synchronization system for voice biometrics.

    Features:
    - Connection Orchestrator with dynamic pool management
    - Circuit Breaker with automatic offline mode
    - FAISS vector cache for <1ms authentication
    - Priority-based write-behind queue with backpressure
    - SHA-256 delta sync with conflict resolution
    - Zero live queries during authentication
    - Self-healing and auto-recovery
    - Comprehensive metrics and telemetry

    SINGLETON: Use get_instance() to get the shared instance.
    """

    # Singleton instance
    _instance: Optional['HybridDatabaseSync'] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, **kwargs) -> 'HybridDatabaseSync':
        """Get singleton instance of HybridDatabaseSync."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls(**kwargs)
                    logger.info("🔧 HybridDatabaseSync singleton created")
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset singleton instance (for testing)."""
        with cls._instance_lock:
            if cls._instance is not None:
                cls._instance._shutdown = True
                cls._instance = None

    def __init__(
        self,
        sqlite_path: Optional[Path] = None,
        cloudsql_config: Optional[Dict[str, Any]] = None,
        sync_interval_seconds: int = 30,
        max_retry_attempts: int = 5,
        batch_size: int = 50,
        max_connections: int = 3,  # Reduced from 10
        enable_faiss_cache: bool = True,
        enable_prometheus: bool = True,
        enable_redis: bool = True,
        enable_ml_prefetch: bool = True,
        prometheus_port: int = 9090,
        redis_url: str = "redis://localhost:6379"
    ):
        """
        Initialize advanced hybrid sync system with Phase 2 features.

        Args:
            sqlite_path: Path to local SQLite database (auto-configured if None)
            cloudsql_config: CloudSQL connection config (auto-configured if None)
            sync_interval_seconds: Interval between sync runs
            max_retry_attempts: Maximum retry attempts
            batch_size: Records per sync batch
            max_connections: Maximum CloudSQL connections
            enable_faiss_cache: Enable FAISS vector cache
            enable_prometheus: Enable Prometheus metrics export
            enable_redis: Enable Redis distributed metrics
            enable_ml_prefetch: Enable ML-based predictive cache warming
            prometheus_port: Prometheus HTTP server port
            redis_url: Redis connection URL
        """
        # Auto-configure from DatabaseConfig if not provided
        if sqlite_path is None or cloudsql_config is None:
            from intelligence.cloud_database_adapter import DatabaseConfig
            config = DatabaseConfig()

            if sqlite_path is None:
                sqlite_path = config.sqlite_path
                logger.debug(f"Auto-configured sqlite_path: {sqlite_path}")

            if cloudsql_config is None:
                cloudsql_config = {
                    "host": config.db_host,
                    "port": config.db_port,
                    "database": config.db_name,
                    "user": config.db_user,
                    "password": config.db_password,
                }
                logger.debug(f"Auto-configured cloudsql_config for database: {config.db_name}")

        self.sqlite_path = sqlite_path
        self.cloudsql_config = cloudsql_config
        self.sync_interval = sync_interval_seconds
        self.max_retry_attempts = max_retry_attempts
        self.batch_size = batch_size
        self.enable_faiss_cache = enable_faiss_cache and FAISS_AVAILABLE

        # Connection management
        self.sqlite_conn: Optional[aiosqlite.Connection] = None

        # Use singleton connection manager instead of ConnectionOrchestrator
        self.connection_manager = get_connection_manager() if CONNECTION_MANAGER_AVAILABLE else None
        self.cloudsql_config = cloudsql_config
        self.max_connections = max_connections

        # Circuit breaker
        self.circuit_breaker = CircuitBreaker()

        # Priority queue for sync operations
        self.sync_queue = PriorityQueue()
        self.pending_syncs: Dict[str, SyncRecord] = {}
        self.sync_lock = asyncio.Lock()

        # FAISS vector cache
        self.faiss_cache: Optional[FAISSVectorCache] = None
        if self.enable_faiss_cache:
            self.faiss_cache = FAISSVectorCache(embedding_dim=192)

        # Phase 2: Prometheus metrics
        self.prometheus: Optional[PrometheusMetrics] = None
        if enable_prometheus and PROMETHEUS_AVAILABLE:
            self.prometheus = PrometheusMetrics(port=prometheus_port)

        # Phase 2: Redis metrics
        self.redis: Optional[RedisMetrics] = None
        if enable_redis and REDIS_AVAILABLE:
            self.redis = RedisMetrics(redis_url=redis_url)

        # Phase 2: ML prefetcher
        self.ml_prefetcher: Optional[MLCachePrefetcher] = None
        if enable_ml_prefetch and self.faiss_cache:
            self.ml_prefetcher = MLCachePrefetcher(faiss_cache=self.faiss_cache)

        # Thread pool for parallel operations (use daemon threads for clean shutdown)
        if DAEMON_EXECUTOR_AVAILABLE:
            self.thread_pool = DaemonThreadPoolExecutor(max_workers=4, thread_name_prefix='HybridSync')
        else:
            self.thread_pool = ThreadPoolExecutor(max_workers=4)

        # Background tasks
        self.sync_task: Optional[asyncio.Task] = None
        self.health_check_task: Optional[asyncio.Task] = None
        self.metrics_task: Optional[asyncio.Task] = None
        self.prefetch_task: Optional[asyncio.Task] = None
        self._shutdown = False
        self._start_time = time.time()

        # Health tracking
        self.cloudsql_healthy = False
        self.last_health_check = datetime.now()

        # Startup mode: suppress CloudSQL warnings until proxy is confirmed ready
        # This prevents noisy logs during early startup when proxy hasn't started yet
        self._startup_mode = True
        self._proxy_ready = False
        self._startup_grace_period = 60  # seconds to wait before CloudSQL attempts

        # Metrics
        self.metrics = SyncMetrics()

        logger.info(f"🚀 Advanced Hybrid Sync V2.0 initialized")
        logger.info(f"   SQLite: {sqlite_path}")
        logger.info(f"   Max Connections: {max_connections}")
        logger.info(f"   FAISS Cache: {'Enabled' if self.enable_faiss_cache else 'Disabled'}")
        logger.info(f"   Prometheus: {'Enabled' if self.prometheus else 'Disabled'}")
        logger.info(f"   Redis: {'Enabled' if self.redis else 'Disabled'}")
        logger.info(f"   ML Prefetcher: {'Enabled' if self.ml_prefetcher else 'Disabled'}")

    @property
    def is_initialized(self) -> bool:
        """Check if the hybrid sync system is fully initialized with CloudSQL."""
        return (
            self.connection_manager is not None
            and self.connection_manager.is_initialized
            and self.sqlite_conn is not None
        )

    def set_proxy_ready(self, ready: bool = True):
        """
        Signal that the Cloud SQL proxy is ready for connections.
        
        Call this after the proxy has been started to enable CloudSQL
        reconnection attempts in the health check loop.
        """
        self._proxy_ready = ready
        self._startup_mode = not ready
        if ready:
            logger.info("✅ CloudSQL proxy marked as ready - enabling reconnection attempts")
        else:
            logger.info("⏸️  CloudSQL proxy marked as not ready - suppressing reconnection attempts")

    @property
    def is_proxy_ready(self) -> bool:
        """Check if the Cloud SQL proxy has been signaled as ready."""
        return self._proxy_ready

    async def initialize(self):
        """Initialize database connections and start background sync"""
        # Initialize SQLite (always available)
        await self._init_sqlite()

        # Initialize CloudSQL connection orchestrator (may fail gracefully)
        await self._init_cloudsql_with_circuit_breaker()

        # Phase 2: Start Prometheus metrics server
        if self.prometheus:
            self.prometheus.start_server()

        # Phase 2: Connect to Redis
        if self.redis:
            await self.redis.connect()

        # Preload FAISS cache from SQLite for <1ms authentication
        if self.faiss_cache:
            await self._preload_faiss_cache()

            # Bootstrap voice profiles from CloudSQL if SQLite cache is empty
            if self.faiss_cache.size() == 0:
                # Check if SQLite actually has profiles (might just need FAISS reload)
                async with self.sqlite_conn.execute("SELECT COUNT(*) FROM speaker_profiles") as cursor:
                    row = await cursor.fetchone()
                    sqlite_count = row[0] if row else 0

                if sqlite_count > 0:
                    # Profiles exist in SQLite, just reload FAISS cache
                    logger.info(f"✅ Voice profiles already cached in SQLite ({sqlite_count} profiles)")
                    logger.info("   FAISS cache will auto-load on first authentication")
                else:
                    # SQLite is empty - bootstrap from CloudSQL
                    logger.info("📥 SQLite cache empty - attempting bootstrap from CloudSQL...")
                    bootstrap_success = await self.bootstrap_voice_profiles_from_cloudsql()
                    if bootstrap_success:
                        logger.info("✅ Voice profiles bootstrapped - ready for offline authentication")
                    else:
                        logger.warning("⚠️  Bootstrap failed - voice authentication requires CloudSQL connection")
            else:
                logger.info(f"✅ Voice cache already loaded: {self.faiss_cache.size()} profile(s)")
                logger.info("   Skipping bootstrap - profiles already cached locally")

        # Start background services
        self.sync_task = asyncio.create_task(self._sync_loop())
        self.health_check_task = asyncio.create_task(self._health_check_loop())
        self.metrics_task = asyncio.create_task(self._metrics_loop())

        # Phase 2: Start ML prefetch loop
        if self.ml_prefetcher:
            self.prefetch_task = asyncio.create_task(self._ml_prefetch_loop())

        logger.info("✅ Advanced hybrid sync V2.0 initialized - zero live queries mode")
        logger.info("   🚀 Phase 2 features active: Prometheus, Redis, ML Prefetch")

    async def _init_sqlite(self):
        """Initialize local SQLite connection"""
        try:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self.sqlite_conn = await aiosqlite.connect(str(self.sqlite_path))

            # Enable WAL mode for better concurrency
            await self.sqlite_conn.execute("PRAGMA journal_mode=WAL")
            await self.sqlite_conn.execute("PRAGMA synchronous=NORMAL")
            await self.sqlite_conn.execute("PRAGMA cache_size=-64000")  # 64MB cache

            # Create sync tracking table
            await self.sqlite_conn.execute("""
                CREATE TABLE IF NOT EXISTS _sync_log (
                    sync_id TEXT PRIMARY KEY,
                    table_name TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    status TEXT NOT NULL,
                    retry_count INTEGER DEFAULT 0,
                    last_error TEXT,
                    data_hash TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create speaker_profiles table for FAISS cache preload
            await self.sqlite_conn.execute("""
                CREATE TABLE IF NOT EXISTS speaker_profiles (
                    speaker_id INTEGER PRIMARY KEY,
                    speaker_name TEXT UNIQUE NOT NULL,
                    voiceprint_embedding BLOB NOT NULL,
                    acoustic_features TEXT NOT NULL,
                    total_samples INTEGER DEFAULT 0,
                    last_updated TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Schema migration: Add acoustic_features column if missing (for existing tables)
            try:
                await self.sqlite_conn.execute(
                    "ALTER TABLE speaker_profiles ADD COLUMN acoustic_features TEXT DEFAULT '{}'"
                )
                logger.info("📦 Added acoustic_features column to speaker_profiles table")
            except Exception:
                # Column already exists, ignore
                pass

            await self.sqlite_conn.commit()

            logger.info("✅ SQLite initialized (WAL mode enabled)")

        except Exception as e:
            logger.error(f"❌ Failed to initialize SQLite: {e}")
            raise

    async def _init_cloudsql_with_circuit_breaker(self):
        """Initialize CloudSQL with intelligent proxy detection and circuit breaker"""
        if not CONNECTION_MANAGER_AVAILABLE or not ASYNCPG_AVAILABLE:
            logger.warning("⚠️  asyncpg or connection manager not available - CloudSQL disabled")
            self.circuit_breaker.record_failure()
            return

        # v5.0: Intelligent proxy detection before attempting connection
        if PROXY_DETECTOR_AVAILABLE:
            proxy_detector = get_proxy_detector()
            proxy_status, proxy_info = await proxy_detector.detect_proxy()

            if proxy_status == ProxyStatus.UNAVAILABLE:
                logger.info(f"ℹ️  {proxy_info}")
                logger.info("   Using SQLite-only mode (Cloud SQL unavailable)")
                self.circuit_breaker.record_failure()
                self.metrics.cloudsql_available = False
                return
            elif proxy_status == ProxyStatus.UNKNOWN:
                logger.debug(f"🔍 Proxy status unknown: {proxy_info}")
                # Continue with connection attempt despite uncertainty

        try:
            # Initialize singleton connection manager
            success = await self.connection_manager.initialize(
                host=self.cloudsql_config.get("host", "127.0.0.1"),
                port=self.cloudsql_config.get("port", 5432),
                database=self.cloudsql_config.get("database"),
                user=self.cloudsql_config.get("user"),
                password=self.cloudsql_config.get("password"),
                max_connections=self.max_connections,
                force_reinit=False  # Reuse existing pool if available
            )

            if success:
                # Test connection through circuit breaker
                if self.circuit_breaker.can_attempt():
                    try:
                        async with self.connection_manager.connection() as conn:
                            await conn.fetchval("SELECT 1")

                        self.circuit_breaker.record_success()
                        self.cloudsql_healthy = True
                        self.metrics.cloudsql_available = True
                        self.metrics.circuit_state = self.circuit_breaker.get_state().value
                        logger.info("✅ CloudSQL connected via singleton manager")
                    except Exception as e:
                        logger.warning(f"⚠️  CloudSQL test query failed: {e}")
                        self.circuit_breaker.record_failure()
                        self.metrics.cloudsql_available = False
            else:
                self.circuit_breaker.record_failure()
                self.metrics.cloudsql_available = False

        except Exception as e:
            logger.warning(f"⚠️  CloudSQL connection manager failed: {e}")
            logger.info("📱 Using cache-first offline mode (will retry in background)")
            self.circuit_breaker.record_failure()
            self.metrics.cloudsql_available = False

    async def _preload_faiss_cache(self):
        """Preload all voice embeddings from SQLite into FAISS cache"""
        if not self.faiss_cache:
            return

        try:
            logger.info("🔄 Preloading FAISS cache from SQLite...")
            start_time = time.time()

            async with self.sqlite_conn.execute("SELECT speaker_name, voiceprint_embedding, acoustic_features, total_samples FROM speaker_profiles") as cursor:
                count = 0
                async for row in cursor:
                    speaker_name, embedding_bytes, features_json, total_samples = row

                    if embedding_bytes:
                        embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
                        metadata = {
                            "speaker_name": speaker_name,
                            "acoustic_features": json.loads(features_json) if features_json else {},
                            "total_samples": total_samples
                        }

                        self.faiss_cache.add_embedding(speaker_name, embedding, metadata)
                        count += 1

            elapsed = (time.time() - start_time) * 1000
            self.metrics.cache_size = self.faiss_cache.size()
            logger.info(f"✅ FAISS cache preloaded: {count} embeddings in {elapsed:.1f}ms")

        except Exception as e:
            logger.error(f"❌ FAISS cache preload failed: {e}")

    async def bootstrap_voice_profiles_from_cloudsql(self, timeout: float = 30.0) -> bool:
        """
        Bootstrap voice profiles from CloudSQL to SQLite cache.
        Called on first startup when SQLite is empty.

        Args:
            timeout: Maximum time to wait for bootstrap (default 30s)

        Returns:
            True if successful, False otherwise
        """
        if not self.connection_manager or not self.connection_manager.is_initialized:
            logger.warning("⚠️  CloudSQL not available - cannot bootstrap voice profiles")
            return False

        try:
            logger.info(f"🔄 Bootstrapping voice profiles from CloudSQL (timeout: {timeout}s)...")
            start_time = time.time()

            # Wrap the entire bootstrap in a timeout to prevent startup hangs
            return await asyncio.wait_for(
                self._bootstrap_voice_profiles_impl(),
                timeout=timeout
            )

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            logger.warning(f"⏱️  Bootstrap timed out after {elapsed:.1f}s - continuing with local-only mode")
            return False
        except Exception as e:
            logger.error(f"❌ Voice profile bootstrap failed: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return False

    async def _bootstrap_voice_profiles_impl(self) -> bool:
        """Internal implementation of bootstrap - separated for timeout wrapper."""
        start_time = time.time()

        # Query all speaker profiles from CloudSQL
        async with self.connection_manager.connection() as conn:
            rows = await conn.fetch("""
                SELECT
                    speaker_id,
                    speaker_name,
                    voiceprint_embedding,
                    total_samples,
                    last_updated,
                    pitch_mean_hz,
                    pitch_std_hz,
                    formant_f1_hz,
                    formant_f2_hz,
                    spectral_centroid_hz,
                    speaking_rate_wpm,
                    energy_mean
                FROM speaker_profiles
                ORDER BY speaker_id
            """)

        if not rows:
            logger.warning("⚠️  No voice profiles found in CloudSQL")
            return False

        # Insert into SQLite
        synced_count = 0
        for row in rows:
            try:
                # Skip profiles with NULL embeddings (incomplete profiles)
                if not row['voiceprint_embedding']:
                    logger.debug(f"   Skipping profile {row['speaker_name']}: NULL embedding")
                    continue

                # Build acoustic_features JSON from individual columns
                acoustic_features = {
                    "pitch_mean_hz": float(row['pitch_mean_hz']) if row['pitch_mean_hz'] else 0.0,
                    "pitch_std_hz": float(row['pitch_std_hz']) if row['pitch_std_hz'] else 0.0,
                    "formant_f1_hz": float(row['formant_f1_hz']) if row['formant_f1_hz'] else 0.0,
                    "formant_f2_hz": float(row['formant_f2_hz']) if row['formant_f2_hz'] else 0.0,
                    "spectral_centroid_hz": float(row['spectral_centroid_hz']) if row['spectral_centroid_hz'] else 0.0,
                    "speaking_rate_wpm": float(row['speaking_rate_wpm']) if row['speaking_rate_wpm'] else 0.0,
                    "energy_mean": float(row['energy_mean']) if row['energy_mean'] else 0.0
                }
                acoustic_features_json = json.dumps(acoustic_features)

                await self.sqlite_conn.execute("""
                    INSERT OR REPLACE INTO speaker_profiles
                    (speaker_id, speaker_name, voiceprint_embedding, acoustic_features, total_samples, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    row['speaker_id'],
                    row['speaker_name'],
                    row['voiceprint_embedding'],
                    acoustic_features_json,
                    row['total_samples'],
                    row['last_updated']
                ))

                # Also add to FAISS cache
                if self.faiss_cache and row['voiceprint_embedding']:
                    embedding = np.frombuffer(bytes(row['voiceprint_embedding']), dtype=np.float32)
                    metadata = {
                        "speaker_name": row['speaker_name'],
                        "acoustic_features": acoustic_features,
                        "total_samples": row['total_samples']
                    }
                    self.faiss_cache.add_embedding(row['speaker_name'], embedding, metadata)

                synced_count += 1
                logger.debug(f"   Synced profile: {row['speaker_name']} ({row['total_samples']} samples)")

            except Exception as e:
                logger.error(f"❌ Failed to sync profile {row['speaker_name']}: {e}")
                continue

        await self.sqlite_conn.commit()

        elapsed = (time.time() - start_time) * 1000
        logger.info(f"✅ Bootstrapped {synced_count}/{len(rows)} voice profiles in {elapsed:.1f}ms")

        # Update metrics
        if self.faiss_cache:
            self.metrics.cache_size = self.faiss_cache.size()
            logger.info(f"   FAISS cache size: {self.metrics.cache_size} embeddings")

        self.metrics.voice_profiles_cached = synced_count
        self.metrics.voice_cache_last_updated = datetime.now()
        self.metrics.last_cache_refresh = datetime.now()

        return synced_count > 0

    async def _warm_cache_on_reconnection(self):
        """
        v18.0: Automatically warm voice profile cache when CloudSQL reconnects.
        Called by health check loop when connection is restored.

        Features:
        - Checks cache staleness before syncing
        - Only syncs if profiles are outdated or missing
        - Updates both SQLite and FAISS cache
        - Non-blocking background operation
        - v18.0: Overall timeout protection to prevent long connection holds
        """
        # v18.0: Overall timeout to prevent holding connections too long
        CACHE_WARM_TIMEOUT = float(os.getenv("CACHE_WARM_TIMEOUT_SECONDS", "30.0"))

        try:
            logger.info("🔥 Warming voice profile cache after reconnection...")

            # v18.0: Wrap entire operation in timeout
            try:
                # Check if cache needs refresh (with timeout)
                needs_refresh = await asyncio.wait_for(
                    self._check_cache_staleness(),
                    timeout=min(10.0, CACHE_WARM_TIMEOUT / 3)
                )
            except asyncio.TimeoutError:
                logger.warning("[v18.0] Cache staleness check timed out - assuming refresh needed")
                needs_refresh = True

            if not needs_refresh:
                logger.info("✅ Voice profile cache is fresh - no refresh needed")
                return

            # v18.0: Bootstrap/refresh voice profiles with timeout
            try:
                success = await asyncio.wait_for(
                    self.bootstrap_voice_profiles_from_cloudsql(),
                    timeout=CACHE_WARM_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning(f"[v18.0] Cache warming timed out after {CACHE_WARM_TIMEOUT}s")
                success = False

            if success:
                logger.info("✅ Voice profile cache warmed and ready for offline auth")
                self.metrics.last_cache_refresh = datetime.now()
            else:
                logger.warning("⚠️  Cache warming failed - will retry on next health check")

        except asyncio.CancelledError:
            logger.debug("[v18.0] Cache warming cancelled")
            raise
        except Exception as e:
            logger.error(f"❌ Cache warming failed: {e}")

    async def _check_cache_staleness(self) -> bool:
        """
        Check if voice profile cache needs refreshing.

        Returns:
            True if cache is stale or missing, False if fresh
        """
        try:
            # Check 1: Is FAISS cache empty?
            if not self.faiss_cache or self.faiss_cache.size() == 0:
                logger.info("📊 Cache check: FAISS cache is empty - refresh needed")
                return True

            # Check 2: Is SQLite cache empty?
            async with self.sqlite_conn.execute("SELECT COUNT(*) FROM speaker_profiles") as cursor:
                row = await cursor.fetchone()
                sqlite_count = row[0] if row else 0

            if sqlite_count == 0:
                logger.info("📊 Cache check: SQLite cache is empty - refresh needed")
                return True

            # Check 3: Compare counts with CloudSQL (only valid profiles with embeddings)
            if self.connection_manager and self.connection_manager.is_initialized:
                try:
                    async with self.connection_manager.connection() as conn:
                        cloudsql_count = await conn.fetchval("""
                            SELECT COUNT(*) FROM speaker_profiles
                            WHERE voiceprint_embedding IS NOT NULL
                        """)

                    if cloudsql_count != sqlite_count:
                        logger.info(f"📊 Cache check: Count mismatch (CloudSQL: {cloudsql_count}, SQLite: {sqlite_count}) - refresh needed")
                        return True

                    # Check 4: Has CloudSQL been updated since last cache refresh?
                    async with self.connection_manager.connection() as conn:
                        latest_update = await conn.fetchval("""
                            SELECT MAX(last_updated)
                            FROM speaker_profiles
                            WHERE voiceprint_embedding IS NOT NULL
                        """)

                    if latest_update:
                        # Get last refresh time from metrics or SQLite
                        async with self.sqlite_conn.execute("""
                            SELECT MAX(last_updated) FROM speaker_profiles
                        """) as cursor:
                            row = await cursor.fetchone()
                            cache_update = row[0] if row and row[0] else None

                        if not cache_update or latest_update > cache_update:
                            logger.info("📊 Cache check: CloudSQL has newer profiles - refresh needed")
                            return True

                except Exception as e:
                    logger.debug(f"Cache staleness check failed (CloudSQL unavailable): {e}")
                    # Can't check CloudSQL, assume cache is okay
                    return False

            logger.info(f"📊 Cache check: Cache is fresh ({sqlite_count} profiles)")
            return False  # Cache is fresh, no refresh needed

        except Exception as e:
            logger.error(f"Cache staleness check error: {e}")
            return True  # On error, assume refresh is needed

    async def ensure_voice_profiles_synced(self, force_refresh: bool = False) -> bool:
        """
        Ensure voice profiles are synced from CloudSQL to SQLite.
        
        ENHANCED v2.0: Integrates with VoiceProfileStartupService for
        production-grade voice profile synchronization.
        
        This method should be called during startup to ensure your voice
        profile is ready for instant unlock.
        
        Args:
            force_refresh: Force re-sync even if cache appears fresh
            
        Returns:
            True if profiles are synced and ready
        """
        try:
            logger.info("🔄 Ensuring voice profiles are synced...")
            
            # Method 1: Use VoiceProfileStartupService (preferred)
            try:
                from voice_unlock.voice_profile_startup_service import (
                    get_voice_profile_service,
                    is_voice_profile_ready,
                )
                
                service = get_voice_profile_service()
                
                # If service is already ready and no force refresh, we're done
                if is_voice_profile_ready() and not force_refresh:
                    profile_count = service.profile_count
                    logger.info(f"✅ Voice profiles already synced: {profile_count} profile(s)")
                    return True
                
                # Initialize the service (handles CloudSQL → SQLite sync)
                success = await service.initialize(timeout=30.0)
                
                if success:
                    profile_count = service.profile_count
                    metrics = service.metrics
                    
                    logger.info(
                        f"✅ Voice profiles synced via VoiceProfileStartupService: "
                        f"{profile_count} profile(s) "
                        f"(CloudSQL={metrics.profiles_from_cloudsql}, "
                        f"SQLite={metrics.profiles_from_sqlite})"
                    )
                    
                    # Update our FAISS cache with the loaded profiles
                    if self.faiss_cache:
                        for speaker_name, profile in service.get_all_profiles().items():
                            if profile.is_valid():
                                self.faiss_cache.add_embedding(
                                    speaker_name,
                                    profile.embedding,
                                    {
                                        "speaker_name": speaker_name,
                                        "source": profile.source.value,
                                        "confidence": profile.recognition_confidence,
                                    }
                                )
                        
                        self.metrics.cache_size = self.faiss_cache.size()
                        logger.info(f"   📦 FAISS cache updated: {self.metrics.cache_size} embedding(s)")
                    
                    return True
                else:
                    logger.warning("⚠️ VoiceProfileStartupService initialization failed")
                    
            except ImportError:
                logger.debug("VoiceProfileStartupService not available - using legacy sync")
            except Exception as e:
                logger.warning(f"VoiceProfileStartupService sync failed: {e}")
            
            # Method 2: Fall back to direct bootstrap
            logger.info("📂 Falling back to direct CloudSQL bootstrap...")
            
            if force_refresh or await self._check_cache_staleness():
                success = await self.bootstrap_voice_profiles_from_cloudsql()
                if success:
                    logger.info("✅ Voice profiles synced via direct bootstrap")
                    return True
            else:
                # Cache is fresh
                logger.info("✅ Voice profile cache is fresh - no sync needed")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"❌ Voice profile sync failed: {e}")
            return False

    async def get_voice_profile_sync_status(self) -> Dict[str, Any]:
        """
        Get comprehensive voice profile sync status.
        
        Returns:
            Dict with sync status, profile counts, and health info
        """
        status = {
            "sqlite_profiles": 0,
            "faiss_cache_size": 0,
            "cloudsql_available": False,
            "cloudsql_profiles": 0,
            "sync_status": "unknown",
            "last_sync": None,
            "profiles": [],
        }
        
        try:
            # Check SQLite
            if self.sqlite_conn:
                async with self.sqlite_conn.execute(
                    "SELECT speaker_name, total_samples, last_updated FROM speaker_profiles"
                ) as cursor:
                    rows = await cursor.fetchall()
                    status["sqlite_profiles"] = len(rows)
                    status["profiles"] = [
                        {"name": r[0], "samples": r[1], "updated": r[2]}
                        for r in rows
                    ]
            
            # Check FAISS cache
            if self.faiss_cache:
                status["faiss_cache_size"] = self.faiss_cache.size()
            
            # Check CloudSQL
            if self.connection_manager and self.connection_manager.is_initialized:
                status["cloudsql_available"] = True
                try:
                    async with self.connection_manager.connection() as conn:
                        status["cloudsql_profiles"] = await conn.fetchval(
                            "SELECT COUNT(*) FROM speaker_profiles WHERE voiceprint_embedding IS NOT NULL"
                        )
                except Exception:
                    status["cloudsql_available"] = False
            
            # Determine sync status
            if status["sqlite_profiles"] > 0 and status["faiss_cache_size"] > 0:
                if status["cloudsql_available"]:
                    if status["sqlite_profiles"] == status["cloudsql_profiles"]:
                        status["sync_status"] = "synced"
                    else:
                        status["sync_status"] = "out_of_sync"
                else:
                    status["sync_status"] = "local_only"
            elif status["sqlite_profiles"] == 0 and status["faiss_cache_size"] == 0:
                status["sync_status"] = "empty"
            else:
                status["sync_status"] = "partial"
            
            # Add last sync time
            if self.metrics.last_cache_refresh:
                status["last_sync"] = self.metrics.last_cache_refresh.isoformat()
            
        except Exception as e:
            logger.error(f"Failed to get sync status: {e}")
            status["sync_status"] = "error"
            status["error"] = str(e)
        
        return status

    async def _health_check_loop(self):
        """
        Background health check for CloudSQL connectivity and cache freshness with intelligent backoff.

        v5.0 Features:
        - Intelligent proxy detection before connection attempts
        - Exponential backoff when proxy unavailable (10s → 10m)
        - Auto-reconnects when proxy becomes available
        - Warms cache on reconnection
        - Periodically refreshes cache (every 5 minutes if CloudSQL healthy)
        - Clean logging - no spam when proxy not running
        """
        last_cache_refresh = datetime.now()
        cache_refresh_interval = 300  # 5 minutes

        # Get proxy detector for intelligent connection management
        proxy_detector = get_proxy_detector() if PROXY_DETECTOR_AVAILABLE else None

        while not self._shutdown:
            try:
                # v5.0: Use intelligent delay from proxy detector
                if proxy_detector and not self.cloudsql_healthy:
                    delay = proxy_detector.get_next_retry_delay()
                else:
                    delay = 10  # Standard 10-second health check when healthy

                await asyncio.sleep(delay)

                # Skip if already healthy and recently checked
                if self.cloudsql_healthy and (datetime.now() - self.last_health_check).seconds < 30:
                    # Check if it's time for periodic cache refresh
                    time_since_refresh = (datetime.now() - last_cache_refresh).seconds
                    if time_since_refresh > cache_refresh_interval:
                        logger.info("🔄 Periodic cache refresh triggered")
                        asyncio.create_task(self._warm_cache_on_reconnection())
                        last_cache_refresh = datetime.now()
                    continue

                # Try to reconnect if unhealthy
                if not self.cloudsql_healthy:
                    # CRITICAL: Don't attempt reconnection if core dependencies are missing
                    # asyncpg/connection_manager won't magically appear at runtime
                    if not CONNECTION_MANAGER_AVAILABLE or not ASYNCPG_AVAILABLE:
                        # Already logged during initialization - don't spam
                        # Only log once per hour at debug level
                        continue

                    # v5.0: Check if proxy detector says we should even try
                    if proxy_detector and not proxy_detector.should_retry():
                        # Proxy detector has determined proxy isn't available (local dev mode)
                        # Don't spam logs - already logged during initialization
                        continue

                    logger.info("🔄 Attempting CloudSQL reconnection...")
                    await self._init_cloudsql_with_circuit_breaker()

                    if self.cloudsql_healthy:
                        logger.info("✅ CloudSQL reconnected - warming cache and syncing")

                        # CRITICAL: Warm voice profile cache on reconnection
                        asyncio.create_task(self._warm_cache_on_reconnection())

                        # Trigger immediate sync of pending changes
                        asyncio.create_task(self._reconcile_pending_syncs())

                # Health check ping - use connection_manager (not cloudsql_pool which doesn't exist)
                elif self.connection_manager is not None:
                    try:
                        # Use the connection manager to verify CloudSQL connectivity
                        pool = await self.connection_manager.get_pool()
                        if pool:
                            async with pool.acquire() as conn:
                                await conn.fetchval("SELECT 1")
                            self.last_health_check = datetime.now()
                    except Exception as e:
                        logger.warning(f"⚠️  CloudSQL health check failed: {e}")
                        self.cloudsql_healthy = False
                        self.metrics.cloudsql_available = False

            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def _sync_loop(self):
        """Background sync loop with circuit breaker and priority processing"""
        while not self._shutdown:
            try:
                # Check circuit breaker before attempting sync
                if not self.circuit_breaker.can_attempt():
                    logger.debug("⏸️  Circuit open, skipping sync")
                    await asyncio.sleep(self.sync_interval)
                    continue

                # Check if we have pending syncs
                if self.sync_queue.size() == 0:
                    await asyncio.sleep(self.sync_interval)
                    continue

                # Check connection load before sync
                load = 0.0
                if self.connection_manager and self.connection_manager.is_initialized:
                    stats = self.connection_manager.get_stats()
                    pool_size = stats.get("pool_size", 0)
                    max_size = stats.get("max_size", 1)
                    load = pool_size / max_size if max_size > 0 else 0.0

                # Backpressure control: defer low-priority syncs if load > 80%
                if load > 0.8:
                    logger.warning(f"🔥 High load ({load:.1%}) - deferring low-priority syncs")
                    # Only process CRITICAL and HIGH priority
                    await self._process_priority_sync_queue(max_priority=SyncPriority.HIGH)
                else:
                    # Normal operation: process all priorities
                    await self._process_sync_queue()

                await asyncio.sleep(self.sync_interval)

            except Exception as e:
                logger.error(f"Sync loop error: {e}")
                self.circuit_breaker.record_failure()
                await asyncio.sleep(self.sync_interval)

    async def _process_sync_queue(self):
        """Process queued sync operations in priority-based batches"""
        batch: List[SyncRecord] = []

        try:
            # Collect batch from priority queue (highest priority first)
            for _ in range(self.batch_size):
                sync_record = await self.sync_queue.get(timeout=0.1)
                if sync_record:
                    batch.append(sync_record)
                else:
                    break

            if not batch:
                return

            logger.debug(f"🔄 Processing sync batch: {len(batch)} records (priorities: {[r.priority.name for r in batch[:3]]}...)")

            # Group by table and operation for efficient batching
            grouped = defaultdict(list)
            for record in batch:
                key = (record.table_name, record.operation)
                grouped[key].append(record)

            # Process each group
            for (table, operation), records in grouped.items():
                await self._sync_batch_to_cloudsql(table, operation, records)

            self.metrics.last_sync_time = datetime.now()

        except Exception as e:
            logger.error(f"Batch sync failed: {e}")
            self.circuit_breaker.record_failure()
            # Re-queue failed records
            for record in batch:
                await self.sync_queue.put(record)

    async def _process_priority_sync_queue(self, max_priority: SyncPriority):
        """Process only high-priority sync operations (backpressure control)"""
        batch: List[SyncRecord] = []

        try:
            # Collect only records up to max_priority
            for _ in range(self.batch_size):
                sync_record = await self.sync_queue.get(timeout=0.1)
                if sync_record:
                    if sync_record.priority.value <= max_priority.value:
                        batch.append(sync_record)
                    else:
                        # Defer low-priority records
                        await self.sync_queue.put(sync_record)
                        self.metrics.total_deferred += 1
                else:
                    break

            if not batch:
                return

            logger.info(f"⚡ Priority sync: {len(batch)} records (max priority: {max_priority.name})")

            # Group and process
            grouped = defaultdict(list)
            for record in batch:
                key = (record.table_name, record.operation)
                grouped[key].append(record)

            for (table, operation), records in grouped.items():
                await self._sync_batch_to_cloudsql(table, operation, records)

            self.metrics.last_sync_time = datetime.now()

        except Exception as e:
            logger.error(f"Priority sync failed: {e}")
            self.circuit_breaker.record_failure()
            for record in batch:
                await self.sync_queue.put(record)

    async def _sync_batch_to_cloudsql(self, table: str, operation: str, records: List[SyncRecord]):
        """Sync a batch of records to CloudSQL via singleton connection manager"""
        # Check circuit breaker before attempting
        if not self.circuit_breaker.can_attempt():
            logger.debug("⏸️  Circuit open, deferring sync")
            for record in records:
                await self.sync_queue.put(record)
            return

        if not self.connection_manager or not self.connection_manager.is_initialized:
            logger.warning("⚠️  Connection manager not initialized")
            self.circuit_breaker.record_failure()
            return

        try:
            start_time = time.time()

            # Acquire connection via singleton manager (automatic context management)
            async with self.connection_manager.connection() as conn:
                async with conn.transaction():
                    for record in records:
                        # Fetch data from SQLite
                        async with self.sqlite_conn.execute(
                            f"SELECT * FROM {table} WHERE speaker_id = ?",
                            (record.record_id,)
                        ) as cursor:
                            row = await cursor.fetchone()

                            if not row:
                                logger.warning(f"Record {record.record_id} not found in SQLite")
                                continue

                            # Extract fields based on operation
                            if operation == "insert" and table == "speaker_profiles":
                                # Insert or update speaker profile in CloudSQL
                                await conn.execute("""
                                    INSERT INTO speaker_profiles
                                    (speaker_id, speaker_name, voiceprint_embedding, acoustic_features, total_samples, last_updated)
                                    VALUES ($1, $2, $3, $4, $5, $6)
                                    ON CONFLICT (speaker_id)
                                    DO UPDATE SET
                                        speaker_name = EXCLUDED.speaker_name,
                                        voiceprint_embedding = EXCLUDED.voiceprint_embedding,
                                        acoustic_features = EXCLUDED.acoustic_features,
                                        total_samples = EXCLUDED.total_samples,
                                        last_updated = EXCLUDED.last_updated
                                """, row[0], row[1], row[2], row[3], row[4] if len(row) > 4 else 0, row[5] if len(row) > 5 else datetime.now())

                            elif operation == "update" and table == "speaker_profiles":
                                # Update existing profile
                                await conn.execute("""
                                    UPDATE speaker_profiles
                                    SET speaker_name = $2,
                                        voiceprint_embedding = $3,
                                        acoustic_features = $4,
                                        total_samples = $5,
                                        last_updated = $6
                                    WHERE speaker_id = $1
                                """, row[0], row[1], row[2], row[3], row[4] if len(row) > 4 else 0, row[5] if len(row) > 5 else datetime.now())

                            elif operation == "delete":
                                # Delete record
                                await conn.execute(
                                    f"DELETE FROM {table} WHERE speaker_id = $1",
                                    record.record_id
                                )

                        # Update sync log in SQLite
                        await self.sqlite_conn.execute(
                            """UPDATE _sync_log SET status = ?, retry_count = 0, last_error = NULL
                               WHERE record_id = ? AND operation = ?""",
                            (SyncStatus.SYNCED.value, record.record_id, operation)
                        )
                        await self.sqlite_conn.commit()

            latency = (time.time() - start_time) * 1000
            self.metrics.cloud_write_latency_ms = latency
            self.metrics.total_synced += len(records)

            # Record success in circuit breaker
            self.circuit_breaker.record_success()
            self.metrics.cloudsql_available = True
            self.metrics.circuit_state = self.circuit_breaker.get_state().value

            logger.info(f"✅ Synced {len(records)} {operation} to {table} ({latency:.1f}ms)")

        except Exception as e:
            logger.error(f"Failed to sync batch to CloudSQL: {e}")
            self.metrics.total_failed += len(records)

            # Record failure in circuit breaker
            self.circuit_breaker.record_failure()
            self.metrics.cloudsql_available = False
            self.metrics.circuit_state = self.circuit_breaker.get_state().value

            # Re-queue with exponential backoff
            for record in records:
                record.retry_count += 1
                record.last_error = str(e)
                record.status = SyncStatus.FAILED

                if record.retry_count < self.max_retry_attempts:
                    # Exponential backoff delay
                    delay = min(2 ** record.retry_count, 60)  # Max 60 seconds
                    await asyncio.sleep(delay)
                    await self.sync_queue.put(record)

                    # Update sync log
                    await self.sqlite_conn.execute(
                        """UPDATE _sync_log SET status = ?, retry_count = ?, last_error = ?
                           WHERE record_id = ? AND operation = ?""",
                        (SyncStatus.FAILED.value, record.retry_count, str(e)[:500], record.record_id, operation)
                    )
                    await self.sqlite_conn.commit()
                else:
                    logger.error(f"❌ Max retries exceeded for {record.record_id}")
                    # Mark as permanently failed
                    await self.sqlite_conn.execute(
                        """UPDATE _sync_log SET status = 'failed_permanent', last_error = ?
                           WHERE record_id = ? AND operation = ?""",
                        (f"Max retries exceeded: {str(e)}"[:500], record.record_id, operation)
                    )
                    await self.sqlite_conn.commit()

            # Connection automatically released by context manager

    async def _metrics_loop(self):
        """Background metrics collection and monitoring"""
        while not self._shutdown:
            try:
                await asyncio.sleep(10)  # Update metrics every 10 seconds

                # Update uptime
                self.metrics.uptime_seconds = time.time() - self._start_time

                # Update queue sizes
                self.metrics.sync_queue_size = self.sync_queue.size()
                self.metrics.priority_queue_sizes = self.sync_queue.sizes_by_priority()

                # Update connection pool load from singleton manager
                if self.connection_manager and self.connection_manager.is_initialized:
                    stats = self.connection_manager.get_stats()
                    pool_size = stats.get("pool_size", 0)
                    max_size = stats.get("max_size", 1)
                    self.metrics.connection_pool_load = pool_size / max_size if max_size > 0 else 0.0

                    # Phase 2: Update Prometheus metrics
                    if self.prometheus:
                        self.prometheus.update_connection_pool(pool_size, self.metrics.connection_pool_load)
                        self.prometheus.update_queue_size(self.metrics.sync_queue_size)
                        self.prometheus.update_circuit_state(self.circuit_breaker.get_state())

                # Update circuit state
                self.metrics.circuit_state = self.circuit_breaker.get_state().value

                # Phase 2: Push metrics to Redis
                if self.redis:
                    await self.redis.set_metric("uptime_seconds", self.metrics.uptime_seconds)
                    await self.redis.set_metric("queue_size", self.metrics.sync_queue_size)
                    await self.redis.set_metric("connection_pool_load", self.metrics.connection_pool_load)
                    await self.redis.set_metric("circuit_state", self.metrics.circuit_state)
                    await self.redis.set_metric("cache_size", self.metrics.cache_size)
                    await self.redis.push_to_timeseries("uptime", self.metrics.uptime_seconds)

                # Log summary if interesting
                if self.metrics.sync_queue_size > 50 or self.metrics.connection_pool_load > 0.7:
                    logger.info(
                        f"📊 Metrics: queue={self.metrics.sync_queue_size}, "
                        f"load={self.metrics.connection_pool_load:.1%}, "
                        f"circuit={self.metrics.circuit_state}, "
                        f"cache_hits={self.metrics.cache_hits}, "
                        f"cache_misses={self.metrics.cache_misses}"
                    )

            except Exception as e:
                logger.error(f"Metrics loop error: {e}")

    async def _ml_prefetch_loop(self):
        """Background ML-based predictive cache warming"""
        while not self._shutdown:
            try:
                await asyncio.sleep(60)  # Run prefetch every 60 seconds

                if not self.ml_prefetcher or not self.sqlite_conn:
                    continue

                # Get ML predictions and prefetch
                await self.ml_prefetcher.prefetch_predicted(self.sqlite_conn)

                # Get and log statistics
                stats = self.ml_prefetcher.get_statistics()
                logger.debug(
                    f"🧠 ML Prefetcher: {stats['unique_speakers']} speakers, "
                    f"{stats['active_patterns']} active patterns, "
                    f"{stats['total_accesses']} total accesses"
                )

                # Phase 2: Push ML stats to Redis
                if self.redis:
                    await self.redis.set_metric("ml_prefetch_stats", stats)

            except Exception as e:
                logger.error(f"ML prefetch loop error: {e}")

    async def _reconcile_pending_syncs(self):
        """Reconcile pending syncs after CloudSQL reconnection"""
        logger.info("🔄 Starting sync reconciliation...")

        try:
            # Load pending syncs from SQLite sync log
            async with self.sqlite_conn.execute(
                "SELECT * FROM _sync_log WHERE status IN ('pending', 'failed') ORDER BY timestamp"
            ) as cursor:
                async for row in cursor:
                    sync_record = SyncRecord(
                        record_id=row[2],
                        table_name=row[1],
                        operation=row[3],
                        timestamp=datetime.fromisoformat(row[4]),
                        source_db=DatabaseType.SQLITE,
                        target_db=DatabaseType.CLOUDSQL,
                        status=SyncStatus(row[5]),
                        retry_count=row[6],
                        last_error=row[7],
                        data_hash=row[8]
                    )
                    await self.sync_queue.put(sync_record)

            logger.info(f"✅ Queued {self.sync_queue.size()} pending syncs")

        except Exception as e:
            logger.error(f"Reconciliation failed: {e}")

    @asynccontextmanager
    async def transaction(self):
        """
        Context manager for transactional operations across both databases.
        Writes to SQLite immediately, queues CloudSQL sync.
        """
        sqlite_transaction = await self.sqlite_conn.execute("BEGIN")

        try:
            yield self

            await self.sqlite_conn.commit()

        except Exception as e:
            await self.sqlite_conn.rollback()
            raise e

    async def write_voice_profile(
        self,
        speaker_id: int,
        speaker_name: str,
        embedding: np.ndarray,
        acoustic_features: Dict[str, float],
        priority: SyncPriority = SyncPriority.HIGH,
        **kwargs
    ) -> bool:
        """
        Write-behind profile persistence with priority queue and FAISS cache update.

        Write flow:
        1. Write to SQLite immediately (local persistence)
        2. Update FAISS cache for instant reads
        3. Queue CloudSQL sync with priority (write-behind)

        Args:
            speaker_id: Speaker ID
            speaker_name: Speaker name
            embedding: Voice embedding array
            acoustic_features: Acoustic feature dict
            priority: Sync priority (default: HIGH for user-facing writes)
            **kwargs: Additional profile fields

        Returns:
            True if successfully written to at least one database
        """
        start_time = time.time()

        try:
            # 1. Write to SQLite IMMEDIATELY (primary persistence)
            await self._write_to_sqlite(speaker_id, speaker_name, embedding, acoustic_features, **kwargs)

            local_latency = (time.time() - start_time) * 1000
            self.metrics.local_read_latency_ms = local_latency

            # 2. Update FAISS cache for instant reads (if available)
            if self.faiss_cache:
                metadata = {
                    "acoustic_features": acoustic_features,
                    "total_samples": kwargs.get('total_samples', 0),
                    "last_updated": datetime.now().isoformat()
                }
                self.faiss_cache.add_embedding(speaker_name, embedding, metadata)
                self.metrics.cache_size = self.faiss_cache.size()
                logger.debug(f"⚡ FAISS cache updated: {speaker_name}")

            # 3. Queue CloudSQL sync (write-behind, async, priority-based)
            data_hash = self._compute_hash(embedding, acoustic_features)
            size_bytes = embedding.nbytes + len(json.dumps(acoustic_features))

            sync_record = SyncRecord(
                record_id=str(speaker_id),
                table_name="speaker_profiles",
                operation="insert",
                timestamp=datetime.now(),
                source_db=DatabaseType.SQLITE,
                target_db=DatabaseType.CLOUDSQL,
                status=SyncStatus.PENDING,
                priority=priority,
                data_hash=data_hash,
                size_bytes=size_bytes
            )

            await self.sync_queue.put(sync_record, priority=priority)

            # Log to sync table
            await self.sqlite_conn.execute(
                """INSERT INTO _sync_log (sync_id, table_name, record_id, operation, timestamp, status, data_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"{speaker_id}_{int(time.time())}",
                    "speaker_profiles",
                    str(speaker_id),
                    "insert",
                    datetime.now().isoformat(),
                    SyncStatus.PENDING.value,
                    data_hash
                )
            )
            await self.sqlite_conn.commit()

            logger.info(f"✅ Write-behind complete: {speaker_name} (local: {local_latency:.1f}ms, priority: {priority.name})")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to write voice profile: {e}")
            return False

    async def _write_to_sqlite(
        self,
        speaker_id: int,
        speaker_name: str,
        embedding: np.ndarray,
        acoustic_features: Dict[str, float],
        **kwargs
    ):
        """Write voice profile to SQLite"""
        # Create table if not exists
        await self.sqlite_conn.execute("""
            CREATE TABLE IF NOT EXISTS speaker_profiles (
                speaker_id INTEGER PRIMARY KEY,
                speaker_name TEXT UNIQUE NOT NULL,
                voiceprint_embedding BLOB NOT NULL,
                acoustic_features TEXT NOT NULL,
                total_samples INTEGER DEFAULT 0,
                last_updated TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.sqlite_conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_speaker_name
            ON speaker_profiles(speaker_name)
        """)
        await self.sqlite_conn.commit()

        embedding_bytes = embedding.tobytes()
        features_json = json.dumps(acoustic_features)
        total_samples = kwargs.get('total_samples', 0)

        await self.sqlite_conn.execute(
            """INSERT OR REPLACE INTO speaker_profiles
               (speaker_id, speaker_name, voiceprint_embedding, acoustic_features, total_samples, last_updated)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (speaker_id, speaker_name, embedding_bytes, features_json, total_samples, datetime.now().isoformat())
        )
        await self.sqlite_conn.commit()

    async def read_voice_profile(self, speaker_name: str) -> Optional[Dict[str, Any]]:
        """
        Cache-first read with sub-millisecond latency (ZERO CloudSQL queries).

        Read priority:
        1. FAISS cache (if available) - <1ms
        2. SQLite - <5ms
        3. Never queries CloudSQL

        Args:
            speaker_name: Speaker name to query

        Returns:
            Voice profile dict or None
        """
        start_time = time.time()

        try:
            # PRIORITY 1: Check FAISS cache first (sub-millisecond)
            if self.faiss_cache:
                cached_data = self.faiss_cache.get_by_name(speaker_name)
                if cached_data:
                    latency = (time.time() - start_time) * 1000
                    latency_seconds = latency / 1000.0
                    self.metrics.cache_hit_latency_ms = latency
                    self.metrics.cache_hits += 1

                    # Phase 2: Record Prometheus metrics
                    if self.prometheus:
                        self.prometheus.record_cache_hit()
                        self.prometheus.observe_read_latency(latency_seconds, source="faiss")
                        self.prometheus.record_query("cache_hit")

                    # Phase 2: Record Redis metrics
                    if self.redis:
                        await self.redis.increment("cache_hits")
                        await self.redis.push_to_timeseries("read_latency_faiss", latency_seconds)

                    # Phase 2: Record ML access pattern
                    if self.ml_prefetcher:
                        self.ml_prefetcher.record_access(speaker_name)

                    logger.debug(f"⚡ Cache hit: {speaker_name} in {latency:.3f}ms")

                    # Reconstruct full profile from cache
                    return {
                        "speaker_name": speaker_name,
                        "name": speaker_name,  # Compatibility
                        "embedding": cached_data.get("embedding"),
                        "voiceprint_embedding": cached_data.get("embedding"),
                        "acoustic_features": cached_data.get("acoustic_features", {}),
                        "total_samples": cached_data.get("total_samples", 0),
                        "last_updated": cached_data.get("last_updated"),
                        "created_at": cached_data.get("created_at")
                    }
                else:
                    self.metrics.cache_misses += 1

                    # Phase 2: Record metrics
                    if self.prometheus:
                        self.prometheus.record_cache_miss()

                    if self.redis:
                        await self.redis.increment("cache_misses")

            # PRIORITY 2: Fallback to SQLite (still fast, <5ms)
            async with self.sqlite_conn.execute(
                "SELECT * FROM speaker_profiles WHERE speaker_name = ?",
                (speaker_name,)
            ) as cursor:
                row = await cursor.fetchone()

                if row:
                    latency = (time.time() - start_time) * 1000
                    latency_seconds = latency / 1000.0
                    self.metrics.local_read_latency_ms = latency

                    # Phase 2: Record Prometheus metrics
                    if self.prometheus:
                        self.prometheus.observe_read_latency(latency_seconds, source="sqlite")
                        self.prometheus.record_query("sqlite_read")

                    # Phase 2: Record Redis metrics
                    if self.redis:
                        await self.redis.push_to_timeseries("read_latency_sqlite", latency_seconds)

                    # Phase 2: Record ML access pattern
                    if self.ml_prefetcher:
                        self.ml_prefetcher.record_access(speaker_name)

                    logger.debug(f"✅ SQLite read: {speaker_name} in {latency:.2f}ms")

                    profile = self._parse_profile_row(row)

                    # Opportunistically update FAISS cache
                    if self.faiss_cache and profile.get("embedding") is not None:
                        embedding = profile["embedding"]
                        if not isinstance(embedding, np.ndarray):
                            embedding = np.array(embedding, dtype=np.float32)

                        self.faiss_cache.add_embedding(
                            speaker_name,
                            embedding,
                            {
                                "acoustic_features": profile.get("acoustic_features", {}),
                                "total_samples": profile.get("total_samples", 0),
                                "last_updated": profile.get("last_updated"),
                                "created_at": profile.get("created_at")
                            }
                        )
                        self.metrics.cache_size = self.faiss_cache.size()

                        # Phase 2: Update Prometheus cache size
                        if self.prometheus:
                            self.prometheus.update_cache_size(self.faiss_cache.size())

                    return profile

                return None

        except Exception as e:
            logger.error(f"Failed to read profile: {e}")
            return None

    async def find_owner_profile(self, hint_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Dynamically find the owner's voice profile using multiple fallback strategies.

        This method implements robust, fuzzy name matching to find the owner profile
        even when names don't match exactly (e.g., "Derek" vs "Derek J. Russell").

        Resolution order:
        1. Exact match on hint_name (if provided)
        2. Read from enrollment config file (~/.jarvis/voice_enrollment.json)
        3. Case-insensitive exact match
        4. Partial/prefix match (first name only)
        5. LIKE query for names containing the hint
        6. First profile with most samples (fallback for single-user systems)

        Args:
            hint_name: Optional name hint (e.g., from VBI_OWNER_NAME env var)

        Returns:
            Voice profile dict with embedding, or None if not found
        """
        start_time = time.time()
        search_names = []

        # Strategy 1: Use hint_name if provided
        if hint_name:
            search_names.append(hint_name)

        # Strategy 2: Read from enrollment config
        try:
            enrollment_path = os.path.expanduser('~/.jarvis/voice_enrollment.json')
            if os.path.exists(enrollment_path):
                with open(enrollment_path, 'r') as f:
                    enrollment = json.load(f)
                    config_name = enrollment.get('owner_name')
                    if config_name and config_name not in search_names:
                        search_names.append(config_name)
                        logger.debug(f"📄 Found owner name in enrollment config: {config_name}")
        except Exception as e:
            logger.debug(f"Could not read enrollment config: {e}")

        # Strategy 3: Try environment variable
        env_name = os.getenv('VBI_OWNER_NAME')
        if env_name and env_name not in search_names:
            search_names.append(env_name)

        # Dynamic fallback: If no hints available, use advanced heuristic-based discovery
        if not search_names:
            logger.info("🔍 No name hints available - using advanced dynamic owner discovery")
            # Use the multi-heuristic discovery method which scores candidates based on:
            # - Sample count (60% weight)
            # - Recency (20% weight)
            # - Embedding quality (20% weight)
            discovered = await self.discover_primary_owner()
            if discovered:
                latency = (time.time() - start_time) * 1000
                logger.info(f"✅ Found owner via heuristic discovery: '{discovered['name']}' in {latency:.2f}ms")
                return discovered
            else:
                logger.warning("❌ No profiles found via heuristic discovery")
                return None

        # Try each name with exact match first (only if we have hints)
        for name in search_names:
            profile = await self.read_voice_profile(name)
            if profile and profile.get('embedding') is not None:
                latency = (time.time() - start_time) * 1000
                logger.info(f"✅ Found owner profile '{name}' (exact match) in {latency:.2f}ms")
                return profile

        # Strategy 4-6: Fuzzy matching via SQLite queries
        # Use explicit column selection to avoid schema mismatch issues
        try:
            for name in search_names:
                # Case-insensitive match
                async with self.sqlite_conn.execute(
                    f"SELECT {self.PROFILE_COLUMNS} FROM speaker_profiles WHERE LOWER(speaker_name) = LOWER(?)",
                    (name,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        profile = self._parse_profile_row(row)
                        if profile.get('embedding') is not None:
                            latency = (time.time() - start_time) * 1000
                            logger.info(f"✅ Found owner profile '{profile['name']}' (case-insensitive) in {latency:.2f}ms")
                            return profile

                # Partial/prefix match - name starts with hint (e.g., "Derek" matches "Derek J. Russell")
                async with self.sqlite_conn.execute(
                    f"SELECT {self.PROFILE_COLUMNS} FROM speaker_profiles WHERE speaker_name LIKE ? || '%'",
                    (name,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        profile = self._parse_profile_row(row)
                        if profile.get('embedding') is not None:
                            latency = (time.time() - start_time) * 1000
                            logger.info(f"✅ Found owner profile '{profile['name']}' (prefix match from '{name}') in {latency:.2f}ms")
                            return profile

                # LIKE query - name contains hint anywhere
                async with self.sqlite_conn.execute(
                    f"SELECT {self.PROFILE_COLUMNS} FROM speaker_profiles WHERE speaker_name LIKE '%' || ? || '%'",
                    (name,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        profile = self._parse_profile_row(row)
                        if profile.get('embedding') is not None:
                            latency = (time.time() - start_time) * 1000
                            logger.info(f"✅ Found owner profile '{profile['name']}' (contains '{name}') in {latency:.2f}ms")
                            return profile

            # Strategy 6: Fallback - get profile with most samples (single-user systems)
            async with self.sqlite_conn.execute(
                f"SELECT {self.PROFILE_COLUMNS} FROM speaker_profiles ORDER BY total_samples DESC LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    profile = self._parse_profile_row(row)
                    if profile.get('embedding') is not None:
                        latency = (time.time() - start_time) * 1000
                        logger.info(f"✅ Found owner profile '{profile['name']}' (fallback: most samples) in {latency:.2f}ms")
                        return profile

        except Exception as e:
            logger.error(f"Fuzzy profile search failed: {e}")
            import traceback
            logger.debug(f"Fuzzy search traceback: {traceback.format_exc()}")

        latency = (time.time() - start_time) * 1000
        logger.warning(f"❌ No owner profile found after {latency:.2f}ms (tried: {search_names})")
        return None

    async def list_all_profiles(self) -> List[Dict[str, Any]]:
        """
        List all speaker profiles in the local database.

        Returns:
            List of profile dicts with basic info (no embeddings for efficiency)
        """
        profiles = []
        try:
            async with self.sqlite_conn.execute(
                "SELECT speaker_id, speaker_name, total_samples, last_updated FROM speaker_profiles ORDER BY total_samples DESC"
            ) as cursor:
                async for row in cursor:
                    profiles.append({
                        'speaker_id': row[0],
                        'speaker_name': row[1],
                        'total_samples': row[2],
                        'last_updated': row[3]
                    })
        except Exception as e:
            logger.error(f"Failed to list profiles: {e}")
        return profiles

    async def discover_primary_owner(self) -> Optional[Dict[str, Any]]:
        """
        Dynamically discover the primary owner using multiple heuristics.

        This method uses a scoring system to identify the most likely primary owner
        without any hardcoded names. It's designed for single-user or primary-owner systems.

        Heuristics (in order of weight):
        1. Most voice samples (60% weight) - indicates primary user
        2. Most recently updated (20% weight) - indicates active user
        3. Embedding quality (20% weight) - well-normalized embeddings indicate proper enrollment

        Returns:
            Best candidate profile dict, or None if database is empty
        """
        start_time = time.time()

        try:
            # Get all profiles with full data
            candidates = []
            async with self.sqlite_conn.execute(
                """SELECT speaker_id, speaker_name, voiceprint_embedding, acoustic_features,
                          total_samples, last_updated, created_at
                   FROM speaker_profiles
                   WHERE voiceprint_embedding IS NOT NULL"""
            ) as cursor:
                async for row in cursor:
                    profile = self._parse_profile_row(row)
                    if profile.get('embedding') is not None:
                        candidates.append(profile)

            if not candidates:
                logger.warning("🔍 No voice profiles found in database for primary owner discovery")
                return None

            if len(candidates) == 1:
                # Single user system - return the only profile
                latency = (time.time() - start_time) * 1000
                logger.info(f"✅ Primary owner discovered: '{candidates[0]['name']}' (single profile) in {latency:.2f}ms")
                return candidates[0]

            # Score each candidate
            scored_candidates = []

            # Get normalization factors
            max_samples = max(c.get('total_samples', 0) for c in candidates)

            for candidate in candidates:
                score = 0.0

                # Heuristic 1: Sample count (60% weight)
                samples = candidate.get('total_samples', 0)
                if max_samples > 0:
                    sample_score = (samples / max_samples) * 0.6
                    score += sample_score

                # Heuristic 2: Recency (20% weight) - more recent = higher score
                last_updated = candidate.get('last_updated')
                if last_updated:
                    try:
                        from datetime import datetime
                        if isinstance(last_updated, str):
                            updated_dt = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                        else:
                            updated_dt = last_updated
                        # Score based on days since update (max 30 days for full score)
                        days_old = (datetime.now(updated_dt.tzinfo if updated_dt.tzinfo else None) - updated_dt).days
                        recency_score = max(0, (30 - min(days_old, 30)) / 30) * 0.2
                        score += recency_score
                    except Exception:
                        pass  # Skip recency scoring if date parsing fails

                # Heuristic 3: Embedding quality (20% weight) - norm close to 1.0 is ideal
                embedding = candidate.get('embedding')
                if embedding is not None:
                    norm = np.linalg.norm(embedding)
                    # Ideal norm is 1.0 for normalized embeddings
                    quality_score = max(0, 1 - abs(1.0 - norm)) * 0.2
                    score += quality_score

                scored_candidates.append((score, candidate))
                logger.debug(f"📊 Candidate '{candidate['name']}': score={score:.4f} (samples={samples})")

            # Sort by score descending
            scored_candidates.sort(key=lambda x: x[0], reverse=True)

            best_candidate = scored_candidates[0][1]
            best_score = scored_candidates[0][0]

            latency = (time.time() - start_time) * 1000
            logger.info(
                f"✅ Primary owner discovered: '{best_candidate['name']}' "
                f"(score={best_score:.4f}, samples={best_candidate.get('total_samples', 0)}) "
                f"in {latency:.2f}ms"
            )

            return best_candidate

        except Exception as e:
            logger.error(f"❌ Primary owner discovery failed: {e}")
            return None

    # SQL query column list for explicit selection (avoids SELECT * schema mismatch)
    PROFILE_COLUMNS = """speaker_id, speaker_name, voiceprint_embedding,
                         total_samples, created_at, last_updated, acoustic_features"""

    def _parse_profile_row(self, row: tuple) -> Dict[str, Any]:
        """
        Parse SQLite row into profile dict.

        Expected column order (from PROFILE_COLUMNS):
        0: speaker_id (INTEGER)
        1: speaker_name (TEXT)
        2: voiceprint_embedding (BLOB)
        3: total_samples (INTEGER)
        4: created_at (TIMESTAMP)
        5: last_updated (TIMESTAMP)
        6: acoustic_features (TEXT/JSON) - optional
        """
        try:
            # Parse embedding from BLOB
            embedding = None
            if len(row) > 2 and row[2]:
                try:
                    embedding = np.frombuffer(row[2], dtype=np.float32)
                except Exception as e:
                    logger.warning(f"Failed to parse embedding: {e}")

            # Parse acoustic_features safely - handle various types
            acoustic_features = {}
            if len(row) > 6 and row[6]:
                af_value = row[6]
                if isinstance(af_value, str):
                    try:
                        acoustic_features = json.loads(af_value)
                    except json.JSONDecodeError:
                        acoustic_features = {}
                elif isinstance(af_value, dict):
                    acoustic_features = af_value
                # Skip if it's an int or other non-JSON type

            return {
                "speaker_id": row[0] if len(row) > 0 else None,
                "speaker_name": row[1] if len(row) > 1 else None,
                "name": row[1] if len(row) > 1 else None,  # Compatibility alias
                "embedding": embedding,
                "voiceprint_embedding": embedding,  # Compatibility alias
                "total_samples": row[3] if len(row) > 3 else 0,
                "created_at": row[4] if len(row) > 4 else None,
                "last_updated": row[5] if len(row) > 5 else None,
                "acoustic_features": acoustic_features,
            }
        except Exception as e:
            logger.error(f"Error parsing profile row: {e}, row length: {len(row)}")
            # Return minimal valid profile to avoid total failure
            return {
                "speaker_id": row[0] if len(row) > 0 else None,
                "speaker_name": row[1] if len(row) > 1 else "Unknown",
                "name": row[1] if len(row) > 1 else "Unknown",
                "embedding": None,
                "voiceprint_embedding": None,
                "total_samples": 0,
                "acoustic_features": {},
            }

    def _compute_hash(self, embedding: np.ndarray, features: Dict[str, Any]) -> str:
        """Compute hash of data for conflict detection"""
        data = f"{embedding.tobytes().hex()}_{json.dumps(features, sort_keys=True)}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def get_metrics(self) -> SyncMetrics:
        """Get current sync metrics"""
        self.metrics.sync_queue_size = self.sync_queue.size()
        return self.metrics

    async def shutdown(self):
        """Graceful shutdown with connection cleanup"""
        logger.info("🛑 Shutting down advanced hybrid sync V2.0...")
        self._shutdown = True

        # Cancel background tasks (including Phase 2 prefetch task)
        tasks_to_cancel = [self.sync_task, self.health_check_task, self.metrics_task, self.prefetch_task]
        for task in tasks_to_cancel:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Flush pending high-priority syncs only (don't wait for all)
        if self.circuit_breaker.can_attempt() and self.sync_queue.size() > 0:
            logger.info("🔄 Flushing high-priority syncs...")
            try:
                await asyncio.wait_for(
                    self._process_priority_sync_queue(max_priority=SyncPriority.HIGH),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.warning("⏱️  Sync flush timed out")

        # Close connections
        if self.sqlite_conn:
            await self.sqlite_conn.close()

        # Singleton connection manager handles its own shutdown via signal handlers
        # No need to manually close it here

        # Phase 2: Close Redis connection
        if self.redis:
            await self.redis.close()

        # Shutdown thread pool
        if self.thread_pool:
            self.thread_pool.shutdown(wait=False)

        logger.info("✅ Advanced hybrid sync V2.0 shutdown complete")
        logger.info(f"   📊 Final stats:")
        logger.info(f"      Synced: {self.metrics.total_synced}")
        logger.info(f"      Failed: {self.metrics.total_failed}")
        logger.info(f"      Cache Hits: {self.metrics.cache_hits}")
        logger.info(f"      Cache Misses: {self.metrics.cache_misses}")
        logger.info(f"      Cache Hit Rate: {self.metrics.cache_hits / max(1, self.metrics.cache_hits + self.metrics.cache_misses) * 100:.1f}%")
        logger.info(f"      Uptime: {self.metrics.uptime_seconds:.1f}s")

        # Phase 2: Log ML prefetcher stats
        if self.ml_prefetcher:
            stats = self.ml_prefetcher.get_statistics()
            logger.info(f"      ML Patterns: {stats['active_patterns']} active")
            logger.info(f"      ML Accesses: {stats['total_accesses']} recorded")
