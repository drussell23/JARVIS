"""
JARVIS Neural Mesh - Shared Knowledge Graph

Persistent, searchable collective memory for all agents with:
- Vector search using ChromaDB for semantic similarity
- Graph storage using NetworkX for relationship traversal
- Multiple knowledge types (errors, patterns, solutions, context)
- Automatic expiration and cleanup
- Version tracking and conflict resolution
- LRU caching for frequent queries

Performance Target: <50ms query latency at p95
Memory Footprint: ~200MB for 10,000 entries (shared across all agents)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import pickle
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

import numpy as np

try:
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    chromadb = None

try:
    import networkx as nx
    NETWORKX_AVAILABLE = True
except ImportError:
    NETWORKX_AVAILABLE = False
    nx = None

try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    SentenceTransformer = None

from ..data_models import (
    KnowledgeEntry,
    KnowledgeRelationship,
    KnowledgeType,
)
from ..config import KnowledgeGraphConfig, get_config

logger = logging.getLogger(__name__)


class LRUCache:
    """Simple LRU cache for knowledge queries."""

    def __init__(self, capacity: int = 1000) -> None:
        self.capacity = capacity
        self._cache: OrderedDict[str, Tuple[Any, float]] = OrderedDict()

    def get(self, key: str, ttl: float = 300.0) -> Optional[Any]:
        """Get item from cache if not expired."""
        if key not in self._cache:
            return None

        value, timestamp = self._cache[key]
        if time.time() - timestamp > ttl:
            del self._cache[key]
            return None

        # Move to end (most recently used)
        self._cache.move_to_end(key)
        return value

    def put(self, key: str, value: Any) -> None:
        """Add item to cache."""
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self.capacity:
                self._cache.popitem(last=False)

        self._cache[key] = (value, time.time())

    def invalidate(self, key: str) -> None:
        """Remove item from cache."""
        self._cache.pop(key, None)

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()


@dataclass
class GraphMetrics:
    """Metrics for the knowledge graph."""

    total_entries: int = 0
    total_relationships: int = 0
    entries_by_type: Dict[str, int] = field(default_factory=dict)
    queries_executed: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    average_query_time_ms: float = 0.0
    total_query_time_ms: float = 0.0

    def cache_hit_rate(self) -> float:
        """Calculate cache hit rate."""
        total = self.cache_hits + self.cache_misses
        if total == 0:
            return 0.0
        return self.cache_hits / total


class SharedKnowledgeGraph:
    """
    Persistent collective intelligence for all agents.

    Features:
    - Semantic search using vector embeddings
    - Relationship graph for knowledge connections
    - Multiple knowledge types
    - Automatic cleanup of expired entries
    - Version tracking
    - LRU caching for performance

    Usage:
        graph = SharedKnowledgeGraph()
        await graph.initialize()

        # Add knowledge
        entry = await graph.add_knowledge(
            knowledge_type=KnowledgeType.ERROR,
            agent_name="vision_agent",
            data={"error": "TypeError", "solution": "Add null check"},
            tags={"python", "error"},
        )

        # Query knowledge
        results = await graph.query(
            query="TypeError in vision processing",
            knowledge_types=[KnowledgeType.ERROR],
            limit=5,
        )

        # Add relationship
        await graph.add_relationship(
            source_id=entry.id,
            target_id=solution_id,
            relationship_type="solved_by",
        )
    """

    def __init__(self, config: Optional[KnowledgeGraphConfig] = None) -> None:
        """Initialize the knowledge graph.

        Args:
            config: Graph configuration. Uses global config if not provided.
        """
        self.config = config or get_config().knowledge_graph

        # Vector database (ChromaDB)
        self._vector_db: Optional[Any] = None
        self._collection: Optional[Any] = None

        # Embedding model
        self._embedder: Optional[Any] = None

        # Graph database (NetworkX)
        self._graph: Optional[Any] = None

        # In-memory storage (fallback if ChromaDB not available)
        self._entries: Dict[str, KnowledgeEntry] = {}
        self._relationships: Dict[str, KnowledgeRelationship] = {}

        # Query cache
        self._cache = LRUCache(capacity=self.config.cache_size)

        # Metrics
        self._metrics = GraphMetrics()

        # State
        self._initialized = False
        self._cleanup_task: Optional[asyncio.Task[None]] = None

        # Locks
        self._write_lock = asyncio.Lock()

        logger.info("SharedKnowledgeGraph created")

    async def initialize(self) -> None:
        """Initialize the knowledge graph."""
        if self._initialized:
            return

        # Initialize embedding model
        if SENTENCE_TRANSFORMERS_AVAILABLE:
            try:
                self._embedder = SentenceTransformer(self.config.embedding_model)
                logger.info(
                    "Loaded embedding model: %s",
                    self.config.embedding_model,
                )
            except Exception as e:
                logger.warning("Failed to load embedding model: %s", e)

        # Initialize ChromaDB with new API (1.0+)
        if CHROMADB_AVAILABLE:
            try:
                persist_dir = self.config.chroma_persist_directory
                if persist_dir:
                    Path(persist_dir).mkdir(parents=True, exist_ok=True)
                    # Use new PersistentClient API (ChromaDB 1.0+)
                    self._vector_db = chromadb.PersistentClient(
                        path=persist_dir,
                        settings=Settings(
                            anonymized_telemetry=False,
                            allow_reset=True,
                        )
                    )
                else:
                    # Use ephemeral client for in-memory only
                    self._vector_db = chromadb.EphemeralClient(
                        settings=Settings(
                            anonymized_telemetry=False,
                        )
                    )

                self._collection = self._vector_db.get_or_create_collection(
                    name=self.config.chroma_collection_name,
                    metadata={
                        "hnsw:space": "cosine",
                        "hnsw:construction_ef": 200,
                        "hnsw:M": 16,
                    },
                )

                # Load existing entries count
                self._metrics.total_entries = self._collection.count()

                logger.info(
                    "ChromaDB initialized with %d entries",
                    self._metrics.total_entries,
                )

            except Exception as e:
                logger.warning("Failed to initialize ChromaDB: %s", e)
                self._vector_db = None
                self._collection = None

        # Initialize NetworkX graph
        if NETWORKX_AVAILABLE:
            self._graph = nx.DiGraph()

            # Load persisted graph if exists
            graph_path = self.config.graph_persist_path
            if graph_path:
                Path(graph_path).mkdir(parents=True, exist_ok=True)
                graph_file = Path(graph_path) / "knowledge_graph.gpickle"
                if graph_file.exists():
                    try:
                        with open(graph_file, "rb") as f:
                            self._graph = pickle.load(f)
                        self._metrics.total_relationships = self._graph.number_of_edges()
                        logger.info(
                            "Loaded graph with %d nodes, %d edges",
                            self._graph.number_of_nodes(),
                            self._graph.number_of_edges(),
                        )
                    except Exception as e:
                        logger.warning("Failed to load graph: %s", e)
                        self._graph = nx.DiGraph()

        # Start cleanup task
        self._cleanup_task = asyncio.create_task(
            self._cleanup_expired_loop(),
            name="knowledge_cleanup",
        )

        self._initialized = True
        logger.info("SharedKnowledgeGraph initialized")

    async def close(self) -> None:
        """Close and persist the knowledge graph."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Persist graph
        if self._graph is not None and self.config.graph_persist_path:
            try:
                graph_file = Path(self.config.graph_persist_path) / "knowledge_graph.gpickle"
                with open(graph_file, "wb") as f:
                    pickle.dump(self._graph, f)
                logger.info("Persisted knowledge graph")
            except Exception as e:
                logger.exception("Failed to persist graph: %s", e)

        # Persist ChromaDB
        if self._vector_db is not None and hasattr(self._vector_db, "persist"):
            try:
                self._vector_db.persist()
                logger.info("Persisted vector database")
            except Exception as e:
                logger.exception("Failed to persist vector DB: %s", e)

        self._initialized = False
        logger.info("SharedKnowledgeGraph closed")

    async def add_knowledge(
        self,
        knowledge_type: KnowledgeType,
        agent_name: str,
        data: Dict[str, Any],
        tags: Optional[Set[str]] = None,
        ttl_seconds: Optional[float] = None,
        confidence: float = 1.0,
        source: str = "",
    ) -> KnowledgeEntry:
        """
        Add knowledge to the graph.

        Args:
            knowledge_type: Type of knowledge
            agent_name: Agent adding this knowledge
            data: Knowledge data
            tags: Searchable tags
            ttl_seconds: Time to live (uses default if not provided)
            confidence: Confidence score (0.0 to 1.0)
            source: Source of this knowledge

        Returns:
            The created knowledge entry
        """
        async with self._write_lock:
            # Calculate expiration
            expires_at = None
            if ttl_seconds is not None:
                expires_at = datetime.now() + timedelta(seconds=ttl_seconds)
            elif self.config.default_ttl_seconds:
                expires_at = datetime.now() + timedelta(
                    seconds=self.config.default_ttl_seconds
                )

            # Create entry
            entry = KnowledgeEntry(
                knowledge_type=knowledge_type,
                agent_name=agent_name,
                data=data,
                tags=tags or set(),
                expires_at=expires_at,
                confidence=confidence,
                source=source,
            )

            # Generate embedding
            if self._embedder is not None:
                text = self._create_searchable_text(entry)
                embedding = self._embedder.encode(text)
                entry.embedding = embedding

            # Add to ChromaDB
            if self._collection is not None and entry.embedding is not None:
                try:
                    self._collection.add(
                        ids=[entry.id],
                        embeddings=[entry.embedding.tolist()],
                        metadatas=[{
                            "knowledge_type": entry.knowledge_type.value,
                            "agent_name": entry.agent_name,
                            "confidence": entry.confidence,
                            "created_at": entry.created_at.isoformat(),
                            "tags": ",".join(entry.tags),
                        }],
                        documents=[json.dumps(entry.data, default=str)],
                    )
                except Exception as e:
                    logger.exception("Failed to add to ChromaDB: %s", e)

            # Add to graph
            if self._graph is not None:
                self._graph.add_node(
                    entry.id,
                    knowledge_type=entry.knowledge_type.value,
                    agent_name=entry.agent_name,
                    data=entry.data,
                    confidence=entry.confidence,
                    created_at=entry.created_at.isoformat(),
                )

            # Add to in-memory storage
            self._entries[entry.id] = entry

            # Update metrics
            self._metrics.total_entries += 1
            type_key = entry.knowledge_type.value
            self._metrics.entries_by_type[type_key] = (
                self._metrics.entries_by_type.get(type_key, 0) + 1
            )

            # Invalidate cache
            self._cache.clear()

            logger.debug(
                "Added knowledge %s (type=%s, agent=%s)",
                entry.id[:8],
                entry.knowledge_type.value,
                entry.agent_name,
            )

            return entry

    async def query(
        self,
        query: str,
        knowledge_types: Optional[List[KnowledgeType]] = None,
        agent_filter: Optional[str] = None,
        tags_filter: Optional[Set[str]] = None,
        min_confidence: float = 0.0,
        limit: int = 10,
        include_expired: bool = False,
    ) -> List[KnowledgeEntry]:
        """
        Query the knowledge graph.

        Args:
            query: Search query text
            knowledge_types: Filter to specific types
            agent_filter: Filter to specific agent
            tags_filter: Filter to entries with these tags
            min_confidence: Minimum confidence threshold
            limit: Maximum results to return
            include_expired: Whether to include expired entries

        Returns:
            List of matching knowledge entries
        """
        start_time = time.perf_counter()

        # Check cache
        cache_key = hashlib.md5(
            f"{query}:{knowledge_types}:{agent_filter}:{tags_filter}:{min_confidence}:{limit}".encode()
        ).hexdigest()

        cached = self._cache.get(cache_key, ttl=self.config.cache_ttl_seconds)
        if cached is not None:
            self._metrics.cache_hits += 1
            return cached

        self._metrics.cache_misses += 1

        results: List[KnowledgeEntry] = []

        # Use ChromaDB for semantic search
        if self._collection is not None and self._embedder is not None:
            try:
                # Generate query embedding
                query_embedding = self._embedder.encode(query).tolist()

                # Build where filter
                where_filter = {}
                if knowledge_types:
                    where_filter["knowledge_type"] = {
                        "$in": [kt.value for kt in knowledge_types]
                    }
                if agent_filter:
                    where_filter["agent_name"] = agent_filter
                if min_confidence > 0:
                    where_filter["confidence"] = {"$gte": min_confidence}

                # Query ChromaDB
                query_result = self._collection.query(
                    query_embeddings=[query_embedding],
                    n_results=min(limit * 2, self.config.max_query_limit),
                    where=where_filter if where_filter else None,
                    include=["metadatas", "documents", "distances"],
                )

                # Convert results
                if query_result["ids"] and query_result["ids"][0]:
                    for i, entry_id in enumerate(query_result["ids"][0]):
                        if entry_id in self._entries:
                            entry = self._entries[entry_id]
                        else:
                            # Reconstruct from ChromaDB data
                            metadata = query_result["metadatas"][0][i]
                            doc = query_result["documents"][0][i]
                            entry = KnowledgeEntry(
                                id=entry_id,
                                knowledge_type=KnowledgeType(metadata["knowledge_type"]),
                                agent_name=metadata["agent_name"],
                                data=json.loads(doc) if doc else {},
                                confidence=metadata.get("confidence", 1.0),
                                created_at=datetime.fromisoformat(metadata["created_at"]),
                                tags=set(metadata.get("tags", "").split(",")) if metadata.get("tags") else set(),
                            )
                            self._entries[entry_id] = entry

                        # Apply filters
                        if not include_expired and entry.is_expired():
                            continue
                        if tags_filter and not tags_filter.issubset(entry.tags):
                            continue

                        results.append(entry)

                        if len(results) >= limit:
                            break

            except Exception as e:
                logger.exception("ChromaDB query failed: %s", e)

        # Fallback to in-memory search
        if not results:
            query_lower = query.lower()
            for entry in self._entries.values():
                if not include_expired and entry.is_expired():
                    continue
                if knowledge_types and entry.knowledge_type not in knowledge_types:
                    continue
                if agent_filter and entry.agent_name != agent_filter:
                    continue
                if min_confidence > 0 and entry.confidence < min_confidence:
                    continue
                if tags_filter and not tags_filter.issubset(entry.tags):
                    continue

                # Simple text matching
                entry_text = self._create_searchable_text(entry).lower()
                if query_lower in entry_text:
                    results.append(entry)

                if len(results) >= limit:
                    break

        # Cache results
        self._cache.put(cache_key, results)

        # Update metrics
        query_time_ms = (time.perf_counter() - start_time) * 1000
        self._metrics.queries_executed += 1
        self._metrics.total_query_time_ms += query_time_ms
        self._metrics.average_query_time_ms = (
            self._metrics.total_query_time_ms / self._metrics.queries_executed
        )

        logger.debug(
            "Query completed in %.2fms with %d results",
            query_time_ms,
            len(results),
        )

        return results

    async def get_by_id(self, entry_id: str) -> Optional[KnowledgeEntry]:
        """Get a knowledge entry by ID."""
        return self._entries.get(entry_id)

    async def update_knowledge(
        self,
        entry_id: str,
        data: Dict[str, Any],
        agent_name: Optional[str] = None,
    ) -> Optional[KnowledgeEntry]:
        """
        Update an existing knowledge entry.

        Args:
            entry_id: ID of the entry to update
            data: New data to merge
            agent_name: Agent making the update

        Returns:
            Updated entry or None if not found
        """
        async with self._write_lock:
            entry = self._entries.get(entry_id)
            if not entry:
                return None

            entry.update(data, agent_name)

            # Update embedding
            if self._embedder is not None:
                text = self._create_searchable_text(entry)
                entry.embedding = self._embedder.encode(text)

            # Update in ChromaDB
            if self._collection is not None and entry.embedding is not None:
                try:
                    self._collection.update(
                        ids=[entry.id],
                        embeddings=[entry.embedding.tolist()],
                        metadatas=[{
                            "knowledge_type": entry.knowledge_type.value,
                            "agent_name": entry.agent_name,
                            "confidence": entry.confidence,
                            "created_at": entry.created_at.isoformat(),
                            "tags": ",".join(entry.tags),
                        }],
                        documents=[json.dumps(entry.data, default=str)],
                    )
                except Exception as e:
                    logger.exception("Failed to update ChromaDB: %s", e)

            # Update graph
            if self._graph is not None and self._graph.has_node(entry_id):
                self._graph.nodes[entry_id].update({
                    "data": entry.data,
                    "updated_at": entry.updated_at.isoformat(),
                })

            self._cache.clear()
            return entry

    async def delete_knowledge(self, entry_id: str) -> bool:
        """
        Delete a knowledge entry.

        Args:
            entry_id: ID of the entry to delete

        Returns:
            True if deleted, False if not found
        """
        async with self._write_lock:
            if entry_id not in self._entries:
                return False

            entry = self._entries.pop(entry_id)

            # Remove from ChromaDB
            if self._collection is not None:
                try:
                    self._collection.delete(ids=[entry_id])
                except Exception as e:
                    logger.warning("Failed to delete from ChromaDB: %s", e)

            # Remove from graph
            if self._graph is not None and self._graph.has_node(entry_id):
                self._graph.remove_node(entry_id)

            # Update metrics
            self._metrics.total_entries -= 1
            type_key = entry.knowledge_type.value
            if type_key in self._metrics.entries_by_type:
                self._metrics.entries_by_type[type_key] -= 1

            self._cache.clear()
            return True

    async def add_relationship(
        self,
        source_id: str,
        target_id: str,
        relationship_type: str,
        strength: float = 1.0,
        bidirectional: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[KnowledgeRelationship]:
        """
        Add a relationship between knowledge entries.

        Args:
            source_id: Source entry ID
            target_id: Target entry ID
            relationship_type: Type of relationship
            strength: Relationship strength (0.0 to 1.0)
            bidirectional: Whether to create reverse relationship
            metadata: Additional relationship data

        Returns:
            Created relationship or None if entries not found
        """
        if source_id not in self._entries or target_id not in self._entries:
            logger.warning(
                "Cannot create relationship: entry not found (%s -> %s)",
                source_id[:8],
                target_id[:8],
            )
            return None

        async with self._write_lock:
            relationship = KnowledgeRelationship(
                source_id=source_id,
                target_id=target_id,
                relationship_type=relationship_type,
                strength=strength,
                bidirectional=bidirectional,
                metadata=metadata or {},
            )

            self._relationships[relationship.id] = relationship

            # Add to graph
            if self._graph is not None:
                self._graph.add_edge(
                    source_id,
                    target_id,
                    relationship_type=relationship_type,
                    strength=strength,
                    metadata=metadata or {},
                )

                if bidirectional:
                    self._graph.add_edge(
                        target_id,
                        source_id,
                        relationship_type=relationship_type,
                        strength=strength,
                        metadata=metadata or {},
                    )

            # Update source entry relationships
            self._entries[source_id].relationships.append(target_id)
            if bidirectional:
                self._entries[target_id].relationships.append(source_id)

            self._metrics.total_relationships += 1

            return relationship

    async def get_related(
        self,
        entry_id: str,
        relationship_types: Optional[List[str]] = None,
        direction: str = "outgoing",
        max_depth: int = 1,
    ) -> List[KnowledgeEntry]:
        """
        Get related knowledge entries.

        Args:
            entry_id: Starting entry ID
            relationship_types: Filter to specific relationship types
            direction: "outgoing", "incoming", or "both"
            max_depth: Maximum traversal depth

        Returns:
            List of related entries
        """
        if self._graph is None or entry_id not in self._entries:
            return []

        if not self._graph.has_node(entry_id):
            return []

        related_ids: Set[str] = set()

        if direction in ("outgoing", "both"):
            if max_depth == 1:
                related_ids.update(self._graph.successors(entry_id))
            else:
                # BFS traversal
                visited = {entry_id}
                queue = [(entry_id, 0)]
                while queue:
                    node, depth = queue.pop(0)
                    if depth >= max_depth:
                        continue
                    for neighbor in self._graph.successors(node):
                        if neighbor not in visited:
                            visited.add(neighbor)
                            related_ids.add(neighbor)
                            queue.append((neighbor, depth + 1))

        if direction in ("incoming", "both"):
            if max_depth == 1:
                related_ids.update(self._graph.predecessors(entry_id))
            else:
                visited = {entry_id}
                queue = [(entry_id, 0)]
                while queue:
                    node, depth = queue.pop(0)
                    if depth >= max_depth:
                        continue
                    for neighbor in self._graph.predecessors(node):
                        if neighbor not in visited:
                            visited.add(neighbor)
                            related_ids.add(neighbor)
                            queue.append((neighbor, depth + 1))

        # Filter by relationship type if specified
        if relationship_types and self._graph is not None:
            filtered_ids = set()
            for rid in related_ids:
                edges = list(self._graph.edges(entry_id, data=True))
                for src, dst, data in edges:
                    if dst == rid and data.get("relationship_type") in relationship_types:
                        filtered_ids.add(rid)
            related_ids = filtered_ids

        return [
            self._entries[rid]
            for rid in related_ids
            if rid in self._entries and not self._entries[rid].is_expired()
        ]

    def get_metrics(self) -> GraphMetrics:
        """Get current graph metrics."""
        return self._metrics

    def _create_searchable_text(self, entry: KnowledgeEntry) -> str:
        """Create searchable text from a knowledge entry."""
        parts = [
            entry.knowledge_type.value,
            entry.agent_name,
            " ".join(entry.tags),
            entry.source,
        ]

        # Add data as text
        def extract_text(obj: Any) -> str:
            if isinstance(obj, str):
                return obj
            elif isinstance(obj, dict):
                return " ".join(extract_text(v) for v in obj.values())
            elif isinstance(obj, (list, tuple)):
                return " ".join(extract_text(v) for v in obj)
            else:
                return str(obj)

        parts.append(extract_text(entry.data))

        return " ".join(p for p in parts if p)

    async def _cleanup_expired_loop(self) -> None:
        """Periodically clean up expired entries."""
        import os
        max_runtime = float(os.getenv("TIMEOUT_KNOWLEDGE_CLEANUP_SESSION", "86400.0"))  # 24 hours
        cleanup_timeout = float(os.getenv("TIMEOUT_KNOWLEDGE_CLEANUP_ITERATION", "60.0"))
        start = time.time()

        while time.time() - start < max_runtime:
            try:
                await asyncio.sleep(self.config.cleanup_interval_seconds)
                await asyncio.wait_for(self._cleanup_expired(), timeout=cleanup_timeout)
            except asyncio.TimeoutError:
                logger.warning("Knowledge cleanup iteration timed out")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in cleanup loop: %s", e)

        logger.info("Knowledge cleanup loop reached max runtime, exiting")

    async def _cleanup_expired(self) -> None:
        """Clean up expired entries."""
        async with self._write_lock:
            expired_ids = [
                entry_id
                for entry_id, entry in self._entries.items()
                if entry.is_expired()
            ]

            for entry_id in expired_ids:
                entry = self._entries.pop(entry_id)

                if self._collection is not None:
                    try:
                        self._collection.delete(ids=[entry_id])
                    except Exception:
                        pass

                if self._graph is not None and self._graph.has_node(entry_id):
                    self._graph.remove_node(entry_id)

                type_key = entry.knowledge_type.value
                if type_key in self._metrics.entries_by_type:
                    self._metrics.entries_by_type[type_key] -= 1

                self._metrics.total_entries -= 1

            if expired_ids:
                self._cache.clear()
                logger.info("Cleaned up %d expired entries", len(expired_ids))

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"SharedKnowledgeGraph("
            f"entries={self._metrics.total_entries}, "
            f"relationships={self._metrics.total_relationships}, "
            f"queries={self._metrics.queries_executed}, "
            f"cache_hit_rate={self._metrics.cache_hit_rate():.2%}"
            f")"
        )
