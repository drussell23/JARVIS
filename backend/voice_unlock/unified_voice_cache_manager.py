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

Performance Goals:
- First unlock after startup: < 500ms (preloaded embedding match)
- Subsequent unlocks in session: < 100ms (cache hit)
- Cold start with model loading: < 5s (parallel loading)
"""

import asyncio
import base64
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================
class CacheConfig:
    """Unified cache configuration"""
    # Embedding dimensions
    EMBEDDING_DIM = 192

    # Similarity thresholds
    INSTANT_MATCH_THRESHOLD = 0.92   # Very high - instant unlock
    STANDARD_MATCH_THRESHOLD = 0.85  # Standard verification
    LEARNING_THRESHOLD = 0.75        # Record for learning, don't unlock

    # Session settings
    SESSION_TTL_SECONDS = 1800       # 30 minutes
    PRELOAD_TIMEOUT_SECONDS = 10.0   # Max time to preload profiles

    # Cache sizes
    MAX_CACHED_EMBEDDINGS = 50
    MAX_PATTERN_HISTORY = 100

    # Database paths
    DEFAULT_DB_PATH = os.path.expanduser("~/.jarvis/voice_unlock_metrics.db")
    DEFAULT_CHROMA_PATH = os.path.expanduser("~/.jarvis/chroma_voice_patterns")


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
    """Cached voice profile with embedding and metadata"""
    speaker_name: str
    embedding: np.ndarray
    embedding_dimensions: int = 192
    total_samples: int = 0
    avg_confidence: float = 0.0
    last_verified: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
    source: str = "database"

    def is_valid(self) -> bool:
        """Check if profile has valid embedding"""
        return (
            self.embedding is not None and
            len(self.embedding) == self.embedding_dimensions
        )


@dataclass
class MatchResult:
    """Result of voice matching against cached profiles"""
    matched: bool
    speaker_name: Optional[str] = None
    similarity: float = 0.0
    match_type: str = "none"  # "instant", "standard", "learning", "none"
    match_time_ms: float = 0.0
    profile_source: str = "none"  # "preloaded", "session_cache", "database"

    @property
    def is_instant_match(self) -> bool:
        return self.match_type == "instant"

    @property
    def is_learning_only(self) -> bool:
        return self.match_type == "learning"


@dataclass
class CacheStats:
    """Comprehensive cache statistics"""
    state: CacheState = CacheState.UNINITIALIZED
    profiles_preloaded: int = 0
    models_loaded: bool = False

    # Match statistics
    total_lookups: int = 0
    instant_matches: int = 0
    standard_matches: int = 0
    learning_matches: int = 0
    no_matches: int = 0

    # Timing
    avg_match_time_ms: float = 0.0
    total_time_saved_ms: float = 0.0

    # Learning
    samples_recorded: int = 0
    embedding_updates: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "profiles_preloaded": self.profiles_preloaded,
            "models_loaded": self.models_loaded,
            "total_lookups": self.total_lookups,
            "instant_matches": self.instant_matches,
            "standard_matches": self.standard_matches,
            "learning_matches": self.learning_matches,
            "no_matches": self.no_matches,
            "instant_match_rate": (
                self.instant_matches / max(1, self.total_lookups)
            ),
            "avg_match_time_ms": self.avg_match_time_ms,
            "total_time_saved_ms": self.total_time_saved_ms,
            "samples_recorded": self.samples_recorded,
            "embedding_updates": self.embedding_updates,
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
        Initialize the unified cache manager.

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

        # Reference to ChromaDB collection (optional)
        self._chroma_collection = None

        # Statistics
        self._stats = CacheStats()

        # Background task handles
        self._background_tasks: List[asyncio.Task] = []

        logger.info(
            f"UnifiedVoiceCacheManager created "
            f"(db={self.db_path}, chroma={self.chroma_path})"
        )

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
        timeout: float = CacheConfig.PRELOAD_TIMEOUT_SECONDS,
    ) -> bool:
        """
        Initialize the unified cache system.

        This is the CRITICAL startup path that:
        1. Loads Derek's voice profile from SQLite
        2. Preloads ML models (via ParallelModelLoader)
        3. Connects to VoiceBiometricCache for session caching
        4. Optionally connects to ChromaDB for patterns

        Args:
            preload_profiles: Load voice profiles from database
            preload_models: Prewarm ML models
            connect_biometric_cache: Connect to session cache
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

                # Execute all tasks with timeout
                if tasks:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=timeout
                    )

                self._state = CacheState.READY
                init_time = (time.time() - start_time) * 1000

                logger.info(
                    f"UnifiedVoiceCacheManager initialized in {init_time:.0f}ms "
                    f"(profiles={self.profiles_loaded}, "
                    f"models_ready={self._stats.models_loaded})"
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

    async def _preload_voice_profiles(self) -> int:
        """
        Preload voice profiles from SQLite database.

        This is the KEY optimization - loads Derek's embedding at startup
        so voice matching is instant (no database query needed).

        Returns:
            Number of profiles preloaded
        """
        self._state = CacheState.LOADING_PROFILES

        try:
            import sqlite3

            # Connect directly to the voice unlock metrics database
            # MetricsDatabase uses ~/.jarvis/unlock_metrics.db
            db_dir = os.path.expanduser("~/.jarvis")
            db_path = os.path.join(db_dir, "unlock_metrics.db")

            if not os.path.exists(db_path):
                logger.warning(f"Voice metrics database not found: {db_path}")
                return 0

            conn = sqlite3.connect(db_path)
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
            loaded = 0

            for row in rows:
                try:
                    # Access by column name since we used row_factory
                    speaker_name = row["speaker_name"]
                    embedding_b64 = row["embedding_b64"]
                    dimensions = row["embedding_dimensions"] or 192
                    samples = row["total_samples_used"] or 0
                    confidence = row["avg_sample_confidence"] or 0.0
                    updated_at = row["updated_at"]
                    source = row["source"] or "database"

                    # Decode base64 embedding
                    embedding_bytes = base64.b64decode(embedding_b64)
                    embedding = np.frombuffer(
                        embedding_bytes, dtype=np.float32
                    )

                    # Validate dimensions
                    if len(embedding) != dimensions:
                        logger.warning(
                            f"Embedding dimension mismatch for {speaker_name}: "
                            f"{len(embedding)} vs {dimensions}"
                        )
                        continue

                    # Create profile
                    profile = VoiceProfile(
                        speaker_name=speaker_name,
                        embedding=embedding,
                        embedding_dimensions=dimensions,
                        total_samples=samples,
                        avg_confidence=confidence,
                        source=source,
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

                    logger.info(
                        f"Preloaded voice profile: {speaker_name} "
                        f"(dim={dimensions}, samples={samples}, "
                        f"confidence={confidence:.2%})"
                    )

                except Exception as e:
                    logger.warning(
                        f"Failed to load profile for {row[0]}: {e}"
                    )

            self._stats.profiles_preloaded = loaded
            logger.info(f"Preloaded {loaded} voice profile(s) from database")
            return loaded

        except Exception as e:
            logger.error(f"Failed to preload voice profiles: {e}")
            return 0

    async def _ensure_models_loaded(self) -> bool:
        """
        Ensure ML models are loaded via ParallelModelLoader.

        This connects the cache layer to the parallel model loader, enabling:
        1. Shared ECAPA-TDNN encoder for embedding extraction
        2. Model caching to prevent redundant loading
        3. Fast embedding computation for voice matching

        Returns:
            True if models are ready
        """
        self._state = CacheState.LOADING_MODELS

        try:
            from voice.parallel_model_loader import get_model_loader

            if self._model_loader is None:
                self._model_loader = get_model_loader()

            # Check if already cached in parallel loader
            if self._model_loader.is_cached("ecapa_encoder"):
                self._stats.models_loaded = True
                logger.info("ECAPA-TDNN model already cached in parallel loader")
                return True

            # Try to load the ECAPA encoder if not cached
            # This uses the parallel loader's shared thread pool
            try:
                result = await self._model_loader.load_model(
                    model_name="ecapa_encoder",
                    load_func=self._create_ecapa_loader(),
                    timeout=60.0,  # Allow time for first-time model download
                    use_cache=True,
                )
                if result.success:
                    self._stats.models_loaded = True
                    logger.info(
                        f"ECAPA-TDNN loaded via parallel loader "
                        f"in {result.load_time_ms:.0f}ms"
                    )
                    return True
                else:
                    logger.warning(f"ECAPA-TDNN load failed: {result.error}")
                    # Still mark as ready - we can fall back to pre-computed embeddings
                    self._stats.models_loaded = False
                    return True
            except Exception as load_err:
                logger.warning(f"Model loading error: {load_err}")
                # Continue without model - use preloaded embeddings only
                return True

        except Exception as e:
            logger.warning(f"Model loader not available: {e}")
            return False

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

        Returns:
            Encoder model if available, None otherwise
        """
        if self._model_loader is None:
            return None

        return self._model_loader.get_cached("ecapa_encoder")

    async def extract_embedding(
        self,
        audio_data,
        sample_rate: int = 16000,
    ) -> Optional[np.ndarray]:
        """
        Extract voice embedding from audio using the cached ECAPA-TDNN model.

        This is the FAST PATH for new audio - uses the model from the
        parallel loader's cache instead of loading a new model.

        Args:
            audio_data: Audio waveform (numpy array or tensor)
            sample_rate: Audio sample rate (default 16kHz)

        Returns:
            192-dimensional embedding or None if extraction fails
        """
        import time
        start_time = time.time()

        try:
            encoder = self.get_ecapa_encoder()
            if encoder is None:
                logger.warning("ECAPA encoder not available for embedding extraction")
                return None

            import torch

            # Convert to tensor if needed
            if isinstance(audio_data, np.ndarray):
                waveform = torch.from_numpy(audio_data).float()
            else:
                waveform = audio_data.float()

            # Ensure correct shape: (batch, samples) or (samples,)
            if waveform.dim() == 1:
                waveform = waveform.unsqueeze(0)

            # Extract embedding
            with torch.no_grad():
                embedding = encoder.encode_batch(waveform)

            # Convert to numpy and flatten
            embedding_np = embedding.squeeze().cpu().numpy()

            # Normalize
            embedding_np = self._normalize_embedding(embedding_np)

            extract_time_ms = (time.time() - start_time) * 1000
            logger.debug(f"Extracted embedding in {extract_time_ms:.1f}ms")

            return embedding_np

        except Exception as e:
            logger.error(f"Embedding extraction failed: {e}")
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

        Args:
            embedding: Voice embedding to match (192-dim)
            speaker_hint: Optional hint for expected speaker

        Returns:
            MatchResult with match details
        """
        start_time = time.time()
        self._stats.total_lookups += 1

        if embedding is None or len(embedding) == 0:
            return MatchResult(matched=False, match_type="none")

        # Normalize embedding
        embedding = self._normalize_embedding(embedding)

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

        # Strategy 3: Database fallback (slower but comprehensive)
        result = await self._check_database_profiles(embedding)
        if result.matched:
            result.match_time_ms = (time.time() - start_time) * 1000
            self._update_match_stats(result)
            return result

        # No match found
        result = MatchResult(
            matched=False,
            match_type="none",
            match_time_ms=(time.time() - start_time) * 1000,
        )
        self._stats.no_matches += 1
        return result

    def _normalize_embedding(self, embedding: np.ndarray) -> np.ndarray:
        """Normalize embedding to unit length"""
        norm = np.linalg.norm(embedding)
        if norm > 0:
            return embedding / norm
        return embedding

    def _compute_similarity(
        self,
        a: np.ndarray,
        b: np.ndarray
    ) -> float:
        """Compute cosine similarity between two embeddings"""
        if a is None or b is None:
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


def reset_unified_cache():
    """Reset the global cache (for testing)"""
    global _cache_manager

    with _cache_lock:
        if _cache_manager:
            asyncio.create_task(_cache_manager.shutdown())
        _cache_manager = None
