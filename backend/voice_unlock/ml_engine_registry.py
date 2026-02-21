#!/usr/bin/env python3
"""
Advanced ML Engine Registry & Parallel Model Loader
====================================================

CRITICAL FIX: Ensures singleton pattern for all ML engines and
prevents multiple instance creation that causes HuggingFace fetches during runtime.

Features:
- 🔒 Thread-safe singleton registry for all ML engines
- ⚡ True async parallel model loading at startup
- 🚫 Blocks runtime HuggingFace downloads (all models preloaded)
- 🚦 Readiness gate - blocks unlock requests until models ready
- 📊 Health monitoring and telemetry
- 🔄 Automatic recovery on failure
- 🎯 Zero hardcoding - fully configurable via environment

Architecture:
    MLEngineRegistry (Singleton)
    ├── SpeechBrain ECAPA-TDNN (Speaker Verification)
    ├── SpeechBrain Wav2Vec2 (STT)
    ├── Whisper (STT)
    └── Vosk (Offline STT)

Usage:
    # At startup (main.py):
    registry = await get_ml_registry()
    await registry.prewarm_all_blocking()  # BLOCKS until all models ready

    # For requests:
    if not registry.is_ready:
        return {"error": "Voice unlock models still loading..."}

    # Get singleton engine:
    ecapa = registry.get_engine("ecapa_tdnn")
    whisper = registry.get_engine("whisper")
"""

import asyncio
import logging
import os
import time
import hashlib
import threading
import warnings
import weakref
from abc import ABC, abstractmethod

# Suppress torchaudio deprecation warning from SpeechBrain (cosmetic, works fine)
warnings.filterwarnings("ignore", message="torchaudio._backend.list_audio_backends has been deprecated")

# v95.0: Suppress "Wav2Vec2Model is frozen" warning (expected for inference - model frozen = not trainable)
warnings.filterwarnings("ignore", message=".*Wav2Vec2Model is frozen.*", category=UserWarning)
warnings.filterwarnings("ignore", message=".*model is frozen.*", category=UserWarning)

# v95.0: Pre-configure SpeechBrain HuggingFace logger to ERROR before any model loading
for _sb_hf_logger in [
    "speechbrain.lobes.models.huggingface_transformers",
    "speechbrain.lobes.models.huggingface_transformers.huggingface",
]:
    logging.getLogger(_sb_hf_logger).setLevel(logging.ERROR)

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, Callable, Dict, List, Optional, Set, Tuple, Type, TypeVar, Union
)
from concurrent.futures import ThreadPoolExecutor
import traceback

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION - All configurable via environment variables
# =============================================================================

class MLConfig:
    """
    Dynamic configuration loader for ML Engine Registry.
    All values configurable via environment variables.

    Integrated with Hybrid Cloud Architecture:
    - Automatically routes to GCP when memory pressure is high
    - Checks startup_decision from MemoryAwareStartup
    - Fallback to cloud when local engines fail
    """

    # Timeout configurations
    PREWARM_TIMEOUT = float(os.getenv("JARVIS_ML_PREWARM_TIMEOUT", "180"))  # 3 minutes total
    MODEL_LOAD_TIMEOUT = float(os.getenv("JARVIS_ML_MODEL_TIMEOUT", "120"))  # Per-model timeout
    HEALTH_CHECK_INTERVAL = float(os.getenv("JARVIS_ML_HEALTH_INTERVAL", "30"))

    # Parallel loading
    MAX_PARALLEL_LOADS = int(os.getenv("JARVIS_ML_MAX_PARALLEL", "4"))
    THREAD_POOL_SIZE = int(os.getenv("JARVIS_ML_THREAD_POOL", "4"))

    # Feature flags
    ENABLE_WHISPER = os.getenv("JARVIS_ML_ENABLE_WHISPER", "true").lower() == "true"
    ENABLE_ECAPA = os.getenv("JARVIS_ML_ENABLE_ECAPA", "true").lower() == "true"
    ENABLE_VOSK = os.getenv("JARVIS_ML_ENABLE_VOSK", "false").lower() == "true"
    ENABLE_SPEECHBRAIN_STT = os.getenv("JARVIS_ML_ENABLE_SPEECHBRAIN_STT", "true").lower() == "true"

    # Skip prewarm (for fast dev restarts)
    SKIP_PREWARM = os.getenv("JARVIS_SKIP_MODEL_PREWARM", "false").lower() == "true"

    # Cache settings
    CACHE_DIR = Path(os.getenv("JARVIS_ML_CACHE_DIR", str(Path.home() / ".cache" / "jarvis")))

    # HuggingFace settings
    HF_OFFLINE_MODE = os.getenv("HF_HUB_OFFLINE", "0") == "1"
    TRANSFORMERS_OFFLINE = os.getenv("TRANSFORMERS_OFFLINE", "0") == "1"

    # ==========================================================================
    # HYBRID CLOUD CONFIGURATION
    # Integrates with MemoryAwareStartup for automatic cloud routing
    # ==========================================================================
    CLOUD_FIRST_MODE = os.getenv("JARVIS_CLOUD_FIRST_ML", "false").lower() == "true"
    CLOUD_FALLBACK_ENABLED = os.getenv("JARVIS_CLOUD_FALLBACK", "true").lower() == "true"
    CLOUD_API_FAILURE_BACKOFF_BASE = float(
        os.getenv("JARVIS_CLOUD_API_FAILURE_BACKOFF_BASE", "20.0")
    )
    CLOUD_API_FAILURE_BACKOFF_MAX = float(
        os.getenv("JARVIS_CLOUD_API_FAILURE_BACKOFF_MAX", "600.0")
    )
    CLOUD_API_FAILURE_STREAK_RESET = float(
        os.getenv("JARVIS_CLOUD_API_FAILURE_STREAK_RESET", "300.0")
    )
    CLOUD_COOLDOWN_LOG_INTERVAL = float(
        os.getenv("JARVIS_CLOUD_COOLDOWN_LOG_INTERVAL", "30.0")
    )

    # RAM thresholds for automatic cloud routing (in GB)
    RAM_THRESHOLD_LOCAL = float(os.getenv("JARVIS_RAM_THRESHOLD_LOCAL", "6.0"))
    RAM_THRESHOLD_CLOUD = float(os.getenv("JARVIS_RAM_THRESHOLD_CLOUD", "4.0"))
    RAM_THRESHOLD_CRITICAL = float(os.getenv("JARVIS_RAM_THRESHOLD_CRITICAL", "2.0"))

    # Memory pressure threshold (0-100%)
    MEMORY_PRESSURE_THRESHOLD = float(os.getenv("JARVIS_MEMORY_PRESSURE_THRESHOLD", "75.0"))

    @classmethod
    def to_dict(cls) -> Dict[str, Any]:
        """Export configuration for logging."""
        return {
            "prewarm_timeout": cls.PREWARM_TIMEOUT,
            "model_load_timeout": cls.MODEL_LOAD_TIMEOUT,
            "max_parallel_loads": cls.MAX_PARALLEL_LOADS,
            "enable_whisper": cls.ENABLE_WHISPER,
            "enable_ecapa": cls.ENABLE_ECAPA,
            "enable_vosk": cls.ENABLE_VOSK,
            "enable_speechbrain_stt": cls.ENABLE_SPEECHBRAIN_STT,
            "skip_prewarm": cls.SKIP_PREWARM,
            "cache_dir": str(cls.CACHE_DIR),
            "cloud_first_mode": cls.CLOUD_FIRST_MODE,
            "cloud_fallback_enabled": cls.CLOUD_FALLBACK_ENABLED,
            "cloud_api_failure_backoff_base": cls.CLOUD_API_FAILURE_BACKOFF_BASE,
            "cloud_api_failure_backoff_max": cls.CLOUD_API_FAILURE_BACKOFF_MAX,
            "cloud_api_failure_streak_reset": cls.CLOUD_API_FAILURE_STREAK_RESET,
            "ram_threshold_local": cls.RAM_THRESHOLD_LOCAL,
            "memory_pressure_threshold": cls.MEMORY_PRESSURE_THRESHOLD,
        }

    @classmethod
    def check_memory_pressure(cls, attempt_relief: bool = True) -> Tuple[bool, float, str]:
        """
        Check current memory pressure and decide routing.

        v95.0: Enhanced with automatic memory relief when close to threshold.

        Args:
            attempt_relief: If True, try memory relief when close to threshold

        Returns:
            (use_cloud, available_ram_gb, reason)
        """
        try:
            import subprocess
            import gc

            # Get available RAM using macOS vm_stat
            result = subprocess.run(
                ["vm_stat"],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0:
                output = result.stdout
                page_size = 16384  # macOS page size

                # Parse vm_stat output
                free_pages = 0
                inactive_pages = 0
                speculative_pages = 0

                for line in output.split('\n'):
                    if 'Pages free:' in line:
                        free_pages = int(line.split(':')[1].strip().rstrip('.'))
                    elif 'Pages inactive:' in line:
                        inactive_pages = int(line.split(':')[1].strip().rstrip('.'))
                    elif 'Pages speculative:' in line:
                        speculative_pages = int(line.split(':')[1].strip().rstrip('.'))

                # Calculate available RAM (free + inactive + speculative)
                available_bytes = (free_pages + inactive_pages + speculative_pages) * page_size
                available_gb = available_bytes / (1024 ** 3)
                initial_available_gb = available_gb

                # v95.0: Attempt memory relief if close to threshold
                if attempt_relief and available_gb < cls.RAM_THRESHOLD_LOCAL and available_gb >= cls.RAM_THRESHOLD_CRITICAL * 0.8:
                    logger.debug(f"[MLConfig] Attempting memory relief (have {available_gb:.1f}GB, need {cls.RAM_THRESHOLD_LOCAL:.1f}GB)")

                    # Try garbage collection first
                    gc.collect()

                    # Try LocalMemoryFallback if available
                    try:
                        from backend.core.gcp_vm_manager import get_local_memory_fallback
                        import asyncio

                        fallback = get_local_memory_fallback()

                        # Run async relief in sync context
                        try:
                            loop = asyncio.get_running_loop()
                            # We're already in async context - can't run another event loop
                            # Just trigger GC which was already done above
                        except RuntimeError:
                            # Not in async context - can run relief
                            loop = asyncio.new_event_loop()
                            try:
                                loop.run_until_complete(
                                    fallback.attempt_local_relief(target_free_mb=cls.RAM_THRESHOLD_LOCAL * 1024)
                                )
                            finally:
                                loop.close()

                    except Exception as relief_error:
                        logger.debug(f"[MLConfig] Memory relief failed: {relief_error}")

                    # Re-check memory after relief
                    result2 = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
                    if result2.returncode == 0:
                        for line in result2.stdout.split('\n'):
                            if 'Pages free:' in line:
                                free_pages = int(line.split(':')[1].strip().rstrip('.'))
                            elif 'Pages inactive:' in line:
                                inactive_pages = int(line.split(':')[1].strip().rstrip('.'))
                            elif 'Pages speculative:' in line:
                                speculative_pages = int(line.split(':')[1].strip().rstrip('.'))

                        available_bytes = (free_pages + inactive_pages + speculative_pages) * page_size
                        available_gb = available_bytes / (1024 ** 3)

                        if available_gb > initial_available_gb:
                            logger.info(f"[MLConfig] Memory relief freed {(available_gb - initial_available_gb):.2f}GB")

                # v95.0: Adaptive thresholds based on system total RAM
                try:
                    import psutil
                    total_gb = psutil.virtual_memory().total / (1024 ** 3)

                    # Scale thresholds for smaller systems
                    if total_gb < 8:
                        effective_local_threshold = max(cls.RAM_THRESHOLD_LOCAL * 0.5, 1.5)
                        effective_critical_threshold = max(cls.RAM_THRESHOLD_CRITICAL * 0.5, 0.8)
                    elif total_gb < 16:
                        effective_local_threshold = max(cls.RAM_THRESHOLD_LOCAL * 0.75, 2.0)
                        effective_critical_threshold = max(cls.RAM_THRESHOLD_CRITICAL * 0.75, 1.2)
                    else:
                        effective_local_threshold = cls.RAM_THRESHOLD_LOCAL
                        effective_critical_threshold = cls.RAM_THRESHOLD_CRITICAL
                except Exception:
                    effective_local_threshold = cls.RAM_THRESHOLD_LOCAL
                    effective_critical_threshold = cls.RAM_THRESHOLD_CRITICAL

                # Decision logic with adaptive thresholds
                if available_gb < effective_critical_threshold:
                    return (True, available_gb, f"Critical RAM: {available_gb:.1f}GB < {effective_critical_threshold:.1f}GB")
                elif available_gb < effective_local_threshold:
                    return (True, available_gb, f"Low RAM: {available_gb:.1f}GB < {effective_local_threshold:.1f}GB")
                else:
                    return (False, available_gb, f"Sufficient RAM: {available_gb:.1f}GB >= {effective_local_threshold:.1f}GB")

        except Exception as e:
            logger.warning(f"Failed to check memory pressure: {e}")
            # Default to local if we can't check
            return (False, 0.0, f"Memory check failed: {e}")

        return (False, 0.0, "Unknown")


# =============================================================================
# ENGINE STATE & TELEMETRY
# =============================================================================

class EngineState(Enum):
    """State machine for ML engine lifecycle."""
    UNINITIALIZED = auto()
    LOADING = auto()
    READY = auto()
    ERROR = auto()
    UNLOADING = auto()
    DISABLED = auto()


@dataclass
class EngineMetrics:
    """Telemetry for a single ML engine."""
    engine_name: str
    state: EngineState = EngineState.UNINITIALIZED
    load_start_time: Optional[float] = None
    load_end_time: Optional[float] = None
    load_attempts: int = 0
    last_error: Optional[str] = None
    last_used: Optional[float] = None
    use_count: int = 0
    avg_inference_ms: float = 0.0

    @property
    def load_duration_ms(self) -> Optional[float]:
        if self.load_start_time and self.load_end_time:
            return (self.load_end_time - self.load_start_time) * 1000
        return None

    @property
    def is_ready(self) -> bool:
        return self.state == EngineState.READY

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine_name": self.engine_name,
            "state": self.state.name,
            "load_duration_ms": self.load_duration_ms,
            "load_attempts": self.load_attempts,
            "last_error": self.last_error,
            "use_count": self.use_count,
            "avg_inference_ms": self.avg_inference_ms,
        }


@dataclass
class RegistryStatus:
    """Overall status of the ML Engine Registry."""
    is_ready: bool = False
    prewarm_started: bool = False
    prewarm_completed: bool = False
    prewarm_start_time: Optional[float] = None
    prewarm_end_time: Optional[float] = None
    engines: Dict[str, EngineMetrics] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    # Non-blocking warmup state tracking
    is_warming_up: bool = False
    warmup_progress: float = 0.0  # 0.0 to 1.0
    warmup_current_engine: Optional[str] = None
    warmup_engines_completed: int = 0
    warmup_engines_total: int = 0
    background_task: Optional[asyncio.Task] = None

    @property
    def prewarm_duration_ms(self) -> Optional[float]:
        if self.prewarm_start_time and self.prewarm_end_time:
            return (self.prewarm_end_time - self.prewarm_start_time) * 1000
        return None

    @property
    def ready_count(self) -> int:
        return sum(1 for e in self.engines.values() if e.is_ready)

    @property
    def total_count(self) -> int:
        return len(self.engines)

    @property
    def warmup_status_message(self) -> str:
        """Human-readable warmup status for health checks."""
        if self.prewarm_completed:
            return "All ML models ready"
        elif self.is_warming_up:
            if self.warmup_current_engine:
                return f"Warming up {self.warmup_current_engine} ({self.warmup_engines_completed}/{self.warmup_engines_total})"
            return f"Warming up ML models ({int(self.warmup_progress * 100)}%)"
        elif self.prewarm_started:
            return "Prewarm started, initializing..."
        else:
            return "Not started"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_ready": self.is_ready,
            "ready_engines": f"{self.ready_count}/{self.total_count}",
            "prewarm_duration_ms": self.prewarm_duration_ms,
            "engines": {k: v.to_dict() for k, v in self.engines.items()},
            "errors": self.errors,
            # Non-blocking warmup status
            "is_warming_up": self.is_warming_up,
            "warmup_progress": self.warmup_progress,
            "warmup_current_engine": self.warmup_current_engine,
            "warmup_status": self.warmup_status_message,
        }


# =============================================================================
# CLOUD EMBEDDING CIRCUIT BREAKER (v21.1.0)
# =============================================================================
# Prevents hammering the cloud endpoint after consecutive failures.
# Follows the same CLOSED -> OPEN -> HALF_OPEN -> CLOSED pattern
# as EndpointCircuitBreaker in cloud_ecapa_client.py.

