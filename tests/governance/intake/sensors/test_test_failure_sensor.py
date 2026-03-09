"""Tests for TestFailureSensor (Sensor B)."""
from unittest.mock import AsyncMock, MagicMock
import pytest

from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)
from backend.core.ouroboros.governance.intent.signals import IntentSignal


def _make_signal(stable: bool = True, streak: int = 2) -> IntentSignal:
    return IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_auth.py",),
        repo="jarvis",
        description="Stable test failure: test_auth::test_login",
        evidence={
            "signature": "AssertionError:tests/test_auth.py",
            "test_id": "tests/test_auth.py::test_login",
            "streak": streak,
            "error_text": "AssertionError",
        },
        confidence=min(0.95, 0.7 + 0.1 * streak),
        stable=stable,
    )


async def test_stable_signal_produces_envelope():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)
    signal = _make_signal(stable=True)
    envelope = await sensor._signal_to_envelope_and_ingest(signal)
    assert envelope is not None
    assert envelope.source == "test_failure"
    assert envelope.target_files == ("tests/test_auth.py",)
    assert envelope.urgency == "high"
    assert envelope.evidence["test_id"] == "tests/test_auth.py::test_login"
    router.ingest.assert_called_once_with(envelope)


async def test_unstable_signal_is_skipped():
    router = MagicMock()
    router.ingest = AsyncMock()
    sensor = TestFailureSensor(repo="jarvis", router=router)
    signal = _make_signal(stable=False)
    result = await sensor._signal_to_envelope_and_ingest(signal)
    assert result is None
    router.ingest.assert_not_called()


async def test_handle_signals_batch():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)
    signals = [_make_signal(stable=True), _make_signal(stable=False)]
    results = await sensor.handle_signals(signals)
    # Only 1 stable signal → 1 envelope
    assert len([r for r in results if r is not None]) == 1


async def test_confidence_preserved_from_signal():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)
    signal = _make_signal(stable=True, streak=5)
    envelope = await sensor._signal_to_envelope_and_ingest(signal)
    assert envelope is not None
    # confidence should reflect streak: min(0.95, 0.7 + 0.1*5) = 0.95 (capped at 1.0 by envelope)
    assert 0.9 <= envelope.confidence <= 1.0
