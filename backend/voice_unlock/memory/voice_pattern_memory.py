"""
Voice Pattern Memory with ChromaDB

Enterprise-grade persistent storage for voice authentication patterns
using ChromaDB vector database.

Features:
- Five specialized collections for different pattern types
- Async-first design with batch operations
- Intelligent caching with LRU eviction
- Temporal queries and pattern matching
- Automatic collection management and cleanup
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from dataclasses import dataclass, field
from contextlib import asynccontextmanager

import numpy as np

try:
    import chromadb
    from chromadb.config import Settings
    from chromadb.api.models.Collection import Collection
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    chromadb = None
    Settings = None
    Collection = None

from .schemas import (
    VoiceMemoryConfig,
    VoiceMemoryRecord,
    VoiceEvolutionRecord,
    BehavioralPatternRecord,
    AttackPatternRecord,
    EnvironmentalProfileRecord,
    SpeechBiometricsRecord,
    AuthenticationEventRecord,
    MemoryQueryResult,
    DriftType,
    AttackType,
    EnvironmentType,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=VoiceMemoryRecord)


# =============================================================================
# COLLECTION DEFINITIONS
# =============================================================================

@dataclass
class CollectionDefinition:
    """Definition for a ChromaDB collection."""

    name: str
    description: str
    record_type: Type[VoiceMemoryRecord]
    embedding_field: str = "embedding"
    has_embedding: bool = True

    def get_full_name(self) -> str:
        """Get full collection name with prefix."""
        prefix = VoiceMemoryConfig.get_collection_prefix()
        return f"{prefix}{self.name}"


# Collection definitions
COLLECTIONS = {
    "voice_evolution": CollectionDefinition(
        name="voice_evolution",
        description="Track voice drift over time",
        record_type=VoiceEvolutionRecord,
        embedding_field="embedding",
        has_embedding=True,
    ),
    "behavioral_patterns": CollectionDefinition(
        name="behavioral_patterns",
        description="Unlock patterns (time, location, device)",
        record_type=BehavioralPatternRecord,
        embedding_field="",
        has_embedding=False,
    ),
    "attack_patterns": CollectionDefinition(
        name="attack_patterns",
        description="Failed spoofing signatures",
        record_type=AttackPatternRecord,
        embedding_field="audio_fingerprint",
        has_embedding=True,
    ),
    "environmental_profiles": CollectionDefinition(
        name="environmental_profiles",
        description="Voice per environment",
        record_type=EnvironmentalProfileRecord,
        embedding_field="adapted_embedding",
        has_embedding=True,
    ),
    "speech_biometrics": CollectionDefinition(
        name="speech_biometrics",
        description="Rhythm, cadence, breathing patterns",
        record_type=SpeechBiometricsRecord,
        embedding_field="speech_pattern_vector",
        has_embedding=True,
    ),
    "authentication_events": CollectionDefinition(
        name="authentication_events",
        description="Complete authentication event records",
        record_type=AuthenticationEventRecord,
        embedding_field="",
        has_embedding=False,
    ),
}


# =============================================================================
# ASYNC LRU CACHE
# =============================================================================

class AsyncLRUCache:
    """Thread-safe async LRU cache for query results."""

    def __init__(self, maxsize: int = 1000, ttl_seconds: int = 300):
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._access_order: List[str] = []
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache."""
        async with self._lock:
            if key in self._cache:
                value, timestamp = self._cache[key]
                if time.time() - timestamp < self.ttl_seconds:
                    # Update access order
                    self._access_order.remove(key)
                    self._access_order.append(key)
                    self._hits += 1
                    return value
                else:
                    # Expired
                    del self._cache[key]
                    self._access_order.remove(key)

            self._misses += 1
            return None

    async def set(self, key: str, value: Any) -> None:
        """Set value in cache."""
        async with self._lock:
            # Evict if at capacity
            while len(self._cache) >= self.maxsize and self._access_order:
                oldest = self._access_order.pop(0)
                if oldest in self._cache:
                    del self._cache[oldest]

            self._cache[key] = (value, time.time())
            if key in self._access_order:
                self._access_order.remove(key)
            self._access_order.append(key)

    async def invalidate(self, key: str) -> None:
        """Invalidate a cache entry."""
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                self._access_order.remove(key)

    async def invalidate_prefix(self, prefix: str) -> int:
        """Invalidate all entries with a key prefix."""
        async with self._lock:
            keys_to_remove = [k for k in self._cache.keys() if k.startswith(prefix)]
            for key in keys_to_remove:
                del self._cache[key]
                self._access_order.remove(key)
            return len(keys_to_remove)

    async def clear(self) -> None:
        """Clear the entire cache."""
        async with self._lock:
            self._cache.clear()
            self._access_order.clear()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self._hits + self._misses
        return {
            "size": len(self._cache),
            "maxsize": self.maxsize,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self._hits / total if total > 0 else 0.0,
        }


# =============================================================================
# VOICE PATTERN MEMORY
# =============================================================================

