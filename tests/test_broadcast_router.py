# [Ouroboros] Modified by Ouroboros (op=op-01a121eb-) at 2026-10-09 18:29 UTC
# Reason: `backend/api/broadcast_router.py` has no corresponding test module. CREATE `tests/test_broadcast_router.py` containing f

from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.api.broadcast_router import (
    BroadcastEvent,
    BroadcastResponse,
    BroadcastConnectionManager,
    broadcast_event,
    get_broadcast_stats,
    get_speech_state,
    broadcast_websocket,
    broadcast_event_alt,
    broadcast_ws_alt,
    broadcast_maintenance_event,
    get_routers,
    manager
)


class MockWebSocket:
    def __init__(self):
        self.accepted = False
        self.sent_messages = []
        self.disconnected = False
        
    async def accept(self):
        self.accepted = True
        
    async def send_json(self, message):
        self.sent_messages.append(message)
        
    async def receive_json(self):
        # Simulate a timeout for idle connections
        await asyncio.sleep(10)
        return {'type': 'ping'}
        
    async def close(self):
        self.disconnected = True


async def test_import_smoke() -> None:
    """Test that the module imports correctly and exposes expected functions."""
    # This should not raise any ImportError
    assert hasattr(BroadcastEvent, '__init__')
    assert hasattr(BroadcastResponse, '__init__')
    assert callable(broadcast_event)
    assert callable(get_broadcast_stats)
    assert callable(get_speech_state)
    assert callable(broadcast_websocket)
    assert callable(broadcast_event_alt)
    assert callable(broadcast_ws_alt)
    assert callable(broadcast_maintenance_event)
    assert callable(get_routers)
    assert hasattr(manager, 'broadcast')


async def test_broadcast_event() -> None:
    """Test broadcast_event function with normal case."""
    # Mock the manager's broadcast method
    with patch.object(manager, 'broadcast', new_callable=AsyncMock) as mock_broadcast:
        mock_broadcast.return_value = 2
        
        event = BroadcastEvent(event="system_updating", data={"message": "Updating..."})
        result = await broadcast_event(event)
        
        assert result.success is True
        assert result.message == "Event system_updating broadcast successfully"
        assert result.clients_notified == 2
        mock_broadcast.assert_called_once()


async def test_broadcast_event_empty_data() -> None:
    """Test broadcast_event function with empty data."""
    with patch.object(manager, 'broadcast', new_callable=AsyncMock) as mock_broadcast:
        mock_broadcast.return_value = 0
        
        event = BroadcastEvent(event="system_restarting")
        result = await broadcast_event(event)
        
        assert result.success is True
        assert result.message == "Event system_restarting broadcast successfully"
        assert result.clients_notified == 0
        mock_broadcast.assert_called_once()


async def test_get_broadcast_stats() -> None:
    """Test get_broadcast_stats function."""
    with patch.object(manager, 'get_stats', return_value={'active_connections': 5}) as mock_stats:
        result = await get_broadcast_stats()
        
        assert result == {'active_connections': 5}
        mock_stats.assert_called_once()








async def test_broadcast_maintenance_event() -> None:
    """Test broadcast_maintenance_event function."""
    with patch.object(manager, 'broadcast', new_callable=AsyncMock) as mock_broadcast:
        mock_broadcast.return_value = 3
        
        result = await broadcast_maintenance_event("updating", "Downloading updates...", 30)
        
        assert result == 3
        mock_broadcast.assert_called_once()


async def test_get_routers() -> None:
    """Test get_routers function."""
    routers = get_routers()
    
    assert len(routers) == 2
    assert hasattr(routers[0], 'prefix')
    assert hasattr(routers[1], 'prefix')




async def test_broadcast_connection_manager_with_connections() -> None:
    """Test BroadcastConnectionManager with active connections."""
    manager_instance = BroadcastConnectionManager()
    
    # Add two mock connections
    mock_websocket1 = MockWebSocket()
    mock_websocket2 = MockWebSocket()
    
    await manager_instance.connect(mock_websocket1)
    await manager_instance.connect(mock_websocket2)
    
    # Test broadcast to multiple clients
    result = await manager_instance.broadcast({'type': 'test_message', 'data': 'test'})
    assert result == 2
    
    # Verify messages were sent
    assert len(mock_websocket1.sent_messages) == 1
    assert len(mock_websocket2.sent_messages) == 1
    
    # Test disconnect
    await manager_instance.disconnect(mock_websocket1)
    assert len(manager_instance._connections) == 1
