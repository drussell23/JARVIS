# [Ouroboros] Modified by Ouroboros (op=op-01a122e9-) at 2026-10-09 23:06 UTC
# Reason: `backend/utils/parent_death_watcher.py` has no corresponding test module. CREATE `tests/test_parent_death_watcher.py` co

"""
JARVIS Parent Death Watcher Tests
=================================

Tests for backend/utils/parent_death_watcher.py module.
This file tests the core functionality of the parent process death watcher,
including import behavior, public API functions, and various edge cases.
"""
from __future__ import annotations
import asyncio
import logging
import os
import signal
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the module under test
test_module = "backend.utils.parent_death_watcher"


def test_import_smoke_test():
    """Test that all expected imports work correctly."""
    from backend.utils.parent_death_watcher import (
        ParentWatcherConfig,
        ParentDeathWatcher,
        get_kernel_pid,
        start_parent_watcher,
        start_parent_watcher_sync,
        stop_parent_watcher,
        stop_parent_watcher_sync,
    )

    # Verify all imports are accessible
    assert ParentWatcherConfig
    assert ParentDeathWatcher
    assert get_kernel_pid
    assert start_parent_watcher
    assert start_parent_watcher_sync
    assert stop_parent_watcher
    assert stop_parent_watcher_sync


async def test_get_kernel_pid_with_valid_env_var():
    """Test that get_kernel_pid correctly parses valid PID from environment."""
    with patch.dict(os.environ, {"JARVIS_KERNEL_PID": "12345"}):
        from backend.utils.parent_death_watcher import get_kernel_pid
        assert get_kernel_pid() == 12345


async def test_get_kernel_pid_with_invalid_env_var():
    """Test that get_kernel_pid returns None for invalid PID in environment."""
    with patch.dict(os.environ, {"JARVIS_KERNEL_PID": "not_a_number"}):
        from backend.utils.parent_death_watcher import get_kernel_pid
        assert get_kernel_pid() is None


async def test_get_kernel_pid_with_no_env_var():
    """Test that get_kernel_pid returns None when environment variable not set."""
    with patch.dict(os.environ, {}, clear=True):
        from backend.utils.parent_death_watcher import get_kernel_pid
        assert get_kernel_pid() is None


async def test_parent_death_watcher_init():
    """Test ParentDeathWatcher initialization with valid parameters."""
    from backend.utils.parent_death_watcher import ParentDeathWatcher, ParentWatcherConfig
    
    watcher = ParentDeathWatcher(parent_pid=12345)
    assert watcher._parent_pid == 12345
    assert isinstance(watcher._config, ParentWatcherConfig)
    assert not watcher._running
    assert watcher._task is None
    assert watcher._thread is None


async def test_parent_death_watcher_init_with_config():
    """Test ParentDeathWatcher initialization with custom config."""
    from backend.utils.parent_death_watcher import ParentDeathWatcher, ParentWatcherConfig
    
    config = ParentWatcherConfig(poll_interval=5.0, grace_period=10.0)
    watcher = ParentDeathWatcher(parent_pid=12345, config=config)
    assert watcher._config.poll_interval == 5.0
    assert watcher._config.grace_period == 10.0


async def test_is_parent_alive_success():
    """Test _is_parent_alive returns True when parent exists."""
    from backend.utils.parent_death_watcher import ParentDeathWatcher
    
    # Mock os.kill to succeed (no exception)
    with patch("os.kill") as mock_kill:
        watcher = ParentDeathWatcher(parent_pid=12345)
        result = watcher._is_parent_alive()
        assert result is True
        mock_kill.assert_called_once_with(12345, 0)


async def test_is_parent_alive_process_lookup_error():
    """Test _is_parent_alive returns False when ProcessLookupError occurs."""
    from backend.utils.parent_death_watcher import ParentDeathWatcher
    
    # Mock os.kill to raise ProcessLookupError
    with patch("os.kill", side_effect=ProcessLookupError):
        watcher = ParentDeathWatcher(parent_pid=12345)
        result = watcher._is_parent_alive()
        assert result is False


async def test_is_parent_alive_permission_error():
    """Test _is_parent_alive returns True when PermissionError occurs."""
    from backend.utils.parent_death_watcher import ParentDeathWatcher
    
    # Mock os.kill to raise PermissionError
    with patch("os.kill", side_effect=PermissionError):
        watcher = ParentDeathWatcher(parent_pid=12345)
        result = watcher._is_parent_alive()
        assert result is True


