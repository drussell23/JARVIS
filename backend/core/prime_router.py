"""
Prime Router v1.0
=================

Central AI inference router that routes all requests through JARVIS-Prime
with automatic fallback to cloud Claude API.

This module is the KEY INTEGRATION POINT that connects JARVIS to its Trinity
architecture. All AI inference requests flow through here.

ARCHITECTURE:
    User Request
         ↓
    PrimeRouter
         ↓
    ┌──────────────────────────────────────────┐
    │  Route Decision (based on health/config) │
    └──────────────────────────────────────────┘
         ↓                    ↓
    LOCAL PRIME          CLOUD CLAUDE
    (Free, Fast)         (Paid, Reliable)
         ↓                    ↓
    ┌──────────────────────────────────────────┐
    │            Response Fusion               │
    │   (Metrics, Logging, Graceful Degrade)   │
    └──────────────────────────────────────────┘
         ↓
    User Response

USAGE:
    from backend.core.prime_router import get_prime_router

    router = await get_prime_router()

    # Generate response (auto-routes to best available backend)
    response = await router.generate(
        prompt="What is the weather?",
        system_prompt="You are JARVIS."
    )

    # Check routing status
    status = router.get_status()
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, AsyncGenerator

from backend.core.async_safety import LazyAsyncLock

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
            logger.info("[PrimeRouter] v88.0 Ultra coordinator initialized")
            return _ultra_coordinator
        except Exception as e:
            logger.debug(f"[PrimeRouter] v88.0 Ultra coordinator not available: {e}")
            return None


# =============================================================================
# CONFIGURATION
# =============================================================================

def _get_env_bool(key: str, default: bool) -> bool:
    """Get bool from environment."""
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


def _get_env_float(key: str, default: float) -> float:
    """Get float from environment."""
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


@dataclass
class PrimeRouterConfig:
    """Configuration for the Prime router."""
    # Routing strategy
    prefer_local: bool = field(default_factory=lambda: _get_env_bool("PRIME_PREFER_LOCAL", True))
    enable_cloud_fallback: bool = field(default_factory=lambda: _get_env_bool("PRIME_ENABLE_CLOUD_FALLBACK", True))
    enable_metrics: bool = field(default_factory=lambda: _get_env_bool("PRIME_ENABLE_METRICS", True))

    # Performance thresholds
    local_timeout: float = field(default_factory=lambda: _get_env_float("PRIME_LOCAL_TIMEOUT", 30.0))
    cloud_timeout: float = field(default_factory=lambda: _get_env_float("PRIME_CLOUD_TIMEOUT", 60.0))

    # Health thresholds
    min_local_health: float = field(default_factory=lambda: _get_env_float("PRIME_MIN_LOCAL_HEALTH", 0.5))


class RoutingDecision(Enum):
    """Routing decision types."""
    LOCAL_PRIME = "local_prime"
    CLOUD_CLAUDE = "cloud_claude"
    HYBRID = "hybrid"  # Try local first, then cloud
    CACHED = "cached"
    DEGRADED = "degraded"


@dataclass
class RoutingMetrics:
    """Metrics for routing decisions."""
    total_requests: int = 0
    local_requests: int = 0
    cloud_requests: int = 0
    fallback_count: int = 0
    total_latency_ms: float = 0.0
    local_latency_ms: float = 0.0
    cloud_latency_ms: float = 0.0
    errors: int = 0

    @property
    def avg_latency_ms(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_latency_ms / self.total_requests

    @property
    def local_ratio(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.local_requests / self.total_requests


@dataclass
class RouterResponse:
    """Response from the router."""
    content: str
    source: str  # local_prime, cloud_claude, cached, degraded
    latency_ms: float
    model: str
    tokens_used: int = 0
    fallback_used: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# PRIME ROUTER
# =============================================================================

class PrimeRouter:
    """
    Central router for all AI inference requests.

    Routes between local JARVIS-Prime and cloud Claude API based on:
    - Health status of components
    - User preferences
    - Cost optimization
    - Performance requirements
    """

    def __init__(self, config: Optional[PrimeRouterConfig] = None):
        self._config = config or PrimeRouterConfig()
        self._metrics = RoutingMetrics()
        self._prime_client = None
        self._cloud_client = None
        self._graceful_degradation = None
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the router and its clients."""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            logger.info("[PrimeRouter] Initializing...")

            # Initialize Prime client
            try:
                from backend.core.prime_client import get_prime_client
                self._prime_client = await get_prime_client()
                logger.info("[PrimeRouter] Prime client initialized")
            except ImportError:
                try:
                    from core.prime_client import get_prime_client
                    self._prime_client = await get_prime_client()
                    logger.info("[PrimeRouter] Prime client initialized (relative import)")
                except Exception as e:
                    logger.warning(f"[PrimeRouter] Could not initialize Prime client: {e}")
                    self._prime_client = None

            # Initialize graceful degradation
            try:
                from backend.core.graceful_degradation import get_degradation
                self._graceful_degradation = get_degradation()
                logger.info("[PrimeRouter] Graceful degradation initialized")
            except ImportError:
                try:
                    from core.graceful_degradation import get_degradation
                    self._graceful_degradation = get_degradation()
                except Exception as e:
                    logger.debug(f"[PrimeRouter] Graceful degradation not available: {e}")

            # Initialize cloud client (lazy - only if needed)
            self._cloud_client = None

            self._initialized = True
            logger.info("[PrimeRouter] Initialization complete")

    async def _get_cloud_client(self):
        """Get or create cloud Claude client (lazy initialization)."""
        if self._cloud_client is None:
            try:
                from anthropic import AsyncAnthropic
                api_key = os.getenv("ANTHROPIC_API_KEY")
                if api_key:
                    self._cloud_client = AsyncAnthropic(api_key=api_key)
                    logger.info("[PrimeRouter] Cloud Claude client initialized")
            except ImportError:
                logger.warning("[PrimeRouter] anthropic package not available")
        return self._cloud_client

    def _decide_route(self) -> RoutingDecision:
        """Decide which backend to route to."""
        # Check if Prime is available
        prime_available = (
            self._prime_client is not None and
            self._prime_client.is_available
        )

        if self._config.prefer_local and prime_available:
            return RoutingDecision.HYBRID  # Try local first, fallback to cloud
        elif prime_available:
            return RoutingDecision.LOCAL_PRIME
        elif self._config.enable_cloud_fallback:
            return RoutingDecision.CLOUD_CLAUDE
        else:
            return RoutingDecision.DEGRADED

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        context: Optional[List[Dict[str, str]]] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs
    ) -> RouterResponse:
        """
        v88.0: Generate a response with ultra protection stack.

        This is the main entry point for all AI inference requests.
        Now includes v88.0 protection:
        - Adaptive circuit breaker with ML-based prediction
        - Backpressure handling with AIMD rate limiting
        - W3C distributed tracing
        - Timeout enforcement

        Args:
            prompt: User prompt
            system_prompt: System prompt for the model
            context: Conversation history
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            **kwargs: Additional parameters

        Returns:
            RouterResponse with the generated content
        """
        if not self._initialized:
            await self.initialize()

        # v88.0: Use ultra coordinator protection if available
        ultra_coord = await _get_ultra_coordinator()
        if ultra_coord:
            timeout = float(os.getenv("PRIME_ROUTER_TIMEOUT", "90.0"))
            success, result, metadata = await ultra_coord.execute_with_protection(
                component="prime_router",
                operation=lambda: self._generate_internal(
                    prompt, system_prompt, context, max_tokens, temperature, **kwargs
                ),
                timeout=timeout,
            )
            if success and result is not None:
                # Inject trace context into response metadata
                if "trace_id" in metadata:
                    result.metadata["v88_trace_id"] = metadata["trace_id"]
                return result
            elif not success:
                # Protection failed, return degraded response
                error_msg = metadata.get("error", "Unknown protection error")
                logger.warning(f"[PrimeRouter] v88.0 Protection failed: {error_msg}")
                return RouterResponse(
                    content="I'm experiencing some difficulties. Please try again.",
                    source="degraded",
                    latency_ms=0,
                    model="none",
                    metadata={"v88_error": error_msg, "circuit_open": metadata.get("circuit_open", False)},
                )

        # Fallback: direct execution without protection
        return await self._generate_internal(
            prompt, system_prompt, context, max_tokens, temperature, **kwargs
        )

    async def _generate_internal(
        self,
        prompt: str,
        system_prompt: Optional[str],
        context: Optional[List[Dict[str, str]]],
        max_tokens: int,
        temperature: float,
        **kwargs
    ) -> RouterResponse:
        """
        v88.0: Internal generation logic (called by protection wrapper).

        Routes between local JARVIS-Prime and cloud Claude API.
        """
        start_time = time.time()
        self._metrics.total_requests += 1

        routing = self._decide_route()
        logger.debug(f"[PrimeRouter] Routing decision: {routing.value}")

        try:
            if routing == RoutingDecision.HYBRID:
                # Try local first, then cloud
                response = await self._generate_hybrid(
                    prompt, system_prompt, context, max_tokens, temperature, **kwargs
                )
            elif routing == RoutingDecision.LOCAL_PRIME:
                response = await self._generate_local(
                    prompt, system_prompt, context, max_tokens, temperature, **kwargs
                )
            elif routing == RoutingDecision.CLOUD_CLAUDE:
                response = await self._generate_cloud(
                    prompt, system_prompt, context, max_tokens, temperature, **kwargs
                )
            else:
                response = self._generate_degraded(prompt)

            # Update metrics
            latency = (time.time() - start_time) * 1000
            response.latency_ms = latency
            self._metrics.total_latency_ms += latency

            if response.source == "local_prime":
                self._metrics.local_requests += 1
                self._metrics.local_latency_ms += latency
            elif response.source == "cloud_claude":
                self._metrics.cloud_requests += 1
                self._metrics.cloud_latency_ms += latency

            if response.fallback_used:
                self._metrics.fallback_count += 1

            return response

        except Exception as e:
            self._metrics.errors += 1
            logger.error(f"[PrimeRouter] Generation failed: {e}")

            # Return degraded response on error
            return RouterResponse(
                content=f"I apologize, but I'm experiencing technical difficulties. Error: {str(e)}",
                source="degraded",
                latency_ms=(time.time() - start_time) * 1000,
                model="none",
                metadata={"error": str(e)},
            )

    async def _generate_hybrid(
        self,
        prompt: str,
        system_prompt: Optional[str],
        context: Optional[List[Dict[str, str]]],
        max_tokens: int,
        temperature: float,
        **kwargs
    ) -> RouterResponse:
        """Try local Prime first, fall back to cloud on failure."""
        try:
            response = await asyncio.wait_for(
                self._generate_local(
                    prompt, system_prompt, context, max_tokens, temperature, **kwargs
                ),
                timeout=self._config.local_timeout,
            )
            return response
        except Exception as e:
            logger.warning(f"[PrimeRouter] Local generation failed, falling back to cloud: {e}")

            if not self._config.enable_cloud_fallback:
                raise

            response = await self._generate_cloud(
                prompt, system_prompt, context, max_tokens, temperature, **kwargs
            )
            response.fallback_used = True
            response.metadata["fallback_reason"] = str(e)
            return response

    async def _generate_local(
        self,
        prompt: str,
        system_prompt: Optional[str],
        context: Optional[List[Dict[str, str]]],
        max_tokens: int,
        temperature: float,
        **kwargs
    ) -> RouterResponse:
        """Generate using local JARVIS-Prime."""
        if self._prime_client is None:
            raise RuntimeError("Prime client not available")

        response = await self._prime_client.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            context=context,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs
        )

        return RouterResponse(
            content=response.content,
            source="local_prime",
            latency_ms=response.latency_ms,
            model=response.model,
            tokens_used=response.tokens_used,
            metadata=response.metadata,
        )

    async def _generate_cloud(
        self,
        prompt: str,
        system_prompt: Optional[str],
        context: Optional[List[Dict[str, str]]],
        max_tokens: int,
        temperature: float,
        **kwargs
    ) -> RouterResponse:
        """Generate using cloud Claude API."""
        client = await self._get_cloud_client()
        if client is None:
            raise RuntimeError("Cloud client not available")

        # Build messages
        messages = []
        if context:
            messages.extend(context)
        messages.append({"role": "user", "content": prompt})

        start_time = time.time()

        response = await asyncio.wait_for(
            client.messages.create(
                model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
                max_tokens=max_tokens,
                system=system_prompt or "You are JARVIS, an intelligent AI assistant.",
                messages=messages,
            ),
            timeout=self._config.cloud_timeout,
        )

        latency_ms = (time.time() - start_time) * 1000
        content = response.content[0].text if response.content else ""

        return RouterResponse(
            content=content,
            source="cloud_claude",
            latency_ms=latency_ms,
            model=response.model,
            tokens_used=response.usage.output_tokens if response.usage else 0,
            metadata={"usage": response.usage.model_dump() if response.usage else {}},
        )

    def _generate_degraded(self, prompt: str) -> RouterResponse:
        """Return degraded response when no backend available."""
        return RouterResponse(
            content="I apologize, but both local and cloud AI services are currently unavailable. Please try again later.",
            source="degraded",
            latency_ms=0,
            model="none",
            metadata={"reason": "no_backend_available"},
        )

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        context: Optional[List[Dict[str, str]]] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """
        Generate streaming response.

        Yields content chunks as they arrive.
        """
        if not self._initialized:
            await self.initialize()

        # Try local Prime streaming first
        if self._prime_client and self._prime_client.is_available:
            try:
                async for chunk in self._prime_client.generate_stream(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    context=context,
                    **kwargs
                ):
                    yield chunk
                return
            except Exception as e:
                logger.warning(f"[PrimeRouter] Local streaming failed: {e}")

        # Fall back to cloud streaming
        if self._config.enable_cloud_fallback:
            client = await self._get_cloud_client()
            if client:
                messages = context or []
                messages.append({"role": "user", "content": prompt})

                async with client.messages.stream(
                    model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
                    max_tokens=kwargs.get("max_tokens", 4096),
                    system=system_prompt or "You are JARVIS.",
                    messages=messages,
                ) as stream:
                    async for text in stream.text_stream:
                        yield text
                return

        # No streaming available
        yield "Streaming not available - services are offline."

    def get_status(self) -> Dict[str, Any]:
        """Get current router status."""
        return {
            "initialized": self._initialized,
            "config": {
                "prefer_local": self._config.prefer_local,
                "enable_cloud_fallback": self._config.enable_cloud_fallback,
                "local_timeout": self._config.local_timeout,
                "cloud_timeout": self._config.cloud_timeout,
            },
            "prime_client": {
                "available": self._prime_client is not None,
                "status": self._prime_client.get_status() if self._prime_client else None,
            },
            "cloud_client": {
                "available": self._cloud_client is not None,
            },
            "metrics": {
                "total_requests": self._metrics.total_requests,
                "local_requests": self._metrics.local_requests,
                "cloud_requests": self._metrics.cloud_requests,
                "fallback_count": self._metrics.fallback_count,
                "avg_latency_ms": round(self._metrics.avg_latency_ms, 2),
                "local_ratio": round(self._metrics.local_ratio, 3),
                "errors": self._metrics.errors,
            },
        }

    async def close(self) -> None:
        """Close the router and cleanup resources."""
        if self._prime_client:
            await self._prime_client.close()
        if self._cloud_client:
            await self._cloud_client.close()
        self._initialized = False
        logger.info("[PrimeRouter] Closed")


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_prime_router: Optional[PrimeRouter] = None
_router_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_prime_router(config: Optional[PrimeRouterConfig] = None) -> PrimeRouter:
    """
    Get the singleton PrimeRouter instance.

    Thread-safe with double-check locking.
    """
    global _prime_router

    if _prime_router is not None and _prime_router._initialized:
        return _prime_router

    async with _router_lock:
        if _prime_router is not None and _prime_router._initialized:
            return _prime_router

        _prime_router = PrimeRouter(config)
        await _prime_router.initialize()
        return _prime_router


async def close_prime_router() -> None:
    """Close the singleton router."""
    global _prime_router

    if _prime_router:
        await _prime_router.close()
        _prime_router = None
