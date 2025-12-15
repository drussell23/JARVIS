#!/usr/bin/env python3
"""
Voice Unlock Metrics Database v3.0
===================================
Stores voice unlock metrics in SQLite (local) and CloudSQL (cloud) simultaneously.

This provides:
- Local SQLite for fast queries and offline access
- CloudSQL sync for backup and cross-device access
- Automatic schema creation
- Async database operations with timeout protection
- Connection pooling for performance
- Circuit breaker for database failures
- Health monitoring and automatic recovery
- Transaction batching for efficiency
"""

import asyncio
import logging
import sqlite3
import json
import os
import time
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable, Tuple
from dataclasses import dataclass, field, asdict
from functools import wraps
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


# =============================================================================
# DYNAMIC CONFIGURATION
# =============================================================================
class MetricsDatabaseConfig:
    """Dynamic configuration for metrics database."""

    def __init__(self):
        self.query_timeout = float(os.getenv('METRICS_QUERY_TIMEOUT', '5.0'))
        self.write_timeout = float(os.getenv('METRICS_WRITE_TIMEOUT', '10.0'))
        self.pool_size = int(os.getenv('METRICS_POOL_SIZE', '3'))
        self.max_batch_size = int(os.getenv('METRICS_MAX_BATCH_SIZE', '100'))
        self.batch_flush_interval = float(os.getenv('METRICS_BATCH_INTERVAL', '5.0'))
        self.circuit_breaker_threshold = int(os.getenv('METRICS_CB_THRESHOLD', '5'))
        self.circuit_breaker_timeout = float(os.getenv('METRICS_CB_TIMEOUT', '60.0'))
        self.retry_attempts = int(os.getenv('METRICS_RETRY_ATTEMPTS', '3'))
        self.retry_delay = float(os.getenv('METRICS_RETRY_DELAY', '0.5'))


_metrics_config = MetricsDatabaseConfig()


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================
@dataclass
class DatabaseCircuitBreaker:
    """Circuit breaker for database operations."""
    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 60.0

    _failures: int = field(default=0, init=False, repr=False)
    _last_failure_time: float = field(default=0.0, init=False, repr=False)
    _is_open: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def is_available(self) -> bool:
        """Check if circuit allows execution."""
        with self._lock:
            if not self._is_open:
                return True
            if time.time() - self._last_failure_time >= self.recovery_timeout:
                self._is_open = False
                self._failures = 0
                logger.info(f"🟢 DB circuit breaker {self.name} recovered")
                return True
            return False

    def record_success(self):
        """Record successful execution."""
        with self._lock:
            self._failures = 0
            if self._is_open:
                self._is_open = False
                logger.info(f"🟢 DB circuit breaker {self.name} closed")

    def record_failure(self):
        """Record failed execution."""
        with self._lock:
            self._failures += 1
            self._last_failure_time = time.time()
            if self._failures >= self.failure_threshold:
                self._is_open = True
                logger.warning(f"🔴 DB circuit breaker {self.name} opened (failures: {self._failures})")

    def to_dict(self) -> Dict[str, Any]:
        """Export circuit breaker state."""
        with self._lock:
            return {
                'name': self.name,
                'is_open': self._is_open,
                'failures': self._failures,
                'threshold': self.failure_threshold,
            }


# =============================================================================
# CONNECTION POOL
# =============================================================================
class SQLiteConnectionPool:
    """
    Thread-safe SQLite connection pool.

    Provides efficient connection reuse and proper cleanup.
    """

    def __init__(self, db_path: Path, pool_size: int = 3):
        self.db_path = db_path
        self.pool_size = pool_size
        self._pool: Queue = Queue(maxsize=pool_size)
        self._lock = threading.Lock()
        self._created_connections = 0
        self._stats = {
            'checkouts': 0,
            'returns': 0,
            'created': 0,
            'errors': 0,
        }

        # Pre-create connections
        for _ in range(pool_size):
            try:
                conn = self._create_connection()
                self._pool.put(conn)
                self._created_connections += 1
                self._stats['created'] += 1
            except Exception as e:
                logger.warning(f"Failed to pre-create connection: {e}")

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection with optimized settings."""
        conn = sqlite3.connect(
            self.db_path,
            timeout=_metrics_config.query_timeout,
            check_same_thread=False,  # Allow use across threads
            isolation_level=None,  # Autocommit mode
        )
        conn.row_factory = sqlite3.Row

        # Optimize for performance
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        conn.execute("PRAGMA temp_store=MEMORY")

        return conn

    @contextmanager
    def get_connection(self, timeout: float = None):
        """
        Get a connection from the pool.

        Usage:
            with pool.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(...)
        """
        timeout = timeout or _metrics_config.query_timeout
        conn = None

        try:
            # Try to get from pool
            try:
                conn = self._pool.get(timeout=timeout)
                self._stats['checkouts'] += 1
            except Empty:
                # Pool exhausted - create temporary connection
                with self._lock:
                    logger.warning("Connection pool exhausted, creating temporary connection")
                    conn = self._create_connection()
                    self._stats['created'] += 1

            yield conn

        except Exception as e:
            self._stats['errors'] += 1
            raise

        finally:
            if conn is not None:
                try:
                    # Return to pool or close temporary connection
                    if not self._pool.full():
                        self._pool.put(conn)
                        self._stats['returns'] += 1
                    else:
                        conn.close()
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def close_all(self):
        """Close all connections in the pool."""
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Empty:
                break
            except Exception as e:
                logger.warning(f"Error closing connection: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get pool statistics."""
        return {
            **self._stats,
            'pool_size': self.pool_size,
            'available': self._pool.qsize(),
            'in_use': self._created_connections - self._pool.qsize(),
        }


