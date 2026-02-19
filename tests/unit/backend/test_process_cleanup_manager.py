"""Tests for ProcessCleanupManager cache-clearing safety."""

import sys
import types

import pytest

from backend.process_cleanup_manager import ProcessCleanupManager


def _make_manager(tmp_path):
    """Create a lightweight ProcessCleanupManager instance for method tests."""
    manager = ProcessCleanupManager.__new__(ProcessCleanupManager)
    manager.backend_path = tmp_path
    return manager


def test_clear_python_cache_does_not_evict_runtime_modules_by_default(tmp_path):
    """Default cache clear should only touch bytecode cache, not sys.modules."""
    manager = _make_manager(tmp_path)
    module_name = "backend.voice._codex_runtime_module_default"
    sys.modules[module_name] = types.ModuleType(module_name)

    try:
        manager._clear_python_cache()
        assert module_name in sys.modules
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.asyncio
async def test_clear_python_cache_skips_runtime_evict_with_active_event_loop(tmp_path):
    """
    Runtime module eviction is blocked in active asyncio contexts to prevent
    singleton-split startup races.
    """
    manager = _make_manager(tmp_path)
    module_name = "backend.core._codex_runtime_module_async"
    sys.modules[module_name] = types.ModuleType(module_name)

    try:
        manager._clear_python_cache(clear_runtime_modules=True)
        assert module_name in sys.modules
    finally:
        sys.modules.pop(module_name, None)
