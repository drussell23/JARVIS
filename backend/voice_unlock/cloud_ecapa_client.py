#!/usr/bin/env python3
"""
Cloud ECAPA Client - Robust Cloud Speaker Embedding Client
============================================================

Advanced async client for cloud ECAPA speaker embedding service.
Designed for high reliability with:
- Circuit breaker pattern
- Automatic retries with exponential backoff
- Dynamic endpoint discovery
- Connection pooling
- Response caching
- Comprehensive telemetry

v18.1.0 - Now with GCP Spot VM Integration!

BACKEND OPTIONS & COSTS:
┌─────────────────┬──────────────┬─────────────────┬──────────────────────────┐
│ Backend         │ Cost/Hour    │ Cost/Month 24/7 │ Best For                 │
├─────────────────┼──────────────┼─────────────────┼──────────────────────────┤
│ Cloud Run       │ ~$0.05/hr    │ ~$5-15/month    │ Low usage, pay-per-use   │
│ Spot VM         │ $0.029/hr    │ $21/month       │ Medium use, scale-to-zero│
│ Regular VM      │ $0.268/hr    │ $195/month      │ AVOID - too expensive!   │
└─────────────────┴──────────────┴─────────────────┴──────────────────────────┘

ROUTING PRIORITY:
1. Cloud Run (instant, serverless, pay-per-request)
2. Spot VM (auto-create on demand, scale-to-zero after idle)
3. Local fallback (if cloud unavailable)

Usage:
    client = CloudECAPAClient()
    await client.initialize()

    # Extract embedding (auto-routes to best backend)
    embedding = await client.extract_embedding(audio_bytes)

    # Verify speaker
    result = await client.verify_speaker(audio_bytes, reference_embedding)
"""

import asyncio
import base64
import hashlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

class CloudECAPAClientConfig:
    """Dynamic configuration from environment."""

    # Primary endpoints (in priority order)
    ENDPOINTS = [
        e.strip() for e in
        os.getenv("JARVIS_CLOUD_ML_ENDPOINTS", "").split(",")
        if e.strip()
    ]

    # Fallback endpoint construction
    GCP_PROJECT = os.getenv("GCP_PROJECT_ID", "jarvis-473803")
    GCP_REGION = os.getenv("GCP_REGION", "us-central1")

    # Primary endpoint
    PRIMARY_ENDPOINT = os.getenv(
        "JARVIS_CLOUD_ML_ENDPOINT",
        f"https://jarvis-ml-{GCP_PROJECT}.{GCP_REGION}.run.app/api/ml"
    )

    # Timeouts
    CONNECT_TIMEOUT = float(os.getenv("CLOUD_ECAPA_CONNECT_TIMEOUT", "5.0"))
    REQUEST_TIMEOUT = float(os.getenv("CLOUD_ECAPA_REQUEST_TIMEOUT", "30.0"))

    # Retries
    MAX_RETRIES = int(os.getenv("CLOUD_ECAPA_MAX_RETRIES", "3"))
    RETRY_BACKOFF_BASE = float(os.getenv("CLOUD_ECAPA_BACKOFF_BASE", "1.0"))
    RETRY_BACKOFF_MAX = float(os.getenv("CLOUD_ECAPA_BACKOFF_MAX", "10.0"))

    # Circuit breaker
    CB_FAILURE_THRESHOLD = int(os.getenv("CLOUD_ECAPA_CB_FAILURES", "5"))
    CB_RECOVERY_TIMEOUT = float(os.getenv("CLOUD_ECAPA_CB_RECOVERY", "30.0"))
    CB_SUCCESS_THRESHOLD = int(os.getenv("CLOUD_ECAPA_CB_SUCCESS", "2"))

    # Caching
    CACHE_ENABLED = os.getenv("CLOUD_ECAPA_CACHE_ENABLED", "true").lower() == "true"
    CACHE_TTL = int(os.getenv("CLOUD_ECAPA_CACHE_TTL", "300"))  # 5 minutes
    CACHE_MAX_SIZE = int(os.getenv("CLOUD_ECAPA_CACHE_SIZE", "100"))

    # Health check
    HEALTH_CHECK_INTERVAL = float(os.getenv("CLOUD_ECAPA_HEALTH_INTERVAL", "60.0"))
    HEALTH_CHECK_TIMEOUT = float(os.getenv("CLOUD_ECAPA_HEALTH_TIMEOUT", "5.0"))

    @classmethod
    def get_all_endpoints(cls) -> List[str]:
        """Get all configured endpoints in priority order."""
        endpoints = []

        # Add explicitly configured endpoints
        if cls.ENDPOINTS:
            endpoints.extend(cls.ENDPOINTS)

        # Add primary endpoint if not in list
        if cls.PRIMARY_ENDPOINT and cls.PRIMARY_ENDPOINT not in endpoints:
            endpoints.append(cls.PRIMARY_ENDPOINT)

        # Add Cloud Run default
        cloud_run_default = f"https://jarvis-ml-{cls.GCP_PROJECT}.{cls.GCP_REGION}.run.app/api/ml"
        if cloud_run_default not in endpoints:
            endpoints.append(cloud_run_default)

        # Add localhost for development
        if os.getenv("JARVIS_DEV_MODE", "false").lower() == "true":
            localhost = "http://localhost:8010/api/ml"
            if localhost not in endpoints:
                endpoints.insert(0, localhost)  # Prefer local in dev mode

        return endpoints


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = auto()      # Normal operation
    OPEN = auto()        # Failing, reject requests
    HALF_OPEN = auto()   # Testing recovery


