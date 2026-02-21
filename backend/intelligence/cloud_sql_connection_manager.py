#!/usr/bin/env python3
"""
Singleton CloudSQL Connection Manager for JARVIS v3.1
======================================================

Production-grade, fully async, thread-safe connection pool manager with:
- Automatic leak detection and IMMEDIATE recovery
- Smart adaptive cleanup (aggressive mode on leak detection)
- Circuit breaker pattern for failure handling
- Background cleanup tasks for orphan connection termination
- Comprehensive metrics and observability
- Signal-aware graceful shutdown (SIGINT, SIGTERM, atexit)
- Strict connection limits for db-f1-micro (max 3 connections)
- Dynamic configuration via environment variables
- Fully async lock patterns for concurrent safety

v3.1 Changes:
- Fixed singleton initialization race condition with proper double-checked locking
- Added retry logic for asyncpg TLS race condition (InvalidStateError)
- Fixed AsyncLock lazy creation race condition
- Added serialized connection setup to prevent thundering herd during pool creation
- Added specific error handling for TLS upgrade protocol failures

Architecture:
┌─────────────────────────────────────────────────────────────────────────────┐
│                  CloudSQL Connection Manager v3.1                            │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐                 │
│  │ Connection     │  │ Leak Detector  │  │ Circuit        │                 │
│  │ Pool (asyncpg) │  │ & Auto-Cleaner │  │ Breaker        │                 │
│  └───────┬────────┘  └───────┬────────┘  └───────┬────────┘                 │
│          └───────────────────┼───────────────────┘                          │
│                              ▼                                               │
│                 ┌─────────────────────────────┐                             │
│                 │    Connection Coordinator   │                             │
│                 │  • Lifecycle management     │                             │
│                 │  • Immediate leak cleanup   │                             │
│                 │  • Adaptive monitoring      │                             │
│                 │  • Dynamic configuration    │                             │
│                 └─────────────────────────────┘                             │
└─────────────────────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 3.1.0
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import time
import traceback
import threading
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Union, TypeVar

from backend.utils.env_config import get_env_int, get_env_float, get_env_bool

try:
    import asyncpg
    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False

# v9.0: Rate limit manager integration for CloudSQL Admin API
RATE_LIMIT_MANAGER_AVAILABLE = False
try:
    from core.gcp_rate_limit_manager import (
        get_rate_limit_manager_sync,
        GCPService,
        OperationType,
    )
    RATE_LIMIT_MANAGER_AVAILABLE = True
except ImportError:
    pass

# v10.0: Intelligent Rate Orchestrator for ML-powered rate limiting
INTELLIGENT_RATE_ORCHESTRATOR_AVAILABLE = False
try:
    from core.intelligent_rate_orchestrator import (
        get_rate_orchestrator,
        ServiceType as RateServiceType,
        OperationType as RateOpType,
        RequestPriority,
    )
    INTELLIGENT_RATE_ORCHESTRATOR_AVAILABLE = True
except ImportError:
    try:
        from backend.core.intelligent_rate_orchestrator import (
            get_rate_orchestrator,
            ServiceType as RateServiceType,
            OperationType as RateOpType,
            RequestPriority,
        )
        INTELLIGENT_RATE_ORCHESTRATOR_AVAILABLE = True
    except ImportError:
        pass

# v5.0: Cloud SQL Proxy Detector for intelligent proxy availability detection
PROXY_DETECTOR_AVAILABLE = False
try:
    from intelligence.cloud_sql_proxy_detector import (
        get_proxy_detector,
        ProxyStatus,
    )
    PROXY_DETECTOR_AVAILABLE = True
except ImportError:
    try:
        from backend.intelligence.cloud_sql_proxy_detector import (
            get_proxy_detector,
            ProxyStatus,
        )
        PROXY_DETECTOR_AVAILABLE = True
    except ImportError:
        pass

# v4.0: Enterprise connection management components
ENTERPRISE_CONNECTION_AVAILABLE = False
try:
    from backend.core.connection import (
        AtomicCircuitBreaker,
        CircuitBreakerConfig,
        ProactiveProxyDetector,
        ProxyStatus as EnterpriseProxyStatus,
        EventLoopAwareLock,
        CircuitState as EnterpriseCircuitState,
    )
    ENTERPRISE_CONNECTION_AVAILABLE = True
except ImportError:
    pass

logger = logging.getLogger(__name__)

# Type variable for generic async operations
T = TypeVar('T')

# Tuple type for type hints
from typing import Tuple

# JSON for config loading
import json
from pathlib import Path
import random


# v260.4: Safe env var parsing helper — prevents ValueError from killing coroutines
def _get_env_float_safe(key: str, default: float) -> float:
    """Parse env var as float with safe fallback.

    If the env var is unset, empty, or unparseable, returns `default` silently.
    This prevents a misconfigured env var from crashing long-lived coroutines
    (e.g. _recheck_loop) before the while loop even starts.
    """
    try:
        val = os.environ.get(key, "")
        return float(val) if val else default
    except (ValueError, TypeError):
        return default


# =============================================================================
# PROXY READINESS GATE v1.0 - Production-Grade Implementation
# =============================================================================
# Single source of truth for Cloud SQL proxy + DB readiness.
# Addresses 30 edge cases for production robustness.
# =============================================================================

# -----------------------------------------------------------------------------
# Process-Wide TLS Semaphore Registry (Edge Case #6, #17)
# -----------------------------------------------------------------------------
# Prevents asyncpg TLS race conditions across all connection creators.
# Keyed by (host, port) so all connections to same proxy share serialization.
# -----------------------------------------------------------------------------

_tls_semaphore_registry: Dict[str, asyncio.Semaphore] = {}
_tls_registry_lock = threading.Lock()
_tls_semaphore_loops: Dict[str, int] = {}  # Track which loop created each semaphore

# v133.0: Global connection sequence tracker for strict serialization
_tls_connection_sequence: int = 0
_tls_sequence_lock = threading.Lock()

# v133.0: Track active TLS operations to detect concurrent attempts
_active_tls_operations: Dict[str, float] = {}  # key -> start_time
_tls_operation_timeout = 30.0  # Max time for a single TLS operation


def _get_next_connection_sequence() -> int:
    """Get next connection sequence number for strict ordering."""
    global _tls_connection_sequence
    with _tls_sequence_lock:
        _tls_connection_sequence += 1
        return _tls_connection_sequence


async def get_tls_semaphore(host: str, port: int) -> asyncio.Semaphore:
    """
    Get or create TLS semaphore for a (host, port) pair.

    v133.0 Enhancements:
    - Detects and warns on concurrent TLS operations
    - Cleans up stale operation tracking
    - Provides detailed logging for debugging race conditions

    Edge Cases Addressed:
    - #6: Process-wide registry keyed by (host, port)
    - #17: Created only in async context with get_running_loop()
    - #29: Tracks loop affinity for multi-loop support
    - v133.0: Detects concurrent TLS attempts even with semaphore

    Must be called from async context to ensure proper loop binding.
    """
    key = f"{host}:{port}"
    loop = asyncio.get_running_loop()
    loop_id = id(loop)

    with _tls_registry_lock:
        # v133.0: Clean up stale TLS operations (older than timeout)
        now = time.time()
        stale_keys = [
            k for k, start_time in _active_tls_operations.items()
            if now - start_time > _tls_operation_timeout
        ]
        for stale_key in stale_keys:
            del _active_tls_operations[stale_key]
            logger.warning(f"[TLS Registry] Cleaned up stale TLS operation: {stale_key}")

        # Check if semaphore exists and is for current loop
        if key in _tls_semaphore_registry:
            if _tls_semaphore_loops.get(key) == loop_id:
                return _tls_semaphore_registry[key]
            # Different loop - need new semaphore
            logger.debug(f"[TLS Registry] Semaphore for {key} was for different loop, recreating")

        # Create new semaphore for current loop
        semaphore = asyncio.Semaphore(1)
        _tls_semaphore_registry[key] = semaphore
        _tls_semaphore_loops[key] = loop_id
        logger.debug(f"[TLS Registry] Created semaphore for {key} on loop {loop_id}")
        return semaphore


async def _mark_tls_operation_start(host: str, port: int, sequence: int) -> None:
    """v133.0: Mark that a TLS operation is starting."""
    key = f"{host}:{port}:seq{sequence}"
    with _tls_registry_lock:
        _active_tls_operations[key] = time.time()


async def _mark_tls_operation_end(host: str, port: int, sequence: int) -> None:
    """v133.0: Mark that a TLS operation has ended."""
    key = f"{host}:{port}:seq{sequence}"
    with _tls_registry_lock:
        _active_tls_operations.pop(key, None)


# =============================================================================
# v132.0: TLS-Safe Connection Factory - UNIFIED ENTRY POINT FOR ALL ASYNCPG
# =============================================================================
# ALL asyncpg connections in the JARVIS ecosystem MUST use these factory
# functions to prevent TLS race conditions (InvalidStateError).
#
# The asyncpg TLS bug occurs when multiple connections attempt TLS upgrade
# simultaneously, corrupting the internal state machine. These factories
# serialize all TLS operations using the process-wide semaphore registry.
#
# DO NOT call asyncpg.connect() or asyncpg.create_pool() directly anywhere
# in the codebase - always use these factory functions instead!
# =============================================================================

async def tls_safe_connect(
    host: str = '127.0.0.1',
    port: int = 5432,
    database: str = 'postgres',
    user: str = 'postgres',
    password: str = '',
    timeout: float = 30.0,
    max_retries: int = 5,
    **kwargs
) -> Optional['asyncpg.Connection']:
    """
    v133.0: TLS-Safe Connection Factory - Robust race condition protection.

    This is the ONLY safe way to create asyncpg connections in JARVIS.
    All other methods bypass TLS serialization and may cause InvalidStateError.

    v133.0 ROOT CAUSE FIX for asyncpg TLS InvalidStateError:
    The error occurs when asyncpg's internal TLSUpgradeProto receives data
    callbacks after the Future has already been resolved. This can happen when:
    1. Network jitter causes data to arrive in unexpected order
    2. Multiple connections race through the semaphore due to async gaps
    3. The TCP transport receives data while TLS state machine is transitioning

    Our solution:
    1. Process-wide sequence numbers ensure strict connection ordering
    2. Extended settling time (100ms) between TLS operations
    3. Dedicated cleanup coroutine runs between attempts to drain any pending callbacks
    4. Connection verification in a separate try/except to catch delayed failures
    5. Explicit garbage collection hint after failed attempts

    Args:
        host: Database host (default: 127.0.0.1 for Cloud SQL proxy)
        port: Database port (default: 5432)
        database: Database name
        user: Database user
        password: Database password
        timeout: Connection timeout in seconds (default: 30s)
        max_retries: Maximum retries on TLS errors (default: 5)
        **kwargs: Additional arguments passed to asyncpg.connect()

    Returns:
        asyncpg.Connection or None if connection failed after all retries
    """
    if not ASYNCPG_AVAILABLE:
        logger.warning("[TLS Factory v133.0] asyncpg not available")
        return None

    last_error = None
    conn = None

    for attempt in range(max_retries):
        # v133.0: Get unique sequence number for strict ordering
        sequence = _get_next_connection_sequence()

        try:
            # Get the process-wide TLS semaphore for this (host, port)
            tls_semaphore = await get_tls_semaphore(host, port)

            async with tls_semaphore:
                # v133.0: Mark operation start for monitoring
                await _mark_tls_operation_start(host, port, sequence)

                try:
                    # v133.0: Extended settling time (100ms instead of 50ms)
                    # This allows any pending TLS callbacks to drain
                    await asyncio.sleep(0.1)

                    # v133.0: Yield to event loop multiple times to ensure
                    # all pending callbacks are processed before we start
                    for _ in range(3):
                        await asyncio.sleep(0)

                    # Create the connection with timeout
                    conn = await asyncio.wait_for(
                        asyncpg.connect(
                            host=host,
                            port=port,
                            database=database,
                            user=user,
                            password=password,
                            **kwargs
                        ),
                        timeout=timeout
                    )

                    # v133.0: Brief pause after connection to let TLS state settle
                    await asyncio.sleep(0.05)

                    # Verify connection is actually usable in separate try block
                    # This catches delayed InvalidStateError that may occur after
                    # the connection appears to be established
                    try:
                        await conn.execute("SELECT 1")
                    except asyncio.InvalidStateError as verify_error:
                        # TLS corruption detected during verification
                        logger.warning(
                            f"[TLS Factory v133.0] InvalidStateError during verification "
                            f"(seq={sequence}): {verify_error}"
                        )
                        try:
                            await conn.close()
                        except Exception:
                            pass
                        conn = None
                        raise verify_error

                    logger.debug(
                        f"[TLS Factory v133.0] Connection created (seq={sequence}, "
                        f"attempt {attempt + 1}/{max_retries})"
                    )
                    return conn

                finally:
                    # v133.0: Always mark operation end
                    await _mark_tls_operation_end(host, port, sequence)

        except asyncio.InvalidStateError as e:
            # TLS race condition - the exact bug we're protecting against
            last_error = e
            logger.warning(
                f"[TLS Factory v133.0] TLS InvalidStateError (seq={sequence}, "
                f"attempt {attempt + 1}/{max_retries}): {e}"
            )

            # v133.0: Clean up any partial connection state
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
                conn = None

            if attempt < max_retries - 1:
                # v133.0: Exponential backoff with higher base delay
                delay = (2 ** attempt) * 1.0 * (0.5 + random.random())
                logger.info(f"[TLS Factory v133.0] Waiting {delay:.2f}s before retry...")
                await asyncio.sleep(delay)

                # v133.0: Multiple yields to fully drain event loop
                for _ in range(5):
                    await asyncio.sleep(0)

                # v133.0: Hint to garbage collector to clean up corrupted state
                import gc
                gc.collect(generation=0)

        except asyncio.TimeoutError:
            last_error = asyncio.TimeoutError(f"Connection timeout after {timeout}s")
            logger.warning(
                f"[TLS Factory v133.0] Connection timeout (seq={sequence}, "
                f"attempt {attempt + 1}/{max_retries})"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(1.0)

        except (OSError, ConnectionError) as e:
            # Network errors - may need proxy to start
            last_error = e
            logger.warning(
                f"[TLS Factory v133.0] Connection error (seq={sequence}, "
                f"attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep((attempt + 1) * 1.0)

        except Exception as e:
            # v133.0: Check for wrapped TLS errors that may come through
            # different exception paths (e.g., from uvloop internals)
            error_str = str(e).lower()
            is_tls_error = (
                "invalid state" in error_str or
                "tlsupgrade" in error_str or
                "tls" in error_str and "error" in error_str or
                "ssl" in error_str and "state" in error_str
            )

            if is_tls_error:
                last_error = e
                logger.warning(
                    f"[TLS Factory v133.0] TLS-related error (seq={sequence}, "
                    f"attempt {attempt + 1}/{max_retries}): {e}"
                )
                if attempt < max_retries - 1:
                    # v133.0: Extended delay for TLS state to clear
                    delay = (2 ** attempt) * 1.0 * (0.5 + random.random())
                    await asyncio.sleep(delay)
                    continue

            # Non-retryable error
            logger.error(
                f"[TLS Factory v133.0] Non-retryable error (seq={sequence}): {e}",
                exc_info=True
            )
            return None

    logger.error(
        f"[TLS Factory v133.0] Connection failed after {max_retries} attempts: {last_error}"
    )
    return None


async def tls_safe_create_pool(
    host: str = '127.0.0.1',
    port: int = 5432,
    database: str = 'postgres',
    user: str = 'postgres',
    password: str = '',
    min_size: int = 1,
    max_size: int = 3,
    timeout: float = 30.0,
    command_timeout: float = 60.0,
    max_inactive_connection_lifetime: float = 300.0,
    max_retries: int = 5,
    pool_creation_timeout: float = 60.0,
    **kwargs
) -> Optional['asyncpg.Pool']:
    """
    v133.0: TLS-Safe Pool Factory - Robust connection pool with race protection.

    This is the ONLY safe way to create asyncpg pools in JARVIS.
    All other methods bypass TLS serialization and may cause InvalidStateError.

    v133.0 ROOT CAUSE FIX for asyncpg TLS InvalidStateError:
    Pool creation is particularly vulnerable because asyncpg opens multiple
    connections simultaneously during create_pool(). Each connection performs
    a TLS handshake, and if they race, the internal state machine corrupts.

    Our solution:
    1. First, verify a single connection works using tls_safe_connect
    2. Create pool with min_size=1 to force sequential connection setup
    3. Use serialized init callback that acquires TLS semaphore per-connection
    4. Verify pool functionality with a test query
    5. Extended settling times between operations

    Args:
        host: Database host (default: 127.0.0.1 for Cloud SQL proxy)
        port: Database port (default: 5432)
        database: Database name
        user: Database user
        password: Database password
        min_size: Minimum pool connections (default: 1)
        max_size: Maximum pool connections (default: 3 for db-f1-micro)
        timeout: Per-connection timeout (default: 30s)
        command_timeout: Query timeout (default: 60s)
        max_inactive_connection_lifetime: Idle connection lifetime (default: 300s)
        max_retries: Maximum retries on TLS errors (default: 5)
        pool_creation_timeout: Overall pool creation timeout (default: 60s)
        **kwargs: Additional arguments passed to asyncpg.create_pool()

    Returns:
        asyncpg.Pool or None if creation failed after all retries
    """
    if not ASYNCPG_AVAILABLE:
        logger.warning("[TLS Factory v133.0] asyncpg not available")
        return None

    # v133.0: Get the process-wide TLS semaphore for this (host, port)
    tls_semaphore = await get_tls_semaphore(host, port)

    last_error = None
    pool = None

    for attempt in range(max_retries):
        # v133.0: Get unique sequence number for this attempt
        sequence = _get_next_connection_sequence()

        try:
            # v133.0: First, verify TLS works with a single connection
            # This catches TLS issues early before pool creation
            if attempt == 0:
                logger.info(
                    f"[TLS Factory v133.0] Testing TLS with single connection before pool (seq={sequence})"
                )
                test_conn = await tls_safe_connect(
                    host=host, port=port, database=database,
                    user=user, password=password,
                    timeout=timeout, max_retries=2,
                    **{k: v for k, v in kwargs.items() if k != 'init'}
                )
                if test_conn:
                    await test_conn.close()
                    logger.debug(f"[TLS Factory v133.0] Single connection test passed (seq={sequence})")
                    # Brief pause to let connection fully close
                    await asyncio.sleep(0.1)
                else:
                    raise ConnectionError("Pre-pool TLS verification failed")

            # v133.0: Create serialized init callback with sequence tracking
            async def serialized_connection_init(conn):
                """
                v133.0: Serialize connection initialization to prevent TLS races.
                Each new connection in the pool goes through this callback.
                """
                init_seq = _get_next_connection_sequence()
                await _mark_tls_operation_start(host, port, init_seq)
                try:
                    async with tls_semaphore:
                        # v133.0: Extended delay (100ms) for TLS state to settle
                        await asyncio.sleep(0.1)
                        # Verify connection is usable
                        try:
                            await conn.execute("SELECT 1")
                        except asyncio.InvalidStateError as verify_err:
                            logger.warning(
                                f"[TLS Factory v133.0] Pool init InvalidStateError (seq={init_seq}): {verify_err}"
                            )
                            raise
                        except Exception as verify_err:
                            logger.debug(f"[TLS Factory v133.0] Pool init verify failed: {verify_err}")
                            raise
                finally:
                    await _mark_tls_operation_end(host, port, init_seq)

            # v133.0: CRITICAL - Always start with min_size=1 to force sequential setup
            initial_min_size = 1

            logger.info(
                f"[TLS Factory v133.0] Creating pool (seq={sequence}, "
                f"attempt {attempt + 1}/{max_retries}, min={initial_min_size}, max={max_size})"
            )

            # Remove 'init' from kwargs if present - we use our own
            kwargs_copy = dict(kwargs)
            kwargs_copy.pop('init', None)

            # v133.0: Acquire semaphore for pool creation itself
            async with tls_semaphore:
                await _mark_tls_operation_start(host, port, sequence)
                try:
                    pool = await asyncio.wait_for(
                        asyncpg.create_pool(
                            host=host,
                            port=port,
                            database=database,
                            user=user,
                            password=password,
                            min_size=initial_min_size,
                            max_size=max_size,
                            timeout=timeout,
                            command_timeout=command_timeout,
                            max_inactive_connection_lifetime=max_inactive_connection_lifetime,
                            init=serialized_connection_init,
                            **kwargs_copy
                        ),
                        timeout=pool_creation_timeout
                    )
                finally:
                    await _mark_tls_operation_end(host, port, sequence)

            # v133.0: Brief pause before verification
            await asyncio.sleep(0.05)

            # Verify pool is functional in separate try block
            try:
                async with pool.acquire() as test_conn:
                    await test_conn.execute("SELECT 1")
            except asyncio.InvalidStateError as verify_error:
                logger.warning(
                    f"[TLS Factory v133.0] Pool verification InvalidStateError (seq={sequence}): {verify_error}"
                )
                try:
                    await pool.close()
                except Exception:
                    pass
                pool = None
                raise verify_error

            logger.info(
                f"[TLS Factory v133.0] Pool created successfully (seq={sequence}, "
                f"size={pool.get_size()}, idle={pool.get_idle_size()})"
            )
            return pool

        except asyncio.InvalidStateError as e:
            # TLS race condition - the exact bug we're protecting against
            last_error = e
            logger.warning(
                f"[TLS Factory v133.0] TLS InvalidStateError (seq={sequence}, "
                f"attempt {attempt + 1}/{max_retries}): {e}"
            )

            # v133.0: Clean up partial pool state
            if pool is not None:
                try:
                    await pool.close()
                except Exception:
                    pass
                pool = None

            if attempt < max_retries - 1:
                # v133.0: Extended backoff for pool creation
                delay = (2 ** attempt) * 1.5 * (0.5 + random.random())
                logger.info(f"[TLS Factory v133.0] Waiting {delay:.2f}s before retry...")
                await asyncio.sleep(delay)

                # v133.0: Multiple yields to drain event loop
                for _ in range(5):
                    await asyncio.sleep(0)

                # v133.0: Garbage collection hint
                import gc
                gc.collect(generation=0)
        except asyncio.TimeoutError:
            last_error = asyncio.TimeoutError(f"Pool creation timeout after {pool_creation_timeout}s")
            logger.warning(
                f"[TLS Factory v133.0] Pool creation timeout (seq={sequence}, "
                f"attempt {attempt + 1}/{max_retries})"
            )
            # v133.0: Clean up partial pool on timeout
            if pool is not None:
                try:
                    await pool.close()
                except Exception:
                    pass
                pool = None
            if attempt < max_retries - 1:
                await asyncio.sleep(1.0)

        except (OSError, ConnectionError) as e:
            last_error = e
            logger.warning(
                f"[TLS Factory v133.0] Connection error (seq={sequence}, "
                f"attempt {attempt + 1}/{max_retries}): {e}"
            )
            if pool is not None:
                try:
                    await pool.close()
                except Exception:
                    pass
                pool = None
            if attempt < max_retries - 1:
                await asyncio.sleep((attempt + 1) * 1.0)

        except Exception as e:
            # v133.0: Check for wrapped TLS errors
            error_str = str(e).lower()
            is_tls_error = (
                "invalid state" in error_str or
                "tlsupgrade" in error_str or
                "tls" in error_str and "error" in error_str or
                "ssl" in error_str and "state" in error_str
            )

            if is_tls_error:
                last_error = e
                logger.warning(
                    f"[TLS Factory v133.0] TLS-related error (seq={sequence}, "
                    f"attempt {attempt + 1}/{max_retries}): {e}"
                )
                if pool is not None:
                    try:
                        await pool.close()
                    except Exception:
                        pass
                    pool = None
                if attempt < max_retries - 1:
                    delay = (2 ** attempt) * 1.0 * (0.5 + random.random())
                    await asyncio.sleep(delay)
                    continue

            logger.error(
                f"[TLS Factory v133.0] Non-retryable error (seq={sequence}): {e}",
                exc_info=True
            )
            return None

    logger.error(
        f"[TLS Factory v133.0] Pool creation failed after {max_retries} attempts: {last_error}"
    )
    return None


# -----------------------------------------------------------------------------
# Readiness State and Result Types (Edge Case #30)
# -----------------------------------------------------------------------------

class ReadinessState(Enum):
    """State machine for proxy readiness."""
    UNKNOWN = "unknown"
    CHECKING = "checking"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    DEGRADED_SQLITE = "degraded_sqlite"


@dataclass
class ReadinessResult:
    """
    Result from wait_for_ready() - explicit timeout vs state distinction.

    Edge Case #7: Returns (state, timed_out) tuple via dataclass.
    Edge Case #11: failure_reason distinguishes credential vs proxy failure.
    Edge Case #30: Consistent naming with ReadinessState enum.
    v113.0: Added latency field for connection timing metrics.
    """
    state: ReadinessState
    timed_out: bool
    failure_reason: Optional[str] = None  # "proxy" | "credentials" | "network" | "shutdown" | "config" | "asyncpg_unavailable"
    message: str = ""
    latency: Optional[float] = None  # v113.0: Connection latency in seconds (if measured)

    @property
    def ready(self) -> bool:
        """v113.0: Convenience property to check if CloudSQL is ready."""
        return self.state == ReadinessState.READY and not self.timed_out


class ReadinessTimeoutError(Exception):
    """Raised when wait_for_ready() times out."""
    def __init__(self, result: ReadinessResult):
        self.result = result
        super().__init__(f"Readiness timeout: state={result.state.value}, reason={result.failure_reason}")


# -----------------------------------------------------------------------------
# Configuration Discovery (Edge Cases #9, #10, #24, #25)
# -----------------------------------------------------------------------------

def _discover_database_config_path() -> Optional[Path]:
    """
    Discover database_config.json path using same paths as CloudSQLProxyManager.

    Edge Case #9: Reuses exact same search paths as proxy manager.

    Returns:
        Path to config file, or None if not found.
    """
    search_paths = [
        Path.home() / ".jarvis" / "gcp" / "database_config.json",
        Path(os.getenv("JARVIS_HOME", ".")) / "gcp" / "database_config.json",
        Path("database_config.json"),
    ]

    for path in search_paths:
        if path.exists():
            return path

    return None


def _load_database_config_for_gate() -> Optional[Dict[str, Any]]:
    """
    Load database configuration for ProxyReadinessGate.

    v114.0: Enhanced with Intelligent Credential Resolution System that:
    - Uses SecretManager for multi-source credential retrieval (GCP Secret Manager, Keychain, env)
    - Supports IAM Database Authentication (passwordless, most secure for GCP)
    - Validates credentials exist before returning
    - Falls back through multiple credential sources intelligently

    Edge Cases Addressed:
    - #9: Uses same config path discovery as proxy manager
    - #10: Password from env override then file
    - #25: Same password sourcing as connection manager
    - #26: Returns None (not raises) if config missing/invalid
    - v114.0: Multi-source credential resolution with SecretManager
    - v114.0: IAM authentication support for passwordless access
    - v114.0: Credential validation before use

    Returns:
        Dict with host, port, database, user, password (or iam_auth), or None if unavailable.
    """
    return IntelligentCredentialResolver.get_database_config()


# =============================================================================
# v115.0: Intelligent Credential Resolution System with GCP Bootstrapping
# =============================================================================
# Comprehensive credential management for CloudSQL with:
# - Multi-source credential resolution (GCP Secret Manager, Keychain, env, file)
# - IAM Database Authentication (passwordless, service account based)
# - Credential validation and caching
# - Automatic refresh on authentication failure
# - Cross-repo credential synchronization
# - v115.0: GCP Application Default Credentials bootstrapping
# - v115.0: Event loop safe pool management
# - v115.0: Intelligent retry with fresh credentials
# - v115.0: Proxy-DB connection coordination
# =============================================================================

class CredentialSource(Enum):
    """Credential sources in priority order."""
    IAM_AUTH = "iam_auth"                    # Most secure - no password needed
    GCP_SECRET_MANAGER = "gcp_secret_manager"  # Enterprise-grade secret storage
    ENVIRONMENT_VARIABLE = "environment"      # CI/CD and container deployments
    MACOS_KEYCHAIN = "macos_keychain"        # Local development on macOS
    CONFIG_FILE = "config_file"               # Fallback for local development
    CROSS_REPO_CACHE = "cross_repo_cache"    # Shared across JARVIS Trinity


@dataclass
class CredentialResult:
    """Result from credential resolution."""
    success: bool
    password: Optional[str] = None
    source: Optional[CredentialSource] = None
    use_iam_auth: bool = False
    error: Optional[str] = None
    cached: bool = False
    resolved_at: float = field(default_factory=time.time)
    gcp_credentials_path: Optional[str] = None  # v115.0: Track GCP credentials used


class IntelligentCredentialResolver:
    """
    v115.0: Intelligent multi-source credential resolution system with GCP Bootstrapping.

    Features:
    - Tries multiple credential sources in priority order
    - Supports IAM Database Authentication (most secure)
    - Caches validated credentials with TTL
    - Provides credential validation before use
    - Handles authentication failures with auto-refresh
    - Synchronizes credentials across JARVIS Trinity repos
    - v115.0: GCP Application Default Credentials bootstrapping
    - v115.0: Event loop safe credential operations
    - v115.0: Intelligent retry after credential reload
    - v115.0: Proxy authentication verification

    Priority Order:
    1. IAM Authentication (if enabled and available)
    2. Environment Variables (JARVIS_DB_PASSWORD, CLOUD_SQL_PASSWORD)
    3. GCP Secret Manager (jarvis-db-password secret)
    4. macOS Keychain (jarvis-db-password)
    5. Cross-repo cache (from JARVIS Trinity shared state)
    6. Config file (database_config.json)
    """

    # Class-level cache with TTL
    _credential_cache: Optional[CredentialResult] = None
    _cache_lock = threading.Lock()
    _cache_ttl = float(os.getenv("JARVIS_CREDENTIAL_CACHE_TTL", "300"))  # 5 minutes default
    _last_validation: float = 0
    _validation_ttl = float(os.getenv("JARVIS_CREDENTIAL_VALIDATION_TTL", "60"))  # 1 minute

    # IAM authentication settings
    _iam_auth_enabled = os.getenv("JARVIS_USE_IAM_AUTH", "false").lower() in ("1", "true", "yes")
    _iam_service_account: Optional[str] = os.getenv("JARVIS_IAM_SERVICE_ACCOUNT")

    # v115.0: GCP credentials bootstrapping
    _gcp_credentials_bootstrapped = False
    _gcp_credentials_path: Optional[str] = None
    _retry_count = 0
    _max_credential_retries = int(os.getenv("JARVIS_MAX_CREDENTIAL_RETRIES", "3"))

    @classmethod
    def bootstrap_gcp_credentials(cls) -> bool:
        """
        v115.0: Bootstrap GCP Application Default Credentials.

        This MUST be called before the Cloud SQL proxy starts. It ensures that
        GOOGLE_APPLICATION_CREDENTIALS is set to a valid credentials file.

        Search order:
        1. GOOGLE_APPLICATION_CREDENTIALS env var (if already set and valid)
        2. ~/.jarvis/gcp/service_account.json (project-specific service account)
        3. ~/.config/gcloud/application_default_credentials.json (gcloud CLI auth)
        4. GCP metadata server (if running on GCP)

        Returns:
            True if credentials are available, False otherwise.
        """
        if cls._gcp_credentials_bootstrapped:
            return cls._gcp_credentials_path is not None

        logger.info("[CredentialResolver v115.0] 🔐 Bootstrapping GCP credentials...")

        # Check if already set
        existing_creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if existing_creds and Path(existing_creds).exists():
            cls._gcp_credentials_path = existing_creds
            cls._gcp_credentials_bootstrapped = True
            logger.info(f"[CredentialResolver v115.0] ✅ Using existing GOOGLE_APPLICATION_CREDENTIALS: {existing_creds}")
            return True

        # Search for credentials in priority order
        search_paths = [
            Path.home() / ".jarvis" / "gcp" / "service_account.json",
            Path.home() / ".config" / "gcloud" / "application_default_credentials.json",
            Path("/etc/jarvis/gcp/service_account.json"),  # System-wide
        ]

        for cred_path in search_paths:
            if cred_path.exists():
                try:
                    # Validate it's a valid JSON file
                    with open(cred_path) as f:
                        cred_data = json.load(f)

                    # Check for required fields
                    cred_type = cred_data.get("type", "")
                    if cred_type in ("service_account", "authorized_user"):
                        # Set the environment variable
                        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(cred_path)
                        cls._gcp_credentials_path = str(cred_path)
                        cls._gcp_credentials_bootstrapped = True

                        logger.info(
                            f"[CredentialResolver v115.0] ✅ GCP credentials bootstrapped: "
                            f"{cred_path} (type={cred_type})"
                        )
                        return True

                except json.JSONDecodeError:
                    logger.debug(f"[CredentialResolver v115.0] Invalid JSON: {cred_path}")
                except Exception as e:
                    logger.debug(f"[CredentialResolver v115.0] Error reading {cred_path}: {e}")

        # Check if running on GCP (metadata server available)
        if cls._is_gcp_metadata_available():
            cls._gcp_credentials_bootstrapped = True
            cls._gcp_credentials_path = "metadata_server"
            logger.info("[CredentialResolver v115.0] ✅ Running on GCP - using metadata server credentials")
            return True

        cls._gcp_credentials_bootstrapped = True
        logger.warning(
            "[CredentialResolver v115.0] ⚠️ No GCP credentials found. Cloud SQL proxy may fail to authenticate. "
            "Run 'gcloud auth application-default login' or provide a service account key."
        )
        return False

    @classmethod
    def _is_gcp_metadata_available(cls) -> bool:
        """Check if GCP metadata server is available (running on GCP)."""
        try:
            import urllib.request
            import urllib.error

            req = urllib.request.Request(
                "http://metadata.google.internal/computeMetadata/v1/",
                headers={"Metadata-Flavor": "Google"}
            )
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    @classmethod
    def get_gcp_credentials_path(cls) -> Optional[str]:
        """v115.0: Get the path to GCP credentials being used."""
        if not cls._gcp_credentials_bootstrapped:
            cls.bootstrap_gcp_credentials()
        return cls._gcp_credentials_path

    @classmethod
    def ensure_gcp_credentials(cls) -> bool:
        """
        v115.0: Ensure GCP credentials are available for proxy authentication.

        This is a convenience method that bootstraps if needed.

        Returns:
            True if credentials are available, False otherwise.
        """
        if not cls._gcp_credentials_bootstrapped:
            return cls.bootstrap_gcp_credentials()
        return cls._gcp_credentials_path is not None

    @classmethod
    def get_database_config(cls) -> Optional[Dict[str, Any]]:
        """
        Get database configuration with intelligently resolved credentials.

        Returns:
            Dict with host, port, database, user, and password/iam_auth, or None if unavailable.
        """
        try:
            # Load base config from file
            config_path = _discover_database_config_path()
            if not config_path:
                logger.debug("[CredentialResolver v114.0] No database_config.json found")
                # Try to build config from environment only
                return cls._build_config_from_env()

            with open(config_path) as f:
                config = json.load(f)

            cloud_sql = config.get("cloud_sql", {})

            if not cloud_sql.get("port"):
                logger.debug("[CredentialResolver v114.0] Config missing port")
                return cls._build_config_from_env()

            # Resolve credentials intelligently
            cred_result = cls._resolve_credentials(cloud_sql)

            if not cred_result.success:
                logger.warning(
                    f"[CredentialResolver v114.0] ❌ Credential resolution failed: {cred_result.error}"
                )
                return None

            db_config = {
                "host": os.getenv("JARVIS_DB_HOST", "127.0.0.1"),
                "port": int(os.getenv("JARVIS_DB_PORT", cloud_sql.get("port", 5432))),
                "database": os.getenv("JARVIS_DB_NAME", cloud_sql.get("database", "jarvis_learning")),
                "user": os.getenv("JARVIS_DB_USER", cloud_sql.get("user", "jarvis")),
            }

            # Add authentication method
            if cred_result.use_iam_auth:
                db_config["iam_auth"] = True
                db_config["password"] = None  # No password for IAM auth
                logger.info("[CredentialResolver v114.0] 🔐 Using IAM Database Authentication")
            else:
                db_config["password"] = cred_result.password
                logger.debug(
                    f"[CredentialResolver v114.0] ✅ Credentials resolved via {cred_result.source.value}"
                )

            return db_config

        except Exception as e:
            logger.warning(f"[CredentialResolver v114.0] Config load failed: {e}")
            return None

    @classmethod
    def _build_config_from_env(cls) -> Optional[Dict[str, Any]]:
        """Build config entirely from environment variables."""
        # Check if we have minimum required env vars
        if not os.getenv("JARVIS_DB_PORT") and not os.getenv("CLOUD_SQL_PORT"):
            return None

        cred_result = cls._resolve_credentials({})

        if not cred_result.success:
            return None

        return {
            "host": os.getenv("JARVIS_DB_HOST", "127.0.0.1"),
            "port": int(os.getenv("JARVIS_DB_PORT", os.getenv("CLOUD_SQL_PORT", "5432"))),
            "database": os.getenv("JARVIS_DB_NAME", "jarvis_learning"),
            "user": os.getenv("JARVIS_DB_USER", "jarvis"),
            "password": cred_result.password if not cred_result.use_iam_auth else None,
            "iam_auth": cred_result.use_iam_auth,
        }

    @classmethod
    def _resolve_credentials(cls, cloud_sql_config: Dict[str, Any]) -> CredentialResult:
        """
        v133.0: Resolve credentials from multiple sources in intelligent priority order.

        Priority (source of truth first):
        1. Check cache first (if valid and not invalidated)
        2. IAM Authentication (if enabled - most secure for GCP)
        3. Environment Variables (explicit override)
        4. Config file (SOURCE OF TRUTH - should be tried early)
        5. GCP Secret Manager (enterprise/production - validate against config)
        6. macOS Keychain (local development fallback)
        7. Cross-repo cache (validate against config file to prevent stale credentials)

        v133.0 Changes:
        - Config file is now priority 4 (up from 7) since it's the source of truth
        - Cross-repo cache is validated against config file before use
        - Better logging for debugging credential resolution
        """
        # Check cache first
        with cls._cache_lock:
            if cls._credential_cache and cls._is_cache_valid():
                logger.debug("[CredentialResolver v133.0] Using cached credentials")
                return cls._credential_cache

        # Get config file password for validation (source of truth)
        config_password = cloud_sql_config.get("password")
        logger.debug(f"[CredentialResolver v133.0] Config file has password: {bool(config_password)}")

        # 1. IAM Authentication (most secure for GCP environments)
        if cls._iam_auth_enabled:
            result = cls._try_iam_auth()
            if result.success:
                cls._update_cache(result)
                return result

        # 2. Environment Variables (explicit override - highest priority for manual control)
        result = cls._try_environment_variables()
        if result.success:
            logger.info("[CredentialResolver v133.0] ✅ Using environment variable credentials")
            cls._update_cache(result)
            return result

        # 3. Config file (SOURCE OF TRUTH - try this early)
        result = cls._try_config_file(cloud_sql_config)
        if result.success:
            logger.info("[CredentialResolver v133.0] ✅ Using config file credentials (source of truth)")
            cls._update_cache(result)
            # Also update cross-repo cache with validated credentials
            if result.password:
                cls.save_to_cross_repo_cache(result.password)
            return result

        # 4. GCP Secret Manager (enterprise/production)
        result = cls._try_gcp_secret_manager()
        if result.success:
            # v133.0: Validate Secret Manager password against config if available
            if config_password and result.password != config_password:
                logger.warning(
                    "[CredentialResolver v133.0] ⚠️ Secret Manager password differs from config file - "
                    "config file is source of truth, skipping Secret Manager"
                )
            else:
                logger.info("[CredentialResolver v133.0] ✅ Using GCP Secret Manager credentials")
                cls._update_cache(result)
                return result

        # 5. macOS Keychain (local development fallback)
        result = cls._try_macos_keychain()
        if result.success:
            logger.info("[CredentialResolver v133.0] ✅ Using macOS Keychain credentials")
            cls._update_cache(result)
            return result

        # 6. Cross-repo cache (JARVIS Trinity shared state - validate to prevent stale credentials)
        result = cls._try_cross_repo_cache()
        if result.success:
            # v133.0: Validate cross-repo cache against config file (source of truth)
            if config_password and result.password != config_password:
                logger.warning(
                    "[CredentialResolver v133.0] ⚠️ Cross-repo cache has STALE credentials "
                    "(differs from config file) - invalidating cache"
                )
                cls._invalidate_cross_repo_cache()
                # Don't use stale credentials - fall through
            else:
                logger.info("[CredentialResolver v133.0] ✅ Using cross-repo cache credentials")
                cls._update_cache(result)
                return result

        return CredentialResult(
            success=False,
            error="No valid credentials found from any source (checked: env, config, secret_manager, keychain, cross_repo)"
        )

    @classmethod
    def _is_cache_valid(cls) -> bool:
        """Check if the credential cache is still valid."""
        if not cls._credential_cache:
            return False
        age = time.time() - cls._credential_cache.resolved_at
        return age < cls._cache_ttl

    @classmethod
    def _update_cache(cls, result: CredentialResult) -> None:
        """Update the credential cache."""
        with cls._cache_lock:
            result.cached = True
            cls._credential_cache = result

    @classmethod
    def invalidate_cache(cls) -> None:
        """
        Invalidate the credential cache.

        Call this when authentication fails to force re-resolution.
        """
        with cls._cache_lock:
            cls._credential_cache = None
            logger.info("[CredentialResolver v114.0] Credential cache invalidated")

    @classmethod
    def _try_iam_auth(cls) -> CredentialResult:
        """
        Try IAM Database Authentication.

        This is the most secure method - uses the service account identity
        instead of a password. Requires Cloud SQL IAM authentication to be enabled.
        """
        try:
            # Check if running on GCP with proper service account
            if not cls._is_gcp_environment():
                return CredentialResult(
                    success=False,
                    error="Not running in GCP environment"
                )

            # Verify IAM auth is possible
            # This would normally involve checking if the Cloud SQL instance
            # has IAM authentication enabled
            logger.info("[CredentialResolver v114.0] IAM authentication available")
            return CredentialResult(
                success=True,
                use_iam_auth=True,
                source=CredentialSource.IAM_AUTH
            )

        except Exception as e:
            return CredentialResult(success=False, error=str(e))

    @classmethod
    def _is_gcp_environment(cls) -> bool:
        """Check if running in a GCP environment."""
        # Check for GCP metadata server or service account
        return (
            os.getenv("GOOGLE_CLOUD_PROJECT") is not None or
            os.getenv("GCP_PROJECT") is not None or
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS") is not None or
            cls._iam_service_account is not None
        )

    @classmethod
    def _try_environment_variables(cls) -> CredentialResult:
        """Try to get credentials from environment variables."""
        password = (
            os.getenv("JARVIS_DB_PASSWORD") or
            os.getenv("CLOUD_SQL_PASSWORD") or
            os.getenv("POSTGRES_PASSWORD") or
            os.getenv("DATABASE_PASSWORD")
        )

        if password:
            return CredentialResult(
                success=True,
                password=password,
                source=CredentialSource.ENVIRONMENT_VARIABLE
            )

        return CredentialResult(success=False, error="No password in environment variables")

    @classmethod
    def _try_gcp_secret_manager(cls) -> CredentialResult:
        """Try to get credentials from GCP Secret Manager."""
        try:
            # Import SecretManager lazily
            try:
                from core.secret_manager import get_db_password
            except ImportError:
                try:
                    from backend.core.secret_manager import get_db_password
                except ImportError:
                    return CredentialResult(
                        success=False,
                        error="SecretManager not available"
                    )

            password = get_db_password()
            if password:
                return CredentialResult(
                    success=True,
                    password=password,
                    source=CredentialSource.GCP_SECRET_MANAGER
                )

            return CredentialResult(
                success=False,
                error="No password in GCP Secret Manager"
            )

        except Exception as e:
            return CredentialResult(success=False, error=f"GCP Secret Manager error: {e}")

    @classmethod
    def _try_macos_keychain(cls) -> CredentialResult:
        """Try to get credentials from macOS Keychain."""
        try:
            import subprocess
            import platform

            if platform.system() != "Darwin":
                return CredentialResult(success=False, error="Not on macOS")

            # Try to get from keychain
            result = subprocess.run(
                [
                    "security", "find-generic-password",
                    "-s", "jarvis-db-password",
                    "-w"
                ],
                capture_output=True,
                text=True,
                timeout=5.0
            )

            if result.returncode == 0 and result.stdout.strip():
                return CredentialResult(
                    success=True,
                    password=result.stdout.strip(),
                    source=CredentialSource.MACOS_KEYCHAIN
                )

            return CredentialResult(success=False, error="Not found in Keychain")

        except Exception as e:
            return CredentialResult(success=False, error=f"Keychain error: {e}")

    @classmethod
    def _try_cross_repo_cache(cls) -> CredentialResult:
        """Try to get credentials from cross-repo shared state."""
        try:
            cross_repo_path = Path.home() / ".jarvis" / "cross_repo" / "credentials_cache.json"

            if not cross_repo_path.exists():
                return CredentialResult(success=False, error="No cross-repo cache")

            with open(cross_repo_path) as f:
                cache = json.load(f)

            # Check if cache is still valid
            cached_at = cache.get("cached_at", 0)
            if time.time() - cached_at > cls._cache_ttl:
                return CredentialResult(success=False, error="Cross-repo cache expired")

            password = cache.get("db_password")
            if password:
                return CredentialResult(
                    success=True,
                    password=password,
                    source=CredentialSource.CROSS_REPO_CACHE
                )

            return CredentialResult(success=False, error="No password in cross-repo cache")

        except Exception as e:
            return CredentialResult(success=False, error=f"Cross-repo cache error: {e}")

    @classmethod
    def _try_config_file(cls, cloud_sql_config: Dict[str, Any]) -> CredentialResult:
        """Try to get credentials from config file."""
        password = cloud_sql_config.get("password")
        if password:
            return CredentialResult(
                success=True,
                password=password,
                source=CredentialSource.CONFIG_FILE
            )

        return CredentialResult(success=False, error="No password in config file")

    @classmethod
    def save_to_cross_repo_cache(cls, password: str) -> bool:
        """
        Save validated credentials to cross-repo cache for JARVIS Trinity.

        This allows JARVIS Prime and Reactor Core to use the same validated credentials.
        """
        try:
            cross_repo_dir = Path.home() / ".jarvis" / "cross_repo"
            cross_repo_dir.mkdir(parents=True, exist_ok=True)

            cache_file = cross_repo_dir / "credentials_cache.json"

            # Don't store the actual password - store a reference or encrypted version
            # For now, we'll store metadata only
            cache_data = {
                "cached_at": time.time(),
                "validated": True,
                "source": "jarvis_main",
                "db_user": os.getenv("JARVIS_DB_USER", "jarvis"),
                # Note: In production, use proper encryption
                "db_password": password,  # TODO: Encrypt this
            }

            with open(cache_file, 'w') as f:
                json.dump(cache_data, f)

            # Set restrictive permissions
            cache_file.chmod(0o600)

            logger.debug("[CredentialResolver v133.0] Credentials saved to cross-repo cache")
            return True

        except Exception as e:
            logger.warning(f"[CredentialResolver v133.0] Failed to save cross-repo cache: {e}")
            return False

    @classmethod
    def _invalidate_cross_repo_cache(cls) -> bool:
        """
        v133.0: Invalidate the cross-repo credential cache.

        This should be called when stale credentials are detected to prevent
        other JARVIS Trinity repos from using outdated passwords.
        """
        try:
            cross_repo_path = Path.home() / ".jarvis" / "cross_repo" / "credentials_cache.json"

            if cross_repo_path.exists():
                cross_repo_path.unlink()
                logger.info("[CredentialResolver v133.0] Cross-repo cache invalidated (stale credentials removed)")
                return True
            return False

        except Exception as e:
            logger.warning(f"[CredentialResolver v133.0] Failed to invalidate cross-repo cache: {e}")
            return False

    @classmethod
    async def validate_credentials_async(
        cls,
        db_config: Dict[str, Any],
        timeout: float = 10.0
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate credentials by attempting a test connection.

        Returns:
            Tuple of (success, error_message)
        """
        if not ASYNCPG_AVAILABLE:
            return False, "asyncpg not available"

        try:
            # Get TLS semaphore for serialized connection
            host = db_config.get("host", "127.0.0.1")
            port = db_config.get("port", 5432)
            tls_semaphore = await get_tls_semaphore(host, port)

            async with tls_semaphore:
                conn = await asyncio.wait_for(
                    asyncpg.connect(
                        host=host,
                        port=port,
                        database=db_config.get("database", "jarvis_learning"),
                        user=db_config.get("user", "jarvis"),
                        password=db_config.get("password"),
                        ssl="prefer",
                        timeout=timeout / 2,
                    ),
                    timeout=timeout
                )

                # Test the connection
                await conn.fetchval("SELECT 1")
                await conn.close()

                # Credentials are valid - save to cross-repo cache
                if db_config.get("password"):
                    cls.save_to_cross_repo_cache(db_config["password"])

                return True, None

        except asyncio.TimeoutError:
            return False, "Connection timeout"
        except Exception as e:
            error_str = str(e).lower()
            if "password authentication failed" in error_str:
                # Invalidate cache on auth failure
                cls.invalidate_cache()
                return False, f"Authentication failed: {e}"
            elif "connection refused" in error_str:
                return False, f"Connection refused: {e}"
            else:
                return False, f"Connection error: {e}"

    @classmethod
    def on_authentication_failure(cls) -> None:
        """
        v115.0: Handle authentication failure with intelligent retry tracking.

        This method:
        1. Invalidates the credential cache
        2. Increments retry counter (for rate limiting)
        3. Broadcasts invalidation to cross-repo
        4. Schedules pool invalidation
        5. Returns whether retry is recommended

        Call this when password authentication fails.
        """
        cls._retry_count += 1
        cls.invalidate_cache()

        if cls._retry_count >= cls._max_credential_retries:
            logger.error(
                f"[CredentialResolver v115.0] ❌ Max credential retries ({cls._max_credential_retries}) exceeded. "
                "Manual intervention required. Check: "
                "1) Database password in config/env/secrets "
                "2) Cloud SQL instance name and connection_name "
                "3) GCP project permissions"
            )
        else:
            logger.warning(
                f"[CredentialResolver v115.0] 🔄 Authentication failure #{cls._retry_count} - "
                f"cache invalidated, will try alternate credential sources "
                f"({cls._max_credential_retries - cls._retry_count} retries remaining)"
            )

        # v115.0: Broadcast credential invalidation to cross-repo (fire-and-forget)
        cls._broadcast_credential_invalidated_sync()
        # v115.0: Schedule pool invalidation (will happen on next event loop iteration)
        cls._schedule_pool_invalidation()

    @classmethod
    def reset_retry_count(cls) -> None:
        """v115.0: Reset retry counter after successful authentication."""
        if cls._retry_count > 0:
            logger.info(f"[CredentialResolver v115.0] ✅ Retry count reset (was {cls._retry_count})")
            cls._retry_count = 0

    @classmethod
    def reset_for_new_startup(cls) -> None:
        """
        v116.0: Reset retry count and state for a fresh startup attempt.

        Call this at the START of ensure_proxy_ready() to ensure that
        stale retry counts from previous attempts don't block new connections.

        This fixes the bug where retry counts would persist across restart
        attempts within the same process, causing false "max retries exceeded"
        errors even when credentials are correct.
        """
        old_count = cls._retry_count
        cls._retry_count = 0
        cls._gcp_credentials_bootstrapped = False  # Re-check GCP credentials

        if old_count > 0:
            logger.info(
                f"[CredentialResolver v116.0] 🔄 Startup reset: retry count cleared "
                f"(was {old_count}), GCP credentials will be re-bootstrapped"
            )
        else:
            logger.debug("[CredentialResolver v116.0] Startup reset: state cleared for fresh attempt")

    @classmethod
    def can_retry(cls) -> bool:
        """v115.0: Check if credential retry is allowed."""
        return cls._retry_count < cls._max_credential_retries

    @classmethod
    def get_retry_status(cls) -> Dict[str, Any]:
        """v115.0: Get current retry status for debugging."""
        return {
            "retry_count": cls._retry_count,
            "max_retries": cls._max_credential_retries,
            "can_retry": cls.can_retry(),
            "retries_remaining": max(0, cls._max_credential_retries - cls._retry_count),
        }

    @classmethod
    def _schedule_pool_invalidation(cls) -> None:
        """
        v115.0: Schedule connection pool invalidation with event loop safety.

        This handles the complex event loop coordination required when:
        1. Called from the main event loop - schedules via create_task
        2. Called from a sync context - uses file-based signaling
        3. Called from a background thread - signals via file

        The key insight is that we CANNOT safely close an asyncpg pool from
        a different event loop than the one that created it. Instead, we
        signal the main loop to do the cleanup.
        """
        try:
            # v115.0: Write signal file for cross-loop pool invalidation
            # This is safer than trying to manage event loops across threads
            signal_file = Path.home() / ".jarvis" / "cross_repo" / "pool_invalidation.signal"
            signal_file.parent.mkdir(parents=True, exist_ok=True)

            signal_data = {
                "timestamp": time.time(),
                "reason": "credential_invalidation",
                "version": "115.0",
            }

            # Write atomically
            temp_file = signal_file.with_suffix(".tmp")
            with open(temp_file, 'w') as f:
                json.dump(signal_data, f)
                f.flush()
                os.fsync(f.fileno())
            temp_file.rename(signal_file)

            logger.debug("[CredentialResolver v115.0] Pool invalidation signaled via file")

            # Also try to invalidate directly if we're in the right loop
            try:
                loop = asyncio.get_running_loop()
                # We're in an async context - schedule task on THIS loop
                asyncio.create_task(
                    cls._safe_pool_invalidation(),
                    name="credential_pool_invalidation"
                )
            except RuntimeError:
                # Not in an event loop - signal file will handle it
                pass

        except Exception as e:
            logger.debug(f"[CredentialResolver v115.0] Failed to schedule pool invalidation: {e}")

    @classmethod
    async def _safe_pool_invalidation(cls) -> None:
        """
        v115.0: Safely invalidate connection pools from the correct event loop.

        This method must be called from the same event loop that created the pools.
        """
        try:
            # Get the global connection manager instance
            manager = CloudSQLConnectionManager.get_instance()
            if manager and manager.pool:
                # Check if pool was created in this loop
                try:
                    await manager._close_pool_internal()
                    manager._is_initialized = False
                    logger.info("[CredentialResolver v115.0] ✅ Connection pool invalidated safely")
                except Exception as e:
                    # If close fails due to loop mismatch, mark for recreation
                    manager.pool = None
                    manager._is_initialized = False
                    logger.debug(f"[CredentialResolver v115.0] Pool marked for recreation: {e}")
        except Exception as e:
            logger.debug(f"[CredentialResolver v115.0] Safe pool invalidation error: {e}")

    @classmethod
    def check_pool_invalidation_signal(cls) -> bool:
        """
        v115.0: Check if a pool invalidation was signaled from another context.

        Call this periodically or before acquiring connections to ensure
        pools are invalidated when credentials change.

        Returns:
            True if invalidation was signaled and should be processed.
        """
        try:
            signal_file = Path.home() / ".jarvis" / "cross_repo" / "pool_invalidation.signal"
            if not signal_file.exists():
                return False

            with open(signal_file) as f:
                signal_data = json.load(f)

            # Check if signal is recent (within 30 seconds)
            signal_age = time.time() - signal_data.get("timestamp", 0)
            if signal_age > 30:
                # Stale signal, clean up
                try:
                    signal_file.unlink()
                except Exception:
                    pass
                return False

            # Clean up the signal file
            try:
                signal_file.unlink()
            except Exception:
                pass

            logger.info(
                f"[CredentialResolver v115.0] Pool invalidation signal detected "
                f"(age={signal_age:.1f}s, reason={signal_data.get('reason')})"
            )
            return True

        except Exception as e:
            logger.debug(f"[CredentialResolver v115.0] Error checking pool invalidation signal: {e}")
            return False

    @classmethod
    def _broadcast_credential_invalidated_sync(cls) -> None:
        """
        v114.0: Broadcast credential invalidation synchronously (fire-and-forget).

        Uses a safer file-based approach to avoid file descriptor issues with
        background threads creating event loops. Writes to a signal file that
        other processes can detect.
        """
        try:
            # v114.0: Use a simple file-based signal instead of async broadcast
            # This avoids file descriptor issues with background thread event loops
            signal_file = Path.home() / ".jarvis" / "cross_repo" / "credential_invalidated.signal"
            signal_file.parent.mkdir(parents=True, exist_ok=True)

            # Safely get source value with null checks
            source_value = "unknown"
            if cls._credential_cache and cls._credential_cache.source:
                source_value = cls._credential_cache.source.value

            signal_data = {
                "timestamp": time.time(),
                "source": source_value,
                "reason": "password_authentication_failed",
                "user": os.getenv("JARVIS_DB_USER", "jarvis"),
                "version": "114.0",
            }

            # Write atomically to avoid partial reads
            temp_file = signal_file.with_suffix(".tmp")
            with open(temp_file, 'w') as f:
                json.dump(signal_data, f)
                f.flush()
                os.fsync(f.fileno())

            # Atomic rename
            temp_file.rename(signal_file)

            logger.debug(
                f"[CredentialResolver v114.0] Credential invalidation signaled via file: {signal_file}"
            )

        except Exception as e:
            logger.debug(f"[CredentialResolver v114.0] Failed to signal invalidation: {e}")

    @classmethod
    async def broadcast_credential_validated_async(
        cls,
        source: CredentialSource,
        latency_ms: Optional[float] = None,
    ) -> None:
        """
        v114.0: Broadcast that credentials have been validated across JARVIS Trinity.

        Call this after a successful database connection.
        """
        try:
            try:
                from core.cross_repo_state_initializer import get_cross_repo_initializer
            except ImportError:
                try:
                    from backend.core.cross_repo_state_initializer import get_cross_repo_initializer
                except ImportError:
                    logger.debug("[CredentialResolver v114.0] Cross-repo initializer not available")
                    return

            # get_cross_repo_initializer is async
            initializer = await get_cross_repo_initializer()
            if initializer:
                await initializer.broadcast_credential_validated(
                    source=source.value,
                    user=os.getenv("JARVIS_DB_USER", "jarvis"),
                    latency_ms=latency_ms,
                )
        except Exception as e:
            logger.debug(f"[CredentialResolver v114.0] Failed to broadcast validation: {e}")

    @classmethod
    def check_invalidation_signal(cls) -> Optional[Dict[str, Any]]:
        """
        v114.0: Check if another JARVIS Trinity repo has signaled credential invalidation.

        This allows repos to detect when credentials have been invalidated by another
        process without needing IPC or network communication.

        Returns:
            Signal data if invalidation was signaled recently (within 60s), None otherwise.
        """
        try:
            signal_file = Path.home() / ".jarvis" / "cross_repo" / "credential_invalidated.signal"
            if not signal_file.exists():
                return None

            with open(signal_file) as f:
                signal_data = json.load(f)

            # Check if signal is recent (within 60 seconds)
            signal_age = time.time() - signal_data.get("timestamp", 0)
            if signal_age > 60:
                # Signal is stale, clean it up
                try:
                    signal_file.unlink()
                except Exception:
                    pass
                return None

            logger.info(
                f"[CredentialResolver v114.0] Detected credential invalidation signal "
                f"(age={signal_age:.1f}s, source={signal_data.get('source')})"
            )

            # Invalidate our local cache in response
            cls.invalidate_cache()

            # Clean up the signal file after processing
            try:
                signal_file.unlink()
            except Exception:
                pass

            return signal_data

        except Exception as e:
            logger.debug(f"[CredentialResolver v114.0] Error checking invalidation signal: {e}")
            return None

    @classmethod
    def diagnose_credential_sources(cls) -> Dict[str, Any]:
        """
        v114.0: Diagnose available credential sources.

        Returns a diagnostic report showing which credential sources
        are available and their status. Useful for debugging credential issues.

        Returns:
            Dict with credential source status information.
        """
        report: Dict[str, Any] = {
            "timestamp": time.time(),
            "cache_valid": cls._is_cache_valid(),
            "iam_auth_enabled": cls._iam_auth_enabled,
            "is_gcp_environment": cls._is_gcp_environment(),
            "sources": {}
        }

        # Check each source
        # 1. Environment Variables
        env_password = (
            os.getenv("JARVIS_DB_PASSWORD") or
            os.getenv("CLOUD_SQL_PASSWORD") or
            os.getenv("POSTGRES_PASSWORD") or
            os.getenv("DATABASE_PASSWORD")
        )
        report["sources"]["environment"] = {
            "available": bool(env_password),
            "vars_checked": [
                "JARVIS_DB_PASSWORD",
                "CLOUD_SQL_PASSWORD",
                "POSTGRES_PASSWORD",
                "DATABASE_PASSWORD"
            ]
        }

        # 2. GCP Secret Manager
        try:
            try:
                from core.secret_manager import SecretManager
                report["sources"]["gcp_secret_manager"] = {
                    "available": True,
                    "module": "core.secret_manager"
                }
            except ImportError:
                try:
                    from backend.core.secret_manager import SecretManager
                    report["sources"]["gcp_secret_manager"] = {
                        "available": True,
                        "module": "backend.core.secret_manager"
                    }
                except ImportError:
                    report["sources"]["gcp_secret_manager"] = {
                        "available": False,
                        "error": "SecretManager module not found"
                    }
        except Exception as e:
            report["sources"]["gcp_secret_manager"] = {
                "available": False,
                "error": str(e)
            }

        # 3. macOS Keychain
        import platform
        if platform.system() == "Darwin":
            try:
                import subprocess
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", "jarvis-db-password", "-w"],
                    capture_output=True, text=True, timeout=2.0
                )
                report["sources"]["macos_keychain"] = {
                    "available": result.returncode == 0,
                    "key": "jarvis-db-password"
                }
            except Exception as e:
                report["sources"]["macos_keychain"] = {
                    "available": False,
                    "error": str(e)
                }
        else:
            report["sources"]["macos_keychain"] = {
                "available": False,
                "reason": "Not on macOS"
            }

        # 4. Cross-repo cache
        cross_repo_path = Path.home() / ".jarvis" / "cross_repo" / "credentials_cache.json"
        if cross_repo_path.exists():
            try:
                with open(cross_repo_path) as f:
                    cache = json.load(f)
                cache_age = time.time() - cache.get("cached_at", 0)
                report["sources"]["cross_repo_cache"] = {
                    "available": cache_age < cls._cache_ttl,
                    "path": str(cross_repo_path),
                    "age_seconds": cache_age,
                    "expired": cache_age >= cls._cache_ttl
                }
            except Exception as e:
                report["sources"]["cross_repo_cache"] = {
                    "available": False,
                    "path": str(cross_repo_path),
                    "error": str(e)
                }
        else:
            report["sources"]["cross_repo_cache"] = {
                "available": False,
                "path": str(cross_repo_path),
                "reason": "File does not exist"
            }

        # 5. Config file
        config_path = _discover_database_config_path()
        if config_path:
            try:
                with open(config_path) as f:
                    config = json.load(f)
                has_password = bool(config.get("cloud_sql", {}).get("password"))
                report["sources"]["config_file"] = {
                    "available": has_password,
                    "path": str(config_path),
                    "has_password": has_password
                }
            except Exception as e:
                report["sources"]["config_file"] = {
                    "available": False,
                    "path": str(config_path),
                    "error": str(e)
                }
        else:
            report["sources"]["config_file"] = {
                "available": False,
                "reason": "No database_config.json found"
            }

        # Summary
        available_sources = [
            name for name, status in report["sources"].items()
            if status.get("available")
        ]
        report["summary"] = {
            "total_sources": len(report["sources"]),
            "available_count": len(available_sources),
            "available_sources": available_sources,
            "recommended_action": (
                "Credentials available" if available_sources else
                "Set JARVIS_DB_PASSWORD or configure GCP Secret Manager"
            )
        }

        return report

    @classmethod
    def print_diagnostic_report(cls) -> None:
        """Print a human-readable diagnostic report of credential sources."""
        report = cls.diagnose_credential_sources()

        print("\n" + "=" * 60)
        print("CREDENTIAL SOURCE DIAGNOSTIC REPORT v114.0")
        print("=" * 60)
        print(f"Timestamp: {datetime.fromtimestamp(report['timestamp'])}")
        print(f"Cache Valid: {report['cache_valid']}")
        print(f"IAM Auth Enabled: {report['iam_auth_enabled']}")
        print(f"GCP Environment: {report['is_gcp_environment']}")
        print("-" * 60)

        for source, status in report["sources"].items():
            available = "✅" if status.get("available") else "❌"
            print(f"{available} {source.upper()}")
            for key, value in status.items():
                if key != "available":
                    print(f"    {key}: {value}")

        print("-" * 60)
        print(f"Available Sources: {report['summary']['available_count']}/{report['summary']['total_sources']}")
        print(f"Action: {report['summary']['recommended_action']}")
        print("=" * 60 + "\n")


