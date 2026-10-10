# [Ouroboros] Modified by Ouroboros (op=op-01a1232e-) at 2026-10-10 00:21 UTC
# Reason: `backend/voice_unlock/metrics_monitor.py` has no corresponding test module. CREATE `tests/test_metrics_monitor.py` conta

from __future__ import annotations
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# Import the module under test
from backend.voice_unlock.metrics_monitor import (
    VoiceUnlockMetricsMonitor,
    initialize_metrics_monitor,
    shutdown_metrics_monitor,
    get_metrics_monitor,
)


class TestImportSmoke:
    """Test that the module can be imported without errors."""

    def test_import_succeeds(self):
        # This test ensures no import-time errors
        assert VoiceUnlockMetricsMonitor is not None
        assert initialize_metrics_monitor is not None
        assert shutdown_metrics_monitor is not None
        assert get_metrics_monitor is not None


class TestPublicFunctions:
    """Test the public functions and their behavior."""

    @pytest.mark.asyncio
    async def test_initialize_metrics_monitor_returns_monitor(self):
        # Mock the actual start method to avoid real process spawning
        with patch('backend.voice_unlock.metrics_monitor.VoiceUnlockMetricsMonitor.start', new_callable=AsyncMock):
            monitor = await initialize_metrics_monitor()
            assert isinstance(monitor, VoiceUnlockMetricsMonitor)



    @pytest.mark.asyncio
    async def test_get_metrics_monitor_returns_instance_when_initialized(self):
        # Mock the actual start method to avoid real process spawning
        with patch('backend.voice_unlock.metrics_monitor.VoiceUnlockMetricsMonitor.start', new_callable=AsyncMock):
            monitor = await initialize_metrics_monitor()
            result = get_metrics_monitor()
            assert result is monitor


class TestEdgeCases:
    """Test edge cases in the metrics monitor implementation."""

    def test_monitor_initialization_creates_directories(self):
        # Mock the filesystem operations to avoid actual file I/O
        with patch('pathlib.Path.mkdir') as mock_mkdir:
            with patch('pathlib.Path.exists', return_value=False):
                monitor = VoiceUnlockMetricsMonitor()
                # Verify mkdir was called for log directory
                mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)


    @pytest.mark.asyncio
    async def test_monitor_cleanup_orphaned_processes_handles_existing_process(self):
        # Test with an existing DB Browser process
        monitor = VoiceUnlockMetricsMonitor()
        with patch('pathlib.Path.exists', return_value=True):
            with patch('builtins.open', new_callable=MagicMock) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = '12345'
                with patch('psutil.pid_exists', return_value=True):
                    with patch('psutil.Process') as mock_process:
                        mock_process.return_value.name.return_value = 'DB Browser for SQLite'
                        await monitor._cleanup_orphaned_processes()
                        # Verify that the process was detected and marked as already running
                        assert monitor.db_browser_already_running is True

    @pytest.mark.asyncio
    async def test_monitor_validate_system_requirements_handles_disk_space(self):
        # Test low disk space scenario
        monitor = VoiceUnlockMetricsMonitor()
        with patch('shutil.disk_usage') as mock_disk_usage:
            mock_disk_usage.return_value.free = 50 * 1024 * 1024  # 50MB (less than 100MB threshold)
            await monitor._validate_system_requirements()
            assert monitor.degraded_mode is True
            assert monitor.degradation_reason == "Low disk space"

    @pytest.mark.asyncio
    async def test_monitor_validate_system_requirements_handles_writable_directory(self):
        # Test non-writable directory scenario
        monitor = VoiceUnlockMetricsMonitor()
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pathlib.Path.touch', side_effect=PermissionError("No permission")):
                await monitor._validate_system_requirements()
                assert monitor.degraded_mode is True
                assert monitor.degradation_reason == "Directory not writable"


    def test_monitor_is_db_browser_running_handles_exceptions(self):
        # Test exception handling in _is_db_browser_running
        monitor = VoiceUnlockMetricsMonitor()
        with patch('psutil.process_iter', side_effect=Exception("Process iteration error")):
            result = monitor._is_db_browser_running()
            assert result is None

