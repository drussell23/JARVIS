"""
Unified Voice Cache Manager
===========================

CRITICAL INTEGRATION: Connects all voice biometric components for real-time
intelligent caching and instant voice recognition.

Architecture:
                                    ┌─────────────────────────────────┐
                                    │    UnifiedVoiceCacheManager     │
                                    │         (Orchestrator)          │
                                    └─────────────┬───────────────────┘
                                                  │
          ┌───────────────────┬───────────────────┼───────────────────┬───────────────────┐
          │                   │                   │                   │                   │
          ▼                   ▼                   ▼                   ▼                   ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│ SQLite/CloudSQL │ │    ChromaDB     │ │ VoiceBiometric  │ │ ParallelModel   │ │  Continuous     │
│  (Voiceprints)  │ │   (Semantic)    │ │     Cache       │ │    Loader       │ │   Learning      │
│                 │ │                 │ │                 │ │                 │ │                 │
│ - Derek's embed │ │ - Pattern match │ │ - Session cache │ │ - Whisper       │ │ - Record auth   │
│ - Unlock hist   │ │ - Similarity    │ │ - Voice embed   │ │ - ECAPA-TDNN    │ │ - Update embed  │
│ - Confidence    │ │ - Anti-spoofing │ │ - Command cache │ │ - Shared pool   │ │ - Adapt thresh  │
└─────────────────┘ └─────────────────┘ └─────────────────┘ └─────────────────┘ └─────────────────┘

Key Features:
1. PRELOAD voice profiles from SQLite at startup (instant recognition)
2. SHARE models between parallel loader and biometric cache
3. SYNC real-time authentication results back to database
4. LEARN from every unlock attempt to improve recognition
5. OPTIMIZE with ChromaDB for semantic voice pattern matching
6. ANTI-SPOOFING via behavioral pattern analysis
7. VOICE EVOLUTION tracking for adaptive recognition
8. COST OPTIMIZATION with Helicone-style semantic caching

Performance Goals:
- First unlock after startup: < 500ms (preloaded embedding match)
- Subsequent unlocks in session: < 100ms (cache hit)
- Cold start with model loading: < 5s (parallel loading)
- Semantic cache hit: < 10ms (ChromaDB similarity search)
"""

import asyncio
import base64
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)

# =============================================================================
# CHROMADB INTEGRATION FOR SEMANTIC VOICE PATTERN CACHING
# =============================================================================
try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    logger.debug("ChromaDB not available - semantic pattern caching disabled")

# Cost tracking integration
try:
    from core.cost_tracker import get_cost_tracker, CostTracker
    COST_TRACKER_AVAILABLE = True
except ImportError:
    COST_TRACKER_AVAILABLE = False
    logger.debug("CostTracker not available")


# =============================================================================
# AUDIO DATA NORMALIZATION HELPERS
# =============================================================================
def normalize_audio_data(audio_data) -> Optional[bytes]:
    """
    Normalize audio data to bytes format for consistent processing.

    Handles:
    - bytes: Pass through
    - str: Decode as base64, or encode as UTF-8 fallback
    - numpy array: Convert to int16 PCM bytes
    - torch tensor: Convert to numpy then to bytes

    Args:
        audio_data: Audio data in any supported format

    Returns:
        Audio data as bytes, or None if conversion fails
    """
    if audio_data is None:
        return None

    try:
        # Already bytes - pass through
        if isinstance(audio_data, bytes):
            return audio_data

        # String - likely base64 encoded
        if isinstance(audio_data, str):
            try:
                import base64
                return base64.b64decode(audio_data)
            except Exception:
                # Not base64, try as raw string (unlikely for audio)
                logger.warning("Audio data is string but not valid base64")
                return audio_data.encode('utf-8')

        # NumPy array - convert to PCM bytes
        if isinstance(audio_data, np.ndarray):
            # Normalize to -1.0 to 1.0 range if needed
            if audio_data.dtype == np.float32 or audio_data.dtype == np.float64:
                # Scale to int16 range
                audio_int16 = (audio_data * 32767).astype(np.int16)
            elif audio_data.dtype == np.int16:
                audio_int16 = audio_data
            else:
                # Convert to float first then to int16
                audio_float = audio_data.astype(np.float32)
                max_val = np.abs(audio_float).max()
                if max_val > 1.0:
                    audio_float = audio_float / max_val
                audio_int16 = (audio_float * 32767).astype(np.int16)
            return audio_int16.tobytes()

        # Torch tensor - convert via numpy
        if hasattr(audio_data, 'numpy'):  # Duck-type check for torch tensor
            try:
                np_array = audio_data.cpu().numpy() if hasattr(audio_data, 'cpu') else audio_data.numpy()
                return normalize_audio_data(np_array)  # Recursive call
            except Exception as e:
                logger.warning(f"Failed to convert tensor to bytes: {e}")
                return None

        # Unknown type - try to convert
        logger.warning(f"Unknown audio data type: {type(audio_data)}")
        return None

    except Exception as e:
        logger.error(f"Audio data normalization failed: {e}")
        return None


def audio_data_to_numpy(audio_data, sample_rate: int = 16000) -> Optional[np.ndarray]:
    """
    Convert audio data to numpy float32 array for ML models.

    Args:
        audio_data: Audio data in any supported format
        sample_rate: Expected sample rate (for validation)

    Returns:
        Audio as numpy float32 array normalized to [-1, 1]
    """
    try:
        # Handle bytes (PCM int16)
        if isinstance(audio_data, bytes):
            audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
            return audio_np

        # Handle base64 string
        if isinstance(audio_data, str):
            try:
                import base64
                audio_bytes = base64.b64decode(audio_data)
                return audio_data_to_numpy(audio_bytes, sample_rate)
            except Exception:
                return None

        # Handle numpy array
        if isinstance(audio_data, np.ndarray):
            if audio_data.dtype == np.int16:
                return audio_data.astype(np.float32) / 32768.0
            elif audio_data.dtype in [np.float32, np.float64]:
                return audio_data.astype(np.float32)
            else:
                return audio_data.astype(np.float32)

        # Handle torch tensor
        if hasattr(audio_data, 'numpy'):
            try:
                np_array = audio_data.cpu().numpy() if hasattr(audio_data, 'cpu') else audio_data.numpy()
                return audio_data_to_numpy(np_array, sample_rate)
            except Exception:
                return None

        return None

    except Exception as e:
        logger.error(f"Audio to numpy conversion failed: {e}")
        return None


# =============================================================================
# CONFIGURATION
# =============================================================================
class CacheConfig:
    """Unified cache configuration - Dynamic, no hardcoding"""
    # Embedding dimensions
    EMBEDDING_DIM = int(os.getenv("VOICE_EMBEDDING_DIM", "192"))

    # Similarity thresholds (configurable via env)
    INSTANT_MATCH_THRESHOLD = float(os.getenv("VBI_INSTANT_THRESHOLD", "0.92"))
    STANDARD_MATCH_THRESHOLD = float(os.getenv("VBI_CONFIDENT_THRESHOLD", "0.85"))
    LEARNING_THRESHOLD = float(os.getenv("VBI_LEARNING_THRESHOLD", "0.75"))
    SPOOFING_DETECTION_THRESHOLD = float(os.getenv("VBI_SPOOFING_THRESHOLD", "0.65"))

    # Session settings
    SESSION_TTL_SECONDS = int(os.getenv("VOICE_SESSION_TTL", "1800"))  # 30 minutes
    PRELOAD_TIMEOUT_SECONDS = float(os.getenv("VOICE_PRELOAD_TIMEOUT", "10.0"))

    # Cache sizes
    MAX_CACHED_EMBEDDINGS = int(os.getenv("MAX_CACHED_EMBEDDINGS", "50"))
    MAX_PATTERN_HISTORY = int(os.getenv("MAX_PATTERN_HISTORY", "100"))

    # Semantic cache settings (Helicone-style)
    SEMANTIC_CACHE_TTL_SECONDS = int(os.getenv("SEMANTIC_CACHE_TTL", "300"))  # 5 min
    SEMANTIC_SIMILARITY_THRESHOLD = float(os.getenv("SEMANTIC_SIMILARITY", "0.95"))

    # Anti-spoofing settings
    REPLAY_DETECTION_WINDOW_SECONDS = int(os.getenv("REPLAY_WINDOW", "60"))
    MAX_REPLAY_SIMILARITY = float(os.getenv("MAX_REPLAY_SIMILARITY", "0.99"))
    MIN_VOICE_VARIATION = float(os.getenv("MIN_VOICE_VARIATION", "0.02"))

    # Voice evolution tracking
    VOICE_DRIFT_THRESHOLD = float(os.getenv("VOICE_DRIFT_THRESHOLD", "0.03"))
    VOICE_DRIFT_WINDOW_DAYS = int(os.getenv("VOICE_DRIFT_WINDOW", "30"))

    # Cost optimization
    ENABLE_COST_TRACKING = os.getenv("ENABLE_COST_TRACKING", "true").lower() == "true"
    COST_PER_EMBEDDING_EXTRACTION = float(os.getenv("COST_PER_EMBEDDING", "0.0001"))

    # Database paths
    DEFAULT_DB_PATH = os.path.expanduser("~/.jarvis/voice_unlock_metrics.db")
    DEFAULT_CHROMA_PATH = os.path.expanduser("~/.jarvis/chroma_voice_patterns")

    # ChromaDB settings
    CHROMA_COLLECTION_NAME = os.getenv("CHROMA_VOICE_COLLECTION", "voice_patterns")
    CHROMA_PERSIST_ENABLED = os.getenv("CHROMA_PERSIST", "true").lower() == "true"


class CacheState(Enum):
    """State of the unified cache system"""
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    LOADING_PROFILES = "loading_profiles"
    LOADING_MODELS = "loading_models"
    READY = "ready"
    ERROR = "error"


@dataclass
class VoiceProfile:
    """Cached voice profile with embedding and metadata for intelligent recognition"""
    speaker_name: str
    embedding: np.ndarray
    embedding_dimensions: int = 192
    total_samples: int = 0
    avg_confidence: float = 0.0
    last_verified: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
    source: str = "database"

    # Voice evolution tracking
    baseline_embedding: Optional[np.ndarray] = None
    drift_percentage: float = 0.0
    last_drift_check: Optional[datetime] = None

    # Anti-spoofing metadata
    known_microphones: List[str] = field(default_factory=list)
    typical_snr_range: Tuple[float, float] = field(default_factory=lambda: (12.0, 25.0))
    typical_f0_range: Tuple[float, float] = field(default_factory=lambda: (85.0, 180.0))

    # Behavioral patterns
    typical_unlock_hours: List[int] = field(default_factory=list)
    avg_unlock_interval_hours: float = 0.0
    total_unlocks: int = 0

    # Environment signatures
    known_environments: Dict[str, np.ndarray] = field(default_factory=dict)

    def is_valid(self) -> bool:
        """Check if profile has valid embedding"""
        return (
            self.embedding is not None and
            len(self.embedding) >= 50  # Allow flexible dimensions
        )

    def update_behavioral_stats(self, unlock_time: datetime):
        """Update behavioral patterns from unlock event"""
        hour = unlock_time.hour
        if hour not in self.typical_unlock_hours:
            self.typical_unlock_hours.append(hour)
            # Keep only top 12 hours
            if len(self.typical_unlock_hours) > 12:
                self.typical_unlock_hours = self.typical_unlock_hours[-12:]

        self.total_unlocks += 1

        if self.last_verified:
            interval = (unlock_time - self.last_verified).total_seconds() / 3600
            # Exponential moving average for interval
            alpha = 0.1
            self.avg_unlock_interval_hours = (
                alpha * interval + (1 - alpha) * self.avg_unlock_interval_hours
            )

    def check_voice_drift(self, new_embedding: np.ndarray) -> float:
        """Check how much voice has drifted from baseline"""
        if self.baseline_embedding is None:
            self.baseline_embedding = self.embedding.copy()
            return 0.0

        # Compute drift as 1 - cosine_similarity
        norm_new = np.linalg.norm(new_embedding)
        norm_base = np.linalg.norm(self.baseline_embedding)
        if norm_new == 0 or norm_base == 0:
            return 0.0

        similarity = np.dot(new_embedding, self.baseline_embedding) / (norm_new * norm_base)
        drift = 1.0 - similarity
        self.drift_percentage = drift
        self.last_drift_check = datetime.now()
        return drift


