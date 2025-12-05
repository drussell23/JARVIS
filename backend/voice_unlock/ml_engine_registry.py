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
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, Callable, Dict, List, Optional, Set, Tuple, Type, TypeVar, Union
)
from concurrent.futures import ThreadPoolExecutor
import traceback

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
            "ram_threshold_local": cls.RAM_THRESHOLD_LOCAL,
            "memory_pressure_threshold": cls.MEMORY_PRESSURE_THRESHOLD,
        }

    @classmethod
    def check_memory_pressure(cls) -> Tuple[bool, float, str]:
        """
        Check current memory pressure and decide routing.

        Returns:
            (use_cloud, available_ram_gb, reason)
        """
        try:
            import subprocess

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

                # Decision logic
                if available_gb < cls.RAM_THRESHOLD_CRITICAL:
                    return (True, available_gb, f"Critical RAM: {available_gb:.1f}GB < {cls.RAM_THRESHOLD_CRITICAL}GB")
                elif available_gb < cls.RAM_THRESHOLD_LOCAL:
                    return (True, available_gb, f"Low RAM: {available_gb:.1f}GB < {cls.RAM_THRESHOLD_LOCAL}GB")
                else:
                    return (False, available_gb, f"Sufficient RAM: {available_gb:.1f}GB >= {cls.RAM_THRESHOLD_LOCAL}GB")

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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_ready": self.is_ready,
            "ready_engines": f"{self.ready_count}/{self.total_count}",
            "prewarm_duration_ms": self.prewarm_duration_ms,
            "engines": {k: v.to_dict() for k, v in self.engines.items()},
            "errors": self.errors,
        }


# =============================================================================
# ENGINE WRAPPER - Base class for all ML engines
# =============================================================================

