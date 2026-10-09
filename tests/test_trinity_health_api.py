# [Ouroboros] Modified by Ouroboros (op=op-01a122b9-) at 2026-10-09 22:14 UTC
# Reason: `backend/api/trinity_health_api.py` has no corresponding test module. CREATE `tests/test_trinity_health_api.py` containi

from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.api.trinity_health_api import (
    ComponentStatus,
    TrinityHealthResponse,
    PrimeStatusResponse,
    get_trinity_health,
    get_prime_health,
    get_routing_status,
    get_degradation_status,
    reset_prime_circuit,
    liveness_probe,
    ping,
    detailed_status,
    _get_readiness_manager_safe,
    _check_prime_health,
    _check_reactor_health,
    _check_router_health,
    _get_routing_metrics,
    _get_degradation_status,
    _fallback_readiness_check
)

class MockReadinessManager:
    def __init__(self):
        self._status = {
            'phase': 'ready',
            'is_ready': True,
            'is_healthy': True,
            'message': 'Test manager ready'
        }
        
    def handle_probe(self, probe_type):
        class MockResponse:
            def __init__(self):
                self.status_code = 200
                self.content = {'status': 'ok', 'ready': True}
            
            def to_dict(self):
                return self.content
        
        return MockResponse()
    
    def get_status(self):
        return self._status


class MockPrimeRouter:
    def __init__(self):
        self._status = {
            'prime_client': {
                'available': True,
                'status': {
                    'status': 'available',
                    'circuit_breaker': {'state': 'closed'}
                }
            },
            'metrics': {'requests': 100, 'errors': 2},
            'config': {'model': 'test_model'}
        }
    
    def get_status(self):
        return self._status


class MockPrimeClient:
    def __init__(self):
        self._status = {
            'status': 'available'
        }
        
    def get_status(self):
        return self._status


class MockServiceRegistry:
    def __init__(self):
        pass

async def test_import_smoke() -> None:
    """Test that the module imports correctly and exposes expected functions."""
    # This should not raise any ImportError
    assert hasattr(ComponentStatus, '__init__')
    assert hasattr(TrinityHealthResponse, '__init__')
    assert hasattr(PrimeStatusResponse, '__init__')
    assert callable(get_trinity_health)
    assert callable(get_prime_health)
    assert callable(get_routing_status)
    assert callable(get_degradation_status)
    assert callable(reset_prime_circuit)
    assert callable(liveness_probe)
    assert callable(ping)
    assert callable(detailed_status)
    assert callable(_get_readiness_manager_safe)


async def test_get_trinity_health_with_manager() -> None:
    """Test get_trinity_health function with readiness manager available."""
    # Mock the manager and helper functions
    with patch('backend.api.trinity_health_api._get_readiness_manager_safe', return_value=MockReadinessManager()):
        with patch('backend.api.trinity_health_api._check_prime_health', return_value=ComponentStatus(name="JARVIS-Prime (Mind)", status="healthy")):
            with patch('backend.api.trinity_health_api._check_router_health', return_value=ComponentStatus(name="Prime Router", status="healthy")):
                with patch('backend.api.trinity_health_api._check_reactor_health', return_value=ComponentStatus(name="Reactor-Core (Nerves)", status="healthy")):
                    with patch('backend.api.trinity_health_api._get_routing_metrics', return_value={"test": "routing"}):
                        with patch('backend.api.trinity_health_api._get_degradation_status', return_value={"test": "degradation"}):
                            
                            result = await get_trinity_health()
                            
                            assert isinstance(result, TrinityHealthResponse)
                            assert result.overall_status == "healthy"
                            assert "jarvis_prime" in result.components
                            assert "prime_router" in result.components
                            assert "reactor_core" in result.components
                            assert result.routing == {"test": "routing"}
                            assert result.graceful_degradation == {"test": "degradation"}






async def test_liveness_probe_with_manager() -> None:
    """Test liveness_probe function with readiness manager available."""
    # Mock the manager
    with patch('backend.api.trinity_health_api._get_readiness_manager_safe', return_value=MockReadinessManager()):
        
        response = await liveness_probe()
        
        assert response.status_code == 200




async def test_detailed_status_with_manager() -> None:
    """Test detailed_status function with readiness manager available."""
    # Mock the manager
    with patch('backend.api.trinity_health_api._get_readiness_manager_safe', return_value=MockReadinessManager()):
        
        result = await detailed_status()
        
        assert isinstance(result, dict)
        assert "endpoint" in result
        assert "timestamp" in result


async def test_detailed_status_without_manager() -> None:
    """Test detailed_status function without readiness manager."""
    # Mock the manager to return None
    with patch('backend.api.trinity_health_api._get_readiness_manager_safe', return_value=None):
        
        result = await detailed_status()
        
        assert isinstance(result, dict)
        assert result["phase"] == "unknown"
        assert result["message"] == "Readiness manager not available"