# -----------------------------------------------------------------------------
# Environment Configuration (Edge Case #13 - no hardcoding)
# -----------------------------------------------------------------------------

def _get_gate_config() -> Dict[str, Any]:
    """
    Load all gate configuration from environment with sensible defaults.

    Edge Case #13: All timeouts/delays externalized to env vars.
    """
    return {
        "readiness_timeout": float(os.getenv("JARVIS_PROXY_READINESS_TIMEOUT", "30.0")),
        "db_check_retries": int(os.getenv("JARVIS_PROXY_DB_CHECK_RETRIES", "5")),
        "db_check_backoff_base": float(os.getenv("JARVIS_PROXY_DB_CHECK_BACKOFF_BASE", "1.0")),
        "db_check_backoff_max": float(os.getenv("JARVIS_PROXY_DB_CHECK_BACKOFF_MAX", "10.0")),
        "db_check_jitter": float(os.getenv("JARVIS_PROXY_DB_CHECK_JITTER", "0.3")),
        "periodic_recheck_interval": float(os.getenv("JARVIS_PROXY_PERIODIC_RECHECK_INTERVAL", "300")),  # 0 = disabled
        "db_check_timeout": float(os.getenv("JARVIS_PROXY_DB_CHECK_TIMEOUT", "5.0")),
    }


