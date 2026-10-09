# [Ouroboros] Modified by Ouroboros (op=op-01a12236-) at 2026-10-09 19:52 UTC
# Reason: `backend/api/experience_api.py` has no corresponding test module. CREATE `tests/test_experience_api.py` containing focus

from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.api.experience_api import (
    OutcomeRequest,
    OutcomeResponse,
    MetricsResponse,
    FileInfo,
    FilesResponse,
    HealthResponse,
    record_outcome,
    get_metrics,
    list_files,
    health_check,
    start_recorder,
    stop_recorder,
    create_test_record
)


async def test_import_smoke() -> None:
    """Test that the module imports correctly and exposes expected functions."""
    # This should not raise any ImportError
    assert hasattr(OutcomeRequest, '__init__')
    assert hasattr(OutcomeResponse, '__init__')
    assert hasattr(MetricsResponse, '__init__')
    assert hasattr(FileInfo, '__init__')
    assert hasattr(FilesResponse, '__init__')
    assert hasattr(HealthResponse, '__init__')
    assert callable(record_outcome)
    assert callable(get_metrics)
    assert callable(list_files)
    assert callable(health_check)
    assert callable(start_recorder)
    assert callable(stop_recorder)
    assert callable(create_test_record)




async def test_record_outcome_failure() -> None:
    """Test failed outcome recording."""
    mock_recorder = AsyncMock()
    mock_recorder.update_outcome.return_value = False
    
    with patch('backend.api.experience_api.get_experience_recorder', return_value=mock_recorder):
        request = OutcomeRequest(
            record_id="test-id",
            signal="negative",
            feedback_text="That was wrong",
            correction=None,
            context={"source": "user"}
        )
        
        result = await record_outcome(request)
        
        assert result.success is False
        assert result.record_id == "test-id"
        assert result.signal == "negative"
        assert result.message == "Failed to record outcome (recorder may be disabled)"


async def test_record_outcome_invalid_signal() -> None:
    """Test recording outcome with invalid signal."""
    with patch('backend.api.experience_api.get_experience_recorder'):
        request = OutcomeRequest(
            record_id="test-id",
            signal="invalid_signal",
            feedback_text=None,
            correction=None,
            context=None
        )
        
        with pytest.raises(Exception) as exc_info:
            await record_outcome(request)
        
        assert "Invalid signal" in str(exc_info.value)












async def test_start_recorder_already_running() -> None:
    """Test starting recorder when already running."""
    mock_recorder = AsyncMock()
    mock_recorder.is_running = True
    
    with patch('backend.api.experience_api.get_experience_recorder', return_value=mock_recorder):
        result = await start_recorder()
        
        assert result["success"] is True
        assert result["message"] == "Recorder already running"
        assert result["was_running"] is True


async def test_stop_recorder_already_stopped() -> None:
    """Test stopping recorder when already stopped."""
    mock_recorder = AsyncMock()
    mock_recorder.is_running = False
    
    with patch('backend.api.experience_api.get_experience_recorder', return_value=mock_recorder):
        result = await stop_recorder()
        
        assert result["success"] is True
        assert result["message"] == "Recorder already stopped"
        assert result["was_running"] is False


