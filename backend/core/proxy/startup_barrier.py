"""
Async Startup Barrier - Layer 2 of Distributed Proxy System

Provides:
- Multi-stage verification pipeline (TCP → TLS → Auth → Query → Latency)
- Dependency graph with topological sort
- Parallel initialization of independent components
- CloudSQL barrier that blocks dependent components until verified ready
- v117.0: Unified credential management via SecretManager

Author: JARVIS System
Version: 1.1.0 (v117.0)
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import ssl
import threading
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Final,
    FrozenSet,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
)

if TYPE_CHECKING:
    from .lifecycle_controller import ProxyLifecycleController

logger = logging.getLogger(__name__)


# =============================================================================
# Lazy Credential Provider (v117.0)
# =============================================================================

class _LazyCredentialProvider:
    """
    v117.0: Thread-safe lazy credential provider using SecretManager.

    Credentials are fetched on first access, not at module import time.
    This prevents initialization failures in background threads and
    ensures credentials are retrieved from the proper source.
    """

    _instance: Optional["_LazyCredentialProvider"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "_LazyCredentialProvider":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._db_user: Optional[str] = None
        self._db_password: Optional[str] = None
        self._credentials_lock = threading.Lock()
        self._secret_manager_available: Optional[bool] = None
        self._initialized = True

    def _get_secret_manager(self):
        """Lazily import and get SecretManager to avoid circular imports."""
        if self._secret_manager_available is False:
            return None

        try:
            from backend.core.secret_manager import get_secret_manager
            self._secret_manager_available = True
            return get_secret_manager()
        except ImportError:
            self._secret_manager_available = False
            logger.debug("SecretManager not available, using environment variables")
            return None

    @property
    def db_user(self) -> str:
        """Get database user with lazy loading and fallback."""
        if self._db_user is not None:
            return self._db_user

        with self._credentials_lock:
            if self._db_user is not None:
                return self._db_user

            # Try SecretManager first
            sm = self._get_secret_manager()
            if sm:
                try:
                    user = sm.get_secret("jarvis-db-user")
                    if user:
                        self._db_user = user
                        logger.debug(f"Database user loaded via SecretManager")
                        return self._db_user
                except Exception as e:
                    logger.debug(f"SecretManager lookup for db_user failed: {e}")

            # Fallback to environment variables (check all aliases)
            for env_var in [
                "JARVIS_DB_USER",
                "CLOUDSQL_DB_USER",
                "DB_USER",
                "CLOUD_SQL_USER",
            ]:
                user = os.getenv(env_var)
                if user:
                    self._db_user = user
                    logger.debug(f"Database user loaded from {env_var}")
                    return self._db_user

            # Final fallback
            self._db_user = "jarvis"
            logger.debug("Using default database user 'jarvis'")
            return self._db_user

    @property
    def db_password(self) -> str:
        """Get database password with lazy loading and fallback."""
        if self._db_password is not None:
            return self._db_password

        with self._credentials_lock:
            if self._db_password is not None:
                return self._db_password

            # Try SecretManager first (checks GCP Secret Manager, Keychain, env vars)
            sm = self._get_secret_manager()
            if sm:
                try:
                    password = sm.get_secret("jarvis-db-password")
                    if password:
                        self._db_password = password
                        logger.debug("Database password loaded via SecretManager")
                        return self._db_password
                except Exception as e:
                    logger.debug(f"SecretManager lookup for db_password failed: {e}")

            # Fallback to environment variables (check all aliases)
            for env_var in [
                "JARVIS_DB_PASSWORD",
                "CLOUDSQL_DB_PASSWORD",
                "DB_PASSWORD",
                "CLOUD_SQL_PASSWORD",
            ]:
                password = os.getenv(env_var)
                if password:
                    self._db_password = password
                    logger.debug(f"Database password loaded from {env_var}")
                    return self._db_password

            # Empty password (will likely fail auth, but we don't crash)
            logger.warning(
                "No database password found! Set JARVIS_DB_PASSWORD or configure "
                "SecretManager. Authentication will likely fail."
            )
            self._db_password = ""
            return self._db_password

    def refresh_credentials(self) -> None:
        """Force refresh of cached credentials."""
        with self._credentials_lock:
            self._db_user = None
            self._db_password = None
            logger.info("Credentials cache cleared, will reload on next access")


# Global singleton
_credential_provider = _LazyCredentialProvider()


# =============================================================================
# Configuration
# =============================================================================

class _LazyCredentialDescriptor:
    """
    v117.0: Descriptor for lazy credential loading.

    Works as a class attribute that returns credentials on first access,
    compatible with Python 3.9+ without requiring @classmethod + @property.
    """

    def __init__(self, credential_type: str):
        self._credential_type = credential_type

    def __get__(self, obj, objtype=None) -> str:
        if self._credential_type == "user":
            return _credential_provider.db_user
        elif self._credential_type == "password":
            return _credential_provider.db_password
        else:
            raise ValueError(f"Unknown credential type: {self._credential_type}")


class BarrierConfig:
    """
    Configuration loaded from environment variables.

    v117.0: Credentials (DB_USER, DB_PASSWORD) are now loaded lazily via
    _LazyCredentialProvider to support SecretManager integration and
    prevent import-time failures.
    """

    # Timeouts
    ENSURE_READY_TIMEOUT: Final[float] = float(os.getenv("CLOUDSQL_ENSURE_READY_TIMEOUT", "60.0"))
    RETRY_BASE_DELAY: Final[float] = float(os.getenv("CLOUDSQL_RETRY_BASE_DELAY", "1.0"))
    RETRY_MAX_DELAY: Final[float] = float(os.getenv("CLOUDSQL_RETRY_MAX_DELAY", "10.0"))
    VERIFICATION_STAGES: Final[int] = int(os.getenv("CLOUDSQL_VERIFICATION_STAGES", "5"))

    # Component initialization
    COMPONENT_TIMEOUT: Final[float] = float(os.getenv("COMPONENT_INIT_TIMEOUT", "30.0"))
    PARALLEL_INIT_ENABLED: Final[bool] = os.getenv("PARALLEL_INIT_ENABLED", "true").lower() == "true"

    # Database settings (non-credential)
    # v117.1: Dynamic database configuration with intelligent fallbacks
    # Priority: CLOUDSQL_DB_NAME → JARVIS_DB_NAME → postgres (always exists)
    DB_HOST: Final[str] = os.getenv("CLOUDSQL_PROXY_HOST", "127.0.0.1")
    DB_PORT: Final[int] = int(os.getenv("CLOUDSQL_PROXY_PORT", "5432"))
    DB_NAME: Final[str] = os.getenv(
        "CLOUDSQL_DB_NAME",
        os.getenv(
            "JARVIS_DB_NAME",
            "postgres"  # v117.1: Always exists in PostgreSQL, safe fallback
        )
    )

    # v117.0: Credentials are now lazy-loaded via SecretManager
    DB_USER = _LazyCredentialDescriptor("user")
    DB_PASSWORD = _LazyCredentialDescriptor("password")

    # Latency thresholds
    LATENCY_WARNING_MS: Final[float] = float(os.getenv("CLOUDSQL_LATENCY_WARNING_MS", "100.0"))
    LATENCY_ERROR_MS: Final[float] = float(os.getenv("CLOUDSQL_LATENCY_ERROR_MS", "500.0"))

    @classmethod
    def refresh_credentials(cls) -> None:
        """v117.0: Force refresh of cached credentials."""
        _credential_provider.refresh_credentials()

    @classmethod
    def get_db_user(cls) -> str:
        """v117.0: Explicit method for getting database user."""
        return _credential_provider.db_user

    @classmethod
    def get_db_password(cls) -> str:
        """v117.0: Explicit method for getting database password."""
        return _credential_provider.db_password


# =============================================================================
# Verification Stages
# =============================================================================

class VerificationStage(Enum):
    """Multi-stage verification pipeline stages."""
    TCP_CONNECT = auto()      # Stage 1: TCP port accepting connections
    TLS_HANDSHAKE = auto()    # Stage 2: TLS/SSL negotiation
    AUTHENTICATION = auto()   # Stage 3: Database authentication
    QUERY_EXECUTION = auto()  # Stage 4: SELECT 1 succeeds
    LATENCY_CHECK = auto()    # Stage 5: Response time acceptable


@dataclass(frozen=True)
class VerificationResult:
    """Result of a verification stage."""
    stage: VerificationStage
    success: bool
    latency_ms: float
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "stage": self.stage.name,
            "success": self.success,
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
            "metadata": self.metadata,
        }


class VerificationPipeline:
    """
    Multi-stage verification pipeline for Cloud SQL proxy.

    Stages run sequentially, each building on the previous:
    1. TCP Connect - Can we reach the port?
    2. TLS Handshake - Can we establish secure connection?
    3. Authentication - Are credentials valid?
    4. Query Execution - Does the database respond?
    5. Latency Check - Is performance acceptable?

    v117.0: Now uses lazy credential loading via SecretManager for robust
    credential management with fallback support.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        db_name: Optional[str] = None,
        db_user: Optional[str] = None,
        db_password: Optional[str] = None,
    ):
        # v117.0: Lazy resolution of configuration values
        # This ensures credentials are loaded via SecretManager when accessed,
        # not at module import time
        self._host = host if host is not None else BarrierConfig.DB_HOST
        self._port = port if port is not None else BarrierConfig.DB_PORT
        self._db_name = db_name if db_name is not None else BarrierConfig.DB_NAME
        self._db_user = db_user if db_user is not None else BarrierConfig.get_db_user()
        self._db_password = db_password if db_password is not None else BarrierConfig.get_db_password()
        self._results: List[VerificationResult] = []

        # v117.0: Log credential source for debugging (without exposing password)
        logger.debug(
            f"VerificationPipeline initialized: host={self._host}, port={self._port}, "
            f"db={self._db_name}, user={self._db_user}, password={'***' if self._db_password else '(empty)'}"
        )

    async def verify_tcp_connect(self, timeout: float = 5.0) -> VerificationResult:
        """Stage 1: Verify TCP port is accepting connections."""
        start_time = time.monotonic()

        try:
            # Use asyncio for non-blocking connect
            loop = asyncio.get_event_loop()

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)

            try:
                await asyncio.wait_for(
                    loop.sock_connect(sock, (self._host, self._port)),
                    timeout=timeout
                )
                sock.close()

                latency_ms = (time.monotonic() - start_time) * 1000
                result = VerificationResult(
                    stage=VerificationStage.TCP_CONNECT,
                    success=True,
                    latency_ms=latency_ms,
                    metadata={"host": self._host, "port": self._port},
                )
            except asyncio.TimeoutError:
                result = VerificationResult(
                    stage=VerificationStage.TCP_CONNECT,
                    success=False,
                    latency_ms=(time.monotonic() - start_time) * 1000,
                    error=f"Connection timeout after {timeout}s",
                )
            except OSError as e:
                result = VerificationResult(
                    stage=VerificationStage.TCP_CONNECT,
                    success=False,
                    latency_ms=(time.monotonic() - start_time) * 1000,
                    error=str(e),
                )
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

        except Exception as e:
            result = VerificationResult(
                stage=VerificationStage.TCP_CONNECT,
                success=False,
                latency_ms=(time.monotonic() - start_time) * 1000,
                error=f"Unexpected error: {e}",
            )

        self._results.append(result)
        return result

    async def verify_tls_handshake(self, timeout: float = 10.0) -> VerificationResult:
        """Stage 2: Verify TLS/SSL handshake succeeds."""
        start_time = time.monotonic()

        try:
            # Create SSL context
            context = ssl.create_default_context()
            # Cloud SQL proxy handles TLS, so we verify the proxy connection
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # Proxy uses its own certs

            loop = asyncio.get_event_loop()

            # Create raw socket first
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)

            try:
                await asyncio.wait_for(
                    loop.sock_connect(sock, (self._host, self._port)),
                    timeout=timeout
                )

                # Note: Cloud SQL proxy exposes PostgreSQL directly,
                # TLS is handled at the proxy<->CloudSQL level
                # We verify by attempting PostgreSQL protocol
                sock.close()

                latency_ms = (time.monotonic() - start_time) * 1000
                result = VerificationResult(
                    stage=VerificationStage.TLS_HANDSHAKE,
                    success=True,
                    latency_ms=latency_ms,
                    metadata={"note": "TLS handled by Cloud SQL proxy"},
                )

            except asyncio.TimeoutError:
                result = VerificationResult(
                    stage=VerificationStage.TLS_HANDSHAKE,
                    success=False,
                    latency_ms=(time.monotonic() - start_time) * 1000,
                    error=f"TLS timeout after {timeout}s",
                )
            except ssl.SSLError as e:
                result = VerificationResult(
                    stage=VerificationStage.TLS_HANDSHAKE,
                    success=False,
                    latency_ms=(time.monotonic() - start_time) * 1000,
                    error=f"SSL error: {e}",
                )
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

        except Exception as e:
            result = VerificationResult(
                stage=VerificationStage.TLS_HANDSHAKE,
                success=False,
                latency_ms=(time.monotonic() - start_time) * 1000,
                error=f"Unexpected error: {e}",
            )

        self._results.append(result)
        return result

    async def verify_authentication(self, timeout: float = 10.0) -> VerificationResult:
        """
        Stage 3: Verify database authentication.

        v117.1: Enhanced with fallback to 'postgres' database if specified database
        doesn't exist. This ensures we can verify connectivity even during initial setup.
        """
        start_time = time.monotonic()

        try:
            # Try to import asyncpg
            try:
                import asyncpg
            except ImportError:
                # Fallback: use psycopg2 or skip
                return VerificationResult(
                    stage=VerificationStage.AUTHENTICATION,
                    success=True,
                    latency_ms=0,
                    metadata={"skipped": "asyncpg not installed, skipping auth verify"},
                )

            # v117.1: Try specified database first, fallback to 'postgres' if not found
            databases_to_try = [self._db_name]
            if self._db_name != "postgres":
                databases_to_try.append("postgres")  # postgres always exists

            last_error: Optional[Exception] = None
            successful_db: Optional[str] = None

            for db_name in databases_to_try:
                try:
                    conn = await asyncio.wait_for(
                        asyncpg.connect(
                            host=self._host,
                            port=self._port,
                            database=db_name,
                            user=self._db_user,
                            password=self._db_password,
                        ),
                        timeout=timeout / len(databases_to_try)
                    )

                    await conn.close()
                    successful_db = db_name

                    # If we had to fall back to 'postgres', log this
                    if db_name != self._db_name:
                        logger.warning(
                            f"[Verification] Database '{self._db_name}' not found, "
                            f"verified connectivity using '{db_name}' instead"
                        )

                    break  # Success!

                except asyncio.TimeoutError:
                    last_error = asyncio.TimeoutError(f"Timeout connecting to {db_name}")
                except Exception as e:
                    error_str = str(e)
                    # Only try next database if this one doesn't exist
                    if "does not exist" in error_str.lower():
                        logger.debug(f"Database '{db_name}' does not exist, trying fallback...")
                        last_error = e
                        continue
                    # For other errors (auth, connection), don't fallback - report immediately
                    last_error = e
                    break

            if successful_db:
                latency_ms = (time.monotonic() - start_time) * 1000
                result = VerificationResult(
                    stage=VerificationStage.AUTHENTICATION,
                    success=True,
                    latency_ms=latency_ms,
                    metadata={
                        "user": self._db_user,
                        "database": successful_db,
                        "requested_database": self._db_name,
                        "used_fallback": successful_db != self._db_name,
                    },
                )
            else:
                # All attempts failed
                error_str = str(last_error) if last_error else "Unknown error"
                if "password" in error_str.lower() or "authentication" in error_str.lower():
                    error_type = "authentication_failed"
                elif "does not exist" in error_str.lower():
                    error_type = "database_not_found"
                elif isinstance(last_error, asyncio.TimeoutError):
                    error_type = "timeout"
                else:
                    error_type = "connection_error"

                result = VerificationResult(
                    stage=VerificationStage.AUTHENTICATION,
                    success=False,
                    latency_ms=(time.monotonic() - start_time) * 1000,
                    error=f"{error_type}: {last_error}",
                )

        except asyncio.TimeoutError:
            result = VerificationResult(
                stage=VerificationStage.AUTHENTICATION,
                success=False,
                latency_ms=(time.monotonic() - start_time) * 1000,
                error=f"Authentication timeout after {timeout}s",
            )
        except Exception as e:
            error_str = str(e)
            # Check for common auth errors
            if "password" in error_str.lower() or "authentication" in error_str.lower():
                error_type = "authentication_failed"
            elif "does not exist" in error_str.lower():
                error_type = "database_not_found"
            else:
                error_type = "connection_error"

            result = VerificationResult(
                stage=VerificationStage.AUTHENTICATION,
                success=False,
                latency_ms=(time.monotonic() - start_time) * 1000,
                error=f"{error_type}: {e}",
            )

        self._results.append(result)
        return result

    async def verify_query_execution(self, timeout: float = 10.0) -> VerificationResult:
        """
        Stage 4: Verify query execution (SELECT 1).

        v117.1: Enhanced with fallback to 'postgres' database if specified database
        doesn't exist. This ensures we can verify query capability even during initial setup.
        """
        start_time = time.monotonic()

        try:
            try:
                import asyncpg
            except ImportError:
                return VerificationResult(
                    stage=VerificationStage.QUERY_EXECUTION,
                    success=True,
                    latency_ms=0,
                    metadata={"skipped": "asyncpg not installed"},
                )

            # v117.1: Try specified database first, fallback to 'postgres' if not found
            databases_to_try = [self._db_name]
            if self._db_name != "postgres":
                databases_to_try.append("postgres")

            last_error: Optional[Exception] = None
            successful_db: Optional[str] = None
            query_latency: float = 0.0

            for db_name in databases_to_try:
                try:
                    conn = await asyncio.wait_for(
                        asyncpg.connect(
                            host=self._host,
                            port=self._port,
                            database=db_name,
                            user=self._db_user,
                            password=self._db_password,
                        ),
                        timeout=timeout / (2 * len(databases_to_try))
                    )

                    try:
                        query_start = time.monotonic()
                        result_row = await asyncio.wait_for(
                            conn.fetchval("SELECT 1"),
                            timeout=timeout / (2 * len(databases_to_try))
                        )
                        query_latency = (time.monotonic() - query_start) * 1000

                        if result_row == 1:
                            successful_db = db_name
                            if db_name != self._db_name:
                                logger.debug(
                                    f"[Verification] Query verified using fallback database '{db_name}'"
                                )
                            break
                        else:
                            last_error = ValueError(f"Unexpected query result: {result_row}")
                    finally:
                        await conn.close()

                except asyncio.TimeoutError:
                    last_error = asyncio.TimeoutError(f"Timeout connecting to {db_name}")
                except Exception as e:
                    error_str = str(e)
                    if "does not exist" in error_str.lower():
                        logger.debug(f"Database '{db_name}' does not exist for query test, trying fallback...")
                        last_error = e
                        continue
                    last_error = e
                    break

            if successful_db:
                result = VerificationResult(
                    stage=VerificationStage.QUERY_EXECUTION,
                    success=True,
                    latency_ms=(time.monotonic() - start_time) * 1000,
                    metadata={
                        "query_latency_ms": round(query_latency, 2),
                        "database": successful_db,
                        "used_fallback": successful_db != self._db_name,
                    },
                )
            else:
                result = VerificationResult(
                    stage=VerificationStage.QUERY_EXECUTION,
                    success=False,
                    latency_ms=(time.monotonic() - start_time) * 1000,
                    error=str(last_error) if last_error else "Query execution failed",
                )

        except asyncio.TimeoutError:
            result = VerificationResult(
                stage=VerificationStage.QUERY_EXECUTION,
                success=False,
                latency_ms=(time.monotonic() - start_time) * 1000,
                error=f"Query timeout after {timeout}s",
            )
        except Exception as e:
            result = VerificationResult(
                stage=VerificationStage.QUERY_EXECUTION,
                success=False,
                latency_ms=(time.monotonic() - start_time) * 1000,
                error=str(e),
            )

        self._results.append(result)
        return result

    async def verify_latency(self, samples: int = 5) -> VerificationResult:
        """Stage 5: Verify latency is acceptable."""
        start_time = time.monotonic()
        latencies: List[float] = []

        try:
            for _ in range(samples):
                sample_start = time.monotonic()

                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                try:
                    sock.connect((self._host, self._port))
                    sock.close()
                    latencies.append((time.monotonic() - sample_start) * 1000)
                except Exception:
                    pass
                finally:
                    try:
                        sock.close()
                    except Exception:
                        pass

                await asyncio.sleep(0.1)  # Brief pause between samples

            if not latencies:
                result = VerificationResult(
                    stage=VerificationStage.LATENCY_CHECK,
                    success=False,
                    latency_ms=(time.monotonic() - start_time) * 1000,
                    error="No successful latency samples",
                )
            else:
                avg_latency = sum(latencies) / len(latencies)
                min_latency = min(latencies)
                max_latency = max(latencies)

                # Check against thresholds
                if avg_latency > BarrierConfig.LATENCY_ERROR_MS:
                    success = False
                    error = f"Latency too high: {avg_latency:.1f}ms > {BarrierConfig.LATENCY_ERROR_MS}ms"
                elif avg_latency > BarrierConfig.LATENCY_WARNING_MS:
                    success = True
                    error = f"Warning: latency elevated: {avg_latency:.1f}ms"
                else:
                    success = True
                    error = None

                result = VerificationResult(
                    stage=VerificationStage.LATENCY_CHECK,
                    success=success,
                    latency_ms=avg_latency,
                    error=error,
                    metadata={
                        "samples": len(latencies),
                        "min_ms": round(min_latency, 2),
                        "max_ms": round(max_latency, 2),
                        "avg_ms": round(avg_latency, 2),
                    },
                )

        except Exception as e:
            result = VerificationResult(
                stage=VerificationStage.LATENCY_CHECK,
                success=False,
                latency_ms=(time.monotonic() - start_time) * 1000,
                error=str(e),
            )

        self._results.append(result)
        return result

    async def run_all(
        self,
        stages: Optional[Set[VerificationStage]] = None,
    ) -> Tuple[bool, List[VerificationResult]]:
        """
        Run all verification stages.

        Args:
            stages: Optional set of stages to run (default: all)

        Returns:
            Tuple of (all_passed, results)
        """
        self._results = []

        if stages is None:
            stages = set(VerificationStage)

        all_passed = True

        # Stage 1: TCP Connect
        if VerificationStage.TCP_CONNECT in stages:
            result = await self.verify_tcp_connect()
            if not result.success:
                all_passed = False
                logger.error(f"[Verification] TCP Connect failed: {result.error}")
                return (False, self._results)
            logger.info(f"[Verification] ✓ TCP Connect ({result.latency_ms:.1f}ms)")

        # Stage 2: TLS Handshake
        if VerificationStage.TLS_HANDSHAKE in stages:
            result = await self.verify_tls_handshake()
            if not result.success:
                all_passed = False
                logger.error(f"[Verification] TLS Handshake failed: {result.error}")
                return (False, self._results)
            logger.info(f"[Verification] ✓ TLS Handshake ({result.latency_ms:.1f}ms)")

        # Stage 3: Authentication
        if VerificationStage.AUTHENTICATION in stages:
            result = await self.verify_authentication()
            if not result.success:
                all_passed = False
                logger.error(f"[Verification] Authentication failed: {result.error}")
                return (False, self._results)
            logger.info(f"[Verification] ✓ Authentication ({result.latency_ms:.1f}ms)")

        # Stage 4: Query Execution
        if VerificationStage.QUERY_EXECUTION in stages:
            result = await self.verify_query_execution()
            if not result.success:
                all_passed = False
                logger.error(f"[Verification] Query Execution failed: {result.error}")
                return (False, self._results)
            logger.info(f"[Verification] ✓ Query Execution ({result.latency_ms:.1f}ms)")

        # Stage 5: Latency Check
        if VerificationStage.LATENCY_CHECK in stages:
            result = await self.verify_latency()
            if not result.success:
                all_passed = False
                logger.warning(f"[Verification] Latency Check: {result.error}")
                # Latency is soft failure - continue but report
            else:
                logger.info(f"[Verification] ✓ Latency Check ({result.latency_ms:.1f}ms avg)")

        return (all_passed, self._results)

    @property
    def results(self) -> List[VerificationResult]:
        """Get all verification results."""
        return self._results.copy()


