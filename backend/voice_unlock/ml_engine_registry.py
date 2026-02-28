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
    CLOUD_ENDPOINT_FAILOVER_ENABLED = os.getenv(
        "JARVIS_CLOUD_ENDPOINT_FAILOVER_ENABLED", "true"
    ).lower() == "true"
    CLOUD_ENDPOINT_FAILURE_BACKOFF_BASE = float(
        os.getenv("JARVIS_CLOUD_ENDPOINT_FAILURE_BACKOFF_BASE", "30.0")
    )
    CLOUD_ENDPOINT_FAILURE_BACKOFF_MAX = float(
        os.getenv("JARVIS_CLOUD_ENDPOINT_FAILURE_BACKOFF_MAX", "900.0")
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
            "cloud_endpoint_failover_enabled": cls.CLOUD_ENDPOINT_FAILOVER_ENABLED,
            "cloud_endpoint_failure_backoff_base": cls.CLOUD_ENDPOINT_FAILURE_BACKOFF_BASE,
            "cloud_endpoint_failure_backoff_max": cls.CLOUD_ENDPOINT_FAILURE_BACKOFF_MAX,
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
                    # v271.0: Even with "sufficient" vm_stat RAM, thrash state
                    # overrides. Active memory thrashing indicates the system
                    # is page-faulting faster than it can reclaim, regardless
                    # of what vm_stat reports as "free/inactive" pages. This
                    # bridges MLConfig to MemoryQuantizer's real-time pagein
                    # tracking, which is the authoritative memory health signal.
                    try:
                        import backend.core.memory_quantizer as _mq_mod
                        _mq_inst = _mq_mod._memory_quantizer_instance
                        if _mq_inst is not None and _mq_inst._thrash_state == "emergency":
                            return (
                                True,
                                available_gb,
                                f"Memory thrash EMERGENCY override "
                                f"(pageins/sec: {_mq_inst._pagein_rate:.0f}, "
                                f"RAM={available_gb:.1f}GB appears sufficient but system is thrashing)"
                            )
                    except Exception:
                        pass
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


class RoutingPolicy(Enum):
    """
    v276.4: Deterministic routing policy for ECAPA backend selection.

    Set by parity checks, flap detection, and operator overrides.
    Honored by ALL embedding extraction and verification paths.

    AUTO:        Normal routing — cloud if available, local fallback.
    CLOUD_ONLY:  Force cloud. Used when parity mismatch shows local is
                 the divergent backend, or when memory blocks local.
    LOCAL_ONLY:  Force local. Used when parity mismatch shows cloud is
                 divergent, or when cloud is unreachable.
    DEGRADED:    Both backends have issues. Accept best-effort with
                 logged warning. Flap dampening may set this.
    """
    AUTO = "auto"
    CLOUD_ONLY = "cloud_only"
    LOCAL_ONLY = "local_only"
    DEGRADED = "degraded"


class RouteDecisionReason(Enum):
    """
    v276.5: Stable enum for every routing decision.

    Each value is a machine-parseable reason code logged and counted
    for SLO tracking (route flaps/hour, degraded dwell time, etc.).
    """
    CLOUD_PRIMARY = "cloud_primary"
    LOCAL_PRIMARY = "local_primary"
    CLOUD_FALLBACK = "cloud_fallback"
    LOCAL_FALLBACK = "local_fallback"
    POLICY_CLOUD_ONLY = "policy_cloud_only"
    POLICY_LOCAL_ONLY = "policy_local_only"
    POLICY_DEGRADED = "policy_degraded"
    FLAP_DAMPENED = "flap_dampened"
    MEMORY_PRESSURE = "memory_pressure"
    RECOVERY_SUCCESS_CLOUD = "recovery_success_cloud"
    RECOVERY_SUCCESS_LOCAL = "recovery_success_local"
    RECOVERY_FAILED = "recovery_failed"
    DEEP_HEALTH_OK = "deep_health_ok"
    DEEP_HEALTH_FAILED = "deep_health_failed"
    SEMANTIC_READINESS_OK = "semantic_readiness_ok"
    SEMANTIC_READINESS_FAILED = "semantic_readiness_failed"
    CROSS_PROCESS_FENCED = "cross_process_fenced"
    HYSTERESIS_DWELL = "hysteresis_dwell"
    STARTUP_PHASE_GATED = "startup_phase_gated"
    PARITY_MISMATCH = "parity_mismatch"
    PARITY_RESTORED = "parity_restored"


class ParityStrictness(Enum):
    """
    v276.5: Per-environment parity enforcement policy.

    ENFORCE:  (prod default) Parity mismatch changes routing policy immediately.
    DEGRADE:  Warning + DEGRADED mode but doesn't force CLOUD_ONLY/LOCAL_ONLY.
    WARN:     (dev default) Log warning only, no routing changes.
    """
    ENFORCE = "enforce"
    DEGRADE = "degrade"
    WARN = "warn"


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
# EMBEDDING PARITY FINGERPRINT (v276.4)
# =============================================================================
# Tracks the model identity/configuration of both cloud and local ECAPA backends
# to detect version drift that causes confidence instability and inconsistent
# auth outcomes. Cloud ECAPA and local ECAPA MUST produce compatible embeddings;
# if model version, embedding dimension, sample rate, or preprocessing diverge,
# cosine-similarity scores become meaningless.

@dataclass
class _ParityFingerprint:
    """
    v276.4: Immutable snapshot of an ECAPA backend's model identity.

    Used for cloud-vs-local parity comparison. Two fingerprints are
    "compatible" iff embedding_dim, sample_rate, and model_source match.
    service_version and load_strategy are informational (don't affect
    embedding compatibility) but are tracked for forensic debugging.
    """
    backend: str = ""               # "cloud" or "local"
    embedding_dim: int = 0          # Expected: 192 for ECAPA-TDNN
    sample_rate: int = 0            # Expected: 16000 Hz
    model_source: str = ""          # e.g. "speechbrain/spkrec-ecapa-voxceleb"
    service_version: str = ""       # Cloud service version (e.g. "21.0.0")
    load_strategy: str = ""         # "jit", "onnx", "speechbrain", or ""
    captured_at: float = 0.0        # time.time() when fingerprint was captured
    raw_metadata: Dict[str, Any] = field(default_factory=dict)

    def is_populated(self) -> bool:
        """True if the fingerprint has been captured (not default-constructed)."""
        return self.embedding_dim > 0 and self.sample_rate > 0

    def is_compatible_with(self, other: "_ParityFingerprint") -> Tuple[bool, str]:
        """
        Check if two fingerprints produce compatible embeddings.

        Returns:
            (compatible, reason) — reason explains the mismatch if incompatible.
        """
        if not self.is_populated() or not other.is_populated():
            return False, (
                f"incomplete fingerprint: self.populated={self.is_populated()}, "
                f"other.populated={other.is_populated()}"
            )

        mismatches = []
        if self.embedding_dim != other.embedding_dim:
            mismatches.append(
                f"embedding_dim: {self.backend}={self.embedding_dim} vs "
                f"{other.backend}={other.embedding_dim}"
            )
        if self.sample_rate != other.sample_rate:
            mismatches.append(
                f"sample_rate: {self.backend}={self.sample_rate} vs "
                f"{other.backend}={other.sample_rate}"
            )
        if (
            self.model_source
            and other.model_source
            and self.model_source != other.model_source
        ):
            mismatches.append(
                f"model_source: {self.backend}={self.model_source} vs "
                f"{other.backend}={other.model_source}"
            )

        if mismatches:
            return False, "; ".join(mismatches)
        return True, "compatible"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "embedding_dim": self.embedding_dim,
            "sample_rate": self.sample_rate,
            "model_source": self.model_source,
            "service_version": self.service_version,
            "load_strategy": self.load_strategy,
            "captured_at": self.captured_at,
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
            # v271.3: Route through centralized safe loader (meta tensor protection).
            # Replaces v271.2's manual dual-import + conditional wrapping with the
            # canonical safe_from_hparams() which ensures patches + recovery in one call.
            try:
                from voice.engines.speechbrain_engine import safe_from_hparams
            except ImportError:
                from backend.voice.engines.speechbrain_engine import safe_from_hparams

            logger.info(f"   [{self.name}] Loading from: speechbrain/spkrec-ecapa-voxceleb")

            model = safe_from_hparams(
                "speechbrain.inference.speaker.EncoderClassifier",
                model_name="ecapa_tdnn",
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir=str(cache_dir),
                run_opts={"device": "cpu"},
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

    async def extract_speaker_embedding(self, audio_data: Any) -> Optional[Any]:
        """Extract speaker embedding from audio data.

        Delegates to the underlying SpeechBrain EncoderClassifier via
        thread-safe engine acquisition.  Audio format handling uses
        _coerce_audio_to_float32() which supports WAV bytes, raw PCM,
        numpy arrays, and torch tensors.

        Args:
            audio_data: Audio in any format accepted by _coerce_audio_to_float32()
                        (WAV bytes, int16 PCM, float32 bytes, numpy, torch tensor)

        Returns:
            192-dimensional numpy embedding, or None on failure.
        """
        if not self.is_loaded:
            logger.debug("[%s] Cannot extract — engine not loaded", self.name)
            return None

        try:
            import numpy as np
            import torch

            engine_ref = self.acquire_engine()
            try:
                def _extract_sync():
                    audio_array = _coerce_audio_to_float32(audio_data)
                    if audio_array is None or len(audio_array) == 0:
                        raise RuntimeError(
                            f"Audio conversion failed "
                            f"(type={type(audio_data).__name__}, "
                            f"len={len(audio_data) if hasattr(audio_data, '__len__') else '?'})"
                        )
                    audio_tensor = torch.tensor(
                        audio_array, dtype=torch.float32,
                    ).unsqueeze(0)
                    with torch.no_grad():
                        embedding = engine_ref.encode_batch(audio_tensor)
                    return embedding.squeeze().detach().clone().cpu().numpy().copy()

                embedding = await asyncio.to_thread(_extract_sync)
            finally:
                self.release_engine()

            if embedding is not None:
                if np.any(np.isnan(embedding)) or np.any(np.isinf(embedding)):
                    logger.warning("[%s] Embedding contains NaN/Inf — rejecting", self.name)
                    return None
                emb_norm = float(np.linalg.norm(embedding))
                if emb_norm < 1e-8:
                    logger.warning(
                        "[%s] Embedding near-zero (norm=%.2e) — rejecting",
                        self.name, emb_norm,
                    )
                    return None

            return embedding

        except EngineNotAvailableError:
            logger.debug("[%s] Engine not available for extraction", self.name)
            return None
        except Exception as e:
            logger.warning(
                "[%s] Embedding extraction failed: %s "
                "(input type=%s, len=%s)",
                self.name, e,
                type(audio_data).__name__,
                len(audio_data) if hasattr(audio_data, '__len__') else '?',
            )
            return None


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
            # v271.3: Route through centralized safe loader (meta tensor protection)
            try:
                from voice.engines.speechbrain_engine import safe_from_hparams
            except ImportError:
                from backend.voice.engines.speechbrain_engine import safe_from_hparams

            cache_dir = MLConfig.CACHE_DIR / "speechbrain" / "speechbrain-wav2vec2"
            cache_dir.mkdir(parents=True, exist_ok=True)

            # Use MPS on Apple Silicon, CPU otherwise
            device = "mps" if is_apple_silicon and torch.backends.mps.is_available() else "cpu"

            logger.info(f"   [{self.name}] Loading from: speechbrain/asr-wav2vec2-commonvoice-en")
            logger.info(f"   [{self.name}] Device: {device}")

            model = safe_from_hparams(
                "speechbrain.inference.ASR.EncoderDecoderASR",
                model_name="wav2vec2_stt",
                source="speechbrain/asr-wav2vec2-commonvoice-en",
                savedir=str(cache_dir),
                run_opts={"device": device},
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
        self._cloud_endpoint_failure_streak: Dict[str, int] = {}
        self._cloud_endpoint_last_failure_at: Dict[str, float] = {}
        self._cloud_endpoint_degraded_until: Dict[str, float] = {}
        self._cloud_endpoint_last_error: Dict[str, str] = {}
        self._cloud_failover_lock = LazyAsyncLock()
        self._cloud_embedding_route = self._normalize_cloud_route(
            os.getenv("JARVIS_CLOUD_EMBEDDING_ROUTE", "/api/ml/speaker_embedding"),
            default="/api/ml/speaker_embedding",
        )
        self._cloud_verify_route = self._normalize_cloud_route(
            os.getenv("JARVIS_CLOUD_VERIFY_ROUTE", "/api/ml/speaker_verify"),
            default="/api/ml/speaker_verify",
        )

        # v21.1.0: Cloud embedding circuit breaker
        self._cloud_embedding_cb = CloudEmbeddingCircuitBreaker()

        # v271.0: Deferred recovery when memory gate blocks initial ECAPA load.
        # Populated by _schedule_deferred_ecapa_recovery() when fallback is refused.
        self._deferred_ecapa_recovery_task: Optional[asyncio.Task] = None
        self._memory_gate_blocked: bool = False

        # v276.3: Recovery idempotency fencing. Prevents concurrent recovery
        # attempts from the deferred poll loop and the MemoryQuantizer callback.
        # Without this, both paths can call _attempt_ecapa_recovery() simultaneously,
        # causing duplicate cloud probes, state corruption on _use_cloud/_cloud_verified,
        # and wasted resources.
        self._recovery_lock = LazyAsyncLock()
        # Tracks the last recovery attempt timestamp to enforce minimum dwell
        # between successive attempts (hysteresis).
        self._last_recovery_attempt_at: float = 0.0
        self._last_recovery_result: Optional[bool] = None
        # v276.3: Structured routing reason for observability/forensics.
        self._last_routing_reason: str = "not_initialized"

        # v276.4: Embedding parity tracking. Captures model identity fingerprints
        # from both cloud and local backends, then compares to detect version drift
        # that would cause confidence instability in speaker verification.
        self._cloud_parity: _ParityFingerprint = _ParityFingerprint(backend="cloud")
        self._local_parity: _ParityFingerprint = _ParityFingerprint(backend="local")
        self._parity_compatible: Optional[bool] = None  # None = not yet checked
        self._parity_last_checked: float = 0.0
        self._parity_last_reason: str = "not_checked"

        # v276.4: Routing policy — deterministic backend selection override.
        # Set by parity checks, flap detection, or operator env var.
        # Honored by extract_speaker_embedding() and verify_speaker_with_best_method().
        _override = os.getenv("JARVIS_ECAPA_ROUTING_POLICY", "auto").lower()
        self._routing_policy: RoutingPolicy = (
            RoutingPolicy(_override) if _override in RoutingPolicy._value2member_map_
            else RoutingPolicy.AUTO
        )
        self._routing_policy_reason: str = (
            f"env_override:{_override}" if _override != "auto"
            else "default"
        )

        # v276.4: Backend flap detection — tracks cloud↔local transitions.
        # If > _flap_threshold transitions in _flap_window seconds, routing
        # is dampened to DEGRADED to prevent state thrashing.
        self._backend_transitions: List[Tuple[str, float]] = []  # (backend, timestamp)
        self._flap_window: float = float(os.getenv(
            "JARVIS_ECAPA_FLAP_WINDOW", "300.0"
        ))  # 5 minutes
        self._flap_threshold: int = int(os.getenv(
            "JARVIS_ECAPA_FLAP_THRESHOLD", "4"
        ))
        self._flap_dampened: bool = False
        self._flap_dampened_at: float = 0.0
        self._flap_dampen_duration: float = float(os.getenv(
            "JARVIS_ECAPA_FLAP_DAMPEN_DURATION", "600.0"
        ))  # 10 minutes lockout

        # v276.4: Periodic deep health task ref (started after first prewarm)
        self._deep_health_task: Optional[asyncio.Task] = None

        # v276.5: Monotonic state sequence — every state transition gets a
        # sequence number. Observers reject events with seq < their last seen.
        # Prevents "ghost recovered/degraded" from out-of-order telemetry.
        self._state_seq: int = 0

        # v277.0: Cross-repo control-plane lease/epoch for ECAPA state writes.
        # This prevents split-brain state publication when multiple repos/processes
        # attempt to write cloud_ecapa_state.json concurrently.
        self._cross_repo_lease_holder: str = (
            f"jarvis:{os.getpid()}:{int(time.time() * 1000)}"
        )
        self._cross_repo_lease_epoch: int = 0
        self._cross_repo_epoch_seq: int = 0

        # v277.0: Cached Trinity phase snapshot to avoid file I/O on every
        # recovery poll. Updated at most once per cache TTL.
        self._phase_cache_value: str = "unknown"
        self._phase_cache_at: float = 0.0

        # v276.5: Cross-process recovery idempotency. File-based token with
        # PID + timestamp + source. Process checks token before starting
        # recovery — if another process is already recovering (token age
        # < fence_ttl), skip to prevent duplicate recoveries.
        self._recovery_fence_ttl: float = float(os.getenv(
            "JARVIS_ECAPA_RECOVERY_FENCE_TTL", "30.0"
        ))  # Max expected recovery duration

        # v276.5: Route decision reason telemetry — stable counters
        # for SLO tracking. get_routing_telemetry() exposes this.
        self._routing_decisions: Dict[str, int] = {
            r.value: 0 for r in RouteDecisionReason
        }
        self._routing_decisions_since: float = time.time()

        # v276.5: Warm-instance degradation tracking.
        # Tracks cloud inference latency percentiles and memory growth
        # to detect slow poisoning on always-warm min-instances=1.
        self._cloud_latency_samples: List[float] = []
        self._cloud_latency_window: int = int(os.getenv(
            "JARVIS_ECAPA_LATENCY_WINDOW", "50"
        ))  # Keep last N latency samples
        self._cloud_latency_p95_baseline: float = 0.0  # Set after first N samples
        self._cloud_latency_degradation_threshold: float = float(os.getenv(
            "JARVIS_ECAPA_LATENCY_DEGRADATION_FACTOR", "3.0"
        ))  # Alert when P95 exceeds baseline by this factor
        self._cloud_memory_baseline_mb: float = 0.0
        self._cloud_memory_growth_threshold_mb: float = float(os.getenv(
            "JARVIS_ECAPA_MEMORY_GROWTH_THRESHOLD_MB", "512.0"
        ))  # Alert when cloud service memory grows by this much

        # v276.5: Parity strictness per environment
        _strictness = os.getenv(
            "JARVIS_ECAPA_PARITY_STRICTNESS",
            "enforce" if os.getenv("ENVIRONMENT", "dev") == "prod" else "warn"
        ).lower()
        self._parity_strictness: ParityStrictness = (
            ParityStrictness(_strictness)
            if _strictness in ParityStrictness._value2member_map_
            else ParityStrictness.WARN
        )

        # v275.1: Cloud Run pre-warming. Cloud Run containers have cold starts
        # of 10-30s. The contract probe (4s timeout) runs during heavy startup
        # when the container is most likely cold → always times out. Fix: send
        # a fire-and-forget warmup ping immediately on registry init. By the
        # time prewarm_all() calls _activate_cloud_routing() (seconds later),
        # the Cloud Run container is already warm from our early ping.
        self._cloud_prewarm_task: Optional[asyncio.Task] = None
        self._cloud_prewarm_started_at: float = 0.0
        self._cloud_prewarm_completed: bool = False

        # Register available engines based on config
        self._register_engines()

        # v275.1: Schedule cloud pre-warm immediately (non-blocking).
        # This runs in the background while other init proceeds.
        self._schedule_cloud_prewarm()

        # v276.2: Register MemoryQuantizer recovery callback for event-driven
        # ECAPA reload. When memory recovers from CRITICAL/EMERGENCY, this fires
        # immediately instead of waiting for the next 30s poll cycle.
        self._register_memory_recovery_callback()

        logger.info(f"🔧 MLEngineRegistry initialized with {len(self._engines)} engines")
        logger.info(f"   Config: {MLConfig.to_dict()}")
        logger.info(f"   Cloud fallback enabled: {self._cloud_fallback_enabled}")

    @staticmethod
    def _normalize_cloud_route(route: Optional[str], default: str) -> str:
        """Normalize API route fragments to '/path' form without trailing slash."""
        candidate = (route or default).strip()
        if not candidate:
            candidate = default
        if not candidate.startswith("/"):
            candidate = f"/{candidate}"
        normalized = candidate.rstrip("/")
        return normalized or default

    def _register_memory_recovery_callback(self) -> None:
        """
        v276.2: Register event-driven ECAPA reload on memory recovery.

        The MemoryQuantizer fires recovery callbacks when memory transitions
        from CRITICAL/EMERGENCY back to stable tiers. This eliminates the
        30s poll delay in _schedule_deferred_ecapa_recovery() — ECAPA reload
        triggers instantly when memory pressure subsides.

        If MemoryQuantizer is not yet initialized (common during startup —
        MLEngineRegistry.__init__ runs synchronously, MemoryQuantizer requires
        async initialize()), schedules deferred retries until registration
        succeeds.
        """
        self._memory_recovery_callback_registered = False

        def _on_memory_recovered(old_tier, new_tier) -> None:
            """Callback fired by MemoryQuantizer when memory stabilizes."""
            if not self._memory_gate_blocked:
                return  # ECAPA wasn't blocked — nothing to recover

            if self.is_ready:
                return  # Already recovered via another path

            logger.info(
                f"[v276.2] Memory recovered ({old_tier.value} → {new_tier.value}) "
                "— triggering immediate ECAPA recovery"
            )

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self._attempt_ecapa_recovery(
                        source="memory_recovery_callback"
                    ),
                    name="ecapa-memory-recovery",
                )
            except RuntimeError:
                logger.debug("[v276.2] No event loop for memory recovery callback")

        # Store reference for deferred registration
        self._memory_recovery_callback_fn = _on_memory_recovered

        def _try_register() -> bool:
            """Attempt registration. Returns True on success."""
            try:
                import backend.core.memory_quantizer as _mq_mod

                _mq = _mq_mod._memory_quantizer_instance
                if _mq is None:
                    return False

                _mq.register_recovery_callback(self._memory_recovery_callback_fn)
                self._memory_recovery_callback_registered = True
                logger.debug("[v276.2] MemoryQuantizer recovery callback registered")
                return True
            except ImportError:
                return False
            except Exception as e:
                logger.debug(f"[v276.2] Callback registration failed: {e}")
                return False

        # Try immediate registration
        if _try_register():
            return

        # MemoryQuantizer not yet initialized — schedule deferred retries.
        # By the time prewarm_all() runs (seconds later), MQ should be up.
        async def _deferred_register() -> None:
            retry_delay = float(os.getenv(
                "JARVIS_ECAPA_MQ_CALLBACK_RETRY_DELAY", "10.0"
            ))
            max_retries = int(os.getenv(
                "JARVIS_ECAPA_MQ_CALLBACK_MAX_RETRIES", "6"
            ))
            for attempt in range(1, max_retries + 1):
                await asyncio.sleep(retry_delay)
                if self._memory_recovery_callback_registered:
                    return  # Registered by another path
                if _try_register():
                    logger.info(
                        f"[v276.2] Deferred MQ callback registration succeeded "
                        f"(attempt {attempt})"
                    )
                    return
            logger.warning(
                "[v276.2] MQ callback registration failed after "
                f"{max_retries} retries. Event-driven ECAPA recovery "
                "unavailable (poll-based recovery still active)."
            )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                _deferred_register(),
                name="ecapa-mq-callback-register",
            )
        except RuntimeError:
            logger.debug("[v276.2] No event loop — MQ callback deferred to prewarm phase")

    def _schedule_cloud_prewarm(self) -> None:
        """
        v275.1: Fire-and-forget Cloud Run container warmup.

        Root cause (the disease):
            Cloud Run containers scale to zero when idle. The ECAPA contract
            probe runs during startup with a 4s timeout. Cloud Run cold start
            takes 10-30s. The probe ALWAYS times out → falls back to local
            ECAPA (500MB+) → memory pressure → Trinity stall at 80%.

        The cure:
            Send a lightweight HTTP GET to the Cloud Run health endpoint
            IMMEDIATELY when the registry is constructed (in __init__). This
            triggers Cloud Run's container provisioning in the background.
            By the time prewarm_all() calls _activate_cloud_routing() (which
            runs the contract probe), the container is already warm from
            our early ping. The 4s probe now succeeds because it hits a
            warm container (~100ms response time).

        Implementation:
            - Discovers cloud endpoints from env vars (same logic as
              _discover_cloud_endpoint_candidates but synchronous/cached)
            - Sends fire-and-forget HTTP GET to each candidate's /health
            - Doesn't wait for response — the point is to trigger cold start
            - If no event loop is running, no-ops gracefully (will work when
              prewarm_all() runs in an async context)
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No event loop yet — prewarm will happen in prewarm_all()

        # Collect cloud endpoint candidates synchronously from env vars
        candidates = []
        for env_key in (
            "ECAPA_CLOUD_RUN_URL",
            "JARVIS_CLOUD_ML_ENDPOINT",
            "JARVIS_CLOUD_ECAPA_ENDPOINT",
            "JARVIS_ML_CLOUD_ENDPOINT",
        ):
            endpoint = os.getenv(env_key, "").strip()
            if endpoint:
                candidates.append(endpoint)

        if not candidates:
            return  # No cloud endpoints configured

        self._cloud_prewarm_started_at = time.time()

        async def _prewarm_ping() -> None:
            """Send lightweight GET to trigger Cloud Run cold start."""
            import aiohttp
            for endpoint in candidates:
                health_url = endpoint.rstrip("/") + "/health"
                try:
                    timeout = aiohttp.ClientTimeout(total=45)  # Cloud Run cold start up to 30s
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(health_url) as resp:
                            if resp.status == 200:
                                logger.info(
                                    f"☁️  [v275.1] Cloud Run pre-warmed: {endpoint} "
                                    f"(cold start: {time.time() - self._cloud_prewarm_started_at:.1f}s)"
                                )
                                self._cloud_prewarm_completed = True
                                return
                            else:
                                logger.debug(
                                    f"[v275.1] Cloud prewarm ping got HTTP {resp.status} from {endpoint}"
                                )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug(f"[v275.1] Cloud prewarm ping to {endpoint} failed: {e}")

        self._cloud_prewarm_task = loop.create_task(
            _prewarm_ping(), name="ecapa-cloud-prewarm"
        )

    def _cloud_route_candidates(self, operation: str) -> List[str]:
        """
        Return ordered route candidates for cloud ML API calls.

        Supports contract drift between '/api/ml/*' and root-level routes.
        """
        if operation == "embedding":
            primary = self._normalize_cloud_route(
                self._cloud_embedding_route,
                default="/api/ml/speaker_embedding",
            )
            fallback = self._normalize_cloud_route(
                os.getenv(
                    "JARVIS_CLOUD_EMBEDDING_ROUTE_FALLBACK", "/speaker_embedding"
                ),
                default="/speaker_embedding",
            )
        elif operation == "verify":
            primary = self._normalize_cloud_route(
                self._cloud_verify_route,
                default="/api/ml/speaker_verify",
            )
            fallback = self._normalize_cloud_route(
                os.getenv(
                    "JARVIS_CLOUD_VERIFY_ROUTE_FALLBACK", "/speaker_verify"
                ),
                default="/speaker_verify",
            )
        else:
            raise ValueError(f"Unknown cloud route operation: {operation}")

        candidates = [primary]
        if fallback not in candidates:
            candidates.append(fallback)
        return candidates

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

                # CRITICAL FIX: Verify cloud backend is actually ready before marking as ready.
                # v276.4: Always require test_extraction=True at startup — process-ready
                # is not inference-ready.
                cloud_ready, cloud_reason = await self._verify_cloud_backend_ready(
                    test_extraction=True,
                )

                if cloud_ready:
                    # Cloud verified - mark as ready
                    self._status.prewarm_completed = True
                    self._status.prewarm_end_time = time.time()
                    self._status.is_ready = True
                    self._ready_event.set()
                    # v276.4: Reconcile state + record transition
                    self._reconcile_state_after_recovery("cloud", "startup_decision")
                    logger.info("✅ Cloud ML backend VERIFIED - voice unlock ready!")
                    return self._status
                else:
                    # Cloud failed - attempt local fallback
                    logger.warning(f"⚠️ Cloud backend not available: {cloud_reason}")
                    fallback_enabled = os.getenv("JARVIS_ECAPA_CLOUD_FALLBACK_ENABLED", "true").lower() == "true"

                    if fallback_enabled:
                        fallback_success = await self._fallback_to_local_ecapa(cloud_reason)
                        # v276.4: Semantic readiness after local fallback at startup
                        if fallback_success:
                            semantic_ok = await self._verify_semantic_readiness(backend="local")
                            if not semantic_ok:
                                fallback_success = False
                                logger.warning(
                                    "[v276.4] Local fallback loaded but failed "
                                    "semantic readiness test"
                                )
                        if fallback_success:
                            self._status.prewarm_completed = True
                            self._status.prewarm_end_time = time.time()
                            self._status.is_ready = True
                            self._ready_event.set()
                            self._reconcile_state_after_recovery("local", "startup_decision_fallback")
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

        # =======================================================================
        # DOCKER ECAPA GATE (Phase 2 already selected Docker as backend)
        # =======================================================================
        # _select_ecapa_backend() runs in Phase 2 and probes Docker, Cloud Run,
        # and Local concurrently.  If Docker was selected, it sets env vars
        # JARVIS_ECAPA_BACKEND=docker and JARVIS_DOCKER_ECAPA_ACTIVE=true.
        # Honor that decision here to avoid redundantly loading ~700MB of
        # PyTorch + SpeechBrain in-process when Docker already handles
        # ECAPA inference with zero additional memory cost.
        _docker_ecapa_active = os.getenv(
            "JARVIS_DOCKER_ECAPA_ACTIVE", "",
        ).lower() in ("1", "true", "yes")
        _ecapa_backend_env = os.getenv("JARVIS_ECAPA_BACKEND", "")
        if _docker_ecapa_active or _ecapa_backend_env == "docker":
            logger.info("=" * 70)
            logger.info("🐳 ML ENGINE REGISTRY: DOCKER ECAPA ACTIVE")
            logger.info("=" * 70)
            logger.info(f"   Backend selected in Phase 2: {_ecapa_backend_env}")
            logger.info("   Action: Routing ECAPA to Docker container, "
                        "skipping local model load (~700MB saved)")
            logger.info("=" * 70)

            self._use_cloud = True
            await self._activate_cloud_routing()

            cloud_ready, cloud_reason = await self._verify_cloud_backend_ready(
                test_extraction=True,
            )

            if cloud_ready:
                self._status.prewarm_completed = True
                self._status.prewarm_end_time = time.time()
                self._status.is_ready = True
                self._ready_event.set()
                self._reconcile_state_after_recovery("cloud", "docker_container")
                logger.info(
                    "✅ Docker ECAPA VERIFIED — voice unlock ready "
                    "(no local model load)"
                )
                return self._status
            else:
                # Docker was healthy in Phase 2 but failed verification now.
                # Fall through to memory pressure check / local prewarm.
                logger.warning(
                    f"⚠️ Docker ECAPA selected in Phase 2 but verification "
                    f"failed now: {cloud_reason}. Falling through to local prewarm."
                )

        # Check memory pressure directly if no startup decision
        use_cloud, available_ram, reason = MLConfig.check_memory_pressure()
        self._memory_pressure_at_init = (use_cloud, available_ram, reason)

        if use_cloud and not MLConfig.CLOUD_FIRST_MODE:
            self._record_routing_decision(RouteDecisionReason.MEMORY_PRESSURE)
            logger.info("=" * 70)
            logger.info("☁️  ML ENGINE REGISTRY: AUTO CLOUD MODE (Memory Pressure)")
            logger.info("=" * 70)
            logger.info(f"   Available RAM: {available_ram:.1f}GB")
            logger.info(f"   Reason: {reason}")
            logger.info(f"   Action: Routing ML to cloud instead of local loading")
            logger.info("=" * 70)

            self._use_cloud = True
            await self._activate_cloud_routing()

            # CRITICAL FIX: Verify cloud backend is actually ready before marking as ready.
            # v276.4: Always require test_extraction=True at startup.
            cloud_ready, cloud_reason = await self._verify_cloud_backend_ready(
                test_extraction=True,
            )

            if cloud_ready:
                self._status.prewarm_completed = True
                self._status.prewarm_end_time = time.time()
                self._status.is_ready = True
                self._ready_event.set()
                self._reconcile_state_after_recovery("cloud", "memory_pressure")
                logger.info("✅ Cloud ML backend VERIFIED - voice unlock ready!")
                return self._status
            else:
                # Cloud failed - attempt local fallback despite memory pressure
                logger.warning(f"⚠️ Cloud backend not available: {cloud_reason}")
                fallback_enabled = os.getenv("JARVIS_ECAPA_CLOUD_FALLBACK_ENABLED", "true").lower() == "true"

                if fallback_enabled:
                    logger.warning("⚠️ Attempting local ECAPA despite memory pressure...")
                    fallback_success = await self._fallback_to_local_ecapa(cloud_reason)
                    # v276.4: Semantic readiness after local fallback
                    if fallback_success:
                        semantic_ok = await self._verify_semantic_readiness(backend="local")
                        if not semantic_ok:
                            fallback_success = False
                            logger.warning(
                                "[v276.4] Local fallback loaded but failed "
                                "semantic readiness test"
                            )
                    if fallback_success:
                        self._status.prewarm_completed = True
                        self._status.prewarm_end_time = time.time()
                        self._status.is_ready = True
                        self._ready_event.set()
                        self._reconcile_state_after_recovery("local", "memory_pressure_fallback")
                        logger.info("✅ Local ECAPA fallback successful - voice unlock ready!")
                        return self._status

                # Both failed — check if memory state forbids local loading.
                # v271.0: If we got here because check_memory_pressure() said
                # "use_cloud" AND the memory gate blocked _fallback_to_local_ecapa(),
                # falling through to full local prewarm would defeat the gate.
                _mem_blocks_local = False
                try:
                    import backend.core.memory_quantizer as _mq_mod
                    from backend.core.memory_quantizer import MemoryTier
                    _mq = _mq_mod._memory_quantizer_instance
                    if _mq is not None:
                        _mem_blocks_local = (
                            _mq._thrash_state == "emergency"
                            or _mq.current_tier in (MemoryTier.CRITICAL, MemoryTier.EMERGENCY)
                        )
                except Exception:
                    pass

                if _mem_blocks_local:
                    logger.warning(
                        "[v271.0] Cloud failed and memory state forbids local loading. "
                        "Voice unlock will be unavailable until memory stabilizes."
                    )
                    self._schedule_deferred_ecapa_recovery()
                    self._status.prewarm_completed = True
                    self._status.prewarm_end_time = time.time()
                    self._status.is_ready = False
                    self._status.errors.append(
                        "Cloud ECAPA unavailable and local loading blocked by memory emergency"
                    )
                    return self._status
                else:
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

        # v276.4: Start periodic deep health validation task.
        # Only after first prewarm — no point checking health before models load.
        if self._deep_health_task is None or self._deep_health_task.done():
            try:
                self._deep_health_task = asyncio.get_running_loop().create_task(
                    self._periodic_deep_health_check()
                )
            except RuntimeError:
                pass  # No event loop

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
            # Endpoint-level breakers are independent; do not carry global
            # request breaker state from a failed endpoint to a new endpoint.
            if new_endpoint and old_endpoint and new_endpoint != old_endpoint:
                self._reset_cloud_request_failures()
                self._cloud_embedding_cb = CloudEmbeddingCircuitBreaker()

    def _reset_cloud_request_failures(self) -> None:
        """Clear global cloud API degraded state without marking endpoint healthy."""
        self._cloud_api_failure_streak = 0
        self._cloud_api_last_failure_at = 0.0
        self._cloud_api_degraded_until = 0.0
        self._cloud_api_last_error = ""
        self._cloud_api_last_cooldown_log_at = 0.0

    def _apply_ecapa_backend_environment(
        self,
        backend: str,
        endpoint: Optional[str],
    ) -> None:
        """
        Publish canonical ECAPA backend env vars from the single authority.

        v277.0: Moves env publication into MLEngineRegistry to avoid split
        control planes between unified_supervisor and ml_engine_registry.
        """
        normalized_endpoint = (endpoint or "").strip()
        if backend == "docker":
            os.environ["JARVIS_CLOUD_ML_ENDPOINT"] = normalized_endpoint
            os.environ["JARVIS_DOCKER_ECAPA_ACTIVE"] = "true"
            os.environ["JARVIS_ECAPA_BACKEND"] = "docker"
        elif backend == "cloud_run":
            os.environ["JARVIS_CLOUD_ML_ENDPOINT"] = normalized_endpoint
            os.environ["JARVIS_DOCKER_ECAPA_ACTIVE"] = "false"
            os.environ["JARVIS_ECAPA_BACKEND"] = "cloud_run"
        elif backend == "local":
            os.environ["JARVIS_DOCKER_ECAPA_ACTIVE"] = "false"
            os.environ["JARVIS_ECAPA_BACKEND"] = "local"

    def _classify_endpoint_backend(self, endpoint: str, source: str) -> str:
        """Classify endpoint candidate into docker vs cloud_run backend."""
        normalized = (endpoint or "").strip().rstrip("/")
        source_norm = (source or "").lower()

        if "docker" in source_norm:
            return "docker"
        if normalized.startswith("http://127.0.0.1:") or normalized.startswith(
            "http://localhost:"
        ):
            _docker_active = os.getenv("JARVIS_DOCKER_ECAPA_ACTIVE", "").lower() in (
                "1",
                "true",
                "yes",
            )
            _backend_env = os.getenv("JARVIS_ECAPA_BACKEND", "")
            if _docker_active or _backend_env == "docker":
                return "docker"

        return "cloud_run"

    def _read_supervisor_system_phase(self) -> str:
        """
        Read Trinity-published supervisor phase (startup/runtime/shutdown).

        Uses a short in-memory cache to keep recovery loops inexpensive.
        """
        now = time.time()
        cache_ttl = max(
            0.1, float(os.getenv("JARVIS_SYSTEM_PHASE_CACHE_TTL", "1.0"))
        )
        if (now - self._phase_cache_at) <= cache_ttl:
            return self._phase_cache_value

        phase = "unknown"
        phase_file = Path.home() / ".jarvis" / "trinity" / "state" / "system_phase.json"
        try:
            if phase_file.exists():
                import json

                payload = json.loads(phase_file.read_text())
                phase = str(payload.get("phase", "unknown")).strip().lower() or "unknown"
        except Exception:
            phase = "unknown"

        self._phase_cache_value = phase
        self._phase_cache_at = now
        return phase

    def _startup_phase_allows_ecapa_recovery(self, source: str) -> bool:
        """
        Decide whether ECAPA recovery may run during startup phases.

        v277.0: Prevents deferred recovery from competing with heavy startup
        phases (notably Two-Tier) under constrained CPU/memory conditions.
        """
        allow_during_startup = os.getenv(
            "JARVIS_ECAPA_RECOVERY_DURING_STARTUP", "false"
        ).lower() in ("1", "true", "yes")
        if allow_during_startup:
            return True

        phase = self._read_supervisor_system_phase()
        if phase != "startup":
            return True

        source_norm = (source or "").lower()
        if source_norm.startswith("deferred_poll_") or source_norm.startswith(
            "memory_recovery_callback"
        ):
            return False
        return True

    def _is_local_startup_backend_allowed(self) -> Tuple[bool, str]:
        """Determine whether local backend is admissible at startup."""
        try:
            import backend.core.memory_quantizer as _mq_mod
            from backend.core.memory_quantizer import MemoryTier

            _mq = _mq_mod._memory_quantizer_instance
            if _mq is not None:
                _thrash = getattr(_mq, "_thrash_state", "unknown")
                _tier = getattr(_mq, "current_tier", None)
                _tier_value = (
                    _tier.value if hasattr(_tier, "value") else str(_tier or "unknown")
                )
                if _thrash == "emergency":
                    return False, "memory_blocked_local:thrash=emergency"
                if _tier in (MemoryTier.CRITICAL, MemoryTier.EMERGENCY):
                    return False, f"memory_blocked_local:tier={_tier_value}"
                return True, f"memory_ok:thrash={_thrash},tier={_tier_value}"
        except Exception:
            pass

        use_cloud, _, reason = MLConfig.check_memory_pressure()
        return (not use_cloud), f"mlconfig:{reason}"

    async def determine_startup_backend(
        self,
        source: str = "unified_supervisor",
    ) -> Dict[str, Any]:
        """
        Select and apply ECAPA backend for startup from a single authority.

        v277.0: Canonical startup backend selector used by unified_supervisor.
        Eliminates duplicated probe/policy logic across modules.
        """
        result: Dict[str, Any] = {
            "selected_backend": None,
            "endpoint": None,
            "decision_reason": "",
            "failure_category": None,
            "probes": {},
        }

        if not MLConfig.ENABLE_ECAPA:
            result["decision_reason"] = "ECAPA disabled by configuration"
            result["failure_category"] = "DISABLED"
            return result

        force_backend = os.getenv("JARVIS_ECAPA_FORCE_BACKEND", "").strip().lower()
        verify_timeout = max(
            1.0,
            float(
                os.getenv(
                    "JARVIS_ECAPA_STARTUP_PROBE_TIMEOUT",
                    os.getenv("JARVIS_CLOUD_CONTRACT_TIMEOUT", "4.0"),
                )
            ),
        )
        verify_retries = max(
            1, int(os.getenv("JARVIS_ECAPA_STARTUP_PROBE_RETRIES", "1"))
        )
        selection_budget = max(
            2.0, float(os.getenv("JARVIS_ECAPA_STARTUP_TOTAL_BUDGET", "10.0"))
        )
        selection_started_at = time.monotonic()

        candidates = await self._discover_cloud_endpoint_candidates()
        if force_backend in ("docker", "cloud_run"):
            preferred: List[Tuple[str, str]] = []
            deferred: List[Tuple[str, str]] = []
            for endpoint, cand_source in candidates:
                target_backend = self._classify_endpoint_backend(endpoint, cand_source)
                if target_backend == force_backend:
                    preferred.append((endpoint, cand_source))
                else:
                    deferred.append((endpoint, cand_source))
            candidates = preferred + deferred

        last_failure_reason = "no_cloud_candidate"
        for endpoint, cand_source in candidates:
            elapsed = time.monotonic() - selection_started_at
            if elapsed >= selection_budget:
                last_failure_reason = (
                    f"startup_selection_budget_exhausted:{elapsed:.1f}s/{selection_budget:.1f}s"
                )
                break

            normalized = endpoint.strip().rstrip("/")
            if not normalized:
                continue

            candidate_backend = self._classify_endpoint_backend(normalized, cand_source)
            probe_key = f"{candidate_backend}:{cand_source}:{normalized}"

            if not self._cloud_endpoint_probe_allowed(normalized):
                result["probes"][probe_key] = {
                    "ready": False,
                    "reason": "endpoint_backoff_active",
                }
                continue

            self._set_cloud_endpoint(normalized, f"{cand_source}|startup_authority")
            self._use_cloud = True
            self._cloud_verified = False
            remaining_budget = max(
                1.0,
                selection_budget - (time.monotonic() - selection_started_at),
            )
            candidate_timeout = min(verify_timeout, remaining_budget)

            ready, verify_msg = await self._verify_cloud_backend_ready(
                timeout=candidate_timeout,
                retry_count=verify_retries,
                test_extraction=False,
                wait_for_ecapa=False,
                ecapa_wait_timeout=candidate_timeout,
            )
            result["probes"][probe_key] = {"ready": ready, "reason": verify_msg}
            if ready:
                self._memory_gate_blocked = False
                self._last_routing_reason = f"startup_selected:{candidate_backend}"
                self._apply_ecapa_backend_environment(candidate_backend, normalized)
                result["selected_backend"] = candidate_backend
                result["endpoint"] = normalized
                result["decision_reason"] = (
                    f"startup authority selected {candidate_backend} "
                    f"(source={cand_source})"
                )
                result["failure_category"] = None
                return result

            last_failure_reason = verify_msg
            self._record_cloud_endpoint_failure(
                normalized,
                reason=f"Startup verification failed: {verify_msg}",
            )

        local_allowed, local_reason = self._is_local_startup_backend_allowed()
        if force_backend == "local" and not local_allowed:
            result["decision_reason"] = (
                f"forced local backend rejected by admission gate: {local_reason}"
            )
            result["failure_category"] = "MEMORY_BLOCKED"
            self._set_cloud_endpoint(None, "none")
            self._use_cloud = False
            self._cloud_verified = False
            return result

        if force_backend in ("docker", "cloud_run"):
            if result["selected_backend"] is None:
                result["decision_reason"] = (
                    f"forced backend '{force_backend}' unavailable: {last_failure_reason}"
                )
                result["failure_category"] = "UNREACHABLE"
                self._set_cloud_endpoint(None, "none")
                self._use_cloud = False
                self._cloud_verified = False
                return result

        if local_allowed:
            self._set_cloud_endpoint(None, "none")
            self._use_cloud = False
            self._cloud_verified = False
            self._memory_gate_blocked = False
            self._last_routing_reason = "startup_selected:local"
            self._apply_ecapa_backend_environment("local", None)
            result["selected_backend"] = "local"
            result["endpoint"] = None
            result["decision_reason"] = f"startup authority selected local ({local_reason})"
            result["failure_category"] = None
            return result

        self._set_cloud_endpoint(None, "none")
        self._use_cloud = False
        self._cloud_verified = False
        result["decision_reason"] = (
            "no startup backend available: "
            f"cloud={last_failure_reason}, local={local_reason}"
        )
        result["failure_category"] = "UNREACHABLE"
        return result

    async def _discover_cloud_endpoint_candidates(self) -> List[Tuple[str, str]]:
        """
        Discover candidate cloud endpoints in priority order.

        Sources are dynamic (memory-aware startup state, env overrides, optional
        local endpoints) and deduplicated to avoid redundant probes.
        """
        candidates: List[Tuple[str, str]] = []

        # 0) Docker ECAPA container (selected by _select_ecapa_backend in Phase 2).
        # Docker is local (127.0.0.1), zero network hop, pre-baked with model
        # cache — highest priority because it avoids ~700MB in-process loading.
        _docker_active = os.getenv(
            "JARVIS_DOCKER_ECAPA_ACTIVE", "",
        ).lower() in ("1", "true", "yes")
        _ecapa_backend_env = os.getenv("JARVIS_ECAPA_BACKEND", "")
        if _docker_active or _ecapa_backend_env == "docker":
            _ecapa_port = int(os.getenv("JARVIS_ECAPA_PORT", "8015"))
            candidates.append(
                (f"http://127.0.0.1:{_ecapa_port}", "docker_container")
            )

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
                        (
                            f"http://{result.get('ip')}:{_ecapa_port}",
                            "memory_aware_activated",
                        )
                    )
        except ImportError:
            logger.debug("MemoryAwareStartup not available")
        except Exception as e:
            logger.debug(f"MemoryAwareStartup endpoint discovery failed: {e}")

        # 2) Endpoint list env overrides (highest explicit operator control).
        for env_key in ("JARVIS_CLOUD_ECAPA_ENDPOINTS", "JARVIS_ML_CLOUD_ENDPOINTS"):
            raw = os.getenv(env_key, "")
            if not raw:
                continue
            for endpoint in raw.split(","):
                normalized = endpoint.strip()
                if normalized:
                    candidates.append((normalized, f"{env_key.lower()}"))

        # 3) Single endpoint env vars (operator-configured only; no hardcoded URL).
        for env_key, source in (
            ("ECAPA_CLOUD_RUN_URL", "cloud_run_env"),
            ("JARVIS_CLOUD_ML_ENDPOINT", "jarvis_cloud_ml_endpoint"),
            ("JARVIS_CLOUD_ECAPA_ENDPOINT", "jarvis_cloud_ecapa_endpoint"),
            ("JARVIS_ML_CLOUD_ENDPOINT", "jarvis_ml_cloud_endpoint"),
        ):
            endpoint = os.getenv(env_key, "").strip()
            if endpoint:
                candidates.append((endpoint, source))

        # 3.5) Cross-repo endpoint sharing (JARVIS Prime / Reactor Core).
        try:
            shared_state = await self._read_cross_repo_ecapa_state()
            if shared_state:
                shared_endpoint = str(shared_state.get("cloud_endpoint", "")).strip()
                shared_source_repo = str(shared_state.get("source_repo", "unknown")).strip()
                if shared_endpoint:
                    candidates.append((shared_endpoint, f"cross_repo_state:{shared_source_repo}"))
        except Exception as e:
            logger.debug(f"Cross-repo endpoint discovery failed: {e}")

        # 4) Optional localhost fallbacks, but only if explicitly allowed.
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

        # Include currently configured endpoint first to avoid unnecessary churn.
        if self._cloud_endpoint:
            candidates.insert(0, (self._cloud_endpoint, self._cloud_endpoint_source))

        # Deduplicate while preserving priority order.
        seen: Set[str] = set()
        deduped_candidates: List[Tuple[str, str]] = []
        for endpoint, source in candidates:
            normalized = endpoint.strip().rstrip("/")
            if not normalized or normalized in seen or "None" in normalized:
                continue
            seen.add(normalized)
            deduped_candidates.append((normalized, source))
        return deduped_candidates

    def _record_cloud_endpoint_failure(
        self,
        endpoint: Optional[str],
        reason: str,
        status_code: Optional[int] = None,
        retry_after_seconds: float = 0.0,
    ) -> None:
        """Track endpoint-specific failures and backoff windows."""
        if not endpoint:
            return
        normalized = endpoint.strip().rstrip("/")
        if not normalized:
            return

        now = time.time()
        prev_ts = self._cloud_endpoint_last_failure_at.get(normalized, 0.0)
        reset_window = max(
            MLConfig.CLOUD_API_FAILURE_STREAK_RESET,
            float(os.getenv("JARVIS_CLOUD_ENDPOINT_FAILURE_RESET_SECONDS", "300.0")),
        )
        if prev_ts and (now - prev_ts) > reset_window:
            self._cloud_endpoint_failure_streak[normalized] = 0

        failure_streak = self._cloud_endpoint_failure_streak.get(normalized, 0) + 1
        self._cloud_endpoint_failure_streak[normalized] = failure_streak
        self._cloud_endpoint_last_failure_at[normalized] = now

        backoff = min(
            MLConfig.CLOUD_ENDPOINT_FAILURE_BACKOFF_BASE
            * (2 ** max(0, failure_streak - 1)),
            MLConfig.CLOUD_ENDPOINT_FAILURE_BACKOFF_MAX,
        )
        if retry_after_seconds > 0:
            backoff = max(backoff, retry_after_seconds)

        self._cloud_endpoint_degraded_until[normalized] = max(
            self._cloud_endpoint_degraded_until.get(normalized, 0.0),
            now + backoff,
        )
        if status_code is None:
            self._cloud_endpoint_last_error[normalized] = reason[:240]
        else:
            self._cloud_endpoint_last_error[normalized] = f"HTTP {status_code}: {reason[:200]}"

    def _cloud_endpoint_probe_allowed(self, endpoint: Optional[str]) -> bool:
        """Whether endpoint is currently eligible for probing/selection."""
        if not endpoint:
            return False
        normalized = endpoint.strip().rstrip("/")
        if not normalized:
            return False
        return self._cloud_endpoint_degraded_until.get(normalized, 0.0) <= time.time()

    async def _attempt_cloud_endpoint_failover(
        self,
        trigger: str,
        failed_endpoint: Optional[str] = None,
    ) -> bool:
        """
        Attempt endpoint rotation after hard cloud failures.

        Returns True only when a different endpoint is selected and contract-verified.
        """
        if not MLConfig.CLOUD_ENDPOINT_FAILOVER_ENABLED:
            return False

        failed = (failed_endpoint or self._cloud_endpoint or "").strip().rstrip("/")
        if not failed:
            return False

        async with self._cloud_failover_lock:
            current = (self._cloud_endpoint or "").strip().rstrip("/")
            if current and current != failed:
                # Another coroutine already switched endpoint.
                return True

            candidates = await self._discover_cloud_endpoint_candidates()
            probe_timeout = max(
                1.0, float(os.getenv("JARVIS_CLOUD_FAILOVER_CONTRACT_TIMEOUT", "3.0"))
            )
            for endpoint, source in candidates:
                normalized = endpoint.strip().rstrip("/")
                if not normalized or normalized == failed:
                    continue
                if not self._cloud_endpoint_probe_allowed(normalized):
                    continue

                contract_ok, contract_reason = await self._verify_cloud_endpoint_contract(
                    endpoint=normalized,
                    timeout=probe_timeout,
                    force=True,
                )
                if contract_ok:
                    old_endpoint = self._cloud_endpoint
                    old_source = self._cloud_endpoint_source
                    self._set_cloud_endpoint(normalized, f"{source}|failover")
                    self._use_cloud = True
                    logger.warning(
                        "Cloud endpoint failover: %s (%s) -> %s (%s) [trigger=%s]",
                        old_endpoint,
                        old_source,
                        self._cloud_endpoint,
                        self._cloud_endpoint_source,
                        trigger,
                    )
                    return True

                self._record_cloud_endpoint_failure(
                    normalized,
                    reason=f"Failover contract validation failed: {contract_reason}",
                )
                logger.debug(
                    "Rejected failover endpoint %s (source=%s): %s",
                    normalized,
                    source,
                    contract_reason,
                )

        return False

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
        # v265.3: Extend timeout under CPU pressure. At 99.8% CPU, the
        # event loop barely gets time slices to process HTTP responses.
        # A 4s timeout measured under normal CPU is too aggressive when
        # everything is starved.
        try:
            import psutil as _psutil
            _cpu = _psutil.cpu_percent(interval=None)
            if _cpu > 90.0:
                _cpu_factor = 1.0 + (_cpu - 90.0) / 10.0 * 2.0
                req_timeout *= _cpu_factor
        except Exception:
            pass
        # v271.0: Extend timeout under memory thrash. During EMERGENCY
        # thrash (5000+ pageins/sec), the event loop is severely starved
        # by constant page faults — even simple HTTP responses can't be
        # read within normal timeouts. This is MORE impactful than CPU
        # pressure because page faults are uninterruptible kernel waits.
        try:
            import backend.core.memory_quantizer as _mq_mod
            _mq_inst = _mq_mod._memory_quantizer_instance
            if _mq_inst is not None:
                if _mq_inst._thrash_state == "emergency":
                    _mem_floor = float(os.getenv(
                        "JARVIS_CLOUD_CONTRACT_TIMEOUT_THRASH_EMERGENCY", "12.0"
                    ))
                    req_timeout = max(req_timeout, _mem_floor)
                elif _mq_inst._thrash_state == "thrashing":
                    _mem_floor = float(os.getenv(
                        "JARVIS_CLOUD_CONTRACT_TIMEOUT_THRASH_WARNING", "8.0"
                    ))
                    req_timeout = max(req_timeout, _mem_floor)
        except Exception:
            pass
        probe_attempts = max(1, int(os.getenv("JARVIS_CLOUD_CONTRACT_ATTEMPTS", "2")))
        probe_backoff = max(0.1, float(os.getenv("JARVIS_CLOUD_CONTRACT_BACKOFF_SECONDS", "0.35")))
        connect_timeout = max(
            0.5,
            min(
                req_timeout,
                float(os.getenv("JARVIS_CLOUD_CONTRACT_CONNECT_TIMEOUT", "2.0")),
            ),
        )
        read_timeout = max(
            0.5,
            float(os.getenv("JARVIS_CLOUD_CONTRACT_READ_TIMEOUT", str(req_timeout))),
        )
        health_url = f"{target}/api/ml/health"
        embed_paths = self._cloud_route_candidates("embedding")

        def _record_contract_failure(reason: str) -> Tuple[bool, str]:
            self._cloud_contract_verified = False
            self._cloud_contract_endpoint = target
            self._cloud_contract_last_checked = time.time()
            self._cloud_contract_last_error = reason[:240]
            return False, reason

        last_transient_reason = ""
        timeout_cfg = aiohttp.ClientTimeout(
            total=req_timeout,
            connect=connect_timeout,
            sock_read=read_timeout,
        )

        async with aiohttp.ClientSession() as session:
            for attempt in range(1, probe_attempts + 1):
                try:
                    # Contract requirement 1: /api/ml/health must exist and advertise ECAPA readiness.
                    async with session.get(
                        health_url,
                        timeout=timeout_cfg,
                        headers={"Accept": "application/json"},
                    ) as response:
                        if response.status != 200:
                            reason = f"/api/ml/health returned HTTP {response.status}"
                            return _record_contract_failure(reason)

                        payload = await response.json()
                        ecapa_ready = payload.get("ecapa_ready")
                        if not isinstance(ecapa_ready, bool):
                            reason = "missing boolean ecapa_ready in /api/ml/health"
                            return _record_contract_failure(reason)

                    # Contract requirement 2: embedding path must be routable.
                    route_selected = False
                    route_failure_reason = ""
                    route_transient_failure = False
                    for embed_path in embed_paths:
                        embed_url = f"{target}{embed_path}"
                        async with session.options(
                            embed_url,
                            timeout=timeout_cfg,
                            headers={"Accept": "application/json"},
                        ) as response:
                            if response.status == 404:
                                route_failure_reason = (
                                    f"{embed_path} route missing (HTTP 404)"
                                )
                                continue
                            if response.status >= 500:
                                route_transient_failure = True
                                route_failure_reason = (
                                    f"{embed_path} options failed (HTTP {response.status})"
                                )
                                continue

                            route_selected = True
                            if embed_path != self._cloud_embedding_route:
                                logger.info(
                                    "Cloud embedding route updated during contract "
                                    "probe: %s -> %s",
                                    self._cloud_embedding_route,
                                    embed_path,
                                )
                                self._cloud_embedding_route = embed_path
                            break

                    if not route_selected:
                        if route_transient_failure and attempt < probe_attempts:
                            last_transient_reason = (
                                route_failure_reason
                                or "embedding route probe failed with transient 5xx"
                            )
                            await asyncio.sleep(
                                min(2.0, probe_backoff * (2 ** (attempt - 1)))
                            )
                            continue
                        return _record_contract_failure(
                            route_failure_reason
                            or "No routable embedding endpoint found"
                        )

                    # Success
                    last_transient_reason = ""
                    break

                except asyncio.TimeoutError:
                    last_transient_reason = (
                        f"contract probe timed out after {req_timeout:.1f}s "
                        f"(attempt {attempt}/{probe_attempts})"
                    )
                except aiohttp.ClientError as e:
                    last_transient_reason = (
                        f"contract probe connection error: {type(e).__name__}: {e}"
                    )
                except Exception as e:
                    last_transient_reason = (
                        f"contract probe error: {type(e).__name__}: {e}"
                    )

                if attempt < probe_attempts:
                    await asyncio.sleep(min(2.0, probe_backoff * (2 ** (attempt - 1))))
                else:
                    return _record_contract_failure(last_transient_reason)

        self._cloud_contract_verified = True
        self._cloud_contract_endpoint = target
        self._cloud_contract_last_checked = time.time()
        self._cloud_contract_last_error = ""

        # v276.4: Schedule parity check (non-blocking). Doesn't gate contract
        # acceptance — parity drift is a WARNING, not a hard failure — but it
        # surfaces early so operators can fix model version mismatches before
        # they cause confidence instability in speaker verification.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._check_embedding_parity(force=True))
        except RuntimeError:
            pass  # No event loop — skip parity check (e.g., sync test context)

        return True, "ECAPA contract verified"

    async def _activate_cloud_routing(self) -> bool:
        """
        Activate cloud routing for ML operations.

        This configures the registry to route speaker verification
        and other ML operations to GCP instead of local processing.

        v275.1: Waits for cloud pre-warm task (if running) before probing.
        This ensures the Cloud Run container is warm when we probe, eliminating
        the 4s timeout failures that previously caused fallback to local ECAPA.

        Returns:
            True if cloud routing was successfully activated
        """
        try:
            # v275.1: If a cloud pre-warm task is running, wait for it to complete
            # (or timeout gracefully). This gives Cloud Run time to cold-start.
            _prewarm_task = self._cloud_prewarm_task
            if _prewarm_task is not None and not _prewarm_task.done():
                _prewarm_wait = float(os.getenv("JARVIS_CLOUD_PREWARM_WAIT", "35.0"))
                logger.info(
                    f"[v275.1] Waiting up to {_prewarm_wait:.0f}s for Cloud Run "
                    f"pre-warm to complete..."
                )
                try:
                    await asyncio.wait_for(
                        asyncio.shield(_prewarm_task), timeout=_prewarm_wait
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[v275.1] Cloud Run pre-warm didn't complete in time — "
                        "proceeding with contract probe (container may still be cold)"
                    )
                except Exception as e:
                    logger.debug(f"[v275.1] Cloud pre-warm task error: {e}")

                if self._cloud_prewarm_completed:
                    logger.info(
                        f"[v275.1] Cloud Run container is warm "
                        f"(cold start: {time.time() - self._cloud_prewarm_started_at:.1f}s)"
                    )

            candidates = await self._discover_cloud_endpoint_candidates()
            # v275.1: Use a generous timeout now that we've pre-warmed.
            # The container should respond in ~100ms if warm.
            contract_timeout = max(
                1.0, float(os.getenv("JARVIS_CLOUD_CONTRACT_TIMEOUT", "8.0"))
            )
            for endpoint, source in candidates:
                if not self._cloud_endpoint_probe_allowed(endpoint):
                    logger.debug(
                        "Skipping cloud endpoint %s (source=%s): endpoint backoff active",
                        endpoint,
                        source,
                    )
                    continue

                contract_ok, contract_reason = await self._verify_cloud_endpoint_contract(
                    endpoint=endpoint,
                    timeout=contract_timeout,
                    force=True,
                )
                if contract_ok:
                    self._set_cloud_endpoint(endpoint, source)
                    self._use_cloud = True
                    backend_kind = self._classify_endpoint_backend(
                        self._cloud_endpoint or "",
                        self._cloud_endpoint_source,
                    )
                    self._apply_ecapa_backend_environment(
                        backend_kind,
                        self._cloud_endpoint,
                    )
                    logger.info(
                        f"☁️  Cloud routing activated for ML operations → {self._cloud_endpoint} "
                        f"(source={self._cloud_endpoint_source})"
                    )
                    return True

                self._record_cloud_endpoint_failure(
                    endpoint,
                    reason=f"Contract validation failed: {contract_reason}",
                )
                logger.warning(
                    f"⚠️ Rejected cloud endpoint {endpoint} (source={source}): {contract_reason}"
                )

            self._set_cloud_endpoint(None, "none")
            self._use_cloud = False
            self._apply_ecapa_backend_environment("local", None)
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

    def _acquire_cross_repo_state_lease(self) -> Optional[Dict[str, Any]]:
        """
        Acquire/renew file-based lease for cross-repo ECAPA state publication.

        v277.0: Adds epoch fencing so concurrent writers cannot silently
        clobber state with stale transitions.
        """
        import json

        now = time.time()
        ttl = max(5.0, float(os.getenv("JARVIS_ECAPA_STATE_LEASE_TTL", "15.0")))
        cross_repo_dir = Path.home() / ".jarvis" / "cross_repo"
        cross_repo_dir.mkdir(parents=True, exist_ok=True)
        lease_file = cross_repo_dir / "cloud_ecapa_state_lease.json"

        try:
            for _ in range(3):
                existing: Optional[Dict[str, Any]] = None
                if lease_file.exists():
                    try:
                        existing = json.loads(lease_file.read_text())
                    except Exception:
                        existing = None

                existing_holder = str((existing or {}).get("holder", "")).strip()
                existing_epoch = int((existing or {}).get("epoch", 0) or 0)
                existing_renewed = float((existing or {}).get("last_renewed", 0.0) or 0.0)
                lease_expired = (now - existing_renewed) > ttl

                if existing_holder and existing_holder != self._cross_repo_lease_holder and not lease_expired:
                    return None

                if existing_holder == self._cross_repo_lease_holder:
                    epoch = max(existing_epoch, self._cross_repo_lease_epoch)
                    acquired_at = float((existing or {}).get("acquired_at", now) or now)
                else:
                    epoch = max(existing_epoch + 1, self._cross_repo_lease_epoch + 1, 1)
                    acquired_at = now

                lease_payload = {
                    "holder": self._cross_repo_lease_holder,
                    "epoch": epoch,
                    "acquired_at": acquired_at,
                    "last_renewed": now,
                    "ttl": ttl,
                    "source_repo": "jarvis",
                    "pid": os.getpid(),
                }

                tmp_file = lease_file.with_suffix(".tmp")
                tmp_file.write_text(json.dumps(lease_payload, indent=2))
                tmp_file.rename(lease_file)

                observed = json.loads(lease_file.read_text())
                if (
                    str(observed.get("holder", "")).strip() == self._cross_repo_lease_holder
                    and int(observed.get("epoch", 0) or 0) == epoch
                ):
                    self._cross_repo_lease_epoch = epoch
                    return observed

            return None
        except Exception as e:
            logger.debug(f"[v277.0] Failed to acquire ECAPA state lease: {e}")
            return None

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

            lease = self._acquire_cross_repo_state_lease()
            if lease is None:
                logger.debug(
                    "[v277.0] Cross-repo ECAPA state write skipped: lease held by another process"
                )
                return

            self._cross_repo_epoch_seq += 1

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
                "version": "v277.0",
                # v276.4: Embedding parity tracking
                "parity": self.get_parity_status(),
                "routing_reason": self._last_routing_reason,
                # v276.5: local monotonic sequence (retained for compatibility)
                "state_seq": self._state_seq,
                "routing_policy": self._routing_policy.value,
                # v277.0: cross-process ordering
                "control_plane_holder": lease.get("holder"),
                "control_plane_epoch": int(lease.get("epoch", 0) or 0),
                "control_plane_seq": self._cross_repo_epoch_seq,
                "control_plane_lease_ttl": float(lease.get("ttl", 0.0) or 0.0),
                "control_plane_last_renewed": float(
                    lease.get("last_renewed", 0.0) or 0.0
                ),
            }

            tmp_file = state_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(state, indent=2))
            tmp_file.rename(state_file)

            logger.debug(
                "[v277.0] Cross-repo ECAPA state written: ready=%s epoch=%s seq=%s",
                is_ready,
                state["control_plane_epoch"],
                state["control_plane_seq"],
            )

        except Exception as e:
            logger.debug(f"[v277.0] Failed to write cross-repo ECAPA state: {e}")

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

            # v277.0: Cross-process staleness check using lease epoch + per-epoch seq.
            # Legacy writers may not include control_plane_* fields; keep compatibility.
            file_epoch = int(state.get("control_plane_epoch", 0) or 0)
            file_cp_seq = int(
                state.get(
                    "control_plane_seq",
                    state.get("state_seq", 0),
                )
                or 0
            )
            source_repo = str(state.get("source_repo", "unknown")).strip().lower()

            if source_repo == "jarvis":
                if file_epoch and file_epoch < self._cross_repo_lease_epoch:
                    logger.debug(
                        "[v277.0] Rejecting stale jarvis ECAPA state epoch "
                        f"({file_epoch} < {self._cross_repo_lease_epoch})"
                    )
                    return None
                if (
                    file_epoch
                    and file_epoch == self._cross_repo_lease_epoch
                    and file_cp_seq < self._cross_repo_epoch_seq
                ):
                    logger.debug(
                        "[v277.0] Rejecting stale jarvis ECAPA state seq "
                        f"({file_cp_seq} < {self._cross_repo_epoch_seq})"
                    )
                    return None

                # Update local watermark from accepted state.
                if file_epoch > self._cross_repo_lease_epoch:
                    self._cross_repo_lease_epoch = file_epoch
                    self._cross_repo_epoch_seq = file_cp_seq
                elif file_epoch == self._cross_repo_lease_epoch:
                    self._cross_repo_epoch_seq = max(
                        self._cross_repo_epoch_seq,
                        file_cp_seq,
                    )

            logger.info(f"[v115.0] Found recent cross-repo ECAPA state from {state.get('source_repo', 'unknown')} ({state_age:.1f}s ago)")
            return state

        except Exception as e:
            logger.debug(f"[v115.0] Failed to read cross-repo ECAPA state: {e}")
            return None

    async def _fallback_to_local_ecapa(self, reason: str) -> bool:
        """
        Fallback to local ECAPA loading when cloud is unavailable.

        v271.0: Memory-aware gate. REFUSES local load when system is in
        memory emergency (thrashing) or critical/emergency tier. Loading
        a 500MB+ ECAPA model during memory crisis would worsen the
        thrashing that other systems (UnifiedModelServing) are trying
        to resolve via GCP offload.

        Args:
            reason: Why we're falling back (for logging)

        Returns:
            True if local ECAPA was successfully loaded
        """
        # v271.0: Memory-aware gate — refuse local load during memory emergency.
        # The MemoryQuantizer singleton tracks pagein rates and thrash state,
        # which are FAR more reliable indicators of memory crisis than raw
        # vm_stat page counts. Loading 500MB+ during emergency = positive
        # feedback loop that worsens the crisis.
        try:
            import backend.core.memory_quantizer as _mq_mod
            from backend.core.memory_quantizer import MemoryTier

            _mq = _mq_mod._memory_quantizer_instance
            if _mq is not None:
                _thrash = _mq._thrash_state
                _tier = _mq.current_tier

                if _thrash == "emergency":
                    logger.warning(
                        "=" * 70 + "\n"
                        "   [v271.0] LOCAL ECAPA LOAD BLOCKED: memory thrash EMERGENCY\n"
                        f"   Pagein rate: {_mq._pagein_rate:.0f}/sec\n"
                        "   Loading 500MB+ model would worsen the crisis.\n"
                        "   Voice unlock will degrade gracefully until memory stabilizes.\n"
                        + "=" * 70
                    )
                    self._schedule_deferred_ecapa_recovery()
                    return False

                if _tier in (MemoryTier.CRITICAL, MemoryTier.EMERGENCY):
                    logger.warning(
                        "=" * 70 + "\n"
                        f"   [v271.0] LOCAL ECAPA LOAD BLOCKED: memory tier {_tier.value}\n"
                        "   Insufficient headroom for model loading.\n"
                        "   Voice unlock will degrade gracefully until memory stabilizes.\n"
                        + "=" * 70
                    )
                    self._schedule_deferred_ecapa_recovery()
                    return False

                if _thrash == "thrashing":
                    logger.warning(
                        f"[v271.0] Memory thrashing detected (pageins/sec: {_mq._pagein_rate:.0f}). "
                        "Proceeding with local ECAPA load cautiously."
                    )
        except ImportError:
            pass
        except Exception as _e:
            logger.debug(f"[v271.0] MemoryQuantizer check failed (proceeding cautiously): {_e}")

        # Before loading ~700MB locally, probe Docker container — it may
        # be healthy even though Cloud Run / GCP failed.  Docker is local
        # (zero network hop) and uses its own memory (Docker VM), so
        # routing to it avoids the in-process memory cost entirely.
        _docker_active = os.getenv(
            "JARVIS_DOCKER_ECAPA_ACTIVE", "",
        ).lower() in ("1", "true", "yes")
        _docker_backend = os.getenv("JARVIS_ECAPA_BACKEND", "") == "docker"
        if _docker_active or _docker_backend:
            try:
                _ecapa_port = int(os.getenv("JARVIS_ECAPA_PORT", "8015"))
                _docker_url = f"http://127.0.0.1:{_ecapa_port}"
                import aiohttp as _fb_aiohttp
                async with _fb_aiohttp.ClientSession(
                    timeout=_fb_aiohttp.ClientTimeout(total=5.0),
                ) as _fb_sess:
                    async with _fb_sess.get(f"{_docker_url}/health") as _fb_resp:
                        if _fb_resp.status == 200:
                            _fb_data = await _fb_resp.json()
                            if _fb_data.get("ecapa_ready"):
                                logger.info(
                                    "🐳 Docker ECAPA is healthy — routing to "
                                    "container instead of loading locally"
                                )
                                self._use_cloud = True
                                self._set_cloud_endpoint(
                                    _docker_url,
                                    "docker_container_fallback",
                                )
                                self._cloud_verified = True
                                self._apply_ecapa_backend_environment(
                                    "docker",
                                    self._cloud_endpoint,
                                )
                                return True
            except Exception as _docker_err:
                logger.debug(
                    f"Docker ECAPA probe failed in fallback: {_docker_err}"
                )

        logger.warning("=" * 70)
        logger.warning("🔄 CLOUD FALLBACK: Attempting local ECAPA load")
        logger.warning("=" * 70)
        logger.warning(f"   Reason: {reason}")
        logger.warning("   Warning: This may cause memory pressure!")
        logger.warning("=" * 70)

        # Disable cloud mode
        self._use_cloud = False
        self._cloud_verified = False
        self._apply_ecapa_backend_environment("local", None)

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

    # ── v276.5: Cross-process recovery fence ─────────────────────────────

    def _acquire_recovery_fence(self, source: str) -> bool:
        """
        v276.5: File-based cross-process idempotency token.

        Before starting recovery, write a fence token with our PID +
        timestamp + source. If another process's fence is still live
        (age < _recovery_fence_ttl), skip recovery to prevent duplicates.

        Returns True if we own the fence. Callers MUST call
        _release_recovery_fence() when done (success or failure).
        """
        import json
        from pathlib import Path

        fence_file = Path.home() / ".jarvis" / "cross_repo" / "ecapa_recovery_fence.json"
        try:
            fence_file.parent.mkdir(parents=True, exist_ok=True)

            # Check existing fence
            if fence_file.exists():
                try:
                    existing = json.loads(fence_file.read_text())
                    fence_age = time.time() - existing.get("timestamp", 0)
                    fence_pid = existing.get("pid", 0)

                    if fence_age < self._recovery_fence_ttl:
                        # Check if fencing process is still alive
                        process_alive = False
                        try:
                            os.kill(fence_pid, 0)  # Signal 0 = existence check
                            process_alive = True
                        except (OSError, ProcessLookupError):
                            pass

                        if process_alive and fence_pid != os.getpid():
                            logger.info(
                                f"[v276.5] Recovery fence held by PID {fence_pid} "
                                f"({fence_age:.1f}s ago, source={existing.get('source', '?')}). "
                                f"Skipping recovery (our source={source})."
                            )
                            return False
                        # Stale fence from dead process — take over
                except (json.JSONDecodeError, KeyError):
                    pass  # Corrupted fence file — overwrite

            # Write our fence
            fence = {
                "pid": os.getpid(),
                "timestamp": time.time(),
                "source": source,
                "state_seq": self._state_seq,
            }
            tmp = fence_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(fence))
            tmp.rename(fence_file)
            return True

        except Exception as e:
            logger.debug(f"[v276.5] Recovery fence error (proceeding anyway): {e}")
            return True  # Fail-open: don't block recovery on fence IO errors

    def _release_recovery_fence(self) -> None:
        """v276.5: Release cross-process recovery fence."""
        from pathlib import Path

        try:
            fence_file = Path.home() / ".jarvis" / "cross_repo" / "ecapa_recovery_fence.json"
            if fence_file.exists():
                import json
                existing = json.loads(fence_file.read_text())
                # Only remove if WE own it
                if existing.get("pid") == os.getpid():
                    fence_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _next_state_seq(self) -> int:
        """v276.5: Monotonically increment and return state sequence number."""
        self._state_seq += 1
        return self._state_seq

    async def _attempt_ecapa_recovery(self, source: str = "deferred") -> bool:
        """
        v276.2→v276.3: Unified ECAPA recovery — cloud-first, then local.

        Hardened with:
        - Idempotency lock: prevents concurrent recovery from deferred loop +
          memory callback racing each other. Single-writer on routing state.
        - Hysteresis dwell: refuses to attempt recovery if the last attempt was
          < DWELL_SECONDS ago. Prevents state-flapping when memory oscillates
          between CRITICAL↔ELEVATED rapidly.
        - Semantic readiness: after cloud verification, runs an actual embedding
          extraction test to confirm the model is truly inference-ready (not just
          process-ready from startup probe).
        - Structured reason codes: every decision logged with a machine-parseable
          reason code for forensic clarity.

        Strategy:
            1. TRY CLOUD FIRST — warm by now after 30s+ cold start completion.
            2. If cloud fails AND memory allows, try local.
            3. Both fail → return False (caller decides retry policy).
        """
        # ── Guard: already ready ──
        if self.is_ready:
            self._last_routing_reason = "already_ready"
            return True

        # ── Guard: startup phase contention gate ──
        # During supervisor startup, defer non-critical ECAPA recovery loops
        # so they do not contend with heavy integration phases.
        if not self._startup_phase_allows_ecapa_recovery(source):
            self._last_routing_reason = "startup_phase_gated"
            self._record_routing_decision(RouteDecisionReason.STARTUP_PHASE_GATED)
            logger.info(
                f"[v277.0] ECAPA recovery gated during startup "
                f"(source={source}, phase={self._read_supervisor_system_phase()})"
            )
            return False

        # ── Guard: idempotency lock (non-blocking check) ──
        if self._recovery_lock.locked():
            logger.debug(
                f"[v276.3] Recovery already in progress — skipping (source={source})"
            )
            self._last_routing_reason = "recovery_in_progress"
            return False

        # ── Guard: cross-process fence (v276.5) ──
        if not self._acquire_recovery_fence(source):
            self._last_routing_reason = "cross_process_fenced"
            self._record_routing_decision(RouteDecisionReason.CROSS_PROCESS_FENCED)
            return False

        # ── Guard: hysteresis dwell window ──
        dwell_seconds = float(os.getenv(
            "JARVIS_ECAPA_RECOVERY_DWELL_SECONDS", "10.0"
        ))
        elapsed = time.time() - self._last_recovery_attempt_at
        if elapsed < dwell_seconds and self._last_recovery_result is not None:
            logger.debug(
                f"[v276.3] Hysteresis dwell: {elapsed:.1f}s < {dwell_seconds}s "
                f"since last attempt (result={self._last_recovery_result}, "
                f"source={source}) — skipping"
            )
            self._last_routing_reason = "hysteresis_dwell"
            self._record_routing_decision(RouteDecisionReason.HYSTERESIS_DWELL)
            self._release_recovery_fence()
            return self._last_recovery_result

        async with self._recovery_lock:
            # Double-check after acquiring lock (another thread may have recovered)
            if self.is_ready:
                self._last_routing_reason = "already_ready_post_lock"
                self._release_recovery_fence()
                return True

            self._last_recovery_attempt_at = time.time()

            logger.info(
                f"[v276.3] ECAPA recovery started (source={source}) — "
                "cloud-first strategy"
            )

            # ── Phase 1: Try cloud ──
            cloud_reason = "not_attempted"
            cloud_success = False
            try:
                cloud_success = await self._fallback_to_cloud(
                    f"recovery (source={source})"
                )
                if cloud_success:
                    cloud_reason = "cloud_warm_ready"
                else:
                    cloud_reason = "cloud_verification_failed"
            except asyncio.TimeoutError:
                # Classify: is this timeout under host stress?
                _cpu_pct = 0.0
                try:
                    import psutil
                    _cpu_pct = psutil.cpu_percent(interval=None)
                except Exception:
                    pass
                if _cpu_pct > 85.0:
                    cloud_reason = "cloud_timeout_under_host_stress"
                    logger.warning(
                        f"[v276.3] Cloud probe timed out under CPU stress "
                        f"({_cpu_pct:.0f}%) — not a backend failure"
                    )
                else:
                    cloud_reason = "cloud_timeout_clean"
            except Exception as e:
                cloud_reason = f"cloud_exception:{type(e).__name__}"
                logger.warning(f"[v276.3] Cloud recovery error: {e}")

            if cloud_success:
                # ── Semantic readiness: verify actual inference capability ──
                semantic_ok = await self._verify_semantic_readiness(backend="cloud")
                if not semantic_ok:
                    cloud_success = False
                    cloud_reason = "cloud_semantic_readiness_failed"
                    self._record_routing_decision(RouteDecisionReason.SEMANTIC_READINESS_FAILED)
                    logger.warning(
                        "[v276.3] Cloud passed health check but failed semantic "
                        "readiness (embedding test). Falling through to local."
                    )
                else:
                    self._record_routing_decision(RouteDecisionReason.SEMANTIC_READINESS_OK)

            if cloud_success:
                self._ready_event.set()
                self._last_recovery_result = True
                self._last_routing_reason = cloud_reason
                # v276.4: Reconcile all state atomically + record transition
                self._reconcile_state_after_recovery("cloud", source)
                self._record_routing_decision(RouteDecisionReason.RECOVERY_SUCCESS_CLOUD)
                logger.info(
                    f"[v276.3] ECAPA recovery SUCCESSFUL via cloud "
                    f"(source={source}, reason={cloud_reason})"
                )
                # v276.4: Capture cloud fingerprint for parity tracking
                try:
                    self._cloud_parity = await self._fetch_cloud_parity_fingerprint()
                except Exception:
                    pass  # Parity is informational, don't block recovery
                self._release_recovery_fence()
                return True

            # ── Phase 2: Check if local loading is safe ──
            local_reason = "not_attempted"
            local_allowed = False
            try:
                import backend.core.memory_quantizer as _mq_mod
                from backend.core.memory_quantizer import MemoryTier

                _mq = _mq_mod._memory_quantizer_instance
                if _mq is not None:
                    _thrash = _mq._thrash_state
                    _tier = _mq.current_tier
                    if _thrash == "healthy" and _tier not in (
                        MemoryTier.CRITICAL, MemoryTier.EMERGENCY
                    ):
                        local_allowed = True
                        local_reason = "memory_healthy"
                    else:
                        local_reason = (
                            f"memory_blocked_local:thrash={_thrash},tier={_tier.value}"
                        )
                else:
                    use_cloud, _, _ = MLConfig.check_memory_pressure()
                    local_allowed = not use_cloud
                    local_reason = "mlconfig_fallback"
            except ImportError:
                use_cloud, _, _ = MLConfig.check_memory_pressure()
                local_allowed = not use_cloud
                local_reason = "mlconfig_fallback_no_mq"
            except Exception:
                use_cloud, _, _ = MLConfig.check_memory_pressure()
                local_allowed = not use_cloud
                local_reason = "mlconfig_fallback_exception"

            if local_allowed:
                logger.info(
                    f"[v276.3] Cloud failed ({cloud_reason}), trying local "
                    f"(source={source}, local_reason={local_reason})"
                )
                try:
                    local_success = await self._fallback_to_local_ecapa(
                        f"recovery after cloud failure (source={source})"
                    )
                    if local_success:
                        # Semantic readiness for local
                        semantic_ok = await self._verify_semantic_readiness(
                            backend="local"
                        )
                        if semantic_ok:
                            self._record_routing_decision(RouteDecisionReason.SEMANTIC_READINESS_OK)
                            self._ready_event.set()
                            self._last_recovery_result = True
                            self._last_routing_reason = "local_loaded"
                            # v276.4: Reconcile all state atomically + record transition
                            self._reconcile_state_after_recovery("local", source)
                            self._record_routing_decision(RouteDecisionReason.RECOVERY_SUCCESS_LOCAL)
                            logger.info(
                                f"[v276.3] ECAPA recovery SUCCESSFUL via local "
                                f"(source={source})"
                            )
                            # v276.4: Capture local fingerprint for parity
                            self._local_parity = self._get_local_parity_fingerprint()
                            self._release_recovery_fence()
                            return True
                        else:
                            self._record_routing_decision(RouteDecisionReason.SEMANTIC_READINESS_FAILED)
                            local_reason = "local_semantic_readiness_failed"
                    else:
                        local_reason = "local_load_failed"
                except Exception as e:
                    local_reason = f"local_exception:{type(e).__name__}"
                    logger.warning(f"[v276.3] Local recovery error: {e}")

            # ── Both failed ──
            self._last_recovery_result = False
            self._last_routing_reason = (
                f"both_unavailable:cloud={cloud_reason},local={local_reason}"
            )
            self._record_routing_decision(RouteDecisionReason.RECOVERY_FAILED)
            logger.warning(
                f"[v276.3] ECAPA recovery failed (source={source}) — "
                f"cloud={cloud_reason}, local={local_reason}"
            )
            self._release_recovery_fence()
            return False

    async def _verify_semantic_readiness(self, backend: str = "cloud") -> bool:
        """
        v276.3: Verify ECAPA is truly inference-ready by running a test embedding.

        A startup probe passing (HTTP 200) means the process is running, but
        the ECAPA model may still be loading tensors, warming JIT caches, or
        have a corrupted model state. This test sends a small synthetic audio
        sample and verifies a valid 192-dimensional embedding is returned.

        Args:
            backend: "cloud" or "local" — determines which path to test

        Returns:
            True if a valid embedding was extracted successfully
        """
        semantic_timeout = float(os.getenv(
            "JARVIS_ECAPA_SEMANTIC_READINESS_TIMEOUT", "10.0"
        ))

        try:
            if backend == "cloud" and self._use_cloud and self._cloud_endpoint:
                # Test cloud endpoint with a synthetic embedding request
                import aiohttp
                import struct

                # Generate minimal valid WAV header + 1s of silence at 16kHz
                # (44 byte header + 32000 bytes of 16-bit PCM silence)
                sample_rate = 16000
                num_samples = sample_rate  # 1 second
                wav_data = bytearray()
                # RIFF header
                data_size = num_samples * 2  # 16-bit = 2 bytes per sample
                wav_data.extend(b'RIFF')
                wav_data.extend(struct.pack('<I', 36 + data_size))
                wav_data.extend(b'WAVE')
                wav_data.extend(b'fmt ')
                wav_data.extend(struct.pack('<IHHIIHH', 16, 1, 1, sample_rate,
                                           sample_rate * 2, 2, 16))
                wav_data.extend(b'data')
                wav_data.extend(struct.pack('<I', data_size))
                wav_data.extend(b'\x00' * data_size)

                endpoint = self._cloud_endpoint.rstrip("/")
                # Try known embedding routes
                for route in self._cloud_route_candidates("embedding"):
                    url = f"{endpoint}{route}"
                    try:
                        timeout = aiohttp.ClientTimeout(total=semantic_timeout)
                        async with aiohttp.ClientSession(timeout=timeout) as session:
                            async with session.post(
                                url,
                                data=bytes(wav_data),
                                headers={"Content-Type": "audio/wav"},
                            ) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    embedding = data.get("embedding", [])
                                    if isinstance(embedding, list) and len(embedding) >= 64:
                                        # v276.4: Capture actual embedding dim
                                        # from live inference for parity tracking
                                        if self._cloud_parity.embedding_dim == 0:
                                            self._cloud_parity.embedding_dim = len(embedding)
                                            self._cloud_parity.captured_at = time.time()
                                        logger.debug(
                                            f"[v276.3] Cloud semantic readiness OK "
                                            f"(embedding dim={len(embedding)})"
                                        )
                                        return True
                                    logger.warning(
                                        f"[v276.3] Cloud returned invalid embedding: "
                                        f"len={len(embedding) if isinstance(embedding, list) else 'N/A'}"
                                    )
                                    return False
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"[v276.3] Semantic readiness timeout ({semantic_timeout}s) "
                            f"on {route}"
                        )
                    except Exception as e:
                        logger.debug(f"[v276.3] Semantic test on {route}: {e}")
                        continue

                logger.warning("[v276.3] Cloud semantic readiness: no working route found")
                return False

            elif backend == "local":
                # v276.5: Full semantic readiness — actually extract embedding
                # from synthetic audio, not just check is_loaded flag.
                # A loaded model with corrupt weights or misconfigured
                # preprocessing passes is_loaded but fails inference.
                ecapa = self._engines.get("ecapa_tdnn")
                if ecapa is None or not ecapa.is_loaded:
                    return False

                try:
                    import struct
                    import numpy as np

                    # Generate 1s 16kHz low-amplitude noise WAV.
                    # Silence (all zeros) produces near-zero embeddings that
                    # fail the standard zero-norm validation in extract_speaker_embedding.
                    # Matches warmup pattern (np.random.randn * 0.3).
                    _sr = 16000
                    _ns = _sr
                    _wav = bytearray()
                    _ds = _ns * 2
                    _wav.extend(b'RIFF')
                    _wav.extend(struct.pack('<I', 36 + _ds))
                    _wav.extend(b'WAVEfmt ')
                    _wav.extend(struct.pack('<IHHIIHH', 16, 1, 1, _sr,
                                            _sr * 2, 2, 16))
                    _wav.extend(b'data')
                    _wav.extend(struct.pack('<I', _ds))
                    _noise = (np.random.randn(_ns).astype(np.float32) * 0.1 * 32767).astype(np.int16)
                    _wav.extend(_noise.tobytes())

                    test_result = await asyncio.wait_for(
                        ecapa.extract_speaker_embedding(bytes(_wav)),
                        timeout=semantic_timeout,
                    )
                    if test_result is None:
                        logger.warning(
                            "[v276.5] Local semantic test: extraction returned None"
                        )
                        return False

                    emb_len = len(test_result) if hasattr(test_result, '__len__') else 0
                    if emb_len < 64:
                        logger.warning(
                            f"[v276.5] Local semantic test: embedding too short "
                            f"(dim={emb_len}, expected >= 64)"
                        )
                        return False

                    # Capture local embedding dim for parity tracking
                    if self._local_parity.embedding_dim == 0:
                        self._local_parity.embedding_dim = emb_len
                        self._local_parity.captured_at = time.time()

                    logger.debug(
                        f"[v276.5] Local semantic readiness OK "
                        f"(embedding dim={emb_len})"
                    )
                    return True

                except asyncio.TimeoutError:
                    logger.warning(
                        f"[v276.5] Local semantic test timed out "
                        f"({semantic_timeout}s)"
                    )
                    return False
                except Exception as e:
                    logger.warning(
                        f"[v276.5] Local semantic test extraction failed: {e}"
                    )
                    return False

            else:
                logger.debug(f"[v276.3] Semantic readiness: unknown backend '{backend}'")
                return False

        except Exception as e:
            logger.warning(f"[v276.3] Semantic readiness check failed: {e}")
            return False

    # ── v276.4: Embedding Parity Version Tracking ──────────────────────────

    async def _fetch_cloud_parity_fingerprint(self) -> _ParityFingerprint:
        """
        v276.4: Fetch ECAPA model identity from the cloud /status endpoint.

        Queries the cloud service's detailed status API to extract:
        - embedding_dim from prebaked manifest
        - sample_rate from config
        - model_source from config.model_path
        - service_version from top-level version
        - load_strategy from model.model_cache.load_source

        Returns a populated _ParityFingerprint or a default (empty) one on failure.
        """
        fp = _ParityFingerprint(backend="cloud")
        if not self._cloud_endpoint:
            return fp

        endpoint = self._cloud_endpoint.rstrip("/")
        # Try /status first (richer metadata), fall back to /api/ml/health
        status_paths = ["/status", "/api/ml/status", "/api/ml/health", "/health"]
        parity_timeout = float(os.getenv(
            "JARVIS_ECAPA_PARITY_FETCH_TIMEOUT", "8.0"
        ))

        try:
            import aiohttp

            timeout_cfg = aiohttp.ClientTimeout(total=parity_timeout)
            async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                for path in status_paths:
                    url = f"{endpoint}{path}"
                    try:
                        async with session.get(
                            url, headers={"Accept": "application/json"}
                        ) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()

                            # Extract from /status response structure
                            fp.raw_metadata = data
                            fp.captured_at = time.time()

                            # Service version — top level
                            fp.service_version = str(
                                data.get("version", "")
                            )

                            # Config section → sample_rate, model_path
                            config = data.get("config", {})
                            if isinstance(config, dict):
                                fp.sample_rate = int(
                                    config.get("sample_rate", 0)
                                    or config.get("SAMPLE_RATE", 0)
                                    or 0
                                )
                                fp.model_source = str(
                                    config.get("model_path", "")
                                    or config.get("model_source", "")
                                )

                            # Model section → load_source
                            model_section = data.get("model", {})
                            if isinstance(model_section, dict):
                                model_cache = model_section.get(
                                    "model_cache", {}
                                )
                                if isinstance(model_cache, dict):
                                    fp.load_strategy = str(
                                        model_cache.get("load_source", "")
                                    )

                            # Prebaked manifest → embedding_dim
                            manifest = data.get("prebaked_manifest", {})
                            if isinstance(manifest, dict):
                                fp.embedding_dim = int(
                                    manifest.get("embedding_dim", 0) or 0
                                )

                            # Fallback: if /health response, extract what we can
                            if not fp.service_version:
                                fp.service_version = str(
                                    data.get("version", "")
                                )

                            # If we got embedding_dim or sample_rate, this
                            # response was useful. If /health only, try harder
                            # with next path for richer data.
                            if fp.embedding_dim > 0 or fp.sample_rate > 0:
                                break

                    except asyncio.TimeoutError:
                        logger.debug(
                            f"[v276.4] Parity fetch timeout on {path}"
                        )
                    except Exception as e:
                        logger.debug(
                            f"[v276.4] Parity fetch error on {path}: {e}"
                        )

            # If /status didn't have embedding_dim, fall back to ECAPA default
            if fp.embedding_dim == 0 and fp.service_version:
                # Known ECAPA-TDNN always produces 192-dim embeddings
                fp.embedding_dim = 192
            if fp.sample_rate == 0 and fp.service_version:
                # Known ECAPA-TDNN always uses 16kHz
                fp.sample_rate = 16000

        except ImportError:
            logger.debug("[v276.4] aiohttp not available for parity fetch")
        except Exception as e:
            logger.debug(f"[v276.4] Cloud parity fingerprint fetch failed: {e}")

        return fp

    def _get_local_parity_fingerprint(self) -> _ParityFingerprint:
        """
        v276.4: Construct ECAPA model identity from local engine state.

        Local ECAPA always uses SpeechBrain's spkrec-ecapa-voxceleb model
        with 192-dim embeddings at 16kHz. The fingerprint captures these
        constants plus the current engine state for forensic comparison.
        """
        fp = _ParityFingerprint(backend="local")
        fp.captured_at = time.time()

        ecapa = self._engines.get("ecapa_tdnn")
        if ecapa is None:
            return fp

        # ECAPA-TDNN constants (same as cloud service)
        fp.embedding_dim = 192
        fp.sample_rate = 16000
        fp.model_source = "speechbrain/spkrec-ecapa-voxceleb"
        fp.load_strategy = "speechbrain"

        # Try to extract more detail from the engine wrapper
        try:
            if hasattr(ecapa, "_model") and getattr(ecapa, "_model", None) is not None:
                fp.service_version = "local"
                # Try to get SpeechBrain version for forensics
                try:
                    import speechbrain
                    fp.raw_metadata["speechbrain_version"] = str(
                        getattr(speechbrain, "__version__", "unknown")
                    )
                except ImportError:
                    pass
                # Try to get PyTorch version
                try:
                    import torch
                    fp.raw_metadata["torch_version"] = str(torch.__version__)
                except ImportError:
                    pass
        except Exception:
            pass

        return fp

    async def _check_embedding_parity(
        self,
        force: bool = False,
    ) -> Tuple[bool, str]:
        """
        v276.4: Compare cloud and local ECAPA fingerprints for compatibility.

        This detects version drift that causes confidence instability:
        - Different embedding dimensions → cosine similarity meaningless
        - Different sample rates → feature extraction mismatch
        - Different model sources → different embedding spaces

        Args:
            force: If True, re-fetch fingerprints even if recently checked.

        Returns:
            (compatible, reason) — reason explains mismatch if incompatible,
            or describes the parity state if compatible.
        """
        parity_ttl = float(os.getenv(
            "JARVIS_ECAPA_PARITY_CHECK_TTL", "300.0"
        ))

        # Skip if recently checked (unless forced)
        if (
            not force
            and self._parity_compatible is not None
            and (time.time() - self._parity_last_checked) < parity_ttl
        ):
            return self._parity_compatible, self._parity_last_reason

        # Fetch cloud fingerprint (async)
        cloud_fp = await self._fetch_cloud_parity_fingerprint()
        # Get local fingerprint (sync — just reads local state)
        local_fp = self._get_local_parity_fingerprint()

        # Store for observability
        self._cloud_parity = cloud_fp
        self._local_parity = local_fp
        self._parity_last_checked = time.time()

        # If only one backend is populated, parity check is not applicable
        if not cloud_fp.is_populated() and not local_fp.is_populated():
            self._parity_compatible = None
            self._parity_last_reason = "both_fingerprints_empty"
            return True, self._parity_last_reason

        if not cloud_fp.is_populated():
            self._parity_compatible = None
            self._parity_last_reason = "cloud_fingerprint_empty"
            # Not a parity failure — cloud just isn't available
            return True, self._parity_last_reason

        if not local_fp.is_populated():
            self._parity_compatible = None
            self._parity_last_reason = "local_fingerprint_empty"
            # Not a parity failure — local just isn't loaded
            return True, self._parity_last_reason

        # Both populated — compare
        compatible, reason = cloud_fp.is_compatible_with(local_fp)
        self._parity_compatible = compatible
        self._parity_last_reason = reason

        if not compatible:
            logger.error(
                f"[v276.4] ECAPA PARITY DRIFT DETECTED: {reason}. "
                f"Cloud and local embeddings are NOT compatible. "
                f"Speaker verification results will be inconsistent. "
                f"Cloud: {cloud_fp.to_dict()}, Local: {local_fp.to_dict()}"
            )
            # v276.4: Enforce routing policy — don't just warn, ACT.
            # Use the backend with the canonical embedding dim (192).
            self._enforce_parity_routing_policy(cloud_fp, local_fp, reason)
        else:
            logger.info(
                f"[v276.4] ECAPA parity OK — cloud and local compatible "
                f"(dim={cloud_fp.embedding_dim}, rate={cloud_fp.sample_rate}, "
                f"model={cloud_fp.model_source or 'default'})"
            )
            # Clear any parity-driven routing override
            if self._routing_policy_reason.startswith("parity_mismatch"):
                self._routing_policy = RoutingPolicy.AUTO
                self._routing_policy_reason = "parity_restored"
                self._record_routing_decision(RouteDecisionReason.PARITY_RESTORED)
                logger.info("[v276.4] Parity restored — routing policy reset to AUTO")

        return compatible, reason

    def _enforce_parity_routing_policy(
        self,
        cloud_fp: _ParityFingerprint,
        local_fp: _ParityFingerprint,
        mismatch_reason: str,
    ) -> None:
        """
        v276.4: Enforce deterministic routing when cloud/local embeddings diverge.

        The canonical ECAPA-TDNN produces 192-dim embeddings at 16kHz from
        'speechbrain/spkrec-ecapa-voxceleb'. Whichever backend matches the
        canonical spec gets traffic. If neither matches or both are wrong,
        enter DEGRADED mode.

        This is NOT a warning — it changes live routing immediately.
        """
        # v276.4: Respect operator env var override — do NOT overwrite
        # a policy that was explicitly set via JARVIS_ECAPA_ROUTING_POLICY.
        if self._routing_policy_reason.startswith("env_override"):
            logger.info(
                f"[v276.4] Parity mismatch detected ({mismatch_reason}) but "
                f"routing policy is operator-locked ({self._routing_policy_reason}). "
                f"Skipping automatic routing policy change."
            )
            return

        # v276.5: Parity strictness per environment
        self._record_routing_decision(RouteDecisionReason.PARITY_MISMATCH)
        if self._parity_strictness == ParityStrictness.WARN:
            logger.warning(
                f"[v276.5] Parity mismatch ({mismatch_reason}) — strictness=warn, "
                f"no routing change applied. Set JARVIS_ECAPA_PARITY_STRICTNESS=enforce "
                f"to change routing on mismatch."
            )
            return
        if self._parity_strictness == ParityStrictness.DEGRADE:
            logger.warning(
                f"[v276.5] Parity mismatch ({mismatch_reason}) — strictness=degrade. "
                f"Entering DEGRADED mode without forcing single-backend routing."
            )
            self._routing_policy = RoutingPolicy.DEGRADED
            self._routing_policy_reason = (
                f"parity_mismatch:degrade ({mismatch_reason})"
            )
            return

        # strictness == ENFORCE (prod default) — full routing policy change
        CANONICAL_DIM = 192
        CANONICAL_RATE = 16000
        CANONICAL_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"

        def _matches_canonical(fp: _ParityFingerprint) -> bool:
            if fp.embedding_dim != CANONICAL_DIM:
                return False
            if fp.sample_rate != CANONICAL_RATE:
                return False
            # model_source may be empty if not reported — don't penalize
            if fp.model_source and CANONICAL_SOURCE not in fp.model_source:
                return False
            return True

        cloud_canonical = _matches_canonical(cloud_fp)
        local_canonical = _matches_canonical(local_fp)

        if cloud_canonical and not local_canonical:
            self._routing_policy = RoutingPolicy.CLOUD_ONLY
            self._routing_policy_reason = (
                f"parity_mismatch:local_divergent ({mismatch_reason})"
            )
            logger.warning(
                f"[v276.4] Routing policy → CLOUD_ONLY: local backend diverged "
                f"from canonical ECAPA spec ({mismatch_reason})"
            )
        elif local_canonical and not cloud_canonical:
            self._routing_policy = RoutingPolicy.LOCAL_ONLY
            self._routing_policy_reason = (
                f"parity_mismatch:cloud_divergent ({mismatch_reason})"
            )
            logger.warning(
                f"[v276.4] Routing policy → LOCAL_ONLY: cloud backend diverged "
                f"from canonical ECAPA spec ({mismatch_reason})"
            )
        elif cloud_canonical and local_canonical:
            # Both match canonical but differ from each other — should not happen
            # unless comparison has a bug. Default to AUTO.
            self._routing_policy = RoutingPolicy.AUTO
            self._routing_policy_reason = (
                f"parity_mismatch:both_canonical_but_different ({mismatch_reason})"
            )
            logger.warning(
                f"[v276.4] Both backends match canonical spec but differ "
                f"from each other — routing policy stays AUTO ({mismatch_reason})"
            )
        else:
            # Neither matches canonical — degraded mode
            self._routing_policy = RoutingPolicy.DEGRADED
            self._routing_policy_reason = (
                f"parity_mismatch:both_non_canonical ({mismatch_reason})"
            )
            logger.error(
                f"[v276.4] DEGRADED: Neither cloud nor local matches canonical "
                f"ECAPA spec. Embedding results will be unreliable. "
                f"({mismatch_reason})"
            )

    def resolve_effective_backend(
        self,
        prefer_cloud: Optional[bool] = None,
    ) -> Tuple[str, str]:
        """
        v276.4: Central routing decision point.

        Combines routing policy, flap dampening, and caller preference
        into a single deterministic backend choice. ALL embedding extraction
        and verification paths MUST use this instead of raw `is_using_cloud`.

        Returns:
            (backend, reason) — backend is "cloud", "local", or "best_effort"
        """
        # 1. Operator env var override is absolute
        if self._routing_policy != RoutingPolicy.AUTO:
            if self._routing_policy == RoutingPolicy.CLOUD_ONLY:
                self._record_routing_decision(RouteDecisionReason.POLICY_CLOUD_ONLY)
                return "cloud", f"policy:{self._routing_policy_reason}"
            elif self._routing_policy == RoutingPolicy.LOCAL_ONLY:
                self._record_routing_decision(RouteDecisionReason.POLICY_LOCAL_ONLY)
                return "local", f"policy:{self._routing_policy_reason}"
            elif self._routing_policy == RoutingPolicy.DEGRADED:
                self._record_routing_decision(RouteDecisionReason.POLICY_DEGRADED)
                # In degraded mode, prefer whichever is available
                if self._use_cloud and self._cloud_endpoint:
                    return "cloud", f"degraded:cloud_available"
                return "local", f"degraded:local_fallback"

        # 2. Flap dampening locks to last-stable backend
        if self._flap_dampened:
            elapsed = time.time() - self._flap_dampened_at
            if elapsed < self._flap_dampen_duration:
                self._record_routing_decision(RouteDecisionReason.FLAP_DAMPENED)
                # Stay on whatever we're currently on
                current = "cloud" if self._use_cloud else "local"
                return current, f"flap_dampened:{elapsed:.0f}s/{self._flap_dampen_duration:.0f}s"
            else:
                # Dampen period expired — release
                self._flap_dampened = False
                logger.info(
                    f"[v276.4] Flap dampening released after "
                    f"{self._flap_dampen_duration:.0f}s"
                )

        # 3. Caller preference (per-call override)
        if prefer_cloud is True:
            return "cloud", "caller_preference:cloud"
        elif prefer_cloud is False:
            return "local", "caller_preference:local"

        # 4. Default routing based on current state
        if self._use_cloud and self._cloud_endpoint:
            return "cloud", "auto:cloud_active"
        return "local", "auto:local_active"

    def _record_backend_transition(self, new_backend: str) -> None:
        """
        v276.4: Record a backend transition for flap detection.

        If more than _flap_threshold transitions occur within _flap_window
        seconds, enter dampened mode to stabilize routing.
        """
        now = time.time()

        # Check if this is actually a transition (not same backend)
        if self._backend_transitions:
            last_backend, _ = self._backend_transitions[-1]
            if last_backend == new_backend:
                return  # Same backend — not a transition

        # Prune transitions outside the window BEFORE appending
        cutoff = now - self._flap_window
        self._backend_transitions = [
            (b, t) for b, t in self._backend_transitions if t >= cutoff
        ]
        self._backend_transitions.append((new_backend, now))

        # Check for flap condition
        if len(self._backend_transitions) >= self._flap_threshold:
            if not self._flap_dampened:
                self._flap_dampened = True
                self._flap_dampened_at = now
                logger.warning(
                    f"[v276.4] BACKEND FLAP DETECTED: "
                    f"{len(self._backend_transitions)} transitions in "
                    f"{self._flap_window:.0f}s (threshold={self._flap_threshold}). "
                    f"Dampening routing for {self._flap_dampen_duration:.0f}s. "
                    f"History: {[(b, f'{t:.0f}') for b, t in self._backend_transitions]}"
                )

    async def _periodic_deep_health_check(self) -> None:
        """
        v276.4: Background task that periodically validates the active ECAPA
        backend can actually produce embeddings (not just respond to /health).

        Detects:
        - Warm instance with corrupted model state
        - Memory leak causing silent quality degradation
        - Stale model cache on long-lived Cloud Run instance
        - Model loaded but inference hanging

        Runs semantic readiness test every N minutes. On failure, triggers
        recovery to switch to the other backend or reload.
        """
        interval = float(os.getenv(
            "JARVIS_ECAPA_DEEP_HEALTH_INTERVAL", "180.0"
        ))  # 3 minutes default
        max_consecutive_failures = int(os.getenv(
            "JARVIS_ECAPA_DEEP_HEALTH_MAX_FAILURES", "2"
        ))

        consecutive_failures = 0

        logger.info(
            f"[v276.4] Periodic deep health check started "
            f"(interval={interval}s, max_failures={max_consecutive_failures})"
        )

        while True:
            try:
                await asyncio.sleep(interval)

                # Skip if not ready (recovery handles this)
                if not self.is_ready:
                    consecutive_failures = 0
                    continue

                # Determine active backend
                active = "cloud" if (self._use_cloud and self._cloud_endpoint) else "local"

                # Run semantic readiness (actual embedding test)
                ok = await self._verify_semantic_readiness(backend=active)

                if ok:
                    consecutive_failures = 0
                    self._record_routing_decision(RouteDecisionReason.DEEP_HEALTH_OK)
                    logger.debug(
                        f"[v276.4] Deep health OK (backend={active})"
                    )
                    # Also refresh parity on success
                    if active == "cloud":
                        self._cloud_parity = await self._fetch_cloud_parity_fingerprint()
                        # v276.5: Check for warm-instance memory growth
                        try:
                            fp = self._cloud_parity
                            mem_mb = fp.raw_metadata.get("memory_mb", 0)
                            if mem_mb > 0:
                                self.record_cloud_memory(mem_mb)
                        except Exception:
                            pass
                    else:
                        self._local_parity = self._get_local_parity_fingerprint()
                    continue

                consecutive_failures += 1
                self._record_routing_decision(RouteDecisionReason.DEEP_HEALTH_FAILED)
                logger.warning(
                    f"[v276.4] Deep health FAILED for {active} "
                    f"(consecutive={consecutive_failures}/{max_consecutive_failures})"
                )

                if consecutive_failures >= max_consecutive_failures:
                    logger.error(
                        f"[v276.4] {active} backend failed {consecutive_failures} "
                        f"consecutive deep health checks — triggering recovery"
                    )
                    consecutive_failures = 0
                    # Trigger recovery (will try the OTHER backend)
                    try:
                        await self._attempt_ecapa_recovery(
                            source=f"deep_health_failure:{active}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[v276.4] Recovery after deep health failure: {e}"
                        )

            except asyncio.CancelledError:
                logger.info("[v276.4] Deep health check task cancelled")
                break
            except Exception as e:
                logger.debug(f"[v276.4] Deep health check error: {e}")
                await asyncio.sleep(30)  # Back off on unexpected error

    def _reconcile_state_after_recovery(
        self,
        new_backend: str,
        source: str,
    ) -> None:
        """
        v276.4: Reconcile all routing state after a backend transition.

        When switching between cloud and local, multiple flags must be
        updated atomically to prevent split-state where one subsystem
        sees 'cloud' while another sees 'local'.

        Args:
            new_backend: "cloud" or "local"
            source: What triggered the transition (for logging)
        """
        # v276.5: Bump monotonic state sequence FIRST — observers
        # that read the state file reject events with seq < their last seen.
        seq = self._next_state_seq()

        # Record transition for flap detection
        self._record_backend_transition(new_backend)

        if new_backend == "cloud":
            self._use_cloud = True
            self._memory_gate_blocked = False
            backend_kind = self._classify_endpoint_backend(
                self._cloud_endpoint or "",
                self._cloud_endpoint_source,
            )
            self._apply_ecapa_backend_environment(
                backend_kind,
                self._cloud_endpoint,
            )
            logger.info(
                f"[v276.5] State reconciled → cloud "
                f"(source={source}, seq={seq})"
            )
        elif new_backend == "local":
            self._use_cloud = False
            self._cloud_verified = False
            self._memory_gate_blocked = False
            self._apply_ecapa_backend_environment("local", None)
            logger.info(
                f"[v276.5] State reconciled → local "
                f"(source={source}, seq={seq})"
            )
        else:
            logger.warning(
                f"[v276.5] _reconcile_state_after_recovery called with "
                f"unexpected backend={new_backend!r} (source={source}). "
                f"No state changes applied — investigate caller."
            )
            return  # Don't write cross-repo state for unknown backend

        # Update cross-repo state file (async — fire-and-forget)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._write_cross_repo_ecapa_state(
                is_ready=self.is_ready,
                reason=f"reconciled:{new_backend}:{source}",
            ))
        except RuntimeError:
            pass  # No event loop — skip (e.g., sync test context)

    def get_routing_status(self) -> Dict[str, Any]:
        """
        v276.4: Return current routing state for health endpoints.

        Provides full observability into routing decisions including
        policy, flap state, parity, and active backend.
        """
        return {
            "routing_policy": self._routing_policy.value,
            "routing_policy_reason": self._routing_policy_reason,
            "active_backend": "cloud" if (self._use_cloud and self._cloud_endpoint) else "local",
            "flap_dampened": self._flap_dampened,
            "flap_transitions": len(self._backend_transitions),
            "flap_window": self._flap_window,
            "flap_threshold": self._flap_threshold,
            "parity": self.get_parity_status(),
            "last_routing_reason": self._last_routing_reason,
            "state_seq": self._state_seq,
            "parity_strictness": self._parity_strictness.value,
            "deep_health_running": (
                self._deep_health_task is not None
                and not self._deep_health_task.done()
            ),
            "telemetry": self.get_routing_telemetry(),
        }

    def get_parity_status(self) -> Dict[str, Any]:
        """
        v276.4: Return current embedding parity state for health endpoints.

        Exposes parity fingerprints and compatibility verdict to callers
        (health checks, dashboards, cross-repo state files).
        """
        return {
            "compatible": self._parity_compatible,
            "last_checked": self._parity_last_checked,
            "last_reason": self._parity_last_reason,
            "cloud": self._cloud_parity.to_dict(),
            "local": self._local_parity.to_dict(),
        }

    # ── v276.5: Telemetry + warm-instance degradation ───────────────────

    def _record_routing_decision(self, reason: RouteDecisionReason) -> None:
        """
        v276.5: Increment stable counter for a routing decision reason.

        Enables SLO tracking: route flaps/hour, degraded dwell time,
        recovery latency, parity mismatch duration. Counters are monotonic
        and never reset (except on process restart).
        """
        self._routing_decisions[reason.value] = (
            self._routing_decisions.get(reason.value, 0) + 1
        )

    def record_cloud_latency(self, latency_s: float) -> Optional[str]:
        """
        v276.5: Record a cloud inference latency sample. Returns a warning
        string if degradation is detected, None otherwise.

        Called from extract_speaker_embedding_cloud() success path.
        """
        samples = self._cloud_latency_samples
        samples.append(latency_s)

        # Sliding window
        if len(samples) > self._cloud_latency_window:
            samples[:] = samples[-self._cloud_latency_window:]

        # Establish baseline after first N samples
        if self._cloud_latency_p95_baseline == 0.0 and len(samples) >= 10:
            sorted_s = sorted(samples)
            p95_idx = int(len(sorted_s) * 0.95)
            self._cloud_latency_p95_baseline = sorted_s[min(p95_idx, len(sorted_s) - 1)]
            logger.info(
                f"[v276.5] Cloud latency baseline established: "
                f"P95={self._cloud_latency_p95_baseline*1000:.0f}ms "
                f"(from {len(samples)} samples)"
            )
            return None

        # Check degradation
        if self._cloud_latency_p95_baseline > 0.0 and len(samples) >= 10:
            sorted_s = sorted(samples[-20:])  # Recent window
            p95_idx = int(len(sorted_s) * 0.95)
            current_p95 = sorted_s[min(p95_idx, len(sorted_s) - 1)]
            threshold = self._cloud_latency_p95_baseline * self._cloud_latency_degradation_threshold

            if current_p95 > threshold:
                msg = (
                    f"Cloud ECAPA latency degraded: P95={current_p95*1000:.0f}ms "
                    f"(baseline={self._cloud_latency_p95_baseline*1000:.0f}ms, "
                    f"threshold={threshold*1000:.0f}ms)"
                )
                logger.warning(f"[v276.5] {msg}")
                self._record_routing_decision(RouteDecisionReason.DEEP_HEALTH_FAILED)
                return msg

        return None

    def record_cloud_memory(self, memory_mb: float) -> Optional[str]:
        """
        v276.5: Record cloud service memory usage. Returns a warning
        string if growth exceeds threshold, None otherwise.

        Called from deep health check when /status exposes memory info.
        """
        if self._cloud_memory_baseline_mb == 0.0:
            self._cloud_memory_baseline_mb = memory_mb
            return None

        growth = memory_mb - self._cloud_memory_baseline_mb
        if growth > self._cloud_memory_growth_threshold_mb:
            msg = (
                f"Cloud ECAPA memory growth detected: "
                f"{memory_mb:.0f}MB (baseline={self._cloud_memory_baseline_mb:.0f}MB, "
                f"growth={growth:.0f}MB, threshold={self._cloud_memory_growth_threshold_mb:.0f}MB)"
            )
            logger.warning(f"[v276.5] {msg}")
            return msg
        return None

    def get_routing_telemetry(self) -> Dict[str, Any]:
        """
        v276.5: Return routing decision counters for SLO monitoring.

        Includes: decision counts by reason, uptime, latency baselines,
        memory tracking, and parity strictness mode.
        """
        uptime = time.time() - self._routing_decisions_since
        total_decisions = sum(self._routing_decisions.values())
        flap_count = (
            self._routing_decisions.get(RouteDecisionReason.RECOVERY_SUCCESS_CLOUD.value, 0)
            + self._routing_decisions.get(RouteDecisionReason.RECOVERY_SUCCESS_LOCAL.value, 0)
        )
        return {
            "decisions": dict(self._routing_decisions),
            "total_decisions": total_decisions,
            "uptime_seconds": uptime,
            "flaps_per_hour": (flap_count / max(uptime, 1)) * 3600,
            "parity_strictness": self._parity_strictness.value,
            "cloud_latency": {
                "baseline_p95_ms": (
                    self._cloud_latency_p95_baseline * 1000
                    if self._cloud_latency_p95_baseline > 0 else None
                ),
                "samples_count": len(self._cloud_latency_samples),
                "degradation_factor": self._cloud_latency_degradation_threshold,
            },
            "cloud_memory": {
                "baseline_mb": self._cloud_memory_baseline_mb or None,
                "growth_threshold_mb": self._cloud_memory_growth_threshold_mb,
            },
        }

    def _schedule_deferred_ecapa_recovery(self) -> None:
        """
        v271.0→v276.2: Schedule background ECAPA recovery with cloud-first strategy.

        ROOT CAUSE OF RECURRING FAILURE (v271.0):
            Original implementation ONLY retried local ECAPA loading. When memory
            emergency blocked local load, recovery waited for memory to stabilize.
            Meanwhile, the Cloud Run container completed its cold start (10-30s)
            and was sitting warm and ready — but recovery never tried it.

        THE CURE (v276.2):
            Recovery now uses _attempt_ecapa_recovery() which tries CLOUD FIRST.
            By T+30s (first recovery poll), Cloud Run has been warm for 0-20s.
            The cloud probe succeeds immediately (~100ms) instead of timing out.

            Additionally, MemoryQuantizer recovery callbacks provide event-driven
            reload (registered in __init__), so recovery doesn't purely rely on
            this 30s poll loop.

        Strategy per attempt:
            1. Try cloud (warm by now) → success? done.
            2. If cloud fails AND memory safe → try local → success? done.
            3. Both fail → wait for next poll interval.
        """
        if self._deferred_ecapa_recovery_task is not None:
            return  # Already scheduled

        self._memory_gate_blocked = True

        async def _recovery_loop() -> None:
            poll_interval = float(os.getenv(
                "JARVIS_ECAPA_RECOVERY_POLL_INTERVAL", "30.0"
            ))
            startup_poll_interval = float(
                os.getenv("JARVIS_ECAPA_RECOVERY_STARTUP_POLL_INTERVAL", "5.0")
            )
            max_attempts = int(os.getenv(
                "JARVIS_ECAPA_RECOVERY_MAX_ATTEMPTS", "20"
            ))
            attempt = 0

            logger.info(
                f"[v276.2] Deferred ECAPA recovery scheduled — cloud-first strategy "
                f"(poll every {poll_interval}s, max {max_attempts} attempts)"
            )

            while attempt < max_attempts:
                if self._read_supervisor_system_phase() == "startup":
                    await asyncio.sleep(max(1.0, startup_poll_interval))
                    continue

                await asyncio.sleep(poll_interval)
                attempt += 1

                # Check if someone else already loaded ECAPA (e.g., memory callback)
                if self.is_ready:
                    logger.info(
                        "[v276.2] ECAPA already ready — deferred recovery exiting"
                    )
                    self._memory_gate_blocked = False
                    return

                logger.info(
                    f"[v276.2] Deferred recovery attempt #{attempt}/{max_attempts}"
                )

                try:
                    success = await self._attempt_ecapa_recovery(
                        source=f"deferred_poll_{attempt}"
                    )
                    if success:
                        logger.info(
                            f"[v276.2] Deferred ECAPA recovery SUCCESSFUL on "
                            f"attempt #{attempt} — voice unlock now available!"
                        )
                        return
                except Exception as e:
                    logger.warning(
                        f"[v276.2] Deferred ECAPA recovery attempt #{attempt} error: {e}"
                    )

            logger.error(
                f"[v276.2] Deferred ECAPA recovery exhausted {max_attempts} attempts "
                f"over {max_attempts * poll_interval:.0f}s. "
                "Voice unlock will remain unavailable until restart."
            )
            self._memory_gate_blocked = False

        try:
            loop = asyncio.get_running_loop()
            self._deferred_ecapa_recovery_task = loop.create_task(
                _recovery_loop(),
                name="ecapa-deferred-recovery",
            )
        except RuntimeError:
            logger.error(
                "[v276.2] CRITICAL: No running event loop for deferred recovery. "
                "ECAPA will remain unavailable until restart."
            )

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

        candidates = await self._discover_cloud_endpoint_candidates()
        if not candidates:
            logger.error("❌ No cloud endpoint candidates available for fallback")
            logger.error(
                "   Configure JARVIS_CLOUD_ECAPA_ENDPOINT(S) or JARVIS_ML_CLOUD_ENDPOINT(S)"
            )
            return False

        verify_timeout = float(os.getenv("JARVIS_ECAPA_CLOUD_TIMEOUT", "15.0"))
        verify_retries = int(os.getenv("JARVIS_ECAPA_CLOUD_RETRIES", "3"))
        last_error = "no candidate attempted"

        for endpoint_candidate, endpoint_source in candidates:
            normalized = endpoint_candidate.strip().rstrip("/")
            if not normalized:
                continue
            if not self._cloud_endpoint_probe_allowed(normalized):
                logger.debug(
                    "Skipping fallback candidate %s (source=%s): endpoint backoff active",
                    normalized,
                    endpoint_source,
                )
                continue

            self._set_cloud_endpoint(
                normalized,
                f"{endpoint_source}|fallback_to_cloud",
            )
            self._use_cloud = True
            self._cloud_verified = False
            logger.info(
                "   Trying cloud fallback candidate: %s (source=%s)",
                self._cloud_endpoint,
                self._cloud_endpoint_source,
            )

            try:
                cloud_ready, verify_msg = await self._verify_cloud_backend_ready(
                    timeout=verify_timeout,
                    retry_count=verify_retries,
                    test_extraction=True,  # Always test extraction for fallback
                )
            except Exception as e:
                cloud_ready = False
                verify_msg = f"exception:{type(e).__name__}: {e}"

            if cloud_ready:
                backend_kind = self._classify_endpoint_backend(
                    self._cloud_endpoint or "",
                    self._cloud_endpoint_source,
                )
                self._apply_ecapa_backend_environment(
                    backend_kind,
                    self._cloud_endpoint,
                )
                logger.info("=" * 70)
                logger.info("✅ CLOUD FALLBACK SUCCESS: Cloud ECAPA activated")
                logger.info("=" * 70)
                logger.info(f"   Verification: {verify_msg}")
                logger.info(f"   Endpoint: {self._cloud_endpoint}")
                logger.info("   All ECAPA operations will use cloud backend")
                logger.info("=" * 70)
                return True

            last_error = verify_msg
            self._record_cloud_endpoint_failure(
                normalized,
                reason=f"Fallback verification failed: {verify_msg}",
            )
            logger.warning(
                "⚠️ Cloud fallback candidate failed: %s (source=%s): %s",
                normalized,
                endpoint_source,
                verify_msg,
            )

        logger.error("=" * 70)
        logger.error("❌ CLOUD FALLBACK FAILED: Cloud verification failed")
        logger.error("=" * 70)
        logger.error(f"   Reason: {last_error}")
        logger.error("   Voice unlock will not work until ECAPA is available")
        logger.error("=" * 70)
        self._set_cloud_endpoint(None, "none")
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
        status["diagnostics"]["cloud_endpoint_failover_enabled"] = (
            MLConfig.CLOUD_ENDPOINT_FAILOVER_ENABLED
        )
        status["diagnostics"]["cloud_endpoint_failure_streak"] = dict(
            self._cloud_endpoint_failure_streak
        )
        status["diagnostics"]["cloud_endpoint_cooldown_remaining"] = {
            endpoint: max(0.0, until - time.time())
            for endpoint, until in self._cloud_endpoint_degraded_until.items()
            if until > time.time()
        }

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
        timeout: float = 30.0,
        _allow_failover_retry: bool = True,
        _bypass_policy: bool = False,
    ) -> Optional[Any]:
        """
        Extract speaker embedding using cloud backend.

        v21.1.0: Added readiness gate, circuit breaker, and 503 handling.
        v276.5: Added routing policy gate — direct callers that bypass
        resolve_effective_backend() are now blocked when policy is LOCAL_ONLY.

        Args:
            audio_data: Raw audio bytes (16kHz, mono, float32)
            timeout: Request timeout in seconds
            _bypass_policy: Internal flag — set True only when called from
                extract_speaker_embedding() which already checked policy.

        Returns:
            Embedding tensor or None if failed
        """
        # v276.5: Routing policy gate — prevent direct callers from
        # bypassing LOCAL_ONLY policy. Internal calls from the public
        # extract_speaker_embedding() set _bypass_policy=True because
        # they've already checked via resolve_effective_backend().
        if not _bypass_policy and self._routing_policy == RoutingPolicy.LOCAL_ONLY:
            logger.debug(
                "[v276.5] extract_speaker_embedding_cloud() blocked by "
                "LOCAL_ONLY routing policy"
            )
            return None

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

            active_endpoint = (self._cloud_endpoint or "").rstrip("/")
            route_candidates = self._cloud_route_candidates("embedding")
            payload = {
                "audio_data": audio_b64,
                "sample_rate": 16000,
                "format": "float32",
            }

            async with aiohttp.ClientSession() as session:
                for route_index, route_path in enumerate(route_candidates):
                    endpoint = f"{active_endpoint}{route_path}"
                    has_route_fallback = route_index + 1 < len(route_candidates)
                    logger.debug(
                        "Sending speaker embedding request to cloud: %s", endpoint
                    )

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

                                if route_path != self._cloud_embedding_route:
                                    logger.info(
                                        "Cloud embedding route switched: %s -> %s",
                                        self._cloud_embedding_route,
                                        route_path,
                                    )
                                    self._cloud_embedding_route = route_path

                                logger.debug(
                                    "Cloud embedding received: shape %s",
                                    embedding_tensor.shape,
                                )
                                self._cloud_embedding_cb.record_success()
                                self._mark_cloud_api_success()
                                # v251.2: Reset fallback warning flag on success
                                # so it fires again if cloud goes down later
                                global _cloud_fallback_warned
                                _cloud_fallback_warned = False
                                return embedding_tensor

                            error_msg = result.get('error', 'unknown')
                            logger.error(f"Cloud embedding failed: {error_msg}")
                            self._cloud_embedding_cb.record_failure(f"API error: {error_msg}")
                            self._mark_cloud_api_failure(f"API error: {error_msg}")
                            self._record_cloud_endpoint_failure(
                                active_endpoint,
                                reason=f"API error: {error_msg}",
                            )
                            return None

                        if response.status == 503:
                            if has_route_fallback:
                                logger.warning(
                                    "Cloud embedding route %s returned HTTP 503; "
                                    "retrying with %s",
                                    route_path,
                                    route_candidates[route_index + 1],
                                )
                                continue

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
                                self._record_cloud_endpoint_failure(
                                    active_endpoint,
                                    reason=detail,
                                    status_code=503,
                                    retry_after_seconds=retry_after_seconds,
                                )
                                if _allow_failover_retry:
                                    switched = await self._attempt_cloud_endpoint_failover(
                                        trigger="embedding_503_startup_failed",
                                        failed_endpoint=active_endpoint,
                                    )
                                    if switched:
                                        return await self.extract_speaker_embedding_cloud(
                                            audio_data,
                                            timeout=timeout,
                                            _allow_failover_retry=False,
                                            _bypass_policy=True,
                                        )

                            return None

                        error_text = await response.text()
                        should_try_next_route = (
                            has_route_fallback
                            and (response.status in (404, 405) or response.status >= 500)
                        )
                        if should_try_next_route:
                            logger.warning(
                                "Cloud embedding route %s failed (HTTP %s); "
                                "retrying with %s",
                                route_path,
                                response.status,
                                route_candidates[route_index + 1],
                            )
                            continue

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
                            self._record_cloud_endpoint_failure(
                                active_endpoint,
                                reason=error_text[:160],
                                status_code=response.status,
                            )
                            self._log_cloud_cooldown("Cloud embedding API hard failure")
                            if _allow_failover_retry:
                                switched = await self._attempt_cloud_endpoint_failover(
                                    trigger=f"embedding_http_{response.status}",
                                    failed_endpoint=active_endpoint,
                                )
                                if switched:
                                    return await self.extract_speaker_embedding_cloud(
                                        audio_data,
                                        timeout=timeout,
                                        _allow_failover_retry=False,
                                        _bypass_policy=True,
                                    )
                        else:
                            self._cloud_verified = False
                        return None

                return None

        except ImportError:
            logger.error("aiohttp not available for cloud requests")
            return None
        except asyncio.TimeoutError:
            failed_endpoint = (self._cloud_endpoint or "").rstrip("/")
            logger.error(
                f"Cloud embedding request timed out ({timeout}s) at "
                f"{self._cloud_endpoint} (source={self._cloud_endpoint_source})"
            )
            self._cloud_embedding_cb.record_failure(f"Timeout after {timeout}s")
            self._mark_cloud_api_failure(f"Timeout after {timeout}s")
            self._record_cloud_endpoint_failure(
                failed_endpoint,
                reason=f"Timeout after {timeout}s",
            )
            if _allow_failover_retry:
                switched = await self._attempt_cloud_endpoint_failover(
                    trigger="embedding_timeout",
                    failed_endpoint=failed_endpoint,
                )
                if switched:
                    return await self.extract_speaker_embedding_cloud(
                        audio_data,
                        timeout=timeout,
                        _allow_failover_retry=False,
                        _bypass_policy=True,
                    )
            return None
        except Exception as e:
            failed_endpoint = (self._cloud_endpoint or "").rstrip("/")
            logger.error(
                f"Cloud embedding request failed at {self._cloud_endpoint} "
                f"(source={self._cloud_endpoint_source}): {e}"
            )
            self._cloud_embedding_cb.record_failure(str(e)[:100])
            self._mark_cloud_api_failure(str(e)[:100])
            self._record_cloud_endpoint_failure(
                failed_endpoint,
                reason=str(e),
            )
            if _allow_failover_retry:
                switched = await self._attempt_cloud_endpoint_failover(
                    trigger="embedding_exception",
                    failed_endpoint=failed_endpoint,
                )
                if switched:
                    return await self.extract_speaker_embedding_cloud(
                        audio_data,
                        timeout=timeout,
                        _allow_failover_retry=False,
                        _bypass_policy=True,
                    )
            return None

    async def verify_speaker_cloud(
        self,
        audio_data: bytes,
        reference_embedding: Any,
        timeout: float = 30.0,
        _allow_failover_retry: bool = True,
        _bypass_policy: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Verify speaker using cloud backend.

        v21.1.0: Added readiness gate, circuit breaker, and 503 handling.
        v276.5: Added routing policy gate.

        Args:
            audio_data: Raw audio bytes (16kHz, mono, float32)
            reference_embedding: Reference embedding to compare against
            timeout: Request timeout in seconds
            _bypass_policy: Internal flag — set True only when called from
                verify_speaker_with_best_method() which already checked policy.

        Returns:
            Dict with verification result or None if failed
        """
        # v276.5: Routing policy gate
        if not _bypass_policy and self._routing_policy == RoutingPolicy.LOCAL_ONLY:
            logger.debug(
                "[v276.5] verify_speaker_cloud() blocked by LOCAL_ONLY routing policy"
            )
            return None
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

            active_endpoint = (self._cloud_endpoint or "").rstrip("/")
            route_candidates = self._cloud_route_candidates("verify")
            payload = {
                "audio_data": audio_b64,
                "reference_embedding": ref_list,
                "sample_rate": 16000,
                "format": "float32",
            }

            async with aiohttp.ClientSession() as session:
                for route_index, route_path in enumerate(route_candidates):
                    endpoint = f"{active_endpoint}{route_path}"
                    has_route_fallback = route_index + 1 < len(route_candidates)
                    logger.debug(
                        "Sending speaker verification request to cloud: %s", endpoint
                    )

                    async with session.post(
                        endpoint,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as response:
                        if response.status == 200:
                            result = await response.json()
                            if route_path != self._cloud_verify_route:
                                logger.info(
                                    "Cloud verification route switched: %s -> %s",
                                    self._cloud_verify_route,
                                    route_path,
                                )
                                self._cloud_verify_route = route_path
                            logger.debug(f"Cloud verification result: {result}")
                            self._cloud_embedding_cb.record_success()
                            self._mark_cloud_api_success()
                            return result

                        if response.status == 503:
                            if has_route_fallback:
                                logger.warning(
                                    "Cloud verification route %s returned HTTP 503; "
                                    "retrying with %s",
                                    route_path,
                                    route_candidates[route_index + 1],
                                )
                                continue

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
                                self._record_cloud_endpoint_failure(
                                    active_endpoint,
                                    reason=detail,
                                    status_code=503,
                                    retry_after_seconds=retry_after_seconds,
                                )
                                if _allow_failover_retry:
                                    switched = await self._attempt_cloud_endpoint_failover(
                                        trigger="verify_503_startup_failed",
                                        failed_endpoint=active_endpoint,
                                    )
                                    if switched:
                                        return await self.verify_speaker_cloud(
                                            audio_data,
                                            reference_embedding,
                                            timeout=timeout,
                                            _allow_failover_retry=False,
                                            _bypass_policy=True,
                                        )
                            return None

                        error_text = await response.text()
                        should_try_next_route = (
                            has_route_fallback
                            and (response.status in (404, 405) or response.status >= 500)
                        )
                        if should_try_next_route:
                            logger.warning(
                                "Cloud verification route %s failed (HTTP %s); "
                                "retrying with %s",
                                route_path,
                                response.status,
                                route_candidates[route_index + 1],
                            )
                            continue

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
                            self._record_cloud_endpoint_failure(
                                active_endpoint,
                                reason=error_text[:160],
                                status_code=response.status,
                            )
                            self._log_cloud_cooldown("Cloud verification API hard failure")
                            if _allow_failover_retry:
                                switched = await self._attempt_cloud_endpoint_failover(
                                    trigger=f"verify_http_{response.status}",
                                    failed_endpoint=active_endpoint,
                                )
                                if switched:
                                    return await self.verify_speaker_cloud(
                                        audio_data,
                                        reference_embedding,
                                        timeout=timeout,
                                        _allow_failover_retry=False,
                                        _bypass_policy=True,
                                    )
                        else:
                            self._cloud_verified = False
                        return None

                return None

        except ImportError:
            logger.error("aiohttp not available for cloud requests")
            return None
        except asyncio.TimeoutError:
            failed_endpoint = (self._cloud_endpoint or "").rstrip("/")
            logger.error(
                f"Cloud verification request timed out ({timeout}s) at "
                f"{self._cloud_endpoint} (source={self._cloud_endpoint_source})"
            )
            self._cloud_embedding_cb.record_failure(f"Timeout after {timeout}s")
            self._mark_cloud_api_failure(f"Timeout after {timeout}s")
            self._record_cloud_endpoint_failure(
                failed_endpoint,
                reason=f"Timeout after {timeout}s",
            )
            if _allow_failover_retry:
                switched = await self._attempt_cloud_endpoint_failover(
                    trigger="verify_timeout",
                    failed_endpoint=failed_endpoint,
                )
                if switched:
                    return await self.verify_speaker_cloud(
                        audio_data,
                        reference_embedding,
                        timeout=timeout,
                        _allow_failover_retry=False,
                        _bypass_policy=True,
                    )
            return None
        except Exception as e:
            failed_endpoint = (self._cloud_endpoint or "").rstrip("/")
            logger.error(
                f"Cloud verification request failed at {self._cloud_endpoint} "
                f"(source={self._cloud_endpoint_source}): {e}"
            )
            self._cloud_embedding_cb.record_failure(str(e)[:100])
            self._mark_cloud_api_failure(str(e)[:100])
            self._record_cloud_endpoint_failure(
                failed_endpoint,
                reason=str(e),
            )
            if _allow_failover_retry:
                switched = await self._attempt_cloud_endpoint_failover(
                    trigger="verify_exception",
                    failed_endpoint=failed_endpoint,
                )
                if switched:
                    return await self.verify_speaker_cloud(
                        audio_data,
                        reference_embedding,
                        timeout=timeout,
                        _allow_failover_retry=False,
                        _bypass_policy=True,
                    )
            return None

    def set_cloud_endpoint(self, endpoint: str) -> None:
        """
        Manually set the cloud endpoint.

        Args:
            endpoint: Cloud ML API endpoint URL
        """
        self._set_cloud_endpoint(endpoint, "manual")
        self._use_cloud = True
        backend_kind = self._classify_endpoint_backend(
            self._cloud_endpoint or "",
            self._cloud_endpoint_source,
        )
        self._apply_ecapa_backend_environment(backend_kind, self._cloud_endpoint)
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
        self._cloud_verified = False
        self._apply_ecapa_backend_environment("local", None)
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

    # Step 4: Load ECAPA directly — don't defer to background prewarm.
    # v266.5: The previous approach passively polled when background prewarm
    # was running, but the prewarm loads ALL engines (Whisper, SpeechBrain,
    # ECAPA) in parallel. Slow engines consumed the entire timeout budget.
    # Fix: call ecapa_wrapper.load() directly. The internal asyncio.Lock
    # ensures idempotency — if prewarm is already loading ECAPA, this
    # awaits the same lock and returns immediately once it finishes.
    remaining = timeout - (time.time() - start_time)
    if ecapa_wrapper and remaining > 2.0:
        if registry.is_warming_up:
            logger.info(
                "🔄 [ENSURE_ECAPA] Prewarm running, but loading ECAPA directly "
                f"(remaining budget: {remaining:.1f}s)"
            )
        else:
            logger.info(
                f"🔄 [ENSURE_ECAPA] Loading ECAPA directly (budget: {remaining:.1f}s)"
            )
        try:
            loaded = await asyncio.wait_for(
                ecapa_wrapper.load(), timeout=remaining - 1.0,
            )
            if loaded:
                elapsed = time.time() - start_time
                logger.info(f"✅ [ENSURE_ECAPA] ECAPA loaded successfully in {elapsed:.1f}s")
                return True, f"Local ECAPA loaded in {elapsed:.1f}s", ecapa_wrapper.get_engine()
        except asyncio.TimeoutError:
            pass  # Fall through to timeout handling below
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"   ECAPA direct load failed: {e}")

    # Check if cloud mode became available while we were loading
    if registry.is_using_cloud and getattr(registry, '_cloud_verified', False):
        elapsed = time.time() - start_time
        logger.info(f"✅ [ENSURE_ECAPA] Cloud ECAPA became available in {elapsed:.1f}s")
        return True, f"Cloud ECAPA available in {elapsed:.1f}s", None

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

    # v276.4: Use centralized routing decision that honors routing policy,
    # flap dampening, and parity-driven overrides — not just raw _use_cloud.
    effective_backend, routing_reason = registry.resolve_effective_backend(
        prefer_cloud=prefer_cloud,
    )
    use_cloud = (effective_backend == "cloud")
    logger.debug(
        f"[v276.4] Embedding routing: backend={effective_backend}, "
        f"reason={routing_reason}"
    )

    # ==========================================================================
    # CLOUD EXTRACTION PATH
    # ==========================================================================
    if use_cloud:
        logger.debug("Using cloud for speaker embedding extraction")

        _t0 = time.time()
        embedding = await registry.extract_speaker_embedding_cloud(
            audio_data, _bypass_policy=True,
        )

        if embedding is not None:
            # v276.5: Track cloud latency for warm-instance degradation detection
            _latency = time.time() - _t0
            registry.record_cloud_latency(_latency)
            registry._record_routing_decision(RouteDecisionReason.CLOUD_PRIMARY)

            logger.debug(f"Cloud embedding extracted: shape {embedding.shape}")
            return embedding

        # Cloud failed - try local if fallback enabled, local is ready, and
        # routing policy allows it (CLOUD_ONLY suppresses local fallback)
        if (
            fallback_enabled
            and registry.is_voice_unlock_ready
            and registry._routing_policy != RoutingPolicy.CLOUD_ONLY
        ):
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
            registry._record_routing_decision(RouteDecisionReason.LOCAL_FALLBACK)
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
            # Local not ready - try cloud fallback (suppressed by LOCAL_ONLY policy)
            if (
                fallback_enabled
                and registry.cloud_endpoint
                and registry._routing_policy != RoutingPolicy.LOCAL_ONLY
            ):
                logger.warning("Local engines not ready, falling back to cloud")
                return await registry.extract_speaker_embedding_cloud(
                    audio_data, _bypass_policy=True,
                )

            logger.error("Local engines not ready and no cloud fallback")
            return None

    embedding = await _extract_local_embedding(registry, audio_data)

    if embedding is not None:
        registry._record_routing_decision(RouteDecisionReason.LOCAL_PRIMARY)
        return embedding

    # Local extraction failed - try cloud fallback (suppressed by LOCAL_ONLY policy)
    if (
        fallback_enabled
        and MLConfig.CLOUD_FALLBACK_ENABLED
        and registry._routing_policy != RoutingPolicy.LOCAL_ONLY
    ):
        logger.warning("Local extraction failed, attempting cloud fallback")
        registry._record_routing_decision(RouteDecisionReason.CLOUD_FALLBACK)

        # Activate cloud if not already active
        if not registry.cloud_endpoint:
            await registry._activate_cloud_routing()

        if registry.cloud_endpoint:
            return await registry.extract_speaker_embedding_cloud(
                audio_data, _bypass_policy=True,
            )

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

    # v276.4: Use centralized routing decision
    effective_backend, routing_reason = registry.resolve_effective_backend()
    logger.debug(
        f"[v276.4] Verify routing: backend={effective_backend}, "
        f"reason={routing_reason}"
    )

    if effective_backend == "cloud":
        # Use cloud verification
        return await registry.verify_speaker_cloud(
            audio_data, reference_embedding, timeout,
            _bypass_policy=True,
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

        # Try cloud fallback (suppressed by LOCAL_ONLY routing policy)
        if (
            registry._cloud_fallback_enabled
            and registry.cloud_endpoint
            and registry._routing_policy != RoutingPolicy.LOCAL_ONLY
        ):
            logger.info("Falling back to cloud verification")
            return await registry.verify_speaker_cloud(
                audio_data, reference_embedding, timeout,
                _bypass_policy=True,
            )

        return None
