"""
Unified Memory Manager
======================

Provides persistent memory across autonomous tasks with:
- Working memory (current task context)
- Episodic memory (task execution records)
- Semantic memory (learned patterns)
- Experience replay for similar goals
- Integration with Neural Mesh knowledge graph

v1.0: Initial implementation with multi-tier memory architecture.

Author: JARVIS AI System
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class MemoryManagerConfig:
    """Configuration for the Unified Memory Manager."""

    # Memory limits
    working_memory_max_items: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_WORKING_MEMORY_MAX", "100"))
    )
    episodic_memory_max_items: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_EPISODIC_MEMORY_MAX", "1000"))
    )
    semantic_memory_max_items: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_SEMANTIC_MEMORY_MAX", "500"))
    )

    # Persistence settings
    persistence_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_MEMORY_PERSIST", "true").lower() == "true"
    )
    persistence_path: str = field(
        default_factory=lambda: os.getenv("JARVIS_MEMORY_PATH", "/tmp/jarvis_memory")
    )
    auto_save_interval: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_MEMORY_SAVE_INTERVAL", "60"))
    )

    # Consolidation settings
    consolidation_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_MEMORY_CONSOLIDATE", "true").lower() == "true"
    )
    consolidation_threshold: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_CONSOLIDATION_THRESHOLD", "10"))
    )

    # Experience replay settings
    replay_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_EXPERIENCE_REPLAY", "true").lower() == "true"
    )
    replay_similarity_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_REPLAY_SIMILARITY", "0.75"))
    )


# =============================================================================
# Memory Types
# =============================================================================


class MemoryType(Enum):
    """Types of memory storage."""

    WORKING = auto()  # Short-term task context
    EPISODIC = auto()  # Task execution records
    SEMANTIC = auto()  # Learned patterns and knowledge


class MemoryImportance(Enum):
    """Importance levels for memory items."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class MemoryItem:
    """A single memory item."""

    memory_id: str
    memory_type: MemoryType
    content: Any
    context: Dict[str, Any]
    importance: MemoryImportance
    created_at: float
    accessed_at: float
    access_count: int
    tags: List[str] = field(default_factory=list)
    embedding: Optional[List[float]] = None
    ttl: Optional[float] = None  # Time-to-live in seconds

    def is_expired(self) -> bool:
        """Check if memory has expired."""
        if self.ttl is None:
            return False
        return time.time() - self.created_at > self.ttl

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "memory_id": self.memory_id,
            "memory_type": self.memory_type.name,
            "content": self.content,
            "context": self.context,
            "importance": self.importance.name,
            "created_at": self.created_at,
            "accessed_at": self.accessed_at,
            "access_count": self.access_count,
            "tags": self.tags,
            "ttl": self.ttl,
        }


@dataclass
class EpisodicRecord:
    """Record of a task execution."""

    task_id: str
    goal: str
    outcome: str  # success, failure, partial
    actions: List[Dict[str, Any]]
    duration: float
    context: Dict[str, Any]
    learnings: List[str]
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "outcome": self.outcome,
            "actions_count": len(self.actions),
            "duration": self.duration,
            "learnings": self.learnings,
            "timestamp": self.timestamp,
        }


@dataclass
class SemanticPattern:
    """A learned pattern or knowledge item."""

    pattern_id: str
    description: str
    conditions: List[str]
    actions: List[str]
    success_rate: float
    usage_count: int
    confidence: float
    created_from: List[str]  # Episodic record IDs
    updated_at: float = field(default_factory=time.time)


# =============================================================================
# Unified Memory Manager
# =============================================================================


