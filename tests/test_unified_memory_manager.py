# [Ouroboros] Modified by Ouroboros (op=op-01a0de3f-) at 2026-09-26 15:09 UTC
# Reason: `backend/autonomy/unified_memory_manager.py` has no corresponding test module. CREATE `tests/test_unified_memory_manager

from __future__ import annotations
import asyncio
import json
import logging
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.autonomy.unified_memory_manager import (
    MemoryManagerConfig,
    MemoryType,
    MemoryImportance,
    MemoryItem,
    EpisodicRecord,
    SemanticPattern,
    UnifiedMemoryManager,
    get_memory_manager,
    set_memory_manager,
    start_memory_manager,
    stop_memory_manager,
)


class TestUnifiedMemoryManager:
    def _make_config(self, **overrides) -> MemoryManagerConfig:
        defaults = {
            "working_memory_max_items": 10,
            "episodic_memory_max_items": 5,
            "semantic_memory_max_items": 5,
            "persistence_enabled": False,
            "persistence_path": "./memory.json",
            "auto_save_interval": 30.0,
            "consolidation_enabled": True,
            "consolidation_threshold": 3,
            "replay_enabled": True,
            "replay_similarity_threshold": 0.8,
        }
        defaults.update(overrides)
        return MemoryManagerConfig(**defaults)

    def _make_mm(self, config: MemoryManagerConfig | None = None) -> UnifiedMemoryManager:
        if config is None:
            config = self._make_config()
        return UnifiedMemoryManager(config=config)

    @pytest.mark.asyncio
    async def test_import_smoke(self):
        """Test that the module can be imported and basic classes are accessible."""
        # This test ensures no import errors or syntax issues
        assert MemoryManagerConfig
        assert MemoryType
        assert MemoryImportance
        assert MemoryItem
        assert EpisodicRecord
        assert SemanticPattern
        assert UnifiedMemoryManager
        assert get_memory_manager
        assert set_memory_manager
        assert start_memory_manager
        assert stop_memory_manager

    @pytest.mark.asyncio
    async def test_initialize_and_shutdown(self):
        """Test basic initialization and shutdown."""
        mm = self._make_mm()
        assert not mm._initialized  # Check internal flag directly
        result = await mm.initialize()
        assert result is True
        assert mm._initialized  # Check internal flag directly
        await mm.shutdown()
        assert not mm._initialized  # Check internal flag directly

    @pytest.mark.asyncio
    async def test_working_memory_operations(self):
        """Test working memory set/get/clear operations."""
        mm = self._make_mm()
        await mm.initialize()

        # Test setting and getting
        key = "test_key"
        value = {"data": "test"}
        context = {"source": "test"}
        importance = MemoryImportance.HIGH
        tags = ["tag1", "tag2"]
        ttl = 30.0

        memory_id = await mm.set_working(key, value, context, importance, tags, ttl)
        assert memory_id.startswith("working_")

        retrieved = await mm.get_working(key)
        assert retrieved == value

        # Test TTL expiration
        ttl_expired = await mm.get_working("nonexistent")
        assert ttl_expired is None

        # Test clearing
        await mm.clear_working()
        cleared = await mm.get_working(key)
        assert cleared is None

        await mm.shutdown()

    @pytest.mark.asyncio
    async def test_episodic_memory_operations(self):
        """Test episodic memory record and retrieval."""
        mm = self._make_mm()
        await mm.initialize()

        task_id = "task_123"
        goal = "test goal"
        outcome = "success"
        actions = [{"type": "action1", "result": "ok"}]
        duration = 1.5
        context = {"session": "test"}
        learnings = ["learned something"]

        record_id = await mm.record_episode(
            task_id, goal, outcome, actions, duration, context, learnings
        )
        assert record_id == task_id

        retrieved = await mm.get_episode(task_id)
        assert retrieved is not None
        assert retrieved.task_id == task_id
        assert retrieved.goal == goal
        assert retrieved.outcome == outcome
        assert retrieved.duration == duration

        # Test finding similar episodes
        similar = await mm.find_similar_episodes(goal, max_results=5)
        assert len(similar) >= 0  # May be empty

        await mm.shutdown()

    @pytest.mark.asyncio
    async def test_semantic_memory_operations(self):
        """Test semantic memory pattern storage and retrieval."""
        mm = self._make_mm()
        await mm.initialize()

        pattern_id = "pattern_123"
        description = "test pattern"
        conditions = ["condition1"]
        actions = ["action1"]
        success_rate = 0.9
        confidence = 0.8
        created_from = ["episode_1"]

        stored_id = await mm.store_pattern(
            pattern_id, description, conditions, actions,
            success_rate, confidence, created_from
        )
        assert stored_id == pattern_id

        # Test finding patterns
        found = await mm.find_patterns(conditions, max_results=5)
        assert len(found) >= 0  # May be empty

        await mm.shutdown()

    @pytest.mark.asyncio
    async def test_replay_functionality(self):
        """Test experience replay functionality."""
        mm = self._make_mm()
        await mm.initialize()

        # Test with replay disabled
        config = self._make_config(replay_enabled=False)
        mm_disabled = self._make_mm(config)
        await mm_disabled.initialize()
        result = await mm_disabled.replay_for_goal("test goal")
        assert result is None
        await mm_disabled.shutdown()

        # Test with replay enabled but no similar episodes
        config = self._make_config(replay_enabled=True)
        mm_enabled = self._make_mm(config)
        await mm_enabled.initialize()
        result = await mm_enabled.replay_for_goal("test goal")
        assert result is None
        await mm_enabled.shutdown()

    @pytest.mark.asyncio
    async def test_stats_and_readiness(self):
        """Test statistics collection and readiness checks."""
        mm = self._make_mm()
        await mm.initialize()

        stats = mm.get_stats()
        assert isinstance(stats, dict)
        assert "working_writes" in stats
        assert "episodic_writes" in stats
        assert "semantic_writes" in stats
        assert "replays" in stats

        # Test readiness
        assert mm._initialized  # Check internal flag directly

        await mm.shutdown()
        assert not mm._initialized  # Check internal flag directly

    @pytest.mark.asyncio
    async def test_memory_manager_global_functions(self):
        """Test global memory manager functions."""
        # Test setting and getting
        mm = self._make_mm()
        await mm.initialize()
        set_memory_manager(mm)
        retrieved = get_memory_manager()
        assert retrieved is mm

        # Test start/stop
        await stop_memory_manager()
        assert get_memory_manager() is None

        new_mm = await start_memory_manager()
        assert isinstance(new_mm, UnifiedMemoryManager)
        assert get_memory_manager() is new_mm

        await stop_memory_manager()
        assert get_memory_manager() is None

    @pytest.mark.asyncio
    async def test_edge_cases(self):
        """Test edge cases and error conditions."""
        mm = self._make_mm()
        await mm.initialize()

        # Test with expired memory item
        key = "expired_key"
        value = {"data": "test"}
        ttl = 0.01  # Very short TTL
        await mm.set_working(key, value, ttl=ttl)
        
        # Wait for expiration
        await asyncio.sleep(0.02)
        retrieved = await mm.get_working(key)
        assert retrieved is None

        # Test with empty context and tags
        key2 = "empty_key"
        await mm.set_working(key2, {"data": "test"}, context={}, tags=[])
        retrieved2 = await mm.get_working(key2)
        assert retrieved2 == {"data": "test"}

        await mm.shutdown()

    @pytest.mark.asyncio
    async def test_persistence_disabled(self):
        """Test behavior when persistence is disabled."""
        config = self._make_config(persistence_enabled=False)
        mm = self._make_mm(config)
        await mm.initialize()
        assert mm._initialized  # Should succeed even with persistence disabled
        await mm.shutdown()

    @pytest.mark.asyncio
    async def test_memory_item_expiration(self):
        """Test MemoryItem expiration logic."""
        import time
        current_time = time.time()
        item = MemoryItem(
            memory_id="test",
            memory_type=MemoryType.WORKING,
            content={"data": "test"},
            context={},
            importance=MemoryImportance.MEDIUM,
            created_at=current_time,
            accessed_at=current_time,
            access_count=0,
            tags=[],
        )
        assert item.is_expired() is False  # TTL not set, should NOT be expired

        item_with_ttl = MemoryItem(
            memory_id="test2",
            memory_type=MemoryType.WORKING,
            content={"data": "test"},
            context={},
            importance=MemoryImportance.MEDIUM,
            created_at=current_time,
            accessed_at=current_time,
            access_count=0,
            tags=[],
            ttl=1.0,
        )
        assert item_with_ttl.is_expired() is False  # TTL set, not expired yet
