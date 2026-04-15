# [Ouroboros] Written by Ouroboros (op=op-019d9368-) at 2026-04-15 UTC
# [Ouroboros] Modified by Ouroboros (op=op-019d9368-) at 2026-04-15 23:23 UTC
# Reason: Write four focused sensor-level test modules for the TestFailureSensor in-flight dedup mechanism shipped in commit 20baa

# Reason: Module D - in-flight marker is refreshed on successful enqueue.

"""Module D: Marker refresh - the in-flight marker timestamp is set (or
refreshed) on every successful enqueue so that the TTL window is anchored
to the most recent submission, not the first one.

Also covers: marker is NOT set when router returns a non-enqueued status,
and release_target() clears the marker so the next signal is re-admitted.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

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


@pytest.mark.asyncio
async def test_marker_set_after_successful_enqueue() -> None:
    """After router.ingest returns 'enqueued', the target file must appear
    in _pending_target_keys with a recent monotonic timestamp.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    before = time.monotonic()
    await sensor._signal_to_envelope_and_ingest(_make_signal(streak=2))
    after = time.monotonic()

    assert "tests/test_auth.py" in sensor._pending_target_keys, (
        "target must be marked in-flight after successful enqueue"
    )
    ts = sensor._pending_target_keys["tests/test_auth.py"]
    assert before <= ts <= after, (
        "marker timestamp must be within the window of the enqueue call"
    )


@pytest.mark.asyncio
async def test_marker_not_set_when_router_returns_queued_behind() -> None:
    """When router.ingest returns 'queued_behind', the target must NOT be
    marked in-flight - the router will re-ingest it later and that
    re-ingest must not be self-suppressed by sensor-side dedup.
    """
    router = _make_router("queued_behind")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    await sensor._signal_to_envelope_and_ingest(_make_signal(streak=2))

    assert "tests/test_auth.py" not in sensor._pending_target_keys, (
        "target must NOT be marked when router returns queued_behind"
    )


@pytest.mark.asyncio
async def test_marker_not_set_when_router_returns_deduplicated() -> None:
    """When router.ingest returns 'deduplicated', the target must NOT be
    marked in-flight.
    """
    router = _make_router("deduplicated")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    await sensor._signal_to_envelope_and_ingest(_make_signal(streak=2))

    assert "tests/test_auth.py" not in sensor._pending_target_keys, (
        "target must NOT be marked when router returns deduplicated"
    )


@pytest.mark.asyncio
async def test_marker_cleared_by_release_target() -> None:
    """release_target() must remove the marker so the next signal for
    the same target is re-admitted without waiting for TTL expiry.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    await sensor._signal_to_envelope_and_ingest(_make_signal(streak=2))
    assert "tests/test_auth.py" in sensor._pending_target_keys

    sensor.release_target("tests/test_auth.py")
    assert "tests/test_auth.py" not in sensor._pending_target_keys, (
        "release_target must clear the in-flight marker"
    )

    # Next signal must now be re-admitted
    env2 = await sensor._signal_to_envelope_and_ingest(_make_signal(streak=3))
    assert env2 is not None, "signal must be re-admitted after release_target"
    assert router.ingest.await_count == 2


@pytest.mark.asyncio
async def test_marker_timestamp_is_monotonic() -> None:
    """The marker timestamp stored in _pending_target_keys must be a
    monotonic clock value (time.monotonic), not wall-clock time, so it
    is immune to system clock adjustments.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    before = time.monotonic()
    await sensor._signal_to_envelope_and_ingest(_make_signal(streak=2))
    after = time.monotonic()

    ts = sensor._pending_target_keys.get("tests/test_auth.py")
    assert ts is not None
    # A wall-clock timestamp (time.time()) would be orders of magnitude
    # larger than a monotonic one on any system running for < ~30 years.
    # We verify the value is within the monotonic window.
    assert before <= ts <= after, (
        "marker must use time.monotonic(), not time.time()"
    )


@pytest.mark.asyncio
async def test_unstable_signal_does_not_set_marker() -> None:
    """An unstable signal is rejected before envelope creation and must
    never set the in-flight marker.
    """
    router = _make_router("enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)

    result = await sensor._signal_to_envelope_and_ingest(_make_signal(stable=False, streak=2))
    assert result is None
    assert "tests/test_auth.py" not in sensor._pending_target_keys, (
        "unstable signal must not set the in-flight marker"
    )
    router.ingest.assert_not_called()
