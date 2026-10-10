# [Ouroboros] Modified by Ouroboros (op=op-01a12319-) at 2026-10-10 00:00 UTC
# Reason: `backend/orchestrator/service_registry.py` has no corresponding test module. CREATE `tests/test_service_registry.py` con

from __future__ import annotations
import asyncio
import dataclasses
import json
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock
from backend.orchestrator.service_registry import (
    ServiceStatus,
    ServiceType,
    ServiceInfo,
    ServiceRegistry,
    get_service_registry,
)

class TestServiceRegistry:
    def test_import_smoke(self):
        """Test that the module can be imported without errors."""
        assert ServiceRegistry is not None
        assert ServiceStatus is not None
        assert ServiceType is not None
        assert ServiceInfo is not None
        assert get_service_registry is not None

    def test_singleton_pattern(self):
        """Test that ServiceRegistry follows singleton pattern."""
        registry1 = ServiceRegistry()
        registry2 = ServiceRegistry()
        assert registry1 is registry2


    def test_register_service_with_metadata(self):
        """Test service registration with metadata."""
        registry = ServiceRegistry()
        metadata = {"key": "value", "number": 42}
        info = registry.register(
            name="metadata-service",
            service_type=ServiceType.REACTOR_CORE,
            port=9000,
            metadata=metadata
        )
        
        assert info.metadata == metadata

    def test_unregister_service(self):
        """Test service unregistration functionality."""
        registry = ServiceRegistry()
        registry.register("test-unregister", ServiceType.JARVIS_PRIME, port=8081)
        
        # Verify service exists
        assert registry.get("test-unregister") is not None
        
        # Unregister and verify removal
        registry.unregister("test-unregister")
        assert registry.get("test-unregister") is None

    def test_get_service(self):
        """Test getting services by name."""
        registry = ServiceRegistry()
        registry.register("get-test", ServiceType.LOADING_SERVER, port=8082)
        
        info = registry.get("get-test")
        assert info is not None
        assert info.name == "get-test"
        
        # Test non-existent service
        assert registry.get("non-existent") is None

    def test_get_all_services(self):
        """Test getting all registered services."""
        registry = ServiceRegistry()
        registry.register("all-test-1", ServiceType.FRONTEND, port=3000)
        registry.register("all-test-2", ServiceType.WEBSOCKET, port=3001)
        
        all_services = registry.get_all()
        assert len(all_services) >= 2  # Default services + our test ones
        assert "all-test-1" in all_services
        assert "all-test-2" in all_services


    def test_get_healthy(self):
        """Test getting healthy services."""
        registry = ServiceRegistry()
        registry.register("healthy-test", ServiceType.JARVIS_PRIME, port=8001)
        
        # Initially should be unknown status
        healthy = registry.get_healthy()
        assert len(healthy) == 0
        
        # Update to healthy
        registry.update_status("healthy-test", ServiceStatus.HEALTHY)
        healthy = registry.get_healthy()
        assert len(healthy) == 1
        assert healthy[0].name == "healthy-test"

    def test_update_status(self):
        """Test updating service status."""
        registry = ServiceRegistry()
        registry.register("status-test", ServiceType.JARVIS_BODY, port=8010)
        
        # Update to healthy
        registry.update_status("status-test", ServiceStatus.HEALTHY)
        info = registry.get("status-test")
        assert info.status == ServiceStatus.HEALTHY
        assert info.last_healthy is not None
        assert info.consecutive_failures == 0
        
        # Update to unhealthy
        registry.update_status("status-test", ServiceStatus.UNHEALTHY)
        info = registry.get("status-test")
        assert info.status == ServiceStatus.UNHEALTHY
        assert info.consecutive_failures == 1

    def test_add_status_callback(self):
        """Test adding status change callbacks."""
        registry = ServiceRegistry()
        callback_called = False
        
        def mock_callback(name, status):
            nonlocal callback_called
            callback_called = True
        
        registry.add_status_callback(mock_callback)
        registry.register("callback-test", ServiceType.JARVIS_PRIME, port=8001)
        registry.update_status("callback-test", ServiceStatus.HEALTHY)
        
        assert callback_called



    def test_pre_flight_cleanup(self):
        """Test pre-flight cleanup functionality."""
        registry = ServiceRegistry()
        
        # Register a service
        registry.register("cleanup-test", ServiceType.JARVIS_BODY, port=8010)
        
        # Run cleanup
        stats = asyncio.run(registry.pre_flight_cleanup())
        
        assert isinstance(stats, dict)
        assert "total_entries" in stats
        assert "valid_entries" in stats
        assert "stale_heartbeats_removed" in stats



    def test_service_registry_summary(self):
        """Test service registry summary functionality."""
        registry = ServiceRegistry()
        
        # Register a few services
        registry.register("summary-test-1", ServiceType.JARVIS_BODY, port=8010)
        registry.register("summary-test-2", ServiceType.JARVIS_PRIME, port=8001)
        
        summary = registry.get_summary()
        assert "total_services" in summary
        assert "healthy" in summary
        assert "unhealthy" in summary
        assert "stopped" in summary
        assert "statuses" in summary