@dataclass
class MatchResult:
    """Result of voice matching against cached profiles with anti-spoofing analysis"""
    matched: bool
    speaker_name: Optional[str] = None
    similarity: float = 0.0
    match_type: str = "none"  # "instant", "standard", "learning", "none"
    match_time_ms: float = 0.0
    profile_source: str = "none"  # "preloaded", "session_cache", "database", "chromadb"

    # Anti-spoofing results
    spoofing_detected: bool = False
    spoofing_type: Optional[str] = None  # "replay", "synthetic", "voice_conversion"
    spoofing_confidence: float = 0.0

    # Cost tracking
    cost_usd: float = 0.0
    cache_saved_cost: float = 0.0

    # Voice evolution
    voice_drift_detected: bool = False
    voice_drift_percentage: float = 0.0

    # Behavioral analysis
    behavioral_score: float = 0.0
    time_of_day_normal: bool = True

    @property
    def is_instant_match(self) -> bool:
        return self.match_type == "instant"

    @property
    def is_learning_only(self) -> bool:
        return self.match_type == "learning"

    @property
    def is_secure(self) -> bool:
        """Check if match is secure (no spoofing detected)"""
        return self.matched and not self.spoofing_detected

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/API response"""
        return {
            "matched": self.matched,
            "speaker_name": self.speaker_name,
            "similarity": round(self.similarity, 4),
            "match_type": self.match_type,
            "match_time_ms": round(self.match_time_ms, 2),
            "profile_source": self.profile_source,
            "spoofing_detected": self.spoofing_detected,
            "spoofing_type": self.spoofing_type,
            "cost_usd": round(self.cost_usd, 6),
            "cache_saved_cost": round(self.cache_saved_cost, 6),
            "voice_drift_percentage": round(self.voice_drift_percentage, 4),
            "behavioral_score": round(self.behavioral_score, 2),
        }


@dataclass
class CacheStats:
    """Comprehensive cache statistics with cost and security metrics"""
    state: CacheState = CacheState.UNINITIALIZED
    profiles_preloaded: int = 0
    models_loaded: bool = False

    # Match statistics
    total_lookups: int = 0
    instant_matches: int = 0
    standard_matches: int = 0
    learning_matches: int = 0
    no_matches: int = 0

    # ChromaDB semantic cache statistics
    chromadb_enabled: bool = False
    chromadb_lookups: int = 0
    chromadb_hits: int = 0
    chromadb_patterns_stored: int = 0

    # Timing
    avg_match_time_ms: float = 0.0
    total_time_saved_ms: float = 0.0
    avg_chromadb_lookup_ms: float = 0.0

    # Learning
    samples_recorded: int = 0
    embedding_updates: int = 0

    # Cost optimization (Helicone-style)
    total_cost_usd: float = 0.0
    total_cost_saved_usd: float = 0.0
    cache_hit_savings_usd: float = 0.0
    embeddings_extracted: int = 0
    embeddings_from_cache: int = 0

    # Anti-spoofing statistics
    spoofing_attempts_detected: int = 0
    replay_attacks_blocked: int = 0
    synthetic_voice_blocked: int = 0

    # Voice evolution
    voice_drift_detections: int = 0
    profiles_auto_updated: int = 0

    def to_dict(self) -> Dict[str, Any]:
        total = max(1, self.total_lookups)
        return {
            "state": self.state.value,
            "profiles_preloaded": self.profiles_preloaded,
            "models_loaded": self.models_loaded,
            "total_lookups": self.total_lookups,
            "instant_matches": self.instant_matches,
            "standard_matches": self.standard_matches,
            "learning_matches": self.learning_matches,
            "no_matches": self.no_matches,
            "instant_match_rate": self.instant_matches / total,
            "overall_match_rate": (self.instant_matches + self.standard_matches) / total,
            "avg_match_time_ms": round(self.avg_match_time_ms, 2),
            "total_time_saved_ms": round(self.total_time_saved_ms, 0),
            "samples_recorded": self.samples_recorded,
            "embedding_updates": self.embedding_updates,
            # ChromaDB stats
            "chromadb_enabled": self.chromadb_enabled,
            "chromadb_lookups": self.chromadb_lookups,
            "chromadb_hits": self.chromadb_hits,
            "chromadb_hit_rate": self.chromadb_hits / max(1, self.chromadb_lookups),
            "chromadb_patterns_stored": self.chromadb_patterns_stored,
            "avg_chromadb_lookup_ms": round(self.avg_chromadb_lookup_ms, 2),
            # Cost stats
            "total_cost_usd": round(self.total_cost_usd, 4),
            "total_cost_saved_usd": round(self.total_cost_saved_usd, 4),
            "cache_hit_savings_usd": round(self.cache_hit_savings_usd, 4),
            "embeddings_extracted": self.embeddings_extracted,
            "embeddings_from_cache": self.embeddings_from_cache,
            "cache_efficiency": self.embeddings_from_cache / max(1, self.embeddings_extracted + self.embeddings_from_cache),
            # Security stats
            "spoofing_attempts_detected": self.spoofing_attempts_detected,
            "replay_attacks_blocked": self.replay_attacks_blocked,
            "synthetic_voice_blocked": self.synthetic_voice_blocked,
            # Evolution stats
            "voice_drift_detections": self.voice_drift_detections,
            "profiles_auto_updated": self.profiles_auto_updated,
        }


class UnifiedVoiceCacheManager:
    """
    Unified Voice Cache Manager - Orchestrates all voice biometric caching.

    This is the central hub that connects:
    1. SQLite database (voice_embeddings table with stored profiles)
    2. VoiceBiometricCache (session-based runtime cache)
    3. ParallelModelLoader (ECAPA-TDNN, Whisper models)
    4. ChromaDB (semantic pattern matching, optional)
    5. Continuous Learning Engine (profile improvement)

    CRITICAL: Preloads voice profiles at startup so Derek's voice is
    instantly recognized without recomputing embeddings!
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        chroma_path: Optional[str] = None,
        config: Optional[CacheConfig] = None,
    ):
        """
        Initialize the unified cache manager with ChromaDB and cost tracking.

        Args:
            db_path: Path to SQLite database with voice embeddings
            chroma_path: Path to ChromaDB storage (optional)
            config: Cache configuration
        """
        self.db_path = db_path or CacheConfig.DEFAULT_DB_PATH
        self.chroma_path = chroma_path or CacheConfig.DEFAULT_CHROMA_PATH
        self.config = config or CacheConfig()

        # State tracking
        self._state = CacheState.UNINITIALIZED
        self._init_lock = asyncio.Lock()

        # Preloaded voice profiles (speaker_name -> VoiceProfile)
        self._preloaded_profiles: Dict[str, VoiceProfile] = {}

        # Session cache for recently verified embeddings
        self._session_cache: Dict[str, Tuple[np.ndarray, datetime]] = {}

        # Reference to parallel model loader (lazy loaded)
        self._model_loader = None

        # Reference to metrics database (lazy loaded)
        self._metrics_db = None

        # Reference to voice biometric cache (for integration)
        self._biometric_cache = None

        # =================================================================
        # CHROMADB SEMANTIC CACHE (Helicone-style pattern caching)
        # =================================================================
        self._chroma_client = None
        self._chroma_collection = None
        self._chromadb_initialized = False

        # =================================================================
        # ANTI-SPOOFING: Recent embeddings for replay detection
        # =================================================================
        self._recent_embeddings: List[Tuple[np.ndarray, datetime, str]] = []
        self._max_recent_embeddings = 50

        # =================================================================
        # COST TRACKING
        # =================================================================
        self._cost_tracker: Optional[CostTracker] = None
        if COST_TRACKER_AVAILABLE and CacheConfig.ENABLE_COST_TRACKING:
            try:
                self._cost_tracker = get_cost_tracker()
            except Exception as e:
                logger.debug(f"Cost tracker not initialized: {e}")

        # Statistics
        self._stats = CacheStats()

        # Background task handles
        self._background_tasks: List[asyncio.Task] = []

        # Voice evolution tracking
        self._evolution_check_interval = timedelta(days=CacheConfig.VOICE_DRIFT_WINDOW_DAYS)

        logger.info(
            f"UnifiedVoiceCacheManager created "
            f"(db={self.db_path}, chroma={self.chroma_path})"
        )
        logger.info(f"  ChromaDB available: {CHROMADB_AVAILABLE}")
        logger.info(f"  Cost tracking enabled: {CacheConfig.ENABLE_COST_TRACKING}")

    @property
    def state(self) -> CacheState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return self._state == CacheState.READY

    @property
    def profiles_loaded(self) -> int:
        return len(self._preloaded_profiles)

    # =========================================================================
    # INITIALIZATION
    # =========================================================================

    async def initialize(
        self,
        preload_profiles: bool = True,
        preload_models: bool = True,
        connect_biometric_cache: bool = True,
        init_chromadb: bool = True,
        timeout: float = CacheConfig.PRELOAD_TIMEOUT_SECONDS,
    ) -> bool:
        """
        Initialize the unified cache system with ChromaDB and cost tracking.

        This is the CRITICAL startup path that:
        1. Loads Derek's voice profile from SQLite
        2. Preloads ML models (via ParallelModelLoader)
        3. Connects to VoiceBiometricCache for session caching
        4. Initializes ChromaDB for semantic pattern caching
        5. Sets up cost tracking integration

        Args:
            preload_profiles: Load voice profiles from database
            preload_models: Prewarm ML models
            connect_biometric_cache: Connect to session cache
            init_chromadb: Initialize ChromaDB semantic cache
            timeout: Maximum time for initialization

        Returns:
            True if initialization successful
        """
        async with self._init_lock:
            if self._state == CacheState.READY:
                logger.debug("UnifiedVoiceCacheManager already initialized")
                return True

            start_time = time.time()
            self._state = CacheState.INITIALIZING

            try:
                # Run initialization tasks in parallel
                tasks = []

                if preload_profiles:
                    tasks.append(self._preload_voice_profiles())

                if preload_models:
                    tasks.append(self._ensure_models_loaded())

                if connect_biometric_cache:
                    tasks.append(self._connect_biometric_cache())

                if init_chromadb and CHROMADB_AVAILABLE:
                    tasks.append(self._initialize_chromadb())

                # Execute all tasks with timeout
                if tasks:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=timeout
                    )

                self._state = CacheState.READY
                init_time = (time.time() - start_time) * 1000

                logger.info(
                    f"✅ UnifiedVoiceCacheManager initialized in {init_time:.0f}ms "
                    f"(profiles={self.profiles_loaded}, "
                    f"models_ready={self._stats.models_loaded}, "
                    f"chromadb={self._chromadb_initialized})"
                )
                return True

            except asyncio.TimeoutError:
                self._state = CacheState.ERROR
                logger.error(
                    f"UnifiedVoiceCacheManager initialization timed out "
                    f"after {timeout}s"
                )
                return False

            except Exception as e:
                self._state = CacheState.ERROR
                logger.error(f"UnifiedVoiceCacheManager initialization failed: {e}")
                return False

    async def _initialize_chromadb(self) -> bool:
        """
        Initialize ChromaDB for semantic voice pattern caching.

        ChromaDB provides:
        - Fast similarity search for voice patterns
        - Persistent storage of authentication patterns
        - Anti-spoofing via pattern anomaly detection
        - Cost optimization by caching similar requests

        Returns:
            True if ChromaDB initialized successfully
        """
        if not CHROMADB_AVAILABLE:
            logger.debug("ChromaDB not available - skipping initialization")
            return False

        try:
            # Create ChromaDB directory if needed
            os.makedirs(self.chroma_path, exist_ok=True)

            # Initialize persistent client
            if CacheConfig.CHROMA_PERSIST_ENABLED:
                self._chroma_client = chromadb.PersistentClient(
                    path=self.chroma_path,
                    settings=ChromaSettings(
                        anonymized_telemetry=False,
                        allow_reset=True,
                    )
                )
            else:
                self._chroma_client = chromadb.Client()

            # Get or create voice patterns collection
            self._chroma_collection = self._chroma_client.get_or_create_collection(
                name=CacheConfig.CHROMA_COLLECTION_NAME,
                metadata={
                    "description": "Voice biometric patterns for semantic caching",
                    "hnsw:space": "cosine",  # Use cosine similarity
                    "created_at": datetime.now().isoformat(),
                }
            )

            self._chromadb_initialized = True
            self._stats.chromadb_enabled = True

            # Get existing pattern count
            count = self._chroma_collection.count()
            self._stats.chromadb_patterns_stored = count

            logger.info(
                f"✅ ChromaDB initialized: {count} patterns stored "
                f"(path={self.chroma_path})"
            )
            return True

        except Exception as e:
            logger.warning(f"ChromaDB initialization failed: {e}")
            self._chromadb_initialized = False
            return False

    async def semantic_cache_lookup(
        self,
        embedding: np.ndarray,
        speaker_hint: Optional[str] = None,
        n_results: int = 3,
    ) -> Optional[MatchResult]:
        """
        Look up embedding in ChromaDB semantic cache.

        This is the FAST PATH for voice pattern matching:
        - Searches for similar patterns in ChromaDB
        - Returns cached result if similarity > threshold
        - Saves cost by avoiding redundant ML processing

        Args:
            embedding: Voice embedding to search for
            speaker_hint: Optional speaker name hint
            n_results: Number of results to retrieve

        Returns:
            MatchResult if cache hit, None otherwise
        """
        if not self._chromadb_initialized or self._chroma_collection is None:
            return None

        start_time = time.time()
        self._stats.chromadb_lookups += 1

        try:
            # Normalize embedding
            embedding_normalized = self._normalize_embedding(embedding)

            # Query ChromaDB for similar patterns
            results = self._chroma_collection.query(
                query_embeddings=[embedding_normalized.tolist()],
                n_results=n_results,
                include=["metadatas", "distances"],
            )

            if not results["ids"] or not results["ids"][0]:
                return None

            # Check best match
            best_distance = results["distances"][0][0] if results["distances"][0] else 1.0
            best_similarity = 1.0 - best_distance  # Convert distance to similarity
            best_metadata = results["metadatas"][0][0] if results["metadatas"][0] else {}

            lookup_time_ms = (time.time() - start_time) * 1000

            # Update stats
            n = self._stats.chromadb_lookups
            self._stats.avg_chromadb_lookup_ms = (
                (self._stats.avg_chromadb_lookup_ms * (n - 1) + lookup_time_ms) / n
            )

            # Check if match is good enough
            if best_similarity >= CacheConfig.SEMANTIC_SIMILARITY_THRESHOLD:
                self._stats.chromadb_hits += 1

                # Calculate cost savings
                cost_saved = CacheConfig.COST_PER_EMBEDDING_EXTRACTION
                self._stats.cache_hit_savings_usd += cost_saved

                return MatchResult(
                    matched=True,
                    speaker_name=best_metadata.get("speaker_name", speaker_hint),
                    similarity=best_similarity,
                    match_type="instant" if best_similarity >= CacheConfig.INSTANT_MATCH_THRESHOLD else "standard",
                    match_time_ms=lookup_time_ms,
                    profile_source="chromadb",
                    cost_usd=0.0,
                    cache_saved_cost=cost_saved,
                )

            return None

        except Exception as e:
            logger.debug(f"ChromaDB lookup error: {e}")
            return None

    async def semantic_cache_store(
        self,
        embedding: np.ndarray,
        speaker_name: str,
        confidence: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Store embedding in ChromaDB semantic cache.

        Args:
            embedding: Voice embedding to store
            speaker_name: Speaker name
            confidence: Verification confidence
            metadata: Additional metadata

        Returns:
            True if stored successfully
        """
        if not self._chromadb_initialized or self._chroma_collection is None:
            return False

        try:
            # Generate unique ID
            embedding_hash = hashlib.md5(embedding.tobytes()).hexdigest()[:16]
            doc_id = f"{speaker_name}_{embedding_hash}_{int(time.time())}"

            # Normalize embedding
            embedding_normalized = self._normalize_embedding(embedding)

            # Prepare metadata
            doc_metadata = {
                "speaker_name": speaker_name,
                "confidence": confidence,
                "timestamp": datetime.now().isoformat(),
                "ttl_expires": (datetime.now() + timedelta(seconds=CacheConfig.SEMANTIC_CACHE_TTL_SECONDS)).isoformat(),
                **(metadata or {})
            }

            # Add to collection
            self._chroma_collection.add(
                ids=[doc_id],
                embeddings=[embedding_normalized.tolist()],
                metadatas=[doc_metadata],
            )

            self._stats.chromadb_patterns_stored += 1
            logger.debug(f"Stored pattern in ChromaDB: {doc_id}")
            return True

        except Exception as e:
            logger.debug(f"ChromaDB store error: {e}")
            return False

    def _to_numpy_flat(self, embedding) -> Optional[np.ndarray]:
        """
        Convert embedding (torch tensor or numpy array) to flat numpy array.

        CRITICAL: This handles the torch → numpy conversion that can cause NaN issues.
        """
        if embedding is None:
            return None

        try:
            import torch
            if isinstance(embedding, torch.Tensor):
                # Proper conversion: detach from graph, move to CPU, convert to numpy
                embedding = embedding.detach().cpu().numpy()
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"⚠️ Torch conversion warning: {e}")

        # Ensure numpy array and flatten
        try:
            embedding_np = np.asarray(embedding, dtype=np.float32).flatten()
        except Exception as e:
            logger.error(f"❌ Failed to convert embedding to numpy: {e}")
            return None

        return embedding_np

    def _normalize_embedding(self, embedding) -> Optional[np.ndarray]:
        """
        Normalize embedding to unit length for cosine similarity.

        CRITICAL: This function GUARANTEES a NaN-free return or None.

        Returns:
            Normalized numpy array (NaN-free) or None if embedding is invalid
        """
        # First, ensure we have a proper numpy array
        embedding_np = self._to_numpy_flat(embedding)
        if embedding_np is None:
            logger.warning("⚠️ _normalize_embedding: conversion to numpy failed")
            return None

        # Check for empty embedding
        if len(embedding_np) == 0:
            logger.warning("⚠️ _normalize_embedding: empty embedding")
            return None

        # CRITICAL: Check for NaN/Inf BEFORE any operations
        if np.any(np.isnan(embedding_np)) or np.any(np.isinf(embedding_np)):
            logger.warning("⚠️ Embedding contains NaN/Inf values - attempting recovery")
            # Replace NaN with 0, Inf with large finite values
            embedding_np = np.nan_to_num(embedding_np, nan=0.0, posinf=1e6, neginf=-1e6)

            # If ALL values were NaN, we get all zeros - this is unrecoverable
            if np.all(embedding_np == 0):
                logger.error("❌ Embedding was entirely NaN - unrecoverable")
                return None

        # Compute norm
        norm = np.linalg.norm(embedding_np)

        # Handle zero/invalid norm
        if norm == 0 or np.isnan(norm) or np.isinf(norm):
            logger.warning(f"⚠️ Invalid embedding norm: {norm}")
            return None

        # Normalize
        normalized = embedding_np / norm

        # FINAL VALIDATION: Ensure result is NaN-free (paranoia check)
        if np.any(np.isnan(normalized)):
            logger.error("❌ Normalization produced NaN - this should never happen!")
            return None

        return normalized

    # =========================================================================
    # ANTI-SPOOFING DETECTION
    # =========================================================================

    async def detect_replay_attack(
        self,
        embedding: np.ndarray,
        audio_fingerprint: Optional[str] = None,
    ) -> Tuple[bool, float, str]:
        """
        Detect potential replay attacks by analyzing embedding patterns.

        Replay attacks are detected by:
        1. Exact embedding matches (recording playback)
        2. Suspiciously similar consecutive attempts
        3. Missing natural voice variation

        Args:
            embedding: Current voice embedding
            audio_fingerprint: Optional audio fingerprint hash

        Returns:
            Tuple of (is_replay, confidence, reason)
        """
        current_time = datetime.now()

        # Clean old entries from recent embeddings
        window_start = current_time - timedelta(seconds=CacheConfig.REPLAY_DETECTION_WINDOW_SECONDS)
        self._recent_embeddings = [
            (emb, ts, fp) for emb, ts, fp in self._recent_embeddings
            if ts > window_start
        ]

        # Check for suspiciously similar embeddings
        for prev_embedding, prev_time, prev_fp in self._recent_embeddings:
            similarity = self._compute_similarity(embedding, prev_embedding)

            # Exact match - strong indicator of replay
            if similarity >= CacheConfig.MAX_REPLAY_SIMILARITY:
                self._stats.replay_attacks_blocked += 1
                self._stats.spoofing_attempts_detected += 1
                return (
                    True,
                    0.95,
                    f"Exact embedding match detected ({similarity:.4f}) - possible recording"
                )

            # Audio fingerprint match
            if audio_fingerprint and prev_fp and audio_fingerprint == prev_fp:
                self._stats.replay_attacks_blocked += 1
                self._stats.spoofing_attempts_detected += 1
                return (
                    True,
                    0.90,
                    "Audio fingerprint matches recent attempt - possible replay"
                )

        # Check for unnaturally consistent voice (no micro-variations)
        if len(self._recent_embeddings) >= 3:
            recent_similarities = []
            for prev_emb, _, _ in self._recent_embeddings[-3:]:
                sim = self._compute_similarity(embedding, prev_emb)
                recent_similarities.append(sim)

            # Natural voice has some variation between utterances
            variation = np.std(recent_similarities)
            if variation < CacheConfig.MIN_VOICE_VARIATION:
                self._stats.spoofing_attempts_detected += 1
                return (
                    True,
                    0.70,
                    f"Unnaturally consistent voice pattern (variation={variation:.4f})"
                )

        # Store current embedding for future comparison
        self._recent_embeddings.append((embedding.copy(), current_time, audio_fingerprint))

        # Limit history size
        if len(self._recent_embeddings) > self._max_recent_embeddings:
            self._recent_embeddings.pop(0)

        return (False, 0.0, "No replay attack detected")

    async def detect_synthetic_voice(
        self,
        embedding: np.ndarray,
        audio_features: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, float, str]:
        """
        Detect potential synthetic/deepfake voice.

        Synthetic voice detection based on:
        1. Embedding pattern analysis
        2. Audio feature anomalies (if provided)
        3. Comparison with known speaker baseline

        Args:
            embedding: Voice embedding to analyze
            audio_features: Optional dict with f0, snr, spectral features

        Returns:
            Tuple of (is_synthetic, confidence, reason)
        """
        # Check audio features if provided
        if audio_features:
            # Check SNR range
            snr = audio_features.get("snr_db", 15.0)
            if snr > 40:  # Unrealistically clean audio
                self._stats.synthetic_voice_blocked += 1
                self._stats.spoofing_attempts_detected += 1
                return (
                    True,
                    0.75,
                    f"Suspiciously clean audio (SNR={snr:.1f}dB)"
                )

            # Check F0 (fundamental frequency) stability
            f0_std = audio_features.get("f0_std", 10.0)
            if f0_std < 2.0:  # Too stable pitch
                self._stats.synthetic_voice_blocked += 1
                self._stats.spoofing_attempts_detected += 1
                return (
                    True,
                    0.70,
                    f"Unnaturally stable pitch (F0 std={f0_std:.2f})"
                )

        # Check against known speaker profiles
        for speaker_name, profile in self._preloaded_profiles.items():
            if not profile.is_valid():
                continue

            similarity = self._compute_similarity(embedding, profile.embedding)

            # If it matches a profile, check for synthetic indicators
            if similarity >= CacheConfig.STANDARD_MATCH_THRESHOLD:
                # Check voice drift from baseline
                if profile.baseline_embedding is not None:
                    drift = profile.check_voice_drift(embedding)
                    if drift > CacheConfig.VOICE_DRIFT_THRESHOLD * 3:
                        # Significant drift could indicate voice conversion
                        return (
                            True,
                            0.60,
                            f"Voice differs significantly from baseline (drift={drift:.4f})"
                        )

        return (False, 0.0, "No synthetic voice indicators detected")

    async def analyze_behavioral_context(
        self,
        speaker_name: str,
        unlock_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Analyze behavioral context for authentication decision.

        Args:
            speaker_name: Speaker attempting authentication
            unlock_time: Time of unlock attempt

        Returns:
            Dict with behavioral analysis scores
        """
        unlock_time = unlock_time or datetime.now()
        profile = self._preloaded_profiles.get(speaker_name)

        result = {
            "behavioral_score": 0.5,  # Neutral default
            "time_of_day_normal": True,
            "interval_normal": True,
            "reasons": [],
        }

        if not profile:
            result["reasons"].append("No profile for behavioral analysis")
            return result

        score = 0.5

        # Check time of day
        current_hour = unlock_time.hour
        if profile.typical_unlock_hours:
            if current_hour in profile.typical_unlock_hours:
                score += 0.2
                result["reasons"].append(f"Normal unlock hour ({current_hour})")
            else:
                score -= 0.1
                result["time_of_day_normal"] = False
                result["reasons"].append(f"Unusual unlock hour ({current_hour})")

        # Check interval since last unlock
        if profile.last_verified and profile.avg_unlock_interval_hours > 0:
            hours_since = (unlock_time - profile.last_verified).total_seconds() / 3600
            expected_interval = profile.avg_unlock_interval_hours

            # Within 2x expected interval is normal
            if hours_since <= expected_interval * 2:
                score += 0.15
                result["reasons"].append("Normal unlock interval")
            elif hours_since > expected_interval * 5:
                score -= 0.1
                result["interval_normal"] = False
                result["reasons"].append(f"Long time since last unlock ({hours_since:.1f}h)")

        # Clamp score
        result["behavioral_score"] = max(0.0, min(1.0, score))
        return result

    def _compute_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Compute cosine similarity between two embeddings"""
        if a is None or b is None:
            return 0.0
        # Handle NaN values - return 0 similarity if either contains NaN
        if np.any(np.isnan(a)) or np.any(np.isnan(b)):
            return 0.0
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0 or np.isnan(norm_a) or np.isnan(norm_b):
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    # =========================================================================
    # VOICE PROFILE LOADING
    # =========================================================================

    async def _preload_voice_profiles(self) -> int:
        """
        Preload voice profiles from SQLite database.

        This is the KEY optimization - loads Derek's embedding at startup
        so voice matching is instant (no database query needed).

        Database source priority:
        1. Learning database (primary) - ~/.jarvis/learning/jarvis_learning.db
        2. Voice unlock metrics (fallback) - ~/.jarvis/voice_unlock_metrics.db

        Returns:
            Number of profiles preloaded
        """
        self._state = CacheState.LOADING_PROFILES

        try:
            import sqlite3

            loaded = 0

            # ================================================================
            # PRIMARY SOURCE: Learning Database (has Derek's actual voiceprint)
            # ================================================================
            db_dir = os.path.expanduser("~/.jarvis/learning")
            learning_db_path = os.path.join(db_dir, "jarvis_learning.db")

            if os.path.exists(learning_db_path):
                try:
                    conn = sqlite3.connect(learning_db_path)
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()

                    # Query speaker_profiles table (has voiceprint_embedding column)
                    cursor.execute("""
                        SELECT
                            speaker_name,
                            voiceprint_embedding,
                            embedding_dimension,
                            total_samples,
                            recognition_confidence,
                            is_primary_user,
                            last_updated
                        FROM speaker_profiles
                        WHERE voiceprint_embedding IS NOT NULL
                        ORDER BY is_primary_user DESC, last_updated DESC
                    """)

                    rows = cursor.fetchall()
                    conn.close()

                    for row in rows:
                        try:
                            speaker_name = row["speaker_name"]
                            embedding_blob = row["voiceprint_embedding"]
                            embedding_dim = row["embedding_dimension"] or CacheConfig.EMBEDDING_DIM
                            samples = row["total_samples"] or 0
                            confidence = row["recognition_confidence"] or 0.0
                            is_primary = row["is_primary_user"] or 0
                            updated_at = row["last_updated"]

                            # voiceprint_embedding is stored as raw bytes (float32 array)
                            embedding = np.frombuffer(embedding_blob, dtype=np.float32)

                            # Validate embedding dimensions
                            # Accept both 192-dim (ECAPA-TDNN) and other valid dimensions
                            actual_dim = len(embedding)
                            expected_dim = embedding_dim or CacheConfig.EMBEDDING_DIM

                            if actual_dim == 0:
                                logger.warning(f"Empty embedding for {speaker_name}")
                                continue

                            if actual_dim != expected_dim and actual_dim != CacheConfig.EMBEDDING_DIM:
                                logger.warning(
                                    f"Embedding dimension mismatch for {speaker_name}: "
                                    f"got {actual_dim}, expected {expected_dim}"
                                )
                                # Still try to use it if it's a valid size
                                if actual_dim < 50:
                                    continue

                            # Create profile with actual dimension
                            profile = VoiceProfile(
                                speaker_name=speaker_name,
                                embedding=embedding,
                                embedding_dimensions=actual_dim,
                                total_samples=samples,
                                avg_confidence=confidence,
                                source="learning_database",
                            )

                            if updated_at:
                                try:
                                    profile.last_verified = datetime.fromisoformat(
                                        updated_at.replace("Z", "+00:00")
                                    )
                                except:
                                    pass

                            self._preloaded_profiles[speaker_name] = profile
                            loaded += 1

                            owner_tag = " [OWNER]" if is_primary else ""
                            logger.info(
                                f"✅ Preloaded voice profile: {speaker_name}{owner_tag} "
                                f"(dim={len(embedding)}, samples={samples}, "
                                f"confidence={confidence:.2%})"
                            )

                        except Exception as e:
                            logger.warning(f"Failed to load profile: {e}")

                    if loaded > 0:
                        logger.info(
                            f"✅ Preloaded {loaded} voice profile(s) from learning database"
                        )

                except sqlite3.Error as e:
                    logger.warning(f"Learning database error: {e}")

            # ================================================================
            # FALLBACK: Voice unlock metrics database (if learning DB failed)
            # ================================================================
            if loaded == 0:
                fallback_db_path = os.path.join(
                    os.path.expanduser("~/.jarvis"),
                    "voice_unlock_metrics.db"
                )

                if os.path.exists(fallback_db_path):
                    try:
                        conn = sqlite3.connect(fallback_db_path)
                        conn.row_factory = sqlite3.Row
                        cursor = conn.cursor()

                        cursor.execute("""
                            SELECT
                                speaker_name,
                                embedding_b64,
                                embedding_dimensions,
                                total_samples_used,
                                avg_sample_confidence,
                                updated_at,
                                source
                            FROM voice_embeddings
                            WHERE embedding_b64 IS NOT NULL
                            ORDER BY updated_at DESC
                        """)

                        rows = cursor.fetchall()
                        conn.close()

                        for row in rows:
                            try:
                                speaker_name = row["speaker_name"]
                                embedding_b64 = row["embedding_b64"]
                                dimensions = row["embedding_dimensions"] or 192
                                samples = row["total_samples_used"] or 0
                                confidence = row["avg_sample_confidence"] or 0.0
                                source = row["source"] or "metrics_database"

                                # Decode base64 embedding
                                embedding_bytes = base64.b64decode(embedding_b64)
                                embedding = np.frombuffer(embedding_bytes, dtype=np.float32)

                                # Validate dimensions
                                if len(embedding) != dimensions:
                                    continue

                                profile = VoiceProfile(
                                    speaker_name=speaker_name,
                                    embedding=embedding,
                                    embedding_dimensions=dimensions,
                                    total_samples=samples,
                                    avg_confidence=confidence,
                                    source=source,
                                )

                                self._preloaded_profiles[speaker_name] = profile
                                loaded += 1

                                logger.info(
                                    f"Preloaded voice profile (fallback): {speaker_name}"
                                )

                            except Exception as e:
                                logger.warning(f"Failed to load fallback profile: {e}")

                        if loaded > 0:
                            logger.info(
                                f"Preloaded {loaded} profile(s) from fallback metrics database"
                            )

                    except sqlite3.Error as e:
                        logger.warning(f"Fallback metrics database error: {e}")

            if loaded == 0:
                logger.warning(
                    "⚠️ No voice profiles loaded! Voice recognition will fail. "
                    "Ensure Derek's voiceprint is enrolled in the learning database."
                )

            self._stats.profiles_preloaded = loaded
            return loaded

        except Exception as e:
            logger.error(f"Failed to preload voice profiles: {e}", exc_info=True)
            return 0

    async def _ensure_models_loaded(self) -> bool:
        """
        Ensure ML models are loaded via ParallelModelLoader.

        This connects the cache layer to the parallel model loader, enabling:
        1. Shared ECAPA-TDNN encoder for embedding extraction
        2. Model caching to prevent redundant loading
        3. Fast embedding computation for voice matching

        ENHANCED v2.0: Smart cloud-aware loading with proper fallback orchestration.

        Flow:
        1. Check ML Registry cloud status
        2. If cloud mode: verify cloud extraction works, skip local load
        3. If local mode: attempt local load with retries
        4. If both fail: clear error with diagnostics

        Returns:
            True if models are ready (local or cloud)
        """
        self._state = CacheState.LOADING_MODELS
        max_retries = int(os.getenv("JARVIS_ECAPA_LOAD_RETRIES", "2"))

        # Track failure reasons for diagnostics
        failure_reasons = []

        # =========================================================================
        # STEP 1: Use ensure_ecapa_available() for robust ECAPA initialization
        # CRITICAL FIX v2.0: This ensures registry is created + ECAPA is loaded
        # =========================================================================
        try:
            from voice_unlock.ml_engine_registry import (
                ensure_ecapa_available,
                get_ml_registry_sync,
            )

            # This single call handles:
            # - Registry creation if not exists
            # - Cloud mode verification
            # - Local ECAPA loading with retry
            # - Timeout handling
            logger.info("🔍 Using ensure_ecapa_available() for ECAPA initialization...")

            ecapa_timeout = float(os.getenv("JARVIS_ECAPA_ENSURE_TIMEOUT", "45.0"))
            success, message, encoder = await ensure_ecapa_available(
                timeout=ecapa_timeout,
                allow_cloud=True,
            )

            if success:
                logger.info(f"✅ ECAPA available via ensure_ecapa_available(): {message}")
                self._stats.models_loaded = True

                # Store the encoder if provided (local mode)
                if encoder is not None:
                    self._direct_ecapa_encoder = encoder
                    self._using_cloud_ecapa = False
                else:
                    # Cloud mode - encoder is remote
                    self._using_cloud_ecapa = True

                return True
            else:
                logger.warning(f"⚠️ ensure_ecapa_available() failed: {message}")
                failure_reasons.append(f"ensure_ecapa_available failed: {message}")

        except ImportError as ie:
            logger.debug(f"ensure_ecapa_available not available: {ie} - using fallback")
        except Exception as e:
            logger.warning(f"⚠️ ensure_ecapa_available() error: {e}")
            failure_reasons.append(f"ensure_ecapa_available error: {e}")

        # =========================================================================
        # STEP 1b: Fallback - Check ML Registry directly (if ensure failed)
        # =========================================================================
        try:
            from voice_unlock.ml_engine_registry import get_ml_registry_sync

            registry = get_ml_registry_sync(auto_create=True)

            if registry is not None:
                # Check if registry is using cloud mode
                if registry.is_using_cloud:
                    logger.info("☁️ ML Registry in cloud mode - checking cloud ECAPA availability...")

                    # Verify cloud is actually verified and ready
                    if getattr(registry, "_cloud_verified", False):
                        logger.info("✅ Cloud ECAPA verified via ML Registry - skipping local load")
                        self._stats.models_loaded = True
                        self._using_cloud_ecapa = True
                        return True
                    else:
                        # Cloud mode but not verified - this is the root cause bug!
                        logger.warning("⚠️ ML Registry in cloud mode but cloud NOT verified!")
                        failure_reasons.append("Cloud mode active but cloud backend not verified")

                        # Check if registry has a working local fallback
                        ecapa_status = registry.get_ecapa_status()
                        if ecapa_status.get("local_loaded"):
                            logger.info("✅ Using local ECAPA from ML Registry (cloud fallback)")
                            self._stats.models_loaded = True
                            self._using_cloud_ecapa = False
                            return True

                # Check if local ECAPA is loaded in registry
                ecapa_status = registry.get_ecapa_status()
                if ecapa_status.get("available"):
                    logger.info(f"✅ ECAPA available via ML Registry (source: {ecapa_status.get('source')})")
                    self._stats.models_loaded = True
                    self._using_cloud_ecapa = ecapa_status.get("source") == "cloud"
                    return True

        except ImportError:
            logger.debug("ML Engine Registry not available - using local loading")
        except Exception as e:
            logger.warning(f"⚠️ ML Registry check failed: {e}")
            failure_reasons.append(f"ML Registry check failed: {e}")

        # =========================================================================
        # STEP 2: Try parallel model loader (standard path)
        # =========================================================================
        try:
            from voice.parallel_model_loader import get_model_loader

            if self._model_loader is None:
                self._model_loader = get_model_loader()

            # Check if already cached in parallel loader
            if self._model_loader.is_cached("ecapa_encoder"):
                self._stats.models_loaded = True
                logger.info("✅ ECAPA-TDNN model already cached in parallel loader")
                return True

            # Try to load the ECAPA encoder with retries
            for attempt in range(max_retries):
                try:
                    logger.info(
                        f"🔄 Loading ECAPA-TDNN encoder (attempt {attempt + 1}/{max_retries})..."
                    )
                    result = await self._model_loader.load_model(
                        model_name="ecapa_encoder",
                        load_func=self._create_ecapa_loader(),
                        timeout=float(os.getenv("JARVIS_ECAPA_LOCAL_TIMEOUT", "60.0")),
                        use_cache=True,
                    )
                    if result.success:
                        self._stats.models_loaded = True
                        logger.info(
                            f"✅ ECAPA-TDNN loaded via parallel loader "
                            f"in {result.load_time_ms:.0f}ms"
                        )
                        return True
                    else:
                        logger.warning(
                            f"⚠️ ECAPA-TDNN load attempt {attempt + 1} failed: {result.error}"
                        )
                        failure_reasons.append(f"Parallel loader attempt {attempt + 1}: {result.error}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(0.5)  # Brief pause before retry

                except asyncio.TimeoutError:
                    logger.warning(
                        f"⏱️ ECAPA-TDNN load attempt {attempt + 1} timed out"
                    )
                    failure_reasons.append(f"Parallel loader attempt {attempt + 1}: Timeout")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.5)

                except Exception as load_err:
                    logger.warning(
                        f"⚠️ ECAPA-TDNN load attempt {attempt + 1} error: {load_err}"
                    )
                    failure_reasons.append(f"Parallel loader attempt {attempt + 1}: {load_err}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.5)

            # All retries exhausted - try direct loading as fallback
            logger.info("🔄 Attempting direct ECAPA-TDNN load (fallback)...")
            try:
                encoder = await self._load_ecapa_directly()
                if encoder is not None:
                    # Cache it manually
                    self._model_loader.cache_model("ecapa_encoder", encoder)
                    self._stats.models_loaded = True
                    logger.info("✅ ECAPA-TDNN loaded via direct fallback")
                    return True
            except Exception as direct_err:
                logger.warning(f"⚠️ Direct ECAPA load failed: {direct_err}")
                failure_reasons.append(f"Direct load: {direct_err}")

        except ImportError as e:
            logger.warning(f"⚠️ ParallelModelLoader not available: {e}")
            failure_reasons.append(f"ParallelModelLoader import: {e}")
            # Try direct loading
            try:
                encoder = await self._load_ecapa_directly()
                if encoder is not None:
                    self._stats.models_loaded = True
                    # Store encoder directly on instance for get_ecapa_encoder fallback
                    self._direct_ecapa_encoder = encoder
                    logger.info("✅ ECAPA-TDNN loaded directly (no parallel loader)")
                    return True
            except Exception as direct_err:
                failure_reasons.append(f"Direct load fallback: {direct_err}")

        except Exception as e:
            logger.warning(f"⚠️ Model loader error: {e}")
            failure_reasons.append(f"Model loader: {e}")

        # =========================================================================
        # STEP 3: Fallback to preloaded embeddings (matching only, no extraction)
        # =========================================================================
        if len(self._preloaded_profiles) > 0:
            logger.warning(
                f"⚠️ ECAPA-TDNN not available. Using {len(self._preloaded_profiles)} "
                f"preloaded embeddings for matching only (no real-time extraction)"
            )
            logger.warning(f"   Failure reasons: {failure_reasons}")
            self._stats.models_loaded = False
            self._encoder_failure_reasons = failure_reasons
            return True  # Can still function with preloaded embeddings

        # =========================================================================
        # STEP 4: Complete failure - log diagnostics
        # =========================================================================
        logger.error("=" * 70)
        logger.error("❌ ECAPA-TDNN ENCODER UNAVAILABLE - Voice verification will FAIL!")
        logger.error("=" * 70)
        logger.error("   Failure chain:")
        for i, reason in enumerate(failure_reasons, 1):
            logger.error(f"   {i}. {reason}")
        logger.error("   No preloaded embeddings available as fallback")
        logger.error("=" * 70)

        self._stats.models_loaded = False
        self._encoder_failure_reasons = failure_reasons
        return False

    def get_encoder_status(self) -> Dict[str, Any]:
        """
        Get detailed ECAPA encoder availability status from all sources.

        Returns comprehensive diagnostics for troubleshooting.
        """
        status = {
            "available": False,
            "source": None,
            "models_loaded": self._stats.models_loaded,
            "using_cloud": getattr(self, "_using_cloud_ecapa", False),
            "preloaded_profiles": len(self._preloaded_profiles),
            "failure_reasons": getattr(self, "_encoder_failure_reasons", []),
            "diagnostics": {}
        }

        # Check parallel model loader
        if self._model_loader is not None:
            try:
                if self._model_loader.is_cached("ecapa_encoder"):
                    status["available"] = True
                    status["source"] = "parallel_loader"
                    status["diagnostics"]["parallel_loader"] = "cached"
            except Exception as e:
                status["diagnostics"]["parallel_loader_error"] = str(e)

        # Check direct encoder
        if hasattr(self, "_direct_ecapa_encoder") and self._direct_ecapa_encoder is not None:
            status["available"] = True
            status["source"] = "direct_encoder"
            status["diagnostics"]["direct_encoder"] = "loaded"

        # Check ML Registry
        try:
            from voice_unlock.ml_engine_registry import get_ml_registry_sync
            registry = get_ml_registry_sync()
            if registry:
                ecapa_status = registry.get_ecapa_status()
                status["diagnostics"]["ml_registry"] = ecapa_status
                if ecapa_status.get("available") and not status["available"]:
                    status["available"] = True
                    status["source"] = f"ml_registry ({ecapa_status.get('source')})"
        except Exception as e:
            status["diagnostics"]["ml_registry_error"] = str(e)

        # Final determination
        if status["available"]:
            status["failure_reasons"] = []  # Clear failure reasons if now available
        elif not status["failure_reasons"]:
            status["failure_reasons"] = ["Unknown - no encoder source available"]

        return status

    async def _load_ecapa_directly(self):
        """
        Direct ECAPA-TDNN loading as fallback when parallel loader fails.

        Returns:
            Loaded encoder or None
        """
        try:
            logger.info("🔄 Loading ECAPA-TDNN directly from SpeechBrain...")

            # Define loader function (imports inside to avoid issues)
            def _load():
                import torch
                from speechbrain.inference.speaker import EncoderClassifier

                # Force CPU to avoid MPS issues
                torch.set_num_threads(1)

                return EncoderClassifier.from_hparams(
                    source="speechbrain/spkrec-ecapa-voxceleb",
                    run_opts={"device": "cpu"}
                )

            # Use asyncio.to_thread (Python 3.9+) for thread-safe execution
            encoder = await asyncio.wait_for(
                asyncio.to_thread(_load),
                timeout=60.0
            )

            logger.info("✅ ECAPA-TDNN loaded directly")
            return encoder

        except ImportError as e:
            logger.warning(f"⚠️ SpeechBrain not installed - cannot load ECAPA-TDNN: {e}")
            return None
        except asyncio.TimeoutError:
            logger.warning("⏱️ Direct ECAPA load timed out after 60s")
            return None
        except Exception as e:
            logger.error(f"❌ Direct ECAPA load failed: {e}")
            return None

    def _create_ecapa_loader(self):
        """
        Create a function to load ECAPA-TDNN encoder.

        Returns a callable that loads the model, for use with ParallelModelLoader.
        """
        def _load_ecapa():
            import torch
            from speechbrain.inference.speaker import EncoderClassifier

            # Force CPU to avoid MPS issues with FFT operations
            torch.set_num_threads(1)

            encoder = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                run_opts={"device": "cpu"}
            )
            return encoder

        return _load_ecapa

    def get_ecapa_encoder(self):
        """
        Get the cached ECAPA-TDNN encoder from the parallel model loader.

        ENHANCED: Checks both parallel loader cache and direct encoder fallback.
        NOTE: This is a sync method - use ensure_encoder_available() for async loading.

        Returns:
            Encoder model if available, None otherwise
        """
        # First check direct encoder fallback (set when parallel loader unavailable)
        if hasattr(self, '_direct_ecapa_encoder') and self._direct_ecapa_encoder is not None:
            return self._direct_ecapa_encoder

        # Then check parallel loader cache
        if self._model_loader is not None:
            encoder = self._model_loader.get_cached("ecapa_encoder")
            if encoder is not None:
                return encoder

        # Check ML Registry as final sync fallback
        try:
            from voice_unlock.ml_engine_registry import get_ml_registry_sync
            registry = get_ml_registry_sync(auto_create=False)  # Don't create here, sync check only
            if registry:
                ecapa_wrapper = registry.get_wrapper("ecapa_tdnn")
                if ecapa_wrapper and ecapa_wrapper.is_loaded:
                    return ecapa_wrapper.get_engine()
        except Exception:
            pass

        logger.debug("ECAPA encoder not available from any source (sync check)")
        return None

    async def ensure_encoder_available(
        self,
        timeout: float = None,
        trigger_load: bool = True,
    ) -> Tuple[bool, Optional[Any], str]:
        """
        CRITICAL FIX v3.0: Ensure ECAPA encoder is available with on-demand loading.

        This method MUST be called before any voice verification to guarantee
        that ECAPA is available. It will trigger loading if necessary.

        Orchestration Flow:
        1. Check if encoder already available (fast path)
        2. If not, call ensure_ecapa_available() to trigger loading
        3. Wait for encoder to be ready
        4. Store encoder reference for fast subsequent access

        Args:
            timeout: Max seconds to wait (default from env or 45s)
            trigger_load: If True, trigger loading if encoder not available

        Returns:
            Tuple[success, encoder, message]:
            - success: True if encoder is available
            - encoder: The ECAPA encoder (or None for cloud mode)
            - message: Status/error message for diagnostics

        Usage:
            success, encoder, msg = await cache.ensure_encoder_available()
            if not success:
                return {"error": f"ECAPA unavailable: {msg}"}
        """
        import time as time_module

        # Get timeout from env or default
        if timeout is None:
            timeout = float(os.getenv("JARVIS_ECAPA_ENSURE_TIMEOUT", "45.0"))

        start_time = time_module.time()

        # Fast path: check if already available
        encoder = self.get_ecapa_encoder()
        if encoder is not None:
            return True, encoder, "ECAPA encoder available (cached)"

        # Check if we're in cloud mode with verified backend
        if getattr(self, "_using_cloud_ecapa", False):
            try:
                from voice_unlock.ml_engine_registry import get_ml_registry_sync
                registry = get_ml_registry_sync(auto_create=True)
                if registry and getattr(registry, "_cloud_verified", False):
                    return True, None, "Cloud ECAPA available (verified)"
            except Exception as e:
                logger.warning(f"Cloud mode check failed: {e}")

        if not trigger_load:
            return False, None, "ECAPA encoder not available and loading disabled"

        # Trigger loading via ensure_ecapa_available()
        logger.info("🔄 [ENSURE_ENCODER] Triggering ECAPA on-demand loading...")

        try:
            from voice_unlock.ml_engine_registry import ensure_ecapa_available

            success, message, loaded_encoder = await ensure_ecapa_available(
                timeout=timeout,
                allow_cloud=True,
            )

            if success:
                elapsed = time_module.time() - start_time
                logger.info(f"✅ [ENSURE_ENCODER] ECAPA available in {elapsed:.1f}s: {message}")

                # Store encoder reference for fast access
                if loaded_encoder is not None:
                    self._direct_ecapa_encoder = loaded_encoder
                    self._stats.models_loaded = True
                    self._using_cloud_ecapa = False
                else:
                    # Cloud mode
                    self._using_cloud_ecapa = True
                    self._stats.models_loaded = True

                return True, loaded_encoder, message
            else:
                return False, None, f"ECAPA loading failed: {message}"

        except ImportError as ie:
            logger.error(f"[ENSURE_ENCODER] ensure_ecapa_available not available: {ie}")
            return False, None, f"Import error: {ie}"
        except Exception as e:
            logger.error(f"[ENSURE_ENCODER] Unexpected error: {e}")
            return False, None, f"Error: {e}"

    async def extract_embedding(
        self,
        audio_data,
        sample_rate: int = 16000,
    ) -> Optional[np.ndarray]:
        """
        Extract voice embedding from audio using the cached ECAPA-TDNN model.

        ENHANCED v3.0: GUARANTEED encoder availability with on-demand loading.

        CRITICAL FIX: This method now ALWAYS ensures the encoder is available
        before attempting extraction. This prevents 0% confidence errors.

        Orchestration flow:
        0. ENSURE encoder is loaded (on-demand loading if necessary)
        1. Cloud extraction (if cloud mode active and verified)
        2. Direct ECAPA encoder (if loaded locally)
        3. ML Registry's extract_speaker_embedding
        4. Speaker Verification Service's engine

        Args:
            audio_data: Audio waveform (numpy array or tensor)
            sample_rate: Audio sample rate (default 16kHz)

        Returns:
            192-dimensional embedding or None if extraction fails
        """
        import time as time_module
        start_time = time_module.time()

        # Track extraction attempts for diagnostics
        attempts = []

        try:
            # =====================================================================
            # STEP 0: CRITICAL - ENSURE ENCODER IS AVAILABLE (ON-DEMAND LOADING)
            # =====================================================================
            # This is the key fix for 0% confidence - we MUST have an encoder
            ensure_start = time_module.time()
            encoder_available, encoder, encoder_status = await self.ensure_encoder_available(
                timeout=30.0,  # Allow up to 30s for first-time model loading
                trigger_load=True,  # Force loading if not available
            )
            ensure_time = (time_module.time() - ensure_start) * 1000
            if ensure_time > 100:
                logger.info(f"⏱️ ensure_encoder_available took {ensure_time:.1f}ms")

            if encoder_available:
                logger.debug(f"✅ Encoder guaranteed available: {encoder_status}")
                # Cache the encoder for immediate use
                if encoder is not None and not hasattr(self, '_direct_ecapa_encoder'):
                    self._direct_ecapa_encoder = encoder
            else:
                # Still continue with fallback paths - they might work
                logger.warning(f"⚠️ Encoder ensure failed: {encoder_status}")
                attempts.append(f"Step 0 ensure_encoder: {encoder_status}")

            # =====================================================================
            # PATH 1: Cloud extraction (if cloud mode active)
            # =====================================================================
            path1_start = time_module.time()
            if getattr(self, "_using_cloud_ecapa", False):
                try:
                    from voice_unlock.ml_engine_registry import get_ml_registry_sync

                    registry = get_ml_registry_sync()
                    if registry and getattr(registry, "_cloud_verified", False):
                        # Normalize audio data to bytes if needed
                        audio_bytes = normalize_audio_data(audio_data)
                        if audio_bytes:
                            cloud_start = time_module.time()
                            embedding = await registry.extract_speaker_embedding_cloud(audio_bytes)
                            cloud_time = (time_module.time() - cloud_start) * 1000
                            logger.info(f"⏱️ PATH 1 Cloud API call took {cloud_time:.1f}ms")
                            if embedding is not None:
                                # Use _normalize_embedding which handles torch→numpy and NaN validation
                                embedding_np = self._normalize_embedding(embedding)
                                if embedding_np is not None:
                                    extract_time_ms = (time_module.time() - start_time) * 1000
                                    logger.info(f"☁️ Extracted embedding via CLOUD in {extract_time_ms:.1f}ms total")
                                    return embedding_np
                                else:
                                    attempts.append("Cloud: normalization failed (NaN/invalid)")
                            else:
                                attempts.append("Cloud: extraction returned None")
                        else:
                            attempts.append("Cloud: audio normalization failed")
                    else:
                        attempts.append("Cloud: registry not verified")
                except Exception as e:
                    attempts.append(f"Cloud: {e}")
                    logger.debug(f"Cloud extraction failed: {e}")
            path1_time = (time_module.time() - path1_start) * 1000
            if path1_time > 100:
                logger.info(f"⏱️ PATH 1 (Cloud) total: {path1_time:.1f}ms")

            # =====================================================================
            # PATH 2: Local encoder (direct or cached)
            # =====================================================================
            path2_start = time_module.time()
            encoder = self.get_ecapa_encoder()

            if encoder is not None:
                try:
                    # Run CPU-intensive PyTorch operations in thread pool to avoid blocking
                    local_start = time_module.time()
                    raw_embedding = await asyncio.to_thread(
                        self._extract_embedding_sync,
                        encoder,
                        audio_data
                    )
                    local_time = (time_module.time() - local_start) * 1000
                    logger.info(f"⏱️ PATH 2 Local extraction took {local_time:.1f}ms")

                    if raw_embedding is not None:
                        # Normalize - handles torch→numpy and NaN validation
                        embedding_np = self._normalize_embedding(raw_embedding)
                        if embedding_np is not None:
                            extract_time_ms = (time_module.time() - start_time) * 1000
                            logger.info(f"🖥️ Extracted embedding in {extract_time_ms:.1f}ms (local)")
                            return embedding_np
                        else:
                            attempts.append("Local encoder: normalization failed (NaN/invalid)")
                    else:
                        attempts.append("Local encoder: extraction returned None")
                except Exception as e:
                    attempts.append(f"Local encoder: {e}")
                    logger.warning(f"Local encoder extraction failed: {e}")
            else:
                attempts.append("Local encoder: not available")
            path2_time = (time_module.time() - path2_start) * 1000
            if path2_time > 100:
                logger.info(f"⏱️ PATH 2 (Local) total: {path2_time:.1f}ms")

            # =====================================================================
            # PATH 3: ML Registry fallback
            # =====================================================================
            path3_start = time_module.time()
            logger.debug("Primary encoder unavailable, trying ML Registry fallback...")
            try:
                from voice_unlock.ml_engine_registry import extract_speaker_embedding as ml_extract

                # Normalize audio data to bytes
                audio_bytes = normalize_audio_data(audio_data)
                if audio_bytes:
                    ml_start = time_module.time()
                    embedding = await ml_extract(audio_bytes)
                    ml_time = (time_module.time() - ml_start) * 1000
                    logger.info(f"⏱️ PATH 3 ML Registry call took {ml_time:.1f}ms")
                    if embedding is not None:
                        # Use _normalize_embedding which handles torch→numpy and NaN validation
                        embedding_np = self._normalize_embedding(embedding)
                        if embedding_np is not None:
                            extract_time_ms = (time_module.time() - start_time) * 1000
                            logger.info(f"🔧 Extracted embedding via ML Registry in {extract_time_ms:.1f}ms")
                            return embedding_np
                        else:
                            attempts.append("ML Registry: normalization failed (NaN/invalid)")
                    else:
                        attempts.append("ML Registry: extraction returned None")
                else:
                    attempts.append("ML Registry: audio normalization failed")
            except ImportError:
                attempts.append("ML Registry: module not available")
            except Exception as e:
                attempts.append(f"ML Registry: {e}")
                logger.debug(f"ML Registry fallback failed: {e}")
            path3_time = (time_module.time() - path3_start) * 1000
            if path3_time > 100:
                logger.info(f"⏱️ PATH 3 (ML Registry) total: {path3_time:.1f}ms")

            # =====================================================================
            # PATH 4: Speaker Verification Service engine (last resort)
            # =====================================================================
            path4_start = time_module.time()
            logger.debug("Trying Speaker Verification Service fallback...")
            try:
                from voice.speaker_verification_service import get_speaker_verification_service

                svc = await get_speaker_verification_service()
                if svc and svc.speechbrain_engine:
                    audio_bytes = normalize_audio_data(audio_data)
                    if audio_bytes:
                        svc_start = time_module.time()
                        embedding = await svc.speechbrain_engine.extract_speaker_embedding(audio_bytes)
                        svc_time = (time_module.time() - svc_start) * 1000
                        logger.info(f"⏱️ PATH 4 Speaker Service call took {svc_time:.1f}ms")
                        if embedding is not None:
                            # Use _normalize_embedding which handles torch→numpy and NaN validation
                            embedding_np = self._normalize_embedding(embedding)
                            if embedding_np is not None:
                                extract_time_ms = (time_module.time() - start_time) * 1000
                                logger.info(f"🎤 Extracted embedding via Speaker Service in {extract_time_ms:.1f}ms")
                                return embedding_np
                            else:
                                attempts.append("Speaker Service: normalization failed (NaN/invalid)")
                        else:
                            attempts.append("Speaker Service: extraction returned None")
                    else:
                        attempts.append("Speaker Service: audio normalization failed")
                else:
                    attempts.append("Speaker Service: engine not available")
            except ImportError:
                attempts.append("Speaker Service: module not available")
            except Exception as e:
                attempts.append(f"Speaker Service: {e}")
                logger.debug(f"Speaker Service fallback failed: {e}")
            path4_time = (time_module.time() - path4_start) * 1000
            if path4_time > 100:
                logger.info(f"⏱️ PATH 4 (Speaker Service) total: {path4_time:.1f}ms")

            # =====================================================================
            # ALL PATHS FAILED - Log detailed diagnostics
            # =====================================================================
            total_time_ms = (time_module.time() - start_time) * 1000
            logger.error("=" * 60)
            logger.error("❌ EMBEDDING EXTRACTION FAILED - All paths exhausted!")
            logger.error(f"   ⏱️ Total time spent: {total_time_ms:.1f}ms")
            logger.error("=" * 60)
            logger.error("   Attempts:")
            for i, attempt in enumerate(attempts, 1):
                logger.error(f"   {i}. {attempt}")
            logger.error("   This will cause 0% confidence in voice verification!")
            logger.error("=" * 60)

            # Store failure reasons for diagnostics
            self._last_extraction_failure = {
                "timestamp": time_module.time(),
                "attempts": attempts,
                "audio_size": len(audio_data) if audio_data else 0
            }

            return None

        except Exception as e:
            logger.error(f"Embedding extraction critical error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _extract_embedding_sync(self, encoder, audio_data) -> Optional[np.ndarray]:
        """
        Synchronous embedding extraction - runs in thread pool.

        CRITICAL: This runs CPU-intensive PyTorch operations off the event loop.

        Handles all audio input types via audio_data_to_numpy helper.
        """
        try:
            import torch

            # Use robust audio conversion helper
            audio_np = audio_data_to_numpy(audio_data)
            if audio_np is None:
                logger.error("Failed to convert audio data to numpy array")
                return None

            # Convert numpy to torch tensor
            waveform = torch.from_numpy(audio_np).float()

            # Ensure correct shape: (batch, samples) or (samples,)
            if waveform.dim() == 1:
                waveform = waveform.unsqueeze(0)

            # Validate audio length (at least 0.5 seconds at 16kHz)
            min_samples = 8000  # 0.5s at 16kHz
            if waveform.shape[-1] < min_samples:
                logger.warning(
                    f"Audio too short for embedding: {waveform.shape[-1]} samples "
                    f"(need at least {min_samples})"
                )
                # Pad with zeros if too short
                padding = min_samples - waveform.shape[-1]
                waveform = torch.nn.functional.pad(waveform, (0, padding))

            # Extract embedding (CPU-intensive)
            with torch.no_grad():
                embedding = encoder.encode_batch(waveform)

            # Convert to numpy and flatten
            embedding_np = embedding.squeeze().cpu().numpy()

            # Validate embedding dimensions
            if embedding_np.shape[-1] != 192:
                logger.warning(
                    f"Unexpected embedding dimension: {embedding_np.shape[-1]} (expected 192)"
                )

            return embedding_np

        except Exception as e:
            logger.error(f"Sync embedding extraction failed: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None

    async def _connect_biometric_cache(self) -> bool:
        """
        Connect to VoiceBiometricCache for session caching.

        Returns:
            True if connected
        """
        try:
            from voice_unlock.voice_biometric_cache import VoiceBiometricCache

            # Create cache with our database recorder
            self._biometric_cache = VoiceBiometricCache(
                voice_sample_recorder=self._record_sample_to_db
            )

            logger.info("Connected to VoiceBiometricCache")
            return True

        except Exception as e:
            logger.warning(f"Failed to connect biometric cache: {e}")
            return False

    # =========================================================================
    # VOICE MATCHING - THE FAST PATH
    # =========================================================================

    async def verify_voice_from_audio(
        self,
        audio_data,
        sample_rate: int = 16000,
        expected_speaker: Optional[str] = None,
    ) -> MatchResult:
        """
        End-to-end voice verification from raw audio.

        This is the UNIFIED FAST PATH that:
        1. Extracts embedding using cached ECAPA-TDNN model
        2. Matches against preloaded voice profiles
        3. Updates session cache for faster future matches
        4. Records to database for continuous learning

        Args:
            audio_data: Raw audio waveform
            sample_rate: Audio sample rate
            expected_speaker: Optional speaker hint (e.g., "Derek")

        Returns:
            MatchResult with verification details
        """
        start_time = time.time()

        # Step 1: Extract embedding from audio
        embedding = await self.extract_embedding(audio_data, sample_rate)
        if embedding is None:
            return MatchResult(
                matched=False,
                match_type="none",
                match_time_ms=(time.time() - start_time) * 1000,
            )

        # Step 2: Match against profiles
        result = await self.match_voice_embedding(embedding, expected_speaker)

        # Step 3: If matched, trigger continuous learning (background)
        if result.matched and result.speaker_name:
            asyncio.create_task(
                self._record_successful_verification(
                    speaker_name=result.speaker_name,
                    embedding=embedding,
                    confidence=result.similarity,
                )
            )

        return result

    async def _record_successful_verification(
        self,
        speaker_name: str,
        embedding: np.ndarray,
        confidence: float,
    ):
        """
        Record successful verification for continuous learning.

        Fire-and-forget task that:
        1. Records to database for analytics
        2. Updates voice profile with new sample (if high confidence)
        """
        try:
            # Record to database
            await self._record_sample_to_db(
                speaker_name=speaker_name,
                confidence=confidence,
                was_verified=True,
            )

            # Update profile if very high confidence
            if confidence >= CacheConfig.INSTANT_MATCH_THRESHOLD:
                await self.update_voice_profile(
                    speaker_name=speaker_name,
                    new_embedding=embedding,
                    confidence=confidence,
                )

        except Exception as e:
            logger.debug(f"Background learning update failed: {e}")

    async def match_voice_embedding(
        self,
        embedding: np.ndarray,
        speaker_hint: Optional[str] = None,
    ) -> MatchResult:
        """
        Match a voice embedding against preloaded profiles.

        This is the FAST PATH for voice recognition:
        1. First check session cache (< 1ms)
        2. Then check preloaded profiles (< 5ms)
        3. Fall back to database query (10-50ms)

        IMPORTANT: Always returns the best similarity found, even if below
        threshold. This enables:
        - Progressive confidence communication (72% vs 0% makes a difference)
        - Continuous learning from borderline cases
        - Proper debugging and transparency

        Args:
            embedding: Voice embedding to match (192-dim)
            speaker_hint: Optional hint for expected speaker

        Returns:
            MatchResult with match details (always includes best similarity)
        """
        start_time = time.time()
        self._stats.total_lookups += 1

        if embedding is None or len(embedding) == 0:
            return MatchResult(matched=False, match_type="none")

        # Normalize embedding
        embedding = self._normalize_embedding(embedding)

        # Track best result across all strategies (even if below threshold)
        best_result: Optional[MatchResult] = None

        # Strategy 1: Session cache (fastest - recently verified)
        result = self._check_session_cache(embedding)
        if result.matched:
            result.match_time_ms = (time.time() - start_time) * 1000
            self._update_match_stats(result)
            return result

        # Strategy 2: Preloaded profiles (fast - loaded at startup)
        result = self._check_preloaded_profiles(embedding, speaker_hint)
        if result.matched:
            # Update session cache for future lookups
            self._update_session_cache(
                result.speaker_name, embedding
            )
            result.match_time_ms = (time.time() - start_time) * 1000
            self._update_match_stats(result)
            return result
        # Track best result even if not matched (for progressive confidence)
        if result.similarity > 0:
            best_result = result

        # Strategy 3: Database fallback (slower but comprehensive)
        result = await self._check_database_profiles(embedding)
        if result.matched:
            result.match_time_ms = (time.time() - start_time) * 1000
            self._update_match_stats(result)
            return result
        # Check if database gave us a better similarity
        if result.similarity > (best_result.similarity if best_result else 0):
            best_result = result

        # No match found - but return the best similarity we found
        # This is CRITICAL for progressive confidence communication
        self._stats.no_matches += 1
        match_time_ms = (time.time() - start_time) * 1000

        if best_result and best_result.similarity > 0:
            # Return the best partial match with updated timing
            best_result.match_time_ms = match_time_ms
            return best_result

        # Truly no matches found
        return MatchResult(
            matched=False,
            match_type="none",
            match_time_ms=match_time_ms,
        )

    # NOTE: _normalize_embedding is defined earlier in this class (line ~891)
    # with comprehensive NaN/Inf handling and torch tensor conversion.
    # Do NOT redefine it here - Python would override the robust version.

    def _compute_similarity(
        self,
        a: np.ndarray,
        b: np.ndarray
    ) -> float:
        """Compute cosine similarity between two embeddings"""
        if a is None or b is None:
            return 0.0
        # Handle NaN in either array
        if np.any(np.isnan(a)) or np.any(np.isnan(b)):
            return 0.0
        # Assume already normalized
        return float(np.dot(a, b))

    def _check_session_cache(
        self,
        embedding: np.ndarray
    ) -> MatchResult:
        """Check session cache for recent matches"""
        now = datetime.now()

        for speaker_name, (cached_emb, cached_time) in list(
            self._session_cache.items()
        ):
            # Check if expired
            age = (now - cached_time).total_seconds()
            if age > CacheConfig.SESSION_TTL_SECONDS:
                del self._session_cache[speaker_name]
                continue

            similarity = self._compute_similarity(embedding, cached_emb)

            if similarity >= CacheConfig.INSTANT_MATCH_THRESHOLD:
                return MatchResult(
                    matched=True,
                    speaker_name=speaker_name,
                    similarity=similarity,
                    match_type="instant",
                    profile_source="session_cache",
                )

        return MatchResult(matched=False)

    def _check_preloaded_profiles(
        self,
        embedding: np.ndarray,
        speaker_hint: Optional[str] = None,
    ) -> MatchResult:
        """Check preloaded profiles for match"""
        best_match = None
        best_similarity = 0.0

        # If we have a hint, check that first
        profiles_to_check = []
        if speaker_hint and speaker_hint in self._preloaded_profiles:
            profiles_to_check.append(
                (speaker_hint, self._preloaded_profiles[speaker_hint])
            )

        # Add all other profiles
        for name, profile in self._preloaded_profiles.items():
            if name != speaker_hint:
                profiles_to_check.append((name, profile))

        for speaker_name, profile in profiles_to_check:
            if not profile.is_valid():
                continue

            # Normalize stored embedding
            stored_emb = self._normalize_embedding(profile.embedding)
            similarity = self._compute_similarity(embedding, stored_emb)

            if similarity > best_similarity:
                best_similarity = similarity
                best_match = speaker_name

        # Determine match type based on similarity
        if best_similarity >= CacheConfig.INSTANT_MATCH_THRESHOLD:
            return MatchResult(
                matched=True,
                speaker_name=best_match,
                similarity=best_similarity,
                match_type="instant",
                profile_source="preloaded",
            )
        elif best_similarity >= CacheConfig.STANDARD_MATCH_THRESHOLD:
            return MatchResult(
                matched=True,
                speaker_name=best_match,
                similarity=best_similarity,
                match_type="standard",
                profile_source="preloaded",
            )
        elif best_similarity >= CacheConfig.LEARNING_THRESHOLD:
            return MatchResult(
                matched=False,  # Don't authenticate, but record for learning
                speaker_name=best_match,
                similarity=best_similarity,
                match_type="learning",
                profile_source="preloaded",
            )

        # Below learning threshold - return similarity for debugging/logging
        return MatchResult(
            matched=False,
            speaker_name=best_match,
            similarity=best_similarity,
            match_type="none",
            profile_source="preloaded",
        )

    async def _check_database_profiles(
        self,
        embedding: np.ndarray
    ) -> MatchResult:
        """Fall back to database query for profiles"""
        # This is the slow path - only used if no preloaded profile matches
        # TODO: Implement if needed for multi-user support
        return MatchResult(matched=False, profile_source="database")

    def _update_session_cache(
        self,
        speaker_name: str,
        embedding: np.ndarray,
    ):
        """Update session cache with verified embedding"""
        self._session_cache[speaker_name] = (
            self._normalize_embedding(embedding),
            datetime.now()
        )

    def _update_match_stats(self, result: MatchResult):
        """Update statistics based on match result"""
        if result.match_type == "instant":
            self._stats.instant_matches += 1
            # Estimate time saved (vs full verification)
            self._stats.total_time_saved_ms += 2000  # ~2s for full verify
        elif result.match_type == "standard":
            self._stats.standard_matches += 1
            self._stats.total_time_saved_ms += 1000  # ~1s saved
        elif result.match_type == "learning":
            self._stats.learning_matches += 1

        # Update average match time
        n = self._stats.total_lookups
        old_avg = self._stats.avg_match_time_ms
        self._stats.avg_match_time_ms = (
            (old_avg * (n - 1) + result.match_time_ms) / n
        )

    # =========================================================================
    # CONTINUOUS LEARNING
    # =========================================================================

    async def _record_sample_to_db(
        self,
        speaker_name: str,
        confidence: float,
        was_verified: bool,
        **kwargs
    ) -> Optional[int]:
        """
        Record voice sample to database for continuous learning.

        This is called by VoiceBiometricCache for ALL authentication
        attempts, enabling JARVIS to continuously improve recognition.

        Args:
            speaker_name: Identified speaker
            confidence: Verification confidence
            was_verified: Whether verification passed
            **kwargs: Additional metadata

        Returns:
            Sample ID if recorded successfully
        """
        self._stats.samples_recorded += 1

        try:
            if self._metrics_db is None:
                from voice_unlock.metrics_database import MetricsDatabase
                self._metrics_db = MetricsDatabase(self.db_path)

            # Record to voice_sample_log table
            result = await self._metrics_db.record_voice_sample(
                speaker_name=speaker_name,
                confidence=confidence,
                was_verified=was_verified,
                **kwargs
            )
            return result

        except Exception as e:
            logger.debug(f"Failed to record voice sample: {e}")
            return None

    async def update_voice_profile(
        self,
        speaker_name: str,
        new_embedding: np.ndarray,
        confidence: float,
    ) -> bool:
        """
        Update a voice profile with a new embedding.

        Uses exponential moving average to smoothly incorporate
        new voice samples while preserving stability.

        Args:
            speaker_name: Speaker to update
            new_embedding: New embedding to incorporate
            confidence: Confidence of the new sample

        Returns:
            True if profile was updated
        """
        if speaker_name not in self._preloaded_profiles:
            logger.warning(f"Cannot update unknown profile: {speaker_name}")
            return False

        profile = self._preloaded_profiles[speaker_name]

        # Only update with high-confidence samples
        if confidence < CacheConfig.STANDARD_MATCH_THRESHOLD:
            logger.debug(
                f"Skipping low-confidence update for {speaker_name}: "
                f"{confidence:.2%}"
            )
            return False

        # Exponential moving average update
        alpha = 0.1  # 10% weight for new sample
        if confidence >= CacheConfig.INSTANT_MATCH_THRESHOLD:
            alpha = 0.05  # Less aggressive for very high confidence

        # Normalize new embedding
        new_embedding = self._normalize_embedding(new_embedding)

        # Update embedding
        profile.embedding = (
            (1 - alpha) * profile.embedding + alpha * new_embedding
        )
        profile.embedding = self._normalize_embedding(profile.embedding)

        # Update metadata
        profile.total_samples += 1
        profile.avg_confidence = (
            (profile.avg_confidence * (profile.total_samples - 1) +
             confidence) / profile.total_samples
        )
        profile.last_verified = datetime.now()

        self._stats.embedding_updates += 1

        logger.info(
            f"Updated voice profile: {speaker_name} "
            f"(samples={profile.total_samples}, "
            f"avg_conf={profile.avg_confidence:.2%})"
        )

        # Schedule background save to database
        asyncio.create_task(
            self._save_profile_to_db(speaker_name, profile)
        )

        return True

    async def _save_profile_to_db(
        self,
        speaker_name: str,
        profile: VoiceProfile,
    ):
        """Save updated profile to database"""
        try:
            if self._metrics_db is None:
                return

            # Encode embedding as base64
            embedding_bytes = profile.embedding.astype(np.float32).tobytes()
            embedding_b64 = base64.b64encode(embedding_bytes).decode('utf-8')

            conn = self._metrics_db._get_connection()
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE voice_embeddings
                SET
                    embedding_b64 = ?,
                    total_samples_used = ?,
                    avg_sample_confidence = ?,
                    updated_at = ?
                WHERE speaker_name = ?
            """, (
                embedding_b64,
                profile.total_samples,
                profile.avg_confidence,
                datetime.now().isoformat(),
                speaker_name,
            ))

            conn.commit()
            logger.debug(f"Saved profile update for {speaker_name}")

        except Exception as e:
            logger.warning(f"Failed to save profile {speaker_name}: {e}")

    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    def get_preloaded_profiles(self) -> Dict[str, VoiceProfile]:
        """Get all preloaded profiles"""
        return self._preloaded_profiles.copy()

    def get_profile(self, speaker_name: str) -> Optional[VoiceProfile]:
        """Get a specific profile"""
        return self._preloaded_profiles.get(speaker_name)

    def clear_session_cache(self):
        """Clear session cache"""
        self._session_cache.clear()
        logger.info("Session cache cleared")

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        return self._stats.to_dict()

    async def shutdown(self):
        """Shutdown the cache manager"""
        # Cancel background tasks
        for task in self._background_tasks:
            if not task.done():
                task.cancel()

        # Clear caches
        self._preloaded_profiles.clear()
        self._session_cache.clear()

        self._state = CacheState.UNINITIALIZED
        logger.info("UnifiedVoiceCacheManager shutdown")