class VoicePatternMemory:
    """
    ChromaDB-based voice pattern memory system.

    Provides persistent storage and retrieval for:
    - Voice evolution tracking
    - Behavioral patterns
    - Attack signatures
    - Environmental profiles
    - Speech biometrics
    - Authentication events

    Usage:
        memory = await get_voice_pattern_memory()

        # Store voice evolution
        await memory.store_voice_evolution(record)

        # Find similar attacks
        similar = await memory.find_similar_attacks(fingerprint)

        # Get behavioral patterns
        patterns = await memory.get_behavioral_patterns(user_id)
    """

    def __init__(
        self,
        persist_directory: Optional[str] = None,
        enable_cache: bool = True,
        cache_ttl_seconds: Optional[int] = None,
    ):
        """
        Initialize voice pattern memory.

        Args:
            persist_directory: Directory for persistent storage
            enable_cache: Whether to enable query caching
            cache_ttl_seconds: Cache TTL override
        """
        if not CHROMADB_AVAILABLE:
            raise ImportError(
                "ChromaDB is required for VoicePatternMemory. "
                "Install with: pip install chromadb"
            )

        self.persist_directory = persist_directory or VoiceMemoryConfig.get_memory_dir()
        self.enable_cache = enable_cache

        # Create cache
        ttl = cache_ttl_seconds or VoiceMemoryConfig.get_cache_ttl_seconds()
        self._cache = AsyncLRUCache(maxsize=1000, ttl_seconds=ttl)

        # Initialize ChromaDB client
        self._client: Optional[chromadb.Client] = None
        self._collections: Dict[str, Collection] = {}
        self._initialized = False
        self._init_lock = asyncio.Lock()

        # Statistics
        self._stats = {
            "stores": 0,
            "queries": 0,
            "deletes": 0,
            "errors": 0,
        }

        logger.info(
            f"VoicePatternMemory initialized with persist_directory={self.persist_directory}"
        )

    async def initialize(self) -> None:
        """Initialize ChromaDB client and collections."""
        async with self._init_lock:
            if self._initialized:
                return

            try:
                # Ensure directory exists
                os.makedirs(self.persist_directory, exist_ok=True)

                # Create ChromaDB client
                self._client = chromadb.PersistentClient(
                    path=self.persist_directory,
                    settings=Settings(
                        anonymized_telemetry=False,
                        allow_reset=True,
                    ),
                )

                # Create/get collections
                for key, definition in COLLECTIONS.items():
                    collection_name = definition.get_full_name()

                    # Get or create collection
                    collection = self._client.get_or_create_collection(
                        name=collection_name,
                        metadata={"description": definition.description},
                    )

                    self._collections[key] = collection
                    logger.debug(f"Initialized collection: {collection_name}")

                self._initialized = True
                logger.info(
                    f"VoicePatternMemory initialized with {len(self._collections)} collections"
                )

            except Exception as e:
                self._stats["errors"] += 1
                logger.exception(f"Failed to initialize VoicePatternMemory: {e}")
                raise

    async def _ensure_initialized(self) -> None:
        """Ensure the memory system is initialized."""
        if not self._initialized:
            await self.initialize()

    def _get_collection(self, name: str) -> Collection:
        """Get a collection by name."""
        if name not in self._collections:
            raise ValueError(f"Unknown collection: {name}")
        return self._collections[name]

    # =========================================================================
    # VOICE EVOLUTION
    # =========================================================================

    async def store_voice_evolution(
        self,
        record: VoiceEvolutionRecord,
    ) -> str:
        """
        Store a voice evolution record.

        Args:
            record: Voice evolution record to store

        Returns:
            Record ID
        """
        await self._ensure_initialized()

        try:
            collection = self._get_collection("voice_evolution")

            # Prepare data
            embedding = record.embedding
            metadata = record.to_chromadb_metadata()
            document = f"Voice evolution for {record.user_id} at {record.created_at.isoformat()}"

            # Store in ChromaDB
            collection.add(
                ids=[record.record_id],
                embeddings=[embedding],
                metadatas=[metadata],
                documents=[document],
            )

            # Invalidate cache
            if self.enable_cache:
                await self._cache.invalidate_prefix(f"evolution:{record.user_id}")

            self._stats["stores"] += 1
            logger.debug(f"Stored voice evolution record: {record.record_id}")

            return record.record_id

        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Failed to store voice evolution: {e}")
            raise

    async def get_voice_evolution_history(
        self,
        user_id: str,
        limit: int = 100,
        days: Optional[int] = None,
    ) -> MemoryQueryResult:
        """
        Get voice evolution history for a user.

        Args:
            user_id: User identifier
            limit: Maximum records to return
            days: Optional day limit

        Returns:
            Query result with evolution records
        """
        await self._ensure_initialized()

        # Check cache
        cache_key = f"evolution:{user_id}:{limit}:{days}"
        if self.enable_cache:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        start_time = time.perf_counter()

        try:
            collection = self._get_collection("voice_evolution")

            # Build where clause
            where = {"user_id": user_id}

            # Query
            results = collection.query(
                query_embeddings=None,
                where=where,
                n_results=limit,
                include=["embeddings", "metadatas", "documents"],
            )

            # Convert to records
            records = []
            if results["ids"] and results["ids"][0]:
                for i, record_id in enumerate(results["ids"][0]):
                    metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                    embedding = results["embeddings"][0][i] if results["embeddings"] else []

                    record = VoiceEvolutionRecord(
                        record_id=record_id,
                        user_id=metadata.get("user_id", user_id),
                        embedding=embedding,
                        baseline_similarity=metadata.get("baseline_similarity", 0.0),
                        drift_magnitude=metadata.get("drift_magnitude", 0.0),
                        drift_type=DriftType(metadata.get("drift_type", "none")),
                        audio_quality_score=metadata.get("audio_quality_score", 0.0),
                        snr_db=metadata.get("snr_db", 0.0),
                        created_at=datetime.fromisoformat(metadata.get("created_at", datetime.now(timezone.utc).isoformat())),
                    )
                    records.append(record)

            # Filter by days if specified
            if days is not None:
                cutoff = datetime.now(timezone.utc) - timedelta(days=days)
                records = [r for r in records if r.created_at >= cutoff]

            query_time = (time.perf_counter() - start_time) * 1000

            result = MemoryQueryResult(
                records=records,
                total_count=len(records),
                query_time_ms=query_time,
                collection_name="voice_evolution",
            )

            # Cache result
            if self.enable_cache:
                await self._cache.set(cache_key, result)

            self._stats["queries"] += 1
            return result

        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Failed to get voice evolution history: {e}")
            raise

    async def find_similar_voice_samples(
        self,
        embedding: List[float],
        user_id: str,
        n_results: int = 10,
        min_similarity: float = 0.8,
    ) -> MemoryQueryResult:
        """
        Find voice samples similar to a given embedding.

        Args:
            embedding: Query embedding
            user_id: User identifier
            n_results: Maximum results
            min_similarity: Minimum similarity threshold

        Returns:
            Query result with similar samples
        """
        await self._ensure_initialized()

        start_time = time.perf_counter()

        try:
            collection = self._get_collection("voice_evolution")

            # Query by embedding similarity
            results = collection.query(
                query_embeddings=[embedding],
                where={"user_id": user_id},
                n_results=n_results,
                include=["embeddings", "metadatas", "distances"],
            )

            records = []
            if results["ids"] and results["ids"][0]:
                for i, record_id in enumerate(results["ids"][0]):
                    # Convert distance to similarity (assuming cosine distance)
                    distance = results["distances"][0][i] if results["distances"] else 1.0
                    similarity = 1.0 - distance

                    if similarity < min_similarity:
                        continue

                    metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                    record_embedding = results["embeddings"][0][i] if results["embeddings"] else []

                    record = VoiceEvolutionRecord(
                        record_id=record_id,
                        user_id=metadata.get("user_id", user_id),
                        embedding=record_embedding,
                        baseline_similarity=similarity,
                        drift_magnitude=metadata.get("drift_magnitude", 0.0),
                        audio_quality_score=metadata.get("audio_quality_score", 0.0),
                    )
                    records.append(record)

            query_time = (time.perf_counter() - start_time) * 1000

            self._stats["queries"] += 1
            return MemoryQueryResult(
                records=records,
                total_count=len(records),
                query_time_ms=query_time,
                collection_name="voice_evolution",
            )

        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Failed to find similar voice samples: {e}")
            raise

    # =========================================================================
    # BEHAVIORAL PATTERNS
    # =========================================================================

    async def store_behavioral_pattern(
        self,
        record: BehavioralPatternRecord,
    ) -> str:
        """
        Store a behavioral pattern record.

        Args:
            record: Behavioral pattern to store

        Returns:
            Record ID
        """
        await self._ensure_initialized()

        try:
            collection = self._get_collection("behavioral_patterns")

            metadata = record.to_chromadb_metadata()
            document = (
                f"Behavioral pattern for {record.user_id}: "
                f"{record.time_category} on day {record.day_of_week}"
            )

            # For non-embedding collections, use a dummy embedding
            dummy_embedding = [0.0] * VoiceMemoryConfig.get_embedding_dimension()

            collection.add(
                ids=[record.record_id],
                embeddings=[dummy_embedding],
                metadatas=[metadata],
                documents=[document],
            )

            if self.enable_cache:
                await self._cache.invalidate_prefix(f"behavioral:{record.user_id}")

            self._stats["stores"] += 1
            return record.record_id

        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Failed to store behavioral pattern: {e}")
            raise

    async def get_behavioral_patterns(
        self,
        user_id: str,
        hour_of_day: Optional[int] = None,
        day_of_week: Optional[int] = None,
        limit: int = 100,
    ) -> MemoryQueryResult:
        """
        Get behavioral patterns for a user.

        Args:
            user_id: User identifier
            hour_of_day: Optional hour filter
            day_of_week: Optional day filter
            limit: Maximum results

        Returns:
            Query result with behavioral patterns
        """
        await self._ensure_initialized()

        cache_key = f"behavioral:{user_id}:{hour_of_day}:{day_of_week}:{limit}"
        if self.enable_cache:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        start_time = time.perf_counter()

        try:
            collection = self._get_collection("behavioral_patterns")

            # Build where clause
            where = {"user_id": user_id}
            if hour_of_day is not None:
                where["hour_of_day"] = hour_of_day
            if day_of_week is not None:
                where["day_of_week"] = day_of_week

            results = collection.query(
                query_embeddings=None,
                where=where,
                n_results=limit,
                include=["metadatas", "documents"],
            )

            records = []
            if results["ids"] and results["ids"][0]:
                for i, record_id in enumerate(results["ids"][0]):
                    metadata = results["metadatas"][0][i] if results["metadatas"] else {}

                    record = BehavioralPatternRecord(
                        record_id=record_id,
                        user_id=metadata.get("user_id", user_id),
                        hour_of_day=metadata.get("hour_of_day", 0),
                        day_of_week=metadata.get("day_of_week", 0),
                        wifi_network_hash=metadata.get("wifi_network_hash", ""),
                        is_known_location=metadata.get("is_known_location", False),
                        device_id=metadata.get("device_id", ""),
                        authentication_result=metadata.get("authentication_result", "unknown"),
                        confidence_score=metadata.get("confidence_score", 0.0),
                        pattern_frequency=metadata.get("pattern_frequency", 1),
                    )
                    records.append(record)

            query_time = (time.perf_counter() - start_time) * 1000

            result = MemoryQueryResult(
                records=records,
                total_count=len(records),
                query_time_ms=query_time,
                collection_name="behavioral_patterns",
            )

            if self.enable_cache:
                await self._cache.set(cache_key, result)

            self._stats["queries"] += 1
            return result

        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Failed to get behavioral patterns: {e}")
            raise

    async def calculate_behavioral_confidence(
        self,
        user_id: str,
        hour_of_day: int,
        day_of_week: int,
        wifi_hash: str = "",
        device_id: str = "",
    ) -> float:
        """
        Calculate behavioral confidence for current context.

        Args:
            user_id: User identifier
            hour_of_day: Current hour
            day_of_week: Current day
            wifi_hash: Current WiFi hash
            device_id: Current device

        Returns:
            Behavioral confidence score
        """
        patterns = await self.get_behavioral_patterns(user_id, limit=1000)

        if patterns.is_empty():
            return 0.5  # No history, neutral confidence

        # Calculate match scores
        time_matches = 0
        location_matches = 0
        device_matches = 0
        total = len(patterns.records)

        for pattern in patterns.records:
            if isinstance(pattern, BehavioralPatternRecord):
                # Time match (within 2 hours)
                hour_diff = abs(pattern.hour_of_day - hour_of_day)
                if hour_diff <= 2 or hour_diff >= 22:  # Handle midnight wrap
                    time_matches += 1

                # Day match
                if pattern.day_of_week == day_of_week:
                    time_matches += 0.5

                # Location match
                if wifi_hash and pattern.wifi_network_hash == wifi_hash:
                    location_matches += 1

                # Device match
                if device_id and pattern.device_id == device_id:
                    device_matches += 1

        # Calculate weighted confidence
        time_score = time_matches / (total * 1.5) if total > 0 else 0.5
        location_score = location_matches / total if total > 0 else 0.5
        device_score = device_matches / total if total > 0 else 0.5

        # Weighted combination
        confidence = (
            time_score * 0.4 +
            location_score * 0.35 +
            device_score * 0.25
        )

        return min(1.0, max(0.0, confidence))

    # =========================================================================
    # ATTACK PATTERNS
    # =========================================================================

    async def store_attack_pattern(
        self,
        record: AttackPatternRecord,
    ) -> str:
        """
        Store an attack pattern record.

        Args:
            record: Attack pattern to store

        Returns:
            Record ID
        """
        await self._ensure_initialized()

        try:
            collection = self._get_collection("attack_patterns")

            embedding = record.audio_fingerprint
            if not embedding:
                embedding = [0.0] * VoiceMemoryConfig.get_embedding_dimension()

            metadata = record.to_chromadb_metadata()
            document = (
                f"Attack pattern: {record.attack_type.value} "
                f"targeting {record.target_user_id}"
            )

            collection.add(
                ids=[record.record_id],
                embeddings=[embedding],
                metadatas=[metadata],
                documents=[document],
            )

            if self.enable_cache:
                await self._cache.invalidate_prefix("attack:")

            self._stats["stores"] += 1
            logger.warning(
                f"Stored attack pattern: {record.attack_type.value} "
                f"(severity: {record.severity_score:.2f})"
            )

            return record.record_id

        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Failed to store attack pattern: {e}")
            raise

    async def find_similar_attacks(
        self,
        audio_fingerprint: List[float],
        n_results: int = 10,
        min_similarity: float = 0.85,
    ) -> MemoryQueryResult:
        """
        Find similar attack patterns.

        Args:
            audio_fingerprint: Query fingerprint
            n_results: Maximum results
            min_similarity: Minimum similarity

        Returns:
            Query result with similar attacks
        """
        await self._ensure_initialized()

        start_time = time.perf_counter()

        try:
            collection = self._get_collection("attack_patterns")

            results = collection.query(
                query_embeddings=[audio_fingerprint],
                n_results=n_results,
                include=["embeddings", "metadatas", "distances"],
            )

            records = []
            if results["ids"] and results["ids"][0]:
                for i, record_id in enumerate(results["ids"][0]):
                    distance = results["distances"][0][i] if results["distances"] else 1.0
                    similarity = 1.0 - distance

                    if similarity < min_similarity:
                        continue

                    metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                    fingerprint = results["embeddings"][0][i] if results["embeddings"] else []

                    record = AttackPatternRecord(
                        record_id=record_id,
                        user_id=metadata.get("user_id", ""),
                        attack_type=AttackType(metadata.get("attack_type", "unknown")),
                        attack_confidence=metadata.get("attack_confidence", 0.0),
                        audio_fingerprint=fingerprint,
                        replay_score=metadata.get("replay_score", 0.0),
                        synthesis_score=metadata.get("synthesis_score", 0.0),
                        liveness_score=metadata.get("liveness_score", 0.0),
                        target_user_id=metadata.get("target_user_id", ""),
                        voice_similarity_to_target=similarity,
                    )
                    records.append(record)

            query_time = (time.perf_counter() - start_time) * 1000

            self._stats["queries"] += 1
            return MemoryQueryResult(
                records=records,
                total_count=len(records),
                query_time_ms=query_time,
                collection_name="attack_patterns",
            )

        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Failed to find similar attacks: {e}")
            raise

    async def check_known_attack(
        self,
        audio_fingerprint: List[float],
    ) -> Optional[AttackPatternRecord]:
        """
        Check if audio matches a known attack pattern.

        Args:
            audio_fingerprint: Audio fingerprint to check

        Returns:
            Matching attack record if found
        """
        threshold = VoiceMemoryConfig.get_attack_similarity_threshold()
        result = await self.find_similar_attacks(
            audio_fingerprint,
            n_results=1,
            min_similarity=threshold,
        )

        if not result.is_empty():
            return result.records[0]
        return None

    # =========================================================================
    # ENVIRONMENTAL PROFILES
    # =========================================================================

    async def store_environmental_profile(
        self,
        record: EnvironmentalProfileRecord,
    ) -> str:
        """Store an environmental profile."""
        await self._ensure_initialized()

        try:
            collection = self._get_collection("environmental_profiles")

            embedding = record.adapted_embedding
            if not embedding:
                embedding = [0.0] * VoiceMemoryConfig.get_embedding_dimension()

            metadata = record.to_chromadb_metadata()
            document = (
                f"Environmental profile: {record.environment_type.value} "
                f"for {record.user_id}"
            )

            collection.add(
                ids=[record.record_id],
                embeddings=[embedding],
                metadatas=[metadata],
                documents=[document],
            )

            if self.enable_cache:
                await self._cache.invalidate_prefix(f"env:{record.user_id}")

            self._stats["stores"] += 1
            return record.record_id

        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Failed to store environmental profile: {e}")
            raise

    async def get_environmental_profile(
        self,
        user_id: str,
        environment_hash: str,
    ) -> Optional[EnvironmentalProfileRecord]:
        """Get environmental profile by hash."""
        await self._ensure_initialized()

        cache_key = f"env:{user_id}:{environment_hash}"
        if self.enable_cache:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        try:
            collection = self._get_collection("environmental_profiles")

            results = collection.query(
                query_embeddings=None,
                where={
                    "user_id": user_id,
                    "environment_hash": environment_hash,
                },
                n_results=1,
                include=["embeddings", "metadatas"],
            )

            if results["ids"] and results["ids"][0]:
                metadata = results["metadatas"][0][0] if results["metadatas"] else {}
                embedding = results["embeddings"][0][0] if results["embeddings"] else []

                record = EnvironmentalProfileRecord(
                    record_id=results["ids"][0][0],
                    user_id=metadata.get("user_id", user_id),
                    environment_hash=environment_hash,
                    environment_type=EnvironmentType(
                        metadata.get("environment_type", "unknown")
                    ),
                    adapted_embedding=embedding,
                    typical_snr_db=metadata.get("typical_snr_db", 0.0),
                    confidence_adjustment=metadata.get("confidence_adjustment", 0.0),
                    sample_count=metadata.get("sample_count", 0),
                    success_rate=metadata.get("success_rate", 0.0),
                )

                if self.enable_cache:
                    await self._cache.set(cache_key, record)

                self._stats["queries"] += 1
                return record

            return None

        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Failed to get environmental profile: {e}")
            raise

    # =========================================================================
    # AUTHENTICATION EVENTS
    # =========================================================================

    async def store_authentication_event(
        self,
        record: AuthenticationEventRecord,
    ) -> str:
        """Store an authentication event for audit trail."""
        await self._ensure_initialized()

        try:
            collection = self._get_collection("authentication_events")

            dummy_embedding = [0.0] * VoiceMemoryConfig.get_embedding_dimension()
            metadata = record.to_chromadb_metadata()
            document = (
                f"Auth event: {record.decision} for {record.user_id} "
                f"(confidence: {record.final_confidence:.2f})"
            )

            collection.add(
                ids=[record.record_id],
                embeddings=[dummy_embedding],
                metadatas=[metadata],
                documents=[document],
            )

            self._stats["stores"] += 1
            return record.record_id

        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Failed to store authentication event: {e}")
            raise

    async def get_authentication_history(
        self,
        user_id: str,
        limit: int = 100,
        decision_filter: Optional[str] = None,
        hours: Optional[int] = None,
    ) -> MemoryQueryResult:
        """Get authentication event history."""
        await self._ensure_initialized()

        start_time = time.perf_counter()

        try:
            collection = self._get_collection("authentication_events")

            where = {"user_id": user_id}
            if decision_filter:
                where["decision"] = decision_filter

            results = collection.query(
                query_embeddings=None,
                where=where,
                n_results=limit,
                include=["metadatas", "documents"],
            )

            records = []
            cutoff = None
            if hours:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

            if results["ids"] and results["ids"][0]:
                for i, record_id in enumerate(results["ids"][0]):
                    metadata = results["metadatas"][0][i] if results["metadatas"] else {}

                    created_at = datetime.fromisoformat(
                        metadata.get("created_at", datetime.now(timezone.utc).isoformat())
                    )

                    if cutoff and created_at < cutoff:
                        continue

                    record = AuthenticationEventRecord(
                        record_id=record_id,
                        user_id=metadata.get("user_id", user_id),
                        session_id=metadata.get("session_id", ""),
                        decision=metadata.get("decision", "unknown"),
                        final_confidence=metadata.get("final_confidence", 0.0),
                        ml_confidence=metadata.get("ml_confidence", 0.0),
                        physics_confidence=metadata.get("physics_confidence", 0.0),
                        behavioral_confidence=metadata.get("behavioral_confidence", 0.0),
                        total_duration_ms=metadata.get("total_duration_ms", 0),
                        reasoning_used=metadata.get("reasoning_used", False),
                        created_at=created_at,
                    )
                    records.append(record)

            query_time = (time.perf_counter() - start_time) * 1000

            # Calculate aggregates
            confidences = [r.final_confidence for r in records if isinstance(r, AuthenticationEventRecord)]
            successes = [r for r in records if isinstance(r, AuthenticationEventRecord) and r.decision == "authenticate"]

            self._stats["queries"] += 1
            return MemoryQueryResult(
                records=records,
                total_count=len(records),
                query_time_ms=query_time,
                collection_name="authentication_events",
                avg_confidence=sum(confidences) / len(confidences) if confidences else None,
                success_rate=len(successes) / len(records) if records else None,
            )

        except Exception as e:
            self._stats["errors"] += 1
            logger.exception(f"Failed to get authentication history: {e}")
            raise

    # =========================================================================
    # UTILITIES
    # =========================================================================

    async def cleanup_expired(self) -> int:
        """Remove expired records from all collections."""
        await self._ensure_initialized()

        deleted = 0
        now = datetime.now(timezone.utc)

        for key, collection in self._collections.items():
            try:
                # Query for expired records
                results = collection.query(
                    query_embeddings=None,
                    n_results=10000,
                    include=["metadatas"],
                )

                if results["ids"] and results["ids"][0]:
                    ids_to_delete = []
                    for i, record_id in enumerate(results["ids"][0]):
                        metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                        expires_at = metadata.get("expires_at")
                        if expires_at:
                            expire_time = datetime.fromisoformat(expires_at)
                            if expire_time < now:
                                ids_to_delete.append(record_id)

                    if ids_to_delete:
                        collection.delete(ids=ids_to_delete)
                        deleted += len(ids_to_delete)

            except Exception as e:
                logger.warning(f"Error cleaning up collection {key}: {e}")

        self._stats["deletes"] += deleted
        logger.info(f"Cleaned up {deleted} expired records")
        return deleted

    async def get_stats(self) -> Dict[str, Any]:
        """Get memory system statistics."""
        collection_stats = {}
        for key, collection in self._collections.items():
            try:
                count = collection.count()
                collection_stats[key] = {"count": count}
            except Exception:
                collection_stats[key] = {"count": "error"}

        return {
            "operations": self._stats.copy(),
            "collections": collection_stats,
            "cache": self._cache.get_stats() if self.enable_cache else None,
            "persist_directory": self.persist_directory,
        }

    async def close(self) -> None:
        """Close the memory system."""
        if self._cache:
            await self._cache.clear()
        self._initialized = False
        logger.info("VoicePatternMemory closed")


