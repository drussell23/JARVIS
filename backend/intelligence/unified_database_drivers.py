#!/usr/bin/env python3
"""
Unified Database Driver Manager v1.0
=====================================

Intelligent, robust PostgreSQL driver management with:
- Lazy loading of database drivers (psycopg2, asyncpg)
- Auto-detection and graceful fallback on missing dependencies
- On-demand installation with pip subprocess
- Unified interface for both sync and async operations
- Connection validation and health checks
- Dynamic driver selection based on context

Architecture:
┌─────────────────────────────────────────────────────────────────┐
│              Unified Database Driver Manager                     │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────────┐    ┌──────────────────┐                   │
│  │   Sync Driver    │    │   Async Driver   │                   │
│  │   (psycopg2)     │    │   (asyncpg)      │                   │
│  │   • Health checks│    │   • Connection   │                   │
│  │   • Admin tasks  │    │     pooling      │                   │
│  │   • Voice verify │    │   • Production   │                   │
│  └────────┬─────────┘    └────────┬─────────┘                   │
│           └───────────┬───────────┘                             │
│                       ▼                                          │
│           ┌───────────────────────┐                             │
│           │   Driver Coordinator  │                             │
│           │   • Lazy loading      │                             │
│           │   • Auto-install      │                             │
│           │   • Fallback logic    │                             │
│           │   • Health validation │                             │
│           └───────────────────────┘                             │
└─────────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, Union

logger = logging.getLogger(__name__)


# =============================================================================
# Driver Status Tracking
# =============================================================================

class DriverStatus(Enum):
    """Status of a database driver."""
    UNKNOWN = auto()
    NOT_INSTALLED = auto()
    INSTALLING = auto()
    INSTALLED = auto()
    IMPORT_ERROR = auto()
    READY = auto()
    FAILED = auto()


@dataclass
class DriverInfo:
    """Information about a database driver."""
    name: str
    package_name: str  # pip package name
    import_name: str   # Python import name
    status: DriverStatus = DriverStatus.UNKNOWN
    version: Optional[str] = None
    error_message: Optional[str] = None
    last_check: Optional[datetime] = None
    install_attempts: int = 0
    max_install_attempts: int = 3


# =============================================================================
# Global Driver Registry
# =============================================================================

_driver_lock = threading.Lock()
_drivers: Dict[str, DriverInfo] = {
    'psycopg2': DriverInfo(
        name='psycopg2',
        package_name='psycopg2-binary',
        import_name='psycopg2',
    ),
    'asyncpg': DriverInfo(
        name='asyncpg',
        package_name='asyncpg',
        import_name='asyncpg',
    ),
    'cloud_sql_connector': DriverInfo(
        name='cloud_sql_connector',
        package_name='cloud-sql-python-connector',
        import_name='google.cloud.sql.connector',
    ),
}


# =============================================================================
# Lazy Loading with Auto-Installation
# =============================================================================

def _get_venv_pip() -> str:
    """Get path to pip in current virtual environment."""
    venv_path = os.environ.get('VIRTUAL_ENV')
    if venv_path:
        pip_path = Path(venv_path) / 'bin' / 'pip'
        if pip_path.exists():
            return str(pip_path)

    # Fallback to sys.executable
    return f"{sys.executable} -m pip"


def _install_package(package_name: str, timeout: int = 120) -> Tuple[bool, str]:
    """
    Install a package using pip.

    Args:
        package_name: Package name to install
        timeout: Installation timeout in seconds

    Returns:
        Tuple of (success: bool, message: str)
    """
    pip_cmd = _get_venv_pip()

    try:
        logger.info(f"📦 Installing {package_name}...")

        # Build command
        if 'pip' in pip_cmd and '-m' in pip_cmd:
            cmd = pip_cmd.split() + ['install', package_name]
        else:
            cmd = [pip_cmd, 'install', package_name]

        # Run installation
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, 'PIP_QUIET': '1'}
        )

        if result.returncode == 0:
            logger.info(f"✅ Successfully installed {package_name}")
            return True, f"Installed {package_name}"
        else:
            error_msg = result.stderr or result.stdout or "Unknown error"
            logger.error(f"❌ Failed to install {package_name}: {error_msg}")
            return False, error_msg

    except subprocess.TimeoutExpired:
        logger.error(f"⏱️ Installation timeout for {package_name}")
        return False, "Installation timeout"
    except Exception as e:
        logger.error(f"❌ Installation error for {package_name}: {e}")
        return False, str(e)


def _try_import_driver(driver_name: str) -> Tuple[bool, Optional[Any], Optional[str]]:
    """
    Try to import a driver module.

    Args:
        driver_name: Name of the driver to import

    Returns:
        Tuple of (success: bool, module: Any, version: str)
    """
    driver_info = _drivers.get(driver_name)
    if not driver_info:
        return False, None, None

    try:
        module = __import__(driver_info.import_name)

        # Get version
        version = getattr(module, '__version__', None)
        if not version:
            version = getattr(module, 'version', None)
        if not version:
            try:
                import importlib.metadata
                version = importlib.metadata.version(driver_info.package_name)
            except Exception:
                version = "unknown"

        return True, module, str(version)

    except ImportError as e:
        return False, None, str(e)


class DatabaseDriverManager:
    """
    Unified manager for PostgreSQL database drivers.

    Provides:
    - Lazy loading of drivers on first use
    - Auto-installation of missing dependencies
    - Unified interface for sync and async operations
    - Health validation and status tracking
    - Thread-safe operations
    """

    _instance: Optional['DatabaseDriverManager'] = None
    _class_lock = threading.Lock()

    def __new__(cls):
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return

        self._lock = threading.Lock()
        self._async_lock: Optional[asyncio.Lock] = None

        # Cached driver modules
        self._psycopg2: Optional[Any] = None
        self._asyncpg: Optional[Any] = None
        self._cloud_connector: Optional[Any] = None

        # Status flags
        self._checked_at_startup = False
        self._auto_install_enabled = True

        self._initialized = True
        logger.debug("🔧 DatabaseDriverManager initialized")

    def _get_async_lock(self) -> asyncio.Lock:
        """Get or create async lock."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    # =========================================================================
    # Driver Status Checking
    # =========================================================================

    def check_driver(self, driver_name: str, auto_install: bool = True) -> DriverStatus:
        """
        Check status of a driver, optionally installing if missing.

        Args:
            driver_name: Name of driver to check
            auto_install: Whether to auto-install if missing

        Returns:
            Current status of the driver
        """
        with self._lock:
            driver_info = _drivers.get(driver_name)
            if not driver_info:
                return DriverStatus.UNKNOWN

            # Try importing
            success, module, version = _try_import_driver(driver_name)

            if success:
                driver_info.status = DriverStatus.READY
                driver_info.version = version
                driver_info.last_check = datetime.now()
                driver_info.error_message = None

                # Cache the module
                if driver_name == 'psycopg2':
                    self._psycopg2 = module
                elif driver_name == 'asyncpg':
                    self._asyncpg = module
                elif driver_name == 'cloud_sql_connector':
                    self._cloud_connector = module

                return DriverStatus.READY

            # Not installed - try to install if enabled
            driver_info.status = DriverStatus.NOT_INSTALLED
            driver_info.error_message = version  # Contains error message

            if auto_install and self._auto_install_enabled:
                if driver_info.install_attempts < driver_info.max_install_attempts:
                    driver_info.status = DriverStatus.INSTALLING
                    driver_info.install_attempts += 1

                    installed, msg = _install_package(driver_info.package_name)

                    if installed:
                        # Retry import after installation
                        success, module, ver = _try_import_driver(driver_name)
                        if success:
                            driver_info.status = DriverStatus.READY
                            driver_info.version = ver
                            driver_info.error_message = None

                            if driver_name == 'psycopg2':
                                self._psycopg2 = module
                            elif driver_name == 'asyncpg':
                                self._asyncpg = module

                            return DriverStatus.READY

                    driver_info.status = DriverStatus.FAILED
                    driver_info.error_message = msg

            return driver_info.status

    async def check_driver_async(self, driver_name: str, auto_install: bool = True) -> DriverStatus:
        """Async version of check_driver."""
        # Run in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.check_driver(driver_name, auto_install)
        )

    def check_all_drivers(self, auto_install: bool = True) -> Dict[str, DriverStatus]:
        """
        Check status of all registered drivers.

        Args:
            auto_install: Whether to auto-install missing drivers

        Returns:
            Dict mapping driver names to their status
        """
        results = {}
        for driver_name in _drivers.keys():
            results[driver_name] = self.check_driver(driver_name, auto_install)
        return results

    async def check_all_drivers_async(self, auto_install: bool = True) -> Dict[str, DriverStatus]:
        """Async version of check_all_drivers."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.check_all_drivers(auto_install)
        )

    # =========================================================================
    # Driver Access
    # =========================================================================

    def get_psycopg2(self, auto_install: bool = True) -> Optional[Any]:
        """
        Get psycopg2 module, installing if necessary.

        Args:
            auto_install: Whether to auto-install if missing

        Returns:
            psycopg2 module or None if unavailable
        """
        if self._psycopg2 is not None:
            return self._psycopg2

        status = self.check_driver('psycopg2', auto_install)
        if status == DriverStatus.READY:
            return self._psycopg2

        return None

    async def get_psycopg2_async(self, auto_install: bool = True) -> Optional[Any]:
        """Async version of get_psycopg2."""
        if self._psycopg2 is not None:
            return self._psycopg2

        status = await self.check_driver_async('psycopg2', auto_install)
        if status == DriverStatus.READY:
            return self._psycopg2

        return None

    def get_asyncpg(self, auto_install: bool = True) -> Optional[Any]:
        """
        Get asyncpg module, installing if necessary.

        Args:
            auto_install: Whether to auto-install if missing

        Returns:
            asyncpg module or None if unavailable
        """
        if self._asyncpg is not None:
            return self._asyncpg

        status = self.check_driver('asyncpg', auto_install)
        if status == DriverStatus.READY:
            return self._asyncpg

        return None

    async def get_asyncpg_async(self, auto_install: bool = True) -> Optional[Any]:
        """Async version of get_asyncpg."""
        if self._asyncpg is not None:
            return self._asyncpg

        status = await self.check_driver_async('asyncpg', auto_install)
        if status == DriverStatus.READY:
            return self._asyncpg

        return None

    # =========================================================================
    # Sync Connection Helpers (using psycopg2)
    # =========================================================================

    def create_sync_connection(
        self,
        host: str = '127.0.0.1',
        port: int = 5432,
        database: str = 'postgres',
        user: str = 'postgres',
        password: str = '',
        timeout: int = 10,
        auto_install: bool = True,
    ) -> Optional[Any]:
        """
        Create a synchronous database connection using psycopg2.

        Args:
            host: Database host
            port: Database port
            database: Database name
            user: Database user
            password: Database password
            timeout: Connection timeout in seconds
            auto_install: Whether to auto-install psycopg2 if missing

        Returns:
            psycopg2 connection or None if unavailable
        """
        psycopg2 = self.get_psycopg2(auto_install)
        if not psycopg2:
            logger.warning("[DRIVER] psycopg2 not available for sync connection")
            return None

        try:
            conn = psycopg2.connect(
                host=host,
                port=port,
                database=database,
                user=user,
                password=password,
                connect_timeout=timeout
            )
            return conn
        except Exception as e:
            logger.debug(f"[DRIVER] Sync connection failed: {e}")
            return None

    def test_sync_connection(
        self,
        host: str = '127.0.0.1',
        port: int = 5432,
        database: str = 'postgres',
        user: str = 'postgres',
        password: str = '',
        timeout: int = 10,
    ) -> Tuple[bool, Optional[str]]:
        """
        Test a synchronous database connection.

        Returns:
            Tuple of (success: bool, error_message: str or None)
        """
        conn = None
        cursor = None
        try:
            conn = self.create_sync_connection(
                host=host,
                port=port,
                database=database,
                user=user,
                password=password,
                timeout=timeout
            )

            if not conn:
                return False, "Failed to create connection (psycopg2 not available)"

            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            return True, None

        except Exception as e:
            return False, str(e)

        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    # =========================================================================
    # Async Connection Helpers (using asyncpg)
    # =========================================================================

    async def create_async_connection(
        self,
        host: str = '127.0.0.1',
        port: int = 5432,
        database: str = 'postgres',
        user: str = 'postgres',
        password: str = '',
        timeout: float = 10.0,
        auto_install: bool = True,
    ) -> Optional[Any]:
        """
        Create an asynchronous database connection using asyncpg.

        Args:
            host: Database host
            port: Database port
            database: Database name
            user: Database user
            password: Database password
            timeout: Connection timeout in seconds
            auto_install: Whether to auto-install asyncpg if missing

        Returns:
            asyncpg connection or None if unavailable
        """
        asyncpg = await self.get_asyncpg_async(auto_install)
        if not asyncpg:
            logger.warning("[DRIVER] asyncpg not available for async connection")
            return None

        try:
            conn = await asyncio.wait_for(
                asyncpg.connect(
                    host=host,
                    port=port,
                    database=database,
                    user=user,
                    password=password,
                ),
                timeout=timeout
            )
            return conn
        except Exception as e:
            logger.debug(f"[DRIVER] Async connection failed: {e}")
            return None

    async def test_async_connection(
        self,
        host: str = '127.0.0.1',
        port: int = 5432,
        database: str = 'postgres',
        user: str = 'postgres',
        password: str = '',
        timeout: float = 10.0,
    ) -> Tuple[bool, Optional[str]]:
        """
        Test an asynchronous database connection.

        Returns:
            Tuple of (success: bool, error_message: str or None)
        """
        conn = None
        try:
            conn = await self.create_async_connection(
                host=host,
                port=port,
                database=database,
                user=user,
                password=password,
                timeout=timeout
            )

            if not conn:
                return False, "Failed to create connection (asyncpg not available)"

            result = await conn.fetchval("SELECT 1")
            if result == 1:
                return True, None
            return False, "Unexpected query result"

        except Exception as e:
            return False, str(e)

        finally:
            if conn:
                try:
                    await conn.close()
                except Exception:
                    pass

    # =========================================================================
    # Status Reporting
    # =========================================================================

    def get_status_report(self) -> Dict[str, Any]:
        """
        Get comprehensive status report of all drivers.

        Returns:
            Dict with driver status information
        """
        report = {
            'timestamp': datetime.now().isoformat(),
            'auto_install_enabled': self._auto_install_enabled,
            'drivers': {}
        }

        for name, info in _drivers.items():
            report['drivers'][name] = {
                'status': info.status.name,
                'version': info.version,
                'package': info.package_name,
                'error': info.error_message,
                'last_check': info.last_check.isoformat() if info.last_check else None,
                'install_attempts': info.install_attempts,
            }

        return report

    def log_status(self) -> None:
        """Log current driver status."""
        for name, info in _drivers.items():
            status_icon = "✅" if info.status == DriverStatus.READY else "❌"
            version_str = f" v{info.version}" if info.version else ""
            logger.info(f"[DRIVER] {status_icon} {name}: {info.status.name}{version_str}")

    # =========================================================================
    # Configuration
    # =========================================================================

    def enable_auto_install(self, enabled: bool = True) -> None:
        """Enable or disable auto-installation of missing drivers."""
        self._auto_install_enabled = enabled
        logger.info(f"[DRIVER] Auto-install {'enabled' if enabled else 'disabled'}")

    def reset_install_attempts(self) -> None:
        """Reset install attempt counters for all drivers."""
        with self._lock:
            for info in _drivers.values():
                info.install_attempts = 0
            logger.info("[DRIVER] Install attempt counters reset")


# =============================================================================
# Global Singleton Accessor
# =============================================================================

_manager: Optional[DatabaseDriverManager] = None
_manager_lock = threading.Lock()


def get_driver_manager() -> DatabaseDriverManager:
    """Get singleton driver manager instance."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = DatabaseDriverManager()
        return _manager