class UnifiedMemoryManager:
    """
    Manages persistent memory across autonomous tasks.

    Provides:
    - Multi-tier memory (working, episodic, semantic)
    - Automatic consolidation of episodic to semantic
    - Experience replay for similar goals
    - Persistence with auto-save
    """

    def __init__(
        self,
        config: Optional[MemoryManagerConfig] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """Initialize the memory manager."""
        self.config = config or MemoryManagerConfig()
        self.logger = logger or logging.getLogger(__name__)

        # Memory stores
        self._working_memory: Dict[str, MemoryItem] = {}
        self._episodic_memory: Dict[str, EpisodicRecord] = {}
        self._semantic_memory: Dict[str, SemanticPattern] = {}

        # Index for fast lookup
        self._tag_index: Dict[str, Set[str]] = {}  # tag -> memory_ids
        self._goal_index: Dict[str, List[str]] = {}  # goal_hash -> episodic_ids

        # Persistence
        self._save_task: Optional[asyncio.Task] = None
        self._dirty = False

        # Statistics
        self._stats = {
            "working_writes": 0,
            "episodic_writes": 0,
            "semantic_writes": 0,
            "consolidations": 0,
            "replays": 0,
            "saves": 0,
            "loads": 0,
        }

        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Initialize the memory manager."""
        if self._initialized:
            return True

        try:
            self.logger.info("[MemoryManager] Initializing Unified Memory Manager...")

            # Create persistence directory
            if self.config.persistence_enabled:
                os.makedirs(self.config.persistence_path, exist_ok=True)
                await self._load_from_disk()

            # Start auto-save task
            if self.config.persistence_enabled:
                self._save_task = asyncio.create_task(self._auto_save_loop())

            self._initialized = True
            self.logger.info(
                f"[MemoryManager] ✓ Initialized with "
                f"{len(self._working_memory)} working, "
                f"{len(self._episodic_memory)} episodic, "
                f"{len(self._semantic_memory)} semantic memories"
            )
            return True

        except Exception as e:
            self.logger.error(f"[MemoryManager] Initialization failed: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown the memory manager."""
        if not self._initialized:
            return

        self.logger.info("[MemoryManager] Shutting down...")

        # Stop auto-save
        if self._save_task:
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass

        # Final save
        if self.config.persistence_enabled and self._dirty:
            await self._save_to_disk()

        self._initialized = False
        self.logger.info("[MemoryManager] ✓ Shutdown complete")

    # =========================================================================
    # Working Memory
    # =========================================================================

    async def set_working(
        self,
        key: str,
        value: Any,
        context: Optional[Dict[str, Any]] = None,
        importance: MemoryImportance = MemoryImportance.MEDIUM,
        tags: Optional[List[str]] = None,
        ttl: Optional[float] = None,
    ) -> str:
        """Store a value in working memory."""
        async with self._lock:
            memory_id = f"working_{key}"

            item = MemoryItem(
                memory_id=memory_id,
                memory_type=MemoryType.WORKING,
                content=value,
                context=context or {},
                importance=importance,
                created_at=time.time(),
                accessed_at=time.time(),
                access_count=0,
                tags=tags or [],
                ttl=ttl,
            )

            # Enforce memory limit
            if len(self._working_memory) >= self.config.working_memory_max_items:
                await self._evict_working_memory()

            self._working_memory[memory_id] = item
            self._index_tags(memory_id, tags or [])
            self._stats["working_writes"] += 1
            self._dirty = True

            return memory_id

    async def get_working(self, key: str) -> Optional[Any]:
        """Retrieve a value from working memory."""
        memory_id = f"working_{key}"
        item = self._working_memory.get(memory_id)

        if item is None:
            return None

        if item.is_expired():
            del self._working_memory[memory_id]
            return None

        item.accessed_at = time.time()
        item.access_count += 1

        return item.content

    async def clear_working(self) -> None:
        """Clear all working memory."""
        async with self._lock:
            self._working_memory.clear()
            self._dirty = True

    async def _evict_working_memory(self) -> None:
        """Evict least important/used items from working memory."""
        if not self._working_memory:
            return

        # Sort by importance (low first) and access count (low first)
        items = sorted(
            self._working_memory.items(),
            key=lambda x: (x[1].importance.value, x[1].access_count),
        )

        # Remove 10% of items
        to_remove = max(1, len(items) // 10)
        for memory_id, _ in items[:to_remove]:
            del self._working_memory[memory_id]

    # =========================================================================
    # Episodic Memory
    # =========================================================================

    async def record_episode(
        self,
        task_id: str,
        goal: str,
        outcome: str,
        actions: List[Dict[str, Any]],
        duration: float,
        context: Optional[Dict[str, Any]] = None,
        learnings: Optional[List[str]] = None,
    ) -> str:
        """Record a task execution episode."""
        async with self._lock:
            record = EpisodicRecord(
                task_id=task_id,
                goal=goal,
                outcome=outcome,
                actions=actions,
                duration=duration,
                context=context or {},
                learnings=learnings or [],
            )

            # Enforce memory limit
            if len(self._episodic_memory) >= self.config.episodic_memory_max_items:
                await self._evict_episodic_memory()

            self._episodic_memory[task_id] = record
            self._stats["episodic_writes"] += 1
            self._dirty = True

            # Index by goal hash
            goal_hash = self._hash_goal(goal)
            if goal_hash not in self._goal_index:
                self._goal_index[goal_hash] = []
            self._goal_index[goal_hash].append(task_id)

            # Check if consolidation is needed
            if self.config.consolidation_enabled:
                await self._maybe_consolidate(goal_hash)

            return task_id

    async def get_episode(self, task_id: str) -> Optional[EpisodicRecord]:
        """Get an episode by task ID."""
        return self._episodic_memory.get(task_id)

    async def find_similar_episodes(
        self,
        goal: str,
        max_results: int = 5,
    ) -> List[EpisodicRecord]:
        """Find episodes with similar goals."""
        goal_hash = self._hash_goal(goal)

        # Get direct matches
        matching_ids = self._goal_index.get(goal_hash, [])

        results = [
            self._episodic_memory[tid]
            for tid in matching_ids
            if tid in self._episodic_memory
        ]

        # Sort by recency and success
        results.sort(
            key=lambda r: (r.outcome == "success", r.timestamp),
            reverse=True,
        )

        return results[:max_results]

    async def _evict_episodic_memory(self) -> None:
        """Evict oldest episodes."""
        if not self._episodic_memory:
            return

        # Sort by timestamp (oldest first)
        items = sorted(
            self._episodic_memory.items(),
            key=lambda x: x[1].timestamp,
        )

        # Remove 10% of items
        to_remove = max(1, len(items) // 10)
        for task_id, _ in items[:to_remove]:
            del self._episodic_memory[task_id]

    # =========================================================================
    # Semantic Memory
    # =========================================================================

    async def store_pattern(
        self,
        pattern_id: str,
        description: str,
        conditions: List[str],
        actions: List[str],
        success_rate: float,
        confidence: float,
        created_from: Optional[List[str]] = None,
    ) -> str:
        """Store a learned pattern."""
        async with self._lock:
            # Check if pattern exists
            existing = self._semantic_memory.get(pattern_id)
            if existing:
                # Update existing pattern
                existing.success_rate = (existing.success_rate + success_rate) / 2
                existing.usage_count += 1
                existing.confidence = (existing.confidence + confidence) / 2
                existing.updated_at = time.time()
                if created_from:
                    existing.created_from.extend(created_from)
            else:
                # Create new pattern
                pattern = SemanticPattern(
                    pattern_id=pattern_id,
                    description=description,
                    conditions=conditions,
                    actions=actions,
                    success_rate=success_rate,
                    usage_count=1,
                    confidence=confidence,
                    created_from=created_from or [],
                )

                # Enforce memory limit
                if len(self._semantic_memory) >= self.config.semantic_memory_max_items:
                    await self._evict_semantic_memory()

                self._semantic_memory[pattern_id] = pattern

            self._stats["semantic_writes"] += 1
            self._dirty = True

            return pattern_id

    async def find_patterns(
        self,
        conditions: List[str],
        max_results: int = 5,
    ) -> List[SemanticPattern]:
        """Find patterns matching conditions."""
        matches: List[Tuple[float, SemanticPattern]] = []

        for pattern in self._semantic_memory.values():
            # Calculate condition overlap
            overlap = len(
                set(c.lower() for c in conditions)
                & set(c.lower() for c in pattern.conditions)
            )
            if overlap > 0:
                score = overlap / max(len(pattern.conditions), 1) * pattern.confidence
                matches.append((score, pattern))

        # Sort by score
        matches.sort(key=lambda x: x[0], reverse=True)

        return [p for _, p in matches[:max_results]]

    async def _evict_semantic_memory(self) -> None:
        """Evict low-value patterns."""
        if not self._semantic_memory:
            return

        # Sort by usage count and success rate
        items = sorted(
            self._semantic_memory.items(),
            key=lambda x: (x[1].usage_count, x[1].success_rate),
        )

        # Remove 10% of items
        to_remove = max(1, len(items) // 10)
        for pattern_id, _ in items[:to_remove]:
            del self._semantic_memory[pattern_id]

    # =========================================================================
    # Consolidation
    # =========================================================================

    async def _maybe_consolidate(self, goal_hash: str) -> None:
        """Check if consolidation should occur for a goal."""
        episode_ids = self._goal_index.get(goal_hash, [])

        if len(episode_ids) < self.config.consolidation_threshold:
            return

        await self._consolidate_episodes(goal_hash, episode_ids)

    async def _consolidate_episodes(
        self,
        goal_hash: str,
        episode_ids: List[str],
    ) -> Optional[str]:
        """Consolidate multiple episodes into a semantic pattern."""
        episodes = [
            self._episodic_memory[tid]
            for tid in episode_ids
            if tid in self._episodic_memory
        ]

        if not episodes:
            return None

        # Calculate success rate
        success_count = sum(1 for e in episodes if e.outcome == "success")
        success_rate = success_count / len(episodes)

        # Extract common actions from successful episodes
        successful_episodes = [e for e in episodes if e.outcome == "success"]
        if not successful_episodes:
            return None

        # Find common action patterns
        action_counts: Dict[str, int] = {}
        for episode in successful_episodes:
            for action in episode.actions:
                action_type = action.get("type", "unknown")
                action_counts[action_type] = action_counts.get(action_type, 0) + 1

        # Get most common actions
        common_actions = [
            action for action, count in action_counts.items()
            if count >= len(successful_episodes) * 0.5
        ]

        # Create pattern
        pattern_id = f"pattern_{goal_hash[:8]}"
        goal = episodes[0].goal  # They should all have similar goals

        await self.store_pattern(
            pattern_id=pattern_id,
            description=f"Pattern for: {goal[:50]}",
            conditions=[goal],
            actions=common_actions,
            success_rate=success_rate,
            confidence=min(0.9, len(successful_episodes) / 10),
            created_from=episode_ids,
        )

        self._stats["consolidations"] += 1
        self.logger.info(
            f"[MemoryManager] Consolidated {len(episode_ids)} episodes into pattern {pattern_id}"
        )

        return pattern_id

    # =========================================================================
    # Experience Replay
    # =========================================================================

    async def replay_for_goal(
        self,
        goal: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Find relevant experience for a new goal.

        Returns replay data if similar successful experience exists.
        """
        if not self.config.replay_enabled:
            return None

        # Find similar episodes
        episodes = await self.find_similar_episodes(goal)

        # Filter to successful ones
        successful = [e for e in episodes if e.outcome == "success"]
        if not successful:
            return None

        # Get best episode
        best = successful[0]

        # Find matching patterns
        patterns = await self.find_patterns([goal])

        self._stats["replays"] += 1

        return {
            "similar_episode": best.to_dict(),
            "suggested_actions": [a.get("type") for a in best.actions[:5]],
            "expected_duration": best.duration,
            "learnings": best.learnings,
            "matching_patterns": [
                {"id": p.pattern_id, "success_rate": p.success_rate}
                for p in patterns[:3]
            ],
        }

    # =========================================================================
    # Persistence
    # =========================================================================

    async def _save_to_disk(self) -> None:
        """Save memory to disk."""
        if not self.config.persistence_enabled:
            return

        try:
            base_path = self.config.persistence_path

            # Save working memory
            working_data = {
                mid: item.to_dict()
                for mid, item in self._working_memory.items()
            }
            with open(os.path.join(base_path, "working.json"), "w") as f:
                json.dump(working_data, f)

            # Save episodic memory
            episodic_data = {
                tid: record.to_dict()
                for tid, record in self._episodic_memory.items()
            }
            with open(os.path.join(base_path, "episodic.json"), "w") as f:
                json.dump(episodic_data, f)

            # Save semantic memory
            semantic_data = {
                pid: {
                    "pattern_id": p.pattern_id,
                    "description": p.description,
                    "conditions": p.conditions,
                    "actions": p.actions,
                    "success_rate": p.success_rate,
                    "usage_count": p.usage_count,
                    "confidence": p.confidence,
                }
                for pid, p in self._semantic_memory.items()
            }
            with open(os.path.join(base_path, "semantic.json"), "w") as f:
                json.dump(semantic_data, f)

            self._stats["saves"] += 1
            self._dirty = False

        except Exception as e:
            self.logger.error(f"[MemoryManager] Save failed: {e}")

    async def _load_from_disk(self) -> None:
        """Load memory from disk."""
        if not self.config.persistence_enabled:
            return

        try:
            base_path = self.config.persistence_path
            self._stats["loads"] += 1

            # Load episodic memory (most useful to restore)
            episodic_path = os.path.join(base_path, "episodic.json")
            if os.path.exists(episodic_path):
                with open(episodic_path, "r") as f:
                    data = json.load(f)
                    # Rebuild index
                    for tid, record_data in data.items():
                        goal_hash = self._hash_goal(record_data.get("goal", ""))
                        if goal_hash not in self._goal_index:
                            self._goal_index[goal_hash] = []
                        self._goal_index[goal_hash].append(tid)

        except Exception as e:
            self.logger.debug(f"[MemoryManager] Load failed (may be first run): {e}")

    async def _auto_save_loop(self) -> None:
        """Periodically save memory to disk."""
        while True:
            try:
                await asyncio.sleep(self.config.auto_save_interval)

                if self._dirty:
                    await self._save_to_disk()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"[MemoryManager] Auto-save error: {e}")

    # =========================================================================
    # Utilities
    # =========================================================================

    def _hash_goal(self, goal: str) -> str:
        """Create a hash for goal similarity matching."""
        # Normalize and hash
        normalized = " ".join(goal.lower().split())
        return hashlib.md5(normalized.encode()).hexdigest()[:16]

    def _index_tags(self, memory_id: str, tags: List[str]) -> None:
        """Index memory by tags."""
        for tag in tags:
            tag_lower = tag.lower()
            if tag_lower not in self._tag_index:
                self._tag_index[tag_lower] = set()
            self._tag_index[tag_lower].add(memory_id)

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get memory statistics."""
        return {
            **self._stats,
            "working_count": len(self._working_memory),
            "episodic_count": len(self._episodic_memory),
            "semantic_count": len(self._semantic_memory),
            "indexed_tags": len(self._tag_index),
            "indexed_goals": len(self._goal_index),
        }

    @property
    def is_ready(self) -> bool:
        """Check if manager is ready."""
        return self._initialized


# =============================================================================
# Module-level Singleton Access
# =============================================================================

_memory_manager_instance: Optional[UnifiedMemoryManager] = None


def get_memory_manager() -> Optional[UnifiedMemoryManager]:
    """Get the global memory manager instance."""
    return _memory_manager_instance


def set_memory_manager(manager: UnifiedMemoryManager) -> None:
    """Set the global memory manager instance."""
    global _memory_manager_instance
    _memory_manager_instance = manager


async def start_memory_manager(
    config: Optional[MemoryManagerConfig] = None,
) -> UnifiedMemoryManager:
    """Start and initialize a new memory manager."""
    global _memory_manager_instance

    if _memory_manager_instance is not None:
        return _memory_manager_instance

    manager = UnifiedMemoryManager(config=config)
    await manager.initialize()
    _memory_manager_instance = manager

    return manager


async def stop_memory_manager() -> None:
    """Stop the global memory manager."""
    global _memory_manager_instance

    if _memory_manager_instance is not None:
        await _memory_manager_instance.shutdown()
        _memory_manager_instance = None
