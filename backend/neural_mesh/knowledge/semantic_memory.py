"""
JARVIS Neural Mesh - Advanced Semantic Memory & Retrieval System

A production-grade semantic memory system providing:
- Vector similarity search for "What did I do last time this happened?"
- Episodic memory (past experiences/events)
- Procedural memory (how-to knowledge)
- Semantic memory (facts and concepts)
- Working memory (short-term context)
- Automatic pattern recognition across past executions
- Memory consolidation and pruning
- Cross-agent knowledge sharing
- Temporal reasoning over memory

Uses ChromaDB 1.3+ with the new PersistentClient API.

Performance Targets:
- Query latency: <50ms at p95
- Memory footprint: ~500MB for 100K entries
- Embedding generation: <100ms per entry
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from backend.core.async_safety import LazyAsyncLock

import numpy as np

# ChromaDB imports with new API
try:
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
    CHROMADB_VERSION = chromadb.__version__
except ImportError:
    CHROMADB_AVAILABLE = False
    CHROMADB_VERSION = "0.0.0"
    chromadb = None

# Sentence transformers for embeddings
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    SentenceTransformer = None

logger = logging.getLogger(__name__)


# =============================================================================
# Enums and Data Types
# =============================================================================

class MemoryType(Enum):
    """Types of memory in the semantic memory system."""
    EPISODIC = "episodic"       # Past experiences and events
    SEMANTIC = "semantic"       # Facts and concepts
    PROCEDURAL = "procedural"   # How-to knowledge and skills
    WORKING = "working"         # Short-term context
    EMOTIONAL = "emotional"     # Emotional associations
    SPATIAL = "spatial"         # Location/context-based memory
    TEMPORAL = "temporal"       # Time-based patterns


class MemoryPriority(Enum):
    """Priority levels for memory entries."""
    CRITICAL = 5    # Never forget
    HIGH = 4        # Very important
    NORMAL = 3      # Standard importance
    LOW = 2         # Can be pruned
    EPHEMERAL = 1   # Short-term only


class RetrievalStrategy(Enum):
    """Strategies for memory retrieval."""
    SEMANTIC = "semantic"           # Vector similarity
    TEMPORAL = "temporal"           # Time-based
    CONTEXTUAL = "contextual"       # Context matching
    ASSOCIATIVE = "associative"     # Related memories
    HYBRID = "hybrid"               # Combination


@dataclass
class MemoryMetadata:
    """Rich metadata for memory entries."""
    source_agent: str
    created_at: datetime = field(default_factory=datetime.now)
    accessed_at: datetime = field(default_factory=datetime.now)
    access_count: int = 0
    importance_score: float = 0.5
    decay_rate: float = 0.1
    emotional_valence: float = 0.0  # -1 (negative) to +1 (positive)
    context_tags: Set[str] = field(default_factory=set)
    related_memories: List[str] = field(default_factory=list)
    consolidation_count: int = 0
    last_consolidated: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "source_agent": self.source_agent,
            "created_at": self.created_at.isoformat(),
            "accessed_at": self.accessed_at.isoformat(),
            "access_count": self.access_count,
            "importance_score": self.importance_score,
            "decay_rate": self.decay_rate,
            "emotional_valence": self.emotional_valence,
            "context_tags": list(self.context_tags),
            "related_memories": self.related_memories,
            "consolidation_count": self.consolidation_count,
            "last_consolidated": self.last_consolidated.isoformat() if self.last_consolidated else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryMetadata":
        """Create from dictionary."""
        return cls(
            source_agent=data.get("source_agent", "unknown"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(),
            accessed_at=datetime.fromisoformat(data["accessed_at"]) if data.get("accessed_at") else datetime.now(),
            access_count=data.get("access_count", 0),
            importance_score=data.get("importance_score", 0.5),
            decay_rate=data.get("decay_rate", 0.1),
            emotional_valence=data.get("emotional_valence", 0.0),
            context_tags=set(data.get("context_tags", [])),
            related_memories=data.get("related_memories", []),
            consolidation_count=data.get("consolidation_count", 0),
            last_consolidated=datetime.fromisoformat(data["last_consolidated"]) if data.get("last_consolidated") else None,
        )


@dataclass
class MemoryEntry:
    """A single memory entry in the semantic memory system."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    memory_type: MemoryType = MemoryType.SEMANTIC
    priority: MemoryPriority = MemoryPriority.NORMAL
    content: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[np.ndarray] = None
    metadata: MemoryMetadata = field(default_factory=lambda: MemoryMetadata(source_agent="system"))
    ttl_seconds: Optional[float] = None
    expires_at: Optional[datetime] = None

    def __post_init__(self):
        """Set expiration based on TTL."""
        if self.ttl_seconds and not self.expires_at:
            self.expires_at = datetime.now() + timedelta(seconds=self.ttl_seconds)

    def is_expired(self) -> bool:
        """Check if memory has expired."""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at

    def calculate_relevance(self, query_time: Optional[datetime] = None) -> float:
        """Calculate current relevance score with temporal decay."""
        query_time = query_time or datetime.now()

        # Base importance
        base_score = self.metadata.importance_score

        # Temporal decay
        age_hours = (query_time - self.metadata.created_at).total_seconds() / 3600
        temporal_factor = np.exp(-self.metadata.decay_rate * age_hours / 24)

        # Access recency boost
        recency_hours = (query_time - self.metadata.accessed_at).total_seconds() / 3600
        recency_factor = 1.0 + (0.5 * np.exp(-recency_hours / 12))

        # Access frequency boost
        frequency_factor = 1.0 + (0.1 * min(self.metadata.access_count, 50))

        # Priority multiplier
        priority_multiplier = self.priority.value / 3.0

        return base_score * temporal_factor * recency_factor * frequency_factor * priority_multiplier

    def to_document(self) -> str:
        """Convert to searchable document."""
        parts = [self.content]
        if self.data:
            parts.append(json.dumps(self.data, default=str))
        parts.extend(self.metadata.context_tags)
        return " ".join(parts)


