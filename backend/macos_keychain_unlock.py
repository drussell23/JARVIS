#!/usr/bin/env python3
"""
macOS Keychain Integration for Screen Unlock
=============================================

Advanced, robust, async keychain password management with:
- Intelligent caching with configurable TTL
- Parallel keychain service lookup (multiple service names)
- Circuit breaker pattern for fault tolerance
- Comprehensive metrics and diagnostics
- Non-blocking async operations throughout
- Dynamic configuration (no hardcoding)

This is the PRIMARY keychain integration for voice biometric screen unlock.
"""

import asyncio
import hashlib
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

from backend.core.async_safety import LazyAsyncLock
from backend.security.credential_eradication import eradicated_path

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION - Dynamic, no hardcoding
# =============================================================================

class KeychainServiceConfig:
    """Dynamic keychain service configuration"""

    # Emptied by the credential eradication sweep. Retained as a named, typed
    # structure so lookup helpers keep working (and find nothing) rather than
    # raising AttributeError on a shape that used to exist.
    SERVICES: List[Tuple[str, str, int]] = []

    # Cache configuration
    DEFAULT_CACHE_TTL_SECONDS = 3600.0  # 1 hour

    # Timeout configuration
    QUERY_TIMEOUT_SECONDS = 2.0
    PARALLEL_LOOKUP_TIMEOUT_SECONDS = 5.0

    # Circuit breaker configuration
    CIRCUIT_BREAKER_THRESHOLD = 3
    CIRCUIT_BREAKER_TIMEOUT_SECONDS = 60.0

    # Retry configuration
    MAX_RETRIES = 2
    RETRY_BACKOFF_BASE_SECONDS = 0.1


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class CachedPassword:
    """Cached password with metadata for intelligent cache management"""
    password: str
    password_hash: str
    service_name: str
    account_name: str
    cached_at: float
    ttl_seconds: float = KeychainServiceConfig.DEFAULT_CACHE_TTL_SECONDS
    access_count: int = 0

    @property
    def is_expired(self) -> bool:
        """Check if cache entry has expired"""
        return (time.time() - self.cached_at) > self.ttl_seconds

    @property
    def age_seconds(self) -> float:
        """Get age of cache entry in seconds"""
        return time.time() - self.cached_at

    @property
    def ttl_remaining_seconds(self) -> float:
        """Get remaining TTL in seconds"""
        return max(0, self.ttl_seconds - self.age_seconds)

    def touch(self) -> None:
        """Record an access to this cache entry"""
        self.access_count += 1


@dataclass
class KeychainMetrics:
    """Comprehensive metrics for keychain operations"""
    total_lookups: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    async_fetches: int = 0
    parallel_lookups: int = 0
    sequential_lookups: int = 0
    failures: int = 0
    timeouts: int = 0
    total_lookup_time_ms: float = 0.0
    last_lookup_time_ms: float = 0.0
    last_success_service: Optional[str] = None
    last_success_time: Optional[float] = None
    circuit_breaker_trips: int = 0

    @property
    def avg_lookup_time_ms(self) -> float:
        """Calculate average lookup time"""
        if self.total_lookups == 0:
            return 0.0
        return self.total_lookup_time_ms / self.total_lookups

    @property
    def cache_hit_rate(self) -> float:
        """Calculate cache hit rate"""
        if self.total_lookups == 0:
            return 0.0
        return self.cache_hits / self.total_lookups

    def record_lookup(self, duration_ms: float, cache_hit: bool, parallel: bool = False) -> None:
        """Record a lookup operation"""
        self.total_lookups += 1
        self.total_lookup_time_ms += duration_ms
        self.last_lookup_time_ms = duration_ms

        if cache_hit:
            self.cache_hits += 1
        else:
            self.cache_misses += 1
            self.async_fetches += 1
            if parallel:
                self.parallel_lookups += 1
            else:
                self.sequential_lookups += 1

    def to_dict(self) -> Dict[str, Any]:
        """Convert metrics to dictionary for JSON serialization"""
        return {
            "total_lookups": self.total_lookups,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": self.cache_hit_rate,
            "async_fetches": self.async_fetches,
            "parallel_lookups": self.parallel_lookups,
            "sequential_lookups": self.sequential_lookups,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "avg_lookup_time_ms": self.avg_lookup_time_ms,
            "last_lookup_time_ms": self.last_lookup_time_ms,
            "last_success_service": self.last_success_service,
            "circuit_breaker_trips": self.circuit_breaker_trips,
        }


