#!/usr/bin/env python3
"""
Cloud ECAPA Speaker Embedding Service v20.3.0
==============================================

Ultra-fast, production-ready cloud service for ECAPA-TDNN speaker embeddings.
Designed for GCP Cloud Run with <2s cold starts using optimized models.

Key Features:
- MULTI-STRATEGY OPTIMIZATION: JIT, ONNX, Quantization with auto-selection
- TORCHSCRIPT JIT: Pre-compiled model loads in <2s (vs 140s standard)
- ONNX RUNTIME: Portable, optimized inference with ONNXRuntime
- DYNAMIC QUANTIZATION: Reduced model size with int8 weights
- STRICT OFFLINE MODE: Zero network calls at runtime
- PRE-BAKED MODEL: Model weights baked into Docker image
- ASYNC PARALLEL LOADING: Non-blocking initialization
- FAST-FAIL: Immediate error if pre-baked cache missing
- Circuit breaker for downstream failures
- Request batching for efficiency
- Embedding caching with TTL
- Comprehensive metrics and telemetry

v20.3.0 - Complete SpeechBrain Lazy Import for Ultra-Fast Cold Starts (<5s)

Endpoints:
    GET  /health              - Health check with ECAPA readiness
    GET  /status              - Detailed service status
    POST /api/ml/speaker_embedding  - Extract speaker embedding
    POST /api/ml/speaker_verify     - Verify speaker against reference
    POST /api/ml/batch_embedding    - Batch embedding extraction

Environment Variables:
    ECAPA_MODEL_PATH        - Path to ECAPA model
    ECAPA_CACHE_DIR         - Cache directory for model files
    ECAPA_SOURCE_CACHE      - Pre-baked cache location (Docker)
    ECAPA_DEVICE            - Device to run on (cpu/cuda/mps)
    ECAPA_STRICT_OFFLINE    - Enforce strict offline mode (no network)
    ECAPA_SKIP_HF_DOWNLOAD  - Skip HuggingFace downloads
    ECAPA_USE_OPTIMIZED     - Use optimized models (jit/onnx/quantized)
    ECAPA_PREFERRED_STRATEGY - Preferred optimization strategy
    PORT                    - Server port (default: 8010)
"""

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import logging
import os
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import traceback

# =============================================================================
# CRITICAL: ENFORCE STRICT OFFLINE MODE BEFORE ANY IMPORTS
# =============================================================================
# This MUST happen before importing torch/speechbrain to prevent network calls
def _enforce_strict_offline():
    """Set all offline environment variables to prevent network access."""
    offline_vars = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
    }

    strict_mode = os.getenv("ECAPA_STRICT_OFFLINE", "true").lower() == "true"
    skip_download = os.getenv("ECAPA_SKIP_HF_DOWNLOAD", "true").lower() == "true"

    if strict_mode or skip_download:
        for key, value in offline_vars.items():
            os.environ[key] = value
        print(f">>> STRICT OFFLINE MODE ENABLED: {list(offline_vars.keys())}", flush=True)

    return strict_mode

STRICT_OFFLINE_MODE = _enforce_strict_offline()

import numpy as np

# Pre-import heavy ML libraries at module load time to avoid thread pool issues
# This happens once when the container starts, before any requests
print(">>> Pre-importing torch at module level...", flush=True)
import torch
print(f">>> torch {torch.__version__} imported successfully", flush=True)

# v20.2.0: CONDITIONAL SpeechBrain import - only load if JIT model not available
# This saves ~60 seconds of cold start time when using JIT!
_SPEECHBRAIN_LOADED = False
EncoderClassifier = None  # Will be lazy-loaded if needed

def _lazy_import_speechbrain():
    """Lazy import SpeechBrain only when needed (fallback from JIT)."""
    global _SPEECHBRAIN_LOADED, EncoderClassifier
    if not _SPEECHBRAIN_LOADED:
        print(">>> Lazy-importing speechbrain (JIT fallback)...", flush=True)
        from speechbrain.inference.speaker import EncoderClassifier as EC
        EncoderClassifier = EC
        _SPEECHBRAIN_LOADED = True
        print(">>> speechbrain imported successfully", flush=True)
    return EncoderClassifier

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ecapa_cloud_service")

# =============================================================================
# DYNAMIC CONFIGURATION
# =============================================================================

