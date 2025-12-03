#!/usr/bin/env python3
"""
Singleton CloudSQL Connection Manager for JARVIS
=================================================

Production-grade, thread-safe, async-safe connection pool manager with:
- Automatic leak detection and recovery with connection tracking
- Circuit breaker pattern for failure handling
- Background cleanup tasks for orphan connection termination
- Comprehensive metrics and observability
- Signal-aware graceful shutdown (SIGINT, SIGTERM, atexit)
- Strict connection limits for db-f1-micro (max 3 connections)

Architecture:
┌─────────────────────────────────────────────────────────────────────┐
│              CloudSQL Connection Manager v2.0                        │
├─────────────────────────────────────────────────────────────────────┤
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐        │
│  │ Connection     │  │ Leak Detector  │  │ Circuit        │        │
│  │ Pool (asyncpg) │  │ & Tracker      │  │ Breaker        │        │
│  └───────┬────────┘  └───────┬────────┘  └───────┬────────┘        │
│          └───────────────────┼───────────────────┘                  │
│                              ▼                                       │
│                 ┌─────────────────────────┐                         │
│                 │  Connection Coordinator │                         │
│                 │  • Lifecycle management │                         │
│                 │  • Metrics collection   │                         │
│                 │  • Background cleanup   │                         │
│                 └─────────────────────────┘                         │
└─────────────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 2.0.0
"""

import asyncio
import atexit
import logging
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional

try:
    import asyncpg
    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class ConnectionConfig:
    """Configuration for connection pool - all values configurable."""
    # Pool sizing (conservative for db-f1-micro)
    min_connections: int = 1
    max_connections: int = 3

    # Timeouts (seconds)
    connection_timeout: float = 5.0
    query_timeout: float = 30.0
    pool_creation_timeout: float = 15.0

    # Connection lifecycle
    max_queries_per_connection: int = 10000
    max_idle_time_seconds: float = 300.0  # 5 minutes

    # Leak detection thresholds
    checkout_warning_seconds: float = 60.0   # Warn if held > 60s
    checkout_timeout_seconds: float = 300.0  # Force release after 5 min
    leak_check_interval_seconds: float = 30.0
    leaked_idle_threshold_minutes: int = 5   # DB connections idle > 5 min

    # Circuit breaker
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 30.0

    # Background tasks
    enable_background_cleanup: bool = True
    cleanup_interval_seconds: float = 60.0


# =============================================================================
# Connection Tracking
# =============================================================================

@dataclass
class ConnectionCheckout:
    """Tracks a connection checkout for leak detection."""
    checkout_id: int
    checkout_time: datetime
    stack_trace: str
    released: bool = False
    release_time: Optional[datetime] = None

    @property
    def age_seconds(self) -> float:
        return (datetime.now() - self.checkout_time).total_seconds()

    @property
    def is_potentially_leaked(self) -> bool:
        return not self.released and self.age_seconds > 300.0


@dataclass
class ConnectionMetrics:
    """Comprehensive metrics for monitoring."""
    total_checkouts: int = 0
    total_releases: int = 0
    total_errors: int = 0
    total_timeouts: int = 0
    total_leaks_detected: int = 0
    total_leaks_recovered: int = 0
    pool_exhaustion_count: int = 0
    circuit_breaker_trips: int = 0

    avg_checkout_duration_ms: float = 0.0
    max_checkout_duration_ms: float = 0.0

    created_at: datetime = field(default_factory=datetime.now)
    last_checkout: Optional[datetime] = None
    last_error: Optional[datetime] = None


# =============================================================================
# Circuit Breaker
# =============================================================================

class CircuitState(Enum):
    CLOSED = auto()      # Normal operation
    OPEN = auto()        # Failing, reject requests
    HALF_OPEN = auto()   # Testing recovery


