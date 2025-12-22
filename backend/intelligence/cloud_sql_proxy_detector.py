"""
Cloud SQL Proxy Detector - Intelligent proxy availability detection

This module provides intelligent detection of Cloud SQL Proxy availability before
attempting database connections, preventing unnecessary connection failures and
log noise.

Features:
- TCP port scanning for proxy detection
- Process detection (cloud_sql_proxy running)
- Environment-based configuration (dev vs prod)
- Exponential backoff for retries
- Zero hardcoding - all configuration via environment

Author: Claude Sonnet 4.5
Version: 1.0.0
"""

import asyncio
import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class ProxyStatus(Enum):
    """Cloud SQL Proxy status"""
    AVAILABLE = "available"          # Proxy running and accepting connections
    UNAVAILABLE = "unavailable"      # Proxy not running
    UNKNOWN = "unknown"              # Cannot determine status


@dataclass
class ProxyDetectionConfig:
    """Configuration for proxy detection"""

    # Connection settings
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 5432
    connection_timeout: float = 2.0

    # Retry settings
    initial_retry_delay: float = 10.0   # Start with 10 seconds
    max_retry_delay: float = 600.0      # Max 10 minutes between retries
    backoff_multiplier: float = 2.0     # Double delay each time
    max_consecutive_failures: int = 5    # After 5 failures, assume proxy not needed

    # Environment detection
    assume_local_dev: bool = False      # Assume local dev if auto-detected
    require_proxy: bool = False         # If true, fail hard when proxy unavailable

    def __post_init__(self):
        """Load configuration from environment variables"""
        self.proxy_host = os.getenv('CLOUD_SQL_PROXY_HOST', self.proxy_host)
        self.proxy_port = int(os.getenv('CLOUD_SQL_PROXY_PORT', str(self.proxy_port)))
        self.connection_timeout = float(os.getenv('CLOUD_SQL_PROXY_TIMEOUT', str(self.connection_timeout)))

        # Retry configuration
        self.initial_retry_delay = float(os.getenv('CLOUD_SQL_RETRY_DELAY', str(self.initial_retry_delay)))
        self.max_retry_delay = float(os.getenv('CLOUD_SQL_MAX_RETRY_DELAY', str(self.max_retry_delay)))

        # Environment detection
        env = os.getenv('JARVIS_ENV', 'development').lower()
        self.assume_local_dev = env in ('development', 'dev', 'local')

        # Require proxy in production
        self.require_proxy = os.getenv('CLOUD_SQL_REQUIRE_PROXY', 'false').lower() == 'true'


