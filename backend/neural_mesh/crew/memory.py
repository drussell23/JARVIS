"""
JARVIS Neural Mesh - Crew Memory System

Comprehensive memory system for multi-agent collaboration.

Features:
- Short-term memory: Current session context
- Long-term memory: Persistent knowledge
- Entity memory: Information about people, projects, etc.
- Episodic memory: Past experiences and events
- Procedural memory: How to perform tasks
- Shared memory: Knowledge accessible by all agents
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from .models import (
    MemoryType,
    MemoryEntry,
    EntityMemory,
    CrewAgent,
    CrewTask,
    TaskOutput,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Memory Backends
# =============================================================================

class MemoryBackend(ABC):
    """Abstract base for memory storage backends."""

    @abstractmethod
    async def store(self, key: str, value: Any, metadata: Dict[str, Any]) -> None:
        """Store a value."""
        pass

    @abstractmethod
    async def retrieve(self, key: str) -> Optional[Any]:
        """Retrieve a value by key."""
        pass

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for matching entries."""
        pass

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete a value."""
        pass

    @abstractmethod
    async def clear(self) -> None:
        """Clear all entries."""
        pass


class InMemoryBackend(MemoryBackend):
    """In-memory storage backend for fast access."""

    def __init__(self, max_entries: int = 10000) -> None:
        self._storage: Dict[str, Dict[str, Any]] = {}
        self._max_entries = max_entries

    async def store(self, key: str, value: Any, metadata: Dict[str, Any]) -> None:
        if len(self._storage) >= self._max_entries:
            # Evict oldest entry
            oldest_key = min(
                self._storage.keys(),
                key=lambda k: self._storage[k].get("created_at", datetime.min)
            )
            del self._storage[oldest_key]

        self._storage[key] = {
            "value": value,
            "metadata": metadata,
            "created_at": datetime.utcnow(),
            "access_count": 0,
        }

    async def retrieve(self, key: str) -> Optional[Any]:
        entry = self._storage.get(key)
        if entry:
            entry["access_count"] += 1
            entry["last_accessed"] = datetime.utcnow()
            return entry["value"]
        return None

    async def search(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        results = []
        query_lower = query.lower()

        for key, entry in self._storage.items():
            # Simple text matching
            value_str = str(entry["value"]).lower()
            if query_lower in value_str or query_lower in key.lower():
                # Apply filters
                if filters:
                    metadata = entry.get("metadata", {})
                    if not all(
                        metadata.get(k) == v for k, v in filters.items()
                    ):
                        continue

                results.append({
                    "key": key,
                    "value": entry["value"],
                    "metadata": entry.get("metadata", {}),
                    "score": 1.0 if query_lower in key.lower() else 0.5,
                })

        # Sort by score and limit
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    async def delete(self, key: str) -> bool:
        if key in self._storage:
            del self._storage[key]
            return True
        return False

    async def clear(self) -> None:
        self._storage.clear()


class ChromaDBBackend(MemoryBackend):
    """ChromaDB vector storage backend for semantic search."""

    def __init__(
        self,
        collection_name: str = "crew_memory",
        persist_directory: Optional[str] = None,
    ) -> None:
        self._collection_name = collection_name
        self._persist_directory = persist_directory
        self._client = None
        self._collection = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize ChromaDB connection."""
        if self._initialized:
            return

        try:
            import chromadb
            from chromadb.config import Settings

            settings = Settings(
                anonymized_telemetry=False,
                allow_reset=True,
            )

            if self._persist_directory:
                self._client = chromadb.PersistentClient(
                    path=self._persist_directory,
                    settings=settings,
                )
            else:
                # v253.6: chromadb.Client() deprecated in 1.x, use EphemeralClient
                self._client = chromadb.EphemeralClient(settings=settings)

            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )

            self._initialized = True
            logger.info(f"ChromaDB initialized: {self._collection_name}")

        except ImportError:
            logger.warning("ChromaDB not installed, falling back to in-memory")
            self._initialized = False

    async def store(self, key: str, value: Any, metadata: Dict[str, Any]) -> None:
        if not self._initialized:
            await self.initialize()
        if not self._collection:
            return

        # Convert value to string for embedding
        doc = str(value) if not isinstance(value, str) else value

        self._collection.upsert(
            ids=[key],
            documents=[doc],
            metadatas=[{**metadata, "raw_value": json.dumps(value)}],
        )

    async def retrieve(self, key: str) -> Optional[Any]:
        if not self._initialized:
            await self.initialize()
        if not self._collection:
            return None

        results = self._collection.get(ids=[key], include=["metadatas"])

        if results["ids"]:
            metadata = results["metadatas"][0] if results["metadatas"] else {}
            raw_value = metadata.pop("raw_value", None)
            if raw_value:
                try:
                    return json.loads(raw_value)
                except json.JSONDecodeError:
                    return raw_value
        return None

    async def search(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not self._initialized:
            await self.initialize()
        if not self._collection:
            return []

        where_filter = None
        if filters:
            # ChromaDB requires at least 2 conditions for $and
            filter_list = [{k: v} for k, v in filters.items()]
            if len(filter_list) == 1:
                where_filter = filter_list[0]
            elif len(filter_list) > 1:
                where_filter = {"$and": filter_list}

        results = self._collection.query(
            query_texts=[query],
            n_results=limit,
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )

        output = []
        if results["ids"] and results["ids"][0]:
            for i, id_ in enumerate(results["ids"][0]):
                metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                raw_value = metadata.pop("raw_value", None)
                value = None
                if raw_value:
                    try:
                        value = json.loads(raw_value)
                    except json.JSONDecodeError:
                        value = raw_value

                output.append({
                    "key": id_,
                    "value": value or results["documents"][0][i],
                    "metadata": metadata,
                    "score": 1.0 - (results["distances"][0][i] if results["distances"] else 0),
                })

        return output

    async def delete(self, key: str) -> bool:
        if not self._initialized:
            await self.initialize()
        if not self._collection:
            return False

        try:
            self._collection.delete(ids=[key])
            return True
        except Exception:
            return False

    async def clear(self) -> None:
        if not self._initialized:
            await self.initialize()
        if self._client:
            self._client.delete_collection(self._collection_name)
            self._collection = self._client.create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )


