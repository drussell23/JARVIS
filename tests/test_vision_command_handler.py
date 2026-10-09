# [Ouroboros] Modified by Ouroboros (op=op-01a121f9-) at 2026-10-09 18:45 UTC
# Reason: `backend/api/vision_command_handler.py` has no corresponding test module. CREATE `tests/test_vision_command_handler.py` 

import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from backend.api.vision_command_handler import (
    VisionCommandHandler,
    VisionDescribeResult,
    WebSocketLogger,
    get_vision_command_handler
)
from datetime import datetime


class TestVisionCommandHandler:
    def test_import_smoke(self):
        """Test that the module can be imported without errors"""
        assert get_vision_command_handler() is not None

    def test_init(self):
        """Test VisionCommandHandler initialization"""
        handler = VisionCommandHandler()
        assert isinstance(handler, VisionCommandHandler)
        assert hasattr(handler, 'intelligence')
        assert hasattr(handler, 'monitoring_active')

    def test_vision_describe_result(self):
        """Test VisionDescribeResult class functionality"""
        result = VisionDescribeResult(
            success=True,
            description="test description",
            data={"key": "value"},
            error=None,
            confidence=0.9
        )
        
        assert result.success is True
        assert result.description == "test description"
        assert result.data == {"key": "value"}
        assert result.error is None
        assert result.confidence == 0.9




    @pytest.mark.asyncio
    async def test_handle_command_timeout(self):
        """Test handle_command with timeout"""
        handler = VisionCommandHandler()
        
        # Mock the internal command handler to raise timeout
        with patch.object(handler, '_handle_command_internal', side_effect=asyncio.TimeoutError()):
            result = await handler.handle_command("test command", timeout=1.0)
            
            assert result["handled"] is True
            assert "timeout" in result
            assert result["error"] is True
            assert result["pure_intelligence"] is True

    @pytest.mark.asyncio
    async def test_handle_command_exception(self):
        """Test handle_command with general exception"""
        handler = VisionCommandHandler()
        
        # Mock the internal command handler to raise an exception
        with patch.object(handler, '_handle_command_internal', side_effect=Exception("General error")):
            result = await handler.handle_command("test command")
            
            assert result["handled"] is True
            assert "exception" in result
            assert result["error"] is True
            assert result["pure_intelligence"] is True



    @pytest.mark.asyncio
    async def test_describe_screen_exception(self):
        """Test describe_screen with exception"""
        handler = VisionCommandHandler()
        
        # Mock intelligence to raise an exception
        with patch.object(handler, 'intelligence') as mock_intelligence:
            mock_intelligence.analyze_screen = AsyncMock(side_effect=Exception("Analysis failed"))
            
            result = await handler.describe_screen()
            
            assert isinstance(result, VisionDescribeResult)
            assert result.success is False
            assert "error" in result.description


    @pytest.mark.asyncio
    async def test_get_vision_command_handler_singleton(self):
        """Test that get_vision_command_handler returns the same instance"""
        handler1 = get_vision_command_handler()
        handler2 = get_vision_command_handler()
        
        assert handler1 is handler2

    @pytest.mark.asyncio
    async def test_websocket_logger(self):
        """Test WebSocketLogger functionality"""
        logger = WebSocketLogger()
        
        # Test setting callback
        mock_callback = AsyncMock()
        logger.set_websocket_callback(mock_callback)
        
        # Test logging
        await logger.log("test message", "info")
        
        assert mock_callback.called

    @pytest.mark.asyncio
    async def test_websocket_logger_error(self):
        """Test WebSocketLogger error handling"""
        logger = WebSocketLogger()
        
        # Test with broken callback
        mock_callback = AsyncMock(side_effect=Exception("WebSocket error"))
        logger.set_websocket_callback(mock_callback)
        
        # Should not raise exception
        await logger.log("test message", "error")
        
        # Should still log to console
        assert mock_callback.called