@dataclass
class MemoryQueryResult:
    """Result of a memory query."""
    entry: MemoryEntry
    similarity_score: float
    relevance_score: float
    combined_score: float
    distance: float = 0.0


@dataclass
class MemoryStats:
    """Statistics for the semantic memory system."""
    total_memories: int = 0
    memories_by_type: Dict[str, int] = field(default_factory=dict)
    memories_by_priority: Dict[str, int] = field(default_factory=dict)
    total_queries: int = 0
    average_query_time_ms: float = 0.0
    cache_hit_rate: float = 0.0
    total_consolidations: int = 0
    total_pruned: int = 0
    embedding_model: str = ""
    chromadb_version: str = ""


# =============================================================================
# Embedding Provider Interface
# =============================================================================

class EmbeddingProvider(ABC):
    """Abstract interface for embedding generation."""

    @abstractmethod
    async def embed(self, text: str) -> np.ndarray:
        """Generate embedding for text."""
        pass

    @abstractmethod
    async def embed_batch(self, texts: List[str]) -> List[np.ndarray]:
        """Generate embeddings for multiple texts."""
        pass

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Embedding dimension."""
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model name."""
        pass


class SentenceTransformerProvider(EmbeddingProvider):
    """Embedding provider using sentence-transformers."""

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        device: str = "cpu",
    ):
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            raise RuntimeError("sentence-transformers not installed")

        self._model_name = model_name
        self._device = device
        self._model: Optional[SentenceTransformer] = None
        self._lock = asyncio.Lock()

    async def _ensure_model(self):
        """Lazy load the model."""
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    # Run in executor to not block
                    loop = asyncio.get_event_loop()
                    self._model = await loop.run_in_executor(
                        None,
                        lambda: SentenceTransformer(self._model_name, device=self._device)
                    )
                    logger.info(f"Loaded embedding model: {self._model_name}")

    async def embed(self, text: str) -> np.ndarray:
        """Generate embedding for text."""
        await self._ensure_model()
        loop = asyncio.get_event_loop()
        # SAFETY: Capture model reference BEFORE passing to executor
        model_ref = self._model
        if model_ref is None:
            raise RuntimeError("Model not loaded for embedding")
        return await loop.run_in_executor(
            None,
            lambda: model_ref.encode(text, convert_to_numpy=True)
        )

    async def embed_batch(self, texts: List[str]) -> List[np.ndarray]:
        """Generate embeddings for multiple texts."""
        await self._ensure_model()
        loop = asyncio.get_event_loop()
        # SAFETY: Capture model reference BEFORE passing to executor
        model_ref = self._model
        if model_ref is None:
            raise RuntimeError("Model not loaded for batch embedding")
        embeddings = await loop.run_in_executor(
            None,
            lambda: model_ref.encode(texts, convert_to_numpy=True)
        )
        return list(embeddings)

    @property
    def dimension(self) -> int:
        """Embedding dimension."""
        # Common model dimensions
        dimensions = {
            "all-MiniLM-L6-v2": 384,
            "all-MiniLM-L12-v2": 384,
            "all-mpnet-base-v2": 768,
            "paraphrase-MiniLM-L6-v2": 384,
        }
        return dimensions.get(self._model_name, 384)

    @property
    def model_name(self) -> str:
        return self._model_name


class MockEmbeddingProvider(EmbeddingProvider):
    """Mock embedding provider for testing without ML dependencies."""

    def __init__(self, dimension: int = 384):
        self._dimension = dimension

    async def embed(self, text: str) -> np.ndarray:
        """Generate deterministic embedding based on text hash."""
        # Create deterministic embedding from text
        text_hash = hashlib.sha256(text.encode()).digest()
        np.random.seed(int.from_bytes(text_hash[:4], 'big'))
        embedding = np.random.randn(self._dimension).astype(np.float32)
        return embedding / np.linalg.norm(embedding)

    async def embed_batch(self, texts: List[str]) -> List[np.ndarray]:
        """Generate embeddings for multiple texts."""
        return [await self.embed(text) for text in texts]

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return "mock-embedding"