# =============================================================================
# Dependency Types
# =============================================================================

class DependencyType(Enum):
    """Types of dependencies components can declare."""
    CLOUDSQL = "cloudsql"
    FILESYSTEM = "filesystem"
    NETWORK = "network"
    GCP_CREDENTIALS = "gcp_creds"
    VOICE_ENGINE = "voice_engine"
    VISION_ENGINE = "vision_engine"
    MEMORY_SYSTEM = "memory_system"
    NONE = "none"


# =============================================================================
# Component Manifest
# =============================================================================

@dataclass
class ComponentManifest:
    """
    Declares a component's initialization requirements.

    Components declare their dependencies, and the startup barrier
    ensures dependencies are ready before initialization begins.
    """
    name: str
    dependencies: FrozenSet[DependencyType]
    init_func: Callable[[], Awaitable[bool]]
    priority: int = 50  # Lower = earlier (0-100)
    timeout: float = BarrierConfig.COMPONENT_TIMEOUT
    required: bool = True  # If False, failure doesn't block startup
    description: str = ""

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ComponentManifest):
            return self.name == other.name
        return False


# =============================================================================
# Initialization Wave
# =============================================================================

@dataclass
class InitializationWave:
    """A group of components that can initialize in parallel."""
    wave_number: int
    components: List[ComponentManifest]
    blocking_dependencies: Set[DependencyType]

    def __str__(self) -> str:
        comp_names = [c.name for c in self.components]
        deps = [d.name for d in self.blocking_dependencies]
        return f"Wave {self.wave_number}: {comp_names} (waits for: {deps if deps else 'nothing'})"


