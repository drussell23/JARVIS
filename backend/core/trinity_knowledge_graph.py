"""
Trinity Shared Knowledge Graph v2.7
===================================

Unified knowledge storage across Trinity repositories:
- JARVIS (Body) - Interaction knowledge, user patterns
- JARVIS Prime (Mind) - Model knowledge, inference patterns
- Reactor Core (Nerves) - Training knowledge, optimization patterns

Features:
- Graph-based knowledge representation
- Cross-repo knowledge synchronization
- Semantic similarity search
- Knowledge versioning and conflict resolution
- Event-driven knowledge updates
- Inference chain tracking

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │                   TRINITY KNOWLEDGE GRAPH                        │
    ├──────────────────────────────────────────────────────────────────┤
    │                                                                  │
    │   ┌─────────────────────────────────────────────────────────┐    │
    │   │                    Node Types                           │    │
    │   │                                                         │    │
    │   │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────────────┐    │    │
    │   │  │ Entity │ │Concept │ │Pattern │ │  Relationship  │    │    │
    │   │  └────────┘ └────────┘ └────────┘ └────────────────┘    │    │
    │   └─────────────────────────────────────────────────────────┘    │
    │                                                                  │
    │   ┌─────────────────────────────────────────────────────────┐    │
    │   │                   Storage Layers                        │    │
    │   │                                                         │    │
    │   │  Local Graph    ─────►  Sync Engine  ─────►  Cloud Graph│    │
    │   │  (SQLite)              (Event-Based)        (GCP)       │    │
    │   └─────────────────────────────────────────────────────────┘    │
    │                                                                  │
    │   ┌─────────────────────────────────────────────────────────┐    │
    │   │                  Knowledge Sources                      │    │
    │   │                                                         │    │
    │   │  JARVIS         PRIME              REACTOR              │    │
    │   │  • User prefs   • Model perf       • Training metrics   │    │
    │   │  • Commands     • Inference logs   • Optimization paths │    │
    │   │  • Context      • Embeddings       • Failure patterns   │    │
    │   └─────────────────────────────────────────────────────────┘    │
    └──────────────────────────────────────────────────────────────────┘

Usage:
    graph = await TrinityKnowledgeGraph.create()

    # Add knowledge
    await graph.add_node(KnowledgeNode(
        node_type=NodeType.PATTERN,
        content="User prefers concise responses",
        source=RepoType.JARVIS,
    ))

    # Query knowledge
    results = await graph.query("user preferences", limit=10)

    # Link knowledge
    await graph.add_edge(node1_id, node2_id, "related_to")

    # Cross-repo sync
    await graph.sync()
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import aiofiles
import aiosqlite

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


class KnowledgeGraphConfig:
    """Configuration for knowledge graph."""

    DB_PATH = _env_str(
        "TRINITY_KNOWLEDGE_DB",
        str(Path.home() / ".jarvis" / "trinity" / "knowledge" / "graph.db")
    )
    SYNC_DIR = _env_str(
        "TRINITY_KNOWLEDGE_SYNC",
        str(Path.home() / ".jarvis" / "trinity" / "knowledge" / "sync")
    )

    SYNC_INTERVAL_SECONDS = _env_float("KNOWLEDGE_SYNC_INTERVAL", 30.0)
    EMBEDDING_DIM = _env_int("KNOWLEDGE_EMBEDDING_DIM", 384)
    MAX_NODES_PER_QUERY = _env_int("KNOWLEDGE_MAX_RESULTS", 100)
    SIMILARITY_THRESHOLD = _env_float("KNOWLEDGE_SIMILARITY_THRESHOLD", 0.5)
    VERSION_RETENTION_DAYS = _env_int("KNOWLEDGE_VERSION_RETENTION", 30)


# =============================================================================
# Enums and Types
# =============================================================================

class RepoType(Enum):
    """Trinity repository types."""
    JARVIS = "jarvis"
    PRIME = "prime"
    REACTOR = "reactor"
    SHARED = "shared"


class NodeType(Enum):
    """Types of knowledge nodes."""
    ENTITY = "entity"          # Named entities (users, apps, files)
    CONCEPT = "concept"        # Abstract concepts
    PATTERN = "pattern"        # Behavioral/usage patterns
    FACT = "fact"              # Verified facts
    PROCEDURE = "procedure"    # How-to knowledge
    PREFERENCE = "preference"  # User/system preferences
    METRIC = "metric"          # Performance metrics
    ERROR = "error"            # Error patterns
    MODEL = "model"            # Model metadata
    TRAINING = "training"      # Training runs


class EdgeType(Enum):
    """Types of relationships."""
    RELATED_TO = "related_to"
    CAUSES = "causes"
    CAUSED_BY = "caused_by"
    DEPENDS_ON = "depends_on"
    PART_OF = "part_of"
    HAS_PART = "has_part"
    SIMILAR_TO = "similar_to"
    OPPOSITE_OF = "opposite_of"
    DERIVED_FROM = "derived_from"
    LEADS_TO = "leads_to"
    IMPROVES = "improves"
    DEGRADES = "degrades"


class SyncStatus(Enum):
    """Sync status."""
    SYNCED = "synced"
    PENDING = "pending"
    CONFLICT = "conflict"
    ERROR = "error"


@dataclass
class KnowledgeNode:
    """A node in the knowledge graph."""
    node_id: str = ""
    node_type: NodeType = NodeType.CONCEPT
    content: str = ""
    source: RepoType = RepoType.JARVIS
    embedding: Optional[List[float]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: Set[str] = field(default_factory=set)
    confidence: float = 1.0
    version: int = 1
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    sync_status: SyncStatus = SyncStatus.PENDING

    def __post_init__(self):
        if not self.node_id:
            self.node_id = self._generate_id()

    def _generate_id(self) -> str:
        content = f"{self.node_type.value}:{self.content}:{self.source.value}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type.value,
            "content": self.content,
            "source": self.source.value,
            "embedding": self.embedding,
            "metadata": self.metadata,
            "tags": list(self.tags),
            "confidence": self.confidence,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "sync_status": self.sync_status.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeNode":
        return cls(
            node_id=data["node_id"],
            node_type=NodeType(data["node_type"]),
            content=data["content"],
            source=RepoType(data["source"]),
            embedding=data.get("embedding"),
            metadata=data.get("metadata", {}),
            tags=set(data.get("tags", [])),
            confidence=data.get("confidence", 1.0),
            version=data.get("version", 1),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            sync_status=SyncStatus(data.get("sync_status", "pending")),
        )


@dataclass
class KnowledgeEdge:
    """An edge connecting two nodes."""
    edge_id: str = ""
    source_id: str = ""
    target_id: str = ""
    edge_type: EdgeType = EdgeType.RELATED_TO
    weight: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        if not self.edge_id:
            content = f"{self.source_id}:{self.edge_type.value}:{self.target_id}"
            self.edge_id = hashlib.sha256(content.encode()).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "edge_type": self.edge_type.value,
            "weight": self.weight,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeEdge":
        return cls(
            edge_id=data["edge_id"],
            source_id=data["source_id"],
            target_id=data["target_id"],
            edge_type=EdgeType(data["edge_type"]),
            weight=data.get("weight", 1.0),
            metadata=data.get("metadata", {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )


@dataclass
class QueryResult:
    """Result of a knowledge query."""
    node: KnowledgeNode
    score: float
    path: List[str] = field(default_factory=list)


@dataclass
class GraphStats:
    """Knowledge graph statistics."""
    total_nodes: int = 0
    total_edges: int = 0
    nodes_by_type: Dict[str, int] = field(default_factory=dict)
    nodes_by_source: Dict[str, int] = field(default_factory=dict)
    pending_sync: int = 0
    conflicts: int = 0
    last_sync: Optional[datetime] = None


# =============================================================================
# Knowledge Graph
# =============================================================================

class TrinityKnowledgeGraph:
    """
    Shared knowledge graph across Trinity repositories.

    Features:
    - Graph-based knowledge storage
    - Semantic similarity search
    - Cross-repo synchronization
    - Version tracking
    - Conflict resolution
    """

    def __init__(self, local_repo: RepoType = RepoType.JARVIS):
        self.local_repo = local_repo
        self._running = False

        # Database
        self._db: Optional[aiosqlite.Connection] = None

        # In-memory caches
        self._node_cache: Dict[str, KnowledgeNode] = {}
        self._edge_cache: Dict[str, KnowledgeEdge] = {}
        self._cache_lock = asyncio.Lock()

        # Embedding model
        self._embedder: Optional[Any] = None

        # Sync
        self._sync_dir = Path(KnowledgeGraphConfig.SYNC_DIR)
        self._sync_dir.mkdir(parents=True, exist_ok=True)
        self._sync_task: Optional[asyncio.Task] = None
        self._pending_sync: Set[str] = set()

        # Event bus integration
        self._event_bus: Optional[Any] = None

        logger.info(f"[KnowledgeGraph] Initialized for {local_repo.value}")

    @classmethod
    async def create(
        cls,
        local_repo: RepoType = RepoType.JARVIS,
    ) -> "TrinityKnowledgeGraph":
        """Create and initialize the knowledge graph."""
        graph = cls(local_repo)
        await graph.initialize()
        return graph

    async def initialize(self) -> None:
        """Initialize the knowledge graph."""
        # Ensure directories
        Path(KnowledgeGraphConfig.DB_PATH).parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        await self._init_database()

        # Initialize embedding model
        await self._init_embedder()

        # Connect to event bus
        await self._connect_event_bus()

        # Load initial cache
        await self._load_cache()

        self._running = True

        # Start sync task
        self._sync_task = asyncio.create_task(self._sync_loop())

        logger.info("[KnowledgeGraph] Initialization complete")

    async def _init_database(self) -> None:
        """Initialize SQLite database."""
        self._db = await aiosqlite.connect(KnowledgeGraphConfig.DB_PATH)

        await self._db.executescript("""
            -- Nodes table
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                node_type TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                embedding BLOB,
                metadata_json TEXT,
                tags_json TEXT,
                confidence REAL DEFAULT 1.0,
                version INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sync_status TEXT DEFAULT 'pending'
            );

            -- Edges table
            CREATE TABLE IF NOT EXISTS edges (
                edge_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (source_id) REFERENCES nodes(node_id),
                FOREIGN KEY (target_id) REFERENCES nodes(node_id)
            );

            -- Node versions for history
            CREATE TABLE IF NOT EXISTS node_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT,
                changed_at TEXT NOT NULL,
                FOREIGN KEY (node_id) REFERENCES nodes(node_id)
            );

            -- Indexes
            CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);
            CREATE INDEX IF NOT EXISTS idx_nodes_source ON nodes(source);
            CREATE INDEX IF NOT EXISTS idx_nodes_sync ON nodes(sync_status);
            CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
            CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
        """)

        await self._db.commit()

    async def _init_embedder(self) -> None:
        """Initialize embedding model for semantic search."""
        try:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("[KnowledgeGraph] Embedder initialized")
        except ImportError:
            logger.warning("[KnowledgeGraph] SentenceTransformers not available")

    async def _connect_event_bus(self) -> None:
        """Connect to Trinity event bus."""
        try:
            from backend.core.trinity_event_bus import get_trinity_event_bus, RepoType as EventRepoType

            self._event_bus = await get_trinity_event_bus(
                EventRepoType(self.local_repo.value)
            )

            # Subscribe to knowledge events
            await self._event_bus.subscribe(
                "knowledge.*",
                self._handle_knowledge_event
            )

            logger.info("[KnowledgeGraph] Connected to event bus")
        except ImportError:
            logger.warning("[KnowledgeGraph] Event bus not available")

    async def _load_cache(self) -> None:
        """Load frequently accessed nodes into cache."""
        if not self._db:
            return

        async with self._cache_lock:
            cursor = await self._db.execute("""
                SELECT * FROM nodes
                ORDER BY updated_at DESC
                LIMIT 1000
            """)

            async for row in cursor:
                node = self._row_to_node(row)
                self._node_cache[node.node_id] = node

        logger.debug(f"[KnowledgeGraph] Loaded {len(self._node_cache)} nodes to cache")

    def _row_to_node(self, row: tuple) -> KnowledgeNode:
        """Convert database row to KnowledgeNode."""
        (node_id, node_type, content, source, embedding_bytes,
         metadata_json, tags_json, confidence, version,
         created_at, updated_at, sync_status) = row

        embedding = None
        if embedding_bytes:
            import struct
            embedding = list(struct.unpack(f'{len(embedding_bytes)//4}f', embedding_bytes))

        return KnowledgeNode(
            node_id=node_id,
            node_type=NodeType(node_type),
            content=content,
            source=RepoType(source),
            embedding=embedding,
            metadata=json.loads(metadata_json) if metadata_json else {},
            tags=set(json.loads(tags_json)) if tags_json else set(),
            confidence=confidence,
            version=version,
            created_at=datetime.fromisoformat(created_at),
            updated_at=datetime.fromisoformat(updated_at),
            sync_status=SyncStatus(sync_status),
        )

    def _generate_embedding(self, text: str) -> Optional[List[float]]:
        """Generate embedding for text."""
        if not self._embedder:
            return None

        try:
            embedding = self._embedder.encode(text)
            return embedding.tolist()
        except Exception as e:
            logger.warning(f"[KnowledgeGraph] Embedding failed: {e}")
            return None

    async def add_node(self, node: KnowledgeNode) -> str:
        """
        Add a node to the knowledge graph.

        Args:
            node: The node to add

        Returns:
            Node ID
        """
        # Generate embedding if not provided
        if node.embedding is None and self._embedder:
            node.embedding = self._generate_embedding(node.content)

        # Update timestamps
        node.updated_at = datetime.now()

        # Check for existing node
        existing = await self.get_node(node.node_id)
        if existing:
            # Update version
            node.version = existing.version + 1
            node.created_at = existing.created_at

            # Save version history
            await self._save_version(existing)

        # Persist to database
        await self._persist_node(node)

        # Update cache
        async with self._cache_lock:
            self._node_cache[node.node_id] = node

        # Mark for sync
        self._pending_sync.add(node.node_id)

        # Publish event
        if self._event_bus:
            from backend.core.trinity_event_bus import TrinityEvent, EventPriority
            await self._event_bus.publish(TrinityEvent(
                topic="knowledge.node_added",
                payload={"node_id": node.node_id, "node_type": node.node_type.value},
                priority=EventPriority.LOW,
            ))

        logger.debug(f"[KnowledgeGraph] Added node {node.node_id}")
        return node.node_id

    async def _persist_node(self, node: KnowledgeNode) -> None:
        """Persist node to database."""
        if not self._db:
            return

        embedding_bytes = None
        if node.embedding:
            import struct
            embedding_bytes = struct.pack(f'{len(node.embedding)}f', *node.embedding)

        await self._db.execute("""
            INSERT OR REPLACE INTO nodes
            (node_id, node_type, content, source, embedding,
             metadata_json, tags_json, confidence, version,
             created_at, updated_at, sync_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node.node_id,
            node.node_type.value,
            node.content,
            node.source.value,
            embedding_bytes,
            json.dumps(node.metadata),
            json.dumps(list(node.tags)),
            node.confidence,
            node.version,
            node.created_at.isoformat(),
            node.updated_at.isoformat(),
            node.sync_status.value,
        ))

        await self._db.commit()

    async def _save_version(self, node: KnowledgeNode) -> None:
        """Save node version to history."""
        if not self._db:
            return

        await self._db.execute("""
            INSERT INTO node_versions
            (node_id, version, content, metadata_json, changed_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            node.node_id,
            node.version,
            node.content,
            json.dumps(node.metadata),
            datetime.now().isoformat(),
        ))

        await self._db.commit()

    async def get_node(self, node_id: str) -> Optional[KnowledgeNode]:
        """Get a node by ID."""
        # Check cache
        async with self._cache_lock:
            if node_id in self._node_cache:
                return self._node_cache[node_id]

        # Query database
        if self._db:
            cursor = await self._db.execute(
                "SELECT * FROM nodes WHERE node_id = ?",
                (node_id,)
            )
            row = await cursor.fetchone()

            if row:
                node = self._row_to_node(row)
                async with self._cache_lock:
                    self._node_cache[node_id] = node
                return node

        return None

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: EdgeType,
        weight: float = 1.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Add an edge between nodes.

        Args:
            source_id: Source node ID
            target_id: Target node ID
            edge_type: Type of relationship
            weight: Edge weight (default 1.0)
            metadata: Additional metadata

        Returns:
            Edge ID
        """
        edge = KnowledgeEdge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            weight=weight,
            metadata=metadata or {},
        )

        # Persist
        if self._db:
            await self._db.execute("""
                INSERT OR REPLACE INTO edges
                (edge_id, source_id, target_id, edge_type, weight, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                edge.edge_id,
                edge.source_id,
                edge.target_id,
                edge.edge_type.value,
                edge.weight,
                json.dumps(edge.metadata),
                edge.created_at.isoformat(),
            ))

            await self._db.commit()

        # Update cache
        async with self._cache_lock:
            self._edge_cache[edge.edge_id] = edge

        logger.debug(f"[KnowledgeGraph] Added edge {source_id} --{edge_type.value}--> {target_id}")
        return edge.edge_id

    async def query(
        self,
        query_text: str,
        node_types: Optional[List[NodeType]] = None,
        sources: Optional[List[RepoType]] = None,
        limit: int = 10,
        min_similarity: float = 0.0,
    ) -> List[QueryResult]:
        """
        Query knowledge graph with semantic search.

        Args:
            query_text: Search query
            node_types: Filter by node types
            sources: Filter by source repos
            limit: Max results
            min_similarity: Minimum similarity threshold

        Returns:
            List of matching nodes with scores
        """
        results = []

        # Generate query embedding
        query_embedding = self._generate_embedding(query_text)

        if not self._db:
            return results

        # Build SQL query
        conditions = []
        params = []

        if node_types:
            placeholders = ",".join("?" * len(node_types))
            conditions.append(f"node_type IN ({placeholders})")
            params.extend([nt.value for nt in node_types])

        if sources:
            placeholders = ",".join("?" * len(sources))
            conditions.append(f"source IN ({placeholders})")
            params.extend([s.value for s in sources])

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        cursor = await self._db.execute(f"""
            SELECT * FROM nodes
            {where_clause}
            ORDER BY updated_at DESC
            LIMIT ?
        """, params + [limit * 10])  # Fetch more for filtering

        async for row in cursor:
            node = self._row_to_node(row)

            # Calculate similarity
            score = 0.0
            if query_embedding and node.embedding:
                score = self._cosine_similarity(query_embedding, node.embedding)
            else:
                # Fallback to text matching
                if query_text.lower() in node.content.lower():
                    score = 0.5

            if score >= min_similarity:
                results.append(QueryResult(node=node, score=score))

        # Sort by score
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    def _cosine_similarity(
        self,
        vec1: List[float],
        vec2: List[float],
    ) -> float:
        """Calculate cosine similarity between vectors."""
        import math

        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot / (norm1 * norm2)

    async def get_neighbors(
        self,
        node_id: str,
        edge_types: Optional[List[EdgeType]] = None,
        direction: str = "both",  # "outgoing", "incoming", "both"
        limit: int = 50,
    ) -> List[Tuple[KnowledgeNode, KnowledgeEdge]]:
        """Get neighboring nodes."""
        neighbors = []

        if not self._db:
            return neighbors

        queries = []

        if direction in ("outgoing", "both"):
            queries.append((
                "SELECT e.*, n.* FROM edges e JOIN nodes n ON e.target_id = n.node_id WHERE e.source_id = ?",
                True
            ))

        if direction in ("incoming", "both"):
            queries.append((
                "SELECT e.*, n.* FROM edges e JOIN nodes n ON e.source_id = n.node_id WHERE e.target_id = ?",
                False
            ))

        for query, is_outgoing in queries:
            cursor = await self._db.execute(query, (node_id,))

            async for row in cursor:
                # Parse edge (first 7 columns)
                edge = KnowledgeEdge(
                    edge_id=row[0],
                    source_id=row[1],
                    target_id=row[2],
                    edge_type=EdgeType(row[3]),
                    weight=row[4],
                    metadata=json.loads(row[5]) if row[5] else {},
                    created_at=datetime.fromisoformat(row[6]),
                )

                # Check edge type filter
                if edge_types and edge.edge_type not in edge_types:
                    continue

                # Parse node (remaining columns)
                node = self._row_to_node(row[7:])
                neighbors.append((node, edge))

        return neighbors[:limit]

    async def find_path(
        self,
        start_id: str,
        end_id: str,
        max_depth: int = 5,
    ) -> Optional[List[str]]:
        """Find shortest path between nodes (BFS)."""
        if start_id == end_id:
            return [start_id]

        visited = {start_id}
        queue = [(start_id, [start_id])]

        while queue and len(visited) < 1000:
            current_id, path = queue.pop(0)

            if len(path) > max_depth:
                continue

            neighbors = await self.get_neighbors(current_id, direction="outgoing", limit=20)

            for node, edge in neighbors:
                if node.node_id == end_id:
                    return path + [node.node_id]

                if node.node_id not in visited:
                    visited.add(node.node_id)
                    queue.append((node.node_id, path + [node.node_id]))

        return None

    async def _handle_knowledge_event(self, event: Any) -> None:
        """Handle knowledge events from other repos."""
        try:
            if event.topic == "knowledge.sync_request":
                await self._send_sync_response(event)
            elif event.topic == "knowledge.sync_data":
                await self._receive_sync_data(event)
        except Exception as e:
            logger.exception(f"[KnowledgeGraph] Event handling error: {e}")

    async def _sync_loop(self) -> None:
        """Background sync loop."""
        while self._running:
            try:
                await asyncio.sleep(KnowledgeGraphConfig.SYNC_INTERVAL_SECONDS)

                if self._pending_sync:
                    await self.sync()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[KnowledgeGraph] Sync error: {e}")

    async def sync(self) -> int:
        """
        Synchronize knowledge with other repos.

        Returns:
            Number of nodes synced
        """
        synced = 0

        # Get pending nodes
        pending_ids = list(self._pending_sync)
        self._pending_sync.clear()

        # Write to sync directory
        for node_id in pending_ids:
            node = await self.get_node(node_id)
            if not node:
                continue

            # Write to repo-specific sync file
            filename = f"{self.local_repo.value}_knowledge.jsonl"
            filepath = self._sync_dir / filename

            try:
                async with aiofiles.open(filepath, "a") as f:
                    line = json.dumps(node.to_dict(), default=str) + "\n"
                    await f.write(line)

                # Update status
                node.sync_status = SyncStatus.SYNCED
                await self._persist_node(node)

                synced += 1
            except Exception as e:
                logger.warning(f"[KnowledgeGraph] Sync write failed: {e}")

        # Read from other repos' sync files
        for repo in RepoType:
            if repo == self.local_repo or repo == RepoType.SHARED:
                continue

            filename = f"{repo.value}_knowledge.jsonl"
            filepath = self._sync_dir / filename

            if not filepath.exists():
                continue

            try:
                nodes_to_merge = []

                async with aiofiles.open(filepath, "r") as f:
                    async for line in f:
                        try:
                            data = json.loads(line.strip())
                            node = KnowledgeNode.from_dict(data)
                            nodes_to_merge.append(node)
                        except (json.JSONDecodeError, KeyError):
                            continue

                # Merge nodes
                for node in nodes_to_merge:
                    existing = await self.get_node(node.node_id)

                    if not existing:
                        # New node
                        await self.add_node(node)
                        synced += 1
                    elif node.version > existing.version:
                        # Newer version
                        await self.add_node(node)
                        synced += 1
                    elif node.version == existing.version and node.updated_at > existing.updated_at:
                        # Same version, newer update (conflict resolution: last-write-wins)
                        await self.add_node(node)
                        synced += 1

                # Clear processed file
                if nodes_to_merge:
                    async with aiofiles.open(filepath, "w") as f:
                        await f.write("")

            except Exception as e:
                logger.warning(f"[KnowledgeGraph] Sync read failed for {repo.value}: {e}")

        if synced > 0:
            logger.info(f"[KnowledgeGraph] Synced {synced} nodes")

        return synced

    async def get_stats(self) -> GraphStats:
        """Get graph statistics."""
        stats = GraphStats()

        if not self._db:
            return stats

        # Total nodes
        cursor = await self._db.execute("SELECT COUNT(*) FROM nodes")
        row = await cursor.fetchone()
        stats.total_nodes = row[0] if row else 0

        # Total edges
        cursor = await self._db.execute("SELECT COUNT(*) FROM edges")
        row = await cursor.fetchone()
        stats.total_edges = row[0] if row else 0

        # Nodes by type
        cursor = await self._db.execute(
            "SELECT node_type, COUNT(*) FROM nodes GROUP BY node_type"
        )
        async for row in cursor:
            stats.nodes_by_type[row[0]] = row[1]

        # Nodes by source
        cursor = await self._db.execute(
            "SELECT source, COUNT(*) FROM nodes GROUP BY source"
        )
        async for row in cursor:
            stats.nodes_by_source[row[0]] = row[1]

        # Pending sync
        stats.pending_sync = len(self._pending_sync)

        return stats

    async def shutdown(self) -> None:
        """Shutdown the knowledge graph."""
        logger.info("[KnowledgeGraph] Shutting down...")
        self._running = False

        if self._sync_task:
            self._sync_task.cancel()

        # Final sync
        if self._pending_sync:
            await self.sync()

        # Close database
        if self._db:
            await self._db.close()

        logger.info("[KnowledgeGraph] Shutdown complete")


# =============================================================================
# Global Instance
# =============================================================================

_graph: Optional[TrinityKnowledgeGraph] = None


async def get_trinity_knowledge_graph(
    local_repo: RepoType = RepoType.JARVIS,
) -> TrinityKnowledgeGraph:
    """Get or create the global knowledge graph."""
    global _graph

    if _graph is None:
        _graph = await TrinityKnowledgeGraph.create(local_repo)

    return _graph


async def shutdown_trinity_knowledge_graph() -> None:
    """Shutdown the global knowledge graph."""
    global _graph

    if _graph:
        await _graph.shutdown()
        _graph = None