class CloudECAPAConfig:
    """
    Dynamic configuration from environment variables.

    v19.0.0 - Enhanced for ultra-fast cold starts with pre-baked model cache.

    Supports robust pre-baked model cache detection with STRICT offline mode.
    Priority order for cache locations:
    1. ECAPA_SOURCE_CACHE (Docker pre-baked: /opt/ecapa_cache) - PREFERRED
    2. ECAPA_CACHE_DIR (Runtime: /tmp/ecapa_cache) - Fallback only
    3. FAST-FAIL if no valid cache (no network downloads in strict mode)
    """

    # Model configuration (dynamic from environment)
    MODEL_PATH = os.getenv("ECAPA_MODEL_PATH", "speechbrain/spkrec-ecapa-voxceleb")

    # Cache locations (priority order)
    SOURCE_CACHE = os.getenv("ECAPA_SOURCE_CACHE", "/opt/ecapa_cache")  # Docker pre-baked
    CACHE_DIR = os.getenv("ECAPA_CACHE_DIR", "/tmp/ecapa_cache")  # Runtime cache
    HOME_CACHE = os.path.expanduser("~/.cache/ecapa")  # Fallback

    # Device configuration (auto-detect if not specified)
    DEVICE = os.getenv("ECAPA_DEVICE", "cpu")  # cpu, cuda, mps

    # Performance settings
    BATCH_SIZE = int(os.getenv("ECAPA_BATCH_SIZE", "8"))
    CACHE_TTL = int(os.getenv("ECAPA_CACHE_TTL", "3600"))  # 1 hour
    CACHE_MAX_SIZE = int(os.getenv("ECAPA_CACHE_MAX_SIZE", "1000"))
    WARMUP_ON_START = os.getenv("ECAPA_WARMUP_ON_START", "true").lower() == "true"
    PORT = int(os.getenv("PORT", "8010"))

    # Circuit breaker settings
    CB_FAILURE_THRESHOLD = int(os.getenv("ECAPA_CB_FAILURES", "5"))
    CB_RECOVERY_TIMEOUT = float(os.getenv("ECAPA_CB_RECOVERY", "30.0"))

    # Request settings
    REQUEST_TIMEOUT = float(os.getenv("ECAPA_REQUEST_TIMEOUT", "30.0"))
    SAMPLE_RATE = int(os.getenv("ECAPA_SAMPLE_RATE", "16000"))

    # STRICT OFFLINE MODE SETTINGS (v19.0.0)
    STRICT_OFFLINE = os.getenv("ECAPA_STRICT_OFFLINE", "true").lower() == "true"
    SKIP_HF_DOWNLOAD = os.getenv("ECAPA_SKIP_HF_DOWNLOAD", "true").lower() == "true"
    CACHE_VERIFICATION_ENABLED = os.getenv("ECAPA_VERIFY_CACHE", "true").lower() == "true"
    PREBAKED_MANIFEST = os.getenv("ECAPA_PREBAKED_MANIFEST", "/opt/ecapa_cache/.prebaked_manifest.json")

    # OPTIMIZED MODEL SETTINGS (v20.0.0)
    USE_OPTIMIZED = os.getenv("ECAPA_USE_OPTIMIZED", "true").lower() == "true"
    PREFERRED_STRATEGY = os.getenv("ECAPA_PREFERRED_STRATEGY", "auto")  # auto, jit, onnx, quantized
    OPTIMIZATION_MANIFEST = os.getenv("ECAPA_OPTIMIZATION_MANIFEST", "/opt/ecapa_cache/.optimization_manifest.json")

    # Known optimized model files
    OPTIMIZED_MODEL_FILES = {
        "jit_trace": "ecapa_jit_traced.pt",
        "jit_script": "ecapa_jit_scripted.pt",
        "onnx": "ecapa_model.onnx",
        "quantize_dynamic": "ecapa_quantized_dynamic.pt",
    }

    # Required files for a valid pre-baked cache
    REQUIRED_CACHE_FILES = [
        "hyperparams.yaml",
        "embedding_model.ckpt",
    ]

    # Optional but expected files
    OPTIONAL_CACHE_FILES = [
        "mean_var_norm_emb.ckpt",
        "classifier.ckpt",
        "label_encoder.txt",
    ]

    @classmethod
    def load_prebaked_manifest(cls) -> Optional[Dict[str, Any]]:
        """
        Load the pre-baked manifest file created during Docker build.
        This provides instant verification that the cache is valid.
        """
        manifest_path = cls.PREBAKED_MANIFEST

        if not os.path.exists(manifest_path):
            # Also check in SOURCE_CACHE
            alt_path = os.path.join(cls.SOURCE_CACHE, ".prebaked_manifest.json")
            if os.path.exists(alt_path):
                manifest_path = alt_path
            else:
                logger.debug(f"No manifest found at {manifest_path} or {alt_path}")
                return None

        try:
            with open(manifest_path, 'r') as f:
                manifest = json.load(f)
            logger.info(f"✅ Pre-baked manifest loaded: v{manifest.get('version', 'unknown')}")
            logger.info(f"   Prebaked at: {manifest.get('prebaked_at', 'unknown')}")
            logger.info(f"   Size: {manifest.get('total_size_mb', 0):.2f} MB")
            return manifest
        except Exception as e:
            logger.warning(f"Failed to load manifest: {e}")
            return None

    @classmethod
    def get_cache_locations(cls) -> List[str]:
        """Get all possible cache locations in priority order."""
        locations = []

        # Priority 1: Docker pre-baked source cache
        if cls.SOURCE_CACHE:
            locations.append(cls.SOURCE_CACHE)

        # Priority 2: Runtime cache (may have been populated by entrypoint.sh)
        if cls.CACHE_DIR and cls.CACHE_DIR != cls.SOURCE_CACHE:
            locations.append(cls.CACHE_DIR)

        # Priority 3: Home directory fallback
        if cls.HOME_CACHE not in locations:
            locations.append(cls.HOME_CACHE)

        return locations

    @classmethod
    def verify_cache_integrity(cls, cache_path: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Verify that a cache directory contains all required model files.

        Returns:
            Tuple of (is_valid, message, diagnostics)
        """
        diagnostics = {
            "path": cache_path,
            "exists": os.path.isdir(cache_path),
            "required_files": {},
            "optional_files": {},
            "total_size_mb": 0,
        }

        if not os.path.isdir(cache_path):
            return False, f"Cache directory does not exist: {cache_path}", diagnostics

        # Check required files
        missing_required = []
        for filename in cls.REQUIRED_CACHE_FILES:
            filepath = os.path.join(cache_path, filename)
            exists = os.path.isfile(filepath)
            # Get file size safely (handle permission errors)
            file_size_kb = 0
            if exists:
                try:
                    file_size_kb = round(os.path.getsize(filepath) / 1024, 2)
                except (OSError, PermissionError) as e:
                    logger.debug(f"Could not get size of {filepath}: {e}")
                    file_size_kb = -1  # Indicate error but file exists

            diagnostics["required_files"][filename] = {
                "exists": exists,
                "size_kb": file_size_kb,
            }
            if not exists:
                missing_required.append(filename)

        # Check optional files
        for filename in cls.OPTIONAL_CACHE_FILES:
            filepath = os.path.join(cache_path, filename)
            exists = os.path.isfile(filepath)
            file_size_kb = 0
            if exists:
                try:
                    file_size_kb = round(os.path.getsize(filepath) / 1024, 2)
                except (OSError, PermissionError) as e:
                    logger.debug(f"Could not get size of {filepath}: {e}")
                    file_size_kb = -1
            diagnostics["optional_files"][filename] = {
                "exists": exists,
                "size_kb": file_size_kb,
            }

        # Calculate total size (with permission error handling)
        total_size = 0
        for root, dirs, files in os.walk(cache_path):
            for f in files:
                try:
                    total_size += os.path.getsize(os.path.join(root, f))
                except (OSError, PermissionError):
                    pass  # Skip files we can't read
        diagnostics["total_size_mb"] = round(total_size / (1024 * 1024), 2)

        if missing_required:
            return False, f"Missing required files: {missing_required}", diagnostics

        return True, "Cache verified successfully", diagnostics

    @classmethod
    def find_valid_cache(cls) -> Tuple[Optional[str], str, Dict[str, Any]]:
        """
        Find the first valid cache location with all required files.

        Returns:
            Tuple of (cache_path or None, message, diagnostics)
        """
        all_diagnostics = {}

        for cache_path in cls.get_cache_locations():
            is_valid, message, diag = cls.verify_cache_integrity(cache_path)
            all_diagnostics[cache_path] = diag

            if is_valid:
                logger.info(f"✅ Found valid pre-baked cache: {cache_path}")
                logger.info(f"   Size: {diag['total_size_mb']}MB")
                return cache_path, message, all_diagnostics
            else:
                logger.debug(f"Cache not valid at {cache_path}: {message}")

        return None, "No valid pre-baked cache found", all_diagnostics

    @classmethod
    def to_dict(cls) -> Dict[str, Any]:
        valid_cache, cache_msg, _ = cls.find_valid_cache()
        return {
            "model_path": cls.MODEL_PATH,
            "cache_dir": cls.CACHE_DIR,
            "source_cache": cls.SOURCE_CACHE,
            "valid_cache_found": valid_cache,
            "cache_status": cache_msg,
            "device": cls.DEVICE,
            "batch_size": cls.BATCH_SIZE,
            "cache_ttl": cls.CACHE_TTL,
            "warmup_on_start": cls.WARMUP_ON_START,
            "skip_hf_download": cls.SKIP_HF_DOWNLOAD,
            "port": cls.PORT,
        }


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = auto()      # Normal operation
    OPEN = auto()        # Failing, reject requests
    HALF_OPEN = auto()   # Testing recovery


@dataclass
class CircuitBreaker:
    """Circuit breaker for ECAPA model operations."""

    failure_threshold: int = CloudECAPAConfig.CB_FAILURE_THRESHOLD
    recovery_timeout: float = CloudECAPAConfig.CB_RECOVERY_TIMEOUT

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: Optional[float] = None
    success_count: int = 0

    def record_success(self):
        """Record successful operation."""
        self.failure_count = 0
        self.success_count += 1
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            logger.info("Circuit breaker: CLOSED (recovered)")

    def record_failure(self, error: str = None):
        """Record failed operation."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(f"Circuit breaker: OPEN (failures={self.failure_count}, error={error})")

    def can_execute(self) -> bool:
        """Check if operation can proceed."""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if self.last_failure_time:
                elapsed = time.time() - self.last_failure_time
                if elapsed >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    logger.info("Circuit breaker: HALF_OPEN (testing recovery)")
                    return True
            return False

        # HALF_OPEN: allow one request to test
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.name,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_failure": self.last_failure_time,
        }


# =============================================================================
# LRU CACHE WITH TTL
# =============================================================================

class TTLCache:
    """LRU cache with time-to-live for embeddings."""

    def __init__(self, max_size: int = 1000, ttl: int = 3600):
        self.max_size = max_size
        self.ttl = ttl
        self.cache: OrderedDict = OrderedDict()
        self.timestamps: Dict[str, float] = {}
        self._lock = asyncio.Lock()

        # Stats
        self.hits = 0
        self.misses = 0

    async def get(self, key: str) -> Optional[Any]:
        """Get item from cache if exists and not expired."""
        async with self._lock:
            if key not in self.cache:
                self.misses += 1
                return None

            # Check expiry
            if time.time() - self.timestamps[key] > self.ttl:
                del self.cache[key]
                del self.timestamps[key]
                self.misses += 1
                return None

            # Move to end (most recently used)
            self.cache.move_to_end(key)
            self.hits += 1
            return self.cache[key]

    async def set(self, key: str, value: Any):
        """Set item in cache with TTL."""
        async with self._lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            else:
                if len(self.cache) >= self.max_size:
                    # Remove oldest item
                    oldest_key = next(iter(self.cache))
                    del self.cache[oldest_key]
                    del self.timestamps[oldest_key]

            self.cache[key] = value
            self.timestamps[key] = time.time()

    @property
    def hit_rate(self) -> float:
        """Calculate cache hit rate."""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def stats(self) -> Dict[str, Any]:
        return {
            "size": len(self.cache),
            "max_size": self.max_size,
            "ttl": self.ttl,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": f"{self.hit_rate * 100:.1f}%",
        }


