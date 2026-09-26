# [Ouroboros] Modified by Ouroboros (op=op-01a0de5f-) at 2026-09-26 15:44 UTC
# Reason: `backend/autonomy/reactor_core_integration.py` has no corresponding test module. CREATE `tests/test_reactor_core_integra

"""
Reactor-Core Integration Module Tests
=====================================

Tests for backend/autonomy/reactor_core_integration.py.
"""
from __future__ import annotations
import asyncio
import datetime
import importlib.util
import json
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional, Tuple
import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_REACTOR_CORE_INTEGRATION_PATH = _REPO_ROOT / "backend" / "autonomy" / "reactor_core_integration.py"


def _load_reactor_core_integration():
    spec = importlib.util.spec_from_file_location(
        "reactor_core_integration", str(_REACTOR_CORE_INTEGRATION_PATH)
    )
    assert spec and spec.loader, f"could not load spec from {_REACTOR_CORE_INTEGRATION_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reactor_core_integration"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def R():
    """Module-scoped fixture: load the reactor core integration module once per test session."""
    return _load_reactor_core_integration()


class TestReactorCoreIntegration:
    def test_import_smoke(self, R):
        """Verify that the module imports without error and has expected classes."""
        assert hasattr(R, 'ReactorCoreConfig')
        assert hasattr(R, 'ReactorCoreIntegration')
        assert hasattr(R, 'PrimeNeuralMeshBridge')
        assert hasattr(R, 'FallbackCommunicationBus')
        assert hasattr(R, 'get_reactor_core_integration')
        assert hasattr(R, 'get_prime_neural_mesh_bridge')
        assert hasattr(R, 'initialize_reactor_core')
        assert hasattr(R, 'initialize_prime_neural_mesh')
        assert hasattr(R, 'shutdown_reactor_core')

    @pytest.mark.asyncio
    async def test_initialize_no_config(self, R):
        """Test initialization without config."""
        integration = R.ReactorCoreIntegration()
        result = await integration.initialize()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_initialize_with_config(self, R):
        """Test initialization with a config."""
        config = R.ReactorCoreConfig(
            reactor_core_path=pathlib.Path("/tmp"),
            jarvis_prime_path=pathlib.Path("/tmp"),
            jarvis_connector_enabled=True,
            experience_lookback_hours=24,
            enable_file_watching=False,
            scout_enabled=True,
            scout_max_topics=10,
            scout_max_pages_per_topic=5,
            scout_concurrency=2,
            scout_use_docker=False,
            prime_connector_enabled=True,
            prime_host="localhost",
            prime_port=8080,
            prime_port_candidates=[8080, 8081],
            prime_websocket_enabled=True,
            prime_websocket_paths=["/ws"],
            prime_health_paths=["/health"],
            prime_event_poll_interval=1.0,
            prime_event_probe_timeout=5.0,
            prime_transport_reprobe_interval=30.0,
            training_enabled=True,
            training_base_model="test-model",
            training_quantization="4bit"
        )
        integration = R.ReactorCoreIntegration(config=config)
        result = await integration.initialize()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_get_recent_experiences_no_connector(self, R):
        """Test get_recent_experiences when connector is not initialized."""
        integration = R.ReactorCoreIntegration()
        result = await integration.get_recent_experiences()
        assert isinstance(result, list)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_get_corrections_no_connector(self, R):
        """Test get_corrections when connector is not initialized."""
        integration = R.ReactorCoreIntegration()
        result = await integration.get_corrections()
        assert isinstance(result, list)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_scrape_topics_no_scout(self, R):
        """Test scrape_topics when scout is not initialized."""
        integration = R.ReactorCoreIntegration()
        result = await integration.scrape_topics(["test-topic"])
        assert isinstance(result, dict)
        assert 'error' in result

    @pytest.mark.asyncio
    async def test_add_scraping_topic_no_scout(self, R):
        """Test add_scraping_topic when scout is not initialized."""
        integration = R.ReactorCoreIntegration()
        result = await integration.add_scraping_topic("test-topic")
        assert isinstance(result, bool)
        assert result is False

    @pytest.mark.asyncio
    async def test_check_prime_health_no_connector(self, R):
        """Test check_prime_health when connector is not initialized."""
        integration = R.ReactorCoreIntegration()
        result = await integration.check_prime_health()
        assert isinstance(result, dict)
        assert 'status' in result
        assert result['status'] == 'unavailable'

    @pytest.mark.asyncio
    async def test_get_prime_interactions_no_connector(self, R):
        """Test get_prime_interactions when connector is not initialized."""
        integration = R.ReactorCoreIntegration()
        result = await integration.get_prime_interactions()
        assert isinstance(result, list)
        assert len(result) == 0
