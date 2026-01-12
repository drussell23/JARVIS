#!/usr/bin/env python3
"""
Semantic Cache with LSH (Locality-Sensitive Hashing)
Purpose: Eliminate redundant API calls through intelligent semantic matching

Multi-Level Cache Architecture:
- L1: Exact Match Cache (20MB, 30s TTL)
- L2: Semantic Similarity Cache (100MB, 5min TTL, LSH)
- L3: Contextual Cache (80MB, 30min TTL)
- L4: Predictive Cache (50MB, dynamic TTL)

Total Memory Allocation: 250MB
"""

import asyncio
import hashlib
import logging
import os
import pickle  # nosec B403 - Used only for internal cache serialization, not untrusted data
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# ML imports
try:
    import faiss
    from sentence_transformers import SentenceTransformer

    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False
    logging.warning(
        "ML libraries not available. Install with: pip install faiss-cpu torch sentence-transformers scikit-learn"
    )

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CacheLevel(Enum):
    """Cache level identifiers"""

    L1_EXACT = "L1_EXACT"
    L2_SEMANTIC = "L2_SEMANTIC"
    L3_CONTEXTUAL = "L3_CONTEXTUAL"
    L4_PREDICTIVE = "L4_PREDICTIVE"


@dataclass
class CacheEntry:
    """Universal cache entry structure"""

    key: str
    value: Any
    embedding: Optional[np.ndarray] = None
    context: Optional[Dict[str, Any]] = None
    timestamp: datetime = field(default_factory=datetime.now)
    last_access: datetime = field(default_factory=datetime.now)
    access_count: int = 0
    ttl_seconds: int = 30
    cache_level: CacheLevel = CacheLevel.L1_EXACT
    size_bytes: int = 0
    similarity_score: float = 1.0

    def is_expired(self) -> bool:
        """Check if entry has expired"""
        return (datetime.now() - self.timestamp).total_seconds() > self.ttl_seconds

    def access(self):
        """Update access statistics"""
        self.last_access = datetime.now()
        self.access_count += 1

    def calculate_value_score(self) -> float:
        """Calculate value score for eviction policy"""
        age_factor = 1.0 / (1 + (datetime.now() - self.timestamp).total_seconds() / 3600)
        access_factor = min(self.access_count / 10, 1.0)
        recency_factor = 1.0 / (1 + (datetime.now() - self.last_access).total_seconds() / 600)
        size_factor = 1.0 / (1 + self.size_bytes / 1024 / 1024)  # Favor smaller entries

        return age_factor * 0.2 + access_factor * 0.4 + recency_factor * 0.3 + size_factor * 0.1