# =============================================================================
# OPTIMIZED MODEL LOADER v20.0.0
# =============================================================================

class OptimizedModelLoader:
    """
    Loader for optimized ECAPA models (JIT, ONNX, Quantized).

    Supports multiple optimization strategies with automatic fallback:
    1. TorchScript JIT (traced or scripted)
    2. ONNX Runtime
    3. Dynamic Quantization
    4. Standard SpeechBrain (fallback)

    v20.0.0 Features:
    - Auto-detection of available optimized models
    - Manifest-based model selection
    - Comprehensive timing and telemetry
    - Graceful fallback to standard loading
    """

    def __init__(self, cache_dir: str, device: str = "cpu"):
        self.cache_dir = cache_dir
        self.device = device

        # Model instances
        self._jit_model = None
        self._onnx_session = None
        self._quantized_model = None
        self._speechbrain_encoder = None

        # Feature extraction components (for JIT/quantized models)
        self._compute_features = None
        self._mean_var_norm = None

        # Torchaudio feature extraction (v20.2.0 - for JIT without SpeechBrain)
        self._mel_transform = None
        self._amplitude_to_db = None

        # State
        self.active_strategy: Optional[str] = None
        self.requires_feature_extraction: bool = False

        # Telemetry
        self.load_time_ms: float = 0.0
        self.model_size_mb: float = 0.0
        self.optimization_manifest: Optional[Dict[str, Any]] = None

    def _load_optimization_manifest(self) -> Optional[Dict[str, Any]]:
        """Load the optimization manifest created during compilation."""
        manifest_paths = [
            CloudECAPAConfig.OPTIMIZATION_MANIFEST,
            os.path.join(self.cache_dir, ".optimization_manifest.json"),
        ]

        for path in manifest_paths:
            if os.path.exists(path):
                try:
                    with open(path, 'r') as f:
                        manifest = json.load(f)
                    logger.info(f"Loaded optimization manifest from {path}")
                    logger.info(f"   Version: {manifest.get('version', 'unknown')}")
                    logger.info(f"   Best strategy: {manifest.get('best_strategy', 'none')}")
                    return manifest
                except Exception as e:
                    logger.warning(f"Failed to load manifest from {path}: {e}")

        return None

    def _find_optimized_models(self) -> Dict[str, str]:
        """Find all available optimized model files."""
        available = {}

        for strategy, filename in CloudECAPAConfig.OPTIMIZED_MODEL_FILES.items():
            path = os.path.join(self.cache_dir, filename)
            if os.path.exists(path):
                size_mb = os.path.getsize(path) / (1024 * 1024)
                available[strategy] = path
                logger.debug(f"Found optimized model: {strategy} ({size_mb:.2f} MB)")

        return available

    def _select_best_strategy(self, available: Dict[str, str]) -> Optional[str]:
        """Select the best optimization strategy based on preference and availability."""
        preferred = CloudECAPAConfig.PREFERRED_STRATEGY.lower()

        # If specific strategy requested, use it if available
        strategy_mapping = {
            "jit": ["jit_trace", "jit_script"],
            "onnx": ["onnx"],
            "quantized": ["quantize_dynamic"],
            "quantize": ["quantize_dynamic"],
        }

        if preferred != "auto" and preferred in strategy_mapping:
            for strategy in strategy_mapping[preferred]:
                if strategy in available:
                    return strategy
            logger.warning(f"Preferred strategy '{preferred}' not available")

        # Auto selection: prioritize based on manifest recommendation or default order
        if self.optimization_manifest:
            best = self.optimization_manifest.get("best_strategy")
            if best and best in available:
                return best

        # Default priority order
        priority = ["jit_trace", "onnx", "quantize_dynamic", "jit_script"]
        for strategy in priority:
            if strategy in available:
                return strategy

        return None

    def load_jit_model(self, model_path: str) -> bool:
        """
        Load a TorchScript JIT model.

        v20.2.0: JIT model only contains embedding extraction.
        Feature extraction is done with torchaudio (no SpeechBrain at runtime).
        """
        logger.info(f"Loading JIT model from {model_path}...")
        start = time.time()

        try:
            # Load JIT config to determine if model needs features or audio
            config_path = os.path.join(self.cache_dir, "ecapa_jit_config.json")
            jit_config = {}
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    jit_config = json.load(f)
                logger.info(f"Loaded JIT config: input_type={jit_config.get('input_type', 'audio')}")

            # Check if this JIT model needs features (v20.2.0 embedding-only model)
            needs_features = jit_config.get("input_type") == "features"

            # Load the JIT model
            self._jit_model = torch.jit.load(model_path, map_location=self.device)
            self._jit_model.eval()

            if needs_features:
                # Set up torchaudio-based feature extraction (no SpeechBrain!)
                logger.info("Setting up torchaudio feature extraction...")
                import torchaudio
                import torchaudio.transforms as T

                n_mels = jit_config.get("n_mels", 80)
                sample_rate = jit_config.get("sample_rate", 16000)
                n_fft = jit_config.get("n_fft", 400)
                hop_length = jit_config.get("hop_length", 160)
                win_length = jit_config.get("win_length", 400)
                f_min = jit_config.get("f_min", 20)
                f_max = jit_config.get("f_max", 7600)

                # Create MelSpectrogram transform
                self._mel_transform = T.MelSpectrogram(
                    sample_rate=sample_rate,
                    n_fft=n_fft,
                    hop_length=hop_length,
                    win_length=win_length,
                    n_mels=n_mels,
                    f_min=f_min,
                    f_max=f_max,
                    power=2.0,
                )

                # Create amplitude to dB transform (log mel)
                self._amplitude_to_db = T.AmplitudeToDB(stype='power', top_db=80)

                self.requires_feature_extraction = True
                logger.info(f"   MelSpec: {n_mels} mels, {sample_rate}Hz, hop={hop_length}")

                # Run warmup with features
                with torch.no_grad():
                    test_audio = torch.randn(1, 32000)
                    test_features = self._extract_features_torchaudio(test_audio)
                    _ = self._jit_model(test_features)
            else:
                # Legacy: JIT model takes audio directly
                self.requires_feature_extraction = False
                self._mel_transform = None
                self._amplitude_to_db = None

                # Run warmup with audio
                with torch.no_grad():
                    test_audio = torch.randn(1, 32000)
                    _ = self._jit_model(test_audio)

            self.load_time_ms = (time.time() - start) * 1000
            self.model_size_mb = os.path.getsize(model_path) / (1024 * 1024)

            logger.info(f"JIT model loaded in {self.load_time_ms:.1f}ms ({self.model_size_mb:.2f} MB)")
            logger.info(f"   Feature extraction: {'torchaudio' if needs_features else 'built-in'}")
            return True

        except Exception as e:
            logger.error(f"Failed to load JIT model: {e}")
            logger.debug(traceback.format_exc())
            return False

    def _extract_features_torchaudio(self, audio: torch.Tensor) -> torch.Tensor:
        """
        Extract mel features using torchaudio (no SpeechBrain dependency).

        Args:
            audio: Raw audio [batch, samples]

        Returns:
            Mel features [batch, time, n_mels]
        """
        if self._mel_transform is None:
            raise RuntimeError("MelSpectrogram transform not initialized")

        # Ensure audio is 2D: [batch, samples]
        if audio.dim() == 1:
            audio = audio.unsqueeze(0)

        # Compute mel spectrogram: [batch, n_mels, time]
        mel = self._mel_transform(audio)

        # Convert to log scale (dB)
        mel_db = self._amplitude_to_db(mel)

        # Transpose to [batch, time, n_mels] for ECAPA model
        mel_db = mel_db.transpose(1, 2)

        # Apply simple normalization (mean/var norm approximation)
        mel_db = (mel_db - mel_db.mean(dim=1, keepdim=True)) / (mel_db.std(dim=1, keepdim=True) + 1e-6)

        return mel_db

    def load_onnx_model(self, model_path: str) -> bool:
        """Load an ONNX model with ONNXRuntime."""
        logger.info(f"Loading ONNX model from {model_path}...")
        start = time.time()

        try:
            import onnxruntime as ort

            # Configure ONNX Runtime session
            sess_options = ort.SessionOptions()
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess_options.intra_op_num_threads = 4
            sess_options.inter_op_num_threads = 2

            # Use CPU execution provider (Cloud Run doesn't have GPU)
            providers = ["CPUExecutionProvider"]

            self._onnx_session = ort.InferenceSession(
                model_path,
                sess_options,
                providers=providers
            )

            # Run warmup inference
            test_audio = np.random.randn(1, 32000).astype(np.float32) * 0.1
            _ = self._onnx_session.run(["embedding"], {"audio": test_audio})

            self.load_time_ms = (time.time() - start) * 1000
            self.model_size_mb = os.path.getsize(model_path) / (1024 * 1024)
            self.requires_feature_extraction = False  # Full pipeline ONNX

            logger.info(f"ONNX model loaded in {self.load_time_ms:.1f}ms ({self.model_size_mb:.2f} MB)")
            logger.info(f"   ONNXRuntime version: {ort.__version__}")
            return True

        except ImportError:
            logger.error("ONNXRuntime not installed. Install with: pip install onnxruntime")
            return False
        except Exception as e:
            logger.error(f"Failed to load ONNX model: {e}")
            logger.debug(traceback.format_exc())
            return False

    def load_quantized_model(self, model_path: str) -> bool:
        """Load a quantized PyTorch model."""
        logger.info(f"Loading quantized model from {model_path}...")
        start = time.time()

        try:
            self._quantized_model = torch.jit.load(model_path, map_location=self.device)
            self._quantized_model.eval()

            # Quantized models typically require feature extraction
            self.requires_feature_extraction = True

            self.load_time_ms = (time.time() - start) * 1000
            self.model_size_mb = os.path.getsize(model_path) / (1024 * 1024)

            logger.info(f"Quantized model loaded in {self.load_time_ms:.1f}ms ({self.model_size_mb:.2f} MB)")
            return True

        except Exception as e:
            logger.error(f"Failed to load quantized model: {e}")
            logger.debug(traceback.format_exc())
            return False

    def load_speechbrain_encoder(self) -> bool:
        """Load standard SpeechBrain encoder as fallback."""
        logger.info("Loading standard SpeechBrain encoder (fallback)...")
        start = time.time()

        try:
            # v20.2.0: Lazy import SpeechBrain only when needed
            EC = _lazy_import_speechbrain()
            self._speechbrain_encoder = EC.from_hparams(
                source=CloudECAPAConfig.MODEL_PATH,
                savedir=self.cache_dir,
                run_opts={"device": self.device}
            )

            # Store feature extraction components for quantized model fallback
            self._compute_features = self._speechbrain_encoder.mods.compute_features
            self._mean_var_norm = self._speechbrain_encoder.mods.mean_var_norm

            self.load_time_ms = (time.time() - start) * 1000
            self.requires_feature_extraction = False

            logger.info(f"SpeechBrain encoder loaded in {self.load_time_ms:.1f}ms")
            return True

        except Exception as e:
            logger.error(f"Failed to load SpeechBrain encoder: {e}")
            logger.debug(traceback.format_exc())
            return False

    def extract_features(self, audio: torch.Tensor) -> torch.Tensor:
        """Extract mel features for quantized model inference."""
        if self._compute_features is None or self._mean_var_norm is None:
            raise RuntimeError("Feature extraction components not loaded")

        with torch.no_grad():
            feats = self._compute_features(audio)
            lens = torch.ones(feats.shape[0], device=feats.device)
            feats = self._mean_var_norm(feats, lens)
            return feats

    def load(self) -> bool:
        """
        Load the best available optimized model.

        Priority:
        1. Check optimization manifest for recommended model
        2. Try to load preferred strategy
        3. Fall back to available optimized models
        4. Fall back to standard SpeechBrain loading
        """
        logger.info("=" * 60)
        logger.info("OPTIMIZED MODEL LOADING v20.0.0")
        logger.info("=" * 60)

        # Skip optimization if disabled
        if not CloudECAPAConfig.USE_OPTIMIZED:
            logger.info("Optimized loading disabled, using standard SpeechBrain")
            if self.load_speechbrain_encoder():
                self.active_strategy = "speechbrain"
                return True
            return False

        # Load manifest
        self.optimization_manifest = self._load_optimization_manifest()

        # Find available optimized models
        available = self._find_optimized_models()
        logger.info(f"Available optimized models: {list(available.keys())}")

        if not available:
            logger.info("No optimized models found, falling back to SpeechBrain")
            if self.load_speechbrain_encoder():
                self.active_strategy = "speechbrain"
                return True
            return False

        # Select best strategy
        selected = self._select_best_strategy(available)
        logger.info(f"Selected strategy: {selected}")

        if selected is None:
            logger.warning("No suitable strategy found, falling back to SpeechBrain")
            if self.load_speechbrain_encoder():
                self.active_strategy = "speechbrain"
                return True
            return False

        # Try to load selected model
        model_path = available[selected]
        success = False

        if selected in ["jit_trace", "jit_script"]:
            success = self.load_jit_model(model_path)
        elif selected == "onnx":
            success = self.load_onnx_model(model_path)
        elif selected == "quantize_dynamic":
            success = self.load_quantized_model(model_path)
            # Quantized models need feature extraction, load SpeechBrain for that
            if success and self.requires_feature_extraction:
                logger.info("Loading feature extraction components...")
                self.load_speechbrain_encoder()

        if success:
            self.active_strategy = selected
            logger.info(f"Model loaded successfully with strategy: {selected}")
            return True

        # Fallback to SpeechBrain
        logger.warning(f"Failed to load {selected}, falling back to SpeechBrain")
        if self.load_speechbrain_encoder():
            self.active_strategy = "speechbrain"
            return True

        return False

    def encode(self, audio: torch.Tensor) -> torch.Tensor:
        """
        Extract speaker embedding from audio.

        Automatically uses the loaded optimized model.
        v20.2.0: JIT models use torchaudio for feature extraction.
        """
        with torch.no_grad():
            if self.active_strategy in ["jit_trace", "jit_script"]:
                # v20.2.0: JIT model may need features, not raw audio
                if self.requires_feature_extraction and self._mel_transform is not None:
                    features = self._extract_features_torchaudio(audio)
                    return self._jit_model(features).squeeze()
                else:
                    return self._jit_model(audio).squeeze()

            elif self.active_strategy == "onnx":
                audio_np = audio.numpy() if isinstance(audio, torch.Tensor) else audio
                result = self._onnx_session.run(["embedding"], {"audio": audio_np})
                return torch.tensor(result[0]).squeeze()

            elif self.active_strategy == "quantize_dynamic":
                features = self.extract_features(audio)
                return self._quantized_model(features).squeeze()

            elif self.active_strategy == "speechbrain":
                return self._speechbrain_encoder.encode_batch(audio).squeeze()

            else:
                raise RuntimeError(f"No model loaded (strategy: {self.active_strategy})")

    def status(self) -> Dict[str, Any]:
        """Get loader status and telemetry."""
        return {
            "active_strategy": self.active_strategy,
            "load_time_ms": round(self.load_time_ms, 2),
            "model_size_mb": round(self.model_size_mb, 2),
            "requires_feature_extraction": self.requires_feature_extraction,
            "optimization_manifest_loaded": self.optimization_manifest is not None,
            "available_strategies": list(self._find_optimized_models().keys()),
        }