# =============================================================================
# PERSONAL CONTEXT CHALLENGE GENERATOR - v1.0 (Clinical-Grade Intelligence Edition)
# =============================================================================

class PersonalContextChallengeGenerator:
    """
    Generate personalized challenge questions from behavioral patterns.

    Leverages ChromaDB-stored authentication events and behavioral patterns
    to create contextual challenge questions that only the real user can answer.

    Features:
    - Three difficulty levels (Easy, Medium, Hard)
    - Temporal reasoning (last unlock, recent patterns, activity sequences)
    - Location-based challenges (where did you unlock from?)
    - Pattern-based challenges (typical unlock times, device usage)
    - Natural language responses (fuzzy matching, time tolerance)

    Challenge Types:
    - EASY: "When did you last unlock?" (within 2 hours tolerance)
    - MEDIUM: "Where have you been unlocking from this week?" (location patterns)
    - HARD: "Your last unlock was morning from home. Before that?" (sequence recall)

    Configuration:
    - JARVIS_CHALLENGE_QUESTIONS: Enable challenge questions (default: true)
    - JARVIS_CHALLENGE_DIFFICULTY: Default difficulty (easy/medium/hard, default: medium)
    - JARVIS_CHALLENGE_TIME_TOLERANCE_HOURS: Time answer tolerance (default: 2)
    """

    def __init__(self, memory: VoicePatternMemory):
        self.memory = memory
        self.logger = logger
        self.enabled = (
            os.getenv("JARVIS_CHALLENGE_QUESTIONS", "true").lower() == "true"
        )
        self.default_difficulty = os.getenv("JARVIS_CHALLENGE_DIFFICULTY", "medium")
        self.time_tolerance_hours = int(
            os.getenv("JARVIS_CHALLENGE_TIME_TOLERANCE_HOURS", "2")
        )

    async def generate_challenge(
        self,
        user_id: str = "owner",
        difficulty: str = "medium"
    ) -> Dict[str, Any]:
        """
        Generate personalized challenge question from user's patterns.

        Args:
            user_id: User ID for pattern lookup
            difficulty: Challenge difficulty (easy, medium, hard)

        Returns:
            Challenge dict with:
            - question: The challenge question
            - difficulty: Difficulty level
            - expected_answer: Expected answer (for verification)
            - answer_type: Type of answer expected (time, location, sequence)
            - challenge_id: Unique challenge ID

        Example:
            {
                "question": "When did you last unlock your Mac?",
                "difficulty": "easy",
                "expected_answer": "2024-01-15 14:30:00",
                "answer_type": "time",
                "challenge_id": "chal_abc123"
            }
        """
        if not self.enabled:
            return {
                "question": "Challenge questions are disabled",
                "difficulty": difficulty,
                "error": "disabled"
            }

        # Get authentication history
        try:
            auth_events = await self.memory.get_authentication_history(
                user_id=user_id,
                limit=20,
                success_only=True
            )

            if len(auth_events) < 2:
                return {
                    "question": "Not enough history to generate challenge",
                    "difficulty": difficulty,
                    "error": "insufficient_history"
                }

            # Route to difficulty-specific generator
            if difficulty == "easy":
                return await self._generate_last_unlock_time_challenge(
                    user_id, auth_events
                )
            elif difficulty == "medium":
                return await self._generate_recent_pattern_challenge(
                    user_id, auth_events
                )
            elif difficulty == "hard":
                return await self._generate_activity_sequence_challenge(
                    user_id, auth_events
                )
            else:
                # Default to medium
                return await self._generate_recent_pattern_challenge(
                    user_id, auth_events
                )

        except Exception as e:
            self.logger.error(f"Challenge generation failed: {e}")
            return {
                "question": "Unable to generate challenge question",
                "difficulty": difficulty,
                "error": str(e)
            }

    async def _generate_last_unlock_time_challenge(
        self,
        user_id: str,
        events: List[AuthenticationEventRecord]
    ) -> Dict[str, Any]:
        """
        Generate EASY challenge: "When did you last unlock?"

        Args:
            user_id: User ID
            events: Recent authentication events

        Returns:
            Challenge dict with time-based question
        """
        if not events:
            return {"error": "no_events"}

        # Get most recent unlock
        last_event = events[0]
        last_unlock_time = datetime.fromisoformat(last_event.timestamp)

        # Calculate time ago
        now = datetime.now()
        time_diff = now - last_unlock_time
        hours_ago = time_diff.total_seconds() / 3600

        # Format question based on how long ago
        if hours_ago < 1:
            minutes_ago = int(time_diff.total_seconds() / 60)
            time_description = f"about {minutes_ago} minutes ago"
        elif hours_ago < 24:
            hours = int(hours_ago)
            time_description = f"about {hours} hour{'s' if hours != 1 else ''} ago"
        else:
            days = int(hours_ago / 24)
            time_description = f"about {days} day{'s' if days != 1 else ''} ago"

        question = f"For verification: When did you last unlock your Mac? (Hint: It was {time_description})"

        return {
            "question": question,
            "difficulty": "easy",
            "expected_answer": last_unlock_time.isoformat(),
            "answer_type": "time",
            "challenge_id": f"time_{user_id}_{int(time.time())}",
            "tolerance_hours": self.time_tolerance_hours,
            "hint": time_description
        }

    async def _generate_recent_pattern_challenge(
        self,
        user_id: str,
        events: List[AuthenticationEventRecord]
    ) -> Dict[str, Any]:
        """
        Generate MEDIUM challenge: "Where have you been unlocking from?"

        Args:
            user_id: User ID
            events: Recent authentication events

        Returns:
            Challenge dict with location/pattern question
        """
        # Analyze location patterns from last 7 days
        week_ago = datetime.now() - timedelta(days=7)
        recent_events = [
            e for e in events
            if datetime.fromisoformat(e.timestamp) >= week_ago
        ]

        if len(recent_events) < 3:
            # Fallback to easy challenge
            return await self._generate_last_unlock_time_challenge(user_id, events)

        # Extract location patterns (network SSIDs from metadata)
        locations = set()
        for event in recent_events:
            metadata = event.metadata or {}
            location = metadata.get("location", "unknown")
            network = metadata.get("network_ssid", None)

            if network:
                locations.add(network)
            elif location != "unknown":
                locations.add(location)

        if not locations:
            # No location data, ask about time pattern instead
            return await self._generate_time_pattern_challenge(user_id, recent_events)

        # Generate location question
        location_list = list(locations)
        if len(location_list) == 1:
            question = f"Quick verification: You've been unlocking from the same location this week. Where is that?"
            expected_answer = location_list[0]
        else:
            question = f"For verification: You've unlocked from {len(location_list)} different locations this week. Can you name one of them?"
            expected_answer = "|".join(location_list)  # Accept any

        return {
            "question": question,
            "difficulty": "medium",
            "expected_answer": expected_answer,
            "answer_type": "location",
            "challenge_id": f"location_{user_id}_{int(time.time())}",
            "acceptable_answers": location_list
        }

    async def _generate_time_pattern_challenge(
        self,
        user_id: str,
        events: List[AuthenticationEventRecord]
    ) -> Dict[str, Any]:
        """
        Generate time pattern challenge (fallback for medium difficulty).

        Args:
            user_id: User ID
            events: Recent authentication events

        Returns:
            Challenge dict about typical unlock times
        """
        # Analyze typical unlock hours
        unlock_hours = []
        for event in events[:10]:  # Last 10 unlocks
            event_time = datetime.fromisoformat(event.timestamp)
            unlock_hours.append(event_time.hour)

        # Find most common hour
        from collections import Counter
        hour_counts = Counter(unlock_hours)
        most_common_hour = hour_counts.most_common(1)[0][0]

        # Format as time period
        if 5 <= most_common_hour < 9:
            time_period = "early morning (5-9 AM)"
        elif 9 <= most_common_hour < 12:
            time_period = "late morning (9 AM-noon)"
        elif 12 <= most_common_hour < 17:
            time_period = "afternoon (noon-5 PM)"
        elif 17 <= most_common_hour < 21:
            time_period = "evening (5-9 PM)"
        else:
            time_period = "late night (9 PM-5 AM)"

        question = f"Quick verification: What time of day do you typically unlock your Mac? (Hint: Check your patterns)"

        return {
            "question": question,
            "difficulty": "medium",
            "expected_answer": time_period,
            "answer_type": "time_pattern",
            "challenge_id": f"timepattern_{user_id}_{int(time.time())}",
            "acceptable_answers": [time_period, str(most_common_hour)],
            "hint": f"You usually unlock around {most_common_hour}:00"
        }

    async def _generate_activity_sequence_challenge(
        self,
        user_id: str,
        events: List[AuthenticationEventRecord]
    ) -> Dict[str, Any]:
        """
        Generate HARD challenge: "Your last unlock was X. Before that?"

        Args:
            user_id: User ID
            events: Recent authentication events

        Returns:
            Challenge dict requiring sequence recall
        """
        if len(events) < 3:
            # Not enough history, fallback to medium
            return await self._generate_recent_pattern_challenge(user_id, events)

        # Get last 3 unlocks
        last_unlock = events[0]
        second_last = events[1]
        third_last = events[2]

        # Format last unlock details
        last_time = datetime.fromisoformat(last_unlock.timestamp)
        last_metadata = last_unlock.metadata or {}
        last_location = last_metadata.get("location", "unknown location")

        # Format time of day
        hour = last_time.hour
        if 5 <= hour < 12:
            time_of_day = "morning"
        elif 12 <= hour < 17:
            time_of_day = "afternoon"
        elif 17 <= hour < 21:
            time_of_day = "evening"
        else:
            time_of_day = "late night"

        # Ask about second-to-last unlock
        second_time = datetime.fromisoformat(second_last.timestamp)
        second_metadata = second_last.metadata or {}
        second_location = second_metadata.get("location", "unknown location")

        # Format expected answer
        time_diff = last_time - second_time
        hours_between = time_diff.total_seconds() / 3600

        if hours_between < 1:
            time_between_desc = f"{int(time_diff.total_seconds() / 60)} minutes before"
        elif hours_between < 24:
            time_between_desc = f"{int(hours_between)} hours before"
        else:
            time_between_desc = f"{int(hours_between / 24)} days before"

        question = (
            f"Verification question: Your last unlock was {time_of_day} from {last_location}. "
            f"Where did you unlock from before that?"
        )

        return {
            "question": question,
            "difficulty": "hard",
            "expected_answer": second_location,
            "answer_type": "sequence",
            "challenge_id": f"sequence_{user_id}_{int(time.time())}",
            "acceptable_answers": [second_location],
            "hint": f"It was {time_between_desc} that",
            "sequence_context": {
                "last": last_location,
                "second_last": second_location,
                "third_last": second_metadata.get("location", "unknown")
            }
        }

    async def verify_challenge_response(
        self,
        challenge: Dict[str, Any],
        user_response: str
    ) -> Tuple[bool, str]:
        """
        Verify user's response to a challenge question.

        Args:
            challenge: Challenge dict from generate_challenge()
            user_response: User's answer

        Returns:
            Tuple of (success: bool, feedback: str)

        Example:
            (True, "Correct! Unlocking now.")
            (False, "That's not quite right. The actual answer was 2:30 PM.")
        """
        answer_type = challenge.get("answer_type", "unknown")
        expected = challenge.get("expected_answer", "")
        acceptable = challenge.get("acceptable_answers", [expected])

        # Normalize user response
        user_response_normalized = user_response.strip().lower()

        if answer_type == "time":
            # Time-based verification with tolerance
            return await self._verify_time_response(
                challenge, user_response_normalized
            )

        elif answer_type == "location":
            # Location verification (fuzzy match)
            return self._verify_location_response(
                acceptable, user_response_normalized
            )

        elif answer_type == "time_pattern":
            # Time pattern verification
            return self._verify_time_pattern_response(
                acceptable, user_response_normalized
            )

        elif answer_type == "sequence":
            # Sequence verification
            return self._verify_sequence_response(
                acceptable, user_response_normalized
            )

        else:
            # Generic string match
            if user_response_normalized in [a.lower() for a in acceptable]:
                return (True, "Correct! Verified.")
            else:
                return (False, f"Incorrect. Expected: {expected}")

    async def _verify_time_response(
        self,
        challenge: Dict[str, Any],
        user_response: str
    ) -> Tuple[bool, str]:
        """Verify time-based response with tolerance."""
        expected_time_str = challenge.get("expected_answer", "")
        tolerance_hours = challenge.get("tolerance_hours", 2)

        try:
            expected_time = datetime.fromisoformat(expected_time_str)

            # Try to parse user's response (flexible parsing)
            # Support formats: "2 hours ago", "14:30", "2:30 PM", etc.
            import re

            # Pattern 1: "X hours ago"
            hours_ago_match = re.search(r"(\d+)\s*hours?\s*ago", user_response)
            if hours_ago_match:
                hours = int(hours_ago_match.group(1))
                user_time = datetime.now() - timedelta(hours=hours)
            # Pattern 2: "X minutes ago"
            elif "minute" in user_response:
                minutes_match = re.search(r"(\d+)\s*minutes?\s*ago", user_response)
                if minutes_match:
                    minutes = int(minutes_match.group(1))
                    user_time = datetime.now() - timedelta(minutes=minutes)
                else:
                    raise ValueError("Could not parse time")
            else:
                # Assume actual time (would need more sophisticated parsing)
                raise ValueError("Time format not recognized")

            # Check if within tolerance
            time_diff = abs((user_time - expected_time).total_seconds() / 3600)
            if time_diff <= tolerance_hours:
                return (True, "Correct! Time matches. Verified.")
            else:
                return (
                    False,
                    f"Close, but not quite. It was actually {challenge.get('hint', 'recently')}."
                )

        except Exception as e:
            # Couldn't parse, give user another chance
            return (
                False,
                f"I couldn't understand that time format. Try: '{challenge.get('hint', 'X hours ago')}'"
            )

    def _verify_location_response(
        self,
        acceptable_answers: List[str],
        user_response: str
    ) -> Tuple[bool, str]:
        """Verify location response with fuzzy matching."""
        # Normalize acceptable answers
        acceptable_normalized = [a.strip().lower() for a in acceptable_answers]

        # Direct match
        if user_response in acceptable_normalized:
            return (True, "Correct location! Verified.")

        # Fuzzy match (substring match)
        for acceptable in acceptable_normalized:
            if acceptable in user_response or user_response in acceptable:
                return (True, "Correct location! Verified.")

        return (
            False,
            f"That location doesn't match. Expected one of: {', '.join(acceptable_answers[:3])}"
        )

    def _verify_time_pattern_response(
        self,
        acceptable_answers: List[str],
        user_response: str
    ) -> Tuple[bool, str]:
        """Verify time pattern response."""
        acceptable_normalized = [str(a).strip().lower() for a in acceptable_answers]

        # Check for match
        for acceptable in acceptable_normalized:
            if acceptable in user_response or user_response in acceptable:
                return (True, "Correct pattern! Verified.")

        return (
            False,
            f"Not quite. You typically unlock during: {acceptable_answers[0]}"
        )

    def _verify_sequence_response(
        self,
        acceptable_answers: List[str],
        user_response: str
    ) -> Tuple[bool, str]:
        """Verify sequence/activity response."""
        return self._verify_location_response(acceptable_answers, user_response)


