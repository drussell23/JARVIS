# [Ouroboros] Modified by Ouroboros (op=op-01a121d0-) at 2026-10-09 17:59 UTC
# Reason: `backend/api/vision_ws_endpoint.py` has no corresponding test module. CREATE `tests/test_vision_ws_endpoint.py` containi

from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.api.vision_ws_endpoint import (
    VisionWebSocketManager,
    vision_websocket_endpoint,
    set_vision_analyzer
)

# Mock WebSocket class
class _FakeWebSocket:
    def __init__(self):
        self.accepted = False
        self.messages = []
        self.disconnected = False
        self.client = MagicMock()
        self.client.host = "127.0.0.1"
        
    async def accept(self):
        self.accepted = True
        
    async def send_json(self, message):
        self.messages.append(message)
        
    async def receive_json(self):
        # Simulate receiving a message
        await asyncio.sleep(0.01)
        return {'type': 'ping'}
        
    async def close(self):
        self.disconnected = True

# Mock vision analyzer
MOCK_ANALYZER = MagicMock()
MOCK_ANALYZER.capture_screen = AsyncMock(return_value=b"mock_image_data")
MOCK_ANALYZER.analyze_screen = AsyncMock(return_value="mock_analysis_result")
MOCK_ANALYZER.start_video_streaming = AsyncMock()
MOCK_ANALYZER.stop_video_streaming = AsyncMock()

async def test_import_smoke() -> None:
    """Test that the module imports correctly and exposes expected functions."""
    # This should not raise any ImportError
    assert callable(VisionWebSocketManager)
    assert callable(vision_websocket_endpoint)
    assert callable(set_vision_analyzer)

async def test_vision_websocket_manager_init() -> None:
    """Test VisionWebSocketManager initialization."""
    manager = VisionWebSocketManager()
    assert manager.active_connections == {}
    assert manager.vision_analyzer is None

async def test_vision_websocket_manager_connect() -> None:
    """Test WebSocket connection handling."""
    manager = VisionWebSocketManager()
    fake_ws = _FakeWebSocket()
    
    await manager.connect(fake_ws, "test_client")
    
    assert fake_ws.accepted is True
    assert "test_client" in manager.active_connections
    assert manager.active_connections["test_client"] is fake_ws
    
    # Check welcome message was sent
    assert len(fake_ws.messages) == 1
    assert fake_ws.messages[0]["type"] == "connection_established"

async def test_vision_websocket_manager_disconnect() -> None:
    """Test WebSocket disconnection handling."""
    manager = VisionWebSocketManager()
    fake_ws = _FakeWebSocket()
    
    await manager.connect(fake_ws, "test_client")
    assert "test_client" in manager.active_connections
    
    manager.disconnect("test_client")
    assert "test_client" not in manager.active_connections

async def test_vision_websocket_manager_handle_message_ping() -> None:
    """Test handling of ping message."""
    manager = VisionWebSocketManager()
    fake_ws = _FakeWebSocket()
    
    result = await manager.handle_message(fake_ws, {"type": "ping"})
    
    assert result["type"] == "pong"
    assert "timestamp" in result

async def test_vision_websocket_manager_handle_message_capture_screen_no_analyzer() -> None:
    """Test handling of capture_screen message when no analyzer is set."""
    manager = VisionWebSocketManager()
    fake_ws = _FakeWebSocket()
    
    result = await manager.handle_message(fake_ws, {"type": "capture_screen"})
    
    assert result["type"] == "error"
    assert result["error"] == "Vision analyzer not available"

async def test_vision_websocket_manager_handle_message_capture_screen_with_analyzer() -> None:
    """Test handling of capture_screen message when analyzer is set."""
    manager = VisionWebSocketManager()
    fake_ws = _FakeWebSocket()
    
    # Set the analyzer
    manager.set_vision_analyzer(MOCK_ANALYZER)
    
    result = await manager.handle_message(fake_ws, {"type": "capture_screen"})
    
    assert result["type"] == "screen_captured"
    assert result["success"] is True
    assert result["has_image"] is True

