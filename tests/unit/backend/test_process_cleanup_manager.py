"""Tests for ProcessCleanupManager cache-clearing safety."""

import sys
import time
import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.process_cleanup_manager import (
    CleanupEvent,
    CleanupEventType,
    ProcessCleanupManager,
)


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


def test_cpu_compound_relief_deferred_during_startup_grace(monkeypatch, tmp_path):
    manager = ProcessCleanupManager.__new__(ProcessCleanupManager)
    manager.health_monitor = SimpleNamespace(
        metrics=SimpleNamespace(current_cpu_usage_percent=0.0)
    )
    manager._schedule_cpu_relief = Mock()
    manager._schedule_memory_relief = Mock()
    manager._last_cpu_pressure_warn = 0.0
    manager._startup_compound_pressure_first_ts = 0.0

    monkeypatch.setenv("JARVIS_SUPERVISOR_LOADING", "1")
    monkeypatch.setenv("JARVIS_CPU_PRESSURE_LOG_COOLDOWN", "0")
    monkeypatch.setenv("JARVIS_CPU_STARTUP_COMPOUND_GRACE_SECONDS", "120")
    monkeypatch.setenv("JARVIS_SIGNAL_DIR", str(tmp_path))

    monkeypatch.setattr(
        "backend.process_cleanup_manager.psutil.virtual_memory",
        lambda: SimpleNamespace(percent=84.4),
    )

    event = CleanupEvent(
        event_type=CleanupEventType.CPU_PRESSURE,
        priority=6,
        source="test",
        data={"cpu_percent": 96.6},
    )
    manager._handle_cpu_pressure(event)

    manager._schedule_cpu_relief.assert_called_once()
    manager._schedule_memory_relief.assert_not_called()


def test_cpu_compound_relief_runs_after_startup_grace(monkeypatch, tmp_path):
    manager = ProcessCleanupManager.__new__(ProcessCleanupManager)
    manager.health_monitor = SimpleNamespace(
        metrics=SimpleNamespace(current_cpu_usage_percent=0.0)
    )
    manager._schedule_cpu_relief = Mock()
    manager._schedule_memory_relief = Mock()
    manager._last_cpu_pressure_warn = 0.0
    manager._startup_compound_pressure_first_ts = time.time() - 180.0

    monkeypatch.setenv("JARVIS_SUPERVISOR_LOADING", "1")
    monkeypatch.setenv("JARVIS_CPU_PRESSURE_LOG_COOLDOWN", "0")
    monkeypatch.setenv("JARVIS_CPU_STARTUP_COMPOUND_GRACE_SECONDS", "60")
    monkeypatch.setenv("JARVIS_SIGNAL_DIR", str(tmp_path))

    monkeypatch.setattr(
        "backend.process_cleanup_manager.psutil.virtual_memory",
        lambda: SimpleNamespace(percent=84.4),
    )

    event = CleanupEvent(
        event_type=CleanupEventType.CPU_PRESSURE,
        priority=6,
        source="test",
        data={"cpu_percent": 96.6},
    )
    manager._handle_cpu_pressure(event)

    manager._schedule_cpu_relief.assert_called_once()
    manager._schedule_memory_relief.assert_called_once()
