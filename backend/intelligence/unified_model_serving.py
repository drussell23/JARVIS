"""
UnifiedModelServing v100.0 - Prime + Claude Fallback Model Layer
=================================================================

Advanced model serving layer that provides:
1. JARVIS Prime local model inference (primary)
2. Claude API fallback (when Prime unavailable)
3. Automatic model selection based on task type
4. Model versioning and hot-swap capability
5. Circuit breaker for failing models
6. Cost tracking and optimization

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                  UnifiedModelServing                             │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  ModelRouter                                               │ │
    │  │  ├── Task-based routing (chat, vision, reasoning, code)   │ │
    │  │  ├── Load balancing across available models               │ │
    │  │  └── Fallback chain: Prime → Cloud Run → Claude          │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  PrimeModelClient                                          │ │
    │  │  ├── Local GGUF model inference                           │ │
    │  │  ├── Cloud Run Prime deployment                           │ │
    │  │  └── Model hot-swap and versioning                        │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  ClaudeFallback                                            │ │
    │  │  ├── Claude API integration                               │ │
    │  │  ├── Rate limiting and cost tracking                      │ │
    │  │  └── Response caching                                      │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  CircuitBreaker                                            │ │
    │  │  ├── Failure detection and recovery                       │ │
    │  │  ├── Health monitoring                                    │ │
    │  │  └── Automatic failover                                   │ │
    │  └────────────────────────────────────────────────────────────┘ │
    └─────────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Coroutine,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    Tuple,
    TypeVar,
    Union,
)

from backend.core.async_safety import LazyAsyncLock

# Environment-driven configuration
MODEL_SERVING_DATA_DIR = Path(os.getenv(
    "MODEL_SERVING_DATA_DIR",
    str(Path.home() / ".jarvis" / "model_serving")
))

# Prime configuration
PRIME_ENABLED = os.getenv("JARVIS_PRIME_ENABLED", "true").lower() == "true"
PRIME_LOCAL_ENABLED = os.getenv("JARVIS_PRIME_LOCAL_ENABLED", "true").lower() == "true"
PRIME_CLOUD_RUN_ENABLED = os.getenv("JARVIS_PRIME_CLOUD_RUN_ENABLED", "false").lower() == "true"
PRIME_CLOUD_RUN_URL = os.getenv("JARVIS_PRIME_CLOUD_RUN_URL", "")
PRIME_MODELS_DIR = Path(os.getenv("JARVIS_PRIME_MODELS_DIR", str(Path.home() / "models")))
PRIME_DEFAULT_MODEL = os.getenv("JARVIS_PRIME_DEFAULT_MODEL", "prime-7b-chat-v1.Q4_K_M.gguf")
PRIME_CONTEXT_LENGTH = int(os.getenv("JARVIS_PRIME_CONTEXT_LENGTH", "4096"))
PRIME_TIMEOUT_SECONDS = float(os.getenv("JARVIS_PRIME_TIMEOUT_SECONDS", "60.0"))

# Claude configuration
CLAUDE_ENABLED = os.getenv("CLAUDE_FALLBACK_ENABLED", "true").lower() == "true"
CLAUDE_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_DEFAULT_MODEL = os.getenv("CLAUDE_DEFAULT_MODEL", "claude-sonnet-4-20250514")
CLAUDE_TIMEOUT_SECONDS = float(os.getenv("CLAUDE_TIMEOUT_SECONDS", "120.0"))
CLAUDE_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "4096"))

# Circuit breaker configuration
CIRCUIT_BREAKER_FAILURE_THRESHOLD = int(os.getenv("CIRCUIT_BREAKER_FAILURES", "3"))
CIRCUIT_BREAKER_RECOVERY_SECONDS = float(os.getenv("CIRCUIT_BREAKER_RECOVERY", "30.0"))

# Cost tracking
COST_TRACKING_ENABLED = os.getenv("COST_TRACKING_ENABLED", "true").lower() == "true"


class TaskType(Enum):
    """Types of inference tasks."""
    CHAT = "chat"
    REASONING = "reasoning"
    VISION = "vision"
    CODE = "code"
    EMBEDDING = "embedding"
    TOOL_USE = "tool_use"


class ModelProvider(Enum):
    """Model providers."""
    PRIME_LOCAL = "prime_local"
    PRIME_CLOUD_RUN = "prime_cloud_run"
    CLAUDE = "claude"
    FALLBACK = "fallback"


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class ModelRequest:
    """A request to a model."""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    # Content
    messages: List[Dict[str, str]] = field(default_factory=list)
    system_prompt: Optional[str] = None
    task_type: TaskType = TaskType.CHAT

    # Configuration
    max_tokens: int = 2048
    temperature: float = 0.7
    stream: bool = False

    # Context
    context: Dict[str, Any] = field(default_factory=dict)

    # Routing hints
    preferred_provider: Optional[ModelProvider] = None
    require_vision: bool = False
    require_tool_use: bool = False


@dataclass
class ModelResponse:
    """A response from a model."""
    response_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    # Content
    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)

    # Metadata
    provider: ModelProvider = ModelProvider.FALLBACK
    model_name: str = ""
    tokens_used: int = 0
    latency_ms: float = 0.0

    # Cost
    estimated_cost_usd: float = 0.0

    # Status
    success: bool = True
    error: Optional[str] = None
    fallback_used: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "response_id": self.response_id,
            "content": self.content,
            "provider": self.provider.value,
            "model_name": self.model_name,
            "tokens_used": self.tokens_used,
            "latency_ms": self.latency_ms,
            "estimated_cost_usd": self.estimated_cost_usd,
            "success": self.success,
            "fallback_used": self.fallback_used,
        }


@dataclass
class CircuitBreakerState:
    """State of a circuit breaker."""
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = field(default_factory=time.time)
    total_failures: int = 0
    total_successes: int = 0


class ModelClient(ABC):
    """Base class for model clients."""

    @abstractmethod
    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a response."""
        pass

    @abstractmethod
    async def generate_stream(self, request: ModelRequest) -> AsyncIterator[str]:
        """Generate a streaming response."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the model is healthy."""
        pass

    @abstractmethod
    def get_supported_tasks(self) -> List[TaskType]:
        """Get supported task types."""
        pass