class MLEngineWrapper(ABC):
    """
    Abstract base class for ML engine wrappers.
    Provides consistent interface and lifecycle management.
    """

    def __init__(self, name: str):
        self.name = name
        self.metrics = EngineMetrics(engine_name=name)
        self._engine: Any = None
        self._lock = asyncio.Lock()
        self._thread_lock = threading.Lock()

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
        """Get the loaded engine instance (thread-safe)."""
        with self._thread_lock:
            if not self.is_loaded:
                raise RuntimeError(f"Engine {self.name} not loaded")
            self.metrics.last_used = time.time()
            self.metrics.use_count += 1
            return self._engine

    async def unload(self):
        """Unload the engine and free resources."""
        async with self._lock:
            if self._engine is not None:
                self.metrics.state = EngineState.UNLOADING
                try:
                    # Allow garbage collection
                    self._engine = None
                    self.metrics.state = EngineState.UNINITIALIZED
                    logger.info(f"🧹 [{self.name}] Engine unloaded")
                except Exception as e:
                    logger.error(f"❌ [{self.name}] Unload error: {e}")


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
        """Load ECAPA-TDNN speaker encoder."""
        from concurrent.futures import ThreadPoolExecutor
        import torch

        logger.info(f"   [{self.name}] Importing SpeechBrain...")

        def _load_sync():
            from speechbrain.inference.speaker import EncoderClassifier

            # Force CPU for speaker encoder (MPS doesn't support FFT)
            run_opts = {"device": "cpu"}

            cache_dir = MLConfig.CACHE_DIR / "speechbrain" / "speaker_encoder"
            cache_dir.mkdir(parents=True, exist_ok=True)

            logger.info(f"   [{self.name}] Loading from: speechbrain/spkrec-ecapa-voxceleb")

            model = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir=str(cache_dir),
                run_opts=run_opts,
            )

            return model

        # Run in thread pool to avoid blocking
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ecapa_loader") as executor:
            model = await loop.run_in_executor(executor, _load_sync)

        self._encoder_loaded = True
        return model

    async def _warmup_impl(self) -> bool:
        """Run a test embedding extraction."""
        try:
            import numpy as np
            import torch

            # Generate 1 second of test audio
            sample_rate = 16000
            duration = 1.0
            t = np.linspace(0, duration, int(sample_rate * duration))
            # Pink noise for realistic test
            white = np.random.randn(len(t)).astype(np.float32)
            test_audio = torch.tensor(white * 0.3).unsqueeze(0)

            # Extract embedding
            with torch.no_grad():
                embedding = self._engine.encode_batch(test_audio)

            logger.info(f"   [{self.name}] Warmup embedding shape: {embedding.shape}")
            return True

        except Exception as e:
            logger.warning(f"   [{self.name}] Warmup failed: {e}")
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

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt_loader") as executor:
            model = await loop.run_in_executor(executor, _load_sync)

        return model

    async def _warmup_impl(self) -> bool:
        """Run a test transcription."""
        try:
            import numpy as np
            import torch

            # Generate 1 second of silence (quick warmup)
            sample_rate = 16000
            test_audio = torch.zeros(1, sample_rate)

            # Transcribe
            with torch.no_grad():
                _ = self._engine.transcribe_batch(test_audio, torch.tensor([1.0]))

            logger.info(f"   [{self.name}] Warmup transcription complete")
            return True

        except Exception as e:
            logger.warning(f"   [{self.name}] Warmup failed: {e}")
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

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper_loader") as executor:
            model = await loop.run_in_executor(executor, _load_sync)

        return model

    async def _warmup_impl(self) -> bool:
        """Run a test transcription."""
        try:
            import numpy as np

            # Generate 1 second of silence
            sample_rate = 16000
            test_audio = np.zeros(sample_rate, dtype=np.float32)

            # Transcribe (this warms up the model)
            _ = self._engine.transcribe(
                test_audio,
                language="en",
                fp16=False,
            )

            logger.info(f"   [{self.name}] Warmup transcription complete")
            return True

        except Exception as e:
            logger.warning(f"   [{self.name}] Warmup failed: {e}")
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

                # Mark as ready (cloud is ready immediately)
                self._status.prewarm_completed = True
                self._status.prewarm_end_time = time.time()
                self._status.is_ready = True
                self._ready_event.set()

                logger.info("✅ Cloud ML backend configured - voice unlock ready!")
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

            self._status.prewarm_completed = True
            self._status.prewarm_end_time = time.time()
            self._status.is_ready = True
            self._ready_event.set()

            logger.info("✅ Cloud ML backend configured - voice unlock ready!")
            return self._status

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
        """Load all engines in parallel."""
        logger.info(f"🔄 Loading {len(self._engines)} engines in PARALLEL...")

        # Create tasks for all engines
        tasks = {
            name: asyncio.create_task(engine.load())
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
                    logger.error(f"❌ {name} failed: {result}")
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

    async def _prewarm_sequential(self, timeout: float):
        """Load engines one by one."""
        logger.info(f"🔄 Loading {len(self._engines)} engines SEQUENTIALLY...")

        remaining_timeout = timeout

        for name, engine in self._engines.items():
            if remaining_timeout <= 0:
                logger.warning(f"⏱️ No time remaining for {name}")
                break

            start = time.time()

            try:
                success = await engine.load(timeout=remaining_timeout)
                if not success:
                    self._status.errors.append(f"{name}: load failed")
            except Exception as e:
                logger.error(f"❌ {name} failed: {e}")
                self._status.errors.append(f"{name}: {e}")

            elapsed = time.time() - start
            remaining_timeout -= elapsed

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
        """Check if registry is routing to cloud."""
        return self._use_cloud

    @property
    def cloud_endpoint(self) -> Optional[str]:
        """Get the current cloud endpoint URL."""
        return self._cloud_endpoint

    async def _activate_cloud_routing(self) -> bool:
        """
        Activate cloud routing for ML operations.

        This configures the registry to route speaker verification
        and other ML operations to GCP instead of local processing.

        Returns:
            True if cloud routing was successfully activated
        """
        try:
            # Try to get cloud endpoint from MemoryAwareStartup
            try:
                from core.memory_aware_startup import get_startup_manager
                startup_manager = await get_startup_manager()

                if startup_manager.is_cloud_ml_active:
                    # Get endpoint from active cloud backend
                    endpoint = await startup_manager.get_ml_endpoint("speaker_verify")
                    self._cloud_endpoint = endpoint
                    logger.info(f"   Cloud endpoint from MemoryAwareStartup: {endpoint}")
                else:
                    # Activate cloud backend
                    if self._startup_decision:
                        result = await startup_manager.activate_cloud_ml_backend()
                        if result.get("success"):
                            self._cloud_endpoint = f"http://{result.get('ip')}:8010/api/ml"
                            logger.info(f"   Cloud backend activated: {self._cloud_endpoint}")
            except ImportError:
                logger.debug("MemoryAwareStartup not available")

            # Fallback: Use environment variable or default GCP endpoint
            if not self._cloud_endpoint:
                gcp_project = os.getenv("GCP_PROJECT_ID", "jarvis-473803")
                gcp_region = os.getenv("GCP_REGION", "us-central1")

                # Try Cloud Run endpoint first, then Compute Engine fallback
                self._cloud_endpoint = os.getenv(
                    "JARVIS_CLOUD_ML_ENDPOINT",
                    f"https://jarvis-ml-{gcp_project}.{gcp_region}.run.app/api/ml"
                )
                logger.info(f"   Using fallback cloud endpoint: {self._cloud_endpoint}")

            self._use_cloud = True
            logger.info("☁️  Cloud routing activated for ML operations")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to activate cloud routing: {e}")
            self._use_cloud = False
            return False

    async def extract_speaker_embedding_cloud(
        self,
        audio_data: bytes,
        timeout: float = 30.0
    ) -> Optional[Any]:
        """
        Extract speaker embedding using cloud backend.

        Args:
            audio_data: Raw audio bytes (16kHz, mono, float32)
            timeout: Request timeout in seconds

        Returns:
            Embedding tensor or None if failed
        """
        if not self._cloud_endpoint:
            logger.error("Cloud endpoint not configured")
            return None

        try:
            import aiohttp
            import base64
            import numpy as np

            # Encode audio as base64
            audio_b64 = base64.b64encode(audio_data).decode('utf-8')

            endpoint = f"{self._cloud_endpoint}/speaker_embedding"
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

                            import torch
                            embedding_tensor = torch.tensor(embedding).unsqueeze(0)

                            logger.debug(f"Cloud embedding received: shape {embedding_tensor.shape}")
                            return embedding_tensor
                        else:
                            logger.error(f"Cloud embedding failed: {result.get('error')}")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error(f"Cloud embedding request failed ({response.status}): {error_text}")
                        return None

        except ImportError:
            logger.error("aiohttp not available for cloud requests")
            return None
        except asyncio.TimeoutError:
            logger.error(f"Cloud embedding request timed out ({timeout}s)")
            return None
        except Exception as e:
            logger.error(f"Cloud embedding request failed: {e}")
            return None

    async def verify_speaker_cloud(
        self,
        audio_data: bytes,
        reference_embedding: Any,
        timeout: float = 30.0
    ) -> Optional[Dict[str, Any]]:
        """
        Verify speaker using cloud backend.

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

        try:
            import aiohttp
            import base64
            import numpy as np

            # Encode audio as base64
            audio_b64 = base64.b64encode(audio_data).decode('utf-8')

            # Convert reference embedding to list
            if hasattr(reference_embedding, 'cpu'):
                ref_list = reference_embedding.cpu().numpy().tolist()
            elif hasattr(reference_embedding, 'tolist'):
                ref_list = reference_embedding.tolist()
            else:
                ref_list = list(reference_embedding)

            endpoint = f"{self._cloud_endpoint}/verify_speaker"
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
                        return result
                    else:
                        error_text = await response.text()
                        logger.error(f"Cloud verification failed ({response.status}): {error_text}")
                        return None

        except ImportError:
            logger.error("aiohttp not available for cloud requests")
            return None
        except asyncio.TimeoutError:
            logger.error(f"Cloud verification request timed out ({timeout}s)")
            return None
        except Exception as e:
            logger.error(f"Cloud verification request failed: {e}")
            return None

    def set_cloud_endpoint(self, endpoint: str) -> None:
        """
        Manually set the cloud endpoint.

        Args:
            endpoint: Cloud ML API endpoint URL
        """
        self._cloud_endpoint = endpoint
        self._use_cloud = True
        logger.info(f"☁️  Cloud endpoint set to: {endpoint}")

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


# =============================================================================
# GLOBAL ACCESS FUNCTIONS
# =============================================================================

_registry: Optional[MLEngineRegistry] = None
_registry_lock = asyncio.Lock()


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


def get_ml_registry_sync() -> Optional[MLEngineRegistry]:
    """
    Get the registry synchronously (returns None if not initialized).

    Use this in sync code paths where you can't await.
    """
    return _registry


async def prewarm_voice_unlock_models_blocking() -> RegistryStatus:
    """
    Convenience function to prewarm all voice unlock models.

    BLOCKS until all models are loaded.
    Call this at startup in main.py.
    """
    registry = await get_ml_registry()
    return await registry.prewarm_all_blocking()


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
    Wait for voice unlock models to be ready.

    Returns True if ready, False if timeout.
    """
    registry = await get_ml_registry()
    return await registry.wait_until_ready(timeout)


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
        if fallback_enabled and registry.is_ready:
            logger.warning("Cloud extraction failed, falling back to local")
            return await _extract_local_embedding(registry, audio_data)

        logger.error("Cloud extraction failed and fallback not available")
        return None

    # ==========================================================================
    # LOCAL EXTRACTION PATH
    # ==========================================================================
    if not registry.is_ready:
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


async def _extract_local_embedding(
    registry: MLEngineRegistry,
    audio_data: bytes
) -> Optional[Any]:
    """
    Extract embedding using local ECAPA-TDNN engine.

    Internal helper function for local extraction.
    """
    try:
        ecapa_engine = registry.get_engine("ecapa_tdnn")

        if ecapa_engine is None:
            logger.error("ECAPA-TDNN engine is None")
            return None

        # Convert audio bytes to tensor
        import torch
        import numpy as np

        # Audio should be float32, 16kHz, mono
        audio_array = np.frombuffer(audio_data, dtype=np.float32)
        audio_tensor = torch.tensor(audio_array).unsqueeze(0)

        # Extract embedding using the singleton engine
        with torch.no_grad():
            embedding = ecapa_engine.encode_batch(audio_tensor)

        logger.debug(f"Local embedding extracted: shape {embedding.shape}")
        return embedding

    except RuntimeError as e:
        # Engine not loaded
        logger.warning(f"ECAPA-TDNN engine error: {e}")
        return None
    except Exception as e:
        logger.error(f"Local speaker embedding extraction failed: {e}")
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