# =============================================================================
# Memory Types
# =============================================================================

class ShortTermMemory:
    """
    Short-term memory for current session context.

    Fast, limited capacity, automatically expires.
    """

    def __init__(
        self,
        max_entries: int = 100,
        ttl_seconds: float = 3600,  # 1 hour default
    ) -> None:
        self._entries: Dict[str, MemoryEntry] = {}
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._access_order: List[str] = []

    async def remember(
        self,
        content: Any,
        summary: str = "",
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Store something in short-term memory."""
        # Clean up expired entries
        self._cleanup_expired()

        # Evict if at capacity
        while len(self._entries) >= self._max_entries:
            oldest_id = self._access_order.pop(0)
            self._entries.pop(oldest_id, None)

        entry = MemoryEntry(
            memory_type=MemoryType.SHORT_TERM,
            content=content,
            summary=summary or str(content)[:100],
            agent_id=agent_id,
            task_id=task_id,
            expires_at=datetime.utcnow() + timedelta(seconds=self._ttl_seconds),
            metadata=metadata or {},
        )

        self._entries[entry.id] = entry
        self._access_order.append(entry.id)

        return entry.id

    async def recall(self, entry_id: str) -> Optional[MemoryEntry]:
        """Retrieve a specific memory entry."""
        entry = self._entries.get(entry_id)
        if entry and not entry.is_expired:
            entry.access_count += 1
            # Move to end of access order
            if entry_id in self._access_order:
                self._access_order.remove(entry_id)
                self._access_order.append(entry_id)
            return entry
        return None

    async def recall_recent(
        self,
        limit: int = 10,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> List[MemoryEntry]:
        """Get recent memory entries."""
        self._cleanup_expired()

        entries = list(self._entries.values())

        # Filter
        if agent_id:
            entries = [e for e in entries if e.agent_id == agent_id]
        if task_id:
            entries = [e for e in entries if e.task_id == task_id]

        # Sort by creation time (most recent first)
        entries.sort(key=lambda e: e.created_at, reverse=True)

        return entries[:limit]

    async def search(
        self,
        query: str,
        limit: int = 10,
    ) -> List[MemoryEntry]:
        """Search short-term memory."""
        self._cleanup_expired()

        query_lower = query.lower()
        results = []

        for entry in self._entries.values():
            score = 0.0
            content_str = str(entry.content).lower()
            summary_lower = entry.summary.lower()

            if query_lower in content_str:
                score += 0.6
            if query_lower in summary_lower:
                score += 0.4

            if score > 0:
                results.append((entry, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return [r[0] for r in results[:limit]]

    async def forget(self, entry_id: str) -> bool:
        """Remove a memory entry."""
        if entry_id in self._entries:
            del self._entries[entry_id]
            if entry_id in self._access_order:
                self._access_order.remove(entry_id)
            return True
        return False

    async def clear(self) -> None:
        """Clear all short-term memory."""
        self._entries.clear()
        self._access_order.clear()

    def _cleanup_expired(self) -> None:
        """Remove expired entries."""
        expired = [
            eid for eid, entry in self._entries.items()
            if entry.is_expired
        ]
        for eid in expired:
            del self._entries[eid]
            if eid in self._access_order:
                self._access_order.remove(eid)


class LongTermMemory:
    """
    Long-term memory for persistent knowledge.

    Uses vector embeddings for semantic search.
    """

    def __init__(
        self,
        backend: Optional[MemoryBackend] = None,
        use_chromadb: bool = True,
    ) -> None:
        if backend:
            self._backend = backend
        elif use_chromadb:
            self._backend = ChromaDBBackend(collection_name="crew_long_term")
        else:
            self._backend = InMemoryBackend()

    async def store(
        self,
        content: Any,
        summary: str,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Store in long-term memory."""
        entry = MemoryEntry(
            memory_type=MemoryType.LONG_TERM,
            content=content,
            summary=summary,
            agent_id=agent_id,
            task_id=task_id,
            metadata={
                **(metadata or {}),
                "tags": tags or [],
            },
        )

        await self._backend.store(
            entry.id,
            content,
            {
                "summary": summary,
                "agent_id": agent_id or "",
                "task_id": task_id or "",
                "tags": json.dumps(tags or []),
                "memory_type": MemoryType.LONG_TERM.value,
            },
        )

        return entry.id

    async def retrieve(self, entry_id: str) -> Optional[Any]:
        """Retrieve by ID."""
        return await self._backend.retrieve(entry_id)

    async def search(
        self,
        query: str,
        limit: int = 10,
        agent_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Semantic search in long-term memory."""
        filters = {}
        if agent_id:
            filters["agent_id"] = agent_id

        results = await self._backend.search(query, limit, filters)

        # Filter by tags if specified
        if tags:
            filtered = []
            for r in results:
                entry_tags = r.get("metadata", {}).get("tags", "[]")
                if isinstance(entry_tags, str):
                    entry_tags = json.loads(entry_tags)
                if any(t in entry_tags for t in tags):
                    filtered.append(r)
            return filtered

        return results

    async def delete(self, entry_id: str) -> bool:
        """Delete from long-term memory."""
        return await self._backend.delete(entry_id)


class EntityMemoryStore:
    """
    Entity memory for information about specific entities.

    Tracks people, projects, tools, and their relationships.
    """

    def __init__(self) -> None:
        self._entities: Dict[str, EntityMemory] = {}
        self._relationships: Dict[str, List[Dict[str, str]]] = defaultdict(list)

    async def remember_entity(
        self,
        entity_type: str,
        name: str,
        attributes: Optional[Dict[str, Any]] = None,
        entity_id: Optional[str] = None,
    ) -> str:
        """Remember an entity."""
        eid = entity_id or hashlib.sha256(
            f"{entity_type}:{name}".encode()
        ).hexdigest()[:16]

        if eid in self._entities:
            # Update existing
            entity = self._entities[eid]
            if attributes:
                entity.attributes.update(attributes)
            entity.updated_at = datetime.utcnow()
        else:
            # Create new
            entity = EntityMemory(
                entity_id=eid,
                entity_type=entity_type,
                name=name,
                attributes=attributes or {},
            )
            self._entities[eid] = entity

        return eid

    async def get_entity(self, entity_id: str) -> Optional[EntityMemory]:
        """Get an entity by ID."""
        return self._entities.get(entity_id)

    async def find_entity(
        self,
        entity_type: Optional[str] = None,
        name: Optional[str] = None,
    ) -> List[EntityMemory]:
        """Find entities by type or name."""
        results = []

        for entity in self._entities.values():
            if entity_type and entity.entity_type != entity_type:
                continue
            if name and name.lower() not in entity.name.lower():
                continue
            results.append(entity)

        return results

    async def add_relationship(
        self,
        from_entity_id: str,
        to_entity_id: str,
        relationship_type: str,
    ) -> None:
        """Add a relationship between entities."""
        self._relationships[from_entity_id].append({
            "to": to_entity_id,
            "type": relationship_type,
            "created_at": datetime.utcnow().isoformat(),
        })

        # Also track reverse relationship
        from_entity = self._entities.get(from_entity_id)
        to_entity = self._entities.get(to_entity_id)

        if from_entity and to_entity:
            from_entity.relationships.append({
                "entity_id": to_entity_id,
                "type": relationship_type,
            })

    async def get_relationships(
        self,
        entity_id: str,
        relationship_type: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Get relationships for an entity."""
        rels = self._relationships.get(entity_id, [])
        if relationship_type:
            rels = [r for r in rels if r.get("type") == relationship_type]
        return rels

    async def add_interaction(
        self,
        entity_id: str,
        interaction: MemoryEntry,
    ) -> None:
        """Record an interaction with an entity."""
        entity = self._entities.get(entity_id)
        if entity:
            entity.interactions.append(interaction)
            entity.updated_at = datetime.utcnow()


class EpisodicMemory:
    """
    Episodic memory for past experiences and events.

    Stores sequences of events that can be replayed.
    """

    def __init__(self, max_episodes: int = 1000) -> None:
        self._episodes: Dict[str, List[MemoryEntry]] = {}
        self._max_episodes = max_episodes
        self._episode_metadata: Dict[str, Dict[str, Any]] = {}

    async def start_episode(
        self,
        episode_type: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Start recording a new episode."""
        episode_id = f"ep_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{len(self._episodes)}"

        self._episodes[episode_id] = []
        self._episode_metadata[episode_id] = {
            "type": episode_type,
            "started_at": datetime.utcnow().isoformat(),
            "completed": False,
            **(metadata or {}),
        }

        return episode_id

    async def record_event(
        self,
        episode_id: str,
        event_type: str,
        content: Any,
        agent_id: Optional[str] = None,
    ) -> None:
        """Record an event in an episode."""
        if episode_id not in self._episodes:
            return

        entry = MemoryEntry(
            memory_type=MemoryType.EPISODIC,
            content=content,
            summary=f"{event_type}: {str(content)[:50]}",
            agent_id=agent_id,
            metadata={"event_type": event_type},
        )

        self._episodes[episode_id].append(entry)

    async def end_episode(
        self,
        episode_id: str,
        outcome: str = "completed",
    ) -> None:
        """End an episode."""
        if episode_id in self._episode_metadata:
            self._episode_metadata[episode_id]["completed"] = True
            self._episode_metadata[episode_id]["ended_at"] = datetime.utcnow().isoformat()
            self._episode_metadata[episode_id]["outcome"] = outcome

    async def recall_episode(
        self,
        episode_id: str,
    ) -> Optional[Tuple[List[MemoryEntry], Dict[str, Any]]]:
        """Recall a full episode."""
        if episode_id not in self._episodes:
            return None
        return (
            self._episodes[episode_id],
            self._episode_metadata.get(episode_id, {}),
        )

    async def find_similar_episodes(
        self,
        episode_type: Optional[str] = None,
        outcome: Optional[str] = None,
        limit: int = 10,
    ) -> List[str]:
        """Find episodes matching criteria."""
        matches = []

        for ep_id, metadata in self._episode_metadata.items():
            if episode_type and metadata.get("type") != episode_type:
                continue
            if outcome and metadata.get("outcome") != outcome:
                continue
            matches.append(ep_id)

        return matches[:limit]


class ProceduralMemory:
    """
    Procedural memory for learned procedures.

    Stores how to perform tasks based on past successes.
    """

    def __init__(self) -> None:
        self._procedures: Dict[str, Dict[str, Any]] = {}
        self._execution_history: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    async def learn_procedure(
        self,
        task_type: str,
        steps: List[str],
        agent_id: str,
        success: bool,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Learn a procedure from task execution."""
        proc_id = hashlib.sha256(
            f"{task_type}:{':'.join(steps)}".encode()
        ).hexdigest()[:16]

        if proc_id in self._procedures:
            # Update existing procedure
            proc = self._procedures[proc_id]
            proc["execution_count"] += 1
            if success:
                proc["success_count"] += 1
            proc["last_executed"] = datetime.utcnow().isoformat()
        else:
            # New procedure
            self._procedures[proc_id] = {
                "task_type": task_type,
                "steps": steps,
                "created_by": agent_id,
                "created_at": datetime.utcnow().isoformat(),
                "last_executed": datetime.utcnow().isoformat(),
                "execution_count": 1,
                "success_count": 1 if success else 0,
                "metadata": metadata or {},
            }

        # Record execution
        self._execution_history[proc_id].append({
            "agent_id": agent_id,
            "success": success,
            "timestamp": datetime.utcnow().isoformat(),
        })

        return proc_id

    async def recall_procedure(
        self,
        task_type: str,
        min_success_rate: float = 0.5,
    ) -> Optional[Dict[str, Any]]:
        """Recall best procedure for a task type."""
        candidates = [
            (proc_id, proc) for proc_id, proc in self._procedures.items()
            if proc["task_type"] == task_type
        ]

        if not candidates:
            return None

        # Filter by success rate
        valid = []
        for proc_id, proc in candidates:
            if proc["execution_count"] > 0:
                rate = proc["success_count"] / proc["execution_count"]
                if rate >= min_success_rate:
                    valid.append((proc_id, proc, rate))

        if not valid:
            return None

        # Return highest success rate
        valid.sort(key=lambda x: x[2], reverse=True)
        return valid[0][1]

    async def get_all_procedures(
        self,
        task_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get all known procedures."""
        if task_type:
            return [
                p for p in self._procedures.values()
                if p["task_type"] == task_type
            ]
        return list(self._procedures.values())


# =============================================================================
# Unified Crew Memory
# =============================================================================

class CrewMemory:
    """
    Unified memory system for crew collaboration.

    Combines all memory types with shared context.
    """

    def __init__(
        self,
        use_chromadb: bool = True,
        short_term_ttl: float = 3600,
        short_term_max: int = 100,
    ) -> None:
        self.short_term = ShortTermMemory(
            max_entries=short_term_max,
            ttl_seconds=short_term_ttl,
        )
        self.long_term = LongTermMemory(use_chromadb=use_chromadb)
        self.entities = EntityMemoryStore()
        self.episodes = EpisodicMemory()
        self.procedures = ProceduralMemory()

        # Shared context across agents
        self._shared_context: Dict[str, Any] = {}
        self._context_lock = asyncio.Lock()

    async def remember(
        self,
        content: Any,
        memory_type: MemoryType = MemoryType.SHORT_TERM,
        summary: str = "",
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Store a memory of the specified type."""
        if memory_type == MemoryType.SHORT_TERM:
            return await self.short_term.remember(
                content, summary, agent_id, task_id, metadata
            )
        elif memory_type == MemoryType.LONG_TERM:
            return await self.long_term.store(
                content, summary or str(content)[:100],
                agent_id, task_id, tags, metadata
            )
        else:
            # Default to short-term
            return await self.short_term.remember(
                content, summary, agent_id, task_id, metadata
            )

    async def recall(
        self,
        query: str,
        memory_types: Optional[List[MemoryType]] = None,
        limit: int = 10,
        agent_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search across memory types."""
        results = []

        types = memory_types or [MemoryType.SHORT_TERM, MemoryType.LONG_TERM]

        if MemoryType.SHORT_TERM in types:
            short_results = await self.short_term.search(query, limit)
            for entry in short_results:
                results.append({
                    "type": "short_term",
                    "content": entry.content,
                    "summary": entry.summary,
                    "agent_id": entry.agent_id,
                    "created_at": entry.created_at.isoformat(),
                })

        if MemoryType.LONG_TERM in types:
            long_results = await self.long_term.search(query, limit, agent_id)
            for r in long_results:
                results.append({
                    "type": "long_term",
                    "content": r.get("value"),
                    "summary": r.get("metadata", {}).get("summary", ""),
                    "score": r.get("score", 0),
                })

        return results[:limit]

    async def share_context(
        self,
        key: str,
        value: Any,
        agent_id: Optional[str] = None,
    ) -> None:
        """Share context with all agents."""
        async with self._context_lock:
            self._shared_context[key] = {
                "value": value,
                "set_by": agent_id,
                "timestamp": datetime.utcnow().isoformat(),
            }

    async def get_shared_context(
        self,
        key: Optional[str] = None,
    ) -> Union[Any, Dict[str, Any]]:
        """Get shared context."""
        if key:
            entry = self._shared_context.get(key)
            return entry["value"] if entry else None
        return {k: v["value"] for k, v in self._shared_context.items()}

    async def record_task_memory(
        self,
        task: CrewTask,
        output: TaskOutput,
        agent: CrewAgent,
    ) -> None:
        """Record memories from a task execution."""
        # Short-term: immediate context
        await self.short_term.remember(
            content={
                "task_description": task.description,
                "output": output.result,
                "success": output.success,
            },
            summary=f"Task '{task.description[:50]}' - {'success' if output.success else 'failed'}",
            agent_id=agent.id,
            task_id=task.id,
        )

        # Long-term: if successful, store the approach
        if output.success:
            await self.long_term.store(
                content={
                    "task_type": task.metadata.get("task_type", "generic"),
                    "description": task.description,
                    "approach": output.metadata.get("approach", ""),
                    "result_summary": str(output.result)[:500],
                },
                summary=f"Successful approach for: {task.description[:50]}",
                agent_id=agent.id,
                task_id=task.id,
                tags=task.required_capabilities,
            )

            # Procedural: learn the steps
            steps = output.metadata.get("steps", [])
            if steps:
                await self.procedures.learn_procedure(
                    task_type=task.metadata.get("task_type", "generic"),
                    steps=steps,
                    agent_id=agent.id,
                    success=True,
                )

    async def get_relevant_context(
        self,
        task: CrewTask,
        agent: CrewAgent,
        limit: int = 5,
    ) -> Dict[str, Any]:
        """Get relevant context for a task."""
        context = {}

        # Search for relevant memories
        memories = await self.recall(
            task.description,
            limit=limit,
            agent_id=agent.id,
        )
        if memories:
            context["relevant_memories"] = memories

        # Get shared context
        shared = await self.get_shared_context()
        if shared:
            context["shared_context"] = shared

        # Check for learned procedures
        task_type = task.metadata.get("task_type", "generic")
        procedure = await self.procedures.recall_procedure(task_type)
        if procedure:
            context["learned_procedure"] = procedure

        return context

    async def summarize(self) -> Dict[str, Any]:
        """Get a summary of all memory stores."""
        return {
            "short_term_entries": len(self.short_term._entries),
            "entity_count": len(self.entities._entities),
            "episode_count": len(self.episodes._episodes),
            "procedure_count": len(self.procedures._procedures),
            "shared_context_keys": list(self._shared_context.keys()),
        }