# =============================================================================
# GLOBAL SINGLETON
# =============================================================================
_cache_manager: Optional[UnifiedVoiceCacheManager] = None
_cache_lock = threading.Lock()


def get_unified_cache_manager() -> UnifiedVoiceCacheManager:
    """
    Get the global unified voice cache manager instance.

    This ensures all components share the same preloaded profiles
    and session cache for maximum performance.
    """
    global _cache_manager

    if _cache_manager is None:
        with _cache_lock:
            if _cache_manager is None:
                _cache_manager = UnifiedVoiceCacheManager()
                logger.info("Global UnifiedVoiceCacheManager created")

    return _cache_manager


async def initialize_unified_cache(
    preload_profiles: bool = True,
    preload_models: bool = True,
) -> bool:
    """
    Initialize the global unified cache.

    Call this at system startup to preload Derek's voice profile
    for instant recognition.

    Args:
        preload_profiles: Load voice profiles from database
        preload_models: Prewarm ML models

    Returns:
        True if initialization successful
    """
    manager = get_unified_cache_manager()
    return await manager.initialize(
        preload_profiles=preload_profiles,
        preload_models=preload_models,
    )


# =============================================================================
# ASYNC GETTER WITH AUTO-INITIALIZATION
# =============================================================================
_init_lock = asyncio.Lock()
_initialized = False


