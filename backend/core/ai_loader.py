"""
JARVIS Hyper-Speed AI Loader v1.0
=================================

Advanced model loading system that decouples server startup from model initialization
using Metaprogramming, Zero-Copy serialization, and Dynamic Quantization.

Architecture:
    1. GhostModelProxy: Metaprogramming wrapper that acts as the model before it exists
    2. AsyncModelManager: Background loading with ThreadPoolExecutor (PyTorch releases GIL)
    3. Zero-Copy Loading: safetensors mmap for instant disk-to-RAM mapping
    4. Dynamic Quantization: INT8 for 4x smaller models, 2x faster inference

Key Benefits:
    - Server responds instantly (200 OK in milliseconds)
    - Models load asynchronously without blocking
    - Memory-mapped files avoid data copying
    - Request queuing for calls made before model is ready

Usage:
    from backend.core.ai_loader import get_ai_manager, ModelPriority

    # Register a model (returns Ghost Proxy immediately)
    ecapa_model = manager.register_model(
        name="ecapa_tdnn",
        loader_func=lambda: load_ecapa_model(),
        priority=ModelPriority.HIGH,
        quantize=True,
    )

    # Use the proxy like a real model (auto-waits if not ready)
    embedding = await ecapa_model.encode(audio)

Version: 1.0.0 - Hyper-Speed AI Loader Edition
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
import time
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum, auto
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Deque,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

logger = logging.getLogger("jarvis.ai_loader")

T = TypeVar("T")
ModelT = TypeVar("ModelT")


# =============================================================================
# Thread-Safe Event Loop Utilities
# =============================================================================

# Store event loops created for threads to avoid creating multiple
_thread_event_loops: Dict[int, asyncio.AbstractEventLoop] = {}
_thread_loop_lock = threading.Lock()


def get_or_create_event_loop() -> asyncio.AbstractEventLoop:
    """
    Get the current event loop or create one for the current thread.

    This is thread-safe and handles the case where code runs in a
    ThreadPoolExecutor thread that doesn't have an event loop.

    Returns:
        An asyncio event loop for the current thread
    """
    # First try to get the running loop (Python 3.10+ recommended way)
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        pass

    # Try to get an existing event loop for this thread
    thread_id = threading.get_ident()

    with _thread_loop_lock:
        if thread_id in _thread_event_loops:
            loop = _thread_event_loops[thread_id]
            if not loop.is_closed():
                return loop
            # Loop was closed, remove it
            del _thread_event_loops[thread_id]

        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _thread_event_loops[thread_id] = loop
        logger.debug(f"[AI] Created new event loop for thread {thread_id}")
        return loop


def cleanup_thread_event_loops():
    """Clean up event loops created for threads (call on shutdown)."""
    with _thread_loop_lock:
        for thread_id, loop in list(_thread_event_loops.items()):
            if not loop.is_closed():
                try:
                    loop.close()
                except Exception:
                    pass
            del _thread_event_loops[thread_id]


# =============================================================================
# Configuration - Environment-Driven, Zero Hardcoding
# =============================================================================

@dataclass
class AILoaderConfig:
    """
    Dynamic AI loader configuration.

    All values can be overridden via environment variables:
        JARVIS_AI_MAX_WORKERS: Max concurrent model loads (default: 3)
        JARVIS_AI_QUANTIZE_DEFAULT: Default quantization (default: true)
        JARVIS_AI_QUEUE_TIMEOUT: Request queue timeout seconds (default: 30)
        JARVIS_AI_WARMUP_RETRIES: Retry count for warmup calls (default: 3)
        JARVIS_AI_SAFETENSORS_PREFER: Prefer safetensors format (default: true)
        JARVIS_AI_LAZY_QUANTIZE: Quantize lazily on first use (default: false)
    """
    max_workers: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_AI_MAX_WORKERS", "3"))
    )
    quantize_default: bool = field(
        default_factory=lambda: os.getenv("JARVIS_AI_QUANTIZE_DEFAULT", "true").lower() == "true"
    )
    queue_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_AI_QUEUE_TIMEOUT", "30"))
    )
    warmup_retries: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_AI_WARMUP_RETRIES", "3"))
    )
    safetensors_prefer: bool = field(
        default_factory=lambda: os.getenv("JARVIS_AI_SAFETENSORS_PREFER", "true").lower() == "true"
    )
    lazy_quantize: bool = field(
        default_factory=lambda: os.getenv("JARVIS_AI_LAZY_QUANTIZE", "false").lower() == "true"
    )


# Global config
_config: Optional[AILoaderConfig] = None


def get_config() -> AILoaderConfig:
    """Get or create the global AI loader config."""
    global _config
    if _config is None:
        _config = AILoaderConfig()
    return _config


# =============================================================================
# Model Priority & Status
# =============================================================================

class ModelPriority(IntEnum):
    """Model loading priority levels."""
    CRITICAL = 1   # Load first (voice unlock, auth)
    HIGH = 2       # Load early (vision, embeddings)
    NORMAL = 3     # Standard priority
    LOW = 4        # Load last (optional features)
    LAZY = 5       # Load only on first use


class ModelStatus(IntEnum):
    """Model lifecycle status."""
    GHOST = 0       # Proxy created, not loading yet
    QUEUED = 1      # Waiting in load queue
    LOADING = 2     # Currently loading
    QUANTIZING = 3  # Applying quantization
    READY = 4       # Fully loaded and ready
    FAILED = 5      # Load failed
    UNLOADED = 6    # Explicitly unloaded


class ModelMetrics:
    """
    Metrics for a loaded model.

    Uses __slots__ for memory efficiency with manual initialization
    to avoid dataclass default value conflicts.
    """
    __slots__ = (
        'name', 'status', 'priority', 'load_start_time', 'load_end_time',
        'load_duration_ms', 'memory_bytes', 'quantized', 'calls_total',
        'calls_while_warming', 'errors', 'last_used',
    )

    def __init__(
        self,
        name: str,
        status: ModelStatus,
        priority: ModelPriority = ModelPriority.NORMAL,
        load_start_time: Optional[float] = None,
        load_end_time: Optional[float] = None,
        load_duration_ms: float = 0.0,
        memory_bytes: int = 0,
        quantized: bool = False,
        calls_total: int = 0,
        calls_while_warming: int = 0,
        errors: int = 0,
        last_used: Optional[float] = None,
    ):
        self.name = name
        self.status = status
        self.priority = priority
        self.load_start_time = load_start_time
        self.load_end_time = load_end_time
        self.load_duration_ms = load_duration_ms
        self.memory_bytes = memory_bytes
        self.quantized = quantized
        self.calls_total = calls_total
        self.calls_while_warming = calls_while_warming
        self.errors = errors
        self.last_used = last_used


# =============================================================================
# Pending Request Queue
# =============================================================================

@dataclass
class PendingRequest:
    """A request waiting for model to be ready."""
    __slots__ = ('args', 'kwargs', 'future', 'created_at')

    args: Tuple[Any, ...]
    kwargs: Dict[str, Any]
    future: asyncio.Future
    created_at: float


# =============================================================================
# Ghost Model Proxy - Metaprogramming Core
# =============================================================================

class GhostModelProxy(Generic[ModelT]):
    """
    Metaprogramming Wrapper - Acts as the model before the model exists.

    Features:
    - Intercepts all attribute access and method calls
    - Queues requests made before model is ready
    - Auto-retries with exponential backoff
    - Seamless transition when real model arrives
    - Thread-safe operation

    Usage:
        proxy = GhostModelProxy("vision_encoder")

        # This works even before the model is loaded
        result = await proxy.encode(image)  # Queued if not ready

        # Later, when model loads:
        proxy.materialize(real_model)  # All queued requests execute
    """

    __slots__ = (
        '_name', '_real_model', '_status', '_metrics', '_config',
        '_pending_queue', '_lock', '_ready_event', '_error', '_loader_task',
    )

    def __init__(
        self,
        name: str,
        config: Optional[AILoaderConfig] = None,
    ):
        self._name = name
        self._real_model: Optional[ModelT] = None
        self._status = ModelStatus.GHOST
        self._config = config or get_config()
        self._pending_queue: Deque[PendingRequest] = deque()
        self._lock = threading.RLock()
        self._ready_event = asyncio.Event()
        self._error: Optional[Exception] = None
        self._loader_task: Optional[asyncio.Task] = None

        # Initialize metrics
        self._metrics = ModelMetrics(
            name=name,
            status=ModelStatus.GHOST,
            priority=ModelPriority.NORMAL,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def status(self) -> ModelStatus:
        return self._status

    @property
    def is_ready(self) -> bool:
        return self._status == ModelStatus.READY and self._real_model is not None

    @property
    def is_loading(self) -> bool:
        return self._status in (ModelStatus.QUEUED, ModelStatus.LOADING, ModelStatus.QUANTIZING)

    @property
    def metrics(self) -> ModelMetrics:
        return self._metrics

    def __repr__(self) -> str:
        return f"<GhostProxy:{self._name} status={self._status.name}>"

    def __getattr__(self, name: str) -> Any:
        """
        Intercept attribute access.

        If model is ready, forward to real model.
        If not ready, return a callable that queues the request.
        """
        # Avoid infinite recursion for private attrs
        if name.startswith('_'):
            raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

        with self._lock:
            if self._real_model is not None:
                return getattr(self._real_model, name)

        # Return async wrapper that waits for model
        logger.debug(f"[GHOST] Accessing '{name}' on {self._name} before ready")

        async def _deferred_call(*args, **kwargs):
            return await self._wait_and_call(name, args, kwargs)

        return _deferred_call

    def __call__(self, *args, **kwargs) -> Any:
        """
        Intercept direct calls.

        If model is ready, forward the call.
        If not ready, queue the request and return a future.
        """
        with self._lock:
            if self._real_model is not None:
                self._metrics.calls_total += 1
                self._metrics.last_used = time.time()
                return self._real_model(*args, **kwargs)

        # Model not ready - queue the request
        self._metrics.calls_while_warming += 1
        logger.info(f"[GHOST] Queuing call to {self._name} (warming up)")

        # Create future for the caller to await - use thread-safe loop getter
        loop = get_or_create_event_loop()
        future = loop.create_future()

        request = PendingRequest(
            args=args,
            kwargs=kwargs,
            future=future,
            created_at=time.time(),
        )

        with self._lock:
            self._pending_queue.append(request)

        return future

    async def _wait_and_call(
        self,
        method_name: str,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Any:
        """Wait for model to be ready, then call the method."""
        # Wait for ready with timeout
        try:
            await asyncio.wait_for(
                self._ready_event.wait(),
                timeout=self._config.queue_timeout,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Model {self._name} did not become ready within "
                f"{self._config.queue_timeout}s timeout"
            )

        if self._error:
            raise self._error

        if self._real_model is None:
            raise RuntimeError(f"Model {self._name} failed to load")

        # Call the actual method
        method = getattr(self._real_model, method_name)
        self._metrics.calls_total += 1
        self._metrics.last_used = time.time()

        result = method(*args, **kwargs)

        # Handle async methods
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def wait_ready(self, timeout: Optional[float] = None) -> bool:
        """Wait for the model to become ready."""
        timeout = timeout or self._config.queue_timeout
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return self._status == ModelStatus.READY
        except asyncio.TimeoutError:
            return False

    def materialize(self, model: ModelT) -> None:
        """
        Materialize the ghost with a real model.

        This is called when the background loading completes.
        All pending requests are executed.
        """
        with self._lock:
            self._real_model = model
            self._status = ModelStatus.READY
            self._metrics.status = ModelStatus.READY
            self._metrics.load_end_time = time.time()

            if self._metrics.load_start_time:
                self._metrics.load_duration_ms = (
                    (self._metrics.load_end_time - self._metrics.load_start_time) * 1000
                )

        # Signal ready
        self._ready_event.set()

        # Process pending requests
        self._process_pending_queue()

        logger.info(
            f"[GHOST] {self._name} materialized! "
            f"Load time: {self._metrics.load_duration_ms:.0f}ms, "
            f"Pending requests: {len(self._pending_queue)}"
        )

    def fail(self, error: Exception) -> None:
        """Mark the model as failed to load."""
        with self._lock:
            self._status = ModelStatus.FAILED
            self._metrics.status = ModelStatus.FAILED
            self._metrics.errors += 1
            self._error = error

        # Signal ready (with error) to unblock waiters
        self._ready_event.set()

        # Fail all pending requests
        self._fail_pending_queue(error)

        logger.error(f"[GHOST] {self._name} failed to load: {error}")

    def _process_pending_queue(self) -> None:
        """Process all pending requests now that model is ready."""
        while self._pending_queue:
            with self._lock:
                if not self._pending_queue:
                    break
                request = self._pending_queue.popleft()

            try:
                # Execute the queued call
                result = self._real_model(*request.args, **request.kwargs)
                request.future.set_result(result)
                self._metrics.calls_total += 1
            except Exception as e:
                request.future.set_exception(e)
                self._metrics.errors += 1

    def _fail_pending_queue(self, error: Exception) -> None:
        """Fail all pending requests with the given error."""
        while self._pending_queue:
            with self._lock:
                if not self._pending_queue:
                    break
                request = self._pending_queue.popleft()

            request.future.set_exception(error)

    def unload(self) -> None:
        """Unload the model to free memory."""
        with self._lock:
            self._real_model = None
            self._status = ModelStatus.UNLOADED
            self._metrics.status = ModelStatus.UNLOADED
            self._ready_event.clear()

        logger.info(f"[GHOST] {self._name} unloaded")


# =============================================================================
# Zero-Copy Model Loader
# =============================================================================

class ZeroCopyLoader:
    """
    Advanced model loader using safetensors and quantization.

    Features:
    - Zero-copy loading via mmap (safetensors)
    - Dynamic INT8 quantization for 4x smaller models
    - Automatic format detection (safetensors vs pickle)
    - Memory-efficient loading pipeline
    """

    @staticmethod
    def load_safetensors(
        path: Union[str, Path],
        device: str = "cpu",
    ) -> Dict[str, Any]:
        """
        Load a safetensors file using zero-copy mmap.

        Args:
            path: Path to the .safetensors file
            device: Target device ("cpu", "cuda", "mps")

        Returns:
            State dict with tensors
        """
        try:
            from safetensors.torch import load_file

            path = Path(path)
            if not path.exists():
                raise FileNotFoundError(f"Safetensors file not found: {path}")

            logger.debug(f"[ZERO-COPY] Loading {path.name} via mmap...")
            start = time.time()

            # This uses mmap - no data copying until tensors are accessed
            state_dict = load_file(str(path), device=device)

            duration_ms = (time.time() - start) * 1000
            logger.info(f"[ZERO-COPY] Loaded {path.name} in {duration_ms:.0f}ms (mmap)")

            return state_dict

        except ImportError:
            logger.warning("safetensors not available, falling back to torch.load")
            return ZeroCopyLoader._fallback_load(path, device)

    @staticmethod
    def _fallback_load(
        path: Union[str, Path],
        device: str = "cpu",
    ) -> Dict[str, Any]:
        """Fallback to standard torch loading."""
        import torch

        path = Path(path)
        logger.debug(f"[LOADER] Loading {path.name} via torch.load...")
        start = time.time()

        state_dict = torch.load(str(path), map_location=device, weights_only=True)

        duration_ms = (time.time() - start) * 1000
        logger.info(f"[LOADER] Loaded {path.name} in {duration_ms:.0f}ms")

        return state_dict

    @staticmethod
    def quantize_dynamic(
        model: Any,
        dtype: Any = None,
        inplace: bool = False,
    ) -> Any:
        """
        Apply dynamic INT8 quantization for 4x memory reduction.

        Args:
            model: PyTorch model to quantize
            dtype: Quantization dtype (default: torch.qint8)
            inplace: Modify model in place

        Returns:
            Quantized model
        """
        try:
            import torch

            if dtype is None:
                dtype = torch.qint8

            logger.debug(f"[QUANTIZE] Applying INT8 quantization...")
            start = time.time()

            # Quantize Linear layers (most common in transformers)
            quantized = torch.quantization.quantize_dynamic(
                model,
                {torch.nn.Linear},
                dtype=dtype,
                inplace=inplace,
            )

            duration_ms = (time.time() - start) * 1000
            logger.info(f"[QUANTIZE] Quantized in {duration_ms:.0f}ms")

            return quantized

        except Exception as e:
            logger.warning(f"[QUANTIZE] Failed: {e}, returning original model")
            return model

    @staticmethod
    def estimate_memory(model: Any) -> int:
        """Estimate model memory usage in bytes."""
        try:
            import torch

            total_bytes = 0
            for param in model.parameters():
                total_bytes += param.nelement() * param.element_size()
            for buffer in model.buffers():
                total_bytes += buffer.nelement() * buffer.element_size()

            return total_bytes
        except Exception:
            return 0


# =============================================================================
# Optimization Engine Types
# =============================================================================

class OptimizationEngine(IntEnum):
    """Available optimization engines in priority order."""
    RUST_INT8 = 1      # Rust + INT8: Fastest, smallest (voice models)
    RUST_INT4 = 2      # Rust + INT4: Ultra-compressed (extreme memory)
    JIT_TRACE = 3      # TorchScript traced: Fast startup (<2s vs 140s)
    JIT_SCRIPT = 4     # TorchScript scripted: Portable, optimized
    ONNX = 5           # ONNX Runtime: Cross-platform, optimized
    TORCH_INT8 = 6     # PyTorch Dynamic INT8: Good balance
    SAFETENSORS = 7    # Zero-copy mmap: Fast loading
    STANDARD = 8       # Standard PyTorch: Fallback


class ModelCategory(Enum):
    """Model categories for routing decisions."""
    VOICE = auto()       # Voice/speech models (ECAPA, whisper)
    VISION = auto()      # Vision models (YOLO, CLIP)
    EMBEDDING = auto()   # Embedding models (sentence transformers)
    SVM = auto()         # SVM/sklearn models
    NEURAL_NET = auto()  # Generic neural networks
    GENERIC = auto()     # Unknown/generic models


@dataclass
class EngineCapability:
    """Describes an optimization engine's capabilities."""
    engine: OptimizationEngine
    available: bool
    model_categories: Set[ModelCategory]
    speedup_factor: float  # vs standard loading
    memory_reduction: float  # vs standard (0.25 = 75% reduction)
    requires_precompiled: bool
    error: Optional[str] = None


