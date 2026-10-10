# [Ouroboros] Modified by Ouroboros (op=op-01a12222-) at 2026-10-09 19:28 UTC
# Reason: `backend/api/context_api.py` has no corresponding test module. CREATE `tests/test_context_api.py` containing focused tes

from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.api.context_api import (
    ContextQuery,
    OCRUpdate,
    ContextEvent,
    query_context,
    get_context_summary,
    update_ocr_context,
    record_context_event,
    get_context_history,
    get_user_patterns,
    reset_context,
    get_active_app
)


def test_import_smoke() -> None:
    """Test that the module imports correctly and exposes expected functions."""
    # This should not raise any ImportError
    assert hasattr(ContextQuery, '__init__')
    assert hasattr(OCRUpdate, '__init__')
    assert hasattr(ContextEvent, '__init__')
    assert callable(query_context)
    assert callable(get_context_summary)
    assert callable(update_ocr_context)
    assert callable(record_context_event)
    assert callable(get_context_history)
    assert callable(get_user_patterns)
    assert callable(reset_context)
    assert callable(get_active_app)


async def test_query_context_working_on() -> None:
    """Test query_context with 'working on' query."""
    with patch('backend.api.context_api.logger') as mock_logger:
        request = ContextQuery(query="What am I working on?")
        result = await query_context(request)
        
        assert result['query'] == "What am I working on?"
        assert 'results' in result
        assert result['results']['type'] == 'current_activity'
        mock_logger.error.assert_not_called()


async def test_query_context_history() -> None:
    """Test query_context with history-related query."""
    with patch('backend.api.context_api.logger') as mock_logger:
        request = ContextQuery(query="What did I do earlier?")
        result = await query_context(request)
        
        assert result['query'] == "What did I do earlier?"
        assert 'results' in result
        assert result['results']['type'] == 'history'
        mock_logger.error.assert_not_called()


async def test_query_context_patterns() -> None:
    """Test query_context with pattern-related query."""
    with patch('backend.api.context_api.logger') as mock_logger:
        request = ContextQuery(query="What do I usually do?")
        result = await query_context(request)
        
        assert result['query'] == "What do I usually do?"
        assert 'results' in result
        assert result['results']['type'] == 'patterns'
        mock_logger.error.assert_not_called()


async def test_query_context_screen() -> None:
    """Test query_context with screen-related query."""
    with patch('backend.api.context_api.logger') as mock_logger:
        request = ContextQuery(query="What's on my screen?")
        result = await query_context(request)
        
        assert result['query'] == "What's on my screen?"
        assert 'results' in result
        assert result['results']['type'] == 'screen_content'
        mock_logger.error.assert_not_called()


async def test_query_context_general() -> None:
    """Test query_context with general query."""
    with patch('backend.api.context_api.logger') as mock_logger:
        request = ContextQuery(query="General question")
        result = await query_context(request)
        
        assert result['query'] == "General question"
        assert 'results' in result
        assert result['results']['type'] == 'general'
        mock_logger.error.assert_not_called()


async def test_get_context_summary() -> None:
    """Test get_context_summary function."""
    with patch('backend.api.context_api.logger') as mock_logger:
        result = await get_context_summary()
        
        assert 'summary' in result
        assert 'timestamp' in result
        mock_logger.error.assert_not_called()


async def test_update_ocr_context() -> None:
    """Test update_ocr_context function."""
    with patch('backend.api.context_api.logger') as mock_logger:
        request = OCRUpdate(ocr_text="Sample OCR text")
        result = await update_ocr_context(request)
        
        assert result['success'] is True
        assert 'message' in result
        assert 'history_size' in result
        mock_logger.error.assert_not_called()


async def test_record_context_event_command() -> None:
    """Test record_context_event with command event."""
    with patch('backend.api.context_api.logger') as mock_logger:
        event = ContextEvent(event_type="command", data={"command": "ls -la"})
        result = await record_context_event(event)
        
        assert result['success'] is True
        assert 'event_id' in result
        mock_logger.error.assert_not_called()


async def test_record_context_event_app_switch() -> None:
    """Test record_context_event with app_switch event."""
    with patch('backend.api.context_api.logger') as mock_logger:
        event = ContextEvent(event_type="app_switch", data={"app": "VS Code"})
        result = await record_context_event(event)
        
        assert result['success'] is True
        assert 'event_id' in result
        mock_logger.error.assert_not_called()


async def test_get_context_history() -> None:
    """Test get_context_history function."""
    with patch('backend.api.context_api.logger') as mock_logger:
        result = await get_context_history()
        
        assert 'events' in result
        assert 'total_count' in result
        mock_logger.error.assert_not_called()


async def test_get_user_patterns() -> None:
    """Test get_user_patterns function."""
    with patch('backend.api.context_api.logger') as mock_logger:
        result = await get_user_patterns()
        
        assert 'common_commands' in result
        assert 'app_usage' in result
        assert 'active_hours' in result
        mock_logger.error.assert_not_called()


async def test_reset_context() -> None:
    """Test reset_context function."""
    with patch('backend.api.context_api.logger') as mock_logger:
        result = await reset_context()
        
        assert result['success'] is True
        assert 'reset_history' in result
        assert 'reset_patterns' in result
        mock_logger.error.assert_not_called()


async def test_get_active_app() -> None:
    """Test get_active_app function."""
    with patch('backend.api.context_api.logger') as mock_logger:
        result = await get_active_app()
        
        assert 'active_app' in result
        assert 'last_update' in result
        assert 'confidence' in result
        mock_logger.error.assert_not_called()