# =============================================================================
# ECAPA MODEL MANAGER
# =============================================================================

class ECAPAModelManager:
    """
    Manages ECAPA-TDNN model lifecycle with async parallel loading and caching.

    v20.0.0 Features:
    - MULTI-STRATEGY OPTIMIZATION: JIT, ONNX, Quantization with auto-selection
    - INSTANT STARTUP: Uses optimized models for <2s cold starts
    - PARALLEL LOADING: Async initialization with thread pool execution
    - STRICT OFFLINE: Fast-fails if no valid cache (no network fallback)
    - PRE-WARMED: JIT/ONNX compilation already done during Docker build
    - Thread-safe async loading with proper locking
    - Comprehensive telemetry and diagnostics
    """

    # Thread pool for parallel model loading
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="ecapa_loader")

    def __init__(self):
        self.model = None
        self.device = CloudECAPAConfig.DEVICE
        self.model_path = CloudECAPAConfig.MODEL_PATH

        # Optimized model loader (v20.0.0)
        self._optimized_loader: Optional[OptimizedModelLoader] = None

        # Pre-baked manifest (instant cache verification)
        self._prebaked_manifest: Optional[Dict[str, Any]] = None
        self._prebaked_cache: Optional[str] = None
        self._cache_diagnostics: Dict[str, Any] = {}
        self._using_prebaked = False
        self._using_optimized = False

        # Will be set during initialization after cache discovery
        self.cache_dir = CloudECAPAConfig.CACHE_DIR

        self._loading = False
        self._load_lock = asyncio.Lock()
        self._ready = False
        self._error: Optional[str] = None
        self._init_start_time: Optional[float] = None

        # Telemetry
        self.load_time_ms: Optional[float] = None
        self.warmup_time_ms: Optional[float] = None
        self.inference_count = 0
        self.total_inference_time_ms = 0.0
        self.load_source: str = "unknown"  # "jit_trace", "onnx", "quantized", "speechbrain"

        # Circuit breaker
        self.circuit_breaker = CircuitBreaker()

        # Embedding cache
        self.embedding_cache = TTLCache(
            max_size=CloudECAPAConfig.CACHE_MAX_SIZE,
            ttl=CloudECAPAConfig.CACHE_TTL
        )

        # Immediately check for pre-baked manifest on construction
        self._prebaked_manifest = CloudECAPAConfig.load_prebaked_manifest()

    @property
    def is_ready(self) -> bool:
        # Ready if we have an optimized loader with a strategy, or a standard model
        if self._optimized_loader and self._optimized_loader.active_strategy:
            return self._ready
        return self._ready and self.model is not None

    @property
    def avg_inference_ms(self) -> float:
        if self.inference_count == 0:
            return 0.0
        return self.total_inference_time_ms / self.inference_count

    async def initialize(self) -> bool:
        """
        Initialize and load the ECAPA model with manifest-based INSTANT verification.

        v19.0.0 Enhancement:
        - If pre-baked manifest exists, use it for INSTANT cache verification (no file scan)
        - Skip redundant file checks when manifest confirms cache integrity
        - Fast-fail if strict offline mode and no valid cache
        - Parallel loading with proper async patterns
        """
        async with self._load_lock:
            if self._ready:
                return True

            if self._loading:
                # Wait for ongoing load
                for _ in range(60):  # 60 second timeout
                    await asyncio.sleep(1)
                    if self._ready:
                        return True
                return False

            self._loading = True
            self._init_start_time = time.time()

            try:
                logger.info("=" * 60)
                logger.info("ECAPA-TDNN Cloud Service v19.0.0 Initialization")
                logger.info("=" * 60)
                logger.info(f"Model: {self.model_path}")
                logger.info(f"Device: {self.device}")
                logger.info(f"Strict Offline: {CloudECAPAConfig.STRICT_OFFLINE}")

                start_time = time.time()

                # =============================================================
                # v19.0.0 INSTANT VERIFICATION VIA MANIFEST
                # =============================================================
                # If we have a pre-baked manifest, trust it and skip file scanning
                if self._prebaked_manifest:
                    logger.info("=" * 50)
                    logger.info("✅ PRE-BAKED MANIFEST DETECTED - INSTANT VERIFICATION")
                    logger.info("=" * 50)
                    manifest_version = self._prebaked_manifest.get('version', 'unknown')
                    manifest_cache = self._prebaked_manifest.get('cache_dir', CloudECAPAConfig.SOURCE_CACHE)
                    prebaked_time = self._prebaked_manifest.get('prebaked_at', 'unknown')

                    logger.info(f"   Manifest version: {manifest_version}")
                    logger.info(f"   Pre-baked at: {prebaked_time}")
                    logger.info(f"   Cache directory: {manifest_cache}")
                    logger.info(f"   Embedding dim: {self._prebaked_manifest.get('embedding_dim', 192)}")

                    # Use manifest cache directory
                    if os.path.isdir(manifest_cache):
                        self.cache_dir = manifest_cache
                        self._prebaked_cache = manifest_cache
                        self._using_prebaked = True
                        self.load_source = "prebaked_manifest"
                        logger.info(f"   Using manifest-verified cache: {manifest_cache}")
                    else:
                        logger.warning(f"   Manifest cache not found at {manifest_cache}, falling back to discovery")
                        self._prebaked_manifest = None  # Clear manifest to trigger discovery
                else:
                    logger.info("No pre-baked manifest found, will discover cache...")

                # Ensure cache directory exists
                os.makedirs(self.cache_dir, exist_ok=True)

                # Load SpeechBrain ECAPA model
                await self._load_model()

                self.load_time_ms = (time.time() - start_time) * 1000
                logger.info(f"Model loaded in {self.load_time_ms:.0f}ms")

                # Run warmup if enabled (should be fast since JIT is pre-compiled)
                if CloudECAPAConfig.WARMUP_ON_START:
                    await self._warmup()

                self._ready = True
                total_init_time = (time.time() - self._init_start_time) * 1000

                logger.info("=" * 60)
                logger.info(f"✅ ECAPA-TDNN READY - Total init: {total_init_time:.0f}ms")
                logger.info(f"   Load source: {self.load_source}")
                logger.info(f"   Using prebaked: {self._using_prebaked}")
                logger.info("=" * 60)

                return True

            except Exception as e:
                self._error = str(e)
                logger.error(f"ECAPA initialization failed: {e}")
                logger.error(traceback.format_exc())

                # In strict offline mode, fail fast if no valid cache
                if CloudECAPAConfig.STRICT_OFFLINE:
                    logger.error("STRICT OFFLINE MODE: No valid pre-baked cache available!")
                    logger.error("Build Docker image with pre-baking to enable fast cold starts.")

                return False
            finally:
                self._loading = False

    async def _load_model(self):
        """Load the ECAPA-TDNN model using optimized loader (runs in thread pool)."""
        logger.info("Starting optimized model load in thread pool...")
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._load_model_optimized)
            logger.info("Thread pool model load completed")
        except Exception as e:
            logger.error(f"Thread pool model load failed: {e}")
            raise

    def _load_model_optimized(self):
        """
        Load model using OptimizedModelLoader (v20.0.0).

        This is the new primary loading path that tries:
        1. TorchScript JIT (fastest, <2s)
        2. ONNX Runtime (fast, portable)
        3. Quantized model (smaller, fast)
        4. Standard SpeechBrain (fallback)
        """
        logger.info("=" * 60)
        logger.info("ECAPA MODEL LOADING v20.0.0 - Optimized Path")
        logger.info("=" * 60)

        # Determine cache directory
        if self._using_prebaked and self._prebaked_cache:
            cache_dir = self._prebaked_cache
        else:
            # Find valid cache
            cache_dir, _, _ = CloudECAPAConfig.find_valid_cache()
            if not cache_dir:
                cache_dir = CloudECAPAConfig.CACHE_DIR

        logger.info(f"Using cache directory: {cache_dir}")
        self.cache_dir = cache_dir

        # Create optimized loader
        self._optimized_loader = OptimizedModelLoader(
            cache_dir=cache_dir,
            device=self.device
        )

        # Load model
        load_start = time.time()
        success = self._optimized_loader.load()

        if success:
            self._using_optimized = True
            self.load_source = self._optimized_loader.active_strategy
            self.load_time_ms = self._optimized_loader.load_time_ms

            logger.info("=" * 60)
            logger.info(f"ECAPA MODEL LOADED - Strategy: {self.load_source}")
            logger.info(f"   Load time: {self.load_time_ms:.1f}ms")
            logger.info(f"   Model size: {self._optimized_loader.model_size_mb:.2f}MB")
            logger.info("=" * 60)
        else:
            # Fall back to legacy loading
            logger.warning("Optimized loading failed, trying legacy path...")
            self._load_model_sync()

            self._using_optimized = False

    def _load_model_sync(self):
        """
        Synchronous model loading with pre-baked cache detection.

        v19.0.0 Enhancement:
        - FAST PATH: If manifest already verified cache, skip redundant discovery
        - STRICT OFFLINE: Fast-fail if no cache and offline mode
        - PARALLEL: Use thread pool executor for non-blocking load

        Priority:
        1. Use manifest-verified cache (fastest - no file scanning)
        2. Use pre-baked Docker cache if available (fast - minimal scanning)
        3. Use runtime cache if populated by entrypoint.sh
        4. Fall back to fresh HuggingFace download (slowest) - ONLY if not strict offline
        """
        logger.info("_load_model_sync: Function entered")
        logger.info("=" * 50)
        logger.info("ECAPA MODEL LOADING v19.0.0 - Cache Detection")
        logger.info("=" * 50)

        # =================================================================
        # FAST PATH: Manifest already verified cache in initialize()
        # =================================================================
        if self._using_prebaked and self._prebaked_cache:
            logger.info(f"✅ FAST PATH: Using manifest-verified cache: {self._prebaked_cache}")
            # Skip discovery - already verified
        else:
            # Step 1: Detect best available cache
            prebaked_cache, cache_msg, all_diag = CloudECAPAConfig.find_valid_cache()
            self._cache_diagnostics = all_diag

            if prebaked_cache:
                logger.info(f"✅ PRE-BAKED CACHE DETECTED: {prebaked_cache}")
                self._prebaked_cache = prebaked_cache
                self._using_prebaked = True
                self.cache_dir = prebaked_cache
                self.load_source = "prebaked"
            else:
                logger.warning(f"⚠️ No pre-baked cache found: {cache_msg}")

                # STRICT OFFLINE MODE: Fast-fail if no cache
                if CloudECAPAConfig.STRICT_OFFLINE:
                    raise RuntimeError(
                        f"STRICT OFFLINE MODE: No valid pre-baked cache found! "
                        f"Checked locations: {CloudECAPAConfig.get_cache_locations()}. "
                        f"Build Docker image with pre-baking to enable fast cold starts."
                    )

                logger.info("   Will attempt fresh download from HuggingFace")
                self.load_source = "fresh_download"

                # Ensure runtime cache directory exists
                os.makedirs(self.cache_dir, exist_ok=True)

        # Set HuggingFace offline mode if using pre-baked cache
        if self._using_prebaked and CloudECAPAConfig.SKIP_HF_DOWNLOAD:
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            logger.info("   HuggingFace download disabled (using pre-baked cache)")

        # Step 2: Determine device dynamically
        if self.device == "mps" and torch.backends.mps.is_available():
            device = "mps"
        elif self.device == "cuda" and torch.cuda.is_available():
            device = "cuda"
        else:
            device = "cpu"

        logger.info(f"   Device: {device}")
        logger.info(f"   Cache dir: {self.cache_dir}")
        logger.info(f"   Model source: {self.model_path}")
        logger.info(f"   Load source: {self.load_source}")
        logger.info("=" * 50)

        # Step 3: Load model with retry logic
        load_start = time.time()
        max_retries = 2 if not CloudECAPAConfig.STRICT_OFFLINE else 1

        for attempt in range(max_retries):
            try:
                logger.info(f"Loading ECAPA-TDNN model (attempt {attempt + 1}/{max_retries})...")

                self.model = EncoderClassifier.from_hparams(
                    source=self.model_path,
                    savedir=self.cache_dir,
                    run_opts={"device": device}
                )

                load_duration = (time.time() - load_start) * 1000
                self.device = device

                logger.info("=" * 50)
                logger.info(f"✅ ECAPA-TDNN LOADED SUCCESSFULLY")
                logger.info(f"   Device: {device}")
                logger.info(f"   Source: {self.load_source}")
                logger.info(f"   Load time: {load_duration:.0f}ms")
                logger.info(f"   Attempts: {attempt + 1}")
                logger.info("=" * 50)
                return  # Success!

            except Exception as e:
                logger.error(f"❌ Model loading attempt {attempt + 1} failed: {e}")

                # Last attempt failed
                if attempt == max_retries - 1:
                    # If pre-baked cache failed and not strict offline, try fresh download
                    if self._using_prebaked and not CloudECAPAConfig.STRICT_OFFLINE:
                        logger.warning("🔄 Pre-baked cache load failed, attempting fresh download...")

                        # Disable offline mode
                        os.environ.pop("HF_HUB_OFFLINE", None)
                        os.environ.pop("TRANSFORMERS_OFFLINE", None)

                        # Use runtime cache for fresh download
                        self.cache_dir = CloudECAPAConfig.CACHE_DIR
                        os.makedirs(self.cache_dir, exist_ok=True)
                        self.load_source = "fresh_download_fallback"
                        self._using_prebaked = False

                        self.model = EncoderClassifier.from_hparams(
                            source=self.model_path,
                            savedir=self.cache_dir,
                            run_opts={"device": device}
                        )

                        self.device = device
                        load_duration = (time.time() - load_start) * 1000
                        logger.info(f"✅ ECAPA-TDNN loaded via fresh download fallback on {device} ({load_duration:.0f}ms)")
                        return
                    else:
                        raise

                # Wait before retry with exponential backoff
                wait_time = 2 ** attempt
                logger.info(f"   Retrying in {wait_time}s...")
                time.sleep(wait_time)

    async def _warmup(self):
        """
        Run warmup inference with multiple test patterns.

        v19.0.0 Enhancement:
        - Multiple test patterns to ensure full JIT compilation
        - Parallel warmup for faster completion (if pre-baked, JIT already done)
        - Varied audio lengths to cover different input sizes
        """
        logger.info("Running warmup inference v19.0.0...")

        start_time = time.time()

        # Generate multiple test patterns for comprehensive warmup
        test_patterns = [
            ("silence_1s", np.zeros(16000, dtype=np.float32)),
            ("silence_2s", np.zeros(32000, dtype=np.float32)),
            ("noise_1s", np.random.randn(16000).astype(np.float32) * 0.1),
        ]

        warmup_results = []

        try:
            for pattern_name, test_audio in test_patterns:
                pattern_start = time.time()
                logger.info(f"Warmup pattern: {pattern_name} ({len(test_audio)} samples)...")

                # Convert to tensor
                audio_tensor = torch.tensor(test_audio).unsqueeze(0)

                # Run inference
                with torch.no_grad():
                    embedding = self.model.encode_batch(audio_tensor).squeeze().cpu().numpy()

                pattern_time_ms = (time.time() - pattern_start) * 1000

                warmup_results.append({
                    "pattern": pattern_name,
                    "samples": len(test_audio),
                    "embedding_shape": embedding.shape,
                    "time_ms": round(pattern_time_ms, 1),
                })

                logger.info(f"   {pattern_name}: {embedding.shape} in {pattern_time_ms:.0f}ms")

            self.warmup_time_ms = (time.time() - start_time) * 1000

            # Log summary
            logger.info("=" * 50)
            logger.info(f"✅ WARMUP COMPLETE - Total: {self.warmup_time_ms:.0f}ms")
            for result in warmup_results:
                logger.info(f"   {result['pattern']}: {result['time_ms']:.0f}ms")

            # Check if JIT was already compiled (fast warmup = pre-baked)
            if self.warmup_time_ms < 5000:
                logger.info("   ⚡ JIT already compiled (pre-baked cache)")
            else:
                logger.info("   🔨 JIT compiled during warmup")
            logger.info("=" * 50)

        except Exception as e:
            logger.warning(f"Warmup inference failed (non-critical): {e}")
            logger.warning(traceback.format_exc())
            self.warmup_time_ms = (time.time() - start_time) * 1000

    async def extract_embedding(
        self,
        audio_data: np.ndarray,
        use_cache: bool = True
    ) -> Optional[np.ndarray]:
        """
        Extract speaker embedding from audio.

        Args:
            audio_data: Audio as numpy array (float32, 16kHz)
            use_cache: Whether to check/use embedding cache

        Returns:
            192-dimensional speaker embedding or None on failure
        """
        if not self.is_ready:
            if not await self.initialize():
                raise RuntimeError("ECAPA model not available")

        # Check circuit breaker
        if not self.circuit_breaker.can_execute():
            raise RuntimeError(f"Circuit breaker OPEN: {self.circuit_breaker.failure_count} failures")

        # Check cache
        if use_cache:
            cache_key = self._compute_audio_hash(audio_data)
            cached = await self.embedding_cache.get(cache_key)
            if cached is not None:
                logger.debug(f"Cache hit for audio hash {cache_key[:8]}...")
                return cached

        # Extract embedding
        start_time = time.time()

        try:
            # Ensure audio is normalized float32
            if audio_data.dtype != np.float32:
                audio_data = audio_data.astype(np.float32)

            # Normalize if needed
            max_val = np.abs(audio_data).max()
            if max_val > 1.0:
                audio_data = audio_data / max_val

            # Convert to tensor
            audio_tensor = torch.tensor(audio_data).unsqueeze(0)

            # Use optimized loader if available (v20.0.0)
            if self._using_optimized and self._optimized_loader:
                # Run in thread pool to avoid blocking
                loop = asyncio.get_running_loop()
                embedding = await loop.run_in_executor(
                    None,
                    lambda: self._optimized_loader.encode(audio_tensor).cpu().numpy()
                )
            else:
                # Legacy path using standard SpeechBrain model
                loop = asyncio.get_running_loop()
                embedding = await loop.run_in_executor(
                    None,
                    lambda: self.model.encode_batch(audio_tensor).squeeze().cpu().numpy()
                )

            # Update stats
            inference_time = (time.time() - start_time) * 1000
            self.inference_count += 1
            self.total_inference_time_ms += inference_time

            self.circuit_breaker.record_success()

            # Cache result
            if use_cache:
                await self.embedding_cache.set(cache_key, embedding)

            logger.debug(f"Embedding extracted in {inference_time:.0f}ms, shape: {embedding.shape}")

            return embedding

        except Exception as e:
            self.circuit_breaker.record_failure(str(e))
            logger.error(f"Embedding extraction failed: {e}")
            raise

    def _compute_audio_hash(self, audio: np.ndarray) -> str:
        """Compute hash of audio for caching."""
        return hashlib.sha256(audio.tobytes()).hexdigest()

    async def compute_similarity(
        self,
        embedding1: np.ndarray,
        embedding2: np.ndarray
    ) -> float:
        """Compute cosine similarity between two embeddings."""
        norm1 = np.linalg.norm(embedding1)
        norm2 = np.linalg.norm(embedding2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(np.dot(embedding1, embedding2) / (norm1 * norm2))

    def status(self) -> Dict[str, Any]:
        """Get detailed model status including cache and optimization diagnostics."""
        status_dict = {
            "ready": self.is_ready,
            "loading": self._loading,
            "error": self._error,
            "device": self.device,
            "model_path": self.model_path,
            "load_time_ms": self.load_time_ms,
            "warmup_time_ms": self.warmup_time_ms,
            "inference_count": self.inference_count,
            "avg_inference_ms": round(self.avg_inference_ms, 2),
            "circuit_breaker": self.circuit_breaker.to_dict(),
            "embedding_cache": self.embedding_cache.stats(),
            # Pre-baked cache information
            "model_cache": {
                "cache_dir": self.cache_dir,
                "load_source": self.load_source,
                "using_prebaked": self._using_prebaked,
                "prebaked_cache_path": self._prebaked_cache,
                "diagnostics": self._cache_diagnostics,
            },
            # Optimized model loader information (v20.0.0)
            "optimization": {
                "using_optimized": self._using_optimized,
                "loader_status": self._optimized_loader.status() if self._optimized_loader else None,
            },
        }

        return status_dict


# =============================================================================
# FASTAPI APPLICATION
# =============================================================================

# Global model manager
_model_manager: Optional[ECAPAModelManager] = None


def get_model_manager() -> ECAPAModelManager:
    """Get or create the global model manager."""
    global _model_manager
    if _model_manager is None:
        _model_manager = ECAPAModelManager()
    return _model_manager


# Create FastAPI app
try:
    from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
    from fastapi.responses import JSONResponse
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel, Field
    import uvicorn

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    logger.warning("FastAPI not available - HTTP server disabled")


if FASTAPI_AVAILABLE:

    app = FastAPI(
        title="ECAPA Cloud Service",
        description="Cloud ECAPA-TDNN Speaker Embedding Service - Ultra-Fast Cold Starts with JIT/ONNX/Quantization",
        version="20.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # =========================================================================
    # Request/Response Models
    # =========================================================================

    class EmbeddingRequest(BaseModel):
        """Request for speaker embedding extraction."""
        audio_data: str = Field(..., description="Base64 encoded audio bytes")
        sample_rate: int = Field(default=16000, description="Audio sample rate")
        format: str = Field(default="float32", description="Audio format (int16, float32)")
        test_mode: bool = Field(default=False, description="Test mode flag")

    class EmbeddingResponse(BaseModel):
        """Response with extracted embedding."""
        success: bool
        embedding: Optional[List[float]] = None
        embedding_size: Optional[int] = None
        processing_time_ms: Optional[float] = None
        cached: bool = False
        error: Optional[str] = None

    class VerifyRequest(BaseModel):
        """Request for speaker verification."""
        audio_data: str = Field(..., description="Base64 encoded audio to verify")
        reference_embedding: List[float] = Field(..., description="Reference embedding")
        sample_rate: int = Field(default=16000)
        format: str = Field(default="float32")

    class VerifyResponse(BaseModel):
        """Response with verification result."""
        success: bool
        verified: bool = False
        similarity: float = 0.0
        confidence: float = 0.0
        threshold: float = 0.85
        processing_time_ms: Optional[float] = None
        error: Optional[str] = None

    class BatchEmbeddingRequest(BaseModel):
        """Request for batch embedding extraction."""
        audio_samples: List[str] = Field(..., description="List of base64 encoded audio")
        sample_rate: int = Field(default=16000)
        format: str = Field(default="float32")

    class BatchEmbeddingResponse(BaseModel):
        """Response with batch embeddings."""
        success: bool
        embeddings: List[Optional[List[float]]] = []
        processing_time_ms: Optional[float] = None
        error: Optional[str] = None

    # =========================================================================
    # Health & Status Endpoints
    # =========================================================================

    @app.on_event("startup")
    async def startup_event():
        """Initialize model on startup."""
        logger.info("Starting ECAPA Cloud Service...")
        manager = get_model_manager()

        # Start initialization in background with exception handling
        async def init_with_logging():
            try:
                logger.info("Background initialization task starting...")
                result = await manager.initialize()
                logger.info(f"Background initialization completed: {result}")
            except Exception as e:
                logger.error(f"Background initialization failed: {e}")
                import traceback
                logger.error(traceback.format_exc())

        asyncio.create_task(init_with_logging())

    @app.get("/health")
    async def health_check():
        """Health check endpoint with cache status."""
        manager = get_model_manager()

        response = {
            "status": "healthy" if manager.is_ready else "initializing",
            "ecapa_ready": manager.is_ready,
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Include cache info if model is loaded
        if manager.is_ready:
            response["load_source"] = manager.load_source
            response["using_prebaked_cache"] = manager._using_prebaked
            response["load_time_ms"] = manager.load_time_ms

        return response

    @app.get("/status")
    async def get_status():
        """Detailed service status with optimization info."""
        manager = get_model_manager()

        return {
            "service": "ecapa_cloud_service",
            "version": "20.0.0",
            "config": CloudECAPAConfig.to_dict(),
            "model": manager.status(),
            "prebaked_manifest": manager._prebaked_manifest,
            "optimization_manifest": manager._optimized_loader.optimization_manifest if manager._optimized_loader else None,
            "timestamp": datetime.utcnow().isoformat(),
        }

    @app.get("/api/ml/health")
    async def ml_health():
        """ML API health endpoint (for compatibility)."""
        manager = get_model_manager()

        return {
            "status": "healthy" if manager.is_ready else "initializing",
            "ecapa_ready": manager.is_ready,
            "circuit_breaker": manager.circuit_breaker.state.name,
        }

    @app.post("/api/ml/prewarm")
    async def prewarm_model():
        """
        Pre-warm the ECAPA model for faster subsequent requests.

        v19.0.0 Enhancement:
        - Forces model initialization if not already loaded
        - Runs comprehensive warmup with multiple test patterns
        - Returns detailed timing and diagnostics

        This endpoint is useful for:
        - Triggering cold start before actual requests
        - Verifying model is working correctly
        - Getting diagnostic information about model state
        """
        start_time = time.time()
        manager = get_model_manager()

        result = {
            "success": False,
            "was_ready": manager.is_ready,
            "initialization_time_ms": None,
            "warmup_time_ms": None,
            "total_time_ms": None,
            "load_source": None,
            "using_prebaked": None,
            "embedding_test": None,
            "error": None,
        }

        try:
            # Force initialization if needed
            if not manager.is_ready:
                logger.info("Prewarm: Initializing model...")
                init_start = time.time()
                init_result = await manager.initialize()
                result["initialization_time_ms"] = round((time.time() - init_start) * 1000, 2)

                if not init_result:
                    result["error"] = "Model initialization failed"
                    return result
            else:
                result["initialization_time_ms"] = 0  # Already initialized

            # Run additional warmup test
            warmup_start = time.time()

            # Generate test audio
            test_audio = np.random.randn(16000).astype(np.float32) * 0.1

            # Extract embedding as warmup test
            embedding = await manager.extract_embedding(test_audio, use_cache=False)

            result["warmup_time_ms"] = round((time.time() - warmup_start) * 1000, 2)
            result["embedding_test"] = {
                "shape": list(embedding.shape) if embedding is not None else None,
                "dim": len(embedding) if embedding is not None else 0,
                "success": embedding is not None and len(embedding) == 192,
            }

            # Populate result
            result["success"] = True
            result["load_source"] = manager.load_source
            result["using_prebaked"] = manager._using_prebaked
            result["total_time_ms"] = round((time.time() - start_time) * 1000, 2)

            logger.info(f"Prewarm completed: {result['total_time_ms']:.0f}ms total")

        except Exception as e:
            result["error"] = str(e)
            result["total_time_ms"] = round((time.time() - start_time) * 1000, 2)
            logger.error(f"Prewarm failed: {e}")

        return result

    @app.post("/api/ml/prepopulate")
    async def prepopulate_cache(reference_embeddings: List[List[float]] = None):
        """
        Pre-populate the embedding cache with reference embeddings.

        v19.0.0 Enhancement:
        - Allows clients to seed the cache with known embeddings
        - Reduces latency for frequently-compared embeddings
        - Returns cache statistics

        Args:
            reference_embeddings: List of 192-dim embeddings to cache
        """
        manager = get_model_manager()

        if not manager.is_ready:
            if not await manager.initialize():
                raise HTTPException(status_code=503, detail="ECAPA model not available")

        result = {
            "success": True,
            "embeddings_cached": 0,
            "cache_stats_before": manager.embedding_cache.stats(),
            "cache_stats_after": None,
        }

        # If reference embeddings provided, add them to cache
        if reference_embeddings:
            for i, emb in enumerate(reference_embeddings):
                if len(emb) == 192:
                    # Generate a synthetic key for reference embeddings
                    emb_array = np.array(emb, dtype=np.float32)
                    cache_key = f"reference_{hashlib.sha256(emb_array.tobytes()).hexdigest()[:16]}"
                    await manager.embedding_cache.set(cache_key, emb_array)
                    result["embeddings_cached"] += 1

        result["cache_stats_after"] = manager.embedding_cache.stats()

        return result

    # =========================================================================
    # Embedding Endpoints
    # =========================================================================

    @app.post("/api/ml/speaker_embedding", response_model=EmbeddingResponse)
    async def extract_embedding(request: EmbeddingRequest):
        """Extract speaker embedding from audio."""
        start_time = time.time()

        manager = get_model_manager()

        try:
            # Ensure model is ready
            if not manager.is_ready:
                if not await manager.initialize():
                    raise HTTPException(
                        status_code=503,
                        detail="ECAPA model not available"
                    )

            # Decode audio
            try:
                audio_bytes = base64.b64decode(request.audio_data)
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid base64 audio data: {e}"
                )

            # Convert to numpy array
            if request.format == "int16":
                audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                audio = np.frombuffer(audio_bytes, dtype=np.float32)

            # Resample if needed (simple linear interpolation)
            if request.sample_rate != 16000:
                duration = len(audio) / request.sample_rate
                new_length = int(duration * 16000)
                audio = np.interp(
                    np.linspace(0, len(audio) - 1, new_length),
                    np.arange(len(audio)),
                    audio
                )

            # Extract embedding
            embedding = await manager.extract_embedding(audio)

            if embedding is None:
                raise HTTPException(
                    status_code=500,
                    detail="Embedding extraction returned None"
                )

            processing_time = (time.time() - start_time) * 1000

            return EmbeddingResponse(
                success=True,
                embedding=embedding.tolist(),
                embedding_size=len(embedding),
                processing_time_ms=round(processing_time, 2),
                cached=False,
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Embedding extraction error: {e}")
            logger.error(traceback.format_exc())
            return EmbeddingResponse(
                success=False,
                error=str(e),
                processing_time_ms=round((time.time() - start_time) * 1000, 2),
            )

    @app.post("/api/ml/speaker_verify", response_model=VerifyResponse)
    async def verify_speaker(request: VerifyRequest):
        """Verify speaker against reference embedding."""
        start_time = time.time()

        manager = get_model_manager()

        try:
            # Ensure model is ready
            if not manager.is_ready:
                if not await manager.initialize():
                    raise HTTPException(
                        status_code=503,
                        detail="ECAPA model not available"
                    )

            # Decode audio
            audio_bytes = base64.b64decode(request.audio_data)

            if request.format == "int16":
                audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                audio = np.frombuffer(audio_bytes, dtype=np.float32)

            # Resample if needed
            if request.sample_rate != 16000:
                duration = len(audio) / request.sample_rate
                new_length = int(duration * 16000)
                audio = np.interp(
                    np.linspace(0, len(audio) - 1, new_length),
                    np.arange(len(audio)),
                    audio
                )

            # Extract embedding
            embedding = await manager.extract_embedding(audio)

            if embedding is None:
                raise HTTPException(
                    status_code=500,
                    detail="Embedding extraction failed"
                )

            # Compare with reference
            reference = np.array(request.reference_embedding, dtype=np.float32)
            similarity = await manager.compute_similarity(embedding, reference)

            # Convert similarity to confidence (0-1 range)
            # Similarity is cosine similarity (-1 to 1), normalize to 0-1
            confidence = (similarity + 1) / 2

            threshold = 0.85
            verified = confidence >= threshold

            processing_time = (time.time() - start_time) * 1000

            return VerifyResponse(
                success=True,
                verified=verified,
                similarity=round(similarity, 4),
                confidence=round(confidence, 4),
                threshold=threshold,
                processing_time_ms=round(processing_time, 2),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Speaker verification error: {e}")
            return VerifyResponse(
                success=False,
                error=str(e),
                processing_time_ms=round((time.time() - start_time) * 1000, 2),
            )

    @app.post("/api/ml/batch_embedding", response_model=BatchEmbeddingResponse)
    async def extract_batch_embeddings(request: BatchEmbeddingRequest):
        """Extract embeddings for multiple audio samples."""
        start_time = time.time()

        manager = get_model_manager()

        try:
            if not manager.is_ready:
                if not await manager.initialize():
                    raise HTTPException(
                        status_code=503,
                        detail="ECAPA model not available"
                    )

            embeddings = []

            for audio_b64 in request.audio_samples:
                try:
                    audio_bytes = base64.b64decode(audio_b64)

                    if request.format == "int16":
                        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    else:
                        audio = np.frombuffer(audio_bytes, dtype=np.float32)

                    if request.sample_rate != 16000:
                        duration = len(audio) / request.sample_rate
                        new_length = int(duration * 16000)
                        audio = np.interp(
                            np.linspace(0, len(audio) - 1, new_length),
                            np.arange(len(audio)),
                            audio
                        )

                    embedding = await manager.extract_embedding(audio)
                    embeddings.append(embedding.tolist() if embedding is not None else None)

                except Exception as e:
                    logger.warning(f"Batch item failed: {e}")
                    embeddings.append(None)

            processing_time = (time.time() - start_time) * 1000

            return BatchEmbeddingResponse(
                success=True,
                embeddings=embeddings,
                processing_time_ms=round(processing_time, 2),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Batch extraction error: {e}")
            return BatchEmbeddingResponse(
                success=False,
                error=str(e),
                processing_time_ms=round((time.time() - start_time) * 1000, 2),
            )


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main():
    """Run the ECAPA cloud service."""
    if not FASTAPI_AVAILABLE:
        logger.error("FastAPI not available. Install with: pip install fastapi uvicorn")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("ECAPA Cloud Service v20.0.0 - Ultra-Fast Cold Starts")
    logger.info("  Optimization: JIT | ONNX | Quantization")
    logger.info("=" * 60)
    logger.info(f"Configuration: {CloudECAPAConfig.to_dict()}")

    uvicorn.run(
        "ecapa_cloud_service:app",
        host="0.0.0.0",
        port=CloudECAPAConfig.PORT,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