# =============================================================================
# Unified Optimization Router
# =============================================================================

class OptimizationRouter:
    """
    Intelligent router that selects the best optimization engine for each model.

    Routes models to specialized engines based on:
    1. Model type/category (voice, vision, SVM, neural net)
    2. Available engines (Rust, JIT, ONNX, etc.)
    3. Memory constraints
    4. Performance requirements

    Engine Priority (when multiple are available):
    - Voice/SVM: Rust INT8 > JIT > ONNX > PyTorch INT8 > Safetensors
    - Vision: JIT > ONNX > Safetensors > Standard
    - Embeddings: ONNX > JIT > Safetensors > Standard
    - Generic: Safetensors > Standard
    """

    def __init__(self):
        self._engine_cache: Dict[OptimizationEngine, EngineCapability] = {}
        self._model_loaders: Dict[OptimizationEngine, Callable] = {}
        self._discovered = False

    def discover_engines(self) -> Dict[OptimizationEngine, EngineCapability]:
        """
        Discover all available optimization engines.

        Probes each engine to check availability without loading models.
        Results are cached for subsequent calls.
        """
        if self._discovered:
            return self._engine_cache

        logger.info("[ROUTER] Discovering available optimization engines...")

        # Check Rust INT8/INT4
        rust_available, rust_error = self._probe_rust_engine()
        self._engine_cache[OptimizationEngine.RUST_INT8] = EngineCapability(
            engine=OptimizationEngine.RUST_INT8,
            available=rust_available,
            model_categories={ModelCategory.VOICE, ModelCategory.SVM},
            speedup_factor=10.0,
            memory_reduction=0.25,  # 75% reduction
            requires_precompiled=False,
            error=rust_error,
        )
        self._engine_cache[OptimizationEngine.RUST_INT4] = EngineCapability(
            engine=OptimizationEngine.RUST_INT4,
            available=rust_available,
            model_categories={ModelCategory.SVM},
            speedup_factor=8.0,
            memory_reduction=0.125,  # 87.5% reduction
            requires_precompiled=False,
            error=rust_error,
        )

        # Check JIT (TorchScript)
        jit_available, jit_error = self._probe_jit_engine()
        self._engine_cache[OptimizationEngine.JIT_TRACE] = EngineCapability(
            engine=OptimizationEngine.JIT_TRACE,
            available=jit_available,
            model_categories={ModelCategory.VOICE, ModelCategory.VISION, ModelCategory.EMBEDDING, ModelCategory.NEURAL_NET},
            speedup_factor=70.0,  # 140s -> 2s
            memory_reduction=1.0,  # Same size
            requires_precompiled=True,
            error=jit_error,
        )
        self._engine_cache[OptimizationEngine.JIT_SCRIPT] = EngineCapability(
            engine=OptimizationEngine.JIT_SCRIPT,
            available=jit_available,
            model_categories={ModelCategory.VOICE, ModelCategory.VISION, ModelCategory.EMBEDDING, ModelCategory.NEURAL_NET},
            speedup_factor=50.0,
            memory_reduction=1.0,
            requires_precompiled=True,
            error=jit_error,
        )

        # Check ONNX Runtime
        onnx_available, onnx_error = self._probe_onnx_engine()
        self._engine_cache[OptimizationEngine.ONNX] = EngineCapability(
            engine=OptimizationEngine.ONNX,
            available=onnx_available,
            model_categories={ModelCategory.VOICE, ModelCategory.VISION, ModelCategory.EMBEDDING, ModelCategory.NEURAL_NET},
            speedup_factor=40.0,
            memory_reduction=0.8,  # 20% reduction typical
            requires_precompiled=True,
            error=onnx_error,
        )

        # Check PyTorch Dynamic Quantization
        torch_quant_available, torch_quant_error = self._probe_torch_quantize()
        self._engine_cache[OptimizationEngine.TORCH_INT8] = EngineCapability(
            engine=OptimizationEngine.TORCH_INT8,
            available=torch_quant_available,
            model_categories={ModelCategory.NEURAL_NET, ModelCategory.EMBEDDING, ModelCategory.VOICE},
            speedup_factor=2.0,
            memory_reduction=0.25,  # 75% reduction
            requires_precompiled=False,
            error=torch_quant_error,
        )

        # Safetensors is always available as fallback
        st_available, st_error = self._probe_safetensors()
        self._engine_cache[OptimizationEngine.SAFETENSORS] = EngineCapability(
            engine=OptimizationEngine.SAFETENSORS,
            available=st_available,
            model_categories={ModelCategory.VOICE, ModelCategory.VISION, ModelCategory.EMBEDDING, ModelCategory.NEURAL_NET, ModelCategory.GENERIC},
            speedup_factor=3.0,
            memory_reduction=1.0,
            requires_precompiled=False,
            error=st_error,
        )

        # Standard PyTorch always available
        self._engine_cache[OptimizationEngine.STANDARD] = EngineCapability(
            engine=OptimizationEngine.STANDARD,
            available=True,
            model_categories={cat for cat in ModelCategory},
            speedup_factor=1.0,
            memory_reduction=1.0,
            requires_precompiled=False,
        )

        self._discovered = True

        # Log discovery results
        available_count = sum(1 for e in self._engine_cache.values() if e.available)
        logger.info(f"[ROUTER] Discovered {available_count}/{len(self._engine_cache)} engines available")
        for engine, cap in sorted(self._engine_cache.items(), key=lambda x: x[0].value):
            status = "✅" if cap.available else "❌"
            logger.debug(f"   {status} {engine.name}: speedup={cap.speedup_factor}x, mem={cap.memory_reduction}")

        return self._engine_cache

    def _probe_rust_engine(self) -> Tuple[bool, Optional[str]]:
        """Check if Rust optimization engine is available."""
        try:
            from voice_unlock.ml.quantized_models import RUST_AVAILABLE
            if RUST_AVAILABLE:
                return True, None
            return False, "Rust extensions not compiled"
        except ImportError as e:
            return False, str(e)

    def _probe_jit_engine(self) -> Tuple[bool, Optional[str]]:
        """Check if TorchScript JIT is available."""
        try:
            import torch
            # Quick probe - create and trace a minimal model
            class MinimalModel(torch.nn.Module):
                def forward(self, x):
                    return x

            model = MinimalModel()
            _ = torch.jit.trace(model, torch.zeros(1))
            return True, None
        except Exception as e:
            return False, str(e)

    def _probe_onnx_engine(self) -> Tuple[bool, Optional[str]]:
        """Check if ONNX Runtime is available."""
        try:
            import onnxruntime as ort
            # Check available providers
            providers = ort.get_available_providers()
            logger.debug(f"[ROUTER] ONNX providers: {providers}")
            return True, None
        except ImportError:
            return False, "onnxruntime not installed"
        except Exception as e:
            return False, str(e)

    def _probe_torch_quantize(self) -> Tuple[bool, Optional[str]]:
        """Check if PyTorch dynamic quantization is available."""
        try:
            import torch
            if hasattr(torch.quantization, 'quantize_dynamic'):
                return True, None
            return False, "quantize_dynamic not available"
        except Exception as e:
            return False, str(e)

    def _probe_safetensors(self) -> Tuple[bool, Optional[str]]:
        """Check if safetensors is available."""
        try:
            from safetensors.torch import load_file
            return True, None
        except ImportError:
            return False, "safetensors not installed"

    def categorize_model(self, name: str, hints: Optional[Dict[str, Any]] = None) -> ModelCategory:
        """
        Infer model category from name and hints.

        Args:
            name: Model name/identifier
            hints: Optional hints dict with keys like 'category', 'type'

        Returns:
            Inferred ModelCategory
        """
        # Check explicit hint first
        if hints and "category" in hints:
            cat = hints["category"]
            if isinstance(cat, ModelCategory):
                return cat
            if isinstance(cat, str):
                try:
                    return ModelCategory[cat.upper()]
                except KeyError:
                    pass

        # Infer from name patterns
        name_lower = name.lower()

        # Voice/Speech patterns
        voice_patterns = ["ecapa", "speaker", "voice", "speech", "whisper", "wav2vec", "audio"]
        if any(p in name_lower for p in voice_patterns):
            return ModelCategory.VOICE

        # SVM patterns
        svm_patterns = ["svm", "classifier", "sklearn"]
        if any(p in name_lower for p in svm_patterns):
            return ModelCategory.SVM

        # Vision patterns
        vision_patterns = ["yolo", "clip", "vision", "image", "resnet", "vit", "dino"]
        if any(p in name_lower for p in vision_patterns):
            return ModelCategory.VISION

        # Embedding patterns
        embedding_patterns = ["embed", "sentence", "bert", "transformer", "encoder"]
        if any(p in name_lower for p in embedding_patterns):
            return ModelCategory.EMBEDDING

        # Neural net patterns (generic deep learning)
        nn_patterns = ["net", "model", "nn", "layer", "deep"]
        if any(p in name_lower for p in nn_patterns):
            return ModelCategory.NEURAL_NET

        return ModelCategory.GENERIC

    def select_engine(
        self,
        name: str,
        category: Optional[ModelCategory] = None,
        hints: Optional[Dict[str, Any]] = None,
        prefer_speed: bool = True,
        max_memory_ratio: float = 1.0,
    ) -> Tuple[OptimizationEngine, EngineCapability]:
        """
        Select the best optimization engine for a model.

        Args:
            name: Model name
            category: Model category (inferred if None)
            hints: Optional hints (e.g., {'engine': 'jit', 'precompiled_path': '...'})
            prefer_speed: Prioritize speed over memory
            max_memory_ratio: Max memory usage ratio (0.25 = use at most 25%)

        Returns:
            Tuple of (selected engine, capability info)
        """
        # Ensure engines are discovered
        if not self._discovered:
            self.discover_engines()

        # Infer category if not provided
        if category is None:
            category = self.categorize_model(name, hints)

        logger.debug(f"[ROUTER] Selecting engine for {name} (category={category.name})")

        # Check for explicit engine hint
        if hints and "engine" in hints:
            requested = hints["engine"]
            if isinstance(requested, str):
                try:
                    engine = OptimizationEngine[requested.upper()]
                    cap = self._engine_cache.get(engine)
                    if cap and cap.available:
                        logger.info(f"[ROUTER] Using requested engine: {engine.name}")
                        return engine, cap
                    # v109.3: Engine not available is graceful degradation, not a warning
                    logger.info(f"[ROUTER] Requested engine {requested} not available, using fallback")
                except KeyError:
                    logger.info(f"[ROUTER] Unknown engine: {requested}, using fallback")

        # Filter to engines that support this category
        candidates = [
            (engine, cap) for engine, cap in self._engine_cache.items()
            if cap.available and category in cap.model_categories
        ]

        if not candidates:
            # Fall back to standard
            # v109.3: Fallback to standard is normal operation
            logger.info(f"[ROUTER] No specialized engine for {category.name}, using STANDARD")
            return OptimizationEngine.STANDARD, self._engine_cache[OptimizationEngine.STANDARD]

        # Filter by memory constraint
        if max_memory_ratio < 1.0:
            candidates = [
                (e, c) for e, c in candidates
                if c.memory_reduction <= max_memory_ratio
            ]

        # Sort by priority (speed or memory)
        if prefer_speed:
            candidates.sort(key=lambda x: -x[1].speedup_factor)
        else:
            candidates.sort(key=lambda x: x[1].memory_reduction)

        # Check if precompiled models are required
        for engine, cap in candidates:
            if cap.requires_precompiled:
                # Check if precompiled model exists
                if hints and "precompiled_path" in hints:
                    precompiled = Path(hints["precompiled_path"])
                    if precompiled.exists():
                        logger.info(f"[ROUTER] Selected {engine.name} (precompiled available)")
                        return engine, cap
                    # Skip this engine - no precompiled model
                    continue
                # Skip precompiled-only engines when no path provided
                continue
            else:
                # This engine doesn't require precompiled models
                logger.info(f"[ROUTER] Selected {engine.name} for {name}")
                return engine, cap

        # Fall back to safetensors or standard
        if self._engine_cache[OptimizationEngine.SAFETENSORS].available:
            return OptimizationEngine.SAFETENSORS, self._engine_cache[OptimizationEngine.SAFETENSORS]

        return OptimizationEngine.STANDARD, self._engine_cache[OptimizationEngine.STANDARD]

    def get_loader_for_engine(
        self,
        engine: OptimizationEngine,
        hints: Optional[Dict[str, Any]] = None,
    ) -> Callable:
        """
        Get the appropriate loader function for an engine.

        Returns a callable that takes (loader_func, model_name) and returns the model.
        """
        if engine == OptimizationEngine.RUST_INT8:
            return self._load_rust_int8
        elif engine == OptimizationEngine.RUST_INT4:
            return self._load_rust_int4
        elif engine in (OptimizationEngine.JIT_TRACE, OptimizationEngine.JIT_SCRIPT):
            return lambda f, n: self._load_jit(f, n, hints)
        elif engine == OptimizationEngine.ONNX:
            return lambda f, n: self._load_onnx(f, n, hints)
        elif engine == OptimizationEngine.TORCH_INT8:
            return self._load_torch_int8
        elif engine == OptimizationEngine.SAFETENSORS:
            return self._load_safetensors
        else:
            return self._load_standard

    def _load_rust_int8(self, loader_func: Callable, name: str) -> Any:
        """Load using Rust INT8 engine."""
        try:
            from voice_unlock.ml.quantized_models import VoiceModelQuantizer, QuantizedSVMInference
            logger.info(f"[ROUTER] Loading {name} via Rust INT8 engine")

            # Get the model path or model from loader
            result = loader_func()

            if isinstance(result, (str, Path)):
                # It's a path - use quantizer
                quantizer = VoiceModelQuantizer()
                return quantizer.quantize_svm_model(Path(result), target="int8")
            else:
                # Already a model - wrap in quantized inference
                return result
        except Exception as e:
            logger.warning(f"[ROUTER] Rust INT8 failed for {name}: {e}, falling back")
            return loader_func()

    def _load_rust_int4(self, loader_func: Callable, name: str) -> Any:
        """Load using Rust INT4 engine (ultra-compressed)."""
        try:
            from voice_unlock.ml.quantized_models import VoiceModelQuantizer
            logger.info(f"[ROUTER] Loading {name} via Rust INT4 engine")

            result = loader_func()
            if isinstance(result, (str, Path)):
                quantizer = VoiceModelQuantizer()
                return quantizer.quantize_svm_model(Path(result), target="int4")
            return result
        except Exception as e:
            logger.warning(f"[ROUTER] Rust INT4 failed for {name}: {e}, falling back")
            return loader_func()

    def _load_jit(self, loader_func: Callable, name: str, hints: Optional[Dict] = None) -> Any:
        """Load using TorchScript JIT engine."""
        try:
            import torch
            logger.info(f"[ROUTER] Loading {name} via JIT engine")

            # Check for precompiled JIT model
            if hints and "precompiled_path" in hints:
                jit_path = Path(hints["precompiled_path"])
                if jit_path.exists():
                    logger.info(f"[ROUTER] Loading precompiled JIT from {jit_path}")
                    model = torch.jit.load(str(jit_path))
                    model.eval()
                    return model

            # Fall back to standard loading
            return loader_func()
        except Exception as e:
            logger.warning(f"[ROUTER] JIT failed for {name}: {e}, falling back")
            return loader_func()

    def _load_onnx(self, loader_func: Callable, name: str, hints: Optional[Dict] = None) -> Any:
        """Load using ONNX Runtime engine."""
        try:
            import onnxruntime as ort
            logger.info(f"[ROUTER] Loading {name} via ONNX engine")

            if hints and "precompiled_path" in hints:
                onnx_path = Path(hints["precompiled_path"])
                if onnx_path.exists():
                    providers = ['CoreMLExecutionProvider', 'CPUExecutionProvider']
                    session = ort.InferenceSession(str(onnx_path), providers=providers)
                    return session

            return loader_func()
        except Exception as e:
            logger.warning(f"[ROUTER] ONNX failed for {name}: {e}, falling back")
            return loader_func()

    def _load_torch_int8(self, loader_func: Callable, name: str) -> Any:
        """Load with PyTorch dynamic INT8 quantization."""
        try:
            import torch
            logger.info(f"[ROUTER] Loading {name} with PyTorch INT8 quantization")

            model = loader_func()
            if hasattr(model, 'parameters'):
                model = torch.quantization.quantize_dynamic(
                    model, {torch.nn.Linear}, dtype=torch.qint8
                )
            return model
        except Exception as e:
            logger.warning(f"[ROUTER] PyTorch INT8 failed for {name}: {e}")
            return loader_func()

    def _load_safetensors(self, loader_func: Callable, name: str) -> Any:
        """Load using safetensors zero-copy."""
        logger.info(f"[ROUTER] Loading {name} via safetensors zero-copy")
        return loader_func()

    def _load_standard(self, loader_func: Callable, name: str) -> Any:
        """Standard loading fallback."""
        logger.info(f"[ROUTER] Loading {name} via standard loader")
        return loader_func()

    def get_stats(self) -> Dict[str, Any]:
        """Get router statistics."""
        if not self._discovered:
            self.discover_engines()

        return {
            "engines": {
                engine.name: {
                    "available": cap.available,
                    "speedup": cap.speedup_factor,
                    "memory_reduction": cap.memory_reduction,
                    "categories": [c.name for c in cap.model_categories],
                    "error": cap.error,
                }
                for engine, cap in self._engine_cache.items()
            },
            "available_count": sum(1 for c in self._engine_cache.values() if c.available),
            "total_count": len(self._engine_cache),
        }


