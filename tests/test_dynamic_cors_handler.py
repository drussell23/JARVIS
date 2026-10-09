# [Ouroboros] Modified by Ouroboros (op=op-01a12206-) at 2026-10-09 18:59 UTC
# Reason: `backend/api/dynamic_cors_handler.py` has no corresponding test module. CREATE `tests/test_dynamic_cors_handler.py` cont

from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict, Optional
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.api.dynamic_cors_handler import (
    DynamicCORSMiddleware,
    create_dynamic_cors_middleware,
    PortMismatchHandler,
    AutoPortDiscovery
)

class _FakeRequest:
    def __init__(self, headers=None, method="GET", client=None, url=None):
        self.headers = headers or {}
        self.method = method
        self.client = client or MagicMock()
        self.url = url or MagicMock()
        self.url.path = "/test"
        self.url.query = ""

class _FakeResponse:
    def __init__(self):
        self.status_code = 200
        self.headers = {}
        self.body = b""

async def test_import_smoke() -> None:
    """Test that the module imports correctly and exposes expected functions."""
    # This should not raise any ImportError
    assert callable(DynamicCORSMiddleware)
    assert callable(create_dynamic_cors_middleware)
    assert callable(PortMismatchHandler)
    assert callable(AutoPortDiscovery)
    assert callable(PortMismatchHandler.get_redirect_info)
    assert callable(PortMismatchHandler.create_port_redirect_endpoint)
    assert callable(AutoPortDiscovery.discover_services)
    assert callable(AutoPortDiscovery.get_recommended_config)


async def test_dynamic_cors_middleware_init() -> None:
    """Test DynamicCORSMiddleware initialization."""
    app = MagicMock()
    middleware = DynamicCORSMiddleware(app)
    
    assert middleware.app == app
    assert isinstance(middleware.known_origins, set)
    assert len(middleware.auto_allowed_patterns) > 0
    assert len(middleware.known_origins) > 0


async def test_is_origin_allowed() -> None:
    """Test origin allowed checking logic."""
    app = MagicMock()
    middleware = DynamicCORSMiddleware(app)
    
    # Test empty origin
    assert middleware.is_origin_allowed("")
    
    # Test known origins
    assert middleware.is_origin_allowed("http://localhost:3000")
    
    # Test auto-allowed patterns
    assert middleware.is_origin_allowed("http://localhost:8000")
    assert middleware.is_origin_allowed("https://127.0.0.1:3000")
    assert middleware.is_origin_allowed("http://[::1]:8000")
    
    # Test non-allowed origin
    assert not middleware.is_origin_allowed("http://evil.com")




async def test_detect_port_mismatch() -> None:
    """Test port mismatch detection."""
    app = MagicMock()
    middleware = DynamicCORSMiddleware(app)
    
    # Test with referer header
    request = _FakeRequest(headers={"referer": "http://localhost:3001/test"})
    result = middleware.detect_port_mismatch(request)
    assert result == 3001
    
    # Test with origin header
    request = _FakeRequest(headers={"origin": "http://localhost:3002/test"})
    result = middleware.detect_port_mismatch(request)
    assert result == 3002
    
    # Test with no port in URL
    request = _FakeRequest(headers={"origin": "http://localhost/test"})
    result = middleware.detect_port_mismatch(request)
    assert result is None
    
    # Test with invalid URL
    request = _FakeRequest(headers={"origin": "invalid-url"})
    result = middleware.detect_port_mismatch(request)
    assert result is None


async def test_create_dynamic_cors_middleware() -> None:
    """Test dynamic CORS middleware creation."""
    app = MagicMock()
    
    # Test with no environment origins
    with patch('os.getenv', return_value=''):
        middleware = create_dynamic_cors_middleware(app)
        assert isinstance(middleware, DynamicCORSMiddleware)
        
    # Test with environment origins
    with patch('os.getenv', return_value='http://env.com,https://another.com'):
        middleware = create_dynamic_cors_middleware(app)
        assert isinstance(middleware, DynamicCORSMiddleware)


async def test_port_mismatch_handler_get_redirect_info() -> None:
    """Test port mismatch handler redirect info generation."""
    request = _FakeRequest(headers={"origin": "http://localhost:3001/test"})
    
    # Test with port mismatch
    result = PortMismatchHandler.get_redirect_info(request, 8000)
    assert "error" in result
    assert result["error"] == "port_mismatch"
    assert "correct_url" in result
    assert "redirect_endpoint" in result
    
    # Test with no port mismatch (same port)
    result = PortMismatchHandler.get_redirect_info(request, 3001)
    assert result == {}


async def test_port_mismatch_handler_create_redirect_endpoint() -> None:
    """Test port mismatch handler redirect endpoint creation."""
    app = MagicMock()
    request = _FakeRequest(headers={"origin": "http://localhost:3001/test?param=value"})
    
    # Test successful redirect
    result = await PortMismatchHandler.create_port_redirect_endpoint(app, request, 8000)
    assert "redirect" in result
    assert result["redirect"] is True
    assert "new_url" in result
    assert "headers" in result
    
    # Test with no origin
    request = _FakeRequest(headers={})
    result = await PortMismatchHandler.create_port_redirect_endpoint(app, request, 8000)
    assert "error" in result


async def test_auto_port_discovery_discover_services() -> None:
    """Test auto port discovery service discovery."""
    # Mock socket to simulate port availability
    with patch('socket.socket') as mock_socket:
        mock_instance = MagicMock()
        mock_socket.return_value = mock_instance
        mock_instance.connect_ex.return_value = 0  # Port is open
        
        result = await AutoPortDiscovery.discover_services()
        assert isinstance(result, dict)
        
    # Test with no ports available
    with patch('socket.socket') as mock_socket:
        mock_instance = MagicMock()
        mock_socket.return_value = mock_instance
        mock_instance.connect_ex.return_value = 1  # Port is closed
        
        result = await AutoPortDiscovery.discover_services()
        assert isinstance(result, dict)


async def test_auto_port_discovery_get_recommended_config() -> None:
    """Test auto port discovery recommended config generation."""
    services = {
        "frontend": {"port": 3000, "url": "http://localhost:3000", "status": "active"},
        "backend": {"port": 8000, "url": "http://localhost:8000", "status": "active"}
    }
    
    result = AutoPortDiscovery.get_recommended_config(services)
    assert "frontend_url" in result
    assert "backend_url" in result
    assert "recommended_cors_origins" in result
    assert len(result["recommended_cors_origins"]) > 0


async def test_dynamic_cors_middleware_call() -> None:
    """Test DynamicCORSMiddleware call method."""
    app = MagicMock()
    middleware = DynamicCORSMiddleware(app)
    
    # Mock call_next to return a response
    async def mock_call_next(request):
        response = _FakeResponse()
        response.status_code = 200
        return response
    
    request = _FakeRequest(headers={"origin": "http://localhost:3001/test"})
    
    # Test with OPTIONS method
    request.method = "OPTIONS"
    result = await middleware.__call__(request, mock_call_next)
    assert result.status_code == 200
    
    # Test with regular GET method
    request.method = "GET"
    result = await middleware.__call__(request, mock_call_next)
    assert result.status_code == 200