# -----------------------------------------------------------------------------
# ProxyReadinessGate - Main Implementation
# -----------------------------------------------------------------------------

class ProxyReadinessGate:
    """
    Single source of truth for Cloud SQL proxy + DB readiness.

    Production-grade implementation addressing 30 edge cases:

    Concurrency & Threading:
    - #1: Single "check in progress" future for concurrent wait_for_ready()
    - #16: asyncio primitives created lazily in async methods
    - #17: TLS semaphore created only in async context
    - #19: Re-check scheduling is idempotent (one in-flight)
    - #29: Events keyed by loop id for multi-loop support

    Configuration:
    - #2: Self-sufficient config loading from database_config.json
    - #9: Reuses proxy manager's config path discovery
    - #10, #25: Password from env override then file
    - #26: Missing/invalid config → UNAVAILABLE, no infinite retry
    - #27: asyncpg unavailable → UNAVAILABLE gracefully

    State Management:
    - #3: READY → invalidation via notify_connection_failed()
    - #4: mark_degraded_sqlite() for SQLite fallback
    - #5: check_db_level() uses one-off asyncpg.connect, not pool
    - #10: No event flapping during re-check from READY
    - #11: Credential vs proxy failure distinction
    - #20: notify_connection_failed() only triggers re-check when READY
    - #21, #22: Lazy event creation handles DEGRADED_SQLITE state

    Events & Subscribers:
    - #6: Process-wide TLS semaphore registry
    - #7: ReadinessResult with explicit timed_out flag
    - #8, #23: Fire-and-forget subscriber callbacks
    - #14: Thread-safe subscriber list
    - #15: subscribe=in-process, cross-repo=HTTP health

    Lifecycle:
    - #12, #28: Shutdown unblocks waiters and cancels recheck task
    - #13: Capped exponential backoff with env config
    - #18: notify_connection_failed() is sync, best-effort

    Type Safety:
    - #30: Consistent ReadinessResult/ReadinessState naming
    """

    _instance: Optional['ProxyReadinessGate'] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> 'ProxyReadinessGate':
        """v113.0: Get the singleton instance of ProxyReadinessGate."""
        return cls()

    def __new__(cls) -> 'ProxyReadinessGate':
        """Singleton pattern with thread-safe initialization."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        """
        Initialize gate state. asyncio primitives created lazily.

        Edge Case #16: No asyncio locks/events in __init__ - created lazily.
        """
        if getattr(self, '_initialized', False):
            return

        # State (thread-safe via _state_lock acquired in async methods)
        self._state = ReadinessState.UNKNOWN
        self._failure_reason: Optional[str] = None
        self._failure_message: str = ""

        # Lazy asyncio primitives (Edge Case #16)
        self._state_lock: Optional[asyncio.Lock] = None
        self._check_lock: Optional[asyncio.Lock] = None

        # Single check-in-progress future (Edge Case #1)
        self._check_future: Optional[asyncio.Future] = None

        # Lazy events per loop (Edge Case #9, #29)
        self._ready_events: Dict[int, asyncio.Event] = {}
        self._degraded_events: Dict[int, asyncio.Event] = {}

        # Subscribers (Edge Case #8, #14)
        self._subscribers: List[Callable[[ReadinessState], Any]] = []
        self._subscriber_lock = threading.Lock()

        # Shutdown flag (Edge Case #12)
        self._shutting_down = False

        # Periodic recheck task (Edge Case #3)
        self._recheck_task: Optional[asyncio.Task] = None

        # Pending recheck flag for sync notify_connection_failed (Edge Case #18)
        self._pending_recheck = False

        # Config cache
        self._db_config: Optional[Dict[str, Any]] = None
        self._gate_config = _get_gate_config()

        # Track if degraded was set before events existed (Edge Case #21)
        self._degraded_before_event = False

        # v117.0: Transient failure tolerance - only signal NOT_READY after consecutive failures
        self._consecutive_health_failures = 0
        self._consecutive_failure_threshold = int(os.getenv("CLOUDSQL_CONSECUTIVE_FAILURES_THRESHOLD", "3"))
        self._startup_time = time.time()
        self._startup_grace_period = float(os.getenv("CLOUDSQL_STARTUP_GRACE_PERIOD", "60.0"))
        self._last_registry_state: Optional[bool] = None  # Track last signaled state to avoid spam

        self._initialized = True
        logger.info("🔐 ProxyReadinessGate v1.0 initialized")

    # -------------------------------------------------------------------------
    # Lazy Primitive Initialization (Edge Cases #16, #17)
    # -------------------------------------------------------------------------

    async def _ensure_locks(self) -> None:
        """Create asyncio locks lazily in async context."""
        loop = asyncio.get_running_loop()

        if self._state_lock is None:
            self._state_lock = asyncio.Lock()
        if self._check_lock is None:
            self._check_lock = asyncio.Lock()

    def _get_ready_event(self, loop: asyncio.AbstractEventLoop) -> asyncio.Event:
        """
        Get or create ready event for the given loop.

        Edge Cases #9, #29: Events keyed by loop id for multi-loop support.
        """
        loop_id = id(loop)
        if loop_id not in self._ready_events:
            event = asyncio.Event()
            # Set if already READY
            if self._state == ReadinessState.READY:
                event.set()
            self._ready_events[loop_id] = event
        return self._ready_events[loop_id]

    def _get_degraded_event(self, loop: asyncio.AbstractEventLoop) -> asyncio.Event:
        """
        Get or create degraded event for the given loop.

        Edge Cases #6, #21: If state is DEGRADED_SQLITE when creating, set it.
        """
        loop_id = id(loop)
        if loop_id not in self._degraded_events:
            event = asyncio.Event()
            # Set if already degraded (Edge Case #21)
            if self._state == ReadinessState.DEGRADED_SQLITE or self._degraded_before_event:
                event.set()
            self._degraded_events[loop_id] = event
        return self._degraded_events[loop_id]

    # -------------------------------------------------------------------------
    # Config Loading (Edge Cases #2, #9, #10, #25, #26)
    # -------------------------------------------------------------------------

    def _ensure_config(self) -> bool:
        """
        Ensure database config is loaded.

        Edge Case #26: Returns False (not raises) if config missing/invalid.
        """
        if self._db_config is not None:
            return True

        self._db_config = _load_database_config_for_gate()
        return self._db_config is not None

    def _reload_config(self) -> bool:
        """
        v114.0: Force reload of database configuration with fresh credentials.

        This is called after a credential failure to re-resolve credentials
        from the IntelligentCredentialResolver (which will now try alternate
        sources since the cache was invalidated).

        Returns:
            True if config was successfully reloaded, False otherwise.
        """
        old_password = self._db_config.get("password", "****") if self._db_config else None
        self._db_config = None  # Clear current config to force reload
        success = self._ensure_config()

        if success and self._db_config:
            new_password = self._db_config.get("password", "****")
            # Check if password actually changed
            if old_password != new_password:
                logger.info(
                    "[ReadinessGate v114.0] 🔄 Credentials reloaded from alternate source"
                )
            else:
                logger.debug(
                    "[ReadinessGate v114.0] Config reloaded (same credentials)"
                )
        else:
            logger.warning(
                "[ReadinessGate v114.0] ❌ Failed to reload credentials from any source"
            )

        return success

    # -------------------------------------------------------------------------
    # Main Public API
    # -------------------------------------------------------------------------

    async def wait_for_ready(
        self,
        timeout: Optional[float] = None,
        raise_on_timeout: bool = False
    ) -> ReadinessResult:
        """
        Wait for DB-level readiness.

        Edge Cases Addressed:
        - #1: Concurrent callers share one check_db_level() run
        - #7: Returns ReadinessResult with explicit timed_out flag
        - #12: Returns immediately if shutting down
        - #16: Locks created lazily
        - #18: Consumes pending recheck flag

        Args:
            timeout: Max wait time in seconds. Default from env.
            raise_on_timeout: If True, raise ReadinessTimeoutError on timeout.

        Returns:
            ReadinessResult with state, timed_out, and failure_reason.
        """
        await self._ensure_locks()
        assert self._check_lock is not None  # Guaranteed by _ensure_locks

        # Edge Case #12: Return immediately if shutting down
        if self._shutting_down:
            return ReadinessResult(
                state=ReadinessState.UNAVAILABLE,
                timed_out=False,
                failure_reason="shutdown",
                message="Gate is shutting down"
            )

        timeout = timeout or self._gate_config["readiness_timeout"]

        # Edge Case #18: Consume pending recheck flag
        if self._pending_recheck:
            self._pending_recheck = False
            if self._state == ReadinessState.READY:
                await self._trigger_recheck()

        # If already in terminal state, return immediately
        if self._state == ReadinessState.READY:
            return ReadinessResult(
                state=ReadinessState.READY,
                timed_out=False,
                message="Proxy ready"
            )
        elif self._state == ReadinessState.DEGRADED_SQLITE:
            return ReadinessResult(
                state=ReadinessState.DEGRADED_SQLITE,
                timed_out=False,
                failure_reason=self._failure_reason,
                message="Using SQLite fallback"
            )

        # Need to check - use shared check future (Edge Case #1)
        async with self._check_lock:
            if self._check_future is None or self._check_future.done():
                # Start new check
                self._check_future = asyncio.create_task(self._run_check())

        # Wait for check to complete or timeout
        try:
            await asyncio.wait_for(
                asyncio.shield(self._check_future),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            result = ReadinessResult(
                state=self._state,
                timed_out=True,
                failure_reason=self._failure_reason or "timeout",
                message=f"Readiness check timed out after {timeout}s"
            )
            if raise_on_timeout:
                raise ReadinessTimeoutError(result)
            return result
        except asyncio.CancelledError:
            return ReadinessResult(
                state=ReadinessState.UNAVAILABLE,
                timed_out=False,
                failure_reason="cancelled",
                message="Wait cancelled"
            )

        return ReadinessResult(
            state=self._state,
            timed_out=False,
            failure_reason=self._failure_reason,
            message=self._failure_message
        )

    async def _run_check(self) -> None:
        """
        Run the actual DB-level check with retries.

        Edge Cases Addressed:
        - #5: Uses one-off asyncpg.connect, not pool
        - #11: Distinguishes credential vs proxy failure
        - #13: Capped exponential backoff
        - #26: Missing config → UNAVAILABLE
        - #27: asyncpg unavailable → UNAVAILABLE
        """
        await self._ensure_locks()
        assert self._state_lock is not None  # Guaranteed by _ensure_locks

        async with self._state_lock:
            if self._state == ReadinessState.READY:
                return  # Already ready
            self._state = ReadinessState.CHECKING

        # Edge Case #27: Check asyncpg availability
        if not ASYNCPG_AVAILABLE:
            await self._set_state(
                ReadinessState.UNAVAILABLE,
                "asyncpg_unavailable",
                "asyncpg not installed"
            )
            return

        # Edge Case #26: Check config availability
        if not self._ensure_config():
            await self._set_state(
                ReadinessState.UNAVAILABLE,
                "config",
                "Database configuration not found"
            )
            return

        # Check for missing password
        if not self._db_config or not self._db_config.get("password"):
            await self._set_state(
                ReadinessState.UNAVAILABLE,
                "credentials",
                "Database password not configured"
            )
            return

        # Run DB-level check with retries (Edge Case #13)
        config = self._gate_config
        max_retries = config["db_check_retries"]
        backoff_base = config["db_check_backoff_base"]
        backoff_max = config["db_check_backoff_max"]
        jitter = config["db_check_jitter"]

        last_error = None
        last_reason = "proxy"

        for attempt in range(max_retries):
            if self._shutting_down:
                await self._set_state(
                    ReadinessState.UNAVAILABLE,
                    "shutdown",
                    "Shutdown during readiness check"
                )
                return

            success, reason = await self._check_db_level()

            if success:
                await self._set_state(
                    ReadinessState.READY,
                    None,
                    "DB-level verification successful"
                )
                # Start periodic recheck if configured
                if config["periodic_recheck_interval"] > 0:
                    self._start_periodic_recheck()
                return

            last_reason = reason or "proxy"

            # v115.0: Handle credential failures with intelligent retry
            if reason == "credentials":
                # Check if we can retry with fresh credentials
                if IntelligentCredentialResolver.can_retry():
                    logger.warning(
                        f"[ReadinessGate v115.0] 🔄 Credential failure - attempting reload "
                        f"({IntelligentCredentialResolver.get_retry_status()['retries_remaining']} retries remaining)"
                    )
                    # Reload config to get fresh credentials from alternate sources
                    if self._reload_config():
                        # Config reloaded successfully, retry immediately
                        logger.info("[ReadinessGate v115.0] ✅ Config reloaded with fresh credentials, retrying...")
                        await asyncio.sleep(0.5)  # Brief pause before retry
                        continue  # Skip backoff, try immediately with new credentials
                    else:
                        # Config reload failed
                        logger.warning("[ReadinessGate v115.0] ❌ Failed to reload credentials from any source")

                # No more retries or reload failed - give up on credentials
                await self._set_state(
                    ReadinessState.UNAVAILABLE,
                    "credentials",
                    f"Authentication failed after credential reload attempts - check credentials"
                )
                return

            # Calculate backoff with cap and jitter (Edge Case #13)
            if attempt < max_retries - 1:
                delay = min(backoff_base * (2 ** attempt), backoff_max)
                delay += random.uniform(0, delay * jitter)
                logger.debug(
                    f"[ReadinessGate] Check attempt {attempt + 1}/{max_retries} failed: {reason}. "
                    f"Retrying in {delay:.2f}s"
                )
                await asyncio.sleep(delay)

        # All retries exhausted
        await self._set_state(
            ReadinessState.UNAVAILABLE,
            last_reason,
            f"DB check failed after {max_retries} attempts"
        )

    async def _check_db_level(self) -> Tuple[bool, Optional[str]]:
        """
        Perform actual PostgreSQL SELECT 1 through proxy.

        Edge Cases Addressed:
        - #5: Uses one-off asyncpg.connect, not pool
        - #6, #17: Uses global TLS semaphore registry
        - #11: Distinguishes credential vs proxy vs network failure

        Returns:
            (success, failure_reason) where failure_reason is None on success
        """
        if not self._db_config:
            return False, "config"

        host = self._db_config["host"]
        port = self._db_config["port"]

        # Get TLS semaphore (Edge Case #6, #17)
        tls_semaphore = await get_tls_semaphore(host, port)

        conn = None
        try:
            async with tls_semaphore:
                # Small delay to let TLS state machine settle
                await asyncio.sleep(0.05)

                conn = await asyncio.wait_for(
                    asyncpg.connect(
                        host=host,
                        port=port,
                        database=self._db_config["database"],
                        user=self._db_config["user"],
                        password=self._db_config["password"],
                    ),
                    timeout=self._gate_config["db_check_timeout"]
                )

                # Actual DB-level verification
                result = await conn.fetchval("SELECT 1")

                if result == 1:
                    logger.info(f"[ReadinessGate v115.0] ✅ DB-level check passed ({host}:{port})")
                    # v115.0: Reset retry count on success
                    IntelligentCredentialResolver.reset_retry_count()
                    # v115.0: Save validated credentials to cross-repo cache
                    if self._db_config.get("password"):
                        IntelligentCredentialResolver.save_to_cross_repo_cache(self._db_config["password"])
                    return True, None
                else:
                    return False, "proxy"

        except asyncio.TimeoutError:
            logger.debug(f"[ReadinessGate] DB check timeout ({host}:{port})")
            return False, "network"

        except Exception as e:
            error_str = str(e).lower()

            # Edge Case #11: Distinguish failure types
            # v116.0: Enhanced error categorization - only count as credential failure
            # if we can verify the proxy is actually running. If proxy isn't running,
            # "password authentication failed" is misleading - it means the proxy
            # couldn't authenticate with GCP, not that the DB password is wrong.
            if "password authentication failed" in error_str:
                # v116.0: Verify proxy is actually running before treating as credential failure
                proxy_running = await self._check_proxy_running()
                if proxy_running:
                    # Proxy is running, so this is a genuine credential failure
                    logger.warning(f"[ReadinessGate v116.0] ❌ Credential failure (proxy verified running): {e}")
                    # v114.0: Invalidate credential cache to force re-resolution
                    IntelligentCredentialResolver.on_authentication_failure()
                    # v114.0: Clear local config so next attempt uses fresh credentials
                    self._db_config = None
                    return False, "credentials"
                else:
                    # Proxy not running - this is a proxy/GCP auth issue, not DB credentials
                    logger.warning(
                        f"[ReadinessGate v116.0] ⚠️ Password auth failed but proxy not running - "
                        f"likely GCP auth issue, not DB credentials: {e}"
                    )
                    # Don't increment credential retry count - this is a proxy issue
                    return False, "proxy"
            elif "connection refused" in error_str or "errno 61" in error_str:
                logger.debug(f"[ReadinessGate] Proxy not available: {e}")
                return False, "proxy"
            elif "timeout" in error_str or "timed out" in error_str:
                logger.debug(f"[ReadinessGate] Network timeout: {e}")
                return False, "network"
            elif "invalid state" in error_str:
                logger.debug(f"[ReadinessGate] TLS race condition: {e}")
                return False, "proxy"
            else:
                logger.debug(f"[ReadinessGate] DB check failed: {e}")
                return False, "proxy"

        finally:
            if conn:
                try:
                    await conn.close()
                except Exception:
                    pass

    async def _set_state(
        self,
        new_state: ReadinessState,
        failure_reason: Optional[str],
        message: str
    ) -> None:
        """
        Set state and manage events.

        Edge Case #10: Don't clear ready event when re-checking from READY.
        """
        old_state = self._state
        self._state = new_state
        self._failure_reason = failure_reason
        self._failure_message = message

        # Manage events based on new state
        if new_state == ReadinessState.READY:
            # Set all ready events
            for event in self._ready_events.values():
                event.set()
            # Clear degraded events
            for event in self._degraded_events.values():
                event.clear()
            self._degraded_before_event = False
            logger.info(f"[ReadinessGate v115.0] ✅ State: READY")

            # v115.0: Proactively signal AgentRegistry if available
            await self._signal_agent_registry_ready(True)

        elif new_state == ReadinessState.UNAVAILABLE:
            # Clear ready events
            for event in self._ready_events.values():
                event.clear()
            logger.warning(f"[ReadinessGate v115.0] ❌ State: UNAVAILABLE ({failure_reason}: {message})")

            # v115.0: Proactively signal AgentRegistry if available
            await self._signal_agent_registry_ready(False)

        elif new_state == ReadinessState.DEGRADED_SQLITE:
            # Clear ready, set degraded (Edge Case #22)
            for event in self._ready_events.values():
                event.clear()
            for event in self._degraded_events.values():
                event.set()
            self._degraded_before_event = True  # Edge Case #21
            logger.info(f"[ReadinessGate] ⚠️ State: DEGRADED_SQLITE")

        # Notify subscribers (Edge Case #8, #23)
        await self._notify_subscribers(new_state)

    # -------------------------------------------------------------------------
    # Invalidation & Re-check (Edge Cases #3, #18, #19, #20)
    # -------------------------------------------------------------------------

    def notify_connection_failed(self, error: Optional[Exception] = None) -> None:
        """
        Called by connection manager on connection failure.
        Triggers re-check if state is READY.

        Edge Cases Addressed:
        - #3: Supports READY → invalidation
        - #18: Sync, best-effort; schedules only if loop exists
        - #19: Idempotent - only one re-check in flight
        - #20: Only triggers when state is READY
        """
        # Edge Case #20: Only re-check when READY
        if self._state != ReadinessState.READY:
            return

        # Edge Case #18: Best-effort scheduling
        try:
            loop = asyncio.get_running_loop()
            # Edge Case #19: Check if already rechecking
            if self._state == ReadinessState.CHECKING:
                return

            # Schedule recheck
            asyncio.create_task(self._trigger_recheck())

        except RuntimeError:
            # No running loop - set flag for next wait_for_ready
            self._pending_recheck = True

    async def _trigger_recheck(self) -> None:
        """
        Trigger a re-check from READY state.

        Edge Case #10: Don't clear ready event until UNAVAILABLE confirmed.
        Edge Case #19: Idempotent via check_lock.

        v260.4: Added retry logic — a single transient failure no longer
        immediately transitions to UNAVAILABLE. Matches the patience of
        _recheck_loop() periodic checks.
        """
        await self._ensure_locks()
        assert self._check_lock is not None  # Guaranteed by _ensure_locks

        # v260.4 (Fix 3 + Fix 7): Retry before declaring UNAVAILABLE
        _max_retries = int(_get_env_float_safe("JARVIS_RECHECK_RETRIES", 2))
        _retry_delay = _get_env_float_safe("JARVIS_RECHECK_RETRY_DELAY", 5.0)

        async with self._check_lock:
            if self._state != ReadinessState.READY:
                return

            # Edge Case #10: Don't clear ready event yet - only on confirmed failure
            self._state = ReadinessState.CHECKING

            success, reason = await self._check_db_level()

            if success:
                self._state = ReadinessState.READY
                return

            # v260.4: Retry before declaring UNAVAILABLE (was single-shot)
            for _retry in range(_max_retries):
                if self._shutting_down:
                    break
                logger.debug(
                    f"[ReadinessGate v260.4] Trigger recheck failed ({reason}), "
                    f"retry {_retry + 1}/{_max_retries} in {_retry_delay * (_retry + 1)}s"
                )
                await asyncio.sleep(_retry_delay * (_retry + 1))
                success, reason = await self._check_db_level()
                if success:
                    self._state = ReadinessState.READY
                    return

            # All retries exhausted
            await self._set_state(
                ReadinessState.UNAVAILABLE,
                reason,
                "Connection failed during operation"
            )

    def _start_periodic_recheck(self) -> None:
        """Start periodic recheck task if configured."""
        if self._recheck_task and not self._recheck_task.done():
            return

        interval = self._gate_config["periodic_recheck_interval"]
        if interval <= 0:
            return

        async def _recheck_loop():
            # v260.4 (Fix 7): Safe env var parsing — bad values won't kill the loop
            _max_retries = int(_get_env_float_safe("JARVIS_RECHECK_RETRIES", 2))
            _retry_delay = _get_env_float_safe("JARVIS_RECHECK_RETRY_DELAY", 5.0)

            # v260.4 (Fix 5): Exponential backoff for recovery probes
            # Starts at _recovery_base, doubles each attempt, caps at _recovery_max.
            # Fast initial probe (10s) when CloudSQL comes back quickly,
            # backs off to 120s to reduce log noise during prolonged outages.
            _recovery_base = _get_env_float_safe("JARVIS_RECHECK_RECOVERY_BASE", 10.0)
            _recovery_max = _get_env_float_safe("JARVIS_RECHECK_RECOVERY_MAX", 120.0)
            _recovery_attempt = 0

            # v260.4 (Fix 6): CancelledError handler — clean shutdown logging
            try:
                while not self._shutting_down:
                    # v260.3: When UNAVAILABLE, probe for recovery instead of
                    # sleeping forever. The old code only ran when state==READY,
                    # so once UNAVAILABLE the loop did nothing.
                    if self._state == ReadinessState.UNAVAILABLE:
                        # v260.4 (Fix 5): Exponential backoff instead of flat 60s
                        _recovery_sleep = min(
                            _recovery_base * (2 ** _recovery_attempt),
                            _recovery_max
                        )
                        await asyncio.sleep(_recovery_sleep)
                        if self._shutting_down:
                            break

                        # v260.4 (Fix 1): Acquire _check_lock to serialize
                        # with _trigger_recheck(). Without this, a concurrent
                        # _trigger_recheck can interleave state transitions
                        # (recovery READY then trigger UNAVAILABLE — events
                        # fire out of order).
                        await self._ensure_locks()
                        assert self._check_lock is not None
                        async with self._check_lock:
                            # Re-check state under lock — may have changed
                            # during sleep or been resolved by _trigger_recheck
                            if self._state != ReadinessState.UNAVAILABLE:
                                _recovery_attempt = 0
                                continue
                            success, reason = await self._check_db_level()
                            if success:
                                _recovery_attempt = 0
                                logger.info(
                                    "[ReadinessGate v260.4] CloudSQL recovered — "
                                    "transitioning to READY"
                                )
                                await self._set_state(
                                    ReadinessState.READY,
                                    None,
                                    "Recovery probe succeeded"
                                )
                            else:
                                _recovery_attempt += 1
                        continue

                    await asyncio.sleep(interval)
                    if self._shutting_down:
                        break

                    if self._state == ReadinessState.READY:
                        # v260.4 (Fix 1): Acquire _check_lock to serialize
                        # periodic checks with _trigger_recheck().
                        await self._ensure_locks()
                        assert self._check_lock is not None
                        async with self._check_lock:
                            # Re-check state under lock
                            if self._state != ReadinessState.READY:
                                continue
                            success, reason = await self._check_db_level()
                            if not success:
                                # v260.3: Retry before declaring UNAVAILABLE
                                for _retry in range(_max_retries):
                                    if self._shutting_down:
                                        break
                                    logger.debug(
                                        f"[ReadinessGate v260.4] Periodic check failed "
                                        f"({reason}), retry {_retry + 1}/{_max_retries} "
                                        f"in {_retry_delay * (_retry + 1)}s"
                                    )
                                    await asyncio.sleep(_retry_delay * (_retry + 1))
                                    success, reason = await self._check_db_level()
                                    if success:
                                        break
                                if not success:
                                    await self._set_state(
                                        ReadinessState.UNAVAILABLE,
                                        reason,
                                        "Periodic health check failed"
                                    )

            except asyncio.CancelledError:
                logger.debug("[ReadinessGate v260.4] Periodic recheck loop cancelled")
            except Exception as e:
                logger.error(
                    f"[ReadinessGate v260.4] Recheck loop crashed: {e}",
                    exc_info=True,
                )

        try:
            self._recheck_task = asyncio.create_task(_recheck_loop())
        except RuntimeError:
            pass  # No running loop

    # -------------------------------------------------------------------------
    # SQLite Fallback (Edge Cases #4, #21, #22)
    # -------------------------------------------------------------------------

    def mark_degraded_sqlite(self) -> None:
        """
        Mark that the system has fallen back to SQLite.

        Edge Cases Addressed:
        - #4: Single call site for DEGRADED_SQLITE transition
        - #21: Sets flag if events don't exist yet
        - #22: Clears ready event, sets degraded event
        """
        self._state = ReadinessState.DEGRADED_SQLITE
        self._failure_reason = "fallback"
        self._failure_message = "Fell back to SQLite"

        # Edge Case #22: Manage events
        for event in self._ready_events.values():
            event.clear()
        for event in self._degraded_events.values():
            event.set()

        # Edge Case #21: Flag for lazy event creation
        self._degraded_before_event = True

        # Notify subscribers
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(self._notify_subscribers(ReadinessState.DEGRADED_SQLITE))
        except RuntimeError:
            pass  # No loop, subscribers will see state on next check

        logger.info("[ReadinessGate] ⚠️ Marked DEGRADED_SQLITE")

    # -------------------------------------------------------------------------
    # Subscribers (Edge Cases #8, #14, #15, #23)
    # -------------------------------------------------------------------------

    def subscribe(self, callback: Callable[[ReadinessState], Any]) -> None:
        """
        Subscribe to state changes (in-process only).

        Edge Cases Addressed:
        - #14: Thread-safe subscriber list
        - #15: Document: subscribe=in-process, cross-repo=HTTP health

        Note: For cross-repo notification, use HTTP health endpoints.
        """
        with self._subscriber_lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[ReadinessState], Any]) -> None:
        """Remove a subscriber."""
        with self._subscriber_lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    # -------------------------------------------------------------------------
    # v112.0: AgentRegistry Integration
    # -------------------------------------------------------------------------

    def setup_agent_registry_integration(
        self,
        agent_registry: Any  # Using Any to avoid import cycles
    ) -> None:
        """
        Set up automatic AgentRegistry integration for CloudSQL dependency tracking.

        v112.0: When CloudSQL proxy readiness state changes, automatically update
        the AgentRegistry's cloudsql dependency state. This prevents CloudSQL-dependent
        agents from being marked offline when CloudSQL is unavailable.

        Args:
            agent_registry: The AgentRegistry instance to integrate with.
                           Must have a set_dependency_ready(name, is_ready) method.

        Example:
            from neural_mesh.registry import AgentRegistry
            from intelligence.cloud_sql_connection_manager import get_proxy_readiness_gate

            registry = AgentRegistry()
            gate = get_proxy_readiness_gate()
            gate.setup_agent_registry_integration(registry)
        """
        if not hasattr(agent_registry, 'set_dependency_ready'):
            logger.warning(
                "[ProxyReadinessGate v112.0] AgentRegistry instance doesn't have "
                "set_dependency_ready method - integration skipped"
            )
            return

        def on_cloudsql_state_change(state: ReadinessState) -> None:
            """
            v117.0: Enhanced callback with transient failure tolerance.

            Uses the same tolerance logic as _signal_agent_registry_ready:
            - Only signals NOT_READY after consecutive failures exceed threshold
            - Implements startup grace period
            - Deduplicates signals
            """
            is_ready = state == ReadinessState.READY

            try:
                # v117.0: Implement transient failure tolerance inline
                if is_ready:
                    self._consecutive_health_failures = 0

                    # Skip if already signaled READY
                    if self._last_registry_state is True:
                        logger.debug("[ProxyReadinessGate v117.0] Already READY - skipping signal")
                        return

                    self._last_registry_state = True
                else:
                    # Increment failure counter
                    self._consecutive_health_failures += 1

                    # Check startup grace period
                    time_since_startup = time.time() - self._startup_time
                    in_grace_period = time_since_startup < self._startup_grace_period
                    effective_threshold = self._consecutive_failure_threshold * (2 if in_grace_period else 1)

                    # Only signal after threshold exceeded
                    if self._consecutive_health_failures < effective_threshold:
                        logger.debug(
                            f"[ProxyReadinessGate v117.0] Transient failure "
                            f"{self._consecutive_health_failures}/{effective_threshold}"
                            f"{' (grace period)' if in_grace_period else ''}"
                        )
                        return

                    # Skip if already signaled NOT_READY
                    if self._last_registry_state is False:
                        logger.debug("[ProxyReadinessGate v117.0] Already NOT_READY - skipping signal")
                        return

                    self._last_registry_state = False
                    logger.warning(
                        f"[ProxyReadinessGate v117.0] Failures exceeded threshold "
                        f"({self._consecutive_health_failures}/{effective_threshold})"
                    )

                # Actually signal the registry
                agent_registry.set_dependency_ready("cloudsql", is_ready)
                logger.info(
                    "[ProxyReadinessGate v117.0] AgentRegistry updated: %s (state: %s)",
                    "READY" if is_ready else "NOT_READY", state.name
                )
            except Exception as e:
                logger.warning(
                    "[ProxyReadinessGate v117.0] Failed to update AgentRegistry: %s", e
                )

        # Subscribe to state changes
        self.subscribe(on_cloudsql_state_change)

        # v117.0: Only set initial state if CloudSQL is actually READY
        # If NOT_READY, don't immediately signal - let the tolerance mechanism handle it
        current_is_ready = self._state == ReadinessState.READY
        try:
            if current_is_ready:
                # CloudSQL is ready - signal immediately
                agent_registry.set_dependency_ready("cloudsql", True)
                self._last_registry_state = True
                logger.info(
                    "[ProxyReadinessGate v117.0] AgentRegistry integration established. "
                    "CloudSQL is READY"
                )
            else:
                # CloudSQL not ready yet - don't signal NOT_READY immediately
                # Let the tolerance mechanism handle transient startup delays
                logger.info(
                    "[ProxyReadinessGate v117.0] AgentRegistry integration established. "
                    "CloudSQL state: %s (will signal after tolerance threshold)",
                    self._state.name
                )
        except Exception as e:
            logger.warning(
                "[ProxyReadinessGate v117.0] Failed to set initial AgentRegistry state: %s", e
            )

    async def _signal_agent_registry_ready(self, is_ready: bool) -> None:
        """
        v117.0: Enhanced AgentRegistry signaling with transient failure tolerance.

        This method updates the AgentRegistry singleton's cloudsql dependency state.

        v117.0 IMPROVEMENTS:
        - Only signals NOT_READY after consecutive failures exceed threshold
        - Implements startup grace period (first 60s uses relaxed tolerance)
        - Tracks and deduplicates signals to avoid log spam
        - Resets failure counter on any success

        This prevents transient network blips or cold start hiccups from
        incorrectly marking CloudSQL-dependent agents as offline.
        """
        try:
            # v117.0: Handle READY state - reset failure counter and signal immediately
            if is_ready:
                self._consecutive_health_failures = 0

                # Only signal if state actually changed
                if self._last_registry_state is True:
                    logger.debug("[ReadinessGate v117.0] CloudSQL already READY - skipping duplicate signal")
                    return

                self._last_registry_state = True

            else:
                # v117.0: NOT_READY - implement transient failure tolerance
                self._consecutive_health_failures += 1

                # Check if we're in startup grace period
                time_since_startup = time.time() - self._startup_time
                in_grace_period = time_since_startup < self._startup_grace_period

                # During grace period, use higher tolerance (2x threshold)
                effective_threshold = self._consecutive_failure_threshold
                if in_grace_period:
                    effective_threshold = self._consecutive_failure_threshold * 2

                # Only signal NOT_READY after threshold consecutive failures
                if self._consecutive_health_failures < effective_threshold:
                    logger.debug(
                        f"[ReadinessGate v117.0] Transient failure {self._consecutive_health_failures}/"
                        f"{effective_threshold} - NOT signaling NOT_READY yet"
                        f"{' (startup grace period)' if in_grace_period else ''}"
                    )
                    return

                # Already signaled NOT_READY? Avoid spam
                if self._last_registry_state is False:
                    logger.debug(
                        f"[ReadinessGate v117.0] CloudSQL already NOT_READY - skipping duplicate signal "
                        f"(failures: {self._consecutive_health_failures})"
                    )
                    return

                self._last_registry_state = False
                logger.warning(
                    f"[ReadinessGate v117.0] CloudSQL health failures exceeded threshold "
                    f"({self._consecutive_health_failures}/{effective_threshold}) - signaling NOT_READY"
                )

            # v116.0: Use the singleton accessor directly
            try:
                from neural_mesh.registry.agent_registry import get_agent_registry
            except ImportError:
                try:
                    from backend.neural_mesh.registry.agent_registry import get_agent_registry
                except ImportError:
                    # AgentRegistry not available - skip
                    logger.debug("[ReadinessGate v117.0] AgentRegistry not available")
                    return

            # Get the singleton registry and update its dependency state
            registry = get_agent_registry()
            registry.set_dependency_ready("cloudsql", is_ready)

            logger.info(
                f"[ReadinessGate v117.0] ✅ AgentRegistry cloudsql dependency updated: "
                f"{'READY' if is_ready else 'NOT_READY'}"
            )

        except Exception as e:
            logger.debug(f"[ReadinessGate v117.0] Could not signal AgentRegistry: {e}")

    # -------------------------------------------------------------------------
    # v113.0: Proactive Proxy Startup System
    # -------------------------------------------------------------------------

    async def ensure_proxy_ready(
        self,
        timeout: Optional[float] = None,
        auto_start: bool = True,
        max_start_attempts: int = 3,
        notify_cross_repo: bool = True,
    ) -> ReadinessResult:
        """
        Proactively ensure the Cloud SQL proxy is running and DB-level ready.

        v113.0: This is the ROOT FIX for "Connection refused" errors. Instead of
        waiting for failures to trigger auto-start, this method proactively:
        1. Checks if proxy is running
        2. Starts it if not (using CloudSQLProxyManager)
        3. Waits for DB-level connectivity (not just TCP port)
        4. Uses intelligent exponential backoff
        5. Notifies cross-repo components when ready

        This should be called during startup BEFORE any CloudSQL-dependent
        components attempt to connect.

        Args:
            timeout: Max time to wait for readiness. Default from env
                    (CLOUDSQL_ENSURE_READY_TIMEOUT, default 60s)
            auto_start: If True, attempt to start proxy if not running
            max_start_attempts: Max proxy start attempts (default 3)
            notify_cross_repo: If True, signal readiness via service registry

        Returns:
            ReadinessResult with state and details

        Example:
            # During startup
            gate = get_proxy_readiness_gate()
            result = await gate.ensure_proxy_ready(timeout=60.0)
            if result.state == ReadinessState.READY:
                print("CloudSQL ready!")
            else:
                print(f"CloudSQL not ready: {result.failure_reason}")
        """
        await self._ensure_locks()

        # v116.0: CRITICAL - Reset credential retry count at startup
        # This fixes the bug where stale retry counts from previous attempts
        # would cause false "max retries exceeded" errors on subsequent startups
        IntelligentCredentialResolver.reset_for_new_startup()

        # v116.0: Bootstrap GCP credentials BEFORE proxy start
        # The proxy needs valid GCP credentials to authenticate with Cloud SQL
        IntelligentCredentialResolver.ensure_gcp_credentials()

        # Get timeout from env (no hardcoding)
        if timeout is None:
            timeout = float(os.environ.get("CLOUDSQL_ENSURE_READY_TIMEOUT", "60.0"))

        start_time = time.time()
        attempts = 0
        last_error: Optional[str] = None

        # Exponential backoff delays (configurable via env)
        base_delay = float(os.environ.get("CLOUDSQL_RETRY_BASE_DELAY", "1.0"))
        max_delay = float(os.environ.get("CLOUDSQL_RETRY_MAX_DELAY", "10.0"))

        logger.info(
            "[ReadinessGate v116.0] 🚀 ensure_proxy_ready() called "
            "(timeout=%.1fs, auto_start=%s, max_attempts=%d)",
            timeout, auto_start, max_start_attempts
        )

        # v3.2: Post-TCP settling delay — proxy needs time after TCP port
        # opens to complete GCP auth before it can handle DB queries.
        # Without this, all 5 DB checks fire against an unauthenticated proxy.
        proxy_settling_delay = float(os.environ.get(
            "CLOUDSQL_PROXY_SETTLING_DELAY", "2.0"
        ))
        # v3.2: Increased timeout cap so all 5 DB retries can actually complete.
        # Old cap of 10s only allowed ~2 retries (5s connect timeout + backoff each).
        db_check_timeout_cap = float(os.environ.get(
            "CLOUDSQL_DB_CHECK_TIMEOUT_CAP", "25.0"
        ))

        just_started_proxy = False

        while (time.time() - start_time) < timeout:
            attempts += 1
            elapsed = time.time() - start_time

            # Step 1: Check if proxy is running (TCP port check)
            proxy_running = await self._check_proxy_running()

            if not proxy_running and auto_start:
                # Step 2: Attempt to start proxy
                start_success = await self._attempt_proxy_start(
                    max_attempts=max_start_attempts,
                    remaining_timeout=timeout - elapsed
                )
                if not start_success:
                    last_error = "proxy_start_failed"
                    # Calculate backoff delay
                    delay = min(base_delay * (2 ** (attempts - 1)), max_delay)
                    logger.debug(
                        "[ReadinessGate v114.0] Proxy start failed, backoff %.1fs (attempt %d)",
                        delay, attempts
                    )
                    await asyncio.sleep(delay)
                    continue
                just_started_proxy = True

            # v3.2: Post-TCP settling delay — give proxy time to complete GCP
            # authentication before attempting DB checks. The proxy opens its TCP
            # port before GCP auth finishes, so immediate DB checks always fail.
            if just_started_proxy:
                just_started_proxy = False
                remaining = timeout - (time.time() - start_time)
                settle = min(proxy_settling_delay, remaining * 0.5)
                if settle > 0:
                    logger.debug(
                        "[ReadinessGate v3.2] Post-TCP settling delay %.1fs "
                        "(proxy needs time for GCP auth)", settle
                    )
                    await asyncio.sleep(settle)
                elapsed = time.time() - start_time

            # v3.2: Reset gate state before DB check attempt — if a previous
            # _run_check() set UNAVAILABLE, we need a fresh check, not a stale result.
            if self._state == ReadinessState.UNAVAILABLE:
                async with self._state_lock:
                    if self._state == ReadinessState.UNAVAILABLE:
                        self._state = ReadinessState.UNKNOWN

            # Step 3: Check DB-level connectivity (the real test)
            result = await self.wait_for_ready(
                timeout=min(db_check_timeout_cap, timeout - elapsed),
                raise_on_timeout=False
            )

            if result.state == ReadinessState.READY:
                logger.info(
                    "[ReadinessGate v114.0] ✅ CloudSQL ready in %.1fs (attempts=%d)",
                    time.time() - start_time, attempts
                )

                # Step 4: Notify cross-repo components with latency info
                if notify_cross_repo:
                    latency_ms = (result.latency or 0) * 1000 if result.latency else None
                    await self._signal_cross_repo_ready(latency_ms=latency_ms)

                return result

            # Not ready yet - backoff and retry
            last_error = result.failure_reason
            delay = min(base_delay * (2 ** (attempts - 1)), max_delay)

            # v114.0: Handle credential failures specially - try to reload from alternate sources
            if result.failure_reason == "credentials":
                logger.warning(
                    "[ReadinessGate v114.0] 🔐 Credential failure detected - "
                    "attempting to reload from alternate sources"
                )
                # The credential cache was already invalidated in _check_db_level()
                # _db_config was also cleared - force reload with fresh credentials
                reload_success = self._reload_config()
                if not reload_success:
                    logger.error(
                        "[ReadinessGate v114.0] ❌ Failed to reload credentials - "
                        "cannot continue without valid credentials. "
                        "Please set JARVIS_DB_PASSWORD or configure credentials in "
                        "GCP Secret Manager, macOS Keychain, or database_config.json"
                    )
                    # Give up on credential failures if we can't get new credentials
                    return ReadinessResult(
                        state=ReadinessState.UNAVAILABLE,
                        timed_out=False,
                        failure_reason="credentials",
                        message="Authentication failed and no alternate credentials available"
                    )
                else:
                    logger.info(
                        "[ReadinessGate v114.0] ✅ Credentials reloaded - retrying connection"
                    )
                    # Use shorter delay for credential retry since we have new creds
                    delay = min(delay, 1.0)

            # Don't sleep if we'll timeout anyway
            if (time.time() - start_time + delay) >= timeout:
                break

            logger.debug(
                "[ReadinessGate v114.0] Not ready (reason=%s), backoff %.1fs (attempt %d)",
                result.failure_reason, delay, attempts
            )
            await asyncio.sleep(delay)

        # Timeout reached
        total_time = time.time() - start_time
        logger.warning(
            "[ReadinessGate v114.0] ⏰ Timeout after %.1fs (attempts=%d, last_error=%s)",
            total_time, attempts, last_error
        )

        return ReadinessResult(
            state=ReadinessState.UNAVAILABLE,
            timed_out=True,
            failure_reason=last_error or "timeout",
            message=f"Could not ensure proxy ready within {timeout}s"
        )

    async def _check_proxy_running(self) -> bool:
        """
        Check if the Cloud SQL proxy process is running (TCP port check).

        v113.0: Quick check before attempting DB-level connectivity.
        """
        if not self._db_config:
            if not self._ensure_config():
                return False

        # v113.1: Safely extract config with proper None handling
        db_config = self._db_config or {}
        host = db_config.get("host", "127.0.0.1")
        port = db_config.get("port", 5432)

        try:
            # Quick TCP connection check
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=2.0
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def _attempt_proxy_start(
        self,
        max_attempts: int = 3,
        remaining_timeout: float = 60.0
    ) -> bool:
        """
        Attempt to start the Cloud SQL proxy using CloudSQLProxyManager.

        v113.0: Centralized proxy startup with proper error handling.
        """
        # Import proxy manager lazily to avoid circular imports
        try:
            try:
                from intelligence.cloud_sql_proxy_manager import get_proxy_manager
            except ImportError:
                from backend.intelligence.cloud_sql_proxy_manager import get_proxy_manager
        except ImportError:
            logger.warning("[ReadinessGate v113.0] CloudSQLProxyManager not available")
            return False

        try:
            proxy_manager = get_proxy_manager()
        except FileNotFoundError as e:
            logger.warning(f"[ReadinessGate v113.0] Proxy config not found: {e}")
            return False
        except Exception as e:
            logger.warning(f"[ReadinessGate v113.0] Failed to get proxy manager: {e}")
            return False

        # Check if already running
        if proxy_manager.is_running():
            logger.debug("[ReadinessGate v116.0] Proxy already running")
            # v116.0: Reset retry count since proxy is confirmed running
            # Any previous credential failures were likely due to proxy issues
            IntelligentCredentialResolver.reset_retry_count()
            return True

        # Attempt start with retries
        logger.info("[ReadinessGate v116.0] 🚀 Starting Cloud SQL proxy...")

        try:
            success = await asyncio.wait_for(
                proxy_manager.start(force_restart=False, max_retries=max_attempts),
                timeout=min(remaining_timeout, 45.0)  # Cap at 45s for proxy start
            )

            if success:
                logger.info("[ReadinessGate v116.0] ✅ Proxy started successfully")
                # v116.0: Reset retry count on successful proxy start
                # Previous credential failures were likely due to proxy not running
                IntelligentCredentialResolver.reset_retry_count()
                return True
            else:
                logger.warning("[ReadinessGate v116.0] ❌ Proxy start returned False")
                return False

        except asyncio.TimeoutError:
            logger.warning("[ReadinessGate v116.0] ⏰ Proxy start timed out")
            return False
        except Exception as e:
            logger.warning(f"[ReadinessGate v116.0] Proxy start error: {e}")
            return False

    async def _signal_cross_repo_ready(self, latency_ms: Optional[float] = None) -> None:
        """
        Signal to cross-repo components that CloudSQL is ready.

        v113.0: Uses service registry to broadcast CloudSQL readiness.
        v113.1: Fixed to use update_supervisor_state with metadata instead of non-existent method.
        v113.2: Also broadcasts via CrossRepoStateInitializer for JARVIS Prime and Reactor Core.
        """
        # Track signaling results
        signaled_via = []

        # Method 1: Service Registry (for local unified state)
        try:
            try:
                from core.service_registry import ServiceRegistry
            except ImportError:
                try:
                    from backend.core.service_registry import ServiceRegistry
                except ImportError:
                    ServiceRegistry = None

            if ServiceRegistry is not None:
                registry = ServiceRegistry()
                import os
                await registry.update_supervisor_state(
                    service_name="cloudsql-proxy",
                    process_alive=True,
                    pid=os.getpid(),
                    metadata={
                        "cloudsql_ready": True,
                        "cloudsql_ready_at": time.time(),
                        "readiness_gate_version": "113.2",
                        "latency_ms": latency_ms,
                    }
                )
                signaled_via.append("service_registry")

        except Exception as e:
            logger.debug(f"[ReadinessGate v113.2] Service registry signaling failed: {e}")

        # Method 2: CrossRepoStateInitializer (for JARVIS Prime and Reactor Core)
        try:
            try:
                from core.cross_repo_state_initializer import (
                    get_cross_repo_initializer,
                )
            except ImportError:
                try:
                    from backend.core.cross_repo_state_initializer import (
                        get_cross_repo_initializer,
                    )
                except ImportError:
                    get_cross_repo_initializer = None

            if get_cross_repo_initializer is not None:
                cross_repo_initializer = await get_cross_repo_initializer()
                await cross_repo_initializer.broadcast_cloudsql_ready(
                    ready=True,
                    latency_ms=latency_ms,
                    metadata={
                        "source": "ProxyReadinessGate",
                        "version": "113.2",
                    }
                )
                signaled_via.append("cross_repo_state")

        except Exception as e:
            logger.debug(f"[ReadinessGate v113.2] Cross-repo state signaling failed: {e}")

        if signaled_via:
            logger.debug(f"[ReadinessGate v113.2] Signaled CloudSQL ready via: {', '.join(signaled_via)}")
        else:
            logger.debug("[ReadinessGate v113.2] No cross-repo signaling methods available")

    async def _notify_subscribers(self, state: ReadinessState) -> None:
        """
        Notify all subscribers of state change.

        Edge Cases Addressed:
        - #8, #23: async via create_task, sync via run_in_executor
        - #14: Thread-safe snapshot of subscriber list
        """
        with self._subscriber_lock:
            subscribers = list(self._subscribers)

        loop = asyncio.get_running_loop()

        for callback in subscribers:
            try:
                if asyncio.iscoroutinefunction(callback):
                    # Async callback - fire and forget
                    asyncio.create_task(self._safe_async_callback(callback, state))
                else:
                    # Sync callback - run in executor to not block loop
                    loop.run_in_executor(None, lambda cb=callback, s=state: self._safe_sync_callback(cb, s))
            except Exception as e:
                logger.debug(f"[ReadinessGate] Error scheduling subscriber: {e}")

    async def _safe_async_callback(self, callback: Callable, state: ReadinessState) -> None:
        """Safely invoke async subscriber."""
        try:
            await callback(state)
        except Exception as e:
            logger.debug(f"[ReadinessGate] Subscriber callback error: {e}")

    def _safe_sync_callback(self, callback: Callable, state: ReadinessState) -> None:
        """Safely invoke sync subscriber."""
        try:
            callback(state)
        except Exception as e:
            logger.debug(f"[ReadinessGate] Subscriber callback error: {e}")

    # -------------------------------------------------------------------------
    # Shutdown (Edge Cases #12, #28)
    # -------------------------------------------------------------------------

    async def shutdown(self) -> None:
        """
        Shutdown gate, unblock waiters, cancel tasks.

        Edge Cases Addressed:
        - #12: Set UNAVAILABLE and unblock waiters
        - #28: Cancel recheck task first, then set state/events
        """
        self._shutting_down = True

        # Edge Case #28: Cancel recheck task first
        if self._recheck_task and not self._recheck_task.done():
            self._recheck_task.cancel()
            try:
                await self._recheck_task
            except asyncio.CancelledError:
                pass

        # Set state to UNAVAILABLE with shutdown reason
        self._state = ReadinessState.UNAVAILABLE
        self._failure_reason = "shutdown"
        self._failure_message = "Gate shutdown"

        # Edge Case #12: Set ready events so waiters wake up
        for event in self._ready_events.values():
            event.set()  # Wake up waiters, they'll see UNAVAILABLE state

        # Notify subscribers
        await self._notify_subscribers(ReadinessState.UNAVAILABLE)

        logger.info("[ReadinessGate] Shutdown complete")

    # -------------------------------------------------------------------------
    # Status & Health (for health endpoints)
    # -------------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """
        Get current gate status for health endpoints.

        Edge Case #15: For cross-repo discovery via HTTP.
        """
        return {
            "state": self._state.value,
            "failure_reason": self._failure_reason,
            "message": self._failure_message,
            "shutting_down": self._shutting_down,
            "db_mode": "sqlite" if self._state == ReadinessState.DEGRADED_SQLITE else "cloudsql",
        }

    @property
    def is_ready(self) -> bool:
        """Quick check if proxy is ready."""
        return self._state == ReadinessState.READY

    @property
    def is_degraded(self) -> bool:
        """Quick check if using SQLite fallback."""
        return self._state == ReadinessState.DEGRADED_SQLITE

    @property
    def state(self) -> ReadinessState:
        """Current state."""
        return self._state


# -----------------------------------------------------------------------------
# Singleton Accessor
# -----------------------------------------------------------------------------

_readiness_gate: Optional[ProxyReadinessGate] = None
_readiness_gate_lock = threading.Lock()


def get_readiness_gate() -> ProxyReadinessGate:
    """Get singleton ProxyReadinessGate instance."""
    global _readiness_gate
    with _readiness_gate_lock:
        if _readiness_gate is None:
            _readiness_gate = ProxyReadinessGate()
        return _readiness_gate


async def get_readiness_gate_async() -> ProxyReadinessGate:
    """Get singleton ProxyReadinessGate instance (async version)."""
    return get_readiness_gate()


# =============================================================================
# Async-Safe Lock Implementation
# =============================================================================

class AsyncLock:
    """
    A lock that works in both sync and async contexts.

    Provides a unified interface for thread-safe operations that can be
    used from both synchronous and asynchronous code.

    v3.1: Fixed lazy async lock creation race condition by using thread lock
    to protect async lock creation.
    """

    def __init__(self):
        self._thread_lock = threading.RLock()
        self._async_lock: Optional[asyncio.Lock] = None
        self._async_lock_creation_lock = threading.Lock()

    def _get_async_lock(self) -> Optional[asyncio.Lock]:
        """
        Lazily create async lock with thread-safe double-checked locking.

        v3.1: Uses separate lock to prevent race condition where multiple
        coroutines could try to create the async lock simultaneously.
        """
        if self._async_lock is None:
            with self._async_lock_creation_lock:
                # Double-check inside lock
                if self._async_lock is None:
                    try:
                        self._async_lock = asyncio.Lock()
                    except RuntimeError:
                        # No event loop available - will be created later
                        pass
        return self._async_lock

    def __enter__(self):
        self._thread_lock.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._thread_lock.release()
        return False

    async def __aenter__(self):
        self._thread_lock.acquire()
        try:
            async_lock = self._get_async_lock()
            if async_lock:
                await async_lock.acquire()
        except Exception:
            self._thread_lock.release()
            raise
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            async_lock = self._get_async_lock()
            if async_lock and async_lock.locked():
                async_lock.release()
        finally:
            self._thread_lock.release()
        return False


# =============================================================================
# Retry Logic with Exponential Backoff
# =============================================================================

class RetryConfig:
    """Configuration for retry behavior."""
    max_retries: int = 3
    base_delay: float = 0.5  # seconds
    max_delay: float = 10.0  # seconds
    exponential_base: float = 2.0
    jitter: bool = True  # Add random jitter to prevent thundering herd


async def retry_with_backoff(
    operation: Callable,
    *args,
    retry_config: Optional[RetryConfig] = None,
    retryable_exceptions: Tuple[type, ...] = (Exception,),
    on_retry: Optional[Callable[[int, Exception], None]] = None,
    **kwargs
) -> T:
    """
    Execute an async operation with exponential backoff retry.

    Args:
        operation: Async callable to execute
        *args: Positional arguments for operation
        retry_config: Retry configuration (uses defaults if None)
        retryable_exceptions: Tuple of exception types to retry on
        on_retry: Optional callback called on each retry (attempt, exception)
        **kwargs: Keyword arguments for operation

    Returns:
        Result of the operation

    Raises:
        Last exception if all retries exhausted
    """
    import random

    config = retry_config or RetryConfig()
    last_exception = None

    for attempt in range(config.max_retries + 1):
        try:
            return await operation(*args, **kwargs)

        except retryable_exceptions as e:
            last_exception = e

            if attempt >= config.max_retries:
                # Exhausted all retries
                logger.error(f"❌ Operation failed after {attempt + 1} attempts: {e}")
                raise

            # Calculate delay with exponential backoff
            delay = min(
                config.base_delay * (config.exponential_base ** attempt),
                config.max_delay
            )

            # Add jitter to prevent thundering herd
            if config.jitter:
                delay = delay * (0.5 + random.random())

            logger.warning(
                f"⚠️ Attempt {attempt + 1}/{config.max_retries + 1} failed: {e}. "
                f"Retrying in {delay:.2f}s..."
            )

            # Call retry callback if provided
            if on_retry:
                try:
                    on_retry(attempt, e)
                except Exception:
                    pass

            await asyncio.sleep(delay)

    # Should never reach here, but just in case
    if last_exception:
        raise last_exception
    raise RuntimeError("Retry logic error - no exception captured")


def is_connection_error(e: Exception) -> bool:
    """
    Check if an exception is a connection-related error that should be retried.

    v3.1: Added InvalidStateError detection for asyncpg TLS race condition.
    This error occurs when multiple connections attempt TLS upgrade simultaneously,
    corrupting the internal state machine in asyncpg's TLSUpgradeProto.
    """
    # v3.1: Check exception type directly for specific asyncpg errors
    if isinstance(e, asyncio.InvalidStateError):
        return True

    error_msg = str(e).lower()
    connection_error_patterns = [
        "connection has been released",
        "connection is closed",
        "pool is closed",
        "connection refused",
        "connection reset",
        "connection timed out",
        "cannot connect",
        "no connection",
        "pool exhausted",
        "server closed",
        "ssl error",
        "network error",
        "connection lost",
        # v3.1: asyncpg TLS race condition errors
        "invalid state",
        "invalidstateerror",
        "tlsupgradeproto",
        "data_received",
        "set_result",
    ]
    return any(pattern in error_msg for pattern in connection_error_patterns)


# =============================================================================
# Dynamic Configuration
# =============================================================================


@dataclass
class ConnectionConfig:
    """
    Dynamic configuration for connection pool.

    All values can be overridden via environment variables:
    - JARVIS_DB_MIN_CONNECTIONS
    - JARVIS_DB_MAX_CONNECTIONS
    - JARVIS_DB_CONNECTION_TIMEOUT
    - JARVIS_DB_QUERY_TIMEOUT
    - JARVIS_DB_POOL_CREATION_TIMEOUT
    - JARVIS_DB_MAX_QUERIES_PER_CONN
    - JARVIS_DB_MAX_IDLE_TIME
    - JARVIS_DB_CHECKOUT_WARNING
    - JARVIS_DB_CHECKOUT_TIMEOUT
    - JARVIS_DB_LEAK_CHECK_INTERVAL
    - JARVIS_DB_LEAKED_IDLE_MINUTES
    - JARVIS_DB_FAILURE_THRESHOLD
    - JARVIS_DB_RECOVERY_TIMEOUT
    - JARVIS_DB_ENABLE_CLEANUP
    - JARVIS_DB_CLEANUP_INTERVAL
    - JARVIS_DB_AGGRESSIVE_CLEANUP
    """

    def __post_init__(self):
        """Load values from environment variables after initialization."""
        self._load_from_env()

    def _load_from_env(self):
        """Load configuration from environment variables."""
        # Pool sizing (conservative for db-f1-micro)
        self.min_connections = get_env_int('JARVIS_DB_MIN_CONNECTIONS', self.min_connections)
        self.max_connections = get_env_int('JARVIS_DB_MAX_CONNECTIONS', self.max_connections)

        # Timeouts
        self.connection_timeout = get_env_float('JARVIS_DB_CONNECTION_TIMEOUT', self.connection_timeout)
        self.startup_connection_timeout = get_env_float('JARVIS_DB_STARTUP_CONNECTION_TIMEOUT', self.startup_connection_timeout)
        self.query_timeout = get_env_float('JARVIS_DB_QUERY_TIMEOUT', self.query_timeout)
        self.pool_creation_timeout = get_env_float('JARVIS_DB_POOL_CREATION_TIMEOUT', self.pool_creation_timeout)

        # Connection lifecycle
        self.max_queries_per_connection = get_env_int('JARVIS_DB_MAX_QUERIES_PER_CONN', self.max_queries_per_connection)
        self.max_idle_time_seconds = get_env_float('JARVIS_DB_MAX_IDLE_TIME', self.max_idle_time_seconds)

        # Leak detection
        self.checkout_warning_seconds = get_env_float('JARVIS_DB_CHECKOUT_WARNING', self.checkout_warning_seconds)
        self.checkout_timeout_seconds = get_env_float('JARVIS_DB_CHECKOUT_TIMEOUT', self.checkout_timeout_seconds)
        self.leak_check_interval_seconds = get_env_float('JARVIS_DB_LEAK_CHECK_INTERVAL', self.leak_check_interval_seconds)
        self.leaked_idle_threshold_minutes = get_env_int('JARVIS_DB_LEAKED_IDLE_MINUTES', self.leaked_idle_threshold_minutes)

        # Circuit breaker
        self.failure_threshold = get_env_int('JARVIS_DB_FAILURE_THRESHOLD', self.failure_threshold)
        self.recovery_timeout_seconds = get_env_float('JARVIS_DB_RECOVERY_TIMEOUT', self.recovery_timeout_seconds)

        # Background tasks
        self.enable_background_cleanup = get_env_bool('JARVIS_DB_ENABLE_CLEANUP', self.enable_background_cleanup)
        self.cleanup_interval_seconds = get_env_float('JARVIS_DB_CLEANUP_INTERVAL', self.cleanup_interval_seconds)

        # Aggressive cleanup mode
        self.aggressive_cleanup_on_leak = get_env_bool('JARVIS_DB_AGGRESSIVE_CLEANUP', self.aggressive_cleanup_on_leak)

    # Pool sizing (conservative for db-f1-micro)
    min_connections: int = 1
    max_connections: int = 3

    # Timeouts (seconds)
    connection_timeout: float = 5.0
    startup_connection_timeout: float = 15.0  # v258.1: longer during startup
    query_timeout: float = 30.0
    pool_creation_timeout: float = 15.0

    # Connection lifecycle
    max_queries_per_connection: int = 10000
    max_idle_time_seconds: float = 180.0  # 3 minutes (more aggressive)

    # Leak detection thresholds (more aggressive)
    checkout_warning_seconds: float = 30.0   # Warn if held > 30s
    checkout_timeout_seconds: float = 120.0  # Force release after 2 min
    leak_check_interval_seconds: float = 15.0  # Check every 15s
    leaked_idle_threshold_minutes: int = 3   # DB connections idle > 3 min (more aggressive)

    # Circuit breaker
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 30.0

    # Background tasks
    enable_background_cleanup: bool = True
    cleanup_interval_seconds: float = 30.0  # Cleanup every 30s (more aggressive)

    # Aggressive cleanup mode - immediately kill leaked connections
    aggressive_cleanup_on_leak: bool = True

    def reload_from_env(self) -> None:
        """Reload configuration from environment variables."""
        self._load_from_env()
        logger.info("🔄 Connection config reloaded from environment")


# =============================================================================
# Connection Tracking
# =============================================================================

@dataclass
class ConnectionCheckout:
    """Tracks a connection checkout for leak detection."""
    checkout_id: int
    checkout_time: datetime
    stack_trace: str
    caller_info: str = ""
    released: bool = False
    release_time: Optional[datetime] = None
    query_count: int = 0

    @property
    def age_seconds(self) -> float:
        return (datetime.now() - self.checkout_time).total_seconds()

    @property
    def is_potentially_leaked(self) -> bool:
        return not self.released and self.age_seconds > 120.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.checkout_id,
            'age_seconds': round(self.age_seconds, 1),
            'released': self.released,
            'caller': self.caller_info,
            'query_count': self.query_count,
        }


@dataclass
class ConnectionMetrics:
    """Comprehensive metrics for monitoring."""
    total_checkouts: int = 0
    total_releases: int = 0
    total_errors: int = 0
    total_timeouts: int = 0
    total_leaks_detected: int = 0
    total_leaks_recovered: int = 0
    total_immediate_cleanups: int = 0
    pool_exhaustion_count: int = 0
    circuit_breaker_trips: int = 0

    avg_checkout_duration_ms: float = 0.0
    max_checkout_duration_ms: float = 0.0
    min_checkout_duration_ms: float = float('inf')

    # Connection health
    healthy_connections: int = 0
    unhealthy_connections: int = 0

    created_at: datetime = field(default_factory=datetime.now)
    last_checkout: Optional[datetime] = None
    last_release: Optional[datetime] = None
    last_error: Optional[datetime] = None
    last_leak_cleanup: Optional[datetime] = None

    def update_checkout_duration(self, duration_ms: float) -> None:
        """Update checkout duration statistics."""
        self.max_checkout_duration_ms = max(self.max_checkout_duration_ms, duration_ms)
        self.min_checkout_duration_ms = min(self.min_checkout_duration_ms, duration_ms)
        n = self.total_releases
        if n > 0:
            self.avg_checkout_duration_ms = (
                (self.avg_checkout_duration_ms * (n - 1) + duration_ms) / n
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'checkouts': self.total_checkouts,
            'releases': self.total_releases,
            'errors': self.total_errors,
            'timeouts': self.total_timeouts,
            'leaks_detected': self.total_leaks_detected,
            'leaks_recovered': self.total_leaks_recovered,
            'immediate_cleanups': self.total_immediate_cleanups,
            'pool_exhaustions': self.pool_exhaustion_count,
            'circuit_breaker_trips': self.circuit_breaker_trips,
            'avg_checkout_ms': round(self.avg_checkout_duration_ms, 2),
            'max_checkout_ms': round(self.max_checkout_duration_ms, 2),
            'min_checkout_ms': round(self.min_checkout_duration_ms, 2) if self.min_checkout_duration_ms != float('inf') else 0,
            'last_checkout': self.last_checkout.isoformat() if self.last_checkout else None,
            'last_error': self.last_error.isoformat() if self.last_error else None,
            'last_leak_cleanup': self.last_leak_cleanup.isoformat() if self.last_leak_cleanup else None,
        }


# =============================================================================
# Circuit Breaker
# =============================================================================

class CircuitState(Enum):
    CLOSED = auto()      # Normal operation
    OPEN = auto()        # Failing, reject requests
    HALF_OPEN = auto()   # Testing recovery


class CircuitBreaker:
    """
    Circuit breaker for connection failures with async support.

    v82.0 Enhancements:
    - Auto-proxy-start: Automatically attempts to start Cloud SQL proxy when
      transitioning from OPEN to HALF_OPEN
    - Connection refused detection: Tracks if failures are due to proxy not running
    - Intelligent recovery: Only attempts proxy start if failures look like proxy issues
    """

    def __init__(self, config: ConnectionConfig):
        self.config = config
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.last_success_time: Optional[datetime] = None
        self._lock = AsyncLock()

        # v82.0: Track connection refused errors for auto-proxy-start
        self._connection_refused_count = 0
        self._last_proxy_start_attempt: Optional[datetime] = None
        self._proxy_start_cooldown_seconds = 60  # Don't spam proxy starts

    async def record_success_async(self) -> None:
        """Record a successful operation (async)."""
        async with self._lock:
            self._record_success_internal()

    def record_success(self) -> None:
        """Record a successful operation (sync)."""
        with self._lock:
            self._record_success_internal()

    def _record_success_internal(self) -> None:
        self.last_success_time = datetime.now()
        self.success_count += 1
        self.failure_count = 0
        if self.state == CircuitState.HALF_OPEN:
            logger.info("🟢 Circuit breaker CLOSED (recovered)")
            self.state = CircuitState.CLOSED

    async def record_failure_async(self) -> None:
        """Record a failed operation (async)."""
        async with self._lock:
            self._record_failure_internal()

    def record_failure(self) -> None:
        """Record a failed operation (sync)."""
        with self._lock:
            self._record_failure_internal()

    def _record_failure_internal(self, error: Optional[Exception] = None) -> None:
        self.failure_count += 1
        self.last_failure_time = datetime.now()

        # v82.0: Track connection refused errors (proxy not running indicator)
        if error:
            error_str = str(error).lower()
            if "connection refused" in error_str or "errno 61" in error_str:
                self._connection_refused_count += 1

        if self.state == CircuitState.HALF_OPEN:
            logger.warning("🔴 Circuit breaker OPEN (recovery failed)")
            self.state = CircuitState.OPEN
        elif self.failure_count >= self.config.failure_threshold:
            if self.state != CircuitState.OPEN:
                logger.warning(f"🔴 Circuit breaker OPEN ({self.failure_count} failures)")
            self.state = CircuitState.OPEN

    def can_execute(self) -> bool:
        """Check if requests are allowed."""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            if self.last_failure_time:
                elapsed = (datetime.now() - self.last_failure_time).total_seconds()
                if elapsed >= self.config.recovery_timeout_seconds:
                    logger.info("🟡 Circuit breaker HALF-OPEN (testing)")
                    self.state = CircuitState.HALF_OPEN

                    # v82.0: Attempt auto-proxy-start if failures were connection refused
                    if self._connection_refused_count > 0:
                        self._try_auto_start_proxy()

                    return True
            return False

        return self.state == CircuitState.HALF_OPEN

    def _try_auto_start_proxy(self) -> None:
        """
        v83.0: Attempt to auto-start Cloud SQL proxy if it appears to be down.

        This is called when transitioning from OPEN to HALF_OPEN and we've
        detected connection refused errors (indicating proxy not running).

        Uses a cooldown to prevent spamming proxy start attempts.

        v83.0 Fixes:
        - Uses get_proxy_manager() singleton instead of constructing with dict
        - Integrates with ProxyReadinessGate for coordinated starts
        - Cooldown configurable from env (JARVIS_CIRCUIT_BREAKER_PROXY_COOLDOWN)
        - Proper async scheduling with running loop detection
        """
        try:
            # Get cooldown from env (no hardcoding)
            cooldown = float(os.getenv("JARVIS_CIRCUIT_BREAKER_PROXY_COOLDOWN", "60"))

            # Check cooldown
            if self._last_proxy_start_attempt:
                elapsed = (datetime.now() - self._last_proxy_start_attempt).total_seconds()
                if elapsed < cooldown:
                    logger.debug(
                        f"[v83.0] Proxy start cooldown active ({elapsed:.0f}s / {cooldown}s)"
                    )
                    return

            self._last_proxy_start_attempt = datetime.now()

            # v83.0: Use singleton proxy manager (correct API!)
            try:
                from intelligence.cloud_sql_proxy_manager import get_proxy_manager
            except ImportError:
                try:
                    from backend.intelligence.cloud_sql_proxy_manager import get_proxy_manager
                except ImportError:
                    logger.debug("[v83.0] Proxy manager not available")
                    return

            try:
                proxy_manager = get_proxy_manager()
            except FileNotFoundError:
                # Config not found - normal for SQLite-only deployments
                logger.debug("[v83.0] Proxy config not found, skipping auto-start")
                return

            if not proxy_manager.is_running():
                logger.info("[v83.0] 🚀 Auto-starting Cloud SQL proxy...")

                # Start proxy in background (non-blocking)
                try:
                    loop = asyncio.get_running_loop()
                    # Schedule async start
                    asyncio.create_task(self._async_start_proxy(proxy_manager))
                except RuntimeError:
                    # No running loop - can't start async
                    logger.debug("[v83.0] No running event loop, skipping auto-start")
            else:
                logger.debug("[v83.0] Proxy already running, no auto-start needed")
                # Reset connection refused count since proxy is running
                self._connection_refused_count = 0

        except Exception as e:
            logger.debug(f"[v83.0] Auto-proxy-start failed: {e}")

    async def _async_start_proxy(self, proxy_manager) -> None:
        """
        Async helper to start proxy without blocking.

        v83.0: Coordinates with ProxyReadinessGate after successful start.
        """
        try:
            success = await proxy_manager.start(force_restart=False, max_retries=2)
            if success:
                logger.info("[v83.0] ✅ Cloud SQL proxy auto-started successfully")
                self._connection_refused_count = 0

                # v83.0: Notify the readiness gate to verify DB-level connectivity
                try:
                    gate = get_readiness_gate()
                    # Trigger a check - this will verify DB-level not just TCP
                    await gate.wait_for_ready(timeout=10.0)
                except Exception as e:
                    logger.debug(f"[v83.0] Readiness gate check after proxy start: {e}")
            else:
                logger.warning("[v83.0] ⚠️ Cloud SQL proxy auto-start failed")
        except Exception as e:
            logger.debug(f"[v83.0] Async proxy start error: {e}")

    def get_state_info(self) -> Dict[str, Any]:
        """Get circuit breaker state information."""
        return {
            'state': self.state.name,
            'failure_count': self.failure_count,
            'success_count': self.success_count,
            'last_failure': self.last_failure_time.isoformat() if self.last_failure_time else None,
            'last_success': self.last_success_time.isoformat() if self.last_success_time else None,
            # v82.0 metrics
            'connection_refused_count': self._connection_refused_count,
            'last_proxy_start_attempt': self._last_proxy_start_attempt.isoformat() if self._last_proxy_start_attempt else None,
        }


# =============================================================================
# Leak Detector with Immediate Cleanup
# =============================================================================

class LeakDetector:
    """
    Advanced leak detection with immediate cleanup capability.

    Features:
    - Real-time leak monitoring
    - Immediate cleanup on detection
    - Adaptive monitoring intervals
    - Connection health scoring
    """

    def __init__(self, config: ConnectionConfig, manager: 'CloudSQLConnectionManager'):
        self.config = config
        self.manager = weakref.ref(manager)  # Avoid circular reference
        self._lock = AsyncLock()
        self._last_check: Optional[datetime] = None
        self._consecutive_clean_checks: int = 0
        self._adaptive_interval: float = config.leak_check_interval_seconds

    async def check_and_cleanup(self) -> int:
        """
        Check for leaks and immediately clean them up.

        v3.1: Added retry logic for TLS race conditions.

        Returns:
            Number of leaked connections cleaned up
        """
        manager = self.manager()
        if not manager or not manager.db_config:
            return 0

        cleaned = 0
        async with self._lock:
            max_retries = 2
            conn = None

            for attempt in range(max_retries):
                try:
                    self._last_check = datetime.now()

                    # Create temporary connection for cleanup with retry
                    conn = await asyncio.wait_for(
                        asyncpg.connect(
                            host=manager.db_config["host"],
                            port=manager.db_config["port"],
                            database=manager.db_config["database"],
                            user=manager.db_config["user"],
                            password=manager.db_config["password"],
                        ),
                        timeout=5.0
                    )
                    # Connection successful - break out of retry loop
                    break

                except asyncio.InvalidStateError:
                    # v3.1: TLS race condition - retry with small delay
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.3 * (attempt + 1))
                        continue
                    # Final attempt failed
                    return 0

                except Exception:
                    # Other errors - don't retry
                    return 0

            if conn is None:
                return 0

            try:
                threshold_minutes = self.config.leaked_idle_threshold_minutes

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
                """, manager.db_config["database"], manager.db_config["user"])

                if leaked:
                    logger.warning(f"⚠️ Found {len(leaked)} leaked connections (idle > {threshold_minutes} min)")
                    manager.metrics.total_leaks_detected += len(leaked)
                    self._consecutive_clean_checks = 0

                    # IMMEDIATE cleanup
                    for row in leaked:
                        idle_mins = row['idle_seconds'] / 60
                        try:
                            await conn.execute("SELECT pg_terminate_backend($1)", row['pid'])
                            logger.info(f"   ✅ Killed PID {row['pid']} (idle {idle_mins:.1f} min)")
                            manager.metrics.total_leaks_recovered += 1
                            manager.metrics.total_immediate_cleanups += 1
                            cleaned += 1
                        except Exception as e:
                            logger.warning(f"   ⚠️ Failed to kill PID {row['pid']}: {e}")

                    manager.metrics.last_leak_cleanup = datetime.now()

                    # Adaptive: decrease interval when leaks found
                    self._adaptive_interval = max(10.0, self.config.leak_check_interval_seconds * 0.5)
                else:
                    self._consecutive_clean_checks += 1
                    # Adaptive: increase interval when clean
                    if self._consecutive_clean_checks > 5:
                        self._adaptive_interval = min(
                            60.0,
                            self.config.leak_check_interval_seconds * 1.5
                        )

            except asyncio.TimeoutError:
                logger.debug("⏱️ Leak check timeout (proxy not running?)")
            except Exception as e:
                logger.debug(f"⚠️ Leak check failed: {e}")
            finally:
                try:
                    await conn.close()
                except Exception:
                    pass

        return cleaned

    @property
    def next_check_interval(self) -> float:
        """Get the adaptive interval for next check."""
        return self._adaptive_interval