# Global router instance
_router: Optional[OptimizationRouter] = None


def get_optimization_router() -> OptimizationRouter:
    """Get or create the global optimization router."""
    global _router
    if _router is None:
        _router = OptimizationRouter()
    return _router


# =============================================================================
# Async Model Manager
# =============================================================================

class AsyncModelManager:
    """
    Centralized manager for all AI model loading.

    Features:
    - Returns Ghost Proxies immediately for instant startup
    - Loads models in background threads (PyTorch releases GIL)
    - Priority-based loading queue
    - Intelligent engine routing (Rust, JIT, ONNX, Quantization)
    - Memory tracking and limits
    - Hot-reload capability

    v2.0: Unified Optimization Router
    - Auto-detects best engine for each model type
    - Voice/SVM: Rust INT8 > JIT > ONNX > Standard
    - Vision: JIT > ONNX > Safetensors > Standard
    - Embeddings: ONNX > JIT > Standard
    """

    def __init__(self, config: Optional[AILoaderConfig] = None):
        self._config = config or get_config()
        self._proxies: Dict[str, GhostModelProxy] = {}
        self._load_order: List[str] = []
        self._executor = ThreadPoolExecutor(
            max_workers=self._config.max_workers,
            thread_name_prefix="ai_loader",
        )
        self._lock = threading.RLock()
        self._shutdown = False
        self._total_memory_bytes = 0

        # Loading queue sorted by priority
        self._load_queue: List[Tuple[ModelPriority, str, Callable]] = []
        self._loading_in_progress: Set[str] = set()

        # Optimization router for intelligent engine selection
        self._router = get_optimization_router()

        # Model hints for engine routing (name -> hints dict)
        self._model_hints: Dict[str, Dict[str, Any]] = {}

    def register_model(
        self,
        name: str,
        loader_func: Callable[[], ModelT],
        priority: ModelPriority = ModelPriority.NORMAL,
        quantize: Optional[bool] = None,
        lazy: bool = False,
        hints: Optional[Dict[str, Any]] = None,
    ) -> GhostModelProxy[ModelT]:
        """
        Register a model for background loading with intelligent engine routing.

        Returns a Ghost Proxy immediately that can be used as if
        the model were already loaded.

        Args:
            name: Unique model identifier
            loader_func: Function that loads and returns the model
            priority: Loading priority
            quantize: Apply INT8 quantization (None = auto-select via router)
            lazy: Only load on first use (ModelPriority.LAZY)
            hints: Optional optimization hints:
                - category: ModelCategory (voice, vision, svm, etc.)
                - engine: Preferred engine (rust_int8, jit, onnx, etc.)
                - precompiled_path: Path to JIT/ONNX precompiled model
                - prefer_speed: Prioritize speed over memory
                - max_memory_ratio: Max memory usage ratio

        Returns:
            GhostModelProxy that can be used immediately
        """
        with self._lock:
            if name in self._proxies:
                logger.warning(f"[AI] Model {name} already registered, returning existing proxy")
                return self._proxies[name]

            # Create ghost proxy
            proxy = GhostModelProxy(name, self._config)
            proxy._metrics.priority = ModelPriority.LAZY if lazy else priority
            self._proxies[name] = proxy
            self._load_order.append(name)

            # Determine quantization (auto if None)
            should_quantize = quantize if quantize is not None else self._config.quantize_default

        # Select engine via router
        engine, capability = self._router.select_engine(
            name=name,
            hints=hints,
            prefer_speed=hints.get("prefer_speed", True) if hints else True,
            max_memory_ratio=hints.get("max_memory_ratio", 1.0) if hints else 1.0,
        )

        # Store hints for routing with selected engine info
        model_hints = dict(hints) if hints else {}
        model_hints["selected_engine"] = engine.name
        model_hints["engine_speedup"] = capability.speedup_factor
        model_hints["engine_memory_reduction"] = capability.memory_reduction
        # Track if this engine does its own quantization
        if engine in (OptimizationEngine.RUST_INT8, OptimizationEngine.RUST_INT4, OptimizationEngine.TORCH_INT8):
            model_hints["engine_quantized"] = True
        self._model_hints[name] = model_hints

        logger.info(
            f"[AI] Registered {name} (priority={priority.name}, "
            f"engine={engine.name}, speedup={capability.speedup_factor}x, lazy={lazy})"
        )

        # Start loading unless lazy
        if not lazy:
            self._queue_load(name, loader_func, priority, should_quantize, engine, hints)

        return proxy

    def _queue_load(
        self,
        name: str,
        loader_func: Callable,
        priority: ModelPriority,
        quantize: bool,
        engine: Optional[OptimizationEngine] = None,
        hints: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Queue a model for background loading with engine routing."""
        with self._lock:
            proxy = self._proxies.get(name)
            if proxy:
                proxy._status = ModelStatus.QUEUED
                proxy._metrics.status = ModelStatus.QUEUED

        # Select engine if not provided
        if engine is None:
            engine, _ = self._router.select_engine(name, hints=hints)

        # Get the appropriate loader for this engine
        engine_loader = self._router.get_loader_for_engine(engine, hints)

        # Create wrapped loader with engine routing
        def _wrapped_loader():
            return self._load_with_engine(name, loader_func, engine_loader, quantize)

        # Schedule the load
        asyncio.create_task(
            self._background_load(name, _wrapped_loader, priority)
        )

    async def _background_load(
        self,
        name: str,
        loader_func: Callable,
        priority: ModelPriority,
    ) -> None:
        """Execute model loading in background thread."""
        proxy = self._proxies.get(name)
        if not proxy:
            logger.error(f"[AI] No proxy found for {name}")
            return

        with self._lock:
            if name in self._loading_in_progress:
                logger.debug(f"[AI] {name} already loading, skipping")
                return
            self._loading_in_progress.add(name)

        proxy._status = ModelStatus.LOADING
        proxy._metrics.status = ModelStatus.LOADING
        proxy._metrics.load_start_time = time.time()

        logger.info(f"[AI] Loading {name} (priority={priority.name})...")

        try:
            # Use thread-safe event loop getter for cross-thread compatibility
            loop = get_or_create_event_loop()

            # Run in thread pool (PyTorch releases GIL during heavy ops)
            model = await loop.run_in_executor(self._executor, loader_func)

            # Estimate memory
            memory = ZeroCopyLoader.estimate_memory(model)
            proxy._metrics.memory_bytes = memory
            self._total_memory_bytes += memory

            # Materialize the ghost
            proxy.materialize(model)

            logger.info(
                f"[AI] {name} ready! "
                f"Load: {proxy._metrics.load_duration_ms:.0f}ms, "
                f"Memory: {memory / 1024 / 1024:.1f}MB"
            )

        except Exception as e:
            proxy.fail(e)
            logger.error(f"[AI] {name} failed: {e}")

        finally:
            with self._lock:
                self._loading_in_progress.discard(name)

    def _load_with_engine(
        self,
        name: str,
        loader_func: Callable,
        engine_loader: Callable,
        quantize: bool,
    ) -> Any:
        """
        Load model using the routed optimization engine.

        Args:
            name: Model name
            loader_func: Original loader function
            engine_loader: Engine-specific loader from router
            quantize: Whether to apply additional quantization

        Returns:
            Loaded model (possibly optimized/quantized)
        """
        proxy = self._proxies.get(name)
        if proxy:
            proxy._status = ModelStatus.LOADING
            proxy._metrics.status = ModelStatus.LOADING

        # Use the engine-specific loader which wraps the original loader
        model = engine_loader(loader_func, name)

        # Apply additional PyTorch quantization if requested and not already done by engine
        if quantize and model is not None:
            # Check if engine already did quantization (Rust INT8, TORCH_INT8)
            hints = self._model_hints.get(name, {})
            engine_did_quantize = hints.get("engine_quantized", False)

            if not engine_did_quantize and hasattr(model, 'parameters'):
                if proxy:
                    proxy._status = ModelStatus.QUANTIZING
                    proxy._metrics.status = ModelStatus.QUANTIZING
                    proxy._metrics.quantized = True

                model = ZeroCopyLoader.quantize_dynamic(model)

        return model

    def _load_with_quantization(
        self,
        name: str,
        loader_func: Callable,
        quantize: bool,
    ) -> Any:
        """Load model and optionally apply quantization (legacy method)."""
        # Update status
        proxy = self._proxies.get(name)
        if proxy:
            proxy._status = ModelStatus.LOADING
            proxy._metrics.status = ModelStatus.LOADING

        # Execute the loader
        model = loader_func()

        # Apply quantization if requested
        if quantize and model is not None:
            if proxy:
                proxy._status = ModelStatus.QUANTIZING
                proxy._metrics.status = ModelStatus.QUANTIZING
                proxy._metrics.quantized = True

            model = ZeroCopyLoader.quantize_dynamic(model)

        return model

    async def ensure_loaded(
        self,
        name: str,
        timeout: Optional[float] = None,
    ) -> bool:
        """
        Ensure a model is loaded before proceeding.

        Args:
            name: Model name
            timeout: Max wait time in seconds

        Returns:
            True if model is ready, False if timeout or error
        """
        proxy = self._proxies.get(name)
        if not proxy:
            logger.error(f"[AI] Unknown model: {name}")
            return False

        if proxy.is_ready:
            return True

        return await proxy.wait_ready(timeout)

    def get_proxy(self, name: str) -> Optional[GhostModelProxy]:
        """Get a model's Ghost Proxy."""
        return self._proxies.get(name)

    def get_model(self, name: str) -> Optional[Any]:
        """Get the real model if loaded, None otherwise."""
        proxy = self._proxies.get(name)
        if proxy and proxy.is_ready:
            return proxy._real_model
        return None

    async def unload(self, name: str) -> bool:
        """Unload a model to free memory."""
        proxy = self._proxies.get(name)
        if not proxy:
            return False

        if proxy._metrics.memory_bytes:
            self._total_memory_bytes -= proxy._metrics.memory_bytes

        proxy.unload()
        return True

    async def reload(
        self,
        name: str,
        loader_func: Optional[Callable] = None,
    ) -> bool:
        """Reload a model (unload then load fresh)."""
        await self.unload(name)

        # If no new loader, we can't reload
        if loader_func is None:
            logger.warning(f"[AI] Cannot reload {name} without loader function")
            return False

        proxy = self._proxies.get(name)
        if proxy:
            self._queue_load(
                name,
                loader_func,
                proxy._metrics.priority,
                proxy._metrics.quantized,
            )
            return True
        return False

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive loading statistics including router info."""
        models = {}
        for name, proxy in self._proxies.items():
            m = proxy._metrics
            # Get engine info for this model if available
            hints = self._model_hints.get(name, {})
            engine_name = hints.get("selected_engine", "unknown")

            models[name] = {
                "status": m.status.name,
                "priority": m.priority.name,
                "load_duration_ms": m.load_duration_ms,
                "memory_mb": m.memory_bytes / 1024 / 1024 if m.memory_bytes else 0,
                "quantized": m.quantized,
                "calls_total": m.calls_total,
                "calls_while_warming": m.calls_while_warming,
                "errors": m.errors,
                "engine": engine_name,
            }

        ready_count = sum(1 for p in self._proxies.values() if p.is_ready)
        loading_count = sum(1 for p in self._proxies.values() if p.is_loading)

        return {
            "models": models,
            "summary": {
                "total": len(self._proxies),
                "ready": ready_count,
                "loading": loading_count,
                "failed": len(self._proxies) - ready_count - loading_count,
                "total_memory_mb": self._total_memory_bytes / 1024 / 1024,
            },
            "config": {
                "max_workers": self._config.max_workers,
                "quantize_default": self._config.quantize_default,
                "queue_timeout": self._config.queue_timeout,
            },
            "router": self._router.get_stats(),
        }

    async def shutdown(self) -> None:
        """Shutdown the manager and unload all models."""
        self._shutdown = True

        # Unload all models
        for name in list(self._proxies.keys()):
            await self.unload(name)

        # Shutdown executor
        self._executor.shutdown(wait=False)

        logger.info("[AI] AsyncModelManager shutdown complete")


# =============================================================================
# Global Manager Instance
# =============================================================================

_manager: Optional[AsyncModelManager] = None


def get_ai_manager() -> AsyncModelManager:
    """Get or create the global AI model manager."""
    global _manager
    if _manager is None:
        _manager = AsyncModelManager()
    return _manager


# =============================================================================
# Convenience Decorators
# =============================================================================

def ghost_model(
    name: str,
    priority: ModelPriority = ModelPriority.NORMAL,
    quantize: bool = True,
    lazy: bool = False,
):
    """
    Decorator to register a model loader function.

    Usage:
        @ghost_model("ecapa_tdnn", priority=ModelPriority.HIGH)
        def load_ecapa():
            return ECAPAModel.from_pretrained("...")

        # Use the model (auto-waits if not ready)
        embedding = await load_ecapa.proxy.encode(audio)
    """
    def decorator(loader_func: Callable[[], T]) -> Callable[[], T]:
        manager = get_ai_manager()
        proxy = manager.register_model(
            name=name,
            loader_func=loader_func,
            priority=priority,
            quantize=quantize,
            lazy=lazy,
        )

        @functools.wraps(loader_func)
        def wrapper() -> GhostModelProxy[T]:
            return proxy

        wrapper.proxy = proxy
        wrapper.name = name

        return wrapper

    return decorator


# =============================================================================
# Test & Debug
# =============================================================================

async def _test_ghost_proxy():
    """Test the Ghost Proxy system with Unified Optimization Router."""
    import asyncio
    import json

    print("=" * 70)
    print("Testing Hyper-Speed AI Loader v2.0 - Unified Optimization Router")
    print("=" * 70)

    # Test 1: Router engine discovery
    print("\n[TEST 1] Engine Discovery")
    print("-" * 50)
    router = get_optimization_router()
    engines = router.discover_engines()
    for engine, cap in sorted(engines.items(), key=lambda x: x[0].value):
        status = "✅" if cap.available else "❌"
        print(f"  {status} {engine.name:15} speedup={cap.speedup_factor:5.1f}x  mem={cap.memory_reduction:.2f}")

    # Test 2: Model categorization
    print("\n[TEST 2] Model Categorization")
    print("-" * 50)
    test_names = [
        "ecapa_tdnn_speaker",
        "yolo_vision_detector",
        "sentence_transformer_embed",
        "voice_svm_classifier",
        "generic_model",
    ]
    for name in test_names:
        category = router.categorize_model(name)
        print(f"  {name:30} -> {category.name}")

    # Test 3: Engine selection
    print("\n[TEST 3] Engine Selection")
    print("-" * 50)
    for name in test_names:
        engine, cap = router.select_engine(name)
        print(f"  {name:30} -> {engine.name:15} ({cap.speedup_factor}x speedup)")

    # Test 4: Ghost proxy with router
    print("\n[TEST 4] Ghost Proxy with Router")
    print("-" * 50)
    manager = get_ai_manager()

    # Simulate model loaders for different categories
    def voice_loader():
        print("  [LOADER] Loading voice model...")
        time.sleep(0.5)
        return {"type": "voice", "dim": 192}

    def vision_loader():
        print("  [LOADER] Loading vision model...")
        time.sleep(0.5)
        return {"type": "vision", "classes": 80}

    # Register models with different categories
    voice_proxy = manager.register_model(
        name="ecapa_speaker_model",
        loader_func=voice_loader,
        priority=ModelPriority.HIGH,
        hints={"category": "voice"},
    )

    vision_proxy = manager.register_model(
        name="yolo_detector",
        loader_func=vision_loader,
        priority=ModelPriority.NORMAL,
        hints={"category": "vision"},
    )

    print(f"  Voice proxy: {voice_proxy} (engine routing active)")
    print(f"  Vision proxy: {vision_proxy} (engine routing active)")

    # Wait for models
    print("\n  Waiting for models to load...")
    await asyncio.gather(
        voice_proxy.wait_ready(timeout=5),
        vision_proxy.wait_ready(timeout=5),
    )

    print(f"  Voice ready: {voice_proxy.is_ready}")
    print(f"  Vision ready: {vision_proxy.is_ready}")

    # Test 5: Stats with router info
    print("\n[TEST 5] Statistics with Router Info")
    print("-" * 50)
    stats = manager.get_stats()
    print(f"  Models registered: {stats['summary']['total']}")
    print(f"  Models ready: {stats['summary']['ready']}")
    print(f"  Router engines available: {stats['router']['available_count']}/{stats['router']['total_count']}")

    for name, model_stats in stats['models'].items():
        print(f"\n  {name}:")
        print(f"    Engine: {model_stats['engine']}")
        print(f"    Status: {model_stats['status']}")
        print(f"    Load time: {model_stats['load_duration_ms']:.0f}ms")

    # Cleanup
    await manager.shutdown()

    print("\n" + "=" * 70)
    print("✅ Unified Optimization Router test complete!")
    print("=" * 70)


async def _test_router_selection():
    """Test router engine selection logic in detail."""
    print("\nTesting Router Engine Selection Logic...")

    router = OptimizationRouter()
    router.discover_engines()

    # Test with explicit hints
    test_cases = [
        ("voice_model", {"category": "voice"}),
        ("svm_classifier", {"category": "svm"}),
        ("clip_vision", {"category": "vision"}),
        ("bert_embedder", {"category": "embedding"}),
        ("neural_net", {"category": "neural_net"}),
        ("generic", None),
        # With explicit engine request
        ("forced_onnx", {"engine": "onnx"}),
        # With memory constraint
        ("memory_constrained", {"max_memory_ratio": 0.25}),
    ]

    for name, hints in test_cases:
        engine, cap = router.select_engine(
            name,
            hints=hints,
            prefer_speed=True,
            max_memory_ratio=hints.get("max_memory_ratio", 1.0) if hints else 1.0,
        )
        cat = router.categorize_model(name, hints)
        print(f"  {name:25} cat={cat.name:12} -> {engine.name}")


if __name__ == "__main__":
    asyncio.run(_test_ghost_proxy())
