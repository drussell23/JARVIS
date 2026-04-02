"""CUExecutionSensor spine tests — envelope flows sensor → router."""
import pytest

from backend.core.ouroboros.governance.intake.unified_intake_router import _PRIORITY_MAP


def test_cu_execution_has_explicit_priority():
    """cu_execution must have an explicit priority, not fallback 99."""
    assert "cu_execution" in _PRIORITY_MAP
    assert _PRIORITY_MAP["cu_execution"] == 5


import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
    CUExecutionRecord,
    CUExecutionSensor,
)


def _make_failure_record(goal: str = "send message to Alice", error: str = "target not found") -> CUExecutionRecord:
    """Build a CU failure record with a deterministic signature."""
    return CUExecutionRecord(
        goal=goal,
        success=False,
        steps_completed=2,
        steps_total=5,
        elapsed_s=3.0,
        error=error,
        is_messaging=True,
        contact="Alice",
        app="messages",
    )


@pytest.fixture()
def fresh_cu_sensor():
    """Yield a fresh CUExecutionSensor with cleared singleton state."""
    # Reset singleton so each test gets a clean sensor
    CUExecutionSensor._instance = None
    sensor = CUExecutionSensor.__new__(CUExecutionSensor)
    sensor._initialized = False
    yield sensor
    # Cleanup
    CUExecutionSensor._instance = None


@pytest.mark.asyncio
async def test_graduation_emits_envelope_to_router(fresh_cu_sensor):
    """After 3 failures with the same signature, sensor calls router.ingest()."""
    mock_router = MagicMock()
    mock_router.ingest = AsyncMock(return_value="enqueued")

    sensor = CUExecutionSensor(router=mock_router, repo="jarvis")

    # Feed 3 failures (graduation threshold is 3)
    for _ in range(3):
        await sensor.record(_make_failure_record())

    # Verify envelope was emitted
    assert sensor._total_envelopes_emitted >= 1
    mock_router.ingest.assert_called_once()

    # Verify envelope contents
    envelope = mock_router.ingest.call_args[0][0]
    assert envelope.source == "cu_execution"
    assert envelope.repo == "jarvis"
    assert "cu_task_planner.py" in envelope.target_files or len(envelope.target_files) > 0


@pytest.mark.asyncio
async def test_no_router_logs_warning_and_drops(fresh_cu_sensor, caplog):
    """Without a router, graduation logs a warning and does not raise."""
    sensor = CUExecutionSensor(router=None, repo="jarvis")

    for _ in range(3):
        await sensor.record(_make_failure_record())

    assert sensor._total_envelopes_emitted == 0
    assert "No router wired" in caplog.text


@pytest.mark.asyncio
async def test_success_records_do_not_trigger_graduation(fresh_cu_sensor):
    """Successful CU executions should not accumulate toward graduation."""
    mock_router = MagicMock()
    mock_router.ingest = AsyncMock(return_value="enqueued")

    sensor = CUExecutionSensor(router=mock_router, repo="jarvis")

    for _ in range(5):
        await sensor.record(CUExecutionRecord(
            goal="send message to Alice",
            success=True,
            steps_completed=5,
            steps_total=5,
            elapsed_s=2.0,
        ))

    assert sensor._total_envelopes_emitted == 0
    mock_router.ingest.assert_not_called()


from backend.core.ouroboros.governance.intake.intake_layer_service import (
    IntakeLayerConfig,
    IntakeLayerService,
)


@pytest.mark.asyncio
async def test_e2e_cu_graduation_through_intake_layer(tmp_path, fresh_cu_sensor):
    """Full E2E: CU failures → sensor graduation → router.ingest().

    This proves the spinal cord is connected: ActionDispatcher feeds
    CUExecutionSensor, which emits to the router wired by IntakeLayerService.
    """
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)

    await svc.start()

    try:
        # Spy on the router's ingest method
        original_ingest = svc._router.ingest
        ingest_calls = []

        async def spy_ingest(envelope):
            ingest_calls.append(envelope)
            return await original_ingest(envelope)

        svc._router.ingest = spy_ingest

        # Get the singleton sensor (now wired by IntakeLayerService)
        sensor = CUExecutionSensor()
        assert sensor._router is not None, "Pre-condition: sensor must have router"

        # Feed 3 identical failures to cross graduation threshold
        for _ in range(3):
            await sensor.record(_make_failure_record())

        # Verify envelope was emitted and reached the router
        assert sensor._total_envelopes_emitted >= 1, (
            "Sensor did not emit any envelopes after 3 failures"
        )
        assert len(ingest_calls) >= 1, (
            "Router.ingest was never called — envelope dropped between sensor and router"
        )

        # Verify envelope metadata
        envelope = ingest_calls[0]
        assert envelope.source == "cu_execution"
        assert envelope.repo == "jarvis"
        assert envelope.urgency == "normal"
    finally:
        await svc.stop()
