# [Ouroboros] Modified by Ouroboros (op=op-019d9249-) at 2026-04-15 18:13 UTC
# Reason: Write four focused sensor-level test modules for the TestFailureSensor in-flight dedup mechanism shipped in commit 20baa

"""Module A: In-flight dedup - second signal for the same target within
the TTL window is suppressed with the expected 'already in-flight' log line.
"""
from __future__ import annotations

import importlib
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    test_failure_sensor as tfs,
)
from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)
from backend.core.ouroboros.governance.intent.signals import IntentSignal


def _make_signal(
    target: str = "tests/test_auth.py",
    stable: bool = True,
    streak: int = 2,
) -> IntentSignal:
    return IntentSignal(
        source="intent:test_failure",
        target_files=(target,),
        repo="jarvis",
        description=f"Stable test failure: {target}::test_login",
        evidence={
            "signature": f"AssertionError:{target}",
            "test_id": f"{target}::test_login",
            "streak": streak,
            "error_text": "AssertionError",
        },
        confidence=min(0.95, 0.7 + 0.1 * streak),
        stable=stable,
    )


def _make_router(return_value: str = "enqueued") -> MagicMock:
    router = MagicMock()
    router.ingest = AsyncMock(return_value=return_value)
    return router


async def test_second_signal_same_target_is_suppressed():
    """Canonical v5 repro: second signal for the same target file within
    the TTL window must never reach router.ingest.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    sig1 = _make_signal(streak=2)
    env1 = await sensor._signal_to_envelope_and_ingest(sig1)
    assert env1 is not None
    assert router.ingest.await_count == 1

    sig2 = _make_signal(streak=3)  # same target, next poll cycle
    env2 = await sensor._signal_to_envelope_and_ingest(sig2)

    assert env2 is None, "second emission must be suppressed while target is in-flight"
    assert router.ingest.await_count == 1, (
        "router.ingest must not be called for the suppressed signal"
    )


async def test_suppression_logged_as_already_in_flight(caplog: pytest.LogCaptureFixture):
    """The suppression decision must be visible in the log at INFO level
    with the 'already in-flight' phrase so operators can trace it.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    sig1 = _make_signal(streak=2)
    await sensor._signal_to_envelope_and_ingest(sig1)

    sig2 = _make_signal(streak=3)
    with caplog.at_level(logging.INFO, logger="backend.core.ouroboros.governance.intake.sensors.test_failure_sensor"):
        await sensor._signal_to_envelope_and_ingest(sig2)

    assert any(
        "already in-flight" in record.message
        for record in caplog.records
    ), "expected 'already in-flight' log line when suppressing a duplicate signal"


async def test_third_signal_also_suppressed():
    """Suppression is not a one-shot: every subsequent signal for the
    same in-flight target is rejected until TTL expiry or explicit release.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    await sensor._signal_to_envelope_and_ingest(_make_signal(streak=2))
    assert router.ingest.await_count == 1

    await sensor._signal_to_envelope_and_ingest(_make_signal(streak=3))
    assert router.ingest.await_count == 1

    await sensor._signal_to_envelope_and_ingest(_make_signal(streak=4))
    assert router.ingest.await_count == 1


async def test_suppression_requires_enqueued_status():
    """If the first ingest returns 'queued_behind' the target is NOT
    marked in-flight, so the next signal for the same target is NOT
    suppressed - the router will handle the queued re-ingest.
    """
    router = _make_router("queued_behind")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    sig1 = _make_signal(streak=2)
    await sensor._signal_to_envelope_and_ingest(sig1)
    assert "tests/test_auth.py" not in sensor._pending_target_keys

    # Second signal must reach the router because target was never marked
    sig2 = _make_signal(streak=3)
    env2 = await sensor._signal_to_envelope_and_ingest(sig2)
    assert env2 is not None
    assert router.ingest.await_count == 2


async def test_dedup_enabled_by_default(monkeypatch: pytest.MonkeyPatch):
    """With no env override the module-level TTL must be positive,
    confirming dedup is active out of the box.
    """
    monkeypatch.delenv("JARVIS_TEST_FAILURE_INFLIGHT_TTL_S", raising=False)
    importlib.reload(tfs)
    try:
        assert tfs._INFLIGHT_TTL_S > 0, "dedup must be enabled by default"
    finally:
        importlib.reload(tfs)
