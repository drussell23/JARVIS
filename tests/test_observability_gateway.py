# [Ouroboros] Modified by Ouroboros (op=op-01a12229-) at 2026-10-09 19:36 UTC
# Reason: `backend/api/observability_gateway.py` has no corresponding test module. CREATE `tests/test_observability_gateway.py` co

from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.api.observability_gateway import (
    GatewayFrame,
    gateway_enabled,
    make_frame,
    frames_for_lifecycle,
    build_causality_graph,
    build_gateway_subscriber,
    register_gateway_bridge,
    command_center_gateway_enabled,
    gateway_host,
    gateway_port,
    build_gateway_app,
    serve_gateway,
    build_router,
    observability_manager
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
    assert callable(gateway_enabled)
    assert callable(make_frame)
    assert callable(frames_for_lifecycle)
    assert callable(build_causality_graph)
    assert callable(build_gateway_subscriber)
    assert callable(register_gateway_bridge)
    assert callable(command_center_gateway_enabled)
    assert callable(gateway_host)
    assert callable(gateway_port)
    assert callable(build_gateway_app)
    assert callable(serve_gateway)
    assert callable(build_router)
    assert hasattr(observability_manager, 'broadcast')


async def test_gateway_enabled() -> None:
    """Test gateway_enabled function with different environment configurations."""
    # Test default (should be True)
    with patch.dict('os.environ', {}, clear=True):
        assert gateway_enabled() is True
    
    # Test explicit True values
    for truthy_val in ('1', 'true', 'yes', 'on'):
        with patch.dict('os.environ', {'JARVIS_OBSERVABILITY_GATEWAY_ENABLED': truthy_val}):
            assert gateway_enabled() is True
    
    # Test False values
    for false_val in ('0', 'false', 'no', 'off', 'maybe'):
        with patch.dict('os.environ', {'JARVIS_OBSERVABILITY_GATEWAY_ENABLED': false_val}):
            assert gateway_enabled() is False


async def test_make_frame() -> None:
    """Test make_frame function with various inputs."""
    # Test basic frame creation
    frame = make_frame('test_kind', op_id='test_op', payload={'key': 'value'})
    assert frame['kind'] == 'test_kind'
    assert frame['op_id'] == 'test_op'
    assert frame['schema_version'] == 'gateway_frame.v1'
    assert frame['payload'] == {'key': 'value'}
    assert isinstance(frame['ts'], float)
    
    # Test with minimal inputs
    frame = make_frame('minimal')
    assert frame['kind'] == 'minimal'
    assert frame['op_id'] == ''
    assert frame['payload'] == {}




async def test_build_causality_graph() -> None:
    """Test build_causality_graph function with normal and error cases."""
    # Mock the cognitive_observability module
    with patch('backend.core.ouroboros.governance.cognitive_observability.recent_why_snapshots') as mock_snapshots:
        
        # Test successful case
        mock_snapshots.return_value = [
            {'op_id': 'op1', 'why': {}, 'target_files': ['file1.txt']},
            {'op_id': 'op2', 'why': {}, 'target_files': ['file2.txt']}
        ]
        
        graph = build_causality_graph(limit=2)
        assert 'nodes' in graph
        assert 'edges' in graph
        assert 'count' in graph
        
        # Test error case (should return default empty structure)
        mock_snapshots.side_effect = Exception('Test error')
        graph = build_causality_graph()
        assert graph == {'nodes': [], 'edges': [], 'diagnostic': 'unavailable'}








async def test_gateway_host_port() -> None:
    """Test gateway_host and gateway_port functions."""
    # Test default values
    with patch.dict('os.environ', {}, clear=True):
        assert gateway_host() == '127.0.0.1'
        assert gateway_port() == 8000
    
    # Test custom values
    with patch.dict('os.environ', {'JARVIS_GATEWAY_HOST': 'custom.host', 'JARVIS_BACKEND_PORT': '9000'}):
        assert gateway_host() == 'custom.host'
        assert gateway_port() == 9000




