"""
Centralized Secret Management for JARVIS
=========================================

A robust, async-capable, multi-backend secret management system with:
  - Lazy imports (no top-level failures)
  - Async/await support for non-blocking operations
  - Intelligent caching with TTL
  - Circuit breaker pattern for failed backends
  - Exponential backoff retry logic
  - Parallel fallback attempts
  - Dynamic backend health monitoring

Supported Backends (priority order):
  1. GCP Secret Manager (production)
  2. macOS Keychain (local development)
  3. Environment variables (CI/CD fallback)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import wraps
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)
import logging

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Circuit Breaker Pattern Implementation
# =============================================================================


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = auto()     # Normal operation
    OPEN = auto()       # Failures exceeded threshold, blocking calls
    HALF_OPEN = auto()  # Testing if service recovered


@dataclass
class CircuitBreaker:
    """
    Circuit breaker to prevent repeated calls to failing backends.

    When failures exceed threshold, circuit opens and blocks calls
    for a cooldown period before testing again.
    """
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 60.0  # seconds
    half_open_max_calls: int = 1

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _half_open_calls: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def state(self) -> CircuitState:
        """Get current state, transitioning from OPEN to HALF_OPEN if timeout passed."""
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    logger.info(f"Circuit '{self.name}' transitioning to HALF_OPEN")
            return self._state

    def can_execute(self) -> bool:
        """Check if a call can be made."""
        state = self.state
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.OPEN:
            return False
        # HALF_OPEN: allow limited calls
        with self._lock:
            if self._half_open_calls < self.half_open_max_calls:
                self._half_open_calls += 1
                return True
            return False

    def record_success(self) -> None:
        """Record a successful call."""
        with self._lock:
            self._failure_count = 0
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                logger.info(f"Circuit '{self.name}' recovered, now CLOSED")

    def record_failure(self) -> None:
        """Record a failed call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning(f"Circuit '{self.name}' failed in HALF_OPEN, reopening")
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    f"Circuit '{self.name}' opened after {self._failure_count} failures"
                )

    def reset(self) -> None:
        """Manually reset the circuit breaker."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_calls = 0


# =============================================================================
# Intelligent Cache with TTL
# =============================================================================


@dataclass
class CacheEntry(Generic[T]):
    """Cache entry with expiration tracking."""
    value: T
    created_at: float
    ttl: float

    @property
    def is_expired(self) -> bool:
        """Check if cache entry has expired."""
        return time.time() - self.created_at > self.ttl


class SecretCache:
    """
    Thread-safe cache for secrets with:
      - Per-entry TTL
      - Automatic cleanup
      - Memory-bounded size
    """

    def __init__(
        self,
        default_ttl: float = 300.0,  # 5 minutes default
        max_size: int = 100,
        cleanup_interval: float = 60.0,
    ):
        self._cache: Dict[str, CacheEntry[str]] = {}
        self._lock = threading.RLock()
        self._default_ttl = default_ttl
        self._max_size = max_size
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.time()

    def get(self, key: str) -> Optional[str]:
        """Get value from cache if exists and not expired."""
        self._maybe_cleanup()

        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.is_expired:
                del self._cache[key]
                return None
            return entry.value

    def set(self, key: str, value: str, ttl: Optional[float] = None) -> None:
        """Set value in cache with optional custom TTL."""
        with self._lock:
            # Evict oldest if at capacity
            if len(self._cache) >= self._max_size:
                self._evict_oldest()

            self._cache[key] = CacheEntry(
                value=value,
                created_at=time.time(),
                ttl=ttl if ttl is not None else self._default_ttl,
            )

    def invalidate(self, key: str) -> None:
        """Remove a specific key from cache."""
        with self._lock:
            self._cache.pop(key, None)

    def clear(self) -> None:
        """Clear all cached values."""
        with self._lock:
            self._cache.clear()

    def _evict_oldest(self) -> None:
        """Evict the oldest cache entry."""
        if not self._cache:
            return
        oldest_key = min(
            self._cache.keys(),
            key=lambda k: self._cache[k].created_at
        )
        del self._cache[oldest_key]

    def _maybe_cleanup(self) -> None:
        """Periodically clean up expired entries."""
        if time.time() - self._last_cleanup < self._cleanup_interval:
            return

        with self._lock:
            expired_keys = [
                k for k, v in self._cache.items() if v.is_expired
            ]
            for key in expired_keys:
                del self._cache[key]
            self._last_cleanup = time.time()


# =============================================================================
# Backend Protocol & Implementations
# =============================================================================


class SecretBackend(ABC):
    """Abstract base class for secret backends."""

    name: str = "unknown"
    priority: int = 0

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend is available."""
        pass

    @abstractmethod
    def get_secret(self, secret_id: str, version: str = "latest") -> Optional[str]:
        """Retrieve a secret synchronously."""
        pass

    @abstractmethod
    async def get_secret_async(self, secret_id: str, version: str = "latest") -> Optional[str]:
        """Retrieve a secret asynchronously."""
        pass

    def set_secret(self, secret_id: str, value: str) -> bool:
        """Set/update a secret (if supported)."""
        return False

    async def set_secret_async(self, secret_id: str, value: str) -> bool:
        """Set/update a secret asynchronously (if supported)."""
        return await asyncio.get_event_loop().run_in_executor(
            None, self.set_secret, secret_id, value
        )


