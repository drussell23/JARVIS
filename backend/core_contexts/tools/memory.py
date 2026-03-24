"""
Atomic memory tools -- store, recall, search, and manage persistent memory.

These tools provide all Core Contexts with semantic memory backed by
ChromaDB vector embeddings.  Memories are stored with types, priorities,
and metadata for rich retrieval.

Delegates to the existing MemoryAgent and SemanticMemory infrastructure.

The 397B Architect selects these tools by reading docstrings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MEMORY_TIMEOUT_S = float(os.environ.get("TOOL_MEMORY_TIMEOUT_S", "10.0"))

_memory_agent = None


@dataclass(frozen=True)
class MemoryEntry:
    """A stored memory entry.

    Attributes:
        id: Unique memory identifier.
        content: The memory content text.
        memory_type: Category (e.g., "fact", "preference", "interaction",
            "error_solution", "voice_profile").
        priority: Importance level (0.0 to 1.0).
        timestamp: When the memory was created.
        metadata: Additional structured data attached to the memory.
    """
    id: str
    content: str
    memory_type: str
    priority: float
    timestamp: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


async def store_memory(
    content: str,
    memory_type: str = "fact",
    priority: float = 0.5,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Store a piece of information in persistent semantic memory.

    The memory is vectorized and stored in ChromaDB for later retrieval
    via semantic similarity search.

    Args:
        content: The information to remember (natural language text).
        memory_type: Category for the memory.  Common values:
            "fact" -- a piece of knowledge about the world or user
            "preference" -- user preference or habit
            "interaction" -- record of a past interaction
            "error_solution" -- how an error was resolved
            "decision" -- a decision and its rationale
        priority: Importance level (0.0 = forgettable, 1.0 = critical).
            Higher priority memories are preferred in retrieval.
        metadata: Optional dict of structured data to attach.

    Returns:
        True if the memory was stored successfully.

    Use when:
        Any context learns something worth remembering for future
        interactions (user preferences, error solutions, task outcomes).
    """
    agent = await _get_memory_agent()
    if agent is None:
        return False

    try:
        result = await asyncio.wait_for(
            agent._store_memory({
                "action": "store_memory",
                "content": content,
                "memory_type": memory_type,
                "priority": priority,
                "data": metadata or {},
            }),
            timeout=_MEMORY_TIMEOUT_S,
        )
        success = result.get("stored", False)
        if success:
            logger.info("[tool:memory] Stored: %s (%s)", content[:60], memory_type)
        return success
    except Exception as exc:
        logger.error("[tool:memory] store_memory error: %s", exc)
        return False


async def recall_memory(
    query: str,
    memory_types: Optional[List[str]] = None,
    limit: int = 5,
) -> List[MemoryEntry]:
    """Search memory for information relevant to a query.

    Uses semantic similarity search (vector embeddings) to find memories
    that are conceptually related to the query, not just keyword matches.

    Args:
        query: Natural language query describing what to recall.
        memory_types: Optional filter to search only specific types.
        limit: Maximum number of memories to return (default 5).

    Returns:
        List of MemoryEntry objects, sorted by relevance.

    Use when:
        Any context needs to recall past knowledge before making a
        decision (e.g., "what does the user prefer for email formatting?"
        or "how did we fix this error last time?").
    """
    agent = await _get_memory_agent()
    if agent is None:
        return []

    try:
        result = await asyncio.wait_for(
            agent._recall_memory({
                "action": "recall_memory",
                "query": query,
                "memory_types": memory_types or [],
                "limit": limit,
            }),
            timeout=_MEMORY_TIMEOUT_S,
        )
        return [
            MemoryEntry(
                id=m.get("id", ""),
                content=m.get("content", ""),
                memory_type=m.get("memory_type", ""),
                priority=m.get("priority", 0.5),
                timestamp=m.get("timestamp", ""),
                metadata=m.get("metadata", {}),
            )
            for m in result.get("memories", [])
        ]
    except Exception as exc:
        logger.error("[tool:memory] recall_memory error: %s", exc)
        return []


async def recall_similar_context(
    context: str,
    memory_types: Optional[List[str]] = None,
    limit: int = 5,
) -> List[MemoryEntry]:
    """Recall memories similar to the current conversational context.

    Unlike recall_memory which takes a specific query, this takes the
    full current context (e.g., recent conversation) and finds memories
    that are contextually relevant.

    Args:
        context: Current conversational or task context.
        memory_types: Optional filter by memory type.
        limit: Maximum results.

    Returns:
        List of contextually relevant MemoryEntry objects.

    Use when:
        The Architect is planning a task and needs to pull in relevant
        past knowledge without a specific search query.
    """
    agent = await _get_memory_agent()
    if agent is None:
        return []

    try:
        result = await asyncio.wait_for(
            agent._recall_similar({
                "action": "recall_similar",
                "context": context,
                "memory_types": memory_types or [],
                "limit": limit,
            }),
            timeout=_MEMORY_TIMEOUT_S,
        )
        return [
            MemoryEntry(
                id=m.get("id", ""),
                content=m.get("content", ""),
                memory_type=m.get("memory_type", ""),
                priority=m.get("priority", 0.5),
                timestamp=m.get("timestamp", ""),
                metadata=m.get("metadata", {}),
            )
            for m in result.get("memories", [])
        ]
    except Exception as exc:
        logger.error("[tool:memory] recall_similar error: %s", exc)
        return []


async def find_patterns(
    pattern_type: str = "temporal",
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Find recurring patterns in stored memories.

    Searches for patterns across memory entries (e.g., repeated errors,
    recurring user behaviors, temporal patterns).

    Args:
        pattern_type: Type of pattern to search for.
            "temporal" -- time-based patterns
            "behavioral" -- user behavior patterns
            "error" -- recurring error patterns
        limit: Maximum patterns to return.

    Returns:
        List of pattern dicts with type, description, and frequency.

    Use when:
        The Observer is looking for recurring patterns to inform
        proactive behavior or alert the user to trends.
    """
    agent = await _get_memory_agent()
    if agent is None:
        return []

    try:
        result = await asyncio.wait_for(
            agent._find_patterns({
                "action": "find_patterns",
                "pattern_type": pattern_type,
                "limit": limit,
            }),
            timeout=_MEMORY_TIMEOUT_S,
        )
        return result.get("patterns", [])
    except Exception as exc:
        logger.error("[tool:memory] find_patterns error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

async def _get_memory_agent():
    global _memory_agent
    if _memory_agent is not None:
        return _memory_agent

    for path in ("backend.neural_mesh.agents.memory_agent",
                 "neural_mesh.agents.memory_agent"):
        try:
            import importlib
            mod = importlib.import_module(path)
            _memory_agent = mod.MemoryAgent()
            return _memory_agent
        except (ImportError, Exception):
            continue
    return None
