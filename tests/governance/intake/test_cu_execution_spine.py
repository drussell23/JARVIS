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
