"""Verify IntakeLayerService wires CUExecutionSensor to the router."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake.intake_layer_service import (
    IntakeLayerConfig,
    IntakeLayerService,
    IntakeServiceState,
)
from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
    CUExecutionSensor,
)


@pytest.fixture()
def fresh_cu_singleton():
    """Reset CUExecutionSensor singleton before/after test."""
    CUExecutionSensor._instance = None
    yield
    CUExecutionSensor._instance = None


@pytest.mark.asyncio
async def test_intake_layer_wires_cu_sensor(tmp_path, fresh_cu_singleton):
    """After start(), the CUExecutionSensor singleton must have a router."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)

    await svc.start()

    try:
        # The singleton should now have a router wired
        sensor = CUExecutionSensor()
        assert sensor._router is not None, (
            "CUExecutionSensor._router is None after IntakeLayerService.start() — "
            "wiring is missing in _build_components()"
        )

        # Verify it's in the sensors list (has start/stop lifecycle)
        cu_sensors = [s for s in svc._sensors if isinstance(s, CUExecutionSensor)]
        assert len(cu_sensors) == 1, (
            f"Expected exactly 1 CUExecutionSensor in _sensors, found {len(cu_sensors)}"
        )
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_cu_sensor_router_matches_intake_router(tmp_path, fresh_cu_singleton):
    """CUExecutionSensor's router must be the same instance as the intake router."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)

    await svc.start()

    try:
        sensor = CUExecutionSensor()
        assert sensor._router is svc._router, (
            "CUExecutionSensor._router is not the same instance as "
            "IntakeLayerService._router — wiring uses wrong router"
        )
    finally:
        await svc.stop()
