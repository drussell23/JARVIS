"""
Memory Integration and Checkpointing for JARVIS LangGraph

This module provides sophisticated memory and state management including:
- Multi-tier memory (working, short-term, long-term, episodic)
- LangGraph checkpointing with persistence
- Conversation history management
- Semantic memory with embeddings
- Experience replay and learning

Features:
- Async-first design
- Multiple storage backends (memory, SQLite, Redis, ChromaDB)
- Automatic memory consolidation
- Context-aware retrieval
- Memory importance scoring
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pickle
import sqlite3
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, Awaitable, Callable, Deque, Dict, Generic, List, Literal,
    Optional, Protocol, Sequence, Set, Tuple, Type, TypeVar, Union
)
from uuid import uuid4

try:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.checkpoint.memory import MemorySaver
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    BaseCheckpointSaver = object
    MemorySaver = None

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ============================================================================
# Enums and Types
# ============================================================================

class MemoryType(str, Enum):
    """Types of memory."""
    WORKING = "working"         # Current task context
    SHORT_TERM = "short_term"   # Recent interactions
    LONG_TERM = "long_term"     # Persistent knowledge
    EPISODIC = "episodic"       # Experience episodes
    SEMANTIC = "semantic"       # Conceptual knowledge
    PROCEDURAL = "procedural"   # How-to knowledge


class MemoryPriority(int, Enum):
    """Memory importance levels."""
    CRITICAL = 0    # Never forget
    HIGH = 1        # Important
    NORMAL = 2      # Standard
    LOW = 3         # Can be forgotten
    TRANSIENT = 4   # Temporary


class StorageBackend(str, Enum):
    """Storage backend types."""
    MEMORY = "memory"
    SQLITE = "sqlite"
    REDIS = "redis"
    CHROMADB = "chromadb"


T = TypeVar('T')


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class MemoryEntry:
    """A single memory entry."""
    entry_id: str
    memory_type: MemoryType
    content: Any
    metadata: Dict[str, Any] = field(default_factory=dict)
    priority: MemoryPriority = MemoryPriority.NORMAL
    importance_score: float = 0.5
    access_count: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_accessed: datetime = field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    embedding: Optional[List[float]] = None
    tags: List[str] = field(default_factory=list)
    associations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "entry_id": self.entry_id,
            "memory_type": self.memory_type.value,
            "content": self.content,
            "metadata": self.metadata,
            "priority": self.priority.value,
            "importance_score": self.importance_score,
            "access_count": self.access_count,
            "created_at": self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "tags": self.tags,
            "associations": self.associations
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryEntry":
        """Create from dictionary."""
        return cls(
            entry_id=data["entry_id"],
            memory_type=MemoryType(data["memory_type"]),
            content=data["content"],
            metadata=data.get("metadata", {}),
            priority=MemoryPriority(data.get("priority", MemoryPriority.NORMAL.value)),
            importance_score=data.get("importance_score", 0.5),
            access_count=data.get("access_count", 0),
            created_at=datetime.fromisoformat(data["created_at"]),
            last_accessed=datetime.fromisoformat(data.get("last_accessed", data["created_at"])),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
            embedding=data.get("embedding"),
            tags=data.get("tags", []),
            associations=data.get("associations", [])
        )


@dataclass
class ConversationTurn:
    """A single conversation turn."""
    turn_id: str
    role: str  # 'user', 'assistant', 'system'
    content: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results
        }


@dataclass
class Episode:
    """An experience episode for episodic memory."""
    episode_id: str
    session_id: str
    goal: str
    actions: List[Dict[str, Any]]
    outcome: str
    success: bool
    reward_signal: float
    context: Dict[str, Any]
    lessons_learned: List[str]
    created_at: datetime = field(default_factory=datetime.utcnow)
    duration_seconds: float = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CheckpointData:
    """Checkpoint data for state persistence."""
    checkpoint_id: str
    thread_id: str
    state: Dict[str, Any]
    metadata: Dict[str, Any]
    parent_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)


# ============================================================================
# Storage Backends
# ============================================================================

class MemoryStore(ABC):
    """Abstract base class for memory storage."""

    @abstractmethod
    async def store(self, entry: MemoryEntry) -> str:
        """Store a memory entry."""
        pass

    @abstractmethod
    async def retrieve(self, entry_id: str) -> Optional[MemoryEntry]:
        """Retrieve a memory entry by ID."""
        pass

    @abstractmethod
    async def search(
        self,
        query: str,
        memory_type: Optional[MemoryType] = None,
        limit: int = 10,
        **kwargs
    ) -> List[MemoryEntry]:
        """Search for memory entries."""
        pass

    @abstractmethod
    async def delete(self, entry_id: str) -> bool:
        """Delete a memory entry."""
        pass

    @abstractmethod
    async def list_by_type(
        self,
        memory_type: MemoryType,
        limit: int = 100
    ) -> List[MemoryEntry]:
        """List entries by type."""
        pass


class InMemoryStore(MemoryStore):
    """In-memory storage backend."""

    def __init__(self, max_entries: int = 10000):
        self.max_entries = max_entries
        self._entries: Dict[str, MemoryEntry] = {}
        self._by_type: Dict[MemoryType, Set[str]] = {t: set() for t in MemoryType}

    async def store(self, entry: MemoryEntry) -> str:
        """Store entry in memory."""
        # Evict if at capacity
        if len(self._entries) >= self.max_entries:
            await self._evict_lru()

        self._entries[entry.entry_id] = entry
        self._by_type[entry.memory_type].add(entry.entry_id)
        return entry.entry_id

    async def retrieve(self, entry_id: str) -> Optional[MemoryEntry]:
        """Retrieve entry from memory."""
        entry = self._entries.get(entry_id)
        if entry:
            entry.access_count += 1
            entry.last_accessed = datetime.utcnow()
        return entry

    async def search(
        self,
        query: str,
        memory_type: Optional[MemoryType] = None,
        limit: int = 10,
        **kwargs
    ) -> List[MemoryEntry]:
        """Search entries."""
        query_lower = query.lower()
        results = []

        for entry in self._entries.values():
            if memory_type and entry.memory_type != memory_type:
                continue

            # Simple text matching
            content_str = str(entry.content).lower()
            if query_lower in content_str:
                results.append(entry)
            elif any(query_lower in tag.lower() for tag in entry.tags):
                results.append(entry)

        # Sort by relevance (simple: importance score)
        results.sort(key=lambda e: e.importance_score, reverse=True)
        return results[:limit]

    async def delete(self, entry_id: str) -> bool:
        """Delete entry."""
        if entry_id in self._entries:
            entry = self._entries[entry_id]
            self._by_type[entry.memory_type].discard(entry_id)
            del self._entries[entry_id]
            return True
        return False

    async def list_by_type(
        self,
        memory_type: MemoryType,
        limit: int = 100
    ) -> List[MemoryEntry]:
        """List entries by type."""
        entry_ids = list(self._by_type[memory_type])[:limit]
        return [self._entries[eid] for eid in entry_ids if eid in self._entries]

    async def _evict_lru(self) -> None:
        """Evict least recently used entry."""
        if not self._entries:
            return

        # Find LRU entry (excluding critical)
        lru_entry = None
        for entry in self._entries.values():
            if entry.priority == MemoryPriority.CRITICAL:
                continue
            if lru_entry is None or entry.last_accessed < lru_entry.last_accessed:
                lru_entry = entry

        if lru_entry:
            await self.delete(lru_entry.entry_id)


class SQLiteStore(MemoryStore):
    """SQLite storage backend."""

    def __init__(self, db_path: str = ".jarvis_cache/memory.db"):
        self.db_path = db_path
        self._ensure_db()

    def _ensure_db(self) -> None:
        """Ensure database exists with schema."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                entry_id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT,
                priority INTEGER DEFAULT 2,
                importance_score REAL DEFAULT 0.5,
                access_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                last_accessed TEXT NOT NULL,
                expires_at TEXT,
                tags TEXT,
                associations TEXT
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_type ON memories(memory_type)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_importance ON memories(importance_score DESC)
        """)

        conn.commit()
        conn.close()

    async def store(self, entry: MemoryEntry) -> str:
        """Store entry in SQLite."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO memories
            (entry_id, memory_type, content, metadata, priority, importance_score,
             access_count, created_at, last_accessed, expires_at, tags, associations)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.entry_id,
            entry.memory_type.value,
            json.dumps(entry.content),
            json.dumps(entry.metadata),
            entry.priority.value,
            entry.importance_score,
            entry.access_count,
            entry.created_at.isoformat(),
            entry.last_accessed.isoformat(),
            entry.expires_at.isoformat() if entry.expires_at else None,
            json.dumps(entry.tags),
            json.dumps(entry.associations)
        ))

        conn.commit()
        conn.close()
        return entry.entry_id

    async def retrieve(self, entry_id: str) -> Optional[MemoryEntry]:
        """Retrieve entry from SQLite."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM memories WHERE entry_id = ?", (entry_id,))
        row = cursor.fetchone()

        if row:
            # Update access count
            cursor.execute("""
                UPDATE memories SET access_count = access_count + 1,
                last_accessed = ? WHERE entry_id = ?
            """, (datetime.utcnow().isoformat(), entry_id))
            conn.commit()

        conn.close()

        if row:
            return self._row_to_entry(row)
        return None

    async def search(
        self,
        query: str,
        memory_type: Optional[MemoryType] = None,
        limit: int = 10,
        **kwargs
    ) -> List[MemoryEntry]:
        """Search entries in SQLite."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        sql = """
            SELECT * FROM memories
            WHERE (content LIKE ? OR tags LIKE ?)
        """
        params = [f"%{query}%", f"%{query}%"]

        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type.value)

        sql += " ORDER BY importance_score DESC LIMIT ?"
        params.append(limit)

        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_entry(row) for row in rows]

    async def delete(self, entry_id: str) -> bool:
        """Delete entry from SQLite."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("DELETE FROM memories WHERE entry_id = ?", (entry_id,))
        deleted = cursor.rowcount > 0

        conn.commit()
        conn.close()
        return deleted

    async def list_by_type(
        self,
        memory_type: MemoryType,
        limit: int = 100
    ) -> List[MemoryEntry]:
        """List entries by type."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM memories WHERE memory_type = ?
            ORDER BY importance_score DESC LIMIT ?
        """, (memory_type.value, limit))

        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_entry(row) for row in rows]

    def _row_to_entry(self, row: Tuple) -> MemoryEntry:
        """Convert database row to MemoryEntry."""
        return MemoryEntry(
            entry_id=row[0],
            memory_type=MemoryType(row[1]),
            content=json.loads(row[2]),
            metadata=json.loads(row[3]) if row[3] else {},
            priority=MemoryPriority(row[4]),
            importance_score=row[5],
            access_count=row[6],
            created_at=datetime.fromisoformat(row[7]),
            last_accessed=datetime.fromisoformat(row[8]),
            expires_at=datetime.fromisoformat(row[9]) if row[9] else None,
            tags=json.loads(row[10]) if row[10] else [],
            associations=json.loads(row[11]) if row[11] else []
        )