@dataclass
class CloudEmbeddingCircuitBreaker:
    """
    Lightweight circuit breaker for cloud embedding/verification requests.

    v3.5: Exponential backoff on recovery timeout. When the cloud service is
    persistently broken (returns 500 every probe), the fixed 30s recovery
    caused infinite OPEN → HALF_OPEN → probe fails → OPEN oscillation,
    generating ERROR + WARNING log pairs every 30 seconds forever.

    Now: each consecutive HALF_OPEN → OPEN transition doubles the effective
    recovery timeout (30s → 60s → 120s → ... → max). A single success
    resets the backoff to base. This naturally quiets log noise for
    persistently broken services while still allowing eventual recovery.

    Configuration driven by environment variables - no hardcoding.
    """
    failure_threshold: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_CLOUD_CB_FAILURES", "3"))
    )
    recovery_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_CLOUD_CB_RECOVERY", "30.0"))
    )
    success_threshold: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_CLOUD_CB_SUCCESS", "2"))
    )
    max_recovery_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_CLOUD_CB_MAX_RECOVERY", "600.0"))
    )

    state: str = "CLOSED"
    failure_count: int = 0
    success_count: int = 0
    half_open_success: int = 0
    last_failure_time: Optional[float] = None
    last_error: Optional[str] = None
    # v3.5: Exponential backoff state
    _consecutive_open_trips: int = 0
    _effective_recovery_timeout: float = 0.0  # Set in __post_init__

    def __post_init__(self):
        self._effective_recovery_timeout = self.recovery_timeout

    def record_success(self) -> None:
        """Record a successful cloud request."""
        self.failure_count = 0
        self.success_count += 1
        if self.state == "HALF_OPEN":
            self.half_open_success += 1
            if self.half_open_success >= self.success_threshold:
                self.state = "CLOSED"
                self.half_open_success = 0
                # v3.5: Reset backoff on full recovery
                self._consecutive_open_trips = 0
                self._effective_recovery_timeout = self.recovery_timeout
                logger.info("[CloudCB] Circuit CLOSED (recovered, backoff reset)")

    def record_failure(self, error: str = "") -> None:
        """Record a failed cloud request."""
        self.failure_count += 1
        self.success_count = 0
        self.last_failure_time = time.time()
        self.last_error = error
        if self.state == "HALF_OPEN":
            self.state = "OPEN"
            self.half_open_success = 0
            # v3.5: Exponential backoff — double the recovery timeout each
            # time a HALF_OPEN probe fails (service is persistently broken).
            # Capped at max_recovery_timeout to allow eventual recovery.
            self._consecutive_open_trips += 1
            self._effective_recovery_timeout = min(
                self.recovery_timeout * (2 ** self._consecutive_open_trips),
                self.max_recovery_timeout,
            )
            logger.warning(
                f"[CloudCB] Circuit OPEN (half-open probe failed: {error}). "
                f"Next probe in {self._effective_recovery_timeout:.0f}s "
                f"(backoff level {self._consecutive_open_trips})"
            )
        elif self.state != "OPEN" and self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            self._consecutive_open_trips = 1
            self._effective_recovery_timeout = self.recovery_timeout
            logger.warning(
                f"[CloudCB] Circuit OPEN ({self.failure_count} consecutive failures, "
                f"probe in {self._effective_recovery_timeout:.0f}s)"
            )

    def can_execute(self) -> Tuple[bool, str]:
        """Check if a request is allowed through the circuit breaker."""
        if self.state == "CLOSED":
            return True, "closed"
        if self.state == "OPEN":
            if (
                self.last_failure_time
                and (time.time() - self.last_failure_time) >= self._effective_recovery_timeout
            ):
                self.state = "HALF_OPEN"
                self.half_open_success = 0
                logger.info(
                    f"[CloudCB] Circuit HALF_OPEN (probing after "
                    f"{self._effective_recovery_timeout:.0f}s backoff)"
                )
                return True, "half_open"
            remaining = self._effective_recovery_timeout - (
                time.time() - (self.last_failure_time or 0)
            )
            return False, (
                f"open (wait {remaining:.0f}s, last: {self.last_error})"
            )
        # HALF_OPEN - allow one test request
        return True, "half_open"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state for diagnostics."""
        return {
            "state": self.state,
            "failure_count": self.failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout_base": self.recovery_timeout,
            "recovery_timeout_effective": self._effective_recovery_timeout,
            "max_recovery_timeout": self.max_recovery_timeout,
            "consecutive_open_trips": self._consecutive_open_trips,
            "last_error": self.last_error,
            "last_failure_time": self.last_failure_time,
        }


# =============================================================================
# ENGINE WRAPPER - Base class for all ML engines
# =============================================================================

class EngineNotAvailableError(RuntimeError):
    """Raised when engine is not available for use (unloaded/unloading)."""
    pass


class MLEngineWrapper(ABC):
    """
    Abstract base class for ML engine wrappers.
    Provides consistent interface and lifecycle management.

    Thread-Safety Guarantees:
    - Reference counting prevents engine unload while in use
    - RLock allows recursive locking from same thread
    - Condition variable coordinates unload with active users
    - All public methods are thread-safe
    """

    def __init__(self, name: str):
        self.name = name
        self.metrics = EngineMetrics(engine_name=name)
        self._engine: Any = None
        self._lock = asyncio.Lock()
        self._thread_lock = threading.Lock()

        # Thread-safe reference counting for engine access
        # Prevents segfaults from engine being unloaded while in use
        self._engine_use_count: int = 0
        self._engine_use_lock = threading.RLock()  # RLock for recursive safety
        self._unload_condition = threading.Condition(self._engine_use_lock)
        self._is_unloading: bool = False

    def acquire_engine(self) -> Any:
        """
        Thread-safe acquisition of engine reference.

        Increments use count and returns the engine.
        MUST be paired with release_engine() call.

        Returns:
            The loaded engine instance

        Raises:
            EngineNotAvailableError: If engine is None, unloading, or not ready
        """
        with self._engine_use_lock:
            # Check if engine is available
            if self._is_unloading:
                raise EngineNotAvailableError(
                    f"Engine {self.name} is being unloaded"
                )

            if self._engine is None:
                raise EngineNotAvailableError(
                    f"Engine {self.name} is not loaded"
                )

            if self.metrics.state != EngineState.READY:
                raise EngineNotAvailableError(
                    f"Engine {self.name} is in state {self.metrics.state.value}, not READY"
                )

            # Increment use count
            self._engine_use_count += 1

            # Return the engine reference
            return self._engine

    def release_engine(self) -> None:
        """
        Release engine reference after use.

        Decrements use count and notifies unload waiters if count reaches 0.
        Safe to call even if acquire failed (will be a no-op).
        """
        with self._engine_use_lock:
            if self._engine_use_count > 0:
                self._engine_use_count -= 1

                # Notify unload() if it's waiting and count is now 0
                if self._engine_use_count == 0:
                    self._unload_condition.notify_all()

    class _EngineContext:
        """Context manager for safe engine access."""

        def __init__(self, wrapper: 'MLEngineWrapper'):
            self._wrapper = wrapper
            self._engine: Any = None
            self._acquired = False

        def __enter__(self) -> Any:
            self._engine = self._wrapper.acquire_engine()
            self._acquired = True
            return self._engine

        def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
            if self._acquired:
                self._wrapper.release_engine()
                self._acquired = False
            return False  # Don't suppress exceptions

    def use_engine(self) -> '_EngineContext':
        """
        Context manager for safe engine access.

        Usage:
            with wrapper.use_engine() as engine:
                result = engine.encode_batch(audio)

        The engine is guaranteed to remain valid within the context.
        Protects against concurrent unload() calls.
        """
        return self._EngineContext(self)

    @property
    def is_loaded(self) -> bool:
        return self._engine is not None and self.metrics.state == EngineState.READY

    @abstractmethod
    async def _load_impl(self) -> Any:
        """Implementation-specific loading logic."""
        pass

    @abstractmethod
    async def _warmup_impl(self) -> bool:
        """Run a warmup inference to fully initialize."""
        pass

    async def load(self, timeout: float = MLConfig.MODEL_LOAD_TIMEOUT) -> bool:
        """
        Load the ML engine with timeout and error handling.
        Thread-safe and idempotent.
        """
        async with self._lock:
            if self.is_loaded:
                logger.debug(f"[{self.name}] Already loaded, skipping")
                return True

            self.metrics.state = EngineState.LOADING
            self.metrics.load_start_time = time.time()
            self.metrics.load_attempts += 1

            try:
                logger.info(f"🔄 [{self.name}] Loading ML engine...")

                # Load with timeout
                self._engine = await asyncio.wait_for(
                    self._load_impl(),
                    timeout=timeout
                )

                if self._engine is None:
                    raise RuntimeError("Engine loaded but returned None")

                # Run warmup inference
                logger.info(f"🔥 [{self.name}] Running warmup inference...")
                warmup_success = await asyncio.wait_for(
                    self._warmup_impl(),
                    timeout=30  # 30 second warmup timeout
                )

                if not warmup_success:
                    logger.warning(f"⚠️ [{self.name}] Warmup failed but engine loaded")

                self.metrics.load_end_time = time.time()
                self.metrics.state = EngineState.READY

                logger.info(
                    f"✅ [{self.name}] Engine ready in "
                    f"{self.metrics.load_duration_ms:.0f}ms"
                )
                return True

            except asyncio.TimeoutError:
                self.metrics.state = EngineState.ERROR
                self.metrics.last_error = f"Timeout after {timeout}s"
                logger.error(f"⏱️ [{self.name}] Load timeout after {timeout}s")
                return False

            except Exception as e:
                self.metrics.state = EngineState.ERROR
                self.metrics.last_error = str(e)
                logger.error(f"❌ [{self.name}] Load failed: {e}")
                logger.debug(traceback.format_exc())
                return False

    def get_engine(self) -> Any:
        """
        Get the loaded engine instance (thread-safe).

        WARNING: This returns a raw reference. For thread-safe access
        that prevents concurrent unload, use use_engine() context manager instead.
        """
        with self._thread_lock:
            if not self.is_loaded:
                raise RuntimeError(f"Engine {self.name} not loaded")
            self.metrics.last_used = time.time()
            self.metrics.use_count += 1
            return self._engine

    def get_use_count(self) -> int:
        """Get current number of active engine users (for debugging)."""
        with self._engine_use_lock:
            return self._engine_use_count

    async def unload(self, timeout: float = 30.0):
        """
        Unload the engine and free resources.

        Waits for all active users to release the engine before unloading.
        This prevents segfaults from engine being freed while in use.

        Args:
            timeout: Maximum seconds to wait for active users (default 30s)

        Raises:
            TimeoutError: If active users don't release within timeout
        """
        async with self._lock:
            if self._engine is None:
                return  # Already unloaded

            self.metrics.state = EngineState.UNLOADING

            # Signal that we're unloading (blocks new acquire_engine calls)
            with self._engine_use_lock:
                self._is_unloading = True

                # Wait for all active users to release
                if self._engine_use_count > 0:
                    logger.info(
                        f"🔄 [{self.name}] Waiting for {self._engine_use_count} "
                        f"active user(s) to release engine..."
                    )

                    # Wait with timeout
                    wait_start = time.time()
                    while self._engine_use_count > 0:
                        remaining = timeout - (time.time() - wait_start)
                        if remaining <= 0:
                            # Timeout - force unload anyway (risky but better than deadlock)
                            logger.warning(
                                f"⚠️ [{self.name}] Timeout waiting for {self._engine_use_count} "
                                f"user(s) to release. Forcing unload."
                            )
                            break

                        # Wait for condition signal with timeout
                        self._unload_condition.wait(timeout=min(1.0, remaining))

            try:
                # Clear the engine reference
                self._engine = None
                self.metrics.state = EngineState.UNINITIALIZED

                # Reset unloading flag
                with self._engine_use_lock:
                    self._is_unloading = False
                    self._engine_use_count = 0  # Reset in case of timeout

                logger.info(f"🧹 [{self.name}] Engine unloaded successfully")

            except Exception as e:
                logger.error(f"❌ [{self.name}] Unload error: {e}")
                with self._engine_use_lock:
                    self._is_unloading = False


# =============================================================================
# CONCRETE ENGINE WRAPPERS
# =============================================================================

class ECAPATDNNWrapper(MLEngineWrapper):
    """
    ECAPA-TDNN Speaker Verification Engine.
    Used for voice biometric authentication.
    """

    def __init__(self):
        super().__init__("ecapa_tdnn")
        self._encoder_loaded = False

    async def _load_impl(self) -> Any:
        """
        Load ECAPA-TDNN speaker encoder.

        v78.1: Fixed to run in executor to avoid blocking event loop.
        Also added intelligent cache checking to speed up cached loads.
        """
        from concurrent.futures import ThreadPoolExecutor
        import torch

        cache_dir = MLConfig.CACHE_DIR / "speechbrain" / "speaker_encoder"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # v78.1: Check if model is already cached (much faster load)
        model_cached = self._check_ecapa_cache(cache_dir)
        if model_cached:
            logger.info(f"   [{self.name}] ✅ Model cached locally, fast load expected")
        else:
            logger.info(f"   [{self.name}] ⚠️ Model not cached, downloading (this may take a while)...")

        logger.info(f"   [{self.name}] Importing SpeechBrain...")

        def _load_sync():
            from speechbrain.inference.speaker import EncoderClassifier

            # Force CPU for speaker encoder (MPS doesn't support FFT)
            run_opts = {"device": "cpu"}

            logger.info(f"   [{self.name}] Loading from: speechbrain/spkrec-ecapa-voxceleb")

            model = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir=str(cache_dir),
                run_opts=run_opts,
            )

            return model

        # v78.1: Run in executor to avoid blocking event loop
        # This is critical for async responsiveness during model loading
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ecapa_loader") as executor:
            model = await loop.run_in_executor(executor, _load_sync)

        self._encoder_loaded = True
        logger.info(f"   [{self.name}] ✅ ECAPA-TDNN loaded successfully")
        return model

    def _check_ecapa_cache(self, cache_dir: Path) -> bool:
        """
        v78.1: Check if ECAPA model files are already cached.

        Returns True if all essential model files exist locally.
        """
        essential_files = [
            "hyperparams.yaml",
            "embedding_model.ckpt",
            "classifier.ckpt",
            "label_encoder.ckpt",
        ]

        for filename in essential_files:
            filepath = cache_dir / filename
            if not filepath.exists():
                return False

        return True

    async def _warmup_impl(self) -> bool:
        """
        Run a test embedding extraction (synchronous on main thread).

        CRITICAL: Run synchronously to prevent segfaults on macOS/Apple Silicon.
        """
        # SAFETY: Capture engine reference
        engine_ref = self._engine
        engine_name = self.name

        if engine_ref is None:
            logger.warning(f"   [{engine_name}] Cannot warmup - engine is None")
            return False

        def _warmup_sync() -> bool:
            # Use captured engine_ref, NOT self._engine
            try:
                import numpy as np
                import torch

                # Double-check reference is valid (extra safety)
                if engine_ref is None:
                    raise RuntimeError("Engine reference became None")

                # Generate 1 second of test audio
                sample_rate = 16000
                duration = 1.0
                t = np.linspace(0, duration, int(sample_rate * duration))
                # Pink noise for realistic test
                white = np.random.randn(len(t)).astype(np.float32)
                test_audio = torch.tensor(white * 0.3).unsqueeze(0)

                # Extract embedding using captured reference
                with torch.no_grad():
                    embedding = engine_ref.encode_batch(test_audio)

                    # CRITICAL: Clone result before returning
                    if hasattr(embedding, 'clone'):
                        _ = embedding.clone().detach().cpu()

                logger.info(f"   [{engine_name}] Warmup embedding shape: {embedding.shape}")
                return True

            except Exception as e:
                logger.warning(f"   [{engine_name}] Warmup failed: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                return False

        try:
            # v122.0: Run warmup in dedicated PyTorch thread to avoid blocking event loop
            # while maintaining thread safety for Apple Silicon
            try:
                from core.pytorch_executor import pytorch_executor
                result = await pytorch_executor.run(_warmup_sync, timeout=30.0)
                return result
            except ImportError:
                # Fallback: run in thread pool to avoid blocking event loop
                # v123.0: Fixed - was running sync on event loop, now properly async
                logger.debug(f"   [{self.name}] pytorch_executor not available, using to_thread")
                result = await asyncio.to_thread(_warmup_sync)
                return result
        except Exception as e:
            logger.warning(f"   [{self.name}] Warmup wrapper failed: {e}")
            return False


class SpeechBrainSTTWrapper(MLEngineWrapper):
    """
    SpeechBrain Wav2Vec2 STT Engine.
    Used for speech-to-text transcription.
    """

    def __init__(self):
        super().__init__("speechbrain_stt")

    async def _load_impl(self) -> Any:
        """Load SpeechBrain Wav2Vec2 ASR model."""
        from concurrent.futures import ThreadPoolExecutor
        import torch
        import sys
        import platform

        is_apple_silicon = platform.machine() == 'arm64' and sys.platform == 'darwin'

        def _load_sync():
            from speechbrain.inference.ASR import EncoderDecoderASR

            cache_dir = MLConfig.CACHE_DIR / "speechbrain" / "speechbrain-wav2vec2"
            cache_dir.mkdir(parents=True, exist_ok=True)

            # Use MPS on Apple Silicon, CPU otherwise
            device = "mps" if is_apple_silicon and torch.backends.mps.is_available() else "cpu"
            run_opts = {"device": device}

            logger.info(f"   [{self.name}] Loading from: speechbrain/asr-wav2vec2-commonvoice-en")
            logger.info(f"   [{self.name}] Device: {device}")

            model = EncoderDecoderASR.from_hparams(
                source="speechbrain/asr-wav2vec2-commonvoice-en",
                savedir=str(cache_dir),
                run_opts=run_opts,
            )

            return model

        # Run synchronously on main thread (macOS stability)
        # loop = asyncio.get_running_loop()
        # with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt_loader") as executor:
        #    model = await loop.run_in_executor(executor, _load_sync)
        model = _load_sync()

        return model

    async def _warmup_impl(self) -> bool:
        """
        Run a test transcription (synchronous on main thread).

        CRITICAL: Run synchronously to prevent segfaults on macOS/Apple Silicon.
        """
        # SAFETY: Capture engine reference
        engine_ref = self._engine
        engine_name = self.name

        if engine_ref is None:
            logger.warning(f"   [{engine_name}] Cannot warmup - engine is None")
            return False

        def _warmup_sync() -> bool:
            # Use captured engine_ref, NOT self._engine
            try:
                import torch

                # Double-check reference is valid
                if engine_ref is None:
                    raise RuntimeError("Engine reference became None")

                # Generate 1 second of silence (quick warmup)
                sample_rate = 16000
                test_audio = torch.zeros(1, sample_rate)

                # Transcribe using captured reference
                with torch.no_grad():
                    _ = engine_ref.transcribe_batch(test_audio, torch.tensor([1.0]))

                logger.info(f"   [{engine_name}] Warmup transcription complete")
                return True

            except Exception as e:
                logger.warning(f"   [{engine_name}] Warmup failed: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                return False

        try:
            # Run synchronously
            result = _warmup_sync()
            return result
        except Exception as e:
            logger.warning(f"   [{self.name}] Warmup wrapper failed: {e}")
            return False


class WhisperWrapper(MLEngineWrapper):
    """
    OpenAI Whisper STT Engine.
    Primary STT engine for voice command recognition.
    """

    def __init__(self):
        super().__init__("whisper")
        self._model_name = os.getenv("JARVIS_WHISPER_MODEL", "base.en")

    async def _load_impl(self) -> Any:
        """Load Whisper model."""
        from concurrent.futures import ThreadPoolExecutor

        def _load_sync():
            import whisper

            logger.info(f"   [{self.name}] Loading model: {self._model_name}")

            # Download and load model
            model = whisper.load_model(
                self._model_name,
                download_root=str(MLConfig.CACHE_DIR / "whisper")
            )

            return model

        # Run synchronously on main thread (macOS stability)
        # loop = asyncio.get_running_loop()
        # with ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper_loader") as executor:
        #    model = await loop.run_in_executor(executor, _load_sync)
        model = _load_sync()

        return model

    async def _warmup_impl(self) -> bool:
        """
        Run a test transcription (synchronous on main thread).

        CRITICAL: Run synchronously to prevent segfaults on macOS/Apple Silicon.
        """
        # SAFETY: Capture engine reference
        engine_ref = self._engine
        engine_name = self.name

        if engine_ref is None:
            logger.warning(f"   [{engine_name}] Cannot warmup - engine is None")
            return False

        def _warmup_sync() -> bool:
            # Use captured engine_ref, NOT self._engine
            try:
                import numpy as np

                # Double-check reference is valid
                if engine_ref is None:
                    raise RuntimeError("Engine reference became None")

                # Generate 1 second of silence
                sample_rate = 16000
                test_audio = np.zeros(sample_rate, dtype=np.float32)

                # Transcribe using captured reference (this warms up the model)
                _ = engine_ref.transcribe(
                    test_audio,
                    language="en",
                    fp16=False,
                )

                logger.info(f"   [{engine_name}] Warmup transcription complete")
                return True

            except Exception as e:
                logger.warning(f"   [{engine_name}] Warmup failed: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                return False

        try:
            # Run synchronously
            result = _warmup_sync()
            return result
        except Exception as e:
            logger.warning(f"   [{self.name}] Warmup wrapper failed: {e}")
            return False


# =============================================================================
# ML ENGINE REGISTRY - The Singleton Manager
# =============================================================================

class MLEngineRegistry:
    """
    Thread-safe singleton registry for all ML engines.

    Ensures:
    - Only one instance of each engine is ever created
    - All engines are prewarmed at startup (blocking)
    - No HuggingFace fetches happen during runtime
    - Requests are blocked until engines are ready

    Hybrid Cloud Integration:
    - Checks memory pressure before loading local engines
    - Automatically routes to GCP when RAM is constrained
    - Integrates with MemoryAwareStartup for coordinated decisions
    - Provides cloud fallback for speaker verification
    """

    _instance: Optional["MLEngineRegistry"] = None
    _instance_lock = threading.Lock()
    _async_lock: Optional[asyncio.Lock] = None

    def __new__(cls) -> "MLEngineRegistry":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._engines: Dict[str, MLEngineWrapper] = {}
        self._status = RegistryStatus()
        self._ready_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()

        # Hybrid Cloud State
        self._use_cloud: bool = False
        self._cloud_endpoint: Optional[str] = None
        self._startup_decision: Optional[Any] = None  # StartupDecision from MemoryAwareStartup
        self._memory_pressure_at_init: Tuple[bool, float, str] = (False, 0.0, "Not checked")
        self._cloud_fallback_enabled: bool = MLConfig.CLOUD_FALLBACK_ENABLED
        self._cloud_verified: bool = False
        self._cloud_last_verified: float = 0.0
        self._cloud_endpoint_source: str = "unset"
        self._cloud_readiness_probe_lock = LazyAsyncLock()
        self._cloud_api_failure_streak: int = 0
        self._cloud_api_last_failure_at: float = 0.0
        self._cloud_api_degraded_until: float = 0.0
        self._cloud_api_last_error: str = ""
        self._cloud_api_last_cooldown_log_at: float = 0.0
        self._cloud_contract_verified: bool = False
        self._cloud_contract_endpoint: Optional[str] = None
        self._cloud_contract_last_checked: float = 0.0
        self._cloud_contract_last_error: str = ""

        # v21.1.0: Cloud embedding circuit breaker
        self._cloud_embedding_cb = CloudEmbeddingCircuitBreaker()

        # Register available engines based on config
        self._register_engines()

        logger.info(f"🔧 MLEngineRegistry initialized with {len(self._engines)} engines")
        logger.info(f"   Config: {MLConfig.to_dict()}")
        logger.info(f"   Cloud fallback enabled: {self._cloud_fallback_enabled}")

    def _register_engines(self):
        """Register all enabled engines."""
        if MLConfig.ENABLE_ECAPA:
            self._engines["ecapa_tdnn"] = ECAPATDNNWrapper()
            self._status.engines["ecapa_tdnn"] = self._engines["ecapa_tdnn"].metrics

        if MLConfig.ENABLE_WHISPER:
            self._engines["whisper"] = WhisperWrapper()
            self._status.engines["whisper"] = self._engines["whisper"].metrics

        if MLConfig.ENABLE_SPEECHBRAIN_STT:
            self._engines["speechbrain_stt"] = SpeechBrainSTTWrapper()
            self._status.engines["speechbrain_stt"] = self._engines["speechbrain_stt"].metrics

    @property
    def is_ready(self) -> bool:
        """Check if all critical engines are ready."""
        # ECAPA-TDNN is critical for voice unlock
        ecapa_ready = (
            not MLConfig.ENABLE_ECAPA or
            (self._engines.get("ecapa_tdnn") and self._engines["ecapa_tdnn"].is_loaded)
        )

        # At least one STT engine must be ready
        stt_ready = (
            (self._engines.get("whisper") and self._engines["whisper"].is_loaded) or
            (self._engines.get("speechbrain_stt") and self._engines["speechbrain_stt"].is_loaded)
        )

        return ecapa_ready and stt_ready

    @property
    def is_voice_unlock_ready(self) -> bool:
        """Check if voice unlock (speaker verification) is ready.

        This only requires ECAPA-TDNN, not STT engines.
        Use this for speaker embedding extraction and voice verification.

        Checks multiple paths for ECAPA availability:
        1. ML Registry's internal engine
        2. SpeechBrain engine's speaker encoder (external singleton)
        """
        # Check 1: ML Registry's internal ECAPA engine
        if self._engines.get("ecapa_tdnn") and self._engines["ecapa_tdnn"].is_loaded:
            return True

        # Check 2: Speaker Verification Service's encoder (singleton)
        try:
            from voice.speaker_verification_service import _speaker_verification_service
            if _speaker_verification_service is not None:
                engine = _speaker_verification_service.speechbrain_engine
                if engine and engine.speaker_encoder is not None:
                    return True
        except Exception:
            pass

        # Check 3: If ECAPA is disabled, we're "ready" (will use cloud/fallback)
        if not MLConfig.ENABLE_ECAPA:
            return True

        return False

    @property
    def status(self) -> RegistryStatus:
        """Get current registry status."""
        self._status.is_ready = self.is_ready
        return self._status

    def get_engine(self, name: str) -> Any:
        """
        Get a loaded engine by name.

        Raises:
            RuntimeError: If engine not loaded or doesn't exist
        """
        if name not in self._engines:
            raise RuntimeError(f"Unknown engine: {name}")

        engine = self._engines[name]
        if not engine.is_loaded:
            raise RuntimeError(
                f"Engine {name} not loaded. "
                f"State: {engine.metrics.state.name}, "
                f"Error: {engine.metrics.last_error}"
            )

        return engine.get_engine()

    def get_wrapper(self, name: str) -> Optional[MLEngineWrapper]:
        """Get engine wrapper (for advanced usage)."""
        return self._engines.get(name)

    async def wait_until_ready(self, timeout: float = 60.0) -> bool:
        """
        Wait until all engines are ready.

        Use this in request handlers to ensure models are loaded.
        """
        if self.is_ready:
            return True

        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ Timeout waiting for ML engines ({timeout}s)")
            return False

    async def prewarm_all_blocking(
        self,
        parallel: bool = True,
        timeout: float = MLConfig.PREWARM_TIMEOUT,
        startup_decision: Optional[Any] = None,
    ) -> RegistryStatus:
        """
        Prewarm ALL engines BLOCKING until complete.

        This should be called at startup BEFORE accepting any requests.
        Unlike background prewarming, this BLOCKS until all models are loaded.

        HYBRID CLOUD INTEGRATION:
        - Checks memory pressure before loading
        - Uses startup_decision from MemoryAwareStartup if provided
        - Skips local loading and routes to cloud if RAM is low
        - Falls back to cloud if local loading fails

        Args:
            parallel: Load engines in parallel (faster, more memory)
            timeout: Total timeout for all engines
            startup_decision: Optional StartupDecision from MemoryAwareStartup

        Returns:
            RegistryStatus with loading results
        """
        if MLConfig.SKIP_PREWARM:
            logger.info("⏭️ Skipping ML prewarm (JARVIS_SKIP_MODEL_PREWARM=true)")
            self._status.prewarm_completed = True
            return self._status

        if self._status.prewarm_completed:
            logger.debug("Prewarm already completed, returning cached status")
            return self._status

        self._status.prewarm_started = True
        self._status.prewarm_start_time = time.time()

        # =======================================================================
        # HYBRID CLOUD DECISION LOGIC
        # =======================================================================
        self._startup_decision = startup_decision

        # Check if we should use cloud based on startup decision
        if startup_decision is not None:
            # Use decision from MemoryAwareStartup
            if hasattr(startup_decision, 'use_cloud_ml') and startup_decision.use_cloud_ml:
                logger.info("=" * 70)
                logger.info("☁️  ML ENGINE REGISTRY: CLOUD-FIRST MODE")
                logger.info("=" * 70)
                logger.info(f"   Reason: {getattr(startup_decision, 'reason', 'StartupDecision requires cloud')}")
                logger.info(f"   Skip local Whisper: {getattr(startup_decision, 'skip_local_whisper', True)}")
                logger.info(f"   Skip local ECAPA: {getattr(startup_decision, 'skip_local_ecapa', True)}")
                logger.info("=" * 70)

                self._use_cloud = True
                await self._activate_cloud_routing()

                # CRITICAL FIX: Verify cloud backend is actually ready before marking as ready
                cloud_ready, cloud_reason = await self._verify_cloud_backend_ready()

                if cloud_ready:
                    # Cloud verified - mark as ready
                    self._status.prewarm_completed = True
                    self._status.prewarm_end_time = time.time()
                    self._status.is_ready = True
                    self._ready_event.set()
                    logger.info("✅ Cloud ML backend VERIFIED - voice unlock ready!")
                    return self._status
                else:
                    # Cloud failed - attempt local fallback
                    logger.warning(f"⚠️ Cloud backend not available: {cloud_reason}")
                    fallback_enabled = os.getenv("JARVIS_ECAPA_CLOUD_FALLBACK_ENABLED", "true").lower() == "true"

                    if fallback_enabled:
                        fallback_success = await self._fallback_to_local_ecapa(cloud_reason)
                        if fallback_success:
                            self._status.prewarm_completed = True
                            self._status.prewarm_end_time = time.time()
                            self._status.is_ready = True
                            self._ready_event.set()
                            logger.info("✅ Local ECAPA fallback successful - voice unlock ready!")
                            return self._status
                        else:
                            logger.error("❌ Both cloud and local ECAPA unavailable!")
                            self._status.errors.append(f"Cloud failed: {cloud_reason}, Local fallback also failed")
                    else:
                        logger.error("❌ Cloud unavailable and fallback disabled!")
                        self._status.errors.append(f"Cloud failed: {cloud_reason}, Fallback disabled")

                    # Mark as NOT ready - voice unlock will fail clearly
                    self._status.prewarm_completed = True
                    self._status.prewarm_end_time = time.time()
                    self._status.is_ready = False
                    logger.error("=" * 70)
                    logger.error("❌ ECAPA ENCODER UNAVAILABLE - Voice unlock will NOT work!")
                    logger.error("=" * 70)
                    return self._status

        # Check memory pressure directly if no startup decision
        use_cloud, available_ram, reason = MLConfig.check_memory_pressure()
        self._memory_pressure_at_init = (use_cloud, available_ram, reason)

        if use_cloud and not MLConfig.CLOUD_FIRST_MODE:
            logger.info("=" * 70)
            logger.info("☁️  ML ENGINE REGISTRY: AUTO CLOUD MODE (Memory Pressure)")
            logger.info("=" * 70)
            logger.info(f"   Available RAM: {available_ram:.1f}GB")
            logger.info(f"   Reason: {reason}")
            logger.info(f"   Action: Routing ML to cloud instead of local loading")
            logger.info("=" * 70)

            self._use_cloud = True
            await self._activate_cloud_routing()

            # CRITICAL FIX: Verify cloud backend is actually ready before marking as ready
            cloud_ready, cloud_reason = await self._verify_cloud_backend_ready()

            if cloud_ready:
                self._status.prewarm_completed = True
                self._status.prewarm_end_time = time.time()
                self._status.is_ready = True
                self._ready_event.set()
                logger.info("✅ Cloud ML backend VERIFIED - voice unlock ready!")
                return self._status
            else:
                # Cloud failed - attempt local fallback despite memory pressure
                logger.warning(f"⚠️ Cloud backend not available: {cloud_reason}")
                fallback_enabled = os.getenv("JARVIS_ECAPA_CLOUD_FALLBACK_ENABLED", "true").lower() == "true"

                if fallback_enabled:
                    logger.warning("⚠️ Attempting local ECAPA despite memory pressure...")
                    fallback_success = await self._fallback_to_local_ecapa(cloud_reason)
                    if fallback_success:
                        self._status.prewarm_completed = True
                        self._status.prewarm_end_time = time.time()
                        self._status.is_ready = True
                        self._ready_event.set()
                        logger.info("✅ Local ECAPA fallback successful - voice unlock ready!")
                        return self._status

                # Both failed - continue to local prewarm as last resort
                logger.warning("🔄 Cloud and quick fallback failed - attempting full local prewarm...")

        # =======================================================================
        # LOCAL PREWARM (Sufficient RAM)
        # =======================================================================
        logger.info("=" * 70)
        logger.info("🚀 STARTING ML ENGINE PREWARM (BLOCKING - LOCAL)")
        logger.info("=" * 70)
        logger.info(f"   Available RAM: {available_ram:.1f}GB")
        logger.info(f"   Engines to load: {list(self._engines.keys())}")
        logger.info(f"   Parallel loading: {parallel}")
        logger.info(f"   Timeout: {timeout}s")
        logger.info("=" * 70)

        try:
            if parallel:
                # Load all engines in parallel
                await self._prewarm_parallel(timeout)
            else:
                # Load sequentially
                await self._prewarm_sequential(timeout)

        except Exception as e:
            logger.error(f"❌ Prewarm error: {e}")
            self._status.errors.append(str(e))

        self._status.prewarm_end_time = time.time()
        self._status.prewarm_completed = True
        self._status.is_ready = self.is_ready

        # Signal ready if successful
        if self.is_ready:
            self._ready_event.set()

        # Log summary
        logger.info("=" * 70)
        if self.is_ready:
            logger.info(f"✅ ML PREWARM COMPLETE - {self._status.ready_count}/{self._status.total_count} engines ready")
            logger.info(f"   Duration: {self._status.prewarm_duration_ms:.0f}ms")
            logger.info("   → Voice unlock will be INSTANT!")
        else:
            logger.warning(f"⚠️ ML PREWARM PARTIAL - {self._status.ready_count}/{self._status.total_count} engines ready")
            logger.warning(f"   Errors: {self._status.errors}")

        for name, engine in self._engines.items():
            status_icon = "✅" if engine.is_loaded else "❌"
            load_time = engine.metrics.load_duration_ms
            load_str = f"{load_time:.0f}ms" if load_time else "N/A"
            logger.info(f"   {status_icon} {name}: {engine.metrics.state.name} ({load_str})")

        logger.info("=" * 70)

        return self._status

    async def _prewarm_parallel(self, timeout: float):
        """Load all engines in parallel with progress tracking."""
        logger.info(f"🔄 Loading {len(self._engines)} engines in PARALLEL...")

        # Initialize progress tracking
        total_engines = len(self._engines)
        self._status.warmup_engines_total = total_engines
        self._status.warmup_engines_completed = 0
        self._status.warmup_progress = 0.0
        self._status.warmup_current_engine = "parallel_loading"

        completed_count = 0
        completed_lock = asyncio.Lock()

        async def load_with_progress(name: str, engine):
            """Load an engine and update progress on completion."""
            nonlocal completed_count
            try:
                result = await engine.load()
                async with completed_lock:
                    completed_count += 1
                    self._status.warmup_engines_completed = completed_count
                    self._status.warmup_progress = completed_count / total_engines
                    logger.info(f"   ✅ {name} loaded ({completed_count}/{total_engines})")
                return result
            except Exception as e:
                async with completed_lock:
                    completed_count += 1
                    self._status.warmup_engines_completed = completed_count
                    self._status.warmup_progress = completed_count / total_engines
                logger.error(f"   ❌ {name} failed ({completed_count}/{total_engines}): {e}")
                raise

        # Create tasks for all engines
        tasks = {
            name: asyncio.create_task(load_with_progress(name, engine))
            for name, engine in self._engines.items()
        }

        # Wait for all with timeout
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks.values(), return_exceptions=True),
                timeout=timeout
            )

            # Process results
            for (name, _), result in zip(tasks.items(), results):
                if isinstance(result, Exception):
                    self._status.errors.append(f"{name}: {result}")
                elif not result:
                    self._status.errors.append(f"{name}: load returned False")

        except asyncio.TimeoutError:
            logger.error(f"⏱️ Parallel prewarm timeout after {timeout}s")
            self._status.errors.append(f"Timeout after {timeout}s")

            # Cancel remaining tasks
            for name, task in tasks.items():
                if not task.done():
                    task.cancel()
                    logger.warning(f"   Cancelled: {name}")

        # Final progress update
        self._status.warmup_current_engine = None
        self._status.warmup_progress = 1.0

    async def _prewarm_sequential(self, timeout: float):
        """Load engines one by one with progress tracking."""
        logger.info(f"🔄 Loading {len(self._engines)} engines SEQUENTIALLY...")

        # Initialize progress tracking
        total_engines = len(self._engines)
        self._status.warmup_engines_total = total_engines
        self._status.warmup_engines_completed = 0
        self._status.warmup_progress = 0.0

        remaining_timeout = timeout
        completed = 0

        for name, engine in self._engines.items():
            if remaining_timeout <= 0:
                logger.warning(f"⏱️ No time remaining for {name}")
                break

            # Update current engine being loaded
            self._status.warmup_current_engine = name

            start = time.time()

            try:
                success = await engine.load(timeout=remaining_timeout)
                if not success:
                    self._status.errors.append(f"{name}: load failed")
                    logger.warning(f"   ⚠️ {name} load returned False")
                else:
                    logger.info(f"   ✅ {name} loaded ({completed + 1}/{total_engines})")
            except Exception as e:
                logger.error(f"   ❌ {name} failed ({completed + 1}/{total_engines}): {e}")
                self._status.errors.append(f"{name}: {e}")

            elapsed = time.time() - start
            remaining_timeout -= elapsed

            # Update progress after each engine
            completed += 1
            self._status.warmup_engines_completed = completed
            self._status.warmup_progress = completed / total_engines

        # Final progress update
        self._status.warmup_current_engine = None
        self._status.warmup_progress = 1.0

    # =========================================================================
    # NON-BLOCKING PREWARM METHODS
    # =========================================================================

    @property
    def is_warming_up(self) -> bool:
        """Check if prewarm is currently in progress."""
        return self._status.is_warming_up

    @property
    def warmup_progress(self) -> float:
        """Get warmup progress (0.0 to 1.0)."""
        return self._status.warmup_progress

    @property
    def warmup_status(self) -> Dict[str, Any]:
        """Get detailed warmup status for health checks."""
        return {
            "is_warming_up": self._status.is_warming_up,
            "is_ready": self.is_ready,
            "progress": self._status.warmup_progress,
            "current_engine": self._status.warmup_current_engine,
            "engines_completed": self._status.warmup_engines_completed,
            "engines_total": self._status.warmup_engines_total,
            "status_message": self._status.warmup_status_message,
            "prewarm_started": self._status.prewarm_started,
            "prewarm_completed": self._status.prewarm_completed,
        }

    def prewarm_background(
        self,
        parallel: bool = True,
        timeout: float = MLConfig.PREWARM_TIMEOUT,
        startup_decision: Optional[Any] = None,
        on_complete: Optional[Callable[[RegistryStatus], None]] = None,
    ) -> asyncio.Task:
        """
        Launch ML model prewarm as a BACKGROUND TASK.

        This method returns IMMEDIATELY and does NOT block FastAPI startup.
        The prewarm runs in the background while the server accepts requests.

        Use this method instead of prewarm_all_blocking() in main.py lifespan
        to ensure FastAPI can respond to health checks during model loading.

        Args:
            parallel: Load engines in parallel (faster, more memory)
            timeout: Total timeout for all engines
            startup_decision: Optional StartupDecision from MemoryAwareStartup
            on_complete: Optional callback when prewarm completes

        Returns:
            asyncio.Task that can be awaited later if needed

        Example:
            # In main.py lifespan:
            prewarm_task = registry.prewarm_background()
            # FastAPI starts immediately, models load in background
            # Optional: await prewarm_task later if you need to wait
        """
        async def _prewarm_wrapper():
            """Wrapper that handles progress tracking and callbacks."""
            try:
                # Set warming up state
                self._status.is_warming_up = True
                self._status.warmup_engines_total = len(self._engines)
                self._status.warmup_engines_completed = 0
                self._status.warmup_progress = 0.0

                logger.info("=" * 70)
                logger.info("🚀 STARTING BACKGROUND ML PREWARM (NON-BLOCKING)")
                logger.info("=" * 70)
                logger.info(f"   FastAPI will continue accepting requests during warmup")
                logger.info(f"   Engines to load: {list(self._engines.keys())}")
                logger.info("=" * 70)

                # Run the actual prewarm
                result = await self.prewarm_all_blocking(
                    parallel=parallel,
                    timeout=timeout,
                    startup_decision=startup_decision,
                )

                # Update state when complete
                self._status.is_warming_up = False
                self._status.warmup_progress = 1.0
                self._status.warmup_current_engine = None

                logger.info("=" * 70)
                logger.info("✅ BACKGROUND PREWARM COMPLETE")
                logger.info(f"   Ready: {self.is_ready}")
                logger.info(f"   Engines: {result.ready_count}/{result.total_count}")
                if result.prewarm_duration_ms:
                    logger.info(f"   Duration: {result.prewarm_duration_ms:.0f}ms")
                logger.info("=" * 70)

                # Call completion callback if provided
                if on_complete:
                    try:
                        on_complete(result)
                    except Exception as e:
                        logger.warning(f"Prewarm completion callback failed: {e}")

                return result

            except Exception as e:
                self._status.is_warming_up = False
                self._status.errors.append(f"Background prewarm failed: {e}")
                logger.error(f"❌ Background prewarm failed: {e}")
                logger.error(traceback.format_exc())
                raise

        # Create and store the background task
        task = asyncio.create_task(_prewarm_wrapper())
        self._status.background_task = task

        logger.info("🔄 Background prewarm task created - FastAPI continues running")
        return task

    async def prewarm_with_progress(
        self,
        parallel: bool = True,
        timeout: float = MLConfig.PREWARM_TIMEOUT,
        startup_decision: Optional[Any] = None,
    ) -> RegistryStatus:
        """
        Prewarm all engines with detailed progress tracking.

        Similar to prewarm_all_blocking but with per-engine progress updates.
        This is useful for showing progress in a UI or status endpoint.

        Args:
            parallel: Load engines in parallel (faster, more memory)
            timeout: Total timeout for all engines
            startup_decision: Optional StartupDecision from MemoryAwareStartup

        Returns:
            RegistryStatus with loading results
        """
        self._status.is_warming_up = True
        self._status.warmup_engines_total = len(self._engines)
        self._status.warmup_engines_completed = 0
        self._status.warmup_progress = 0.0

        try:
            if parallel:
                # Load engines in parallel with progress tracking
                await self._prewarm_parallel_with_progress(timeout)
            else:
                # Load sequentially with progress tracking
                await self._prewarm_sequential_with_progress(timeout)

            # Run the standard prewarm for any remaining setup
            return await self.prewarm_all_blocking(
                parallel=False,  # Don't re-load
                timeout=timeout,
                startup_decision=startup_decision,
            )

        finally:
            self._status.is_warming_up = False
            self._status.warmup_progress = 1.0

    async def _prewarm_parallel_with_progress(self, timeout: float):
        """Load all engines in parallel with progress tracking."""
        logger.info(f"🔄 Loading {len(self._engines)} engines in PARALLEL with progress tracking...")

        async def load_with_progress(name: str, engine: MLEngineWrapper):
            """Load a single engine and update progress."""
            self._status.warmup_current_engine = name
            try:
                result = await engine.load()
                self._status.warmup_engines_completed += 1
                self._status.warmup_progress = (
                    self._status.warmup_engines_completed / self._status.warmup_engines_total
                )
                logger.info(f"   ✅ {name} loaded ({self._status.warmup_engines_completed}/{self._status.warmup_engines_total})")
                return result
            except Exception as e:
                self._status.warmup_engines_completed += 1
                self._status.warmup_progress = (
                    self._status.warmup_engines_completed / self._status.warmup_engines_total
                )
                logger.error(f"   ❌ {name} failed: {e}")
                return False

        # Create tasks for all engines
        tasks = [
            asyncio.create_task(load_with_progress(name, engine))
            for name, engine in self._engines.items()
        ]

        # Wait for all with timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.error(f"⏱️ Parallel prewarm with progress timeout after {timeout}s")
            # Cancel remaining tasks
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def _prewarm_sequential_with_progress(self, timeout: float):
        """Load engines sequentially with progress tracking."""
        logger.info(f"🔄 Loading {len(self._engines)} engines SEQUENTIALLY with progress tracking...")

        remaining_timeout = timeout

        for i, (name, engine) in enumerate(self._engines.items()):
            if remaining_timeout <= 0:
                logger.warning(f"⏱️ No time remaining for {name}")
                break

            self._status.warmup_current_engine = name
            self._status.warmup_progress = i / self._status.warmup_engines_total

            start = time.time()

            try:
                await engine.load(timeout=remaining_timeout)
                logger.info(f"   ✅ {name} loaded ({i + 1}/{self._status.warmup_engines_total})")
            except Exception as e:
                logger.error(f"   ❌ {name} failed: {e}")

            self._status.warmup_engines_completed = i + 1
            elapsed = time.time() - start
            remaining_timeout -= elapsed

    async def cancel_background_prewarm(self) -> bool:
        """
        Cancel the background prewarm task if running.

        Returns:
            True if task was cancelled, False if not running
        """
        if self._status.background_task and not self._status.background_task.done():
            self._status.background_task.cancel()
            self._status.is_warming_up = False
            logger.info("🛑 Background prewarm cancelled")
            return True
        return False

    async def shutdown(self):
        """Gracefully shutdown all engines."""
        logger.info("🛑 Shutting down ML Engine Registry...")
        self._shutdown_event.set()

        for name, engine in self._engines.items():
            try:
                await engine.unload()
            except Exception as e:
                logger.error(f"Error unloading {name}: {e}")

        logger.info("✅ ML Engine Registry shutdown complete")

    # =========================================================================
    # HYBRID CLOUD ROUTING METHODS
    # =========================================================================

    @property
    def is_using_cloud(self) -> bool:
        """Check if registry is routing to cloud.

        v3.4: Gate on _cloud_endpoint too. _use_cloud can be set True before
        endpoint discovery completes (or after discovery fails to find one).
        Without this, callers see is_using_cloud=True, attempt cloud extraction,
        and hit "Cloud endpoint not configured" → ERROR → fallback to local
        on every single call. The extra check makes the property truthful.
        """
        return self._use_cloud and self._cloud_endpoint is not None

    @property
    def cloud_endpoint(self) -> Optional[str]:
        """Get the current cloud endpoint URL."""
        return self._cloud_endpoint

    def _set_cloud_endpoint(self, endpoint: Optional[str], source: str) -> None:
        """Set cloud endpoint and reset readiness/contract state if changed."""
        normalized = (endpoint or "").strip().rstrip("/")
        new_endpoint = normalized or None
        old_endpoint = (self._cloud_endpoint or "").strip().rstrip("/") or None

        changed = (new_endpoint != old_endpoint) or (source != self._cloud_endpoint_source)
        self._cloud_endpoint = new_endpoint
        self._cloud_endpoint_source = source

        if changed:
            self._cloud_verified = False
            self._cloud_last_verified = 0.0
            self._cloud_contract_verified = False
            self._cloud_contract_endpoint = None
            self._cloud_contract_last_checked = 0.0
            self._cloud_contract_last_error = ""

    async def _verify_cloud_endpoint_contract(
        self,
        endpoint: Optional[str] = None,
        timeout: Optional[float] = None,
        force: bool = False,
    ) -> Tuple[bool, str]:
        """Validate that endpoint implements the ECAPA ML API contract."""
        import aiohttp

        target = (endpoint or self._cloud_endpoint or "").strip().rstrip("/")
        if not target:
            return False, "Cloud endpoint not configured"

        ttl = max(5.0, float(os.getenv("JARVIS_CLOUD_CONTRACT_VERIFY_TTL", "180.0")))
        now = time.time()
        if (
            not force
            and endpoint is None
            and self._cloud_contract_verified
            and self._cloud_contract_endpoint == target
            and (now - self._cloud_contract_last_checked) <= ttl
        ):
            return True, "Contract verification cached"

        req_timeout = max(1.0, float(timeout or os.getenv("JARVIS_CLOUD_CONTRACT_TIMEOUT", "4.0")))
        health_url = f"{target}/api/ml/health"
        embed_url = f"{target}/api/ml/speaker_embedding"

        try:
            async with aiohttp.ClientSession() as session:
                # Contract requirement 1: /api/ml/health must exist and advertise ECAPA readiness.
                async with session.get(
                    health_url,
                    timeout=aiohttp.ClientTimeout(total=req_timeout),
                    headers={"Accept": "application/json"},
                ) as response:
                    if response.status != 200:
                        reason = f"/api/ml/health returned HTTP {response.status}"
                        self._cloud_contract_verified = False
                        self._cloud_contract_endpoint = target
                        self._cloud_contract_last_checked = time.time()
                        self._cloud_contract_last_error = reason
                        return False, reason

                    payload = await response.json()
                    ecapa_ready = payload.get("ecapa_ready")
                    if not isinstance(ecapa_ready, bool):
                        reason = "missing boolean ecapa_ready in /api/ml/health"
                        self._cloud_contract_verified = False
                        self._cloud_contract_endpoint = target
                        self._cloud_contract_last_checked = time.time()
                        self._cloud_contract_last_error = reason
                        return False, reason

                # Contract requirement 2: embedding path must be routable.
                async with session.options(
                    embed_url,
                    timeout=aiohttp.ClientTimeout(total=req_timeout),
                    headers={"Accept": "application/json"},
                ) as response:
                    if response.status == 404:
                        reason = "/api/ml/speaker_embedding route missing (HTTP 404)"
                        self._cloud_contract_verified = False
                        self._cloud_contract_endpoint = target
                        self._cloud_contract_last_checked = time.time()
                        self._cloud_contract_last_error = reason
                        return False, reason
                    if response.status >= 500:
                        reason = f"/api/ml/speaker_embedding options failed (HTTP {response.status})"
                        self._cloud_contract_verified = False
                        self._cloud_contract_endpoint = target
                        self._cloud_contract_last_checked = time.time()
                        self._cloud_contract_last_error = reason
                        return False, reason

        except asyncio.TimeoutError:
            reason = f"contract probe timed out after {req_timeout:.1f}s"
            self._cloud_contract_verified = False
            self._cloud_contract_endpoint = target
            self._cloud_contract_last_checked = time.time()
            self._cloud_contract_last_error = reason
            return False, reason
        except Exception as e:
            reason = f"contract probe error: {type(e).__name__}: {e}"
            self._cloud_contract_verified = False
            self._cloud_contract_endpoint = target
            self._cloud_contract_last_checked = time.time()
            self._cloud_contract_last_error = reason[:240]
            return False, reason

        self._cloud_contract_verified = True
        self._cloud_contract_endpoint = target
        self._cloud_contract_last_checked = time.time()
        self._cloud_contract_last_error = ""
        return True, "ECAPA contract verified"

    async def _activate_cloud_routing(self) -> bool:
        """
        Activate cloud routing for ML operations.

        This configures the registry to route speaker verification
        and other ML operations to GCP instead of local processing.

        Returns:
            True if cloud routing was successfully activated
        """
        try:
            candidates: List[Tuple[str, str]] = []

            # 1) MemoryAwareStartup candidate (if available).
            try:
                from core.memory_aware_startup import get_startup_manager
                startup_manager = await get_startup_manager()

                if startup_manager.is_cloud_ml_active:
                    endpoint = await startup_manager.get_ml_endpoint("speaker_verify")
                    if endpoint:
                        candidates.append((endpoint, "memory_aware_active"))
                elif self._startup_decision:
                    result = await startup_manager.activate_cloud_ml_backend()
                    if result.get("success") and result.get("ip"):
                        _ecapa_port = int(os.getenv("JARVIS_ECAPA_PORT", "8015"))
                        candidates.append(
                            (f"http://{result.get('ip')}:{_ecapa_port}", "memory_aware_activated")
                        )
            except ImportError:
                logger.debug("MemoryAwareStartup not available")
            except Exception as e:
                logger.debug(f"MemoryAwareStartup endpoint discovery failed: {e}")

            # 2) Explicit cloud endpoint env vars (Cloud Run preferred).
            cloud_run_url = os.getenv(
                "ECAPA_CLOUD_RUN_URL",
                os.getenv(
                    "JARVIS_CLOUD_ML_ENDPOINT",
                    "https://jarvis-ml-888774109345.us-central1.run.app",
                ),
            )
            if cloud_run_url:
                candidates.append((cloud_run_url, "cloud_run_env"))

            explicit_cloud_endpoint = os.getenv(
                "JARVIS_CLOUD_ECAPA_ENDPOINT",
                os.getenv("JARVIS_ML_CLOUD_ENDPOINT", ""),
            )
            if explicit_cloud_endpoint:
                candidates.append((explicit_cloud_endpoint, "explicit_env"))

            # 3) Optional localhost fallbacks, but only if explicitly allowed.
            allow_local_fallback = os.getenv(
                "JARVIS_CLOUD_ALLOW_LOCAL_ENDPOINTS", "false"
            ).lower() in ("1", "true", "yes")
            if allow_local_fallback:
                try:
                    import socket

                    for host_port, source in (
                        (("127.0.0.1", 8090), "local_reactor_core"),
                        (("127.0.0.1", 8000), "local_jarvis_prime"),
                    ):
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(0.3)
                        result = sock.connect_ex(host_port)
                        sock.close()
                        if result == 0:
                            candidates.append((f"http://{host_port[0]}:{host_port[1]}", source))
                except Exception as e:
                    logger.debug(f"Local endpoint discovery failed: {e}")

            # Deduplicate while preserving priority order.
            seen: Set[str] = set()
            deduped_candidates: List[Tuple[str, str]] = []
            for endpoint, source in candidates:
                normalized = endpoint.strip().rstrip("/")
                if not normalized or normalized in seen or "None" in normalized:
                    continue
                seen.add(normalized)
                deduped_candidates.append((normalized, source))

            # Accept first endpoint that passes ECAPA contract checks.
            for endpoint, source in deduped_candidates:
                self._set_cloud_endpoint(endpoint, source)
                contract_ok, contract_reason = await self._verify_cloud_endpoint_contract(
                    force=True
                )
                if contract_ok:
                    self._use_cloud = True
                    logger.info(
                        f"☁️  Cloud routing activated for ML operations → {self._cloud_endpoint} "
                        f"(source={self._cloud_endpoint_source})"
                    )
                    return True
                logger.warning(
                    f"⚠️ Rejected cloud endpoint {endpoint} (source={source}): {contract_reason}"
                )

            self._set_cloud_endpoint(None, "none")
            self._use_cloud = False
            logger.info("☁️  No ECAPA-compatible cloud endpoint discovered — staying in local mode")
            return False

        except Exception as e:
            logger.error(f"❌ Failed to activate cloud routing: {e}")
            self._use_cloud = False
            return False

    async def _verify_cloud_backend_ready(
        self,
        timeout: float = None,
        retry_count: int = None,
        test_extraction: bool = None,
        wait_for_ecapa: bool = None,
        ecapa_wait_timeout: float = None
    ) -> Tuple[bool, str]:
        """
        v116.0: ROBUST Cloud Backend Verification with Intelligent Polling.

        CRITICAL: Verifies cloud endpoint works BEFORE marking registry as ready.
        Otherwise, voice unlock fails with 0% confidence.

        ROOT CAUSE FIX v116.0 (fixes v115.0 endpoint priority bug):
        - Uses SHARED aiohttp session for all polling (prevents connection overhead)
        - Adaptive polling intervals (fast initially, slower as time passes)
        - Progressive diagnostics (more verbose logging as timeout approaches)
        - Handles Cloud Run cold start (30-90s) vs warm response (1-5s)
        - Cross-repo health state coordination
        - Circuit breaker pattern for repeated failures

        Three-phase verification:
        1. Health check (fast) - verify endpoint is reachable
        2. Wait for ECAPA ready (adaptive) - poll until model is loaded
        3. Test extraction (optional) - verify ECAPA actually works

        Args:
            timeout: Request timeout in seconds (default from env JARVIS_ECAPA_CLOUD_TIMEOUT)
            retry_count: Number of retry attempts (default from env JARVIS_ECAPA_CLOUD_RETRIES)
            test_extraction: Actually test embedding extraction (default from env)
            wait_for_ecapa: Wait for ECAPA to become ready (default from env)
            ecapa_wait_timeout: Max time to wait for ECAPA ready (default from env)

        Returns:
            Tuple of (is_ready: bool, reason: str)
        """
        import aiohttp

        # v115.0: Configuration with intelligent defaults for Cloud Run cold start
        timeout = timeout or float(os.getenv("JARVIS_ECAPA_CLOUD_TIMEOUT", "30.0"))
        retry_count = retry_count or int(os.getenv("JARVIS_ECAPA_CLOUD_RETRIES", "3"))
        fallback_enabled = os.getenv("JARVIS_ECAPA_CLOUD_FALLBACK_ENABLED", "true").lower() == "true"
        test_extraction = test_extraction if test_extraction is not None else os.getenv("JARVIS_ECAPA_CLOUD_TEST_EXTRACTION", "true").lower() == "true"

        # v115.0: Wait for ECAPA with adaptive timeouts
        wait_for_ecapa = wait_for_ecapa if wait_for_ecapa is not None else os.getenv("JARVIS_ECAPA_WAIT_FOR_READY", "true").lower() == "true"
        ecapa_wait_timeout = ecapa_wait_timeout or float(os.getenv("JARVIS_ECAPA_WAIT_TIMEOUT", "90.0"))  # Reduced from 120s - faster feedback

        # v115.0: Adaptive polling intervals
        poll_interval_initial = float(os.getenv("JARVIS_ECAPA_POLL_INTERVAL_INITIAL", "2.0"))  # Fast initially
        poll_interval_max = float(os.getenv("JARVIS_ECAPA_POLL_INTERVAL_MAX", "10.0"))  # Slow down over time
        poll_interval_growth = float(os.getenv("JARVIS_ECAPA_POLL_INTERVAL_GROWTH", "1.5"))  # Growth factor

        if not self._cloud_endpoint:
            return False, "Cloud endpoint not configured"

        base_url = self._cloud_endpoint.rstrip('/')
        strict_contract = os.getenv("JARVIS_CLOUD_STRICT_CONTRACT", "true").lower() in (
            "1",
            "true",
            "yes",
        )

        contract_ok, contract_reason = await self._verify_cloud_endpoint_contract(
            timeout=min(timeout, float(os.getenv("JARVIS_CLOUD_CONTRACT_TIMEOUT", "4.0"))),
            force=True,
        )
        if not contract_ok:
            self._cloud_verified = False
            if strict_contract:
                self._use_cloud = False
            reason = (
                f"Cloud endpoint contract validation failed for {base_url} "
                f"(source={self._cloud_endpoint_source}): {contract_reason}"
            )
            logger.warning(reason)
            return False, reason

        # =====================================================================
        # v115.0: CHECK CROSS-REPO STATE FIRST (Trinity Coordination)
        # =====================================================================
        # If another repo (JARVIS Prime, Reactor Core) has recently verified
        # Cloud ECAPA, we can skip our own verification and use their result.
        # This significantly speeds up Trinity startup when multiple repos
        # start simultaneously.
        # =====================================================================
        cross_repo_state = await self._read_cross_repo_ecapa_state()
        if cross_repo_state:
            cross_ready = cross_repo_state.get("cloud_ecapa_ready", False)
            cross_endpoint = cross_repo_state.get("cloud_endpoint", "")
            cross_source = cross_repo_state.get("source_repo", "unknown")
            cross_age = time.time() - cross_repo_state.get("timestamp", 0)
            cross_contract_verified = cross_repo_state.get("cloud_contract_verified", True)

            # Only use cross-repo state if it's for the same endpoint
            if cross_ready and cross_endpoint == self._cloud_endpoint and cross_contract_verified:
                logger.info(f"✅ [v115.0] Using cross-repo ECAPA state from {cross_source} ({cross_age:.1f}s ago)")
                self._cloud_verified = True
                self._cloud_last_verified = cross_repo_state.get("timestamp", time.time())
                return True, f"Cross-repo verified by {cross_source}"
            elif cross_ready and cross_endpoint == self._cloud_endpoint and not cross_contract_verified:
                logger.info(
                    f"ℹ️  [v115.0] Cross-repo state from {cross_source} rejected "
                    f"(missing/failed contract verification)"
                )
            elif not cross_ready:
                logger.info(f"ℹ️  [v115.0] Cross-repo state from {cross_source}: ECAPA not ready")
                # Continue with our own verification - the other repo might have timed out

        logger.info(f"🔍 [v115.0] Verifying cloud backend: {base_url}")
        logger.info(f"   Wait for ECAPA: {wait_for_ecapa}, Max wait: {ecapa_wait_timeout}s")

        # v115.0: Expanded health paths with priority order
        health_paths = [
            "/health",                  # Standard health (fastest)
            "/api/ml/health",           # ML-specific health (has ecapa_ready)
            "/healthz",                 # Kubernetes/Cloud Run standard
            "/api/voice-unlock/status", # JARVIS Voice Unlock API
            "/v1/models",               # JARVIS-Prime/OpenAI compatible
        ]

        # Prevent self-deadlock if checking localhost during startup
        is_localhost = "localhost" in base_url or "127.0.0.1" in base_url or "0.0.0.0" in base_url
        if is_localhost and not self.is_ready:
            logger.info(f"   Note: Checking local backend {base_url} during startup")

        endpoint_reachable = False
        ecapa_ready = False
        reason = "Unknown error"
        last_health_data = {}
        working_health_path = None
        consecutive_failures = 0
        total_poll_count = 0
        verification_start = time.time()

        # =====================================================================
        # PHASE 1: Health check with INTELLIGENT ENDPOINT DISCOVERY (v115.0)
        # =====================================================================
        # v115.0: Use SHARED session for all requests to reduce overhead
        connector = aiohttp.TCPConnector(
            limit=10,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )

        async with aiohttp.ClientSession(connector=connector) as session:
            for attempt in range(1, retry_count + 1):
                paths_to_try = [working_health_path] if working_health_path else health_paths

                for health_path in paths_to_try:
                    health_endpoint = f"{base_url}{health_path}"

                    try:
                        async with session.get(
                            health_endpoint,
                            timeout=aiohttp.ClientTimeout(total=timeout),
                            headers={"Accept": "application/json"}
                        ) as response:
                            if response.status == 200:
                                try:
                                    data = await response.json()
                                    last_health_data = data
                                    endpoint_reachable = True
                                    working_health_path = health_path

                                    # v115.0: Enhanced ECAPA detection
                                    if data.get("ecapa_ready", False) or data.get("status") == "healthy" and "ecapa" in str(data).lower():
                                        ecapa_ready = data.get("ecapa_ready", False)
                                        if ecapa_ready:
                                            load_source = data.get("load_source", "unknown")
                                            startup_ms = data.get("startup_duration_ms", "N/A")
                                            logger.info(f"✅ Cloud ECAPA ready on first check! Source: {load_source}, Startup: {startup_ms}ms")
                                            break
                                        else:
                                            status = data.get("status", "unknown")
                                            logger.info(f"☁️  Endpoint reachable (path: {health_path}), ECAPA initializing (status: {status})")
                                            break
                                    else:
                                        status = data.get("status", "unknown")
                                        logger.info(f"☁️  Cloud endpoint reachable (path: {health_path}), status: {status}")
                                        break

                                except Exception as json_err:
                                    endpoint_reachable = True
                                    working_health_path = health_path
                                    logger.info(f"✅ Cloud backend responded (non-JSON, path: {health_path})")
                                    break

                            elif response.status == 404:
                                logger.debug(f"   Path {health_path} returned 404, trying next...")
                                continue
                            elif response.status >= 500:
                                reason = f"Cloud returned HTTP {response.status}"
                                logger.warning(f"⚠️ Attempt {attempt}/{retry_count}: {reason} (server error)")
                            else:
                                reason = f"Cloud returned HTTP {response.status}"
                                logger.debug(f"   Attempt {attempt}/{retry_count}: {reason}")

                    except asyncio.TimeoutError:
                        reason = f"Cloud health check timed out after {timeout}s"
                        logger.info(f"⏱️ Attempt {attempt}/{retry_count}: {reason}")
                        consecutive_failures += 1
                    except aiohttp.ClientError as e:
                        reason = f"Cloud connection error: {type(e).__name__}: {e}"
                        logger.info(f"🔌 Attempt {attempt}/{retry_count}: {reason}")
                        consecutive_failures += 1
                    except Exception as e:
                        reason = f"Cloud verification error: {e}"
                        logger.info(f"   Attempt {attempt}/{retry_count}: {reason}")
                        consecutive_failures += 1

                if endpoint_reachable:
                    break

                # v115.0: Adaptive backoff
                if attempt < retry_count:
                    backoff = min(2 ** (attempt - 1), 5)
                    logger.debug(f"   Retrying in {backoff}s...")
                    await asyncio.sleep(backoff)

            # =====================================================================
            # PHASE 2: Wait for ECAPA with ADAPTIVE POLLING (v115.0)
            # =====================================================================
            if endpoint_reachable and not ecapa_ready and wait_for_ecapa:
                logger.info(f"⏳ [v115.0] Waiting for Cloud ECAPA (adaptive polling, max {ecapa_wait_timeout}s)...")
                wait_start = time.time()
                current_poll_interval = poll_interval_initial
                last_status = "unknown"
                poll_endpoint = f"{base_url}{working_health_path}" if working_health_path else f"{base_url}/health"

                while time.time() - wait_start < ecapa_wait_timeout:
                    try:
                        total_poll_count += 1
                        elapsed = time.time() - wait_start
                        remaining = ecapa_wait_timeout - elapsed

                        async with session.get(
                            poll_endpoint,
                            timeout=aiohttp.ClientTimeout(total=min(timeout, 15.0)),  # Cap individual request timeout
                            headers={"Accept": "application/json"}
                        ) as response:
                            if response.status == 200:
                                data = await response.json()
                                last_health_data = data
                                consecutive_failures = 0  # Reset on success

                                if data.get("ecapa_ready", False):
                                    ecapa_ready = True
                                    load_source = data.get("load_source", "unknown")
                                    load_time_ms = data.get("load_time_ms", data.get("startup_duration_ms", "N/A"))
                                    using_prebaked = data.get("using_prebaked_cache", False)
                                    logger.info(f"✅ Cloud ECAPA ready after {elapsed:.1f}s ({total_poll_count} polls)")
                                    logger.info(f"   Load source: {load_source}, Prebaked: {using_prebaked}")
                                    if load_time_ms and load_time_ms != "N/A":
                                        logger.info(f"   Cloud model load time: {load_time_ms}ms")
                                    break

                                # v115.0: Adaptive logging based on time elapsed
                                status = data.get("status", data.get("startup_state", "unknown"))
                                if status != last_status or elapsed > 30:
                                    if elapsed < 15:
                                        logger.debug(f"   [{elapsed:.0f}s] ECAPA status: {status} (cold start expected)")
                                    elif elapsed < 45:
                                        logger.info(f"   [{elapsed:.0f}s] ECAPA status: {status} (waiting {remaining:.0f}s more)")
                                    else:
                                        logger.warning(f"   [{elapsed:.0f}s] ⚠️ ECAPA still not ready: {status} ({remaining:.0f}s remaining)")
                                    last_status = status

                            elif response.status >= 500:
                                consecutive_failures += 1
                                logger.debug(f"   Poll returned HTTP {response.status} (failure #{consecutive_failures})")
                            else:
                                logger.debug(f"   Poll returned HTTP {response.status}")

                    except asyncio.TimeoutError:
                        consecutive_failures += 1
                        if consecutive_failures >= 3:
                            logger.warning(f"   [{elapsed:.0f}s] ⚠️ {consecutive_failures} consecutive timeouts")
                    except aiohttp.ClientError as e:
                        consecutive_failures += 1
                        logger.debug(f"   Poll error: {type(e).__name__}")
                    except Exception as e:
                        consecutive_failures += 1
                        logger.debug(f"   Poll error: {e}")

                    # v115.0: Adaptive interval - start fast, slow down over time
                    await asyncio.sleep(current_poll_interval)
                    current_poll_interval = min(current_poll_interval * poll_interval_growth, poll_interval_max)

                    # v115.0: Circuit breaker - too many failures means backend is down
                    if consecutive_failures >= 10:
                        reason = f"Too many consecutive failures ({consecutive_failures}) - backend appears down"
                        logger.warning(f"🔌 {reason}")
                        break

                if not ecapa_ready:
                    elapsed = time.time() - wait_start
                    reason = f"ECAPA not ready after {elapsed:.1f}s ({total_poll_count} polls, last status: {last_health_data.get('status', last_health_data.get('startup_state', 'unknown'))})"
                    logger.info(f"⏱️ {reason}")

        # v115.0: END OF SHARED SESSION SCOPE
        # =====================================================================

        # v115.0: Calculate total verification time for diagnostics
        total_verification_time = time.time() - verification_start

        # Determine if health check passed
        health_check_passed = endpoint_reachable and ecapa_ready

        if not health_check_passed and not ecapa_ready and endpoint_reachable:
            reason = f"Cloud endpoint reachable but ECAPA not ready after {ecapa_wait_timeout}s ({total_poll_count} polls)"

        # If health check failed, return early
        if not health_check_passed:
            self._cloud_verified = False
            logger.info(f"ℹ️  Cloud ECAPA not available ({total_verification_time:.1f}s verification)")
            logger.info(f"   Reason: {reason}")
            if fallback_enabled:
                logger.info("🔄 Using local ECAPA processing (cloud fallback enabled)")
            return False, reason

        # =====================================================================
        # PHASE 3: Test actual embedding extraction (ensures ECAPA works)
        # =====================================================================
        if test_extraction:
            logger.info("🧪 Testing cloud ECAPA embedding extraction...")

            try:
                # Generate minimal test audio (100ms of silence at 16kHz)
                import numpy as np
                import base64

                test_audio = np.zeros(1600, dtype=np.float32)  # 100ms at 16kHz
                test_audio_bytes = test_audio.tobytes()
                test_audio_b64 = base64.b64encode(test_audio_bytes).decode('utf-8')

                embed_endpoint = f"{self._cloud_endpoint.rstrip('/')}/api/ml/speaker_embedding"

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        embed_endpoint,
                        json={
                            "audio_data": test_audio_b64,
                            "sample_rate": 16000,
                            "format": "float32",
                            "test_mode": True  # Signal this is a verification test
                        },
                        timeout=aiohttp.ClientTimeout(total=timeout * 2),  # Allow more time for extraction
                        headers={"Accept": "application/json"}
                    ) as response:
                        if response.status == 200:
                            result = await response.json()
                            if result.get("success") or result.get("embedding"):
                                embedding_size = len(result.get("embedding", []))
                                logger.info(f"✅ Cloud ECAPA extraction verified (embedding size: {embedding_size})")
                                self._cloud_verified = True
                                self._cloud_last_verified = time.time()
                                await self._write_cross_repo_ecapa_state(
                                    True,
                                    "Cloud backend healthy (health + extraction test)",
                                )
                                return True, f"Cloud ECAPA verified (health + extraction test)"
                            else:
                                reason = f"Cloud extraction returned no embedding: {result}"
                                logger.warning(f"⚠️ {reason}")
                        else:
                            reason = f"Cloud extraction returned HTTP {response.status}"
                            logger.warning(f"⚠️ {reason}")

            except asyncio.TimeoutError:
                reason = f"Cloud extraction test timed out after {timeout * 2}s"
                logger.warning(f"⏱️ {reason}")
            except Exception as e:
                reason = f"Cloud extraction test error: {e}"
                logger.warning(f"❌ {reason}")

            # Extraction test failed - cloud is reachable but ECAPA doesn't work
            logger.error("❌ Cloud ECAPA extraction test FAILED")
            logger.error("   The cloud endpoint is reachable but ECAPA embedding extraction failed.")
            logger.error("   This would cause 0% confidence if we marked cloud as ready.")

            if fallback_enabled:
                logger.warning("🔄 Falling back to local ECAPA...")

            self._cloud_verified = False
            return False, reason

        # No extraction test - just use health check result
        self._cloud_verified = True
        self._cloud_last_verified = time.time()

        # v115.0: Write cross-repo health state for Trinity coordination
        await self._write_cross_repo_ecapa_state(True, "Cloud backend healthy (extraction test skipped)")

        return True, "Cloud backend healthy (extraction test skipped)"

    async def _write_cross_repo_ecapa_state(
        self,
        is_ready: bool,
        reason: str,
        endpoint: str = None,
    ) -> None:
        """
        v115.0: Write Cloud ECAPA health state for cross-repo coordination.

        This allows JARVIS, JARVIS Prime, and Reactor Core to share the
        Cloud ECAPA verification result, avoiding redundant verification
        attempts and improving startup time.

        Args:
            is_ready: Whether Cloud ECAPA is verified and ready
            reason: Status message or failure reason
            endpoint: The cloud endpoint URL (uses self._cloud_endpoint if None)
        """
        try:
            import json
            from pathlib import Path

            cross_repo_dir = Path.home() / ".jarvis" / "cross_repo"
            cross_repo_dir.mkdir(parents=True, exist_ok=True)

            state_file = cross_repo_dir / "cloud_ecapa_state.json"

            state = {
                "cloud_ecapa_ready": is_ready,
                "cloud_ecapa_verified": self._cloud_verified,
                "cloud_endpoint": endpoint or self._cloud_endpoint,
                "cloud_endpoint_source": self._cloud_endpoint_source,
                "cloud_contract_verified": self._cloud_contract_verified,
                "cloud_contract_endpoint": self._cloud_contract_endpoint,
                "cloud_contract_last_checked": self._cloud_contract_last_checked,
                "cloud_contract_last_error": self._cloud_contract_last_error,
                "timestamp": time.time(),
                "timestamp_iso": datetime.now().isoformat(),
                "reason": reason,
                "source_repo": "jarvis",
                "pid": os.getpid(),
                "version": "v115.0",
            }

            # Atomic write with temp file
            tmp_file = state_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(state, indent=2))
            tmp_file.rename(state_file)

            logger.debug(f"[v115.0] Cross-repo ECAPA state written: ready={is_ready}")

        except Exception as e:
            logger.debug(f"[v115.0] Failed to write cross-repo ECAPA state: {e}")

    async def _read_cross_repo_ecapa_state(self) -> Optional[Dict[str, Any]]:
        """
        v115.0: Read Cloud ECAPA health state from cross-repo coordination file.

        If another repo (JARVIS Prime, Reactor Core) has recently verified
        Cloud ECAPA, we can skip verification and use their result.

        Returns:
            State dictionary if found and recent (< 60s), None otherwise
        """
        try:
            import json
            from pathlib import Path

            state_file = Path.home() / ".jarvis" / "cross_repo" / "cloud_ecapa_state.json"

            if not state_file.exists():
                return None

            state = json.loads(state_file.read_text())

            # Check if state is recent (< 60 seconds old)
            state_age = time.time() - state.get("timestamp", 0)
            max_age = float(os.getenv("JARVIS_ECAPA_STATE_MAX_AGE", "60.0"))

            if state_age > max_age:
                logger.debug(f"[v115.0] Cross-repo ECAPA state too old ({state_age:.1f}s > {max_age}s)")
                return None

            logger.info(f"[v115.0] Found recent cross-repo ECAPA state from {state.get('source_repo', 'unknown')} ({state_age:.1f}s ago)")
            return state

        except Exception as e:
            logger.debug(f"[v115.0] Failed to read cross-repo ECAPA state: {e}")
            return None

    async def _fallback_to_local_ecapa(self, reason: str) -> bool:
        """
        Fallback to local ECAPA loading when cloud is unavailable.

        Args:
            reason: Why we're falling back (for logging)

        Returns:
            True if local ECAPA was successfully loaded
        """
        logger.warning("=" * 70)
        logger.warning("🔄 CLOUD FALLBACK: Attempting local ECAPA load")
        logger.warning("=" * 70)
        logger.warning(f"   Reason: {reason}")
        logger.warning("   Warning: This may cause memory pressure!")
        logger.warning("=" * 70)

        # Disable cloud mode
        self._use_cloud = False
        self._cloud_verified = False

        # Check if ECAPA engine is registered
        if "ecapa_tdnn" not in self._engines:
            logger.error("❌ ECAPA engine not registered - cannot fallback")
            return False

        ecapa_engine = self._engines["ecapa_tdnn"]

        # Attempt to load ECAPA locally
        try:
            timeout = float(os.getenv("JARVIS_ECAPA_LOCAL_TIMEOUT", "60.0"))
            success = await ecapa_engine.load(timeout=timeout)

            if success:
                logger.info("✅ Local ECAPA loaded successfully as fallback")
                return True
            else:
                logger.error(f"❌ Local ECAPA load failed: {ecapa_engine.metrics.last_error}")
                return False

        except Exception as e:
            logger.error(f"❌ Local ECAPA fallback exception: {e}")
            return False

    async def _fallback_to_cloud(self, reason: str) -> bool:
        """
        Fallback to cloud ECAPA when local is unavailable (memory pressure, timeout, etc).

        This is the INVERSE of _fallback_to_local_ecapa. When local ECAPA loading
        fails (e.g., due to memory pressure on macOS), we attempt to use the
        GCP Cloud Run ECAPA service instead.

        Args:
            reason: Why we're falling back to cloud (for logging)

        Returns:
            True if cloud ECAPA was successfully verified and activated
        """
        logger.warning("=" * 70)
        logger.warning("🔄 LOCAL FALLBACK: Attempting cloud ECAPA activation")
        logger.warning("=" * 70)
        logger.warning(f"   Reason: {reason}")
        logger.warning("   Cloud Run service will handle ECAPA embedding extraction")
        logger.warning("=" * 70)

        endpoint_candidate = self._cloud_endpoint
        if not endpoint_candidate:
            endpoint_candidate = os.getenv(
                "JARVIS_CLOUD_ECAPA_ENDPOINT",
                os.getenv("JARVIS_ML_CLOUD_ENDPOINT", None),
            )

        if not endpoint_candidate:
            region = os.getenv("GCP_REGION", "us-central1")
            service_name = os.getenv("GCP_ECAPA_SERVICE", "jarvis-ml")
            logger.warning("   No cloud endpoint configured - checking for Cloud Run URL...")

            cloud_run_urls = [
                os.getenv("CLOUD_RUN_ECAPA_URL"),
                f"https://{service_name}-pvalxny6iq-uc.a.run.app",
                f"https://{service_name}-888774109345.{region}.run.app",
            ]

            for url in cloud_run_urls:
                if url:
                    endpoint_candidate = url
                    logger.info(f"   Trying cloud endpoint: {url}")
                    break

        if endpoint_candidate:
            self._set_cloud_endpoint(endpoint_candidate, "fallback_to_cloud")

        if not self._cloud_endpoint:
            logger.error("❌ No cloud endpoint available for fallback")
            logger.error("   Set JARVIS_CLOUD_ECAPA_ENDPOINT environment variable")
            return False

        logger.info(f"   Cloud endpoint: {self._cloud_endpoint}")

        # Enable cloud mode
        self._use_cloud = True
        self._cloud_verified = False

        # Verify the cloud backend is actually ready
        try:
            cloud_ready, verify_msg = await self._verify_cloud_backend_ready(
                timeout=float(os.getenv("JARVIS_ECAPA_CLOUD_TIMEOUT", "15.0")),
                retry_count=int(os.getenv("JARVIS_ECAPA_CLOUD_RETRIES", "3")),
                test_extraction=True  # Always test extraction for fallback
            )

            if cloud_ready:
                logger.info("=" * 70)
                logger.info("✅ CLOUD FALLBACK SUCCESS: Cloud ECAPA activated")
                logger.info("=" * 70)
                logger.info(f"   Verification: {verify_msg}")
                logger.info(f"   Endpoint: {self._cloud_endpoint}")
                logger.info("   All ECAPA operations will use cloud backend")
                logger.info("=" * 70)
                return True
            else:
                logger.error("=" * 70)
                logger.error("❌ CLOUD FALLBACK FAILED: Cloud verification failed")
                logger.error("=" * 70)
                logger.error(f"   Reason: {verify_msg}")
                logger.error("   Voice unlock will not work until ECAPA is available")
                logger.error("=" * 70)
                self._use_cloud = False
                self._cloud_verified = False
                return False

        except Exception as e:
            logger.error(f"❌ Cloud fallback exception: {e}")
            import traceback
            logger.error(traceback.format_exc())
            self._use_cloud = False
            self._cloud_verified = False
            return False

    def get_ecapa_status(self) -> Dict[str, Any]:
        """
        Get detailed ECAPA encoder availability status.

        Returns:
            Dict with availability information from all sources
        """
        status = {
            "available": False,
            "source": None,
            "cloud_mode": self._use_cloud,
            "cloud_verified": getattr(self, "_cloud_verified", False),
            "cloud_endpoint": self._cloud_endpoint,
            "cloud_endpoint_source": self._cloud_endpoint_source,
            "local_loaded": False,
            "local_error": None,
            "diagnostics": {}
        }

        # Check local ECAPA
        if "ecapa_tdnn" in self._engines:
            ecapa = self._engines["ecapa_tdnn"]
            status["local_loaded"] = ecapa.is_loaded
            status["local_error"] = ecapa.metrics.last_error
            status["diagnostics"]["local_state"] = ecapa.metrics.state.name

            if ecapa.is_loaded:
                status["available"] = True
                status["source"] = "local"

        # Check cloud
        if self._use_cloud and getattr(self, "_cloud_verified", False):
            status["available"] = True
            status["source"] = "cloud" if not status["local_loaded"] else "local_preferred"
            status["diagnostics"]["cloud_last_verified"] = getattr(self, "_cloud_last_verified", None)

        # v21.1.0: Include cloud circuit breaker state
        if hasattr(self, '_cloud_embedding_cb'):
            status["diagnostics"]["cloud_circuit_breaker"] = self._cloud_embedding_cb.to_dict()
        status["diagnostics"]["cloud_api_failure_streak"] = self._cloud_api_failure_streak
        status["diagnostics"]["cloud_api_degraded_until"] = self._cloud_api_degraded_until
        status["diagnostics"]["cloud_api_last_error"] = self._cloud_api_last_error
        status["diagnostics"]["cloud_api_cooldown_remaining"] = self._cloud_api_cooldown_remaining()
        status["diagnostics"]["cloud_contract_verified"] = self._cloud_contract_verified
        status["diagnostics"]["cloud_contract_endpoint"] = self._cloud_contract_endpoint
        status["diagnostics"]["cloud_contract_last_checked"] = self._cloud_contract_last_checked
        status["diagnostics"]["cloud_contract_last_error"] = self._cloud_contract_last_error

        # Final determination
        if not status["available"]:
            status["error"] = "No ECAPA encoder available (local not loaded, cloud not verified)"

        return status

    def _cloud_api_cooldown_remaining(self) -> float:
        """Seconds remaining before cloud API calls should be retried."""
        return max(0.0, self._cloud_api_degraded_until - time.time())

    def _parse_retry_after_seconds(self, retry_after: Optional[str]) -> float:
        """Parse Retry-After header value safely."""
        if not retry_after:
            return 0.0
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            return 0.0

    def _apply_cloud_retry_after(self, retry_after_seconds: float, reason: str = "") -> None:
        """
        Apply server-provided cooldown without counting it as a hard API failure.
        """
        if retry_after_seconds <= 0:
            return
        self._cloud_api_degraded_until = max(
            self._cloud_api_degraded_until,
            time.time() + retry_after_seconds,
        )
        if reason:
            self._cloud_api_last_error = reason[:240]

    def _mark_cloud_api_failure(
        self,
        reason: str,
        status_code: Optional[int] = None,
        retry_after_seconds: float = 0.0,
    ) -> None:
        """
        Record a hard cloud API failure and enter degraded cooldown mode.
        """
        now = time.time()
        if (
            self._cloud_api_last_failure_at
            and (now - self._cloud_api_last_failure_at) > MLConfig.CLOUD_API_FAILURE_STREAK_RESET
        ):
            self._cloud_api_failure_streak = 0

        self._cloud_api_failure_streak += 1
        backoff = min(
            MLConfig.CLOUD_API_FAILURE_BACKOFF_BASE * (2 ** max(0, self._cloud_api_failure_streak - 1)),
            MLConfig.CLOUD_API_FAILURE_BACKOFF_MAX,
        )
        if retry_after_seconds > 0:
            backoff = max(backoff, retry_after_seconds)

        self._cloud_api_degraded_until = max(self._cloud_api_degraded_until, now + backoff)
        self._cloud_api_last_failure_at = now
        if status_code is None:
            self._cloud_api_last_error = reason[:240]
        else:
            self._cloud_api_last_error = f"HTTP {status_code}: {reason[:200]}"

        self._cloud_verified = False

    def _mark_cloud_api_success(self) -> None:
        """Clear degraded cloud API state after a successful API request."""
        self._cloud_api_failure_streak = 0
        self._cloud_api_last_failure_at = 0.0
        self._cloud_api_degraded_until = 0.0
        self._cloud_api_last_error = ""
        self._cloud_api_last_cooldown_log_at = 0.0
        self._cloud_verified = True
        self._cloud_last_verified = time.time()

    def _log_cloud_cooldown(self, context: str) -> None:
        """Rate-limited cooldown log to avoid warning spam."""
        remaining = self._cloud_api_cooldown_remaining()
        if remaining <= 0:
            return

        msg = (
            f"{context}; cooldown {remaining:.0f}s, "
            f"failure_streak={self._cloud_api_failure_streak}, "
            f"last_error={self._cloud_api_last_error or 'n/a'}, "
            f"endpoint={self._cloud_endpoint or 'unset'}, "
            f"source={self._cloud_endpoint_source}"
        )
        now = time.time()
        if (now - self._cloud_api_last_cooldown_log_at) >= MLConfig.CLOUD_COOLDOWN_LOG_INTERVAL:
            logger.warning(msg)
            self._cloud_api_last_cooldown_log_at = now
        else:
            logger.debug(msg)

    async def _check_cloud_readiness(self) -> bool:
        """
        v21.1.0: Quick non-blocking readiness check before cloud requests.

        Checks _cloud_verified flag and re-verifies via a fast health check
        if the verification is stale. Returns True if cloud is ready.
        """
        if not self._cloud_endpoint:
            return False

        if self._cloud_api_cooldown_remaining() > 0:
            self._log_cloud_cooldown("Cloud API in degraded state, skipping readiness check")
            return False

        cloud_verified = self._cloud_verified
        cloud_last_verified = self._cloud_last_verified
        verify_ttl = float(os.getenv("JARVIS_CLOUD_VERIFY_TTL", "300"))
        verification_stale = (
            (time.time() - cloud_last_verified) > verify_ttl
            if cloud_last_verified else True
        )

        if cloud_verified and not verification_stale:
            return True

        # Serialize health probes to prevent request stampede during failures.
        async with self._cloud_readiness_probe_lock:
            if self._cloud_api_cooldown_remaining() > 0:
                self._log_cloud_cooldown("Cloud API in degraded state (post-lock), skipping readiness check")
                return False

            contract_ok, contract_reason = await self._verify_cloud_endpoint_contract(
                timeout=float(os.getenv("JARVIS_CLOUD_CONTRACT_TIMEOUT", "4.0")),
                force=False,
            )
            if not contract_ok:
                self._mark_cloud_api_failure(f"Contract validation failed: {contract_reason}")
                self._log_cloud_cooldown("Cloud endpoint contract invalid")
                return False

            cloud_verified = self._cloud_verified
            cloud_last_verified = self._cloud_last_verified
            verification_stale = (
                (time.time() - cloud_last_verified) > verify_ttl
                if cloud_last_verified else True
            )
            if cloud_verified and not verification_stale:
                return True

            # Quick single-shot health check (not the full polling verifier)
            quick_timeout = float(os.getenv("JARVIS_CLOUD_QUICK_HEALTH_TIMEOUT", "3.0"))
            try:
                import aiohttp
                health_url = f"{self._cloud_endpoint.rstrip('/')}/api/ml/health"
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        health_url,
                        timeout=aiohttp.ClientTimeout(total=quick_timeout),
                        headers={"Accept": "application/json"},
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            ecapa_ready = data.get("ecapa_ready")
                            if isinstance(ecapa_ready, bool) and ecapa_ready:
                                self._cloud_verified = True
                                self._cloud_last_verified = time.time()
                                logger.debug("Cloud ECAPA re-verified via quick health check")
                                return True

                            self._cloud_verified = False
                            status = data.get("status", data.get("startup_state", "unknown"))
                            retry_after = self._parse_retry_after_seconds(
                                response.headers.get("Retry-After")
                            )
                            if retry_after <= 0:
                                retry_after = float(
                                    os.getenv("JARVIS_CLOUD_NOT_READY_COOLDOWN_SECONDS", "5.0")
                                )
                            self._apply_cloud_retry_after(
                                retry_after,
                                reason=f"health status={status}",
                            )
                            logger.warning(
                                f"Cloud ECAPA not ready (status: {status}, endpoint={self._cloud_endpoint}), "
                                "skipping cloud request"
                            )
                            return False

                        if response.status >= 500:
                            error_text = await response.text()
                            self._mark_cloud_api_failure(
                                reason=f"Health check failed: {error_text[:120]}",
                                status_code=response.status,
                            )
                            self._log_cloud_cooldown("Cloud health endpoint failed")
                            return False

                        retry_after = self._parse_retry_after_seconds(
                            response.headers.get("Retry-After")
                        )
                        if retry_after <= 0 and response.status in (408, 425, 429):
                            retry_after = float(
                                os.getenv("JARVIS_CLOUD_NOT_READY_COOLDOWN_SECONDS", "5.0")
                            )
                        self._apply_cloud_retry_after(
                            retry_after,
                            reason=f"health HTTP {response.status}",
                        )
                        logger.warning(
                            f"Cloud health check returned {response.status} for {self._cloud_endpoint}, "
                            "skipping cloud request"
                        )
                        return False
            except Exception as e:
                self._mark_cloud_api_failure(f"Health check exception: {e}")
                self._log_cloud_cooldown("Quick cloud health check failed")
                return False

    async def extract_speaker_embedding_cloud(
        self,
        audio_data: bytes,
        timeout: float = 30.0
    ) -> Optional[Any]:
        """
        Extract speaker embedding using cloud backend.

        v21.1.0: Added readiness gate, circuit breaker, and 503 handling.

        Args:
            audio_data: Raw audio bytes (16kHz, mono, float32)
            timeout: Request timeout in seconds

        Returns:
            Embedding tensor or None if failed
        """
        if not self._cloud_endpoint:
            logger.error("Cloud endpoint not configured")
            return None

        # v21.1.0: Readiness gate - verify cloud is ready before sending
        if not await self._check_cloud_readiness():
            return None

        # v21.1.0: Circuit breaker check
        can_exec, cb_reason = self._cloud_embedding_cb.can_execute()
        if not can_exec:
            logger.warning(f"Cloud embedding circuit breaker OPEN: {cb_reason}")
            return None

        try:
            import aiohttp
            import base64
            import numpy as np
            strict_contract = os.getenv("JARVIS_CLOUD_STRICT_CONTRACT", "true").lower() in (
                "1",
                "true",
                "yes",
            )

            # Encode audio as base64
            audio_b64 = base64.b64encode(audio_data).decode('utf-8')

            endpoint = f"{self._cloud_endpoint.rstrip('/')}/api/ml/speaker_embedding"
            payload = {
                "audio_data": audio_b64,
                "sample_rate": 16000,
                "format": "float32",
            }

            logger.debug(f"Sending speaker embedding request to cloud: {endpoint}")

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    if response.status == 200:
                        result = await response.json()

                        if result.get("success") and result.get("embedding"):
                            # Convert embedding back to numpy/tensor
                            embedding_list = result["embedding"]
                            embedding = np.array(embedding_list, dtype=np.float32)

                            # CRITICAL: Validate for NaN values
                            if np.any(np.isnan(embedding)):
                                logger.error("❌ Cloud embedding contains NaN values!")
                                self._cloud_embedding_cb.record_failure("NaN in embedding")
                                return None

                            import torch
                            embedding_tensor = torch.tensor(embedding).unsqueeze(0)

                            logger.debug(f"Cloud embedding received: shape {embedding_tensor.shape}")
                            self._cloud_embedding_cb.record_success()
                            self._mark_cloud_api_success()
                            # v251.2: Reset fallback warning flag on success
                            # so it fires again if cloud goes down later
                            global _cloud_fallback_warned
                            _cloud_fallback_warned = False
                            return embedding_tensor
                        else:
                            error_msg = result.get('error', 'unknown')
                            logger.error(f"Cloud embedding failed: {error_msg}")
                            self._cloud_embedding_cb.record_failure(f"API error: {error_msg}")
                            self._mark_cloud_api_failure(f"API error: {error_msg}")
                            return None

                    elif response.status == 503:
                        # v21.1.0: Server not ready - parse structured 503 response
                        retry_after = response.headers.get("Retry-After")
                        try:
                            body = await response.json()
                            detail = body.get("detail", body.get("error", "unknown"))
                            startup_state = body.get("startup_state", "")
                        except Exception:
                            detail = await response.text()
                            startup_state = ""

                        logger.warning(
                            f"Cloud ECAPA not ready (503): {detail}"
                            + (f", retry after {retry_after}s" if retry_after else "")
                        )

                        # Invalidate verification since server is not ready
                        self._cloud_verified = False
                        retry_after_seconds = self._parse_retry_after_seconds(retry_after)
                        self._apply_cloud_retry_after(
                            retry_after_seconds,
                            reason=f"503: {detail}",
                        )

                        # Only trip circuit breaker for permanent failures,
                        # not transient init states
                        if startup_state in ("failed", "degraded"):
                            self._cloud_embedding_cb.record_failure(f"503: {detail}")
                            self._mark_cloud_api_failure(
                                reason=detail,
                                status_code=503,
                                retry_after_seconds=retry_after_seconds,
                            )

                        return None

                    else:
                        error_text = await response.text()
                        if response.status in (404, 405):
                            reason = (
                                f"Embedding route unavailable (HTTP {response.status}) "
                                f"at {endpoint}"
                            )
                            self._cloud_contract_verified = False
                            self._cloud_contract_endpoint = self._cloud_endpoint.rstrip("/")
                            self._cloud_contract_last_checked = time.time()
                            self._cloud_contract_last_error = reason[:240]
                            self._cloud_verified = False
                            if strict_contract:
                                self._use_cloud = False
                            logger.warning(reason)
                            self._cloud_embedding_cb.record_failure(reason[:100])
                            return None

                        logger.warning(
                            f"Cloud embedding request failed ({response.status}) on "
                            f"{endpoint} (source={self._cloud_endpoint_source}): {error_text[:200]}"
                        )
                        self._cloud_embedding_cb.record_failure(
                            f"HTTP {response.status}: {error_text[:100]}"
                        )
                        # v3.5: Invalidate readiness cache on server errors
                        # (health says "ready" but API returns 500).
                        # Forces re-verification via _check_cloud_readiness()
                        # before the next circuit breaker probe attempt.
                        if response.status >= 500:
                            self._mark_cloud_api_failure(
                                reason=error_text[:160],
                                status_code=response.status,
                            )
                            self._log_cloud_cooldown("Cloud embedding API hard failure")
                        else:
                            self._cloud_verified = False
                        return None

        except ImportError:
            logger.error("aiohttp not available for cloud requests")
            return None
        except asyncio.TimeoutError:
            logger.error(
                f"Cloud embedding request timed out ({timeout}s) at "
                f"{self._cloud_endpoint} (source={self._cloud_endpoint_source})"
            )
            self._cloud_embedding_cb.record_failure(f"Timeout after {timeout}s")
            self._mark_cloud_api_failure(f"Timeout after {timeout}s")
            return None
        except Exception as e:
            logger.error(
                f"Cloud embedding request failed at {self._cloud_endpoint} "
                f"(source={self._cloud_endpoint_source}): {e}"
            )
            self._cloud_embedding_cb.record_failure(str(e)[:100])
            self._mark_cloud_api_failure(str(e)[:100])
            return None

    async def verify_speaker_cloud(
        self,
        audio_data: bytes,
        reference_embedding: Any,
        timeout: float = 30.0
    ) -> Optional[Dict[str, Any]]:
        """
        Verify speaker using cloud backend.

        v21.1.0: Added readiness gate, circuit breaker, and 503 handling.

        Args:
            audio_data: Raw audio bytes (16kHz, mono, float32)
            reference_embedding: Reference embedding to compare against
            timeout: Request timeout in seconds

        Returns:
            Dict with verification result or None if failed
        """
        if not self._cloud_endpoint:
            logger.error("Cloud endpoint not configured")
            return None

        # v21.1.0: Readiness gate - verify cloud is ready before sending
        if not await self._check_cloud_readiness():
            return None

        # v21.1.0: Circuit breaker check
        can_exec, cb_reason = self._cloud_embedding_cb.can_execute()
        if not can_exec:
            logger.warning(f"Cloud verification circuit breaker OPEN: {cb_reason}")
            return None

        try:
            import aiohttp
            import base64
            import numpy as np
            strict_contract = os.getenv("JARVIS_CLOUD_STRICT_CONTRACT", "true").lower() in (
                "1",
                "true",
                "yes",
            )

            # Encode audio as base64
            audio_b64 = base64.b64encode(audio_data).decode('utf-8')

            # Convert reference embedding to list
            if hasattr(reference_embedding, 'cpu'):
                ref_list = reference_embedding.cpu().numpy().tolist()
            elif hasattr(reference_embedding, 'tolist'):
                ref_list = reference_embedding.tolist()
            else:
                ref_list = list(reference_embedding)

            endpoint = f"{self._cloud_endpoint.rstrip('/')}/api/ml/speaker_verify"
            payload = {
                "audio_data": audio_b64,
                "reference_embedding": ref_list,
                "sample_rate": 16000,
                "format": "float32",
            }

            logger.debug(f"Sending speaker verification request to cloud: {endpoint}")

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        logger.debug(f"Cloud verification result: {result}")
                        self._cloud_embedding_cb.record_success()
                        self._mark_cloud_api_success()
                        return result

                    elif response.status == 503:
                        # v21.1.0: Server not ready - handle structured 503
                        try:
                            body = await response.json()
                            detail = body.get("detail", body.get("error", "unknown"))
                            startup_state = body.get("startup_state", "")
                        except Exception:
                            detail = await response.text()
                            startup_state = ""

                        retry_after = response.headers.get("Retry-After")
                        logger.warning(
                            f"Cloud verification not ready (503): {detail}"
                            + (f", retry after {retry_after}s" if retry_after else "")
                        )
                        self._cloud_verified = False
                        retry_after_seconds = self._parse_retry_after_seconds(retry_after)
                        self._apply_cloud_retry_after(
                            retry_after_seconds,
                            reason=f"503: {detail}",
                        )

                        if startup_state in ("failed", "degraded"):
                            self._cloud_embedding_cb.record_failure(f"503: {detail}")
                            self._mark_cloud_api_failure(
                                reason=detail,
                                status_code=503,
                                retry_after_seconds=retry_after_seconds,
                            )
                        return None

                    else:
                        error_text = await response.text()
                        if response.status in (404, 405):
                            reason = (
                                f"Speaker verification route unavailable (HTTP {response.status}) "
                                f"at {endpoint}"
                            )
                            self._cloud_contract_verified = False
                            self._cloud_contract_endpoint = self._cloud_endpoint.rstrip("/")
                            self._cloud_contract_last_checked = time.time()
                            self._cloud_contract_last_error = reason[:240]
                            self._cloud_verified = False
                            if strict_contract:
                                self._use_cloud = False
                            logger.warning(reason)
                            self._cloud_embedding_cb.record_failure(reason[:100])
                            return None

                        logger.warning(
                            f"Cloud verification failed ({response.status}) on "
                            f"{endpoint} (source={self._cloud_endpoint_source}): {error_text[:200]}"
                        )
                        self._cloud_embedding_cb.record_failure(
                            f"HTTP {response.status}: {error_text[:100]}"
                        )
                        # v3.5: Invalidate readiness cache on server errors
                        if response.status >= 500:
                            self._mark_cloud_api_failure(
                                reason=error_text[:160],
                                status_code=response.status,
                            )
                            self._log_cloud_cooldown("Cloud verification API hard failure")
                        else:
                            self._cloud_verified = False
                        return None

        except ImportError:
            logger.error("aiohttp not available for cloud requests")
            return None
        except asyncio.TimeoutError:
            logger.error(
                f"Cloud verification request timed out ({timeout}s) at "
                f"{self._cloud_endpoint} (source={self._cloud_endpoint_source})"
            )
            self._cloud_embedding_cb.record_failure(f"Timeout after {timeout}s")
            self._mark_cloud_api_failure(f"Timeout after {timeout}s")
            return None
        except Exception as e:
            logger.error(
                f"Cloud verification request failed at {self._cloud_endpoint} "
                f"(source={self._cloud_endpoint_source}): {e}"
            )
            self._cloud_embedding_cb.record_failure(str(e)[:100])
            self._mark_cloud_api_failure(str(e)[:100])
            return None

    def set_cloud_endpoint(self, endpoint: str) -> None:
        """
        Manually set the cloud endpoint.

        Args:
            endpoint: Cloud ML API endpoint URL
        """
        self._set_cloud_endpoint(endpoint, "manual")
        self._use_cloud = True
        logger.info(f"☁️  Cloud endpoint set to: {self._cloud_endpoint}")

    async def switch_to_cloud(self, reason: str = "Manual switch") -> bool:
        """
        Switch from local to cloud processing.

        Args:
            reason: Reason for switching to cloud

        Returns:
            True if switch was successful
        """
        logger.info(f"☁️  Switching to cloud ML: {reason}")
        return await self._activate_cloud_routing()

    async def switch_to_local(self, reason: str = "Manual switch") -> bool:
        """
        Switch from cloud to local processing.

        Requires local engines to be loaded.

        Args:
            reason: Reason for switching to local

        Returns:
            True if switch was successful
        """
        if not self.is_ready:
            logger.warning("Cannot switch to local - engines not loaded")
            return False

        logger.info(f"🏠 Switching to local ML: {reason}")
        self._use_cloud = False
        return True

    async def activate_cloud_routing(self) -> bool:
        """
        Public method to activate cloud routing for ML operations.
        
        Called by process_cleanup_manager during memory pressure to offload
        heavy ML models to GCP Cloud Run.
        
        Returns:
            True if cloud routing was successfully activated
        """
        logger.info("☁️  [PUBLIC] activate_cloud_routing called by cleanup manager")
        return await self._activate_cloud_routing()

    async def unload_local_models(self) -> int:
        """
        Unload local ML models to free memory.
        
        Called by process_cleanup_manager during high memory pressure
        to free RAM by releasing locally loaded models.
        
        Uses the proper async unload() method of each MLEngineWrapper to:
        1. Wait for active users to complete their operations
        2. Safely clear engine references
        3. Update state tracking
        
        Returns:
            Number of models unloaded
        """
        unloaded_count = 0
        
        try:
            logger.info("☁️  [UNLOAD] Starting local model unload...")
            
            # Process all engines in parallel for faster unloading
            unload_tasks = []
            engine_names = []
            
            for engine_name, wrapper in list(self._engines.items()):
                # Check if engine is loaded using is_loaded property (thread-safe)
                if wrapper.is_loaded:
                    engine_names.append(engine_name)
                    # Use the wrapper's async unload method for proper cleanup
                    unload_tasks.append(wrapper.unload(timeout=10.0))
            
            if unload_tasks:
                # Execute all unloads in parallel
                results = await asyncio.gather(*unload_tasks, return_exceptions=True)
                
                for engine_name, result in zip(engine_names, results):
                    if isinstance(result, Exception):
                        logger.warning(f"☁️  [UNLOAD] Failed to unload {engine_name}: {result}")
                    else:
                        unloaded_count += 1
                        logger.info(f"☁️  [UNLOAD] Unloaded {engine_name}")
            else:
                logger.info("☁️  [UNLOAD] No local models loaded to unload")
            
            # Force garbage collection to actually free memory
            import gc
            gc.collect()
            
            # If using PyTorch, clear CUDA cache (safe to call even if not using CUDA)
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                # Also clear MPS cache on Apple Silicon
                if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                    torch.mps.empty_cache()
            except Exception:
                pass  # PyTorch not available or no GPU
            
            logger.info(f"☁️  [UNLOAD] Unloaded {unloaded_count} local models, GC completed")
            
        except Exception as e:
            logger.error(f"☁️  [UNLOAD] Error during model unload: {e}")
        
        return unloaded_count


# =============================================================================
# GLOBAL ACCESS FUNCTIONS
# =============================================================================

_registry: Optional[MLEngineRegistry] = None
_registry_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_ml_registry() -> MLEngineRegistry:
    """
    Get or create the global ML Engine Registry.

    Usage:
        registry = await get_ml_registry()
        await registry.prewarm_all_blocking()
    """
    global _registry

    if _registry is None:
        async with _registry_lock:
            if _registry is None:
                _registry = MLEngineRegistry()

    return _registry


_sync_registry_lock = threading.Lock()


def get_ml_registry_sync(auto_create: bool = True) -> Optional[MLEngineRegistry]:
    """
    Get the registry synchronously with optional auto-creation.

    CRITICAL FIX v2.0: Now auto-creates registry if not initialized.
    This ensures voice unlock components can always access the registry,
    even if main.py startup didn't initialize it first.

    Args:
        auto_create: If True (default), creates registry if it doesn't exist.
                    Set to False to only return existing registry.

    Use this in sync code paths where you can't await.

    Thread Safety:
        Uses a threading.Lock to prevent race conditions during creation.
    """
    global _registry

    if _registry is not None:
        return _registry

    if not auto_create:
        return None

    # Thread-safe lazy initialization
    with _sync_registry_lock:
        # Double-check pattern
        if _registry is None:
            logger.info("🔧 [SYNC] Auto-creating ML Engine Registry (lazy init)")
            _registry = MLEngineRegistry()
            logger.info("✅ [SYNC] ML Engine Registry created successfully")

    return _registry


async def prewarm_voice_unlock_models_blocking() -> RegistryStatus:
    """
    Convenience function to prewarm all voice unlock models.

    BLOCKS until all models are loaded.
    Call this at startup in main.py.
    """
    registry = await get_ml_registry()
    return await registry.prewarm_all_blocking()


def prewarm_voice_unlock_models_background(
    startup_decision: Optional[Any] = None,
    on_complete: Optional[Callable[[RegistryStatus], None]] = None,
) -> asyncio.Task:
    """
    Launch voice unlock model prewarm as a BACKGROUND TASK (non-blocking).

    This function returns IMMEDIATELY and does NOT block FastAPI startup.
    Models load in the background while the server accepts requests.

    Use this instead of prewarm_voice_unlock_models_blocking() in main.py
    to ensure FastAPI can respond to health checks during model loading.

    Args:
        startup_decision: Optional StartupDecision from MemoryAwareStartup
        on_complete: Optional callback when prewarm completes

    Returns:
        asyncio.Task that can be awaited later if needed

    Example:
        # In main.py lifespan:
        prewarm_task = prewarm_voice_unlock_models_background()
        # FastAPI starts immediately, models load in background
        # The task runs async, no blocking
    """
    global _registry

    # Get or create registry synchronously (must already be initialized)
    registry = _registry
    if registry is None:
        # Create synchronously for background launch
        registry = MLEngineRegistry()
        _registry = registry

    # Launch background prewarm
    return registry.prewarm_background(
        parallel=True,
        startup_decision=startup_decision,
        on_complete=on_complete,
    )


async def ensure_ecapa_available(
    timeout: float = MLConfig.MODEL_LOAD_TIMEOUT,  # v78.1: Use configured timeout (120s default)
    allow_cloud: bool = True,
) -> Tuple[bool, str, Optional[Any]]:
    """
    CRITICAL FIX v2.0: Ensures ECAPA-TDNN is available for voice verification.

    This function MUST be called before any voice verification attempt.
    It ensures ECAPA is loaded (either locally or via cloud).

    Orchestration Flow:
    1. Get or create ML Registry (lazy init)
    2. Check if ECAPA is already loaded → return immediately
    3. If cloud mode: verify cloud backend is ready
    4. If local mode: trigger ECAPA loading
    5. Wait for ECAPA to be available (with timeout)

    Args:
        timeout: Maximum seconds to wait for ECAPA to load
        allow_cloud: If True, cloud mode is acceptable

    Returns:
        Tuple[bool, str, Optional[encoder]]:
        - success: True if ECAPA is available
        - message: Status/error message
        - encoder: The ECAPA encoder if available locally (None for cloud mode)

    Usage:
        success, message, encoder = await ensure_ecapa_available()
        if not success:
            return {"error": f"Voice verification unavailable: {message}"}
    """
    global _registry

    start_time = time.time()
    logger.info("🔍 [ENSURE_ECAPA] Starting ECAPA availability check...")

    # Step 1: Get or create registry
    registry = get_ml_registry_sync(auto_create=True)
    if registry is None:
        return False, "Failed to create ML Engine Registry", None

    # Step 2: Check if already in cloud mode with verified backend
    if registry.is_using_cloud:
        cloud_verified = getattr(registry, '_cloud_verified', False)
        if cloud_verified:
            logger.info("✅ [ENSURE_ECAPA] Cloud mode active and verified")
            return True, "Cloud ECAPA available", None
        else:
            # v236.1: Only attempt verification if an endpoint is actually configured.
            # _activate_cloud_routing() should guarantee this, but guard defensively
            # to prevent spurious "Cloud endpoint not configured" warnings.
            _has_endpoint = bool(getattr(registry, '_cloud_endpoint', None))
            if _has_endpoint and hasattr(registry, '_verify_cloud_backend_ready'):
                # Cloud mode but not verified - try to verify
                logger.info("🔄 [ENSURE_ECAPA] Cloud mode active but not verified, checking...")
                verified, verify_msg = await registry._verify_cloud_backend_ready(
                    timeout=min(10.0, timeout / 2),
                    test_extraction=True,
                )
                if verified:
                    logger.info("✅ [ENSURE_ECAPA] Cloud backend verified successfully")
                    return True, "Cloud ECAPA verified and available", None
                else:
                    logger.warning(f"⚠️ [ENSURE_ECAPA] Cloud verification failed: {verify_msg}")
                    # Fall through to try local loading
            elif not _has_endpoint:
                logger.info(
                    "🔄 [ENSURE_ECAPA] Cloud mode flag set but no endpoint configured "
                    "— falling through to local loading"
                )
                # Fall through to try local loading

    # Step 3: Check if ECAPA engine is already loaded locally
    # Use get_wrapper() which is safe (doesn't throw)
    ecapa_wrapper = registry.get_wrapper("ecapa_tdnn")
    if ecapa_wrapper and ecapa_wrapper.is_loaded:
        logger.info("✅ [ENSURE_ECAPA] Local ECAPA already loaded")
        return True, "Local ECAPA available", ecapa_wrapper.get_engine()

    # Step 4: Need to load ECAPA - trigger prewarm if not already running
    logger.info("🔄 [ENSURE_ECAPA] ECAPA not loaded, triggering load...")

    # Check if prewarm is already running
    if registry.is_warming_up:
        logger.info("   Prewarm already in progress, waiting...")
    else:
        # Trigger ECAPA load specifically
        if ecapa_wrapper:
            try:
                # Load ECAPA engine directly
                load_task = asyncio.create_task(ecapa_wrapper.load())
                # Don't await here, we'll wait in the loop below
            except Exception as e:
                logger.warning(f"   Failed to trigger ECAPA load: {e}")

    # Step 5: Wait for ECAPA to become available
    poll_interval = 0.5
    while (time.time() - start_time) < timeout:
        # Check local ECAPA (use get_wrapper for safe access)
        ecapa_wrapper = registry.get_wrapper("ecapa_tdnn")
        if ecapa_wrapper and ecapa_wrapper.is_loaded:
            elapsed = time.time() - start_time
            logger.info(f"✅ [ENSURE_ECAPA] ECAPA loaded successfully in {elapsed:.1f}s")
            return True, f"Local ECAPA loaded in {elapsed:.1f}s", ecapa_wrapper.get_engine()

        # Check if cloud mode became available
        if registry.is_using_cloud and getattr(registry, '_cloud_verified', False):
            elapsed = time.time() - start_time
            logger.info(f"✅ [ENSURE_ECAPA] Cloud ECAPA became available in {elapsed:.1f}s")
            return True, f"Cloud ECAPA available in {elapsed:.1f}s", None

        await asyncio.sleep(poll_interval)

    # Timeout reached
    elapsed = time.time() - start_time
    error_msg = f"ECAPA load timeout after {elapsed:.1f}s"
    logger.error(f"❌ [ENSURE_ECAPA] {error_msg}")

    # Last resort: check cloud if allowed
    if allow_cloud and not registry.is_using_cloud:
        logger.info("🔄 [ENSURE_ECAPA] Trying cloud fallback...")
        if hasattr(registry, '_fallback_to_cloud'):
            fallback_ok = await registry._fallback_to_cloud("Local ECAPA timeout")
            if fallback_ok:
                return True, "Fell back to cloud ECAPA", None

    return False, error_msg, None


def get_ml_warmup_status() -> Dict[str, Any]:
    """
    Get current ML warmup status for health checks.

    Returns dict with:
    - is_warming_up: True if prewarm is in progress
    - is_ready: True if all critical engines are ready
    - progress: 0.0 to 1.0 warmup progress
    - current_engine: Name of engine currently loading
    - status_message: Human-readable status string

    Example:
        status = get_ml_warmup_status()
        if status["is_warming_up"]:
            return {"status": "warming_up", "progress": status["progress"]}
    """
    if _registry is None:
        return {
            "is_warming_up": False,
            "is_ready": False,
            "progress": 0.0,
            "current_engine": None,
            "status_message": "ML Registry not initialized",
            "engines_completed": 0,
            "engines_total": 0,
        }

    return _registry.warmup_status


def is_ml_warming_up() -> bool:
    """
    Quick check if ML models are currently warming up.

    Use this in health checks to return appropriate status during warmup.
    """
    if _registry is None:
        return False
    return _registry.is_warming_up


def is_voice_unlock_ready() -> bool:
    """
    Quick check if voice unlock is ready.

    Use this at the start of unlock request handlers.
    """
    if _registry is None:
        return False
    return _registry.is_ready


async def wait_for_voice_unlock_ready(timeout: float = 60.0) -> bool:
    """
    Wait for voice unlock models (ECAPA-TDNN only) to be ready.

    This is different from wait_until_ready() which waits for ALL engines.
    Voice unlock only needs ECAPA-TDNN, not STT engines.

    Returns True if ready, False if timeout.
    """
    registry = await get_ml_registry()

    # Check if already ready
    if registry.is_voice_unlock_ready:
        return True

    # Poll with timeout
    start_time = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start_time < timeout:
        if registry.is_voice_unlock_ready:
            return True
        await asyncio.sleep(0.1)

    logger.warning(f"⏱️ Timeout waiting for voice unlock engines ({timeout}s)")
    return False


# =============================================================================
# READINESS DECORATOR
# =============================================================================

def require_ml_ready(timeout: float = 30.0):
    """
    Decorator that ensures ML engines are ready before running a function.

    Usage:
        @require_ml_ready(timeout=10.0)
        async def handle_unlock(audio_data: bytes):
            ...
    """
    def decorator(func):
        async def wrapper(*args, **kwargs):
            if not is_voice_unlock_ready():
                ready = await wait_for_voice_unlock_ready(timeout)
                if not ready:
                    raise RuntimeError(
                        "Voice unlock models not ready. "
                        "Please wait for startup to complete."
                    )
            return await func(*args, **kwargs)
        return wrapper
    return decorator


# =============================================================================
# SPEAKER EMBEDDING FUNCTIONS - Hybrid Local/Cloud with Automatic Fallback
# =============================================================================

# v251.3: Module-level flag for cloud→local fallback log-once pattern.
# Reset on cloud success so the WARNING fires again if cloud re-fails.
_cloud_fallback_warned: bool = False

async def extract_speaker_embedding(
    audio_data: bytes,
    prefer_cloud: Optional[bool] = None,
    fallback_enabled: bool = True,
) -> Optional[Any]:
    """
    Extract speaker embedding using the best available method.

    HYBRID CLOUD INTEGRATION:
    - Automatically routes to cloud if memory pressure is high
    - Falls back to cloud if local extraction fails
    - Can be forced to cloud or local via prefer_cloud parameter

    Args:
        audio_data: Raw audio bytes (16kHz, mono, float32)
        prefer_cloud: Force cloud (True), force local (False), or auto (None)
        fallback_enabled: Allow fallback to cloud if local fails

    Returns:
        Embedding tensor or None if all methods fail
    """
    registry = await get_ml_registry()

    # Determine routing: cloud, local, or auto
    use_cloud = prefer_cloud if prefer_cloud is not None else registry.is_using_cloud

    # ==========================================================================
    # CLOUD EXTRACTION PATH
    # ==========================================================================
    if use_cloud:
        logger.debug("Using cloud for speaker embedding extraction")

        embedding = await registry.extract_speaker_embedding_cloud(audio_data)

        if embedding is not None:
            logger.debug(f"Cloud embedding extracted: shape {embedding.shape}")
            return embedding

        # Cloud failed - try local if fallback enabled and local is ready
        if fallback_enabled and registry.is_voice_unlock_ready:
            # v251.2: Log-once pattern for cloud→local fallback.  This fires
            # on EVERY voice verification while cloud is down — very spammy
            # when the circuit breaker is open.  Only log the first occurrence
            # at WARNING; subsequent hits log at DEBUG.
            global _cloud_fallback_warned
            if not _cloud_fallback_warned:
                logger.warning("Cloud extraction failed, falling back to local")
                _cloud_fallback_warned = True
            else:
                logger.debug("Cloud extraction failed, falling back to local")
            return await _extract_local_embedding(registry, audio_data)

        logger.error("Cloud extraction failed and fallback not available")
        return None

    # ==========================================================================
    # LOCAL EXTRACTION PATH
    # ==========================================================================
    if not registry.is_voice_unlock_ready:
        logger.warning("Local ECAPA-TDNN not ready, waiting...")
        ready = await wait_for_voice_unlock_ready(timeout=30.0)

        if not ready:
            # Local not ready - try cloud fallback
            if fallback_enabled and registry.cloud_endpoint:
                logger.warning("Local engines not ready, falling back to cloud")
                return await registry.extract_speaker_embedding_cloud(audio_data)

            logger.error("Local engines not ready and no cloud fallback")
            return None

    embedding = await _extract_local_embedding(registry, audio_data)

    if embedding is not None:
        return embedding

    # Local extraction failed - try cloud fallback
    if fallback_enabled and MLConfig.CLOUD_FALLBACK_ENABLED:
        logger.warning("Local extraction failed, attempting cloud fallback")

        # Activate cloud if not already active
        if not registry.cloud_endpoint:
            await registry._activate_cloud_routing()

        if registry.cloud_endpoint:
            return await registry.extract_speaker_embedding_cloud(audio_data)

    logger.error("Speaker embedding extraction failed (local and cloud)")
    return None


def _coerce_audio_to_float32(audio_data) -> Optional["np.ndarray"]:
    """Convert any supported audio format to float32 numpy array in [-1, 1].

    v226.1: Unified audio format detection for the local ECAPA extraction
    pipeline.  Handles every format that callers actually pass:

    1. **int16 PCM bytes** — from ``normalize_audio_data()`` in
       unified_voice_cache_manager and from raw WebSocket/mic capture.
       Detected when byte-length is even and is NOT a valid WAV header.
    2. **float32 bytes** — from callers that pre-convert to float32.
       Detected by byte-length being a multiple of 4 AND the resulting
       values being in a plausible audio range.
    3. **WAV container bytes** — from unified_supervisor voice enrollment.
       Detected by the ``RIFF`` magic header.
    4. **numpy arrays** — from parallel_vbi_orchestrator / vbi_debug_tracer.
       Detected by ``isinstance(audio_data, np.ndarray)``.
    5. **torch tensors** — from internal pipeline.
       Detected by ``hasattr(audio_data, 'numpy')``.

    Returns None for unrecognizable input rather than producing garbage.
    """
    import numpy as np

    # --- numpy array ---
    if isinstance(audio_data, np.ndarray):
        if audio_data.dtype == np.int16:
            return audio_data.astype(np.float32) / 32768.0
        return audio_data.astype(np.float32)

    # --- torch tensor ---
    if hasattr(audio_data, 'detach') and hasattr(audio_data, 'cpu'):
        try:
            np_arr = audio_data.detach().cpu().numpy().copy()
            return _coerce_audio_to_float32(np_arr)
        except Exception:
            return None

    # --- bytes ---
    if isinstance(audio_data, (bytes, bytearray)):
        raw = bytes(audio_data)

        if len(raw) < 4:
            return None

        # WAV container: decode via soundfile or wave stdlib
        if raw[:4] == b'RIFF':
            try:
                import io
                import wave
                with wave.open(io.BytesIO(raw), 'rb') as wf:
                    frames = wf.readframes(wf.getnframes())
                    sw = wf.getsampwidth()
                if sw == 2:
                    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                elif sw == 4:
                    return np.frombuffer(frames, dtype=np.float32)
                else:
                    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            except Exception:
                pass  # fall through to raw PCM heuristics

        # Heuristic: try float32 first, validate range
        if len(raw) % 4 == 0:
            candidate = np.frombuffer(raw, dtype=np.float32)
            if len(candidate) > 0:
                if not np.any(np.isnan(candidate)) and not np.any(np.isinf(candidate)):
                    absmax = np.abs(candidate).max()
                    # Plausible float32 audio lives in roughly [-10, 10].
                    # int16-reinterpreted-as-float32 produces values like 1e30+
                    if absmax < 100.0:
                        return candidate

        # Default: treat as int16 PCM (the most common raw format)
        if len(raw) % 2 == 0:
            return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

        # Odd-length bytes: pad and treat as int16
        padded = raw + b'\x00'
        return np.frombuffer(padded, dtype=np.int16).astype(np.float32) / 32768.0

    # --- base64 string ---
    if isinstance(audio_data, str):
        try:
            import base64
            decoded = base64.b64decode(audio_data)
            return _coerce_audio_to_float32(decoded)
        except Exception:
            return None

    logger.warning(
        f"[AudioCoerce] Unsupported audio type: {type(audio_data).__name__}"
    )
    return None


async def _extract_local_embedding(
    registry: MLEngineRegistry,
    audio_data
) -> Optional[Any]:
    """
    Extract embedding using local ECAPA-TDNN engine.

    Internal helper function for local extraction.
    Tries multiple paths:
    1. ML Registry's internal engine
    2. SpeechBrain engine's speaker encoder (external singleton)
    """
    import numpy as np

    # Path 1: Try ML Registry's internal engine
    try:
        ecapa_engine = registry.get_engine("ecapa_tdnn")
        if ecapa_engine is not None:
            import torch

            # SAFETY: Capture engine reference for thread closure
            engine_ref = ecapa_engine

            def _extract_sync():
                """
                Run blocking PyTorch operations in thread pool.

                Uses captured engine_ref to prevent null pointer access if
                engine is unloaded during extraction.
                """
                # Double-check engine reference is valid
                if engine_ref is None:
                    raise RuntimeError("Engine reference became None during extraction")

                # v226.1: Robust audio format detection.
                #
                # Root cause fix: callers pass audio in multiple formats —
                # int16 PCM bytes (from normalize_audio_data), numpy arrays
                # (from parallel_vbi_orchestrator), WAV container bytes
                # (from unified_supervisor), or float32 bytes.
                #
                # Previously assumed float32 bytes unconditionally, causing
                # NaN when int16 bytes were reinterpreted as float32
                # (IEEE 754 NaN bit patterns from negative int16 values).
                audio_array = _coerce_audio_to_float32(audio_data)

                if audio_array is None or len(audio_array) == 0:
                    raise RuntimeError(
                        "Audio conversion failed — could not interpret "
                        f"input as valid audio (type={type(audio_data).__name__}, "
                        f"len={len(audio_data) if hasattr(audio_data, '__len__') else '?'})"
                    )

                audio_tensor = torch.tensor(audio_array, dtype=torch.float32).unsqueeze(0)

                with torch.no_grad():
                    embedding = engine_ref.encode_batch(audio_tensor)

                # CRITICAL: Return a copy to avoid memory issues when tensor is GC'd
                return embedding.squeeze().detach().clone().cpu().numpy().copy()

            # CRITICAL FIX: Run blocking PyTorch in thread pool to avoid blocking event loop
            embedding = await asyncio.to_thread(_extract_sync)

            # v226.0: Validate local embedding before returning.
            # Silent/corrupt audio can produce zero-vector or NaN embeddings
            # from ECAPA-TDNN. Reject early so callers see None (= failed)
            # instead of getting a poisoned embedding into caches/profiles.
            if embedding is not None:
                if np.any(np.isnan(embedding)) or np.any(np.isinf(embedding)):
                    logger.warning(
                        "⚠️ [LocalECAPA] Embedding contains NaN/Inf — rejecting"
                    )
                    return None
                emb_norm = np.linalg.norm(embedding)
                if emb_norm < 1e-8:
                    logger.warning(
                        f"⚠️ [LocalECAPA] Embedding near-zero "
                        f"(norm={emb_norm:.2e}) — rejecting"
                    )
                    return None

            logger.debug(f"Local embedding extracted via ML Registry: shape {embedding.shape}")
            return embedding
    except RuntimeError as e:
        logger.debug(f"ML Registry engine not available: {e}")
    except EngineNotAvailableError as e:
        logger.debug(f"ML Registry engine not available: {e}")
    except Exception as e:
        logger.debug(f"ML Registry extraction failed: {e}")

    # Path 2: Try Speaker Verification Service's engine
    try:
        from voice.speaker_verification_service import get_speaker_verification_service
        svc = await get_speaker_verification_service()
        if svc and svc.speechbrain_engine and svc.speechbrain_engine.speaker_encoder is not None:
            # Use the service's engine to extract embedding
            embedding = await svc.speechbrain_engine.extract_speaker_embedding(audio_data)
            if embedding is not None:
                logger.debug(f"Local embedding extracted via Speaker Verification Service: shape {embedding.shape}")
                return embedding
    except Exception as e:
        logger.debug(f"Speaker Verification Service extraction failed: {e}")

    logger.error("Local speaker embedding extraction failed (all paths)")
    return None


def get_ecapa_encoder_sync() -> Optional[Any]:
    """
    Get the ECAPA-TDNN encoder synchronously.

    Use this in sync code paths where you can't await.
    Returns None if not initialized.
    """
    registry = get_ml_registry_sync()
    if registry is None or not registry.is_ready:
        return None

    try:
        return registry.get_engine("ecapa_tdnn")
    except RuntimeError:
        return None


async def get_ecapa_encoder_async() -> Optional[Any]:
    """
    Get the ECAPA-TDNN encoder asynchronously.

    Waits for the engine to be ready if it's still loading.
    """
    if not is_voice_unlock_ready():
        ready = await wait_for_voice_unlock_ready(timeout=30.0)
        if not ready:
            return None

    try:
        registry = await get_ml_registry()
        return registry.get_engine("ecapa_tdnn")
    except RuntimeError:
        return None


# =============================================================================
# CLOUD ROUTING HELPER FUNCTIONS
# =============================================================================

def is_using_cloud_ml() -> bool:
    """
    Check if the registry is currently routing to cloud.

    Returns:
        True if using cloud for ML operations
    """
    if _registry is None:
        return False
    return _registry.is_using_cloud


def get_cloud_endpoint() -> Optional[str]:
    """
    Get the current cloud ML endpoint.

    Returns:
        Cloud endpoint URL or None if not configured
    """
    if _registry is None:
        return None
    return _registry.cloud_endpoint


async def switch_to_cloud_ml(reason: str = "Manual switch") -> bool:
    """
    Switch ML operations to cloud.

    Args:
        reason: Reason for the switch

    Returns:
        True if switch was successful
    """
    registry = await get_ml_registry()
    return await registry.switch_to_cloud(reason)


async def switch_to_local_ml(reason: str = "Manual switch") -> bool:
    """
    Switch ML operations to local.

    Args:
        reason: Reason for the switch

    Returns:
        True if switch was successful
    """
    registry = await get_ml_registry()
    return await registry.switch_to_local(reason)


def get_ml_routing_status() -> Dict[str, Any]:
    """
    Get comprehensive ML routing status.

    Returns:
        Dict with routing status information
    """
    if _registry is None:
        return {
            "initialized": False,
            "is_ready": False,
            "using_cloud": False,
            "cloud_endpoint": None,
            "local_engines": {},
            "memory_pressure": MLConfig.check_memory_pressure(),
        }

    use_cloud, available_ram, reason = MLConfig.check_memory_pressure()

    return {
        "initialized": True,
        "is_ready": _registry.is_ready,
        "using_cloud": _registry.is_using_cloud,
        "cloud_endpoint": _registry.cloud_endpoint,
        "cloud_fallback_enabled": _registry._cloud_fallback_enabled,
        "local_engines": {
            name: {
                "loaded": engine.is_loaded,
                "state": engine.metrics.state.name,
                "load_time_ms": engine.metrics.load_duration_ms,
            }
            for name, engine in _registry._engines.items()
        },
        "memory_pressure": {
            "should_use_cloud": use_cloud,
            "available_ram_gb": available_ram,
            "reason": reason,
        },
        "config": {
            "cloud_first_mode": MLConfig.CLOUD_FIRST_MODE,
            "ram_threshold_local": MLConfig.RAM_THRESHOLD_LOCAL,
            "ram_threshold_cloud": MLConfig.RAM_THRESHOLD_CLOUD,
        },
    }


async def verify_speaker_with_best_method(
    audio_data: bytes,
    reference_embedding: Any,
    timeout: float = 30.0,
) -> Optional[Dict[str, Any]]:
    """
    Verify speaker using the best available method (local or cloud).

    Automatically routes to local or cloud based on registry state.

    Args:
        audio_data: Raw audio bytes
        reference_embedding: Reference embedding to compare against
        timeout: Request timeout

    Returns:
        Dict with verification result or None if failed
    """
    registry = await get_ml_registry()

    if registry.is_using_cloud:
        # Use cloud verification
        return await registry.verify_speaker_cloud(
            audio_data, reference_embedding, timeout
        )

    # Use local verification
    try:
        embedding = await extract_speaker_embedding(audio_data)
        if embedding is None:
            return None

        # Calculate cosine similarity
        import torch
        import torch.nn.functional as F

        if hasattr(reference_embedding, 'cpu'):
            ref = reference_embedding
        else:
            ref = torch.tensor(reference_embedding)

        # Ensure same shape
        if embedding.dim() == 3:
            embedding = embedding.squeeze(0)
        if ref.dim() == 3:
            ref = ref.squeeze(0)

        # Calculate cosine similarity
        similarity = F.cosine_similarity(
            embedding.view(1, -1),
            ref.view(1, -1)
        ).item()

        return {
            "success": True,
            "similarity": similarity,
            "verified": similarity > 0.7,  # Default threshold
            "method": "local",
        }

    except Exception as e:
        logger.error(f"Local speaker verification failed: {e}")

        # Try cloud fallback
        if registry._cloud_fallback_enabled and registry.cloud_endpoint:
            logger.info("Falling back to cloud verification")
            return await registry.verify_speaker_cloud(
                audio_data, reference_embedding, timeout
            )

        return None