class GCPSecretManagerBackend(SecretBackend):
    """Google Cloud Secret Manager backend."""

    name = "gcp_secret_manager"
    priority = 100  # Highest priority

    def __init__(self, project_id: Optional[str] = None):
        self._project_id = project_id or os.getenv("GCP_PROJECT_ID", "jarvis-473803")
        self._client = None
        self._initialized = False
        self._init_lock = threading.Lock()

    def _ensure_initialized(self) -> bool:
        """Lazy initialization of GCP client."""
        if self._initialized:
            return self._client is not None

        with self._init_lock:
            if self._initialized:
                return self._client is not None

            try:
                from google.cloud import secretmanager
                self._client = secretmanager.SecretManagerServiceClient()
                self._initialized = True
                logger.info("✅ GCP Secret Manager client initialized")
                return True
            except ImportError as e:
                logger.debug(f"GCP Secret Manager package not installed: {e}")
                self._initialized = True
                return False
            except Exception as e:
                logger.debug(f"GCP Secret Manager not available: {e}")
                self._initialized = True
                return False

    def is_available(self) -> bool:
        """Check if GCP Secret Manager is available."""
        return self._ensure_initialized()

    def get_secret(self, secret_id: str, version: str = "latest") -> Optional[str]:
        """Retrieve secret from GCP Secret Manager."""
        if not self._ensure_initialized() or self._client is None:
            return None

        try:
            name = f"projects/{self._project_id}/secrets/{secret_id}/versions/{version}"
            response = self._client.access_secret_version(request={"name": name})
            return response.payload.data.decode("UTF-8")
        except Exception as e:
            logger.debug(f"Failed to get '{secret_id}' from GCP: {e}")
            return None

    async def get_secret_async(self, secret_id: str, version: str = "latest") -> Optional[str]:
        """Retrieve secret asynchronously using thread pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_secret, secret_id, version)

    def set_secret(self, secret_id: str, value: str) -> bool:
        """Add new version to existing secret."""
        if not self._ensure_initialized() or self._client is None:
            return False

        try:
            parent = f"projects/{self._project_id}/secrets/{secret_id}"
            payload = {"data": value.encode("UTF-8")}
            self._client.add_secret_version(request={"parent": parent, "payload": payload})
            return True
        except Exception as e:
            logger.error(f"Failed to set '{secret_id}' in GCP: {e}")
            return False

    def list_secrets(self) -> List[str]:
        """List all secrets in GCP Secret Manager."""
        if not self._ensure_initialized() or self._client is None:
            return []

        try:
            parent = f"projects/{self._project_id}"
            secrets = self._client.list_secrets(request={"parent": parent})
            return [secret.name.split("/")[-1] for secret in secrets]
        except Exception as e:
            logger.error(f"Failed to list secrets: {e}")
            return []


class KeychainBackend(SecretBackend):
    """macOS Keychain backend for local development."""

    name = "macos_keychain"
    priority = 50

    def __init__(self, service_name: str = "JARVIS"):
        self._service_name = service_name
        self._keyring = None
        self._initialized = False
        self._init_lock = threading.Lock()

    def _ensure_initialized(self) -> bool:
        """Lazy initialization of keyring."""
        if self._initialized:
            return self._keyring is not None

        with self._init_lock:
            if self._initialized:
                return self._keyring is not None

            try:
                import keyring
                self._keyring = keyring
                self._initialized = True
                return True
            except ImportError:
                logger.debug("keyring module not available")
                self._initialized = True
                return False

    def is_available(self) -> bool:
        """Check if keychain is available."""
        return self._ensure_initialized()

    def get_secret(self, secret_id: str, version: str = "latest") -> Optional[str]:
        """Retrieve secret from macOS Keychain."""
        if not self._ensure_initialized() or self._keyring is None:
            return None

        try:
            return self._keyring.get_password(self._service_name, secret_id)
        except Exception as e:
            logger.debug(f"Failed to get '{secret_id}' from Keychain: {e}")
            return None

    async def get_secret_async(self, secret_id: str, version: str = "latest") -> Optional[str]:
        """Retrieve secret asynchronously."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_secret, secret_id, version)

    def set_secret(self, secret_id: str, value: str) -> bool:
        """Store secret in macOS Keychain."""
        if not self._ensure_initialized() or self._keyring is None:
            return False

        try:
            self._keyring.set_password(self._service_name, secret_id, value)
            return True
        except Exception as e:
            logger.error(f"Failed to set '{secret_id}' in Keychain: {e}")
            return False