# ============================================================================
# Memory Manager
# ============================================================================

class MemoryManager:
    """
    Multi-tier memory management system.

    Manages different types of memory with automatic consolidation,
    importance scoring, and retrieval optimization.
    """

    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        working_memory_size: int = 100,
        short_term_duration_minutes: int = 60,
        consolidation_interval_seconds: float = 300.0,
        enable_auto_consolidation: bool = True
    ):
        self.store = store or InMemoryStore()
        self.working_memory_size = working_memory_size
        self.short_term_duration = timedelta(minutes=short_term_duration_minutes)
        self.consolidation_interval = consolidation_interval_seconds
        self.enable_auto_consolidation = enable_auto_consolidation

        # Working memory (fast access, limited size)
        self._working_memory: Deque[MemoryEntry] = deque(maxlen=working_memory_size)

        # Consolidation task
        self._consolidation_task: Optional[asyncio.Task] = None

        self.logger = logging.getLogger(__name__)

    async def start(self) -> None:
        """Start memory manager."""
        if self.enable_auto_consolidation:
            self._consolidation_task = asyncio.create_task(self._consolidation_loop())

    async def stop(self) -> None:
        """Stop memory manager."""
        if self._consolidation_task:
            self._consolidation_task.cancel()
            try:
                await self._consolidation_task
            except asyncio.CancelledError:
                pass

    async def remember(
        self,
        content: Any,
        memory_type: MemoryType = MemoryType.SHORT_TERM,
        priority: MemoryPriority = MemoryPriority.NORMAL,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        ttl_seconds: Optional[float] = None
    ) -> str:
        """
        Store a new memory.

        Args:
            content: Content to remember
            memory_type: Type of memory
            priority: Importance priority
            tags: Tags for retrieval
            metadata: Additional metadata
            ttl_seconds: Time-to-live in seconds

        Returns:
            Memory entry ID
        """
        entry = MemoryEntry(
            entry_id=str(uuid4()),
            memory_type=memory_type,
            content=content,
            metadata=metadata or {},
            priority=priority,
            importance_score=self._calculate_importance(content, priority, tags),
            tags=tags or [],
            expires_at=datetime.utcnow() + timedelta(seconds=ttl_seconds) if ttl_seconds else None
        )

        # Add to working memory if short-term or working
        if memory_type in (MemoryType.WORKING, MemoryType.SHORT_TERM):
            self._working_memory.append(entry)

        # Persist to store
        await self.store.store(entry)

        return entry.entry_id

    async def recall(
        self,
        query: str,
        memory_type: Optional[MemoryType] = None,
        limit: int = 10,
        include_working: bool = True
    ) -> List[MemoryEntry]:
        """
        Recall memories matching a query.

        Args:
            query: Search query
            memory_type: Filter by type
            limit: Maximum results
            include_working: Include working memory

        Returns:
            Matching memory entries
        """
        results = []

        # Search working memory first
        if include_working:
            query_lower = query.lower()
            for entry in self._working_memory:
                if memory_type and entry.memory_type != memory_type:
                    continue
                content_str = str(entry.content).lower()
                if query_lower in content_str:
                    results.append(entry)

        # Search persistent store
        store_results = await self.store.search(query, memory_type, limit)
        results.extend(store_results)

        # Deduplicate and sort by importance
        seen = set()
        unique_results = []
        for entry in results:
            if entry.entry_id not in seen:
                seen.add(entry.entry_id)
                unique_results.append(entry)

        unique_results.sort(key=lambda e: e.importance_score, reverse=True)
        return unique_results[:limit]

    async def forget(self, entry_id: str) -> bool:
        """
        Forget a memory.

        Args:
            entry_id: ID of memory to forget

        Returns:
            True if forgotten
        """
        # Remove from working memory
        self._working_memory = deque(
            (e for e in self._working_memory if e.entry_id != entry_id),
            maxlen=self.working_memory_size
        )

        # Remove from store
        return await self.store.delete(entry_id)

    async def get_working_memory(self) -> List[MemoryEntry]:
        """Get current working memory."""
        return list(self._working_memory)

    async def get_context_summary(self, max_entries: int = 5) -> Dict[str, Any]:
        """
        Get a summary of relevant context.

        Args:
            max_entries: Maximum entries to include

        Returns:
            Context summary
        """
        working = list(self._working_memory)[-max_entries:]

        return {
            "working_memory_size": len(self._working_memory),
            "recent_memories": [
                {
                    "type": e.memory_type.value,
                    "content_preview": str(e.content)[:100],
                    "importance": e.importance_score,
                    "age_seconds": (datetime.utcnow() - e.created_at).total_seconds()
                }
                for e in working
            ],
            "timestamp": datetime.utcnow().isoformat()
        }

    async def consolidate(self) -> int:
        """
        Consolidate short-term memories to long-term.

        Returns:
            Number of memories consolidated
        """
        consolidated = 0
        now = datetime.utcnow()

        # Get short-term memories
        short_term = await self.store.list_by_type(MemoryType.SHORT_TERM)

        for entry in short_term:
            age = now - entry.created_at

            # Consolidate if old enough and accessed frequently
            if age > self.short_term_duration:
                if entry.access_count >= 3 or entry.importance_score >= 0.7:
                    # Promote to long-term
                    entry.memory_type = MemoryType.LONG_TERM
                    await self.store.store(entry)
                    consolidated += 1
                elif entry.expires_at and now > entry.expires_at:
                    # Expired - delete
                    await self.store.delete(entry.entry_id)

        return consolidated

    async def _consolidation_loop(self) -> None:
        """Background consolidation loop."""
        consolidation_timeout = float(os.getenv("TIMEOUT_MEMORY_CONSOLIDATION", "60.0"))
        while True:
            try:
                await asyncio.sleep(self.consolidation_interval)
                count = await asyncio.wait_for(
                    self.consolidate(),
                    timeout=consolidation_timeout
                )
                if count > 0:
                    self.logger.info(f"Consolidated {count} memories")
            except asyncio.TimeoutError:
                self.logger.warning("Memory consolidation timed out")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Consolidation error: {e}")

    def _calculate_importance(
        self,
        content: Any,
        priority: MemoryPriority,
        tags: Optional[List[str]]
    ) -> float:
        """Calculate importance score for a memory."""
        # Base score from priority
        priority_scores = {
            MemoryPriority.CRITICAL: 1.0,
            MemoryPriority.HIGH: 0.8,
            MemoryPriority.NORMAL: 0.5,
            MemoryPriority.LOW: 0.3,
            MemoryPriority.TRANSIENT: 0.1
        }
        score = priority_scores.get(priority, 0.5)

        # Boost for rich content
        content_str = str(content)
        if len(content_str) > 500:
            score = min(1.0, score + 0.1)

        # Boost for tags
        if tags and len(tags) > 2:
            score = min(1.0, score + 0.1)

        return score