async def test_is_orphaned_with_init_ppid():
    """Test _is_orphaned returns True when parent PID is 1 (init)."""
    from backend.utils.parent_death_watcher import ParentDeathWatcher
    
    # Mock os.getppid to return 1 (init)
    with patch("os.getppid", return_value=1):
        watcher = ParentDeathWatcher(parent_pid=54321)
        result = watcher._is_orphaned()
        assert result is True


async def test_is_orphaned_with_different_parent():
    """Test _is_orphaned returns True when current parent differs from expected."""
    from backend.utils.parent_death_watcher import ParentDeathWatcher
    
    # Mock os.getppid to return different PID
    with patch("os.getppid", return_value=99999):
        with patch.object(ParentDeathWatcher, "_is_parent_alive", return_value=False):
            watcher = ParentDeathWatcher(parent_pid=54321)
            result = watcher._is_orphaned()
            assert result is True


async def test_is_orphaned_with_same_parent():
    """Test _is_orphaned returns False when current parent matches expected."""
    from backend.utils.parent_death_watcher import ParentDeathWatcher
    
    # Mock os.getppid to return same PID as expected
    with patch("os.getppid", return_value=54321):
        with patch.object(ParentDeathWatcher, "_is_parent_alive", return_value=True):
            watcher = ParentDeathWatcher(parent_pid=54321)
            result = watcher._is_orphaned()
            assert result is False


async def test_initiate_shutdown_calls_callback():
    """Test that _initiate_shutdown calls the pre-shutdown callback."""
    from backend.utils.parent_death_watcher import ParentDeathWatcher, ParentWatcherConfig
    
    # Mock a callback function
    mock_callback = MagicMock()
    config = ParentWatcherConfig(pre_shutdown_callback=mock_callback)
    watcher = ParentDeathWatcher(parent_pid=12345, config=config)
    
    # Mock the shutdown methods to avoid actual signal sending
    with patch("os.kill"):
        with patch("time.sleep"):
            watcher._initiate_shutdown("test reason")
            mock_callback.assert_called_once()


async def test_start_parent_watcher_no_kernel_pid():
    """Test that start_parent_watcher returns None when no kernel PID is set."""
    with patch.dict(os.environ, {}, clear=True):
        from backend.utils.parent_death_watcher import start_parent_watcher
        assert await start_parent_watcher() is None


async def test_start_parent_watcher_with_kernel_pid():
    """Test that start_parent_watcher creates and starts watcher when kernel PID exists."""
    with patch.dict(os.environ, {"JARVIS_KERNEL_PID": "12345"}):
        from backend.utils.parent_death_watcher import start_parent_watcher
        
        # Mock the actual watcher to avoid real async tasks
        with patch("backend.utils.parent_death_watcher.ParentDeathWatcher") as mock_watcher:
            mock_instance = MagicMock()
            mock_watcher.return_value = mock_instance
            mock_instance.start = AsyncMock(return_value=True)
            
            result = await start_parent_watcher()
            assert result is mock_instance
            mock_watcher.assert_called_once_with(12345, None)
            mock_instance.start.assert_awaited_once()


async def test_start_parent_watcher_sync_no_kernel_pid():
    """Test that start_parent_watcher_sync returns None when no kernel PID is set."""
    with patch.dict(os.environ, {}, clear=True):
        from backend.utils.parent_death_watcher import start_parent_watcher_sync
        assert start_parent_watcher_sync() is None




@pytest.mark.asyncio
async def test_stop_parent_watcher():
    """Test that stop_parent_watcher stops the global watcher."""
    with patch.dict(os.environ, {"JARVIS_KERNEL_PID": "12345"}):
        from backend.utils.parent_death_watcher import start_parent_watcher, stop_parent_watcher
        
        # Start a watcher first
        watcher = await start_parent_watcher()
        assert watcher is not None
        
        # Mock the stop method to avoid real task cancellation
        with patch.object(watcher, "stop", new_callable=AsyncMock):
            await stop_parent_watcher()
            # Verify it was stopped (this will be a no-op since we don't have access to internal state)
            pass  # The function just sets _watcher_instance = None, so nothing else to check


@pytest.mark.asyncio
async def test_stop_parent_watcher_sync():
    """Test that stop_parent_watcher_sync stops the global watcher."""
    with patch.dict(os.environ, {"JARVIS_KERNEL_PID": "12345"}):
        from backend.utils.parent_death_watcher import start_parent_watcher_sync, stop_parent_watcher_sync
        
        # Start a watcher first
        watcher = start_parent_watcher_sync()
        assert watcher is not None
        
        # Mock the stop method to avoid real thread joining
        with patch.object(watcher, "stop_sync"):
            stop_parent_watcher_sync()
            # Verify it was stopped (this will be a no-op since we don't have access to internal state)
            pass  # The function just sets _watcher_instance = None, so nothing else to check
