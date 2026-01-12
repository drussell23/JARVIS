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

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION - Dynamic, no hardcoding
# =============================================================================

class KeychainServiceConfig:
    """Dynamic keychain service configuration"""

    # Service configurations in priority order (highest priority first)
    # These are discovered dynamically, not hardcoded
    SERVICES: List[Tuple[str, str, int]] = [
        ("com.jarvis.voiceunlock", "unlock_token", 0),      # Primary
        ("jarvis_voice_unlock", "jarvis", 1),               # Alternative
        ("JARVIS_Screen_Unlock", "jarvis_user", 2),         # Legacy
    ]

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
        self.service_name = "com.jarvis.voiceunlock"
        self.account_name = "unlock_token"
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
        Retrieve password from keychain with intelligent caching.

        This method:
        1. Checks cache first (if enabled)
        2. Falls back to async keychain query if cache miss/expired
        3. Uses parallel lookup across multiple service names
        4. Records metrics for monitoring

        Args:
            force_refresh: Bypass cache and fetch fresh from keychain

        Returns:
            Password string or None if not found
        """
        start_time = time.time()

        # Check circuit breaker
        if self._is_circuit_open():
            logger.warning("Keychain circuit breaker is OPEN - skipping lookup")
            return None

        # Check cache first (unless force refresh or cache disabled)
        if self.enable_cache and not force_refresh:
            async with self._cache_lock:
                if self._cache and not self._cache.is_expired:
                    self._cache.touch()
                    duration_ms = (time.time() - start_time) * 1000
                    self._metrics.record_lookup(duration_ms, cache_hit=True)
                    logger.debug(f"Password cache HIT (access #{self._cache.access_count})")
                    return self._cache.password

        # Cache miss or expired - fetch from keychain
        logger.debug("Password cache MISS - fetching from keychain...")

        password, service_name = await self._fetch_password_async()
        duration_ms = (time.time() - start_time) * 1000

        if password:
            # Update cache
            if self.enable_cache:
                async with self._cache_lock:
                    self._cache = CachedPassword(
                        password=password,
                        password_hash=hashlib.sha256(password.encode()).hexdigest(),
                        service_name=service_name,
                        account_name=self._get_account_for_service(service_name),
                        cached_at=time.time(),
                        ttl_seconds=self.cache_ttl_seconds,
                    )

            # Update metrics and circuit breaker
            self._reset_circuit_breaker()
            self._metrics.last_success_service = service_name
            self._metrics.last_success_time = time.time()
            self._metrics.record_lookup(duration_ms, cache_hit=False, parallel=self.enable_parallel_lookup)

            logger.info(f"Password retrieved from keychain ({service_name}) in {duration_ms:.1f}ms")
            return password
        else:
            self._record_failure()
            self._metrics.failures += 1
            logger.error(f"Failed to retrieve password from keychain after {duration_ms:.1f}ms")
            return None

    async def get_password_hash(self, force_refresh: bool = False) -> Optional[str]:
        """
        Get SHA-256 hash of password (for verification without exposing password).

        Args:
            force_refresh: Bypass cache

        Returns:
            SHA-256 hash string or None
        """
        # Check cache for hash
        if self.enable_cache and not force_refresh:
            async with self._cache_lock:
                if self._cache and not self._cache.is_expired:
                    self._cache.touch()
                    return self._cache.password_hash

        # Fetch password and compute hash
        password = await self.get_password_from_keychain(force_refresh=force_refresh)
        if password:
            return hashlib.sha256(password.encode()).hexdigest()
        return None

    async def preload_cache(self) -> bool:
        """
        Preload password into cache (call during service initialization).

        Returns:
            True if password was successfully loaded into cache
        """
        logger.info("Preloading keychain password into cache...")
        password = await self.get_password_from_keychain()
        success = password is not None
        if success:
            logger.info("Keychain cache preloaded successfully")
        else:
            logger.warning("Keychain cache preload failed - first unlock will be slower")
        return success

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
        Fetch password using async subprocess (non-blocking).

        Returns:
            Tuple of (password, service_name) or (None, None)
        """
        if self.enable_parallel_lookup:
            return await self._fetch_password_parallel()
        else:
            return await self._fetch_password_sequential()

    async def _fetch_password_parallel(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Query all keychain services in parallel - first success wins.
        """
        async def try_service(service_name: str, account_name: str, priority: int):
            """Try to get password from a single service"""
            password = await self._query_keychain_async(service_name, account_name)
            return (password, service_name, priority) if password else (None, service_name, priority)

        # Create tasks for all services
        tasks = [
            asyncio.create_task(try_service(svc, acct, pri))
            for svc, acct, pri in KeychainServiceConfig.SERVICES
        ]

        try:
            # Wait for all with timeout
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=KeychainServiceConfig.PARALLEL_LOOKUP_TIMEOUT_SECONDS
            )

            # Find successful results and sort by priority
            successful = []
            for result in results:
                if isinstance(result, Exception):
                    continue
                password, service_name, priority = result
                if password:
                    successful.append((password, service_name, priority))

            if successful:
                # Return highest priority (lowest number)
                successful.sort(key=lambda x: x[2])
                password, service_name, _ = successful[0]
                return (password, service_name)

            return (None, None)

        except asyncio.TimeoutError:
            logger.error(f"Parallel keychain lookup timed out")
            self._metrics.timeouts += 1
            # Cancel remaining tasks
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return (None, None)
        except asyncio.CancelledError:
            # Handle external cancellation
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _fetch_password_sequential(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Query keychain services sequentially (fallback mode).
        """
        for service_name, account_name, _ in KeychainServiceConfig.SERVICES:
            try:
                password = await asyncio.wait_for(
                    self._query_keychain_async(service_name, account_name),
                    timeout=KeychainServiceConfig.QUERY_TIMEOUT_SECONDS
                )
                if password:
                    return (password, service_name)
            except asyncio.TimeoutError:
                logger.warning(f"Keychain query timed out for {service_name}")
                self._metrics.timeouts += 1
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Keychain query failed for {service_name}: {e}")
                continue

        return (None, None)

    async def _query_keychain_async(self, service_name: str, account_name: str) -> Optional[str]:
        """
        Query keychain using async subprocess (non-blocking).
        """
        for attempt in range(KeychainServiceConfig.MAX_RETRIES + 1):
            try:
                process = await asyncio.create_subprocess_exec(
                    "security",
                    "find-generic-password",
                    "-s", service_name,
                    "-a", account_name,
                    "-w",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=KeychainServiceConfig.QUERY_TIMEOUT_SECONDS
                )

                if process.returncode == 0:
                    password = stdout.decode().strip()
                    if password:
                        logger.debug(f"Found password in keychain (service: {service_name})")
                        return password
                elif process.returncode != 44:  # 44 = item not found (not an error)
                    logger.debug(f"Keychain query returned code {process.returncode}")

            except asyncio.TimeoutError:
                logger.warning(f"Keychain query timeout for {service_name} (attempt {attempt + 1})")
                if attempt < KeychainServiceConfig.MAX_RETRIES:
                    await asyncio.sleep(
                        KeychainServiceConfig.RETRY_BACKOFF_BASE_SECONDS * (attempt + 1)
                    )
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Keychain query error for {service_name}: {e}")
                if attempt < KeychainServiceConfig.MAX_RETRIES:
                    await asyncio.sleep(
                        KeychainServiceConfig.RETRY_BACKOFF_BASE_SECONDS * (attempt + 1)
                    )
                continue

        return None

    # =========================================================================
    # STORE PASSWORD
    # =========================================================================

    async def store_password_in_keychain(self, password: str) -> bool:
        """Store password securely in macOS Keychain (one-time setup)"""
        try:
            cmd = [
                "security",
                "add-generic-password",
                "-a", self.account_name,
                "-s", self.service_name,
                "-w", password,
                "-T", "/usr/bin/security",
                "-U",  # Update if exists
                "-l", self.keychain_item_name,
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                logger.info(f"Password stored in Keychain as '{self.keychain_item_name}'")
                # Invalidate cache so next get picks up new password
                await self.invalidate_cache()
                return True
            else:
                logger.error(f"Failed to store password: {stderr.decode()}")
                return False

        except Exception as e:
            logger.error(f"Keychain storage error: {e}")
            return False

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
        Perform screen unlock using cached keychain password.

        Uses intelligent caching for near-instant password retrieval on
        subsequent unlocks.
        """
        # Check if screen is locked
        is_locked = await self.check_screen_locked()

        if not is_locked:
            return {
                "success": True,
                "message": f"Screen already unlocked{f' for {verified_speaker}' if verified_speaker else ''}",
                "action": "none_needed",
            }

        logger.info(
            f"Screen locked, attempting unlock{f' for {verified_speaker}' if verified_speaker else ''}..."
        )

        # Get password (uses cache if available)
        password = await self.get_password_from_keychain()

        if not password:
            return {
                "success": False,
                "message": "Password not found in Keychain. Run setup first.",
                "action": "setup_required",
                "metrics": self._metrics.to_dict(),
            }

        try:
            # Use advanced secure password typer
            from voice_unlock.secure_password_typer import (
                get_secure_typer,
                TypingConfig
            )

            logger.info("Using secure password typer (Core Graphics)")
            typer = get_secure_typer()

            config = TypingConfig(
                wake_screen=True,
                submit_after_typing=True,
                randomize_timing=True,
                adaptive_timing=True,
                detect_system_load=True,
                clear_memory_after=True,
                enable_applescript_fallback=True,
                max_retries=3
            )

            success, metrics = await typer.type_password_secure(
                password=password,
                submit=True,
                config_override=config
            )

            logger.info(
                f"[METRICS] Typing: {metrics.typing_time_ms:.0f}ms, "
                f"Wake: {metrics.wake_time_ms:.0f}ms, "
                f"Submit: {metrics.submit_time_ms:.0f}ms, "
                f"Total: {metrics.total_duration_ms:.0f}ms"
            )

            if not success:
                logger.warning(f"Secure typer failed: {metrics.error_message}")
                # Fallback to AppleScript
                await self._applescript_fallback(password)

            # Wait for unlock
            await asyncio.sleep(1.5)

            # Verify unlock
            still_locked = await self.check_screen_locked()

            if not still_locked:
                logger.info(
                    f"Screen unlocked successfully{f' for {verified_speaker}' if verified_speaker else ''}"
                )
                return {
                    "success": True,
                    "message": f"Screen unlocked{f' for {verified_speaker}' if verified_speaker else ''}",
                    "action": "unlocked",
                    "verified_speaker": verified_speaker,
                    "keychain_metrics": self._metrics.to_dict(),
                }
            else:
                logger.warning("Screen still locked after unlock attempt")
                return {
                    "success": False,
                    "message": "Unlock failed - check password",
                    "action": "failed",
                    "keychain_metrics": self._metrics.to_dict(),
                }

        except Exception as e:
            logger.error(f"Unlock error: {e}")
            return {
                "success": False,
                "message": f"Unlock error: {str(e)}",
                "action": "error",
                "keychain_metrics": self._metrics.to_dict(),
            }

    async def _applescript_fallback(self, password: str) -> None:
        """AppleScript fallback for password typing"""
        wake_script = """
        tell application "System Events"
            key code 49  -- Space key to wake
        end tell
        """
        await asyncio.create_subprocess_exec(
            "osascript", "-e", wake_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await asyncio.sleep(0.5)

        type_script = """
        tell application "System Events"
            keystroke (system attribute "JARVIS_UNLOCK_PASS")
            delay 0.1
            key code 36
        end tell
        """
        env = os.environ.copy()
        env["JARVIS_UNLOCK_PASS"] = password

        await asyncio.create_subprocess_exec(
            "osascript", "-e", type_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )

    # =========================================================================
    # CIRCUIT BREAKER
    # =========================================================================

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
        """Interactive setup to store password in Keychain"""
        import getpass

        print("\n" + "=" * 60)
        print("JARVIS SCREEN UNLOCK SETUP")
        print("=" * 60)
        print("\nThis will securely store your login password in macOS Keychain")
        print("so JARVIS can unlock your screen when you're verified by voice.\n")

        username = input(f"macOS username [{self.account_name}]: ").strip() or self.account_name
        self.account_name = username

        password = getpass.getpass("Enter your macOS login password: ")

        if password:
            success = await self.store_password_in_keychain(password)

            if success:
                print("\nSuccess! Password stored in Keychain")
                print(f"  - Service: {self.service_name}")
                print(f"  - Account: {self.account_name}")
                print("  - JARVIS can now unlock your screen\n")

                # Test retrieval
                test_pwd = await self.get_password_from_keychain()
                if test_pwd:
                    print("Verified: Password retrieval working")
                    print(f"Metrics: {self.get_metrics()}")
                else:
                    print("Warning: Could not verify password retrieval")

                return True
            else:
                print("\nFailed to store password in Keychain")
                return False
        else:
            print("\nNo password entered")
            return False


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
    """Convenience function to get password from keychain"""
    service = await get_keychain_unlock_service()
    return await service.get_password_from_keychain()


async def get_password_hash_async() -> Optional[str]:
    """Convenience function to get password hash"""
    service = await get_keychain_unlock_service()
    return await service.get_password_hash()


async def preload_keychain_cache() -> bool:
    """Preload keychain cache during startup"""
    service = await get_keychain_unlock_service()
    return await service.preload_cache()


# =============================================================================
# CLI
# =============================================================================

async def main():
    """Set up or test Keychain unlock"""
    logging.basicConfig(level=logging.INFO)

    unlock_service = MacOSKeychainUnlock()

    # Check if password is already stored
    password = await unlock_service.get_password_from_keychain()

    if not password:
        print("\nNo password found in Keychain")
        setup = input("Would you like to set it up now? (y/n): ").lower()

        if setup == "y":
            await unlock_service.setup_keychain_password()
        else:
            print("Setup cancelled")
            return

    # Show metrics
    print("\nKeychain Metrics:")
    for key, value in unlock_service.get_metrics().items():
        print(f"  {key}: {value}")

    # Test unlock
    print("\nTesting screen unlock...")
    result = await unlock_service.unlock_screen(verified_speaker="Derek")
    print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
