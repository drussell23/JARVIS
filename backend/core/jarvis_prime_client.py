"""
JARVIS-Prime Intelligent Client - Memory-Aware Hybrid Routing
==============================================================

A sophisticated client for JARVIS-Prime (Tier-0 Brain) with:
- Memory-aware routing (local vs Cloud Run)
- Multi-tier fallback chain (Local → Cloud Run → Gemini API)
- Circuit breaker pattern for resilience
- Connection pooling and async operations
- Dynamic threshold adjustment based on system state
- Real-time telemetry and cost tracking

Architecture:
    ┌──────────────────────────────────────────────────────────────┐
    │              JarvisPrimeClient (Memory-Aware)                 │
    │  ┌────────────────────────────────────────────────────────┐  │
    │  │                Memory Pressure Monitor                  │  │
    │  │  RAM > 8GB → LOCAL    RAM < 8GB → CLOUD    < 4GB → API │  │
    │  └─────────────────────────┬──────────────────────────────┘  │
    │                            │                                  │
    │  ┌─────────────┐   ┌──────┴─────┐   ┌──────────────────┐    │
    │  │   Local     │   │  Cloud Run │   │   Gemini API     │    │
    │  │ (Port 8002) │→→→│  (GCR URL) │→→→│  (Fallback)      │    │
    │  └─────────────┘   └────────────┘   └──────────────────┘    │
    │         ↓                ↓                   ↓               │
    │  ┌──────────────────────────────────────────────────────────┐│
    │  │              CircuitBreaker + Retry Logic                ││
    │  └──────────────────────────────────────────────────────────┘│
    └──────────────────────────────────────────────────────────────┘

Version: 1.0.0
Author: JARVIS AI System
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Callable, Awaitable

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class JarvisPrimeConfig:
    """Configuration for the JARVIS-Prime client."""

    # Memory thresholds (configurable via env vars)
    memory_threshold_local_gb: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_MEMORY_THRESHOLD_GB", "8.0"))
    )
    memory_threshold_cloud_gb: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_MEMORY_THRESHOLD_CLOUD_GB", "4.0"))
    )

    # Local JARVIS-Prime settings
    local_host: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_HOST", "127.0.0.1")
    )
    local_port: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_PORT", "8002"))
    )

    # Cloud Run settings
    cloud_run_url: str = field(
        default_factory=lambda: os.getenv(
            "JARVIS_PRIME_CLOUD_RUN_URL",
            "https://jarvis-prime-dev-888774109345.us-central1.run.app"
        )
    )
    use_cloud_run: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_USE_CLOUD_RUN", "true").lower() == "true"
    )

    # Gemini fallback settings
    gemini_api_key: str = field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY", "")
    )
    gemini_model: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_GEMINI_MODEL", "gemini-1.5-flash")
    )
    use_gemini_fallback: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_GEMINI_FALLBACK", "true").lower() == "true"
    )

    # Timeouts
    local_timeout_ms: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_LOCAL_TIMEOUT_MS", "5000"))
    )
    cloud_timeout_ms: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_CLOUD_TIMEOUT_MS", "30000"))
    )
    api_timeout_ms: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_API_TIMEOUT_MS", "10000"))
    )

    # Circuit breaker settings
    circuit_breaker_threshold: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_CB_THRESHOLD", "3"))
    )
    circuit_breaker_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_CB_TIMEOUT_S", "60"))
    )

    # Retry settings
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_MAX_RETRIES", "2"))
    )
    retry_delay_ms: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_RETRY_DELAY_MS", "500"))
    )

    # Force mode (for testing/override)
    force_mode: Optional[str] = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_FORCE_MODE", None)
    )


class RoutingMode(str, Enum):
    """Routing mode for JARVIS-Prime requests."""
    LOCAL = "local"           # Local subprocess (free, fast)
    CLOUD_RUN = "cloud_run"   # Cloud Run (pay-per-use)
    GEMINI_API = "gemini_api" # Gemini API fallback (cheapest)
    DISABLED = "disabled"     # All backends unavailable


class CircuitState(str, Enum):
    """Circuit breaker state."""
    CLOSED = "closed"     # Normal operation
    OPEN = "open"         # Failing, skip calls
    HALF_OPEN = "half_open"  # Testing recovery


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ChatMessage:
    """A chat message."""
    role: str  # "system", "user", "assistant"
    content: str


@dataclass
class CompletionResponse:
    """Response from a completion request."""
    success: bool
    content: Optional[str] = None
    error: Optional[str] = None
    latency_ms: float = 0.0
    backend: str = "unknown"
    tokens_used: int = 0
    cost_estimate: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthStatus:
    """Health status of a backend."""
    available: bool
    latency_ms: float = 0.0
    model_loaded: bool = False
    error: Optional[str] = None
    last_check: float = field(default_factory=time.time)


# =============================================================================
# Circuit Breaker
# =============================================================================

class CircuitBreaker:
    """
    Circuit breaker pattern for backend resilience.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, skip requests
    - HALF_OPEN: Testing if backend recovered
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        timeout_seconds: float = 60,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._success_count = 0

    @property
    def state(self) -> CircuitState:
        """Get current state, checking for timeout."""
        if self._state == CircuitState.OPEN:
            if self._last_failure_time and \
               (time.time() - self._last_failure_time) > self.timeout_seconds:
                logger.info(f"[CircuitBreaker:{self.name}] Transitioning OPEN → HALF_OPEN")
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
        return self._state

    def allow_request(self) -> bool:
        """Check if request should be allowed."""
        state = self.state
        if state == CircuitState.CLOSED:
            return True
        elif state == CircuitState.HALF_OPEN:
            return True  # Allow test request
        else:  # OPEN
            return False

    def record_success(self):
        """Record a successful call."""
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= 2:  # 2 successes to close
                logger.info(f"[CircuitBreaker:{self.name}] Transitioning HALF_OPEN → CLOSED")
                self._state = CircuitState.CLOSED
                self._failure_count = 0
        else:
            self._failure_count = 0

    def record_failure(self):
        """Record a failed call."""
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._state == CircuitState.HALF_OPEN:
            logger.warning(f"[CircuitBreaker:{self.name}] Transitioning HALF_OPEN → OPEN")
            self._state = CircuitState.OPEN
        elif self._failure_count >= self.failure_threshold:
            logger.warning(
                f"[CircuitBreaker:{self.name}] Transitioning CLOSED → OPEN "
                f"(failures: {self._failure_count})"
            )
            self._state = CircuitState.OPEN


