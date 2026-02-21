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
import threading
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

# v3A.0: Canonical circuit breaker from kernel
try:
    from backend.kernel.circuit_breaker import (
        get_circuit_breaker as _get_canonical_cb,
        CircuitBreakerConfig as _CBConfig,
        CircuitBreakerState as _CanonicalCBState,
    )
    _CANONICAL_CB_AVAILABLE = True
except ImportError:
    _CANONICAL_CB_AVAILABLE = False
    _get_canonical_cb = None
    _CBConfig = None
    _CanonicalCBState = None

# Phase 5A: Bounded queue backpressure
try:
    from backend.core.bounded_queue import BoundedAsyncQueue, OverflowPolicy
except ImportError:
    BoundedAsyncQueue = None

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

    # ── Concrete helpers available to all subclasses ──────────────

    def _build_request_metadata(self, request: ModelRequest) -> Dict[str, Any]:
        """Build JSON-safe metadata propagated to J-Prime routing layer.

        Shared by PrimeAPIClient and PrimeCloudRunClient so task-type
        and conversation context reach J-Prime regardless of pathway.
        """
        metadata = dict(request.context or {})
        metadata.setdefault("model_task_type", request.task_type.value)
        try:
            json.dumps(metadata)
            return metadata
        except TypeError:
            _log = getattr(self, "logger", logging.getLogger(__name__))
            _log.warning(
                "[Model] Metadata contains non-serializable values, "
                "stringifying: %s",
                list(metadata.keys()),
            )
            return {str(key): str(value) for key, value in metadata.items()}


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

    # v234.2: Quantization-aware model catalog
    # Sorted by quality_rank (best quality first). Dynamic selection
    # iterates this list and picks the best model that fits available RAM.
    QUANT_CATALOG: List[Dict[str, Any]] = [
        {
            "name": "mistral-7b-q8",
            "filename": "mistral-7b-instruct-v0.2.Q8_0.gguf",
            "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
            "size_mb": 7700,
            "min_ram_gb": 12,
            "context_length": 32768,
            "quality_rank": 1,
        },
        {
            "name": "llama-3-8b-q4",
            "filename": "Meta-Llama-3-8B-Instruct.Q4_K_M.gguf",
            "repo_id": "MaziyarPanahi/Meta-Llama-3-8B-Instruct-GGUF",
            "size_mb": 4900,
            "min_ram_gb": 10,
            "context_length": 8192,
            "quality_rank": 2,
        },
        {
            "name": "mistral-7b-q4",
            "filename": "mistral-7b-instruct-v0.2.Q4_K_M.gguf",
            "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
            "size_mb": 4370,
            "min_ram_gb": 8,
            "context_length": 32768,
            "quality_rank": 3,
        },
        {
            "name": "phi-3-mini-q4",
            "filename": "Phi-3-mini-4k-instruct-q4.gguf",
            "repo_id": "microsoft/Phi-3-mini-4k-instruct-gguf",
            "size_mb": 2500,
            "min_ram_gb": 6,
            "context_length": 4096,
            "quality_rank": 4,
        },
        {
            "name": "phi-2-q4",
            "filename": "phi-2.Q4_K_M.gguf",
            "repo_id": "TheBloke/phi-2-GGUF",
            "size_mb": 1800,
            "min_ram_gb": 4,
            "context_length": 2048,
            "quality_rank": 5,
        },
        # v234.3: Lower quantization levels for constrained RAM
        {
            "name": "mistral-7b-q3",
            "filename": "mistral-7b-instruct-v0.2.Q3_K_S.gguf",
            "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
            "size_mb": 3160,
            "min_ram_gb": 5,
            "context_length": 32768,
            "quality_rank": 6,
        },
        {
            "name": "mistral-7b-q2",
            "filename": "mistral-7b-instruct-v0.2.Q2_K.gguf",
            "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
            "size_mb": 2720,
            "min_ram_gb": 4,
            "context_length": 32768,
            "quality_rank": 7,
        },
        {
            "name": "tinyllama-1.1b-q4",
            "filename": "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
            "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
            "size_mb": 670,
            "min_ram_gb": 1,
            "context_length": 2048,
            "quality_rank": 8,
        },
    ]

    # Model name aliases — discovers ANY model from the catalog
    MODEL_ALIASES = {
        "prime-7b-chat-v1.Q4_K_M.gguf": [
            entry["filename"] for entry in QUANT_CATALOG
        ],
    }

    # HuggingFace repo mappings — built from QUANT_CATALOG
    HF_MODEL_REPOS = {
        entry["filename"]: entry["repo_id"]
        for entry in QUANT_CATALOG
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
                    # v234.2: Prefer smaller files in fallback (safer for RAM)
                    # Dynamic quality selection happens in _select_best_model()
                    gguf_files.sort(key=lambda p: p.stat().st_size)
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

    async def _select_best_model(
        self, available_gb: float
    ) -> Optional[Tuple[Dict[str, Any], Path]]:
        """
        v234.2/v234.3: Select the best quantization level that fits
        available RAM.

        Iterates QUANT_CATALOG by quality_rank (best first), checks if
        min_ram_gb fits, then looks for the model on disk. If no on-disk
        model fits, attempts auto-download of the best candidate.

        v234.3: When mmap is enabled, min_ram_gb thresholds are reduced
        by 40% since physical RAM usage is ~50% of file size.

        Args:
            available_gb: Available system RAM in GB

        Returns:
            Tuple of (catalog_entry, model_path) or None
        """
        # v234.3: mmap reduces physical RAM requirements
        _use_mmap = os.getenv(
            "JARVIS_USE_MMAP", "true"
        ).lower() in ("true", "1", "yes")
        _mmap_factor = 0.65 if _use_mmap else 1.0  # v235.1: 0.6→0.65 (less aggressive)

        download_candidate = None

        for entry in self.QUANT_CATALOG:
            _effective_min = entry["min_ram_gb"] * _mmap_factor
            if _effective_min > available_gb:
                self.logger.debug(
                    f"[v234.3] Skipping {entry['name']}: "
                    f"needs {_effective_min:.1f}GB "
                    f"(base={entry['min_ram_gb']}GB, "
                    f"mmap={'on' if _use_mmap else 'off'}), "
                    f"have {available_gb:.1f}GB"
                )
                continue

            # Check if this model exists on disk
            model_path = self._discover_model(entry["filename"])
            if model_path is not None:
                self.logger.info(
                    f"[v234.3] Selected {entry['name']} "
                    f"(quality_rank={entry['quality_rank']}, "
                    f"needs {_effective_min:.1f}GB "
                    f"[base={entry['min_ram_gb']}GB, "
                    f"mmap={'on' if _use_mmap else 'off'}], "
                    f"have {available_gb:.1f}GB)"
                )
                return (entry, model_path)

            # Track best downloadable candidate
            if download_candidate is None:
                download_candidate = entry

        # No on-disk model fits — try auto-download
        if download_candidate and not self._discovery_attempted:
            self._discovery_attempted = True
            self.logger.info(
                f"[v234.2] No suitable model on disk. "
                f"Attempting download: {download_candidate['name']}"
            )
            model_path = await self._auto_download_model(
                download_candidate["filename"]
            )
            if model_path is not None:
                return (download_candidate, model_path)

        return None

    async def load_model(self, model_name: Optional[str] = None) -> bool:
        """
        Load a Prime model with intelligent, memory-aware discovery.

        v234.2: Uses QUANT_CATALOG to dynamically select the best
        quantization level for available RAM. Falls through the quality
        ladder (Q8 -> Q4 -> Phi-3 -> Phi-2) until a model fits.

        Args:
            model_name: Optional specific model filename to load
                       (bypasses dynamic selection)

        Returns:
            True if model loaded successfully
        """
        async with self._lock:
            # If already loaded, skip
            if self._loaded and self._model is not None:
                return True

            try:
                # Step 1: Get available RAM
                available_gb = None
                try:
                    from backend.core.memory_quantizer import (
                        get_memory_quantizer,
                    )
                    _mq = await get_memory_quantizer()
                    _metrics = _mq.get_current_metrics()
                    available_gb = _metrics.system_memory_available_gb
                    self.logger.info(
                        f"[v234.2] Available RAM: {available_gb:.1f}GB"
                    )
                except Exception as e:
                    self.logger.debug(
                        f"[v234.2] MemoryQuantizer unavailable: {e}"
                    )

                # Step 2: Select best model for available RAM
                selected_entry = None
                model_path = None

                if model_name is not None:
                    # Explicit model requested — use it directly
                    model_path = self._discover_model(model_name)
                    if model_path is None and not self._discovery_attempted:
                        self._discovery_attempted = True
                        model_path = await self._auto_download_model(
                            model_name
                        )
                elif available_gb is not None:
                    # Dynamic selection from QUANT_CATALOG
                    result = await self._select_best_model(available_gb)
                    if result is not None:
                        selected_entry, model_path = result
                else:
                    # Fallback: no memory info, try default model
                    model_path = self._discover_model(PRIME_DEFAULT_MODEL)
                    if model_path is None and not self._discovery_attempted:
                        self._discovery_attempted = True
                        model_path = await self._auto_download_model(
                            PRIME_DEFAULT_MODEL
                        )

                if model_path is None:
                    if not PrimeLocalClient._warned_missing_model:
                        PrimeLocalClient._warned_missing_model = True
                        self.logger.warning(
                            f"No suitable GGUF model found. "
                            f"Available RAM: {available_gb or 'unknown'}GB. "
                            f"Set JARVIS_PRIME_AUTO_DOWNLOAD=true to enable "
                            f"auto-download, or place a model in "
                            f"{PRIME_MODELS_DIR}"
                        )
                    return False

                # Step 3: Final RAM check (actual file size + headroom)
                # v234.3: mmap reduces physical RAM to ~50% of file size
                _use_mmap = os.getenv(
                    "JARVIS_USE_MMAP", "true"
                ).lower() in ("true", "1", "yes")
                _model_size_gb = model_path.stat().st_size / (1024 ** 3)
                _effective_size_gb = (
                    _model_size_gb * 0.5 if _use_mmap
                    else _model_size_gb
                )

                # v235.1: Dynamic headroom based on memory pressure tier (Fix C1)
                _headroom_gb = 1.0  # default
                _tier = None
                try:
                    from backend.core.memory_quantizer import MemoryTier
                    _tier = _metrics.tier if '_metrics' in dir() and _metrics else None
                    if _tier in (MemoryTier.ABUNDANT, MemoryTier.OPTIMAL):
                        _headroom_gb = 0.75
                    elif _tier == MemoryTier.ELEVATED:
                        _headroom_gb = 1.0
                    elif _tier == MemoryTier.CONSTRAINED:
                        _headroom_gb = 1.5
                    elif _tier in (MemoryTier.CRITICAL, MemoryTier.EMERGENCY):
                        self.logger.warning(
                            f"[v235.1] Memory tier {_tier.value} — refusing to load model "
                            f"(available: {available_gb:.1f}GB)"
                        )
                        return False
                except (ImportError, NameError):
                    pass

                # Step 4: Load via llama-cpp-python
                from llama_cpp import Llama

                _n_gpu = int(os.getenv("JARVIS_N_GPU_LAYERS", "-1"))
                import platform
                if platform.machine() != "arm64":
                    _n_gpu = 0

                # Use context_length from catalog if available
                # v234.3: Reduce n_ctx under memory pressure to save
                # KV cache RAM (~64MB per 1024 tokens for 7B models)
                _ctx = PRIME_CONTEXT_LENGTH
                if selected_entry and "context_length" in selected_entry:
                    _ctx = selected_entry["context_length"]
                if available_gb is not None and available_gb < 6.0:
                    _ctx = min(_ctx, 2048)
                    self.logger.info(
                        f"[v234.3] Reducing context to {_ctx} "
                        f"(low RAM: {available_gb:.1f}GB)"
                    )

                # v235.1: Include KV cache in RAM estimation (Fix C3)
                # KV cache ~64MB per 1024 tokens for 7B-class models
                _model_size_mb = selected_entry.get("size_mb", 4000) if selected_entry else 4000
                _size_scale = min(2.0, _model_size_mb / 4000)
                _kv_cache_gb = (_ctx / 1024) * 0.064 * _size_scale

                if available_gb is not None:
                    _total_needed = _effective_size_gb + _kv_cache_gb + _headroom_gb
                    if _total_needed > available_gb:
                        self.logger.warning(
                            f"[v235.1] Insufficient RAM including KV cache: "
                            f"model={_effective_size_gb:.2f}GB + kv={_kv_cache_gb:.2f}GB "
                            f"+ headroom={_headroom_gb:.1f}GB = {_total_needed:.2f}GB, "
                            f"available={available_gb:.1f}GB, ctx={_ctx} "
                            f"(tier: {_tier.value if _tier else 'unknown'})"
                        )
                        # Try reducing context before giving up
                        if _ctx > 2048:
                            _reduced_ctx = 2048
                            _reduced_kv = (_reduced_ctx / 1024) * 0.064 * _size_scale
                            _reduced_total = _effective_size_gb + _reduced_kv + _headroom_gb
                            if _reduced_total <= available_gb:
                                self.logger.info(
                                    f"[v235.1] Reducing context {_ctx}->{_reduced_ctx} to fit: "
                                    f"{_reduced_total:.2f}GB needed, {available_gb:.1f}GB available"
                                )
                                _ctx = _reduced_ctx
                                _kv_cache_gb = _reduced_kv
                            else:
                                return False
                        else:
                            return False
                    else:
                        self.logger.info(
                            f"[v235.1] RAM budget: model={_effective_size_gb:.2f}GB "
                            f"+ kv={_kv_cache_gb:.2f}GB + headroom={_headroom_gb:.1f}GB "
                            f"= {_total_needed:.2f}GB / {available_gb:.1f}GB available "
                            f"(tier: {_tier.value if _tier else 'unknown'})"
                        )

                self._model = Llama(
                    model_path=str(model_path),
                    n_ctx=_ctx,
                    n_threads=os.cpu_count() or 4,
                    n_gpu_layers=_n_gpu,
                    use_mmap=_use_mmap,
                    verbose=False,
                )

                # v235.1: Post-load memory validation (Fix C2)
                try:
                    import psutil
                    _post_mem = psutil.virtual_memory()
                    _post_available = _post_mem.available / (1024 ** 3)
                    _ram_delta = (available_gb - _post_available) if available_gb else 0
                    self.logger.info(
                        f"[v235.1] Post-load RAM: available={_post_available:.1f}GB "
                        f"(delta={_ram_delta:.2f}GB, predicted={_effective_size_gb:.2f}GB)"
                    )
                    if available_gb and _ram_delta > _effective_size_gb * 1.5:
                        self.logger.warning(
                            f"[v235.1] Model used {_ram_delta:.2f}GB vs predicted "
                            f"{_effective_size_gb:.2f}GB — 50%+ over budget. "
                            f"Consider lower quantization."
                        )
                    if _post_available < 1.0:
                        self.logger.warning(
                            f"[v235.1] CRITICAL: Only {_post_available:.1f}GB RAM remaining "
                            f"after model load. System may become unstable."
                        )
                except Exception:
                    pass

                self._model_path = model_path
                self._loaded = True
                _name = (
                    selected_entry["name"]
                    if selected_entry
                    else model_path.name
                )
                self.logger.info(
                    f"[v234.3] Loaded: {_name} "
                    f"(GPU layers: {_n_gpu}, "
                    f"size: {_model_size_gb:.1f}GB, "
                    f"mmap: {_use_mmap}, "
                    f"effective: {_effective_size_gb:.1f}GB, "
                    f"ctx: {_ctx})"
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
        """v239.0: Streaming with cancellation token + adaptive timeout.

        Routes streaming inference through self._inference_executor
        (single-worker ThreadPoolExecutor) to serialize with non-streaming
        generate() calls. Uses asyncio.Queue as a thread-safe bridge.

        Adaptive timeout: 90s for first chunk (cold start), 30s thereafter.
        Cancellation token stops the inference thread on timeout/disconnect.
        """
        if not self._loaded or self._model is None:
            if not await self.load_model():
                yield "[Error: Model not loaded]"
                return

        prompt = self._build_prompt(request.messages, request.system_prompt)
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = (
            BoundedAsyncQueue(maxsize=500, policy=OverflowPolicy.BLOCK, name="llm_stream_bridge")
            if BoundedAsyncQueue is not None else asyncio.Queue()
        )
        _sentinel = object()
        _cancel = threading.Event()  # v239.0: cancellation signal
        fut = None

        def _stream_in_thread() -> None:
            try:
                for chunk in self._model(
                    prompt,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    stop=["</s>", "Human:", "User:"],
                    stream=True,
                ):
                    if _cancel.is_set():
                        break
                    text = chunk["choices"][0]["text"]
                    if text:
                        loop.call_soon_threadsafe(queue.put_nowait, text)
                loop.call_soon_threadsafe(queue.put_nowait, _sentinel)
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)

        try:
            fut = loop.run_in_executor(self._inference_executor, _stream_in_thread)

            # v239.0: Adaptive per-chunk timeout
            _first_chunk_timeout = float(os.environ.get(
                "JARVIS_STREAM_FIRST_CHUNK_TIMEOUT", "90"
            ))
            _chunk_timeout = float(os.environ.get(
                "JARVIS_STREAM_CHUNK_TIMEOUT", "30"
            ))
            _current_timeout = _first_chunk_timeout

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_current_timeout)
                except asyncio.TimeoutError:
                    _cancel.set()
                    self.logger.warning(
                        f"[v239.0] Stream chunk timeout ({_current_timeout:.0f}s)"
                    )
                    raise TimeoutError(
                        f"Stream timeout ({_current_timeout:.0f}s) — model stopped responding"
                    )

                if item is _sentinel:
                    break
                if isinstance(item, BaseException):
                    self.logger.error(f"Prime streaming error: {item}")
                    raise item
                yield item
                _current_timeout = _chunk_timeout

            await fut

        except Exception as e:
            # v239.0: Signal cancellation and wait for executor thread to finish
            # so it doesn't block the next inference on the single-worker executor.
            _cancel.set()
            if fut is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(fut), timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    pass
            self.logger.error(f"Prime streaming error: {e}")
            raise  # Re-raise so UnifiedModelServing.generate_stream can failover

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
            # v258.3: Always include — _build_request_metadata guarantees
            # at least model_task_type is present (setdefault).
            payload["metadata"] = self._build_request_metadata(request)

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
            # v258.3: Always include — _build_request_metadata guarantees
            # at least model_task_type is present (setdefault).
            payload["metadata"] = self._build_request_metadata(request)

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
                "metadata": self._build_request_metadata(request),
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
    """
    Circuit breaker for model clients.

    v3A.0: Delegates to the canonical CircuitBreakerRegistry from
    backend.kernel.circuit_breaker when available. Falls back to inline
    implementation otherwise. Preserves the same synchronous per-provider
    interface used by UnifiedModelServing and module-level helper functions.

    Backward compatibility:
    - self._states dict is maintained for external code that reads it
      (e.g. get_stats() serializes _states)
    - self.failure_threshold and self.recovery_seconds remain mutable
      for runtime config updates
    """

    def __init__(
        self,
        failure_threshold: int = CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        recovery_seconds: float = CIRCUIT_BREAKER_RECOVERY_SECONDS
    ):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.logger = logging.getLogger("CircuitBreaker")

        # Backward-compat: keep _states dict for external code that reads it
        self._states: Dict[str, CircuitBreakerState] = {}
        # v240.0: Thread-safe lock for all state mutations.
        # Prevents race conditions between concurrent async tasks that
        # interleave at await points between can_execute() and record_failure().
        self._lock = threading.Lock()

        # v3A.0: Whether to use canonical circuit breakers
        self._use_canonical = _CANONICAL_CB_AVAILABLE

    def _get_cb(self, provider: str):
        """v3A.0: Get or create canonical CircuitBreaker for this provider."""
        if not self._use_canonical:
            return None
        cb_name = f"model-serving-{provider}"
        return _get_canonical_cb(
            cb_name,
            _CBConfig(
                failure_threshold=self.failure_threshold,
                recovery_timeout_seconds=self.recovery_seconds,
            ),
        )

    def _sync_state_from_canonical(self, provider: str, cb) -> None:
        """v3A.0: Sync backward-compat _states dict from canonical breaker."""
        # Map canonical state to local CircuitState enum
        state_map = {
            "closed": CircuitState.CLOSED,
            "open": CircuitState.OPEN,
            "half_open": CircuitState.HALF_OPEN,
        }
        if provider not in self._states:
            self._states[provider] = CircuitBreakerState()
        local = self._states[provider]
        local.state = state_map.get(cb.state.value, CircuitState.CLOSED)
        local.failure_count = cb._failure_count
        local.last_failure_time = (
            cb._last_failure_time.timestamp() if cb._last_failure_time else 0.0
        )
        local.last_success_time = (
            cb._last_success_time.timestamp() if cb._last_success_time else time.time()
        )

    def get_state(self, provider: str) -> CircuitBreakerState:
        """Get circuit state for a provider."""
        cb = self._get_cb(provider)
        if cb is not None:
            self._sync_state_from_canonical(provider, cb)
            return self._states[provider]

        with self._lock:
            if provider not in self._states:
                self._states[provider] = CircuitBreakerState()
            return self._states[provider]

    def can_execute(self, provider: str) -> bool:
        """Check if requests can be made to this provider."""
        cb = self._get_cb(provider)
        if cb is not None:
            allowed = cb.can_execute_sync()
            self._sync_state_from_canonical(provider, cb)
            if not allowed:
                self.logger.debug(f"Circuit for {provider} is open (canonical)")
            return allowed

        # Fallback: inline implementation
        with self._lock:
            state = self._states.get(provider)
            if state is None:
                self._states[provider] = CircuitBreakerState()
                return True

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
        cb = self._get_cb(provider)
        if cb is not None:
            # Sync-safe direct state mutation for the canonical breaker.
            # We use self._lock (threading.Lock) because the canonical breaker's
            # _state_lock is asyncio.Lock and cannot be acquired synchronously.
            # CPython's GIL makes individual attribute writes atomic, so this
            # threading lock provides the same mutual exclusion as the original code.
            from datetime import datetime as _dt
            with self._lock:
                cb._success_count += 1
                cb._last_success_time = _dt.now()
                cb._failure_count = 0
                if cb._state == _CanonicalCBState.HALF_OPEN:
                    if cb._success_count >= cb._config.success_threshold:
                        cb._state = _CanonicalCBState.CLOSED
                        cb._failure_count = 0
                        cb._success_count = 0
                        self.logger.info(f"Circuit for {provider} closed (recovered)")
                elif cb._state == _CanonicalCBState.CLOSED:
                    cb._failure_count = 0
            self._sync_state_from_canonical(provider, cb)
            return

        # Fallback: inline implementation
        with self._lock:
            state = self._states.get(provider)
            if state is None:
                self._states[provider] = CircuitBreakerState()
                return
            state.failure_count = 0
            state.last_success_time = time.time()
            state.total_successes += 1

            if state.state == CircuitState.HALF_OPEN:
                state.state = CircuitState.CLOSED
                self.logger.info(f"Circuit for {provider} closed (recovered)")

    def record_failure(self, provider: str) -> None:
        """Record a failed request."""
        cb = self._get_cb(provider)
        if cb is not None:
            # Sync-safe direct state mutation (see record_success for rationale)
            from datetime import datetime as _dt
            with self._lock:
                cb._failure_count += 1
                cb._last_failure_time = _dt.now()
                # Record in history
                cb._failure_history.append({
                    "time": _dt.now().isoformat(),
                    "error": f"model-serving {provider} failure",
                    "state": cb._state.value,
                })
                if len(cb._failure_history) > 100:
                    cb._failure_history = cb._failure_history[-50:]
                if cb._state == _CanonicalCBState.HALF_OPEN:
                    cb._state = _CanonicalCBState.OPEN
                    cb._success_count = 0
                    self.logger.warning(f"Circuit for {provider} opened (too many failures)")
                elif cb._failure_count >= cb._config.failure_threshold:
                    if cb._state != _CanonicalCBState.OPEN:
                        cb._state = _CanonicalCBState.OPEN
                        self.logger.warning(f"Circuit for {provider} opened (too many failures)")
            self._sync_state_from_canonical(provider, cb)
            return

        # Fallback: inline implementation
        with self._lock:
            state = self._states.get(provider)
            if state is None:
                self._states[provider] = CircuitBreakerState()
                state = self._states[provider]
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

        # v239.0: Relative cost efficiency per provider (higher = cheaper)
        self._PROVIDER_COST_EFFICIENCY: Dict[ModelProvider, float] = {
            ModelProvider.PRIME_LOCAL: 1.0,      # Free (already-running machine)
            ModelProvider.PRIME_API: 0.9,        # GCP VM (~$0.02/hr amortized)
            ModelProvider.PRIME_CLOUD_RUN: 0.7,  # Cloud Run per-request pricing
            ModelProvider.CLAUDE: 0.3,           # $3/M input + $15/M output tokens
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

        # v239.0: Cost-aware scoring with protected success floor
        _cost_weight = max(0.0, min(0.50, float(os.environ.get("JARVIS_ROUTING_COST_WEIGHT", "0.10"))))
        _success_weight = max(0.50, 0.6 * (1.0 - _cost_weight))
        _remaining = 1.0 - _success_weight - _cost_weight
        _latency_weight = _remaining * 0.5
        _recency_weight = _remaining * 0.5

        cost_score = self._PROVIDER_COST_EFFICIENCY.get(provider, 0.5)

        return (
            _success_weight * success_score +
            _latency_weight * latency_score +
            _recency_weight * recency_score +
            _cost_weight * cost_score
        )

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

        # v239.0: When GCP boot is in progress and PRIME_LOCAL has no model loaded,
        # promote CLAUDE ahead of PRIME_LOCAL to avoid triggering an expensive
        # local model load that'll be unloaded seconds later when GCP arrives.
        if os.environ.get("JARVIS_INVINCIBLE_NODE_BOOTING") == "true":
            _local_client = None
            if _model_serving is not None:
                _local_client = _model_serving._clients.get(ModelProvider.PRIME_LOCAL)
            _local_loaded = _local_client and getattr(_local_client, '_loaded', False)

            if not _local_loaded and ModelProvider.CLAUDE in result:
                result = [p for p in result if p != ModelProvider.CLAUDE]
                _local_idx = next(
                    (i for i, p in enumerate(result) if p == ModelProvider.PRIME_LOCAL),
                    len(result)
                )
                result.insert(_local_idx, ModelProvider.CLAUDE)

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
        self._prime_api_source: Optional[str] = None  # v261.0: "gcp" | "local_jprime" | None

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
        # v261.0: Quick probe with dedicated client — don't block startup for 15s
        if PRIME_API_ENABLED:
            self.logger.info("  🔄 Checking J-Prime API availability...")
            _quick_timeout = float(os.getenv("JARVIS_PRIME_API_QUICK_TIMEOUT", "2.0"))
            probe_client = PrimeAPIClient(wait_timeout=_quick_timeout)
            if await probe_client.wait_for_ready():
                # J-Prime already available — register immediately
                self._clients[ModelProvider.PRIME_API] = probe_client
                self._prime_api_source = "local_jprime"
                self.logger.info(f"  ✓ J-Prime API ready ({len(probe_client._available_models)} models)")
                prime_available = True
            else:
                await probe_client._close_session()
                self.logger.info(
                    "  ⏳ J-Prime API not ready yet — will hot-swap when available"
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

        # v235.1: Start memory pressure monitor (Fix C4)
        self._memory_monitor_task: Optional[asyncio.Task] = None
        if ModelProvider.PRIME_LOCAL in self._clients:
            self._memory_monitor_task = asyncio.create_task(
                self._start_memory_monitor()
            )
            self.logger.info("[v235.1] Memory pressure monitor started")

    async def _start_memory_monitor(self) -> None:
        """v235.1: Background monitor that unloads local model under memory pressure (Fix C4)."""
        _critical_since: Optional[float] = None
        _unload_threshold_seconds = 30.0

        while self._running:
            try:
                await asyncio.sleep(15)  # Check every 15s

                # Check if a local model is loaded (via PrimeLocalClient)
                _local = self._clients.get(ModelProvider.PRIME_LOCAL)
                if not _local or not getattr(_local, '_model', None):
                    _critical_since = None
                    continue

                try:
                    from backend.core.memory_quantizer import (
                        get_memory_quantizer, MemoryTier,
                    )
                    _mq = await get_memory_quantizer()
                    _mem_metrics = _mq.get_current_metrics()
                    _mem_tier = _mem_metrics.tier
                except Exception:
                    continue

                if _mem_tier in (MemoryTier.CRITICAL, MemoryTier.EMERGENCY):
                    if _critical_since is None:
                        _critical_since = time.time()
                        self.logger.warning(
                            f"[v235.1] Memory pressure {_mem_tier.value} detected "
                            f"with local model loaded "
                            f"(available: {_mem_metrics.system_memory_available_gb:.1f}GB). "
                            f"Monitoring for {_unload_threshold_seconds:.0f}s..."
                        )
                    elif time.time() - _critical_since > _unload_threshold_seconds:
                        self.logger.warning(
                            f"[v235.1] Memory pressure {_mem_tier.value} sustained for "
                            f">{_unload_threshold_seconds:.0f}s — unloading local model "
                            f"to reclaim RAM "
                            f"(available: {_mem_metrics.system_memory_available_gb:.1f}GB)"
                        )
                        await self._unload_local_model()
                        _critical_since = None
                else:
                    if _critical_since is not None:
                        self.logger.info(
                            f"[v235.1] Memory pressure resolved ({_mem_tier.value}), "
                            f"model retained"
                        )
                    _critical_since = None

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.debug(f"[v235.1] Memory monitor error: {e}")
                await asyncio.sleep(30)

    async def _unload_local_model(self) -> None:
        """v235.1: Safely unload the local GGUF model to reclaim RAM."""
        _local = self._clients.get(ModelProvider.PRIME_LOCAL)
        if _local and getattr(_local, '_model', None) is not None:
            try:
                _model_path = getattr(_local._model, 'model_path', 'unknown')
                del _local._model
                _local._model = None
                _local._loaded = False
                import gc
                gc.collect()
                # Trip PRIME_LOCAL circuit breaker so routing skips to CLAUDE
                for _ in range(CIRCUIT_BREAKER_FAILURE_THRESHOLD):
                    self._circuit_breaker.record_failure(
                        ModelProvider.PRIME_LOCAL.value
                    )
                self.logger.info(
                    f"[v235.1] Local model unloaded ({_model_path}). "
                    f"PRIME_LOCAL circuit breaker tripped — "
                    f"inference falls through to CLAUDE tier."
                )
            except Exception as e:
                self.logger.warning(f"[v235.1] Model unload error: {e}")

    async def stop(self) -> None:
        """Stop the model serving layer.

        v234.1: Properly shuts down PrimeLocalClient's single-worker
        inference executor to release the thread and prevent resource leaks.
        Also unloads any loaded GGUF models to free GPU/system memory.
        """
        self._running = False

        # v235.1: Stop memory pressure monitor
        if hasattr(self, '_memory_monitor_task') and self._memory_monitor_task:
            self._memory_monitor_task.cancel()
            self._memory_monitor_task = None

        # v234.1: Clean up PrimeLocalClient resources
        local_client = self._clients.get(ModelProvider.PRIME_LOCAL)
        if local_client and isinstance(local_client, PrimeLocalClient):
            # Shut down the single-worker inference executor
            if hasattr(local_client, "_inference_executor"):
                local_client._inference_executor.shutdown(wait=False)
                self.logger.debug(
                    "[v234.1] PrimeLocalClient inference executor shut down"
                )
            # Release the loaded model from memory
            if local_client._model is not None:
                local_client._model = None
                local_client._loaded = False
                self.logger.debug(
                    "[v234.1] PrimeLocalClient model unloaded"
                )

        # Close aiohttp sessions for API-based clients
        for provider, client in self._clients.items():
            if hasattr(client, "_close_session"):
                try:
                    await client._close_session()
                except Exception:
                    pass

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
        """Generate a streaming response with mid-stream failover (v239.0).

        If a provider fails mid-stream, captures the error, records circuit
        breaker failure, and retries with the next tier using the original prompt.

        Known behavior: On failover after partial output, the consumer sees
        the beginning of the response twice (partial from tier 1 + full from
        tier 2). A [Stream interrupted] marker separates them so UIs can
        detect and clean up the display.
        """
        available = list(self._clients.keys())
        providers = self._router.get_preferred_providers(request, available)

        if not providers:
            yield "[Error: No suitable model providers available]"
            return

        last_error = None
        for provider in providers:
            if not self._circuit_breaker.can_execute(provider.value):
                continue

            client = self._clients.get(provider)
            if not client:
                continue

            try:
                chunks_yielded = 0
                async for chunk in client.generate_stream(request):
                    yield chunk
                    chunks_yielded += 1
                # Stream completed successfully
                self._circuit_breaker.record_success(provider.value)
                return
            except Exception as e:
                self.logger.warning(
                    f"[v239.0] Stream failed from {provider.value} after "
                    f"{chunks_yielded} chunks: {e}"
                )
                self._circuit_breaker.record_failure(provider.value)
                last_error = str(e)

                if chunks_yielded > 0:
                    yield "\n\n[Stream interrupted — retrying with backup...]\n\n"
                continue

        if last_error:
            yield f"[Error: All providers failed. Last: {last_error}]"

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

    url = url.rstrip("/")
    logger = logging.getLogger("UnifiedModelServing")

    if _model_serving is None:
        # v236.0: Even without the singleton, update the module-level URL
        # so any NEW PrimeAPIClient created after this point uses the GCP URL.
        # The supervisor will also call us again after _model_serving is created.
        PRIME_API_URL = url
        logger.info(
            f"[v236.0] GCP URL pre-registered ({PRIME_API_URL}) — "
            f"will be used when PrimeAPIClient initializes"
        )
        return False  # Still return False so caller knows hot-swap didn't happen

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
        _model_serving._prime_api_source = "gcp"  # v261.0: GCP wins priority
        # v239.0: Free RAM by unloading local model now that GCP is primary
        try:
            await _model_serving._unload_local_model()
            logger.info("[v239.0] Local model unloaded — GCP is now primary tier")
        except Exception as e:
            logger.warning(f"[v239.0] Local model unload failed (non-fatal): {e}")
        return True
    else:
        # No PrimeAPIClient yet — create one pointing at GCP
        new_client = PrimeAPIClient(base_url=url)
        if await new_client.wait_for_ready():
            _model_serving._clients[ModelProvider.PRIME_API] = new_client
            _model_serving._circuit_breaker.record_success(
                ModelProvider.PRIME_API.value
            )
            _model_serving._prime_api_source = "gcp"  # v261.0: GCP wins priority
            logger.info(f"[v234.0] GCP endpoint activated: {url}")
            # v239.0: Free RAM by unloading local model now that GCP is primary
            try:
                await _model_serving._unload_local_model()
                logger.info("[v239.0] Local model unloaded — GCP is now primary tier")
            except Exception as e:
                logger.warning(f"[v239.0] Local model unload failed (non-fatal): {e}")
            return True
        else:
            logger.warning(f"[v234.0] GCP endpoint not ready: {url}")
            await new_client._close_session()
            return False


# =============================================================================
# v261.0: J-Prime API Deferred Registration (called by unified_supervisor.py)
# =============================================================================

async def notify_jprime_api_ready(url: str) -> bool:
    """
    v261.0: Notify UnifiedModelServing that local J-Prime API is ready.

    Called by unified_supervisor when Early Prime prewarm or Trinity
    detects J-Prime is healthy. Hot-swaps or creates PrimeAPIClient.

    Priority: GCP > local J-Prime. If GCP is already active as the
    PRIME_API source, this function refuses to downgrade.

    Mirrors the notify_gcp_endpoint_ready() pattern.

    Args:
        url: Full URL of J-Prime (e.g., "http://localhost:8000")

    Returns:
        True if J-Prime API client was activated successfully
    """
    global _model_serving, PRIME_API_URL

    url = url.rstrip("/")
    logger = logging.getLogger("UnifiedModelServing")

    if not PRIME_API_ENABLED:
        logger.debug("[v261.0] PRIME_API disabled, ignoring J-Prime ready notification")
        return False

    if _model_serving is None:
        # Pre-register: update module-level URL for when singleton initializes
        PRIME_API_URL = url
        logger.info(f"[v261.0] J-Prime URL pre-registered ({url})")
        return False

    # v261.0 R2-#1: Priority check — GCP > local J-Prime
    if _model_serving._prime_api_source == "gcp":
        logger.info(
            f"[v261.0] J-Prime local ready at {url}, but GCP is active — "
            f"keeping GCP as primary (higher priority)"
        )
        return False

    # Check if already registered at this URL
    existing = _model_serving._clients.get(ModelProvider.PRIME_API)
    if (existing and isinstance(existing, PrimeAPIClient)
            and existing.base_url == url
            and _model_serving._prime_api_source == "local_jprime"):
        logger.debug("[v261.0] J-Prime API already registered at this URL")
        return True

    # Validate endpoint before registering
    test_client = PrimeAPIClient(
        base_url=url,
        wait_timeout=float(os.getenv("JARVIS_JPRIME_VALIDATION_TIMEOUT", "10.0")),
    )
    if not await test_client.wait_for_ready():
        logger.warning(f"[v261.0] J-Prime endpoint validation failed ({url})")
        await test_client._close_session()
        return False

    # Register the validated client
    if existing and isinstance(existing, PrimeAPIClient):
        # Hot-swap existing client
        old_url = existing.base_url
        existing.base_url = url
        # v261.0 R2-#2: Use _ready = False (matches GCP pattern) — forces re-validation
        # on next generate() call. Prevents racing with in-flight requests on stale session.
        existing._ready = False
        existing._available_models = test_client._available_models
        await existing._close_session()  # Close old connection pool
        await test_client._close_session()
        _model_serving._circuit_breaker.record_success(ModelProvider.PRIME_API.value)
        _model_serving._prime_api_source = "local_jprime"
        logger.info(f"[v261.0] J-Prime API hot-swapped: {old_url} → {url}")
    else:
        # New registration — use the test_client directly (already validated)
        test_client._ready = False  # R2-#2: Force re-validation on first use
        _model_serving._clients[ModelProvider.PRIME_API] = test_client
        _model_serving._circuit_breaker.record_success(ModelProvider.PRIME_API.value)
        _model_serving._prime_api_source = "local_jprime"
        logger.info(
            f"[v261.0] J-Prime API activated: {url} "
            f"({len(test_client._available_models)} models)"
        )

    # Update module-level URL for any future clients
    PRIME_API_URL = url
    return True


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