class BaseCacheLayer(ABC):
    """Abstract base class for cache layers with memory pressure awareness"""

    def __init__(self, size_mb: int, ttl_seconds: int, cache_level: CacheLevel):
        self.size_mb = size_mb
        self.max_size_bytes = size_mb * 1024 * 1024
        self.ttl_seconds = ttl_seconds
        self.cache_level = cache_level
        self.current_size_bytes = 0
        self.hits = 0
        self.misses = 0
        self.cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()

        # Memory pressure integration
        self._memory_manager = None
        self._adaptive_sizing = os.getenv("ADAPTIVE_CACHE_SIZING", "true").lower() == "true"
        self._base_size_mb = size_mb  # Store original size

    def set_memory_manager(self, manager):
        """Set memory manager for adaptive sizing"""
        self._memory_manager = manager
        logger.info(f"✅ {self.cache_level.value} connected to memory pressure manager")

    def get_effective_size_limit(self) -> int:
        """Get effective cache size limit based on memory pressure"""
        if not self._adaptive_sizing or not self._memory_manager:
            return self.max_size_bytes

        # Get pressure-based scaling factor
        from ..macos_memory_manager import MemoryPressure

        pressure = self._memory_manager.current_pressure

        # Scale down cache size based on pressure
        if pressure == MemoryPressure.RED:
            scale_factor = 0.25  # 25% of original size
        elif pressure == MemoryPressure.YELLOW:
            scale_factor = 0.5  # 50% of original size
        else:  # GREEN or UNKNOWN
            scale_factor = 1.0  # Full size

        new_size = int(self._base_size_mb * scale_factor * 1024 * 1024)

        if new_size != self.max_size_bytes:
            logger.debug(
                f"{self.cache_level.value}: Adjusting cache size "
                f"{self.max_size_bytes / 1024 / 1024:.0f}MB → {new_size / 1024 / 1024:.0f}MB "
                f"(pressure: {pressure.value})"
            )
            self.max_size_bytes = new_size

        return self.max_size_bytes

    @abstractmethod
    async def get(self, key: str, **kwargs) -> Optional[CacheEntry]:
        """Get entry from cache"""

    @abstractmethod
    async def put(self, key: str, value: Any, **kwargs) -> bool:
        """Put entry in cache"""

    async def evict_if_needed(self, required_bytes: int):
        """Evict entries if needed to make space (memory-pressure aware)"""
        # Get dynamic size limit
        effective_limit = self.get_effective_size_limit()

        while self.current_size_bytes + required_bytes > effective_limit and self.cache:
            # Calculate value scores
            entries_with_scores = [
                (k, entry, entry.calculate_value_score()) for k, entry in self.cache.items()
            ]

            # Sort by value score (lowest first for eviction)
            entries_with_scores.sort(key=lambda x: x[2])

            # Evict lowest value entry
            key_to_evict = entries_with_scores[0][0]
            evicted_entry = self.cache.pop(key_to_evict)
            self.current_size_bytes -= evicted_entry.size_bytes

            logger.debug(f"Evicted {key_to_evict} from {self.cache_level.value}")

    async def evict_to_pressure_limit(self):
        """Aggressively evict to meet memory pressure limits"""
        effective_limit = self.get_effective_size_limit()

        if self.current_size_bytes <= effective_limit:
            return  # Already within limits

        evicted_count = 0
        while self.current_size_bytes > effective_limit and self.cache:
            # Evict oldest entry
            key_to_evict = next(iter(self.cache))
            evicted_entry = self.cache.pop(key_to_evict)
            self.current_size_bytes -= evicted_entry.size_bytes
            evicted_count += 1

        if evicted_count > 0:
            logger.info(
                f"🗑️  {self.cache_level.value}: Evicted {evicted_count} entries "
                f"due to memory pressure (now {self.current_size_bytes / 1024 / 1024:.1f}MB)"
            )

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics with memory pressure info"""
        total_requests = self.hits + self.misses
        hit_rate = self.hits / total_requests if total_requests > 0 else 0

        stats = {
            "level": self.cache_level.value,
            "size_mb": self.size_mb,
            "current_size_mb": self.current_size_bytes / 1024 / 1024,
            "effective_limit_mb": self.get_effective_size_limit() / 1024 / 1024,
            "entries": len(self.cache),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": hit_rate,
            "ttl_seconds": self.ttl_seconds,
            "adaptive_sizing": self._adaptive_sizing,
        }

        # Add memory pressure info if available
        if self._memory_manager:
            stats["memory_pressure"] = self._memory_manager.current_pressure.value

        return stats


class L1ExactMatchCache(BaseCacheLayer):
    """Level 1: Exact match cache with O(1) lookup"""

    def __init__(self):
        super().__init__(size_mb=20, ttl_seconds=30, cache_level=CacheLevel.L1_EXACT)

    async def get(self, key: str, **kwargs) -> Optional[CacheEntry]:
        """Get exact match from cache"""
        async with self._lock:
            if key in self.cache:
                entry = self.cache[key]

                # Check expiration
                if entry.is_expired():
                    self.cache.pop(key)
                    self.current_size_bytes -= entry.size_bytes
                    self.misses += 1
                    return None

                # Move to end (LRU) and update access
                self.cache.move_to_end(key)
                entry.access()
                self.hits += 1
                return entry

            self.misses += 1
            return None

    async def put(self, key: str, value: Any, **kwargs) -> bool:
        """Put exact match in cache"""
        async with self._lock:
            # Calculate size
            size_bytes = len(pickle.dumps(value))

            # Evict if needed
            await self.evict_if_needed(size_bytes)

            # Create entry
            entry = CacheEntry(
                key=key,
                value=value,
                ttl_seconds=self.ttl_seconds,
                cache_level=self.cache_level,
                size_bytes=size_bytes,
            )

            # Store
            self.cache[key] = entry
            self.current_size_bytes += size_bytes

            return True


class LSHIndex:
    """Locality-Sensitive Hashing implementation for similarity search"""

    def __init__(self, dim: int, num_tables: int = 10, hash_size: int = 8):
        self.dim = dim
        self.num_tables = num_tables
        self.hash_size = hash_size
        self.tables: List[Dict[str, List[str]]] = [defaultdict(list) for _ in range(num_tables)]
        self.random_projections = [
            np.random.randn(hash_size, dim).astype(np.float32) for _ in range(num_tables)
        ]

    def _hash(self, vector: np.ndarray, table_idx: int) -> str:
        """Generate hash for vector using random projection"""
        projection = self.random_projections[table_idx]
        hash_values = (projection @ vector) > 0
        return "".join(["1" if x else "0" for x in hash_values])

    def add(self, key: str, vector: np.ndarray):
        """Add vector to LSH index"""
        for i in range(self.num_tables):
            hash_key = self._hash(vector, i)
            self.tables[i][hash_key].append(key)

    def query(self, vector: np.ndarray, max_candidates: int = 50) -> List[str]:
        """Query similar vectors"""
        candidates = set()

        for i in range(self.num_tables):
            hash_key = self._hash(vector, i)

            # Check exact bucket
            if hash_key in self.tables[i]:
                candidates.update(self.tables[i][hash_key])

            # Check nearby buckets (1 bit flip)
            for bit_idx in range(self.hash_size):
                nearby_hash = list(hash_key)
                nearby_hash[bit_idx] = "0" if nearby_hash[bit_idx] == "1" else "1"
                nearby_hash_key = "".join(nearby_hash)

                if nearby_hash_key in self.tables[i]:
                    candidates.update(self.tables[i][nearby_hash_key])

                if len(candidates) >= max_candidates:
                    break

        return list(candidates)[:max_candidates]

    def remove(self, key: str, vector: np.ndarray):
        """Remove vector from index"""
        for i in range(self.num_tables):
            hash_key = self._hash(vector, i)
            if hash_key in self.tables[i] and key in self.tables[i][hash_key]:
                self.tables[i][hash_key].remove(key)
                if not self.tables[i][hash_key]:
                    del self.tables[i][hash_key]


class L2SemanticSimilarityCache(BaseCacheLayer):
    """Level 2: Semantic similarity cache with LSH"""

    def __init__(self, embedding_dim: int = 384):
        super().__init__(size_mb=100, ttl_seconds=300, cache_level=CacheLevel.L2_SEMANTIC)
        self.embedding_dim = embedding_dim
        self.similarity_threshold = 0.85

        # Initialize LSH
        self.lsh_index = LSHIndex(embedding_dim, num_tables=12, hash_size=10)

        # Initialize embedding model if available
        self.embedder = None
        if EMBEDDINGS_AVAILABLE:
            try:
                self.embedder = SentenceTransformer("all-MiniLM-L6-v2")
                logger.info("Initialized sentence transformer for semantic cache")
            except Exception as e:
                logger.warning(f"Could not initialize embedder: {e}")

        # Initialize FAISS index for exact similarity
        self.faiss_index = None
        if EMBEDDINGS_AVAILABLE:
            try:
                self.faiss_index = faiss.IndexFlatIP(
                    embedding_dim
                )  # Inner product for cosine similarity
                logger.info("Initialized FAISS index for semantic search")
            except Exception as e:
                logger.warning(f"Could not initialize FAISS: {e}")

        self.key_to_idx: Dict[str, int] = {}
        self.idx_to_key: Dict[int, str] = {}
        self.next_idx = 0

    def _generate_embedding(self, text: str) -> Optional[np.ndarray]:
        """Generate embedding for text"""
        if self.embedder is None:
            # Fallback: simple hash-based embedding
            hash_obj = hashlib.sha384(text.encode())
            hash_bytes = hash_obj.digest()
            embedding = np.frombuffer(hash_bytes, dtype=np.uint8).astype(np.float32)
            embedding = embedding[: self.embedding_dim]
            # Pad if needed
            if len(embedding) < self.embedding_dim:
                embedding = np.pad(embedding, (0, self.embedding_dim - len(embedding)))
            return embedding / np.linalg.norm(embedding)

        try:
            embedding = self.embedder.encode(text, convert_to_numpy=True)
            return embedding / np.linalg.norm(embedding)  # Normalize
        except Exception as e:
            logger.warning(f"Embedding generation failed: {e}")
            return None

    async def get(
        self, key: str, query_embedding: Optional[np.ndarray] = None, **kwargs
    ) -> Optional[CacheEntry]:
        """Get semantically similar entry from cache"""
        async with self._lock:
            # Generate embedding if not provided
            if query_embedding is None:
                query_embedding = self._generate_embedding(key)
                if query_embedding is None:
                    self.misses += 1
                    return None

            # Get candidates from LSH
            candidates = self.lsh_index.query(query_embedding, max_candidates=20)

            if not candidates:
                self.misses += 1
                return None

            # Find best match
            best_match = None
            best_similarity = 0.0

            for candidate_key in candidates:
                if candidate_key not in self.cache:
                    continue

                entry = self.cache[candidate_key]

                # Check expiration
                if entry.is_expired():
                    self.cache.pop(candidate_key)
                    self.current_size_bytes -= entry.size_bytes
                    # Remove from indices
                    if candidate_key in self.key_to_idx:
                        idx = self.key_to_idx[candidate_key]
                        del self.key_to_idx[candidate_key]
                        del self.idx_to_key[idx]
                    continue

                # Calculate similarity
                if entry.embedding is not None:
                    similarity = float(np.dot(query_embedding, entry.embedding))
                    if similarity > best_similarity and similarity >= self.similarity_threshold:
                        best_similarity = similarity
                        best_match = entry

            if best_match:
                best_match.access()
                best_match.similarity_score = best_similarity
                self.hits += 1
                return best_match

            self.misses += 1
            return None

    async def put(
        self, key: str, value: Any, embedding: Optional[np.ndarray] = None, **kwargs
    ) -> bool:
        """Put entry with semantic embedding in cache"""
        async with self._lock:
            # Generate embedding if not provided
            if embedding is None:
                embedding = self._generate_embedding(key)
                if embedding is None:
                    return False

            # Calculate size
            size_bytes = len(pickle.dumps(value)) + embedding.nbytes

            # Evict if needed
            await self.evict_if_needed(size_bytes)

            # Create entry
            entry = CacheEntry(
                key=key,
                value=value,
                embedding=embedding,
                ttl_seconds=self.ttl_seconds,
                cache_level=self.cache_level,
                size_bytes=size_bytes,
            )

            # Add to LSH index
            self.lsh_index.add(key, embedding)

            # Add to FAISS if available
            if self.faiss_index is not None:
                idx = self.next_idx
                self.key_to_idx[key] = idx
                self.idx_to_key[idx] = key
                self.next_idx += 1
                self.faiss_index.add(embedding.reshape(1, -1))

            # Store
            self.cache[key] = entry
            self.current_size_bytes += size_bytes

            return True


class L3ContextualCache(BaseCacheLayer):
    """Level 3: Context-aware cache with goal-based matching"""

    def __init__(self):
        super().__init__(size_mb=80, ttl_seconds=1800, cache_level=CacheLevel.L3_CONTEXTUAL)
        self.context_index: Dict[str, List[str]] = defaultdict(list)

    def _extract_context_key(self, context: Dict[str, Any]) -> str:
        """Extract context key for indexing"""
        # Create context signature
        context_parts = []

        if "goal" in context:
            context_parts.append(f"goal:{context['goal']}")
        if "app_id" in context:
            context_parts.append(f"app:{context['app_id']}")
        if "workflow" in context:
            context_parts.append(f"workflow:{context['workflow']}")
        if "user_state" in context:
            context_parts.append(f"state:{context['user_state']}")

        return "|".join(sorted(context_parts))

    async def get(
        self, key: str, context: Optional[Dict[str, Any]] = None, **kwargs
    ) -> Optional[CacheEntry]:
        """Get context-aware entry from cache"""
        async with self._lock:
            # Try exact key match first
            if key in self.cache:
                entry = self.cache[key]
                if not entry.is_expired():
                    entry.access()
                    self.hits += 1
                    return entry
                else:
                    self.cache.pop(key)
                    self.current_size_bytes -= entry.size_bytes

            # Try context-based matching
            if context:
                context_key = self._extract_context_key(context)
                if context_key in self.context_index:
                    for candidate_key in self.context_index[context_key]:
                        if candidate_key in self.cache:
                            entry = self.cache[candidate_key]
                            if not entry.is_expired():
                                entry.access()
                                self.hits += 1
                                return entry

            self.misses += 1
            return None

    async def put(
        self, key: str, value: Any, context: Optional[Dict[str, Any]] = None, **kwargs
    ) -> bool:
        """Put context-aware entry in cache"""
        async with self._lock:
            # Calculate size
            size_bytes = len(pickle.dumps(value))
            if context:
                size_bytes += len(pickle.dumps(context))

            # Evict if needed
            await self.evict_if_needed(size_bytes)

            # Create entry
            entry = CacheEntry(
                key=key,
                value=value,
                context=context,
                ttl_seconds=self.ttl_seconds,
                cache_level=self.cache_level,
                size_bytes=size_bytes,
            )

            # Index by context
            if context:
                context_key = self._extract_context_key(context)
                self.context_index[context_key].append(key)

            # Store
            self.cache[key] = entry
            self.current_size_bytes += size_bytes

            return True


class L4PredictiveCache(BaseCacheLayer):
    """Level 4: Predictive cache with pattern-based pre-computation"""

    def __init__(self):
        super().__init__(
            size_mb=50, ttl_seconds=-1, cache_level=CacheLevel.L4_PREDICTIVE
        )  # Dynamic TTL
        self.pattern_predictor = PatternPredictor()
        self.pre_compute_queue: asyncio.Queue = asyncio.Queue()
        self.prediction_task = None

    async def start_prediction_loop(self):
        """Start background prediction task"""
        if self.prediction_task is None:
            self.prediction_task = asyncio.create_task(self._prediction_loop())

    async def stop_prediction_loop(self):
        """Stop background prediction task"""
        if self.prediction_task:
            self.prediction_task.cancel()
            try:
                await self.prediction_task
            except asyncio.CancelledError:
                pass
            self.prediction_task = None

    async def _prediction_loop(self):
        """Background loop for predictive caching"""
        max_runtime = float(os.getenv("TIMEOUT_VISION_SESSION", "3600.0"))  # 1 hour default
        session_start = time.monotonic()
        while time.monotonic() - session_start < max_runtime:
            try:
                # Get prediction request
                prediction_request = await self.pre_compute_queue.get()

                # Generate predictions
                predictions = self.pattern_predictor.predict_next_queries(
                    prediction_request["history"], prediction_request["context"]
                )

                # Pre-compute and cache top predictions
                for prediction in predictions[:3]:
                    if prediction["confidence"] > 0.7:
                        # This would trigger pre-computation
                        logger.info(f"Pre-computing for predicted query: {prediction['query']}")
                        # Store with dynamic TTL based on confidence
                        ttl = int(300 * prediction["confidence"])  # 5 minutes max

                        # Create placeholder entry
                        await self.put(
                            prediction["query"],
                            {"pre_computed": True, "confidence": prediction["confidence"]},
                            ttl_seconds=ttl,
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Prediction loop error: {e}")
                await asyncio.sleep(5)
        else:
            logger.info("Semantic cache prediction loop timeout, stopping")

    async def get(self, key: str, **kwargs) -> Optional[CacheEntry]:
        """Get predictive entry from cache"""
        async with self._lock:
            if key in self.cache:
                entry = self.cache[key]

                # Dynamic TTL based on access patterns
                if entry.access_count > 5:
                    # Extend TTL for frequently accessed entries
                    entry.ttl_seconds = 600
                elif (
                    entry.access_count < 2
                    and (datetime.now() - entry.timestamp).total_seconds() > 300
                ):
                    # Expire rarely accessed entries sooner
                    self.cache.pop(key)
                    self.current_size_bytes -= entry.size_bytes
                    self.misses += 1
                    return None

                entry.access()
                self.hits += 1
                return entry

            self.misses += 1
            return None

    async def put(self, key: str, value: Any, ttl_seconds: Optional[int] = None, **kwargs) -> bool:
        """Put predictive entry in cache"""
        async with self._lock:
            # Calculate size
            size_bytes = len(pickle.dumps(value))

            # Evict if needed
            await self.evict_if_needed(size_bytes)

            # Create entry with dynamic TTL
            if ttl_seconds is None:
                ttl_seconds = 600  # Default 10 minutes

            entry = CacheEntry(
                key=key,
                value=value,
                ttl_seconds=ttl_seconds,
                cache_level=self.cache_level,
                size_bytes=size_bytes,
            )

            # Store
            self.cache[key] = entry
            self.current_size_bytes += size_bytes

            return True

    async def record_access_pattern(self, query: str, context: Dict[str, Any]):
        """Record access pattern for prediction"""
        await self.pre_compute_queue.put(
            {
                "history": self.pattern_predictor.get_recent_history(),
                "context": context,
                "timestamp": datetime.now(),
            }
        )


class PatternPredictor:
    """Simple pattern predictor for cache pre-warming"""

    def __init__(self):
        self.access_history: List[Tuple[str, datetime]] = []
        self.pattern_sequences: Dict[str, List[str]] = defaultdict(list)
        self.max_history = 1000

    def record_access(self, query: str):
        """Record query access"""
        self.access_history.append((query, datetime.now()))

        # Maintain max history size
        if len(self.access_history) > self.max_history:
            self.access_history = self.access_history[-self.max_history :]

        # Update pattern sequences
        if len(self.access_history) >= 2:
            prev_query = self.access_history[-2][0]
            self.pattern_sequences[prev_query].append(query)

    def get_recent_history(self, limit: int = 10) -> List[str]:
        """Get recent query history"""
        return [query for query, _ in self.access_history[-limit:]]

    def predict_next_queries(
        self, history: List[str], context: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Predict next likely queries"""
        predictions = []

        if not history:
            return predictions

        last_query = history[-1]

        # Check pattern sequences
        if last_query in self.pattern_sequences:
            next_queries = self.pattern_sequences[last_query]

            # Count frequencies
            query_counts = defaultdict(int)
            for query in next_queries:
                query_counts[query] += 1

            # Calculate confidence scores
            total_count = len(next_queries)
            for query, count in query_counts.items():
                confidence = count / total_count
                predictions.append({"query": query, "confidence": confidence, "source": "pattern"})

        # Sort by confidence
        predictions.sort(key=lambda x: x["confidence"], reverse=True)

        return predictions[:5]