async def test_vision_websocket_manager_handle_message_analyze_screen_no_analyzer() -> None:
    """Test handling of analyze_screen message when no analyzer is set."""
    manager = VisionWebSocketManager()
    fake_ws = _FakeWebSocket()
    
    result = await manager.handle_message(fake_ws, {"type": "analyze_screen"})
    
    assert result["type"] == "error"
    assert result["error"] == "Vision analyzer not available"

async def test_vision_websocket_manager_handle_message_analyze_screen_with_analyzer() -> None:
    """Test handling of analyze_screen message when analyzer is set."""
    manager = VisionWebSocketManager()
    fake_ws = _FakeWebSocket()
    
    # Set the analyzer
    manager.set_vision_analyzer(MOCK_ANALYZER)
    
    result = await manager.handle_message(fake_ws, {"type": "analyze_screen", "prompt": "test prompt"})
    
    assert result["type"] == "analysis_result"
    assert result["result"] == "mock_analysis_result"

async def test_vision_websocket_manager_handle_message_start_monitoring_no_analyzer() -> None:
    """Test handling of start_monitoring message when no analyzer is set."""
    manager = VisionWebSocketManager()
    fake_ws = _FakeWebSocket()
    
    result = await manager.handle_message(fake_ws, {"type": "start_monitoring"})
    
    assert result["type"] == "error"
    assert result["error"] == "Vision analyzer not available"

async def test_vision_websocket_manager_handle_message_start_monitoring_with_analyzer() -> None:
    """Test handling of start_monitoring message when analyzer is set."""
    manager = VisionWebSocketManager()
    fake_ws = _FakeWebSocket()
    
    # Set the analyzer
    manager.set_vision_analyzer(MOCK_ANALYZER)
    
    result = await manager.handle_message(fake_ws, {"type": "start_monitoring"})
    
    assert result["type"] == "monitoring_started"

async def test_vision_websocket_manager_handle_message_stop_monitoring_no_analyzer() -> None:
    """Test handling of stop_monitoring message when no analyzer is set."""
    manager = VisionWebSocketManager()
    fake_ws = _FakeWebSocket()
    
    result = await manager.handle_message(fake_ws, {"type": "stop_monitoring"})
    
    assert result["type"] == "error"
    assert result["error"] == "Vision analyzer not available"

async def test_vision_websocket_manager_handle_message_stop_monitoring_with_analyzer() -> None:
    """Test handling of stop_monitoring message when analyzer is set."""
    manager = VisionWebSocketManager()
    fake_ws = _FakeWebSocket()
    
    # Set the analyzer
    manager.set_vision_analyzer(MOCK_ANALYZER)
    
    result = await manager.handle_message(fake_ws, {"type": "stop_monitoring"})
    
    assert result["type"] == "monitoring_stopped"

async def test_vision_websocket_manager_handle_message_unknown_type() -> None:
    """Test handling of unknown message type."""
    manager = VisionWebSocketManager()
    fake_ws = _FakeWebSocket()
    
    result = await manager.handle_message(fake_ws, {"type": "unknown_type"})
    
    assert result["type"] == "error"
    assert "Unknown message type" in result["error"]
    assert "supported_types" in result


async def test_vision_websocket_manager_set_vision_analyzer() -> None:
    """Test setting the vision analyzer."""
    manager = VisionWebSocketManager()
    
    assert manager.vision_analyzer is None
    
    manager.set_vision_analyzer(MOCK_ANALYZER)
    
    assert manager.vision_analyzer is MOCK_ANALYZER

async def test_set_vision_analyzer_function() -> None:
    """Test the global set_vision_analyzer function."""
    # This should not raise any exception
    from backend.api.vision_ws_endpoint import vision_ws_manager
    
    assert vision_ws_manager.vision_analyzer is None
    
    set_vision_analyzer(MOCK_ANALYZER)
    
    assert vision_ws_manager.vision_analyzer is MOCK_ANALYZER