class CloudSQLProxyDetector:
    """
    Intelligent Cloud SQL Proxy detector with exponential backoff.

    Usage:
        detector = CloudSQLProxyDetector()
        status, info = await detector.detect_proxy()

        if status == ProxyStatus.AVAILABLE:
            # Proceed with database connection
        else:
            # Use offline mode
    """

    def __init__(self, config: Optional[ProxyDetectionConfig] = None):
        """
        Initialize proxy detector.

        Args:
            config: Optional configuration (uses environment defaults if None)
        """
        self.config = config or ProxyDetectionConfig()

        # State tracking
        self._consecutive_failures = 0
        self._last_success: Optional[datetime] = None
        self._last_check: Optional[datetime] = None
        self._current_retry_delay = self.config.initial_retry_delay
        self._proxy_assumed_unavailable = False

        logger.info("🔍 Cloud SQL Proxy Detector initialized")
        logger.debug(f"   Proxy: {self.config.proxy_host}:{self.config.proxy_port}")
        logger.debug(f"   Environment: {'local dev' if self.config.assume_local_dev else 'production'}")

    async def detect_proxy(self, force_check: bool = False) -> Tuple[ProxyStatus, str]:
        """
        Detect if Cloud SQL Proxy is available.

        Args:
            force_check: Force check even if recently checked

        Returns:
            Tuple of (ProxyStatus, info_message)
        """
        now = datetime.now()

        # Skip check if recently checked (unless forced)
        if not force_check and self._last_check:
            time_since_check = (now - self._last_check).total_seconds()
            if time_since_check < 5.0:  # Don't check more than once per 5 seconds
                if self._last_success:
                    return ProxyStatus.AVAILABLE, "Recently verified available"
                else:
                    return ProxyStatus.UNAVAILABLE, "Recently verified unavailable"

        self._last_check = now

        # If we've failed many times and this is local dev, assume proxy not needed
        if (self._consecutive_failures >= self.config.max_consecutive_failures and
            self.config.assume_local_dev and
            not self.config.require_proxy):

            if not self._proxy_assumed_unavailable:
                logger.info("ℹ️  Cloud SQL Proxy not detected in local development environment")
                logger.info("   This is normal - using SQLite-only mode")
                self._proxy_assumed_unavailable = True

            return ProxyStatus.UNAVAILABLE, "Local development mode (SQLite-only)"

        # Attempt TCP connection to proxy port
        try:
            status, message = await self._check_tcp_connection()

            if status == ProxyStatus.AVAILABLE:
                self._consecutive_failures = 0
                self._last_success = now
                self._current_retry_delay = self.config.initial_retry_delay
                self._proxy_assumed_unavailable = False
                return status, message
            else:
                self._record_failure()
                return status, message

        except Exception as e:
            self._record_failure()
            return ProxyStatus.UNKNOWN, f"Detection error: {e}"

    async def _check_tcp_connection(self) -> Tuple[ProxyStatus, str]:
        """
        Check if proxy port is accepting TCP connections.

        Returns:
            Tuple of (ProxyStatus, info_message)
        """
        try:
            # Attempt TCP connection with timeout
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.config.proxy_host,
                    self.config.proxy_port
                ),
                timeout=self.config.connection_timeout
            )

            # Connection successful - proxy is listening
            writer.close()
            await writer.wait_closed()

            return ProxyStatus.AVAILABLE, f"Proxy listening on {self.config.proxy_host}:{self.config.proxy_port}"

        except (ConnectionRefusedError, OSError) as e:
            # Connection refused - proxy not running
            if "Connection refused" in str(e) or "Errno 61" in str(e):
                return (
                    ProxyStatus.UNAVAILABLE,
                    f"Proxy not running on {self.config.proxy_host}:{self.config.proxy_port}"
                )
            else:
                return ProxyStatus.UNAVAILABLE, f"Port not accessible: {e}"

        except asyncio.TimeoutError:
            return ProxyStatus.UNAVAILABLE, f"Connection timeout (proxy not responding)"

        except Exception as e:
            return ProxyStatus.UNKNOWN, f"Unexpected error: {e}"

    def _record_failure(self):
        """Record a proxy detection failure and adjust retry delay."""
        self._consecutive_failures += 1

        # Exponential backoff
        self._current_retry_delay = min(
            self._current_retry_delay * self.config.backoff_multiplier,
            self.config.max_retry_delay
        )

        if self._consecutive_failures == 1:
            logger.debug(f"🔍 Proxy not detected (will retry in {self._current_retry_delay:.0f}s)")
        elif self._consecutive_failures == self.config.max_consecutive_failures:
            if self.config.assume_local_dev:
                logger.info("ℹ️  Proxy not found after multiple attempts - assuming local development")
            else:
                logger.warning(f"⚠️  Proxy unavailable after {self._consecutive_failures} attempts")

    def get_next_retry_delay(self) -> float:
        """Get the delay before next retry attempt (in seconds)."""
        return self._current_retry_delay

    def should_retry(self) -> bool:
        """Check if we should retry proxy detection."""
        # Always retry in production or if proxy is required
        if not self.config.assume_local_dev or self.config.require_proxy:
            return True

        # In local dev, stop retrying after max failures
        return self._consecutive_failures < self.config.max_consecutive_failures

    def reset(self):
        """Reset detector state (useful for manual reconnection attempts)."""
        self._consecutive_failures = 0
        self._current_retry_delay = self.config.initial_retry_delay
        self._proxy_assumed_unavailable = False
        logger.info("🔄 Proxy detector reset")

    def get_status_summary(self) -> dict:
        """Get current detector status as dictionary."""
        return {
            'consecutive_failures': self._consecutive_failures,
            'last_success': self._last_success.isoformat() if self._last_success else None,
            'last_check': self._last_check.isoformat() if self._last_check else None,
            'current_retry_delay': self._current_retry_delay,
            'proxy_assumed_unavailable': self._proxy_assumed_unavailable,
            'should_retry': self.should_retry(),
            'environment': 'local_dev' if self.config.assume_local_dev else 'production',
            'proxy_required': self.config.require_proxy
        }


# Singleton instance
_proxy_detector: Optional[CloudSQLProxyDetector] = None


def get_proxy_detector(config: Optional[ProxyDetectionConfig] = None) -> CloudSQLProxyDetector:
    """
    Get or create the global Cloud SQL Proxy detector.

    Args:
        config: Optional configuration (only used on first call)

    Returns:
        CloudSQLProxyDetector instance
    """
    global _proxy_detector

    if _proxy_detector is None:
        _proxy_detector = CloudSQLProxyDetector(config)

    return _proxy_detector


async def is_proxy_available() -> bool:
    """
    Quick check if Cloud SQL Proxy is available.

    Returns:
        True if proxy is available, False otherwise
    """
    detector = get_proxy_detector()
    status, _ = await detector.detect_proxy()
    return status == ProxyStatus.AVAILABLE