# =============================================================================
# FACTORY FUNCTIONS
# =============================================================================

_memory_instance: Optional[VoicePatternMemory] = None
_memory_lock = asyncio.Lock()


async def get_voice_pattern_memory(
    force_new: bool = False,
) -> VoicePatternMemory:
    """
    Get or create the voice pattern memory instance.

    Args:
        force_new: Force creation of new instance

    Returns:
        VoicePatternMemory instance
    """
    global _memory_instance

    async with _memory_lock:
        if _memory_instance is None or force_new:
            _memory_instance = VoicePatternMemory()
            await _memory_instance.initialize()
        return _memory_instance


def create_voice_pattern_memory(
    persist_directory: Optional[str] = None,
    enable_cache: bool = True,
) -> VoicePatternMemory:
    """
    Create a new voice pattern memory instance.

    Args:
        persist_directory: Custom persist directory
        enable_cache: Enable caching

    Returns:
        New VoicePatternMemory instance
    """
    return VoicePatternMemory(
        persist_directory=persist_directory,
        enable_cache=enable_cache,
    )


__all__ = [
    "VoicePatternMemory",
    "get_voice_pattern_memory",
    "create_voice_pattern_memory",
    "COLLECTIONS",
    "CollectionDefinition",
    "AsyncLRUCache",
]