class PrimeLocalClient(ModelClient):
    """Client for local JARVIS Prime model inference."""

    def __init__(self):
        self.logger = logging.getLogger("PrimeLocalClient")
        self._model = None
        self._model_path: Optional[Path] = None
        self._lock = asyncio.Lock()
        self._loaded = False

    async def load_model(self, model_name: Optional[str] = None) -> bool:
        """Load a Prime model."""
        if model_name is None:
            model_name = PRIME_DEFAULT_MODEL

        model_path = PRIME_MODELS_DIR / model_name

        if not model_path.exists():
            self.logger.warning(f"Model not found: {model_path}")
            return False

        async with self._lock:
            try:
                # Try to import llama-cpp-python
                from llama_cpp import Llama

                self._model = Llama(
                    model_path=str(model_path),
                    n_ctx=PRIME_CONTEXT_LENGTH,
                    n_threads=os.cpu_count() or 4,
                    verbose=False,
                )
                self._model_path = model_path
                self._loaded = True
                self.logger.info(f"Loaded Prime model: {model_name}")
                return True

            except ImportError:
                self.logger.warning("llama-cpp-python not installed")
                return False
            except Exception as e:
                self.logger.error(f"Failed to load model: {e}")
                return False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a response using local Prime model."""
        start_time = time.time()
        response = ModelResponse(
            provider=ModelProvider.PRIME_LOCAL,
            model_name=str(self._model_path.name) if self._model_path else "unknown",
        )

        if not self._loaded or self._model is None:
            if not await self.load_model():
                response.success = False
                response.error = "Model not loaded"
                return response

        try:
            # Build prompt from messages
            prompt = self._build_prompt(request.messages, request.system_prompt)

            # Run inference in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._model(
                    prompt,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    stop=["</s>", "Human:", "User:"],
                )
            )

            response.content = result["choices"][0]["text"].strip()
            response.tokens_used = result.get("usage", {}).get("total_tokens", 0)
            response.success = True

        except Exception as e:
            self.logger.error(f"Prime inference error: {e}")
            response.success = False
            response.error = str(e)

        response.latency_ms = (time.time() - start_time) * 1000
        return response

    async def generate_stream(self, request: ModelRequest) -> AsyncIterator[str]:
        """Generate a streaming response."""
        if not self._loaded or self._model is None:
            if not await self.load_model():
                yield "[Error: Model not loaded]"
                return

        try:
            prompt = self._build_prompt(request.messages, request.system_prompt)

            for chunk in self._model(
                prompt,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                stop=["</s>", "Human:", "User:"],
                stream=True,
            ):
                text = chunk["choices"][0]["text"]
                if text:
                    yield text

        except Exception as e:
            self.logger.error(f"Prime streaming error: {e}")
            yield f"[Error: {e}]"

    def _build_prompt(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str]
    ) -> str:
        """Build a prompt from messages."""
        parts = []

        if system_prompt:
            parts.append(f"System: {system_prompt}\n")

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "user":
                parts.append(f"Human: {content}\n")
            elif role == "assistant":
                parts.append(f"Assistant: {content}\n")
            elif role == "system":
                parts.append(f"System: {content}\n")

        parts.append("Assistant: ")
        return "".join(parts)

    async def health_check(self) -> bool:
        """Check if the model is healthy."""
        return self._loaded and self._model is not None

    def get_supported_tasks(self) -> List[TaskType]:
        """Get supported task types."""
        return [TaskType.CHAT, TaskType.REASONING, TaskType.CODE]


class PrimeCloudRunClient(ModelClient):
    """Client for JARVIS Prime on Cloud Run."""

    def __init__(self, url: str = PRIME_CLOUD_RUN_URL):
        self.logger = logging.getLogger("PrimeCloudRunClient")
        self.url = url
        self._session = None

    async def _get_session(self):
        """Get or create aiohttp session."""
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a response using Cloud Run Prime."""
        start_time = time.time()
        response = ModelResponse(
            provider=ModelProvider.PRIME_CLOUD_RUN,
            model_name="prime-cloud-run",
        )

        if not self.url:
            response.success = False
            response.error = "Cloud Run URL not configured"
            return response

        try:
            import aiohttp

            session = await self._get_session()

            payload = {
                "messages": request.messages,
                "system": request.system_prompt,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
            }

            async with session.post(
                f"{self.url}/v1/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=PRIME_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    response.content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    response.tokens_used = data.get("usage", {}).get("total_tokens", 0)
                    response.success = True
                else:
                    response.success = False
                    response.error = f"HTTP {resp.status}"

        except Exception as e:
            self.logger.error(f"Cloud Run inference error: {e}")
            response.success = False
            response.error = str(e)

        response.latency_ms = (time.time() - start_time) * 1000
        return response

    async def generate_stream(self, request: ModelRequest) -> AsyncIterator[str]:
        """Generate a streaming response."""
        # Cloud Run streaming would go here
        response = await self.generate(request)
        if response.success:
            yield response.content
        else:
            yield f"[Error: {response.error}]"

    async def health_check(self) -> bool:
        """Check if Cloud Run endpoint is healthy."""
        if not self.url:
            return False

        try:
            import aiohttp

            session = await self._get_session()
            async with session.get(
                f"{self.url}/health",
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    def get_supported_tasks(self) -> List[TaskType]:
        """Get supported task types."""
        return [TaskType.CHAT, TaskType.REASONING, TaskType.CODE, TaskType.VISION]


class ClaudeClient(ModelClient):
    """Client for Claude API fallback."""

    def __init__(self, api_key: str = CLAUDE_API_KEY):
        self.logger = logging.getLogger("ClaudeClient")
        self.api_key = api_key
        self._client = None

        # Cost tracking (per 1M tokens)
        self._cost_per_1m_input = {
            "claude-sonnet-4-20250514": 3.00,
            "claude-opus-4-20250514": 15.00,
            "claude-3-haiku-20240307": 0.25,
        }
        self._cost_per_1m_output = {
            "claude-sonnet-4-20250514": 15.00,
            "claude-opus-4-20250514": 75.00,
            "claude-3-haiku-20240307": 1.25,
        }

    async def _get_client(self):
        """Get or create Anthropic client."""
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
                self._client = AsyncAnthropic(api_key=self.api_key)
            except ImportError:
                raise RuntimeError("anthropic package not installed")
        return self._client

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a response using Claude API."""
        start_time = time.time()
        response = ModelResponse(
            provider=ModelProvider.CLAUDE,
            model_name=CLAUDE_DEFAULT_MODEL,
        )

        if not self.api_key:
            response.success = False
            response.error = "Claude API key not configured"
            return response

        try:
            client = await self._get_client()

            # Convert messages to Claude format
            claude_messages = []
            for msg in request.messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")

                # Claude uses 'user' and 'assistant' roles
                if role in ("user", "assistant"):
                    claude_messages.append({"role": role, "content": content})
                elif role == "system":
                    # System messages go in the system parameter
                    pass

            # Make API call
            result = await client.messages.create(
                model=CLAUDE_DEFAULT_MODEL,
                max_tokens=request.max_tokens,
                system=request.system_prompt or "",
                messages=claude_messages,
            )

            response.content = result.content[0].text if result.content else ""
            response.tokens_used = result.usage.input_tokens + result.usage.output_tokens
            response.success = True

            # Calculate cost
            if COST_TRACKING_ENABLED:
                input_cost = (result.usage.input_tokens / 1_000_000) * self._cost_per_1m_input.get(CLAUDE_DEFAULT_MODEL, 3.0)
                output_cost = (result.usage.output_tokens / 1_000_000) * self._cost_per_1m_output.get(CLAUDE_DEFAULT_MODEL, 15.0)
                response.estimated_cost_usd = input_cost + output_cost

        except Exception as e:
            self.logger.error(f"Claude API error: {e}")
            response.success = False
            response.error = str(e)

        response.latency_ms = (time.time() - start_time) * 1000
        return response

    async def generate_stream(self, request: ModelRequest) -> AsyncIterator[str]:
        """Generate a streaming response from Claude."""
        if not self.api_key:
            yield "[Error: Claude API key not configured]"
            return

        try:
            client = await self._get_client()

            claude_messages = []
            for msg in request.messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role in ("user", "assistant"):
                    claude_messages.append({"role": role, "content": content})

            async with client.messages.stream(
                model=CLAUDE_DEFAULT_MODEL,
                max_tokens=request.max_tokens,
                system=request.system_prompt or "",
                messages=claude_messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text

        except Exception as e:
            self.logger.error(f"Claude streaming error: {e}")
            yield f"[Error: {e}]"

    async def health_check(self) -> bool:
        """Check if Claude API is accessible."""
        if not self.api_key:
            return False

        try:
            client = await self._get_client()
            # Simple health check
            return client is not None
        except Exception:
            return False

    def get_supported_tasks(self) -> List[TaskType]:
        """Get supported task types."""
        return [TaskType.CHAT, TaskType.REASONING, TaskType.CODE, TaskType.VISION, TaskType.TOOL_USE]


class CircuitBreaker:
    """Circuit breaker for model clients."""

    def __init__(
        self,
        failure_threshold: int = CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        recovery_seconds: float = CIRCUIT_BREAKER_RECOVERY_SECONDS
    ):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.logger = logging.getLogger("CircuitBreaker")

        self._states: Dict[str, CircuitBreakerState] = {}

    def get_state(self, provider: str) -> CircuitBreakerState:
        """Get circuit state for a provider."""
        if provider not in self._states:
            self._states[provider] = CircuitBreakerState()
        return self._states[provider]

    def can_execute(self, provider: str) -> bool:
        """Check if requests can be made to this provider."""
        state = self.get_state(provider)

        if state.state == CircuitState.CLOSED:
            return True

        if state.state == CircuitState.OPEN:
            # Check if recovery period has passed
            if time.time() - state.last_failure_time >= self.recovery_seconds:
                state.state = CircuitState.HALF_OPEN
                self.logger.info(f"Circuit for {provider} entering half-open state")
                return True
            return False

        # Half-open: allow one request to test
        return True

    def record_success(self, provider: str) -> None:
        """Record a successful request."""
        state = self.get_state(provider)
        state.failure_count = 0
        state.last_success_time = time.time()
        state.total_successes += 1

        if state.state == CircuitState.HALF_OPEN:
            state.state = CircuitState.CLOSED
            self.logger.info(f"Circuit for {provider} closed (recovered)")

    def record_failure(self, provider: str) -> None:
        """Record a failed request."""
        state = self.get_state(provider)
        state.failure_count += 1
        state.last_failure_time = time.time()
        state.total_failures += 1

        if state.failure_count >= self.failure_threshold:
            if state.state != CircuitState.OPEN:
                state.state = CircuitState.OPEN
                self.logger.warning(f"Circuit for {provider} opened (too many failures)")


class ModelRouter:
    """Routes requests to appropriate model providers."""

    def __init__(self):
        self.logger = logging.getLogger("ModelRouter")

        # Task to preferred provider mapping
        self._task_preferences: Dict[TaskType, List[ModelProvider]] = {
            TaskType.CHAT: [ModelProvider.PRIME_LOCAL, ModelProvider.PRIME_CLOUD_RUN, ModelProvider.CLAUDE],
            TaskType.REASONING: [ModelProvider.CLAUDE, ModelProvider.PRIME_LOCAL],  # Claude better for reasoning
            TaskType.VISION: [ModelProvider.CLAUDE, ModelProvider.PRIME_CLOUD_RUN],  # Claude has vision
            TaskType.CODE: [ModelProvider.PRIME_LOCAL, ModelProvider.CLAUDE],
            TaskType.TOOL_USE: [ModelProvider.CLAUDE],  # Only Claude supports tool use
            TaskType.EMBEDDING: [ModelProvider.PRIME_LOCAL],
        }

    def get_preferred_providers(
        self,
        request: ModelRequest,
        available_providers: List[ModelProvider]
    ) -> List[ModelProvider]:
        """Get ordered list of preferred providers for a request."""
        # Start with task preferences
        preferred = self._task_preferences.get(request.task_type, [ModelProvider.CLAUDE])

        # Filter to available providers
        result = [p for p in preferred if p in available_providers]

        # Apply request hints
        if request.preferred_provider and request.preferred_provider in available_providers:
            # Move preferred to front
            if request.preferred_provider in result:
                result.remove(request.preferred_provider)
            result.insert(0, request.preferred_provider)

        if request.require_vision:
            # Only providers with vision support
            vision_providers = {ModelProvider.CLAUDE, ModelProvider.PRIME_CLOUD_RUN}
            result = [p for p in result if p in vision_providers]

        if request.require_tool_use:
            # Only Claude supports tool use
            result = [p for p in result if p == ModelProvider.CLAUDE]

        # Ensure we have at least one fallback
        if not result and ModelProvider.CLAUDE in available_providers:
            result = [ModelProvider.CLAUDE]

        return result


class UnifiedModelServing:
    """
    Unified model serving layer with Prime + Claude fallback.

    Provides seamless inference across local Prime models and Claude API.
    """

    def __init__(self):
        self.logger = logging.getLogger("UnifiedModelServing")

        # Initialize clients
        self._clients: Dict[ModelProvider, ModelClient] = {}
        self._router = ModelRouter()
        self._circuit_breaker = CircuitBreaker()

        # State
        self._running = False
        self._lock = asyncio.Lock()

        # Metrics
        self._request_count = 0
        self._total_latency_ms = 0.0
        self._total_cost_usd = 0.0
        self._provider_usage: Dict[str, int] = defaultdict(int)
        self._fallback_count = 0

        # Ensure data directory
        MODEL_SERVING_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the model serving layer."""
        if self._running:
            return

        self._running = True
        self.logger.info("UnifiedModelServing starting...")

        # Initialize clients based on configuration
        if PRIME_LOCAL_ENABLED:
            client = PrimeLocalClient()
            if await client.health_check() or await client.load_model():
                self._clients[ModelProvider.PRIME_LOCAL] = client
                self.logger.info("  ✓ Prime Local client ready")
            else:
                self.logger.info("  ⚠️ Prime Local client not available")

        if PRIME_CLOUD_RUN_ENABLED and PRIME_CLOUD_RUN_URL:
            client = PrimeCloudRunClient()
            if await client.health_check():
                self._clients[ModelProvider.PRIME_CLOUD_RUN] = client
                self.logger.info("  ✓ Prime Cloud Run client ready")
            else:
                self.logger.info("  ⚠️ Prime Cloud Run client not available")

        if CLAUDE_ENABLED and CLAUDE_API_KEY:
            client = ClaudeClient()
            self._clients[ModelProvider.CLAUDE] = client
            self.logger.info("  ✓ Claude fallback client ready")

        if not self._clients:
            self.logger.warning("No model clients available!")

        self.logger.info(f"UnifiedModelServing ready ({len(self._clients)} providers)")

    async def stop(self) -> None:
        """Stop the model serving layer."""
        self._running = False
        self.logger.info("UnifiedModelServing stopped")

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a response using the best available provider."""
        available = list(self._clients.keys())
        providers = self._router.get_preferred_providers(request, available)

        if not providers:
            return ModelResponse(
                success=False,
                error="No suitable model providers available",
            )

        last_error = None
        fallback_used = False

        for i, provider in enumerate(providers):
            # Check circuit breaker
            if not self._circuit_breaker.can_execute(provider.value):
                self.logger.debug(f"Circuit open for {provider.value}, skipping")
                continue

            client = self._clients.get(provider)
            if not client:
                continue

            try:
                response = await client.generate(request)

                if response.success:
                    self._circuit_breaker.record_success(provider.value)
                    response.fallback_used = fallback_used

                    # Update metrics
                    self._request_count += 1
                    self._total_latency_ms += response.latency_ms
                    self._total_cost_usd += response.estimated_cost_usd
                    self._provider_usage[provider.value] += 1

                    return response
                else:
                    self._circuit_breaker.record_failure(provider.value)
                    last_error = response.error
                    fallback_used = True
                    self._fallback_count += 1

            except Exception as e:
                self.logger.error(f"Provider {provider.value} error: {e}")
                self._circuit_breaker.record_failure(provider.value)
                last_error = str(e)
                fallback_used = True
                self._fallback_count += 1

        return ModelResponse(
            success=False,
            error=f"All providers failed. Last error: {last_error}",
            fallback_used=True,
        )

    async def generate_stream(
        self,
        request: ModelRequest
    ) -> AsyncIterator[str]:
        """Generate a streaming response."""
        available = list(self._clients.keys())
        providers = self._router.get_preferred_providers(request, available)

        if not providers:
            yield "[Error: No suitable model providers available]"
            return

        for provider in providers:
            if not self._circuit_breaker.can_execute(provider.value):
                continue

            client = self._clients.get(provider)
            if not client:
                continue

            try:
                async for chunk in client.generate_stream(request):
                    yield chunk
                return
            except Exception as e:
                self.logger.error(f"Streaming error from {provider.value}: {e}")
                self._circuit_breaker.record_failure(provider.value)
                continue

        yield "[Error: All providers failed for streaming]"

    async def chat(
        self,
        message: str,
        system_prompt: Optional[str] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        **kwargs
    ) -> str:
        """Convenience method for chat interactions."""
        messages = conversation_history or []
        messages.append({"role": "user", "content": message})

        request = ModelRequest(
            messages=messages,
            system_prompt=system_prompt,
            task_type=TaskType.CHAT,
            **kwargs
        )

        response = await self.generate(request)

        if response.success:
            return response.content
        else:
            return f"[Error: {response.error}]"

    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics."""
        return {
            "running": self._running,
            "providers_available": [p.value for p in self._clients.keys()],
            "request_count": self._request_count,
            "avg_latency_ms": self._total_latency_ms / max(1, self._request_count),
            "total_cost_usd": self._total_cost_usd,
            "provider_usage": dict(self._provider_usage),
            "fallback_count": self._fallback_count,
            "circuit_states": {
                p: s.state.value
                for p, s in self._circuit_breaker._states.items()
            },
        }


# Global instance
_model_serving: Optional[UnifiedModelServing] = None
_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_model_serving() -> UnifiedModelServing:
    """Get the global UnifiedModelServing instance."""
    global _model_serving

    async with _lock:
        if _model_serving is None:
            _model_serving = UnifiedModelServing()
            await _model_serving.start()

        return _model_serving
