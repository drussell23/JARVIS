"""
Voice Biometric Semantic Cache with Continuous Learning
========================================================

Intelligent caching for voice biometric authentication to enable instant
unlock responses for repeated "unlock my screen" requests.

Architecture:
- L1: Voice Embedding Cache (voiceprint similarity matching)
- L2: Command Semantic Cache (unlock phrase variations)
- L3: Session Authentication Cache (time-windowed auth tokens)
- L4: Database Recording Layer (continuous voice learning)

Performance Goals:
- First unlock: Full biometric verification (2-5 seconds)
- Subsequent unlocks within session: < 100ms (cache hit)
- Voice similarity threshold: 0.90 for cache hit

Security:
- Session expires after configurable timeout (default: 30 minutes)
- Invalidated on screen lock events
- Voice embedding must match within similarity threshold

Continuous Learning:
- ALL authentication attempts (including cache hits) are recorded to SQLite
- This enables JARVIS to continuously improve voice recognition
- Cache provides SPEED, Database provides LEARNING
- Fire-and-forget async recording to avoid latency impact
"""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple
from weakref import WeakValueDictionary

import numpy as np

logger = logging.getLogger(__name__)


# =============================================================================
# DATABASE RECORDING CALLBACK TYPES
# =============================================================================
# Callback signature for recording voice samples to database
# async def callback(speaker_name, confidence, was_verified, **kwargs) -> Optional[int]
VoiceSampleRecorderCallback = Callable[..., "asyncio.coroutine"]


# =============================================================================
# CONFIGURATION CONSTANTS
# =============================================================================
VOICE_EMBEDDING_CACHE_SIZE_MB = 50
VOICE_EMBEDDING_TTL_SECONDS = 1800  # 30 minutes
VOICE_SIMILARITY_THRESHOLD = 0.90  # Must be very similar for security
COMMAND_CACHE_TTL_SECONDS = 300  # 5 minutes for command phrases
SESSION_AUTH_TTL_SECONDS = 1800  # 30 minutes session validity
MAX_CACHE_ENTRIES = 100


class CacheHitType(Enum):
    """Types of cache hits"""
    MISS = "miss"
    VOICE_EMBEDDING = "voice_embedding"
    COMMAND_SEMANTIC = "command_semantic"
    SESSION_AUTH = "session_auth"


@dataclass
class VoiceCacheEntry:
    """Cached voice authentication result"""
    speaker_name: str
    voice_embedding: Optional[np.ndarray]
    verification_confidence: float
    authentication_time: datetime
    session_id: str
    is_owner: bool
    ttl_seconds: int
    access_count: int = 0
    last_access: datetime = field(default_factory=datetime.now)

    def is_expired(self) -> bool:
        """Check if cache entry has expired"""
        return (datetime.now() - self.authentication_time).total_seconds() > self.ttl_seconds

    def is_session_valid(self) -> bool:
        """Check if session is still valid"""
        return not self.is_expired() and self.is_owner

    def access(self):
        """Update access statistics"""
        self.last_access = datetime.now()
        self.access_count += 1


@dataclass
class CommandCacheEntry:
    """Cached command semantic mapping"""
    original_text: str
    normalized_command: str
    is_unlock_command: bool
    confidence: float
    created_at: datetime
    ttl_seconds: int = COMMAND_CACHE_TTL_SECONDS

    def is_expired(self) -> bool:
        return (datetime.now() - self.created_at).total_seconds() > self.ttl_seconds


@dataclass
class BiometricCacheResult:
    """Result from biometric cache lookup"""
    hit_type: CacheHitType
    speaker_name: Optional[str] = None
    verification_confidence: float = 0.0
    is_owner: bool = False
    session_id: Optional[str] = None
    cache_age_seconds: float = 0.0
    similarity_score: float = 0.0


