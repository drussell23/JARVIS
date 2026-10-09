# [Ouroboros] Modified by Ouroboros (op=op-01a122c0-) at 2026-10-09 22:21 UTC
# Reason: `backend/utils/cache_registry.py` has no corresponding test module. CREATE `tests/test_cache_registry.py` containing foc

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from backend.utils.cache_registry import (
    CacheLike,
    ClearableCacheLike,
    CacheRegistry,
    get_cache_registry,
)


def test_import_smoke():
    """Test that the module can be imported without errors."""
    assert CacheLike is not None
    assert ClearableCacheLike is not None
    assert CacheRegistry is not None
    assert get_cache_registry is not None


def test_get_cache_registry_singleton():
    """Test that get_cache_registry returns the same instance."""
    registry1 = get_cache_registry()
    registry2 = get_cache_registry()
    assert registry1 is registry2


def test_cache_registry_initialization():
    """Test CacheRegistry initialization."""
    registry = CacheRegistry()
    assert registry._caches == {}
    assert registry._registered_at == {}
    assert hasattr(registry, '_lock')


def test_register_and_unregister():
    """Test registering and unregistering caches."""
    registry = CacheRegistry()
    mock_cache = MagicMock()
    mock_cache.get_stats.return_value = {'size_mb': 10.0}

    registry.register('test_cache', mock_cache)
    assert 'test_cache' in registry._caches
    assert 'test_cache' in registry._registered_at

    registry.unregister('test_cache')
    assert 'test_cache' not in registry._caches
    assert 'test_cache' not in registry._registered_at


def test_register_replaces_existing():
    """Test that registering a cache with same name replaces the old one."""
    registry = CacheRegistry()
    mock_cache1 = MagicMock()
    mock_cache1.get_stats.return_value = {'size_mb': 10.0}
    mock_cache2 = MagicMock()
    mock_cache2.get_stats.return_value = {'size_mb': 20.0}

    registry.register('test_cache', mock_cache1)
    registry.register('test_cache', mock_cache2)

    assert registry._caches['test_cache'] is mock_cache2


def test_get_all_stats():
    """Test get_all_stats returns correct stats."""
    registry = CacheRegistry()
    mock_cache = MagicMock()
    mock_cache.get_stats.return_value = {'size_mb': 10.0, 'entries': 5}

    registry.register('test_cache', mock_cache)
    stats = registry.get_all_stats()

    assert 'test_cache' in stats
    assert stats['test_cache']['size_mb'] == 10.0
    assert stats['test_cache']['entries'] == 5
    assert '_registered_at' in stats['test_cache']
    assert '_type' in stats['test_cache']


def test_get_all_stats_handles_exception():
    """Test get_all_stats handles exceptions in cache.get_stats()."""
    registry = CacheRegistry()
    mock_cache = MagicMock()
    mock_cache.get_stats.side_effect = Exception('Test error')

    registry.register('test_cache', mock_cache)
    stats = registry.get_all_stats()

    assert 'test_cache' in stats
    assert 'error' in stats['test_cache']


def test_get_summary():
    """Test get_summary returns correct summary."""
    registry = CacheRegistry()
    mock_cache1 = MagicMock()
    mock_cache1.get_stats.return_value = {'size_mb': 10.0, 'entries': 5, 'evictions': 2}
    mock_cache2 = MagicMock()
    mock_cache2.get_stats.return_value = {'size_mb': 15.0, 'entries': 8, 'evictions': 3}

    registry.register('cache1', mock_cache1)
    registry.register('cache2', mock_cache2)
    summary = registry.get_summary()

    assert summary['cache_count'] == 2
    assert summary['total_size_mb'] == 25.0
    assert summary['total_entries'] == 13
    assert summary['total_evictions'] == 5
    assert 'cache1' in summary['caches']
    assert 'cache2' in summary['caches']


def test_get_summary_handles_missing_fields():
    """Test get_summary handles missing fields in cache stats."""
    registry = CacheRegistry()
    mock_cache = MagicMock()
    mock_cache.get_stats.return_value = {'size': 100}

    registry.register('test_cache', mock_cache)
    summary = registry.get_summary()

    assert summary['total_size_mb'] == 0.0
    assert summary['total_entries'] == 100




async def test_evict_under_pressure_sync_clear():
    """Test evict_under_pressure with sync clear function."""
    registry = CacheRegistry()
    mock_cache = MagicMock()
    mock_cache.get_stats.side_effect = [{'size_mb': 10.0}, {'size_mb': 5.0}]
    mock_cache.clear = MagicMock()

    registry.register('test_cache', mock_cache)
    freed = await registry.evict_under_pressure(target_mb=5.0)

    assert freed >= 0.0
    mock_cache.clear.assert_called()


async def test_evict_under_pressure_handles_exception():
    """Test evict_under_pressure handles exceptions during clear."""
    registry = CacheRegistry()
    mock_cache = MagicMock()
    mock_cache.get_stats.return_value = {'size_mb': 10.0}
    mock_cache.clear.side_effect = Exception('Clear failed')

    registry.register('test_cache', mock_cache)
    freed = await registry.evict_under_pressure(target_mb=5.0)

    assert freed >= 0.0




def test_cache_count_property():
    """Test cache_count property."""
    registry = CacheRegistry()
    assert registry.cache_count == 0

    mock_cache = MagicMock()
    mock_cache.get_stats.return_value = {'size_mb': 10.0}
    registry.register('test_cache', mock_cache)
    assert registry.cache_count == 1
