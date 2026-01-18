#!/usr/bin/env python3
"""
Centralized Optional Dependency Manager for JARVIS v16.0
=========================================================

Provides robust, cached checking of optional dependencies (PyTorch, CoreML, etc.)
with graceful degradation and intelligent fallback mechanisms.

Features:
- Single point of truth for all optional dependency checks
- Lazy loading with caching to avoid repeated import attempts
- Consolidated logging (one message per missing dependency)
- Thread-safe singleton pattern
- Environment-driven configuration
- Automatic capability detection (GPU, MPS, etc.)
- Graceful degradation with fallback suggestions

Architecture:
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Optional Dependency Manager v16.0                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌────────────────────┐  ┌────────────────────┐  ┌────────────────────┐     │
│  │  PyTorch Checker   │  │  CoreML Checker    │  │  ML Libs Checker   │     │
│  │  • torch           │  │  • coremltools     │  │  • transformers    │     │
│  │  • torchaudio      │  │  • mlx             │  │  • sentence-trans  │     │
│  │  • torchvision     │  │  • ane             │  │  • speechbrain     │     │
│  └─────────┬──────────┘  └─────────┬──────────┘  └─────────┬──────────┘     │
│            └───────────────────────┼───────────────────────┘                │
│                                    ▼                                        │
│              ┌─────────────────────────────────────────┐                    │
│              │      Dependency Cache (Singleton)       │                    │
│              │  • is_available(name) -> bool           │                    │
│              │  • get_module(name) -> Optional[module] │                    │
│              │  • get_capabilities() -> Dict           │                    │
│              └─────────────────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────────────────────┘

Usage:
    from backend.core.optional_dependencies import (
        is_torch_available,
        is_coreml_available,
        get_optional_module,
        get_ml_capabilities,
    )

    # Check availability
    if is_torch_available():
        import torch
        model = torch.nn.Linear(10, 5)

    # Or get module safely
    torch = get_optional_module("torch")
    if torch:
        model = torch.nn.Linear(10, 5)

Author: JARVIS System
Version: 16.0.0
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type

logger = logging.getLogger(__name__)


class DependencyCategory(Enum):
    """Categories of optional dependencies."""

    ML_CORE = auto()      # PyTorch, TensorFlow
    ML_AUDIO = auto()     # torchaudio, speechbrain, whisper
    ML_VISION = auto()    # torchvision, opencv
    ML_NLP = auto()       # transformers, sentence-transformers, spacy
    APPLE_ML = auto()     # coremltools, mlx, ANE
    ACCELERATION = auto()  # CUDA, MPS, Metal
    DATABASE = auto()     # asyncpg, redis
    WEB = auto()          # aiohttp, httpx
    MISC = auto()         # Other optional deps


@dataclass
class DependencyInfo:
    """Information about an optional dependency."""

    name: str
    import_name: str  # Actual module name for import
    category: DependencyCategory
    description: str
    fallback_message: str = ""
    min_version: Optional[str] = None
    required_by: List[str] = field(default_factory=list)

    # Cached state
    _available: Optional[bool] = field(default=None, repr=False)
    _module: Optional[Any] = field(default=None, repr=False)
    _error: Optional[str] = field(default=None, repr=False)
    _checked: bool = field(default=False, repr=False)


class OptionalDependencyManager:
    """
    Centralized manager for optional dependencies.

    Thread-safe singleton that checks dependencies once and caches results.
    Provides consolidated logging to avoid warning spam.
    """

    _instance: Optional["OptionalDependencyManager"] = None
    _lock = threading.Lock()

    # Registry of known optional dependencies
    KNOWN_DEPENDENCIES: Dict[str, DependencyInfo] = {
        # ML Core
        "torch": DependencyInfo(
            name="PyTorch",
            import_name="torch",
            category=DependencyCategory.ML_CORE,
            description="Deep learning framework",
            fallback_message="ML features will use CPU-based alternatives or be disabled",
            required_by=["voice_unlock", "speaker_verification", "vad"],
        ),
        "tensorflow": DependencyInfo(
            name="TensorFlow",
            import_name="tensorflow",
            category=DependencyCategory.ML_CORE,
            description="Machine learning framework",
            fallback_message="TensorFlow models will not be available",
        ),

        # ML Audio
        "torchaudio": DependencyInfo(
            name="TorchAudio",
            import_name="torchaudio",
            category=DependencyCategory.ML_AUDIO,
            description="Audio processing with PyTorch",
            fallback_message="Audio ML features may be limited",
            required_by=["speaker_verification", "voice_analysis"],
        ),
        "speechbrain": DependencyInfo(
            name="SpeechBrain",
            import_name="speechbrain",
            category=DependencyCategory.ML_AUDIO,
            description="Speech processing toolkit",
            fallback_message="Speaker verification will use alternative methods",
            required_by=["speaker_verification"],
        ),
        "whisper": DependencyInfo(
            name="OpenAI Whisper",
            import_name="whisper",
            category=DependencyCategory.ML_AUDIO,
            description="Speech recognition model",
            fallback_message="Local speech recognition will use cloud APIs",
            required_by=["speech_to_text"],
        ),

        # ML Vision
        "torchvision": DependencyInfo(
            name="TorchVision",
            import_name="torchvision",
            category=DependencyCategory.ML_VISION,
            description="Computer vision with PyTorch",
            fallback_message="Vision ML features may be limited",
        ),
        "cv2": DependencyInfo(
            name="OpenCV",
            import_name="cv2",
            category=DependencyCategory.ML_VISION,
            description="Computer vision library",
            fallback_message="Image processing features may be limited",
            required_by=["screen_capture", "vision_analysis"],
        ),

        # ML NLP
        "transformers": DependencyInfo(
            name="HuggingFace Transformers",
            import_name="transformers",
            category=DependencyCategory.ML_NLP,
            description="State-of-the-art NLP models",
            fallback_message="Local NLP will use simpler models or cloud APIs",
            required_by=["embeddings", "text_classification"],
        ),
        "sentence_transformers": DependencyInfo(
            name="Sentence Transformers",
            import_name="sentence_transformers",
            category=DependencyCategory.ML_NLP,
            description="Sentence embeddings",
            fallback_message="Text embeddings will use alternative methods",
            required_by=["semantic_search", "embeddings"],
        ),
        "spacy": DependencyInfo(
            name="spaCy",
            import_name="spacy",
            category=DependencyCategory.ML_NLP,
            description="Industrial NLP library",
            fallback_message="NLP features will use simpler text processing",
        ),

        # Apple ML
        "coremltools": DependencyInfo(
            name="CoreML Tools",
            import_name="coremltools",
            category=DependencyCategory.APPLE_ML,
            description="Apple CoreML model conversion",
            fallback_message="CoreML model export will not be available",
        ),
        "mlx": DependencyInfo(
            name="Apple MLX",
            import_name="mlx",
            category=DependencyCategory.APPLE_ML,
            description="Apple Silicon ML framework",
            fallback_message="MLX acceleration will not be available",
        ),

        # Database
        "asyncpg": DependencyInfo(
            name="asyncpg",
            import_name="asyncpg",
            category=DependencyCategory.DATABASE,
            description="PostgreSQL async driver",
            fallback_message="PostgreSQL features will not be available",
            required_by=["cloud_sql", "learning_db"],
        ),
        "redis": DependencyInfo(
            name="Redis",
            import_name="redis",
            category=DependencyCategory.DATABASE,
            description="Redis client",
            fallback_message="Redis caching will use local alternatives",
        ),

        # Web
        "aiohttp": DependencyInfo(
            name="aiohttp",
            import_name="aiohttp",
            category=DependencyCategory.WEB,
            description="Async HTTP client/server",
            fallback_message="Async HTTP features may be limited",
        ),
        "httpx": DependencyInfo(
            name="httpx",
            import_name="httpx",
            category=DependencyCategory.WEB,
            description="Modern HTTP client",
            fallback_message="HTTP requests will use requests library",
        ),
    }

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._check_lock = threading.Lock()
        self._capabilities_cache: Optional[Dict[str, Any]] = None
        self._startup_logged = False

        # Environment overrides
        self._force_disabled: Set[str] = set(
            os.getenv("JARVIS_DISABLE_OPTIONAL_DEPS", "").split(",")
        )

        logger.debug("OptionalDependencyManager initialized")

    def check_dependency(self, name: str, force_recheck: bool = False) -> bool:
        """
        Check if a dependency is available.

        Args:
            name: Dependency name (e.g., "torch", "coremltools")
            force_recheck: Force re-checking even if cached

        Returns:
            True if dependency is available
        """
        # Normalize name
        name = name.lower().replace("-", "_")

        # Check if forced disabled
        if name in self._force_disabled:
            return False

        # Get or create dependency info
        if name not in self.KNOWN_DEPENDENCIES:
            # Unknown dependency - try to import directly
            return self._check_unknown_dependency(name, force_recheck)

        dep_info = self.KNOWN_DEPENDENCIES[name]

        # Return cached result if available
        if dep_info._checked and not force_recheck:
            return dep_info._available or False

        # Thread-safe check
        with self._check_lock:
            # Double-check after acquiring lock
            if dep_info._checked and not force_recheck:
                return dep_info._available or False

            try:
                module = importlib.import_module(dep_info.import_name)
                dep_info._available = True
                dep_info._module = module
                dep_info._error = None

            except ImportError as e:
                dep_info._available = False
                dep_info._module = None
                dep_info._error = str(e)

            except Exception as e:
                dep_info._available = False
                dep_info._module = None
                dep_info._error = f"Unexpected error: {e}"

            dep_info._checked = True

        return dep_info._available or False

    def _check_unknown_dependency(self, name: str, force_recheck: bool = False) -> bool:
        """Check an unknown dependency by trying to import it."""
        # Create a temporary info object
        if name not in self.KNOWN_DEPENDENCIES:
            self.KNOWN_DEPENDENCIES[name] = DependencyInfo(
                name=name,
                import_name=name,
                category=DependencyCategory.MISC,
                description=f"Unknown optional dependency: {name}",
            )

        return self.check_dependency(name, force_recheck)

    def get_module(self, name: str) -> Optional[Any]:
        """
        Get the module if available.

        Args:
            name: Dependency name

        Returns:
            The imported module, or None if not available
        """
        name = name.lower().replace("-", "_")

        if not self.check_dependency(name):
            return None

        if name in self.KNOWN_DEPENDENCIES:
            return self.KNOWN_DEPENDENCIES[name]._module

        return None

    def get_error(self, name: str) -> Optional[str]:
        """Get the error message for a failed dependency check."""
        name = name.lower().replace("-", "_")

        if name in self.KNOWN_DEPENDENCIES:
            return self.KNOWN_DEPENDENCIES[name]._error

        return None

    def get_capabilities(self) -> Dict[str, Any]:
        """
        Get ML capabilities based on available dependencies.

        Returns comprehensive information about what's available.
        """
        if self._capabilities_cache is not None:
            return self._capabilities_cache

        caps: Dict[str, Any] = {
            "ml_available": False,
            "gpu_available": False,
            "mps_available": False,
            "cuda_available": False,
            "apple_silicon": False,
            "categories": {},
            "missing_optional": [],
            "available_optional": [],
        }

        # Check PyTorch and GPU availability
        if self.check_dependency("torch"):
            torch = self.get_module("torch")
            caps["ml_available"] = True
            caps["cuda_available"] = torch.cuda.is_available() if hasattr(torch, "cuda") else False
            caps["mps_available"] = (
                hasattr(torch.backends, "mps") and
                torch.backends.mps.is_available()
            ) if hasattr(torch, "backends") else False
            caps["gpu_available"] = caps["cuda_available"] or caps["mps_available"]

        # Check Apple Silicon
        import platform
        caps["apple_silicon"] = (
            platform.machine() == "arm64" and
            platform.system() == "Darwin"
        )

        # Check each category
        for category in DependencyCategory:
            category_deps = [
                name for name, info in self.KNOWN_DEPENDENCIES.items()
                if info.category == category
            ]

            available = [
                name for name in category_deps
                if self.check_dependency(name)
            ]

            caps["categories"][category.name] = {
                "available": available,
                "missing": [n for n in category_deps if n not in available],
            }

            caps["available_optional"].extend(available)
            caps["missing_optional"].extend([n for n in category_deps if n not in available])

        self._capabilities_cache = caps
        return caps

    def log_startup_status(self) -> None:
        """
        Log a consolidated status of all optional dependencies.

        This should be called ONCE at startup to avoid log spam.
        """
        if self._startup_logged:
            return

        self._startup_logged = True
        caps = self.get_capabilities()

        # Build consolidated message
        available_count = len(caps["available_optional"])
        missing_count = len(caps["missing_optional"])

        if missing_count == 0:
            logger.info(
                "✅ All %d optional dependencies available",
                available_count
            )
            return

        # Log summary
        logger.info(
            "📦 Optional Dependencies: %d available, %d not installed",
            available_count,
            missing_count
        )

        # Log by category (only categories with missing deps)
        for category_name, category_info in caps["categories"].items():
            missing = category_info["missing"]
            if missing:
                # Get fallback messages
                fallbacks = []
                for name in missing[:3]:  # Limit to first 3
                    if name in self.KNOWN_DEPENDENCIES:
                        fb = self.KNOWN_DEPENDENCIES[name].fallback_message
                        if fb:
                            fallbacks.append(fb)

                logger.info(
                    "   • %s: %s not available%s",
                    category_name,
                    ", ".join(missing[:3]) + ("..." if len(missing) > 3 else ""),
                    f" ({fallbacks[0]})" if fallbacks else ""
                )

        # GPU status
        if caps["ml_available"]:
            if caps["gpu_available"]:
                gpu_type = "MPS" if caps["mps_available"] else "CUDA"
                logger.info("   • GPU Acceleration: %s available", gpu_type)
            else:
                logger.info("   • GPU Acceleration: Not available (using CPU)")

        logger.debug(
            "Full capability report: %s",
            {k: v for k, v in caps.items() if k != "categories"}
        )

    def get_status_summary(self) -> Dict[str, Any]:
        """Get a summary suitable for health checks and APIs."""
        caps = self.get_capabilities()

        return {
            "ml_ready": caps["ml_available"],
            "gpu_ready": caps["gpu_available"],
            "gpu_type": (
                "mps" if caps["mps_available"] else
                "cuda" if caps["cuda_available"] else
                "cpu"
            ),
            "available_count": len(caps["available_optional"]),
            "missing_count": len(caps["missing_optional"]),
            "categories": {
                cat_name: {
                    "available": len(cat_info["available"]),
                    "missing": len(cat_info["missing"]),
                }
                for cat_name, cat_info in caps["categories"].items()
            },
        }


# =============================================================================
# Global singleton instance and convenience functions
# =============================================================================

_manager: Optional[OptionalDependencyManager] = None


def get_dependency_manager() -> OptionalDependencyManager:
    """Get the global dependency manager singleton."""
    global _manager
    if _manager is None:
        _manager = OptionalDependencyManager()
    return _manager


def is_available(name: str) -> bool:
    """Check if an optional dependency is available."""
    return get_dependency_manager().check_dependency(name)


def get_optional_module(name: str) -> Optional[Any]:
    """Get an optional module if available, None otherwise."""
    return get_dependency_manager().get_module(name)


# =============================================================================
# Specific dependency checkers (convenience functions)
# =============================================================================

def is_torch_available() -> bool:
    """Check if PyTorch is available."""
    return is_available("torch")


def is_torchaudio_available() -> bool:
    """Check if TorchAudio is available."""
    return is_available("torchaudio")


def is_coreml_available() -> bool:
    """Check if CoreML tools are available."""
    return is_available("coremltools")


def is_mlx_available() -> bool:
    """Check if Apple MLX is available."""
    return is_available("mlx")


def is_transformers_available() -> bool:
    """Check if HuggingFace Transformers is available."""
    return is_available("transformers")


def is_speechbrain_available() -> bool:
    """Check if SpeechBrain is available."""
    return is_available("speechbrain")


def is_whisper_available() -> bool:
    """Check if OpenAI Whisper is available."""
    return is_available("whisper")


def is_spacy_available() -> bool:
    """Check if spaCy is available."""
    return is_available("spacy")


def is_asyncpg_available() -> bool:
    """Check if asyncpg is available."""
    return is_available("asyncpg")


def is_gpu_available() -> bool:
    """Check if any GPU acceleration is available."""
    caps = get_dependency_manager().get_capabilities()
    return caps.get("gpu_available", False)


def is_mps_available() -> bool:
    """Check if Apple MPS (Metal Performance Shaders) is available."""
    caps = get_dependency_manager().get_capabilities()
    return caps.get("mps_available", False)


def is_cuda_available() -> bool:
    """Check if NVIDIA CUDA is available."""
    caps = get_dependency_manager().get_capabilities()
    return caps.get("cuda_available", False)


def get_ml_capabilities() -> Dict[str, Any]:
    """Get comprehensive ML capabilities information."""
    return get_dependency_manager().get_capabilities()


def log_dependency_status() -> None:
    """Log consolidated dependency status (call once at startup)."""
    get_dependency_manager().log_startup_status()


# =============================================================================
# Context managers for optional features
# =============================================================================

class optional_dependency:
    """
    Context manager for optional dependency blocks.

    Usage:
        with optional_dependency("torch") as torch:
            if torch:
                # Use PyTorch
                model = torch.nn.Linear(10, 5)
            else:
                # Fallback
                pass
    """

    def __init__(self, name: str, log_fallback: bool = False):
        self.name = name
        self.log_fallback = log_fallback
        self.module = None

    def __enter__(self) -> Optional[Any]:
        self.module = get_optional_module(self.name)
        if self.module is None and self.log_fallback:
            info = get_dependency_manager().KNOWN_DEPENDENCIES.get(self.name)
            if info and info.fallback_message:
                logger.debug(
                    "%s not available: %s",
                    info.name,
                    info.fallback_message
                )
        return self.module

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def require_dependency(name: str) -> Callable:
    """
    Decorator that skips a function if a dependency is not available.

    Usage:
        @require_dependency("torch")
        def train_model():
            import torch
            # ...
    """
    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            if not is_available(name):
                logger.debug(
                    "Skipping %s: %s not available",
                    func.__name__,
                    name
                )
                return None
            return func(*args, **kwargs)
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper
    return decorator


# =============================================================================
# Intelligent Fallback System
# =============================================================================

@dataclass
class FallbackOption:
    """Represents a fallback option for an unavailable dependency."""

    name: str
    dependency: str  # What this can replace
    priority: int  # Lower = higher priority
    capability_level: str  # "full", "partial", "minimal"
    description: str
    check_fn: Optional[Callable[[], bool]] = None


class IntelligentFallbackManager:
    """
    Manages intelligent fallbacks for unavailable dependencies.

    When a dependency is unavailable, this manager suggests and provides
    the best available alternative based on what's installed.
    """

    # Define fallback chains: dependency -> list of fallback options
    FALLBACK_CHAINS: Dict[str, List[FallbackOption]] = {
        # ML Core Fallbacks
        "torch": [
            FallbackOption(
                name="tensorflow",
                dependency="torch",
                priority=1,
                capability_level="partial",
                description="TensorFlow can handle many PyTorch use cases",
            ),
            FallbackOption(
                name="numpy",
                dependency="torch",
                priority=2,
                capability_level="minimal",
                description="NumPy for basic numerical operations only",
            ),
        ],

        # Audio Processing Fallbacks
        "torchaudio": [
            FallbackOption(
                name="librosa",
                dependency="torchaudio",
                priority=1,
                capability_level="partial",
                description="Librosa for audio processing without PyTorch",
            ),
            FallbackOption(
                name="scipy",
                dependency="torchaudio",
                priority=2,
                capability_level="minimal",
                description="SciPy for basic audio signal processing",
            ),
        ],

        "speechbrain": [
            FallbackOption(
                name="cloud_api",
                dependency="speechbrain",
                priority=1,
                capability_level="full",
                description="Cloud API for speaker verification",
            ),
        ],

        "whisper": [
            FallbackOption(
                name="cloud_api",
                dependency="whisper",
                priority=1,
                capability_level="full",
                description="Cloud STT API (Google/OpenAI)",
            ),
        ],

        # NLP Fallbacks
        "transformers": [
            FallbackOption(
                name="sentence_transformers",
                dependency="transformers",
                priority=1,
                capability_level="partial",
                description="Sentence Transformers for embeddings",
            ),
            FallbackOption(
                name="cloud_api",
                dependency="transformers",
                priority=2,
                capability_level="full",
                description="Cloud API for NLP tasks",
            ),
        ],

        "spacy": [
            FallbackOption(
                name="nltk",
                dependency="spacy",
                priority=1,
                capability_level="partial",
                description="NLTK for basic NLP tasks",
            ),
        ],

        # Apple ML Fallbacks
        "coremltools": [
            FallbackOption(
                name="torch",
                dependency="coremltools",
                priority=1,
                capability_level="full",
                description="PyTorch for inference (no CoreML export)",
            ),
        ],

        "mlx": [
            FallbackOption(
                name="torch",
                dependency="mlx",
                priority=1,
                capability_level="full",
                description="PyTorch for Apple Silicon inference",
            ),
        ],
    }

    @classmethod
    def get_best_fallback(cls, dependency: str) -> Optional[FallbackOption]:
        """
        Get the best available fallback for a dependency.

        Args:
            dependency: Name of the unavailable dependency

        Returns:
            Best available FallbackOption, or None if no fallback available
        """
        if dependency not in cls.FALLBACK_CHAINS:
            return None

        manager = get_dependency_manager()

        for option in cls.FALLBACK_CHAINS[dependency]:
            # Check if the fallback itself is available
            if option.name == "cloud_api":
                # Cloud API fallback is always available
                return option

            if manager.check_dependency(option.name):
                return option

        return None

    @classmethod
    def get_all_fallbacks(cls, dependency: str) -> List[Tuple[FallbackOption, bool]]:
        """
        Get all fallback options for a dependency with availability status.

        Args:
            dependency: Name of the dependency

        Returns:
            List of (FallbackOption, is_available) tuples
        """
        if dependency not in cls.FALLBACK_CHAINS:
            return []

        manager = get_dependency_manager()
        result = []

        for option in cls.FALLBACK_CHAINS[dependency]:
            if option.name == "cloud_api":
                result.append((option, True))
            else:
                result.append((option, manager.check_dependency(option.name)))

        return result

    @classmethod
    def suggest_installation(cls, dependency: str) -> str:
        """
        Generate an installation suggestion for a missing dependency.

        Args:
            dependency: Name of the missing dependency

        Returns:
            Installation command or suggestion string
        """
        install_commands = {
            "torch": "pip install torch torchvision torchaudio",
            "tensorflow": "pip install tensorflow",
            "torchaudio": "pip install torchaudio",
            "speechbrain": "pip install speechbrain",
            "whisper": "pip install openai-whisper",
            "transformers": "pip install transformers",
            "sentence_transformers": "pip install sentence-transformers",
            "spacy": "pip install spacy && python -m spacy download en_core_web_sm",
            "coremltools": "pip install coremltools",
            "mlx": "pip install mlx",
            "librosa": "pip install librosa",
            "scipy": "pip install scipy",
            "cv2": "pip install opencv-python",
            "asyncpg": "pip install asyncpg",
            "redis": "pip install redis",
        }

        return install_commands.get(dependency, f"pip install {dependency}")


def get_fallback_for(dependency: str) -> Optional[FallbackOption]:
    """Get the best available fallback for a dependency."""
    return IntelligentFallbackManager.get_best_fallback(dependency)


def suggest_install(dependency: str) -> str:
    """Get installation suggestion for a missing dependency."""
    return IntelligentFallbackManager.suggest_installation(dependency)


def get_fallback_chain(dependency: str) -> List[Tuple[FallbackOption, bool]]:
    """Get all fallback options with availability status."""
    return IntelligentFallbackManager.get_all_fallbacks(dependency)


# =============================================================================
# Cross-Repo Dependency Coordination
# =============================================================================

class CrossRepoDependencyCoordinator:
    """
    Coordinates dependency availability across JARVIS, J-Prime, and Reactor-Core.

    Ensures that ML capabilities are properly detected and communicated across
    the Trinity ecosystem for optimal resource allocation.
    """

    _instance: Optional["CrossRepoDependencyCoordinator"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._capability_cache: Dict[str, Dict[str, Any]] = {}
        self._last_sync = 0.0
        self._sync_interval = float(os.getenv("JARVIS_DEP_SYNC_INTERVAL", "60.0"))

    def get_local_capabilities(self) -> Dict[str, Any]:
        """Get ML capabilities for this repo."""
        manager = get_dependency_manager()
        return manager.get_capabilities()

    def get_capability_summary(self) -> Dict[str, Any]:
        """
        Get a summary suitable for cross-repo communication.

        Returns a compact summary that can be sent via events or IPC.
        """
        caps = self.get_local_capabilities()

        return {
            "ml_ready": caps.get("ml_available", False),
            "gpu_type": (
                "mps" if caps.get("mps_available") else
                "cuda" if caps.get("cuda_available") else
                "cpu"
            ),
            "torch_available": is_torch_available(),
            "transformers_available": is_transformers_available(),
            "apple_silicon": caps.get("apple_silicon", False),
            "categories_available": {
                cat: len(info.get("available", []))
                for cat, info in caps.get("categories", {}).items()
            },
            "timestamp": time.time(),
        }

    def recommend_compute_location(self, task_type: str) -> str:
        """
        Recommend where to run a task based on available capabilities.

        Args:
            task_type: Type of task ("inference", "training", "embedding", etc.)

        Returns:
            Recommendation: "local", "jprime", "reactor", or "cloud"
        """
        caps = self.get_local_capabilities()

        # Heavy training should go to Reactor-Core
        if task_type == "training":
            return "reactor"

        # Inference with GPU should be local
        if task_type == "inference" and caps.get("gpu_available"):
            return "local"

        # J-Prime for complex reasoning
        if task_type in ("reasoning", "planning"):
            return "jprime"

        # Embeddings prefer local if transformers available
        if task_type == "embedding" and is_transformers_available():
            return "local"

        # Default to cloud for safety
        return "cloud"


def get_dependency_coordinator() -> CrossRepoDependencyCoordinator:
    """Get the global dependency coordinator singleton."""
    return CrossRepoDependencyCoordinator()


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Core classes
    "DependencyCategory",
    "DependencyInfo",
    "OptionalDependencyManager",
    # Manager functions
    "get_dependency_manager",
    "is_available",
    "get_optional_module",
    # Specific dependency checkers
    "is_torch_available",
    "is_torchaudio_available",
    "is_coreml_available",
    "is_mlx_available",
    "is_transformers_available",
    "is_speechbrain_available",
    "is_whisper_available",
    "is_spacy_available",
    "is_asyncpg_available",
    "is_gpu_available",
    "is_mps_available",
    "is_cuda_available",
    "get_ml_capabilities",
    "log_dependency_status",
    # Context managers and decorators
    "optional_dependency",
    "require_dependency",
    # Fallback system
    "FallbackOption",
    "IntelligentFallbackManager",
    "get_fallback_for",
    "suggest_install",
    "get_fallback_chain",
    # Cross-repo coordination
    "CrossRepoDependencyCoordinator",
    "get_dependency_coordinator",
]