# =============================================================================
# MAIN CLASS - MacOSKeychainUnlock (Enhanced)
# =============================================================================

class MacOSKeychainUnlock:
    """
    Advanced macOS Keychain integration for voice biometric screen unlock.

    Features:
    - Async-first design (never blocks event loop)
    - Intelligent password caching with configurable TTL
    - Parallel keychain service lookup
    - Circuit breaker for fault tolerance
    - Comprehensive metrics and diagnostics
    - Dynamic service discovery (no hardcoding)

    Usage:
        unlock_service = MacOSKeychainUnlock()

        # Get password (cached after first call)
        password = await unlock_service.get_password_from_keychain()

        # Get password hash for verification
        password_hash = await unlock_service.get_password_hash()

        # Preload cache during initialization
        await unlock_service.preload_cache()

        # Full unlock flow
        result = await unlock_service.unlock_screen(verified_speaker="Derek")
    """

    def __init__(
        self,
        cache_ttl_seconds: float = KeychainServiceConfig.DEFAULT_CACHE_TTL_SECONDS,
        enable_parallel_lookup: bool = True,
        enable_cache: bool = True,
    ):
        """
        Initialize the keychain unlock service.

        Args:
            cache_ttl_seconds: How long to cache passwords (default: 1 hour)
            enable_parallel_lookup: Query all services in parallel (faster)
            enable_cache: Enable password caching (recommended)
        """
        # Configuration
        self.cache_ttl_seconds = cache_ttl_seconds
        self.enable_parallel_lookup = enable_parallel_lookup
        self.enable_cache = enable_cache

        # Primary service info (for backwards compatibility)
        # Credential coordinates deliberately absent: there is no stored login
        # credential, and naming one here would be a resurrection foothold.
        self.service_name = ""
        self.account_name = ""
        self.keychain_item_name = "JARVIS Voice Unlock"

        # Cache state
        self._cache: Optional[CachedPassword] = None
        self._cache_lock = asyncio.Lock()

        # Metrics
        self._metrics = KeychainMetrics()

        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_breaker_reset_time: Optional[float] = None

        logger.info(
            f"MacOSKeychainUnlock initialized "
            f"(cache_ttl={cache_ttl_seconds}s, parallel={enable_parallel_lookup}, cache={enable_cache})"
        )

    # =========================================================================
    # PASSWORD RETRIEVAL (Enhanced with caching)
    # =========================================================================

    async def get_password_from_keychain(self, force_refresh: bool = False) -> Optional[str]:
        """
        ERADICATED. Cleartext login-credential retrieval no longer exists.

        This was the chokepoint every unlock path funnelled through. It fetched
        the macOS login password in cleartext so it could be typed into the lock
        screen -- a premise that cannot work, because ``SecurityAgent`` holds
        SecureEventInput and drops every synthesised keystroke (47/47 recorded
        sessions typed 13/13 characters into a void with the screen still
        locked).

        The signature is retained so that callers fail loudly at the seam rather
        than silently taking a different dead-end branch.

        Raises:
            CleartextCredentialEradicated: Always.
        """
        eradicated_path("MacOSKeychainUnlock.get_password_from_keychain")

    async def get_password_hash(self, force_refresh: bool = False) -> Optional[str]:
        """
        ERADICATED. Hashing the credential still required retrieving it.

        Raises:
            CleartextCredentialEradicated: Always.
        """
        eradicated_path("MacOSKeychainUnlock.get_password_hash")

    async def preload_cache(self) -> bool:
        """
        ERADICATED. There is no credential to preload.

        Raises:
            CleartextCredentialEradicated: Always.
        """
        eradicated_path("MacOSKeychainUnlock.preload_cache")

    async def invalidate_cache(self) -> None:
        """Invalidate the cached password"""
        async with self._cache_lock:
            self._cache = None
        logger.info("Keychain cache invalidated")

    # =========================================================================
    # ASYNC KEYCHAIN QUERIES
    # =========================================================================

    async def _fetch_password_async(self) -> Tuple[Optional[str], Optional[str]]:
        """
        ERADICATED. The keychain fetch machinery has been removed.

        Previously fanned out ``security find-generic-password`` across the
        service list in :class:`KeychainServiceConfig`, in parallel or
        sequentially, and returned the login password in cleartext.

        Raises:
            CleartextCredentialEradicated: Always.
        """
        eradicated_path("MacOSKeychainUnlock._fetch_password_async")

    async def _query_keychain_async(self, service_name: str, account_name: str) -> Optional[str]:
        """
        ERADICATED. No code path may shell out to retrieve a login credential.

        Raises:
            CleartextCredentialEradicated: Always.
        """
        eradicated_path(
            f"MacOSKeychainUnlock._query_keychain_async({service_name}/{account_name})"
        )


    # =========================================================================
    # STORE PASSWORD
    # =========================================================================

    async def store_password_in_keychain(self, password: str) -> bool:
        """
        ERADICATED. Wrote the login password into the login keychain.

        Two distinct defects, both fatal:

        * The password was passed as ``-w <password>`` on the argv of a
          subprocess, so it was visible to any process able to read the process
          table while the command ran.
        * ``-T /usr/bin/security`` added the ``security`` binary to the item's
          ACL, which is why the credential could later be read back in cleartext
          with no authorisation prompt.

        Raises:
            CleartextCredentialEradicated: Always.
        """
        eradicated_path("MacOSKeychainUnlock.store_password_in_keychain")

    # =========================================================================
    # SCREEN LOCK DETECTION
    # =========================================================================

    async def check_screen_locked(self) -> bool:
        """Check if screen is currently locked"""
        try:
            from voice_unlock.objc.server.screen_lock_detector import is_screen_locked
            return is_screen_locked()
        except ImportError:
            # Fallback to AppleScript
            logger.debug("Using fallback AppleScript for screen detection")
            script = """
            tell application "System Events"
                set isLocked to false
                if (exists process "ScreenSaverEngine") then
                    set isLocked to true
                end if
                try
                    set frontApp to name of first application process whose frontmost is true
                    if frontApp is "loginwindow" then
                        set isLocked to true
                    end if
                end try
                return isLocked
            end tell
            """
            try:
                process = await asyncio.create_subprocess_exec(
                    "osascript", "-e", script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await process.communicate()
                return stdout.decode().strip() == "true"
            except Exception as e:
                logger.error(f"Failed to check screen status: {e}")
                return False

    # =========================================================================
    # SCREEN UNLOCK
    # =========================================================================

    async def unlock_screen(self, verified_speaker: Optional[str] = None) -> Dict[str, Any]:
        """
        Refuse the keystroke-synthesis unlock; name the mechanism that replaces it.

        This returns a structured refusal rather than raising, because it sits on
        the voice-command response path: the operator needs a spoken explanation,
        not a traceback. The credential paths beneath it raise; this seam reports.

        Args:
            verified_speaker: Speaker whose identity was verified upstream.

        Returns:
            A result dict with ``success=False`` and ``action="eradicated"``.
        """
        logger.error(
            "unlock_screen called on the eradicated keystroke path "
            "(speaker=%s); SecureEventInput makes this mechanism impossible",
            verified_speaker or "unverified",
        )
        return {
            "success": False,
            "message": (
                "Screen unlock by simulated typing is not possible on this version of "
                "macOS: the lock screen holds SecureEventInput, so synthesised "
                "keystrokes are discarded. Install the JARVIS Authorization Plugin to "
                "unlock without a password."
            ),
            "action": "eradicated",
            "replacement": "backend/voice_unlock/authplugin",
            "verified_speaker": verified_speaker,
        }


    def _is_circuit_open(self) -> bool:
        """Check if circuit breaker is open"""
        if self._consecutive_failures < KeychainServiceConfig.CIRCUIT_BREAKER_THRESHOLD:
            return False

        if self._circuit_breaker_reset_time:
            if time.time() >= self._circuit_breaker_reset_time:
                self._consecutive_failures = 0
                self._circuit_breaker_reset_time = None
                logger.info("Keychain circuit breaker RESET")
                return False

        return True

    def _record_failure(self) -> None:
        """Record a failure for circuit breaker"""
        self._consecutive_failures += 1
        if self._consecutive_failures >= KeychainServiceConfig.CIRCUIT_BREAKER_THRESHOLD:
            self._circuit_breaker_reset_time = (
                time.time() + KeychainServiceConfig.CIRCUIT_BREAKER_TIMEOUT_SECONDS
            )
            self._metrics.circuit_breaker_trips += 1
            logger.warning(
                f"Keychain circuit breaker OPEN "
                f"(will reset in {KeychainServiceConfig.CIRCUIT_BREAKER_TIMEOUT_SECONDS}s)"
            )

    def _reset_circuit_breaker(self) -> None:
        """Reset circuit breaker on success"""
        if self._consecutive_failures > 0:
            logger.debug(f"Circuit breaker reset after {self._consecutive_failures} failures")
        self._consecutive_failures = 0
        self._circuit_breaker_reset_time = None

    # =========================================================================
    # UTILITIES
    # =========================================================================

    def _get_account_for_service(self, service_name: str) -> str:
        """Get account name for a service"""
        for svc, acct, _ in KeychainServiceConfig.SERVICES:
            if svc == service_name:
                return acct
        return "unknown"

    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics as dictionary"""
        metrics = self._metrics.to_dict()
        metrics["circuit_breaker_open"] = self._is_circuit_open()
        metrics["cache_enabled"] = self.enable_cache
        metrics["cache_valid"] = self._cache is not None and not self._cache.is_expired
        metrics["cache_ttl_remaining_s"] = (
            self._cache.ttl_remaining_seconds if self._cache else 0.0
        )
        return metrics

    def get_cache_info(self) -> Dict[str, Any]:
        """Get detailed cache information"""
        if not self._cache:
            return {"cached": False}

        return {
            "cached": True,
            "expired": self._cache.is_expired,
            "service_name": self._cache.service_name,
            "account_name": self._cache.account_name,
            "cached_at": self._cache.cached_at,
            "age_seconds": self._cache.age_seconds,
            "ttl_remaining_seconds": self._cache.ttl_remaining_seconds,
            "access_count": self._cache.access_count,
        }

    # =========================================================================
    # SETUP
    # =========================================================================

    async def setup_keychain_password(self):
        """
        ERADICATED. Prompted for the login password and stored it.

        This was the other half of the vulnerability: eradicating retrieval while
        leaving the storer intact would let any setup script re-plant the
        credential.

        Raises:
            CleartextCredentialEradicated: Always.
        """
        eradicated_path("MacOSKeychainUnlock.setup_keychain_password")


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_keychain_unlock_instance: Optional[MacOSKeychainUnlock] = None
_instance_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_keychain_unlock_service() -> MacOSKeychainUnlock:
    """
    Get or create the global keychain unlock service instance.
    Thread-safe singleton pattern.
    """
    global _keychain_unlock_instance

    if _keychain_unlock_instance is None:
        async with _instance_lock:
            if _keychain_unlock_instance is None:
                _keychain_unlock_instance = MacOSKeychainUnlock()

    return _keychain_unlock_instance


async def get_password_async() -> Optional[str]:
    """
    ERADICATED. Module-level shortcut to the removed credential fetch.

    Raises:
        CleartextCredentialEradicated: Always.
    """
    eradicated_path("macos_keychain_unlock.get_password_async")


async def get_password_hash_async() -> Optional[str]:
    """
    ERADICATED. Hashing still required retrieving the credential.

    Raises:
        CleartextCredentialEradicated: Always.
    """
    eradicated_path("macos_keychain_unlock.get_password_hash_async")


async def preload_keychain_cache() -> bool:
    """
    ERADICATED. Nothing to preload; boot must not warm a credential cache.

    Raises:
        CleartextCredentialEradicated: Always.
    """
    eradicated_path("macos_keychain_unlock.preload_keychain_cache")


# =============================================================================
# CLI
# =============================================================================

async def main():
    """Report that the keychain setup/test CLI has been eradicated."""
    logging.basicConfig(level=logging.INFO)

    print("\n" + "=" * 68)
    print("JARVIS SCREEN UNLOCK -- KEYCHAIN PATH ERADICATED")
    print("=" * 68)
    print(
        "\nStoring the macOS login password so JARVIS could type it at the lock\n"
        "screen has been removed. It could never work: SecurityAgent holds\n"
        "SecureEventInput, so every synthesised keystroke was discarded.\n\n"
        "The replacement is the JARVIS Authorization Plugin, which satisfies the\n"
        "system.login.screensaver right directly and needs no password at all:\n\n"
        "    backend/voice_unlock/authplugin/\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