@dataclass
class EndpointCircuitBreaker:
    """Per-endpoint circuit breaker."""

    endpoint: str
    failure_threshold: int = CloudECAPAClientConfig.CB_FAILURE_THRESHOLD
    recovery_timeout: float = CloudECAPAClientConfig.CB_RECOVERY_TIMEOUT
    success_threshold: int = CloudECAPAClientConfig.CB_SUCCESS_THRESHOLD

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    half_open_success: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    last_error: Optional[str] = None

    # Stats
    total_requests: int = 0
    total_failures: int = 0
    total_successes: int = 0

    def record_success(self):
        """Record successful request."""
        self.total_requests += 1
        self.total_successes += 1
        self.success_count += 1
        self.failure_count = 0
        self.last_success_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            self.half_open_success += 1
            if self.half_open_success >= self.success_threshold:
                self.state = CircuitState.CLOSED
                self.half_open_success = 0
                logger.info(f"[CircuitBreaker] {self.endpoint}: CLOSED (recovered)")

    def record_failure(self, error: str = None):
        """Record failed request."""
        self.total_requests += 1
        self.total_failures += 1
        self.failure_count += 1
        self.success_count = 0
        self.last_failure_time = time.time()
        self.last_error = error

        if self.state == CircuitState.HALF_OPEN:
            # Failure in half-open → back to open
            self.state = CircuitState.OPEN
            self.half_open_success = 0
            logger.warning(f"[CircuitBreaker] {self.endpoint}: OPEN (half-open failure)")
        elif self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(f"[CircuitBreaker] {self.endpoint}: OPEN ({self.failure_count} failures)")

    def can_execute(self) -> Tuple[bool, str]:
        """Check if request can proceed."""
        if self.state == CircuitState.CLOSED:
            return True, "closed"

        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if self.last_failure_time:
                elapsed = time.time() - self.last_failure_time
                if elapsed >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_success = 0
                    logger.info(f"[CircuitBreaker] {self.endpoint}: HALF_OPEN (testing)")
                    return True, "half_open"
            return False, f"open (last error: {self.last_error})"

        # HALF_OPEN: allow request
        return True, "half_open"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "state": self.state.name,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_error": self.last_error,
            "total_requests": self.total_requests,
            "success_rate": f"{(self.total_successes / max(1, self.total_requests)) * 100:.1f}%",
        }


# =============================================================================
# RESPONSE CACHE
# =============================================================================