class VoiceBiometricCache:
    """
    Intelligent voice biometric cache for instant unlock authentication
    with continuous learning via database recording.

    Caches verified voice authentications so repeated "unlock my screen"
    requests within a session window are instant (< 100ms vs 2-5 seconds).

    IMPORTANT: All authentication attempts (including cache hits) are recorded
    to the SQLite database for continuous voice learning. The cache provides
    SPEED while the database provides LEARNING.
    """

    def __init__(
        self,
        embedding_ttl: int = VOICE_EMBEDDING_TTL_SECONDS,
        similarity_threshold: float = VOICE_SIMILARITY_THRESHOLD,
        max_entries: int = MAX_CACHE_ENTRIES,
        voice_sample_recorder: Optional[VoiceSampleRecorderCallback] = None,
    ):
        """
        Initialize voice biometric cache with optional database recording.

        Args:
            embedding_ttl: Time-to-live for cached voice embeddings (seconds)
            similarity_threshold: Cosine similarity threshold for cache hit
            max_entries: Maximum number of cache entries
            voice_sample_recorder: Async callback to record voice samples to DB.
                                   If provided, all auth attempts are recorded
                                   for continuous voice learning.
        """
        self.embedding_ttl = embedding_ttl
        self.similarity_threshold = similarity_threshold
        self.max_entries = max_entries

        # Voice embedding cache (primary)
        self._voice_cache: OrderedDict[str, VoiceCacheEntry] = OrderedDict()

        # Command semantic cache (unlock phrase variations)
        self._command_cache: OrderedDict[str, CommandCacheEntry] = OrderedDict()

        # Session authentication cache
        self._session_cache: Dict[str, VoiceCacheEntry] = {}

        # Current active session
        self._active_session_id: Optional[str] = None
        self._active_speaker: Optional[str] = None

        # 🎯 DATABASE RECORDING: Callback for continuous voice learning
        # This records ALL authentication attempts (cache hits too!) to SQLite
        self._voice_sample_recorder = voice_sample_recorder

        # Track background recording tasks for cleanup
        self._recording_tasks: List[asyncio.Task] = []

        # Statistics (extended with DB recording stats)
        self._stats = {
            "total_lookups": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "voice_embedding_hits": 0,
            "command_semantic_hits": 0,
            "session_auth_hits": 0,
            "avg_lookup_time_ms": 0.0,
            "total_time_saved_ms": 0.0,
            # Database recording stats
            "db_recordings_attempted": 0,
            "db_recordings_successful": 0,
            "db_recordings_failed": 0,
            "cache_hits_recorded_to_db": 0,
        }

        # Lock for thread safety
        self._lock = asyncio.Lock()

        recorder_status = "WITH DB recording" if voice_sample_recorder else "NO DB recording"
        logger.info(
            f"🔐 VoiceBiometricCache initialized ({recorder_status}): "
            f"TTL={embedding_ttl}s, similarity_threshold={similarity_threshold}"
        )

    def set_voice_sample_recorder(self, recorder: VoiceSampleRecorderCallback):
        """
        Set the database recorder callback for continuous voice learning.

        This allows lazy initialization - you can create the cache first,
        then set the recorder once MetricsDatabase is available.

        Args:
            recorder: Async callback to record voice samples to database.
                      Should match MetricsDatabase.record_voice_sample signature.
        """
        self._voice_sample_recorder = recorder
        logger.info("🎯 Voice sample recorder registered for continuous learning")

    def _generate_voice_key(self, embedding: np.ndarray) -> str:
        """Generate cache key from voice embedding"""
        # Use first 64 dimensions for quick hashing
        embedding_sample = embedding[:64] if len(embedding) > 64 else embedding
        hash_input = embedding_sample.tobytes()
        return hashlib.sha256(hash_input).hexdigest()[:32]

    def _generate_session_id(self, speaker_name: str) -> str:
        """Generate unique session ID"""
        timestamp = datetime.now().isoformat()
        return hashlib.sha256(f"{speaker_name}:{timestamp}".encode()).hexdigest()[:16]

    def _compute_cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Compute cosine similarity between two embeddings"""
        if a is None or b is None:
            return 0.0
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    async def _record_to_database_async(
        self,
        speaker_name: str,
        confidence: float,
        was_verified: bool,
        sample_source: str = "cache_hit",
        audio_quality: float = 0.8,  # Cache hits have good quality by definition
        threshold_used: float = VOICE_SIMILARITY_THRESHOLD,
        added_to_profile: bool = False,  # Cache hits don't add new samples
        rejection_reason: Optional[str] = None,
        cache_hit_type: Optional[CacheHitType] = None,
        similarity_score: float = 0.0,
    ):
        """
        Fire-and-forget database recording for continuous voice learning.

        This is called for EVERY authentication attempt (cache hit or miss)
        so JARVIS can continuously learn and improve voice recognition.

        Args:
            speaker_name: Identified speaker name
            confidence: Verification confidence score
            was_verified: Whether verification passed
            sample_source: Source of sample (cache_hit, cache_miss, etc.)
            audio_quality: Audio quality estimate (0-1)
            threshold_used: Threshold used for verification
            added_to_profile: Whether sample was added to voice profile
            rejection_reason: Why sample was rejected (if any)
            cache_hit_type: Type of cache hit (for analytics)
            similarity_score: Similarity score (for cache hits)
        """
        if not self._voice_sample_recorder:
            return  # No recorder configured

        self._stats["db_recordings_attempted"] += 1

        try:
            # Determine sample source based on cache hit type
            if cache_hit_type:
                if cache_hit_type == CacheHitType.SESSION_AUTH:
                    sample_source = "cache_hit_session"
                elif cache_hit_type == CacheHitType.VOICE_EMBEDDING:
                    sample_source = "cache_hit_embedding"
                elif cache_hit_type == CacheHitType.COMMAND_SEMANTIC:
                    sample_source = "cache_hit_semantic"
                else:
                    sample_source = "cache_miss"

            # Call the database recorder with timeout
            result = await asyncio.wait_for(
                self._voice_sample_recorder(
                    speaker_name=speaker_name,
                    confidence=confidence,
                    was_verified=was_verified,
                    audio_quality=audio_quality,
                    snr_db=18.0,  # Assume good SNR for cache hits
                    audio_duration_ms=2000,  # Typical duration
                    sample_source=sample_source,
                    environment_type="cached_session",
                    threshold_used=threshold_used,
                    added_to_profile=added_to_profile,
                    rejection_reason=rejection_reason,
                ),
                timeout=2.0  # Don't block more than 2 seconds
            )

            self._stats["db_recordings_successful"] += 1
            if cache_hit_type and cache_hit_type != CacheHitType.MISS:
                self._stats["cache_hits_recorded_to_db"] += 1

            logger.debug(
                f"📝 [CACHE] Recorded to DB: {speaker_name} ({sample_source}, "
                f"conf={confidence:.2%}, similarity={similarity_score:.2%})"
            )

        except asyncio.TimeoutError:
            self._stats["db_recordings_failed"] += 1
            logger.debug(f"⏱️ [CACHE] DB recording timed out for {speaker_name}")
        except Exception as e:
            self._stats["db_recordings_failed"] += 1
            logger.debug(f"⚠️ [CACHE] DB recording failed: {e}")

    def _schedule_db_recording(
        self,
        speaker_name: str,
        confidence: float,
        was_verified: bool,
        cache_hit_type: Optional[CacheHitType] = None,
        similarity_score: float = 0.0,
        **kwargs
    ):
        """
        Schedule a fire-and-forget database recording task.

        This does NOT block the cache lookup - recordings happen in background.
        Failed recordings are logged but don't affect cache performance.
        """
        if not self._voice_sample_recorder:
            return

        # Create background task
        task = asyncio.create_task(
            self._record_to_database_async(
                speaker_name=speaker_name,
                confidence=confidence,
                was_verified=was_verified,
                cache_hit_type=cache_hit_type,
                similarity_score=similarity_score,
                **kwargs
            )
        )

        # Track task for cleanup
        self._recording_tasks.append(task)

        # Cleanup completed tasks periodically
        self._recording_tasks = [t for t in self._recording_tasks if not t.done()]

    async def lookup_voice_authentication(
        self,
        voice_embedding: Optional[np.ndarray] = None,
        transcribed_text: Optional[str] = None,
    ) -> BiometricCacheResult:
        """
        Look up cached voice authentication.

        Returns cached authentication if:
        1. Voice embedding matches cached embedding with high similarity
        2. Session is still valid (within TTL window)
        3. Speaker is verified owner

        IMPORTANT: All lookups (hits AND misses) are recorded to the database
        for continuous voice learning. This happens in background (fire-and-forget).

        Args:
            voice_embedding: Current voice embedding to match
            transcribed_text: Transcribed command text

        Returns:
            BiometricCacheResult with hit type and cached data
        """
        start_time = time.time()
        self._stats["total_lookups"] += 1

        async with self._lock:
            result = BiometricCacheResult(hit_type=CacheHitType.MISS)

            # Strategy 1: Check active session first (fastest path)
            if self._active_session_id and self._active_speaker:
                session_entry = self._session_cache.get(self._active_session_id)
                if session_entry and session_entry.is_session_valid():
                    # Session is valid, but verify voice is similar enough
                    if voice_embedding is not None and session_entry.voice_embedding is not None:
                        similarity = self._compute_cosine_similarity(
                            voice_embedding, session_entry.voice_embedding
                        )
                        if similarity >= self.similarity_threshold:
                            session_entry.access()
                            result = BiometricCacheResult(
                                hit_type=CacheHitType.SESSION_AUTH,
                                speaker_name=session_entry.speaker_name,
                                verification_confidence=session_entry.verification_confidence,
                                is_owner=session_entry.is_owner,
                                session_id=session_entry.session_id,
                                cache_age_seconds=(datetime.now() - session_entry.authentication_time).total_seconds(),
                                similarity_score=similarity,
                            )
                            self._stats["cache_hits"] += 1
                            self._stats["session_auth_hits"] += 1
                            self._update_timing_stats(start_time)

                            # 🎯 CONTINUOUS LEARNING: Record cache hit to database
                            self._schedule_db_recording(
                                speaker_name=result.speaker_name,
                                confidence=result.verification_confidence,
                                was_verified=True,
                                cache_hit_type=CacheHitType.SESSION_AUTH,
                                similarity_score=similarity,
                            )

                            return result

            # Strategy 2: Voice embedding similarity search
            if voice_embedding is not None:
                best_match: Optional[VoiceCacheEntry] = None
                best_similarity = 0.0

                for entry in self._voice_cache.values():
                    if entry.is_expired():
                        continue
                    if entry.voice_embedding is None:
                        continue

                    similarity = self._compute_cosine_similarity(
                        voice_embedding, entry.voice_embedding
                    )
                    if similarity > best_similarity and similarity >= self.similarity_threshold:
                        best_similarity = similarity
                        best_match = entry

                if best_match:
                    best_match.access()
                    # Move to end (LRU)
                    key = self._generate_voice_key(best_match.voice_embedding)
                    if key in self._voice_cache:
                        self._voice_cache.move_to_end(key)

                    result = BiometricCacheResult(
                        hit_type=CacheHitType.VOICE_EMBEDDING,
                        speaker_name=best_match.speaker_name,
                        verification_confidence=best_match.verification_confidence,
                        is_owner=best_match.is_owner,
                        session_id=best_match.session_id,
                        cache_age_seconds=(datetime.now() - best_match.authentication_time).total_seconds(),
                        similarity_score=best_similarity,
                    )
                    self._stats["cache_hits"] += 1
                    self._stats["voice_embedding_hits"] += 1
                    self._update_timing_stats(start_time)

                    # 🎯 CONTINUOUS LEARNING: Record cache hit to database
                    self._schedule_db_recording(
                        speaker_name=result.speaker_name,
                        confidence=result.verification_confidence,
                        was_verified=True,
                        cache_hit_type=CacheHitType.VOICE_EMBEDDING,
                        similarity_score=best_similarity,
                    )

                    return result

            # Cache miss - still record for learning (unknown speaker or no match)
            self._stats["cache_misses"] += 1
            self._update_timing_stats(start_time)

            # 🎯 CONTINUOUS LEARNING: Record cache miss to database
            # This helps JARVIS learn about failed attempts too
            self._schedule_db_recording(
                speaker_name="unknown",  # Will be identified by full verification
                confidence=0.0,
                was_verified=False,
                cache_hit_type=CacheHitType.MISS,
                similarity_score=0.0,
            )

            return result

    async def cache_authentication(
        self,
        speaker_name: str,
        voice_embedding: Optional[np.ndarray],
        verification_confidence: float,
        is_owner: bool,
        transcribed_text: Optional[str] = None,
    ) -> str:
        """
        Cache a successful voice authentication.

        Args:
            speaker_name: Verified speaker name
            voice_embedding: Voice embedding used for verification
            verification_confidence: Confidence score from verification
            is_owner: Whether speaker is the device owner
            transcribed_text: Original transcribed command

        Returns:
            Session ID for this authentication
        """
        async with self._lock:
            session_id = self._generate_session_id(speaker_name)

            # Create cache entry
            entry = VoiceCacheEntry(
                speaker_name=speaker_name,
                voice_embedding=voice_embedding,
                verification_confidence=verification_confidence,
                authentication_time=datetime.now(),
                session_id=session_id,
                is_owner=is_owner,
                ttl_seconds=self.embedding_ttl,
            )

            # Add to voice embedding cache
            if voice_embedding is not None:
                voice_key = self._generate_voice_key(voice_embedding)

                # Evict if at max capacity
                while len(self._voice_cache) >= self.max_entries:
                    self._voice_cache.popitem(last=False)

                self._voice_cache[voice_key] = entry

            # Add to session cache
            self._session_cache[session_id] = entry

            # Update active session
            if is_owner:
                self._active_session_id = session_id
                self._active_speaker = speaker_name

            # Cache command if provided
            if transcribed_text:
                await self._cache_command(transcribed_text, is_unlock=True)

            logger.info(
                f"🔐 Cached voice auth: speaker={speaker_name}, "
                f"confidence={verification_confidence:.2%}, session={session_id[:8]}..."
            )

            return session_id

    async def _cache_command(self, text: str, is_unlock: bool):
        """Cache command text for semantic matching"""
        normalized = text.lower().strip()
        entry = CommandCacheEntry(
            original_text=text,
            normalized_command=normalized,
            is_unlock_command=is_unlock,
            confidence=1.0,
            created_at=datetime.now(),
        )

        # Use normalized text as key
        key = hashlib.md5(normalized.encode()).hexdigest()[:16]

        # Evict if at max capacity
        while len(self._command_cache) >= self.max_entries:
            self._command_cache.popitem(last=False)

        self._command_cache[key] = entry

    async def is_cached_unlock_command(self, text: str) -> Tuple[bool, float]:
        """
        Check if text is a cached unlock command.

        Uses semantic similarity to match unlock phrase variations like:
        - "unlock my screen"
        - "unlock the screen"
        - "unlock screen"
        - "jarvis unlock"

        Returns:
            Tuple of (is_unlock_command, confidence)
        """
        normalized = text.lower().strip()
        key = hashlib.md5(normalized.encode()).hexdigest()[:16]

        async with self._lock:
            # Exact match
            if key in self._command_cache:
                entry = self._command_cache[key]
                if not entry.is_expired():
                    return entry.is_unlock_command, entry.confidence

            # Semantic matching for unlock phrases
            unlock_phrases = [
                "unlock my screen", "unlock the screen", "unlock screen",
                "unlock", "jarvis unlock", "unlock it", "open screen",
                "unlock my mac", "unlock the mac", "unlock computer",
            ]

            for phrase in unlock_phrases:
                if phrase in normalized or normalized in phrase:
                    # Cache this variation
                    await self._cache_command(text, is_unlock=True)
                    return True, 0.9

            # Check for partial matches
            if any(word in normalized for word in ["unlock", "open"]):
                if any(word in normalized for word in ["screen", "mac", "computer"]):
                    await self._cache_command(text, is_unlock=True)
                    return True, 0.8

            return False, 0.0

    async def invalidate_session(self, session_id: Optional[str] = None):
        """
        Invalidate authentication session.

        Called when:
        - Screen is locked
        - User explicitly logs out
        - Security event detected
        """
        async with self._lock:
            if session_id:
                self._session_cache.pop(session_id, None)
                if self._active_session_id == session_id:
                    self._active_session_id = None
                    self._active_speaker = None
            else:
                # Invalidate all sessions
                self._session_cache.clear()
                self._active_session_id = None
                self._active_speaker = None

            logger.info("🔒 Voice authentication session invalidated")

    async def cleanup_expired(self):
        """Remove expired cache entries"""
        async with self._lock:
            # Clean voice cache
            expired_voice_keys = [
                k for k, v in self._voice_cache.items() if v.is_expired()
            ]
            for key in expired_voice_keys:
                self._voice_cache.pop(key, None)

            # Clean command cache
            expired_command_keys = [
                k for k, v in self._command_cache.items() if v.is_expired()
            ]
            for key in expired_command_keys:
                self._command_cache.pop(key, None)

            # Clean session cache
            expired_sessions = [
                k for k, v in self._session_cache.items() if v.is_expired()
            ]
            for session_id in expired_sessions:
                self._session_cache.pop(session_id, None)
                if self._active_session_id == session_id:
                    self._active_session_id = None
                    self._active_speaker = None

            if expired_voice_keys or expired_command_keys or expired_sessions:
                logger.debug(
                    f"🧹 Cleaned cache: {len(expired_voice_keys)} voice, "
                    f"{len(expired_command_keys)} commands, {len(expired_sessions)} sessions"
                )

    def _update_timing_stats(self, start_time: float):
        """Update timing statistics"""
        elapsed_ms = (time.time() - start_time) * 1000
        total_lookups = self._stats["total_lookups"]

        # Update rolling average
        if total_lookups > 1:
            prev_avg = self._stats["avg_lookup_time_ms"]
            self._stats["avg_lookup_time_ms"] = (
                prev_avg * (total_lookups - 1) + elapsed_ms
            ) / total_lookups
        else:
            self._stats["avg_lookup_time_ms"] = elapsed_ms

        # Estimate time saved (assume full verification takes 3000ms)
        if self._stats["cache_hits"] > 0:
            self._stats["total_time_saved_ms"] = self._stats["cache_hits"] * 3000 - (
                self._stats["cache_hits"] * self._stats["avg_lookup_time_ms"]
            )

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics including database recording stats"""
        total = self._stats["cache_hits"] + self._stats["cache_misses"]
        hit_rate = self._stats["cache_hits"] / total if total > 0 else 0.0

        # Calculate DB recording success rate
        db_total = self._stats["db_recordings_attempted"]
        db_success_rate = (
            self._stats["db_recordings_successful"] / db_total
            if db_total > 0 else 0.0
        )

        return {
            "total_lookups": self._stats["total_lookups"],
            "cache_hits": self._stats["cache_hits"],
            "cache_misses": self._stats["cache_misses"],
            "hit_rate": hit_rate,
            "voice_embedding_hits": self._stats["voice_embedding_hits"],
            "command_semantic_hits": self._stats["command_semantic_hits"],
            "session_auth_hits": self._stats["session_auth_hits"],
            "avg_lookup_time_ms": self._stats["avg_lookup_time_ms"],
            "total_time_saved_ms": self._stats["total_time_saved_ms"],
            "voice_cache_entries": len(self._voice_cache),
            "command_cache_entries": len(self._command_cache),
            "session_cache_entries": len(self._session_cache),
            "active_session": self._active_session_id is not None,
            "active_speaker": self._active_speaker,
            # Database recording stats for continuous learning
            "db_recording_enabled": self._voice_sample_recorder is not None,
            "db_recordings_attempted": self._stats["db_recordings_attempted"],
            "db_recordings_successful": self._stats["db_recordings_successful"],
            "db_recordings_failed": self._stats["db_recordings_failed"],
            "db_recording_success_rate": db_success_rate,
            "cache_hits_recorded_to_db": self._stats["cache_hits_recorded_to_db"],
            "pending_recording_tasks": len([t for t in self._recording_tasks if not t.done()]),
        }

    def get_active_session(self) -> Optional[Dict[str, Any]]:
        """Get current active session info"""
        if not self._active_session_id:
            return None

        entry = self._session_cache.get(self._active_session_id)
        if not entry or entry.is_expired():
            return None

        return {
            "session_id": entry.session_id,
            "speaker_name": entry.speaker_name,
            "is_owner": entry.is_owner,
            "verification_confidence": entry.verification_confidence,
            "authenticated_at": entry.authentication_time.isoformat(),
            "expires_in_seconds": entry.ttl_seconds - (datetime.now() - entry.authentication_time).total_seconds(),
            "access_count": entry.access_count,
        }


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================
_voice_biometric_cache: Optional[VoiceBiometricCache] = None


def get_voice_biometric_cache() -> VoiceBiometricCache:
    """Get global voice biometric cache instance"""
    global _voice_biometric_cache
    if _voice_biometric_cache is None:
        _voice_biometric_cache = VoiceBiometricCache()
    return _voice_biometric_cache


async def invalidate_all_voice_sessions():
    """Invalidate all voice authentication sessions (called on screen lock)"""
    cache = get_voice_biometric_cache()
    await cache.invalidate_session()


__all__ = [
    "VoiceBiometricCache",
    "BiometricCacheResult",
    "CacheHitType",
    "get_voice_biometric_cache",
    "invalidate_all_voice_sessions",
]