# =============================================================================
# Memory Monitor
# =============================================================================

class MemoryMonitor:
    """Monitors system memory for routing decisions."""

    def __init__(self):
        self._psutil = None
        self._last_check: Optional[float] = None
        self._cached_available_gb: float = 16.0  # Assume plenty
        self._cache_ttl_seconds = 5.0

    def _get_psutil(self):
        """Lazy load psutil."""
        if self._psutil is None:
            try:
                import psutil
                self._psutil = psutil
            except ImportError:
                logger.warning("[MemoryMonitor] psutil not available")
        return self._psutil

    def get_available_gb(self) -> float:
        """Get available memory in GB with caching."""
        now = time.time()

        # Use cache if fresh
        if self._last_check and (now - self._last_check) < self._cache_ttl_seconds:
            return self._cached_available_gb

        psutil = self._get_psutil()
        if psutil is None:
            return 16.0  # Optimistic default

        try:
            mem = psutil.virtual_memory()
            self._cached_available_gb = mem.available / (1024 ** 3)
            self._last_check = now
            return self._cached_available_gb
        except Exception as e:
            logger.debug(f"[MemoryMonitor] Error: {e}")
            return self._cached_available_gb

    def get_pressure_percent(self) -> float:
        """Get memory pressure as percentage (0-100)."""
        psutil = self._get_psutil()
        if psutil is None:
            return 0.0

        try:
            mem = psutil.virtual_memory()
            return mem.percent
        except Exception:
            return 0.0


# =============================================================================
# JARVIS-Prime Client
# =============================================================================