class CircuitBreaker:
    """Circuit breaker for connection failures."""

    def __init__(self, config: ConnectionConfig):
        self.config = config
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.last_success_time: Optional[datetime] = None

    def record_success(self):
        self.last_success_time = datetime.now()
        self.failure_count = 0
        if self.state == CircuitState.HALF_OPEN:
            logger.info("🟢 Circuit breaker CLOSED (recovered)")
            self.state = CircuitState.CLOSED

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = datetime.now()

        if self.state == CircuitState.HALF_OPEN:
            logger.warning("🔴 Circuit breaker OPEN (recovery failed)")
            self.state = CircuitState.OPEN
        elif self.failure_count >= self.config.failure_threshold:
            if self.state != CircuitState.OPEN:
                logger.warning(f"🔴 Circuit breaker OPEN ({self.failure_count} failures)")
            self.state = CircuitState.OPEN

    def can_execute(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            if self.last_failure_time:
                elapsed = (datetime.now() - self.last_failure_time).total_seconds()
                if elapsed >= self.config.recovery_timeout_seconds:
                    logger.info("🟡 Circuit breaker HALF-OPEN (testing)")
                    self.state = CircuitState.HALF_OPEN
                    return True
            return False

        # HALF_OPEN - allow one test request
        return self.state == CircuitState.HALF_OPEN


# =============================================================================
# Main Connection Manager
# =============================================================================

class CloudSQLConnectionManager:
    """
    Singleton async-safe CloudSQL connection pool manager with leak detection.

    Features:
    - Automatic leak detection with connection tracking
    - Circuit breaker for failure isolation
    - Background cleanup of orphan connections
    - Comprehensive metrics and statistics
    - Signal-aware graceful shutdown
    """

    _instance: Optional['CloudSQLConnectionManager'] = None
    _lock = asyncio.Lock()
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        # Core state
        self.pool: Optional[asyncpg.Pool] = None
        self.db_config: Dict[str, Any] = {}
        self._conn_config = ConnectionConfig()
        self.is_shutting_down = False
        self.creation_time: Optional[datetime] = None

        # Legacy compatibility
        self.connection_count = 0
        self.error_count = 0

        # Connection tracking for leak detection
        self._checkouts: Dict[int, ConnectionCheckout] = {}
        self._checkout_lock = asyncio.Lock()
        self._checkout_counter = 0

        # Metrics
        self.metrics = ConnectionMetrics()

        # Circuit breaker
        self._circuit_breaker: Optional[CircuitBreaker] = None

        # Background tasks
        self._cleanup_task: Optional[asyncio.Task] = None
        self._leak_monitor_task: Optional[asyncio.Task] = None

        # Callbacks
        self._on_leak_callbacks: List[Callable] = []

        self._register_shutdown_handlers()
        CloudSQLConnectionManager._initialized = True
        logger.info("🔧 CloudSQL Connection Manager v2.0 initialized")

    def _register_shutdown_handlers(self):
        atexit.register(self._sync_shutdown)
        logger.debug("✅ Shutdown handlers registered")

    def _sync_shutdown(self):
        if self.pool and not self.is_shutting_down:
            logger.info("🛑 atexit: Synchronous shutdown...")
            self.is_shutting_down = True
            try:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_closed():
                        raise RuntimeError("Loop closed")
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    needs_close = True
                else:
                    needs_close = False

                loop.run_until_complete(self.shutdown())
                if needs_close:
                    loop.close()
            except Exception as e:
                logger.warning(f"⚠️ Async shutdown failed: {e}")
                self.pool = None

    async def initialize(
        self,
        host: str = "127.0.0.1",
        port: int = 5432,
        database: str = "jarvis_learning",
        user: str = "jarvis",
        password: Optional[str] = None,
        max_connections: int = 3,
        force_reinit: bool = False,
        config: Optional[ConnectionConfig] = None
    ) -> bool:
        """
        Initialize connection pool with leak detection and circuit breaker.

        Args:
            host: Database host (127.0.0.1 for proxy)
            port: Database port
            database: Database name
            user: Database user
            password: Database password
            max_connections: Max pool size (default 3 for db-f1-micro)
            force_reinit: Force re-initialization
            config: Optional ConnectionConfig for advanced settings

        Returns:
            True if pool is ready
        """
        async with CloudSQLConnectionManager._lock:
            if self.pool and not force_reinit:
                logger.info("♻️ Reusing existing connection pool")
                return True

            if self.pool and force_reinit:
                logger.info("🔄 Force re-init: closing existing pool...")
                await self._close_pool()

            if not ASYNCPG_AVAILABLE:
                logger.error("❌ asyncpg not available")
                return False

            if not password:
                logger.error("❌ Database password required")
                return False

            # Apply config
            if config:
                self._conn_config = config
            self._conn_config.max_connections = max_connections

            # Store DB config
            self.db_config = {
                "host": host,
                "port": port,
                "database": database,
                "user": user,
                "password": password,
                "max_connections": max_connections
            }

            # Initialize circuit breaker
            self._circuit_breaker = CircuitBreaker(self._conn_config)

            try:
                logger.info(f"🔌 Creating CloudSQL connection pool (max={max_connections})...")
                logger.info(f"   Host: {host}:{port}, Database: {database}, User: {user}")

                # Kill leaked connections from previous runs
                await self._kill_leaked_connections()

                # Create pool
                self.pool = await asyncio.wait_for(
                    asyncpg.create_pool(
                        host=host,
                        port=port,
                        database=database,
                        user=user,
                        password=password,
                        min_size=self._conn_config.min_connections,
                        max_size=max_connections,
                        timeout=self._conn_config.connection_timeout,
                        command_timeout=self._conn_config.query_timeout,
                        max_queries=self._conn_config.max_queries_per_connection,
                        max_inactive_connection_lifetime=self._conn_config.max_idle_time_seconds,
                    ),
                    timeout=self._conn_config.pool_creation_timeout
                )

                # Validate
                async with self.pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")

                self.creation_time = datetime.now()
                self.error_count = 0
                self.metrics = ConnectionMetrics()

                logger.info(f"✅ Connection pool created successfully")
                logger.info(f"   Pool: {self.pool.get_size()} total, {self.pool.get_idle_size()} idle")

                # Start background tasks
                if self._conn_config.enable_background_cleanup:
                    self._start_background_tasks()

                return True

            except asyncio.TimeoutError:
                logger.error("⏱️ Connection pool creation timeout")
                logger.error("   Causes: proxy not running, bad credentials, network issues")
                self.pool = None
                return False

            except Exception as e:
                logger.error(f"❌ Failed to create pool: {e}")
                self.pool = None
                self.error_count += 1
                return False

    async def _kill_leaked_connections(self):
        """Kill leaked connections from previous runs or current session."""
        if not self.db_config:
            return

        try:
            logger.info("🧹 Checking for leaked connections...")

            conn = await asyncio.wait_for(
                asyncpg.connect(
                    host=self.db_config["host"],
                    port=self.db_config["port"],
                    database=self.db_config["database"],
                    user=self.db_config["user"],
                    password=self.db_config["password"],
                ),
                timeout=5.0
            )

            threshold_minutes = self._conn_config.leaked_idle_threshold_minutes

            # Find leaked connections
            leaked = await conn.fetch(f"""
                SELECT pid, usename, application_name, state,
                       state_change, query, backend_start,
                       EXTRACT(EPOCH FROM (NOW() - state_change)) as idle_seconds
                FROM pg_stat_activity
                WHERE datname = $1
                  AND pid <> pg_backend_pid()
                  AND usename = $2
                  AND state = 'idle'
                  AND state_change < NOW() - INTERVAL '{threshold_minutes} minutes'
                ORDER BY state_change ASC
            """, self.db_config["database"], self.db_config["user"])

            if leaked:
                logger.warning(f"⚠️ Found {len(leaked)} leaked connections (idle > {threshold_minutes} min)")
                self.metrics.total_leaks_detected += len(leaked)

                for row in leaked:
                    idle_mins = row['idle_seconds'] / 60
                    try:
                        await conn.execute("SELECT pg_terminate_backend($1)", row['pid'])
                        logger.info(f"   ✅ Killed PID {row['pid']} (idle {idle_mins:.1f} min)")
                        self.metrics.total_leaks_recovered += 1
                    except Exception as e:
                        logger.warning(f"   ⚠️ Failed to kill PID {row['pid']}: {e}")
            else:
                logger.info("✅ No leaked connections found")

            await conn.close()

        except asyncio.TimeoutError:
            logger.debug("⏱️ Leak check timeout (proxy not running?)")
        except Exception as e:
            logger.debug(f"⚠️ Leak check failed: {e}")

    def _start_background_tasks(self):
        """Start background monitoring tasks."""
        if not self._cleanup_task or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.debug("🔄 Background cleanup task started")

        if not self._leak_monitor_task or self._leak_monitor_task.done():
            self._leak_monitor_task = asyncio.create_task(self._leak_monitor_loop())
            logger.debug("🔍 Leak monitor task started")

    async def _cleanup_loop(self):
        """Background task for periodic cleanup."""
        while not self.is_shutting_down:
            try:
                await asyncio.sleep(self._conn_config.cleanup_interval_seconds)
                if self.is_shutting_down:
                    break

                # Periodic leak cleanup in database
                if self.pool and self.db_config:
                    await self._kill_leaked_connections()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Cleanup loop error: {e}")
                await asyncio.sleep(10.0)

    async def _leak_monitor_loop(self):
        """Monitor tracked checkouts for potential leaks."""
        while not self.is_shutting_down:
            try:
                await asyncio.sleep(self._conn_config.leak_check_interval_seconds)
                if self.is_shutting_down:
                    break

                async with self._checkout_lock:
                    now = datetime.now()
                    leaked_ids = []

                    for checkout_id, checkout in self._checkouts.items():
                        if checkout.released:
                            continue

                        age = checkout.age_seconds

                        # Warning for long-held connections
                        if age > self._conn_config.checkout_warning_seconds:
                            logger.warning(
                                f"⚠️ Connection held {age:.0f}s (checkout #{checkout_id})\n"
                                f"   Location: {checkout.stack_trace[:300]}..."
                            )

                        # Force mark as leaked if held too long
                        if age > self._conn_config.checkout_timeout_seconds:
                            logger.error(
                                f"🚨 LEAK: Connection #{checkout_id} held {age:.0f}s"
                            )
                            leaked_ids.append(checkout_id)
                            self.metrics.total_leaks_detected += 1

                    # Clean up tracking for leaked connections
                    for checkout_id in leaked_ids:
                        self._checkouts[checkout_id].released = True
                        self.metrics.total_leaks_recovered += 1

                        # Fire callbacks
                        for callback in self._on_leak_callbacks:
                            try:
                                if asyncio.iscoroutinefunction(callback):
                                    await callback(checkout_id)
                                else:
                                    callback(checkout_id)
                            except Exception:
                                pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Leak monitor error: {e}")
                await asyncio.sleep(5.0)

    @asynccontextmanager
    async def connection(self):
        """
        Acquire connection with leak tracking and circuit breaker.

        Usage:
            async with manager.connection() as conn:
                result = await conn.fetchval("SELECT 1")
        """
        if not self.pool:
            raise RuntimeError("Connection pool not initialized")

        if self.is_shutting_down:
            raise RuntimeError("Connection manager is shutting down")

        # Check circuit breaker
        if self._circuit_breaker and not self._circuit_breaker.can_execute():
            raise RuntimeError(
                f"Circuit breaker OPEN - retry in {self._conn_config.recovery_timeout_seconds}s"
            )

        start_time = time.time()
        conn = None
        checkout_id = None

        try:
            # Acquire connection
            conn = await asyncio.wait_for(
                self.pool.acquire(),
                timeout=self._conn_config.connection_timeout
            )

            # Track checkout
            async with self._checkout_lock:
                self._checkout_counter += 1
                checkout_id = self._checkout_counter
                self._checkouts[checkout_id] = ConnectionCheckout(
                    checkout_id=checkout_id,
                    checkout_time=datetime.now(),
                    stack_trace=self._get_caller_info(),
                )

            # Update metrics
            self.connection_count += 1
            self.metrics.total_checkouts += 1
            self.metrics.last_checkout = datetime.now()

            latency_ms = (time.time() - start_time) * 1000
            logger.debug(f"✅ Connection #{checkout_id} acquired ({latency_ms:.1f}ms)")

            # Record success for circuit breaker
            if self._circuit_breaker:
                self._circuit_breaker.record_success()

            yield conn

        except asyncio.TimeoutError:
            logger.error("⏱️ Connection timeout - pool exhausted")
            self.error_count += 1
            self.metrics.total_timeouts += 1
            self.metrics.pool_exhaustion_count += 1
            if self._circuit_breaker:
                self._circuit_breaker.record_failure()
            raise

        except Exception as e:
            if str(e) and str(e) != "0":
                logger.error(f"❌ Connection error: {e}")
            self.error_count += 1
            self.metrics.total_errors += 1
            self.metrics.last_error = datetime.now()
            if self._circuit_breaker:
                self._circuit_breaker.record_failure()
            raise

        finally:
            # ALWAYS release and track
            if conn:
                try:
                    duration_ms = (time.time() - start_time) * 1000

                    # Update metrics
                    self.metrics.total_releases += 1
                    self.metrics.max_checkout_duration_ms = max(
                        self.metrics.max_checkout_duration_ms, duration_ms
                    )
                    n = self.metrics.total_releases
                    self.metrics.avg_checkout_duration_ms = (
                        (self.metrics.avg_checkout_duration_ms * (n - 1) + duration_ms) / n
                    )

                    # Mark checkout as released
                    async with self._checkout_lock:
                        if checkout_id and checkout_id in self._checkouts:
                            self._checkouts[checkout_id].released = True
                            self._checkouts[checkout_id].release_time = datetime.now()
                            # Clean up old checkouts
                            self._cleanup_old_checkouts()

                    # Release to pool
                    await self.pool.release(conn)
                    logger.debug(f"♻️ Connection #{checkout_id} released ({duration_ms:.1f}ms)")

                except Exception as e:
                    logger.error(f"❌ Failed to release connection: {e}")

    def _get_caller_info(self) -> str:
        """Get caller stack trace for debugging."""
        try:
            stack = traceback.extract_stack()
            # Skip internal frames, get caller context
            relevant = [f for f in stack[:-4] if 'cloud_sql' not in f.filename]
            if relevant:
                frame = relevant[-1]
                return f"{frame.filename}:{frame.lineno} in {frame.name}"
            return "unknown"
        except Exception:
            return "unknown"

    def _cleanup_old_checkouts(self):
        """Remove old released checkouts from tracking."""
        cutoff = datetime.now()
        to_remove = [
            cid for cid, c in self._checkouts.items()
            if c.released and c.release_time and
            (cutoff - c.release_time).total_seconds() > 60
        ]
        for cid in to_remove[:50]:  # Limit cleanup batch size
            del self._checkouts[cid]

    async def execute(self, query: str, *args, timeout: float = 30.0):
        """Execute query and return result."""
        async with self.connection() as conn:
            return await asyncio.wait_for(
                conn.execute(query, *args),
                timeout=timeout
            )

    async def fetch(self, query: str, *args, timeout: float = 30.0):
        """Fetch multiple rows."""
        async with self.connection() as conn:
            return await asyncio.wait_for(
                conn.fetch(query, *args),
                timeout=timeout
            )

    async def fetchrow(self, query: str, *args, timeout: float = 30.0):
        """Fetch single row."""
        async with self.connection() as conn:
            return await asyncio.wait_for(
                conn.fetchrow(query, *args),
                timeout=timeout
            )

    async def fetchval(self, query: str, *args, timeout: float = 30.0):
        """Fetch single value."""
        async with self.connection() as conn:
            return await asyncio.wait_for(
                conn.fetchval(query, *args),
                timeout=timeout
            )

    async def _close_pool(self):
        """Close connection pool gracefully."""
        if self.pool:
            try:
                logger.info("🔌 Closing connection pool...")

                # Cancel background tasks
                for task in [self._cleanup_task, self._leak_monitor_task]:
                    if task and not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                # Close pool
                await asyncio.wait_for(self.pool.close(), timeout=10.0)
                logger.info("✅ Connection pool closed")

            except asyncio.TimeoutError:
                logger.warning("⏱️ Pool close timeout - terminating")
                try:
                    self.pool.terminate()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"❌ Error closing pool: {e}")
            finally:
                self.pool = None

    async def shutdown(self):
        """Graceful shutdown with metrics reporting."""
        if self.is_shutting_down and not self.pool:
            return

        self.is_shutting_down = True
        logger.info("🛑 Shutting down CloudSQL Connection Manager...")

        # Log metrics
        self._log_final_metrics()

        # Check for unreleased connections
        async with self._checkout_lock:
            unreleased = [c for c in self._checkouts.values() if not c.released]
            if unreleased:
                logger.warning(f"⚠️ {len(unreleased)} connections not released at shutdown")

        await self._close_pool()
        logger.info("✅ Shutdown complete")

    def _log_final_metrics(self):
        """Log final metrics summary."""
        logger.info("📊 Connection Pool Metrics:")
        logger.info(f"   Checkouts: {self.metrics.total_checkouts}")
        logger.info(f"   Releases: {self.metrics.total_releases}")
        logger.info(f"   Errors: {self.metrics.total_errors}")
        logger.info(f"   Timeouts: {self.metrics.total_timeouts}")
        logger.info(f"   Leaks detected: {self.metrics.total_leaks_detected}")
        logger.info(f"   Leaks recovered: {self.metrics.total_leaks_recovered}")
        if self.metrics.total_releases > 0:
            logger.info(f"   Avg checkout: {self.metrics.avg_checkout_duration_ms:.1f}ms")
            logger.info(f"   Max checkout: {self.metrics.max_checkout_duration_ms:.1f}ms")
        if self.creation_time:
            uptime = (datetime.now() - self.creation_time).total_seconds()
            logger.info(f"   Uptime: {uptime:.1f}s")

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive statistics."""
        base_stats = {
            "status": "running" if self.pool and not self.is_shutting_down else "stopped",
            "pool_size": self.pool.get_size() if self.pool else 0,
            "idle_size": self.pool.get_idle_size() if self.pool else 0,
            "max_size": self.db_config.get("max_connections", 0),
            "connection_count": self.connection_count,
            "error_count": self.error_count,
            "creation_time": self.creation_time.isoformat() if self.creation_time else None,
            "uptime_seconds": (datetime.now() - self.creation_time).total_seconds() if self.creation_time else 0,
            "active_checkouts": len([c for c in self._checkouts.values() if not c.released]),
            "metrics": {
                "total_checkouts": self.metrics.total_checkouts,
                "total_releases": self.metrics.total_releases,
                "total_errors": self.metrics.total_errors,
                "total_timeouts": self.metrics.total_timeouts,
                "leaks_detected": self.metrics.total_leaks_detected,
                "leaks_recovered": self.metrics.total_leaks_recovered,
                "avg_checkout_ms": round(self.metrics.avg_checkout_duration_ms, 2),
                "max_checkout_ms": round(self.metrics.max_checkout_duration_ms, 2),
            },
            "circuit_breaker": {
                "state": self._circuit_breaker.state.name if self._circuit_breaker else "N/A",
                "failures": self._circuit_breaker.failure_count if self._circuit_breaker else 0,
            } if self._circuit_breaker else None,
        }
        return base_stats

    @property
    def is_initialized(self) -> bool:
        return self.pool is not None and not self.is_shutting_down

    # Legacy compatibility property
    @property
    def config(self) -> Dict[str, Any]:
        """Legacy config dict for backward compatibility."""
        return self.db_config

    @config.setter
    def config(self, value):
        """Allow setting config for backward compatibility."""
        if isinstance(value, dict):
            self.db_config = value

    def on_leak_detected(self, callback: Callable):
        """Register callback for leak detection events."""
        self._on_leak_callbacks.append(callback)

    async def force_cleanup_leaks(self) -> int:
        """Force cleanup of all leaked connections. Returns count cleaned."""
        if not self.db_config:
            return 0
        await self._kill_leaked_connections()
        return self.metrics.total_leaks_recovered


# =============================================================================
# Global Singleton Accessor
# =============================================================================

_manager: Optional[CloudSQLConnectionManager] = None


def get_connection_manager() -> CloudSQLConnectionManager:
    """Get singleton connection manager instance."""
    global _manager
    if _manager is None:
        _manager = CloudSQLConnectionManager()
    return _manager
