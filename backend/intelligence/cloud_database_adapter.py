#!/usr/bin/env python3
"""
Cloud Database Adapter for JARVIS
Supports both local SQLite and GCP Cloud SQL (PostgreSQL)
Seamless switching between local and cloud databases
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

# Database drivers
import aiosqlite  # For local SQLite

try:
    import asyncpg  # For PostgreSQL/Cloud SQL

    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False
    logging.warning("asyncpg not available - install with: pip install asyncpg")

# Import singleton connection manager
try:
    from intelligence.cloud_sql_connection_manager import get_connection_manager
    CONNECTION_MANAGER_AVAILABLE = True
except ImportError:
    CONNECTION_MANAGER_AVAILABLE = False
    logging.warning("CloudSQL connection manager not available")

try:
    pass

    CLOUD_SQL_CONNECTOR_AVAILABLE = True
except ImportError:
    CLOUD_SQL_CONNECTOR_AVAILABLE = False
    logging.warning(
        "Cloud SQL Connector not available - install with: pip install cloud-sql-python-connector[asyncpg]"
    )

# Import centralized secret manager
try:
    from core.secret_manager import get_db_password
    SECRET_MANAGER_AVAILABLE = True
except ImportError:
    try:
        from backend.core.secret_manager import get_db_password
        SECRET_MANAGER_AVAILABLE = True
    except ImportError:
        SECRET_MANAGER_AVAILABLE = False

logger = logging.getLogger(__name__)


class DatabaseConfig:
    """
    Intelligent database configuration with dynamic backend detection.

    Features:
    - Auto-detects best available database backend
    - Silent fallback to SQLite when Cloud SQL isn't fully configured
    - Only warns when Cloud SQL was explicitly requested but unavailable
    - Caches detection results to avoid repeated checks
    """

    # Class-level cache for detection results
    _detection_cache = None
    _detection_timestamp = None
    _cache_ttl = 300  # 5 minutes

    def __init__(self):
        # Initialize all attributes with defaults
        self.db_type = "sqlite"
        self.connection_name = None
        self.db_host = "127.0.0.1"
        self.db_port = 5432
        self.db_name = "jarvis_learning"
        self.db_user = "jarvis"
        self.db_password = ""  # nosec - default empty password, overridden by config

        # Local SQLite config
        self.sqlite_path = Path.home() / ".jarvis" / "learning" / "jarvis_learning.db"

        # Track if Cloud SQL was explicitly requested
        self._explicit_cloudsql_request = False

        # Track configuration completeness
        self._config_file_exists = False
        self._has_connection_name = False
        self._has_password = False
        self._has_asyncpg = ASYNCPG_AVAILABLE

        # Load from config file if exists
        self._load_from_config()

        # Check for explicit Cloud SQL request via environment
        env_db_type = os.getenv("JARVIS_DB_TYPE", "").lower()
        if env_db_type == "cloudsql":
            self._explicit_cloudsql_request = True

        # Environment variables can override config file
        self.connection_name = os.getenv("JARVIS_DB_CONNECTION_NAME", self.connection_name)
        self.db_host = os.getenv("JARVIS_DB_HOST", self.db_host)
        self.db_port = int(os.getenv("JARVIS_DB_PORT", str(self.db_port)))
        self.db_name = os.getenv("JARVIS_DB_NAME", self.db_name)
        self.db_user = os.getenv("JARVIS_DB_USER", self.db_user)

        # Track connection name availability
        self._has_connection_name = bool(self.connection_name)

        # Get password with fallback chain: Secret Manager -> environment -> config file
        if SECRET_MANAGER_AVAILABLE:
            self.db_password = get_db_password() or self.db_password
        else:
            self.db_password = os.getenv("JARVIS_DB_PASSWORD", self.db_password)

        # Track password availability
        self._has_password = bool(self.db_password)

        # INTELLIGENT DB TYPE SELECTION
        # Only use Cloud SQL if ALL requirements are met
        self.db_type = self._determine_optimal_db_type(env_db_type)

        # CRITICAL: Always use localhost for Cloud SQL proxy connections
        # The proxy running locally handles the actual connection to Cloud SQL
        if self.db_type == "cloudsql" and self.connection_name:
            self.db_host = "127.0.0.1"

    def _determine_optimal_db_type(self, env_db_type: str) -> str:
        """
        Intelligently determine the optimal database type based on available resources.

        Priority:
        1. If Cloud SQL explicitly requested AND fully configured -> use cloudsql
        2. If Cloud SQL requirements not met -> fall back to sqlite (warn only if explicit request)
        3. If nothing specified, use sqlite (stable, always available)

        Returns:
            str: 'cloudsql' or 'sqlite'
        """
        import time

        # Check cache
        if (DatabaseConfig._detection_cache is not None and
            DatabaseConfig._detection_timestamp is not None and
            time.time() - DatabaseConfig._detection_timestamp < DatabaseConfig._cache_ttl):
            return DatabaseConfig._detection_cache

        # Check Cloud SQL requirements
        cloudsql_requirements = {
            'asyncpg_available': self._has_asyncpg,
            'connection_name': self._has_connection_name,
            'password': self._has_password,
            'config_file': self._config_file_exists,
        }

        all_requirements_met = all(cloudsql_requirements.values())

        # Determine final db_type
        if env_db_type == "cloudsql" or (not env_db_type and self._has_connection_name):
            if all_requirements_met:
                logger.info(f"☁️  Cloud SQL configuration complete - will use cloudsql")
                result = "cloudsql"
            else:
                # Only warn if explicitly requested
                if self._explicit_cloudsql_request:
                    missing = [k for k, v in cloudsql_requirements.items() if not v]
                    logger.warning(
                        f"⚠️  Cloud SQL explicitly requested but requirements not met: {missing}. "
                        f"Falling back to SQLite."
                    )
                else:
                    # Silent fallback - Cloud SQL config incomplete, just use SQLite
                    logger.debug(
                        f"Cloud SQL config incomplete (missing: "
                        f"{[k for k, v in cloudsql_requirements.items() if not v]}). "
                        f"Using SQLite."
                    )
                result = "sqlite"
        elif env_db_type == "sqlite":
            result = "sqlite"
        else:
            # Default: use SQLite (always available, stable)
            result = "sqlite"

        # Cache result
        DatabaseConfig._detection_cache = result
        DatabaseConfig._detection_timestamp = time.time()

        return result

    def _load_from_config(self):
        """Load config from JSON file"""
        config_path = Path.home() / ".jarvis" / "gcp" / "database_config.json"
        if config_path.exists():
            self._config_file_exists = True
            try:
                with open(config_path, "r") as f:
                    config = json.load(f)
                    cloud_sql = config.get("cloud_sql", {})
                    self.connection_name = cloud_sql.get("connection_name", self.connection_name)
                    self.db_host = cloud_sql.get("host", self.db_host)
                    self.db_port = cloud_sql.get("port", self.db_port)
                    self.db_name = cloud_sql.get("database", self.db_name)
                    self.db_user = cloud_sql.get("user", self.db_user)

                    self.db_password = cloud_sql.get("password", self.db_password)

                    logger.debug(f"Loaded database config from {config_path}")
            except Exception as e:
                logger.warning(f"Failed to load config from {config_path}: {e}")
                self._config_file_exists = False
        else:
            self._config_file_exists = False

    @property
    def use_cloud_sql(self) -> bool:
        """Check if we should use Cloud SQL"""
        return (
            self.db_type == "cloudsql"
            and ASYNCPG_AVAILABLE
            and self.db_password
            and self.connection_name
        )


class CloudDatabaseAdapter:
    """
    Adapter that provides unified interface for both SQLite and Cloud SQL
    Automatically chooses backend based on configuration
    """

    def __init__(self, config: Optional[DatabaseConfig] = None):
        self.config = config or DatabaseConfig()
        self.pool: Optional[Any] = None  # Deprecated: kept for backward compatibility
        self.connector: Optional[Any] = None
        self._local_connection: Optional[aiosqlite.Connection] = None

        # Use singleton connection manager for CloudSQL
        self.connection_manager = get_connection_manager() if CONNECTION_MANAGER_AVAILABLE else None

        logger.info(f"🔧 DatabaseAdapter initialized (type: {self.config.db_type})")

    async def initialize(self):
        """Initialize database connection pool"""
        if self.config.use_cloud_sql:
            await self._init_cloud_sql()
        else:
            await self._init_sqlite()

    async def _ensure_proxy_running(self) -> bool:
        """
        Ensure Cloud SQL proxy is running, start it if not.

        Features:
        - Intelligent detection with caching
        - Only warns when Cloud SQL was explicitly requested
        - Auto-start with exponential backoff
        - Silent fallback when proxy unavailable

        Returns:
            bool: True if proxy is running, False otherwise
        """
        import asyncio
        import socket
        import time

        # Check if proxy port is already listening
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex((self.config.db_host, self.config.db_port))
            sock.close()

            if result == 0:
                logger.debug(f"Cloud SQL proxy detected on port {self.config.db_port}")
                return True
        except Exception as e:
            logger.debug(f"Port check failed: {e}")

        # Proxy not running - check if we should even try to start it
        # Only log warning if Cloud SQL was explicitly requested
        if self.config._explicit_cloudsql_request:
            logger.warning(f"⚠️  Cloud SQL proxy not detected on port {self.config.db_port}")
            logger.info(f"🚀 Attempting to start Cloud SQL proxy...")
        else:
            logger.debug(f"Cloud SQL proxy not running on port {self.config.db_port}")

        try:
            from intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager

            proxy_manager = CloudSQLProxyManager()
            started = await proxy_manager.start(force_restart=False)

            if started:
                logger.info(f"✅ Cloud SQL proxy started successfully")
                # Wait a moment for proxy to be fully ready
                await asyncio.sleep(2)
                return True
            else:
                if self.config._explicit_cloudsql_request:
                    logger.error(f"❌ Failed to start Cloud SQL proxy")
                else:
                    logger.debug(f"Cloud SQL proxy not available - will use SQLite")
                return False

        except FileNotFoundError as e:
            # Config file or proxy binary not found - this is expected if not configured
            if self.config._explicit_cloudsql_request:
                logger.error(f"❌ Cloud SQL proxy not configured: {e}")
            else:
                logger.debug(f"Cloud SQL proxy not configured: {e}")
            return False

        except Exception as e:
            if self.config._explicit_cloudsql_request:
                logger.error(f"❌ Error starting Cloud SQL proxy: {e}")
            else:
                logger.debug(f"Cloud SQL proxy error: {e}")
            return False

    async def _init_sqlite(self):
        """Initialize local SQLite connection"""
        self.config.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"📂 Using local SQLite: {self.config.sqlite_path}")

    async def _init_cloud_sql(self):
        """
        Initialize Cloud SQL connection via singleton connection manager.

        v5.5: Enhanced with intelligent credential validation and recovery.

        Features:
        - Pre-validates credentials before connection attempt
        - Intelligent proxy detection with auto-start
        - Specific error detection for password/authentication issues
        - Clear guidance on credential refresh
        - Silent fallback to SQLite when Cloud SQL unavailable
        """
        import asyncio

        if not CONNECTION_MANAGER_AVAILABLE or not self.connection_manager:
            if self.config._explicit_cloudsql_request:
                logger.error("❌ CloudSQL connection manager not available")
            else:
                logger.debug("CloudSQL connection manager not available - using SQLite")
            await self._init_sqlite()
            return

        try:
            logger.debug(f"Attempting Cloud SQL connection: {self.config.connection_name}")

            # ROBUSTNESS: Ensure Cloud SQL proxy is running before attempting connection
            proxy_running = await self._ensure_proxy_running()

            if not proxy_running:
                if self.config._explicit_cloudsql_request:
                    logger.warning("⚠️  Cloud SQL proxy not available, falling back to SQLite")
                else:
                    logger.debug("Cloud SQL proxy not available - using SQLite")
                await self._init_sqlite()
                return

            logger.info(f"☁️  Connecting to Cloud SQL: {self.config.connection_name}")
            logger.debug(
                f"   Connecting via proxy at {self.config.db_host}:{self.config.db_port}"
            )
            logger.debug(f"   Database: {self.config.db_name}, User: {self.config.db_user}")

            # Use singleton connection manager (reuses existing pool if available)
            success = await self.connection_manager.initialize(
                host=self.config.db_host,
                port=self.config.db_port,
                database=self.config.db_name,
                user=self.config.db_user,
                password=self.config.db_password,
                max_connections=3,  # Strict limit for db-f1-micro
                force_reinit=False  # Reuse existing pool
            )

            if success:
                # Set pool reference for backward compatibility
                self.pool = self.connection_manager.pool
                logger.info("✅ Cloud SQL connection initialized")
            else:
                # v5.5: Enhanced error detection for credential issues
                await self._handle_connection_failure()

        except Exception as e:
            # v5.5: Detect specific error types
            error_str = str(e).lower()

            if "password authentication failed" in error_str:
                await self._handle_auth_failure()
            elif "connection refused" in error_str:
                if self.config._explicit_cloudsql_request:
                    logger.error("❌ Connection refused - Cloud SQL proxy may not be running")
                await self._init_sqlite()
            elif "timeout" in error_str:
                if self.config._explicit_cloudsql_request:
                    logger.error("❌ Connection timeout - Cloud SQL may be overloaded or unreachable")
                await self._init_sqlite()
            else:
                if self.config._explicit_cloudsql_request:
                    logger.error(f"❌ Cloud SQL initialization failed: {e}")
                    logger.error(f"   Connection: host={self.config.db_host}, port={self.config.db_port}, db={self.config.db_name}")
                else:
                    logger.debug(f"Cloud SQL initialization failed: {e} - using SQLite")
                await self._init_sqlite()

            self.pool = None

    async def _handle_auth_failure(self):
        """
        Handle authentication failure with intelligent diagnostics.

        v5.5: Provides clear guidance on credential issues.
        """
        logger.error("❌ Password authentication failed for Cloud SQL")
        logger.error("   The password in GCP Secret Manager does not match Cloud SQL.")
        logger.error("")
        logger.error("   To fix this issue:")
        logger.error("   1. Get the current Cloud SQL password from your admin")
        logger.error("   2. Update GCP Secret Manager:")
        logger.error(f"      gcloud secrets versions add jarvis-db-password --data-file=-")
        logger.error("   3. Or update Cloud SQL user password:")
        logger.error(f"      gcloud sql users set-password {self.config.db_user} \\")
        logger.error(f"        --instance=jarvis-learning-db --password=NEW_PASSWORD")
        logger.error("")
        logger.warning("⚠️  Falling back to local SQLite database")

        await self._init_sqlite()

    async def _handle_connection_failure(self):
        """
        Handle generic connection failure with diagnostics.

        v5.5: Checks for common issues and provides guidance.
        """
        # Try to get more specific error info from the connection manager
        if self.connection_manager and hasattr(self.connection_manager, 'last_error'):
            last_error = getattr(self.connection_manager, 'last_error', None)
            if last_error and "password authentication" in str(last_error).lower():
                await self._handle_auth_failure()
                return

        if self.config._explicit_cloudsql_request:
            logger.warning("⚠️  Cloud SQL connection failed, falling back to SQLite")
            logger.warning("   Check: 1) Cloud SQL proxy running 2) Credentials valid 3) DB exists")
        else:
            logger.debug("Cloud SQL connection failed - using SQLite")

        self.pool = None
        await self._init_sqlite()

    @asynccontextmanager
    async def connection(self):
        """
        Get database connection (context manager).

        v82.0: Intelligent Graceful Degradation
        ======================================
        When Cloud SQL is unavailable (circuit breaker open, connection refused,
        timeout, etc.), automatically falls back to local SQLite to prevent
        cascade failures. This ensures the system remains functional even when
        Cloud SQL is temporarily unavailable.

        Fallback is transparent to callers - they get a working connection
        regardless of which backend is used.

        Priority order:
        1. Cloud SQL via connection manager (preferred)
        2. Cloud SQL via direct pool (backward compat)
        3. Local SQLite (fallback)
        """
        # Track whether we're using fallback for logging
        using_fallback = False

        # Try Cloud SQL first
        if self.connection_manager and self.connection_manager.is_initialized:
            try:
                # Cloud SQL (PostgreSQL) via singleton connection manager
                async with self.connection_manager.connection() as conn:
                    yield CloudSQLConnection(conn)
                    return  # Success - exit without fallback
            except asyncio.CancelledError:
                # v82.1: Task cancellation during connection release - graceful handling
                # This can happen during shutdown when asyncio.shield() is interrupted
                logger.debug("[v82.1] Cloud SQL operation cancelled (likely shutdown), falling back to SQLite")
                using_fallback = True
            except RuntimeError as e:
                # v82.0: Circuit breaker OPEN or connection pool exhausted
                if "Circuit breaker OPEN" in str(e) or "shutting down" in str(e):
                    logger.debug(f"[v82.0] Cloud SQL unavailable ({e}), falling back to SQLite")
                    using_fallback = True
                else:
                    raise  # Re-raise unexpected RuntimeErrors
            except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
                # v82.0: Connection-level errors - graceful fallback
                logger.debug(f"[v82.0] Cloud SQL connection failed ({type(e).__name__}), falling back to SQLite")
                using_fallback = True
            except asyncio.InvalidStateError as e:
                # v15.0: TLS protocol state error - connection was corrupted
                # This happens when asyncpg's TLS upgrade receives data after connection finalized
                logger.debug(f"[v15.0] TLS protocol state error ({e}), falling back to SQLite")
                using_fallback = True
            except Exception as e:
                # v82.0: Any other database error - try fallback
                error_type = type(e).__name__
                error_str = str(e).lower()
                # v15.0: Added "invalid state", "tls", "protocol" to fallback triggers
                fallback_triggers = ["connection", "timeout", "refused", "pool", "cancel",
                                     "invalid state", "tls", "protocol", "transport"]
                if any(x in error_str for x in fallback_triggers):
                    logger.debug(f"[v15.0] Cloud SQL error ({error_type}: {e}), falling back to SQLite")
                    using_fallback = True
                else:
                    raise  # Re-raise non-connection errors

        # Try direct pool (backward compat) if manager not available
        if not using_fallback and self.pool:
            try:
                async with self.pool.acquire() as conn:
                    yield CloudSQLConnection(conn)
                    return
            except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
                logger.debug(f"[v82.0] Direct pool connection failed, falling back to SQLite")
                using_fallback = True

        # Fallback to SQLite (always available)
        if using_fallback:
            logger.info("[v82.0] 📂 Using SQLite fallback (Cloud SQL temporarily unavailable)")

        async with aiosqlite.connect(self.config.sqlite_path) as conn:
            yield SQLiteConnection(conn)

    async def close(self):
        """Close database connections"""
        # Singleton connection manager handles its own shutdown via signal handlers
        # No need to manually close it here

        if self._local_connection:
            await self._local_connection.close()
            logger.info("✅ SQLite connection closed")

    @property
    def is_cloud(self) -> bool:
        """Check if using cloud database"""
        if self.connection_manager:
            return self.connection_manager.is_initialized
        return self.pool is not None


class SQLiteConnection:
    """Wrapper for SQLite connection with unified interface"""

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        self.conn.row_factory = aiosqlite.Row

    async def execute(self, query: str, *args):
        """Execute query"""
        return await self.conn.execute(query, args)

    async def fetch(self, query: str, *args):
        """Fetch all results"""
        async with self.conn.execute(query, args) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def fetchone(self, query: str, *args):
        """Fetch one result"""
        async with self.conn.execute(query, args) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def fetchval(self, query: str, *args):
        """Fetch single value"""
        async with self.conn.execute(query, args) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def commit(self):
        """Commit transaction"""
        await self.conn.commit()

    async def upsert(
        self, table: str, unique_cols: List[str], data: Dict[str, Any]
    ) -> None:
        """
        Database-agnostic UPSERT (INSERT OR REPLACE for SQLite)

        Args:
            table: Table name
            unique_cols: List of columns that form the unique constraint
            data: Dictionary of column_name: value to insert/update
        """
        cols = list(data.keys())
        placeholders = ",".join(["?" for _ in cols])
        col_names = ",".join(cols)
        values = tuple(data.values())

        query = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"
        await self.execute(query, *values)


class CloudSQLConnection:
    """
    v18.0: Wrapper for Cloud SQL (PostgreSQL) connection with unified interface.

    Features:
    - Query-level timeout protection to prevent hanging queries
    - Automatic placeholder conversion (? -> $1, $2, etc)
    - Graceful timeout handling with clear error messages
    """

    # v18.0: Default query timeout (configurable via env)
    DEFAULT_QUERY_TIMEOUT = float(os.getenv("CLOUDSQL_QUERY_TIMEOUT_SECONDS", "30.0"))

    def __init__(self, conn, query_timeout: float = None):
        self.conn = conn
        self.query_timeout = query_timeout or self.DEFAULT_QUERY_TIMEOUT

    async def execute(self, query: str, *args, timeout: float = None):
        """Execute query with timeout protection (convert ? to $1, $2, etc for PostgreSQL)"""
        pg_query = self._convert_placeholders(query)
        effective_timeout = timeout or self.query_timeout
        try:
            return await asyncio.wait_for(
                self.conn.execute(pg_query, *args),
                timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"[v18.0] Query timeout ({effective_timeout}s): {pg_query[:100]}...")
            raise

    async def fetch(self, query: str, *args, timeout: float = None):
        """Fetch all results with timeout protection"""
        pg_query = self._convert_placeholders(query)
        effective_timeout = timeout or self.query_timeout
        try:
            rows = await asyncio.wait_for(
                self.conn.fetch(pg_query, *args),
                timeout=effective_timeout
            )
            return [dict(row) for row in rows]
        except asyncio.TimeoutError:
            logger.warning(f"[v18.0] Query timeout ({effective_timeout}s): {pg_query[:100]}...")
            raise

    async def fetchone(self, query: str, *args, timeout: float = None):
        """Fetch one result with timeout protection"""
        pg_query = self._convert_placeholders(query)
        effective_timeout = timeout or self.query_timeout
        try:
            row = await asyncio.wait_for(
                self.conn.fetchrow(pg_query, *args),
                timeout=effective_timeout
            )
            return dict(row) if row else None
        except asyncio.TimeoutError:
            logger.warning(f"[v18.0] Query timeout ({effective_timeout}s): {pg_query[:100]}...")
            raise

    async def fetchval(self, query: str, *args, timeout: float = None):
        """Fetch single value with timeout protection"""
        pg_query = self._convert_placeholders(query)
        effective_timeout = timeout or self.query_timeout
        try:
            return await asyncio.wait_for(
                self.conn.fetchval(pg_query, *args),
                timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"[v18.0] Query timeout ({effective_timeout}s): {pg_query[:100]}...")
            raise

    async def commit(self):
        """No-op for PostgreSQL (auto-commit)"""

    async def upsert(
        self, table: str, unique_cols: List[str], data: Dict[str, Any]
    ) -> None:
        """
        Database-agnostic UPSERT (INSERT...ON CONFLICT for PostgreSQL)

        Args:
            table: Table name
            unique_cols: List of columns that form the unique constraint
            data: Dictionary of column_name: value to insert/update
        """
        cols = list(data.keys())
        placeholders = ",".join([f"${i+1}" for i in range(len(cols))])
        col_names = ",".join(cols)
        values = tuple(data.values())

        # PostgreSQL ON CONFLICT syntax
        conflict_target = ",".join(unique_cols)
        update_cols = [col for col in cols if col not in unique_cols]
        update_set = ",".join([f"{col} = EXCLUDED.{col}" for col in update_cols])

        if update_set:
            query = f"""
                INSERT INTO {table} ({col_names})
                VALUES ({placeholders})
                ON CONFLICT ({conflict_target})
                DO UPDATE SET {update_set}
            """
        else:
            # No non-unique columns to update, just ignore conflicts
            query = f"""
                INSERT INTO {table} ({col_names})
                VALUES ({placeholders})
                ON CONFLICT ({conflict_target}) DO NOTHING
            """

        await self.conn.execute(query, *values)

    def _convert_placeholders(self, query: str) -> str:
        """Convert SQLite ? placeholders to PostgreSQL $1, $2, etc"""
        result = []
        param_num = 1
        i = 0
        while i < len(query):
            if query[i] == "?":
                result.append(f"${param_num}")
                param_num += 1
            else:
                result.append(query[i])
            i += 1
        return "".join(result)


# Global adapter instance
_adapter: Optional[CloudDatabaseAdapter] = None


async def get_database_adapter() -> CloudDatabaseAdapter:
    """Get or create global database adapter"""
    global _adapter
    if _adapter is None:
        _adapter = CloudDatabaseAdapter()
        await _adapter.initialize()
    return _adapter


async def close_database_adapter():
    """Close global database adapter"""
    global _adapter
    if _adapter:
        await _adapter.close()
        _adapter = None
