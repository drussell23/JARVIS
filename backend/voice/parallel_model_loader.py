#!/usr/bin/env python3
"""
Parallel Model Loader for Voice Services
=========================================

CRITICAL PERFORMANCE FIX: Provides true parallel model loading with a shared
thread pool to maximize CPU utilization during system startup.

Key Features:
1. Shared thread pool (4 workers) across all model loaders
2. True concurrent loading - multiple models load simultaneously
3. Progress tracking with callbacks
4. Model caching to prevent redundant loading
5. Graceful timeout handling

Architecture:
- Uses ProcessPoolExecutor for CPU-bound model loading (PyTorch)
- Falls back to ThreadPoolExecutor when ProcessPool isn't suitable
- Global singleton ensures consistent state across all services

Usage:
    from voice.parallel_model_loader import get_model_loader

    loader = get_model_loader()

    # Load multiple models in parallel
    results = await loader.load_models_parallel([
        ("whisper", load_whisper_func),
        ("ecapa", load_ecapa_func),
    ])
"""

import asyncio
import logging
import os
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple
import threading

logger = logging.getLogger(__name__)


# ============================================================================
# HUGGINGFACE/TRANSFORMERS WARNING SUPPRESSION
# ============================================================================
# The Wav2Vec2 model emits a benign warning about weights not initialized from
# the checkpoint. This is expected behavior when using the model with SpeechBrain
# for speaker verification. The warning about "training this model" doesn't apply
# since we're using it for inference with pre-trained weights.
# ============================================================================

@contextmanager
def suppress_hf_model_warnings():
    """
    Context manager to suppress expected HuggingFace model loading warnings.

    These warnings are benign and occur when:
    1. Wav2Vec2 model is loaded with PyTorch parametrizations
    2. The model is being used for inference (not training)

    Example:
        with suppress_hf_model_warnings():
            model = load_some_transformer_model()
    """
    with warnings.catch_warnings():
        # Suppress "Some weights were not initialized" warnings
        warnings.filterwarnings(
            "ignore",
            message="Some weights of.*were not initialized.*",
            category=UserWarning,
        )
        # Suppress "You should probably TRAIN this model" warnings
        warnings.filterwarnings(
            "ignore",
            message=".*TRAIN this model.*",
            category=UserWarning,
        )
        yield


class ModelState(Enum):
    """State of a model in the loading pipeline."""
    PENDING = "pending"
    LOADING = "loading"
    LOADED = "loaded"
    FAILED = "failed"
    CACHED = "cached"


@dataclass
class ModelLoadResult:
    """Result of loading a single model."""
    name: str
    state: ModelState
    model: Optional[Any] = None
    load_time_ms: float = 0.0
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def success(self) -> bool:
        return self.state in (ModelState.LOADED, ModelState.CACHED)


@dataclass
class ParallelLoadResult:
    """Result of parallel model loading."""
    results: Dict[str, ModelLoadResult] = field(default_factory=dict)
    total_time_ms: float = 0.0
    parallel_speedup: float = 1.0

    @property
    def all_success(self) -> bool:
        return all(r.success for r in self.results.values())

    @property
    def loaded_models(self) -> List[str]:
        return [name for name, r in self.results.items() if r.success]

    @property
    def failed_models(self) -> List[str]:
        return [name for name, r in self.results.items() if not r.success]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_time_ms": self.total_time_ms,
            "parallel_speedup": self.parallel_speedup,
            "all_success": self.all_success,
            "loaded_models": self.loaded_models,
            "failed_models": self.failed_models,
            "results": {
                name: {
                    "state": r.state.value,
                    "load_time_ms": r.load_time_ms,
                    "error": r.error
                }
                for name, r in self.results.items()
            }
        }


