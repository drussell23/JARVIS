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
# v17.0: J-Prime API configuration (connects to jarvis-prime repo server)
PRIME_API_ENABLED = os.getenv("JARVIS_PRIME_API_ENABLED", "true").lower() == "true"
PRIME_API_URL = os.getenv("JARVIS_PRIME_API_URL", "http://localhost:8000")
PRIME_API_TIMEOUT = float(os.getenv("JARVIS_PRIME_API_TIMEOUT", "30.0"))
PRIME_API_WAIT_TIMEOUT = float(os.getenv("JARVIS_PRIME_API_WAIT_TIMEOUT", "15.0"))
# Local GGUF model configuration (fallback if J-Prime API unavailable)
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
    PRIME_API = "prime_api"  # v17.0: J-Prime API (jarvis-prime repo server)
    PRIME_LOCAL = "prime_local"  # Local GGUF model
    PRIME_CLOUD_RUN = "prime_cloud_run"  # Cloud Run deployment
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
    """
    Client for local JARVIS Prime model inference.

    Features:
    - Intelligent model discovery across multiple directories
    - Optional auto-download from Hugging Face
    - Graceful degradation (warns once, not repeatedly)
    - Fallback model support
    """

    # Class-level flag to prevent repeated warnings
    _warned_missing_model: bool = False
    _warned_missing_llama: bool = False

    # Common model search directories
    MODEL_SEARCH_PATHS = [
        Path.home() / "models",
        Path.home() / ".jarvis" / "models",
        Path.home() / ".cache" / "huggingface" / "hub",
        Path.home() / "Documents" / "models",
        Path("/usr/local/share/jarvis/models"),
        Path("/opt/jarvis/models"),
    ]

    # Model name aliases for flexible discovery
    MODEL_ALIASES = {
        "prime-7b-chat-v1.Q4_K_M.gguf": [
            "prime-7b-chat-v1.Q4_K_M.gguf",
            "prime-7b-chat.Q4_K_M.gguf",
            "mistral-7b-instruct-v0.2.Q4_K_M.gguf",
            "mistral-7b-instruct.Q4_K_M.gguf",
            "llama-2-7b-chat.Q4_K_M.gguf",
            "dolphin-2.6-mistral-7b.Q4_K_M.gguf",
            "openhermes-2.5-mistral-7b.Q4_K_M.gguf",
        ],
    }

    # Hugging Face repo mappings for auto-download
    HF_MODEL_REPOS = {
        "mistral-7b-instruct-v0.2.Q4_K_M.gguf": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
        "llama-2-7b-chat.Q4_K_M.gguf": "TheBloke/Llama-2-7B-Chat-GGUF",
    }

    def __init__(self):
        self.logger = logging.getLogger("PrimeLocalClient")
        self._model = None
        self._model_path: Optional[Path] = None
        self._lock = asyncio.Lock()
        self._loaded = False
        self._discovery_attempted = False
        # v234.0: Single-worker executor to serialize LLM inference (prevents OOM)
        import concurrent.futures
        self._inference_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="llm-inference"
        )

    def _discover_model(self, model_name: str) -> Optional[Path]:
        """
        Discover a model file by searching multiple directories.

        Args:
            model_name: Name of the model file to find

        Returns:
            Path to the model if found, None otherwise
        """
        # Get aliases for this model
        aliases = self.MODEL_ALIASES.get(model_name, [model_name])

        # Build search paths from environment + defaults
        search_paths = []

        # Add configured models dir first
        if PRIME_MODELS_DIR.exists():
            search_paths.append(PRIME_MODELS_DIR)

        # Add environment-configured additional paths
        extra_paths = os.getenv("JARVIS_MODEL_SEARCH_PATHS", "")
        if extra_paths:
            for p in extra_paths.split(":"):
                path = Path(p)
                if path.exists() and path not in search_paths:
                    search_paths.append(path)

        # Add default search paths
        for path in self.MODEL_SEARCH_PATHS:
            if path.exists() and path not in search_paths:
                search_paths.append(path)

        # Search for model in all paths with all aliases
        for search_dir in search_paths:
            for alias in aliases:
                # Direct file
                model_path = search_dir / alias
                if model_path.exists():
                    self.logger.debug(f"Found model: {model_path}")
                    return model_path

                # Check subdirectories (e.g., huggingface cache structure)
                for subdir in search_dir.iterdir() if search_dir.is_dir() else []:
                    if subdir.is_dir():
                        model_path = subdir / alias
                        if model_path.exists():
                            self.logger.debug(f"Found model in subdir: {model_path}")
                            return model_path

        # Also scan for any .gguf files as fallback
        for search_dir in search_paths:
            if search_dir.is_dir():
                gguf_files = list(search_dir.glob("*.gguf"))
                if gguf_files:
                    # Prefer larger files (likely better quality)
                    gguf_files.sort(key=lambda p: p.stat().st_size, reverse=True)
                    self.logger.info(f"Using fallback GGUF model: {gguf_files[0].name}")
                    return gguf_files[0]

        return None

    async def _auto_download_model(self, model_name: str) -> Optional[Path]:
        """
        Attempt to auto-download a model from Hugging Face.

        Args:
            model_name: Name of the model to download

        Returns:
            Path to downloaded model, or None if download failed/disabled
        """
        # Check if auto-download is enabled
        auto_download = os.getenv("JARVIS_PRIME_AUTO_DOWNLOAD", "false").lower() == "true"
        if not auto_download:
            return None

        # Get HF repo for this model
        repo_id = self.HF_MODEL_REPOS.get(model_name)
        if not repo_id:
            # Try to find a matching repo from aliases
            aliases = self.MODEL_ALIASES.get(PRIME_DEFAULT_MODEL, [])
            for alias in aliases:
                if alias in self.HF_MODEL_REPOS:
                    repo_id = self.HF_MODEL_REPOS[alias]
                    model_name = alias
                    break

        if not repo_id:
            self.logger.debug(f"No HuggingFace repo configured for {model_name}")
            return None

        try:
            from huggingface_hub import hf_hub_download

            self.logger.info(f"Auto-downloading model {model_name} from {repo_id}...")

            # Ensure download directory exists
            download_dir = PRIME_MODELS_DIR
            download_dir.mkdir(parents=True, exist_ok=True)

            # Download model
            loop = asyncio.get_event_loop()
            model_path = await loop.run_in_executor(
                None,
                lambda: hf_hub_download(
                    repo_id=repo_id,
                    filename=model_name,
                    local_dir=str(download_dir),
                    local_dir_use_symlinks=False,
                )
            )

            self.logger.info(f"Downloaded model to: {model_path}")
            return Path(model_path)

        except ImportError:
            self.logger.debug("huggingface_hub not installed, can't auto-download")
            return None
        except Exception as e:
            self.logger.warning(f"Auto-download failed: {e}")
            return None

    async def load_model(self, model_name: Optional[str] = None) -> bool:
        """
        Load a Prime model with intelligent discovery.

        Args:
            model_name: Optional model name to load

        Returns:
            True if model loaded successfully
        """
        if model_name is None:
            model_name = PRIME_DEFAULT_MODEL

        # Try to discover the model
        model_path = self._discover_model(model_name)

        # If not found, try auto-download
        if model_path is None and not self._discovery_attempted:
            self._discovery_attempted = True
            model_path = await self._auto_download_model(model_name)

        if model_path is None:
            # Only warn once to avoid log spam
            if not PrimeLocalClient._warned_missing_model:
                PrimeLocalClient._warned_missing_model = True
                self.logger.warning(
                    f"Model not found: {model_name}. "
                    f"Searched: {[str(p) for p in self.MODEL_SEARCH_PATHS if p.exists()]}. "
                    f"Set JARVIS_PRIME_AUTO_DOWNLOAD=true to enable auto-download, "
                    f"or manually download a GGUF model to {PRIME_MODELS_DIR}"
                )
            return False

        async with self._lock:
            try:
                # v234.0: Check available RAM before loading
                _model_size_gb = model_path.stat().st_size / (1024 ** 3)
                try:
                    from backend.core.memory_quantizer import (
                        get_memory_quantizer,
                    )
                    _mq = await get_memory_quantizer()
                    _metrics = _mq.get_current_metrics()
                    _avail = _metrics.system_memory_available_gb
                    # Need model size + 1GB headroom
                    if _model_size_gb + 1.0 > _avail:
                        self.logger.warning(
                            f"[v234.0] Insufficient RAM for {model_path.name}: "
                            f"need {_model_size_gb + 1.0:.1f}GB, "
                            f"available {_avail:.1f}GB"
                        )
                        return False
                    self.logger.info(
                        f"[v234.0] RAM check passed: {_avail:.1f}GB available, "
                        f"model needs {_model_size_gb:.1f}GB"
                    )
                except Exception as e:
                    self.logger.debug(f"[v234.0] RAM check skipped: {e}")

                # Try to import llama-cpp-python
                from llama_cpp import Llama

                # v234.0: Detect Apple Silicon for Metal GPU offload
                _n_gpu = int(os.getenv("JARVIS_N_GPU_LAYERS", "-1"))
                import platform
                if platform.machine() != "arm64":
                    _n_gpu = 0  # No Metal on Intel

                self._model = Llama(
                    model_path=str(model_path),
                    n_ctx=PRIME_CONTEXT_LENGTH,
                    n_threads=os.cpu_count() or 4,
                    n_gpu_layers=_n_gpu,
                    verbose=False,
                )
                self._model_path = model_path
                self._loaded = True
                self.logger.info(
                    f"Loaded Prime model: {model_path.name} "
                    f"(GPU layers: {_n_gpu}, size: {_model_size_gb:.1f}GB)"
                )
                return True

            except ImportError:
                if not PrimeLocalClient._warned_missing_llama:
                    PrimeLocalClient._warned_missing_llama = True
                    self.logger.warning(
                        "llama-cpp-python not installed. "
                        "Install with: pip install llama-cpp-python"
                    )
                return False
            except Exception as e:
                self.logger.error(f"Failed to load model {model_path}: {e}")
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

            # v234.0: Run inference in single-worker executor (prevents concurrent OOM)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._inference_executor,
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