async def get_driver_manager_async() -> DatabaseDriverManager:
    """Get singleton driver manager instance (async version)."""
    return get_driver_manager()


# =============================================================================
# Convenience Functions
# =============================================================================

def is_psycopg2_available() -> bool:
    """Check if psycopg2 is available (without auto-install)."""
    manager = get_driver_manager()
    status = manager.check_driver('psycopg2', auto_install=False)
    return status == DriverStatus.READY


def is_asyncpg_available() -> bool:
    """Check if asyncpg is available (without auto-install)."""
    manager = get_driver_manager()
    status = manager.check_driver('asyncpg', auto_install=False)
    return status == DriverStatus.READY


def ensure_drivers_installed() -> Dict[str, bool]:
    """
    Ensure all PostgreSQL drivers are installed.

    Returns:
        Dict mapping driver names to installation success
    """
    manager = get_driver_manager()
    results = manager.check_all_drivers(auto_install=True)
    return {name: status == DriverStatus.READY for name, status in results.items()}


async def ensure_drivers_installed_async() -> Dict[str, bool]:
    """Async version of ensure_drivers_installed."""
    manager = get_driver_manager()
    results = await manager.check_all_drivers_async(auto_install=True)
    return {name: status == DriverStatus.READY for name, status in results.items()}