async def get_unified_voice_cache() -> UnifiedVoiceCacheManager:
    """
    Get the unified voice cache with automatic initialization.

    This is the RECOMMENDED way to access the unified voice cache because:
    1. It ensures the cache is properly initialized before use
    2. It preloads voice profiles from database (critical for recognition!)
    3. It's async-safe with proper locking
    4. It handles errors gracefully

    Usage:
        cache = await get_unified_voice_cache()
        result = await cache.verify_voice_from_audio(audio_data)

    Returns:
        Initialized UnifiedVoiceCacheManager instance
    """
    global _initialized

    manager = get_unified_cache_manager()

    # Check if already initialized (fast path)
    if _initialized and manager.state == CacheState.READY:
        return manager

    # Async-safe initialization
    async with _init_lock:
        # Double-check after acquiring lock
        if _initialized and manager.state == CacheState.READY:
            return manager

        # Initialize if needed
        if manager.state in [CacheState.UNINITIALIZED, CacheState.ERROR]:
            logger.info("🔄 Auto-initializing unified voice cache...")

            try:
                success = await asyncio.wait_for(
                    manager.initialize(
                        preload_profiles=True,
                        preload_models=True,
                    ),
                    timeout=15.0  # Allow time for model loading
                )

                if success:
                    _initialized = True
                    logger.info(
                        f"✅ Unified voice cache initialized: "
                        f"{manager.profiles_loaded} profiles preloaded"
                    )
                else:
                    logger.warning("⚠️ Unified voice cache initialization returned False")

            except asyncio.TimeoutError:
                logger.error("⏱️ Unified voice cache initialization timed out")
            except Exception as e:
                logger.error(f"❌ Unified voice cache initialization failed: {e}")

    return manager


def reset_unified_cache():
    """Reset the global cache (for testing)"""
    global _cache_manager, _initialized

    with _cache_lock:
        if _cache_manager:
            asyncio.create_task(_cache_manager.shutdown())
        _cache_manager = None
        _initialized = False
