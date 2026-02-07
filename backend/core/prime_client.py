"""
Prime Client v1.0
=================

Advanced client for communicating with JARVIS-Prime (the Mind component).
This is THE critical integration point that connects JARVIS to its local AI brain.

FEATURES:
    - Connection pooling via aiohttp for high performance
    - Circuit breaker integration for fault tolerance
    - Streaming support for real-time responses
    - Automatic fallback to Cloud Claude when Prime unavailable
    - Health monitoring and reconnection logic
    - Request queuing during temporary outages
    - Timeout protection at every level

USAGE:
    from backend.core.prime_client import get_prime_client, PrimeRequest

    client = await get_prime_client()

    # Simple request
    response = await client.generate(
        prompt="What is the capital of France?",
        system_prompt="You are a helpful assistant."
    )

    # Streaming request
    async for chunk in client.generate_stream(prompt="Tell me a story"):
        print(chunk, end="", flush=True)

    # With fallback handling
    result = await client.generate_with_fallback(
        prompt="Complex question here",
        fallback_to_cloud=True
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Dict,
    List,
    Optional,
    TypeVar,
)
from contextlib import asynccontextmanager

from backend.core.async_safety import TimeoutConfig, LazyAsyncEvent, get_shutdown_event

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# CONFIGURATION
# =============================================================================

def _get_env_float(key: str, default: float) -> float:
    """Get float from environment with fallback."""
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _get_env_int(key: str, default: int) -> int:
    """Get int from environment with fallback."""
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _get_env_bool(key: str, default: bool) -> bool:
    """Get bool from environment with fallback."""
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


def _resolve_prime_host() -> str:
    """
    Resolve JARVIS Prime host with intelligent priority order.

    v233.2 ROOT CAUSE FIX: The supervisor sets multiple env vars when GCP VM is ready:
      - JARVIS_PRIME_URL (full URL like http://34.45.154.209:8000)
      - JARVIS_INVINCIBLE_NODE_IP (just the IP)
      - JARVIS_PRIME_HOST (explicit host override)

    Priority order (highest wins):
      1. JARVIS_PRIME_URL (parse host from full URL — set by _propagate_invincible_node_url)
      2. JARVIS_INVINCIBLE_NODE_IP (set by supervisor when GCP VM is ready)
      3. JARVIS_PRIME_HOST (explicit override)
      4. "localhost" (fallback)
    """
    # Priority 1: Parse host from JARVIS_PRIME_URL (set by supervisor)
    prime_url = os.getenv("JARVIS_PRIME_URL", "")
    if prime_url:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(prime_url)
            if parsed.hostname:
                return parsed.hostname
        except Exception:
            pass

    # Priority 2: Invincible Node IP (set by supervisor for GCP VMs)
    invincible_ip = os.getenv("JARVIS_INVINCIBLE_NODE_IP", "")
    if invincible_ip:
        return invincible_ip

    # Priority 3: Explicit host override
    return os.getenv("JARVIS_PRIME_HOST", "localhost")


def _resolve_prime_port() -> int:
    """
    Resolve JARVIS Prime port with intelligent priority order.

    v233.2 ROOT CAUSE FIX: Same as _resolve_prime_host — ensures port
    from GCP VM propagation is picked up without requiring explicit JARVIS_PRIME_PORT.

    Priority order:
      1. JARVIS_PRIME_URL (parse port from full URL)
      2. JARVIS_INVINCIBLE_NODE_PORT (set by supervisor)
      3. JARVIS_PRIME_PORT (explicit override)
      4. 8000 (fallback)
    """
    # Priority 1: Parse port from JARVIS_PRIME_URL
    prime_url = os.getenv("JARVIS_PRIME_URL", "")
    if prime_url:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(prime_url)
            if parsed.port:
                return parsed.port
        except Exception:
            pass

    # Priority 2: Invincible Node port
    invincible_port = os.getenv("JARVIS_INVINCIBLE_NODE_PORT", "")
    if invincible_port:
        try:
            return int(invincible_port)
        except ValueError:
            pass

    # Priority 3: Explicit port override
    return _get_env_int("JARVIS_PRIME_PORT", 8000)


@dataclass
class PrimeClientConfig:
    """
    Configuration for Prime client.

    v233.2: Uses intelligent env var resolution that respects the supervisor's
    GCP VM propagation (JARVIS_PRIME_URL, JARVIS_INVINCIBLE_NODE_IP/PORT)
    in addition to the explicit JARVIS_PRIME_HOST/PORT overrides.
    """
    # Connection settings — resolved dynamically from multiple env var sources
    prime_host: str = field(default_factory=_resolve_prime_host)
    prime_port: int = field(default_factory=_resolve_prime_port)
    prime_api_version: str = field(default_factory=lambda: os.getenv("JARVIS_PRIME_API_VERSION", "v1"))

    # Connection pool settings
    pool_size: int = field(default_factory=lambda: _get_env_int("PRIME_POOL_SIZE", 10))
    pool_timeout: float = field(default_factory=lambda: _get_env_float("PRIME_POOL_TIMEOUT", 30.0))

    # Timeout settings
    connect_timeout: float = field(default_factory=lambda: _get_env_float("PRIME_CONNECT_TIMEOUT", 5.0))
    read_timeout: float = field(default_factory=lambda: _get_env_float("PRIME_READ_TIMEOUT", 120.0))
    total_timeout: float = field(default_factory=lambda: _get_env_float("PRIME_TOTAL_TIMEOUT", 180.0))

    # Retry settings
    max_retries: int = field(default_factory=lambda: _get_env_int("PRIME_MAX_RETRIES", 3))
    retry_base_delay: float = field(default_factory=lambda: _get_env_float("PRIME_RETRY_BASE_DELAY", 0.5))
    retry_max_delay: float = field(default_factory=lambda: _get_env_float("PRIME_RETRY_MAX_DELAY", 10.0))

    # Circuit breaker settings
    circuit_failure_threshold: int = field(default_factory=lambda: _get_env_int("PRIME_CIRCUIT_FAILURE_THRESHOLD", 5))
    circuit_reset_timeout: float = field(default_factory=lambda: _get_env_float("PRIME_CIRCUIT_RESET_TIMEOUT", 30.0))
    circuit_half_open_requests: int = field(default_factory=lambda: _get_env_int("PRIME_CIRCUIT_HALF_OPEN_REQUESTS", 3))

    # Health check settings
    health_check_interval: float = field(default_factory=lambda: _get_env_float("PRIME_HEALTH_CHECK_INTERVAL", 10.0))
    health_check_timeout: float = field(default_factory=lambda: _get_env_float("PRIME_HEALTH_CHECK_TIMEOUT", 5.0))

    # Fallback settings
    enable_cloud_fallback: bool = field(default_factory=lambda: _get_env_bool("PRIME_ENABLE_CLOUD_FALLBACK", True))
    prefer_local: bool = field(default_factory=lambda: _get_env_bool("PRIME_PREFER_LOCAL", True))

    def __post_init__(self):
        """Log resolved configuration for debugging."""
        is_gcp = self.prime_host != "localhost" and self.prime_host != "127.0.0.1"
        if is_gcp:
            logger.info(
                f"[PrimeClientConfig] Resolved to GCP VM: {self.prime_host}:{self.prime_port} "
                f"(source: {self._identify_source()})"
            )

    def _identify_source(self) -> str:
        """Identify which env var was used to resolve the endpoint."""
        if os.getenv("JARVIS_PRIME_URL"):
            return "JARVIS_PRIME_URL"
        if os.getenv("JARVIS_INVINCIBLE_NODE_IP"):
            return "JARVIS_INVINCIBLE_NODE_IP/PORT"
        if os.getenv("JARVIS_PRIME_HOST"):
            return "JARVIS_PRIME_HOST/PORT"
        return "default (localhost)"

    @property
    def base_url(self) -> str:
        """Get the base URL for Prime API (OpenAI-compatible format without /api prefix)."""
        return f"http://{self.prime_host}:{self.prime_port}/{self.prime_api_version}"

    @property
    def health_url(self) -> str:
        """Get the health check URL."""
        return f"http://{self.prime_host}:{self.prime_port}/health"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


class PrimeStatus(Enum):
    """Prime availability status."""
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass
class PrimeRequest:
    """Request to Prime."""
    prompt: str
    system_prompt: Optional[str] = None
    context: Optional[List[Dict[str, str]]] = None
    max_tokens: int = 4096
    temperature: float = 0.7
    stream: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    request_id: Optional[str] = None

    def __post_init__(self):
        if self.request_id is None:
            import uuid
            self.request_id = str(uuid.uuid4())[:8]


@dataclass
class PrimeResponse:
    """Response from Prime."""
    content: str
    request_id: str
    model: str = "jarvis-prime"
    source: str = "local_prime"  # local_prime, cloud_claude, cached
    latency_ms: float = 0.0
    tokens_used: int = 0
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CircuitBreakerState:
    """Internal circuit breaker state."""
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    successes_in_half_open: int = 0
    last_failure_time: float = 0.0
    last_state_change: float = field(default_factory=time.time)


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class PrimeCircuitBreaker:
    """
    Circuit breaker for Prime client.

    Prevents cascading failures by tracking errors and temporarily
    blocking requests when Prime is unhealthy.
    """

    def __init__(self, config: PrimeClientConfig):
        self._config = config
        self._state = CircuitBreakerState()
        self._lock = asyncio.Lock()
        self._half_open_semaphore: Optional[asyncio.Semaphore] = None

    async def can_execute(self) -> bool:
        """Check if request can proceed."""
        async with self._lock:
            now = time.time()

            if self._state.state == CircuitState.CLOSED:
                return True

            if self._state.state == CircuitState.OPEN:
                # Check if we should transition to half-open
                if now - self._state.last_failure_time >= self._config.circuit_reset_timeout:
                    self._state.state = CircuitState.HALF_OPEN
                    self._state.successes_in_half_open = 0
                    self._state.last_state_change = now
                    self._half_open_semaphore = asyncio.Semaphore(
                        self._config.circuit_half_open_requests
                    )
                    logger.info("[PrimeCircuit] Transitioning to HALF_OPEN")
                    return True
                return False

            if self._state.state == CircuitState.HALF_OPEN:
                # Allow limited requests in half-open
                if self._half_open_semaphore:
                    try:
                        return self._half_open_semaphore.locked() is False
                    except Exception:
                        return True
                return True

            return False

    async def record_success(self) -> None:
        """Record successful request."""
        async with self._lock:
            if self._state.state == CircuitState.HALF_OPEN:
                self._state.successes_in_half_open += 1
                if self._state.successes_in_half_open >= self._config.circuit_half_open_requests:
                    self._state.state = CircuitState.CLOSED
                    self._state.failures = 0
                    self._state.last_state_change = time.time()
                    logger.info("[PrimeCircuit] Circuit CLOSED - Prime recovered")
            else:
                # Reset failure count on success in closed state
                self._state.failures = 0

    async def record_failure(self) -> None:
        """Record failed request."""
        async with self._lock:
            now = time.time()
            self._state.failures += 1
            self._state.last_failure_time = now

            if self._state.state == CircuitState.HALF_OPEN:
                # Immediately open on failure in half-open
                self._state.state = CircuitState.OPEN
                self._state.last_state_change = now
                logger.warning("[PrimeCircuit] Circuit OPEN - failure in half-open state")
            elif self._state.state == CircuitState.CLOSED:
                if self._state.failures >= self._config.circuit_failure_threshold:
                    self._state.state = CircuitState.OPEN
                    self._state.last_state_change = now
                    logger.warning(
                        f"[PrimeCircuit] Circuit OPEN - {self._state.failures} consecutive failures"
                    )

    def get_state(self) -> Dict[str, Any]:
        """Get current circuit state."""
        return {
            "state": self._state.state.value,
            "failures": self._state.failures,
            "last_failure_time": self._state.last_failure_time,
            "last_state_change": self._state.last_state_change,
        }


# =============================================================================
# CONNECTION POOL
# =============================================================================

class PrimeConnectionPool:
    """
    Connection pool for Prime client using aiohttp.

    Manages persistent connections for better performance.
    """

    def __init__(self, config: PrimeClientConfig):
        self._config = config
        self._session: Optional[Any] = None  # aiohttp.ClientSession
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the connection pool."""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            try:
                import aiohttp

                # Create connection pool with limits
                connector = aiohttp.TCPConnector(
                    limit=self._config.pool_size,
                    limit_per_host=self._config.pool_size,
                    keepalive_timeout=30,
                    enable_cleanup_closed=True,
                )

                # Create timeout configuration
                timeout = aiohttp.ClientTimeout(
                    total=self._config.total_timeout,
                    connect=self._config.connect_timeout,
                    sock_read=self._config.read_timeout,
                )

                self._session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "JARVIS-AI-Agent/1.0",
                    },
                )

                self._initialized = True
                logger.info(
                    f"[PrimePool] Initialized with pool_size={self._config.pool_size}"
                )

            except ImportError:
                logger.warning("[PrimePool] aiohttp not available, using fallback")
                self._initialized = True

    async def close(self) -> None:
        """Close the connection pool."""
        async with self._lock:
            if self._session:
                await self._session.close()
                self._session = None
            self._initialized = False

    @asynccontextmanager
    async def get_session(self):
        """Get a session from the pool."""
        if not self._initialized:
            await self.initialize()

        if self._session:
            yield self._session
        else:
            # Fallback to httpx if aiohttp not available
            try:
                import httpx
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        connect=self._config.connect_timeout,
                        read=self._config.read_timeout,
                        write=30.0,
                        pool=self._config.pool_timeout,
                    )
                ) as client:
                    yield client
            except ImportError:
                raise RuntimeError("Neither aiohttp nor httpx available")


