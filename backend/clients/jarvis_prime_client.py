"""
JARVIS Prime Client - Cognitive Mind Integration.
==================================================

Provides robust communication with JARVIS Prime (the cognitive mind
of the Trinity architecture).

Features:
- Circuit breaker for fault tolerance
- Hot-reload model swapping
- LLM inference streaming
- Cognitive task delegation
- Model health monitoring
- Dead letter queue for failed operations

API Endpoints:
    POST /api/inference          - Run inference
    POST /api/inference/stream   - Streaming inference
    POST /api/model/swap         - Hot-swap model
    GET  /api/model/status       - Get model status
    GET  /api/health             - Health check
    POST /api/cognitive/delegate - Delegate cognitive task

Author: JARVIS Trinity v81.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

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
# Environment Helpers
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
    """Configuration for JARVIS Prime client."""
    name: str = "jarvis_prime"
    base_url: str = field(default_factory=lambda: _env_str(
        "JARVIS_PRIME_URL", "http://localhost:8002"
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
    # Streaming
    stream_buffer_size: int = field(default_factory=lambda: _env_int(
        "JARVIS_PRIME_STREAM_BUFFER", 16
    ))
    # Model swap
    model_swap_timeout: float = field(default_factory=lambda: _env_float(
        "JARVIS_PRIME_SWAP_TIMEOUT", 120.0
    ))
    # Fallback
    fallback_to_cloud: bool = field(default_factory=lambda: _env_bool(
        "JARVIS_PRIME_FALLBACK_TO_CLOUD", True
    ))
    cloud_api_url: str = field(default_factory=lambda: _env_str(
        "JARVIS_PRIME_CLOUD_URL", ""
    ))


# =============================================================================
# JARVIS Prime Client
# =============================================================================

class JARVISPrimeClient(TrinityBaseClient[Dict[str, Any]]):
    """
    Client for JARVIS Prime (cognitive mind).

    Features:
    - LLM inference with streaming support
    - Hot-swap model reloading
    - Cognitive task delegation
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

        # HTTP session
        self._session = None

        logger.info(
            f"[JARVISPrime] Client initialized with base_url={self._prime_config.base_url}"
        )

    async def _get_session(self):
        """Get or create HTTP session."""
        if self._session is None:
            try:
                import aiohttp
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self._prime_config.timeout)
                )
            except ImportError:
                logger.warning("[JARVISPrime] aiohttp not available")
                raise
        return self._session

    async def _health_check(self) -> bool:
        """Check if JARVIS Prime is healthy."""
        try:
            session = await self._get_session()
            url = f"{self._prime_config.base_url}/health"

            async with session.get(url, timeout=5.0) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("status") in ("healthy", "ok", "ready")
                return False

        except Exception as e:
            logger.debug(f"[JARVISPrime] Health check failed: {e}")
            return False

    async def _execute_request(
        self,
        operation: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a request to JARVIS Prime."""
        session = await self._get_session()

        # Map operation to endpoint
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
        url = f"{self._prime_config.base_url}{endpoint}"

        try:
            if method == "GET":
                async with session.get(url, params=payload) as response:
                    response.raise_for_status()
                    return await response.json()
            else:
                async with session.post(url, json=payload) as response:
                    response.raise_for_status()
                    return await response.json()

        except Exception as e:
            logger.debug(f"[JARVISPrime] Request failed: {operation} - {e}")
            raise

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
_client_lock = asyncio.Lock()


async def get_jarvis_prime_client(
    config: Optional[JARVISPrimeConfig] = None,
) -> JARVISPrimeClient:
    """Get or create the singleton JARVIS Prime client."""
    global _client

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
