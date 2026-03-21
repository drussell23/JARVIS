"""Tests for ProactiveDriveService async lifecycle."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from backend.core.topology.proactive_drive_service import (
    ProactiveDriveService,
    ProactiveDriveConfig,
    ServiceState,
)
from backend.core.topology.hardware_env import ComputeTier, HardwareEnvironmentState
from backend.core.topology.idle_verifier import LittlesLawVerifier
from backend.core.topology.topology_map import CapabilityNode, TopologyMap


class TestProactiveDriveConfig:
    def test_from_env_defaults(self):
        config = ProactiveDriveConfig.from_env()
        assert config.tick_interval_seconds == 10.0
        assert config.max_queue_depth == 1000

    def test_from_env_override(self):
        with patch.dict("os.environ", {"JARVIS_PROACTIVE_TICK_INTERVAL": "5.0"}):
            config = ProactiveDriveConfig.from_env()
            assert config.tick_interval_seconds == 5.0


class TestServiceState:
    def test_enum_values(self):
        assert ServiceState.INACTIVE.value == "inactive"
        assert ServiceState.ACTIVE.value == "active"
        assert ServiceState.FAILED.value == "failed"


class TestProactiveDriveService:
    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        assert service.state == ServiceState.INACTIVE
        await service.start()
        assert service.state == ServiceState.ACTIVE
        assert service.hardware is not None
        await service.stop()
        assert service.state == ServiceState.INACTIVE

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        await service.start()  # second call is no-op
        assert service.state == ServiceState.ACTIVE
        await service.stop()

    @pytest.mark.asyncio
    async def test_stop_idempotent(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.stop()  # stop without start
        assert service.state == ServiceState.INACTIVE

    @pytest.mark.asyncio
    async def test_hardware_discovered_at_start(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        assert service.hardware is not None
        assert service.hardware.cpu_logical_cores >= 1
        await service.stop()

    @pytest.mark.asyncio
    async def test_verifier_created_for_jarvis(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        assert "jarvis" in service.verifiers
        assert isinstance(service.verifiers["jarvis"], LittlesLawVerifier)
        await service.stop()

    @pytest.mark.asyncio
    async def test_record_sample_feeds_verifier(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        service.record_sample("jarvis", depth=5, latency_ms=100.0)
        assert len(service.verifiers["jarvis"]._samples) == 1
        await service.stop()

    @pytest.mark.asyncio
    async def test_tick_loop_runs(self):
        config = ProactiveDriveConfig(tick_interval_seconds=0.05)
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        await asyncio.sleep(0.15)  # let a few ticks run
        assert service.drive.state in ("REACTIVE", "MEASURING")
        await service.stop()

    @pytest.mark.asyncio
    async def test_health_returns_dict(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        h = service.health()
        assert "state" in h
        assert "drive_state" in h
        assert "hardware_tier" in h
        await service.stop()

    @pytest.mark.asyncio
    async def test_telemetry_emitted_on_tick(self):
        mock_bus = MagicMock()
        mock_bus.emit = MagicMock()
        config = ProactiveDriveConfig(tick_interval_seconds=0.05)
        service = ProactiveDriveService(config=config, telemetry_bus=mock_bus)
        await service.start()
        await asyncio.sleep(0.15)
        await service.stop()
        # Bus should have been called at least once (hardware at start + drive ticks)
        assert mock_bus.emit.call_count >= 1

    @pytest.mark.asyncio
    async def test_record_sample_unknown_repo_is_noop(self):
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        await service.start()
        service.record_sample("nonexistent", depth=5, latency_ms=100.0)
        # Should not raise
        await service.stop()