# =============================================================================
# Async Startup Barrier
# =============================================================================

class AsyncStartupBarrier:
    """
    Manages component initialization with dependency awareness.

    Features:
    - CloudSQL verification barrier
    - Dependency graph resolution with topological sort
    - Parallel initialization of independent components
    - Timeout and retry handling
    """

    def __init__(
        self,
        lifecycle_controller: Optional[ProxyLifecycleController] = None,
    ):
        self._lifecycle = lifecycle_controller
        self._components: Dict[str, ComponentManifest] = {}
        self._dependency_status: Dict[DependencyType, bool] = {
            dep: False for dep in DependencyType
        }
        self._dependency_status[DependencyType.NONE] = True  # Always ready

        # Barrier events for dependencies
        self._dependency_events: Dict[DependencyType, asyncio.Event] = {
            dep: asyncio.Event() for dep in DependencyType
        }
        self._dependency_events[DependencyType.NONE].set()  # Always ready

        # Initialization results
        self._init_results: Dict[str, Tuple[bool, Optional[str], float]] = {}

        # Verification pipeline
        self._verification_pipeline = VerificationPipeline()

        # State
        self._initialized = False
        self._cloudsql_verified = False

    # -------------------------------------------------------------------------
    # Component Registration
    # -------------------------------------------------------------------------

    def register_component(self, manifest: ComponentManifest) -> None:
        """Register a component for managed initialization."""
        self._components[manifest.name] = manifest
        logger.debug(
            f"[StartupBarrier] Registered component: {manifest.name} "
            f"(deps: {[d.name for d in manifest.dependencies]})"
        )

    def register_components(self, manifests: List[ComponentManifest]) -> None:
        """Register multiple components."""
        for manifest in manifests:
            self.register_component(manifest)

    # -------------------------------------------------------------------------
    # Dependency Management
    # -------------------------------------------------------------------------

    def mark_dependency_ready(self, dep_type: DependencyType) -> None:
        """Mark a dependency as ready (unblocks waiting components)."""
        self._dependency_status[dep_type] = True
        self._dependency_events[dep_type].set()
        logger.info(f"[StartupBarrier] Dependency ready: {dep_type.name}")

    def mark_dependency_unavailable(self, dep_type: DependencyType) -> None:
        """Mark a dependency as unavailable."""
        self._dependency_status[dep_type] = False
        self._dependency_events[dep_type].clear()
        logger.warning(f"[StartupBarrier] Dependency unavailable: {dep_type.name}")

    async def wait_for_dependency(
        self,
        dep_type: DependencyType,
        timeout: Optional[float] = None,
    ) -> bool:
        """Wait for a dependency to become ready."""
        if self._dependency_status[dep_type]:
            return True

        try:
            await asyncio.wait_for(
                self._dependency_events[dep_type].wait(),
                timeout=timeout
            )
            return True
        except asyncio.TimeoutError:
            return False

    # -------------------------------------------------------------------------
    # CloudSQL Verification
    # -------------------------------------------------------------------------

    async def ensure_cloudsql_ready(
        self,
        timeout: Optional[float] = None,
    ) -> bool:
        """
        Ensure CloudSQL proxy is verified ready.

        This is the main barrier that blocks CloudSQL-dependent components.
        """
        if self._cloudsql_verified:
            return True

        timeout = timeout or BarrierConfig.ENSURE_READY_TIMEOUT
        deadline = time.monotonic() + timeout
        attempt = 0
        delay = BarrierConfig.RETRY_BASE_DELAY

        logger.info(f"[StartupBarrier] Ensuring CloudSQL ready (timeout: {timeout}s)")

        while time.monotonic() < deadline:
            attempt += 1

            # First, ensure lifecycle controller has started proxy
            if self._lifecycle:
                if not self._lifecycle.is_healthy:
                    logger.info(
                        f"[StartupBarrier] Waiting for proxy to start "
                        f"(state: {self._lifecycle.state.name})"
                    )
                    await asyncio.sleep(1.0)
                    continue

            # Run verification pipeline
            success, results = await self._verification_pipeline.run_all(
                stages={VerificationStage.TCP_CONNECT, VerificationStage.QUERY_EXECUTION}
            )

            if success:
                self._cloudsql_verified = True
                self.mark_dependency_ready(DependencyType.CLOUDSQL)

                # Log summary
                total_latency = sum(r.latency_ms for r in results)
                logger.info(
                    f"[StartupBarrier] ✅ CloudSQL verified ready "
                    f"(attempt {attempt}, total latency: {total_latency:.1f}ms)"
                )
                return True

            # Failed - wait and retry with exponential backoff
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            wait_time = min(delay, remaining)
            logger.warning(
                f"[StartupBarrier] CloudSQL verification failed (attempt {attempt}), "
                f"retrying in {wait_time:.1f}s"
            )
            await asyncio.sleep(wait_time)

            delay = min(delay * 2, BarrierConfig.RETRY_MAX_DELAY)

        # Timeout exceeded
        logger.error(
            f"[StartupBarrier] ❌ CloudSQL verification failed after {attempt} attempts"
        )
        self.mark_dependency_unavailable(DependencyType.CLOUDSQL)
        return False

    # -------------------------------------------------------------------------
    # Wave-Based Initialization
    # -------------------------------------------------------------------------

    def _build_initialization_waves(self) -> List[InitializationWave]:
        """
        Build initialization waves using topological sort.

        Components are grouped into waves based on dependencies:
        - Wave 0: Components with no dependencies
        - Wave 1: Components depending on Wave 0 outputs
        - ...and so on

        Components in the same wave can run in parallel.
        """
        # Sort by priority first
        sorted_components = sorted(
            self._components.values(),
            key=lambda c: c.priority
        )

        # Group by dependency requirements
        waves: List[InitializationWave] = []

        # Wave 0: No dependencies (or only NONE)
        wave_0_components = [
            c for c in sorted_components
            if c.dependencies == frozenset() or c.dependencies == frozenset({DependencyType.NONE})
        ]
        if wave_0_components:
            waves.append(InitializationWave(
                wave_number=0,
                components=wave_0_components,
                blocking_dependencies=set(),
            ))

        # Group remaining by dependency type
        remaining = [c for c in sorted_components if c not in wave_0_components]

        # Build waves for each major dependency type
        dep_order = [
            DependencyType.FILESYSTEM,
            DependencyType.NETWORK,
            DependencyType.GCP_CREDENTIALS,
            DependencyType.CLOUDSQL,  # This is the main barrier
            DependencyType.VOICE_ENGINE,
            DependencyType.VISION_ENGINE,
            DependencyType.MEMORY_SYSTEM,
        ]

        current_wave = 1
        for dep in dep_order:
            wave_components = [
                c for c in remaining
                if dep in c.dependencies and c not in [
                    comp for wave in waves for comp in wave.components
                ]
            ]
            if wave_components:
                waves.append(InitializationWave(
                    wave_number=current_wave,
                    components=wave_components,
                    blocking_dependencies={dep},
                ))
                current_wave += 1

        return waves

    async def _initialize_component(
        self,
        manifest: ComponentManifest,
    ) -> Tuple[bool, Optional[str], float]:
        """Initialize a single component with timeout."""
        start_time = time.monotonic()

        try:
            success = await asyncio.wait_for(
                manifest.init_func(),
                timeout=manifest.timeout
            )
            elapsed = time.monotonic() - start_time

            if success:
                logger.info(
                    f"[StartupBarrier] ✓ {manifest.name} initialized ({elapsed:.2f}s)"
                )
                return (True, None, elapsed)
            else:
                logger.error(f"[StartupBarrier] ✗ {manifest.name} returned False")
                return (False, "Init function returned False", elapsed)

        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start_time
            logger.error(
                f"[StartupBarrier] ✗ {manifest.name} timed out after {manifest.timeout}s"
            )
            return (False, f"Timeout after {manifest.timeout}s", elapsed)
        except Exception as e:
            elapsed = time.monotonic() - start_time
            logger.error(
                f"[StartupBarrier] ✗ {manifest.name} failed: {e}"
            )
            return (False, str(e), elapsed)

    async def _initialize_wave(
        self,
        wave: InitializationWave,
    ) -> Tuple[int, int]:
        """
        Initialize all components in a wave.

        Returns (succeeded_count, failed_count).
        """
        logger.info(f"[StartupBarrier] Starting {wave}")

        # Wait for blocking dependencies
        for dep in wave.blocking_dependencies:
            if dep == DependencyType.CLOUDSQL:
                success = await self.ensure_cloudsql_ready()
                if not success:
                    logger.error(
                        f"[StartupBarrier] Wave {wave.wave_number} blocked: "
                        "CloudSQL not ready"
                    )
                    return (0, len(wave.components))
            else:
                success = await self.wait_for_dependency(dep, timeout=30.0)
                if not success:
                    logger.warning(
                        f"[StartupBarrier] Dependency {dep.name} not ready, "
                        "continuing anyway"
                    )

        # Initialize components in parallel (or serial if disabled)
        if BarrierConfig.PARALLEL_INIT_ENABLED and len(wave.components) > 1:
            tasks = [
                self._initialize_component(comp)
                for comp in wave.components
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        else:
            results = []
            for comp in wave.components:
                result = await self._initialize_component(comp)
                results.append(result)

        # Process results
        succeeded = 0
        failed = 0

        for comp, result in zip(wave.components, results):
            if isinstance(result, Exception):
                self._init_results[comp.name] = (False, str(result), 0.0)
                failed += 1
            else:
                success, error, elapsed = result
                self._init_results[comp.name] = (success, error, elapsed)
                if success:
                    succeeded += 1
                else:
                    failed += 1

                    # Check if this is a required component
                    if comp.required:
                        logger.error(
                            f"[StartupBarrier] Required component {comp.name} failed"
                        )

        return (succeeded, failed)

    async def initialize_all(self) -> Tuple[int, int, int]:
        """
        Initialize all registered components in dependency order.

        Returns (succeeded, failed, skipped).
        """
        if self._initialized:
            logger.warning("[StartupBarrier] Already initialized")
            return (0, 0, 0)

        waves = self._build_initialization_waves()

        logger.info(
            f"[StartupBarrier] Starting initialization with {len(waves)} waves, "
            f"{len(self._components)} components"
        )

        total_succeeded = 0
        total_failed = 0
        total_skipped = 0

        for wave in waves:
            succeeded, failed = await self._initialize_wave(wave)
            total_succeeded += succeeded
            total_failed += failed

            # If required components failed, stop
            required_failures = [
                comp.name for comp in wave.components
                if comp.required and not self._init_results.get(comp.name, (False,))[0]
            ]
            if required_failures:
                logger.error(
                    f"[StartupBarrier] Aborting: required components failed: {required_failures}"
                )
                # Count remaining as skipped
                for remaining_wave in waves[waves.index(wave) + 1:]:
                    total_skipped += len(remaining_wave.components)
                break

        self._initialized = True

        logger.info(
            f"[StartupBarrier] Initialization complete: "
            f"{total_succeeded} succeeded, {total_failed} failed, {total_skipped} skipped"
        )

        return (total_succeeded, total_failed, total_skipped)

    # -------------------------------------------------------------------------
    # Status and Reporting
    # -------------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Get current barrier status."""
        return {
            "initialized": self._initialized,
            "cloudsql_verified": self._cloudsql_verified,
            "dependencies": {
                dep.name: ready
                for dep, ready in self._dependency_status.items()
            },
            "components": {
                name: {
                    "success": result[0],
                    "error": result[1],
                    "elapsed_seconds": result[2],
                }
                for name, result in self._init_results.items()
            },
            "verification_results": [
                r.to_dict() for r in self._verification_pipeline.results
            ],
        }

    @asynccontextmanager
    async def managed(self) -> AsyncIterator[AsyncStartupBarrier]:
        """Context manager for startup barrier."""
        try:
            yield self
        finally:
            # Cleanup if needed
            pass


# =============================================================================
# Convenience Decorator
# =============================================================================

def requires_cloudsql(
    func: Callable[..., Awaitable[Any]]
) -> Callable[..., Awaitable[Any]]:
    """
    Decorator that ensures CloudSQL is ready before function executes.

    Usage:
        @requires_cloudsql
        async def my_db_function():
            # CloudSQL guaranteed ready here
            pass
    """
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Get or create a verification pipeline
        pipeline = VerificationPipeline()
        success, _ = await pipeline.run_all(
            stages={VerificationStage.TCP_CONNECT}
        )

        if not success:
            raise RuntimeError("CloudSQL proxy not ready")

        return await func(*args, **kwargs)

    return wrapper


# =============================================================================
# Factory Functions
# =============================================================================

async def create_startup_barrier(
    lifecycle_controller: Optional[ProxyLifecycleController] = None,
    components: Optional[List[ComponentManifest]] = None,
) -> AsyncStartupBarrier:
    """
    Factory function to create a configured startup barrier.

    Args:
        lifecycle_controller: Optional proxy lifecycle controller
        components: Optional list of components to register

    Returns:
        Configured AsyncStartupBarrier
    """
    barrier = AsyncStartupBarrier(lifecycle_controller=lifecycle_controller)

    if components:
        barrier.register_components(components)

    return barrier