# =============================================================================
# Module Initialization
# =============================================================================

def _init_on_import():
    """Initialize drivers on module import (lazy, non-blocking)."""
    # Just create the manager, don't check drivers yet
    get_driver_manager()


# Run lazy init
_init_on_import()


if __name__ == '__main__':
    # CLI for testing
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    parser = argparse.ArgumentParser(description='Database Driver Manager')
    parser.add_argument(
        'command',
        choices=['check', 'install', 'status', 'test'],
        help='Command to run'
    )
    parser.add_argument(
        '--driver',
        choices=['psycopg2', 'asyncpg', 'all'],
        default='all',
        help='Driver to check/install'
    )
    parser.add_argument('--host', default='127.0.0.1', help='Database host')
    parser.add_argument('--port', type=int, default=5432, help='Database port')
    parser.add_argument('--database', default='postgres', help='Database name')
    parser.add_argument('--user', default='postgres', help='Database user')
    parser.add_argument('--password', default='', help='Database password')

    args = parser.parse_args()
    manager = get_driver_manager()

    if args.command == 'check':
        if args.driver == 'all':
            results = manager.check_all_drivers(auto_install=False)
            for name, status in results.items():
                print(f"{name}: {status.name}")
        else:
            status = manager.check_driver(args.driver, auto_install=False)
            print(f"{args.driver}: {status.name}")

    elif args.command == 'install':
        if args.driver == 'all':
            results = manager.check_all_drivers(auto_install=True)
            for name, status in results.items():
                print(f"{name}: {status.name}")
        else:
            status = manager.check_driver(args.driver, auto_install=True)
            print(f"{args.driver}: {status.name}")

    elif args.command == 'status':
        report = manager.get_status_report()
        import json
        print(json.dumps(report, indent=2))

    elif args.command == 'test':
        success, error = manager.test_sync_connection(
            host=args.host,
            port=args.port,
            database=args.database,
            user=args.user,
            password=args.password
        )
        if success:
            print("✅ Connection test successful")
        else:
            print(f"❌ Connection test failed: {error}")