class JarvisPrimeClient:
    """
    Intelligent client for JARVIS-Prime with memory-aware routing.

    Features:
    - Automatic mode selection based on available RAM
    - Multi-tier fallback (Local → Cloud Run → Gemini)
    - Circuit breaker for each backend
    - Connection pooling and async operations
    - Cost tracking and telemetry
    - Dynamic memory monitoring with mode switching
    """

    def __init__(self, config: Optional[JarvisPrimeConfig] = None):
        self.config = config or JarvisPrimeConfig()

        # Memory monitor
        self._memory_monitor = MemoryMonitor()

        # Circuit breakers for each backend
        self._circuit_breakers = {
            RoutingMode.LOCAL: CircuitBreaker(
                "local",
                failure_threshold=self.config.circuit_breaker_threshold,
                timeout_seconds=self.config.circuit_breaker_timeout_s,
            ),
            RoutingMode.CLOUD_RUN: CircuitBreaker(
                "cloud_run",
                failure_threshold=self.config.circuit_breaker_threshold,
                timeout_seconds=self.config.circuit_breaker_timeout_s,
            ),
            RoutingMode.GEMINI_API: CircuitBreaker(
                "gemini_api",
                failure_threshold=self.config.circuit_breaker_threshold,
                timeout_seconds=self.config.circuit_breaker_timeout_s * 2,  # Longer for API
            ),
        }

        # Health status cache
        self._health_cache: Dict[RoutingMode, HealthStatus] = {}
        self._health_cache_ttl = 30.0  # seconds

        # HTTP client (lazy loaded)
        self._http_client = None

        # Stats
        self._request_count = 0
        self._local_count = 0
        self._cloud_count = 0
        self._api_count = 0
        self._fallback_count = 0
        self._total_cost = 0.0

        # Dynamic monitoring
        self._current_mode: Optional[RoutingMode] = None
        self._mode_change_callbacks: List[Callable[[RoutingMode, RoutingMode, str], Awaitable[None]]] = []
        self._monitoring_task: Optional[asyncio.Task] = None
        self._monitoring_interval_seconds = float(os.getenv("JARVIS_PRIME_MONITOR_INTERVAL", "30"))
        self._shutdown_event = asyncio.Event()

        logger.info(
            f"[JarvisPrimeClient] Initialized with thresholds: "
            f"local>{self.config.memory_threshold_local_gb}GB, "
            f"cloud>{self.config.memory_threshold_cloud_gb}GB"
        )

    async def _get_http_client(self):
        """Lazy load HTTP client."""
        if self._http_client is None:
            try:
                import httpx
                self._http_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(60.0, connect=10.0),
                    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                )
            except ImportError:
                logger.warning("[JarvisPrimeClient] httpx not available")
        return self._http_client

    # =========================================================================
    # Mode Selection
    # =========================================================================

    def decide_mode(self) -> Tuple[RoutingMode, str]:
        """
        Decide which routing mode to use based on memory and availability.

        Priority Order:
        1. Local (RAM >= 8GB) - FREE, fastest
        2. Cloud Run (always available if configured) - ~$0.02/request
        3. Gemini API (if API key configured) - cheapest fallback

        Returns:
            Tuple of (mode, reason)
        """
        # Check for forced mode (testing/override)
        if self.config.force_mode:
            mode = RoutingMode(self.config.force_mode)
            return mode, f"Forced mode: {mode.value}"

        available_gb = self._memory_monitor.get_available_gb()

        # Decision logic
        # Priority 1: Local if we have enough RAM
        if available_gb >= self.config.memory_threshold_local_gb:
            cb = self._circuit_breakers[RoutingMode.LOCAL]
            if cb.allow_request():
                return RoutingMode.LOCAL, f"Sufficient RAM ({available_gb:.1f}GB >= {self.config.memory_threshold_local_gb}GB)"
            else:
                logger.info(f"[JarvisPrimeClient] Local circuit breaker OPEN, trying Cloud Run")

        # Priority 2: Cloud Run (lightweight HTTP call, works even with low RAM)
        # Cloud Run is always available regardless of memory since it's just an HTTP call
        if self.config.use_cloud_run and self.config.cloud_run_url:
            cb = self._circuit_breakers[RoutingMode.CLOUD_RUN]
            if cb.allow_request():
                if available_gb < self.config.memory_threshold_local_gb:
                    return RoutingMode.CLOUD_RUN, f"Low RAM ({available_gb:.1f}GB) - using Cloud Run"
                else:
                    return RoutingMode.CLOUD_RUN, f"Cloud Run available (RAM: {available_gb:.1f}GB)"
            else:
                logger.info(f"[JarvisPrimeClient] Cloud Run circuit breaker OPEN, trying Gemini")

        # Priority 3: Gemini API (if configured)
        if self.config.use_gemini_fallback and self.config.gemini_api_key:
            cb = self._circuit_breakers[RoutingMode.GEMINI_API]
            if cb.allow_request():
                return RoutingMode.GEMINI_API, f"Using Gemini API fallback (RAM: {available_gb:.1f}GB)"
            else:
                logger.info(f"[JarvisPrimeClient] Gemini circuit breaker OPEN")

        # Priority 4: Try local anyway (might work with some memory pressure)
        if available_gb >= 2.0:  # Minimum 2GB for any operation
            cb = self._circuit_breakers[RoutingMode.LOCAL]
            if cb.allow_request():
                return RoutingMode.LOCAL, f"Attempting local with low RAM ({available_gb:.1f}GB)"

        return RoutingMode.DISABLED, f"All backends unavailable (RAM: {available_gb:.1f}GB)"

    # =========================================================================
    # Health Checks
    # =========================================================================

    async def check_health(self, mode: RoutingMode) -> HealthStatus:
        """Check health of a specific backend."""
        now = time.time()

        # Use cache if fresh
        if mode in self._health_cache:
            cached = self._health_cache[mode]
            if (now - cached.last_check) < self._health_cache_ttl:
                return cached

        if mode == RoutingMode.LOCAL:
            status = await self._check_local_health()
        elif mode == RoutingMode.CLOUD_RUN:
            status = await self._check_cloud_run_health()
        elif mode == RoutingMode.GEMINI_API:
            status = await self._check_gemini_health()
        else:
            status = HealthStatus(available=False, error="Unknown mode")

        self._health_cache[mode] = status
        return status

    async def _check_local_health(self) -> HealthStatus:
        """Check local JARVIS-Prime health."""
        client = await self._get_http_client()
        if client is None:
            return HealthStatus(available=False, error="HTTP client not available")

        url = f"http://{self.config.local_host}:{self.config.local_port}/health"
        start = time.time()

        try:
            resp = await client.get(url, timeout=5.0)
            latency = (time.time() - start) * 1000

            if resp.status_code == 200:
                data = resp.json()
                return HealthStatus(
                    available=True,
                    latency_ms=latency,
                    model_loaded=data.get("model_loaded", False),
                )
            else:
                return HealthStatus(
                    available=False,
                    error=f"HTTP {resp.status_code}",
                )
        except Exception as e:
            return HealthStatus(available=False, error=str(e))

    async def _check_cloud_run_health(self) -> HealthStatus:
        """Check Cloud Run JARVIS-Prime health."""
        if not self.config.cloud_run_url:
            return HealthStatus(available=False, error="Cloud Run URL not configured")

        client = await self._get_http_client()
        if client is None:
            return HealthStatus(available=False, error="HTTP client not available")

        url = f"{self.config.cloud_run_url}/health"
        start = time.time()

        try:
            resp = await client.get(url, timeout=30.0)  # Longer for cold start
            latency = (time.time() - start) * 1000

            if resp.status_code == 200:
                data = resp.json()
                # Service is available if it responds with 200
                # Model might not be loaded, but we can still route to it
                return HealthStatus(
                    available=True,
                    latency_ms=latency,
                    model_loaded=data.get("model_loaded", False),
                )
            else:
                return HealthStatus(available=False, error=f"HTTP {resp.status_code}")
        except asyncio.TimeoutError:
            return HealthStatus(available=False, error="Connection timeout (cold start?)")
        except Exception as e:
            logger.debug(f"[JarvisPrimeClient] Cloud Run health check error: {e}")
            return HealthStatus(available=False, error=str(e))

    async def _check_gemini_health(self) -> HealthStatus:
        """Check Gemini API availability."""
        if not self.config.gemini_api_key:
            return HealthStatus(available=False, error="Gemini API key not configured")

        # Just check if we have the key - actual availability checked on call
        return HealthStatus(available=True, model_loaded=True)

    async def is_available(self) -> bool:
        """Check if any backend is available."""
        mode, _ = self.decide_mode()
        if mode == RoutingMode.DISABLED:
            return False

        status = await self.check_health(mode)
        return status.available

    # =========================================================================
    # Completion
    # =========================================================================

    async def complete(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        messages: Optional[List[ChatMessage]] = None,
    ) -> CompletionResponse:
        """
        Complete a prompt with automatic routing and fallback.

        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            max_tokens: Max tokens to generate
            temperature: Sampling temperature
            messages: Optional list of messages (overrides prompt)

        Returns:
            CompletionResponse with result
        """
        self._request_count += 1

        # Decide initial mode
        mode, reason = self.decide_mode()
        logger.info(f"[JarvisPrimeClient] Mode: {mode.value} ({reason})")

        if mode == RoutingMode.DISABLED:
            return CompletionResponse(
                success=False,
                error="All backends unavailable",
                backend="none",
            )

        # Build messages if not provided
        if messages is None:
            messages = []
            if system_prompt:
                messages.append(ChatMessage(role="system", content=system_prompt))
            messages.append(ChatMessage(role="user", content=prompt))

        # Try in order: decided mode → Cloud Run → Gemini
        fallback_order = self._get_fallback_order(mode)

        for try_mode in fallback_order:
            cb = self._circuit_breakers[try_mode]

            if not cb.allow_request():
                logger.debug(f"[JarvisPrimeClient] Skipping {try_mode.value} (circuit breaker OPEN)")
                continue

            try:
                response = await self._execute_completion(
                    mode=try_mode,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )

                if response.success:
                    cb.record_success()
                    self._update_stats(try_mode, response)
                    return response
                else:
                    cb.record_failure()
                    logger.warning(f"[JarvisPrimeClient] {try_mode.value} failed: {response.error}")
                    self._fallback_count += 1

            except Exception as e:
                cb.record_failure()
                logger.warning(f"[JarvisPrimeClient] {try_mode.value} exception: {e}")
                self._fallback_count += 1

        return CompletionResponse(
            success=False,
            error="All backends failed",
            backend="none",
        )

    def _get_fallback_order(self, initial_mode: RoutingMode) -> List[RoutingMode]:
        """Get fallback order based on initial mode."""
        order = []

        if initial_mode == RoutingMode.LOCAL:
            order = [RoutingMode.LOCAL]
            if self.config.use_cloud_run:
                order.append(RoutingMode.CLOUD_RUN)
            if self.config.use_gemini_fallback:
                order.append(RoutingMode.GEMINI_API)

        elif initial_mode == RoutingMode.CLOUD_RUN:
            order = [RoutingMode.CLOUD_RUN]
            if self.config.use_gemini_fallback:
                order.append(RoutingMode.GEMINI_API)

        elif initial_mode == RoutingMode.GEMINI_API:
            order = [RoutingMode.GEMINI_API]

        return order

    async def _execute_completion(
        self,
        mode: RoutingMode,
        messages: List[ChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> CompletionResponse:
        """Execute completion on a specific backend."""
        if mode == RoutingMode.LOCAL:
            return await self._complete_local(messages, max_tokens, temperature)
        elif mode == RoutingMode.CLOUD_RUN:
            return await self._complete_cloud_run(messages, max_tokens, temperature)
        elif mode == RoutingMode.GEMINI_API:
            return await self._complete_gemini(messages, max_tokens, temperature)
        else:
            return CompletionResponse(success=False, error=f"Unknown mode: {mode}")

    async def _complete_local(
        self,
        messages: List[ChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> CompletionResponse:
        """Complete via local JARVIS-Prime."""
        client = await self._get_http_client()
        if client is None:
            return CompletionResponse(success=False, error="HTTP client not available", backend="local")

        url = f"http://{self.config.local_host}:{self.config.local_port}/v1/chat/completions"
        timeout = self.config.local_timeout_ms / 1000.0

        payload = {
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        start = time.time()
        try:
            resp = await client.post(url, json=payload, timeout=timeout)
            latency = (time.time() - start) * 1000

            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return CompletionResponse(
                    success=True,
                    content=content,
                    latency_ms=latency,
                    backend="local",
                    tokens_used=data.get("usage", {}).get("total_tokens", 0),
                    cost_estimate=0.0,  # Local is free
                )
            elif resp.status_code == 503:
                data = resp.json()
                return CompletionResponse(
                    success=False,
                    error=data.get("detail", "Model not loaded"),
                    backend="local",
                )
            else:
                return CompletionResponse(
                    success=False,
                    error=f"HTTP {resp.status_code}",
                    backend="local",
                )
        except Exception as e:
            return CompletionResponse(success=False, error=str(e), backend="local")

    async def _complete_cloud_run(
        self,
        messages: List[ChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> CompletionResponse:
        """Complete via Cloud Run JARVIS-Prime."""
        if not self.config.cloud_run_url:
            return CompletionResponse(success=False, error="Cloud Run URL not configured", backend="cloud_run")

        client = await self._get_http_client()
        if client is None:
            return CompletionResponse(success=False, error="HTTP client not available", backend="cloud_run")

        url = f"{self.config.cloud_run_url}/v1/chat/completions"
        timeout = self.config.cloud_timeout_ms / 1000.0

        payload = {
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        start = time.time()
        try:
            resp = await client.post(url, json=payload, timeout=timeout)
            latency = (time.time() - start) * 1000

            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                tokens = data.get("usage", {}).get("total_tokens", 0)

                # Estimate Cloud Run cost (~$0.02 per request)
                cost = 0.02 if latency > 1000 else 0.01  # Cold start costs more

                return CompletionResponse(
                    success=True,
                    content=content,
                    latency_ms=latency,
                    backend="cloud_run",
                    tokens_used=tokens,
                    cost_estimate=cost,
                )
            elif resp.status_code == 503:
                data = resp.json()
                return CompletionResponse(
                    success=False,
                    error=data.get("detail", "Model not loaded"),
                    backend="cloud_run",
                )
            else:
                return CompletionResponse(
                    success=False,
                    error=f"HTTP {resp.status_code}",
                    backend="cloud_run",
                )
        except Exception as e:
            return CompletionResponse(success=False, error=str(e), backend="cloud_run")

    async def _complete_gemini(
        self,
        messages: List[ChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> CompletionResponse:
        """Complete via Gemini API (fallback)."""
        if not self.config.gemini_api_key:
            return CompletionResponse(success=False, error="Gemini API key not configured", backend="gemini")

        # Try the new google-genai SDK first, fallback to deprecated SDK
        try:
            from google import genai
            use_new_sdk = True
        except ImportError:
            try:
                import google.generativeai as genai_old
                use_new_sdk = False
            except ImportError:
                return CompletionResponse(success=False, error="google-genai not installed", backend="gemini")

        # Build prompt from messages (convert to Gemini format)
        prompt_parts = []
        for m in messages:
            if m.role == "system":
                prompt_parts.append(f"System: {m.content}")
            elif m.role == "user":
                prompt_parts.append(f"User: {m.content}")
            elif m.role == "assistant":
                prompt_parts.append(f"Assistant: {m.content}")
        prompt_parts.append("Assistant:")
        prompt = "\n".join(prompt_parts)

        start = time.time()
        try:
            if use_new_sdk:
                # New google-genai SDK (recommended)
                client = genai.Client(api_key=self.config.gemini_api_key)

                # Use gemini-2.0-flash-lite for fastest/cheapest, fallback to configured model
                model_name = self.config.gemini_model
                if model_name == "gemini-1.5-flash":
                    model_name = "gemini-2.0-flash-lite"  # Upgrade to newer model

                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=prompt,
                    config={
                        "max_output_tokens": max_tokens,
                        "temperature": temperature,
                    },
                )
                content = response.text
            else:
                # Deprecated SDK (backwards compatibility)
                genai_old.configure(api_key=self.config.gemini_api_key)
                model = genai_old.GenerativeModel(self.config.gemini_model)
                response = await asyncio.to_thread(
                    model.generate_content,
                    prompt,
                    generation_config=genai_old.types.GenerationConfig(
                        max_output_tokens=max_tokens,
                        temperature=temperature,
                    ),
                )
                content = response.text

            latency = (time.time() - start) * 1000

            # Estimate Gemini cost (~$0.075/1M input, ~$0.30/1M output for Flash)
            input_tokens = len(prompt.split())
            output_tokens = len(content.split())
            cost = (input_tokens * 0.000000075) + (output_tokens * 0.0000003)

            return CompletionResponse(
                success=True,
                content=content,
                latency_ms=latency,
                backend="gemini",
                tokens_used=input_tokens + output_tokens,
                cost_estimate=cost,
            )
        except Exception as e:
            return CompletionResponse(success=False, error=str(e), backend="gemini")

    # =========================================================================
    # Stats and Cleanup
    # =========================================================================

    def _update_stats(self, mode: RoutingMode, response: CompletionResponse):
        """Update usage statistics."""
        if mode == RoutingMode.LOCAL:
            self._local_count += 1
        elif mode == RoutingMode.CLOUD_RUN:
            self._cloud_count += 1
        elif mode == RoutingMode.GEMINI_API:
            self._api_count += 1

        self._total_cost += response.cost_estimate

        # Record to observability hub (async-safe fire-and-forget)
        asyncio.create_task(self._record_to_observability(mode, response))

    async def _record_to_observability(self, mode: RoutingMode, response: CompletionResponse):
        """Record completion to the observability hub."""
        try:
            from observability import record_llm_call, CostTier

            # Map routing mode to cost tier
            tier_map = {
                RoutingMode.LOCAL: "local",
                RoutingMode.CLOUD_RUN: "cloud_run",
                RoutingMode.GEMINI_API: "gemini_api",
            }

            await record_llm_call(
                input_text=response.metadata.get("input", "")[:500] if response.metadata else "",
                output_text=response.content[:500] if response.content else "",
                tier=tier_map.get(mode, "local"),
                input_tokens=response.metadata.get("input_tokens", 0) if response.metadata else 0,
                output_tokens=response.tokens_used,
                latency_ms=response.latency_ms,
                routing_mode=mode.value,
                routing_reason=response.metadata.get("routing_reason", "") if response.metadata else "",
                memory_available_gb=self._memory_monitor.get_available_gb(),
                cached=response.metadata.get("cached", False) if response.metadata else False,
            )
        except ImportError:
            # Observability not available, skip silently
            pass
        except Exception as e:
            logger.debug(f"Observability recording failed: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get usage statistics."""
        total = self._request_count or 1
        return {
            "total_requests": self._request_count,
            "local_count": self._local_count,
            "cloud_count": self._cloud_count,
            "api_count": self._api_count,
            "fallback_count": self._fallback_count,
            "local_pct": (self._local_count / total) * 100,
            "cloud_pct": (self._cloud_count / total) * 100,
            "api_pct": (self._api_count / total) * 100,
            "total_cost": self._total_cost,
            "memory_available_gb": self._memory_monitor.get_available_gb(),
            "current_mode": self._current_mode.value if self._current_mode else "unknown",
            "monitoring_active": self._monitoring_task is not None and not self._monitoring_task.done(),
            "circuit_breakers": {
                mode.value: cb.state.value
                for mode, cb in self._circuit_breakers.items()
            },
        }

    # =========================================================================
    # Dynamic Memory Monitoring
    # =========================================================================

    def register_mode_change_callback(
        self,
        callback: Callable[[RoutingMode, RoutingMode, str], Awaitable[None]]
    ) -> None:
        """
        Register a callback for mode changes.

        The callback receives (old_mode, new_mode, reason).
        """
        self._mode_change_callbacks.append(callback)

    def unregister_mode_change_callback(
        self,
        callback: Callable[[RoutingMode, RoutingMode, str], Awaitable[None]]
    ) -> None:
        """Unregister a mode change callback."""
        if callback in self._mode_change_callbacks:
            self._mode_change_callbacks.remove(callback)

    async def start_monitoring(self) -> None:
        """
        Start background memory monitoring.

        This periodically checks memory and switches modes if needed.
        """
        if self._monitoring_task and not self._monitoring_task.done():
            logger.warning("[JarvisPrimeClient] Monitoring already active")
            return

        self._shutdown_event.clear()
        self._current_mode, _ = self.decide_mode()
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        logger.info(
            f"[JarvisPrimeClient] Started memory monitoring "
            f"(interval: {self._monitoring_interval_seconds}s)"
        )

    async def stop_monitoring(self) -> None:
        """Stop background memory monitoring."""
        if self._monitoring_task:
            self._shutdown_event.set()
            try:
                await asyncio.wait_for(self._monitoring_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._monitoring_task.cancel()
            self._monitoring_task = None
            logger.info("[JarvisPrimeClient] Stopped memory monitoring")

    async def _monitoring_loop(self) -> None:
        """Background loop for memory monitoring."""
        while not self._shutdown_event.is_set():
            try:
                # Check for mode change
                new_mode, reason = self.decide_mode()

                if self._current_mode and new_mode != self._current_mode:
                    old_mode = self._current_mode
                    self._current_mode = new_mode

                    logger.info(
                        f"[JarvisPrimeClient] Mode change: "
                        f"{old_mode.value} → {new_mode.value} ({reason})"
                    )

                    # Notify callbacks
                    for callback in self._mode_change_callbacks:
                        try:
                            await callback(old_mode, new_mode, reason)
                        except Exception as e:
                            logger.error(f"[JarvisPrimeClient] Callback error: {e}")

                elif not self._current_mode:
                    self._current_mode = new_mode

                # Wait for next check
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self._monitoring_interval_seconds
                    )
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    continue  # Normal timeout, continue monitoring

            except Exception as e:
                logger.error(f"[JarvisPrimeClient] Monitoring error: {e}")
                await asyncio.sleep(5.0)  # Brief pause on error

    async def force_mode_check(self) -> Tuple[RoutingMode, str]:
        """
        Force an immediate mode check and potential switch.

        Returns:
            Tuple of (new_mode, reason)
        """
        new_mode, reason = self.decide_mode()

        if self._current_mode and new_mode != self._current_mode:
            old_mode = self._current_mode
            self._current_mode = new_mode

            logger.info(
                f"[JarvisPrimeClient] Forced mode change: "
                f"{old_mode.value} → {new_mode.value} ({reason})"
            )

            # Notify callbacks
            for callback in self._mode_change_callbacks:
                try:
                    await callback(old_mode, new_mode, reason)
                except Exception as e:
                    logger.error(f"[JarvisPrimeClient] Callback error: {e}")

        return new_mode, reason

    def get_current_mode(self) -> Optional[RoutingMode]:
        """Get the current active routing mode."""
        return self._current_mode

    async def close(self):
        """Clean up resources."""
        await self.stop_monitoring()
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None


# =============================================================================
# Singleton Access
# =============================================================================

_client_instance: Optional[JarvisPrimeClient] = None


def get_jarvis_prime_client() -> JarvisPrimeClient:
    """Get the global JARVIS-Prime client instance."""
    global _client_instance
    if _client_instance is None:
        _client_instance = JarvisPrimeClient()
    return _client_instance


def set_jarvis_prime_client(client: JarvisPrimeClient):
    """Set the global JARVIS-Prime client instance."""
    global _client_instance
    _client_instance = client


async def create_jarvis_prime_client(config: Optional[JarvisPrimeConfig] = None) -> JarvisPrimeClient:
    """Create and configure a new JARVIS-Prime client."""
    global _client_instance
    _client_instance = JarvisPrimeClient(config)
    return _client_instance


# =============================================================================
# Legacy Compatibility Alias
# =============================================================================
# For backward compatibility with existing code that uses JarvisPrimeClientConfig
JarvisPrimeClientConfig = JarvisPrimeConfig


# =============================================================================
# Memory-Aware Mode Decision Helper
# =============================================================================

def decide_jarvis_prime_mode() -> Tuple[str, str, float]:
    """
    Determine the optimal JARVIS-Prime mode based on current memory.

    Returns:
        Tuple of (mode, reason, available_gb)

    Modes:
        - "local": Sufficient RAM (>8GB) - run local subprocess (FREE)
        - "cloud_run": Low RAM (4-8GB) - use Cloud Run (pay-per-use)
        - "gemini_api": Very low RAM (<4GB) - use Gemini API (cheapest fallback)
        - "disabled": All backends unavailable
    """
    client = get_jarvis_prime_client()
    mode, reason = client.decide_mode()
    available_gb = client._memory_monitor.get_available_gb()

    return mode.value, reason, available_gb


async def get_system_memory_status() -> Dict[str, Any]:
    """
    Get comprehensive system memory status for routing decisions.

    Returns:
        Dict with memory info and recommended mode
    """
    try:
        import psutil
        mem = psutil.virtual_memory()

        available_gb = mem.available / (1024 ** 3)
        total_gb = mem.total / (1024 ** 3)
        percent_used = mem.percent

        # Determine recommended mode
        if available_gb >= 8.0:
            recommended_mode = "local"
            mode_reason = "Sufficient RAM for local inference"
        elif available_gb >= 4.0:
            recommended_mode = "cloud_run"
            mode_reason = "Low RAM - use Cloud Run for inference"
        else:
            recommended_mode = "gemini_api"
            mode_reason = "Very low RAM - use Gemini API fallback"

        return {
            "available_gb": round(available_gb, 2),
            "total_gb": round(total_gb, 2),
            "percent_used": round(percent_used, 1),
            "recommended_mode": recommended_mode,
            "mode_reason": mode_reason,
            "can_run_local": available_gb >= 8.0,
            "can_run_cloud": available_gb >= 4.0,
            "timestamp": time.time(),
        }
    except ImportError:
        return {
            "available_gb": 16.0,  # Optimistic default
            "total_gb": 32.0,
            "percent_used": 50.0,
            "recommended_mode": "local",
            "mode_reason": "psutil not available - assuming sufficient RAM",
            "can_run_local": True,
            "can_run_cloud": True,
            "timestamp": time.time(),
        }
