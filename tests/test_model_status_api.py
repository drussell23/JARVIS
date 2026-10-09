# [Ouroboros] Modified by Ouroboros (op=op-01a1215b-) at 2026-10-09 15:55 UTC
# Reason: `backend/api/model_status_api.py` has no corresponding test module, so a change to it cannot be validated. CREATE `tests

from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Import the module under test
from backend.api.model_status_api import (
    ModelStatusManager,
    get_model_status,
    model_status_websocket,
    get_loading_progress,
    broadcast_model_status,
    status_manager
)

# Mock the loader status function
MOCK_LOADER_STATUS = {
    "models_loaded": True,
    "whisper": "on-demand",
    "embeddings": True,
    "claude_vision": True,
    "llama": True,
    "total": 3,
    "loaded": 3,
    "failed": 0,
    "loading": 0,
    "models": {
        "whisper": {"status": "loaded", "load_time": 1.5},
        "embeddings": {"status": "loaded", "load_time": 2.0},
        "claude_vision": {"status": "loaded", "load_time": 0.8}
    }
}

MOCK_LOADER_STATUS_INCOMPLETE = {
    "models_loaded": False,
    "whisper": "on-demand",
    "embeddings": True,
    "claude_vision": True,
    "llama": True,
    "total": 3,
    "loaded": 1,
    "failed": 1,
    "loading": 1,
    "models": {
        "whisper": {"status": "failed", "load_time": 0.0},
        "embeddings": {"status": "loaded", "load_time": 2.0},
        "claude_vision": {"status": "loading", "load_time": 0.0}
    }
}

async def _mock_get_loader_status() -> Dict[str, Any]:
    return MOCK_LOADER_STATUS

async def _mock_get_loader_status_incomplete() -> Dict[str, Any]:
    return MOCK_LOADER_STATUS_INCOMPLETE

class _FakeWebSocket:
    def __init__(self):
        self.accepted = False
        self.messages = []
        self.disconnected = False
        
    async def accept(self):
        self.accepted = True
        
    async def send_json(self, message):
        self.messages.append(message)
        
    async def receive_text(self):
        # Simulate no messages received
        await asyncio.sleep(0.01)
        raise asyncio.TimeoutError()
        
    async def close(self):
        self.disconnected = True


def test_import_smoke() -> None:
    """Test that the module imports correctly and exposes expected functions."""
    # This should not raise any ImportError
    assert hasattr(ModelStatusManager, '__init__')
    assert callable(get_model_status)
    assert callable(model_status_websocket)
    assert callable(get_loading_progress)
    assert callable(broadcast_model_status)
    assert hasattr(status_manager, 'active_connections')










def test_model_status_manager_connect_disconnect() -> None:
    """Test ModelStatusManager connect and disconnect functionality."""
    manager = ModelStatusManager()
    websocket = _FakeWebSocket()
    
    # Test connect
    asyncio.run(manager.connect(websocket))
    assert len(manager.active_connections) == 1
    assert websocket.accepted is True
    assert len(websocket.messages) == 1  # Should have sent initial status
    
    # Test disconnect
    manager.disconnect(websocket)
    assert len(manager.active_connections) == 0


def test_model_status_manager_broadcast() -> None:
    """Test ModelStatusManager broadcast functionality."""
    manager = ModelStatusManager()
    websocket1 = _FakeWebSocket()
    websocket2 = _FakeWebSocket()
    
    # Connect two websockets
    asyncio.run(manager.connect(websocket1))
    asyncio.run(manager.connect(websocket2))
    
    # Broadcast status
    test_status = {'test': 'data'}
    asyncio.run(manager.broadcast_status(test_status))
    
    # Both should have received the message
    assert len(websocket1.messages) == 2  # Initial + broadcast
    assert len(websocket2.messages) == 2
    
    # Clean up
    manager.disconnect(websocket1)
    manager.disconnect(websocket2)


def test_broadcast_model_status() -> None:
    """Test the global broadcast_model_status function."""
    with patch('backend.api.model_status_api.get_loader_status', side_effect=_mock_get_loader_status):
        # Mock the manager's broadcast_status method
        with patch.object(status_manager, 'broadcast_status') as mock_broadcast:
            asyncio.run(broadcast_model_status())
            mock_broadcast.assert_called_once()