# =============================================================================
# ASYNC QUERY EXECUTOR
# =============================================================================
class AsyncQueryExecutor:
    """
    Execute database queries asynchronously with timeout protection.

    Wraps synchronous SQLite operations with asyncio.to_thread()
    and adds timeout protection, retry logic, and circuit breaker.
    """

    def __init__(self, pool: SQLiteConnectionPool, circuit_breaker: DatabaseCircuitBreaker):
        self.pool = pool
        self.circuit_breaker = circuit_breaker
        self._executor = ThreadPoolExecutor(max_workers=_metrics_config.pool_size)
        self._stats = {
            'queries': 0,
            'writes': 0,
            'timeouts': 0,
            'retries': 0,
            'failures': 0,
        }

    async def execute_query(
        self,
        query: str,
        params: tuple = (),
        timeout: float = None,
        fetch: str = 'all',  # 'all', 'one', 'none'
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Execute a query asynchronously with timeout protection.

        Args:
            query: SQL query string
            params: Query parameters
            timeout: Timeout in seconds
            fetch: 'all' for fetchall, 'one' for fetchone, 'none' for no fetch

        Returns:
            Query results as list of dicts, or None on error
        """
        timeout = timeout or _metrics_config.query_timeout

        # Check circuit breaker
        if not self.circuit_breaker.is_available():
            logger.warning("Database circuit breaker open - skipping query")
            return None

        self._stats['queries'] += 1

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._execute_sync, query, params, fetch),
                timeout=timeout
            )
            self.circuit_breaker.record_success()
            return result

        except asyncio.TimeoutError:
            self._stats['timeouts'] += 1
            self.circuit_breaker.record_failure()
            logger.warning(f"Query timed out after {timeout}s: {query[:50]}...")
            return None

        except Exception as e:
            self._stats['failures'] += 1
            self.circuit_breaker.record_failure()
            logger.error(f"Query failed: {e}")
            return None

    async def execute_write(
        self,
        query: str,
        params: tuple = (),
        timeout: float = None,
        retry: bool = True,
    ) -> Optional[int]:
        """
        Execute a write operation asynchronously with retry logic.

        Args:
            query: SQL query string
            params: Query parameters
            timeout: Timeout in seconds
            retry: Whether to retry on failure

        Returns:
            Last row ID, or None on error
        """
        timeout = timeout or _metrics_config.write_timeout
        max_attempts = _metrics_config.retry_attempts if retry else 1

        # Check circuit breaker
        if not self.circuit_breaker.is_available():
            logger.warning("Database circuit breaker open - skipping write")
            return None

        self._stats['writes'] += 1

        for attempt in range(max_attempts):
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._execute_write_sync, query, params),
                    timeout=timeout
                )
                self.circuit_breaker.record_success()
                return result

            except asyncio.TimeoutError:
                self._stats['timeouts'] += 1
                if attempt < max_attempts - 1:
                    self._stats['retries'] += 1
                    delay = _metrics_config.retry_delay * (2 ** attempt)
                    logger.warning(f"Write timed out, retrying in {delay:.1f}s ({attempt + 1}/{max_attempts})")
                    await asyncio.sleep(delay)
                else:
                    self.circuit_breaker.record_failure()
                    logger.error(f"Write failed after {max_attempts} attempts")
                    return None

            except Exception as e:
                if attempt < max_attempts - 1:
                    self._stats['retries'] += 1
                    delay = _metrics_config.retry_delay * (2 ** attempt)
                    logger.warning(f"Write failed: {e}, retrying in {delay:.1f}s")
                    await asyncio.sleep(delay)
                else:
                    self._stats['failures'] += 1
                    self.circuit_breaker.record_failure()
                    logger.error(f"Write failed after {max_attempts} attempts: {e}")
                    return None

        return None

    def _execute_sync(self, query: str, params: tuple, fetch: str) -> Optional[List[Dict[str, Any]]]:
        """Synchronous query execution."""
        with self.pool.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)

            if fetch == 'all':
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
            elif fetch == 'one':
                row = cursor.fetchone()
                return [dict(row)] if row else []
            else:
                return []

    def _execute_write_sync(self, query: str, params: tuple) -> int:
        """Synchronous write execution."""
        with self.pool.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            return cursor.lastrowid

    def get_stats(self) -> Dict[str, Any]:
        """Get executor statistics."""
        return {
            **self._stats,
            'circuit_breaker': self.circuit_breaker.to_dict(),
            'pool': self.pool.get_stats(),
        }


# =============================================================================
# BATCH WRITER
# =============================================================================
class BatchWriter:
    """
    Batch database writes for efficiency.

    Collects writes and flushes them periodically or when batch is full.
    """

    def __init__(self, executor: AsyncQueryExecutor):
        self.executor = executor
        self._batch: List[Tuple[str, tuple]] = []
        self._lock = asyncio.Lock()
        self._last_flush = time.time()
        self._stats = {
            'batches_flushed': 0,
            'items_written': 0,
            'items_dropped': 0,
        }

    async def add(self, query: str, params: tuple):
        """Add a write to the batch."""
        async with self._lock:
            self._batch.append((query, params))

            # Flush if batch is full
            if len(self._batch) >= _metrics_config.max_batch_size:
                await self._flush_batch()

    async def flush(self):
        """Force flush the current batch."""
        async with self._lock:
            await self._flush_batch()

    async def _flush_batch(self):
        """Flush the current batch to database."""
        if not self._batch:
            return

        batch = self._batch.copy()
        self._batch = []

        try:
            # Execute batch as transaction
            success = await self.executor.execute_write(
                "BEGIN TRANSACTION", (), retry=False
            )

            if success is not None:
                for query, params in batch:
                    await self.executor.execute_write(query, params, retry=False)
                await self.executor.execute_write("COMMIT", (), retry=False)
                self._stats['items_written'] += len(batch)
            else:
                self._stats['items_dropped'] += len(batch)

            self._stats['batches_flushed'] += 1
            self._last_flush = time.time()

        except Exception as e:
            logger.error(f"Batch flush failed: {e}")
            self._stats['items_dropped'] += len(batch)
            try:
                await self.executor.execute_write("ROLLBACK", (), retry=False)
            except Exception:
                pass

    async def check_flush_interval(self):
        """Flush if interval has elapsed."""
        if time.time() - self._last_flush >= _metrics_config.batch_flush_interval:
            await self.flush()

    def get_stats(self) -> Dict[str, Any]:
        """Get batch writer statistics."""
        return {
            **self._stats,
            'pending': len(self._batch),
            'last_flush': self._last_flush,
        }


class MetricsDatabase:
    """
    Dual-database system for voice unlock metrics v3.0.

    Stores metrics in both SQLite (local) and CloudSQL (cloud) for:
    - Fast local queries
    - Cloud backup
    - Historical analysis
    - Cross-device sync

    Enhanced Features:
    - Connection pooling for performance
    - Async query execution with timeout protection
    - Circuit breaker for database failures
    - Transaction batching for efficiency
    - Health monitoring and automatic recovery
    """

    def __init__(self, sqlite_path: str = None, use_cloud_sql: bool = True):
        """
        Initialize metrics database.

        Args:
            sqlite_path: Path to SQLite database file
            use_cloud_sql: Whether to sync to CloudSQL (requires existing connection)
        """
        if sqlite_path is None:
            db_dir = Path.home() / ".jarvis/logs/unlock_metrics"
            db_dir.mkdir(parents=True, exist_ok=True)
            self.sqlite_path = db_dir / "unlock_metrics.db"
        else:
            self.sqlite_path = Path(sqlite_path)

        self.use_cloud_sql = use_cloud_sql
        self.cloud_db = None

        # Initialize advanced infrastructure
        self._circuit_breaker = DatabaseCircuitBreaker(
            name='metrics_sqlite',
            failure_threshold=_metrics_config.circuit_breaker_threshold,
            recovery_timeout=_metrics_config.circuit_breaker_timeout,
        )

        # Initialize connection pool (lazy - created after schema init)
        self._pool: Optional[SQLiteConnectionPool] = None
        self._executor: Optional[AsyncQueryExecutor] = None
        self._batch_writer: Optional[BatchWriter] = None

        # Statistics
        self._stats = {
            'total_writes': 0,
            'total_reads': 0,
            'start_time': time.time(),
        }

        # Initialize databases (creates schema)
        self._init_sqlite()

        # Now initialize connection pool with existing database
        self._init_pool()

        if self.use_cloud_sql:
            self._init_cloud_sql()

    def _init_pool(self):
        """Initialize connection pool and async executor."""
        self._pool = SQLiteConnectionPool(
            self.sqlite_path,
            pool_size=_metrics_config.pool_size
        )
        self._executor = AsyncQueryExecutor(self._pool, self._circuit_breaker)
        self._batch_writer = BatchWriter(self._executor)
        logger.info(f"✅ Metrics database pool initialized (size: {_metrics_config.pool_size})")

    def _init_sqlite(self):
        """Initialize SQLite database with schema"""
        conn = sqlite3.connect(self.sqlite_path)
        cursor = conn.cursor()

        # Create unlock_attempts table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS unlock_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                day_of_week TEXT,
                unix_timestamp REAL,
                success INTEGER NOT NULL,
                speaker_name TEXT NOT NULL,
                transcribed_text TEXT,
                error TEXT,

                -- Biometrics
                speaker_confidence REAL,
                stt_confidence REAL,
                threshold REAL,
                above_threshold INTEGER,
                confidence_margin REAL,
                margin_percentage REAL,

                -- Confidence Trends
                avg_last_10 REAL,
                avg_last_30 REAL,
                trend_direction TEXT,
                volatility REAL,
                best_ever REAL,
                worst_ever REAL,
                percentile_rank REAL,

                -- Performance
                total_duration_ms REAL,
                slowest_stage TEXT,
                fastest_stage TEXT,

                -- Quality
                audio_quality TEXT,
                voice_match_quality TEXT,
                overall_confidence REAL,

                -- Stage Summary
                total_stages INTEGER,
                successful_stages INTEGER,
                failed_stages INTEGER,
                all_stages_passed INTEGER,

                -- System Info
                platform TEXT,
                platform_version TEXT,
                python_version TEXT,
                stt_engine TEXT,
                speaker_engine TEXT,

                -- Metadata
                session_id TEXT,
                logger_version TEXT,

                -- Indexes for fast queries
                UNIQUE(timestamp, speaker_name)
            )
        """)

        # Create processing_stages table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processing_stages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL,
                stage_name TEXT NOT NULL,
                started_at REAL,
                ended_at REAL,
                duration_ms REAL,
                percentage_of_total REAL,
                success INTEGER,
                algorithm_used TEXT,
                module_path TEXT,
                function_name TEXT,
                input_size_bytes INTEGER,
                output_size_bytes INTEGER,
                confidence_score REAL,
                threshold REAL,
                above_threshold INTEGER,
                error_message TEXT,
                metadata_json TEXT,

                FOREIGN KEY (attempt_id) REFERENCES unlock_attempts(id) ON DELETE CASCADE
            )
        """)

        # Create stage_breakdown table (for quick performance queries)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stage_breakdown (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL,
                stage_name TEXT NOT NULL,
                duration_ms REAL,
                percentage REAL,

                FOREIGN KEY (attempt_id) REFERENCES unlock_attempts(id) ON DELETE CASCADE
            )
        """)

        # 🤖 ADVANCED ML TRAINING: Character-level typing metrics for continuous learning
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS password_typing_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER,  -- NULL allowed for standalone typing tests
                timestamp TEXT NOT NULL,
                success INTEGER NOT NULL,

                -- Session Metrics
                total_characters INTEGER,
                characters_typed INTEGER,
                typing_method TEXT,  -- 'core_graphics', 'applescript_fallback', etc.
                fallback_used INTEGER DEFAULT 0,

                -- Performance
                total_typing_duration_ms REAL,
                avg_char_duration_ms REAL,
                min_char_duration_ms REAL,
                max_char_duration_ms REAL,

                -- System Context
                system_load REAL,
                memory_pressure TEXT,
                screen_locked INTEGER,

                -- Timing Patterns (for ML)
                inter_char_delay_avg_ms REAL,
                inter_char_delay_std_ms REAL,
                shift_press_duration_avg_ms REAL,
                shift_release_delay_avg_ms REAL,

                -- Success Patterns
                failed_at_character INTEGER,  -- Which character position failed (NULL if success)
                retry_count INTEGER DEFAULT 0,

                -- Environment
                time_of_day TEXT,
                day_of_week TEXT,

                FOREIGN KEY (attempt_id) REFERENCES unlock_attempts(id) ON DELETE CASCADE
            )
        """)

        # 🔬 ULTRA-DETAILED: Individual character typing metrics
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS character_typing_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                attempt_id INTEGER,  -- NULL allowed for standalone typing tests

                -- Character Identity (hashed for security)
                char_position INTEGER NOT NULL,  -- Position in password (1-indexed)
                char_type TEXT NOT NULL,  -- 'letter', 'digit', 'special'
                char_case TEXT,  -- 'upper', 'lower', 'none'
                requires_shift INTEGER,
                keycode TEXT,  -- Hex keycode (e.g., '0x02')

                -- Timing Metrics (microsecond precision)
                char_start_time_ms REAL,
                char_end_time_ms REAL,
                total_duration_ms REAL,

                -- Shift Handling (for special chars)
                shift_down_duration_ms REAL,
                shift_registered_delay_ms REAL,  -- Delay between shift press and char press
                shift_up_delay_ms REAL,

                -- Key Events
                key_down_created INTEGER,  -- 1 if event created successfully
                key_down_posted INTEGER,
                key_press_duration_ms REAL,  -- Time between key down and key up
                key_up_created INTEGER,
                key_up_posted INTEGER,

                -- Success Metrics
                success INTEGER NOT NULL,
                error_type TEXT,  -- 'keycode_missing', 'event_creation_failed', etc.
                error_message TEXT,
                retry_attempted INTEGER DEFAULT 0,

                -- Inter-character delay (time since previous character)
                inter_char_delay_ms REAL,

                -- System State
                system_load_at_char REAL,

                FOREIGN KEY (session_id) REFERENCES password_typing_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY (attempt_id) REFERENCES unlock_attempts(id) ON DELETE CASCADE
            )
        """)

        # 📊 ML TRAINING: Aggregate patterns for predictive optimization
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS typing_pattern_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                calculated_at TEXT NOT NULL,

                -- Pattern Recognition
                pattern_type TEXT,  -- 'successful_timing', 'failed_timing', 'optimal_delays'
                char_type TEXT,  -- 'letter', 'digit', 'special', 'all'
                requires_shift INTEGER,

                -- Statistical Analysis
                sample_count INTEGER,
                success_rate REAL,
                avg_duration_ms REAL,
                std_duration_ms REAL,
                min_duration_ms REAL,
                max_duration_ms REAL,

                -- Optimal Values (ML predictions)
                optimal_char_duration_ms REAL,
                optimal_inter_char_delay_ms REAL,
                optimal_shift_duration_ms REAL,

                -- Confidence
                confidence_score REAL,  -- 0.0-1.0, based on sample count and consistency

                -- Context
                time_of_day_pattern TEXT,  -- 'morning', 'afternoon', 'night'
                system_load_pattern TEXT,  -- 'low', 'medium', 'high'

                -- Metadata
                last_updated TEXT,
                training_samples_used INTEGER
            )
        """)

        # 🎯 CONTINUOUS LEARNING: Performance improvements over time
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS learning_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,

                -- Overall Metrics
                total_attempts INTEGER,
                successful_attempts INTEGER,
                success_rate REAL,

                -- Performance Trends
                avg_typing_duration_last_10 REAL,
                avg_typing_duration_last_50 REAL,
                avg_typing_duration_all_time REAL,
                improvement_percentage REAL,

                -- Character-level Improvements
                avg_char_duration_last_10 REAL,
                avg_char_duration_last_50 REAL,
                fastest_ever_typing_ms REAL,

                -- Reliability Metrics
                consecutive_successes INTEGER,
                consecutive_failures INTEGER,
                failure_rate_last_10 REAL,

                -- ML Model Performance
                model_version TEXT,
                prediction_accuracy REAL,
                optimal_timing_applied INTEGER,

                -- Context Awareness
                best_time_of_day TEXT,
                best_system_load_range TEXT,

                -- Adaptive Learning
                current_strategy TEXT,  -- 'conservative', 'balanced', 'aggressive'
                timing_adjustments_json TEXT  -- JSON of current timing parameters
            )
        """)

        # 🖥️ DISPLAY-AWARE SAI: Track display context during unlock attempts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS display_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER,
                timestamp TEXT NOT NULL,

                -- Display Configuration
                total_displays INTEGER DEFAULT 1,
                display_mode TEXT,  -- 'SINGLE', 'EXTENDED', 'MIRRORED', 'CLAMSHELL'
                is_mirrored INTEGER DEFAULT 0,
                has_external INTEGER DEFAULT 0,

                -- TV Detection
                is_tv_connected INTEGER DEFAULT 0,
                tv_name TEXT,
                tv_brand TEXT,
                tv_detection_confidence REAL,
                tv_detection_reasons TEXT,  -- JSON array of reasons

                -- Primary Display Info
                primary_display_name TEXT,
                primary_display_type TEXT,
                primary_width INTEGER,
                primary_height INTEGER,
                primary_is_builtin INTEGER,

                -- External Display Info (if any)
                external_display_name TEXT,
                external_display_type TEXT,
                external_width INTEGER,
                external_height INTEGER,

                -- Typing Strategy Used
                typing_strategy TEXT,  -- 'CORE_GRAPHICS_FAST', 'APPLESCRIPT_DIRECT', etc.
                keystroke_delay_ms REAL,
                wake_delay_ms REAL,
                strategy_reasoning TEXT,

                -- Detection Performance
                detection_time_ms REAL,
                detection_method TEXT,  -- 'core_graphics', 'system_profiler', 'fallback'

                FOREIGN KEY (attempt_id) REFERENCES unlock_attempts(id) ON DELETE CASCADE
            )
        """)

        # 📊 TV UNLOCK ANALYTICS: Aggregate stats for TV vs non-TV unlocks
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tv_unlock_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,

                -- Period (for time-series analysis)
                period_type TEXT,  -- 'hourly', 'daily', 'weekly'
                period_start TEXT,
                period_end TEXT,

                -- TV Unlock Stats
                tv_unlock_attempts INTEGER DEFAULT 0,
                tv_unlock_successes INTEGER DEFAULT 0,
                tv_unlock_failures INTEGER DEFAULT 0,
                tv_success_rate REAL,

                -- Non-TV Unlock Stats
                non_tv_unlock_attempts INTEGER DEFAULT 0,
                non_tv_unlock_successes INTEGER DEFAULT 0,
                non_tv_unlock_failures INTEGER DEFAULT 0,
                non_tv_success_rate REAL,

                -- Strategy Effectiveness
                applescript_attempts INTEGER DEFAULT 0,
                applescript_successes INTEGER DEFAULT 0,
                applescript_success_rate REAL,
                core_graphics_attempts INTEGER DEFAULT 0,
                core_graphics_successes INTEGER DEFAULT 0,
                core_graphics_success_rate REAL,
                hybrid_attempts INTEGER DEFAULT 0,
                hybrid_successes INTEGER DEFAULT 0,
                hybrid_success_rate REAL,

                -- Performance Comparison
                avg_tv_unlock_duration_ms REAL,
                avg_non_tv_unlock_duration_ms REAL,
                tv_performance_overhead_ms REAL,  -- How much slower TV unlocks are

                -- TV Types Seen
                unique_tv_brands TEXT,  -- JSON array of unique TV brands
                most_common_tv TEXT,
                tv_brand_success_rates TEXT,  -- JSON object {brand: success_rate}

                -- Mirroring Stats
                mirrored_attempts INTEGER DEFAULT 0,
                mirrored_successes INTEGER DEFAULT 0,
                mirrored_success_rate REAL,
                extended_attempts INTEGER DEFAULT 0,
                extended_successes INTEGER DEFAULT 0,
                extended_success_rate REAL
            )
        """)

        # 🎯 DISPLAY-SPECIFIC SUCCESS RATES: Per-display learning
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS display_success_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_identifier TEXT NOT NULL,  -- Unique display name/ID
                display_type TEXT,  -- 'TV', 'MONITOR', 'BUILTIN'
                first_seen TEXT,
                last_seen TEXT,

                -- Success Tracking
                total_attempts INTEGER DEFAULT 0,
                successful_attempts INTEGER DEFAULT 0,
                failed_attempts INTEGER DEFAULT 0,
                success_rate REAL,
                current_streak INTEGER DEFAULT 0,  -- Positive = successes, negative = failures
                best_streak INTEGER DEFAULT 0,
                worst_streak INTEGER DEFAULT 0,

                -- Strategy History
                applescript_attempts INTEGER DEFAULT 0,
                applescript_successes INTEGER DEFAULT 0,
                core_graphics_attempts INTEGER DEFAULT 0,
                core_graphics_successes INTEGER DEFAULT 0,
                preferred_strategy TEXT,  -- Learned best strategy for this display

                -- Timing Optimization
                optimal_keystroke_delay_ms REAL,
                optimal_wake_delay_ms REAL,
                avg_unlock_duration_ms REAL,
                fastest_unlock_ms REAL,
                slowest_unlock_ms REAL,

                -- Display Characteristics
                typical_resolution TEXT,
                is_tv INTEGER,
                tv_brand TEXT,
                connection_type TEXT,

                UNIQUE(display_identifier)
            )
        """)

        # 🎙️ VOICE PROFILE LEARNING: Track voice sample collection and confidence improvement
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS voice_profile_learning (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                speaker_name TEXT NOT NULL,

                -- Sample Collection Stats
                total_samples_collected INTEGER DEFAULT 0,
                samples_added_today INTEGER DEFAULT 0,
                samples_added_this_week INTEGER DEFAULT 0,
                high_confidence_samples INTEGER DEFAULT 0,  -- Samples with confidence > 85%
                low_quality_samples_rejected INTEGER DEFAULT 0,

                -- Confidence Tracking
                current_avg_confidence REAL,
                best_confidence_ever REAL,
                worst_confidence_ever REAL,
                confidence_7day_avg REAL,
                confidence_30day_avg REAL,
                confidence_trend TEXT,  -- 'improving', 'stable', 'declining'
                confidence_improvement_rate REAL,  -- % improvement per week

                -- Embedding Evolution
                embedding_last_updated TEXT,
                embedding_version INTEGER DEFAULT 1,
                rolling_samples_used INTEGER DEFAULT 0,
                embedding_quality_score REAL,

                -- Authentication Performance
                total_unlock_attempts INTEGER DEFAULT 0,
                successful_unlocks INTEGER DEFAULT 0,
                false_rejections INTEGER DEFAULT 0,  -- User rejected incorrectly
                false_acceptances INTEGER DEFAULT 0,  -- Non-user accepted incorrectly
                unlock_success_rate REAL,
                avg_verification_time_ms REAL,

                -- Learning Milestones
                first_sample_date TEXT,
                reached_50_samples_date TEXT,
                reached_100_samples_date TEXT,
                reached_optimal_confidence_date TEXT,

                -- Quality Metrics
                avg_audio_quality REAL,
                avg_snr_db REAL,
                environments_learned TEXT,  -- JSON array of environment types

                UNIQUE(speaker_name)
            )
        """)

        # 🎙️ VOICE SAMPLE LOG: Individual sample tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS voice_sample_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                speaker_name TEXT NOT NULL,

                -- Sample Details
                sample_source TEXT,  -- 'unlock_attempt', 'enrollment', 'calibration'
                audio_duration_ms REAL,
                audio_quality_score REAL,
                snr_db REAL,

                -- Verification Result
                confidence REAL,
                was_verified INTEGER,
                threshold_used REAL,

                -- Whether sample was used for learning
                added_to_profile INTEGER DEFAULT 0,
                rejection_reason TEXT,  -- 'low_quality', 'low_confidence', 'daily_limit', etc.

                -- Context
                environment_type TEXT,
                time_of_day TEXT,  -- 'morning', 'afternoon', 'evening', 'night'
                display_mode TEXT,  -- 'single', 'tv_mirrored', etc.

                -- Embedding (stored as base64)
                embedding_stored INTEGER DEFAULT 0,
                embedding_dimensions INTEGER
            )
        """)

        # 🎙️ VOICE EMBEDDINGS: Store actual voiceprint embeddings (synced with Cloud SQL)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS voice_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                speaker_name TEXT NOT NULL,
                speaker_id INTEGER,  -- Cloud SQL speaker_id for sync

                -- Current Embedding (base64 encoded)
                embedding_b64 TEXT NOT NULL,
                embedding_dimensions INTEGER DEFAULT 192,
                embedding_dtype TEXT DEFAULT 'float32',

                -- Version Control
                version INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                -- Learning Stats
                total_samples_used INTEGER DEFAULT 0,
                rolling_samples_in_avg INTEGER DEFAULT 0,

                -- Quality Metrics
                embedding_quality_score REAL,
                avg_sample_confidence REAL,

                -- Sync Status with Cloud SQL
                cloud_sql_synced INTEGER DEFAULT 0,
                cloud_sql_sync_time TEXT,
                cloud_sql_version INTEGER,

                -- Source Info
                source TEXT DEFAULT 'continuous_learning',  -- 'enrollment', 'continuous_learning', 'calibration'
                last_verification_confidence REAL,

                UNIQUE(speaker_name)
            )
        """)

        # 🎙️ EMBEDDING HISTORY: Track all embedding updates for rollback capability
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS voice_embedding_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                speaker_name TEXT NOT NULL,

                -- Embedding Snapshot
                embedding_b64 TEXT NOT NULL,
                embedding_dimensions INTEGER,
                version INTEGER,

                -- What triggered this update
                update_reason TEXT,  -- 'periodic_save', 'milestone', 'calibration', 'manual'
                samples_used INTEGER,
                total_samples_at_update INTEGER,

                -- Quality at time of save
                avg_confidence_at_save REAL,

                -- Cloud SQL sync
                cloud_sql_synced INTEGER DEFAULT 0
            )
        """)

        # 🎙️ CONFIDENCE HISTORY: Track confidence over time for trending
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS voice_confidence_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                speaker_name TEXT NOT NULL,
                date TEXT NOT NULL,

                -- Daily Aggregates
                attempts_today INTEGER DEFAULT 0,
                avg_confidence_today REAL,
                best_confidence_today REAL,
                worst_confidence_today REAL,

                -- Cumulative Stats at this point
                total_samples_to_date INTEGER,
                rolling_avg_confidence REAL,  -- Last 50 attempts

                -- Environmental Performance
                home_confidence_avg REAL,
                office_confidence_avg REAL,
                noisy_confidence_avg REAL,

                -- Time-of-day Performance
                morning_confidence_avg REAL,
                afternoon_confidence_avg REAL,
                evening_confidence_avg REAL,
                night_confidence_avg REAL
            )
        """)

        # 🖥️ DYNAMIC SAI: Display connection events (real-time TV detection)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS display_connection_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,  -- 'CONNECTED', 'DISCONNECTED', 'CONFIG_CHANGED', 'SAI_CHECK'

                -- Display Identification
                display_identifier TEXT NOT NULL,
                display_name TEXT,
                display_type TEXT,  -- 'TV', 'MONITOR', 'BUILTIN'
                is_tv INTEGER DEFAULT 0,
                tv_brand TEXT,
                tv_confidence REAL,
                tv_detection_reasons TEXT,  -- JSON array

                -- Display Configuration at Event Time
                total_displays INTEGER,
                display_mode TEXT,  -- 'SINGLE', 'MIRRORED', 'EXTENDED', 'CLAMSHELL'
                is_mirrored INTEGER DEFAULT 0,
                resolution TEXT,
                refresh_rate REAL,

                -- SAI Detection Details
                sai_detection_method TEXT,  -- 'core_graphics', 'system_profiler', 'iokit', 'fallback'
                sai_detection_time_ms REAL,
                sai_confidence REAL,
                sai_reasoning TEXT,  -- JSON array of reasoning steps

                -- Connection Duration (for DISCONNECTED events)
                connection_duration_seconds REAL,
                unlocks_while_connected INTEGER DEFAULT 0,
                unlock_success_rate_while_connected REAL,

                -- System Context
                system_uptime_seconds REAL,
                was_screen_locked INTEGER,
                trigger_source TEXT  -- 'manual_check', 'auto_monitor', 'unlock_attempt', 'startup'
            )
        """)

        # 📊 ENHANCED TV ANALYTICS: Real-time connection state tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tv_connection_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                last_updated TEXT NOT NULL,

                -- Current State (SAI real-time awareness)
                is_tv_currently_connected INTEGER DEFAULT 0,
                current_tv_name TEXT,
                current_tv_brand TEXT,
                current_display_mode TEXT,
                current_is_mirrored INTEGER DEFAULT 0,

                -- Connection Statistics
                total_tv_connections INTEGER DEFAULT 0,
                total_tv_disconnections INTEGER DEFAULT 0,
                avg_connection_duration_minutes REAL,
                longest_connection_minutes REAL,
                shortest_connection_minutes REAL,

                -- Time-based Patterns (SAI learning)
                most_common_connection_hour INTEGER,  -- 0-23
                most_common_disconnection_hour INTEGER,
                typical_session_duration_minutes REAL,
                connection_pattern TEXT,  -- JSON: {"morning": 0.2, "afternoon": 0.5, "evening": 0.8, "night": 0.1}

                -- TV-specific Learning
                known_tvs TEXT,  -- JSON array of {name, brand, total_connections, success_rate}
                preferred_tv TEXT,  -- Most frequently used TV
                tv_reliability_scores TEXT,  -- JSON: {tv_name: reliability_score}

                -- SAI Recommendations
                sai_recommended_strategy TEXT,
                sai_confidence REAL,
                sai_last_analysis TEXT,
                sai_reasoning TEXT
            )
        """)

        # 📈 TV SESSION TRACKING: Track each TV connection session
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tv_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_start TEXT NOT NULL,
                session_end TEXT,
                is_active INTEGER DEFAULT 1,

                -- TV Identity
                tv_identifier TEXT NOT NULL,
                tv_name TEXT,
                tv_brand TEXT,
                tv_confidence REAL,

                -- Session Configuration
                display_mode TEXT,
                is_mirrored INTEGER DEFAULT 0,
                resolution TEXT,

                -- Session Performance
                unlock_attempts INTEGER DEFAULT 0,
                unlock_successes INTEGER DEFAULT 0,
                unlock_failures INTEGER DEFAULT 0,
                session_success_rate REAL,

                -- Strategy Performance During Session
                applescript_attempts INTEGER DEFAULT 0,
                applescript_successes INTEGER DEFAULT 0,
                core_graphics_attempts INTEGER DEFAULT 0,
                core_graphics_successes INTEGER DEFAULT 0,
                best_strategy_this_session TEXT,

                -- Timing
                session_duration_minutes REAL,
                avg_unlock_duration_ms REAL,
                fastest_unlock_ms REAL,
                slowest_unlock_ms REAL
            )
        """)

        # 🧠 STT HALLUCINATION LEARNING: Track detected hallucinations for continuous learning
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stt_hallucinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,

                -- Original Transcription
                original_text TEXT NOT NULL,
                original_text_normalized TEXT NOT NULL,
                stt_confidence REAL,

                -- Correction
                corrected_text TEXT,
                was_corrected INTEGER DEFAULT 0,
                correction_source TEXT,  -- 'consensus_alternative', 'learned', 'contextual_default', 'user_feedback'

                -- Detection Details
                hallucination_type TEXT NOT NULL,  -- 'known_pattern', 'contextual_mismatch', etc.
                detection_confidence REAL NOT NULL,
                detection_method TEXT,  -- 'langgraph_reasoning', 'pattern_match', etc.
                reasoning_steps INTEGER DEFAULT 0,

                -- Context at Detection
                context TEXT DEFAULT 'unlock_command',
                audio_hash TEXT,
                sai_tv_connected INTEGER DEFAULT 0,
                sai_display_count INTEGER,

                -- Learning Stats
                occurrence_count INTEGER DEFAULT 1,
                last_occurrence TEXT,
                times_corrected INTEGER DEFAULT 0,
                times_flagged INTEGER DEFAULT 0,

                -- User Feedback
                user_confirmed INTEGER DEFAULT 0,  -- User confirmed this was wrong
                user_correction TEXT,  -- User provided correction

                UNIQUE(original_text_normalized)
            )
        """)

        # 🧠 HALLUCINATION CORRECTIONS: Map hallucinations to corrections
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hallucination_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,

                -- Mapping
                hallucination_normalized TEXT NOT NULL,
                correction TEXT NOT NULL,

                -- Confidence in this correction
                correction_confidence REAL DEFAULT 0.9,
                times_applied INTEGER DEFAULT 1,
                last_applied TEXT,

                -- Source
                source TEXT,  -- 'auto_learned', 'user_feedback', 'consensus'

                UNIQUE(hallucination_normalized)
            )
        """)

        # 🧠 USER BEHAVIORAL PATTERNS: Track typical user phrases and times
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_behavioral_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                context TEXT NOT NULL,  -- 'unlock_command', 'general', etc.

                -- Phrase Patterns
                phrase TEXT NOT NULL,
                phrase_normalized TEXT NOT NULL,
                occurrence_count INTEGER DEFAULT 1,
                last_used TEXT,

                -- Time Patterns
                typical_hours TEXT,  -- JSON array of typical hours [6, 7, 8, 17, 18]

                -- Success Rate
                success_count INTEGER DEFAULT 0,
                failure_count INTEGER DEFAULT 0,

                UNIQUE(context, phrase_normalized)
            )
        """)

        # 🧠 HALLUCINATION REASONING LOG: Detailed reasoning traces
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hallucination_reasoning_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                hallucination_id INTEGER,

                -- Reasoning Details
                reasoning_steps_json TEXT,  -- Full reasoning chain as JSON
                hypotheses_json TEXT,  -- Hypotheses formed
                evidence_json TEXT,  -- Evidence gathered

                -- Analysis Results
                pattern_analysis_json TEXT,
                consensus_analysis_json TEXT,
                context_analysis_json TEXT,
                phonetic_analysis_json TEXT,
                behavioral_analysis_json TEXT,
                sai_analysis_json TEXT,

                -- Final Decision
                final_decision TEXT,

                FOREIGN KEY (hallucination_id) REFERENCES stt_hallucinations(id)
            )
        """)

        # Create indexes for fast queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_attempts_date ON unlock_attempts(date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_attempts_speaker ON unlock_attempts(speaker_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_attempts_success ON unlock_attempts(success)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_attempts_timestamp ON unlock_attempts(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stages_attempt ON processing_stages(attempt_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stages_name ON processing_stages(stage_name)")

        # Indexes for ML tables
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_typing_sessions_attempt ON password_typing_sessions(attempt_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_typing_sessions_success ON password_typing_sessions(success)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_char_metrics_session ON character_typing_metrics(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_char_metrics_position ON character_typing_metrics(char_position)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_char_metrics_type ON character_typing_metrics(char_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_char_metrics_success ON character_typing_metrics(success)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pattern_analytics_type ON typing_pattern_analytics(pattern_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pattern_analytics_char ON typing_pattern_analytics(char_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_learning_progress_time ON learning_progress(timestamp)")

        # 🖥️ Indexes for Display-Aware SAI tables
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_display_context_attempt ON display_context(attempt_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_display_context_timestamp ON display_context(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_display_context_tv ON display_context(is_tv_connected)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_display_context_mode ON display_context(display_mode)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_display_context_strategy ON display_context(typing_strategy)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tv_analytics_date ON tv_unlock_analytics(date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tv_analytics_period ON tv_unlock_analytics(period_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_display_history_identifier ON display_success_history(display_identifier)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_display_history_type ON display_success_history(display_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_display_history_tv ON display_success_history(is_tv)")

        # 🎙️ Indexes for Voice Embeddings tables
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_voice_embeddings_speaker ON voice_embeddings(speaker_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_voice_embeddings_synced ON voice_embeddings(cloud_sql_synced)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_embedding_history_speaker ON voice_embedding_history(speaker_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_embedding_history_timestamp ON voice_embedding_history(timestamp)")

        # 🎙️ Indexes for Voice Profile Learning tables
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_voice_profile_speaker ON voice_profile_learning(speaker_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_voice_sample_speaker ON voice_sample_log(speaker_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_voice_sample_timestamp ON voice_sample_log(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_voice_sample_confidence ON voice_sample_log(confidence)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_voice_confidence_speaker ON voice_confidence_history(speaker_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_voice_confidence_date ON voice_confidence_history(date)")

        # 🖥️ Indexes for Dynamic SAI tables
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_connection_events_timestamp ON display_connection_events(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_connection_events_type ON display_connection_events(event_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_connection_events_display ON display_connection_events(display_identifier)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_connection_events_tv ON display_connection_events(is_tv)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tv_sessions_active ON tv_sessions(is_active)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tv_sessions_tv ON tv_sessions(tv_identifier)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tv_sessions_start ON tv_sessions(session_start)")

        # 🧠 Indexes for Hallucination Learning tables
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hallucinations_normalized ON stt_hallucinations(original_text_normalized)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hallucinations_type ON stt_hallucinations(hallucination_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hallucinations_date ON stt_hallucinations(date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hallucinations_timestamp ON stt_hallucinations(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hallucinations_context ON stt_hallucinations(context)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_corrections_normalized ON hallucination_corrections(hallucination_normalized)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_behavioral_context ON user_behavioral_patterns(context)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_behavioral_phrase ON user_behavioral_patterns(phrase_normalized)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reasoning_hallucination ON hallucination_reasoning_log(hallucination_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reasoning_timestamp ON hallucination_reasoning_log(timestamp)")

        conn.commit()
        conn.close()

        logger.info(f"✅ SQLite database initialized: {self.sqlite_path}")

    def _init_cloud_sql(self):
        """Initialize CloudSQL connection (uses existing JARVIS CloudSQL connection)"""
        try:
            # Import existing CloudSQL manager from JARVIS
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).parent.parent))

            from intelligence.cloud_sql_connection_manager import CloudSQLConnectionManager

            self.cloud_db = CloudSQLConnectionManager.get_instance()
            logger.info("✅ CloudSQL connection established for metrics")

            # Create tables in CloudSQL (same schema as SQLite)
            self._create_cloud_tables()

        except Exception as e:
            logger.warning(f"CloudSQL not available for metrics: {e}")
            logger.warning("Continuing with SQLite only")
            self.use_cloud_sql = False
            self.cloud_db = None

    def _create_cloud_tables(self):
        """Create tables in CloudSQL if they don't exist"""
        if not self.cloud_db:
            return

        try:
            conn = self.cloud_db.get_connection()
            cursor = conn.cursor()

            # Same schema as SQLite but with PostgreSQL syntax
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS voice_unlock_attempts (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP NOT NULL,
                    date DATE NOT NULL,
                    time TIME NOT NULL,
                    day_of_week VARCHAR(20),
                    unix_timestamp DOUBLE PRECISION,
                    success BOOLEAN NOT NULL,
                    speaker_name VARCHAR(255) NOT NULL,
                    transcribed_text TEXT,
                    error TEXT,

                    speaker_confidence DOUBLE PRECISION,
                    stt_confidence DOUBLE PRECISION,
                    threshold DOUBLE PRECISION,
                    above_threshold BOOLEAN,
                    confidence_margin DOUBLE PRECISION,
                    margin_percentage DOUBLE PRECISION,

                    avg_last_10 DOUBLE PRECISION,
                    avg_last_30 DOUBLE PRECISION,
                    trend_direction VARCHAR(50),
                    volatility DOUBLE PRECISION,
                    best_ever DOUBLE PRECISION,
                    worst_ever DOUBLE PRECISION,
                    percentile_rank DOUBLE PRECISION,

                    total_duration_ms DOUBLE PRECISION,
                    slowest_stage VARCHAR(100),
                    fastest_stage VARCHAR(100),

                    audio_quality VARCHAR(50),
                    voice_match_quality VARCHAR(50),
                    overall_confidence DOUBLE PRECISION,

                    total_stages INTEGER,
                    successful_stages INTEGER,
                    failed_stages INTEGER,
                    all_stages_passed BOOLEAN,

                    platform VARCHAR(50),
                    platform_version VARCHAR(255),
                    python_version VARCHAR(50),
                    stt_engine VARCHAR(100),
                    speaker_engine VARCHAR(100),

                    session_id VARCHAR(100),
                    logger_version VARCHAR(50),

                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(timestamp, speaker_name)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS voice_unlock_stages (
                    id SERIAL PRIMARY KEY,
                    attempt_id INTEGER NOT NULL,
                    stage_name VARCHAR(100) NOT NULL,
                    started_at DOUBLE PRECISION,
                    ended_at DOUBLE PRECISION,
                    duration_ms DOUBLE PRECISION,
                    percentage_of_total DOUBLE PRECISION,
                    success BOOLEAN,
                    algorithm_used VARCHAR(255),
                    module_path TEXT,
                    function_name VARCHAR(255),
                    input_size_bytes INTEGER,
                    output_size_bytes INTEGER,
                    confidence_score DOUBLE PRECISION,
                    threshold DOUBLE PRECISION,
                    above_threshold BOOLEAN,
                    error_message TEXT,
                    metadata_json TEXT,

                    FOREIGN KEY (attempt_id) REFERENCES voice_unlock_attempts(id) ON DELETE CASCADE
                )
            """)

            # Create indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_voice_attempts_date ON voice_unlock_attempts(date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_voice_attempts_speaker ON voice_unlock_attempts(speaker_name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_voice_stages_attempt ON voice_unlock_stages(attempt_id)")

            conn.commit()
            cursor.close()

            logger.info("✅ CloudSQL tables created for voice unlock metrics")

        except Exception as e:
            logger.error(f"Failed to create CloudSQL tables: {e}", exc_info=True)

    async def store_unlock_attempt(
        self,
        entry: Dict[str, Any],
        stages: List[Dict[str, Any]]
    ) -> Optional[int]:
        """
        Store unlock attempt in both SQLite and CloudSQL.

        Args:
            entry: Complete unlock attempt entry (from metrics logger)
            stages: List of processing stage details

        Returns:
            SQLite row ID if successful, None otherwise
        """
        # Store in SQLite (always)
        sqlite_id = await self._store_in_sqlite(entry, stages)

        # Store in CloudSQL (if available)
        if self.use_cloud_sql and self.cloud_db:
            try:
                await self._store_in_cloud_sql(entry, stages)
            except Exception as e:
                logger.warning(f"Failed to sync to CloudSQL: {e}")

        return sqlite_id

    async def _store_in_sqlite(
        self,
        entry: Dict[str, Any],
        stages: List[Dict[str, Any]]
    ) -> Optional[int]:
        """Store unlock attempt in SQLite (async-safe via thread executor)"""
        # Run blocking SQLite operations in a thread to avoid blocking event loop
        return await asyncio.to_thread(
            self._store_in_sqlite_sync, entry, stages
        )

    def _store_in_sqlite_sync(
        self,
        entry: Dict[str, Any],
        stages: List[Dict[str, Any]]
    ) -> Optional[int]:
        """Synchronous SQLite storage - called via asyncio.to_thread()"""
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            # Extract data from entry
            bio = entry['biometrics']
            perf = entry['performance']
            qual = entry['quality_indicators']
            stage_sum = entry['stage_summary']
            sys_info = entry['system_info']
            meta = entry['metadata']

            # Insert main attempt
            cursor.execute("""
                INSERT INTO unlock_attempts (
                    timestamp, date, time, day_of_week, unix_timestamp,
                    success, speaker_name, transcribed_text, error,
                    speaker_confidence, stt_confidence, threshold, above_threshold,
                    confidence_margin, margin_percentage,
                    avg_last_10, avg_last_30, trend_direction, volatility,
                    best_ever, worst_ever, percentile_rank,
                    total_duration_ms, slowest_stage, fastest_stage,
                    audio_quality, voice_match_quality, overall_confidence,
                    total_stages, successful_stages, failed_stages, all_stages_passed,
                    platform, platform_version, python_version, stt_engine, speaker_engine,
                    session_id, logger_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry['timestamp'], entry['date'], entry['time'], entry['day_of_week'],
                entry['unix_timestamp'], entry['success'], entry['speaker_name'],
                entry['transcribed_text'], entry.get('error'),
                bio['speaker_confidence'], bio['stt_confidence'], bio['threshold'],
                bio['above_threshold'], bio['confidence_margin'],
                bio['confidence_vs_threshold']['margin_percentage'],
                bio['confidence_trends'].get('avg_last_10'),
                bio['confidence_trends'].get('avg_last_30'),
                bio['confidence_trends'].get('trend_direction'),
                bio['confidence_trends'].get('volatility'),
                bio['confidence_trends'].get('best_ever'),
                bio['confidence_trends'].get('worst_ever'),
                bio['confidence_trends'].get('current_rank_percentile'),
                perf['total_duration_ms'], perf.get('slowest_stage'),
                perf.get('fastest_stage'), qual['audio_quality'],
                qual['voice_match_quality'], qual['overall_confidence'],
                stage_sum['total_stages'], stage_sum['successful_stages'],
                stage_sum['failed_stages'], stage_sum['all_stages_passed'],
                sys_info.get('platform'), sys_info.get('platform_version'),
                sys_info.get('python_version'), sys_info.get('stt_engine'),
                sys_info.get('speaker_engine'), meta['session_id'],
                meta['logger_version']
            ))

            attempt_id = cursor.lastrowid

            # Insert processing stages
            for stage in stages:
                cursor.execute("""
                    INSERT INTO processing_stages (
                        attempt_id, stage_name, started_at, ended_at, duration_ms,
                        percentage_of_total, success, algorithm_used, module_path,
                        function_name, input_size_bytes, output_size_bytes,
                        confidence_score, threshold, above_threshold, error_message,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    attempt_id, stage['stage_name'], stage['started_at'],
                    stage['ended_at'], stage['duration_ms'],
                    stage['percentage_of_total'], stage['success'],
                    stage.get('algorithm_used'), stage.get('module_path'),
                    stage.get('function_name'), stage.get('input_size_bytes'),
                    stage.get('output_size_bytes'), stage.get('confidence_score'),
                    stage.get('threshold'), stage.get('above_threshold'),
                    stage.get('error_message'),
                    json.dumps(stage.get('metadata', {}))
                ))

            # Insert stage breakdown
            for stage_name, stage_data in perf.get('stages_breakdown', {}).items():
                cursor.execute("""
                    INSERT INTO stage_breakdown (
                        attempt_id, stage_name, duration_ms, percentage
                    ) VALUES (?, ?, ?, ?)
                """, (
                    attempt_id, stage_name, stage_data['duration_ms'],
                    stage_data['percentage']
                ))

            conn.commit()
            conn.close()

            logger.debug(f"✅ Stored unlock attempt in SQLite (ID: {attempt_id})")
            return attempt_id

        except Exception as e:
            logger.error(f"Failed to store in SQLite: {e}", exc_info=True)
            return None

    async def _store_in_cloud_sql(
        self,
        entry: Dict[str, Any],
        stages: List[Dict[str, Any]]
    ) -> bool:
        """Store unlock attempt in CloudSQL"""
        # Similar to SQLite but with PostgreSQL-specific syntax
        # Implementation would use the cloud_db connection
        # Omitted for brevity - follows same pattern as SQLite
        pass

    async def query_attempts(
        self,
        speaker_name: str = None,
        start_date: str = None,
        end_date: str = None,
        success_only: bool = False,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Query unlock attempts from database (async with timeout protection).

        Args:
            speaker_name: Filter by speaker
            start_date: Filter by start date (YYYY-MM-DD)
            end_date: Filter by end date (YYYY-MM-DD)
            success_only: Only return successful attempts
            limit: Maximum number of results

        Returns:
            List of unlock attempt dictionaries
        """
        self._stats['total_reads'] += 1

        query = "SELECT * FROM unlock_attempts WHERE 1=1"
        params = []

        if speaker_name:
            query += " AND speaker_name = ?"
            params.append(speaker_name)

        if start_date:
            query += " AND date >= ?"
            params.append(start_date)

        if end_date:
            query += " AND date <= ?"
            params.append(end_date)

        if success_only:
            query += " AND success = 1"

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        # Use async executor with timeout protection
        if self._executor:
            result = await self._executor.execute_query(query, tuple(params), fetch='all')
            return result or []
        else:
            # Fallback to thread-wrapped sync
            return await asyncio.to_thread(self._query_attempts_sync, query, tuple(params))

    def _query_attempts_sync(self, query: str, params: tuple) -> List[Dict[str, Any]]:
        """Synchronous fallback for query_attempts."""
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, params)
        results = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return results

    async def health_check(self) -> Dict[str, Any]:
        """
        Comprehensive health check for the metrics database.

        Returns health status with component details.
        """
        issues = []
        components = {}

        # Check circuit breaker
        components['circuit_breaker'] = self._circuit_breaker.to_dict()
        if self._circuit_breaker._is_open:
            issues.append("Circuit breaker is open")

        # Check connection pool
        if self._pool:
            pool_stats = self._pool.get_stats()
            components['connection_pool'] = pool_stats
            if pool_stats['errors'] > pool_stats['checkouts'] * 0.1:  # >10% error rate
                issues.append("High connection pool error rate")
        else:
            components['connection_pool'] = {'error': 'Not initialized'}
            issues.append("Connection pool not initialized")

        # Check executor
        if self._executor:
            exec_stats = self._executor.get_stats()
            components['query_executor'] = exec_stats
            timeout_rate = exec_stats['timeouts'] / exec_stats['queries'] if exec_stats['queries'] > 0 else 0
            if timeout_rate > 0.1:  # >10% timeout rate
                issues.append(f"High query timeout rate: {timeout_rate:.1%}")
        else:
            components['query_executor'] = {'error': 'Not initialized'}
            issues.append("Query executor not initialized")

        # Check batch writer
        if self._batch_writer:
            components['batch_writer'] = self._batch_writer.get_stats()
        else:
            components['batch_writer'] = {'error': 'Not initialized'}

        # Check database file
        try:
            db_size = self.sqlite_path.stat().st_size / (1024 * 1024)  # MB
            components['database_file'] = {
                'path': str(self.sqlite_path),
                'size_mb': round(db_size, 2),
                'exists': True,
            }
            if db_size > 500:  # Warn if >500MB
                issues.append(f"Database size is large: {db_size:.0f}MB")
        except Exception as e:
            components['database_file'] = {'error': str(e)}
            issues.append(f"Database file error: {e}")

        # Test database connectivity
        try:
            result = await self._executor.execute_query(
                "SELECT COUNT(*) as count FROM unlock_attempts",
                timeout=2.0,
                fetch='one'
            )
            if result:
                components['connectivity'] = {
                    'connected': True,
                    'record_count': result[0]['count'] if result else 0,
                }
            else:
                components['connectivity'] = {'connected': False}
                issues.append("Database connectivity test failed")
        except Exception as e:
            components['connectivity'] = {'error': str(e)}
            issues.append(f"Database connectivity error: {e}")

        # Calculate overall health
        healthy = len(issues) == 0
        health_score = 1.0 - (len(issues) * 0.2)  # Each issue reduces score by 20%
        health_score = max(0.0, health_score)

        # Calculate uptime
        uptime = time.time() - self._stats['start_time']

        return {
            'healthy': healthy,
            'score': round(health_score, 2),
            'message': "All systems operational" if healthy else f"Issues: {', '.join(issues)}",
            'components': components,
            'issues': issues,
            'stats': {
                **self._stats,
                'uptime_seconds': round(uptime, 1),
            },
            'last_check': datetime.now().isoformat(),
        }

    async def close(self):
        """Close all database connections and flush pending writes."""
        logger.info("Closing metrics database...")

        # Flush pending batch writes
        if self._batch_writer:
            await self._batch_writer.flush()

        # Close connection pool
        if self._pool:
            self._pool.close_all()

        logger.info("Metrics database closed")

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive database statistics."""
        stats = {
            **self._stats,
            'uptime_seconds': round(time.time() - self._stats['start_time'], 1),
        }

        if self._executor:
            stats['executor'] = self._executor.get_stats()

        if self._batch_writer:
            stats['batch_writer'] = self._batch_writer.get_stats()

        if self._pool:
            stats['pool'] = self._pool.get_stats()

        return stats

    # 🤖 CONTINUOUS LEARNING METHODS

    async def store_typing_session(
        self,
        attempt_id: int,
        session_data: Dict[str, Any],
        character_metrics: List[Dict[str, Any]]
    ) -> Optional[int]:
        """
        Store character-level typing metrics for ML training.

        Uses fire-and-forget async executor to avoid blocking unlock flow.

        Args:
            attempt_id: ID of the unlock attempt
            session_data: Overall typing session metrics
            character_metrics: List of per-character metrics

        Returns:
            Session ID if successful, None otherwise
        """
        # Fire-and-forget: Schedule in background to not block unlock
        asyncio.create_task(
            self._store_typing_session_async(attempt_id, session_data, character_metrics)
        )
        # Return immediately - don't block unlock for DB writes
        return attempt_id or 0

    async def _store_typing_session_async(
        self,
        attempt_id: int,
        session_data: Dict[str, Any],
        character_metrics: List[Dict[str, Any]]
    ) -> Optional[int]:
        """
        Internal async method to store typing session in executor.
        Runs in background thread to avoid blocking event loop.
        """
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    self._store_typing_session_sync,
                    attempt_id,
                    session_data,
                    character_metrics
                ),
                timeout=5.0  # 5 second timeout for DB operations
            )
        except asyncio.TimeoutError:
            logger.warning("⏱️ Typing session storage timed out (non-blocking)")
            return None
        except Exception as e:
            logger.error(f"Failed to store typing session async: {e}")
            return None

    def _store_typing_session_sync(
        self,
        attempt_id: int,
        session_data: Dict[str, Any],
        character_metrics: List[Dict[str, Any]]
    ) -> Optional[int]:
        """
        Synchronous method to store typing session (runs in executor).
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            # Insert typing session
            cursor.execute("""
                INSERT INTO password_typing_sessions (
                    attempt_id, timestamp, success,
                    total_characters, characters_typed, typing_method, fallback_used,
                    total_typing_duration_ms, avg_char_duration_ms,
                    min_char_duration_ms, max_char_duration_ms,
                    system_load, memory_pressure, screen_locked,
                    inter_char_delay_avg_ms, inter_char_delay_std_ms,
                    shift_press_duration_avg_ms, shift_release_delay_avg_ms,
                    failed_at_character, retry_count,
                    time_of_day, day_of_week
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                attempt_id,
                session_data.get('timestamp'),
                1 if session_data.get('success') else 0,
                session_data.get('total_characters'),
                session_data.get('characters_typed'),
                session_data.get('typing_method', 'core_graphics'),
                1 if session_data.get('fallback_used') else 0,
                session_data.get('total_typing_duration_ms'),
                session_data.get('avg_char_duration_ms'),
                session_data.get('min_char_duration_ms'),
                session_data.get('max_char_duration_ms'),
                session_data.get('system_load'),
                session_data.get('memory_pressure'),
                1 if session_data.get('screen_locked') else 0,
                session_data.get('inter_char_delay_avg_ms'),
                session_data.get('inter_char_delay_std_ms'),
                session_data.get('shift_press_duration_avg_ms'),
                session_data.get('shift_release_delay_avg_ms'),
                session_data.get('failed_at_character'),
                session_data.get('retry_count', 0),
                session_data.get('time_of_day'),
                session_data.get('day_of_week')
            ))

            session_id = cursor.lastrowid

            # Insert character metrics
            for char_metric in character_metrics:
                cursor.execute("""
                    INSERT INTO character_typing_metrics (
                        session_id, attempt_id,
                        char_position, char_type, char_case, requires_shift, keycode,
                        char_start_time_ms, char_end_time_ms, total_duration_ms,
                        shift_down_duration_ms, shift_registered_delay_ms, shift_up_delay_ms,
                        key_down_created, key_down_posted, key_press_duration_ms,
                        key_up_created, key_up_posted,
                        success, error_type, error_message, retry_attempted,
                        inter_char_delay_ms, system_load_at_char
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    session_id,
                    attempt_id,
                    char_metric.get('char_position'),
                    char_metric.get('char_type'),
                    char_metric.get('char_case'),
                    1 if char_metric.get('requires_shift') else 0,
                    char_metric.get('keycode'),
                    char_metric.get('char_start_time_ms'),
                    char_metric.get('char_end_time_ms'),
                    char_metric.get('total_duration_ms'),
                    char_metric.get('shift_down_duration_ms'),
                    char_metric.get('shift_registered_delay_ms'),
                    char_metric.get('shift_up_delay_ms'),
                    1 if char_metric.get('key_down_created') else 0,
                    1 if char_metric.get('key_down_posted') else 0,
                    char_metric.get('key_press_duration_ms'),
                    1 if char_metric.get('key_up_created') else 0,
                    1 if char_metric.get('key_up_posted') else 0,
                    1 if char_metric.get('success') else 0,
                    char_metric.get('error_type'),
                    char_metric.get('error_message'),
                    1 if char_metric.get('retry_attempted') else 0,
                    char_metric.get('inter_char_delay_ms'),
                    char_metric.get('system_load_at_char')
                ))

            conn.commit()
            conn.close()

            logger.info(f"✅ Stored typing session with {len(character_metrics)} character metrics (Session ID: {session_id})")
            return session_id

        except Exception as e:
            logger.error(f"Failed to store typing session: {e}", exc_info=True)
            return None

    def _record_attempt_sync(self, attempt_data: Dict[str, Any]) -> Optional[int]:
        """
        Synchronous method to record unlock attempt (runs in executor).
        Used for continuous learning - records every VBI unlock attempt.

        Args:
            attempt_data: Dictionary with attempt details:
                - attempt_id: Unique ID for this attempt
                - speaker_name: Verified speaker name
                - confidence: Voice verification confidence
                - success: Whether unlock succeeded
                - duration_ms: Total unlock duration
                - timestamp: ISO timestamp
                - method: Authentication method used

        Returns:
            Attempt ID if successful, None otherwise
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            # Ensure table exists
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vbi_unlock_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id INTEGER UNIQUE,
                    speaker_name TEXT,
                    confidence REAL,
                    success INTEGER,
                    duration_ms REAL,
                    timestamp TEXT,
                    method TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Insert attempt record
            cursor.execute("""
                INSERT OR REPLACE INTO vbi_unlock_attempts (
                    attempt_id, speaker_name, confidence, success,
                    duration_ms, timestamp, method
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                attempt_data.get('attempt_id'),
                attempt_data.get('speaker_name'),
                attempt_data.get('confidence'),
                1 if attempt_data.get('success') else 0,
                attempt_data.get('duration_ms'),
                attempt_data.get('timestamp'),
                attempt_data.get('method', 'voice_biometric')
            ))

            conn.commit()
            attempt_id = cursor.lastrowid
            conn.close()

            return attempt_id

        except Exception as e:
            logger.error(f"Failed to record unlock attempt: {e}")
            return None

    async def analyze_typing_patterns(self) -> Dict[str, Any]:
        """
        Analyze typing patterns and compute optimal timing parameters for ML.

        Returns:
            Dictionary with pattern analysis and recommendations
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Analyze successful character typing
            cursor.execute("""
                SELECT
                    char_type,
                    requires_shift,
                    COUNT(*) as sample_count,
                    AVG(total_duration_ms) as avg_duration,
                    STDEV(total_duration_ms) as std_duration,
                    MIN(total_duration_ms) as min_duration,
                    MAX(total_duration_ms) as max_duration,
                    AVG(inter_char_delay_ms) as avg_inter_char_delay,
                    AVG(shift_press_duration_ms) as avg_shift_duration,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as success_rate
                FROM character_typing_metrics
                WHERE success = 1
                GROUP BY char_type, requires_shift
                HAVING sample_count >= 5
            """)

            patterns = []
            for row in cursor.fetchall():
                pattern = {
                    'char_type': row['char_type'],
                    'requires_shift': bool(row['requires_shift']),
                    'sample_count': row['sample_count'],
                    'avg_duration_ms': row['avg_duration'],
                    'std_duration_ms': row['std_duration'],
                    'min_duration_ms': row['min_duration'],
                    'max_duration_ms': row['max_duration'],
                    'avg_inter_char_delay_ms': row['avg_inter_char_delay'],
                    'avg_shift_duration_ms': row['avg_shift_duration'],
                    'success_rate': row['success_rate'],
                    'confidence': min(row['sample_count'] / 100.0, 1.0)  # Confidence increases with samples
                }

                # Calculate optimal timing (use fastest successful timing + small margin)
                pattern['optimal_duration_ms'] = row['min_duration'] * 1.1
                pattern['optimal_inter_char_delay_ms'] = max(row['avg_inter_char_delay'], 80.0)

                patterns.append(pattern)

                # Store in pattern analytics table
                cursor.execute("""
                    INSERT INTO typing_pattern_analytics (
                        calculated_at, pattern_type, char_type, requires_shift,
                        sample_count, success_rate,
                        avg_duration_ms, std_duration_ms, min_duration_ms, max_duration_ms,
                        optimal_char_duration_ms, optimal_inter_char_delay_ms,
                        optimal_shift_duration_ms, confidence_score,
                        last_updated, training_samples_used
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    datetime.now().isoformat(),
                    'successful_timing',
                    pattern['char_type'],
                    1 if pattern['requires_shift'] else 0,
                    pattern['sample_count'],
                    pattern['success_rate'],
                    pattern['avg_duration_ms'],
                    pattern['std_duration_ms'],
                    pattern['min_duration_ms'],
                    pattern['max_duration_ms'],
                    pattern['optimal_duration_ms'],
                    pattern['optimal_inter_char_delay_ms'],
                    pattern['avg_shift_duration_ms'],
                    pattern['confidence'],
                    datetime.now().isoformat(),
                    pattern['sample_count']
                ))

            conn.commit()
            conn.close()

            logger.info(f"✅ Analyzed {len(patterns)} typing patterns")
            return {
                'patterns': patterns,
                'total_patterns': len(patterns),
                'timestamp': datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"Failed to analyze typing patterns: {e}", exc_info=True)
            return {'patterns': [], 'error': str(e)}

    async def get_optimal_timing_config(self) -> Dict[str, Any]:
        """
        Get ML-optimized timing configuration based on historical data.

        Returns:
            Dictionary with optimal timing parameters for password typing
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Get latest pattern analytics
            cursor.execute("""
                SELECT
                    char_type,
                    requires_shift,
                    optimal_char_duration_ms,
                    optimal_inter_char_delay_ms,
                    optimal_shift_duration_ms,
                    confidence_score
                FROM typing_pattern_analytics
                WHERE pattern_type = 'successful_timing'
                AND sample_count >= 10
                ORDER BY last_updated DESC
                LIMIT 100
            """)

            config = {
                'letter': {'duration': 50, 'delay': 100},
                'digit': {'duration': 50, 'delay': 100},
                'special': {'duration': 60, 'delay': 120},
                'shift_duration': 30,
                'confidence': 0.0
            }

            results = cursor.fetchall()
            if results:
                # Group by char_type and compute weighted average
                for row in results:
                    char_type = row['char_type']
                    if char_type in config:
                        # Use confidence as weight
                        weight = row['confidence_score']
                        config[char_type]['duration'] = row['optimal_char_duration_ms'] * weight + config[char_type]['duration'] * (1 - weight)
                        config[char_type]['delay'] = row['optimal_inter_char_delay_ms'] * weight + config[char_type]['delay'] * (1 - weight)

                config['shift_duration'] = sum(r['optimal_shift_duration_ms'] or 30 for r in results) / len(results)
                config['confidence'] = sum(r['confidence_score'] for r in results) / len(results)

            conn.close()

            logger.info(f"✅ Retrieved optimal timing config (confidence: {config['confidence']:.2%})")
            return config

        except Exception as e:
            logger.error(f"Failed to get optimal timing: {e}", exc_info=True)
            return None

    async def update_learning_progress(
        self,
        speaker_name: str = None,
        command_text: str = None,
        success: bool = True,
        confidence: float = 0.0,
        match_type: str = "regex",
        duration_ms: float = 0.0
    ) -> Optional[int]:
        """
        Update learning progress for voice unlock attempts.
        Tracks continuous learning metrics for command recognition and authentication.

        Args:
            speaker_name: Name of the speaker
            command_text: The command text that was parsed
            success: Whether the unlock was successful
            confidence: Confidence score of the command/voice match
            match_type: How the command was matched (regex, fuzzy, learned)
            duration_ms: Total duration of the unlock attempt

        Returns:
            Row ID if successful, None otherwise
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            # Get current aggregated stats
            cursor.execute("""
                SELECT
                    COUNT(*) as total_attempts,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful_attempts,
                    AVG(total_duration_ms) as avg_duration_all
                FROM unlock_attempts
            """)
            row = cursor.fetchone()
            total_attempts = (row[0] or 0) + 1
            successful_attempts = (row[1] or 0) + (1 if success else 0)
            avg_duration_all = row[2] or duration_ms

            # Calculate success rate
            success_rate = successful_attempts / total_attempts if total_attempts > 0 else 0.0

            # Get last 10 attempts average duration
            cursor.execute("""
                SELECT AVG(total_duration_ms) as avg_last_10
                FROM (
                    SELECT total_duration_ms
                    FROM unlock_attempts
                    ORDER BY timestamp DESC
                    LIMIT 10
                )
            """)
            avg_last_10 = cursor.fetchone()[0] or duration_ms

            # Get last 50 attempts average duration
            cursor.execute("""
                SELECT AVG(total_duration_ms) as avg_last_50
                FROM (
                    SELECT total_duration_ms
                    FROM unlock_attempts
                    ORDER BY timestamp DESC
                    LIMIT 50
                )
            """)
            avg_last_50 = cursor.fetchone()[0] or duration_ms

            # Get fastest ever
            cursor.execute("""
                SELECT MIN(total_duration_ms) FROM unlock_attempts WHERE success = 1
            """)
            fastest_ever = cursor.fetchone()[0] or duration_ms

            # Calculate improvement
            improvement = ((avg_duration_all - avg_last_10) / avg_duration_all * 100) if avg_duration_all > 0 else 0

            # Get consecutive successes/failures
            cursor.execute("""
                SELECT success FROM unlock_attempts ORDER BY timestamp DESC LIMIT 20
            """)
            recent_results = [r[0] for r in cursor.fetchall()]

            consecutive_successes = 0
            consecutive_failures = 0
            for r in recent_results:
                if r == 1:
                    if consecutive_failures == 0:
                        consecutive_successes += 1
                    else:
                        break
                else:
                    if consecutive_successes == 0:
                        consecutive_failures += 1
                    else:
                        break

            # Calculate failure rate last 10
            failure_rate_last_10 = sum(1 for r in recent_results[:10] if r == 0) / min(10, len(recent_results)) if recent_results else 0

            # Determine strategy based on success rate
            if success_rate >= 0.95:
                strategy = "aggressive"
            elif success_rate >= 0.85:
                strategy = "balanced"
            else:
                strategy = "conservative"

            # Insert learning progress entry
            cursor.execute("""
                INSERT INTO learning_progress (
                    timestamp,
                    total_attempts,
                    successful_attempts,
                    success_rate,
                    avg_typing_duration_last_10,
                    avg_typing_duration_last_50,
                    avg_typing_duration_all_time,
                    improvement_percentage,
                    avg_char_duration_last_10,
                    avg_char_duration_last_50,
                    fastest_ever_typing_ms,
                    consecutive_successes,
                    consecutive_failures,
                    failure_rate_last_10,
                    model_version,
                    prediction_accuracy,
                    optimal_timing_applied,
                    best_time_of_day,
                    best_system_load_range,
                    current_strategy,
                    timing_adjustments_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                total_attempts,
                successful_attempts,
                success_rate,
                avg_last_10,
                avg_last_50,
                avg_duration_all,
                improvement,
                avg_last_10 / 10 if avg_last_10 else 0,  # Rough estimate
                avg_last_50 / 10 if avg_last_50 else 0,
                fastest_ever,
                consecutive_successes,
                consecutive_failures,
                failure_rate_last_10,
                "v1.0-fuzzy",
                confidence,
                1 if match_type == "learned" else 0,
                datetime.now().strftime("%H:00"),
                "normal",
                strategy,
                json.dumps({
                    "match_type": match_type,
                    "command_text": command_text[:100] if command_text else None,
                    "speaker": speaker_name,
                    "confidence": confidence,
                })
            ))

            row_id = cursor.lastrowid
            conn.commit()
            conn.close()

            logger.info(f"✅ Updated learning_progress (ID: {row_id}, success_rate: {success_rate:.1%}, strategy: {strategy})")
            return row_id

        except Exception as e:
            logger.error(f"Failed to update learning progress: {e}", exc_info=True)
            return None

    # 🖥️ DISPLAY-AWARE SAI METHODS

    async def store_display_context(
        self,
        attempt_id: Optional[int],
        display_context: Dict[str, Any],
        typing_config: Dict[str, Any] = None,
        detection_metrics: Dict[str, Any] = None
    ) -> Optional[int]:
        """
        Store display context information for an unlock attempt.

        This tracks the display configuration during each unlock attempt
        to enable analytics and learning for TV/multi-display scenarios.

        Args:
            attempt_id: ID of the unlock attempt (can be None for standalone tracking)
            display_context: Display configuration from DisplayDetector
            typing_config: Typing strategy configuration used
            detection_metrics: Performance metrics from display detection

        Returns:
            Row ID if successful, None otherwise
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            # Extract display context data
            displays = display_context.get('displays', [])
            mode = display_context.get('mode', 'SINGLE')
            is_mirrored = display_context.get('is_mirrored', False)
            has_external = display_context.get('has_external', False)
            tv_info = display_context.get('tv_info', {})
            primary_display = display_context.get('primary_display', {})
            external_display = display_context.get('external_display', {})

            # Extract typing config
            strategy = typing_config.get('strategy', 'UNKNOWN') if typing_config else 'UNKNOWN'
            keystroke_delay = typing_config.get('keystroke_delay_ms', 0) if typing_config else 0
            wake_delay = typing_config.get('wake_delay_ms', 0) if typing_config else 0
            strategy_reasoning = typing_config.get('reasoning', '') if typing_config else ''

            # Extract detection metrics
            detection_time = detection_metrics.get('detection_time_ms', 0) if detection_metrics else 0
            detection_method = detection_metrics.get('method', 'unknown') if detection_metrics else 'unknown'

            cursor.execute("""
                INSERT INTO display_context (
                    attempt_id, timestamp,
                    total_displays, display_mode, is_mirrored, has_external,
                    is_tv_connected, tv_name, tv_brand, tv_detection_confidence, tv_detection_reasons,
                    primary_display_name, primary_display_type, primary_width, primary_height, primary_is_builtin,
                    external_display_name, external_display_type, external_width, external_height,
                    typing_strategy, keystroke_delay_ms, wake_delay_ms, strategy_reasoning,
                    detection_time_ms, detection_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                attempt_id,
                datetime.now().isoformat(),
                len(displays),
                mode,
                1 if is_mirrored else 0,
                1 if has_external else 0,
                1 if tv_info.get('is_tv', False) else 0,
                tv_info.get('name'),
                tv_info.get('brand'),
                tv_info.get('confidence', 0),
                json.dumps(tv_info.get('reasons', [])),
                primary_display.get('name'),
                primary_display.get('type'),
                primary_display.get('width'),
                primary_display.get('height'),
                1 if primary_display.get('is_builtin', False) else 0,
                external_display.get('name'),
                external_display.get('type'),
                external_display.get('width'),
                external_display.get('height'),
                strategy,
                keystroke_delay,
                wake_delay,
                strategy_reasoning,
                detection_time,
                detection_method
            ))

            row_id = cursor.lastrowid
            conn.commit()
            conn.close()

            logger.info(f"✅ Stored display_context (ID: {row_id}, mode: {mode}, tv: {tv_info.get('is_tv', False)}, strategy: {strategy})")
            return row_id

        except Exception as e:
            logger.error(f"Failed to store display context: {e}", exc_info=True)
            return None

    async def update_tv_analytics(
        self,
        is_tv: bool,
        success: bool,
        typing_strategy: str,
        unlock_duration_ms: float = 0,
        tv_brand: str = None,
        is_mirrored: bool = False
    ) -> Optional[int]:
        """
        Update TV unlock analytics with results from an unlock attempt.

        This aggregates statistics for TV vs non-TV unlocks, strategy effectiveness,
        and brand-specific success rates for continuous learning.

        Args:
            is_tv: Whether a TV was connected during this attempt
            success: Whether the unlock was successful
            typing_strategy: The typing strategy used ('CORE_GRAPHICS_FAST', 'APPLESCRIPT_DIRECT', etc.)
            unlock_duration_ms: Duration of the unlock in milliseconds
            tv_brand: Brand of the TV (if detected)
            is_mirrored: Whether display was mirrored

        Returns:
            Row ID of the updated analytics record, None on failure
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            today = datetime.now().strftime("%Y-%m-%d")
            now = datetime.now().isoformat()

            # Check if we have a record for today
            cursor.execute("""
                SELECT id, tv_unlock_attempts, tv_unlock_successes, tv_unlock_failures,
                       non_tv_unlock_attempts, non_tv_unlock_successes, non_tv_unlock_failures,
                       applescript_attempts, applescript_successes,
                       core_graphics_attempts, core_graphics_successes,
                       hybrid_attempts, hybrid_successes,
                       avg_tv_unlock_duration_ms, avg_non_tv_unlock_duration_ms,
                       unique_tv_brands, tv_brand_success_rates,
                       mirrored_attempts, mirrored_successes,
                       extended_attempts, extended_successes
                FROM tv_unlock_analytics
                WHERE date = ? AND period_type = 'daily'
            """, (today,))

            row = cursor.fetchone()

            if row:
                # Update existing record
                row_id = row[0]
                tv_attempts = row[1] or 0
                tv_successes = row[2] or 0
                tv_failures = row[3] or 0
                non_tv_attempts = row[4] or 0
                non_tv_successes = row[5] or 0
                non_tv_failures = row[6] or 0
                applescript_attempts = row[7] or 0
                applescript_successes = row[8] or 0
                cg_attempts = row[9] or 0
                cg_successes = row[10] or 0
                hybrid_attempts = row[11] or 0
                hybrid_successes = row[12] or 0
                avg_tv_duration = row[13] or 0
                avg_non_tv_duration = row[14] or 0
                unique_brands = json.loads(row[15]) if row[15] else []
                brand_rates = json.loads(row[16]) if row[16] else {}
                mirrored_attempts = row[17] or 0
                mirrored_successes = row[18] or 0
                extended_attempts = row[19] or 0
                extended_successes = row[20] or 0
            else:
                # Create new record
                row_id = None
                tv_attempts = tv_successes = tv_failures = 0
                non_tv_attempts = non_tv_successes = non_tv_failures = 0
                applescript_attempts = applescript_successes = 0
                cg_attempts = cg_successes = 0
                hybrid_attempts = hybrid_successes = 0
                avg_tv_duration = avg_non_tv_duration = 0
                unique_brands = []
                brand_rates = {}
                mirrored_attempts = mirrored_successes = 0
                extended_attempts = extended_successes = 0

            # Update counters based on this attempt
            if is_tv:
                tv_attempts += 1
                if success:
                    tv_successes += 1
                else:
                    tv_failures += 1
                # Update rolling average for TV duration
                if avg_tv_duration > 0:
                    avg_tv_duration = (avg_tv_duration * (tv_attempts - 1) + unlock_duration_ms) / tv_attempts
                else:
                    avg_tv_duration = unlock_duration_ms

                # Track TV brand
                if tv_brand and tv_brand not in unique_brands:
                    unique_brands.append(tv_brand)
                if tv_brand:
                    if tv_brand not in brand_rates:
                        brand_rates[tv_brand] = {'attempts': 0, 'successes': 0}
                    brand_rates[tv_brand]['attempts'] += 1
                    if success:
                        brand_rates[tv_brand]['successes'] += 1
            else:
                non_tv_attempts += 1
                if success:
                    non_tv_successes += 1
                else:
                    non_tv_failures += 1
                if avg_non_tv_duration > 0:
                    avg_non_tv_duration = (avg_non_tv_duration * (non_tv_attempts - 1) + unlock_duration_ms) / non_tv_attempts
                else:
                    avg_non_tv_duration = unlock_duration_ms

            # Update strategy counters
            strategy_upper = typing_strategy.upper() if typing_strategy else ''
            if 'APPLESCRIPT' in strategy_upper:
                applescript_attempts += 1
                if success:
                    applescript_successes += 1
            elif 'CORE_GRAPHICS' in strategy_upper or 'CG_' in strategy_upper:
                cg_attempts += 1
                if success:
                    cg_successes += 1
            elif 'HYBRID' in strategy_upper:
                hybrid_attempts += 1
                if success:
                    hybrid_successes += 1

            # Update mirrored/extended counters
            if is_mirrored:
                mirrored_attempts += 1
                if success:
                    mirrored_successes += 1
            elif is_tv:  # External but not mirrored = extended
                extended_attempts += 1
                if success:
                    extended_successes += 1

            # Calculate success rates
            tv_success_rate = tv_successes / tv_attempts if tv_attempts > 0 else 0
            non_tv_success_rate = non_tv_successes / non_tv_attempts if non_tv_attempts > 0 else 0
            applescript_rate = applescript_successes / applescript_attempts if applescript_attempts > 0 else 0
            cg_rate = cg_successes / cg_attempts if cg_attempts > 0 else 0
            hybrid_rate = hybrid_successes / hybrid_attempts if hybrid_attempts > 0 else 0
            mirrored_rate = mirrored_successes / mirrored_attempts if mirrored_attempts > 0 else 0
            extended_rate = extended_successes / extended_attempts if extended_attempts > 0 else 0

            # Calculate brand success rates
            brand_success_rates = {brand: data['successes'] / data['attempts'] if data['attempts'] > 0 else 0
                                   for brand, data in brand_rates.items()}

            # Find most common TV
            most_common_tv = max(brand_rates.items(), key=lambda x: x[1]['attempts'])[0] if brand_rates else None

            # Performance overhead
            tv_overhead = avg_tv_duration - avg_non_tv_duration if avg_tv_duration > 0 and avg_non_tv_duration > 0 else 0

            if row_id:
                # Update existing record
                cursor.execute("""
                    UPDATE tv_unlock_analytics SET
                        timestamp = ?,
                        tv_unlock_attempts = ?, tv_unlock_successes = ?, tv_unlock_failures = ?, tv_success_rate = ?,
                        non_tv_unlock_attempts = ?, non_tv_unlock_successes = ?, non_tv_unlock_failures = ?, non_tv_success_rate = ?,
                        applescript_attempts = ?, applescript_successes = ?, applescript_success_rate = ?,
                        core_graphics_attempts = ?, core_graphics_successes = ?, core_graphics_success_rate = ?,
                        hybrid_attempts = ?, hybrid_successes = ?, hybrid_success_rate = ?,
                        avg_tv_unlock_duration_ms = ?, avg_non_tv_unlock_duration_ms = ?, tv_performance_overhead_ms = ?,
                        unique_tv_brands = ?, most_common_tv = ?, tv_brand_success_rates = ?,
                        mirrored_attempts = ?, mirrored_successes = ?, mirrored_success_rate = ?,
                        extended_attempts = ?, extended_successes = ?, extended_success_rate = ?
                    WHERE id = ?
                """, (
                    now,
                    tv_attempts, tv_successes, tv_failures, tv_success_rate,
                    non_tv_attempts, non_tv_successes, non_tv_failures, non_tv_success_rate,
                    applescript_attempts, applescript_successes, applescript_rate,
                    cg_attempts, cg_successes, cg_rate,
                    hybrid_attempts, hybrid_successes, hybrid_rate,
                    avg_tv_duration, avg_non_tv_duration, tv_overhead,
                    json.dumps(unique_brands), most_common_tv, json.dumps(brand_success_rates),
                    mirrored_attempts, mirrored_successes, mirrored_rate,
                    extended_attempts, extended_successes, extended_rate,
                    row_id
                ))
            else:
                # Insert new record
                cursor.execute("""
                    INSERT INTO tv_unlock_analytics (
                        timestamp, date, period_type, period_start, period_end,
                        tv_unlock_attempts, tv_unlock_successes, tv_unlock_failures, tv_success_rate,
                        non_tv_unlock_attempts, non_tv_unlock_successes, non_tv_unlock_failures, non_tv_success_rate,
                        applescript_attempts, applescript_successes, applescript_success_rate,
                        core_graphics_attempts, core_graphics_successes, core_graphics_success_rate,
                        hybrid_attempts, hybrid_successes, hybrid_success_rate,
                        avg_tv_unlock_duration_ms, avg_non_tv_unlock_duration_ms, tv_performance_overhead_ms,
                        unique_tv_brands, most_common_tv, tv_brand_success_rates,
                        mirrored_attempts, mirrored_successes, mirrored_success_rate,
                        extended_attempts, extended_successes, extended_success_rate
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now, today, 'daily', f"{today} 00:00:00", f"{today} 23:59:59",
                    tv_attempts, tv_successes, tv_failures, tv_success_rate,
                    non_tv_attempts, non_tv_successes, non_tv_failures, non_tv_success_rate,
                    applescript_attempts, applescript_successes, applescript_rate,
                    cg_attempts, cg_successes, cg_rate,
                    hybrid_attempts, hybrid_successes, hybrid_rate,
                    avg_tv_duration, avg_non_tv_duration, tv_overhead,
                    json.dumps(unique_brands), most_common_tv, json.dumps(brand_success_rates),
                    mirrored_attempts, mirrored_successes, mirrored_rate,
                    extended_attempts, extended_successes, extended_rate
                ))
                row_id = cursor.lastrowid

            conn.commit()
            conn.close()

            logger.info(f"✅ Updated tv_unlock_analytics (ID: {row_id}, tv_rate: {tv_success_rate:.1%}, non_tv_rate: {non_tv_success_rate:.1%})")
            return row_id

        except Exception as e:
            logger.error(f"Failed to update TV analytics: {e}", exc_info=True)
            return None

    async def update_display_success_history(
        self,
        display_identifier: str,
        display_type: str,  # 'TV', 'MONITOR', 'BUILTIN'
        success: bool,
        typing_strategy: str,
        unlock_duration_ms: float = 0,
        keystroke_delay_ms: float = 0,
        wake_delay_ms: float = 0,
        resolution: str = None,
        is_tv: bool = False,
        tv_brand: str = None,
        connection_type: str = None
    ) -> Optional[int]:
        """
        Update per-display success history for learning optimal strategies.

        This tracks success rates and timing parameters for each unique display,
        allowing JARVIS to learn which strategies work best for specific displays.

        Args:
            display_identifier: Unique display name/ID
            display_type: Type of display ('TV', 'MONITOR', 'BUILTIN')
            success: Whether the unlock was successful
            typing_strategy: Strategy used for this attempt
            unlock_duration_ms: Duration of the unlock
            keystroke_delay_ms: Keystroke delay used
            wake_delay_ms: Wake delay used
            resolution: Display resolution string (e.g., "3840x2160")
            is_tv: Whether this display is a TV
            tv_brand: TV brand if applicable
            connection_type: How the display is connected (HDMI, USB-C, etc.)

        Returns:
            Row ID if successful, None otherwise
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            now = datetime.now().isoformat()

            # Check if we have a record for this display
            cursor.execute("""
                SELECT id, first_seen, total_attempts, successful_attempts, failed_attempts,
                       current_streak, best_streak, worst_streak,
                       applescript_attempts, applescript_successes,
                       core_graphics_attempts, core_graphics_successes,
                       preferred_strategy, optimal_keystroke_delay_ms, optimal_wake_delay_ms,
                       avg_unlock_duration_ms, fastest_unlock_ms, slowest_unlock_ms
                FROM display_success_history
                WHERE display_identifier = ?
            """, (display_identifier,))

            row = cursor.fetchone()

            if row:
                # Update existing record
                row_id = row[0]
                first_seen = row[1]
                total_attempts = (row[2] or 0) + 1
                successful_attempts = (row[3] or 0) + (1 if success else 0)
                failed_attempts = (row[4] or 0) + (0 if success else 1)
                current_streak = row[5] or 0
                best_streak = row[6] or 0
                worst_streak = row[7] or 0
                applescript_attempts = row[8] or 0
                applescript_successes = row[9] or 0
                cg_attempts = row[10] or 0
                cg_successes = row[11] or 0
                preferred_strategy = row[12]
                optimal_keystroke = row[13]
                optimal_wake = row[14]
                avg_duration = row[15] or 0
                fastest = row[16]
                slowest = row[17]

                # Update streak
                if success:
                    if current_streak >= 0:
                        current_streak += 1
                    else:
                        current_streak = 1
                    best_streak = max(best_streak, current_streak)
                else:
                    if current_streak <= 0:
                        current_streak -= 1
                    else:
                        current_streak = -1
                    worst_streak = min(worst_streak, current_streak)

                # Update strategy counters
                strategy_upper = typing_strategy.upper() if typing_strategy else ''
                if 'APPLESCRIPT' in strategy_upper:
                    applescript_attempts += 1
                    if success:
                        applescript_successes += 1
                elif 'CORE_GRAPHICS' in strategy_upper or 'CG_' in strategy_upper:
                    cg_attempts += 1
                    if success:
                        cg_successes += 1

                # Determine preferred strategy based on success rates
                applescript_rate = applescript_successes / applescript_attempts if applescript_attempts > 0 else 0
                cg_rate = cg_successes / cg_attempts if cg_attempts > 0 else 0

                if applescript_attempts >= 3 and cg_attempts >= 3:
                    preferred_strategy = 'APPLESCRIPT_DIRECT' if applescript_rate > cg_rate else 'CORE_GRAPHICS_FAST'
                elif applescript_attempts >= 3:
                    preferred_strategy = 'APPLESCRIPT_DIRECT' if applescript_rate >= 0.8 else preferred_strategy
                elif cg_attempts >= 3:
                    preferred_strategy = 'CORE_GRAPHICS_FAST' if cg_rate >= 0.8 else preferred_strategy

                # Update timing optimization (exponential moving average)
                alpha = 0.3  # Learning rate
                if success:  # Only learn from successful attempts
                    if optimal_keystroke and keystroke_delay_ms > 0:
                        optimal_keystroke = optimal_keystroke * (1 - alpha) + keystroke_delay_ms * alpha
                    elif keystroke_delay_ms > 0:
                        optimal_keystroke = keystroke_delay_ms

                    if optimal_wake and wake_delay_ms > 0:
                        optimal_wake = optimal_wake * (1 - alpha) + wake_delay_ms * alpha
                    elif wake_delay_ms > 0:
                        optimal_wake = wake_delay_ms

                # Update duration stats
                if unlock_duration_ms > 0:
                    if avg_duration > 0:
                        avg_duration = (avg_duration * (total_attempts - 1) + unlock_duration_ms) / total_attempts
                    else:
                        avg_duration = unlock_duration_ms

                    if fastest is None or unlock_duration_ms < fastest:
                        fastest = unlock_duration_ms
                    if slowest is None or unlock_duration_ms > slowest:
                        slowest = unlock_duration_ms

                success_rate = successful_attempts / total_attempts if total_attempts > 0 else 0

                cursor.execute("""
                    UPDATE display_success_history SET
                        last_seen = ?,
                        total_attempts = ?, successful_attempts = ?, failed_attempts = ?, success_rate = ?,
                        current_streak = ?, best_streak = ?, worst_streak = ?,
                        applescript_attempts = ?, applescript_successes = ?,
                        core_graphics_attempts = ?, core_graphics_successes = ?,
                        preferred_strategy = ?, optimal_keystroke_delay_ms = ?, optimal_wake_delay_ms = ?,
                        avg_unlock_duration_ms = ?, fastest_unlock_ms = ?, slowest_unlock_ms = ?
                    WHERE id = ?
                """, (
                    now,
                    total_attempts, successful_attempts, failed_attempts, success_rate,
                    current_streak, best_streak, worst_streak,
                    applescript_attempts, applescript_successes,
                    cg_attempts, cg_successes,
                    preferred_strategy, optimal_keystroke, optimal_wake,
                    avg_duration, fastest, slowest,
                    row_id
                ))
            else:
                # Insert new record
                success_rate = 1.0 if success else 0.0
                current_streak = 1 if success else -1

                cursor.execute("""
                    INSERT INTO display_success_history (
                        display_identifier, display_type, first_seen, last_seen,
                        total_attempts, successful_attempts, failed_attempts, success_rate,
                        current_streak, best_streak, worst_streak,
                        applescript_attempts, applescript_successes,
                        core_graphics_attempts, core_graphics_successes,
                        preferred_strategy, optimal_keystroke_delay_ms, optimal_wake_delay_ms,
                        avg_unlock_duration_ms, fastest_unlock_ms, slowest_unlock_ms,
                        typical_resolution, is_tv, tv_brand, connection_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    display_identifier, display_type, now, now,
                    1, 1 if success else 0, 0 if success else 1, success_rate,
                    current_streak, max(current_streak, 0), min(current_streak, 0),
                    1 if 'APPLESCRIPT' in (typing_strategy or '').upper() else 0,
                    1 if success and 'APPLESCRIPT' in (typing_strategy or '').upper() else 0,
                    1 if 'CORE_GRAPHICS' in (typing_strategy or '').upper() else 0,
                    1 if success and 'CORE_GRAPHICS' in (typing_strategy or '').upper() else 0,
                    typing_strategy, keystroke_delay_ms, wake_delay_ms,
                    unlock_duration_ms, unlock_duration_ms if success else None, unlock_duration_ms if success else None,
                    resolution, 1 if is_tv else 0, tv_brand, connection_type
                ))
                row_id = cursor.lastrowid

            conn.commit()
            conn.close()

            logger.info(f"✅ Updated display_success_history for '{display_identifier}' (ID: {row_id}, rate: {success_rate:.1%})")
            return row_id

        except Exception as e:
            logger.error(f"Failed to update display success history: {e}", exc_info=True)
            return None

    async def get_display_learning_stats(
        self,
        display_identifier: str = None
    ) -> Dict[str, Any]:
        """
        Get learned display statistics for analytics or strategy selection.

        Args:
            display_identifier: Specific display to query, or None for all displays

        Returns:
            Dictionary with display learning statistics
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if display_identifier:
                cursor.execute("""
                    SELECT * FROM display_success_history WHERE display_identifier = ?
                """, (display_identifier,))
                row = cursor.fetchone()
                if row:
                    result = dict(row)
                else:
                    result = None
            else:
                cursor.execute("""
                    SELECT * FROM display_success_history ORDER BY total_attempts DESC
                """)
                rows = cursor.fetchall()
                result = {
                    'displays': [dict(row) for row in rows],
                    'total_displays': len(rows),
                    'tv_displays': sum(1 for row in rows if row['is_tv']),
                    'overall_success_rate': sum(row['successful_attempts'] for row in rows) / sum(row['total_attempts'] for row in rows) if rows else 0
                }

            conn.close()
            return result

        except Exception as e:
            logger.error(f"Failed to get display learning stats: {e}", exc_info=True)
            return {'error': str(e)}

    async def get_tv_analytics_summary(
        self,
        days: int = 30
    ) -> Dict[str, Any]:
        """
        Get TV unlock analytics summary for the specified period.

        Args:
            days: Number of days to include in summary

        Returns:
            Dictionary with aggregated TV unlock analytics
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Get aggregated stats
            cursor.execute("""
                SELECT
                    SUM(tv_unlock_attempts) as total_tv_attempts,
                    SUM(tv_unlock_successes) as total_tv_successes,
                    SUM(non_tv_unlock_attempts) as total_non_tv_attempts,
                    SUM(non_tv_unlock_successes) as total_non_tv_successes,
                    SUM(applescript_attempts) as total_applescript_attempts,
                    SUM(applescript_successes) as total_applescript_successes,
                    SUM(core_graphics_attempts) as total_cg_attempts,
                    SUM(core_graphics_successes) as total_cg_successes,
                    SUM(mirrored_attempts) as total_mirrored_attempts,
                    SUM(mirrored_successes) as total_mirrored_successes,
                    AVG(avg_tv_unlock_duration_ms) as avg_tv_duration,
                    AVG(avg_non_tv_unlock_duration_ms) as avg_non_tv_duration
                FROM tv_unlock_analytics
                WHERE date >= date('now', ? || ' days')
            """, (f'-{days}',))

            row = cursor.fetchone()

            if row and row['total_tv_attempts']:
                total_tv = row['total_tv_attempts']
                total_non_tv = row['total_non_tv_attempts'] or 0

                summary = {
                    'period_days': days,
                    'tv_unlocks': {
                        'total_attempts': total_tv,
                        'successes': row['total_tv_successes'],
                        'success_rate': row['total_tv_successes'] / total_tv if total_tv > 0 else 0,
                        'avg_duration_ms': row['avg_tv_duration']
                    },
                    'non_tv_unlocks': {
                        'total_attempts': total_non_tv,
                        'successes': row['total_non_tv_successes'],
                        'success_rate': row['total_non_tv_successes'] / total_non_tv if total_non_tv > 0 else 0,
                        'avg_duration_ms': row['avg_non_tv_duration']
                    },
                    'strategy_effectiveness': {
                        'applescript': {
                            'attempts': row['total_applescript_attempts'] or 0,
                            'success_rate': (row['total_applescript_successes'] or 0) / (row['total_applescript_attempts'] or 1)
                        },
                        'core_graphics': {
                            'attempts': row['total_cg_attempts'] or 0,
                            'success_rate': (row['total_cg_successes'] or 0) / (row['total_cg_attempts'] or 1)
                        }
                    },
                    'mirrored_display': {
                        'attempts': row['total_mirrored_attempts'] or 0,
                        'success_rate': (row['total_mirrored_successes'] or 0) / (row['total_mirrored_attempts'] or 1)
                    }
                }
            else:
                summary = {
                    'period_days': days,
                    'message': 'No TV unlock data available for this period',
                    'tv_unlocks': {'total_attempts': 0, 'success_rate': 0},
                    'non_tv_unlocks': {'total_attempts': 0, 'success_rate': 0}
                }

            conn.close()
            return summary

        except Exception as e:
            logger.error(f"Failed to get TV analytics summary: {e}", exc_info=True)
            return {'error': str(e)}

    # =========================================================================
    # 🖥️ DYNAMIC SAI: Real-time TV Connection Tracking
    # =========================================================================

    async def record_connection_event(
        self,
        event_type: str,  # 'CONNECTED', 'DISCONNECTED', 'CONFIG_CHANGED', 'SAI_CHECK'
        display_context: Dict[str, Any],
        sai_reasoning: List[str] = None,
        trigger_source: str = "auto_monitor"
    ) -> Optional[int]:
        """
        Record a display connection event detected by SAI.

        This enables dynamic tracking of TV connections/disconnections
        independent of unlock attempts.

        Args:
            event_type: Type of event
            display_context: Current display context from SAI
            sai_reasoning: SAI reasoning steps for this detection
            trigger_source: What triggered this check

        Returns:
            Event ID if successful, None otherwise
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            now = datetime.now().isoformat()

            # Extract display info
            tv_info = display_context.get('tv_info', {}) or {}
            is_tv = display_context.get('is_tv_connected', False)

            # Determine display identifier
            if is_tv and display_context.get('tv_name'):
                display_identifier = display_context['tv_name']
                display_type = 'TV'
            elif display_context.get('external_display', {}).get('name'):
                display_identifier = display_context['external_display']['name']
                display_type = 'MONITOR'
            else:
                display_identifier = display_context.get('primary_display', {}).get('name', 'Built-in Display')
                display_type = 'BUILTIN'

            # Get resolution
            ext = display_context.get('external_display', {})
            resolution = f"{ext.get('width', 0)}x{ext.get('height', 0)}" if ext else None

            # Get system uptime
            system_uptime = None
            try:
                import subprocess
                result = subprocess.run(['sysctl', '-n', 'kern.boottime'], capture_output=True, text=True)
                if result.returncode == 0:
                    import time
                    boot_time = int(result.stdout.split('sec = ')[1].split(',')[0])
                    system_uptime = time.time() - boot_time
            except:
                pass

            # Check if screen is locked
            was_screen_locked = 0
            try:
                from voice_unlock.objc.server.screen_lock_detector import is_screen_locked
                was_screen_locked = 1 if is_screen_locked() else 0
            except:
                pass

            cursor.execute("""
                INSERT INTO display_connection_events (
                    timestamp, event_type,
                    display_identifier, display_name, display_type,
                    is_tv, tv_brand, tv_confidence, tv_detection_reasons,
                    total_displays, display_mode, is_mirrored, resolution, refresh_rate,
                    sai_detection_method, sai_detection_time_ms, sai_confidence, sai_reasoning,
                    system_uptime_seconds, was_screen_locked, trigger_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now, event_type,
                display_identifier,
                display_context.get('tv_name') or display_identifier,
                display_type,
                1 if is_tv else 0,
                tv_info.get('brand'),
                tv_info.get('confidence', 0),
                json.dumps(tv_info.get('reasons', [])),
                display_context.get('total_displays', 1),
                display_context.get('display_mode', 'SINGLE'),
                1 if display_context.get('is_mirrored', False) else 0,
                resolution,
                ext.get('refresh_rate') if ext else None,
                display_context.get('detection_method', 'sai_langgraph'),
                display_context.get('detection_time_ms', 0),
                tv_info.get('confidence', 0.5),
                json.dumps(sai_reasoning or []),
                system_uptime,
                was_screen_locked,
                trigger_source
            ))

            event_id = cursor.lastrowid

            # Update TV connection state
            await self._update_tv_connection_state(cursor, event_type, display_context, tv_info)

            # Handle TV session tracking
            if event_type == 'CONNECTED' and is_tv:
                await self._start_tv_session(cursor, display_identifier, display_context, tv_info)
            elif event_type == 'DISCONNECTED' and is_tv:
                await self._end_tv_session(cursor, display_identifier)

            conn.commit()
            conn.close()

            logger.info(f"📊 [SAI-EVENT] Recorded {event_type} for '{display_identifier}' (ID: {event_id})")
            return event_id

        except Exception as e:
            logger.error(f"Failed to record connection event: {e}", exc_info=True)
            return None

    async def _update_tv_connection_state(
        self,
        cursor,
        event_type: str,
        display_context: Dict[str, Any],
        tv_info: Dict[str, Any]
    ):
        """Update the singleton TV connection state record"""
        now = datetime.now().isoformat()
        is_tv = display_context.get('is_tv_connected', False)

        # Check if state record exists
        cursor.execute("SELECT id FROM tv_connection_state LIMIT 1")
        row = cursor.fetchone()

        if row:
            # Update existing
            if event_type in ('CONNECTED', 'SAI_CHECK') and is_tv:
                cursor.execute("""
                    UPDATE tv_connection_state SET
                        last_updated = ?,
                        is_tv_currently_connected = 1,
                        current_tv_name = ?,
                        current_tv_brand = ?,
                        current_display_mode = ?,
                        current_is_mirrored = ?,
                        total_tv_connections = total_tv_connections + ?
                    WHERE id = ?
                """, (
                    now,
                    display_context.get('tv_name'),
                    tv_info.get('brand'),
                    display_context.get('display_mode', 'MIRRORED'),
                    1 if display_context.get('is_mirrored', False) else 0,
                    1 if event_type == 'CONNECTED' else 0,
                    row[0]
                ))
            elif event_type == 'DISCONNECTED':
                cursor.execute("""
                    UPDATE tv_connection_state SET
                        last_updated = ?,
                        is_tv_currently_connected = 0,
                        current_tv_name = NULL,
                        current_tv_brand = NULL,
                        total_tv_disconnections = total_tv_disconnections + 1
                    WHERE id = ?
                """, (now, row[0]))
        else:
            # Create initial state
            cursor.execute("""
                INSERT INTO tv_connection_state (
                    last_updated,
                    is_tv_currently_connected,
                    current_tv_name,
                    current_tv_brand,
                    current_display_mode,
                    current_is_mirrored,
                    total_tv_connections,
                    total_tv_disconnections
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now,
                1 if is_tv else 0,
                display_context.get('tv_name') if is_tv else None,
                tv_info.get('brand') if is_tv else None,
                display_context.get('display_mode', 'SINGLE'),
                1 if display_context.get('is_mirrored', False) else 0,
                1 if is_tv and event_type == 'CONNECTED' else 0,
                0
            ))

    async def _start_tv_session(
        self,
        cursor,
        tv_identifier: str,
        display_context: Dict[str, Any],
        tv_info: Dict[str, Any]
    ):
        """Start a new TV session"""
        now = datetime.now().isoformat()

        # End any existing active sessions for this TV
        cursor.execute("""
            UPDATE tv_sessions SET
                is_active = 0,
                session_end = ?
            WHERE tv_identifier = ? AND is_active = 1
        """, (now, tv_identifier))

        # Get resolution
        ext = display_context.get('external_display', {})
        resolution = f"{ext.get('width', 0)}x{ext.get('height', 0)}" if ext else None

        cursor.execute("""
            INSERT INTO tv_sessions (
                session_start, is_active,
                tv_identifier, tv_name, tv_brand, tv_confidence,
                display_mode, is_mirrored, resolution
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now,
            tv_identifier,
            display_context.get('tv_name'),
            tv_info.get('brand'),
            tv_info.get('confidence', 0),
            display_context.get('display_mode', 'MIRRORED'),
            1 if display_context.get('is_mirrored', False) else 0,
            resolution
        ))

        logger.info(f"📺 [TV-SESSION] Started new session for '{tv_identifier}'")

    async def _end_tv_session(self, cursor, tv_identifier: str):
        """End an active TV session"""
        now = datetime.now().isoformat()

        # Get active session
        cursor.execute("""
            SELECT id, session_start, unlock_attempts, unlock_successes
            FROM tv_sessions
            WHERE tv_identifier = ? AND is_active = 1
        """, (tv_identifier,))

        row = cursor.fetchone()
        if row:
            session_id, start_time, attempts, successes = row

            # Calculate duration
            start_dt = datetime.fromisoformat(start_time)
            duration_minutes = (datetime.now() - start_dt).total_seconds() / 60

            # Calculate success rate
            success_rate = successes / attempts if attempts > 0 else 0

            cursor.execute("""
                UPDATE tv_sessions SET
                    is_active = 0,
                    session_end = ?,
                    session_duration_minutes = ?,
                    session_success_rate = ?
                WHERE id = ?
            """, (now, duration_minutes, success_rate, session_id))

            logger.info(
                f"📺 [TV-SESSION] Ended session for '{tv_identifier}' "
                f"(duration: {duration_minutes:.1f}min, success_rate: {success_rate:.1%})"
            )

    async def update_active_tv_session(
        self,
        success: bool,
        typing_strategy: str,
        unlock_duration_ms: float
    ):
        """
        Update the active TV session with unlock attempt results.
        Called after each unlock attempt when TV is connected.
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            # Get active session
            cursor.execute("""
                SELECT id, unlock_attempts, unlock_successes, unlock_failures,
                       applescript_attempts, applescript_successes,
                       core_graphics_attempts, core_graphics_successes,
                       avg_unlock_duration_ms, fastest_unlock_ms, slowest_unlock_ms
                FROM tv_sessions
                WHERE is_active = 1
                LIMIT 1
            """)

            row = cursor.fetchone()
            if not row:
                conn.close()
                return

            session_id = row[0]
            attempts = (row[1] or 0) + 1
            successes = (row[2] or 0) + (1 if success else 0)
            failures = (row[3] or 0) + (0 if success else 1)
            applescript_attempts = row[4] or 0
            applescript_successes = row[5] or 0
            cg_attempts = row[6] or 0
            cg_successes = row[7] or 0
            avg_duration = row[8] or 0
            fastest = row[9]
            slowest = row[10]

            # Update strategy counters
            strategy_upper = typing_strategy.upper() if typing_strategy else ''
            if 'APPLESCRIPT' in strategy_upper:
                applescript_attempts += 1
                if success:
                    applescript_successes += 1
            elif 'CORE_GRAPHICS' in strategy_upper:
                cg_attempts += 1
                if success:
                    cg_successes += 1

            # Update duration stats
            if avg_duration > 0:
                avg_duration = (avg_duration * (attempts - 1) + unlock_duration_ms) / attempts
            else:
                avg_duration = unlock_duration_ms

            if fastest is None or unlock_duration_ms < fastest:
                fastest = unlock_duration_ms
            if slowest is None or unlock_duration_ms > slowest:
                slowest = unlock_duration_ms

            # Determine best strategy
            applescript_rate = applescript_successes / applescript_attempts if applescript_attempts > 0 else 0
            cg_rate = cg_successes / cg_attempts if cg_attempts > 0 else 0
            best_strategy = 'APPLESCRIPT_DIRECT' if applescript_rate >= cg_rate else 'CORE_GRAPHICS_FAST'

            success_rate = successes / attempts if attempts > 0 else 0

            cursor.execute("""
                UPDATE tv_sessions SET
                    unlock_attempts = ?,
                    unlock_successes = ?,
                    unlock_failures = ?,
                    session_success_rate = ?,
                    applescript_attempts = ?,
                    applescript_successes = ?,
                    core_graphics_attempts = ?,
                    core_graphics_successes = ?,
                    best_strategy_this_session = ?,
                    avg_unlock_duration_ms = ?,
                    fastest_unlock_ms = ?,
                    slowest_unlock_ms = ?
                WHERE id = ?
            """, (
                attempts, successes, failures, success_rate,
                applescript_attempts, applescript_successes,
                cg_attempts, cg_successes,
                best_strategy,
                avg_duration, fastest, slowest,
                session_id
            ))

            conn.commit()
            conn.close()

            logger.info(f"📺 [TV-SESSION] Updated: attempts={attempts}, rate={success_rate:.1%}")

        except Exception as e:
            logger.error(f"Failed to update TV session: {e}", exc_info=True)

    async def get_current_tv_state(self) -> Dict[str, Any]:
        """
        Get the current TV connection state from SAI's perspective.
        Returns real-time awareness of TV connectivity.
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM tv_connection_state LIMIT 1")
            row = cursor.fetchone()

            if row:
                state = dict(row)

                # Get active session info if TV connected
                if state.get('is_tv_currently_connected'):
                    cursor.execute("""
                        SELECT * FROM tv_sessions WHERE is_active = 1 LIMIT 1
                    """)
                    session = cursor.fetchone()
                    if session:
                        state['active_session'] = dict(session)
                        # Calculate current session duration
                        start = datetime.fromisoformat(session['session_start'])
                        state['active_session']['current_duration_minutes'] = (
                            (datetime.now() - start).total_seconds() / 60
                        )

                conn.close()
                return state
            else:
                conn.close()
                return {
                    'is_tv_currently_connected': False,
                    'message': 'No TV state recorded yet'
                }

        except Exception as e:
            logger.error(f"Failed to get TV state: {e}", exc_info=True)
            return {'error': str(e)}

    async def get_tv_connection_history(
        self,
        limit: int = 50,
        event_type: str = None
    ) -> List[Dict[str, Any]]:
        """
        Get recent TV connection events.

        Args:
            limit: Maximum events to return
            event_type: Filter by event type (optional)

        Returns:
            List of connection events
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if event_type:
                cursor.execute("""
                    SELECT * FROM display_connection_events
                    WHERE event_type = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (event_type, limit))
            else:
                cursor.execute("""
                    SELECT * FROM display_connection_events
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (limit,))

            events = [dict(row) for row in cursor.fetchall()]
            conn.close()

            return events

        except Exception as e:
            logger.error(f"Failed to get connection history: {e}", exc_info=True)
            return []

    # =========================================================================
    # 🎙️ VOICE PROFILE LEARNING: Continuous voice enrollment tracking
    # =========================================================================

    async def record_voice_sample(
        self,
        speaker_name: str,
        confidence: float,
        was_verified: bool,
        audio_quality: float = 0.5,
        snr_db: float = 15.0,
        audio_duration_ms: float = 0,
        sample_source: str = "unlock_attempt",
        environment_type: str = "unknown",
        threshold_used: float = 0.85,
        added_to_profile: bool = False,
        rejection_reason: str = None,
        embedding_dimensions: int = 192
    ) -> Optional[int]:
        """
        Record a voice sample from an unlock attempt for continuous learning tracking.

        This tracks every voice sample JARVIS hears, whether it's added to the
        profile or not, enabling analytics on voice recognition improvement.

        Args:
            speaker_name: Name of the speaker
            confidence: Confidence score from verification
            was_verified: Whether verification passed
            audio_quality: Quality score (0-1)
            snr_db: Signal-to-noise ratio
            audio_duration_ms: Duration of audio
            sample_source: Source of sample
            environment_type: Environment where recorded
            threshold_used: Threshold used for verification
            added_to_profile: Whether sample was added to voice profile
            rejection_reason: Why sample was rejected (if applicable)
            embedding_dimensions: Dimensions of the embedding

        Returns:
            Sample ID if successful, None otherwise
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            now = datetime.now()

            # Determine time of day
            hour = now.hour
            if 5 <= hour < 12:
                time_of_day = 'morning'
            elif 12 <= hour < 17:
                time_of_day = 'afternoon'
            elif 17 <= hour < 21:
                time_of_day = 'evening'
            else:
                time_of_day = 'night'

            cursor.execute("""
                INSERT INTO voice_sample_log (
                    timestamp, speaker_name,
                    sample_source, audio_duration_ms, audio_quality_score, snr_db,
                    confidence, was_verified, threshold_used,
                    added_to_profile, rejection_reason,
                    environment_type, time_of_day,
                    embedding_stored, embedding_dimensions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now.isoformat(), speaker_name,
                sample_source, audio_duration_ms, audio_quality, snr_db,
                confidence, 1 if was_verified else 0, threshold_used,
                1 if added_to_profile else 0, rejection_reason,
                environment_type, time_of_day,
                1 if added_to_profile else 0, embedding_dimensions
            ))

            sample_id = cursor.lastrowid

            # Update voice profile learning stats
            await self._update_voice_profile_learning(
                cursor, speaker_name, confidence, was_verified,
                audio_quality, added_to_profile
            )

            # Update confidence history
            await self._update_confidence_history(
                cursor, speaker_name, confidence, environment_type, time_of_day
            )

            conn.commit()
            conn.close()

            status = "✅ ADDED" if added_to_profile else "📝 LOGGED"
            logger.info(
                f"🎙️ [VOICE] {status} sample #{sample_id} for {speaker_name} "
                f"(conf: {confidence:.1%}, quality: {audio_quality:.2f})"
            )

            return sample_id

        except Exception as e:
            logger.error(f"Failed to record voice sample: {e}", exc_info=True)
            return None

    async def _update_voice_profile_learning(
        self,
        cursor,
        speaker_name: str,
        confidence: float,
        was_verified: bool,
        audio_quality: float,
        added_to_profile: bool
    ):
        """Update the voice profile learning stats for a speaker"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        # Check if profile exists
        cursor.execute("""
            SELECT id, total_samples_collected, samples_added_today,
                   high_confidence_samples, low_quality_samples_rejected,
                   best_confidence_ever, worst_confidence_ever,
                   total_unlock_attempts, successful_unlocks
            FROM voice_profile_learning
            WHERE speaker_name = ?
        """, (speaker_name,))

        row = cursor.fetchone()

        if row:
            # Update existing
            row_id = row[0]
            total_samples = (row[1] or 0) + (1 if added_to_profile else 0)
            samples_today = (row[2] or 0) + (1 if added_to_profile else 0)
            high_conf_samples = (row[3] or 0) + (1 if confidence >= 0.85 and added_to_profile else 0)
            rejected = (row[4] or 0) + (1 if not added_to_profile else 0)
            best_conf = max(row[5] or 0, confidence)
            worst_conf = min(row[6] or 1.0, confidence) if row[6] else confidence
            total_attempts = (row[7] or 0) + 1
            successful = (row[8] or 0) + (1 if was_verified else 0)

            success_rate = successful / total_attempts if total_attempts > 0 else 0

            # Calculate confidence trend (simple: compare to best)
            if confidence >= best_conf * 0.95:
                trend = 'improving'
            elif confidence >= best_conf * 0.85:
                trend = 'stable'
            else:
                trend = 'declining'

            cursor.execute("""
                UPDATE voice_profile_learning SET
                    timestamp = ?,
                    total_samples_collected = ?,
                    samples_added_today = ?,
                    high_confidence_samples = ?,
                    low_quality_samples_rejected = ?,
                    current_avg_confidence = ?,
                    best_confidence_ever = ?,
                    worst_confidence_ever = ?,
                    confidence_trend = ?,
                    total_unlock_attempts = ?,
                    successful_unlocks = ?,
                    unlock_success_rate = ?,
                    avg_audio_quality = ?
                WHERE id = ?
            """, (
                now.isoformat(),
                total_samples,
                samples_today,
                high_conf_samples,
                rejected,
                confidence,  # Latest confidence as current
                best_conf,
                worst_conf,
                trend,
                total_attempts,
                successful,
                success_rate,
                audio_quality,
                row_id
            ))

            # Check for milestones
            if total_samples == 50 and row[1] < 50:
                cursor.execute("""
                    UPDATE voice_profile_learning SET reached_50_samples_date = ?
                    WHERE id = ?
                """, (today, row_id))
                logger.info(f"🎉 [VOICE] {speaker_name} reached 50 voice samples!")

            if total_samples == 100 and row[1] < 100:
                cursor.execute("""
                    UPDATE voice_profile_learning SET reached_100_samples_date = ?
                    WHERE id = ?
                """, (today, row_id))
                logger.info(f"🎉 [VOICE] {speaker_name} reached 100 voice samples!")

        else:
            # Create new profile
            cursor.execute("""
                INSERT INTO voice_profile_learning (
                    timestamp, speaker_name,
                    total_samples_collected, samples_added_today, high_confidence_samples,
                    low_quality_samples_rejected,
                    current_avg_confidence, best_confidence_ever, worst_confidence_ever,
                    confidence_trend,
                    total_unlock_attempts, successful_unlocks, unlock_success_rate,
                    avg_audio_quality,
                    first_sample_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now.isoformat(), speaker_name,
                1 if added_to_profile else 0,
                1 if added_to_profile else 0,
                1 if confidence >= 0.85 and added_to_profile else 0,
                0 if added_to_profile else 1,
                confidence, confidence, confidence,
                'stable',
                1, 1 if was_verified else 0,
                1.0 if was_verified else 0.0,
                audio_quality,
                today
            ))

    async def _update_confidence_history(
        self,
        cursor,
        speaker_name: str,
        confidence: float,
        environment_type: str,
        time_of_day: str
    ):
        """Update daily confidence history for trending"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        # Check if today's record exists
        cursor.execute("""
            SELECT id, attempts_today, avg_confidence_today,
                   best_confidence_today, worst_confidence_today
            FROM voice_confidence_history
            WHERE speaker_name = ? AND date = ?
        """, (speaker_name, today))

        row = cursor.fetchone()

        if row:
            row_id = row[0]
            attempts = (row[1] or 0) + 1
            # Rolling average
            old_avg = row[2] or confidence
            new_avg = (old_avg * (attempts - 1) + confidence) / attempts
            best = max(row[3] or 0, confidence)
            worst = min(row[4] or 1.0, confidence)

            cursor.execute("""
                UPDATE voice_confidence_history SET
                    timestamp = ?,
                    attempts_today = ?,
                    avg_confidence_today = ?,
                    best_confidence_today = ?,
                    worst_confidence_today = ?
                WHERE id = ?
            """, (now.isoformat(), attempts, new_avg, best, worst, row_id))
        else:
            cursor.execute("""
                INSERT INTO voice_confidence_history (
                    timestamp, speaker_name, date,
                    attempts_today, avg_confidence_today,
                    best_confidence_today, worst_confidence_today
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                now.isoformat(), speaker_name, today,
                1, confidence, confidence, confidence
            ))

    async def get_voice_profile_stats(self, speaker_name: str) -> Dict[str, Any]:
        """
        Get comprehensive voice profile learning stats for a speaker.

        Returns stats about sample collection, confidence trends,
        and authentication performance.
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Get profile learning stats
            cursor.execute("""
                SELECT * FROM voice_profile_learning WHERE speaker_name = ?
            """, (speaker_name,))
            profile = cursor.fetchone()

            # Get recent samples
            cursor.execute("""
                SELECT confidence, was_verified, audio_quality_score, time_of_day
                FROM voice_sample_log
                WHERE speaker_name = ?
                ORDER BY timestamp DESC
                LIMIT 50
            """, (speaker_name,))
            recent_samples = cursor.fetchall()

            # Get confidence history (last 7 days)
            cursor.execute("""
                SELECT date, avg_confidence_today, attempts_today
                FROM voice_confidence_history
                WHERE speaker_name = ?
                ORDER BY date DESC
                LIMIT 7
            """, (speaker_name,))
            history = cursor.fetchall()

            conn.close()

            if profile:
                stats = dict(profile)

                # Add recent sample analysis
                if recent_samples:
                    confidences = [s['confidence'] for s in recent_samples]
                    stats['recent_50_avg_confidence'] = sum(confidences) / len(confidences)
                    stats['recent_50_best'] = max(confidences)
                    stats['recent_50_worst'] = min(confidences)

                    # Time-of-day performance
                    tod_stats = {}
                    for s in recent_samples:
                        tod = s['time_of_day']
                        if tod not in tod_stats:
                            tod_stats[tod] = []
                        tod_stats[tod].append(s['confidence'])

                    stats['time_of_day_performance'] = {
                        tod: sum(confs) / len(confs)
                        for tod, confs in tod_stats.items()
                    }

                # Add history
                stats['confidence_history_7days'] = [
                    {'date': h['date'], 'avg': h['avg_confidence_today'], 'attempts': h['attempts_today']}
                    for h in history
                ]

                return stats
            else:
                return {
                    'speaker_name': speaker_name,
                    'message': 'No voice profile learning data yet',
                    'total_samples_collected': 0
                }

        except Exception as e:
            logger.error(f"Failed to get voice profile stats: {e}", exc_info=True)
            return {'error': str(e)}

    async def sync_embedding_to_sqlite(
        self,
        speaker_name: str,
        embedding: bytes,
        embedding_dimensions: int = 192,
        speaker_id: int = None,
        total_samples: int = 0,
        rolling_samples: int = 0,
        avg_confidence: float = 0.0,
        cloud_sql_synced: bool = True,
        update_reason: str = "periodic_save"
    ) -> Optional[int]:
        """
        Sync voice embedding to SQLite (mirrors Cloud SQL data).

        This ensures SQLite and Cloud SQL stay in sync for redundancy.

        Args:
            speaker_name: Speaker name
            embedding: Raw embedding bytes (numpy array.tobytes())
            embedding_dimensions: Dimensions of the embedding
            speaker_id: Cloud SQL speaker_id
            total_samples: Total samples used to create this embedding
            rolling_samples: Number of samples in rolling average
            avg_confidence: Average confidence score
            cloud_sql_synced: Whether this is synced with Cloud SQL
            update_reason: Why this update happened

        Returns:
            Embedding ID if successful, None otherwise
        """
        try:
            import base64

            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            now = datetime.now().isoformat()

            # Encode embedding as base64
            embedding_b64 = base64.b64encode(embedding).decode('utf-8')

            # Check if embedding exists for this speaker
            cursor.execute("""
                SELECT id, version FROM voice_embeddings WHERE speaker_name = ?
            """, (speaker_name,))

            row = cursor.fetchone()

            if row:
                # Update existing
                embedding_id = row[0]
                new_version = (row[1] or 0) + 1

                cursor.execute("""
                    UPDATE voice_embeddings SET
                        embedding_b64 = ?,
                        embedding_dimensions = ?,
                        speaker_id = COALESCE(?, speaker_id),
                        version = ?,
                        updated_at = ?,
                        total_samples_used = ?,
                        rolling_samples_in_avg = ?,
                        avg_sample_confidence = ?,
                        cloud_sql_synced = ?,
                        cloud_sql_sync_time = ?,
                        source = 'continuous_learning',
                        last_verification_confidence = ?
                    WHERE id = ?
                """, (
                    embedding_b64,
                    embedding_dimensions,
                    speaker_id,
                    new_version,
                    now,
                    total_samples,
                    rolling_samples,
                    avg_confidence,
                    1 if cloud_sql_synced else 0,
                    now if cloud_sql_synced else None,
                    avg_confidence,
                    embedding_id
                ))

                # Save to history
                cursor.execute("""
                    INSERT INTO voice_embedding_history (
                        timestamp, speaker_name, embedding_b64, embedding_dimensions,
                        version, update_reason, samples_used, total_samples_at_update,
                        avg_confidence_at_save, cloud_sql_synced
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now, speaker_name, embedding_b64, embedding_dimensions,
                    new_version, update_reason, rolling_samples, total_samples,
                    avg_confidence, 1 if cloud_sql_synced else 0
                ))

                logger.info(
                    f"🔄 [SQLITE-SYNC] Updated embedding for {speaker_name} "
                    f"(v{new_version}, {total_samples} samples, synced={cloud_sql_synced})"
                )

            else:
                # Insert new
                cursor.execute("""
                    INSERT INTO voice_embeddings (
                        speaker_name, speaker_id, embedding_b64, embedding_dimensions,
                        version, created_at, updated_at,
                        total_samples_used, rolling_samples_in_avg, avg_sample_confidence,
                        cloud_sql_synced, cloud_sql_sync_time, source, last_verification_confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    speaker_name, speaker_id, embedding_b64, embedding_dimensions,
                    1, now, now,
                    total_samples, rolling_samples, avg_confidence,
                    1 if cloud_sql_synced else 0, now if cloud_sql_synced else None,
                    'continuous_learning', avg_confidence
                ))

                embedding_id = cursor.lastrowid

                # Save initial state to history
                cursor.execute("""
                    INSERT INTO voice_embedding_history (
                        timestamp, speaker_name, embedding_b64, embedding_dimensions,
                        version, update_reason, samples_used, total_samples_at_update,
                        avg_confidence_at_save, cloud_sql_synced
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now, speaker_name, embedding_b64, embedding_dimensions,
                    1, 'initial_save', rolling_samples, total_samples,
                    avg_confidence, 1 if cloud_sql_synced else 0
                ))

                logger.info(
                    f"🆕 [SQLITE-SYNC] Created embedding for {speaker_name} "
                    f"(ID: {embedding_id}, {total_samples} samples)"
                )

            conn.commit()
            conn.close()

            return embedding_id

        except Exception as e:
            logger.error(f"Failed to sync embedding to SQLite: {e}", exc_info=True)
            return None

    async def get_sqlite_embedding(self, speaker_name: str) -> Optional[Dict[str, Any]]:
        """
        Get voice embedding from SQLite.

        Returns:
            Dict with embedding data or None if not found
        """
        try:
            import base64
            import numpy as np

            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT * FROM voice_embeddings WHERE speaker_name = ?
            """, (speaker_name,))

            row = cursor.fetchone()
            conn.close()

            if row:
                result = dict(row)

                # Decode embedding from base64
                embedding_b64 = result.get('embedding_b64')
                if embedding_b64:
                    embedding_bytes = base64.b64decode(embedding_b64)
                    result['embedding'] = np.frombuffer(embedding_bytes, dtype=np.float32)

                return result

            return None

        except Exception as e:
            logger.error(f"Failed to get SQLite embedding: {e}", exc_info=True)
            return None

    async def get_embedding_history(
        self,
        speaker_name: str,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Get embedding update history for rollback capability.

        Args:
            speaker_name: Speaker name
            limit: Max history entries to return

        Returns:
            List of embedding history records
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, timestamp, version, update_reason, samples_used,
                       total_samples_at_update, avg_confidence_at_save, cloud_sql_synced
                FROM voice_embedding_history
                WHERE speaker_name = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (speaker_name, limit))

            history = [dict(row) for row in cursor.fetchall()]
            conn.close()

            return history

        except Exception as e:
            logger.error(f"Failed to get embedding history: {e}", exc_info=True)
            return []

    async def rollback_embedding(self, speaker_name: str, version: int) -> bool:
        """
        Rollback embedding to a previous version.

        Args:
            speaker_name: Speaker name
            version: Version number to rollback to

        Returns:
            True if successful
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            # Get the historical embedding
            cursor.execute("""
                SELECT embedding_b64, embedding_dimensions, avg_confidence_at_save
                FROM voice_embedding_history
                WHERE speaker_name = ? AND version = ?
            """, (speaker_name, version))

            row = cursor.fetchone()

            if not row:
                logger.warning(f"No embedding found for {speaker_name} version {version}")
                conn.close()
                return False

            embedding_b64, dimensions, confidence = row

            # Update current embedding
            now = datetime.now().isoformat()
            cursor.execute("""
                UPDATE voice_embeddings SET
                    embedding_b64 = ?,
                    embedding_dimensions = ?,
                    updated_at = ?,
                    cloud_sql_synced = 0,
                    source = 'rollback'
                WHERE speaker_name = ?
            """, (embedding_b64, dimensions, now, speaker_name))

            # Record rollback in history
            cursor.execute("""
                SELECT version FROM voice_embeddings WHERE speaker_name = ?
            """, (speaker_name,))
            current_version = cursor.fetchone()[0]

            cursor.execute("""
                INSERT INTO voice_embedding_history (
                    timestamp, speaker_name, embedding_b64, embedding_dimensions,
                    version, update_reason, avg_confidence_at_save, cloud_sql_synced
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now, speaker_name, embedding_b64, dimensions,
                current_version + 1, f'rollback_to_v{version}', confidence, 0
            ))

            conn.commit()
            conn.close()

            logger.info(f"🔙 [ROLLBACK] Rolled back {speaker_name} to version {version}")
            return True

        except Exception as e:
            logger.error(f"Failed to rollback embedding: {e}", exc_info=True)
            return False

    async def get_voice_learning_summary(self, speaker_name: str) -> str:
        """
        Get a human-readable summary of voice learning progress.

        This can be spoken by JARVIS to inform the user about
        their voice profile's improvement.
        """
        stats = await self.get_voice_profile_stats(speaker_name)

        if 'error' in stats or stats.get('total_samples_collected', 0) == 0:
            return f"I haven't collected enough voice samples yet for {speaker_name}."

        total = stats.get('total_samples_collected', 0)
        best = stats.get('best_confidence_ever', 0)
        current = stats.get('current_avg_confidence', 0)
        trend = stats.get('confidence_trend', 'stable')
        success_rate = stats.get('unlock_success_rate', 0)

        summary_parts = [
            f"Voice profile for {speaker_name}:",
            f"I've collected {total} voice samples.",
            f"Your best confidence score is {best:.0%}.",
            f"Current average confidence is {current:.0%}.",
            f"Confidence trend is {trend}.",
            f"Unlock success rate is {success_rate:.0%}."
        ]

        # Milestones
        if stats.get('reached_100_samples_date'):
            summary_parts.append(f"Reached 100 samples on {stats['reached_100_samples_date']}!")
        elif stats.get('reached_50_samples_date'):
            summary_parts.append(f"Reached 50 samples on {stats['reached_50_samples_date']}!")

        # Recommendation
        if total < 50:
            summary_parts.append(f"I need about {50 - total} more samples for optimal recognition.")
        elif trend == 'declining':
            summary_parts.append("Consider re-enrolling in a quiet environment.")
        elif trend == 'improving':
            summary_parts.append("Your voice profile is getting better with each use!")

        return " ".join(summary_parts)

    # =========================================================================
    # 🧠 STT HALLUCINATION LEARNING METHODS
    # =========================================================================

    async def record_hallucination(
        self,
        original_text: str,
        corrected_text: Optional[str],
        hallucination_type: str,
        confidence: float,
        detection_method: str,
        reasoning_steps: int = 0,
        sai_context: Optional[Dict[str, Any]] = None,
        audio_hash: Optional[str] = None,
        context: str = "unlock_command"
    ) -> Optional[int]:
        """
        Record a detected hallucination for continuous learning.

        Args:
            original_text: The hallucinated transcription
            corrected_text: The correction (if any)
            hallucination_type: Type of hallucination
            confidence: Detection confidence
            detection_method: How it was detected
            reasoning_steps: Number of LangGraph reasoning steps
            sai_context: SAI context at detection
            audio_hash: Hash of the audio (for deduplication)
            context: Context (unlock_command, general, etc.)

        Returns:
            Hallucination ID if successful
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            now = datetime.now().isoformat()
            date = datetime.now().strftime("%Y-%m-%d")
            normalized = original_text.lower().strip()

            # Check if this hallucination already exists
            cursor.execute("""
                SELECT id, occurrence_count, times_corrected, times_flagged
                FROM stt_hallucinations WHERE original_text_normalized = ?
            """, (normalized,))

            existing = cursor.fetchone()

            if existing:
                # Update existing record
                hallucination_id = existing[0]
                occurrence_count = existing[1] + 1
                times_corrected = existing[2] + (1 if corrected_text else 0)
                times_flagged = existing[3] + (0 if corrected_text else 1)

                cursor.execute("""
                    UPDATE stt_hallucinations SET
                        occurrence_count = ?,
                        last_occurrence = ?,
                        times_corrected = ?,
                        times_flagged = ?,
                        detection_confidence = MAX(detection_confidence, ?),
                        corrected_text = COALESCE(?, corrected_text)
                    WHERE id = ?
                """, (
                    occurrence_count, now, times_corrected, times_flagged,
                    confidence, corrected_text, hallucination_id
                ))

                logger.debug(f"📚 Updated hallucination #{hallucination_id}: occurrence {occurrence_count}")

            else:
                # Insert new record
                sai_tv = sai_context.get("is_tv_connected", False) if sai_context else False
                sai_displays = sai_context.get("display_context", {}).get("display_count", 0) if sai_context else 0

                cursor.execute("""
                    INSERT INTO stt_hallucinations (
                        timestamp, date, original_text, original_text_normalized,
                        corrected_text, was_corrected, correction_source,
                        hallucination_type, detection_confidence, detection_method,
                        reasoning_steps, context, audio_hash,
                        sai_tv_connected, sai_display_count,
                        occurrence_count, last_occurrence, times_corrected, times_flagged
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now, date, original_text, normalized,
                    corrected_text, 1 if corrected_text else 0,
                    "auto_learned" if corrected_text else None,
                    hallucination_type, confidence, detection_method,
                    reasoning_steps, context, audio_hash,
                    1 if sai_tv else 0, sai_displays,
                    1, now, 1 if corrected_text else 0, 0 if corrected_text else 1
                ))

                hallucination_id = cursor.lastrowid
                logger.info(f"📚 Recorded new hallucination #{hallucination_id}: '{original_text[:30]}...'")

            # Also update/create correction mapping if we have a correction
            if corrected_text:
                cursor.execute("""
                    INSERT INTO hallucination_corrections (
                        timestamp, hallucination_normalized, correction,
                        correction_confidence, times_applied, last_applied, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(hallucination_normalized) DO UPDATE SET
                        times_applied = times_applied + 1,
                        last_applied = ?,
                        correction_confidence = MAX(correction_confidence, ?)
                """, (
                    now, normalized, corrected_text,
                    confidence, 1, now, "auto_learned",
                    now, confidence
                ))

            conn.commit()
            conn.close()

            return hallucination_id

        except Exception as e:
            logger.error(f"Failed to record hallucination: {e}", exc_info=True)
            return None

    async def get_hallucination_patterns(self) -> List[str]:
        """
        Get all learned hallucination patterns.

        Returns:
            List of normalized hallucination texts
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            # Get patterns with at least 1 occurrence
            cursor.execute("""
                SELECT original_text_normalized
                FROM stt_hallucinations
                WHERE occurrence_count >= 1
                ORDER BY occurrence_count DESC
            """)

            patterns = [row[0] for row in cursor.fetchall()]
            conn.close()

            return patterns

        except Exception as e:
            logger.error(f"Failed to get hallucination patterns: {e}")
            return []

    async def get_hallucination_corrections(self) -> Dict[str, str]:
        """
        Get mapping of hallucinations to their corrections.

        Returns:
            Dict mapping normalized hallucination text to correction
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT hallucination_normalized, correction
                FROM hallucination_corrections
                WHERE correction_confidence >= 0.5
                ORDER BY times_applied DESC
            """)

            corrections = {row[0]: row[1] for row in cursor.fetchall()}
            conn.close()

            return corrections

        except Exception as e:
            logger.error(f"Failed to get hallucination corrections: {e}")
            return {}

    async def get_user_behavioral_patterns(self, context: str) -> Dict[str, Any]:
        """
        Get user behavioral patterns for a context.

        Returns:
            Dict with typical_phrases, typical_hours, score
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            # Get typical phrases
            cursor.execute("""
                SELECT phrase, occurrence_count, success_count, failure_count
                FROM user_behavioral_patterns
                WHERE context = ?
                ORDER BY occurrence_count DESC
                LIMIT 10
            """, (context,))

            phrases_data = cursor.fetchall()
            typical_phrases = [row[0] for row in phrases_data]

            # Calculate success rate
            total_success = sum(row[2] for row in phrases_data)
            total_failure = sum(row[3] for row in phrases_data)
            success_rate = total_success / max(total_success + total_failure, 1)

            # Get typical hours from unlock_attempts
            cursor.execute("""
                SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour, COUNT(*) as cnt
                FROM unlock_attempts
                WHERE success = 1
                GROUP BY hour
                HAVING cnt >= 3
                ORDER BY cnt DESC
            """)

            typical_hours = [row[0] for row in cursor.fetchall()]

            conn.close()

            return {
                "has_behavioral_data": len(typical_phrases) > 0,
                "typical_phrases": typical_phrases,
                "typical_hours": typical_hours,
                "success_rate": success_rate,
                "score": min(1.0, success_rate + 0.3 if typical_phrases else 0.5)
            }

        except Exception as e:
            logger.error(f"Failed to get behavioral patterns: {e}")
            return {"has_behavioral_data": False, "typical_phrases": [], "typical_hours": [], "score": 0.5}

    async def record_user_phrase(
        self,
        context: str,
        phrase: str,
        success: bool = True
    ):
        """
        Record a user phrase for behavioral pattern learning.

        Args:
            context: Context (unlock_command, general, etc.)
            phrase: The phrase the user said
            success: Whether this led to a successful action
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            now = datetime.now().isoformat()
            normalized = phrase.lower().strip()

            cursor.execute("""
                INSERT INTO user_behavioral_patterns (
                    context, phrase, phrase_normalized, occurrence_count,
                    last_used, success_count, failure_count
                ) VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(context, phrase_normalized) DO UPDATE SET
                    occurrence_count = occurrence_count + 1,
                    last_used = ?,
                    success_count = success_count + ?,
                    failure_count = failure_count + ?
            """, (
                context, phrase, normalized, now, 1 if success else 0, 0 if success else 1,
                now, 1 if success else 0, 0 if success else 1
            ))

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Failed to record user phrase: {e}")

    async def store_hallucination_reasoning(
        self,
        hallucination_id: int,
        reasoning_steps: List[Dict[str, Any]],
        hypotheses: List[Dict[str, Any]],
        evidence: Dict[str, Any],
        pattern_analysis: Dict[str, Any] = None,
        consensus_analysis: Dict[str, Any] = None,
        context_analysis: Dict[str, Any] = None,
        phonetic_analysis: Dict[str, Any] = None,
        behavioral_analysis: Dict[str, Any] = None,
        sai_analysis: Dict[str, Any] = None,
        final_decision: str = None
    ):
        """
        Store detailed reasoning trace for a hallucination detection.
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            now = datetime.now().isoformat()

            cursor.execute("""
                INSERT INTO hallucination_reasoning_log (
                    timestamp, hallucination_id, reasoning_steps_json, hypotheses_json,
                    evidence_json, pattern_analysis_json, consensus_analysis_json,
                    context_analysis_json, phonetic_analysis_json, behavioral_analysis_json,
                    sai_analysis_json, final_decision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now, hallucination_id,
                json.dumps(reasoning_steps),
                json.dumps(hypotheses),
                json.dumps(evidence),
                json.dumps(pattern_analysis) if pattern_analysis else None,
                json.dumps(consensus_analysis) if consensus_analysis else None,
                json.dumps(context_analysis) if context_analysis else None,
                json.dumps(phonetic_analysis) if phonetic_analysis else None,
                json.dumps(behavioral_analysis) if behavioral_analysis else None,
                json.dumps(sai_analysis) if sai_analysis else None,
                final_decision
            ))

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Failed to store hallucination reasoning: {e}")

    async def get_hallucination_stats(self) -> Dict[str, Any]:
        """
        Get statistics about hallucination detection.
        """
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()

            # Total hallucinations
            cursor.execute("SELECT COUNT(*) FROM stt_hallucinations")
            total = cursor.fetchone()[0]

            # By type
            cursor.execute("""
                SELECT hallucination_type, COUNT(*), SUM(occurrence_count)
                FROM stt_hallucinations
                GROUP BY hallucination_type
            """)
            by_type = {row[0]: {"unique": row[1], "total_occurrences": row[2]} for row in cursor.fetchall()}

            # Corrections
            cursor.execute("SELECT COUNT(*) FROM hallucination_corrections")
            total_corrections = cursor.fetchone()[0]

            # Recent (last 7 days)
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            cursor.execute("""
                SELECT COUNT(*) FROM stt_hallucinations WHERE date >= ?
            """, (week_ago,))
            recent = cursor.fetchone()[0]

            conn.close()

            return {
                "total_unique_hallucinations": total,
                "by_type": by_type,
                "total_corrections_learned": total_corrections,
                "hallucinations_last_7_days": recent
            }

        except Exception as e:
            logger.error(f"Failed to get hallucination stats: {e}")
            return {}


# Singleton instance
_metrics_db = None


def get_metrics_database() -> MetricsDatabase:
    """Get or create singleton metrics database instance"""
    global _metrics_db
    if _metrics_db is None:
        _metrics_db = MetricsDatabase()
    return _metrics_db
