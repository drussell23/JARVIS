#!/usr/bin/env python3
"""
JARVIS Lifecycle Manager
========================

Centralized lifecycle management for JARVIS initialization and shutdown.
Handles:
- Singleton CloudSQL connection manager initialization
- Signal handlers for graceful shutdown
- Resource cleanup on exit
- Connection leak prevention
- Process cleanup coordination

Author: JARVIS System
Version: 1.0.0
"""

import asyncio
import atexit
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class LifecycleManager:
    """
    Centralized lifecycle management for JARVIS.

    Features:
    - Initialize all database connections (singleton pattern)
    - Register comprehensive shutdown handlers
    - Clean up resources on exit (connections, processes, VMs)
    - Handle SIGINT, SIGTERM, KeyboardInterrupt gracefully
    """

    _instance: Optional['LifecycleManager'] = None
    _initialized = False

    def __new__(cls):
        """Singleton pattern"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize lifecycle manager (runs once)"""
        if self._initialized:
            return

        self.connection_manager = None
        self.db_adapter = None
        self.hybrid_sync = None
        self.shutdown_initiated = False

        LifecycleManager._initialized = True
        logger.info("🔧 Lifecycle Manager initialized")

    async def initialize_database_connections(self):
        """
        Initialize all database connections using singleton pattern.
        This ensures only ONE connection pool exists across the entire application.
        """
        logger.info("🔌 Initializing database connections...")

        try:
            # Step 1: Get singleton connection manager
            from intelligence.cloud_sql_connection_manager import get_connection_manager

            self.connection_manager = get_connection_manager()
            logger.info("✅ Singleton CloudSQL connection manager loaded")

            # Step 2: Initialize CloudSQL connection pool (if configured)
            from intelligence.cloud_database_adapter import get_database_adapter

            self.db_adapter = await get_database_adapter()

            if self.db_adapter.is_cloud:
                logger.info("✅ CloudSQL connection pool initialized")
                stats = self.connection_manager.get_stats()
                logger.info(f"   Pool size: {stats['pool_size']}, Idle: {stats['idle_size']}, Max: {stats['max_size']}")
            else:
                logger.info("✅ Using local SQLite database")

            return True

        except Exception as e:
            logger.error(f"❌ Failed to initialize database connections: {e}")
            logger.info("📂 Falling back to SQLite-only mode")
            return False

    async def initialize_hybrid_sync(self, sqlite_path: Path, cloudsql_config: dict):
        """
        Initialize hybrid database sync with singleton connection manager.
        """
        logger.info("🔄 Initializing hybrid database sync...")

        try:
            from intelligence.hybrid_database_sync import HybridDatabaseSync

            # Determine Redis configuration from environment
            import os
            redis_host = os.getenv("REDIS_HOST")
            redis_port = os.getenv("REDIS_PORT", "6379")
            redis_enabled = bool(redis_host)
            redis_url = f"redis://{redis_host}:{redis_port}" if redis_host else "redis://localhost:6379"

            if redis_enabled:
                logger.info(f"✅ Redis configuration detected: {redis_host}:{redis_port}")

            self.hybrid_sync = HybridDatabaseSync.get_instance(
                sqlite_path=sqlite_path,
                cloudsql_config=cloudsql_config,
                max_connections=3,  # Strict limit for db-f1-micro
                enable_faiss_cache=True,
                enable_prometheus=True,
                enable_redis=redis_enabled,
                redis_url=redis_url,
            )

            await self.hybrid_sync.initialize()
            logger.info("✅ Hybrid database sync initialized")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to initialize hybrid sync: {e}")
            return False

    def register_shutdown_handlers(self):
        """
        Register comprehensive shutdown handlers for graceful cleanup.
        Ensures connections are released even on crashes.
        """
        logger.info("🛡️  Registering shutdown handlers...")

        # Register atexit handler
        atexit.register(self._sync_shutdown)

        # Register signal handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._signal_handler)

        logger.info("✅ Shutdown handlers registered (SIGINT, SIGTERM, atexit)")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        if self.shutdown_initiated:
            logger.warning("⚠️  Shutdown already in progress")
            return

        sig_name = signal.Signals(signum).name
        logger.info(f"📡 Received {sig_name} - initiating graceful shutdown...")
        self.shutdown_initiated = True

        # Run async shutdown
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self.shutdown())
            else:
                asyncio.run(self.shutdown())
        except Exception as e:
            logger.error(f"❌ Error during signal shutdown: {e}")

        # Re-raise KeyboardInterrupt for SIGINT
        if signum == signal.SIGINT:
            raise KeyboardInterrupt

    def _sync_shutdown(self):
        """Synchronous shutdown for atexit"""
        if not self.shutdown_initiated:
            logger.info("🛑 atexit: Running shutdown...")
            self.shutdown_initiated = True

            try:
                asyncio.run(self.shutdown())
            except Exception as e:
                logger.error(f"❌ Error during atexit shutdown: {e}")

    async def shutdown(self):
        """
        Comprehensive graceful shutdown.
        Cleans up all resources in the correct order.
        """
        if not self.shutdown_initiated:
            self.shutdown_initiated = True

        logger.info("🛑 Starting JARVIS graceful shutdown...")

        try:
            # Step 1: Shutdown hybrid sync (flushes pending writes)
            if self.hybrid_sync:
                logger.info("🔄 Shutting down hybrid database sync...")
                await self.hybrid_sync.shutdown()
                logger.info("✅ Hybrid sync shutdown complete")

            # Step 2: Close database adapter
            if self.db_adapter:
                logger.info("🔌 Closing database adapter...")
                await self.db_adapter.close()
                logger.info("✅ Database adapter closed")

            # Step 3: Shutdown singleton connection manager
            # Note: The connection manager has its own signal handlers,
            # but we trigger it explicitly here for completeness
            if self.connection_manager and self.connection_manager.is_initialized:
                logger.info("🔌 Shutting down CloudSQL connection manager...")
                await self.connection_manager.shutdown()
                logger.info("✅ Connection manager shutdown complete")

            # Step 4: Clean up process resources
            try:
                from process_cleanup_manager import ProcessCleanupManager

                cleanup_manager = ProcessCleanupManager()

                # Clean up CloudSQL connections
                cloudsql_cleanup = cleanup_manager._cleanup_cloudsql_connections()
                if cloudsql_cleanup["processes_terminated"] > 0:
                    logger.info(f"🧹 Cleaned up {cloudsql_cleanup['processes_terminated']} processes with DB connections")

                # Clean up IPC resources
                ipc_cleanup = cleanup_manager._cleanup_ipc_resources()
                if sum(ipc_cleanup.values()) > 0:
                    logger.info(f"🧹 Cleaned up {sum(ipc_cleanup.values())} IPC resources")

            except Exception as e:
                logger.warning(f"⚠️  Process cleanup failed: {e}")

            logger.info("✅ JARVIS graceful shutdown complete")

        except Exception as e:
            logger.error(f"❌ Error during shutdown: {e}")
            import traceback
            traceback.print_exc()

    def get_connection_stats(self) -> dict:
        """Get current connection pool statistics"""
        if self.connection_manager:
            return self.connection_manager.get_stats()
        return {"status": "not_initialized"}

    def is_database_healthy(self) -> bool:
        """Check if database connections are healthy"""
        if not self.connection_manager:
            return False
        return self.connection_manager.is_initialized


# Global singleton instance
_lifecycle_manager: Optional[LifecycleManager] = None


def get_lifecycle_manager() -> LifecycleManager:
    """Get singleton lifecycle manager instance"""
    global _lifecycle_manager
    if _lifecycle_manager is None:
        _lifecycle_manager = LifecycleManager()
    return _lifecycle_manager


async def initialize_jarvis_lifecycle():
    """
    Initialize JARVIS lifecycle management.
    Call this at the start of main() before any other initialization.
    """
    manager = get_lifecycle_manager()

    # Register shutdown handlers FIRST
    manager.register_shutdown_handlers()

    # Initialize database connections
    await manager.initialize_database_connections()

    return manager


async def shutdown_jarvis_lifecycle():
    """
    Shutdown JARVIS lifecycle management.
    Call this at the end of main() or in exception handlers.
    """
    manager = get_lifecycle_manager()
    await manager.shutdown()