# =============================================================================
# Main Connection Manager
# =============================================================================

class CloudSQLConnectionManager:
    """
    Singleton async-safe CloudSQL connection pool manager with leak detection.

    Features:
    - Automatic leak detection with IMMEDIATE cleanup
    - Circuit breaker for failure isolation
    - Adaptive background cleanup
    - Comprehensive metrics and statistics
    - Signal-aware graceful shutdown
    - Fully async-safe operations
    - Dynamic configuration via environment
    """

    _instance: Optional['CloudSQLConnectionManager'] = None
    _class_lock = threading.Lock()
    _initialized = False
    _initializing = False  # v3.1: Prevent concurrent initialization race

    def __new__(cls):
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self):
        # v3.1: Thread-safe singleton initialization with proper double-checked locking
        # This prevents race conditions where multiple threads could start initialization
        # before _initialized is set to True
        with CloudSQLConnectionManager._class_lock:
            if CloudSQLConnectionManager._initialized:
                return
            if CloudSQLConnectionManager._initializing:
                # Another thread is initializing, wait and return
                return
            # Mark as initializing BEFORE releasing lock to prevent races
            CloudSQLConnectionManager._initializing = True

        try:
            # Core state
            self.pool: Optional[asyncpg.Pool] = None
            self.db_config: Dict[str, Any] = {}
            self._conn_config = ConnectionConfig()
            self.is_shutting_down = False
            self.creation_time: Optional[datetime] = None

            # Async-safe locks - use enterprise version if available
            if ENTERPRISE_CONNECTION_AVAILABLE:
                self._pool_lock = EventLoopAwareLock()
                self._checkout_lock = EventLoopAwareLock()
            else:
                self._pool_lock = AsyncLock()
                self._checkout_lock = AsyncLock()

            # Legacy compatibility
            self.connection_count = 0
            self.error_count = 0

            # Connection tracking for leak detection
            self._checkouts: Dict[int, ConnectionCheckout] = {}
            self._checkout_counter = 0
            self._active_connections: Set[int] = set()

            # Metrics
            self.metrics = ConnectionMetrics()

            # Circuit breaker
            self._circuit_breaker: Optional[CircuitBreaker] = None

            # Leak detector
            self._leak_detector: Optional[LeakDetector] = None

            # Background tasks
            self._cleanup_task: Optional[asyncio.Task] = None
            self._leak_monitor_task: Optional[asyncio.Task] = None
            self._health_check_task: Optional[asyncio.Task] = None

            # Callbacks
            self._on_leak_callbacks: List[Callable] = []
            self._on_error_callbacks: List[Callable] = []

            # Startup mode: suppress connection errors until proxy is confirmed ready
            # This prevents noisy logs during early startup when proxy hasn't started yet
            self._startup_mode = True
            self._proxy_ready = False
            self._start_time = time.time()
            self._startup_grace_period = 60  # seconds before logging connection errors

            # v5.5: Store last error for diagnostic purposes
            self.last_error: Optional[str] = None
            self.last_error_time: Optional[datetime] = None

            # v3.1: Connection initialization lock to prevent TLS race conditions
            # asyncpg can hit InvalidStateError when multiple connections try TLS upgrade simultaneously
            self._conn_init_lock = asyncio.Lock()

            self._register_shutdown_handlers()
            CloudSQLConnectionManager._initialized = True
            logger.info("🔧 CloudSQL Connection Manager v3.1 initialized")
        except Exception as e:
            # Reset initialization flags on failure so retry is possible
            CloudSQLConnectionManager._initializing = False
            raise

    def _register_shutdown_handlers(self):
        """Register cleanup handlers for graceful shutdown."""
        atexit.register(self._sync_shutdown)
        logger.debug("✅ Shutdown handlers registered")

    def _sync_shutdown(self):
        """Synchronous shutdown handler for atexit."""
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
        max_connections: Optional[int] = None,  # v258.1 R2-#1: None = use config default
        force_reinit: bool = False,
        config: Optional[ConnectionConfig] = None,
        auto_resolve_credentials: bool = True,
    ) -> bool:
        """
        Initialize connection pool with leak detection and circuit breaker.

        v132.0: Auto-resolves credentials from IntelligentCredentialResolver if
        password is not provided and auto_resolve_credentials is True (default).

        Args:
            host: Database host (127.0.0.1 for proxy)
            port: Database port
            database: Database name
            user: Database user
            password: Database password (if None, auto-resolves from credential sources)
            max_connections: Max pool size (default 3 for db-f1-micro)
            force_reinit: Force re-initialization
            config: Optional ConnectionConfig for advanced settings
            auto_resolve_credentials: Auto-fetch credentials if not provided (default: True)

        Returns:
            True if pool is ready
        """
        async with self._pool_lock:
            if self.pool and not force_reinit:
                logger.info("♻️ Reusing existing connection pool")
                return True

            if self.pool and force_reinit:
                logger.info("🔄 Force re-init: closing existing pool...")
                await self._close_pool_internal()

            if not ASYNCPG_AVAILABLE:
                logger.error("❌ asyncpg not available")
                return False

            # v132.0: Auto-resolve credentials if not provided
            if not password and auto_resolve_credentials:
                logger.info("[v132.0] Auto-resolving credentials from IntelligentCredentialResolver...")
                try:
                    db_config = IntelligentCredentialResolver.get_database_config()
                    if db_config:
                        # Use resolved config values (with fallbacks to provided values)
                        host = db_config.get("host", host)
                        port = db_config.get("port", port)
                        database = db_config.get("database", database)
                        user = db_config.get("user", user)
                        password = db_config.get("password")

                        if password:
                            logger.info(
                                f"[v132.0] ✅ Credentials resolved: {user}@{host}:{port}/{database}"
                            )
                        else:
                            logger.warning("[v132.0] Credential resolver returned config but no password")
                    else:
                        logger.warning("[v132.0] Credential resolver returned None config")
                except Exception as e:
                    logger.warning(f"[v132.0] Credential auto-resolve failed: {e}")

            if not password:
                logger.error("❌ Database password required (auto-resolve also failed)")
                return False

            # Apply config
            if config:
                self._conn_config = config
            else:
                # Reload from environment
                self._conn_config.reload_from_env()

            # v258.1 R2-#1: Only override config if caller explicitly provides max_connections
            if max_connections is not None:
                self._conn_config.max_connections = max_connections
            # R3-#1: Use config value for all downstream references (avoid None)
            effective_max_connections = self._conn_config.max_connections

            # Store DB config
            self.db_config = {
                "host": host,
                "port": port,
                "database": database,
                "user": user,
                "password": password,
                "max_connections": effective_max_connections
            }

            # v4.0: Initialize circuit breaker (use enterprise version if available)
            if ENTERPRISE_CONNECTION_AVAILABLE:
                self._circuit_breaker = AtomicCircuitBreaker(
                    config=CircuitBreakerConfig(
                        failure_threshold=self._conn_config.failure_threshold,
                        recovery_timeout_seconds=self._conn_config.recovery_timeout_seconds,
                        half_open_max_requests=1,
                    )
                )
                logger.debug("   Using AtomicCircuitBreaker (enterprise)")
            else:
                self._circuit_breaker = CircuitBreaker(self._conn_config)

            # Initialize leak detector
            self._leak_detector = LeakDetector(self._conn_config, self)

            # v4.0: Proactive proxy detection (sub-100ms fast-fail)
            if ENTERPRISE_CONNECTION_AVAILABLE:
                try:
                    detector = ProactiveProxyDetector()
                    proxy_status, proxy_msg = await detector.detect()
                    if proxy_status == EnterpriseProxyStatus.UNAVAILABLE:
                        logger.warning(f"⚠️ Proxy not available: {proxy_msg}")
                        self.last_error = f"Proxy unavailable: {proxy_msg}"
                        self.last_error_time = datetime.now()
                        return False
                    logger.info(f"   Proxy detected: {proxy_msg}")
                except Exception as e:
                    logger.debug(f"   Proxy detection error (continuing): {e}")

            try:
                logger.info(f"🔌 Creating CloudSQL connection pool (max={max_connections})...")
                logger.info(f"   Host: {host}:{port}, Database: {database}, User: {user}")
                logger.info(f"   Config: idle_timeout={self._conn_config.max_idle_time_seconds}s, "
                           f"leak_threshold={self._conn_config.leaked_idle_threshold_minutes}min")

                # IMMEDIATE cleanup of leaked connections before creating pool
                await self._immediate_leak_cleanup()

                # v16.0: Enhanced TLS-Safe Connection Pool Creation
                # ═══════════════════════════════════════════════════════════════════
                # ROOT CAUSE FIX for asyncpg TLS InvalidStateError:
                # The error occurs in TLSUpgradeProto.data_received when set_result()
                # is called on an already-completed future. This happens when:
                # 1. Multiple connections try TLS upgrade simultaneously
                # 2. The internal state machine gets corrupted by race conditions
                #
                # FIX STRATEGY:
                # 1. Create connections ONE AT A TIME using a semaphore
                # 2. Start with min_size=1 to verify TLS works first
                # 3. Use a custom init callback that serializes connection setup
                # 4. Wrap all connection operations in defensive error handling
                # ═══════════════════════════════════════════════════════════════════

                # Global semaphore to serialize ALL TLS connection establishment
                # This prevents the race condition at the uvloop level
                if not hasattr(self, '_tls_connection_semaphore'):
                    self._tls_connection_semaphore = asyncio.Semaphore(1)

                async def create_pool_with_tls_safety():
                    """Create pool with TLS race condition prevention."""
                    max_retries = 5
                    last_error = None

                    for attempt in range(max_retries):
                        try:
                            # v16.0: Connection init callback that serializes TLS setup
                            # Each connection waits for the semaphore before completing setup
                            async def serialized_connection_init(conn):
                                """Serialize connection initialization to prevent TLS races."""
                                async with self._tls_connection_semaphore:
                                    # Small delay to let TLS state machine settle
                                    await asyncio.sleep(0.05)
                                    # Verify connection is actually usable
                                    try:
                                        await conn.execute("SELECT 1")
                                    except Exception as verify_err:
                                        logger.debug(f"Connection verify failed: {verify_err}")
                                        raise

                            # v16.0: CRITICAL - Start with min_size=1 to verify TLS first
                            # This prevents multiple simultaneous TLS handshakes during pool init
                            initial_min_size = 1 if attempt == 0 else self._conn_config.min_connections

                            # v242.1: DEBUG level to avoid CWE-532 (clear-text logging in credential scope).
                            # Only pool sizes are logged — no credentials, hosts, or connection strings.
                            logger.debug(f"   [v16.0] Creating pool (attempt {attempt + 1}/{max_retries}, "
                                         f"min={initial_min_size}, max={effective_max_connections})")

                            # Create pool with serialized init
                            pool = await asyncpg.create_pool(
                                host=host,
                                port=port,
                                database=database,
                                user=user,
                                password=password,
                                min_size=initial_min_size,
                                max_size=effective_max_connections,
                                timeout=300.0,  # v258.1 R2-#2: delegate to outer wait_for for dynamic control
                                command_timeout=self._conn_config.query_timeout,
                                max_queries=self._conn_config.max_queries_per_connection,
                                max_inactive_connection_lifetime=self._conn_config.max_idle_time_seconds,
                                init=serialized_connection_init,
                            )

                            # v16.0: Verify pool is functional with a test query
                            async with pool.acquire() as test_conn:
                                await test_conn.execute("SELECT 1")

                            return pool

                        except asyncio.InvalidStateError as e:
                            # v16.0: TLS race condition - this is the specific error we're fixing
                            last_error = e
                            logger.warning(
                                f"⚠️ [v16.0] TLS InvalidStateError on attempt {attempt + 1}/{max_retries}: {e}"
                            )

                            if attempt < max_retries - 1:
                                # Exponential backoff with jitter
                                import random
                                delay = (2 ** attempt) * 0.5 * (0.5 + random.random())
                                logger.info(f"   [v16.0] Waiting {delay:.2f}s before retry...")
                                await asyncio.sleep(delay)

                                # Clear any corrupted event loop state
                                await asyncio.sleep(0)  # Yield to event loop

                        except (OSError, ConnectionError, asyncio.TimeoutError) as e:
                            # Network/connection errors - retry with backoff
                            last_error = e
                            if attempt < max_retries - 1:
                                delay = (attempt + 1) * 1.0
                                logger.warning(
                                    f"⚠️ [v16.0] Connection error on attempt {attempt + 1}/{max_retries}: {e}. "
                                    f"Retrying in {delay:.1f}s..."
                                )
                                await asyncio.sleep(delay)
                            else:
                                raise

                        except Exception as e:
                            # Check if this is a wrapped TLS error
                            if "invalid state" in str(e).lower() or "InvalidStateError" in str(type(e).__name__):
                                last_error = e
                                if attempt < max_retries - 1:
                                    await asyncio.sleep(1.0)
                                    continue
                            # Non-retryable error
                            raise

                    if last_error:
                        logger.error(f"❌ [v16.0] Pool creation failed after {max_retries} attempts: {last_error}")
                        raise last_error
                    raise RuntimeError("Pool creation failed without error")

                # Create pool
                self.pool = await asyncio.wait_for(
                    create_pool_with_tls_safety(),
                    timeout=self._conn_config.pool_creation_timeout
                )

                # Validate
                async with self.pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")

                self.creation_time = datetime.now()
                self.error_count = 0
                self.metrics = ConnectionMetrics()
                self.is_shutting_down = False

                logger.info(f"✅ Connection pool created successfully")
                logger.info(f"   Pool: {self.pool.get_size()} total, {self.pool.get_idle_size()} idle")

                # Start background tasks
                if self._conn_config.enable_background_cleanup:
                    await self._start_background_tasks()

                return True

            except asyncio.TimeoutError:
                logger.error("⏱️ Connection pool creation timeout")
                logger.error("   Causes: proxy not running, bad credentials, network issues")
                self.pool = None
                self.last_error = "Connection timeout"
                self.last_error_time = datetime.now()
                return False

            except asyncio.InvalidStateError as e:
                # v3.1: asyncpg TLS race condition - this should have been retried above
                logger.error(f"❌ TLS race condition after retries: {e}")
                logger.error("   This is an asyncpg bug when multiple connections attempt TLS simultaneously")
                logger.error("   Try reducing min_connections or increasing connection timeouts")
                self.pool = None
                self.last_error = f"TLS race condition: {e}"
                self.last_error_time = datetime.now()
                return False

            except Exception as e:
                error_str = str(e)
                self.last_error = error_str
                self.last_error_time = datetime.now()

                # v5.5: Detect specific error types for better diagnostics
                if "invalid state" in error_str.lower():
                    # v3.1: asyncpg TLS race condition
                    logger.error(f"❌ TLS race condition: {e}")
                    logger.error("   asyncpg hit InvalidStateError during connection initialization")
                    self.pool = None
                    self.error_count += 1
                    return False
                elif "password authentication failed" in error_str.lower():
                    logger.error(f"❌ Failed to create pool: {e}")
                    logger.error("")
                    logger.error("   🔐 CREDENTIAL MISMATCH DETECTED")
                    logger.error("   The password from GCP Secret Manager doesn't match Cloud SQL.")
                    logger.error("")
                    logger.error("   To fix this, either:")
                    logger.error("   A) Update Secret Manager with the correct password:")
                    logger.error("      echo 'YOUR_PASSWORD' | gcloud secrets versions add jarvis-db-password --data-file=-")
                    logger.error("")
                    logger.error("   B) Reset the Cloud SQL user password to match:")
                    logger.error(f"      gcloud sql users set-password {user} \\")
                    logger.error(f"        --instance=jarvis-learning-db --password='YOUR_PASSWORD'")
                    logger.error("")
                elif "connection refused" in error_str.lower():
                    logger.error(f"❌ Failed to create pool: Connection refused")
                    logger.error("   Cloud SQL proxy may not be running on port 5432")
                elif "does not exist" in error_str.lower():
                    logger.error(f"❌ Failed to create pool: {e}")
                    logger.error(f"   Database '{database}' may not exist on the Cloud SQL instance")
                else:
                    logger.error(f"❌ Failed to create pool: {e}")

                self.pool = None
                self.error_count += 1
                return False

    async def _immediate_leak_cleanup(self) -> int:
        """
        Immediately cleanup all leaked connections.

        v3.1: Added retry logic to handle TLS race conditions on direct connections.
        """
        if self._leak_detector:
            return await self._leak_detector.check_and_cleanup()

        # Fallback if leak detector not initialized
        if not self.db_config:
            return 0

        cleaned = 0
        max_retries = 2

        for attempt in range(max_retries):
            try:
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

                try:
                    threshold = self._conn_config.leaked_idle_threshold_minutes
                    leaked = await conn.fetch(f"""
                        SELECT pid FROM pg_stat_activity
                        WHERE datname = $1 AND pid <> pg_backend_pid()
                          AND usename = $2 AND state = 'idle'
                          AND state_change < NOW() - INTERVAL '{threshold} minutes'
                    """, self.db_config["database"], self.db_config["user"])

                    for row in leaked:
                        try:
                            await conn.execute("SELECT pg_terminate_backend($1)", row['pid'])
                            cleaned += 1
                        except Exception:
                            pass
                finally:
                    await conn.close()

                # Success - break out of retry loop
                break

            except asyncio.InvalidStateError:
                # v3.1: TLS race condition - retry with small delay
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                # Final attempt failed
                pass

            except Exception:
                # Other errors - don't retry
                pass

        return cleaned

    async def _start_background_tasks(self) -> None:
        """Start background monitoring tasks."""
        # Cancel existing tasks
        for task in [self._cleanup_task, self._leak_monitor_task, self._health_check_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Start new tasks
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="cloudsql_cleanup"
        )
        self._leak_monitor_task = asyncio.create_task(
            self._leak_monitor_loop(),
            name="cloudsql_leak_monitor"
        )
        self._health_check_task = asyncio.create_task(
            self._health_check_loop(),
            name="cloudsql_health_check"
        )

        logger.debug("🔄 Background tasks started")

    async def _cleanup_loop(self) -> None:
        """Background task for periodic cleanup with adaptive intervals."""
        while not self.is_shutting_down:
            try:
                interval = (
                    self._leak_detector.next_check_interval
                    if self._leak_detector
                    else self._conn_config.cleanup_interval_seconds
                )
                await asyncio.sleep(interval)

                if self.is_shutting_down:
                    break

                # Periodic leak cleanup
                if self.pool and self.db_config and self._leak_detector:
                    await self._leak_detector.check_and_cleanup()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Cleanup loop error: {e}")
                await asyncio.sleep(10.0)

    async def _leak_monitor_loop(self) -> None:
        """Monitor tracked checkouts for potential leaks."""
        while not self.is_shutting_down:
            try:
                await asyncio.sleep(self._conn_config.leak_check_interval_seconds)
                if self.is_shutting_down:
                    break

                await self._check_checkout_leaks()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Leak monitor error: {e}")
                await asyncio.sleep(5.0)

    async def _check_checkout_leaks(self) -> None:
        """Check tracked checkouts for leaks and handle them."""
        async with self._checkout_lock:
            leaked_ids = []

            for checkout_id, checkout in self._checkouts.items():
                if checkout.released:
                    continue

                age = checkout.age_seconds

                # Warning for long-held connections
                if age > self._conn_config.checkout_warning_seconds:
                    if age <= self._conn_config.checkout_timeout_seconds:
                        logger.warning(
                            f"⚠️ Connection held {age:.0f}s (checkout #{checkout_id})\n"
                            f"   Location: {checkout.caller_info}"
                        )

                # Force mark as leaked if held too long
                if age > self._conn_config.checkout_timeout_seconds:
                    logger.error(
                        f"🚨 LEAK: Connection #{checkout_id} held {age:.0f}s - forcing release"
                    )
                    leaked_ids.append(checkout_id)
                    self.metrics.total_leaks_detected += 1

            # Handle leaked connections
            for checkout_id in leaked_ids:
                self._checkouts[checkout_id].released = True
                self._checkouts[checkout_id].release_time = datetime.now()
                self.metrics.total_leaks_recovered += 1

                # Fire callbacks
                await self._fire_leak_callbacks(checkout_id)

            # Cleanup tracking for old checkouts
            await self._cleanup_old_checkouts_async()

    async def _fire_leak_callbacks(self, checkout_id: int) -> None:
        """Fire registered leak callbacks."""
        for callback in self._on_leak_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(checkout_id)
                else:
                    callback(checkout_id)
            except Exception as e:
                logger.debug(f"Leak callback error: {e}")

    async def _health_check_loop(self) -> None:
        """Periodic health check of the connection pool with intelligent proxy detection."""
        # v5.0: Get proxy detector for intelligent health checking
        proxy_detector = get_proxy_detector() if PROXY_DETECTOR_AVAILABLE else None

        while not self.is_shutting_down:
            try:
                # v5.0: Use intelligent delay based on proxy status
                if proxy_detector and not self.pool:
                    delay = proxy_detector.get_next_retry_delay()
                else:
                    delay = 60.0  # Standard 60-second health check when pool exists

                await asyncio.sleep(delay)
                if self.is_shutting_down:
                    break

                # v5.0: Check proxy availability before attempting health check
                if proxy_detector:
                    proxy_status, proxy_info = await proxy_detector.detect_proxy()

                    if proxy_status == ProxyStatus.UNAVAILABLE:
                        # Proxy not available - skip health check silently
                        # (already logged by hybrid_database_sync)
                        if not proxy_detector.should_retry():
                            # Local dev mode - stop checking entirely
                            continue
                        # Otherwise, just skip this iteration (will retry with backoff)
                        continue

                if self.pool:
                    try:
                        # v258.1 R3-#2: Outer timeout on acquire+query
                        # Pool timeout=300s is delegation-only; health check must fail fast
                        async def _health_check_query():
                            async with self.pool.acquire() as conn:
                                await conn.fetchval("SELECT 1")
                        await asyncio.wait_for(_health_check_query(), timeout=10.0)
                        self.metrics.healthy_connections += 1
                    except Exception as e:
                        self.metrics.unhealthy_connections += 1
                        # v5.0: Only log warning if proxy is supposed to be available
                        if proxy_detector:
                            proxy_status, _ = await proxy_detector.detect_proxy()
                            if proxy_status == ProxyStatus.AVAILABLE:
                                logger.warning("⚠️ Health check failed - pool may be degraded")
                            # If proxy unavailable, don't spam warnings
                        else:
                            # No proxy detector - use legacy behavior
                            if not self._should_suppress_error():
                                logger.warning("⚠️ Health check failed - pool may be degraded")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Health check error: {e}")
                await asyncio.sleep(30.0)

    @asynccontextmanager
    async def connection(self):
        """
        Acquire connection with leak tracking, circuit breaker, and ML-powered rate limiting.

        v10.0: Integrates with IntelligentRateOrchestrator for:
        - Predictive rate limit forecasting
        - Adaptive throttling based on usage patterns
        - Priority-based request scheduling

        Usage:
            async with manager.connection() as conn:
                result = await conn.fetchval("SELECT 1")
        """
        if not self.pool:
            raise RuntimeError("Connection pool not initialized")

        if self.is_shutting_down:
            raise RuntimeError("Connection manager is shutting down")

        # Start timing for metrics
        start_time = time.time()

        # Check circuit breaker (v4.0: handle both async and sync versions)
        if self._circuit_breaker:
            # Enterprise AtomicCircuitBreaker has async can_execute(), legacy has sync
            if ENTERPRISE_CONNECTION_AVAILABLE and hasattr(self._circuit_breaker, 'can_execute_sync'):
                # Use async version for full atomic HALF_OPEN transition
                can_proceed = await self._circuit_breaker.can_execute()
            else:
                # Legacy sync version
                can_proceed = self._circuit_breaker.can_execute()

            if not can_proceed:
                raise RuntimeError(
                    f"Circuit breaker OPEN - retry in {self._conn_config.recovery_timeout_seconds}s"
                )

        # v125.1: Proxy readiness pre-check - fail fast when proxy is known to be down
        # This prevents connection attempt storms when the proxy hasn't started
        if PROXY_DETECTOR_AVAILABLE and not self._proxy_ready:
            proxy_detector = get_proxy_detector()
            if proxy_detector:
                try:
                    proxy_status, proxy_info = await proxy_detector.detect_proxy()
                    if proxy_status == ProxyStatus.UNAVAILABLE:
                        # Check if we should even be attempting connections
                        if not proxy_detector.should_retry():
                            raise RuntimeError(
                                "CloudSQL proxy unavailable - using SQLite fallback mode"
                            )
                        # Proxy unavailable but should retry - let the attempt proceed
                        # but log a debug message
                        logger.debug(
                            "[v125.1] Proxy currently unavailable, attempting connection anyway "
                            "(may be in startup)"
                        )
                except Exception as e:
                    # Proxy detection failed - proceed with connection attempt
                    logger.debug(f"[v125.1] Proxy detection error (proceeding): {e}")

        # v10.5: Enhanced intelligent rate limiting with adaptive backoff
        if INTELLIGENT_RATE_ORCHESTRATOR_AVAILABLE:
            try:
                orchestrator = await get_rate_orchestrator()

                # Retry with adaptive exponential backoff when throttled
                max_retries = 10  # Increased from 5 to 10 for better resilience
                retry_count = 0
                base_delay = 0.1  # Start with 100ms (reduced from 200ms for faster initial retries)

                while retry_count < max_retries:
                    acquired, reason = await orchestrator.acquire(
                        RateServiceType.CLOUDSQL_CONNECTIONS,
                        RateOpType.QUERY,
                        RequestPriority.NORMAL,
                    )

                    if acquired:
                        # Successfully acquired rate limit token
                        if retry_count > 0:
                            logger.debug(
                                f"⚡ CloudSQL rate limit acquired after {retry_count} "
                                f"retries ({(time.time() - start_time):.2f}s)"
                            )
                        break

                    # Throttled - calculate adaptive exponential backoff
                    retry_count += 1
                    if retry_count < max_retries:
                        # Adaptive backoff: slower growth for early retries, faster later
                        # 100ms, 200ms, 400ms, 600ms, 900ms, 1.3s, 1.8s, 2.4s, 3.2s, 4.2s
                        if retry_count <= 3:
                            # Linear growth for first 3 retries (fast initial attempts)
                            delay = base_delay * retry_count
                        else:
                            # Exponential growth after 3 retries (back off more aggressively)
                            delay = min(base_delay * (1.5 ** retry_count), 5.0)

                        # Only log every 3rd retry to reduce noise
                        if retry_count % 3 == 0 or retry_count == 1:
                            logger.debug(
                                f"⚡ CloudSQL connection throttled: {reason} "
                                f"(retry {retry_count}/{max_retries}, waiting {delay:.2f}s)"
                            )

                        await asyncio.sleep(delay)
                    else:
                        # Max retries reached - proceed anyway but log as info (not warning)
                        # This is expected behavior during high load, not an error
                        logger.info(
                            f"⚡ CloudSQL rate orchestrator busy after {max_retries} retries. "
                            f"Proceeding with database query (rate limits managed by orchestrator)."
                        )

            except Exception as e:
                logger.debug(f"Rate orchestrator check failed: {e}")

        # Initialize connection tracking
        conn = None
        checkout_id = None

        # v258.1 R2-#9: Use grace period timer, not just _startup_mode flag
        # (proxy_ready can turn off _startup_mode while callers still contend)
        effective_timeout = self._conn_config.connection_timeout
        if self._startup_mode or (time.time() - self._start_time < self._startup_grace_period):
            effective_timeout = self._conn_config.startup_connection_timeout

        try:
            # Acquire connection
            conn = await asyncio.wait_for(
                self.pool.acquire(),
                timeout=effective_timeout
            )

            # Track checkout
            async with self._checkout_lock:
                self._checkout_counter += 1
                checkout_id = self._checkout_counter
                caller_info = self._get_caller_info()
                self._checkouts[checkout_id] = ConnectionCheckout(
                    checkout_id=checkout_id,
                    checkout_time=datetime.now(),
                    stack_trace=traceback.format_stack()[-5:-2],
                    caller_info=caller_info,
                )
                self._active_connections.add(checkout_id)

            # Update metrics
            self.connection_count += 1
            self.metrics.total_checkouts += 1
            self.metrics.last_checkout = datetime.now()

            latency_ms = (time.time() - start_time) * 1000
            logger.debug(f"✅ Connection #{checkout_id} acquired ({latency_ms:.1f}ms) from {caller_info}")

            # Record success for circuit breaker
            if self._circuit_breaker:
                await self._circuit_breaker.record_success_async()

            yield conn

        except asyncio.TimeoutError:
            # Suppress timeout errors during startup mode (before proxy is ready)
            if not self._should_suppress_error():
                # v258.1: Include pool state in error for diagnostics
                _pool_size = self.pool.get_size() if self.pool else 0
                _pool_free = self.pool.get_idle_size() if self.pool else 0
                _pool_max = self._conn_config.max_connections
                logger.error(
                    f"⏱️ Connection timeout - pool exhausted "
                    f"(size={_pool_size}/{_pool_max}, free={_pool_free}, "
                    f"timeout={effective_timeout:.0f}s, startup={self._startup_mode})"
                )
            else:
                logger.debug("⏱️ Connection timeout during startup (proxy not ready yet)")
            self.error_count += 1
            self.metrics.total_timeouts += 1
            self.metrics.pool_exhaustion_count += 1
            if self._circuit_breaker:
                await self._circuit_breaker.record_failure_async()

            # Aggressive cleanup on pool exhaustion
            if self._conn_config.aggressive_cleanup_on_leak:
                asyncio.create_task(self._immediate_leak_cleanup())

            raise

        except asyncio.InvalidStateError as e:
            # v15.0: TLS protocol state error - connection was corrupted during handshake
            # This happens when asyncpg's TLS upgrade receives data after connection finalized
            # Usually caused by connection being reused while another operation is pending
            logger.warning(f"⚠️ TLS protocol state error: {e}")
            self.error_count += 1
            self.metrics.total_errors += 1
            self.metrics.last_error = datetime.now()
            self.last_error = f"TLS InvalidStateError: {e}"
            self.last_error_time = datetime.now()
            if self._circuit_breaker:
                await self._circuit_breaker.record_failure_async()

            # v15.0: Force pool recreation on TLS errors to clear corrupted connections
            asyncio.create_task(self._recreate_pool_on_tls_error())
            raise

        except Exception as e:
            # Suppress connection errors during startup mode (before proxy is ready)
            if str(e) and str(e) != "0":
                if not self._should_suppress_error():
                    logger.error(f"❌ Connection error: {e}")
                else:
                    logger.debug(f"⏳ Connection error during startup (proxy not ready yet): {e}")
            self.error_count += 1
            self.metrics.total_errors += 1
            self.metrics.last_error = datetime.now()
            self.last_error = str(e)
            self.last_error_time = datetime.now()
            if self._circuit_breaker:
                await self._circuit_breaker.record_failure_async()

            # Fire error callbacks
            for callback in self._on_error_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(e)
                    else:
                        callback(e)
                except Exception:
                    pass

            raise

        finally:
            # ALWAYS release and track
            if conn:
                try:
                    duration_ms = (time.time() - start_time) * 1000

                    # Update metrics
                    self.metrics.total_releases += 1
                    self.metrics.last_release = datetime.now()
                    self.metrics.update_checkout_duration(duration_ms)

                    # Mark checkout as released
                    async with self._checkout_lock:
                        if checkout_id:
                            if checkout_id in self._checkouts:
                                self._checkouts[checkout_id].released = True
                                self._checkouts[checkout_id].release_time = datetime.now()
                            self._active_connections.discard(checkout_id)

                    # Release to pool with CancelledError protection
                    # v82.1: During shutdown, asyncio.shield() can be interrupted
                    # causing CancelledError in pool.release(). We catch this and
                    # let the connection be cleaned up by the pool's shutdown.
                    try:
                        await self.pool.release(conn)
                        logger.debug(f"♻️ Connection #{checkout_id} released ({duration_ms:.1f}ms)")
                    except asyncio.CancelledError:
                        # Connection will be cleaned up during pool shutdown
                        logger.debug(f"⚠️ Connection #{checkout_id} release cancelled (shutdown in progress)")

                except asyncio.CancelledError:
                    # v82.1: Outer CancelledError - don't log as error
                    logger.debug(f"⚠️ Connection operation cancelled for #{checkout_id} (shutdown)")
                except Exception as e:
                    logger.error(f"❌ Failed to release connection: {e}")

    def _get_caller_info(self) -> str:
        """Get caller stack trace for debugging.

        v241.1: Also skip contextlib frames — they mask the real caller
        when the connection is acquired via @asynccontextmanager wrappers.
        Previously reported "contextlib.py:175 in __aenter__" instead of
        the actual caller module.
        """
        _skip = {'cloud_sql', 'contextlib', 'asyncio'}
        try:
            stack = traceback.extract_stack()
            relevant = [
                f for f in stack[:-4]
                if not any(s in f.filename.lower() for s in _skip)
            ]
            if relevant:
                frame = relevant[-1]
                return f"{frame.filename.split('/')[-1]}:{frame.lineno} in {frame.name}"
            return "unknown"
        except Exception:
            return "unknown"

    async def _cleanup_old_checkouts_async(self) -> None:
        """Remove old released checkouts from tracking (async)."""
        cutoff = datetime.now() - timedelta(seconds=60)
        to_remove = [
            cid for cid, c in self._checkouts.items()
            if c.released and c.release_time and c.release_time < cutoff
        ]
        for cid in to_remove[:100]:  # Limit cleanup batch size
            del self._checkouts[cid]

    def _cleanup_old_checkouts(self) -> None:
        """Remove old released checkouts from tracking (sync)."""
        cutoff = datetime.now() - timedelta(seconds=60)
        to_remove = [
            cid for cid, c in self._checkouts.items()
            if c.released and c.release_time and c.release_time < cutoff
        ]
        for cid in to_remove[:100]:
            del self._checkouts[cid]

    async def _recreate_pool_on_tls_error(self) -> None:
        """
        v15.0: Recreate connection pool after TLS protocol error.

        TLS InvalidStateError indicates corrupted connection state, usually due to:
        - Connection being reused while TLS handshake still pending
        - Race condition in connection initialization
        - Network interruption during TLS upgrade

        This method safely closes all existing connections and creates a fresh pool.
        """
        if self.is_shutting_down:
            return

        async with self._pool_lock:
            try:
                logger.info("🔄 [v15.0] Recreating connection pool due to TLS error...")

                # Close existing pool if any
                if self.pool:
                    old_pool = self.pool
                    self.pool = None

                    # Give active connections a moment to complete
                    await asyncio.sleep(0.5)

                    try:
                        # Terminate all connections forcefully
                        old_pool.terminate()
                        await asyncio.wait_for(old_pool.close(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning("⚠️ [v15.0] Pool close timeout, connections may be leaked")
                    except Exception as e:
                        logger.debug(f"[v15.0] Pool close error (ignored): {e}")

                # Wait for any pending TLS operations to settle
                await asyncio.sleep(1.0)

                # v125.1: Check proxy readiness BEFORE attempting pool creation
                # This prevents retry storms when proxy is known to be down
                proxy_detector = get_proxy_detector() if PROXY_DETECTOR_AVAILABLE else None
                if proxy_detector:
                    proxy_status, proxy_info = await proxy_detector.detect_proxy()
                    if proxy_status == ProxyStatus.UNAVAILABLE:
                        logger.warning(
                            "[v125.1] Pool recreation skipped - proxy unavailable. "
                            "Will retry when proxy is detected."
                        )
                        return  # Don't attempt - proxy is known to be down

                # v125.1: Use TLS-safe factory instead of direct asyncpg.create_pool()
                # This ensures proper serialization of TLS operations
                if self.db_config:
                    self.pool = await tls_safe_create_pool(
                        host=self.db_config.get('host', '127.0.0.1'),
                        port=self.db_config.get('port', 5432),
                        database=self.db_config.get('database', 'jarvis_learning'),
                        user=self.db_config.get('user', 'jarvis'),
                        password=self.db_config.get('password'),
                        min_size=1,
                        max_size=self._conn_config.max_pool_size,
                        command_timeout=self._conn_config.command_timeout,
                        timeout=self._conn_config.connection_timeout,
                        max_retries=3,  # Limit retries in recreation scenario
                    )

                    if self.pool:
                        logger.info("✅ [v125.1] Connection pool recreated successfully (TLS-safe)")

                        # Reset error counters
                        self.error_count = 0
                        self.metrics.total_errors = 0
                        if self._circuit_breaker:
                            self._circuit_breaker.reset()
                    else:
                        logger.warning("[v125.1] Pool recreation failed - will retry on next request")

            except Exception as e:
                logger.error(f"❌ [v125.1] Failed to recreate pool: {e}")
                # v125.1: Record failure but don't artificially inflate failure count
                # Let the circuit breaker work naturally based on actual failures
                if self._circuit_breaker:
                    await self._circuit_breaker.record_failure_async()

    async def execute(self, query: str, *args, timeout: Optional[float] = None, retry: bool = True) -> str:
        """
        Execute query and return result with automatic retry on connection errors.

        Args:
            query: SQL query string
            *args: Query parameters
            timeout: Query timeout in seconds
            retry: Whether to retry on connection errors (default True)

        Returns:
            Query result status string
        """
        async def _execute():
            timeout_val = timeout or self._conn_config.query_timeout
            async with self.connection() as conn:
                return await asyncio.wait_for(
                    conn.execute(query, *args),
                    timeout=timeout_val
                )

        if retry:
            return await self._execute_with_retry(_execute, "execute")
        return await _execute()

    async def fetch(self, query: str, *args, timeout: Optional[float] = None, retry: bool = True) -> List[asyncpg.Record]:
        """
        Fetch multiple rows with automatic retry on connection errors.

        Args:
            query: SQL query string
            *args: Query parameters
            timeout: Query timeout in seconds
            retry: Whether to retry on connection errors (default True)

        Returns:
            List of database records
        """
        async def _fetch():
            timeout_val = timeout or self._conn_config.query_timeout
            async with self.connection() as conn:
                return await asyncio.wait_for(
                    conn.fetch(query, *args),
                    timeout=timeout_val
                )

        if retry:
            return await self._execute_with_retry(_fetch, "fetch")
        return await _fetch()

    async def fetchrow(self, query: str, *args, timeout: Optional[float] = None, retry: bool = True) -> Optional[asyncpg.Record]:
        """
        Fetch single row with automatic retry on connection errors.

        Args:
            query: SQL query string
            *args: Query parameters
            timeout: Query timeout in seconds
            retry: Whether to retry on connection errors (default True)

        Returns:
            Single database record or None
        """
        async def _fetchrow():
            timeout_val = timeout or self._conn_config.query_timeout
            async with self.connection() as conn:
                return await asyncio.wait_for(
                    conn.fetchrow(query, *args),
                    timeout=timeout_val
                )

        if retry:
            return await self._execute_with_retry(_fetchrow, "fetchrow")
        return await _fetchrow()

    async def fetchval(self, query: str, *args, timeout: Optional[float] = None, retry: bool = True) -> Any:
        """
        Fetch single value with automatic retry on connection errors.

        Args:
            query: SQL query string
            *args: Query parameters
            timeout: Query timeout in seconds
            retry: Whether to retry on connection errors (default True)

        Returns:
            Single value from query result
        """
        async def _fetchval():
            timeout_val = timeout or self._conn_config.query_timeout
            async with self.connection() as conn:
                return await asyncio.wait_for(
                    conn.fetchval(query, *args),
                    timeout=timeout_val
                )

        if retry:
            return await self._execute_with_retry(_fetchval, "fetchval")
        return await _fetchval()

    async def _execute_with_retry(self, operation: Callable, operation_name: str, max_retries: int = 3) -> Any:
        """
        Execute an operation with automatic retry on connection errors.

        This method handles transient connection failures by:
        1. Detecting connection-related errors
        2. Attempting pool recovery if needed
        3. Retrying with exponential backoff

        Args:
            operation: Async callable to execute
            operation_name: Name of operation for logging
            max_retries: Maximum retry attempts

        Returns:
            Result of the operation

        Raises:
            Exception: If all retries exhausted
        """
        import random

        last_exception = None
        base_delay = 0.5

        for attempt in range(max_retries + 1):
            try:
                return await operation()

            except Exception as e:
                last_exception = e
                error_msg = str(e).lower()

                # Check if this is a retryable connection error
                if not is_connection_error(e):
                    # Not a connection error - don't retry
                    raise

                if attempt >= max_retries:
                    logger.error(
                        f"❌ {operation_name} failed after {attempt + 1} attempts: {e}"
                    )
                    raise

                # Calculate exponential backoff with jitter
                delay = min(base_delay * (2 ** attempt), 10.0)
                delay = delay * (0.5 + random.random())

                logger.warning(
                    f"⚠️ {operation_name} attempt {attempt + 1}/{max_retries + 1} failed: {e}. "
                    f"Retrying in {delay:.2f}s..."
                )

                # Try to recover the pool if it seems degraded
                if "released" in error_msg or "closed" in error_msg:
                    await self._try_pool_recovery()

                await asyncio.sleep(delay)

        if last_exception:
            raise last_exception

    async def _try_pool_recovery(self) -> bool:
        """
        Attempt to recover the connection pool after errors.

        This method:
        1. Cleans up leaked connections
        2. Attempts to validate pool health
        3. Recreates pool if necessary

        Returns:
            True if recovery successful
        """
        try:
            logger.info("🔄 Attempting pool recovery...")

            # First, try to clean up leaked connections
            cleaned = await self._immediate_leak_cleanup()
            if cleaned > 0:
                logger.info(f"   Cleaned {cleaned} leaked connections")

            # Check if pool is still valid
            if self.pool:
                try:
                    # Python 3.9 compatible (asyncio.timeout is 3.11+)
                    async def _validate_pool():
                        async with self.pool.acquire() as conn:
                            await conn.fetchval("SELECT 1")
                    await asyncio.wait_for(_validate_pool(), timeout=5.0)
                    logger.info("   ✅ Pool is healthy after cleanup")
                    return True
                except Exception as e:
                    logger.warning(f"   Pool validation failed: {e}")

            # Pool seems broken - attempt reinit
            if self.db_config and self.db_config.get("password"):
                logger.info("   Attempting pool reinitialization...")
                success = await self.initialize(
                    host=self.db_config.get("host", "127.0.0.1"),
                    port=self.db_config.get("port", 5432),
                    database=self.db_config.get("database", "jarvis_learning"),
                    user=self.db_config.get("user", "jarvis"),
                    password=self.db_config["password"],
                    max_connections=self.db_config.get("max_connections", 3),
                    force_reinit=True
                )
                if success:
                    logger.info("   ✅ Pool reinitialized successfully")
                    return True
                else:
                    logger.error("   ❌ Pool reinitialization failed")
                    return False

            return False

        except Exception as e:
            logger.error(f"❌ Pool recovery failed: {e}")
            return False

    async def _close_pool_internal(self) -> None:
        """
        v95.1: Close connection pool gracefully (internal, no lock).

        Handles edge cases during shutdown:
        - TCPTransport already closed (connection dropped before shutdown)
        - Event loop closing during pool close
        - Timeout waiting for pool to close
        """
        if self.pool:
            try:
                logger.info("🔌 Closing connection pool...")

                # Cancel background tasks
                for task in [self._cleanup_task, self._leak_monitor_task, self._health_check_task]:
                    if task and not task.done():
                        task.cancel()
                        try:
                            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass

                # v95.1: Check if pool is already closed or transport gone
                try:
                    # Close pool with timeout
                    await asyncio.wait_for(self.pool.close(), timeout=10.0)
                    logger.info("✅ Connection pool closed")
                except (ConnectionResetError, BrokenPipeError) as e:
                    # Transport already closed - this is fine during shutdown
                    logger.debug(f"Pool transport already closed: {e}")
                except Exception as e:
                    error_msg = str(e).lower()
                    # v95.1: Handle "handler is closed" and "transport closed" gracefully
                    if "closed" in error_msg or "handler" in error_msg or "transport" in error_msg:
                        logger.debug(f"Pool already closed during shutdown: {e}")
                    else:
                        raise

            except asyncio.TimeoutError:
                logger.warning("⏱️ Pool close timeout - terminating")
                try:
                    self.pool.terminate()
                except Exception:
                    pass
            except Exception as e:
                # v95.1: Only log as error if it's a genuine problem
                error_msg = str(e).lower()
                if "closed" in error_msg or "handler" in error_msg:
                    logger.debug(f"Pool cleanup during shutdown: {e}")
                else:
                    logger.error(f"❌ Error closing pool: {e}")
            finally:
                self.pool = None

    async def invalidate_pool_on_credential_change(self) -> bool:
        """
        v114.0: Invalidate the connection pool when credentials change.

        This method should be called when credentials are invalidated or refreshed
        to ensure the pool is recreated with the new credentials.

        Returns:
            True if pool was successfully invalidated, False if no action needed.
        """
        if not self.pool:
            logger.debug("[CloudSQLConnectionManager v114.0] No pool to invalidate")
            return False

        logger.info(
            "[CloudSQLConnectionManager v114.0] 🔄 Invalidating connection pool due to credential change"
        )

        try:
            # Close the existing pool
            await self._close_pool_internal()

            # Reset initialization state so pool will be recreated
            self._is_initialized = False

            # Clear the credential resolver cache
            IntelligentCredentialResolver.invalidate_cache()

            logger.info(
                "[CloudSQLConnectionManager v114.0] ✅ Pool invalidated - "
                "will be recreated with fresh credentials on next use"
            )
            return True

        except Exception as e:
            logger.error(f"[CloudSQLConnectionManager v114.0] ❌ Pool invalidation failed: {e}")
            return False

    @classmethod
    async def invalidate_all_pools_on_credential_change(cls) -> int:
        """
        v114.0: Invalidate all connection pools across all instances when credentials change.

        This is a class-level method that finds all instances and invalidates their pools.

        Returns:
            Number of pools invalidated.
        """
        invalidated = 0

        # Get the singleton instance
        if cls._instance and cls._instance.pool:
            success = await cls._instance.invalidate_pool_on_credential_change()
            if success:
                invalidated += 1

        logger.info(f"[CloudSQLConnectionManager v114.0] Invalidated {invalidated} pool(s)")
        return invalidated

    async def shutdown(self) -> None:
        """
        Graceful shutdown with metrics reporting.

        v95.11: Now integrates with the OperationGuard to drain
        in-flight database operations before closing the pool.
        """
        if self.is_shutting_down and not self.pool:
            return

        self.is_shutting_down = True
        logger.info("🛑 Shutting down CloudSQL Connection Manager...")

        # v95.11: Start draining database operations
        drain_timeout = float(os.getenv("SHUTDOWN_DB_DRAIN_TIMEOUT", "10.0"))
        try:
            from core.resilience.graceful_shutdown import get_operation_guard_sync
            guard = get_operation_guard_sync()

            # Signal that we're draining - no new operations allowed
            await guard.begin_drain("database")
            logger.info(f"[v95.11] Draining database operations (timeout: {drain_timeout}s)...")

            # Wait for in-flight operations to complete
            active_before = guard.get_count("database")
            drained = await guard.wait_for_drain("database", timeout=drain_timeout)

            if drained:
                logger.info("[v95.11] ✅ All database operations drained successfully")
            else:
                remaining = guard.get_count("database")
                logger.warning(
                    f"[v95.11] ⚠️ Drain timeout: {remaining} operations still active "
                    f"(started with {active_before})"
                )
        except ImportError:
            logger.debug("[v95.11] OperationGuard not available, skipping drain")
        except Exception as e:
            logger.warning(f"[v95.11] Error during drain: {e}")

        # Log metrics
        self._log_final_metrics()

        # Check for unreleased connections
        async with self._checkout_lock:
            unreleased = [c for c in self._checkouts.values() if not c.released]
            if unreleased:
                logger.warning(f"⚠️ {len(unreleased)} connections not released at shutdown:")
                for c in unreleased[:5]:
                    logger.warning(f"   - #{c.checkout_id}: {c.caller_info} (held {c.age_seconds:.0f}s)")

        async with self._pool_lock:
            await self._close_pool_internal()

        logger.info("✅ Shutdown complete")

    def _log_final_metrics(self) -> None:
        """Log final metrics summary."""
        logger.info("📊 Connection Pool Metrics:")
        logger.info(f"   Checkouts: {self.metrics.total_checkouts}")
        logger.info(f"   Releases: {self.metrics.total_releases}")
        logger.info(f"   Errors: {self.metrics.total_errors}")
        logger.info(f"   Timeouts: {self.metrics.total_timeouts}")
        logger.info(f"   Leaks detected: {self.metrics.total_leaks_detected}")
        logger.info(f"   Leaks recovered: {self.metrics.total_leaks_recovered}")
        logger.info(f"   Immediate cleanups: {self.metrics.total_immediate_cleanups}")
        if self.metrics.total_releases > 0:
            logger.info(f"   Avg checkout: {self.metrics.avg_checkout_duration_ms:.1f}ms")
            logger.info(f"   Max checkout: {self.metrics.max_checkout_duration_ms:.1f}ms")
        if self.creation_time:
            uptime = (datetime.now() - self.creation_time).total_seconds()
            logger.info(f"   Uptime: {uptime:.1f}s")

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive statistics."""
        active_checkouts = [c.to_dict() for c in self._checkouts.values() if not c.released]

        return {
            "status": "running" if self.pool and not self.is_shutting_down else "stopped",
            "pool_size": self.pool.get_size() if self.pool else 0,
            "idle_size": self.pool.get_idle_size() if self.pool else 0,
            "max_size": self.db_config.get("max_connections", 0),
            "connection_count": self.connection_count,
            "error_count": self.error_count,
            "creation_time": self.creation_time.isoformat() if self.creation_time else None,
            "uptime_seconds": (datetime.now() - self.creation_time).total_seconds() if self.creation_time else 0,
            "active_checkouts": len(active_checkouts),
            "active_checkout_details": active_checkouts[:10],  # Limit for readability
            "metrics": self.metrics.to_dict(),
            "circuit_breaker": self._circuit_breaker.get_state_info() if self._circuit_breaker else None,
            "config": {
                "max_idle_time": self._conn_config.max_idle_time_seconds,
                "leak_threshold_min": self._conn_config.leaked_idle_threshold_minutes,
                "checkout_warning_sec": self._conn_config.checkout_warning_seconds,
                "aggressive_cleanup": self._conn_config.aggressive_cleanup_on_leak,
            },
        }

    async def get_stats_async(self) -> Dict[str, Any]:
        """Get comprehensive statistics (async version)."""
        return self.get_stats()

    async def get_pool(self) -> Optional["asyncpg.Pool"]:
        """
        Get the underlying connection pool.
        
        This method provides async-compatible access to the raw pool for
        advanced use cases like health checks that need direct pool access.
        
        Returns:
            The asyncpg pool if initialized, None otherwise
        """
        if not self.pool:
            logger.warning("get_pool() called but pool is not initialized")
            return None
        if self.is_shutting_down:
            logger.warning("get_pool() called but manager is shutting down")
            return None
        return self.pool

    @property
    def is_initialized(self) -> bool:
        return self.pool is not None and not self.is_shutting_down

    @property
    def config(self) -> Dict[str, Any]:
        """Legacy config dict for backward compatibility."""
        return self.db_config

    @config.setter
    def config(self, value: Dict[str, Any]) -> None:
        """Allow setting config for backward compatibility."""
        if isinstance(value, dict):
            self.db_config = value

    def on_leak_detected(self, callback: Callable) -> None:
        """Register callback for leak detection events."""
        self._on_leak_callbacks.append(callback)

    def on_error(self, callback: Callable) -> None:
        """Register callback for error events."""
        self._on_error_callbacks.append(callback)

    async def force_cleanup_leaks(self) -> int:
        """Force cleanup of all leaked connections. Returns count cleaned."""
        if self._leak_detector:
            return await self._leak_detector.check_and_cleanup()
        return await self._immediate_leak_cleanup()

    async def reload_config(self) -> None:
        """Reload configuration from environment variables."""
        self._conn_config.reload_from_env()
        logger.info("🔄 Configuration reloaded")

    def set_proxy_ready(self, ready: bool = True) -> None:
        """
        Signal that the Cloud SQL proxy is ready for connections.
        
        Call this after the proxy has been started to enable connection
        error logging. During startup mode, connection errors are suppressed
        to avoid noisy logs before the proxy is ready.
        """
        self._proxy_ready = ready
        self._startup_mode = not ready
        if ready:
            logger.info("✅ CloudSQL proxy marked as ready - connection error logging enabled")

    @property
    def is_proxy_ready(self) -> bool:
        """Check if the Cloud SQL proxy has been signaled as ready."""
        return self._proxy_ready

    def _should_suppress_error(self) -> bool:
        """Check if connection errors should be suppressed (during startup)."""
        if not self._startup_mode:
            return False
        if self._proxy_ready:
            return False
        # Check if we've exceeded the grace period
        elapsed = time.time() - self._start_time
        if elapsed >= self._startup_grace_period:
            self._startup_mode = False
            return False
        return True


# =============================================================================
# Global Singleton Accessor
# =============================================================================

_manager: Optional[CloudSQLConnectionManager] = None
_manager_lock = threading.Lock()


def get_connection_manager() -> CloudSQLConnectionManager:
    """Get singleton connection manager instance."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = CloudSQLConnectionManager()
        return _manager


async def get_connection_manager_async() -> CloudSQLConnectionManager:
    """Get singleton connection manager instance (async version)."""
    return get_connection_manager()


# =============================================================================
# PROXY WATCHDOG v1.0 - Aggressive Auto-Recovery System
# =============================================================================
# Production-grade continuous monitoring with sub-10-second recovery.
# This is the ROOT FIX for proxy crashes - proactive, not reactive.
# =============================================================================

@dataclass
class ProxyHealthMetrics:
    """
    Real-time proxy health metrics for predictive monitoring.

    v1.1: Added cold_start awareness to prevent false degradation warnings
    during initial connections after proxy restart (TLS handshake overhead).
    """
    timestamp: float = field(default_factory=time.time)
    tcp_latency_ms: Optional[float] = None
    db_latency_ms: Optional[float] = None
    connection_count: int = 0
    is_healthy: bool = False
    failure_reason: Optional[str] = None
    recovery_attempts: int = 0
    last_successful_check: Optional[float] = None
    uptime_seconds: float = 0.0
    is_cold_start: bool = False  # v1.1: Track if this is first connection after restart

    def is_degraded(self, cold_start_grace: bool = True) -> bool:
        """
        Detect degradation before full failure using latency analysis.

        v1.1: Now aware of cold start conditions. First few connections
        after proxy restart are expected to have higher latency due to:
        - TLS handshake (100-500ms)
        - Connection establishment (50-200ms)
        - Authentication (100-300ms)

        Args:
            cold_start_grace: If True, use relaxed thresholds during cold start
        """
        if not self.is_healthy:
            return True

        # v1.1: Relaxed thresholds during cold start (first 60s after restart)
        if cold_start_grace and (self.is_cold_start or self.uptime_seconds < 60):
            # Cold start thresholds: Allow higher latency during warmup
            tcp_threshold = float(os.getenv("PROXY_COLD_TCP_THRESHOLD_MS", "1000"))
            db_threshold = float(os.getenv("PROXY_COLD_DB_THRESHOLD_MS", "5000"))
        else:
            # Warm connection thresholds: Expect faster responses
            tcp_threshold = float(os.getenv("PROXY_WARM_TCP_THRESHOLD_MS", "500"))
            db_threshold = float(os.getenv("PROXY_WARM_DB_THRESHOLD_MS", "2000"))

        # Predictive: High latency indicates impending failure
        if self.tcp_latency_ms and self.tcp_latency_ms > tcp_threshold:
            return True
        if self.db_latency_ms and self.db_latency_ms > db_threshold:
            return True
        return False


class ProxyWatchdog:
    """
    Aggressive proxy health monitor with sub-10-second recovery.

    v1.0 Features:
    - Continuous monitoring every 5-10 seconds (configurable via env)
    - Immediate restart on failure (no cooldown in aggressive mode)
    - Predictive health detection (latency degradation → preemptive restart)
    - Cross-repo coordination via shared health state
    - Circuit breaker with fast recovery (5s instead of 60s)
    - Exponential backoff only after repeated failures
    - Background task auto-start during initialization

    This is the ROOT FIX for "Connection refused" errors.
    Instead of waiting for failures to cascade, we detect and fix immediately.
    """

    _instance: Optional['ProxyWatchdog'] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> 'ProxyWatchdog':
        """Thread-safe singleton."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return

        # Configuration from environment (no hardcoding)
        self._check_interval = float(os.getenv("PROXY_WATCHDOG_INTERVAL", "10.0"))
        self._aggressive_interval = float(os.getenv("PROXY_WATCHDOG_AGGRESSIVE_INTERVAL", "3.0"))
        self._recovery_cooldown = float(os.getenv("PROXY_WATCHDOG_RECOVERY_COOLDOWN", "5.0"))
        self._max_consecutive_failures = int(os.getenv("PROXY_WATCHDOG_MAX_FAILURES", "5"))
        self._predictive_restart_enabled = os.getenv("PROXY_WATCHDOG_PREDICTIVE", "true").lower() == "true"

        # v117.0: Startup grace period - use relaxed thresholds during initial warmup
        self._startup_grace_period = float(os.getenv("PROXY_WATCHDOG_STARTUP_GRACE", "60.0"))
        self._watchdog_start_time: Optional[float] = None
        self._retry_before_fail_count = int(os.getenv("PROXY_WATCHDOG_RETRY_BEFORE_FAIL", "2"))

        # State
        self._is_running = False
        self._watchdog_task: Optional[asyncio.Task] = None
        self._metrics_history: List[ProxyHealthMetrics] = []
        self._max_history = 100
        self._consecutive_failures = 0
        self._total_recoveries = 0
        self._last_recovery_time: Optional[float] = None
        self._proxy_start_time: Optional[float] = None
        self._aggressive_mode = False  # Enabled after first failure

        # Cross-repo coordination
        self._cross_repo_health_path = Path.home() / ".jarvis" / "cross_repo" / "proxy_health.json"
        self._cross_repo_health_path.parent.mkdir(parents=True, exist_ok=True)

        # Subscribers for health state changes
        self._health_subscribers: List[Callable[[ProxyHealthMetrics], Any]] = []
        self._subscriber_lock = threading.Lock()

        self._initialized = True
        logger.info(
            f"[ProxyWatchdog v1.0] Initialized "
            f"(interval={self._check_interval}s, aggressive={self._aggressive_interval}s, "
            f"predictive={self._predictive_restart_enabled})"
        )

    def subscribe(self, callback: Callable[[ProxyHealthMetrics], Any]) -> None:
        """Subscribe to health state changes."""
        with self._subscriber_lock:
            self._health_subscribers.append(callback)

    def _notify_subscribers(self, metrics: ProxyHealthMetrics) -> None:
        """Notify all subscribers of health state change (fire-and-forget)."""
        with self._subscriber_lock:
            subscribers = list(self._health_subscribers)

        for callback in subscribers:
            try:
                result = callback(metrics)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:
                logger.debug(f"[ProxyWatchdog] Subscriber callback error: {e}")

    async def start(self) -> bool:
        """
        Start the watchdog background task.

        Should be called once during supervisor initialization.
        Idempotent - safe to call multiple times.

        v132.2: Added CloudSQL configuration check - operates silently if not configured.
        """
        if self._is_running and self._watchdog_task and not self._watchdog_task.done():
            logger.debug("[ProxyWatchdog] Already running")
            return True

        # v132.2: Check if CloudSQL is configured before starting noisy monitoring
        self._cloudsql_configured = self._check_cloudsql_configuration()
        if not self._cloudsql_configured:
            skip_if_unconfigured = os.getenv("CLOUDSQL_SKIP_IF_UNCONFIGURED", "true").lower() == "true"
            optional_mode = os.getenv("CLOUDSQL_OPTIONAL", "true").lower() == "true"

            if skip_if_unconfigured or optional_mode:
                logger.info(
                    "[ProxyWatchdog v132.2] CloudSQL not configured - operating in silent mode "
                    "(no health check warnings)"
                )
                self._silent_mode = True
            else:
                self._silent_mode = False
        else:
            self._silent_mode = False

        self._is_running = True
        # v117.0: Record start time for grace period calculation
        self._watchdog_start_time = time.time()
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(),
            name="proxy_watchdog"
        )
        logger.info(
            f"[ProxyWatchdog v117.0] 🐕 Started monitoring "
            f"(grace_period={self._startup_grace_period}s, retry_before_fail={self._retry_before_fail_count}"
            f", silent={getattr(self, '_silent_mode', False)})"
        )
        return True

    def _check_cloudsql_configuration(self) -> bool:
        """v132.2: Check if CloudSQL is properly configured."""
        gcp_project = os.getenv("GCP_PROJECT", "")
        cloudsql_instance = os.getenv("CLOUDSQL_INSTANCE_NAME", "")
        return bool(gcp_project and cloudsql_instance)

    async def stop(self) -> None:
        """Stop the watchdog gracefully."""
        self._is_running = False
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        logger.info("[ProxyWatchdog] Stopped")

    async def _watchdog_loop(self) -> None:
        """
        Main watchdog loop with adaptive monitoring.

        Uses aggressive mode after first failure for faster recovery.
        """
        logger.info("[ProxyWatchdog] Starting monitoring loop...")

        while self._is_running:
            try:
                # Adaptive interval: aggressive after failures
                interval = self._aggressive_interval if self._aggressive_mode else self._check_interval
                await asyncio.sleep(interval)

                # Perform health check
                metrics = await self._check_health()

                # Store metrics history
                self._metrics_history.append(metrics)
                if len(self._metrics_history) > self._max_history:
                    self._metrics_history.pop(0)

                # Handle health state
                if not metrics.is_healthy:
                    await self._handle_failure(metrics)
                else:
                    self._handle_success(metrics)

                # v1.1: Predictive restart if degraded (with cold start awareness)
                # During cold start, we use relaxed thresholds - only warn on actual degradation
                is_actual_degradation = metrics.is_degraded(cold_start_grace=True)
                is_cold_start = metrics.is_cold_start or metrics.uptime_seconds < 60

                if self._predictive_restart_enabled and is_actual_degradation and metrics.is_healthy:
                    if is_cold_start:
                        # Cold start: high latency is expected, just log info
                        logger.info(
                            f"[ProxyWatchdog] Cold start latency "
                            f"(tcp={metrics.tcp_latency_ms:.0f}ms, db={metrics.db_latency_ms:.0f}ms, "
                            f"uptime={metrics.uptime_seconds:.1f}s) - normal during warmup"
                        )
                    else:
                        # Warm connection degradation: this is concerning
                        logger.warning(
                            f"[ProxyWatchdog] ⚠️ Degraded performance detected "
                            f"(tcp={metrics.tcp_latency_ms:.0f}ms, db={metrics.db_latency_ms:.0f}ms) - "
                            f"scheduling preemptive restart"
                        )
                        # Only enable aggressive mode for actual degradation (not cold start)
                        self._aggressive_mode = True

                # Notify subscribers
                self._notify_subscribers(metrics)

                # Update cross-repo health state
                await self._update_cross_repo_health(metrics)

            except asyncio.CancelledError:
                logger.info("[ProxyWatchdog] Loop cancelled")
                break
            except Exception as e:
                logger.error(f"[ProxyWatchdog] Loop error: {e}", exc_info=True)
                await asyncio.sleep(5.0)  # Brief pause on error

    async def _check_health(self) -> ProxyHealthMetrics:
        """
        Perform comprehensive health check with latency measurement.
        """
        metrics = ProxyHealthMetrics()

        try:
            # Get database config
            gate = get_readiness_gate()
            if not gate._ensure_config():
                metrics.failure_reason = "config_missing"
                return metrics

            db_config = gate._db_config
            if not db_config:
                metrics.failure_reason = "config_empty"
                return metrics

            host = db_config.get("host", "127.0.0.1")
            port = db_config.get("port", 5432)

            # TCP latency check
            tcp_start = time.time()
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=5.0
                )
                metrics.tcp_latency_ms = (time.time() - tcp_start) * 1000
                writer.close()
                await writer.wait_closed()
            except Exception as e:
                metrics.failure_reason = f"tcp_failed: {e}"
                return metrics

            # DB-level check with latency
            if ASYNCPG_AVAILABLE:
                db_start = time.time()
                try:
                    # Use TLS-safe connection
                    conn = await tls_safe_connect(
                        host=host,
                        port=port,
                        database=db_config.get("database", "jarvis_learning"),
                        user=db_config.get("user", "jarvis"),
                        password=db_config.get("password", ""),
                        timeout=10.0
                    )
                    if conn:
                        # Quick SELECT 1 test
                        await conn.fetchval("SELECT 1")
                        metrics.db_latency_ms = (time.time() - db_start) * 1000
                        await conn.close()
                        metrics.is_healthy = True
                        metrics.last_successful_check = time.time()

                        # Calculate uptime and cold start status
                        if self._proxy_start_time:
                            metrics.uptime_seconds = time.time() - self._proxy_start_time
                            # v1.1: Mark as cold start if proxy restarted within 60s
                            metrics.is_cold_start = metrics.uptime_seconds < 60.0
                        else:
                            metrics.is_cold_start = True  # No startup time = cold start
                    else:
                        metrics.failure_reason = "connection_returned_none"
                except Exception as e:
                    error_str = str(e).lower()
                    if "password" in error_str or "authentication" in error_str:
                        metrics.failure_reason = "credentials"
                    elif "connection refused" in error_str or "errno 61" in error_str:
                        metrics.failure_reason = "proxy_not_running"
                    else:
                        metrics.failure_reason = f"db_error: {e}"
            else:
                # No asyncpg - TCP success is enough
                metrics.is_healthy = True
                metrics.last_successful_check = time.time()

        except Exception as e:
            metrics.failure_reason = f"check_error: {e}"

        return metrics

    async def _handle_failure(self, metrics: ProxyHealthMetrics) -> None:
        """
        v117.0: Enhanced failure handling with retry-before-signal and grace period.

        Improvements:
        - Performs immediate retries before marking as failed
        - Uses relaxed thresholds during startup grace period
        - Only signals NOT_READY after confirming persistent failure
        """
        # v117.0: Check if we're in startup grace period
        in_grace_period = False
        if self._watchdog_start_time:
            time_since_start = time.time() - self._watchdog_start_time
            in_grace_period = time_since_start < self._startup_grace_period

        # v117.0: Perform immediate retries before counting as failure
        if self._consecutive_failures == 0 and self._retry_before_fail_count > 0:
            logger.debug(
                f"[ProxyWatchdog v117.0] First failure detected - "
                f"performing {self._retry_before_fail_count} immediate retries"
            )

            for retry in range(self._retry_before_fail_count):
                await asyncio.sleep(1.0)  # Brief pause between retries
                retry_metrics = await self._check_health()

                if retry_metrics.is_healthy:
                    logger.info(
                        f"[ProxyWatchdog v117.0] ✅ Retry {retry + 1}/{self._retry_before_fail_count} "
                        f"succeeded - transient failure resolved"
                    )
                    return  # Transient failure, don't count it

            logger.debug(
                f"[ProxyWatchdog v117.0] All {self._retry_before_fail_count} retries failed - "
                f"counting as real failure"
            )

        self._consecutive_failures += 1
        self._aggressive_mode = True
        metrics.recovery_attempts = self._consecutive_failures

        # v117.0: During grace period, use higher failure threshold
        effective_max_failures = self._max_consecutive_failures
        if in_grace_period:
            effective_max_failures = self._max_consecutive_failures * 2
            logger.debug(
                f"[ProxyWatchdog v117.0] In grace period ({time_since_start:.1f}s) - "
                f"using relaxed threshold: {effective_max_failures}"
            )

        # v132.2: Only warn if not in silent mode (CloudSQL configured but failing)
        if getattr(self, '_silent_mode', False):
            logger.debug(
                f"[ProxyWatchdog v132.2] Health check failed (silent mode) "
                f"(reason={metrics.failure_reason}, consecutive={self._consecutive_failures})"
            )
        else:
            logger.warning(
                f"[ProxyWatchdog v117.0] ❌ Health check failed "
                f"(reason={metrics.failure_reason}, consecutive={self._consecutive_failures}/"
                f"{effective_max_failures}{' [grace period]' if in_grace_period else ''})"
            )

        # Check cooldown
        if self._last_recovery_time:
            elapsed = time.time() - self._last_recovery_time
            if elapsed < self._recovery_cooldown:
                logger.debug(
                    f"[ProxyWatchdog v117.0] Recovery cooldown active "
                    f"({elapsed:.1f}s / {self._recovery_cooldown}s)"
                )
                return

        # Attempt recovery
        if self._consecutive_failures <= effective_max_failures:
            await self._attempt_recovery(metrics)
        else:
            # Max failures - longer backoff
            backoff = min(60.0, 5.0 * (2 ** (self._consecutive_failures - effective_max_failures)))
            logger.error(
                f"[ProxyWatchdog v117.0] ❌ Max failures exceeded - backing off {backoff:.0f}s"
            )
            await asyncio.sleep(backoff)
            # Reset and try again
            self._consecutive_failures = 0

    async def _attempt_recovery(self, metrics: ProxyHealthMetrics) -> None:
        """
        Attempt to recover the proxy.
        """
        self._last_recovery_time = time.time()
        self._total_recoveries += 1

        logger.info(
            f"[ProxyWatchdog] 🔄 Attempting recovery #{self._total_recoveries} "
            f"(failure={metrics.failure_reason})"
        )

        try:
            # Import proxy manager
            try:
                from intelligence.cloud_sql_proxy_manager import get_proxy_manager
            except ImportError:
                from backend.intelligence.cloud_sql_proxy_manager import get_proxy_manager

            proxy_manager = get_proxy_manager()

            # Force restart
            success = await proxy_manager.start(force_restart=True, max_retries=2)

            if success:
                logger.info("[ProxyWatchdog] ✅ Proxy recovery successful")
                self._proxy_start_time = time.time()
                self._consecutive_failures = 0
                self._aggressive_mode = False

                # Verify with DB-level check
                await asyncio.sleep(2.0)  # Brief settle time
                verification = await self._check_health()
                if verification.is_healthy:
                    logger.info(
                        f"[ProxyWatchdog] ✅ DB-level verification passed "
                        f"(latency={verification.db_latency_ms:.0f}ms)"
                    )
                    # Notify the readiness gate
                    gate = get_readiness_gate()
                    await gate._signal_agent_registry_ready(True)
                else:
                    logger.warning(
                        f"[ProxyWatchdog] ⚠️ DB-level verification failed: "
                        f"{verification.failure_reason}"
                    )
            else:
                logger.error("[ProxyWatchdog] ❌ Proxy recovery failed")

        except FileNotFoundError:
            logger.warning("[ProxyWatchdog] Proxy manager config not found - SQLite only deployment?")
        except Exception as e:
            logger.error(f"[ProxyWatchdog] Recovery error: {e}", exc_info=True)

    def _handle_success(self, metrics: ProxyHealthMetrics) -> None:
        """Handle successful health check."""
        if self._consecutive_failures > 0:
            logger.info(
                f"[ProxyWatchdog] ✅ Health restored after {self._consecutive_failures} failures"
            )

        self._consecutive_failures = 0

        # Disable aggressive mode after sustained health
        if self._aggressive_mode and len(self._metrics_history) >= 5:
            recent = self._metrics_history[-5:]
            if all(m.is_healthy for m in recent):
                self._aggressive_mode = False
                logger.debug("[ProxyWatchdog] Aggressive mode disabled (sustained health)")

    async def _update_cross_repo_health(self, metrics: ProxyHealthMetrics) -> None:
        """
        Update cross-repo health state for coordination.

        Other repos can read this to determine if they should attempt CloudSQL connections.
        """
        try:
            health_data = {
                "timestamp": metrics.timestamp,
                "is_healthy": metrics.is_healthy,
                "failure_reason": metrics.failure_reason,
                "tcp_latency_ms": metrics.tcp_latency_ms,
                "db_latency_ms": metrics.db_latency_ms,
                "uptime_seconds": metrics.uptime_seconds,
                "consecutive_failures": self._consecutive_failures,
                "total_recoveries": self._total_recoveries,
                "aggressive_mode": self._aggressive_mode,
                "reporter": "jarvis-core",
                "pid": os.getpid(),
            }

            # Write atomically
            temp_path = self._cross_repo_health_path.with_suffix('.tmp')
            with open(temp_path, 'w') as f:
                json.dump(health_data, f, indent=2)
            temp_path.rename(self._cross_repo_health_path)

        except Exception as e:
            logger.debug(f"[ProxyWatchdog] Cross-repo health update failed: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get current watchdog status."""
        recent_health = self._metrics_history[-1] if self._metrics_history else None
        return {
            "is_running": self._is_running,
            "consecutive_failures": self._consecutive_failures,
            "total_recoveries": self._total_recoveries,
            "aggressive_mode": self._aggressive_mode,
            "check_interval": self._aggressive_interval if self._aggressive_mode else self._check_interval,
            "last_check": recent_health.timestamp if recent_health else None,
            "is_healthy": recent_health.is_healthy if recent_health else None,
            "failure_reason": recent_health.failure_reason if recent_health else None,
            "tcp_latency_ms": recent_health.tcp_latency_ms if recent_health else None,
            "db_latency_ms": recent_health.db_latency_ms if recent_health else None,
            "uptime_seconds": recent_health.uptime_seconds if recent_health else None,
        }


# Singleton accessor
_proxy_watchdog: Optional[ProxyWatchdog] = None
_proxy_watchdog_lock = threading.Lock()


def get_proxy_watchdog() -> ProxyWatchdog:
    """Get singleton ProxyWatchdog instance."""
    global _proxy_watchdog
    with _proxy_watchdog_lock:
        if _proxy_watchdog is None:
            _proxy_watchdog = ProxyWatchdog()
        return _proxy_watchdog


async def start_proxy_watchdog() -> bool:
    """
    Start the proxy watchdog.

    Call this during supervisor initialization to enable automatic
    proxy health monitoring and recovery.
    """
    watchdog = get_proxy_watchdog()
    return await watchdog.start()


async def stop_proxy_watchdog() -> None:
    """Stop the proxy watchdog gracefully."""
    if _proxy_watchdog:
        await _proxy_watchdog.stop()


# =============================================================================
# Enhanced Startup Integration
# =============================================================================

async def ensure_cloudsql_ready_with_watchdog(
    timeout: float = 60.0,
    start_watchdog: bool = True
) -> ReadinessResult:
    """
    Comprehensive CloudSQL startup with watchdog integration.

    This is the recommended way to initialize CloudSQL for production:
    1. Ensures proxy is running and DB-level ready
    2. Starts the background watchdog for continuous monitoring
    3. Coordinates with AgentRegistry for dependency tracking

    Args:
        timeout: Maximum time to wait for readiness
        start_watchdog: If True, start the background watchdog after ready

    Returns:
        ReadinessResult with status

    Example:
        result = await ensure_cloudsql_ready_with_watchdog()
        if result.state == ReadinessState.READY:
            print("CloudSQL ready with watchdog monitoring!")
    """
    gate = get_readiness_gate()

    # First ensure proxy is ready
    result = await gate.ensure_proxy_ready(timeout=timeout)

    if result.state == ReadinessState.READY and start_watchdog:
        # Start the watchdog for continuous monitoring
        watchdog = get_proxy_watchdog()
        await watchdog.start()
        logger.info(
            "[CloudSQL] ✅ Ready with watchdog monitoring enabled "
            f"(interval={watchdog._check_interval}s)"
        )

    return result
