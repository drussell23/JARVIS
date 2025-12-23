"""
JARVIS-Prime Client - Tier 0 Local Brain HTTP Client
=====================================================

Async HTTP client for communicating with JARVIS-Prime (local brain).
Provides OpenAI-compatible API access with circuit breaker, retries,
and intelligent fallback.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │  TieredCommandRouter                                             │
    │  ├── Tier 0: JarvisPrimeClient (this file) → Local Brain         │
    │  ├── Tier 1: Gemini Flash → Cloud (fast, cheap)                  │
    │  └── Tier 2: Claude Sonnet → Cloud (Computer Use)                │
    └─────────────────────────────────────────────────────────────────┘

Features:
    - OpenAI-compatible /v1/chat/completions API
    - Circuit breaker pattern for failure isolation
    - Exponential backoff retries
    - Streaming support
    - Latency tracking and metrics
    - Connection pooling via aiohttp

Integration:
    from backend.core.jarvis_prime_client import (
        JarvisPrimeClient,
        get_jarvis_prime_client,
    )

Author: JARVIS v5.0 Living OS
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, Union

import aiohttp

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class JarvisPrimeClientConfig:
    """Configuration for JARVIS-Prime HTTP client."""

    # Server connection
    host: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_HOST", "127.0.0.1")
    )
    port: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_PORT", "8000"))
    )

    # Timeouts
    connect_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_CONNECT_TIMEOUT", "5.0"))
    )
    request_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_REQUEST_TIMEOUT", "60.0"))
    )

    # Retry settings
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_MAX_RETRIES", "2"))
    )
    retry_backoff_base: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_RETRY_BACKOFF", "0.5"))
    )

    # Circuit breaker settings
    circuit_breaker_threshold: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_CB_THRESHOLD", "5"))
    )
    circuit_breaker_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_CB_TIMEOUT", "30.0"))
    )

    # Feature flags
    enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_CLIENT_ENABLED", "true").lower() == "true"
    )

    @property
    def base_url(self) -> str:
        """Get the base URL."""
        return f"http://{self.host}:{self.port}"


class CircuitState(str, Enum):
    """Circuit breaker state."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Blocking requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class ClientMetrics:
    """Metrics for the JARVIS-Prime client."""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_latency_ms: float = 0.0
    circuit_trips: int = 0
    last_request_time: Optional[datetime] = None
    last_error: Optional[str] = None

    @property
    def average_latency_ms(self) -> float:
        """Calculate average latency."""
        if self.successful_requests == 0:
            return 0.0
        return self.total_latency_ms / self.successful_requests

    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        if self.total_requests == 0:
            return 1.0
        return self.successful_requests / self.total_requests

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "average_latency_ms": self.average_latency_ms,
            "success_rate": self.success_rate,
            "circuit_trips": self.circuit_trips,
            "last_request_time": self.last_request_time.isoformat() if self.last_request_time else None,
            "last_error": self.last_error,
        }


@dataclass
class ChatMessage:
    """A chat message."""
    role: str  # "system", "user", "assistant"
    content: str

    def to_dict(self) -> Dict[str, str]:
        """Convert to dictionary."""
        return {"role": self.role, "content": self.content}


@dataclass
class ChatResponse:
    """Response from chat completion."""
    success: bool
    content: Optional[str] = None
    model: Optional[str] = None
    finish_reason: Optional[str] = None
    latency_ms: float = 0.0
    error: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "success": self.success,
            "content": self.content,
            "model": self.model,
            "finish_reason": self.finish_reason,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


# =============================================================================
# JARVIS-Prime Client
# =============================================================================