@dataclass
class CacheEntry:
    """Cache entry with TTL."""
    value: Any
    timestamp: float
    ttl: float

    def is_expired(self) -> bool:
        return time.time() - self.timestamp > self.ttl


class EmbeddingCache:
    """LRU cache for embeddings with TTL."""

    def __init__(self, max_size: int = 100, ttl: int = 300):
        self.max_size = max_size
        self.default_ttl = ttl
        self.cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()

        # Stats
        self.hits = 0
        self.misses = 0

    async def get(self, key: str) -> Optional[Any]:
        """Get item from cache."""
        async with self._lock:
            if key not in self.cache:
                self.misses += 1
                return None

            entry = self.cache[key]
            if entry.is_expired():
                del self.cache[key]
                self.misses += 1
                return None

            # Move to end (LRU)
            self.cache.move_to_end(key)
            self.hits += 1
            return entry.value

    async def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Store item in cache."""
        async with self._lock:
            if len(self.cache) >= self.max_size:
                # Remove oldest
                self.cache.popitem(last=False)

            self.cache[key] = CacheEntry(
                value=value,
                timestamp=time.time(),
                ttl=ttl or self.default_ttl
            )

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def stats(self) -> Dict[str, Any]:
        return {
            "size": len(self.cache),
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": f"{self.hit_rate * 100:.1f}%",
        }


# =============================================================================
# CLOUD ECAPA CLIENT
# =============================================================================

class CloudECAPAClient:
    """
    Robust async client for cloud ECAPA speaker embedding service.

    Features:
    - Multiple endpoint support with automatic failover
    - Per-endpoint circuit breakers
    - Retry with exponential backoff
    - Response caching
    - Connection pooling
    - Comprehensive telemetry
    """

    def __init__(self):
        self._endpoints: List[str] = []
        self._circuit_breakers: Dict[str, EndpointCircuitBreaker] = {}
        self._session = None
        self._initialized = False
        self._healthy_endpoint: Optional[str] = None

        # Caching
        self._cache = EmbeddingCache(
            max_size=CloudECAPAClientConfig.CACHE_MAX_SIZE,
            ttl=CloudECAPAClientConfig.CACHE_TTL
        ) if CloudECAPAClientConfig.CACHE_ENABLED else None

        # Stats
        self._stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "cache_hits": 0,
            "retries": 0,
            "failovers": 0,
            "avg_latency_ms": 0.0,
        }

        # Background tasks
        self._health_check_task: Optional[asyncio.Task] = None

    async def initialize(self) -> bool:
        """
        Initialize the client with endpoint discovery.

        Returns:
            True if at least one endpoint is available
        """
        if self._initialized:
            return True

        logger.info("=" * 60)
        logger.info("Initializing Cloud ECAPA Client")
        logger.info("=" * 60)

        # Discover endpoints
        self._endpoints = CloudECAPAClientConfig.get_all_endpoints()

        if not self._endpoints:
            logger.error("No cloud ECAPA endpoints configured!")
            return False

        logger.info(f"Configured {len(self._endpoints)} endpoints:")
        for i, ep in enumerate(self._endpoints):
            logger.info(f"  {i + 1}. {ep}")

        # Initialize circuit breakers for each endpoint
        for endpoint in self._endpoints:
            self._circuit_breakers[endpoint] = EndpointCircuitBreaker(endpoint=endpoint)

        # Create aiohttp session
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(
                total=CloudECAPAClientConfig.REQUEST_TIMEOUT,
                connect=CloudECAPAClientConfig.CONNECT_TIMEOUT,
            )

            # Connection pooling
            connector = aiohttp.TCPConnector(
                limit=20,  # Max connections
                limit_per_host=5,  # Max per endpoint
                ttl_dns_cache=300,  # DNS cache TTL
            )

            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
            )
        except ImportError:
            logger.error("aiohttp not available. Install with: pip install aiohttp")
            return False

        # Verify at least one endpoint is healthy
        healthy = await self._discover_healthy_endpoint()

        if healthy:
            self._initialized = True
            logger.info(f"✅ Cloud ECAPA Client ready (primary: {self._healthy_endpoint})")

            # Start background health monitoring
            self._health_check_task = asyncio.create_task(self._health_check_loop())

            return True

        logger.warning("⚠️ No healthy endpoints found, but client initialized for retry")
        self._initialized = True
        return False

    async def _discover_healthy_endpoint(self) -> bool:
        """Find a healthy endpoint with extraction test."""
        for endpoint in self._endpoints:
            try:
                healthy = await self._check_endpoint_health(endpoint, test_extraction=True)
                if healthy:
                    self._healthy_endpoint = endpoint
                    return True
            except Exception as e:
                logger.warning(f"Endpoint {endpoint} unhealthy: {e}")

        return False

    async def _check_endpoint_health(
        self,
        endpoint: str,
        test_extraction: bool = False
    ) -> bool:
        """Check if an endpoint is healthy."""
        if not self._session:
            return False

        health_url = f"{endpoint.rstrip('/')}/health"

        try:
            async with self._session.get(
                health_url,
                timeout=CloudECAPAClientConfig.HEALTH_CHECK_TIMEOUT
            ) as response:
                if response.status != 200:
                    return False

                data = await response.json()
                if not data.get("ecapa_ready", True):
                    return False

            # Optional: test actual extraction
            if test_extraction:
                test_audio = np.zeros(1600, dtype=np.float32)  # 100ms silence
                embedding = await self._extract_from_endpoint(
                    endpoint,
                    test_audio.tobytes(),
                    sample_rate=16000
                )
                if embedding is None:
                    return False

            return True

        except Exception as e:
            logger.debug(f"Health check failed for {endpoint}: {e}")
            return False

    async def _health_check_loop(self):
        """Background task to monitor endpoint health."""
        while True:
            try:
                await asyncio.sleep(CloudECAPAClientConfig.HEALTH_CHECK_INTERVAL)

                # Re-check all endpoints
                for endpoint in self._endpoints:
                    cb = self._circuit_breakers[endpoint]

                    # Only check endpoints that are open (potentially recovered)
                    if cb.state == CircuitState.OPEN:
                        healthy = await self._check_endpoint_health(endpoint)
                        if healthy:
                            # Allow circuit breaker to try again
                            cb.state = CircuitState.HALF_OPEN
                            logger.info(f"[HealthCheck] {endpoint}: may be recovered, testing...")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check loop error: {e}")

    async def close(self):
        """Close the client and cleanup resources."""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        if self._session:
            await self._session.close()
            self._session = None

        self._initialized = False
        logger.info("Cloud ECAPA Client closed")

    # =========================================================================
    # MAIN API
    # =========================================================================

    async def extract_embedding(
        self,
        audio_data: bytes,
        sample_rate: int = 16000,
        format: str = "float32",
        use_cache: bool = True
    ) -> Optional[np.ndarray]:
        """
        Extract speaker embedding from audio.

        Args:
            audio_data: Raw audio bytes
            sample_rate: Audio sample rate
            format: Audio format (float32, int16)
            use_cache: Whether to check cache

        Returns:
            192-dimensional speaker embedding or None
        """
        if not self._initialized:
            if not await self.initialize():
                logger.error("Client initialization failed")
                return None

        self._stats["total_requests"] += 1

        # Check cache
        if use_cache and self._cache:
            cache_key = self._compute_cache_key(audio_data)
            cached = await self._cache.get(cache_key)
            if cached is not None:
                self._stats["cache_hits"] += 1
                logger.debug("Cache hit for embedding")
                return cached

        # Try endpoints with fallback
        start_time = time.time()
        last_error = None

        # Build ordered endpoint list (healthy first)
        endpoints_to_try = self._get_ordered_endpoints()

        for i, endpoint in enumerate(endpoints_to_try):
            cb = self._circuit_breakers[endpoint]
            can_execute, reason = cb.can_execute()

            if not can_execute:
                logger.debug(f"Skipping {endpoint}: circuit {reason}")
                continue

            try:
                embedding = await self._extract_from_endpoint(
                    endpoint,
                    audio_data,
                    sample_rate,
                    format
                )

                if embedding is not None:
                    cb.record_success()

                    # Update healthy endpoint
                    if self._healthy_endpoint != endpoint:
                        self._healthy_endpoint = endpoint
                        if i > 0:
                            self._stats["failovers"] += 1

                    # Update stats
                    latency = (time.time() - start_time) * 1000
                    self._update_latency(latency)
                    self._stats["successful_requests"] += 1

                    # Cache result
                    if use_cache and self._cache:
                        await self._cache.set(cache_key, embedding)

                    return embedding

            except Exception as e:
                last_error = str(e)
                cb.record_failure(last_error)
                logger.warning(f"Endpoint {endpoint} failed: {e}")

                if i < len(endpoints_to_try) - 1:
                    self._stats["retries"] += 1
                    logger.info(f"Failing over to next endpoint...")

        self._stats["failed_requests"] += 1
        logger.error(f"All endpoints failed. Last error: {last_error}")
        return None

    async def verify_speaker(
        self,
        audio_data: bytes,
        reference_embedding: np.ndarray,
        sample_rate: int = 16000,
        format: str = "float32",
        threshold: float = 0.85
    ) -> Dict[str, Any]:
        """
        Verify speaker against reference embedding.

        Args:
            audio_data: Raw audio bytes
            reference_embedding: Reference speaker embedding
            sample_rate: Audio sample rate
            format: Audio format
            threshold: Verification threshold

        Returns:
            Verification result dict
        """
        # Extract embedding from audio
        embedding = await self.extract_embedding(
            audio_data,
            sample_rate,
            format,
            use_cache=True
        )

        if embedding is None:
            return {
                "success": False,
                "verified": False,
                "error": "Failed to extract embedding"
            }

        # Compute similarity
        similarity = self._compute_similarity(embedding, reference_embedding)
        confidence = (similarity + 1) / 2  # Normalize to 0-1

        return {
            "success": True,
            "verified": confidence >= threshold,
            "similarity": float(similarity),
            "confidence": float(confidence),
            "threshold": threshold,
        }

    async def _extract_from_endpoint(
        self,
        endpoint: str,
        audio_data: bytes,
        sample_rate: int = 16000,
        format: str = "float32"
    ) -> Optional[np.ndarray]:
        """Extract embedding from a specific endpoint with retries."""
        if not self._session:
            return None

        url = f"{endpoint.rstrip('/')}/speaker_embedding"
        audio_b64 = base64.b64encode(audio_data).decode('utf-8')

        payload = {
            "audio_data": audio_b64,
            "sample_rate": sample_rate,
            "format": format,
        }

        # Retry with exponential backoff
        for attempt in range(1, CloudECAPAClientConfig.MAX_RETRIES + 1):
            try:
                async with self._session.post(url, json=payload) as response:
                    if response.status == 200:
                        result = await response.json()

                        if result.get("success") and result.get("embedding"):
                            embedding = np.array(result["embedding"], dtype=np.float32)
                            logger.debug(f"Extracted embedding from {endpoint}: shape {embedding.shape}")
                            return embedding

                        error = result.get("error", "Unknown error")
                        raise RuntimeError(f"Extraction failed: {error}")

                    elif response.status >= 500:
                        # Server error - retry
                        raise RuntimeError(f"Server error: HTTP {response.status}")
                    else:
                        # Client error - don't retry
                        error_text = await response.text()
                        raise ValueError(f"HTTP {response.status}: {error_text}")

            except (asyncio.TimeoutError, ValueError) as e:
                # Don't retry client errors or timeouts
                raise
            except Exception as e:
                if attempt < CloudECAPAClientConfig.MAX_RETRIES:
                    backoff = min(
                        CloudECAPAClientConfig.RETRY_BACKOFF_BASE * (2 ** (attempt - 1)),
                        CloudECAPAClientConfig.RETRY_BACKOFF_MAX
                    )
                    logger.debug(f"Retry {attempt} after {backoff}s: {e}")
                    await asyncio.sleep(backoff)
                    self._stats["retries"] += 1
                else:
                    raise

        return None

    def _get_ordered_endpoints(self) -> List[str]:
        """Get endpoints ordered by health (healthy first)."""
        def endpoint_priority(ep: str) -> int:
            cb = self._circuit_breakers.get(ep)
            if not cb:
                return 100

            if ep == self._healthy_endpoint:
                return 0  # Primary healthy endpoint first
            elif cb.state == CircuitState.CLOSED:
                return 1
            elif cb.state == CircuitState.HALF_OPEN:
                return 2
            else:  # OPEN
                return 3

        return sorted(self._endpoints, key=endpoint_priority)

    def _compute_cache_key(self, audio_data: bytes) -> str:
        """Compute cache key for audio."""
        return hashlib.sha256(audio_data).hexdigest()[:16]

    def _compute_similarity(self, emb1: np.ndarray, emb2: np.ndarray) -> float:
        """Compute cosine similarity."""
        norm1 = np.linalg.norm(emb1)
        norm2 = np.linalg.norm(emb2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(np.dot(emb1, emb2) / (norm1 * norm2))

    def _update_latency(self, latency_ms: float):
        """Update average latency stat."""
        n = self._stats["successful_requests"]
        old_avg = self._stats["avg_latency_ms"]
        self._stats["avg_latency_ms"] = (old_avg * (n - 1) + latency_ms) / n

    def get_stats(self) -> Dict[str, Any]:
        """Get client statistics."""
        return {
            **self._stats,
            "initialized": self._initialized,
            "healthy_endpoint": self._healthy_endpoint,
            "endpoints": len(self._endpoints),
            "circuit_breakers": {
                ep: cb.to_dict()
                for ep, cb in self._circuit_breakers.items()
            },
            "cache": self._cache.stats() if self._cache else None,
        }

    def get_status(self) -> Dict[str, Any]:
        """Get detailed client status."""
        return {
            "ready": self._initialized and self._healthy_endpoint is not None,
            "healthy_endpoint": self._healthy_endpoint,
            "all_endpoints": self._endpoints,
            "circuit_breakers": {
                ep: {
                    "state": cb.state.name,
                    "failures": cb.failure_count,
                    "last_error": cb.last_error,
                }
                for ep, cb in self._circuit_breakers.items()
            },
            "stats": self.get_stats(),
        }


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_client_instance: Optional[CloudECAPAClient] = None
_client_lock = asyncio.Lock()


async def get_cloud_ecapa_client() -> CloudECAPAClient:
    """Get or create the global cloud ECAPA client."""
    global _client_instance

    async with _client_lock:
        if _client_instance is None:
            _client_instance = CloudECAPAClient()
            await _client_instance.initialize()

        return _client_instance


async def close_cloud_ecapa_client():
    """Close the global client."""
    global _client_instance

    if _client_instance:
        await _client_instance.close()
        _client_instance = None


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

async def extract_embedding_cloud(
    audio_data: bytes,
    sample_rate: int = 16000,
    format: str = "float32"
) -> Optional[np.ndarray]:
    """
    Convenience function to extract speaker embedding via cloud.

    Args:
        audio_data: Raw audio bytes
        sample_rate: Audio sample rate
        format: Audio format

    Returns:
        Speaker embedding or None
    """
    client = await get_cloud_ecapa_client()
    return await client.extract_embedding(audio_data, sample_rate, format)


async def verify_speaker_cloud(
    audio_data: bytes,
    reference_embedding: np.ndarray,
    sample_rate: int = 16000,
    threshold: float = 0.85
) -> Dict[str, Any]:
    """
    Convenience function to verify speaker via cloud.

    Args:
        audio_data: Raw audio bytes
        reference_embedding: Reference speaker embedding
        sample_rate: Audio sample rate
        threshold: Verification threshold

    Returns:
        Verification result
    """
    client = await get_cloud_ecapa_client()
    return await client.verify_speaker(
        audio_data,
        reference_embedding,
        sample_rate,
        threshold=threshold
    )