# =============================================================================
# PRIME CLIENT
# =============================================================================

class PrimeClient:
    """
    Advanced client for JARVIS-Prime communication.

    This is the main integration point between JARVIS and its local AI brain.
    """

    def __init__(self, config: Optional[PrimeClientConfig] = None):
        self._config = config or PrimeClientConfig()
        self._pool = PrimeConnectionPool(self._config)
        self._circuit = PrimeCircuitBreaker(self._config)
        self._status = PrimeStatus.UNKNOWN
        self._last_health_check = 0.0
        self._health_check_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._initialized = False
        self._cloud_client: Optional[Any] = None  # Claude client for fallback
        # v232.0: GCP VM endpoint hot-swap state
        self._endpoint_source: str = "local"  # "local" or "gcp_vm"
        self._fallback_host: Optional[str] = None
        self._fallback_port: Optional[int] = None
        self._consecutive_gcp_failures: int = 0

    async def initialize(self) -> None:
        """Initialize the client and start health monitoring."""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            await self._pool.initialize()

            # Initial health check
            await self._check_health()

            # Start background health monitor
            self._health_check_task = asyncio.create_task(
                self._health_monitor_loop()
            )

            self._initialized = True
            logger.info(f"[PrimeClient] Initialized, status={self._status.value}")

    async def close(self) -> None:
        """Close the client and cleanup resources."""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        await self._pool.close()
        self._initialized = False
        logger.info("[PrimeClient] Closed")

    # -----------------------------------------------------------------
    # v232.0: GCP VM endpoint hot-swap
    # -----------------------------------------------------------------

    async def update_endpoint(self, host: str, port: int) -> bool:
        """
        v232.0: Hot-swap the Prime endpoint to a new host:port.

        Used when GCP VM becomes ready and should replace the local endpoint.
        Validates the new endpoint before switching.  Keeps old config for fallback.

        Returns True if endpoint was updated and is healthy.
        """
        async with self._lock:
            old_host = self._config.prime_host
            old_port = self._config.prime_port

            # Skip if already pointing there
            if old_host == host and old_port == port:
                logger.debug(f"[PrimeClient] Endpoint already at {host}:{port}, skipping")
                return self._status in (PrimeStatus.AVAILABLE, PrimeStatus.DEGRADED)

            logger.info(
                f"[PrimeClient] v232.0: Endpoint promotion: {old_host}:{old_port} -> {host}:{port}"
            )

            # Store old config for fallback
            self._fallback_host = old_host
            self._fallback_port = old_port

            # Update config — health_url property auto-recomputes
            self._config.prime_host = host
            self._config.prime_port = port

            # Reinitialize connection pool for the new endpoint
            try:
                await self._pool.close()
            except Exception:
                pass
            self._pool = PrimeConnectionPool(self._config)
            await self._pool.initialize()

            # Reset circuit breaker for fresh start
            self._circuit = PrimeCircuitBreaker(self._config)

            # Run health check against new endpoint
            new_status = await self._check_health()

            if new_status in (PrimeStatus.AVAILABLE, PrimeStatus.DEGRADED):
                logger.info(
                    f"[PrimeClient] v232.0: GCP endpoint healthy ({new_status.value}), "
                    f"promotion complete: {host}:{port}"
                )
                self._endpoint_source = "gcp_vm"
                self._consecutive_gcp_failures = 0
                return True
            else:
                # Revert to old endpoint
                logger.warning(
                    f"[PrimeClient] v232.0: GCP endpoint unhealthy ({new_status.value}), "
                    f"reverting to {old_host}:{old_port}"
                )
                self._config.prime_host = old_host
                self._config.prime_port = old_port
                try:
                    await self._pool.close()
                except Exception:
                    pass
                self._pool = PrimeConnectionPool(self._config)
                await self._pool.initialize()
                self._circuit = PrimeCircuitBreaker(self._config)
                await self._check_health()
                self._endpoint_source = "local"
                self._fallback_host = None
                self._fallback_port = None
                return False

    async def demote_to_fallback(self) -> bool:
        """
        v232.0: Demote back to the fallback (local) endpoint.

        Called when the current GCP endpoint becomes unhealthy after
        consecutive failures exceed the demotion threshold.

        Returns True if successfully reverted, False if no fallback available.
        """
        if self._fallback_host is None:
            return False

        async with self._lock:
            logger.info(
                f"[PrimeClient] v232.0: Demoting from GCP "
                f"({self._config.prime_host}:{self._config.prime_port}) "
                f"back to local ({self._fallback_host}:{self._fallback_port})"
            )
            self._config.prime_host = self._fallback_host
            self._config.prime_port = self._fallback_port

            try:
                await self._pool.close()
            except Exception:
                pass
            self._pool = PrimeConnectionPool(self._config)
            await self._pool.initialize()
            self._circuit = PrimeCircuitBreaker(self._config)
            await self._check_health()

            self._endpoint_source = "local"
            self._fallback_host = None
            self._fallback_port = None
            self._consecutive_gcp_failures = 0
            return True

    async def _health_monitor_loop(self) -> None:
        """Background health monitoring loop with timeout protection."""
        shutdown_event = get_shutdown_event()
        max_iterations = int(os.getenv("PRIME_HEALTH_MAX_ITERATIONS", "0")) or None
        iteration = 0

        while True:
            # Check for shutdown
            if shutdown_event.is_set():
                logger.info("[PrimeClient] Health monitor stopped via shutdown event")
                break

            # Check max iterations (for testing/safety)
            if max_iterations and iteration >= max_iterations:
                logger.info(f"[PrimeClient] Health monitor reached max iterations ({max_iterations})")
                break

            iteration += 1

            try:
                await asyncio.sleep(self._config.health_check_interval)
                # Add timeout protection for health check
                await asyncio.wait_for(
                    self._check_health(),
                    timeout=TimeoutConfig.HEALTH_CHECK
                )

                # v232.0: Auto-demote GCP endpoint after consecutive failures
                if (
                    self._endpoint_source == "gcp_vm"
                    and self._status == PrimeStatus.UNAVAILABLE
                    and self._fallback_host is not None
                ):
                    self._consecutive_gcp_failures += 1
                    _demote_threshold = int(os.getenv("PRIME_GCP_DEMOTE_THRESHOLD", "3"))
                    if self._consecutive_gcp_failures >= _demote_threshold:
                        logger.warning(
                            f"[PrimeClient] v232.0: GCP endpoint failed "
                            f"{self._consecutive_gcp_failures} times, demoting to local"
                        )
                        await self.demote_to_fallback()
                elif self._endpoint_source == "gcp_vm" and self._status in (
                    PrimeStatus.AVAILABLE, PrimeStatus.DEGRADED
                ):
                    self._consecutive_gcp_failures = 0

                # v232.0: Belt-and-suspenders env-var polling for GCP promotion
                _hollow_active = os.environ.get(
                    "JARVIS_HOLLOW_CLIENT_ACTIVE", ""
                ).lower() == "true"
                if _hollow_active and self._endpoint_source == "local":
                    _new_ip = os.environ.get("JARVIS_INVINCIBLE_NODE_IP", "")
                    _new_port_str = os.environ.get("JARVIS_INVINCIBLE_NODE_PORT", "")
                    if _new_ip and _new_port_str:
                        try:
                            _new_port = int(_new_port_str)
                            if _new_ip != self._config.prime_host or _new_port != self._config.prime_port:
                                logger.info(
                                    f"[PrimeClient] v232.0: Detected GCP VM via env vars: "
                                    f"{_new_ip}:{_new_port}"
                                )
                                await self.update_endpoint(_new_ip, _new_port)
                        except (ValueError, Exception) as _env_err:
                            logger.debug(f"[PrimeClient] Env var promotion check failed: {_env_err}")
                elif not _hollow_active and self._endpoint_source == "gcp_vm":
                    logger.info("[PrimeClient] v232.0: Hollow client deactivated, demoting")
                    await self.demote_to_fallback()

            except asyncio.TimeoutError:
                logger.warning(f"[PrimeClient] Health check timed out after {TimeoutConfig.HEALTH_CHECK}s")
            except asyncio.CancelledError:
                logger.info("[PrimeClient] Health monitor cancelled")
                break
            except Exception as e:
                logger.debug(f"[PrimeClient] Health check error: {e}")

    async def _check_health(self) -> PrimeStatus:
        """Check Prime health status."""
        try:
            async with self._pool.get_session() as session:
                # Handle both aiohttp and httpx
                if hasattr(session, 'get'):
                    # aiohttp style
                    async with session.get(
                        self._config.health_url,
                        timeout=self._config.health_check_timeout
                    ) as resp:
                        if resp.status == 200:
                            self._status = PrimeStatus.AVAILABLE
                        elif resp.status < 500:
                            self._status = PrimeStatus.DEGRADED
                        else:
                            self._status = PrimeStatus.UNAVAILABLE
                else:
                    # httpx style
                    resp = await session.get(self._config.health_url)
                    if resp.status == 200:
                        self._status = PrimeStatus.AVAILABLE
                    elif resp.status < 500:
                        self._status = PrimeStatus.DEGRADED
                    else:
                        self._status = PrimeStatus.UNAVAILABLE

            self._last_health_check = time.time()
            return self._status

        except Exception as e:
            logger.debug(f"[PrimeClient] Health check failed: {e}")
            self._status = PrimeStatus.UNAVAILABLE
            self._last_health_check = time.time()
            return self._status

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        context: Optional[List[Dict[str, str]]] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs
    ) -> PrimeResponse:
        """
        Generate a response from Prime.

        Args:
            prompt: The user prompt
            system_prompt: Optional system prompt
            context: Optional conversation history
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            **kwargs: Additional parameters

        Returns:
            PrimeResponse with the generated content
        """
        request = PrimeRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            context=context,
            max_tokens=max_tokens,
            temperature=temperature,
            metadata=kwargs,
        )

        return await self._execute_request(request)

    async def generate_with_fallback(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        context: Optional[List[Dict[str, str]]] = None,
        fallback_to_cloud: bool = True,
        **kwargs
    ) -> PrimeResponse:
        """
        Generate with automatic fallback to cloud if Prime fails.

        This is the recommended method for production use.
        """
        request = PrimeRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            context=context,
            metadata=kwargs,
        )

        # Try Prime first
        if await self._circuit.can_execute() and self._status != PrimeStatus.UNAVAILABLE:
            try:
                response = await self._execute_request(request)
                return response
            except Exception as e:
                logger.warning(f"[PrimeClient] Prime request failed: {e}")
                if not fallback_to_cloud or not self._config.enable_cloud_fallback:
                    raise

        # Fallback to cloud
        if fallback_to_cloud and self._config.enable_cloud_fallback:
            return await self._execute_cloud_fallback(request)

        # No fallback available
        raise RuntimeError("Prime unavailable and cloud fallback disabled")

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        context: Optional[List[Dict[str, str]]] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """
        Generate streaming response from Prime.

        Yields content chunks as they arrive.
        """
        request = PrimeRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            context=context,
            stream=True,
            metadata=kwargs,
        )

        async for chunk in self._execute_stream_request(request):
            yield chunk

    async def _execute_request(self, request: PrimeRequest) -> PrimeResponse:
        """Execute a request to Prime."""
        if not self._initialized:
            await self.initialize()

        # Check circuit breaker
        if not await self._circuit.can_execute():
            raise RuntimeError("Circuit breaker is open - Prime appears unhealthy")

        start_time = time.time()

        try:
            payload = self._build_payload(request)

            async with self._pool.get_session() as session:
                # Use OpenAI-compatible chat completions endpoint
                url = f"{self._config.base_url}/chat/completions"

                # aiohttp.ClientSession - use context manager for response
                try:
                    async with session.post(url, json=payload) as resp:
                        if resp.status != 200:
                            text = await resp.text()
                            raise RuntimeError(f"Prime returned {resp.status}: {text}")
                        data = await resp.json()
                except TypeError:
                    # Fallback for httpx style (no context manager on response)
                    resp = await session.post(url, json=payload)
                    if resp.status_code != 200:
                        raise RuntimeError(f"Prime returned {resp.status_code}: {resp.text}")
                    data = resp.json()

            latency_ms = (time.time() - start_time) * 1000

            await self._circuit.record_success()

            # Parse OpenAI-compatible response format
            content = ""
            if "choices" in data and len(data["choices"]) > 0:
                choice = data["choices"][0]
                if "message" in choice:
                    content = choice["message"].get("content", "")
                elif "text" in choice:
                    content = choice["text"]
            else:
                # Fallback for non-standard responses
                content = data.get("content", data.get("response", ""))

            # Extract token usage
            usage = data.get("usage", {})
            tokens_used = usage.get("total_tokens", 0)

            return PrimeResponse(
                content=content,
                request_id=request.request_id,
                model=data.get("model", "jarvis-prime"),
                source="local_prime",
                latency_ms=latency_ms,
                tokens_used=tokens_used,
                metadata=data.get("metadata", {}),
            )

        except Exception as e:
            await self._circuit.record_failure()
            logger.error(f"[PrimeClient] Request failed: {e}")
            raise

    async def _execute_stream_request(
        self,
        request: PrimeRequest
    ) -> AsyncGenerator[str, None]:
        """Execute a streaming request to Prime."""
        if not self._initialized:
            await self.initialize()

        if not await self._circuit.can_execute():
            raise RuntimeError("Circuit breaker is open")

        try:
            payload = self._build_payload(request)
            payload["stream"] = True

            async with self._pool.get_session() as session:
                url = f"{self._config.base_url}/generate/stream"

                if hasattr(session, 'post') and hasattr(session.post, '__aenter__'):
                    # aiohttp style
                    async with session.post(url, json=payload) as resp:
                        if resp.status != 200:
                            text = await resp.text()
                            raise RuntimeError(f"Prime returned {resp.status}: {text}")

                        async for line in resp.content:
                            if line:
                                decoded = line.decode('utf-8').strip()
                                if decoded.startswith("data: "):
                                    data = json.loads(decoded[6:])
                                    if "content" in data:
                                        yield data["content"]
                else:
                    # httpx style (async streaming)
                    async with session.stream('POST', url, json=payload) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"Prime returned {resp.status}")

                        async for line in resp.aiter_lines():
                            if line.startswith("data: "):
                                data = json.loads(line[6:])
                                if "content" in data:
                                    yield data["content"]

            await self._circuit.record_success()

        except Exception as e:
            await self._circuit.record_failure()
            logger.error(f"[PrimeClient] Stream request failed: {e}")
            raise

    async def _execute_cloud_fallback(self, request: PrimeRequest) -> PrimeResponse:
        """Execute fallback to cloud Claude API."""
        start_time = time.time()

        try:
            # Import graceful degradation for cloud fallback
            try:
                from backend.core.graceful_degradation import get_degradation, InferenceTarget
                degradation = get_degradation()
            except ImportError:
                degradation = None

            # Try to use existing Claude client
            if self._cloud_client is None:
                try:
                    from anthropic import AsyncAnthropic
                    api_key = os.getenv("ANTHROPIC_API_KEY")
                    if api_key:
                        self._cloud_client = AsyncAnthropic(api_key=api_key)
                except ImportError:
                    pass

            if self._cloud_client is None:
                raise RuntimeError("Cloud Claude client not available")

            # Build messages for Claude
            messages = []
            if request.context:
                messages.extend(request.context)
            messages.append({"role": "user", "content": request.prompt})

            # Call Claude API
            response = await self._cloud_client.messages.create(
                model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
                max_tokens=request.max_tokens,
                system=request.system_prompt or "You are JARVIS, an intelligent AI assistant.",
                messages=messages,
            )

            latency_ms = (time.time() - start_time) * 1000

            # Update degradation status if available
            if degradation:
                degradation.update_target_health(
                    InferenceTarget.CLOUD_CLAUDE,
                    degradation._targets[InferenceTarget.CLOUD_CLAUDE].health
                )

            content = response.content[0].text if response.content else ""

            return PrimeResponse(
                content=content,
                request_id=request.request_id,
                model=response.model,
                source="cloud_claude",
                latency_ms=latency_ms,
                tokens_used=response.usage.output_tokens if response.usage else 0,
                fallback_used=True,
                fallback_reason="prime_unavailable",
                metadata={"usage": response.usage.model_dump() if response.usage else {}},
            )

        except Exception as e:
            logger.error(f"[PrimeClient] Cloud fallback failed: {e}")
            raise RuntimeError(f"Both Prime and cloud fallback failed: {e}")

    def _build_payload(self, request: PrimeRequest) -> Dict[str, Any]:
        """Build request payload for Prime API (OpenAI-compatible format)."""
        # Build messages list in OpenAI format
        messages = []

        # Add system prompt if provided
        if request.system_prompt:
            messages.append({
                "role": "system",
                "content": request.system_prompt
            })

        # Add context as system message if provided
        if request.context:
            context_str = str(request.context) if not isinstance(request.context, str) else request.context
            messages.append({
                "role": "system",
                "content": f"Context: {context_str}"
            })

        # Add the user prompt
        messages.append({
            "role": "user",
            "content": request.prompt
        })

        payload = {
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "model": "jarvis-prime",  # Required by OpenAI format
        }

        if request.metadata:
            payload["metadata"] = request.metadata

        return payload

    def get_status(self) -> Dict[str, Any]:
        """Get current client status."""
        return {
            "status": self._status.value,
            "initialized": self._initialized,
            "last_health_check": self._last_health_check,
            "circuit_breaker": self._circuit.get_state(),
            "config": {
                "base_url": self._config.base_url,
                "pool_size": self._config.pool_size,
                "enable_cloud_fallback": self._config.enable_cloud_fallback,
            },
        }

    @property
    def is_available(self) -> bool:
        """Check if Prime is currently available."""
        return self._status in (PrimeStatus.AVAILABLE, PrimeStatus.DEGRADED)


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_prime_client: Optional[PrimeClient] = None
_client_lock: Optional[asyncio.Lock] = None  # v90.0: Lazy lock initialization


async def get_prime_client(config: Optional[PrimeClientConfig] = None) -> PrimeClient:
    """
    Get the singleton PrimeClient instance.

    Thread-safe with double-check locking.
    """
    global _prime_client, _client_lock

    if _prime_client is not None and _prime_client._initialized:
        return _prime_client

    # v90.0: Lazy lock creation to avoid "no event loop" errors at module load
    if _client_lock is None:
        _client_lock = asyncio.Lock()

    async with _client_lock:
        if _prime_client is not None and _prime_client._initialized:
            return _prime_client

        _prime_client = PrimeClient(config)
        await _prime_client.initialize()
        return _prime_client


async def close_prime_client() -> None:
    """Close the singleton client."""
    global _prime_client

    if _prime_client:
        await _prime_client.close()
        _prime_client = None