class JarvisPrimeClient:
    """
    Async HTTP client for JARVIS-Prime (Tier 0 local brain).

    Provides OpenAI-compatible API access with:
    - Circuit breaker for failure isolation
    - Retry logic with exponential backoff
    - Streaming support
    - Connection pooling
    - Comprehensive metrics

    Example:
        >>> client = get_jarvis_prime_client()
        >>> response = await client.chat([
        ...     ChatMessage("user", "What's the weather?")
        ... ])
        >>> if response.success:
        ...     print(response.content)
    """

    def __init__(self, config: Optional[JarvisPrimeClientConfig] = None):
        """
        Initialize the JARVIS-Prime client.

        Args:
            config: Client configuration
        """
        self.config = config or JarvisPrimeClientConfig()

        # HTTP session (lazy initialized)
        self._session: Optional[aiohttp.ClientSession] = None

        # Circuit breaker state
        self._circuit_state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._circuit_opened_at: Optional[datetime] = None

        # Metrics
        self._metrics = ClientMetrics()

        # State
        self._initialized = False

        logger.info(
            f"[JarvisPrimeClient] Initialized "
            f"(url={self.config.base_url}, enabled={self.config.enabled})"
        )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Ensure HTTP session exists."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                connect=self.config.connect_timeout_seconds,
                total=self.config.request_timeout_seconds,
            )
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._initialized = True
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        self._initialized = False

    # =========================================================================
    # Circuit Breaker
    # =========================================================================

    def _check_circuit(self) -> bool:
        """
        Check if request should be allowed.

        Returns:
            True if request can proceed, False if blocked by circuit breaker
        """
        if self._circuit_state == CircuitState.CLOSED:
            return True

        if self._circuit_state == CircuitState.OPEN:
            # Check if timeout has passed
            if self._circuit_opened_at:
                elapsed = (datetime.now() - self._circuit_opened_at).total_seconds()
                if elapsed >= self.config.circuit_breaker_timeout_seconds:
                    logger.info("[JarvisPrimeClient] Circuit breaker half-open, testing")
                    self._circuit_state = CircuitState.HALF_OPEN
                    return True
            return False

        # HALF_OPEN - allow one request
        return True

    def _record_success(self) -> None:
        """Record a successful request."""
        self._consecutive_failures = 0

        if self._circuit_state == CircuitState.HALF_OPEN:
            logger.info("[JarvisPrimeClient] Circuit breaker closed (recovered)")
            self._circuit_state = CircuitState.CLOSED

    def _record_failure(self, error: str) -> None:
        """Record a failed request."""
        self._consecutive_failures += 1
        self._metrics.last_error = error

        if self._circuit_state == CircuitState.HALF_OPEN:
            # Test request failed - reopen circuit
            logger.warning("[JarvisPrimeClient] Circuit breaker re-opened (test failed)")
            self._circuit_state = CircuitState.OPEN
            self._circuit_opened_at = datetime.now()
            self._metrics.circuit_trips += 1
            return

        if self._consecutive_failures >= self.config.circuit_breaker_threshold:
            logger.warning(
                f"[JarvisPrimeClient] Circuit breaker OPEN "
                f"({self._consecutive_failures} failures)"
            )
            self._circuit_state = CircuitState.OPEN
            self._circuit_opened_at = datetime.now()
            self._metrics.circuit_trips += 1

    # =========================================================================
    # Health Check
    # =========================================================================

    async def health_check(self) -> bool:
        """
        Check if JARVIS-Prime is healthy.

        Returns:
            True if healthy
        """
        if not self.config.enabled:
            return False

        try:
            session = await self._ensure_session()
            url = f"{self.config.base_url}/health"

            async with session.get(url) as response:
                return response.status == 200

        except Exception as e:
            logger.debug(f"[JarvisPrimeClient] Health check failed: {e}")
            return False

    async def is_available(self) -> bool:
        """
        Check if JARVIS-Prime is available for requests.

        Returns:
            True if available (healthy and circuit closed)
        """
        if not self.config.enabled:
            return False

        if not self._check_circuit():
            return False

        return await self.health_check()

    # =========================================================================
    # Chat Completion API
    # =========================================================================

    async def chat(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
    ) -> ChatResponse:
        """
        Send a chat completion request.

        Args:
            messages: List of chat messages
            model: Model name (optional, uses default)
            temperature: Sampling temperature (0.0-2.0)
            max_tokens: Maximum tokens to generate
            stream: Whether to stream response

        Returns:
            ChatResponse with result or error
        """
        if not self.config.enabled:
            return ChatResponse(
                success=False,
                error="JARVIS-Prime client is disabled",
            )

        # Check circuit breaker
        if not self._check_circuit():
            return ChatResponse(
                success=False,
                error="Circuit breaker is OPEN - JARVIS-Prime unavailable",
            )

        start_time = time.perf_counter()
        self._metrics.total_requests += 1
        self._metrics.last_request_time = datetime.now()

        # Build request body
        body = {
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
            "stream": stream,
        }
        if model:
            body["model"] = model
        if max_tokens:
            body["max_tokens"] = max_tokens

        # Retry loop
        last_error = None
        for attempt in range(self.config.max_retries + 1):
            try:
                session = await self._ensure_session()
                url = f"{self.config.base_url}/v1/chat/completions"

                async with session.post(url, json=body) as response:
                    latency_ms = (time.perf_counter() - start_time) * 1000

                    if response.status == 200:
                        data = await response.json()

                        # Parse response
                        choices = data.get("choices", [])
                        if choices:
                            choice = choices[0]
                            message = choice.get("message", {})
                            content = message.get("content", "")
                            finish_reason = choice.get("finish_reason")
                        else:
                            content = ""
                            finish_reason = None

                        # Record success
                        self._metrics.successful_requests += 1
                        self._metrics.total_latency_ms += latency_ms
                        self._record_success()

                        return ChatResponse(
                            success=True,
                            content=content,
                            model=data.get("model"),
                            finish_reason=finish_reason,
                            latency_ms=latency_ms,
                            raw_response=data,
                        )
                    else:
                        error_text = await response.text()
                        last_error = f"HTTP {response.status}: {error_text[:200]}"
                        logger.warning(f"[JarvisPrimeClient] Request failed: {last_error}")

            except asyncio.TimeoutError:
                last_error = "Request timeout"
                logger.warning(f"[JarvisPrimeClient] Timeout on attempt {attempt + 1}")
            except aiohttp.ClientError as e:
                last_error = f"Client error: {e}"
                logger.warning(f"[JarvisPrimeClient] Error on attempt {attempt + 1}: {e}")
            except Exception as e:
                last_error = f"Unexpected error: {e}"
                logger.error(f"[JarvisPrimeClient] Unexpected error: {e}")

            # Backoff before retry
            if attempt < self.config.max_retries:
                backoff = self.config.retry_backoff_base * (2 ** attempt)
                await asyncio.sleep(backoff)

        # All retries failed
        latency_ms = (time.perf_counter() - start_time) * 1000
        self._metrics.failed_requests += 1
        self._record_failure(last_error or "Unknown error")

        return ChatResponse(
            success=False,
            error=last_error,
            latency_ms=latency_ms,
        )

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """
        Stream a chat completion response.

        Args:
            messages: List of chat messages
            model: Model name (optional)
            temperature: Sampling temperature
            max_tokens: Maximum tokens

        Yields:
            Content chunks as they arrive
        """
        if not self.config.enabled:
            return

        if not self._check_circuit():
            return

        try:
            session = await self._ensure_session()
            url = f"{self.config.base_url}/v1/chat/completions"

            body = {
                "messages": [m.to_dict() for m in messages],
                "temperature": temperature,
                "stream": True,
            }
            if model:
                body["model"] = model
            if max_tokens:
                body["max_tokens"] = max_tokens

            async with session.post(url, json=body) as response:
                if response.status != 200:
                    self._record_failure(f"HTTP {response.status}")
                    return

                # Read SSE stream
                async for line in response.content:
                    line = line.decode().strip()
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            import json
                            chunk = json.loads(data)
                            choices = chunk.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                        except Exception:
                            pass

                self._record_success()

        except Exception as e:
            logger.error(f"[JarvisPrimeClient] Stream error: {e}")
            self._record_failure(str(e))

    # =========================================================================
    # Simple Inference API
    # =========================================================================

    async def complete(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        **kwargs,
    ) -> ChatResponse:
        """
        Simple completion API.

        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            **kwargs: Additional arguments for chat()

        Returns:
            ChatResponse with result
        """
        messages = []

        if system_prompt:
            messages.append(ChatMessage("system", system_prompt))

        messages.append(ChatMessage("user", prompt))

        return await self.chat(messages, **kwargs)

    # =========================================================================
    # Metrics
    # =========================================================================

    def get_metrics(self) -> ClientMetrics:
        """Get client metrics."""
        return self._metrics

    def get_circuit_state(self) -> CircuitState:
        """Get current circuit breaker state."""
        return self._circuit_state

    def reset_circuit(self) -> None:
        """Manually reset the circuit breaker."""
        self._circuit_state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._circuit_opened_at = None
        logger.info("[JarvisPrimeClient] Circuit breaker manually reset")


# =============================================================================
# Singleton Access
# =============================================================================

_client_instance: Optional[JarvisPrimeClient] = None


def get_jarvis_prime_client(
    config: Optional[JarvisPrimeClientConfig] = None,
) -> JarvisPrimeClient:
    """
    Get the global JARVIS-Prime client instance.

    Args:
        config: Optional configuration (only used on first call)

    Returns:
        The global client instance
    """
    global _client_instance

    if _client_instance is None:
        _client_instance = JarvisPrimeClient(config=config)

    return _client_instance


async def close_jarvis_prime_client() -> None:
    """Close the global JARVIS-Prime client."""
    global _client_instance

    if _client_instance:
        await _client_instance.close()
        _client_instance = None