class ParallelModelLoader:
    """
    Parallel model loader with shared thread pool and caching.

    Provides true concurrent model loading by using a shared thread pool
    with multiple workers. This allows multiple heavy ML models to load
    simultaneously, reducing total startup time.
    """

    # Configuration
    DEFAULT_MAX_WORKERS = 4  # 4 threads for parallel loading
    DEFAULT_TIMEOUT = 120.0  # 2 minutes per model

    def __init__(
        self,
        max_workers: int = DEFAULT_MAX_WORKERS,
        use_process_pool: bool = False
    ):
        """
        Initialize the parallel model loader.

        Args:
            max_workers: Maximum number of parallel loading threads
            use_process_pool: Use ProcessPool instead of ThreadPool
                            (better for CPU-bound but has IPC overhead)
        """
        self.max_workers = max_workers
        self.use_process_pool = use_process_pool

        # Model cache - stores loaded models to prevent redundant loading
        self._model_cache: Dict[str, Any] = {}
        self._cache_lock = threading.Lock()

        # Loading state tracking
        self._loading_tasks: Dict[str, asyncio.Task] = {}
        self._load_times: Dict[str, float] = {}

        # Shared executor - created lazily
        self._executor: Optional[ThreadPoolExecutor] = None
        self._executor_lock = threading.Lock()

        # Stats
        self._total_loads = 0
        self._cache_hits = 0
        self._total_parallel_time_saved_ms = 0.0

        logger.info(
            f"ParallelModelLoader initialized "
            f"(workers={max_workers}, process_pool={use_process_pool})"
        )

    def _get_executor(self) -> ThreadPoolExecutor:
        """Get or create the shared thread pool executor."""
        if self._executor is None:
            with self._executor_lock:
                if self._executor is None:
                    if self.use_process_pool:
                        # ProcessPool is better for CPU-bound but has IPC overhead
                        # Not suitable for PyTorch models due to CUDA context issues
                        logger.warning(
                            "ProcessPool requested but not recommended for PyTorch models. "
                            "Using ThreadPool instead."
                        )

                    self._executor = ThreadPoolExecutor(
                        max_workers=self.max_workers,
                        thread_name_prefix="model_loader"
                    )
                    logger.info(
                        f"Created shared ThreadPoolExecutor with {self.max_workers} workers"
                    )
        return self._executor

    def is_cached(self, model_name: str) -> bool:
        """Check if a model is already cached."""
        with self._cache_lock:
            return model_name in self._model_cache

    def get_cached(self, model_name: str) -> Optional[Any]:
        """Get a cached model if available."""
        with self._cache_lock:
            return self._model_cache.get(model_name)

    def cache_model(self, model_name: str, model: Any) -> None:
        """Cache a loaded model."""
        with self._cache_lock:
            self._model_cache[model_name] = model
            logger.debug(f"Cached model: {model_name}")

    def clear_cache(self, model_name: Optional[str] = None) -> None:
        """Clear model cache (specific model or all)."""
        with self._cache_lock:
            if model_name:
                self._model_cache.pop(model_name, None)
                logger.info(f"Cleared cache for model: {model_name}")
            else:
                self._model_cache.clear()
                logger.info("Cleared all model cache")

    async def load_model(
        self,
        model_name: str,
        load_func: Callable[[], Any],
        timeout: float = DEFAULT_TIMEOUT,
        use_cache: bool = True,
        force_reload: bool = False
    ) -> ModelLoadResult:
        """
        Load a single model asynchronously with caching.

        Args:
            model_name: Unique identifier for the model
            load_func: Synchronous function that loads and returns the model
            timeout: Maximum time to wait for loading (seconds)
            use_cache: Whether to use/update the model cache
            force_reload: Force reload even if cached

        Returns:
            ModelLoadResult with model and timing info
        """
        self._total_loads += 1

        # Check cache first
        if use_cache and not force_reload:
            cached = self.get_cached(model_name)
            if cached is not None:
                self._cache_hits += 1
                logger.info(f"Cache hit for model: {model_name}")
                return ModelLoadResult(
                    name=model_name,
                    state=ModelState.CACHED,
                    model=cached,
                    load_time_ms=0.0
                )

        # Check if already loading
        if model_name in self._loading_tasks:
            task = self._loading_tasks[model_name]
            if not task.done():
                logger.info(f"Model {model_name} already loading, waiting...")
                try:
                    result = await asyncio.wait_for(task, timeout=timeout)
                    return result
                except asyncio.TimeoutError:
                    return ModelLoadResult(
                        name=model_name,
                        state=ModelState.FAILED,
                        error=f"Timeout waiting for existing load ({timeout}s)"
                    )

        # Start new loading task
        start_time = time.time()
        logger.info(f"Loading model: {model_name}")

        try:
            executor = self._get_executor()
            loop = asyncio.get_running_loop()

            # Run the synchronous load function in the thread pool
            model = await asyncio.wait_for(
                loop.run_in_executor(executor, load_func),
                timeout=timeout
            )

            load_time_ms = (time.time() - start_time) * 1000
            self._load_times[model_name] = load_time_ms

            # Cache the model
            if use_cache:
                self.cache_model(model_name, model)

            logger.info(f"Loaded model {model_name} in {load_time_ms:.0f}ms")

            return ModelLoadResult(
                name=model_name,
                state=ModelState.LOADED,
                model=model,
                load_time_ms=load_time_ms
            )

        except asyncio.TimeoutError:
            load_time_ms = (time.time() - start_time) * 1000
            error_msg = f"Timeout after {timeout}s ({load_time_ms:.0f}ms elapsed)"
            logger.error(f"Model {model_name} loading timed out: {error_msg}")
            return ModelLoadResult(
                name=model_name,
                state=ModelState.FAILED,
                load_time_ms=load_time_ms,
                error=error_msg
            )

        except Exception as e:
            load_time_ms = (time.time() - start_time) * 1000
            error_msg = str(e)
            logger.error(f"Model {model_name} loading failed: {error_msg}")
            return ModelLoadResult(
                name=model_name,
                state=ModelState.FAILED,
                load_time_ms=load_time_ms,
                error=error_msg
            )

    async def load_models_parallel(
        self,
        models: List[Tuple[str, Callable[[], Any]]],
        timeout_per_model: float = DEFAULT_TIMEOUT,
        use_cache: bool = True,
        progress_callback: Optional[Callable[[str, ModelState, float], None]] = None
    ) -> ParallelLoadResult:
        """
        Load multiple models in parallel using the shared thread pool.

        This is the key performance optimization - multiple models load
        simultaneously in different threads, reducing total startup time
        from sum(individual_times) to max(individual_times).

        Args:
            models: List of (name, load_function) tuples
            timeout_per_model: Timeout for each individual model
            use_cache: Whether to use/update model cache
            progress_callback: Optional callback(model_name, state, progress_pct)

        Returns:
            ParallelLoadResult with all results and timing info
        """
        if not models:
            return ParallelLoadResult()

        start_time = time.time()
        total_models = len(models)
        completed = 0

        logger.info(f"Starting parallel load of {total_models} models...")

        # Create loading tasks for all models
        async def load_with_progress(name: str, func: Callable) -> ModelLoadResult:
            nonlocal completed

            if progress_callback:
                progress_callback(name, ModelState.LOADING, completed / total_models)

            result = await self.load_model(
                name, func, timeout=timeout_per_model, use_cache=use_cache
            )

            completed += 1
            if progress_callback:
                progress_callback(name, result.state, completed / total_models)

            return result

        # Execute all loads in parallel
        tasks = [
            load_with_progress(name, func)
            for name, func in models
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        total_time_ms = (time.time() - start_time) * 1000

        # Process results
        result_dict = {}
        sequential_time_ms = 0.0

        for i, (name, _) in enumerate(models):
            res = results[i]
            if isinstance(res, Exception):
                result_dict[name] = ModelLoadResult(
                    name=name,
                    state=ModelState.FAILED,
                    error=str(res)
                )
            else:
                result_dict[name] = res
                sequential_time_ms += res.load_time_ms

        # Calculate parallel speedup
        parallel_speedup = sequential_time_ms / total_time_ms if total_time_ms > 0 else 1.0
        time_saved_ms = sequential_time_ms - total_time_ms
        self._total_parallel_time_saved_ms += max(0, time_saved_ms)

        logger.info(
            f"Parallel load complete: {total_time_ms:.0f}ms "
            f"(sequential would be {sequential_time_ms:.0f}ms, "
            f"speedup={parallel_speedup:.2f}x)"
        )

        return ParallelLoadResult(
            results=result_dict,
            total_time_ms=total_time_ms,
            parallel_speedup=parallel_speedup
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get loader statistics."""
        return {
            "total_loads": self._total_loads,
            "cache_hits": self._cache_hits,
            "cache_hit_rate": self._cache_hits / max(1, self._total_loads),
            "cached_models": list(self._model_cache.keys()),
            "load_times_ms": self._load_times.copy(),
            "total_time_saved_ms": self._total_parallel_time_saved_ms,
            "max_workers": self.max_workers,
            "executor_active": self._executor is not None
        }

    def shutdown(self) -> None:
        """Shutdown the executor and clear caches."""
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None
            logger.info("ParallelModelLoader executor shutdown")

        self.clear_cache()


# =============================================================================
# GLOBAL SINGLETON
# =============================================================================

_model_loader: Optional[ParallelModelLoader] = None
_loader_lock = threading.Lock()


def get_model_loader() -> ParallelModelLoader:
    """
    Get the global parallel model loader instance.

    This ensures all services share the same thread pool and cache,
    maximizing parallelization and preventing redundant model loading.
    """
    global _model_loader

    if _model_loader is None:
        with _loader_lock:
            if _model_loader is None:
                # Determine optimal worker count based on CPU cores
                cpu_count = os.cpu_count() or 4
                # Use 2-4 workers for model loading (memory constrained)
                max_workers = min(4, max(2, cpu_count // 2))

                _model_loader = ParallelModelLoader(max_workers=max_workers)
                logger.info(
                    f"Global ParallelModelLoader created with {max_workers} workers"
                )

    return _model_loader


def reset_model_loader() -> None:
    """Reset the global model loader (for testing)."""
    global _model_loader

    with _loader_lock:
        if _model_loader:
            _model_loader.shutdown()
        _model_loader = None


# =============================================================================
# CONVENIENCE FUNCTIONS FOR COMMON MODELS
# =============================================================================

async def load_whisper_model() -> Any:
    """Load Whisper model with caching."""
    def _load():
        from voice.whisper_audio_fix import _whisper_handler
        _whisper_handler.load_model()
        return _whisper_handler

    loader = get_model_loader()
    result = await loader.load_model("whisper", _load)
    return result.model if result.success else None


async def load_ecapa_encoder() -> Any:
    """Load ECAPA-TDNN speaker encoder with caching."""
    def _load():
        # v271.3: Route through centralized safe loader (meta tensor protection)
        try:
            from voice.engines.speechbrain_engine import safe_from_hparams
        except ImportError:
            from backend.voice.engines.speechbrain_engine import safe_from_hparams
        import torch

        # Force CPU for ECAPA-TDNN (MPS doesn't support required FFT ops)
        torch.set_num_threads(1)

        # Suppress benign HuggingFace warnings about Wav2Vec2 weight initialization
        with suppress_hf_model_warnings():
            encoder = safe_from_hparams(
                "speechbrain.inference.speaker.EncoderClassifier",
                model_name="ecapa_parallel_loader",
                source="speechbrain/spkrec-ecapa-voxceleb",
                run_opts={"device": "cpu"},
            )
        return encoder

    loader = get_model_loader()
    result = await loader.load_model("ecapa_encoder", _load, timeout=120.0)
    return result.model if result.success else None


async def load_all_voice_models() -> ParallelLoadResult:
    """
    Load all voice models in parallel.

    This is the optimized entry point for system startup - loads
    Whisper and ECAPA-TDNN simultaneously for minimum startup time.
    """
    from voice.whisper_audio_fix import _whisper_handler
    # v271.3: Route through centralized safe loader (meta tensor protection)
    try:
        from voice.engines.speechbrain_engine import safe_from_hparams
    except ImportError:
        from backend.voice.engines.speechbrain_engine import safe_from_hparams
    import torch

    def load_whisper():
        _whisper_handler.load_model()
        return _whisper_handler

    def load_ecapa():
        torch.set_num_threads(1)
        # Suppress benign HuggingFace warnings about Wav2Vec2 weight initialization
        with suppress_hf_model_warnings():
            return safe_from_hparams(
                "speechbrain.inference.speaker.EncoderClassifier",
                model_name="ecapa_parallel_all",
                source="speechbrain/spkrec-ecapa-voxceleb",
                run_opts={"device": "cpu"},
            )

    loader = get_model_loader()
    return await loader.load_models_parallel([
        ("whisper", load_whisper),
        ("ecapa_encoder", load_ecapa),
    ])