# ============================================================================
# Conversation Memory
# ============================================================================

class ConversationMemory:
    """
    Manages conversation history with windowing and summarization.

    Features:
    - Sliding window for recent turns
    - Automatic summarization for long conversations
    - Role-aware retrieval
    - Tool call tracking
    """

    def __init__(
        self,
        max_turns: int = 50,
        summary_threshold: int = 30,
        enable_summarization: bool = True
    ):
        self.max_turns = max_turns
        self.summary_threshold = summary_threshold
        self.enable_summarization = enable_summarization

        self._turns: Deque[ConversationTurn] = deque(maxlen=max_turns)
        self._summaries: List[str] = []
        self._tool_history: List[Dict[str, Any]] = []

        self.logger = logging.getLogger(__name__)

    def add_turn(
        self,
        role: str,
        content: str,
        tool_calls: Optional[List[Dict]] = None,
        tool_results: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = None
    ) -> str:
        """
        Add a conversation turn.

        Args:
            role: 'user', 'assistant', or 'system'
            content: Message content
            tool_calls: Tool calls made
            tool_results: Results from tools
            metadata: Additional metadata

        Returns:
            Turn ID
        """
        turn = ConversationTurn(
            turn_id=str(uuid4()),
            role=role,
            content=content,
            tool_calls=tool_calls or [],
            tool_results=tool_results or [],
            metadata=metadata or {}
        )

        self._turns.append(turn)

        # Track tool usage
        if tool_calls:
            for tc in tool_calls:
                self._tool_history.append({
                    "turn_id": turn.turn_id,
                    "tool": tc,
                    "timestamp": turn.timestamp.isoformat()
                })

        # Check if summarization needed
        if (self.enable_summarization and
            len(self._turns) >= self.summary_threshold and
            len(self._turns) % self.summary_threshold == 0):
            asyncio.create_task(self._summarize_old_turns())

        return turn.turn_id

    def get_recent_turns(self, count: int = 10) -> List[ConversationTurn]:
        """Get recent conversation turns."""
        return list(self._turns)[-count:]

    def get_context_messages(
        self,
        max_tokens: int = 4000,
        include_system: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Get conversation messages for context.

        Args:
            max_tokens: Approximate token limit
            include_system: Include system messages

        Returns:
            List of message dicts
        """
        messages = []
        estimated_tokens = 0
        avg_chars_per_token = 4  # Rough estimate

        # Add summaries first
        for summary in self._summaries:
            msg = {"role": "system", "content": f"[Previous conversation summary]: {summary}"}
            estimated_tokens += len(summary) // avg_chars_per_token
            messages.append(msg)

        # Add recent turns
        for turn in reversed(list(self._turns)):
            if not include_system and turn.role == "system":
                continue

            content_tokens = len(turn.content) // avg_chars_per_token
            if estimated_tokens + content_tokens > max_tokens:
                break

            msg = {"role": turn.role, "content": turn.content}
            if turn.tool_calls:
                msg["tool_calls"] = turn.tool_calls
            if turn.tool_results:
                msg["tool_results"] = turn.tool_results

            messages.insert(len(self._summaries), msg)
            estimated_tokens += content_tokens

        return messages

    def get_tool_usage_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent tool usage history."""
        return self._tool_history[-limit:]

    def clear(self) -> None:
        """Clear conversation history."""
        self._turns.clear()
        self._summaries.clear()
        self._tool_history.clear()

    async def _summarize_old_turns(self) -> None:
        """Summarize older turns to save context space."""
        if len(self._turns) < self.summary_threshold:
            return

        # Get oldest turns to summarize
        turns_to_summarize = list(self._turns)[:self.summary_threshold // 2]

        if not turns_to_summarize:
            return

        # Simple extractive summary (could be enhanced with LLM)
        summary_parts = []
        for turn in turns_to_summarize:
            if turn.role == "user":
                summary_parts.append(f"User asked about: {turn.content[:50]}...")
            elif turn.role == "assistant" and turn.tool_calls:
                tools = [tc.get("name", "unknown") for tc in turn.tool_calls]
                summary_parts.append(f"Assistant used tools: {', '.join(tools)}")

        if summary_parts:
            self._summaries.append(" | ".join(summary_parts[:5]))

        # Remove summarized turns
        for _ in range(len(turns_to_summarize)):
            if self._turns:
                self._turns.popleft()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "turns": [t.to_dict() for t in self._turns],
            "summaries": self._summaries,
            "tool_history_count": len(self._tool_history)
        }


# ============================================================================
# Episodic Memory
# ============================================================================

class EpisodicMemory:
    """
    Manages experience episodes for learning.

    Stores complete episodes of goal → actions → outcome
    for experience replay and pattern learning.
    """

    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        max_episodes: int = 1000
    ):
        self.store = store or InMemoryStore()
        self.max_episodes = max_episodes
        self._recent_episodes: Deque[Episode] = deque(maxlen=100)

        self.logger = logging.getLogger(__name__)

    async def record_episode(
        self,
        session_id: str,
        goal: str,
        actions: List[Dict[str, Any]],
        outcome: str,
        success: bool,
        context: Optional[Dict[str, Any]] = None,
        lessons: Optional[List[str]] = None
    ) -> str:
        """
        Record a new episode.

        Args:
            session_id: Session this episode belongs to
            goal: Goal that was attempted
            actions: Actions taken
            outcome: Final outcome
            success: Whether goal was achieved
            context: Context at the time
            lessons: Lessons learned

        Returns:
            Episode ID
        """
        # Calculate reward signal
        reward = self._calculate_reward(success, actions, outcome)

        # Calculate duration
        if actions:
            first_time = actions[0].get("timestamp")
            last_time = actions[-1].get("timestamp")
            if first_time and last_time:
                try:
                    t1 = datetime.fromisoformat(first_time)
                    t2 = datetime.fromisoformat(last_time)
                    duration = (t2 - t1).total_seconds()
                except (ValueError, TypeError):
                    duration = 0
            else:
                duration = 0
        else:
            duration = 0

        episode = Episode(
            episode_id=str(uuid4()),
            session_id=session_id,
            goal=goal,
            actions=actions,
            outcome=outcome,
            success=success,
            reward_signal=reward,
            context=context or {},
            lessons_learned=lessons or [],
            duration_seconds=duration
        )

        self._recent_episodes.append(episode)

        # Store as memory entry
        entry = MemoryEntry(
            entry_id=episode.episode_id,
            memory_type=MemoryType.EPISODIC,
            content=episode.to_dict(),
            priority=MemoryPriority.HIGH if success else MemoryPriority.NORMAL,
            importance_score=reward,
            tags=["episode", "success" if success else "failure", goal.split()[0]]
        )

        await self.store.store(entry)

        return episode.episode_id

    async def find_similar_episodes(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
        limit: int = 5,
        success_only: bool = False
    ) -> List[Episode]:
        """
        Find similar past episodes.

        Args:
            goal: Current goal
            context: Current context
            limit: Maximum episodes to return
            success_only: Only return successful episodes

        Returns:
            Similar episodes
        """
        # Search in store
        entries = await self.store.search(
            goal,
            memory_type=MemoryType.EPISODIC,
            limit=limit * 2  # Get extra to filter
        )

        episodes = []
        for entry in entries:
            try:
                ep_data = entry.content
                if isinstance(ep_data, dict):
                    ep = Episode(**ep_data)
                    if not success_only or ep.success:
                        episodes.append(ep)
            except (TypeError, KeyError) as e:
                self.logger.warning(f"Failed to parse episode: {e}")

        return episodes[:limit]

    async def get_lessons_for_goal(self, goal: str) -> List[str]:
        """Get lessons learned from similar past episodes."""
        episodes = await self.find_similar_episodes(goal, limit=10)
        lessons = []

        for ep in episodes:
            if ep.lessons_learned:
                lessons.extend(ep.lessons_learned)

        return list(set(lessons))  # Deduplicate

    def _calculate_reward(
        self,
        success: bool,
        actions: List[Dict],
        outcome: str
    ) -> float:
        """Calculate reward signal for the episode."""
        base_reward = 1.0 if success else 0.0

        # Efficiency bonus (fewer actions is better)
        if success and actions:
            efficiency = max(0, 1 - len(actions) / 20)
            base_reward += efficiency * 0.2

        return min(1.0, base_reward)


