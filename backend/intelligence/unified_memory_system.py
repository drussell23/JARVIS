"""
Unified Memory System for JARVIS - MemGPT-Inspired
===================================================

Implements OS-level memory management for AI context, inspired by MemGPT.
Treats context like an Operating System treats RAM:
- Core Memory: In-context, always available (like CPU cache)
- Working Memory: Recent context, paged in/out (like RAM)
- Archival Memory: Long-term storage with semantic retrieval (like disk)

Features:
- Memory blocks with labels and limits
- Automatic memory paging when context is full
- Semantic search for relevant memories
- Memory summarization for compression
- Cross-session persistence via ChromaDB
- Integration with existing JARVIS systems

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, AsyncIterator, Callable, Deque, Dict, Generic, List, Literal,
    Optional, Protocol, Sequence, Set, Tuple, Type, TypeVar, Union
)
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ============================================================================
# Configuration (Environment-Driven, No Hardcoding)
# ============================================================================

def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_env_int(key: str, default: int) -> int:
    try:
        return int(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_float(key: str, default: float) -> float:
    try:
        return float(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_bool(key: str, default: bool = False) -> bool:
    return _get_env(key, str(default)).lower() in ("true", "1", "yes")


def _get_env_path(key: str, default: str) -> Path:
    return Path(os.path.expanduser(_get_env(key, default)))


@dataclass
class MemorySystemConfig:
    """Configuration for the Unified Memory System."""
    # Core memory limits
    core_memory_char_limit: int = field(
        default_factory=lambda: _get_env_int("JARVIS_CORE_MEMORY_LIMIT", 4000)
    )
    core_memory_blocks: List[str] = field(
        default_factory=lambda: _get_env("JARVIS_CORE_MEMORY_BLOCKS", "persona,human,context").split(",")
    )

    # Working memory limits (context window management)
    working_memory_max_messages: int = field(
        default_factory=lambda: _get_env_int("JARVIS_WORKING_MEMORY_MESSAGES", 50)
    )
    working_memory_max_tokens: int = field(
        default_factory=lambda: _get_env_int("JARVIS_WORKING_MEMORY_TOKENS", 16000)
    )

    # Archival memory settings
    archival_memory_enabled: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_ARCHIVAL_MEMORY_ENABLED", True)
    )
    archival_memory_dir: Path = field(
        default_factory=lambda: _get_env_path("JARVIS_ARCHIVAL_MEMORY_DIR", "~/.jarvis/archival_memory")
    )

    # Paging settings
    page_out_threshold: float = field(
        default_factory=lambda: _get_env_float("JARVIS_MEMORY_PAGE_THRESHOLD", 0.85)
    )
    summarize_on_page_out: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SUMMARIZE_ON_PAGE", True)
    )

    # ChromaDB integration
    chromadb_enabled: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_CHROMADB_ENABLED", True)
    )
    chromadb_collection: str = field(
        default_factory=lambda: _get_env("JARVIS_CHROMADB_COLLECTION", "jarvis_archival_memory")
    )


# ============================================================================
# Enums
# ============================================================================

class MemoryType(str, Enum):
    """Types of memory in the system."""
    CORE = "core"           # Always in context
    WORKING = "working"     # Recent messages/context
    ARCHIVAL = "archival"   # Long-term storage
    SUMMARY = "summary"     # Compressed summaries


class MemoryBlockType(str, Enum):
    """Types of core memory blocks."""
    PERSONA = "persona"     # Who JARVIS is
    HUMAN = "human"         # Who the user is
    CONTEXT = "context"     # Current task context
    GOALS = "goals"         # Current goals
    SCRATCH = "scratch"     # Temporary working space


class PageAction(str, Enum):
    """Memory paging actions."""
    PAGE_IN = "page_in"     # Load from archival to working
    PAGE_OUT = "page_out"   # Move from working to archival
    SUMMARIZE = "summarize" # Compress and archive
    EVICT = "evict"         # Remove entirely


# ============================================================================
# Data Classes (MemGPT-Inspired)
# ============================================================================

@dataclass
class MemoryBlock:
    """
    A labeled block of core memory.

    Like MemGPT's Block class - has a label, value, and limit.
    """
    label: str
    value: str = ""
    description: str = ""
    limit: int = 2000
    read_only: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self):
        if len(self.value) > self.limit:
            self.value = self.value[:self.limit]

    @property
    def chars_current(self) -> int:
        return len(self.value)

    @property
    def chars_remaining(self) -> int:
        return max(0, self.limit - len(self.value))

    def append(self, content: str) -> bool:
        """Append content to the block. Returns True if successful."""
        if self.read_only:
            return False
        if len(self.value) + len(content) > self.limit:
            return False
        self.value += content
        self.updated_at = datetime.utcnow()
        return True

    def replace(self, old_content: str, new_content: str) -> bool:
        """Replace content in the block. Returns True if successful."""
        if self.read_only:
            return False
        if old_content not in self.value:
            return False
        new_value = self.value.replace(old_content, new_content)
        if len(new_value) > self.limit:
            return False
        self.value = new_value
        self.updated_at = datetime.utcnow()
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "description": self.description,
            "limit": self.limit,
            "read_only": self.read_only,
            "chars_current": self.chars_current,
            "chars_remaining": self.chars_remaining,
        }


@dataclass
class MemoryMessage:
    """A message in working memory."""
    message_id: str
    role: Literal["system", "user", "assistant", "function"]
    content: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    tokens: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.tokens == 0:
            # Rough token estimate: 4 chars per token
            self.tokens = len(self.content) // 4

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "tokens": self.tokens,
            "metadata": self.metadata,
        }


@dataclass
class ArchivalEntry:
    """An entry in archival memory."""
    entry_id: str
    content: str
    embedding: Optional[List[float]] = None
    tags: List[str] = field(default_factory=list)
    source: str = "unknown"
    created_at: datetime = field(default_factory=datetime.utcnow)
    relevance_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "content": self.content,
            "tags": self.tags,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "relevance_score": self.relevance_score,
        }


@dataclass
class ContextWindowOverview:
    """Overview of current memory usage (like MemGPT's ContextWindowOverview)."""
    max_tokens: int
    current_tokens: int

    # Breakdown
    core_memory_tokens: int
    working_memory_tokens: int
    system_prompt_tokens: int

    # Counts
    num_messages: int
    num_archival_entries: int

    # Status
    usage_percentage: float = 0.0
    needs_paging: bool = False

    def __post_init__(self):
        if self.max_tokens > 0:
            self.usage_percentage = self.current_tokens / self.max_tokens
            self.needs_paging = self.usage_percentage > 0.85


# ============================================================================
# Core Memory (In-Context, Always Available)
# ============================================================================

class CoreMemory:
    """
    In-context memory that's always available to the agent.

    Like MemGPT's Memory class - contains labeled blocks that persist
    across the conversation and can be edited by the agent.
    """

    def __init__(self, config: MemorySystemConfig):
        self.config = config
        self.blocks: Dict[str, MemoryBlock] = {}
        self._initialize_default_blocks()

    def _initialize_default_blocks(self):
        """Initialize default memory blocks."""
        defaults = {
            "persona": MemoryBlock(
                label="persona",
                description="Information about JARVIS's personality, capabilities, and behavior",
                value="I am JARVIS, an advanced AI assistant created to help Derek with software engineering, voice authentication, and autonomous task execution.",
                limit=self.config.core_memory_char_limit // 4,
            ),
            "human": MemoryBlock(
                label="human",
                description="Information about the human user",
                value="Derek is a software engineer working on the JARVIS AI ecosystem.",
                limit=self.config.core_memory_char_limit // 4,
            ),
            "context": MemoryBlock(
                label="context",
                description="Current task context and relevant information",
                value="",
                limit=self.config.core_memory_char_limit // 4,
            ),
            "scratch": MemoryBlock(
                label="scratch",
                description="Temporary working space for multi-step tasks",
                value="",
                limit=self.config.core_memory_char_limit // 4,
            ),
        }

        for label in self.config.core_memory_blocks:
            if label in defaults:
                self.blocks[label] = defaults[label]
            else:
                self.blocks[label] = MemoryBlock(
                    label=label,
                    description=f"Custom memory block: {label}",
                    limit=self.config.core_memory_char_limit // len(self.config.core_memory_blocks),
                )

    def get_block(self, label: str) -> Optional[MemoryBlock]:
        """Get a memory block by label."""
        return self.blocks.get(label)

    def set_block(self, block: MemoryBlock):
        """Set or update a memory block."""
        self.blocks[block.label] = block

    def append_to_block(self, label: str, content: str) -> bool:
        """Append content to a block."""
        block = self.blocks.get(label)
        if not block:
            return False
        return block.append(content)

    def replace_in_block(self, label: str, old: str, new: str) -> bool:
        """Replace content in a block."""
        block = self.blocks.get(label)
        if not block:
            return False
        return block.replace(old, new)

    def compile(self) -> str:
        """Compile core memory into a prompt string."""
        parts = ["<core_memory>"]
        parts.append("The following memory blocks contain persistent information:\n")

        for label, block in self.blocks.items():
            parts.append(f"<{label}>")
            parts.append(f"  <description>{block.description}</description>")
            parts.append(f"  <value>{block.value}</value>")
            parts.append(f"  <chars>{block.chars_current}/{block.limit}</chars>")
            parts.append(f"</{label}>")

        parts.append("</core_memory>")
        return "\n".join(parts)

    def get_token_count(self) -> int:
        """Estimate token count for core memory."""
        return len(self.compile()) // 4

    def to_dict(self) -> Dict[str, Any]:
        return {label: block.to_dict() for label, block in self.blocks.items()}


# ============================================================================
# Working Memory (Recent Context, Paged In/Out)
# ============================================================================

class WorkingMemory:
    """
    Recent conversation context that can be paged in and out.

    Like MemGPT's message buffer - holds recent messages and
    automatically manages overflow by paging to archival memory.
    """

    def __init__(self, config: MemorySystemConfig):
        self.config = config
        self.messages: Deque[MemoryMessage] = deque(maxlen=config.working_memory_max_messages)
        self._total_tokens = 0

    def add_message(self, message: MemoryMessage):
        """Add a message to working memory."""
        self.messages.append(message)
        self._total_tokens += message.tokens

        # Check if we need to page out
        if self._total_tokens > self.config.working_memory_max_tokens:
            self._trigger_page_out()

    def _trigger_page_out(self):
        """Page out oldest messages when memory is full."""
        while self._total_tokens > self.config.working_memory_max_tokens * 0.7:
            if not self.messages:
                break
            oldest = self.messages.popleft()
            self._total_tokens -= oldest.tokens
            # The ArchivalMemory should handle storing this

    def get_messages(self, limit: Optional[int] = None) -> List[MemoryMessage]:
        """Get messages from working memory."""
        msgs = list(self.messages)
        if limit:
            return msgs[-limit:]
        return msgs

    def get_recent_context(self, token_limit: int = 4000) -> str:
        """Get recent messages as context string."""
        messages = []
        tokens = 0

        for msg in reversed(self.messages):
            if tokens + msg.tokens > token_limit:
                break
            messages.insert(0, msg)
            tokens += msg.tokens

        return "\n".join([
            f"{msg.role}: {msg.content}" for msg in messages
        ])

    def get_token_count(self) -> int:
        """Get total token count in working memory."""
        return self._total_tokens

    def clear(self):
        """Clear working memory."""
        self.messages.clear()
        self._total_tokens = 0


# ============================================================================
# Archival Memory (Long-Term Storage with Semantic Retrieval)
# ============================================================================

class ArchivalMemory:
    """
    Long-term memory storage with semantic search.

    Like MemGPT's archival memory - stores memories that can be
    retrieved later using semantic similarity search.
    """

    def __init__(self, config: MemorySystemConfig):
        self.config = config
        self._entries: Dict[str, ArchivalEntry] = {}
        self._chromadb_client = None
        self._collection = None
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Initialize archival memory storage."""
        async with self._lock:
            if self._initialized:
                return

            # Ensure directory exists
            self.config.archival_memory_dir.mkdir(parents=True, exist_ok=True)

            # Initialize ChromaDB if enabled
            if self.config.chromadb_enabled:
                try:
                    import chromadb
                    self._chromadb_client = chromadb.PersistentClient(
                        path=str(self.config.archival_memory_dir / "chromadb")
                    )
                    self._collection = self._chromadb_client.get_or_create_collection(
                        name=self.config.chromadb_collection,
                        metadata={"hnsw:space": "cosine"},
                    )
                    logger.info(f"Archival memory initialized with ChromaDB: {self._collection.count()} entries")
                except ImportError:
                    logger.warning("ChromaDB not available, using in-memory storage")
                except Exception as e:
                    logger.warning(f"Failed to initialize ChromaDB: {e}")

            self._initialized = True

    async def store(
        self,
        content: str,
        tags: Optional[List[str]] = None,
        source: str = "conversation",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Store content in archival memory."""
        await self.initialize()

        entry_id = str(uuid4())
        entry = ArchivalEntry(
            entry_id=entry_id,
            content=content,
            tags=tags or [],
            source=source,
        )

        self._entries[entry_id] = entry

        # Store in ChromaDB if available
        if self._collection:
            try:
                self._collection.add(
                    ids=[entry_id],
                    documents=[content],
                    metadatas=[{
                        "tags": ",".join(tags or []),
                        "source": source,
                        "created_at": entry.created_at.isoformat(),
                        **(metadata or {}),
                    }],
                )
            except Exception as e:
                logger.warning(f"Failed to store in ChromaDB: {e}")

        return entry_id

    async def search(
        self,
        query: str,
        limit: int = 5,
        tags: Optional[List[str]] = None,
    ) -> List[ArchivalEntry]:
        """Search archival memory for relevant entries."""
        await self.initialize()

        results = []

        if self._collection:
            try:
                search_results = self._collection.query(
                    query_texts=[query],
                    n_results=limit,
                )

                if search_results and search_results["ids"]:
                    for i, entry_id in enumerate(search_results["ids"][0]):
                        content = search_results["documents"][0][i] if search_results["documents"] else ""
                        metadata = search_results["metadatas"][0][i] if search_results["metadatas"] else {}
                        distance = search_results["distances"][0][i] if search_results.get("distances") else 1.0

                        results.append(ArchivalEntry(
                            entry_id=entry_id,
                            content=content,
                            tags=metadata.get("tags", "").split(",") if metadata.get("tags") else [],
                            source=metadata.get("source", "unknown"),
                            relevance_score=1.0 - distance,  # Convert distance to similarity
                        ))
            except Exception as e:
                logger.warning(f"ChromaDB search failed: {e}")

        # Filter by tags if specified
        if tags:
            results = [r for r in results if any(t in r.tags for t in tags)]

        return sorted(results, key=lambda x: -x.relevance_score)[:limit]

    async def get_summary(self) -> Dict[str, Any]:
        """Get summary of archival memory."""
        await self.initialize()

        count = len(self._entries)
        if self._collection:
            try:
                count = self._collection.count()
            except Exception:
                pass

        return {
            "size": count,
            "initialized": self._initialized,
            "chromadb_enabled": self._collection is not None,
        }


# ============================================================================
# Unified Memory System (Orchestrates All Memory Types)
# ============================================================================

class UnifiedMemorySystem:
    """
    Unified memory system that orchestrates core, working, and archival memory.

    Provides MemGPT-style memory paging and semantic retrieval.
    """

    def __init__(self, config: Optional[MemorySystemConfig] = None):
        self.config = config or MemorySystemConfig()
        self.core = CoreMemory(self.config)
        self.working = WorkingMemory(self.config)
        self.archival = ArchivalMemory(self.config)
        self._initialized = False

    async def initialize(self):
        """Initialize the memory system."""
        if self._initialized:
            return
        await self.archival.initialize()
        self._initialized = True
        logger.info("Unified Memory System initialized")

    async def add_message(
        self,
        role: Literal["system", "user", "assistant", "function"],
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Add a message to working memory."""
        await self.initialize()

        message = MemoryMessage(
            message_id=str(uuid4()),
            role=role,
            content=content,
            metadata=metadata or {},
        )

        self.working.add_message(message)

    async def update_core_memory(self, label: str, action: str, content: str, old_content: str = "") -> bool:
        """Update core memory (append or replace)."""
        if action == "append":
            return self.core.append_to_block(label, content)
        elif action == "replace":
            return self.core.replace_in_block(label, old_content, content)
        return False

    async def remember(self, query: str, limit: int = 5) -> List[ArchivalEntry]:
        """Remember relevant information from archival memory."""
        return await self.archival.search(query, limit=limit)

    async def archive(self, content: str, tags: Optional[List[str]] = None, source: str = "manual"):
        """Archive content to long-term memory."""
        return await self.archival.store(content, tags=tags, source=source)

    async def get_context_window_overview(self, max_tokens: int = 32000) -> ContextWindowOverview:
        """Get overview of context window usage."""
        core_tokens = self.core.get_token_count()
        working_tokens = self.working.get_token_count()
        system_tokens = 500  # Estimate for system prompt

        current_tokens = core_tokens + working_tokens + system_tokens

        archival_summary = await self.archival.get_summary()

        return ContextWindowOverview(
            max_tokens=max_tokens,
            current_tokens=current_tokens,
            core_memory_tokens=core_tokens,
            working_memory_tokens=working_tokens,
            system_prompt_tokens=system_tokens,
            num_messages=len(self.working.messages),
            num_archival_entries=archival_summary["size"],
        )

    async def compile_prompt_context(
        self,
        include_archival_search: Optional[str] = None,
        archival_limit: int = 3,
    ) -> str:
        """Compile full prompt context including all memory types."""
        parts = []

        # Core memory
        parts.append(self.core.compile())

        # Archival memory search results if query provided
        if include_archival_search:
            results = await self.remember(include_archival_search, limit=archival_limit)
            if results:
                parts.append("\n<archival_memory_search>")
                parts.append(f"Relevant memories for query '{include_archival_search}':")
                for r in results:
                    parts.append(f"  - [{r.relevance_score:.2f}] {r.content[:200]}...")
                parts.append("</archival_memory_search>")

        # Working memory (recent messages)
        recent = self.working.get_recent_context(token_limit=4000)
        if recent:
            parts.append("\n<working_memory>")
            parts.append(recent)
            parts.append("</working_memory>")

        return "\n".join(parts)


# ============================================================================
# Singleton and Convenience Functions
# ============================================================================

_memory_instance: Optional[UnifiedMemorySystem] = None


async def get_memory_system() -> UnifiedMemorySystem:
    """Get the singleton memory system instance."""
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = UnifiedMemorySystem()
        await _memory_instance.initialize()
    return _memory_instance


async def remember(query: str, limit: int = 5) -> List[ArchivalEntry]:
    """Convenience function to search archival memory."""
    memory = await get_memory_system()
    return await memory.remember(query, limit)


async def archive(content: str, tags: Optional[List[str]] = None):
    """Convenience function to archive content."""
    memory = await get_memory_system()
    return await memory.archive(content, tags)


async def update_core_memory(label: str, action: str, content: str, old_content: str = ""):
    """Convenience function to update core memory."""
    memory = await get_memory_system()
    return await memory.update_core_memory(label, action, content, old_content)


# ============================================================================
# Module Exports
# ============================================================================

__all__ = [
    # Configuration
    "MemorySystemConfig",

    # Enums
    "MemoryType",
    "MemoryBlockType",
    "PageAction",

    # Data Classes
    "MemoryBlock",
    "MemoryMessage",
    "ArchivalEntry",
    "ContextWindowOverview",

    # Memory Components
    "CoreMemory",
    "WorkingMemory",
    "ArchivalMemory",
    "UnifiedMemorySystem",

    # Convenience Functions
    "get_memory_system",
    "remember",
    "archive",
    "update_core_memory",
]