class SemanticCacheWithLSH:
    """Main semantic cache system with multi-level architecture and memory pressure awareness"""

    def __init__(self, memory_manager=None):
        """Initialize multi-level semantic cache with optional memory manager"""
        # Initialize cache layers
        self.l1_cache = L1ExactMatchCache()
        self.l2_cache = L2SemanticSimilarityCache()
        self.l3_cache = L3ContextualCache()
        self.l4_cache = L4PredictiveCache()

        # Cache hierarchy
        self.cache_layers = [self.l1_cache, self.l2_cache, self.l3_cache, self.l4_cache]

        # Connect memory manager to all layers
        self.memory_manager = memory_manager
        if memory_manager:
            for layer in self.cache_layers:
                layer.set_memory_manager(memory_manager)
            logger.info("✅ Semantic cache connected to memory pressure manager")

        # Global statistics
        self.total_requests = 0
        self.cache_bypass_count = 0

        # Pattern tracking
        self.pattern_predictor = self.l4_cache.pattern_predictor

        # Integration points
        self.goal_system = None
        self.anomaly_detector = None

        # Start predictive caching
        self._init_task = None
        self._pressure_monitor_task = None

    async def initialize(self):
        """Initialize async components"""
        await self.l4_cache.start_prediction_loop()
        logger.info("Semantic cache with LSH initialized")

        # Start memory pressure monitoring if available
        if self.memory_manager:
            self._pressure_monitor_task = asyncio.create_task(self._monitor_memory_pressure())

    async def _monitor_memory_pressure(self):
        """Monitor memory pressure and trigger evictions when needed"""
        max_runtime = float(os.getenv("TIMEOUT_VISION_SESSION", "3600.0"))  # 1 hour default
        session_start = time.monotonic()
        try:
            while time.monotonic() - session_start < max_runtime:
                await asyncio.sleep(10)  # Check every 10 seconds

                if not self.memory_manager:
                    break

                from ..macos_memory_manager import MemoryPressure

                pressure = self.memory_manager.current_pressure

                # Trigger aggressive eviction on YELLOW or RED
                if pressure in (MemoryPressure.YELLOW, MemoryPressure.RED):
                    logger.info(f"⚠️ Memory pressure {pressure.value} - triggering cache evictions")
                    for layer in self.cache_layers:
                        await layer.evict_to_pressure_limit()
            else:
                logger.info("Semantic cache memory pressure monitoring timeout, stopping")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in memory pressure monitoring: {e}")

    async def shutdown(self):
        """Shutdown async components"""
        if self._pressure_monitor_task:
            self._pressure_monitor_task.cancel()
            try:
                await self._pressure_monitor_task
            except asyncio.CancelledError:
                pass
        await self.l4_cache.stop_prediction_loop()

    async def get(
        self,
        key: str,
        context: Optional[Dict[str, Any]] = None,
        embedding: Optional[np.ndarray] = None,
        bypass_cache: bool = False,
    ) -> Optional[Tuple[Any, CacheLevel, float]]:
        """
        Get value from cache hierarchy

        Returns:
            Tuple of (value, cache_level, similarity_score) or None
        """
        self.total_requests += 1

        # Check bypass conditions
        if bypass_cache:
            self.cache_bypass_count += 1
            return None

        # Record access pattern
        self.pattern_predictor.record_access(key)

        # Try each cache level
        # L1: Exact match
        entry = await self.l1_cache.get(key)
        if entry:
            return entry.value, CacheLevel.L1_EXACT, 1.0

        # L2: Semantic similarity
        if embedding is not None or key:
            entry = await self.l2_cache.get(key, query_embedding=embedding)
            if entry:
                return entry.value, CacheLevel.L2_SEMANTIC, entry.similarity_score

        # L3: Contextual
        if context:
            entry = await self.l3_cache.get(key, context=context)
            if entry:
                return entry.value, CacheLevel.L3_CONTEXTUAL, 0.9

        # L4: Predictive
        entry = await self.l4_cache.get(key)
        if entry:
            return entry.value, CacheLevel.L4_PREDICTIVE, 0.8

        return None

    async def put(
        self,
        key: str,
        value: Any,
        context: Optional[Dict[str, Any]] = None,
        embedding: Optional[np.ndarray] = None,
        cache_levels: Optional[List[CacheLevel]] = None,
    ):
        """
        Put value in appropriate cache levels
        """
        if cache_levels is None:
            # Determine appropriate cache levels based on value characteristics
            cache_levels = self._determine_cache_levels(value, context, embedding)

        # Store in specified cache levels
        for level in cache_levels:
            if level == CacheLevel.L1_EXACT:
                await self.l1_cache.put(key, value)
            elif level == CacheLevel.L2_SEMANTIC and embedding is not None:
                await self.l2_cache.put(key, value, embedding=embedding)
            elif level == CacheLevel.L3_CONTEXTUAL and context is not None:
                await self.l3_cache.put(key, value, context=context)
            elif level == CacheLevel.L4_PREDICTIVE:
                await self.l4_cache.put(key, value)

        # Update predictive cache
        if context:
            await self.l4_cache.record_access_pattern(key, context)

    def _determine_cache_levels(
        self, value: Any, context: Optional[Dict[str, Any]], embedding: Optional[np.ndarray]
    ) -> List[CacheLevel]:
        """Determine appropriate cache levels for value"""
        levels = []

        # Always try L1 for recent/exact matches
        levels.append(CacheLevel.L1_EXACT)

        # L2 if we have embeddings
        if embedding is not None:
            levels.append(CacheLevel.L2_SEMANTIC)

        # L3 if we have rich context
        if context and len(context) > 2:
            levels.append(CacheLevel.L3_CONTEXTUAL)

        # L4 for patterns (selectively)
        if context and context.get("predictable", False):
            levels.append(CacheLevel.L4_PREDICTIVE)

        return levels

    def get_statistics(self) -> Dict[str, Any]:
        """Get comprehensive cache statistics with memory pressure info"""
        stats = {
            "total_requests": self.total_requests,
            "cache_bypass_count": self.cache_bypass_count,
            "overall_hit_rate": 0.0,
            "memory_usage_mb": 0.0,
            "effective_limit_mb": 0.0,
            "layers": {},
        }

        total_hits = 0
        total_memory = 0.0
        total_effective_limit = 0.0

        for layer in self.cache_layers:
            layer_stats = layer.get_stats()
            stats["layers"][layer_stats["level"]] = layer_stats
            total_hits += layer_stats["hits"]
            total_memory += layer_stats["current_size_mb"]
            total_effective_limit += layer_stats["effective_limit_mb"]

        if self.total_requests > 0:
            stats["overall_hit_rate"] = total_hits / self.total_requests

        stats["memory_usage_mb"] = total_memory
        stats["effective_limit_mb"] = total_effective_limit

        # Add memory pressure info if available
        if self.memory_manager:
            stats["memory_pressure"] = self.memory_manager.get_stats_summary()

        return stats

    def set_integration_points(self, goal_system=None, anomaly_detector=None):
        """Set integration points for intelligent caching"""
        self.goal_system = goal_system
        self.anomaly_detector = anomaly_detector

    def should_bypass_cache(self, context: Dict[str, Any]) -> bool:
        """Determine if cache should be bypassed"""
        # Bypass for anomalies
        if self.anomaly_detector and context.get("anomaly_detected", False):
            return True

        # Bypass for critical operations
        if context.get("priority", "normal") == "critical":
            return True

        # Bypass for real-time requirements
        if context.get("real_time", False):
            return True

        return False


# Global instance management
_semantic_cache_instance: Optional[SemanticCacheWithLSH] = None


async def get_semantic_cache(memory_manager=None) -> SemanticCacheWithLSH:
    """Get global semantic cache instance with optional memory manager"""
    global _semantic_cache_instance
    if _semantic_cache_instance is None:
        _semantic_cache_instance = SemanticCacheWithLSH(memory_manager=memory_manager)
        await _semantic_cache_instance.initialize()
    return _semantic_cache_instance


# Export main classes
__all__ = ["SemanticCacheWithLSH", "CacheLevel", "CacheEntry", "get_semantic_cache", "LSHIndex"]