# ============================================================================
# LangGraph Checkpointer
# ============================================================================

class JARVISCheckpointer(BaseCheckpointSaver if LANGGRAPH_AVAILABLE else object):
    """
    Custom checkpointer for LangGraph with JARVIS integration.

    Supports multiple storage backends and integrates with
    the memory management system.
    """

    def __init__(
        self,
        storage_path: str = ".jarvis_cache/checkpoints",
        backend: StorageBackend = StorageBackend.SQLITE
    ):
        self.storage_path = Path(storage_path)
        self.backend = backend
        self._ensure_storage()

        self._checkpoints: Dict[str, Dict[str, CheckpointData]] = {}  # thread_id -> checkpoint_id -> data

        self.logger = logging.getLogger(__name__)

    def _ensure_storage(self) -> None:
        """Ensure storage directory exists."""
        self.storage_path.mkdir(parents=True, exist_ok=True)

        if self.backend == StorageBackend.SQLITE:
            self._init_sqlite()

    def _init_sqlite(self) -> None:
        """Initialize SQLite database."""
        db_path = self.storage_path / "checkpoints.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                state BLOB NOT NULL,
                metadata TEXT,
                parent_id TEXT,
                created_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_thread ON checkpoints(thread_id)
        """)

        conn.commit()
        conn.close()

    def put(
        self,
        config: Dict[str, Any],
        checkpoint: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Save a checkpoint.

        Args:
            config: Configuration with thread_id
            checkpoint: Checkpoint data
            metadata: Additional metadata

        Returns:
            Updated config with checkpoint_id
        """
        thread_id = config.get("configurable", {}).get("thread_id", "default")
        checkpoint_id = str(uuid4())

        data = CheckpointData(
            checkpoint_id=checkpoint_id,
            thread_id=thread_id,
            state=checkpoint,
            metadata=metadata or {},
            parent_id=config.get("configurable", {}).get("checkpoint_id")
        )

        # Store based on backend
        if self.backend == StorageBackend.MEMORY:
            if thread_id not in self._checkpoints:
                self._checkpoints[thread_id] = {}
            self._checkpoints[thread_id][checkpoint_id] = data

        elif self.backend == StorageBackend.SQLITE:
            self._save_sqlite(data)

        # Update config
        new_config = dict(config)
        if "configurable" not in new_config:
            new_config["configurable"] = {}
        new_config["configurable"]["checkpoint_id"] = checkpoint_id

        return new_config

    def get(self, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Get a checkpoint.

        Args:
            config: Configuration with thread_id and optionally checkpoint_id

        Returns:
            Checkpoint data or None
        """
        thread_id = config.get("configurable", {}).get("thread_id", "default")
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")

        data = None

        if self.backend == StorageBackend.MEMORY:
            thread_checkpoints = self._checkpoints.get(thread_id, {})
            if checkpoint_id:
                data = thread_checkpoints.get(checkpoint_id)
            elif thread_checkpoints:
                # Get latest
                data = max(thread_checkpoints.values(), key=lambda d: d.created_at)

        elif self.backend == StorageBackend.SQLITE:
            data = self._load_sqlite(thread_id, checkpoint_id)

        if data:
            return data.state
        return None

    async def aget(self, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Async version of get."""
        return self.get(config)

    async def aput(
        self,
        config: Dict[str, Any],
        checkpoint: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Async version of put."""
        return self.put(config, checkpoint, metadata)

    def list_checkpoints(self, thread_id: str) -> List[Dict[str, Any]]:
        """List all checkpoints for a thread."""
        if self.backend == StorageBackend.MEMORY:
            checkpoints = self._checkpoints.get(thread_id, {})
            return [
                {
                    "checkpoint_id": cp.checkpoint_id,
                    "created_at": cp.created_at.isoformat(),
                    "metadata": cp.metadata
                }
                for cp in sorted(checkpoints.values(), key=lambda c: c.created_at, reverse=True)
            ]

        elif self.backend == StorageBackend.SQLITE:
            return self._list_sqlite(thread_id)

        return []

    def delete_checkpoint(self, thread_id: str, checkpoint_id: str) -> bool:
        """Delete a specific checkpoint."""
        if self.backend == StorageBackend.MEMORY:
            if thread_id in self._checkpoints:
                if checkpoint_id in self._checkpoints[thread_id]:
                    del self._checkpoints[thread_id][checkpoint_id]
                    return True

        elif self.backend == StorageBackend.SQLITE:
            return self._delete_sqlite(thread_id, checkpoint_id)

        return False

    def _save_sqlite(self, data: CheckpointData) -> None:
        """Save checkpoint to SQLite."""
        db_path = self.storage_path / "checkpoints.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO checkpoints
            (checkpoint_id, thread_id, state, metadata, parent_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            data.checkpoint_id,
            data.thread_id,
            pickle.dumps(data.state),
            json.dumps(data.metadata),
            data.parent_id,
            data.created_at.isoformat()
        ))

        conn.commit()
        conn.close()

    def _load_sqlite(
        self,
        thread_id: str,
        checkpoint_id: Optional[str] = None
    ) -> Optional[CheckpointData]:
        """Load checkpoint from SQLite."""
        db_path = self.storage_path / "checkpoints.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        if checkpoint_id:
            cursor.execute(
                "SELECT * FROM checkpoints WHERE thread_id = ? AND checkpoint_id = ?",
                (thread_id, checkpoint_id)
            )
        else:
            cursor.execute(
                "SELECT * FROM checkpoints WHERE thread_id = ? ORDER BY created_at DESC LIMIT 1",
                (thread_id,)
            )

        row = cursor.fetchone()
        conn.close()

        if row:
            return CheckpointData(
                checkpoint_id=row[0],
                thread_id=row[1],
                state=pickle.loads(row[2]),
                metadata=json.loads(row[3]) if row[3] else {},
                parent_id=row[4],
                created_at=datetime.fromisoformat(row[5])
            )

        return None

    def _list_sqlite(self, thread_id: str) -> List[Dict[str, Any]]:
        """List checkpoints from SQLite."""
        db_path = self.storage_path / "checkpoints.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        cursor.execute(
            "SELECT checkpoint_id, created_at, metadata FROM checkpoints WHERE thread_id = ? ORDER BY created_at DESC",
            (thread_id,)
        )

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "checkpoint_id": row[0],
                "created_at": row[1],
                "metadata": json.loads(row[2]) if row[2] else {}
            }
            for row in rows
        ]

    def _delete_sqlite(self, thread_id: str, checkpoint_id: str) -> bool:
        """Delete checkpoint from SQLite."""
        db_path = self.storage_path / "checkpoints.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        cursor.execute(
            "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_id = ?",
            (thread_id, checkpoint_id)
        )

        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()

        return deleted


# ============================================================================
# Factory Functions
# ============================================================================

def create_memory_manager(
    backend: StorageBackend = StorageBackend.SQLITE,
    **kwargs
) -> MemoryManager:
    """
    Create a configured memory manager.

    Args:
        backend: Storage backend type
        **kwargs: Additional configuration

    Returns:
        Configured MemoryManager
    """
    if backend == StorageBackend.MEMORY:
        store = InMemoryStore(**kwargs.get("store_config", {}))
    elif backend == StorageBackend.SQLITE:
        store = SQLiteStore(**kwargs.get("store_config", {}))
    else:
        store = InMemoryStore()

    return MemoryManager(store=store, **{k: v for k, v in kwargs.items() if k != "store_config"})


def create_checkpointer(
    backend: StorageBackend = StorageBackend.SQLITE,
    storage_path: str = ".jarvis_cache/checkpoints"
) -> JARVISCheckpointer:
    """
    Create a configured checkpointer.

    Args:
        backend: Storage backend
        storage_path: Path for storage

    Returns:
        Configured JARVISCheckpointer
    """
    return JARVISCheckpointer(storage_path=storage_path, backend=backend)


# ============================================================================
# Convenience Functions
# ============================================================================

_default_memory_manager: Optional[MemoryManager] = None
_default_conversation: Optional[ConversationMemory] = None


def get_memory_manager() -> MemoryManager:
    """Get or create default memory manager."""
    global _default_memory_manager
    if _default_memory_manager is None:
        _default_memory_manager = create_memory_manager()
    return _default_memory_manager


def get_conversation_memory() -> ConversationMemory:
    """Get or create default conversation memory."""
    global _default_conversation
    if _default_conversation is None:
        _default_conversation = ConversationMemory()
    return _default_conversation


async def remember(
    content: Any,
    memory_type: MemoryType = MemoryType.SHORT_TERM,
    **kwargs
) -> str:
    """Remember content using default memory manager."""
    manager = get_memory_manager()
    return await manager.remember(content, memory_type, **kwargs)


async def recall(query: str, **kwargs) -> List[MemoryEntry]:
    """Recall memories using default memory manager."""
    manager = get_memory_manager()
    return await manager.recall(query, **kwargs)