# =============================================================================
# v17.0: PRIME API CLIENT - Connect to J-Prime Server
# =============================================================================

class PrimeAPIClient(ModelClient):
    """
    v17.0: Client for JARVIS Prime API server (jarvis-prime repo).

    Connects to the J-Prime server running on localhost:8000 (configurable).
    This is the primary model serving pathway - uses the J-Prime server
    which manages local models, model hot-swap, and intelligent routing.

    Features:
    - Automatic service readiness detection with retry
    - OpenAI-compatible API format
    - Model discovery from J-Prime server
    - Circuit breaker integration
    - Graceful degradation when unavailable
    """

    def __init__(
        self,
        base_url: str = PRIME_API_URL,
        timeout: float = PRIME_API_TIMEOUT,
        wait_timeout: float = PRIME_API_WAIT_TIMEOUT,
    ):
        self.logger = logging.getLogger("PrimeAPIClient")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.wait_timeout = wait_timeout
        self._session = None
        self._available_models: List[str] = []
        self._ready = False
        self._last_health_check = 0.0
        self._health_check_cache_ttl = 10.0

    async def _get_session(self):
        """Get or create aiohttp session."""
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def _close_session(self):
        """Close aiohttp session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def wait_for_ready(self) -> bool:
        """
        Wait for J-Prime server to be ready.

        Uses ServiceReadinessChecker for intelligent waiting with
        exponential backoff.

        Returns:
            True if server is ready, False if timeout
        """
        try:
            from backend.core.ouroboros.integration import (
                ServiceReadinessChecker,
                ServiceReadinessLevel,
            )

            checker = ServiceReadinessChecker(
                service_name="jarvis_prime_api",
                base_url=self.base_url,
                health_check_timeout=3.0,
            )

            is_ready = await checker.wait_for_ready(
                timeout=self.wait_timeout,
                min_level=ServiceReadinessLevel.DEGRADED,
            )

            if is_ready:
                self._ready = True
                snapshot = checker.last_health_snapshot
                if snapshot and snapshot.available_models:
                    self._available_models = snapshot.available_models
                    self.logger.info(
                        f"[v17.0] J-Prime API ready with {len(self._available_models)} models"
                    )
                else:
                    self.logger.info("[v17.0] J-Prime API ready")
                return True
            else:
                self.logger.warning(
                    f"[v17.0] J-Prime API not ready after {self.wait_timeout}s"
                )
                return False

        except ImportError:
            # Fallback to simple health check
            return await self.health_check()

    async def discover_models(self) -> List[str]:
        """Discover available models from J-Prime server."""
        try:
            session = await self._get_session()
            async with session.get(f"{self.base_url}/v1/models") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = data.get("data", [])
                    self._available_models = [m.get("id", "unknown") for m in models]
                    return self._available_models
        except Exception as e:
            self.logger.debug(f"[v17.0] Model discovery failed: {e}")
        return []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a response using J-Prime API."""
        start_time = time.time()
        response = ModelResponse(
            provider=ModelProvider.PRIME_API,
            model_name=self._available_models[0] if self._available_models else "prime-api",
        )

        if not self._ready:
            # Try to become ready
            if not await self.wait_for_ready():
                response.success = False
                response.error = "J-Prime API not available"
                return response

        try:
            import aiohttp
            session = await self._get_session()

            # Use first available model or request override
            model_name = (
                getattr(request, 'model_override', None)
                or (self._available_models[0] if self._available_models else "default")
            )

            # Build OpenAI-compatible request
            messages = []
            if request.system_prompt:
                messages.append({"role": "system", "content": request.system_prompt})

            for msg in request.messages:
                messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                })

            payload = {
                "model": model_name,
                "messages": messages,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "stream": False,
            }

            async with session.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
            ) as resp:
                latency = (time.time() - start_time) * 1000
                response.latency_ms = latency

                if resp.status == 200:
                    data = await resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        response.content = choices[0].get("message", {}).get("content", "")
                        response.success = True
                        response.model_name = data.get("model", model_name)
                        usage = data.get("usage", {})
                        response.tokens_used = usage.get("total_tokens", 0)
                else:
                    error_text = await resp.text()
                    response.success = False
                    response.error = f"J-Prime API error {resp.status}: {error_text[:200]}"

        except aiohttp.ClientConnectorError as e:
            response.success = False
            response.error = f"J-Prime API connection refused: {e}"
            self._ready = False  # Mark as not ready for next attempt

        except asyncio.TimeoutError:
            response.success = False
            response.error = "J-Prime API timeout"

        except Exception as e:
            response.success = False
            response.error = f"J-Prime API error: {type(e).__name__}: {e}"

        return response

    async def generate_stream(self, request: ModelRequest) -> AsyncIterator[str]:
        """
        v17.0: Generate a streaming response using J-Prime API.

        Uses Server-Sent Events (SSE) in OpenAI-compatible format.
        The J-Prime server streams responses as "data: {...}" lines.
        """
        if not self._ready:
            if not await self.wait_for_ready():
                yield "[Error: J-Prime API not available]"
                return

        try:
            import aiohttp
            session = await self._get_session()

            # Use first available model or request override
            model_name = (
                getattr(request, 'model_override', None)
                or (self._available_models[0] if self._available_models else "default")
            )

            # Build OpenAI-compatible request
            messages = []
            if request.system_prompt:
                messages.append({"role": "system", "content": request.system_prompt})

            for msg in request.messages:
                messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                })

            payload = {
                "model": model_name,
                "messages": messages,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "stream": True,  # Enable streaming
            }

            # Use longer timeout for streaming
            stream_timeout = aiohttp.ClientTimeout(
                total=self.timeout * 3,  # 3x normal timeout for streaming
                sock_read=30.0,  # 30s read timeout between chunks
            )

            async with session.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                timeout=stream_timeout,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    yield f"[Error: J-Prime API {resp.status}: {error_text[:100]}]"
                    return

                # Parse SSE stream
                async for line in resp.content:
                    line = line.decode("utf-8").strip()

                    if not line:
                        continue

                    if line.startswith("data: "):
                        data_str = line[6:]  # Remove "data: " prefix

                        # Handle stream end marker
                        if data_str == "[DONE]":
                            break

                        try:
                            data = json.loads(data_str)
                            choices = data.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                        except json.JSONDecodeError:
                            # Skip malformed JSON chunks
                            self.logger.debug(f"[v17.0] Skipping malformed SSE: {data_str[:50]}")
                            continue

        except aiohttp.ClientConnectorError as e:
            self._ready = False
            yield f"[Error: J-Prime API connection refused: {e}]"

        except asyncio.TimeoutError:
            yield "[Error: J-Prime API stream timeout]"

        except Exception as e:
            self.logger.error(f"[v17.0] J-Prime streaming error: {e}")
            yield f"[Error: {type(e).__name__}: {e}]"

    async def health_check(self) -> bool:
        """Check if J-Prime server is healthy."""
        # Use cached result if recent
        now = time.time()
        if self._ready and (now - self._last_health_check) < self._health_check_cache_ttl:
            return True

        try:
            import aiohttp
            session = await self._get_session()

            async with session.get(
                f"{self.base_url}/health",
                timeout=aiohttp.ClientTimeout(total=5.0)
            ) as resp:
                self._last_health_check = now
                if resp.status == 200:
                    self._ready = True
                    return True

            # Try /v1/models as alternative health check
            async with session.get(
                f"{self.base_url}/v1/models",
                timeout=aiohttp.ClientTimeout(total=5.0)
            ) as resp:
                self._last_health_check = now
                if resp.status == 200:
                    self._ready = True
                    data = await resp.json()
                    models = data.get("data", [])
                    self._available_models = [m.get("id", "unknown") for m in models]
                    return True

        except aiohttp.ClientConnectorError:
            self.logger.debug("[v17.0] J-Prime API connection refused")
            self._ready = False

        except Exception as e:
            self.logger.debug(f"[v17.0] J-Prime API health check failed: {e}")
            self._ready = False

        return False

    def get_supported_tasks(self) -> List[TaskType]:
        """Get supported task types."""
        return [TaskType.CHAT, TaskType.REASONING, TaskType.CODE, TaskType.VISION]


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

        # v234.0: Task preferences with 3-tier fallback: PRIME_API → PRIME_LOCAL → CLAUDE
        # PRIME_API = GCP golden image VM or local J-Prime server (Tier 1)
        # PRIME_LOCAL = Local GGUF via llama-cpp-python (Tier 2)
        # CLAUDE = Anthropic API fallback (Tier 3)
        self._task_preferences: Dict[TaskType, List[ModelProvider]] = {
            TaskType.CHAT: [ModelProvider.PRIME_API, ModelProvider.PRIME_LOCAL, ModelProvider.PRIME_CLOUD_RUN, ModelProvider.CLAUDE],
            TaskType.REASONING: [ModelProvider.PRIME_API, ModelProvider.PRIME_LOCAL, ModelProvider.PRIME_CLOUD_RUN, ModelProvider.CLAUDE],
            TaskType.VISION: [ModelProvider.PRIME_API, ModelProvider.PRIME_CLOUD_RUN, ModelProvider.PRIME_LOCAL, ModelProvider.CLAUDE],
            TaskType.CODE: [ModelProvider.PRIME_API, ModelProvider.PRIME_LOCAL, ModelProvider.PRIME_CLOUD_RUN, ModelProvider.CLAUDE],
            TaskType.TOOL_USE: [ModelProvider.CLAUDE, ModelProvider.PRIME_API, ModelProvider.PRIME_LOCAL],
            TaskType.EMBEDDING: [ModelProvider.PRIME_API, ModelProvider.PRIME_LOCAL, ModelProvider.PRIME_CLOUD_RUN],
        }

        # v100.2: Performance-based preference weighting (adaptive routing)
        self._provider_performance: Dict[ModelProvider, Dict[str, float]] = {
            provider: {
                "success_rate": 1.0,
                "avg_latency_ms": 0.0,
                "error_count": 0,
                "total_requests": 0,
                "last_success": 0.0,
            }
            for provider in ModelProvider
        }

    def record_provider_result(
        self,
        provider: ModelProvider,
        success: bool,
        latency_ms: float = 0.0,
    ) -> None:
        """
        Record provider performance for adaptive routing (v100.2).

        This enables learning-based routing where providers with better
        performance metrics get higher priority dynamically.
        """
        perf = self._provider_performance[provider]
        perf["total_requests"] += 1

        if success:
            perf["last_success"] = time.time()
            # Exponential moving average for success rate (alpha=0.1)
            perf["success_rate"] = 0.9 * perf["success_rate"] + 0.1 * 1.0
            # Exponential moving average for latency
            if latency_ms > 0:
                if perf["avg_latency_ms"] == 0:
                    perf["avg_latency_ms"] = latency_ms
                else:
                    perf["avg_latency_ms"] = 0.9 * perf["avg_latency_ms"] + 0.1 * latency_ms
        else:
            perf["error_count"] += 1
            perf["success_rate"] = 0.9 * perf["success_rate"] + 0.1 * 0.0

    def _calculate_provider_score(self, provider: ModelProvider) -> float:
        """
        Calculate dynamic score for provider based on performance (v100.2).

        Higher score = better provider. Used for adaptive preference ordering.

        Score components:
        - Success rate: 0.6 weight (most important)
        - Latency: 0.2 weight (lower is better)
        - Recency: 0.2 weight (recently successful = higher score)
        """
        perf = self._provider_performance[provider]

        # Success rate component (0-1, higher is better)
        success_score = perf["success_rate"]

        # Latency component (normalize: 0-5000ms maps to 1-0)
        latency_ms = perf["avg_latency_ms"]
        if latency_ms <= 0:
            latency_score = 1.0  # No data = assume good
        else:
            latency_score = max(0.0, 1.0 - (latency_ms / 5000.0))

        # Recency component (how recently did this provider succeed)
        last_success = perf["last_success"]
        if last_success <= 0:
            recency_score = 0.5  # No data = neutral
        else:
            age_seconds = time.time() - last_success
            # Decay over 1 hour (3600 seconds)
            recency_score = max(0.0, 1.0 - (age_seconds / 3600.0))

        # Weighted combination
        return 0.6 * success_score + 0.2 * latency_score + 0.2 * recency_score

    def get_preferred_providers(
        self,
        request: ModelRequest,
        available_providers: List[ModelProvider]
    ) -> List[ModelProvider]:
        """
        Get ordered list of preferred providers for a request.

        v100.2: Now uses adaptive routing based on provider performance.
        Providers are scored dynamically and ordered by score within
        their task preference tier.
        """
        # Start with task preferences
        preferred = self._task_preferences.get(request.task_type, [ModelProvider.CLAUDE])

        # Filter to available providers
        result = [p for p in preferred if p in available_providers]

        # v100.2: Apply adaptive scoring within preference tiers
        # Providers with significantly better scores can be promoted
        if len(result) > 1:
            scored = [(p, self._calculate_provider_score(p)) for p in result]
            # Sort by score but respect tier boundaries (don't fully reorder)
            # Only swap adjacent items if score difference > 0.3
            for i in range(len(scored) - 1):
                if scored[i+1][1] - scored[i][1] > 0.3:
                    scored[i], scored[i+1] = scored[i+1], scored[i]
            result = [p for p, _ in scored]

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
            # Only Claude supports tool use (Prime tool use is experimental)
            result = [p for p in result if p == ModelProvider.CLAUDE]

        # Ensure we have at least one fallback
        if not result and ModelProvider.CLAUDE in available_providers:
            result = [ModelProvider.CLAUDE]

        return result

    def get_provider_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get performance statistics for all providers (v100.2)."""
        return {
            provider.value: {
                **self._provider_performance[provider],
                "score": self._calculate_provider_score(provider),
            }
            for provider in ModelProvider
        }


@dataclass
class RegisteredModel:
    """A model registered with the serving layer."""
    model_id: str
    model_path: Path
    model_type: str  # "lora", "merged", "gguf", "base"
    version: str
    registered_at: float = field(default_factory=time.time)
    source: str = "reactor_core"  # Where the model came from
    base_model: Optional[str] = None  # Base model this was fine-tuned from
    metrics: Dict[str, Any] = field(default_factory=dict)  # Training metrics
    active: bool = True  # Whether this model is currently being served
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_path": str(self.model_path),
            "model_type": self.model_type,
            "version": self.version,
            "registered_at": self.registered_at,
            "source": self.source,
            "base_model": self.base_model,
            "metrics": self.metrics,
            "active": self.active,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RegisteredModel":
        return cls(
            model_id=data["model_id"],
            model_path=Path(data["model_path"]),
            model_type=data.get("model_type", "unknown"),
            version=data.get("version", "1.0.0"),
            registered_at=data.get("registered_at", time.time()),
            source=data.get("source", "unknown"),
            base_model=data.get("base_model"),
            metrics=data.get("metrics", {}),
            active=data.get("active", True),
            metadata=data.get("metadata", {}),
        )


class UnifiedModelServing:
    """
    Unified model serving layer with Prime + Claude fallback.

    Provides seamless inference across local Prime models and Claude API.

    v100.1: Added model registration, hot-swap, and routing for Trinity Loop integration.
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

        # v100.1: Model registry for Trinity Loop hot-swap
        self._registered_models: Dict[str, RegisteredModel] = {}
        self._model_versions: Dict[str, List[str]] = defaultdict(list)  # model_id -> [versions]
        self._active_model_id: Optional[str] = None
        self._registry_file = MODEL_SERVING_DATA_DIR / "model_registry.json"

        # v100.1: Routing configuration
        self._routing_config: Dict[str, Dict[str, Any]] = {}
        self._routing_file = MODEL_SERVING_DATA_DIR / "routing_config.json"

        # Metrics
        self._request_count = 0
        self._total_latency_ms = 0.0
        self._total_cost_usd = 0.0
        self._provider_usage: Dict[str, int] = defaultdict(int)
        self._fallback_count = 0
        self._hot_swaps: int = 0

        # Ensure data directory
        MODEL_SERVING_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the model serving layer."""
        if self._running:
            return

        self._running = True
        self.logger.info("UnifiedModelServing starting...")

        # v100.1: Load persisted state
        await self._load_registry()
        await self._load_routing_config()

        # v17.0: Initialize clients in priority order
        # 1. J-Prime API (primary - connects to jarvis-prime repo server)
        # 2. Prime Local (fallback - direct GGUF loading if J-Prime unavailable)
        # 3. Cloud Run (serverless fallback)
        # 4. Claude (API fallback)

        prime_available = False

        # v17.0: Try J-Prime API first (primary model serving pathway)
        if PRIME_API_ENABLED:
            self.logger.info("  🔄 Checking J-Prime API availability...")
            client = PrimeAPIClient()
            if await client.wait_for_ready():
                self._clients[ModelProvider.PRIME_API] = client
                self.logger.info(f"  ✓ J-Prime API ready ({len(client._available_models)} models)")
                prime_available = True
            else:
                self.logger.info("  ⚠️ J-Prime API not available")
                self.logger.info(
                    "     → J-Prime server not running. Start jarvis-prime repo or "
                    "set JARVIS_PRIME_API_ENABLED=false to skip"
                )

        # v234.0: PRIME_LOCAL as Tier 2 — register even if PRIME_API available
        # Model loads lazily on first generate() call to conserve RAM
        if PRIME_LOCAL_ENABLED:
            self.logger.info("  🔄 Registering Prime Local (Tier 2, lazy-load)...")
            client = PrimeLocalClient()
            self._clients[ModelProvider.PRIME_LOCAL] = client
            self.logger.info(
                "  ✓ Prime Local client registered (model loads on demand)"
            )
            if not prime_available:
                prime_available = True

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
        elif not prime_available:
            self.logger.warning(
                "⚠️ No Prime model available - using Claude-only mode. "
                "Start J-Prime server for local model inference."
            )

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

    # =========================================================================
    # v100.1: Model Registration and Hot-Swap Methods (Trinity Loop Integration)
    # =========================================================================

    async def register_model(
        self,
        model_id: str,
        model_path: Union[str, Path],
        model_type: str = "lora",
        version: Optional[str] = None,
        source: str = "reactor_core",
        base_model: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
        activate: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Register a new trained model for serving.

        This method is called by trinity_handlers.py when Reactor Core
        completes training and sends a MODEL_READY event.

        Args:
            model_id: Unique identifier for the model
            model_path: Path to the model files
            model_type: Type of model ("lora", "merged", "gguf", "base")
            version: Model version (auto-generated if not provided)
            source: Where the model came from ("reactor_core", "manual", etc.)
            base_model: Base model this was fine-tuned from
            metrics: Training metrics (loss, accuracy, etc.)
            activate: Whether to make this the active model immediately
            metadata: Additional metadata

        Returns:
            True if registration successful
        """
        async with self._lock:
            try:
                path = Path(model_path)

                # Validate model path exists
                if not path.exists():
                    self.logger.error(f"Model path does not exist: {path}")
                    return False

                # Auto-generate version if not provided
                if version is None:
                    existing_versions = self._model_versions.get(model_id, [])
                    version = f"1.0.{len(existing_versions)}"

                # Create registered model entry
                registered_model = RegisteredModel(
                    model_id=model_id,
                    model_path=path,
                    model_type=model_type,
                    version=version,
                    source=source,
                    base_model=base_model,
                    metrics=metrics or {},
                    active=activate,
                    metadata=metadata or {},
                )

                # Generate unique key combining model_id and version
                registry_key = f"{model_id}@{version}"

                # Deactivate previous version if activating this one
                if activate:
                    for key, model in self._registered_models.items():
                        if model.model_id == model_id and model.active:
                            model.active = False

                # Register the model
                self._registered_models[registry_key] = registered_model
                self._model_versions[model_id].append(version)

                if activate:
                    self._active_model_id = registry_key
                    self._hot_swaps += 1

                # Persist registry
                await self._save_registry()

                self.logger.info(
                    f"Registered model: {model_id}@{version} "
                    f"(type={model_type}, active={activate}, source={source})"
                )

                # If it's a GGUF model and we have Prime Local client, trigger reload
                if model_type == "gguf" and ModelProvider.PRIME_LOCAL in self._clients:
                    client = self._clients[ModelProvider.PRIME_LOCAL]
                    if isinstance(client, PrimeLocalClient):
                        client._model_path = path
                        client._loaded = False
                        await client.load_model(str(path.name))
                        self.logger.info(f"Hot-swapped Prime Local model to: {path.name}")

                return True

            except Exception as e:
                self.logger.error(f"Model registration failed: {e}")
                return False

    async def rollback_model(
        self,
        model_id: str,
        target_version: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """
        Rollback to a previous model version.

        This method is called by trinity_handlers.py when a MODEL_ROLLBACK
        event is received, typically due to model validation failure.

        Args:
            model_id: Model identifier to rollback
            target_version: Specific version to rollback to (default: previous)
            reason: Reason for rollback (logged for auditing)

        Returns:
            True if rollback successful
        """
        async with self._lock:
            try:
                versions = self._model_versions.get(model_id, [])
                if len(versions) < 2:
                    self.logger.warning(f"No previous version to rollback to for {model_id}")
                    return False

                # Determine target version
                if target_version is None:
                    # Rollback to previous version
                    target_version = versions[-2]

                if target_version not in versions:
                    self.logger.error(f"Target version {target_version} not found for {model_id}")
                    return False

                target_key = f"{model_id}@{target_version}"
                if target_key not in self._registered_models:
                    self.logger.error(f"Model {target_key} not in registry")
                    return False

                # Deactivate current active model
                for key, model in self._registered_models.items():
                    if model.model_id == model_id and model.active:
                        model.active = False

                # Activate target version
                target_model = self._registered_models[target_key]
                target_model.active = True
                self._active_model_id = target_key

                # Persist changes
                await self._save_registry()

                self.logger.info(
                    f"Rolled back {model_id} to version {target_version}"
                    f"{f' (reason: {reason})' if reason else ''}"
                )

                # If it's a GGUF model, reload in Prime Local
                if target_model.model_type == "gguf" and ModelProvider.PRIME_LOCAL in self._clients:
                    client = self._clients[ModelProvider.PRIME_LOCAL]
                    if isinstance(client, PrimeLocalClient):
                        client._model_path = target_model.model_path
                        client._loaded = False
                        await client.load_model(str(target_model.model_path.name))

                return True

            except Exception as e:
                self.logger.error(f"Rollback failed: {e}")
                return False

    async def update_routing(
        self,
        model_id: Optional[str] = None,
        task_preferences: Optional[Dict[str, List[str]]] = None,
        provider_weights: Optional[Dict[str, float]] = None,
        circuit_config: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Update model routing configuration.

        This method allows dynamic adjustment of which models handle
        which task types, useful after training new specialized models.

        Args:
            model_id: Optional model to configure routing for
            task_preferences: Task type -> provider list mapping
            provider_weights: Provider -> weight mapping for load balancing
            circuit_config: Circuit breaker configuration updates

        Returns:
            True if update successful
        """
        async with self._lock:
            try:
                config_key = model_id or "_default"

                if config_key not in self._routing_config:
                    self._routing_config[config_key] = {}

                if task_preferences:
                    # Update router task preferences
                    for task_str, providers in task_preferences.items():
                        try:
                            task = TaskType(task_str)
                            provider_enums = [ModelProvider(p) for p in providers]
                            self._router._task_preferences[task] = provider_enums
                        except ValueError as e:
                            self.logger.warning(f"Invalid task/provider in routing update: {e}")

                    self._routing_config[config_key]["task_preferences"] = task_preferences

                if provider_weights:
                    self._routing_config[config_key]["provider_weights"] = provider_weights
                    # Could be used for weighted load balancing (future enhancement)

                if circuit_config:
                    # Update circuit breaker settings
                    if "failure_threshold" in circuit_config:
                        self._circuit_breaker.failure_threshold = circuit_config["failure_threshold"]
                    if "recovery_seconds" in circuit_config:
                        self._circuit_breaker.recovery_seconds = circuit_config["recovery_seconds"]

                    self._routing_config[config_key]["circuit_config"] = circuit_config

                # Persist routing config
                await self._save_routing_config()

                self.logger.info(f"Updated routing config for {config_key}")
                return True

            except Exception as e:
                self.logger.error(f"Routing update failed: {e}")
                return False

    async def get_registered_models(
        self,
        model_id: Optional[str] = None,
        active_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Get list of registered models.

        Args:
            model_id: Filter by model ID
            active_only: Only return active models

        Returns:
            List of model info dictionaries
        """
        result = []

        for key, model in self._registered_models.items():
            if model_id and model.model_id != model_id:
                continue
            if active_only and not model.active:
                continue

            result.append(model.to_dict())

        return result

    async def _save_registry(self) -> None:
        """Persist model registry to disk."""
        try:
            import aiofiles

            data = {
                key: model.to_dict()
                for key, model in self._registered_models.items()
            }

            async with aiofiles.open(self._registry_file, "w") as f:
                await f.write(json.dumps(data, indent=2, default=str))

        except Exception as e:
            self.logger.error(f"Failed to save registry: {e}")

    async def _load_registry(self) -> None:
        """Load model registry from disk."""
        try:
            if not self._registry_file.exists():
                return

            import aiofiles

            async with aiofiles.open(self._registry_file, "r") as f:
                content = await f.read()
                data = json.loads(content)

            for key, model_data in data.items():
                model = RegisteredModel.from_dict(model_data)
                self._registered_models[key] = model
                if model.version not in self._model_versions[model.model_id]:
                    self._model_versions[model.model_id].append(model.version)
                if model.active:
                    self._active_model_id = key

            self.logger.info(f"Loaded {len(self._registered_models)} models from registry")

        except Exception as e:
            self.logger.warning(f"Failed to load registry: {e}")

    async def _save_routing_config(self) -> None:
        """Persist routing configuration to disk."""
        try:
            import aiofiles

            async with aiofiles.open(self._routing_file, "w") as f:
                await f.write(json.dumps(self._routing_config, indent=2))

        except Exception as e:
            self.logger.error(f"Failed to save routing config: {e}")

    async def _load_routing_config(self) -> None:
        """Load routing configuration from disk."""
        try:
            if not self._routing_file.exists():
                return

            import aiofiles

            async with aiofiles.open(self._routing_file, "r") as f:
                content = await f.read()
                self._routing_config = json.loads(content)

            self.logger.info("Loaded routing configuration")

        except Exception as e:
            self.logger.warning(f"Failed to load routing config: {e}")

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
            "hot_swaps": self._hot_swaps,
            "registered_models": len(self._registered_models),
            "active_model": self._active_model_id,
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


# v100.1: Alias for Trinity Loop integration (trinity_handlers.py uses this name)
async def get_unified_model_serving() -> UnifiedModelServing:
    """
    Get the global UnifiedModelServing instance.

    Alias for get_model_serving() for Trinity Loop integration compatibility.
    """
    return await get_model_serving()


# =============================================================================
# v234.0: GCP Endpoint Hot-Swap (called by unified_supervisor.py)
# =============================================================================

async def notify_gcp_endpoint_ready(url: str) -> bool:
    """
    v234.0: Notify UnifiedModelServing that GCP endpoint is ready.

    Called by unified_supervisor._propagate_invincible_node_url() when the
    golden image VM becomes healthy. Validates the endpoint, then updates
    the PrimeAPIClient to route to the GCP endpoint.

    Args:
        url: Full URL of the GCP endpoint (e.g., "http://34.56.78.90:8000")

    Returns:
        True if endpoint was updated successfully
    """
    global _model_serving, PRIME_API_URL
    if _model_serving is None:
        return False

    url = url.rstrip("/")
    logger = logging.getLogger("UnifiedModelServing")

    # v234.0: Update module-level constant so any NEW PrimeAPIClient
    # instances created after this point use the GCP URL, not localhost
    PRIME_API_URL = url

    client = _model_serving._clients.get(ModelProvider.PRIME_API)
    if client and isinstance(client, PrimeAPIClient):
        # v234.0: Validate new endpoint BEFORE closing old session
        test_client = PrimeAPIClient(base_url=url)
        if not await test_client.wait_for_ready():
            logger.warning(
                f"[v234.0] GCP endpoint failed validation ({url}), "
                f"keeping current endpoint ({client.base_url})"
            )
            await test_client._close_session()
            return False
        await test_client._close_session()

        old_url = client.base_url
        client.base_url = url
        client._ready = False  # Force re-validation on next request
        await client._close_session()  # Close old connection pool
        # Reset circuit breaker to give GCP a clean slate
        _model_serving._circuit_breaker.record_success(
            ModelProvider.PRIME_API.value
        )
        logger.info(f"[v234.0] GCP endpoint hot-swapped: {old_url} → {url}")
        return True
    else:
        # No PrimeAPIClient yet — create one pointing at GCP
        new_client = PrimeAPIClient(base_url=url)
        if await new_client.wait_for_ready():
            _model_serving._clients[ModelProvider.PRIME_API] = new_client
            _model_serving._circuit_breaker.record_success(
                ModelProvider.PRIME_API.value
            )
            logger.info(f"[v234.0] GCP endpoint activated: {url}")
            return True
        else:
            logger.warning(f"[v234.0] GCP endpoint not ready: {url}")
            await new_client._close_session()
            return False


async def notify_gcp_endpoint_unhealthy() -> None:
    """
    v234.0: GCP endpoint is unhealthy — trip circuit breaker to force fallback.

    Called by unified_supervisor._clear_invincible_node_url() when the golden
    image VM becomes unreachable. Trips the PRIME_API circuit breaker so
    requests automatically fall through to PRIME_LOCAL (Tier 2) or CLAUDE (Tier 3).
    """
    global _model_serving
    if _model_serving is None:
        return

    logger = logging.getLogger("UnifiedModelServing")

    # Trip circuit breaker (need failure_threshold failures to open)
    for _ in range(CIRCUIT_BREAKER_FAILURE_THRESHOLD):
        _model_serving._circuit_breaker.record_failure(
            ModelProvider.PRIME_API.value
        )

    logger.warning(
        "[v234.0] GCP endpoint marked unhealthy — "
        "PRIME_API circuit breaker opened, falling through to Tier 2/3"
    )