class EnvironmentBackend(SecretBackend):
    """Environment variable backend for CI/CD."""

    name = "environment"
    priority = 10  # Lowest priority

    def is_available(self) -> bool:
        """Environment variables are always available."""
        return True

    def get_secret(self, secret_id: str, version: str = "latest") -> Optional[str]:
        """Retrieve secret from environment variable."""
        # Convert secret-id to SECRET_ID format
        env_var = secret_id.upper().replace("-", "_")
        return os.getenv(env_var)

    async def get_secret_async(self, secret_id: str, version: str = "latest") -> Optional[str]:
        """Retrieve secret from environment variable (non-blocking)."""
        return self.get_secret(secret_id, version)

    def set_secret(self, secret_id: str, value: str) -> bool:
        """Set environment variable (session-only)."""
        env_var = secret_id.upper().replace("-", "_")
        os.environ[env_var] = value
        return True


# =============================================================================
# Main Secret Manager
# =============================================================================


class SecretManager:
    """
    Centralized secret management for JARVIS with:
      - Multi-backend support with automatic fallback
      - Async/await for non-blocking operations
      - Intelligent caching with TTL
      - Circuit breaker for failed backends
      - Retry with exponential backoff
    """

    # Common secret ID mappings
    # v117.0: Added CLOUDSQL_* aliases for unified credential access
    SECRET_ALIASES: Dict[str, List[str]] = {
        "anthropic-api-key": ["ANTHROPIC_API_KEY", "anthropic_api_key"],
        "jarvis-db-password": [
            "JARVIS_DB_PASSWORD",
            "jarvis_db_password",
            "DB_PASSWORD",
            "CLOUDSQL_DB_PASSWORD",  # v117.0: Unified with startup_barrier
            "CLOUD_SQL_PASSWORD",
        ],
        "jarvis-db-user": [
            "JARVIS_DB_USER",
            "jarvis_db_user",
            "DB_USER",
            "CLOUDSQL_DB_USER",  # v117.0: Unified with startup_barrier
            "CLOUD_SQL_USER",
        ],
        "picovoice-access-key": ["PICOVOICE_ACCESS_KEY", "picovoice_access_key"],
        "openai-api-key": ["OPENAI_API_KEY", "openai_api_key"],
    }

    def __init__(
        self,
        project_id: Optional[str] = None,
        cache_ttl: float = 300.0,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
    ):
        """
        Initialize SecretManager.

        Args:
            project_id: GCP project ID (defaults to env var or jarvis-473803)
            cache_ttl: Cache TTL in seconds (default: 5 minutes)
            max_retries: Maximum retry attempts per backend
            retry_base_delay: Base delay for exponential backoff
        """
        self._project_id = project_id or os.getenv("GCP_PROJECT_ID", "jarvis-473803")
        self._cache = SecretCache(default_ttl=cache_ttl)
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay

        # Initialize backends (lazy - actual clients initialized on first use)
        self._backends: List[SecretBackend] = [
            GCPSecretManagerBackend(self._project_id),
            KeychainBackend(),
            EnvironmentBackend(),
        ]

        # Circuit breakers per backend
        self._circuit_breakers: Dict[str, CircuitBreaker] = {
            backend.name: CircuitBreaker(name=backend.name)
            for backend in self._backends
        }

        # Thread pool for async operations
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="secret_mgr")

        logger.info(
            f"SecretManager initialized with {len(self._backends)} backends "
            f"(cache_ttl={cache_ttl}s, max_retries={max_retries})"
        )

    def _normalize_secret_id(self, secret_id: str) -> str:
        """Normalize secret ID to canonical form."""
        # Check if this is an alias
        for canonical, aliases in self.SECRET_ALIASES.items():
            if secret_id in aliases or secret_id == canonical:
                return canonical
        return secret_id

    def _get_cache_key(self, secret_id: str, version: str) -> str:
        """Generate cache key for a secret."""
        return f"{secret_id}:{version}"

    def _retry_with_backoff(
        self,
        func: Callable[[], Optional[T]],
        backend_name: str,
    ) -> Optional[T]:
        """Execute function with exponential backoff retry."""
        circuit = self._circuit_breakers.get(backend_name)

        for attempt in range(self._max_retries):
            if circuit and not circuit.can_execute():
                logger.debug(f"Circuit open for {backend_name}, skipping")
                return None

            try:
                result = func()
                if result is not None:
                    if circuit:
                        circuit.record_success()
                    return result
                # None result is not a failure, just not found
                return None
            except Exception as e:
                if circuit:
                    circuit.record_failure()

                if attempt < self._max_retries - 1:
                    delay = self._retry_base_delay * (2 ** attempt)
                    logger.debug(
                        f"Retry {attempt + 1}/{self._max_retries} for {backend_name} "
                        f"after {delay}s: {e}"
                    )
                    time.sleep(delay)
                else:
                    logger.debug(f"All retries exhausted for {backend_name}: {e}")

        return None

    async def _retry_with_backoff_async(
        self,
        func: Callable[[], Coroutine[Any, Any, Optional[T]]],
        backend_name: str,
    ) -> Optional[T]:
        """Execute async function with exponential backoff retry."""
        circuit = self._circuit_breakers.get(backend_name)

        for attempt in range(self._max_retries):
            if circuit and not circuit.can_execute():
                logger.debug(f"Circuit open for {backend_name}, skipping")
                return None

            try:
                result = await func()
                if result is not None:
                    if circuit:
                        circuit.record_success()
                    return result
                return None
            except Exception as e:
                if circuit:
                    circuit.record_failure()

                if attempt < self._max_retries - 1:
                    delay = self._retry_base_delay * (2 ** attempt)
                    logger.debug(
                        f"Retry {attempt + 1}/{self._max_retries} for {backend_name} "
                        f"after {delay}s: {e}"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.debug(f"All retries exhausted for {backend_name}: {e}")

        return None

    def get_secret(
        self,
        secret_id: str,
        version: str = "latest",
        skip_cache: bool = False,
    ) -> Optional[str]:
        """
        Retrieve secret with automatic fallback chain.

        Args:
            secret_id: Secret identifier (e.g., 'anthropic-api-key')
            version: Secret version (default: 'latest')
            skip_cache: Bypass cache for fresh retrieval

        Returns:
            Secret value or None if not found in any backend
        """
        normalized_id = self._normalize_secret_id(secret_id)
        cache_key = self._get_cache_key(normalized_id, version)

        # Check cache first
        if not skip_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug(f"Cache hit for '{normalized_id}'")
                return cached

        # Try backends in priority order
        for backend in sorted(self._backends, key=lambda b: -b.priority):
            if not backend.is_available():
                continue

            result = self._retry_with_backoff(
                lambda b=backend: b.get_secret(normalized_id, version),
                backend.name,
            )

            if result is not None:
                self._cache.set(cache_key, result)
                logger.info(f"✅ Retrieved '{normalized_id}' from {backend.name}")
                return result

        logger.warning(f"❌ Secret '{normalized_id}' not found in any backend")
        return None

    async def get_secret_async(
        self,
        secret_id: str,
        version: str = "latest",
        skip_cache: bool = False,
    ) -> Optional[str]:
        """
        Retrieve secret asynchronously with automatic fallback.

        Args:
            secret_id: Secret identifier
            version: Secret version
            skip_cache: Bypass cache

        Returns:
            Secret value or None
        """
        normalized_id = self._normalize_secret_id(secret_id)
        cache_key = self._get_cache_key(normalized_id, version)

        # Check cache first
        if not skip_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug(f"Cache hit for '{normalized_id}'")
                return cached

        # Try backends in priority order
        for backend in sorted(self._backends, key=lambda b: -b.priority):
            if not backend.is_available():
                continue

            result = await self._retry_with_backoff_async(
                lambda b=backend: b.get_secret_async(normalized_id, version),
                backend.name,
            )

            if result is not None:
                self._cache.set(cache_key, result)
                logger.info(f"✅ Retrieved '{normalized_id}' from {backend.name}")
                return result

        logger.warning(f"❌ Secret '{normalized_id}' not found in any backend")
        return None

    async def get_secrets_parallel(
        self,
        secret_ids: List[str],
        version: str = "latest",
    ) -> Dict[str, Optional[str]]:
        """
        Retrieve multiple secrets in parallel.

        Args:
            secret_ids: List of secret identifiers
            version: Secret version

        Returns:
            Dict mapping secret_id to value (or None if not found)
        """
        tasks = [
            self.get_secret_async(secret_id, version)
            for secret_id in secret_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        return {
            secret_id: result if not isinstance(result, Exception) else None
            for secret_id, result in zip(secret_ids, results)
        }

    def rotate_secret(self, secret_id: str, new_value: str) -> bool:
        """
        Rotate (add new version) to a secret.

        Args:
            secret_id: Secret identifier
            new_value: New secret value

        Returns:
            True if successful
        """
        normalized_id = self._normalize_secret_id(secret_id)

        for backend in sorted(self._backends, key=lambda b: -b.priority):
            if not backend.is_available():
                continue

            if backend.set_secret(normalized_id, new_value):
                # Invalidate cache
                self._cache.invalidate(self._get_cache_key(normalized_id, "latest"))
                logger.info(f"✅ Rotated '{normalized_id}' in {backend.name}")
                return True

        logger.error(f"❌ Failed to rotate '{normalized_id}'")
        return False

    async def rotate_secret_async(self, secret_id: str, new_value: str) -> bool:
        """Rotate secret asynchronously."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, self.rotate_secret, secret_id, new_value
        )

    def list_secrets(self) -> List[str]:
        """List all available secrets from GCP."""
        for backend in self._backends:
            if isinstance(backend, GCPSecretManagerBackend) and backend.is_available():
                return backend.list_secrets()
        return []

    def get_backend_health(self) -> Dict[str, Dict[str, Any]]:
        """Get health status of all backends."""
        return {
            backend.name: {
                "available": backend.is_available(),
                "priority": backend.priority,
                "circuit_state": self._circuit_breakers[backend.name].state.name,
            }
            for backend in self._backends
        }

    def reset_circuit_breakers(self) -> None:
        """Reset all circuit breakers to closed state."""
        for circuit in self._circuit_breakers.values():
            circuit.reset()
        logger.info("All circuit breakers reset")

    def clear_cache(self) -> None:
        """Clear the secret cache."""
        self._cache.clear()
        logger.info("Secret cache cleared")

    # Convenience methods for common secrets
    def get_anthropic_key(self) -> Optional[str]:
        """Get Anthropic API key."""
        return self.get_secret("anthropic-api-key")

    async def get_anthropic_key_async(self) -> Optional[str]:
        """Get Anthropic API key asynchronously."""
        return await self.get_secret_async("anthropic-api-key")

    def get_db_password(self) -> Optional[str]:
        """Get database password."""
        return self.get_secret("jarvis-db-password")

    async def get_db_password_async(self) -> Optional[str]:
        """Get database password asynchronously."""
        return await self.get_secret_async("jarvis-db-password")

    def get_picovoice_key(self) -> Optional[str]:
        """Get Picovoice access key."""
        return self.get_secret("picovoice-access-key")

    async def get_picovoice_key_async(self) -> Optional[str]:
        """Get Picovoice access key asynchronously."""
        return await self.get_secret_async("picovoice-access-key")


# =============================================================================
# Global Singleton & Convenience Functions
# =============================================================================

_secret_manager: Optional[SecretManager] = None
_secret_manager_lock = threading.Lock()


def get_secret_manager() -> SecretManager:
    """
    Get or create global SecretManager instance (thread-safe singleton).

    Returns:
        SecretManager instance
    """
    global _secret_manager

    if _secret_manager is None:
        with _secret_manager_lock:
            if _secret_manager is None:
                _secret_manager = SecretManager()

    return _secret_manager


def get_secret(secret_id: str, version: str = "latest") -> Optional[str]:
    """Quick access to any secret."""
    return get_secret_manager().get_secret(secret_id, version)


async def get_secret_async(secret_id: str, version: str = "latest") -> Optional[str]:
    """Quick async access to any secret."""
    return await get_secret_manager().get_secret_async(secret_id, version)


def get_anthropic_key() -> Optional[str]:
    """Quick access to Anthropic API key."""
    return get_secret_manager().get_anthropic_key()


async def get_anthropic_key_async() -> Optional[str]:
    """Quick async access to Anthropic API key."""
    return await get_secret_manager().get_anthropic_key_async()


def get_db_password() -> Optional[str]:
    """Quick access to database password."""
    return get_secret_manager().get_db_password()


async def get_db_password_async() -> Optional[str]:
    """Quick async access to database password."""
    return await get_secret_manager().get_db_password_async()


def get_db_user() -> str:
    """
    v117.0: Quick access to database user.

    Returns the database username from secrets or environment,
    with 'jarvis' as the fallback default.
    """
    user = get_secret_manager().get_secret("jarvis-db-user")
    return user if user else "jarvis"


async def get_db_user_async() -> str:
    """
    v117.0: Quick async access to database user.

    Returns the database username from secrets or environment,
    with 'jarvis' as the fallback default.
    """
    user = await get_secret_manager().get_secret_async("jarvis-db-user")
    return user if user else "jarvis"


def get_picovoice_key() -> Optional[str]:
    """Quick access to Picovoice access key."""
    return get_secret_manager().get_picovoice_key()


async def get_picovoice_key_async() -> Optional[str]:
    """Quick async access to Picovoice access key."""
    return await get_secret_manager().get_picovoice_key_async()


# =============================================================================
# CLI/Test Entrypoint
# =============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )

    print("🔐 JARVIS Secret Manager Test\n")
    print("=" * 60)

    mgr = get_secret_manager()

    # Show backend health
    print("\n📊 Backend Health:")
    for name, health in mgr.get_backend_health().items():
        status = "✅" if health["available"] else "❌"
        print(f"  {status} {name}: priority={health['priority']}, circuit={health['circuit_state']}")

    # List secrets
    print("\n📋 Available Secrets (from GCP):")
    secrets = mgr.list_secrets()
    if secrets:
        for secret in secrets:
            print(f"  - {secret}")
    else:
        print("  (No GCP access or no secrets found)")

    # Test secret retrieval
    print("\n🔍 Testing Secret Retrieval:")

    test_secrets = [
        ("anthropic-api-key", "Anthropic API Key"),
        ("jarvis-db-password", "DB Password"),
        ("picovoice-access-key", "Picovoice Key"),
    ]

    for secret_id, display_name in test_secrets:
        value = mgr.get_secret(secret_id)
        if value:
            masked = value[:10] + "..." + value[-5:] if len(value) > 20 else "****"
            print(f"  ✅ {display_name}: {masked}")
        else:
            print(f"  ❌ {display_name}: Not found")

    # Test async retrieval
    print("\n⚡ Testing Async Parallel Retrieval:")

    async def test_parallel():
        results = await mgr.get_secrets_parallel([s[0] for s in test_secrets])
        for secret_id, value in results.items():
            status = "✅" if value else "❌"
            print(f"  {status} {secret_id}")

    asyncio.run(test_parallel())

    print("\n" + "=" * 60)
    print("✅ Secret Manager test complete!")