# =============================================================================
# LRU Cache for Query Results
# =============================================================================

class AsyncLRUCache:
    """Thread-safe async LRU cache with TTL support."""

    def __init__(self, capacity: int = 1000, default_ttl: float = 300.0):
        self._capacity = capacity
        self._default_ttl = default_ttl
        self._cache: OrderedDict[str, Tuple[Any, float, float]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    async def get(self, key: str, ttl: Optional[float] = None) -> Optional[Any]:
        """Get item from cache if not expired."""
        ttl = ttl or self._default_ttl
        async with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None

            value, timestamp, _ = self._cache[key]
            if time.time() - timestamp > ttl:
                del self._cache[key]
                self._misses += 1
                return None

            # Move to end (most recently used)
            self._cache.move_to_end(key)
            self._hits += 1
            return value

    async def put(self, key: str, value: Any, ttl: Optional[float] = None):
        """Add item to cache."""
        ttl = ttl or self._default_ttl
        async with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            else:
                if len(self._cache) >= self._capacity:
                    self._cache.popitem(last=False)

            self._cache[key] = (value, time.time(), ttl)

    async def invalidate(self, key: str):
        """Remove item from cache."""
        async with self._lock:
            self._cache.pop(key, None)

    async def clear(self):
        """Clear all cache entries."""
        async with self._lock:
            self._cache.clear()

    @property
    def hit_rate(self) -> float:
        """Calculate cache hit rate."""
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    @property
    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return {
            "size": len(self._cache),
            "capacity": self._capacity,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self.hit_rate,
        }


# =============================================================================
# Main Semantic Memory Class
# =============================================================================

class SemanticMemory:
    """
    Advanced semantic memory system with vector similarity search.

    Provides human-like memory capabilities:
    - "What happened last time?" - Episodic recall
    - "How do I do this?" - Procedural knowledge
    - "What is this?" - Semantic understanding
    - "What's relevant now?" - Working memory

    Features:
    - Multiple memory types (episodic, semantic, procedural, working)
    - Vector similarity search using ChromaDB
    - Temporal decay and relevance scoring
    - Memory consolidation and pruning
    - Cross-agent knowledge sharing
    - Pattern recognition across past executions

    Usage:
        memory = SemanticMemory()
        await memory.initialize()

        # Store a memory
        entry = await memory.store(
            content="Fixed TypeError by adding null check",
            memory_type=MemoryType.PROCEDURAL,
            data={"error": "TypeError", "solution": "null check"},
            agent_name="debug_agent",
        )

        # Query similar memories
        results = await memory.recall(
            query="How to fix TypeError?",
            memory_types=[MemoryType.PROCEDURAL],
            limit=5,
        )

        # Get memories from similar context
        context_memories = await memory.recall_similar_context(
            context={"file": "main.py", "function": "process_data"},
            limit=10,
        )
    """

    def __init__(
        self,
        persist_directory: Optional[str] = None,
        collection_name: str = "jarvis_semantic_memory",
        embedding_provider: Optional[EmbeddingProvider] = None,
        cache_capacity: int = 1000,
        cache_ttl: float = 300.0,
        enable_consolidation: bool = True,
        consolidation_interval: float = 3600.0,  # 1 hour
        enable_pruning: bool = True,
        pruning_threshold: float = 0.1,  # Prune memories with relevance < 0.1
    ):
        """
        Initialize semantic memory.

        Args:
            persist_directory: Directory for ChromaDB persistence
            collection_name: Name of the ChromaDB collection
            embedding_provider: Custom embedding provider (defaults to sentence-transformers)
            cache_capacity: Maximum cache entries
            cache_ttl: Cache entry TTL in seconds
            enable_consolidation: Whether to run memory consolidation
            consolidation_interval: Interval between consolidation runs
            enable_pruning: Whether to prune low-relevance memories
            pruning_threshold: Minimum relevance to keep
        """
        self._persist_directory = persist_directory or str(
            Path.home() / ".jarvis" / "semantic_memory"
        )
        self._collection_name = collection_name

        # ChromaDB client and collection
        self._client: Optional[Any] = None
        self._collection: Optional[Any] = None

        # Embedding provider
        self._embedding_provider = embedding_provider

        # In-memory storage
        self._memories: Dict[str, MemoryEntry] = {}
        self._memories_by_type: Dict[MemoryType, Set[str]] = defaultdict(set)

        # Cache
        self._cache = AsyncLRUCache(capacity=cache_capacity, default_ttl=cache_ttl)

        # Configuration
        self._enable_consolidation = enable_consolidation
        self._consolidation_interval = consolidation_interval
        self._enable_pruning = enable_pruning
        self._pruning_threshold = pruning_threshold

        # Background tasks
        self._consolidation_task: Optional[asyncio.Task] = None
        self._pruning_task: Optional[asyncio.Task] = None

        # Statistics
        self._stats = MemoryStats()
        self._query_times: List[float] = []

        # Locks
        self._write_lock = asyncio.Lock()

        # State
        self._initialized = False

        logger.info("SemanticMemory created")

    async def initialize(self):
        """Initialize the semantic memory system."""
        if self._initialized:
            return

        logger.info("Initializing SemanticMemory...")

        # Initialize embedding provider
        if self._embedding_provider is None:
            if SENTENCE_TRANSFORMERS_AVAILABLE:
                self._embedding_provider = SentenceTransformerProvider()
            else:
                logger.warning("sentence-transformers not available, using mock embeddings")
                self._embedding_provider = MockEmbeddingProvider()

        self._stats.embedding_model = self._embedding_provider.model_name

        # Initialize ChromaDB with new API
        if CHROMADB_AVAILABLE:
            try:
                # Create persist directory
                Path(self._persist_directory).mkdir(parents=True, exist_ok=True)

                # Use the new PersistentClient API (ChromaDB 1.0+)
                self._client = chromadb.PersistentClient(
                    path=self._persist_directory,
                    settings=Settings(
                        anonymized_telemetry=False,
                        allow_reset=True,
                    )
                )

                # Get or create collection
                self._collection = self._client.get_or_create_collection(
                    name=self._collection_name,
                    metadata={
                        "hnsw:space": "cosine",
                        "hnsw:construction_ef": 200,
                        "hnsw:M": 16,
                    },
                    embedding_function=None,  # We provide embeddings ourselves
                )

                # Load existing memories count
                self._stats.total_memories = self._collection.count()
                self._stats.chromadb_version = CHROMADB_VERSION

                logger.info(
                    f"ChromaDB initialized with {self._stats.total_memories} memories "
                    f"(version {CHROMADB_VERSION})"
                )

            except Exception as e:
                logger.exception(f"Failed to initialize ChromaDB: {e}")
                self._client = None
                self._collection = None
        else:
            logger.warning("ChromaDB not available, using in-memory storage only")

        # Start background tasks
        if self._enable_consolidation:
            self._consolidation_task = asyncio.create_task(
                self._consolidation_loop(),
                name="memory_consolidation"
            )

        if self._enable_pruning:
            self._pruning_task = asyncio.create_task(
                self._pruning_loop(),
                name="memory_pruning"
            )

        self._initialized = True
        logger.info("SemanticMemory initialized successfully")

    async def close(self):
        """Close the semantic memory system."""
        logger.info("Closing SemanticMemory...")

        # Cancel background tasks
        for task in [self._consolidation_task, self._pruning_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Clear cache
        await self._cache.clear()

        self._initialized = False
        logger.info("SemanticMemory closed")

    # =========================================================================
    # Storage Methods
    # =========================================================================

    async def store(
        self,
        content: str,
        memory_type: MemoryType = MemoryType.SEMANTIC,
        priority: MemoryPriority = MemoryPriority.NORMAL,
        data: Optional[Dict[str, Any]] = None,
        agent_name: str = "system",
        context_tags: Optional[Set[str]] = None,
        importance: float = 0.5,
        ttl_seconds: Optional[float] = None,
        emotional_valence: float = 0.0,
    ) -> MemoryEntry:
        """
        Store a memory.

        Args:
            content: Main content of the memory
            memory_type: Type of memory
            priority: Priority level
            data: Additional structured data
            agent_name: Source agent
            context_tags: Tags for context matching
            importance: Importance score (0-1)
            ttl_seconds: Time to live
            emotional_valence: Emotional association (-1 to +1)

        Returns:
            Created memory entry
        """
        async with self._write_lock:
            # Create metadata
            metadata = MemoryMetadata(
                source_agent=agent_name,
                importance_score=importance,
                emotional_valence=emotional_valence,
                context_tags=context_tags or set(),
            )

            # Create entry
            entry = MemoryEntry(
                memory_type=memory_type,
                priority=priority,
                content=content,
                data=data or {},
                metadata=metadata,
                ttl_seconds=ttl_seconds,
            )

            # Generate embedding
            document = entry.to_document()
            entry.embedding = await self._embedding_provider.embed(document)

            # Store in ChromaDB
            if self._collection is not None:
                try:
                    self._collection.add(
                        ids=[entry.id],
                        embeddings=[entry.embedding.tolist()],
                        metadatas=[{
                            "memory_type": entry.memory_type.value,
                            "priority": entry.priority.value,
                            "source_agent": metadata.source_agent,
                            "importance_score": metadata.importance_score,
                            "created_at": metadata.created_at.isoformat(),
                            "context_tags": ",".join(metadata.context_tags),
                            "emotional_valence": metadata.emotional_valence,
                        }],
                        documents=[document],
                    )
                except Exception as e:
                    logger.exception(f"Failed to store in ChromaDB: {e}")

            # Store in memory
            self._memories[entry.id] = entry
            self._memories_by_type[entry.memory_type].add(entry.id)

            # Update stats
            self._stats.total_memories += 1
            type_key = entry.memory_type.value
            self._stats.memories_by_type[type_key] = \
                self._stats.memories_by_type.get(type_key, 0) + 1
            priority_key = entry.priority.name
            self._stats.memories_by_priority[priority_key] = \
                self._stats.memories_by_priority.get(priority_key, 0) + 1

            # Invalidate cache
            await self._cache.clear()

            logger.debug(
                f"Stored memory {entry.id[:8]} "
                f"(type={entry.memory_type.value}, agent={agent_name})"
            )

            return entry

    async def store_experience(
        self,
        description: str,
        outcome: str,
        context: Dict[str, Any],
        agent_name: str,
        success: bool = True,
        lessons_learned: Optional[List[str]] = None,
    ) -> MemoryEntry:
        """
        Store an episodic experience memory.

        This is ideal for "What happened last time?" queries.

        Args:
            description: What happened
            outcome: The result
            context: Context data (file, function, error type, etc.)
            agent_name: Agent that had this experience
            success: Whether the outcome was successful
            lessons_learned: Key takeaways

        Returns:
            Created memory entry
        """
        content = f"{description}. Outcome: {outcome}"
        if lessons_learned:
            content += f". Lessons: {', '.join(lessons_learned)}"

        data = {
            "description": description,
            "outcome": outcome,
            "context": context,
            "success": success,
            "lessons_learned": lessons_learned or [],
        }

        # Successful experiences are more important
        importance = 0.7 if success else 0.6
        emotional_valence = 0.3 if success else -0.2

        return await self.store(
            content=content,
            memory_type=MemoryType.EPISODIC,
            priority=MemoryPriority.HIGH if not success else MemoryPriority.NORMAL,
            data=data,
            agent_name=agent_name,
            context_tags=set(context.keys()),
            importance=importance,
            emotional_valence=emotional_valence,
        )

    async def store_procedure(
        self,
        task: str,
        steps: List[str],
        prerequisites: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
        agent_name: str = "system",
    ) -> MemoryEntry:
        """
        Store procedural knowledge (how-to).

        Args:
            task: What this procedure accomplishes
            steps: Step-by-step instructions
            prerequisites: What's needed before starting
            warnings: Things to watch out for
            agent_name: Source agent

        Returns:
            Created memory entry
        """
        content = f"How to {task}: {' -> '.join(steps)}"
        if warnings:
            content += f". Warning: {', '.join(warnings)}"

        data = {
            "task": task,
            "steps": steps,
            "prerequisites": prerequisites or [],
            "warnings": warnings or [],
        }

        return await self.store(
            content=content,
            memory_type=MemoryType.PROCEDURAL,
            priority=MemoryPriority.HIGH,
            data=data,
            agent_name=agent_name,
            context_tags={"procedure", "how-to", task.lower().replace(" ", "_")},
            importance=0.8,
        )

    async def store_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        confidence: float = 1.0,
        source: str = "system",
    ) -> MemoryEntry:
        """
        Store a semantic fact (subject-predicate-object).

        Args:
            subject: Subject of the fact
            predicate: Relationship/predicate
            obj: Object of the fact
            confidence: How confident we are (0-1)
            source: Source of this fact

        Returns:
            Created memory entry
        """
        content = f"{subject} {predicate} {obj}"

        data = {
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "confidence": confidence,
            "source": source,
        }

        return await self.store(
            content=content,
            memory_type=MemoryType.SEMANTIC,
            priority=MemoryPriority.NORMAL,
            data=data,
            agent_name=source,
            context_tags={subject.lower(), obj.lower()},
            importance=confidence * 0.7,
        )

    # =========================================================================
    # Retrieval Methods
    # =========================================================================

    async def recall(
        self,
        query: str,
        memory_types: Optional[List[MemoryType]] = None,
        agent_filter: Optional[str] = None,
        min_importance: float = 0.0,
        min_relevance: float = 0.0,
        limit: int = 10,
        strategy: RetrievalStrategy = RetrievalStrategy.HYBRID,
        include_expired: bool = False,
    ) -> List[MemoryQueryResult]:
        """
        Recall memories matching a query.

        Args:
            query: Search query
            memory_types: Filter to specific types
            agent_filter: Filter to specific agent's memories
            min_importance: Minimum importance score
            min_relevance: Minimum relevance score
            limit: Maximum results
            strategy: Retrieval strategy
            include_expired: Include expired memories

        Returns:
            List of matching memories with scores
        """
        start_time = time.perf_counter()

        # Check cache
        cache_key = hashlib.sha256(
            f"{query}:{memory_types}:{agent_filter}:{min_importance}:{limit}".encode()
        ).hexdigest()

        cached = await self._cache.get(cache_key)
        if cached is not None:
            return cached

        results: List[MemoryQueryResult] = []

        # Generate query embedding
        query_embedding = await self._embedding_provider.embed(query)

        # Query ChromaDB
        if self._collection is not None:
            try:
                # Build where filter
                where_filter = {}
                if memory_types:
                    where_filter["memory_type"] = {
                        "$in": [mt.value for mt in memory_types]
                    }
                if agent_filter:
                    where_filter["source_agent"] = agent_filter
                if min_importance > 0:
                    where_filter["importance_score"] = {"$gte": min_importance}

                # Query
                query_result = self._collection.query(
                    query_embeddings=[query_embedding.tolist()],
                    n_results=min(limit * 3, 100),  # Get more for filtering
                    where=where_filter if where_filter else None,
                    include=["metadatas", "documents", "distances", "embeddings"],
                )

                # Process results
                if query_result["ids"] and query_result["ids"][0]:
                    for i, memory_id in enumerate(query_result["ids"][0]):
                        # Get or reconstruct entry
                        if memory_id in self._memories:
                            entry = self._memories[memory_id]
                        else:
                            # Reconstruct from ChromaDB
                            entry = self._reconstruct_entry(
                                memory_id,
                                query_result["metadatas"][0][i],
                                query_result["documents"][0][i],
                                query_result["embeddings"][0][i] if query_result.get("embeddings") else None,
                            )
                            self._memories[memory_id] = entry

                        # Skip expired
                        if not include_expired and entry.is_expired():
                            continue

                        # Calculate scores
                        distance = query_result["distances"][0][i]
                        similarity = 1.0 - distance  # Cosine distance to similarity
                        relevance = entry.calculate_relevance()

                        # Skip low relevance
                        if relevance < min_relevance:
                            continue

                        # Combined score (weighted)
                        combined = (similarity * 0.6) + (relevance * 0.4)

                        results.append(MemoryQueryResult(
                            entry=entry,
                            similarity_score=similarity,
                            relevance_score=relevance,
                            combined_score=combined,
                            distance=distance,
                        ))

                        # Update access metadata
                        entry.metadata.access_count += 1
                        entry.metadata.accessed_at = datetime.now()

            except Exception as e:
                logger.exception(f"ChromaDB query failed: {e}")

        # Fallback to in-memory search if no results
        if not results:
            results = await self._in_memory_search(
                query_embedding, memory_types, agent_filter, min_importance, limit
            )

        # Sort by combined score and limit
        results.sort(key=lambda r: r.combined_score, reverse=True)
        results = results[:limit]

        # Cache results
        await self._cache.put(cache_key, results)

        # Update stats
        query_time = (time.perf_counter() - start_time) * 1000
        self._query_times.append(query_time)
        if len(self._query_times) > 1000:
            self._query_times = self._query_times[-1000:]
        self._stats.total_queries += 1
        self._stats.average_query_time_ms = sum(self._query_times) / len(self._query_times)
        self._stats.cache_hit_rate = self._cache.hit_rate

        logger.debug(f"Recall query completed in {query_time:.2f}ms with {len(results)} results")

        return results

    async def recall_similar_context(
        self,
        context: Dict[str, Any],
        memory_types: Optional[List[MemoryType]] = None,
        limit: int = 10,
    ) -> List[MemoryQueryResult]:
        """
        Recall memories from similar contexts.

        Useful for "What did I do last time in this situation?"

        Args:
            context: Current context (file, function, error, etc.)
            memory_types: Filter to specific types
            limit: Maximum results

        Returns:
            List of memories from similar contexts
        """
        # Build query from context
        context_parts = []
        for key, value in context.items():
            if isinstance(value, str):
                context_parts.append(f"{key}: {value}")
            elif isinstance(value, (list, tuple)):
                context_parts.append(f"{key}: {', '.join(str(v) for v in value)}")
            else:
                context_parts.append(f"{key}: {value}")

        query = " ".join(context_parts)

        return await self.recall(
            query=query,
            memory_types=memory_types or [MemoryType.EPISODIC],
            limit=limit,
        )

    async def recall_recent(
        self,
        hours: float = 24.0,
        memory_types: Optional[List[MemoryType]] = None,
        agent_filter: Optional[str] = None,
        limit: int = 50,
    ) -> List[MemoryEntry]:
        """
        Recall recent memories.

        Args:
            hours: How far back to look
            memory_types: Filter to specific types
            agent_filter: Filter to specific agent
            limit: Maximum results

        Returns:
            List of recent memories
        """
        cutoff = datetime.now() - timedelta(hours=hours)

        results = []
        for entry in self._memories.values():
            if entry.metadata.created_at < cutoff:
                continue
            if memory_types and entry.memory_type not in memory_types:
                continue
            if agent_filter and entry.metadata.source_agent != agent_filter:
                continue
            results.append(entry)

        # Sort by creation time (newest first)
        results.sort(key=lambda e: e.metadata.created_at, reverse=True)
        return results[:limit]

    async def recall_by_pattern(
        self,
        pattern_type: str,
        pattern_data: Dict[str, Any],
        limit: int = 10,
    ) -> List[MemoryQueryResult]:
        """
        Recall memories matching a pattern.

        Useful for finding similar errors, similar actions, etc.

        Args:
            pattern_type: Type of pattern (error, action, context, etc.)
            pattern_data: Pattern data to match
            limit: Maximum results

        Returns:
            List of matching memories
        """
        # Build query from pattern
        query_parts = [pattern_type]
        for key, value in pattern_data.items():
            query_parts.append(f"{key}={value}")

        return await self.recall(
            query=" ".join(query_parts),
            memory_types=[MemoryType.EPISODIC, MemoryType.PROCEDURAL],
            limit=limit,
        )

    async def get_by_id(self, memory_id: str) -> Optional[MemoryEntry]:
        """Get a memory by ID."""
        return self._memories.get(memory_id)

    # =========================================================================
    # Memory Management
    # =========================================================================

    async def update(
        self,
        memory_id: str,
        content: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        importance: Optional[float] = None,
    ) -> Optional[MemoryEntry]:
        """Update an existing memory."""
        async with self._write_lock:
            entry = self._memories.get(memory_id)
            if not entry:
                return None

            # Update fields
            if content:
                entry.content = content
            if data:
                entry.data.update(data)
            if importance is not None:
                entry.metadata.importance_score = importance

            # Regenerate embedding
            document = entry.to_document()
            entry.embedding = await self._embedding_provider.embed(document)

            # Update in ChromaDB
            if self._collection is not None:
                try:
                    self._collection.update(
                        ids=[memory_id],
                        embeddings=[entry.embedding.tolist()],
                        documents=[document],
                        metadatas=[{
                            "memory_type": entry.memory_type.value,
                            "priority": entry.priority.value,
                            "source_agent": entry.metadata.source_agent,
                            "importance_score": entry.metadata.importance_score,
                            "created_at": entry.metadata.created_at.isoformat(),
                            "context_tags": ",".join(entry.metadata.context_tags),
                            "emotional_valence": entry.metadata.emotional_valence,
                        }],
                    )
                except Exception as e:
                    logger.exception(f"Failed to update in ChromaDB: {e}")

            await self._cache.clear()
            return entry

    async def delete(self, memory_id: str) -> bool:
        """Delete a memory."""
        async with self._write_lock:
            if memory_id not in self._memories:
                return False

            entry = self._memories.pop(memory_id)
            self._memories_by_type[entry.memory_type].discard(memory_id)

            # Delete from ChromaDB
            if self._collection is not None:
                try:
                    self._collection.delete(ids=[memory_id])
                except Exception as e:
                    logger.warning(f"Failed to delete from ChromaDB: {e}")

            # Update stats
            self._stats.total_memories -= 1
            type_key = entry.memory_type.value
            if type_key in self._stats.memories_by_type:
                self._stats.memories_by_type[type_key] -= 1

            await self._cache.clear()
            return True

    async def strengthen(self, memory_id: str, boost: float = 0.1):
        """Strengthen a memory (increase importance)."""
        entry = self._memories.get(memory_id)
        if entry:
            entry.metadata.importance_score = min(1.0, entry.metadata.importance_score + boost)
            entry.metadata.access_count += 1
            entry.metadata.accessed_at = datetime.now()

    async def weaken(self, memory_id: str, decay: float = 0.1):
        """Weaken a memory (decrease importance)."""
        entry = self._memories.get(memory_id)
        if entry:
            entry.metadata.importance_score = max(0.0, entry.metadata.importance_score - decay)

    # =========================================================================
    # Background Tasks
    # =========================================================================

    async def _consolidation_loop(self):
        """Periodically consolidate memories."""
        max_runtime = float(os.getenv("TIMEOUT_MEMORY_CONSOLIDATION_SESSION", "86400.0"))  # 24 hours
        consolidation_timeout = float(os.getenv("TIMEOUT_MEMORY_CONSOLIDATION_ITERATION", "120.0"))
        start = time.time()

        while time.time() - start < max_runtime:
            try:
                await asyncio.sleep(self._consolidation_interval)
                await asyncio.wait_for(self._consolidate_memories(), timeout=consolidation_timeout)
            except asyncio.TimeoutError:
                logger.warning("Memory consolidation iteration timed out")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in consolidation loop: {e}")

        logger.info("Memory consolidation loop reached max runtime, exiting")

    async def _consolidate_memories(self):
        """
        Consolidate memories by:
        - Merging similar memories
        - Strengthening frequently accessed memories
        - Creating summary memories from clusters
        """
        logger.debug("Running memory consolidation...")

        # Find memories to consolidate
        consolidation_candidates = []
        for entry in self._memories.values():
            if entry.metadata.consolidation_count < 3:  # Max 3 consolidations
                consolidation_candidates.append(entry)

        if len(consolidation_candidates) < 10:
            return

        # Group by memory type
        type_groups: Dict[MemoryType, List[MemoryEntry]] = defaultdict(list)
        for entry in consolidation_candidates:
            type_groups[entry.memory_type].append(entry)

        consolidated_count = 0
        for memory_type, entries in type_groups.items():
            if len(entries) < 5:
                continue

            # Strengthen frequently accessed memories
            for entry in entries:
                if entry.metadata.access_count > 5:
                    entry.metadata.importance_score = min(
                        1.0,
                        entry.metadata.importance_score * 1.1
                    )
                    entry.metadata.consolidation_count += 1
                    entry.metadata.last_consolidated = datetime.now()
                    consolidated_count += 1

        if consolidated_count > 0:
            self._stats.total_consolidations += consolidated_count
            logger.info(f"Consolidated {consolidated_count} memories")

    async def _pruning_loop(self):
        """Periodically prune low-relevance memories."""
        max_runtime = float(os.getenv("TIMEOUT_MEMORY_PRUNING_SESSION", "86400.0"))  # 24 hours
        pruning_timeout = float(os.getenv("TIMEOUT_MEMORY_PRUNING_ITERATION", "120.0"))
        start = time.time()

        while time.time() - start < max_runtime:
            try:
                await asyncio.sleep(self._consolidation_interval * 2)  # Less frequent than consolidation
                await asyncio.wait_for(self._prune_memories(), timeout=pruning_timeout)
            except asyncio.TimeoutError:
                logger.warning("Memory pruning iteration timed out")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in pruning loop: {e}")

        logger.info("Memory pruning loop reached max runtime, exiting")

    async def _prune_memories(self):
        """Prune memories with very low relevance."""
        logger.debug("Running memory pruning...")

        to_prune = []
        for memory_id, entry in self._memories.items():
            # Never prune critical memories
            if entry.priority == MemoryPriority.CRITICAL:
                continue

            # Check relevance
            relevance = entry.calculate_relevance()
            if relevance < self._pruning_threshold:
                to_prune.append(memory_id)

        # Prune
        for memory_id in to_prune:
            await self.delete(memory_id)

        if to_prune:
            self._stats.total_pruned += len(to_prune)
            logger.info(f"Pruned {len(to_prune)} low-relevance memories")

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _reconstruct_entry(
        self,
        memory_id: str,
        metadata: Dict[str, Any],
        document: str,
        embedding: Optional[List[float]],
    ) -> MemoryEntry:
        """Reconstruct a memory entry from ChromaDB data."""
        return MemoryEntry(
            id=memory_id,
            memory_type=MemoryType(metadata.get("memory_type", "semantic")),
            priority=MemoryPriority(metadata.get("priority", 3)),
            content=document,
            data={},
            embedding=np.array(embedding) if embedding is not None and len(embedding) > 0 else None,
            metadata=MemoryMetadata(
                source_agent=metadata.get("source_agent", "unknown"),
                created_at=datetime.fromisoformat(metadata["created_at"]) if metadata.get("created_at") else datetime.now(),
                importance_score=metadata.get("importance_score", 0.5),
                emotional_valence=metadata.get("emotional_valence", 0.0),
                context_tags=set(metadata.get("context_tags", "").split(",")) if metadata.get("context_tags") else set(),
            ),
        )

    async def _in_memory_search(
        self,
        query_embedding: np.ndarray,
        memory_types: Optional[List[MemoryType]],
        agent_filter: Optional[str],
        min_importance: float,
        limit: int,
    ) -> List[MemoryQueryResult]:
        """Fallback in-memory search using cosine similarity."""
        results = []

        for entry in self._memories.values():
            # Apply filters
            if memory_types and entry.memory_type not in memory_types:
                continue
            if agent_filter and entry.metadata.source_agent != agent_filter:
                continue
            if entry.metadata.importance_score < min_importance:
                continue
            if entry.is_expired():
                continue
            if entry.embedding is None:
                continue

            # Calculate cosine similarity
            similarity = float(np.dot(query_embedding, entry.embedding) /
                             (np.linalg.norm(query_embedding) * np.linalg.norm(entry.embedding)))

            relevance = entry.calculate_relevance()
            combined = (similarity * 0.6) + (relevance * 0.4)

            results.append(MemoryQueryResult(
                entry=entry,
                similarity_score=similarity,
                relevance_score=relevance,
                combined_score=combined,
                distance=1.0 - similarity,
            ))

        results.sort(key=lambda r: r.combined_score, reverse=True)
        return results[:limit]

    # =========================================================================
    # Statistics and Info
    # =========================================================================

    def get_stats(self) -> MemoryStats:
        """Get current statistics."""
        return self._stats

    def __repr__(self) -> str:
        return (
            f"SemanticMemory("
            f"memories={self._stats.total_memories}, "
            f"queries={self._stats.total_queries}, "
            f"avg_query_time={self._stats.average_query_time_ms:.2f}ms, "
            f"cache_hit_rate={self._stats.cache_hit_rate:.2%})"
        )


# =============================================================================
# Singleton Access
# =============================================================================

_semantic_memory_instance: Optional[SemanticMemory] = None
_semantic_memory_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_semantic_memory() -> SemanticMemory:
    """Get or create the global semantic memory instance."""
    global _semantic_memory_instance

    async with _semantic_memory_lock:
        if _semantic_memory_instance is None:
            _semantic_memory_instance = SemanticMemory()
            await _semantic_memory_instance.initialize()
        return _semantic_memory_instance


async def close_semantic_memory():
    """Close the global semantic memory instance."""
    global _semantic_memory_instance

    async with _semantic_memory_lock:
        if _semantic_memory_instance is not None:
            await _semantic_memory_instance.close()
            _semantic_memory_instance = None
