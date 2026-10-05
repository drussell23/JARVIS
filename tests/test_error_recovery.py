# [Ouroboros] Modified by Ouroboros (op=op-01a10a08-) at 2026-10-05 03:23 UTC
# Reason: `backend/autonomy/error_recovery.py` has no corresponding test module. CREATE `tests/test_error_recovery.py` containing 

from __future__ import annotations
import asyncio
import time
from typing import Any, Dict, Optional
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Import the module under test
from backend.autonomy.error_recovery import (
    ErrorSeverity,
    ErrorCategory,
    RecoveryStrategy,
    ErrorRecord,
    RecoveryAction,
    ErrorRecoveryManager,
    test_error_recovery,
)

class TestErrorRecoveryImport:
    def test_import_smoke(self):
        """Smoke test that the module imports correctly."""
        assert ErrorSeverity is not None
        assert ErrorCategory is not None
        assert RecoveryStrategy is not None
        assert ErrorRecord is not None
        assert RecoveryAction is not None
        assert ErrorRecoveryManager is not None
        assert test_error_recovery is not None

class TestErrorRecoveryManager:
    @pytest.mark.asyncio
    async def test_register_component_reset(self):
        """Test component reset registration."""
        manager = ErrorRecoveryManager()
        mock_reset = AsyncMock()
        manager.register_component_reset("test_component", mock_reset)
        assert "test_component" in manager.component_resets
        assert manager.component_resets["test_component"] == mock_reset

    @pytest.mark.asyncio
    async def test_handle_error_creates_record(self):
        """Test that handle_error creates and stores error records."""
        manager = ErrorRecoveryManager()
        error = ValueError("test error")
        
        record = await manager.handle_error(
            error=error,
            component="test_component",
            category=ErrorCategory.VISION,
            severity=ErrorSeverity.HIGH,
            context={"key": "value"}
        )
        
        assert record.error_id is not None
        assert record.category == ErrorCategory.VISION
        assert record.severity == ErrorSeverity.HIGH
        assert record.message == "test error"
        assert record.component == "test_component"
        assert record.context == {"key": "value"}
        assert record.error_id in manager.active_errors
        assert len(manager.error_history) == 1

    @pytest.mark.asyncio
    async def test_handle_error_categorization(self):
        """Test automatic error categorization."""
        manager = ErrorRecoveryManager()
        
        # Test vision error
        vision_error = ValueError("screen capture failed")
        record = await manager.handle_error(vision_error, "test_component")
        assert record.category == ErrorCategory.VISION
        
        # Test network error
        network_error = TimeoutError("connection timeout")
        record = await manager.handle_error(network_error, "test_component")
        assert record.category == ErrorCategory.NETWORK
        
        # Test permission error
        perm_error = PermissionError("permission denied")
        record = await manager.handle_error(perm_error, "test_component")
        assert record.category == ErrorCategory.PERMISSION

    @pytest.mark.asyncio
    async def test_handle_error_severity_assessment(self):
        """Test automatic severity assessment."""
        manager = ErrorRecoveryManager()
        
        # Test critical error
        critical_error = MemoryError("out of memory")
        record = await manager.handle_error(critical_error, "test_component")
        assert record.severity == ErrorSeverity.CRITICAL
        
        # Test permission error (should be high)
        perm_error = PermissionError("permission denied")
        record = await manager.handle_error(perm_error, "test_component")
        assert record.severity == ErrorSeverity.HIGH
        
        # Test unknown category (should be high)
        unknown_error = RuntimeError("unknown error")
        record = await manager.handle_error(unknown_error, "test_component")
        assert record.severity == ErrorSeverity.HIGH

    @pytest.mark.asyncio
    async def test_add_error_callback(self):
        """Test adding error callbacks."""
        manager = ErrorRecoveryManager()
        mock_callback = AsyncMock()
        
        manager.add_error_callback(mock_callback)
        assert len(manager.error_callbacks) == 1
        assert manager.error_callbacks[0] == mock_callback

    @pytest.mark.asyncio
    async def test_add_recovery_callback(self):
        """Test adding recovery callbacks."""
        manager = ErrorRecoveryManager()
        mock_callback = AsyncMock()
        
        manager.add_recovery_callback(mock_callback)
        assert len(manager.recovery_callbacks) == 1
        assert manager.recovery_callbacks[0] == mock_callback

    @pytest.mark.asyncio
    async def test_get_error_statistics(self):
        """Test getting error statistics."""
        manager = ErrorRecoveryManager()
        
        # Add some errors
        await manager.handle_error(ValueError("test1"), "comp1", ErrorCategory.VISION, ErrorSeverity.HIGH)
        await manager.handle_error(ValueError("test2"), "comp2", ErrorCategory.OCR, ErrorSeverity.MEDIUM)
        
        stats = manager.get_error_statistics()
        
        assert stats["total_errors"] == 2
        assert stats["errors_by_category"][ErrorCategory.VISION.value] == 1
        assert stats["errors_by_category"][ErrorCategory.OCR.value] == 1
        assert stats["errors_by_severity"][ErrorSeverity.HIGH.name] == 1
        assert stats["errors_by_severity"][ErrorSeverity.MEDIUM.name] == 1
        assert "active_errors" in stats
        assert "recent_errors" in stats

    @pytest.mark.asyncio
    async def test_get_active_errors(self):
        """Test getting active errors."""
        manager = ErrorRecoveryManager()
        
        await manager.handle_error(ValueError("test1"), "comp1", ErrorCategory.VISION, ErrorSeverity.HIGH)
        await manager.handle_error(ValueError("test2"), "comp2", ErrorCategory.OCR, ErrorSeverity.MEDIUM)
        
        active = manager.get_active_errors()
        assert len(active) == 2

    @pytest.mark.asyncio
    async def test_clear_resolved_errors(self):
        """Test clearing resolved errors."""
        manager = ErrorRecoveryManager()
        
        # Add some errors
        await manager.handle_error(ValueError("test1"), "comp1", ErrorCategory.VISION, ErrorSeverity.HIGH)
        await manager.handle_error(ValueError("test2"), "comp2", ErrorCategory.OCR, ErrorSeverity.MEDIUM)
        
        # Clear old resolved errors (should clear nothing since they're all active)
        manager.clear_resolved_errors(older_than_hours=1)
        assert len(manager.error_history) == 2

    @pytest.mark.asyncio
    async def test_recovery_strategies(self):
        """Test that different recovery strategies are applied correctly."""
        manager = ErrorRecoveryManager()
        
        # Test retry strategy
        with patch.object(manager, '_retry_recovery') as mock_retry:
            error = ValueError("test")
            record = await manager.handle_error(error, "test_component", ErrorCategory.VISION, ErrorSeverity.MEDIUM)
            # The recovery should be initiated
            assert mock_retry.called
        
        # Test backoff strategy
        with patch.object(manager, '_backoff_recovery') as mock_backoff:
            error = TimeoutError("timeout")
            record = await manager.handle_error(error, "test_component", ErrorCategory.NETWORK, ErrorSeverity.HIGH)
            # The recovery should be initiated
            assert mock_backoff.called
        
        # Test reset strategy
        with patch.object(manager, '_reset_component') as mock_reset:
            manager.register_component_reset("test_component", AsyncMock())
            error = ValueError("vision error")
            record = await manager.handle_error(error, "test_component", ErrorCategory.VISION, ErrorSeverity.HIGH)
            # The recovery should be initiated
            assert mock_reset.called

    @pytest.mark.asyncio
    async def test_recovery_action_defaults(self):
        """Test that default recovery actions are applied for unknown error types."""
        manager = ErrorRecoveryManager()
        
        # Test with an unknown category and severity
        error = RuntimeError("unknown error")
        record = await manager.handle_error(error, "test_component")
        
        # Should default to SKIP strategy
        assert record.error_id in manager.recovery_actions
        assert manager.recovery_actions[record.error_id].strategy == RecoveryStrategy.SKIP

    @pytest.mark.asyncio
    async def test_component_reset_functionality(self):
        """Test that component reset works correctly."""
        manager = ErrorRecoveryManager()
        
        # Register a mock reset function
        reset_mock = AsyncMock()
        manager.register_component_reset("test_component", reset_mock)
        
        # Create an error that should trigger reset
        error = ValueError("vision error")
        record = await manager.handle_error(error, "test_component", ErrorCategory.VISION, ErrorSeverity.HIGH)
        
        # The reset function should be called
        assert reset_mock.called

    @pytest.mark.asyncio
    async def test_error_callback_notification(self):
        """Test that error callbacks are notified correctly."""
        manager = ErrorRecoveryManager()
        
        # Add a mock callback
        mock_callback = AsyncMock()
        manager.add_error_callback(mock_callback)
        
        # Handle an error
        error = ValueError("test error")
        await manager.handle_error(error, "test_component")
        
        # Callback should be called
        assert mock_callback.called

    @pytest.mark.asyncio
    async def test_recovery_callback_notification(self):
        """Test that recovery callbacks are notified correctly."""
        manager = ErrorRecoveryManager()
        
        # Add a mock callback
        mock_callback = AsyncMock()
        manager.add_recovery_callback(mock_callback)
        
        # Handle an error
        error = ValueError("test error")
        await manager.handle_error(error, "test_component")
        
        # Callback should be called
        assert mock_callback.called

    @pytest.mark.asyncio
    async def test_error_statistics_update(self):
        """Test that error statistics are properly updated."""
        manager = ErrorRecoveryManager()
        
        # Handle multiple errors
        await manager.handle_error(ValueError("test1"), "comp1", ErrorCategory.VISION, ErrorSeverity.HIGH)
        await manager.handle_error(ValueError("test2"), "comp2", ErrorCategory.OCR, ErrorSeverity.MEDIUM)
        await manager.handle_error(PermissionError("test3"), "comp3", ErrorCategory.PERMISSION, ErrorSeverity.HIGH)
        
        stats = manager.get_error_statistics()
        
        # Check that all statistics are updated
        assert stats["total_errors"] == 3
        assert stats["errors_by_category"][ErrorCategory.VISION.value] == 1
        assert stats["errors_by_category"][ErrorCategory.OCR.value] == 1
        assert stats["errors_by_category"][ErrorCategory.PERMISSION.value] == 1
        assert stats["errors_by_severity"][ErrorSeverity.HIGH.name] == 2
        assert stats["errors_by_severity"][ErrorSeverity.MEDIUM.name] == 1
