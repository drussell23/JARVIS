import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent, GapSignalBus
from backend.core.ouroboros.governance.intake.sensors.capability_gap_sensor import (
    CapabilityGapSensor,
)


def _evt(task_type="vision_action", target_app="xcode"):
    return CapabilityGapEvent(
        goal="open prefs",
        task_type=task_type,
        target_app=target_app,
        source="primary_fallback",
    )


@pytest.mark.asyncio
async def test_sensor_submits_envelope_for_gap():
    bus = GapSignalBus(maxsize=10)
    mock_router = AsyncMock()
    mock_router.submit = AsyncMock()

    with patch(
        "backend.core.ouroboros.governance.intake.sensors.capability_gap_sensor.make_envelope"
    ) as mock_envelope:
        mock_envelope.return_value = MagicMock()
        sensor = CapabilityGapSensor(intake_router=mock_router, repo="jarvis", bus=bus)
        task = asyncio.create_task(
            asyncio.wait_for(sensor._poll_once(), timeout=0.5)
        )
        bus.emit(_evt())
        try:
            await task
        except asyncio.TimeoutError:
            pass

    mock_envelope.assert_called_once()
    call_kwargs = mock_envelope.call_args[1] if mock_envelope.call_args[1] else {}
    call_args = mock_envelope.call_args[0] if mock_envelope.call_args[0] else ()
    # source="capability_gap" must be in the call
    all_args = list(call_args) + list(call_kwargs.values())
    assert any("capability_gap" in str(a) for a in all_args) or \
           call_kwargs.get("source") == "capability_gap"


def test_sensor_location():
    """Sensor must be in the sensors/ subdirectory, not directly in intake/."""
    import importlib
    mod = importlib.import_module(
        "backend.core.ouroboros.governance.intake.sensors.capability_gap_sensor"
    )
    assert hasattr(mod, "CapabilityGapSensor")


def test_capability_gap_in_valid_sources():
    from backend.core.ouroboros.governance.intake.intent_envelope import _VALID_SOURCES
    assert "capability_gap" in _VALID_SOURCES


def test_capability_gap_sensor_registered_in_agent_initializer():
    """agent_initializer must import CapabilityGapSensor."""
    import importlib.util
    src = importlib.util.find_spec(
        "backend.neural_mesh.agents.agent_initializer"
    )
    assert src is not None
    assert src.origin is not None
    text = open(src.origin).read()
    assert "CapabilityGapSensor" in text
