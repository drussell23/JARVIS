"""
JARVIS Prime Client - Cognitive Mind Integration.
==================================================

v84.0 - Advanced Trinity Integration with Intelligent Routing

Provides robust communication with JARVIS Prime (the cognitive mind
of the Trinity architecture).

Features:
- Intelligent service discovery (heartbeat file + HTTP probing)
- OpenAI-compatible API format support
- Circuit breaker with adaptive thresholds
- Connection pooling with health monitoring
- Hot-reload model swapping
- LLM inference streaming with backpressure
- Cognitive task delegation
- Latency-based routing
- Dead letter queue with automatic retry

API Compatibility:
    OpenAI Format (J-Prime Server):
        POST /v1/chat/completions    - Chat completions
        POST /v1/completions         - Text completions
        GET  /v1/models              - List models
        POST /v1/embeddings          - Embeddings

    Custom Format (Legacy):
        POST /api/inference          - Run inference
        GET  /api/health             - Health check

Author: JARVIS Trinity v84.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)

# =============================================================================
# v88.0: ULTRA COORDINATOR INTEGRATION
# =============================================================================

# v88.0: Module-level ultra coordinator for protection
_ultra_coordinator: Optional[Any] = None
_ultra_coord_lock: Optional[asyncio.Lock] = None


async def _get_ultra_coordinator() -> Optional[Any]:
    """v88.0: Get ultra coordinator with lazy initialization."""
    global _ultra_coordinator, _ultra_coord_lock

    # Skip if disabled
    if os.getenv("JARVIS_ENABLE_ULTRA_COORD", "true").lower() not in ("true", "1", "yes"):
        return None

    if _ultra_coordinator is not None:
        return _ultra_coordinator

    # Lazy init lock
    if _ultra_coord_lock is None:
        _ultra_coord_lock = asyncio.Lock()

    async with _ultra_coord_lock:
        if _ultra_coordinator is not None:
            return _ultra_coordinator

        try:
            from backend.core.trinity_integrator import get_ultra_coordinator
            _ultra_coordinator = await get_ultra_coordinator()
            logger.info("[JARVISPrime] v88.0 Ultra coordinator initialized")
            return _ultra_coordinator
        except Exception as e:
            logger.debug(f"[JARVISPrime] v88.0 Ultra coordinator not available: {e}")
            return None

# Import base client components
try:
    from backend.clients.trinity_base_client import (
        TrinityBaseClient,
        ClientConfig,
        CircuitBreakerConfig,
        RetryConfig,
    )
except ImportError:
    from trinity_base_client import (
        TrinityBaseClient,
        ClientConfig,
        CircuitBreakerConfig,
        RetryConfig,
    )


# =============================================================================
# v84.0: ADVANCED ENVIRONMENT HELPERS
# =============================================================================

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")

def _env_list(key: str, default: List[str], separator: str = ",") -> List[str]:
    """Parse comma-separated environment variable."""
    value = os.getenv(key)
    if value:
        return [v.strip() for v in value.split(separator) if v.strip()]
    return default


# =============================================================================
# v84.0: INTELLIGENT SERVICE DISCOVERY
# =============================================================================

class ServiceDiscoveryMethod(Enum):
    """Methods for discovering J-Prime service."""
    HEARTBEAT_FILE = auto()  # Read from ~/.jarvis/trinity/components/jarvis_prime.json
    HTTP_PROBE = auto()       # Probe known ports
    ENVIRONMENT = auto()      # From JARVIS_PRIME_URL
    MULTICAST = auto()        # Future: mDNS/Bonjour


@dataclass
class DiscoveredService:
    """A discovered J-Prime service instance."""
    url: str
    port: int
    method: ServiceDiscoveryMethod
    model_loaded: bool = False
    model_name: Optional[str] = None
    latency_ms: float = float('inf')
    last_seen: float = field(default_factory=time.time)
    healthy: bool = True
    api_format: str = "openai"  # "openai" or "custom"

    def __hash__(self):
        return hash(self.url)


class IntelligentServiceDiscovery:
    """
    v84.0 + v134.0: Intelligent service discovery for J-Prime with auto-recovery.

    Features:
    - Multiple discovery methods with fallback
    - Health-based service selection
    - Latency-aware routing
    - Automatic port detection
    - API format detection (OpenAI vs custom)
    - v134.0: Automatic service startup trigger when offline
    - v134.0: Cross-repo coordination for Trinity components
    """

    # Known ports to probe (ordered by preference)
    PROBE_PORTS: List[int] = [
        int(os.getenv("JARVIS_PRIME_PORT", "8000")),  # Default/configured
        8000,  # Standard J-Prime port
        8001,  # Alternate
        8002,  # Legacy
        11434, # Ollama compatibility
    ]

    # Health check endpoints (ordered by preference)
    HEALTH_ENDPOINTS: List[str] = [
        "/health",
        "/v1/models",
        "/api/health",
    ]

    def __init__(self):
        self._discovered_services: Dict[str, DiscoveredService] = {}
        self._primary_service: Optional[DiscoveredService] = None
        self._discovery_lock = asyncio.Lock()
        self._last_discovery_time: float = 0.0
        self._discovery_interval: float = float(os.getenv(
            "JARVIS_PRIME_DISCOVERY_INTERVAL", "30.0"
        ))

        # Trinity directory for heartbeat files
        self._trinity_dir = Path(os.getenv(
            "TRINITY_DIR",
            str(Path.home() / ".jarvis" / "trinity")
        ))

        # v134.0: Auto-recovery settings
        self._auto_recovery_enabled = os.getenv(
            "JARVIS_PRIME_AUTO_RECOVERY", "true"
        ).lower() in ("true", "1", "yes")
        self._last_recovery_attempt: float = 0.0
        self._recovery_cooldown: float = float(os.getenv(
            "JARVIS_PRIME_RECOVERY_COOLDOWN", "60.0"  # Don't retry recovery within 60s
        ))
        self._consecutive_failures: int = 0
        self._max_consecutive_failures: int = int(os.getenv(
            "JARVIS_PRIME_MAX_FAILURES_BEFORE_RECOVERY", "3"
        ))
        self._startup_callback: Optional[Callable[[], Awaitable[bool]]] = None

    def register_startup_callback(
        self,
        callback: Callable[[], "asyncio.Future[bool]"]
    ) -> None:
        """
        v134.0: Register a callback to start the J-Prime service.

        This callback is invoked when discovery finds no healthy service
        and auto-recovery conditions are met.

        Args:
            callback: Async function that attempts to start J-Prime.
                      Should return True if startup succeeded, False otherwise.
        """
        self._startup_callback = callback
        logger.info("[Discovery] v134.0 Auto-recovery callback registered")

    async def _attempt_auto_recovery(self) -> bool:
        """
        v134.0: Attempt to recover the J-Prime service.

        Returns:
            True if recovery was attempted and service is now available
        """
        now = time.time()

        # Check cooldown
        if now - self._last_recovery_attempt < self._recovery_cooldown:
            remaining = self._recovery_cooldown - (now - self._last_recovery_attempt)
            logger.debug(
                f"[Discovery] v134.0 Auto-recovery in cooldown ({remaining:.0f}s remaining)"
            )
            return False

        # Check if recovery is enabled and callback is set
        if not self._auto_recovery_enabled:
            logger.debug("[Discovery] v134.0 Auto-recovery disabled")
            return False

        if not self._startup_callback:
            logger.debug("[Discovery] v134.0 No startup callback registered")
            return False

        # Check consecutive failure threshold
        if self._consecutive_failures < self._max_consecutive_failures:
            logger.debug(
                f"[Discovery] v134.0 Not enough failures for recovery "
                f"({self._consecutive_failures}/{self._max_consecutive_failures})"
            )
            return False

        # Attempt recovery
        self._last_recovery_attempt = now
        logger.info(
            f"[Discovery] v134.0 Attempting auto-recovery after "
            f"{self._consecutive_failures} consecutive failures"
        )

        try:
            success = await self._startup_callback()
            if success:
                logger.info("[Discovery] v134.0 Auto-recovery successful")
                self._consecutive_failures = 0
                # Wait briefly for service to fully initialize
                await asyncio.sleep(2.0)
                return True
            else:
                logger.warning("[Discovery] v134.0 Auto-recovery callback returned False")
                return False
        except Exception as e:
            logger.error(f"[Discovery] v134.0 Auto-recovery failed: {e}")
            return False

    async def discover(self, force: bool = False) -> Optional[DiscoveredService]:
        """
        v134.0: Discover J-Prime service with auto-recovery support.

        Returns:
            Best available service or None
        """
        async with self._discovery_lock:
            now = time.time()

            # Skip if recently discovered (unless forced)
            if not force and (now - self._last_discovery_time) < self._discovery_interval:
                if self._primary_service and self._primary_service.healthy:
                    self._consecutive_failures = 0  # Reset on success
                    return self._primary_service

            self._last_discovery_time = now

            # Try discovery methods in priority order
            discovered = []

            # Method 1: Environment variable (highest priority)
            env_service = await self._discover_from_environment()
            if env_service:
                discovered.append(env_service)

            # Method 2: Heartbeat file (second priority)
            heartbeat_service = await self._discover_from_heartbeat()
            if heartbeat_service:
                discovered.append(heartbeat_service)

            # Method 3: Port probing (fallback)
            if not discovered:
                probed = await self._discover_by_probing()
                discovered.extend(probed)

            # Select best service (lowest latency, healthy)
            healthy_services = [s for s in discovered if s.healthy]
            if healthy_services:
                # Sort by latency
                healthy_services.sort(key=lambda s: s.latency_ms)
                self._primary_service = healthy_services[0]

                # Cache all discovered services
                for service in discovered:
                    self._discovered_services[service.url] = service

                # v134.0: Reset failure counter on successful discovery
                self._consecutive_failures = 0

                logger.info(
                    f"[Discovery] Primary service: {self._primary_service.url} "
                    f"(latency={self._primary_service.latency_ms:.1f}ms, "
                    f"format={self._primary_service.api_format})"
                )
                return self._primary_service

            # v134.0: No healthy service found - increment failure counter
            self._consecutive_failures += 1
            logger.warning(
                f"[Discovery] No healthy J-Prime service found "
                f"(failures: {self._consecutive_failures})"
            )

            # v134.0: Attempt auto-recovery if conditions are met
            if await self._attempt_auto_recovery():
                # Re-try discovery after recovery
                probed = await self._discover_by_probing()
                healthy = [s for s in probed if s.healthy]
                if healthy:
                    healthy.sort(key=lambda s: s.latency_ms)
                    self._primary_service = healthy[0]
                    logger.info(
                        f"[Discovery] v134.0 Service recovered: {self._primary_service.url}"
                    )
                    return self._primary_service

            return None

    async def _discover_from_environment(self) -> Optional[DiscoveredService]:
        """Discover from JARVIS_PRIME_URL environment variable."""
        url = os.getenv("JARVIS_PRIME_URL")
        if not url:
            return None

        try:
            # Extract port from URL
            from urllib.parse import urlparse
            parsed = urlparse(url)
            port = parsed.port or 8000

            # Probe health
            latency, api_format, model_info = await self._probe_service(url)
            if latency < float('inf'):
                return DiscoveredService(
                    url=url,
                    port=port,
                    method=ServiceDiscoveryMethod.ENVIRONMENT,
                    latency_ms=latency,
                    api_format=api_format,
                    model_loaded=model_info.get("loaded", False),
                    model_name=model_info.get("name"),
                    healthy=True,
                )

        except Exception as e:
            logger.debug(f"[Discovery] Environment URL probe failed: {e}")

        return None

    async def _discover_from_heartbeat(self) -> Optional[DiscoveredService]:
        """Discover from Trinity heartbeat file."""
        heartbeat_file = self._trinity_dir / "components" / "jarvis_prime.json"

        if not heartbeat_file.exists():
            return None

        try:
            with open(heartbeat_file, 'r') as f:
                data = json.load(f)

            # Check freshness (30 second threshold)
            timestamp = data.get("timestamp", 0)
            age = time.time() - timestamp
            if age > 30.0:
                logger.debug(f"[Discovery] Heartbeat stale ({age:.1f}s old)")
                return None

            port = data.get("port", 8000)
            url = f"http://localhost:{port}"

            # Probe to verify and measure latency
            latency, api_format, model_info = await self._probe_service(url)
            if latency < float('inf'):
                return DiscoveredService(
                    url=url,
                    port=port,
                    method=ServiceDiscoveryMethod.HEARTBEAT_FILE,
                    latency_ms=latency,
                    api_format=api_format,
                    model_loaded=data.get("model_loaded", False),
                    model_name=data.get("model_name"),
                    healthy=True,
                )

        except Exception as e:
            logger.debug(f"[Discovery] Heartbeat read failed: {e}")

        return None

    async def _discover_by_probing(self) -> List[DiscoveredService]:
        """Probe known ports for J-Prime service."""
        discovered = []

        # Get unique ports to probe
        probe_ports = list(dict.fromkeys([
            int(os.getenv("JARVIS_PRIME_PORT", "8000")),
            8000, 8001, 8002, 11434
        ]))

        # Probe ports in parallel
        async def probe_port(port: int) -> Optional[DiscoveredService]:
            url = f"http://localhost:{port}"
            try:
                latency, api_format, model_info = await self._probe_service(url)
                if latency < float('inf'):
                    return DiscoveredService(
                        url=url,
                        port=port,
                        method=ServiceDiscoveryMethod.HTTP_PROBE,
                        latency_ms=latency,
                        api_format=api_format,
                        model_loaded=model_info.get("loaded", False),
                        model_name=model_info.get("name"),
                        healthy=True,
                    )
            except Exception:
                pass
            return None

        # Probe all ports concurrently with timeout
        tasks = [probe_port(port) for port in probe_ports]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, DiscoveredService):
                discovered.append(result)

        return discovered

    async def _probe_service(
        self,
        base_url: str,
        timeout: float = 5.0,
    ) -> Tuple[float, str, Dict[str, Any]]:
        """
        Probe a service for health and detect API format.

        Returns:
            (latency_ms, api_format, model_info)
        """
        try:
            import aiohttp
        except ImportError:
            return (float('inf'), "unknown", {})

        model_info = {}

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            # Try OpenAI format first (preferred)
            start = time.time()
            try:
                async with session.get(f"{base_url}/v1/models") as resp:
                    if resp.status == 200:
                        latency = (time.time() - start) * 1000
                        data = await resp.json()
                        if "data" in data or "models" in data:
                            models = data.get("data", data.get("models", []))
                            if models:
                                model_info["loaded"] = True
                                model_info["name"] = models[0].get("id", "unknown")
                            return (latency, "openai", model_info)
            except Exception:
                pass

            # Try custom health endpoint
            start = time.time()
            try:
                async with session.get(f"{base_url}/health") as resp:
                    if resp.status == 200:
                        latency = (time.time() - start) * 1000
                        data = await resp.json()
                        model_info["loaded"] = data.get("model_loaded", False)
                        return (latency, "custom", model_info)
            except Exception:
                pass

            # Try legacy health
            start = time.time()
            try:
                async with session.get(f"{base_url}/api/health") as resp:
                    if resp.status == 200:
                        latency = (time.time() - start) * 1000
                        return (latency, "legacy", model_info)
            except Exception:
                pass

        return (float('inf'), "unknown", model_info)

    def get_primary_service(self) -> Optional[DiscoveredService]:
        """Get the current primary service."""
        return self._primary_service

    def invalidate(self) -> None:
        """Invalidate discovery cache."""
        self._last_discovery_time = 0.0
        if self._primary_service:
            self._primary_service.healthy = False


# Global discovery instance
_service_discovery = IntelligentServiceDiscovery()


def register_jprime_startup_callback(
    callback: Callable[[], "asyncio.Future[bool]"]
) -> None:
    """
    v134.0: Register a callback to start J-Prime when service discovery fails.

    This enables automatic recovery when the J-Prime service goes offline.
    The callback should attempt to start the J-Prime server and return True
    if successful.

    Args:
        callback: Async function that starts J-Prime and returns success status

    Example:
        async def start_jprime():
            # Start J-Prime subprocess
            return True

        register_jprime_startup_callback(start_jprime)
    """
    _service_discovery.register_startup_callback(callback)


def get_service_discovery() -> IntelligentServiceDiscovery:
    """
    v134.0: Get the global service discovery instance.

    Useful for registering callbacks or checking discovery status.
    """
    return _service_discovery


# =============================================================================
# Types and Enums
# =============================================================================

class InferenceMode(str, Enum):
    """Inference modes."""
    STANDARD = "standard"       # Full context, slower
    FAST = "fast"               # Reduced context, faster
    STREAMING = "streaming"     # Stream tokens
    BATCH = "batch"             # Batch multiple prompts


class CognitiveTaskType(str, Enum):
    """Types of cognitive tasks."""
    REASONING = "reasoning"
    PLANNING = "planning"
    ANALYSIS = "analysis"
    SUMMARIZATION = "summarization"
    CODE_GENERATION = "code_generation"
    CONVERSATION = "conversation"
    DECISION = "decision"
    CREATIVE = "creative"


class ModelStatus(str, Enum):
    """Model status states."""
    UNLOADED = "unloaded"
    LOADING = "loading"
    READY = "ready"
    SWAPPING = "swapping"
    ERROR = "error"


@dataclass
class InferenceRequest:
    """Request for LLM inference."""
    prompt: str
    max_tokens: int = 1024
    temperature: float = 0.7
    top_p: float = 0.9
    stop_sequences: List[str] = field(default_factory=list)
    system_prompt: Optional[str] = None
    mode: InferenceMode = InferenceMode.STANDARD
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "stop_sequences": self.stop_sequences,
            "system_prompt": self.system_prompt,
            "mode": self.mode.value,
            "metadata": self.metadata,
        }


@dataclass
class InferenceResponse:
    """Response from LLM inference."""
    text: str
    tokens_used: int
    latency_ms: float
    model_version: str
    finish_reason: str  # stop, length, error
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CognitiveTask:
    """A cognitive task to delegate to JARVIS Prime."""
    task_id: str
    task_type: CognitiveTaskType
    description: str
    context: Dict[str, Any]
    priority: int = 5  # 1-10, higher is more urgent
    timeout_seconds: float = 60.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type.value,
            "description": self.description,
            "context": self.context,
            "priority": self.priority,
            "timeout_seconds": self.timeout_seconds,
            "metadata": self.metadata,
        }


@dataclass
class CognitiveResult:
    """Result of a cognitive task."""
    task_id: str
    success: bool
    result: Any
    reasoning: Optional[str] = None
    confidence: float = 0.0
    latency_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class ModelInfo:
    """Information about loaded model."""
    model_id: str
    version: str
    status: ModelStatus
    path: str
    loaded_at: Optional[float] = None
    memory_mb: float = 0.0
    context_length: int = 4096
    parameters: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# JARVIS Prime Client Configuration
# =============================================================================

@dataclass
class JARVISPrimeConfig(ClientConfig):
    """
    v84.0: Configuration for JARVIS Prime client.

    NOTE: base_url defaults to port 8000 (J-Prime standard port).
    Set JARVIS_PRIME_URL environment variable to override.
    """
    name: str = "jarvis_prime"
    # v84.0: Fixed port to 8000 (was incorrectly 8002)
    base_url: str = field(default_factory=lambda: _env_str(
        "JARVIS_PRIME_URL", "http://localhost:8000"  # ✅ Correct port
    ))
    timeout: float = field(default_factory=lambda: _env_float(
        "JARVIS_PRIME_TIMEOUT", 60.0
    ))
    # Inference settings
    default_max_tokens: int = field(default_factory=lambda: _env_int(
        "JARVIS_PRIME_DEFAULT_MAX_TOKENS", 1024
    ))
    default_temperature: float = field(default_factory=lambda: _env_float(
        "JARVIS_PRIME_DEFAULT_TEMPERATURE", 0.7
    ))
    default_top_p: float = field(default_factory=lambda: _env_float(
        "JARVIS_PRIME_DEFAULT_TOP_P", 0.9
    ))
    # Streaming
    stream_buffer_size: int = field(default_factory=lambda: _env_int(
        "JARVIS_PRIME_STREAM_BUFFER", 16
    ))
    # Model swap
    model_swap_timeout: float = field(default_factory=lambda: _env_float(
        "JARVIS_PRIME_SWAP_TIMEOUT", 120.0
    ))
    # v84.0: Service discovery settings
    enable_discovery: bool = field(default_factory=lambda: _env_bool(
        "JARVIS_PRIME_ENABLE_DISCOVERY", True
    ))
    discovery_interval: float = field(default_factory=lambda: _env_float(
        "JARVIS_PRIME_DISCOVERY_INTERVAL", 30.0
    ))
    # v84.0: API format preference
    api_format: str = field(default_factory=lambda: _env_str(
        "JARVIS_PRIME_API_FORMAT", "auto"  # "auto", "openai", "custom"
    ))
    # Fallback
    fallback_to_cloud: bool = field(default_factory=lambda: _env_bool(
        "JARVIS_PRIME_FALLBACK_TO_CLOUD", True
    ))
    cloud_api_url: str = field(default_factory=lambda: _env_str(
        "JARVIS_PRIME_CLOUD_URL", ""
    ))
    # v84.0: Connection pool settings
    pool_size: int = field(default_factory=lambda: _env_int(
        "JARVIS_PRIME_POOL_SIZE", 3
    ))
    # v84.0: Retry settings
    max_retries: int = field(default_factory=lambda: _env_int(
        "JARVIS_PRIME_MAX_RETRIES", 3
    ))
    retry_base_delay: float = field(default_factory=lambda: _env_float(
        "JARVIS_PRIME_RETRY_DELAY", 0.5
    ))


# =============================================================================
# JARVIS Prime Client
# =============================================================================

class JARVISPrimeClient(TrinityBaseClient[Dict[str, Any]]):
    """
    v84.0: Advanced client for JARVIS Prime (cognitive mind).

    Features:
    - Intelligent service discovery with fallback
    - OpenAI-compatible API format
    - LLM inference with streaming support
    - Hot-swap model reloading
    - Cognitive task delegation
    - Latency-based routing
    - Connection pooling
    - Fallback to cloud when local is unavailable
    """

    def __init__(
        self,
        config: Optional[JARVISPrimeConfig] = None,
        circuit_config: Optional[CircuitBreakerConfig] = None,
        retry_config: Optional[RetryConfig] = None,
    ):
        self._prime_config = config or JARVISPrimeConfig()

        super().__init__(
            config=self._prime_config,
            circuit_config=circuit_config,
            retry_config=retry_config,
        )

        # Model state
        self._model_info: Optional[ModelInfo] = None
        self._is_swapping = False

        # Metrics
        self._total_inferences = 0
        self._total_tokens = 0
        self._avg_latency_ms = 0.0
        self._latency_samples: deque = deque(maxlen=100)

        # v84.0: Service discovery
        self._discovered_service: Optional[DiscoveredService] = None
        self._api_format: str = "auto"  # Will be detected

        # HTTP session
        self._session = None

        logger.info(
            f"[JARVISPrime] Client initialized (discovery={'enabled' if self._prime_config.enable_discovery else 'disabled'})"
        )

    async def _get_session(self):
        """Get or create HTTP session."""
        if self._session is None:
            try:
                import aiohttp
                # v84.0: Configure connection pooling
                connector = aiohttp.TCPConnector(
                    limit=self._prime_config.pool_size,
                    limit_per_host=self._prime_config.pool_size,
                    keepalive_timeout=30,
                )
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(total=self._prime_config.timeout)
                )
            except ImportError:
                logger.warning("[JARVISPrime] aiohttp not available")
                raise
        return self._session

    async def _get_base_url(self) -> str:
        """
        v84.0: Get base URL using intelligent discovery.

        Priority:
        1. Discovered service (if enabled and healthy)
        2. Environment variable JARVIS_PRIME_URL
        3. Config base_url
        """
        if self._prime_config.enable_discovery:
            service = await _service_discovery.discover()
            if service:
                self._discovered_service = service
                self._api_format = service.api_format
                return service.url

        return self._prime_config.base_url

    async def _health_check(self) -> bool:
        """
        Check if JARVIS Prime is healthy with robust error reporting.

        v89.0: Enhanced with:
        - Configurable timeout via PRIME_HEALTH_CHECK_TIMEOUT env var
        - Detailed logging for each endpoint attempt (not silent)
        - Reordered endpoints by likelihood of success
        - Connection error differentiation

        v90.0: Fixed aiohttp import issue - now properly imports at method level
        """
        # v90.0: Import aiohttp at method level to avoid NameError
        try:
            import aiohttp
        except ImportError:
            logger.warning("[JARVISPrime] aiohttp not installed - health check skipped")
            return False

        try:
            base_url = await self._get_base_url()
            session = await self._get_session()

            # v89.0: Use configurable timeout (default 10s, was hardcoded 5s)
            health_timeout = float(os.getenv("PRIME_HEALTH_CHECK_TIMEOUT", "10.0"))

            # v89.0: Reordered by likelihood - /v1/models is most reliable for OpenAI format
            # /health is standard, /api/health is legacy fallback
            health_endpoints = ["/v1/models", "/health", "/api/health"]

            last_error = None
            for endpoint in health_endpoints:
                url = f"{base_url}{endpoint}"
                try:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=health_timeout)
                    ) as response:
                        if response.status == 200:
                            logger.debug(f"[JARVISPrime] Health check passed: {endpoint}")
                            return True
                        else:
                            logger.debug(
                                f"[JARVISPrime] Health check {endpoint}: status {response.status}"
                            )
                except aiohttp.ClientConnectorError as e:
                    # Connection refused, host unreachable, etc.
                    last_error = f"Connection error: {e}"
                    logger.debug(f"[JARVISPrime] Health check {endpoint}: {last_error}")
                except asyncio.TimeoutError:
                    last_error = f"Timeout after {health_timeout}s"
                    logger.debug(f"[JARVISPrime] Health check {endpoint}: {last_error}")
                except Exception as e:
                    last_error = str(e)
                    logger.debug(f"[JARVISPrime] Health check {endpoint}: {last_error}")

            # v89.0: Log the actual failure reason instead of silent return
            if last_error:
                logger.warning(
                    f"[JARVISPrime] All health endpoints failed at {base_url}: {last_error}"
                )
            return False

        except Exception as e:
            logger.warning(f"[JARVISPrime] Health check setup failed: {e}")
            return False

    async def _execute_request(
        self,
        operation: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        v88.0: Execute request with intelligent endpoint mapping + ultra protection.

        Supports both OpenAI format and custom format based on detected API.
        Now includes v88.0 protection stack:
        - Adaptive circuit breaker with ML-based prediction
        - Backpressure handling with AIMD rate limiting
        - W3C distributed tracing
        - Timeout enforcement
        """
        # v88.0: Use ultra coordinator protection if available
        ultra_coord = await _get_ultra_coordinator()
        if ultra_coord:
            timeout = float(os.getenv("JARVIS_PRIME_REQUEST_TIMEOUT", "60.0"))
            success, result, metadata = await ultra_coord.execute_with_protection(
                component="jarvis_prime",
                operation=lambda: self._execute_request_internal(operation, payload),
                timeout=timeout,
            )
            if success and result is not None:
                return result
            elif not success:
                error_msg = metadata.get("error", "Unknown protection error")
                if metadata.get("circuit_open"):
                    logger.warning(f"[JARVISPrime] v88.0 Circuit breaker open: {error_msg}")
                    _service_discovery.invalidate()
                raise RuntimeError(f"[v88.0] Protected request failed: {error_msg}")

        # Fallback: direct execution without protection
        return await self._execute_request_internal(operation, payload)

    async def _execute_request_internal(
        self,
        operation: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        v84.0/v88.0: Internal request execution (called by protection wrapper).

        Supports both OpenAI format and custom format based on detected API.
        """
        base_url = await self._get_base_url()
        session = await self._get_session()

        # v84.0: Determine API format
        api_format = self._api_format
        if api_format == "auto":
            api_format = self._discovered_service.api_format if self._discovered_service else "openai"

        # v84.0: Map operation to endpoint based on API format
        if api_format == "openai":
            endpoint_map = {
                "inference": ("POST", "/v1/chat/completions"),
                "model_status": ("GET", "/v1/models"),
                "model_swap": ("POST", "/v1/models/load"),  # Custom extension
                "cognitive_delegate": ("POST", "/v1/chat/completions"),  # Use chat for delegation
                "embeddings": ("POST", "/v1/embeddings"),
            }
        else:
            # Legacy/custom format
            endpoint_map = {
                "inference": ("POST", "/api/inference"),
                "model_status": ("GET", "/api/model/status"),
                "model_swap": ("POST", "/api/model/swap"),
                "cognitive_delegate": ("POST", "/api/cognitive/delegate"),
                "embeddings": ("POST", "/api/embeddings"),
            }

        if operation not in endpoint_map:
            raise ValueError(f"Unknown operation: {operation}")

        method, endpoint = endpoint_map[operation]
        url = f"{base_url}{endpoint}"

        # v84.0: Transform payload for OpenAI format if needed
        if api_format == "openai" and operation == "inference":
            payload = self._to_openai_format(payload)

        # v84.0: Execute with retry and latency tracking
        start_time = time.time()
        last_error = None

        for attempt in range(self._prime_config.max_retries):
            try:
                if method == "GET":
                    async with session.get(url, params=payload) as response:
                        response.raise_for_status()
                        result = await response.json()
                else:
                    async with session.post(url, json=payload) as response:
                        response.raise_for_status()
                        result = await response.json()

                # Track latency
                latency_ms = (time.time() - start_time) * 1000
                self._latency_samples.append(latency_ms)

                # v84.0: Transform response from OpenAI format if needed
                if api_format == "openai" and operation == "inference":
                    result = self._from_openai_format(result, latency_ms)

                return result

            except Exception as e:
                last_error = e
                if attempt < self._prime_config.max_retries - 1:
                    # Exponential backoff with jitter
                    delay = self._prime_config.retry_base_delay * (2 ** attempt)
                    jitter = delay * 0.1 * random.random()
                    await asyncio.sleep(delay + jitter)
                    logger.debug(f"[JARVISPrime] Retry {attempt + 1}: {e}")

        # All retries failed
        logger.warning(f"[JARVISPrime] Request failed after {self._prime_config.max_retries} retries: {last_error}")
        _service_discovery.invalidate()  # Mark service as unhealthy
        raise last_error

    def _to_openai_format(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Convert internal format to OpenAI chat completions format."""
        messages = []

        # Add system prompt if present
        system_prompt = payload.get("system_prompt")
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # Add user prompt
        prompt = payload.get("prompt", "")
        messages.append({"role": "user", "content": prompt})

        return {
            "messages": messages,
            "max_tokens": payload.get("max_tokens", self._prime_config.default_max_tokens),
            "temperature": payload.get("temperature", self._prime_config.default_temperature),
            "top_p": payload.get("top_p", self._prime_config.default_top_p),
            "stop": payload.get("stop_sequences", []),
            "stream": payload.get("stream", False),
        }

    def _from_openai_format(self, response: Dict[str, Any], latency_ms: float) -> Dict[str, Any]:
        """Convert OpenAI chat completions response to internal format."""
        choices = response.get("choices", [])
        if not choices:
            return {"text": "", "tokens_used": 0, "latency_ms": latency_ms, "finish_reason": "error"}

        choice = choices[0]
        message = choice.get("message", {})
        usage = response.get("usage", {})

        return {
            "text": message.get("content", ""),
            "tokens_used": usage.get("total_tokens", 0),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "latency_ms": latency_ms,
            "model_version": response.get("model", "unknown"),
            "finish_reason": choice.get("finish_reason", "unknown"),
            "metadata": {
                "id": response.get("id"),
                "created": response.get("created"),
            },
        }

    async def disconnect(self) -> None:
        """Disconnect and cleanup."""
        if self._session:
            await self._session.close()
            self._session = None

        await super().disconnect()

    # =========================================================================
    # Inference API
    # =========================================================================

    async def inference(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: float = 0.9,
        stop_sequences: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
        mode: InferenceMode = InferenceMode.STANDARD,
    ) -> Optional[InferenceResponse]:
        """
        Run inference on JARVIS Prime.

        Args:
            prompt: Input prompt
            max_tokens: Max tokens to generate
            temperature: Sampling temperature
            top_p: Nucleus sampling threshold
            stop_sequences: Stop sequences
            system_prompt: Optional system prompt
            mode: Inference mode

        Returns:
            InferenceResponse or None if failed
        """
        request = InferenceRequest(
            prompt=prompt,
            max_tokens=max_tokens or self._prime_config.default_max_tokens,
            temperature=temperature or self._prime_config.default_temperature,
            top_p=top_p,
            stop_sequences=stop_sequences or [],
            system_prompt=system_prompt,
            mode=mode,
        )

        result = await self.execute("inference", request.to_dict())

        if result:
            self._total_inferences += 1
            self._total_tokens += result.get("tokens_used", 0)

            # Update average latency
            latency = result.get("latency_ms", 0)
            self._avg_latency_ms = (
                (self._avg_latency_ms * (self._total_inferences - 1) + latency)
                / self._total_inferences
            )

            return InferenceResponse(
                text=result.get("text", ""),
                tokens_used=result.get("tokens_used", 0),
                latency_ms=latency,
                model_version=result.get("model_version", "unknown"),
                finish_reason=result.get("finish_reason", "unknown"),
                metadata=result.get("metadata", {}),
            )

        # Fallback to cloud if enabled
        if self._prime_config.fallback_to_cloud and self._prime_config.cloud_api_url:
            return await self._cloud_inference(request)

        return None

    async def inference_stream(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """
        Stream inference tokens from JARVIS Prime.

        Yields:
            Token strings as they're generated
        """
        if not self._is_online:
            return

        session = await self._get_session()
        url = f"{self._prime_config.base_url}/api/inference/stream"

        payload = {
            "prompt": prompt,
            "max_tokens": max_tokens or self._prime_config.default_max_tokens,
            "temperature": temperature or self._prime_config.default_temperature,
            "system_prompt": system_prompt,
            "stream": True,
        }

        try:
            async with session.post(url, json=payload) as response:
                response.raise_for_status()

                async for chunk in response.content.iter_chunked(
                    self._prime_config.stream_buffer_size
                ):
                    if chunk:
                        yield chunk.decode("utf-8")

        except Exception as e:
            logger.warning(f"[JARVISPrime] Stream error: {e}")

    async def _cloud_inference(
        self,
        request: InferenceRequest,
    ) -> Optional[InferenceResponse]:
        """Fallback to cloud inference."""
        if not self._prime_config.cloud_api_url:
            return None

        try:
            session = await self._get_session()
            url = f"{self._prime_config.cloud_api_url}/api/inference"

            async with session.post(url, json=request.to_dict()) as response:
                if response.status == 200:
                    result = await response.json()
                    return InferenceResponse(
                        text=result.get("text", ""),
                        tokens_used=result.get("tokens_used", 0),
                        latency_ms=result.get("latency_ms", 0),
                        model_version="cloud",
                        finish_reason=result.get("finish_reason", "unknown"),
                        metadata={"fallback": True},
                    )

        except Exception as e:
            logger.warning(f"[JARVISPrime] Cloud fallback failed: {e}")

        return None

    # =========================================================================
    # Model Management
    # =========================================================================

    async def get_model_status(self) -> Optional[ModelInfo]:
        """Get current model status."""
        result = await self.execute("model_status", {})

        if result:
            self._model_info = ModelInfo(
                model_id=result.get("model_id", "unknown"),
                version=result.get("version", "unknown"),
                status=ModelStatus(result.get("status", "unknown")),
                path=result.get("path", ""),
                loaded_at=result.get("loaded_at"),
                memory_mb=result.get("memory_mb", 0),
                context_length=result.get("context_length", 4096),
                parameters=result.get("parameters", {}),
            )
            return self._model_info

        return None

    async def swap_model(
        self,
        model_path: str,
        version_id: Optional[str] = None,
        validate_before: bool = True,
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        Hot-swap the loaded model.

        Args:
            model_path: Path to new model file
            version_id: Optional version identifier
            validate_before: Validate model before swap
            force: Force swap even if validation fails

        Returns:
            Swap result with success status
        """
        if self._is_swapping and not force:
            return {"success": False, "error": "Model swap already in progress"}

        self._is_swapping = True

        try:
            import aiohttp

            session = await self._get_session()
            url = f"{self._prime_config.base_url}/api/model/swap"

            payload = {
                "model_path": model_path,
                "version_id": version_id,
                "validate_before_swap": validate_before,
                "force": force,
            }

            timeout = aiohttp.ClientTimeout(total=self._prime_config.model_swap_timeout)

            async with session.post(url, json=payload, timeout=timeout) as response:
                result = await response.json()

                if response.status == 200 and result.get("success"):
                    logger.info(
                        f"[JARVISPrime] Model swap SUCCESS: "
                        f"{result.get('old_version')} → {result.get('new_version')}"
                    )
                    # Update model info
                    await self.get_model_status()
                else:
                    logger.warning(
                        f"[JARVISPrime] Model swap FAILED: {result.get('error')}"
                    )

                return result

        except Exception as e:
            logger.error(f"[JARVISPrime] Model swap error: {e}")
            return {"success": False, "error": str(e)}

        finally:
            self._is_swapping = False

    # =========================================================================
    # Cognitive Delegation
    # =========================================================================

    async def delegate_cognitive_task(
        self,
        task: CognitiveTask,
    ) -> Optional[CognitiveResult]:
        """
        Delegate a cognitive task to JARVIS Prime.

        Args:
            task: Cognitive task to delegate

        Returns:
            CognitiveResult or None if failed
        """
        result = await self.execute("cognitive_delegate", task.to_dict())

        if result:
            return CognitiveResult(
                task_id=task.task_id,
                success=result.get("success", False),
                result=result.get("result"),
                reasoning=result.get("reasoning"),
                confidence=result.get("confidence", 0.0),
                latency_ms=result.get("latency_ms", 0),
                error=result.get("error"),
            )

        return None

    async def reason(
        self,
        question: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[CognitiveResult]:
        """
        Run reasoning task.

        Args:
            question: Question to reason about
            context: Optional context

        Returns:
            CognitiveResult
        """
        import uuid

        task = CognitiveTask(
            task_id=str(uuid.uuid4())[:8],
            task_type=CognitiveTaskType.REASONING,
            description=question,
            context=context or {},
        )

        return await self.delegate_cognitive_task(task)

    async def plan(
        self,
        goal: str,
        constraints: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[CognitiveResult]:
        """
        Run planning task.

        Args:
            goal: Goal to plan for
            constraints: Optional constraints
            context: Optional context

        Returns:
            CognitiveResult with plan
        """
        import uuid

        task = CognitiveTask(
            task_id=str(uuid.uuid4())[:8],
            task_type=CognitiveTaskType.PLANNING,
            description=goal,
            context={
                **(context or {}),
                "constraints": constraints or [],
            },
        )

        return await self.delegate_cognitive_task(task)

    # =========================================================================
    # Embeddings
    # =========================================================================

    async def get_embeddings(
        self,
        texts: List[str],
        model: str = "default",
    ) -> Optional[List[List[float]]]:
        """
        Get embeddings for texts.

        Args:
            texts: List of texts to embed
            model: Embedding model to use

        Returns:
            List of embedding vectors
        """
        result = await self.execute("embeddings", {
            "texts": texts,
            "model": model,
        })

        if result:
            return result.get("embeddings", [])

        return None

    # =========================================================================
    # Metrics
    # =========================================================================

    def get_metrics(self) -> Dict[str, Any]:
        """Get client metrics."""
        metrics = super().get_metrics()
        metrics.update({
            "total_inferences": self._total_inferences,
            "total_tokens": self._total_tokens,
            "avg_latency_ms": self._avg_latency_ms,
            "model_status": self._model_info.status.value if self._model_info else "unknown",
            "is_swapping": self._is_swapping,
        })
        return metrics


# =============================================================================
# Singleton Access
# =============================================================================

_client: Optional[JARVISPrimeClient] = None
_client_lock: Optional[asyncio.Lock] = None  # v90.0: Lazy lock initialization


async def get_jarvis_prime_client(
    config: Optional[JARVISPrimeConfig] = None,
) -> JARVISPrimeClient:
    """Get or create the singleton JARVIS Prime client."""
    global _client, _client_lock

    # v90.0: Lazy lock creation to avoid "no event loop" errors at module load
    if _client_lock is None:
        _client_lock = asyncio.Lock()

    async with _client_lock:
        if _client is None:
            _client = JARVISPrimeClient(config)
            await _client.connect()
        return _client


async def close_jarvis_prime_client() -> None:
    """Close the JARVIS Prime client."""
    global _client

    if _client:
        await _client.disconnect()
        _client = None


# =============================================================================
# Convenience Functions
# =============================================================================

async def inference(
    prompt: str,
    max_tokens: int = 1024,
    temperature: float = 0.7,
) -> Optional[str]:
    """Run inference and return text."""
    client = await get_jarvis_prime_client()
    result = await client.inference(
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return result.text if result else None


async def reason(question: str) -> Optional[str]:
    """Run reasoning and return result."""
    client = await get_jarvis_prime_client()
    result = await client.reason(question)
    return result.result if result and result.success else None


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Types
    "InferenceMode",
    "CognitiveTaskType",
    "ModelStatus",
    "InferenceRequest",
    "InferenceResponse",
    "CognitiveTask",
    "CognitiveResult",
    "ModelInfo",
    # Config
    "JARVISPrimeConfig",
    # Client
    "JARVISPrimeClient",
    # Access
    "get_jarvis_prime_client",
    "close_jarvis_prime_client",
    # Convenience
    "inference",
    "reason",
]
